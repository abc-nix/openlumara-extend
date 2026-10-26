# -- AI GENERATED CODE (deepseek-v4.1-flash) :: (2026-10-26)
"""
Small pure helpers shared by module.py: URL normalisation for navigate/new_tab
and safe tail-slicing of the console/network deques.

Kept out of page.py (DOM/screenshot work) so they need no core/aiohttp stubs and
can be tested directly - see tests/test_util.py.
"""

import urllib.parse

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
