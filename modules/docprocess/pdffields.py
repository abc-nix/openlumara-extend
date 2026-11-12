"""pdffields - field and column pairing straight from the page's geometry.

WHY THIS EXISTS
---------------
The converter hands xberg's markdown to textfix and extras as one string, and
that string has already lost the one thing a printed form carries: which value
sits beside which label. On a two-column form the extractor emits the labels as
one block and the values as another (some of them promoted to headings), so the
report reads as prose and the numbers are useless for arithmetic.

This module rebuilds the grid from pdfgeom's runs: rows by baseline, columns
from a header row or from the cell edges, then label/value/casilla. Same
contract as pdfgeom and textfix: EVERY helper returns None when it cannot
prove its claim, and the caller must then keep the existing prose output.

WHAT IT DOES NOT DO
--------------------
- no OCR: pdfgeom reads a text layer, so scanned pages get no tables here
  (xberg's OCR-element geometry is not readable from Python, see note #35)
- no box splitting: two side-by-side boxes at the same heights look exactly
  like a label column and its values, so a page that is mostly single-cell
  rows is refused rather than mis-paired (the HAP page 1 case)
"""
import bisect
import re

from . import pdfgeom as pg

BAND_GAP = 25.0         # pts: a wider gap inside a row starts a new cell
GRID_GAP = 8.0          # pts: cell split used once the column edges exist
ROW_TOL = 2.0           # pts: runs this close in y are one visual row
REGION_GAP = 30.0       # pts: a wider vertical gap starts a new block of rows
MIN_COL_GAP = 8.0       # pts: narrowest empty slot between cells that is an edge
PROSE_RUNS = 7          # a row with more runs than this is a line of prose
HEAD_MIN, HEAD_MAX = 3, 22   # cells / chars per cell of a table header row
# Raised from 0.30 to 0.60 on the HAP evidence: that page's two side-by-side
# boxes put a payee line and a payment label on the same y, so 43% of rows look
# multi-cell and the page was read as one table, pairing "MARGARET O'CONNOR"
# with "Supp ID". A real field form has most rows carrying label+value (IRPF p2
# 69%, AIB p1 73%), so a majority threshold separates them. A page that fails
# is left to the existing prose conversion instead of being mangled.
GATE_MULTI = 0.60       # share of rows carrying 2+ cells
GATE_VALUES = 0.35      # share of those rows whose last cell looks like a value
GATE_LABELS = 0.60      # ... or whose first cell is a printed "label:"
GATE_LABEL_MIN = 6      # ... and at least this many such rows

_CASILLA = re.compile(r"^\d{3,4}$")
_MONEY = re.compile(r"^-?[\d.,]+$")
_DATE = re.compile(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}$")
_PCT = re.compile(r"^-?[\d.,]+ ?%$")
# Identifiers are values too: the AIB leaflet's IBAN (IE05AIBK935069XXXX4024),
# an AEAT cadastral reference (29051A023000500000MK) and a PPS number
# (3127894G) were all dropped as "not a value" in v3, which lost the IBAN row.
# A token with a digit AND a letter and no space is an identifier, never a
# printed label (labels are words with spaces).
_CODE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9./-]{5,}$")
_VALUEISH = (_MONEY, _DATE, _CASILLA, _PCT, _CODE)


def valueish(text):
    """Does this cell read as a filled-in value rather than a label?"""
    t = (text or "").strip()
    if not t:
        return False
    if t.upper() in ("X", "XX") or t.lower() in ("yes", "no", "si", "sí", "true",
                                                  "false", "n/a", "na"):
        return True
    # (a bare upper-case word is NOT a value: HAP's header row is
    # "Sonraisc | EUR | EUR", and counting EUR as a value kept it out of the
    # header run)
    return any(r.match(t) for r in _VALUEISH)


def run_width(text, size):
    """Width of a run in points, from its character count and font size.

    pdfgeom reports where a run STARTS, never where it ends. One thing cannot
    be read without the end: on the AEAT forms the value ("X1385258Z") and the
    casilla code printed to its right ("0001") start 51 pt apart, but the code
    is only set off by the value's own extent, so their column edge sits at a
    5 pt slot that only the widths reveal. Helvetica advance widths / 1000 em.
    """
    line = max((text or "").split("\n"), key=len)
    w = 0.0
    for c in line:
        if c.isdigit():
            w += 0.556
        elif c.isspace():
            w += 0.278
        elif c.isupper():
            w += 0.667
        elif c.islower():
            w += 0.5
        elif c in "il.,:;'|![]()":
            w += 0.28
        else:
            w += 0.4
    return w * (size or 9.0)


