# Third-party code

`editor.js` is a minified bundle of [CodeMirror 6](https://codemirror.net/) (`@codemirror/*`, MIT, Copyright Marijn Haverbeke and others)
including the One Dark theme. `editor-src.js` is the entry file it is built from:

    npm i codemirror @codemirror/{state,view,language,commands,search,autocomplete,lint,lang-json,lang-yaml,lang-xml,lang-markdown,lang-javascript,lang-python,legacy-modes,theme-one-dark} esbuild
    npx esbuild editor-src.js --bundle --minify --format=iife --target=es2020 --legal-comments=none --outfile=editor.js
