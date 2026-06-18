# CreeperCrest

Lightweight Minecraft server management panel. No external dependencies — pure Python stdlib only.

## Features

- Start, stop, and restart servers
- Live console output per server with command input
- Memory (RAM) allocation per server
- One-click backups — zipped and saved to `~/mc-backups`
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

The deploy script will ask which user to run as, copy files, install and start the systemd service.

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
| `refresh_interval` | How often the UI polls for status updates, in seconds (default: `5`) |
| `servers` | Managed automatically by the UI — do not edit by hand |

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
| Extra JVM args | G1GC flags etc. — safe to leave as default |

## Backups

Clicking **Backup** on a server card:

- Zips the entire server directory (skips `logs/` and `crash-reports/` to save space)
- Saves the zip to `~/mc-backups/<server-id>-YYYYMMDD-HHMMSS.zip`
- The zip appears in the **Backups** section with a **Download** link
- The server does not need to be stopped to take a backup

## File Layout

```
creepercrest/
├── creepercrest.py      # everything — web server, process manager, backup logic
├── creepercrest.service # systemd service template (User=crafty)
├── deploy.sh            # interactive install script
├── config.json          # auto-managed, edit only host/port/backup_dir
└── README.md
```

Backups are stored outside this directory at `~/mc-backups/` (configurable).

## Stopping CreeperCrest

If running manually: `Ctrl+C` — all running Minecraft servers are sent the `stop` command before exit.

If running as a service: `sudo systemctl stop creepercrest`

## Permissions Note

CreeperCrest must run as the same user that owns the server files. If your servers were previously managed by Crafty Controller they are likely owned by the `crafty` user — run CreeperCrest as `crafty` (see systemd service above).
