# delegate — tool descriptions (cropped 2026-11-06)

The four tool descriptions were written as full prose with `Args:`/`Returns:`
sections. Measured against the rest of the codebase they were badly out of
line, so they were cut to one-liners in the house style. This file keeps the
original texts and what was dropped, so the decision can be reviewed or undone.

## Why: model-visible size before the crop

Size = description + parameter descriptions, i.e. exactly what the tool loader
(`core/tool_loader.py`, `parse_tool_docstring`) puts in the request. `Returns:`
sections never reach the model — the loader strips them.

| module | tools | model-visible chars | median per tool |
|---|---|---|---|
| host_shell | 1 | 163 | 163 (no parameter descriptions at all) |
| coder | 12 | 520 | 49 |
| editor | 16 | 1,867 | 116 |
| web_search | 5 | 2,615 | 494 |
| **delegate (before)** | 4 | **3,055** | **876** |
| **delegate (after)** | 4 | **530** | **133** |

Four delegate tools outweighed all sixteen editor tools, and each delegate tool
was ~7.5x the median editor tool. `delegate` is preloaded, so that text sat in
**every** request — about 760 tokens permanently; it is now about 130.

## What each tool says now

- `delegate_new` — "Delegate a new task to an agent in a new session. Blocks
  until done and returns the answer; `background=True` runs it in the background.
  `model` defaults to the current model."
- `delegate_send` — "Add a message to an existing delegated session so the agent
  continues its work. `background=True` runs it in the background; `model`
  switches the session's model."
- `delegate_list` — "List the most recent delegated sessions, including any
  running in the background."
- `delegate_read` — "Fetch the answer from a specific session (requires
  `task_id`)."

## What was dropped, and where the fact still surfaces

- **Background's two conditions** (a free background slot, and the chat's current
  model) — dropped from `delegate_new`/`delegate_send`. They surface as the
  refusal message built in `policy.start_refusal()` (`all N background delegate
  slot(s) are in use ...`, `a background delegate must use the chat's current
  model ...`), and in the `max_background_tasks` setting description.
- **Parameter hints** — `title` (panel label), `status` (running/done/error/
  interrupted), `limit` (newest first, max 100), `chars` (last N, 0 = all),
  `query` (title/id/model). Defaults are in the signature; `delegate_list`
  output shows the status values, and `delegate_read` reports `truncated`, so
  the model can react without the prose.
- **`Returns:` blocks** — never model-visible; they duplicated the dicts the
  tools actually return.
- **The "sees this chat's system prompt but not its history" note** — dropped as
  noise (the user's wording). If a delegate's prompt ever reads as if it knew
  this conversation, this is the missing sentence.
- **"stored reasoning is kept for display only" / "not a re-summary"** — dropped
  from `delegate_send`; `keep_reasoning_in_context` documents the re-send
  behaviour in the settings.

## Original texts (before the crop, verbatim)

### delegate_new (1,248 chars)

```
Delegate a self-contained task to a separate AI session with its own context.

The delegate sees this chat's system prompt but none of its history, so the
prompt must stand on its own. It has the same tools you do and its whole
transcript (reasoning, tool calls, tool results) is saved under the returned
task_id: the user can watch it live in the delegate panel, and you can
continue the same conversation later with delegate_send(task_id, ...).

This call blocks until the delegate finishes and returns its final answer, unless `background=True`, which returns at once with status running.

Args:
    prompt: the task, written self-contained (the delegate cannot see this chat).
    title: short display label for the panel and task list. Derived from the prompt when empty.
    model: model to run the delegate on. Defaults to the current model; must be one of the available models.
    background: return immediately with status running instead of waiting for the run (default False). The run continues in the background; check it with delegate_list or read it in the panel. Refused when no background slot is free or when `model` is not the chat's current model - the error says which, and running it in the foreground (omit background) is always possible.

Returns:
    {task_id, status, model, rounds, result} - or {task_id, status: "running", background: true} when background=True
```

### delegate_send (876 chars)

```
Continue a delegated task: a follow-up message in the same conversation.

The delegate resumes with its full stored context (every message and tool
result is replayed; the stored reasoning is kept for display only), so this
is a normal continuation, not a re-summary. Use it to ask why the delegate
did something, or to hand it another step.

Args:
    task_id: id of the task, as returned by delegate_new (for example "01AB3F7G").
    prompt: the follow-up request.
    model: switch the task to another model. Defaults to the model the task already uses.
    background: return immediately with status running instead of waiting for the run (default False); the run continues in the background. Refused when no background slot is free or when the task's model is not the chat's current one - the error says which; run it in the foreground (omit background) instead, or switch the chat to that model first.

Returns:
    {task_id, status, model, rounds, result} - or {task_id, status: "running", background: true} when background=True
```

### delegate_list (471 chars)

```
List the stored delegated tasks, newest first.

This reads the task index on disk, so it also works when the ids from
earlier in this conversation were compressed away: use it to recover a
task id instead of asking the user for one.

Args:
    query: text matched against the title, id and model (empty = every task).
    status: only tasks with this status - "running", "done", "error" or "interrupted" (empty = any).
    limit: how many tasks to return, newest first (default 20, max 100).

Returns:
    {count, tasks: [{task_id, title, status, model, rounds, updated, error}], truncated}
```

### delegate_read (460 chars)

```
Read a delegated task's final answer (its last assistant message).

Use this to collect a run that was started with background=True or was
detached from its tool call - no panel and no file access needed. It also
reports the task's current status, so it can be polled while it runs.

Args:
    task_id: id of the task, as returned by delegate_new / delegate_list.
    chars: return only the last N characters of the answer (default 4000, max 40000); 0 returns it whole.

Returns:
    {task_id, status, model, rounds, result, truncated}
```
