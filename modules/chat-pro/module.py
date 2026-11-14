# -- AI GENERATED CODE (deepseek-v4.1-flash-1017-maya-s) :: 2026-11-14 harness:OpenLumara

"""chat-pro - extra options for the chat ui.

first feature: fork.

forking takes the open conversation up to and including one message and copies
it into a brand-new chat: the exact same messages (tool calls, tool responses,
attached files and every _metadata field included), the same chat metadata
(character etc.), the same category. the source chat is left untouched, so the
fork becomes an independent branch you can keep talking in while the original
stays as it was.

the button lives in the message action row (leftmost, via the `before_buttons`
slot of chat/message_buttons.html). the copy itself has to happen here: the
webui cannot write a history file, while this route reuses the same Chat and
Messages plumbing the core uses (StorageList over
chats/<channel>/history/<id>.json), so the fork is indistinguishable from a chat
that was written normally.

note: a fork on a user message intentionally keeps that message unanswered -
the fork opens as a ready-to-edit prompt.
"""

import copy

import core

import channels.webui.api as webui


class ChatPro(core.module.Module):

    category = "utilities"

    settings = {
        "enable_webui_extension": {
            "description": "Load this module's WebUI Extensions (the fork button in the chat UI).",
            "default": True
        },
        "enable_fork_button": {
            "description": "Show the fork button in the message action row.",
            "default": True
        },
        "fork_title_prefix": {
            "description": "Prefix of a fork's title, so forks are easy to spot in the chat list.",
            "default": "Fork: "
        },
        "copy_tags": {
            "description": "Copy the source chat's tags onto the fork.",
            "default": True
        },
        "add_fork_tag": {
            "description": "Add a tag of its own to the fork, in addition to any copied tags.",
            "default": True
        },
        "fork_tag": {
            "description": "The tag added to forks.",
            "default": "fork"
        }
    }

    # ----------------
    # helpers
    # ----------------
    def _fork_title(self, title):
        """'<prefix><source title>', clamped like core's auto-titles"""
        prefix = str(self.config.get("fork_title_prefix") or "Fork: ")
        source = str(title or "").strip() or "New chat"
        return f"{prefix}{source}"[:120]

    def _fork_tags(self, tags):
        """the source tags (optionally) plus the fork tag (optionally)"""
        copied = []
        if self.config.get("copy_tags", True):
            copied = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]

        if self.config.get("add_fork_tag", True):
            tag = str(self.config.get("fork_tag") or "fork").strip()
            if tag and tag not in copied:
                copied.append(tag)

        return copied

    # ----------------
    # webui api
    # ----------------
    @webui.route("fork", method="POST")
    async def _route_fork(self, body=None, query=None):
        """copy the open chat up to and including message `index` into a new chat

        body: {"index": <0-based index into the raw message history>}
        returns: {"id": <new chat id>, "title": ..., "messages": <copied count>, "source": <old id>}
        """

        channel = self.channel
        if channel is None:
            return self.result("no active channel", success=False)

        ws_mgr = getattr(channel, "websocket_manager", None)

        # the client disables the button while streaming; this is the real guard
        if ws_mgr is not None and ws_mgr.stream_state != "idle":
            return self.result("a response is still streaming - stop it before forking", success=False)

        chat = channel.context.chat
        if chat.current is None:
            return self.result("no chat is currently loaded", success=False)

        payload = body if isinstance(body, dict) else {}
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            return self.result("missing or invalid message index", success=False)

        source = chat.get()
        history = await chat.messages.get()

        if index < 0 or index >= len(history):
            return self.result(f"message index {index} is out of range (0..{len(history) - 1})", success=False)

        # snapshot everything that belongs to the source chat before chat.new()
        # moves the store onto a fresh chat. deepcopy: the message dicts end up
        # in a different file and must never be shared with the source history
        cut = copy.deepcopy(list(history)[:index + 1])
        source_id = source.get("id")
        category = source.get("category") or "general"
        metadata = copy.deepcopy(source.get("metadata") or {})
        tags = list(source.get("tags") or [])
        title = self._fork_title(source.get("title"))

        new_id = await chat.new(category=category, title=title, metadata=metadata)

        # write the copied history into the new chat's file, exactly like a
        # normally written chat: <channel>/history/<id>.json
        chat.messages.data.load(cut)
        chat.messages.data.save()

        chat.data[chat.current]["tags"] = self._fork_tags(tags)
        chat.data.save()

        self.log("chat-pro", f"forked {source_id} at message {index} -> {new_id} ({len(cut)} messages)")

        if ws_mgr is not None:
            # same as chat_ops.op_chat_new(): every client follows the switch
            ws_mgr.active_chat_id = new_id
            await ws_mgr.broadcast({"type": "chat_switched", "id": new_id})

        return {
            "id": new_id,
            "title": title,
            "messages": len(cut),
            "source": source_id
        }
