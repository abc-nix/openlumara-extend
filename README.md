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

A library repo must mirror OpenLumara's own folder layout — `core/module_manager.py`
only picks entries up from these folders:

| folder in this repo | installed as | entry file |
|---|---|---|
| `modules/<name>/` | user module (`user_modules/<name>`) | `module.py` |
| `channels/<name>/` | user channel (`user_channels/<name>`) | `channel.py` |
| `skills/<name>/` | skill | `SKILL.md` |
| `themes/<name>.json` | webui theme | (json, optional `.css` sidecar) |

A `module.py` sitting at the repo root, or in any other folder, is ignored.

## How it is wired on this machine

```
~/git/openlumara/user_modules  ->  ~/git/openlumara-extend/modules
```

That symlink means editing a module through the usual
`openlumara/user_modules/<name>/` path edits the files here directly (no copy,
no sync step). Consequences:

- OpenLumara's own checkout keeps `user_modules/` gitignored, so `git status`
  there stays clean — this repo is the source of truth for module changes.
- After editing `module.py`, a settings save reloads it; edits to helper files
  (`cdp.py`, `page.py`, …) need a full server restart, not a reload.
- Never copy `__pycache__/` in when moving a module; it is ignored anyway.

## Modules

| module | notes |
|---|---|
| `browser` | Chrome/Chromium over CDP. Open backlog in `modules/browser/REVIEW.md` (items B1–B25, phases in §9). |
| `theme_gallery` | WebUI theme browser; see `BUGS.md` / `ELEMENTS.md` next to it. |

## Screenshot grid (browser module)

The grid is drawn **onto the captured PNG** (`page.draw_grid`), never into the
page. The old in-page canvas overlay needed a full-size canvas, which the
browser refuses past 16000 px per dimension — exactly the tall full-page
captures where a grid is most useful — and it briefly mutated the page the AI
may inspect next.

- Lines sit on document-coordinate multiples of the spacing; labels read as
  document coordinates and the origin is the `region` the tool reports.
- `grid` requires `format="png"`: jpeg + grid is refused rather than silently
  changing the format.
- Measured: **0.76 s** for a 1272x1305 capture, ~0.5 s per extra megapixel.
- `full_page_max_height` (default 8000): Chrome paints the **whole page** for
  `captureBeyondViewport` regardless of any clip (measured 2026-10-27 — an 8k
  clip on a 40k page still ran past 10 s), so a taller page is refused up front
  with its real height instead of stalling into the 15 s tool budget. A whole
  Wikipedia article is ~40k px.

## Tests

The stateless helpers in `page.py` have a pure-stdlib suite: it stubs `core`
and `aiohttp`, so it runs anywhere without touching the install, and it never
imports the entry `module.py`.

```
python3 -m unittest discover -s modules/browser/tests -v
```

## Conventions

- One branch per fix phase (`browser/phase-1-settings`, …), squash-merged into
  `master`; a bad phase is then one `git revert` away.
- A docstring changes in the same commit as the behavior it describes.
- Never commit credentials or local data — module settings live in
  `openlumara/config.yml`, which stays in (and ignored by) the main checkout.
- Module code follows the upstream template:
  `modules/coder/templates/openlumara_module.py`.
