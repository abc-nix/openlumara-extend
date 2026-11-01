# -- AI GENERATED CODE (deepseek-v4.1-flash-1017-maya-s) :: 2026-11-01 harness:OpenLumara

"""chat_nav — navigator for the open chat: block outline + full-text search.

The webui already has the whole open chat in memory (GET /api/chat/load returns
turn_history) and only renders the turns near the viewport (ui.js lazy
mounting), which is why Ctrl+F cannot see the rest of a long session.

This module keeps a searchable copy of that in-memory history, client-side, and
jumps with the core's ui.scrollToTurn(), which mounts the target turn before
scrolling to it. No backend, no index server, no core edits.

A block is one user message or one whole assistant chain (a "turn"). Inside an
assistant block the text is split into records by kind (answer / process / tool
call / tool result); the sidebar chips decide which kinds get searched.
"""

import core


class ChatNav(core.module.Module):

    category = "utilities"

    settings = {
        "enable_webui_extension": {
            "description": "Show the chat navigator tab in the sidebar and load its webui assets.",
            "default": True
        },
        "search_user_by_default": {
            "description": "Search your own messages by default.",
            "default": True
        },
        "search_answers_by_default": {
            "description": "Search the assistant's final answers by default.",
            "default": True
        },
        "search_process_by_default": {
            "description": "Search reasoning and intermediate narration by default (noisy, off by default).",
            "default": False
        },
        "search_tools_by_default": {
            "description": "Search tool calls and tool results by default (noisy, off by default).",
            "default": False
        },
        "search_commands_by_default": {
            "description": "Search slash commands and system notices by default.",
            "default": False
        },
        "max_results": {
            "description": "Maximum number of search hits listed at once.",
            "default": 200
        },
        "tool_output_index_limit": {
            "description": "Characters of each tool call/result kept in the search index (0 = unlimited, costs memory).",
            "default": 4000
        },
        "highlight_in_chat": {
            "description": "Highlight the matched text inside the chat after jumping to a hit.",
            "default": True
        },
        "highlight_seconds": {
            "description": "How long the highlight stays after a jump, in seconds. 0 keeps it until you search again, close the bar or jump to another hit, and re-applies it as messages render while you scroll.",
            "default": 0,
            "type": "number",
            "min": 0,
            "max": 120,
            "step": 1
        },
        "search_per_occurrence": {
            "description": "Navigate every occurrence of a word instead of one hit per message, landing on that occurrence and painting it apart from the other matches. Applies to your messages and the assistant's final answers; reasoning and tool hits stay one per block, because their rendered text cannot be addressed per occurrence.",
            "default": True
        },
        "take_over_ctrl_f": {
            "description": "Ctrl+F opens the find bar while the chat is in focus, instead of the browser's own find. Inputs, the sidebar and modals keep the native one.",
            "default": True
        }
    }
