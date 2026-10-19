// Background jobs with progress, polled by the UI at /api/job/<id>.
#pragma once
#include "json.h"
#include <atomic>
#include <functional>
#include <mutex>
#include <stdexcept>
#include <string>

namespace jobs {

struct Job {
    std::string id;
    std::mutex mu;
    std::string label, unit = "bytes", state = "running", error;
    json::Value result;
    std::atomic<int64_t> done{0}, total{0};
    int64_t t = 0;
    void set_label(const std::string &l) { std::lock_guard<std::mutex> g(mu); label = l; }
    void set_unit(const std::string &u, int64_t tot) { std::lock_guard<std::mutex> g(mu); unit = u; total = tot; done = 0; }
};

// Thrown by start() when too many tasks are already running.
struct Busy : std::runtime_error { Busy() : std::runtime_error("Too many running tasks, wait for one to finish") {} };
constexpr int kMaxRunning = 8;

// fn throws std::exception to report failure; its message becomes the job error.
std::string start(const std::string &label, std::function<json::Value(Job &)> fn, const std::string &unit = "bytes");
bool view(const std::string &id, json::Value &out);

} // namespace jobs
