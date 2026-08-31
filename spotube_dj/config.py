"""
Shared config/state for Spotube DJ.

Everything lives under ~/.spotube-dj/ so it never collides with the
schultz-dev0/SpotifyDJ app (~/.spotify-ai-dj/) and you can keep both.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("SPOTUBE_DJ_HOME", str(Path.home() / ".spotube-dj")))
STATE_FILE = APP_DIR / "state.json"
HISTORY_FILE = APP_DIR / "history.jsonl"
M3U_OUT = APP_DIR / "spotube_dj.m3u8"
LLM_CONFIG_FILE = APP_DIR / "config.json"

# ---------------------------------------------------------------- LLM config
# Mirrors SpotifyDJ's setup so you can reuse the same Gemini key / Ollama.
#
# IMPORTANT: these are *overridable* defaults. The real values live in
# ~/.spotube-dj/config.json (see load_llm_config) because anything kept only in
# module globals is gone the moment the app restarts - which is exactly how a
# "0% success" brain happens: you paste a key, it works once, next launch it is
# empty and every request silently falls back to the offline parser.
#
# LLM_BASE_URL defaults to "" (not the Gemini URL) on purpose: brain.py applies
# that default only when it knows a key exists. An empty base URL plus a saved
# localhost URL must not silently turn back into "gemini with no key".
GEMINI_DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta"
# gemini-2.0-flash was SHUT DOWN on 2026-06-01 (a 404 "no longer available"),
# which is what a saved key looked like: "0% success". 3.5 Flash is the current
# baseline for routine high-throughput calls; brain.py walks a short ladder and
# honours whatever model the API itself suggests, so this list ageing over is
# survivable rather than fatal.
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"
# the spoken DJ: a Google audio model + a voice. Despina is warm and smooth.
# The default is the Gemini **Live** native-audio model (gemini-3.1-flash-live-preview),
# which speaks over the Live API WebSocket. The Live API has no free-tier request
# cap for this account (it works where the generateContent REST TTS endpoint rate-
# limits), and a `systemInstruction` in the Live setup makes it READ the prewritten
# DJ line as an on-air announcement rather than answering it like a chatbot. The
# older generateContent TTS endpoint (gemini-*-tts-preview) reads verbatim but can
# trip a REST quota, so it is only used when explicitly selected.
# Override per shell (SPOTUBE_DJ_TTS_MODEL) or in Settings.
GEMINI_DEFAULT_TTS_MODEL = os.environ.get("SPOTUBE_DJ_TTS_MODEL",
                                          "gemini-3.1-flash-live-preview")
DJ_VOICE = os.environ.get("SPOTUBE_DJ_TTS_VOICE", "Despina")

LLM_BASE_URL = os.environ.get("SPOTUBE_DJ_BASE_URL", "")
LLM_API_KEY = os.environ.get("GEMINI_API_KEY", os.environ.get("SPOTUBE_DJ_API_KEY", ""))
LLM_MODEL = os.environ.get("SPOTUBE_DJ_MODEL", "")
def _env_timeout() -> float:
    # a typo in the env var must not make the whole package unimportable
    raw = os.environ.get("SPOTUBE_DJ_LLM_TIMEOUT", "").strip()
    try:
        return max(5.0, float(raw)) if raw else 45.0
    except ValueError:
        return 45.0

LLM_TIMEOUT = _env_timeout()

MAX_RECENT_HISTORY = 800


LLM_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT", "DJ_VOICE")

# The Gemini speech-generation voices (name, gender, character, written language).
# The "language" is the written language the DJ composes in; choosing an Arabic
# voice makes the DJ write Arabic, the rest are English accents (US/GB/IN).
GEMINI_TTS_VOICES = (
    ("Zephyr", "Female", "bright", "English"),
    ("Puck", "Male", "upbeat", "English"),
    ("Charon", "Male", "informative", "English"),
    ("Kore", "Female", "firm", "English"),
    ("Fenrir", "Male", "excitable", "English"),
    ("Leda", "Female", "youthful", "English"),
    ("Orus", "Male", "firm", "English"),
    ("Aoede", "Female", "breezy", "English"),
    ("Callirrhoe", "Female", "easy-going", "English"),
    ("Autonoe", "Female", "bright", "English"),
    ("Enceladus", "Male", "breathy", "English"),
    ("Iapetus", "Male", "clear", "English"),
    ("Umbriel", "Male", "easy-going", "English"),
    ("Algieba", "Male", "smooth", "English"),
    ("Despina", "Female", "smooth", "English"),
    ("Erinome", "Female", "clear", "English"),
    ("Algenib", "Male", "gravelly", "English"),
    ("Rasalgethi", "Male", "informative", "English"),
    ("Laomedeia", "Female", "upbeat", "English"),
    ("Achernar", "Female", "soft", "Arabic"),
    ("Alnilam", "Male", "firm", "English"),
    ("Schedar", "Male", "even", "English"),
    ("Gacrux", "Female", "mature", "Arabic"),
    ("Pulcherrima", "Female", "forward", "English"),
    ("Achird", "Male", "friendly", "Arabic"),
    ("Zubenelgenubi", "Male", "casual", "English"),
    ("Vindemiatrix", "Female", "gentle", "English"),
    ("Sadachbia", "Male", "lively", "English"),
    ("Sadaltager", "Male", "knowledgeable", "English"),
    ("Sulafat", "Female", "warm", "English"),
)
GEMINI_TTS_VOICE_NAMES = [v[0] for v in GEMINI_TTS_VOICES]
# how many seconds before a song ends the DJ starts the next-up announcement
DJ_LEAD_SECS = max(2.0, min(60.0,
                            float(os.environ.get("SPOTUBE_DJ_LEAD_SECS", "10"))))


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def apply_llm_overrides() -> None:
    """Push saved config.json values into the module globals every consumer reads."""
    data = load_llm_config()
    for key in LLM_KEYS:
        if key in data:
            globals()[key] = data[key]


# The state/LLM JSON files are read on the hot path: /api/state is polled about
# once a second (and pushed on every change), and each poll calls load_state() and
# load_llm_config(). Re-reading and re-parsing a small JSON file fifty times a minute
# is the kind of waste that shows up as a busy loop on a laptop fan. Both readers
# cache by (mtime, size) - a file that has not changed is handed back as a copy,
# and save_*() updates the mtime so the next call re-reads. A copy (not the cached
# object) is returned because taste mutates the dict it gets and then saves it;
# handing out the cached object would poison the cache.
_cache_lock = threading.Lock()
_state_cache: tuple | None = None        # (mtime_ns, size, data)
_llm_cache: tuple | None = None          # (mtime_ns, size, data)


def _file_identity(path: Path) -> tuple | None:
    """(path, mtime_ns, size) for a readable file, or None if it is missing/unreadable.

    The path is part of the key on purpose: tests (and a restart that re-points
    APP_DIR) swap STATE_FILE/config.json between runs, and two different files can
    share an mtime and a size. Keying on path alone isn't enough either (an edit in
    place keeps the path) - the mtime/size change is what catches a rewrite.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def _cached(path: Path, cache: tuple | None) -> tuple | None:
    """Return (identity, parsed-dict) when the file is unchanged, else None."""
    if cache is None:
        return None
    ident, data = cache
    if _file_identity(path) == ident:
        return ident, data
    return None


