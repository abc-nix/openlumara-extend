"""htmlmd - convert HTML tables inside (VLM-produced) markdown to pipe tables.

stdlib-only. Handles: escaped '&lt;table&gt;' (xberg escape_markdown),
thead/tbody, th/td, colspan/rowspan (expanded with empty cells - pipe
tables cannot merge), <br>, nested tags flattened, | in cells escaped.
mode: "convert" (default) or "keep" (no-op passthrough).
"""
import re
from html.parser import HTMLParser


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cur = [], None
        self.pending = []          # cell fragments
        self.cell_attrs = {}
        self.depth = 0             # nested <table> depth: 1 = the one we parse
        self.rowspan_pending = {}  # col_index -> (remaining, text) filled on row close

    def _new_row(self):
        row = []
        # materialize pending rowspans
        for col in sorted(self.rowspan_pending):
            rem, txt = self.rowspan_pending[col]
            while len(row) < col:
                row.append("")
            if len(row) == col:
                row.append(txt)
                self.rowspan_pending[col] = (rem - 1, txt)
                if rem - 1 <= 0:
                    del self.rowspan_pending[col]
        return row

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
        elif tag == "tr" and self.depth == 1:
            self.cur = self._new_row()
        elif tag in ("td", "th") and self.depth == 1:
            self.cell_attrs = dict(attrs)
            self.pending = []
        elif tag == "br" and self.cur is not None:
            self.pending.append(" ")
        elif self.depth == 1 and tag == "table":
            pass

    def handle_endtag(self, tag):
        if tag == "table":
            self.depth -= 1
        elif tag in ("td", "th") and self.depth == 1 and self.cur is not None:
            txt = "".join(self.pending).strip()
            cs = int(self.cell_attrs.get("colspan") or 1)
            self.cur.extend([txt] + [""] * (cs - 1))
            rs = int(self.cell_attrs.get("rowspan") or 1)
            if rs > 1:
                self.rowspan_pending[len(self.cur) - 1] = (rs - 1, txt)
            self.pending = []
        elif tag == "tr" and self.depth == 1 and self.cur is not None:
            self.rows.append(self.cur)
            self.cur = None

    def handle_data(self, data):
        if self.cur is not None:
            self.pending.append(data)


def _to_md(rows):
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    def esc(c):
        return c.replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
           "| " + " | ".join("---" for _ in range(width)) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out)


def unescape_angles(text):
    """Restore `<` and `>` that xberg escaped, for office documents only.

    Word's underline arrives in the extracted markdown as `\\<u\\>Section B\\</u\\>`
    (escape_markdown protects angle brackets), which is not markup a reader can
    see: it prints as a literal backslash-tag. In a PDF those brackets are
    noise, but in an Office document they are real inline markup, and unescaping
    them yields `<u>Section B</u>`, which markdown renders as intended. Only the
    angle brackets are touched - the pipe/asterisk escapes stay escaped.
    """
    return (text or "").replace("\\<", "<").replace("\\>", ">")


_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)


def html_tables_to_md(text, mode="convert"):
    if mode == "keep" or not text:
        return text
    # xberg escaped angle brackets: \<table\> -> <table> (targeted, safe)
    probe = text.replace("\\<", "<").replace("\\>", ">")
    changed = [False]

    def repl(m):
        p = _TableParser()
        try:
            p.feed(m.group(0))
            p.close()
        except Exception:
            return m.group(0)
        if not p.rows:
            return m.group(0)
        changed[0] = True
        return _to_md(p.rows)

    converted = _TABLE_RE.sub(repl, probe)
    if changed[0]:
        return converted
    # nothing table-shaped to convert: restore original escapes untouched
    return text
