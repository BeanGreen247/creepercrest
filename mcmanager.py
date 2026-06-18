#!/usr/bin/env python3
"""MCManager — lightweight Minecraft server manager. Zero external dependencies."""

import os
import sys
import io
import json
import time
import zipfile
import threading
import subprocess
from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(BASE_DIR, "config.json")

_DEFAULT_CFG = {
    "host":       "0.0.0.0",
    "port":       8080,
    "backup_dir": "~/mc-backups",
    "servers":    {},
}

def load_cfg():
    if not os.path.exists(CFG_FILE):
        save_cfg(_DEFAULT_CFG.copy())
        return _DEFAULT_CFG.copy()
    with open(CFG_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_cfg(data):
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ── Managed server ─────────────────────────────────────────────────────────────

class ManagedServer:
    def __init__(self, sid, scfg):
        self.id        = sid
        self.cfg       = scfg
        self.process   = None
        self.logs      = deque(maxlen=300)
        self._lock     = threading.Lock()
        self._last_cpu = None  # (cpu_ticks, monotonic_time)

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        with self._lock:
            if self.is_running():
                return False, "Already running"
            directory = os.path.expanduser(self.cfg.get("directory", ""))
            jar       = self.cfg.get("jar", "server.jar")
            # min/max RAM — fall back to legacy memory_mb if new keys absent
            legacy    = self.cfg.get("memory_mb", 1024)
            min_mem   = int(self.cfg.get("memory_min_mb", legacy))
            max_mem   = int(self.cfg.get("memory_max_mb", legacy))
            extra     = self.cfg.get("extra_args", "").split()
            jar_path  = jar if os.path.isabs(jar) else os.path.join(directory, jar)
            if not os.path.isfile(jar_path):
                return False, f"JAR not found: {jar_path}"
            cmd = (
                ["java", f"-Xms{min_mem}M", f"-Xmx{max_mem}M"]
                + extra
                + ["-jar", jar_path, "--nogui"]
            )
            try:
                self.process = subprocess.Popen(
                    cmd, cwd=directory,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except FileNotFoundError:
                return False, "java not found — is a JRE installed?"
            except Exception as e:
                return False, str(e)
            self._append(f"[MCManager] Started PID {self.process.pid}  |  {' '.join(cmd)}")
            threading.Thread(target=self._tail, daemon=True, name=f"tail-{self.id}").start()
            return True, f"Started (PID {self.process.pid})"

    def stop(self, timeout=30):
        with self._lock:
            if not self.is_running():
                return False, "Not running"
            try:
                self.process.stdin.write("stop\n")
                self.process.stdin.flush()
            except Exception:
                pass
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self._append("[MCManager] Server stopped")
            self.process = None
            return True, "Stopped"

    def restart(self):
        if self.is_running():
            ok, msg = self.stop()
            if not ok:
                return False, msg
        return self.start()

    def send_command(self, cmd):
        if not self.is_running():
            return False, "Not running"
        try:
            self.process.stdin.write(cmd.rstrip() + "\n")
            self.process.stdin.flush()
            self._append(f"> {cmd}")
            return True, "Sent"
        except Exception as e:
            return False, str(e)

    def _append(self, line):
        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")

    def _tail(self):
        try:
            for line in self.process.stdout:
                self._append(line.rstrip())
        except Exception:
            pass

    def metrics(self):
        if not self.is_running():
            self._last_cpu = None
            return {"cpu_pct": 0.0, "rss_mb": 0.0}
        pid = self.process.pid
        try:
            rss_kb = 0
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
            with open(f"/proc/{pid}/stat") as fh:
                fields = fh.read().split()
            cpu_ticks = int(fields[13]) + int(fields[14])
            now = time.monotonic()
            hz  = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            cpu_pct = 0.0
            if self._last_cpu is not None:
                dt = now - self._last_cpu[1]
                dc = (cpu_ticks - self._last_cpu[0]) / hz
                if dt > 0.1:
                    cpu_pct = min(100.0, (dc / dt) * 100)
            self._last_cpu = (cpu_ticks, now)
            return {"cpu_pct": round(cpu_pct, 1), "rss_mb": round(rss_kb / 1024, 1)}
        except Exception:
            return {"cpu_pct": 0.0, "rss_mb": 0.0}

    def status(self):
        legacy  = self.cfg.get("memory_mb", 1024)
        m = self.metrics()
        return {
            "id":            self.id,
            "name":          self.cfg.get("name", self.id),
            "running":       self.is_running(),
            "pid":           self.process.pid if self.is_running() else None,
            "memory_min_mb": self.cfg.get("memory_min_mb", legacy),
            "memory_max_mb": self.cfg.get("memory_max_mb", legacy),
            "directory":     self.cfg.get("directory", ""),
            "jar":           self.cfg.get("jar", "server.jar"),
            "extra_args":    self.cfg.get("extra_args", ""),
            "cpu_pct":       m["cpu_pct"],
            "rss_mb":        m["rss_mb"],
        }

# ── Global state ───────────────────────────────────────────────────────────────

cfg     = load_cfg()
servers = {sid: ManagedServer(sid, sc) for sid, sc in cfg.get("servers", {}).items()}

# ── Backups ────────────────────────────────────────────────────────────────────

SKIP_DIRS = {"logs", "crash-reports", "debug"}

def do_backup(sid):
    if sid not in servers:
        return False, "Server not found"
    srv     = servers[sid]
    src     = os.path.expanduser(srv.cfg.get("directory", ""))
    bak_dir = os.path.expanduser(cfg.get("backup_dir", "~/mc-backups"))
    os.makedirs(bak_dir, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname   = f"{sid}-{ts}.zip"
    fpath   = os.path.join(bak_dir, fname)
    try:
        with zipfile.ZipFile(fpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in files:
                    fp = os.path.join(root, name)
                    zf.write(fp, os.path.relpath(fp, src))
        size_mb = round(os.path.getsize(fpath) / 1024 / 1024, 2)
        srv._append(f"[MCManager] Backup saved → {fpath}  ({size_mb} MB)")
        return True, {"file": fname, "path": fpath, "size_mb": size_mb}
    except Exception as e:
        return False, str(e)

def list_backups():
    bak_dir = os.path.expanduser(cfg.get("backup_dir", "~/mc-backups"))
    if not os.path.isdir(bak_dir):
        return []
    out = []
    for name in sorted(os.listdir(bak_dir), reverse=True):
        if not name.endswith(".zip"):
            continue
        fp = os.path.join(bak_dir, name)
        out.append({
            "name":    name,
            "size_mb": round(os.path.getsize(fp) / 1024 / 1024, 2),
            "created": datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M"),
        })
    return out

# ── File browser helpers ────────────────────────────────────────────────────────

def _safe_path(server_dir, rel):
    """Resolve rel inside server_dir; return None if it escapes the root."""
    base   = os.path.realpath(os.path.expanduser(server_dir))
    joined = os.path.realpath(os.path.join(base, rel.lstrip("/\\"))) if rel else base
    if joined == base or joined.startswith(base + os.sep):
        return joined
    return None

def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"

def _parse_multipart(raw, boundary):
    """Return list of (filename, bytes). Pure stdlib, no cgi module."""
    sep   = b"--" + boundary
    files = []
    for part in raw.split(sep)[1:]:
        if part.startswith(b"--"):
            break
        if b"\r\n\r\n" not in part:
            continue
        headers_raw, body = part.split(b"\r\n\r\n", 1)
        body = body.rstrip(b"\r\n")
        cd   = ""
        for line in headers_raw.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition"):
                cd = line.decode(errors="replace")
        fname = None
        for token in cd.split(";"):
            token = token.strip()
            if token.lower().startswith("filename="):
                fname = token[9:].strip().strip('"')
        if fname:
            files.append((fname, body))
    return files

# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCManager</title>
<script>(function(){const s=localStorage.getItem('mcm-theme');const d=window.matchMedia('(prefers-color-scheme: dark)').matches;if(s==='dark'||(s===null&&d))document.documentElement.setAttribute('data-theme','dark');})()</script>
<style>
:root{
  --bg:#ffffff;--text:#1a1a1a;--muted:#555555;
  --accent:#0066cc;--accent-h:#004499;
  --border:#dddddd;--section-bg:#f8f8f8;
  --up:#2e7d32;--down:#c62828;--degraded:#e65100;
  --bar-empty:#e0e0e0;
}
[data-theme="dark"]{
  --bg:#111111;--text:#e0e0e0;--muted:#999999;
  --accent:#4d9fff;--accent-h:#80bcff;
  --border:#2a2a2a;--section-bg:#1a1a1a;
  --up:#43a047;--down:#ef5350;--degraded:#fb8c00;
  --bar-empty:#2a2a2a;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  min-height:100vh;line-height:1.6}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-h);text-decoration:underline}

header{background:var(--bg);border-bottom:1px solid var(--border);
  padding:.9rem 2rem;display:flex;align-items:center;gap:1rem;
  position:sticky;top:0;z-index:10}
header h1{font-size:1.3rem;color:var(--text);font-weight:700}
.theme-btn{background:var(--section-bg);border:1px solid var(--border);border-radius:4px;
  padding:.25rem .55rem;cursor:pointer;color:var(--muted);font-size:.82rem;line-height:1.4}
.theme-btn:hover{color:var(--text);border-color:var(--accent)}
.refresh-ctrl{display:flex;align-items:center;gap:.4rem;font-size:.78rem;color:var(--muted);margin-left:auto}
.refresh-ctrl input{width:52px;background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:.25rem .4rem;border-radius:4px;font-size:.78rem;text-align:center;outline:none}
.refresh-ctrl input:focus{border-color:var(--accent)}
#upd{color:var(--muted);font-size:.8rem}

main{padding:1.5rem 2rem;max-width:1300px;margin:0 auto}
section+section{margin-top:2rem}
.sec-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:.9rem}
.sec-hdr h2{font-size:1rem;font-weight:600;color:var(--text);
  padding-bottom:.35rem;border-bottom:2px solid var(--accent)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:1rem}

.card{background:var(--section-bg);border:1px solid var(--border);border-radius:8px;padding:1.2rem}
.card-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:.9rem}
.card-name{font-size:1rem;font-weight:600;color:var(--text)}
.badge{font-size:.72rem;font-weight:700;padding:.2rem .55rem;border-radius:4px;
  border:1px solid var(--border);background:transparent}
.badge.on{border-color:var(--up);color:var(--up)}
.badge.off{border-color:var(--down);color:var(--down)}

.info{font-size:.78rem;color:var(--muted);margin-bottom:.85rem;line-height:1.7}
.info b{color:var(--text)}

.ram-row{display:flex;align-items:center;gap:.5rem;margin-bottom:.85rem;flex-wrap:wrap}
.ram-row label{font-size:.78rem;color:var(--muted);white-space:nowrap}
.ram-row input{background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:.3rem .55rem;border-radius:5px;font-size:.83rem;width:75px;outline:none}
.ram-row input:focus{border-color:var(--accent)}
.ram-sep{color:var(--border);font-size:.85rem}

.btn-row{display:flex;flex-wrap:wrap;gap:.45rem;margin-bottom:.85rem}
.btn{border:1px solid var(--border);border-radius:5px;padding:.38rem .8rem;font-size:.8rem;
  font-weight:500;cursor:pointer;transition:opacity .12s;
  background:var(--section-bg);color:var(--text)}
.btn:hover{opacity:.8}
.btn:disabled{opacity:.35;cursor:not-allowed}
.bg-green{background:transparent;color:var(--up);border-color:var(--up)}
.bg-red{background:transparent;color:var(--down);border-color:var(--down)}
.bg-blue{background:transparent;color:var(--accent);border-color:var(--accent)}
.bg-teal{background:transparent;color:#0097a7;border-color:#0097a7}
.bg-yellow{background:transparent;color:var(--degraded);border-color:var(--degraded)}
.bg-gray{background:var(--section-bg);color:var(--text);border-color:var(--border)}
.bg-danger{background:transparent;color:var(--down);border-color:var(--down)}

.con-toggle{font-size:.78rem;color:var(--accent);cursor:pointer;user-select:none;
  display:inline-block;margin-bottom:.5rem}
.console{display:none;background:var(--bg);border:1px solid var(--border);border-radius:5px;
  padding:.6rem .7rem;height:190px;overflow-y:auto;font-family:'Consolas',monospace;
  font-size:.76rem;color:var(--muted);margin-bottom:.6rem;line-height:1.55}
.console.open{display:block}
.console p{white-space:pre-wrap;word-break:break-all}
.cmd-row{display:none;gap:.4rem}
.cmd-row.open{display:flex}
.cmd-row input[type=text]{flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:.3rem .55rem;border-radius:5px;font-size:.83rem;font-family:monospace;outline:none}
.cmd-row input[type=text]:focus{border-color:var(--accent)}

.backup-row{display:flex;align-items:center;gap:1rem;padding:.6rem .9rem;
  background:var(--section-bg);border:1px solid var(--border);border-radius:6px;
  margin-bottom:.4rem;font-size:.82rem}
.backup-row input[type=checkbox]{accent-color:var(--accent);width:14px;height:14px;flex-shrink:0}
.backup-row .bname{flex:1;font-family:monospace;color:var(--text);font-size:.8rem;word-break:break-all}
.backup-row .bmeta{color:var(--muted);white-space:nowrap}

.empty{color:var(--muted);font-size:.88rem;padding:1.8rem;text-align:center;
  border:1px dashed var(--border);border-radius:8px}

.overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
  align-items:center;justify-content:center;z-index:100}
.overlay.open{display:flex}
.modal{background:var(--bg);border:1px solid var(--border);border-radius:10px;
  padding:1.5rem;width:min(520px,94vw)}
.modal h3{margin-bottom:1.1rem;color:var(--text);font-size:1rem;font-weight:600}
.frow{margin-bottom:.7rem}
.frow label{display:block;font-size:.78rem;color:var(--muted);margin-bottom:.3rem;font-weight:600}
.frow input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:.4rem .7rem;border-radius:5px;font-size:.85rem;outline:none}
.frow input:focus{border-color:var(--accent)}
.frow-2{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
.modal-btns{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1.1rem}

#fb-overlay{z-index:200}
.fb-modal{background:var(--bg);border:1px solid var(--border);border-radius:10px;
  width:min(900px,96vw);max-height:90vh;display:flex;flex-direction:column}
.fb-header{padding:1rem 1.2rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:.7rem}
.fb-header h3{color:var(--text);font-size:1rem;margin-right:auto}
.breadcrumb{display:flex;align-items:center;gap:.3rem;flex-wrap:wrap;font-size:.8rem;color:var(--muted);flex:1}
.breadcrumb span{cursor:pointer;color:var(--accent)}
.breadcrumb span:hover{text-decoration:underline}
.breadcrumb .sep{color:var(--border)}
.fb-toolbar{padding:.7rem 1.2rem;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.fb-toolbar label{font-size:.78rem;color:var(--muted);cursor:pointer;display:flex;align-items:center;gap:.35rem;margin-right:.5rem}
.fb-body{flex:1;overflow-y:auto;padding:0}
.fb-foot{padding:.6rem 1.2rem;border-top:1px solid var(--border);font-size:.75rem;color:var(--muted)}
.fb-table{width:100%;border-collapse:collapse}
.fb-table th{text-align:left;padding:.5rem 1rem;font-size:.75rem;font-weight:600;
  color:var(--muted);border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg)}
.fb-table td{padding:.45rem 1rem;font-size:.82rem;border-bottom:1px solid var(--section-bg)}
.fb-table tr:hover td{background:var(--section-bg)}
.fb-table .fn{color:var(--text);cursor:pointer}
.fb-table .fn:hover{color:var(--accent);text-decoration:underline}
.fb-table .dir .fn{color:var(--accent)}
.fb-table .fsize,.fb-table .fdate{color:var(--muted);white-space:nowrap}
.fb-table input[type=checkbox]{accent-color:var(--accent);width:14px;height:14px}
.fb-empty{padding:2rem;text-align:center;color:var(--muted);font-size:.88rem}
.upload-btn{position:relative;overflow:hidden;display:inline-block}
.upload-btn input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;font-size:100px}

.metrics-section{margin-bottom:.75rem}
.metric-row{display:flex;align-items:center;gap:.5rem;margin-bottom:.35rem}
.metric-lbl{font-size:.72rem;color:var(--muted);width:30px;flex-shrink:0;
  text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.bar-track{flex:1;height:7px;background:var(--bar-empty);border-radius:4px;overflow:hidden}
.bar-fill{height:100%;border-radius:4px;transition:width .5s ease}
.metric-val{font-size:.72rem;color:var(--muted);white-space:nowrap;min-width:90px;
  text-align:right;font-family:"JetBrains Mono","Fira Code",Consolas,monospace}

.flash{position:fixed;bottom:1.5rem;right:1.5rem;background:var(--section-bg);color:var(--text);
  border:1px solid var(--border);border-radius:6px;padding:.6rem 1rem;font-size:.85rem;
  opacity:0;transition:opacity .3s;z-index:300;max-width:340px;pointer-events:none}
.flash.show{opacity:1}
.flash.err{background:transparent;color:var(--down);border-color:var(--down)}
</style>
</head>
<body>
<header>
  <h1>&#9935; MCManager</h1>
  <button class="theme-btn" onclick="toggleTheme()" title="Toggle dark/light mode">&#9728; / &#9790;</button>
  <div class="refresh-ctrl">
    <label for="refresh-secs">Refresh every</label>
    <input id="refresh-secs" type="number" min="1" max="300" value="5"/>
    <span>s</span>
  </div>
  <span id="upd">connecting…</span>
</header>
<main>
  <section>
    <div class="sec-hdr">
      <h2>Servers</h2>
      <button class="btn bg-blue" onclick="openAdd()">+ Add Server</button>
    </div>
    <div class="grid" id="grid"><div class="empty">No servers configured yet.</div></div>
  </section>
  <section>
    <div class="sec-hdr">
      <h2>Backups</h2>
      <div style="display:flex;align-items:center;gap:.7rem">
        <small style="color:var(--muted);font-size:.78rem">Saved to <code id="bak-dir">~/mc-backups</code></small>
        <label style="font-size:.78rem;color:var(--muted);cursor:pointer;display:flex;align-items:center;gap:.3rem">
          <input type="checkbox" id="bak-selall" onchange="toggleBakSelAll(this.checked)" style="accent-color:var(--accent)"> All
        </label>
        <button class="btn bg-danger" id="bak-del" onclick="delBackups()" disabled style="font-size:.78rem;padding:.3rem .65rem">&#128465; Delete Selected</button>
      </div>
    </div>
    <div id="blist"><div class="empty">No backups yet.</div></div>
  </section>
</main>

<!-- Add server overlay -->
<div class="overlay" id="add-overlay">
  <div class="modal">
    <h3>Add Server</h3>
    <div class="frow-2">
      <div class="frow"><label>ID (letters, numbers, dash)</label><input id="f-id" placeholder="survival"/></div>
      <div class="frow"><label>Display Name</label><input id="f-name" placeholder="Survival SMP"/></div>
    </div>
    <div class="frow"><label>Server Directory (full path)</label><input id="f-dir" placeholder="/home/crafty/servers/survival"/></div>
    <div class="frow"><label>JAR filename</label><input id="f-jar" value="server.jar"/></div>
    <div class="frow-2">
      <div class="frow"><label>Min RAM (MB)</label><input id="f-min" type="number" value="512" min="256" step="256"/></div>
      <div class="frow"><label>Max RAM (MB)</label><input id="f-max" type="number" value="2048" min="256" step="256"/></div>
    </div>
    <div class="frow"><label>Extra JVM args</label><input id="f-args" value="-XX:+UseG1GC -XX:+UnlockExperimentalVMOptions -XX:MaxGCPauseMillis=200"/></div>
    <div class="modal-btns">
      <button class="btn bg-gray" onclick="closeAdd()">Cancel</button>
      <button class="btn bg-green" onclick="submitAdd()">Add Server</button>
    </div>
  </div>
</div>

<!-- File browser overlay -->
<div class="overlay" id="fb-overlay">
  <div class="fb-modal">
    <div class="fb-header">
      <h3 id="fb-title">Files</h3>
      <div class="breadcrumb" id="fb-crumb"></div>
      <button class="btn bg-gray" onclick="closeFB()" style="margin-left:.5rem">&#10005;</button>
    </div>
    <div class="fb-toolbar">
      <label><input type="checkbox" id="fb-selall" onchange="toggleSelAll(this.checked)"> All</label>
      <div class="upload-btn">
        <button class="btn bg-teal">&#8679; Upload</button>
        <input type="file" id="fb-upload" multiple onchange="doUpload()"/>
      </div>
      <button class="btn bg-blue"   onclick="dlSelected()">&#8681; Download</button>
      <button class="btn bg-yellow" onclick="dlZip()">&#128230; Download ZIP</button>
      <button class="btn bg-danger" onclick="delSelected()" id="fb-del">&#128465; Delete</button>
      <span id="fb-sel-info" style="margin-left:auto;font-size:.75rem;color:#7d8590"></span>
    </div>
    <div class="fb-body">
      <table class="fb-table">
        <thead><tr>
          <th style="width:20px"></th>
          <th>Name</th>
          <th>Size</th>
          <th>Modified</th>
        </tr></thead>
        <tbody id="fb-rows"></tbody>
      </table>
    </div>
    <div class="fb-foot" id="fb-foot"></div>
  </div>
</div>

<div class="flash" id="flash"></div>

<script>
const logOpen = {};
let fb = { sid: null, path: '', sel: new Set(), entries: [] };

// ── Utilities ──────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function flash(msg, err) {
  const el = document.getElementById('flash');
  el.textContent = msg;
  el.className = 'flash show' + (err ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 3500);
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  return r.json();
}

// ── Server cards ───────────────────────────────────────────────────────────────

function barColor(pct) {
  if (pct >= 85) return 'var(--down)';
  if (pct >= 60) return 'var(--degraded)';
  return 'var(--up)';
}

function metricsHTML(s) {
  const ramPct  = s.memory_max_mb > 0 ? Math.min(100, (s.rss_mb / s.memory_max_mb) * 100) : 0;
  const cpuPct  = s.cpu_pct || 0;
  const ramUsed = s.rss_mb >= 1024 ? (s.rss_mb / 1024).toFixed(2) + ' GB' : s.rss_mb + ' MB';
  const ramMax  = s.memory_max_mb >= 1024 ? (s.memory_max_mb / 1024).toFixed(1) + ' GB' : s.memory_max_mb + ' MB';
  return `
<div class="metrics-section">
  <div class="metric-row">
    <span class="metric-lbl">CPU</span>
    <div class="bar-track"><div class="bar-fill" style="width:${cpuPct}%;background:${barColor(cpuPct)}"></div></div>
    <span class="metric-val">${cpuPct}%</span>
  </div>
  <div class="metric-row">
    <span class="metric-lbl">RAM</span>
    <div class="bar-track"><div class="bar-fill" style="width:${ramPct.toFixed(1)}%;background:${barColor(ramPct)}"></div></div>
    <span class="metric-val">${ramUsed} / ${ramMax}</span>
  </div>
</div>`;
}

function cardHTML(s) {
  const run = s.running;
  return `
<div class="card" id="card-${s.id}">
  <div class="card-top">
    <span class="card-name">${esc(s.name)}</span>
    <span class="badge ${run?'on':'off'}">${run?'&#9679; RUNNING':'&#9675; STOPPED'}</span>
  </div>
  <div class="info">
    <b>Dir</b> ${esc(s.directory)}&nbsp;&nbsp;<b>JAR</b> ${esc(s.jar)}${s.pid?`&nbsp;&nbsp;<b>PID</b> ${s.pid}`:''}
  </div>
  <div class="ram-row">
    <label>Min RAM</label>
    <input id="min-${s.id}" type="number" value="${s.memory_min_mb}" min="256" step="256"/>
    <span style="color:#7d8590;font-size:.75rem">MB</span>
    <span class="ram-sep">|</span>
    <label>Max RAM</label>
    <input id="max-${s.id}" type="number" value="${s.memory_max_mb}" min="256" step="256"/>
    <span style="color:#7d8590;font-size:.75rem">MB</span>
    <button class="btn bg-gray" onclick="saveRAM('${s.id}')" style="font-size:.75rem;padding:.3rem .65rem">Save</button>
  </div>
  ${run ? metricsHTML(s) : ''}
  <div class="btn-row">
    <button class="btn bg-green"  ${run?'disabled':''} onclick="act('${s.id}','start')">&#9654; Start</button>
    <button class="btn bg-red"    ${run?'':'disabled'} onclick="act('${s.id}','stop')">&#9632; Stop</button>
    <button class="btn bg-blue"   onclick="act('${s.id}','restart')">&#8635; Restart</button>
    <button class="btn bg-teal"   onclick="openFB('${s.id}')">&#128193; Files</button>
    <button class="btn bg-yellow" onclick="doBackup('${s.id}',this)">&#128190; Backup</button>
    <button class="btn bg-danger" onclick="delServer('${s.id}')">Remove</button>
  </div>
  <span class="con-toggle" onclick="toggleLogs('${s.id}')">&#9654; Console</span>
  <div class="console" id="con-${s.id}"></div>
  <div class="cmd-row" id="cmd-${s.id}">
    <input type="text" id="inp-${s.id}" placeholder="say Hello World"
      onkeydown="if(event.key==='Enter')sendCmd('${s.id}')"/>
    <button class="btn bg-gray" onclick="sendCmd('${s.id}')">Send</button>
  </div>
</div>`;
}

function renderServers(list) {
  const grid = document.getElementById('grid');
  if (!list.length) { grid.innerHTML = '<div class="empty">No servers configured yet.</div>'; return; }
  const wasOpen = Object.keys(logOpen).filter(k => logOpen[k]);
  grid.innerHTML = list.map(cardHTML).join('');
  wasOpen.forEach(sid => { logOpen[sid] = true; openConsole(sid, false); });
}

const bakSel = new Set();

function renderBackups(list) {
  const el = document.getElementById('blist');
  if (!list.length) {
    el.innerHTML = '<div class="empty">No backups yet.</div>';
    bakSel.clear();
    updateBakDel();
    return;
  }
  el.innerHTML = list.map(b => `
<div class="backup-row">
  <input type="checkbox" data-name="${esc(b.name)}" onchange="toggleBakSel('${esc(b.name)}',this.checked)"
    ${bakSel.has(b.name) ? 'checked' : ''}>
  <span class="bname">${esc(b.name)}</span>
  <span class="bmeta">${b.size_mb} MB</span>
  <span class="bmeta">${b.created}</span>
  <a class="btn bg-gray" href="/backups/${encodeURIComponent(b.name)}">&#8681; Download</a>
</div>`).join('');
}

function toggleBakSel(name, checked) {
  checked ? bakSel.add(name) : bakSel.delete(name);
  updateBakDel();
}

function toggleBakSelAll(checked) {
  document.querySelectorAll('#blist input[type=checkbox]').forEach(cb => {
    cb.checked = checked;
    checked ? bakSel.add(cb.dataset.name) : bakSel.delete(cb.dataset.name);
  });
  updateBakDel();
}

function updateBakDel() {
  document.getElementById('bak-del').disabled = bakSel.size === 0;
}

async function delBackups() {
  if (!bakSel.size) return;
  if (!confirm(`Permanently delete ${bakSel.size} backup(s)? This cannot be undone.`)) return;
  let failed = 0;
  for (const name of [...bakSel]) {
    const r = await api('DELETE', `/api/backup/${encodeURIComponent(name)}`);
    if (r.ok) bakSel.delete(name); else failed++;
  }
  document.getElementById('bak-selall').checked = false;
  updateBakDel();
  if (failed) flash(`Done — ${failed} deletion(s) failed`, true);
  else flash('Backup(s) deleted');
  refresh();
}

async function refresh() {
  try {
    const d = await api('GET', '/api/status');
    renderServers(d.servers || []);
    renderBackups(d.backups || []);
    if (d.backup_dir) document.getElementById('bak-dir').textContent = d.backup_dir;
    document.getElementById('upd').textContent = 'Updated ' + new Date().toLocaleTimeString();
    for (const sid of Object.keys(logOpen)) {
      if (logOpen[sid]) fetchLogs(sid);
    }
  } catch {
    document.getElementById('upd').textContent = 'Connection lost';
  }
}

// ── Server actions ─────────────────────────────────────────────────────────────

async function act(sid, action) {
  const r = await api('POST', `/api/${sid}/${action}`);
  if (r.ok) flash(`${action}: ${r.msg}`);
  else flash(r.msg || r.error, true);
  refresh();
}

async function doBackup(sid, btn) {
  btn.disabled = true; btn.textContent = 'Backing up…';
  const r = await api('POST', `/api/${sid}/backup`);
  btn.disabled = false; btn.innerHTML = '&#128190; Backup';
  if (r.ok) flash(`Backup: ${r.result.file}  (${r.result.size_mb} MB)`);
  else flash('Backup failed: ' + r.result, true);
  refresh();
}

async function saveRAM(sid) {
  const min = parseInt(document.getElementById(`min-${sid}`).value);
  const max = parseInt(document.getElementById(`max-${sid}`).value);
  if (!min || min < 256) { flash('Min RAM must be at least 256 MB', true); return; }
  if (!max || max < min) { flash('Max RAM must be ≥ Min RAM', true); return; }
  const r = await api('POST', `/api/${sid}/config`, {memory_min_mb: min, memory_max_mb: max});
  if (r.ok) flash('RAM saved — restart server to apply');
  else flash(r.error, true);
}

async function sendCmd(sid) {
  const inp = document.getElementById(`inp-${sid}`);
  const cmd = inp.value.trim();
  if (!cmd) return;
  const r = await api('POST', `/api/${sid}/command`, {command: cmd});
  if (!r.ok) flash(r.msg || r.error, true);
  inp.value = '';
  fetchLogs(sid);
}

async function fetchLogs(sid) {
  const el = document.getElementById(`con-${sid}`);
  if (!el) return;
  const r = await api('GET', `/api/${sid}/logs`);
  if (!r.logs) return;
  el.innerHTML = r.logs.map(l => `<p>${esc(l)}</p>`).join('');
  el.scrollTop = el.scrollHeight;
}

function openConsole(sid, fetch = true) {
  const con = document.getElementById(`con-${sid}`);
  const cmd = document.getElementById(`cmd-${sid}`);
  const tog = document.querySelector(`#card-${sid} .con-toggle`);
  if (!con) return;
  con.classList.add('open'); cmd.classList.add('open');
  if (tog) tog.innerHTML = '&#9660; Console';
  if (fetch) fetchLogs(sid);
}

function toggleLogs(sid) {
  if (logOpen[sid]) {
    logOpen[sid] = false;
    document.getElementById(`con-${sid}`).classList.remove('open');
    document.getElementById(`cmd-${sid}`).classList.remove('open');
    const tog = document.querySelector(`#card-${sid} .con-toggle`);
    if (tog) tog.innerHTML = '&#9654; Console';
  } else {
    logOpen[sid] = true;
    openConsole(sid);
  }
}

async function delServer(sid) {
  if (!confirm(`Remove "${sid}" from MCManager?\nServer files will NOT be deleted.`)) return;
  const r = await api('DELETE', `/api/${sid}`);
  if (!r.ok) { flash(r.error, true); return; }
  flash(`Removed ${sid}`);
  refresh();
}

// ── Add server modal ───────────────────────────────────────────────────────────

function openAdd()  { document.getElementById('add-overlay').classList.add('open'); }
function closeAdd() { document.getElementById('add-overlay').classList.remove('open'); }

async function submitAdd() {
  const g = id => document.getElementById(id).value.trim();
  const body = {
    id:         g('f-id'),
    name:       g('f-name'),
    directory:  g('f-dir'),
    jar:        g('f-jar') || 'server.jar',
    memory_min_mb: parseInt(g('f-min')) || 512,
    memory_max_mb: parseInt(g('f-max')) || 2048,
    extra_args: g('f-args'),
  };
  if (!body.id)        { flash('ID is required', true); return; }
  if (!body.directory) { flash('Directory is required', true); return; }
  if (body.memory_min_mb > body.memory_max_mb) { flash('Max RAM must be ≥ Min RAM', true); return; }
  const r = await api('POST', '/api/add', body);
  if (!r.ok) { flash(r.error, true); return; }
  closeAdd();
  flash(`Server "${body.name || body.id}" added`);
  refresh();
}

// ── File browser ───────────────────────────────────────────────────────────────

function openFB(sid) {
  fb.sid  = sid;
  fb.path = '';
  fb.sel  = new Set();
  const srv = document.querySelector(`#card-${sid} .card-name`);
  document.getElementById('fb-title').textContent = 'Files — ' + (srv ? srv.textContent : sid);
  document.getElementById('fb-overlay').classList.add('open');
  loadDir('');
}

function closeFB() {
  document.getElementById('fb-overlay').classList.remove('open');
}

async function loadDir(path) {
  fb.path = path;
  fb.sel  = new Set();
  document.getElementById('fb-selall').checked = false;
  updateSelInfo();
  const r = await api('GET', `/api/${fb.sid}/files?path=${encodeURIComponent(path)}`);
  if (r.error) { flash(r.error, true); return; }
  fb.entries = r.entries || [];
  renderCrumb(r.path || '');
  renderRows(fb.entries);
  document.getElementById('fb-foot').textContent =
    `${fb.entries.filter(e=>e.type==='dir').length} folders, ` +
    `${fb.entries.filter(e=>e.type==='file').length} files`;
}

function renderCrumb(path) {
  const crumb = document.getElementById('fb-crumb');
  const parts = path ? path.split('/').filter(Boolean) : [];
  let html = `<span onclick="loadDir('')">&#127968; root</span>`;
  let acc  = '';
  for (const p of parts) {
    acc += (acc ? '/' : '') + p;
    const cur = acc;
    html += `<span class="sep">/</span><span onclick="loadDir('${esc(cur)}')">${esc(p)}</span>`;
  }
  crumb.innerHTML = html;
}

function renderRows(entries) {
  const tbody = document.getElementById('fb-rows');
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="fb-empty">Empty directory</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(e => {
    const icon = e.type === 'dir' ? '&#128193;' : fileIcon(e.name);
    const namePath = fb.path ? fb.path + '/' + e.name : e.name;
    const clickFn  = e.type === 'dir'
      ? `loadDir('${esc(namePath)}')`
      : `dlFile('${esc(namePath)}')`;
    return `<tr class="${e.type}">
  <td><input type="checkbox" data-name="${esc(e.name)}" onchange="toggleSel('${esc(e.name)}')"></td>
  <td>${icon} <span class="fn" onclick="${clickFn}">${esc(e.name)}</span></td>
  <td class="fsize">${e.type === 'dir' ? '—' : e.size}</td>
  <td class="fdate">${e.modified}</td>
</tr>`;
  }).join('');
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  const map = {jar:'&#9881;', zip:'&#128230;', gz:'&#128230;', json:'&#128221;',
               yml:'&#128221;', yaml:'&#128221;', txt:'&#128196;', log:'&#128196;',
               sh:'&#128196;', properties:'&#128221;'};
  return map[ext] || '&#128196;';
}

function toggleSel(name) {
  if (fb.sel.has(name)) fb.sel.delete(name); else fb.sel.add(name);
  updateSelInfo();
}

function toggleSelAll(checked) {
  fb.sel = checked ? new Set(fb.entries.map(e => e.name)) : new Set();
  document.querySelectorAll('#fb-rows input[type=checkbox]').forEach(cb => {
    cb.checked = checked;
  });
  updateSelInfo();
}

function updateSelInfo() {
  const n = fb.sel.size;
  document.getElementById('fb-sel-info').textContent = n ? `${n} selected` : '';
  document.getElementById('fb-del').disabled = n === 0;
}

function dlFile(relPath) {
  window.location.href = `/api/${fb.sid}/file?path=${encodeURIComponent(relPath)}`;
}

function dlSelected() {
  if (!fb.sel.size) { flash('Select at least one file', true); return; }
  const files = [...fb.sel];
  const paths = files.map(f => fb.path ? fb.path + '/' + f : f);
  if (files.length === 1) {
    const entry = fb.entries.find(e => e.name === files[0]);
    if (entry && entry.type === 'file') { dlFile(paths[0]); return; }
  }
  dlZip();
}

async function dlZip() {
  const names = fb.sel.size
    ? [...fb.sel].map(f => fb.path ? fb.path + '/' + f : f)
    : fb.entries.filter(e => e.type === 'file').map(e => fb.path ? fb.path + '/' + e.name : e.name);
  if (!names.length) { flash('No files to download', true); return; }
  const r = await fetch(`/api/${fb.sid}/zip`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({files: names}),
  });
  if (!r.ok) { flash('ZIP failed', true); return; }
  const blob = await r.blob();
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), {
    href: url, download: `${fb.sid}-files.zip`
  });
  a.click();
  URL.revokeObjectURL(url);
}

