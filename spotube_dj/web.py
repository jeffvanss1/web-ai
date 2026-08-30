"""
The front end for the same DJ: a browser tab, on a localhost server.

Why this replaced the Tk window: a window can only be as good as its toolkit
lets it be - no sub-pixel text, no rounded anything, icons that had to be
characters, and no way to blur a cover behind the page. A browser does all of
that with CSS. The engine is already separated from the display (everything the
UI shows is either `DJ.status()` or a rule in `viewmodel.py`), so the page is a
reader of that, not a second implementation. This file talks to `DJ` exactly the way
`--daemon` does, and still opens the control API on its own port, so the terminal
verbs (`spotube-dj next`) keep working against a browser-driven session.

What it deliberately is not:

* not a server. It binds 127.0.0.1, refuses a `Host:` header from anywhere else
  (that is also the DNS-rebinding guard), and serves one HTML document with no
  outbound requests - no CDN, no fonts, works with the network down.
* not the player. Audio stays in mpv, driven by `DJ`. `--web --backend none`
  gives a queue you can drive from the phone-ish layout, and the Open button
  hands the URL to `playerctl`/the browser like the CLI does.
* not authenticated, so it is localhost-only by construction. Don't port-forward
  it; anyone who can reach the port can control your speakers.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent as agent_mod
import config
import covers
import player as player_mod
import providers as prov
import taste
import thumbs
import viewmodel as vm
import webapp

DEFAULT_PORT = 8766
ART_NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}\.(?:png|jpe?g|gif)$")
# a state push per this many seconds; the progress bar is the thing that cares
TICK = 0.7


# ------------------------------------------------------------------ the state
def row_view(t: dict, *, note: str = "", liked: bool | None = None) -> dict:
    """One queue/search row, as the page draws it (never a raw internal dict)."""
    t = t or {}
    thumb = str(t.get("thumbnail") or "")
    out = {
        "id": str(t.get("id") or ""),
        "title": str(t.get("title") or "?"),
        "artist": str(t.get("artist") or ""),
        "channel": str(t.get("channel") or t.get("uploader") or ""),
        "dur": vm.mmss(t.get("duration") or 0) if t.get("duration") else "",
        "found": str(t.get("query") or ""),        # which search turned this up
        "cached": bool(t.get("cached") or t.get("from_cache")),
        # the row's own cover is shown immediately (a search/album/discography row
        # carries one); the artwork lane still upgrades it to the cached album art
        # when that lands, and to nothing smarter if the row has no url
        "art": thumb if thumb.startswith("http") else "",
        "art_card": thumb if thumb.startswith("http") else "",
        # the raw innerTube thumbnail rides along too, so the page's art() has a
        # last-resort URL even if some later pass blanked the two slots above: a
        # queue row or an album-page row that only carried `thumbnail` must still
        # be dressed rather than drawn as a tinted initial.
        "thumbnail": thumb if thumb.startswith("http") else "",
        # a page row may carry its own badge (a discography entry's year, an album
        # track's record name); an explicit `note` from the caller wins, else use it
        "note": note or (str(t.get("note") or "") if isinstance(t.get("note"), str)
                         else ""),
    }
    # page rows are not always a playable song: a discography entry is an *album*
    # that opens on a click, so its kind / browse id / record facts travel with it
    for extra in ("kind", "album", "browse_id", "release_year"):
        if t.get(extra) is not None:
            out[extra] = t[extra]
    if liked is not None:
        out["liked"] = bool(liked)
    return out


def _ago(ts, now_ts: float | None = None) -> str:
    """'just now' / '14 m' / '3 h' / '2 wk' - how long ago a history row is."""
    try:
        then = float(ts)
        now = float(now_ts if now_ts is not None else time.time())
    except (TypeError, ValueError, OverflowError):
        return ""
    # both ends must be real numbers: a NaN (json accepts it) or an infinity would
    # otherwise be clamped to 0 by the max() below and read as "just now"
    if not (math.isfinite(then) and math.isfinite(now)):
        return ""
    secs = max(0.0, now - then)
    for span, unit in ((86400 * 7, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if secs >= span:
            return f"{int(secs // span)}{unit} ago"
    return "just now"


def library_view(prof: dict, limit: int = 12) -> dict:
    """
    The three lists the sidebar shows, in the order a listener expects them.

    Loved songs and the artists the profile leans on come from the state file;
    "recently played" comes from the history log, which is the only place that
    knows what was *heard* rather than what was queued. Nothing here reaches the
    network, because this is rebuilt on every state tick: no cover lookups, no
    search, only fields already on disk. Rows without a real video id carry no
    art, and the page draws a tinted tile for those.
    """
    now_ts = time.time()
    liked = (prof.get("liked") or [])[-limit:]
    # row_view is what gives a row the keys the page reads - `art` and `art_card`
    # included, which a hand-built loved row used to omit, so the loved list could
    # not be stamped even after its covers were on disk. The id travels with it for
    # the same reason: without one there is nothing to look a cover up by, which is
    # why the whole left column was a row of letters.
    loved = [dict(row_view({"id": str(r.get("id") or ""),
                            "title": r.get("display_title") or r.get("title") or "?",
                            "artist": r.get("display_artist") or r.get("artist") or ""}),
                  note=_ago(r.get("ts"), now_ts) or "loved",
                  q=" ".join(x for x in (r.get("display_artist") or r.get("artist"),
                                         r.get("display_title") or r.get("title"))
                             if x).strip())
             for r in reversed(liked)]
    weights = prof.get("artists") or {}
    counts: dict[str, int] = {}
    for r in (prof.get("liked") or []):
        key = taste.norm(r.get("artist") or "")
        counts[key] = counts.get(key, 0) + 1
    artists = [{"name": name, "w": round(float(w or 0), 1), "loved": counts.get(name, 0),
                 "q": f"songs like {name}"}
               for name, w in sorted(weights.items(), key=lambda kv: -abs(float(kv[1] or 0)))
               if abs(float(w or 0)) > 0][:limit]
    moods: list[dict] = []
    seen: set[str] = set()
    for row in reversed(config.load_history(limit=80)):
        q = str(row.get("query") or "").strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        moods.append({"q": q, "note": _ago(row.get("ts"), now_ts)})
        if len(moods) >= limit:
            break
    if not moods and prof.get("last_request"):
        moods = [{"q": str(prof["last_request"]), "note": ""}]
    recents = [dict(row_view({"id": row.get("id") or "", "title": row.get("title") or "?",
                              "artist": row.get("artist") or ""}),
                    ts=_ago(row.get("ts"), now_ts))
               for row in reversed(config.load_history(limit=limit))]
    return {"loved": loved, "artists": artists, "moods": moods, "recents": recents,
            "counts": {"loved": len(prof.get("liked") or []), "artists": len(artists),
                       "recents": len(recents)}}


def loved_rows(limit: int = 60, state: dict | None = None) -> list[dict]:
    state = state if state is not None else taste.load_state()
    rows = []
    for r in (state.get("liked") or [])[-limit:]:
        rows.append({"id": str(r.get("id") or ""),
                     "title": r.get("display_title") or r.get("title") or "?",
                     "artist": r.get("display_artist") or r.get("artist") or "",
                     "channel": "", "dur": "", "art": "", "note": "loved"})
    return rows


def mask(key: str) -> str:
    """Show enough of an API key to confirm which one got saved, and no more."""
    k = str(key or "")
    if len(k) <= 8:
        return "·" * len(k)
    return f"{k[:3]}{'·' * 6}{k[-3:]}"


def settings_view() -> dict:
    """
    What the page's Settings panel shows.

    The saved key is deliberately not included, not even masked from the file: the
    mask is computed from the live value so the panel can say "ends ···abc" without
    a request ever carrying the secret.
    """
    import brain
    data = config.load_llm_config()
    key = str(config.LLM_API_KEY or data.get("LLM_API_KEY") or "")
    return {"key_set": bool(key), "key_mask": mask(key),
            "base": str(config.LLM_BASE_URL or data.get("LLM_BASE_URL") or ""),
            "model": str(data.get("LLM_MODEL") or config.LLM_MODEL
                         or config.GEMINI_DEFAULT_MODEL),
            "dj_voice": config.load_dj_voice(),
            "dj_voice_model": str(config.GEMINI_DEFAULT_TTS_MODEL),
            "dj_voices": [{"name": name, "gender": gender, "trait": trait,
                           "lang": lang if lang != "English" else ""}
                          for name, gender, trait, lang in config.GEMINI_TTS_VOICES],
            "dj_lead": float(config.DJ_LEAD_SECS or 10.0),
            "engine": brain.configured_engine(),
            "note": brain.why_offline()}


def engine_note(info: dict) -> str:
    """
    The engine line in the page header: which planner answered, in one phrase.

    Both skins ask `viewmodel` rather than each keeping a copy of "what do I say
    when the LLM fell back", which is how a UI drifts into contradicting the other.
    """
    info = info or {}
    line, _colour = vm.human_status(str(info.get("engine") or ""),
                                    str(info.get("llm_error") or ""))
    return line


def engine_pill(info: dict) -> str:
    """
    The header pill: `engine_note` cut to a label, because the pill is 24px tall.

    The full sentence stays in the tooltip; a note that reads "Built-in planner (works
    with no ..." in the bar looks like the app is broken, and it is the kind of thing
    only a screenshot catches.
    """
    note = engine_note(info)
    if not note:
        return "brain: offline"
    for cut in (" (", " - ", ". ", "; "):
        head = note.split(cut)[0].strip()
        if head:
            return head[:34]
    return note[:34]


def dj_line(ctx) -> str:
    """A short Spotify-DJ-style line: why this song is playing + what's next."""
    try:
        return agent_mod.narrate(agent_mod.dj_snapshot(ctx))
    except Exception:
        return ""                       # a broken snapshot must not take the page down


