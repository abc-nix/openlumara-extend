"""delegate runner tests: one run's loop, without an OpenLumara boot.

The runner only talks to the framework through a handful of objects (the API's
stream, the tool-call manager, the store, the event bus), so the loop can be
driven with fakes: no network, no model, no restart.

Run from the repo root:

    python3 -m unittest discover -s modules/delegate/tests -v
"""

import asyncio
import copy
import json
import sys
import time
import types
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]

if "core" not in sys.modules:
    _core = types.ModuleType("core")
    _core.config = types.SimpleNamespace(config={})
    _core.get_data_path = lambda *args, **kwargs: None
    _core.storage = types.SimpleNamespace(StorageList=None)
    # the runner only touches core.config (model scoping) at run time
    sys.modules["core"] = _core

sys.path.insert(0, str(MODULE_DIR.parent))

from delegate import runner as runner_module  # noqa: E402  (needs the stub above first)
from delegate import store as store_module    # noqa: E402  (repair(): transcript validity)
from delegate.events import BlockBuilder      # noqa: E402  (the live block feed)


CALL = {
    "type": "tool_calls",
    "tool_calls": [
        {"id": "call-1", "function": {"name": "delegate_list", "arguments": "{}"}}
    ],
}

# what a model emits one round later, inside core's recursion: a call with
# arguments that are not valid JSON (json_repair fixes them in core, and the
# runner has to repair them before showing them)
RECURSIVE_CALL = {
    "type": "tool_calls",
    "tool_calls": [
        {"id": "call-2", "function": {"name": "editor_read", "arguments": "{'path': 'notes.md'}"}}
    ],
}

RECURSIVE_CALL_2 = {
    "type": "tool_calls",
    "tool_calls": [
        {"id": "call-3", "function": {"name": "editor_edit", "arguments": '{"path": "notes.md"}'}}
    ],
}


def tool_token(call_id):
    return {"type": "tool", "tool_call_id": call_id, "content": f"output of {call_id}"}


def reasoning_token(text):
    return {"type": "reasoning", "content": text}


def content_token(text):
    return {"type": "content", "content": text}


def final_token(text="the answer"):
    return {"type": "final", "content": {"role": "assistant", "content": text}}


