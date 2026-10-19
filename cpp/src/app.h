#pragma once
#include "http.h"
#include "json.h"
#include <mutex>
#include <string>

namespace app {

struct Config {
    std::string base_dir;
    json::Value raw;                 // config.json as loaded (servers, backup_dir, ...)
    std::string host = "0.0.0.0";
    int port = 8080;
    int refresh_interval = 5;
    bool lan_mode = false, auth_disabled = false;
    std::vector<std::string> trusted_proxies, allowed_hosts;
    std::string tls_cert, tls_key;
};

extern Config cfg;
bool load_config(const std::string &base_dir);
bool save_config();
void handle(http::Request &rq, http::Response &rs);   // top-level request handler
// shared response helpers (apply the security headers)
void send_json(const http::Request &rq, http::Response &rs, const json::Value &v, int code = 200);
void err_json(const http::Request &rq, http::Response &rs, const std::string &msg, int code);
void security_headers(const http::Request &rq, http::Response &rs);
std::string client_ip(const http::Request &rq);
std::string expand_home(const std::string &p);
std::string default_jvm_args();

} // namespace app
