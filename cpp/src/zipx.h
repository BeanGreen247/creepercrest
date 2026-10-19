// Zip create/extract on top of libzip, with zip-slip protection and progress callbacks.
#pragma once
#include <cstdint>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace zipx {

using Progress = std::function<void(int64_t done, int64_t total)>;   // total==0 means unknown

bool is_zip(const std::string &path);
bool has_entry(const std::string &path, const std::string &name);
// Extracts everything under dest. Throws std::runtime_error on failure.
// limit_bytes caps what the archive may expand to (0 = no cap): declared sizes are checked up front and the real
// bytes written are counted too, so a zip bomb cannot fill the disk.
void extract(const std::string &zip_path, const std::string &dest, const Progress &prog = nullptr, bool by_files = true,
             uint64_t limit_bytes = 0);
// files: (path on disk, name inside zip). Deflate level 6. Throws on failure.
void create(const std::string &zip_path, const std::vector<std::pair<std::string, std::string>> &files,
            const Progress &prog = nullptr);

} // namespace zipx
