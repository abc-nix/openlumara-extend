# -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
"""
Page/DOM operation helpers for the browser module (step-6 split of
module.py; see README.md).

Stateless helpers that operate on an attached tab: element refs, the
accessibility-tree walker, screenshots (incl. the grid overlay), image
dimension parsing, viewport metrics, and tab-state capture/restore
(restart headless<->visible). Every function receives what it needs
explicitly (cdp client, session id, dirs, refs) — no module state.
Dependency direction: module.py -> page.py -> cdp.py (constants only).
"""

import asyncio
import base64
import json
import os
import time
from datetime import datetime

from .cdp import SCREENSHOT_TIMEOUT

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
                             full_page=False, fmt="png", quality=80):
    """Capture a screenshot; returns (absolute_path, bytes).

    Args:
        full_page: Capture the whole page instead of the viewport.
        fmt: 'png' or 'jpeg' (CDP-native encoding; no post-processing).
        quality: JPEG quality 0-100 (ignored for png).
    """
    ext = "jpg" if fmt == "jpeg" else "png"
    params = {
        "format": fmt,
        "captureBeyondViewport": bool(full_page),
        "fromSurface": True,
    }
    if fmt == "jpeg":
        params["quality"] = max(0, min(100, int(quality)))
    res = await cdp_client.cmd("Page.captureScreenshot", params, sid, timeout=SCREENSHOT_TIMEOUT)
    raw = base64.b64decode(res.get("data", ""))
    if not raw:
        raise RuntimeError("Screenshot came back empty")
    path = os.path.join(screenshots_dir, new_filename("shot", ext))
    with open(path, "wb") as f:
        f.write(raw)
    return path, raw


async def viewport_metrics(cdp_client, sid):
    """Read viewport/doc metrics in one evaluate. Returns a dict or None."""
    try:
        res = await cdp_client.cmd("Runtime.evaluate", {
            "expression": "JSON.stringify({"
                          "scrollX: window.scrollX, scrollY: window.scrollY,"
                          "innerWidth: window.innerWidth, innerHeight: window.innerHeight,"
                          "devicePixelRatio: window.devicePixelRatio || 1,"
                          "docW: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0),"
                          "docH: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)})",
            "returnByValue": True,
        }, sid, timeout=15.0)
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
            import struct
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
# Grid overlay (in-page canvas)
# ---------------------------------------------------------------------------

