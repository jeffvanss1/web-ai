"""
A local web front end for the same DJ: a browser tab instead of a Tk window.

Why this exists next to the Tk skin: the window can only be as good as Tk lets
it be - no sub-pixel text, no rounded anything, icons that had to be characters.
The engine is already separated from the display (everything the UI shows is
either `DJ.status()` or a rule in `viewmodel.py`), so a second skin is a reader
of that, not a second implementation. This file talks to `DJ` exactly the way
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
    out = {
        "id": str(t.get("id") or ""),
        "title": str(t.get("title") or "?"),
        "artist": str(t.get("artist") or ""),
        "channel": str(t.get("channel") or t.get("uploader") or ""),
        "dur": vm.mmss(t.get("duration") or 0) if t.get("duration") else "",
        "found": str(t.get("query") or ""),        # which search turned this up
        "cached": bool(t.get("cached") or t.get("from_cache")),
        "art": "",                       # filled by the warm-up thread
        "note": note,
    }
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
    loved = [{"title": r.get("display_title") or r.get("title") or "?",
              "artist": r.get("display_artist") or r.get("artist") or "",
              "note": _ago(r.get("ts"), now_ts) or "loved",
              "q": " ".join(x for x in (r.get("display_artist") or r.get("artist"),
                                        r.get("display_title") or r.get("title")) if x).strip(),
              } for r in reversed(liked)]
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
        rows.append({"id": "", "title": r.get("display_title") or r.get("title") or "?",
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
            "engine": brain.configured_engine(),
            "note": brain.why_offline()}


def engine_note(info: dict) -> str:
    """
    The engine line, in the same words the Tk header pill uses.

    Both skins ask `viewmodel` rather than each keeping a copy of "what do I say
    when the LLM fell back", which is how a UI drifts into contradicting the other.
    """
    info = info or {}
    line, _colour = vm.human_status(str(info.get("engine") or ""),
                                    str(info.get("llm_error") or ""))
    return line


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
    idle = str(st.get("idle") or "")
    why = {"finished": "the queue ran out - press Play or type a mood to keep going",
           "no stream would start": ("this song is still playing; the next few streams "
                                     "refused to start (rate limit or expired signature), "
                                     "so it retries by itself")}.get(idle, "")
    if not why:
        why = str((dj.info or {}).get("why") or "")
    if getattr(dj, "station", ""):
        # say what a set is built around, or 20 similar tracks just look random
        why = (why + "  ·  " if why else "") + f"station: {dj.station}"
    # one string for both slots: the page shows `idle_note || why`, and a state
    # consumer that reads only `why` deserves the station suffix and the same
    # "queue ran out" explanation rather than a second, thinner version of it
    prof = taste.load_state()
    artists = sorted((prof.get("artists") or {}).items(), key=lambda kv: -kv[1])
    tags = sorted((prof.get("genres") or {}).items(), key=lambda kv: -kv[1])
    cache = st.get("cache") or [0, 0]
    art_q = ctx.art
    for t in [np] + (st.get("up_next") or []):
        if t and t.get("id"):
            try:
                art_q.put_nowait(t)
            except queue.Full:
                pass            # a full warm-up lane delays art, never a click
    return {
        "now": now,
        "up_next": rows,
        "queued": int(st.get("queued") or 0),
        "position": float(st.get("position") or 0),
        "duration": float(st.get("duration") or 0),
        "paused": bool(st.get("paused")),
        "auto": bool(st.get("auto")),
        "volume": ctx.volume,
        "backend": str(st.get("backend") or ""),
        "idle": idle,
        "idle_note": why,
        "why": why,
        "request": str(st.get("request") or ""),
        "queries": list(st.get("queries") or []),
        "engine_note": engine_note(dj.info),
        "cache_note": (f"audio cache: {int(cache[1]) if len(cache) > 1 else 0} stored"
                       f", {int(cache[0]) if cache else 0} downloading"),
        "foot": ("YouTube Music, no Premium anywhere in this. Queue and likes live in "
                 f"{config.APP_DIR}. Art is served at the size each slot needs."),
        "log": list(dj.log[-40:]),
        "search": {"pending": bool(ctx.search.get("pending")),
                   "q": str(ctx.search.get("q") or ""),
                   "note": str(ctx.search.get("note") or ""),
                   "rows": [row_view(t) for t in (ctx.search.get("rows") or [])]},
        "loved": loved_rows(state=prof),
        "station": str(st.get("station") or ""),
        "repeat": str(st.get("repeat") or "off"),
        "shuffle": bool(st.get("shuffle")),
        "library": library_view(prof),
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
    """Stamp the hrefs for artwork that is already on disk; never fetch here."""
    if state.get("now"):
        href = ctx.art_href(state["now"].get("id") or "", big=True)
        if href:
            state["now"]["art"] = href
    for t in state["up_next"]:
        href = ctx.art_href(t.get("id") or "")
        if href:
            t["art"] = href
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
        self._search_seq = 0                  # which search is allowed to land
        self.job = None                       # the thread building a mix
        self._job_lock = threading.Lock()     # who is allowed to start the next one
        self.art: queue.Queue = queue.Queue(maxsize=64)
        self._seen_art: set[str] = set()
        try:
            # a cover that lands late still has to reach the page; this callback runs
            # on the covers thread and only touches the two dicts below
            covers.set_notifier(self._cover_ready)
        except Exception:
            pass
        self._hrefs: dict[str, str] = {}      # id -> row-sized artwork href
        self._bigs: dict[str, str] = {}       # id -> now-playing-sized artwork href
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
    def art_href(self, vid: str, big: bool = False) -> str:
        """The href for artwork already on disk ("" = not yet, never a fetch)."""
        if not vid:
            return ""
        with self._lock:
            if big:
                return self._bigs.get(str(vid)) or self._hrefs.get(str(vid), "")
            return self._hrefs.get(str(vid), "")

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

    def _store_href(self, vid: str, path: str, big: bool = False) -> None:
        href = self._href_for(path)
        if not vid or not href:
            return
        with self._lock:
            self._hrefs[str(vid)] = href
            if big:
                self._bigs[str(vid)] = href

    def _art_loop(self) -> None:
        # Two sources, in the order that makes the window look right: the Cover Art
        # Archive gives a square album cover (the only artwork worth blurring behind
        # the page), and a frame of the video is the fallback that is always there.
        # `covers.attach` never waits here - it queues onto covers' own paced thread
        # and the notifier hands the file back when it lands.
        while not self._stop.is_set():
            try:
                t = self.art.get(timeout=0.4)
            except queue.Empty:
                continue
            vid = str((t or {}).get("id") or "")
            if not vid or vid in self._seen_art:
                continue
            self._seen_art.add(vid)
            current = vid == str((self.dj.current or {}).get("id") or "")
            try:
                covers.remember_track(t)          # one album may dress its whole set
                if current or covers.row_mode():
                    covers.attach(t)
            except Exception:
                pass
            try:
                row = thumbs.get(t, "row")
                self._store_href(vid, row)
                if current:
                    self._store_href(vid, thumbs.get(t, "big"), big=True)
            except Exception:
                pass                     # no art is a look, not a failure

    def _cover_ready(self, vid, kind, path) -> None:
        """`covers` calls this on its own thread when an album image lands."""
        try:
            self._store_href(str(vid or ""), str(path or ""), big=str(kind or "") == "big")
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
    t = _row_ids(ctx).get((fields.get("id") or [""])[0])
    if not t:
        return "that row is gone"
    q = ctx.dj.queue
    q.insert_at(q.pos, dict(t))
    ctx.dj.next(force=True)
    return f"playing {t.get('title')}"


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


def action_open(ctx, fields: dict) -> str:
    """
    Hand the current track to a real client: Spotube or the browser.

    `open_externally` is the same handoff `--backend spotube` uses (flatpak-aware,
    so it reaches the sandboxed Spotube too). It is deliberately NOT
    `playerctl play`: this button says "open this song", and pressing it should
    not resume a player the user paused on purpose.
    """
    t = ctx.dj.current or {}
    url = t.get("url") or (f"https://music.youtube.com/watch?v={t.get('id')}"
                           if t.get("id") else "")
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
    return "station label cleared"


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
    # 45s retry hold - same rule the CLI verbs and the Tk buttons follow
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
    "seek": action_seek,
    "volume": action_volume,
    "request": action_request,
    "mix": action_mix,
    "radio": action_radio,
    "play_row": action_play_row,
    "queue_next": action_queue_next,
    "love_row": action_row_love,
    "open": action_open,
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
    if one("clear_key") == "1":
        vals["LLM_API_KEY"] = ""
    elif one("key"):
        vals["LLM_API_KEY"] = one("key")
    if not vals:
        return 400, {"error": "nothing to save - send key, base, model or clear_key"}
    try:
        config.save_llm_config(**vals)
    except OSError as e:
        # the message is a filesystem complaint, not a secret; replacing it with a
        # class name leaves "could not write the config" and no idea which file stuck
        return 500, {"error": f"could not write the config: {e.__class__.__name__}: {e}"}
    return 200, {"settings": settings_view(),
                 "note": "saved" if "LLM_API_KEY" in vals else "saved (key kept as it was)"}


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
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                               "script-src 'unsafe-inline'; img-src 'self' data:; "
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
        ctx.job = threading.Thread(
            target=lambda: _first_mix(dj, request, playlist, count), daemon=True)
        ctx.job.start()
    elif _has_profile():
        # open the app, hear music: the profile is a perfectly good request
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
