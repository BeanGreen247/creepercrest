// One managed game server: process lifecycle, console ring buffer, config hardening, whitelist enforcement.
#pragma once
#include "json.h"
#include <atomic>
#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <vector>

class ManagedServer : public std::enable_shared_from_this<ManagedServer> {
public:
    ManagedServer(std::string id, json::Value cfg);

    const std::string id;

    // config (copy in/out so callers never hold a reference across threads)
    json::Value cfg() const;
    void set_cfg(const json::Value &c);
    std::string directory() const;   // ~ expanded

    bool is_running() const { return running_.load(); }
    std::pair<bool, std::string> start();
    std::pair<bool, std::string> stop(int timeout_s = 30);
    std::pair<bool, std::string> restart();
    std::pair<bool, std::string> send_command(const std::string &cmd);
    void append(const std::string &line);                       // timestamped console line
    // Returns lines newer than `since`; full=true when the caller must replace its buffer.
    void logs_since(long since, std::vector<std::string> &lines, long &seq, bool &full) const;
    json::Value status();

    std::set<std::string> banned;     // names/IPs already auto-banned this run (guarded by ban_mu)
    std::mutex ban_mu;

private:
    void reader_loop(int fd);
    void check_not_whitelisted(const std::string &line);

    mutable std::mutex cfg_mu_;
    json::Value cfg_;
    std::mutex proc_mu_;               // serialises start/stop/restart
    std::mutex in_mu_;                 // guards stdin_fd_
    int stdin_fd_ = -1;
    std::atomic<int> pid_{0};
    std::atomic<bool> running_{false};
    std::mutex ex_mu_;
    std::condition_variable ex_cv_;
    mutable std::mutex log_mu_;
    std::deque<std::string> logs_;
    long log_seq_ = 0;
    // jstat cache (only consulted for java servers that can expose perf data)
    int64_t jstat_t_ = 0;
    json::Value jstat_;
};

namespace srvutil {
// server.properties helpers
bool set_properties(const std::string &dir, const std::vector<std::pair<std::string, std::string>> &updates);
void harden(const std::string &dir, bool allow_offline);        // throws std::runtime_error on properties failure
std::string prop_escape(const std::string &v);
std::string to_bool(const std::string &v);
// Validates wizard properties; returns false with err set. out holds ready-to-write pairs.
bool validate_props(const json::Value &raw, std::vector<std::pair<std::string, std::string>> &out, std::string &err);
bool check_pack(const std::string &url, const std::string &sha1, const std::string &prompt, std::string &err);
void apply_resource_pack(json::Value &scfg);                   // may add resource_pack_id; writes server.properties
bool valid_uuid(const std::string &s);
bool valid_player(const std::string &s);
std::string uuid4();
std::pair<bool, std::string> whitelist_remove_uuid(ManagedServer &srv, const std::string &uuid);
json::Value whitelist_entries(ManagedServer &srv);
int host_java_major();                                           // 0 if unknown
std::string default_jvm_args();
} // namespace srvutil
