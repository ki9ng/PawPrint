#!/usr/bin/env bash
# ============================================================
#  Pawprint Installer v2.2
#  APRS web interface for Direwolf + AllStarLink 3
#  https://github.com/yourusername/pawprint
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/pawprint"
DATA_DIR="/var/lib/pawprint"
DIREWOLF_CONF="/etc/direwolf.conf"
SERVICE_FILE="/etc/systemd/system/pawprint.service"
APACHE_CONF="/etc/apache2/conf-available/pawprint.conf"
WEB_PATH="/pawprint"   # URL path — change to /aprs or anything you like

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run with sudo: sudo bash install.sh"

info "Pawprint installer v2.2"

# ── Detect callsign from direwolf.conf ────────────────────────────────────────
MYCALL=""
APRS_PASS=""
if [[ -f "$DIREWOLF_CONF" ]]; then
    MYCALL=$(grep -i "^MYCALL" "$DIREWOLF_CONF" | awk '{print $2}' | head -1)
    APRS_PASS=$(grep -i "^IGLOGIN" "$DIREWOLF_CONF" | awk '{print $3}' | head -1)
    [[ -n "$MYCALL" ]] && info "Detected callsign: $MYCALL" || warn "MYCALL not found in direwolf.conf"
    [[ -n "$APRS_PASS" ]] && info "Detected APRS-IS passcode" || warn "IGLOGIN not found in direwolf.conf"
fi
if [[ -z "$MYCALL" ]]; then
    read -p "Enter your callsign with SSID (e.g., W1ABC-10): " MYCALL
    [[ -z "$MYCALL" ]] && error "Callsign required"
fi
if [[ -z "$APRS_PASS" ]]; then
    read -p "Enter your APRS-IS passcode: " APRS_PASS
    [[ -z "$APRS_PASS" ]] && warn "No passcode entered — APRS-IS receive-only mode"
fi

# ── Detect service user ───────────────────────────────────────────────────────
if   id asterisk &>/dev/null; then SERVICE_USER="asterisk"
elif id repeater &>/dev/null; then SERVICE_USER="repeater"
elif [[ -n "${SUDO_USER:-}" ]];   then SERVICE_USER="$SUDO_USER"
else error "Could not detect service user"; fi
info "Service user: $SERVICE_USER"

# ── System packages ───────────────────────────────────────────────────────────
info "Installing system packages…"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv apache2 || true

