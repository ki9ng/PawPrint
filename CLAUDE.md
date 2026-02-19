# CLAUDE.md — Pawprint Project Context

Read this before making any changes. It contains hard-won lessons.

## What This Is

Pawprint is a Flask single-page APRS web app for Raspberry Pi + Direwolf + AllStarLink 3.
It shows heard stations on a map, tracks your own position, and lets you send APRS messages.

**Operator**: KI9NG-10, AllStarLink Node 604011  
**URL**: `https://604011.ki9ng.com/pawprint`  
**Version**: 2.4

## System Layout

| Path | Purpose |
|------|---------|
| `/opt/pawprint/app.py` | Flask backend |
| `/opt/pawprint/templates/index.html` | Frontend (APRS sprites embedded as base64) |
| `/opt/pawprint/venv/` | Python virtualenv |
| `/var/lib/pawprint/stations.json` | Persisted stations (max 500) |
| `/var/lib/pawprint/messages.json` | Persisted messages (max 200) |
| `/var/lib/pawprint/tracks.json` | Track history for polylines |
| `/etc/systemd/system/pawprint.service` | Systemd unit |
| `/etc/apache2/conf-available/pawprint.conf` | Apache proxy config |
| `/etc/direwolf.conf` | Direwolf config (modified by Settings tab) |
| `/var/log/direwolf/direwolf_console.log` | Direwolf log (binary with ANSI codes — use `strings` to grep) |

**Service user**: `asterisk` on ASL3  
**Flask port**: 5000  
**Apache path**: `/pawprint`

## Architecture

```
Browser <─SSE/REST─> Flask:5000 <─Apache /pawprint─> Internet
              |
    ┌─────────┼─────────┐
APRS-IS    AGW:8080   Direwolf log tail
(stations) (messages)  (own position)
```

## How Own Position Works (CRITICAL)

**APRS-IS does NOT echo your own packets back.** So Pawprint cannot learn your position from APRS-IS.

Instead, `direwolf_log_thread()` in app.py tails `/var/log/direwolf/direwolf_console.log` 
and parses lines like:
```
[ig] KI9NG-10>APDW16:!4230.21N/08613.02Wf360/000AllStar Node 604011
```

When a match is found it:
1. Parses DDMM.mm lat/lon to decimal degrees
2. Updates `state["own_position"]`
3. Pushes a `position` SSE event to all browser clients
4. Calls `add_track_point()` to add to track history
5. Calls `maybe_update_filter()` to update APRS-IS filter

The log file contains binary ANSI color codes — always use `strings` when grepping:
```bash
sudo strings /var/log/direwolf/direwolf_console.log | grep KI9NG-10 | tail -5
```

## Frontend BASE Variable

The frontend determines its API base URL from the URL path:
```javascript
const BASE = (window.location.pathname.match(/^(.*?\/pawprint)/) || ['',''])[1];
```

**This must match the Apache proxy path.** The installer updates both.

If you change the web path, update both:
1. `WEB_PATH` in `install.sh`
2. `const BASE` in `templates/index.html`
3. `APACHE_CONF` in Apache

### beacon_now returns 400 "No position known yet" even with GPS lock
**Symptom**: Clicking "Beacon Now" returns 400 even though GPS has a fix and Direwolf has beaconed  
**Cause 1 — tail -n 0**: `direwolf_log_thread()` used `tail -F -n 0` which only watches *new* lines written after pawprint starts. If no beacon fires between restarts, `own_position` stays `None` forever.  
**Cause 2 — [ig] only**: The regex matched only `[ig]` (iGated packets). Direwolf also logs RF transmits as `[0L]`, `[0H]`, etc. If APRS-IS is down or the packet was RF-only, position was never captured.  
**Cause 3 — ! only**: The regex required `!` as the position type identifier. Direwolf TBEACON/SMARTBEACONING also uses `=` and `@`.  
**Fix**: On startup, `try_seed_from_log()` scans the last 200 lines of the existing log (most-recent-first) and seeds `own_position` immediately. The live-tail regex now matches any channel `[\S+]` and all position type identifiers `[!=@]`.

## Known Bugs (Fixed in v2.4)

### Station cull window was days-only with no sub-day resolution
**Symptom**: Minimum cull window was 1 day; no way to expire stations within hours  
**Fix**: Renamed `station_max_age_days` to `station_max_age_hours` everywhere (state, pawprint.json, API, UI). Settings card replaced number input with a select dropdown: 1h, 2h, 4h, 8h, 12h, 1d, 2d, 3d, 7d, 14d, 30d. `load_pawprint_cfg()` auto-migrates old `station_max_age_days` values (multiplies by 24). All internal time math uses `state["station_max_age_hours"] * 3600`.

