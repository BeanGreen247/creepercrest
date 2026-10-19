#include "app.h"
#include "assets.h"
#include "api.h"
#include "auth.h"
#include "proc.h"
#include "server.h"
#include "util.h"
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <algorithm>
#include <cstring>
#include <ctime>
#include <map>
#include <mutex>

namespace app {

Config cfg;

static const int64_t SESSION_IDLE = 8 * 3600, SESSION_MAX = 24 * 3600;
static const size_t MAX_UPLOAD = (size_t)2 << 30;

// ── config ──────────────────────────────────────────────────────────────────

static bool read_file(const std::string &path, std::string &out) {
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return false;
    char buf[8192];
    size_t n;
    out.clear();
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) out.append(buf, n);
    fclose(f);
    return true;
}

std::string expand_home(const std::string &p) {
    if (!p.empty() && p[0] == '~' && (p.size() == 1 || p[1] == '/')) {
        const char *h = getenv("HOME");
        return std::string(h ? h : "") + p.substr(1);
    }
    return p;
}

bool save_config() {
    std::string tmp = cfg.base_dir + "/config.json.tmp", data = json::dump(cfg.raw, 2);
    int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);   // private: it names every server folder
    if (fd < 0) return false;
    size_t off = 0;
    bool ok = true;
    while (off < data.size()) {
        ssize_t w = write(fd, data.data() + off, data.size() - off);
        if (w <= 0) { ok = false; break; }
        off += (size_t)w;
    }
    ok = (close(fd) == 0) && ok;
    return ok && rename(tmp.c_str(), (cfg.base_dir + "/config.json").c_str()) == 0;
}

static std::vector<std::string> str_list(const json::Value &v, const char *key) {
    std::vector<std::string> out;
    if (auto *a = v.find(key); a && a->is_arr())
        for (auto &x : a->a) if (x.is_str()) out.push_back(x.s);
    return out;
}

bool load_config(const std::string &base_dir) {
    cfg.base_dir = base_dir;
    std::string s, path = base_dir + "/config.json";
    if (!read_file(path, s)) {
        cfg.raw = json::Value::object();
        cfg.raw["host"] = "0.0.0.0";
        cfg.raw["port"] = 8080;
        cfg.raw["backup_dir"] = "~/mc-backups";
        cfg.raw["refresh_interval"] = 5;
        cfg.raw["servers"] = json::Value::object();
        save_config();
    } else {
        try { cfg.raw = json::parse(s); } catch (...) { fprintf(stderr, "config.json is not valid JSON\n"); return false; }
        if (!cfg.raw.is_obj()) return false;
    }
    auto &r = cfg.raw;
    cfg.host = r.str("host", "0.0.0.0");
    cfg.port = (int)r.num("port", 8080);
    cfg.refresh_interval = (int)r.num("refresh_interval", 5);
    cfg.lan_mode = r.flag("lan_mode");
    cfg.auth_disabled = r.flag("auth_disabled");
    cfg.trusted_proxies = str_list(r, "trusted_proxies");
    cfg.allowed_hosts = str_list(r, "allowed_hosts");
    cfg.tls_cert = expand_home(r.str("tls_cert"));
    cfg.tls_key = expand_home(r.str("tls_key"));
    return true;
}

// ── sessions and login throttle ──────────────────────────────────────────────

struct Session { std::string user, csrf; int64_t created, last; };
static std::mutex sess_mu;
static std::map<std::string, Session> sessions;
static std::map<std::string, std::vector<int64_t>> fails;
static const int FAIL_WINDOW = 900, FAIL_IP = 5, FAIL_USER = 10;
static const std::string OPEN_TOKEN = util::token_urlsafe(24);   // CSRF token for LAN / open mode

static int64_t now_s() { return (int64_t)time(nullptr); }

static std::vector<int64_t> &prune_fails(const std::string &key) {   // caller holds sess_mu
    auto &v = fails[key];
    int64_t cut = now_s() - FAIL_WINDOW;
    v.erase(std::remove_if(v.begin(), v.end(), [&](int64_t t) { return t <= cut; }), v.end());
    return v;
}

static bool login_throttled(const std::string &ip, const std::string &user) {
    std::lock_guard<std::mutex> g(sess_mu);
    return (int)prune_fails("ip:" + ip).size() >= FAIL_IP || (int)prune_fails("user:" + user).size() >= FAIL_USER;
}

