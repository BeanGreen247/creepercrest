#include "jobs.h"
#include "util.h"
#include <ctime>
#include <map>
#include <memory>
#include <thread>

namespace jobs {

static std::mutex mu;
static std::map<std::string, std::shared_ptr<Job>> all;

std::string start(const std::string &label, std::function<json::Value(Job &)> fn, const std::string &unit) {
    auto job = std::make_shared<Job>();
    std::string raw = util::random_bytes(6), id;
    for (unsigned char c : raw) { char b[3]; snprintf(b, sizeof b, "%02x", c); id += b; }
    job->id = id;
    job->label = label;
    job->unit = unit;
    job->t = (int64_t)time(nullptr);
    {
        std::lock_guard<std::mutex> g(mu);
        int running = 0;
        for (auto &kv : all) { std::lock_guard<std::mutex> jg(kv.second->mu); if (kv.second->state == "running") running++; }
        if (running >= kMaxRunning) throw Busy();
        int64_t now = (int64_t)time(nullptr);
        for (auto it = all.begin(); it != all.end();) {
            bool old;
            { std::lock_guard<std::mutex> jg(it->second->mu); old = it->second->state != "running" && now - it->second->t > 600; }
            it = old ? all.erase(it) : std::next(it);
        }
        all[id] = job;
    }
    std::thread([job, fn]() {
        try {
            json::Value r = fn(*job);
            std::lock_guard<std::mutex> g(job->mu);
            job->result = r;
            job->state = "done";
        } catch (const std::exception &e) {
            std::lock_guard<std::mutex> g(job->mu);
            job->error = e.what()[0] ? e.what() : "error";
            job->state = "error";
        }
        std::lock_guard<std::mutex> g(job->mu);
        job->t = (int64_t)time(nullptr);
    }).detach();
    return id;
}

bool view(const std::string &id, json::Value &out) {
    std::shared_ptr<Job> job;
    {
        std::lock_guard<std::mutex> g(mu);
        auto it = all.find(id);
        if (it == all.end()) return false;
        job = it->second;
    }
    std::lock_guard<std::mutex> g(job->mu);
    out = json::Value::object();
    out["state"] = job->state;
    out["label"] = job->label;
    out["unit"] = job->unit;
    out["done"] = (int64_t)job->done;
    out["total"] = (int64_t)job->total;
    out["result"] = job->state == "done" ? job->result : json::Value();
    out["error"] = job->error.empty() ? json::Value() : json::Value(job->error);
    return true;
}

} // namespace jobs
