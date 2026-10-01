# CreeperCrest

Lightweight Minecraft server management panel. No external dependencies - pure Python stdlib only.

## Features

- Start, stop, and restart servers
- Live console output per server with command input
- Memory (RAM) allocation per server
- One-click backups - zipped and saved to `~/mc-backups`
- Direct backup download from the browser
- Add and remove servers via the web UI
- Auto-refreshes every 5 seconds

## Requirements

- Python 3.7+
- Java (JRE/JDK) installed on the host
- Run as a user that has read/write access to the server directories

## Installation

```bash
git clone https://github.com/BeanGreen247/creepercrest
cd creepercrest
sudo bash deploy.sh
```

The deploy script will ask which user to run as, then:

- installs Python 3, `qrencode` (QR codes for 2FA enrolment), OpenJDK (newest of 25/21/17 the distro offers, skipped if `java` already exists) and `ufw` on apt or dnf systems
- configures `ufw`: allows SSH first (so you can't be locked out), the panel port (local network only by default, or anywhere), a Minecraft port range (default `25565-25575`), the Geyser/Bedrock UDP port (default `28258`, or `none`), and any port already used by your servers, then enables it
- copies the files, installs and starts the systemd service

If the firewall is managed elsewhere (for example by Ansible), skip every firewall step with `CC_SKIP_FIREWALL=1 sudo -E bash deploy.sh` or `sudo bash deploy.sh --skip-firewall`.

The panel port also serves resource packs to players, so it must be reachable from the machines that join your servers. The web UI has no login, so keep it on your local network unless you need outside players to receive resource packs.

**Manual file placement (optional):**
```bash
sudo cp -r creepercrest /home/crafty/creepercrest
sudo chown -R crafty:crafty /home/crafty/creepercrest
```

## Running

**Manually:**
```bash
sudo -u crafty python3 /home/crafty/creepercrest/creepercrest.py
```

**As a systemd service (runs on boot):**

```bash
sudo cp creepercrest.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now creepercrest
sudo systemctl status creepercrest
```

Then open `http://<your-server-ip>:8888` in a browser.

## Configuration

`config.json` is created automatically on first run. Edit it to change the port, host, or backup directory.

```json
{
  "host": "0.0.0.0",
  "port": 8888,
  "backup_dir": "~/mc-backups",
  "refresh_interval": 5,
  "servers": {}
}
```

| Key | Description |
|-----|-------------|
| `host` | Interface to listen on. `0.0.0.0` = all interfaces |
| `port` | Web UI port |
| `backup_dir` | Where zip backups are saved. `~` resolves to the running user's home |
| `max_backups` | Backups kept per server; oldest are deleted after each backup. `0` = unlimited (default), otherwise 3-28 (editable under Backups > Edit Schedule) |
| `lan_mode` | `true` = no login for private-network clients (see Security) |
| `trusted_proxies` | Reverse-proxy addresses whose `X-Forwarded-For` is trusted |
| `tls_cert` / `tls_key` | Serve HTTPS directly from these files |
| `allowed_hosts` | Extra hostnames accepted in LAN mode |
| `auth_disabled` | Set `true` to turn the login off (not recommended) |
| `refresh_interval` | How often the UI polls for status updates, in seconds (default: `5`) |
| `servers` | Managed automatically by the UI - do not edit by hand |

## Security

CreeperCrest is a remote-control panel: anyone who can sign in can run commands on the host through the Minecraft console, upload JARs and set Java arguments. Treat a login as full access to the machine's service user.

**Built in**
- Password + TOTP 2FA, scrypt hashing, login throttling, replay-proof codes, 8 h idle / 24 h sessions, `HttpOnly` + `SameSite=Strict` cookies (`Secure` over HTTPS).
- CSRF token on every state-changing request, plus an `Origin` check; security headers (`CSP`, `X-Frame-Options`, `nosniff`, `no-referrer`, HSTS over HTTPS).
- File access is confined to each server's directory (symlink-safe); backup names are validated; server IDs are restricted; file names are never placed in inline JavaScript.
- Resource-pack downloads from a URL refuse private/internal addresses (SSRF guard) unless LAN mode or `allow_private_fetch` is on.
- Idle connections are dropped after 60 s; request bodies are capped (2 GB uploads, 1 MB JSON). The service unit sets `NoNewPrivileges` and friends.

**Putting it on the internet**
1. Prefer a VPN (Tailscale/WireGuard) over opening the port at all.
2. Otherwise serve it over HTTPS: put it behind a reverse proxy (Caddy/nginx) that sets `X-Forwarded-Proto: https` and `X-Forwarded-For`, and set `"trusted_proxies": ["127.0.0.1"]` so login throttling sees real client addresses. Or terminate TLS in the app with `"tls_cert"` / `"tls_key"` in `config.json`.
3. Open only that HTTPS port (`CC_SKIP_FIREWALL=1` if you manage the firewall yourself) and keep the panel port itself LAN-only.
4. Use strong, unique passwords. Remove users you no longer need (`--remove-user`).

**LAN mode (no login on a private network)**

For an isolated home/LAN setup you can skip accounts entirely:

```bash
python3 creepercrest.py lan-mode on     # then restart; "off" to disable
```

Requests from private or loopback addresses (192.168.x, 10.x, 172.16-31.x, localhost) are let in without a login. Requests from any other address, or that arrive through a proxy (`X-Forwarded-For` etc.), or with a hostname that is not an IP / `localhost` / the machine name / `*.local` / in `"allowed_hosts"`, still need a login. The CSRF and cross-origin protections stay on. Do not port-forward the panel while LAN mode is on: a request forwarded straight through your router still arrives from a public address and would be asked to sign in, but a proxy on your LAN would not be recognised as outside unless it sends the usual forwarding headers.

## Login and 2FA

The panel is locked behind a username, password and a 6-digit authenticator code (Google Authenticator, Authy, any TOTP app). Until at least one user exists it shows a "setup required" page.

Manage users on the server (stdlib only, nothing to install):

```bash
python3 creepercrest.py adduser <name>     # generates a password + 2FA key, shown once
python3 creepercrest.py adduser <name> --prompt   # choose your own password instead
python3 creepercrest.py passwd <name>      # new generated password
python3 creepercrest.py reset-2fa <name>   # new 2FA key (re-enrol the app)
python3 creepercrest.py deluser <name>      # also: --remove-user <name>; add --yes to skip the prompt
python3 creepercrest.py users
```

The 2FA key is printed with an `otpauth://` URI. Install `qrencode` to also get a QR code in the terminal; add the optional `qrcode` Python module to show one on the panel's **2FA** page. Otherwise enter the key manually in your app.

- Users live in `users.json` next to `config.json` (mode 600, passwords hashed with scrypt, ignored by git).
- Sessions are in memory, last 8 h idle / 24 h total, and are cleared on restart.
- 5 failed logins from one address, or 10 for one username, lock sign-in for 15 minutes. A used authenticator code cannot be replayed.
- State-changing requests need a CSRF token; logins and changes are logged to the console / journal (`[auth]`, `[audit]`).
- The only public URL is `/resourcepack/<id>.zip`, because Minecraft clients cannot log in.
- Serving over plain HTTP sends the session cookie unencrypted - put the panel behind HTTPS (a reverse proxy that sets `X-Forwarded-Proto: https` makes the cookie `Secure`) or keep it on a trusted network.
- To run without a login (not recommended) set `"auth_disabled": true` in `config.json`.

## Adding a Server

1. Open the web UI
2. Click **+ Add Server**
3. Fill in the fields:

| Field | Description |
|-------|-------------|
| ID | Short identifier, e.g. `survival` |
| Display Name | Name shown in the UI |
| Server Directory | Full path to the folder containing the JAR |
| JAR filename | Usually `server.jar` or `paper.jar` |
| Min RAM (MB) | Minimum RAM allocated with `-Xms` |
| Max RAM (MB) | Maximum RAM allocated with `-Xmx` |
| Extra JVM args | G1GC flags etc. - safe to leave as default |
| Download server JAR | Optional; pick Paper, Vanilla, Purpur or Fabric plus a Minecraft version and CreeperCrest downloads the JAR into the server directory |
| World seed | Optional; written to `level-seed` in `server.properties` (new worlds only) |
| Resource pack URL / SHA-1 | Optional; written to `resource-pack` / `resource-pack-sha1` in `server.properties`, also editable per server |

## Backups

Clicking **Backup** on a server card:

- Zips the entire server directory (skips `logs/` and `crash-reports/` to save space)
- Saves the zip to `~/mc-backups/<server-id>-YYYYMMDD-HHMMSS.zip`
- The zip appears in the **Backups** section with a **Download** link
- The server does not need to be stopped to take a backup

## File Layout

```
creepercrest/
├── creepercrest.py      # everything - web server, process manager, backup logic
├── creepercrest.service # systemd service template (User=crafty)
├── deploy.sh            # interactive install script
├── config.json          # auto-managed, edit only host/port/backup_dir
└── README.md
```

Backups are stored outside this directory at `~/mc-backups/` (configurable).

## Stopping CreeperCrest

If running manually: `Ctrl+C` - all running Minecraft servers are sent the `stop` command before exit.

If running as a service: `sudo systemctl stop creepercrest`

## Permissions Note

CreeperCrest must run as the same user that owns the server files. If your servers were previously managed by Crafty Controller they are likely owned by the `crafty` user - run CreeperCrest as `crafty` (see systemd service above).

## Support

If this project is useful to you, consider supporting its development via PayPal:

[![Donate with PayPal](.github/paypal-qr.png)](https://paypal.me/beangreen2471)

**PayPal:** https://paypal.me/beangreen2471
