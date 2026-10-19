#include "registry.h"
#include "app.h"
#include <mutex>

namespace registry {

static std::recursive_mutex mu;
static std::vector<std::pair<std::string, std::shared_ptr<ManagedServer>>> servers;

void load() {
    std::lock_guard<std::recursive_mutex> g(mu);
    servers.clear();
    if (auto *s = app::cfg.raw.find("servers"); s && s->is_obj())
        for (auto &kv : s->o) servers.emplace_back(kv.first, std::make_shared<ManagedServer>(kv.first, kv.second));
}

std::shared_ptr<ManagedServer> get(const std::string &sid) {
    std::lock_guard<std::recursive_mutex> g(mu);
    for (auto &p : servers) if (p.first == sid) return p.second;
    return nullptr;
}

std::vector<std::shared_ptr<ManagedServer>> all() {
    std::lock_guard<std::recursive_mutex> g(mu);
    std::vector<std::shared_ptr<ManagedServer>> v;
    for (auto &p : servers) v.push_back(p.second);
    return v;
}

void persist(const std::string &sid, const json::Value &scfg) {
    std::lock_guard<std::recursive_mutex> g(mu);
    app::cfg.raw["servers"][sid] = scfg;
    app::save_config();
}

void add(const std::string &sid, const json::Value &scfg) {
    std::lock_guard<std::recursive_mutex> g(mu);
    persist(sid, scfg);
    servers.emplace_back(sid, std::make_shared<ManagedServer>(sid, scfg));
}

bool remove(const std::string &sid) {
    std::lock_guard<std::recursive_mutex> g(mu);
    bool found = false;
    for (auto it = servers.begin(); it != servers.end(); ++it)
        if (it->first == sid) { servers.erase(it); found = true; break; }
    if (!found) return false;
    app::cfg.raw["servers"].erase(sid);
    app::save_config();
    return true;
}

void stop_all() {
    for (auto &s : all()) {
        if (s->is_running()) {
            printf("  Stopping %s...\n", s->id.c_str());
            fflush(stdout);
            s->stop();
        }
    }
}

fetch::Policy fetch_policy() {
    fetch::Policy p;
    p.allow_private = app::cfg.lan_mode || app::cfg.raw.flag("allow_private_fetch");
    return p;
}

std::string cfg_str(const char *key, const std::string &def) {
    std::lock_guard<std::recursive_mutex> g(mu);
    return app::cfg.raw.str(key, def);
}
int64_t cfg_num(const char *key, int64_t def) {
    std::lock_guard<std::recursive_mutex> g(mu);
    return app::cfg.raw.num(key, def);
}

} // namespace registry

namespace registry {
json::Value cfg_get(const char *key) {
    std::lock_guard<std::recursive_mutex> g(mu);
    auto *v = app::cfg.raw.find(key);
    return v ? *v : json::Value();
}
void cfg_set(const std::string &key, const json::Value &v) {
    std::lock_guard<std::recursive_mutex> g(mu);
    app::cfg.raw[key] = v;
    app::save_config();
}
} // namespace registry
