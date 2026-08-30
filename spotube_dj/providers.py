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
        # Two or three acceptable rows from a music-only catalogue still beat a
        # slower, dirtier plain-YouTube sweep, so don't fall back just because a
        # sparse query only had three songs on its first page.
        if len(kept) >= min(max(1, limit // 3), 3):
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
    out = (strong if len(strong) >= min(3, limit) else kept)[:limit]
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
