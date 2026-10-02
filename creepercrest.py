#!/usr/bin/env python3
"""CreeperCrest - lightweight Minecraft server manager. Zero external dependencies.
By BeanGreen247 - https://github.com/BeanGreen247/creepercrest
"""

import os
import sys
import io
import re
import json
import zipfile
import time
import threading
import subprocess
import shutil
import hashlib
import socket
import uuid
import base64
import ipaddress
import ssl
import hmac
import secrets
import struct
from html import escape as html_escape
from http.cookies import SimpleCookie
import urllib.request
from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(BASE_DIR, "config.json")

_DEFAULT_CFG = {
    "host":             "0.0.0.0",
    "port":             8080,
    "backup_dir":       "~/mc-backups",
    "refresh_interval": 5,
    "servers":          {},
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

# ── Background jobs (progress reporting) ───────────────────────────────────────

_jobs = {}
_jobs_lock = threading.Lock()

class Job:
    def __init__(self, label, unit):
        self.id     = uuid.uuid4().hex[:12]
        self.label  = label
        self.unit   = unit      # "bytes" or "files"
        self.done   = 0
        self.total  = 0
        self.state  = "running"
        self.result = None
        self.error  = None
        self.t      = time.time()

    def view(self):
        return {"state": self.state, "label": self.label, "unit": self.unit, "done": self.done,
                "total": self.total, "result": self.result, "error": self.error}

def start_job(label, fn, unit="bytes"):
    """Run fn(job) in a thread; the UI polls /api/job/<id> for progress."""
    job = Job(label, unit)
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if j.state != "running" and time.time() - j.t > 600]:
            del _jobs[k]
        _jobs[job.id] = job
    def run():
        try:
            job.result = fn(job)
            job.state  = "done"
        except Exception as e:
            job.error = str(e) or e.__class__.__name__
            job.state = "error"
        job.t = time.time()
    threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
    return job.id

def _expect_ok(res):
    ok, val = res
    if not ok:
        raise RuntimeError(val)
    return val

def _extract_zip(zf, dest, job=None):
    members = zf.infolist()
    if job:
        job.unit, job.total, job.done = "files", len(members), 0
    for m in members:
        zf.extract(m, dest)
        if job:
            job.done += 1

# ── Default JVM arguments ──────────────────────────────────────────────────────

_JVM_BASE_ARGS = "-XX:+UseG1GC -XX:+UnlockExperimentalVMOptions -XX:MaxGCPauseMillis=100 -XX:+ParallelRefProcEnabled -XX:+DisableExplicitGC -XX:+AlwaysPreTouch -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M -XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 -XX:InitiatingHeapOccupancyPercent=20 -XX:G1MixedGCLiveThresholdPercent=90 -XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1"

_java_major = None

def host_java_major():
    """Major version of the `java` on PATH, or None."""
    global _java_major
    if _java_major is None:
        try:
            out = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=10).stderr
            m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
            if m:
                _java_major = int(m.group(2) or 0) if m.group(1) == "1" else int(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
        _java_major = _java_major or 0
    return _java_major or None

def required_java(ver):
    """Java major version Mojang lists for a Minecraft release, or None if unknown."""
    m = _get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    entry = next((v for v in m["versions"] if v["id"] == ver), None)
    if not entry:
        return None
    return _get_json(entry["url"]).get("javaVersion", {}).get("majorVersion")

def default_jvm_args():
    """Tuned G1 flags; GC thread counts follow the host CPU count (all threads parallel, half concurrent)."""
    n = os.cpu_count() or 4
    return f"{_JVM_BASE_ARGS} -XX:ParallelGCThreads={n} -XX:ConcGCThreads={max(1, n // 2)}"

# ── System info ────────────────────────────────────────────────────────────────

_sysinfo_cache = {
    'cpu_pct': None, 'ram_used_mb': None, 'ram_total_mb': None,
    'disk_used_gb': None, 'disk_total_gb': None,
}
_cpu_stat_prev  = None  # (idle_jiffies, total_jiffies)
_pid_cpu_cache  = {}    # { pid: cpu_pct }
_pid_cpu_prev   = {}    # { pid: (proc_jiffies, sys_total_jiffies) }

def _sysinfo_sampler():
    global _cpu_stat_prev, _sysinfo_cache, _pid_cpu_prev, _pid_cpu_cache
    while True:
        sys_total = None
        try:
            with open('/proc/stat') as f:
                raw = f.readline().split()[1:]      # all fields: user nice sys idle iowait irq softirq steal …
            vals  = list(map(int, raw))
            # guest/guest_nice are already counted inside user/nice - exclude to avoid double-counting
            idle  = vals[3] + vals[4]               # idle + iowait
            total = sum(vals[:8])                   # user nice sys idle iowait irq softirq steal
            sys_total = total
            if _cpu_stat_prev is not None:
                d_idle, d_total = idle - _cpu_stat_prev[0], total - _cpu_stat_prev[1]
                if d_total > 0:
                    _sysinfo_cache['cpu_pct'] = round(100.0 * (1.0 - d_idle / d_total), 1)
            _cpu_stat_prev = (idle, total)
        except Exception:
            pass
        # Per-process CPU using /proc/<pid>/stat deltas against the same system total
        if sys_total is not None:
            active_pids = set()
            for srv in list(servers.values()):
                if not srv.is_running():
                    continue
                pid = srv.process.pid
                active_pids.add(pid)
                try:
                    with open(f'/proc/{pid}/stat') as f:
                        data = f.read()
                    # comm field may contain spaces; parse past closing paren
                    rest = data[data.rfind(')') + 2:].split()
                    proc_time = int(rest[11]) + int(rest[12])  # utime + stime
                    if pid in _pid_cpu_prev:
                        prev_proc, prev_total = _pid_cpu_prev[pid]
                        d_proc  = proc_time - prev_proc
                        d_total = sys_total  - prev_total
                        if d_total > 0:
                            _pid_cpu_cache[pid] = round(100.0 * d_proc / d_total, 1)
                    _pid_cpu_prev[pid] = (proc_time, sys_total)
                except Exception:
                    pass
            # Clean up stale entries
            for pid in list(_pid_cpu_cache):
                if pid not in active_pids:
                    _pid_cpu_cache.pop(pid, None)
                    _pid_cpu_prev.pop(pid, None)
        try:
            info = {}
            with open('/proc/meminfo') as f:
                for line in f:
                    k, v = line.split(':')
                    info[k.strip()] = int(v.split()[0])
            _sysinfo_cache['ram_total_mb'] = info['MemTotal'] // 1024
            _sysinfo_cache['ram_used_mb']  = (info['MemTotal'] - info['MemAvailable']) // 1024
        except Exception:
            pass
        try:
            st = os.statvfs('/')
            _sysinfo_cache['disk_total_gb'] = round(st.f_blocks * st.f_frsize / 1_073_741_824, 1)
            _sysinfo_cache['disk_used_gb']  = round((st.f_blocks - st.f_bavail) * st.f_frsize / 1_073_741_824, 1)
        except Exception:
            pass
        time.sleep(2)

def _get_sysinfo():
    return dict(_sysinfo_cache)

# ── Managed server ─────────────────────────────────────────────────────────────

def _set_properties(directory, updates):
    """Set keys in <directory>/server.properties, preserving other lines."""
    path = os.path.join(os.path.expanduser(directory), "server.properties")
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

CONNECTION_THROTTLE_MS = 5000    # Bukkit/Paper: min ms between logins from one IP (default 4000); keep low, remote players may share a tunnel IP

def _harden_server(directory):
    """Force whitelist + online mode + login throttling before every start."""
    directory = os.path.expanduser(directory)
    _set_properties(directory, {
        "white-list":               "true",
        "enforce-whitelist":        "true",
        "online-mode":              "true",
        "enable-query":             "false",
        "enable-rcon":              "false",
    })
    path = os.path.join(directory, "bukkit.yml")
    line = f"  connection-throttle: {CONNECTION_THROTTLE_MS}"
    try:
        text = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        if re.search(r"^\s*connection-throttle:", text, re.M):
            text = re.sub(r"^\s*connection-throttle:.*$", line, text, flags=re.M)
        elif re.search(r"^settings:\s*$", text, re.M):
            text = re.sub(r"^settings:\s*$", "settings:\n" + line, text, count=1, flags=re.M)
        else:
            text += ("\n" if text and not text.endswith("\n") else "") + "settings:\n" + line + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass

_PLAYER_RE = re.compile(r"[A-Za-z0-9_.]{1,32}")

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")

def _whitelist_path(srv):
    return os.path.join(os.path.expanduser(srv.cfg.get("directory", "")), "whitelist.json")

def whitelist_entries(srv):
    try:
        with open(_whitelist_path(srv), encoding="utf-8") as f:
            return sorted(({"name": e.get("name", ""), "uuid": e.get("uuid", "")} for e in json.load(f)
                           if e.get("name") or e.get("uuid")), key=lambda e: e["name"].lower())
    except (OSError, ValueError, AttributeError):
        return []

def whitelist_remove_uuid(srv, uuid_str):
    """Drop a UUID from whitelist.json (works while stopped; a running server is told to reload and kicks them)."""
    want = uuid_str.replace("-", "").lower()
    path = _whitelist_path(srv)
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return False, "No whitelist.json to edit"
    kept = [e for e in entries if str(e.get("uuid", "")).replace("-", "").lower() != want]
    if len(kept) == len(entries):
        return False, "That UUID is not on the whitelist"
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kept, f, indent=2)
    os.replace(tmp, path)
    if srv.is_running():
        srv.send_command("whitelist reload")
    return True, "Removed"

_NOT_WL_RE = re.compile(r"not\s+white-?listed", re.I)
_ADDR_RE   = re.compile(r"/((?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]{3,39}):\d+")
_NAME_RES  = (re.compile(r"name=([A-Za-z0-9_.]{1,32})[,)]"),
              re.compile(r"Disconnecting ([A-Za-z0-9_.]{1,32}) \("),
              re.compile(r"([A-Za-z0-9_.]{1,32})\[/[^\]]+\] lost connection"))

def _check_not_whitelisted(srv, line):
    """Auto-ban a name (always) and its IP (public IPs only) when it is refused by the whitelist."""
    if not _NOT_WL_RE.search(line):
        return
    name = next((m.group(1) for r in _NAME_RES if (m := r.search(line))), None)
    m = _ADDR_RE.search(line)
    ip = m.group(1) if m else None
    if name and name not in srv._banned:
        srv._banned.add(name)
        srv.send_command(f"ban {name} Not whitelisted")
    if ip and ip not in srv._banned:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return
        if a.is_global:     # never ban a LAN/proxy address: that would lock out every player behind it
            srv._banned.add(ip)
            srv.send_command(f"ban-ip {ip} Not whitelisted")

def _check_pack(url, sha1, prompt=""):
    if re.search(r"[\r\n]", prompt) or len(prompt) > 256:
        return "Resource pack prompt must be one line, 256 characters or fewer"
    if url and not re.match(r"^https?://", url, re.I):
        return "Resource pack URL must start with http:// or https://"
    if sha1 and not re.fullmatch(r"[0-9a-f]{40}", sha1):
        return "Resource pack SHA-1 must be 40 hex characters"
    return None

def _apply_resource_pack(scfg):
    """Write the pack keys to server.properties; 1.20.3+ also wants a stable resource-pack-id UUID."""
    if scfg.get("resource_pack") and not scfg.get("resource_pack_id"):
        scfg["resource_pack_id"] = str(uuid.uuid4())
    _set_properties(scfg["directory"], {
        "resource-pack":         scfg.get("resource_pack", ""),
        "resource-pack-sha1":    scfg.get("resource_pack_sha1", ""),
        "resource-pack-id":      scfg.get("resource_pack_id", "") if scfg.get("resource_pack") else "",
        "require-resource-pack": _bool(scfg.get("resource_pack_required", False)),
        "resource-pack-prompt":  _prop_escape(scfg.get("resource_pack_prompt", "")),
    })

RP_MAX_BYTES = 250 * 1024 * 1024

def _rp_path(srv):
    return os.path.join(os.path.expanduser(srv.cfg.get("directory", "")), "resource-pack.zip")

def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

def _pack_base(host):
    """Public base URL for the hosted pack; a localhost Host header is useless to players, so use the LAN IP."""
    if cfg.get("public_url"):
        return cfg["public_url"].rstrip("/")
    name, _, port = host.partition(":") if not host.startswith("[") else (host, "", "")
    if name in ("localhost", "127.0.0.1", "[::1]"):
        name = _lan_ip()
    return f"http://{name}:{port}" if port else f"http://{name}"

def _rp_result(srv, url):
    return {"msg": url, "sha1": srv.cfg.get("resource_pack_sha1", "")}

def install_resource_pack(srv, data, host):
    """Validate + store a pack zip in the server dir, host it via CreeperCrest, point server.properties at it."""
    if len(data) > RP_MAX_BYTES:
        return False, "Pack larger than 250 MB"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "pack.mcmeta" not in zf.namelist():
                return False, "Not a resource pack (pack.mcmeta missing at zip root)"
    except zipfile.BadZipFile:
        return False, "Not a valid zip file"
    directory = os.path.expanduser(srv.cfg.get("directory", ""))
    if not directory or not os.path.isdir(directory):
        return False, "Server directory not found"
    with open(_rp_path(srv), "wb") as f:
        f.write(data)
    base = _pack_base(host)
    srv.cfg["resource_pack"]      = f"{base}/resourcepack/{srv.id}.zip"
    srv.cfg["resource_pack_sha1"] = hashlib.sha1(data).hexdigest()
    _apply_resource_pack(srv.cfg)
    cfg["servers"][srv.id] = srv.cfg
    save_cfg(cfg)
    srv._append(f"[CreeperCrest] Resource pack installed ({len(data)//1024} KB), hosted at {srv.cfg['resource_pack']}")
    return True, srv.cfg["resource_pack"]

def _assert_public_url(url):
    """Refuse URLs that resolve to loopback/private/link-local addresses (SSRF), unless LAN mode allows it."""
    if cfg.get("lan_mode") or cfg.get("allow_private_fetch"):
        return
    host = urlparse(url).hostname
    if not host:
        raise ValueError("invalid URL")
    for info in socket.getaddrinfo(host, None):
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ValueError("refusing to fetch from a private or internal address")

class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not re.match(r"^https?://", newurl, re.I):
            raise ValueError("redirect to a non-http URL refused")
        _assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_opener = urllib.request.build_opener(_SafeRedirect)

def _stream(url, write, job=None, limit=None):
    """Stream url into write(); socket timeout is per read, so a stalled host errors out instead of hanging."""
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("URL must start with http:// or https://")
    _assert_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "CreeperCrest"})
    with _opener.open(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        if limit and total > limit:
            raise ValueError(f"File larger than {limit // 1048576} MB")
        if job:
            job.unit, job.total, job.done = "bytes", total, 0
        n = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if limit and n > limit:
                raise ValueError(f"File larger than {limit // 1048576} MB")
            write(chunk)
            if job:
                job.done = n

def fetch_url(url, job=None):
    buf = io.BytesIO()
    _stream(url, buf.write, job, RP_MAX_BYTES)
    return buf.getvalue()

# ── server.properties validation (wizard) ──────────────────────────────────────

def _bool(v):
    return "true" if str(v).lower() in ("true", "1", "yes", "on") else "false"

_ENUM = lambda *vals: {v: v for v in vals}
_PROP_SPECS = {
    "motd":                 ("str", 200),
    "level-name":           ("name", 60),
    "level-seed":           ("str", 200),
    "level-type":           ("enum", {"normal": "minecraft\\:normal", "flat": "minecraft\\:flat",
                                      "large_biomes": "minecraft\\:large_biomes",
                                      "amplified": "minecraft\\:amplified"}),
    "gamemode":             ("enum", _ENUM("survival", "creative", "adventure", "spectator")),
    "difficulty":           ("enum", _ENUM("peaceful", "easy", "normal", "hard")),
    "max-players":          ("int", 1, 1000),
    "server-port":          ("int", 1024, 65535),
    "view-distance":        ("int", 2, 32),
    "simulation-distance":  ("int", 3, 32),
    "spawn-protection":     ("int", 0, 1000),
    "online-mode":          ("bool",),
    "pvp":                  ("bool",),
    "hardcore":             ("bool",),
    "white-list":           ("bool",),
    "allow-flight":         ("bool",),
    "allow-nether":         ("bool",),
    "enable-command-block": ("bool",),
}

def _prop_escape(v):
    out = []
    for ch in v.replace("\\", "\\\\"):
        out.append(ch if 32 <= ord(ch) < 127 else f"\\u{ord(ch):04x}")
    return "".join(out)

def validate_props(raw):
    """Return (props ready to write, error)."""
    out = {}
    for k, v in (raw or {}).items():
        spec = _PROP_SPECS.get(k)
        if not spec:
            return None, f"unknown property: {k}"
        v = str(v).strip()
        kind = spec[0]
        if kind in ("str", "name"):
            if re.search(r"[\r\n]", v) or len(v) > spec[1]:
                return None, f"invalid value for {k}"
            if kind == "name" and (not v or re.search(r"[/\\]|\.\.", v)):
                return None, f"invalid value for {k}"
            out[k] = _prop_escape(v)
        elif kind == "enum":
            if v not in spec[1]:
                return None, f"invalid value for {k}"
            out[k] = spec[1][v]
        elif kind == "int":
            try:
                n = int(v)
            except ValueError:
                return None, f"{k} must be a number"
            if not spec[1] <= n <= spec[2]:
                return None, f"{k} must be between {spec[1]} and {spec[2]}"
            out[k] = str(n)
        else:
            out[k] = _bool(v)
    return out, None

def list_dirs(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        raise ValueError("not a directory")
    names = sorted((n for n in os.listdir(path)
                    if not n.startswith(".") and os.path.isdir(os.path.join(path, n))), key=str.lower)
    return {"path": path, "parent": os.path.dirname(path) if path != os.path.dirname(path) else None,
            "dirs": names, "home": os.path.expanduser("~")}

# ── Server JAR providers ───────────────────────────────────────────────────────

JAR_TYPES = {"vanilla": "Vanilla", "paper": "Paper", "purpur": "Purpur", "fabric": "Fabric"}

def _get_json(url):
    return json.loads(fetch_url(url))

def jar_versions(jtype):
    """Release versions for a provider, newest first."""
    if jtype == "vanilla":
        m = _get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        return [v["id"] for v in m["versions"] if v["type"] == "release"]
    if jtype == "paper":
        d = _get_json("https://fill.papermc.io/v3/projects/paper")
        return [v for grp in d["versions"].values() for v in grp if "-" not in v]
    if jtype == "purpur":
        return list(reversed(_get_json("https://api.purpurmc.org/v2/purpur")["versions"]))
    if jtype == "fabric":
        return [v["version"] for v in _get_json("https://meta.fabricmc.net/v2/versions/game") if v["stable"]]
    raise ValueError("unknown server type")

def jar_url(jtype, ver):
    if jtype == "vanilla":
        m = _get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        entry = next((v for v in m["versions"] if v["id"] == ver), None)
        if not entry:
            raise ValueError("unknown version")
        server = _get_json(entry["url"])["downloads"].get("server")
        if not server:
            raise ValueError("no server jar published for this version")
        return server["url"]
    if jtype == "paper":
        d = _get_json(f"https://fill.papermc.io/v3/projects/paper/versions/{ver}/builds/latest")
        return d["downloads"]["server:default"]["url"]
    if jtype == "purpur":
        return f"https://api.purpurmc.org/v2/purpur/{ver}/latest/download"
    if jtype == "fabric":
        loader    = next(v["version"] for v in _get_json("https://meta.fabricmc.net/v2/versions/loader") if v["stable"])
        installer = next(v["version"] for v in _get_json("https://meta.fabricmc.net/v2/versions/installer") if v["stable"])
        return f"https://meta.fabricmc.net/v2/versions/loader/{ver}/{loader}/{installer}/server/jar"
    raise ValueError("unknown server type")

def download_jar(jtype, ver, dest_path, job=None):
    if not re.match(r"^[A-Za-z0-9._-]+$", ver or ""):
        raise ValueError("invalid version")
    tmp = dest_path + ".part"
    try:
        with open(tmp, "wb") as f:
            _stream(jar_url(jtype, ver), f.write, job, 1 << 29)
        os.replace(tmp, dest_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

class ManagedServer:
    def __init__(self, sid, scfg):
        self.id      = sid
        self.cfg     = scfg
        self.process = None
        self.logs    = deque(maxlen=300)
        self.log_seq = 0
        self._lock   = threading.Lock()
        self._banned = set()

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        with self._lock:
            if self.is_running():
                return False, "Already running"
            directory = os.path.expanduser(self.cfg.get("directory", ""))
            jar       = self.cfg.get("jar", "server.jar")
            # min/max RAM - fall back to legacy memory_mb if new keys absent
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
                _harden_server(directory)
            except OSError as e:
                return False, f"failed to harden server config: {e}"
            try:
                self.process = subprocess.Popen(
                    cmd, cwd=directory,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except FileNotFoundError:
                return False, "java not found - is a JRE installed?"
            except Exception as e:
                return False, str(e)
            self._append(f"[CreeperCrest] Started PID {self.process.pid}  |  {' '.join(cmd)}")
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
            self._append("[CreeperCrest] Server stopped")
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
        self.log_seq += 1

    def _tail(self):
        try:
            for line in self.process.stdout:
                self._append(line.rstrip())
                _check_not_whitelisted(self, line)
        except Exception:
            pass

    def status(self):
        legacy        = self.cfg.get("memory_mb", 1024)
        cpu_pct       = None
        ram_mb        = None
        heap_used_mb  = None
        heap_total_mb = None
        ygc           = None
        fgc           = None
        if self.is_running():
            pid = self.process.pid
            cpu_pct = _pid_cpu_cache.get(pid)
            try:
                with open(f'/proc/{pid}/status') as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            ram_mb = int(line.split()[1]) // 1024
                            break
            except Exception:
                pass
            try:
                rj = subprocess.run(
                    ["jstat", "-gc", str(pid)],
                    capture_output=True, text=True, timeout=3,
                )
                if rj.returncode == 0:
                    lines = rj.stdout.strip().splitlines()
                    if len(lines) >= 2:
                        vals = lines[-1].split()
                        if len(vals) >= 15:
                            s0c, s1c = float(vals[0]), float(vals[1])
                            s0u, s1u = float(vals[2]), float(vals[3])
                            ec,  eu  = float(vals[4]), float(vals[5])
                            oc,  ou  = float(vals[6]), float(vals[7])
                            heap_used_mb  = round((s0u + s1u + eu + ou) / 1024, 1)
                            heap_total_mb = round((s0c + s1c + ec + oc) / 1024, 1)
                            ygc = int(float(vals[12]))
                            fgc = int(float(vals[14]))
            except Exception:
                pass
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
            "autostart":     self.cfg.get("autostart", False),
            "resource_pack":      self.cfg.get("resource_pack", ""),
            "resource_pack_sha1": self.cfg.get("resource_pack_sha1", ""),
            "resource_pack_required": bool(self.cfg.get("resource_pack_required", False)),
            "resource_pack_prompt":   self.cfg.get("resource_pack_prompt", ""),
            "cpu_pct":       cpu_pct,
            "ram_mb":        ram_mb,
            "heap_used_mb":  heap_used_mb,
            "heap_total_mb": heap_total_mb,
            "ygc":           ygc,
            "fgc":           fgc,
        }

# ── Global state ───────────────────────────────────────────────────────────────

cfg     = load_cfg()
servers = {sid: ManagedServer(sid, sc) for sid, sc in cfg.get("servers", {}).items()}

# ── Authentication (password + TOTP 2FA) ───────────────────────────────────────

USERS_FILE    = os.path.join(BASE_DIR, "users.json")
EDIT_MAX      = 512 * 1024                    # largest file the built-in text editor opens or saves
MAX_UPLOAD    = 2 * 1024 ** 3                 # largest accepted request body (uploads are held in memory)
_OPEN_TOKEN   = secrets.token_urlsafe(24)     # CSRF token for LAN / open mode (no session to bind it to)
TLS_ON        = bool(cfg.get("tls_cert") and cfg.get("tls_key"))
AUTH_DISABLED = bool(cfg.get("auth_disabled", False))
SESSION_IDLE  = 8 * 3600
SESSION_MAX   = 24 * 3600
USER_RE       = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")

def load_users():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def save_users(users):
    tmp = USERS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, USERS_FILE)

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1

def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32, maxmem=128 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()

def verify_password(pw, stored):
    try:
        f = stored.split("$")
        n, r, pp = (2 ** 14, 8, 1) if len(f) == 3 else (int(f[1]), int(f[2]), int(f[3]))   # 3-part = original format
        salt, want = f[-2], f[-1]
        if n > 2 ** 17:
            return False
        h = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=n, r=r, p=pp, dklen=32, maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(h, base64.b64decode(want))
    except (ValueError, TypeError, IndexError):
        return False

_DUMMY_HASH = hash_password("creepercrest-dummy")   # equalises timing for unknown usernames
_DUMMY_TOTP = "JBSWY3DPEHPK3PXP"

def new_totp_secret():
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")

def totp_at(secret, counter):
    key  = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h    = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    o    = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7fffffff) % 10 ** 6:06d}"

