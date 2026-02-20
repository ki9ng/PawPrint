# Pawprint CLAUDE.md v2.5

## Identity
Op:KI9NG-10 ASL:604011 URL:https://604011.ki9ng.com/pawprint

## Paths
app=/opt/pawprint/app.py tmpl=/opt/pawprint/templates/index.html venv=/opt/pawprint/venv/
data=/var/lib/pawprint/{stations,messages,tracks,pawprint}.json
svc=/etc/systemd/system/pawprint.service apache=/etc/apache2/conf-available/pawprint.conf
direwolf=/etc/direwolf.conf dw-log=/var/log/direwolf/direwolf_console.log(binary+ANSI→use strings)
svcuser=asterisk flask=:5000 apachepath=/pawprint

## Architecture
Browser←SSE/REST→Flask:5000←Apache/pawprint→Internet
Flask←[APRS-IS noam.aprs2.net:14580 filtered-rx][AGW localhost:8080 tx-only][tail dw-log own-pos]

## Deployment workflow (ALWAYS present as tarball)
After making changes, package and present as tarball. User downloads to ~/Downloads/. Give these commands:
```bash
tar -xzf ~/Downloads/pawprint_vX_X.tar.gz -C ~/
cd ~/pawprint
git add -A && git commit -m "vX.X: description"
git push git@github.com:ki9ng/pawprint.git main
sudo bash update.sh
```
SSH already verified for ki9ng. Remote: git@github.com:ki9ng/pawprint.git

## Frontend BASE
```js
const BASE=(window.location.pathname.match(/^(.*?\/pawprint)/)||['',''])[1];
```
Must match Apache proxy path. If path changes: update WEB_PATH(install.sh)+BASE(index.html)+Apache.

## Apache SSE requirement
```apache
ProxyPass /pawprint/api/stream http://127.0.0.1:5000/api/stream flushpackets=on
<Location /pawprint/api/stream>
  SetEnv proxy-nokeepalive 1
</Location>
```

## Own position (CRITICAL)
APRS-IS does NOT echo own packets. Position from tailing dw-log.
direwolf_log_thread() parses: `[\S+] CALL>...[!=@]DDMM.mmN/DDDMM.mmW...`
On match: updates state[own_position], pushes position SSE, add_track_point(MYCALL), maybe_update_filter.
try_seed_from_log() scans last 200 lines on startup.
Log has binary ANSI → always use `strings` to grep.

## Key state keys
stations:{callsign→station_dict} tracks:{callsign→[{lat,lon,ts}]}
messages:[...] own_position:{lat,lon} filter_center:{lat,lon}
filter_radius:km station_max_age_hours:int aprs_is_connected:bool

## Stack
Flask dev server(not gunicorn) aprslib+manual fallback AGW→Direwolf send-only
APRS-IS filtered-rx SSE for push Leaflet+OSM sprites=base64-embedded no CDN

## Update commands
```bash
cd ~/pawprint && sudo bash update.sh
# manual: sudo cp ~/pawprint/app.py /opt/pawprint/ && sudo cp ~/pawprint/templates/index.html /opt/pawprint/templates/ && sudo systemctl restart pawprint
```

## Diagnostics
```bash
sudo systemctl status pawprint direwolf
sudo journalctl -u pawprint -f
curl -s http://localhost:5000/api/status|python3 -m json.tool
sudo strings /var/log/direwolf/direwolf_console.log|grep KI9NG-10|tail -5
nc -zv 127.0.0.1 8080
python3 -c "import json;print(len(json.load(open('/var/lib/pawprint/stations.json'))),'stations')"
```

## Bug history (fixed)

### v2.5 Object packet callsign wrong
Symptom: Gated objects (Winlink RMS etc) show gateway callsign not station (e.g. WINLINK not W9ML-10).
Cause: process_packet used FROM field; object_name in INFO field ignored.
Fix: check parsed.get("object_name"); fallback if INFO[0]==';' take bytes 1-9.
Set from_call=object_name, store gateway=original_from, is_object=True.
Objects skip add_track_point (fixed infra, no tracks needed).

### v2.4
- station_max_age_days→station_max_age_hours(×24 auto-migrate); select dropdown 1h-30d
- tracks same duration as station; cull_stations() deletes tracks; load_tracks() uses hours
- /api/cull_all POST; Cull All button 2-tap arm
- maybe_update_filter() updated state but not socket→filter frozen; fix: call push_filter_now()
- MYCALL track_point SSE never emitted from direwolf_log_thread; fix: check add_track_point return
- refreshTracks() race on concurrent track_point events; fix: 300ms debounce

### v2.3
- track_point SSE not emitted→polylines stale; fix: add_track_point returns T/F emit on True
- duplicate callsignColor/setTrackWindow/refreshTracks defs removed
- cull only on startup→added cull_loop hourly thread + immediate on age change
- filter_radius not persisted→save/load pawprint.json

### v2.2
- fallback parse_aprs_position used '.' not '[\/\\A-Za-z0-9]'→fabricated coords
- coord bounds check added after both parse paths
- add_track_point called twice per packet→removed duplicate

### v2.1
- duplicate let declarations→tabs broken
- own_position not updating→filter frozen
- gpsd.socket conflicts direwolf: sudo systemctl disable gpsd.socket

## beacon_now 400 "No position known yet"
Cause1: tail -n 0 misses pre-startup beacons→try_seed_from_log() scans last 200 lines
Cause2: regex matched only [ig] not [0L]/[0H] RF-only; fix: match [\S+]
Cause3: regex required '!' only; fix: [!=@]
