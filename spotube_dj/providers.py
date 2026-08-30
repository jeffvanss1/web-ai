"""
providers.py - the metadata + audio sources.

YouTube Music  : search + stream URLs. Free, no key, no OAuth. This is the
                 engine that makes Premium unnecessary.
Spotify (opt.) : metadata only - liked songs / playlist seeds. Uses the
                 Web API with whatever client id you have. We NEVER call
                 /me/player/* (that's the Premium-only family).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import filters

API = "https://api.spotify.com/v1"
AUTH = "https://accounts.spotify.com"
REDIRECT = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8899/callback")
CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "")
SCOPES = "user-read-currently-playing user-read-recently-playlist playlist-read-private user-top-read"
CACHE = Path.home() / ".spotube-dj" / ".spotify_token.json"

_YTDLP = [sys.executable, "-m", "yt_dlp"]


# ------------------------------------------------------------- youtube music
def _run(cmd: list[str], timeout: int) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        raise
    return p.stdout or ""


def _thumb_of(d: dict) -> str:
    """Best thumbnail URL from an yt-dlp entry (list form or singular)."""
    for key in ("thumbnail", "thumb"):
        v = d.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    rows = d.get("thumbnails")
    if isinstance(rows, list):
        best = ""
        for row in rows:
            u = row.get("url") if isinstance(row, dict) else None
            if isinstance(u, str) and u.startswith("http"):
                # prefer the largest that is still a small, fast file
                if "hqdefault" in u or "maxresdefault" in u:
                    return u
                best = best or u
        return best
    return ""


YTM_ENDPOINT = "https://music.youtube.com/youtubei/v1/search"
YTM_CLIENT = {"clientName": "WEB_REMIX", "clientVersion": "1.20250101.00.00",
               "gl": "US", "hl": "en"}


def ytm_enabled() -> bool:
    """
    The YouTube Music search endpoint is tried first: it can only answer with
    music, so a Donnie Yen fight scene or a 24/7 radio loop cannot appear at
    all. SPOTUBE_DJ_YTM=off skips it (or when urllib is unhappy) and the old
    yt-dlp search runs instead, still filtered.
    """
    return (os.environ.get("SPOTUBE_DJ_YTM") or "").strip().lower() not in (
        "0", "off", "no", "false")


def _http_json(url: str, body: dict, timeout: int = 20) -> dict | None:
    """One POST, JSON out. The seam every test stubs, so nothing here hits net."""
    import json as _json
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(
            url, data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) spotube-dj/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8", "replace")) or {}
    except Exception:
        return None


def _ytm_rows(data: dict) -> list[dict]:
    """
    Pull every musicResponsiveListItemRenderer out of an InnerTube response.

    The response shape is a tree of *Renderers* with no stable path to them, and
    YouTube has changed the wrapping twice in the years this kind of code has
    existed, so the walk is deliberately structural: find the rows, then read
    what they say, and let the caller decide if it got anything usable.
    """
    out: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "musicResponsiveListItemRenderer":
                    out.append(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data or {})
    return out


def _ytm_text(node) -> str:
    if isinstance(node, dict):
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(str(r.get("text") or "") for r in runs)
        if isinstance(node.get("text"), str):
            return node["text"]
    return ""


def _ytm_columns(row: dict) -> list[str]:
    cols = []
    for col in row.get("flexColumns") or []:
        renderer = col.get("musicResponsiveListItemFlexColumnRenderer") or {}
        cols.append(_ytm_text(renderer.get("text")))
    return [c for c in cols if c]


def _ytm_thumb(row: dict, vid: str) -> str:
    """
    The row's own artwork, biggest offered size, else a URL derived from the id.

    InnerTube puts the sizes under `musicThumbnailRenderer.thumbnail.thumbnails`
    and older responses spelled it `sources`; reading only the second one meant
    every row from the music search arrived art-less, and the panel drew a
    coloured initial for anything that had no MusicBrainz release to look up.
    A `Song` row's thumbnail is the album cover, which is the better picture for
    a now-playing card anyway, so it wins over the derived video frame.
    """
    node = ((row.get("thumbnail") or {}).get("musicThumbnailRenderer") or {})
    node = node.get("thumbnail") or {}
    cands = [c for c in (list(node.get("thumbnails") or [])
                         + list(node.get("sources") or []))
             if isinstance(c, dict) and str(c.get("url") or "").startswith("http")]

    def area(c):
        try:
            return int(c.get("width") or 0) * int(c.get("height") or 0)
        except (TypeError, ValueError):
            return 0
    # The search rows offer 60 and 120 px tiles, which for a `Song` is the
    # *artist avatar* (three different songs, one identical URL) - too small and
    # the wrong picture. Only take the row's own art when it is big enough to be
    # the cover, which is what the Album rows carry at 544 px.
    def clean(url: str) -> str:
        # YouTube's own URLs come signed (`?sqp=..&rs=..`) and a signed URL is one
        # that can expire; the thumbnail cache keeps the filename, not the fetch,
        # so strip it back to the plain form for the same picture.
        base = str(url or "").split("?", 1)[0]
        if "ytimg.com/vi/" in base and base.endswith((".jpg", ".png", ".webp")):
            return base
        return base

    best = max(cands, key=area) if cands else None
    if best is not None and (area(best) >= 160 * 160 or not (
            best.get("width") or best.get("height"))):
        # an advertised size under 160 px is the artist avatar, not the cover:
        # three different songs came back with one identical 120 px URL
        return clean(best["url"])
    if vid and vid.replace("-", "").replace("_", "").isalnum():
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return clean(best["url"]) if best else ""


def _ytm_artist(row: dict, title: str) -> str:
    """
    The artist, from the play button's accessibility label if nowhere else.

    InnerTube's `Song` rows sometimes carry "Song \u2022 Nirvana" in the visible
    column and sometimes only "Song \u2022 5:02" - no artist at all. A track with no
    artist name is a track whose like teaches the taste profile nothing, so the
    label ("Play <title> - <artist>") is the last place the name is written.
    """
    content = ((row.get("overlay") or {}).get("musicItemThumbnailOverlayRenderer") or {})
    play = (content.get("content") or {}).get("musicPlayButtonRenderer") or {}
    for key in ("accessibilityPlayData", "accessibilityPauseData"):
        label = ((play.get(key) or {}).get("accessibilityData") or {}).get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        label = re.sub(r"^\s*(?:Play|Pause)\s+", "", label.strip())
        for sep in (" - ", " \u2013 ", " \u2014 "):
            if sep in label:
                name = label.rsplit(sep, 1)[-1].strip()
                name = re.sub(r"\s+from\s+.*$", "", name).strip()
                if name and name.lower() != (title or "").lower():
                    return name
    return ""


def _is_origin(t: dict) -> bool:
    """
    True when a row is the artist's own recording, not a fan upload of it.

    A YTM `Song` row is the artist's catalog entry; the sparse ` - topic` channel is
    its official audio channel. A cover/remix/live upload is neither, and when a
    search returns both, the originals are the ones Apple Music would show.
    """
    if (t or {}).get("official"):
        return True
    ch = str((t or {}).get("channel") or (t or {}).get("uploader") or "")
    return bool(filters._TOP_CHANNEL.search(ch))


def _prefer_originals(rows: list[dict]) -> list[dict]:
    """
    Keep only the artist's own recordings when a search returned any.

    The complaint was the queue being filled with "whatever youtuber doing remix of
    that songs or doing cover of the original". The recording's catalog entry is the
    one a mood mix is for, so when at least one original exists the rest is dropped
    rather than ranked - a cover scoring -1.5 still makes the queue otherwise. A
    search that returns *only* covers/live keeps them, because silence is worse.
    """
    origin = [x for x in (rows or []) if _is_origin(x)]
    return origin if origin else list(rows or [])


def ytm_search(query: str, limit: int = 12) -> list[dict]:
    """
    Search YouTube Music proper. -> normalised track dicts (possibly empty).

    Two row shapes come back and they say different things: a *Song* row is the
    artist's own recording ("Song • Nirvana"), which is what a DJ should play, so
    it is tagged `official`; the other rows are ordinary videos inside the music
    catalog (a live bootleg, a cover band) and carry `artist • 62M views • 5:31`
    in the subtitle, from which the duration is read.
    """
    data = _http_json(YTM_ENDPOINT, {"context": {"client": YTM_CLIENT},
                                     "query": query})
    if not data:
        return []
    tracks: list[dict] = []
    for row in _ytm_rows(data):
        vid = (row.get("playlistItemData") or {}).get("videoId") or ""
        cols = _ytm_columns(row)
        if not vid or not cols:
            continue                      # an artist/album card, not a track
        title = cols[0]
        sub = cols[1] if len(cols) > 1 else ""
        parts = [x.strip() for x in re.split(r"\u2022|\u00b7|\|", sub) if x.strip()]
        official = bool(parts) and parts[0].startswith("Song")
        # "Song • Nirvana" -> the artist is after the bullet;
        # "Nirvana • 62M views • 5:31" -> the artist is before it.
        if official:
            # "Song • Nirvana • 5:02" and "Nirvana • 5:02" both occur depending on
            # the row kind, so take the first field that is a name: not the label
            # word, not a duration, not a play count.
            artist = next((x for x in parts[1:]
                           if not filters.parse_duration(x)
                           and not filters.parse_views(x)), "")
            channel = artist            # a Song row is the artist's own release
        else:
            # a video row's subtitle is `uploader • 6.6M views • 4:46`: the first
            # field is who uploaded it, not whose song it is. Calling it "artist"
            # would put "ScottishTeeVee" on the player and into the taste profile.
            channel = parts[0] if parts else ""
            artist = ""
        duration = 0
        views = 0
        for part in reversed(parts):
            if not duration:
                duration = filters.parse_duration(part)
            if not views:
                views = filters.parse_views(part)
        thumb = _ytm_thumb(row, vid)
        tracks.append({
            "id": vid,
            "source": "youtube-music",
            # which surface answered, for --doctor and for a bug report: the music
            # catalog and the plain video search can disagree, and the user
            # deserves to know which one gave them a fight scene
            "endpoint": "music-search",
            "url": f"https://music.youtube.com/watch?v={vid}",
            "title": title,
            # never the query: "lofi beats to relax" as an *artist* name goes
            # straight into the taste profile and comes back as a search later
            "artist": artist or _ytm_artist(row, title),
            "duration": duration,
            "channel": channel,
            "view_count": views,
            "thumbnail": thumb,
            "official": official,
        })
    return tracks[: max(limit * 3, limit)]


def yt_search(query: str, limit: int = 12, min_dur: int = 45, max_dur: int = 3600,
            verdicts: list | None = None) -> list[dict]:
    """
    Search for music. Returns normalised track dicts, junk-free.

    Order of preference, because each tier is a different kind of trustworthy:
      1. YouTube Music's own search (music only, by construction)
      2. plain YouTube via yt-dlp, then run through filters.decide
    Both are then gated on the same verdict, so a live broadcast, a movie clip, a
    karaoke cover or a 40-minute "vibes for studying" set cannot be played as a
    song - which is exactly what used to happen: an "Ip Man" search returned a
    fight scene, 214 seconds, verified channel, and every keyword rule waved it
    through.

    Each row's reasons are kept under `why`, so the GUI can log what it dropped
    instead of just "0 results". Pass a list as `verdicts` and every candidate -
    kept *and* refused - is appended as its full verdict; that is what `--why`
    reads, because printing only the survivors could never explain a refusal.
    """
    kept, dropped = [], []
    if ytm_enabled():
        for t in ytm_search(query, limit):
            v = filters.decide(t)
            t["why"] = v["reasons"]
            t["_score"] = v["score"]
            if verdicts is not None:
                verdicts.append(v)
            if v["kind"] != filters.TRACK:
                dropped.append(v)
                continue
            dur = t.get("duration") or 0
            if dur and dur < min_dur:
                t["short"] = True         # YTM Song rows carry no length at all
            kept.append(t)
        kept.sort(key=lambda t: -float(t.get("_score") or 0))
        # demote, then drop, anything the filter could not place on the music
        # surface: a story about a song only reaches the queue if nothing else did
        strong = [x for x in kept if "not shaped like a track"
                  not in (x.get("why") or [])]
        if len(strong) >= min(3, limit):
            kept = strong
        # only the artist's own recordings when the search returned any: covers and
        # remixes were filling the queue ("whatever youtuber doing remix of that
        # songs or doing cover of the original"), so the originals win - even if
        # that means fewer than the fallback threshold, because a plain YouTube
        # sweep would only re-introduce the uploads we just removed.
        origin = _prefer_originals(kept)
        # Two or three acceptable rows from a music-only catalogue still beat a
        # slower, dirtier plain-YouTube sweep, so don't fall back just because a
        # sparse query only had three songs on its first page.
        if len(kept) >= min(max(1, limit // 3), 3):
            kept = origin
            for t in kept:
                t.pop("_score", None)
            return kept[: limit]
        kept = []

    raw = _run(_YTDLP + ["--flat-playlist", "--dump-json", "--no-warnings",
                         "--playlist-end", str(max(limit * 3, limit + 6)),
                         f"ytsearch{max(limit * 3, limit + 6)}:{query}"], timeout=180)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("id"):
            continue
        ch = d.get("channel") or d.get("uploader") or ""
        dur = d.get("duration") or 0
        t = {
            "id": d.get("id"),
            "source": "youtube-music",
            "endpoint": "ytsearch",
            "url": f"https://music.youtube.com/watch?v={d.get('id')}",
            "title": d.get("title") or "",
            "artist": ch,
            "duration": dur,
            "channel": ch,
            # art for the GUI list. flat-playlist sometimes omits it; the id is
            # enough to derive i.ytimg.com, and if both are missing the GUI draws
            # a coloured tile, so this is never a hard dependency.
            "thumbnail": _thumb_of(d),
            # the facts filters.decide reads; kept on the row so --list can show
            # why something was refused without re-running the search
            "is_live": d.get("is_live"),
            "live_status": d.get("live_status"),
            "was_live": d.get("was_live"),
            "concurrent_view_count": d.get("concurrent_view_count"),
            "availability": d.get("availability"),
            "channel_is_verified": d.get("channel_is_verified"),
            "description": d.get("description") or "",
            "view_count": d.get("view_count") or 0,
        }
        v = filters.decide(t)
        t["why"] = v["reasons"]
        t["_score"] = v["score"]
        if verdicts is not None:
            verdicts.append(v)
        if v["kind"] == filters.UNPLAYABLE or v["kind"] in (filters.SPEECH,
                                                             filters.NOTAUDIO,
                                                             filters.EVENT):
            dropped.append(v)
            continue
        if v["kind"] == filters.LONGFORM or (dur and dur > max_dur) or (dur and dur < min_dur):
            if v["kind"] == filters.LONGFORM or (dur and dur > max_dur):
                dropped.append(v)
                continue
        kept.append(t)

    kept.sort(key=lambda x: -float(x.get("_score") or 0))
    # Plain YouTube answers a music query with videos, so anything the filter
    # could not positively identify as a music upload waits behind everything it
    # could: a fight scene only reaches the queue when there is no music at all.
    strong = [x for x in kept if "not shaped like a track" not in (x.get("why") or [])]
    out = _prefer_originals(
        (strong if len(strong) >= min(3, limit) else kept))
    out = out[:limit]
    for t in out:
        t.pop("_score", None)
    if out:
        return out
    # Nothing ordinary survived. One long-form upload - the *shortest* one - beats
    # a silent speaker, but exactly one, so a desperate fallback can never turn a
    # DJ set into a 6-hour "chill lofi beats to study to" livestream archive.
    rescue = [e for e in dropped if e.get("kind") == filters.LONGFORM]
    rescue.sort(key=lambda e: -(e.get("duration") or 0))
    if rescue:
        best = rescue[-1]
        track = best.get("entry")
        if isinstance(track, dict):
            track = dict(track)
            track["longform"] = True
            track["why"] = ["nothing matched as a 3-8 minute track; played the "
                            "shortest long upload as a last resort"]
            return [track]
    return []


def yt_stream_url(video_id: str, fmt: str = "bestaudio[ext=m4a]/bestaudio/best") -> str | None:
    out = _run(_YTDLP + ["-f", fmt, "--get-url", "--no-warnings", "--no-playlist",
                         "--skip-download", f"https://music.youtube.com/watch?v={video_id}"],
               timeout=150)
    url = out.strip().splitlines()
    return url[0] if url else None


# a release year is the one line "album" adds to the credits. It is fetched
# best-effort on its own lane, never in the search path, so a slow or empty
# answer costs the panel a blank line and never a row.
def _release_year(d: dict) -> int | None:
    for key in ("release_year", "release_date", "upload_date", "year"):
        v = d.get(key)
        if isinstance(v, int) and 1900 <= v <= 2099:
            return v
        if isinstance(v, str):
            s = v.strip()
            # yt-dlp dates arrive as "19940822", "1994-08-22" or "1994"; take the
            # leading 4 digits so the year is read out of all three cleanly
            m = re.match(r"\s*(\d{4})", s)
            if m:
                y = int(m.group(1))
                if 1900 <= y <= 2099:
                    return y
            # otherwise a "Aug 22, 1994" style string: a 4-digit year on its own
            m = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", s)
            if m:
                return int(m.group(0))
    return None


def yt_track_meta(video_id: str) -> dict:
    """
    Best-effort album + release-year for one video, from yt-dlp's own metadata.

    A full (not `--flat-playlist`) yt-dlp query on a music video returns `album`,
    `release_date`/`release_year`, `track` and `artist`; the flat search path that
    fills the queue does not. This only runs once per track and is guarded so a
    missing album, a rate limit or no yt-dlp is a blank line, never a failure.
    Returns {} when nothing useful came back.
    """
    if not video_id:
        return {}
    out = _run(_YTDLP + ["--dump-json", "--no-warnings", "--no-playlist",
                         "--skip-download",
                         f"https://music.youtube.com/watch?v={video_id}"],
               timeout=25)
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        album = d.get("album") or ""
        if isinstance(album, dict):            # some providers nest it
            album = album.get("name") or album.get("title") or ""
        year = _release_year(d)
        meta = {}
        if album:
            meta["album"] = str(album)
        year = _release_year(d)
        if year:
            meta["release_year"] = year
        artist = d.get("artist") or d.get("creator") or d.get("uploader") or ""
        if artist:
            meta["artist"] = str(artist)
        track = d.get("track") or d.get("title") or ""
        if track:
            meta["track"] = str(track)
        # a reliable "album page" URL is rarely exposed, but a Music search for
        # "<album> <artist>" almost always lands on it - good enough for a
        # "See album" handoff that is one click, never a dead link
        if album:
            q = urllib.parse.quote(f"{album} {artist}".strip())
            meta["album_url"] = f"https://music.youtube.com/search?q={q}"
        return meta
    return {}


# --------------------------------------------------- browse (album/artist pages)
# The search endpoint answers with *songs*; a deep Artist / Album page needs the
# browse endpoint, which returns the tracks of one album and an artist's
# discography. Both reuse the same InnerTube row walkers as search, so the parsing
# is one shape and the rows are the same normalised dicts the queue uses.
YTM_BROWSE = "https://music.youtube.com/youtubei/v1/browse"


def ytm_browse(browse_id: str, params: str | None = None, timeout: int = 25) -> dict | None:
    """POST the browse endpoint for one browseId; -> the response dict or None."""
    if not browse_id:
        return None
    body: dict = {"context": {"client": YTM_CLIENT}, "browseId": str(browse_id)}
    if params:
        body["params"] = params
    return _http_json(YTM_BROWSE, body, timeout=timeout)


def _ytm_any_rows(data: dict) -> list[dict]:
    """
    Every track *or* card renderer in a response, in the order the page lists them.

    `_ytm_rows` only keeps `musicResponsiveListItemRenderer` (the song rows); the
    search endpoint answers an album/artist query with `musicTwoRowItemRenderer`
    *cards* too (an album/artist result is not a playable row), so a page builder
    needs both. Same structural walk, one extra key.
    """
    out: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in ("musicResponsiveListItemRenderer", "musicTwoRowItemRenderer"):
                    out.append(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data or {})
    return out


def _ytm_card_text(node, key: str) -> str:
    """The text of a `musicTwoRowItemRenderer` title/subtitle block."""
    sub = node.get(key) or {}
    runs = sub.get("runs") if isinstance(sub, dict) else None
    if isinstance(runs, list):
        return "".join(str(r.get("text") or "") for r in runs)
    if isinstance(sub, dict) and isinstance(sub.get("text"), str):
        return sub["text"]
    return ""


def _ytm_card_info(row: dict) -> tuple[str, str, str]:
    """
    -> (title, subtitle, browse_id) for any renderer row.

    `musicTwoRowItemRenderer` cards put the name in `title`/`subtitle`; the list
    items `_ytm_rows` already knows put it in the flex columns. Reading both means
    one card reader for the search result that happened to be a song or an album.
    """
    bid = ((row.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get(
        "browseId") or ""
    title = _ytm_card_text(row, "title")
    subtitle = _ytm_card_text(row, "subtitle")
    if not title:
        cols = _ytm_columns(row)
        if cols:
            title = cols[0]
            subtitle = cols[1] if len(cols) > 1 else ""
    return title.strip(), subtitle.strip(), bid


def _ytm_image_candidates(node: dict) -> list[tuple[int, str]]:
    """
    Every (area, url) in an InnerTube thumbnail subtree.

    `musicTwoRowItemRenderer.thumbnail` has been spelled `musicThumbnailRenderer`,
    `videoThumbnailRenderer`, a single `{url,width,height}` object, or the older
    `sources` list across the years. Reading one shape is exactly why a discography
    entry or an album row came back with no cover - the art was there, in a branch
    the parser was not looking at.
    """
    out: list[tuple[int, str]] = []
    if not isinstance(node, dict):
        return out
    def area(c):
        try:
            return int(c.get("width") or 0) * int(c.get("height") or 0)
        except (TypeError, ValueError):
            return 0
    url = node.get("url")
    if isinstance(url, str) and url.startswith("http") and "ytimg" in url:
        out.append((area(node), url))
    for key in ("thumbnails", "sources"):
        for c in node.get(key) or []:
            if not isinstance(c, dict):
                continue
            u = c.get("url")
            if isinstance(u, str) and u.startswith("http") and "ytimg" in u:
                out.append((area(c), u))
            out += _ytm_image_candidates(c)
    for key in ("thumbnail", "musicThumbnailRenderer", "videoThumbnailRenderer",
                "musicTwoRowItemRenderer", "musicMultiSelectItemRenderer"):
        sub = node.get(key)
        if isinstance(sub, dict):
            out += _ytm_image_candidates(sub)
    return out


def _ytm_card_thumb(row: dict) -> str:
    """
    The artwork of a `musicTwoRowItemRenderer` card (an album / artist result).

    Search rows' small tiles are the artist avatar; an Album card's art *is* the
    cover, so a discography entry and the album page can show it right away instead
    of a coloured initial until the cover lane catches up.
    """
    cands = _ytm_image_candidates(row.get("thumbnail") or {})
    if not cands:
        return ""
    best = max(cands, key=lambda c: c[0])
    # strip the signed query back so the URL is stable to cache/reuse
    return str(best[1]).split("?", 1)[0]


def _year_in(text) -> int | None:
    """The 4-digit year in a subtitle like 'Album • Portishead • 1994'."""
    m = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", str(text or ""))
    return int(m.group(0)) if m else None


def _clean_artist(name: str) -> str:
    """'Portishead songs' / 'Radiohead top songs' -> 'Portishead'."""
    name = str(name or "").strip()
    # longest first: "radiohead top songs" must lose " top songs", not " songs"
    for tail in (" top songs", " best songs", " songs", " music", " discography"):
        if name.lower().endswith(tail):
            return name[: -len(tail)].strip()
    return name


def _find_card_album(data: dict, album: str, artist: str) -> tuple[str, str, str, str]:
    """The album card's (title, subtitle, browseId, cover_url), or all blank."""
    want = str(album or "").strip().lower()
    best: tuple[str, str, str, str] = ("", "", "", "")
    for row in _ytm_any_rows(data):
        title, subtitle, bid = _ytm_card_info(row)
        if not bid:
            continue
        subtitle_l = subtitle.lower()
        # an album result is a browseable card, and its subtitle says "Album";
        # a raw "Album of the year" playlists row can too, so prefer the one whose
        # title actually matches what was asked for
        if "album" not in subtitle_l:
            continue
        thumb = _ytm_card_thumb(row)
        if want and (title.lower() in want or want in title.lower()):
            return title, subtitle, bid, thumb
        if not best[0] or "album" in subtitle_l:
            best = (title, subtitle, bid, thumb)
    if best and (not want or not best[0]):
        return best
    return best if best[0] else ("", "", "", "")