def find_track(ctx, tid: str) -> dict | None:
    """Look an id up across everything the page can click, queue included."""
    tid = str(tid or "")
    if not tid:
        return None
    dj = ctx.dj
    pools = [dj.queue.items, ctx.search.get("rows") or [],
             [dj.current] if dj.current else []]
    for pool in pools:
        for t in pool or []:
            if t and str(t.get("id") or "") == tid:
                return t
    return None


# what each slot of the page is drawn at, and what it may borrow. A small slot can
# always take a bigger file (the browser downscales, and it looks better); a big slot
# must never take a smaller one, which is the exact mistake that made the card grid
# a mosaic of 64 px thumbnails smeared to 190.
ART_SIZES = ("row", "card", "big")
ART_BORROW = {"row": ("row", "card", "big"), "card": ("card", "big"), "big": ("big",)}


JARGON = re.compile(r"\b(parse|facets?|llm|prompt|token|schema|json|http \d{3}"
                    r"|generator|fallback|model)\b", re.I)


def human_why(raw, dj) -> str:
    """
    The planner's note, only in words a listener would use.

    `dj.info["why"]` carries things like "offline parse: 3 facets" - true, and
    useless in a panel titled WHY THIS SONG, where it reads as a bug. Engine
    vocabulary is dropped and what is left is what the set was actually built
    from: the request, or the likes it came from.
    """
    text = str(raw or "").strip()
    if text and not JARGON.search(text):
        return text[:180]
    req = str(getattr(dj, "request", "") or "").strip()
    if req:
        return f'from "{req[:70]}"'
    try:
        likes = len((taste.load_state().get("liked") or []))
    except Exception:
        likes = 0
    if likes:
        return f"built from your {likes} loved track{'s' if likes != 1 else ''}"
    return ""


def build_state(ctx) -> dict:
    dj = ctx.dj
    st = dj.status()
    np = st.get("now_playing") or {}
    rows = []
    playing = str(np.get("id") or "")
    # the page has a card grid and a list, so it wants more than a status line's
    # worth of rows; the queue is right here, so read 12 rather than the 5 the
    # CLI prints (an observer still observes: no side effects on this path)
    upcoming = list(st.get("up_next") or [])
    try:
        if len(upcoming) < 12:
            upcoming = [dict(x) for x in dj.queue.upcoming(12)]
    except Exception:
        pass                            # a double without a queue keeps the short view
    for t in upcoming:
        # "Next playing still bugging, the chronology is not synchronised" was the
        # complaint: the row that is audible must not also be the first "up next".
        # status() reads the queue from its cursor, so in a mid-advance state that
        # row can still be here - the skin drops it rather than trusting the timing.
        if playing and str(t.get("id") or "") == playing:
            continue
        note = "cached" if t.get("cached") else ("from your likes" if t.get("mixed") else "")
        rows.append(row_view(t, note=note))
    now = row_view(np, liked=dj.is_liked(np)) if np else {}   # {} = nothing playing
    if now:
        # album / release-year are fetched once per track on a background lane and
        # merged in here, so the Credits block can name the record without ever
        # holding the state socket open
        now = {**now, **ctx.meta_for(np)}
    idle = str(st.get("idle") or "")
    why = {"finished": "the queue ran out - press Play or type a mood to keep going",
           "no stream would start": ("this song is still playing; the next few streams "
                                     "refused to start (rate limit or expired signature), "
                                     "so it retries by itself")}.get(idle, "")
    if not why:
        why = human_why((dj.info or {}).get("why"), dj)
    if getattr(dj, "station", ""):
        # say what a set is built around, or 20 similar tracks just look random
        why = (why + "  ·  " if why else "") + f"station: {dj.station}"
    # one string for both slots: the page shows `idle_note || why`, and a state
    # consumer that reads only `why` deserves the station suffix and the same
    # "queue ran out" explanation rather than a second, thinner version of it
    prof = taste.load_state()
    lib = library_view(prof)
    lib_loved = list(lib.get("loved") or [])[:10]
    lib_recents = list(lib.get("recents") or [])[:10]
    artists = sorted((prof.get("artists") or {}).items(), key=lambda kv: -kv[1])
    tags = sorted((prof.get("genres") or {}).items(), key=lambda kv: -kv[1])
    cache = st.get("cache") or [0, 0]
    # three slots, three sizes. The grid is warmed only as far as the first
    # screenful: asking for 200 covers nobody scrolled to is how a list gets slow,
    # and request_art is a dict probe per row, so calling it every tick is free.
    # card first: it is what the eye is on, and once a 256px file exists a 40px row
    # borrows it instead of downloading a second copy of the same picture
    # Warm every row a person can see, not just the first screenful. The old caps
    # (12 card / 14 row) were the "covers don't show on every song" report: a 40
    # track queue dressed the first screen, and the rows past the cap stayed as
    # coloured initials for the whole session. The lane is still bounded by its
    # own queue size and the seen-set, so this is "all of them in order", not
    # "an unbounded burst".
    ctx.request_art(upcoming, "card", limit=200)
    ctx.request_art(upcoming, "row", limit=200)
    ctx.request_art(lib_loved + lib_recents, "card", limit=60)
    ctx.request_art(lib_loved + lib_recents, "row", limit=60)
    if np:
        ctx.request_art([np], "big", limit=1)
    # the in-app album/artist page: warm the covers too, so an album tracklist or a
    # discography is dressed rather than a column of initial tiles
    if isinstance(ctx.page, dict) and ctx.page.get("rows"):
        pg_rows = [x for x in ctx.page["rows"] if (x or {}).get("id")]
        ctx.request_art(pg_rows, "card", limit=60)
        ctx.request_art(pg_rows, "row", limit=60)
    return {
        "now": now,
        "up_next": rows,
        "queued": int(st.get("queued") or 0),
        "position": float(st.get("position") or 0),
        "duration": float(st.get("duration") or 0),
        "paused": bool(st.get("paused")),
        "auto": bool(st.get("auto")),
        "autoplay": bool((dj.state or {}).get("autoplay")),
        "voice": bool((dj.state or {}).get("voice", True)),
        "voice_note": (("gemini · " + config.load_dj_voice()) if config.LLM_API_KEY
                       else "offline (no key)"),
        "volume": ctx.volume,
        "backend": str(st.get("backend") or ""),
        "idle": idle,
        "idle_note": why,
        "why": why,
        "vibe": str((dj.info or {}).get("vibe") or ""),   # "lofi tuesday night"
        "dj_line": dj_line(ctx),    # the DJ says why this song + what's next
        "request": str(st.get("request") or ""),
        "queries": list(st.get("queries") or []),
        "engine_note": engine_note(dj.info),
        "engine_pill": engine_pill(dj.info),
        "cache_note": (f"audio cache: {int(cache[1]) if len(cache) > 1 else 0} stored"
                       f", {int(cache[0]) if cache else 0} downloading"),
        "foot": ("YouTube Music, no Premium anywhere in this. Queue and likes live in "
                 f"{config.APP_DIR}. Art is served at the size each slot needs."),
        "log": list(dj.log[-40:]),
        "search": {"pending": bool(ctx.search.get("pending")),
                   "q": str(ctx.search.get("q") or ""),
                   "note": str(ctx.search.get("note") or ""),
                   "rows": [row_view(t) for t in (ctx.search.get("rows") or [])]},
        "page": dict(ctx.page) if ctx.page else None,
        "loved": loved_rows(state=prof),
        "station": str(st.get("station") or ""),
        "repeat": str(st.get("repeat") or "off"),
        "shuffle": bool(st.get("shuffle")),
        "library": lib,
        "busy": _busy(ctx),
        "taste": {"likes": len(prof.get("liked") or []),
                  "skips": len(prof.get("skipped") or []),
                  "artists": [{"name": a, "w": round(float(w), 1)}
                              for a, w in artists[:8] if w > 0],
                  "tags": [{"name": g, "w": round(float(w), 1)}
                           for g, w in tags[:6] if w > 0],
                  "has_backup": taste.has_backup(),
                  "has_profile": bool((prof.get("liked") or [])
                                      or any(w > 0
                                             for w in (prof.get("artists") or {}).values())),
                  "engine": engine_note(dj.info),
                  "last_request": str(prof.get("last_request") or "")},
        "settings": settings_view(),
    }


