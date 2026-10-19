// Subprocess capture and IP classification helpers.
#pragma once
#include <string>
#include <vector>

namespace proc {

// Runs argv (no shell), returns exit code (-1 on spawn failure / timeout). stdout and stderr go to *out / *err.
int run_capture(const std::vector<std::string> &argv, std::string *out, std::string *err, int timeout_ms);
bool ip_is_global(const std::string &ip);   // mirrors Python ipaddress.is_global for the common cases
bool ip_is_private_or_loopback(const std::string &ip);

} // namespace proc
