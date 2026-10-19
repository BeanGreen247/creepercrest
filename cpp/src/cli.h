#pragma once
#include <string>
bool is_cli_command(const std::string &c);
int cli(int argc, char **argv);   // argv[0] is the command name