def _ytm_browse_tracks(data: dict, album: str = "", artist: str = "",
                       year: int | None = None, cover: str = "") -> list[dict]:
    """The rows of one album page (the tracklist) -> normalised track dicts."""
    tracks: list[dict] = []
    for row in _ytm_rows(data):
        vid = (row.get("playlistItemData") or {}).get("videoId") or ""
        cols = _ytm_columns(row)
        if not vid or not cols:
            continue
        title = cols[0]
        sub = cols[1] if len(cols) > 1 else ""
        parts = [x.strip() for x in re.split(r"\u2022|\u00b7|\|", sub) if x.strip()]
        # album-page rows are "Title | Artist • Album • 5:03"; the artist is the
        # first field that is not the album we already know and not a duration
        track_artist = artist
        if not track_artist:
            for part in parts:
                if filters.parse_duration(part):
                    break
                if part.lower() != (album or "").lower():
                    track_artist = part
                    break
        duration = 0
        for part in reversed(parts):
            if not duration:
                duration = filters.parse_duration(part)
        # the record's own cover is the right picture for every track on the album,
        # so the album card's art is preferred over a per-track video frame
        thumb = cover or _ytm_thumb(row, vid)
        track = {
            "id": vid,
            "source": "youtube-music",
            "endpoint": "browse-album",
            "url": f"https://music.youtube.com/watch?v={vid}",
            "title": title,
            "artist": track_artist,
            "duration": duration,
            "channel": track_artist,
            "thumbnail": thumb,
            "album": album,
            # the release year travels with the row so the page can badge it, and it
            # is exactly the "Albums in the right order with dates" a deep page wants
            "release_year": year or _year_in(sub),
        }
        if album:
            row_year = year or _year_in(sub)
            track["note"] = (f"{album}" + (f" \u00b7 {row_year}" if row_year else ""))
        tracks.append(track)
    return tracks


