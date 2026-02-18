#!/usr/bin/env bash
# ============================================================
#  Pawprint Updater v2.2
#  Applies fixes to an existing Pawprint installation
#  Run from the directory containing this script:
#    sudo bash update.sh              # auto-detect web path from Apache
#    sudo bash update.sh /pawprint    # override web path
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/pawprint"
APACHE_CONF="/etc/apache2/conf-available/pawprint.conf"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run with sudo: sudo bash update.sh [/web-path]"
[[ -d "$INSTALL_DIR" ]] || error "Pawprint not found at $INSTALL_DIR — run install.sh first"

info "Pawprint updater v2.2"

# ── Determine web path ────────────────────────────────────────────────────────
# Priority: 1) command-line argument  2) Apache conf  3) default /pawprint
if [[ -n "${1:-}" ]]; then
    WEB_PATH="${1}"
    info "Web path (from argument): $WEB_PATH"
elif [[ -f "$APACHE_CONF" ]]; then
    WEB_PATH=$(grep 'ProxyPass ' "$APACHE_CONF" | grep -v stream | grep -v PassReverse | head -1 | awk '{print $2}')
    [[ -z "$WEB_PATH" ]] && WEB_PATH="/pawprint"
    info "Web path (from Apache conf): $WEB_PATH"
else
    WEB_PATH="/pawprint"
    info "Web path (default): $WEB_PATH"
fi

# ── Preserve callsign and passcode from the running install ──────────────────
MYCALL=$(grep    '^MYCALL'       "$INSTALL_DIR/app.py" | awk -F'"' '{print $2}')
APRS_PASS=$(grep '^APRS_IS_PASS' "$INSTALL_DIR/app.py" | awk -F'"' '{print $2}')
[[ -n "$MYCALL" ]] && info "Preserving callsign: $MYCALL" || error "Could not read MYCALL from $INSTALL_DIR/app.py"

# ── Back up the old app.py ────────────────────────────────────────────────────
BACKUP="$INSTALL_DIR/app.py.bak.$(date +%Y%m%d%H%M%S)"
cp "$INSTALL_DIR/app.py" "$BACKUP"
info "Backed up old app.py → $BACKUP"

# ── Copy new files ────────────────────────────────────────────────────────────
info "Installing updated files…"
cp app.py                   "$INSTALL_DIR/app.py"
cp templates/index.html     "$INSTALL_DIR/templates/index.html"
[[ -d static ]] && cp -r static/* "$INSTALL_DIR/static/" || true

# ── Re-inject callsign and passcode into app.py ───────────────────────────────
info "Re-applying callsign and passcode…"
sed -i "s|^MYCALL.*=.*|MYCALL          = \"$MYCALL\"|"         "$INSTALL_DIR/app.py"
sed -i "s|^APRS_IS_PASS.*=.*|APRS_IS_PASS    = \"$APRS_PASS\"|" "$INSTALL_DIR/app.py"

# ── Rewrite frontend BASE path and MYCALL ─────────────────────────────────────
python3 - << PYEOF
import re
webpath = "$WEB_PATH"
slug    = webpath.lstrip('/')
mycall  = "$MYCALL"

with open('$INSTALL_DIR/templates/index.html', 'r') as f:
    c = f.read()

# Rewrite the entire const BASE line from scratch — robust against any
# prior corruption left by previous broken install.sh attempts.
new_c, n = re.subn(
    r"const BASE = \(window\.location\.pathname\.match\([^)]+\).*?\)\[1\];[^\n]*",
    r"const BASE = (window.location.pathname.match(/^(.*?\\/" + slug + r")/) || ['',''])[1];  // INSTALL_PATH - updated by installer",
    c
)
if n:
    print(f"  BASE path set to {webpath}")
else:
    print(f"  WARNING: could not find const BASE line in index.html")
    new_c = c

new_c = new_c.replace('const MYCALL = "KI9NG-10";', f'const MYCALL = "{mycall}";')
print(f"  MYCALL set to {mycall}")

with open('$INSTALL_DIR/templates/index.html', 'w') as f:
    f.write(new_c)
PYEOF

# ── Update Apache conf if web path changed ────────────────────────────────────
if [[ -f "$APACHE_CONF" ]]; then
    CURRENT_PATH=$(grep 'ProxyPass ' "$APACHE_CONF" | grep -v stream | grep -v PassReverse | head -1 | awk '{print $2}')
    if [[ "$CURRENT_PATH" != "$WEB_PATH" ]]; then
        info "Updating Apache conf: $CURRENT_PATH → $WEB_PATH"
        ESCAPED_OLD=$(echo "$CURRENT_PATH" | sed 's|/|\\/|g')
        ESCAPED_NEW=$(echo "$WEB_PATH"     | sed 's|/|\\/|g')
        sed -i "s|${ESCAPED_OLD}|${ESCAPED_NEW}|g" "$APACHE_CONF"
        apache2ctl configtest && systemctl reload apache2 && info "Apache reloaded ✓" \
            || warn "Apache config check failed — check manually"
    else
        info "Apache conf already set to $WEB_PATH — no change needed"
    fi
else
    warn "Apache conf not found at $APACHE_CONF — skipping"
fi

# ── Fix ownership ──────────────────────────────────────────────────────────────
if   id asterisk &>/dev/null; then SERVICE_USER="asterisk"
elif id repeater &>/dev/null; then SERVICE_USER="repeater"
elif [[ -n "${SUDO_USER:-}" ]];   then SERVICE_USER="$SUDO_USER"
else warn "Could not detect service user — skipping chown"; SERVICE_USER=""; fi
[[ -n "$SERVICE_USER" ]] && chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── Restart service ───────────────────────────────────────────────────────────
info "Restarting pawprint service…"
systemctl restart pawprint
sleep 3

if systemctl is-active --quiet pawprint; then
    info "pawprint service running ✓"
else
    systemctl status pawprint --no-pager -l
    error "pawprint failed to start — check logs: sudo journalctl -u pawprint -n 50"
fi

PI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║      Pawprint updated to v2.2  📡        ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Callsign  : ${YELLOW}$MYCALL${NC}"
echo -e "  Local URL : ${YELLOW}http://${PI_IP}${WEB_PATH}${NC}"
echo -e "  Public URL: ${YELLOW}https://604011.ki9ng.com${WEB_PATH}${NC}"
echo -e "  Logs      : ${YELLOW}sudo journalctl -u pawprint -f${NC}"
echo ""
echo -e "${GREEN}Changes in v2.2:${NC}"
echo -e "  • Fixed erratic station jumping (position regex bug)"
echo -e "  • Added coordinate sanity checks (rejects out-of-range lat/lon)"
echo -e "  • Removed duplicate track point recording"
echo -e "  • Fixed BASE path installer corruption"
echo ""
