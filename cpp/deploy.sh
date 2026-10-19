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

# ── Dependencies: build tools (only when the binary is not prebuilt), OpenJDK, ufw ────────────────────────────────────────
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
    echo "  Unsupported package manager - make sure g++, make, OpenSSL and libzip (dev packages), OpenJDK and ufw are installed."
else
    if [ ! -x "$SCRIPT_DIR/creepercrest" ]; then
        echo "  Installing build tools (g++, make, OpenSSL and libzip headers)..."
        case "$PKG" in
            apt) pkg_install g++ make libssl-dev libzip-dev zlib1g-dev ;;
            dnf) pkg_install gcc-c++ make openssl-devel libzip-devel zlib-devel ;;
        esac
    fi
    case "$PKG" in   # runtime libraries the binary links against
        apt) pkg_install libssl3 libzip4 2>/dev/null || pkg_install libssl3t64 libzip4t64 2>/dev/null || true ;;
        dnf) pkg_install openssl-libs libzip ;;
    esac

    if command -v java &>/dev/null; then
        echo "  java  → $(java -version 2>&1 | head -1)"
    else
        JDK_PKG=$(pick_jdk)
        echo "  Installing $JDK_PKG..."
        pkg_install "$JDK_PKG"
    fi

    command -v qrencode &>/dev/null || { echo "  Installing qrencode (QR codes for 2FA enrolment, on screen and in the web page)..."; pkg_install qrencode || true; }

    if [ "$SKIP_FW" = 1 ]; then
        echo "  ufw   → skipped (CC_SKIP_FIREWALL)"
    elif command -v ufw &>/dev/null || [ -x /usr/sbin/ufw ]; then
        echo "  ufw   → installed"
    else
        echo "  Installing ufw..."
        pkg_install ufw
    fi
fi

if [ ! -x "$SCRIPT_DIR/creepercrest" ]; then
    echo ""
    echo "Building creepercrest..."
    make -C "$SCRIPT_DIR" -j"$(nproc)"
fi
[ -x "$SCRIPT_DIR/creepercrest" ] || { echo "Error: $SCRIPT_DIR/creepercrest was not built."; exit 1; }

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

sudo systemctl stop creepercrest 2>/dev/null || true   # the running binary cannot be overwritten in place
copy_if_different "$SCRIPT_DIR/creepercrest" "$TARGET_DIR/creepercrest" "creepercrest"
sudo chmod 755 "$TARGET_DIR/creepercrest"
copy_if_different "$SCRIPT_DIR/../README.md" "$TARGET_DIR/README.md" "README.md"

# The web UI and editor are embedded in the binary: nothing else to copy.

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
    [yY]*) sudo -u "$TARGET_USER" "$TARGET_DIR/creepercrest" lan-mode on ;;
    *)     sudo -u "$TARGET_USER" "$TARGET_DIR/creepercrest" lan-mode off >/dev/null ;;
esac

# ── Login users (password + 2FA) ───────────────────────────────────────────────
echo ""
echo "Login users"
echo "  The panel requires a login with an authenticator-app code. Until a user exists it stays locked."
if [ -s "$TARGET_DIR/users.json" ]; then
    echo "  Existing users: $(sudo -u "$TARGET_USER" "$TARGET_DIR/creepercrest" users | tr '\n' ' ')"
fi
read -rp "Usernames to create now (space separated, blank to skip): " NEW_USERS
for u in $NEW_USERS; do
    sudo -u "$TARGET_USER" "$TARGET_DIR/creepercrest" adduser "$u" || true
done

# ── Firewall (ufw) ─────────────────────────────────────────────────────────────
UFW=$(command -v ufw || echo /usr/sbin/ufw)
if [ "$SKIP_FW" = 1 ]; then
    echo ""
    echo "Firewall: skipped (CC_SKIP_FIREWALL) - open the panel port and Minecraft ports yourself."
elif [ -x "$UFW" ]; then
    CONF_PORT=$(sudo -u "$TARGET_USER" "$TARGET_DIR/creepercrest" config-get port 2>/dev/null || echo 8888)
    PANEL_PORT="${CONF_PORT:-8888}"
    # the network of the interface that carries the default route (not a link-local 169.254.x.x route)
    LAN_DEV=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
    LAN_NET=$([ -n "$LAN_DEV" ] && ip -o -4 route show dev "$LAN_DEV" scope link 2>/dev/null | awk '$1 !~ /^169\.254\./ { print $1; exit }')

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
    EXTRA_PORTS=$(sudo -u "$TARGET_USER" "$TARGET_DIR/creepercrest" server-ports 2>/dev/null || true)
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
ExecStart=$TARGET_DIR/creepercrest
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
PORT=$(sudo -u "$TARGET_USER" "$TARGET_DIR/creepercrest" config-get port 2>/dev/null || echo 8888)

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
