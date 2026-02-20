#!/usr/bin/env python3
"""
Pawprint - APRS web interface for Direwolf/AllStarLink nodes
KI9NG - Node 604011
"""

import json
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import aprslib
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

# ─── Configuration ────────────────────────────────────────────────────────────

MYCALL          = "KI9NG-10"
AGW_HOST        = "localhost"
AGW_PORT        = 8080
APRS_IS_HOST    = "noam.aprs2.net"
APRS_IS_PORT    = 14580
APRS_IS_PASS    = ""  # Set by installer from direwolf.conf
DIREWOLF_CONF   = "/etc/direwolf.conf"
DATA_DIR        = Path("/var/lib/pawprint")
STATIONS_FILE   = DATA_DIR / "stations.json"
MESSAGES_FILE   = DATA_DIR / "messages.json"
TRACKS_FILE     = DATA_DIR / "tracks.json"
PAWPRINT_CFG    = DATA_DIR / "pawprint.json"

def _resolve_data_dir():
    """Use /var/lib/pawprint if writable, otherwise fall back to ./data next to app.py."""
    global DATA_DIR, STATIONS_FILE, MESSAGES_FILE
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write_test"
        probe.touch()
        probe.unlink()
    except OSError:
        fallback = Path(__file__).parent / "data"
        log.warning("Cannot write to %s — using fallback %s", DATA_DIR, fallback)
        DATA_DIR      = fallback
        STATIONS_FILE = DATA_DIR / "stations.json"
        MESSAGES_FILE = DATA_DIR / "messages.json"
        TRACKS_FILE   = DATA_DIR / "tracks.json"
        PAWPRINT_CFG  = DATA_DIR / "pawprint.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
VOICEAPRS_LOG   = Path("/var/log/voiceaprs-messages.log")
MAX_STATIONS    = 500
TRACK_MAX_PTS   = 2016        # max points per track (2016 @ 5-min intervals ≈ 7 days)
DEFAULT_FILTER_RADIUS = 50    # km

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("pawprint")

app = Flask(__name__)

# Trust X-Forwarded-* headers from Apache reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_prefix=1)

# ─── Shared State ─────────────────────────────────────────────────────────────

state = {
    "stations":            {},      # callsign -> station dict
    "messages":            [],      # received/sent APRS messages
    "own_position":        None,    # {"lat": float, "lon": float}
    "filter_radius":       DEFAULT_FILTER_RADIUS,
    "filter_center":       None,    # {"lat": float, "lon": float} last filter sent
    "aprs_is_connected":   False,
    "agw_connected":       False,
    "tracks":              {},      # callsign -> list of {lat,lon,ts}
    "station_max_age_hours": 168,   # cull stations not heard within this window (hours)
}
state_lock = threading.Lock()

# SSE subscriber queues — one per connected browser tab
sse_queues = []
sse_lock   = threading.Lock()

msg_seq = 0   # outgoing message sequence number
msg_seq_lock = threading.Lock()

# ─── Persistence ──────────────────────────────────────────────────────────────

def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def load_pawprint_cfg():
    """Load Pawprint-specific settings from pawprint.json and apply to state.
    Falls back to defaults if the file doesn't exist yet.
    Handles migration from old station_max_age_days key.
    """
    try:
        with open(PAWPRINT_CFG) as f:
            cfg = json.load(f)
        with state_lock:
            if "station_max_age_hours" in cfg:
                state["station_max_age_hours"] = max(1, int(cfg["station_max_age_hours"]))
            elif "station_max_age_days" in cfg:
                # Migrate from old key
                state["station_max_age_hours"] = max(1, int(cfg["station_max_age_days"])) * 24
            if "filter_radius" in cfg:
                state["filter_radius"] = max(10, int(cfg["filter_radius"]))
        log.info("Loaded pawprint config: station_max_age_hours=%d filter_radius=%d",
                 state["station_max_age_hours"], state["filter_radius"])
    except FileNotFoundError:
        pass  # first run — defaults stay in place
    except Exception as e:
        log.warning("Could not load pawprint config: %s", e)

