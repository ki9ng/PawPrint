# Pawprint APRS Web Interface

**v2.5** — A mobile-friendly APRS web interface for Raspberry Pi running alongside Direwolf and AllStarLink 3. Monitor heard stations in real-time, track your GPS position on a map, send APRS messages, and configure beaconing from your browser.

## Features

- **Live Station List** — Real-time via Server-Sent Events, sortable by distance/time/callsign
- **Interactive Map** — Your position updates on every SmartBeacon transmission
- **Follow Me Mode** — Map smoothly tracks your movement
- **APRS Messaging** — Send/receive via Direwolf AGW
- **Web Configuration** — SmartBeaconing, symbol, comment, filter radius, station age
- **Mobile-First Design**

## Prerequisites

### Required

| Software | Purpose | Notes |
|----------|---------|-------|
| **Direwolf 1.6+** | APRS TNC/iGate | Must have AGW on port 8080 |
| **VoiceAPRS Monitor** | APRS message handling | Included with ASL3 |
| **GPSD** | GPS daemon | Provides position to Direwolf |
| **GPS module** | Hardware position source | u-blox, BU-353, etc. |
| **Apache2** | Reverse proxy | Pre-installed on ASL3 |
| **Python 3.7+** | Runtime | Pre-installed on ASL3 |

### Direwolf Configuration

Your `/etc/direwolf.conf` needs these lines:

```ini
MYCALL  YOURCALL-SSID
AGWPORT 8080
GPSD
SMARTBEACONING 60 2:00 5 6:00 0:30 30 255
TBEACON sendto=IG delay=1 every=10 symbol="car" comment="Your comment"
IGSERVER noam.aprs2.net
IGLOGIN YOURCALL-SSID PASSCODE
IGFILTER r/LAT/LON/RADIUS
```

### GPSD Configuration

```bash
# Edit /etc/default/gpsd
DEVICES="/dev/ttyACM0"   # Your GPS device
GPSD_OPTIONS="-n"

# Critical: disable socket activation which conflicts with Direwolf
sudo systemctl disable gpsd.socket
sudo systemctl restart gpsd
sudo systemctl restart direwolf
```

## Installation

```bash
git clone https://github.com/ki9ng/pawprint.git
cd pawprint
sudo bash install.sh
```

The installer:
1. Reads your callsign and APRS passcode from `direwolf.conf` automatically
2. Installs to `/opt/pawprint/` with Python virtualenv
3. Creates `/var/lib/pawprint/` for persistent data
4. Configures Apache reverse proxy
5. Starts the `pawprint` systemd service

**Access at**: `http://<your-pi-ip>/pawprint`

## How Position Tracking Works

Pawprint watches the Direwolf console log for your outgoing beacon lines:

```
[ig] KI9NG-10>APDW16:!4230.21N/08613.02Wf360/000AllStar Node 604011
```

Every time Direwolf transmits your position, Pawprint:
- Updates your map marker
- Adds a track point to your path history
- Updates the APRS-IS filter to your new location
- Pushes the position to all connected browsers via SSE

> **Note**: APRS-IS does not echo your own packets back to you. This is why Pawprint reads from the Direwolf log directly instead of APRS-IS.

## Service Management

```bash
sudo systemctl status pawprint
sudo journalctl -u pawprint -f
sudo systemctl restart pawprint
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tabs don't work | JavaScript error -- check browser console |
| No position marker | Direwolf not beaconing yet -- check `sudo strings /var/log/direwolf/direwolf_console.log \| grep YOURCALL` |
| Wrong stations on map | Filter not updated -- increase radius in Settings, wait for next beacon |
| AGW not connected | Direwolf not running: `sudo systemctl start direwolf` |
| GPS timeouts in Direwolf | Disable socket activation: `sudo systemctl disable gpsd.socket && sudo systemctl restart gpsd` |
| Can't send messages | VoiceAPRS monitor not running: `sudo systemctl status voiceaprs-monitor` |

## Architecture

```
Browser <--SSE/REST--> Flask:5000 <--Apache /pawprint
                           |
             .-------------+-------------.
        APRS-IS          AGW:8080    Direwolf log
     (heard stations)  (messages)  (own position)
     noam.aprs2.net   localhost    /var/log/direwolf/
