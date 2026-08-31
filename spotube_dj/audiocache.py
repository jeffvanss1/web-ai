"""
audiocache.py - the next few tracks, already on disk.

The gap you hear between songs is not mpv: it is this app, in the moment you
press play, running yt-dlp to fetch a page, pick a format and get a signed
googlevideo URL - several seconds, every track, and longer when YouTube is
throttling. Playing a *file* has none of that: no resolve, no DNS, no expiring
signature, no 403 halfway through.

So: while a track plays, the next couple are downloaded as m4a into
~/.spotube-dj/audio/, and the player is handed the local path when one exists.
At ~128 kbps a 3-minute song is about 3 MB, so the default 2 GB ceiling is over
six hundred tracks; oldest-out, because the DJ will not want them again.

Two rules that keep this from being worse than not doing it:
  * one download at a time, rate-limited - a prefetch that saturates the pipe
    makes the *currently playing* stream stutter, which is the opposite of the
    point;
  * never trust a partial file: it is written as `.part` and renamed only when
    yt-dlp says it is done.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import bins
import config

_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
_TIMEOUT = 240
_LIMIT_RATE = "1500K"          # keep the playing stream's bandwidth free
_DEFAULT_CAP_MB = 2048

_lock = threading.Lock()
_start_lock = threading.Lock()   # only for creating/replacing the worker thread
_inflight: set[str] = set()
_queue: list = []
# vid -> (stream url, when we learned it). A row the downloader has *looked at* is
# already resolved, and a resolved row can start in mpv before its file has finished:
# that is the difference between "next" feeling instant and waiting 2-3 s on a
# resolver call while the listener is mid-skip. Signed URLs expire, so there is a TTL.
_resolved: dict = {}
RESOLVED_TTL = 1800.0
_thread: threading.Thread | None = None
_stop = threading.Event()
_notifier = None
_stats = {"hit": 0, "stored": 0, "failed": 0, "pruned": 0, "bytes": 0}


def cache_dir() -> Path:
    return config.APP_DIR / "audio"


def enabled() -> bool:
    if (os.environ.get("SPOTUBE_DJ_CACHE") or "").strip().lower() in (
            "0", "off", "no", "false"):
        return False
    # bins.find, not shutil.which: a launch from the app menu has a PATH without
    # /usr/local/bin, where pip put yt-dlp - the same trap that hid the cover art.
    return bins.find("yt-dlp") is not None or _module_ytdlp()


def _module_ytdlp() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


def cap_bytes() -> int:
    raw = (os.environ.get("SPOTUBE_DJ_CACHE_MB") or "").strip()
    try:
        mb = float(raw) if raw else float(_DEFAULT_CAP_MB)
    except ValueError:
        mb = float(_DEFAULT_CAP_MB)
    return max(0, int(mb * 1024 * 1024))


def set_notifier(fn) -> None:
    """`fn(event, detail)` - so the page can log 'cached 2 ahead' without this
    module knowing a front end exists."""
    global _notifier
    _notifier = fn


def brief() -> tuple[int, int]:
    """(waiting, fetched-ahead) from the counters only - safe to poll fast."""
    with _lock:
        return len(_queue) + len(_inflight), _stats["stored"]


def stats() -> dict:
    with _lock:
        out = dict(_stats)
    out["pending"] = len(_queue) + len(_inflight)
    out["files"] = len(list(_files()))
    out["dir"] = str(cache_dir())
    out["cap_mb"] = cap_bytes() // (1024 * 1024)
    return out


def path_for(video_id: str) -> str | None:
    """-> a usable local file for this track, or None."""
    if not video_id:
        return None
    for suffix in (".m4a", ".webm", ".mka", ".opus", ".mp3"):
        p = cache_dir() / f"{video_id}{suffix}"
        if p.is_file() and p.stat().st_size > 4096:
            return str(p)
    return None


def url_for(path: str) -> str:
    """mpv takes a URL-ish string; a local file wants the scheme."""
    return f"file://{os.path.abspath(path)}"


def lookup(track: dict) -> tuple[str | None, str | None]:
    """
    -> (local_path, url_or_None) for a track. The (\"\", None) pair means "not
    cached"; the caller resolves the stream as usual and should call prefetch().

    A track that is not on disk yet but *was* already looked at by the lane comes
    back as (None, url): no file to play yet, but nothing to wait for either.
    """
    if not track or not enabled():
        return None, None
    path = path_for(str(track.get("id") or ""))
    if not path:
        return None, resolved(str(track.get("id") or ""))
    with _lock:
        _stats["hit"] += 1
    return path, url_for(path)


def prefetch(tracks, ahead: int = 2, priority: bool = False) -> int:
    """
    Queue the next `ahead` uncached tracks and make sure a thread exists to do
    it. Returns how many were queued. Never blocks, never raises.

    `priority=True` puts them at the *front* of the lane. That flag is what an
    actively skipping listener needs: with a plain FIFO, the row about to be heard
    queues behind three rows from the end of the set and never wins the race.
    """
    if not enabled():
        return 0
    vids: list[str] = []
    with _lock:
        for t in list(tracks or []):
            if len(vids) >= max(0, ahead):
                break
            vid = str((t or {}).get("id") or "")
            if not vid or vid in _inflight or path_for(vid) or _queued(vid):
                continue
            vids.append(vid)
        if vids:
            # one splice, in order: inserting one at a time at index 0 would hand
            # the worker the rows backwards, and the row about to be heard second
            # would be downloaded first
            if priority:
                _queue[:0] = vids
            else:
                _queue.extend(vids)
    if vids:
        _start()
    return len(vids)


def promote(video_id: str) -> bool:
    """
    Move a still-queued track to the front. -> whether it was in the lane at all.

    Only ever touches work that has not started: `_queue` is the pending list, and
    `_inflight` is what the worker is already downloading, which cannot be un-begun
    without leaving a `.part` file behind for nothing.
    """
    vid = str(video_id or "")
    if not vid:
        return False
    with _lock:
        try:
            _queue.remove(vid)
        except ValueError:
            return False
        _queue.insert(0, vid)
        return True


def resolved(video_id: str) -> str | None:
    """The stream URL the lane found for a track, if it is still young enough."""
    vid = str(video_id or "")
    if not vid:
        return None
    with _lock:
        hit = _resolved.get(vid)
    if not hit:
        return None
    url, when = hit
    if time.time() - float(when) > RESOLVED_TTL:
        with _lock:
            _resolved.pop(vid, None)
        return None
    return url or None


def remember(video_id: str, url: str) -> None:
    """Record a stream URL the caller resolved, so the lane and the player share it."""
    _remember(str(video_id or ""), str(url or ""))


def _peek_url(video_id: str) -> str:
    """
    Ask yt-dlp for the audio URL and nothing else (`-g`), ~1 s, no bytes moved.

    This is what makes a fast skipper's next press cost a file read instead of a
    resolver round trip: the lane is going to spawn yt-dlp for this track anyway, so
    the price is one process in a background thread, and the payoff is that the row
    is *startable* long before it is *downloadable*.
    """
    cmd = _yt_dlp_cmd() + [
        "-g", "-f", _FORMAT, "--no-playlist", "--no-warnings", "--retries", "1",
        f"https://music.youtube.com/watch?v={video_id}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(("http://", "https://")):
            return line            # first protocol line: -f can print several
    return ""


def _remember(video_id: str, url: str) -> None:
    with _lock:
        _resolved[video_id] = (url or "", time.time())
        # bounded the same way the state file is: this lives for the length of a
        # listening session, and a week-long daemon must not grow a URL table
        while len(_resolved) > 400:
            _resolved.pop(next(iter(_resolved)), None)


_prefetch = prefetch          # the GUI's worker calls this after each advance


def _queued(vid: str) -> bool:
    return any(v == vid for v in _queue)


def _start() -> None:
    global _thread
    with _start_lock:
        t = _thread
        # same rule as covers.py: a thread that was just stopped is still alive
        # for one poll, and treating that as "a worker exists" strands the queue
        if t is not None and t.is_alive() and not _stop.is_set():
            return
        if t is not None:
            t.join(timeout=2.0)
        _stop.clear()
        _thread = threading.Thread(target=_worker, name="audiocache", daemon=True)
        _thread.start()


def stop(wait: bool = False) -> None:
    """Stop the downloader and drop its pending list (see covers.stop for why)."""
    _stop.set()
    with _lock:
        del _queue[:]
    if wait and _thread is not None:
        _thread.join(timeout=5)


def _worker() -> None:
    while not _stop.is_set():
        vid = None
        with _lock:
            if _queue:
                vid = _queue.pop(0)
                _inflight.add(vid)
        if vid is None:
            _stop.wait(0.4)
            continue
        if not resolved(vid):
            url = _peek_url(vid)
            if url:
                _remember(vid, url)
        try:
            path = fetch(vid)
        except Exception:
            path = None
        with _lock:
            _inflight.discard(vid)
            if path:
                _stats["stored"] += 1
            else:
                _stats["failed"] += 1
        if _notifier:
            try:
                _notifier("cached" if path else "cache-failed", (vid, path or ""))
            except Exception:
                pass
        prune()


def fetch(video_id: str) -> str | None:
    """One track -> a cached file. Blocking: call from a worker thread only."""
    if not video_id or not enabled():
        return None
    already = path_for(video_id)
    if already:
        return already
    out = cache_dir() / f"{video_id}.m4a"
    part = str(out) + ".part"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = _yt_dlp_cmd() + [
        "-f", _FORMAT, "--no-playlist", "--newline",
        "--limit-rate", (os.environ.get("SPOTUBE_DJ_CACHE_RATE") or _LIMIT_RATE),
        "--retries", "2", "--fragment-retries", "2",
        "--no-mtime", "-o", part,
        f"https://music.youtube.com/watch?v={video_id}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except Exception:
        _unlink(part)
        return None
    if proc.returncode != 0:
        _unlink(part)
        return None
    # yt-dlp names the file after the container it got, which is not always the
    # .m4a we asked for - take whatever `<part-prefix>.*` appeared next to it.
    found = _take(part, out)
    if not found:
        _unlink(part)
        return None
    with _lock:
        _stats["bytes"] += found
    return str(out) if path_for(video_id) else None


def _yt_dlp_cmd() -> list:
    """The standalone binary if there is one, else this interpreter's module."""
    exe = bins.find("yt-dlp")
    if exe:
        return [exe]
    import sys
    return [sys.executable, "-m", "yt_dlp"]


_EXTS = (".m4a", ".webm", ".mka", ".opus", ".mp3")


def _take(part: str, wanted: Path) -> int:
    """
    Rename the finished artefact to `<id>.m4a`; -> its size, 0 if nothing.

    yt-dlp was handed `<out>.m4a.part` as the output template, so a completed
    download is either exactly that (we got the container we asked for) or that
    with the real container appended - `<out>.m4a.part.webm`, which is what
    happens when the track is opus-in-webm and no m4a exists. Both are accepted,
    and the file is stored under the name path_for() looks for.
    """
    base = Path(part)
    stem = str(base.with_name(base.name[:-5]))      # strip ".part"
    cands = [Path(stem + ext) for ext in _EXTS] + [Path(f"{part}{ext}") for ext in _EXTS]
    for cand in cands:
        if cand.is_file() and cand.stat().st_size > 4096:
            try:
                cand.replace(wanted)
                return wanted.stat().st_size
            except OSError:
                return 0
    p = Path(part)
    if p.is_file() and p.stat().st_size > 4096:
        try:
            p.replace(wanted)
            return wanted.stat().st_size
        except OSError:
            return 0
    return 0


def _unlink(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _files():
    try:
        yield from cache_dir().glob("*")
    except OSError:
        return


def prune() -> int:
    """Oldest-out until the directory fits the cap. -> files removed."""
    cap = cap_bytes()
    files = [p for p in _files() if p.is_file() and not p.name.endswith(".part")]
    total = sum(p.stat().st_size for p in files)
    if total <= cap:
        return 0
    removed = 0
    for p in sorted(files, key=lambda q: q.stat().st_mtime):
        try:
            total -= p.stat().st_size
            p.unlink()
            removed += 1
        except OSError:
            continue
        if total <= cap:
            break
    with _lock:
        _stats["pruned"] += removed
    return removed


def clear() -> int:
    """Delete every cached track. -> how many."""
    n = 0
    for p in list(_files()):
        try:
            if p.is_file():
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def doctor_lines() -> list:
    if not enabled():
        return ["audio cache  off (SPOTUBE_DJ_CACHE=off, and no yt-dlp to use)"]
    s = stats()
    used = sum(p.stat().st_size for p in _files() if p.is_file()) / 1048576
    return [f"audio cache  {s['files']} files, {used:.0f} MB of {s['cap_mb']} MB in "
            f"{s['dir']}; {s['hit']} instant starts, {s['stored']} fetched ahead"]


if __name__ == "__main__":
    import sys

    vid = sys.argv[1] if len(sys.argv) > 1 else "dQw4w9WgXcQ"
    print("enabled:", enabled(), "| cap:", cap_bytes() // 1048576, "MB")
    t0 = time.time()
    print("fetched:", fetch(vid), f"({time.time() - t0:.1f}s)")
    print("lookup:", lookup({"id": vid}))
    print("stats :", stats())