class FakeAPI:
    """send_stream() replays one scripted token list per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.cancel_request = False
        self.models = []
        self.contexts = []

    def get_model(self):
        return "test-model"

    def send_stream(self, ctx, **kwargs):
        tokens = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        self.models.append(kwargs.get("model"))
        self.contexts.append(ctx)

        async def stream():
            for token in tokens:
                yield token

        return stream()


class FakeToolCalls:
    """process() replays a scripted list of sub-tokens, like core's recursion.

    With no `sub_tokens`: the round's tool result plus, when asked for, a final
    message - one round, nothing recursive. A `tool_calls` token in the script
    stands for core recursing into another round: core yields that token to its
    caller and runs the round inside the same process() call, so the runner only
    ever sees the token (that is the shape P1 had to learn to handle).

    Like core, process() appends the assistant request and every tool result to
    the `context` it is handed, `_repair_toolcall_token` returns a token copy
    with canonical JSON arguments, and every generator records whether the
    caller consumed it to the end or closed it early.
    """

    def __init__(self, final_message=None, sub_tokens=None):
        self.final_message = final_message
        self.sub_tokens = sub_tokens
        self.requests = []
        self.repaired = []        # tokens that went through repair
        self.generations = []     # {"exhausted": bool, "closed": bool}

    async def _build_recursive_request(self, token, content, reasoning):
        # core's shape: content/reasoning only when there was some
        request = {"role": "assistant"}
        if content:
            request["content"] = "".join(content)
        if reasoning:
            request["reasoning_content"] = "".join(reasoning)
        request["tool_calls"] = copy.deepcopy(token.get("tool_calls") or [])
        self.requests.append(request)
        return request

    async def _repair_toolcall_token(self, token):
        if not token.get("tool_calls"):
            return token

        self.repaired.append(token)
        repaired = []
        for call in token["tool_calls"]:
            function = dict(call.get("function") or {})
            raw = function.get("arguments")
            if isinstance(raw, dict):
                parsed = raw
            else:
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    parsed = {}       # core: json_repair, {} when beyond repair
            # the marker is how a test can tell that the runner announced the
            # *repaired* calls instead of the raw token it was handed
            function["arguments"] = json.dumps({**parsed, "_repaired": True})
            repaired.append({**call, "function": function})

        out = token.copy()
        out["tool_calls"] = repaired
        return out

    def process(self, request, context=None):
        script = self.sub_tokens
        if script is None:
            script = [tool_token("call-1")]
            if self.final_message:
                script.append({"type": "final", "content": dict(self.final_message)})

        state = {"exhausted": False, "closed": False}
        self.generations.append(state)

        if context is not None:
            # core adds the request to the context before running its tools
            context.append(dict(request))

        async def stream():
            try:
                for token in script:
                    yield token
                    if context is None:
                        continue
                    # a round core recurses into only reaches the context once the
                    # caller let that recursion start
                    if token.get("type") == "tool_calls":
                        context.append({
                            "role": "assistant",
                            "tool_calls": copy.deepcopy(token.get("tool_calls") or []),
                        })
                    elif token.get("type") == "tool":
                        context.append({
                            "role": "tool",
                            "tool_call_id": token.get("tool_call_id"),
                            "content": token.get("content"),
                        })
                state["exhausted"] = True
            finally:
                state["closed"] = True

        return stream()


class FakeBus:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.events = []

    def start(self, task_id, **fields):
        self.started.append((task_id, fields))

    def set_rounds(self, task_id, rounds):
        self.events.append(("rounds", task_id, rounds))

    def emit(self, task_id, kind, **fields):
        self.events.append((kind, task_id, fields))

    def stop(self, task_id, error=None):
        self.stopped.append((task_id, error))


class FakeStore:
    def __init__(self, task=None):
        self.tasks = {"T1": dict(task or {"id": "T1", "title": "test", "rounds": 0})}
        self.saved = {}

    def find(self, task_id):
        return self.tasks.get(task_id)

    def save(self, task_id, messages):
        self.saved[task_id] = [dict(message) for message in messages]

    def update(self, task_id, **fields):
        self.tasks.setdefault(task_id, {}).update(fields)
        return self.tasks[task_id]


class FakeConfig:
    """the module settings the runner reads (a dict with .get())"""

    def __init__(self):
        self.values = {"keep_reasoning_in_context": True}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeModule:
    def __init__(self, script, final_message=None, sub_tokens=None):
        self.api = FakeAPI(script)
        self.toolcalls = FakeToolCalls(final_message, sub_tokens)
        self.store = FakeStore()
        self.config = FakeConfig()
        self.bus = FakeBus()
        self.manager = types.SimpleNamespace(
            API=self.api, tools=[], log=lambda *args: None
        )
        self.channel = types.SimpleNamespace(tc_manager=self.toolcalls)
        self.logs = []

    def log(self, category, message):
        self.logs.append((category, message))

    # the module's diagnostic channel (verbose_logs): off by default, so the
    # runner's timings are silent unless a test switches it on
    def _verbose(self):
        return bool(self.config.get("verbose_logs", False))

    def _debug(self, message):
        if self._verbose():
            self.log("delegate", message)


def make(script, final_message=None, sub_tokens=None):
    module = FakeModule(script, final_message, sub_tokens)
    return module, runner_module.DelegateRunner(module)


class RunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # _use_model writes the current model into core.config, never saved
        self._core_config = sys.modules["core"].config.config
        self._core_config.clear()

    async def test_plain_answer_is_stored_and_the_task_is_done(self):
        module, delegate = make([[{"type": "content", "content": "hel"},
                                  {"type": "content", "content": "lo"}]])

        answer = await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertEqual(answer, "hello")
        self.assertEqual(module.store.tasks["T1"]["status"], "done")
        stored = module.store.saved["T1"]
        self.assertEqual(stored[0]["role"], "user")
        self.assertEqual(stored[-1]["role"], "assistant")
        self.assertEqual(stored[-1]["content"], "hello")
        self.assertIn("_metadata", stored[-1])
        self.assertEqual(module.api.models, ["test-model"])
        self.assertEqual(module.bus.stopped, [("T1", None)])

    async def test_round_cap_aborts_the_run_and_marks_it_error(self):
        # max_rounds=1: the first tool round is allowed, the second is not
        module, delegate = make([[CALL], [CALL]])

        with self.assertRaises(RuntimeError) as raised:
            await delegate.run("T1", "hi", "test-model", [], 1)

        self.assertIn("round cap", str(raised.exception))
        self.assertEqual(module.store.tasks["T1"]["status"], "error")
        self.assertIn("round cap", module.store.tasks["T1"]["last_error"])
        self.assertEqual(module.store.tasks["T1"]["rounds"], 2)
        # the transcript up to the abort is kept, so a continuation still works
        self.assertTrue(module.store.saved["T1"])
        self.assertEqual(module.bus.stopped[0][0], "T1")
        self.assertIn("round cap", str(module.bus.stopped[0][1]))

    async def test_a_tool_round_that_ends_with_a_final_message_is_the_answer(self):
        module, delegate = make([[CALL]], final_message={"role": "assistant", "content": "the answer"})

        answer = await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertEqual(answer, "the answer")
        self.assertEqual(module.store.tasks["T1"]["status"], "done")
        self.assertEqual(module.store.tasks["T1"]["rounds"], 1)
        self.assertEqual(module.store.saved["T1"][-1]["content"], "the answer")

    async def test_an_empty_answer_fails_instead_of_storing_nothing(self):
        module, delegate = make([[]])

        with self.assertRaises(RuntimeError) as raised:
            await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertIn("without a final message", str(raised.exception))
        self.assertEqual(module.store.tasks["T1"]["status"], "error")

    async def test_rounds_continue_from_the_stored_count(self):
        module, delegate = make([[{"type": "content", "content": "ok"}]])
        module.store.tasks["T1"]["rounds"] = 5

        await delegate.run("T1", "hi", "test-model", [], 40)

        # a plain answer adds no tool round: the cumulative count is unchanged
        self.assertEqual(module.store.tasks["T1"]["rounds"], 5)

    # ---- core's recursion: the rounds after the first are announced too ----

    async def test_a_recursive_round_is_announced_before_its_result(self):
        # core's process() runs the rest of the chain by itself and yields the
        # tool_calls token of every round it recurses into. Before this fix the
        # runner dropped those tokens: the panel never learned that round 2
        # existed and its tool result had nothing to attach to.
        module, delegate = make([[CALL]], sub_tokens=[
            tool_token("call-1"),
            RECURSIVE_CALL,
            tool_token("call-2"),
            final_token("the answer"),
        ])

        answer = await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertEqual(
            [event[0] for event in module.bus.events],
            ["rounds", "tool_call", "tool_result", "rounds", "tool_call", "tool_result"],
        )

        calls = [event for event in module.bus.events if event[0] == "tool_call"]
        self.assertEqual([call[2]["call_id"] for call in calls], ["call-1", "call-2"])
        self.assertEqual([call[2]["name"] for call in calls], ["delegate_list", "editor_read"])
        self.assertEqual([call[2]["round"] for call in calls], [1, 2])

        # the recursive token carries arguments that are not valid JSON; what
        # the panel gets is the repaired form, so its card can parse them
        self.assertEqual(module.toolcalls.repaired, [RECURSIVE_CALL])
        self.assertTrue(json.loads(calls[1][2]["args"])["_repaired"])

        self.assertEqual(
            [event[2]["call_id"] for event in module.bus.events if event[0] == "tool_result"],
            ["call-1", "call-2"],
        )
        self.assertEqual([event[2] for event in module.bus.events if event[0] == "rounds"], [1, 2])
        self.assertEqual(answer, "the answer")
        self.assertEqual(module.store.tasks["T1"]["rounds"], 2)
        self.assertEqual(module.store.tasks["T1"]["status"], "done")

    async def test_two_recursive_rounds_are_announced_and_counted(self):
        module, delegate = make([[CALL]], sub_tokens=[
            tool_token("call-1"),
            RECURSIVE_CALL,
            tool_token("call-2"),
            RECURSIVE_CALL_2,
            tool_token("call-3"),
            final_token("done for real"),
        ])

        answer = await delegate.run("T1", "hi", "test-model", [], 40)

        calls = [event for event in module.bus.events if event[0] == "tool_call"]
        self.assertEqual([call[2]["call_id"] for call in calls], ["call-1", "call-2", "call-3"])
        self.assertEqual([call[2]["round"] for call in calls], [1, 2, 3])
        self.assertEqual([event[2] for event in module.bus.events if event[0] == "rounds"], [1, 2, 3])
        self.assertEqual(answer, "done for real")
        self.assertEqual(module.store.tasks["T1"]["rounds"], 3)

        # every round core ran inside one process() call still lands in the
        # transcript, call and result side by side
        stored = module.store.saved["T1"]
        self.assertEqual(
            [message["role"] for message in stored],
            ["user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "assistant"],
        )
        self.assertEqual(stored[-1]["content"], "done for real")
        _, dropped = store_module.repair(stored)
        self.assertFalse(dropped)

    async def test_a_recursive_round_hits_the_round_cap(self):
        # core's recursion has no cap of its own, so a runaway delegate used to
        # loop inside a single process() call forever. max_rounds has to bind
        # the recursive rounds as well as the outer ones.
        module, delegate = make([[CALL]], sub_tokens=[
            tool_token("call-1"),
            RECURSIVE_CALL,
            tool_token("call-2"),
            final_token("never reached"),
        ])

        with self.assertRaises(RuntimeError) as raised:
            await delegate.run("T1", "hi", "test-model", [], 1)

        self.assertIn("round cap", str(raised.exception))
        self.assertEqual(module.store.tasks["T1"]["status"], "error")
        self.assertIn("round cap", module.store.tasks["T1"]["last_error"])
        self.assertEqual(module.store.tasks["T1"]["rounds"], 2)

        # the refused round never ran: no card for it, and its token was not
        # even repaired
        self.assertNotIn(
            "call-2",
            [event[2]["call_id"] for event in module.bus.events if event[0] == "tool_call"],
        )
        self.assertEqual(module.toolcalls.repaired, [])

        # core's generator was closed on the way out, not left suspended
        state = module.toolcalls.generations[0]
        self.assertTrue(state["closed"])
        self.assertFalse(state["exhausted"])

        # the round that did run survives the abort, and it is a valid prompt
        stored = module.store.saved["T1"]
        self.assertEqual([message["role"] for message in stored], ["user", "assistant", "tool"])
        _, dropped = store_module.repair(stored)
        self.assertFalse(dropped)

        self.assertEqual(module.bus.stopped[0][0], "T1")
        self.assertIn("round cap", str(module.bus.stopped[0][1]))

    async def test_a_call_is_never_announced_twice(self):
        # a repeated tool_calls token would otherwise put a second card in the
        # panel, and that card could never be answered
        module, delegate = make([[CALL, CALL], [{"type": "content", "content": "ok"}]])

        answer = await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertEqual(
            [event[2]["call_id"] for event in module.bus.events if event[0] == "tool_call"],
            ["call-1"],
        )
        # the round still counts, only the duplicate announcement is dropped
        self.assertEqual([event[2] for event in module.bus.events if event[0] == "rounds"], [1, 2])
        self.assertEqual(answer, "ok")

    async def test_the_cap_counts_this_run_not_the_tasks_lifetime(self):
        # `rounds` is cumulative across continuations (the panel's statistic),
        # while max_rounds is a budget for ONE run. Comparing the cap against
        # the lifetime total would brick a long-lived task after ~4 runs.
        module, delegate = make([[CALL]], sub_tokens=[
            tool_token("call-1"),
            RECURSIVE_CALL,
            tool_token("call-2"),
            final_token("done"),
        ])
        module.store.tasks["T1"]["rounds"] = 30

        answer = await delegate.run("T1", "hi", "test-model", [], 2)

        self.assertEqual(answer, "done")
        self.assertEqual(module.store.tasks["T1"]["status"], "done")
        # cumulative: 30 from before + the 2 rounds this run used
        self.assertEqual(module.store.tasks["T1"]["rounds"], 32)

    async def test_cancellation_keeps_the_rounds_that_ran(self):
        # Stop / a dropped connection during a recursive round: the rounds core
        # already ran inside process() must be folded into the transcript, not
        # silently discarded with the run
        module, delegate = make([[CALL]], sub_tokens=[tool_token("call-1"), RECURSIVE_CALL])
        original = module.toolcalls.process

        def process(request, context=None):
            stream = original(request, context)

            async def failing():
                async for token in stream:
                    yield token
                    if token.get("type") == "tool_calls":
                        raise asyncio.CancelledError()

            return failing()

        module.toolcalls.process = process

        with self.assertRaises(asyncio.CancelledError):
            await delegate.run("T1", "hi", "test-model", [], 40)

        stored = module.store.saved["T1"]
        roles = [message["role"] for message in stored]

        # the round that ran is in the transcript (it would be just the user
        # message without the absorb in the cancellation handler)
        self.assertIn("tool", roles)
        self.assertIn("assistant", roles)
        self.assertEqual(module.store.tasks["T1"]["status"], "interrupted")


# a three-round run whose every round streams reasoning + content before its
# call, so the live feed gets a thinking/text/tool block per round
THREE_ROUNDS = [
    tool_token("call-1"), reasoning_token("r1"), content_token("c1"),
    RECURSIVE_CALL, tool_token("call-2"), reasoning_token("r2"), content_token("c2"),
    RECURSIVE_CALL_2, tool_token("call-3"), reasoning_token("r3"), content_token("c3"),
    final_token("done"),
]


def of_kind(events, kind):
    return [fields for name, _task, fields in events if name == kind]


class LiveFieldTests(unittest.IsolatedAsyncioTestCase):
    """the fields the live block feed needs (step/round), on a multi-round run.

    `step` is the run-local index of the assistant message a chunk belongs to:
    one message = one step = the unit the stored chain shows. `round` stays the
    task's cumulative tool-round counter, so the old event shape is intact.
    """

    def setUp(self):
        self._core_config = sys.modules["core"].config.config
        self._core_config.clear()

    async def test_every_stream_event_carries_its_step_and_round(self):
        module, delegate = make(
            [[reasoning_token("r0"), content_token("preamble"), CALL]],
            sub_tokens=list(THREE_ROUNDS),
        )

        await delegate.run("T1", "hi", "test-model", [], 40)

        # one step per assistant message: the preamble and each round's
        # follow-up, then the last message
        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "reasoning")],
                         [1, 2, 3, 4])
        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "content")],
                         [1, 2, 3, 4])
        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "tool_call")],
                         [1, 2, 3])
        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "tool_result")],
                         [1, 2, 3])

        # `round` is unchanged: the cumulative counter, as before this change
        self.assertEqual([event["round"] for event in of_kind(module.bus.events, "tool_call")],
                         [1, 2, 3])
        self.assertEqual([event["round"] for event in of_kind(module.bus.events, "reasoning")],
                         [0, 1, 2, 3])
        self.assertEqual([event["round"] for event in of_kind(module.bus.events, "tool_result")],
                         [1, 2, 3])

    async def test_a_plain_answer_is_step_one(self):
        module, delegate = make([[reasoning_token("why"), content_token("hello")]])

        await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "reasoning")], [1])
        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "content")], [1])
        self.assertEqual(of_kind(module.bus.events, "tool_call"), [])

    async def test_the_step_starts_at_one_on_a_continuation(self):
        # `rounds` is cumulative across continuations (30 here) but `step` is
        # run-local: the feed of this run starts at 1 however old the task is
        module, delegate = make([
            [reasoning_token("r0"), CALL],
            [content_token("answer")],
        ], sub_tokens=[tool_token("call-1")])
        module.store.tasks["T1"]["rounds"] = 30

        await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "reasoning")], [1])
        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "tool_call")], [1])
        self.assertEqual([event["step"] for event in of_kind(module.bus.events, "content")], [2])
        # the round counter keeps its cumulative meaning
        self.assertEqual([event["round"] for event in of_kind(module.bus.events, "tool_call")], [31])

    async def test_the_announced_events_build_ordered_blocks(self):
        """the contract between runner and bus: replaying what the runner emits
        through the block builder gives one thinking/tool/text run per step."""
        module, delegate = make(
            [[reasoning_token("r0"), content_token("preamble"), CALL]],
            sub_tokens=list(THREE_ROUNDS),
        )

        await delegate.run("T1", "hi", "test-model", [], 40)

        builder = BlockBuilder()
        for kind, task_id, fields in module.bus.events:
            if kind == "rounds":      # set_rounds: not a display event
                continue
            builder.apply({"kind": kind, "task_id": task_id, **fields})

        blocks = builder.export()["blocks"]
        self.assertEqual(
            [block["kind"] for block in blocks],
            ["thinking", "text", "tool"] * 3 + ["thinking", "text"],
        )
        self.assertEqual([block["step"] for block in blocks],
                         [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4])

        # every tool block collapsed on its own result
        self.assertTrue(all(block["state"] == "collapsed"
                            for block in blocks if block["kind"] == "tool"))
        self.assertTrue(all(block["state"] == "collapsed"
                            for block in blocks if block["kind"] == "thinking"))

        state = builder.export()
        self.assertEqual(state["steps"], 4)
        self.assertEqual(state["step"], 4)      # the last answer is still streaming
        self.assertEqual(state["steps_done"], 3)


class TrailingPartialTests(unittest.TestCase):
    """_trailing_partial(): the signature of a follow-up request cut mid-answer."""

    def test_content_only_assistant_message_is_a_cut_answer(self):
        ctx = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "cut mid sen"}]
        self.assertTrue(runner_module.DelegateRunner._trailing_partial(ctx, None))

    def test_assistant_message_with_tool_calls_is_not_a_cut_answer(self):
        ctx = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}]
        self.assertFalse(runner_module.DelegateRunner._trailing_partial(ctx, None))

    def test_the_request_itself_is_never_a_cut_answer(self):
        request = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}
        ctx = [request]
        self.assertFalse(runner_module.DelegateRunner._trailing_partial(ctx, request))

    def test_blank_content_is_not_a_cut_answer(self):
        ctx = [{"role": "assistant", "content": "   "}]
        self.assertFalse(runner_module.DelegateRunner._trailing_partial(ctx, None))

    def test_tool_message_is_not_a_cut_answer(self):
        ctx = [{"role": "tool", "tool_call_id": "c1", "content": "result"}]
        self.assertFalse(runner_module.DelegateRunner._trailing_partial(ctx, None))

    def test_empty_context(self):
        self.assertFalse(runner_module.DelegateRunner._trailing_partial([], None))


class AbsorbTests(unittest.TestCase):
    """_absorb(): the wire context stays clean, the transcript keeps the rest."""

    def test_reasoning_moves_to_metadata_and_leaves_the_stored_copy_clean(self):
        module, delegate = make([[]])
        delegate._model = "some-model"

        ctx = [{"role": "assistant", "content": "x", "reasoning_content": "why"}]
        rich = []
        delegate._absorb(ctx, rich, 3)

        # the stored transcript stays lean; the wire copy keeps the reasoning
        # (keep_reasoning defaults to on, so the provider's prompt cache hits)
        self.assertNotIn("reasoning_content", rich[0])
        self.assertEqual(ctx[0]["reasoning_content"], "why")
        self.assertEqual(rich[0]["_metadata"]["reasoning"], "why")
        self.assertEqual(rich[0]["_metadata"]["round"], 3)
        self.assertEqual(rich[0]["_metadata"]["model"], "some-model")

    def test_with_keep_reasoning_off_the_wire_is_stripped(self):
        module, delegate = make([[]])
        module.config.values["keep_reasoning_in_context"] = False

        ctx = [{"role": "assistant", "content": "x", "reasoning_content": "why"}]
        rich = []
        delegate._absorb(ctx, rich, 1)

        self.assertNotIn("reasoning_content", ctx[0])
        self.assertNotIn("reasoning_content", rich[0])
        self.assertEqual(rich[0]["_metadata"]["reasoning"], "why")

    def test_final_reasoning_is_attached_to_the_last_message_only(self):
        module, delegate = make([[]])
        delegate._model = ""
        ctx = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "done"}]
        rich = [dict(ctx[0])]

        delegate._absorb(ctx, rich, 0, reasoning="thought")

        self.assertEqual(rich[1]["_metadata"]["reasoning"], "thought")
        self.assertNotIn("reasoning", rich[0].get("_metadata", {}))


class ShortTests(unittest.TestCase):
    """_short(): the live panel's truncation for tool args/results."""

    def test_short_text_is_unchanged(self):
        self.assertEqual(runner_module._short("abc", 50), "abc")

    def test_long_text_is_cut_with_an_ellipsis(self):
        cut = runner_module._short("x" * 20, 10)
        self.assertEqual(cut, "x" * 10 + " \u2026")

    def test_none_becomes_empty(self):
        self.assertEqual(runner_module._short(None), "")