def _load_state_dict() -> dict:
    """Read + normalise the state file; the part of load_state that hits the disk."""
    defaults = {
        "liked": [],
        "skipped": [],
        "artists": {},
        "genres": {},
        "volume": 70,
        "autoplay": False,
        "repeat": "off",
        "shuffle": False,
        "last_request": "",
        "player": "mpv",
    }
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        return defaults
    if not isinstance(data, dict):
        return defaults
    for k, v in defaults.items():
        if k not in data or data[k] is None:
            data[k] = v
    for key in ("liked", "skipped"):
        rows = data[key]
        data[key] = [x for x in rows if isinstance(x, dict)] if isinstance(
            rows, (list, tuple)) else []
    data["artists"] = _weights(data["artists"])
    data["genres"] = _weights(data["genres"])
    try:
        data["volume"] = max(0, min(100, int(float(data["volume"]))))
    except (TypeError, ValueError):
        data["volume"] = 70
    data["autoplay"] = bool(data["autoplay"])
    if str(data["repeat"]) not in ("off", "all", "one"):
        data["repeat"] = "off"
    data["shuffle"] = bool(data["shuffle"])
    data["last_request"] = str(data["last_request"] or "")
    if str(data["player"]) not in ("mpv", "spotube"):
        data["player"] = "mpv"
    return data


