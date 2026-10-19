#include "http.h"
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#include <atomic>
#include <cstring>
#include <cerrno>
#include <csignal>
#include <memory>
#include <algorithm>
#include <strings.h>

namespace http {

const char *status_text(int c) {
    switch (c) {
    case 200: return "OK"; case 204: return "No Content"; case 206: return "Partial Content";
    case 303: return "See Other"; case 304: return "Not Modified";
    case 400: return "Bad Request"; case 401: return "Unauthorized"; case 403: return "Forbidden";
    case 404: return "Not Found"; case 405: return "Method Not Allowed"; case 408: return "Request Timeout";
    case 411: return "Length Required"; case 413: return "Payload Too Large"; case 429: return "Too Many Requests";
    case 431: return "Request Header Fields Too Large"; case 500: return "Internal Server Error";
    case 501: return "Not Implemented"; case 503: return "Service Unavailable";
    default: return "OK";
    }
}

std::string url_decode(const std::string &s, bool plus) {
    std::string o;
    for (size_t i = 0; i < s.size(); i++) {
        if (s[i] == '%' && i + 2 < s.size() + 0 && isxdigit((unsigned char)s[i + 1]) && isxdigit((unsigned char)s[i + 2])) {
            o += (char)strtol(s.substr(i + 1, 2).c_str(), nullptr, 16);
            i += 2;
        } else if (s[i] == '+' && plus) o += ' ';
        else o += s[i];
    }
    return o;
}

std::map<std::string, std::string> parse_urlencoded(const std::string &s) {
    std::map<std::string, std::string> m;
    size_t pos = 0;
    while (pos <= s.size()) {
        size_t amp = s.find('&', pos);
        std::string kv = s.substr(pos, amp == std::string::npos ? amp : amp - pos);
        if (!kv.empty()) {
            size_t eq = kv.find('=');
            std::string k = url_decode(kv.substr(0, eq));
            if (!m.count(k)) m[k] = eq == std::string::npos ? "" : url_decode(kv.substr(eq + 1));
        }
        if (amp == std::string::npos) break;
        pos = amp + 1;
    }
    return m;
}

const std::string *Request::header(const char *n) const {
    for (auto &h : headers) if (h.first == n) return &h.second;
    return nullptr;
}
std::map<std::string, std::string> Request::query_args() const { return parse_urlencoded(query); }
std::map<std::string, std::string> Request::form() const { return parse_urlencoded(body); }

std::string Request::cookie(const std::string &name) const {
    std::string c = hdr("cookie");
    size_t pos = 0;
    while (pos < c.size()) {
        while (pos < c.size() && (c[pos] == ' ' || c[pos] == ';')) pos++;
        size_t end = c.find(';', pos);
        std::string kv = c.substr(pos, end == std::string::npos ? end : end - pos);
        size_t eq = kv.find('=');
        if (eq != std::string::npos && kv.substr(0, eq) == name) return kv.substr(eq + 1);
        if (end == std::string::npos) break;
        pos = end + 1;
    }
    return "";
}

namespace {

struct Conn {
    explicit Conn(int f) : fd(f) {}
    int fd;
    SSL *ssl = nullptr;
    std::string rbuf;   // bytes read past the current request
    ssize_t rd(void *b, size_t n) {
        if (ssl) { int r = SSL_read(ssl, b, (int)n); return r > 0 ? r : -1; }
        ssize_t r;
        do r = recv(fd, b, n, 0); while (r < 0 && errno == EINTR);
        return r > 0 ? r : -1;
    }
    bool wr(const char *b, size_t n) {
        while (n) {
            ssize_t w;
            if (ssl) { int r = SSL_write(ssl, b, (int)n); w = r > 0 ? r : -1; }
            else do w = send(fd, b, n, MSG_NOSIGNAL); while (w < 0 && errno == EINTR);
            if (w <= 0) return false;
            b += w; n -= (size_t)w;
        }
        return true;
    }
};

struct Ctx {
    const Options *opt;
    Handler *h;
    SSL_CTX *tls;
    std::atomic<int> *live;
};

struct ThreadArg { Ctx ctx; int fd; std::string peer; };

bool send_simple(Conn &c, int code, const char *msg) {
    std::string r = std::string("HTTP/1.1 ") + std::to_string(code) + " " + status_text(code) +
                    "\r\nContent-Length: " + std::to_string(strlen(msg)) + "\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n" + msg;
    return c.wr(r.data(), r.size());
}

bool ieq(const std::string &a, const char *b) { return strcasecmp(a.c_str(), b) == 0; }

// Reads one request. Returns 0 ok, 1 clean close, 2 error already answered.
int read_request(Conn &c, const Options &opt, Request &rq) {
    std::string &buf = c.rbuf;
    size_t hend;
    char tmp[8192];
    while ((hend = buf.find("\r\n\r\n")) == std::string::npos) {
        if (buf.size() > 16384) { send_simple(c, 431, "headers too large"); return 2; }
        ssize_t n = c.rd(tmp, sizeof tmp);
        if (n <= 0) return buf.empty() ? 1 : 2;
        buf.append(tmp, (size_t)n);
    }
    if (hend > 16384) { send_simple(c, 431, "headers too large"); return 2; }
    std::string head = buf.substr(0, hend);
    buf.erase(0, hend + 4);
    size_t le = head.find("\r\n");
    std::string line = head.substr(0, le);
    size_t s1 = line.find(' '), s2 = line.rfind(' ');
    if (s1 == std::string::npos || s2 == s1) { send_simple(c, 400, "bad request"); return 2; }
    rq.method = line.substr(0, s1);
    rq.target = line.substr(s1 + 1, s2 - s1 - 1);
    std::string ver = line.substr(s2 + 1);
    if (ver != "HTTP/1.1" && ver != "HTTP/1.0") { send_simple(c, 400, "bad request"); return 2; }
    rq.headers.emplace_back("x-http-version", ver);
    size_t q = rq.target.find('?');
    rq.path = url_decode(rq.target.substr(0, q), false);
    rq.query = q == std::string::npos ? "" : rq.target.substr(q + 1);
    size_t pos = le == std::string::npos ? head.size() : le + 2;
    while (pos < head.size()) {
        size_t e = head.find("\r\n", pos);
        std::string h = head.substr(pos, e == std::string::npos ? e : e - pos);
        pos = e == std::string::npos ? head.size() : e + 2;
        size_t col = h.find(':');
        if (col == std::string::npos) continue;
        std::string k = h.substr(0, col), v = h.substr(col + 1);
        for (auto &ch : k) ch = (char)tolower((unsigned char)ch);
        size_t a = v.find_first_not_of(" \t"), b = v.find_last_not_of(" \t");
        rq.headers.emplace_back(k, a == std::string::npos ? "" : v.substr(a, b - a + 1));
    }
    if (rq.header("transfer-encoding")) { send_simple(c, 501, "chunked bodies unsupported"); return 2; }
    std::string cl = rq.hdr("content-length");
    size_t len = 0;
    if (!cl.empty()) {
        char *e;
        unsigned long long v = strtoull(cl.c_str(), &e, 10);
        if (*e || cl[0] == '-') { send_simple(c, 400, "bad content-length"); return 2; }
        if (v > opt.max_body) { send_simple(c, 413, "request too large"); return 2; }
        len = (size_t)v;
    }
    rq.body_len = len;
    if (len <= kMemBody) {
        while (buf.size() < len) {
            ssize_t n = c.rd(tmp, sizeof tmp);
            if (n <= 0) return 2;
            buf.append(tmp, (size_t)n);
        }
        rq.body = buf.substr(0, len);
        buf.erase(0, len);
    } else {
        int fd = open(opt.spool_dir.c_str(), O_TMPFILE | O_RDWR | O_CLOEXEC, 0600);
        if (fd < 0) {   // filesystem without O_TMPFILE: create and unlink immediately
            std::string tpl = opt.spool_dir + "/.cc-spool-XXXXXX";
            fd = mkostemp(&tpl[0], O_CLOEXEC);
            if (fd >= 0) unlink(tpl.c_str());
        }
        if (fd < 0) { send_simple(c, 500, "cannot spool upload"); return 2; }
        size_t got = 0;
        size_t take = std::min(buf.size(), len);
        if (take && write(fd, buf.data(), take) != (ssize_t)take) { close(fd); send_simple(c, 500, "spool failed"); return 2; }
        got = take;
        buf.erase(0, take);
        std::string chunk(65536, '\0');
        while (got < len) {
            ssize_t n = c.rd(&chunk[0], std::min(chunk.size(), len - got));
            if (n <= 0) { close(fd); return 2; }
            if (write(fd, chunk.data(), (size_t)n) != n) { close(fd); send_simple(c, 500, "spool failed"); return 2; }
            got += (size_t)n;
        }
        rq.body_fd = fd;
    }
    return 0;
}

bool write_response(Conn &c, Request &rq, Response &rs, bool keep) {
    int fd = -1;
    size_t flen = 0;
    if (!rs.file.empty()) {
        fd = open(rs.file.c_str(), O_RDONLY | O_CLOEXEC);
        struct stat st;
        if (fd < 0 || fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
            if (fd >= 0) close(fd);
            fd = -1; rs.code = 404; rs.body = "not found"; rs.file.clear();
        } else flen = (size_t)st.st_size;
    }
    size_t blen = fd >= 0 ? flen : rs.body.size();
    std::string out = "HTTP/1.1 " + std::to_string(rs.code) + " " + status_text(rs.code) + "\r\nServer: CreeperCrest-cpp\r\n";
    bool has_len = false;
    for (auto &h : rs.headers) {
        if (!strcasecmp(h.first.c_str(), "content-length")) has_len = true;
        out += h.first + ": " + h.second + "\r\n";
    }
    if (!has_len) out += "Content-Length: " + std::to_string(blen) + "\r\n";
    out += keep ? "Connection: keep-alive\r\n\r\n" : "Connection: close\r\n\r\n";
    bool ok = c.wr(out.data(), out.size());
    bool head = rq.method == "HEAD";
    if (ok && !head) {
        if (fd >= 0) {
            std::string chunk(65536, '\0');
            size_t left = flen;
            while (ok && left) {
                ssize_t n = read(fd, &chunk[0], std::min(chunk.size(), left));
                if (n <= 0) { ok = false; break; }
                ok = c.wr(chunk.data(), (size_t)n);
                left -= (size_t)n;
            }
        } else if (!rs.body.empty()) ok = c.wr(rs.body.data(), rs.body.size());
    }
    if (fd >= 0) close(fd);
    if (rs.file_unlink && !rs.file.empty()) unlink(rs.file.c_str());
    return ok;
}

void *conn_main(void *p) {
    std::unique_ptr<ThreadArg> a((ThreadArg *)p);
    Conn c(a->fd);
    struct timeval tv{a->ctx.opt->timeout_s, 0};
    setsockopt(c.fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(c.fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    int one = 1;
    setsockopt(c.fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    bool ok = true;
    if (a->ctx.tls) {
        c.ssl = SSL_new(a->ctx.tls);
        SSL_set_fd(c.ssl, c.fd);
        if (SSL_accept(c.ssl) != 1) ok = false;
    }
    for (int served = 0; ok && served < 100; served++) {
        Request rq;
        rq.peer = a->peer;
        rq.tls = c.ssl != nullptr;
        int r = read_request(c, *a->ctx.opt, rq);
        if (r) break;
        Response rs;
        try {
            (*a->ctx.h)(rq, rs);
        } catch (const std::exception &e) {
            fprintf(stderr, "[error] %s %s: %s\n", rq.method.c_str(), rq.path.c_str(), e.what());
            rs = Response();
            rs.code = 500; rs.body = "internal error"; rs.set("Content-Type", "text/plain");
        }
        if (rq.body_fd >= 0) close(rq.body_fd);
        std::string conn = rq.hdr("connection");
        bool keep = (rq.hdr("x-http-version") == "HTTP/1.1" ? !ieq(conn, "close") : ieq(conn, "keep-alive"));
        if (!write_response(c, rq, rs, keep) || !keep) break;
    }
    if (c.ssl) { SSL_shutdown(c.ssl); SSL_free(c.ssl); }
    close(c.fd);
    --*a->ctx.live;
    return nullptr;
}

} // namespace

static SSL_CTX *g_tls = nullptr;

int listen_on(const Options &opt, std::string &err) {
    SSL_CTX *tls = nullptr;
    if (!opt.tls_cert.empty() && !opt.tls_key.empty()) {
        tls = SSL_CTX_new(TLS_server_method());
        SSL_CTX_set_min_proto_version(tls, TLS1_2_VERSION);
        if (SSL_CTX_use_certificate_chain_file(tls, opt.tls_cert.c_str()) != 1 ||
            SSL_CTX_use_PrivateKey_file(tls, opt.tls_key.c_str(), SSL_FILETYPE_PEM) != 1) {
            err = "cannot load TLS certificate/key";
            return -1;
        }
    }
    struct addrinfo hints{}, *res = nullptr;
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_PASSIVE;
    if (getaddrinfo(opt.host.c_str(), std::to_string(opt.port).c_str(), &hints, &res) != 0 || !res) {
        err = "cannot resolve listen address " + opt.host;
        return -1;
    }
    int ls = socket(res->ai_family, SOCK_STREAM | SOCK_CLOEXEC, 0), one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (res->ai_family == AF_INET6) { int zero = 0; setsockopt(ls, IPPROTO_IPV6, IPV6_V6ONLY, &zero, sizeof zero); }
    if (bind(ls, res->ai_addr, res->ai_addrlen) != 0 || listen(ls, 64) != 0) {
        err = std::string("cannot listen on ") + opt.host + ":" + std::to_string(opt.port) + ": " + strerror(errno);
        freeaddrinfo(res);
        close(ls);
        return -1;
    }
    freeaddrinfo(res);
    g_tls = tls;
    return ls;
}

void run(int ls, const Options &opt, Handler h) {
    SSL_CTX *tls = g_tls;
    signal(SIGPIPE, SIG_IGN);
    std::atomic<int> live{0};
    Ctx ctx{&opt, &h, tls, &live};
    while (true) {
        struct sockaddr_storage ss;
        socklen_t sl = sizeof ss;
        int fd = accept4(ls, (struct sockaddr *)&ss, &sl, SOCK_CLOEXEC);
        if (fd < 0) { if (errno == EINTR) continue; usleep(100000); continue; }
        char ip[INET6_ADDRSTRLEN] = "?";
        if (ss.ss_family == AF_INET) inet_ntop(AF_INET, &((sockaddr_in *)&ss)->sin_addr, ip, sizeof ip);
        else inet_ntop(AF_INET6, &((sockaddr_in6 *)&ss)->sin6_addr, ip, sizeof ip);
        std::string peer = ip;
        if (peer.compare(0, 7, "::ffff:") == 0 && peer.find('.') != std::string::npos) peer = peer.substr(7);
        if (live.load() >= opt.max_conns) {
            Conn c(fd);
            send_simple(c, 503, "busy");
            close(fd);
            continue;
        }
        ++live;
        pthread_attr_t at;
        pthread_attr_init(&at);
        pthread_attr_setstacksize(&at, 512 * 1024);
        pthread_attr_setdetachstate(&at, PTHREAD_CREATE_DETACHED);
        pthread_t t;
        auto *arg = new ThreadArg{ctx, fd, peer};
        if (pthread_create(&t, &at, conn_main, arg) != 0) { delete arg; close(fd); --live; }
        pthread_attr_destroy(&at);
    }
}

} // namespace http
