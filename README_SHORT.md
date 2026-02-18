# Pawprint APRS Web Interface

A lightweight, mobile-friendly APRS web interface designed to run on a Raspberry Pi alongside [Direwolf](https://github.com/wb2osz/direwolf) and [AllStarLink 3](https://allstarlink.org/). Monitor heard stations in real-time, view them on a map with your live GPS position, send APRS messages, and manage your beacon configuration - all from your phone or browser.

![License](https://img.shields.io/badge/License-MIT-green) ![Version](https://img.shields.io/badge/Version-2.0-blue)

Originally built for **KI9NG-10** (AllStarLink Node 604011), but designed to work with any ham radio APRS setup.

## Features

- 📻 **Live Station List** — Real-time updates via Server-Sent Events
- 🗺️ **Interactive Map** — Your GPS position tracks automatically with "Follow Me" mode
- 💬 **APRS Messaging** — Send messages via Direwolf AGW
- ⚙️ **Configuration** — SmartBeaconing, symbols, IGFILTER - all from the web UI
- 📱 **Mobile-First** — Clean interface optimized for phones

## Prerequisites

### Required Software

1. **Direwolf** (1.6+) - APRS TNC with AGW on port 8080
2. **VoiceAPRS Monitor** - For AGW messaging (included with ASL3)
3. **GPSD** (recommended) - For GPS position tracking
4. **Apache2** (optional) - For reverse proxy
5. **Python 3.7+** - With pip and venv

### Direwolf Configuration

Your `/etc/direwolf.conf` must include:

```ini
MYCALL YOURCALL-SSID
AGWPORT 8080
GPSD
SMARTBEACONING 60 2:00 5 6:00 0:30 30 255
TBEACON sendto=IG delay=1 every=10 symbol="car" comment="Your comment"
IGSERVER noam.aprs2.net
IGLOGIN YOURCALL-SSID PASSCODE
IGFILTER r/LAT/LON/RADIUS
```

## Quick Install

```bash
cd ~
wget https://github.com/yourusername/pawprint/archive/main.tar.gz
tar -xzf main.tar.gz
cd pawprint-main
sudo bash install.sh
```

Access at: `http://<your-pi>/aprs`

See [README.md](README.md) for full documentation.

## License

MIT - See LICENSE file

**73!** 📡
