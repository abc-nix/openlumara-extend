"""delegate — one delegated run against its own persisted message list.

The loop mirrors the `subagents` module (stream -> on `tool_calls`, hand the
round to `channel.tc_manager.process(context=ctx)` so tools run exactly like
they do for the main agent), with two differences that matter here:

  * the assistant message built for each tool round keeps the content and
    reasoning that preceded the call (subagents pass empty lists), so the
    stored transcript - and the next continuation - keeps the model's preamble
    and chain of thought for that round;
  * every message that lands in the context is folded into the rich transcript
    and saved, so a crash costs at most the current round.

`ctx` stays wire-clean: `_absorb()` moves `reasoning_content` into the rich
copy's `_metadata` as messages appear, so `process()` never re-sends it.
"""

import asyncio
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

    def _emit_calls(self, task_id, round_no, tool_calls):
        for call in tool_calls or []:
            function = call.get("function") or {}
            self.module.bus.emit(
                task_id,
                "tool_call",
                round=round_no,
                call_id=call.get("id", ""),
                name=function.get("name", ""),
                args=_short(function.get("arguments", ""), PANEL_ARGS_LIMIT),
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
        rounds = int(task.get("rounds") or 0)

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
                            bus.emit(task_id, "content", text=chunk)
                        elif token_type == "reasoning":
                            chunk = token.get("content") or ""
                            reasoning.append(chunk)
                            bus.emit(task_id, "reasoning", text=chunk)
                        elif token_type == "error":
                            raise RuntimeError(str(token.get("content") or "the API returned an error"))
                        elif token_type == "tool_calls":
                            called_tool = True
                            rounds += 1
                            bus.set_rounds(task_id, rounds)
                            if rounds > max_rounds:
                                raise RuntimeError(
                                    f"the delegate hit its round cap ({max_rounds} tool rounds)"
                                )

                            request = await toolcalls._build_recursive_request(
                                token, content, reasoning
                            )
                            self._emit_calls(task_id, rounds, request.get("tool_calls"))
                            content, reasoning = [], []

                            async for sub_token in toolcalls.process(request, context=ctx):
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
                                elif sub_type == "tool":
                                    bus.emit(
                                        task_id,
                                        "tool_result",
                                        call_id=sub_token.get("tool_call_id", ""),
                                        text=_short(sub_token.get("content")),
                                    )
                                elif sub_type == "content":
                                    bus.emit(task_id, "content", text=sub_token.get("content") or "")
                                elif sub_type == "reasoning":
                                    bus.emit(task_id, "reasoning", text=sub_token.get("content") or "")
                                elif sub_type == "error":
                                    raise RuntimeError(
                                        str(sub_token.get("content") or "the API returned an error")
                                    )

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
            # was aborted, or Stop was pressed. Keep what landed and never leave
            # the task looking "running" forever - a cancelled task must read as
            # interrupted in the index and the panel.
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
            store.save(task_id, rich)
            if isinstance(error, DelegateCancelled):
                store.update(task_id, status="interrupted", rounds=rounds, last_error=str(error))
            else:
                store.update(task_id, status="error", rounds=rounds, last_error=str(error))
            bus.stop(task_id, error=str(error))
            raise

        finally:
            self._restore_model(scoped_from)
