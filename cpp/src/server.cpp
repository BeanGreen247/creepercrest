#include "server.h"
#include "fsu.h"
#include "proc.h"
#include "sysinfo.h"
#include "util.h"
#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>
#include <algorithm>
#include <cerrno>
#include <strings.h>
#include <stdexcept>
#include <chrono>
#include <cstring>
#include <ctime>
#include <regex>
#include <thread>

using json::Value;

static const int CONNECTION_THROTTLE_MS = 5000;   // Bukkit/Paper: min ms between logins from one IP

// ── helpers ──────────────────────────────────────────────────────────────────

namespace srvutil {

std::string to_bool(const std::string &v) {
    std::string l = v;
    for (auto &c : l) c = (char)tolower((unsigned char)c);
    return (l == "true" || l == "1" || l == "yes" || l == "on") ? "true" : "false";
}

std::string prop_escape(const std::string &in) {
    std::string out;
    for (size_t i = 0; i < in.size();) {
        unsigned char c = in[i];
        if (c == '\\') { out += "\\\\"; i++; continue; }
        if (c >= 32 && c < 127) { out += (char)c; i++; continue; }
        uint32_t cp = c;
        int n = 1;
        if (c >= 0xF0) { cp = c & 7; n = 4; } else if (c >= 0xE0) { cp = c & 15; n = 3; } else if (c >= 0xC0) { cp = c & 31; n = 2; }
        for (int k = 1; k < n && i + k < in.size(); k++) cp = (cp << 6) | (in[i + k] & 63);
        i += n;
        char b[16];
        if (cp >= 0x10000) {
            cp -= 0x10000;
            snprintf(b, sizeof b, "\\u%04x\\u%04x", 0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF));
        } else snprintf(b, sizeof b, "\\u%04x", cp);
        out += b;
    }
    return out;
}

std::string uuid4() {
    std::string r = util::random_bytes(16);
    r[6] = (char)((r[6] & 0x0F) | 0x40);
    r[8] = (char)((r[8] & 0x3F) | 0x80);
    char b[40];
    unsigned char *u = (unsigned char *)r.data();
    snprintf(b, sizeof b, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x", u[0], u[1], u[2], u[3], u[4], u[5],
             u[6], u[7], u[8], u[9], u[10], u[11], u[12], u[13], u[14], u[15]);
    return b;
}

bool valid_uuid(const std::string &s) {
    static const std::regex re("[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}");
    return std::regex_match(s, re);
}

bool valid_player(const std::string &s) {
    if (s.empty() || s.size() > 32) return false;
    for (char c : s) if (!isalnum((unsigned char)c) && c != '_' && c != '.') return false;
    return true;
}

static std::vector<std::string> split_lines(const std::string &t) {
    std::vector<std::string> v;
    size_t i = 0;
    while (i < t.size()) {
        size_t j = t.find('\n', i);
        if (j == std::string::npos) j = t.size();
        std::string l = t.substr(i, j - i);
        if (!l.empty() && l.back() == '\r') l.pop_back();
        v.push_back(l);
        i = j + 1;
    }
    return v;
}

static std::string trim(const std::string &s) {
    size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
}

bool set_properties(const std::string &dir0, const std::vector<std::pair<std::string, std::string>> &updates) {
    std::string dir = fsu::expand_home(dir0), path = dir + "/server.properties", text;
    std::vector<std::string> lines;
    if (fsu::is_file(path)) {
        if (!fsu::read_file(path, text)) return false;
        lines = split_lines(text);
    }
    std::vector<std::pair<std::string, std::string>> pending = updates;
    for (auto &line : lines) {
        std::string key = trim(line.substr(0, line.find('=')));
        size_t f = line.find_first_not_of(" \t");
        if (f != std::string::npos && line[f] == '#') continue;
        auto it = std::find_if(pending.begin(), pending.end(), [&](auto &p) { return p.first == key; });
        if (it != pending.end()) { line = key + "=" + it->second; pending.erase(it); }
    }
    for (auto &p : pending) lines.push_back(p.first + "=" + p.second);
    fsu::mkdirs(dir);
    std::string out;
    for (auto &l : lines) out += l + "\n";
    return fsu::write_file(path, out);
}

