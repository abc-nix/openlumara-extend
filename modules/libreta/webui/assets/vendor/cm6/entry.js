// Build entry for libreta's CodeMirror 6 bundle (`cm6.min.js`).
//
// WHY THIS FILE EXISTS: the bundle was built once in 2026-11-02 and the entry was
// never committed, so the recipe in README.md could not reproduce the artifact.
// This file was reconstructed on 2026-11-13 from the shipped bundle itself: the
// export table and every piece of custom code (highlight style, frontmatter
// config, language map, codeLanguages) are still visible inside the minified
// artifact. Keep this in sync with what the module actually consumes: grep for
// `CM.` and `LB_CM6.` in ../../js/libreta.js.
//
// Build (pinned versions in README.md):
//   npm i esbuild@0.25 @codemirror/state @codemirror/view@6.43.15 \
//     @codemirror/language @codemirror/commands @codemirror/search@6.7.2 \
//     @codemirror/lang-markdown@6.5.2 @codemirror/lang-javascript \
//     @codemirror/lang-python @codemirror/lang-json @codemirror/lang-yaml \
//     @codemirror/lang-css @codemirror/lang-html @codemirror/lang-xml \
//     @lezer/common @lezer/highlight @lezer/markdown \
//     @plannotator/atomic-editor@0.9.0 react react-dom
//   npx esbuild entry.js --bundle --format=iife --global-name=LibretaCM \
//     --minify --external:react --external:react-dom --target=es2020 \
//     --outfile=cm6.min.js

import { Compartment, EditorState, Prec } from "@codemirror/state";
import {
    EditorView,
    keymap,
    lineNumbers,
    highlightActiveLine,
    placeholder,
} from "@codemirror/view";
import {
    defaultKeymap,
    history,
    historyKeymap,
    indentWithTab,
    toggleComment,
} from "@codemirror/commands";
import {
    SearchQuery,
    RegExpCursor,
    SearchCursor,
    search,
    searchKeymap,
    openSearchPanel,
    closeSearchPanel,
    searchPanelOpen,
    setSearchQuery,
    getSearchQuery,
    findNext,
    findPrevious,
    replaceNext,
    replaceAll,
    selectMatches,
    highlightSelectionMatches,
} from "@codemirror/search";
import {
    syntaxHighlighting,
    HighlightStyle,
    LanguageDescription,
    bracketMatching,
    codeFolding,
    foldGutter,
    foldKeymap,
    foldCode,
    unfoldCode,
    foldAll,
    unfoldAll,
} from "@codemirror/language";
import { tags as t } from "@lezer/highlight";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { insertNewlineContinueMarkup } from "@codemirror/lang-markdown";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { Table } from "@lezer/markdown";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { json } from "@codemirror/lang-json";
import { yaml } from "@codemirror/lang-yaml";
import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { xml } from "@codemirror/lang-xml";
import { inlinePreview, tables, imageBlocks } from "@plannotator/atomic-editor";

// ---------------------------------------------------------------------------
// frontmatter — copied VERBATIM from
// @plannotator/atomic-editor/dist/frontmatter.js (MIT).
//
// It cannot be imported from the package: atomic-editor's `exports` map only
// exposes `.`, `./code-languages` and `./styles.css`, so `dist/frontmatter.js`
// is unreachable from an external build. Keep this copy byte-identical; the
// module's own copy of the file is the reference.
// ---------------------------------------------------------------------------
const CLOSE_FENCE = /^---[ \t]*$/;
const frontmatter = {
    defineNodes: [
        { name: "Frontmatter", block: true },
        { name: "FrontmatterMark" },
    ],
    parseBlock: [
        {
            name: "Frontmatter",
            before: "HorizontalRule",
            parse(cx, line) {
                if (cx.lineStart !== 0 || line.text !== "---")
                    return false;
                const marks = [cx.elt("FrontmatterMark", 0, 3)];
                let end = 3;
                while (cx.nextLine()) {
                    end = cx.lineStart + line.text.length;
                    if (CLOSE_FENCE.test(line.text)) {
                        marks.push(cx.elt("FrontmatterMark", cx.lineStart, end));
                        cx.nextLine();
                        cx.addElement(cx.elt("Frontmatter", 0, end, marks));
                        return true;
                    }
                }
                cx.addElement(cx.elt("Frontmatter", 0, end, marks));
                return true;
            },
        },
    ],
};

// ---------------------------------------------------------------------------
// language lookup for Code mode (libreta passes the file extension)
// ---------------------------------------------------------------------------
const BY_EXTENSION = {
    js: javascript,
    mjs: javascript,
    cjs: javascript,
    jsx: () => javascript({ jsx: true }),
    ts: () => javascript({ typescript: true }),
    tsx: () => javascript({ jsx: true, typescript: true }),
    py: python,
    json,
    yml: yaml,
    yaml: yaml,
    css: css,
    scss: css,
    html: html,
    htm: html,
    xml: xml,
    svg: xml,
};

function languageForExt(ext) {
    const make = BY_EXTENSION[String(ext || "").toLowerCase()];
    try {
        return make ? make() : null;
    } catch {
        return null;
    }
}

