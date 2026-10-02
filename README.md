# CreeperCrest

A small web panel for running Minecraft servers. One Python file, standard library only, works offline.

## Features

- Start, stop and restart servers, with a live console and command box
- Setup wizard: pick Paper, Vanilla, Purpur or Fabric and a version, and the JAR is downloaded for you
- File manager with a built-in code editor (syntax highlighting, find/replace, Ctrl+S)
- Whitelist editor per server, with removal by UUID
- Resource packs: upload or download one and CreeperCrest hosts it
- One-click and scheduled backups, restore, per-server RAM and autostart
- Login with password and 2FA

## Install

```bash
git clone https://github.com/BeanGreen247/creepercrest
cd creepercrest
sudo bash deploy.sh
```

`deploy.sh` installs Python, OpenJDK and `ufw`, opens the firewall ports you choose, creates login users and installs a systemd service. It is safe to rerun: it keeps `config.json`, `users.json` and your servers, but it never deletes old firewall rules, so check `sudo ufw status numbered` afterwards.

Skip the firewall steps with `CC_SKIP_FIREWALL=1 sudo -E bash deploy.sh`. Open `http://<server-ip>:8888` when it finishes.

Run it by hand with `python3 creepercrest.py`. Run it as the user that owns the server files.

## Ports

| Port | Purpose |
|------|---------|
| 8888/tcp | Panel and resource-pack downloads. Keep it on your LAN. |
| 25565/tcp | Java servers (one per server) |
| Geyser UDP (default 28258) | Bedrock players |

Players outside your network need the panel port to download a resource pack, so for them either host the pack elsewhere or reach the panel over a VPN.

## Server protection

Every time a server starts, CreeperCrest enforces:

- `white-list=true`, `enforce-whitelist=true`, `online-mode=true`
- query and RCON off
- a 5 second login throttle per IP in `bukkit.yml` (Paper and Spigot)

Anyone refused by the whitelist is banned by name, and by IP when the address is public. LAN and proxy addresses are never IP-banned. Add players from the **Whitelist** button on the server card. The server must be running to add; removing works any time.

The whitelist matches the account UUID, so a hijacked account still gets in. If a friend's account is compromised, remove their UUID from the whitelist and ban the account until they have recovered it.

## Login and 2FA

The panel is locked until a user exists. Manage users on the host:

```bash
python3 creepercrest.py adduser <name>      # prints a generated password and 2FA key once
python3 creepercrest.py passwd <name>
python3 creepercrest.py reset-2fa <name>
python3 creepercrest.py deluser <name>
python3 creepercrest.py users
```

Passwords are hashed with scrypt, logins are throttled, sessions last 8 h idle, and every state-changing request carries a CSRF token. The only public URL is `/resourcepack/<id>.zip`, because Minecraft clients cannot log in. Install `qrencode` or the Python `qrcode` module to get a QR code for enrolment.

**LAN mode** (`python3 creepercrest.py lan-mode on`) skips the login for private-network clients. Never port-forward the panel while it is on.

**On the internet:** prefer a VPN. Otherwise serve it over HTTPS, either behind a reverse proxy that sets `X-Forwarded-Proto` and `X-Forwarded-For` (then set `trusted_proxies`), or with `tls_cert` / `tls_key` in `config.json`.

## Configuration

`config.json` is created on first run.

| Key | Description |
|-----|-------------|
| `host`, `port` | Listen address and port (default `0.0.0.0:8888`) |
| `backup_dir` | Backup folder (default `~/mc-backups`) |
| `max_backups` | Backups kept per server, `0` = unlimited |
| `lan_mode` | No login for private-network clients |
| `trusted_proxies`, `allowed_hosts` | Reverse-proxy and hostname allow-lists |
| `tls_cert`, `tls_key` | Serve HTTPS directly |
| `refresh_interval` | UI refresh in seconds (default `5`) |
| `auth_disabled` | Turn the login off (not recommended) |

`servers` is managed by the UI.

## Files

```
creepercrest.py        web server, process manager, backups
static/editor.js       bundled CodeMirror editor (see static/LICENSES.md)
deploy.sh              installer
creepercrest.service   systemd unit template
config.json            settings
```

Backups are stored outside the project, in `~/mc-backups`.

## Support

If this project is useful to you, consider supporting its development via PayPal:

[![Donate with PayPal](.github/paypal-qr.png)](https://paypal.me/beangreen2471)

**PayPal:** https://paypal.me/beangreen2471