# -------------------------
#   rows and blocks
# -------------------------
def page_rows(data, page):
    """[(y, [(x, text, size)])] per visual row, top-down, runs left-to-right."""
    # keep the run text unstripped: the leading space of " 055655" is what
    # keeps it apart from "LODGMENT" when the two are joined into one cell
    runs = [(y, x, t, s) for (p, y, x, t, s) in (pg.chunks(data) or [])
            if p == page and t.strip()]
    runs.sort(key=lambda r: (-r[0], r[1]))
    out, cur, cy = [], [], None
    for y, x, text, size in runs:
        if cy is None or abs(y - cy) <= ROW_TOL:
            cur.append((x, text, size))
            cy = y if cy is None else cy
        else:
            cur.sort()
            out.append((cy, cur))
            cur, cy = [(x, text, size)], y
    if cur:
        cur.sort()
        out.append((cy, cur))
    return [(y, glue(parts)) for y, parts in out]


_PUNCT = set(":;,.·”’!?")


def glue(parts):
    """Attach a run that is only punctuation to the run it follows.

    IRPF prints the colon after every label as its own run, at a different x
    per row (60.9 after "NIF", 116 after "Apellidos y nombre"), so left alone
    each colon invents a column and the label/value table falls apart.
    """
    out = []
    for item in parts:
        t = item[1].strip()
        if out and t and all(c in _PUNCT for c in t):
            x, prev, s = out[-1]
            out[-1] = (x, prev + item[1], s)
        else:
            out.append(item)
    return out


def blocks(rows):
    """Split rows into blocks where the vertical gap is much bigger than the
    row pitch. On these documents a gap over 30 pt marks the end of a box or
    table (AIB: 35 pt between the customer block and the fine print), while a
    table's own row pitch stays under 25 pt even when a row is empty."""
    out, cur, prev = [], [], None
    for row in rows:
        if prev is not None and prev - row[0] > REGION_GAP:
            out.append(cur)
            cur = []
        cur.append(row)
        prev = row[0]
    if cur:
        out.append(cur)
    return out


def bands(parts, gap=BAND_GAP):
    """[(x, text)] - one entry per cell of a row, cells split at > `gap` pt.

    Used with the wide 25 pt gap to read a row before any column edges exist
    (the gate, a header row, the start-based edges), because a number the
    engine splits in two ("1000" at 278 / ".00" at 296) must stay one cell.
    The grid itself is built with an 8 pt gap: there the column edges put the
    fragments back together, and 25 pt would swallow the 10 pt slot between a
    ticked box ("X" at 543) and the casilla printed beside it ("0068" at 553).
    """
    cells, cur_x = [], None
    for x, text, _size in parts:
        if cur_x is None or x - cur_x > gap:
            cells.append([x, [text]])
            cur_x = x
        else:
            cells[-1][1].append(text)
    return [(x, "".join(v)) for x, v in cells]


# -------------------------
#   column edges
# -------------------------
def is_header_row(cells):
    """>=3 short cells, none of them a value: the table's own column titles."""
    return (len(cells) >= HEAD_MIN
            and all(len(t.strip()) <= HEAD_MAX and not valueish(t) for _x, t in cells))


def header_len(block):
    """(first header row, count) for a block, or (0, 0) when it has none.

    Rows with a single cell above the table are its titles (AIB: "Personal
    Demand Deposit", "Statement of Account with ..."), so they are skipped
    before the test - but only they: the search stops at the first row that
    has two cells, so a header buried mid-page is not invented. HAP prints its
    header over three lines (English, Irish, continuation), so once found the
    header is a run of such rows, not one row.
    """
    k = 0
    while k < len(block) and len(bands(block[k][1])) < 2:
        k += 1
    n = 0
    while k + n < len(block) and is_header_row(bands(block[k + n][1])):
        n += 1
    if n and is_label_row(block, k):
        return 0, 0
    return k, n


def is_label_row(block, k):
    """A row of titles whose row below is the same width and holds the values.

    AIB prints "Branch | National Sort Code | Account Name" over
    "Carrick-On-Suir | 93-50-50 | MARGARET O CONNOR-FORNELLS": three short
    cells with no value, exactly like a table header, but it is a LABEL row
    and the two rows pair 1:1. Without this test the label row is eaten as a
    header and the values below are left with no labels to sit under.
    """
    if k + 1 >= len(block):
        return False
    here, below = bands(block[k][1]), bands(block[k + 1][1])
    if len(here) != len(below):
        return False
    return (all(not any(c.isdigit() for c in t) for _x, t in here)
            and any(any(c.isdigit() for c in t) for _x, t in below))


