// Users, password hashing (scrypt), TOTP 2FA. Formats match the Python panel byte for byte.
#pragma once
#include "json.h"
#include <string>

namespace auth {

extern std::string users_file;          // set by main from the base dir
json::Value load_users();
bool save_users(const json::Value &users);

std::string hash_password(const std::string &pw);
bool verify_password(const std::string &pw, const std::string &stored);

std::string new_totp_secret();
std::string totp_at(const std::string &secret_b32, uint64_t counter);
// Returns the matching time step or 0. Does not claim it; call consume_totp once the whole login is accepted.
uint64_t verify_totp(const std::string &secret, const std::string &code, const std::string &user);
// Atomically claims a time step for a user: false if it (or a later one) was already used, so one code cannot open
// two sessions even when two requests carry it at the same moment.
bool consume_totp(const std::string &user, uint64_t step);
std::string totp_uri(const std::string &user, const std::string &secret);

bool valid_username(const std::string &name);

} // namespace auth
