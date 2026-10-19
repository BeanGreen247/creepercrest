#include "util.h"
#include <openssl/crypto.h>
#include <openssl/rand.h>
#include <cstdio>
#include <cstdlib>

namespace util {

static const char B32[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string b32_encode(const std::string &raw) {
    std::string out;
    uint32_t buf = 0;
    int bits = 0;
    for (unsigned char c : raw) {
        buf = (buf << 8) | c;
        bits += 8;
        while (bits >= 5) { out += B32[(buf >> (bits - 5)) & 31]; bits -= 5; }
    }
    if (bits) out += B32[(buf << (5 - bits)) & 31];
    return out;
}

bool b32_decode(const std::string &s, std::string &out) {
    out.clear();
    uint32_t buf = 0;
    int bits = 0;
    for (char c : s) {
        if (c == '=') break;
        int v;
        if (c >= 'A' && c <= 'Z') v = c - 'A';
        else if (c >= 'a' && c <= 'z') v = c - 'a';
        else if (c >= '2' && c <= '7') v = c - '2' + 26;
        else return false;
        buf = (buf << 5) | v;
        bits += 5;
        if (bits >= 8) { out += (char)((buf >> (bits - 8)) & 255); bits -= 8; }
    }
    return true;
}

std::string b64_encode(const std::string &raw) {
    std::string out;
    size_t n = raw.size(), i = 0;
    for (; i + 2 < n; i += 3) {
        uint32_t v = ((unsigned char)raw[i] << 16) | ((unsigned char)raw[i + 1] << 8) | (unsigned char)raw[i + 2];
        out += B64[v >> 18]; out += B64[(v >> 12) & 63]; out += B64[(v >> 6) & 63]; out += B64[v & 63];
    }
    if (i + 1 == n) {
        uint32_t v = (unsigned char)raw[i] << 16;
        out += B64[v >> 18]; out += B64[(v >> 12) & 63]; out += "==";
    } else if (i + 2 == n) {
        uint32_t v = ((unsigned char)raw[i] << 16) | ((unsigned char)raw[i + 1] << 8);
        out += B64[v >> 18]; out += B64[(v >> 12) & 63]; out += B64[(v >> 6) & 63]; out += '=';
    }
    return out;
}

bool b64_decode(const std::string &s, std::string &out) {
    out.clear();
    uint32_t buf = 0;
    int bits = 0;
    for (char c : s) {
        if (c == '=') break;
        int v;
        if (c >= 'A' && c <= 'Z') v = c - 'A';
        else if (c >= 'a' && c <= 'z') v = c - 'a' + 26;
        else if (c >= '0' && c <= '9') v = c - '0' + 52;
        else if (c == '+') v = 62;
        else if (c == '/') v = 63;
        else return false;
        buf = (buf << 6) | v;
        bits += 6;
        if (bits >= 8) { out += (char)((buf >> (bits - 8)) & 255); bits -= 8; }
    }
    return true;
}

std::string b64url(const std::string &raw) {
    std::string s = b64_encode(raw);
    for (auto &c : s) { if (c == '+') c = '-'; else if (c == '/') c = '_'; }
    while (!s.empty() && s.back() == '=') s.pop_back();
    return s;
}

std::string random_bytes(size_t n) {
    std::string r(n, '\0');
    if (RAND_bytes((unsigned char *)&r[0], (int)n) != 1) abort();
    return r;
}

std::string token_urlsafe(size_t nbytes) { return b64url(random_bytes(nbytes)); }

bool ct_equal(const std::string &a, const std::string &b) {
    return a.size() == b.size() && CRYPTO_memcmp(a.data(), b.data(), a.size()) == 0;
}

std::string url_quote(const std::string &s) {
    std::string o;
    for (unsigned char c : s) {
        if (isalnum(c) || c == '_' || c == '.' || c == '-' || c == '~' || c == '/') o += (char)c;
        else { char b[4]; snprintf(b, sizeof b, "%%%02X", c); o += b; }
    }
    return o;
}

std::string html_escape(const std::string &s) {
    std::string o;
    for (char c : s) {
        switch (c) {
        case '&': o += "&amp;"; break;
        case '<': o += "&lt;"; break;
        case '>': o += "&gt;"; break;
        case '"': o += "&quot;"; break;
        case '\'': o += "&#x27;"; break;
        default: o += c;
        }
    }
    return o;
}

} // namespace util
