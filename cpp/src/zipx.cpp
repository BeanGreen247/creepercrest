#include "zipx.h"
#include "fsu.h"
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <zip.h>
#include <cstring>
#include <memory>
#include <stdexcept>

namespace zipx {

namespace {
struct Zip {
    zip_t *z = nullptr;
    ~Zip() { if (z) zip_discard(z); }
};

std::string zerr(int code) {
    zip_error_t e;
    zip_error_init_with_code(&e, code);
    std::string s = zip_error_strerror(&e);
    zip_error_fini(&e);
    return s;
}

// Returns "" if the entry name is unsafe (absolute or contains ..).
std::string clean_name(const char *raw) {
    std::string n = raw;
    std::string out;
    size_t i = 0;
    while (i <= n.size()) {
        size_t j = n.find('/', i);
        if (j == std::string::npos) j = n.size();
        std::string c = n.substr(i, j - i);
        if (c == "..") return "";
        if (!c.empty() && c != ".") out += (out.empty() ? "" : "/") + c;
        i = j + 1;
    }
    if (!n.empty() && n[0] == '/') return "";
    return out;
}
} // namespace

bool is_zip(const std::string &path) {
    int err = 0;
    zip_t *z = zip_open(path.c_str(), ZIP_RDONLY, &err);
    if (!z) return false;
    zip_close(z);
    return true;
}

bool has_entry(const std::string &path, const std::string &name) {
    int err = 0;
    zip_t *z = zip_open(path.c_str(), ZIP_RDONLY, &err);
    if (!z) return false;
    bool ok = zip_name_locate(z, name.c_str(), 0) >= 0;
    zip_close(z);
    return ok;
}

void extract(const std::string &path, const std::string &dest, const Progress &prog, bool by_files, uint64_t limit_bytes) {
    int err = 0;
    zip_t *z = zip_open(path.c_str(), ZIP_RDONLY, &err);
    if (!z) throw std::runtime_error(zerr(err));
    std::unique_ptr<zip_t, void (*)(zip_t *)> guard(z, [](zip_t *p) { zip_close(p); });
    zip_int64_t n = zip_get_num_entries(z, 0);
    int64_t total_bytes = 0;
    uint64_t declared = 0;
    if (n > 1000000) throw std::runtime_error("zip has too many entries");
    for (zip_int64_t i = 0; i < n; i++) { zip_stat_t st; if (zip_stat_index(z, i, 0, &st) == 0) declared += st.size; }
    if (limit_bytes && declared > limit_bytes)
        throw std::runtime_error("zip would expand to " + std::to_string(declared >> 20) + " MB, more than the " +
                                 std::to_string(limit_bytes >> 20) + " MB allowed");
    if (!by_files) total_bytes = (int64_t)declared;
    uint64_t written = 0;
    int64_t done = 0;
    int dfd = fsu::open_dir_fd(dest);
    if (dfd < 0) throw std::runtime_error("cannot open " + dest);
    struct DirGuard { int fd; ~DirGuard() { close(fd); } } dir_guard{dfd};
    if (prog) prog(0, by_files ? n : total_bytes);
    std::string buf(256 * 1024, '\0');
    for (zip_int64_t i = 0; i < n; i++) {
        zip_stat_t st;
        if (zip_stat_index(z, i, 0, &st) != 0) throw std::runtime_error("bad zip entry");
        std::string name = clean_name(st.name);
        if (name.empty() && st.name[0] != 0 && strcmp(st.name, "./") != 0 && strcmp(st.name, "/") != 0)
            throw std::runtime_error(std::string("unsafe path in zip: ") + st.name);
        if (name.empty()) { done++; continue; }
        size_t len = strlen(st.name);
        if (len && st.name[len - 1] == '/') {
            if (!fsu::mkdirs_beneath(dfd, name)) throw std::runtime_error("cannot create folder " + name + " (is it a link?)");
        } else {
            zip_file_t *f = zip_fopen_index(z, i, 0);
            if (!f) throw std::runtime_error(std::string("cannot read ") + st.name);
            zip_uint8_t opsys;
            zip_uint32_t attr = 0;
            int mode = 0644;
            if (zip_file_get_external_attributes(z, i, 0, &opsys, &attr) == 0 && opsys == ZIP_OPSYS_UNIX) {
                int m = (attr >> 16) & 0777;
                if (m) mode = m | 0400;
            }
            int fd = fsu::create_beneath(dfd, name, mode);
            if (fd < 0) { zip_fclose(f); throw std::runtime_error("cannot write " + name + " (is it a link?)"); }
            zip_int64_t r;
            bool ok = true;
            while ((r = zip_fread(f, &buf[0], buf.size())) > 0) {
                const char *p = buf.data();
                zip_int64_t left = r;
                while (left > 0) {
                    ssize_t w = write(fd, p, (size_t)left);
                    if (w <= 0) { ok = false; break; }
                    p += w; left -= w;
                }
                if (!ok) break;
                written += (uint64_t)r;
                if (limit_bytes && written > limit_bytes) { ok = false; zip_fclose(f); close(fd); throw std::runtime_error("zip expands past the allowed size"); }
                if (!by_files) { done += r; if (prog) prog(done, total_bytes); }
            }
            if (r < 0) ok = false;
            zip_fclose(f);
            if (close(fd) != 0) ok = false;
            if (!ok) throw std::runtime_error("write failed for " + name);
        }
        if (by_files) { done++; if (prog) prog(done, n); }
    }
}

namespace {
struct ProgCtx { const Progress *p; int64_t total; };
void on_progress(zip_t *, double frac, void *ud) {
    auto *c = (ProgCtx *)ud;
    (*c->p)((int64_t)(frac * (double)c->total), c->total);
}
} // namespace

void create(const std::string &zip_path, const std::vector<std::pair<std::string, std::string>> &files, const Progress &prog) {
    int err = 0;
    zip_t *z = zip_open(zip_path.c_str(), ZIP_CREATE | ZIP_TRUNCATE, &err);
    if (!z) throw std::runtime_error(zerr(err));
    Zip g;
    g.z = z;
    int64_t total = 0;
    for (auto &f : files) {
        zip_source_t *src = zip_source_file(z, f.first.c_str(), 0, ZIP_LENGTH_TO_END);
        if (!src) throw std::runtime_error("cannot read " + f.first);
        zip_int64_t idx = zip_file_add(z, f.second.c_str(), src, ZIP_FL_OVERWRITE | ZIP_FL_ENC_UTF_8);
        if (idx < 0) { zip_source_free(src); throw std::runtime_error(zip_strerror(z)); }
        zip_set_file_compression(z, (zip_uint64_t)idx, ZIP_CM_DEFLATE, 6);
        struct stat st;
        if (stat(f.first.c_str(), &st) == 0) {
            total += st.st_size;
            zip_file_set_external_attributes(z, (zip_uint64_t)idx, 0, ZIP_OPSYS_UNIX, ((zip_uint32_t)(st.st_mode & 07777) | 0100000) << 16);
        }
    }
    ProgCtx ctx{&prog, total};
    if (prog) zip_register_progress_callback_with_state(z, 0.01, on_progress, nullptr, &ctx);
    if (zip_close(z) != 0) { std::string m = zip_strerror(z); g.z = nullptr; throw std::runtime_error(m); }
    g.z = nullptr;
    if (prog) prog(total, total);
}

} // namespace zipx
