"""delegate event-bus tests: the live block model, without an OpenLumara boot.

events.py imports nothing from `core`, so the whole feed (block order, collapse
rules, call matching, step counting, capping) can be driven directly: a script
of events in, an ordered list of blocks out.

Run from the repo root:

    python3 -m unittest discover -s modules/delegate/tests -v
"""

import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(MODULE_DIR.parent))

from delegate.events import BlockBuilder, EventBus, _tool_label  # noqa: E402


def feed(events, **kwargs):
    """run a scripted event list through one builder"""
    builder = BlockBuilder(**kwargs)
    for event in events:
        builder.apply(event)
    return builder


def thinking(text, step=1):
    return {"kind": "reasoning", "text": text, "step": step}


def content(text, step=1):
    return {"kind": "content", "text": text, "step": step}


def call(call_id, name="editor_read", args='{"path": "a.md"}', step=1):
    return {"kind": "tool_call", "call_id": call_id, "name": name, "args": args, "step": step}


def result(call_id, text="ok", step=1):
    return {"kind": "tool_result", "call_id": call_id, "text": text, "step": step}


def kinds(builder):
    return [block["kind"] for block in builder.export()["blocks"]]


class OrderTests(unittest.TestCase):
    """the feed is ordered: each new block appears below the previous one."""

    def test_two_rounds_and_an_answer_give_ordered_blocks(self):
        builder = feed([
            thinking("round one thinking"), content("let me look", step=1),
            call("c1", step=1), result("c1", step=1),
            thinking("round two thinking", step=2), call("c2", step=2),
            result("c2", step=2),
            content("the answer", step=3),
        ])

        self.assertEqual(
            kinds(builder), ["thinking", "text", "tool", "thinking", "tool", "text"]
        )
        self.assertEqual([block["step"] for block in builder.export()["blocks"]],
                         [1, 1, 1, 2, 2, 3])

    def test_chunks_of_one_step_append_to_one_block(self):
        builder = feed([
            thinking("one "), thinking("two"),
            content("hel"), content("lo"),
        ])
        blocks = builder.export()["blocks"]

        self.assertEqual([block["kind"] for block in blocks], ["thinking", "text"])
        self.assertEqual(blocks[0]["text"], "one two")
        self.assertEqual(blocks[1]["text"], "hello")

    def test_every_block_gets_a_unique_id(self):
        builder = feed([thinking("a"), call("c1"), result("c1"), content("b", step=2)])
        ids = [block["id"] for block in builder.export()["blocks"]]

        self.assertEqual(len(ids), len(set(ids)))


class ThinkingTests(unittest.TestCase):
    """a thinking block streams, then collapses to a dot with its last phrase."""

    def test_thinking_is_streaming_while_it_streams(self):
        builder = feed([thinking("thinking about it")])
        block = builder.export()["blocks"][0]

        self.assertEqual(block["state"], "streaming")
        self.assertEqual(block["phrase"], "thinking about it")

    def test_thinking_collapses_when_the_message_starts_speaking(self):
        builder = feed([thinking("hmm"), content("sure", step=1)])
        block = builder.export()["blocks"][0]

        self.assertEqual(block["state"], "collapsed")
        self.assertEqual(block["text"], "hmm")

    def test_thinking_collapses_when_its_tool_call_arrives(self):
        builder = feed([thinking("need a file"), call("c1")])

        self.assertEqual(builder.export()["blocks"][0]["state"], "collapsed")

    def test_the_dot_reads_the_last_phrase_not_the_first(self):
        builder = feed([thinking("first line\nstill going\n  the last phrase  ")])

        self.assertEqual(builder.export()["blocks"][0]["phrase"], "the last phrase")

    def test_a_long_line_keeps_its_tail(self):
        line = "x" * 20 + " the end"
        builder = feed([thinking(line)], phrase_limit=12)
        phrase = builder.export()["blocks"][0]["phrase"]

        self.assertTrue(phrase.startswith("\u2026"))
        self.assertTrue(phrase.endswith("the end"))
        self.assertEqual(len(phrase), 12)

    def test_a_new_step_starts_a_new_thinking_block(self):
        builder = feed([
            thinking("round one"), call("c1", step=1), result("c1", step=1),
            thinking("round two", step=2),
        ])
        thinking_blocks = [b for b in builder.export()["blocks"] if b["kind"] == "thinking"]

        self.assertEqual([b["step"] for b in thinking_blocks], [1, 2])
        self.assertEqual(thinking_blocks[0]["state"], "collapsed")
        self.assertEqual(thinking_blocks[1]["state"], "streaming")


