# -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
"""Page and DOM helpers for one attached tab: element refs, the accessibility
walker, screenshots with the grid annotation, image size parsing, viewport
metrics, and tab-state capture/restore.

Every function takes what it needs (cdp client, session id, dirs, refs) - no
module state. page.py never imports cdp.py: a command's deadline comes from
util.CALL_BUDGET inside cdp.cmd.
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


# Modifier name -> (key, code, virtual key code, CDP modifiers bit). The bits
# are Input.dispatchKeyEvent's: 1 Alt, 2 Control, 4 Meta, 8 Shift.
MODIFIER_KEYS = {
    "control": ("Control", "ControlLeft", 17, 2),
    "alt": ("Alt", "AltLeft", 18, 1),
    "meta": ("Meta", "MetaLeft", 91, 4),
    "shift": ("Shift", "ShiftLeft", 16, 8),
}


def key_definition(name):
    """(virtual key code, key, code) for one key name, or None.

    KEY_MAP carries the named keys; a single letter or digit is addressed by
    its character ("a", "7"), which is what a chord like Control+A needs. The
    virtual key code of a letter is its upper-case ASCII value.
    """
    text = str(name or "").strip()
    if not text:
        return None
    if text.lower() in KEY_MAP:
        return KEY_MAP[text.lower()]
    if len(text) == 1 and text.isascii() and (text.isalpha() or text.isdigit()):
        if text.isalpha():
            upper = text.upper()
            return (ord(upper), text, "Key" + upper)
        return (ord(text), text, "Digit" + text)
    return None


# ---------------------------------------------------------------------------
# Targets (pure lookups)
# ---------------------------------------------------------------------------

async def list_page_targets(cdp_client):
    """Page targets in the order Chrome reports them, stable across calls.

    Not sorted by `attached`: that flag only says a debugger is attached, so
    the order would change as soon as this module attached to a second tab and
    `switch_tab(0)` stopped meaning what `list_tabs` showed first. Callers need
    one order that holds from call to call; the tab we drive is
    `self._current_target`.
    """
    res = await cdp_client.cmd("Target.getTargets")
    return [t for t in res.get("targetInfos", []) if t.get("type") == "page"]


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
# The user's tab (who is looking at what)
# ---------------------------------------------------------------------------

async def visibility_state(cdp_client, sid, timeout=None):
    """'visible', 'hidden', or None when it cannot be read.

    document.visibilityState describes the TAB (a background tab reports
    "hidden"), unlike document.hasFocus(), which is false whenever the
    browser window is not the focused window - the usual case when the user
    is chatting in the webui. None and "hidden" must not be confused: the
    caller says different things about a tab it knows is in the background
    and about one whose state it could not read (a stalled page hangs this
    evaluate too).
    """
    try:
        res = await cdp_client.cmd("Runtime.evaluate", {
            "expression": "document.visibilityState", "returnByValue": True,
        }, sid, timeout=timeout)
        return res.get("result", {}).get("value")
    except Exception:
        return None


async def is_active_tab(cdp_client, sid, timeout=None):
    """True if that tab is the active one: the tab the user is looking at."""
    return await visibility_state(cdp_client, sid, timeout=timeout) == "visible"


async def active_targets(cdp_client, targets, attached=None):
    """targetIds of the active tab(s): the tab(s) the user is looking at.

    One session per tab: the ones in `attached` are reused, the rest are
    attached without enabling any domain, so no event traffic is switched on
    for tabs we do not drive. Sessions opened here are detached again.

    Several ids when the browser has several windows (one visible tab each, in
    report order); none when the window is minimised.
    """
    attached = attached or {}
    found = []
    for target in targets:
        tid = target.get("targetId")
        if not tid:
            continue
        sid = attached.get(tid)
        opened = sid is None
        try:
            if opened:
                res = await cdp_client.cmd("Target.attachToTarget",
                                           {"targetId": tid, "flatten": True})
                sid = res["sessionId"]
            if await is_active_tab(cdp_client, sid):
                found.append(tid)
        except Exception:
            continue
        finally:
            if opened and sid:
                try:
                    await cdp_client.cmd("Target.detachFromTarget",
                                         {"sessionId": sid})
                except Exception:
                    pass
    return found


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


async def call_on(cdp_client, object_id, function_declaration, sid, args=None):
    """Run `function_declaration` on an element, by value.

    `args` are serialised as plain values, so the function receives them as
    ordinary JS arguments.
    """
    params = {
        "objectId": object_id,
        "functionDeclaration": function_declaration,
        "returnByValue": True,
    }
    if args is not None:
        params["arguments"] = [{"value": arg} for arg in args]
    res = await cdp_client.cmd("Runtime.callFunctionOn", params, sid)
    return res


async def tag_name(cdp_client, object_id, sid):
    """Upper-case tag name of an element ('' when it cannot be read)."""
    try:
        res = await call_on(cdp_client, object_id,
                            "function(){ return this.tagName; }", sid)
        return str((res.get("result") or {}).get("value") or "").upper()
    except Exception:
        return ""


# A <select> is set through its native value setter plus input/change events,
# the way a user pick reaches React/Vue (same trick as restore_page_state).
# Matching order: exact label, exact value, then both case-insensitive.
_SELECT_OPTION_JS = (
    "function(wanted){"
    "var want=String(wanted==null?'':wanted).trim();"
    "var opts=Array.from(this.options||[]);"
    "function norm(s){return String(s==null?'':s).trim();}"
    "var hit=opts.find(function(o){return norm(o.text)===want;})"
    "||opts.find(function(o){return norm(o.value)===want;})"
    "||opts.find(function(o){return norm(o.text).toLowerCase()===want.toLowerCase();})"
    "||opts.find(function(o){return norm(o.value).toLowerCase()===want.toLowerCase();});"
    "if(!hit){return JSON.stringify({ok:false,"
    "labels:opts.slice(0,5).map(function(o){return norm(o.text);})});}"
    "var d=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value');"
    "if(d&&d.set){d.set.call(this,hit.value);}else{this.value=hit.value;}"
    "this.dispatchEvent(new Event('input',{bubbles:true}));"
    "this.dispatchEvent(new Event('change',{bubbles:true}));"
    "return JSON.stringify({ok:true,text:norm(hit.text),value:this.value});"
    "}"
)


async def select_option(cdp_client, object_id, wanted, sid):
    """Set a <select> to the option whose label or value matches `wanted`.

    Returns {"ok": True, "text": ..., "value": ...} or {"ok": False,
    "labels": [...]} with the first few labels; {} when the call itself fails.
    """
    try:
        res = await call_on(cdp_client, object_id, _SELECT_OPTION_JS, sid,
                            args=[str(wanted)])
        value = (res.get("result") or {}).get("value")
        data = json.loads(value) if value else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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


async def viewport_size(cdp_client, sid):
    """(innerWidth, innerHeight) of the tab's viewport, or None."""
    try:
        res = await cdp_client.cmd("Runtime.evaluate", {
            "expression": "innerWidth + ',' + innerHeight",
            "returnByValue": True}, sid)
        w, h = str(res.get("result", {}).get("value", "")).split(",")
        return float(w), float(h)
    except Exception:
        return None