if __name__ == "__main__":
    unittest.main()


class UnwrapCallTests(unittest.TestCase):
    """_unwrap_call(): the live feed shows the proxied tool, not tools_call.

    A delegated run loads its tools dynamically, so its calls reach the API as
    `tools_call` with the real tool inside the arguments. The stored transcript
    keeps that raw form; the panel's row must read the proxied one.
    """

    def test_a_proxied_call_is_unwrapped(self):
        name, args = runner_module.DelegateRunner._unwrap_call(
            "tools_call", '{"name": "browser_navigate", "params": {"url": "http://x"}}'
        )
        self.assertEqual(name, "browser_navigate")
        self.assertEqual(json.loads(args), {"url": "http://x"})

    def test_params_given_as_a_json_string_are_passed_through(self):
        name, args = runner_module.DelegateRunner._unwrap_call(
            "tools_call", '{"name": "browser_evaluate", "params": "{\\"expression\\": \\"1\\"}"}'
        )
        self.assertEqual(name, "browser_evaluate")
        self.assertEqual(json.loads(args), {"expression": "1"})

    def test_a_normal_call_is_untouched(self):
        self.assertEqual(
            runner_module.DelegateRunner._unwrap_call("delegate_list", '{"status": "done"}'),
            ("delegate_list", '{"status": "done"}'),
        )

    def test_broken_arguments_keep_the_raw_call(self):
        raw = '{"name": "browser_evaluate", "params": {' + "\n"
        name, args = runner_module.DelegateRunner._unwrap_call("tools_call", raw)
        self.assertEqual(name, "tools_call")
        self.assertEqual(args, raw)

    def test_a_bridge_call_without_a_target_keeps_its_name(self):
        self.assertEqual(
            runner_module.DelegateRunner._unwrap_call("tools_call", '{"params": {}}'),
            ("tools_call", '{"params": {}}'),
        )

    def test_the_emitted_event_carries_the_unwrapped_call(self):
        module, delegate = make([[CALL]], sub_tokens=[
            tool_token("call-1"),
            final_token("done"),
        ])
        # the round-1 token the runner sees is a proxied one
        module.api.script[0] = [{
            "type": "tool_calls",
            "tool_calls": [{
                "id": "call-1",
                "function": {
                    "name": "tools_call",
                    "arguments": '{"name": "browser_status", "params": {}}',
                },
            }],
        }]

        asyncio.run(delegate.run("T1", "hi", "test-model", [], 40))

        emitted = [event for event in module.bus.events if event[0] == "tool_call"][0]
        self.assertEqual(emitted[2]["name"], "browser_status")
        self.assertEqual(json.loads(emitted[2]["args"]), {})