async function doUpload() {
  const input = document.getElementById('fb-upload');
  if (!input.files.length) return;
  const form = new FormData();
  for (const f of input.files) form.append('files', f, f.name);
  const r = await fetch(`/api/${fb.sid}/upload?path=${encodeURIComponent(fb.path)}`, {
    method: 'POST', body: form,
  });
  input.value = '';
  const d = await r.json();
  if (d.ok) flash(`Uploaded ${d.count} file(s)`);
  else flash(d.error, true);
  loadDir(fb.path);
}

async function delSelected() {
  const names = [...fb.sel];
  if (!names.length) return;
  if (!confirm(`Delete ${names.length} item(s)? This cannot be undone.`)) return;
  let failed = 0;
  for (const name of names) {
    const p = fb.path ? fb.path + '/' + name : name;
    const r = await api('DELETE', `/api/${fb.sid}/file?path=${encodeURIComponent(p)}`);
    if (!r.ok) failed++;
  }
  flash(failed ? `Done (${failed} failed)` : `Deleted ${names.length} item(s)`, !!failed);
  loadDir(fb.path);
}

// ── Theme ──────────────────────────────────────────────────────────────────────

function toggleTheme() {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
  localStorage.setItem('mcm-theme', isDark ? 'light' : 'dark');
}

