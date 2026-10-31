"""Page tools: navigation, the accessibility snapshot, pointer and keyboard
input, form filling, screenshots and script evaluation.

A mixin; see tools_session.py for why module.py copies these into Browser.
"""

import asyncio
import base64
import json
import os

import core

from . import page
from .cdp import CDPError
from .util import (normalize_url, grid_fits, capture_fits, first_capture_window,
                   rejected_top_level_await, wrap_await, untrusted,
                   split_chord, checkbox_value)


class PageTools:
    """What the AI does on a page."""

    @core.module.tool
    async def navigate(self, url: str, wait_seconds: int = 8):
        """Navigates the current tab to a URL. Prepends https:// if no scheme is given.

        Args:
            url: The URL to navigate to (e.g. https://example.com).
            wait_seconds: Seconds to wait for the page load event.
        """
        budget = self._budget()
        await self._ensure_connection()
        url = normalize_url(url)
        sid = self._current_session()
        try:
            wait = max(0, int(wait_seconds))
        except (TypeError, ValueError):
            wait = 8
        self._load_event.clear()
        res = await self._cdp.cmd("Page.navigate", {"url": url}, sid)
        if res.get("errorText"):
            return self.result(f"Navigation failed: {res['errorText']}", success=False)
        loaded = True
        window = 0.0
        try:
            # the load wait shares the same deadline: it can only use the time
            # the command above did not, never a number of its own
            window = min(wait, budget.left())
            await asyncio.wait_for(self._load_event.wait(), timeout=window)
        except asyncio.TimeoutError:
            loaded = False
        out = {"url": url, "loaded": loaded}
        if not loaded:
            # omit the key instead of carrying "note": null into every result
            if window < wait:
                out["note"] = ("Page load not seen before the call ran out; it may "
                               "still be loading.")
            else:
                out["note"] = ("Page load not seen within the wait window; it may "
                               "still be loading.")
        return self.result(out)

    @core.module.tool
    async def snapshot(self, max_chars: int = None):
        """Returns the page's accessibility tree as text; interactive elements get refs like @e1. Long snapshots are saved to a file and the path returned.

        Args:
            max_chars: Max characters to return inline (default: snapshot_max_chars setting).
        """
        sid = await self._sid()
        limit = max_chars or int(self.config.get("snapshot_max_chars", default=15000))
        # one big command on a long page: bounded by the tool call, not by a
        # module-private number
        ax = await self._cdp.cmd("Accessibility.getFullAXTree", {}, sid)
        nodes = ax.get("nodes", [])
        if not nodes:
            return self.result("Accessibility tree is empty.", success=False)

        lines, refs = page.walk_ax_tree(nodes)
        self._refs_by_tab[self._current_target] = refs

        title, current_url = "", ""
        try:
            info = await self._cdp.cmd("Runtime.evaluate", {
                "expression": "JSON.stringify({title: document.title, url: location.href})",
                "returnByValue": True,
            }, sid)
            parsed_info = json.loads(info.get("result", {}).get("value", "{}"))
            title = parsed_info.get("title", "")
            current_url = parsed_info.get("url", "")
        except Exception:
            pass

        body = "\n".join(lines)
        snapshot = f"URL: {current_url}\nTitle: {title}\n\n{body}"
        truncated = False
        full_path = None
        if len(snapshot) > limit:
            truncated = True
            if self.config.get("save_full_snapshots", True):
                full_path = os.path.join(
                    self._snapshots_dir, page.new_filename("snapshot", "txt"))
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(snapshot)
            snapshot = snapshot[:limit]
            snapshot += f"\n\n[truncated - full snapshot saved to {full_path}]" if full_path else "\n\n[truncated]"

        return self.result(untrusted({
            "snapshot": snapshot,
            "url": current_url,
            "title": title,
            "truncated": truncated,
            "full_path": full_path,
        }, self._page_source()))

    @core.module.tool
    async def click(self, target: str):
        """Clicks an element by ref (@e1) or CSS selector; scrolls it into view and sends real mouse events.

        Args:
            target: Element ref (e.g. @e3) or CSS selector (e.g. '#submit').
        """
        sid = await self._sid()
        handle = await page.resolve_target(self._cdp, sid, self._tab_refs(), target)
        try:
            await self._cdp.cmd("DOM.scrollIntoViewIfNeeded", handle, sid)
        except Exception:
            pass
        # The mouse click lands on the box centre, so both the box and where it
        # sits decide whether a click can land at all: hidden elements have no
        # box, and off-canvas ones sit outside the viewport.
        cx = cy = None
        try:
            box = await self._cdp.cmd("DOM.getBoxModel", handle, sid)
            quad = box.get("model", {}).get("border", [])
        except Exception:
            quad = []
        if len(quad) >= 8:
            xs = [quad[i] for i in range(0, 8, 2)]
            ys = [quad[i] for i in range(1, 8, 2)]
            cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)

        reason = "no layout box (hidden?)"
        if cx is not None:
            size = await page.viewport_size(self._cdp, sid)
            if size and 0 <= cx < size[0] and 0 <= cy < size[1]:
                try:
                    for etype in ("mousePressed", "mouseReleased"):
                        await self._cdp.cmd("Input.dispatchMouseEvent", {
                            "type": etype, "x": cx, "y": cy,
                            "button": "left", "clickCount": 1,
                        }, sid)
                    return self.result(f"Clicked {target} at ({cx}, {cy}).")
                except Exception:
                    reason = f"mouse click failed at ({cx}, {cy})"
            else:
                reason = f"off-screen at ({cx}, {cy})"

        # Fallback: JS click, which bypasses hit-testing - say when it is used
        object_id = await page.resolve_object_id(self._cdp, handle, sid)
        if not object_id:
            return self.result(f"Could not resolve element '{target}'.", success=False)
        await page.call_on(self._cdp, object_id, "function(){ this.click(); }", sid)
        return self.result(f"Clicked {target} via JS - {reason}.")

    @core.module.tool
    async def type_text(self, target: str, text: str):
        """Types text into an element (input/textarea/contenteditable): focuses, clears, then types.

        On a <select>, text picks the option whose label or value matches.
        On a checkbox or radio, text sets it: true/yes/on or false/no/off.

        Args:
            target: Element ref (e.g. @e5) or CSS selector.
            text: The text to type, the option to select, or true/false for a checkbox.
        """
        sid = await self._sid()
        ok, message = await self._fill_field(sid, target, text)
        return self.result(message, success=ok)

    @core.module.tool
    async def fill_form(self, fields: list = None):
        """Fills several form fields in one call; stops at nothing, reports each by name.

        Each item is {"target": ref or selector, "text": value}; a checkbox or
        radio takes true/false, a <select> the option's label or value.

        Args:
            fields: List of {"target": ..., "text": ...} objects, in page order.
        """
        sid = await self._sid()
        items = _field_items(fields)
        if items is None:
            return self.result(
                "Pass fields as a list of {target, text} objects.", success=False)
        results = []
        failed = 0
        for item in items:
            target = str(item.get("target") or item.get("ref")
                         or item.get("selector") or "")
            text = item.get("text", item.get("value", ""))
            if not target:
                results.append({"target": "", "ok": False, "message": "No target."})
                failed += 1
                continue
            try:
                ok, message = await self._fill_field(sid, target, text)
            except (CDPError, TimeoutError, ConnectionError, RuntimeError,
                    OSError) as exc:
                # one bad field must not hide the rest of the form
                ok, message = False, str(exc) or exc.__class__.__name__
            results.append({"target": target, "ok": ok, "message": message})
            if not ok:
                failed += 1
        data = {"filled": len(results) - failed, "failed": failed,
                "results": results}
        return self.result(data, success=failed == 0)

    @core.module.tool
    async def press(self, key: str, modifiers: str = None):
        """Presses a keyboard key or a chord in the current tab (enter, tab, escape, arrows, f5, Control+A).

        Args:
            key: Key name (enter, tab, escape, arrows, a-z, 0-9, f5) or a chord like 'Control+A'.
            modifiers: Modifiers held while pressing (e.g. 'control shift'); a chord in `key` works too.
        """
        sid = await self._sid()
        try:
            held_names, name = split_chord(key, modifiers)
        except ValueError as e:
            return self.result(str(e), success=False)
        if name.lower() == "f5":
            if held_names:
                return self.result("f5 cannot be combined with modifiers.", success=False)
            await self._cdp.cmd("Page.reload", {}, sid)
            return self.result("Reloaded the page (F5).")
        definition = page.key_definition(name)
        if definition is None:
            return self.result(
                f"Unsupported key '{name}'. Supported: a-z, 0-9, enter, tab, "
                "escape, arrows, f5.", success=False,
            )
        vk, key_str, code = definition
        # character keys need the `text` payload on keyDown or Chrome never
        # reaches the keypress stage -> no implicit form submit on Enter, no
        # space insertion. A Control/Alt/Meta chord inserts no character.
        mask = 0
        for mod in held_names:
            mask |= page.MODIFIER_KEYS[mod][3]
        if len(key_str) == 1 and key_str.isalpha() and mask & (1 | 2 | 4):
            # a Control/Alt/Meta chord reports the lower-case key, as Chrome
            # does for a real Control+A press
            key_str = key_str.lower()
        text = None
        if not mask & (1 | 2 | 4):
            if key_str == "Enter":
                text = "\r"
            elif len(key_str) == 1:
                text = key_str
        live = 0
        held = []
        for mod in held_names:
            mkey, mcode, mvk, mbit = page.MODIFIER_KEYS[mod]
            live |= mbit
            held.append((mkey, mcode, mvk, mbit))
            await self._dispatch_key(sid, "keyDown", mkey, mcode, mvk, live)
        try:
            await self._dispatch_key(sid, "keyDown", key_str, code, vk, mask, text)
            await self._dispatch_key(sid, "keyUp", key_str, code, vk, mask)
        finally:
            # release in reverse order, each event carrying only the modifiers
            # still held at that moment
            for mkey, mcode, mvk, mbit in reversed(held):
                live &= ~mbit
                await self._dispatch_key(sid, "keyUp", mkey, mcode, mvk, live)
        prefix = "+".join(mod.capitalize() for mod in held_names)
        shown = "Space" if key_str == " " else name
        return self.result(f"Pressed {prefix + '+' if prefix else ''}{shown}.")

    @core.module.tool
    async def scroll(self, direction: str = "down", amount: int = 300):
        """Scrolls the page in the given direction.

        Args:
            direction: 'down' or 'up'.
            amount: Pixels to scroll (default 300).
        """
        sid = await self._sid()
        delta = int(amount)
        if direction == "up":
            delta = -delta
        await self._cdp.cmd("Runtime.evaluate", {
            "expression": f"window.scrollBy({{top: {delta}, behavior: 'auto'}})",
            "returnByValue": True,
        }, sid)
        return self.result(f"Scrolled {direction} by {abs(delta)}px.")

    @core.module.tool
    async def back(self):
        """Navigates back to the previous page in the current tab's history."""
        sid = await self._sid()
        hist = await self._cdp.cmd("Page.getNavigationHistory", {}, sid)
        idx = hist.get("currentIndex", 0)
        entries = hist.get("entries", [])
        if idx <= 0 or not entries:
            return self.result("No previous page in history.", success=False)
        entry_id = entries[idx - 1].get("id")
        await self._cdp.cmd("Page.navigateToHistoryEntry", {"entryId": entry_id}, sid)
        return self.result("Navigated back.")

    @core.module.tool
    async def screenshot(self, full_page: bool = False, include_image: bool = True, grid=None,
                         format: str = "png", quality: int = 80):
        """Takes a screenshot of the current tab (PNG/JPEG) and returns the path plus an inline image.

        Args:
            full_page: Capture the whole page instead of the viewport. Captures larger than full_page_max_pixels are refused with their real size.
            include_image: Deliver the screenshot to the model as an image message.
            grid: Labeled grid drawn on the image. True = 100px spacing, int = custom px. Requires format='png'.
            format: 'png' (default) or 'jpeg'.
            quality: JPEG quality 0-100 (default 80).
        """
        # taken BEFORE anything else: launching Chrome (1-3s), the metrics read,
        # the capture and the grid all have to fit inside core.tool_timeout
        budget = self._budget()
        sid = await self._sid()
        fmt = "jpeg" if format in ("jpeg", "jpg") else ("png" if format == "png" else None)
        if fmt is None:
            return self.result(
                f"Unsupported screenshot format '{format}'. Use 'png' or 'jpeg'.", success=False)
        try:
            quality = max(0, min(100, int(quality)))
        except (TypeError, ValueError):
            quality = 80
        grid_spacing = page.normalize_grid(grid)
        if grid_spacing and fmt != "png":
            # 1px lines do not survive JPEG, and decoding JPEG would need an
            # imaging library: ask for png instead of silently changing format
            return self.result(
                "A grid needs format='png' (JPEG is lossy).", success=False,
            )

        # metrics BEFORE the capture: they decide whether a full-page capture
        # is feasible, in the raster size Chrome will actually paint
        metrics = await page.viewport_metrics(self._cdp, sid) or {}
        shot_w = int(metrics.get("docWpx") or 0)
        shot_h = int(metrics.get("docHpx") or 0)
        max_px = self._full_page_max_pixels()
        if full_page and not metrics:
            # No metrics means the cap below cannot be checked. Refusing is the
            # honest answer: an unmeasurably large page would spend the whole
            # tool timeout and still return nothing (viewport captures don't
            # depend on the cap, so they stay allowed).
            return self.result(
                "Cannot read the page size, so full_page is refused. Use a "
                "viewport capture.", success=False,
            )
        fits, mpx = capture_fits(shot_w, shot_h, max_px)
        if full_page and not fits:
            # captureBeyondViewport paints the whole page whatever `clip` says,
            # so an oversized capture only stalls into a timeout: refuse up
            # front, with the real size.
            return self.result(
                f"Page is {mpx:.1f} Mpx; full_page is capped at "
                f"{max_px // 1_000_000} Mpx (full_page_max_pixels). Use viewport "
                f"captures or raise it.", success=False,
            )
        scale = float(metrics.get("devicePixelRatio") or 1.0)
        # `region` is in image pixels, the grid labels in document pixels
        doc_origin_x = int(metrics.get("scrollX") or 0)
        doc_origin_y = int(metrics.get("scrollY") or 0)
        origin_x = 0 if full_page else int(round(doc_origin_x * scale))
        origin_y = 0 if full_page else int(round(doc_origin_y * scale))

        # Read before the capture: this only explains a timeout, and the answer
        # must already be there when the capture gives up. None = could not
        # read it (a stalled page hangs this evaluate too), so then no claim is
        # made about the cause.
        background = await page.visibility_state(self._cdp, sid, timeout=1.0) == "hidden"

        # a full-page capture gets part of the call, not all of it: if
        # Chrome stalls on this document, the rest of the time is still usable
        window = first_capture_window(budget.left(), full_page)
        fallback_note = None
        try:
            path, raw = await page.capture_screenshot(
                self._cdp, sid, self._screenshots_dir,
                full_page=full_page, fmt=fmt, quality=quality, timeout=window)
        except TimeoutError as e:
            self.log("browser", f"screenshot: {core.detail_error(e)}")
            if not full_page:
                return self.result(
                    self._capture_failed_text(background=background),
                    success=False,
                )
            # Chrome did not answer the full-page capture: use what the call
            # still has and return the viewport, saying exactly that
            fallback_note = ("Full-page capture timed out; this is the viewport. "
                             "Reload the tab (f5) if it repeats.")
            self.log("browser", fallback_note)
            try:
                path, raw = await page.capture_screenshot(
                    self._cdp, sid, self._screenshots_dir,
                    full_page=False, fmt=fmt, quality=quality)
            except TimeoutError as e2:
                self.log("browser", f"screenshot: {core.detail_error(e2)}")
                return self.result(
                    self._capture_failed_text(background=background),
                    success=False,
                )
            # what was written is a viewport capture: region and `full_page`
            # have to describe the image, not the request
            full_page = False
            origin_x = int(round(doc_origin_x * scale))
            origin_y = int(round(doc_origin_y * scale))

        # parsed dimensions first: they feed both the grid cost estimate and the
        # region hint, so neither can drift from the pixels actually written
        # (clipping, Chrome rounding). draw_grid never changes the dimensions.
        px_w, px_h = page.image_size(raw, fmt) or (0, 0)

        # grid: drawn onto the saved PNG (no page mutation, no canvas limit).
        # Pure CPU with no command timeout watching it, so it only runs when it
        # still fits in this call (~0.85 s per megapixel).
        grid_note = None
        if grid_spacing:
            fits, need = grid_fits(px_w, px_h, budget.left())
            if not fits:
                grid_note = (f"Grid not drawn (needs ~{need:.1f}s, "
                             f"{budget.left():.1f}s left). Plain capture attached.")
                self.log("browser", grid_note)
            else:
                try:
                    raw = page.draw_grid(raw, grid_spacing, doc_origin_x,
                                         doc_origin_y, scale=scale)
                    with open(path, "wb") as f:
                        f.write(raw)
                except Exception as e:
                    grid_note = f"grid not drawn: {core.detail_error(e)}"
                    self.log("browser", grid_note)

        # region describes the IMAGE that was actually written: the parsed
        # dimensions win over the pre-capture metrics
        if full_page:
            region = {
                "x": 0, "y": 0,
                "width": px_w or shot_w or 0,
                "height": px_h or shot_h or 0,
            }
        else:
            region = {
                "x": origin_x, "y": origin_y,
                "width": px_w or int(round(int(metrics.get("innerWidth") or 0) * scale)),
                "height": px_h or int(round(int(metrics.get("innerHeight") or 0) * scale)),
            }
        region = {key: int(value or 0) for key, value in region.items()}
        # the tool result stays plain text (path + geometry): the image can't
        # travel in a `tool` message, so it is added to context as a user
        # message instead - the inference API only accepts images in user
        # turns. adding it directly avoids channel.send() inside a tool call,
        # which deadlocks the channel's stream chain.
        if scale == 1.0:
            hint = ("Screenshot image is attached. Coordinates: doc_x = shot_x + region.x, "
                    "doc_y = shot_y + region.y.")
        else:
            hint = (f"Screenshot image is attached. The capture has {scale:g} image px per "
                    f"document px: doc_x = (shot_x + region.x) / {scale:g}, "
                    f"doc_y = (shot_y + region.y) / {scale:g}. `region` itself is in image px.")
        if grid_spacing and not grid_note:
            hint += " The grid labels are document coordinates."
        meta = {
            "path": path,
            "file": os.path.basename(path),
            "bytes": len(raw),
            "full_page": bool(full_page),
            "region": region,
            "format": fmt,
            "hint": hint,
        }
        if fmt == "jpeg":
            meta["quality"] = quality
        if scale != 1.0:
            meta["scale"] = scale
        if fallback_note:
            meta["note"] = fallback_note
        if grid_spacing:
            meta["grid"] = {"spacing": grid_spacing, "applied": grid_note is None}
            if grid_note:
                meta["grid"]["reason"] = grid_note

        if include_image:
            mime = "image/jpeg" if fmt == "jpeg" else "image/png"
            await self.channel.context.chat.messages.add({
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,"
                                         + base64.b64encode(raw).decode("ascii")},
                }],
                "_metadata": {"internal": "screenshot", "path": path},
            })

        return self.result(meta)

    @core.module.tool
    async def evaluate(self, expression: str):
        """Evaluates JavaScript in the current tab and returns the result.

        A top-level `await` is supported: the expression is retried in Chrome's
        REPL mode, which does not await a promise left as the last statement.

        Args:
            expression: JavaScript expression to evaluate (e.g. 'document.title').
        """
        sid = await self._sid()
        # one command: it gets the whole call, so a slow script is bounded by
        # core.tool_timeout and nothing else
        params = {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        }
        res = await self._cdp.cmd("Runtime.evaluate", params, sid)
        if rejected_top_level_await(res.get("exceptionDetails")):
            # a script has no top-level await, but Chrome's REPL mode does:
            # the same text is retried as a console statement, so a statement
            # body ("await p; 2") runs too and the last statement's value comes
            # back. REPL mode ignores awaitPromise, so a promise left as the
            # last statement is not awaited - why it is only used for a retry.
            repl = dict(params, replMode=True)
            try:
                res = await self._cdp.cmd("Runtime.evaluate", repl, sid)
            except CDPError as e:
                if "replMode" not in str(e):
                    raise
                # older build without REPL mode: wrap the expression instead;
                # that shape only accepts a single expression
                wrapped = dict(params, expression=wrap_await(expression))
                res = await self._cdp.cmd("Runtime.evaluate", wrapped, sid)
        if res.get("exceptionDetails"):
            exc = res["exceptionDetails"].get("exception", {})
            return self.result(
                exc.get("description") or res["exceptionDetails"].get("text", "JS error"),
                success=False,
            )
        result = res.get("result", {})
        if "value" in result:
            value = result["value"]
            # a string is page prose the model may read as text, so it is
            # fenced like any other page content; scalars and objects are not
            if isinstance(value, str):
                return self.result(untrusted(value, self._page_source()))
            return self.result(value)
        return self.result(result.get("description", "undefined"))

    async def _fill_field(self, sid, target, text):
        """Put `text` into one field; returns (ok, message).

        A field is an input/textarea/contenteditable (typing), a <select>
        (option pick) or a checkbox/radio (checked state). type_text and
        fill_form both go through here, so they report a field the same way.
        """
        handle = await page.resolve_target(self._cdp, sid, self._tab_refs(), target)
        object_id = await page.resolve_object_id(self._cdp, handle, sid)
        if not object_id:
            return False, f"Could not resolve element '{target}'."
        if await page.tag_name(self._cdp, object_id, sid) == "SELECT":
            return await self._select_target_option(sid, object_id, target, text)
        if await page.input_type(self._cdp, object_id, sid) in ("checkbox", "radio"):
            wanted = checkbox_value(text)
            if wanted is None:
                return False, f"Use true or false for {target}."
            state = await page.set_checked(self._cdp, object_id, wanted, sid)
            if not state or state.get("checked") != wanted:
                return False, f"Could not set {target}."
            return True, f"{'Checked' if wanted else 'Unchecked'} {target}."
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        # contenteditable has no select(): select the contents with a range, so
        # the Backspace below deletes the old text instead of leaving it in
        # front of what gets typed.
        await page.call_on(
            self._cdp,
            object_id,
            "function(){ this.scrollIntoView({block:'center'}); this.focus();"
            " if (typeof this.select === 'function') { this.select(); return; }"
            " if (this.isContentEditable) {"
            "   var r = document.createRange(); r.selectNodeContents(this);"
            "   var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);"
            " } }",
            sid,
        )
        # clear selected content
        for ktype in ("keyDown", "keyUp"):
            await self._cdp.cmd("Input.dispatchKeyEvent", {
                "type": ktype, "key": "Backspace", "code": "Backspace",
                "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
            }, sid)
        await self._cdp.cmd("Input.insertText", {"text": text}, sid)
        return True, f"Typed {len(text)} characters into {target}."

    async def _select_target_option(self, sid, object_id, target, wanted):
        """Pick a <select> option by label or value; (ok, message) for _fill_field."""
        picked = await page.select_option(self._cdp, object_id, wanted, sid)
        if not picked:
            return False, f"Could not set {target}."
        if picked.get("ok"):
            return True, f"Selected '{picked.get('text')}' in {target}."
        labels = [(label[:12] + "\u2026") if len(label) > 12 else label
                  for label in (picked.get("labels") or [])[:3]]
        options = ", ".join(labels) or "none"
        return False, f"No option '{wanted}' in {target}. Options: {options}"

    async def _dispatch_key(self, sid, ktype, key, code, vk, modifiers, text=None):
        """One Input.dispatchKeyEvent; `text` only on a character key's keyDown."""
        params = {"type": ktype, "key": key, "code": code,
                  "windowsVirtualKeyCode": vk,
                  "nativeVirtualKeyCode": vk,
                  "modifiers": modifiers}
        if text is not None:
            params["text"] = text
            params["unmodifiedText"] = text
        await self._cdp.cmd("Input.dispatchKeyEvent", params, sid)


def _field_items(fields):
    """The {target, text} items of a fill_form argument, or None when unusable.

    A JSON string is accepted because a tool argument may arrive as text;
    "ref" and "selector" are read as target names, "value" as the text.
    """
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except (TypeError, ValueError):
            return None
    if isinstance(fields, dict):
        fields = [fields]
    if not isinstance(fields, (list, tuple)):
        return None
    items = []
    for item in list(fields)[:50]:
        if isinstance(item, dict):
            items.append(item)
        else:
            items.append({"text": item})
    return items