class KeepReasoningTests(unittest.IsolatedAsyncioTestCase):
    """keep_reasoning_in_context: a continuation gets its stored reasoning back.

    The stored transcript keeps reasoning in `_metadata.reasoning` (display
    only), so `to_wire()` never yields it. With the setting on, the rebuilt
    context carries `reasoning_content` again - the same text the provider saw
    during the previous run, so its prompt cache matches instead of reprocessing
    the whole transcript.
    """

    def setUp(self):
        self._core_config = sys.modules["core"].config.config
        self._core_config.clear()

    @staticmethod
    def history():
        return [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "prev", "_metadata": {"reasoning": "because"}},
        ]

    async def test_a_continuation_resends_the_stored_reasoning(self):
        module, delegate = make([[{"type": "content", "content": "ok"}]])

        await delegate.run("T1", "again", "test-model", self.history(), 40)

        sent = module.api.contexts[0]
        assistant = [message for message in sent if message["role"] == "assistant"][0]
        self.assertEqual(assistant.get("reasoning_content"), "because")

    async def test_switched_off_the_continuation_stays_lean(self):
        module, delegate = make([[{"type": "content", "content": "ok"}]])
        module.config.values["keep_reasoning_in_context"] = False

        await delegate.run("T1", "again", "test-model", self.history(), 40)

        sent = module.api.contexts[0]
        assistant = [message for message in sent if message["role"] == "assistant"][0]
        self.assertNotIn("reasoning_content", assistant)


