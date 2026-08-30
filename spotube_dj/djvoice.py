"""
djvoice.py - the spoken DJ.

The on-screen announcer (agent.narrate) is visual. This module makes the same
"why this song / what's next" line actually *speak* on each new track, so the DJ
reads it aloud over the music like Spotify's DJ. No API key is needed:

  1. **edge-tts** (Microsoft neural voices, online, free) gives the best voice.
  2. **espeak / espeak-ng** (offline) is used as a fallback; it is robotic but
     works with no account and no network.
  3. If neither is available, or the player's audio can't be reached, it stays
     silent - the vocal line is a nicety, never a crash.

Speech is synthesized and played on a background thread so it never blocks the
engine or the web server, and it degrades gracefully in a sandbox with no audio.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import bins
import config

# A confident, announcer-ish male voice by default; overridable per shell or in
# the config, and a female alternative that also reads well.
DEFAULT_VOICE = "en-US-ChristopherNeural"
DEFAULT_VOICE_F = "en-US-AriaNeural"
_cache = {"edge": None}          # import edge_tts once, keep talking to it


def enabled(dj=None) -> bool:
    """Is the spoken DJ switched on?

    Two switches: the env var (a hard `SPOTUBE_DJ_VOICE=off` for a deploy) and the
    DJ's persisted `voice` setting (on by default, toggled from the page).
    """
    raw = (os.environ.get("SPOTUBE_DJ_VOICE") or "on").strip().lower()
    if raw in ("0", "off", "no", "false"):
        return False
    if dj is not None:
        try:
            if not bool((dj.state or {}).get("voice", True)):
                return False
        except Exception:
            pass
    return True


def voice_name() -> str:
    """The configured TTS voice (edge-tts names)."""
    raw = (os.environ.get("SPOTUBE_DJ_VOICE_NAME") or DEFAULT_VOICE).strip()
    return raw or DEFAULT_VOICE


def engines() -> list[str]:
    """Which speech engine(s) this machine can actually use, in preference order."""
    out = []
    try:
        import edge_tts  # noqa: F401
        out.append("edge-tts")
    except Exception:
        pass
    if bins.find("espeak-ng") or bins.find("espeak"):
        out.append("espeak")
    if bins.find("mpv"):
        out.append("mpv")
    return out


# one speech worker: a rapid fire of track changes (a fast skipper) collapses to
# the *latest* line so the DJ never runs a queue of scripts or talks over itself.
_cond = threading.Condition()
_pending: str | None = None
_worker = False


def speak_for(dj) -> None:
    """Fire-and-forget: speak the DJ line for the current track of `dj`.

    Returns immediately; the synthesis + playback happen on a single daemon
    worker so a slow network voice never stalls the engine, and only the most
    recent line is announced. Never raises.
    """
    global _pending
    if not enabled(dj):
        return
    try:
        import agent
        text = agent.dj_speech(dj)
    except Exception:
        return
    if not text:
        return
    global _worker
    with _cond:
        _pending = text
        if not _worker:
            _worker = True
            threading.Thread(target=_worker_loop, daemon=True).start()


def _worker_loop() -> None:
    """Serial synthesis+play: take the latest pending line, then stop when idle."""
    global _pending, _worker
    while True:
        with _cond:
            text = _pending
            _pending = None
        if not text:
            with _cond:
                _worker = False
                return                    # idle; the next speak_for restarts me
        # a little breathing room so two DJ lines don't overlap
        time.sleep(0.15)
        _say(text)


def _say(text: str) -> None:
    """Synthesize then play one line, on this (background) thread. Never raises."""
    clip = _synth(text)
    if not clip:
        return
    try:
        _play(clip)
    finally:
        def _cleanup():
            time.sleep(30)
            try:
                Path(clip).unlink(missing_ok=True)
            except OSError:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()


def _engine() -> str:
    """Pick the engine: edge-tts if importable, else espeak, else none."""
    try:
        import edge_tts  # noqa: F401
        _cache["edge"] = True
        return "edge-tts"
    except Exception:
        _cache["edge"] = False
    if bins.find("espeak-ng") or bins.find("espeak"):
        return "espeak"
    return ""


def _edge_synth(text: str, path: Path) -> bool:
    """Synthesize with edge-tts (needs network). Returns True on success."""
    try:
        import edge_tts
    except Exception:
        return False
    try:
        voice = voice_name()
        # edge_tts.Communicate is async; drive it on the current thread's loop.
        asyncio.run(edge_tts.Communicate(text, voice=voice).save(str(path)))
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False


def _espeak_synth(text: str, path: Path) -> bool:
    """Synthesize offline with espeak-ng (or espeak). Returns True on success."""
    exe = bins.find("espeak-ng") or bins.find("espeak")
    if not exe:
        return False
    try:
        subprocess.run([exe, "-w", str(path), text],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False


def _synth(text: str) -> str | None:
    """Return a path to a spoken clip for `text`, or None if it could not be made."""
    engine = _engine()
    if not engine:
        return None
    clip = Path(tempfile.mkstemp(suffix=".mp3" if engine == "edge-tts" else ".wav")[1])
    ok = _edge_synth(text, clip) if engine == "edge-tts" else _espeak_synth(text, clip)
    if not ok:
        try:
            clip.unlink(missing_ok=True)
        except OSError:
            pass
        # if edge-tts failed (e.g. no network), try the offline engine as a fallback
        if engine == "edge-tts":
            clip = Path(tempfile.mkstemp(suffix=".wav")[1])
            if _espeak_synth(text, clip):
                return str(clip)
            try:
                clip.unlink(missing_ok=True)
            except OSError:
                pass
        return None
    return str(clip)


def _play(path: str) -> None:
    """Play the clip with mpv (a second process mixes with the music on the OS)."""
    exe = bins.find("mpv")
    if not exe:
        return
    try:
        # `--force-window=no --no-video --really-quiet`: a tiny audio-only process.
        # Volume a touch below the music so the voice sits in front of it, and
        # `--keep-open=no` lets it exit the moment the clip ends.
        subprocess.Popen([exe, "--really-quiet", "--no-video", "--force-window=no",
                          "--volume=90", "--", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# expose for tests / tools
def _has_edge() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False
