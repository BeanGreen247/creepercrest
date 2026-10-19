#include "jars.h"
#include "fsu.h"
#include "registry.h"
#include <unistd.h>
#include <algorithm>
#include <cstdio>

using json::Value;

namespace jars {

static Value get_json(const std::string &url) {
    return json::parse(fetch::get_string(url, registry::fetch_policy(), 32 << 20));
}

bool known_type(const std::string &t) { return t == "vanilla" || t == "paper" || t == "purpur" || t == "fabric"; }

static const char *MOJANG = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json";

std::vector<std::string> versions(const std::string &type) {
    std::vector<std::string> out;
    if (type == "vanilla") {
        Value m = get_json(MOJANG);
        for (auto &v : m.find("versions")->a) if (v.str("type") == "release") out.push_back(v.str("id"));
    } else if (type == "paper") {
        Value d = get_json("https://fill.papermc.io/v3/projects/paper");
        for (auto &grp : d.find("versions")->o)
            for (auto &v : grp.second.a) if (v.is_str() && v.s.find('-') == std::string::npos) out.push_back(v.s);
    } else if (type == "purpur") {
        Value d = get_json("https://api.purpurmc.org/v2/purpur");
        for (auto &v : d.find("versions")->a) out.push_back(v.s);
        std::reverse(out.begin(), out.end());
    } else if (type == "fabric") {
        Value d = get_json("https://meta.fabricmc.net/v2/versions/game");
        for (auto &v : d.a) if (v.flag("stable")) out.push_back(v.str("version"));
    } else throw std::runtime_error("unknown server type");
    return out;
}

static std::string first_stable(const std::string &url) {
    Value d = get_json(url);
    for (auto &v : d.a) if (v.flag("stable")) return v.str("version");
    throw std::runtime_error("no stable version found");
}

static std::string jar_url(const std::string &type, const std::string &ver) {
    if (type == "vanilla") {
        Value m = get_json(MOJANG);
        for (auto &v : m.find("versions")->a) {
            if (v.str("id") != ver) continue;
            Value meta = get_json(v.str("url"));
            auto *dl = meta.find("downloads");
            auto *srv = dl ? dl->find("server") : nullptr;
            if (!srv) throw std::runtime_error("no server jar published for this version");
            return srv->str("url");
        }
        throw std::runtime_error("unknown version");
    }
    if (type == "paper") {
        Value d = get_json("https://fill.papermc.io/v3/projects/paper/versions/" + ver + "/builds/latest");
        auto *dl = d.find("downloads");
        auto *def = dl ? dl->find("server:default") : nullptr;
        if (!def) throw std::runtime_error("no download found for this version");
        return def->str("url");
    }
    if (type == "purpur") return "https://api.purpurmc.org/v2/purpur/" + ver + "/latest/download";
    if (type == "fabric") {
        std::string loader = first_stable("https://meta.fabricmc.net/v2/versions/loader");
        std::string installer = first_stable("https://meta.fabricmc.net/v2/versions/installer");
        return "https://meta.fabricmc.net/v2/versions/loader/" + ver + "/" + loader + "/" + installer + "/server/jar";
    }
    throw std::runtime_error("unknown server type");
}

void download(const std::string &type, const std::string &ver, const std::string &dest, jobs::Job *job) {
    if (ver.empty() || ver.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-") != std::string::npos)
        throw std::runtime_error("invalid version");
    std::string tmp = dest + ".part";
    FILE *f = fopen(tmp.c_str(), "wb");
    if (!f) throw std::runtime_error("cannot write " + tmp);
    try {
        fetch::get(jar_url(type, ver), registry::fetch_policy(),
                   [&](const char *p, size_t n) { if (fwrite(p, 1, n, f) != n) throw std::runtime_error("disk write failed"); },
                   [&](int64_t d, int64_t t) { if (job) { job->total = t; job->done = d; } }, (int64_t)1 << 29);
        if (fclose(f) != 0) { f = nullptr; throw std::runtime_error("disk write failed"); }
        f = nullptr;
        if (rename(tmp.c_str(), dest.c_str()) != 0) throw std::runtime_error("cannot move JAR into place");
    } catch (...) {
        if (f) fclose(f);
        unlink(tmp.c_str());
        throw;
    }
}

int required_java(const std::string &ver) {
    try {
        Value m = get_json(MOJANG);
        for (auto &v : m.find("versions")->a) {
            if (v.str("id") != ver) continue;
            Value meta = get_json(v.str("url"));
            auto *jv = meta.find("javaVersion");
            return jv ? (int)jv->num("majorVersion") : 0;
        }
    } catch (...) {}
    return 0;
}

} // namespace jars