class TextTests(unittest.TestCase):
    """a normal response renders: it never collapses, and a tool does not close it."""

    def test_text_never_collapses(self):
        builder = feed([content("all done", step=1)])
        self.assertEqual(builder.export()["blocks"][0]["state"], "streaming")

        builder = feed([
            content("preamble", step=1), call("c1", step=1), result("c1", step=1),
            thinking("next", step=2),
        ])
        text = [b for b in builder.export()["blocks"] if b["kind"] == "text"][0]
        self.assertEqual(text["state"], "done")
        self.assertEqual(text["text"], "preamble")

    def test_a_tool_call_starts_below_the_text_instead_of_closing_it(self):
        builder = feed([content("let me check", step=1), call("c1", step=1)])

        self.assertEqual(kinds(builder), ["text", "tool"])
        self.assertEqual(builder.export()["blocks"][0]["state"], "streaming")

    def test_text_of_a_later_step_is_a_new_block(self):
        builder = feed([
            content("first", step=1), thinking("hmm", step=2), content("second", step=2),
        ])
        texts = [b for b in builder.export()["blocks"] if b["kind"] == "text"]

        self.assertEqual([text["text"] for text in texts], ["first", "second"])

    def test_a_continued_answer_stays_in_one_block(self):
        # a cut answer is re-asked on the same step, so the continuation joins it
        builder = feed([content("cut mid sen", step=3), content("tence", step=3)])
        texts = [b for b in builder.export()["blocks"] if b["kind"] == "text"]

        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0]["text"], "cut mid sentence")


class ToolTests(unittest.TestCase):
    """a tool block runs until every call of the round has its result."""

    def test_a_call_is_streaming_until_its_result_arrives(self):
        builder = feed([call("c1")])
        block = builder.export()["blocks"][0]

        self.assertEqual(block["kind"], "tool")
        self.assertEqual(block["state"], "streaming")
        self.assertEqual(block["calls"][0]["name"], "editor_read")
        self.assertEqual(block["calls"][0]["args"], '{"path": "a.md"}')
        self.assertFalse(block["calls"][0]["done"])

    def test_the_result_lands_on_its_call_and_collapses_the_block(self):
        builder = feed([call("c1"), result("c1", text="file contents")])
        block = builder.export()["blocks"][0]

        self.assertEqual(block["state"], "collapsed")
        self.assertTrue(block["calls"][0]["done"])
        self.assertEqual(block["calls"][0]["result"], "file contents")

    def test_several_calls_of_one_round_are_rows_of_one_block(self):
        builder = feed([
            call("c1", name="editor_read"), call("c2", name="editor_list"),
            result("c2", text="two"), result("c1", text="one"),
        ])
        blocks = builder.export()["blocks"]

        self.assertEqual(len(blocks), 1)
        self.assertEqual([c["call_id"] for c in blocks[0]["calls"]], ["c1", "c2"])
        self.assertEqual([c["result"] for c in blocks[0]["calls"]], ["one", "two"])
        self.assertEqual(blocks[0]["state"], "collapsed")

    def test_the_block_stays_open_while_one_call_is_unanswered(self):
        builder = feed([call("c1"), call("c2"), result("c1")])

        self.assertEqual(builder.export()["blocks"][0]["state"], "streaming")

    def test_a_result_for_an_unknown_call_is_ignored(self):
        builder = feed([call("c1"), result("nope")])
        block = builder.export()["blocks"][0]

        self.assertEqual(block["state"], "streaming")
        self.assertFalse(block["calls"][0]["done"])

    def test_a_result_without_an_id_closes_the_newest_waiting_call(self):
        builder = feed([call(""), call("c2"), result("")])

        self.assertTrue(builder.export()["blocks"][0]["calls"][1]["done"])
        self.assertFalse(builder.export()["blocks"][0]["calls"][0]["done"])

    def test_a_later_step_retires_a_block_that_never_answered(self):
        # Stop / an error leaves a call without a result: the block must not keep
        # rendering as an open body when the run moves on
        builder = feed([call("c1"), thinking("moving on", step=2)])
        tool = [b for b in builder.export()["blocks"] if b["kind"] == "tool"][0]

        self.assertEqual(tool["state"], "collapsed")
        self.assertFalse(tool["calls"][0]["done"])

    def test_a_call_of_a_later_step_is_a_new_block(self):
        builder = feed([
            call("c1", step=1), result("c1", step=1),
            call("c2", step=2), result("c2", step=2),
        ])
        tools = [b for b in builder.export()["blocks"] if b["kind"] == "tool"]

        self.assertEqual([t["step"] for t in tools], [1, 2])


