"""
djvoice.py - the spoken DJ, using Gemini's speech generation (the Despina voice).

The on-screen announcer (agent.narrate) is visual. This module makes the same
"why this song / what's next" line actually *speak* on each new track, over the
music, like Spotify's DJ. It is not a plain robotic read:

  1. **Gemini** writes the line and voices it. By default it uses the Gemini
     **Live** native-audio model (`gemini-3.1-flash-live-preview`) over the Live
     API WebSocket, which has no free-tier request cap here and speaks the written
     DJ line in the chosen voice; a `systemInstruction` in the Live setup makes it
     read the line as an on-air DJ announcement instead of chatting. The voice is
     **Despina** (warm, smooth) by default, changeable by voice name. The
     `generateContent` TTS endpoint (`gemini-*-tts-preview`) reads verbatim but can
     trip a REST quota, so it is only used if explicitly selected.
  2. There is no offline/robotic fallback - if Gemini cannot speak (no key, a
     socket failure, or a model-access error) the DJ stays silent. The vocal line
     is a nicety, never a crash, and never a robotic substitute.

The line for each upcoming song is written and synthesized on a background thread
AS SOON as the current track starts, so the announcement is already a ready clip
and plays instantly at the hand-off - a slow text/TTS call can no longer trail
30s into the next song. A fast skipper drops the stale clip (the line for a track
that already advanced is never played), and the engine/webserver are never blocked.
It degrades gracefully in a sandbox with no network or audio.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import wave
from pathlib import Path

import bins
import config


# Optional diagnostic hook: the DJ passes its `_note` here so voice progress (and
# any failure) shows up in the on-screen log drawer, not just in stderr. This is
# the whole point when the voice "doesn't come out" - you can see if it was the
# key, the socket, the audio bytes or the player, in one place.
_log: callable | None = None


def set_logger(fn) -> None:
    """Route voice diagnostics to `fn(msg)` (the DJ's `_note`)."""
    global _log
    _log = fn


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
    """The speech-generation model (gemini-*-live-preview or gemini-*-tts-preview)."""
    return (os.environ.get("SPOTUBE_DJ_TTS_MODEL") or config.GEMINI_DEFAULT_TTS_MODEL
            or "gemini-3.1-flash-live-preview")


def _is_live_model(model: str) -> bool:
    """Is `model` a Live native-audio model (spoken over the Live API WebSocket)?"""
    m = str(model or "").lower()
    return "-live" in m or m.endswith("live-preview") or m.endswith("live")


def _live_url() -> str:
    """The BidiGenerateContent WebSocket URL for the Live API (key in the query)."""
    base = (config.LLM_BASE_URL or "").rstrip("/")
    if not base or "generativelanguage" not in base:
        base = config.GEMINI_DEFAULT_URL
    host = base.replace("https://", "").replace("http://", "").split("/")[0]
    key = urllib.parse.quote(config.LLM_API_KEY)
    return (f"wss://{host}/ws/google.ai.generativelanguage.v1beta."
            f"GenerativeService.BidiGenerateContent?key={key}")


def engines() -> list[str]:
    """Which engines this machine can possibly use, in preference order.

    There is no offline (espeak) fallback: the DJ only speaks via Gemini, so the
    only engine here is "gemini" (when a key is set) - otherwise nothing.
    """
    return ["gemini"] if config.LLM_API_KEY else []


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
            _play_clip(clip)
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
    lead = lead_secs()
    deadline = time.monotonic() + 60.0        # a hard cap so a dead beat cannot hang
    while time.monotonic() < deadline:
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
            time.sleep(0.5)
            continue
        remaining = dur - pos
        if remaining <= min(lead, max(0.0, dur * 0.3)):   # short tracks lead sooner
            return True
        time.sleep(0.25)
    return False


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
    if brain is not None and config.LLM_API_KEY:
        try:
            lang = config.voice_lang(config.load_dj_voice())
            prompt = (agent.lead_prompt(dj, lang) if next_up
                      else agent.dj_prompt(dj, lang))
            if prompt:
                line = brain.free_text(prompt, max_chars=400)
                if line:
                    return line
        except Exception:
            pass
    return (agent.lead_line(dj) if next_up else agent.dj_speech(dj))


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


def _play_clip(clip: str) -> None:
    """Play a ready clip and schedule its temp file cleanup. Never raises."""
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
    _play_clip(clip)


def _engine() -> str:
    """Pick the engine: Gemini if a key is set, else none (no offline fallback)."""
    return "gemini" if config.LLM_API_KEY else ""


# HTTP statuses worth a short retry: a rate limit (429) or a transient server
# blip (5xx) can clear in a couple of seconds. A 4xx like a bad key or a missing
# model is NOT retried - that needs a human fix, and hammering it is pointless.
_TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})


