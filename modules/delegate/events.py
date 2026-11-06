"""delegate — live event buffer + the live block model for the webui panel.

The webui has no module -> frontend push channel (the core websocket handler
has no default branch and extensions cannot subscribe to it), so the panel
polls `/api/ext/delegate/state?since=<seq>` and this bus answers with only what
it has not delivered yet. One monotonic cursor spans every task, so the panel
needs a single `seq`.

The buffer is capped; once a task finishes the transcript on disk is the
canonical record and the panel reloads it, so losing old events is harmless.

Blocks (the live feed)
----------------------
The panel used to fold the events into one growing object, so every reasoning
chunk of a run became one blob, tool cards stacked, and the order was lost.
The feed is therefore built here, in `emit()`, from the event kind: one ordered
list of blocks per running task, which the panel renders as it comes.

Building it server-side (instead of in the browser) is also what makes the feed
correct for a late joiner: the ring keeps ~1 minute of stream and a panel that
is opened mid-run, switched to another task or refreshed never received the
earlier events, so folding them client-side loses the earlier blocks. The
blocks live in the run state, so any poll returns the whole feed.

    reasoning chunks   append to the open thinking block; it closes when the
                       message moves on (a `content`/`tool_call`, or a later
                       step's reasoning), then collapses to a dot.
    content chunks     append to an open text block, which never collapses. A
                       following `tool_call` does not close it: the tool block
                       simply starts below it.
    tool_call          opens a tool block for its round (one row per call);
                       `tool_result` marks that call done by call_id and the
                       block collapses once every call of the round is done.
    run_note           a small note line, in order.
    run_end/run_error  leave the blocks alone — the panel switches to the
                       stored chain at that point.
"""

import re
import time
from collections import deque

MAX_EVENTS = 2000

# The panel polls this every 300 ms while it is open, so the payload per block
# is capped: only the tail is kept (the interesting end of a stream) and the
# full length travels as `chars`, so the panel can say that it was cut.
BLOCK_TEXT_LIMIT = 8000

# the collapsed thinking dot shows the tail of the reasoning, not its opening
BLOCK_PHRASE_LIMIT = 90

# a runaway run cannot grow one poll's payload without bound (the default
# max_rounds is 40, so ~3 blocks per round never reaches this)
MAX_BLOCKS = 400


def _tail(text, limit):
    """the last `limit` characters of `text`"""
    value = "" if text is None else str(text)
    if limit <= 0 or len(value) <= limit:
        return value
    return value[-limit:]


def _phrase(text, limit=BLOCK_PHRASE_LIMIT):
    """the last non-empty line of a thinking block, tail-capped.

    The collapsed dot reads `Thinking: <phrase>`, so it has to be the end of the
    reasoning - what the model is on about right now - not its opening line.
    """
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        if len(line) <= limit:
            return line
        return "\u2026" + line[-(limit - 1):]
    return ""