def with_art(state: dict, ctx) -> dict:
    """
    Stamp the hrefs for artwork already on disk; never fetch here.

    `art` is the list/row slot and `art_card` the grid tile, so one row can show a
    48 px square in the queue and a sharp cover in the card without either slot
    borrowing the other's file. A track with no cached picture keeps `art: ""` and
    the page draws its initial instead - no broken-image flash.
    """
    if state.get("now"):
        vid = state["now"].get("id") or ""
        href = ctx.art_href(vid, "big") or ctx.art_href(vid, "card")
        if href:
            state["now"]["art"] = href
        # the transport bar is a 40px box showing the same track: it gets the small
        # file (or an empty string, and the page uses the hero's) so a click on the
        # seek bar is not decoding a 512px JPEG it is about to throw away
        state["now"]["art_tile"] = ctx.art_href(vid, "row")
    # the sidebar's loved list and its recents are real rows with real ids, so they
    # get the same treatment as the queue; `artists`/`moods` are search chips and
    # have nothing to draw
    lists = [state.get("up_next") or []]
    lib = state.get("library")
    if isinstance(lib, dict):
        lists += [lib.get(key) or [] for key in ("loved", "recents")]
    if isinstance(state.get("page"), dict):
        lists.append(state["page"].get("rows") or [])
    for rows in lists:
        for t in rows:
            if not isinstance(t, dict):
                continue
            for slot, size in (("art", "row"), ("art_card", "card")):
                # the artwork lane's own file is preferred the moment it exists: it is
                # served from this same origin (so it never fights a cross-origin image
                # CDN that some browsers/network paths refuse), and at `card` size it is
                # the album art rather than a video frame. Until a file is on disk the
                # row's own thumbnail (or nothing) shows, so a good picture is never
                # replaced by a *worse* one before the lane has something better.
                href = ctx.art_href(t.get("id") or "", size)
                if href:
                    t[slot] = href
    for t in state["search"]["rows"]:
        href = ctx.art_href(t.get("id") or "")
        if href:
            t["art"] = href
    return state


# ---------------------------------------------------------------- the context
class Context:
    """Everything the HTTP layer mutates, so the handler stays thin and testable."""

    def __init__(self, dj, volume: int = 70) -> None:
        self.dj = dj
        self.volume = int(volume)
        self.search = {"pending": False, "q": "", "rows": [], "note": ""}
        # the in-app album/artist page: filled on a background thread the same way
        # search is, so opening "See album" or an artist never holds the socket.
        self.page = None            # {"kind","title","sub","rows","pending",...} or None
        self._page_seq = 0
        self._search_seq = 0                  # which search is allowed to land
        self.job = None                       # the thread building a mix
        self._job_lock = threading.Lock()     # who is allowed to start the next one
        self.art: queue.Queue = queue.Queue(maxsize=128)   # (track, size) pairs
        self._seen_art: set[tuple[str, str]] = set()       # already drawn / gave up
        self._want_art: set[tuple[str, str]] = set()       # queued, not drawn yet
        self._art_retry: dict[tuple[str, str], int] = {}   # (id, size) -> miss count
        try:
            # a cover that lands late still has to reach the page; this callback runs
            # on the covers thread and only touches the two dicts below
            covers.set_notifier(self._cover_ready)
        except Exception:
            pass
        self._hrefs: dict[str, dict[str, str]] = {}    # id -> {size: artwork href}
        self._meta: dict[str, dict] = {}               # id -> {album, release_year, ...}
        self._meta_inflight: set[str] = set()          # ids being fetched right now
        # metadata lookups run on ONE worker, not a thread per unseen id: a 40-row
        # queue used to spawn 40 subprocesses at once (each a yt-dlp metadata call),
        # which is the kind of burst that makes a laptop fan scream and a server fall
        # over. One paced lane keeps it stable; `_meta_inflight` still dedupes. The
        # queue is bounded so a feed that outruns the lane (say, a 200-row album page
        # that wants Credits for every row) drops old asks instead of growing memory.
        self._meta_queue: queue.Queue = queue.Queue(maxsize=64)
        self._meta_started = False            # the lane starts on first need
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.note = ""                        # last one-shot message for the page
        self._workers: list[threading.Thread] = []

    def start_job(self, target) -> bool:
        """
        Start one background job, or refuse because one is already running.

        The test and the assignment are one step on purpose. Read-then-write across
        two handler threads (this server is threaded, and the page can fire two
        clicks 20 ms apart) let both of them pass a `if _busy(ctx)` check, and the
        result was two search bursts racing to fill one queue: twice the tracks,
        twice the notes, and no way for a listener to tell they double-pressed.
        """
        with self._job_lock:
            if self.job is not None and self.job.is_alive():
                return False
            self.job = threading.Thread(target=target, daemon=True)
            self.job.start()
            return True

    # -- artwork: warmed on a thread, keyed by id so a slow CDN cannot stall state
    def art_href(self, vid: str, size: str = "row") -> str:
        """
        The href for artwork already on disk at `size` ("" = not yet, never a fetch).

        A slot that has nothing at its own size borrows a *larger* file - the browser
        downscales and it looks right - and never a smaller one, because upscaling a
        72 px row thumbnail into a card is the pixelation this replaced.
        """
        if not vid:
            return ""
        with self._lock:
            have = self._slots(str(vid))
            for rung in ART_BORROW.get(size, ("row",)):
                if have.get(rung):
                    return have[rung]
            return ""

    def _slots(self, vid: str) -> dict:
        """The size -> href map for one id, accepting a bare string as the row slot."""
        have = self._hrefs.get(vid)
        if isinstance(have, str):        # anything written before sizes existed
            return {"row": have}
        return have if isinstance(have, dict) else {}

    def meta_for(self, track) -> dict:
        """
        The album / release-year facts for one track, or {} before they land.

        Called from build_state every tick, so it has to be a dict lookup. The first
        time an id is seen a background thread fetches it once (yt-dlp on the video's
        own metadata, ~1 s at most) and caches it; a slow or missing answer is a blank
        line in Credits, never a stalled /api/state. This deliberately never blocks the
        state socket, the same rule the artwork lane follows.
        """
        vid = str((track or {}).get("id") or "") if isinstance(track, dict) else ""
        if not vid:
            return {}
        with self._lock:
            if vid in self._meta:
                return dict(self._meta[vid])
            if vid not in self._meta_inflight:
                self._meta_inflight.add(vid)
                try:
                    self._meta_queue.put_nowait(vid)
                except queue.Full:
                    self._meta_inflight.discard(vid)   # full: retry next tick
                    return {}
                if not self._meta_started:
                    self._meta_started = True
                    threading.Thread(target=self._meta_loop, daemon=True).start()
        return {}

    def _fetch_meta(self, vid) -> None:
        """One best-effort album/release-year lookup, cached under the id."""
        try:
            meta = prov.yt_track_meta(vid)
        except Exception:
            meta = {}
        with self._lock:
            self._meta[vid] = meta
            self._meta_inflight.discard(vid)

    def _meta_loop(self) -> None:
        """One paced lane for album/release-year lookups (never a thread per row)."""
        while not self._stop.is_set():
            try:
                vid = self._meta_queue.get(timeout=0.4)
            except queue.Empty:
                continue
            if not self._stop.is_set():
                self._fetch_meta(vid)

    def request_art(self, tracks, size: str = "row", limit: int = 14) -> int:
        """
        Tell the artwork lane which rows still have no picture at `size`.

        Called from build_state on every tick, so the whole thing has to be dict
        lookups: the seen-set is what keeps a 20-row list from re-asking 20 times a
        second, and `limit` is what keeps a giant search result from filling the lane
        with covers nobody scrolled to. Returns how much work was queued.
        """
        if not tracks or size not in ART_SIZES:
            return 0
        wanted = 0
        with self._lock:
            for t in tracks:
                if wanted >= limit:
                    break
                t = t if isinstance(t, dict) else {}
                vid = str(t.get("id") or "")
                if not vid or (vid, size) in self._seen_art:
                    continue
                if self._slots(vid).get(size) or (vid, size) in self._want_art:
                    continue
                self._want_art.add((vid, size))
                wanted += 1
                try:
                    self.art.put_nowait((t, size))
                except queue.Full:
                    break        # a full warm-up lane delays art, never a click
        return wanted

    @staticmethod
    def _href_for(path) -> str:
        """The URL for one cached file. `..` fails here even though its basename
        looks harmless - the href is handed to an <img>, so it gets the same
        suspicion as the request that reads it back."""
        text = str(path or "")
        if ".." in text:
            return ""
        name = Path(text).name
        return "/art/" + name if ART_NAME.match(name) else ""

    def _store_href(self, vid: str, path: str, size: str = "row") -> bool:
        """
        Record one cached picture for a row, and say whether it stored.

        Returns False when there was nothing to store (no path, not a safe name),
        which is the signal `_art_loop` uses to know a fetch *failed* and should
        be retried rather than remembered as "this row has no art".
        """
        href = self._href_for(path)
        if not vid or not href:
            return False
        with self._lock:
            self._hrefs.setdefault(str(vid), {})[size] = href
        return True

    def _art_loop(self) -> None:
        # Two sources, in the order that makes the window look right: the Cover Art
        # Archive gives a square album cover (the only artwork worth blurring behind
        # the page), and a frame of the video is the fallback that is always there.
        # `covers.attach` never waits here - it queues onto covers' own paced thread
        # and the notifier hands the file back when it lands.
        while not self._stop.is_set():
            try:
                item = self.art.get(timeout=0.4)
            except queue.Empty:
                continue
            t, size = item if isinstance(item, tuple) else (item, "row")
            vid = str((t or {}).get("id") or "") if isinstance(t, dict) else ""
            self._want_art.discard((vid, size))
            if not vid or (vid, size) in self._seen_art:
                continue
            current = vid == str((self.dj.current or {}).get("id") or "")
            # the picture that can be shown *now* is fetched first: a frame off the
            # video CDN answers in ~15 ms, while the archive hop is MusicBrainz (paced
            # at one call per second, per its docs), a redirect, then the image - and
            # doing that first is what made a page of fresh tracks take minutes to
            # dress. The archive still wins when it lands, through the notifier below.
            stored = False
            try:
                stored = self._store_href(vid, thumbs.get(t, size), size)
            except Exception:
                stored = False          # no art is a look, not a failure
            if stored:
                # a real file landed: done with this slot, and clear the miss count
                self._seen_art.add((vid, size))
                self._art_retry.pop((vid, size), None)
            else:
                # no picture *yet* (CDN miss, ffmpeg absent, a row that just showed
                # up). Retry a bounded number of times so a transient miss does not
                # leave the row as a coloured initial forever, then give up so the
                # tick does not keep re-asking for a cover that does not exist.
                tries = self._art_retry.get((vid, size), 0) + 1
                if tries >= 3:
                    self._seen_art.add((vid, size))
                    self._art_retry.pop((vid, size), None)
                else:
                    self._art_retry[(vid, size)] = tries
            try:
                covers.remember_track(t)          # one album may dress its whole set
                # the card slot is where a wrong cover shows most, so the archive is
                # asked for it too; `covers` paces itself and remembers every answer,
                # including "no cover"
                if current or covers.row_mode() or size == "card":
                    covers.attach(t)
            except Exception:
                pass
            if size != "big" and current:
                # only the record being heard earns the 512px file: it is the hero
                # and the blurred backdrop behind the whole page, and fetching it
                # for all 12 rows bought 12 pictures nobody can see at that size
                try:
                    self.request_art([t], "big", limit=1)
                except Exception:
                    pass

    def _cover_ready(self, vid, kind, path) -> None:
        """
        `covers` calls this on its own thread when an album image lands.

        A real cover replaces every slot for that track, not just the one that was
        asked for: the archive file is the best picture there is and it is big enough
        for all three boxes, so stamping one size only would leave a card showing a
        video frame while its own row showed the cover.
        """
        try:
            vid = str(vid or "")
            if not vid:
                return
            for size in ART_SIZES:
                self._store_href(vid, str(path or ""), size)
        except Exception:
            pass

    # -- subscribers (SSE). A slow client loses frames rather than blocking the
    #    broadcast, which is what would make *everyone's* player bar stutter.
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def broadcast(self, payload: str) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def start(self) -> None:
        for target in (self._art_loop, self._tick_loop):
            th = threading.Thread(target=target, daemon=True)
            th.start()
            self._workers.append(th)

    def stop(self) -> None:
        self._stop.set()
        self.art = queue.Queue()          # drop pending work; see the shutdown rule
        try:
            covers.stop()                 # same rule for the lookup thread
        except Exception:
            pass

    def _tick_loop(self) -> None:
        """Push state on a timer; the page owns the progress bar between pushes."""
        while not self._stop.is_set():
            if self._subs:
                try:
                    self.broadcast(json.dumps(with_art(build_state(self), self),
                                               default=str))
                except Exception:
                    pass
            time.sleep(TICK)


