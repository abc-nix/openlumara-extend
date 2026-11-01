# openlumara-extend

Personal OpenLumara content: user **modules** now, channels/skills/themes as
they are moved over. Private repo — it mirrors the folder layout OpenLumara's
module libraries expect, so the same tree can be installed on another machine:

```
/library add git@github.com:abc-nix/openlumara-extend.git
/library sync
/library install browser
```

## Layout

| folder in this repo | installed as | entry file |
|---|---|---|
| `modules/<name>/` | user module (`user_modules/<name>`) | `module.py` |
| `channels/<name>/` | user channel (`user_channels/<name>`) | `channel.py` |
| `skills/<name>/` | skill | `SKILL.md` |
| `themes/<name>.json` | webui theme | (json, optional `.css` sidecar) |

## Modules

### browser

A dedicated Chrome/Chromium window your AI can drive: it opens pages, clicks,
types, fills forms, runs JavaScript and takes screenshots, and can read what
the page logs and requests. It runs on its own profile — separate from your
everyday browser — so sites you log in to once stay logged in.

It drives a Chromium-based browser already installed on your system (Chrome,
Chromium or Brave — detected automatically, or a path you set), so there is
nothing extra to install or run.

**Tools**

| tool | what it does |
|---|---|
| `open` / `close` / `status` | start (or attach to) the browser, quit it, or report what is connected and which tabs are open |
| `navigate` | go to a URL in the current tab |
| `back` | return to the previous page |
| `scroll` | scroll the page up or down |
| `snapshot` | read the page as a list of elements; interactive ones get `@e1`-style handles for clicking/typing |
| `click` | click an element by handle or CSS selector |
| `type_text` | type into a field, pick an option in a dropdown, or tick a checkbox |
| `fill_form` | fill several fields in one call and report each one |
| `press` | press a key or a combination (`Control+A`, `Shift+Tab`, `f5`) |
| `screenshot` | capture the visible area or the whole page, PNG or JPEG, optionally with a labelled coordinate grid |
| `evaluate` | run a snippet of JavaScript and return its result |
| `console` | recent console messages and errors, filtered by severity or type |
| `network_requests` | recent web requests, filtered by URL or kind; common background assets are hidden by default |
| `get_images` | images on the page with their sizes; icons and duplicates are skipped |
| `cookies` | cookies of the current site or a given domain |
| `list_tabs` / `new_tab` / `switch_tab` / `close_tab` | manage tabs; each tab is marked as the one the AI drives and the one you are looking at |
| `handle_dialog` | accept or dismiss an alert/confirm/prompt |
| `cdp` | raw Chrome DevTools command, off by default |

**WebUI**

- A **browser tab in the sidebar**: connection status, browser and mode, the
  open tabs with the one the AI drives and the one you are viewing, and
  **Open**, **Close** and **Restart (headless ↔ visible)** buttons.
- Restart keeps the open tabs, scroll positions and filled-in (non-secret)
  form fields; browsing history and passwords are not restored.
- **Screenshots** appear in the chat as preview cards you can open at full size.
- A short **help** panel explains the buttons and the settings worth knowing.

### chat_nav

Search and navigate the open chat. A sidebar tab mirrors the conversation as a
block outline, and a floating find bar searches the whole session — including
the messages the browser's own find cannot reach, because the webui only
renders the turns near the viewport and leaves the rest as placeholders.

It works entirely on the chat the webui already holds in memory: no backend,
no index server.

**Tools**

None.

**WebUI**

- A **chat nav tab in the sidebar**: the block count, a search box, kind chips
  and the outline — one row per block (a message of yours, or a whole assistant
  chain) with a two-line preview, its tool count and a length bar. The row you
  are reading is followed while you scroll; clicking any row jumps there.
- A **find bar** under the chat header: field, `position / total` counter,
  previous/next arrows, close button. Enter / Shift+Enter walk the matches, Esc
  closes it, and the query is kept for the next time it opens.
- **Kind chips** decide what is searched: your messages and the assistant's
  answers by default, plus reasoning, tool calls/results and command output if
  you turn them on. Only the enabled kinds are counted, so a search that only
  hits tool output reports no matches instead of noise.
- **Per occurrence**: hits step through each occurrence of a word; the current
  one is painted in the accent colour and the rest grey, and matches in
  messages that render later light up as you scroll to them.
- Jumps reuse the webui's own lazy mounting, centre on the matched text and
  flash the block.

**Settings**

`search_per_occurrence` (one hit per occurrence), `highlight_seconds` (`0` keeps
the highlight until the search changes), `take_over_ctrl_f` (Ctrl+F opens the
find bar; the sidebar and modals keep the native find) and the per-kind search
defaults.

### docprocess

Converts documents — PDF, Office, EPUB, images — into Markdown or plain text,
with OCR when needed. Conversions run as background jobs.

**Tools**

| tool | what it does |
|---|---|
| `convert` | convert a file and write the result next to it (or where you ask); optional OCR mode |
| `convert_status` | progress and results of running or finished conversions |
| `vlm_models` | list the vision-capable models an OpenAI-compatible endpoint offers |

**WebUI**

- Upgrades the **VLM model** field in the settings screen: one combobox that
  lists the models served by the configured endpoint, with a vision/all filter
  and a refresh button.

### theme_gallery

A development tool for looking at the WebUI's real components under different
theme families and light/dark modes, side by side. It only reads theme data —
it never applies a theme to your interface.

**Tools**

None.

**WebUI**

- A **theme gallery tab in the sidebar** that opens a full-screen preview: chat
  messages, composer, tool cards, sidebars, modals, settings screens and form
  controls, rendered under each theme family.
- Browse by category, compare coverage across elements, and switch light/dark
  without touching the app's actual appearance.
