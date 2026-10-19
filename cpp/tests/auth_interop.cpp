// Usage: auth_interop hash <pw> <stored> | mk <pw> | totp <secret> <counter>
#include "../src/auth.h"
#include <cstdio>
#include <cstring>
#include <cstdlib>
int main(int, char **argv) {
    if (!strcmp(argv[1], "hash")) return auth::verify_password(argv[2], argv[3]) ? 0 : 1;
    if (!strcmp(argv[1], "mk")) { puts(auth::hash_password(argv[2]).c_str()); return 0; }
    if (!strcmp(argv[1], "totp")) { puts(auth::totp_at(argv[2], strtoull(argv[3], 0, 10)).c_str()); return 0; }
}
