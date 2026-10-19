#include "auth.h"
#include "util.h"
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <fcntl.h>
#include <unistd.h>
#include <cstdio>
#include <ctime>
#include <map>
#include <mutex>
#include <regex>

namespace auth {

std::string users_file = "users.json";

static const uint64_t SCRYPT_N = 1u << 15, SCRYPT_R = 8, SCRYPT_P = 1;

json::Value load_users() {
    FILE *f = fopen(users_file.c_str(), "rb");
    if (!f) return json::Value::object();
    std::string s;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) s.append(buf, n);
    fclose(f);
    try {
        json::Value v = json::parse(s);
        return v.is_obj() ? v : json::Value::object();
    } catch (...) {
        return json::Value::object();
    }
}

bool save_users(const json::Value &users) {
    std::string tmp = users_file + ".tmp", data = json::dump(users, 2);
    int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;
    bool ok = write(fd, data.data(), data.size()) == (ssize_t)data.size();
    ok = (close(fd) == 0) && ok;
    return ok && rename(tmp.c_str(), users_file.c_str()) == 0;
}

static bool scrypt(const std::string &pw, const std::string &salt, uint64_t n, uint64_t r, uint64_t p,
                   uint64_t maxmem, std::string &out) {
    out.assign(32, '\0');
    return EVP_PBE_scrypt(pw.data(), pw.size(), (const unsigned char *)salt.data(), salt.size(), n, r, p, maxmem,
                          (unsigned char *)&out[0], out.size()) == 1;
}

std::string hash_password(const std::string &pw) {
    std::string salt = util::random_bytes(16), h;
    if (!scrypt(pw, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, 128u << 20, h)) abort();
    return "scrypt$" + std::to_string(SCRYPT_N) + "$" + std::to_string(SCRYPT_R) + "$" + std::to_string(SCRYPT_P) +
           "$" + util::b64_encode(salt) + "$" + util::b64_encode(h);
}

bool verify_password(const std::string &pw, const std::string &stored) {
    std::vector<std::string> f;
    size_t pos = 0;
    while (true) {
        size_t d = stored.find('$', pos);
        f.push_back(stored.substr(pos, d == std::string::npos ? d : d - pos));
        if (d == std::string::npos) break;
        pos = d + 1;
    }
    uint64_t n, r, p;
    if (f.size() == 3) { n = 1u << 14; r = 8; p = 1; }   // original format
    else if (f.size() == 6) {
        char *e;
        n = strtoull(f[1].c_str(), &e, 10); if (*e) return false;
        r = strtoull(f[2].c_str(), &e, 10); if (*e) return false;
        p = strtoull(f[3].c_str(), &e, 10); if (*e) return false;
    } else return false;
    if (n > (1u << 17) || n < 2 || (n & (n - 1)) || r == 0 || r > 32 || p == 0 || p > 16) return false;
    std::string salt, want, h;
    if (!util::b64_decode(f[f.size() - 2], salt) || !util::b64_decode(f.back(), want)) return false;
    if (!scrypt(pw, salt, n, r, p, 256u << 20, h)) return false;
    return util::ct_equal(h, want);
}

std::string new_totp_secret() { return util::b32_encode(util::random_bytes(20)); }

std::string totp_at(const std::string &secret, uint64_t counter) {
    std::string key;
    util::b32_decode(secret, key);
    unsigned char msg[8], mac[20];
    for (int i = 7; i >= 0; i--) { msg[i] = counter & 255; counter >>= 8; }
    unsigned len = sizeof mac;
    HMAC(EVP_sha1(), key.data(), (int)key.size(), msg, 8, mac, &len);
    int o = mac[19] & 15;
    uint32_t v = (((uint32_t)mac[o] << 24) | (mac[o + 1] << 16) | (mac[o + 2] << 8) | mac[o + 3]) & 0x7fffffff;
    char out[8];
    snprintf(out, sizeof out, "%06u", v % 1000000);
    return out;
}

static std::mutex totp_mu;
static std::map<std::string, uint64_t> totp_last;   // username -> last accepted step (blocks replay)

uint64_t verify_totp(const std::string &secret, const std::string &code_in, const std::string &user) {
    std::string code;
    for (char c : code_in) if (!isspace((unsigned char)c)) code += c;
    if (code.size() != 6) return 0;
    for (char c : code) if (c < '0' || c > '9') return 0;
    uint64_t now = (uint64_t)time(nullptr) / 30, found = 0, last;
    {
        std::lock_guard<std::mutex> g(totp_mu);
        auto it = totp_last.find(user);
        last = it == totp_last.end() ? 0 : it->second;
    }
    for (uint64_t step : {now - 1, now, now + 1})
        if (util::ct_equal(totp_at(secret, step), code) && step > last) found = step;
    return found;
}

bool consume_totp(const std::string &user, uint64_t step) {
    std::lock_guard<std::mutex> g(totp_mu);
    uint64_t &last = totp_last[user];
    if (step <= last) return false;
    last = step;
    return true;
}

std::string totp_uri(const std::string &user, const std::string &secret) {
    return "otpauth://totp/CreeperCrest:" + util::url_quote(user) + "?secret=" + secret +
           "&issuer=CreeperCrest&algorithm=SHA1&digits=6&period=30";
}

bool valid_username(const std::string &name) {
    if (name.size() < 2 || name.size() > 32) return false;
    auto ok0 = [](char c) { return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'); };
    if (!ok0(name[0])) return false;
    for (char c : name) if (!ok0(c) && c != '_' && c != '.' && c != '-') return false;
    return true;
}

} // namespace auth