def ytm_album_tracklist(album: str, artist: str = "") -> list[dict]:
    """
    The tracks of one album, from YouTube Music's own album page (browse).

    A plain search of "<album> <artist>" returns songs from wherever; a browse of
    the album's own id returns the record's tracklist in release order with the
    right artist. Returns [] when no album card can be found or the page gives
    nothing - the caller falls back to a search, so a hand-to-the-album never
    turns into a dead end. One network round trip, no yt-dlp.
    """
    album = str(album or "").strip()
    if not album:
        return []
    q = f"{album} {artist}".strip()
    data = _http_json(YTM_ENDPOINT, {"context": {"client": YTM_CLIENT}, "query": q})
    if not data:
        return []
    title, subtitle, browse_id, cover = _find_card_album(data, album, artist)
    if not browse_id:
        return []
    page = ytm_browse(browse_id)
    if not page:
        return []
    # the year lives on the album *card* ("Album • Portishead • 1994"); the browse
    # page's own track rows usually omit it, so thread it onto every track
    year = _year_in(subtitle)
    rows = _ytm_browse_tracks(page, album=title or album,
                              artist=artist or _clean_artist(subtitle), year=year,
                              cover=cover)
    # the album page can carry non-track rows (the header card itself); drop them
    return [r for r in rows if r.get("id")]