async def viewport_metrics(cdp_client, sid, timeout=None):
    """Viewport and document size from Page.getLayoutMetrics. Dict or None.

    Not page JS: the page that makes a capture time out can have its main
    thread blocked, and a value read through a blocked thread tells us nothing.
    None therefore means "unknown", and module.screenshot refuses full_page on
    it instead of treating it as no limit.

    Units per the protocol: `css*` fields are CSS pixels; the deprecated
    layoutViewport/contentSize are device pixels, the raster size a capture
    produces (what the pixel cap, the grid and `region` need). `docW`/`docH`
    stay in CSS pixels, the space refs and scroll offsets use.
    """
    try:
        res = await cdp_client.cmd("Page.getLayoutMetrics", {}, sid, timeout=timeout)
    except Exception:
        return None
    lv = res.get("cssLayoutViewport") or {}
    cs = res.get("cssContentSize") or {}
    dev_cs = res.get("contentSize") or {}
    if not lv and res.get("layoutViewport"):
        # pre-M93 Chrome: only the deprecated (device pixel) fields exist.
        # Read them as CSS pixels - exact at devicePixelRatio 1, and the only
        # reading available otherwise.
        lv = res.get("layoutViewport") or {}
        cs = dev_cs
    doc_w = int(cs.get("width") or 0)
    doc_h = int(cs.get("height") or 0)
    if doc_w <= 0 or doc_h <= 0:
        return None
    px_w = int(dev_cs.get("width") or 0)
    px_h = int(dev_cs.get("height") or 0)
    # image px per CSS px, straight from the two content sizes
    scale = (px_w / doc_w) if px_w and doc_w else 1.0
    if px_w <= 0 or px_h <= 0:
        px_w, px_h = int(round(doc_w * scale)), int(round(doc_h * scale))
    return {
        "scrollX": int(lv.get("pageX") or 0),
        "scrollY": int(lv.get("pageY") or 0),
        "innerWidth": int(lv.get("clientWidth") or 0),
        "innerHeight": int(lv.get("clientHeight") or 0),
        "docW": doc_w, "docH": doc_h,
        "docWpx": px_w, "docHpx": px_h,
        "devicePixelRatio": round(scale, 3),
    }


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
# Drawn on the PNG: a page canvas has a size guard on exactly the tall
# full-page captures a grid is for, and mutating the page would change what the
# AI inspects next. Cost is decode/draw/encode, ~0.15 s per megapixel plus a
# fixed viewport cost.
#
# Grid => PNG only: 1 px lines survive losslessly, JPEG would need a decoder.
# module.screenshot refuses grid + jpeg instead of picking a format.

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


