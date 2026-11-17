/* -- AI GENERATED CODE (deepseek-v4.1-flash) :: (2026-10-25) harness:openlumara
   editor module: per-view helpers for its tool displays, vendored from the
   coder module (modules/coder/webui/assets/js/custom_tool_views/*.js) so the
   editor views work while coder is disabled. the shared streaming/render
   machinery these build on (streamMemo, codeLinesHtml, highlightedCode,
   escapeHtml, hljs) is still core - webui renderers/custom_tool_views/shared.js.

   every global here is editor-PREFIXED on purpose: classic scripts share one
   top-level lexical scope, so re-using coder's generic names (langForPath,
   grepGroups, diffHtml, const READ_LANG_BY_EXT, ...) would throw
   "Identifier already declared" in the console as soon as BOTH modules are
   enabled, killing every script loaded after them. */

// ---- language detection (langForPath) ---------------------------------------
const EDITOR_LANG_BY_EXT = {
    py: 'python', js: 'javascript', mjs: 'javascript', ts: 'typescript',
    json: 'json', html: 'html', xml: 'xml', css: 'css', scss: 'scss',
    yml: 'yaml', yaml: 'yaml', md: 'markdown', rs: 'rust', go: 'go',
    c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', sh: 'bash', bash: 'bash',
    toml: 'ini', ini: 'ini', sql: 'sql', java: 'java', rb: 'ruby',
    php: 'php', txt: 'plaintext',
};

function editorLangForPath(path) {
    const ext = (path || '').split('.').pop()?.toLowerCase();
    return EDITOR_LANG_BY_EXT[ext];
}

// ---- grep: grouped matches (grepGroups / grepRows) --------------------------
// editor_grep returns { matches: [{ file, line_num, line, char_start,
// context_before?, context_after? }], note? } (or a bare list). group by the
// per-match file, falling back to the searched sub_path.
function editorGrepGroups(response, args) {
    if (!response || response.status !== 'success') return [];
    const c = response.content;
    const items = Array.isArray(c) ? c : (Array.isArray(c?.matches) ? c.matches : []);
    const fallbackFile = args?.sub_path ?? 'matches';
    const groups = new Map();
    for (const m of items) {
        const file = m.file ?? fallbackFile;
        if (!groups.has(file)) groups.set(file, []);
        groups.get(file).push(m);
    }
    return [...groups.entries()].map(([file, matches]) => ({ file, matches }));
}

// one file's matches -> display rows: dimmed context_before, the match line,
// dimmed context_after. line numbers only on the match itself.
function editorGrepRows(matches) {
    const rows = [];
    for (const m of matches ?? []) {
        for (const line of m.context_before ?? [])
            rows.push({ num: '', text: line, kind: 'ctx' });
        rows.push({ num: m.line_num, text: m.line, kind: 'hit' });
        for (const line of m.context_after ?? [])
            rows.push({ num: '', text: line, kind: 'ctx' });
    }
    return rows;
}

// ---- glob: file list (globFiles) --------------------------------------------
// editor_glob returns a list of paths, or { results, truncated, total, note }
// when the result cap hit.
function editorGlobFiles(response) {
    if (!response || response.status !== 'success') return [];
    const c = response.content;
    const items = Array.isArray(c) ? c : (Array.isArray(c?.results) ? c.results : []);
    return items.filter(f => typeof f === 'string');
}

// ---- diff: line diff + unified/side-by-side markup --------------------------
// true line diff (LCS). common lines are context, changed ones -/+; long
// unchanged stretches collapse to a '..' marker. works on partial (streaming)
// inputs - it just re-diffs as they grow.
function editorDiffLines(aText, bText) {
    const a = (aText || '').replace(/\n$/, '').split('\n');
    const b = (bText || '').replace(/\n$/, '').split('\n');
    if (a.length === 1 && a[0] === '') a.pop();
    if (b.length === 1 && b[0] === '') b.pop();

    const n = a.length, m = b.length;
    if (n * m > 250000) return [...a.map(t => ['-', t]), ...b.map(t => ['+', t])];

    const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--)
        for (let j = m - 1; j >= 0; j--)
            dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);

    const out = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
        if (a[i] === b[j]) { out.push([' ', a[i]]); i++; j++; }
        else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push(['-', a[i]]); i++; }
        else { out.push(['+', b[j]]); j++; }
    }
    while (i < n) out.push(['-', a[i++]]);
    while (j < m) out.push(['+', b[j++]]);
    return out;
}

function editorCollapseContext(rows, keep) {
    const out = [];
    let run = [];
    const flush = () => {
        if (run.length > keep * 2 + 1) {
            out.push(...run.slice(0, keep));
            out.push(['..', `${run.length - keep * 2} unchanged`]);
            out.push(...run.slice(-keep));
        } else out.push(...run);
        run = [];
    };
    for (const row of rows) {
        if (row[0] === ' ') run.push(row);
        else { flush(); out.push(row); }
    }
    flush();
    return out;
}

// per-line highlighting for diff rows (known languages only - the diff shows
// excerpts, so whole-file highlighting is impossible and auto-detect would
// guess wildly). tiny cache: diffs re-run on every streamed chunk.
const _editorDiffLineCache = new Map();
function editorHighlightDiffLine(text, lang) {
    if (!text) return '';
    if (!lang || typeof hljs === 'undefined' || !hljs.getLanguage(lang))
        return escapeHtml(text);
    const key = lang + '\u0000' + text;
    let html = _editorDiffLineCache.get(key);
    if (html === undefined) {
        try {
            html = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
        } catch {
            html = escapeHtml(text);
        }
        if (_editorDiffLineCache.size > 2000) _editorDiffLineCache.clear();
        _editorDiffLineCache.set(key, html);
    }
    return html;
}

