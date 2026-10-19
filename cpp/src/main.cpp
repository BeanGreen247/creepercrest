#include "auth.h"
#include "app.h"
#include "backup.h"
#include "cli.h"
#include "registry.h"
#include <csignal>
#include <cstdio>
#include <pthread.h>
#include <thread>
#include <cstdlib>
#include <string>
#include <unistd.h>
#include <libgen.h>
#include <limits.h>

static std::string base_dir() {
    char buf[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof buf - 1);
    if (n <= 0) return ".";
    buf[n] = 0;
    return dirname(buf);
}

int main(int argc, char **argv) {
    const char *env = getenv("CREEPERCREST_DIR");
    std::string base = env && *env ? env : base_dir();
    auth::users_file = base + "/users.json";
    if (!app::load_config(base)) return 1;
    if (argc > 1 && is_cli_command(argv[1])) return cli(argc - 1, argv + 1);
    registry::load();
    http::Options opt;
    opt.spool_dir = base;
    opt.body_gate = app::body_gate;
    opt.unlimited_ips = app::cfg.trusted_proxies;   // a reverse proxy carries many clients from one address
    opt.host = app::cfg.host;
    opt.port = app::cfg.port;
    opt.tls_cert = app::cfg.tls_cert;
    opt.tls_key = app::cfg.tls_key;
    if (app::cfg.auth_disabled)
        fprintf(stderr, "WARNING: auth_disabled is set - the panel has NO login. Anyone who can reach it controls your servers.\n");
    else if (app::cfg.lan_mode)
        fprintf(stderr, "LAN mode ON: private-network clients need no login; other addresses must sign in. Never port-forward this panel.\n");
    if (!app::cfg.auth_disabled && auth::load_users().o.empty() && !app::cfg.lan_mode)
        fprintf(stderr, "WARNING: no users exist, so the panel is locked. Create one with:  creepercrest adduser <name>\n");
    // graceful shutdown: stop every managed server, then exit
    sigset_t set;
    sigemptyset(&set);
    sigaddset(&set, SIGINT);
    sigaddset(&set, SIGTERM);
    pthread_sigmask(SIG_BLOCK, &set, nullptr);
    std::thread([set]() mutable {
        int sig;
        sigwait(&set, &sig);
        printf("\nShutting down - stopping all running servers...\n");
        fflush(stdout);
        registry::stop_all();
        _exit(0);
    }).detach();
    std::string err;
    int ls = http::listen_on(opt, err);     // bind first: a port clash must not leave autostarted servers behind
    if (ls < 0) { fprintf(stderr, "%s\n", err.c_str()); return 1; }
    printf("CreeperCrest  ->  %s://%s:%d\nPress Ctrl+C to stop.\n", opt.tls_cert.empty() ? "http" : "https", opt.host.c_str(), opt.port);
    fflush(stdout);
    backup::start_scheduler();
    for (auto &s : registry::all()) {
        if (s->cfg().flag("autostart")) {
            printf("  Auto-starting %s...\n", s->id.c_str());
            auto r = s->start();
            if (!r.first) printf("    Failed: %s\n", r.second.c_str());
        }
    }
    fflush(stdout);
    http::run(ls, opt, app::handle);
    return 0;
}
