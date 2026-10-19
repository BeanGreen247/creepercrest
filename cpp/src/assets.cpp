// Embeds the UI via the assembler so the binary has no runtime file dependencies.
#include "assets.h"
#define INCBIN(sym, file) \
    asm(".section .rodata\n.global " #sym "\n" #sym ":\n.incbin \"" file "\"\n.global " #sym "_end\n" #sym "_end:\n.byte 0\n.previous\n")
INCBIN(ui_index_html, "ui/index.html");
INCBIN(ui_editor_js_gz, "build/editor.js.gz");
