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

from .tree import ChatTree, preview


class ChatPro(core.module.Module):

    category = "utilities"

    settings = {
        "enable_webui_extension": {
            "description": "Load this module's WebUI Extensions (fork/branch controls, the Chat Pro right panel).",
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
        "host_delegate": {
            "description": "Render delegate's task panel inside this dock's Tasks tab, and send delegate's header button to that tab. Turn it off to let delegate keep its own panel (requires delegate to be enabled).",
            "default": True
        },
        "file_open_action": {
            "description": "What a file reference does when clicked: copy the path, open it with a module, or run a shell command. URLs always open in a tab.",
            "default": "copy",
            "type": "select",
            "options": {
                "copy": "Copy the path to the clipboard.",
                "module": "Ask a module to open it (see file_open_module).",
                "command": "Run a shell command (see file_open_command)."
            }
        },
        "file_open_module": {
            "description": "Module used by 'open with module'. It needs POST /api/ext/<module>/open and a store named <module>View with open(workspace, path) - libreta provides both.",
            "default": "libreta",
            "type": "text"
        },
        "file_open_command": {
            "description": "Command used by 'open with command'. {path} is replaced with the shell-quoted path.",
            "default": "xdg-open {path}",
            "type": "text"
        },
        "tab_order": {
            "description": "Order of the dock tabs, comma separated: branches, search, links, tasks. Unknown names are ignored and any missing tab is appended at the end, so this only reorders - it never hides a tab. Applied on save (the page reloads).",
            "default": "branches,search,links,tasks",
            "type": "text"
        },
        "always_active": {
            "description": "Keep every chat on the branch tree from its first message, so core's own Regenerate/Delete preserve answers even before a branch is made. Costs a tree file, ids in every message and one reconcile per change; off by default.",
            "default": False
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

    def _always_active(self):
        return bool(self.config.get("always_active", False))

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
        if channel is None:
            return self.result("no active channel", success=False)

        chat = channel.context.chat
        if chat.current is None:
            return {
                "has_tree": False,
                "chat_id": None,
                "branches": [],
                "branch_points": 0,
                "variants": {},
                "index_ids": {},
                "active": [],
            }

        chat_id = chat.get("id")
        messages = await chat.messages.get()
        tree = self._tree(chat_id)

        if tree.load():
            if tree.reconcile(messages):
                # ids were just stamped into the live history: persist them, or
                # the next reconcile would have to guess
                chat.messages.data.save()
                tree.save()
        elif self._always_active() and messages:
            # always-active: this chat becomes a tree at its first message, so
            # core's own regenerate/delete keep the dropped answers as variants
            # even though the user never made a branch
            tree.capture(messages)
            chat.messages.data.save()
            tree.save()
            self.log("chat-pro", f"chat {chat_id} is always-active ({len(tree.nodes)} messages)")
        else:
            return {
                "has_tree": False,
                "chat_id": chat_id,
                "variants": {},
                "nodes": [],
                "active": [],
                "index_ids": {},
            }

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
        # persist the history first: the ids were just stamped into it, and a tree
        # written before its history would keep nodes no message carries (an
        # interrupted write would then show phantom roots on the next reconcile)
        chat.messages.data.save()
        tree.save()

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

    @staticmethod
    def _message_text(message):
        """the plain text of a message, blocks joined: what a card or the hover
        miniature can say about it"""
        content = message.get("content")
        if isinstance(content, list):
            return "\n\n".join(str(block.get("text")) for block in content
                               if isinstance(block, dict) and block.get("type") == "text" and block.get("text"))
        return str(content or "")

    @classmethod
    def _answer_message(cls, nodes, group):
        """the message a turn stands for: the assistant's final answer when the
        chain has one, otherwise the last message carrying text (a tool-only
        step, a tool result). never the step list."""
        def has_text(message):
            return bool(cls._message_text(message).strip())

        for node_id in reversed(group):
            message = (nodes.get(node_id) or {}).get("message") or {}
            if message.get("role") == "assistant" and not message.get("tool_calls") and has_text(message):
                return message
        for node_id in reversed(group):
            message = (nodes.get(node_id) or {}).get("message") or {}
            if has_text(message):
                return message
        return (nodes.get(group[0]) or {}).get("message") or {}

    @webui.route("node")
    async def _route_node(self, body=None, query=None):
        """one turn's text, for the hover miniature

        the graph payload deliberately carries only a short preview, and the
        full text of an off-path message exists nowhere but the tree file, so it
        is fetched per hover and cached client-side.
        """
        channel = self.channel
        if channel is None:
            return self.result("no active channel", success=False)

        chat = channel.context.chat
        if chat.current is None:
            return self.result("no chat is currently loaded", success=False)

        params = query if isinstance(query, dict) else {}
        node_id = str(params.get("id") or "")
        if not node_id:
            return self.result("missing node id", success=False)

        tree = self._tree(chat.get("id"))
        if not tree.load():
            return self.result("this chat has no branches", success=False)

        tree.reconcile(await chat.messages.get())
        turns, turn_of_node = tree.turns()

        # a node in the middle of a chain still resolves to its turn
        turn = next((item for item in turns if item["id"] == node_id), None)
        if turn is None:
            owner = turn_of_node.get(node_id)
            turn = next((item for item in turns if item["id"] == owner), None)
        if turn is None:
            return self.result("unknown branch node", success=False)

        message = self._answer_message(tree.nodes, turn["nodes"])
        metadata = message.get("_metadata") or {}

        return {
            "id": turn["id"],
            "role": message.get("role") or "?",
            "content": self._message_text(message)[:8000],
            "time": metadata.get("timestamp"),
            "steps": max(0, len(turn["nodes"]) - 1),
        }

    @webui.route("graph")
    async def _route_graph(self, body=None, query=None):
        """the whole tree as *light* nodes, for the canvas

        `state` collapses to fork points on purpose (a 113-message chat must stay
        a handful of rows); a canvas needs every node, so this is a separate and
        much heavier payload - fetched only while the graph modal is open, never
        by the poll. previews are capped, and no content / reasoning / tool output
        travels: single messages can be hundreds of KB.
        """
        channel = self.channel
        if channel is None:
            return self.result("no active channel", success=False)

        chat = channel.context.chat
        if chat.current is None:
            return self.result("no chat is currently loaded", success=False)

        chat_id = chat.get("id")
        messages = await chat.messages.get()
        tree = self._tree(chat_id)

        if tree.load():
            if tree.reconcile(messages):
                chat.messages.data.save()
                tree.save()
        elif self._always_active() and messages:
            tree.capture(messages)
            chat.messages.data.save()
            tree.save()
        else:
            return {"nodes": [], "roots": [], "active": [], "stats": {"nodes": 0, "branches": 0}}

        # one card per *turn*, not per step: a user message, or a whole assistant
        # chain (answer + tool calls + tool results). see ChatTree.turns().
        turns, turn_of_node = tree.turns()
        on_path = set(tree.active)

        def message_of(node_id):
            return (tree.nodes.get(node_id) or {}).get("message") or {}

        cards = []
        for turn in turns:
            group = turn["nodes"]
            start = turn["id"]
            start_message = message_of(start)
            answer = self._answer_message(tree.nodes, group)

            # a turn carrying the compression signal is the handoff summary
            compressed = any(
                ((message_of(node_id).get("_metadata") or {}).get("signal") == "SUMMARIZATION_CUTOFF")
                for node_id in group
            )

            parent_node = (tree.nodes.get(start) or {}).get("parent")
            cards.append({
                "id": start,
                "parent": turn_of_node.get(parent_node) if parent_node else None,
                "seq": (tree.nodes.get(start) or {}).get("seq", 0),
                "role": start_message.get("role") or "?",
                "preview": preview(answer, 220),
                "time": (message_of(group[-1]).get("_metadata") or {}).get("timestamp"),
                "steps": max(0, len(group) - 1),
                "compressed": compressed,
                "tool": any(message_of(node_id).get("role") == "tool" or message_of(node_id).get("tool_calls")
                            for node_id in group),
                "on_path": start in on_path,
                "children": 0,
            })

        counts = {}
        for card in cards:
            if card["parent"]:
                counts[card["parent"]] = counts.get(card["parent"], 0) + 1
        for card in cards:
            card["children"] = counts.get(card["id"], 0)

        active_turns = []
        for node_id in tree.active:
            turn_id = turn_of_node.get(node_id)
            if turn_id and turn_id not in active_turns:
                active_turns.append(turn_id)

        return {
            "nodes": cards,
            "roots": [card["id"] for card in cards if not card["parent"]],
            "active": active_turns,
            "stats": {"nodes": len(cards), "branches": sum(1 for card in cards if card["children"] > 1)},
        }
