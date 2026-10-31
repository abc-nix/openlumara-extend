# Tests for the browser module's stateless helpers.
#
# They deliberately do NOT import module.py (the entry file pulls in core,
# aiohttp and the webui channel). Instead they load cdp.py/page.py as a tiny
# package with `core` and `aiohttp` stubbed out, so the suite is pure stdlib
# and has no side effects on the running install.
#
# Run from anywhere:
#   python3 -m unittest discover -s openlumara-extend/modules/browser/tests -v
# (the path only matters for discovery; imports are resolved from __file__)

import importlib.util
import struct
import sys
import types
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
PKG = "_browser_helpers_pkg"


def _load_helpers():
    """import cdp.py + page.py as a package, with core/aiohttp stubbed."""
    if PKG in sys.modules:
        return sys.modules[PKG]

    # cdp.py imports these at module level but only calls them from methods
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    core_stub = types.ModuleType("core")
    core_stub.get_data_path = lambda subpath=None: str(MODULE_DIR)   # noqa: E731
    core_stub.detail_error = lambda e: str(e)                        # noqa: E731
    sys.modules.setdefault("core", core_stub)

    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(MODULE_DIR)]
    sys.modules[PKG] = pkg
    return pkg


_load_helpers()

from _browser_helpers_pkg import cdp, page  # noqa: E402


class NormalizeGridTests(unittest.TestCase):
    def test_off_values(self):
        for value in (None, False, "", "false", "FALSE", "0", "none", "  "):
            self.assertIsNone(page.normalize_grid(value), f"{value!r} should be off")

    def test_on_values(self):
        for value in (True, "true", "yes", "TRUE"):
            self.assertEqual(page.normalize_grid(value), 100, value)
        self.assertEqual(page.normalize_grid("50"), 50)
        self.assertEqual(page.normalize_grid(80), 80)

    def test_clamping_and_fallback(self):
        self.assertEqual(page.normalize_grid(1), 20)       # clamped up
        self.assertEqual(page.normalize_grid(99999), 2000)  # clamped down
        self.assertEqual(page.normalize_grid("not-a-number"), 100)  # documented default


class FindTargetTests(unittest.TestCase):
    TARGETS = [
        {"targetId": "AAA"},
        {"targetId": "BBB"},
    ]

    def test_by_index(self):
        self.assertEqual(page.find_target(self.TARGETS, "0"), "AAA")
        self.assertEqual(page.find_target(self.TARGETS, "1"), "BBB")

    def test_by_id(self):
        self.assertEqual(page.find_target(self.TARGETS, "BBB"), "BBB")

    def test_misses(self):
        self.assertIsNone(page.find_target(self.TARGETS, "9"))
        self.assertIsNone(page.find_target(self.TARGETS, "ZZZ"))
        self.assertIsNone(page.find_target([], "0"))


class WalkAxTreeTests(unittest.TestCase):
    def _ax_nodes(self):
        return [
            {
                "nodeId": 1,
                "role": {"value": "rootWebArea"},
                "name": {"value": "Example"},
                "childIds": [2, 3],
            },
            {
                "nodeId": 2,
                "role": {"value": "button"},
                "name": {"value": "Submit"},
                "backendDOMNodeId": 42,
                "properties": [{"name": "focusable", "value": {"value": True}}],
                "childIds": [4],
            },
            {
                "nodeId": 3,
                "role": {"value": "StaticText"},
                "name": {"value": "Hello"},
            },
            {
                "nodeId": 4,
                "role": {"value": "StaticText"},
                "name": {"value": "Submit"},
            },
        ]

    def test_interactive_nodes_get_refs(self):
        lines, refs = page.walk_ax_tree(self._ax_nodes())
        self.assertEqual(refs, {"@e1": 42})
        # lines are indented by depth, so match on the line content
        self.assertTrue(any(l.strip() == "button: Submit [@e1]" for l in lines), lines)
        self.assertEqual(lines[0], "rootWebArea: Example")  # depth 0

    def test_non_interactive_nodes_get_no_ref(self):
        lines, _ = page.walk_ax_tree(self._ax_nodes())
        hello = next(l for l in lines if l.strip() == "StaticText: Hello")
        self.assertNotIn("@e", hello)