def _post_retry(url: str, payload: dict, headers: dict, retries: int = 2, base_wait: float = 2.0) -> dict:
    """POST to Gemini, retrying transient 429/5xx with a backoff.

    The DJ pre-synthesizes at track start while the station-build / queue-topup
    Gemini calls also run, so a short burst can trip the free-tier 429. A couple
    of bounded retries let that pass instead of quietly silencing the DJ.
    """
    import brain
    for attempt in range(retries + 1):
        try:
            return brain._post(url, payload, headers)
        except urllib.error.HTTPError as e:
            if e.code not in _TRANSIENT_HTTP or attempt >= retries:
                raise
            delay = base_wait * (2 ** attempt)        # 2s, 4s
            try:
                ra = float(e.headers.get("Retry-After") or 0)
                if ra > 0:
                    delay = ra
            except Exception:
                pass
            delay = max(0.5, min(delay, 30.0))
            _info(f"gemini (generateContent): HTTP {e.code} - retrying in {delay:.0f}s "
                  f"(attempt {attempt + 1}/{retries})")
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt >= retries:
                raise
            delay = max(0.5, min(base_wait * (2 ** attempt), 30.0))
            _info(f"gemini (generateContent): transient "
                  f"{e.__class__.__name__} - retrying in {delay:.0f}s")
            time.sleep(delay)
    raise RuntimeError("unreachable")   # pragma: no cover


def _gemini_synth(text: str, path: Path) -> bool:
    """Ask Gemini's speech model to speak `text` with the configured voice.

    The response is base64 PCM (signed 16-bit, mono, 24 kHz by default); we wrap
    it as a WAV so mpv can play it. Returns True on success; a missing key, an
    HTTP error or a bad payload is False (the caller stays silent - there is no
    offline/robotic fallback).
    """
    if not config.LLM_API_KEY:
        _info("gemini (generateContent): no API key")
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
        data = _post_retry(url, payload, brain._gemini_headers())
    except Exception as e:
        _info(f"gemini (generateContent) failed: {e.__class__.__name__}: {e}")
        return False
    try:
        part = (data["candidates"][0]["content"]["parts"][0])
        inline = part.get("inlineData") or {}
        b64 = inline.get("data")
        if not b64:
            _info("gemini (generateContent) returned no audio inlineData")
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
        ok = path.exists() and path.stat().st_size > 0
        _info(f"gemini (generateContent) spoke: {len(pcm)} PCM bytes @ {rate} Hz "
              f"({path.stat().st_size if ok else 0} bytes wav)")
        return ok
    except Exception as e:
        _info(f"gemini (generateContent) bad payload: {e.__class__.__name__}: {e}")
        return False


# --- a tiny dependency-free WebSocket (RFC 6455) client, only for the Live API.
# The project deliberately stays single-dependency (yt-dlp), and websocket-client
# cannot be pip-installed everywhere, so this implements just enough of the client
# for one text -> audio turn: connect, upgrade, send a masked text frame, read the
# server's JSON frames. It is not a general websocket library.
def _ws_read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("websocket closed")
        buf += chunk
    return buf


def _ws_send_frame(sock, opcode: int, payload: bytes, mask: bool = True) -> None:
    """Send a single frame on the socket (client->server frames MUST be masked)."""
    length = len(payload)
    head = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        head.append((0x80 if mask else 0) | length)
    elif length < 65536:
        head.append((0x80 if mask else 0) | 126)
        head += length.to_bytes(2, "big")
    else:
        head.append((0x80 if mask else 0) | 127)
        head += length.to_bytes(8, "big")
    key = os.urandom(4) if mask else b""
    if mask:
        head += key
    body = bytes(b ^ key[i % 4] for i, b in enumerate(payload)) if mask else payload
    sock.sendall(bytes(head) + body)


