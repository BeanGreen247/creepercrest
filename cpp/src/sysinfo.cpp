#include "sysinfo.h"
#include "fsu.h"
#include <sys/statvfs.h>
#include <ctime>
#include <map>
#include <mutex>
#include <cstdio>
#include <cstring>

namespace sysinfo {

static std::mutex mu;
static bool have_prev = false;
static uint64_t prev_idle = 0, prev_total = 0, cur_total = 0;
static int64_t last_sample = 0;
static double cpu_pct = -1;
static std::map<int, std::pair<uint64_t, uint64_t>> pid_prev;   // pid -> (proc jiffies, system total)

static bool read_stat(uint64_t &idle, uint64_t &total) {
    FILE *f = fopen("/proc/stat", "r");
    if (!f) return false;
    unsigned long long v[10] = {0};
    int n = fscanf(f, "cpu %llu %llu %llu %llu %llu %llu %llu %llu", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7]);
    fclose(f);
    if (n < 5) return false;
    idle = v[3] + v[4];
    total = v[0] + v[1] + v[2] + v[3] + v[4] + v[5] + v[6] + v[7];
    return true;
}

static void sample_locked() {
    int64_t now = (int64_t)time(nullptr);
    if (now - last_sample < 2 && last_sample) return;
    uint64_t idle, total;
    if (!read_stat(idle, total)) return;
    if (have_prev && now - last_sample > 60) have_prev = false;   // stale baseline: do not average over idle gaps
    if (have_prev && total > prev_total) cpu_pct = fsu::round_to(100.0 * (1.0 - (double)(idle - prev_idle) / (double)(total - prev_total)), 1);
    else cpu_pct = -1;
    prev_idle = idle;
    prev_total = total;
    cur_total = total;
    have_prev = true;
    last_sample = now;
}

json::Value host() {
    json::Value o = json::Value::object();
    {
        std::lock_guard<std::mutex> g(mu);
        sample_locked();
        o["cpu_pct"] = cpu_pct < 0 ? json::Value() : json::Value(cpu_pct);
    }
    long long total = 0, avail = 0;
    if (FILE *f = fopen("/proc/meminfo", "r")) {
        char line[256];
        while (fgets(line, sizeof line, f)) {
            if (!strncmp(line, "MemTotal:", 9)) total = atoll(line + 9);
            else if (!strncmp(line, "MemAvailable:", 13)) avail = atoll(line + 13);
        }
        fclose(f);
    }
    o["ram_used_mb"] = total ? json::Value((int64_t)((total - avail) / 1024)) : json::Value();
    o["ram_total_mb"] = total ? json::Value((int64_t)(total / 1024)) : json::Value();
    struct statvfs st;
    if (statvfs("/", &st) == 0) {
        o["disk_used_gb"] = fsu::round_to((double)(st.f_blocks - st.f_bavail) * st.f_frsize / 1073741824.0, 1);
        o["disk_total_gb"] = fsu::round_to((double)st.f_blocks * st.f_frsize / 1073741824.0, 1);
    } else {
        o["disk_used_gb"] = json::Value();
        o["disk_total_gb"] = json::Value();
    }
    return o;
}

double pid_cpu(int pid) {
    std::lock_guard<std::mutex> g(mu);
    sample_locked();
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    std::string data;
    if (!fsu::read_file(path, data)) return -1;
    size_t p = data.rfind(')');
    if (p == std::string::npos) return -1;
    unsigned long long ut = 0, st = 0;
    // fields after ")": state is field 3; utime/stime are fields 14/15 -> offsets 11/12 after the state token
    if (sscanf(data.c_str() + p + 2, "%*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %llu %llu", &ut, &st) != 2) return -1;
    uint64_t proc_time = ut + st;
    double out = -1;
    auto it = pid_prev.find(pid);
    if (it != pid_prev.end() && cur_total > it->second.second) {
        out = fsu::round_to(100.0 * (double)(proc_time - it->second.first) / (double)(cur_total - it->second.second), 1);
    }
    pid_prev[pid] = {proc_time, cur_total};
    return out;
}

int64_t pid_rss_mb(int pid) {
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/status", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[256];
    int64_t mb = -1;
    while (fgets(line, sizeof line, f))
        if (!strncmp(line, "VmRSS:", 6)) { mb = atoll(line + 6) / 1024; break; }
    fclose(f);
    return mb;
}

void forget_except(const std::vector<int> &live) {
    std::lock_guard<std::mutex> g(mu);
    for (auto it = pid_prev.begin(); it != pid_prev.end();) {
        bool keep = false;
        for (int p : live) if (p == it->first) keep = true;
        it = keep ? std::next(it) : pid_prev.erase(it);
    }
}

} // namespace sysinfo
