// -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-26) harness:openlumara status:reviewed
// screenshot view: the tool result is plain text (path + geometry) and the
// picture is fetched from /api/ext/browser/shot, so nothing image-shaped ever
// enters the tool message. markup in webui/templates/chat/tool_displays.html,
// styling in webui/assets/css/browser.css.
document.addEventListener('alpine:init', () => {
    registerToolDisplay({
        match: /^browser_screenshot$/,
        view: 'browser-shot'
    });
});