_totp_last = {}   # username -> last accepted time step (blocks code replay)

def verify_totp(secret, code, user=None):
    """Return the matching time step (truthy) or 0. Does not consume it - call mark_totp_used after a full login."""
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return 0
    now, found = int(time.time() // 30), 0
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(totp_at(secret, step), code) and step > _totp_last.get(user, 0):
            found = step
    return found

def mark_totp_used(user, step):
    _totp_last[user] = step

def totp_uri(user, secret):
    return (f"otpauth://totp/CreeperCrest:{quote(user)}?secret={secret}"
            "&issuer=CreeperCrest&algorithm=SHA1&digits=6&period=30")

def qr_svg_img(uri):
    """Inline QR code if the optional `qrcode` module is installed, else ''."""
    try:
        import qrcode, qrcode.image.svg
        buf = io.BytesIO()
        qrcode.make(uri, image_factory=qrcode.image.svg.SvgFillImage).save(buf)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return (f'<img src="data:image/svg+xml;base64,{b64}" width="200" height="200" alt="TOTP QR code" '
                'style="display:block;background:#fff;padding:8px;border-radius:6px"/>')
    except Exception:
        return ""

def qr_ascii(uri):
    """Terminal QR via `qrencode` if installed, else None."""
    try:
        r = subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "1", uri], capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None

# sessions + login throttling (in memory; a restart logs everyone out)
_sessions  = {}
_sess_lock = threading.Lock()
_fails     = {}
_FAIL_WINDOW, _FAIL_IP, _FAIL_USER = 900, 5, 10

def _prune_fails(key):
    cutoff = time.time() - _FAIL_WINDOW
    _fails[key] = [t for t in _fails.get(key, []) if t > cutoff]
    return _fails[key]

def login_throttled(ip, user):
    return len(_prune_fails("ip:" + ip)) >= _FAIL_IP or len(_prune_fails("user:" + user)) >= _FAIL_USER

def record_login_failure(ip, user):
    for key in ("ip:" + ip, "user:" + user):
        _prune_fails(key).append(time.time())

def create_session(user):
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with _sess_lock:
        for k in [k for k, v in _sessions.items() if now - v["created"] > SESSION_MAX or now - v["last"] > SESSION_IDLE]:
            del _sessions[k]
        _sessions[tok] = {"user": user, "csrf": secrets.token_urlsafe(24), "created": now, "last": now}
    return tok

def get_session(tok):
    now = time.time()
    with _sess_lock:
        s = _sessions.get(tok)
        if not s:
            return None
        if now - s["created"] > SESSION_MAX or now - s["last"] > SESSION_IDLE:
            del _sessions[tok]
            return None
        s["last"] = now
        return s

def drop_session(tok):
    with _sess_lock:
        _sessions.pop(tok, None)

_AUTH_CSS = """*{box-sizing:border-box;margin:0}body{background:#0d1117;color:#c9d1d9;font-family:system-ui,sans-serif;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:1.8rem;width:min(400px,94vw);box-shadow:0 12px 36px rgba(0,0,0,.5)}
h1{font-size:1.1rem;color:#f0f6fc;margin-bottom:1.2rem}label{display:block;font-size:.78rem;color:#7d8590;margin:.8rem 0 .3rem}
input{width:100%;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;padding:.55rem .7rem;font-size:.9rem}
input:focus{outline:none;border-color:#58a6ff}.totp{letter-spacing:.25em;text-align:center;font-size:1.1rem}
button{width:100%;margin-top:1.2rem;background:#238636;color:#fff;border:0;border-radius:8px;padding:.6rem;font-weight:600;cursor:pointer}
.err{background:#3d1616;color:#f85149;border:1px solid #8b1a1a;border-radius:8px;padding:.5rem .7rem;font-size:.82rem;margin-bottom:.4rem}
p{font-size:.85rem;line-height:1.5;color:#8b949e}code{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:.15rem .4rem;color:#c9d1d9;word-break:break-all}
a{color:#58a6ff}"""

def _acct_html(user):
    if user.startswith("("):
        return '<span style="color:#e3b341">LAN mode &middot; no login</span>'
    return (f'{html_escape(user)} &middot; <a href="/2fa-setup" style="color:#58a6ff">2FA</a> &middot; '
            '<a href="#" onclick="logout();return false" style="color:#58a6ff">Logout</a>')

def render_login(error=""):
    err = f'<div class="err">{html_escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%233eae30'/%3E%3C/svg%3E">
<title>Creeper Crest - Sign in</title><style>{_AUTH_CSS}</style></head><body><form class="card" method="post" action="/login" autocomplete="off">
<h1>Creeper Crest</h1>{err}
<label for="u">Username</label><input id="u" name="username" autocomplete="username" autofocus required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="t">Authenticator code</label><input id="t" name="totp" class="totp" inputmode="numeric" maxlength="7" placeholder="000000" autocomplete="one-time-code" required>
<button type="submit">Sign in</button></form></body></html>"""

def render_setup_required():
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Creeper Crest - Setup required</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%233eae30'/%3E%3C/svg%3E">
<style>{_AUTH_CSS}</style></head><body><div class="card"><h1>Setup required</h1>
<p>No users exist yet, so the panel is locked. On the server, create a login (it prints a generated password and a 2FA secret):</p>
<p style="margin-top:.8rem"><code>python3 creepercrest.py adduser yourname</code></p>
<p style="margin-top:.8rem">Then reload this page.</p></div></body></html>"""

def render_2fa_setup(user):
    rec = load_users().get(user)
    if not rec:
        return render_setup_required()
    uri = totp_uri(user, rec["totp"])
    qr  = qr_svg_img(uri) or "<p>Install the optional <code>qrcode</code> Python module to show a QR code here, or enter the key manually.</p>"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Creeper Crest - 2FA</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%233eae30'/%3E%3C/svg%3E">