class ImageSizeTests(unittest.TestCase):
    @staticmethod
    def _png(width, height):
        # PNG layout: 8-byte signature | length(4) | "IHDR"(4) | width(4) | height(4)
        # png_size() reads the magic at 0..8, "IHDR" at 12..16 and w/h at 16..24
        return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
                + struct.pack(">II", width, height) + b"\x00" * 8)

    @staticmethod
    def _jpeg(width, height):
        sof = b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width) + b"\x03" * 9
        return b"\xff\xd8" + sof + b"\xff\xd9"

    def test_png(self):
        self.assertEqual(page.png_size(self._png(320, 200)), (320, 200))

    def test_jpeg(self):
        self.assertEqual(page.jpeg_size(self._jpeg(1024, 768)), (1024, 768))

    def test_dispatch(self):
        self.assertEqual(page.image_size(self._png(10, 20), "png"), (10, 20))
        self.assertEqual(page.image_size(self._jpeg(30, 40), "jpeg"), (30, 40))

    def test_garbage(self):
        self.assertIsNone(page.png_size(b"nope"))
        self.assertIsNone(page.jpeg_size(b"nope"))
        self.assertIsNone(page.jpeg_size(b"\xff\xd8\xff\xc0"))


class _FakeCDP:
    """Just enough of cdp.CDPClient for viewport_metrics."""

    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls = []

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        self.calls.append(method)
        if self.error:
            raise self.error
        return self.result


class ListPageTargetsTests(unittest.TestCase):
    def test_order_is_the_order_chrome_reports(self):
        # B26: sorting by `attached` made the index depend on our own
        # attachments, so switch_tab(0) could hit another tab than list_tabs(0)
        import asyncio
        fake = _FakeCDP({"targetInfos": [
            {"targetId": "B", "type": "page", "attached": False},
            {"targetId": "A", "type": "page", "attached": True},
            {"targetId": "S", "type": "service_worker"},
        ]})
        targets = asyncio.run(page.list_page_targets(fake))
        self.assertEqual([t["targetId"] for t in targets], ["B", "A"])


class _VisibilityCDP:
    """Answers attach / evaluate / detach, one visibility state per target."""

    def __init__(self, states=None, fail_attach=(), fail_eval=()):
        self.states = states or {}
        self.fail_attach = set(fail_attach)
        self.fail_eval = set(fail_eval)
        self.sessions = {}          # sessionId -> targetId
        self.calls = []             # (method, targetId, sessionId)

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        self.calls.append((method, params.get("targetId"), session_id))
        if method == "Target.attachToTarget":
            tid = params["targetId"]
            if tid in self.fail_attach:
                raise RuntimeError("cannot attach")
            self.sessions["sid-" + tid] = tid
            return {"sessionId": "sid-" + tid}
        if method == "Runtime.evaluate":
            tid = self.sessions[session_id]
            if tid in self.fail_eval:
                raise RuntimeError("evaluation failed")
            return {"result": {"value": self.states.get(tid, "hidden")}}
        if method == "Target.detachFromTarget":
            return {}
        raise AssertionError(method)


def _page_targets(*ids):
    return [{"targetId": i, "type": "page", "url": "https://x.test/"} for i in ids]