def _ws_read_frame(sock) -> tuple[int, bytes]:
    """Read one server frame; returns (opcode, payload). Server frames are unmasked."""
    b1, b2 = _ws_read_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = int.from_bytes(_ws_read_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_ws_read_exact(sock, 8), "big")
    key = _ws_read_exact(sock, 4) if masked else b""
    payload = _ws_read_exact(sock, length)
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _ws_read_message(sock) -> tuple[int, bytes]:
    """Read one complete WebSocket message, reassembling continuation frames.

    Returns (opcode, payload) where `opcode` is the first (message-typing) frame's
    opcode (0x1 text, 0x2 binary, 0x8 close, 0x9 ping, 0xA pong). Continuation
    frames (opcode 0) are reassembled into `payload`. Pings/pongs are surfaced to
    the caller (who answers pings) rather than swallowed, so the DJ log can show
    whether the server is alive even when it never completes `setup`.
    """
    first_opcode = None
    buf = b""
    while True:
        b1, b2 = _ws_read_exact(sock, 2)
        opcode = b1 & 0x0F
        fin = bool(b1 & 0x80)
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(_ws_read_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(_ws_read_exact(sock, 8), "big")
        key = _ws_read_exact(sock, 4) if masked else b""
        payload = _ws_read_exact(sock, length)
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if opcode == 0:                                  # continuation
            if first_opcode is not None:
                buf += payload
        else:
            first_opcode = opcode
            buf = payload
        if fin:
            return first_opcode, buf


def _ws_close_reason(payload: bytes) -> str:
    """Turn a WebSocket close frame payload into a readable status + reason."""
    if not payload:
        return "no status (empty close frame)"
    try:
        code = int.from_bytes(payload[:2], "big")
        reason = payload[2:].decode("utf-8", "replace").strip()
    except Exception:
        return f"unparseable close payload {payload!r}"
    return (f"status {code}" + (f" '{reason}'" if reason else " (no reason)"))


def _frame_repr(opcode: int, data: bytes) -> str:
    """A short human-readable description of one WS frame, for the log."""
    names = {0: "continuation", 1: "text", 2: "binary", 8: "close", 9: "ping", 10: "pong"}
    name = names.get(opcode, "opcode-%d" % opcode)
    preview = ""
    if opcode in (1, 2):
        preview = " " + repr(data[:60])
    return f"{name} ({len(data)} bytes){preview}"


def _decode_ws_message(data: bytes):
    """Return (message_dict, raw_text) for a JSON text frame, else None.

    The Gemini Live API sends its structured messages (setupComplete, serverContent,
    toolCall...) as WebSocket **binary** frames whose body is JSON - not as text
    frames. So this is used for both opcode 1 (text) and opcode 2 (binary) frames;
    both carry UTF-8 JSON. Returns None if the bytes are not valid JSON.
    """
    try:
        txt = data.decode("utf-8")
    except Exception:
        return None
    try:
        return json.loads(txt), txt
    except Exception:
        return None


def _ws_connect(url: str, timeout: float = 30.0) -> socket.socket:
    """Open + handshake a websocket connection; returns the wrapped socket.

    TLS is applied only for a `wss://` scheme (the Live endpoint, production);
    a `ws://` URL connects in plaintext, which is how the integration tests run
    the client against a local mock server. The API key rides in the URL query
    (`?key=...`), exactly as the Live API get-started example does; the WebSocket
    upgrade does not take a key header.
    """
    u = urllib.parse.urlparse(url)
    host, port = u.hostname, (u.port or (443 if u.scheme == "wss" else 80))
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    raw = socket.create_connection((host, port), timeout=timeout)
    if u.scheme == "wss":
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw, server_hostname=host)
    else:
        sock = raw
    sock.settimeout(timeout)
    nonce = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\n"
           f"Host: {host}\r\n"
           f"Upgrade: websocket\r\n"
           f"Connection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {nonce}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        c = sock.recv(1)
        if not c:
            raise EOFError("no websocket handshake")
        head += c
    status = head.decode("latin1").split("\r\n", 1)[0]
    if " 101 " not in status:
        raise OSError(f"websocket handshake refused: {status}")
    return sock


