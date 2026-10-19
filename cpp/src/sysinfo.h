// Host and per-process resource sampling, taken on demand (no background thread, no idle wakeups).
#pragma once
#include "json.h"
#include <vector>

namespace sysinfo {

json::Value host();                          // cpu_pct, ram_*, disk_* (null where unknown)
// Per-process CPU% since the previous call for that pid; -1 when there is no baseline yet.
double pid_cpu(int pid);
int64_t pid_rss_mb(int pid);                 // -1 if unknown
void forget_except(const std::vector<int> &live_pids);

} // namespace sysinfo