void harden(const std::string &dir0, bool allow_offline) {
    std::string dir = fsu::expand_home(dir0);
    std::vector<std::pair<std::string, std::string>> forced = {
        {"white-list", "true"}, {"enforce-whitelist", "true"}, {"online-mode", "true"},
        {"enable-query", "false"}, {"enable-rcon", "false"}};
    if (allow_offline) forced.erase(std::find_if(forced.begin(), forced.end(), [](auto &p) { return p.first == "online-mode"; }));
    if (!set_properties(dir, forced)) throw std::runtime_error("cannot write server.properties");
    // bukkit.yml login throttle (best effort, like the Python panel)
    std::string path = dir + "/bukkit.yml", text;
    if (fsu::is_file(path) && !fsu::read_file(path, text)) return;
    std::string line = "  connection-throttle: " + std::to_string(CONNECTION_THROTTLE_MS);
    auto lines = split_lines(text);
    bool had_nl = !text.empty() && text.back() == '\n';
    bool done = false;
    for (auto &l : lines)
        if (trim(l).compare(0, 20, "connection-throttle:") == 0) { l = line; done = true; }
    if (!done) {
        for (size_t i = 0; i < lines.size(); i++)
            if (trim(lines[i]) == "settings:" && lines[i].find_first_not_of(" \t") == 0) { lines.insert(lines.begin() + i + 1, line); done = true; break; }
    }
    if (!done) { lines.push_back("settings:"); lines.push_back(line); }
    std::string out;
    for (auto &l : lines) out += l + "\n";
    (void)had_nl;
    fsu::write_file(path, out);
}

bool validate_props(const Value &raw, std::vector<std::pair<std::string, std::string>> &out, std::string &err) {
    struct Spec { const char *key; char kind; int a, b; std::vector<std::pair<const char *, const char *>> en; };
    static const std::vector<Spec> specs = {
        {"motd", 's', 200, 0, {}}, {"level-name", 'n', 60, 0, {}}, {"level-seed", 's', 200, 0, {}},
        {"level-type", 'e', 0, 0, {{"normal", "minecraft\\:normal"}, {"flat", "minecraft\\:flat"}, {"large_biomes", "minecraft\\:large_biomes"}, {"amplified", "minecraft\\:amplified"}}},
        {"gamemode", 'e', 0, 0, {{"survival", "survival"}, {"creative", "creative"}, {"adventure", "adventure"}, {"spectator", "spectator"}}},
        {"difficulty", 'e', 0, 0, {{"peaceful", "peaceful"}, {"easy", "easy"}, {"normal", "normal"}, {"hard", "hard"}}},
        {"max-players", 'i', 1, 1000, {}}, {"server-port", 'i', 1024, 65535, {}}, {"view-distance", 'i', 2, 32, {}},
        {"simulation-distance", 'i', 3, 32, {}}, {"spawn-protection", 'i', 0, 1000, {}},
        {"online-mode", 'b', 0, 0, {}}, {"pvp", 'b', 0, 0, {}}, {"hardcore", 'b', 0, 0, {}}, {"white-list", 'b', 0, 0, {}},
        {"allow-flight", 'b', 0, 0, {}}, {"allow-nether", 'b', 0, 0, {}}, {"enable-command-block", 'b', 0, 0, {}}};
    out.clear();
    if (!raw.is_obj()) return true;
    for (auto &kv : raw.o) {
        const Spec *sp = nullptr;
        for (auto &s : specs) if (kv.first == s.key) sp = &s;
        if (!sp) { err = "unknown property: " + kv.first; return false; }
        std::string v;
        const Value &x = kv.second;
        if (x.type == Value::Str) v = x.s;
        else if (x.type == Value::Int) v = std::to_string(x.i);
        else if (x.type == Value::Bool) v = x.b ? "True" : "False";
        else if (x.type == Value::Real) v = std::to_string((long long)x.d);
        v = trim(v);
        const std::string k = kv.first;
        if (sp->kind == 's' || sp->kind == 'n') {
            if (v.find_first_of("\r\n") != std::string::npos || (int)v.size() > sp->a) { err = "invalid value for " + k; return false; }
            if (sp->kind == 'n' && (v.empty() || v.find_first_of("/\\") != std::string::npos || v.find("..") != std::string::npos)) { err = "invalid value for " + k; return false; }
            out.emplace_back(k, prop_escape(v));
        } else if (sp->kind == 'e') {
            bool ok = false;
            for (auto &e : sp->en) if (v == e.first) { out.emplace_back(k, e.second); ok = true; }
            if (!ok) { err = "invalid value for " + k; return false; }
        } else if (sp->kind == 'i') {
            char *e;
            long n = strtol(v.c_str(), &e, 10);
            if (v.empty() || *e) { err = k + " must be a number"; return false; }
            if (n < sp->a || n > sp->b) { err = k + " must be between " + std::to_string(sp->a) + " and " + std::to_string(sp->b); return false; }
            out.emplace_back(k, std::to_string(n));
        } else out.emplace_back(k, to_bool(v));
    }
    return true;
}