class ToolClockTests(unittest.TestCase):
    """the detach clock belongs to one run of one task, not to the module.

    `seconds_since_tool()` is what the module's detach decision reads. It used
    to be a single attribute on the runner, so a background run starting or
    calling a tool could make a foreground run look tool-less (or tool-active)
    and change whether that foreground call detached.
    """

    def test_a_task_with_no_run_reports_no_tool_call(self):
        runner = runner_module.DelegateRunner(None)
        self.assertIsNone(runner.seconds_since_tool("A"))

    def test_one_tasks_tool_call_does_not_leak_into_another(self):
        runner = runner_module.DelegateRunner(None)
        runner._tool_at["foreground"] = time.monotonic()

        self.assertGreaterEqual(runner.seconds_since_tool("foreground"), 0.0)
        self.assertIsNone(runner.seconds_since_tool("background"))

    def test_a_background_run_starting_does_not_clear_a_foreground_clock(self):
        runner = runner_module.DelegateRunner(None)
        runner._tool_at["foreground"] = time.monotonic()
        runner._tool_at["background"] = None        # run() start of the background task

        self.assertIsNotNone(runner.seconds_since_tool("foreground"))
        self.assertIsNone(runner.seconds_since_tool("background"))

    def test_a_finished_run_forgets_its_clock(self):
        runner = runner_module.DelegateRunner(None)
        runner._tool_at["A"] = time.monotonic()
        runner._tool_at.pop("A", None)              # run() finally

        self.assertIsNone(runner.seconds_since_tool("A"))


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    """diagnostics live behind verbose_logs; a clean run logs nothing by default"""

    def setUp(self):
        self._core_config = sys.modules["core"].config.config
        self._core_config.clear()

    async def test_a_clean_run_logs_nothing_by_default(self):
        module, delegate = make([[{"type": "content", "content": "ok"}]])

        await delegate.run("T1", "hi", "test-model", [], 40)

        self.assertEqual(module.logs, [])

    async def test_verbose_turns_the_timings_on(self):
        module, delegate = make([[{"type": "content", "content": "ok"}]])
        module.config.values["verbose_logs"] = True

        await delegate.run("T1", "hi", "test-model", [], 40)

        messages = [message for _, message in module.logs]
        self.assertTrue(any("start (" in message for message in messages))
        self.assertTrue(any("first token after" in message for message in messages))
        self.assertTrue(any("done in " in message for message in messages))