def load_llm_config() -> dict:
    """
    Read ~/.spotube-dj/config.json. Env vars win over the file so a shell
    export or .env still overrides the GUI, which is what people expect.
    """
    global _llm_cache
    with _cache_lock:
        hit = _cached(LLM_CONFIG_FILE, _llm_cache)
        if hit is not None:
            raw = hit[1]
        else:
            try:
                raw = json.loads(LLM_CONFIG_FILE.read_text())
            except Exception:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            _llm_cache = (_file_identity(LLM_CONFIG_FILE), raw)
    out = {}
    for k in LLM_KEYS:
        if k in raw and raw[k] is not None:
            out[k] = copy.deepcopy(raw[k])
    if "LLM_TIMEOUT" in out:
        try:
            out["LLM_TIMEOUT"] = max(5.0, float(out["LLM_TIMEOUT"]))
        except (TypeError, ValueError):
            out.pop("LLM_TIMEOUT")
    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("SPOTUBE_DJ_API_KEY")
    if env_key:
        out["LLM_API_KEY"] = env_key
    if os.environ.get("SPOTUBE_DJ_BASE_URL"):
        out["LLM_BASE_URL"] = os.environ["SPOTUBE_DJ_BASE_URL"]
    if os.environ.get("SPOTUBE_DJ_MODEL"):
        out["LLM_MODEL"] = os.environ["SPOTUBE_DJ_MODEL"]
    if os.environ.get("SPOTUBE_DJ_TTS_VOICE"):
        out["DJ_VOICE"] = os.environ["SPOTUBE_DJ_TTS_VOICE"]
    return out


def load_dj_voice() -> str:
    """The effective Gemini voice: env override > saved config > default Despina."""
    return str(load_llm_config().get("DJ_VOICE") or DJ_VOICE or "Despina")


def voice_lang(name: str | None) -> str:
    """The written language the DJ should compose in for a given voice."""
    name = str(name or "")
    for n, _g, _t, lang in GEMINI_TTS_VOICES:
        if n.lower() == name.lower():
            return lang
    return "English"


def save_llm_config(**values) -> dict:
    """Persist the LLM settings. Called by the GUI's Save and by --set-key."""
    ensure_dirs()
    try:
        data = json.loads(LLM_CONFIG_FILE.read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    for k, v in values.items():
        if k in LLM_KEYS and v is not None:
            data[k] = v
    tmp = LLM_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(LLM_CONFIG_FILE)
    try:
        LLM_CONFIG_FILE.chmod(0o600)      # a key on disk should not be world-readable
    except Exception:
        pass
    with _cache_lock:
        global _llm_cache
        _llm_cache = None                 # next load re-reads the fresh file
    apply_llm_overrides()
    return data


def load_state() -> dict:
    """Mutable taste profile + settings, persisted as JSON."""
    global _state_cache
    with _cache_lock:
        hit = _cached(STATE_FILE, _state_cache)
        if hit is None:
            data = _load_state_dict()
            _state_cache = (_file_identity(STATE_FILE), data)
        else:
            data = hit[1]
    # a copy, never the cached object: taste mutates the dict it gets and saves it
    return copy.deepcopy(data)


def _weights(value) -> dict:
    """
    `artists` and `genres` are name -> number. Keep what is salvageable and drop
    the rest: a mapping with a null in it, or a list of [name, weight] pairs from
    some other tool, must not be able to stop the app from starting. A bare list of
    names is dropped rather than invented into weights, because "the DJ likes this
    artist a lot" is not information a list of strings carries.
    """
    out: dict = {}
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, (list, tuple)):
        items = [x for x in value if isinstance(x, (list, tuple)) and len(x) == 2]
    else:
        return out
    for name, weight in items:
        try:
            out[str(name)] = float(weight)
        except (TypeError, ValueError):
            continue
    return out


def save_state(state: dict) -> None:
    global _state_cache
    ensure_dirs()
    state["liked"] = list(state.get("liked") or [])[-200:]
    state["skipped"] = list(state.get("skipped") or [])[-200:]
    # decay + prune weights so the profile can't grow forever
    weights = _weights(state.get("artists"))
    state["artists"] = {k: v for k, v in weights.items() if abs(v) >= 0.05}
    tags = _weights(state.get("genres"))
    state["genres"] = {k: v for k, v in tags.items() if abs(v) >= 0.05}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)
    with _cache_lock:
        _state_cache = None          # next load re-reads the fresh file


def append_history(entry: dict) -> None:
    ensure_dirs()
    with HISTORY_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history(limit: int = 200) -> list[dict]:
    """
    The rows `append_history` writes, oldest first. Only the tail is read: the
    file grows forever and a GUI list of 40 rows has no business parsing 20k
    lines to draw itself. A line that will not parse is skipped rather than
    raised - a half-written last line after a crash must not empty the library.
    """
    if not HISTORY_FILE.exists():
        return []
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    if limit:
        lines = lines[-int(limit):]
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def recent_uris(limit: int = MAX_RECENT_HISTORY) -> set[str]:
    """Track ids already played recently - used to stop repeats."""
    if not HISTORY_FILE.exists():
        return set()
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return set()
    out = set()
    for ln in lines:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("id"):
            out.add(e["id"])
    return out


def touch_last_request(text: str) -> None:
    state = load_state()
    state["last_request"] = text
    save_state(state)


def now() -> float:
    return time.time()
