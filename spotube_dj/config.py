"""
Shared config/state for Spotube DJ.

Everything lives under ~/.spotube-dj/ so it never collides with the
schultz-dev0/SpotifyDJ app (~/.spotify-ai-dj/) and you can keep both.
"""

from __future__ import annotations

import json
import os
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


LLM_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT")


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def apply_llm_overrides() -> None:
    """Push saved config.json values into the module globals every consumer reads."""
    data = load_llm_config()
    for key in LLM_KEYS:
        if key in data:
            globals()[key] = data[key]


def load_llm_config() -> dict:
    """
    Read ~/.spotube-dj/config.json. Env vars win over the file so a shell
    export or .env still overrides the GUI, which is what people expect.
    """
    out: dict = {}
    try:
        raw = json.loads(LLM_CONFIG_FILE.read_text())
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        for k in LLM_KEYS:
            if k in raw and raw[k] is not None:
                out[k] = raw[k]
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
    return out


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
    apply_llm_overrides()
    return data


def load_state() -> dict:
    """Mutable taste profile + settings, persisted as JSON."""
    defaults = {
        "liked": [],            # [{title, artist, id, ts}]
        "skipped": [],          # [{title, artist, ts, reason}]
        "artists": {},          # artist -> weight
        "genres": {},           # genre -> weight
        "volume": 70,
        # autoplay: start a mix and play on open. Off by default - the web skin
        # waits for the listener to press Play or search ("even i dont start the
        # button yet"), and one tap turns it back on.
        "autoplay": False,
        "repeat": "off",        # off | all | one
        "shuffle": False,
        "last_request": "",
        "player": "mpv",        # mpv | spotube
    }
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        return defaults
    if not isinstance(data, dict):
        # a half-written file, or a list someone pasted in by hand. The state file
        # is the one thing in this app a listener is likely to open and poke, and a
        # traceback at startup is not a useful response to a typo in their own file.
        return defaults
    for k, v in defaults.items():
        if k not in data or data[k] is None:
            data[k] = v
    # normalise shapes written by older/edited files
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
