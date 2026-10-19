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
// Returns the matching time step or 0. Does not consume it; call mark_totp_used after a full login.
uint64_t verify_totp(const std::string &secret, const std::string &code, const std::string &user);
void mark_totp_used(const std::string &user, uint64_t step);
std::string totp_uri(const std::string &user, const std::string &secret);

bool valid_username(const std::string &name);

} // namespace auth
