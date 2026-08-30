"""
djvoice.py - the spoken DJ, using Gemini's speech generation (the Despina voice).

The on-screen announcer (agent.narrate) is visual. This module makes the same
"why this song / what's next" line actually *speak* on each new track, over the
music, like Spotify's DJ. It is not a plain robotic read:

  1. **Gemini** writes the line and voices it. By default it uses the Gemini
     **Live** native-audio model (`gemini-3.1-flash-live-preview`) over the Live
     API WebSocket, so the DJ speaks in a warm, playful voice - every track gets
     its own phrasing. The voice is **Despina** (warm, smooth) by default,
     changeable by voice name. If the configured model is a `*-tts-preview`
     model, it instead uses the synchronous `generateContent` speech endpoint.
  2. **espeak / espeak-ng** is the offline fallback (robotic, but no key/network).
  3. If there is no key and no espeak, it stays silent - the vocal line is a
     nicety, never a crash.

Speech is generated and played on a single worker thread, so a fast skipper only
hears the latest line and the engine/webserver are never blocked. It degrades
gracefully in a sandbox with no network or audio.
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


def lead_secs() -> float:
    """Seconds before a track ends that the DJ starts the next-up announcement."""
    return getattr(config, "DJ_LEAD_SECS", 10.0)


def speak_for(dj) -> None:
    """[back-compat] Speak the current-track line now (see `speak_intro`)."""
    speak_intro(dj)


def speak_intro(dj) -> None:
    """Announce the *current* song now - used for the first track of a set."""
    set_logger(getattr(dj, "_note", None))
    if not enabled(dj):
        _info("voice is off (env or the DJ's on/off setting)")
        return
    try:
        import agent
        text = _creative_line(dj, agent, next_up=False)
    except Exception as e:
        _info(f"intro line failed: {e.__class__.__name__}: {e}")
        return
    _info(f"announcing track now (engine={_engine() or 'none'}, "
          f"model={tts_model()}, voice={voice_name()})")
    _queue(text)


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
    """Generate the up-next line, wait until near the end, then speak it."""
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
    if not _wait_lead(dj, current_id):
        return
    _queue(text)


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


def _queue(text: str) -> None:
    """Enqueue a line for the single speech worker (serializes rapid changes)."""
    global _pending
    if not text:
        return
    global _worker
    with _cond:
        _pending = text
        if not _worker:
            _worker = True
            threading.Thread(target=_worker_loop, daemon=True).start()


def _creative_line(dj, agent, next_up: bool = False) -> str:
    """A creative DJ line: ask Gemini to write it, fall back to the template."""
    if config.LLM_API_KEY:
        try:
            import brain
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
        data = brain._post(url, payload, brain._gemini_headers())
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


def _decode_ws_message(data: bytes):
    """Return (message_dict, raw_text) for an opcode-1 text frame, else None."""
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
            opcode, data = _ws_read_frame(sock)
        except Exception as e:
            _info(f"gemini (live): socket read failed: {e.__class__.__name__}: {e}")
            return audio, rate, compressed, False
        if opcode == 8:                                   # close
            _info("gemini (live): server sent close -> " + _ws_close_reason(data))
            return audio, rate, compressed, True
        if opcode == 9:                                   # ping -> pong
            try:
                _ws_send_frame(sock, 10, data)
            except Exception:
                pass
            continue
        if opcode == 10:                                  # pong
            continue
        if opcode == 2:                                   # raw audio bytes
            audio += data
            continue
        if opcode == 1:
            try:
                msg = json.loads(data)
            except Exception:
                continue
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
# DJ keeps speaking instead of dropping to espeak/silence. We never swap the voice
# when setup succeeds but just no audio arrives - that is a different failure.
_LIVE_FALLBACK_VOICE = "kore"


def _live_setup(sock, setup: dict, timeout: float = 15.0) -> bool:
    """Send one Live `setup` and wait for `setupComplete`.

    Returns True only once the server answers setupComplete; otherwise logs the
    exact reason (close status/reason, goAway, non-JSON frame, or a timeout) and
    returns False, so the caller can try another setup on a fresh connection.
    """
    _ws_send_frame(sock, 1, json.dumps({"setup": setup}).encode())
    _info("gemini (live): setup:: " + json.dumps(setup)[:300])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            opcode, data = _ws_read_frame(sock)
        except socket.timeout:
            _info(f"gemini (live): server never sent setupComplete within {timeout:.0f}s")
            return False
        except Exception as e:
            _info(f"gemini (live): setup read failed: {e.__class__.__name__}: {e}")
            return False
        if opcode == 8:                                   # close
            _info("gemini (live): server closed during setup -> " +
                  _ws_close_reason(data))
            return False
        if opcode == 1:
            decoded = _decode_ws_message(data)
            if decoded is None:
                _info("gemini (live): setup-phase non-JSON text frame " +
                      repr(data)[:200])
                continue
            msg, _raw = decoded
            if "setupComplete" in msg:
                _info("gemini (live): setupComplete")
                return True
            if "goAway" in msg:
                _info(f"gemini (live): goAway -> {msg.get('goAway')}")
                return False
            _info("gemini (live): setup-phase message " + json.dumps(msg)[:300])
    _info(f"gemini (live): server never sent setupComplete within {timeout:.0f}s")
    return False


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

    This is the path for `*-live-*` models (default `gemini-3.1-flash-live-preview`),
    which only speak over the Live API's BidiGenerateContent socket - the
    synchronous generateContent TTS endpoint is a different family and returns
    nothing for them (which is why the old default model silently fell back).
    The server replies with PCM; we wrap it as a WAV. Returns False on any failure
    so the caller can try the TTS endpoint / espeak / silence.
    """
    if not config.LLM_API_KEY:
        _info("gemini (live): no API key")
        return False
    model = tts_model()
    if not _is_live_model(model):
        return False
    voice = voice_name() or _LIVE_FALLBACK_VOICE

    def setup_for(v: str) -> dict:
        # The api/live reference is authoritative: `responseModalities` and
        # `speechConfig` both sit **inside** `generationConfig` on the setup
        # message. (A quickstart snippet puts responseModalities at the top of
        # setup, but the real server rejects that with 1007.) No systemInstruction:
        # the line is already written as text and we only want it voiced as-is.
        return {
            "model": "models/" + model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                    "voiceName": v}}},
            },
        }

    def attempt(v: str) -> tuple[bool, bool]:
        """Run a full connect+setup+turn on a fresh connection.

        Returns (spoke_ok, setup_rejected). setup_rejected is True when the server
        closed/refused during setup (a real config problem), distinct from "setup
        OK but no audio came back".
        """
        sock = None
        label = "voice=%s" % v
        setup_ok = False
        try:
            sock = _ws_connect(_live_url())
            _info(f"gemini (live): connected, model={model}, {label}")
            setup_ok = _live_setup(sock, setup_for(v))
            if not setup_ok:
                return False, True
        except Exception as e:
            _info(f"gemini (live): connect/setup failed ({label}): "
                  f"{e.__class__.__name__}: {e}")
            return False, True
        finally:
            # a failed setup left an unneeded connection; close it before returning
            if not setup_ok and sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        try:
            if not setup_ok or sock is None:
                return False, True
            audio, rate, compressed, _finished = _send_text_turn(sock, text)
        except Exception as e:
            _info(f"gemini (live): turn failed ({label}): {e.__class__.__name__}: {e}")
            return False, False
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if not audio:
            _info(f"gemini (live): setup OK but no audio came back ({label})")
            return False, False
        if compressed:
            # a container (ogg/opus/mp3) is written as-is; mpv sniffs the content
            # and does not care that the temp name happens to end in .wav
            path.write_bytes(audio)
            ok = path.exists() and path.stat().st_size > 0
            _info(f"gemini (live): spoke with voice {v}, wrote {len(audio)} bytes "
                  f"of audio container")
            return ok, False
        _pcm_to_wav(audio, rate, path)
        ok = path.exists() and path.stat().st_size > 0
        _info(f"gemini (live): spoke with voice {v}, {len(audio)} PCM bytes "
              f"@ {rate} Hz ({path.stat().st_size if ok else 0} bytes wav)")
        return ok, False

    # 1. Use exactly the configured voice. Never pre-emptively substitute it.
    spoke, _setup_rejected = attempt(voice)
    if spoke:
        return True
    # 2. Only if the server actually refused the configured voice during setup do we
    #    retry on a fresh connection with a voice guaranteed to be available, so the
    #    DJ still speaks at all. A successful setup with no audio is NOT a voice
    #    problem, so we do not change the voice for it (espeak handles that case).
    if _setup_rejected and voice.lower() != _LIVE_FALLBACK_VOICE:
        spoke, _ = attempt(_LIVE_FALLBACK_VOICE)
        if spoke:
            return True
    _info(f"gemini (live): could not speak with voice {voice} - falling back")
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
    if engine == "":
        _info("no speech engine: no Gemini key and no espeak installed")
        return None
    _info(f"engine={engine} synthesizing {len(text)} chars")
    clip = Path(tempfile.mkstemp(suffix=".wav")[1])
    ok = False
    used = ""
    if engine == "gemini":
        # a Live native-audio model (-live-) speaks over the WebSocket, a TTS
        # model (-tts-) over generateContent. Try the one the model prefers first,
        # then the other, so either works; espeak is the offline stamp.
        if _is_live_model(tts_model()):
            ok = _gemini_live_synth(text, clip)
            used = "live"
            if not ok:
                ok = _gemini_synth(text, clip)
                used = "generateContent"
        else:
            ok = _gemini_synth(text, clip)
            used = "generateContent"
            if not ok:
                ok = _gemini_live_synth(text, clip)
                used = "live"
        if not ok and (bins.find("espeak-ng") or bins.find("espeak")):
            _info("gemini said nothing - falling back to espeak")
            ok = _espeak_synth(text, clip)
            used = "espeak"
    elif engine == "espeak":
        ok = _espeak_synth(text, clip)
        used = "espeak"
    if not ok:
        _info(f"no audio was produced ({used or 'unknown engine'})")
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
