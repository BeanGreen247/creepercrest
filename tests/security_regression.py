#!/usr/bin/env python3
"""Security regression tests for the CreeperCrest panel. Works against either backend.

    python3 tests/security_regression.py --backend cpp    [--cpp-bin cpp/creepercrest]
    python3 tests/security_regression.py --backend python [--python-script python/creepercrest.py]
    python3 tests/security_regression.py --backend cpp --only forged_ban,symlink_upload

Each test starts from a temporary directory with its own panel process, so nothing on the machine is touched.
Standard library only. Exit status is the number of failed tests.
"""
import argparse, base64, hashlib, hmac, http.client, io, json, os, re, shutil, signal, socket, stat
import struct, subprocess, sys, tempfile, threading, time, urllib.parse, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOST = "127.0.0.1"


def free_port():
    s = socket.socket()
    s.bind((HOST, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def totp(secret, step=None):
    step = int(time.time() // 30) if step is None else step
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", step), "sha1").digest()
    o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6)


FAKE_SERVER = """#!/bin/bash
# stand-in native server: "echo <text>" prints <text>; every stdin line is recorded next to this script
echo "[00:00:00 INFO]: Done (0.1s)! For help, type \\"help\\""
while IFS= read -r line; do
  echo "$line" >> "$(dirname "$0")/stdin.log"
  case "$line" in echo\\ *) echo "${line#echo }";; stop) exit 0;; esac
done
"""


class Panel:
    def __init__(self, args, cfg=None, umask=None):
        self.args, self.cfg, self.umask = args, cfg or {}, umask
        self.dir = tempfile.mkdtemp(prefix="cc-sec-")
        self.port = free_port()
        self.proc = None
        self.csrf = ""
        self.cookie = ""
        self.fake = os.path.join(self.dir, "fake.sh")
        with open(self.fake, "w") as f:
            f.write(FAKE_SERVER)
        os.chmod(self.fake, 0o755)
        if args.backend == "python":
            shutil.copy(args.python_script, os.path.join(self.dir, "creepercrest.py"))
            src_static = os.path.join(os.path.dirname(args.python_script), "static")
            if os.path.isdir(src_static):
                shutil.copytree(src_static, os.path.join(self.dir, "static"))
            self.cmd = [sys.executable, os.path.join(self.dir, "creepercrest.py")]
        else:
            self.cmd = [os.path.abspath(args.cpp_bin)]
        self.env = dict(os.environ, CREEPERCREST_DIR=self.dir)
        self.write_cfg({"host": HOST, "port": self.port, "backup_dir": os.path.join(self.dir, "bk"), "servers": {}})
        self.log_path = os.path.join(self.dir, "panel.log")
        out = subprocess.run(self.cmd + ["adduser", "tester"], env=self.env, capture_output=True, text=True).stdout
        self.password = re.search(r"Password\s*:\s*(\S+)", out).group(1)
        self.secret = re.search(r"2FA key\s*:\s*(\S+)", out).group(1)

    def write_cfg(self, updates):
        p = os.path.join(self.dir, "config.json")
        try:
            c = json.load(open(p))
        except OSError:
            c = {}
        c.update(updates)
        json.dump(c, open(p, "w"), indent=2)

    def start(self, **cfg):
        self.cfg.update(cfg)
        if self.cfg:
            self.write_cfg(self.cfg)
        um = self.umask

        def pre():
            if um is not None:
                os.umask(um)
        self.logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(self.cmd, env=self.env, stdout=self.logf, stderr=subprocess.STDOUT,
                                     cwd=self.dir, preexec_fn=pre)
        for _ in range(100):
            try:
                socket.create_connection((HOST, self.port), timeout=0.3).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("panel did not start: " + open(self.log_path).read()[-400:])

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "logf", None):
            self.logf.close()

    def restart(self, **cfg):
        self.stop()
        return self.start(**cfg)

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def log(self):
        try:
            return open(self.log_path, errors="replace").read()
        except OSError:
            return ""

    # ---- HTTP helpers -------------------------------------------------
    def req(self, method, path, body=None, headers=None, raw=False, auth=True, host=None):
        c = http.client.HTTPConnection(host or HOST, self.port, timeout=60)
        h = {}
        if auth:
            h.update({"Cookie": self.cookie, "X-CC-CSRF": self.csrf})
        h.update(headers or {})
        data = None
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        elif body is not None:
            data = body if isinstance(body, bytes) else body.encode()
        c.request(method, path, data, h)
        r = c.getresponse()
        b = r.read()
        if raw:
            return r.status, dict(r.getheaders()), b
        try:
            return r.status, json.loads(b)
        except ValueError:
            return r.status, b

    def login(self):
        c = http.client.HTTPConnection(HOST, self.port)
        form = urllib.parse.urlencode({"username": "tester", "password": self.password, "totp": totp(self.secret)})
        c.request("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        self.cookie = r.getheader("Set-Cookie", "").split(";")[0]
        c.request("GET", "/", headers={"Cookie": self.cookie})
        page = c.getresponse().read().decode()
        m = re.search(r"const CSRF = '([^']+)'", page)
        if not m:
            raise RuntimeError("login failed")
        self.csrf = m.group(1)
        return self

    def job(self, jid, timeout=60):
        t = time.time()
        while time.time() - t < timeout:
            st, j = self.req("GET", "/api/job/" + jid)
            if st == 200 and j["state"] != "running":
                return j
            time.sleep(0.3)
        raise TimeoutError("job still running")

    def add_fake(self, sid="fake", extra=None):
        d = os.path.join(self.dir, "srv-" + sid)
        os.makedirs(d, exist_ok=True)
        body = {"id": sid, "name": sid, "directory": d, "jar": self.fake, "runtime": "native"}
        body.update(extra or {})
        st, j = self.req("POST", "/api/add", body)
        assert st == 200, (st, j)
        return d

    def raw(self, data, wait=1.0, source=None):
        s = socket.socket()
        s.settimeout(3)
        if source:
            s.bind((source, 0))
        s.connect((HOST, self.port))
        s.sendall(data)
        s.settimeout(wait)
        out = b""
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                out += d
        except OSError:
            pass
        s.close()
        return out


def multipart(files, fields=None):
    b = "----ccsec"
    out = b""
    for k, v in (fields or {}).items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, fn, data in files:
        out += (f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), {"Content-Type": f"multipart/form-data; boundary={b}"}


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in entries:
            zf.writestr(n, d)
    return buf.getvalue()


def wait_for(cond, timeout=8.0, step=0.2):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(step)
    return cond()


# ======================================================================
# tests: each takes (args) and returns (ok, detail)
# ======================================================================

def t_prebody(a):
    """An unauthenticated client must not be able to make the panel read a huge body."""
    p = Panel(a).start()
    try:
        s = socket.create_connection((HOST, p.port))
        n = 50 << 20
        s.sendall(f"POST /api/status HTTP/1.1\r\nHost: x\r\nContent-Length: {n}\r\n\r\n".encode())
        chunk = b"A" * (1 << 20)
        sent = 0
        s.setblocking(False)
        t0 = time.time()
        try:
            while sent < n and time.time() - t0 < 10:
                try:
                    s.send(chunk)
                    sent += len(chunk)
                except BlockingIOError:
                    time.sleep(0.01)
        except OSError:
            pass
        s.close()
        return sent < (8 << 20), f"{sent >> 20} MB accepted before the panel stopped reading"
    finally:
        p.cleanup()


def t_forged_ban(a):
    """Chat text must never trigger the automatic bans; the real server message still must."""
    p = Panel(a).start().login()
    try:
        p.add_fake()
        p.req("POST", "/api/fake/start", {})
        time.sleep(1)
        stdin = os.path.join(p.dir, "stdin.log")
        open(stdin, "w").close()
        forged = [
            "[12:00:00 INFO]: <Mallory> Disconnecting Alice (/8.8.8.8:1234): you are not whitelisted",
            "[12:00:00 INFO]: [Not Secure] <Mallory> name=Bob, x not white-listed (/9.9.9.9:5)",
            "[12:00:00 INFO]: Mallory issued server command: /say Disconnecting Carol (/7.7.7.7:5): not whitelisted",
            "[12:00:00 INFO]: [Mallory -> me] Dave[/6.6.6.6:5] lost connection: not whitelisted",
        ]
        for line in forged:
            p.req("POST", "/api/fake/command", {"command": "echo " + line})
        time.sleep(1.5)
        bans = [l for l in open(stdin).read().splitlines() if l.startswith("ban")]
        if bans:
            return False, "forged lines caused: " + "; ".join(bans)
        p.req("POST", "/api/fake/command", {"command": "echo [12:00:00 INFO]: Disconnecting Erin (/8.8.4.4:1234): You are not whitelisted on this server!"})
        p.req("POST", "/api/fake/command", {"command": "echo [12:00:00 INFO]: com.mojang.authlib.GameProfile@5f1d2a3b[id=069a79f4-44e9-4726-a5be-fca90e38aaf5,name=Frank,properties={},legacy=false] (/5.5.5.5:1234) lost connection: You are not whitelisted on this server!"})
        p.req("POST", "/api/fake/command", {"command": "echo [12:00:00 INFO]: Gina[/4.4.4.4:55] lost connection: You are not whitelisted on this server!"})

        def banned():
            got = open(stdin).read().splitlines()
            return all(any(l.startswith(x) for l in got) for x in ("ban Erin", "ban-ip 8.8.4.4", "ban Frank", "ban-ip 5.5.5.5", "ban Gina", "ban-ip 4.4.4.4"))
        ok = wait_for(banned, timeout=10)
        p.req("POST", "/api/fake/stop", {})
        return ok, "forged lines ignored; real not-whitelisted message " + ("still bans" if ok else "NO LONGER bans")
    finally:
        p.cleanup()


def t_symlink_upload(a):
    """A symlink inside a server folder must not let an upload or an extraction write outside it."""
    p = Panel(a).start().login()
    try:
        d = p.add_fake()
        victim = os.path.join(p.dir, "victim.txt")
        outside = os.path.join(p.dir, "outside")
        os.makedirs(outside)
        open(victim, "w").write("ORIGINAL")
        os.symlink(victim, os.path.join(d, "link.txt"))
        os.symlink(outside, os.path.join(d, "sub"))
        body, h = multipart([("f", "link.txt", b"OVERWRITTEN")])
        p.req("POST", "/api/fake/upload?path=", body, h)
        notes = []
        if open(victim).read() != "ORIGINAL":
            notes.append("upload overwrote a file outside the folder")
        # restore a backup into a folder that contains a symlinked sub-directory
        os.makedirs(os.path.join(p.dir, "bk"), exist_ok=True)
        open(os.path.join(p.dir, "bk", "evil-20260101-000000.zip"), "wb").write(make_zip([("sub/pwned.txt", "x")]))
        st, j = p.req("POST", "/api/create_from_backup", {"backup": "evil-20260101-000000.zip", "id": "zz", "directory": d})
        if st == 200 and "job" in j:
            p.job(j["job"])
        if os.path.exists(os.path.join(outside, "pwned.txt")):
            notes.append("extraction followed a symlinked directory")
        return not notes, "; ".join(notes) or "writes stayed inside the server folder"
    finally:
        p.cleanup()


def t_log_forging(a):
    """Line breaks in a username or URL must not forge log lines."""
    p = Panel(a).start().login()
    try:
        c = http.client.HTTPConnection(HOST, p.port)
        c.request("POST", "/login", urllib.parse.urlencode({"username": "x\n[auth] login ok user=admin ip=1.2.3.4", "password": "p", "totp": "000000"}),
                  {"Content-Type": "application/x-www-form-urlencoded"})
        c.getresponse().read()
        p.req("POST", "/api/x%0A%5Baudit%5D%20forged%20admin%20POST%20/api/evil%20from%209.9.9.9", {})
        time.sleep(0.3)
        bad = [l for l in p.log().splitlines() if l.startswith("[auth] login ok user=admin") or l.startswith("[audit] forged")]
        return not bad, ("forged lines: %s" % bad) if bad else "control characters are escaped in the log"
    finally:
        p.cleanup()


def t_conn_cap(a):
    """Idle half-open connections from one address must not lock out other addresses, and must be dropped."""
    p = Panel(a).start()
    socks = []
    try:
        for _ in range(140):
            try:
                s = socket.socket()
                s.settimeout(2)
                s.bind(("127.0.0.1", 0))
                s.connect((HOST, p.port))
                s.sendall(b"GET /login HTTP/1.1\r\nHost: a\r\nX-A: ")
                socks.append(s)
            except OSError:
                break
        time.sleep(0.5)
        other = p.raw(b"GET /login HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\n", wait=3, source="127.0.0.2").split(b"\r\n")[0]
        if b" 200 " not in other:
            return False, "another address was refused while one address held idle connections: " + other.decode(errors="replace")
        # the idle connections must be dropped by the server on its own
        def served():
            r = p.raw(b"GET /login HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\n", wait=3).split(b"\r\n")[0]
            return b" 200 " in r
        ok = wait_for(served, timeout=25, step=1.0)
        return ok, "other addresses stay served; the held address recovers on its own" if ok else "the same address stayed locked out for 25 s"
    finally:
        for s in socks:
            s.close()
        p.cleanup()


class _Hits(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_):
        pass


def t_lan_ssrf(a):
    """LAN mode skips the login but must not switch off the protection against fetching internal addresses."""
    web = ThreadingHTTPServer((HOST, 0), _Hits)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    _Hits.hits = 0
    p = Panel(a, cfg={"lan_mode": True}).start()
    try:
        page = http.client.HTTPConnection(HOST, p.port)
        page.request("GET", "/")
        p.csrf = re.search(r"const CSRF = '([^']+)'", page.getresponse().read().decode()).group(1)
        p.add_fake()
        st, j = p.req("POST", "/api/fake/rp_fetch", {"url": f"http://{HOST}:{web.server_address[1]}/pack.zip"}, auth=False,
                      headers={"X-CC-CSRF": p.csrf})
        res = p.job(j["job"]) if st == 200 and "job" in j else {"state": "rejected", "error": str(j)}
        return _Hits.hits == 0, f"internal web server was contacted {_Hits.hits}x ({res.get('error')})"
    finally:
        web.shutdown()
        p.cleanup()


def t_2fa_secret(a):
    """A logged-in session alone must not be enough to read the authenticator secret."""
    p = Panel(a).start().login()
    try:
        st, page = p.req("GET", "/2fa-setup", raw=True)[0::2]
        if p.secret.encode() in page:
            return False, "the 2FA page shows the secret without asking for the password"
        st1, _ = p.req("POST", "/api/2fa", {"password": "wrong-password"})
        st2, j = p.req("POST", "/api/2fa", {"password": p.password})
        good = st1 in (401, 403) and st2 == 200 and isinstance(j, dict) and j.get("secret") == p.secret
        return good, f"wrong password -> {st1}, right password -> {st2}"
    finally:
        p.cleanup()


def t_dup_headers(a):
    """Duplicate Content-Length or Host headers must be refused (request smuggling guard)."""
    p = Panel(a).start()
    try:
        cl = p.raw(b"POST /login HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\nContent-Length: 0\r\n\r\nhello").split(b"\r\n")[0]
        ho = p.raw(b"GET /login HTTP/1.1\r\nHost: a\r\nHost: b\r\nConnection: close\r\n\r\n").split(b"\r\n")[0]
        return b" 400 " in cl and b" 400 " in ho, f"duplicate Content-Length -> {cl.decode(errors='replace')}; duplicate Host -> {ho.decode(errors='replace')}"
    finally:
        p.cleanup()


def t_rp_newline(a):
    """A line break in a resource-pack setting must not add lines to server.properties."""
    p = Panel(a).start().login()
    try:
        d = p.add_fake()
        statuses = []
        for body in ({"resource_pack": "http://x/p.zip\nenable-rcon=true"}, {"resource_pack_sha1": "a" * 39 + "\n"},
                     {"resource_pack": "http://x/p.zip\renable-rcon=true"}):
            statuses.append(p.req("POST", "/api/fake/config", body)[0])
        props = open(os.path.join(d, "server.properties")).read() if os.path.exists(os.path.join(d, "server.properties")) else ""
        return all(s == 400 for s in statuses) and "enable-rcon=true" not in props, f"statuses {statuses}"
    finally:
        p.cleanup()


def t_zip_bomb(a):
    """Extraction must stop at the configured size cap instead of filling the disk."""
    p = Panel(a, cfg={"max_extract_mb": 50}).start().login()
    try:
        bomb = make_zip([("server.jar", b"0" * (200 << 20))])
        body, h = multipart([("zip", "big.zip", bomb)], {"id": "bomb", "directory": os.path.join(p.dir, "bombdir")})
        st, j = p.req("POST", "/api/import", body, h)
        res = p.job(j["job"]) if st == 200 and "job" in j else {"state": "rejected", "error": str(j)}
        size = 0
        for r, _, fs in os.walk(os.path.join(p.dir, "bombdir")):
            size += sum(os.path.getsize(os.path.join(r, f)) for f in fs)
        return res["state"] == "error" and size < (60 << 20), f"{res['state']}: {res.get('error')} ({size >> 20} MB written)"
    finally:
        p.cleanup()


def t_job_cap(a):
    """Only a bounded number of background tasks may run at once."""
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            time.sleep(15)

        def log_message(self, *_):
            pass
    web = ThreadingHTTPServer((HOST, 0), Slow)
    web.daemon_threads = True
    threading.Thread(target=web.serve_forever, daemon=True).start()
    p = Panel(a, cfg={"allow_private_fetch": True}).start().login()
    try:
        p.add_fake()
        codes = [p.req("POST", "/api/fake/rp_fetch", {"url": f"http://{HOST}:{web.server_address[1]}/p.zip"})[0] for _ in range(14)]
        return 429 in codes, f"response codes {codes}"
    finally:
        web.shutdown()
        p.cleanup()


def t_perms(a):
    """Config and backups must not be readable by other users, whatever the umask."""
    p = Panel(a, umask=0o022).start().login()
    try:
        p.add_fake()
        p.req("POST", "/api/auto_backup", {"enabled": False, "day": "monday", "hour": 1, "minute": 0})
        st, j = p.req("POST", "/api/fake/backup", {})
        r = p.job(j["job"])
        notes = []
        mode = lambda path: stat.S_IMODE(os.stat(path).st_mode)
        if mode(os.path.join(p.dir, "config.json")) & 0o077:
            notes.append("config.json is %o" % mode(os.path.join(p.dir, "config.json")))
        bk = os.path.join(p.dir, "bk")
        if mode(bk) & 0o077:
            notes.append("backup folder is %o" % mode(bk))
        if r["state"] == "done":
            z = os.path.join(bk, r["result"]["file"])
            if mode(z) & 0o077:
                notes.append("backup zip is %o" % mode(z))
        else:
            notes.append("backup failed: %s" % r.get("error"))
        return not notes, "; ".join(notes) or "config 600, backup folder 700, backup zip 600"
    finally:
        p.cleanup()


def t_csp(a):
    """The content-security policy must limit where scripts can send data."""
    p = Panel(a).start().login()
    try:
        st, hd, _ = p.req("GET", "/", raw=True)
        csp = hd.get("Content-Security-Policy", "")
        need = ["default-src 'self'", "connect-src 'self'", "img-src 'self' data:", "object-src 'none'", "frame-ancestors 'none'"]
        need_headers = ["Permissions-Policy", "Cross-Origin-Opener-Policy", "Cross-Origin-Resource-Policy", "X-Content-Type-Options", "X-Frame-Options"]
        missing = [n for n in need if n not in csp] + [h for h in need_headers if h not in hd]
        return not missing, ("missing: %s" % missing) if missing else "policy restricts connect/img/default sources"
    finally:
        p.cleanup()


def t_server_id(a):
    """A hand-edited config with an unsafe server id must not reach the page."""
    p = Panel(a)
    bad = "a');alert(1);//"
    p.write_cfg({"servers": {bad: {"name": "x", "directory": p.dir, "jar": "server.jar", "runtime": "native"},
                             "good": {"name": "ok", "directory": p.dir, "jar": "server.jar", "runtime": "native"}}})
    p.start().login()
    try:
        st, j = p.req("GET", "/api/status")
        ids = [s["id"] for s in j["servers"]]
        return bad not in ids and "good" in ids, f"ids shown: {ids}"
    finally:
        p.cleanup()


def t_totp_atomic(a):
    """One code can only ever open one session, even when sent many times at once."""
    p = Panel(a).start()
    try:
        code = totp(p.secret)
        res = []

        def go():
            c = http.client.HTTPConnection(HOST, p.port)
            c.request("POST", "/login", urllib.parse.urlencode({"username": "tester", "password": p.password, "totp": code}),
                      {"Content-Type": "application/x-www-form-urlencoded"})
            r = c.getresponse()
            r.read()
            res.append(r.status)
        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return res.count(303) == 1, f"statuses {sorted(res)}"
    finally:
        p.cleanup()


def t_ssrf_normal(a):
    """Internal addresses are refused in normal mode (guards against regressions in the fetch policy)."""
    web = ThreadingHTTPServer((HOST, 0), _Hits)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    _Hits.hits = 0
    p = Panel(a).start().login()
    try:
        p.add_fake()
        port = web.server_address[1]
        urls = [f"http://127.0.0.1:{port}/a.zip", f"http://localhost:{port}/a.zip", f"http://2130706433:{port}/a.zip",
                f"http://[::ffff:127.0.0.1]:{port}/a.zip", f"http://0.0.0.0:{port}/a.zip", "http://169.254.169.254/x"]
        bad = []
        for u in urls:
            st, j = p.req("POST", "/api/fake/rp_fetch", {"url": u})
            r = p.job(j["job"], 30) if st == 200 and "job" in j else {"state": "error", "error": "rejected"}
            if "private or internal" not in (r.get("error") or "") and r["state"] != "error":
                bad.append(u)
        return not bad and _Hits.hits == 0, f"not refused: {bad}; internal hits {_Hits.hits}"
    finally:
        web.shutdown()
        p.cleanup()


def t_lockout(a):
    """Wrong guesses from strangers must not lock the owner out of an account they already use."""
    p = Panel(a).start().login()          # the owner logs in from 127.0.0.1
    try:
        def attempt(source, password, code):
            s = socket.socket()
            s.bind((source, 0))
            s.settimeout(5)
            s.connect((HOST, p.port))
            form = urllib.parse.urlencode({"username": "tester", "password": password, "totp": code}).encode()
            s.sendall(b"POST /login HTTP/1.1\r\nHost: a\r\nContent-Type: application/x-www-form-urlencoded\r\nConnection: close\r\nContent-Length: "
                      + str(len(form)).encode() + b"\r\n\r\n" + form)
            out = b""
            try:
                while True:
                    d = s.recv(4096)
                    if not d:
                        break
                    out += d
            except OSError:
                pass
            s.close()
            return int(out.split(b" ", 2)[1]) if out.startswith(b"HTTP/") else 0
        for i in range(2, 6):                     # 4 stranger addresses x 4 wrong guesses = 16 failures for the user
            for _ in range(4):
                attempt(f"127.0.0.{i}", "wrong-password", "000000")
        time.sleep(31 - time.time() % 30)         # fresh one-time code (the login above already used the current one)
        owner = attempt("127.0.0.1", p.password, totp(p.secret))
        stranger = attempt("127.0.0.9", p.password, totp(p.secret))
        return owner == 303 and stranger == 429, f"owner's usual address -> {owner}, a new address -> {stranger} (want 303 and 429)"
    finally:
        p.cleanup()


def t_delete_guard(a):
    """Removing a server must never delete the home folder or the panel folder, even if one was typed as its folder."""
    p = Panel(a).start().login()
    try:
        keep = os.path.join(p.dir, "precious.txt")
        open(keep, "w").write("x")
        # a server whose folder is the panel's own folder (a typing mistake the UI would accept)
        st, j = p.req("POST", "/api/add", {"id": "oops", "name": "oops", "directory": p.dir, "jar": p.fake, "runtime": "native"})
        st, j = p.req("DELETE", "/api/oops")
        return os.path.exists(keep) and os.path.exists(os.path.join(p.dir, "config.json")), f"delete -> {st} {str(j)[:120]}"
    finally:
        p.cleanup()


def t_bad_input(a):
    """Malformed numbers and lengths get a clean 400, never a dropped connection or a hang."""
    p = Panel(a).start().login()
    try:
        p.add_fake()
        res = {}
        res["hour=abc"] = p.req("POST", "/api/auto_backup", {"enabled": True, "day": "monday", "hour": "abc", "minute": 0})[0]
        res["memory=x"] = p.req("POST", "/api/fake/config", {"memory_min_mb": "x"})[0]
        res["since=x"] = p.req("GET", "/api/fake/logs?since=notanumber")[0]
        res["CL=-5"] = int(p.raw(b"POST /login HTTP/1.1\r\nHost: a\r\nContent-Length: -5\r\nConnection: close\r\n\r\n", wait=3).split(b" ", 2)[1] or 0)
        res["CL=abc"] = int(p.raw(b"POST /login HTTP/1.1\r\nHost: a\r\nContent-Length: abc\r\nConnection: close\r\n\r\n", wait=3).split(b" ", 2)[1] or 0)
        alive = p.req("GET", "/api/status")[0] == 200
        ok = all(v in (200, 400) for k, v in res.items() if k in ("since=x",)) and res["hour=abc"] == 400 and res["memory=x"] == 400 \
            and res["CL=-5"] == 400 and res["CL=abc"] == 400 and alive
        return ok, f"{res}, panel alive: {alive}"
    finally:
        p.cleanup()


def t_upload_bytes(a):
    """An uploaded file must arrive byte for byte, including trailing newlines and binary data."""
    p = Panel(a).start().login()
    try:
        d = p.add_fake()
        data = {"a.txt": b"line\n", "b.txt": b"trail\r\n\r\n", "c.bin": b"\x00\x01\r\n", "d.txt": b"", "e.bin": os.urandom(100000)}
        body, h = multipart([("f", k, v) for k, v in data.items()])
        st, j = p.req("POST", "/api/fake/upload?path=", body, h)
        bad = [k for k, v in data.items() if not os.path.exists(os.path.join(d, k)) or open(os.path.join(d, k), "rb").read() != v]
        return st == 200 and not bad, f"status {st}, altered or missing: {bad}"
    finally:
        p.cleanup()


def t_tls(a):
    """With TLS on, only TLS 1.2+ with forward-secret AEAD suites may be negotiated."""
    import ssl
    p = Panel(a)
    k, c = os.path.join(p.dir, "k.pem"), os.path.join(p.dir, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", k, "-out", c, "-days", "2", "-subj", "/CN=localhost"],
                   capture_output=True)
    p.start(tls_cert=c, tls_key=k)
    try:
        def handshake(ciphers, version=None):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers(ciphers + ":@SECLEVEL=0")
            if version:
                ctx.minimum_version = ctx.maximum_version = version
            try:
                with socket.create_connection((HOST, p.port), timeout=5) as raw, ctx.wrap_socket(raw) as s:
                    return s.cipher()[0]
            except (ssl.SSLError, OSError):
                return None
        rsa = handshake("AES256-GCM-SHA384", ssl.TLSVersion.TLSv1_2)       # no forward secrecy
        cbc = handshake("ECDHE-RSA-AES256-SHA", ssl.TLSVersion.TLSv1_2)    # not authenticated encryption
        good = handshake("ECDHE-RSA-AES256-GCM-SHA384", ssl.TLSVersion.TLSv1_2)
        tls13 = handshake("DEFAULT", ssl.TLSVersion.TLSv1_3)
        ok = rsa is None and cbc is None and good and tls13
        return bool(ok), f"RSA key exchange {rsa}, CBC suite {cbc}, ECDHE-GCM {good}, TLS 1.3 {tls13}"
    finally:
        p.cleanup()


TESTS = {k[2:]: v for k, v in sorted(globals().items()) if k.startswith("t_")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("cpp", "python"), required=True)
    ap.add_argument("--cpp-bin", default=os.path.join(ROOT, "cpp", "creepercrest"))
    ap.add_argument("--python-script", default=os.path.join(ROOT, "python", "creepercrest.py"))
    ap.add_argument("--only", default="", help="comma separated test names")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        print("\n".join(TESTS))
        return 0
    names = [n for n in a.only.split(",") if n] or list(TESTS)
    fails = 0
    for n in names:
        try:
            ok, detail = TESTS[n](a)
        except Exception as e:   # a crashed test counts as a failure
            ok, detail = False, f"test error: {type(e).__name__}: {e}"
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {n:15s} {detail}", flush=True)
    print(f"\n{len(names) - fails}/{len(names)} passed ({a.backend})")
    return fails


if __name__ == "__main__":
    sys.exit(main())