// unified diff markup for the view box. an html-string view: the box paints
// via x-fade-html, and token fade needs whole-content repaints (x-for rows
// can't survive that), so the rows are generated here. all text goes through
// escapeHtml or hljs (escaped output), so the markup is safe.
function editorDiffHtml(original, replacement, lang, cacheKey) {
    return streamMemo('editor-edit:' + cacheKey, [original, replacement, lang],
        () => _editorDiffHtmlNow(original, replacement, lang));
}

function _editorDiffHtmlNow(original, replacement, lang) {
    const clsFor = sign =>
        sign === '..' ? 'diff-gap' : sign === '-' ? 'diff-del' : sign === '+' ? 'diff-add' : 'diff-ctx';
    let rows;
    if (!original && !replacement) return '';
    if (!original || !replacement) {
        const solo = (original || replacement || '').replace(/\n$/, '').split('\n');
        rows = solo.map(t => [' ', t]);
    } else {
        rows = editorCollapseContext(editorDiffLines(original, replacement), 2);
    }
    return rows.map(([sign, text]) => {
        const gutter = sign === '..' ? '..' : sign;
        const body = sign === '..' ? escapeHtml(text) : editorHighlightDiffLine(text, lang);
        return `<div class="diff-line ${clsFor(sign)}">` +
            `<span class="diff-gutter">${gutter}</span>` +
            `<span class="diff-text">${body}</span></div>`;
    }).join('');
}

// pairs linear diff rows into split-view rows: consecutive -/+ runs are
// matched line-by-line (shorter side gets a filler cell); context and gap
// rows span the full width like github's split view.
function editorSideBySideRows(rows) {
    const out = [];
    let dels = [], adds = [];
    const flush = () => {
        const n = Math.max(dels.length, adds.length);
        for (let k = 0; k < n; k++)
            out.push({
                left: k < dels.length ? dels[k] : null,
                right: k < adds.length ? adds[k] : null,
            });
        dels = []; adds = [];
    };
    for (const [sign, text] of rows) {
        if (sign === '-') dels.push(text);
        else if (sign === '+') adds.push(text);
        else { flush(); out.push({ full: text, kind: sign === '..' ? 'gap' : 'ctx' }); }
    }
    flush();
    return out;
}

// side-by-side markup. which layout is VISIBLE is pure css: .diff-container is
// a size container and a container query swaps them at a min card width
// (custom_tool_views/file_edit.css), so it follows the chat width settings.
// both layouts render; the per-line highlight cache is shared between them.
function editorDiffSideHtml(original, replacement, lang, cacheKey) {
    return streamMemo('editor-edit2:' + cacheKey, [original, replacement, lang],
        () => _editorDiffSideHtmlNow(original, replacement, lang));
}

function _editorDiffSideHtmlNow(original, replacement, lang) {
    if (!original && !replacement) return '';
    let rows;
    const cell = (gutter, text, cls) =>
        `<div class="diff2-cell ${cls}"><span class="diff2-gutter">${gutter}</span>` +
        `<span class="diff2-text">${editorHighlightDiffLine(text, lang) || '\u200b'}</span></div>`;
    if (!original || !replacement) {
        const solo = (original || replacement || '').replace(/\n$/, '').split('\n')
            .map(t => '<div class="diff2-row diff2-changed">' +
                (original
                    ? cell('', t, '') + '<div class="diff2-cell diff2-empty"></div>'
                    : '<div class="diff2-cell diff2-empty"></div>' + cell('', t, '')) +
                '</div>').join('');
        return solo;
    }
    rows = editorCollapseContext(editorDiffLines(original, replacement), 2);
    return editorSideBySideRows(rows).map(p => {
        if (p.full !== undefined) {
            if (p.kind === 'ctx') {
                return '<div class="diff2-row diff2-ctx">' +
                    cell('', p.full, '') +
                    '<div class="diff2-cell diff2-empty"></div></div>';
            }
            return `<div class="diff2-row diff2-gap">` +
                `<div class="diff2-cell diff2-full"><span class="diff2-gutter">..</span>` +
                `<span class="diff2-text">${escapeHtml(p.full)}</span></div></div>`;
        }
        return '<div class="diff2-row diff2-changed">' +
            (p.left === null ? '<div class="diff2-cell diff2-empty"></div>' : cell('-', p.left, 'diff2-del')) +
            (p.right === null ? '<div class="diff2-cell diff2-empty"></div>' : cell('+', p.right, 'diff2-add')) +
            '</div>';
    }).join('');
}


// block card (replace_block / insert_block): the new block, highlighted but
// WITHOUT a line gutter - a block's own line 1 is not the file's line 12, so
// numbering it would mislead. memoized/throttled like codeLinesHtml, or the
// highlight re-runs for every streamed chunk.
function editorBlockHtml(code, lang, cacheKey) {
    if (!code) return '';
    // the highlighted html ONLY: the scroll box (and x-auto-scroll) lives on the
    // wrapper in the template. a <pre> here would own the overflow instead, and
    // x-auto-scroll would sit on a non-scrolling parent - it then never follows
    // the stream, so the card stays pinned to the first lines.
    return streamMemo('block:' + cacheKey, [code, lang], () => highlightedCode(code, lang));
}