def draw_grid(raw, spacing, origin_x=0, origin_y=0, scale=1.0,
              font_scale=2, label_every=1000):
    """Draw a labeled coordinate grid onto a PNG capture; returns new PNG bytes.

    Lines sit on multiples of `spacing` in *document* space, so the labels read
    as document coordinates: `origin_x`/`origin_y` are the document position of
    the image's top-left and `scale` is image pixels per document pixel
    (devicePixelRatio). At scale 2 one document pixel is two image pixels, so
    the lines are twice as far apart in the image while the labels must still
    be document numbers, not image ones.

    Raises ValueError when the image cannot be decoded (caller reports
    "grid not drawn" and keeps the plain capture).
    """
    width, height, bpp, pixels = decode_png(raw)
    stride = width * bpp
    spacing = max(4, int(spacing))
    scale = max(0.01, float(scale))

    # image-space positions where document coordinates are multiples of spacing
    step = max(1, int(round(spacing * scale)))
    x_start = (-int(round(origin_x * scale))) % step
    y_start = (-int(round(origin_y * scale))) % step

    column_ink = [bytes([_GRID_RED[c]]) for c in range(3)]
    row_ink = bytes(_GRID_RED) * width

    for x in range(x_start, width, step):
        for channel in range(3):
            start = x * bpp + channel
            pixels[start::stride] = column_ink[channel] * height
    for y in range(y_start, height, step):
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
    for x in range(x_start, width, step):
        text = f"x={int(round(origin_x + x / scale))}"
        for ly in range(2, height, label_every):
            _draw_label(pixels, width, height, bpp, text, x + 3, ly, font_scale)
    for y in range(y_start, height, step):
        label = f"y={int(round(origin_y + y / scale))}"
        _draw_label(pixels, width, height, bpp, label, 2, y + 3, font_scale)

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
    """Re-inject captured form values and scroll position into a loaded tab.

    Native value setters plus input/change events, so React/Vue inputs notice
    the programmatic fill; best-effort per selector.

    Known limit: a radio/checkbox group without ids is captured as one selector
    per member (all identical), and every member is rewritten on restore, so
    "the last captured entry wins" - such a group can come back with the wrong
    option, or none, selected.
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
