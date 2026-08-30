"""
djvoice.py - the spoken DJ, using Gemini's speech generation (the Despina voice).

The on-screen announcer (agent.narrate) is visual. This module makes the same
"why this song / what's next" line actually *speak* on each new track, over the
music, like Spotify's DJ. It is not a plain robotic read:

  1. **Gemini** writes the line and voices it. A Gemini model is asked (via the
     speech-generation API, model `gemini-3.1-flash-tts-preview`) to say the
     facts in a warm, playful DJ voice - so every track gets its own phrasing.
     The voice is **Despina** (warm, smooth) by default, changeable by voice name.
  2. **espeak / espeak-ng** is the offline fallback (robotic, but no key/network).
  3. If there is no key and no espeak, it stays silent - the vocal line is a
     nicety, never a crash.

Speech is generated and played on a single worker thread, so a fast skipper only
hears the latest line and the engine/webserver are never blocked. It degrades
gracefully in a sandbox with no network or audio.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import bins
import config


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
    """The Gemini voice used for the DJ (Despina by default)."""
    return (os.environ.get("SPOTUBE_DJ_TTS_VOICE") or config.DJ_VOICE or "Despina")


def tts_model() -> str:
    """The speech-generation model (a gemini-*-tts-preview)."""
    return (os.environ.get("SPOTUBE_DJ_TTS_MODEL") or config.GEMINI_DEFAULT_TTS_MODEL
            or "gemini-3.1-flash-tts-preview")


def engines() -> list[str]:
    """Which engines this machine can possibly use, in preference order."""
    out = []
    if config.LLM_API_KEY:
        out.append("gemini")
    if bins.find("espeak-ng") or bins.find("espeak"):
        out.append("espeak")
    return out


# one speech worker: a rapid fire of track changes (a fast skipper) collapses to
# the *latest* line so the DJ never runs a queue of scripts or talks over itself.
_cond = threading.Condition()
_pending: str | None = None
_worker = False


def speak_for(dj) -> None:
    """Fire-and-forget: speak the DJ line for the current track of `dj`.

    Returns immediately; the line is written (creatively, by Gemini when a key is
    set) and synthesized/played on a single daemon worker. Never raises.
    """
    global _pending
    if not enabled(dj):
        return
    try:
        import agent
        text = _creative_line(dj, agent)
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


def _creative_line(dj, agent) -> str:
    """A creative DJ line: ask Gemini to write it, fall back to the template."""
    if config.LLM_API_KEY:
        try:
            import brain
            prompt = agent.dj_prompt(dj)
            if prompt:
                line = brain.free_text(prompt, max_chars=400)
                if line:
                    return line
        except Exception:
            pass
    return agent.dj_speech(dj)            # keyless, still reads naturally enough


def _worker_loop() -> None:
    """Serial line-write + synth + play: take the latest pending, stop when idle."""
    global _pending, _worker
    while True:
        with _cond:
            text = _pending
            _pending = None
        if not text:
            with _cond:
                _worker = False
                return                    # idle; the next speak_for restarts me
        time.sleep(0.15)                  # breathing room so two lines don't overlap
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
    """Pick the engine: Gemini if a key is set, else espeak, else none."""
    if config.LLM_API_KEY:
        return "gemini"
    if bins.find("espeak-ng") or bins.find("espeak"):
        return "espeak"
    return ""


def _gemini_synth(text: str, path: Path) -> bool:
    """Ask Gemini's speech model to speak `text` with the configured voice.

    The response is base64 PCM (signed 16-bit, mono, 24 kHz by default); we wrap
    it as a WAV so mpv can play it. Returns True on success; a missing key, an
    HTTP error or a bad payload is False (the caller falls back to espeak).
    """
    if not config.LLM_API_KEY:
        return False
    try:
        import brain
        url = brain._gemini_url(tts_model())
        payload = {"contents": [{"parts": [{"text": text}]}],
                   "generationConfig": {
                       "responseModalities": ["AUDIO"],
                       "speechConfig": {
                           "voiceConfig": {
                               "prebuiltVoiceConfig": {"voiceName": voice_name()}
                           }
                       },
                   }}
        data = brain._post(url, payload, brain._gemini_headers())
    except Exception:
        return False
    try:
        part = (data["candidates"][0]["content"]["parts"][0])
        inline = part.get("inlineData") or {}
        b64 = inline.get("data")
        if not b64:
            return False
        pcm = base64.b64decode(b64)
        rate = 24000
        mime = str(inline.get("mimeType") or "")
        if "rate=" in mime:
            try:
                rate = int(mime.split("rate=")[1].split(";")[0])
            except (ValueError, IndexError):
                rate = 24000
        _pcm_to_wav(pcm, rate, path)
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False


def _pcm_to_wav(pcm: bytes, rate: int, path: Path) -> None:
    """Wrap raw signed-16-bit mono PCM in a WAV container (mpv plays WAV natively)."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(max(8000, rate))
        wf.writeframes(pcm)


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
    clip = Path(tempfile.mkstemp(suffix=".wav")[1])
    ok = False
    if engine == "gemini":
        ok = _gemini_synth(text, clip)
        if not ok and (bins.find("espeak-ng") or bins.find("espeak")):
            ok = _espeak_synth(text, clip)      # offline fallback after a Gemini miss
    elif engine == "espeak":
        ok = _espeak_synth(text, clip)
    if not ok:
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
        subprocess.Popen([exe, "--really-quiet", "--no-video", "--force-window=no",
                          "--volume=90", "--", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
