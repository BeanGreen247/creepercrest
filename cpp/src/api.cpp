// Authenticated JSON/file API consumed by the web UI. Route shapes match the original Python panel.
#include "api.h"
#include "app.h"
#include "backup.h"
#include "fsu.h"
#include "jars.h"
#include "jobs.h"
#include "multipart.h"
#include "proc.h"
#include "registry.h"
#include "server.h"
#include "sysinfo.h"
#include "util.h"
#include "zipx.h"
#include <arpa/inet.h>
#include <dirent.h>
#include <openssl/evp.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#include <algorithm>
#include <cstring>
#include <regex>
#include <thread>

using json::Value;
using http::Request;
using http::Response;

namespace api {

namespace {

const size_t EDIT_MAX = 512 * 1024;
const size_t RP_MAX_BYTES = (size_t)250 * 1024 * 1024;

struct Bad : std::runtime_error { int code; Bad(const std::string &m, int c = 400) : std::runtime_error(m), code(c) {} };

void ok(const Request &rq, Response &rs, const Value &v, int code = 200) { app::send_json(rq, rs, v, code); }
void fail(const Request &rq, Response &rs, const std::string &m, int code = 400) { app::err_json(rq, rs, m, code); }

Value obj() { return Value::object(); }

Value body_json(const Request &rq) {
    if (rq.body_fd >= 0 || rq.body.size() > http::kMemBody) throw Bad("request body too large");
    if (rq.body.empty()) return obj();
    try {
        Value v = json::parse(rq.body);
        return v.is_obj() ? v : obj();
    } catch (...) { throw Bad("invalid JSON body"); }
}

int64_t to_int(const Value *v, int64_t def) {
    if (!v) return def;
    if (v->type == Value::Int) return v->i;
    if (v->type == Value::Real) return (int64_t)v->d;
    if (v->type == Value::Bool) return v->b;
    if (v->type == Value::Str) {
        char *e;
        long long n = strtoll(v->s.c_str(), &e, 10);
        if (!v->s.empty() && !*e) return n;
        throw Bad("invalid number: " + v->s);
    }
    return def;
}

std::string trim(const std::string &s) {
    size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
}
std::string lower(std::string s) { for (auto &c : s) c = (char)tolower((unsigned char)c); return s; }

std::vector<std::string> segs(const std::string &path) {
    std::vector<std::string> v;
    size_t i = 0;
    while (i < path.size()) {
        size_t j = path.find('/', i);
        if (j == std::string::npos) j = path.size();
        if (j > i) v.push_back(path.substr(i, j - i));
        i = j + 1;
    }
    return v;
}

std::string qarg(const Request &rq, const char *k, const std::string &def = "") {
    auto m = rq.query_args();
    auto it = m.find(k);
    return it == m.end() ? def : it->second;
}

bool sid_ok(const std::string &s) {
    static const std::regex re("^[a-z0-9][a-z0-9-]{0,31}$");
    return std::regex_match(s, re);
}

std::string norm_sid(const std::string &raw) {
    std::string s = lower(trim(raw));
    for (auto &c : s) if (c == ' ') c = '-';
    return s;
}

bool valid_utf8(const std::string &s) {
    size_t i = 0, n = s.size();
    while (i < n) {
        unsigned char c = s[i];
        int len = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
        if (!len || i + len > n) return false;
        for (int k = 1; k < len; k++) if (((unsigned char)s[i + k] & 0xC0) != 0x80) return false;
        if (len == 2 && c < 0xC2) return false;
        if (len == 3) {
            unsigned cp = ((c & 15) << 12) | (((unsigned char)s[i + 1] & 63) << 6) | ((unsigned char)s[i + 2] & 63);
            if (cp < 0x800 || (cp >= 0xD800 && cp < 0xE000)) return false;
        }
        if (len == 4) {
            unsigned cp = ((c & 7) << 18) | (((unsigned char)s[i + 1] & 63) << 12) | (((unsigned char)s[i + 2] & 63) << 6) | ((unsigned char)s[i + 3] & 63);
            if (cp < 0x10000 || cp > 0x10FFFF) return false;
        }
        i += len;
    }
    return true;
}

void send_file(const Request &rq, Response &rs, const std::string &path, const char *ctype, const std::string &filename, bool unlink_after = false) {
    rs.code = 200;
    rs.file = path;
    rs.file_unlink = unlink_after;
    rs.set("Content-Type", ctype);
    if (!filename.empty()) {
        std::string safe;
        for (unsigned char c : filename) safe += (isalnum(c) || c == '_' || c == '.' || c == ' ' || c == '-') ? (char)c : '_';
        if (safe.size() > 150) safe.resize(150);
        if (safe.empty()) safe = "download";
        rs.set("Content-Disposition", "attachment; filename=\"" + safe + "\"");
    }
    app::security_headers(rq, rs);
}

std::string lan_ip() {
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return "127.0.0.1";
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(1);
    inet_pton(AF_INET, "10.255.255.255", &a.sin_addr);
    std::string out = "127.0.0.1";
    if (connect(s, (sockaddr *)&a, sizeof a) == 0) {
        sockaddr_in me{};
        socklen_t l = sizeof me;
        if (getsockname(s, (sockaddr *)&me, &l) == 0) { char b[32]; inet_ntop(AF_INET, &me.sin_addr, b, sizeof b); out = b; }
    }
    close(s);
    return out;
}

std::string pack_base(const std::string &host) {
    std::string pub = registry::cfg_str("public_url");
    if (!pub.empty()) { while (!pub.empty() && pub.back() == '/') pub.pop_back(); return pub; }
    std::string name = host, port;
    if (host.empty() || host[0] != '[') {
        size_t c = host.find(':');
        if (c != std::string::npos) { name = host.substr(0, c); port = host.substr(c + 1); }
    }
    if (name == "localhost" || name == "127.0.0.1" || name == "[::1]") name = lan_ip();
    return "http://" + name + (port.empty() ? "" : ":" + port);
}

std::string sha1_hex(const std::string &path) {
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return "";
    EVP_MD_CTX *c = EVP_MD_CTX_new();
    EVP_DigestInit_ex(c, EVP_sha1(), nullptr);
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) EVP_DigestUpdate(c, buf, n);
    fclose(f);
    unsigned char md[20];
    unsigned len = 0;
    EVP_DigestFinal_ex(c, md, &len);
    EVP_MD_CTX_free(c);
    std::string h;
    for (unsigned i = 0; i < len; i++) { char b[3]; snprintf(b, sizeof b, "%02x", md[i]); h += b; }
    return h;
}

std::string rp_path(ManagedServer &s) { return s.directory() + "/resource-pack.zip"; }

// Validates the zip at `tmp`, moves it into the server dir, points server.properties at it. Throws Bad.
std::string install_pack(const std::shared_ptr<ManagedServer> &srv, const std::string &tmp, const std::string &host) {
    int64_t sz = fsu::file_size(tmp);
    if (sz > (int64_t)RP_MAX_BYTES) throw Bad("Pack larger than 250 MB");
    if (!zipx::is_zip(tmp)) throw Bad("Not a valid zip file");
    if (!zipx::has_entry(tmp, "pack.mcmeta")) throw Bad("Not a resource pack (pack.mcmeta missing at zip root)");
    std::string dir = srv->directory();
    if (dir.empty() || !fsu::is_dir(dir)) throw Bad("Server directory not found");
    std::string sha = sha1_hex(tmp);
    if (rename(tmp.c_str(), rp_path(*srv).c_str()) != 0) throw Bad("cannot store the pack");
    Value c = srv->cfg();
    c["resource_pack"] = pack_base(host) + "/resourcepack/" + srv->id + ".zip";
    c["resource_pack_sha1"] = sha;
    try { srvutil::apply_resource_pack(c); } catch (const std::exception &e) { throw Bad(e.what()); }
    srv->set_cfg(c);
    registry::persist(srv->id, c);
    srv->append("[CreeperCrest] Resource pack installed (" + std::to_string(sz / 1024) + " KB), hosted at " + c.str("resource_pack"));
    return c.str("resource_pack");
}

Value rp_result(ManagedServer &s, const std::string &url) {
    Value v = obj();
    v["msg"] = url;
    v["sha1"] = s.cfg().str("resource_pack_sha1");
    return v;
}

Value new_server_cfg(const Value &b, const std::string &sid) {
    int64_t legacy = std::max<int64_t>(256, to_int(b.find("memory_mb"), 1024));
    Value c = obj();
    std::string name = trim(b.str("name", sid)), jar = trim(b.str("jar", "server.jar"));
    c["name"] = name.empty() ? sid : name;
    c["directory"] = trim(b.str("directory"));
    c["jar"] = jar.empty() ? "server.jar" : jar;
    c["runtime"] = b.str("runtime") == "native" ? "native" : "java";
    c["memory_min_mb"] = std::max<int64_t>(256, to_int(b.find("memory_min_mb"), legacy));
    c["memory_max_mb"] = std::max<int64_t>(256, to_int(b.find("memory_max_mb"), legacy));
    c["extra_args"] = trim(b.str("extra_args"));
    c["autostart"] = false;
    if (c.num("memory_min_mb") > c.num("memory_max_mb")) throw Bad("max RAM must be >= min RAM");
    return c;
}

void check_new_id(const std::string &sid) {
    if (sid.empty()) throw Bad("id required");
    if (!sid_ok(sid)) throw Bad("id: lowercase letters, digits and dashes only (max 32)");
    if (registry::get(sid)) throw Bad("id \"" + sid + "\" already exists");
}

// ── directory picker ─────────────────────────────────────────────────────────

Value list_dirs(const std::string &in, bool files) {
    std::string path = fsu::normalize(fsu::expand_home(in.empty() ? "~" : in));
    if (!fsu::is_dir(path)) throw Bad("not a directory");
    std::vector<std::string> dirs, fl;
    for (auto &n : fsu::list_names(path)) {
        if (n[0] == '.') continue;
        std::string full = path + "/" + n;
        if (fsu::is_dir(full)) dirs.push_back(n);
        else if (files && fsu::is_file(full)) fl.push_back(n);
    }
    auto cmp = [](const std::string &a, const std::string &b) { return lower(a) < lower(b); };
    std::sort(dirs.begin(), dirs.end(), cmp);
    std::sort(fl.begin(), fl.end(), cmp);
    Value o = obj();
    o["path"] = path;
    o["parent"] = path == "/" ? Value() : Value(fsu::dir_name(path));
    Value d = Value::array();
    for (auto &n : dirs) d.push(n);
    o["dirs"] = d;
    const char *h = getenv("HOME");
    o["home"] = std::string(h ? h : "");
    if (files) {
        Value f = Value::array();
        for (size_t i = 0; i < fl.size() && i < 2000; i++) {
            Value e = obj();
            e["n"] = fl[i];
            e["x"] = fsu::is_executable(path + "/" + fl[i]);
            f.push(e);
        }
        o["files"] = f;
    }
    return o;
}

// ── GET ──────────────────────────────────────────────────────────────────────

bool do_get(Request &rq, Response &rs, const std::vector<std::string> &p, const std::string &ip) {
    if (p.size() == 2 && p[0] == "api" && p[1] == "status") {
        Value v = obj();
        Value servers = Value::array();
        std::vector<int> live;
        for (auto &s : registry::all()) {
            Value st = s->status();
            if (st.flag("running")) live.push_back((int)st.num("pid"));
            servers.push(st);
        }
        sysinfo::forget_except(live);
        v["backend"] = "cpp";
        v["servers"] = servers;
        v["backups"] = backup::list();
        v["backup_dir"] = backup::dir();
        v["sysinfo"] = sysinfo::host();
        Value ab = registry::cfg_get("auto_backup");
        if (!ab.is_obj()) { ab = obj(); ab["enabled"] = false; ab["day"] = "sunday"; ab["hour"] = 3; ab["minute"] = 0; }
        v["auto_backup"] = ab;
        v["max_backups"] = registry::cfg_num("max_backups", 0);
        ok(rq, rs, v);
        return true;
    }
    if (p.size() == 3 && p[0] == "api" && p[1] == "job") {
        Value v;
        if (!jobs::view(p[2], v)) fail(rq, rs, "job not found", 404);
        else ok(rq, rs, v);
        return true;
    }
    if (p.size() == 2 && p[0] == "api" && p[1] == "browse") {
        try { ok(rq, rs, list_dirs(qarg(rq, "path", "~"), qarg(rq, "files", "0") == "1")); }
        catch (const Bad &e) { fail(rq, rs, e.what(), 400); }
        return true;
    }
    if (p.size() == 2 && p[0] == "api" && p[1] == "java") {
        std::string ver = qarg(rq, "version");
        static const std::regex re("^[A-Za-z0-9._-]+$");
        int req = std::regex_match(ver, re) ? jars::required_java(ver) : 0;
        int host = srvutil::host_java_major();
        Value v = obj();
        v["host"] = host ? Value(host) : Value();
        v["required"] = req ? Value(req) : Value();
        ok(rq, rs, v);
        return true;
    }
    if (p.size() == 2 && p[0] == "api" && p[1] == "jar_versions") {
        try {
            Value a = Value::array();
            for (auto &s : jars::versions(qarg(rq, "type"))) a.push(s);
            Value v = obj();
            v["versions"] = a;
            ok(rq, rs, v);
        } catch (const std::exception &e) { fail(rq, rs, std::string("could not load versions: ") + e.what(), 502); }
        return true;
    }
    if (p.size() == 2 && p[0] == "backups") {
        std::string f = backup::path_of(http::url_decode(p[1], false));
        if (f.empty() || !fsu::is_file(f)) fail(rq, rs, "not found", 404);
        else send_file(rq, rs, f, "application/zip", fsu::base_name(f));
        return true;
    }
    if (p.size() == 3 && p[0] == "api") {
        auto srv = registry::get(p[1]);
        const std::string &act = p[2];
        if (act != "logs" && act != "whitelist" && act != "files" && act != "filetext" && act != "file") return false;
        if (!srv) { fail(rq, rs, "not found", 404); return true; }
        if (act == "logs") {
            long since = 0;
            try { since = std::stol(qarg(rq, "since", "0")); } catch (...) {}
            std::vector<std::string> lines;
            long seq;
            bool full;
            srv->logs_since(since, lines, seq, full);
            Value v = obj(), a = Value::array();
            for (auto &l : lines) a.push(l);
            v["logs"] = a;
            v["seq"] = (int64_t)seq;
            v["full"] = full;
            ok(rq, rs, v);
            return true;
        }
        if (act == "whitelist") {
            Value v = obj();
            v["entries"] = srvutil::whitelist_entries(*srv);
            ok(rq, rs, v);
            return true;
        }
        std::string rel = qarg(rq, "path");
        std::string safe = fsu::safe_path(srv->cfg().str("directory"), rel);
        if (act == "files") {
            if (safe.empty() || !fsu::is_dir(safe)) { fail(rq, rs, "invalid path", 400); return true; }
            struct E { std::string name, size, modified; bool dir; };
            std::vector<E> es;
            for (auto &n : fsu::list_names(safe)) {
                std::string fp = safe + "/" + n;
                struct stat st;
                if (stat(fp.c_str(), &st) != 0 && lstat(fp.c_str(), &st) != 0) continue;
                bool d = S_ISDIR(st.st_mode);
                es.push_back({n, d ? "-" : fsu::fmt_size((double)st.st_size), fsu::fmt_time(st.st_mtime, "%Y-%m-%d %H:%M"), d});
            }
            std::sort(es.begin(), es.end(), [](const E &a, const E &b) {
                if (a.dir != b.dir) return a.dir;
                return lower(a.name) < lower(b.name);
            });
            Value arr = Value::array();
            for (auto &e : es) {
                Value o = obj();
                o["name"] = e.name; o["type"] = e.dir ? "dir" : "file"; o["size"] = e.size; o["modified"] = e.modified;
                arr.push(o);
            }
            Value v = obj();
            v["path"] = rel;
            v["entries"] = arr;
            ok(rq, rs, v);
            return true;
        }
        if (safe.empty() || !fsu::is_file(safe)) { fail(rq, rs, "file not found", 404); return true; }
        if (act == "filetext") {
            if (fsu::file_size(safe) > (int64_t)EDIT_MAX) { fail(rq, rs, "file is larger than " + std::to_string(EDIT_MAX / 1024) + " KB; download it instead", 400); return true; }
            std::string raw;
            fsu::read_file(safe, raw);
            if (raw.find('\0') != std::string::npos || !valid_utf8(raw)) { fail(rq, rs, "not a UTF-8 text file", 400); return true; }
            Value v = obj();
            v["text"] = raw;
            v["crlf"] = raw.find("\r\n") != std::string::npos;
            ok(rq, rs, v);
            return true;
        }
        send_file(rq, rs, safe, "application/octet-stream", fsu::base_name(safe));
        return true;
    }
    if (p.size() == 2 && p[0] == "resourcepack" && p[1].size() > 4 && p[1].compare(p[1].size() - 4, 4, ".zip") == 0) {
        auto srv = registry::get(p[1].substr(0, p[1].size() - 4));
        if (!srv || !fsu::is_file(rp_path(*srv))) { fail(rq, rs, "not found", 404); return true; }
        std::string who;
        for (char c : rq.hdr("x-minecraft-username")) if (isalnum((unsigned char)c) || c == '_') who += c;
        if (who.size() > 16) who.resize(16);
        srv->append("[CreeperCrest] Resource pack requested by " + ip + " (" + (who.empty() ? "unknown player" : who) + ")");
        send_file(rq, rs, rp_path(*srv), "application/zip", "resource-pack.zip");
        return true;
    }
    return false;
}

// ── POST ─────────────────────────────────────────────────────────────────────

std::string spool_to_temp(const multipart::Body &body, const multipart::Part &part, const std::string &dir) {
    std::string tmp = dir + "/.cc-upload-" + util::token_urlsafe(6) + ".zip";
    if (!multipart::write_part(body, part, tmp)) { unlink(tmp.c_str()); throw Bad("disk write failed"); }
    return tmp;
}

bool do_post(Request &rq, Response &rs, const std::vector<std::string> &p, const std::string &ip) {
    (void)ip;
    if (p.size() == 2 && p[0] == "api") {
        const std::string &a = p[1];
        if (a == "restore") {
            Value b = body_json(rq);
            std::string fname = trim(b.str("backup")), sid = trim(b.str("sid"));
            if (fname.empty() || sid.empty()) { fail(rq, rs, "backup and sid required", 400); return true; }
            std::string id = jobs::start("Restoring backup...", [fname, sid](jobs::Job &j) {
                Value r = obj();
                r["msg"] = backup::restore(fname, sid, &j);
                return r;
            }, "files");
            Value v = obj(); v["ok"] = true; v["job"] = id;
            ok(rq, rs, v);
            return true;
        }
        if (a == "auto_backup") {
            Value b = body_json(rq);
            Value sched = obj();
            sched["enabled"] = b.flag("enabled");
            std::string day = lower(b.str("day", "sunday"));
            sched["day"] = day;
            sched["hour"] = std::max<int64_t>(0, std::min<int64_t>(23, to_int(b.find("hour"), 3)));
            sched["minute"] = std::max<int64_t>(0, std::min<int64_t>(59, to_int(b.find("minute"), 0)));
            static const char *wd[] = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"};
            if (std::none_of(std::begin(wd), std::end(wd), [&](const char *w) { return day == w; })) { fail(rq, rs, "invalid day", 400); return true; }
            // optional subset of servers; absent means "all", and a selection covering every server is stored as "all"
            if (auto *sv = b.find("servers"); sv && sv->is_arr()) {
                Value chosen = Value::array();
                size_t known = registry::all().size();
                for (auto &x : sv->a)
                    if (x.is_str() && registry::get(x.s)) {
                        bool dup = false;
                        for (auto &y : chosen.a) if (y.s == x.s) dup = true;
                        if (!dup) chosen.push(x.s);
                    }
                if (sched.flag("enabled") && chosen.a.empty() && known) throw Bad("Tick at least one server to back up");
                if (chosen.a.size() < known) sched["servers"] = chosen;
            }
            registry::cfg_set("auto_backup", sched);
            int64_t mb = to_int(b.find("max_backups"), registry::cfg_num("max_backups", 0));
            int64_t m = mb <= 0 ? 0 : std::max<int64_t>(3, std::min<int64_t>(28, mb));
            registry::cfg_set("max_backups", Value(m));
            Value v = obj(); v["ok"] = true; v["auto_backup"] = sched; v["max_backups"] = m;
            ok(rq, rs, v);
            return true;
        }
        if (a == "browse_mkdir") {
            Value b = body_json(rq);
            std::string name = trim(b.str("name"));
            if (name.empty() || name.find_first_of("/\\") != std::string::npos || name == "." || name == "..") { fail(rq, rs, "invalid folder name", 400); return true; }
            std::string base = fsu::normalize(fsu::expand_home(b.str("path")));
            std::string target = base + "/" + name;
            if (!fsu::mkdirs(target)) { fail(rq, rs, "cannot create folder", 400); return true; }
            Value v = obj(); v["ok"] = true; v["path"] = target;
            ok(rq, rs, v);
            return true;
        }
        if (a == "create_from_backup") {
            Value b = body_json(rq);
            std::string fname = trim(b.str("backup")), sid = norm_sid(b.str("id"));
            check_new_id(sid);
            if (fname.empty()) throw Bad("backup required");
            std::string fpath = backup::path_of(fname);
            if (fpath.empty() || !fsu::is_file(fpath)) throw Bad("backup file not found");
            if (trim(b.str("directory")).empty()) throw Bad("directory required");
            Value scfg = new_server_cfg(b, sid);
            std::string dest = fsu::expand_home(scfg.str("directory"));
            std::string id = jobs::start("Extracting backup...", [=](jobs::Job &j) {
                fsu::mkdirs(dest);
                j.set_unit("files", 0);
                try {
                    zipx::extract(fpath, dest, [&](int64_t d, int64_t t) { j.total = t; j.done = d; }, true, registry::extract_limit(dest));
                } catch (const std::exception &e) { throw std::runtime_error(std::string("failed to extract backup: ") + e.what()); }
                registry::add(sid, scfg);
                if (auto s = registry::get(sid)) s->append("[CreeperCrest] Created from backup: " + fname);
                Value r = obj(); r["id"] = sid;
                return r;
            }, "files");
            Value v = obj(); v["ok"] = true; v["job"] = id;
            ok(rq, rs, v);
            return true;
        }
        if (a == "import") {
            multipart::Body body(rq);
            std::vector<multipart::Part> parts;
            if (!multipart::parse(rq, body, parts)) throw Bad("expected multipart/form-data");
            auto field = [&](const char *n) { for (auto &q : parts) if (!q.is_file && q.name == n) return std::string(q.data, q.len); return std::string(); };
            std::string sid = norm_sid(field("id"));
            check_new_id(sid);
            std::string directory = trim(field("directory"));
            if (directory.empty()) throw Bad("directory required");
            const multipart::Part *zp = nullptr;
            for (auto &q : parts) if (q.is_file && lower(q.filename).size() >= 4 && lower(q.filename).compare(lower(q.filename).size() - 4, 4, ".zip") == 0) { zp = &q; break; }
            if (!zp || !zp->len) throw Bad("no zip file uploaded");
            std::string tmp = spool_to_temp(body, *zp, app::cfg.base_dir);
            if (!zipx::is_zip(tmp)) { unlink(tmp.c_str()); throw Bad("uploaded file is not a valid zip"); }
            Value src = obj();
            src["name"] = field("name").empty() ? sid : field("name");
            src["directory"] = directory;
            src["jar"] = field("jar").empty() ? "server.jar" : field("jar");
            src["runtime"] = field("runtime");
            auto num = [&](const char *k, int64_t def) { std::string s = trim(field(k)); return s.empty() ? def : (int64_t)atoll(s.c_str()); };
            int64_t legacy = std::max<int64_t>(256, num("memory_mb", 1024));
            src["memory_min_mb"] = num("memory_min_mb", legacy);
            src["memory_max_mb"] = num("memory_max_mb", legacy);
            src["extra_args"] = field("extra_args");
            Value scfg;
            try { scfg = new_server_cfg(src, sid); } catch (...) { unlink(tmp.c_str()); throw; }
            std::string dest = fsu::expand_home(directory);
            std::string id = jobs::start("Extracting server files...", [=](jobs::Job &j) {
                struct Cleanup { std::string f; ~Cleanup() { unlink(f.c_str()); } } cl{tmp};
                fsu::mkdirs(dest);
                j.set_unit("files", 0);
                try {
                    zipx::extract(tmp, dest, [&](int64_t d, int64_t t) { j.total = t; j.done = d; }, true, registry::extract_limit(dest));
                } catch (const std::exception &e) { throw std::runtime_error(std::string("failed to extract zip: ") + e.what()); }
                registry::add(sid, scfg);
                Value r = obj(); r["id"] = sid;
                return r;
            }, "files");
            Value v = obj(); v["ok"] = true; v["job"] = id;
            ok(rq, rs, v);
            return true;
        }
        if (a == "add") {
            Value b = body_json(rq);
            std::string sid = norm_sid(b.str("id"));
            check_new_id(sid);
            Value scfg = new_server_cfg(b, sid);
            scfg["autostart"] = b.flag("autostart");
            scfg["resource_pack"] = trim(b.str("resource_pack"));
            scfg["resource_pack_sha1"] = lower(trim(b.str("resource_pack_sha1")));
            scfg["resource_pack_required"] = b.flag("resource_pack_required");
            scfg["resource_pack_prompt"] = trim(b.str("resource_pack_prompt"));
            std::string err;
            if (!srvutil::check_pack(scfg.str("resource_pack"), scfg.str("resource_pack_sha1"), scfg.str("resource_pack_prompt"), err)) throw Bad(err);
            std::vector<std::pair<std::string, std::string>> props;
            if (!srvutil::validate_props(b.find("properties") ? *b.find("properties") : Value(), props, err)) throw Bad(err);
            if (scfg.str("directory").empty()) throw Bad("directory required");
            std::string jtype = lower(trim(b.str("jar_type"))), ver = trim(b.str("jar_version"));
            if (scfg.str("runtime") == "native") jtype.clear();
            if (!jtype.empty()) {
                if (!jars::known_type(jtype)) throw Bad("unknown server type");
                if (fsu::base_name(scfg.str("jar")) != scfg.str("jar")) throw Bad("JAR must be a plain filename");
                if (ver.empty()) throw Bad("pick a Minecraft version");
            }
            bool eula = b.flag("eula");
            auto finish = [=](jobs::Job *j) mutable {
                std::string dest_dir = fsu::expand_home(scfg.str("directory"));
                if (!fsu::mkdirs(dest_dir)) throw std::runtime_error("cannot create folder: " + dest_dir);
                if (!jtype.empty()) {
                    try { jars::download(jtype, ver, dest_dir + "/" + scfg.str("jar"), j); }
                    catch (const std::exception &e) { throw std::runtime_error(std::string("JAR download failed: ") + e.what()); }
                }
                try {
                    if (!props.empty() && !srvutil::set_properties(scfg.str("directory"), props)) throw std::runtime_error("write failed");
                    if (eula && !fsu::write_file(dest_dir + "/eula.txt", "eula=true\n")) throw std::runtime_error("write failed");
                    if (!scfg.str("resource_pack").empty() || scfg.flag("resource_pack_required") || !scfg.str("resource_pack_prompt").empty())
                        srvutil::apply_resource_pack(scfg);
                } catch (const std::exception &e) { throw std::runtime_error(std::string("failed to write server.properties: ") + e.what()); }
                registry::add(sid, scfg);
                Value r = obj(); r["id"] = sid;
                return r;
            };
            Value v = obj();
            v["ok"] = true;
            if (!jtype.empty()) v["job"] = jobs::start("Downloading server JAR...", [finish](jobs::Job &j) mutable { return finish(&j); });
            else {
                try { finish(nullptr); } catch (const std::exception &e) { throw Bad(e.what()); }
                v["id"] = sid;
            }
            ok(rq, rs, v);
            return true;
        }
        return false;
    }
    if (p.size() != 3 || p[0] != "api") return false;
    auto srv = registry::get(p[1]);
    const std::string &action = p[2];
    if (!srv) { fail(rq, rs, "server not found", 404); return true; }
    std::string sid = srv->id;

    if (action == "upload") {
        multipart::Body body(rq);
        std::vector<multipart::Part> parts;
        if (!multipart::parse(rq, body, parts)) throw Bad("expected multipart/form-data");
        std::string dest = fsu::safe_path(srv->cfg().str("directory"), qarg(rq, "path"));
        if (dest.empty()) throw Bad("invalid path");
        int64_t saved = 0;
        bool any = false;
        for (auto &q : parts) if (q.is_file) any = true;
        if (!any) throw Bad("no files found in upload");
        fsu::mkdirs(dest);
        int dfd = fsu::open_dir_fd(dest);
        if (dfd < 0) throw Bad("invalid path");
        struct DirGuard { int fd; ~DirGuard() { close(fd); } } guard{dfd};
        for (auto &q : parts) {
            if (!q.is_file) continue;
            std::string name = fsu::base_name(q.filename);
            if (name.empty() || name == "." || name == ".." || name.find('\0') != std::string::npos) continue;
            if (!multipart::write_part_beneath(body, q, dfd, name)) throw Bad("cannot write " + name + " (is it a link?)");
            saved++;
        }
        Value v = obj(); v["ok"] = true; v["count"] = saved;
        ok(rq, rs, v);
        return true;
    }
    if (action == "filesave") {
        Value b = body_json(rq);
        auto *t = b.find("text");
        std::string safe = fsu::safe_path(srv->cfg().str("directory"), qarg(rq, "path"));
        if (!t || !t->is_str() || safe.empty() || !fsu::is_file(safe)) throw Bad("invalid file");
        if (t->s.size() > EDIT_MAX) throw Bad("file would exceed " + std::to_string(EDIT_MAX / 1024) + " KB");
        std::string head;
        fsu::read_file(safe, head, EDIT_MAX);
        if (head.find('\0') != std::string::npos || head.size() > EDIT_MAX) throw Bad("refusing to overwrite a binary or oversized file");
        if (!valid_utf8(head)) throw Bad("refusing to overwrite a non-UTF-8 file");
        if (!fsu::write_file_atomic(safe, t->s, safe)) { fail(rq, rs, "write failed", 500); return true; }
        Value v = obj(); v["ok"] = true; v["size"] = (int64_t)t->s.size();
        ok(rq, rs, v);
        return true;
    }
    if (action == "mkdir") {
        Value b = body_json(rq);
        std::string name = trim(b.str("name"));
        size_t a0 = name.find_first_not_of("/\\"), b0 = name.find_last_not_of("/\\");
        name = a0 == std::string::npos ? "" : name.substr(a0, b0 - a0 + 1);
        name = fsu::base_name(name);
        if (name.empty() || name == "." || name == "..") throw Bad("invalid folder name");
        std::string rel = qarg(rq, "path"), dir = srv->cfg().str("directory");
        std::string parent = fsu::safe_path(dir, rel);
        if (parent.empty() || !fsu::is_dir(parent)) throw Bad("invalid path");
        std::string target = fsu::safe_path(dir, fsu::join(rel, name));
        if (target.empty()) throw Bad("invalid path");
        if (fsu::exists(target)) throw Bad("already exists");
        if (mkdir(target.c_str(), 0755) != 0) { fail(rq, rs, strerror(errno), 500); return true; }
        Value v = obj(); v["ok"] = true; v["name"] = name;
        ok(rq, rs, v);
        return true;
    }
    if (action == "zip") {
        Value b = body_json(rq);
        std::string base = srv->directory();
        std::string basereal = fsu::resolve(base);
        std::vector<std::pair<std::string, std::string>> files;
        if (auto *f = b.find("files"); f && f->is_arr()) {
            for (auto &r : f->a) {
                if (!r.is_str()) continue;
                std::string safe = fsu::safe_path(base, r.s);
                if (safe.empty()) continue;
                if (fsu::is_file(safe)) files.emplace_back(safe, r.s);
                else if (fsu::is_dir(safe)) {
                    std::vector<std::string> stack = {safe};
                    while (!stack.empty()) {
                        std::string d = stack.back();
                        stack.pop_back();
                        for (auto &n : fsu::list_names(d)) {
                            std::string fp = d + "/" + n;
                            if (fsu::is_dir(fp)) stack.push_back(fp);
                            else if (fsu::is_file(fp)) files.emplace_back(fp, fp.substr(basereal.size() + 1));
                        }
                    }
                }
            }
        }
        if (files.empty()) throw Bad("no files matched");
        std::string tmp = app::cfg.base_dir + "/.cc-zip-" + util::token_urlsafe(6) + ".zip";
        try { zipx::create(tmp, files); } catch (const std::exception &e) { unlink(tmp.c_str()); fail(rq, rs, e.what(), 500); return true; }
        send_file(rq, rs, tmp, "application/zip", sid + "-files.zip", true);
        return true;
    }
    if (action == "rp_upload") {
        multipart::Body body(rq);
        std::vector<multipart::Part> parts;
        if (rq.body_len > RP_MAX_BYTES + 65536) throw Bad("Pack larger than 250 MB");
        if (!multipart::parse(rq, body, parts)) throw Bad("expected multipart/form-data");
        const multipart::Part *fp = nullptr;
        for (auto &q : parts) if (q.is_file) { fp = &q; break; }
        if (!fp) throw Bad("no file uploaded");
        std::string tmp = spool_to_temp(body, *fp, srv->directory().empty() ? app::cfg.base_dir : srv->directory());
        std::string url;
        try { url = install_pack(srv, tmp, rq.hdr("host").empty() ? "localhost" : rq.hdr("host")); }
        catch (...) { unlink(tmp.c_str()); throw; }
        Value v = rp_result(*srv, url);
        v["ok"] = true;
        ok(rq, rs, v);
        return true;
    }

    Value b = body_json(rq);
    std::pair<bool, std::string> r{true, ""};
    if (action == "start") r = srv->start();
    else if (action == "stop") r = srv->stop();
    else if (action == "restart") r = srv->restart();
    else if (action == "command") r = srv->send_command(b.str("command"));
    else if (action == "whitelist") {
        std::string name = trim(b.find("name") && b.find("name")->is_str() ? b.str("name") : ""), op = b.str("op");
        if (op == "remove" && srvutil::valid_uuid(name)) {
            auto rr = srvutil::whitelist_remove_uuid(*srv, name);
            Value v = obj(); v["ok"] = rr.first; v["msg"] = rr.second;
            ok(rq, rs, v, rr.first ? 200 : 400);
            return true;
        }
        if ((op != "add" && op != "remove") || !srvutil::valid_player(name)) throw Bad("invalid player name");
        if (op == "add") {
            { std::lock_guard<std::mutex> g(srv->ban_mu); srv->banned.erase(name); }
            srv->send_command("pardon " + name);
        }
        r = srv->send_command("whitelist " + op + " " + name);
        if (!r.first) r.second = "Start the server first to change its whitelist";
    } else if (action == "backup") {
        std::string id = jobs::start("Creating backup...", [sid](jobs::Job &j) { return backup::create(sid, &j); });
        Value v = obj(); v["ok"] = true; v["job"] = id;
        ok(rq, rs, v);
        return true;
    } else if (action == "rp_fetch") {
        std::string url = trim(b.str("url")), host = rq.hdr("host").empty() ? "localhost" : rq.hdr("host");
        if (strncasecmp(url.c_str(), "http://", 7) && strncasecmp(url.c_str(), "https://", 8)) throw Bad("URL must start with http:// or https://");
        std::string id = jobs::start("Downloading resource pack...", [srv, url, host](jobs::Job &j) {
            std::string dir = srv->directory(), tmp = (fsu::is_dir(dir) ? dir : app::cfg.base_dir) + "/.cc-rp-" + util::token_urlsafe(6) + ".zip";
            FILE *f = fopen(tmp.c_str(), "wb");
            if (!f) throw std::runtime_error("cannot write temporary file");
            try {
                fetch::get(url, registry::fetch_policy(), [&](const char *p, size_t n) { if (fwrite(p, 1, n, f) != n) throw std::runtime_error("disk write failed"); },
                           [&](int64_t d, int64_t t) { j.total = t; j.done = d; }, (int64_t)RP_MAX_BYTES);
                FILE *done = f;
                f = nullptr;                      // closed exactly once: the catch below must not close it again
                if (fclose(done) != 0) throw std::runtime_error("disk write failed");
                j.set_label("Installing resource pack...");
                j.total = 0;
                std::string u = install_pack(srv, tmp, host);
                return rp_result(*srv, u);
            } catch (...) {
                if (f) fclose(f);
                unlink(tmp.c_str());
                throw;
            }
        });
        Value v = obj(); v["ok"] = true; v["job"] = id;
        ok(rq, rs, v);
        return true;
    } else if (action == "config") {
        Value c = srv->cfg();
        auto sets = [&](const char *k, bool trimmed) { if (auto *x = b.find(k); x && x->is_str()) c[k] = trimmed ? trim(x->s) : x->s; };
        if (b.find("name") && b.find("name")->is_str() && !trim(b.str("name")).empty()) c["name"] = trim(b.str("name"));
        if (b.find("directory") && b.find("directory")->is_str() && !trim(b.str("directory")).empty()) c["directory"] = trim(b.str("directory"));
        if (b.find("jar")) c["jar"] = trim(b.str("jar")).empty() ? "server.jar" : trim(b.str("jar"));
        if (b.find("runtime")) c["runtime"] = b.str("runtime") == "native" ? "native" : "java";
        if (b.find("allow_offline")) c["allow_offline"] = b.flag("allow_offline");
        if (b.find("memory_min_mb")) c["memory_min_mb"] = std::max<int64_t>(256, to_int(b.find("memory_min_mb"), 256));
        if (b.find("memory_max_mb")) c["memory_max_mb"] = std::max<int64_t>(256, to_int(b.find("memory_max_mb"), 256));
        if (b.find("extra_args")) sets("extra_args", false);
        if (b.find("autostart")) c["autostart"] = b.flag("autostart");
        static const char *rpk[] = {"resource_pack", "resource_pack_sha1", "resource_pack_required", "resource_pack_prompt"};
        Value old = obj();
        for (auto k : rpk) old[k] = c.find(k) ? *c.find(k) : Value(k[14] == 'r' ? Value(false) : Value(""));
        if (b.find("resource_pack_required")) c["resource_pack_required"] = b.flag("resource_pack_required");
        if (b.find("resource_pack_prompt")) c["resource_pack_prompt"] = trim(b.str("resource_pack_prompt"));
        if (b.find("resource_pack")) c["resource_pack"] = trim(b.str("resource_pack"));
        if (b.find("resource_pack_sha1")) c["resource_pack_sha1"] = lower(trim(b.str("resource_pack_sha1")));
        bool changed = false;
        for (auto k : rpk) {
            Value cur = c.find(k) ? *c.find(k) : Value(k[14] == 'r' ? Value(false) : Value(""));
            if (json::dump(cur) != json::dump(*old.find(k))) changed = true;
        }
        std::string err;
        if (!srvutil::check_pack(c.str("resource_pack"), c.str("resource_pack_sha1"), c.str("resource_pack_prompt"), err)) throw Bad(err);
        if (c.num("memory_min_mb", 256) > c.num("memory_max_mb", 256)) throw Bad("max RAM must be >= min RAM");
        if (changed) {
            try { srvutil::apply_resource_pack(c); } catch (const std::exception &e) { throw Bad(std::string("failed to write server.properties: ") + e.what()); }
        }
        srv->set_cfg(c);
        registry::persist(sid, c);
        r = {true, "Saved"};
    } else {
        throw Bad("unknown action");
    }
    Value v = obj();
    v["ok"] = r.first;
    v["msg"] = r.second;
    ok(rq, rs, v);
    return true;
}

// ── DELETE ───────────────────────────────────────────────────────────────────

bool do_delete(Request &rq, Response &rs, const std::vector<std::string> &p) {
    if (p.size() == 3 && p[0] == "api" && p[1] == "backup") {
        std::string f = backup::path_of(http::url_decode(p[2], false));
        if (f.empty() || !fsu::is_file(f)) { fail(rq, rs, "not found", 404); return true; }
        if (unlink(f.c_str()) != 0) { fail(rq, rs, strerror(errno), 500); return true; }
        Value v = obj(); v["ok"] = true;
        ok(rq, rs, v);
        return true;
    }
    if (p.size() == 2 && p[0] == "api") {
        auto srv = registry::get(p[1]);
        if (!srv) { fail(rq, rs, "not found", 404); return true; }
        if (srv->is_running()) { fail(rq, rs, "stop the server before removing it", 400); return true; }
        std::string dir = srv->directory();
        registry::remove(p[1]);
        Value v = obj(); v["ok"] = true;
        if (!dir.empty() && fsu::is_dir(dir)) {
            std::string real = fsu::resolve(dir), home = fsu::resolve(app::expand_home("~")), panel = fsu::resolve(app::cfg.base_dir);
            auto inside_or_equal = [](const std::string &outer, const std::string &inner) {   // is `outer` an ancestor-or-self of `inner`
                return inner == outer || outer == "/" || (inner.size() > outer.size() && inner.compare(0, outer.size(), outer) == 0 && inner[outer.size()] == '/');
            };
            if (real == "/" || inside_or_equal(real, home) || inside_or_equal(real, panel))
                v["warn"] = "Removed from config, but the folder was not deleted because it contains your home or the panel: " + dir;
            else if (!fsu::rmtree(dir)) v["warn"] = "Removed from config but could not delete directory: " + dir;
        }
        ok(rq, rs, v);
        return true;
    }
    if (p.size() == 3 && p[0] == "api" && p[2] == "file") {
        auto srv = registry::get(p[1]);
        if (!srv) { fail(rq, rs, "not found", 404); return true; }
        std::string base = srv->cfg().str("directory");
        std::string safe = fsu::safe_path(base, qarg(rq, "path"));
        if (safe.empty() || safe == fsu::resolve(fsu::expand_home(base))) { fail(rq, rs, "invalid path", 400); return true; }
        struct stat st;
        if (lstat(safe.c_str(), &st) != 0) { fail(rq, rs, "not found", 404); return true; }
        bool good = S_ISDIR(st.st_mode) ? fsu::rmtree(safe) : unlink(safe.c_str()) == 0;
        if (!good) { fail(rq, rs, strerror(errno), 500); return true; }
        Value v = obj(); v["ok"] = true;
        ok(rq, rs, v);
        return true;
    }
    return false;
}

} // namespace

bool handle(Request &rq, Response &rs, const std::string &ip) {
    auto p = segs(rq.path);
    try {
        if (rq.method == "GET") return do_get(rq, rs, p, ip);
        if (rq.method == "POST") return do_post(rq, rs, p, ip);
        if (rq.method == "DELETE") return do_delete(rq, rs, p);
    } catch (const Bad &e) {
        fail(rq, rs, e.what(), e.code);
        return true;
    } catch (const std::exception &e) {
        fail(rq, rs, e.what(), 500);
        return true;
    }
    return false;
}

bool handle_head(Request &rq, Response &rs) {
    auto p = segs(rq.path);
    if (p.size() == 2 && p[0] == "resourcepack" && p[1].size() > 4 && p[1].compare(p[1].size() - 4, 4, ".zip") == 0) {
        auto srv = registry::get(p[1].substr(0, p[1].size() - 4));
        if (srv && fsu::is_file(rp_path(*srv))) {
            rs.code = 200;
            rs.set("Content-Type", "application/zip");
            rs.set("Content-Length", std::to_string(fsu::file_size(rp_path(*srv))));
            return true;
        }
        rs.code = 404;
        rs.set("Content-Length", "0");
        return true;
    }
    return false;
}

} // namespace api
