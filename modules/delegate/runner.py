"""delegate — one delegated run against its own persisted message list.

The loop mirrors the `subagents` module (stream -> on `tool_calls`, hand the
round to `channel.tc_manager.process(context=ctx)` so tools run exactly like
they do for the main agent), with three differences that matter here:

  * the assistant message built for each tool round keeps the content and
    reasoning that preceded the call (subagents pass empty lists), so the
    stored transcript - and the next continuation - keeps the model's preamble
    and chain of thought for that round;
  * every message that lands in the context is folded into the rich transcript
    and saved, so a crash costs at most the current round;
  * the `tool_calls` tokens `process()` yields for the rounds it recurses into
    are announced to the panel and counted against `max_rounds`, because core's
    recursion runs the whole rest of the chain inside one `process()` call and
    has no cap of its own.

`ctx` stays wire-clean: `_absorb()` moves `reasoning_content` into the rich
copy's `_metadata` as messages appear, so `process()` never re-sends it.

Every event the panel receives carries two counters: `round` (the task's
cumulative tool-round count, unchanged) and `step` (this run's 1-based
assistant-message index). `step` is what the live block feed in events.py
groups chunks, calls and results by - one step = one assistant message = the
unit the stored chain counts as a "step".
"""

import asyncio
import json
import time

import core

from .store import to_wire

PANEL_TEXT_LIMIT = 1500   # tool results are truncated for the live panel only
PANEL_ARGS_LIMIT = 400


def _short(text, limit=PANEL_TEXT_LIMIT):
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + " …"


class DelegateCancelled(Exception):
    """the user pressed Stop while the delegate was running"""