# ── Install application files ─────────────────────────────────────────────────
info "Installing to $INSTALL_DIR…"
mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"
cp app.py                       "$INSTALL_DIR/app.py"
cp templates/index.html         "$INSTALL_DIR/templates/index.html"
[[ -d static ]] && cp -r static/* "$INSTALL_DIR/static/" || true

# ── Configure callsign and passcode in app.py ─────────────────────────────────
info "Configuring callsign: $MYCALL"
sed -i "s|^MYCALL.*=.*|MYCALL          = \"$MYCALL\"|" "$INSTALL_DIR/app.py"
sed -i "s|^APRS_IS_PASS.*=.*|APRS_IS_PASS    = \"$APRS_PASS\"|" "$INSTALL_DIR/app.py"

# ── Configure URL path and callsign in frontend ──────────────────────────────
info "Configuring frontend: path=$WEB_PATH callsign=$MYCALL"
python3 - << PYEOF
import re
webpath = "$WEB_PATH"          # e.g. /aprs
slug    = webpath.lstrip('/')  # e.g. aprs
mycall  = "$MYCALL"

with open('$INSTALL_DIR/templates/index.html', 'r') as f:
    c = f.read()

# Rewrite the entire const BASE line from scratch — more robust than
# trying to surgically replace just the slug, which fails if a previous
# install left the line corrupted.
new_c, n = re.subn(
    r"const BASE = \(window\.location\.pathname\.match\([^)]+\).*?\)\[1\];[^\n]*",
    r"const BASE = (window.location.pathname.match(/^(.*?\\/" + slug + r")/) || ['',''])[1];  // INSTALL_PATH - updated by installer",
    c
)
if n:
    print(f"  BASE path set to {webpath}")
else:
    print(f"  WARNING: const BASE line not found in index.html")
    new_c = c

# Fix MYCALL constant
new_c = new_c.replace('const MYCALL = "KI9NG-10";', f'const MYCALL = "{mycall}";')
print(f"  MYCALL set to {mycall}")

with open('$INSTALL_DIR/templates/index.html', 'w') as f:
    f.write(new_c)
PYEOF

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── Data directory ────────────────────────────────────────────────────────────
info "Creating $DATA_DIR…"
mkdir -p "$DATA_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 755 "$DATA_DIR"

# ── Python virtualenv ─────────────────────────────────────────────────────────
info "Setting up Python venv…"
rm -rf "$INSTALL_DIR/venv"
if sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv" 2>/dev/null; then
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q flask aprslib
    PYTHON_BIN="$INSTALL_DIR/venv/bin/python"
    info "Venv ready ✓"
else
    warn "venv failed, using system python"
    PYTHON_BIN="$(which python3)"
fi

# ── direwolf.conf permissions ─────────────────────────────────────────────────
if [[ -f "$DIREWOLF_CONF" ]]; then
    info "Setting direwolf.conf permissions…"
    getent group aprsconf &>/dev/null || groupadd aprsconf
    usermod -aG aprsconf "$SERVICE_USER"
    chown root:aprsconf "$DIREWOLF_CONF"
    chmod 664 "$DIREWOLF_CONF"
    info "direwolf.conf writable by aprsconf group ✓"
    # Add IGFILTER if missing
    grep -qi "^IGFILTER" "$DIREWOLF_CONF" || echo "IGFILTER r/0/0/50" >> "$DIREWOLF_CONF"
else
    warn "direwolf.conf not found at $DIREWOLF_CONF — skipping"
fi

# ── sudoers ───────────────────────────────────────────────────────────────────
info "Writing sudoers entry…"
cat > /etc/sudoers.d/pawprint << EOF
$SERVICE_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart direwolf
$SERVICE_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart voiceaprs-monitor
EOF
chmod 440 /etc/sudoers.d/pawprint
visudo -c -f /etc/sudoers.d/pawprint || { rm /etc/sudoers.d/pawprint; error "sudoers syntax error"; }
info "sudoers ✓"

# ── Systemd service ───────────────────────────────────────────────────────────
info "Installing systemd service…"
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Pawprint APRS Web Interface for $MYCALL
After=network.target direwolf.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON_BIN $INSTALL_DIR/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pawprint
systemctl restart pawprint
sleep 3

if systemctl is-active --quiet pawprint; then
    info "pawprint service running ✓"
else
    systemctl status pawprint --no-pager -l
    error "pawprint failed to start"
fi

# ── Apache reverse proxy ──────────────────────────────────────────────────────
if [[ -d /etc/apache2 ]]; then
    info "Configuring Apache at $WEB_PATH…"
    a2enmod proxy proxy_http headers 2>/dev/null || true

    cat > "$APACHE_CONF" << EOF
# Pawprint APRS — reverse proxy at $WEB_PATH
<IfModule mod_proxy.c>
    ProxyPreserveHost On

    # SSE stream MUST come before wildcard — flushpackets for real-time push
    ProxyPass        $WEB_PATH/api/stream  http://127.0.0.1:5000/api/stream  flushpackets=on
    ProxyPassReverse $WEB_PATH/api/stream  http://127.0.0.1:5000/api/stream

    ProxyPass        $WEB_PATH  http://127.0.0.1:5000
    ProxyPassReverse $WEB_PATH  http://127.0.0.1:5000

    <Location $WEB_PATH/api/stream>
        SetEnv proxy-nokeepalive 1
        SetEnv proxy-initial-not-pooled 1
        RequestHeader set X-Forwarded-Prefix "$WEB_PATH"
    </Location>
</IfModule>
EOF

    a2enconf pawprint 2>/dev/null || true
    apache2ctl configtest && systemctl reload apache2 && info "Apache configured ✓" || warn "Apache config issue — check manually"
    APACHE_OK=true
else
    warn "Apache not found — skipping proxy setup"
    APACHE_OK=false
fi

# ── Restart Direwolf ──────────────────────────────────────────────────────────
if systemctl is-active --quiet direwolf 2>/dev/null; then
    info "Restarting Direwolf…"
    systemctl restart direwolf
    sleep 2
    systemctl is-active --quiet voiceaprs-monitor 2>/dev/null && systemctl restart voiceaprs-monitor || true
fi

# ── Done ──────────────────────────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║    Pawprint installed successfully! 📡   ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Callsign : ${YELLOW}$MYCALL${NC}"
if [[ "${APACHE_OK:-false}" == "true" ]]; then
    echo -e "  URL      : ${YELLOW}http://${PI_IP}${WEB_PATH}${NC}"
else
    echo -e "  URL      : ${YELLOW}http://${PI_IP}:5000${NC}"
fi
echo ""
echo -e "  Logs     : ${YELLOW}sudo journalctl -u pawprint -f${NC}"
echo -e "  Restart  : ${YELLOW}sudo systemctl restart pawprint${NC}"
echo ""
echo -e "${GREEN}Next steps:${NC}"
echo -e "  1. Verify Direwolf is beaconing your position"
echo -e "     ${YELLOW}sudo strings /var/log/direwolf/direwolf_console.log | grep $MYCALL | tail -3${NC}"
echo -e "  2. Open the web interface and click 📍 Follow Me on the map"
echo -e "  3. Your position marker will appear after the first beacon"
echo ""