# -------------------------------------------------------------------- actions
def _row_ids(ctx) -> dict:
    """id -> the actual queued dict, for the verbs that act on a list row."""
    out: dict[str, dict] = {}
    for t in (list(ctx.dj.queue.items) + list(ctx.search.get("rows") or [])
              + ([ctx.dj.current] if ctx.dj.current else [])):
        if t and t.get("id"):
            out.setdefault(str(t["id"]), t)
    return out


def _busy(ctx) -> bool:
    return bool(ctx.job and ctx.job.is_alive())


def action_request(ctx, fields: dict) -> str:
    """
    Play a mood - or, with no words at all, play what the profile knows.

    An empty box used to be refused with "type a mood first", which is a strange
    answer from an app whose promise is that pressing the heart a few times is
    enough. `DJ.taste_mix()` is the same call the CLI verb makes, so the two skins
    and the terminal cannot drift into three different answers.
    """
    text = (fields.get("q") or [""])[0].strip() or ctx.dj.request or ""
    try:
        count = max(5, min(60, int((fields.get("count") or ["20"])[0])))
    except (TypeError, ValueError):
        count = 20
    def job():
        try:
            if text:
                ctx.dj.start(text, count=count)
            else:
                ctx.dj.taste_mix(count=count)
        except Exception as e:            # a failed mix must not be silence
            ctx.dj._note(f"[warn] the mix could not be built: "
                         f"{e.__class__.__name__}: {e}")

    if not ctx.start_job(job):
        return "still building the previous mix - one second"
    return (f"building a mix from {text!r}" if text
            else "building a mix from what you like")


def action_mix(ctx, fields: dict) -> str:
    """The dedicated "from your likes" verb, so that button needs no words."""
    def job():
        try:
            ctx.dj.taste_mix(count=24)
        except Exception as e:
            ctx.dj._note(f"[warn] the mix could not be built: "
                         f"{e.__class__.__name__}: {e}")

    if not ctx.start_job(job):
        return "still building the previous mix - one second"
    return "mixing from your likes"


def action_radio(ctx, fields: dict) -> str:
    t = _row_ids(ctx).get((fields.get("id") or [""])[0])
    if not t:
        return "that row is gone - click it again from the list"
    label = f"{t.get('artist') or t.get('channel') or ''} - {t.get('title')}".strip(" -")
    def job():
        try:
            ctx.dj.radio_from(t, count=20)     # the engine owns the station rules
        except Exception as e:
            ctx.dj._note(f"[warn] station failed: {e.__class__.__name__}: {e}")

    if not ctx.start_job(job):
        return "still building the previous station - one second"
    return f"building a station around {label}"


def action_play_row(ctx, fields: dict) -> str:
    tid = str((fields.get("id") or [""])[0])
    t = _row_ids(ctx).get(tid)
    if not t:
        return "that row is gone"
    q = ctx.dj.queue
    # "what if i chose a song? the songs next to it build around it" - a plain pick
    # of a song anchors the queue: the upcoming rows are replaced by a tight build
    # around this exact song (radio/similar-to), not left as whatever mix the song
    # happened to sit in. The anchor is set *before* the track is started, so even
    # the refill `next()` triggers reads this song and keeps the queue on its vibe,
    # and it stays set if a build is already running - the set is then built off the
    # socket's thread.
    label = f"{t.get('artist') or t.get('channel') or ''} - {t.get('title')}".strip(" -")
    ctx.dj.station = label
    ctx.dj.station_seed = {"title": t.get("title", ""),
                           "artist": t.get("artist") or t.get("channel", ""),
                           "url": t.get("url", "")}
    # A queued row clicked again used to be copied on top of itself: the original
    # stayed ahead of the cursor, so the same song came up twice in a row. Drop it
    # from wherever it sits first, then put the one copy at the head and play it.
    q.remove_id(tid)
    q.insert_at(q.pos, dict(t))
    ctx.dj.next(force=True)
    def job():
        try:
            ctx.dj.radio_from(dict(t), count=20, replace=True)
        except Exception as e:
            ctx.dj._note(f"[warn] building around '{t.get('title')}' failed: "
                         f"{e.__class__.__name__}: {e}")
    if not ctx.start_job(job):
        # a build is already running; the picked song plays and the anchor is set, so
        # when the queue runs low the refill still builds around it
        return f"playing {t.get('title')} - anchored on {label or 'this'}; the set is building"
    return f"playing {t.get('title')} - building {label or 'similar'} around it"


def action_queue_next(ctx, fields: dict) -> str:
    t = _row_ids(ctx).get((fields.get("id") or [""])[0])
    if not t:
        return "that row is gone"
    q = ctx.dj.queue
    q.insert_at(q.pos + 1, dict(t))
    return f"queued {t.get('title')}"


def action_row_love(ctx, fields: dict) -> str:
    t = _row_ids(ctx).get((fields.get("id") or [""])[0])
    if not t:
        return "that row is gone"
    taste.record_like(t)
    ctx.dj.state = config.load_state()      # same reload DJ.like() does
    return "loved - it will pull the mix that way"


