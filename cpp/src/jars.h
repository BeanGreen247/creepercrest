// Server JAR providers: Vanilla, Paper, Purpur, Fabric.
#pragma once
#include "jobs.h"
#include <string>
#include <vector>

namespace jars {

bool known_type(const std::string &t);
std::vector<std::string> versions(const std::string &type);          // newest first; throws on failure
void download(const std::string &type, const std::string &ver, const std::string &dest, jobs::Job *job);
int required_java(const std::string &ver);                          // 0 if unknown (errors swallowed)

} // namespace jars
