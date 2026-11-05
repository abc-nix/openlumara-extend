"""delegate — hand a self-contained task to an AI session with its own context.

Unlike the `subagents` module, a delegated run is *persisted*: its transcript
(reasoning, assistant text, every tool call and every tool result) is stored
under data/delegate/history/<task_id>.json. That makes two things possible
that subagents cannot do:

  * the user can watch the whole process live in the delegate panel instead of
    having to trust the final answer;
  * the same conversation can be continued later with delegate_send(task_id,
    ...), replaying the full stored context instead of re-summarising it.

v1 blocks the calling turn: the delegate's final message is returned as the
tool result (there is no background mode yet).

Running the delegate's tool loop through channel.tc_manager.process(context=...)
accepts three core side effects for v1 (see runner.py): the main chat can be
auto-compressed mid-run, the delegate's API usage is counted against the main
context, and a steering message can be flushed into the main context mid-run.
"""

import core
import channels.webui.api as webui
import channels.webui.chat_listing as chat_listing

from .store import DelegateStore, repair
from .events import EventBus
from .runner import DelegateRunner, DelegateCancelled

TOOL_NEW = "delegate_new"
TOOL_SEND = "delegate_send"
TOOL_LIST = "delegate_list"

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
        "font_scale": {
            "type": "number",
            "default": 94,
            "min": 70,
            "max": 130,
            "step": 1,
            "description": "Font size of the delegate panel, as a percentage of the chat's text (94 = the chat size minus ~1px).",
        },
    }

    async def on_ready(self):
        self.store = DelegateStore()
        self.bus = EventBus()
        self.runner = DelegateRunner(self)
        self._active_task = None
        self.store.mark_stale()

    # ------------------------------------------------------------------ tools
    @core.module.tool(bypass_timeout=True)
    async def new(self, prompt: str, title: str = "", model: str = "") -> dict:
        """Delegate a self-contained task to a separate AI session with its own context.

        The delegate sees this chat's system prompt but none of its history, so the
        prompt must stand on its own. It has the same tools you do and its whole
        transcript (reasoning, tool calls, tool results) is saved under the returned
        task_id: the user can watch it live in the delegate panel, and you can
        continue the same conversation later with delegate_send(task_id, ...).

        This call blocks until the delegate finishes; its final answer is the result.

        Args:
            prompt: the task, written self-contained (the delegate cannot see this chat).
            title: short display label for the panel and task list. Derived from the prompt when empty.
            model: model to run the delegate on. Defaults to the current model; must be one of the available models.

        Returns:
            {task_id, status, model, rounds, result}
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return self.result("prompt must not be blank", success=False)

        if self._active_task:
            return self.result(
                f"a delegate is already running ({self._active_task}); finish it before starting another",
                success=False,
            )

        model_name, error = await self._resolve_model(model)
        if error:
            return self.result(error, success=False)

        chat = self.channel.context.chat
        task = self.store.create(
            title=(title or "").strip() or self._derive_title(prompt),
            model=model_name or self.manager.API.get_model(),
            channel_name=self.channel.name,
            chat_id=chat.get("id"),
        )

        system_prompt = await self._system_prompt()
        history = [{"role": "system", "content": system_prompt}] if system_prompt else []

        return await self._execute(task, prompt, model_name, history)

    @core.module.tool(bypass_timeout=True)
    async def send(self, task_id: str, prompt: str, model: str = "") -> dict:
        """Continue a delegated task: a follow-up message in the same conversation.

        The delegate resumes with its full stored context (every message and tool
        result is replayed; the stored reasoning is kept for display only), so this
        is a normal continuation, not a re-summary. Use it to ask why the delegate
        did something, or to hand it another step.

        Args:
            task_id: id of the task, as returned by delegate_new (for example "01AB3F7G").
            prompt: the follow-up request.
            model: switch the task to another model. Defaults to the model the task already uses.

        Returns:
            {task_id, status, model, rounds, result}
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return self.result("prompt must not be blank", success=False)

        task = self.store.find(task_id)
        if task is None:
            return self.result(f"no delegate task with id '{task_id}'", success=False)

        identifier = task["id"]
        if task.get("status") == "running" or self._active_task == identifier:
            return self.result(f"delegate task {identifier} is still running", success=False)

        if self._active_task:
            return self.result(
                f"a delegate is already running ({self._active_task}); finish it before continuing another",
                success=False,
            )

        model_name, error = await self._resolve_model(model)
        if error:
            return self.result(error, success=False)

        if model_name and model_name.lower() != str(task.get("model") or "").lower():
            self.store.update(identifier, model=model_name)
            task = self.store.find(identifier)

        history, dropped = repair(self.store.load(identifier))
        if dropped:
            self.store.save(identifier, history)
        if not any(message.get("role") == "system" for message in history):
            system_prompt = await self._system_prompt()
            if system_prompt:
                history.insert(0, {"role": "system", "content": system_prompt})
                self.store.save(identifier, history)

        self.store.update(identifier, status="running", last_error=None)

        return await self._execute(task, prompt, model_name or task.get("model") or None, history)

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

    # ------------------------------------------------------------- execution
    async def _execute(self, task, prompt, model, history):
        task_id = task["id"]
        self._active_task = task_id

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
        finally:
            self._active_task = None

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
        """new live events since a cursor, plus the tasks currently running"""
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
            "running": [job["task_id"] for job in self.bus.running_list()],
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
        if self._active_task == identifier or task.get("status") == "running":
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
            "Delegated tasks inherit your tools."
        )

    def on_tool_summary(self, name, args, response):
        """one-line hint for the collapsed tool card: id · rounds · status"""
        if name not in (TOOL_NEW, TOOL_SEND, TOOL_LIST):
            return ""

        payload = response.get("content") if isinstance(response, dict) else None
        if not isinstance(payload, dict):
            return ""

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
