/*
 * chat-pro - frontend half of the fork button.
 *
 * the button itself lives in webui/templates/chat/message_buttons.html, inlined
 * into the message action row, so it has the enclosing turn/turnIndex in scope.
 * a fork is a backend job (a new history file has to be written), so this store
 * only asks for it and then switches chats the same way the core does.
 */

const CHAT_PRO = {
    forking: false,

    async fork(turnIndex) {
        if (this.forking) { return; }

        const chat = Alpine.store("chat");
        const stream = Alpine.store("stream");
        const turn = (chat.turnHistory || [])[turnIndex];

        if (!turn) { return; }

        // the button is disabled while streaming; guard again for edge cases
        if (stream.state !== "idle") { return; }

        // an assistant turn ends at its last message (tool calls and tool
        // responses included); a user turn has exactly one, and forking there
        // keeps the unanswered prompt on purpose
        const index = turn.last_message_index ?? turn.first_message_index;
        if (index === null || index === undefined) { return; }

        const kept = turn.role === "user"
            ? "your message (the fork starts with it unanswered)"
            : "this reply";

        if (!confirm(
            `Fork from ${kept}?\n\n` +
            "A new chat gets a copy of the conversation up to this point, " +
            "with the same metadata and attachments. " +
            "The chat you are in stays exactly as it is."
        )) { return; }

        this.forking = true;

        try {
            const result = await simpleApiPost("/api/ext/chat-pro/fork", { index });
            if (!result || !result.id) { return; }

            // the backend broadcasts chat_switched as well: if that already
            // switched us, loadChat is a no-op, otherwise it switches here
            await chat.reloadChats();
            if (chat.selectedChat !== result.id) {
                await chat.loadChat(result.id);
            }
        } catch (error) {
            console.error("chat-pro: fork failed:", error);
            // ui.notice is rendered with x-html, so never put raw error text in it
            const text = String(error).replace(/[&<>"']/g, (ch) => ({
                "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
            }[ch]));
            const ui = Alpine.store("ui");
            ui.notice = `Fork failed: ${text}`;
            setTimeout(() => {
                if (ui.notice && ui.notice.startsWith("Fork failed:")) { ui.notice = null; }
            }, 6000);
        } finally {
            this.forking = false;
        }
    }
};

document.addEventListener("alpine:init", () => {
    Alpine.store("chatPro", CHAT_PRO);
});
