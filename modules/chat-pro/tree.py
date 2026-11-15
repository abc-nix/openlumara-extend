# -- AI GENERATED CODE (deepseek-v4.1-flash-1017-maya-s) :: 2026-11-14 harness:OpenLumara

"""chat-pro - the conversation tree of one chat.

nothing here exists until a chat is branched for the first time: a plain chat is
just a history file. capture() turns it into a tree, and from then on the tree is
the source of truth while the history file stays the *materialised active path*
(which is all core ever sees).

data/chat-pro/trees/<chat_id>.json
{
  "version": 1,
  "chat_id": "...",
  "seq": 41,                     # creation counter -> stable sibling order
  "active": ["id1", "id2"],      # ids of the currently materialised path
  "nodes": {
    "<id>": {
      "id": "<id>",
      "parent": "<id|null>",       # tree edge
      "active_child": "<id|null>",  # which continuation this node prefers
      "seq": 7,
      "message": {...}             # the raw message, verbatim
    }
  }
}

siblings = the children of one node, ordered by seq. a branch point is a node
with more than one child; the `< n/m >` control on a message row counts the
siblings of that message, and switching picks one and follows its own
active_child chain down to its tip.

ids live in the message's own _metadata (chatpro_id), which core never sends to
the API (context.get() keeps only role/content/reasoning_content/tool_calls/
tool_call_id/function_call/tool) and never drops on edit, so reconciliation is a
diff by id instead of a diff by index.
"""

import copy
import json
import os
import uuid

import core


NODE_KEY = "chatpro_id"
TREE_VERSION = 1


def new_id():
    return uuid.uuid4().hex[:10]


def message_id(message):
    metadata = message.get("_metadata")
    if isinstance(metadata, dict):
        return metadata.get(NODE_KEY)
    return None


