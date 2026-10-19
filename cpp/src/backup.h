#pragma once
#include "jobs.h"
#include "json.h"
#include <string>

namespace backup {

// Both throw std::runtime_error on failure (the message is shown to the user).
json::Value create(const std::string &sid, jobs::Job *job);              // {file,path,size_mb}
std::string restore(const std::string &fname, const std::string &sid, jobs::Job *job);   // returns message
std::string path_of(const std::string &fname);                           // "" if not a plain backup name
json::Value list();
std::string dir();                                                       // expanded backup dir
void start_scheduler();

} // namespace backup
