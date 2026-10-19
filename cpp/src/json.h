// Minimal JSON value: insertion-ordered objects (like Python dicts), int64/double split.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace json {

struct Value;
using Array = std::vector<Value>;
using Object = std::vector<std::pair<std::string, Value>>;

struct Value {
    enum Type { Null, Bool, Int, Real, Str, Arr, Obj } type = Null;
    bool b = false;
    int64_t i = 0;
    double d = 0;
    std::string s;
    Array a;
    Object o;

    Value() = default;
    Value(std::nullptr_t) {}
    Value(bool v) : type(Bool), b(v) {}
    Value(int v) : type(Int), i(v) {}
    Value(int64_t v) : type(Int), i(v) {}
    Value(double v) : type(Real), d(v) {}
    Value(const char *v) : type(Str), s(v) {}
    Value(std::string v) : type(Str), s(std::move(v)) {}
    static Value array() { Value v; v.type = Arr; return v; }
    static Value object() { Value v; v.type = Obj; return v; }

    bool is_obj() const { return type == Obj; }
    bool is_arr() const { return type == Arr; }
    bool is_str() const { return type == Str; }

    const Value *find(const std::string &k) const {
        for (auto &kv : o) if (kv.first == k) return &kv.second;
        return nullptr;
    }
    Value &operator[](const std::string &k) {
        if (type != Obj) { *this = object(); }
        for (auto &kv : o) if (kv.first == k) return kv.second;
        o.emplace_back(k, Value());
        return o.back().second;
    }
    void push(Value v) { if (type != Arr) *this = array(); a.push_back(std::move(v)); }
    void erase(const std::string &k) {
        for (size_t n = 0; n < o.size(); n++) if (o[n].first == k) { o.erase(o.begin() + n); return; }
    }
    std::string str(const std::string &k, const std::string &def = "") const {
        auto *v = find(k); return v && v->type == Str ? v->s : def;
    }
    int64_t num(const std::string &k, int64_t def = 0) const {
        auto *v = find(k);
        if (!v) return def;
        if (v->type == Int) return v->i;
        if (v->type == Real) return (int64_t)v->d;
        if (v->type == Bool) return v->b;
        return def;
    }
    bool flag(const std::string &k, bool def = false) const {
        auto *v = find(k);
        if (!v) return def;
        if (v->type == Bool) return v->b;
        if (v->type == Int) return v->i != 0;
        return def;
    }
};

inline void escape(std::string &out, const std::string &s) {
    out += '"';
    for (unsigned char c : s) {
        switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20) { char buf[8]; snprintf(buf, sizeof buf, "\\u%04x", c); out += buf; }
            else out += (char)c;
        }
    }
    out += '"';
}

inline void dump(const Value &v, std::string &out, int indent = -1, int depth = 0) {
    auto nl = [&](int d) { if (indent >= 0) { out += '\n'; out.append((size_t)d * indent, ' '); } };
    switch (v.type) {
    case Value::Null: out += "null"; break;
    case Value::Bool: out += v.b ? "true" : "false"; break;
    case Value::Int: out += std::to_string(v.i); break;
    case Value::Real: { char buf[32]; snprintf(buf, sizeof buf, "%.17g", v.d); out += buf; break; }
    case Value::Str: escape(out, v.s); break;
    case Value::Arr:
        if (v.a.empty()) { out += "[]"; break; }
        out += '[';
        for (size_t n = 0; n < v.a.size(); n++) {
            if (n) out += ',';
            nl(depth + 1); dump(v.a[n], out, indent, depth + 1);
        }
        nl(depth); out += ']'; break;
    case Value::Obj:
        if (v.o.empty()) { out += "{}"; break; }
        out += '{';
        for (size_t n = 0; n < v.o.size(); n++) {
            if (n) out += ',';
            nl(depth + 1); escape(out, v.o[n].first); out += indent >= 0 ? ": " : ":";
            dump(v.o[n].second, out, indent, depth + 1);
        }
        nl(depth); out += '}'; break;
    }
}

inline std::string dump(const Value &v, int indent = -1) { std::string s; dump(v, s, indent); return s; }