def _ytm_browse_discography(data: dict, artist: str = "") -> list[dict]:
    """The albums of an artist's browse page -> page-row dicts, each with a year."""
    albums: list[dict] = []
    singles: list[dict] = []
    seen: set[str] = set()
    for row in _ytm_any_rows(data):
        title, subtitle, bid = _ytm_card_info(row)
        if not title or not bid:
            continue
        subtitle_l = subtitle.lower()
        # an artist browse page is a stack of sections (top songs, albums, singles);
        # only album and single rows belong on a release list - a song row is a track
        if "album" not in subtitle_l and "single" not in subtitle_l \
                and _year_in(subtitle) is None:
            continue
        year = _year_in(subtitle)
        is_album = "album" in subtitle_l
        badge = ("album" if is_album else "single")
        if year:
            badge = f"{year} \u00b7 {badge}"
        key = f"{title.lower()}|{artist.lower()}|{year or ''}"
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "id": "",
            "source": "youtube-music",
            "endpoint": "browse-artist",
            "title": title,
            "artist": artist,
            "album": title,
            "release_year": year,
            "browse_id": bid,
            "dur": "",
            # the album card's own art is the cover, so the row is not a blank tile
            "thumbnail": _ytm_card_thumb(row),
            "note": badge,
            "kind": "album",
        }
        (albums if is_album else singles).append(entry)
    # a discography is the artist's *albums*; singles (and EPs) only appear when
    # they never cut an album - "why its show all single" was the Singles & EPs
    # shelf outranking the records
    return albums or singles


