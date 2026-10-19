#include "multipart.h"
#include <sys/mman.h>
#include <algorithm>
#include <cstdint>
#include <cstring>

namespace multipart {

Body::Body(const http::Request &rq) {
    if (rq.body_fd >= 0 && rq.body_len) {
        map_ = mmap(nullptr, rq.body_len, PROT_READ, MAP_PRIVATE, rq.body_fd, 0);
        if (map_ == MAP_FAILED) map_ = nullptr;
        else { p = (const char *)map_; n = rq.body_len; }
    } else { p = rq.body.data(); n = rq.body.size(); }
}
Body::~Body() { if (map_) munmap(map_, n); }

void Body::drop(const char *q, size_t len) const {
    if (!map_ || !len) return;
    uintptr_t a = ((uintptr_t)q + 4095) & ~(uintptr_t)4095, e = ((uintptr_t)q + len) & ~(uintptr_t)4095;
    if (e > a) madvise((void *)a, e - a, MADV_DONTNEED);
}

bool write_part(const Body &body, const Part &part, const std::string &path) {
    FILE *f = fopen(path.c_str(), "wb");
    if (!f) return false;
    bool ok = true;
    const size_t step = 4 << 20;
    for (size_t off = 0; ok && off < part.len; off += step) {
        size_t n = std::min(step, part.len - off);
        ok = fwrite(part.data + off, 1, n, f) == n;
        body.drop(part.data + off, n);
    }
    return fclose(f) == 0 && ok;
}

// memmem over a big mapped region in windows, releasing pages behind the cursor.
static const char *scan(const Body &body, const char *h, size_t hn, const char *nd, size_t nn) {
    const size_t step = 4 << 20;
    const char *cur = h, *end = h + hn;
    while (cur < end) {
        size_t win = std::min<size_t>(step + nn, (size_t)(end - cur));
        const char *r = (const char *)memmem(cur, win, nd, nn);
        if (r) return r;
        if (cur + win >= end) break;
        body.drop(cur, step);
        cur += step;
    }
    return nullptr;
}

static const char *find(const char *h, size_t hn, const char *nd, size_t nn) {
    if (nn == 0 || hn < nn) return nullptr;
    return (const char *)memmem(h, hn, nd, nn);
}

static std::string param(const std::string &hdr, const char *key) {
    std::string low = hdr;
    for (auto &c : low) c = (char)tolower((unsigned char)c);
    std::string k = std::string(key) + "=";
    size_t p = 0;
    while ((p = low.find(k, p)) != std::string::npos) {
        if (p == 0 || low[p - 1] == ' ' || low[p - 1] == ';') {
            size_t s = p + k.size();
            if (s < hdr.size() && hdr[s] == '"') { size_t e = hdr.find('"', s + 1); return hdr.substr(s + 1, e == std::string::npos ? e : e - s - 1); }
            size_t e = hdr.find(';', s);
            std::string v = hdr.substr(s, e == std::string::npos ? e : e - s);
            while (!v.empty() && isspace((unsigned char)v.back())) v.pop_back();
            return v;
        }
        p++;
    }
    return "";
}

bool parse(const http::Request &rq, const Body &body, std::vector<Part> &parts) {
    std::string ct = rq.hdr("content-type");
    std::string boundary = param(ct, "boundary");
    if (boundary.empty()) return false;
    std::string delim = "--" + boundary;
    const char *p = body.p, *end = body.p + body.n;
    const char *q = find(p, (size_t)(end - p), delim.data(), delim.size());
    while (q) {
        const char *after = q + delim.size();
        if (end - after >= 2 && after[0] == '-' && after[1] == '-') break;
        if (end - after >= 2 && after[0] == '\r' && after[1] == '\n') after += 2;
        const char *hend = find(after, (size_t)(end - after), "\r\n\r\n", 4);
        if (!hend) break;
        std::string headers(after, hend);
        const char *data = hend + 4;
        std::string nd = "\r\n" + delim;
        const char *next = scan(body, data, (size_t)(end - data), nd.data(), nd.size());
        const char *dend = next ? next : end;
        Part part;
        // find Content-Disposition
        size_t pos = 0;
        while (pos < headers.size()) {
            size_t e = headers.find("\r\n", pos);
            std::string line = headers.substr(pos, e == std::string::npos ? e : e - pos);
            pos = e == std::string::npos ? headers.size() : e + 2;
            std::string low = line.substr(0, 19);
            for (auto &c : low) c = (char)tolower((unsigned char)c);
            if (low == "content-disposition") {
                part.name = param(line, "name");
                std::string fn = param(line, "filename");
                part.filename = fn;
                part.is_file = line.find("filename=") != std::string::npos || line.find("FILENAME=") != std::string::npos;
            }
        }
        part.data = data;
        part.len = (size_t)(dend - data);
        if (!part.name.empty() || part.is_file) parts.push_back(part);
        q = next ? next + 2 : nullptr;
    }
    return true;
}

} // namespace multipart
