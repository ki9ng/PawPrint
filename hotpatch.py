#!/usr/bin/env python3
"""
Pawprint hotpatch — apply v2.2 fixes directly to /opt/pawprint/app.py
without needing to transfer the full tarball.

Run on the Pi:
    sudo python3 hotpatch.py
"""
import re, subprocess, sys, shutil, time
from pathlib import Path

APP = Path("/opt/pawprint/app.py")

if not APP.exists():
    print(f"ERROR: {APP} not found — is pawprint installed?")
    sys.exit(1)

# Backup
backup = APP.with_suffix(f".py.bak.hotpatch.{int(time.time())}")
shutil.copy(APP, backup)
print(f"Backed up: {backup}")

c = APP.read_text()
changes = 0

# ── Fix 1: position regex — replace . with explicit symbol-table charset ──────
old1 = r"r'[!=/@](\d{2})(\d{2}\.\d+)([NS]).(\d{3})(\d{2}\.\d+)([EW])'"
new1 = r"r'[!=/@](\d{2})(\d{2}\.\d+)([NS])[\/\\A-Za-z0-9](\d{3})(\d{2}\.\d+)([EW])'"
if old1 in c:
    c = c.replace(old1, new1)
    print("✓ Fix 1: parse_aprs_position regex hardened")
    changes += 1
else:
    print("- Fix 1: already applied or not found")

# ── Fix 2: coordinate sanity check ────────────────────────────────────────────
old2 = "    # Fallback position parse if aprslib missed it\n    if lat is None and \":\" in raw:"
new2 = """    # Fallback position parse if aprslib missed it
    if lat is None and \":\" in raw:"""
# Check if sanity check already present
if "Discarding invalid coordinates" not in c:
    target = "    now_ts  = time.time()"
    insert = """\
    # Sanity-check coordinates from aprslib (compressed packets can occasionally
    # produce out-of-range values on partial parse failures)
    if lat is not None and (lat < -90.0 or lat > 90.0 or lon is None or lon < -180.0 or lon > 180.0):
        log.warning("Discarding invalid coordinates from %s: lat=%s lon=%s", from_call, lat, lon)
        lat, lon = None, None

    """
    if target in c:
        c = c.replace(target, insert + target, 1)
        print("✓ Fix 2: coordinate sanity check added")
        changes += 1
    else:
        print("- Fix 2: insertion point not found, skipping")
else:
    print("- Fix 2: already applied")

# ── Fix 3: remove duplicate add_track_point call ──────────────────────────────
dup = """\
    if lat is not None and lon is not None:
        add_track_point(from_call, lat, lon, now_ts)
        save_tracks()

    if lat is not None and lon is not None:
        add_track_point(from_call, lat, lon, now_ts)
        save_tracks()"""
single = """\
    if lat is not None and lon is not None:
        add_track_point(from_call, lat, lon, now_ts)
        save_tracks()"""
if dup in c:
    c = c.replace(dup, single)
    print("✓ Fix 3: duplicate add_track_point removed")
    changes += 1
else:
    print("- Fix 3: already applied")

# ── Fix 4: direwolf log thread — seed from history + match all channels ────────
if "try_seed_from_log" not in c:
    old_thread = '''    beacon_re = re.compile(
        r"\\[ig\\]\\s+" + re.escape(MYCALL) +
        r">[\\w-]+:!" +
        r"(\\d{2})(\\d{2}\\.\\d+)([NS])" +
        r"[/\\\\]" +
        r"(\\d{3})(\\d{2}\\.\\d+)([EW])"
    )
    def parse_pos(m):
        lat = int(m.group(1)) + float(m.group(2)) / 60
        if m.group(3) == "S": lat = -lat
        lon = int(m.group(4)) + float(m.group(5)) / 60
        if m.group(6) == "W": lon = -lon
        return lat, lon
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
                        add_track_point(MYCALL, lat, lon, time.time())
                        maybe_update_filter(lat, lon)
                except Exception as e:
                    log.warning("Log parse error: %s", e)
        except Exception as e:
            log.warning("Log tail error: %s - retrying in 15s", e)
            time.sleep(15)'''

    new_thread = '''    # Match any Direwolf output channel: [ig] iGated, [0L] RF, [0H] heard, etc.
    # Also match both ! and = position type identifiers used by TBEACON/SMARTBEACONING.
    beacon_re = re.compile(
        r"\\[\\S+\\]\\s+" + re.escape(MYCALL) +
        r">[\\w,-]+:[!=@]" +
        r"(\\d{2})(\\d{2}\\.\\d+)([NS])" +
        r"[\\/\\\\A-Za-z0-9]" +
        r"(\\d{3})(\\d{2}\\.\\d+)([EW])"
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
                        add_track_point(MYCALL, lat, lon, time.time())
                        maybe_update_filter(lat, lon)
                except Exception as e:
                    log.warning("Log parse error: %s", e)
        except Exception as e:
            log.warning("Log tail error: %s - retrying in 15s", e)
            time.sleep(15)'''

    if old_thread in c:
        c = c.replace(old_thread, new_thread)
        print("✓ Fix 4: direwolf log thread — seeds from history, matches all channels")
        changes += 1
    else:
        print("- Fix 4: old pattern not found (may already be patched or formatting differs)")
else:
    print("- Fix 4: already applied")

# ── Write ──────────────────────────────────────────────────────────────────────
APP.write_text(c)
print(f"\n{changes} fix(es) applied to {APP}")

if changes > 0:
    print("Restarting pawprint service…")
    result = subprocess.run(["systemctl", "restart", "pawprint"])
    if result.returncode == 0:
        print("✓ Service restarted")
    else:
        print("✗ Restart failed — check: sudo journalctl -u pawprint -n 20")
else:
    print("No changes needed — already up to date")
