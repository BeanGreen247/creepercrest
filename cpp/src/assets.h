#pragma once
#include <cstddef>
// Files embedded at build time (see Makefile / assets.cpp).
extern "C" {
extern const char ui_index_html[], ui_index_html_end[];
extern const char ui_editor_js_gz[], ui_editor_js_gz_end[];
}
