"""delegate runner tests: one run's loop, without an OpenLumara boot.

The runner only talks to the framework through a handful of objects (the API's
stream, the tool-call manager, the store, the event bus), so the loop can be
driven with fakes: no network, no model, no restart.

Run from the repo root:

    python3 -m unittest discover -s modules/delegate/tests -v
"""

import sys
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


CALL = {
    "type": "tool_calls",
    "tool_calls": [
        {"id": "call-1", "function": {"name": "delegate_list", "arguments": "{}"}}
    ],
}


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
    """process() answers a round with a tool result and, optionally, a final message."""

    def __init__(self, final_message=None):
        self.final_message = final_message
        self.requests = []

    async def _build_recursive_request(self, token, content, reasoning):
        request = {
            "role": "assistant",
            "content": "".join(content) or None,
            "tool_calls": list(token.get("tool_calls") or []),
        }
        self.requests.append(request)
        return request

    def process(self, request, context=None):
        final_message = self.final_message

        async def stream():
            yield {"type": "tool", "tool_call_id": "call-1", "content": "tool output"}
            if final_message:
                yield {"type": "final", "content": dict(final_message)}

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


class FakeModule:
    def __init__(self, script, final_message=None):
        self.api = FakeAPI(script)
        self.toolcalls = FakeToolCalls(final_message)
        self.store = FakeStore()
        self.bus = FakeBus()
        self.manager = types.SimpleNamespace(
            API=self.api, tools=[], log=lambda *args: None
        )
        self.channel = types.SimpleNamespace(tc_manager=self.toolcalls)
        self.logs = []

    def log(self, category, message):
        self.logs.append((category, message))


def make(script, final_message=None):
    module = FakeModule(script, final_message)
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

    def test_reasoning_moves_to_metadata_and_leaves_the_wire_clean(self):
        module, delegate = make([[]])
        delegate._model = "some-model"

        ctx = [{"role": "assistant", "content": "x", "reasoning_content": "why"}]
        rich = []
        delegate._absorb(ctx, rich, 3)

        self.assertNotIn("reasoning_content", ctx[0])
        self.assertEqual(rich[0]["_metadata"]["reasoning"], "why")
        self.assertEqual(rich[0]["_metadata"]["round"], 3)
        self.assertEqual(rich[0]["_metadata"]["model"], "some-model")

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
