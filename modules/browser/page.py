# -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
"""
Page/DOM operation helpers for the browser module (step-6 split of
module.py; see README.md).

Stateless helpers that operate on an attached tab: element refs, the
accessibility-tree walker, screenshots (plus the grid annotation drawn onto
the capture), image dimension parsing, viewport metrics, and tab-state
capture/restore
(restart headless<->visible). Every function receives what it needs
explicitly (cdp client, session id, dirs, refs) — no module state.
Dependency direction: module.py -> page.py -> cdp.py (constants only).
"""

import asyncio
import base64
import json
import os
import struct
import time
import zlib
from datetime import datetime


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAPSHOT_MAX_DEPTH = 30
DEFAULT_SNAPSHOT_CHARS = 15000

# Accessibility roles considered interactive enough to deserve a @eN ref
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "searchbox", "checkbox", "radio",
    "combobox", "listbox", "option", "tab", "switch", "slider",
    "spinbutton", "menuitem", "menuitemcheckbox", "menuitemradio",
    "treeitem", "gridcell",
}

# Key name -> (windowsVirtualKeyCode, key, code)
KEY_MAP = {
    "enter": (13, "Enter", "Enter"),
    "tab": (9, "Tab", "Tab"),
    "escape": (27, "Escape", "Escape"),
    "backspace": (8, "Backspace", "Backspace"),
    "delete": (46, "Delete", "Delete"),
    "arrowup": (38, "ArrowUp", "ArrowUp"),
    "arrowdown": (40, "ArrowDown", "ArrowDown"),
    "arrowleft": (37, "ArrowLeft", "ArrowLeft"),
    "arrowright": (39, "ArrowRight", "ArrowRight"),
    "home": (36, "Home", "Home"),
    "end": (35, "End", "End"),
    "pageup": (33, "PageUp", "PageUp"),
    "pagedown": (34, "PageDown", "PageDown"),
    "space": (32, " ", "Space"),
}


# ---------------------------------------------------------------------------
# Targets (pure lookups)
# ---------------------------------------------------------------------------

async def list_page_targets(cdp_client):
    res = await cdp_client.cmd("Target.getTargets")
    pages = [t for t in res.get("targetInfos", []) if t.get("type") == "page"]
    pages.sort(key=lambda t: 0 if t.get("attached") else 1)
    return pages


def find_target(targets, target):
    """Resolve a tab id or zero-based index to a targetId, or None."""
    if target.isdigit():
        idx = int(target)
        if 0 <= idx < len(targets):
            return targets[idx]["targetId"]
    for t in targets:
        if t.get("targetId") == target:
            return t["targetId"]
    return None


# ---------------------------------------------------------------------------
# Accessibility snapshot (pure data transform)
# ---------------------------------------------------------------------------

def is_interactive(node):
    role = node.get("role", {}).get("value")
    if role in INTERACTIVE_ROLES:
        return True
    for p in node.get("properties", []) or []:
        if p.get("name") == "focusable" and p.get("value", {}).get("value") is True:
            return True
    return False


def walk_ax_tree(nodes):
    """AX tree (Accessibility.getFullAXTree nodes) -> (lines, refs).

    lines: indented "role: name (props) [@eN]" snapshot lines.
    refs:  "@eN" -> backendDOMNodeId for interactive nodes (the caller owns
           the mapping; refs change every walk). Pure function, unit-testable
           with a saved AX-tree fixture.
    """
    by_id = {n["nodeId"]: n for n in nodes}
    children_of = {}
    child_set = set()
    for n in nodes:
        cids = n.get("childIds") or []
        children_of[n["nodeId"]] = cids
        child_set.update(cids)
    roots = [n["nodeId"] for n in nodes if n["nodeId"] not in child_set]
    if not roots:
        roots = [n["nodeId"] for n in nodes if n.get("role", {}).get("value") == "rootWebArea"]

    refs = {}
    ref_counter = [0]
    lines = []

    def walk(node_id, depth):
        if depth > SNAPSHOT_MAX_DEPTH:
            return
        n = by_id.get(node_id)
        if n is None:
            return
        if not n.get("ignored", False):
            role = n.get("role", {}).get("value") or "generic"
            name = n.get("name", {}).get("value") or ""
            props = []
            for p in n.get("properties", []) or []:
                pn = p.get("name")
                pv = p.get("value", {}).get("value")
                if pn in ("level", "checked", "pressed", "selected",
                          "expanded", "valuetext", "value") and pv is not None:
                    props.append(f"{pn}={pv}")
            ref = ""
            if n.get("backendDOMNodeId") and is_interactive(n):
                ref_counter[0] += 1
                ref_id = f"@e{ref_counter[0]}"
                refs[ref_id] = n["backendDOMNodeId"]
                ref = f" [{ref_id}]"
            suffix = f" ({', '.join(props)})" if props else ""
            lines.append(f"{'  ' * depth}{role}: {name}{suffix}{ref}")
        for c in children_of.get(node_id, []):
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    return lines, refs