### Tracks stored longer than their station
**Symptom**: After a station was culled its track data remained in `state["tracks"]` and `tracks.json` indefinitely  
**Fix 1 — cull_stations() cleanup**: Now deletes `state["tracks"][call]` for every evicted station and calls `save_tracks()`.  
**Fix 2 — add_track_point() storage cutoff**: Uses `state["station_max_age_hours"] * 3600` instead of the old hardcoded `TRACK_MAX_AGE` constant (which was 7 days regardless of settings). Tracks now live exactly as long as their station.  
**Fix 3 — load_tracks() cutoff**: Also uses `station_max_age_hours * 3600` so stale track data is dropped on startup.  
**Fix 4 — api/tracks default**: The `/api/tracks` endpoint's default `max_age` parameter now derives from state rather than the removed constant.

### No way to immediately clear the heard-stations list
**Symptom**: No UI to wipe all stations at once without restarting the service or waiting for cull  
**Fix**: Added `POST /api/cull_all` endpoint that clears `state["stations"]` and `state["tracks"]`, pushes `station_remove` SSE for each callsign, saves both files. Added "Cull All" button to the heard-stations toolbar with a confirmation prompt.

## Known Bugs (Fixed in v2.3)

### Track polylines did not update in real time — new points invisible until track window toggled
**Symptom**: After map loads, track polylines for heard stations are drawn correctly from history. But as new packets arrive, the polylines never extend — only old (pre-load) track history is visible. Toggling the track window dropdown forces a refresh and shows the missing points.  
**Cause**: `add_track_point()` wrote new points to `state["tracks"]` and `save_tracks()` persisted them, but no SSE event was emitted. The only way the frontend called `refreshTracks()` was at `initMap()` time and on manual track-window changes. Also: `callsignColor()`, `setTrackWindow()`, and `refreshTracks()` were defined twice in index.html — the duplicate block was dead code but a source of confusion.  
**Fix**: `add_track_point()` now returns `True`/`False`. In `process_packet`, when a point is actually added (`True`), a `track_point` SSE event is pushed with `{callsign, lat, lon, ts}`. The frontend `track_point` handler checks if a polyline already exists for the callsign — if so, it calls `getLatLngs()` and appends the new point with `setLatLngs()` (no round-trip needed). If no polyline exists yet (first/second point for a brand-new track), it triggers a single `refreshTracks()` fetch. The duplicate function definitions were also removed.

### Station cull only fired at startup
**Symptom**: Reducing `station_max_age_days` in Settings had no immediate effect — stale stations stayed on map and list until next restart  
**Cause**: `load_stations()` filtered by age at load time, but there was no mechanism to evict stations from the live in-memory state  
**Fix**: Added `cull_stations()` which evicts stale stations from `state["stations"]`, pushes a `station_remove` SSE event per evicted callsign, and saves to disk. Called immediately when `station_max_age_days` changes via the API, and by a new `cull_loop()` background thread that runs every hour. Frontend handles `station_remove` events by deleting from the `stations{}` dict, removing the Leaflet marker, and refreshing the list.

### filter_radius reset to 50 km on every restart
**Symptom**: After setting a custom APRS-IS filter radius in Settings, the value reverted to 50 km whenever the service restarted  
**Cause**: `filter_radius` was only held in memory — never persisted  
**Fix**: Both `station_max_age_days` and `filter_radius` are now saved to `/var/lib/pawprint/pawprint.json` and loaded by `load_pawprint_cfg()` before `load_stations()` runs in `startup()`.

## Known Bugs (Fixed in v2.2)

### BASE path not set — all API calls 404
**Symptom**: Console errors like `GET https://604011.ki9ng.com/api/stream 404` — note the missing `/pawprint` prefix  
**Cause**: `install.sh` / `update.sh` had three competing broken frontend path-rewriting attempts. The `sed` mangled shell escaping, the first Python block used a literal that never matched, and the second Python block used a regex that didn't match the actual escaped `\\/` in the template. Result: `const BASE` was left unchanged and resolved to `""`.  
**Fix**: Replaced all three blocks with a single Python block using `re.subn()` that correctly matches the literal `\\/` in the JS regex:
```python
re.subn(r"(pathname\.match\(/\^\(\.\*\?\\/)[\w-]+(\)/\))", r"\g<1>" + slug + r"\2", c)
```
**Manual fix on existing broken install**:
```bash
sudo bash update.sh   # re-run the updater — it now patches correctly
```

### Erratic station jumping / wild track lines on map
**Symptom**: Stations appear to teleport across the map; green polylines span hundreds of miles; does not match aprs.fi  
**Cause**: The fallback `parse_aprs_position()` used `.` (any character) in the regex where the APRS symbol-table character belongs:
```python
# BAD — '.' matches digits, causing the regex to latch onto wrong numeric sequences
r'[!=/@](\d{2})(\d{2}\.\d+)([NS]).(\d{3})(\d{2}\.\d+)([EW])'
```
This lets it match inside comment text, timestamps, or other numbers in the packet and produce completely fabricated lat/lon.  
**Fix**: Replaced `.` with `[\/\\A-Za-z0-9]` — the actual set of valid APRS symbol-table characters. Also added a coordinate bounds sanity check (`-90 ≤ lat ≤ 90`, `-180 ≤ lon ≤ 180`) after both the aprslib parse and the fallback parse.