def action_row_remove(ctx, fields: dict) -> str:
    """
    Remove one row from the queue, leaving the song that is playing alone.

    The row a person clicks is in the queue by id; `remove_id` only drops things
    at or after the cursor, so it can never delete the audible track or rewrite
    history. The answer says what left the list, which is the one fact a Remove
    button has to be honest about - a silent success looks like a dead button.
    """
    t = _row_ids(ctx).get((fields.get("id") or [""])[0])
    if not t:
        return "that row is gone"
    removed = ctx.dj.queue.remove_id(str(t.get("id") or ""))
    if not removed:
        return "that row is no longer queued - the song here already left the list"
    ctx.dj._note(f"removed from queue: {removed.get('title') or '?'}")
    return f"removed '{removed.get('title') or '?'}' from the queue"


def action_row_dislike(ctx, fields: dict) -> str:
    """
    The explicit "never again" on one row: teaches the taste model and removes it.

    This is the complement of the heart. A dislike is a *verdict*, not just a
    skip: it drops the row from the queue and the artist/title weight in the
    profile, so the next mix leans away from that sound instead of re-proposing
    it. The row is removed from the queue either way - a track you pressed 👎 on
    should not sit in the list waiting to play.
    """
    t = _row_ids(ctx).get((fields.get("id") or [""])[0])
    if not t:
        return "that row is gone"
    taste.record_dislike(t)
    ctx.dj.state = config.load_state()      # same reload DJ.like() does
    ctx.dj.queue.remove_id(str(t.get("id") or ""))
    ctx.dj._note(f"disliked: {t.get('title') or '?'}")
    return f"won't suggest '{t.get('title') or '?'}' again"


def action_like(ctx, fields: dict) -> str:
    if not ctx.dj.current:
        return "nothing playing to love yet"
    if ctx.dj.is_liked(ctx.dj.current):
        ctx.dj.unlike()
        return "unloved"
    ctx.dj.like()
    return "loved"


def action_seek(ctx, fields: dict) -> str:
    secs = float((fields.get("secs") or ["0"])[0] or 0)
    return "" if ctx.dj.seek(secs) else "the player would not seek"


def action_volume(ctx, fields: dict) -> str:
    """
    Set the level. The slider sends a number and a hand-built request sends
    anything, so a value that is not a level gets an answer instead of a 500 -
    `int("NaN")` raising is a server error for a client mistake.
    """
    raw = str((fields.get("pct") or [""])[0]).strip()
    if not raw:
        return f"volume {ctx.volume}%"            # nothing to set: say what it is
    try:
        want = int(float(raw))
    except ValueError:
        return f"{raw[:12]!r} is not a level - volume stays {ctx.volume}%"
    pct = max(0, min(100, want))
    ctx.volume = pct
    ctx.dj.volume(pct)
    return f"volume {pct}%" + ("" if pct == want else f" (clamped from {want})")


_ON = {"1", "on", "true", "yes", "keep"}
_OFF = {"0", "off", "false", "no", "stop"}


def action_auto(ctx, fields: dict) -> str:
    """
    `auto` with no value flips it - that is what the page's checkbox and the CLI
    verb do - and an explicit on/off sets it. Any other word used to count as "on",
    which is how a typo became a settings change nobody asked for.
    """
    raw = str((fields.get("on") or [""])[0]).strip().lower()
    cur = bool(ctx.dj.auto)
    if raw in _ON:
        want = True
    elif raw in _OFF:
        want = False
    elif raw in ("", "toggle", "flip"):
        want = not cur
    else:
        return (f"{raw[:12]!r} is not on or off - keep mixing is already "
                f"{'on' if cur else 'off'}")
    ctx.dj.set_auto(want)
    return "keep mixing: on" if want else "keep mixing: off"


def action_autoplay(ctx, fields: dict) -> str:
    """
    Whether opening the app should start a mix and play, or wait for a press.

    The complaint was "when the first start the queue start mixing and playing even
    i dont start the button yet". One tap stops that; the setting is persisted in
    the same state file as volume/repeat, so it sticks across reloads.
    """
    raw = str((fields.get("on") or [""])[0]).strip().lower()
    cur = bool((ctx.dj.state or {}).get("autoplay"))
    if raw in _ON:
        want = True
    elif raw in _OFF:
        want = False
    elif raw in ("", "toggle", "flip"):
        want = not cur
    else:
        return (f"{raw[:12]!r} is not on or off - autoplay is already "
                f"{'on' if cur else 'off'}")
    ctx.dj.state["autoplay"] = want
    config.save_state(ctx.dj.state)
    return ("autoplay on - it starts a mix on open" if want
            else "autoplay off - press Play or type a mood first")


def action_voice(ctx, fields: dict) -> str:
    """Turn the spoken DJ on/off. Persisted with autoplay/volume/repeat."""
    raw = str((fields.get("on") or [""])[0]).strip().lower()
    cur = bool((ctx.dj.state or {}).get("voice", True))
    if raw in _ON:
        want = True
    elif raw in _OFF:
        want = False
    elif raw in ("", "toggle", "flip"):
        want = not cur
    else:
        return (f"{raw[:12]!r} is not on or off - the DJ voice is already "
                f"{'on' if cur else 'off'}")
    ctx.dj.state["voice"] = want
    config.save_state(ctx.dj.state)
    return ("the DJ will talk about the songs now" if want
            else "the DJ voice is off - it only shows the line")


def action_open(ctx, fields: dict) -> str:
    """
    Hand the current track to a real client: Spotube or the browser.

    `open_externally` is the same handoff `--backend spotube` uses (flatpak-aware,
    so it reaches the sandboxed Spotube too). It is deliberately NOT
    `playerctl play`: this button says "open this song", and pressing it should
    not resume a player the user paused on purpose.
    """
    t = ctx.dj.current or {}
    url = (fields.get("url") or [""])[0] or t.get("url") or \
        (f"https://music.youtube.com/watch?v={t.get('id')}" if t.get("id") else "")
    if not url:
        return "nothing playing to open"
    return "" if player_mod.open_externally(url) else \
        "no browser or Spotube answered on this machine"


def no_move_note(dj) -> str:
    """
    Why a transport click did not change the track - in words.

    The Tk skin learned this the hard way: "nothing left" while six tracks are
    queued and still audible is simply false, and it sends the user off to look
    for a bug that isn't there. Same three-way split here.
    """
    try:
        left = len(dj.queue)
    except Exception:
        left = 0
    if left <= 0:
        return "nothing queued after this - type a mood or press Refill"
    if getattr(dj, "idle", ""):
        return "the next few streams refused to start - it keeps retrying by itself"
    return "nothing could start just now - the activity log has the reason"


def action_next(ctx, fields) -> str:
    # dj.skip() is the engine's *human* next: it forces past the retry cooldown
    # and leaves the taste judgement to the one place that knows how much was heard
    return "" if ctx.dj.skip() else no_move_note(ctx.dj)


def action_prev(ctx, fields) -> str:
    return "" if ctx.dj.prev() else no_move_note(ctx.dj)


def _do(call, note: str = "") -> str:
    """Run an engine call and answer with the note. The engine's own return value
    (a track dict, mostly) is not something a browser should be sent."""
    call()
    return note


def action_clear_queue(ctx, fields) -> str:
    """
    Empty what is lined up; the song that is playing keeps playing.

    The number comes back in the note because the number *is* the answer: "0 dropped"
    means the queue was already empty (and keep mixing will still fill it), while
    "23 dropped" is the button doing what was asked. A silent clear reads as a broken
    clear, which is how a working button gets reported as a missing feature.
    """
    note = ctx.dj.clear_queue()
    if ctx.dj.auto:
        # "shortly" has to mean something, and it cannot mean "inside this request":
        # a refill is searches, so it goes on the job lane like every other thing the
        # page asks for, and the busy pill is what the listener sees while it runs.
        def refill() -> None:
            try:
                ctx.dj._topup(force=True)
            except Exception as e:
                ctx.dj._note(f"[error] the refill after clearing did not finish: "
                             f"{e.__class__.__name__}: {e}")
        if not ctx.start_job(refill):
            note += " (a search is already running - that one will fill it)"
    return note


def action_clear_taste(ctx, fields: dict) -> str:
    """
    Forget the learned profile - the one destructive thing in this app.

    So the route requires sure=1 rather than trusting the button: the page arms
    itself on the first tap and sends it on the second, and anything reaching this
    verb by hand (a script, a mistyped curl, a pasted link) gets an explanation
    instead of a wiped profile. The wipe itself leaves a snapshot behind, and
    `restore_taste` puts it back.
    """
    if str((fields.get("sure") or [""])[0]).strip() not in ("1", "yes", "true"):
        return ("this wipes every like and judgement the app learned - "
                "send sure=1 if that is really what you want")
    gone = ctx.dj.forget_taste()            # the engine counts and logs it once
    n = sum(int(gone.get(k) or 0) for k in ("liked", "skipped"))
    return (f"forgot {n} judgements - the mix starts from scratch, "
            f"and one tap brings them back" if n
            else "there was nothing to forget yet")