class NoteAndStepTests(unittest.TestCase):
    """run_note is a line in order; the step count matches the stored chain."""

    def test_a_run_note_is_a_line_in_order(self):
        builder = feed([
            thinking("a", step=1), call("c1", step=1), result("c1", step=1),
            {"kind": "run_note", "text": "the response was interrupted — continuing"},
            content("b", step=2),
        ])
        blocks = builder.export()["blocks"]

        self.assertEqual([block["kind"] for block in blocks],
                         ["thinking", "tool", "note", "text"])
        self.assertEqual(blocks[2]["text"], "the response was interrupted — continuing")
        self.assertEqual(blocks[2]["state"], "done")

    def test_a_note_does_not_count_as_a_step(self):
        builder = feed([{"kind": "run_note", "text": "note"}])

        self.assertEqual(builder.steps(), 0)
        self.assertEqual(builder.running_step(), 0)

    def test_run_boundaries_do_not_change_the_feed(self):
        builder = feed([
            {"kind": "run_start"}, thinking("a", step=1),
            {"kind": "run_error", "text": "boom"},
        ])

        self.assertEqual(kinds(builder), ["thinking"])

    def test_two_rounds_plus_the_answer_are_three_steps(self):
        builder = feed([
            thinking("one", step=1), call("c1", step=1), result("c1", step=1),
            thinking("two", step=2), call("c2", step=2), result("c2", step=2),
            content("the answer", step=3),
        ])
        state = builder.export()

        self.assertEqual(state["steps"], 3)
        self.assertEqual(state["step"], 3)          # the answer is still streaming
        self.assertEqual(state["steps_done"], 2)

    def test_a_running_tool_step_is_the_in_flight_one(self):
        builder = feed([
            thinking("one", step=1), call("c1", step=1), result("c1", step=1),
            thinking("two", step=2), call("c2", step=2),
        ])
        state = builder.export()

        self.assertEqual(state["steps"], 2)
        self.assertEqual(state["step"], 2)
        self.assertEqual(state["steps_done"], 1)

    def test_steps_are_counted_per_assistant_message_not_per_event(self):
        builder = feed([content("a", step=1), content("b", step=1), content("c", step=1)])

        self.assertEqual(builder.steps(), 1)


class CapTests(unittest.TestCase):
    """what a poll ships is capped, and it says when it cut"""

    def test_text_keeps_its_tail_and_reports_the_full_length(self):
        builder = feed([thinking("a" * 60)], text_limit=10)
        block = builder.export()["blocks"][0]

        self.assertEqual(block["text"], "a" * 10)
        self.assertEqual(block["chars"], 60)
        self.assertTrue(block["truncated"])

    def test_short_text_is_untouched(self):
        builder = feed([thinking("short")], text_limit=10)
        block = builder.export()["blocks"][0]

        self.assertEqual(block["text"], "short")
        self.assertFalse(block["truncated"])

    def test_the_feed_drops_old_blocks_past_its_cap(self):
        script = []
        for step in range(1, 6):
            script += [thinking("t%d" % step, step=step), call("c%d" % step, step=step),
                       result("c%d" % step, step=step)]

        builder = feed(script, max_blocks=4)
        state = builder.export()

        # two blocks per step (its thinking block, then its tool block)
        self.assertEqual(len(state["blocks"]), 4)
        self.assertEqual(state["dropped"], 6)
        # the newest blocks survive, in order
        self.assertEqual([block["step"] for block in state["blocks"]], [4, 4, 5, 5])
        self.assertEqual([block["kind"] for block in state["blocks"]],
                         ["thinking", "tool", "thinking", "tool"])


