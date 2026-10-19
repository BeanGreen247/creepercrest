#include "fetch.h"
#include "proc.h"
#include <arpa/inet.h>
#include <netdb.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cerrno>
#include <strings.h>
#include <algorithm>
#include <cstring>
#include <memory>
#include <stdexcept>

namespace fetch {

namespace {

struct Url { std::string scheme, host, port, path; };

Url parse_url(const std::string &u) {
    Url r;
    size_t s = u.find("://");
    if (s == std::string::npos) throw std::runtime_error("URL must start with http:// or https://");
    r.scheme = u.substr(0, s);
    for (auto &c : r.scheme) c = (char)tolower((unsigned char)c);
    if (r.scheme != "http" && r.scheme != "https") throw std::runtime_error("URL must start with http:// or https://");
    size_t a = s + 3, e = u.find_first_of("/?#", a);
    std::string auth = u.substr(a, e == std::string::npos ? e : e - a);
    r.path = e == std::string::npos ? "/" : u.substr(e);
    if (r.path[0] != '/') r.path = "/" + r.path;
    size_t h = r.path.find('#');
    if (h != std::string::npos) r.path.resize(h);
    if (auth.find('@') != std::string::npos) auth = auth.substr(auth.rfind('@') + 1);
    if (!auth.empty() && auth[0] == '[') {
        size_t b = auth.find(']');
        if (b == std::string::npos) throw std::runtime_error("invalid URL");
        r.host = auth.substr(1, b - 1);
        if (b + 1 < auth.size() && auth[b + 1] == ':') r.port = auth.substr(b + 2);
    } else {
        size_t c = auth.rfind(':');
        r.host = auth.substr(0, c);
        if (c != std::string::npos) r.port = auth.substr(c + 1);
    }
    if (r.host.empty()) throw std::runtime_error("invalid URL");
    if (r.port.empty()) r.port = r.scheme == "https" ? "443" : "80";
    return r;
}

struct Sock {
    int fd = -1;
    SSL *ssl = nullptr;
    ~Sock() { if (ssl) { SSL_shutdown(ssl); SSL_free(ssl); } if (fd >= 0) close(fd); }
    ssize_t rd(void *b, size_t n) {
        if (ssl) { int r = SSL_read(ssl, b, (int)n); return r > 0 ? r : (SSL_get_error(ssl, r) == SSL_ERROR_ZERO_RETURN ? 0 : -1); }
        return recv(fd, b, n, 0);
    }
    bool wr(const std::string &s) {
        const char *p = s.data();
        size_t n = s.size();
        while (n) {
            int w = ssl ? SSL_write(ssl, p, (int)n) : (int)send(fd, p, n, MSG_NOSIGNAL);
            if (w <= 0) return false;
            p += w; n -= (size_t)w;
        }
        return true;
    }
};

SSL_CTX *client_ctx() {
    static SSL_CTX *ctx = [] {
        SSL_CTX *c = SSL_CTX_new(TLS_client_method());
        SSL_CTX_set_min_proto_version(c, TLS1_2_VERSION);
        SSL_CTX_set_default_verify_paths(c);
        SSL_CTX_set_verify(c, SSL_VERIFY_PEER, nullptr);
        return c;
    }();
    return ctx;
}

void connect_to(Sock &s, const Url &u, const Policy &pol) {
    struct addrinfo hints{}, *res = nullptr;
    hints.ai_socktype = SOCK_STREAM;
    int gai = getaddrinfo(u.host.c_str(), u.port.c_str(), &hints, &res);
    if (gai != 0 || !res) throw std::runtime_error("cannot resolve " + u.host);
    std::unique_ptr<addrinfo, void (*)(addrinfo *)> g(res, freeaddrinfo);
    if (!pol.allow_private) {
        for (auto *a = res; a; a = a->ai_next) {
            char ip[INET6_ADDRSTRLEN] = "";
            if (a->ai_family == AF_INET) inet_ntop(AF_INET, &((sockaddr_in *)a->ai_addr)->sin_addr, ip, sizeof ip);
            else if (a->ai_family == AF_INET6) inet_ntop(AF_INET6, &((sockaddr_in6 *)a->ai_addr)->sin6_addr, ip, sizeof ip);
            if (!proc::ip_is_global(ip)) throw std::runtime_error("refusing to fetch from a private or internal address");
        }
    }
    std::string last = "connect failed";
    for (auto *a = res; a; a = a->ai_next) {   // connect to the addresses we just vetted (no second DNS lookup)
        int fd = socket(a->ai_family, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd < 0) continue;
        struct timeval tv{30, 0};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        if (connect(fd, a->ai_addr, a->ai_addrlen) == 0) { s.fd = fd; break; }
        last = strerror(errno);
        close(fd);
    }
    if (s.fd < 0) throw std::runtime_error(last);
    if (u.scheme == "https") {
        s.ssl = SSL_new(client_ctx());
        SSL_set_fd(s.ssl, s.fd);
        SSL_set_tlsext_host_name(s.ssl, u.host.c_str());
        SSL_set1_host(s.ssl, u.host.c_str());
        if (SSL_connect(s.ssl) != 1) throw std::runtime_error("TLS handshake failed for " + u.host);
    }
}

} // namespace

void get(const std::string &url0, const Policy &pol, const std::function<void(const char *, size_t)> &sink,
         const std::function<void(int64_t, int64_t)> &progress, int64_t limit) {
    std::string url = url0;
    for (int hop = 0; hop < 6; hop++) {
        Url u = parse_url(url);
        Sock s;
        connect_to(s, u, pol);
        std::string host = u.host.find(':') != std::string::npos ? "[" + u.host + "]" : u.host;
        if ((u.scheme == "https" && u.port != "443") || (u.scheme == "http" && u.port != "80")) host += ":" + u.port;
        if (!s.wr("GET " + u.path + " HTTP/1.1\r\nHost: " + host + "\r\nUser-Agent: CreeperCrest\r\n"
                  "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"))
            throw std::runtime_error("send failed");
        std::string buf;
        char tmp[16384];
        size_t he;
        while ((he = buf.find("\r\n\r\n")) == std::string::npos) {
            if (buf.size() > 65536) throw std::runtime_error("bad response header");
            ssize_t n = s.rd(tmp, sizeof tmp);
            if (n <= 0) throw std::runtime_error("connection closed before response");
            buf.append(tmp, (size_t)n);
        }
        std::string head = buf.substr(0, he);
        buf.erase(0, he + 4);
        int code = atoi(head.c_str() + (head.size() > 9 ? 9 : 0));
        auto hv = [&](const char *name) {
            std::string low = head;
            for (auto &c : low) c = (char)tolower((unsigned char)c);
            std::string key = std::string("\r\n") + name + ":";
            size_t p = low.find(key);
            if (p == std::string::npos) return std::string();
            p += key.size();
            size_t e = head.find("\r\n", p);
            std::string v = head.substr(p, e == std::string::npos ? e : e - p);
            size_t a = v.find_first_not_of(" \t"), b = v.find_last_not_of(" \t");
            return a == std::string::npos ? std::string() : v.substr(a, b - a + 1);
        };
        if (code >= 300 && code < 400 && !hv("location").empty()) {
            std::string loc = hv("location");
            if (loc.compare(0, 2, "//") == 0) loc = u.scheme + ":" + loc;
            else if (loc[0] == '/') loc = u.scheme + "://" + host + loc;
            else if (loc.find("://") == std::string::npos) throw std::runtime_error("redirect to a non-http URL refused");
            if (strncasecmp(loc.c_str(), "http://", 7) && strncasecmp(loc.c_str(), "https://", 8))
                throw std::runtime_error("redirect to a non-http URL refused");
            url = loc;
            continue;
        }
        if (code != 200) throw std::runtime_error("HTTP " + std::to_string(code));
        bool chunked = false;
        { std::string te = hv("transfer-encoding"); for (auto &c : te) c = (char)tolower((unsigned char)c); chunked = te.find("chunked") != std::string::npos; }
        int64_t total = chunked ? 0 : atoll(hv("content-length").c_str());
        if (limit && total > limit) throw std::runtime_error("File larger than " + std::to_string(limit >> 20) + " MB");
        int64_t got = 0;
        auto emit = [&](const char *p, size_t n) {
            got += (int64_t)n;
            if (limit && got > limit) throw std::runtime_error("File larger than " + std::to_string(limit >> 20) + " MB");
            sink(p, n);
            if (progress) progress(got, total);
        };
        if (progress) progress(0, total);
        if (!chunked) {
            if (!buf.empty()) emit(buf.data(), buf.size());
            while (total == 0 || got < total) {
                ssize_t n = s.rd(tmp, sizeof tmp);
                if (n == 0) break;
                if (n < 0) throw std::runtime_error("download interrupted");
                emit(tmp, (size_t)n);
            }
            if (total && got < total) throw std::runtime_error("download truncated");
            return;
        }
        // chunked: parse size lines out of buf, refilling as needed
        auto fill = [&]() { ssize_t n = s.rd(tmp, sizeof tmp); if (n <= 0) throw std::runtime_error("download interrupted"); buf.append(tmp, (size_t)n); };
        while (true) {
            size_t le;
            while ((le = buf.find("\r\n")) == std::string::npos) fill();
            long sz = strtol(buf.substr(0, le).c_str(), nullptr, 16);
            buf.erase(0, le + 2);
            if (sz <= 0) return;
            size_t left = (size_t)sz;
            while (left) {
                if (buf.empty()) fill();
                size_t take = std::min(left, buf.size());
                emit(buf.data(), take);
                buf.erase(0, take);
                left -= take;
            }
            while (buf.size() < 2) fill();
            buf.erase(0, 2);
        }
    }
    throw std::runtime_error("too many redirects");
}

std::string get_string(const std::string &url, const Policy &pol, int64_t limit) {
    std::string out;
    get(url, pol, [&](const char *p, size_t n) { out.append(p, n); }, nullptr, limit);
    return out;
}

} // namespace fetch
