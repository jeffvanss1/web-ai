"""
The AI DJ: a Gemini Live API text agent that talks about and runs the queue.

This is the Spotify-DJ-style assistant. It is *not* a voice call (the app streams
licensed YouTube Music audio and has no TTS/STT path); it is a text conversation
with a DJ-host persona that:

  * knows what is playing and what is queued (a live snapshot of the set),
  * title the set with the Daylist name the mixer already computes,
  * acts on the player through the same verbs the buttons use - play a mood,
    skip, like, dislike, pause/resume, volume, mix from your likes - via the
    Gemini Live API's function-calling (toolCall -> toolResponse) loop.

The Live API is a stateful WebSocket (WSS). We connect server-to-server so the
Gemini key stays in ~/.spotube-dj/config.json and never reaches the browser. A
turn is one request/response round: connect, send `setup`, send the conversation
history plus the new user turn (`clientContent.turnComplete`), read text
(`serverContent.modelTurn.parts`) and any `toolCall`, run the tool, send
`toolResponse`, and keep reading until `turnComplete`.

Everything is deliberately dependency-light: `websocket-client` is the only new
require and is imported lazily so the rest of the app keeps working (and the
tests can inject a fake connection) when it is not installed.
"""

from __future__ import annotations

import json
import threading
import urllib.parse

import config
import taste

# The Live API endpoint (Google's stateful WSS). The host is a constant rather
# than LLM_BASE_URL on purpose: a localhost Ollama base URL must never hijack the
# real-time Google session the way it would a generateContent call.
LIVE_HOST = "generativelanguage.googleapis.com"
LIVE_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"

# The key check and the WS handshake both raise this so the web layer can show a
# "how do I make this work" hint instead of a bare 500.
class LiveUnavailable(RuntimeError):
    """The DJ chat object is not usable (no key, no websockets pkg, no network)."""


def _saved(key: str) -> str:
    """A config.json value, read through the cached reader so it is cheap."""
    try:
        return str(config.load_llm_config().get(key) or "")
    except Exception:
        return ""


def api_key() -> str:
    """The Gemini key: the module global first, then the saved config.json."""
    return str(config.LLM_API_KEY or _saved("LLM_API_KEY") or "").strip()


def live_model() -> str:
    """The Live-capable model, preferring any saved/configured override."""
    return str(config.LIVE_MODEL or _saved("LIVE_MODEL")
               or config.GEMINI_DEFAULT_LIVE_MODEL or "").strip()


def live_ws_url(key: str, host: str = LIVE_HOST) -> str:
    """-> the wss:// URL the Live session connects to. Never raises on a bad key."""
    if not (key or "").strip():
        raise LiveUnavailable("set a Gemini key in Settings, then the DJ can talk")
    return f"wss://{host}{LIVE_PATH}?key={urllib.parse.quote(str(key), safe='')}"


