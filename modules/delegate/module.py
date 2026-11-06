"""delegate — hand a self-contained task to an AI session with its own context.

Unlike the `subagents` module, a delegated run is *persisted*: its transcript
(reasoning, assistant text, every tool call and every tool result) is stored
under data/delegate/history/<task_id>.json. That makes two things possible
that subagents cannot do:

  * the user can watch the whole process live in the delegate panel instead of
    having to trust the final answer;
  * the same conversation can be continued later with delegate_send(task_id,
    ...), replaying the full stored context instead of re-summarising it.

A run normally blocks the calling turn: the delegate's final message is returned
as the tool result. `background=True` returns at once instead (`{task_id,
status: "running"}`); a run that is still silent when the caller's tool deadline
approaches detaches by itself so it is not cut. Background runs are capped
(`max_background_tasks`, default 1) and must use the chat's current model: core
keeps its model pointer on the run's model for the run's whole life, so another
model there would leak into the chat's own requests. A background request that
breaks either rule is refused with an explanation.

Running the delegate's tool loop through channel.tc_manager.process(context=...)
accepts three core side effects for v1 (see runner.py): the main chat can be
auto-compressed mid-run, the delegate's API usage is counted against the main
context, and a steering message can be flushed into the main context mid-run.
"""

import asyncio
import contextvars
import inspect
import time

import core
import channels.webui.api as webui
import channels.webui.chat_listing as chat_listing

from . import policy
from .store import DelegateStore, repair
from .events import EventBus
from .runner import DelegateRunner, DelegateCancelled

TOOL_NEW = "delegate_new"
TOOL_SEND = "delegate_send"
TOOL_LIST = "delegate_list"
TOOL_READ = "delegate_read"

DELEGATE_PREAMBLE = (
    "\n\n# Delegation\n"
    "You are running as a delegated session with your own context, working on a task "
    "handed to you by the main assistant. You cannot delegate further. Do the task "
    "and finish with a final message that answers it."
)