# ---------------------------------------------------------------------------
# Element resolution
# ---------------------------------------------------------------------------

async def resolve_target(cdp_client, sid, refs, target):
    """Resolve '@e1' ref or CSS selector to a node handle dict."""
    if target.startswith("@"):
        backend = refs.get(target)
        if backend is None:
            raise RuntimeError(
                f"Unknown ref {target}. Take a fresh snapshot first "
                "(refs change as the page changes)."
            )
        return {"backendNodeId": backend}
    # CSS selector
    doc = await cdp_client.cmd("DOM.getDocument", {"depth": -1, "pierce": True}, sid)
    res = await cdp_client.cmd("DOM.querySelector",
                               {"nodeId": doc["root"]["nodeId"],
                                "selector": target}, sid)
    node_id = res.get("nodeId")
    if node_id is None or node_id == 0:
        raise RuntimeError(f"No element found for selector '{target}'")
    return {"nodeId": node_id}


async def resolve_object_id(cdp_client, handle, sid):
    """Turn a node handle into a Runtime objectId."""
    if "objectId" in handle:
        return handle["objectId"]
    if "backendNodeId" in handle:
        res = await cdp_client.cmd("DOM.resolveNode",
                                   {"backendNodeId": handle["backendNodeId"]}, sid)
    else:
        res = await cdp_client.cmd("DOM.resolveNode",
                                   {"nodeId": handle["nodeId"]}, sid)
    return res.get("object", {}).get("objectId")


async def call_on(cdp_client, object_id, function_declaration, sid):
    res = await cdp_client.cmd("Runtime.callFunctionOn", {
        "objectId": object_id,
        "functionDeclaration": function_declaration,
        "returnByValue": True,
    }, sid)
    return res


# ---------------------------------------------------------------------------
# Screenshots & metrics
# ---------------------------------------------------------------------------

def new_filename(prefix, ext):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rand = f"{time.time_ns():x}"[-6:]
    return f"{prefix}_{ts}_{rand}.{ext}"


def normalize_grid(value):
    """Parse the screenshot tool's `grid` param -> spacing int, or None (off).

    True/'true' -> 100; ints/custom clamp to 20-2000; unusable values fall
    back to the documented default (100) rather than erroring.
    """
    if value is None or value is False or str(value).strip().lower() in ("", "false", "0", "none"):
        return None
    if value is True or str(value).strip().lower() in ("true", "yes"):
        spacing = 100
    else:
        try:
            spacing = int(value)
        except (TypeError, ValueError):
            spacing = 100
    return max(20, min(2000, spacing))


async def capture_screenshot(cdp_client, sid, screenshots_dir,
                             full_page=False, fmt="png", quality=80, timeout=None):
    """Capture a screenshot; returns (absolute_path, bytes).

    Args:
        full_page: Capture the whole page instead of the viewport. Chrome
              paints the entire page regardless of a `clip`, so callers must
              check the page height first (module.screenshot does).
        fmt: 'png' or 'jpeg' (CDP-native encoding; no post-processing).
        quality: JPEG quality 0-100 (ignored for png).
        timeout: seconds for the capture command; None means whatever is left
              of the running tool call's budget (cdp.cmd resolves it).
    """
    ext = "jpg" if fmt == "jpeg" else "png"
    params = {
        "format": fmt,
        "captureBeyondViewport": bool(full_page),
        "fromSurface": True,
    }
    if fmt == "jpeg":
        params["quality"] = max(0, min(100, int(quality)))
    res = await cdp_client.cmd("Page.captureScreenshot", params, sid, timeout=timeout)
    raw = base64.b64decode(res.get("data", ""))
    if not raw:
        raise RuntimeError("Screenshot came back empty")
    path = os.path.join(screenshots_dir, new_filename("shot", ext))
    with open(path, "wb") as f:
        f.write(raw)
    return path, raw


