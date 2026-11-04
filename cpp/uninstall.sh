#!/usr/bin/env bash
# CreeperCrest uninstaller: the counterpart of deploy.sh.
#   sudo bash uninstall.sh              interactive menu
#   sudo bash uninstall.sh --dry-run    show what would happen, change nothing
set -e

DRY=0
for arg in "$@"; do [ "$arg" = "--dry-run" ] && DRY=1; done

SERVICE=/etc/systemd/system/creepercrest.service

# ── Locate the install ─────────────────────────────────────────────────────────
DETECTED=$(sed -n 's/^User=//p' "$SERVICE" 2>/dev/null | head -1)
DETECTED="${DETECTED:-$(basename "$HOME")}"

echo ""
echo "CreeperCrest Uninstall"
echo "──────────────────────"
[ "$DRY" = 1 ] && echo "(dry run: nothing will be changed)"
read -rp "Installed for user [${DETECTED}]: " INPUT
TARGET_USER="${INPUT:-$DETECTED}"
id "$TARGET_USER" &>/dev/null || { echo "Error: user '$TARGET_USER' does not exist on this system."; exit 1; }

TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
TARGET_DIR="$TARGET_HOME/creepercrest"
APP_DIR="$TARGET_DIR/cpp"
BIN="$APP_DIR/creepercrest"

expand() { case "$1" in "~"|"~/"*) echo "$TARGET_HOME${1#\~}" ;; *) echo "$1" ;; esac; }

cc() { [ -x "$BIN" ] && sudo -u "$TARGET_USER" "$BIN" "$@" 2>/dev/null || true; }   # ask the installed binary (config-get ...)

BACKUP_DIR=$(expand "$(cc config-get backup_dir)")
BACKUP_DIR="${BACKUP_DIR:-$TARGET_HOME/mc-backups}"
PANEL_PORT=$(cc config-get port); PANEL_PORT="${PANEL_PORT:-8888}"

# Minecraft server folders recorded in config.json (these hold the worlds)
SERVER_DIRS=()
if [ -f "$APP_DIR/config.json" ] && command -v python3 &>/dev/null; then
    while IFS= read -r d; do [ -n "$d" ] && SERVER_DIRS+=("$(expand "$d")"); done < <(
        python3 -I - "$APP_DIR/config.json" <<'PY'
import json, sys
try:
    for s in (json.load(open(sys.argv[1])).get("servers") or {}).values():
        if isinstance(s, dict) and s.get("directory"): print(s["directory"])
except Exception:
    pass
PY
    )
fi

echo ""
echo "  User     : $TARGET_USER"
echo "  Software : $APP_DIR"
echo "  Service  : $([ -f "$SERVICE" ] && echo "$SERVICE" || echo "not installed")"
echo "  Backups  : $BACKUP_DIR $([ -d "$BACKUP_DIR" ] && echo "($(ls -1 "$BACKUP_DIR" 2>/dev/null | wc -l) files)" || echo "(none)")"
echo "  Servers  : ${#SERVER_DIRS[@]} configured Minecraft server folder(s)"
for d in "${SERVER_DIRS[@]}"; do echo "             $d"; done

# ── Helpers ────────────────────────────────────────────────────────────────────
run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }
yes_no() { local a; read -rp "$1 [y/N]: " a; case "${a:-n}" in [yY]*) return 0 ;; *) return 1 ;; esac; }
type_to_confirm() {   # destructive: the user must type the word
    local a; read -rp "$1 Type '$2' to continue: " a; [ "$a" = "$2" ]
}
safe_rm() {   # refuses obviously dangerous paths
    local p="$1"
    case "$p" in ""|"/"|"$TARGET_HOME"|"$TARGET_HOME/"|"~") echo "  refusing to remove '$p'"; return 1 ;; esac
    [ -e "$p" ] || return 0
    run sudo rm -rf -- "$p"; echo "  removed $p"
}