def action_restore_taste(ctx, fields: dict) -> str:
    """
    Bring back a cleared profile.

    One snapshot is kept, which is enough to undo a mistake without pretending to
    be a history: the file is overwritten by the next clear, so a restore is always
    "the last thing you threw away" and never a version picker.
    """
    back = ctx.dj.restore_taste()
    if not back:
        return "there is nothing saved to bring back"
    return (f"brought back {back.get('liked', 0)} loved and "
            f"{back.get('artists', 0)} artists")


def action_shuffle(ctx, fields: dict) -> str:
    return ("shuffle on - the queue was mixed" if ctx.dj.toggle_shuffle()
            else "shuffle off - the DJ's order is back")


def action_repeat(ctx, fields: dict) -> str:
    got = ctx.dj.set_repeat(str((fields.get("mode") or ["cycle"])[0]))
    return {"off": "repeat off", "all": "repeat all - the set loops",
            "one": "repeat one - this track loops"}[got]


def action_unfollow(ctx, fields: dict) -> str:
    """Drop one artist from the profile; the loved songs stay (see taste.forget_artist)."""
    import taste as taste_mod
    name = str((fields.get("name") or [""])[0]).strip()
    if not name:
        return "which artist? send name=<artist>"
    n = taste_mod.forget_artist(name)
    ctx.dj._note(f"forgot {name} as a leaning" if n else f"nothing was leaning on {name}")
    return (f"{name} will not pull the mix any more"
            if n else f"the profile has no weight for {name}")


def _clear_station(ctx) -> str:
    ctx.dj.station = ""
    ctx.dj.station_seed = None
    return "station label cleared"


def _clean_name(s: str) -> str:
    return " ".join(str(s or "").split()).strip()


def action_open_album(ctx, fields: dict) -> str:
    """
    Open the album/record behind the playing track as an in-app page.

    The album name comes from the track's own metadata (already fetched for Credits);
    this searches "<album> <artist>" on the trusted Music endpoint and presents the
    rows as a page. If no album is known yet, fall back to a plain "song" search so
    the button always does something rather than silently doing nothing.
    """
    album = _clean_name((fields.get("album") or [""])[0])
    artist = _clean_name((fields.get("artist") or [""])[0])
    if not album:
        # no album metadata yet: treat it as a page for the current track's artist
        if artist:
            start_page(ctx, "artist", f"{artist} songs", artist, "discography")
            return f"showing {artist} - albums and songs"
        return "no album or artist to show for the current track"
    title = album
    sub = artist or "album"
    start_page(ctx, "album", f"{album} {artist}".strip(), title, sub)
    return f"showing album '{album}'" + (f" by {artist}" if artist else "")


def action_open_artist(ctx, fields: dict) -> str:
    """
    Open an artist as an in-app page: "Songs by <artist>" on the Music endpoint.

    This is the clickable artist name in Now Playing - what the user asked for when
    they said "the artist page you didnt make it". It uses the same reliable search
    that the Search tab uses, so it works without a separate browse endpoint.
    """
    artist = _clean_name((fields.get("artist") or [""])[0])
    if not artist:
        return "no artist to show"
    # query carries "<artist> songs" for the search fallback; the bare name goes in
    # `title` so page_rows can drive the browse discography by the artist alone
    start_page(ctx, "artist", f"{artist} songs", artist, "discography")
    return f"showing {artist} - albums and songs"


def action_test_brain(ctx, fields: dict) -> str:
    """
    Ask the configured brain one question, on a thread.

    The button exists because "engine: offline" is not a diagnosis - a wrong model
    name, a bad key and a blocked network all look the same from the outside, and
    they are three different fixes. Same probe `--doctor` runs, same answer.
    """
    def job():
        import brain
        try:
            config.apply_llm_overrides()
            r = brain.probe()
            ctx.dj._note(f"brain test: {r['engine']} ok={r['ok']} {r['ms']}ms - "
                         f"{r['detail'][:140]}")
        except Exception as e:
            ctx.dj._note(f"[warn] brain test failed: {e.__class__.__name__}: {e}")

    if not ctx.start_job(job):
        return "one thing at a time - still working"
    return "testing the AI planner - watch the activity log"


ACTIONS = {
    # next/prev are typed by a person, and DJ only lets a *forced* move past the
    # 45s retry hold - same rule the CLI verbs follow
    "next": action_next,
    "prev": action_prev,
    "skip": lambda c, f: _do(c.dj.skip),
    "pause": lambda c, f: _do(c.dj.pause),
    "resume": lambda c, f: _do(c.dj.resume),
    "playpause": lambda c, f: _do(c.dj.resume if c.dj.paused else c.dj.pause),
    "stop": lambda c, f: _do(c.dj.stop, "stopped"),
    "topup": lambda c, f: _do(lambda: c.dj._topup(force=True), "queue refilled"),
    "like": action_like,
    "unlike": lambda c, f: _do(c.dj.unlike, "unloved"),
    "auto": action_auto,
    "autoplay": action_autoplay,
    "voice": action_voice,
    "seek": action_seek,
    "volume": action_volume,
    "request": action_request,
    "mix": action_mix,
    "radio": action_radio,
    "play_row": action_play_row,
    "queue_next": action_queue_next,
    "love_row": action_row_love,
    "remove_queue": action_row_remove,
    "dislike": action_row_dislike,
    "open": action_open,
    "open_album": action_open_album,
    "open_artist": action_open_artist,
    "test_brain": action_test_brain,
    "clear_station": lambda c, f: _clear_station(c),
    "shuffle": action_shuffle,
    "repeat": action_repeat,
    "unfollow": action_unfollow,
    "clear_queue": action_clear_queue,
    "clear_taste": action_clear_taste,
    "restore_taste": action_restore_taste,
}

def save_settings(fields: dict) -> tuple[int, dict]:
    """
    POST /api/settings. Blank means different things for different fields.

    A password input is emptied by the browser on every reload, so "blank = clear"
    would delete a working key the first time someone pressed Save to change the
    model. The key is therefore kept unless the form says `clear_key=1`; the base
    URL and the model are ordinary text inputs, where blank really does mean "back
    to the default", and those follow the Tk dialog's rule.

    The key goes to `~/.spotube-dj/config.json` at 0600 through the one function
    that knows how (`config.save_llm_config`), and the response never echoes it
    back - only the mask, so a shoulder or a page history cannot read it.
    """
    def one(name):
        return str((fields.get(name) or [""])[0]).strip()

    vals = {}
    if "base" in fields:
        vals["LLM_BASE_URL"] = one("base")
    if "model" in fields:
        vals["LLM_MODEL"] = one("model")
    if "voice" in fields:
        voice = one("voice")
        names = [n.lower() for n in config.GEMINI_TTS_VOICE_NAMES]
        if voice.lower() not in names:
            return 400, {"error": (f"unknown TTS voice {voice!r}; pick one of the "
                                   "voices in the dropdown")}
        vals["DJ_VOICE"] = config.GEMINI_TTS_VOICE_NAMES[names.index(voice.lower())]
    if one("clear_key") == "1":
        vals["LLM_API_KEY"] = ""
    elif one("key"):
        vals["LLM_API_KEY"] = one("key")
    key_touched = ("key" in fields) or ("clear_key" in fields)
    if not vals and not key_touched:
        return 400, {"error": "nothing to save - send key, base, model or clear_key"}
    try:
        config.save_llm_config(**vals)
    except OSError as e:
        # the message is a filesystem complaint, not a secret; replacing it with a
        # class name leaves "could not write the config" and no idea which file stuck
        return 500, {"error": f"could not write the config: {e.__class__.__name__}: {e}"}
    return 200, {"settings": settings_view(),
                 "note": ("saved" if (("LLM_API_KEY" in vals) or not key_touched)
                          else "saved (key kept as it was)")}


def run_action(ctx, name: str, fields: dict) -> tuple[int, dict]:
    """-> (status, {"note":…, "state":…}). One place that decides 400 vs 500."""
    fn = ACTIONS.get(name)
    if fn is None:
        return 400, {"error": f"unknown action {name!r}",
                     "actions": sorted(ACTIONS)}
    try:
        out = fn(ctx, fields)
    except Exception as e:                    # one bad action must not kill the tab
        return 500, {"error": f"{e.__class__.__name__}: {e}"}
    # an action's return value IS its note; the tuple form is what the one-liners
    # below produce, and dropping their note here is what made the page look dead
    if isinstance(out, tuple):
        note = str(out[1]) if len(out) > 1 else ""
    else:
        note = str(out or "")
    ctx.note = note or ctx.note
    return 200, {"note": note}


def do_search(ctx, text: str) -> tuple[int, dict]:
    """
    One implementation of `q=` for both verbs, so that GET and POST cannot
    disagree about what an empty search means.
    """
    text = str(text or "").strip()
    if not text:
        return HTTPStatus.BAD_REQUEST, {"error": "q= required",
                                        "example": "/api/search?q=portishead"}
    start_search(ctx, text)
    return HTTPStatus.OK, {"search": {"pending": True, "q": text, "rows": [], "note": ""},
                           "note": "searching - the results land in the Search tab"}


