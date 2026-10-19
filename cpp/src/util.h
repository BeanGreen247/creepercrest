// Small encoding / crypto-random helpers shared across the panel.
#pragma once
#include <cstdint>
#include <string>

namespace util {

std::string b32_encode(const std::string &raw);               // RFC 4648, no padding
bool b32_decode(const std::string &s, std::string &out);      // tolerates missing padding, case-insensitive
std::string b64_encode(const std::string &raw);               // standard alphabet, padded
bool b64_decode(const std::string &s, std::string &out);
std::string b64url(const std::string &raw);                   // url-safe, no padding (token_urlsafe)
std::string random_bytes(size_t n);                           // from the OS CSPRNG
std::string token_urlsafe(size_t nbytes);                     // like Python secrets.token_urlsafe
bool ct_equal(const std::string &a, const std::string &b);    // constant-time compare
std::string url_quote(const std::string &s);                  // like urllib.parse.quote (safe="/")
std::string html_escape(const std::string &s);

} // namespace util