<style>{_AUTH_CSS}</style></head><body><div class="card"><h1>Two-factor authentication</h1>
<p style="color:#3fb950;font-weight:600">2FA is active for {html_escape(user)}</p>
<p style="margin:.8rem 0">Add this account to Google Authenticator, Authy, or any TOTP app.</p>{qr}
<label>Setup key (manual entry)</label><code style="display:block;padding:.5rem">{html_escape(rec['totp'])}</code>
<p style="margin-top:1rem"><a href="/">Back to panel</a></p></div></body></html>"""

def cli(argv):
    """User management: adduser / passwd / reset-2fa / deluser / users."""
    import getpass
    cmd = _CLI_ALIASES.get(argv[0], argv[0])
    if cmd == "lan-mode":
        want = argv[1].lower() if len(argv) > 1 else "status"
        if want in ("on", "off"):
            cfg["lan_mode"] = (want == "on")
            save_cfg(cfg)
            print(f"LAN mode {'ENABLED' if cfg['lan_mode'] else 'disabled'}. Restart CreeperCrest to apply.")
            if cfg["lan_mode"]:
                print("Clients on private networks (192.168.x, 10.x, 172.16-31.x, localhost) need no login; "
                      "any other address still must sign in. Do NOT port-forward this panel to the internet.")
        else:
            print("LAN mode is", "on" if cfg.get("lan_mode") else "off")
        return 0
    users = load_users()
    if cmd == "users":
        for name in sorted(users):
            print(name)
        return 0
    if len(argv) < 2:
        print(f"usage: creepercrest.py {argv[0]} <username> [--prompt | --yes]")
        return 2
    name = argv[1].strip().lower()
    prompt = "--prompt" in argv

    def pick_password():
        if not prompt:
            return secrets.token_urlsafe(15), True
        a, b = getpass.getpass("Password (12+ chars): "), getpass.getpass("Repeat: ")
        if a != b or len(a) < 12:
            raise SystemExit("Passwords differ or are shorter than 12 characters.")
        return a, False

    def show(pw, generated, secret):
        uri = totp_uri(name, secret)
        print(f"\n  Username : {name}")
        if generated:
            print(f"  Password : {pw}   (shown once - store it in your password manager)")
        print(f"  2FA key  : {secret}")
        print(f"  2FA URI  : {uri}")
        art = qr_ascii(uri)
        if art:
            print("\n" + art)
        else:
            print("\n  (no QR shown - enter the 2FA key manually in your authenticator app,")
            print("   or install `qrencode` to see a QR code here)")
        print()

    if cmd == "adduser":
        if not USER_RE.match(name):
            print("Username: 2-32 chars, lowercase letters, digits, . _ -")
            return 2
        if name in users:
            print(f"User '{name}' already exists (use passwd / reset-2fa).")
            return 1
        pw, gen = pick_password()
        secret = new_totp_secret()
        users[name] = {"pw": hash_password(pw), "totp": secret, "created": datetime.now().isoformat(timespec="seconds")}
        save_users(users)
        show(pw, gen, secret)
        return 0
    if name not in users:
        print(f"No such user: {name}")
        return 1
    if cmd == "passwd":
        pw, gen = pick_password()
        users[name]["pw"] = hash_password(pw)
        save_users(users)
        print(f"\n  New password for {name}: {pw}\n" if gen else f"\n  Password updated for {name}.\n")
        return 0
    if cmd == "reset-2fa":
        users[name]["totp"] = new_totp_secret()
        save_users(users)
        show("", False, users[name]["totp"])
        return 0
    if cmd == "deluser":
        if "--yes" not in argv and "-y" not in argv:
            if input(f"Remove user '{name}'? They will be signed out and cannot log in again. [y/N]: ").strip().lower() != "y":
                print("Cancelled.")
                return 1
        del users[name]
        save_users(users)
        print(f"Removed {name}.")
        return 0
    return 2

_CLI_ALIASES = {"--remove-user": "deluser", "remove-user": "deluser", "removeuser": "deluser", "rmuser": "deluser"}
CLI_COMMANDS = {"adduser", "passwd", "reset-2fa", "deluser", "users", "lan-mode"} | set(_CLI_ALIASES)


# ── Backups ────────────────────────────────────────────────────────────────────

SKIP_DIRS = {"logs", "crash-reports", "debug"}

def do_backup(sid, job=None):
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
        todo = []
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                fp = os.path.join(root, name)
                try:
                    todo.append((fp, os.path.getsize(fp)))
                except OSError:
                    pass
        if job:
            job.unit, job.total, job.done = "bytes", sum(n for _, n in todo), 0
        with zipfile.ZipFile(fpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for fp, size in todo:
                zf.write(fp, os.path.relpath(fp, src))
                if job:
                    job.done += size
        size_mb = round(os.path.getsize(fpath) / 1024 / 1024, 2)
        srv._append(f"[CreeperCrest] Backup saved → {fpath}  ({size_mb} MB)")
        _prune_backups(sid, bak_dir)
        return True, {"file": fname, "path": fpath, "size_mb": size_mb}
    except Exception as e:
        return False, str(e)

def _prune_backups(sid, bak_dir):
    limit = int(cfg.get("max_backups", 0) or 0)
    if limit <= 0:
        return
    pat = re.compile(rf"^{re.escape(sid)}-\d{{8}}-\d{{6}}\.zip$")
    mine = sorted(n for n in os.listdir(bak_dir) if pat.match(n))
    for name in mine[:-limit]:
        try:
            os.remove(os.path.join(bak_dir, name))
        except OSError:
            pass

_BACKUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.zip$")
SID_RE     = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

def backup_path(fname):
    """Absolute path of a backup zip, or None if the name is not a plain file name inside the backup dir."""
    if not isinstance(fname, str) or not _BACKUP_RE.match(fname) or ".." in fname:
        return None
    bak_dir = os.path.realpath(os.path.expanduser(cfg.get("backup_dir", "~/mc-backups")))
    path = os.path.realpath(os.path.join(bak_dir, fname))
    return path if os.path.dirname(path) == bak_dir else None

def list_backups():
    bak_dir = os.path.expanduser(cfg.get("backup_dir", "~/mc-backups"))
    if not os.path.isdir(bak_dir):
        return []
    out = []
    for name in sorted(os.listdir(bak_dir), reverse=True):
        if not _BACKUP_RE.match(name):
            continue
        fp = os.path.join(bak_dir, name)
        out.append({
            "name":    name,
            "size_mb": round(os.path.getsize(fp) / 1024 / 1024, 2),
            "created": datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M"),
        })
    return out

def restore_backup(fname, sid, job=None):
    if sid not in servers:
        return False, "Server not found"
    if servers[sid].is_running():
        return False, "Stop the server before restoring"
    fpath = backup_path(fname)
    if not fpath or not os.path.isfile(fpath):
        return False, "Backup file not found"
    dest = os.path.expanduser(servers[sid].cfg.get("directory", ""))
    if not dest or not os.path.isdir(dest):
        return False, "Server directory not found"
    try:
        with zipfile.ZipFile(fpath, "r") as zf:
            _extract_zip(zf, dest, job)
        servers[sid]._append(f"[CreeperCrest] Restored from backup: {fname}")
        return True, f"Restored {fname}"
    except Exception as e:
        return False, str(e)

# ── Scheduled auto-backup ──────────────────────────────────────────────────────

_WEEKDAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

def _autobackup_scheduler():
    """Runs in a background thread; fires weekly backups at the configured day/time."""
    last_fired = None
    while True:
        time.sleep(60)
        sched = cfg.get("auto_backup", {})
        if not sched.get("enabled", False):
            continue
        now      = datetime.now()
        day_name = _WEEKDAYS[now.weekday()]
        hh, mm   = sched.get("hour", 3), sched.get("minute", 0)
        if day_name != sched.get("day", "sunday").lower():
            continue
        if now.hour != hh or now.minute != mm:
            continue
        # Fire once per minute window
        key = (now.year, now.isocalendar()[1], day_name)
        if last_fired == key:
            continue
        last_fired = key
        for sid in list(servers):
            ok, result = do_backup(sid)
            if ok:
                print(f"[auto-backup] {sid} → {result['file']} ({result['size_mb']} MB)")
            else:
                print(f"[auto-backup] {sid} failed: {result}")

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

def _parse_multipart_full(raw, boundary):
    """Return (fields_dict, files_list). Handles both form fields and file parts."""
    sep    = b"--" + boundary
    fields = {}
    files  = []
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
        name, fname = None, None
        for token in cd.split(";"):
            token = token.strip()
            if token.lower().startswith("name="):
                name = token[5:].strip().strip('"')
            elif token.lower().startswith("filename="):
                fname = token[9:].strip().strip('"')
        if name and fname:
            files.append((fname, body))
        elif name:
            fields[name] = body.decode(errors="replace")
    return fields, files

# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CreeperCrest</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%233eae30'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{scrollbar-color:#30363d transparent}
body{background:
    radial-gradient(1100px 520px at 12% -8%, rgba(31,111,235,.10), transparent 60%),
    radial-gradient(900px 480px at 100% 0%, rgba(63,185,80,.06), transparent 55%),
    #0a0d12;
  color:#c9d1d9;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;letter-spacing:.1px}
a{color:#58a6ff;text-decoration:none}

header{background:rgba(22,27,34,.97);position:sticky;top:0;z-index:50;
  border-bottom:1px solid #21262d;padding:.85rem 2rem;display:flex;align-items:center;gap:1.1rem;
  box-shadow:0 1px 0 rgba(0,0,0,.4)}
header h1{font-size:1.22rem;color:#f0f6fc;font-weight:700;display:flex;align-items:center;gap:.55rem}
header h1 svg{filter:drop-shadow(0 0 8px rgba(62,174,48,.45))}
.refresh-ctrl{display:flex;align-items:center;gap:.4rem;font-size:.78rem;color:#7d8590;margin-left:auto}
.refresh-ctrl input{width:52px;background:#0a0d12;border:1px solid #30363d;color:#c9d1d9;
  padding:.25rem .4rem;border-radius:6px;font-size:.78rem;text-align:center;outline:none;transition:border-color .15s}
.refresh-ctrl input:focus{border-color:#58a6ff;box-shadow:0 0 0 3px rgba(88,166,255,.15)}
#upd{color:#7d8590;font-size:.8rem;padding:.25rem .6rem;background:#161b22;border:1px solid #21262d;border-radius:20px}

main{padding:1.8rem 2rem 3rem;max-width:1300px;margin:0 auto}
section+section{margin-top:2.2rem}
.sec-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
.sec-hdr h2{font-size:1.02rem;font-weight:700;color:#f0f6fc;display:flex;align-items:center;gap:.6rem}
.sec-hdr h2::before{content:'';display:block;width:4px;height:16px;border-radius:3px;
  background:linear-gradient(180deg,#58a6ff,#3fb950)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:1.1rem}

.card{background:linear-gradient(180deg,#161b22 0%,#141920 100%);border:1px solid #21262d;
  border-left:3px solid #30363d;border-radius:12px;padding:1.25rem;
  box-shadow:0 2px 10px rgba(0,0,0,.35);transition:border-color .2s,box-shadow .2s,transform .2s}
.card:has(.badge.on){border-left-color:#238636}
.card:has(.badge.off){border-left-color:#8b1a1a}
.card:hover{box-shadow:0 6px 22px rgba(0,0,0,.45);transform:translateY(-1px)}
.card-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:.9rem}
.card-name{font-size:1.02rem;font-weight:700;color:#f0f6fc;letter-spacing:.1px}
.badge{font-size:.7rem;font-weight:700;padding:.24rem .6rem .24rem .5rem;border-radius:20px;
  display:inline-flex;align-items:center;gap:.4rem;text-transform:uppercase;letter-spacing:.04em}
.badge::before{content:'';width:6px;height:6px;border-radius:50%}
.badge.on{background:rgba(35,134,54,.15);color:#3fb950;border:1px solid rgba(35,134,54,.4)}
.badge.on::before{background:#3fb950;box-shadow:0 0 6px #3fb950}
.badge.off{background:rgba(139,26,26,.15);color:#f85149;border:1px solid rgba(139,26,26,.4)}
.badge.off::before{background:#f85149}

.info{font-size:.78rem;color:#7d8590;margin-bottom:.85rem;line-height:1.7}
.info b{color:#c9d1d9}

.ram-row{display:flex;align-items:center;gap:.5rem;margin-bottom:.85rem;flex-wrap:wrap}
.ram-row label{font-size:.78rem;color:#7d8590;white-space:nowrap}
.ram-row input{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;
  padding:.3rem .55rem;border-radius:5px;font-size:.83rem;width:75px;outline:none}
.ram-row input:focus{border-color:#58a6ff}
.ram-sep{color:#30363d;font-size:.85rem}

.btn-row{display:flex;flex-wrap:wrap;gap:.45rem;margin-bottom:.85rem}
.btn-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.45rem;margin:0 auto .6rem;width:min(100%,380px)}
.btn-grid .btn{padding:.6rem .5rem;font-size:.85rem;font-weight:600;width:100%}
.btn-grid .btn-full{grid-column:1/-1}
.btn{border:none;border-radius:8px;padding:.42rem .85rem;font-size:.8rem;font-weight:600;
  cursor:pointer;transition:filter .12s,transform .1s,box-shadow .15s;letter-spacing:.1px}
.btn:hover{filter:brightness(1.18)}
.btn:active{transform:scale(.96)}
.btn:disabled{opacity:.35;cursor:not-allowed;filter:none;transform:none;box-shadow:none}
.bg-green{background:linear-gradient(180deg,#2ea043,#238636);color:#fff;box-shadow:0 2px 8px rgba(35,134,54,.35)}
.bg-red{background:#8b1a1a;color:#f85149;border:1px solid #8b1a1a}
.bg-blue{background:linear-gradient(180deg,#2361c9,#1f3a6e);color:#eaf3ff;border:1px solid #1f6feb;
  box-shadow:0 2px 8px rgba(31,111,235,.3)}
.bg-teal{background:#0f3d3d;color:#56d4c8;border:1px solid #0d6e6e}
.bg-yellow{background:#5a3e13;color:#e3b341;border:1px solid #9e6a03}
.bg-gray{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
.bg-danger{background:transparent;color:#f85149;border:1px solid #6e2222}

.card{display:flex;flex-direction:row;gap:1.2rem;align-items:stretch;grid-column:1 / -1}
.card-main{min-width:400px;flex-shrink:0;display:flex;flex-direction:column}
.card-con{flex:1;min-width:0;display:flex;flex-direction:column}
.console{background:#0a0c10;border:1px solid #21262d;border-radius:8px;
  padding:.7rem .8rem;overflow-y:auto;font-family:'Consolas',monospace;
  font-size:.76rem;color:#8b949e;line-height:1.55;height:420px;flex-shrink:0;
  box-shadow:inset 0 2px 8px rgba(0,0,0,.35)}
.console p{white-space:pre-wrap;word-break:break-all}
.cmd-row{display:flex;gap:.4rem;margin-top:.6rem;flex-shrink:0}
.cmd-row input[type=text]{flex:1;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;
  padding:.3rem .55rem;border-radius:5px;font-size:.83rem;font-family:monospace;outline:none}
.cmd-row input[type=text]:focus{border-color:#58a6ff}
.wl-input{flex:1;min-width:160px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:.4rem .7rem;border-radius:5px;font-size:.85rem;outline:none}
.wl-input:focus{border-color:#58a6ff}
#wl-overlay{z-index:210}
.fb-split{display:flex;flex:1;min-height:0}
.fb-split .fb-body{flex:1;min-width:0}
.fb-editor{display:none;flex:1.2;min-width:0;flex-direction:column;border-left:1px solid #30363d;background:#0d1117}
.fb-modal.with-editor{width:min(1800px,98vw)}
.fb-ed-cm{flex:1;min-height:0;overflow:hidden}
.fb-ed-cm .cm-editor{height:100%}
.fb-modal.with-editor .fb-editor{display:flex}
.fb-modal.with-editor .fb-body{flex:.8}
.fb-ed-bar{display:flex;align-items:center;gap:.5rem;padding:.6rem .8rem;border-bottom:1px solid #21262d}
.fb-ed-name{flex:1;min-width:0;font-size:.82rem;color:#c9d1d9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb-ed-name.dirty::after{content:" (unsaved)";color:#e3b341}
#fb-ed-text{flex:1;width:100%;resize:none;border:0;outline:none;background:#0d1117;color:#c9d1d9;
  font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:.8rem;tab-size:4;white-space:pre;overflow:auto}

/* ── Backups ── */
.backup-row{display:flex;align-items:center;gap:1rem;padding:.6rem .9rem;
  background:#161b22;border:1px solid #30363d;border-radius:6px;margin-bottom:.4rem;font-size:.82rem}
.backup-row input[type=checkbox]{accent-color:#58a6ff;width:14px;height:14px;flex-shrink:0}
.backup-row .bname{flex:1;font-family:monospace;color:#c9d1d9;font-size:.8rem;word-break:break-all}
.backup-row .bmeta{color:#7d8590;white-space:nowrap}

.empty{color:#7d8590;font-size:.88rem;padding:2.2rem;text-align:center;
  border:1px dashed #30363d;border-radius:12px;background:rgba(255,255,255,.015)}

/* ── Add server modal ── */
.overlay{position:fixed;inset:0;background:rgba(1,4,9,.78);display:none;
  align-items:center;justify-content:center;z-index:100}
.overlay.open{display:flex}
.usage-rows{display:contents}
.modal{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:1.5rem;width:min(520px,94vw);
  box-shadow:0 12px 36px rgba(0,0,0,.5)}
.wz-modal{width:min(640px,94vw);max-height:92vh;overflow-y:auto}
.wz-steps{display:flex;gap:.35rem;margin-bottom:1rem}
.wz-steps span{flex:1;height:4px;border-radius:2px;background:#21262d}
.wz-steps span.done{background:#1f6feb}
.wz-help{font-size:.82rem;color:#7d8590;margin:-.6rem 0 1rem;line-height:1.45}
.wz-pane{display:none}
.wz-pane.show{display:block}
.wz-eula{display:flex;align-items:center;gap:.5rem;font-size:.8rem;color:#c9d1d9;margin:.4rem 0 .8rem}
.wz-eula a{color:#58a6ff}
.wz-summary{font-size:.78rem;color:#7d8590;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:.6rem .8rem;line-height:1.6;word-break:break-all}
.wz-summary b{color:#c9d1d9;font-weight:600}
.frow.chk label{display:flex;align-items:center;gap:.5rem;color:#c9d1d9;cursor:pointer;margin:0}
.frow.chk input{width:auto}
.dp-list{max-height:320px;overflow-y:auto;border:1px solid #21262d;border-radius:8px;background:#0d1117}
.dp-item{padding:.45rem .8rem;font-size:.83rem;color:#79c0ff;cursor:pointer;border-bottom:1px solid #161b22}
.dp-item:hover{background:#1c2128}
.dp-empty{padding:1rem;text-align:center;color:#7d8590;font-size:.83rem}
.modal h3{margin-bottom:1.1rem;color:#f0f6fc;font-size:1rem}
.frow{margin-bottom:.7rem}
.frow label{display:block;font-size:.78rem;color:#7d8590;margin-bottom:.3rem}
.frow input,.frow select{width:100%;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;
  padding:.4rem .7rem;border-radius:5px;font-size:.85rem;outline:none}
.frow input:focus,.frow select:focus{border-color:#58a6ff}
.frow select option{background:#161b22}
.frow-2{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
.modal-btns{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1.1rem}

/* ── File browser ── */
#fb-overlay{z-index:200}
.fb-modal{background:#161b22;border:1px solid #30363d;border-radius:10px;
  width:min(1200px,96vw);height:90vh;display:flex;flex-direction:column}
.fb-header{padding:1rem 1.2rem;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:.7rem}
.fb-header h3{color:#f0f6fc;font-size:1rem;margin-right:auto}
.breadcrumb{display:flex;align-items:center;gap:.3rem;flex-wrap:wrap;font-size:.8rem;color:#7d8590;flex:1}
.breadcrumb span{cursor:pointer;color:#58a6ff}
.breadcrumb span:hover{text-decoration:underline}
.breadcrumb .sep{color:#30363d}
.fb-toolbar{padding:.7rem 1.2rem;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.fb-toolbar label{font-size:.78rem;color:#7d8590;cursor:pointer;display:flex;align-items:center;gap:.35rem;margin-right:.5rem}
.fb-body{flex:1;overflow-y:auto;padding:0}
.fb-foot{padding:.6rem 1.2rem;border-top:1px solid #21262d;font-size:.75rem;color:#7d8590}
.fb-prog{padding:.5rem 1.2rem;border-bottom:1px solid #21262d;display:none;align-items:center;gap:.7rem;flex-shrink:0;background:#0d1117}
.fb-prog.show{display:flex}
.fb-prog-label{font-size:.78rem;color:#c9d1d9;white-space:nowrap;min-width:130px}
.fb-prog-track{flex:1;height:6px;background:#21262d;border-radius:3px;overflow:hidden}
.fb-prog-fill{height:100%;border-radius:3px;background:#1f6feb;width:0%;transition:width .15s}
.fb-prog-fill.indet{width:35%;animation:fb-indet 1.4s ease-in-out infinite}
@keyframes fb-indet{0%{margin-left:-35%}60%{margin-left:80%}100%{margin-left:105%}}
.fb-prog-val{font-size:.74rem;color:#7d8590;white-space:nowrap;min-width:90px;text-align:right}
.fb-table{width:100%;border-collapse:collapse}
.fb-table th{text-align:left;padding:.5rem 1rem;font-size:.75rem;font-weight:600;
  color:#7d8590;border-bottom:1px solid #21262d;position:sticky;top:0;background:#161b22}
.fb-table td{padding:.45rem 1rem;font-size:.82rem;border-bottom:1px solid #161b22}
.fb-table tr:hover td{background:#1c2128}
.fb-table .fn{color:#c9d1d9;cursor:pointer}
.fb-table .fn:hover{color:#58a6ff;text-decoration:underline}
.fb-table .dir .fn{color:#79c0ff}
.fb-table .fsize,.fb-table .fdate{color:#7d8590;white-space:nowrap}
.fb-table input[type=checkbox]{accent-color:#58a6ff;width:14px;height:14px}
.fb-empty{padding:2rem;text-align:center;color:#7d8590;font-size:.88rem}
.upload-btn{position:relative;overflow:hidden;display:inline-block}
.upload-btn input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;font-size:100px}

/* ── System stats card ── */
.sys-card{background:linear-gradient(180deg,#161b22,#141920);border:1px solid #21262d;border-radius:12px;
  padding:1.1rem 1.5rem;display:grid;grid-template-columns:repeat(3,1fr);gap:1.4rem;
  box-shadow:0 2px 10px rgba(0,0,0,.3)}
.sys-col{display:flex;flex-direction:column;gap:.4rem}
.sys-col-label{font-size:.7rem;font-weight:700;color:#7d8590;text-transform:uppercase;letter-spacing:.08em}
.sys-col-val{font-size:.85rem;color:#f0f6fc;font-weight:600}
.sys-card .usage-bar{height:9px}

/* ── Usage bars ── */
.usage-bars{margin-top:.6rem}
.usage-row{display:flex;align-items:center;gap:.5rem;margin-bottom:.32rem}
.usage-label{font-size:.72rem;color:#7d8590;width:30px;flex-shrink:0}
.usage-bar{flex:1;height:7px;background:#0a0d12;border-radius:20px;overflow:hidden;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.5)}
.usage-fill{height:100%;border-radius:20px;transition:width .6s ease}
.cpu-fill{background:linear-gradient(90deg,#1f6feb,#58a6ff);box-shadow:0 0 8px rgba(88,166,255,.5)}
.ram-fill{background:linear-gradient(90deg,#238636,#3fb950);box-shadow:0 0 8px rgba(63,185,80,.4)}
.ram-fill.hi{background:#e3b341;box-shadow:0 0 8px rgba(227,179,65,.5)}
.ram-fill.crit{background:#f85149;box-shadow:0 0 8px rgba(248,81,73,.5)}
.heap-fill{background:linear-gradient(90deg,#7c3aed,#a371f7);box-shadow:0 0 8px rgba(124,58,237,.4)}
.heap-fill.hi{background:#e3b341;box-shadow:0 0 8px rgba(227,179,65,.5)}
.heap-fill.crit{background:#f85149;box-shadow:0 0 8px rgba(248,81,73,.5)}
.heap-graph{margin-top:.45rem}
.heap-graph svg{display:block;width:100%;height:64px;border-radius:4px;background:#0a0c10}
.heap-legend{display:flex;gap:.8rem;margin-top:.25rem;font-size:.68rem;color:#484f58}
.heap-legend span{display:flex;align-items:center;gap:.3rem}
.hl-young{display:inline-block;width:12px;height:2px;background:#e3b341;border-radius:1px}
.hl-full{display:inline-block;width:12px;height:2px;background:#f85149;border-radius:1px}
.usage-val{font-size:.72rem;color:#7d8590;white-space:nowrap;min-width:90px;text-align:right}

/* ── Nav tabs ── */
nav#main-nav{display:flex;gap:.15rem;margin-left:.6rem;background:#0a0d12;padding:.25rem;
  border-radius:10px;border:1px solid #21262d}
.nav-link{padding:.32rem .95rem;font-size:.82rem;color:#7d8590;border-radius:7px;font-weight:600;
  cursor:pointer;border:1px solid transparent;background:transparent;font-family:inherit;
  white-space:nowrap;transition:color .15s,background .15s}
.nav-link:hover{color:#c9d1d9}
.nav-link.active{color:#eaf3ff;background:linear-gradient(180deg,#2361c9,#1f3a6e);
  box-shadow:0 2px 6px rgba(31,111,235,.4)}
.page{display:none}
.page.active{display:block}

/* ── Schedule info banner ── */
.sched-banner{display:flex;align-items:center;gap:.8rem;padding:.75rem 1rem;
  border-radius:6px;margin-bottom:.5rem;font-size:.83rem;flex-wrap:wrap}
.sched-banner.on{background:#1a3a28;border:1px solid #238636}
.sched-banner.off{background:#21262d;border:1px solid #30363d}
.sched-banner-icon{font-size:1.1rem;flex-shrink:0}
.sched-banner-text{flex:1;color:#c9d1d9;line-height:1.5}
.sched-banner-text b{color:#f0f6fc}
.sched-banner-text span{color:#7d8590;font-size:.78rem}

/* ── Flash ── */
.flash{position:fixed;bottom:1.5rem;right:1.5rem;background:#1f3a6e;color:#79c0ff;
  border:1px solid #1f6feb;border-radius:6px;padding:.6rem 1rem;font-size:.85rem;
  opacity:0;transition:opacity .3s;z-index:300;max-width:340px;pointer-events:none}
.flash.show{opacity:1}
.flash.err{background:#3d1616;color:#f85149;border-color:#8b1a1a}
</style>
</head>
<body>
<header>
  <h1><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160.07292 160.07292" width="22" height="22" aria-hidden="true" style="vertical-align:-.25rem"><rect fill="#3eae30" width="160.07292" height="160.07292"/><g transform="translate(36.947174,215.35194)"><path fill="#000" d="M 14.646575,-93.549115 H 28.820683 V -107.72322 H 57.73586 v 13.985119 H 72.287943 V -136.73289 H 58.019342 v -42.99479 H 86.840026 V -150.907 H 0.0944922 v -28.82068 H 29.009669 v 43.08928 H 14.646575 Z"/></g></svg> Creeper Crest</h1>
  <nav id="main-nav">
    <button class="nav-link active" onclick="showPage('servers',this)">Servers</button>
    <button class="nav-link" onclick="showPage('backups',this)">Backups</button>
  </nav>
  <a href="https://github.com/BeanGreen247/creepercrest" target="_blank" rel="noopener" style="font-size:.73rem;color:#7d8590;white-space:nowrap">by BeanGreen247</a>
  <div class="refresh-ctrl">
    <label for="refresh-secs">Refresh every</label>
    <input id="refresh-secs" type="number" min="5" max="300" value="__REFRESH_INTERVAL__"/>
    <span>s</span>
  </div>
  <span id="upd">connecting…</span>
  <span style="font-size:.73rem;color:#7d8590;white-space:nowrap">__ACCT__</span>
</header>
<main>
  <!-- Servers page -->
  <div id="page-servers" class="page active">
    <section>
      <div class="sec-hdr"><h2>System</h2></div>
      <div class="sys-card">
        <div class="sys-col">
          <span class="sys-col-label">CPU</span>
          <div id="sys-cpu-bar"><div class="usage-bar"><div class="usage-fill cpu-fill" style="width:0%"></div></div></div>
          <span class="sys-col-val" id="sys-cpu-val">-</span>
        </div>
        <div class="sys-col">
          <span class="sys-col-label">RAM</span>
          <div id="sys-ram-bar"><div class="usage-bar"><div class="usage-fill ram-fill" style="width:0%"></div></div></div>
          <span class="sys-col-val" id="sys-ram-val">-</span>
        </div>
        <div class="sys-col">
          <span class="sys-col-label">Disk /</span>
          <div id="sys-disk-bar"><div class="usage-bar"><div class="usage-fill heap-fill" style="width:0%"></div></div></div>
          <span class="sys-col-val" id="sys-disk-val">-</span>
        </div>
      </div>
    </section>
    <section>
      <div class="sec-hdr">
        <h2>Servers</h2>
        <div style="display:flex;gap:.5rem">
          <button class="btn bg-gray" onclick="openImport()">Import Server (ZIP)</button>
          <button class="btn bg-blue" onclick="openWizard()">+ Add Server</button>
        </div>
      </div>
      <div class="grid" id="grid"><div class="empty">No servers configured yet.</div></div>
    </section>
  </div>

  <!-- Backups page -->
  <div id="page-backups" class="page">
    <section>
      <div class="sec-hdr">
        <h2>Backup Schedule</h2>
        <button class="btn bg-gray" onclick="openAutoBackup()" style="font-size:.78rem;padding:.3rem .65rem">&#9998; Edit Schedule</button>
      </div>
      <div id="sched-display"></div>
    </section>
    <section>
      <div class="sec-hdr">
        <h2>Backup Files</h2>
        <div style="display:flex;align-items:center;gap:.7rem;flex-wrap:wrap">
          <small style="color:#7d8590;font-size:.78rem">Saved to <code id="bak-dir">~/mc-backups</code></small>
          <label style="font-size:.78rem;color:#7d8590;cursor:pointer;display:flex;align-items:center;gap:.3rem">
            <input type="checkbox" id="bak-selall" onchange="toggleBakSelAll(this.checked)" style="accent-color:#58a6ff"> All
          </label>
          <button class="btn bg-yellow" id="bak-restore" onclick="openRestore()" disabled style="font-size:.78rem;padding:.3rem .65rem">&#9100; Restore</button>
          <button class="btn bg-blue"   id="bak-create"  onclick="openCreateFromBackup()" disabled style="font-size:.78rem;padding:.3rem .65rem">&#10133; Create Server</button>
          <button class="btn bg-danger" id="bak-del"     onclick="delBackups()"  disabled style="font-size:.78rem;padding:.3rem .65rem">&#128465; Delete Selected</button>
        </div>
      </div>
      <div id="blist"><div class="empty">No backups yet.</div></div>
    </section>
  </div>
</main>
<footer style="text-align:center;padding:1.2rem 2rem;border-top:1px solid #30363d;margin-top:2rem;font-size:.75rem;color:#484f58">
  <a href="https://github.com/BeanGreen247/creepercrest" target="_blank" rel="noopener" style="color:#7d8590">CreeperCrest</a>
  &nbsp;-&nbsp;
  <a href="https://github.com/BeanGreen247" target="_blank" rel="noopener" style="color:#7d8590">BeanGreen247</a>
</footer>

<!-- Add / Edit server overlay -->
<div class="overlay" id="add-overlay">
  <div class="modal">
    <h3 id="modal-title">Add Server</h3>
    <div class="frow-2">
      <div class="frow" id="f-id-wrap"><label>ID (letters, numbers, dash)</label><input id="f-id" placeholder="survival"/></div>
      <div class="frow"><label>Display Name</label><input id="f-name" placeholder="Survival SMP"/></div>
    </div>
    <div class="frow"><label>Server Directory (full path)</label><input id="f-dir" placeholder="/home/crafty/servers/survival"/></div>
    <div class="frow"><label>JAR filename</label><input id="f-jar" value="server.jar"/></div>
    <div class="frow-2">
      <div class="frow"><label>Min RAM (MB)</label><input id="f-min" type="number" value="512" min="256" step="256"/></div>
      <div class="frow"><label>Max RAM (MB)</label><input id="f-max" type="number" value="2048" min="256" step="256"/></div>
    </div>
    <div class="frow"><label>Extra JVM args</label><input id="f-args" value="__JVM_ARGS__"/></div>
    <div class="frow-2">
      <div class="frow"><label>Resource pack URL (optional)</label><input id="f-rp" placeholder="https://example.com/pack.zip"/></div>
      <div class="frow"><label>Resource pack SHA-1 (optional)</label><input id="f-rp-sha1" placeholder="40 hex chars"/></div>
    </div>
    <div class="frow"><label>Resource pack prompt shown to players (optional)</label><input id="f-rp-prompt" maxlength="256"/></div>
    <div class="frow chk"><label><input type="checkbox" id="f-rp-req"/> Require the resource pack (players who decline are kicked)</label></div>
    <div class="frow" id="f-rp-actions" style="display:none">
      <label>Resource pack - Download/Upload hosts it on CreeperCrest and fills in the SHA-1 for you</label>
      <div style="display:flex;gap:.5rem;flex-wrap:wrap">
        <button class="btn bg-blue" type="button" onclick="rpFetch()">&#8681; Download from URL</button>
        <div class="upload-btn">
          <button class="btn bg-teal" type="button">&#8679; Upload ZIP</button>
          <input type="file" id="f-rp-file" accept=".zip" onchange="rpUpload()"/>
        </div>
      </div>
    </div>
    <div class="modal-btns">
      <button class="btn bg-gray" onclick="closeAdd()">Cancel</button>
      <button class="btn bg-green" id="modal-submit-btn" onclick="submitModal()">Add Server</button>
    </div>
  </div>
</div>

<!-- Create server wizard -->
<div class="overlay" id="wz-overlay">
  <div class="modal wz-modal">
    <div class="wz-steps" id="wz-steps"></div>
    <h3 id="wz-title"></h3>
    <p class="wz-help" id="wz-help"></p>

    <div class="wz-pane" data-step="0">
      <div class="frow"><label>Server name *</label><input id="wz-name" placeholder="Survival SMP" oninput="wzName()"/></div>
      <div class="frow"><label>ID (letters, numbers, dash)</label><input id="wz-id" placeholder="survival-smp" oninput="_wzIdTouched=true"/></div>
      <div class="frow"><label>MOTD (shown in the player's server list)</label><input id="wz-p-motd" value="A Minecraft Server" maxlength="200"/></div>
    </div>

    <div class="wz-pane" data-step="1">
      <div class="frow"><label>Server folder *</label>
        <div style="display:flex;gap:.5rem">
          <input id="wz-dir" placeholder="/home/crafty/servers/survival"/>
          <button class="btn bg-teal" type="button" onclick="dpOpen()">&#128193; Browse</button>
        </div>
      </div>
    </div>

    <div class="wz-pane" data-step="2">
      <div class="frow-2">
        <div class="frow"><label>Server software</label>
          <select id="wz-jtype" onchange="wzLoadVersions()">
            <option value="paper">Paper (recommended)</option>
            <option value="vanilla">Vanilla</option>
            <option value="purpur">Purpur</option>
            <option value="fabric">Fabric</option>
            <option value="">I already have a JAR</option>
          </select>
        </div>
        <div class="frow"><label>Minecraft version</label>
          <select id="wz-jver" disabled onchange="wzJavaCheck()"><option value="">-</option></select>
        </div>
      </div>
      <div class="frow" id="wz-upload-wrap" style="display:none">
        <label>Upload your server JAR (optional - skip it if the JAR is already in the folder)</label>
        <div class="upload-btn" style="width:100%">
          <button class="btn bg-teal" type="button" style="width:100%;text-align:left" id="wz-jar-label">&#8679; Choose JAR file...</button>
          <input type="file" id="wz-jar-file" accept=".jar" onchange="wzJarChosen(this)"/>
        </div>
      </div>
      <div class="frow"><label>JAR filename</label><input id="wz-jar" value="server.jar"/></div>
      <p id="wz-java-warn" style="display:none;font-size:.8rem;color:#e3b341;background:#2a2108;border:1px solid #9e6a03;border-radius:8px;padding:.5rem .7rem;margin-top:.4rem"></p>
    </div>

    <div class="wz-pane" data-step="3">
      <div class="frow-2" id="wz-props"></div>
    </div>

    <div class="wz-pane" data-step="4">
      <div class="frow-2">
        <div class="frow"><label>Min RAM (MB)</label><input id="wz-min" type="number" value="512" min="256" step="256"/></div>
        <div class="frow"><label>Max RAM (MB)</label><input id="wz-max" type="number" value="2048" min="256" step="256"/></div>
      </div>
      <div class="frow"><label>Java arguments</label><input id="wz-args"/></div>
      <div class="frow-2">
        <div class="frow"><label>Resource pack URL (optional)</label><input id="wz-rp" placeholder="https://example.com/pack.zip"/></div>
        <div class="frow"><label>Resource pack SHA-1 (optional)</label><input id="wz-rp-sha1"/></div>
      </div>
      <div class="frow"><label>Resource pack prompt shown to players (optional)</label><input id="wz-rp-prompt" maxlength="256"/></div>
      <div class="frow chk"><label><input type="checkbox" id="wz-rp-req"/> Require the resource pack (players who decline are kicked)</label></div>
      <label class="wz-eula"><input type="checkbox" id="wz-eula"/> I accept the
        <a href="https://aka.ms/MinecraftEULA" target="_blank" rel="noopener">Minecraft EULA</a> (required to run a server)</label>
      <div class="wz-summary" id="wz-summary"></div>
    </div>

    <div class="modal-btns">
      <button class="btn bg-gray" onclick="wzClose()">Cancel</button>
      <button class="btn bg-gray" id="wz-back" onclick="wzGo(-1)">Back</button>
      <button class="btn bg-blue" id="wz-next" onclick="wzGo(1)">Next</button>
      <button class="btn bg-green" id="wz-create" onclick="wzCreate()">Create Server</button>
    </div>
  </div>
</div>

<!-- Folder picker -->
<div class="overlay" id="dp-overlay" style="z-index:300">
  <div class="modal" style="width:min(560px,94vw)">
    <h3>Choose a folder</h3>
    <div class="breadcrumb" id="dp-path" style="margin-bottom:.6rem;word-break:break-all"></div>
    <div id="dp-list" class="dp-list"></div>
    <div class="modal-btns" style="justify-content:space-between">
      <button class="btn bg-gray" onclick="dpNew()">&#10133; New folder</button>
      <span style="display:flex;gap:.5rem">
        <button class="btn bg-gray" onclick="dpClose()">Cancel</button>
        <button class="btn bg-green" onclick="dpSelect()">Select this folder</button>
      </span>
    </div>
  </div>
</div>

<!-- Import server from ZIP overlay -->
<div class="overlay" id="import-overlay">
  <div class="modal">
    <h3>Import Server from ZIP</h3>
    <div class="frow">
      <label>Server ZIP file</label>
      <div class="upload-btn" style="width:100%">
        <button class="btn bg-teal" style="width:100%;text-align:left" id="import-file-label">&#8679; Choose ZIP file…</button>
        <input type="file" id="import-zip" accept=".zip" onchange="importFileChosen(this)"/>
      </div>
    </div>
    <div class="frow-2">
      <div class="frow"><label>ID (letters, numbers, dash)</label><input id="fi-id" placeholder="survival"/></div>
      <div class="frow"><label>Display Name</label><input id="fi-name" placeholder="Survival SMP"/></div>
    </div>
    <div class="frow"><label>Extract to Directory (full path)</label><input id="fi-dir" placeholder="/home/crafty/servers/survival"/></div>
    <div class="frow"><label>JAR filename</label><input id="fi-jar" value="server.jar"/></div>
    <div class="frow-2">
      <div class="frow"><label>Min RAM (MB)</label><input id="fi-min" type="number" value="512" min="256" step="256"/></div>
      <div class="frow"><label>Max RAM (MB)</label><input id="fi-max" type="number" value="2048" min="256" step="256"/></div>
    </div>
    <div class="frow"><label>Extra JVM args</label><input id="fi-args" value="__JVM_ARGS__"/></div>
    <div id="import-prog" style="display:none;margin-bottom:.7rem">
      <div style="display:flex;align-items:center;gap:.7rem">
        <span id="import-prog-label" style="font-size:.78rem;color:#c9d1d9;white-space:nowrap;min-width:130px">Uploading…</span>
        <div style="flex:1;height:6px;background:#21262d;border-radius:3px;overflow:hidden">
          <div id="import-prog-fill" style="height:100%;border-radius:3px;background:#1f6feb;width:0%;transition:width .15s"></div>
        </div>
        <span id="import-prog-val" style="font-size:.74rem;color:#7d8590;white-space:nowrap;min-width:90px;text-align:right"></span>
      </div>
    </div>
    <div class="modal-btns">
      <button class="btn bg-gray" onclick="closeImport()">Cancel</button>
      <button class="btn bg-green" onclick="submitImport()" id="import-submit-btn">Import Server</button>
    </div>
  </div>
</div>

<!-- Restore backup overlay -->
<div class="overlay" id="restore-overlay">
  <div class="modal">
    <h3>Restore Backup</h3>
    <p style="font-size:.82rem;color:#7d8590;margin-bottom:1rem">This will <b style="color:#e3b341">overwrite all files</b> in the server directory. The server must be stopped first.</p>
    <div class="frow"><label>Backup file</label><input id="restore-fname" readonly style="cursor:default;color:#7d8590"/></div>
    <div class="frow"><label>Restore to server</label><select id="restore-sid"></select></div>
    <div class="modal-btns">
      <button class="btn bg-gray" onclick="closeRestore()">Cancel</button>
      <button class="btn bg-yellow" onclick="submitRestore()">Restore</button>
    </div>
  </div>
</div>

<!-- Create server from backup overlay -->
<div class="overlay" id="cfb-overlay">
  <div class="modal">
    <h3>Create Server from Backup</h3>
    <div class="frow"><label>Backup file</label><input id="cfb-fname" readonly style="cursor:default;color:#7d8590"/></div>
    <div class="frow-2">
      <div class="frow"><label>ID (letters, numbers, dash)</label><input id="cfb-id" placeholder="survival"/></div>
      <div class="frow"><label>Display Name</label><input id="cfb-name" placeholder="Survival SMP"/></div>
    </div>
    <div class="frow"><label>Server Directory (full path)</label><input id="cfb-dir" placeholder="/home/crafty/servers/survival"/></div>
    <div class="frow"><label>JAR filename</label><input id="cfb-jar" value="server.jar"/></div>
    <div class="frow-2">
      <div class="frow"><label>Min RAM (MB)</label><input id="cfb-min" type="number" value="512" min="256" step="256"/></div>
      <div class="frow"><label>Max RAM (MB)</label><input id="cfb-max" type="number" value="2048" min="256" step="256"/></div>
    </div>
    <div class="frow"><label>Extra JVM args</label><input id="cfb-args" value="__JVM_ARGS__"/></div>
    <div class="modal-btns">
      <button class="btn bg-gray" onclick="closeCreateFromBackup()">Cancel</button>
      <button class="btn bg-green" onclick="submitCreateFromBackup()">Create Server</button>
    </div>
  </div>
</div>

<!-- Busy / progress overlay -->
<div class="overlay" id="busy-overlay" style="z-index:400">
  <div class="modal" style="width:min(360px,90vw);text-align:center;padding:2rem 1.6rem">
    <p id="busy-label" style="color:#c9d1d9;font-size:.87rem">Working...</p>
    <div class="fb-prog-track" style="margin-top:1rem"><div class="fb-prog-fill indet" id="busy-fill"></div></div>
    <p id="busy-val" style="margin-top:.5rem;color:#7d8590;font-size:.76rem;min-height:1em"></p>
  </div>
</div>

<!-- Auto-backup schedule overlay -->
<div class="overlay" id="ab-overlay">
  <div class="modal">
    <h3>Auto-Backup Schedule</h3>
    <p style="font-size:.82rem;color:#7d8590;margin-bottom:1rem">Automatically backs up <b style="color:#c9d1d9">all servers</b> once a week at the chosen time.</p>
    <div class="frow">
      <label>Enable weekly auto-backup</label>
      <select id="ab-enabled">
        <option value="0">Disabled</option>
        <option value="1">Enabled</option>
      </select>
    </div>
    <div class="frow-2">
      <div class="frow"><label>Day of week</label>
        <select id="ab-day">
          <option>monday</option><option>tuesday</option><option>wednesday</option>
          <option>thursday</option><option>friday</option><option>saturday</option>
          <option selected>sunday</option>
        </select>
      </div>
      <div class="frow"><label>Time (24h HH:MM)</label>
        <div style="display:flex;gap:.4rem">
          <input id="ab-hour"   type="number" min="0" max="23" value="3"  style="width:60px"/>
          <span style="color:#7d8590;line-height:2.4">:</span>
          <input id="ab-minute" type="number" min="0" max="59" value="0"  style="width:60px"/>
        </div>
      </div>
    </div>
    <div class="frow"><label>Max backups kept per server (0 = unlimited, otherwise 3-28)</label>
      <input id="ab-max" type="number" min="0" max="28" value="0" style="width:100px" onchange="clampMaxBackups(this)"/>
    </div>
    <div class="modal-btns">
      <button class="btn bg-gray" onclick="closeAutoBackup()">Cancel</button>
      <button class="btn bg-green" onclick="submitAutoBackup()">Save Schedule</button>
    </div>
  </div>
</div>

<!-- File browser overlay -->
<div class="overlay" id="fb-overlay">
  <div class="fb-modal" id="fb-modal">
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
      <button class="btn bg-gray" id="fb-up" onclick="fbUp()" disabled>&#8592; Back</button>
      <button class="btn bg-gray" onclick="newFolder()">&#128193; New Folder</button>
      <button class="btn bg-blue"   onclick="dlSelected()">&#8681; Download</button>
      <button class="btn bg-yellow" onclick="dlZip()">&#128230; Download ZIP</button>
      <button class="btn bg-danger" onclick="delSelected()" id="fb-del">&#128465; Delete</button>
      <span id="fb-sel-info" style="margin-left:auto;font-size:.75rem;color:#7d8590"></span>
    </div>
    <div class="fb-prog" id="fb-prog">
      <span class="fb-prog-label" id="fb-prog-label">Working…</span>
      <div class="fb-prog-track"><div class="fb-prog-fill" id="fb-prog-fill"></div></div>
      <span class="fb-prog-val"  id="fb-prog-val"></span>
    </div>
    <div class="fb-split">
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
    <div class="fb-editor" id="fb-editor">
      <div class="fb-ed-bar">
        <span class="fb-ed-name" id="fb-ed-name"></span>
        <button class="btn bg-green" id="fb-ed-save" onclick="edSave()">&#128190; Save</button>
        <button class="btn bg-blue" onclick="edDownload()">&#8681;</button>
        <button class="btn bg-gray" onclick="edClose()">&#10005;</button>
      </div>
      <div id="fb-ed-cm" class="fb-ed-cm"></div>
      <textarea id="fb-ed-text" spellcheck="false" wrap="off" style="display:none"></textarea>
    </div>
    </div>
    <div class="fb-foot" id="fb-foot"></div>
  </div>
</div>

<!-- Whitelist editor overlay -->
<div class="overlay" id="wl-overlay">
  <div class="fb-modal" style="width:min(640px,96vw);height:auto;max-height:80vh">
    <div class="fb-header">
      <h3 id="wl-title">Whitelist</h3>
      <button class="btn bg-gray" onclick="closeWL()">&#10005;</button>
    </div>
    <div class="fb-toolbar">
      <input type="text" id="wl-name" class="wl-input" placeholder="Player name to add" maxlength="32"
        onkeydown="if(event.key==='Enter')wlAdd()"/>
      <button class="btn bg-green" onclick="wlAdd()">Add</button>
      <span id="wl-count" style="margin-left:auto;font-size:.75rem;color:#7d8590"></span>
    </div>
    <div class="fb-body">
      <table class="fb-table">
        <thead><tr><th>Player</th><th>UUID</th><th style="width:80px"></th></tr></thead>
        <tbody id="wl-rows"></tbody>
      </table>
    </div>
    <div class="fb-foot">The server must be running to add players. Removing works any time and kicks the player if they are online.</div>
  </div>
</div>

<script src="/static/editor.js" defer></script>
<div class="flash" id="flash"></div>

<script>
let fb = { sid: null, path: '', sel: new Set(), entries: [] };
let _servers = [];
const _heapHist = {};   // { sid: [{used, total, ygc, fgc, gcY, gcF}] }
const HEAP_PTS  = 60;   // ~5 min at 5 s refresh

function updateHeapGraph(s) {
  const sid = s.id;
  if (!_heapHist[sid]) _heapHist[sid] = [];
  const hist = _heapHist[sid];

  if (s.heap_used_mb !== null) {
    const prev = hist.length ? hist[hist.length - 1] : null;
    hist.push({
      used: s.heap_used_mb,
      total: s.heap_total_mb,
      ygc:  s.ygc || 0,
      fgc:  s.fgc || 0,
      gcY:  prev !== null && s.ygc > prev.ygc,
      gcF:  prev !== null && s.fgc > prev.fgc,
    });
    if (hist.length > HEAP_PTS) hist.shift();
  }

  const svg = document.getElementById('hg-' + sid);
  if (!svg || hist.length < 2) return;

  const W = 300, H = 64;
  const maxV = Math.max(...hist.map(p => p.total || p.used), 1);
  const xi = (i) => ((HEAP_PTS - hist.length + i) / (HEAP_PTS - 1)) * W;
  const yi = (v) => H - 2 - (v / maxV) * (H - 4);

  const pts = hist.map((p, i) => xi(i).toFixed(1) + ',' + yi(p.used).toFixed(1)).join(' ');
  const x0 = xi(0).toFixed(1), xN = xi(hist.length - 1).toFixed(1);

  let out = '';

  // GC marker lines
  hist.forEach((p, i) => {
    if (!p.gcY && !p.gcF) return;
    const x = xi(i).toFixed(1);
    const col = p.gcF ? '#f85149' : '#e3b341';
    out += '<line x1="' + x + '" y1="0" x2="' + x + '" y2="' + H + '" stroke="' + col + '" stroke-width="1.5" opacity=".6"/>';
  });

  // Filled area under the line
  out += '<polygon points="' + x0 + ',' + H + ' ' + pts + ' ' + xN + ',' + H + '" fill="#7c3aed" opacity=".18"/>';

  // Line itself
  out += '<polyline points="' + pts + '" fill="none" stroke="#7c3aed" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>';

  svg.innerHTML = out;
}

function updateAllHeapGraphs(list) {
  for (const s of list) updateHeapGraph(s);
}

// ── Page navigation ────────────────────────────────────────────────────────────

function showPage(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  localStorage.setItem('cc-page', name);
}

function renderScheduleDisplay(ab) {
  const el = document.getElementById('sched-display');
  if (!el) return;
  if (!ab) { el.innerHTML = ''; return; }
  const en = ab.enabled;
  const timeStr = en
    ? `Every <b>${ab.day.charAt(0).toUpperCase()+ab.day.slice(1)}</b> at <b>${String(ab.hour).padStart(2,'0')}:${String(ab.minute).padStart(2,'0')}</b>`
    : '';
  el.innerHTML = `<div class="sched-banner ${en?'on':'off'}">
  <span class="sched-banner-icon">${en?'&#9989;':'&#8987;'}</span>
  <span class="sched-banner-text">${en
    ? `Auto-backup is <b>active</b> - ${timeStr}, backs up all servers.`
    : 'Auto-backup is <b>disabled</b>. <span>Click "Edit Schedule" to configure a recurring backup.</span>'
  }</span>
</div>`;
}

// ── Utilities ──────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function flash(msg, err) {
  const el = document.getElementById('flash');
  el.textContent = msg;
  el.className = 'flash show' + (err ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 3500);
}

const CSRF = '__CSRF__';

// Associate every <label> with its field so Chrome/Brave DevTools does not log a form issue per label.
let _lblN = 0;
function fixLabels(root) {
  (root || document).querySelectorAll('label:not([for])').forEach(l => {
    if (l.querySelector('input,select,textarea')) return;
    const holder = l.closest('.frow, .ram-row, .sys-col, div') || l.parentElement;
    const f = holder && holder.querySelector('input,select,textarea');
    if (!f) return;
    if (!f.id) f.id = 'auto-f' + (++_lblN);
    l.htmlFor = f.id;
  });
}

async function logout() {
  await api('POST', '/logout', {});
  location.href = '/login';
}

async function api(method, path, body) {
  const opts = { method, headers: {'X-CC-CSRF': CSRF} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = '/login'; return {error: 'login required'}; }
  return r.json();
}

// ── Server cards ───────────────────────────────────────────────────────────────

function usageRowsHTML(s) {
  return `
${(()=>{if(s.cpu_pct===null)return '<div class="usage-row"><span class="usage-label">CPU</span><div class="usage-bar"></div><span class="usage-val" style="color:#484f58">-</span></div>';const pct=Math.min(s.cpu_pct,100);return '<div class="usage-row"><span class="usage-label">CPU</span><div class="usage-bar"><div class="usage-fill cpu-fill" style="width:'+pct+'%"></div></div><span class="usage-val">'+s.cpu_pct.toFixed(1)+'%</span></div>';})()}
      ${(()=>{if(s.ram_mb===null)return '<div class="usage-row"><span class="usage-label">RAM</span><div class="usage-bar"></div><span class="usage-val" style="color:#484f58">-</span></div>';const pct=Math.min(s.memory_max_mb?Math.round(s.ram_mb/s.memory_max_mb*100):0,100);const cls=pct>=90?'crit':pct>=75?'hi':'';return '<div class="usage-row"><span class="usage-label">RAM</span><div class="usage-bar"><div class="usage-fill ram-fill '+cls+'" style="width:'+pct+'%"></div></div><span class="usage-val">'+s.ram_mb+' / '+s.memory_max_mb+' MB</span></div>';})()}
      ${(()=>{if(s.heap_used_mb===null)return '<div class="usage-row"><span class="usage-label">Heap</span><div class="usage-bar"></div><span class="usage-val" style="color:#484f58">-</span></div>';const pct=Math.min(s.heap_total_mb?Math.round(s.heap_used_mb/s.heap_total_mb*100):0,100);const cls=pct>=90?'crit':pct>=75?'hi':'';return '<div class="usage-row"><span class="usage-label">Heap</span><div class="usage-bar"><div class="usage-fill heap-fill '+cls+'" style="width:'+pct+'%"></div></div><span class="usage-val">'+s.heap_used_mb+' / '+s.heap_total_mb+' MB</span></div>';})()}
`;
}

function cardHTML(s) {
  const run = s.running;
  return `
<div class="card" id="card-${s.id}">
  <div class="card-main">
    <div class="card-top">
      <span class="card-name">${esc(s.name)}</span>
      <span class="badge ${run?'on':'off'}">${run?'RUNNING':'STOPPED'}</span>
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
    <div class="btn-grid">
      <button class="btn bg-green"  ${run?'disabled':''} onclick="act('${s.id}','start')">&#9654; Start</button>
      <button class="btn bg-red"    ${run?'':'disabled'} onclick="act('${s.id}','stop')">&#9632; Stop</button>
      <button class="btn bg-blue"   onclick="act('${s.id}','restart')">&#8635; Restart</button>
      <button class="btn bg-teal"   onclick="openFB('${s.id}')">&#128193; Files</button>
      <button class="btn bg-blue"   onclick="openWL('${s.id}')">&#128100; Whitelist</button>
      <button class="btn bg-yellow" onclick="doBackup('${s.id}',this)">&#128190; Backup</button>
      <button class="btn bg-gray"   onclick="openEdit('${s.id}')">&#9998; Edit</button>
      <button class="btn ${s.autostart?'bg-green':'bg-gray'} btn-full" onclick="toggleAutostart('${s.id}',${s.autostart})">&#9654;&#9654; Autostart: ${s.autostart?'ON':'OFF'}</button>
      <button class="btn bg-danger btn-full" onclick="delServer('${s.id}')">Remove</button>
    </div>
    <div class="usage-bars">
      <div class="usage-rows" id="ur-${s.id}">${usageRowsHTML(s)}</div>
      <div class="heap-graph"><svg id="hg-${s.id}" viewBox="0 0 300 64" preserveAspectRatio="none"></svg><div class="heap-legend"><span><i class="hl-young"></i>Young GC</span><span><i class="hl-full"></i>Full GC</span></div></div>
    </div>
  </div>
  <div class="card-con">
    <div class="console" id="con-${s.id}"></div>
    <div class="cmd-row" id="cmd-${s.id}">
      <input type="text" id="inp-${s.id}" placeholder="say Hello World"
        onkeydown="if(event.key==='Enter')sendCmd('${s.id}')"/>
      <button class="btn bg-gray" onclick="sendCmd('${s.id}')">Send</button>
    </div>
  </div>
</div>`;
}

// Update cards in place: rebuild a card only when something other than live usage figures changed,
// so the console, typed input and scroll position survive a refresh and the browser does far less layout work.
const _cardSig = {};
const cardSig = s => [s.name, s.running, s.pid, s.directory, s.jar, s.autostart, s.memory_min_mb, s.memory_max_mb].join('|');

function renderServers(list) {
  const grid = document.getElementById('grid');
  if (!list.length) {
    grid.innerHTML = '<div class="empty">No servers configured yet.</div>';
    grid.dataset.ids = '';
    return;
  }
  const ids = list.map(s => s.id).join(',');
  if (grid.dataset.ids !== ids) {
    grid.innerHTML = list.map(cardHTML).join('');
    grid.dataset.ids = ids;
    for (const s of list) { _cardSig[s.id] = cardSig(s); _logSeq[s.id] = 0; }
    fixLabels(grid);
    return;
  }
  for (const s of list) {
    const sig = cardSig(s);
    if (_cardSig[s.id] !== sig) {
      const el = document.getElementById('card-' + s.id);
      if (el) { el.outerHTML = cardHTML(s); _logSeq[s.id] = 0; fixLabels(document.getElementById('card-' + s.id)); }
      _cardSig[s.id] = sig;
    } else {
      const ur = document.getElementById('ur-' + s.id);
      if (ur) ur.innerHTML = usageRowsHTML(s);
    }
  }
}

const bakSel = new Set();

let _bakSig = '';
function renderBackups(list) {
  const sig = JSON.stringify(list);
  if (sig === _bakSig) return;
  _bakSig = sig;
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
  document.getElementById('bak-del').disabled     = bakSel.size === 0;
  document.getElementById('bak-restore').disabled = bakSel.size !== 1;
  document.getElementById('bak-create').disabled  = bakSel.size !== 1;
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
  if (failed) flash(`Done - ${failed} deletion(s) failed`, true);
  else flash('Backup(s) deleted');
  refresh();
}

function renderSysinfo(sys) {
  function setBar(barId, fillCls, pct) {
    const p = Math.min(pct || 0, 100);
    const c = p >= 90 ? ' crit' : p >= 75 ? ' hi' : '';
    document.getElementById(barId).innerHTML =
      '<div class="usage-bar"><div class="usage-fill ' + fillCls + c + '" style="width:' + p + '%"></div></div>';
  }
  setBar('sys-cpu-bar', 'cpu-fill',  sys.cpu_pct);
  document.getElementById('sys-cpu-val').textContent =
    sys.cpu_pct !== null ? sys.cpu_pct.toFixed(1) + '%' : '-';

  const ramPct = sys.ram_total_mb ? Math.round(sys.ram_used_mb / sys.ram_total_mb * 100) : 0;
  setBar('sys-ram-bar', 'ram-fill', ramPct);
  document.getElementById('sys-ram-val').textContent = sys.ram_used_mb !== null
    ? (sys.ram_used_mb / 1024).toFixed(1) + ' / ' + (sys.ram_total_mb / 1024).toFixed(1) + ' GB' : '-';

  const diskPct = sys.disk_total_gb ? Math.round(sys.disk_used_gb / sys.disk_total_gb * 100) : 0;
  setBar('sys-disk-bar', 'heap-fill', diskPct);
  document.getElementById('sys-disk-val').textContent = sys.disk_used_gb !== null
    ? sys.disk_used_gb + ' / ' + sys.disk_total_gb + ' GB' : '-';
}

let _refreshing = false;

async function refresh() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    const d = await api('GET', '/api/status');
    _servers = d.servers || [];
    renderServers(_servers);
    renderBackups(d.backups || []);
    if (d.backup_dir)  document.getElementById('bak-dir').textContent = d.backup_dir;
    if (d.sysinfo)     renderSysinfo(d.sysinfo);
    if (d.max_backups !== undefined) _maxBackups = d.max_backups;
    if (d.auto_backup) { _autoBackupCfg = d.auto_backup; renderScheduleDisplay(d.auto_backup); }
    document.getElementById('upd').textContent = 'Updated ' + new Date().toLocaleTimeString();
    fetchAllLogs(d.servers || []);
    updateAllHeapGraphs(d.servers || []);
  } catch {
    document.getElementById('upd').textContent = 'Connection lost';
  } finally {
    _refreshing = false;
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
  try {
    const r = await api('POST', `/api/${sid}/backup`);
    if (!r.job) throw new Error(r.error || 'failed');
    const res = await runJob(r.job, 'Creating backup...');
    flash(`Backup: ${res.file}  (${res.size_mb} MB)`);
  } catch (e) {
    flash('Backup failed: ' + e.message, true);
  } finally {
    busyHide();
    btn.disabled = false; btn.innerHTML = '&#128190; Backup';
  }
  refresh();
}

async function saveRAM(sid) {
  const min = parseInt(document.getElementById(`min-${sid}`).value);
  const max = parseInt(document.getElementById(`max-${sid}`).value);
  if (!min || min < 256) { flash('Min RAM must be at least 256 MB', true); return; }
  if (!max || max < min) { flash('Max RAM must be ≥ Min RAM', true); return; }
  const r = await api('POST', `/api/${sid}/config`, {memory_min_mb: min, memory_max_mb: max});
  if (r.ok) flash('RAM saved - restart server to apply');
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

const wl = {sid: null};

function openWL(sid) {
  wl.sid = sid;
  const srv = document.querySelector(`#card-${sid} .card-name`);
  document.getElementById('wl-title').textContent = 'Whitelist - ' + (srv ? srv.textContent : sid);
  document.getElementById('wl-name').value = '';
  document.getElementById('wl-overlay').classList.add('open');
  wlLoad();
}

function closeWL() {
  document.getElementById('wl-overlay').classList.remove('open');
}

async function wlLoad() {
  const r = await api('GET', `/api/${wl.sid}/whitelist`);
  if (r.error) { flash(r.error, true); return; }
  const body = document.getElementById('wl-rows');
  const entries = r.entries || [];
  document.getElementById('wl-count').textContent = `${entries.length} player${entries.length === 1 ? '' : 's'} whitelisted`;
  body.textContent = '';
  if (!entries.length) {
    const tr = body.insertRow(), td = tr.insertCell();
    td.colSpan = 3; td.className = 'fb-empty'; td.textContent = 'Nobody is whitelisted';
    return;
  }
  for (const e of entries) {
    const tr = body.insertRow();
    tr.insertCell().textContent = e.name || '(unknown)';
    const u = tr.insertCell(); u.className = 'fsize'; u.textContent = e.uuid;
    const btn = document.createElement('button');
    btn.className = 'btn bg-danger'; btn.textContent = 'Remove';
    btn.onclick = () => wlRemove(e);
    tr.insertCell().append(btn);
  }
}

async function wlRemove(e) {
  if (!confirm(`Remove ${e.name || e.uuid} from the whitelist?`)) return;
  const r = await api('POST', `/api/${wl.sid}/whitelist`, {name: e.uuid || e.name, op: 'remove'});
  if (!r.ok) { flash(r.msg || r.error, true); return; }
  flash(`${e.name || e.uuid} removed`);
  wlLoad();
}

async function wlAdd() {
  const inp = document.getElementById('wl-name');
  const name = inp.value.trim();
  if (!/^[A-Za-z0-9_.]{1,32}$/.test(name)) { flash('Enter a valid player name', true); return; }
  const r = await api('POST', `/api/${wl.sid}/whitelist`, {name, op: 'add'});
  if (!r.ok) { flash(r.msg || r.error, true); return; }
  flash(`${name} whitelisted`);
  inp.value = '';
  setTimeout(wlLoad, 800);
}

const _logSeq = {};

async function fetchLogs(sid) {
  const el = document.getElementById(`con-${sid}`);
  if (!el) return;
  const r = await api('GET', `/api/${sid}/logs?since=${_logSeq[sid] || 0}`);
  if (!r.logs) return;
  _logSeq[sid] = r.seq;
  if (!r.full && !r.logs.length) return;          // nothing new: leave the DOM alone
  const stick = r.full || el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  const html = r.logs.map(l => `<p>${esc(l)}</p>`).join('');
  if (r.full) el.innerHTML = html;
  else {
    el.insertAdjacentHTML('beforeend', html);
    while (el.childElementCount > 300) el.firstElementChild.remove();
  }
  if (stick) el.scrollTop = el.scrollHeight;
}

function fetchAllLogs(list) {
  for (const s of list) fetchLogs(s.id);
}

async function toggleAutostart(sid, current) {
  const r = await api('POST', `/api/${sid}/config`, {autostart: !current});
  if (r.ok) flash(`Autostart ${!current ? 'enabled' : 'disabled'} - takes effect on next CreeperCrest restart`);
  else flash(r.error || r.msg, true);
  refresh();
}

async function delServer(sid) {
  const s = _servers.find(x => x.id === sid);
  const name = s ? s.name : sid;
  const dir  = s ? s.directory : '';
  if (!confirm(`Remove "${name}" and permanently delete its server directory?\n\n${dir}\n\nThis cannot be undone.`)) return;
  const r = await api('DELETE', `/api/${sid}`);
  if (!r.ok) { flash(r.error, true); return; }
  flash(`Removed ${name} and deleted server directory`);
  refresh();
}

// ── Create server wizard ───────────────────────────────────────────────────────

const DEFAULT_JVM_ARGS = '__JVM_ARGS__';

const WZ_STEPS = [
  ['Name your server', 'Pick a display name and the message players see under the server name in their multiplayer list (MOTD).'],
  ['Choose where it lives', 'Pick the folder that will hold the server files and world. Use Browse to navigate and create a folder.'],
  ['Choose the server software', 'CreeperCrest downloads the server JAR for you. Paper is a good default for most servers; choose "I already have a JAR" to use one you placed in the folder yourself.'],
  ['World and gameplay', 'These are written to server.properties. The defaults match a normal survival server; seed and world type only matter when the world is first created.'],
  ['Resources and finish', 'Memory and Java arguments are pre-filled with tuned defaults. Review the summary, accept the EULA and create the server.'],
];

const WZ_PROPS = [
  {k:'level-name', l:'World folder name', t:'text', d:'world'},
  {k:'level-seed', l:'World seed (blank = random)', t:'text', d:''},
  {k:'level-type', l:'World type', t:'sel', d:'normal', o:[['normal','Default'],['flat','Superflat'],['large_biomes','Large biomes'],['amplified','Amplified']]},
  {k:'gamemode', l:'Default game mode', t:'sel', d:'survival', o:[['survival','Survival'],['creative','Creative'],['adventure','Adventure'],['spectator','Spectator']]},
  {k:'difficulty', l:'Difficulty', t:'sel', d:'easy', o:[['peaceful','Peaceful'],['easy','Easy'],['normal','Normal'],['hard','Hard']]},
  {k:'max-players', l:'Max players', t:'num', d:20, min:1, max:1000},
  {k:'server-port', l:'Server port', t:'num', d:25565, min:1024, max:65535},
  {k:'view-distance', l:'View distance (chunks)', t:'num', d:10, min:2, max:32},
  {k:'simulation-distance', l:'Simulation distance (chunks)', t:'num', d:10, min:3, max:32},
  {k:'spawn-protection', l:'Spawn protection (blocks)', t:'num', d:16, min:0, max:1000},
  {k:'online-mode', l:'Online mode (verify accounts with Mojang)', t:'bool', d:true},
  {k:'pvp', l:'Player vs player', t:'bool', d:true},
  {k:'hardcore', l:'Hardcore', t:'bool', d:false},
  {k:'white-list', l:'Whitelist', t:'bool', d:true},
  {k:'allow-nether', l:'Allow the Nether', t:'bool', d:true},
  {k:'allow-flight', l:'Allow flight', t:'bool', d:false},
  {k:'enable-command-block', l:'Command blocks', t:'bool', d:false},
];

let _wzStep = 0, _wzIdTouched = false, _wzHome = '';

function wzProps() {
  document.getElementById('wz-props').innerHTML = WZ_PROPS.map(p => {
    const id = 'wz-p-' + p.k;
    if (p.t === 'bool') return `<div class="frow chk"><label><input type="checkbox" id="${id}" ${p.d ? 'checked' : ''}/> ${esc(p.l)}</label></div>`;
    if (p.t === 'sel')  return `<div class="frow"><label>${esc(p.l)}</label><select id="${id}">${p.o.map(o => `<option value="${o[0]}" ${o[0] === p.d ? 'selected' : ''}>${esc(o[1])}</option>`).join('')}</select></div>`;
    if (p.t === 'num')  return `<div class="frow"><label>${esc(p.l)}</label><input id="${id}" type="number" min="${p.min}" max="${p.max}" value="${p.d}"/></div>`;
    return `<div class="frow"><label>${esc(p.l)}</label><input id="${id}" value="${esc(p.d)}"/></div>`;
  }).join('');
  fixLabels(document.getElementById('wz-props'));
}

async function openWizard() {
  _wzStep = 0; _wzIdTouched = false;
  wzProps();
  const set = (id, v) => document.getElementById(id).value = v;
  set('wz-name', ''); set('wz-id', ''); set('wz-p-motd', 'A Minecraft Server'); set('wz-dir', '');
  set('wz-jar', 'server.jar'); set('wz-min', '512'); set('wz-max', '2048'); set('wz-args', DEFAULT_JVM_ARGS);
  set('wz-rp', ''); set('wz-rp-sha1', ''); set('wz-rp-prompt', ''); set('wz-jtype', 'paper');
  document.getElementById('wz-rp-req').checked = false;
  document.getElementById('wz-jar-file').value = '';
  document.getElementById('wz-jar-label').innerHTML = '&#8679; Choose JAR file...';
  document.getElementById('wz-eula').checked = false;
  // spread default ports so a second server does not collide with the first
  document.getElementById('wz-p-server-port').value = 25565 + (_servers ? _servers.length : 0);
  wzLoadVersions();
  document.getElementById('wz-overlay').classList.add('open');
  wzRender();
  const r = await api('GET', '/api/browse?path=~');
  if (r.home) _wzHome = r.home;
}

function wzClose() { document.getElementById('wz-overlay').classList.remove('open'); }

function wzSlug(v) { return v.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, ''); }

function wzName() {
  if (_wzIdTouched) return;
  document.getElementById('wz-id').value = wzSlug(document.getElementById('wz-name').value);
}

async function wzLoadVersions() {
  const t = document.getElementById('wz-jtype').value, sel = document.getElementById('wz-jver');
  sel.innerHTML = '<option value="">-</option>'; sel.disabled = true;
  document.getElementById('wz-upload-wrap').style.display = t ? 'none' : '';
  if (!t) return;
  sel.innerHTML = '<option value="">Loading...</option>';
  const r = await api('GET', '/api/jar_versions?type=' + t);
  if (r.error) { sel.innerHTML = '<option value="">-</option>'; flash(r.error, true); return; }
  sel.innerHTML = r.versions.map(v => `<option>${esc(v)}</option>`).join('');
  sel.disabled = false;
  wzJavaCheck();
}

async function wzJavaCheck() {
  const warn = document.getElementById('wz-java-warn');
  const ver = document.getElementById('wz-jver').value;
  warn.style.display = 'none';
  if (!ver || !document.getElementById('wz-jtype').value) return;
  const r = await api('GET', '/api/java?version=' + encodeURIComponent(ver));
  if (document.getElementById('wz-jver').value !== ver) return;   // selection changed while waiting
  if (!r.host) {
    warn.textContent = 'Java was not found on this host - install OpenJDK' + (r.required ? ' ' + r.required + '+' : '') + ' before starting the server.';
  } else if (r.required && r.host < r.required) {
    warn.textContent = `Minecraft ${ver} needs Java ${r.required} or newer, but this host has Java ${r.host}. The server will not start until you upgrade Java.`;
  } else return;
  warn.style.display = 'block';
}

function wzJarChosen(input) {
  const f = input.files[0];
  if (!f) return;
  if (!f.name.toLowerCase().endsWith('.jar')) { flash('Choose a .jar file', true); input.value = ''; return; }
  document.getElementById('wz-jar').value = f.name;
  document.getElementById('wz-jar-label').textContent = `${f.name} (${(f.size / 1048576).toFixed(1)} MB)`;
}

function wzUploadJar(sid, file) {
  const form = new FormData();
  form.append('files', file, file.name);
  return xhrUpload(`/api/${sid}/upload?path=`, form, 'Uploading JAR...');
}

function wzRender() {
  document.getElementById('wz-title').textContent = `Step ${_wzStep + 1} of ${WZ_STEPS.length}: ${WZ_STEPS[_wzStep][0]}`;
  document.getElementById('wz-help').textContent  = WZ_STEPS[_wzStep][1];
  document.getElementById('wz-steps').innerHTML   = WZ_STEPS.map((_, i) => `<span class="${i <= _wzStep ? 'done' : ''}"></span>`).join('');
  document.querySelectorAll('.wz-pane').forEach(p => p.classList.toggle('show', +p.dataset.step === _wzStep));
  document.getElementById('wz-back').style.display   = _wzStep ? '' : 'none';
  document.getElementById('wz-next').style.display   = _wzStep < WZ_STEPS.length - 1 ? '' : 'none';
  document.getElementById('wz-create').style.display = _wzStep === WZ_STEPS.length - 1 ? '' : 'none';
  if (_wzStep === WZ_STEPS.length - 1) wzSummary();
}

function wzCheck(step) {
  const v = id => document.getElementById(id).value.trim();
  if (step === 0) {
    if (!v('wz-name')) return 'Give your server a name';
    if (!v('wz-id'))   return 'ID is required';
  }
  if (step === 1) {
    if (!v('wz-dir')) return 'Choose a server folder';
    if (!v('wz-dir').startsWith('/') && !v('wz-dir').startsWith('~')) return 'Use a full path, for example ' + (_wzHome || '/home/user') + '/servers/' + v('wz-id');
  }
  if (step === 2) {
    if (!v('wz-jar')) return 'JAR filename is required';
    if (document.getElementById('wz-jtype').value && !v('wz-jver')) return 'Pick a Minecraft version';
  }
  if (step === 3) {
    for (const p of WZ_PROPS) {
      if (p.t !== 'num') continue;
      const n = parseInt(v('wz-p-' + p.k));
      if (isNaN(n) || n < p.min || n > p.max) return `${p.l} must be between ${p.min} and ${p.max}`;
    }
    if (!v('wz-p-level-name')) return 'World folder name is required';
  }
  if (step === 4) {
    if (parseInt(v('wz-min')) > parseInt(v('wz-max'))) return 'Max RAM must be at least Min RAM';
    if (!document.getElementById('wz-eula').checked) return 'Accept the Minecraft EULA to continue';
  }
  return null;
}

function wzGo(d) {
  if (d > 0) {
    const err = wzCheck(_wzStep);
    if (err) { flash(err, true); return; }
  }
  _wzStep = Math.max(0, Math.min(WZ_STEPS.length - 1, _wzStep + d));
  if (_wzStep === 1 && !document.getElementById('wz-dir').value && _wzHome)
    document.getElementById('wz-dir').value = _wzHome + '/servers/' + document.getElementById('wz-id').value.trim();
  wzRender();
}

function wzSummary() {
  const v = id => document.getElementById(id).value.trim();
  const jt = document.getElementById('wz-jtype');
  document.getElementById('wz-summary').innerHTML =
    `<b>${esc(v('wz-name'))}</b> (${esc(v('wz-id'))})<br>` +
    `Folder: <b>${esc(v('wz-dir'))}</b><br>` +
    `Software: <b>${jt.value ? esc(jt.options[jt.selectedIndex].text.replace(' (recommended)', '')) + ' ' + esc(v('wz-jver')) : 'existing ' + esc(v('wz-jar'))}</b><br>` +
    `Port <b>${esc(v('wz-p-server-port'))}</b>, ${esc(v('wz-p-gamemode'))}, ${esc(v('wz-p-difficulty'))}, ${esc(v('wz-p-max-players'))} players`;
}

async function wzCreate() {
  for (let i = 0; i < WZ_STEPS.length; i++) {
    const err = wzCheck(i);
    if (err) { _wzStep = i; wzRender(); flash(err, true); return; }
  }
  const v = id => document.getElementById(id).value.trim();
  const properties = {motd: v('wz-p-motd')};
  for (const p of WZ_PROPS) {
    const el = document.getElementById('wz-p-' + p.k);
    properties[p.k] = p.t === 'bool' ? String(el.checked) : el.value.trim();
  }
  const body = {
    id: v('wz-id'), name: v('wz-name'), directory: v('wz-dir'), jar: v('wz-jar'),
    memory_min_mb: parseInt(v('wz-min')) || 512, memory_max_mb: parseInt(v('wz-max')) || 2048,
    extra_args: v('wz-args'), jar_type: v('wz-jtype'), jar_version: v('wz-jver'),
    resource_pack: v('wz-rp'), resource_pack_sha1: v('wz-rp-sha1'),
    resource_pack_prompt: v('wz-rp-prompt'), resource_pack_required: document.getElementById('wz-rp-req').checked,
    eula: true, properties,
  };
  busyShow('Creating server...');
  try {
    const r = await api('POST', '/api/add', body);
    if (!r.ok) { flash(r.error || 'Failed to create server', true); return; }
    if (r.job) await runJob(r.job, 'Downloading server JAR...');
    const jf = document.getElementById('wz-jar-file').files[0];
    if (!body.jar_type && jf) {
      const u = await wzUploadJar(body.id, jf);
      if (!u.ok) {
        wzClose(); refresh();
        flash(`Server created, but JAR upload failed (${u.error}) - upload it from the file browser`, true);
        return;
      }
    }
    wzClose();
    flash(`Server "${body.name}" created`);
    refresh();
  } catch (e) { flash(e.message || 'Failed to create server', true); }
  finally { busyHide(); }
}

// ── Folder picker ──────────────────────────────────────────────────────────────

let _dpPath = '', _dpParent = null;

function dpOpen() {
  document.getElementById('dp-overlay').classList.add('open');
  dpLoad(document.getElementById('wz-dir').value.trim() || '~');
}
function dpClose() { document.getElementById('dp-overlay').classList.remove('open'); }

async function dpLoad(path) {
  let r = await api('GET', '/api/browse?path=' + encodeURIComponent(path));
  if (r.error) r = await api('GET', '/api/browse?path=~');   // typed path does not exist yet
  if (r.error) { flash(r.error, true); return; }
  _dpPath = r.path; _dpParent = r.parent;
  document.getElementById('dp-path').textContent = r.path;
  const rows = [];
  if (r.parent) rows.push(`<div class="dp-item" data-p="${esc(r.parent)}">&#8593; ..</div>`);
  r.dirs.forEach(n => rows.push(`<div class="dp-item" data-p="${esc((r.path.endsWith('/') ? r.path : r.path + '/') + n)}">&#128193; ${esc(n)}</div>`));
  const list = document.getElementById('dp-list');
  list.innerHTML = rows.join('') || '<div class="dp-empty">No sub-folders</div>';
  list.querySelectorAll('.dp-item').forEach(el => el.onclick = () => dpLoad(el.dataset.p));
}

async function dpNew() {
  const name = (prompt('New folder name:') || '').trim();
  if (!name) return;
  const r = await api('POST', '/api/browse_mkdir', {path: _dpPath, name});
  if (r.ok) dpLoad(r.path); else flash(r.error, true);
}

function dpSelect() {
  document.getElementById('wz-dir').value = _dpPath;
  dpClose();
}

// ── Add / Edit server modal ────────────────────────────────────────────────────

let _editSid = null;

function openEdit(sid) {
  const s = _servers.find(x => x.id === sid);
  if (!s) return;
  _editSid = sid;
  document.getElementById('modal-title').textContent = `Edit Server - ${s.name}`;
  document.getElementById('modal-submit-btn').textContent = 'Save Changes';
  document.getElementById('f-id-wrap').style.display = 'none';
  document.getElementById('f-name').value = s.name;
  document.getElementById('f-dir').value  = s.directory;
  document.getElementById('f-jar').value  = s.jar;
  document.getElementById('f-min').value  = s.memory_min_mb;
  document.getElementById('f-max').value  = s.memory_max_mb;
  document.getElementById('f-args').value = s.extra_args || '';
  document.getElementById('f-rp-actions').style.display = '';
  document.getElementById('f-rp').value = s.resource_pack || '';
  document.getElementById('f-rp-sha1').value = s.resource_pack_sha1 || '';
  document.getElementById('f-rp-prompt').value = s.resource_pack_prompt || '';
  document.getElementById('f-rp-req').checked = !!s.resource_pack_required;
  document.getElementById('add-overlay').classList.add('open');
}

function rpDone(r) {
  if (r.ok && typeof r.msg === 'string') {
    document.getElementById('f-rp').value = r.msg;
    document.getElementById('f-rp-sha1').value = r.sha1 || '';
    flash('Resource pack installed - restart the server to apply. Clients must be able to reach ' + r.msg);
    refresh();
  } else flash(r.error || 'Failed', true);
}

async function rpFetch() {
  const url = document.getElementById('f-rp').value.trim();
  if (!url) { flash('Enter the pack URL in the Resource pack URL field first', true); return; }
  try {
    const r = await api('POST', `/api/${_editSid}/rp_fetch`, {url});
    if (!r.job) { flash(r.error || 'Failed', true); return; }
    rpDone({ok: true, ...(await runJob(r.job, 'Downloading resource pack...'))});
  } catch (e) { flash(e.message, true); }
  finally { busyHide(); }
}

async function rpUpload() {
  const input = document.getElementById('f-rp-file');
  if (!input.files.length) return;
  const form = new FormData();
  form.append('file', input.files[0], input.files[0].name);
  const r = await xhrUpload(`/api/${_editSid}/rp_upload`, form, 'Uploading resource pack...');
  busyHide();
  input.value = '';
  rpDone(r);
}

function closeAdd() { document.getElementById('add-overlay').classList.remove('open'); }

function submitModal() { submitEdit(); }

async function submitEdit() {
  const g = id => document.getElementById(id).value.trim();
  const sid = _editSid;
  const body = {
    name:          g('f-name'),
    directory:     g('f-dir'),
    jar:           g('f-jar') || 'server.jar',
    memory_min_mb: parseInt(g('f-min')) || 512,
    memory_max_mb: parseInt(g('f-max')) || 2048,
    extra_args:    g('f-args'),
    resource_pack:      g('f-rp'),
    resource_pack_sha1: g('f-rp-sha1'),
    resource_pack_prompt:   g('f-rp-prompt'),
    resource_pack_required: document.getElementById('f-rp-req').checked,
  };
  if (!body.directory) { flash('Directory is required', true); return; }
  if (body.memory_min_mb > body.memory_max_mb) { flash('Max RAM must be ≥ Min RAM', true); return; }
  const r = await api('POST', `/api/${sid}/config`, body);
  if (!r.ok) { flash(r.error || r.msg, true); return; }
  closeAdd();
  flash(`"${body.name || sid}" saved - restart server to apply changes`);
  refresh();
}

// ── Import server from ZIP ─────────────────────────────────────────────────────

function openImport() {
  document.getElementById('fi-id').value   = '';
  document.getElementById('fi-name').value = '';
  document.getElementById('fi-dir').value  = '';
  document.getElementById('fi-jar').value  = 'server.jar';
  document.getElementById('fi-min').value  = '512';
  document.getElementById('fi-max').value  = '2048';
  document.getElementById('fi-args').value = DEFAULT_JVM_ARGS;
  document.getElementById('import-file-label').textContent = '\\u2B06 Choose ZIP file\\u2026';
  document.getElementById('import-zip').value = '';
  document.getElementById('import-prog').style.display = 'none';
  document.getElementById('import-submit-btn').disabled = false;
  document.getElementById('import-overlay').classList.add('open');
}

function closeImport() { document.getElementById('import-overlay').classList.remove('open'); }

function importFileChosen(input) {
  document.getElementById('import-file-label').textContent =
    input.files.length ? '\\u2B06 ' + input.files[0].name : '\\u2B06 Choose ZIP file\\u2026';
}

async function submitImport() {
  const g = id => document.getElementById(id).value.trim();
  const zipInput = document.getElementById('import-zip');
  if (!zipInput.files.length) { flash('Choose a ZIP file first', true); return; }
  const sid = g('fi-id');
  if (!sid) { flash('ID is required', true); return; }
  if (!/^[a-z0-9][a-z0-9\\-]*$/.test(sid)) { flash('ID: use only lowercase letters, numbers, dashes', true); return; }
  const dir = g('fi-dir');
  if (!dir) { flash('Directory is required', true); return; }
  const min = parseInt(g('fi-min')) || 512;
  const max = parseInt(g('fi-max')) || 2048;
  if (min > max) { flash('Max RAM must be \\u2265 Min RAM', true); return; }

  const form = new FormData();
  form.append('file', zipInput.files[0], zipInput.files[0].name);
  form.append('id',            sid);
  form.append('name',          g('fi-name') || sid);
  form.append('directory',     dir);
  form.append('jar',           g('fi-jar') || 'server.jar');
  form.append('memory_min_mb', min);
  form.append('memory_max_mb', max);
  form.append('extra_args',    g('fi-args'));

  const prog = document.getElementById('import-prog');
  const fill = document.getElementById('import-prog-fill');
  const val  = document.getElementById('import-prog-val');
  const lbl  = document.getElementById('import-prog-label');
  const btn  = document.getElementById('import-submit-btn');
  prog.style.display = 'block';
  btn.disabled = true;
  lbl.textContent = 'Uploading\\u2026';

  try {
    const d = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.upload.onprogress = e => {
        if (!e.lengthComputable) return;
        const mb = v => (v / 1048576).toFixed(1);
        fill.style.width = Math.min(e.loaded / e.total * 95, 95) + '%';
        val.textContent = mb(e.loaded) + ' / ' + mb(e.total) + ' MB';
      };
      xhr.onload  = () => { try { resolve(JSON.parse(xhr.responseText)); } catch { reject(); } };
      xhr.onerror = () => reject();
      xhr.open('POST', '/api/import');
      xhr.setRequestHeader('X-CC-CSRF', CSRF);
      xhr.send(form);
    });
    prog.style.display = 'none';
    btn.disabled = false;
    if (!d.ok) { flash(d.error || 'Import failed', true); return; }
    closeImport();
    await runJob(d.job, 'Extracting server files...');
    flash('Server imported successfully');
    refresh();
  } catch (e) {
    prog.style.display = 'none';
    btn.disabled = false;
    flash(e && e.message ? e.message : 'Import failed', true);
  } finally {
    busyHide();
  }
}

// ── File browser ───────────────────────────────────────────────────────────────

function openFB(sid) {
  fb.sid  = sid;
  fb.path = '';
  fb.sel  = new Set();
  const srv = document.querySelector(`#card-${sid} .card-name`);
  document.getElementById('fb-title').textContent = 'Files - ' + (srv ? srv.textContent : sid);
  document.getElementById('fb-overlay').classList.add('open');
  loadDir('');
}

function fbUp() {
  const i = fb.path.lastIndexOf('/');
  loadDir(i < 0 ? '' : fb.path.slice(0, i));
}

function closeFB() {
  if (ed.dirty && !confirm('Discard unsaved changes?')) return;
  edReset();
  document.getElementById('fb-overlay').classList.remove('open');
}

async function loadDir(path) {
  fb.path = path;
  fb.sel  = new Set();
  document.getElementById('fb-up').disabled = !path;
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
  let html = `<span data-path="">&#127968; root</span>`;
  let acc  = '';
  for (const p of parts) {
    acc += (acc ? '/' : '') + p;
    html += `<span class="sep">/</span><span data-path="${esc(acc)}">${esc(p)}</span>`;
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
    return `<tr class="${e.type}">
  <td><input type="checkbox" data-name="${esc(e.name)}"></td>
  <td>${icon} <span class="fn" data-kind="${e.type}" data-path="${esc(namePath)}">${esc(e.name)}</span></td>
  <td class="fsize">${e.type === 'dir' ? '-' : e.size}</td>
  <td class="fdate">${e.modified}</td>
</tr>`;
  }).join('');
}

// File names come from the server's disk, so they are never placed in inline JS: handlers read data-* attributes.
document.getElementById('fb-crumb').addEventListener('click', ev => {
  const t = ev.target.closest('[data-path]');
  if (t) loadDir(t.dataset.path);
});
document.getElementById('fb-rows').addEventListener('click', ev => {
  const t = ev.target.closest('.fn');
  if (!t) return;
  if (t.dataset.kind === 'dir') loadDir(t.dataset.path);
  else if (EDITABLE.test(t.dataset.path)) edOpen(t.dataset.path);
  else dlFile(t.dataset.path);
});
document.getElementById('fb-rows').addEventListener('change', ev => {
  if (ev.target.matches('input[type=checkbox]')) toggleSel(ev.target.dataset.name);
});

// Built-in text editor: opens to the right of the file list for text-like files.
const EDITABLE = /[.](properties|json|ya?ml|txt|log|cfg|conf|toml|ini|md|xml|csv|sh|mcmeta|lang|secret|env|bat|js|py)$/i;
const ed = {path: null, dirty: false, cm: null};

function edText() { return ed.cm ? ed.cm.getValue() : document.getElementById('fb-ed-text').value; }

function edReset() {
  if (ed.cm) { ed.cm.destroy(); ed.cm = null; }
  document.getElementById('fb-ed-text').value = '';
  ed.path = null; edMark(false);
  document.getElementById('fb-modal').classList.remove('with-editor');
}

function edMark(dirty) {
  ed.dirty = dirty;
  document.getElementById('fb-ed-name').classList.toggle('dirty', dirty);
}

async function edOpen(path) {
  if (ed.dirty && !confirm('Discard unsaved changes?')) return;
  const r = await api('GET', `/api/${fb.sid}/filetext?path=${encodeURIComponent(path)}`);
  if (r.error) { flash(r.error, true); return; }
  ed.path = path;
  document.getElementById('fb-ed-name').textContent = path;
  const ta = document.getElementById('fb-ed-text'), host = document.getElementById('fb-ed-cm');
  if (ed.cm) { ed.cm.destroy(); ed.cm = null; }
  host.textContent = '';
  if (window.CCEditor) {     // CodeMirror; the plain textarea below is the fallback if the bundle failed to load
    ta.style.display = 'none'; host.style.display = '';
    ed.cm = CCEditor.create(host, {text: r.text, filename: path, onChange: () => edMark(true), onSave: edSave});
  } else {
    host.style.display = 'none'; ta.style.display = '';
    ta.value = r.text; ta.scrollTop = 0;
  }
  document.getElementById('fb-modal').classList.add('with-editor');
  edMark(false);
  (ed.cm || ta).focus();
}

async function edSave() {
  if (!ed.path) return;
  const r = await api('POST', `/api/${fb.sid}/filesave?path=${encodeURIComponent(ed.path)}`,
                      {text: edText()});
  if (r.ok) { edMark(false); flash('Saved ' + ed.path.split('/').pop()); loadDir(fb.path); }
  else flash(r.error, true);
}

function edDownload() { if (ed.path) dlFile(ed.path); }

function edClose() {
  if (ed.dirty && !confirm('Discard unsaved changes?')) return;
  edReset();
}

document.getElementById('fb-ed-text').addEventListener('input', () => edMark(true));
document.getElementById('fb-ed-text').addEventListener('keydown', ev => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') { ev.preventDefault(); edSave(); }
  if (ev.key === 'Tab') {
    ev.preventDefault();
    const t = ev.target, a = t.selectionStart;
    t.setRangeText('  ', a, t.selectionEnd, 'end');
    edMark(true);
  }
});

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

// ── File browser progress helpers ──────────────────────────────────────────────

function fbProgShow(label, indet = false) {
  document.getElementById('fb-prog').classList.add('show');
  document.getElementById('fb-prog-label').textContent = label;
  document.getElementById('fb-prog-val').textContent   = '';
  const fill = document.getElementById('fb-prog-fill');
  fill.style.width = '0%';
  fill.classList.toggle('indet', indet);
}
function fbProgSet(pct, val = '') {
  const fill = document.getElementById('fb-prog-fill');
  fill.classList.remove('indet');
  fill.style.width = Math.min(pct, 100) + '%';
  if (val !== '') document.getElementById('fb-prog-val').textContent = val;
}
function fbProgHide() {
  document.getElementById('fb-prog').classList.remove('show');
  const fill = document.getElementById('fb-prog-fill');
  fill.classList.remove('indet');
  fill.style.width = '0%';
}

async function dlZip() {
  const names = fb.sel.size
    ? [...fb.sel].map(f => fb.path ? fb.path + '/' + f : f)
    : fb.entries.filter(e => e.type === 'file').map(e => fb.path ? fb.path + '/' + e.name : e.name);
  if (!names.length) { flash('No files to download', true); return; }
  fbProgShow('Preparing ZIP…', true);
  try {
    const r = await fetch(`/api/${fb.sid}/zip`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CC-CSRF': CSRF},
      body: JSON.stringify({files: names}),
    });
    if (!r.ok) { fbProgHide(); flash('ZIP failed', true); return; }
    fbProgSet(100, 'Saving…');
    const blob = await r.blob();
    fbProgHide();
    const url = URL.createObjectURL(blob);
    Object.assign(document.createElement('a'), { href: url, download: `${fb.sid}-files.zip` }).click();
    URL.revokeObjectURL(url);
  } catch { fbProgHide(); flash('ZIP failed', true); }
}

async function newFolder() {
  const name = (prompt('New folder name:') || '').trim();
  if (!name) return;
  const r = await api('POST', `/api/${fb.sid}/mkdir?path=${encodeURIComponent(fb.path)}`, {name});
  if (r.ok) { flash('Created "' + r.name + '"'); loadDir(fb.path); }
  else flash(r.error || 'Create failed', true);
}

async function doUpload() {
  const input = document.getElementById('fb-upload');
  if (!input.files.length) return;
  const form = new FormData();
  for (const f of input.files) form.append('files', f, f.name);
  fbProgShow('Uploading…');
  try {
    const d = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.upload.onprogress = e => {
        if (!e.lengthComputable) return;
        const mb = v => (v / 1048576).toFixed(1);
        fbProgSet(e.loaded / e.total * 100, mb(e.loaded) + ' / ' + mb(e.total) + ' MB');
      };
      xhr.onload  = () => { try { resolve(JSON.parse(xhr.responseText)); } catch { reject(); } };
      xhr.onerror = () => reject();
      xhr.open('POST', '/api/' + fb.sid + '/upload?path=' + encodeURIComponent(fb.path));
      xhr.setRequestHeader('X-CC-CSRF', CSRF);
      xhr.send(form);
    });
    fbProgHide();
    input.value = '';
    if (d.ok) flash('Uploaded ' + d.count + ' file(s)');
    else flash(d.error, true);
    loadDir(fb.path);
  } catch { fbProgHide(); input.value = ''; flash('Upload failed', true); }
}

async function delSelected() {
  const names = [...fb.sel];
  if (!names.length) return;
  if (!confirm(`Delete ${names.length} item(s)? This cannot be undone.`)) return;
  let done = 0, failed = 0;
  fbProgShow('Deleting…');
  fbProgSet(0, '0 / ' + names.length);
  for (const name of names) {
    const p = fb.path ? fb.path + '/' + name : name;
    const r = await api('DELETE', `/api/${fb.sid}/file?path=${encodeURIComponent(p)}`);
    if (!r.ok) failed++;
    fbProgSet(++done / names.length * 100, done + ' / ' + names.length);
  }
  fbProgHide();
  flash(failed ? `Done (${failed} failed)` : `Deleted ${names.length} item(s)`, !!failed);
  loadDir(fb.path);
}

// ── Restore backup ─────────────────────────────────────────────────────────────

function openRestore() {
  if (bakSel.size !== 1) return;
  const fname = [...bakSel][0];
  document.getElementById('restore-fname').value = fname;
  const select = document.getElementById('restore-sid');
  select.innerHTML = _servers.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  if (!select.options.length) { flash('No servers configured', true); return; }
  // Guess server from filename: strip trailing -YYYYMMDD-HHmmss.zip
  const guessed = fname.replace(/-\\d{8}-\\d{6}\\.zip$/, '');
  for (const opt of select.options) { if (opt.value === guessed) { opt.selected = true; break; } }
  document.getElementById('restore-overlay').classList.add('open');
}

function closeRestore() { document.getElementById('restore-overlay').classList.remove('open'); }

function busyShow(label) {
  document.getElementById('busy-label').textContent = label;
  busySet(null, '');
  document.getElementById('busy-overlay').classList.add('open');
}
function busySet(pct, val) {
  const fill = document.getElementById('busy-fill');
  if (pct == null) { fill.classList.add('indet'); fill.style.width = ''; }
  else { fill.classList.remove('indet'); fill.style.width = Math.min(100, pct) + '%'; }
  document.getElementById('busy-val').textContent = val || '';
}
const _mb = v => (v / 1048576).toFixed(1);
async function runJob(jid, label) {
  busyShow(label);
  for (;;) {
    const j = await api('GET', '/api/job/' + jid);
    if (j.error && !j.state) throw new Error(j.error);
    if (j.label) document.getElementById('busy-label').textContent = j.label;
    if (j.total > 0) busySet(j.done / j.total * 100, j.unit === 'files' ? `${j.done} / ${j.total} files` : `${_mb(j.done)} / ${_mb(j.total)} MB`);
    else busySet(null, j.unit === 'bytes' && j.done ? _mb(j.done) + ' MB' : '');
    if (j.state === 'done') return j.result;
    if (j.state === 'error') throw new Error(j.error);
    await new Promise(r => setTimeout(r, 300));
  }
}
function xhrUpload(url, form, label) {
  busyShow(label);
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.upload.onprogress = e => { if (e.lengthComputable) busySet(e.loaded / e.total * 100, `${_mb(e.loaded)} / ${_mb(e.total)} MB`); };
    xhr.onload  = () => { try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({error: 'upload failed'}); } };
    xhr.onerror = () => resolve({error: 'upload failed'});
    xhr.open('POST', url);
    xhr.setRequestHeader('X-CC-CSRF', CSRF);
    xhr.send(form);
  });
}
function busyHide() { document.getElementById('busy-overlay').classList.remove('open'); }

async function submitRestore() {
  const fname = document.getElementById('restore-fname').value;
  const sid   = document.getElementById('restore-sid').value;
  const srv   = _servers.find(s => s.id === sid);
  if (srv && srv.running) { flash('Stop the server before restoring', true); return; }
  if (!confirm(`Restore "${fname}" onto "${srv ? srv.name : sid}"?\n\nThis OVERWRITES all server files and cannot be undone.`)) return;
  closeRestore();
  try {
    const r = await api('POST', '/api/restore', {backup: fname, sid});
    if (!r.job) flash(r.msg || r.error, true);
    else { const res = await runJob(r.job, 'Restoring backup...'); flash(res.msg || `Restored ${fname}`); }
  } catch (e) {
    flash(e.message, true);
  } finally {
    busyHide();
  }
  refresh();
}

// -- Create server from backup ---------------------------------------------------

function openCreateFromBackup() {
  if (bakSel.size !== 1) return;
  const fname = [...bakSel][0];
  document.getElementById('cfb-fname').value = fname;
  const guessed = fname.replace(/-\\d{8}-\\d{6}\\.zip$/, '');
  const src = _servers.find(s => s.id === guessed);
  let newId = guessed + '-restored';
  while (_servers.some(s => s.id === newId)) newId += '2';
  document.getElementById('cfb-id').value   = newId;
  document.getElementById('cfb-name').value = src ? src.name + ' (restored)' : guessed;
  document.getElementById('cfb-dir').value  = src
    ? (src.directory.endsWith('/') ? src.directory.slice(0, -1) : src.directory) + '-restored'
    : '';
  document.getElementById('cfb-jar').value  = src ? src.jar           : 'server.jar';
  document.getElementById('cfb-min').value  = src ? src.memory_min_mb : 512;
  document.getElementById('cfb-max').value  = src ? src.memory_max_mb : 2048;
  document.getElementById('cfb-args').value = src ? (src.extra_args || '')
    : DEFAULT_JVM_ARGS;
  document.getElementById('cfb-overlay').classList.add('open');
}

function closeCreateFromBackup() { document.getElementById('cfb-overlay').classList.remove('open'); }

async function submitCreateFromBackup() {
  const g = id => document.getElementById(id).value.trim();
  const body = {
    backup:        g('cfb-fname'),
    id:            g('cfb-id'),
    name:          g('cfb-name'),
    directory:     g('cfb-dir'),
    jar:           g('cfb-jar') || 'server.jar',
    memory_min_mb: parseInt(g('cfb-min')) || 512,
    memory_max_mb: parseInt(g('cfb-max')) || 2048,
    extra_args:    g('cfb-args'),
  };
  if (!body.id)        { flash('ID is required', true); return; }
  if (!body.directory) { flash('Directory is required', true); return; }
  if (body.memory_min_mb > body.memory_max_mb) { flash('Max RAM must be \\u2265 Min RAM', true); return; }
  closeCreateFromBackup();
  try {
    const r = await api('POST', '/api/create_from_backup', body);
    if (!r.ok) { flash(r.error, true); return; }
    await runJob(r.job, 'Extracting backup...');
    flash(`Server "${body.name || body.id}" created from backup`);
    refresh();
  } catch (e) {
    flash(e.message, true);
  } finally {
    busyHide();
  }
}

// ── Auto-backup schedule ───────────────────────────────────────────────────────

let _autoBackupCfg = {enabled: false, day: 'sunday', hour: 3, minute: 0};
let _maxBackups = 0;

function openAutoBackup() {
  document.getElementById('ab-enabled').value = _autoBackupCfg.enabled ? '1' : '0';
  document.getElementById('ab-day').value     = _autoBackupCfg.day    || 'sunday';
  document.getElementById('ab-hour').value    = _autoBackupCfg.hour   ?? 3;
  document.getElementById('ab-minute').value  = _autoBackupCfg.minute ?? 0;
  document.getElementById('ab-max').value     = _maxBackups;
  document.getElementById('ab-overlay').classList.add('open');
}

function closeAutoBackup() { document.getElementById('ab-overlay').classList.remove('open'); }

function normMaxBackups(v) {
  v = parseInt(v) || 0;
  return v <= 0 ? 0 : Math.max(3, Math.min(28, v));
}

function clampMaxBackups(el) { el.value = normMaxBackups(el.value); }

async function submitAutoBackup() {
  const body = {
    enabled: document.getElementById('ab-enabled').value === '1',
    day:     document.getElementById('ab-day').value,
    hour:    parseInt(document.getElementById('ab-hour').value)   || 0,
    minute:  parseInt(document.getElementById('ab-minute').value) || 0,
    max_backups: normMaxBackups(document.getElementById('ab-max').value),
  };
  const r = await api('POST', '/api/auto_backup', body);
  if (r.ok) {
    _autoBackupCfg = r.auto_backup;
    _maxBackups = r.max_backups;
    renderScheduleDisplay(r.auto_backup);
    flash(body.enabled
      ? `Auto-backup scheduled - every ${body.day} at ${String(body.hour).padStart(2,'0')}:${String(body.minute).padStart(2,'0')}`
      : 'Auto-backup disabled');
    closeAutoBackup();
  } else flash(r.error || 'Failed to save schedule', true);
}

// ── Boot ───────────────────────────────────────────────────────────────────────

let _refreshTimer = null;

function startRefresh() {
  const input = document.getElementById('refresh-secs');
  const secs  = Math.max(5, parseInt(input.value) || 5);
  input.value = secs;
  clearInterval(_refreshTimer);
  _refreshTimer = setInterval(() => { if (!document.hidden) refresh(); }, secs * 1000);
}

document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

document.getElementById('refresh-secs').addEventListener('change', startRefresh);

fixLabels(document);

// Restore last active page
(function () {
  const page = localStorage.getItem('cc-page') || 'servers';
  const btn  = document.querySelector(`#main-nav .nav-link:${page === 'backups' ? 'last-child' : 'first-child'}`);
  showPage(page, btn);
})();

refresh();
startRefresh();
</script>
</body>
</html>
"""

# ── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    timeout        = 60              # idle socket timeout: drops slow/stalled connections
    server_version = "CreeperCrest"
    sys_version    = ""

    def log_message(self, *_): pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control",  "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control",  "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, content_type, filename=None):
        self.send_response(200)
        self.send_header("Content-Type",   content_type)
        self.send_header("Content-Length", len(data))
        if filename:
            safe_name = re.sub(r"[^\w. -]", "_", filename)[:150] or "download"
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    # ── Authentication gate ────────────────────────────────────────────────────

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'")
        if TLS_ON or self.headers.get("X-Forwarded-Proto", "") == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _redirect(self, loc, cookie=None):
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self._security_headers()
        self.end_headers()

    def _cookie(self, value, max_age):
        flags = "HttpOnly; SameSite=Strict; Path=/"
        if TLS_ON or self.headers.get("X-Forwarded-Proto", "") == "https":
            flags += "; Secure"
        return f"cc_session={value}; {flags}; Max-Age={max_age}"

    def _session_token(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c["cc_session"].value if "cc_session" in c else ""
        except Exception:
            return ""

    def _login_attempt(self):
        ip = self.client_ip()
        n = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        user = form.get("username", [""])[0].strip().lower()[:64]
        pw, code = form.get("password", [""])[0], form.get("totp", [""])[0]
        if login_throttled(ip, user):
            print(f"[auth] LOCKED OUT login attempt user={user!r} ip={ip}", flush=True)
            return self.send_html(render_login("Too many failed attempts. Try again in 15 minutes."), 429)
        rec = load_users().get(user)
        pw_ok   = verify_password(pw, rec["pw"] if rec else _DUMMY_HASH)
        step    = verify_totp(rec["totp"] if rec else _DUMMY_TOTP, code, user if rec else None)
        if rec and pw_ok and step:
            mark_totp_used(user, step)
            print(f"[auth] login ok user={user} ip={ip}", flush=True)
            return self._redirect("/", self._cookie(create_session(user), SESSION_MAX))
        record_login_failure(ip, user)
        print(f"[auth] login FAILED user={user!r} ip={ip}", flush=True)
        return self.send_html(render_login("Invalid username, password or authenticator code."), 401)

    def client_ip(self):
        """Peer address; X-Forwarded-For is honoured only when the peer is a configured trusted proxy."""
        peer = self.client_address[0]
        trusted = set(cfg.get("trusted_proxies", []))
        xff = self.headers.get("X-Forwarded-For", "")
        if peer in trusted and xff:
            for hop in reversed([h.strip() for h in xff.split(",")]):
                if hop not in trusted:
                    return hop
        return peer

    def _lan_open(self):
        """True when LAN mode is on and this request comes straight from a private/loopback address."""
        if not cfg.get("lan_mode"):
            return False
        if any(self.headers.get(h) for h in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Via", "CF-Connecting-IP")):
            return False            # came through a proxy: could be the internet
        try:
            ip = ipaddress.ip_address(self.client_address[0].split("%")[0])
        except ValueError:
            return False
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]").lower()   # blocks DNS-rebinding hostnames
        if host in ("localhost", socket.gethostname().lower()) or host.endswith(".local") \
                or host in [h.lower() for h in cfg.get("allowed_hosts", [])]:
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def gate(self, method):
        """Return True if the request was answered (login page, redirect, 401...); sets self.sess otherwise."""
        self.sess = {"user": "(auth disabled)", "csrf": ""}
        path = urlparse(self.path).path
        if method == "POST" and int(self.headers.get("Content-Length", 0) or 0) > MAX_UPLOAD:
            self.send_json({"error": "request too large"}, 413)
            return True
        if method == "GET" and path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return True
        if method == "GET" and path.startswith("/resourcepack/"):   # Minecraft clients cannot log in
            return False
        origin = self.headers.get("Origin")
        if method in ("POST", "DELETE") and origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            self.send_json({"error": "cross-origin request refused"}, 403)
            return True
        open_mode = AUTH_DISABLED or self._lan_open()
        if path == "/login":
            if open_mode:
                self._redirect("/")
            elif method == "POST":
                self._login_attempt()
            elif get_session(self._session_token()):
                self._redirect("/")
            else:
                self.send_html(render_login() if load_users() else render_setup_required())
            return True
        if open_mode:
            sess = {"user": "(lan)", "csrf": _OPEN_TOKEN}
        else:
            sess = get_session(self._session_token())
            if sess and sess["user"] not in load_users():
                drop_session(self._session_token())
                sess = None
            if not sess:
                if not load_users():
                    self.send_html(render_setup_required(), 503)
                elif path.startswith("/api/") or method != "GET":
                    self.send_json({"error": "login required"}, 401)
                else:
                    self._redirect("/login")
                return True
        if method in ("POST", "DELETE") and not hmac.compare_digest(self.headers.get("X-CC-CSRF", ""), sess["csrf"]):
            self.send_json({"error": "bad CSRF token - reload the page"}, 403)
            return True
        self.sess = sess
        if method in ("POST", "DELETE"):
            print(f"[audit] {sess['user']} {method} {path} from {self.client_ip()}", flush=True)
        return False

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 1 << 20:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(n)) if n else {}

    def qs(self):
        return parse_qs(urlparse(self.path).query)

    def segs(self):
        return [s for s in urlparse(self.path).path.split("/") if s]

    # ── HEAD (resource pack only) ──────────────────────────────────────────────

    def do_HEAD(self):
        parts = self.segs()
        if len(parts) == 2 and parts[0] == "resourcepack" and parts[1].endswith(".zip"):
            srv = servers.get(parts[1][:-4])
            if srv and os.path.isfile(_rp_path(srv)):
                self.send_response(200)
                self.send_header("Content-Type",   "application/zip")
                self.send_header("Content-Length", str(os.path.getsize(_rp_path(srv))))
                self.end_headers()
                return
            self.send_response(404)
        else:
            self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.gate("GET"):
            return
        parts = self.segs()

        if not parts:
            interval = str(int(cfg.get("refresh_interval", 5)))
            return self.send_html(HTML.replace("__REFRESH_INTERVAL__", interval)
                                      .replace("__JVM_ARGS__", default_jvm_args())
                                      .replace("__CSRF__", self.sess["csrf"])
                                      .replace("__ACCT__", _acct_html(self.sess["user"])))

        if parts == ["static", "editor.js"]:     # bundled CodeMirror, served locally so the panel works offline
            try:
                with open(os.path.join(BASE_DIR, "static", "editor.js"), "rb") as f:
                    data = f.read()
            except OSError:
                return self.send_json({"error": "not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "private, max-age=3600")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)
            return

        if parts == ["2fa-setup"]:
            return self.send_html(render_2fa_setup(self.sess["user"]))

        if parts == ["api", "status"]:
            return self.send_json({
                "servers":     [s.status() for s in servers.values()],
                "backups":     list_backups(),
                "backup_dir":  os.path.expanduser(cfg.get("backup_dir", "~/mc-backups")),
                "sysinfo":     _get_sysinfo(),
                "auto_backup": cfg.get("auto_backup", {"enabled": False, "day": "sunday", "hour": 3, "minute": 0}),
                "max_backups": int(cfg.get("max_backups", 0) or 0),
            })

        if len(parts) == 3 and parts[0] == "api" and parts[2] == "logs":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            srv   = servers[sid]
            seq   = srv.log_seq
            lines = list(srv.logs)
            try:
                since = int(self.qs().get("since", ["0"])[0])
            except ValueError:
                since = 0
            new = seq - since
            if since <= 0 or new < 0 or new > len(lines):
                return self.send_json({"logs": lines, "seq": seq, "full": True})
            return self.send_json({"logs": lines[len(lines) - new:] if new else [], "seq": seq, "full": False})

        if len(parts) == 3 and parts[0] == "api" and parts[2] == "whitelist":
            if parts[1] not in servers:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json({"entries": whitelist_entries(servers[parts[1]])})

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
                    entries.append({"name": name, "type": "dir",  "size": "-",                   "modified": mtime})
                else:
                    entries.append({"name": name, "type": "file", "size": _fmt_size(stat.st_size), "modified": mtime})
            # dirs first
            entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
            return self.send_json({"path": rel, "entries": entries})

        # /api/{id}/filetext?path=  - read a text file for the built-in editor
        if len(parts) == 3 and parts[0] == "api" and parts[2] == "filetext":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            safe = _safe_path(servers[sid].cfg.get("directory", ""), unquote(self.qs().get("path", [""])[0]))
            if not safe or not os.path.isfile(safe):
                return self.send_json({"error": "file not found"}, 404)
            if os.path.getsize(safe) > EDIT_MAX:
                return self.send_json({"error": f"file is larger than {EDIT_MAX // 1024} KB; download it instead"}, 400)
            try:
                with open(safe, "rb") as f:
                    raw = f.read()
                if b"\0" in raw:
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return self.send_json({"error": "not a UTF-8 text file"}, 400)
            return self.send_json({"text": text, "crlf": "\r\n" in text})

        # /api/{id}/file?path=  - download single file
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

        # /api/job/{id}  - progress of a background job
        if len(parts) == 3 and parts[:2] == ["api", "job"]:
            job = _jobs.get(parts[2])
            if not job:
                return self.send_json({"error": "job not found"}, 404)
            return self.send_json(job.view())

        # /api/browse?path=  - list sub-directories (folder picker)
        if parts == ["api", "browse"]:
            try:
                return self.send_json(list_dirs(unquote(self.qs().get("path", ["~"])[0])))
            except (OSError, ValueError) as e:
                return self.send_json({"error": str(e)}, 400)

        # /api/java?version=  - host Java vs the Java the chosen Minecraft version needs
        if parts == ["api", "java"]:
            ver = self.qs().get("version", [""])[0]
            try:
                req = required_java(ver) if re.match(r"^[A-Za-z0-9._-]+$", ver) else None
            except Exception:
                req = None
            return self.send_json({"host": host_java_major(), "required": req})

        # /api/jar_versions?type=paper
        if parts == ["api", "jar_versions"]:
            try:
                return self.send_json({"versions": jar_versions(self.qs().get("type", [""])[0])})
            except Exception as e:
                return self.send_json({"error": f"could not load versions: {e}"}, 502)

        # /resourcepack/{sid}.zip  - public, fetched by Minecraft clients
        if len(parts) == 2 and parts[0] == "resourcepack" and parts[1].endswith(".zip"):
            srv = servers.get(parts[1][:-4])
            if not srv or not os.path.isfile(_rp_path(srv)):
                return self.send_json({"error": "not found"}, 404)
            with open(_rp_path(srv), "rb") as f:
                data = f.read()
            srv._append(f"[CreeperCrest] Resource pack requested by {self.client_ip()} "
                        f"({re.sub(r'[^A-Za-z0-9_]', '', self.headers.get('X-Minecraft-Username', ''))[:16] or 'unknown player'})")
            self.send_bytes(data, "application/zip", "resource-pack.zip")
            return

        # /backups/{filename}
        if len(parts) == 2 and parts[0] == "backups":
            fpath = backup_path(unquote(parts[1]))
            if not fpath or not os.path.isfile(fpath):
                return self.send_json({"error": "not found"}, 404)
            with open(fpath, "rb") as f:
                data = f.read()
            self.send_bytes(data, "application/zip", os.path.basename(fpath))
            return

        self.send_json({"error": "not found"}, 404)

    # ── POST ───────────────────────────────────────────────────────────────────

    def do_POST(self):
        if self.gate("POST"):
            return
        parts = self.segs()

        if parts == ["logout"]:
            drop_session(self._session_token())
            self.send_response(200)
            self.send_header("Set-Cookie", self._cookie("", 0))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        # /api/restore
        if parts == ["api", "restore"]:
            b     = self.body()
            fname = b.get("backup", "").strip()
            sid   = b.get("sid",    "").strip()
            if not fname or not sid:
                return self.send_json({"error": "backup and sid required"}, 400)
            return self.send_json({"ok": True, "job": start_job(
                "Restoring backup...", lambda job: {"msg": _expect_ok(restore_backup(fname, sid, job))}, "files")})

        # /api/auto_backup  - save auto-backup schedule
        if parts == ["api", "auto_backup"]:
            b = self.body()
            sched = {
                "enabled": bool(b.get("enabled", False)),
                "day":     b.get("day",    "sunday").lower(),
                "hour":    max(0, min(23, int(b.get("hour",   3)))),
                "minute":  max(0, min(59, int(b.get("minute", 0)))),
            }
            if sched["day"] not in _WEEKDAYS:
                return self.send_json({"error": "invalid day"}, 400)
            cfg["auto_backup"] = sched
            mb = int(b.get("max_backups", cfg.get("max_backups", 0)) or 0)
            cfg["max_backups"] = 0 if mb <= 0 else max(3, min(28, mb))
            save_cfg(cfg)
            return self.send_json({"ok": True, "auto_backup": sched, "max_backups": cfg["max_backups"]})

        # /api/import  - upload a zip, extract it, register a new server
        if parts == ["api", "import"]:
            ct = self.headers.get("Content-Type", "")
            cl = int(self.headers.get("Content-Length", 0))
            if "boundary=" not in ct:
                return self.send_json({"error": "expected multipart/form-data"}, 400)
            boundary = ct.split("boundary=")[1].strip().encode()
            raw      = self.rfile.read(cl)
            fields, files = _parse_multipart_full(raw, boundary)

            sid = fields.get("id", "").strip().lower().replace(" ", "-")
            if not sid:
                return self.send_json({"error": "id required"}, 400)
            if not SID_RE.match(sid):
                return self.send_json({"error": "id: lowercase letters, digits and dashes only (max 32)"}, 400)
            if sid in servers:
                return self.send_json({"error": f'id "{sid}" already exists'}, 400)

            directory = fields.get("directory", "").strip()
            if not directory:
                return self.send_json({"error": "directory required"}, 400)

            zip_data = next((data for fname, data in files if fname.lower().endswith(".zip")), None)
            if not zip_data:
                return self.send_json({"error": "no zip file uploaded"}, 400)

            dest = os.path.expanduser(directory)
            if not zipfile.is_zipfile(io.BytesIO(zip_data)):
                return self.send_json({"error": "uploaded file is not a valid zip"}, 400)
            legacy = max(256, int(fields.get("memory_mb", "1024") or 1024))
            scfg = {
                "name":          (fields.get("name", sid).strip() or sid),
                "directory":     directory,
                "jar":           (fields.get("jar", "server.jar").strip() or "server.jar"),
                "memory_min_mb": max(256, int(fields.get("memory_min_mb", legacy) or legacy)),
                "memory_max_mb": max(256, int(fields.get("memory_max_mb", legacy) or legacy)),
                "extra_args":    fields.get("extra_args", "").strip(),
                "autostart":     False,
            }
            if scfg["memory_min_mb"] > scfg["memory_max_mb"]:
                return self.send_json({"error": "max RAM must be >= min RAM"}, 400)

            def finish(job):
                os.makedirs(dest, exist_ok=True)
                try:
                    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                        _extract_zip(zf, dest, job)
                except Exception as e:
                    raise RuntimeError(f"failed to extract zip: {e}")
                cfg["servers"][sid] = scfg
                save_cfg(cfg)
                servers[sid] = ManagedServer(sid, scfg)
                return {"id": sid}
            return self.send_json({"ok": True, "job": start_job("Extracting server files...", finish, "files")})

        # /api/create_from_backup - register a new server and extract a backup into it
        if parts == ["api", "create_from_backup"]:
            b     = self.body()
            fname = b.get("backup", "").strip()
            sid   = b.get("id", "").strip().lower().replace(" ", "-")
            if not sid:
                return self.send_json({"error": "id required"}, 400)
            if not SID_RE.match(sid):
                return self.send_json({"error": "id: lowercase letters, digits and dashes only (max 32)"}, 400)
            if sid in servers:
                return self.send_json({"error": f'id "{sid}" already exists'}, 400)
            if not fname:
                return self.send_json({"error": "backup required"}, 400)
            fpath = backup_path(fname)
            if not fpath or not os.path.isfile(fpath):
                return self.send_json({"error": "backup file not found"}, 400)
            directory = b.get("directory", "").strip()
            if not directory:
                return self.send_json({"error": "directory required"}, 400)
            legacy = max(256, int(b.get("memory_mb", 1024)))
            scfg = {
                "name":          b.get("name", sid).strip() or sid,
                "directory":     directory,
                "jar":           b.get("jar", "server.jar").strip() or "server.jar",
                "memory_min_mb": max(256, int(b.get("memory_min_mb", legacy))),
                "memory_max_mb": max(256, int(b.get("memory_max_mb", legacy))),
                "extra_args":    b.get("extra_args", "").strip(),
                "autostart":     False,
            }
            if scfg["memory_min_mb"] > scfg["memory_max_mb"]:
                return self.send_json({"error": "max RAM must be >= min RAM"}, 400)
            dest = os.path.expanduser(directory)
            def finish(job):
                os.makedirs(dest, exist_ok=True)
                try:
                    with zipfile.ZipFile(fpath, "r") as zf:
                        _extract_zip(zf, dest, job)
                except Exception as e:
                    raise RuntimeError(f"failed to extract backup: {e}")
                cfg["servers"][sid] = scfg
                save_cfg(cfg)
                servers[sid] = ManagedServer(sid, scfg)
                servers[sid]._append(f"[CreeperCrest] Created from backup: {fname}")
                return {"id": sid}
            return self.send_json({"ok": True, "job": start_job("Extracting backup...", finish, "files")})

        # /api/browse_mkdir  - create a folder from the folder picker
        if parts == ["api", "browse_mkdir"]:
            b    = self.body()
            name = b.get("name", "").strip()
            if not name or re.search(r"[/\\]", name) or name in (".", ".."):
                return self.send_json({"error": "invalid folder name"}, 400)
            try:
                base = os.path.abspath(os.path.expanduser(b.get("path", "")))
                os.makedirs(os.path.join(base, name), exist_ok=True)
                return self.send_json({"ok": True, "path": os.path.join(base, name)})
            except OSError as e:
                return self.send_json({"error": str(e)}, 400)

        # /api/add
        if parts == ["api", "add"]:
            b   = self.body()
            sid = b.get("id", "").strip().lower().replace(" ", "-")
            if not sid:
                return self.send_json({"error": "id required"}, 400)
            if not SID_RE.match(sid):
                return self.send_json({"error": "id: lowercase letters, digits and dashes only (max 32)"}, 400)
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
                "autostart":     bool(b.get("autostart", False)),
                "resource_pack":      b.get("resource_pack", "").strip(),
                "resource_pack_sha1": b.get("resource_pack_sha1", "").strip().lower(),
                "resource_pack_required": bool(b.get("resource_pack_required", False)),
                "resource_pack_prompt":   b.get("resource_pack_prompt", "").strip(),
            }
            if scfg["memory_min_mb"] > scfg["memory_max_mb"]:
                return self.send_json({"error": "max RAM must be >= min RAM"}, 400)
            perr = _check_pack(scfg["resource_pack"], scfg["resource_pack_sha1"], scfg["resource_pack_prompt"])
            props, perr2 = validate_props(b.get("properties"))
            perr = perr or perr2
            if perr:
                return self.send_json({"error": perr}, 400)
            if not scfg["directory"]:
                return self.send_json({"error": "directory required"}, 400)
            jtype = b.get("jar_type", "").strip().lower()
            ver   = b.get("jar_version", "").strip()
            if jtype:
                if jtype not in JAR_TYPES:
                    return self.send_json({"error": "unknown server type"}, 400)
                if os.path.basename(scfg["jar"]) != scfg["jar"]:
                    return self.send_json({"error": "JAR must be a plain filename"}, 400)
                if not ver:
                    return self.send_json({"error": "pick a Minecraft version"}, 400)
            eula = bool(b.get("eula"))

            def finish(job=None):
                dest_dir = os.path.expanduser(scfg["directory"])
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                except OSError as e:
                    raise RuntimeError(f"cannot create folder: {e}")
                if jtype:
                    try:
                        download_jar(jtype, ver, os.path.join(dest_dir, scfg["jar"]), job)
                    except Exception as e:
                        raise RuntimeError(f"JAR download failed: {e}")
                try:
                    if props:
                        _set_properties(scfg["directory"], props)
                    if eula:
                        with open(os.path.join(dest_dir, "eula.txt"), "w") as f:
                            f.write("eula=true\n")
                    if scfg["resource_pack"] or scfg["resource_pack_required"] or scfg["resource_pack_prompt"]:
                        _apply_resource_pack(scfg)
                except OSError as e:
                    raise RuntimeError(f"failed to write server.properties: {e}")
                cfg["servers"][sid] = scfg
                save_cfg(cfg)
                servers[sid] = ManagedServer(sid, scfg)
                return {"id": sid}

            if jtype:
                return self.send_json({"ok": True, "job": start_job("Downloading server JAR...", finish)})
            try:
                finish()
            except RuntimeError as e:
                return self.send_json({"error": str(e)}, 400)
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

            # /api/{id}/filesave?path=  - overwrite an existing text file from the built-in editor
            if action == "filesave":
                b    = self.body()
                text = b.get("text")
                safe = _safe_path(srv.cfg.get("directory", ""), unquote(self.qs().get("path", [""])[0]))
                if not isinstance(text, str) or not safe or not os.path.isfile(safe):
                    return self.send_json({"error": "invalid file"}, 400)
                data = text.encode("utf-8")
                if len(data) > EDIT_MAX:
                    return self.send_json({"error": f"file would exceed {EDIT_MAX // 1024} KB"}, 400)
                try:
                    with open(safe, "rb") as f:
                        head = f.read(EDIT_MAX + 1)
                    if b"\0" in head or len(head) > EDIT_MAX:
                        return self.send_json({"error": "refusing to overwrite a binary or oversized file"}, 400)
                    head.decode("utf-8")
                    tmp = safe + ".cc-part"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    shutil.copymode(safe, tmp)
                    os.replace(tmp, safe)
                except UnicodeDecodeError:
                    return self.send_json({"error": "refusing to overwrite a non-UTF-8 file"}, 400)
                except OSError as e:
                    return self.send_json({"error": str(e)}, 500)
                return self.send_json({"ok": True, "size": len(data)})

            # /api/{id}/mkdir?path=  - create a folder
            if action == "mkdir":
                b    = self.body()
                name = os.path.basename(b.get("name", "").strip().strip("/\\"))
                if not name or name in (".", ".."):
                    return self.send_json({"error": "invalid folder name"}, 400)
                rel    = unquote(self.qs().get("path", [""])[0])
                base   = os.path.expanduser(srv.cfg.get("directory", ""))
                parent = _safe_path(base, rel)
                if not parent or not os.path.isdir(parent):
                    return self.send_json({"error": "invalid path"}, 400)
                target = _safe_path(base, os.path.join(rel, name))
                if not target:
                    return self.send_json({"error": "invalid path"}, 400)
                if os.path.exists(target):
                    return self.send_json({"error": "already exists"}, 400)
                try:
                    os.mkdir(target)
                except Exception as e:
                    return self.send_json({"error": str(e)}, 500)
                return self.send_json({"ok": True, "name": name})

            # /api/{id}/zip  - download selected files as zip
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

            # multipart body must be read before the JSON body parse below
            if action == "rp_upload":
                ct = self.headers.get("Content-Type", "")
                cl = int(self.headers.get("Content-Length", 0))
                if "boundary=" not in ct:
                    return self.send_json({"error": "expected multipart/form-data"}, 400)
                if cl > RP_MAX_BYTES + 65536:
                    return self.send_json({"error": "Pack larger than 250 MB"}, 400)
                boundary = ct.split("boundary=")[1].strip().encode()
                files = _parse_multipart(self.rfile.read(cl), boundary)
                if not files:
                    return self.send_json({"error": "no file uploaded"}, 400)
                ok, msg = install_resource_pack(srv, files[0][1], self.headers.get("Host", "localhost"))
                if not ok:
                    return self.send_json({"error": msg}, 400)
                return self.send_json({"ok": True, **_rp_result(srv, msg)})

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
            elif action == "whitelist":
                name, op = str(b.get("name", "")).strip(), b.get("op")
                if op == "remove" and _UUID_RE.fullmatch(name):
                    ok, msg = whitelist_remove_uuid(srv, name)
                    return self.send_json({"ok": ok, "msg": msg}, 200 if ok else 400)
                if op not in ("add", "remove") or not _PLAYER_RE.fullmatch(name):
                    return self.send_json({"error": "invalid player name"}, 400)
                if op == "add":
                    srv._banned.discard(name)
                    srv.send_command(f"pardon {name}")     # lift an auto-ban from an earlier attempt
                ok, msg = srv.send_command(f"whitelist {op} {name}")
                if not ok:
                    msg = "Start the server first to change its whitelist"
            elif action == "backup":
                return self.send_json({"ok": True, "job": start_job(
                    "Creating backup...", lambda job: _expect_ok(do_backup(sid, job)))})
            elif action == "rp_fetch":
                url  = b.get("url", "").strip()
                host = self.headers.get("Host", "localhost")
                if not re.match(r"^https?://", url, re.I):
                    return self.send_json({"error": "URL must start with http:// or https://"}, 400)
                def finish(job):
                    data = fetch_url(url, job)
                    job.label, job.total = "Installing resource pack...", 0
                    return _rp_result(srv, _expect_ok(install_resource_pack(srv, data, host)))
                return self.send_json({"ok": True, "job": start_job("Downloading resource pack...", finish)})
            elif action == "config":
                if "name" in b and b["name"].strip():
                    srv.cfg["name"] = b["name"].strip()
                if "directory" in b and b["directory"].strip():
                    srv.cfg["directory"] = b["directory"].strip()
                if "jar" in b:
                    srv.cfg["jar"] = b["jar"].strip() or "server.jar"
                if "memory_min_mb" in b:
                    srv.cfg["memory_min_mb"] = max(256, int(b["memory_min_mb"]))
                if "memory_max_mb" in b:
                    srv.cfg["memory_max_mb"] = max(256, int(b["memory_max_mb"]))
                if "extra_args" in b:
                    srv.cfg["extra_args"] = b["extra_args"]
                if "autostart" in b:
                    srv.cfg["autostart"] = bool(b["autostart"])
                _rpk = ("resource_pack", "resource_pack_sha1", "resource_pack_required", "resource_pack_prompt")
                old_rp = tuple(srv.cfg.get(k, "") for k in _rpk)
                if "resource_pack_required" in b:
                    srv.cfg["resource_pack_required"] = bool(b["resource_pack_required"])
                if "resource_pack_prompt" in b:
                    srv.cfg["resource_pack_prompt"] = b["resource_pack_prompt"].strip()
                if "resource_pack" in b:
                    srv.cfg["resource_pack"] = b["resource_pack"].strip()
                if "resource_pack_sha1" in b:
                    srv.cfg["resource_pack_sha1"] = b["resource_pack_sha1"].strip().lower()
                rp_changed = tuple(srv.cfg.get(k, "") for k in _rpk) != old_rp
                perr = _check_pack(srv.cfg.get("resource_pack", ""), srv.cfg.get("resource_pack_sha1", ""),
                                   srv.cfg.get("resource_pack_prompt", ""))
                if perr:
                    for k, v in zip(_rpk, old_rp):
                        srv.cfg[k] = v
                    return self.send_json({"error": perr}, 400)
                if srv.cfg.get("memory_min_mb", 256) > srv.cfg.get("memory_max_mb", 256):
                    return self.send_json({"error": "max RAM must be >= min RAM"}, 400)
                if rp_changed:
                    try:
                        _apply_resource_pack(srv.cfg)
                    except OSError as e:
                        return self.send_json({"error": f"failed to write server.properties: {e}"}, 400)
                cfg["servers"][sid] = srv.cfg
                save_cfg(cfg)
                ok, msg = True, "Saved"
            else:
                return self.send_json({"error": "unknown action"}, 400)

            return self.send_json({"ok": ok, "msg": msg})

        self.send_json({"error": "not found"}, 404)

    # ── DELETE ─────────────────────────────────────────────────────────────────

    def do_DELETE(self):
        if self.gate("DELETE"):
            return
        parts = self.segs()

        # /api/backup/{filename}  - delete a backup zip
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "backup":
            fpath = backup_path(unquote(parts[2]))
            if not fpath or not os.path.isfile(fpath):
                return self.send_json({"error": "not found"}, 404)
            try:
                os.remove(fpath)
                return self.send_json({"ok": True})
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)

        # /api/{id}  - remove server and delete its directory
        if len(parts) == 2 and parts[0] == "api":
            sid = parts[1]
            if sid not in servers:
                return self.send_json({"error": "not found"}, 404)
            if servers[sid].is_running():
                return self.send_json({"error": "stop the server before removing it"}, 400)
            directory = os.path.expanduser(servers[sid].cfg.get("directory", ""))
            del servers[sid]
            del cfg["servers"][sid]
            save_cfg(cfg)
            if directory and os.path.isdir(directory):
                import shutil
                try:
                    shutil.rmtree(directory)
                except Exception as e:
                    return self.send_json({"ok": True, "warn": f"Removed from config but could not delete directory: {e}"})
            return self.send_json({"ok": True})

        # /api/{id}/file?path=  - delete file or directory
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

    if len(sys.argv) > 1 and sys.argv[1] in CLI_COMMANDS:
        sys.exit(cli(sys.argv[1:]))

    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8080))

    def shutdown(*_):
        print("\nShutting down - stopping all running servers…")
        for srv in servers.values():
            if srv.is_running():
                print(f"  Stopping {srv.id}…")
                srv.stop()
        sys.exit(0)

    _sig.signal(_sig.SIGTERM, shutdown)
    _sig.signal(_sig.SIGINT,  shutdown)

    threading.Thread(target=_sysinfo_sampler,        daemon=True, name='sysinfo').start()
    threading.Thread(target=_autobackup_scheduler,   daemon=True, name='autobackup').start()

    for sid, srv in list(servers.items()):
        if srv.cfg.get("autostart", False):
            print(f"  Auto-starting {sid}…")
            ok, msg = srv.start()
            if not ok:
                print(f"    Failed: {msg}")

    if AUTH_DISABLED:
        print("WARNING: auth_disabled is set - the panel has NO login. Anyone who can reach it controls your servers.")
    elif cfg.get("lan_mode"):
        print("LAN mode ON: private-network clients need no login; other addresses must sign in. Never port-forward this panel.")
    if not AUTH_DISABLED and not load_users() and not cfg.get("lan_mode"):
        print("WARNING: no users exist, so the panel is locked. Create one with:  python3 creepercrest.py adduser <name>")

    httpd = ThreadingHTTPServer((host, port), Handler)
    if TLS_ON:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(os.path.expanduser(cfg["tls_cert"]), os.path.expanduser(cfg["tls_key"]))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"CreeperCrest  →  {'https' if TLS_ON else 'http'}://{host}:{port}")
    print(f"Backups    →  {os.path.expanduser(cfg.get('backup_dir', '~/mc-backups'))}")
    print("Press Ctrl+C to stop.\n")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