def _live_collect(sock, timeout: float, rate: int) -> tuple[bytes, int, bool, bool]:
    """Collect one Live turn's audio; -> (audio, rate, compressed, finished).

    `audio` is raw bytes the server sent (PCM or a container, per the mimeType);
    `finished` is True when the server signalled turnComplete (or the socket closed).
    """
    audio = b""
    compressed = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            opcode, data = _ws_read_message(sock)
        except socket.timeout:
            break
        except Exception as e:
            _info(f"gemini (live): socket read failed: {e.__class__.__name__}: {e}")
            return audio, rate, compressed, False
        if opcode == 8:                                   # close
            _info("gemini (live): server sent close -> " + _ws_close_reason(data))
            return audio, rate, compressed, True
        if opcode == 9:                                   # ping -> pong
            _info("gemini (live): <- ping (server alive)")
            try:
                _ws_send_frame(sock, 10, data)
            except Exception:
                pass
            continue
        if opcode == 10:                                  # pong
            continue
        # The Live API wraps its JSON in BINARY frames (opcode 2). A JSON frame holds
        # serverContent (with inbox ASCII base64 audio), so try to decode it as JSON.
        # Only a binary frame that is NOT valid JSON is treated as raw audio bytes.
        if opcode in (1, 2):
            decoded = _decode_ws_message(data)
            if decoded is None:
                if opcode == 2:                           # non-JSON binary = raw audio
                    audio += data
                continue
            msg, _raw = decoded
            sc = msg.get("serverContent") or {}
            parts = (sc.get("modelTurn") or {}).get("parts") or []
            for part in parts:
                inline = part.get("inlineData") or {}
                b64 = inline.get("data")
                if not b64:
                    continue
                mime = str(inline.get("mimeType") or "")
                if any(x in mime for x in ("ogg", "opus", "mpeg", "mp3", "webm")):
                    compressed = True
                elif "rate=" in mime:
                    try:
                        rate = int(mime.split("rate=")[1].split(";")[0])
                    except (ValueError, IndexError):
                        rate = 24000
                audio += base64.b64decode(b64)
            if sc.get("turnComplete"):
                return audio, rate, compressed, True
            if sc.get("interrupted"):
                _info("gemini (live): turn interrupted")
    return audio, rate, compressed, False


# Every Gemini Live native-audio model speaks with any of the 30 TTS voices; the
# list is the same as the generateContent TTS voices, so a chosen voice is always
# valid on the Live path. We only fall back to a guaranteed-initially-available voice
# if the *server* closes during setup (a genuine rejection of the config), so the
# DJ keeps speaking instead of staying silent. We never swap the voice when setup
# succeeds but just no audio arrives - that is a different failure.
_LIVE_FALLBACK_VOICE = "kore"


def _live_setup(sock, setup: dict, timeout: float = 10.0) -> str:
    """Send one Live `setup` and wait for `setupComplete`.

    Returns a status string: "ok" (setupComplete), "reject" (the server closed or
    sent goAway during setup - a genuine refusal of the config), "timeout" (the
    connection was accepted but the server never completed setup), or "error".
    The caller treats "reject" as a config/voice problem worth retrying differently,
    and "timeout"/"error" as a session that never became ready (usually an API-key
    or model-access issue, not a voice problem).
    """
    _ws_send_frame(sock, 1, json.dumps({"setup": setup}).encode())
    _info("gemini (live): setup:: " + json.dumps(setup)[:320])
    deadline = time.monotonic() + timeout
    status = "timeout"
    while time.monotonic() < deadline:
        try:
            opcode, data = _ws_read_message(sock)
        except socket.timeout:
            break
        except EOFError:
            _info("gemini (live): socket closed before setupComplete")
            status = "reject"
            break
        except Exception as e:
            _info(f"gemini (live): setup read failed: {e.__class__.__name__}: {e}")
            status = "error"
            break
        _info("gemini (live): <- " + _frame_repr(opcode, data))
        if opcode == 8:                                   # close
            _info("gemini (live): server closed during setup -> " +
                  _ws_close_reason(data))
            status = "reject"
            break
        if opcode == 9:                                   # ping -> pong
            try:
                _ws_send_frame(sock, 10, data)
            except Exception:
                pass
            continue
        if opcode == 10:                                  # pong
            continue
        # The Live API wraps its JSON in BINARY frames (opcode 2), not text frames.
        # Accept both so the server's setupComplete / goAway are recognised regardless.
        if opcode in (1, 2):
            decoded = _decode_ws_message(data)
            if decoded is None:
                _info("gemini (live): setup-phase non-JSON " + repr(data)[:200])
                continue
            msg, _raw = decoded
            if "setupComplete" in msg:
                _info("gemini (live): setupComplete")
                status = "ok"
                break
            if "goAway" in msg:
                _info(f"gemini (live): goAway -> {msg.get('goAway')}")
                status = "reject"
                break
            _info("gemini (live): setup-phase message " + json.dumps(msg)[:300])
        # continuation / anything else: logged above, keep waiting
    if status == "timeout":
        _info(f"gemini (live): server never sent setupComplete within {timeout:.0f}s")
    return status