bool check_pack(const std::string &url, const std::string &sha1, const std::string &prompt, std::string &err) {
    if (prompt.find_first_of("\r\n") != std::string::npos || prompt.size() > 256) { err = "Resource pack prompt must be one line, 256 characters or fewer"; return false; }
    if (!url.empty() && strncasecmp(url.c_str(), "http://", 7) && strncasecmp(url.c_str(), "https://", 8)) { err = "Resource pack URL must start with http:// or https://"; return false; }
    if (!sha1.empty()) {
        bool ok = sha1.size() == 40;
        for (char c : sha1) if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) ok = false;
        if (!ok) { err = "Resource pack SHA-1 must be 40 hex characters"; return false; }
    }
    return true;
}

void apply_resource_pack(Value &scfg) {
    std::string rp = scfg.str("resource_pack");
    if (!rp.empty() && scfg.str("resource_pack_id").empty()) scfg["resource_pack_id"] = uuid4();
    if (!set_properties(scfg.str("directory"), {
            {"resource-pack", rp},
            {"resource-pack-sha1", scfg.str("resource_pack_sha1")},
            {"resource-pack-id", rp.empty() ? "" : scfg.str("resource_pack_id")},
            {"require-resource-pack", scfg.flag("resource_pack_required") ? "true" : "false"},
            {"resource-pack-prompt", prop_escape(scfg.str("resource_pack_prompt"))}}))
        throw std::runtime_error("cannot write server.properties");
}

Value whitelist_entries(ManagedServer &srv) {
    Value out = Value::array();
    std::string text;
    if (!fsu::read_file(srv.directory() + "/whitelist.json", text)) return out;
    try {
        Value v = json::parse(text);
        if (!v.is_arr()) return out;
        std::vector<std::pair<std::string, std::string>> e;
        for (auto &x : v.a) {
            if (!x.is_obj()) continue;
            std::string n = x.str("name"), u = x.str("uuid");
            if (!n.empty() || !u.empty()) e.emplace_back(n, u);
        }
        std::sort(e.begin(), e.end(), [](auto &a, auto &b) {
            std::string x = a.first, y = b.first;
            for (auto &c : x) c = (char)tolower((unsigned char)c);
            for (auto &c : y) c = (char)tolower((unsigned char)c);
            return x < y;
        });
        for (auto &p : e) { Value o = Value::object(); o["name"] = p.first; o["uuid"] = p.second; out.push(o); }
    } catch (...) {}
    return out;
}

