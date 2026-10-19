#include "backup.h"
#include "app.h"
#include "fsu.h"
#include "registry.h"
#include "zipx.h"
#include <dirent.h>
#include <sys/stat.h>
#include <algorithm>
#include <ctime>
#include <regex>
#include <set>
#include <thread>
#include <unistd.h>

using json::Value;

namespace backup {

static const std::set<std::string> SKIP_DIRS = {"logs", "crash-reports", "debug"};

std::string dir() { return fsu::expand_home(registry::cfg_str("backup_dir", "~/mc-backups")); }

static bool name_ok(const std::string &n) {
    static const std::regex re("^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\\.zip$");
    return std::regex_match(n, re) && n.find("..") == std::string::npos;
}

std::string path_of(const std::string &fname) {
    if (!name_ok(fname)) return "";
    std::string bak = fsu::resolve(dir());
    std::string path = fsu::resolve(bak + "/" + fname);
    return fsu::dir_name(path) == bak ? path : "";
}

static void walk(const std::string &root, const std::string &rel, std::vector<std::pair<std::string, std::string>> &out, bool skip) {
    std::string cur = rel.empty() ? root : root + "/" + rel;
    DIR *d = opendir(cur.c_str());
    if (!d) return;
    std::vector<std::string> names;
    while (auto *e = readdir(d)) {
        std::string n = e->d_name;
        if (n != "." && n != "..") names.push_back(n);
    }
    closedir(d);
    for (auto &n : names) {
        std::string r = rel.empty() ? n : rel + "/" + n, full = root + "/" + r;
        struct stat st;
        if (stat(full.c_str(), &st) != 0) continue;
        if (S_ISDIR(st.st_mode)) {
            if (skip && SKIP_DIRS.count(n)) continue;
            walk(root, r, out, skip);
        } else if (S_ISREG(st.st_mode)) out.emplace_back(full, r);
    }
}

static void prune(const std::string &sid, const std::string &bak) {
    int64_t limit = registry::cfg_num("max_backups", 0);
    if (limit <= 0) return;
    std::regex pat("^" + sid + "-\\d{8}-\\d{6}\\.zip$");
    std::vector<std::string> mine;
    for (auto &n : fsu::list_names(bak)) if (std::regex_match(n, pat)) mine.push_back(n);
    std::sort(mine.begin(), mine.end());
    for (size_t i = 0; i + (size_t)limit < mine.size(); i++) unlink((bak + "/" + mine[i]).c_str());
}

Value create(const std::string &sid, jobs::Job *job) {
    auto srv = registry::get(sid);
    if (!srv) throw std::runtime_error("Server not found");
    std::string src = srv->directory(), bak = dir();
    fsu::mkdirs(bak);
    std::string fname = sid + "-" + fsu::now_str("%Y%m%d-%H%M%S") + ".zip", fpath = bak + "/" + fname;
    std::vector<std::pair<std::string, std::string>> files;
    walk(src, "", files, true);
    if (job) job->set_unit("bytes", 0);
    try {
        zipx::create(fpath, files, [&](int64_t d, int64_t t) { if (job) { job->total = t; job->done = d; } });
    } catch (...) {
        unlink(fpath.c_str());
        throw;
    }
    double mb = fsu::round_to((double)fsu::file_size(fpath) / 1024 / 1024, 2);
    srv->append("[CreeperCrest] Backup saved -> " + fpath + "  (" + std::to_string(mb).substr(0, std::to_string(mb).find('.') + 3) + " MB)");
    prune(sid, bak);
    Value r = Value::object();
    r["file"] = fname;
    r["path"] = fpath;
    r["size_mb"] = mb;
    return r;
}

std::string restore(const std::string &fname, const std::string &sid, jobs::Job *job) {
    auto srv = registry::get(sid);
    if (!srv) throw std::runtime_error("Server not found");
    if (srv->is_running()) throw std::runtime_error("Stop the server before restoring");
    std::string fpath = path_of(fname);
    if (fpath.empty() || !fsu::is_file(fpath)) throw std::runtime_error("Backup file not found");
    std::string dest = srv->directory();
    if (dest.empty() || !fsu::is_dir(dest)) throw std::runtime_error("Server directory not found");
    if (job) job->set_unit("files", 0);
    zipx::extract(fpath, dest, [&](int64_t d, int64_t t) { if (job) { job->total = t; job->done = d; } }, true, registry::extract_limit(dest));
    srv->append("[CreeperCrest] Restored from backup: " + fname);
    return "Restored " + fname;
}

Value list() {
    Value out = Value::array();
    std::string bak = dir();
    if (!fsu::is_dir(bak)) return out;
    auto names = fsu::list_names(bak);
    std::sort(names.rbegin(), names.rend());
    for (auto &n : names) {
        if (!name_ok(n)) continue;
        std::string fp = bak + "/" + n;
        Value o = Value::object();
        o["name"] = n;
        o["size_mb"] = fsu::round_to((double)fsu::file_size(fp) / 1024 / 1024, 2);
        o["created"] = fsu::fmt_time(fsu::mtime(fp), "%Y-%m-%d %H:%M");
        out.push(o);
    }
    return out;
}

static const char *WEEKDAYS[] = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"};

void start_scheduler() {
    std::thread([] {
        std::string last_fired;
        while (true) {
            sleep(60);
            Value sched;
            sched = registry::cfg_get("auto_backup");
            if (!sched.flag("enabled")) continue;
            time_t t = time(nullptr);
            struct tm tm;
            localtime_r(&t, &tm);
            std::string day = WEEKDAYS[(tm.tm_wday + 6) % 7], want = sched.str("day", "sunday");
            for (auto &c : want) c = (char)tolower((unsigned char)c);
            if (day != want) continue;
            if (tm.tm_hour != sched.num("hour", 3) || tm.tm_min != sched.num("minute", 0)) continue;
            char wk[16];
            strftime(wk, sizeof wk, "%G-%V", &tm);
            std::string key = std::string(wk) + day;
            if (key == last_fired) continue;
            last_fired = key;
            const Value *only = sched.find("servers");   // absent = every server
            for (auto &s : registry::all()) {
                if (only && only->is_arr()) {
                    bool in = false;
                    for (auto &x : only->a) if (x.is_str() && x.s == s->id) in = true;
                    if (!in) continue;
                }
                try {
                    Value r = create(s->id, nullptr);
                    printf("[auto-backup] %s -> %s (%g MB)\n", s->id.c_str(), r.str("file").c_str(), r.find("size_mb")->d);
                } catch (const std::exception &e) {
                    printf("[auto-backup] %s failed: %s\n", s->id.c_str(), e.what());
                }
                fflush(stdout);
            }
        }
    }).detach();
}

} // namespace backup