class BusTests(unittest.TestCase):
    """the bus feeds the builder from emit() and hands the feed out per run"""

    def test_emit_builds_the_feed_of_a_running_task(self):
        bus = EventBus()
        bus.start("T1", title="t", model="m")
        bus.emit("T1", "reasoning", text="thinking", step=1)
        bus.emit("T1", "tool_call", call_id="c1", name="editor_read", args="{}", step=1)
        bus.emit("T1", "tool_result", call_id="c1", text="ok", step=1)

        job = bus.running_list()[0]
        self.assertEqual(job["task_id"], "T1")
        self.assertEqual([block["kind"] for block in job["blocks"]], ["thinking", "tool"])
        self.assertEqual(job["steps"], 1)
        self.assertEqual(job["steps_done"], 1)
        self.assertEqual(job["step"], 0)

    def test_events_keep_their_cursor(self):
        bus = EventBus()
        bus.start("T1", title="t", model="m")
        events, seq = bus.since(0)

        self.assertEqual(events[0]["kind"], "run_start")
        self.assertEqual(seq, 1)
        self.assertEqual(bus.since(seq)[0], [])

    def test_a_stopped_run_leaves_no_feed_behind(self):
        bus = EventBus()
        bus.start("T1", title="t", model="m")
        bus.emit("T1", "content", text="hi", step=1)
        bus.stop("T1")

        self.assertEqual(bus.running_list(), [])
        # the events (run_start, content, run_end) are still there for the panel
        self.assertEqual([event["kind"] for event in bus.since(0)[0]],
                         ["run_start", "content", "run_end"])

    def test_set_rounds_is_unchanged(self):
        bus = EventBus()
        bus.start("T1", title="t", model="m")
        bus.set_rounds("T1", 7)

        self.assertEqual(bus.running_list()[0]["rounds"], 7)


if __name__ == "__main__":
    unittest.main()


class ToolLabelTests(unittest.TestCase):
    """a call's display label: core's meta tools are unwrapped, like the chain.

    A delegated run loads tools dynamically, so its calls arrive as `tools_call`
    with the real tool name in the arguments. The stored chain unwraps them
    (core/turns.py `_bridge_from_args`); the live feed must too, or every row
    reads `tools_call`.
    """

    def test_meta_tool_call_is_unwrapped(self):
        self.assertEqual(
            _tool_label("tools_call", '{"name": "browser_evaluate", "params": {"expression": "1"}}'),
            "Browser: evaluate",
        )

    def test_a_truncated_argument_blob_still_unwraps(self):
        # the event's args are cut for the panel (PANEL_ARGS_LIMIT): the target
        # name sits near the start, so it survives the cut
        self.assertEqual(
            _tool_label("tools_call", '{"name": "editor_read", "params": {"path": "/home/a'),
            "Editor: read",
        )

    def test_tools_load_reads_as_a_human(self):
        self.assertEqual(_tool_label("tools_load", '{"module_names": ["browser"]}'), "Load tools")

    def test_a_normal_tool_is_pretty_printed(self):
        self.assertEqual(_tool_label("delegate_list", "{}"), "Delegate: list")

    def test_a_name_without_an_underscore_is_kept(self):
        self.assertEqual(_tool_label("ping", "{}"), "ping")

    def test_an_unparseable_bridge_call_keeps_its_name(self):
        self.assertEqual(_tool_label("tools_call", "not json"), "tools_call")

    def test_the_call_keeps_both_names(self):
        builder = feed([call("c1", name="tools_call",
                             args='{"name": "browser_navigate", "params": {"url": "x"}}')])
        entry = builder.export()["blocks"][0]["calls"][0]
        self.assertEqual(entry["name"], "tools_call")          # raw, for debugging
        self.assertEqual(entry["label"], "Browser: navigate")  # what the feed shows