class ActiveTabTests(unittest.TestCase):
    """The active tab: the one the user is looking at (visibilityState)."""

    def test_visible_tab_is_the_active_one(self):
        import asyncio
        cdp = _VisibilityCDP({"A": "visible"})
        found = asyncio.run(page.active_targets(cdp, _page_targets("A", "B")))
        self.assertEqual(found, ["A"])

    def test_several_windows_report_every_active_tab(self):
        import asyncio
        cdp = _VisibilityCDP({"A": "visible", "B": "visible"})
        found = asyncio.run(page.active_targets(cdp, _page_targets("A", "B")))
        self.assertEqual(found, ["A", "B"])

    def test_sessions_it_opened_are_detached_again(self):
        import asyncio
        cdp = _VisibilityCDP()
        asyncio.run(page.active_targets(cdp, _page_targets("A")))
        self.assertEqual([c[0] for c in cdp.calls],
                         ["Target.attachToTarget", "Runtime.evaluate",
                          "Target.detachFromTarget"])

    def test_our_own_session_is_reused_and_kept(self):
        # the tab this module drives already has a session: no attach, no
        # detach, and no domain enables on a tab the user is working in
        import asyncio
        cdp = _VisibilityCDP({"A": "visible"})
        cdp.sessions["sid-A"] = "A"          # the session the module already has
        found = asyncio.run(page.active_targets(cdp, _page_targets("A"),
                                               {"A": "sid-A"}))
        self.assertEqual(found, ["A"])
        self.assertEqual([c[0] for c in cdp.calls], ["Runtime.evaluate"])

    def test_attach_failure_skips_that_tab(self):
        import asyncio
        cdp = _VisibilityCDP({"B": "visible"}, fail_attach=("A",))
        found = asyncio.run(page.active_targets(cdp, _page_targets("A", "B")))
        self.assertEqual(found, ["B"])

    def test_eval_failure_reads_as_not_active(self):
        import asyncio
        cdp = _VisibilityCDP({"A": "visible"}, fail_eval=("A",))
        self.assertEqual(
            asyncio.run(page.active_targets(cdp, _page_targets("A"))), [])

    def test_minimised_window_has_no_active_tab(self):
        import asyncio
        cdp = _VisibilityCDP()
        self.assertEqual(
            asyncio.run(page.active_targets(cdp, _page_targets("A", "B"))), [])

    def test_is_active_tab_reads_the_state(self):
        import asyncio

        class One:
            def __init__(self, value):
                self.value = value

            async def cmd(self, method, params=None, session_id=None, **kw):
                return {"result": {"value": self.value}}

        self.assertTrue(asyncio.run(page.is_active_tab(One("visible"), "sid")))
        self.assertFalse(asyncio.run(page.is_active_tab(One("hidden"), "sid")))

    def test_is_active_tab_swallows_errors(self):
        import asyncio

        class Broken:
            async def cmd(self, *a, **k):
                raise RuntimeError("gone")

        self.assertFalse(asyncio.run(page.is_active_tab(Broken(), "sid")))

    def test_visibility_state_distinguishes_hidden_from_unknown(self):
        # "hidden" is a fact about the tab, None is "could not read it": the
        # screenshot timeout text must not blame the background for a stalled
        # page whose evaluate also hung
        import asyncio

        class One:
            def __init__(self, value):
                self.value = value

            async def cmd(self, method, params=None, session_id=None, **kw):
                return {"result": {"value": self.value}}

        class Broken:
            async def cmd(self, *a, **k):
                raise RuntimeError("gone")

        self.assertEqual(
            asyncio.run(page.visibility_state(One("hidden"), "sid")), "hidden")
        self.assertIsNone(asyncio.run(page.visibility_state(Broken(), "sid")))


class ViewportSizeTests(unittest.TestCase):
    """The click tool needs the viewport to know if a box centre is inside it."""

    def test_returns_width_and_height(self):
        import asyncio

        class One:
            async def cmd(self, *a, **k):
                return {"result": {"value": "1272,1305"}}

        self.assertEqual(asyncio.run(page.viewport_size(One(), "sid")),
                         (1272.0, 1305.0))

    def test_unreadable_is_none(self):
        import asyncio

        class Broken:
            async def cmd(self, *a, **k):
                raise RuntimeError("gone")

        self.assertIsNone(asyncio.run(page.viewport_size(Broken(), "sid")))