// ── Boot ───────────────────────────────────────────────────────────────────────

let _refreshTimer = null;

function startRefresh() {
  const input = document.getElementById('refresh-secs');
  const secs  = Math.max(1, parseInt(input.value) || 5);
  input.value = secs;
  localStorage.setItem('mcm-refresh', secs);
  clearInterval(_refreshTimer);
  _refreshTimer = setInterval(refresh, secs * 1000);
}

// Restore saved interval
(function () {
  const saved = parseInt(localStorage.getItem('mcm-refresh'));
  if (saved >= 1) document.getElementById('refresh-secs').value = saved;
})();

document.getElementById('refresh-secs').addEventListener('change', startRefresh);

refresh();
startRefresh();
</script>
</body>
</html>
"""

# ── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, content_type, filename=None):
        self.send_response(200)
        self.send_header("Content-Type",   content_type)
        self.send_header("Content-Length", len(data))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def qs(self):
        return parse_qs(urlparse(self.path).query)

    def segs(self):
        return [s for s in urlparse(self.path).path.split("/") if s]

    # ── GET ────────────────────────────────────────────────────────────────────

    def do_GET(self):
        parts = self.segs()

        if not parts:
            return self.send_html(HTML)

        if parts == ["api", "status"]:
            return self.send_json({
                "servers":    [s.status() for s in servers.values()],
                "backups":    list_backups(),
                "backup_dir": os.path.expanduser(cfg.get("backup_dir", "~/mc-backups")),
            })

        if len(parts) == 3 and parts[0] == "api" and parts[2] == "logs":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json({"logs": list(servers[sid].logs)})

        # /api/{id}/files?path=
        if len(parts) == 3 and parts[0] == "api" and parts[2] == "files":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            rel  = unquote(self.qs().get("path", [""])[0])
            base = os.path.expanduser(servers[sid].cfg.get("directory", ""))
            safe = _safe_path(base, rel)
            if not safe or not os.path.isdir(safe):
                return self.send_json({"error": "invalid path"}, 400)
            entries = []
            for name in sorted(os.listdir(safe)):
                fp    = os.path.join(safe, name)
                stat  = os.stat(fp)
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                if os.path.isdir(fp):
                    entries.append({"name": name, "type": "dir",  "size": "—",                   "modified": mtime})
                else:
                    entries.append({"name": name, "type": "file", "size": _fmt_size(stat.st_size), "modified": mtime})
            # dirs first
            entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
            return self.send_json({"path": rel, "entries": entries})

        # /api/{id}/file?path=  — download single file
        if len(parts) == 3 and parts[0] == "api" and parts[2] == "file":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            rel  = unquote(self.qs().get("path", [""])[0])
            base = os.path.expanduser(servers[sid].cfg.get("directory", ""))
            safe = _safe_path(base, rel)
            if not safe or not os.path.isfile(safe):
                return self.send_json({"error": "file not found"}, 404)
            with open(safe, "rb") as f:
                data = f.read()
            self.send_bytes(data, "application/octet-stream", os.path.basename(safe))
            return

        # /backups/{filename}
        if len(parts) == 2 and parts[0] == "backups":
            fname   = parts[1]
            bak_dir = os.path.expanduser(cfg.get("backup_dir", "~/mc-backups"))
            fpath   = os.path.join(bak_dir, fname)
            if not fname.endswith(".zip") or not os.path.isfile(fpath):
                return self.send_json({"error": "not found"}, 404)
            with open(fpath, "rb") as f:
                data = f.read()
            self.send_bytes(data, "application/zip", fname)
            return

        self.send_json({"error": "not found"}, 404)

    # ── POST ───────────────────────────────────────────────────────────────────

    def do_POST(self):
        parts = self.segs()

        # /api/add
        if parts == ["api", "add"]:
            b   = self.body()
            sid = b.get("id", "").strip().lower().replace(" ", "-")
            if not sid:
                return self.send_json({"error": "id required"}, 400)
            if sid in servers:
                return self.send_json({"error": f'id "{sid}" already exists'}, 400)
            legacy = max(256, int(b.get("memory_mb", 1024)))
            scfg = {
                "name":          b.get("name", sid).strip() or sid,
                "directory":     b.get("directory", "").strip(),
                "jar":           b.get("jar", "server.jar").strip() or "server.jar",
                "memory_min_mb": max(256, int(b.get("memory_min_mb", legacy))),
                "memory_max_mb": max(256, int(b.get("memory_max_mb", legacy))),
                "extra_args":    b.get("extra_args", "").strip(),
            }
            if scfg["memory_min_mb"] > scfg["memory_max_mb"]:
                return self.send_json({"error": "max RAM must be >= min RAM"}, 400)
            cfg["servers"][sid] = scfg
            save_cfg(cfg)
            servers[sid] = ManagedServer(sid, scfg)
            return self.send_json({"ok": True, "id": sid})

        if len(parts) == 3 and parts[0] == "api":
            sid, action = parts[1], parts[2]
            if sid not in servers:
                return self.send_json({"error": "server not found"}, 404)
            srv = servers[sid]

            # /api/{id}/upload?path=
            if action == "upload":
                ct = self.headers.get("Content-Type", "")
                cl = int(self.headers.get("Content-Length", 0))
                if "boundary=" not in ct:
                    return self.send_json({"error": "expected multipart/form-data"}, 400)
                boundary = ct.split("boundary=")[1].strip().encode()
                raw      = self.rfile.read(cl)
                files    = _parse_multipart(raw, boundary)
                if not files:
                    return self.send_json({"error": "no files found in upload"}, 400)
                rel  = unquote(self.qs().get("path", [""])[0])
                base = os.path.expanduser(srv.cfg.get("directory", ""))
                dest = _safe_path(base, rel)
                if not dest:
                    return self.send_json({"error": "invalid path"}, 400)
                os.makedirs(dest, exist_ok=True)
                saved = 0
                for fname, data in files:
                    out = os.path.join(dest, os.path.basename(fname))
                    with open(out, "wb") as f:
                        f.write(data)
                    saved += 1
                return self.send_json({"ok": True, "count": saved})

            # /api/{id}/zip  — download selected files as zip
            if action == "zip":
                b     = self.body()
                paths = b.get("files", [])
                base  = os.path.expanduser(srv.cfg.get("directory", ""))
                buf   = io.BytesIO()
                count = 0
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for rel in paths:
                        safe = _safe_path(base, rel)
                        if safe and os.path.isfile(safe):
                            zf.write(safe, rel)
                            count += 1
                        elif safe and os.path.isdir(safe):
                            for root, _, files in os.walk(safe):
                                for name in files:
                                    fp    = os.path.join(root, name)
                                    arcn  = os.path.relpath(fp, base)
                                    zf.write(fp, arcn)
                                    count += 1
                if not count:
                    return self.send_json({"error": "no files matched"}, 400)
                self.send_bytes(buf.getvalue(), "application/zip", f"{sid}-files.zip")
                return

            # standard actions
            b = self.body()
            if action == "start":
                ok, msg = srv.start()
            elif action == "stop":
                ok, msg = srv.stop()
            elif action == "restart":
                ok, msg = srv.restart()
            elif action == "command":
                ok, msg = srv.send_command(b.get("command", ""))
            elif action == "backup":
                ok, result = do_backup(sid)
                return self.send_json({"ok": ok, "result": result})
            elif action == "config":
                if "memory_min_mb" in b:
                    srv.cfg["memory_min_mb"] = max(256, int(b["memory_min_mb"]))
                if "memory_max_mb" in b:
                    srv.cfg["memory_max_mb"] = max(256, int(b["memory_max_mb"]))
                if "extra_args" in b:
                    srv.cfg["extra_args"] = b["extra_args"]
                cfg["servers"][sid] = srv.cfg
                save_cfg(cfg)
                ok, msg = True, "Saved"
            else:
                return self.send_json({"error": "unknown action"}, 400)

            return self.send_json({"ok": ok, "msg": msg})

        self.send_json({"error": "not found"}, 404)

    # ── DELETE ─────────────────────────────────────────────────────────────────

    def do_DELETE(self):
        parts = self.segs()

        # /api/backup/{filename}  — delete a backup zip
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "backup":
            fname   = unquote(parts[2])
            bak_dir = os.path.expanduser(cfg.get("backup_dir", "~/mc-backups"))
            fpath   = os.path.join(bak_dir, fname)
            if not fname.endswith(".zip") or not os.path.isfile(fpath):
                return self.send_json({"error": "not found"}, 404)
            try:
                os.remove(fpath)
                return self.send_json({"ok": True})
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)

        # /api/{id}  — remove server
        if len(parts) == 2 and parts[0] == "api":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            if servers[sid].is_running():
                return self.send_json({"error": "stop the server before removing it"}, 400)
            del servers[sid]
            del cfg["servers"][sid]
            save_cfg(cfg)
            return self.send_json({"ok": True})

        # /api/{id}/file?path=  — delete file or directory
        if len(parts) == 3 and parts[0] == "api" and parts[2] == "file":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            rel  = unquote(self.qs().get("path", [""])[0])
            base = os.path.expanduser(servers[sid].cfg.get("directory", ""))
            safe = _safe_path(base, rel)
            if not safe or safe == os.path.realpath(base):
                return self.send_json({"error": "invalid path"}, 400)
            try:
                if os.path.isdir(safe):
                    import shutil
                    shutil.rmtree(safe)
                elif os.path.isfile(safe):
                    os.remove(safe)
                else:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json({"ok": True})
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)

        self.send_json({"error": "not found"}, 404)

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    import signal as _sig

    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8080))

    def shutdown(*_):
        print("\nShutting down — stopping all running servers…")
        for srv in servers.values():
            if srv.is_running():
                print(f"  Stopping {srv.id}…")
                srv.stop()
        sys.exit(0)

    _sig.signal(_sig.SIGTERM, shutdown)
    _sig.signal(_sig.SIGINT,  shutdown)

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"MCManager  →  http://{host}:{port}")
    print(f"Backups    →  {os.path.expanduser(cfg.get('backup_dir', '~/mc-backups'))}")
    print("Press Ctrl+C to stop.\n")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
