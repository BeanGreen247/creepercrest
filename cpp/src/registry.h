// Server registry + persisted per-server config (shares app::cfg.raw["servers"]).
#pragma once
#include "fetch.h"
#include "server.h"
#include <memory>
#include <string>
#include <vector>

namespace registry {

void load();                                                       // build servers from config.json
std::shared_ptr<ManagedServer> get(const std::string &sid);        // nullptr if missing
std::vector<std::shared_ptr<ManagedServer>> all();                 // config order
void add(const std::string &sid, const json::Value &scfg);         // persists
bool remove(const std::string &sid);                               // persists; false if missing
void persist(const std::string &sid, const json::Value &scfg);     // after a config edit
void stop_all();
fetch::Policy fetch_policy();
std::string cfg_str(const char *key, const std::string &def = "");
json::Value cfg_get(const char *key);                                // copy of a top-level config value
void cfg_set(const std::string &key, const json::Value &v);        // set + save config.json
int64_t cfg_num(const char *key, int64_t def = 0);

} // namespace registry