def _step(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _pretty_tool(name):
    """`browser_evaluate` -> `Browser: evaluate`, the chat's chip style"""
    head, _, rest = str(name or "").partition("_")
    if not rest:
        return str(name or "tool")
    return f"{head[:1].upper() + head[1:]}: {rest.replace('_', ' ')}"


def _arg_field(raw, field):
    """a top-level string field of a JSON argument blob, via regex.

    The event's `args` is already truncated for the panel (`PANEL_ARGS_LIMIT`),
    so json.loads could fail on a long call; the fields we read sit near the
    start, so a narrow regex is both safe and enough.
    """
    match = re.search(r'"%s"\s*:\s*"([^"]+)"' % re.escape(field), str(raw or ""))
    return match.group(1) if match else ""


def _tool_label(name, args):
    """the display label for one call, unwrapping core's meta tools.

    A delegated run uses dynamic tool loading, so most of its calls arrive as
    `tools_call` with the real tool name inside the arguments, and `tools_load`
    with module names. The stored chain unwraps the same way (core/turns.py
    `_bridge_from_args`); without this every live row would read `tools_call`.
    """
    name = str(name or "")
    raw = str(args or "")

    if name == "tools_call":
        target = _arg_field(raw, "name")
        return _pretty_tool(target) if target else "tools_call"
    if name == "tools_load":
        return "Load tools"
    return _pretty_tool(name)


class BlockBuilder:
    """one run's live feed, built from the events as they are emitted.

    Blocks are only ever appended (so the order of the feed is the order of the
    stream) and each one is updated in place by the events that belong to it.

      thinking  the reasoning of one assistant message; collapses to a dot when
                that message moves on.
      text      the model's normal response text; never collapses.
      tool      the tool calls of one round, one row per call; collapses when
                every call of the round has its result.
      note      a `run_note` line.

    `step` is the run-local index of the assistant message a block belongs to
    (1 for the first message of the run), which is what the panel counts as a
    "step" - the same unit the stored chain shows.
    """

    THINKING = "thinking"
    TEXT = "text"
    TOOL = "tool"
    NOTE = "note"

    def __init__(self, text_limit=BLOCK_TEXT_LIMIT, max_blocks=MAX_BLOCKS,
                 phrase_limit=BLOCK_PHRASE_LIMIT):
        self._text_limit = max(1, int(text_limit))
        self._max_blocks = max(1, int(max_blocks))
        self._phrase_limit = max(1, int(phrase_limit))
        self._blocks = []
        self._next_id = 1
        self._dropped = 0
        self._thinking = None   # the blocks currently taking chunks
        self._text = None
        self._tool = None

    # ------------------------------------------------------------- building
    def _open(self, kind, step=0):
        block = {
            "id": self._next_id,
            "kind": kind,
            "step": _step(step),
            "state": "streaming",
            "text": "",
            "chars": 0,
            "truncated": False,
        }
        if kind == self.TOOL:
            block["calls"] = []
        elif kind == self.THINKING:
            block["phrase"] = ""

        self._next_id += 1
        self._blocks.append(block)
        self._trim()
        return block

    def _trim(self):
        """drop the oldest blocks once the feed outgrows its cap"""
        while len(self._blocks) > self._max_blocks:
            dropped = self._blocks.pop(0)
            self._dropped += 1
            for name in ("_thinking", "_text", "_tool"):
                if getattr(self, name) is dropped:
                    setattr(self, name, None)

    def _close(self, block):
        """settle a block: text keeps rendering, the rest becomes a dot"""
        if block is None:
            return
        block["state"] = "done" if block["kind"] == self.TEXT else "collapsed"

    def _append(self, block, chunk):
        """add streamed text, keeping only the tail of the block"""
        text = "" if chunk is None else str(chunk)
        if not text:
            return

        block["chars"] += len(text)
        merged = block["text"] + text
        if len(merged) > self._text_limit:
            merged = merged[-self._text_limit:]
            block["truncated"] = True
        block["text"] = merged

        if block["kind"] == self.THINKING:
            block["phrase"] = _phrase(block["text"], self._phrase_limit)

    def _retire_tool(self, step):
        """a tool block ends once the run moves on to a later step

        Normally its last `tool_result` closed it already; this covers a round
        that never answered (Stop, an error) - without it the block would stay
        "streaming" forever and render as an open body.
        """
        if self._tool is not None and self._tool["step"] < _step(step):
            self._close(self._tool)
            self._tool = None

    @staticmethod
    def _all_done(block):
        calls = block.get("calls") or []
        return bool(calls) and all(call.get("done") for call in calls)

    def _find_call(self, call_id):
        """(block, call) of a call id, newest first; id-less matches the newest
        call that is still waiting for its result"""
        for block in reversed(self._blocks):
            if block["kind"] != self.TOOL:
                continue
            for call in reversed(block.get("calls") or []):
                if not call.get("done") and (not call_id or call["call_id"] == call_id):
                    return block, call
        return None, None

    # ---------------------------------------------------------------- events
    def apply(self, event):
        """fold one bus event into the feed"""
        if not event:
            return

        kind = event.get("kind")
        step = _step(event.get("step"))

        if kind == "reasoning":
            # a new thinking block means the previous message is done: its text
            # block (if any) settles now, and a tool block of that message
            # collapses even if a call never answered
            if self._text is not None:
                self._close(self._text)
                self._text = None
            self._retire_tool(step)

            if self._thinking is None or self._thinking["step"] != step:
                self._close(self._thinking)
                self._thinking = self._open(self.THINKING, step)
            self._append(self._thinking, event.get("text"))
            return

        if kind == "content":
            # reasoning ends the moment the message starts speaking
            if self._thinking is not None:
                self._close(self._thinking)
                self._thinking = None
            self._retire_tool(step)

            if self._text is None or self._text["step"] != step:
                self._close(self._text)
                self._text = self._open(self.TEXT, step)
            self._append(self._text, event.get("text"))
            return

        if kind == "tool_call":
            # the call starts below whatever the model said first: the text
            # block stays open, the tool block is a new one
            if self._thinking is not None:
                self._close(self._thinking)
                self._thinking = None
            self._retire_tool(step)

            if self._tool is None:
                self._tool = self._open(self.TOOL, step)
            raw_name = str(event.get("name") or "")
            raw_args = _tail(event.get("args"), self._text_limit)
            self._tool["calls"].append({
                "call_id": str(event.get("call_id") or ""),
                "name": raw_name,                       # raw, unwrapped name (debugging)
                "label": _tool_label(raw_name, raw_args),  # what the feed shows
                "args": raw_args,
                "result": "",
                "done": False,
            })
            return

        if kind == "tool_result":
            block, call = self._find_call(str(event.get("call_id") or ""))
            if call is None:
                return      # nothing was announced for that id: nothing to close
            call["result"] = _tail(event.get("text"), self._text_limit)
            call["done"] = True
            if self._all_done(block):
                self._close(block)
                if self._tool is block:
                    self._tool = None
            return

        if kind == "run_note":
            block = self._open(self.NOTE, 0)
            self._append(block, event.get("text"))
            block["state"] = "done"
            return

        # run_start/run_end/run_error and anything else do not shape the feed

    # ------------------------------------------------------------ reading it
    def steps(self):
        """steps this run has started: one per assistant message with content,
        which is the number the stored chain shows for the same turn"""
        return len({block["step"] for block in self._blocks if block["step"] > 0})

    def running_step(self):
        """the step still streaming, or 0 when nothing is in flight"""
        live = [
            block["step"]
            for block in (self._thinking, self._text, self._tool)
            if block is not None and block["state"] == "streaming"
        ]
        return max(live) if live else 0

    def export(self):
        """the feed as the panel reads it (a copy: the builder keeps ownership)"""
        blocks = []
        for block in self._blocks:
            copy = dict(block)
            if "calls" in copy:
                copy["calls"] = [dict(call) for call in block["calls"]]
            blocks.append(copy)

        steps = self.steps()
        running = self.running_step()
        return {
            "blocks": blocks,
            "steps": steps,
            "steps_done": steps - 1 if running else steps,
            "step": running,
            "dropped": self._dropped,
        }


class EventBus:
    def __init__(self, cap=MAX_EVENTS):
        self._seq = 0
        self._events = deque(maxlen=cap)
        self.running = {}

    # ---------------------------------------------------------------- jobs
    def start(self, task_id, title, model, rounds=0):
        self.running[task_id] = {
            "task_id": task_id,
            "title": title,
            "model": model,
            "rounds": rounds,
            "started": int(time.time()),
            "feed": BlockBuilder(),
        }
        self.emit(task_id, "run_start", title=title, model=model, rounds=rounds)

    def stop(self, task_id, error=None):
        self.running.pop(task_id, None)
        if error:
            self.emit(task_id, "run_error", text=str(error))
        else:
            self.emit(task_id, "run_end")

    def set_rounds(self, task_id, rounds):
        job = self.running.get(task_id)
        if job is not None:
            job["rounds"] = rounds

    def running_ids(self):
        """the ids of the runs in flight (cheap: no feed copy)"""
        return list(self.running.keys())

    def running_list(self):
        """the running runs, each with its live feed (blocks/steps)"""
        jobs = []
        for job in self.running.values():
            entry = {
                key: job[key]
                for key in ("task_id", "title", "model", "rounds", "started")
            }
            feed = job.get("feed")
            if feed is not None:
                entry.update(feed.export())
            jobs.append(entry)
        return jobs

    # -------------------------------------------------------------- events
    def emit(self, task_id, kind, **payload):
        self._seq += 1
        event = {
            "seq": self._seq,
            "task_id": task_id,
            "kind": kind,
            "ts": int(time.time()),
        }
        event.update(payload)
        self._events.append(event)

        # the feed is built here, not in the browser: any poll can then answer
        # with the whole run so far, for a panel opened mid-run or refreshed
        job = self.running.get(task_id)
        if job is not None:
            feed = job.get("feed")
            if feed is not None:
                feed.apply(event)
        return event

    def since(self, cursor):
        """(events newer than the cursor, current seq)"""
        try:
            cursor = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            cursor = 0
        events = [event for event in self._events if event["seq"] > cursor]
        return events, self._seq
