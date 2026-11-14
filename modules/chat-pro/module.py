# -- AI GENERATED CODE (deepseek-v4.1-flash-1017-maya-s) :: 2026-11-14 harness:OpenLumara

"""chat-pro - extra options for the chat ui.

features:

1. fork - copy the open conversation up to one message into a brand-new chat
   (own chat_id, own panel entry, independent forever).

2. branch - an alternative continuation inside the *same* chat. the first
   branch turns the chat into a tree (see tree.py); after that the history file
   is the materialised active path and the tree holds the whole lineage, one
   copy of the shared prefix. `> ` on a message starts a variant:
     - assistant message: regenerate it, the old answer stays as variant 1
     - user message: edit it as a new variant (saving streams the reply)
   core's own Regenerate/Delete become branch-preserving for free, since the
   dropped tail is still in the tree.

design notes that keep this safe:
  - a plain chat stays a plain chat; tree.py only exists after the first branch
  - the tree is written atomically (temp + os.replace), so a crash can never
    truncate a chat
  - every switch resets token_usage / token_usage_mark / using_api_token_data,
    the same bookkeeping context.compress() resets
  - message ids ride in _metadata.chatpro_id, which context.get() strips before
    the API call and which core's message_edit keeps, so reconcile is a diff by
    id instead of by index
  - the history file (not the tree) is always what core reads, so with the
    module disabled the chat is simply its last active path
"""

import base64
import copy

import core

import channels.webui.api as webui

from .tree import ChatTree