def stamp_id(message, node_id):
    metadata = message.get("_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        message["_metadata"] = metadata
    metadata[NODE_KEY] = node_id


def preview(message, limit=80):
    """one line of the message for the branch panel"""
    role = message.get("role") or "?"
    content = message.get("content")

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(str(block.get("text")))
        content = " ".join(parts)

    text = " ".join(str(content or "").split())

    if role == "tool":
        text = "[tool result] " + text
    elif not text:
        if message.get("tool_calls"):
            text = "[tool call]"
        elif message.get("reasoning_content"):
            text = "[reasoning] " + " ".join(str(message.get("reasoning_content")).split())
        else:
            text = "[empty]"

    if len(text) > limit:
        text = text[:limit - 1] + "\u2026"

    return text


class ChatTree:
    """the branch tree of one chat; load() tells you whether it exists"""

    def __init__(self, channel, chat_id):
        self.channel = channel
        self.chat_id = str(chat_id)
        self.path = core.get_data_path(os.path.join("chat-pro", "trees", f"{self.chat_id}.json"))
        self.nodes = {}
        self.active = []
        self._seq = 0
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass

    # ----------------
    # persistence
    # ----------------
    def load(self):
        """returns True when this chat has a tree file"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self.nodes = {}
            self.active = []
            self._seq = 0
            return False
        except Exception as e:
            core.log("chat-pro", f"could not read the tree of chat {self.chat_id}: {e}")
            self.nodes = {}
            self.active = []
            self._seq = 0
            return False

        self.nodes = data.get("nodes") or {}
        self.active = [node_id for node_id in (data.get("active") or []) if node_id in self.nodes]
        self._seq = int(data.get("seq") or 0)
        return bool(self.nodes)

    def save(self):
        """atomic write: a crash mid-write can never truncate the tree"""
        payload = {
            "version": TREE_VERSION,
            "chat_id": self.chat_id,
            "seq": self._seq,
            "active": list(self.active),
            "nodes": self.nodes,
        }
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception as e:
            core.log("chat-pro", f"could not write the tree of chat {self.chat_id}: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        return True

    # ----------------
    # nodes
    # ----------------
    def _new_node(self, message, parent):
        node_id = new_id()
        self._seq += 1
        self.nodes[node_id] = {
            "id": node_id,
            "parent": parent,
            "active_child": None,
            "seq": self._seq,
            "message": message,
        }
        stamp_id(message, node_id)
        return node_id

    def _children(self, parent_id):
        return sorted(
            (nid for nid, node in self.nodes.items() if node.get("parent") == parent_id),
            key=lambda nid: self.nodes[nid].get("seq", 0),
        )

    def _roots(self):
        """first messages of the chat: branching the very first message makes a
        second root, and roots are siblings of each other"""
        return sorted(
            (nid for nid, node in self.nodes.items() if not node.get("parent")),
            key=lambda nid: self.nodes[nid].get("seq", 0),
        )

    def siblings(self, node_id):
        node = self.nodes.get(node_id)
        if node is None:
            return []
        parent = node.get("parent")
        if not parent or parent not in self.nodes:
            return self._roots()
        return [nid for nid in self._children(parent) if nid in self.nodes]

    def is_on_active(self, node_id):
        return node_id in self.active

    # ----------------
    # capture / sync
    # ----------------
    def capture(self, messages):
        """turn the live history of a plain chat into a tree (first branch)"""
        self.nodes = {}
        self.active = []
        self._seq = 0

        parent = None
        for message in messages:
            node_id = self._new_node(message, parent)
            self.active.append(node_id)
            if parent is not None:
                self.nodes[parent]["active_child"] = node_id
            parent = node_id

        if parent is not None:
            self.nodes[parent]["active_child"] = None

        # the caller persists: ids were just stamped into the live history, and a
        # tree written before that history would hold nodes no message carries
        return len(self.active)

    def reconcile(self, messages):
        """fold the live history into the tree; returns True when ids were added
        (the caller then has to save the live history too, or the ids are lost)"""
        changed = False
        active = []
        parent = None

        for message in messages:
            node_id = message_id(message)

            # a node can only appear once on a path: a repeated id means someone
            # re-sent a stored message (regenerate) - ignore the duplicate
            if node_id and node_id in active:
                continue

            node = self.nodes.get(node_id) if node_id else None

            if node is None:
                # new message (or one whose id we no longer know): a fresh node
                node_id = self._new_node(message, parent)
                changed = True
            else:
                # known message: keep the live copy (edits land here) and its parent
                if node.get("parent") != parent:
                    node["parent"] = parent
                    changed = True
                node["message"] = message

            if node_id in active:
                continue
            active.append(node_id)

            if parent is not None and parent in self.nodes:
                if self.nodes[parent].get("active_child") != node_id:
                    self.nodes[parent]["active_child"] = node_id

            parent = node_id

        if parent is not None and self.nodes[parent].get("active_child") is not None:
            self.nodes[parent]["active_child"] = None

        if active != self.active:
            self.active = active
            changed = True

        return changed

    # ----------------
    # paths
    # ----------------
    def _chain_to_root(self, node_id):
        chain = []
        current = node_id
        seen = set()
        while current and current in self.nodes and current not in seen:
            seen.add(current)
            chain.append(current)
            current = self.nodes[current].get("parent")
        chain.reverse()
        return chain

    def switch(self, node_id):
        """make this node (and the continuation it prefers) the active path"""
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError(f"unknown branch node {node_id}")

        parent = node.get("parent")
        if parent and parent in self.nodes:
            self.nodes[parent]["active_child"] = node_id

        chain = self._chain_to_root(node_id)

        # follow the branch's own preference down to its tip
        seen = set(chain)
        while True:
            nxt = self.nodes[chain[-1]].get("active_child")
            if not nxt or nxt not in self.nodes or nxt in seen:
                break
            seen.add(nxt)
            chain.append(nxt)

        for a, b in zip(chain, chain[1:]):
            self.nodes[a]["active_child"] = b

        self.active = chain
        return chain

    def materialize(self):
        """the active path as a plain history list, ids included"""
        messages = []
        for node_id in self.active:
            node = self.nodes.get(node_id)
            if node is None:
                continue
            message = copy.deepcopy(node["message"])
            stamp_id(message, node_id)
            messages.append(message)
        return messages

    # ----------------
    # pruning / state
    # ----------------
    def prune(self, node_id):
        """remove an off-path node and its subtree; returns how many went away"""
        if node_id not in self.nodes:
            raise ValueError(f"unknown branch node {node_id}")
        if node_id in self.active:
            raise ValueError("can't prune a message on the active path")

        parent = self.nodes[node_id].get("parent")
        doomed = {node_id}

        added = True
        while added:
            added = False
            for nid, node in self.nodes.items():
                if nid not in doomed and node.get("parent") in doomed:
                    doomed.add(nid)
                    added = True

        for nid in doomed:
            self.nodes.pop(nid, None)

        if parent and parent in self.nodes and self.nodes[parent].get("active_child") in doomed:
            self.nodes[parent]["active_child"] = None

        # the caller persists, so one operation is one write: the model never
        # writes on its own, and nothing is written until prune() has not raised
        return len(doomed)

    def _children_map(self):
        """parent id -> ordered child ids (the tree's edges)"""
        children = {}
        for nid, node in self.nodes.items():
            parent = node.get("parent")
            if parent and parent in self.nodes:
                children.setdefault(parent, []).append(nid)
        for kids in children.values():
            kids.sort(key=lambda nid: self.nodes[nid].get("seq", 0))
        return children

    def _variants(self, children):
        """node id -> its position among siblings (only where there is a choice)"""
        variants = {}
        for parent, kids in children.items():
            if len(kids) < 2:
                continue
            for position, kid in enumerate(kids, start=1):
                variants[kid] = {
                    "position": position,
                    "total": len(kids),
                    "siblings": list(kids),
                }

        # branching the first message of a chat leaves several roots: they are
        # siblings too, so the counter and the panel must see them as one group
        roots = self._roots()
        if len(roots) > 1:
            for position, kid in enumerate(roots, start=1):
                variants[kid] = {
                    "position": position,
                    "total": len(roots),
                    "siblings": list(roots),
                }
        return variants

    def _preferred_length(self, node_id):
        """how many messages this node's branch holds (follows active_child)"""
        total = 1
        current = node_id
        seen = {node_id}
        while True:
            nxt = (self.nodes.get(current) or {}).get("active_child")
            if not nxt or nxt not in self.nodes or nxt in seen:
                break
            seen.add(nxt)
            total += 1
            current = nxt
        return total

    @staticmethod
    def _subtree_size(node_id, children):
        total = 1
        stack = [node_id]
        while stack:
            kids = children.get(stack.pop(), [])
            total += len(kids)
            stack.extend(kids)
        return total

    def turns(self):
        """group nodes into turns: one user message, or one assistant chain

        a turn is what the graph draws (and what chat_nav calls a block): the
        user message, plus every non-user step that follows it *linearly* -
        assistant text, tool calls, tool results - collapsed into one card. a
        user message always starts a turn, and a fork ends one: each
        alternative starts its own, because they are different continuations.

        returns (turns, turn_of_node): turns is a list of {id, nodes} in seq
        order (id = the first node, which is also the switch target), and
        turn_of_node maps every node id to the turn that contains it.
        """
        children = self._children_map()
        nodes = self.nodes

        def role(node_id):
            message = (nodes.get(node_id) or {}).get("message") or {}
            return message.get("role")

        def kids(node_id):
            return children.get(node_id, [])

        def is_cutoff(node_id):
            # context compression appends a signal message before the summary;
            # without this the summary would be folded into the answer before it
            message = (nodes.get(node_id) or {}).get("message") or {}
            return (message.get("_metadata") or {}).get("signal") == "SUMMARIZATION_CUTOFF"

        starts = set()
        for node_id, node in nodes.items():
            parent = node.get("parent")
            if is_cutoff(node_id):
                starts.add(node_id)          # the summary starts a turn of its own
            elif not parent or parent not in nodes:
                starts.add(node_id)          # a root
            elif role(node_id) == "user" or role(parent) == "user":
                starts.add(node_id)          # a prompt, or the answer to one
            elif len(kids(parent)) > 1:
                starts.add(node_id)          # an alternative inside a chain

        turns = []
        turn_of_node = {}
        for node_id in sorted(nodes, key=lambda n: nodes[n].get("seq", 0)):
            if node_id not in starts:
                continue

            group = [node_id]
            current = node_id
            while True:
                next_ids = kids(current)
                if len(next_ids) != 1:
                    break                    # a fork (or the tip)
                nxt = next_ids[0]
                if role(nxt) == "user" or nxt in starts:
                    break                    # the next turn begins here
                group.append(nxt)
                current = nxt

            for member in group:
                turn_of_node[member] = node_id
            turns.append({"id": node_id, "nodes": group})

        return turns, turn_of_node

    def branch_map(self, children=None):
        """rows for the branches panel.

        only fork points and their alternatives are emitted: linear runs of
        messages collapse into nothing, so a 113-message chat with two splits is
        a handful of rows, and `depth` counts *branch levels*, not messages.
        """
        children = children if children is not None else self._children_map()
        active = set(self.active)
        rows = []

        def add(kind, nid, depth, fork=None):
            message = (self.nodes.get(nid) or {}).get("message") or {}
            rows.append({
                "kind": kind,
                "id": nid,
                "fork": fork,
                "depth": depth,
                "role": message.get("role"),
                "preview": preview(message),
                "on_path": nid in active,
                "messages": self._preferred_length(nid),
                "subtree": self._subtree_size(nid, children),
                "variants": len(children.get(nid, [])),
            })

        def walk(nid, depth):
            kids = children.get(nid, [])
            if len(kids) >= 2:
                add("fork", nid, depth)
                for kid in kids:
                    add("branch", kid, depth + 1, fork=nid)
                    walk(kid, depth + 1)
                return
            # a linear chain adds no indent: keep looking for the next split
            for kid in kids:
                walk(kid, depth)

        roots = self._roots()
        if len(roots) > 1:
            # branching the very first message: the roots are the alternatives
            for root in roots:
                add("branch", root, 0)
                walk(root, 0)
        else:
            for root in roots:
                walk(root, 0)

        return rows

    def state(self):
        """everything the ui needs: counters per message + the branch map"""
        children = self._children_map()
        branches = self.branch_map(children)
        return {
            "active": list(self.active),
            "variants": self._variants(children),
            "branches": branches,
            "branch_points": sum(1 for row in branches if row["kind"] == "fork"),
        }

    def node_message(self, node_id):
        node = self.nodes.get(node_id)
        return node.get("message") if node else None