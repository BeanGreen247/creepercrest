#include "control.h"
#include "app.h"
#include "proc.h"
#include "registry.h"
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <sstream>
#include <thread>
#include <vector>

namespace control {

static std::string sock_path(const std::string &base) { return base + "/control.sock"; }

// ── server side ──────────────────────────────────────────────────────────────

static std::vector<std::string> words(const std::string &line) {
    std::vector<std::string> w;
    std::istringstream is(line);
    for (std::string t; is >> t;) w.push_back(t);
    return w;
}

static std::string handle(const std::string &line) {
    auto w = words(line);
    if (w.empty()) return "ERR empty request\n";
    const std::string &c = w[0];
    if (c == "list") {
        std::string out = "OK\n";
        for (auto &s : registry::all()) {
            json::Value cf = s->cfg();
            out += s->id + "\t" + cf.str("name", s->id) + "\t" + (s->is_running() ? "running" : "stopped") + "\n";
        }
        return out;
    }
    if (c == "panel-restart" || c == "panel-shutdown") {
        app::panel_power(c == "panel-restart");
        return c == "panel-restart" ? "OK restarting (running servers are stopped first)\n" : "OK shutting down (running servers are stopped first)\n";
    }
    if (w.size() < 2) return "ERR missing server id\n";
    auto srv = registry::get(w[1]);
    if (!srv) return "ERR no such server: " + w[1] + "\n";
    std::pair<bool, std::string> r;
    if (c == "start") r = srv->start();
    else if (c == "stop") r = srv->stop();
    else if (c == "restart") r = srv->restart();
    else if (c == "send") {
        size_t p = line.find(w[1]);
        std::string cmd = p == std::string::npos ? "" : line.substr(p + w[1].size());
        size_t f = cmd.find_first_not_of(" \t");
        if (f == std::string::npos) return "ERR nothing to send\n";
        r = srv->send_command(cmd.substr(f));
    } else if (c == "console") {
        long n = w.size() > 2 ? strtol(w[2].c_str(), nullptr, 10) : 30;
        if (n < 1) n = 30;
        std::vector<std::string> lines;
        long seq;
        bool full;
        srv->logs_since(0, lines, seq, full);
        std::string out = "OK\n";
        for (size_t i = lines.size() > (size_t)n ? lines.size() - (size_t)n : 0; i < lines.size(); i++) out += lines[i] + "\n";
        return out;
    } else return "ERR unknown command: " + c + "\n";
    return (r.first ? "OK " : "ERR ") + r.second + "\n";
}

static void serve(int ls) {
    for (;;) {
        int fd = accept4(ls, nullptr, nullptr, SOCK_CLOEXEC);
        if (fd < 0) { if (errno == EINTR) continue; return; }
        std::thread([fd] {
            std::string line;
            char ch;
            while (line.size() < 4096 && read(fd, &ch, 1) == 1 && ch != '\n') line += ch;
            std::string reply = handle(line);
            for (size_t o = 0; o < reply.size();) {
                ssize_t n = write(fd, reply.data() + o, reply.size() - o);
                if (n <= 0) break;
                o += (size_t)n;
            }
            close(fd);
        }).detach();
    }
}

void start(const std::string &base) {
    std::string path = sock_path(base);
    sockaddr_un a{};
    a.sun_family = AF_UNIX;
    if (path.size() >= sizeof a.sun_path) { fprintf(stderr, "control socket disabled: path too long (%s)\n", path.c_str()); return; }
    memcpy(a.sun_path, path.c_str(), path.size() + 1);
    int ls = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (ls < 0) return;
    unlink(path.c_str());
    mode_t old = umask(0177);                       // the socket is created owner-only, never briefly wider
    int rc = bind(ls, (sockaddr *)&a, sizeof a);
    umask(old);
    if (rc != 0 || listen(ls, 8) != 0) { fprintf(stderr, "control socket disabled: %s\n", strerror(errno)); close(ls); return; }
    chmod(path.c_str(), 0600);
    std::thread(serve, ls).detach();
}

// ── client side ──────────────────────────────────────────────────────────────

static bool ask(const std::string &base, const std::string &req, std::string &reply) {
    std::string path = sock_path(base);
    sockaddr_un a{};
    a.sun_family = AF_UNIX;
    if (path.size() >= sizeof a.sun_path) return false;
    memcpy(a.sun_path, path.c_str(), path.size() + 1);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return false;
    if (connect(fd, (sockaddr *)&a, sizeof a) != 0) { close(fd); return false; }
    std::string r = req + "\n";
    if (write(fd, r.data(), r.size()) != (ssize_t)r.size()) { close(fd); return false; }
    char buf[4096];
    ssize_t n;
    reply.clear();
    while ((n = read(fd, buf, sizeof buf)) > 0) reply.append(buf, (size_t)n);
    close(fd);
    return true;
}

struct Item { std::string id, name, state; };

static bool fetch_list(const std::string &base, std::vector<Item> &out, bool &up) {
    std::string rep;
    up = ask(base, "list", rep);
    if (!up) return false;
    std::istringstream is(rep);
    std::string l;
    std::getline(is, l);
    while (std::getline(is, l)) {
        Item it;
        size_t a = l.find('\t'), b = a == std::string::npos ? a : l.find('\t', a + 1);
        if (b == std::string::npos) continue;
        it = {l.substr(0, a), l.substr(a + 1, b - a - 1), l.substr(b + 1)};
        out.push_back(it);
    }
    return true;
}

static void print_list(const std::vector<Item> &v) {
    if (v.empty()) { puts("No servers are configured."); return; }
    for (size_t i = 0; i < v.size(); i++)
        printf("  %2zu  %-20s %-9s %s\n", i + 1, v[i].id.c_str(), v[i].state.c_str(), v[i].name.c_str());
}

// A number from the list, or an id. With nothing given, show the list and let the user pick.
static bool pick(const std::string &base, const std::string &arg, std::string &id) {
    std::vector<Item> v;
    bool up;
    if (!fetch_list(base, v, up)) return false;
    if (v.empty()) { fprintf(stderr, "No servers are configured.\n"); return false; }
    std::string sel = arg;
    if (sel.empty()) {
        if (v.size() == 1) sel = "1";
        else {
            if (!isatty(STDIN_FILENO)) { fprintf(stderr, "Give a server number or id (run `creepercrest -l` to see them).\n"); return false; }
            print_list(v);
            printf("Pick a server [1-%zu]: ", v.size());
            fflush(stdout);
            if (!std::getline(std::cin, sel)) return false;
        }
    }
    char *e;
    long n = strtol(sel.c_str(), &e, 10);
    if (!sel.empty() && !*e) {
        if (n < 1 || (size_t)n > v.size()) { fprintf(stderr, "No server number %ld (1-%zu).\n", n, v.size()); return false; }
        id = v[(size_t)n - 1].id;
        return true;
    }
    for (auto &it : v) if (it.id == sel) { id = sel; return true; }
    fprintf(stderr, "No such server: %s\n", sel.c_str());
    return false;
}

bool is_command(const std::string &c) {
    static const char *n[] = {"servers", "list", "-l", "start", "-s", "stop", "-t", "restart", "-r", "console", "-c", "send",
                              "panel-start", "panel-restart", "panel-shutdown", "reboot", "shutdown", "help", "-h", "--help"};
    for (auto x : n) if (c == x) return true;
    return false;
}

static void usage() {
    puts("creepercrest - control the panel and its servers from a terminal (SSH or a local console)\n"
         "\n"
         "Servers (the panel must be running; leave the number out to pick from a list):\n"
         "  creepercrest -l | list | servers      list servers with their numbers\n"
         "  creepercrest -s | start   [N|id]      start a server\n"
         "  creepercrest -t | stop    [N|id]      stop a server\n"
         "  creepercrest -r | restart [N|id]      restart a server\n"
         "  creepercrest -c | console [N|id] [lines]   show the last console lines (default 30)\n"
         "  creepercrest send [N|id] <command...> run a console command, e.g. send 1 say hello\n"
         "\n"
         "The panel itself:\n"
         "  creepercrest panel-start              start it: through systemd when the unit exists, else in this terminal\n"
         "  creepercrest panel-restart | reboot   stop all servers and restart the panel\n"
         "  creepercrest panel-shutdown | shutdown   stop all servers and shut the panel down\n"
         "\n"
         "Run these as the user that runs the panel (for the service: sudo -u <user> creepercrest ...),\n"
         "with CREEPERCREST_DIR set if the panel keeps its files somewhere else.\n"
         "Accounts: adduser, passwd, reset-2fa, deluser, users.  Network: lan-mode on|off.");
}

static int start_panel(const std::string &base) {
    std::string rep;
    if (ask(base, "list", rep)) { puts("The panel is already running."); return 0; }
    std::string out;
    if (proc::run_capture({"systemctl", "cat", "creepercrest"}, &out, nullptr, 3000) == 0) {
        puts("Starting the creepercrest service...");
        if (proc::run_capture({"systemctl", "start", "creepercrest"}, &out, nullptr, 30000) == 0) { puts("Started."); return 0; }
        fprintf(stderr, "Could not start the service (needs root): sudo systemctl start creepercrest\n");
        return 1;
    }
    puts("No creepercrest service found - starting the panel in this terminal (Ctrl+C stops it).");
    return RUN_PANEL;
}

int client(int argc, char **argv) {
    std::string cmd = argv[0], base = app::cfg.base_dir;
    if (cmd == "help" || cmd == "-h" || cmd == "--help") { usage(); return 0; }
    if (cmd == "panel-start") return start_panel(base);
    auto down = [&] { fprintf(stderr, "The panel is not running (no answer on %s). Start it with: creepercrest panel-start\n", sock_path(base).c_str()); return 1; };
    std::string rep;
    if (cmd == "list" || cmd == "-l" || cmd == "servers") {
        std::vector<Item> v;
        bool up;
        if (!fetch_list(base, v, up)) return down();
        print_list(v);
        return 0;
    }
    if (cmd == "panel-restart" || cmd == "reboot" || cmd == "panel-shutdown" || cmd == "shutdown") {
        bool re = cmd == "panel-restart" || cmd == "reboot";
        if (!ask(base, re ? "panel-restart" : "panel-shutdown", rep)) {
            if (re) { puts("The panel was not running, starting it."); return start_panel(base); }
            return down();
        }
        fputs(rep.c_str(), stdout);
        return rep.compare(0, 2, "OK") == 0 ? 0 : 1;
    }
    std::string id;
    std::string verb = cmd == "-s" ? "start" : cmd == "-t" ? "stop" : cmd == "-r" ? "restart" : cmd == "-c" ? "console" : cmd;
    std::string arg = argc > 1 ? argv[1] : "";
    std::vector<Item> probe;
    bool up;
    if (!fetch_list(base, probe, up)) return down();
    if (verb == "send") {
        // the first word is a server number or id only when it matches one; otherwise it is part of the command
        std::string first = arg, text;
        int from = 2;
        char *e;
        long n = strtol(first.c_str(), &e, 10);
        bool is_sel = !first.empty() && ((!*e && n >= 1 && (size_t)n <= probe.size()));
        for (auto &it : probe) if (it.id == first) is_sel = true;
        if (!is_sel) { first.clear(); from = 1; }
        if (argc <= from) { fprintf(stderr, "usage: creepercrest send [N|id] <command...>\n"); return 2; }
        for (int i = from; i < argc; i++) text += std::string(i > from ? " " : "") + argv[i];
        if (!pick(base, first, id)) return 1;
        if (!ask(base, "send " + id + " " + text, rep)) return down();
    } else {
        if (verb != "start" && verb != "stop" && verb != "restart" && verb != "console") { usage(); return 2; }
        if (!pick(base, arg, id)) return 1;
        std::string req = verb + " " + id;
        if (verb == "console" && argc > 2) req += std::string(" ") + argv[2];
        if (verb != "console") printf("%s %s...\n", verb == "start" ? "Starting" : verb == "stop" ? "Stopping" : "Restarting", id.c_str());
        fflush(stdout);
        if (!ask(base, req, rep)) return down();
    }
    if (rep.compare(0, 3, "OK\n") == 0) { fputs(rep.c_str() + 3, stdout); return 0; }
    if (rep.compare(0, 2, "OK") == 0) { fputs(rep.c_str() + 3, stdout); return 0; }
    fputs(rep.compare(0, 4, "ERR ") == 0 ? rep.c_str() + 4 : rep.c_str(), stderr);
    return 1;
}

} // namespace control