class ChatPro(core.module.Module):

    category = "utilities"

    settings = {
        "enable_webui_extension": {
            "description": "Load this module's WebUI Extensions (fork/branch controls, the branches tab).",
            "default": True
        },
        "enable_fork_button": {
            "description": "Show the fork button in the message action row (copy the chat up to that message into a new chat).",
            "default": True
        },
        "enable_branch_controls": {
            "description": "Show the branch control in the message action row (> to start a variant, < n/m > to navigate them).",
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
        prefix = str(self.config.get("fork_title_prefix") or "Fork: ")
        source = str(title or "").strip() or "New chat"
        return f"{prefix}{source}"[:120]

    def _fork_tags(self, tags):
        copied = []
        if self.config.get("copy_tags", True):
            copied = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]

        if self.config.get("add_fork_tag", True):
            tag = str(self.config.get("fork_tag") or "fork").strip()
            if tag and tag not in copied:
                copied.append(tag)

        return copied

    def _tree(self, chat_id):
        return ChatTree(self.channel, chat_id)

    def _check_idle(self, ws_mgr):
        """every branch operation rewrites the open chat: never while streaming"""
        if ws_mgr is not None and ws_mgr.stream_state != "idle":
            return "a response is still streaming - stop it before branching"
        return None

    async def _build_variant_message(self, channel, content, files, filenames):
        """the user message of an edited variant. deliberately carries no
        chatpro_id, so the node it becomes is a *sibling* of the original."""
        file_list = files if isinstance(files, list) else []
        files_dict = None
        if file_list:
            files_dict = {
                entry.get("name"): base64.b64decode(entry.get("data") or "")
                for entry in file_list
                if isinstance(entry, dict) and entry.get("name")
            }

        if isinstance(content, list):
            blocks = [block for block in content if isinstance(block, dict)]
            names = list(filenames or [])
            if files_dict:
                processed = await channel._process_multimodal(message=None, files=files_dict)
                blocks = blocks + list(processed.get("content") or [])
                names = names + list((processed.get("_metadata") or {}).get("filenames") or [])
            message = {"role": "user", "content": blocks}
            if names:
                message["_metadata"] = {"filenames": names}
            return message

        text = "" if content is None else str(content)
        if files_dict:
            return await channel._process_multimodal(message=text, files=files_dict)
        return text

    # ----------------
    # fork
    # ----------------
    @webui.route("fork", method="POST")
    async def _route_fork(self, body=None, query=None):
        """copy the open chat up to and including message `index` into a new chat"""
        channel = self.channel
        if channel is None:
            return self.result("no active channel", success=False)

        ws_mgr = getattr(channel, "websocket_manager", None)
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

        cut = copy.deepcopy(list(history)[:index + 1])
        source_id = source.get("id")
        category = source.get("category") or "general"
        metadata = copy.deepcopy(source.get("metadata") or {})
        tags = list(source.get("tags") or [])
        title = self._fork_title(source.get("title"))

        new_id = await chat.new(category=category, title=title, metadata=metadata)

        chat.messages.data.load(cut)
        chat.messages.data.save()

        chat.data[chat.current]["tags"] = self._fork_tags(tags)
        chat.data.save()

        self.log("chat-pro", f"forked {source_id} at message {index} -> {new_id} ({len(cut)} messages)")

        if ws_mgr is not None:
            ws_mgr.active_chat_id = new_id
            await ws_mgr.broadcast({"type": "chat_switched", "id": new_id})

        return {"id": new_id, "title": title, "messages": len(cut), "source": source_id}

    # ----------------
    # branches
    # ----------------
    @webui.route("state")
    async def _route_state(self, body=None, query=None):
        """branch state of the open chat: variants per message + the whole tree"""
        channel = self.channel
        chat = channel.context.chat
        if chat.current is None:
            return {"has_tree": False, "variants": {}, "nodes": [], "active": []}

        chat_id = chat.get("id")
        tree = self._tree(chat_id)
        if not tree.load():
            return {"has_tree": False, "chat_id": chat_id, "variants": {}, "nodes": [], "active": []}

        changed = tree.reconcile(await chat.messages.get())
        if changed:
            # ids were just stamped into the live history: persist them, or the
            # next reconcile would have to guess
            chat.messages.data.save()
            tree.save()

        state = tree.state()
        # live message index -> node id. the client cannot read chatpro_id off
        # its own turn objects: turn_hash() ignores _metadata, so mergeTurnHistory
        # keeps the stale turn and the id would never show up. indices always
        # match the list the client just loaded.
        state["index_ids"] = {str(index): node_id for index, node_id in enumerate(tree.active)}
        state.update({"has_tree": True, "chat_id": chat_id})
        return state

    @webui.route("branch", method="POST")
    async def _route_branch(self, body=None, query=None):
        """start an alternative continuation at a message

        body:
            {"mode": "regenerate", "index": <first message of an assistant turn>}
            {"mode": "edit", "index": <a user message>, "content": ..., "filenames": [...], "files": [...]}
        """
        channel = self.channel
        ws_mgr = getattr(channel, "websocket_manager", None)
        if ws_mgr is None:
            return self.result("branching needs the webui channel", success=False)

        problem = self._check_idle(ws_mgr)
        if problem:
            return self.result(problem, success=False)

        payload = body if isinstance(body, dict) else {}
        mode = str(payload.get("mode") or "")
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            return self.result("missing or invalid message index", success=False)

        chat = channel.context.chat
        if chat.current is None:
            return self.result("no chat is currently loaded", success=False)

        chat_id = chat.get("id")
        messages = await chat.messages.get()
        if index < 0 or index >= len(messages):
            return self.result(f"message index {index} is out of range", success=False)

        # validate before touching anything: a rejected request must not leave a
        # freshly captured tree behind
        role = messages[index].get("role")
        if mode == "regenerate" and role != "assistant":
            return self.result("regenerate variants only apply to an assistant turn", success=False)
        if mode == "edit" and role != "user":
            return self.result("edited variants only apply to a user message", success=False)
        if mode not in ("regenerate", "edit"):
            return self.result("unknown branch mode", success=False)

        user_index = None
        user_message = None
        if mode == "regenerate":
            # the assistant turn starts at `index`; its prompt is the user message
            # before it. that user message is re-sent *with its id*, so it stays a
            # shared node and only the answer becomes a sibling.
            user_index = await chat.messages.get_last_message_with_role("user", cutoff_index=index)
            if user_index < 0:
                return self.result("no user message to regenerate from", success=False)

            user_message = dict(await chat.messages.get(user_index))
            if not user_message.get("content") and not (user_message.get("_metadata") or {}).get("filenames"):
                return self.result("that message has no content to re-send", success=False)

        # first branch activates the chat: the live history becomes the tree
        tree = self._tree(chat_id)
        if tree.load():
            tree.reconcile(messages)
        else:
            tree.capture(messages)
            self.log("chat-pro", f"chat {chat_id} became a tree ({len(tree.nodes)} messages)")
        tree.save()
        chat.messages.data.save()

        if mode == "regenerate":
            await chat.messages.delete_from(user_index)
            await ws_mgr.broadcast({"type": "sync"})
            await ws_mgr.start_stream(channel, chat_id, user_message)

            return {"mode": "regenerate", "index": index, "messages": len(tree.nodes)}

        if mode == "edit":
            message = await self._build_variant_message(
                channel,
                payload.get("content"),
                payload.get("files"),
                payload.get("filenames"),
            )

            # the edited prompt replaces this one as a *sibling*: drop the message
            # and everything under it, then send the new prompt as a normal turn
            await chat.messages.delete_from(index)
            await ws_mgr.broadcast({"type": "sync"})
            await ws_mgr.start_stream(channel, chat_id, message)

            return {"mode": "edit", "index": index, "messages": len(tree.nodes)}

        return self.result("unknown branch mode", success=False)

    @webui.route("switch", method="POST")
    async def _route_switch(self, body=None, query=None):
        """make one branch (and the continuation it prefers) the open chat"""
        channel = self.channel
        ws_mgr = getattr(channel, "websocket_manager", None)
        problem = self._check_idle(ws_mgr)
        if problem:
            return self.result(problem, success=False)

        payload = body if isinstance(body, dict) else {}
        node_id = str(payload.get("node") or "")
        if not node_id:
            return self.result("missing branch node", success=False)

        chat = channel.context.chat
        if chat.current is None:
            return self.result("no chat is currently loaded", success=False)

        tree = self._tree(chat.get("id"))
        if not tree.load():
            return self.result("this chat has no branches", success=False)

        tree.reconcile(await chat.messages.get())

        try:
            tree.switch(node_id)
        except ValueError as e:
            return self.result(str(e), success=False)

        messages = tree.materialize()
        chat.messages.data.load(messages)
        chat.messages.data.save()
        tree.save()

        # a switch is a different timeline: the cached API measurement (and its
        # message-count mark) described the old one, exactly like compress()
        if chat.current is not None:
            chat.data[chat.current]["token_usage"] = 0
            chat.data[chat.current]["token_usage_mark"] = 0
            chat.data.save()
        channel.context.using_api_token_data = False

        self.log("chat-pro", f"switched chat {chat.get('id')} to branch {node_id} ({len(messages)} messages)")

        if ws_mgr is not None:
            await ws_mgr.broadcast({"type": "sync"})
            await ws_mgr.push_context_update()

        return {"node": node_id, "messages": len(messages)}

    @webui.route("prune", method="POST")
    async def _route_prune(self, body=None, query=None):
        """permanently remove an off-path branch and everything under it"""
        channel = self.channel
        ws_mgr = getattr(channel, "websocket_manager", None)
        problem = self._check_idle(ws_mgr)
        if problem:
            return self.result(problem, success=False)

        payload = body if isinstance(body, dict) else {}
        node_id = str(payload.get("node") or "")
        if not node_id:
            return self.result("missing branch node", success=False)

        chat = channel.context.chat
        if chat.current is None:
            return self.result("no chat is currently loaded", success=False)

        tree = self._tree(chat.get("id"))
        if not tree.load():
            return self.result("this chat has no branches", success=False)

        if tree.reconcile(await chat.messages.get()):
            chat.messages.data.save()

        try:
            removed = tree.prune(node_id)
        except ValueError as e:
            return self.result(str(e), success=False)

        tree.save()
        self.log("chat-pro", f"pruned branch {node_id} of chat {chat.get('id')} ({removed} messages)")

        return {"removed": removed}
