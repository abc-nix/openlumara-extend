"""delegate — task storage: index + per-task transcripts.

Everything lives under <data>/delegate/, never inside chats/, so a delegated
conversation can never interfere with a chat's `current` index or its history
files:

    <data>/delegate/index.mp            StorageList(msgpack): task metadata
    <data>/delegate/history/<id>.json   StorageList(json): the transcript

The transcript is the *rich* form: each message keeps a `_metadata` dict with
its timestamp, round number and (for assistant messages) the reasoning that led
to it. Requests are built from the wire form (`to_wire`), which drops
`_metadata` and `reasoning_content`, so nothing display-only reaches the API.
"""

import datetime
import os

import core
import ulid

WIRE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def to_wire(message):
    """strip the display-only parts of a stored message for the API payload"""
    return {key: value for key, value in message.items() if key in WIRE_KEYS}


def repair(messages):
    """drop an incomplete trailing tool round (crash, Stop pressed, API error).

    OpenAI's format requires every `tool_calls` id to be answered by a `tool`
    message immediately after the assistant message. A run that dies mid-round
    leaves a dangling call, which would make the *next* continuation an invalid
    prompt. Truncating at that assistant message is the cheapest valid fix: the
    dropped round is noted in `last_error` and still visible in the live panel.

    returns (messages, dropped: bool)
    """

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue

        needed = {call.get("id") for call in message["tool_calls"] if call.get("id")}
        if not needed:
            return messages, False

        answered = {
            reply.get("tool_call_id")
            for reply in messages[index + 1:]
            if reply.get("role") == "tool"
        }
        if needed - answered:
            return messages[:index], True

        return messages, False

    return messages, False


class DelegateStore:
    def __init__(self):
        self.root = core.get_data_path("delegate")
        self.history_root = os.path.join(self.root, "history")
        os.makedirs(self.history_root, exist_ok=True)
        self.index = core.storage.StorageList("index", "msgpack", path=self.root)

    # ------------------------------------------------------------------ ids
    def new_id(self):
        """an 8-char id shaped like a chat id, guaranteed unique in the index"""
        while True:
            candidate = str(ulid.ULID())[-8:]
            if self.find(candidate) is None:
                return candidate

    # ---------------------------------------------------------------- index
    def all(self):
        """every task, most recently updated first"""
        return sorted(self.index, key=lambda task: task.get("updated", ""), reverse=True)

    def find(self, task_id):
        wanted = str(task_id or "").strip().upper()
        if not wanted:
            return None
        for task in self.index:
            if str(task.get("id", "")).upper() == wanted:
                return task
        return None

    def create(self, title, model, channel_name, chat_id):
        task = {
            "id": self.new_id(),
            "title": title,
            "model": model,
            "status": "running",
            "created": now(),
            "updated": now(),
            "parent_channel": channel_name,
            "parent_chat_id": chat_id,
            "rounds": 0,
            "last_error": None,
        }
        self.index.append(task)
        self.index.save()
        return task

    def update(self, task_id, **fields):
        task = self.find(task_id)
        if task is None:
            return None
        task.update(fields)
        task["updated"] = now()
        self.index.save()
        return task

    def delete(self, task_id):
        """remove a task: its index entry and its transcript file"""
        task = self.find(task_id)
        if task is None:
            return False

        path = os.path.join(self.history_root, f"{task['id']}.json")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

        for position, entry in enumerate(self.index):
            if str(entry.get("id", "")) == str(task["id"]):
                self.index.pop(position)
                break

        self.index.save()
        return True

    def mark_stale(self):
        """nothing can still be running after a restart or a module reload"""
        changed = False
        for task in self.index:
            if task.get("status") == "running":
                task["status"] = "interrupted"
                task["last_error"] = "interrupted (the framework restarted mid-run)"
                changed = True
        if changed:
            self.index.save()

    # ----------------------------------------------------------- transcripts
    def _history(self, task_id):
        return core.storage.StorageList(task_id, "json", path=self.history_root)

    def load(self, task_id):
        """the stored transcript as plain dicts (rich form, with _metadata)"""
        return [dict(message) for message in self._history(task_id)]

    def save(self, task_id, messages):
        store = self._history(task_id)
        store.load([dict(message) for message in messages])
        store.save()
