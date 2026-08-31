"""
The browser skin, as three real static files.

`static/index.html`, `static/app.css` and `static/app.js` are the front end -
markup, stylesheet and behaviour, each in its own file the way any other web
project keeps them, editable with the server stopped and deployable as-is (to
Cloudflare Pages, or any static host) with `--build-static`.

This module is the composer, and it composes in two shapes:

* **`page()`** - one self-contained document, CSS and JS inlined. This is what
  the local server sends and what `web-preview.html` is, and it is why the app
  still works with the network down: no CDN, no webfont, no external request.
* **`static_files()`** - the same three files as a deployable site, with the
  palette and the icon table baked in.

Both come out of the same assets, so neither can drift from the other, and both
are still generated rather than hand-maintained: the palette comes from
`viewmodel.py`, the same constants every other surface reads, so a colour change
is one edit and cannot leave two skins disagreeing. `@@TOKEN@@` replacement (not
str.format) because CSS and JS are full of braces.

Everything is offline-safe: system fonts, inline SVG paths, no request that
leaves the machine except the local API. No server data is ever assigned with
innerHTML - titles and artist names go in as textContent, so a track called
`<img onerror=alert(1)>` is a song title and nothing else.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import viewmodel as vm


# The palette, as tokens the stylesheet names. It comes from `viewmodel.py` - the
# same constants the CLI and every other surface read - so a colour is edited in
# one place and cannot leave two skins disagreeing.
COLORS = {
    "@@BG@@": getattr(vm, "BG", "#121212"),
    "@@PANEL@@": getattr(vm, "PANEL", "#181818"),
    "@@CARD@@": getattr(vm, "CARD", "#232323"),
    "@@EDGE@@": vm.PANEL_EDGE,
    "@@HOVER@@": vm.HOVER,
    "@@INPUT@@": vm.INPUT_BG,
    "@@TEXT@@": vm.TEXT,
    "@@MUTED@@": vm.MUTED,
    "@@FAINT@@": vm.MUTED_DK,
    "@@ACCENT@@": vm.ACCENT,
    "@@ACCENT_DK@@": vm.ACCENT_DK,
    "@@PLAYING@@": vm.PLAYING_BG,
    "@@ERROR@@": vm.ERROR,
    "@@HEART@@": getattr(vm, "HEART", "#1ed760"),
    "@@TILES@@": ",".join(vm.TILE_PALETTE),
}


# ------------------------------------------------------------------ the assets
# The three files in static/ are the source of truth. They are read through one
# cached helper because `/` and `/app.css` are both rendered per request, and a
# cold read of 90 KB on every tick-adjacent hit is the kind of thing that only
# shows up as "the page feels slow on a laptop".
STATIC_DIR = Path(__file__).resolve().parent / "static"
_CACHE: dict[str, tuple[str, float]] = {}


def _asset(name: str) -> str:
    """
    One cached read of a file in static/, keyed by the file's mtime.

    The mtime is the point: you can edit app.css with the server running and
    reload, which is the whole reason the front end is a file on disk now
    instead of a string inside a .py. A missing asset is reported as an empty
    string so the page still builds and the log names what is gone.
    """
    path = STATIC_DIR / name
    try:
        stamp = path.stat().st_mtime
    except OSError as e:
        print(f"webapp: could not read static/{name}: {e}", file=sys.stderr)
        _CACHE.pop(name, None)
        return ""
    hit = _CACHE.get(name)
    if hit and hit[1] == stamp:
        return hit[0]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"webapp: could not read static/{name}: {e}", file=sys.stderr)
        return ""
    _CACHE[name] = (text, stamp)
    return text


def forget_assets() -> None:
    """Drop the asset cache - tests write their own copies and want them seen."""
    _CACHE.clear()


def _load_icons() -> dict:
    """The icon table is an asset too: 23 inline SVG paths in static/icons.json.

    Markup names one with `@@name@@` and the script looks the same one up at
    runtime, so both read one copy rather than keeping two that can drift.
    """
    try:
        got = json.loads(_asset("icons.json") or "{}")
    except ValueError:
        got = {}
    return got if isinstance(got, dict) else {}


ICONS = _load_icons()


def _icons_js() -> str:
    """The same SVG strings, for the buttons the JS builds at runtime."""
    return json.dumps(ICONS)


def app_constants() -> dict:
    """
    The small tables the page shares with the rest of the app, as JSON-ready data.

    Moods, views and the greeting ranges live in `viewmodel.py`; inlining them here
    is what stops the browser skin from keeping a second copy of a list that is
    supposed to be the product's opinion about what people want to listen to.
    """
    return {"moods": [{"label": label, "q": q} for label, q in vm.MOODS],
            "views": list(vm.VIEWS),
            "nav": [{"view": v, "icon": i, "label": l} for v, i, l in vm.NAV],
            "greeting": [list(g) for g in vm.GREETING]}


# The two tags `page()` rewrites to inline the assets. Written as whole tags so
# a search for app.css in the repo finds the link and the file together.
CSS_LINK = '<link rel="stylesheet" href="app.css">'
JS_SRC = '<script src="app.js"></script>'
DATA_TAG = "@@APP_DATA@@"


def body_markup() -> str:
    """The markup alone - index.html between `<body>` and the data script."""
    html = _asset("index.html")
    start = html.find("<body>")
    end = html.find("<script>", start)
    if start == -1 or end == -1:
        return ""
    return html[start + len("<body>"):end].strip("\n")


def __getattr__(name: str) -> str:
    """
    `BODY` / `CSS` / `JS` read through to the static files.

    They were string literals in this module for a long time and the tests, the
    doctor and the preview all read them by name. Rather than leave three stale
    names pointing at nothing, they now resolve to the files that replaced them
    - so `webapp.BODY` is still "the markup, with its @@icon@@ tokens", only it
    is the markup the browser is actually served.
    """
    if name == "BODY":
        return body_markup()
    if name in ("CSS", "JS"):
        return _asset("app.css" if name == "CSS" else "app.js")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _substitute(text: str) -> str:
    """Icons and palette, in that order (an SVG path can carry @@ never)."""
    for key, svg in ICONS.items():
        text = text.replace(f"@@{key}@@", svg)
    for tok, val in COLORS.items():
        text = text.replace(tok, val)
    return text


def app_css() -> str:
    """The stylesheet, with the palette from `viewmodel` substituted in."""
    return _substitute(_asset("app.css"))


def app_js() -> str:
    """The behaviour. No substitution: the data it needs is inline above it."""
    return _asset("app.js")


def app_data() -> str:
    """The generated data the script reads: the icon table and viewmodel's tables."""
    return ("const ICONS=" + _icons_js() + ";\n"
            + "const VM=" + json.dumps(app_constants()) + ";\n")