def _send_text_turn(sock, text: str) -> tuple[bytes, int, bool, bool]:
    """Send the line (realtimeInput, then clientContent as a backstop) and collect."""
    for attempt in ({"realtimeInput": {"text": text}},
                    {"clientContent": {"turns": [{"role": "user",
                                                  "parts": [{"text": text}]}],
                                       "turnComplete": True}}):
        try:
            _ws_send_frame(sock, 1, json.dumps(attempt).encode())
        except Exception as e:
            _info(f"gemini (live): send failed: {e.__class__.__name__}: {e}")
            return b"", 24000, False, False
        audio, rate, compressed, finished = _live_collect(sock, timeout=12.0, rate=24000)
        if audio or finished:
            return audio, rate, compressed, finished
    return b"", 24000, False, False


def _gemini_live_synth(text: str, path: Path) -> bool:
    """Ask the Gemini Live native-audio model to speak `text` over the WebSocket.

    The default path for `*-live-*` models (e.g. `gemini-3.1-flash-live-preview`),
    which speak over the Live API WebSocket and have no free-tier request cap here
    (unlike the generateContent REST TTS endpoint, which 429s). The Live model is
    conversational by design, so the setup includes a `systemInstruction` telling
    it to READ the sent line as an on-air DJ announcement and not answer it like a
    chatbot. The server replies with PCM; we wrap it as a WAV. Returns False on any
    failure, and the caller stays silent.
    """
    if not config.LLM_API_KEY:
        _info("gemini (live): no API key")
        return False
    model = tts_model()
    if not _is_live_model(model):
        return False
    voice = voice_name() or _LIVE_FALLBACK_VOICE

    def setup_for(v: str) -> dict:
        # The raw-wire BidiGenerateContentSetup schema (ai.google.dev/api/live) is
        # authoritative: `model` is `models/{model}`; `responseModalities` and
        # `speechConfig` live **inside** `generationConfig` (NOT at the top level,
        # which the server 1007-rejects). `systemInstruction` is a TOP-LEVEL setup
        # field (a sibling of generationConfig). We add one so the Live model reads
        # the line we send as an ON-AIR ANNOUNCEMENT instead of answering it like a
        # chatbot ("anything else you'd like to hear?"). No outputAudioTranscription.
        return {
            "model": "models/" + model,
            "systemInstruction": {"parts": [{
                "text": (
                    "You are a warm, smooth, energetic radio DJ announcer on air. "
                    "The text you are sent is the announcement to SAY OUT LOUD "
                    "word for word: name the track, the reason it is playing and "
                    "what is up next. Read it exactly as written. Do not ask the "
                    "listener anything, do not add a question, and do not carry on "
                    "a conversation - speak the script and stop."
                ),
            }]},
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                    "voiceName": v}}},
            },
        }

    def attempt(v: str) -> tuple[bool, str]:
        """Run a full connect+setup+turn on a fresh connection.

        Returns (spoke_ok, status). `status` is the setup outcome from
        `_live_setup` ("ok"/"reject"/"timeout"/"error"); when the setup succeeds it
        is "ok". The caller only swaps the voice when status == "reject".
        """
        sock = None
        label = "voice=%s" % v
        status = "error"
        try:
            sock = _ws_connect(_live_url())
            _info(f"gemini (live): connected, model={model}, {label}")
            status = _live_setup(sock, setup_for(v))
            if status != "ok":
                return False, status
        except Exception as e:
            _info(f"gemini (live): connect/setup failed ({label}): "
                  f"{e.__class__.__name__}: {e}")
            return False, "error"
        finally:
            # a failed setup left an unneeded connection; close it before returning
            if status != "ok" and sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        try:
            if sock is None:
                return False, "error"
            audio, rate, compressed, _finished = _send_text_turn(sock, text)
        except Exception as e:
            _info(f"gemini (live): turn failed ({label}): {e.__class__.__name__}: {e}")
            return False, "error"
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if not audio:
            _info(f"gemini (live): setup OK but no audio came back ({label})")
            return False, "ok"
        if compressed:
            # a container (ogg/opus/mp3) is written as-is; mpv sniffs the content
            # and does not care that the temp name happens to end in .wav
            path.write_bytes(audio)
            ok = path.exists() and path.stat().st_size > 0
            _info(f"gemini (live): spoke with voice {v}, wrote {len(audio)} bytes "
                  f"of audio container")
            return ok, "ok"
        _pcm_to_wav(audio, rate, path)
        ok = path.exists() and path.stat().st_size > 0
        _info(f"gemini (live): spoke with voice {v}, {len(audio)} PCM bytes "
              f"@ {rate} Hz ({path.stat().st_size if ok else 0} bytes wav)")
        return ok, "ok"

    # 1. Use exactly the configured voice. Never pre-emptively substitute it.
    spoke, status = attempt(voice)
    if spoke:
        return True
    # 2. Only if the server actually refused the configured voice during setup do we
    #    retry on a fresh connection with a voice guaranteed to be available, so the
    #    DJ still speaks at all. A successful setup with no audio (status "ok") is
    #    NOT a voice problem, so we do not change the voice for it; and a timeout/error
    #    means the session never became ready, not that the voice is wrong.
    if status == "reject" and voice.lower() != _LIVE_FALLBACK_VOICE:
        spoke, status = attempt(_LIVE_FALLBACK_VOICE)
        if spoke:
            return True
        if status != "ok":
            _info("gemini (live): the server refused setup even with the fallback voice")
            return False
    if status in ("timeout", "error"):
        _info(f"gemini (live): the Live session never became ready for {model} "
              f"(setup was {status}). The most common cause is that the API key or "
              f"project does not have access to this preview model (or the Live API "
              f"is not enabled for it) - it is not a voice problem.")
    _info(f"gemini (live): could not speak with voice {voice} - falling back")
    return False