def start_page(ctx, kind: str, query: str, title: str, sub: str = "") -> None:
    """
    Build the in-app Artist / Album page on a thread.

    A page is a hand onto the *same* trusted search endpoint the Search tab uses, so
    it works offline-friendly and never depends on fragile InnerTube browse parsing:
    "Songs by <artist>" for an artist, "<album> <artist>" for an album. The result
    goes in `ctx.page`, which /api/state exposes and the `page` view renders. Only
    the latest open wins (a second click during a lookup discards the first), exactly
    like search.
    """
    with ctx._lock:
        ctx._page_seq += 1
        mine = ctx._page_seq
    ctx.page = {"kind": kind, "title": title, "sub": sub, "rows": [],
                "pending": True, "note": ""}
    ctx.broadcast(json.dumps({"page": dict(ctx.page)}))

    def job():
        try:
            # a page is a hand into the browse endpoint when that answers (an album
            # tracklist, an artist's discography) and the same trusted search when it
            # doesn't - so "deep" never costs a dead page when browse has nothing
            rows = prov.page_rows(kind, query, title, sub)
        except Exception as e:
            rows, note = [], (f"lookup failed: {e.__class__.__name__}"
                              + (f": {e}" if str(e) else ""))
        else:
            note = ("" if rows else
                    "no tracks came back for that - try a better-known artist or album")
        for t in rows or []:
            try:
                ctx.art.put_nowait(t)
            except queue.Full:
                pass
        if mine != ctx._page_seq:
            return                     # a newer page was opened; drop this one
        ctx.page = {"kind": kind, "title": title, "sub": sub,
                    "rows": [row_view(t) for t in (rows or [])],
                    "pending": False, "note": note}
        ctx.broadcast(json.dumps({"page": dict(ctx.page)}))

    threading.Thread(target=job, daemon=True).start()


def start_search(ctx, text: str) -> None:
    """The page's search, on a thread: a 0.5-40 s lookup must not hold a socket."""
    with ctx._lock:              # two searches started in the same tick still order
        ctx._search_seq += 1     # themselves: the later bump is the one that lands
        mine = ctx._search_seq
    ctx.search = {"pending": True, "q": text, "rows": [], "note": ""}
    ctx.broadcast(json.dumps({"search": dict(ctx.search, rows=[])}))

    def job():
        try:
            rows = prov.yt_search(text, limit=20)
        except Exception as e:
            rows, note = [], (f"search failed: {e.__class__.__name__}"
                             + (f": {e}" if str(e) else ""))
        else:
            note = ("" if rows else
                    "nothing survived the filter as a 3-8 minute track - see the "
                    "activity log for what was refused")
        view = [row_view(t) for t in (rows or [])]
        for t in rows or []:
            try:
                ctx.art.put_nowait(t)
            except queue.Full:
                pass
        if mine != ctx._search_seq:
            # a second search was started while this one was in the network: the
            # tab already shows the newer words, and landing these older results on
            # top of it would look like the box had eaten the typing
            return
        ctx.search = {"pending": False, "q": text, "rows": rows or [], "note": note}
        ctx.broadcast(json.dumps({"search": dict(ctx.search, rows=view)}))

    threading.Thread(target=job, daemon=True).start()


# ---------------------------------------------------------------------- server
def safe_art_path(root: Path, name: str) -> Path | None:
    """
    Resolve one cached artwork file, or None.

    Two checks because `name` comes from the browser: the shape has to be a bare
    cache filename (no slashes, no dots but the extension) *and* the resolved
    path has to still be inside the thumbs directory. That is the whole
    traversal story, and it means `/art/../../etc/passwd` is a 404, not a file.
    """
    if not ART_NAME.match(name or ""):
        return None
    try:
        target = (root / name).resolve()
        base = root.resolve()
    except OSError:
        return None
    if base not in target.parents and target.parent != base:
        return None
    return target if target.is_file() else None


ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def split_host(header: str) -> str:
    """`[::1]:8766` and `localhost:8766` both reduce to their host part."""
    host = (header or "").strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_ok(header: str | None, allow_any_host: bool = False) -> bool:
    """
    Refuse any Host that isn't loopback - the DNS-rebinding door.

    It looks paranoid until you remember what this port can do: enqueue tracks and
    move the speaker volume. A page on `evil.test` cannot read localhost without a
    rebinding trick, and this check is what breaks it - the browser would send
    `Host: evil.test`, and a rebinding attack cannot add to this set.

    `allow_any_host` is LAN mode (`--web-host 0.0.0.0`, or the sandbox preview): the
    user asked for the socket to be reachable by other machines, so the address
    itself can no longer be the trust boundary. The warning at startup says so.
    """
    if allow_any_host:
        return True
    if not header:
        return False
    extra = {h.strip().lower() for h in
             os.environ.get("SPOTUBE_DJ_WEB_HOSTS", "").split(",") if h.strip()}
    return split_host(header).lower() in (ALLOWED_HOSTS | extra)


HTML_HEADERS = {
    # `img-src` opens the cover CDNs as well as 'self': a row's own thumbnail only
    # ever rendered after the artwork lane copied it to /art/ before, because the
    # strict 'self' allowance silently blocked every i.ytimg.com / googleusercontent
    # cover (the img fired onerror and was removed, leaving the tinted tile). Now the
    # row's picture can load straight away and the lane still dresses the rest.
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                               "script-src 'unsafe-inline'; "
                               "img-src 'self' data: https://i.ytimg.com "
                               "https://lh3.googleusercontent.com https://yt3.ggpht.com; "
                               "connect-src 'self'; font-src 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


ROUTES = ("/", "/api/state", "/api/stream", "/api/action", "/api/search",
          "/api/settings", "/art/<file>")


def not_found_payload() -> dict:
    """
    One map for every 404, routes and verbs both.

    Somebody at a terminal typing `/api/stat` or `POST /api/next` is one glance from
    the right shape, and a bare "not found" makes them guess instead of read.
    """
    return {"error": "not found", "routes": list(ROUTES), "actions": sorted(ACTIONS)}


class Handler(BaseHTTPRequestHandler):
    # and not a word about the interpreter: BaseHTTPRequestHandler appends
    # "Python/3.13" to whatever this says, which is free information for anybody
    # scanning the port and useless to the page
    server_version = "spotube-dj/1.0"
    sys_version = ""

    def version_string(self):
        # the default joins the two with a space, and an empty sys_version leaves a
        # trailing space in every response header - one line, and it is exact
        return self.server_version
    ctx: Context = None                      # set by make_handler
    allow_any_host: bool = False             # LAN mode; see host_ok

    # -- plumbing -------------------------------------------------------------
    def log_message(self, *a):
        pass                                 # the DJ's own log is the interesting one

    def _guard(self) -> bool:
        if not host_ok(self.headers.get("Host"), self.allow_any_host):
            self._send(HTTPStatus.FORBIDDEN, b"this app serves loopback only",
                       "text/plain; charset=utf-8")
            return False
        return True

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in HTML_HEADERS.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body and not getattr(self, "_head", False):
                self.wfile.write(body)      # HEAD gets the headers and the length
        except (BrokenPipeError, ConnectionResetError):
            pass                             # a closed tab is not an exception worth logs

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _fields(self) -> dict:
        # a second call must not read the socket again: the body is already in
        # memory, and reading again blocks until the request times out - which is
        # how one settings branch came to wedge a worker thread for 240 seconds
        cached = getattr(self, "_fields_once", None)
        if cached is not None:
            return cached
        raw = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length:
            raw = self.rfile.read(min(length, 64 * 1024))
        # keep_blank_values: an emptied text field is a *value* here - "clear the
        # base URL" and "back to the default model" are posted as blank strings,
        # and dropping them made those two fields impossible to reset from a page
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query,
                                      keep_blank_values=True)
        body = urllib.parse.parse_qs(raw.decode("utf-8", "replace"),
                                     keep_blank_values=True)
        out = dict(query)
        out.update(body)
        self._fields_once = out
        return out

    # -- routes ---------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, webapp.page().encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(HTTPStatus.OK, with_art(build_state(self.ctx), self.ctx))
        elif path == "/api/stream":
            self._stream()
        elif path == "/favicon.svg":
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                   f'<rect width="24" height="24" rx="6" fill="{vm.ACCENT}"/>'
                   '<path d="M7 6.5v11L19 12z" fill="#000"/></svg>')
            self._send(HTTPStatus.OK, svg.encode(), "image/svg+xml")
        elif path.startswith("/art/"):
            self._art(path[len("/art/"):])
        elif path == "/api/search":
            # a search is a read - and being able to type /api/search?q=portishead
            # into a tab, or point curl at it, is how you debug this thing at 1am
            code, payload = do_search(self.ctx,
                                      (urllib.parse.parse_qs(parsed.query)
                                       .get("q") or [""])[0])
            self._json(code, payload)
        else:
            self._json(HTTPStatus.NOT_FOUND, not_found_payload())

    def do_HEAD(self):  # noqa: N802
        """
        The routes a `curl -I` or a link previewer asks for, headers only.

        A 501 in front of a working app reads as a broken server, so this is the
        same dispatch as GET with the body dropped - except /api/stream, which is a
        held-open response and has no meaning as a HEAD.
        """
        if not self._guard():
            return
        if urllib.parse.urlparse(self.path).path == "/api/stream":
            self._json(HTTPStatus.METHOD_NOT_ALLOWED,
                       {"error": "the event stream is GET-only", "allow": "GET"})
            return
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def do_PUT(self):  # noqa: N802
        self._not_allowed()

    def do_DELETE(self):  # noqa: N802
        self._not_allowed()

    def do_PATCH(self):  # noqa: N802
        self._not_allowed()

    def do_OPTIONS(self):  # noqa: N802
        self._not_allowed()

    def _not_allowed(self) -> None:
        # 501 out of BaseHTTPRequestHandler is an HTML page with none of the
        # security headers on it; this app answers JSON with headers, all the way
        # down to "no", and says what it does take
        self._send(HTTPStatus.METHOD_NOT_ALLOWED,
                   json.dumps({"error": "this route takes GET or POST",
                               "allow": "GET, HEAD, POST",
                               "note": "no cross-origin writes: this is a same-"
                                       "origin localhost app"}).encode(),
                   "application/json", {"Allow": "GET, HEAD, POST"})

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        fields = self._fields()
        if path == "/api/action":
            code, payload = run_action(self.ctx, (fields.get("action") or [""])[0],
                                       fields)
            if code == 200:
                payload["state"] = with_art(build_state(self.ctx), self.ctx)
            self._json(code, payload)
        elif path == "/api/search":
            code, payload = do_search(self.ctx, (fields.get("q") or [""])[0])
            self._json(code, payload)
        elif path == "/api/settings":
            code, payload = save_settings(fields)      # fields, not _fields(): see below
            self._json(code, payload)
        else:
            self._json(HTTPStatus.NOT_FOUND, not_found_payload())

    def _art(self, name: str) -> None:
        target = safe_art_path(Path(thumbs.cache_dir()), urllib.parse.unquote(name))
        if not target:
            self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            return
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif"}.get(target.suffix.lower(), "application/octet-stream")
        try:
            body = target.read_bytes()
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            return
        self._send(HTTPStatus.OK, body, ctype,
                   {"Cache-Control": "public, max-age=86400"})

    def _stream(self) -> None:
        """Server-sent events: one push per tick, so there is no request storm."""
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        q = self.ctx.subscribe()
        try:
            first = json.dumps(with_art(build_state(self.ctx), self.ctx), default=str)
            self.wfile.write(f"data: {first}\n\n".encode())
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    payload = ": keep-alive\n\n"
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass                            # the tab was closed; that is normal
        finally:
            self.ctx.unsubscribe(q)


