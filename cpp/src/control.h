// Local control channel: a unix socket next to config.json (mode 0600, so only the panel's own user can use it),
// and the command line client for it. Lets you run servers from SSH or a console when the web page is unreachable.
#pragma once
#include <string>

namespace control {

void start(const std::string &base_dir);           // begin serving <base_dir>/control.sock in a background thread
bool is_command(const std::string &c);
constexpr int RUN_PANEL = -100;                    // client result: caller should go on and start the panel in this process
int client(int argc, char **argv);                 // argv[0] is the command name

} // namespace control
