#!/usr/bin/env bash
set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# ── Detect user from the home directory this script was run from ───────────────
DETECTED=$(basename "$HOME")

echo ""
echo "CreeperCrest Deployment"
echo "───────────────────────"
read -rp "Deploy as user [${DETECTED}]: " INPUT
TARGET_USER="${INPUT:-$DETECTED}"

# Verify the user exists on this system
if ! id "$TARGET_USER" &>/dev/null; then
    echo "Error: user '$TARGET_USER' does not exist on this system."
    exit 1
fi

TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
TARGET_DIR="$TARGET_HOME/creepercrest"

echo ""
echo "  User    : $TARGET_USER"
echo "  Install : $TARGET_DIR"
echo "  Backups : $TARGET_HOME/mc-backups"
echo ""
read -rp "Continue? [Y/n]: " CONFIRM
case "${CONFIRM:-y}" in
    [yY]*) ;;
    *) echo "Aborted."; exit 0 ;;
esac

# ── Copy files ─────────────────────────────────────────────────────────────────
echo ""
echo "Copying files..."

sudo mkdir -p "$TARGET_DIR"

copy_if_different() {
    local src="$1" dst="$2" label="$3"
    if [ "$(realpath "$src" 2>/dev/null)" = "$(realpath "$dst" 2>/dev/null)" ]; then
        echo "  $label  → same file, skipped"
    else
        sudo cp "$src" "$dst"
        echo "  $label  → copied"
    fi
}

copy_if_different "$SCRIPT_DIR/creepercrest.py" "$TARGET_DIR/creepercrest.py" "creepercrest.py"
copy_if_different "$SCRIPT_DIR/README.md"       "$TARGET_DIR/README.md"       "README.md"

# Only copy config if one doesn't already exist — preserve existing settings
if [ ! -f "$TARGET_DIR/config.json" ]; then
    copy_if_different "$SCRIPT_DIR/config.json" "$TARGET_DIR/config.json" "config.json"
    echo "  config.json  → created"
else
    echo "  config.json  → kept existing (not overwritten)"
fi

sudo chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_DIR"
echo "  Ownership set to $TARGET_USER"

# ── Systemd service ────────────────────────────────────────────────────────────
echo ""
echo "Installing systemd service..."

sudo tee /etc/systemd/system/creepercrest.service > /dev/null <<EOF
[Unit]
Description=CreeperCrest - Minecraft Server Manager
After=network.target

[Service]
User=$TARGET_USER
WorkingDirectory=$TARGET_DIR
ExecStart=python3 $TARGET_DIR/creepercrest.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable creepercrest

# Restart if already running, otherwise start fresh
if sudo systemctl is-active --quiet creepercrest; then
    echo "  Service already running — restarting..."
    sudo systemctl restart creepercrest
else
    sudo systemctl start creepercrest
fi

# ── Done ───────────────────────────────────────────────────────────────────────
PORT=$(sudo -u "$TARGET_USER" python3 -c \
    "import json; c=json.load(open('$TARGET_DIR/config.json')); print(c.get('port',8888))" \
    2>/dev/null || echo 8888)

# Best-effort local IP
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
IP="${IP:-localhost}"

echo ""
echo "────────────────────────────────────"
echo "  CreeperCrest deployed successfully"
echo "  Running as : $TARGET_USER"
echo "  UI         : http://${IP}:${PORT}"
echo "────────────────────────────────────"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status creepercrest"
echo "  sudo systemctl stop   creepercrest"
echo "  sudo journalctl -fu   creepercrest"
echo ""
