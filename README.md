# Pawprint APRS Web Interface

**v2.3** — A mobile-friendly APRS web interface for Raspberry Pi running alongside Direwolf and AllStarLink 3. Monitor heard stations in real-time, track your GPS position on a map, send APRS messages, and configure beaconing from your browser.

## Features

- 📻 **Live Station List** — Real-time via Server-Sent Events, sortable by distance/time/callsign
- 🗺️ **Interactive Map** — Your position updates on every SmartBeacon transmission
- 📍 **Follow Me Mode** — Map smoothly tracks your movement
- 💬 **APRS Messaging** — Send/receive via Direwolf AGW
- ⚙️ **Web Configuration** — SmartBeaconing, symbol, comment, filter radius
- 📱 **Mobile-First Design**

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
git clone https://github.com/yourusername/pawprint.git
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
| Tabs don't work | JavaScript error — check browser console |
| No position marker | Direwolf not beaconing yet — check `sudo strings /var/log/direwolf/direwolf_console.log \| grep YOURCALL` |
| Wrong stations on map | Filter not updated — increase radius in Settings, wait for next beacon |
| AGW not connected | Direwolf not running: `sudo systemctl start direwolf` |
| GPS timeouts in Direwolf | Disable socket activation: `sudo systemctl disable gpsd.socket && sudo systemctl restart gpsd` |
| Can't send messages | VoiceAPRS monitor not running: `sudo systemctl status voiceaprs-monitor` |

## Architecture

```
Browser ←─SSE/REST─→ Flask:5000 ←─Apache /pawprint
                          |
            ┌─────────────┼──────────────┐
       APRS-IS          AGW:8080    Direwolf log
    (heard stations)  (messages)  (own position)
    noam.aprs2.net   localhost    /var/log/direwolf/
```

## Credits

- APRS symbols: [hessu/aprs-symbols](https://github.com/hessu/aprs-symbols) (public domain)  
- Map: [OpenStreetMap](https://openstreetmap.org) + [Leaflet](https://leafletjs.com)  
- APRS parsing: [aprslib](https://github.com/rossengeorgiev/aprs-python)  
- TNC: [Direwolf](https://github.com/wb2osz/direwolf) by WB2OSZ

## Updating an Existing Install

```bash
git pull   # or extract the new tarball
sudo bash update.sh
```

`update.sh` automatically preserves your callsign, APRS passcode, and web path from the running installation, copies the new files, and restarts the service. A timestamped backup of your old `app.py` is saved to `/opt/pawprint/`.

## Changelog

### v2.3
- **Added: Configurable station age / heard-list culling** — New "Heard Stations" card in Settings lets you set how many days a station must be silent before it's removed (1–30 days, default 7).
- **Added: Realtime station cull** — Changing the age limit in Settings evicts stale stations immediately from the live map and list via SSE `station_remove` events. No restart needed.
- **Added: Hourly background cull** — A `cull_loop` thread runs every hour to evict stations that age out during long uptimes, even if the setting never changes.
- **Fix: filter_radius reset to 50 km on restart** — APRS-IS filter radius and station age limit are now persisted in `/var/lib/pawprint/pawprint.json` and restored on startup.

### v2.2
- **Fix: Erratic station jumping / wild green track lines on map** — The fallback APRS position parser used an unanchored `.` regex where the APRS symbol-table character belongs. This caused the regex to latch onto unrelated numeric sequences in packet comments, paths, or timestamps and produce completely fabricated coordinates. Fixed by using an explicit character class `[\/\\A-Za-z0-9]` that matches only valid APRS symbol table identifiers.
- **Fix: Coordinate sanity check** — Added bounds validation (`-90 ≤ lat ≤ 90`, `-180 ≤ lon ≤ 180`) after both the aprslib parse and fallback parse to discard any garbage values that slip through.
- **Fix: Duplicate track point recording** — `add_track_point()` was called twice per position packet due to a copy-paste error, causing double disk writes on every received position. Removed the duplicate block.
- **Added: `update.sh`** — One-command updater for existing installs that preserves all local configuration.

### v2.1
- Initial public release

## License

MIT — see [LICENSE](LICENSE)

---
*Built for KI9NG-10, AllStarLink Node 604011. 73!* 📡
