# CreeperCrest

A small web panel for running Minecraft servers. One native C++ binary (about 1 MB) with the web UI and editor built in, works offline. The Python version has been removed and is no longer supported.

## Features

- Start, stop and restart servers, with a live console and command box
- Setup wizard: pick Paper, Vanilla, Purpur or Fabric and a version, and the JAR is downloaded for you
- Runtime per server: Java JAR, or a native binary such as [Painite](https://github.com/BeanGreen247/painite) (choose it in the wizard, or switch it later in Edit; a Browse button next to the JAR/binary field fills in the full path)
- File manager with a built-in code editor (syntax highlighting, find/replace, Ctrl+S); the editor is a small hand-written one of about 15 KB, not a bundled library
- Whitelist editor per server, with removal by UUID
- Resource packs: upload or download one and CreeperCrest hosts it
- One-click and scheduled backups, restore, per-server RAM and autostart; tick which servers the weekly scheduled backup covers
- Login with password and 2FA
- Options page per server: RAM and property sliders, toggles, an Apply button and a restart notice
- Build options (`O2`, `DISABLE_2FA`, `DISABLE_BACKUPS`, `DEBUG`) to tune or trim the binary, see [Build options](#build-options)

## Install

```bash
git clone https://github.com/BeanGreen247/creepercrest
cd creepercrest/cpp
sudo bash deploy.sh
```

Want a different build? You can choose `-O2` instead of `-O3`, turn off 2FA or backups, or make a debug build. See [Build options](#build-options) below, and build with them *before* running `deploy.sh`.

`deploy.sh` builds the binary if it is not built yet (needs `g++`, `make`, and the OpenSSL and libzip development packages, which it installs), then installs OpenJDK and `ufw`, opens the firewall ports you choose, creates login users and installs a systemd service. It is safe to rerun: it keeps `config.json`, `users.json` and your servers, but it never deletes old firewall rules, so check `sudo ufw status numbered` afterwards.

Skip the firewall steps with `CC_SKIP_FIREWALL=1 sudo -E bash deploy.sh`. Open `http://<server-ip>:8888` when it finishes.

Build and run by hand with `make -C cpp && cpp/creepercrest`. It keeps `config.json` and `users.json` next to the binary (or in the folder named by `CREEPERCREST_DIR`). Run it as the user that owns the server files.

### Restart and shut down the panel from the web page

For testing, local sessions and LAN-only setups, the page header shows **Restart panel** and **Shut down** buttons. They appear only when `lan_mode` or `auth_disabled` is set in `config.json`, and only for requests from a loopback or private address with no proxy header; everywhere else the API answers 403. Both stop every running Minecraft server first. Restart re-launches the same binary in place (the systemd service keeps running); shutdown exits cleanly, so the service stays stopped until you run `sudo systemctl start creepercrest`.

### Build options

Pass them to `make` as `NAME=y` or `NAME=n`. Leave one out and the default applies. Changing an option rebuilds everything.

| Option | Default | Effect when `y` |
|---|---|---|
| `O2` | `n` (builds with `-O3`) | Build with `-O2` instead of `-O3`, for architectures where `-O3` misbehaves |
| `DISABLE_2FA` | `n` | Login needs only the password, no authenticator code. Only use this on a trusted network |
| `DISABLE_BACKUPS` | `n` | Backups, restores, backup-based server creation and the backup schedule are compiled out |
| `DEBUG` | `n` | `-Og -g` build that logs every request to stderr |

```bash
make -C cpp O2=y DISABLE_BACKUPS=y
```

`deploy.sh` only builds when the binary is missing, so build with your options first, or run `CC_SKIP_FIREWALL=1 sudo -E env O2=y bash cpp/deploy.sh` on a fresh checkout. Run `make -C cpp clean` before switching options on an already built tree if you want a guaranteed clean build.

Update the C++ version with `sudo bash cpp/deploy.sh`; it keeps your settings.


## Ports

| Port | Purpose |
|------|---------|
| 8888/tcp | Panel and resource-pack downloads. Keep it on your LAN. |
| 25565/tcp | Java servers (one per server) |
| Geyser UDP (default 28258) | Bedrock players |

Players outside your network need the panel port to download a resource pack, so for them either host the pack elsewhere or reach the panel over a VPN.

## Server protection

Every time a server starts, CreeperCrest enforces:

- `white-list=true`, `enforce-whitelist=true`, `online-mode=true` (a server can opt out of the `online-mode` part with **Allow cracked/offline clients** in Edit)
- query and RCON off
- a 5 second login throttle per IP in `bukkit.yml` (Paper and Spigot)

Anyone refused by the whitelist is banned by name, and by IP when the address is public. LAN and proxy addresses are never IP-banned. Add players from the **Whitelist** button on the server card. The server must be running to add; removing works any time.

The whitelist matches the account UUID, so a hijacked account still gets in. If a friend's account is compromised, remove their UUID from the whitelist and ban the account until they have recovered it.

## Login and 2FA

The panel is locked until a user exists. Manage users on the host:

```bash
creepercrest adduser <name>             # prints a generated password and 2FA key once
creepercrest passwd <name>
creepercrest reset-2fa <name>
creepercrest deluser <name>
creepercrest users
```

Passwords are hashed with scrypt, logins are throttled, sessions last 8 h idle, and every state-changing request carries a CSRF token. The only public URL is `/resourcepack/<id>.zip`, because Minecraft clients cannot log in. Install `qrencode` to get a QR code for enrolment, both in the terminal and on the 2FA page. The 2FA page only shows the key and QR code after you type your password again, so a stolen session cookie is not enough to read it.

**LAN mode** (`creepercrest lan-mode on`) skips the login for private-network clients. Never port-forward the panel while it is on.

**On the internet:** prefer a VPN. Otherwise serve it over HTTPS, either behind a reverse proxy that sets `X-Forwarded-Proto` and `X-Forwarded-For` (then set `trusted_proxies`), or with `tls_cert` / `tls_key` in `config.json`.

## Configuration

`config.json` is created on first run.

| Key | Description |
|-----|-------------|
| `host`, `port` | Listen address and port (default `0.0.0.0:8888`) |
| `backup_dir` | Backup folder (default `~/mc-backups`) |
| `max_backups` | Backups kept per server, `0` = unlimited |
| `auto_backup` | Weekly schedule: `enabled`, `day`, `hour`, `minute` and optional `servers` (list of server ids; leave it out for all servers). Edit it from the Backups page |
| `lan_mode` | No login for private-network clients |
| `max_extract_mb` | Largest size a zip (import, restore, create from backup) may expand to. Default: the free space on the target disk minus 1 GB |
| `allow_private_fetch` | Let the panel download from private and loopback addresses, for example a resource pack on your LAN (default off, and not implied by `lan_mode`) |
| `trusted_proxies`, `allowed_hosts` | Reverse-proxy and hostname allow-lists |
| `tls_cert`, `tls_key` | Serve HTTPS directly |
| `refresh_interval` | UI refresh in seconds (default `5`) |
| `auth_disabled` | Turn the login off (not recommended) |

`servers` is managed by the UI.

## Files

```
cpp/src/               C++ sources: HTTP server, auth and 2FA, process manager, backups, API
cpp/ui/                web UI (index.html) and the built-in editor (editor.js), embedded in the binary
cpp/deploy.sh          installer for the C++ version
cpp/creepercrest.service  systemd unit template
tests/                 security regression tests that run against the C++ version
deploy.sh              installer
creepercrest.service   systemd unit template
config.json            settings
```

Backups are stored outside the project, in `~/mc-backups`.

## Security

CreeperCrest is a single-owner admin tool. Every login is a full administrator of the account the panel runs as: the **native runtime** starts any program you point it at, the folder picker can browse the whole disk, and a server's folder can be any path. Share a login only with people you would give a shell on that machine. Keep the panel on your LAN or behind a VPN, and serve it over HTTPS: without it, passwords, authenticator codes and session cookies cross the network unencrypted. `deploy.sh` offers to create a self-signed certificate for you.

What the panel does for you:

- Passwords are hashed with scrypt, 2FA codes can be used once, and logins are throttled per address and per user. Failed guesses from strangers cannot lock you out of an address you already log in from.
- Every state-changing request needs a CSRF token, cookies are `HttpOnly` and `SameSite=Strict`, and the pages are served with a content-security policy, frame and cross-origin isolation headers, and HSTS when HTTPS is on.
- The authenticator key is only shown after you type your password again.
- Files are written without following symlinks, paths are confined to the server folder, zips cannot escape or expand past `max_extract_mb` (default: free disk space minus 1 GB), and removing a server never deletes your home folder or the panel.
- Downloads (JARs, resource packs) refuse private and loopback addresses and connect only to the address that passed the check; `allow_private_fetch` turns that off on purpose.
- `config.json`, backups and the backup folder are private to the panel user, and at most 8 background tasks run at once.
- Only the server's own "not whitelisted" messages trigger the automatic bans, never chat text.
- HTTPS uses TLS 1.2 or 1.3 with forward-secret AES-GCM or ChaCha20 suites only.
- The systemd unit written by `deploy.sh` makes system paths read-only and limits devices, namespaces, sockets and capabilities. A real Paper server runs, downloads and backs up under it.

Known limits: the pages still use inline scripts, so the content-security policy cannot forbid them and output escaping is what prevents script injection; files that the panel itself rewrites inside a server folder (for example `server.properties`) follow a symlink if you deliberately made one; and all logins are equal administrators.

Run the security regression tests against the C++ version (each test starts its own throwaway panel in a temporary folder):

```bash
python3 tests/security_regression.py --backend cpp       # needs cpp/creepercrest built
python3 tests/security_regression.py --backend cpp --only forged_ban,symlink_upload
```

## Support

If this project is useful to you, consider supporting its development via PayPal:

[![Donate with PayPal](.github/paypal-qr.png)](https://paypal.me/beangreen2471)

**PayPal:** https://paypal.me/beangreen2471

## License

Source-available, all rights reserved. Read, build and run for personal use; no forks, redistribution or derivative works. See `LICENSE`.