struct Parser {
    const char *p, *e;
    int depth = 0;
    [[noreturn]] void fail() { throw std::runtime_error("bad json"); }
    void ws() { while (p < e && (*p == ' ' || *p == '\n' || *p == '\t' || *p == '\r')) p++; }
    bool lit(const char *w) {
        size_t n = strlen(w);
        if ((size_t)(e - p) >= n && !memcmp(p, w, n)) { p += n; return true; }
        return false;
    }
    static void utf8(std::string &o, unsigned cp) {
        if (cp < 0x80) o += (char)cp;
        else if (cp < 0x800) { o += (char)(0xC0 | cp >> 6); o += (char)(0x80 | (cp & 63)); }
        else if (cp < 0x10000) { o += (char)(0xE0 | cp >> 12); o += (char)(0x80 | ((cp >> 6) & 63)); o += (char)(0x80 | (cp & 63)); }
        else { o += (char)(0xF0 | cp >> 18); o += (char)(0x80 | ((cp >> 12) & 63)); o += (char)(0x80 | ((cp >> 6) & 63)); o += (char)(0x80 | (cp & 63)); }
    }
    unsigned hex4() {
        if (e - p < 4) fail();
        unsigned v = 0;
        for (int n = 0; n < 4; n++) {
            char c = *p++; v <<= 4;
            if (c >= '0' && c <= '9') v |= c - '0';
            else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
            else fail();
        }
        return v;
    }
    std::string string() {
        if (p >= e || *p != '"') fail();
        p++;
        std::string o;
        while (true) {
            if (p >= e) fail();
            char c = *p++;
            if (c == '"') break;
            if (c != '\\') { o += c; continue; }
            if (p >= e) fail();
            switch (char x = *p++) {
            case 'n': o += '\n'; break;
            case 'r': o += '\r'; break;
            case 't': o += '\t'; break;
            case 'b': o += '\b'; break;
            case 'f': o += '\f'; break;
            case '/': case '\\': case '"': o += x; break;
            case 'u': {
                unsigned cp = hex4();
                if (cp >= 0xD800 && cp < 0xDC00 && e - p >= 6 && p[0] == '\\' && p[1] == 'u') {
                    p += 2; unsigned lo = hex4();
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                }
                utf8(o, cp); break;
            }
            default: fail();
            }
        }
        return o;
    }
    Value value() {
        ws();
        if (p >= e || ++depth > 64) fail();
        Value v;
        char c = *p;
        if (c == '{') {
            p++; v = Value::object(); ws();
            if (p < e && *p == '}') p++;
            else while (true) {
                ws(); std::string k = string(); ws();
                if (p >= e || *p++ != ':') fail();
                Value x = value();
                v.o.emplace_back(std::move(k), std::move(x)); ws();
                if (p >= e) fail();
                if (*p == ',') { p++; continue; }
                if (*p == '}') { p++; break; }
                fail();
            }
        } else if (c == '[') {
            p++; v = Value::array(); ws();
            if (p < e && *p == ']') p++;
            else while (true) {
                v.a.push_back(value()); ws();
                if (p >= e) fail();
                if (*p == ',') { p++; continue; }
                if (*p == ']') { p++; break; }
                fail();
            }
        } else if (c == '"') v = Value(string());
        else if (lit("true")) v = Value(true);
        else if (lit("false")) v = Value(false);
        else if (lit("null")) v = Value();
        else {
            const char *s = p; bool real = false;
            if (*p == '-') p++;
            while (p < e && ((*p >= '0' && *p <= '9') || *p == '.' || *p == 'e' || *p == 'E' || *p == '+' || *p == '-')) {
                if (*p == '.' || *p == 'e' || *p == 'E') real = true;
                p++;
            }
            if (p == s) fail();
            std::string t(s, p);
            if (real) v = Value(strtod(t.c_str(), nullptr));
            else v = Value((int64_t)strtoll(t.c_str(), nullptr, 10));
        }
        depth--;
        return v;
    }
};

inline Value parse(const std::string &s) {
    Parser ps{s.data(), s.data() + s.size()};
    Value v = ps.value();
    ps.ws();
    if (ps.p != ps.e) ps.fail();
    return v;
}

} // namespace json