```

## Credits

- APRS symbols: hessu/aprs-symbols (public domain)
- Map: OpenStreetMap + Leaflet
- APRS parsing: aprslib by Rossen Georgiev
- TNC: Direwolf by WB2OSZ

## Updating an Existing Install

```bash
git pull   # or extract the new tarball
sudo bash update.sh
```

`update.sh` automatically preserves your callsign, APRS passcode, and web path from the running installation, copies the new files, and restarts the service. A timestamped backup of your old `app.py` is saved to `/opt/pawprint/`.

## Changelog

### v2.5
- **Fix: APRS Object packet stations show wrong callsign** -- Gated objects (Winlink RMS gateways, weather stations via hubs, etc.) were displayed with the transmitting gateway callsign (e.g. "WINLINK") instead of the actual object name (e.g. "W9ML-10"). Fixed by reading `object_name` from aprslib parse result, with raw-packet fallback. Gateway callsign stored separately in station record. Objects are correctly excluded from track history (fixed infrastructure doesn't move).

### v2.4
- **Added: Fine-grained station cull window** -- Settings now uses a select with options from 1 hour to 30 days (replaces integer day input). State and config file use `station_max_age_hours` internally.
- **Added: Track length tied to station age** -- Tracks are stored for the same duration as their station. If you set the cull window to 4 hours, tracks are also only kept for 4 hours. The map track-display dropdown is the view window into that stored history.
- **Added: "Tracks:" label on map toolbar** -- The track display window dropdown is now clearly labeled.
- **Added: Cull All button on heard-stations tab** -- Instantly removes all stations and their tracks from the live list and map. Prompts for confirmation.
- **Added: /api/cull_all endpoint** -- POST to wipe all stations and tracks, with `station_remove` SSE events per callsign so all open tabs update immediately.
- **Changed: Orphan tracks cleaned on cull** -- When `cull_stations()` removes a station, its track data is also deleted from state and disk.
- **Migration: station_max_age_days -> station_max_age_hours** -- `pawprint.json` is auto-migrated on first load (old days value * 24).

### v2.3
- **Fix: Track polylines not updating in real time** -- Added `track_point` SSE event; frontend extends polylines in-place via Leaflet `getLatLngs`/`setLatLngs`.
- **Fix: Duplicate callsignColor / setTrackWindow / refreshTracks definitions** -- Removed stale first copy, a latent bug pre-dating v2.2.
- **Added: Configurable station age / heard-list culling** -- Settings card for station retention window.
- **Added: Realtime station cull** -- Age change evicts stale stations immediately via SSE `station_remove` events.
- **Added: Hourly background cull** -- `cull_loop` thread evicts stations that age out during long uptimes.
- **Fix: filter_radius reset to 50 km on restart** -- Both filter radius and station age persisted in `/var/lib/pawprint/pawprint.json`.

### v2.2
- **Fix: Erratic station jumping / wild track lines on map** -- Fallback APRS position parser used unanchored `.` where the symbol-table character belongs, producing fabricated coordinates. Fixed with explicit character class `[\/\\A-Za-z0-9]`.
- **Fix: Coordinate sanity check** -- Added bounds validation after both aprslib parse and fallback parse.
- **Fix: Duplicate track point recording** -- `add_track_point()` was called twice per packet. Removed duplicate block.
- **Added: update.sh** -- One-command updater for existing installs that preserves local configuration.

### v2.1
- Initial public release

## License

MIT -- see LICENSE

---
Built for KI9NG-10, AllStarLink Node 604011. 73!
