# CreeperCrest

A small web panel for running Minecraft servers. One native C++ binary (about 1 MB) with the web UI and editor built in, works offline. The original single-file Python version is kept in `python/` until the migration is complete; the C++ port in `cpp/` has the same features, the same `config.json` and `users.json`, and the same login and 2FA.

## Features

- Start, stop and restart servers, with a live console and command box
- Setup wizard: pick Paper, Vanilla, Purpur or Fabric and a version, and the JAR is downloaded for you
- Runtime per server: Java JAR, or a native binary such as [Painite](https://github.com/BeanGreen247/painite) (choose it in the wizard, or switch it later in Edit; a Browse button next to the JAR/binary field fills in the full path)
- File manager with a built-in code editor (syntax highlighting, find/replace, Ctrl+S); the editor is a small hand-written one of about 15 KB, not a bundled library
- Whitelist editor per server, with removal by UUID
- Resource packs: upload or download one and CreeperCrest hosts it
- One-click and scheduled backups, restore, per-server RAM and autostart; tick which servers the weekly scheduled backup covers
- Login with password and 2FA

## Install

```bash
git clone https://github.com/BeanGreen247/creepercrest
cd creepercrest/cpp
sudo bash deploy.sh
```

`deploy.sh` builds the binary if it is not built yet (needs `g++`, `make`, and the OpenSSL and libzip development packages, which it installs), then installs OpenJDK and `ufw`, opens the firewall ports you choose, creates login users and installs a systemd service. It is safe to rerun: it keeps `config.json`, `users.json` and your servers, but it never deletes old firewall rules, so check `sudo ufw status numbered` afterwards.

Skip the firewall steps with `CC_SKIP_FIREWALL=1 sudo -E bash deploy.sh`. Open `http://<server-ip>:8888` when it finishes.

Build and run by hand with `make -C cpp && cpp/creepercrest`. It keeps `config.json` and `users.json` next to the binary (or in the folder named by `CREEPERCREST_DIR`). Run it as the user that owns the server files. The Python version still runs with `python3 python/creepercrest.py` and installs with `sudo bash python/deploy.sh`.

## Python version: keeping it, switching, migrating fully

The Python version in `python/` and the C++ version in `cpp/` share the same install folder (`~/creepercrest`), the same `creepercrest` systemd service, the same port, and the same `config.json` and `users.json`. Only one of them can run at a time. The page header tells you which one you are talking to: a green **C++ BACKEND** pill, or an amber **PYTHON BACKEND** pill.

### Updating a clone you deployed from (when `git pull` stops)

If you installed by running `deploy.sh` inside your clone, your live `config.json` sits in the repository folder. Older versions kept `config.json` and `creepercrest.service` in the repository root, and they now live in `python/`, so `git pull` stops with "Your local changes to the following files would be overwritten". Back your files up, let the pull go through, then put your config back and redeploy. After the pull the Python script has moved too, so redeploy right away: the running service keeps working, but the next restart or reboot fails until you do.

Stay on Python:

```bash
cd ~/creepercrest
cp config.json ~/config.json.bak
cp users.json ~/users.json.bak
cp creepercrest.service ~/creepercrest.service.bak
git checkout -- config.json creepercrest.service
git pull
cp ~/config.json.bak config.json
sudo bash python/deploy.sh
```

Switch to C++ instead: run the same first six lines, then `cd cpp && sudo bash deploy.sh`.

Restoring `config.json` is what brings your servers back; without it the installer creates an empty config. `users.json` is not tracked by git, so your logins and 2FA are untouched. The installer writes the real service file under `/etc/systemd/system` itself, so the `creepercrest.service` backup only matters if you edited that template on purpose. Because the clone and the install are the same folder, the installer leaves `creepercrest.py`, `static/` and `config.json` as untracked files in the repository root; that is harmless, or clone a second copy elsewhere to pull from.

### Already running the Python version

Nothing breaks and nothing needs doing. The installed copy keeps running. After a `git pull`, the Python installer lives in `python/`, so update it with:

```bash
sudo bash python/deploy.sh
```

### Switching to the C++ version

```bash
cd creepercrest/cpp
sudo bash deploy.sh
```

Your users, 2FA codes, servers and settings carry over. Everyone is signed out once. Check it worked:

```bash
systemctl cat creepercrest | grep ExecStart    # should end in .../creepercrest, not python3
```

and look for the green **C++ BACKEND** pill in the page header.

### Going back to Python

```bash
sudo bash python/deploy.sh
```

This rewrites the service to start the Python script again. One difference to know about: the per-server tick for scheduled backups is only understood by the C++ version, so Python backs up every server on the schedule.

### Migrating fully (removing the Python version)

Do this only after the C++ version has run correctly for you for a while, because it removes your way back.

1. Back up your settings: `cp ~/creepercrest/config.json ~/creepercrest/users.json ~/`
2. Switch with `cpp/deploy.sh` as above, and confirm the green pill and the `ExecStart` line.
3. Remove the Python leftovers from the install folder (the C++ binary needs none of them):
   ```bash
   rm -f ~/creepercrest/creepercrest.py
   rm -rf ~/creepercrest/static ~/creepercrest/__pycache__
   ```
   Keep `creepercrest` (the binary), `config.json`, `users.json` and your server folders.
4. Optional: if nothing else on the machine uses them, remove the Python packages the old installer added: `sudo apt remove python3-qrcode python3-qrcodegen` (leave `python3` itself alone, since the system usually needs it). Keep `qrencode`; the C++ version uses it for QR codes.
5. Optional, in your clone of this repository: delete the Python version with `git rm -r python/` and commit. After that, `python/deploy.sh` no longer exists, so going back means checking out an older commit.

Run `sudo bash cpp/deploy.sh` again whenever you update the C++ version; it keeps your settings.

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

## C++ version versus Python version

Measured on the dev machine (Debian 13, 2 cores) with an empty panel and the same `users.json`. Memory is `VmRSS` from `/proc`; "private" is `RssAnon`, the part that is not the program file or shared libraries.

| | Python | C++ |
|---|---|---|
| Time from launch to first page served | 0.37 s | 0.02 s |
| Memory used, idle | 32 MB | 9.7 MB |
| Private (anonymous) memory, idle | 18.9 MB | 1.3 MB |
| CPU used over 60 s idle | 0.02 s | 0.00 s |
| Peak memory while uploading a 200 MB file | 852 MB | 42 MB |
| Program size | 4,315-line script plus 651 KB editor bundle, needs Python | 1.06 MB binary, editor 15 KB |
| Runtime needs | Python 3 | OpenSSL and libzip libraries |

The C++ version also reads host and process CPU only when the page asks for it, so it never wakes up on its own except for a once-a-minute backup schedule check. The Minecraft server itself is unaffected by the choice: a Paper server in the same test used about 800 to 1,030 MB, so the panel is a small part of the total either way. These are single runs on one machine; repeat them with `ps` and `/proc/<pid>/status` before relying on the exact numbers.

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
tests/                 security regression tests that run against either version
python/                original Python version: creepercrest.py, static/ (CodeMirror bundle), deploy.sh, service file, default config.json
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

Run the security regression tests against either version (each test starts its own throwaway panel in a temporary folder):

```bash
python3 tests/security_regression.py --backend cpp       # needs cpp/creepercrest built
python3 tests/security_regression.py --backend python
python3 tests/security_regression.py --backend cpp --only forged_ban,symlink_upload
```

## Support

If this project is useful to you, consider supporting its development via PayPal:

[![Donate with PayPal](.github/paypal-qr.png)](https://paypal.me/beangreen2471)

**PayPal:** https://paypal.me/beangreen2471

## License

Source-available, all rights reserved. Read, build and run for personal use; no forks, redistribution or derivative works. See `LICENSE`.