def shell() -> str:
    """index.html with its icons substituted and its data block filled in."""
    html = _asset("index.html")
    if not html:
        return ""
    return _substitute(html).replace(DATA_TAG, app_data())


def page() -> str:
    """
    The finished document: CSS and JS inlined, self-contained, no outbound request.

    This is what the local server sends and what `web-preview.html` is, and it
    is the form the tests read. One request, one file, works with the network
    unplugged - the promise the old single-string page made, kept.

    The static site carries two script elements (generated data, then the
    script); the one-document form merges them into one. Two inline scripts is
    two chances for the page to half-load, and it puts a literal `</script>`
    inside what a reader - or `node --check` - takes for one script body.
    """
    html = shell()
    if not html:
        return ""
    html = html.replace(CSS_LINK, "<style>" + app_css() + "</style>")
    one = "<script>" + app_data() + app_js() + "</script>"
    merged, n = re.subn(r"<script>.*?</script>\s*" + re.escape(JS_SRC),
                        lambda _m: one, html, flags=re.S)
    return merged if n else html.replace(JS_SRC, one)


ASSET_TYPES = {"/app.css": ("text/css; charset=utf-8", app_css),
               "/app.js": ("application/javascript; charset=utf-8", app_js)}


def asset(path: str) -> tuple[bytes, str] | None:
    """
    One static file by URL path: /app.css or /app.js -> (body, content type).

    This is what makes the front end a static site rather than a string the
    server holds: same-origin, no CDN, still nothing leaving the machine.
    """
    entry = ASSET_TYPES.get(str(path or ""))
    if not entry:
        return None
    ctype, build = entry
    return build().encode("utf-8"), ctype


def static_files() -> dict[str, bytes]:
    """
    The deployable site: index.html + app.css + app.js, tokens baked in.

    `python3 -m spotube_dj --build-static out/` writes these to a directory;
    from there it is any static host, including Cloudflare Pages next to the
    Worker. The page still needs the local server for /api/*, so this is a
    deployable front end, not a hosted player.
    """
    return {"index.html": shell().encode("utf-8"),
            "app.css": app_css().encode("utf-8"),
            "app.js": app_js().encode("utf-8"),
            "favicon.svg": (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                f'<rect width="24" height="24" rx="6" fill="{vm.ACCENT}"/>'
                '<path d="M7 6.5v11L19 12z" fill="#000"/></svg>').encode("utf-8")}


def build_static(out_dir) -> list[str]:
    """Write `static_files()` to `out_dir`. -> the files written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, body in static_files().items():
        (out / name).write_bytes(body)
        written.append(name)
    return written


if __name__ == "__main__":            # python3 -m webapp > /tmp/ui.html
    print(page())