std::pair<bool, std::string> whitelist_remove_uuid(ManagedServer &srv, const std::string &uuid) {
    auto norm = [](std::string s) {
        std::string o;
        for (char c : s) if (c != '-') o += (char)tolower((unsigned char)c);
        return o;
    };
    std::string want = norm(uuid), path = srv.directory() + "/whitelist.json", text;
    if (!fsu::read_file(path, text)) return {false, "No whitelist.json to edit"};
    Value entries;
    try { entries = json::parse(text); } catch (...) { return {false, "No whitelist.json to edit"}; }
    if (!entries.is_arr()) return {false, "No whitelist.json to edit"};
    Value kept = Value::array();
    for (auto &e : entries.a) if (!(e.is_obj() && norm(e.str("uuid")) == want)) kept.push(e);
    if (kept.a.size() == entries.a.size()) return {false, "That UUID is not on the whitelist"};
    if (!fsu::write_file_atomic(path, json::dump(kept, 2))) return {false, "Could not write whitelist.json"};
    if (srv.is_running()) srv.send_command("whitelist reload");
    return {true, "Removed"};
}

static int java_major = -1;
int host_java_major() {
    if (java_major >= 0) return java_major;
    std::string out, err;
    int rc = proc::run_capture({"java", "-version"}, &out, &err, 10000);
    java_major = 0;
    if (rc >= 0) {
        std::smatch m;
        static const std::regex re("version \"(\\d+)(?:\\.(\\d+))?");
        if (std::regex_search(err, m, re)) java_major = m[1].str() == "1" ? (m[2].matched ? atoi(m[2].str().c_str()) : 0) : atoi(m[1].str().c_str());
    }
    return java_major;
}

std::string default_jvm_args() {
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    if (n < 1) n = 4;
    return std::string("-XX:+UseG1GC -XX:+UnlockExperimentalVMOptions -XX:MaxGCPauseMillis=100 -XX:+ParallelRefProcEnabled "
                       "-XX:+DisableExplicitGC -XX:+AlwaysPreTouch -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 "
                       "-XX:G1HeapRegionSize=8M -XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 "
                       "-XX:InitiatingHeapOccupancyPercent=20 -XX:G1MixedGCLiveThresholdPercent=90 "
                       "-XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem "
                       "-XX:MaxTenuringThreshold=1") +
           " -XX:ParallelGCThreads=" + std::to_string(n) + " -XX:ConcGCThreads=" + std::to_string(std::max(1L, n / 2));
}

} // namespace srvutil

// ── ManagedServer ────────────────────────────────────────────────────────────

ManagedServer::ManagedServer(std::string i, Value c) : id(std::move(i)), cfg_(std::move(c)) {}

Value ManagedServer::cfg() const { std::lock_guard<std::mutex> g(cfg_mu_); return cfg_; }
void ManagedServer::set_cfg(const Value &c) { std::lock_guard<std::mutex> g(cfg_mu_); cfg_ = c; }
std::string ManagedServer::directory() const { return fsu::expand_home(cfg().str("directory")); }

void ManagedServer::append(const std::string &line) {
    std::lock_guard<std::mutex> g(log_mu_);
    logs_.push_back("[" + fsu::now_str("%H:%M:%S") + "] " + line);
    if (logs_.size() > 300) logs_.pop_front();
    log_seq_++;
}

void ManagedServer::logs_since(long since, std::vector<std::string> &lines, long &seq, bool &full) const {
    std::lock_guard<std::mutex> g(log_mu_);
    seq = log_seq_;
    long n = log_seq_ - since;
    if (since <= 0 || n < 0 || n > (long)logs_.size()) {
        lines.assign(logs_.begin(), logs_.end());
        full = true;
        return;
    }
    full = false;
    lines.assign(logs_.end() - n, logs_.end());
}

static std::vector<std::string> split_ws(const std::string &s) {
    std::vector<std::string> v;
    size_t i = 0;
    while (i < s.size()) {
        while (i < s.size() && isspace((unsigned char)s[i])) i++;
        size_t j = i;
        while (j < s.size() && !isspace((unsigned char)s[j])) j++;
        if (j > i) v.push_back(s.substr(i, j - i));
        i = j;
    }
    return v;
}