// fenced code blocks inside notes: on-demand language loading by extension
const codeLanguages = [
    LanguageDescription.of({
        name: "JavaScript",
        alias: ["js", "jsx", "ts", "typescript"],
        extensions: ["js", "jsx", "ts", "mjs", "cjs"],
        load: async () => javascript({ jsx: true, typescript: true }),
    }),
    LanguageDescription.of({
        name: "Python",
        alias: ["py"],
        extensions: ["py"],
        load: async () => python(),
    }),
    LanguageDescription.of({
        name: "JSON",
        alias: ["jsonc"],
        extensions: ["json"],
        load: async () => json(),
    }),
    LanguageDescription.of({
        name: "YAML",
        alias: ["yml"],
        extensions: ["yml", "yaml"],
        load: async () => yaml(),
    }),
    LanguageDescription.of({
        name: "CSS",
        extensions: ["css", "scss"],
        load: async () => css(),
    }),
    LanguageDescription.of({
        name: "HTML",
        extensions: ["html", "htm"],
        load: async () => html(),
    }),
    LanguageDescription.of({
        name: "XML",
        extensions: ["xml", "svg"],
        load: async () => xml(),
    }),
];

// Notes mode grammar: GFM (via markdownLanguage) + our frontmatter block.
function markdownSupport() {
    return markdown({
        base: markdownLanguage,
        extensions: [Table, frontmatter],
        codeLanguages,
    });
}

// ---------------------------------------------------------------------------
// token colours — CSS variables with the old One Dark values as fallbacks.
// libreta.css defines the variables; see the "Syntax palette" block there.
// ---------------------------------------------------------------------------
const libretaHighlight = syntaxHighlighting(
    HighlightStyle.define([
        { tag: t.keyword, color: "var(--lb-hl-keyword, #c792ea)" },
        { tag: [t.string, t.special(t.string)], color: "var(--lb-hl-string, #c3e88d)" },
        { tag: [t.number, t.bool, t.null], color: "var(--lb-hl-number, #f78c6c)" },
        { tag: t.comment, color: "var(--lb-hl-comment, #6a7a82)", fontStyle: "italic" },
        { tag: [t.typeName, t.className], color: "var(--lb-hl-type, #ffcb6b)" },
        {
            tag: [t.function(t.variableName), t.function(t.propertyName)],
            color: "var(--lb-hl-function, #82aaff)",
        },
        { tag: t.propertyName, color: "var(--lb-hl-property, #82aaff)" },
        { tag: t.variableName, color: "var(--lb-hl-variable, inherit)" },
        { tag: [t.operator, t.punctuation], color: "var(--lb-hl-operator, #89ddff)" },
        { tag: t.tagName, color: "var(--lb-hl-tag, #f07178)" },
        { tag: t.attributeName, color: "var(--lb-hl-type, #ffcb6b)" },
        { tag: t.heading, fontWeight: "600" },
        { tag: t.strong, fontWeight: "600" },
        { tag: t.emphasis, fontStyle: "italic" },
        { tag: t.strikethrough, textDecoration: "line-through" },
        { tag: t.link, color: "var(--accent, #7c3aed)", textDecoration: "underline" },
        {
            tag: t.monospace,
            fontFamily: "var(--lb-mono-font, ui-monospace, SFMono-Regular, Menlo, monospace)",
        },
        { tag: t.invalid, color: "var(--lb-hl-invalid, #ff5370)" },
    ])
);

// ---------------------------------------------------------------------------
// the surface the module consumes (36 names — matches the shipped artifact)
// ---------------------------------------------------------------------------
export {
    Compartment,
    EditorState,
    EditorView,
    Prec,
    RegExpCursor,
    SearchCursor,
    SearchQuery,
    Table,
    closeSearchPanel,
    codeLanguages,
    defaultKeymap,
    findNext,
    findPrevious,
    getSearchQuery,
    highlightActiveLine,
    history,
    historyKeymap,
    imageBlocks,
    indentWithTab,
    inlinePreview,
    keymap,
    languageForExt,
    libretaHighlight,
    lineNumbers,
    markdown,
    markdownSupport,
    openSearchPanel,
    placeholder,
    replaceAll,
    replaceNext,
    search,
    searchKeymap,
    searchPanelOpen,
    selectMatches,
    setSearchQuery,
    tables,

    // --- parity extras (2026-11-13) ----------------------------------------
    // None of these were in the original bundle: entry.js never imported them,
    // so esbuild tree-shook the code out. The module wires them per mode.
    bracketMatching,
    codeFolding,
    foldGutter,
    foldKeymap,
    foldCode,
    unfoldCode,
    foldAll,
    unfoldAll,
    closeBrackets,
    closeBracketsKeymap,
    toggleComment,
    insertNewlineContinueMarkup,
    highlightSelectionMatches,

    // --- theme primitives (data-driven code themes, see the theme plan) ------
    // A theme file becomes a HighlightStyle at runtime; these are the building
    // blocks. The existing libretaHighlight stays as the "theme" default.
    HighlightStyle,
    syntaxHighlighting,
    t as tags,
};