def bounds_from_header(parts):
    """Column edges from the header row's titles.

    An edge goes in the middle of the slot between one title's END and the next
    title's START - not between their starts. Measured difference: the AIB
    ledger's "Credit €" title starts at 313 but its numbers run to 365, so the
    midpoint of the starts (341) would file "12.52" as Balance.
    """
    cells = bands(parts)
    edges = []
    for (x0, t0), (x1, _t1) in zip(cells, cells[1:]):
        edges.append((x0 + run_width(t0, parts[0][2]) + x1) / 2.0)
    return edges


def bounds_from_cells(block):
    """Column edges from where cells start, for blocks with no header row.

    A cell that starts far from every other start is in a column of its own, so
    an edge goes in every empty slot wider than MIN_COL_GAP (a word space is
    2.5-3.3 pt, a column slot is 20 pt+). Right-aligned numbers give ragged
    starts; `merge_singletons` below puts them back in one column.
    """
    starts = sorted({c[0] for _y, parts in block for c in bands(parts)})
    if not starts:
        return []
    edges = []
    for a, b in zip(starts, starts[1:]):
        if b - a >= MIN_COL_GAP:
            edges.append((a + b) / 2.0)
    return edges


def to_columns(block, edges):
    """[[text]] per row, one entry per column ('' where the row has nothing)."""
    if not edges:
        return [[t for _x, t in bands(parts)] for _y, parts in block]
    ncol = len(edges) + 1
    grid = []
    for _y, parts in block:
        cells = [[] for _ in range(ncol)]
        for x, text in bands(parts, GRID_GAP):
            cells[bisect.bisect_left(edges, x)].append(text)
        grid.append([" ".join("".join(c).split()) for c in cells])
    return grid


def merge_singletons(grid):
    """Join neighbouring columns that never hold a cell in the same row.

    A right-aligned number column starts at a different x in every row
    ("53,46" at 526, "5.108,98" at 514), so start-based edges cut it into a
    dozen one-row columns. No row ever fills two of them, so they are one
    column; the casilla column beside it (553, in every row) is never merged
    because it does co-occur.
    """
    while True:
        ncol = len(grid[0])
        merged = False
        for i in range(ncol - 1):
            if not any(r[i].strip() and r[i + 1].strip() for r in grid):
                for r in grid:
                    r[i] = r[i] + r[i + 1]
                    del r[i + 1]
                merged = True
                break
        if not merged:
            return grid


def prune(grid):
    """Drop columns no row ever fills (a start-based edge can cut a hole)."""
    if not grid:
        return grid
    used = [any(r[i].strip() for r in grid) for i in range(len(grid[0]))]
    return [[c for c, u in zip(r, used) if u] for r in grid]


# -------------------------
#   gate
# -------------------------
def gate(data, page):
    """FORM / NOT A FORM from one page's rows.

    Both ratios are measured over the rows the pairing stage consumes: rows
    with 2+ cells, after dropping prose lines (one run per word). Measured on
    the corpus: every form page scores 0.39-1.00 on value-shaped last cells,
    the AEAT letter scores 0.00-0.23. The letter's own field panel is covered
    by the label test (its field rows are a minority among prose rows: 6/13,
    against 8/8 on the IRPF cover sheet).
    """
    rows = [r for r in page_rows(data, page) if len(r[1]) <= PROSE_RUNS]
    if not rows:
        return None, [], "no text layer"
    n = len(rows)
    multi = val = lab = 0
    for _y, parts in rows:
        cells = bands(parts)
        if len(cells) < 2:
            continue
        multi += 1
        if valueish(cells[-1][1]):
            val += 1
        if cells[0][1].rstrip().endswith(":"):
            lab += 1
    ok = (n > 0 and multi / n >= GATE_MULTI
          and (val / max(multi, 1) >= GATE_VALUES
               or (lab >= GATE_LABEL_MIN and lab / max(multi, 1) >= GATE_LABELS)))
    why = (f"rows={n} multi={multi} value-shaped={val} labels={lab} "
           f"({multi / max(n, 1):.0%}/{val / max(multi, 1):.0%})")
    return ok, rows, why


# -------------------------
#   pairing
# -------------------------
def field_row(cells):
    """A row that proves itself a field: a printed "label:" or a value right."""
    texts = [t for t in cells if t.strip()]
    if len(texts) < 2:
        return False
    return valueish(texts[-1]) or texts[0].rstrip().endswith(":")


