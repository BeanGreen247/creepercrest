# CreeperCrest C++ port

Same features, same UI, same `config.json` / `users.json`, smaller footprint. One binary, no Python.

Dependencies: OpenSSL (scrypt, HMAC-SHA1, TLS, HTTPS client), libzip + zlib. HTTP/1.1 server, JSON and base32/64 are in-tree. `qrencode` is optional (QR codes).

Status: all phases done and tested on the dev machine.

1. Core: JSON, users.json compatibility (scrypt, TOTP), user CLI. Hashes and codes interoperate with Python.
2. HTTP server, TLS, sessions, CSRF, login throttle, login/2FA pages, security headers, embedded UI.
3. Process manager: start/stop/restart, console ring buffer, commands, autostart, hardening, whitelist auto-ban.
4. File manager and editor API, uploads (large bodies spooled to disk), browse, properties.
5. Backups (libzip), restore, weekly scheduler with a per-server selection, resource packs.
6. JAR downloads (Vanilla/Paper/Purpur/Fabric) over OpenSSL HTTPS with an SSRF guard, background jobs.
7. Installer (`deploy.sh`), systemd unit, README, footprint comparison.

Tests live outside the repo for now (HTTP API suite, lifecycle run with a real Paper server, sanitizer run). See `tests/` for the interop checks.