def ytm_artist_discography(artist: str) -> list[dict]:
    """
    An artist's discography (their albums with release dates) from the browse page.

    Searches for the artist to get their browseId, then reads the album rows off
    the artist's own page - the same page that lists top songs - and returns them
    as page rows. Each carries `album`, `release_year` and `browse_id`, so the page
    can list a release by date and open one on a click. Returns [] when the browse
    gives nothing useful; the caller falls back to a songs search.
    """
    artist = _clean_artist(str(artist or "").strip())
    if not artist:
        return []
    data = _http_json(YTM_ENDPOINT, {"context": {"client": YTM_CLIENT},
                                     "query": artist})
    if not data:
        return []
    browse_id = ""
    for row in _ytm_any_rows(data):
        title, subtitle, bid = _ytm_card_info(row)
        if bid and (subtitle.lower() == "artist" or "artist" in subtitle.lower()
                    or not subtitle):
            browse_id = bid
            break
    if not browse_id:
        return []
    page = ytm_browse(browse_id)
    if not page:
        return []
    return _ytm_browse_discography(page, artist=artist)


# One dispatcher the web layer hands a page's kind to: try the deep browse path,
# then fall back to the ordinary search that a page used before, so "no browse
# data" degrades to the songs the UI already knew how to show.
def page_rows(kind: str, query: str, title: str = "", sub: str = "") -> list[dict]:
    kind = str(kind or "").strip().lower()
    if kind == "album":
        rows = ytm_album_tracklist(title or query, artist=sub)
        if rows:
            return rows
    elif kind == "artist":
        rows = ytm_artist_discography(title or _clean_artist(query))
        if rows:
            return rows
    return yt_search(query, limit=20)