def paircand(cells):
    """Weaker shape: two cells, the left one short (HAP "BIC | AIBKIE2DXXX").

    Not proof on its own - it is also what a two-column line of prose looks
    like - so it is tried last, after the label/value pairing had its chance.
    """
    texts = [t for t in cells if t.strip()]
    return len(texts) == 2 and len(texts[0].strip()) <= 40


def field_block(block, grid, nhead):
    """Is this block of rows a form/table, or letterhead and prose?

    Three ways to qualify, all measured: a header row (a table declares
    itself); a majority of field rows; or a majority of 2+ cell rows whose
    left cell is short. The last one is what admits HAP's payment-account box,
    where no cell is value-shaped ("BIC | AIBKIE2DXXX") but every row is a
    label/value pair.
    """
    if nhead:
        return True
    strict = sum(1 for r in grid if field_row(r))
    paired = sum(1 for r in grid if sum(1 for c in r if c.strip()) >= 2)
    left = sorted(len(r[0].strip()) for r in grid if r[0].strip())
    if strict / max(len(grid), 1) >= 0.5:
        return True
    return (paired / max(len(grid), 1) >= 0.35 and bool(left)
            and left[len(left) // 2] <= 40)


_FURNITURE = ("http", "www.", ".gob.", "sede.", "página", "pagina", "page ",
              ".indd", "@", ".ie", ".es")


def _is_heading(text):
    """A single-cell row that names the group of fields beneath it.

    IRPF prints "Inmueble 1" / "Cónyuge" as standalone lines between fields.
    They are not page furniture (which is dropped) and not values: dropping them
    made the repeated rows ("Referencia catastral", "Porcentaje de propiedad")
    indistinguishable between properties. The space test keeps identifiers out
    (AIB's bare "70738A101" is a code, not a heading).
    """
    t = (text or "").strip()
    if not t or len(t) > 60 or " " not in t:
        return False
    if not any(c.isalpha() for c in t):
        return False
    low = t.lower()
    return not any(f in low for f in _FURNITURE)


_FURNITURE_ROWS = ("sede.", "www.", "http", ".gob", ".ie", "página", "pagina",
                   "teléfono:", "telefono:", ".indd")
_FURNITURE_FIRST = ("agencia tributaria", "modelo", "ejercicio 20",
                    "impuesto sobre la renta", "page ", "página ")


def _furniture_row(cells):
    """A page-header/footer line that some row-based shape makes look like data.

    IRPF's header box prints "sede.agenciatributaria.gob.es | Ejercicio 2025 |
    100", and 100 is value-shaped, so the row passed as a field as soon as the
    block was kept. These are the printed page identities, not fields of the
    form; every page repeats them.
    """
    filled = [c.strip() for c in cells if c.strip()]
    if not filled:
        return False
    txt = " ".join(filled).lower()
    if any(f in txt for f in _FURNITURE_ROWS):
        return True
    return filled[0].lower().startswith(_FURNITURE_FIRST)


def pairs(grid, k, n):
    """[("header"|"inline"|"stacked"|"group", cells)] for one block."""
    out = []
    if n:
        # the header titles, joined per column (HAP prints its header 3x)
        head = [""] * len(grid[0])
        for r in grid[k:k + n]:
            head = [(a + b).strip() if a and b else (a or b) for a, b in zip(head, r)]
        out.append(("header", head))
    # in a table a row may carry a single cell: AIB's "Credited 18.68" sits
    # alone under Details with the date column blank. Keep it when the block is
    # mostly field rows, otherwise that one cell is a title, not a field.
    orphans = bool(n) and (sum(1 for r in grid if field_row(r))
                           / max(len(grid), 1) >= 0.6)
    # With no header row (n == 0) `k` is only where the header SCAN stopped and
    # carries no meaning: page_tables already trimmed the grid to its first
    # field row, so starting at k would silently skip that many fields (IRPF
    # page 2 lost "NIF | X1385258Z | 0001" and "Apellidos y nombre | ..." this
    # way, because header_len reported k=2).
    i = (k + n) if n else 0
    # Rows above a block's first real field are its page header box: IRPF prints
    # "Agencia Tributaria | Impuesto sobre la Renta ..." and "sede.agenciatrib
    # utaria.gob.es | Ejercicio 2025 | 100" in the same block as the form body.
    # Only blocks with a proven field row are trimmed, so AIB's customer box (no
    # value-shaped cell anywhere) keeps every row.
    first = next((j for j in range(i, len(grid)) if field_row(grid[j])), None)
    if first is not None:
        i = first
    started = True          # the block already proved itself a table
    carry = ""              # a wrapped label's first line, waiting for its value
    while i < len(grid):
        cells = grid[i]
        filled = [j for j, c in enumerate(cells) if c.strip()]
        if _furniture_row(cells):
            i += 1
            continue
        if len(filled) == 1 and filled[0] == 0:
            t = cells[0].strip()
            if _is_heading(t):
                # a group label ("Inmueble 1"): one row inside the same table,
                # so the rows under it stay attributable to that property
                out.append(("group", [t]))
                carry = ""
                i += 1
                continue
            if len(t) > 60:
                # a label long enough to wrap: its first line printed on its own
                # row, the value and casilla on the next (IRPF "... Real
                # Decreto-Ley 7/2013, de 28 de junio" / "(BOE del 29) X 0106").
                # Hold it for the row that carries the value.
                carry = t
                i += 1
                continue
        if orphans and len(filled) == 1 and filled[0] >= 1:
            out.append(("inline", cells))
            i += 1
            continue
        if len(filled) >= 2:
            texts = [c for c in cells if c.strip()]
            if field_row(cells):
                if carry:
                    cells = [carry + " " + cells[0].strip()] + list(cells[1:])
                    carry = ""
                out.append(("inline", cells))
                started = True
                i += 1
                continue
            # a row of labels above a row of values, same width: pair 1:1.
            # Look one row past a bare single-cell line: AIB prints a small
            # branch code ("70738A101") between "Telephone | Account Number"
            # and its values, and that must not break the pair.
            j = i + 1
            while j < len(grid) and len([c for c in grid[j] if c.strip()]) < 2:
                j += 1
            if j < len(grid):
                nxt = [c for c in grid[j] if c.strip()]
                if (len(nxt) == len(texts) and j <= i + 2
                        and all(not any(ch.isdigit() for ch in c) for c in texts)
                        and any(any(ch.isdigit() for ch in c) for c in nxt)):
                    for left, right in zip(texts, nxt):
                        out.append(("stacked", [left, right]))
                    started = True
                    i = j + 1
                    continue
            if started and paircand(cells):
                out.append(("inline", cells))
                i += 1
                continue
        i += 1
    return out


def page_tables(data, page):
    """[(n_columns, [("header"|..., cells)])] for one page, block by block."""
    rows = [r for r in page_rows(data, page) if len(r[1]) <= PROSE_RUNS]
    tables = []
    for block in blocks(rows):
        if not block:
            continue
        k, n = header_len(block)
        if n:
            # the header titles already state the columns, trust them
            grid = prune(to_columns(block, bounds_from_header(block[k][1])))
        else:
            # without a header a short block is not evidence of a table: the
            # AIB fine print ("our website - www.aib.ie" / IBAN) is two rows
            # and would otherwise pair as a 2-column table
            if len(block) < 3:
                continue
            grid = to_columns(block, bounds_from_cells(block))
            # Rows above the block's first field row are its page header box;
            # drop them before the columns are repaired, so a stray cell in the
            # header ("Ejercicio 2025" ... "100") cannot keep two data columns
            # apart that no data row ever fills together.
            first = next((j for j, r in enumerate(grid) if field_row(r)), None)
            if first:
                grid = grid[first:]
            grid = prune(merge_singletons(grid))
        if not grid or not grid[0]:
            continue
        if not field_block(block, grid, n):
            continue
        rows_out = pairs(grid, k, n)
        if rows_out:
            tables.append((len(grid[0]), rows_out))
    return tables



def page_markdown(data, page, min_rows=3):
    """Pipe tables for one page, or None when the page is not a proven form.

    `page` is 0-based, matching pdfgeom. None means "keep what the converter
    produced": the caller must not invent structure for a prose page, and a
    page whose tables hold fewer than `min_rows` rows is not evidence of a form
    either (a two-line footer would otherwise become a table).
    """
    ok, _rows, _why = gate(data, page)
    if not ok:
        return None
    tables = page_tables(data, page)
    if not tables:
        return None
    if sum(len(out) for _n, out in tables) < min_rows:
        return None
    blocks = []
    for ncol, out in tables:
        lines = []
        for kind, cells in out:
            if kind == "group":
                lab = "**" + (cells[0] or "").replace("|", "\\|") + "**"
                row = [lab] + [""] * max(0, ncol - 1)
                lines.append("| " + " | ".join(row) + " |")
                continue
            esc = [(c or "").replace("|", "\\|") for c in cells]
            lines.append("| " + " | ".join(esc) + " |")
            if kind == "header":
                lines.append("| " + " | ".join(["---"] * ncol) + " |")
        blocks.append("\n".join(lines))
    return ("\n\n").join(blocks)