static void record_failure(const std::string &ip, const std::string &user) {
    std::lock_guard<std::mutex> g(sess_mu);
    for (auto &k : {"ip:" + ip, "user:" + user}) prune_fails(k).push_back(now_s());
}

static std::string create_session(const std::string &user) {
    std::string tok = util::token_urlsafe(32);
    int64_t now = now_s();
    std::lock_guard<std::mutex> g(sess_mu);
    for (auto it = sessions.begin(); it != sessions.end();)
        it = (now - it->second.created > SESSION_MAX || now - it->second.last > SESSION_IDLE) ? sessions.erase(it) : std::next(it);
    sessions[tok] = {user, util::token_urlsafe(24), now, now};
    return tok;
}

static bool get_session(const std::string &tok, Session &out) {
    if (tok.empty()) return false;
    int64_t now = now_s();
    std::lock_guard<std::mutex> g(sess_mu);
    auto it = sessions.find(tok);
    if (it == sessions.end()) return false;
    if (now - it->second.created > SESSION_MAX || now - it->second.last > SESSION_IDLE) { sessions.erase(it); return false; }
    it->second.last = now;
    out = it->second;
    return true;
}

static void drop_session(const std::string &tok) {
    std::lock_guard<std::mutex> g(sess_mu);
    sessions.erase(tok);
}

// ── pages ────────────────────────────────────────────────────────────────────

static const char *AUTH_CSS =
    "*{box-sizing:border-box;margin:0}body{background:#0d1117;color:#c9d1d9;font-family:system-ui,sans-serif;\n"
    "min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem}\n"
    ".card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:1.8rem;width:min(400px,94vw);box-shadow:0 12px 36px rgba(0,0,0,.5)}\n"
    "h1{font-size:1.1rem;color:#f0f6fc;margin-bottom:1.2rem}label{display:block;font-size:.78rem;color:#7d8590;margin:.8rem 0 .3rem}\n"
    "input{width:100%;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;padding:.55rem .7rem;font-size:.9rem}\n"
    "input:focus{outline:none;border-color:#58a6ff}.totp{letter-spacing:.25em;text-align:center;font-size:1.1rem}\n"
    "button{width:100%;margin-top:1.2rem;background:#238636;color:#fff;border:0;border-radius:8px;padding:.6rem;font-weight:600;cursor:pointer}\n"
    ".err{background:#3d1616;color:#f85149;border:1px solid #8b1a1a;border-radius:8px;padding:.5rem .7rem;font-size:.82rem;margin-bottom:.4rem}\n"
    "p{font-size:.85rem;line-height:1.5;color:#8b949e}code{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:.15rem .4rem;color:#c9d1d9;word-break:break-all}\n"
    "a{color:#58a6ff}";

static const char *ICON =
    "<link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%233eae30'/%3E%3C/svg%3E\">";