def is_playlist_ref(ref: str) -> bool:
    """True for URLs / URIs / bare 22-char ids; False for plain seed words."""
    return Spotify().playlist_id(ref) is not None


def yt_audio_only_flags() -> list[str]:
    return ["--no-video", "--really-quiet"]


# ------------------------------------------------------------------ spotify
class Spotify:
    """
    Metadata-only Spotify access. Graceful: if you have no client id, or your
    app is post-Feb-2026 and you're not Premium, this just reports unavailable
    and the DJ keeps working off YouTube Music.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._exp = 0.0
        self.reason_unavailable = ""

    # -- auth
    def _cached(self) -> bool:
        try:
            d = json.loads(CACHE.read_text())
            if d.get("expires_at", 0) > time.time() + 30:
                self._token, self._exp = d["access_token"], d["expires_at"]
                return True
            if d.get("refresh_token"):
                return self._refresh(d["refresh_token"])
        except Exception:
            pass
        return False

    def _refresh(self, refresh_token: str) -> bool:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).encode()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(f"{AUTH}/api/token", data=body,
                                           method="POST",
                                           headers={"Authorization": self._basic()}),
                    timeout=20) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            self.reason_unavailable = f"token refresh failed ({e.__class__.__name__})"
            return False
        self._token, self._exp = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({
            "access_token": self._token, "expires_at": self._exp,
            "refresh_token": d.get("refresh_token", refresh_token)}))
        return True

    def _basic(self) -> str:
        import base64
        return "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    def client_token(self) -> bool:
        """App-level token: enough for /search, /tracks/{id}, /artists/{id}."""
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": CLIENT_ID,
                                       "client_secret": CLIENT_SECRET}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{AUTH}/api/token", data=body,
                                       method="POST", headers={"Authorization": self._basic()}),
                                       timeout=20) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            self.reason_unavailable = f"client-credentials refused ({e.__class__.__name__})"
            return False
        self._token, self._exp = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        return True

    def login(self) -> bool:
        """Loopback OAuth for user data (liked songs, your playlists)."""
        if not CLIENT_ID or not CLIENT_SECRET:
            self.reason_unavailable = "no SPOTIPY_CLIENT_ID/SECRET in environment"
            return False
        if self._cached() or self._refresh_token_login():
            return True
        self.reason_unavailable = self.reason_unavailable or "oauth flow did not complete"
        return False

    def _refresh_token_login(self) -> bool:
        import http.server
        import webbrowser

        holder: dict[str, str] = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                holder["code"] = (q.get("code") or [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>SpotifyDJ bridge authorised.</h2>You can close this tab.")

            def log_message(self, *a):  # silence
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 8899), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        url = (f"{AUTH}/authorize?response_type=code&client_id={CLIENT_ID}"
               f"&scope={urllib.parse.quote(SCOPES)}&redirect_uri={urllib.parse.quote(REDIRECT)}")
        print(f"\n[spotify] one-time authorisation - open:\n\n  {url}\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        deadline = time.time() + 180
        while not holder.get("code") and time.time() < deadline:
            time.sleep(0.4)
        srv.server_close()
        code = holder.get("code")
        if not code:
            self.reason_unavailable = "no auth code received (timeout)"
            return False
        body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": REDIRECT, "client_id": CLIENT_ID,
                                       "client_secret": CLIENT_SECRET}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{AUTH}/api/token", data=body,
                                       method="POST", headers={"Authorization": self._basic()}),
                                       timeout=20) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            self.reason_unavailable = f"code exchange failed ({e.__class__.__name__})"
            return False
        self._token, self._exp = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"access_token": self._token, "expires_at": self._exp,
                                    "refresh_token": d.get("refresh_token", "")}))
        return True

    # -- http
    def _get(self, path: str, **params) -> dict | None:
        if not self._token and not (self._cached() or self.client_token()):
            return None
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{API}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            self.reason_unavailable = f"HTTP {e.code} on {path}"
            return None
        except Exception as e:
            self.reason_unavailable = f"{e.__class__.__name__} on {path}"
            return None

    @staticmethod
    def _norm_track(item: dict) -> dict:
        return {
            "id": item.get("id") or "",
            "source": "spotify",
            "title": item.get("name") or "",
            "artist": ", ".join(a.get("name", "") for a in item.get("artists", [])),
            "album": (item.get("album") or {}).get("name", ""),
            "duration": (item.get("duration_ms") or 0) // 1000,
            "uri": item.get("uri") or "",
            "explicit": bool(item.get("explicit_content")),
        }

    def me(self) -> dict | None:
        return self._get("/me")

    def liked(self, limit: int = 50) -> list[dict]:
        d = self._get("/me/tracks", limit=min(limit, 50))
        if not d:
            return []
        return [self._norm_track(x["track"]) for x in d.get("items", []) if x.get("track")]

    def top_artists(self, limit: int = 20) -> list[str]:
        d = self._get("/me/top/artists", limit=min(limit, 50))
        if not d:
            return []
        return [a.get("name", "") for a in d.get("items", []) if a.get("name")]

    def playlist_id(self, ref: str) -> str | None:
        """Accepts a URL, a spotify:playlist: URI, or a bare 22-char id.

        Deliberately strict about bare ids (22-24 chars) so that a plain seed
        word like "bibio" is never mistaken for a playlist reference.
        """
        ref = (ref or "").strip()
        m = re.search(r"(?:playlist/|spotify:playlist:)([A-Za-z0-9]{22,})", ref)
        if m:
            return m.group(1)
        return ref if re.fullmatch(r"[A-Za-z0-9]{22,24}", ref) else None

    def playlist_seed(self, ref: str, limit: int = 40) -> list[dict]:
        """
        Note: since Feb 2026 Spotify only returns items for playlists you own
        or collaborate on - so seed your DJ from a *your* playlist.
        """
        pid = self.playlist_id(ref)
        if not pid:
            return []
        d = self._get(f"/playlists/{pid}/items", limit=100)
        if not d:
            return []
        out = []
        for row in d.get("items", []):
            it = row.get("track") or row.get("item")
            if it and it.get("type", "track") == "track":
                out.append(self._norm_track(it))
            if len(out) >= limit:
                break
        return out

    def recently_played(self, limit: int = 25) -> list[dict]:
        """Works on free accounts - this is how we sync with Spotube's UI."""
        d = self._get("/me/player/recently-played", limit=min(limit, 50))
        if not d:
            return []
        return [self._norm_track(x["track"]) for x in d.get("items", []) if x.get("track")]

    def save_to_playlist(self, playlist_id: str, uris: list[str]) -> bool:
        if not self._token and not (self._cached() or self.client_token()):
            return False
        body = json.dumps({"uris": uris}).encode()
        req = urllib.request.Request(
            f"{API}/playlists/{playlist_id}/items", data=body, method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25):
                return True
        except urllib.error.HTTPError as e:
            self.reason_unavailable = f"HTTP {e.code} saving to playlist (needs playlist-modify scope + Premium-free write path)"
            return False
        except Exception as e:
            self.reason_unavailable = str(e)
            return False