def make_handler(ctx: Context, allow_any_host: bool = False) -> type:
    """
    A handler class with the context bolted on.

    `http.server` instantiates the class per request, so the DJ has to live on the
    class - this is the one-line way to do that without a global, and it keeps the
    tests able to build a handler around a fake DJ.
    """
    return type("BoundHandler", (Handler,), {"ctx": ctx,
                                             "allow_any_host": bool(allow_any_host)})


def make_server(ctx: Context, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                quiet: bool | None = None):
    """
    The bound socket, separate from `serve()` so a test can use port 0.

    Anything but loopback is a deliberate choice by whoever passed `--web-host`,
    and it flips the Host guard open (there is no address left to trust), so say
    which interface actually got bound rather than the one that was asked for.
    """
    httpd = ThreadingHTTPServer((host, port), make_handler(ctx, is_open(host)))
    httpd.daemon_threads = True            # a stuck SSE stream must not hold the exit
    if quiet is None:
        quiet = (os.environ.get("SPOTUBE_DJ_WEB_DEBUG") or "").strip() in ("", "0",
                                                                           "off", "false")
    httpd.quiet = bool(quiet)
    if httpd.quiet:
        # `HTTPServer.handle_error` prints the full stack of anything a handler lets
        # escape: right while you are reading the code, noise from a browser (a tab
        # that stops reading mid-write is a broken-pipe traceback on every reload).
        # Swapped on the instance instead of by subclassing, so that patching
        # ThreadingHTTPServer - which is how the bind address gets tested - still
        # lands on this constructor.
        httpd.handle_error = lambda request, client_address: None
    httpd.bound_host = host
    # what to *print*: "0.0.0.0" is not a link anybody can click. The bind itself
    # is left alone - rewriting it to one address would quietly un-bind loopback,
    # and then localhost on the machine running the DJ stops working.
    httpd.display_host = (_lan_ip() or host) if host in ("0.0.0.0", "::", "") else host
    return httpd


def is_open(host: str) -> bool:
    """True when the socket is reachable from outside this machine."""
    return host not in ALLOWED_HOSTS


def _lan_ip() -> str:
    """Best-effort local address, to print instead of 0.0.0.0 in the URL line."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return str(s.getsockname()[0])
    except OSError:
        return ""
    finally:
        s.close()


def serve(dj, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          request: str = "", count: int = 20,
          playlist: str = "", headless: bool = False, open_browser: bool = True,
          control_port: int = 0, search_for: str = "") -> int:
    """
    Run the browser UI for an existing DJ. Blocks until Ctrl-C.

    `control_port` also starts `DJ`'s own JSON API, so `spotube-dj next` from a
    terminal drives a session you are watching in the browser.
    """
    ctx = Context(dj, volume=int((dj.state or {}).get("volume", 70) or 70))
    ctx.start()
    if control_port:
        try:
            dj.serve(control_port)
        except OSError as e:
            dj._note(f"[warn] control API not started: {e}")

    httpd = make_server(ctx, host, port)
    if should_run_loop(dj):
        threading.Thread(target=dj.run, daemon=True, name="dj-advance").start()
    # the socket comes up FIRST: a browser opened on the URL while the planner is
    # still searching would find nothing listening, and "it never loads" is a worse
    # report than an empty queue with a note in the activity log
    if search_for:
        # `--search` (and the desktop "Search for a song" action) means: open the
        # page already showing results, not open it and wait for a click
        start_search(ctx, search_for)
    if request or playlist:
        # an explicit --request / --playlist IS the user pressing a button; play it
        ctx.job = threading.Thread(
            target=lambda: _first_mix(dj, request, playlist, count), daemon=True)
        ctx.job.start()
    elif bool((dj.state or {}).get("autoplay")) and _has_profile():
        # autoplay is off by default: opening the app must not start a mix and play
        # before the listener asks. With it on, the profile is a fine request.
        ctx.job = threading.Thread(target=lambda: _auto_open(dj), daemon=True)
        ctx.job.start()
    bound = getattr(httpd, "display_host", host)
    open_net = is_open(getattr(httpd, "bound_host", host))
    url = f"http://{bound}:{port}/"
    print(f"web player: {url}   (Ctrl-C stops it; the queue lives in {config.APP_DIR})")
    if open_net:
        print(f"          LAN mode: anything on {bound}:{port} can drive this DJ - "
              "the Host check is off, so keep it off public wifi")
    else:
        print("          nothing on this port is reachable from another machine")
    if open_browser:
        threading.Timer(0.4, lambda: _open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        ctx.stop()
        ctx.dj.stop()
        httpd.server_close()
    return 0


def should_run_loop(dj) -> bool:
    """
    Whether `--web` should own the auto-advance loop.

    Without it the browser skin plays one track and stops: `DJ.run()` is what
    notices an ended track, tops the queue up and mixes the next one. It is only
    started when the DJ actually has a player, because with `--backend none` there
    is nothing to watch - the loop would keep "starting" tracks nobody can hear and
    the page would show a Now Playing that never moves.
    """
    return getattr(dj, "player", None) is not None


def _has_profile() -> bool:
    st = taste.load_state()
    return bool((st.get("liked") or []) or any(
        w > 0 for w in (st.get("artists") or {}).values()))


def _auto_open(dj) -> None:
    try:
        dj.taste_mix()
    except Exception as e:
        dj._note(f"[warn] the opening mix failed: {e.__class__.__name__}: {e}")


def _first_mix(dj, request: str, playlist: str, count: int) -> None:
    """The opening mix, off the socket's thread. Its notes land in dj.log."""
    try:
        dj.start(request, seed_refs=[playlist] if playlist else None, count=count)
    except Exception as e:
        dj._note(f"[warn] the first mix could not be built: {e.__class__.__name__}: {e}")


def _open(url: str) -> None:
    try:
        if not os.environ.get("SPOTUBE_DJ_NO_BROWSER"):
            webbrowser.open(url)
    except Exception:
        pass                                 # no browser is fine; the URL was printed


def doctor_line() -> tuple[str, bool, str]:
    """For --doctor: the web skin needs nothing beyond the standard library."""
    try:
        import http.server                          # noqa: F401
        ok = bool(webapp.page())
    except Exception as e:
        return ("web player", False, f"page could not be built: {e.__class__.__name__}")
    return ("web player", ok, f"http://127.0.0.1:{DEFAULT_PORT} - one HTML file, "
            f"stdlib only, no CDN")


if __name__ == "__main__":            # python3 -m web  (dev: a demo DJ on a port)
    from dj import DJ
    d = DJ(backend="none", headless=True)
    raise SystemExit(serve(d, request="lofi beats", open_browser=False))
