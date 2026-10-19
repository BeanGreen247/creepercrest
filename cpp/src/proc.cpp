#include "proc.h"
#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>
#include <chrono>
#include <cstring>

namespace proc {

int run_capture(const std::vector<std::string> &argv, std::string *out, std::string *err, int timeout_ms) {
    int po[2], pe[2];
    if (pipe2(po, O_CLOEXEC) != 0) return -1;
    if (pipe2(pe, O_CLOEXEC) != 0) { close(po[0]); close(po[1]); return -1; }
    std::vector<char *> av;
    for (auto &a : argv) av.push_back(const_cast<char *>(a.c_str()));
    av.push_back(nullptr);
    pid_t pid = fork();
    if (pid < 0) { close(po[0]); close(po[1]); close(pe[0]); close(pe[1]); return -1; }
    if (pid == 0) {
        int nul = open("/dev/null", O_RDONLY);
        { sigset_t none; sigemptyset(&none); sigprocmask(SIG_SETMASK, &none, nullptr); }   // children must not inherit the panel's blocked signals
        dup2(nul, 0);
        dup2(po[1], 1);
        dup2(pe[1], 2);
        execvp(av[0], av.data());
        _exit(127);
    }
    close(po[1]); close(pe[1]);
    auto start = std::chrono::steady_clock::now();
    struct pollfd fds[2] = {{po[0], POLLIN, 0}, {pe[0], POLLIN, 0}};
    int open_fds = 2;
    bool timed_out = false;
    char buf[4096];
    while (open_fds > 0) {
        int left = timeout_ms - (int)std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - start).count();
        if (left <= 0) { timed_out = true; break; }
        if (poll(fds, 2, left) < 0) continue;
        for (int i = 0; i < 2; i++) {
            if (fds[i].fd < 0 || !(fds[i].revents & (POLLIN | POLLHUP))) continue;
            ssize_t n = read(fds[i].fd, buf, sizeof buf);
            if (n <= 0) { close(fds[i].fd); fds[i].fd = -1; open_fds--; }
            else if (i == 0 && out) out->append(buf, (size_t)n);
            else if (i == 1 && err) err->append(buf, (size_t)n);
        }
    }
    for (auto &f : fds) if (f.fd >= 0) close(f.fd);
    if (timed_out) kill(pid, SIGKILL);
    int st = 0;
    waitpid(pid, &st, 0);
    if (timed_out) return -1;
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

static bool v4_global(uint32_t v) {
    auto in = [&](uint32_t net, int bits) { return (v >> (32 - bits)) == (net >> (32 - bits)); };
    return !(in(0x00000000, 8) || in(0x0A000000, 8) || in(0x64400000, 10) || in(0x7F000000, 8) || in(0xA9FE0000, 16) ||
             in(0xAC100000, 12) || in(0xC0000000, 24) || in(0xC0000200, 24) || in(0xC0A80000, 16) || in(0xC6120000, 15) ||
             in(0xC6336400, 24) || in(0xCB007100, 24) || in(0xE0000000, 4) || in(0xF0000000, 4));
}

bool ip_is_global(const std::string &ip) {
    std::string s = ip.substr(0, ip.find('%'));
    in_addr a4;
    in6_addr a6;
    if (inet_pton(AF_INET, s.c_str(), &a4) == 1) return v4_global(ntohl(a4.s_addr));
    if (inet_pton(AF_INET6, s.c_str(), &a6) == 1) {
        const unsigned char *b = a6.s6_addr;
        static const unsigned char mapped[12] = {0,0,0,0,0,0,0,0,0,0,0xff,0xff};
        if (!memcmp(b, mapped, 12)) return v4_global(((uint32_t)b[12] << 24) | (b[13] << 16) | (b[14] << 8) | b[15]);
        if ((b[0] & 0xE0) != 0x20) return false;                          // only 2000::/3 is global unicast
        if (b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x0d && b[3] == 0xb8) return false;   // documentation
        return true;
    }
    return false;
}

bool ip_is_private_or_loopback(const std::string &ip) {
    std::string s = ip.substr(0, ip.find('%'));
    in_addr a4;
    in6_addr a6;
    if (inet_pton(AF_INET, s.c_str(), &a4) == 1) {
        uint32_t v = ntohl(a4.s_addr);
        return (v >> 24) == 10 || (v >> 24) == 127 || (v >> 20) == 0xAC1 || (v >> 16) == 0xC0A8 || (v >> 16) == 0xA9FE;
    }
    if (inet_pton(AF_INET6, s.c_str(), &a6) == 1) {
        static const unsigned char lo[16] = {0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1};
        return !memcmp(&a6, lo, 16) || (a6.s6_addr[0] & 0xFE) == 0xFC || (a6.s6_addr[0] == 0xFE && (a6.s6_addr[1] & 0xC0) == 0x80);
    }
    return false;
}

} // namespace proc
