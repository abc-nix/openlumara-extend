# -- AI GENERATED CODE (deepseek-v4.1-flash) :: (2026-10-26)
"""
Small pure helpers shared by module.py: URL normalisation for navigate/new_tab,
safe tail-slicing of the console/network deques, and the per-tool-call time
budget.

Kept out of page.py (DOM/screenshot work) so they need no core/aiohttp stubs and
can be tested directly - see tests/test_util.py.
"""

import contextvars
import time
import urllib.parse

# Cost of the grid annotation drawn onto a PNG (decode + draw + encode),
# measured on this module's own saved captures - real Chrome PNGs are mostly
# filter-4 (Paeth) rows, so decode_png's Python loop dominates: ~0.85 s/Mpx
# (1272x1305 -> 1.6 s, 1257x2952 -> 3.1 s).
GRID_COST_S_PER_MPX = 0.85

# schemes navigate/new_tab accept as-is; anything else gets https:// prefixed
ALLOWED_SCHEMES = ("http", "https", "file", "about", "data", "chrome")


def normalize_url(url):
    """Make an AI-supplied URL navigable.

    - empty -> about:blank
    - protocol-relative ("//host") -> https:...
    - known scheme (http/https/file/about/data/chrome) -> unchanged
    - anything else -> https:// prefixed

    urlparse treats "localhost:3000" as scheme "localhost", which is why a
    fixed allow-list is used instead of the naive "has a scheme" test.
    """
    url = str(url or "").strip()
    if not url:
        return "about:blank"
    if url.startswith("//"):
        return "https:" + url
    if urllib.parse.urlparse(url).scheme not in ALLOWED_SCHEMES:
        return "https://" + url
    return url


def tail_entries(buffer, count):
    """The last `count` entries of a deque.

    Slicing with -0 returns the WHOLE buffer, which is why 0 (and anything
    unparseable or negative) is handled explicitly.
    """
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        count = 0
    return list(buffer)[-count:] if count else []


class Budget:
    """One deadline for a whole tool call.

    The framework cancels any tool call running past core.tool_timeout and the
    model then sees only "Tool timed out after Ns" (core/toolcalls.py:229).
    A browser tool issues several CDP commands back to back, so the budget is
    taken once at tool entry and every command gets what is left of it: the
    call always finishes inside the framework's number, and reports *that*
    number, so there is exactly one timeout to raise (Settings -> Core -> Tool
    Timeout) instead of a module-private one the caller has never heard of.

    The deadline starts when the tool starts, which also covers the work that
    happens before any CDP call (launching Chrome costs 1-3 s).
    """

    __slots__ = ("total", "_end")

    def __init__(self, total):
        self.total = float(total)
        self._end = time.monotonic() + self.total

    def left(self):
        """Seconds still available; never negative."""
        return max(0.0, self._end - time.monotonic())

    def cap(self, ceiling):
        """Timeout for the next command: what it usually needs, or what is left."""
        return min(float(ceiling), self.left())

    def spent(self):
        """Seconds used so far (for logs/messages)."""
        return max(0.0, self.total - self.left())


# Budget of the tool call that is running right now (set by module._guarded).
# A ContextVar, not an attribute on the module: the module object is shared by
# every caller, and two tool calls running at the same time must not see each
# other's deadline.
CALL_BUDGET = contextvars.ContextVar("browser_call_budget", default=None)


def tool_budget(total, reserve=0.5):
    """Deadline for one tool call, from the framework's `total` seconds.

    A slice of `total` is held back so the module answers *before* core's own
    timer fires: core starts counting before the tool body runs and the answer
    still has to travel back. The slice shrinks with very small totals
    (`min(reserve, total * 0.2)`) so the budget is always shorter than the
    call, even if someone sets tool_timeout to one second.
    """
    total = max(1.0, float(total))
    return Budget(total - min(float(reserve), total * 0.2))


def resolve_timeout(timeout, budget=None, fallback=10.0):
    """The deadline for one CDP command.

    An explicit `timeout` always wins - a caller that passes one means it.
    Without one the command gets what is left of the running tool call, so
    the framework's number stays the only limit a caller can hit; `fallback`
    is for calls made outside a tool call (webui routes, tests).
    """
    if timeout is None:
        timeout = budget.left() if budget is not None else fallback
    # never hand a command a timeout of zero: wait_for would raise at once
    # with a message that reads like a bug rather than an exhausted budget
    return max(0.1, float(timeout))


def grid_fits(width, height, seconds_left):
    """Would annotating a WxH capture fit in `seconds_left`?

    Returns (fits, estimated_seconds). The grid is pure CPU *after* the CDP
    reply, so no command timeout covers it: starting it with no time left just
    makes the caller wait past the tool budget for an answer they then lose.
    """
    try:
        pixels = max(0, int(width)) * max(0, int(height))
    except (TypeError, ValueError):
        return False, 0.0
    need = pixels / 1e6 * GRID_COST_S_PER_MPX
    return need <= max(0.0, float(seconds_left)), need


