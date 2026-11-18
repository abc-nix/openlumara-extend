# abc-extras — small WebUI pieces, re-added through module extension slots

A user module for [OpenLumara](https://github.com/Rose22/openlumara). It brings back two
elements the core chat header no longer renders by itself, without touching core:

- **stream-state bubble** — shows the websocket's stream state: `idle`, `sending`,
  `received`, `thinking`, `streaming`, `processing`, `preparing tools`, `calling tools`.
  Active states pulse; unknown states fall back to their raw name.
- **model dropdown** — the quick model switcher, back in the chat header.
- **settings gear** — the settings modal button, back in the header instead of the sidebar footer.

Both are gated from **Settings → Modules → abc-extras**.

## Settings

| Setting | What it does |
|---|---|
| `enable_webui_extension` | Load the header slot, styles and script at all |
| `show_model_switcher` | Model dropdown in the header |
| `show_stream_state` | The state bubble |
| `show_settings_button` | Settings gear in the header (replaces the one core moved to the sidebar footer); hidden automatically when admin settings are disabled |
| `state_style` | `badge` (bordered pill, the old core look), `text` (no border), `dot` (dot only, state on hover) |
| `hide_state_when_idle` | Hide the bubble while nothing is happening |
| `header_slot` | `right` (with the header controls) or `left` (next to the title) |
| `cluster_order` | CSS flex `order` inside the header cluster: `-1` before the other module buttons, `0` default (alphabetical), `100`+ after chat-pro (which pins itself at `99`) |
| `element_order` | `state_first` or `model_first`, when both are shown |

### About "before / after the slot"

A module renders *inside* the slot that calls it, so it cannot place itself before or after that
slot — the available knobs are **which** slot (`left()` / `right()`, both called by the core
header) and the order of the two elements. When several modules contribute to the same slot,
their order is alphabetical by module name.

## Turning it on without getting duplicates

1. Enable the module: add `abc-extras` to `user_modules.enabled` in `config.yml`, or use the
   module list in Settings (then restart — module code loads at startup).
2. Turn **off** the built-in switcher, `channels.webui.enable_model_switcher`, otherwise the
   input bar keeps its own dropdown and you end up with two.
3. `channels.webui.enable_thinking_toggle` can stay off; this module does not touch it.

## How it plugs in

- `webui/templates/chat/header.html` → `{% macro right() %}`, called by the core header's
  `{{ extension_slot("right") }}` (the core header's own buttons are commented out upstream).
- `webui/assets/css/abc_extras.css` → auto-globbed into every page; the badge style reuses the
  core `.stream-state` pill so it follows theme vars.
- `webui/assets/js/abc_extras.js` → defines `abcExtrasState()`; the dropdown reuses core's
  `modelQuickSwitch()`, so it stays in sync with the settings modal.

No AI tools, no routes, no storage — pure UI.
