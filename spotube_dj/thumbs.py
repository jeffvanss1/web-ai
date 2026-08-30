"""
thumbs.py - the cover-art cache: one file per video id per size, no new dependency.

Rules this file exists to satisfy:
* The page is an <img>, so any format the browser reads could be cached as it
  came. ffmpeg is used when it is present because it also *sizes* the file - the
  JPEG is piped through it and the exact-slot PNG is cached - but it is not
  required, and there is no Pillow or ImageMagick dependency either way.
* Never block a request: `get()` is only ever called on the artwork lane's
  thread. The web layer consumes a file path and nothing else.
* Never fail loudly: no art, no ffmpeg, offline, blocked CDN - every case
  returns None and the caller draws a coloured tile instead. A music app that
  shows empty grey squares is fine; one that hangs on an image is not.
* Bounded disk use: a small fixed set of sizes and an LRU-ish prune.
"""

from __future__ import annotations

import io
import os
import subprocess
import urllib.request

import bins
import config

# Three slots, sized against what the page actually draws them at - not one file
# shared by everything, which is what made the card grid a mosaic of 64 px
# thumbnails stretched to 190. `card` and `big` match a rung the Cover Art Archive
# really serves (front-250, front-500), so a release cover is never upscaled.
SIZES = {"row": 72, "card": 256, "big": 512}
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
    way this module had of scaling and decoding was ffmpeg. A browser decodes PNG,
    GIF and baseline JPEG itself, so art is now on unless the user turns it off;
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
        # the archive gives one URL per rung (front, front-250, front-500), and the
        # file covers.py points at is already the biggest useful one: every slot
        # down from 500 is a local downscale, which is why a card can be sharp
        # before the slot-specific fetch ever happens
        return ("caa", url)
    url = art_url(track, size)
    return (rung_of(url), url)