std::pair<bool, std::string> ManagedServer::start() {
    std::lock_guard<std::mutex> pg(proc_mu_);
    if (running_) return {false, "Already running"};
    Value c = cfg();
    std::string dir = fsu::expand_home(c.str("directory")), jar = c.str("jar", "server.jar");
    if (jar.empty()) jar = "server.jar";
    int64_t legacy = c.num("memory_mb", 1024);
    int64_t min_mem = c.num("memory_min_mb", legacy), max_mem = c.num("memory_max_mb", legacy);
    std::vector<std::string> extra = split_ws(c.str("extra_args"));
    std::string jar_path = !jar.empty() && jar[0] == '/' ? jar : dir + "/" + jar;
    bool native = c.str("runtime", "java") == "native";
    std::vector<std::string> cmd;
    if (native) {
        if (!fsu::is_file(jar_path)) return {false, "Binary not found: " + jar_path};
        if (!fsu::is_executable(jar_path)) return {false, "Binary is not executable: " + jar_path};
        cmd = {jar_path, "nogui"};
        cmd.insert(cmd.end(), extra.begin(), extra.end());
    } else {
        if (!fsu::is_file(jar_path)) return {false, "JAR not found: " + jar_path};
        cmd = {"java", "-Xms" + std::to_string(min_mem) + "M", "-Xmx" + std::to_string(max_mem) + "M"};
        cmd.insert(cmd.end(), extra.begin(), extra.end());
        cmd.push_back("-jar");
        cmd.push_back(jar_path);
        cmd.push_back("--nogui");
    }
    try {
        srvutil::harden(dir, c.flag("allow_offline"));
    } catch (const std::exception &e) {
        return {false, std::string("failed to harden server config: ") + e.what()};
    }
    if (!fsu::is_dir(dir)) return {false, "Server directory not found: " + dir};

    int in[2], out[2], ep[2];
    if (pipe2(in, O_CLOEXEC) != 0 || pipe2(out, O_CLOEXEC) != 0 || pipe2(ep, O_CLOEXEC) != 0) return {false, "pipe failed"};
    std::vector<char *> av;
    for (auto &a : cmd) av.push_back(const_cast<char *>(a.c_str()));
    av.push_back(nullptr);
    pid_t pid = fork();
    if (pid < 0) { close(in[0]); close(in[1]); close(out[0]); close(out[1]); close(ep[0]); close(ep[1]); return {false, "fork failed"}; }
    if (pid == 0) {
        setpgid(0, 0);
        { sigset_t none; sigemptyset(&none); sigprocmask(SIG_SETMASK, &none, nullptr); }   // children must not inherit the panel's blocked signals
        dup2(in[0], 0);
        dup2(out[1], 1);
        dup2(out[1], 2);
        int err = 0;
        if (chdir(dir.c_str()) != 0) err = errno;
        else { execvp(av[0], av.data()); err = errno; }
        if (write(ep[1], &err, sizeof err) < 0) {}
        _exit(127);
    }
    close(in[0]); close(out[1]); close(ep[1]);
    int child_err = 0;
    ssize_t er = read(ep[0], &child_err, sizeof child_err);   // returns 0 on successful exec (CLOEXEC)
    close(ep[0]);
    if (er > 0) {
        int st;
        waitpid(pid, &st, 0);
        close(in[1]); close(out[0]);
        return {false, native ? "binary not found" : (child_err == ENOENT ? "java not found - is a JRE installed?" : std::string("cannot start: ") + strerror(child_err))};
    }
    {
        std::lock_guard<std::mutex> ig(in_mu_);
        stdin_fd_ = in[1];
    }
    pid_ = pid;
    running_ = true;
    std::string joined;
    for (size_t i = 0; i < cmd.size(); i++) joined += (i ? " " : "") + cmd[i];
    append("[CreeperCrest] Started PID " + std::to_string(pid) + "  |  " + joined);
    auto self = shared_from_this();
    int rfd = out[0];
    std::thread([self, rfd]() { self->reader_loop(rfd); }).detach();
    return {true, "Started (PID " + std::to_string(pid) + ")"};
}