async def viewport_metrics(cdp_client, sid, timeout=None):
    """Read viewport/doc metrics in one evaluate. Returns a dict or None.

    Returns None when the read fails, so the caller cannot tell a huge page
    from an unreadable one - see the note in module.screenshot.
    """
    try:
        res = await cdp_client.cmd("Runtime.evaluate", {
            "expression": "JSON.stringify({"
                          "scrollX: window.scrollX, scrollY: window.scrollY,"
                          "innerWidth: window.innerWidth, innerHeight: window.innerHeight,"
                          "devicePixelRatio: window.devicePixelRatio || 1,"
                          "docW: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0),"
                          "docH: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)})",
            "returnByValue": True,
        }, sid, timeout=timeout)
        val = res.get("result", {}).get("value")
        if val:
            return json.loads(val)
    except Exception:
        return None
    return None


def png_size(raw):
    """Parse PNG pixel dimensions from the IHDR chunk; (width, height) or None."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            w, h = struct.unpack(">II", raw[16:24])
            return w, h
    except Exception:
        pass
    return None


def jpeg_size(raw):
    """Parse JPEG pixel dimensions from the SOF marker; (width, height) or None."""
    try:
        if raw[:2] != b"\xff\xd8":
            return None
        i = 2
        n = len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            # skip padding 0xFF bytes
            if marker == 0xFF:
                i += 1
                continue
            # SOF markers carrying dimensions (exclude DHT C4, JPG C8, DAC CC)
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                # segment: FF xx | len(2) | precision(1) | height(2) | width(2)
                h = (raw[i + 5] << 8) | raw[i + 6]
                w = (raw[i + 7] << 8) | raw[i + 8]
                if w > 0 and h > 0:
                    return w, h
            # standalone markers without length (RST, SOI, EOI, TEM)
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                i += 2
                continue
            seg_len = (raw[i + 2] << 8) | raw[i + 3]
            if seg_len < 2:
                return None
            i += 2 + seg_len
        return None
    except Exception:
        return None


def image_size(raw, fmt):
    """Parse pixel dimensions for png or jpeg; (width, height) or None."""
    if fmt == "jpeg":
        return jpeg_size(raw)
    return png_size(raw)


# ---------------------------------------------------------------------------
# Grid annotation (drawn onto the captured PNG, never into the page)
# ---------------------------------------------------------------------------
# 2026-10-27: replaces the in-page canvas overlay. That overlay refused tall
# pages by design (canvas guard: >16000 px in either dimension), i.e. exactly
# the full-page captures where a grid is most useful, and it mutated the page
# the AI may inspect next. Drawing on the PNG instead has no size limit beyond
# RAM and keeps the page untouched. Measured cost with the stdlib path below:
# ~0.7 s for a 1272x1305 viewport capture, ~0.15 s per additional megapixel.
#
# Grid => PNG: 1 px lines survive PNG losslessly; JPEG would need a decoder.
# `module.screenshot` refuses (grid + jpeg) rather than picking a format.

_GRID_RED = (224, 0, 0)

# 3x5 bitmap glyphs for the label alphabet ("x=1234", "y=567")
_FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "x": ("101", "101", "010", "101", "101"),
    "y": ("101", "101", "010", "010", "010"),
    "=": ("000", "111", "000", "111", "000"),
}


def decode_png(raw):
    """PNG bytes -> (width, height, bpp, pixel bytearray).

    Only what CDP emits is supported: 8-bit RGB or RGBA, non-interlaced.
    Raises ValueError for anything else (the caller reports it as
    "grid not drawn" instead of guessing).
    """
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width = height = colortype = None
    idat = []
    pos = 8
    while pos + 8 <= len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        ctype = raw[pos + 4:pos + 8]
        chunk = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, depth, colortype, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", chunk)
            if depth != 8 or interlace:
                raise ValueError(f"unsupported PNG (depth {depth}, interlace {interlace})")
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if colortype not in (2, 6):
        raise ValueError(f"unsupported PNG colour type {colortype}")

    bpp = 3 if colortype == 2 else 4
    stride = width * bpp
    filtered = zlib.decompress(b"".join(idat))
    pixels = bytearray(height * stride)
    prev = bytes(stride)
    pos = 0
    for y in range(height):
        ftype = filtered[pos]
        row = filtered[pos + 1:pos + 1 + stride]
        pos += 1 + stride
        if ftype == 1:                      # Sub
            acc = bytearray(row)
            for i in range(bpp, stride):
                acc[i] = (acc[i] + acc[i - bpp]) & 255
            row = bytes(acc)
        elif ftype == 2:                    # Up
            row = bytes([(a + b) & 255 for a, b in zip(row, prev)])
        elif ftype == 3:                    # Average
            acc = bytearray(row)
            for i in range(stride):
                left = acc[i - bpp] if i >= bpp else 0
                acc[i] = (acc[i] + ((left + prev[i]) >> 1)) & 255
            row = bytes(acc)
        elif ftype == 4:                    # Paeth
            acc = bytearray(row)
            for i in range(stride):
                a = acc[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                acc[i] = (acc[i] + pred) & 255
            row = bytes(acc)
        pixels[y * stride:(y + 1) * stride] = row
        prev = row
    return width, height, bpp, pixels


def encode_png(width, height, bpp, pixels, level=6):
    """pixel bytearray -> PNG bytes, all rows filter 0 (fast, valid)."""
    stride = width * bpp
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2 if bpp == 3 else 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + chunk(b"IEND", b""))


def _draw_label(pixels, width, height, bpp, text, x, y, scale):
    """White-backed red bitmap label, clamped inside the image."""
    stride = width * bpp
    cell_w = (3 + 1) * scale
    text_w = len(text) * cell_w
    box_w, box_h = text_w + 2 * scale, 5 * scale + 2 * scale
    x = max(0, min(x, width - box_w))
    y = max(0, min(y, height - box_h))
    if x < 0 or y < 0 or box_w > width:
        return
    white = bytes([255]) * (box_w * bpp)          # opaque white, alpha included
    for row in range(y, y + box_h):
        start = row * stride + x * bpp
        pixels[start:start + box_w * bpp] = white
    ink = bytes(_GRID_RED)
    for char_index, char in enumerate(text):
        glyph = _FONT.get(char)
        if not glyph:
            continue
        cx = x + scale + char_index * cell_w
        for gy, glyph_row in enumerate(glyph):
            for gx, bit in enumerate(glyph_row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px = cx + gx * scale + dx
                        py = y + scale + gy * scale + dy
                        if 0 <= px < width and 0 <= py < height:
                            start = py * stride + px * bpp
                            pixels[start:start + 3] = ink


def draw_grid(raw, spacing, origin_x=0, origin_y=0, font_scale=2, label_every=1000):
    """Draw a labeled coordinate grid onto a PNG capture; returns new PNG bytes.

    Lines sit on multiples of `spacing` in *document* space, so the labels read
    as document coordinates: `origin_x`/`origin_y` are the document position of
    the image's top-left (exactly the `region` the screenshot tool reports).

    Raises ValueError when the image cannot be decoded (caller reports
    "grid not drawn" and keeps the plain capture).
    """
    width, height, bpp, pixels = decode_png(raw)
    stride = width * bpp
    spacing = max(4, int(spacing))

    # image-space positions where document coordinates are multiples of spacing
    x_start = (-int(origin_x)) % spacing
    y_start = (-int(origin_y)) % spacing

    column_ink = [bytes([_GRID_RED[c]]) for c in range(3)]
    row_ink = bytes(_GRID_RED) * width

    for x in range(x_start, width, spacing):
        for channel in range(3):
            start = x * bpp + channel
            pixels[start::stride] = column_ink[channel] * height
    for y in range(y_start, height, spacing):
        row_start = y * stride
        if bpp == 3:
            pixels[row_start:row_start + stride] = row_ink
        else:
            # RGBA: write the three colour channels per pixel, leave alpha alone
            for channel in range(3):
                pixels[row_start + channel:row_start + stride:4] = \
                    bytes([_GRID_RED[channel]]) * width

    # labels: x= on top of each vertical line (repeated down tall captures),
    # y= at the left edge of each horizontal line
    for x in range(x_start, width, spacing):
        text = f"x={origin_x + x}"
        for ly in range(2, height, label_every):
            _draw_label(pixels, width, height, bpp, text, x + 3, ly, font_scale)
    for y in range(y_start, height, spacing):
        _draw_label(pixels, width, height, bpp, f"y={origin_y + y}", 2, y + 3, font_scale)

    return encode_png(width, height, bpp, pixels)


# ---------------------------------------------------------------------------
# Tab-state capture & restore (restart headless <-> visible)
# ---------------------------------------------------------------------------
# What CAN be restored across a relaunch: url/title (module side), scroll
# position, and filled form values (addressed by id/name). Password fields
# are deliberately NEVER captured. What CANNOT: navigation history stacks
# (no CDP API to inject one), sessionStorage semantics, downloads, dialogs.

async def capture_page_state(cdp_client, sid):
    """Scrape scroll position + non-secret form values of one tab."""
    js = (
        "(function(){"
        "var out={scrollX:window.scrollX,scrollY:window.scrollY,forms:[]};"
        "var els=document.querySelectorAll('input,textarea,select');"
        "for(var i=0;i<els.length;i++){"
        "var e=els[i];var sel=null;"
        "if(e.id){sel='#'+CSS.escape(e.id);}"
        "else if(e.name){sel=e.tagName.toLowerCase()+'[name=\"'+CSS.escape(e.name)+'\"]';}"
        "if(!sel)continue;"
        "var t=(e.type||'').toLowerCase();"
        "if(t==='password'||t==='file')continue;"
        "if(t==='checkbox'||t==='radio'){out.forms.push({s:sel,c:!!e.checked});continue;}"
        "if(t==='submit'||t==='button'||t==='image')continue;"
        "out.forms.push({s:sel,v:e.value});"
        "}"
        "return JSON.stringify(out);"
        "})()"
    )
    try:
        res = await cdp_client.cmd("Runtime.evaluate", {
            "expression": js, "returnByValue": True,
        }, sid)
        val = res.get("result", {}).get("value")
        if val:
            return json.loads(val)
    except Exception:
        pass
    return None


async def restore_page_state(cdp_client, sid, state):
    """Re-inject captured form values + scroll position into a loaded tab.

    Uses native value setters + input/change events so React/Vue inputs
    notice the programmatic fill; applied to querySelectorAll matches so
    radio groups work. Best-effort per selector.
    """
    if not state:
        return False
    js = (
        "(function(st){"
        "if(!st)return;"
        "(st.forms||[]).forEach(function(f){"
        "var list;try{list=document.querySelectorAll(f.s);}catch(e){return;}"
        "list.forEach(function(e){"
        "if('c' in f){if(e.type==='checkbox'||e.type==='radio'){e.checked=f.c;return;}}"
        "var proto=e.tagName==='SELECT'?HTMLSelectElement.prototype:"
        "(e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype);"
        "var d=Object.getOwnPropertyDescriptor(proto,'value');"
        "if(d&&d.set){d.set.call(e,f.v);}else{e.value=f.v;}"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));"
        "});});"
        "window.scrollTo(st.scrollX||0,st.scrollY||0);"
        "})(" + json.dumps(state) + ")"
    )
    try:
        await cdp_client.cmd("Runtime.evaluate", {
            "expression": js, "returnByValue": True,
        }, sid)
        return True
    except Exception:
        return False


async def wait_page_ready(cdp_client, sid, timeout=10.0):
    """Poll document.readyState until 'complete' (or give up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            res = await cdp_client.cmd("Runtime.evaluate", {
                "expression": "document.readyState", "returnByValue": True,
            }, sid)
            if res.get("result", {}).get("value") == "complete":
                return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# Window bounds (Browser domain, via a tab session)
# ---------------------------------------------------------------------------

async def get_window_bounds(cdp_client, sid):
    """(left, top, width, height) of the tab's window, or None."""
    if not sid:
        return None
    try:
        w = await cdp_client.cmd("Browser.getWindowForTarget", {}, sid)
        b = await cdp_client.cmd("Browser.getWindowBounds", {"windowId": w["windowId"]})
        return b.get("bounds")
    except Exception:
        return None


async def set_window_bounds(cdp_client, sid, bounds):
    """Restore window position/size (best-effort; no-op in headless)."""
    if not bounds or not sid:
        return
    try:
        w = await cdp_client.cmd("Browser.getWindowForTarget", {}, sid)
        params = {k: bounds[k] for k in ("left", "top", "width", "height")
                  if k in bounds and bounds[k] is not None}
        if params:
            await cdp_client.cmd("Browser.setWindowBounds",
                                 {"windowId": w["windowId"], "bounds": params})
    except Exception:
        pass