do_stop_service() {
    if systemctl list-unit-files creepercrest.service &>/dev/null; then
        run sudo systemctl stop creepercrest 2>/dev/null || true
        run sudo systemctl disable creepercrest 2>/dev/null || true
    fi
}
do_remove_service() {
    do_stop_service
    if [ -f "$SERVICE" ]; then run sudo rm -f "$SERVICE"; echo "  removed $SERVICE"; fi
    run sudo systemctl daemon-reload
    run sudo systemctl reset-failed creepercrest 2>/dev/null || true
}
do_remove_software() {   # binary and build output only: config, users, tls, servers, backups stay
    do_stop_service
    for f in "$BIN" "$TARGET_DIR/README.md"; do [ -e "$f" ] && { run sudo rm -f "$f"; echo "  removed $f"; }; done
    for f in build creepercrest; do [ -e "$APP_DIR/$f" ] && safe_rm "$APP_DIR/$f"; done
    [ -d "$TARGET_DIR/.git" ] && echo "  (the git checkout in $TARGET_DIR was left alone)"
    true
}
do_remove_panel_data() {   # settings, logins and certificate
    do_stop_service
    for f in config.json users.json tls; do safe_rm "$APP_DIR/$f"; done
}
do_remove_firewall() {
    local UFW; UFW=$(command -v ufw || echo /usr/sbin/ufw)
    [ -x "$UFW" ] || { echo "  ufw not available, nothing to do."; return 0; }
    local MC GEY lan
    read -rp "  Minecraft port range that was opened [25565-25575]: " MC; MC="${MC:-25565-25575}"
    read -rp "  Geyser UDP port(s), comma separated, or 'none' [28258]: " GEY; GEY="${GEY:-28258}"
    local SSH_PORTS; SSH_PORTS=$(sudo ss -tlnp 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}' | sort -u); SSH_PORTS="${SSH_PORTS:-22}"
    echo "  (SSH stays open on port(s): $(echo $SSH_PORTS); ufw is not disabled.)"
    ufw_del() {   # never deletes a rule for a port sshd listens on
        local spec="$1" port; port="${spec%%/*}"
        for sp in $SSH_PORTS; do [ "$port" = "$sp" ] && { echo "  keeping $spec (ssh port)"; return 0; }; done
        run sudo "$UFW" --force delete allow "$@" >/dev/null 2>&1 || true
    }
    ufw_del "$PANEL_PORT/tcp"
    for lan in $(ip -o -4 route show scope link 2>/dev/null | awk '$1 !~ /^169\.254\./ {print $1}'); do
        ufw_del "$PANEL_PORT/tcp" from "$lan" to any port "$PANEL_PORT" proto tcp
    done
    case " $(seq "${MC%-*}" "${MC#*-}" 2>/dev/null | tr '\n' ' ') " in *" ${SSH_PORTS%% *} "*) echo "  keeping Minecraft range rule (it covers the ssh port)" ;; *) ufw_del "${MC/-/:}/tcp" ;; esac
    if [ "$GEY" != "none" ]; then
        for p in ${GEY//,/ }; do ufw_del "$p/udp"; done
    fi
    if [ -x "$BIN" ]; then
        for p in $(cc server-ports); do ufw_del "$p/tcp"; done
    fi
    for sp in $SSH_PORTS; do run sudo "$UFW" allow "$sp/tcp" >/dev/null; done   # belt and braces: ssh stays allowed
    echo "  firewall rules for panel port $PANEL_PORT, $MC and Geyser removed (where present)"
}
do_remove_servers() {   # Minecraft server folders = worlds, jars, plugins
    [ ${#SERVER_DIRS[@]} -gt 0 ] || { echo "  no server folders recorded."; return 0; }
    for d in "${SERVER_DIRS[@]}"; do safe_rm "$d"; done
}
do_remove_app_dir() {
    [ -d "$APP_DIR" ] && safe_rm "$APP_DIR"
    [ -d "$TARGET_DIR" ] && [ -z "$(ls -A "$TARGET_DIR" 2>/dev/null)" ] && safe_rm "$TARGET_DIR"
    true
}
do_remove_backups() {
    [ -d "$BACKUP_DIR" ] || { echo "  no backup folder at $BACKUP_DIR"; return 0; }
    safe_rm "$BACKUP_DIR"
}

backups_menu() {
    [ -d "$BACKUP_DIR" ] || { echo "  no backup folder at $BACKUP_DIR"; return 0; }
    echo ""
    echo "Backups in $BACKUP_DIR"
    ls -lh "$BACKUP_DIR" 2>/dev/null | sed 's/^/  /'
    echo "    1) Delete ALL backups"
    echo "    2) Delete backups older than N days"
    echo "    3) Delete all backups of one server (by id)"
    echo "    4) Cancel"
    local c n id
    read -rp "Choice [4]: " c
    case "${c:-4}" in
        1) type_to_confirm "This deletes every backup." DELETE && {
               if [ "$DRY" = 1 ]; then echo "  [dry-run] delete $BACKUP_DIR/*.zip"; else sudo find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.zip' -delete; fi
               echo "  all backups deleted"; } ;;
        2) read -rp "Older than how many days: " n
           [[ "$n" =~ ^[0-9]+$ ]] || { echo "  not a number"; return 0; }
           echo "  would delete:"; find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.zip' -mtime "+$n" | sed 's/^/    /'
           yes_no "Delete these?" && { if [ "$DRY" = 1 ]; then echo "  [dry-run] delete"; else sudo find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.zip' -mtime "+$n" -delete; fi; echo "  done"; } ;;
        3) read -rp "Server id: " id
           [[ "$id" =~ ^[a-z0-9-]+$ ]] || { echo "  invalid id"; return 0; }
           echo "  would delete:"; ls -1 "$BACKUP_DIR" | grep -E "^${id}-[0-9]{8}-[0-9]{6}\.zip$" | sed 's/^/    /' || true
           yes_no "Delete these?" && { if [ "$DRY" = 1 ]; then echo "  [dry-run] delete"; else sudo find "$BACKUP_DIR" -maxdepth 1 -type f -regextype posix-extended -regex ".*/${id}-[0-9]{8}-[0-9]{6}\.zip" -delete; fi; echo "  done"; } ;;
        *) echo "  cancelled" ;;
    esac
}

# ── Menu ───────────────────────────────────────────────────────────────────────
echo ""
echo "What do you want to remove?"
echo "  1) Everything, but keep the backups   (service, firewall rules, software, settings, logins, Minecraft server folders)"
echo "  2) Everything, including the backups"
echo "  3) The software only                  (binary and build; keeps settings, logins, servers, backups; the service is stopped)"
echo "  4) The service only                   (systemd unit; keeps everything else including backups)"
echo "  5) Firewall rules only                (ufw rules the installer opened)"
echo "  6) Panel settings and logins only     (config.json, users.json, tls/; keeps servers and backups)"
echo "  7) Minecraft server folders only      (worlds, jars, plugins; keeps the panel and backups)"
echo "  8) Backups only                       (delete all, old, or one server's)"
echo "  9) Quit"
read -rp "Choice [9]: " CHOICE

case "${CHOICE:-9}" in
    1) type_to_confirm "This removes the panel AND your Minecraft server folders (worlds). Backups are kept." REMOVE || { echo "Aborted."; exit 0; }
       do_remove_service; do_remove_firewall; do_remove_servers; do_remove_app_dir ;;
    2) type_to_confirm "This removes the panel, your Minecraft server folders AND every backup." EVERYTHING || { echo "Aborted."; exit 0; }
       do_remove_service; do_remove_firewall; do_remove_servers; do_remove_backups; do_remove_app_dir ;;
    3) yes_no "Remove the CreeperCrest binary and build output?" || { echo "Aborted."; exit 0; }
       do_remove_software ;;
    4) yes_no "Stop and remove the creepercrest systemd service?" || { echo "Aborted."; exit 0; }
       do_remove_service ;;
    5) yes_no "Remove the ufw rules?" || { echo "Aborted."; exit 0; }
       do_remove_firewall ;;
    6) type_to_confirm "This deletes config.json, users.json (all logins and 2FA) and the HTTPS certificate." PANEL || { echo "Aborted."; exit 0; }
       do_remove_panel_data ;;
    7) type_to_confirm "This deletes the Minecraft server folders listed above, including worlds." SERVERS || { echo "Aborted."; exit 0; }
       do_stop_service; do_remove_servers ;;
    8) backups_menu ;;
    *) echo "Nothing changed."; exit 0 ;;
esac

echo ""
echo "Done."
[ "${CHOICE:-9}" = 1 ] || [ "${CHOICE:-9}" = 2 ] && echo "Installed packages (JDK, build tools, ufw, qrencode) were left in place; remove them with your package manager if you no longer need them."
true
