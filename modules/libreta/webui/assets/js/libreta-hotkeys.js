/* libreta — keyboard shortcuts, in their own file.
 *
 * Only ACTIONS LIBRETA OWNS are listed here. Undo/redo and CodeMirror's
 * movement/selection keys are deliberately absent: they are dozens of bindings
 * that are not ours to rebind, and rebinding them is how an editor gets broken.
 *
 * STORAGE (2026-11-13): the per-browser overrides live in localStorage under
 * `libreta.keys` as {actionId: chord}. That is deliberate for now — it applies
 * immediately and needs no restart, like the other editor preferences
 * (show_line_numbers, viewPadding).
 *
 * FUTURE (when the data should follow the user, not the browser): move that
 * object to `data/libreta/hotkeys.json` behind a `state/hotkeys` route read at
 * page load, exactly like `data/libreta/autosave.json`. Everything here reads
 * through lbHotkeyMap(), so the storage swap stays inside this file. The
 * registry below is plain data and does not change.
 *
 * Chord syntax follows CodeMirror: modifiers first, `Mod` = Ctrl (Cmd on mac),
 * e.g. "Mod-s", "Mod-Alt-g", "Mod-Shift-[", "Escape". Shifted punctuation is
 * normalised to its base key so a captured "{" is stored as "[" — otherwise a
 * default like "Mod-Shift-[" would never match.
 */