def _pcm_to_wav(pcm: bytes, rate: int, path: Path) -> None:
    """Wrap raw signed-16-bit mono PCM in a WAV container (mpv plays WAV natively)."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(max(8000, rate))
        wf.writeframes(pcm)


def _synth(text: str) -> str | None:
    """Return a path to a spoken clip for `text`, or None if it could not be made.

    There is no offline (espeak) fallback: if Gemini cannot speak, the DJ stays
    silent. The only engine is "gemini"; anything else returns None.
    """
    engine = _engine()
    if engine == "":
        _info("no Gemini API key - the DJ stays silent (no offline voice)")
        return None
    _info(f"engine={engine} synthesizing {len(text)} chars")
    clip = Path(tempfile.mkstemp(suffix=".wav")[1])
    ok = False
    used = ""
    # The model type decides the path ONCE: the Live native-audio model (-live-,
    # the default) speaks over the WebSocket and has no REST quota cap here; a
    # generateContent TTS model (-tts-) reads the line verbatim over REST but can
    # trip a rate limit. We never cross-fall back to the other, so the DJ only
    # ever announces. No espeak fallback either.
    if _is_live_model(tts_model()):
        ok = _gemini_live_synth(text, clip)
        used = "live"
    else:
        ok = _gemini_synth(text, clip)
        used = "generateContent"
    if not ok:
        _info(f"no audio was produced via {used or 'gemini'} - the DJ stays silent")
        try:
            clip.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    _info(f"produced {clip.stat().st_size} bytes via {used}")
    return str(clip)


def _play(path: str) -> None:
    """Play the clip with mpv (a second process mixes with the music on the OS)."""
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
