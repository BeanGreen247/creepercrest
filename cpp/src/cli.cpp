// User management commands: adduser / passwd / reset-2fa / deluser / users.
#include "cli.h"
#include "app.h"
#include "auth.h"
#include "fsu.h"
#include "proc.h"
#include "util.h"
#include <algorithm>
#include <cstring>
#include <termios.h>
#include <unistd.h>
#include <cstdio>
#include <ctime>
#include <iostream>

static std::string read_hidden(const char *prompt) {
    fputs(prompt, stderr);
    termios old{}, raw{};
    bool tty = tcgetattr(STDIN_FILENO, &old) == 0;
    if (tty) { raw = old; raw.c_lflag &= ~ECHO; tcsetattr(STDIN_FILENO, TCSAFLUSH, &raw); }
    std::string s;
    std::getline(std::cin, s);
    if (tty) { tcsetattr(STDIN_FILENO, TCSAFLUSH, &old); fputc('\n', stderr); }
    return s;
}

static std::string qr_ascii(const std::string &uri) {   // argument list, no shell involved
    std::string out;
    return proc::run_capture({"qrencode", "-t", "ANSIUTF8", "-m", "1", uri}, &out, nullptr, 5000) == 0 ? out : "";
}

static void show(const std::string &name, const std::string &pw, bool generated, const std::string &secret) {
    std::string uri = auth::totp_uri(name, secret);
    printf("\n  Username : %s\n", name.c_str());
    if (generated) printf("  Password : %s   (shown once - store it in your password manager)\n", pw.c_str());
    printf("  2FA key  : %s\n  2FA URI  : %s\n", secret.c_str(), uri.c_str());
    std::string art = qr_ascii(uri);
    if (!art.empty()) printf("\n%s\n", art.c_str());
    else printf("\n  (no QR shown - enter the 2FA key manually in your authenticator app,\n"
                "   or install `qrencode` to see a QR code here)\n");
    printf("\n");
}

static bool has(int argc, char **argv, const char *flag) {
    for (int i = 0; i < argc; i++) if (!strcmp(argv[i], flag)) return true;
    return false;
}

bool is_cli_command(const std::string &c) {
    static const char *names[] = {"adduser", "passwd", "reset-2fa", "deluser", "users", "--remove-user",
                                  "remove-user", "removeuser", "rmuser", "lan-mode", "config-get", "server-ports"};
    for (auto n : names) if (c == n) return true;
    return false;
}

