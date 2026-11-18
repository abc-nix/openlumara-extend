/*
 * abc-extras -- stream-state labels for the chat-header bubble.
 *
 * the backend drives $store.stream.state over the websocket (a "stream_state"
 * message per change), and the webui itself only ever compares it against a
 * couple of literals -- so the full set is easy to lose track of. these are
 * the states channels/webui/channel.py actually sets:
 *
 *   idle, message_sending, message_received, thinking, streaming,
 *   processing, processing_tools, calling_tools
 *
 * unknown states fall back to the raw string with underscores turned into
 * spaces, so a state added upstream still renders something sensible.
 */
const ABC_EXTRAS_STATES = {
    idle:             { label: "idle",            tone: "idle" },
    message_sending:  { label: "sending",         tone: "sending" },
    message_received: { label: "received",        tone: "received" },
    thinking:         { label: "thinking",        tone: "thinking" },
    streaming:        { label: "streaming",       tone: "streaming" },
    processing:       { label: "processing",      tone: "thinking" },
    processing_tools: { label: "preparing tools", tone: "tools" },
    calling_tools:    { label: "calling tools",   tone: "tools" },
};

function abcExtrasState(opts = {}) {
    return {
        // set from the template, which reads the module's settings
        hideWhenIdle: opts.hideWhenIdle === true,

        get state() {
            const store = Alpine.store("stream");
            return (store && store.state) || "idle";
        },

        // anything but idle counts as activity (drives the pulse and the
        // hide-while-idle option)
        get busy() {
            return this.state !== "idle";
        },

        get label() {
            const known = ABC_EXTRAS_STATES[this.state];
            return known ? known.label : this.state.replace(/_/g, " ");
        },

        get tone() {
            const known = ABC_EXTRAS_STATES[this.state];
            return known ? known.tone : "busy";
        },

        get visible() {
            return !this.hideWhenIdle || this.busy;
        },
    };
}