class ViewportMetricsTests(unittest.TestCase):
    """B38: the metrics must come from CDP, in the right units."""

    LAYOUT = {"pageX": 10, "pageY": 20, "clientWidth": 1200, "clientHeight": 800}
    CSS = {"width": 1200, "height": 9000}
    DEV = {"width": 2400, "height": 18000}

    def _metrics(self, result):
        import asyncio
        return asyncio.run(page.viewport_metrics(_FakeCDP(result), "sid"))

    def test_reads_css_and_device_pixels(self):
        m = self._metrics({"cssLayoutViewport": self.LAYOUT,
                           "cssContentSize": self.CSS, "contentSize": self.DEV})
        self.assertEqual((m["docW"], m["docH"]), (1200, 9000))     # CSS px
        self.assertEqual((m["docWpx"], m["docHpx"]), (2400, 18000))  # capture px
        self.assertEqual(m["devicePixelRatio"], 2.0)
        self.assertEqual((m["scrollX"], m["scrollY"]), (10, 20))
        self.assertEqual((m["innerWidth"], m["innerHeight"]), (1200, 800))

    def test_does_not_touch_page_javascript(self):
        # the page that blocks its main thread is exactly the one whose guard
        # value must still be readable
        fake = _FakeCDP({"cssLayoutViewport": self.LAYOUT, "cssContentSize": self.CSS,
                         "contentSize": self.DEV})
        import asyncio
        asyncio.run(page.viewport_metrics(fake, "sid"))
        self.assertEqual(fake.calls, ["Page.getLayoutMetrics"])

    def test_unknown_size_is_none_not_zero(self):
        # None means "cannot measure", which module.screenshot refuses; 0 would
        # have read as "nothing to cap"
        self.assertIsNone(self._metrics({"cssContentSize": {"width": 0, "height": 0}}))

    def test_cdp_failure_is_none(self):
        import asyncio
        fake = _FakeCDP(error=TimeoutError("no time"))
        self.assertIsNone(asyncio.run(page.viewport_metrics(fake, "sid")))

    def test_old_chrome_without_css_fields(self):
        m = self._metrics({"layoutViewport": self.LAYOUT,
                           "contentSize": {"width": 800, "height": 4000}})
        self.assertEqual((m["docW"], m["docH"]), (800, 4000))
        self.assertEqual(m["devicePixelRatio"], 1.0)


class _NoHTTP:
    """HTTP stub whose every request fails at once (a port nobody listens on)."""

    def get(self, *args, **kwargs):
        raise RuntimeError("connection refused")


class LaunchFailureTests(unittest.TestCase):
    """B7: a browser that never comes up must not be left running.

    This is the process harness: a real child process (`exec sleep`, so it
    replaces the shell and there is no orphan) stands in for Chrome.
    """

    def test_a_launch_that_never_comes_up_is_killed(self):
        import asyncio
        import os
        import socket
        import tempfile
        import time as _time

        if not os.path.isdir("/proc"):
            self.skipTest("process check needs /proc")
        tmp = tempfile.mkdtemp()
        pid_file = os.path.join(tmp, "child.pid")
        script = os.path.join(tmp, "fake-chrome")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n"
                    f"echo $$ > {pid_file}\n"
                    "exec sleep 30\n")
        os.chmod(script, 0o755)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        class Config:
            def get(self, key=None, default=None, **kwargs):
                return {"browser_binary": "custom", "custom_binary_path": script,
                        "user_data_dir": os.path.join(tmp, "profile"),
                        "port": port, "headless": False,
                        "window_size": "800,600"}.get(key, default)

        started = _time.monotonic()
        with self.assertRaises(TimeoutError):
            asyncio.run(cdp.launch_browser(
                Config(), lambda *a: None, _NoHTTP(),
                deadline=_time.monotonic() + 0.6))
        self.assertLess(_time.monotonic() - started, 5)

        with open(pid_file) as f:
            pid = int(f.read().strip())
        # the launch failed, so the caller must not own a stray process
        deadline = _time.monotonic() + 3
        while _time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            _time.sleep(0.1)
        with self.assertRaises(OSError):
            os.kill(pid, 0)


