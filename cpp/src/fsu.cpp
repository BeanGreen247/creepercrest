#include "fsu.h"
#include <dirent.h>
#include <fcntl.h>
#include <ftw.h>
#include <sys/stat.h>
#include <unistd.h>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

namespace fsu {

std::string expand_home(const std::string &p) {
    if (!p.empty() && p[0] == '~' && (p.size() == 1 || p[1] == '/')) {
        const char *h = getenv("HOME");
        return std::string(h ? h : "") + p.substr(1);
    }
    return p;
}

static std::vector<std::string> split(const std::string &p) {
    std::vector<std::string> out;
    size_t i = 0;
    while (i < p.size()) {
        size_t j = p.find('/', i);
        if (j == std::string::npos) j = p.size();
        if (j > i) out.push_back(p.substr(i, j - i));
        i = j + 1;
    }
    return out;
}

std::string normalize(const std::string &in) {
    std::string p = in;
    if (p.empty() || p[0] != '/') {
        char cwd[4096];
        p = std::string(getcwd(cwd, sizeof cwd) ? cwd : "/") + "/" + p;
    }
    std::vector<std::string> st;
    for (auto &c : split(p)) {
        if (c == ".") continue;
        if (c == "..") { if (!st.empty()) st.pop_back(); continue; }
        st.push_back(c);
    }
    std::string o;
    for (auto &c : st) o += "/" + c;
    return o.empty() ? "/" : o;
}

std::string resolve(const std::string &in) {
    std::string cur = "/";
    std::vector<std::string> todo = split(!in.empty() && in[0] == '/' ? in : normalize(in));
    // process components from the front; symlink targets are spliced in
    std::vector<std::string> q(todo.rbegin(), todo.rend());
    int links = 0;
    while (!q.empty()) {
        std::string c = q.back();
        q.pop_back();
        if (c == ".") continue;
        if (c == "..") {
            size_t s = cur.rfind('/');
            cur = s == 0 ? "/" : cur.substr(0, s);
            continue;
        }
        std::string cand = cur == "/" ? "/" + c : cur + "/" + c;
        struct stat st;
        if (lstat(cand.c_str(), &st) == 0 && S_ISLNK(st.st_mode)) {
            if (++links > 40) return "";
            char buf[4096];
            ssize_t n = readlink(cand.c_str(), buf, sizeof buf - 1);
            if (n < 0) return "";
            buf[n] = 0;
            std::string t = buf;
            if (!t.empty() && t[0] == '/') cur = "/";
            auto parts = split(t);
            for (auto it = parts.rbegin(); it != parts.rend(); ++it) q.push_back(*it);
        } else cur = cand;
    }
    return cur;
}

std::string join(const std::string &a, const std::string &b) {
    if (b.empty()) return a;
    if (b[0] == '/') return b;
    if (a.empty() || a.back() == '/') return a + b;
    return a + "/" + b;
}

std::string safe_path(const std::string &server_dir, const std::string &rel) {
    std::string base = resolve(expand_home(server_dir));
    if (base.empty()) return "";
    std::string joined = base;
    if (!rel.empty()) {
        size_t k = rel.find_first_not_of("/\\");
        joined = resolve(join(base, k == std::string::npos ? "" : rel.substr(k)));
    }
    if (joined == base || (joined.size() > base.size() && joined.compare(0, base.size(), base) == 0 &&
                           (base == "/" || joined[base.size()] == '/')))
        return joined;
    return "";
}

std::string base_name(const std::string &p) {
    size_t e = p.size();
    while (e > 1 && p[e - 1] == '/') e--;
    size_t s = p.rfind('/', e ? e - 1 : 0);
    return s == std::string::npos ? p.substr(0, e) : p.substr(s + 1, e - s - 1);
}

std::string dir_name(const std::string &p) {
    size_t s = p.rfind('/');
    if (s == std::string::npos) return "";
    return s == 0 ? "/" : p.substr(0, s);
}

static bool st_of(const std::string &p, struct stat &st) { return stat(p.c_str(), &st) == 0; }
bool is_dir(const std::string &p) { struct stat st; return st_of(p, st) && S_ISDIR(st.st_mode); }
bool is_file(const std::string &p) { struct stat st; return st_of(p, st) && S_ISREG(st.st_mode); }
bool exists(const std::string &p) { struct stat st; return st_of(p, st); }
int64_t file_size(const std::string &p) { struct stat st; return st_of(p, st) ? (int64_t)st.st_size : -1; }
int64_t mtime(const std::string &p) { struct stat st; return st_of(p, st) ? (int64_t)st.st_mtime : 0; }
bool is_executable(const std::string &p) { return access(p.c_str(), X_OK) == 0; }

int open_dir_fd(const std::string &path) { return open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC); }

