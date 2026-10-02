#!/usr/bin/env bash
set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# Skip all firewall changes (ufw install + rules) on hosts whose firewall is managed elsewhere, e.g. Ansible:
#   CC_SKIP_FIREWALL=1 sudo -E bash deploy.sh      or      sudo bash deploy.sh --skip-firewall
SKIP_FW="${CC_SKIP_FIREWALL:-0}"
for arg in "$@"; do
    [ "$arg" = "--skip-firewall" ] && SKIP_FW=1
done

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

# ── Dependencies: python3, OpenJDK, ufw ────────────────────────────────────────
echo ""
echo "Checking dependencies..."

if command -v apt-get &>/dev/null; then PKG=apt
elif command -v dnf &>/dev/null;     then PKG=dnf
else PKG=""
fi

APT_UPDATED=0
pkg_install() {
    case "$PKG" in
        apt)
            if [ "$APT_UPDATED" = 0 ]; then sudo apt-get update -qq; APT_UPDATED=1; fi
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf) sudo dnf install -y -q "$@" ;;
    esac
}

# Newest OpenJDK the distro offers (recent Minecraft versions need Java 21+, newest need 25)
pick_jdk() {
    local v
    for v in 25 21 17; do
        case "$PKG" in
            apt) apt-cache show "openjdk-$v-jdk-headless" &>/dev/null && { echo "openjdk-$v-jdk-headless"; return; } ;;
            dnf) dnf -q list --available "java-$v-openjdk-devel" &>/dev/null && { echo "java-$v-openjdk-devel"; return; } ;;
        esac
    done
    case "$PKG" in
        apt) echo "default-jdk-headless" ;;
        dnf) echo "java-latest-openjdk-devel" ;;
    esac
}

if [ -z "$PKG" ]; then
    echo "  Unsupported package manager - make sure python3, python3-qrcode, python3-qrcodegen, OpenJDK and ufw are installed."
else
    command -v python3 &>/dev/null || { echo "  Installing python3..."; pkg_install python3; }

    if command -v java &>/dev/null; then
        echo "  java  → $(java -version 2>&1 | head -1)"
    else
        JDK_PKG=$(pick_jdk)
        echo "  Installing $JDK_PKG..."
        pkg_install "$JDK_PKG"
    fi

    command -v qrencode &>/dev/null || { echo "  Installing qrencode (QR codes for 2FA enrolment)..."; pkg_install qrencode || true; }
    command -v python3-qrcode &>/dev/null || { echo "  Installing python3-qrcode (QR code for 2FA python module)..."; pkg_install python3-qrcode || true; }
    command -v python3-qrcodegen &>/dev/null || { echo "  Installing python3-qrcodegen (QR code generator for 2FA python module)..."; pkg_install python3-qrcodegen || true; }

    if [ "$SKIP_FW" = 1 ]; then
        echo "  ufw   → skipped (CC_SKIP_FIREWALL)"
    elif command -v ufw &>/dev/null || [ -x /usr/sbin/ufw ]; then
        echo "  ufw   → installed"
    else
        echo "  Installing ufw..."
        pkg_install ufw
    fi
fi

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

# Bundled editor (CodeMirror) - served locally so the panel works offline
sudo mkdir -p "$TARGET_DIR/static"
copy_if_different "$SCRIPT_DIR/static/editor.js" "$TARGET_DIR/static/editor.js" "static/editor.js"

# Only copy config if one doesn't already exist - preserve existing settings
if [ ! -f "$TARGET_DIR/config.json" ]; then
    copy_if_different "$SCRIPT_DIR/config.json" "$TARGET_DIR/config.json" "config.json"
    echo "  config.json  → created"
else
    echo "  config.json  → kept existing (not overwritten)"
fi

sudo chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_DIR"
echo "  Ownership set to $TARGET_USER"

# ── LAN mode (no login on a private network) ───────────────────────────────────
echo ""
echo "LAN mode"
echo "  On a private network that is NOT reachable from the internet you can skip the login:"
echo "  clients on 192.168.x / 10.x / 172.16-31.x / localhost get in directly, every other address must still sign in."
echo "  Never port-forward the panel to the internet while this is on."
read -rp "Enable LAN mode (no login on the local network)? [y/N]: " LAN_CHOICE
case "${LAN_CHOICE:-n}" in
    [yY]*) sudo -u "$TARGET_USER" python3 "$TARGET_DIR/creepercrest.py" lan-mode on ;;
    *)     sudo -u "$TARGET_USER" python3 "$TARGET_DIR/creepercrest.py" lan-mode off >/dev/null ;;