class ProcessOwnershipTests(unittest.TestCase):
    """B5: a pid is only ours if its command line says so."""

    def test_pidfile_round_trip(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "nested", "browser.pid")
        self.assertTrue(cdp.write_pidfile(path, 4242, 9222, "/tmp/p"))
        self.assertEqual(cdp.read_pidfile(path),
                         {"pid": 4242, "port": 9222, "profile": "/tmp/p"})
        cdp.clear_pidfile(path)
        self.assertIsNone(cdp.read_pidfile(path))

    def test_garbage_pidfile_is_none(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "browser.pid")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(cdp.read_pidfile(path))

    def test_another_process_is_not_ours(self):
        import os
        # this python process is alive, but its command line names neither the
        # profile nor the port, so it must not be adopted
        self.assertFalse(cdp.process_owns_browser(os.getpid(), 9711, "/nonexistent"))

    def test_ownership_needs_the_recorded_profile_and_port(self):
        import asyncio
        import os
        import tempfile

        if not os.path.isdir("/proc"):
            self.skipTest("needs /proc")
        profile = tempfile.mkdtemp()

        async def main():
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c", "exec sleep 10", "sh",
                f"--user-data-dir={profile}", "--remote-debugging-port=9711")
            try:
                self.assertTrue(cdp.process_owns_browser(proc.pid, 9711, profile))
                self.assertFalse(cdp.process_owns_browser(proc.pid, 9712, profile))
                self.assertFalse(cdp.process_owns_browser(proc.pid, 9711, profile + "-x"))
                # stop_pid signals it; reap it ourselves before checking (a
                # child stays a zombie - and a live pid - until wait() runs,
                # which made this test flaky once)
                await cdp.stop_pid(proc.pid)
                await proc.wait()
                with self.assertRaises(OSError):
                    os.kill(proc.pid, 0)
            finally:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass

        asyncio.run(main())


class _FakeWS:
    """A websocket that records sends, or fails them on demand."""

    closed = False

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_json(self, msg):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(msg)


class CDPCommandTests(unittest.TestCase):
    """B21: a command that never completes must not leave a pending future."""

    @staticmethod
    def _client(fail=False):
        client = cdp.CDPClient(None, "", None, lambda *args: None)
        client.ws = _FakeWS(fail=fail)
        return client

    def test_failed_send_leaves_nothing_pending(self):
        import asyncio
        client = self._client(fail=True)
        with self.assertRaises(ConnectionError):
            asyncio.run(client.cmd("Page.enable"))
        self.assertEqual(client._pending, {})

    def test_timeout_reports_and_cleans_up(self):
        import asyncio
        client = self._client()
        with self.assertRaises(TimeoutError) as ctx:
            asyncio.run(client.cmd("Page.captureScreenshot", timeout=0.05))
        self.assertIn("'Page.captureScreenshot'", str(ctx.exception))
        self.assertIn("after 0.1s", str(ctx.exception))   # one decimal, no 14.49999
        self.assertEqual(client._pending, {})

    def test_cancellation_leaves_nothing_pending(self):
        import asyncio
        client = self._client()

        async def main():
            task = asyncio.create_task(client.cmd("Page.enable", timeout=30))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(main())
        self.assertEqual(client._pending, {})

    def test_a_reply_resolves_the_pending_future(self):
        import asyncio
        client = self._client()
        client._id = 0

        async def main():
            task = asyncio.create_task(client.cmd("Page.enable", timeout=2))
            while not client._pending:
                await asyncio.sleep(0.01)
            mid, fut = next(iter(client._pending.items()))
            fut.set_result({"ok": True})
            return await task

        self.assertEqual(asyncio.run(main()), {"ok": True})
        self.assertEqual(client._pending, {})


if __name__ == "__main__":
    unittest.main()
