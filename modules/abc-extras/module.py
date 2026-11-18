"""abc-extras \u2014 small WebUI pieces worth re-adding without patching core.

Upstream's chat header now renders extension slots only (search / export / clear
sit behind TODO comments), so anything that used to live there has to arrive
through the slot the core still calls: {{ extension_slot("right") }}.

What this module ships:
  webui/templates/chat/header.html -> macro right():
      * the stream-state bubble (idle / thinking / calling tools / ...)
      * the model dropdown
  webui/assets/css/abc_extras.css   -> dot, tones and the lighter bubble styles
  webui/assets/js/abc_extras.js     -> short labels for the backend's stream states

Nothing in core is edited, and the module ships no AI tools: it is pure WebUI.

The dropdown reuses core's modelQuickSwitch() component (assets/js/components/
model_switcher.js, loaded on every page), so it stays in sync with the settings
modal and the input bar exactly like the built-in one did. That also means two
dropdowns appear if the built-in switcher is still on: turn off
channels.webui.enable_model_switcher when this module shows its own.
"""

import core


class AbcExtras(core.module.Module):
    """Re-adds header elements the core layout no longer renders by itself."""

    category = "utilities"

    settings = {
        "enable_webui_extension": {
            "default": True,
            "description": "Load this module's header slot, styles and script.",
        },
        "show_model_switcher": {
            "type": "boolean",
            "default": True,
            "description": "Show the model dropdown in the chat header. Turn off the built-in one (Settings -> WebUI -> 'enable_model_switcher') or you get a second copy in the input bar.",
        },
        "show_settings_button": {
            "type": "boolean",
            "default": True,
            "description": "Show a settings gear in the header, replacing the one the core moved to the sidebar footer. Hidden automatically when admin settings are disabled (window.ADMIN_SETTINGS_DISABLED), exactly like the core button was.",
        },
        "show_stream_state": {
            "type": "boolean",
            "default": True,
            "description": "Show the stream-state bubble in the chat header: idle, thinking, streaming, calling tools, and so on.",
        },
        "state_style": {
            "type": "select",
            "default": "badge",
            "depends": {"show_stream_state": True},
            "description": "How the state bubble looks.",
            "options": {
                "badge": "Bordered pill (the old core look)",
                "text": "Plain text, no border",
                "dot": "Only a coloured dot; the full state shows on hover",
            },
        },
        "hide_state_when_idle": {
            "type": "boolean",
            "default": False,
            "depends": {"show_stream_state": True},
            "description": "Hide the bubble while nothing is happening, so only actual activity shows it.",
        },
        "header_slot": {
            "type": "select",
            "default": "right",
            "description": "Which core header extension slot renders these elements: 'right' sits with the header's controls, 'left' sits next to the title. A module always renders *inside* the slot that calls it, so 'before/after the slot' is not expressible here -- and across modules the order inside one slot is alphabetical by module name.",
            "options": {
                "right": "Right side, with the header controls",
                "left": "Left side, next to the title",
            },
        },
        "cluster_order": {
            "type": "number",
            "default": 0,
            "min": -999,
            "max": 999,
            "description": "Position inside the header cluster, as a CSS flex order. The cluster is a flex row, so this is the same trick chat-pro uses (it pins itself at order 99 to land after the core buttons). -1 renders before libreta and other default (0) modules; 0 keeps the alphabetical default; 100+ renders after chat-pro.",
        },
        "element_order": {
            "type": "select",
            "default": "state_first",
            "description": "Which element comes first when both are shown.",
            "options": {
                "state_first": "State bubble, then model dropdown",
                "model_first": "Model dropdown, then state bubble",
            },
        },
    }