def first_capture_window(seconds_left, full_page):
    """How long the first capture command may take.

    A viewport capture is the reliable one (it has never been seen to stall) and
    gets everything that is left. A full-page capture can stall Chrome on some
    documents (B43: three hangs on one example.com document, ~14.5 s each,
    while the same page captured in a fresh tab) and used to eat the whole call
    doing it. Giving it half the call leaves the other half for a viewport
    fallback, so a stall still answers with an image instead of with nothing.
    """
    try:
        left = max(0.0, float(seconds_left))
    except (TypeError, ValueError):
        left = 0.0            # unknown reads as "nothing left": no invented wait
    if not full_page:
        return left
    return min(left, max(2.0, left * 0.5))


def capture_fits(width, height, max_pixels):
    """Would a WxH full-page capture fit the pixel cap? (fits, megapixels).

    Area, not height: cost grows with pixels (see GRID_COST_S_PER_MPX), and a
    height cap refuses a tall-but-narrow page that fits while accepting a
    short-but-huge one.
    """
    try:
        pixels = max(0, int(width)) * max(0, int(height))
        limit = max(0, int(max_pixels))
    except (TypeError, ValueError):
        return False, 0.0
    return pixels <= limit, pixels / 1e6


def timeout_text(text, limit, capture=False):
    """The message for a call that ran out of the framework's budget (B40).

    `text` is the failure itself ("CDP command 'X' timed out after 14.5s") and
    `limit` is `core.tool_timeout` - the number in Settings, the only timeout a
    caller can raise. The command's own number is a little lower because the
    module answers just before core's timer fires; naming `limit` rather than
    the budget keeps the message consistent with the setting the user edits
    (naming a module-private limit would send them to a setting that does not
    help - that was B40).
    """
    out = (f"{text} - this tool call gets {float(limit):.1f}s in total "
           "(Settings > Core > Tool Timeout), and the module stops just short "
           "of that so it can answer instead of being cut off. The page may be "
           "busy or very large: retry with a narrower action, or raise that "
           "setting.")
    if capture:
        out += " For a capture, try the viewport instead of full_page."
    return out


def rejected_top_level_await(details):
    """Did Chrome reject the expression because of a top-level `await` (B30)?

    `Runtime.evaluate` runs the string as a script, where `await` is a syntax
    error no matter that `awaitPromise` is on. The answer is a SyntaxError
    whose text names await, which is what separates it from every other
    exception the page can throw.
    """
    if not details:
        return False
    text = (str(details.get("text", ""))
            + str((details.get("exception") or {}).get("description", "")))
    return "SyntaxError" in text and "await" in text


def wrap_await(expression):
    """Wrap an expression so a top-level `await` becomes legal (B30).

    Kept as `return (<expression>)` so an expression keeps its value. The
    newline matters: it stops a trailing `//` comment from swallowing the
    closing bracket.
    """
    return "(async () => { return (" + str(expression) + "\n); })()"


def headless_from_version(info):
    """Was the browser that answered /json/version headless? (B33, corrected)

    The "Browser" product string is not enough. Measured 2026-10-29 with
    Chrome 156 and `--headless=new`: /json/version answers
    "Browser": "Chrome/156.0.8078.73" while its User-Agent still says
    HeadlessChrome - so the product-string check reported a headless browser
    as visible, the exact error B33 was meant to end. The UA is the browser's
    own claim about itself and wins; the product string stays as the fallback
    for builds whose UA says nothing either way.
    """
    if not info:
        return None
    ua = str(info.get("User-Agent") or "")
    if "HeadlessChrome" in ua:
        return True
    product = headless_from_product(info.get("Browser"))
    if product is not None:
        return product
    if ua:
        return False
    return None


def headless_from_product(product):
    """Was `product` (CDP /json/version "Browser") running headless?

    Chrome reports "HeadlessChrome/120.0…" in headless mode and "Chrome/120.0…"
    otherwise, so the running browser states its own mode. The `headless`
    setting only predicts what the *next launch* would use and says nothing at
    all about a browser we merely attached to - trust the string when we have
    it, return None when we don't.
    """
    text = str(product or "").strip()
    if not text:
        return None
    return text.split("/")[0].lower().startswith("headless")


def drop_target(attached, current_target, target_id):
    """Forget a tab that died (Target.targetDestroyed / targetCrashed).

    Drops its session id so a later attach creates a fresh one instead of
    reusing a dead sessionId (which answers -32001 forever). Returns the
    current target to keep: None when the current tab was the one that died.
    """
    if not target_id:
        return current_target
    attached.pop(target_id, None)
    return None if current_target == target_id else current_target