(function () {
    "use strict";

    const LS_KEY = "libreta.keys";

    const MOD_ORDER = ["Mod", "Alt", "Shift"];

    // keydown fires for these before the real key; they are never a binding.
    const MODIFIER_KEYS = ["Control", "Shift", "Alt", "Meta", "OS", "AltGraph", "CapsLock"];

    // base key for the punctuation a US layout produces while Shift is held
    const SHIFTED = {
        "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
        "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=",
        "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'",
        "<": ",", ">": ".", "?": "/", "~": "`",
    };

    // the actions, in modal order. `where` decides who consumes the chord:
    // "editor" (CodeMirror keymap) or "window" (libreta's document listener).
    const HOTKEYS = [
        { id: "save", label: "Save the file", group: "File", def: "Mod-s", where: "window" },
        { id: "quickopen", label: "Quick open a file", group: "Navigate", def: "Mod-p", where: "window" },
        { id: "goto", label: "Go to line", group: "Navigate", def: "Mod-Alt-g", where: "editor" },
        { id: "find", label: "Find & replace", group: "Search", def: "Mod-f", where: "window" },
        { id: "comment", label: "Toggle comment (code mode)", group: "Code", def: "Mod-/", where: "editor" },
        { id: "fold", label: "Fold at cursor (code mode)", group: "Code", def: "Mod-Shift-[", where: "editor" },
        { id: "unfold", label: "Unfold at cursor (code mode)", group: "Code", def: "Mod-Shift-]", where: "editor" },
        { id: "pasteplain", label: "Paste as plain text", group: "Edit", def: "Mod-Shift-v", where: "window" },
        { id: "closecover", label: "Close the editor", group: "View", def: "Escape", where: "window" },
    ];

    function safeParse(raw) {
        try {
            const parsed = JSON.parse(raw || "{}");
            return parsed && typeof parsed === "object" ? parsed : {};
        } catch (e) {
            return {};
        }
    }

    // parsed overrides + chord->action lookup, cached until something writes.
    // Without the cache every keystroke in the page would parse localStorage
    // several times (the document listener looks up four actions per event).
    let mapCache = null;
    let chordCache = null;

    function lbHotkeyMap() {
        if (mapCache) return mapCache;
        try {
            mapCache = safeParse(localStorage.getItem(LS_KEY));
        } catch (e) {
            mapCache = {};
        }
        return mapCache;
    }

    function lbHotkeySaveMap(map) {
        mapCache = map;
        chordCache = null;
        try {
            localStorage.setItem(LS_KEY, JSON.stringify(map));
        } catch (e) {
            /* storage disabled: the session stays on the defaults */
        }
    }

    // chord -> action id, rebuilt when a binding changes
    function lbChordIndex() {
        if (chordCache) return chordCache;
        const index = {};
        for (const entry of HOTKEYS) {
            const chord = lbNormChord(lbHotkeyChord(entry.id));
            if (chord) index[chord] = entry.id;
        }
        chordCache = index;
        return index;
    }

    // the action a keyboard event maps to, or "" — one lookup per event
    function lbHotkeyAction(event) {
        const chord = lbChordOf(event);
        if (!chord) return "";
        return lbChordIndex()[chord] || "";
    }

    function lbHotkeyDef(id) {
        const entry = HOTKEYS.find((item) => item.id === id);
        return entry ? entry.def : "";
    }

    // effective chord: the stored override, "" when cleared, else the default
    function lbHotkeyChord(id) {
        const map = lbHotkeyMap();
        return Object.prototype.hasOwnProperty.call(map, id) ? map[id] : lbHotkeyDef(id);
    }

    function lbNormChord(chord) {
        const text = String(chord || "").trim();
        if (!text) return "";
        const parts = text.split("-");
        const key = parts.pop();
        const mods = MOD_ORDER.filter((mod) => parts.includes(mod));
        // anything left in `parts` that is not a known modifier is ignored
        return mods.concat([key.length === 1 ? key.toLowerCase() : key]).join("-");
    }

    // the chord an event represents, normalised the same way as stored chords
    function lbChordOf(event) {
        const mods = [];
        if (event.ctrlKey || event.metaKey) mods.push("Mod");
        if (event.altKey) mods.push("Alt");
        if (event.shiftKey) mods.push("Shift");

        let key = event.key || "";
        if (key === " " || key === "Spacebar" || key === "Space") key = "Space";
        if (key.length === 1) {
            const base = SHIFTED[key];
            key = (base || key).toLowerCase();
        }
        if (!key) return "";
        return mods.concat([key]).join("-");
    }

    function lbHotkeyMatches(event, id) {
        const wanted = lbNormChord(lbHotkeyChord(id));
        if (!wanted) return false;
        return lbChordOf(event) === wanted;
    }

    // a binding must be reachable with one hand and not steal plain typing
    function lbChordAllowed(chord) {
        if (!chord) return false;
        const key = chord.split("-").pop();
        // a modifier alone is not a chord, whatever the event order said
        if (MODIFIER_KEYS.includes(key) || key === "") return false;
        if (key === "Escape") return true;
        if (/^F\d{1,2}$/.test(key)) return true;
        return chord.includes("Mod-") || chord.startsWith("Alt-") || chord.includes("-Alt-") || chord.includes("-Shift-");
    }

    function lbHotkeyList() {
        return HOTKEYS.map((entry) => ({
            id: entry.id,
            label: entry.label,
            group: entry.group,
            def: entry.def,
            where: entry.where,
            chord: lbHotkeyChord(entry.id),
            custom: Object.prototype.hasOwnProperty.call(lbHotkeyMap(), entry.id),
        }));
    }

    // ----------------------------------------------------------------- store
    document.addEventListener("alpine:init", () => {
        Alpine.store("libretaHotkeys", {
            open: false,
            recording: "",
            error: "",
            draft: {},      // id -> chord while the modal is open

            list() {
                // Plain property reads on the draft: Alpine tracks those.
                // hasOwnProperty() on its proxy is NOT tracked, which is why a
                // fresh binding only appeared after closing and reopening the
                // modal. The draft is seeded with every chord on show().
                return HOTKEYS.map((entry) => ({
                    id: entry.id,
                    label: entry.label,
                    group: entry.group,
                    def: entry.def,
                    where: entry.where,
                    chord: this.draft[entry.id],
                    custom: this.draft[entry.id] !== entry.def,
                }));
            },

            groups() {
                const seen = [];
                for (const row of this.list()) {
                    if (!seen.includes(row.group)) seen.push(row.group);
                }
                return seen;
            },

            show() {
                this.open = true;
                this.recording = "";
                this.error = "";
                // seed the draft with the effective chords: from here on the
                // modal only reads draft[id], so edits re-render immediately
                const draft = {};
                for (const entry of HOTKEYS) draft[entry.id] = lbHotkeyChord(entry.id);
                this.draft = draft;
            },

            hide() {
                this.open = false;
                this.recording = "";
                this.error = "";
                this.draft = {};
            },

            start(id) {
                this.recording = id;
                this.error = "";
            },

            stop() {
                this.recording = "";
            },

            // returns the id of another action already using `chord`, or ""
            conflict(id, chord) {
                const hit = this.list().find((row) => row.id !== id && row.chord === chord);
                return hit ? hit.label : "";
            },

            assign(id, chord) {
                if (!lbChordAllowed(chord)) {
                    this.error = "Use a modifier (Ctrl/Alt/Shift) or a function key.";
                    return;
                }
                const other = this.conflict(id, chord);
                if (other) {
                    this.error = "Already used by \u201c" + other + "\u201d.";
                    return;
                }
                this.draft[id] = chord;
                this.error = "";
            },

            clear(id) {
                this.draft[id] = "";
                this.error = "";
            },

            reset(id) {
                this.draft[id] = lbHotkeyDef(id);
                this.error = "";
            },

            resetAll() {
                const draft = {};
                for (const entry of HOTKEYS) draft[entry.id] = entry.def;
                this.draft = draft;
                try { localStorage.removeItem(LS_KEY); } catch (e) { /* ignore */ }
                lbHotkeyInvalidate();
                this.error = "";
            },

            save() {
                const map = {};
                for (const entry of HOTKEYS) {
                    // only what differs from the default is stored, so a later
                    // change of a default is picked up instead of frozen
                    const chord = this.draft[entry.id];
                    if (chord !== entry.def) map[entry.id] = chord;
                }
                lbHotkeySaveMap(map);
                this.draft = {};
                this.hide();
                // the editor keymap is a compartment: reconfigure it now, and
                // let libreta re-apply the window-level lookups by reloading
                // the bindings it holds.
                if (window.LibretaHotkeysChanged) window.LibretaHotkeysChanged();
            },
        });
    });

    // ---------------------------------------------------------- capture
    // Capture runs on the capture phase and stops the event before libreta's
    // own document listeners see it, so recording "Mod-s" never saves the file.
    document.addEventListener(
        "keydown",
        (event) => {
            let store;
            try { store = Alpine.store("libretaHotkeys"); } catch (e) { return; }
            if (!store || !store.open || !store.recording) return;

            event.preventDefault();
            event.stopImmediatePropagation();

            // Pressing Ctrl/Shift/Alt on its own is the START of a chord, not a
            // chord: keydown fires for the modifier first, and treating that as
            // the finished binding ended recording before the letter arrived.
            if (MODIFIER_KEYS.includes(event.key)) return;

            if (event.key === "Escape") {
                store.stop();
                return;
            }
            if (event.key === "Backspace" || event.key === "Delete") {
                store.clear(store.recording);
                store.stop();
                return;
            }
            const chord = lbChordOf(event);
            if (!chord) return;
            store.assign(store.recording, chord);
            store.stop();
        },
        true
    );

    // Esc closes the modal itself (unless the capture above already handled it)
    document.addEventListener("keydown", (event) => {
        let store;
        try { store = Alpine.store("libretaHotkeys"); } catch (e) { return; }
        if (store && store.open && !store.recording && event.key === "Escape") {
            store.hide();
        }
    });

    function lbHotkeyInvalidate() {
        mapCache = null;
        chordCache = null;
    }

    // A chord that USED to belong to an action but no longer does. Swallowing it
    // keeps the browser's own shortcut from firing (Ctrl+S would open "Save
    // page" the moment Save is rebound elsewhere).
    function lbHotkeyIsMovedDefault(event) {
        const chord = lbChordOf(event);
        if (!chord) return false;
        for (const entry of HOTKEYS) {
            if (lbNormChord(entry.def) === chord && lbNormChord(lbHotkeyChord(entry.id)) !== chord) {
                return true;
            }
        }
        return false;
    }

    // exposed for libreta.js (separate classic script, same global scope)
    window.LB_HOTKEY_ACTIONS = HOTKEYS;
    window.lbHotkeyAction = lbHotkeyAction;
    window.lbHotkeyInvalidate = lbHotkeyInvalidate;
    window.lbHotkeyIsMovedDefault = lbHotkeyIsMovedDefault;
    window.lbHotkeyList = lbHotkeyList;
    window.lbHotkeyChord = lbHotkeyChord;
    window.lbHotkeyDef = lbHotkeyDef;
    window.lbHotkeyMatches = lbHotkeyMatches;
    window.lbChordOf = lbChordOf;
    window.lbNormChord = lbNormChord;
})();