esac

# ── Login users (password + 2FA) ───────────────────────────────────────────────
echo ""
echo "Login users"
echo "  The panel requires a login with an authenticator-app code. Until a user exists it stays locked."
if [ -s "$TARGET_DIR/users.json" ]; then
    echo "  Existing users: $(sudo -u "$TARGET_USER" python3 "$TARGET_DIR/creepercrest.py" users | tr '\n' ' ')"
fi
read -rp "Usernames to create now (space separated, blank to skip): " NEW_USERS
for u in $NEW_USERS; do
    sudo -u "$TARGET_USER" python3 "$TARGET_DIR/creepercrest.py" adduser "$u" || true
done

# ── Firewall (ufw) ─────────────────────────────────────────────────────────────
UFW=$(command -v ufw || echo /usr/sbin/ufw)
if [ "$SKIP_FW" = 1 ]; then
    echo ""
    echo "Firewall: skipped (CC_SKIP_FIREWALL) - open the panel port and Minecraft ports yourself."
elif [ -x "$UFW" ]; then
    CONF_PORT=$(python3 -c "import json; print(json.load(open('$TARGET_DIR/config.json')).get('port', 8888))" 2>/dev/null || echo 8888)
    PANEL_PORT="${CONF_PORT:-8888}"
    LAN=$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4; exit}')
    LAN_NET=$(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$LAN" 2>/dev/null || echo "")

    echo ""
    echo "Firewall"
    echo "  The panel port ($PANEL_PORT) serves the web UI AND the resource packs players download."
    echo "  The UI has no login, so restricting it to your local network is safest."
    echo "    1) Local network only${LAN_NET:+ ($LAN_NET)}  [default]"
    echo "    2) Anywhere (needed for players outside your network to get resource packs)"
    read -rp "Allow panel port from [1/2]: " FW_CHOICE
    read -rp "Minecraft port range to open [25565-25575]: " MC_RANGE
    MC_RANGE="${MC_RANGE:-25565-25575}"
    read -rp "Geyser (Bedrock) UDP port(s), comma separated, or 'none' [28258]: " GEYSER_PORTS
    GEYSER_PORTS="${GEYSER_PORTS:-28258}"

    # SSH first, so enabling the firewall can never lock this machine out
    SSH_PORTS=$(sudo ss -tlnp 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}' | sort -u)
    for p in ${SSH_PORTS:-22}; do sudo "$UFW" allow "$p/tcp" >/dev/null; echo "  allow $p/tcp (ssh)"; done

    if [ "${FW_CHOICE:-1}" = "2" ] || [ -z "$LAN_NET" ]; then
        sudo "$UFW" allow "$PANEL_PORT/tcp" >/dev/null
        echo "  allow $PANEL_PORT/tcp (panel + resource packs, anywhere)"
    else
        sudo "$UFW" allow from "$LAN_NET" to any port "$PANEL_PORT" proto tcp >/dev/null
        echo "  allow $PANEL_PORT/tcp from $LAN_NET (panel + resource packs)"
    fi

    sudo "$UFW" allow "${MC_RANGE/-/:}/tcp" >/dev/null
    echo "  allow ${MC_RANGE/-/:}/tcp (Minecraft)"

    if [ "$GEYSER_PORTS" != "none" ]; then
        for p in ${GEYSER_PORTS//,/ }; do
            sudo "$UFW" allow "$p/udp" >/dev/null && echo "  allow $p/udp (Geyser / Bedrock)"
        done
    fi

    # Also open any custom server-port already used by configured servers
    EXTRA_PORTS=$(python3 - "$TARGET_DIR/config.json" <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    for sc in json.load(open(sys.argv[1])).get("servers", {}).values():
        pf = os.path.join(os.path.expanduser(sc.get("directory", "")), "server.properties")
        for line in open(pf):
            if line.startswith("server-port="):
                print(line.split("=", 1)[1].strip())
except Exception:
    pass
PYEOF
)
    for p in $EXTRA_PORTS; do
        sudo "$UFW" allow "$p/tcp" >/dev/null && echo "  allow $p/tcp (existing server)"
    done

    sudo "$UFW" --force enable >/dev/null
    echo "  ufw enabled"
else
    echo ""
    echo "Warning: ufw not available - open the panel and Minecraft ports in your firewall manually."
fi

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
NoNewPrivileges=true
PrivateTmp=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable creepercrest

# Restart if already running, otherwise start fresh
if sudo systemctl is-active --quiet creepercrest; then
    echo "  Service already running - restarting..."
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
