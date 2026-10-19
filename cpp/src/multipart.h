// multipart/form-data parsing over an in-memory or mmap'd request body (zero copy).
#pragma once
#include "http.h"
#include <string>
#include <vector>

namespace multipart {

struct Body {
    const char *p = nullptr;
    size_t n = 0;
    explicit Body(const http::Request &rq);
    ~Body();
    // Tell the kernel the pages in [p, p+n) are no longer needed so a big upload does not stay resident.
    void drop(const char *p, size_t n) const;
    bool mapped() const { return map_ != nullptr; }
    Body(const Body &) = delete;
    Body &operator=(const Body &) = delete;
private:
    void *map_ = nullptr;
};

struct Part {
    std::string name, filename;
    bool is_file = false;
    const char *data = nullptr;
    size_t len = 0;
};

// Writes a part's bytes to path (truncating), in chunks, releasing pages as it goes. Returns false on I/O error.
bool write_part(const Body &body, const Part &part, const std::string &path);

// Same, but creates `name` beneath dirfd without ever following a symlink (uploads into a server folder).
bool write_part_beneath(const Body &body, const Part &part, int dirfd, const std::string &name);

// Returns false if the content type has no boundary.
bool parse(const http::Request &rq, const Body &body, std::vector<Part> &parts);

} // namespace multipart
