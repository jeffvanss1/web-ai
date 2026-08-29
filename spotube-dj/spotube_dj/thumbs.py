"""
thumbs.py - cover art for the GUI list, without a single extra dependency.

Rules this file exists to satisfy:
* Tk's PhotoImage reads PNG/GIF but NOT the JPEG that YouTube's CDN (and the
  Cover Art Archive) serves, so
  the JPEG is piped through ffmpeg (already a hard dependency of this app) and
  the PNG is cached. No Pillow, no ImageMagick requirement.
* Never block the UI: `get()` is designed to be called on the Bridge worker
  thread. The GUI only ever consumes a file path.
* Never fail loudly: no art, no ffmpeg, offline, blocked CDN - every case
  returns None and the caller draws a coloured tile instead. A music app that
  shows empty grey squares is fine; one that hangs on an image is not.
* Bounded disk use: a small fixed set of sizes and an LRU-ish prune.
"""

from __future__ import annotations

import os
import subprocess
import urllib.request

import bins
import config

# two sizes only: every row shares one file, every now-playing panel shares one
SIZES = {"row": 64, "big": 220}
_TIMEOUT = 6.0
_MAX_BYTES = 400_000
_MAX_FILES = 400


def cache_dir() -> "object":
    return config.APP_DIR / "thumbs"


def enabled() -> bool:
    """
    Art is opt-out: SPOTUBE_DJ_ART=0 skips it (offline machines, privacy).

    This used to be `shutil.which("ffmpeg") is not None`, which meant a launch from
    a .desktop file or an app menu - where PATH is the shell-less default and
    ~/.local/bin is not in it - silently turned *all* artwork off, since the only
    way this module had of scaling and decoding was ffmpeg. Tk can read PNG, GIF
    and baseline JPEG by itself, so art is now on unless the user turns it off;
    ffmpeg is used when it happens to be found (better: exact size, any format).
    """
    if (os.environ.get("SPOTUBE_DJ_ART") or "").strip().lower() in ("0", "off", "no"):
        return False
    return True


def source_of(track: dict, size: str = "big") -> tuple[str, str]:
    """
    -> (tag, url). "caa" means real release art from the Cover Art Archive
    (covers.py found it), "yt" a frame of the video. The tag is part of the
    cache filename, so an artwork upgrade can never be served from - or
    overwrite - the thumbnail that was there before it arrived.
    """
    url = track.get("cover_url") if isinstance(track, dict) else None
    if isinstance(url, str) and url.startswith("http"):
        # the archive offers front / front-250 / front-500, which are separate
        # endpoints rather than a size parameter, so its URL is used as given
        return ("caa", url)
    return ("yt", art_url(track, size))


def art_url(track: dict, size: str = "big") -> str:
    """
    The URL yt-dlp reported, or one derived from the video id - YouTube serves
    /vi/<id>/mqdefault.jpg for every video, so an id alone is enough.

    `size` then asks the CDN for an image that actually fits the slot. Measured on
    a live row: hqdefault is 21 KB and 480x360, `default` is 2.9 KB and 120x90,
    and the row tile is 42 px wide. Tk decodes the whole thing before it shrinks
    it, so asking for the right size is what turned ~30 ms per cover into ~0.4 ms
    - and 14 covers arriving at once is exactly the "everything is loading" feeling.
    """
    url = ""
    for key in ("thumbnail", "art", "thumb"):
        cand = track.get(key)
        if isinstance(cand, str) and cand.startswith("http"):
            url = cand
            break
    if not url:
        vid = str(track.get("id") or "")
        if vid and vid.replace("-", "").replace("_", "").isalnum():
            url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    return fit_size(url, SIZES.get(size, SIZES["big"]))


def fit_size(url: str, px: int) -> str:
    """
    Rewrite a known CDN URL to the smallest variant that still fills `px`.

    Only the two hosts whose size we can name are touched; anything else (the
    Cover Art Archive, Spotify) is served as it was offered. A cache entry is
    keyed by size, so this can never mix a 120 px tile into a 220 px slot.
    """
    if not url:
        return ""
    try:
        target = max(32, int(px))
    except (TypeError, ValueError):
        return url
    base = url.split("?", 1)[0]
    if "i.ytimg.com/vi/" in base:
        # YouTube's fixed ladder. 120x90 covers a 64 px tile after Tk's 2x
        # subsample; anything larger takes hqdefault (480x360) over mqdefault,
        # which is the same pixels with black bars on some uploads.
        name = "default.jpg" if target <= 96 else "hqdefault.jpg"
        head = base.split("/vi/")[0]
        vid = base.rsplit("/vi/", 1)[-1].split("/")[0]
        return f"{head}/vi/{vid}/{name}"
    if "yt3.googleusercontent.com/" in base or "ggpht.com/" in base:
        # this host sizes anything with a `=s<N>-c` suffix: N px, cropped square,
        # which is what album art wants anyway
        root = base.split("=", 1)[0]
        return f"{root}=s{max(64, target * 2)}-c"
    return url


def path_for(vid: str, size: str = "row", tag: str = "yt",
             ext: str = "png") -> "object":
    px = SIZES.get(size, SIZES["row"])
    return cache_dir() / f"{vid}-{tag}-{px}.{ext}"