class Delegate(core.module.Module):
    """Lets the AI hand self-contained tasks to a separate AI session whose full
    transcript is saved, inspectable in a docked panel and continuable by id."""

    category = "core"

    settings = {
        "enable_webui_extension": {
            "default": True,
            "description": "Show the delegate button in the chat header and the docked panel it opens.",
        },
        "max_rounds": {
            "type": "number",
            "default": 40,
            "description": "Tool rounds a single delegated run may use before it is aborted.",
        },
        "max_background_tasks": {
            "type": "number",
            "default": 1,
            "min": 1,
            "max": 10,
            "description": "How many delegated tasks may run in the background at once (1 = one). A background request beyond this is refused with a message to run it in the foreground; a run that would detach at the tool deadline waits for a free slot instead. Background runs are limited to the chat's current model.",
        },
        "font_scale": {
            "type": "number",
            "default": 94,
            "min": 70,
            "max": 130,
            "step": 1,
            "description": "Font size of the delegate panel, as a percentage of the chat's text (94 = the chat size minus ~1px).",
        },
        "announce_completion": {
            "type": "select",
            "default": "Push",
            "description": "What happens when a background or detached delegated task finishes. Inline runs are unaffected: their answer was already returned.",
            "options": {
                "Off": "Do nothing. The task just shows as done in the panel.",
                "Push": "Add a one-line notice to the chat. The model sees it at its next turn and can call delegate_read for the answer. Never starts a turn.",
                "Wake": "Deliver the notice where a steering message goes: the turn that is running reads it at its next tool round, and if that turn has already ended the notice is answered as a fresh turn. When the chat is idle the notice simply waits for the next turn. Costs tokens when it triggers a fresh turn.",
            },
        },
        "keep_reasoning_in_context": {
            "default": True,
            "description": "Send a delegated task's earlier reasoning back with its next message (continuations and tool rounds). Keeps the model's prompt cache warm, so a continued task starts fast; off re-sends only conclusions and saves context.",
        },
    }

    async def on_ready(self):
        self.store = DelegateStore()
        self.bus = EventBus()
        self.runner = DelegateRunner(self)
        self._runs = {}               # task_id -> the live run (foreground or background)
        self._bg_runs = {}            # task_id -> runs holding a background slot
        self._background = set()      # every live run/announce task: keep a reference
        self._depth = contextvars.ContextVar("delegate_depth", default=0)
        self.store.mark_stale()

    async def on_shutdown(self):
        # a module reload must not leave a detached run writing into a dead store
        for task in list(self._background):
            task.cancel()
        self._background.clear()

    @staticmethod
    def _as_bool(value):
        """tool arguments arrive as JSON: tolerate real bools and the usual strings"""
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in ("1", "true", "yes", "on", "background")

    def _max_background(self):
        """how many delegated runs may sit in the background at once"""
        try:
            return max(1, min(10, int(self.config.get("max_background_tasks", 1) or 1)))
        except (TypeError, ValueError):
            return 1

    def _current_model(self):
        try:
            return self.manager.API.get_model()
        except Exception:
            return ""

    def _start_refusal(self, background, effective_model):
        """the start policy (see policy.start_refusal): None or an error message"""
        return policy.start_refusal(
            background,
            self._max_background(),
            self._bg_runs,
            effective_model,
            self._current_model(),
        )

    def _inside_run(self):
        """True when the call comes from inside a delegated run.

        A delegate inherits the full tool set, `delegate_new`/`delegate_send`
        included, so nesting has to be refused by the tools themselves; the
        context variable is set around `runner.run()` and every tool call the
        run makes is awaited inside that context.
        """
        return self._depth.get() > 0

    def _call_chain(self):
        """debug: the immediate Python callers of a tool call.

        A native call arrives as `_run_tool` -> the method. A call routed
        through core's dynamic-loading bridge shows `tools_call` in the chain,
        and that path has no `bypass_timeout` - so the whole run is awaited
        under `core.tool_timeout` and gets cut when it runs longer.
        """
        try:
            return " <- ".join(frame.function for frame in inspect.stack()[1:6])
        except Exception:
            return "?"

    # ------------------------------------------------------------------ tools
    @core.module.tool(bypass_timeout=True)
    async def new(self, prompt: str, title: str = "", model: str = "", background: bool = False) -> dict:
        """Delegate a self-contained task to a separate AI session with its own context.

        The delegate sees this chat's system prompt but none of its history, so the
        prompt must stand on its own. It has the same tools you do and its whole
        transcript (reasoning, tool calls, tool results) is saved under the returned
        task_id: the user can watch it live in the delegate panel, and you can
        continue the same conversation later with delegate_send(task_id, ...).

        This call blocks until the delegate finishes and returns its final answer, unless `background=True`, which returns at once with status running.

        Args:
            prompt: the task, written self-contained (the delegate cannot see this chat).
            title: short display label for the panel and task list. Derived from the prompt when empty.
            model: model to run the delegate on. Defaults to the current model; must be one of the available models.
            background: return immediately with status running instead of waiting for the run (default False). The run continues in the background; check it with delegate_list or read it in the panel. Refused when no background slot is free or when `model` is not the chat's current model - the error says which, and running it in the foreground (omit background) is always possible.

        Returns:
            {task_id, status, model, rounds, result} - or {task_id, status: "running", background: true} when background=True
        """
        self.log("delegate", f"new: called via {self._call_chain()}")
        background = self._as_bool(background)

        prompt = (prompt or "").strip()
        if not prompt:
            return self.result("prompt must not be blank", success=False)

        if self._inside_run():
            return self.result(
                "a delegated run cannot delegate: nested delegation is not allowed",
                success=False,
            )

        model_name, error = await self._resolve_model(model)
        if error:
            return self.result(error, success=False)

        refusal = self._start_refusal(background, model_name or self._current_model())
        if refusal:
            return self.result(refusal, success=False)

        chat = self.channel.context.chat
        task = self.store.create(
            title=(title or "").strip() or self._derive_title(prompt),
            model=model_name or self.manager.API.get_model(),
            channel_name=self.channel.name,
            chat_id=chat.get("id"),
        )

        system_prompt = await self._system_prompt()
        history = [{"role": "system", "content": system_prompt}] if system_prompt else []

        return await self._execute_or_detach(task, prompt, model_name, history, background)

    @core.module.tool(bypass_timeout=True)
    async def send(self, task_id: str, prompt: str, model: str = "", background: bool = False) -> dict:
        """Continue a delegated task: a follow-up message in the same conversation.

        The delegate resumes with its full stored context (every message and tool
        result is replayed; the stored reasoning is kept for display only), so this
        is a normal continuation, not a re-summary. Use it to ask why the delegate
        did something, or to hand it another step.

        Args:
            task_id: id of the task, as returned by delegate_new (for example "01AB3F7G").
            prompt: the follow-up request.
            model: switch the task to another model. Defaults to the model the task already uses.
            background: return immediately with status running instead of waiting for the run (default False); the run continues in the background. Refused when no background slot is free or when the task's model is not the chat's current one - the error says which; run it in the foreground (omit background) instead, or switch the chat to that model first.

        Returns:
            {task_id, status, model, rounds, result} - or {task_id, status: "running", background: true} when background=True
        """
        background = self._as_bool(background)
        prompt = (prompt or "").strip()
        if not prompt:
            return self.result("prompt must not be blank", success=False)

        task = self.store.find(task_id)
        if task is None:
            return self.result(f"no delegate task with id '{task_id}'", success=False)

        identifier = task["id"]
        if task.get("status") == "running" or identifier in self._runs:
            return self.result(f"delegate task {identifier} is still running", success=False)

        if self._inside_run():
            return self.result(
                "a delegated run cannot continue another task: nested delegation is not allowed",
                success=False,
            )

        model_name, error = await self._resolve_model(model)
        if error:
            return self.result(error, success=False)

        effective = model_name or task.get("model") or self._current_model()
        refusal = self._start_refusal(background, effective)
        if refusal:
            return self.result(refusal, success=False)

        if model_name and model_name.lower() != str(task.get("model") or "").lower():
            self.store.update(identifier, model=model_name)
            task = self.store.find(identifier)

        started = time.monotonic()
        self.log(
            "delegate",
            f"send {identifier}: accepted (status={task.get('status')}, "
            f"rounds={task.get('rounds')}) via {self._call_chain()}",
        )

        history, dropped = repair(self.store.load(identifier))
        if dropped:
            self.store.save(identifier, history)

        self.log(
            "delegate",
            f"send {identifier}: history loaded in {time.monotonic() - started:.2f}s "
            f"({len(history)} messages, dropped={dropped})",
        )
        if not any(message.get("role") == "system" for message in history):
            system_prompt = await self._system_prompt()
            if system_prompt:
                history.insert(0, {"role": "system", "content": system_prompt})
                self.store.save(identifier, history)

        self.store.update(identifier, status="running", last_error=None)

        return await self._execute_or_detach(
            task, prompt, model_name or task.get("model") or None, history, background
        )

    @core.module.tool()
    async def list(self, query: str = "", status: str = "", limit: int = 20) -> dict:
        """List the stored delegated tasks, newest first.

        This reads the task index on disk, so it also works when the ids from
        earlier in this conversation were compressed away: use it to recover a
        task id instead of asking the user for one.

        Args:
            query: text matched against the title, id and model (empty = every task).
            status: only tasks with this status - "running", "done", "error" or "interrupted" (empty = any).
            limit: how many tasks to return, newest first (default 20, max 100).

        Returns:
            {count, tasks: [{task_id, title, status, model, rounds, updated, error}], truncated}
        """
        query = (query or "").strip().lower()
        status = (status or "").strip().lower()
        try:
            limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            limit = 20

        matched = []
        for task in self.store.all():   # already sorted newest first
            if status and str(task.get("status") or "").lower() != status:
                continue
            if query:
                haystack = " ".join(
                    str(task.get(key) or "") for key in ("title", "id", "model")
                ).lower()
                if query not in haystack:
                    continue

            matched.append({
                "task_id": task.get("id", ""),
                "title": task.get("title") or task.get("id", ""),
                "status": task.get("status") or "",
                "model": task.get("model") or "",
                "rounds": task.get("rounds") or 0,
                "updated": task.get("updated") or "",
                "error": task.get("last_error") or "",
            })

        return self.result({
            "count": len(matched),
            "tasks": matched[:limit],
            "truncated": len(matched) > limit,
        })

    @core.module.tool()
    async def read(self, task_id: str, chars: int = 4000) -> dict:
        """Read a delegated task's final answer (its last assistant message).

        Use this to collect a run that was started with background=True or was
        detached from its tool call - no panel and no file access needed. It also
        reports the task's current status, so it can be polled while it runs.

        Args:
            task_id: id of the task, as returned by delegate_new / delegate_list.
            chars: return only the last N characters of the answer (default 4000, max 40000); 0 returns it whole.

        Returns:
            {task_id, status, model, rounds, result, truncated}
        """
        task = self.store.find(task_id)
        if task is None:
            return self.result(f"no delegate task with id '{task_id}'", success=False)

        messages, dropped = repair(self.store.load(task["id"]))
        answer = ""
        for message in reversed(messages):
            if message.get("role") == "assistant" and str(message.get("content") or "").strip():
                answer = str(message.get("content"))
                break

        try:
            limit = int(chars)
        except (TypeError, ValueError):
            limit = 4000
        limit = max(0, min(40000, limit))

        truncated = False
        if limit and len(answer) > limit:
            answer = answer[-limit:]
            truncated = True

        return self.result({
            "task_id": task["id"],
            "status": task.get("status") or "",
            "model": task.get("model") or "",
            "rounds": task.get("rounds") or 0,
            "result": answer,
            "truncated": truncated,
            "dropped": dropped,
        })

    # ------------------------------------------------------------- execution
    def _timeout_limit(self):
        """the deadline core will put on this tool call (it reads the same value)"""
        try:
            return float(core.config.get("core", "tool_timeout", default=10.0))
        except Exception:
            return 10.0

    async def _execute_or_detach(self, task, prompt, model, history, background=False):
        task_id = task["id"]

        # authoritative start policy: `new`/`send` pre-check it for a clean error,
        # but the slot is claimed here, synchronously, so two starts racing for the
        # same last slot cannot both win
        refusal = self._start_refusal(background, model or self._current_model())
        if refusal:
            self.log("delegate", f"task {task_id} not started: {refusal}")
            self.store.update(task_id, status="error", last_error=refusal)
            return self.result(refusal, success=False)

        work = asyncio.create_task(self._execute(task, prompt, model, history))
        self._runs[task_id] = work
        self._background.add(work)
        work.add_done_callback(self._background.discard)
        work.add_done_callback(lambda _done, tid=task_id: self._runs.pop(tid, None))

        if background:
            self._bg_runs[task_id] = work
            work.add_done_callback(lambda _done, tid=task_id: self._bg_runs.pop(tid, None))
            self.log("delegate", f"task {task_id} started in the background; returning at once")
            announce = asyncio.create_task(self._announce(task_id, work))
            self._background.add(announce)
            announce.add_done_callback(self._background.discard)
            return self.result({
                "task_id": task_id,
                "status": "running",
                "background": True,
                "note": (
                    "started in the background; it keeps running and the panel shows it "
                    "live. Check it with delegate_list, or open the task."
                ),
            })

        # Block normally, and keep blocking through the caller's deadline while
        # the run is showing tool activity - a tool-active run is not cut in
        # practice. Detach only when there has been no tool call in the last
        # second as the deadline arrives, one second early, so the tool call
        # returns `running` instead of being cut - and only while a background
        # slot is free. The cap is hard: with no slot the call keeps holding and
        # re-checks every second, so it can take the moment one frees up.
        limit = self._timeout_limit()
        deadline = time.monotonic() + max(2.0, limit - 1.0)

        while True:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    return await asyncio.wait_for(asyncio.shield(work), timeout=remaining)
                except asyncio.TimeoutError:
                    pass        # the slice elapsed: is the run still active?

            quiet = self.runner.seconds_since_tool()
            slots = policy.slots_left(self._max_background(), self._bg_runs)
            if policy.should_detach(quiet, slots):
                break       # silent run and a free slot: detach before the deadline
            deadline = time.monotonic() + 1.0   # tool in flight / no slot: hold

        self.log(
            "delegate",
            f"task {task_id} silent at the tool deadline; detached from the tool call",
        )
        self._bg_runs[task_id] = work
        work.add_done_callback(lambda _done, tid=task_id: self._bg_runs.pop(tid, None))
        announce = asyncio.create_task(self._announce(task_id, work))
        self._background.add(announce)
        announce.add_done_callback(self._background.discard)
        return self.result({
            "task_id": task_id,
            "status": "running",
            "detached": True,
            "note": (
                "the run is still going; it keeps running in the background and the "
                "panel shows it live. Use delegate_list to check it, or open the task; "
                "delegate_send on it is refused until it finishes."
            ),
        })

    def _announce_mode(self):
        """`announce_completion`: Off / Push / Wake (see the module settings)"""
        try:
            # default= keyword MUST be a keyword here: ModuleConfig.get() treats a
            # trailing *string* positional argument as another key (it only pops
            # non-strings as defaults), so a positional "Push" made the lookup
            # return None and every completion fell back to pushing.
            return str(self.config.get("announce_completion", default="Push")).strip().lower()
        except Exception:
            return "push"

    async def _announce(self, task_id, work):
        """push a one-line notice when a detached / background run finishes.

        Only the status and the size travel - never the answer. The transcript
        keeps the text and `delegate_read` fetches it when it is actually wanted,
        so the main context pays ~20 tokens for the notification instead of the
        whole result.
        """
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.log("delegate", f"task {task_id} ended with {error}")

        task = self.store.find(task_id) or {}
        length = 0
        try:
            messages, _ = repair(self.store.load(task_id))
            for message in reversed(messages):
                if message.get("role") == "assistant" and str(message.get("content") or "").strip():
                    length = len(str(message.get("content")))
                    break
        except Exception:
            pass

        status = task.get("status") or "unknown"
        parts = [f"[delegate] task {task_id} finished ({status})"]
        if length:
            parts.append(f"{length} chars")
        parts.append("use delegate_read to see it")
        note = " - ".join(parts)

        mode = self._announce_mode()
        if mode == "off":
            return

        busy = bool(getattr(self.channel, "sending_message", False))
        route = policy.announce_delivery(mode, busy, len(self._runs))
        self.log(
            "delegate",
            f"announcing task {task_id}: {route} (sending_message={busy}, "
            f"live_runs={len(self._runs)})",
        )

        try:
            if route == policy.STEERING:
                # The slot a user steering message uses: the running chain drains
                # it at its next tool-round boundary, and if that turn ends first
                # the continuation loops answer it as a fresh turn. That is what
                # channel.send() did for Wake - minus the duplicated turn (send()
                # re-enters the whole user-facing send path).
                await self.channel.queue_steering_message(note)
            else:
                # defer=True already gates on _chain_idle inside _push_consumer, so
                # there is no wait of our own: that wait only added an ordering race
                # against the next user message.
                await self.channel.push(
                    {"role": "assistant", "content": note},
                    defer=route == policy.DEFERRED,
                )
        except Exception as error:
            import traceback
            trace = traceback.format_exc().strip().splitlines()
            self.log(
                "delegate",
                f"could not announce task {task_id} ({route}): "
                f"{type(error).__name__}: {error}",
            )
            for line in trace[-12:]:
                self.log("delegate", f"  {line}")

    async def _execute(self, task, prompt, model, history):
        """run the task inside the delegation context.

        The context variable marks "we are inside a delegated run": every tool
        call the run makes is awaited in it, so `delegate_new`/`delegate_send`
        can refuse nesting (the delegate inherits the full tool set).
        """
        token = self._depth.set(self._depth.get() + 1)
        try:
            return await self._run_task(task, prompt, model, history)
        finally:
            self._depth.reset(token)

    async def _run_task(self, task, prompt, model, history):
        task_id = task["id"]
        try:
            answer = await self.runner.run(
                task_id, prompt, model, history, self._max_rounds()
            )
        except DelegateCancelled:
            return self.result(f"delegate {task_id} was cancelled", success=False)
        except Exception as error:
            self.log("delegate", f"task {task_id} failed: {core.detail_error(error)}")
            current = self.store.find(task_id) or {}
            return self.result(
                {
                    "task_id": task_id,
                    "status": current.get("status", "error"),
                    "error": str(error),
                },
                success=False,
            )

        current = self.store.find(task_id) or task
        return self.result({
            "task_id": task_id,
            "status": current.get("status", "done"),
            "model": current.get("model", ""),
            "rounds": current.get("rounds", 0),
            "result": answer,
        })

    def _max_rounds(self):
        try:
            return max(1, int(self.config.get("max_rounds", 40) or 40))
        except (TypeError, ValueError):
            return 40

    def _font_scale(self):
        """the panel font size as a percentage of the chat's text, always in range"""
        try:
            scale = int(round(float(self.config.get("font_scale", 94) or 94)))
        except (TypeError, ValueError):
            scale = 94
        return max(70, min(130, scale))

    async def _system_prompt(self):
        """the current chat's frozen system prompt, plus the delegation preamble"""
        try:
            prompt = await self.channel.context.chat.get_system_prompt() or ""
        except Exception as error:
            self.log("delegate", f"could not build the chat system prompt: {error}")
            prompt = ""
        return (prompt + DELEGATE_PREAMBLE).strip()

    def _derive_title(self, prompt):
        line = ""
        for candidate in str(prompt).splitlines():
            if candidate.strip():
                line = candidate.strip()
                break
        if len(line) > 60:
            line = line[:57] + "…"
        return line or "delegated task"

    async def _resolve_model(self, requested):
        """(model name to use, error message or None)

        an empty request means "the current model". An explicit name is
        normalised to the spelling the inference server reports, so the task
        record (and future continuations) carry the canonical name.
        """
        name = (requested or "").strip()
        if not name:
            return None, None

        try:
            available = await self.manager.API.list_models()
        except Exception:
            available = None

        if not available:
            return name, None   # can't validate: let the API decide

        for candidate in available:
            if str(candidate).strip().lower() == name.lower():
                return str(candidate), None

        listing = ", ".join(str(model) for model in available)
        return None, f"model '{name}' is not available. Available: {listing}"

    # ---------------------------------------------------------------- webui
    @webui.route("state")
    async def _route_state(self, body=None, query=None):
        """new live events since a cursor, plus the tasks currently running.

        `events`/`seq` stay as they were (the panel triggers on run_start/
        run_end/run_error), and each running task also carries its live feed:
        `blocks` in stream order, `steps` started, `steps_done`, the `step` in
        flight and how many old blocks the cap dropped. The feed is built in the
        bus, so a panel opened mid-run gets the whole run, not just what
        arrived after it started polling.
        """
        events, seq = self.bus.since((query or {}).get("since"))
        return {"seq": seq, "events": events, "running": self.bus.running_list()}

    @webui.route("tasks")
    async def _route_tasks(self, body=None, query=None):
        """the task index for the panel, with the same day grouping the chat
        sidebar uses (Today / Yesterday / weekday / Last Week / month, year)"""
        try:
            tz_offset = int((query or {}).get("tz_offset") or 0)
        except (TypeError, ValueError):
            tz_offset = 0

        tasks = []
        for task in self.store.all():
            entry = dict(task)
            key = chat_listing.group_key(entry.get("updated", ""), tz_offset)
            entry["group_key"] = key or "undated"
            entry["group_label"] = chat_listing.day_label_from_key(key, tz_offset)
            tasks.append(entry)

        return {
            "tasks": tasks,
            "running": self.bus.running_ids(),
            "today": chat_listing.local_today(tz_offset).isoformat(),
            "font_scale": self._font_scale(),
        }

    @webui.route("font-scale", method="POST")
    async def _route_font_scale(self, body=None, query=None):
        """store the panel's font size (the panel applies it optimistically)

        The value is a percentage of the chat's text size, clamped to the
        setting's declared range; persisting it (instead of core editing) means
        the slider and the settings modal always agree.
        """
        try:
            scale = int(round(float((body or {}).get("scale"))))
        except (TypeError, ValueError):
            return self.result("scale must be a number", success=False)

        # write through the module's ConfigManager (it creates the path if the
        # setting is missing and saves the root config itself)
        self.config.config["font_scale"] = max(70, min(130, scale))
        return {"font_scale": self._font_scale()}

    @webui.route("rename", method="POST")
    async def _route_rename(self, body=None, query=None):
        """rename a task (the transcript is untouched)"""
        data = body or {}
        task = self.store.find(data.get("id", ""))
        if task is None:
            return self.result("no delegate task with that id", success=False)

        title = str(data.get("title") or "").strip()
        if not title:
            return self.result("title must not be blank", success=False)

        updated = self.store.update(task["id"], title=title[:120])
        return {"task": updated}

    @webui.route("delete", method="POST")
    async def _route_delete(self, body=None, query=None):
        """delete a task: its index entry and its stored transcript"""
        data = body or {}
        task = self.store.find(data.get("id", ""))
        if task is None:
            return self.result("no delegate task with that id", success=False)

        identifier = task["id"]
        if identifier in self._runs or task.get("status") == "running":
            return self.result("that task is running; stop it before deleting it", success=False)

        self.store.delete(identifier)
        return {"deleted": identifier}

    @webui.route("chain")
    async def _route_chain(self, body=None, query=None):
        """a task's transcript grouped exactly like chat turns (steps + final
        messages), so the panel can render it with the webui's own chain markup.

        Reasoning lives in `_metadata.reasoning` in our transcript (it is moved
        there so the wire never carries it), but the turn collector builds step
        headers from `reasoning_content` - so it is rehydrated here, on a copy.
        """
        task = self.store.find((query or {}).get("id", ""))
        if task is None:
            return self.result("no delegate task with that id", success=False)

        messages, dropped = repair(self.store.load(task["id"]))

        history = []
        for message in messages:
            if message.get("role") == "system":
                continue
            message = dict(message)
            reasoning = (message.get("_metadata") or {}).get("reasoning")
            if reasoning and not message.get("reasoning_content"):
                message["reasoning_content"] = reasoning
            history.append(message)

        turns = await self.channel.turncollector.group_history(history)
        return {"task": task, "turns": turns, "dropped": dropped}

    @webui.route("transcript")
    async def _route_transcript(self, body=None, query=None):
        """a task's stored transcript (rich form: _metadata included)"""
        task = self.store.find((query or {}).get("id", ""))
        if task is None:
            return self.result("no delegate task with that id", success=False)

        messages, dropped = repair(self.store.load(task["id"]))
        return {"task": task, "messages": messages, "dropped": dropped}

    # --------------------------------------------------------------- hooks
    async def on_system_prompt(self):
        return (
            "You can hand a self-contained task to a separate AI session with its own context:\n"
            "- `delegate_new(prompt, title="", model="")` runs one task and blocks until it finishes; "
            "its final message comes back as the tool result. The delegate cannot see this chat, so write "
            "`prompt` self-contained.\n"
            "- Delegated runs are saved as tasks with an id. The user can watch any task live in the "
            "delegate panel, so they do not need to trust the summary blindly.\n"
            "- `delegate_send(task_id, prompt, model="")` continues the same task later: its full stored "
            "context (including every tool result) is replayed, so use it to ask why it did something or "
            "to hand it another step. Do not ask the user for a task id - use the one delegate_new returned.\n"
            "- If those ids are no longer in the conversation (e.g. after context compression), "
            "`delegate_list(query=\"\", status=\"\", limit=20)` returns the stored tasks newest first, "
            "so an id can be recovered instead of asking the user.\n"
            "- `delegate_new`, `delegate_send` and `delegate_list` are preloaded tools: call them "
            "directly by name, not through `tools_call`.\n"
            "- A run that outlives the caller's tool deadline comes back as `{task_id, status: "
            "\"running\", detached: true}` instead of the final text. It keeps running in the "
            "background: check it with `delegate_list`, read it in the delegate panel, or "
            "`delegate_send` once it is done. `delegate_read(task_id)` returns its final answer. "
            "Pass `background=True` to skip the wait entirely and get the task id at once.\n"
            "- `background=True` needs a free background slot (`max_background_tasks`, default 1) "
            "and the chat's current model; a refusal names the reason, and retrying without "
            "`background` runs the task in the foreground. While a background task runs you can "
            "keep going in this chat.\n"
            "- A delegated run cannot delegate further: `delegate_new`/`delegate_send` called "
            "from inside a run are refused.\n"
            "Delegated tasks inherit your tools."
        )

    def on_tool_summary(self, name, args, response):
        """one-line hint for the collapsed tool card: id · rounds · status"""
        if name not in (TOOL_NEW, TOOL_SEND, TOOL_LIST, TOOL_READ):
            return ""

        payload = response.get("content") if isinstance(response, dict) else None
        if not isinstance(payload, dict):
            return ""

        if name == TOOL_READ:
            text = str(payload.get("result") or "")
            status = str(payload.get("status") or "?")
            return f"{status} · {len(text)} chars" if text else status

        if name == TOOL_LIST:
            count = payload.get("count")
            if count is None:
                return ""
            return f"{count} task" if count == 1 else f"{count} tasks"

        task_id = payload.get("task_id")
        if not task_id:
            return ""

        parts = [str(task_id)]
        rounds = payload.get("rounds")
        if rounds is not None:
            parts.append(f"{rounds} rounds" if rounds != 1 else "1 round")
        status = payload.get("status")
        if status:
            parts.append(str(status))
        return " · ".join(parts)
