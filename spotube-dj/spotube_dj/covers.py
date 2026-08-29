"""
covers.py - real album artwork from the Cover Art Archive, for the now-playing
square and the list rows.

YouTube's thumbnail is a *frame of the video*, so the "album art" the app had
until now was often a screenshot, a lyric card, or the uploader's face. The
Cover Art Archive (https://musicbrainz.org/doc/Cover_Art_Archive/API) serves the
actual release art, and unlike most cover APIs it is free, keyless and has no
rate limiting rules of its own.

What the docs do require is on the *lookup* side - MusicBrainz itself - and both
rules are obeyed here because breaking them gets an IP banned:

  * "each of their client applications never make more than ONE call per second"
    -> _pace() gates every musicbrainz.org request (coverartarchive.org is
    exempt: "There are currently no rate limiting rules in place").
  * "you must have a meaningful user-agent string"
    -> USER_AGENT below; set SPOTUBE_DJ_MBUA to your own
    "App/1.0 ( https://yourpage ; you@example.com )" if you fork this.

So the cost of a cover is at most two MusicBrainz calls, and only for tracks
this machine has never seen: the answer - including "this has no cover art" -
goes into ~/.spotube-dj/covers.json and is reused for a month. Negative caching
is not pessimism, it is what keeps 1 request/second sufficient: a list of 12
unloved rows would otherwise burn 24 calls on every search.

Nothing here is allowed to block or raise. `attach()` only queues; the pixels
land on the GUI's next art pass, and the image conversion and disk cache are
thumbs.py's job, not this file's.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import thumbs

CAA_HOST = "https://coverartarchive.org"
MB_HOST = "https://musicbrainz.org/ws/2"

# MusicBrainz: one call per second, per application. A hair over a second so a
# slow response can never squeeze two calls into the same second.
MIN_INTERVAL = 1.05
# The Archive says it has "no rate limiting rules in place", so only the search
# is paced to the letter of its documentation. A small courtesy gap on images is
# ours to keep: 12 rows x 2 sizes should not all fire in the same millisecond.
PACE = {"musicbrainz.org": MIN_INTERVAL, "coverartarchive.org": 0.25}
# CAA serves these thumbnails; docs: /release/{mbid}/front-(250|500|1200)
SIZE_BY_KIND = {"row": 250, "big": 500}
TTL = 30 * 86400          # a resolved album does not change
TTL_MISS = 3 * 86400      # but "no cover yet" gets revisited fairly soon
_TIMEOUT = 8.0
_MAX_BYTES = 2_000_000
TTL_THROTTLE = 120        # "we do not know yet", retried in two minutes
# MusicBrainz publishes its throttle state in the error headers; honour it.
_RATE_COOLDOWN = 30.0

USER_AGENT = (os.environ.get("SPOTUBE_DJ_MBUA")
              or "spotube-dj/1.0 (free-tier local DJ; +https://github.com/"
                 "schultz-dev0/SpotifyDJ)").strip()

_lock = threading.Lock()
_last_call = {"musicbrainz.org": 0.0, "coverartarchive.org": 0.0}
_rate_limited_until = 0.0
_retry_after = 0.0
_index: dict | None = None
_index_mtime = 0.0
_queue: list = []
_thread: threading.Thread | None = None
_start_lock = threading.Lock()     # only ever taken to create/replace _thread
_stop = threading.Event()
_notifier = None
_stats = {"lookups": 0, "hits": 0, "found": 0, "misses": 0, "errors": 0,
          "skipped": 0, "throttled": 0, "images": 0}
_last_error = ""     # why the last lookup failed, "" if it did not
_answered = True     # did the service actually reply? no reply != no cover art

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                   r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ------------------------------------------------------------------ switches
def enabled() -> bool:
    """Covers are opt-out, and useless without something to decode the JPEG."""
    if (os.environ.get("SPOTUBE_DJ_COVERS") or "").strip().lower() in (
            "0", "off", "no", "false"):
        return False
    if (os.environ.get("SPOTUBE_DJ_ART") or "").strip().lower() in (
            "0", "off", "no", "false"):
        return False
    return True


def row_mode() -> bool:
    """
    Look up art for every visible row, not just the record being played.

    Off by default because of the rule that starts this file: a lookup is at
    most two MusicBrainz calls and they must not come faster than one per
    second, so a 12-row page would take ~24 s of pacing to dress. Cache hits are
    free, which is why the second time you see that list it is instant.
    Set SPOTUBE_DJ_COVERS=rows to opt in.
    """
    return (os.environ.get("SPOTUBE_DJ_COVERS") or "").strip().lower() in (
        "rows", "all", "every")


def set_notifier(fn) -> None:
    """
    `fn(video_id, kind, path)` is called on the covers thread when art lands, so a
    front end can repaint the row without polling. Kept as a callback precisely so
    this module never has to know who is drawing anything.
    """
    global _notifier
    _notifier = fn


def answered() -> bool:
    """
    Whether the last lookup got a reply. The distinction is the whole point of
    caching carefully: "MusicBrainz says this album has nothing" can be believed
    for a month; "MusicBrainz was busy" must not be believed for a minute.
    """
    return _answered


def last_error() -> str:
    """
    What went wrong on the last lookup, or "" when MusicBrainz answered and the
    answer was simply "nothing". "no cover exists" and "the server was busy" are
    different problems and must never print as the same message.
    """
    return _last_error


def _num(value) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def stats() -> dict:
    with _lock:
        out = dict(_stats)
    out["pending"] = len(_queue)
    out["cached"] = len(_load_index())
    out["rate_limited"] = time.time() < _rate_limited_until
    out["user_agent"] = USER_AGENT
    return out


# ------------------------------------------------------------------- surface
def key_for(artist: str, what: str) -> str:
    from taste import norm
    return f"{norm(artist or '')}|{norm(what or '')}"


def album_of(track: dict) -> str:
    """The album name if the source knew it (Spotify seeding), else ''."""
    alb = track.get("album")
    if isinstance(alb, dict):
        alb = alb.get("name")
    return str(alb or "").strip()


def attach(track: dict) -> str:
    """
    Make artwork available for `track` as soon as it can be. Returns the local
    PNG path if one already exists, else "" after queuing a lookup.

    Sets track["cover_url"], which is what tells thumbs.py to fetch the release
    art instead of the YouTube frame.
    """
    if not track or not enabled():
        return ""
    vid = str(track.get("id") or "")
    if not vid:
        return ""
    done = _ready_path(track)
    if done:
        return done
    entry = _known_entry(track)
    if entry:
        use(track, str(entry.get("mbid") or ""), str(entry.get("flavour") or "release"))
        return _ready_path(track)
    remember_track(track)
    _enqueue(track)
    return ""


def _ready_path(track: dict) -> str:
    for kind in ("big", "row"):
        path = thumbs.cached_path(track, kind)
        if path:
            return path
    return ""


def known(track: dict) -> bool:
    """Whether we already have an answer (good or bad) for this track."""
    return bool(track.get("cover_mbid") or _known_entry(track))


def use(track: dict, mbid: str, flavour: str = "release") -> None:
    """Point a track at a MusicBrainz release and queue the actual download."""
    if not track or not mbid:
        return
    track["cover_url"] = caa_url(mbid, "big", group=flavour == "group")
    track["cover_mbid"] = mbid
    track["cover_flavour"] = flavour
    _start()


def caa_url(mbid: str, kind: str = "big", group: bool = False) -> str:
    """
    https://musicbrainz.org/doc/Cover_Art_Archive/API -> /release/{mbid}/front-250
    A 307 to the image follows; urllib takes it for us.
    """
    px = SIZE_BY_KIND.get(kind, SIZE_BY_KIND["big"])
    return f"{CAA_HOST}/{'release-group' if group else 'release'}/{mbid}/front-{px}"


def resolve(artist: str, what: str, album: str = "") -> str:
    """
    -> MusicBrainz id for this track/album, or "". Blocking: for the CLI and
    tests; the GUI goes through attach() and never waits on this.
    """
    return _resolve(artist or "", what or "", album or "")[0]


def resolve_blocking(artist: str, what: str, album: str = "",
                     budget: float = 30.0) -> tuple[str, str]:
    """
    -> (mbid, "release"|"group"), for `--cover`. Worth waiting out, because "the
    search quota is busy" is a *temporary* answer and a person at a terminal can
    afford to sit through it - the GUI cannot, so it never does.

    The important part is what it waits *for*: the cooldown this module sets when
    it is put in time-out. Retrying at fixed one-second intervals just spends the
    whole budget on calls that are refused before they are made (it did, once).
    """
    t0 = time.monotonic()
    attempt = 0
    mbid, flavour = "", "release"
    while True:
        hold = _rate_limited_until - time.time()
        if hold > 0:
            if time.monotonic() - t0 + min(hold, 1.0) > budget:
                return mbid, flavour          # tell them to come back, do not hang
            time.sleep(min(hold, 1.0))
            continue
        attempt += 1
        mbid, flavour = _resolve(artist, what, album, fresh=attempt > 1)
        if mbid or _answered:
            return mbid, flavour              # a real miss is a real answer
        if time.monotonic() - t0 > budget:
            return mbid, flavour


# ------------------------------------------------------------------ the queue
def _enqueue(track: dict) -> None:
    artist = str(track.get("artist") or "")
    album = album_of(track)
    what = album or str(track.get("title") or "")
    if not (artist and what):
        _bump("skipped")
        return
    key = key_for(artist, what)
    with _lock:
        if any(item[0] == key for item in _queue):
            return
        _queue.append((key, artist, what, album, str(track.get("id") or "")))
    _start()


def _start() -> None:
    global _thread
    with _start_lock:
        t = _thread
        # `is_alive()` alone is not enough: a thread that was just stop()ed is
        # still alive for up to one poll interval, and returning early here used to
        # leave the queue with nobody serving it (a restarted Bridge then silently
        # got no artwork, which is exactly the kind of bug a test finds and a
        # person does not).
        if t is not None and t.is_alive() and not _stop.is_set():
            return
        if t is not None:
            t.join(timeout=0.9)          # the old one exits within its poll
        _stop.clear()
        _thread = threading.Thread(target=_worker, name="covers", daemon=True)
        _thread.start()


def stop(wait: bool = False) -> None:
    """
    Stop the lookup thread, and drop what it had not got to yet.

    Pending work survives across a Bridge in this process (the settings window
    makes a second one, and tests make many), and a queue of lookups for rows
    that have scrolled away starves the one track that is actually playing -
    one MusicBrainz call per 1.05 s means twenty stale rows are twenty seconds of
    nobody getting art. Restarted, this service starts from nothing.
    """
    _stop.set()
    with _lock:
        del _queue[:]
    if wait and _thread is not None:
        _thread.join(timeout=5)


def _worker() -> None:
    while not _stop.is_set():
        item = None
        with _lock:
            if _queue:
                item = _queue.pop(0)
        if item is None:
            # 0.2s, not 0.5s: nothing else waits on this thread,
            # but a restart of the GUI (or a test) does join it, and a slow exit
            # there is a slow first picture for the person.
            _stop.wait(0.2)
            continue
        key, artist, what, album, _vid = item
        try:
            mbid, flavour = _resolve(artist, what, album)
        except Exception:
            mbid, flavour = "", "release"
            _bump("errors")
        if not mbid and not _answered:
            # "no answer" is not "no cover". Park a short marker so the next paint
            # does not re-burn a search-quota call, and let the cool-off the
            # server asked for actually pass.
            _remember(key, "", "release", throttled=True)
            _stop.wait(min(max(_retry_after, 1.0), 8.0))
            continue
        _remember(key, mbid, flavour)
        if not mbid:
            _bump("misses")
            continue
        _dress(key, mbid, flavour)


def _dress(key: str, mbid: str, flavour: str = "release") -> int:
    """
    Give every track we have seen from this album its own cached image, and hand
    each finished file to the GUI under *that track's* id - two rows can share an
    album, but never a cache slot.
    """
    done = 0
    for t in _tracks_with_key(key):
        use(t, mbid, flavour)
        vid = str(t.get("id") or "")
        for kind in ("big", "row"):
            url = caa_url(mbid, kind, group=flavour == "group")
            path = thumbs.download_url(url, t, kind)
            if not path:
                continue
            done += 1
            _bump("images")
            if _notifier and vid:
                try:
                    _notifier(vid, kind, path)
                except Exception:
                    pass
    return done


_seen: dict = {}      # key -> [weak-ish track refs], so a lookup serves all rows


def _tracks_with_key(key: str) -> list:
    out = [t for t in _seen.get(key, []) if t is not None]
    return out or []


def remember_track(track: dict) -> None:
    """Let one album lookup fill every row that shows a track from it."""
    if not track:
        return
    artist = str(track.get("artist") or "")
    album = album_of(track)
    for what in (album, str(track.get("title") or "")):
        if artist and what:
            _seen.setdefault(key_for(artist, what), [])
            bucket = _seen[key_for(artist, what)]
            if not any(t is track for t in bucket):
                bucket.append(track)
                del bucket[:-8]          # bounded; art is disposable


# --------------------------------------------------------- musicbrainz calls
def _pace(host: str) -> bool:
    """Sleep so no host sees two calls in the same second. False if we must not.
    The cooldown a throttle sets applies to the *search* only: that is what
    MusicBrainz's shared bucket protects, and it must not also hold back an
    image we already earned from the Archive.
    """
    global _rate_limited_until
    if host == "musicbrainz.org" and time.time() < _rate_limited_until:
        return False
    step = PACE.get(host, 0.0)
    if not step:
        return True
    with _lock:
        last = _last_call.get(host, 0.0)
        wait = step - (time.monotonic() - last)
        _last_call[host] = time.monotonic() + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)
    return True


def _get_json(url: str, host: str) -> dict | None:
    global _last_error, _rate_limited_until, _retry_after, _answered
    _answered = True
    if not _pace(host):
        # we are cooling off, so *we* did not ask - which must not be mistaken
        # for "the server answered and there is no such album" (that verdict is
        # cached for days; this one is not cached at all)
        _bump("skipped")
        _last_error = ("not asked: MusicBrainz's shared search quota put us in "
                       f"time-out for another {int(_rate_limited_until - time.time())}"
                       "s (this is not your app hammering it; try again in a minute)")
        _answered = False
        return None
    try:
        # no Accept header on purpose: fmt=json is already in the URL, and the
        # docs say fmt wins anyway ("if both are set, fmt= takes precedence")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            raw = r.read(_MAX_BYTES)
        _last_error = ""
        return json.loads(raw.decode("utf-8", "replace")) or {}
    except urllib.error.HTTPError as e:
        # 503 here is the shared search limiter (x-ratelimit-zone: search-global,
        # one bucket for every client of MusicBrainz), not our own pacing - the
        # body literally says "currently busy". It is temporary, so cool off for
        # the seconds the server asked for and cache *nothing* about the album.
        head = {str(k).lower(): str(val) for k, val in (e.headers or {}).items()}
        _answered = False
        _last_error = f"musicbrainz.org answered {e.code}"
        if e.code in (429, 503):
            wait = _num(head.get("retry-after")) or _RATE_COOLDOWN
            _retry_after = min(max(wait, 2.0), 120.0)
            _rate_limited_until = time.time() + _retry_after
            left = head.get("x-ratelimit-remaining")
            _last_error += (f" (search quota busy, {left} calls left in that "
                            f"window - retrying in {int(_retry_after)}s)"
                            if left else f" (busy, retrying in {int(_retry_after)}s)")
            _bump("throttled")
        elif e.code == 404:
            _last_error = ""      # a 404 from the Archive is a real "no art"
        return None
    except Exception as e:
        _last_error = f"no answer from musicbrainz.org ({e.__class__.__name__})"
        _answered = False
        return None


def _resolve(artist: str, what: str, album: str,
             fresh: bool = False) -> tuple[str, str]:
    """-> (mbid, "release"|"group"). Empty mbid + empty last_error() means the
    Archive genuinely has nothing for this; anything else means we did not get
    far enough to know."""
    global _answered
    key = key_for(artist, what)
    cached = _load_index().get(key)
    if isinstance(cached, dict) and not fresh:
        mbid = _entry_mbid(cached)
        age = time.time() - float(cached.get("seen") or 0)
        throttled = bool(cached.get("throttled"))
        ttl = TTL_THROTTLE if throttled else (TTL if mbid else TTL_MISS)
        if throttled and age < ttl:
            # a hold, not a verdict: nothing was learned last time, so say so and
            # let the next paint try again rather than burning another quota call
            _answered = False
            _bump("hits")
            return "", _entry_flavour(cached)
        if not throttled and age < ttl:
            _answered = True
            _bump("hits")
            return mbid, _entry_flavour(cached)
        # expired, or a hold that ran out. It has to go, or the code below would
        # still find it and answer from the stale value (it did, once).
        _load_index().pop(key, None)
    _bump("lookups")
    _answered = True
    mbid, flavour = "", "release"
    if album:
        got = _release_group_mbid(artist, album)
        if got:
            mbid, flavour = got, "group"
    if not mbid:
        mbid = _recording_release_mbid(artist, what) or ""
    if _answered:
        _remember(key, mbid, flavour)
        _bump("found" if mbid else "misses")
    return mbid, flavour


def _entry_mbid(entry: dict) -> str:
    return str(entry.get("mbid") or entry.get("release") or "")


def _entry_flavour(entry: dict) -> str:
    return "group" if str(entry.get("flavour") or "") == "group" else "release"


def _release_group_mbid(artist: str, album: str) -> str:
    """
    One call: search release-groups, then let CAA pick the front image of the
    best release in that group (docs §7, /release-group/{mbid}/front).
    """
    # `artist:` - measured, not guessed: `artist-name:` on a release-group query
    # makes MusicBrainz answer 503 "server currently busy" for every search.
    q = f'release-group:"{_lucene(album)}" AND artist:"{_lucene(artist)}"'
    data = _get_json(f"{MB_HOST}/release-group?query={urllib.parse.quote(q)}"
                     f"&limit=5&fmt=json", "musicbrainz.org")
    if not data:
        return ""
    groups = [g for g in (data.get("release-groups") or []) if g.get("id")]
    if not groups:
        return ""
    from taste import norm
    want = norm(album)
    # results come back ranked by Lucene score, which happily puts
    # "Random Access Memories: The Collaborators" first; an exact title is worth
    # more than a score, so take that when it is in the list at all
    for g in groups:
        if norm(str(g.get("title") or "")) == want:
            return str(g["id"])
    # then prefer the things that look like a real album, because that is what
    # has cover art worth showing (no undocumented type=a|b|c filter)
    for wanted in ("Album", "EP", "Single"):
        for g in groups:
            if str(g.get("primary-type") or "") == wanted:
                return str(g["id"])
    return str(groups[0]["id"])


def _recording_release_mbid(artist: str, title: str) -> str:
    """
    Two calls, only when no album name came with the track: find the recording,
    then ask it which release it appears on (Lookups: ?inc=releases).
    """
    q = f'recording:"{_lucene(title)}" AND artist:"{_lucene(artist)}"'
    data = _get_json(f"{MB_HOST}/recording?query={urllib.parse.quote(q)}"
                     f"&limit=1&fmt=json", "musicbrainz.org")
    recs = (data or {}).get("recordings") or []
    if not recs:
        return ""
    rid = str(recs[0].get("id") or "")
    if not _UUID.match(rid):
        return ""
    data = _get_json(f"{MB_HOST}/recording/{rid}?inc=releases&fmt=json",
                     "musicbrainz.org")
    if not data:
        return ""
    rels = [r for r in (data.get("releases") or []) if r.get("id")]
    if not rels:
        return ""
    return str(_pick_release(rels)[0]["id"])


def _pick_release(rels: list) -> list:
    """
    Rank a recording's releases so the artwork is the one a listener would
    recognise: an official release over a bootleg, and the earliest dated one of
    those - a song that appears on 400 compilations otherwise returns whatever
    MusicBrainz happened to list first, which is how "A Horse With No Name" came
    back with a 2005 live-album cover instead of its own.
    """
    def key(r: dict) -> tuple:
        date = str(r.get("date") or "")
        # a *missing* status is common in compact results and means nothing;
        # only an explicit non-official label demotes a release
        official = 1 if str(r.get("status") or "") in ("Bootleg", "Pseudo-Release") else 0
        return (official, date or "9999-99-99", str(r.get("title") or ""))
    return sorted(rels, key=key)


_LUCENE = re.compile(r'[+\-!(){}\[\]^~*?:/\\]|AND\b|OR\b', re.I)


def _lucene(s: str) -> str:
    """Enough escaping that a song called "Why? (Don't Buy It)" cannot error."""
    out = _LUCENE.sub(" ", str(s or ""))
    return re.sub(r"\s+", " ", out).strip()


# ------------------------------------------------------------------ the index
def index_path():
    return config.APP_DIR / "covers.json"


def _load_index() -> dict:
    global _index, _index_mtime
    path = index_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if _index is None:
            _index = {}
        return _index
    if _index is None or mtime != _index_mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _index = data if isinstance(data, dict) else {}
        except Exception:
            _index = {}
        _index_mtime = mtime
    return _index


def _remember(key: str, mbid: str, flavour: str = "release",
              throttled: bool = False) -> None:
    if not key:
        return
    index = _load_index()
    index[key] = {"mbid": mbid or "", "flavour": flavour or "release",
                  "seen": int(time.time()), "throttled": bool(throttled)}
    global _index
    _index = index
    try:
        config.ensure_dirs()
        tmp = index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(index_path())
    except OSError:
        pass


def _known_entry(track: dict) -> dict | None:
    """The index entry for this track, if we have one that is still fresh."""
    mbid = str(track.get("cover_mbid") or "")
    if mbid:
        return {"mbid": mbid, "flavour": str(track.get("cover_flavour") or "release")}
    artist = str(track.get("artist") or "")
    album = album_of(track)
    for what in (album, str(track.get("title") or "")):
        if not (artist and what):
            continue
        entry = _load_index().get(key_for(artist, what))
        if not isinstance(entry, dict):
            continue
        if entry.get("mbid") is None and entry.get("release") is None:
            continue
        age = time.time() - float(entry.get("seen") or 0)
        if entry.get("throttled"):
            if age < TTL_THROTTLE:
                return None          # in cool-off: do not re-queue it per paint
            return None
        if age < (TTL if _entry_mbid(entry) else TTL_MISS):
            _bump("hits")
            return entry
        _enqueue(track)          # stale: re-ask, in the background
        return None
    return None


def _bump(name: str) -> None:
    with _lock:
        _stats[name] = _stats.get(name, 0) + 1


def doctor_check() -> tuple:
    """One (label, ok, detail) line for --doctor."""
    if not enabled():
        return ("cover art  (Cover Art Archive)", False,
                "off - SPOTUBE_DJ_COVERS=off is set (ffmpeg is optional: Tk decodes "
                "PNG/GIF/JPEG itself; ffmpeg only adds exact-size scaling and WebP)")
    s = stats()
    detail = (f"{s['found']} albums with art, {s['misses']} without, "
              f"{s['images']} images cached, {s['hits']} from a "
              f"{s['cached']}-entry index")
    if s["rate_limited"]:
        detail += "; rate limited, cooling off"
    if s["errors"]:
        detail += f"; {s['errors']} errors"
    if not s["cached"]:
        detail += f"; paces MusicBrainz to 1 call per {MIN_INTERVAL}s"
    return ("cover art  (Cover Art Archive)", True, detail)
if __name__ == "__main__":
    import sys

    artist = sys.argv[1] if len(sys.argv) > 1 else "America"
    what = sys.argv[2] if len(sys.argv) > 2 else "A Horse With No Name"
    album = sys.argv[3] if len(sys.argv) > 3 else ""
    print("enabled :", enabled(), "| row mode:", row_mode())
    print("user agent:", USER_AGENT)
    mbid, flavour = resolve_blocking(artist, what, album)
    print(f"mbid    : {mbid or '(none)'}  [{flavour}]")
    if not mbid and last_error():
        print("why     :", last_error())
    if mbid:
        url = caa_url(mbid, "big", group=flavour == "group")
        print("caa url :", url)
        fake = {"id": "probe", "artist": artist, "title": what, "cover_url": url}
        print("png     :", thumbs.get(fake, "big"))
    print("stats   :", stats())
