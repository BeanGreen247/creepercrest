// Filesystem helpers: path resolution/containment, atomic writes, recursive ops, time/size formatting.
#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace fsu {

std::string expand_home(const std::string &p);
std::string normalize(const std::string &p);                 // lexical abspath (no symlink resolution)
std::string resolve(const std::string &p);                   // like Python os.path.realpath (works for missing tails)
// Resolve rel inside server_dir; "" if it escapes the root.
std::string safe_path(const std::string &server_dir, const std::string &rel);
std::string base_name(const std::string &p);
std::string dir_name(const std::string &p);
std::string join(const std::string &a, const std::string &b);

bool is_dir(const std::string &p);
bool is_file(const std::string &p);
bool exists(const std::string &p);
int64_t file_size(const std::string &p);
int64_t mtime(const std::string &p);
bool is_executable(const std::string &p);

bool read_file(const std::string &p, std::string &out, size_t max = (size_t)-1);
bool write_file(const std::string &p, const std::string &data, int mode = 0644);
bool write_file_atomic(const std::string &p, const std::string &data, const std::string &mode_from = "");
bool mkdirs(const std::string &p, int mode = 0755);
bool rmtree(const std::string &p);
std::vector<std::string> list_names(const std::string &dir);  // unsorted, no . and ..

std::string fmt_size(double n);                               // "12.3 KB", "512 B"
std::string fmt_time(int64_t t, const char *fmt);             // local time
std::string now_str(const char *fmt);
double round_to(double v, int digits);

} // namespace fsu
