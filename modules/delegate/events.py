"""delegate — live event buffer for the webui panel.

The webui has no module -> frontend push channel (the core websocket handler
has no default branch and extensions cannot subscribe to it), so the panel
polls `/api/ext/delegate/state?since=<seq>` and this bus answers with only what
it has not delivered yet. One monotonic cursor spans every task, so the panel
needs a single `seq`.

The buffer is capped; once a task finishes the transcript on disk is the
canonical record and the panel reloads it, so losing old events is harmless.
"""

import time
from collections import deque

MAX_EVENTS = 2000


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

    def running_list(self):
        return list(self.running.values())

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
        return event

    def since(self, cursor):
        """(events newer than the cursor, current seq)"""
        try:
            cursor = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            cursor = 0
        events = [event for event in self._events if event["seq"] > cursor]
        return events, self._seq