### Duplicate track points / double disk writes
**Symptom**: `tracks.json` grows faster than expected; `save_tracks()` called twice per packet  
**Cause**: `add_track_point()` + `save_tracks()` block was copy-pasted twice in `process_packet()` (lines 347–353)  
**Fix**: Removed the duplicate block. The dedup check inside `add_track_point()` usually prevented double points but the double I/O remained.

## Known Bugs (Fixed in v2.1)

### Duplicate variable declarations
**Symptom**: Tabs don't work, "switchTab is not defined" in console  
**Cause**: `let trackLines`, `let trackWindow`, `let mapLocked` declared twice  
**Fix**: Already removed from source — don't reintroduce them

### APRS-IS filter not moving with you
**Symptom**: Hearing stations from old location after moving  
**Cause**: `own_position` not updating, so `maybe_update_filter()` never fires  
**Fix**: `direwolf_log_thread()` now captures each beacon and updates position

### GPSD socket activation conflict
**Symptom**: Direwolf logs "Timeout waiting for GPS data" repeatedly  
**Cause**: `gpsd.socket` (socket activation) conflicts with Direwolf's direct GPSD connection  
**Fix**:
```bash
sudo systemctl stop gpsd.socket
sudo systemctl disable gpsd.socket
sudo systemctl restart gpsd
sudo systemctl restart direwolf
```

### Map not following position
**Symptom**: "Follow Me" button does nothing  
**Cause**: No `position` SSE events reaching browser  
**Fix**: Backend pushes `position` events from `direwolf_log_thread()`; frontend has `es.addEventListener("position", ...)` handler

## Subpath Routing

Flask has no concept of the subpath — it only sees paths after the prefix.
Apache strips `/pawprint` and forwards to Flask at `/`.

The `BASE` JS variable is set to the subpath prefix so all API calls work:
```javascript
fetch(BASE + "/api/stations")  // → /pawprint/api/stations → Flask /api/stations
```

**SSE requires special Apache config** — `flushpackets=on` and `proxy-nokeepalive`:
```apache
ProxyPass /pawprint/api/stream http://127.0.0.1:5000/api/stream flushpackets=on
<Location /pawprint/api/stream>
    SetEnv proxy-nokeepalive 1
</Location>
```
Without this, SSE events buffer and "Follow Me" won't work in real time.

## Deployment

```bash
# Recommended update (preserves callsign/passcode/path automatically)
cd ~/pawprint && sudo bash update.sh

# Manual quick update
sudo cp ~/pawprint/app.py /opt/pawprint/
sudo cp ~/pawprint/templates/index.html /opt/pawprint/templates/
sudo systemctl restart pawprint

# Full reinstall
cd ~/pawprint && sudo bash install.sh
```

## Diagnostics

```bash
# Service health
sudo systemctl status pawprint direwolf

# Live logs
sudo journalctl -u pawprint -f

# Current position + filter
curl -s http://localhost:5000/api/status | python3 -m json.tool

# Recent own beacons (binary log — use strings)
sudo strings /var/log/direwolf/direwolf_console.log | grep KI9NG-10 | tail -5

# AGW connectivity
nc -zv 127.0.0.1 8080

# Station count
python3 -c "import json; print(len(json.load(open('/var/lib/pawprint/stations.json'))), 'stations')"

# Direwolf GPS status
sudo strings /var/log/direwolf/direwolf_console.log | grep -i "gps\|fix\|timeout" | tail -10
```

## Prerequisites

1. **Direwolf 1.6+** with:
   - `AGWPORT 8080`
   - `GPSD` (reads from GPSD)
   - `TBEACON sendto=IG` (sends position to APRS-IS)
   - `SMARTBEACONING` (optional, controls beacon rate)
   - `IGLOGIN CALL PASSCODE`

2. **VoiceAPRS Monitor** — for AGW message handling

3. **GPSD** — provides GPS to Direwolf
   - GPS device (u-blox, etc.) on `/dev/ttyACM0` or similar
   - Disable socket activation: `sudo systemctl disable gpsd.socket`

4. **Apache2** — reverse proxy

5. **Python 3.7+** with flask, aprslib

## Stack Notes

- Flask dev server (not gunicorn) — fine for single-user local use
- aprslib for packet parsing + manual fallback `extract_aprs_symbol()` regex
- AGW to Direwolf on `localhost:8080` — send only (TX messages)
- APRS-IS on `noam.aprs2.net:14580` — filtered receive
- SSE for live push to browser (requires flushpackets in Apache)
- Sprites embedded as base64 in index.html — no CDN needed

## License

MIT

---
*Last updated: 2026-02-19 (v2.4 — fine-grained cull window, track storage tied to station age, Cull All button, /api/cull_all, README emoji cleanup)*