def art_url(track: dict, size: str = "big") -> str:
    """
    The URL yt-dlp reported, or one derived from the video id - YouTube serves
    /vi/<id>/mqdefault.jpg for every video, so an id alone is enough.

    `size` asks the CDN for an image that actually fits the slot. Measured on the
    live ladder: default is 2.9 KB at 120x90 (too soft for anything but a favicon),
    mqdefault 8 KB at 320x180, sddefault ~30 KB at 640x480. A 40 px row given
    sddefault costs 14 x 30 KB of decode for nothing; a 190 px card given 120x90 is
    the pixelated grid this replaced. Asking for the right rung is also what keeps
    a 14-row list from arriving all at once - the "everything is loading" feeling.
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
        # YouTube's ladder, measured rather than trusted: maxresdefault (1280x720)
        # and mqdefault (320x180) are the video's real 16:9 frame - clean corners -
        # while sddefault (640x480) and hqdefault (480x360) are the 4:3 canvas, and
        # YouTube *pads them itself*: every corner of every sddefault we pulled was
        # (0,0,0). A barred image cropped to a square keeps the bars inside the
        # card, which is the "why is there a grey band" look. So: the small slot
        # takes mqdefault, everything bigger takes maxres, and the 4:3 rungs are
        # never asked for at all.
        name = "mqdefault.jpg" if target <= 96 else "maxresdefault.jpg"
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
    -> path to a cached image that fits `size`, or None. Safe on a worker thread.

    A slot with no file of its own takes the largest one already on disk instead of
    fetching again: the UI downscales it, it looks better than a second rung of the
    same picture, and one row of a 20-row list stops costing three CDN round trips.
    A smaller file is never stretched into a bigger slot - that is the smear this
    whole module exists to avoid - so the borrow only goes one way.
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
    bigger = _bigger_cached(vid, SIZES.get(size, SIZES["row"]))
    if bigger:
        return bigger
    if not url:
        return None
    return _store(url, path_for(vid, size, tag), SIZES.get(size, SIZES["row"]))


def _bigger_cached(vid: str, px: int) -> str:
    """
    The largest picture already on disk for this id that is at least `px` wide.

    Deliberately not scoped to one source tag: a 72px row can absolutely be drawn
    from the frame cached for the card, and refusing it because the filename names a
    different rung is exactly the "why did it download the same art twice" waste.
    Never the other way round - a smaller file is not stretched into a bigger box.
    """
    d = cache_dir()
    if not d.exists():
        return ""
    best, best_px = "", px
    for name, size in SIZES.items():
        if size <= best_px:
            continue
        for path in d.iterdir():
            stem = path.stem                       # name without the extension
            if (stem.startswith(f"{vid}-") and stem.endswith(f"-{size}")
                    and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif")
                    and path.stat().st_size > 0):
                best, best_px = str(path), size
                break
    return best


def smaller_rung(url: str) -> str:
    """
    The next rung down worth having, for when the one we asked for 404s.

    maxresdefault does not exist for every upload, and the ladder's next step down
    is a 4:3 padded frame - so it is skipped. mqdefault is smaller but it is the
    clean 16:9 image, and a soft picture with no bars beats a sharp one with them.
    """
    if "/maxresdefault." in url:
        return url.replace("/maxresdefault.", "/mqdefault.")
    for barred in ("sddefault", "hqdefault"):
        if f"/{barred}." in url:
            return url.replace(f"/{barred}.", "/mqdefault.")
    return ""


def rung_of(url: str) -> str:
    """
    Which rung this URL is, as the cache filename's source tag.

    The rung belongs in the name because the rungs are not interchangeable: an image
    cached from the 4:3 padding cannot be reused for a 16:9 slot, and without the
    rung in the filename a fix to the ladder would keep serving the barred file
    that was cached before it.
    """
    for name, tag in (("maxresdefault", "yt-max"), ("mqdefault", "yt-mq"),
                      ("sddefault", "yt-sd"), ("hqdefault", "yt-hq"),
                      ("default", "yt-df")):
        if f"/{name}." in url:
            return tag
    return "yt"


def _store(url: str, out, px: int) -> str | None:
    """
    One URL -> one cached image. Any failure at all just means "no art".

    ffmpeg first, because it scales to the exact slot and reads anything. Without
    it the bytes are stored as they came (`.jpg`/`.png`/`.gif`, all of which a
    browser decodes natively) and CSS shrinks them with `object-fit`, so a machine
    with no ffmpeg still gets artwork instead of coloured initials.
    WebP/AVIF are not stored: those come from YouTube's CDN only when the client
    asks for them, and we never do.
    """
    blob = _fetch(url)
    if not blob:
        alt = smaller_rung(url)
        return _store(alt, out, px) if alt else None
    if len(blob) < 2048 and "/vi/" in url:
        # a *tiny* JPEG off i.ytimg.com is YouTube's placeholder, not the cover -
        # the classic missing-maxresdefault answer. Take the rung that always exists
        # rather than caching grey pixels into a card.
        alt = smaller_rung(url)
        if alt:
            other = _fetch(alt)
            if other and len(other) > len(blob):
                blob = other
    png = _to_png(blob, px)
    if png:
        data, target = png, out
    else:
        shrunk = _shrink(blob, px)
        ext = "jpg" if shrunk else _image_format(blob)
        if not ext:
            return None
        data, target = (shrunk or blob), out.with_suffix("." + ext)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        part.write_bytes(data)
        part.replace(target)              # atomic: a torn file never loads
        _prune(target.parent)
        return str(target)
    except OSError:
        return None


def _shrink(blob: bytes, px: int) -> bytes:
    """
    The same frame, sized to the box, using Pillow instead of ffmpeg.

    Without this a 256 px card ships the CDN's whole 1280x720 JPEG - 46 KB, on a
    phone metering its data, per row. ffmpeg can do this job but is a 60 MB install
    nobody has for a thumbnail, so it is optional here too: no Pillow, no shrink,
    the bytes are stored as they came and CSS scales them, exactly as before.

    Only the SHORTER edge is brought down to the slot. Every source is 16:9 and every
    box is square, so `object-fit:cover` crops the sides - resize the height to the
    box and the width follows, and the pixels the browser actually paints are the
    pixels we stored. Shrinking to `px` wide instead would put a soft 256x144 where a
    crisp one belongs.
    """
    try:
        from PIL import Image
    except Exception:
        return b""
    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
        if min(im.width, im.height) <= px:
            return b""                        # already at or below the slot
        scale = px / min(im.width, im.height)
        size = (max(px, round(im.width * scale)), max(px, round(im.height * scale)))
        im = im.convert("RGB").resize(size, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=86, optimize=True)
        data = buf.getvalue()
        # never trade resolution for a bigger file: a photo re-encoded at q86 can
        # exceed a clean CDN JPEG, and then the "optimisation" achieved nothing
        return data if 2048 < len(data) < len(blob) else b""
    except Exception:
        return b""


# what a browser (and no decoder of ours) is guaranteed to accept as-is
IMAGE_FORMATS = {b"\x89PNG": "png", b"GIF8": "gif", b"\xff\xd8\xff": "jpg"}


def _image_format(blob: bytes) -> str:
    """Extension this blob can be stored under, or "" for 'we cannot use it'."""
    for magic, ext in IMAGE_FORMATS.items():
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
        # which reads as a truncated download. Checking the signature keeps a silent
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
