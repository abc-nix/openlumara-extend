// editor module: registers its own tool-call display views (full injector).
// The display system (registerToolDisplay, toolArgs, toolResponse) is core;
// this file only maps editor_* tools to view names. Markup lives in
// webui/templates/chat/tool_displays.html (tool_views slot), helpers in
// assets/js/tool_view_helpers.js, styling in assets/css/custom_tool_views/.
//
// header hints (the "+N −M" / "N lines" / "n matches · m files" lines) are
// NOT set here anymore: the registry only matches and names views, and
// core/turns.py asks the owning module's on_tool_summary() hook, which
// stamps _metadata.result_summary (see Editor.on_tool_summary).
document.addEventListener('alpine:init', () => {
    // editor_edit: diff view
    registerToolDisplay({
        match: /^editor_edit$/,
        view: 'editor-edit'
    });

    // editor_read: highlighted code block (response content)
    registerToolDisplay({
        match: /^editor_read$/,
        view: 'editor-code'
    });

    // editor_write/append/create: highlighted code block (streamed content)
    registerToolDisplay({
        match: /^editor_(write|append|create)$/,
        view: 'editor-code'
    });

    // editor_grep: file-grouped match list
    registerToolDisplay({
        match: /^editor_grep$/,
        view: 'editor-grep'
    });

    // editor_glob: file list; move/copy/delete/mkdir: running one-liner
    registerToolDisplay({
        match: /^editor_(glob|move|copy|delete|mkdir)$/,
        view: 'editor-path'
    });

    // editor_read_media: the image itself, re-fetched from the module route
    // (the tool result only carries metadata - the picture goes to the model
    // as its own internal user message, like browser_screenshot)
    registerToolDisplay({
        match: /^editor_read_media$/,
        view: 'editor-media'
    });
});