# NOTE (2026-10-05): alternative to this in-page canvas overlay is to
# post-process the captured image instead (grid drawn onto the PNG after
# the capture). A pure-stdlib prototype lives at tmp/grid_proto.py
# (decode/draw/re-encode: ~0.6s viewport, ~4s worst case; JPEG cannot be
# re-encoded without a third-party lib; upside: zero DOM involvement and
# grids could also be applied retroactively to already-saved screenshots).
# Revisit if we ever want grids that never touch the page.
async def inject_grid(cdp_client, sid, spacing, full_page=False, log=None):
    """
    Inject a temporary labeled grid overlay (single canvas). Returns the
    element id, or None on failure.

    Viewport captures: fixed canvas over the viewport; lines land on
    document-coordinate multiples of the spacing so they align with the
    doc_x/doc_y hints regardless of scroll position.
    Full-page captures: document-sized absolute canvas, corrected so its
    origin lands exactly on the document origin even when body/ancestors
    are positioned or transformed. Oversized documents (canvas limits)
    are refused and reported as applied:false in the tool metadata.
    Labels carry ABSOLUTE document coordinates and are clamped to the
    actual grid span (x-labels repeat down tall pages so they stay
    within reach).
    """
    js = (
        "(function(){"
        "var old=document.getElementById('ol-grid'); if(old) old.remove();"
        "var sp=%d;"
        "var full=%s;"  # true = doc-anchored absolute (full page); false = viewport-anchored fixed
        "var de=document.documentElement;"
        "var sx=window.scrollX, sy=window.scrollY;"
        "var vw=innerWidth, vh=innerHeight;"
        "var docW=Math.max(de.scrollWidth, document.body?document.body.scrollWidth:0);"
        "var docH=Math.max(de.scrollHeight, document.body?document.body.scrollHeight:0);"
        "var W=full?docW:vw, H=full?docH:vh;"
        "var dpr=window.devicePixelRatio||1;"
        "if(W<1||H<1||W*dpr>16000||H*dpr>16000||W*H*dpr*dpr>1.34e8) return 'too-big';"
        "var c=document.createElement('canvas'); c.id='ol-grid';"
        "c.style.cssText=(full?'position:absolute;':'position:fixed;')+'left:0;top:0;width:'+W+'px;height:'+H+'px;z-index:999999;pointer-events:none;';"
        "(document.body||de).appendChild(c);"
        # empirical anchoring: shift the canvas so its (0,0) lands exactly on
        # the capture origin (viewport top-left for fixed, document (0,0) for
        # absolute) even under positioned/transformed ancestors
        "var cr=c.getBoundingClientRect();"
        "c.style.left=(0-(cr.left+(full?sx:0)))+'px';"
        "c.style.top=(0-(cr.top+(full?sy:0)))+'px';"
        "c.width=Math.round(W*dpr); c.height=Math.round(H*dpr);"
        "var ctx=c.getContext('2d'); ctx.scale(dpr,dpr);"
        "ctx.lineWidth=1; ctx.strokeStyle='rgba(255,0,0,0.7)';"
        "ctx.font='11px monospace'; ctx.textBaseline='top';"
        "function vl(x){ctx.beginPath();ctx.moveTo(x+0.5,0);ctx.lineTo(x+0.5,H);ctx.stroke();}"
        "function hl(y){ctx.beginPath();ctx.moveTo(0,y+0.5);ctx.lineTo(W,y+0.5);ctx.stroke();}"
        "function lb(t,x,y){var w=ctx.measureText(t).width;"
        "x=Math.max(2,Math.min(x,W-w-7)); y=Math.max(2,Math.min(y,H-15));"
        "ctx.fillStyle='rgba(255,255,255,0.85)'; ctx.fillRect(x,y,w+5,13);"
        "ctx.fillStyle='#c00'; ctx.fillText(t,x+2,y+1);}"
        "if(!full){"
        "var x0=(sp-(sx%%sp))%%sp;"
        "for(var x=x0;x<=W;x+=sp){vl(x);lb('x='+Math.round(sx+x),x+3,3);}"
        "var y0=(sp-(sy%%sp))%%sp;"
        "for(var y=y0;y<=H;y+=sp){hl(y);lb('y='+Math.round(sy+y),3,y+3);}"
        "}else{"
        "for(var x2=0;x2<=W;x2+=sp){vl(x2);}"
        "for(var y2=0;y2<=H;y2+=sp){hl(y2);}"
        "for(var ly=0;ly<H;ly+=vh){for(var x3=0;x3<=W;x3+=sp){lb('x='+x3,x3+3,ly+3);}}"
        "for(var y4=0;y4<=H;y4+=sp){lb('y='+y4,3,y4+3);}"
        "}"
        "return 'ok';"
        "})()" % (int(spacing), "true" if full_page else "false")
    )
    try:
        res = await cdp_client.cmd("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        }, sid, timeout=15.0)
        val = res.get("result", {}).get("value")
        if val == "ok":
            return "ol-grid"
        if log:
            log("browser", f"grid overlay not applied (reason: {val})")
    except Exception:
        pass
    return None


async def remove_grid(cdp_client, sid, grid_id):
    """Remove the injected grid overlay; best-effort."""
    if not grid_id:
        return
    try:
        await cdp_client.cmd("Runtime.evaluate", {
            "expression": "var g=document.getElementById('" + grid_id + "'); if(g) g.remove();",
            "returnByValue": True,
        }, sid, timeout=15.0)
    except Exception:
        pass


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
        }, sid, timeout=15.0)
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
        }, sid, timeout=15.0)
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
            }, sid, timeout=15.0)
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
