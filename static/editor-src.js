import {EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
        drawSelection, rectangularSelection, crosshairCursor, dropCursor, highlightSpecialChars} from "@codemirror/view";
import {EditorState, Compartment} from "@codemirror/state";
import {defaultKeymap, history, historyKeymap, indentWithTab} from "@codemirror/commands";
import {searchKeymap, highlightSelectionMatches} from "@codemirror/search";
import {foldGutter, foldKeymap, indentOnInput, bracketMatching, syntaxHighlighting,
        defaultHighlightStyle, StreamLanguage} from "@codemirror/language";
import {closeBrackets, closeBracketsKeymap} from "@codemirror/autocomplete";
import {oneDark} from "@codemirror/theme-one-dark";
import {json} from "@codemirror/lang-json";
import {yaml} from "@codemirror/lang-yaml";
import {xml} from "@codemirror/lang-xml";
import {markdown} from "@codemirror/lang-markdown";
import {javascript} from "@codemirror/lang-javascript";
import {python} from "@codemirror/lang-python";
import {properties} from "@codemirror/legacy-modes/mode/properties";
import {shell} from "@codemirror/legacy-modes/mode/shell";
import {toml} from "@codemirror/legacy-modes/mode/toml";

const legacy = m => StreamLanguage.define(m);

function languageFor(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  switch (ext) {
    case "json": case "mcmeta": return json();
    case "yml": case "yaml": return yaml();
    case "xml": return xml();
    case "md": return markdown();
    case "js": return javascript();
    case "py": return python();
    case "sh": case "bat": return legacy(shell);
    case "toml": return legacy(toml);
    case "properties": case "cfg": case "conf": case "ini": case "env": case "secret": case "lang": return legacy(properties);
    default: return [];
  }
}

// create(parent, {text, filename, onChange, onSave}) -> {getValue, focus, destroy}
function create(parent, opts) {
  const lang = new Compartment();
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: opts.text || "",
      extensions: [
        lineNumbers(), highlightActiveLineGutter(), highlightSpecialChars(), history(), foldGutter(),
        drawSelection(), dropCursor(), EditorState.allowMultipleSelections.of(true), indentOnInput(),
        syntaxHighlighting(defaultHighlightStyle, {fallback: true}), bracketMatching(), closeBrackets(),
        rectangularSelection(), crosshairCursor(), highlightActiveLine(), highlightSelectionMatches(),
        keymap.of([
          {key: "Mod-s", preventDefault: true, run: () => { if (opts.onSave) opts.onSave(); return true; }},
          ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap, ...foldKeymap, indentWithTab,
        ]),
        oneDark,
        lang.of(languageFor(opts.filename || "")),
        EditorView.updateListener.of(u => { if (u.docChanged && opts.onChange) opts.onChange(); }),
        EditorView.theme({"&": {height: "100%"}, ".cm-scroller": {overflow: "auto", fontFamily: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace", fontSize: "13px"}}),
      ],
    }),
  });
  return {
    getValue: () => view.state.doc.toString(),
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}

window.CCEditor = {create};