def save_pawprint_cfg():
    """Persist Pawprint-specific settings to pawprint.json."""
    with state_lock:
        cfg = {
            "station_max_age_hours": state["station_max_age_hours"],
            "filter_radius":         state["filter_radius"],
        }
    try:
        with open(PAWPRINT_CFG, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.warning("Could not save pawprint config: %s", e)

def load_stations():
    if not STATIONS_FILE.exists():
        return
    try:
        with open(STATIONS_FILE) as f:
            data = json.load(f)
        now = time.time()
        with state_lock:
            max_age = state["station_max_age_hours"] * 3600
            for call, s in data.items():
                age = now - s.get("last_heard_ts", 0)
                if age < max_age:
                    state["stations"][call] = s
        log.info("Loaded %d stations from disk", len(state["stations"]))
    except Exception as e:
        log.warning("Could not load stations file: %s", e)

def save_stations():
    ensure_data_dir()
    try:
        with state_lock:
            # keep only the most recent MAX_STATIONS by last_heard_ts
            stations = dict(
                sorted(
                    state["stations"].items(),
                    key=lambda x: x[1].get("last_heard_ts", 0),
                    reverse=True
                )[:MAX_STATIONS]
            )
        with open(STATIONS_FILE, "w") as f:
            json.dump(stations, f)
    except Exception as e:
        log.warning("Could not save stations: %s", e)

def cull_stations():
    """Remove stations older than station_max_age_hours from the live state,
    push a station_remove SSE event for each one, prune their tracks, then persist.
    Safe to call at any time — from the API handler or the background cull thread.
    Returns the number of stations removed.
    """
    with state_lock:
        max_age = state["station_max_age_hours"] * 3600
        cutoff  = time.time() - max_age
        stale   = [call for call, s in state["stations"].items()
                   if s.get("last_heard_ts", 0) < cutoff]
        for call in stale:
            del state["stations"][call]
            state["tracks"].pop(call, None)

    for call in stale:
        push_event("station_remove", {"callsign": call})

    if stale:
        log.info("Culled %d stale station(s): %s", len(stale), ", ".join(stale))
        save_stations()
        save_tracks()

    return len(stale)

def cull_loop():
    """Background thread: cull stale stations once per hour."""
    while True:
        time.sleep(3600)
        try:
            cull_stations()
        except Exception as e:
            log.warning("cull_loop error: %s", e)

def load_messages():
    if not MESSAGES_FILE.exists():
        return
    try:
        with open(MESSAGES_FILE) as f:
            data = json.load(f)
        with state_lock:
            state["messages"] = data[-200:]   # cap to last 200
        log.info("Loaded %d messages from disk", len(state["messages"]))
    except Exception as e:
        log.warning("Could not load messages: %s", e)

def save_messages():
    ensure_data_dir()
    try:
        with state_lock:
            msgs = state["messages"][-200:]
        with open(MESSAGES_FILE, "w") as f:
            json.dump(msgs, f)
    except Exception as e:
        log.warning("Could not save messages: %s", e)

def load_voiceaprs_message_log():
    """Disabled — voiceaprs log contains internal ACK lines, not real incoming messages."""
    return

def load_tracks():
    if not TRACKS_FILE.exists():
        return
    try:
        with open(TRACKS_FILE) as f:
            data = json.load(f)
        with state_lock:
            max_age = state["station_max_age_hours"] * 3600
        cutoff = time.time() - max_age
        with state_lock:
            for call, pts in data.items():
                fresh = [p for p in pts if p.get("ts", 0) > cutoff]
                if fresh:
                    state["tracks"][call] = fresh
        log.info("Loaded tracks for %d stations", len(state["tracks"]))
    except Exception as e:
        log.warning("Could not load tracks: %s", e)

def save_tracks():
    ensure_data_dir()
    try:
        with state_lock:
            tracks = dict(state["tracks"])
        with open(TRACKS_FILE, "w") as f:
            json.dump(tracks, f)
    except Exception as e:
        log.warning("Could not save tracks: %s", e)

def add_track_point(callsign, lat, lon, ts):
    """Append a position fix to a station's track. Skips duplicates, trims old points.
    Returns True if a new point was added, False if it was a duplicate skip.
    """
    with state_lock:
        pts = state["tracks"].get(callsign, [])
        if pts:
            last = pts[-1]
            if abs(last["lat"] - lat) < 0.0001 and abs(last["lon"] - lon) < 0.0001:
                return False  # same location, skip
        pts.append({"lat": lat, "lon": lon, "ts": ts})
        max_age = state["station_max_age_hours"] * 3600
        cutoff = ts - max_age
        pts = [p for p in pts if p["ts"] > cutoff]
        if len(pts) > TRACK_MAX_PTS:
            pts = pts[-TRACK_MAX_PTS:]
        state["tracks"][callsign] = pts
    return True


# ─── SSE Helpers ──────────────────────────────────────────────────────────────

def push_event(event_type: str, data: dict):
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_queues:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_queues.remove(q)

# ─── APRS Packet Processing ───────────────────────────────────────────────────

def parse_aprs_position(info: str):
    """Return (lat, lon) or (None, None) from an APRS info field.

    Handles uncompressed APRS position format: !DDMM.mmN/DDDMM.mmW
    The character between [NS] and the longitude digits is the APRS symbol
    table identifier (/, \\, or an overlay character A-Za-z0-9).  Using a
    literal '.' here is the classic erratic-position bug: it matches any
    character including digits, which can cause the regex to latch onto an
    entirely wrong part of the packet and produce garbage coordinates.
    """
    m = re.search(
        r'[!=/@](\d{2})(\d{2}\.\d+)([NS])[\/\\A-Za-z0-9](\d{3})(\d{2}\.\d+)([EW])', info
    )
    if m:
        lat = int(m.group(1)) + float(m.group(2)) / 60.0
        if m.group(3) == 'S':
            lat = -lat
        lon = int(m.group(4)) + float(m.group(5)) / 60.0
        if m.group(6) == 'W':
            lon = -lon
        # Sanity-check: reject obviously invalid coordinates
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon
    return None, None

def extract_aprs_symbol(info: str):
    """Return (table, symbol) chars from info field, or ('/', '>') default."""
    # Position with symbol: !DDMM.mmN T SDDMM.mmW  (T=table, S=symbol)
    m = re.search(
        r'[!=/@]\d{4}\.\d+[NS](.).\d{5}\.\d+[EW](.)', info
    )
    if m:
        return m.group(1), m.group(2)
    return '/', '>'

def process_packet(raw: str):
    """
    Parse a raw APRS packet string, update station store, push SSE event.
    raw format: "FROM>TO,PATH:INFO"
    """
    if not raw or '>' not in raw:
        return

    try:
        parsed = aprslib.parse(raw)
    except Exception:
        parsed = {}

    # Extract fields with fallbacks
    from_call = parsed.get("from", raw.split(">")[0].strip())
    to_call   = parsed.get("to",   "")
    lat       = parsed.get("latitude")
    lon       = parsed.get("longitude")
    comment   = parsed.get("comment", parsed.get("text", ""))
    symbol_t  = parsed.get("symbol_table", "/")
    symbol_c  = parsed.get("symbol",       ">")
    pkt_type  = parsed.get("format",       "unknown")

    # ── APRS Object packet handling ────────────────────────────────────────
    # Object packets (format=="object") use the transmitting gateway as FROM,
    # but the real station identity is the object_name embedded in the INFO field.
    # e.g. WINLINK>APWL2K,...:;W9ML-10  *...  → callsign should be W9ML-10
    # Also handle aprslib parse failure fallback: if INFO starts with ';', extract
    # bytes 1-9 directly as the object name.
    object_name = parsed.get("object_name", "").strip()
    if not object_name and ":" in raw:
        info = raw.split(":", 1)[1]
        if info.startswith(";"):
            object_name = info[1:10].strip()
    is_object = bool(object_name)
    gateway   = from_call if is_object else None
    if is_object:
        from_call = object_name

    # Fallback position parse if aprslib missed it
    if lat is None and ":" in raw:
        info = raw.split(":", 1)[1]
        lat, lon = parse_aprs_position(info)
        if lat is not None:
            st, sc    = extract_aprs_symbol(info)
            symbol_t  = st
            symbol_c  = sc

    # Sanity-check coordinates from aprslib (compressed packets can occasionally
    # produce out-of-range values on partial parse failures)
    if lat is not None and (lat < -90.0 or lat > 90.0 or lon is None or lon < -180.0 or lon > 180.0):
        log.warning("Discarding invalid coordinates from %s: lat=%s lon=%s", from_call, lat, lon)
        lat, lon = None, None

    now_ts  = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Own position tracking ──────────────────────────────────────────────
    if from_call == MYCALL and lat is not None:
        with state_lock:
            state["own_position"] = {"lat": lat, "lon": lon}
        maybe_update_filter(lat, lon)
        # Push position update to all connected clients so map follows
        push_event("position", {"lat": lat, "lon": lon})
        log.info("Updated own position: %.5f, %.5f", lat, lon)

    # ── APRS message handling ──────────────────────────────────────────────
    if pkt_type == "message":
        addressee = parsed.get("addresse", "").strip()
        msg_text  = parsed.get("message_text", "")
        msg_id    = parsed.get("msgNo", "")
        is_ack    = parsed.get("response") == "ack"

        # Handle ACK for our sent messages — update status, never display as a message
        if is_ack:
            with state_lock:
                for m in state["messages"]:
                    if m.get("direction") == "tx" and m.get("msg_id") == msg_id:
                        m["status"] = "acked"
                        push_event("ack", {"msg_id": msg_id})
                        break
            save_messages()
            return

        # Only show real messages addressed to us, skip ACKs and other control packets
        if addressee.upper() == MYCALL.upper() and msg_text and not msg_text.startswith("ack"):
            entry = {
                "direction": "rx",
                "from_call": from_call,
                "to_call":   MYCALL,
                "text":      msg_text,
                "msg_id":    msg_id,
                "ts":        now_ts,
                "ts_iso":    now_iso,
                "status":    "received",
            }
            with state_lock:
                state["messages"].append(entry)
            save_messages()
            push_event("message", entry)

        return  # don't add message packets to station list as position beacons

    # ── Station store update ───────────────────────────────────────────────
    if from_call == MYCALL:
        return   # don't clutter list with our own beacons

    with state_lock:
        existing = state["stations"].get(from_call, {})
        station = {
            "callsign":      from_call,
            "to":            to_call,
            "lat":           lat if lat is not None else existing.get("lat"),
            "lon":           lon if lon is not None else existing.get("lon"),
            "comment":       comment or existing.get("comment", ""),
            "symbol_table":  symbol_t,
            "symbol":        symbol_c,
            "type":          pkt_type,
            "is_object":     is_object,
            "gateway":       gateway,
            "last_heard_ts": now_ts,
            "last_heard":    now_iso,
            "packet_count":  existing.get("packet_count", 0) + 1,
            "raw":           raw,
        }
        state["stations"][from_call] = station

    # Record position in track history — objects are fixed infrastructure, skip tracks
    if lat is not None and lon is not None and not is_object:
        added = add_track_point(from_call, lat, lon, now_ts)
        if added:
            push_event("track_point", {"callsign": from_call, "lat": lat, "lon": lon, "ts": now_ts})
            save_tracks()

    save_stations()
    push_event("station", station)

# ─── APRS-IS Receive Thread ───────────────────────────────────────────────────

def aprs_is_thread():
    """
    Maintains a read-only APRS-IS connection for receiving filtered packets.
    Reconnects automatically. Updates filter when own position changes.
    """
    while True:
        try:
            log.info("Connecting to APRS-IS %s:%d", APRS_IS_HOST, APRS_IS_PORT)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(60)
            sock.connect((APRS_IS_HOST, APRS_IS_PORT))
            f = sock.makefile("r", encoding="latin-1")

            # Read banner
            banner = f.readline()
            log.info("APRS-IS banner: %s", banner.strip())

            # Login
            login_str = (
                f"user {MYCALL} pass {APRS_IS_PASS} "
                f"vers pawprint 2.4 filter r/41.54/-87.14/{DEFAULT_FILTER_RADIUS}\r\n"
            )
            sock.sendall(login_str.encode())
            log.info("APRS-IS login sent")

            # Share the live socket so agw_send_message can inject packets on it
            global _aprs_is_sock
            with _aprs_is_sock_lock:
                _aprs_is_sock = sock

            with state_lock:
                state["aprs_is_connected"] = True
                fc = state.get("filter_center")

            push_event("status", {"aprs_is_connected": True})

            last_filter_check = time.time()

            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    # Server comment — check for login ok
                    if "verified" in line.lower() or "unverified" in line.lower():
                        log.info("APRS-IS: %s", line)
                    continue

                process_packet(line)

                # Periodically check if filter needs updating
                now = time.time()
                if now - last_filter_check > 30:
                    last_filter_check = now
                    with state_lock:
                        pos = state.get("own_position")
                        fc  = state.get("filter_center")
                        radius = state.get("filter_radius", DEFAULT_FILTER_RADIUS)
                    if pos and _should_update_filter(pos, fc):
                        filt = f"#filter r/{pos['lat']:.4f}/{pos['lon']:.4f}/{radius}\r\n"
                        try:
                            sock.sendall(filt.encode())
                            with state_lock:
                                state["filter_center"] = {"lat": pos["lat"], "lon": pos["lon"]}
                            log.info("Updated APRS-IS filter: %s", filt.strip())
                        except Exception as e:
                            log.warning("Filter update failed: %s", e)

        except Exception as e:
            log.warning("APRS-IS connection error: %s — retrying in 30s", e)
            with _aprs_is_sock_lock:
                _aprs_is_sock = None
            with state_lock:
                state["aprs_is_connected"] = False
            push_event("status", {"aprs_is_connected": False})
            time.sleep(30)

def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _should_update_filter(pos, last_center, threshold_km=2.0):
    if last_center is None:
        return True
    dist = _haversine_km(pos["lat"], pos["lon"], last_center["lat"], last_center["lon"])
    return dist > threshold_km

def push_filter_now(lat, lon, radius):
    """Immediately send a #filter command on the live APRS-IS socket."""
    filt = f"#filter r/{lat:.4f}/{lon:.4f}/{radius}\r\n"
    with _aprs_is_sock_lock:
        s = _aprs_is_sock
    if s is None:
        log.warning("push_filter_now: no live socket yet")
        return False
    try:
        s.sendall(filt.encode("latin-1"))
        with state_lock:
            state["filter_center"] = {"lat": lat, "lon": lon}
        log.info("Filter pushed immediately: %s", filt.strip())
        return True
    except Exception as e:
        log.warning("push_filter_now failed: %s", e)
        return False

def maybe_update_filter(lat, lon):
    """Send a #filter update to APRS-IS if position has moved enough to warrant it."""
    with state_lock:
        fc     = state.get("filter_center")
        radius = state.get("filter_radius", DEFAULT_FILTER_RADIUS)
    if _should_update_filter({"lat": lat, "lon": lon}, fc):
        push_filter_now(lat, lon, radius)  # push_filter_now updates filter_center on success

# ─── AGW Thread (Send Only) ───────────────────────────────────────────────────

agw_sock      = None
agw_sock_lock = threading.Lock()

def agw_connect():
    global agw_sock
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((AGW_HOST, AGW_PORT))
        # Register our callsign — AGW frame: port=0, type='X', call=MYCALL
        call_padded = MYCALL.ljust(10).encode()
        header = struct.pack("<BBBBBBBB10sH10sH",
            0, 0, 0, 0,   # port, reserved, reserved, reserved
            ord('X'), 0,  # DataKind, reserved
            0, 0,         # PID, reserved
            call_padded, 0,  # CallFrom, reserved
            b''.ljust(10), 0  # CallTo, reserved
        )
        # Simpler: send minimal AGW register frame
        # Format: 36-byte header, DataKind='X', CallFrom=MYCALL, DataLen=0
        frame = bytearray(36)
        frame[4] = ord('X')
        frame[8:8+len(MYCALL)] = MYCALL.encode()
        s.sendall(bytes(frame))
        with agw_sock_lock:
            agw_sock = s
        with state_lock:
            state["agw_connected"] = True
        push_event("status", {"agw_connected": True})
        log.info("AGW connected to %s:%d", AGW_HOST, AGW_PORT)
        return True
    except Exception as e:
        log.warning("AGW connect failed: %s", e)
        with state_lock:
            state["agw_connected"] = False
        return False

def agw_send_message(to_call: str, text: str, msg_id: str) -> bool:
    """Send an APRS message via the live APRS-IS connection (primary) or a fresh connection (fallback)."""
    to_padded   = to_call.ljust(9)[:9]
    aprs_info   = f":{to_padded}:{text}{{{msg_id}}}"
    aprs_packet = f"{MYCALL}>APRS,TCPIP*:{aprs_info}"

    # Try injecting into the live APRS-IS socket first
    if _aprs_is_send_on_live_socket(aprs_packet):
        return True
    # Fallback: open a fresh verified connection
    return aprs_is_send_message(aprs_packet)

# Live APRS-IS socket reference — set by aprs_is_thread
_aprs_is_sock = None
_aprs_is_sock_lock = threading.Lock()

def _aprs_is_send_on_live_socket(aprs_packet: str) -> bool:
    """Inject a packet on the already-open, already-verified APRS-IS socket."""
    global _aprs_is_sock
    with _aprs_is_sock_lock:
        if _aprs_is_sock is None:
            return False
        try:
            _aprs_is_sock.sendall((aprs_packet + "\r\n").encode("latin-1"))
            log.info("Sent via live APRS-IS socket: %s", aprs_packet)
            return True
        except Exception as e:
            log.warning("Live APRS-IS send failed: %s", e)
            _aprs_is_sock = None
            return False

def aprs_is_send_message(aprs_packet: str) -> bool:
    """Open a fresh verified APRS-IS connection just to send one packet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect((APRS_IS_HOST, APRS_IS_PORT))
        f = s.makefile("r", encoding="latin-1")
        f.readline()  # banner
        login = f"user {MYCALL} pass {APRS_IS_PASS} vers pawprint 1.0\r\n"
        s.sendall(login.encode())
        time.sleep(1.5)  # wait for logresp
        s.sendall((aprs_packet + "\r\n").encode("latin-1"))
        s.close()
        log.info("Sent via fresh APRS-IS connection: %s", aprs_packet)
        return True
    except Exception as e:
        log.error("APRS-IS send failed: %s", e)
        return False

# ─── Config Helpers ───────────────────────────────────────────────────────────

def read_config():
    """Parse direwolf.conf and return relevant fields."""
    try:
        with open(DIREWOLF_CONF) as f:
            content = f.read()
    except PermissionError:
        return {"error": "Permission denied reading direwolf.conf"}

    cfg = {
        "mycall":        "",
        "symbol":        "car",
        "comment":       "",
        "smartbeaconing": {},
        "igfilter":      "",
        "raw":           content,
    }

    for line in content.splitlines():
        line = line.strip()
        if line.startswith('#'):
            continue
        if line.upper().startswith("MYCALL "):
            cfg["mycall"] = line.split(None, 1)[1].strip()
        elif line.upper().startswith("TBEACON") or line.upper().startswith("PBEACON"):
            sym_char  = ""
            sym_table = "/"
            m = re.search(r'symbol\s*=\s*"([^"]+)"', line, re.I)
            if m:
                sym_char = m.group(1)
            m2 = re.search(r'symbol_table\s*=\s*"([^"]+)"', line, re.I)
            if m2:
                sym_table = m2.group(1)
            if sym_char:
                cfg["symbol"] = sym_table + sym_char
            m = re.search(r'comment\s*=\s*"([^"]+)"', line, re.I)
            if m:
                cfg["comment"] = m.group(1)
        elif re.match(r'SMARTBEACONING\s', line, re.I):
            parts = line.split()
            if len(parts) >= 8:
                cfg["smartbeaconing"] = {
                    "fast_speed": parts[1],
                    "fast_rate":  parts[2],
                    "slow_speed": parts[3],
                    "slow_rate":  parts[4],
                    "min_turn":   parts[5],
                    "turn_angle": parts[6],
                    "max_rate":   parts[7],
                }
        elif line.upper().startswith("IGFILTER"):
            cfg["igfilter"] = line.split(None, 1)[1].strip() if len(line.split()) > 1 else ""

    return cfg

def write_config(updates: dict) -> dict:
    """
    Update specific fields in direwolf.conf.
    updates may contain: symbol, comment, smartbeaconing (dict), igfilter
    """
    try:
        with open(DIREWOLF_CONF) as f:
            lines = f.readlines()
    except PermissionError:
        return {"ok": False, "error": "Permission denied"}

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#'):
            new_lines.append(line)
            continue

        # Symbol / comment in TBEACON or PBEACON
        if (re.match(r'(TBEACON|PBEACON)\s', stripped, re.I)
                and ("symbol" in updates or "comment" in updates)):
            if "symbol" in updates:
                raw_sym = updates["symbol"]
                # Accept both "table+char" format (e.g. "/>") and legacy names (e.g. "car")
                LEGACY_SYMBOLS = {
                    "car": "/>", "truck": "/k", "van": "/v", "jeep": "/j",
                    "house": "/-", "bike": "/b", "ship": "/s", "aircraft": "/^",
                    "weather": "/_", "digi": "/#", "igate": "/&", "ambulance": "/a",
                }
                sym = LEGACY_SYMBOLS.get(raw_sym, raw_sym)
                # sym should now be 2 chars: table + symbol
                if len(sym) == 2:
                    sym_table = sym[0]
                    sym_char  = sym[1]
                    # direwolf uses SYMBOL and SYMBOL_TABLE or combined in overlay
                    if re.search(r'symbol\s*=', line, re.I):
                        line = re.sub(r'symbol\s*=\s*"[^"]*"', f'symbol="{sym_char}"', line, flags=re.I)
                        line = re.sub(r"symbol\s*=\s*'[^']*'", f'symbol="{sym_char}"', line, flags=re.I)
                    else:
                        line = line.rstrip() + f' symbol="{sym_char}"\n'
                    # Also handle SYMBOL_TABLE
                    if re.search(r'symbol_table\s*=', line, re.I):
                        line = re.sub(r'symbol_table\s*=\s*"[^"]*"', f'symbol_table="{sym_table}"', line, flags=re.I)
                    else:
                        line = line.rstrip().rstrip('\n') + f' symbol_table="{sym_table}"\n'
                else:
                    # Fallback: write as-is
                    if re.search(r'symbol\s*=', line, re.I):
                        line = re.sub(r'symbol\s*=\s*"[^"]*"', f'symbol="{sym}"', line)
                    else:
                        line = line.rstrip() + f' symbol="{sym}"\n'
            if "comment" in updates:
                cmt = updates["comment"]
                if re.search(r'comment\s*=', line, re.I):
                    line = re.sub(r'comment\s*=\s*"[^"]*"', f'comment="{cmt}"', line)
                    line = re.sub(r"comment\s*=\s*'[^']*'", f'comment="{cmt}"', line)
                else:
                    line = line.rstrip() + f' comment="{cmt}"\n' 

        # SmartBeaconing
        elif re.match(r'SMARTBEACONING\s', stripped, re.I) and "smartbeaconing" in updates:
            sb = updates["smartbeaconing"]
            line = (
                f"SMARTBEACONING {sb['fast_speed']} {sb['fast_rate']} "
                f"{sb['slow_speed']} {sb['slow_rate']} "
                f"{sb['min_turn']} {sb['turn_angle']} {sb['max_rate']}\n"
            )

        # IGFILTER
        elif stripped.upper().startswith("IGFILTER") and "igfilter" in updates:
            line = f"IGFILTER {updates['igfilter']}\n"

        new_lines.append(line)

    # Add IGFILTER line if it didn't exist yet
    if "igfilter" in updates and not any(
        re.match(r'IGFILTER\s', l.strip(), re.I) for l in new_lines
    ):
        new_lines.append(f"IGFILTER {updates['igfilter']}\n")

    try:
        with open(DIREWOLF_CONF, "w") as f:
            f.writelines(new_lines)
        return {"ok": True}
    except PermissionError:
        return {"ok": False, "error": "Permission denied writing direwolf.conf"}

# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "static"), filename)


# SPRITE_URLS removed — sprites embedded in HTML as base64

# download_sprites_background() removed — sprites embedded in HTML


# /api/download_sprites removed


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify({
            "aprs_is_connected":   state["aprs_is_connected"],
            "agw_connected":       state["agw_connected"],
            "station_count":       len(state["stations"]),
            "own_position":        state["own_position"],
            "filter_radius":        state["filter_radius"],
            "filter_center":        state["filter_center"],
            "station_max_age_hours": state["station_max_age_hours"],
        })

@app.route("/api/stations")
def api_stations():
    with state_lock:
        stations = list(state["stations"].values())
    stations.sort(key=lambda s: s.get("last_heard_ts", 0), reverse=True)
    return jsonify(stations)

@app.route("/api/messages")
def api_messages():
    with state_lock:
        return jsonify(list(state["messages"]))

@app.route("/api/send_message", methods=["POST"])
def api_send_message():
    global msg_seq
    data    = request.json or {}
    to_call = data.get("to_call", "").strip().upper()
    text    = data.get("text", "").strip()

    if not to_call or not text:
        return jsonify({"ok": False, "error": "to_call and text required"}), 400
    if len(text) > 67:
        return jsonify({"ok": False, "error": "Message too long (max 67 chars)"}), 400

    with msg_seq_lock:
        msg_seq += 1
        seq = msg_seq
    msg_id = str(seq)

    now_ts  = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    entry = {
        "direction": "tx",
        "from_call": MYCALL,
        "to_call":   to_call,
        "text":      text,
        "msg_id":    msg_id,
        "ts":        now_ts,
        "ts_iso":    now_iso,
        "status":    "sending",
    }

    with state_lock:
        state["messages"].append(entry)
    save_messages()
    push_event("message", entry)

    # Send in background so HTTP response is immediate
    def do_send():
        ok = agw_send_message(to_call, text, msg_id)
        with state_lock:
            for m in state["messages"]:
                if m.get("msg_id") == msg_id:
                    m["status"] = "sent" if ok else "failed"
                    push_event("message_status", {"msg_id": msg_id, "status": m["status"]})
                    break
        save_messages()

    threading.Thread(target=do_send, daemon=True).start()
    return jsonify({"ok": True, "msg_id": msg_id})

@app.route("/api/cull_all", methods=["POST"])
def api_cull_all():
    """Remove ALL heard stations and their tracks from live state immediately."""
    with state_lock:
        all_calls = list(state["stations"].keys())
        state["stations"].clear()
        state["tracks"].clear()

    for call in all_calls:
        push_event("station_remove", {"callsign": call})

    if all_calls:
        save_stations()
        save_tracks()
        log.info("Cull all: removed %d station(s)", len(all_calls))

    return jsonify({"ok": True, "removed": len(all_calls)})

@app.route("/api/config")
def api_config():
    return jsonify(read_config())

@app.route("/api/config", methods=["POST"])
def api_config_post():
    updates = request.json or {}
    pawprint_cfg_changed = False

    # Pawprint-specific settings — handled here, not written to direwolf.conf
    if "station_max_age_hours" in updates:
        try:
            hours = max(1, int(updates["station_max_age_hours"]))
            with state_lock:
                state["station_max_age_hours"] = hours
            pawprint_cfg_changed = True
            log.info("station_max_age_hours updated to %d", hours)
            # Apply immediately — evict stale stations from live state right now
            culled = cull_stations()
            log.info("Immediate cull after age change: removed %d station(s)", culled)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "station_max_age_hours must be an integer"}), 400

    # If filter radius changed, update state AND push new filter immediately
    if "filter_radius" in updates:
        try:
            r = int(updates["filter_radius"])
            with state_lock:
                state["filter_radius"] = r
                pos = state.get("own_position")
            if pos:
                push_filter_now(pos["lat"], pos["lon"], r)
            else:
                log.warning("Filter radius updated but no own_position yet — filter will apply on next position update")
            pawprint_cfg_changed = True
        except ValueError:
            pass

    # Persist Pawprint-specific settings whenever either value changed
    if pawprint_cfg_changed:
        save_pawprint_cfg()

    # Only call write_config for keys that actually belong in direwolf.conf
    direwolf_keys = {"symbol", "comment", "smartbeaconing", "igfilter"}
    direwolf_updates = {k: v for k, v in updates.items() if k in direwolf_keys}
    result = write_config(direwolf_updates) if direwolf_updates else {"ok": True}

    # If it was a pawprint-only update (no direwolf keys), still return ok
    if pawprint_cfg_changed and not direwolf_updates:
        return jsonify({"ok": True})

    # Return the actual config read back from disk so the UI shows what was really saved
    if result.get("ok"):
        result["config"] = read_config()
    return jsonify(result)

@app.route("/api/beacon_now", methods=["POST"])
def api_beacon_now():
    """Force an immediate APRS position beacon via APRS-IS."""
    with state_lock:
        pos = state.get("own_position")

    if pos is None:
        return jsonify({"ok": False, "error": "No position known yet — waiting for GPS beacon"}), 400

    cfg = read_config()
    symbol_t = "/"
    symbol_c = ">"
    sym = cfg.get("symbol", "car")
    # Map common direwolf symbol names to APRS table/char
    sym_map = {
        "car": ("/>", "/", ">"), "truck": ("/k", "/", "k"),
        "van":  ("/v", "/", "v"), "jeep": ("/j", "/", "j"),
        "house": ("/-", "/", "-"),
    }
    if sym in sym_map:
        _, symbol_t, symbol_c = sym_map[sym]
    elif len(sym) == 2:
        symbol_t, symbol_c = sym[0], sym[1]

    comment = cfg.get("comment", f"AllStar Node {MYCALL}")
    lat = pos["lat"]
    lon = pos["lon"]

    # Format lat/lon in APRS uncompressed format: DDMM.mmN/DDDMM.mmW
    lat_d = int(abs(lat))
    lat_m = (abs(lat) - lat_d) * 60
    lon_d = int(abs(lon))
    lon_m = (abs(lon) - lon_d) * 60
    lat_str = f"{lat_d:02d}{lat_m:05.2f}{'N' if lat >= 0 else 'S'}"
    lon_str = f"{lon_d:03d}{lon_m:05.2f}{'E' if lon >= 0 else 'W'}"

    info   = f"={lat_str}{symbol_t}{lon_str}{symbol_c}{comment}"
    packet = f"{MYCALL}>APRS,TCPIP*:{info}"

    ok = _aprs_is_send_on_live_socket(packet)
    if not ok:
        ok = aprs_is_send_message(packet)

    if ok:
        log.info("Beacon sent: %s", packet)
        return jsonify({"ok": True, "packet": packet})
    else:
        return jsonify({"ok": False, "error": "Send failed — check logs"}), 500


@app.route("/api/restart_direwolf", methods=["POST"])
def api_restart_direwolf():
    try:
        subprocess.run(
            ["sudo", "systemctl", "restart", "direwolf"],
            check=True, timeout=15
        )
        # Also restart voiceaprs-monitor so it reconnects cleanly
        subprocess.run(
            ["sudo", "systemctl", "restart", "voiceaprs-monitor"],
            check=True, timeout=15
        )
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Restart timed out"}), 500

@app.route("/api/tracks")
def api_tracks():
    """Track history for all stations, filtered by max_age seconds."""
    with state_lock:
        default_age = state["station_max_age_hours"] * 3600
    try:
        max_age = float(request.args.get("max_age", default_age))
    except (ValueError, TypeError):
        max_age = default_age
    cutoff = time.time() - max_age
    with state_lock:
        result = {
            call: pts
            for call, pts in state["tracks"].items()
            for pts in [[p for p in pts if p["ts"] >= cutoff]]
            if len(pts) >= 2
        }
    return jsonify(result)

@app.route("/api/stream")
def api_stream():
    """Server-Sent Events endpoint for live updates."""
    q = queue.Queue(maxsize=100)
    with sse_lock:
        sse_queues.append(q)

    def generate():
        # Send current state immediately on connect
        with state_lock:
            stations = list(state["stations"].values())
            pos      = state["own_position"]
            ai_conn  = state["aprs_is_connected"]
            agw_conn = state["agw_connected"]

        yield f"event: init\ndata: {json.dumps({'stations': stations, 'own_position': pos, 'aprs_is_connected': ai_conn, 'agw_connected': agw_conn})}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                if q in sse_queues:
                    sse_queues.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )

# ─── Direwolf Log Monitor ────────────────────────────────────────────────────

def direwolf_log_thread():
    """Tail Direwolf console log and capture own beacon positions."""
    import subprocess
    log_path = "/var/log/direwolf/direwolf_console.log"
    log.info("Starting Direwolf log monitor")

    # Match any Direwolf output channel: [ig] iGated, [0L] RF, [0H] heard, etc.
    # Also match both ! and = position type identifiers used by TBEACON/SMARTBEACONING.
    beacon_re = re.compile(
        r"\[\S+\]\s+" + re.escape(MYCALL) +
        r">[\w,-]+:[!=@]" +
        r"(\d{2})(\d{2}\.\d+)([NS])" +
        r"[\/\\A-Za-z0-9]" +
        r"(\d{3})(\d{2}\.\d+)([EW])"
    )

    def parse_pos(m):
        lat = int(m.group(1)) + float(m.group(2)) / 60
        if m.group(3) == "S": lat = -lat
        lon = int(m.group(4)) + float(m.group(5)) / 60
        if m.group(6) == "W": lon = -lon
        return lat, lon

    def try_seed_from_log():
        """Scan the last 200 lines of the existing log to seed own_position on startup."""
        try:
            result = subprocess.run(
                ["tail", "-n", "200", log_path],
                capture_output=True, timeout=5
            )
            lines = result.stdout.decode("utf-8", errors="ignore").splitlines()
            # Walk in reverse so we get the most recent beacon first
            for line in reversed(lines):
                m = beacon_re.search(line)
                if m:
                    lat, lon = parse_pos(m)
                    with state_lock:
                        state["own_position"] = {"lat": lat, "lon": lon}
                    log.info("Seeded own_position from log history: %.5f, %.5f", lat, lon)
                    push_event("position", {"lat": lat, "lon": lon})
                    maybe_update_filter(lat, lon)
                    return
            log.info("No own beacon found in recent log history — waiting for next beacon")
        except Exception as e:
            log.warning("Could not seed position from log: %s", e)

    # Seed position immediately from existing log so beacon_now works right away
    try_seed_from_log()

    while True:
        try:
            proc = subprocess.Popen(
                ["tail", "-F", "-n", "0", log_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            log.info("Watching Direwolf log: %s", log_path)
            for raw in proc.stdout:
                try:
                    line = raw.decode("utf-8", errors="ignore")
                    m = beacon_re.search(line)
                    if m:
                        lat, lon = parse_pos(m)
                        with state_lock:
                            state["own_position"] = {"lat": lat, "lon": lon}
                        log.info("Beacon captured: %.5f, %.5f", lat, lon)
                        push_event("position", {"lat": lat, "lon": lon})
                        now_ts = time.time()
                        added = add_track_point(MYCALL, lat, lon, now_ts)
                        if added:
                            push_event("track_point", {"callsign": MYCALL, "lat": lat, "lon": lon, "ts": now_ts})
                            save_tracks()
                        maybe_update_filter(lat, lon)
                except Exception as e:
                    log.warning("Log parse error: %s", e)
        except Exception as e:
            log.warning("Log tail error: %s - retrying in 15s", e)
            time.sleep(15)

# ─── Startup ──────────────────────────────────────────────────────────────────

def startup():
    _resolve_data_dir()   # pick writable data dir before anything else
    ensure_data_dir()
    load_pawprint_cfg()   # load station_max_age_hours + filter_radius before loading stations
    load_stations()
    load_messages()
    load_tracks()

    # Seed own_position from the hardcoded filter coordinates so the map
    # has something to center on before we hear our own beacon back.
    with state_lock:
        if state["own_position"] is None:
            state["own_position"] = None  # Will be set on first beacon
            log.info("Waiting for first Direwolf beacon to set position")

    agw_connect()

    t = threading.Thread(target=aprs_is_thread, daemon=True, name="aprs-is")
    t.start()
    
    # Start GPSD monitoring thread if available
    t_log = threading.Thread(target=direwolf_log_thread, daemon=True, name="direwolf-log")
    t_log.start()

    # Background hourly cull of stations that have aged out
    t_cull = threading.Thread(target=cull_loop, daemon=True, name="station-cull")
    t_cull.start()

    # Sprite download removed — sprites are embedded in HTML as base64

# startup() runs unconditionally so background threads launch whether
# this file is invoked directly (python app.py) or by a WSGI runner.
startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
