#pragma once
#include "http.h"
#include <string>

namespace api {
bool handle(http::Request &rq, http::Response &rs, const std::string &client_ip);   // authenticated routes; false = not mine
bool handle_head(http::Request &rq, http::Response &rs);                           // public resource-pack HEAD
}