int cli(int argc, char **argv) {
    std::string cmd = argv[0];
    if (cmd == "--remove-user" || cmd == "remove-user" || cmd == "removeuser" || cmd == "rmuser") cmd = "deluser";
    if (cmd == "config-get") {   // prints one top-level scalar from config.json (used by deploy.sh)
        if (argc < 2) return 2;
        const json::Value *v = app::cfg.raw.find(argv[1]);
        if (!v) return 1;
        if (v->is_str()) puts(v->s.c_str());
        else if (v->type == json::Value::Int) printf("%lld\n", (long long)v->i);
        else if (v->type == json::Value::Bool) puts(v->b ? "true" : "false");
        else return 1;
        return 0;
    }
    if (cmd == "server-ports") {   // server-port of every configured server (used to open firewall ports)
        if (auto *sv = app::cfg.raw.find("servers"); sv && sv->is_obj())
            for (auto &kv : sv->o) {
                std::string text;
                if (!fsu::read_file(fsu::expand_home(kv.second.str("directory")) + "/server.properties", text)) continue;
                size_t p = 0;
                while (p < text.size()) {
                    size_t e = text.find('\n', p);
                    std::string line = text.substr(p, e == std::string::npos ? e : e - p);
                    if (line.compare(0, 12, "server-port=") == 0) puts(line.substr(12).c_str());
                    if (e == std::string::npos) break;
                    p = e + 1;
                }
            }
        return 0;
    }
    if (cmd == "lan-mode") {
        std::string want = argc > 1 ? argv[1] : "status";
        for (auto &c : want) c = (char)tolower((unsigned char)c);
        if (want == "on" || want == "off") {
            app::cfg.raw["lan_mode"] = want == "on";
            app::save_config();
            printf("LAN mode %s. Restart CreeperCrest to apply.\n", want == "on" ? "ENABLED" : "disabled");
            if (want == "on")
                puts("Clients on private networks (192.168.x, 10.x, 172.16-31.x, localhost) need no login; "
                     "any other address still must sign in. Do NOT port-forward this panel to the internet.");
        } else printf("LAN mode is %s\n", app::cfg.raw.flag("lan_mode") ? "on" : "off");
        return 0;
    }
    json::Value users = auth::load_users();
    if (cmd == "users") {
        std::vector<std::string> names;
        for (auto &kv : users.o) names.push_back(kv.first);
        std::sort(names.begin(), names.end());
        for (auto &n : names) puts(n.c_str());
        return 0;
    }
    if (argc < 2) { printf("usage: creepercrest %s <username> [--prompt | --yes]\n", argv[0]); return 2; }
    std::string name = argv[1];
    for (auto &c : name) c = (char)tolower((unsigned char)c);
    while (!name.empty() && isspace((unsigned char)name.back())) name.pop_back();
    bool prompt = has(argc, argv, "--prompt");

    auto pick_password = [&](std::string &pw) -> bool {
        if (!prompt) { pw = util::token_urlsafe(15); return true; }
        std::string a = read_hidden("Password (12+ chars): "), b = read_hidden("Repeat: ");
        if (a != b || a.size() < 12) { fprintf(stderr, "Passwords differ or are shorter than 12 characters.\n"); exit(1); }
        pw = a;
        return false;
    };

    if (cmd == "adduser") {
        if (!auth::valid_username(name)) { puts("Username: 2-32 chars, lowercase letters, digits, . _ -"); return 2; }
        if (users.find(name)) { printf("User '%s' already exists (use passwd / reset-2fa).\n", name.c_str()); return 1; }
        std::string pw;
        bool gen = pick_password(pw), secret_dummy = false;
        (void)secret_dummy;
        std::string secret = auth::new_totp_secret();
        char ts[32];
        time_t t = time(nullptr);
        strftime(ts, sizeof ts, "%Y-%m-%dT%H:%M:%S", localtime(&t));
        json::Value u = json::Value::object();
        u["pw"] = auth::hash_password(pw);
        u["totp"] = secret;
        u["created"] = std::string(ts);
        users[name] = u;
        if (!auth::save_users(users)) { fprintf(stderr, "cannot write %s\n", auth::users_file.c_str()); return 1; }
        show(name, pw, gen, secret);
        return 0;
    }
    if (!users.find(name)) { printf("No such user: %s\n", name.c_str()); return 1; }
    if (cmd == "passwd") {
        std::string pw;
        bool gen = pick_password(pw);
        users[name]["pw"] = auth::hash_password(pw);
        if (!auth::save_users(users)) { fprintf(stderr, "cannot write %s\n", auth::users_file.c_str()); return 1; }
        if (gen) printf("\n  New password for %s: %s\n\n", name.c_str(), pw.c_str());
        else printf("\n  Password updated for %s.\n\n", name.c_str());
        return 0;
    }
    if (cmd == "reset-2fa") {
        std::string secret = auth::new_totp_secret();
        users[name]["totp"] = secret;
        if (!auth::save_users(users)) { fprintf(stderr, "cannot write %s\n", auth::users_file.c_str()); return 1; }
        show(name, "", false, secret);
        return 0;
    }
    if (cmd == "deluser") {
        if (!has(argc, argv, "--yes") && !has(argc, argv, "-y")) {
            printf("Remove user '%s'? They will be signed out and cannot log in again. [y/N]: ", name.c_str());
            std::string ans;
            std::getline(std::cin, ans);
            if (ans != "y" && ans != "Y") { puts("Cancelled."); return 1; }
        }
        users.erase(name);
        if (!auth::save_users(users)) { fprintf(stderr, "cannot write %s\n", auth::users_file.c_str()); return 1; }
        printf("Removed %s.\n", name.c_str());
        return 0;
    }
    return 2;
}