// Walks rel one component at a time. With make_file, the last component is created as a file and its fd returned;
// otherwise every component is a directory and the final directory fd is returned.
static int walk_beneath(int dirfd, const std::string &rel, bool make_file, int mode, int dir_mode) {
    std::vector<std::string> parts = split(rel);
    if (parts.empty() && make_file) return -1;
    int cur = dup(dirfd);
    if (cur < 0) return -1;
    for (size_t i = 0; i < parts.size(); i++) {
        const std::string &c = parts[i];
        if (c == "." ) continue;
        if (c == "..") { close(cur); return -1; }
        bool last = i + 1 == parts.size();
        if (last && make_file) {
            int fd = openat(cur, c.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, mode);
            close(cur);
            return fd;
        }
        if (mkdirat(cur, c.c_str(), (mode_t)dir_mode) != 0 && errno != EEXIST) { close(cur); return -1; }
        int next = openat(cur, c.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(cur);
        if (next < 0) return -1;
        cur = next;
    }
    return cur;
}

bool mkdirs_beneath(int dirfd, const std::string &rel, int mode) {
    int fd = walk_beneath(dirfd, rel, false, 0, mode);
    if (fd < 0) return false;
    close(fd);
    return true;
}

int create_beneath(int dirfd, const std::string &rel, int mode) { return walk_beneath(dirfd, rel, true, mode, 0755); }

bool read_file(const std::string &p, std::string &out, size_t max) {
    int fd = open(p.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    out.clear();
    char buf[65536];
    ssize_t n;
    while ((n = read(fd, buf, sizeof buf)) > 0) {
        out.append(buf, (size_t)n);
        if (out.size() > max) break;
    }
    close(fd);
    return n >= 0;
}

static bool write_all(int fd, const std::string &d) {
    size_t off = 0;
    while (off < d.size()) {
        ssize_t n = write(fd, d.data() + off, d.size() - off);
        if (n < 0) { if (errno == EINTR) continue; return false; }
        off += (size_t)n;
    }
    return true;
}

bool write_file(const std::string &p, const std::string &data, int mode) {
    int fd = open(p.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode);
    if (fd < 0) return false;
    bool ok = write_all(fd, data);
    return close(fd) == 0 && ok;
}

bool write_file_atomic(const std::string &p, const std::string &data, const std::string &mode_from) {
    std::string tmp = p + ".cc-part";
    struct stat st;
    int mode = (!mode_from.empty() && stat(mode_from.c_str(), &st) == 0) ? (st.st_mode & 07777) : 0644;
    int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode);
    if (fd < 0) return false;
    bool ok = write_all(fd, data);
    if (ok) fchmod(fd, (mode_t)mode);
    ok = (close(fd) == 0) && ok;
    if (!ok || rename(tmp.c_str(), p.c_str()) != 0) { unlink(tmp.c_str()); return false; }
    return true;
}

bool mkdirs(const std::string &p, int mode) {
    if (is_dir(p)) return true;
    std::string parent = dir_name(p);
    if (!parent.empty() && parent != p && !mkdirs(parent, mode)) return false;
    return mkdir(p.c_str(), (mode_t)mode) == 0 || is_dir(p);
}

static int rm_cb(const char *path, const struct stat *, int, struct FTW *) { return remove(path); }
bool rmtree(const std::string &p) { return nftw(p.c_str(), rm_cb, 32, FTW_DEPTH | FTW_PHYS) == 0; }

std::vector<std::string> list_names(const std::string &dir) {
    std::vector<std::string> out;
    DIR *d = opendir(dir.c_str());
    if (!d) return out;
    while (auto *e = readdir(d)) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
        out.push_back(e->d_name);
    }
    closedir(d);
    return out;
}

std::string fmt_size(double n) {
    const char *u[] = {"B", "KB", "MB", "GB"};
    char buf[48];
    for (int i = 0; i < 4; i++) {
        if (n < 1024) {
            if (i == 0) snprintf(buf, sizeof buf, "%lld B", (long long)n);
            else snprintf(buf, sizeof buf, "%.1f %s", n, u[i]);
            return buf;
        }
        n /= 1024;
    }
    snprintf(buf, sizeof buf, "%.1f TB", n);
    return buf;
}

std::string fmt_time(int64_t t, const char *fmt) {
    time_t tt = (time_t)t;
    struct tm tm;
    localtime_r(&tt, &tm);
    char buf[64];
    strftime(buf, sizeof buf, fmt, &tm);
    return buf;
}

std::string now_str(const char *fmt) { return fmt_time((int64_t)time(nullptr), fmt); }

double round_to(double v, int d) {
    double m = std::pow(10.0, d);
    return std::round(v * m) / m;
}

} // namespace fsu