class DelegateRunner:
    def __init__(self, module):
        self.module = module
        self._model = ""        # model this run is on, recorded per assistant message
        self._announced_calls = set()   # call ids already sent to the panel (this run)
        self._round_base = 0    # task's cumulative rounds at the start of this run

    # ----------------------------------------------------------- run-local step
    def _step_now(self, rounds):
        """the run-local index of the assistant message being streamed. 1-based,
        so the panel's live feed can group one message's chunks into one step and
        count the run's steps the way the stored chain does."""
        return max(1, int(rounds) - self._round_base + 1)

    def _step_of_round(self, rounds):
        """the step of the message that carried a tool call of round `rounds`
        (the round counter is already incremented when a call is announced)"""
        return max(1, int(rounds) - self._round_base)

    # ------------------------------------------------------------ model scope
    def _use_model(self, model):
        """point core's current model at the delegate's model for this run.

        core's tc_manager.process() re-sends its follow-up request with no model
        argument (core/toolcalls.py:313), so it falls back to core.config's
        current model. Left alone, a tool round would continue on the chat's
        model instead of the delegate's - and on a single-slot router that means
        a swap away and back in the middle of one delegated run.

        Returns the previous model name so it can be restored, or None when
        nothing changed. The config is never saved: this only lasts for the run.
        """
        if not model:
            return None

        api = self.module.manager.API
        previous = api.get_model()
        if str(previous).strip().lower() == str(model).strip().lower():
            return None

        core.config.config.setdefault("model", {})["name"] = model
        return previous

    def _restore_model(self, previous):
        if previous is None:
            return
        core.config.config.setdefault("model", {})["name"] = previous

    # ---------------------------------------------------------------- helpers
    def _absorb(self, ctx, rich, round_no, reasoning=None):
        """fold messages that appeared in `ctx` into the display transcript.

        `ctx` is the wire list used for requests, `rich` the stored transcript.
        They stay index-aligned, so anything new in `ctx` becomes a rich copy
        with `_metadata`. `reasoning` is passed only for the final plain-content
        message, which was appended by us and never carried a reasoning field.
        """
        while len(rich) < len(ctx):
            wire = ctx[len(rich)]
            metadata = {"timestamp": int(time.time()), "round": round_no}
            if self._model:
                metadata["model"] = self._model

            found = wire.pop("reasoning_content", None)
            if found:
                metadata["reasoning"] = found
            elif reasoning:
                metadata["reasoning"] = reasoning
                reasoning = None

            rich.append({**wire, "_metadata": metadata})

    @staticmethod
    def _trailing_partial(ctx, request):
        """True when the follow-up request was cut before it finished.

        core's tc_manager.process() answers CancelledError by appending the
        partial assistant message it had accumulated to the context, so the
        signature is: the last context message is an assistant message with
        content but no tool calls, and it is not the tool call we handed over.
        """
        last = ctx[-1] if ctx else None
        if not isinstance(last, dict) or last.get("role") != "assistant":
            return False
        if last.get("tool_calls") or last is request:
            return False
        return bool(str(last.get("content") or "").strip())

    @staticmethod
    def _unwrap_call(name, raw_args):
        """(display name, display args) for one call.

        A delegated run loads its tools dynamically, so most of its calls arrive
        as `tools_call` with the real tool inside the arguments
        (`{"name": "browser_navigate", "params": {...}}`). Emitting that verbatim
        makes the panel's row read `tools_call({"name": ...})`, so the event
        carries the proxied name and its own params instead. The stored
        transcript keeps the raw call; only the live feed uses this form.
        """
        name = str(name or "")
        if name != "tools_call":
            return name, raw_args

        try:
            payload = json.loads(raw_args or "{}")
        except (TypeError, ValueError):
            return name, raw_args
        if not isinstance(payload, dict):
            return name, raw_args

        target = payload.get("name")
        if not isinstance(target, str) or not target:
            return name, raw_args

        params = payload.get("params")
        if isinstance(params, str):
            return target, params
        return target, json.dumps(params if isinstance(params, dict) else {}, ensure_ascii=False)

    def _emit_calls(self, task_id, round_no, tool_calls):
        """announce one round's calls to the panel, once per call id.

        The outer branch announces the first round and the inner loop announces
        every round core's `process()` recursion runs after it. Those ids are
        disjoint in core's flow (process() yields only the calls of its own
        follow-up requests), but a re-emitted id would duplicate a card in the
        panel, so the ids announced in this run are remembered.
        """
        for call in tool_calls or []:
            function = call.get("function") or {}
            call_id = call.get("id", "")

            if call_id:
                if call_id in self._announced_calls:
                    continue
                self._announced_calls.add(call_id)

            name, args = self._unwrap_call(
                function.get("name", ""), function.get("arguments", "")
            )
            self.module.bus.emit(
                task_id,
                "tool_call",
                # `round` is the task's cumulative round counter (unchanged, it
                # is what the panel's round badge used), `step` is this run's
                # 1-based assistant-message index, which is what the live feed
                # groups calls/blocks by
                round=round_no,
                step=self._step_of_round(round_no),
                call_id=call_id,
                name=name,
                args=_short(args, PANEL_ARGS_LIMIT),
            )

    # -------------------------------------------------------------------- run
    async def run(self, task_id, prompt, model, history, max_rounds):
        """run one turn of a delegated task and return its final message.

        `history` is the stored rich transcript (system prompt first, for a new
        task). Raises on failure; the caller owns the task's index status.
        """
        module = self.module
        channel = module.channel
        toolcalls = channel.tc_manager
        store = module.store
        bus = module.bus

        task = store.find(task_id) or {}
        # `rounds` is the task's lifetime statistic (shown in the panel and tool
        # summary, cumulative across continuations). `max_rounds` is a budget for
        # THIS run, so it is compared against a run-local count - otherwise a
        # long-lived task would brick itself once its lifetime total passes the
        # cap, and every later continuation would abort on its first tool call.
        rounds = int(task.get("rounds") or 0)
        run_rounds = 0

        rich = [dict(message) for message in history]
        rich.append({
            "role": "user",
            "content": prompt,
            "_metadata": {"timestamp": int(time.time()), "round": rounds},
        })
        store.save(task_id, rich)

        ctx = [to_wire(message) for message in rich]

        effective_model = model or module.manager.API.get_model() or ""
        self._model = effective_model
        self._announced_calls = set()   # fresh per run: the calls already announced
        self._round_base = rounds        # run-local steps start at 1, see _step_now

        bus.start(task_id, title=task.get("title", ""), model=effective_model, rounds=rounds)

        # the delegate's answer is everything it said after its last tool round.
        # a follow-up request can be cut mid-answer (cancel, connection reset);
        # the next pass then continues the same sentence, so those parts are
        # joined without a separator instead of looking like separate paragraphs.
        answer_parts = []       # [(text, separator_before)]
        answer_cut = False

        # a tool round's follow-up request is made by core with no model argument,
        # so the delegate's model has to BE core's current model for the run
        scoped_from = self._use_model(effective_model)

        try:
            while True:
                content, reasoning = [], []
                called_tool = False
                finished = False

                kwargs = {"tools": module.manager.tools}
                if effective_model:
                    kwargs["model"] = effective_model

                stream = module.manager.API.send_stream(ctx, **kwargs)
                try:
                    async for token in stream:
                        if module.manager.API.cancel_request:
                            raise DelegateCancelled("cancelled by the user")

                        token_type = token.get("type")
                        if token_type == "content":
                            chunk = token.get("content") or ""
                            content.append(chunk)
                            bus.emit(
                                task_id, "content",
                                text=chunk, round=rounds, step=self._step_now(rounds),
                            )
                        elif token_type == "reasoning":
                            chunk = token.get("content") or ""
                            reasoning.append(chunk)
                            bus.emit(
                                task_id, "reasoning",
                                text=chunk, round=rounds, step=self._step_now(rounds),
                            )
                        elif token_type == "error":
                            raise RuntimeError(str(token.get("content") or "the API returned an error"))
                        elif token_type == "tool_calls":
                            called_tool = True
                            rounds += 1
                            run_rounds += 1
                            bus.set_rounds(task_id, rounds)
                            if run_rounds > max_rounds:
                                raise RuntimeError(
                                    f"the delegate hit its round cap ({max_rounds} tool rounds)"
                                )

                            request = await toolcalls._build_recursive_request(
                                token, content, reasoning
                            )
                            self._emit_calls(task_id, rounds, request.get("tool_calls"))
                            content, reasoning = [], []

                            # core's process() runs the whole rest of the tool chain
                            # itself: it yields the `tool_calls` token of every round
                            # after this one. Announcing them here is what makes the
                            # panel live past round 1 - and what makes max_rounds bind,
                            # because core's recursion has no cap of its own.
                            sub_stream = toolcalls.process(request, context=ctx)
                            try:
                                async for sub_token in sub_stream:
                                    sub_type = sub_token.get("type")
                                    if sub_type == "final":
                                        message = sub_token.get("content")
                                        if message:
                                            ctx.append(message)
                                            self._absorb(ctx, rich, rounds)
                                            piece = message.get("content") or ""
                                            if piece:
                                                answer_parts.append((piece, "" if answer_cut else "\n\n"))
                                                answer_cut = False
                                            finished = True
                                    elif sub_type == "tool_calls":
                                        rounds += 1
                                        run_rounds += 1
                                        bus.set_rounds(task_id, rounds)
                                        if run_rounds > max_rounds:
                                            # the rounds that did run are already in ctx:
                                            # fold them into the transcript before giving
                                            # up, so the abort does not silently drop them
                                            self._absorb(ctx, rich, rounds)
                                            raise RuntimeError(
                                                f"the delegate hit its round cap ({max_rounds} tool rounds)"
                                            )

                                        # the token is raw: its arguments may be broken
                                        # JSON, which the panel could not parse. repair it
                                        # exactly as core does before showing it.
                                        repaired = await toolcalls._repair_toolcall_token(sub_token)
                                        self._emit_calls(
                                            task_id, rounds, repaired.get("tool_calls")
                                        )
                                    elif sub_type == "tool":
                                        bus.emit(
                                            task_id,
                                            "tool_result",
                                            # a result belongs to the message that
                                            # carried the call: core yields a
                                            # round's results before the next
                                            # round's stream, so `rounds` is it
                                            round=rounds,
                                            step=self._step_of_round(rounds),
                                            call_id=sub_token.get("tool_call_id", ""),
                                            text=_short(sub_token.get("content")),
                                        )
                                    elif sub_type == "content":
                                        bus.emit(
                                            task_id, "content",
                                            text=sub_token.get("content") or "",
                                            round=rounds, step=self._step_now(rounds),
                                        )
                                    elif sub_type == "reasoning":
                                        bus.emit(
                                            task_id, "reasoning",
                                            text=sub_token.get("content") or "",
                                            round=rounds, step=self._step_now(rounds),
                                        )
                                    elif sub_type == "error":
                                        raise RuntimeError(
                                            str(sub_token.get("content") or "the API returned an error")
                                        )
                            finally:
                                # same shape as the outer stream's finally: an abort out
                                # of this loop (round cap, API error) must not leave
                                # core's generator - and the model stream behind it -
                                # suspended until the garbage collector gets round to it
                                close = getattr(sub_stream, "aclose", None)
                                if close is not None:
                                    try:
                                        await close()
                                    except Exception:
                                        pass

                            self._absorb(ctx, rich, rounds)
                            store.save(task_id, rich)
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        try:
                            await close()
                        except Exception:
                            pass

                if called_tool and not finished:
                    # the round ended without a final message: ask again so the
                    # delegate can finish its thought (bounded by max_rounds).
                    # when the last context message is a content-only assistant
                    # message, the request was cut mid-answer (core catches the
                    # cancellation and stores what it had), so the next part
                    # continues the same sentence instead of a new paragraph.
                    if self._trailing_partial(ctx, request):
                        self.module.log(
                            "delegate",
                            f"task {task_id}: round {rounds} was interrupted mid-answer; asking again to finish it",
                        )
                        bus.emit(task_id, "run_note", text="the response was interrupted — continuing")
                        answer_cut = True
                    continue

                if not called_tool and content:
                    message = {"role": "assistant", "content": "".join(content)}
                    ctx.append(message)
                    self._absorb(ctx, rich, rounds, reasoning="".join(reasoning))
                    store.save(task_id, rich)
                    if message["content"]:
                        answer_parts.append((message["content"], "" if answer_cut else "\n\n"))
                        answer_cut = False

                break

            answer = ""
            for piece, separator in answer_parts:
                if not piece:
                    continue
                answer += (separator if answer else "") + piece

            if not answer.strip():
                raise RuntimeError("the delegate finished without a final message")

            store.update(task_id, status="done", rounds=rounds, last_error=None)
            bus.stop(task_id)
            return answer

        except asyncio.CancelledError:
            # the caller was cancelled: the tool call hit its timeout, the turn
            # was aborted, or Stop was pressed. Keep what landed - including the
            # rounds core ran inside this process() call - and never leave the
            # task looking "running" forever: a cancelled task must read as
            # interrupted in the index and the panel. An aborted round may end on
            # a dangling tool call; repair() truncates it on the next continuation.
            self._absorb(ctx, rich, rounds)
            store.save(task_id, rich)
            store.update(
                task_id,
                status="interrupted",
                rounds=rounds,
                last_error="cancelled before the delegate finished",
            )
            bus.stop(task_id, error="cancelled before the delegate finished")
            raise

        except Exception as error:
            self._absorb(ctx, rich, rounds)
            store.save(task_id, rich)
            if isinstance(error, DelegateCancelled):
                store.update(task_id, status="interrupted", rounds=rounds, last_error=str(error))
            else:
                store.update(task_id, status="error", rounds=rounds, last_error=str(error))
            bus.stop(task_id, error=str(error))
            raise

        finally:
            self._restore_model(scoped_from)