void ManagedServer::reader_loop(int fd) {
    std::string pending;
    char buf[8192];
    ssize_t n;
    auto flush_line = [&](std::string line) {
        while (!line.empty() && isspace((unsigned char)line.back())) line.pop_back();
        append(line);
        check_not_whitelisted(line);
    };
    while ((n = read(fd, buf, sizeof buf)) > 0) {
        pending.append(buf, (size_t)n);
        size_t p;
        while ((p = pending.find('\n')) != std::string::npos) {
            flush_line(pending.substr(0, p));
            pending.erase(0, p + 1);
        }
        if (pending.size() > 65536) { flush_line(pending); pending.clear(); }
    }
    if (!pending.empty()) flush_line(pending);
    close(fd);
    int pid = pid_.load(), st;
    if (pid > 0) waitpid(pid, &st, 0);
    {
        std::lock_guard<std::mutex> ig(in_mu_);
        if (stdin_fd_ >= 0) { close(stdin_fd_); stdin_fd_ = -1; }
    }
    {
        std::lock_guard<std::mutex> g(ex_mu_);
        running_ = false;
    }
    ex_cv_.notify_all();
}

std::pair<bool, std::string> ManagedServer::stop(int timeout_s) {
    std::lock_guard<std::mutex> pg(proc_mu_);
    if (!running_) return {false, "Not running"};
    {
        std::lock_guard<std::mutex> ig(in_mu_);
        if (stdin_fd_ >= 0) { ssize_t w = write(stdin_fd_, "stop\n", 5); (void)w; }
    }
    std::unique_lock<std::mutex> lk(ex_mu_);
    if (!ex_cv_.wait_for(lk, std::chrono::seconds(timeout_s), [&] { return !running_.load(); })) {
        kill(pid_.load(), SIGKILL);
        ex_cv_.wait(lk, [&] { return !running_.load(); });
    }
    lk.unlock();
    append("[CreeperCrest] Server stopped");
    return {true, "Stopped"};
}

std::pair<bool, std::string> ManagedServer::restart() {
    if (is_running()) {
        auto r = stop();
        if (!r.first) return r;
    }
    return start();
}

std::pair<bool, std::string> ManagedServer::send_command(const std::string &cmd) {
    if (!is_running()) return {false, "Not running"};
    std::string line = cmd;
    while (!line.empty() && isspace((unsigned char)line.back())) line.pop_back();
    {
        std::lock_guard<std::mutex> ig(in_mu_);
        if (stdin_fd_ < 0) return {false, "Not running"};
        std::string data = line + "\n";
        if (write(stdin_fd_, data.data(), data.size()) != (ssize_t)data.size()) return {false, strerror(errno)};
    }
    append("> " + cmd);
    return {true, "Sent"};
}

static bool ci_find_not_whitelisted(const std::string &line) {
    std::string l = line;
    for (auto &c : l) c = (char)tolower((unsigned char)c);
    size_t p = 0;
    while ((p = l.find("not", p)) != std::string::npos) {
        size_t q = p + 3;
        if (q >= l.size() || !isspace((unsigned char)l[q])) { p++; continue; }
        while (q < l.size() && isspace((unsigned char)l[q])) q++;
        if (l.compare(q, 5, "white") == 0) {
            q += 5;
            if (q < l.size() && l[q] == '-') q++;
            if (l.compare(q, 6, "listed") == 0) return true;
        }
        p++;
    }
    return false;
}

void ManagedServer::check_not_whitelisted(const std::string &line) {
    if (!ci_find_not_whitelisted(line)) return;
    static const std::regex name_res[3] = {std::regex("name=([A-Za-z0-9_.]{1,32})[,)]"),
                                           std::regex("Disconnecting ([A-Za-z0-9_.]{1,32}) \\("),
                                           std::regex("([A-Za-z0-9_.]{1,32})\\[/[^\\]]+\\] lost connection")};
    static const std::regex addr_re("/((?:\\d{1,3}\\.){3}\\d{1,3}|[0-9a-fA-F:]{3,39}):\\d+");
    std::string name, ip;
    std::smatch m;
    for (auto &r : name_res) if (std::regex_search(line, m, r)) { name = m[1]; break; }
    if (std::regex_search(line, m, addr_re)) ip = m[1];
    bool ban_name = false, ban_ip = false;
    {
        std::lock_guard<std::mutex> g(ban_mu);
        if (!name.empty() && !banned.count(name)) { banned.insert(name); ban_name = true; }
        if (!ip.empty() && !banned.count(ip) && proc::ip_is_global(ip)) { banned.insert(ip); ban_ip = true; }   // never ban LAN/proxy addresses
    }
    if (ban_name) send_command("ban " + name + " Not whitelisted");
    if (ban_ip) send_command("ban-ip " + ip + " Not whitelisted");
}