def spotify_search(query: str, limit: int = 20) -> list[dict]:
    """Public /v1/search - limit is 25 max on new apps since the 2026 changes."""
    sp = Spotify()
    d = sp._get("/search", type="track", q=query, limit=min(limit, 25))
    if not d:
        return []
    return [Spotify._norm_track(t) for t in d.get("tracks", {}).get("items", []) if t]


# ------------------------------------------------------------------- m3u out
def write_m3u(tracks: list[dict], path: Path, title: str = "Spotube DJ") -> Path:
    """
    An m3u8 Spotube can open (Settings -> local files / OS 'open with').
    `tracks` may already carry a resolved 'stream' URL; otherwise we emit the
    music.youtube.com page URL and let the player resolve it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", f"#EXTINF:-1,{title}"]
    for t in tracks:
        dur = int(t.get("duration") or -1)
        label = f"{t.get('artist', '?')} - {t.get('title', '?')}"
        lines.append(f"#EXTINF:{dur},{label}")
        lines.append(t.get("stream") or t.get("url") or "")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "midwest emo guitar instrumental"
    res = yt_search(q, limit=5)
    print(f"{len(res)} results for {q!r}")
    for r in res:
        print(f"  {r['title'][:58]:58} {r['artist'][:22]:22} {r['duration']}s")
