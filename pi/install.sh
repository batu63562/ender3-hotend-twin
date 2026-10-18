#!/usr/bin/env bash
# Installs the hotend twin as a systemd service on the Klipper Raspberry Pi.
# Run on the Pi, from the folder deploy.ps1 copied:   bash pi/install.sh [--samba]
#   --samba  also share the twin's folder read-only on the network, so the Unity viewer on the PC can read the logs.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
PY=python3
WEB_PORT=8090        # 8080 is usually the webcam (crowsnest) on a Klipper Pi

echo "== ender3-twin install: $APP_DIR (user $USER_NAME, $(uname -m), $($PY --version))"

# tomllib (config.toml) needs Python 3.11+
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "!! Python 3.11 or newer is needed. Update the OS image (Debian 12 / Bookworm has 3.11)."
    exit 1
fi

if ! $PY -c 'import venv, ensurepip' 2>/dev/null; then
    sudo apt-get update && sudo apt-get install -y python3-venv
fi
[ -d "$APP_DIR/venv" ] || $PY -m venv "$APP_DIR/venv"
PIP="$APP_DIR/venv/bin/pip"
"$PIP" install --upgrade pip >/dev/null

# The live twin itself only needs websockets. numpy/pandas are for the G-code plan, the fault classifier and the
# session summary; if they cannot be installed the twin still runs and only those side features report an error.
"$PIP" install "websockets>=17.0"
if ! "$PIP" install "numpy>=2.0" "pandas>=3.0"; then
    echo "!! numpy/pandas could not be installed: the twin runs, but without the G-code plan and fault categories."
fi

# Moonraker runs on this Pi: talk to it directly on its own port (no web server in between).
if [ ! -f "$APP_DIR/config.toml" ]; then
    printf 'printer_host = "127.0.0.1:7125"\nweb_port = %s\n' "$WEB_PORT" > "$APP_DIR/config.toml"
    echo "== wrote config.toml (Moonraker at 127.0.0.1:7125, phone page on port $WEB_PORT)"
fi

sed -e "s|@USER@|$USER_NAME|g" -e "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/pi/ender3-twin.service" \
    | sudo tee /etc/systemd/system/ender3-twin.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ender3-twin >/dev/null
sudo systemctl restart ender3-twin
echo "== service started: systemctl status ender3-twin   |   live console: journalctl -u ender3-twin -f"

if [ "${1:-}" = "--samba" ]; then
    command -v smbd >/dev/null || { sudo apt-get update && sudo apt-get install -y samba; }
    if ! grep -q '^\[ender3-twin\]' /etc/samba/smb.conf; then
        printf '\n[ender3-twin]\n   path = %s\n   read only = yes\n   guest ok = yes\n   browseable = yes\n   force user = %s\n' \
            "$APP_DIR" "$USER_NAME" | sudo tee -a /etc/samba/smb.conf >/dev/null
    fi
    sudo systemctl restart smbd
    echo "== read-only share: \\\\$(hostname -I | awk '{print $1}')\\ender3-twin  (Unity StartMenu > Data Folder)"
fi

echo "== phone page: http://$(hostname -I | awk '{print $1}'):$WEB_PORT"
