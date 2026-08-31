"""
djvoice.py - the spoken DJ, using Gemini's speech generation (the Despina voice).

The on-screen announcer (agent.narrate) is visual. This module makes the same
"why this song / what's next" line actually *speak* on each new track, over the
music, like Spotify's DJ. It is not a plain robotic read:

  1. **Gemini writes the line and voices it**, through the Cloudflare Worker
     (`POST /v1/speech`), which is the only route this app has to Gemini. The
     voice is **Despina** (warm, smooth) by default, changeable in Settings.
     The Worker answers with a WAV file and caches it by
     sha256(text|voice|model), so the free-tier quota that used to 429 mid-set
     is spent once per line rather than once per play.
  2. **The clip is played in the browser.** `web.py` registers itself as the
     sink with `set_sink()`, the clip is published into the state snapshot, and
     the page plays it through an `<audio>` element - so the voice comes out of
     the same machine the music is on, at a volume the page controls, with no
     second process. With no browser attached (a headless `--daemon`) the clip
     is handed to mpv, exactly as it always was.
  3. There is no offline/robotic fallback - if the Worker or Gemini cannot
     speak, the DJ stays silent. The vocal line is a nicety, never a crash, and
     never a robotic substitute.

The line for each upcoming song is written and synthesized on a background thread
AS SOON as the current track starts, so the announcement is already a ready clip
and plays instantly at the hand-off - a slow text/TTS call can no longer trail
30s into the next song. A fast skipper drops the stale clip (the line for a track
that already advanced is never played), and the engine/webserver are never blocked.
It degrades gracefully in a sandbox with no network or audio.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import bins
import config
import workerclient


# Optional diagnostic hook: the DJ passes its `_note` here so voice progress (and
# any failure) shows up in the on-screen log drawer, not just in stderr. This is
# the whole point when the voice "doesn't come out" - you can see if it was the
# key, the Worker, the audio bytes or the player, in one place.
WORKER = "worker"

_log: callable | None = None

# Where a finished clip is handed for playback. `web.py` installs a sink that
# publishes the clip to the page (browser audio); with no sink the clip goes to
# mpv. One function, so the two paths cannot disagree about what "played" means.
_sink: callable | None = None


def set_logger(fn) -> None:
    """Route voice diagnostics to `fn(msg)` (the DJ's `_note`)."""
    global _log
    _log = fn


def set_sink(fn) -> None:
    """
    Route finished clips to `fn(path, text) -> bool` (True = something played it).

    `web.py` calls this with the web player's publisher, which copies the clip
    into the served voice dir and puts its URL in the next state snapshot. A
    sink that returns False (no tab attached) falls back to mpv.
    """
    global _sink
    _sink = fn


def _info(msg: str) -> None:
    line = f"[voice] {msg}"
    if _log:
        try:
            _log(line)
            return
        except Exception:
            pass
    print(line, flush=True)


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
    """The Gemini voice used for the DJ (Despina by default, configurable in Settings)."""
    return config.load_dj_voice()


def tts_model() -> str:
    """The speech-generation model the Worker will speak with.

    A `*-live-*` name still works: the Worker maps it onto its REST TTS sibling
    (it cannot open the Live API's WebSocket as a client) and says so in the
    response. The default is the TTS model so the common case needs no mapping.
    """
    return (os.environ.get("SPOTUBE_DJ_TTS_MODEL") or config.GEMINI_DEFAULT_TTS_MODEL
            or "gemini-3.1-flash-tts-preview")


def engines() -> list[str]:
    """Which engines this machine can possibly use, in preference order.

    There is no offline (espeak) fallback: the DJ only speaks via Gemini, and
    Gemini is only reached through the Worker, so the list is one entry or none.
    """
    return [WORKER] if workerclient.configured() else []


def lead_secs() -> float:
    """Seconds before a track ends that the DJ starts the next-up announcement."""
    return getattr(config, "DJ_LEAD_SECS", 10.0)


def speak_for(dj) -> None:
    """[back-compat] Speak the current-track line now (see `speak_intro`)."""
    speak_intro(dj)


def speak_intro(dj) -> None:
    """Announce the *current* song now - used for the first track of a set.

    Runs entirely on a background thread so it never stalls the engine: the
    creative line is written and synthesized there and played as soon as it is
    ready. If the track advances while the line is being written, it stays quiet.
    """
    set_logger(getattr(dj, "_note", None))
    if not enabled(dj):
        _info("voice is off (env or the DJ's on/off setting)")
        return
    current_id = (dj.current or {}).get("id")

    def run() -> None:
        try:
            import agent
            text = _creative_line(dj, agent, next_up=False)
        except Exception as e:
            _info(f"intro line failed: {e.__class__.__name__}: {e}")
            return
        if not text:
            return
        if (dj.current or {}).get("id") != current_id:
            _info("intro line is stale (track advanced) - staying quiet")
            return
        _info(f"announcing track now (engine={_engine() or 'none'}, "
              f"model={tts_model()}, voice={voice_name()})")
        _say(text)          # the line is written; synth + play on this thread

    threading.Thread(target=run, daemon=True).start()


def schedule_next(dj) -> None:
    """Announce the *upcoming* song ~`lead_secs` before the current one ends.

    The line is written and synthesized ahead of time; a monitor thread waits
    until the current track is close to its end, then speaks. So the DJ leads
    into the next song instead of coming on late, after it has started.
    """
    set_logger(getattr(dj, "_note", None))
    if not enabled(dj):
        _info("voice is off (env or the DJ's on/off setting)")
        return
    threading.Thread(target=_monitor, args=(dj,), daemon=True).start()
    _info(f"scheduled the next-song line (model={tts_model()}, "
          f"lead={lead_secs():.0f}s before the end)")


def _monitor(dj) -> None:
    """Write + pre-synthesize the up-next line, then play it near the track's end.

    The clip is synthesized EARLY on this background thread (as soon as the track
    starts), so when the track is close to its end the announcement is already
    ready and plays immediately. That is the whole fix for the voice trailing
    ~30s into a song: a slow TTS call used to run *at* the hand-off moment and
    spill into the next track, and the creative line (a slow text call) blocked
    the engine.
    """
    try:
        import agent
        current_id = (dj.current or {}).get("id")
        if not current_id:
            return
        text = _lead_line(dj, agent)          # write it now, so it is ready in time
    except Exception:
        return
    if not text:
        return
    # a skip may have happened while writing; don't spend a TTS call on a stale line
    if (dj.current or {}).get("id") != current_id:
        _info("up-next line is stale (track advanced)")
        return
    clip = _synth(text)                        # pre-synthesize NOW, on this thread
    if not clip:
        return
    played = False
    try:
        if _wait_lead(dj, current_id):
            _play_clip(clip, text)
            played = True
    except Exception:
        pass
    finally:
        if not played:                        # skipped; drop the stale clip
            _unlink(clip)


def _wait_lead(dj, current_id: str) -> bool:
    """Poll the player; True when the current track is within `lead` of its end."""
    player = getattr(dj, "player", None)
    prog = getattr(player, "progress", None)
    if prog is None:
        return False
    # mpv's own end-of-file signal (eof-reached / idle-after-play). It is used as a
    # fallback when mpv reports no length for a stream, so a stream with no
    # `duration` is still announced at the hand-off instead of going silent. It also
    # tells a natural end apart from a manual skip, which is why it outranks the
    # stale-track guard below.
    fin = getattr(player, "finished", None)
    lead = lead_secs()
    # The clip is pre-synthesized at track START, so the wait must last the whole
    # song to reach the lead-in window near its END - a fixed short cap only ever
    # fired on sub-minute tracks and left every normal (3-5 min) song silent. Once
    # we can read a length we bound the wait by that track's own remaining time
    # (+ the lead + a small margin) so a dead beat cannot hang the thread forever.
    # A manual skip/force-advance changes `current.id` and aborts (so a skipped
    # track's line is never played); a *natural* end is the hand-off we want to
    # speak at, so when the track has no reported length we wait for `finished()`.
    deadline = time.monotonic() + 120.0
    while True:
        # The track naturally ended (EOF/idle-after-play): that IS the hand-off, so
        # speak even if the engine has already advanced `current.id` in the same
        # instant. A manual skip replaces the file before it can end, so `finished()`
        # stays False there and the stale-guard below goes silent instead.
        if fin is not None:
            try:
                if fin():
                    return True
            except Exception:
                pass
        # a skip/advance changes `current.id`; abort the stale line (never announce
        # a song the user skipped) - but only after the natural-end check above.
        if (dj.current or {}).get("id") != current_id:
            return False                      # advanced/skipped; the line is stale
        if getattr(dj, "paused", False):
            time.sleep(0.5)
            continue
        try:
            pos, dur = prog()
        except Exception:
            return False
        if not dur or dur <= 0:
            # No reported length (e.g. a stream mpv cannot measure, which is why the
            # player bar shows no total time). Don't give up at a fixed cap - the
            # track still has an end, and the engine advances on `finished()`'s own
            # EOF/idle signal. We wait on that same signal (checked at the top of the
            # loop) so a normal-length song without a reported duration is still
            # announced at the hand-off.
            time.sleep(0.25)
            continue
        remaining = dur - pos
        if remaining <= min(lead, max(0.0, dur * 0.3)):   # short tracks lead sooner
            return True
        # follow the song to its end: extend the bound past the remaining time
        deadline = max(deadline, time.monotonic() + remaining + lead + 2.0)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _creative_line(dj, agent, next_up: bool = False) -> str:
    """A creative DJ line: ask Gemini to write it, fall back to the template.

    The key is read from config BEFORE the check so a key saved in Settings
    (config.json) is honoured - `apply_llm_overrides` pushes it into the globals.
    Otherwise the first check sees the unloaded default and silently degrades to
    the plain template, which is how "the AI doesn't announce like a DJ" happens.
    """
    try:
        config.apply_llm_overrides()        # load a key saved in Settings (config.json)
    except Exception:
        pass
    try:
        import brain
    except Exception:
        brain = None
    lang = config.load_dj_lang()          # the spoken language (default: Indonesian)
    if brain is not None and workerclient.configured():
        try:
            prompt = (agent.lead_prompt(dj, lang) if next_up
                      else agent.dj_prompt(dj, lang))
            if prompt:
                line = brain.free_text(prompt, max_chars=400)
                if line:
                    return line
        except Exception:
            pass
    return (agent.lead_line(dj, lang) if next_up
            else agent.dj_speech(dj, lang))


def _lead_line(dj, agent) -> str:
    """A creative up-next line (Gemini) or a readable template fallback."""
    if not _has_any_next(dj):
        return ""
    return _creative_line(dj, agent, next_up=True)


def _has_any_next(dj) -> bool:
    """Is there an upcoming row after the current one?"""
    try:
        return bool(dj.queue.upcoming(1))
    except Exception:
        return False


def _unlink(path: str) -> None:
    """Best-effort remove a temp clip (never raises)."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _play_clip(clip: str, text: str = "") -> None:
    """
    Play a ready clip: the browser if a tab is attached, mpv otherwise.

    The browser is preferred because that is where the music is being listened
    to, and because it needs no second process and no mixer guesswork. A sink
    that cannot publish (no subscriber on the SSE channel yet) returns False and
    the clip goes to mpv, so a headless `--daemon` still has a voice.
    """
    global _sink
    sink = _sink
    if sink is not None:
        try:
            if sink(clip, text):
                _info("playing the line in the browser")
                return
        except Exception as e:                      # noqa: BLE001 - never raise
            _info(f"the browser sink failed ({e.__class__.__name__}: {e}) - using mpv")
    try:
        _play(clip)
    finally:
        def _cleanup():
            time.sleep(30)
            _unlink(clip)
        threading.Thread(target=_cleanup, daemon=True).start()


def _say(text: str) -> None:
    """Synthesize then play one line, on this (background) thread. Never raises."""
    clip = _synth(text)
    if not clip:
        return
    _play_clip(clip, text)


def _engine() -> str:
    """The only engine is the Worker. No Worker URL means silence, not espeak."""
    return WORKER if workerclient.configured() else ""


def _worker_synth(text: str, path: Path) -> bool:
    """
    Ask the Worker to speak `text` and write the WAV it answers with.

    The Worker does the model mapping (a Live model name becomes its REST TTS
    sibling, because a Worker cannot open the Live socket), the voice
    negotiation and the clip cache. This side writes bytes and reports.
    """
    if not workerclient.configured():
        _info("no Worker URL - the DJ stays silent (Settings -> Worker)")
        return False
    try:
        wav = workerclient.speech(text, voice=voice_name(), model=tts_model())
    except workerclient.WorkerError as e:
        _info(f"the Worker could not speak ({e.kind}): {e.detail}")
        return False
    except Exception as e:                           # noqa: BLE001 - never raise
        _info(f"speech failed: {e.__class__.__name__}: {e}")
        return False
    if not wav or len(wav) < 44:
        _info(f"the Worker returned {len(wav or b'')} bytes - not a clip, staying silent")
        return False
    if wav[:4] != b"RIFF":
        _info("the Worker's audio is not a RIFF/WAV container - staying silent")
        return False
    try:
        path.write_bytes(wav)
    except OSError as e:
        _info(f"could not write the clip: {e}")
        return False
    _info(f"got {len(wav)} bytes of WAV from the Worker")
    return True


def _synth(text: str) -> str | None:
    """Return a path to a spoken clip for `text`, or None if it could not be made.

    There is no offline (espeak) fallback and no direct call to Google: if the
    Worker cannot speak, the DJ stays silent. That is a deliberate narrowing -
    the old code could reach Gemini two different ways, which meant two places
    for "the voice doesn't come out" to hide and only one of them was ever
    exercised.
    """
    engine = _engine()
    if engine == "":
        _info("no Worker URL - the DJ stays silent (no offline voice)")
        return None
    _info(f"engine={engine} synthesizing {len(text)} chars "
          f"(voice={voice_name()}, model={tts_model()})")
    clip = Path(tempfile.mkstemp(suffix=".wav")[1])
    if not _worker_synth(text, clip):
        _info("no audio was produced - the DJ stays silent")
        try:
            clip.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return str(clip)


def _play(path: str) -> None:
    """Play the clip with mpv - the path for a player with no browser attached.

    When a tab is open the clip goes to the page instead (see `_play_clip`);
    this is the headless `--daemon` case, where a second process mixing on the
    OS is the only way to hear anything.
    """
    exe = bins.find("mpv")
    if not exe:
        _info("mpv not found - the spoken line was synthesized but cannot be played; "
              "install mpv (or run with --backend spotube) to hear the DJ")
        return
    try:
        proc = subprocess.Popen([exe, "--really-quiet", "--no-video", "--force-window=no",
                                 "--volume=90", "--", path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _info(f"playing line via mpv (pid {proc.pid})")
    except Exception as e:
        _info(f"mpv could not start: {e.__class__.__name__}: {e}")