def build_setup(model: str, system_prompt: str, tools: list[dict]) -> dict:
    """The `setup` message: configuration, personality and the tool schema."""
    setup: dict = {
        "model": f"models/{model}",
        "generationConfig": {"responseModalities": ["TEXT"]},
    }
    if system_prompt:
        setup["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    if tools:
        setup["tools"] = [{"functionDeclarations": tools}]
    # the model-choosing handshake: some Live models answer a `setup` that names a
    # modality they do not serve with a clear error rather than silently degrading
    return {"setup": setup}


# ------------------------------------------------------------------ tools
# These are the function declarations exposed to the model. The names map to the
# `execute_tool` dispatcher, which calls the same DJ verbs the buttons use. Keep
# the schema small so the model actually calls them instead of chattering.
def tool_declarations() -> list[dict]:
    return [
        {"name": "get_status",
         "description": "What is playing and what is queued, right now.",
         "parameters": {"type": "object",
                        "properties": {},
                        "required": []}},
        {"name": "play",
         "description": "Search YouTube Music and start a mix for a song, artist or "
                        "mood. Use this whenever the listener asks for music.",
         "parameters": {"type": "object",
                        "properties": {"query": {"type": "string",
                                                 "description": "song / artist / mood to play"}},
                        "required": ["query"]}},
        {"name": "mix",
         "description": "Build a mix from the artist and mood the profile likes.",
         "parameters": {"type": "object",
                        "properties": {},
                        "required": []}},
        {"name": "skip",
         "description": "Move to the next track in the queue.",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "like",
         "description": "Love the song that is playing (adds it to the liked profile).",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "dislike",
         "description": "Never play this track/artist again, and move on.",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "pause",
         "description": "Pause playback.",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "resume",
         "description": "Resume playback.",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "volume",
         "description": "Set the volume.",
         "parameters": {"type": "object",
                        "properties": {"level": {"type": "integer",
                                                 "description": "0 to 100"}},
                        "required": ["level"]}},
    ]


_NAME_TO_VERB = {
    "play": "play", "skip": "next", "dislike": "remove_queue",
}


def execute_tool(ctx, name: str, args: dict | None) -> dict:
    """
    Run one function call against the live DJ. -> a JSON-serialisable result.

    `ctx` is the web Context; actions that build a queue go through `start_job`
    so a chat message never blocks the state socket for the seconds a mix takes.
    """
    args = args or {}
    dj = ctx.dj
    if name == "get_status":
        return dj_snapshot(ctx)
    if name == "play":
        q = str(args.get("query") or "").strip() or dj.request or ""
        if not q:
            return {"note": "say what you want to hear first"}
        _start(ctx, lambda: (dj.start(q)))
        return {"note": f"building a mix from {q!r}"}
    if name == "mix":
        _start(ctx, lambda: dj.taste_mix())
        return {"note": "mixing from your likes"}
    if name == "skip":
        t = dj.skip()
        return {"note": "next up" if t else "nothing to skip to"}
    if name == "like":
        if not dj.current:
            return {"note": "nothing playing to love yet"}
        if dj.is_liked(dj.current):
            dj.unlike()
            return {"note": "unloved"}
        dj.like()
        return {"note": "loved it"}
    if name == "dislike":
        if not dj.current:
            return {"note": "nothing playing to dislike"}
        taste.record_dislike(dj.current)
        dj.state = config.load_state()
        dj.skip()
        return {"note": "won't play that again"}
    if name == "pause":
        dj.pause()
        return {"note": "paused"}
    if name == "resume":
        dj.resume()
        return {"note": "resumed"}
    if name == "volume":
        try:
            level = int(float(str(args.get("level") or "")))
        except (TypeError, ValueError):
            return {"note": "volume needs a number 0-100"}
        level = max(0, min(100, level))
        dj.volume(level)
        return {"note": f"volume {level}%"}
    return {"error": f"unknown tool {name!r}"}


def _start(ctx, target) -> None:
    """Start a queue-building job if one is not already running; never raise."""
    try:
        ctx.start_job(target)
    except Exception:
        pass


# ------------------------------------------------------------- snapshot
def _current_why(dj, info: dict, np: dict) -> str:
    """A factual reason for the current song, from what the mixer actually did."""
    if not np:
        return ""                       # nothing playing, so no "why this song"
    parts: list[str] = []
    req = str(dj.request or "").strip()
    if req:
        parts.append(f"you asked for {req!r}")
    station = str(getattr(dj, "station", "") or "").strip()
    if station:
        parts.append(f"it's around the {station} station")
    if np.get("mixed"):
        parts.append("it's one of your picks (from your likes)")
    why = str(info.get("why") or "").strip()
    if why:
        # the planner/offline reason already reads like a reason ("90s trip hop, dark")
        parts.append(why)
    vibe = str(info.get("vibe") or "").strip()
    if not parts and vibe:
        parts.append(f"it fits the '{vibe}' set")
    return "; ".join(parts)


def dj_snapshot(ctx) -> dict:
    """A compact read of the set the DJ is hosting, for the system prompt and tools."""
    dj = ctx.dj
    np = dj.current or {}
    info = dj.info or {}
    upnext: list[dict] = []
    try:
        upnext = [dict(x) for x in dj.queue.upcoming(8)]
    except Exception:
        pass
    try:
        prof = taste.load_state()
        artists = sorted((prof.get("artists") or {}).items(), key=lambda kv: -kv[1])
        genres = sorted((prof.get("genres") or {}).items(), key=lambda kv: -kv[1])
        liked_artists = [str(k) for k, v in artists if v > 0][:5]
        moods = [str(k) for k, v in genres if v > 0][:5]
    except Exception:
        liked_artists, moods = [], []
    return {
        "now": (f"{np.get('artist') or '?'} - {np.get('title') or '?'}"
                if np else "nothing playing"),
        "vibe": str(info.get("vibe") or ""),
        "why": _current_why(dj, info, np),
        "queued": len(upnext),
        "up_next": [f"{t.get('artist') or '?'} - {t.get('title') or '?'}" for t in upnext],
        "request": str(dj.request or ""),
        "station": str(getattr(dj, "station", "") or ""),
        "volume": int(getattr(ctx, "volume", 70) or 70),
        "paused": bool(getattr(dj, "paused", False)),
        "liked_artists": liked_artists,
        "moods": moods,
    }


def build_system_prompt(snap: dict) -> str:
    """The DJ-host persona, seeded with the actual set so it talks about *your* mix."""
    now = snap.get("now") or "nothing playing"
    vibe = (snap.get("vibe") or "").strip()
    why = (snap.get("why") or "").strip()
    queued = int(snap.get("queued") or 0)
    up = ", ".join(snap.get("up_next") or []) or "nothing queued"
    request = snap.get("request") or "none"
    station = snap.get("station") or "none"
    liked = ", ".join(snap.get("liked_artists") or []) or "none yet"
    moods = ", ".join(snap.get("moods") or []) or "none yet"
    mood_line = f"The mix is '{vibe}'." if vibe else "This set has no name yet."
    return (
        "You are the DJ inside Spotube DJ, a personal YouTube Music player. "
        "You are a warm, concise host: read the room, name the vibe, and keep it "
        "moving. You answer in a couple of sentences, not paragraphs.\n\n"
        "RIGHT NOW\n"
        f"- Now playing: {now}\n"
        f"- {mood_line}\n"
        f"- Why this is playing: {why or 'the reason is not tracked yet'}\n"
        f"- In the queue: {queued} track(s) - {up}\n"
        f"- The request that built this: {request}\n"
        f"- Station: {station}\n"
        f"- Loved artists: {liked}\n"
        f"- Favoured moods: {moods}\n\n"
        "YOU CAN RUN THE PLAYER\n"
        "Use the tools when the listener wants something: play (a song/artist/mood), "
        "mix (from their likes), skip, like, dislike, pause, resume, volume, get_status.\n\n"
        "RULES\n"
        "- To act, CALL a tool; only claim something after you have called it.\n"
        "- When asked WHY a song is playing, explain from the facts: what they asked for, "
        "the artist/mood it matches (from Loved artists / Favoured moods), whether it is one "
        "of their picks, and the set's name. Never invent a reason - if the reason is not "
        "tracked, say so rather than guess.\n"
        "- Short and alive. Celebrate good asks. Use the mix name when you mention the set.\n"
        "- Do not invent facts about tracks you cannot see; only what the snapshot says.\n"
        "- If you cannot tell, check get_status before promising anything.\n"
    )


# ------------------------------------------------------------ live transport
def _ws_connect(url: str, timeout: float):
    """Open a websocket-client connection, giving a clear error when it is absent."""
    try:
        import websocket  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on the environment
        raise LiveUnavailable(
            "the DJ chat needs the 'websocket-client' package - "
            "pip install websocket-client") from exc
    try:
        return websocket.create_connection(url, timeout=float(timeout or 45.0))
    except Exception as exc:  # no network, a bad key, HTTP error at the handshake
        raise LiveUnavailable(
            f"could not reach the Gemini Live API ({exc.__class__.__name__})") from exc


class LiveConnection:
    """Thin wrapper so a real connection and a test double share one interface."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def send(self, message: dict) -> None:
        self._conn.send(json.dumps(message))

    def recv(self) -> dict:
        raw = self._conn.recv()
        if isinstance(raw, dict):
            return raw                       # a test double may already decode
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"serverContent": {"modelTurn": {"parts": [{"text": raw}]}}}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def run_turn(conn, *, setup: dict, history: list[dict], user_text: str,
             executor, on_text=None) -> str:
    """
    One Live round-trip: configure, send the turn, read text and run tool calls
    until the model says `turnComplete`. -> the assembled reply text.

    `conn` is a LiveConnection-like object (duck-typed so tests inject a fake).
    `on_text(text)` is called per text chunk so a caller can stream the reply.
    """
    conn.send(setup)
    # wait for setupComplete before sending any turn
    while True:
        msg = conn.recv()
        if "setupComplete" in msg:
            break
        if "goAway" in msg or _fatal(msg):
            return _failure_reply(msg)

    turns = ([{"role": str(m.get("role") or "user"), "parts": [{"text": str(m.get("text") or "")}]}
              for m in history[:40]]
             + [{"role": "user", "parts": [{"text": str(user_text)}]}])
    conn.send({"clientContent": {"turns": turns, "turnComplete": True}})

    out: list[str] = []

    def emit(t: str) -> None:
        if t:
            out.append(t)
            if on_text:
                on_text(t)

    while True:
        msg = conn.recv()
        sc = msg.get("serverContent")
        if sc:
            for part in (sc.get("modelTurn") or {}).get("parts") or []:
                if isinstance(part, dict) and part.get("text"):
                    emit(str(part["text"]))
            if sc.get("turnComplete"):
                break
        tc = msg.get("toolCall")
        if tc:
            responses = []
            for fc in tc.get("functionCalls") or []:
                fn, fid = str(fc.get("name") or ""), fc.get("id")
                try:
                    result = executor(fn, fc.get("args") or {})
                except LiveUnavailable:
                    result = {"error": "the DJ is not configured"}
                except Exception as exc:  # a tool bug must not kill the whole turn
                    result = {"error": f"{exc.__class__.__name__}: {exc}"}
                if not isinstance(result, dict):
                    result = {"result": result}
                responses.append({"id": fid, "name": fn, "response": result})
            conn.send({"toolResponse": {"functionResponses": responses}})
            continue
        if msg.get("goAway") or _fatal(msg):
            break

    return " ".join("".join(out).split())


def _fatal(msg: dict) -> bool:
    return bool(msg.get("error"))


def _failure_reply(msg: dict) -> str:
    err = msg.get("error")
    return f"the DJ connection failed ({err})" if err else "the DJ session ended"


# ----------------------------------------------------------------- agent
class DJAgent:
    """Holds the conversation and knows how to talk to the Live API."""

    def __init__(self, ctx, executor=None, connect=_ws_connect) -> None:
        self.ctx = ctx
        self.executor = executor or (lambda n, a: execute_tool(ctx, n, a))
        self.connect = connect
        self.history: list[dict] = []
        self._lock = threading.Lock()

    def system_prompt(self) -> str:
        return build_system_prompt(dj_snapshot(self.ctx))

    def available(self) -> bool:
        """True when a key is present; the websockets package is checked lazily too."""
        return bool(api_key())

    def chat(self, user_text: str, on_text=None) -> str:
        """One conversation turn. Returns the assistant reply (may be empty on failure)."""
        text = (user_text or "").strip()
        if not text:
            return ""
        if not self.available():
            raise LiveUnavailable("set a Gemini key in Settings, then the DJ can talk")
        url = live_ws_url(api_key())
        conn = LiveConnection(self.connect(url, timeout=float(config.LLM_TIMEOUT or 45.0)))
        try:
            reply = run_turn(conn, setup=build_setup(live_model(), self.system_prompt(),
                                                     tool_declarations()),
                             history=self.history, user_text=text,
                             executor=self.executor, on_text=on_text)
        finally:
            conn.close()
        with self._lock:
            self.history.append({"role": "user", "text": text})
            if reply:
                self.history.append({"role": "model", "text": reply})
            if len(self.history) > 40:
                self.history = self.history[-40:]
        return reply
