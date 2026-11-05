"""delegate store tests: transcript repair and the wire form.

Pure logic, no OpenLumara boot. store.py imports `core` only for the data
paths (DelegateStore), which these tests never construct, so a bare stub is
enough to load it.

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
    sys.modules["core"] = _core

sys.path.insert(0, str(MODULE_DIR.parent))

from delegate import store  # noqa: E402  (needs the stub above first)


def assistant(*call_ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "function": {"name": "delegate_list", "arguments": "{}"}}
            for call_id in call_ids
        ],
    }


def tool(call_id):
    return {"role": "tool", "tool_call_id": call_id, "content": "result"}


class RepairTests(unittest.TestCase):
    """repair() drops an incomplete trailing tool round, and nothing else."""

    def test_complete_tool_round_is_kept(self):
        messages = [{"role": "user", "content": "go"}, assistant("c1"), tool("c1")]
        kept, dropped = store.repair(messages)
        self.assertFalse(dropped)
        self.assertEqual(kept, messages)

    def test_dangling_round_truncates_at_its_assistant_message(self):
        # the shape left behind by Stop / a timeout in the middle of a tool round
        messages = [
            {"role": "user", "content": "go"},
            assistant("c1"),
            tool("c1"),
            {"role": "assistant", "content": "next"},
            assistant("c2"),
        ]
        kept, dropped = store.repair(messages)
        self.assertTrue(dropped)
        self.assertEqual(len(kept), 4)
        self.assertEqual(kept[-1]["content"], "next")

    def test_partially_answered_round_is_dropped(self):
        # one of two calls answered is still an invalid prompt
        messages = [assistant("c1", "c2"), tool("c1")]
        kept, dropped = store.repair(messages)
        self.assertTrue(dropped)
        self.assertEqual(kept, [])

    def test_plain_conversation_is_untouched(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "sure"},
        ]
        kept, dropped = store.repair(messages)
        self.assertFalse(dropped)
        self.assertIs(kept, messages)

    def test_calls_without_any_id_are_ignored(self):
        messages = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x"}}]}]
        kept, dropped = store.repair(messages)
        self.assertFalse(dropped)
        self.assertEqual(kept, messages)

    def test_empty_transcript_is_fine(self):
        self.assertEqual(store.repair([]), ([], False))

    def test_only_the_last_dangling_round_matters(self):
        # an early dangling round cannot happen in practice, but if it did the
        # last assistant-with-calls still decides: complete -> keep everything
        messages = [assistant("c1"), tool("c1"), assistant("c2"), tool("c2")]
        kept, dropped = store.repair(messages)
        self.assertFalse(dropped)
        self.assertEqual(kept, messages)


class ToWireTests(unittest.TestCase):
    """to_wire() keeps the API-relevant keys and drops display-only ones."""

    def test_metadata_and_reasoning_are_dropped(self):
        message = {
            "role": "assistant",
            "content": "x",
            "tool_calls": [],
            "_metadata": {"reasoning": "why", "timestamp": 1, "round": 0},
            "reasoning_content": "why",
        }
        self.assertEqual(
            store.to_wire(message),
            {"role": "assistant", "content": "x", "tool_calls": []},
        )

    def test_tool_messages_keep_their_call_id_and_name(self):
        message = {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "delegate_list",
            "content": "ok",
            "_metadata": {},
        }
        self.assertEqual(
            store.to_wire(message),
            {"role": "tool", "tool_call_id": "c1", "name": "delegate_list", "content": "ok"},
        )

    def test_the_stored_message_is_not_mutated(self):
        message = {"role": "user", "content": "hi", "_metadata": {"round": 0}}
        store.to_wire(message)
        self.assertIn("_metadata", message)


if __name__ == "__main__":
    unittest.main()
