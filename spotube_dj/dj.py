"""
dj.py - the DJ core: plan -> search -> rank -> queue -> play -> learn.

Works with zero credentials (offline brain + YouTube Music audio).
Adds taste learning from likes/skips, and optional Spotify metadata seeding.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import audiocache
import brain
import config
import player as player_mod
import providers as prov
import taste

# how much of a track you must hear before we count it as "enough"
HEARD_ENOUGH = 0.72
HEARD_BARELY = 0.28


class Queue:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.pos = 0
        self._lock = threading.RLock()

    def extend(self, tracks: list[dict]) -> None:
        with self._lock:
            self.items.extend(tracks)

    def upcoming(self, n: int) -> list[dict]:
        with self._lock:
            return self.items[self.pos:self.pos + n]

    def insert_at(self, i: int, t: dict) -> None:
        with self._lock:
            self.items.insert(max(0, min(i, len(self.items))), t)

    def shuffle(self) -> int:
        """
        Randomise what is left to play; returns how many rows were mixed.

        Only from the cursor on: the row that is audible is not in the queue, and
        moving the cursor under the player would make the progress bar lie.
        """
        with self._lock:
            rest = self.items[self.pos:]
            random.shuffle(rest)
            self.items[self.pos:] = rest
            return len(rest)

    def rewind_all(self) -> None:
        """Back to the first row (repeat-all); the rows themselves are untouched."""
        with self._lock:
            self.pos = 0

    def rewind(self, n: int) -> None:
        """Give back the last n popped items (an unplayable track is not a heard one)."""
        with self._lock:
            self.pos = max(0, self.pos - max(0, n))

    def pop(self) -> dict | None:
        with self._lock:
            if self.pos >= len(self.items):
                return None
            t = self.items[self.pos]
            self.pos += 1
            return t

    def __len__(self) -> int:
        with self._lock:
            return max(0, len(self.items) - self.pos)

    def clear_ahead(self) -> int:
        """
        Forget everything after the cursor; -> how many rows went.

        The row being heard is not in here (`pos` is the *next* row to play), which is
        what makes "Clear queue" safe to put next to a Stop button: it empties the
        list without silencing the song, and it leaves history and taste alone -
        those are the listener's record, not the queue's.
        """
        with self._lock:
            kept = min(self.pos, len(self.items))
            n = len(self.items) - kept
            del self.items[kept:]
            return n

    def remove_id(self, vid: str) -> dict | None:
        """
        Drop one queued row by its video id; -> the row, or None if it is not there.

        Only rows at or after the cursor are removable - that is what "still
        queued" means. The row being heard was already popped out of `items`
        (so `pos` is the next one to play), and the rows before the cursor are
        history that a Remove button must not touch. This is what a per-row
        "Remove from queue" button needs that `clear_ahead()` does not offer:
        one row, not everything after the cursor.
        """
        vid = str(vid or "")
        if not vid:
            return None
        with self._lock:
            for i in range(self.pos, len(self.items)):
                t = self.items[i]
                if str(t.get("id") or "") == vid:
                    return self.items.pop(i)
        return None


# Words that appear in a *request* but never in a legit track title - if a
# search hit shares none of the real signal, it's an ad / off-topic upload.
_STOP_QUERY = {"for", "the", "a", "an", "to", "with", "and", "of", "some",
               "play", "please", "music", "songs", "song", "track", "tracks",
               "something", "like", "my", "me", "best", "new", "official"}

_AD_SIGNAL = re.compile(
    r"unboxing|review\b|sponsored|ad\s*break|giveaway|discount|coupon|"
    r"glasses|insurance|vpn|casino|crypto|course|tutorial|how to|"
    r"podcast|highlights|full match|reaction|compile|fails", re.I)


def _relevant(track: dict, query: str) -> bool:
    """
    A hit must share at least one meaningful token with the query it came from.
    Guards against YT Music surfacing 'I tried the new night driving glasses'
    for a query of 'dark synthwave for night driving'.
    """
    qt = [w for w in taste.tokens(query) if w not in _STOP_QUERY]
    if not qt:
        return True
    hay = taste.norm(f"{track.get('title', '')} {track.get('artist', '')}")
    if _AD_SIGNAL.search(hay):
        return False
    return any(q in hay for q in qt)


def _is_stream(t: dict) -> bool:
    """A 24/7 radio / multi-hour broadcast is not a DJ-able unit."""
    # The length window is enforced once, in providers.yt_search. This guard
    # only catches what that cannot see: YouTube reports NO duration for live
    # broadcasts, so "longform-ish title + no duration" == 24/7 stream.
    _LIVEY = re.compile(r"\bradio\b|24/7|live\s*stream|streaming\s+now|"
                        r"\d+\s*hours?|\d+h\s*(?:loop|mix|radio)", re.I)
    hay = f"{t.get('title') or ''} {t.get('artist') or ''} {t.get('channel') or ''}"
    return not (t.get("duration") or 0) and bool(_LIVEY.search(hay))


def build_queue(request: str, seed_refs: list[str] | None = None,
                count: int = 25, extra_queries: list[str] | None = None,
                on_progress=None, seeds: list[dict] | None = None
                ) -> tuple[list[dict], dict]:
    """
    Returns (tracks, info). Searches YouTube Music for each planned query,
    dedupes against what you've already heard, ranks by taste, interleaves.

    Two kinds of seed, because two UIs need them: `seed_refs` are *references*
    (a Spotify playlist URL/id, or artist words) that have to be fetched, while
    `seeds` are tracks already in hand - which is what "start a station from
    this song" has: one row from the queue, no playlist to read. Passing `seeds`
    also skips the liked-songs fallback, so a station stays about that song
    instead of quietly turning into your whole library.
    """
    seeds: list[dict] = [s for s in (seeds or []) if s]
    sp = prov.Spotify()
    sp_available = bool(prov.CLIENT_ID and prov.CLIENT_SECRET)
    no_playlist_data = 0

    if seed_refs:
        # an explicit --playlist always attempts a read; Spotify.playlist_seed
        # itself returns [] when there are no credentials or the API refuses,
        # so we don't gate this on the client id being present.
        for ref in seed_refs:
            if not ref:
                continue
            if not prov.is_playlist_ref(ref):
                # plain seed words ("bibio, hammock") -> treat as artists to explore
                for w in re.split(r"[,;]|\band\b", ref):
                    w = w.strip()
                    if w:
                        seeds.append({"id": "", "title": "", "artist": w, "duration": 0})
                continue
            got = sp.playlist_seed(ref)
            if got:
                seeds.extend(got)
            else:
                no_playlist_data += 1
    elif sp_available and not seeds:
        # only when nobody said what the station is about: a "more like this song"
        # request that also pulled in your whole liked list stops being about that
        # song, which is the opposite of what the button promises
        seeds.extend(sp.liked(limit=30)[:30])
        # also fold in what you've been playing in Spotube/Spotify lately
        seeds.extend(sp.recently_played(limit=15))

    plan = brain.plan(request, seeds=seeds[:12] if seeds else None)
    # trimmed here rather than in the parser so an LLM-written plan gets the
    # same hygiene as the offline one, and so `why` can still name the mood
    raw_queries = list((plan.get("queries") or [])) + list(extra_queries or [])
    queries = list(dict.fromkeys(
        q for q in (brain.search_query(x) for x in raw_queries)
        # a plan built by splitting "more like Portishead - Dummy" on the dash
        # hands back "- dummy", which is not a search; require some letters first
        if q and len(re.sub(r"[^0-9a-z\u00c0-\uffff]", "", q)) > 3))[:10]
    if not queries:
        queries = [(request or "eclectic favorites").strip().lower()[:80]]
        why = (plan.get("why") or "") + ", searched as asked (nothing left to search)"
        plan = dict(plan)
        plan["why"] = why

    if seeds:
        # seed -> "more like this" queries built from real artist names
        for s in seeds[:6]:
            a = (s.get("artist") or "").split(",")[0].strip()
            if a:
                queries.append(f"{a.lower()} top songs")
        # each query is a network search of half a second or more, so the number of
        # them follows the size of the ask: a 12-track station does not need 11
        queries = list(dict.fromkeys(queries))[:max(4, min(10, count // 2 + 1))]

    seen_ids, seen_titles = set(), set()
    seen_ids |= config.recent_uris(400)
    buckets: list[list[dict]] = []
    errors = 0
    filtered = 0
    streams = 0

    for n, q in enumerate(queries, 1):
        if on_progress:
            try:
                on_progress(f"[{n}/{len(queries)}] searching '{q[:44]}' ...")
            except Exception:
                pass
        res = prov.yt_search(q, limit=8, max_dur=3600)
        if not res:
            errors += 1
            continue
        kept = []
        for t in res:
            if not _relevant(t, q):
                filtered += 1
                continue
            if (_is_stream(t) or t.get("longform")) and len(queries) > 1:
                # a fallback long-form may survive yt_search when a query has
                # nothing else; it must not pollute a multi-query DJ set. The tag
                # is what catches the 40-minute "genre | chill vibes" set, which
                # looks like a song to _is_stream because it has a duration.
                streams += 1
                continue
            # fingerprint() folds "Artist - Song (Official Video)" and
            # "Song" together so one song isn't queued from two channels
            key = taste.fingerprint(t.get("title", ""))
            if t["id"] in seen_ids or (key and key in seen_titles):
                continue
            seen_ids.add(t["id"])
            if key:
                seen_titles.add(key)
            t["query"] = q
            kept.append(t)
        if kept:
            buckets.append(kept)

    flat = [t for t in itertools.chain.from_iterable(itertools.zip_longest(*buckets)) if t]
    ranked = taste.score_tracks(flat, avoid=plan.get("avoid"))

    if on_progress:
        try:
            on_progress(f"scoring {len(flat)} candidates, building the set ...")
        except Exception:
            pass
    out = _interleave(ranked, buckets, count)
    if not out and ranked:          # everything got capped; fall back to raw order
        out = ranked[:count]

    info = {
        "engine": plan.get("engine"),
        "why": plan.get("why"),
        # carried through so the GUI/CLI can say WHY it fell back instead of
        # just showing "offline" (build_queue used to drop this, which silently
        # hid every real LLM error from the user)
        "llm_error": plan.get("llm_error") or "",
        "llm_notes": plan.get("llm_notes") or [],
        "queries": queries,
        "avoid": plan.get("avoid") or [],
        "searched": len(queries),
        "empty_searches": errors,
        "candidates": len(flat),
        "seeded_from": len(seeds),
        "playlist_unreadable": no_playlist_data,
        "off_topic_filtered": filtered,
        "streams_dropped": streams,
        "spotify": "metadata on" if sp_available else "off (no client id)",
    }
    return out, info


def _spread(rows: list[dict], cap: int = 3) -> list[dict]:
    """
    At most `cap` tracks per artist in one batch, with the extras pushed to the end.

    Searches overlap on the obvious artist, so a taste mix born from three loved
    records can arrive as 18 rows of Portishead - which is not a DJ set, it is a
    discography. Dropping the extras outright would shorten the queue for no gain,
    so they go back: if everything else was already heard, the fifth Portishead
    track still beats an empty queue.
    """
    out: list[dict] = []
    rest: list[dict] = []
    counts: dict[str, int] = {}
    for t in rows or []:
        a = taste.norm((t or {}).get("artist") or (t or {}).get("channel") or "")
        if not a or counts.get(a, 0) < cap:
            counts[a] = counts.get(a, 0) + 1
            out.append(t)
        else:
            rest.append(t)
    return out + rest


def _weave(rows: list[dict], mixed: list[dict], every: int = 3) -> list[dict]:
    """
    Spread the taste picks through the queue instead of bolting them on the end.

    A top-up returns request results first and profile results second, so a plain
    `extend` buries the "it learned from my likes" rows 15-20 deep: the listener
    never hears one of them and concludes the learning is fake. Every `every`
    tracks, one of the profile's own choices comes up - enough to be noticed, not
    enough to feel like the same three artists on repeat.
    """
    if not mixed:
        return list(rows)
    pick = {id(t) for t in mixed}
    plain = [x for x in rows if id(x) not in pick]
    taste_rows = [x for x in rows if id(x) in pick]
    out: list[dict] = []
    while plain or taste_rows:
        out += plain[:every]
        plain = plain[every:]
        if taste_rows:
            out.append(taste_rows.pop(0))
    return out


def _interleave(ranked: list[dict], buckets: list[list[dict]], count: int) -> list[dict]:
    """
    Round-robin across the query buckets so the set doesn't become 20 tracks
    from one search. Global artist cap keeps it from feeling repetitive.
    """
    by_id = {id(t): t for t in ranked}
    queues: list[list[dict]] = []
    for b in buckets:
        q = [t for t in b if id(t) in by_id]
        q.sort(key=lambda x: -x.get("score", 0.0))
        if q:
            queues.append(q)
    out: list[dict] = []
    seen: set[str] = set()
    caps: dict[str, int] = {}
    while queues and len(out) < count:
        for q in list(queues):
            picked = None
            for cand in q:
                a = taste.norm(cand.get("artist", ""))
                if cand["id"] in seen or caps.get(a, 0) >= 2:
                    continue
                picked = cand
                break
            if picked is None:
                queues.remove(q)
                continue
            q.remove(picked)
            seen.add(picked["id"])
            caps[taste.norm(picked.get("artist", ""))] = caps.get(taste.norm(picked.get("artist", "")), 0) + 1
            out.append(picked)
            if len(out) >= count:
                break
    return out


class DJ:
    # Defaults on the class, not only in __init__: tests and both skins build a DJ
    # with DJ.__new__(DJ) to skip the player setup, and `_note`/`status` read these.
    # progress is an optional live sink for the engine's own lines (the GUI log);
    # station names the row a "start a station from this song" set was built around.
    progress = None
    station = ""
    # read by status() and next() on doubles built with DJ.__new__, so the defaults
    # live on the class and not only in __init__
    repeat = "off"
    shuffle = False
    # the loop's watchdog state; on the class as well so a `DJ.__new__` double can
    # call `_tick()` without an AttributeError arriving instead of a track change
    _stall = 0.0
    _tick_pos: float | None = None
    _ended_errors = 0
    _handoff_noted = False

    def __init__(self, backend: str = "mpv", volume: int | None = None,
                 headless: bool = False) -> None:
        self.queue = Queue()
        self.state = config.load_state()
        self.backend = backend
        self.headless = headless or backend == "none"
        self.player = None
        self.current: dict | None = None
        # "" while playing; "finished" / "no stream would start" when a move
        # could not be made. The panel needs the reason, not a blank box.
        self.idle = ""
        self._skip_pressed = False
        self._judged_key: str | None = None
        self._hold_until: float = 0.0
        self.started_at = 0.0
        self.last_pos = 0.0
        self.paused = False
        self.auto = True
        self.repeat = str(self.state.get("repeat") or "off")
        self.shuffle = bool(self.state.get("shuffle"))
        # every query already turned into a search this session, so the mixer
        # never asks the same thing twice and the topping-up keeps widening
        self._mix_used: set[str] = set()
        self.request = ""
        self.seed_refs: list[str] | None = None
        self.info: dict = {}
        # The audio-advance loop must never block on the network/LLM. `_topup_async`
        # runs the (potentially slow) refill on one daemon thread at a time and the
        # play loop carries on, so a next-track that needs a mix-build starts the
        # current track immediately instead of waiting for the planner.
        self._refill: threading.Thread | None = None
        self._refill_lock = threading.Lock()
        # one advance at a time. `_topup_async` can finish and want to start the row
        # it just queued from a background thread while the play loop is already
        # mid-`next()`; without a guard both would pop and start two tracks.
        self._advancing = threading.Lock()
        # bounded on purpose: `_note` is called from the refill loop, the search
        # lane and every click, and a --daemon that runs for a week would otherwise
        # carry a year of timestamps in RAM to serve the last 40 of them. A list
        # rather than a deque because callers slice it (`log[-40:]`), and a deque
        # cannot.
        self.log: list[str] = []
        self._stop = threading.Event()
        if volume is not None:
            self.state["volume"] = volume
        if not self.headless and backend == "mpv" and player_mod.HAS_MPV:
            try:
                self.player = player_mod.MPVPlayer(volume=int(self.state.get("volume", 70)))
            except player_mod.PlayerError as e:
                self._note(f"mpv unavailable ({e}); falling back to spotube handoff")
                self.backend = "spotube"
                self.player = None

    # ------------------------------------------------------------- helpers
    def _note(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {msg}")
        if len(self.log) > self.LOG_LINES:
            del self.log[: len(self.log) - self.LOG_LINES]
        print(f"{msg}", flush=True)
        sink = self.progress
        if sink is not None:
            try:
                sink(msg)
            except Exception:
                pass          # a dead UI sink must never break the player

    def _label(self, t: dict | None) -> str:
        if not t:
            return "-"
        # a video row's artist is empty by design (the uploader is not the
        # artist), so the card says whose upload it is rather than "?"
        who = t.get("artist") or t.get("channel") or "?"
        return f"{who} - {t.get('title', '?')}"

    def _learn_from_heard(self) -> None:
        """
        Judge the track we just left, from how much of it we heard.

        Exactly once per track: the auto-advance loop calls next() again while
        `current` still points at the finished track (the queue was empty, or
        the next stream failed to resolve), and without this guard every one of
        those retries recorded another like - which is how a finished queue
        turned into "liked 21x, heard 292%" and wrecked the ranking.
        """
        pressed = getattr(self, "_skip_pressed", False)
        self._skip_pressed = False
        t, pos = self.current, self.last_pos
        if not t or (not pos and not pressed):
            return
        if t.get("id") in (None, "") and t.get("title") in (None, ""):
            return
        if getattr(self, "_judged_key", None) == self._track_key(t):
            return
        self._judged_key = self._track_key(t)
        dur = float(t.get("duration") or 0) or None
        ratio = (pos / dur) if dur else None
        if ratio is None:
            if pressed:
                # no length to measure, but a pressed ⏭ is unambiguous
                taste.record_skip(t, "skip")
                self._note("  learned: skipped by hand")
            return
        ratio = min(ratio, 1.0)          # a player that kept counting past the end
        if pressed:
            taste.record_skip(t, "early-skip" if ratio < 0.35 else "skip")
            self._note(f"  learned: skipped by hand (heard {ratio * 100:.0f}%)")
            return
        if ratio >= HEARD_ENOUGH:
            taste.record_like(t)
            self._note(f"  learned: liked (heard {ratio*100:.0f}%)")
        elif ratio <= HEARD_BARELY:
            taste.record_skip(t, "early-skip")
            self._note(f"  learned: skipped (heard {ratio*100:.0f}%)")
        else:
            taste.record_skip(t, "partial")

    @staticmethod
    def _track_key(t: dict) -> str:
        return f"{t.get('id') or ''}|{t.get('url') or ''}|{t.get('title') or ''}"

    def _topup(self, keep: int = 12, force: bool = False) -> None:
        """
        Refill the queue. Called from next(), so this happens while a track is
        still playing and the listener never reaches the end of it.

        Two sources, in order: the original request (what you asked for), then -
        when the DJ is on - queries derived from what you *liked*. That second
        half is what makes it a DJ instead of a playlist: re-running the request
        alone can only ever return the same handful of tracks YouTube ranks for
        those words, while a taste profile knows you also like a Nirvana deep cut
        and a Jethro Tull song you never typed anywhere.
        """
        if not force and len(self.queue) >= keep:
            return
        # The request is a source, not a precondition. This used to `return` when
        # `self.request` was empty, which meant a listener who pressed hearts but
        # typed nothing got a queue that never refilled - the taste model had the
        # answer and nobody asked it. `run()` also leans on this call, so the same
        # gate silently turned "--daemon" into "plays one track and stops".
        request = self.request or str(config.load_state().get("last_request") or "")
        pool: list[dict] = []
        info: dict = {}
        if request:
            tracks, info = build_queue(request, seed_refs=self.seed_refs,
                                       count=keep * 2)
            pool += tracks or []
        mixed: list[dict] = []
        if self.auto:
            mixed = self._auto_mix(keep)
            pool += mixed
        fresh = self._fresh(pool)
        if not fresh:
            if not request and not mixed:
                self._note("nothing to top up from yet - press the heart on a song "
                           "or type a mood, and the queue starts refilling itself")
            else:
                self._note("no new tracks to add (everything found is queued, "
                           "in your history, or was refused as non-music)")
            return
        # taste picks belong *inside* the set, not at the end of it: appended, they
        # arrive 20 tracks later, which is close to never, and the listener concludes
        # the DJ ignores their likes. Shuffle mixes the new rows, not the woven order
        # of the ones already queued - the weave is the DJ's judgement and it stays.
        rows = _spread(_weave(fresh, mixed))
        if self.shuffle:
            random.shuffle(rows)
        self.queue.extend(rows)
        self.info["queries"] = list(dict.fromkeys((self.info.get("queries") or []) +
                                                  (info.get("queries") or [])))
        n_mix = sum(1 for x in fresh if x.get("mixed"))
        self._note(f"queue topped up: +{len(fresh)} tracks"
                   + (f" ({n_mix} from what you like)" if n_mix else ""))

    def _topup_async(self, keep: int = 12, force: bool = False) -> None:
        """
        Refill the queue on a background thread, never holding the play loop.

        `next()` calls this instead of `_topup()` directly. The user-facing fix was
        "the audio freezes while the AI is processing the queue": when the queue ran
        low, a track change ran `build_queue` -> `brain.plan` (an LLM HTTP call, 5-30 s)
        synchronously on the DJ loop, so the next song waited for the planner and the
        player sat silent. Now the next row starts what is already queued and the
        refill happens alongside it. `_start_if_idle` restarts playback if this fill
        is what first gave the queue something to play.
        """
        with self._refill_lock:
            if self._refill is not None and self._refill.is_alive():
                return                  # one refill at a time; the running one covers it
            def work():
                before = len(self.queue.items)
                try:
                    self._topup(keep=keep, force=force)
                except Exception as e:
                    # a failed refill must never end the loop or go un-reported
                    self._note(f"[warn] background top-up failed: "
                               f"{e.__class__.__name__}: {e}")
                self._start_if_idle(len(self.queue.items) - before)
            self._refill = threading.Thread(target=work, daemon=True)
            self._refill.start()

    def _fresh(self, tracks: list[dict]) -> list[dict]:
        """
        Anything already queued or playing is dropped: searches overlap.

        The id alone is not enough. A top-up runs a *different* query, so the
        same song comes back as another video - the "Audio" upload against the
        "Official Video", a visualizer against the real thing - and the queue
        shows "Touch Me Softly" twice in five rows. `taste.fingerprint` folds
        those titles together, which is the same rule build_queue uses.
        """
        have = {t.get("id") for t in self.queue.items[self.queue.pos:]}
        keys = {taste.fingerprint(t.get("title", ""))
                for t in self.queue.items[self.queue.pos:]}
        if self.current:
            have.add(self.current.get("id"))
            keys.add(taste.fingerprint(self.current.get("title", "")))
        out = []
        for t in tracks or []:
            tid = (t or {}).get("id")
            key = taste.fingerprint((t or {}).get("title", ""))
            if not tid or tid in have or (key and key in keys):
                continue
            have.add(tid)
            if key:
                keys.add(key)
            out.append(t)
        return out

    def _auto_mix(self, keep: int = 12) -> list[dict]:
        """
        A few searches born from the profile, ranked by it, capped per artist.

        The cap is what keeps a mix from becoming "10 songs by the last thing you
        loved"; the exception is an artist you loved *twice*, which is a much
        stronger signal than one heart and can carry a couple of rows.
        """
        qs = taste.next_queries(avoid=sorted(self._mix_used), limit=4)
        if not qs:
            return []
        want = max(4, keep // 2)        # a top-up that returns one row is noise
        weights = config.load_state().get("artists") or {}
        out: list[dict] = []
        # one cap sheet for the whole refill, not one per query: three rows of an
        # artist per search over four searches is still "twelve songs by the last
        # thing you loved", which is the thing this cap exists to prevent
        caps: dict[str, int] = {}
        for q in qs:
            self._mix_used.add(q)
            try:
                rows = prov.yt_search(q, limit=max(6, keep))
            except Exception as e:
                self._note(f"auto-mix: {q!r} failed ({e.__class__.__name__})")
                continue
            rows = [dict(r, query=q, mixed=True) for r in (rows or [])]
            rows = taste.score_tracks(rows)
            picked: list[dict] = []
            for r in rows:
                a = taste.norm(r.get("artist") or r.get("channel") or "")
                w = float(weights.get(a, 0.0) or 0.0)
                cap = 3 if w >= 6.0 else (2 if w >= 3.5 else 1)
                if caps.get(a, 0) >= cap:
                    continue
                caps[a] = caps.get(a, 0) + 1
                picked.append(r)
            out += picked
            if picked:
                self._note(f"auto-mix: {len(picked)} more from {q!r} "
                           f"(because of what you liked)")
            if len(out) >= want:
                break                   # enough for this refill; the next one widens
        return out

    LOG_LINES = 400      # what one DJ process keeps in memory for the log drawer

    # Signed googlevideo URLs expire (usually ~6h); refresh well before that.
    TICK_SECONDS = 0.75  # how often the loop looks at the player
    STALL_SECONDS = 45.0  # a position this stale is not a song, it is a hang
    SKIP_LIMIT = 3       # unplayable tracks tried before holding off
    HOLD_SECONDS = 45    # long enough for a rate limit to relax, short enough to notice
    STREAM_TTL = 3 * 3600

    def _resolve(self, t: dict) -> str | None:
        cached, at = t.get("stream"), t.get("stream_at", 0)
        if cached and (time.time() - float(at or 0)) < self.STREAM_TTL:
            return cached
        url = prov.yt_stream_url(t["id"])
        if url:
            t["stream"] = url
            t["stream_at"] = time.time()
        return url

    # -------------------------------------------------------------- control
    def start(self, request: str, seed_refs: list[str] | None = None,
              count: int = 20, extra_queries: list[str] | None = None,
              on_progress=None) -> dict:
        self.request = request
        self.seed_refs = seed_refs
        config.touch_last_request(request)
        self._note(f"planning: {request!r}")
        tracks, info = build_queue(request, seed_refs=seed_refs, count=count,
                                   extra_queries=extra_queries, on_progress=on_progress)
        self.info = info
        # the mixer must not repeat this request's own queries back at us
        self._mix_used = set(info.get("queries") or []) | {request}
        if not tracks:
            self._note("no candidates found - try a different phrasing")
            return {"ok": False, "info": info, "tracks": []}
        self.queue.extend(tracks)
        self._note(f"{info['engine']}: {info['why'] or 'queued'}")
        self._note(f"queries: {', '.join(info['queries'][:6])}")
        self._note(f"{len(tracks)} tracks queued from {info['candidates']} candidates")
        if self.player and not self.headless:
            # start downloading track 2 now: the resolve of track 1 takes a
            # couple of seconds and that is exactly the time it needs
            audiocache.prefetch(tracks[1:4], ahead=2)
        if not self.headless and self.backend in ("mpv",) and self.player:
            # force: a cooldown left over from the *previous* request must not
            # swallow the first track of the one the user just asked for.
            self.next(force=True)
        return {"ok": True, "info": info, "tracks": tracks}

    def taste_mix(self, count: int = 24) -> dict:
        """
        Fill the queue from the profile alone - no words required.

        This is the difference between a playlist and a DJ: the listener presses
        the heart a few times and expects the app to keep going on its own. Every
        search is asked of `taste.next_queries` (deeper cuts from a loved artist,
        then a favoured mood on its own), and the loved records are handed to the
        planner as seeds so an LLM - if one is configured - plans around real
        music rather than a guessed genre word.

        Returns {"ok", "reason", "tracks", "info"}. The empty-profile case answers
        with a sentence, because "the queue is empty" is not information.
        """
        state = config.load_state()
        liked = [{"title": r.get("display_title") or r.get("title") or "",
                  "artist": r.get("display_artist") or r.get("artist") or ""}
                 for r in (state.get("liked") or [])[-10:]]
        liked = [x for x in liked if x["title"] or x["artist"]]
        weights = state.get("artists") or {}
        if not liked and not any(w > 0 for w in weights.values()):
            self._note("nothing to mix from yet - press the heart on a song (or "
                       "--sync your Spotify likes) and this becomes a real DJ set")
            return {"ok": False, "reason": "no likes yet", "tracks": []}
        artists = [a for a, w in sorted(weights.items(), key=lambda kv: -kv[1])
                   if w > 0][:3]
        request = ("songs like " + ", ".join(artists)) if artists else \
            "eclectic favourites from the records I liked"
        qs = taste.next_queries(avoid=sorted(self._mix_used), limit=3)
        # the notes are this app's only voice, so "searching 1 ways" reads broken
        n_rec = len(liked)
        self._note(f"mixing from your likes: {n_rec} loved record{'s' if n_rec != 1 else ''}"
                   + (f", searching {len(qs)} {'way' if len(qs) == 1 else 'ways'}"
                      if qs else ""))
        # the planner already turns loved artists into "<artist> top songs"
        # searches, so at most two of ours go in as insurance - a fifth query that
        # re-asks for the same band buys duplicates, not variety
        tracks, info = build_queue(request, count=count, seeds=liked or None,
                                   extra_queries=qs[:2], on_progress=self.progress)
        self.info = info
        self._mix_used |= set(info.get("queries") or []) | {request}
        fresh = self._fresh(tracks)
        if not fresh:
            self._note("that led back to what you have already heard - like a few "
                       "more songs, or type a mood to widen the net")
            return {"ok": False, "reason": "nothing new found", "tracks": [],
                    "info": info}
        self.queue.extend(_spread(fresh))
        # a count belongs in the log: "mixing from your likes" on its own reads like
        # a promise the app never closed, and this is the line both skins show
        self._note(f"mix ready: {len(fresh)} tracks from what you like")
        self._start_if_idle(len(fresh))
        return {"ok": True, "tracks": fresh, "info": info}

    def radio_from(self, track: dict | None, count: int = 20) -> dict:
        """
        "Start a station from this song", for both skins.

        One row of the queue becomes the seed: the planner is told the artist and
        title, `build_queue` adds the "<artist> top songs" searches that actually
        return that artist's music, and the taste ranking then pulls in the
        neighbours. It lands in the queue rather than replacing it, so pressing it
        mid-album cannot lose the rest of what you were about to hear.

        It used to be duplicated in each skin, and both copies were broken in their
        own way - the Tk one passed a keyword that did not exist, the web one built
        a station nobody ever started.
        """
        t = track or {}
        if not (t.get("title") or t.get("artist") or t.get("channel")):
            return {"ok": False, "reason": "that row has no song to build from",
                    "tracks": []}
        label = f"{t.get('artist') or t.get('channel') or ''} - {t.get('title')}".strip(" -")
        seed = {"title": t.get("title", ""), "artist": t.get("artist") or t.get("channel", ""),
                "url": t.get("url", "")}
        self.station = label
        self._note(f"building a station around: {label}")
        try:
            tracks, info = build_queue(f"more like {label}", count=count, seeds=[seed],
                                       on_progress=self.progress)
        except Exception as e:
            self._note(f"[warn] the station could not be built: {e.__class__.__name__}: {e}")
            return {"ok": False, "reason": f"{e.__class__.__name__}", "tracks": []}
        self.info = dict(self.info or {})
        self.info.update({k: info[k] for k in ("engine", "why", "queries") if k in info})
        # a station should persist: if there was no mood at all, this becomes it
        self.request = self.request or f"more like {label}"
        fresh = self._fresh(tracks)
        if not fresh:
            self._note("no similar tracks found - try a better-known artist, or a "
                       "different row")
            return {"ok": False, "reason": "nothing similar found", "tracks": [],
                    "info": info}
        self.queue.extend(_spread(fresh, cap=2))
        self._note(f"station ready: {len(fresh)} tracks around {label}")
        self._start_if_idle(len(fresh))
        return {"ok": True, "tracks": fresh, "info": info}

    def _start_if_idle(self, added: int) -> None:
        """Start the new row if - and only if - nothing is playing right now."""
        if not added or self.current or self.headless or not self.player:
            return
        self.next(force=True)

    def next(self, force: bool = False) -> dict | None:
        """
        Move to the next playable track.

        A stream that cannot be resolved or started must NOT cost the user the
        rest of the queue: this used to recurse (return self.next()) and one
        click on a rate-limited day popped all 8 tracks in ~0ms. Now at most
        SKIP_LIMIT are tried, the queue is rewound, and playback holds off for a
        cooldown so a transient yt-dlp block recovers by itself.

        `force` is for a *human* pressing next/prev: the cooldown exists to stop
        the machine hammering a rate-limited resolver, never to make a button
        dead. A forced call drops the hold and tries once more (re-holding if it
        still fails), which is why a manual skip must pass force=True.
        """
        # If the play loop (or the background refill) is already moving to the next
        # row, this call is a no-op rather than a second advance. The no-op returns
        # None, which the caller that raced in treats as "didn't move" and retries on
        # the next tick - so nothing is ever skipped or double-started.
        if not self._advancing.acquire(blocking=False):
            return None
        try:
            return self._next_locked(force)
        finally:
            self._advancing.release()

    def _next_locked(self, force: bool = False) -> dict | None:
        """Body of `next`, run only while holding the single-advance guard."""
        if self._hold_until and time.time() < self._hold_until:
            if not force:
                return None
            self._hold_until = 0.0
            self._note("manual change during the cooldown - retrying the resolver now")
        self._learn_from_heard()
        self._topup_async()
        if self.repeat == "one" and not force and self.current:
            # repeat-one is about a track ending, not about a button: pressing next
            # still moves on, exactly like the player everybody already knows
            self.queue.insert_at(self.queue.pos, self.current)
        unplayable: list[dict] = []
        while len(unplayable) <= self.SKIP_LIMIT:
            t = self.queue.pop()
            if t is None:
                if unplayable:
                    self.queue.rewind(len(unplayable))
                unplayable = []
                self.last_pos = 0.0
                if self.repeat == "all" and self.queue.items:
                    # the set ends and starts again. This does not fight `auto`:
                    # a refill adds to the end and looping is about the front, so
                    # someone who wants one fixed set turns Keep mixing off
                    self.queue.rewind_all()
                    if self.shuffle:
                        self.queue.shuffle()
                    self._note("repeat all - starting the set again")
                    continue
                self._rest_after_failure("finished")
                self._note("queue empty")
                return None
            self.current, self.started_at = t, time.time()
            self.last_pos, self.idle = 0.0, ""
            ok, why = self._try_start(t)
            if ok:
                for back in unplayable:            # they were never heard; put them back
                    self.queue.insert_at(self.queue.pos, back)
                unplayable = []
                config.append_history({"id": t["id"], "title": t["title"],
                                       "artist": t["artist"], "ts": time.time(),
                                       "query": t.get("query", "")})
                self._note(f"playing: {self._label(t)}"
                           + ("  [from cache]" if t.get("from_cache") else ""))
                # while this one plays, the next two go onto disk - at the *front* of
                # the lane. Appending behind rows from the end of the set is what made
                # an active skipper wait: the row they were about to hear was 14th in
                # line for a download, so every press started cold.
                up = self.queue.upcoming(3)
                if up:
                    audiocache.promote(str(up[0].get("id") or ""))
                audiocache.prefetch(up, ahead=2, priority=True)
                return t
            self._note(why)
            unplayable.append(t)
        # too many failures in a row: stop burning the queue
        self.queue.rewind(len(unplayable))
        self.last_pos = 0.0
        self._rest_after_failure("no stream would start")
        self._hold_until = time.time() + self.HOLD_SECONDS
        self._note(f"[error] {len(unplayable)} streams would not start in a row - "
                   f"holding {self.HOLD_SECONDS}s and trying again "
                   f"(rate-limited? signed URLs expire). Your queue is intact.")
        return None

    def _rest_after_failure(self, why: str) -> None:
        """
        Called when nothing could be started. What the panel says here is the
        difference between "the app lost the plot" and "the queue ran out".

        The old code set `current = None` unconditionally, which blanked Now
        Playing to "Nothing playing" while the previous track was still audible
        (mpv is not told to stop by a failed *skip*), and - because the Up Next
        list is built as `[now_playing] + up_next` - it also deleted row 1 of the
        chronology, so everything shown was one track ahead of reality. The title
        now stays up and `idle` says why nothing new is coming.
        """
        audible = False
        try:
            pl = self.player
            probe = getattr(pl, "is_playing", None) if pl is not None else None
            if callable(probe):
                audible = bool(probe())
        except Exception:
            audible = False
        audible = audible or bool(self.paused)   # paused is still "this track""
        self.idle = why
        if not audible:
            self.current = None

    def _try_start(self, t: dict) -> tuple[bool, str]:
        label = self._label(t)
        if self.headless or self.backend != "mpv" or not self.player:
            if self.backend == "spotube":
                ok = player_mod.playerctl("play") or player_mod.open_externally(t["url"])
                self._note("handed to Spotube/browser" if ok else f"open manually: {t['url']}")
            return True, ""
        # Cache first: a downloaded file needs no resolver call, no DNS, no
        # signed URL that can expire mid-song - and it starts in milliseconds.
        path, url = audiocache.lookup(t)
        if path:
            t["from_cache"] = True
        else:
            t.pop("from_cache", None)
            if not url:
                url = self._resolve(t)
                if url:
                    audiocache.remember(str(t.get("id") or ""), url)
        if not url:
            return False, f"could not resolve stream for {label}"
        if not self.player.play_url(url):
            if not self.player.alive():
                raise player_mod.PlayerError("mpv died - see mpv.log")
            if url and not path:
                # a URL the cache lane stashed is an hour old at best and a signed
                # one can expire inside that; "mpv refused it" is not evidence the
                # track is unplayable, so resolve once more before calling it dead
                fresh = self._resolve(t)
                if fresh and fresh != url and self.player.play_url(fresh):
                    audiocache.remember(str(t.get("id") or ""), fresh)
                    return True, ""
            return False, f"player could not start {label} (see {self.player.log_path})"
        return True, ""

    def prev(self) -> dict | None:
        if self.queue.pos > 1:
            self.queue.pos -= 2
            return self.next(force=True)     # see skip(): a button is never held off
        return self.current

    def skip(self) -> dict | None:
        """
        Human 'next'. force=True so a queued track is never trapped behind the
        auto-retry cooldown.

        The taste record is written by `_learn_from_heard()`, which `next()`
        calls and which already knows how much of the track was heard. Judging
        here as well meant one press wrote two entries - a skip and then
        "liked (heard 96%)" for the same song, which is a profile fighting with
        itself. And judging *before* the move meant a press that moved nothing
        (queue empty, or no stream would start) still marked the song you are
        still listening to as skipped.
        """
        if len(self.queue) <= 0:
            # A press with nothing to move to must not be read as a verdict on
            # the track you are still hearing: `next()` judges the track it is
            # leaving, so calling it here would mark an still-audible song as
            # skipped just because the queue ran out.
            self.idle = "finished"
            self._note("nothing to skip to - the queue is empty")
            return None
        self._skip_pressed = True
        pos, dur = (self.player.progress() if self.player else (0, 0))
        if pos:
            self.last_pos = pos
        t = self.next(force=True)
        if t is None:
            self._skip_pressed = False    # nothing moved: no verdict, no blank
        return t

    def like(self) -> None:
        if self.current:
            taste.record_like(self.current)
            self._note(f"liked: {self._label(self.current)}")
            # keep this track in the rotation: bump its artist and re-queue it
            # later rather than letting history suppress it forever.
            self.state = config.load_state()

    def forget_taste(self) -> dict:
        """
        Wipe the learned profile, resync, and say what was lost.

        A verb on the engine rather than something each skin does with `taste` and
        `config` by hand: the web panel, the CLI verbs and `--clear-taste` then all
        report the same counts, and the log line is written once.
        """
        gone = taste.clear()            # which files a copy in `taste-undo.json`
        self.state = config.load_state()
        self._note(f"taste cleared: {gone.get('liked', 0)} loved, "
                   f"{gone.get('skipped', 0)} refused, {gone.get('artists', 0)} artists "
                   f"and {gone.get('genres', 0)} tags forgotten"
                   + (" - undo is one tap away" if any(gone.values()) else ""))
        return gone

    def restore_taste(self) -> dict:
        """
        Undo `forget_taste()`.

        Returns what came back; an empty dict means there was no snapshot, which is
        a different thing from "I restored nothing you care about" and reads that way
        in the log line below.
        """
        back = taste.restore()
        self.state = config.load_state()
        if back:
            self._note(f"taste restored: {back.get('liked', 0)} loved, "
                       f"{back.get('artists', 0)} artists back in the mix")
        else:
            self._note("nothing to restore - no cleared profile is being kept")
        return back

    def unlike(self) -> None:
        """Undo a heart. The artist bump stays - retraining on a retraction is
        how recommender loops start, and one +2.0 in a long profile is noise."""
        if self.current:
            n = taste.forget_like(self.current)
            self._note(("unliked: " if n else "was not liked: ") + self._label(self.current))

    def is_liked(self, t: dict | None) -> bool:
        return bool(t) and taste.is_liked(t)

    def pause(self) -> None:
        self.paused = True
        if self.player:
            pos, _ = self.player.progress()
            self.last_pos = pos
            self.player.pause()

    def resume(self) -> None:
        self.paused = False
        if self.player:
            self.player.resume()

    def volume(self, pct: int) -> None:
        self.state["volume"] = int(pct)
        config.save_state(self.state)
        if self.player:
            self.player.volume(pct)

    def add(self, tracks: list[dict]) -> None:
        self.queue.extend(tracks)
        self._note(f"queued {len(tracks)} more")

    def clear_queue(self) -> str:
        """
        Empty the queue, keep the song, and say what happens next. -> the sentence.

        "why theres no clear queue list button" - there was no verb to put a button on.
        The wording is the other half of the feature: with keep mixing on this is a
        *pause* in programming, not a silence, and a listener who is not told so will
        report "the clear button does nothing" - correctly, from where they sit. It
        deliberately does no searching here: a button press must not become the thing
        that waits on YouTube, so the refill is scheduled by whoever has a job lane
        (the web action) or left to the loop that is already running.
        """
        n = self.queue.clear_ahead()
        dropped = f"queue cleared: {n} track{'s' if n != 1 else ''} dropped"
        note = (dropped + " - keep mixing is on, so the list fills again shortly"
                if self.auto else
                dropped + " - nothing refills it while keep mixing is off")
        self._note(note)
        return note

    # ------------------------------------------------------------------ loop
    # -------------------------------------------------- gui-facing controls
    def seek(self, seconds: float) -> bool:
        """
        Jump to an absolute position (the GUI progress bar). Also moves
        `last_pos`, otherwise a seek to 3:50 of a 3:52 track would be judged as
        'heard 99%' and loved itself into your taste profile.
        """
        if not self.player:
            return False
        try:
            self.player.seek(float(seconds))
            self.last_pos = float(seconds)
            self.started_at = time.time() - float(seconds)
            return True
        except Exception:
            return False

    def play_now(self, track: dict) -> dict | None:
        """Put `track` in front of the queue and start it (GUI row double-click)."""
        t = dict(track)
        self.queue.insert_at(self.queue.pos, t)
        return self.next(force=True)

    def queue_next(self, track: dict) -> None:
        """'Add to queue' without interrupting what is playing."""
        self.queue.insert_at(self.queue.pos + 1, dict(track))
        self._note(f"added to queue: {self._label(track)}")

    def set_auto(self, on: bool) -> None:
        """Keep mixing when the queue runs dry."""
        self.auto = bool(on)
        self._note("auto-DJ on - the queue keeps refilling from what you like"
                   if self.auto else "auto-DJ off - it stops after the current queue")

    def toggle_repeat(self) -> str:
        """off -> all -> one -> off, the order the transport button cycles through."""
        return self.set_repeat({"off": "all", "all": "one"}.get(self.repeat, "off"))

    def set_repeat(self, mode: str) -> str:
        mode = str(mode or "").strip().lower()
        if mode in ("", "cycle", "toggle"):
            return self.toggle_repeat()
        if mode not in ("off", "all", "one"):
            mode = "off"
        self.repeat = mode
        self.state["repeat"] = mode
        config.save_state(self.state)
        self._note({"all": "repeat all - the set starts over when it runs out",
                    "one": "repeat one - this track again when it ends",
                    "off": "repeat off"}[mode])
        return mode

    def toggle_shuffle(self) -> bool:
        """
        Flip it, and mix what is queued right now so the button takes effect
        immediately instead of someday, at the next refill.
        """
        self.shuffle = not self.shuffle
        self.state["shuffle"] = self.shuffle
        config.save_state(self.state)
        n = self.queue.shuffle() if self.shuffle else 0
        self._note(f"shuffle on - {n} queued track{'s' if n != 1 else ''} mixed"
                   if self.shuffle else "shuffle off - back to the order the DJ built")
        return self.shuffle

    def status(self) -> dict:
        pos, dur = self.player.progress() if self.player else (0.0, 0.0)
        # deliberately read-only: --status / the daemon poll this, and it used to
        # overwrite last_pos, which is the number the like/skip "heard N%" judgement
        # is computed from. An observer must not change what it observes.
        return {
            "now_playing": self.current,
            # why nothing new is coming, when nothing new is coming: "finished"
            # or "no stream would start". Empty while playing.
            "idle": self.idle,
            "backend": self.backend,
            "paused": self.paused,
            "position": round(pos, 1),
            "duration": round(dur, 1),
            "request": self.request,
            "queued": len(self.queue),
            "up_next": [dict(t, cached=bool(t.get("id") and
                                             audiocache.path_for(str(t["id"]))))
                       for t in self.queue.upcoming(5)],
            "cache": list(audiocache.brief()),
            "engine": self.info.get("engine"),
            "queries": self.info.get("queries", []),
            "auto": self.auto,
            "station": self.station,
            "repeat": self.repeat,
            "shuffle": bool(self.shuffle),
        }

    def _track_ended(self, pos: float, dur: float) -> str:
        """
        Why the loop should move on from what is loaded, or "" to stay put.

        The reason comes back rather than a bare bool because the log needs it: "the
        song ended" and "the song sat still" are different complaints, and only one of
        them is the player's fault.

        The player is asked first, because only it can tell "ended" from "buffering"
        from "the user paused it"; the old `dur and pos >= dur - 1.0 or eof()` test was
        the whole bug - a real mpv without `--keep-open` clears `eof-reached` the moment
        it sets it and reports (0, 0) afterwards, so the condition was never true and
        the DJ sat at the end of a finished song with 24 tracks in the queue. The
        watchdog is the last resort for a stream that neither ends nor advances.
        """
        pl = self.player
        if pl is None:
            return False
        fin = getattr(pl, "finished", None)
        if callable(fin):
            try:
                over = fin()
            except Exception as e:
                self._unreadable(e)             # a broken read is not an end of song
                over = False
            else:
                self._ended_errors = 0
            if over:
                return "ended"
        else:
            try:
                eof = getattr(pl, "eof", None)
                over = bool(eof()) if callable(eof) else False
            except Exception as e:
                self._unreadable(e)
                over = False
            else:
                self._ended_errors = 0
            if over:
                return "ended"
            if dur and pos >= dur - 1.0:
                return "ended"
        return "watchdog" if self._stall >= self.STALL_SECONDS else ""

    def _unreadable(self, e: Exception) -> None:
        """
        Say something about a player that cannot be read, once per burst.

        Skipping the tick is right for a socket that is busy - the next one asks
        again - and is exactly the old silence if the player object is broken for
        good. Two notes, 60 ticks apart: enough to read as a warning, not enough to
        fill a log at 1.3 lines a second.
        """
        self._ended_errors += 1
        if self._ended_errors not in (1, 60):
            return
        self._note(f"cannot read the player ({e.__class__.__name__}: {e}) - "
                   "watching for a stalled position instead"
                   + (", and it has not answered for a minute"
                      if self._ended_errors == 60 else ""))

    def _watch_position(self, pos: float) -> None:
        """Feed the watchdog, and never let a pause or a seek look like a stall."""
        if pos and pos == self._tick_pos:
            self._stall += self.TICK_SECONDS
        else:
            self._stall = 0.0
        self._tick_pos = pos
        if self._stall:
            try:
                probe = getattr(self.player, "is_playing", None)
                if callable(probe) and not probe():
                    self._stall = 0.0          # paused by hand is not stuck
            except Exception:
                pass

    def _explain_handoff(self) -> None:
        """
        Say once why nothing is advancing when mpv is not the one making the sound.

        `--backend spotube` (and `--headless`) deliberately leave the queue alone - the
        app cannot see another player's position - which is correct but invisible, so a
        listener was left to conclude the DJ was broken.
        """
        if self._handoff_noted or not self.current:
            return
        if time.time() - float(self.started_at or 0.0) < 20:
            return
        self._handoff_noted = True
        self._note("this queue will not advance itself: playback belongs to "
                   "Spotube/the browser, which the DJ cannot read - press next there, "
                   "or run with --backend mpv and it advances on its own")

    def _tick(self) -> float:
        """One pass of the auto-DJ loop; returns how long to wait before the next.

        A method rather than inline `while` body, for the same reason the filters are:
        a loop a test cannot call once is a loop nobody can prove advances at all.
        """
        if self.paused:
            return 0.5
        if not self.current:
            if not self.next():
                return 1.0
            return self.TICK_SECONDS
        pl = self.player
        if pl is None:
            # Someone else is making the sound (Spotube via --backend spotube), or
            # nobody is (--headless). We must not advance a track we cannot hear, but
            # the queue still has to stay full, because whoever IS playing will reach
            # the end of it. That `self.next()` this branch used to call in headless
            # mode just burned through the queue at 5 tracks a second.
            if self.current:
                self._topup()
                self._explain_handoff()
                return 0.5
            self.next()
            return 0.4
        pos, dur = pl.progress()
        if pos:
            self.last_pos = pos
        self._watch_position(pos)
        why = self._track_ended(pos, dur)
        if why:
            self._stall, self._tick_pos = 0.0, None
            if why == "watchdog":
                # without this line a skip looks like the DJ losing its taste
                self._note(f"{self._label(self.current)} sat still for "
                           f"{int(self.STALL_SECONDS)}s - moving on")
            self.next()
        return self.TICK_SECONDS

    def run(self) -> None:
        """Blocking auto-DJ loop: play till the end, learn, top up, repeat."""
        try:
            while not self._stop.is_set():
                try:
                    wait = self._tick()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    # A single bad tick must not end the loop. It used to: an
                    # exception here killed the thread, the page went on showing the
                    # last track with a full queue nothing would ever advance, and the
                    # reason was in a stderr no menu launcher keeps.
                    self._note(f"the DJ loop hit {e.__class__.__name__}: {e} - carrying on")
                    self._stall = 0.0
                    wait = 1.0
                if wait and self._stop.wait(wait):
                    break
        except KeyboardInterrupt:
            self._note("stopping")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self.player:
            try:
                self.player.stop()
                self.player.quit()
            except Exception:
                pass

    # ------------------------------------------------------------- controls
    def serve(self, port: int = 8765) -> None:
        dj = self

        def _station(idx) -> dict:
            """`?action=station&i=2`: build a station from the 3rd row of Up Next."""
            rows = dj.queue.items[dj.queue.pos:] or ([dj.current] if dj.current else [])
            try:
                i = max(0, min(int(idx), len(rows) - 1))
            except (TypeError, ValueError):
                i = 0
            return dj.radio_from(rows[i] if rows else None)

        class H(BaseHTTPRequestHandler):
            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = urlparse(self.path).path
                if path == "/status":
                    self._send(200, dj.status())
                elif path == "/log":
                    self._send(200, {"log": dj.log[-40:]})
                else:
                    self._send(404, {"error": "not found", "routes": [
                        "/status", "/control?action=next|prev|like|skip|pause|resume|topup|stop|seek|auto|mix"
                        "  (station: ?action=station&i=<row in Up Next>)",
                        "/request?q=..."]})

            def do_POST(self):  # noqa: N802
                path = urlparse(self.path).path
                q = parse_qs(urlparse(self.path).query)
                if path == "/control":
                    action = (q.get("action") or [""])[0]
                    # next/prev are typed by a person: force past the auto-retry
                    # cooldown, or `spotube-dj next` silently does nothing for 45s.
                    fn = {"next": lambda: dj.next(force=True), "prev": dj.prev,
                          "skip": dj.skip, "like": dj.like,
                          "pause": dj.pause, "resume": dj.resume,
                          "topup": lambda: dj._topup(force=True), "stop": dj.stop,
                          # the two "no words needed" verbs the page also has
                          "mix": dj.taste_mix,
                          "station": lambda: _station((q.get("i") or ["0"])[0]),
                          # the seek bar and the keep-mixing switch, for a terminal,
                          # a remote, or anything else that speaks the control API
                          "seek": lambda: dj.seek(float((q.get("secs") or ["0"])[0])),
                          "auto": lambda: dj.set_auto(
                              (q.get("on") or ["1"])[0] not in ("0", "off", "false")),
                          "shuffle": dj.toggle_shuffle,
                          # no mode (or mode=cycle) steps off -> all -> one, which is
                          # what the transport button and a terminal user both want
                          "repeat": lambda: dj.set_repeat((q.get("mode") or ["cycle"])[0]),
                          }.get(action)
                    if not fn:
                        self._send(400, {"error": f"unknown action {action!r}"})
                        return
                    try:
                        fn()
                    except Exception as e:      # never let one action kill the request
                        self._send(500, {"error": f"{e.__class__.__name__}: {e}"})
                        return
                    self._send(200, dj.status())
                elif path == "/request":
                    text = (q.get("q") or [""])[0]
                    if not text:
                        self._send(400, {"error": "q= required"})
                        return
                    try:
                        res = dj.start(text, count=int((q.get("count") or ["20"])[0]))
                        self._send(200, {"ok": res["ok"], "info": res["info"],
                                         "status": dj.status()})
                    except Exception as e:
                        self._send(500, {"error": f"{e.__class__.__name__}: {e}"})
                elif path == "/volume":
                    dj.volume(int((q.get("pct") or ["70"])[0]))
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})

            def log_message(self, *a):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self._note(f"control API on http://127.0.0.1:{port}  (GET /status, POST /control?action=next)")
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()


if __name__ == "__main__":
    import sys
    d = DJ(backend="none", headless=True)
    r = d.start(" ".join(sys.argv[1:]) or "chill lofi for late night coding", count=12)
    print(json.dumps({"info": r["info"], "first5": [
        {"t": t["title"][:50], "a": t["artist"][:24], "score": t.get("score")} for t in r["tracks"][:5]]},
        indent=2, ensure_ascii=False))