def _cached(vid: str, size: str, tag: str) -> "object":
    """Any already-stored file for this slot, whatever format it ended up in."""
    for ext in ("png", "jpg", "gif"):
        p = path_for(vid, size, tag, ext)
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def cached_path(track: dict, size: str = "row") -> str | None:
    """The cached file for this track's *current* art source, or None."""
    if not track:
        return None
    vid = str(track.get("id") or "")
    if not vid:
        return None
    tag, _url = source_of(track)
    out = _cached(vid, size, tag)
    return str(out) if out else None


def download_url(url: str, track: dict, size: str = "row",
                 tag: str = "caa") -> str | None:
    """
    Fetch one specific URL into this track's cache slot. Used by covers.py, which
    runs on its own thread and must not depend on the GUI asking again.
    """
    if not url or not enabled() or not track:
        return None
    vid = str(track.get("id") or "")
    if not vid:
        return None
    out = _cached(vid, size, tag)
    if out:
        return str(out)
    return _store(url, path_for(vid, size, tag), SIZES.get(size, SIZES["row"]))


def have(track: dict, size: str = "row") -> bool:
    """
    Is there already a cached picture for this row? Free: a directory scan, no
    network. Lets the UI ask for the rows that are *missing* art instead of
    re-asking for the first ten rows of every list forever.
    """
    if not track:
        return False
    vid = str(track.get("id") or "")
    if not vid:
        return False
    return bool(_cached(vid, size, source_of(track, size)[0]))


def get(track: dict, size: str = "row") -> str | None:
    """
    -> path to a cached PNG, or None. Safe to call from a worker thread.
    """
    if not track or not enabled():
        return None
    vid = str(track.get("id") or "")
    if not vid:
        return None
    tag, url = source_of(track, size)
    have = _cached(vid, size, tag)
    if have:
        return str(have)
    if not url:
        return None
    return _store(url, path_for(vid, size, tag), SIZES.get(size, SIZES["row"]))


def _store(url: str, out, px: int) -> str | None:
    """
    One URL -> one cached image. Any failure at all just means "no art".

    ffmpeg first, because it scales to the exact tile and reads anything. Without
    it the bytes are stored as they came (`.jpg`/`.png`/`.gif`, all of which Tk
    8.6 decodes natively) and the UI shrinks them with PhotoImage -subsample, so a
    machine with no ffmpeg still gets artwork instead of coloured initials.
    WebP/AVIF have no Tk decoder and are not stored: those come from YouTube's
    CDN only when the client asks for them, and we never do.
    """
    blob = _fetch(url)
    if not blob:
        return None
    png = _to_png(blob, px)
    if png:
        data, target = png, out
    else:
        ext = _tk_format(blob)
        if not ext:
            return None
        data, target = blob, out.with_suffix("." + ext)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        part.write_bytes(data)
        part.replace(target)              # atomic: a torn file never loads
        _prune(target.parent)
        return str(target)
    except OSError:
        return None


# what Tk's own photo reader accepts -> the extension it must be stored under
TK_FORMATS = {b"\x89PNG": "png", b"GIF8": "gif", b"\xff\xd8\xff": "jpg"}


def _tk_format(blob: bytes) -> str:
    """Extension Tk can decode this blob under, or "" for 'we cannot use it'."""
    for magic, ext in TK_FORMATS.items():
        if blob.startswith(magic):
            return ext
    return ""


# ---------------------------------------------------------------- internals
def _fetch(url: str) -> bytes:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "spotube-dj/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = r.read(_MAX_BYTES)
        return data if data and len(data) > 200 else b""
    except Exception:
        return b""


def _to_png(blob: bytes, px: int) -> bytes:
    """JPEG bytes -> PNG bytes, scaled to `px` wide, via ffmpeg on stdio."""
    exe = bins.find("ffmpeg")
    if not exe:
        return b""
    try:
        # `image2pipe -c:v png` is mandatory: plain `-f image2 pipe:1` happily
        # writes the JPEG back out (1448 bytes of \xff\xd8 instead of a PNG),
        # which PhotoImage then refuses. Checking the signature keeps a silent
        # codec change from becoming a blank square in the UI.
        proc = subprocess.run(
            [exe, "-loglevel", "error", "-i", "pipe:0",
             "-vf", f"scale={px}:-2", "-frames:v", "1",
             "-f", "image2pipe", "-c:v", "png", "pipe:1"],
            input=blob, capture_output=True, timeout=15)
    except Exception:
        return b""
    out = proc.stdout or b""
    return out if out[:8] == b"\x89PNG\r\n\x1a\n" else b""


def _prune(directory) -> None:
    """Keep the cache from growing forever; art is disposable by definition."""
    try:
        files = sorted([p for pat in ("*.png", "*.jpg", "*.gif")
                        for p in directory.glob(pat)], key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in files[: max(0, len(files) - _MAX_FILES)]:
        try:
            old.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    import json
    import sys
    t = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"id": "dQw4w9WgXcQ"}
    print("art url :", art_url(t))
    print("enabled :", enabled())
    print("png     :", get(t))