Value ManagedServer::status() {
    Value c = cfg();
    int64_t legacy = c.num("memory_mb", 1024);
    bool run = is_running();
    int pid = pid_.load();
    Value o = Value::object();
    o["id"] = id;
    o["name"] = c.str("name", id);
    o["running"] = run;
    o["pid"] = run ? Value((int64_t)pid) : Value();
    o["memory_min_mb"] = c.num("memory_min_mb", legacy);
    o["memory_max_mb"] = c.num("memory_max_mb", legacy);
    o["directory"] = c.str("directory");
    o["jar"] = c.str("jar", "server.jar");
    o["runtime"] = c.str("runtime", "java");
    o["allow_offline"] = c.flag("allow_offline");
    o["extra_args"] = c.str("extra_args");
    o["autostart"] = c.flag("autostart");
    o["resource_pack"] = c.str("resource_pack");
    o["resource_pack_sha1"] = c.str("resource_pack_sha1");
    o["resource_pack_required"] = c.flag("resource_pack_required");
    o["resource_pack_prompt"] = c.str("resource_pack_prompt");
    Value cpu, ram, hu, ht, ygc, fgc;
    if (run) {
        double p = sysinfo::pid_cpu(pid);
        if (p >= 0) cpu = p;
        int64_t r = sysinfo::pid_rss_mb(pid);
        if (r >= 0) ram = r;
        // jstat cannot attach when perf data is disabled (the default JVM flags do that), so skip the JVM launch then
        bool java = c.str("runtime", "java") != "native";
        if (java && c.str("extra_args").find("PerfDisableSharedMem") == std::string::npos) {
            int64_t now = (int64_t)time(nullptr);
            if (now - jstat_t_ >= 15) {
                jstat_t_ = now;
                jstat_ = Value();
                std::string out;
                if (proc::run_capture({"jstat", "-gc", std::to_string(pid)}, &out, nullptr, 3000) == 0) {
                    auto lines = srvutil::split_lines(out);
                    if (lines.size() >= 2) {
                        auto v = split_ws(lines.back());
                        if (v.size() >= 15) {
                            double s0c = atof(v[0].c_str()), s1c = atof(v[1].c_str()), s0u = atof(v[2].c_str()), s1u = atof(v[3].c_str());
                            double ec = atof(v[4].c_str()), eu = atof(v[5].c_str()), oc = atof(v[6].c_str()), ou = atof(v[7].c_str());
                            Value j = Value::object();
                            j["hu"] = fsu::round_to((s0u + s1u + eu + ou) / 1024, 1);
                            j["ht"] = fsu::round_to((s0c + s1c + ec + oc) / 1024, 1);
                            j["ygc"] = (int64_t)atof(v[12].c_str());
                            j["fgc"] = (int64_t)atof(v[14].c_str());
                            jstat_ = j;
                        }
                    }
                }
            }
            if (jstat_.is_obj()) {
                hu = jstat_.find("hu")->d; ht = jstat_.find("ht")->d;
                ygc = jstat_.num("ygc"); fgc = jstat_.num("fgc");
            }
        }
    }
    o["cpu_pct"] = cpu;
    o["ram_mb"] = ram;
    o["heap_used_mb"] = hu;
    o["heap_total_mb"] = ht;
    o["ygc"] = ygc;
    o["fgc"] = fgc;
    return o;
}