static std::string page(const std::string &title, const std::string &body) {
    return std::string("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">") +
           ICON + "<title>" + title + "</title><style>" + AUTH_CSS + "</style></head><body>" + body + "</body></html>";
}

static std::string render_login(const std::string &error = "") {
    std::string err = error.empty() ? "" : "<div class=\"err\">" + util::html_escape(error) + "</div>";
    return page("Creeper Crest - Sign in",
        "<form class=\"card\" method=\"post\" action=\"/login\" autocomplete=\"off\">\n<h1>Creeper Crest <span style=\"font-size:.65rem;font-weight:600;padding:.1rem .45rem;border-radius:999px;color:#3fb950;border:1px solid #1f6b30;background:#0d2615;vertical-align:middle;margin-left:.4rem\">C++ BACKEND</span></h1>" + err +
        "\n<label for=\"u\">Username</label><input id=\"u\" name=\"username\" autocomplete=\"username\" autofocus required>"
        "\n<label for=\"p\">Password</label><input id=\"p\" name=\"password\" type=\"password\" autocomplete=\"current-password\" required>"
        "\n<label for=\"t\">Authenticator code</label><input id=\"t\" name=\"totp\" class=\"totp\" inputmode=\"numeric\" maxlength=\"7\" placeholder=\"000000\" autocomplete=\"one-time-code\" required>"
        "\n<button type=\"submit\">Sign in</button></form>");
}

static std::string render_setup_required() {
    return page("Creeper Crest - Setup required",
        "<div class=\"card\"><h1>Setup required</h1>\n"
        "<p>No users exist yet, so the panel is locked. On the server, create a login (it prints a generated password and a 2FA secret):</p>\n"
        "<p style=\"margin-top:.8rem\"><code>creepercrest adduser yourname</code></p>\n"
        "<p style=\"margin-top:.8rem\">Then reload this page.</p></div>");
}

// The page itself never contains the authenticator secret: a stolen session cookie alone must not be enough to read it.
// The secret is fetched with POST /api/2fa after the password is typed again.
static std::string render_2fa_setup(const std::string &user, const std::string &csrf) {
    if (!auth::load_users().find(user)) return render_setup_required();
    return page("Creeper Crest - 2FA",
        "<div class=\"card\"><h1>Two-factor authentication</h1>\n"
        "<p style=\"color:#3fb950;font-weight:600\">2FA is active for " + util::html_escape(user) + "</p>\n"
        "<div id=\"gate\"><p style=\"margin:.8rem 0\">To add this account to an authenticator app, confirm your password.</p>"
        "<label for=\"pw\">Password</label><input id=\"pw\" type=\"password\" autocomplete=\"current-password\">"
        "<button id=\"go\" type=\"button\">Show setup key</button><p id=\"msg\" style=\"color:#f85149;margin-top:.6rem\"></p></div>"
        "<div id=\"key\" style=\"display:none\"></div>"
        "<p style=\"margin-top:1rem\"><a href=\"/\">Back to panel</a></p></div>"
        "<script>const CSRF='" + csrf + "';"
        "const q=id=>document.getElementById(id);"
        "async function show(){q('msg').textContent='';"
        "const r=await fetch('/api/2fa',{method:'POST',headers:{'Content-Type':'application/json','X-CC-CSRF':CSRF},body:JSON.stringify({password:q('pw').value})});"
        "const j=await r.json();q('pw').value='';"
        "if(!r.ok){q('msg').textContent=j.error||'Failed';return}"
        "q('gate').style.display='none';const k=q('key');k.style.display='';k.textContent='';"
        "const add=(tag,text,css)=>{const e=document.createElement(tag);e.textContent=text;if(css)e.style.cssText=css;k.appendChild(e);return e};"
        "if(j.qr){const i=document.createElement('img');i.src='data:image/svg+xml;base64,'+j.qr;i.width=200;i.height=200;i.alt='TOTP QR code';i.style.cssText='display:block;background:#fff;padding:8px;border-radius:6px';k.appendChild(i)}"
        "const a=add('p','Enter the setup key manually, or open this link on your phone: ');const l=document.createElement('a');l.textContent='add to authenticator';l.href=j.uri;a.appendChild(l);"
        "add('label','Setup key (manual entry)');add('code',j.secret,'display:block;padding:.5rem')}"
        "q('go').addEventListener('click',show);q('pw').addEventListener('keydown',e=>{if(e.key==='Enter')show()});</script>");
}

static std::string acct_html(const std::string &user) {
    if (!user.empty() && user[0] == '(') return "<span style=\"color:#e3b341\">LAN mode &middot; no login</span>";
    return util::html_escape(user) + " &middot; <a href=\"/2fa-setup\" style=\"color:#58a6ff\">2FA</a> &middot; "
           "<a href=\"#\" onclick=\"logout();return false\" style=\"color:#58a6ff\">Logout</a>";
}

std::string default_jvm_args() { return srvutil::default_jvm_args(); }

static void replace_all(std::string &s, const std::string &from, const std::string &to) {
    for (size_t p = 0; (p = s.find(from, p)) != std::string::npos; p += to.size()) s.replace(p, from.size(), to);
}

static std::string render_index(const Session &s) {
    std::string h(ui_index_html, ui_index_html_end);
    replace_all(h, "__REFRESH_INTERVAL__", std::to_string(cfg.refresh_interval));
    replace_all(h, "__JVM_ARGS__", default_jvm_args());
    replace_all(h, "__CSRF__", s.csrf);
    replace_all(h, "__ACCT__", acct_html(s.user));
    replace_all(h, "__BACKEND__",
                "<span title=\"Native C++ backend, built " __DATE__ " " __TIME__ "\" style=\"font-size:.68rem;font-weight:600;letter-spacing:.03em;padding:.12rem .5rem;"
                "border-radius:999px;color:#3fb950;border:1px solid #1f6b30;background:#0d2615;white-space:nowrap\">C++ BACKEND</span>");
    replace_all(h, "<title>CreeperCrest</title>", "<title>CreeperCrest (C++)</title>");
    return h;
}

// ── request plumbing ─────────────────────────────────────────────────────────

static bool tls_on() { return !cfg.tls_cert.empty() && !cfg.tls_key.empty(); }
static bool https_req(const http::Request &rq) { return tls_on() || rq.hdr("x-forwarded-proto") == "https"; }

void security_headers(const http::Request &rq, http::Response &rs) {
    rs.set("X-Content-Type-Options", "nosniff");
    rs.set("X-Frame-Options", "DENY");
    rs.set("Referrer-Policy", "same-origin");
    rs.set("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
    if (https_req(rq)) rs.set("Strict-Transport-Security", "max-age=31536000");
}

void send_json(const http::Request &rq, http::Response &rs, const json::Value &v, int code) {
    rs.code = code;
    rs.body = json::dump(v);
    rs.set("Content-Type", "application/json");
    rs.set("Cache-Control", "no-store");
    security_headers(rq, rs);
}

static void send_html(const http::Request &rq, http::Response &rs, const std::string &html, int code = 200) {
    rs.code = code;
    rs.body = html;
    rs.set("Content-Type", "text/html; charset=utf-8");
    rs.set("Cache-Control", "no-store");
    security_headers(rq, rs);
}

void err_json(const http::Request &rq, http::Response &rs, const std::string &msg, int code) {
    json::Value v = json::Value::object();
    v["error"] = msg;
    send_json(rq, rs, v, code);
}

static void redirect(const http::Request &rq, http::Response &rs, const std::string &loc, const std::string &cookie = "") {
    rs.code = 303;
    rs.set("Location", loc);
    if (!cookie.empty()) rs.set("Set-Cookie", cookie);
    security_headers(rq, rs);
}

static std::string session_cookie(const http::Request &rq, const std::string &value, int64_t max_age) {
    std::string c = "cc_session=" + value + "; HttpOnly; SameSite=Strict; Path=/";
    if (https_req(rq)) c += "; Secure";
    return c + "; Max-Age=" + std::to_string(max_age);
}

static bool in_list(const std::vector<std::string> &l, const std::string &s) {
    return std::find(l.begin(), l.end(), s) != l.end();
}

std::string client_ip(const http::Request &rq) {
    std::string xff = rq.hdr("x-forwarded-for");
    if (in_list(cfg.trusted_proxies, rq.peer) && !xff.empty()) {
        std::vector<std::string> hops;
        size_t pos = 0;
        while (pos <= xff.size()) {
            size_t c = xff.find(',', pos);
            std::string h = xff.substr(pos, c == std::string::npos ? c : c - pos);
            size_t a = h.find_first_not_of(' '), b = h.find_last_not_of(' ');
            hops.push_back(a == std::string::npos ? "" : h.substr(a, b - a + 1));
            if (c == std::string::npos) break;
            pos = c + 1;
        }
        for (auto it = hops.rbegin(); it != hops.rend(); ++it)
            if (!in_list(cfg.trusted_proxies, *it)) return *it;
    }
    return rq.peer;
}

static bool private_addr(const std::string &ip) {
    in_addr a4;
    in6_addr a6;
    if (inet_pton(AF_INET, ip.c_str(), &a4) == 1) {
        uint32_t v = ntohl(a4.s_addr);
        return (v >> 24) == 10 || (v >> 24) == 127 || (v >> 20) == 0xAC1 || (v >> 16) == 0xC0A8 || (v >> 16) == 0xA9FE;
    }
    if (inet_pton(AF_INET6, ip.c_str(), &a6) == 1) {
        static const unsigned char lo[16] = {0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1};
        return !memcmp(&a6, lo, 16) || (a6.s6_addr[0] & 0xFE) == 0xFC || (a6.s6_addr[0] == 0xFE && (a6.s6_addr[1] & 0xC0) == 0x80);
    }
    return false;
}

static bool lan_open(const http::Request &rq) {
    if (!cfg.lan_mode) return false;
    for (auto h : {"x-forwarded-for", "forwarded", "x-real-ip", "via", "cf-connecting-ip"})
        if (!rq.hdr(h).empty()) return false;
    if (!private_addr(rq.peer)) return false;
    std::string host = rq.hdr("host");
    if (!host.empty() && host[0] == '[') { size_t e = host.find(']'); host = host.substr(1, e == std::string::npos ? e : e - 1); }
    else { size_t c = host.rfind(':'); if (c != std::string::npos && host.find(':') == c) host.resize(c); }
    for (auto &c : host) c = (char)tolower((unsigned char)c);
    char me[256] = "";
    gethostname(me, sizeof me - 1);
    std::string mine = me;
    for (auto &c : mine) c = (char)tolower((unsigned char)c);
    if (host == "localhost" || host == mine || (host.size() > 6 && host.compare(host.size() - 6, 6, ".local") == 0)) return true;
    for (auto &h : cfg.allowed_hosts) {
        std::string l = h;
        for (auto &c : l) c = (char)tolower((unsigned char)c);
        if (l == host) return true;
    }
    in_addr a4; in6_addr a6;
    return inet_pton(AF_INET, host.c_str(), &a4) == 1 || inet_pton(AF_INET6, host.c_str(), &a6) == 1;
}

// Values that came from a request (user names, paths) may contain line breaks or terminal escapes: show them
// escaped so nobody can forge a log line.
static std::string log_safe(const std::string &in) {
    std::string out;
    for (unsigned char c : in) {
        if (c < 0x20 || c == 0x7f) { char b[8]; snprintf(b, sizeof b, "\\x%02x", c); out += b; }
        else out += (char)c;
        if (out.size() > 300) { out += "..."; break; }
    }
    return out;
}

static void log_line(const char *tag, const std::string &msg) { printf("[%s] %s\n", tag, log_safe(msg).c_str()); fflush(stdout); }

static const char *DUMMY_TOTP = "JBSWY3DPEHPK3PXP";
static const std::string &dummy_hash() {
    static const std::string h = auth::hash_password("creepercrest-dummy");   // equalises timing for unknown users
    return h;
}

static void login_attempt(const http::Request &rq, http::Response &rs) {
    std::string ip = client_ip(rq);
    if (rq.body_len > 4096) return send_html(rq, rs, render_login("Invalid request."), 400);
    auto form = rq.form();
    std::string user = form["username"];
    while (!user.empty() && isspace((unsigned char)user.back())) user.pop_back();
    user.erase(0, user.find_first_not_of(" \t\r\n"));
    for (auto &c : user) c = (char)tolower((unsigned char)c);
    if (user.size() > 64) user.resize(64);
    if (login_throttled(ip, user)) {
        log_line("auth", "LOCKED OUT login attempt user='" + user + "' ip=" + ip);
        return send_html(rq, rs, render_login("Too many failed attempts. Try again in 15 minutes."), 429);
    }
    json::Value users = auth::load_users();
    const json::Value *rec = users.find(user);
    bool pw_ok = auth::verify_password(form["password"], rec ? rec->str("pw") : dummy_hash());
    uint64_t step = auth::verify_totp(rec ? rec->str("totp") : DUMMY_TOTP, form["totp"], rec ? user : "");
    if (rec && pw_ok && step && auth::consume_totp(user, step)) {
        log_line("auth", "login ok user=" + user + " ip=" + ip);
        return redirect(rq, rs, "/", session_cookie(rq, create_session(user), SESSION_MAX));
    }
    record_failure(ip, user);
    log_line("auth", "login FAILED user='" + user + "' ip=" + ip);
    send_html(rq, rs, render_login("Invalid username, password or authenticator code."), 401);
}

// Returns true when the request was fully answered by the gate.
static bool gate(http::Request &rq, http::Response &rs, Session &sess) {
    const std::string &method = rq.method;
    sess = {"(auth disabled)", "", 0, 0};
    if (method == "POST" && rq.body_len > MAX_UPLOAD) { err_json(rq, rs, "request too large", 413); return true; }
    if (method == "GET" && rq.path == "/favicon.ico") { rs.code = 204; return true; }
    if (method == "GET" && rq.path.compare(0, 14, "/resourcepack/") == 0) return false;   // Minecraft clients cannot log in
    std::string origin = rq.hdr("origin");
    if ((method == "POST" || method == "DELETE") && !origin.empty()) {
        size_t s = origin.find("://");
        std::string netloc = s == std::string::npos ? origin : origin.substr(s + 3);
        netloc = netloc.substr(0, netloc.find('/'));
        if (netloc != rq.hdr("host")) { err_json(rq, rs, "cross-origin request refused", 403); return true; }
    }
    bool open_mode = cfg.auth_disabled || lan_open(rq);
    std::string tok = rq.cookie("cc_session");
    if (rq.path == "/login") {
        Session tmp;
        if (open_mode) redirect(rq, rs, "/");
        else if (method == "POST") login_attempt(rq, rs);
        else if (get_session(tok, tmp)) redirect(rq, rs, "/");
        else {
            bool any = !auth::load_users().o.empty();
            send_html(rq, rs, any ? render_login() : render_setup_required());
        }
        return true;
    }
    if (open_mode) sess = {"(lan)", OPEN_TOKEN, 0, 0};
    else {
        json::Value users = auth::load_users();
        bool ok = get_session(tok, sess);
        if (ok && !users.find(sess.user)) { drop_session(tok); ok = false; }
        if (!ok) {
            if (users.o.empty()) send_html(rq, rs, render_setup_required(), 503);
            else if (rq.path.compare(0, 5, "/api/") == 0 || method != "GET") err_json(rq, rs, "login required", 401);
            else redirect(rq, rs, "/login");
            return true;
        }
    }
    if (method == "POST" || method == "DELETE") {
        if (!util::ct_equal(rq.hdr("x-cc-csrf"), sess.csrf)) { err_json(rq, rs, "bad CSRF token - reload the page", 403); return true; }
        log_line("audit", sess.user + " " + method + " " + rq.path + " from " + client_ip(rq));
    }
    return false;
}

bool body_gate(const http::Request &rq, http::Response &rs) {
    if (rq.method == "GET" || rq.method == "HEAD") return true;
    if (cfg.auth_disabled || lan_open(rq)) return true;
    Session s;
    if (get_session(rq.cookie("cc_session"), s) && auth::load_users().find(s.user)) return true;
    err_json(rq, rs, "login required", 401);
    return false;
}

void handle(http::Request &rq, http::Response &rs) {
    Session sess;
    if (rq.method != "GET" && rq.method != "POST" && rq.method != "DELETE" && rq.method != "HEAD") {
        rs.code = 405; rs.set("Allow", "GET, HEAD, POST, DELETE"); return;
    }
    if (rq.method == "HEAD") {   // resource pack only
        if (!api::handle_head(rq, rs)) { rs.code = 405; rs.set("Content-Length", "0"); }
        return;
    }
    if (gate(rq, rs, sess)) return;
    const std::string &p = rq.path;
    if (rq.method == "GET") {
        if (p == "/") return send_html(rq, rs, render_index(sess));
        if (p == "/static/editor.js") {
            rs.code = 200;
            rs.body.assign(ui_editor_js_gz, ui_editor_js_gz_end);
            rs.set("Content-Type", "application/javascript; charset=utf-8");
            rs.set("Content-Encoding", "gzip");
            rs.set("Cache-Control", "private, max-age=3600");
            security_headers(rq, rs);
            return;
        }
        if (p == "/2fa-setup") return send_html(rq, rs, render_2fa_setup(sess.user, sess.csrf));
    }
    if (rq.method == "POST" && p == "/api/2fa") {
        std::string ip = client_ip(rq);
        if (sess.user.empty() || sess.user[0] == '(') return err_json(rq, rs, "No account is signed in (LAN mode)", 400);
        if (login_throttled(ip, sess.user)) return err_json(rq, rs, "Too many failed attempts. Try again in 15 minutes.", 429);
        std::string pw;
        try { pw = json::parse(rq.body).str("password"); } catch (...) {}
        json::Value users = auth::load_users();
        const json::Value *rec = users.find(sess.user);
        if (!rec || !auth::verify_password(pw, rec->str("pw"))) {
            record_failure(ip, sess.user);
            log_line("auth", "2FA key request FAILED user='" + sess.user + "' ip=" + ip);
            return err_json(rq, rs, "Wrong password", 403);
        }
        std::string secret = rec->str("totp"), uri = auth::totp_uri(sess.user, secret), svg;
        json::Value v = json::Value::object();
        v["secret"] = secret;
        v["uri"] = uri;
        if (proc::run_capture({"qrencode", "-t", "SVG", "-m", "1", "-o", "-", uri}, &svg, nullptr, 5000) == 0 && !svg.empty()) v["qr"] = util::b64_encode(svg);
        else v["qr"] = "";
        log_line("auth", "2FA key shown to user=" + sess.user + " ip=" + ip);
        return send_json(rq, rs, v);
    }
    if (rq.method == "POST" && p == "/logout") {
        drop_session(rq.cookie("cc_session"));
        json::Value v = json::Value::object();
        v["ok"] = true;
        send_json(rq, rs, v);
        rs.set("Set-Cookie", session_cookie(rq, "", 0));
        return;
    }
    if (api::handle(rq, rs, client_ip(rq))) return;
    err_json(rq, rs, "not found", 404);
}

} // namespace app
