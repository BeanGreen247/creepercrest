// Small HTTP/1.1 server: thread per connection, blocking sockets with timeouts, optional TLS via OpenSSL.
#pragma once
#include <functional>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace http {

struct Request {
    std::string method, target, path, query;
    std::vector<std::pair<std::string, std::string>> headers;   // names lower-cased
    std::string body;            // bodies up to kMemBody
    int body_fd = -1;            // larger bodies are spooled to an unlinked temp file (owned by the server)
    size_t body_len = 0;
    std::string peer;            // socket peer address
    bool tls = false;

    const std::string *header(const char *lname) const;
    std::string hdr(const char *lname) const { auto *h = header(lname); return h ? *h : std::string(); }
    std::map<std::string, std::string> query_args() const;
    std::map<std::string, std::string> form() const;            // application/x-www-form-urlencoded body
    std::string cookie(const std::string &name) const;
};

struct Response {
    int code = 200;
    std::vector<std::pair<std::string, std::string>> headers;
    std::string body;
    std::string file;            // if set, stream this file instead of body (Content-Length = file size)
    bool file_unlink = false;    // delete `file` once sent (temporary downloads)
    void set(const std::string &k, const std::string &v) { headers.emplace_back(k, v); }
};

constexpr size_t kMemBody = 1 << 20;

using Handler = std::function<void(Request &, Response &)>;

// Called after the request headers are parsed and before a body larger than kPreAuthBody is read.
// Return false (with rs filled in) to refuse it; the connection is then closed without reading the body.
using BodyGate = std::function<bool(const Request &, Response &)>;
constexpr size_t kPreAuthBody = 64 * 1024;

struct Options {
    BodyGate body_gate;
    std::string host = "0.0.0.0";
    int port = 8888;
    std::string tls_cert, tls_key;
    size_t max_body = (size_t)2 << 30;
    int timeout_s = 60;            // per read/write once a request is being served
    int header_s = 10;             // a client must finish its request headers within this long
    int idle_s = 15;               // idle keep-alive connections are closed after this long
    int max_conns = 256;
    int max_conns_per_ip = 32;     // one address cannot hold every slot; trusted proxies are exempt
    std::vector<std::string> unlimited_ips;
    std::string spool_dir = "/tmp";
};

// Binds and listens (and loads TLS material). Returns the listening fd, or -1 with err set.
int listen_on(const Options &opt, std::string &err);
// Accept loop; blocks forever. `opt` and `h` must outlive the call.
void run(int listen_fd, const Options &opt, Handler h);

std::string url_decode(const std::string &s, bool plus_is_space = true);
std::map<std::string, std::string> parse_urlencoded(const std::string &s);
const char *status_text(int code);

} // namespace http
