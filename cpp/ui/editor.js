/* CreeperCrest built-in editor: a textarea with a syntax-highlighted mirror, line numbers and find/replace.
   API (same as the bundle it replaces): CCEditor.create(parent, {text, filename, onChange, onSave}) -> {getValue, focus, destroy} */
(function () {
  "use strict";
  var MAX_HL = 300000;   // above this many characters, highlighting is skipped to keep typing instant

  var CSS = ".cce{position:relative;height:100%;display:flex;flex-direction:column;background:#0d1117;color:#c9d1d9;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}" +
    ".cce-bar{display:none;gap:.4rem;align-items:center;padding:.35rem .5rem;border-bottom:1px solid #21262d;background:#161b22;flex-wrap:wrap}" +
    ".cce-bar.on{display:flex}.cce-bar input{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:.25rem .45rem;font:inherit;width:11rem;min-width:0}" +
    ".cce-bar input:focus{outline:none;border-color:#58a6ff}.cce-bar button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:.2rem .5rem;font:inherit;cursor:pointer}" +
    ".cce-bar button:hover{border-color:#58a6ff}.cce-bar button.on{background:#1f6feb;border-color:#1f6feb;color:#fff}.cce-n{color:#7d8590;font-size:.75rem}" +
    ".cce-body{position:relative;flex:1;min-height:0;overflow:hidden}" +
    ".cce-layer,.cce-ta{position:absolute;left:0;top:0;margin:0;border:0;padding:6px 8px 6px 0;font:inherit;line-height:inherit;letter-spacing:0;tab-size:4;white-space:pre;word-wrap:normal}" +
    ".cce-layer{pointer-events:none;overflow:visible;will-change:transform;counter-reset:ln;min-width:100%}" +
    ".cce-layer div{height:1.5em;padding-left:var(--gw)}.cce-layer div:before{counter-increment:ln;content:counter(ln);position:absolute;left:0;width:calc(var(--gw) - 12px);text-align:right;color:#484f58}" +
    ".cce-layer div.cur{background:rgba(110,118,129,.14)}.cce-layer div.cur:before{color:#c9d1d9}" +
    ".cce-ta{width:100%;height:100%;padding-left:var(--gw);background:transparent;color:transparent;caret-color:#e6edf3;resize:none;outline:none;overflow:auto;box-sizing:border-box}" +
    ".cce-ta::selection{background:rgba(56,139,253,.4);color:transparent}" +
    ".cce-m{color:transparent}.cce-m mark{color:transparent;background:rgba(210,153,34,.35);border-radius:2px}.cce-m mark.cm{background:rgba(240,136,62,.65)}" +
    ".cce .k{color:#ff7b72}.cce .s{color:#a5d6ff}.cce .n{color:#79c0ff}.cce .c{color:#8b949e;font-style:italic}.cce .p{color:#7ee787}.cce .t{color:#d2a8ff}.cce .o{color:#ff7b72}.cce .h{color:#ffa657}";

  function injectCss() {
    if (document.getElementById("cce-css")) return;
    var st = document.createElement("style");
    st.id = "cce-css";
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  function kw(words) { return new RegExp("\\b(?:" + words.replace(/ /g, "|") + ")\\b", "y"); }
  var NUM = /-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/y;
  var DQ = /"(?:[^"\\\n]|\\.)*"?/y, SQ = /'(?:[^'\\\n]|\\.)*'?/y;

  var LANGS = {
    json: [["p", /"(?:[^"\\\n]|\\.)*"(?=\s*:)/y], ["s", DQ], ["n", NUM], ["k", kw("true false null")]],
    yaml: [["c", /#[^\n]*/y], ["p", /^[ \t-]*[\w.\/"'-]+(?=\s*:(?:\s|$))/my], ["s", DQ], ["s", SQ], ["n", NUM], ["k", kw("true false null yes no on off")]],
    props: [["c", /^[ \t]*[#!][^\n]*/my], ["p", /^[ \t]*[^\s=:#!][^=:\n]*(?=[=:])/my], ["o", /[=:]/y], ["n", NUM], ["k", kw("true false")]],
    toml: [["c", /#[^\n]*/y], ["h", /^\s*\[[^\]\n]*\]/my], ["p", /^[ \t]*[\w.-]+(?=\s*=)/my], ["s", DQ], ["s", SQ], ["n", NUM], ["k", kw("true false")]],
    xml: [["c", /<!--[\s\S]*?(?:-->|$)/y], ["t", /<\/?[\w:.-]+/y], ["p", /[\w:.-]+(?=\s*=)/y], ["s", DQ], ["s", SQ], ["t", /\/?>/y]],
    md: [["h", /^#{1,6}[^\n]*/my], ["s", /`[^`\n]*`/y], ["k", /\*\*[^*\n]+\*\*/y], ["t", /\[[^\]\n]*\]\([^)\n]*\)/y]],
    js: [["c", /\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$)/y], ["s", DQ], ["s", SQ], ["s", /`(?:[^`\\]|\\.)*`?/y], ["n", NUM],
         ["k", kw("var let const function return if else for while do switch case break continue new this class extends import export from async await try catch finally throw typeof in of null undefined true false")]],
    py: [["c", /#[^\n]*/y], ["s", /"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)/y], ["s", DQ], ["s", SQ], ["n", NUM],
         ["k", kw("def class return if elif else for while import from as with try except finally raise pass break continue lambda yield in is not and or None True False self")]],
    sh: [["c", /#[^\n]*/y], ["s", DQ], ["s", SQ], ["p", /\$\{?\w+\}?/y], ["n", NUM],
         ["k", kw("if then else elif fi for while do done case esac function return exit echo export set cd java rem")]]
  };
  var BY_EXT = { json: "json", mcmeta: "json", yml: "yaml", yaml: "yaml", properties: "props", cfg: "props", conf: "props", ini: "props",
    env: "props", secret: "props", lang: "props", toml: "toml", xml: "xml", md: "md", js: "js", py: "py", sh: "sh", bat: "sh" };

  function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  // Returns HTML with one <div> per line; spans are split at newlines.
  function highlight(text, rules, cur) {
    var out = [], line = "", i = 0, n = text.length, plainStart = 0;
    function pushText(s, cls) {
      var parts = s.split("\n");
      for (var k = 0; k < parts.length; k++) {
        if (k) { out.push(line); line = ""; }
        if (parts[k]) line += cls ? '<span class="' + cls + '">' + esc(parts[k]) + "</span>" : esc(parts[k]);
      }
    }
    if (!rules) { pushText(text, ""); out.push(line); return wrap(out, cur); }
    while (i < n) {
      var hit = null, r, m;
      for (var j = 0; j < rules.length; j++) {
        r = rules[j][1];
        r.lastIndex = i;
        m = r.exec(text);
        if (m && m.index === i && m[0].length) { hit = [rules[j][0], m[0]]; break; }
      }
      if (hit) {
        if (i > plainStart) pushText(text.slice(plainStart, i), "");
        pushText(hit[1], hit[0]);
        i += hit[1].length;
        plainStart = i;
      } else {
        // skip to the next plausible token start for speed
        var c = text.charCodeAt(i);
        i++;
        if ((c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 95) {
          while (i < n) { c = text.charCodeAt(i); if ((c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 95) i++; else break; }
        }
      }
    }
    if (n > plainStart) pushText(text.slice(plainStart), "");
    out.push(line);
    return wrap(out, cur);
  }
  function wrap(lines, cur) {
    var h = "";
    for (var i = 0; i < lines.length; i++) h += "<div" + (i === cur ? ' class="cur"' : "") + ">" + (lines[i] || "&#8203;") + "</div>";
    return h;
  }

  function create(parent, opts) {
    injectCss();
    var ext = ((opts.filename || "").split(".").pop() || "").toLowerCase();
    var rules = LANGS[BY_EXT[ext]] || null;
    var root = document.createElement("div");
    root.className = "cce";
    root.innerHTML =
      '<div class="cce-bar"><input class="f" placeholder="Find" spellcheck="false"><input class="r" placeholder="Replace" spellcheck="false">' +
      '<button class="prev" title="Previous (Shift+Enter)">&#8593;</button><button class="next" title="Next (Enter)">&#8595;</button>' +
      '<button class="case" title="Match case">Aa</button><button class="rep">Replace</button><button class="all">All</button>' +
      '<span class="cce-n"></span><button class="x" title="Close (Esc)">&#10005;</button></div>' +
      '<div class="cce-body"><pre class="cce-layer cce-m"></pre><pre class="cce-layer hl"></pre>' +
      '<textarea class="cce-ta" spellcheck="false" wrap="off" autocapitalize="off" autocomplete="off"></textarea></div>';
    parent.appendChild(root);
    var bar = root.querySelector(".cce-bar"), fi = bar.querySelector(".f"), ri = bar.querySelector(".r"), cnt = bar.querySelector(".cce-n"),
        caseBtn = bar.querySelector(".case"), body = root.querySelector(".cce-body"), mirror = root.querySelector(".cce-m"),
        hl = root.querySelector(".hl"), ta = root.querySelector(".cce-ta");
    var matches = [], mi = -1, matchCase = false, raf = 0, curLine = -1;
    ta.value = opts.text || "";

    function gutter() {
      var lines = ta.value.split("\n").length;
      root.style.setProperty("--gw", (Math.max(String(lines).length, 3) + 2) * 8 + "px");
    }
    function lineOf(pos) { var c = 0, v = ta.value; for (var i = v.indexOf("\n"); i !== -1 && i < pos; i = v.indexOf("\n", i + 1)) c++; return c; }
    function render() {
      raf = 0;
      gutter();
      var v = ta.value;
      curLine = lineOf(ta.selectionStart);
      hl.innerHTML = highlight(v, v.length > MAX_HL ? null : rules, curLine);
      drawMarks();
      sync();
    }
    function schedule() { if (!raf) raf = requestAnimationFrame(render); }
    function sync() {
      var t = "translate(" + (-ta.scrollLeft) + "px," + (-ta.scrollTop) + "px)";
      mirror.style.transform = hl.style.transform = t;
    }
    function drawMarks() {
      if (!bar.classList.contains("on") || !fi.value) { mirror.innerHTML = ""; matches = []; cnt.textContent = ""; return; }
      var v = ta.value, q = fi.value, hay = matchCase ? v : v.toLowerCase(), nd = matchCase ? q : q.toLowerCase(), out = "", last = 0, p = 0;
      matches = [];
      while ((p = hay.indexOf(nd, p)) !== -1 && matches.length < 5000) { matches.push(p); p += nd.length; }
      if (mi >= matches.length) mi = matches.length - 1;
      for (var k = 0; k < matches.length; k++) {
        out += esc(v.slice(last, matches[k])) + "<mark" + (k === mi ? ' class="cm"' : "") + ">" + esc(v.substr(matches[k], q.length)) + "</mark>";
        last = matches[k] + q.length;
      }
      out += esc(v.slice(last));
      mirror.innerHTML = out.split("\n").map(function (l) { return "<div>" + (l || "&#8203;") + "</div>"; }).join("");
      cnt.textContent = matches.length ? (mi + 1) + "/" + matches.length : "0";
    }
    function go(dir) {
      drawMarks();
      if (!matches.length) return;
      var from = ta.selectionStart, idx = -1;
      if (dir > 0) { for (var i = 0; i < matches.length; i++) if (matches[i] >= from + (mi >= 0 && ta.selectionStart === matches[mi] ? 1 : 0)) { idx = i; break; } if (idx < 0) idx = 0; }
      else { for (var j = matches.length - 1; j >= 0; j--) if (matches[j] < from) { idx = j; break; } if (idx < 0) idx = matches.length - 1; }
      mi = idx;
      ta.setSelectionRange(matches[idx], matches[idx] + fi.value.length);
      reveal(matches[idx]);
      drawMarks();
    }
    function reveal(pos) {   // scroll the textarea so the position is visible
      var lh = 19.5, line = lineOf(pos), top = line * lh;
      if (top < ta.scrollTop + 10) ta.scrollTop = Math.max(0, top - 40);
      else if (top > ta.scrollTop + ta.clientHeight - 50) ta.scrollTop = top - ta.clientHeight / 2;
      sync();
    }
    function insert(text) {   // keeps the native undo stack where the browser allows it
      if (!document.execCommand || !document.execCommand("insertText", false, text)) {
        ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
        ta.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    function replaceCurrent() {
      var s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e), q = fi.value;
      if (q && (matchCase ? sel === q : sel.toLowerCase() === q.toLowerCase())) { ta.focus(); insert(ri.value); }
      go(1);
    }
    function replaceAll() {
      var q = fi.value;
      if (!q) return;
      drawMarks();
      if (!matches.length) return;
      var v = ta.value, out = "", last = 0;
      matches.forEach(function (p) { out += v.slice(last, p) + ri.value; last = p + q.length; });
      out += v.slice(last);
      ta.focus(); ta.select(); insert(out);
      mi = -1; drawMarks();
    }
    function openBar(withReplace) {
      bar.classList.add("on");
      var sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
      if (sel && sel.indexOf("\n") === -1) fi.value = sel;
      (withReplace ? ri : fi).focus();
      (withReplace ? ri : fi).select();
      mi = -1; drawMarks();
    }
    function closeBar() { bar.classList.remove("on"); drawMarks(); ta.focus(); }

    ta.addEventListener("input", function () { if (opts.onChange) opts.onChange(); if (bar.classList.contains("on")) mi = -1; schedule(); });
    ta.addEventListener("scroll", sync);
    ["keyup", "click", "focus"].forEach(function (ev) { ta.addEventListener(ev, function () { var l = lineOf(ta.selectionStart); if (l !== curLine) schedule(); }); });
    ta.addEventListener("keydown", function (ev) {
      var mod = ev.ctrlKey || ev.metaKey, k = ev.key;
      if (mod && !ev.shiftKey && !ev.altKey && (k === "s" || k === "S")) { ev.preventDefault(); if (opts.onSave) opts.onSave(); return; }
      if (mod && (k === "f" || k === "F")) { ev.preventDefault(); openBar(false); return; }
      if (mod && (k === "h" || k === "H")) { ev.preventDefault(); openBar(true); return; }
      if (k === "F3") { ev.preventDefault(); go(ev.shiftKey ? -1 : 1); return; }
      if (k === "Tab" && !mod && !ev.altKey) {
        ev.preventDefault();
        var v = ta.value, s = ta.selectionStart, e = ta.selectionEnd;
        if (s === e && !ev.shiftKey) { insert("  "); return; }
        var ls = v.lastIndexOf("\n", s - 1) + 1, block = v.slice(ls, e), res;
        if (ev.shiftKey) res = block.replace(/^(?: {1,2}|\t)/gm, ""); else res = block.replace(/^/gm, "  ");
        ta.setSelectionRange(ls, e); insert(res);
        ta.setSelectionRange(ls, ls + res.length);
        return;
      }
      if (k === "Enter" && !mod && !ev.shiftKey && !ev.altKey && ta.selectionStart === ta.selectionEnd) {
        var v2 = ta.value, p = ta.selectionStart, st = v2.lastIndexOf("\n", p - 1) + 1, ind = /^[ \t]*/.exec(v2.slice(st, p))[0];
        if (ind) { ev.preventDefault(); insert("\n" + ind); }
      }
    });
    bar.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); closeBar(); }
      else if (ev.key === "Enter") { ev.preventDefault(); if (ev.target === ri) replaceCurrent(); else go(ev.shiftKey ? -1 : 1); }
      else if ((ev.ctrlKey || ev.metaKey) && (ev.key === "s" || ev.key === "S")) { ev.preventDefault(); if (opts.onSave) opts.onSave(); }
    });
    fi.addEventListener("input", function () { mi = -1; drawMarks(); });
    bar.querySelector(".next").onclick = function () { go(1); };
    bar.querySelector(".prev").onclick = function () { go(-1); };
    bar.querySelector(".rep").onclick = replaceCurrent;
    bar.querySelector(".all").onclick = replaceAll;
    bar.querySelector(".x").onclick = closeBar;
    caseBtn.onclick = function () { matchCase = !matchCase; caseBtn.classList.toggle("on", matchCase); mi = -1; drawMarks(); };

    render();
    return {
      getValue: function () { return ta.value; },
      focus: function () { ta.focus(); },
      destroy: function () { if (raf) cancelAnimationFrame(raf); root.remove(); }
    };
  }

  window.CCEditor = { create: create };
})();
