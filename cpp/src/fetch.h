// Blocking HTTP/HTTPS GET client with redirects, size limits and an SSRF guard.
#pragma once
#include <functional>
#include <string>

namespace fetch {

struct Policy {
    bool allow_private = false;        // LAN mode / allow_private_fetch
};

// Streams the body into sink. progress(done,total) is optional (total 0 if unknown). Throws std::runtime_error.
void get(const std::string &url, const Policy &pol, const std::function<void(const char *, size_t)> &sink,
         const std::function<void(int64_t, int64_t)> &progress = nullptr, int64_t limit = 0);
std::string get_string(const std::string &url, const Policy &pol, int64_t limit = 8 << 20);

} // namespace fetch
