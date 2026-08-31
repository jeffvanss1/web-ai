"""
The app's only route to Gemini: the Cloudflare Worker in `worker/`.

```
brain.py / agent.py  ──▶  POST /v1/plan · /v1/text
djvoice.py           ──▶  POST /v1/speech        (audio/wav back)
web.py (DJ state)    ──▶  GET·PUT /v1/state · POST·GET /v1/events   (D1)
```

Three reasons this exists rather than calling Google directly, and they are the
same reason twice over:

* **the key is not on this machine.** It is a `wrangler secret`. A laptop that
  runs the player holds a URL and a token, neither of which is worth anything
  to anyone who finds them.
* **a retired model is not an outage.** Google retires `gemini-*` names on a
  schedule. The ladder is walked in the Worker, so a retirement costs one extra
  request once, centrally, instead of every install editing a field.
* **the failure is one kind, not nine.** The Worker answers JSON with the same
  `kind`s this app has always used (`key`/`access`/`model`/`quota`/`payload`),
  so the wording in the log drawer and the pill did not have to change.

There is deliberately **no fallback to calling Google from here.** A fallback
only runs when something is already broken, which is when it is least tested;
two implementations of "ask Gemini" means two places for a bug to hide. No
`WORKER_URL` configured means offline, and the app says so.

`_urlopen` is a module-level hook so the tests can answer requests without a
network or a deployed Worker.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import config

# Which Gemini route the Worker asked for last, for the header pill and --doctor.
LAST_OK: float | None = None
LAST_ERROR: dict | None = None
_HEALTH_TTL = 600.0          # don't re-ask /v1/health on every track
_health: dict = {"at": 0.0, "data": None}
# True while /v1/health is in flight. The key question ("does this Worker want
# our key?") is answered BY that call, so the call must not stop to ask it -
# the obvious version of this recursed until Python gave up.
_probing = False


class WorkerError(Exception):
    """A Worker call that did not produce an answer.

    `kind` is one of the kinds brain.py already words for the user:
    key · access · model · quota · payload · timeout · network · empty ·
    parse · auth · no_d1 · crash · offline (nothing configured at all).
    """

    def __init__(self, kind: str, detail: str = "", *, status: str = "",
                 http: int = 0, notes: list[str] | None = None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = str(kind or "other")
        self.detail = str(detail or "")
        self.status = str(status or "")
        self.http = int(http or 0)
        self.notes = list(notes or [])

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "status": self.status,
                "http": self.http, "notes": self.notes}


# ------------------------------------------------------------------ the config

def settings() -> dict:
    """url / token / profile / sync, from config.json + env (see config.load_worker_config)."""
    return config.load_worker_config()


def base_url() -> str:
    return str(settings().get("url") or "").strip().rstrip("/")


def configured() -> bool:
    """Is a Worker URL set at all? Everything else is downstream of this."""
    return bool(base_url())


def enabled() -> bool:
    """Same question, named for the callers that read like a feature switch."""
    return configured()


def profile() -> str:
    return str(settings().get("profile") or "default")


def sync_on() -> bool:
    return str(settings().get("sync") or "on").lower() == "on"


def _timeout() -> float:
    try:
        return max(5.0, float(getattr(config, "LLM_TIMEOUT", 45.0)))
    except (TypeError, ValueError):
        return 45.0


# -------------------------------------------------------------------- transport

def _urlopen(req, timeout: float):
    """The one place a socket is opened. Tests replace this."""
    return urllib.request.urlopen(req, timeout=timeout)


# Cloudflare's Browser Integrity Check answers a request with no User-Agent -
# or with urllib's default "Python-urllib/3.11" - with HTTP 403 and
# "Error 1010: The site owner has blocked access based on your browser's
# signature", *before* the Worker is ever reached. The page loads in a browser
# and fails from Python, which is the signature of exactly that.
#
# So this client announces itself the way every HTTP client that speaks for a
# person does: the Mozilla/5.0 (compatible; ...) form exists for this, and it
# still says what it is. It is not a disguise - the app, its version and its
# source are all in the string.
UA = "Mozilla/5.0 (compatible; spotube-dj/1.0; +https://github.com/jeffvanss1/web-ai)"
BROWSERISH = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _headers(extra: dict | None = None) -> dict:
    cfg = settings()
    out = dict(BROWSERISH)
    out["Content-Type"] = "application/json"
    token = str(cfg.get("token") or "")
    if token:
        out["Authorization"] = f"Bearer {token}"
    # A key the Worker's operator never set is the only reason to send one: the
    # /v1/health reply says which side of that this deployment is on.
    if not _probing and wants_client_key():
        key = str(getattr(config, "LLM_API_KEY", "") or "")
        if key:
            out["X-Gemini-Key"] = key
    if extra:
        out.update(extra)
    return out


def _url(path: str) -> str:
    base = base_url()
    if not base:
        raise WorkerError("offline", "no Worker URL configured (Settings -> Worker)")
    return f"{base}{path}"


def _fail(err: WorkerError) -> WorkerError:
    global LAST_ERROR
    LAST_ERROR = {"at": time.time(), **err.as_dict()}
    return err


def _ok() -> None:
    global LAST_OK
    LAST_OK = time.time()
    LAST_ERROR = None


def _read_error(err: urllib.error.HTTPError) -> WorkerError:
    body = ""
    try:
        body = (err.read() or b"").decode("utf-8", "replace")
    except Exception:
        body = ""
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        inner = payload.get("error")
        if isinstance(inner, dict):
            return WorkerError(str(inner.get("kind") or "other"),
                               str(inner.get("detail") or body[:200]),
                               status=str(inner.get("status") or ""),
                               http=err.code,
                               notes=list(inner.get("notes") or []))
        cf = _cloudflare_words(payload)
        if cf:
            return WorkerError("edge", cf, http=err.code)
        return WorkerError("other", str(payload)[:300], http=err.code)
    return WorkerError("other", (body or f"HTTP {err.code}")[:300], http=err.code)


def _cloudflare_words(payload: dict) -> str:
    """
    Cloudflare's own error pages (403 "Error 1010", 1020, 1015...) are JSON
    with a `title` and a support URL, and they are answered by the *edge*, not
    by the Worker - so nothing in the Worker's config can explain them and
    nothing in the Worker's logs will show them. Say which one it is and what
    to do, rather than printing the JSON.
    """
    title = str(payload.get("title") or "")
    detail = str(payload.get("detail") or "")
    if "cloudflare" not in str(payload.get("type") or "").lower() and not title.startswith("Error "):
        return ""
    code = ""
    if title.startswith("Error "):
        code = title[len("Error "):].split(":")[0].strip()
    host = base_url() or "the Worker"
    if code == "1010":
        return (f"Cloudflare blocked this client before it reached the Worker "
                f"(Error 1010, browser integrity check). Open {host} in a "
                f"browser: if the page loads there, it is the request's "
                f"signature, not your config. Turn off Bot Fight Mode / Browser "
                f"Integrity Check for the zone, or put the Worker on your own "
                f"domain with those off.")
    if code:
        return (f"Cloudflare answered {host} with {title} rather than letting "
                f"the request through: {detail}")
    return ""


def request(method: str, path: str, payload: dict | None = None, *,
            timeout: float | None = None, raw: bool = False):
    """One call to the Worker. Returns bytes when `raw`, else the parsed JSON dict.

    Raises `WorkerError` for anything that is not an answer - including "no
    Worker configured", which is `kind="offline"` and is a setting, not a
    failure of the network.
    """
    if not configured():
        raise _fail(WorkerError(
            "offline",
            "no Worker URL configured - the planner and the spoken DJ are off "
            "(Settings -> Worker, or SPOTUBE_DJ_WORKER_URL)"))
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(_url(path), data=data, headers=_headers(),
                                 method=method)
    try:
        with _urlopen(req, timeout or _timeout()) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise _fail(_read_error(e)) from None
    except urllib.error.URLError as e:
        raise _fail(WorkerError("network", _net_words(e, path))) from None
    except TimeoutError as e:
        raise _fail(WorkerError(
            "timeout", f"no answer from {base_url()} in {int(_timeout())}s")) from None
    except OSError as e:                                   # noqa: BLE001
        raise _fail(WorkerError("network", _net_words(e, path))) from None
    if raw:
        _ok()
        return body
    try:
        out = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        raise _fail(WorkerError("parse", f"the Worker at {path} did not answer JSON: "
                                        f"{body[:120]!r}")) from None
    if isinstance(out, dict) and out.get("ok") is False:
        inner = out.get("error") if isinstance(out.get("error"), dict) else {}
        raise _fail(WorkerError(str(inner.get("kind") or "other"),
                                str(inner.get("detail") or "the Worker refused"),
                                status=str(inner.get("status") or ""),
                                notes=list(inner.get("notes") or [])))
    _ok()
    return out


def _net_words(exc: Exception, where: str) -> str:
    reason = str(getattr(exc, "reason", exc) or exc.__class__.__name__)
    low = reason.lower()
    host = base_url() or where
    if "refused" in low:
        return f"nothing listening at {host} - is the Worker deployed (or `wrangler dev` running)?"
    if "timed out" in low:
        return f"timed out after {int(_timeout())}s talking to {host}"
    if "name resolution" in low or "nodename" in low or "getaddrinfo" in low:
        return f"cannot resolve the host in {host} (typo in the Worker URL, or no network?)"
    return f"{reason} ({host})"[:300]


# ------------------------------------------------------------------------ plan

def plan(prompt: str, system: str = "", model: str = "", *,
         timeout: float | None = None) -> dict:
    """-> {"plan": {...}, "model": str, "notes": [...]}. Raises `WorkerError`."""
    payload = {"prompt": str(prompt or "")}
    if system:
        payload["system"] = system
    if model:
        payload["model"] = str(model)
    payload["timeoutMs"] = int(timeout or _timeout())
    out = request("POST", "/v1/plan", payload, timeout=timeout)
    plan_json = out.get("plan")
    if not isinstance(plan_json, dict):
        raise _fail(WorkerError("parse", "the Worker sent no plan object"))
    return {"plan": plan_json,
            "model": str(out.get("model") or ""),
            "notes": [str(n) for n in (out.get("notes") or [])]}


def text(prompt: str, system: str = "", model: str = "", max_chars: int = 600,
         temperature: float = 0.9, *, timeout: float | None = None) -> str:
    """One-shot creative text. '' when the model had nothing to say."""
    payload = {"prompt": str(prompt or ""), "maxChars": int(max_chars or 600),
               "temperature": float(temperature)}
    if system:
        payload["system"] = system
    if model:
        payload["model"] = str(model)
    out = request("POST", "/v1/text", payload, timeout=timeout)
    return str(out.get("text") or "").strip()


def speech(text_in: str, voice: str = "", model: str = "",
           *, timeout: float | None = None) -> bytes:
    """WAV bytes for `text_in` in `voice`. Raises `WorkerError` when it cannot."""
    payload = {"text": str(text_in or "")}
    if voice:
        payload["voice"] = str(voice)
    if model:
        payload["model"] = str(model)
    body = request("POST", "/v1/speech", payload, raw=True,
                   timeout=timeout or max(_timeout(), 60.0))
    if not body:
        raise _fail(WorkerError("empty", "the Worker returned no audio"))
    return body


# --------------------------------------------------------------------- D1 state

def state_get(name: str | None = None, *, timeout: float | None = None) -> dict | None:
    """The saved taste snapshot, or None for a profile nobody has written yet."""
    out = request("GET", f"/v1/state?profile={_q(name)}", timeout=timeout or 15.0)
    state = out.get("state")
    return state if isinstance(state, dict) else None


def state_put(state: dict, name: str | None = None, *,
              timeout: float | None = None) -> int:
    """Upsert the taste snapshot. -> the Worker's `updated_at` (epoch ms)."""
    out = request("PUT", "/v1/state", {"profile": name or profile(), "state": state},
                  timeout=timeout or 20.0)
    return int(out.get("updated_at") or 0)


def events_post(events: list[dict], name: str | None = None, *,
                timeout: float | None = None) -> int:
    """Append taste events (like/skip/dislike/play/...). -> the last id stored."""
    if not events:
        return 0
    out = request("POST", "/v1/events",
                  {"profile": name or profile(), "events": list(events)},
                  timeout=timeout or 20.0)
    return int(out.get("last_id") or 0)


def events_get(since: int = 0, name: str | None = None, limit: int = 500, *,
               timeout: float | None = None) -> list[dict]:
    """Events newer than id `since`, oldest first."""
    path = (f"/v1/events?profile={_q(name)}&since={int(max(0, since))}"
            f"&limit={int(max(1, min(limit, 2000)))}")
    out = request("GET", path, timeout=timeout or 15.0)
    rows = out.get("events")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _q(name: str | None) -> str:
    import urllib.parse
    return urllib.parse.quote(str(name or profile()))


# ----------------------------------------------------------------------- health

def health(force: bool = False) -> dict:
    """`/v1/health`, cached for ten minutes. Never raises."""
    global _probing
    now = time.time()
    if not force and _health["data"] and now - float(_health["at"]) < _HEALTH_TTL:
        return dict(_health["data"])
    if not configured():
        _health["data"] = {"ok": False, "error": {"kind": "offline",
                                                  "detail": "no Worker URL configured"}}
        _health["at"] = now
        return dict(_health["data"])
    _probing = True
    try:
        out = request("GET", "/v1/health", timeout=15.0)
    except WorkerError as e:
        out = {"ok": False, "error": e.as_dict()}
    finally:
        _probing = False
    _health["data"] = out if isinstance(out, dict) else {}
    _health["at"] = now
    return dict(_health["data"])


def wants_client_key() -> bool:
    """True when this Worker has no GEMINI_API_KEY secret and needs ours.

    Answered from the cached /v1/health, so a deployment that holds its own key
    never sees ours on the wire. A Worker we cannot reach is assumed to hold
    one, because sending a key to a host that does not want it is worse than
    not sending it to one that does.
    """
    info = health()
    if not info.get("ok"):
        return False
    return str(info.get("key_source") or "").startswith("client")


def probe() -> dict:
    """What Settings -> Test the worker and --doctor print. Never raises."""
    out = {"configured": configured(), "url": base_url(), "ok": False,
           "detail": "", "ms": 0, "notes": []}
    if not configured():
        out["detail"] = ("no Worker URL - the planner is the offline parser and "
                         "the DJ does not speak (Settings -> Worker)")
        return out
    t0 = time.monotonic()
    info = health(force=True)
    out["ms"] = int((time.monotonic() - t0) * 1000)
    if not info.get("ok"):
        err = info.get("error") if isinstance(info.get("error"), dict) else {}
        out["detail"] = f"{err.get('kind', 'error')}: {err.get('detail', 'no answer')}"[:300]
        return out
    out["ok"] = True
    bits = [f"health ok in {out['ms']}ms", f"model {info.get('model') or '?'}"]
    if info.get("d1"):
        bits.append("D1 bound")
    else:
        bits.append("no D1 - state sync off")
    if info.get("clips"):
        bits.append("voice cache on")
    bits.append("key from " + ("the Worker's secret" if not wants_client_key()
                               else "this machine"))
    out["detail"] = ", ".join(bits)
    # a health check is not proof the planner works, so ask it something real
    t0 = time.monotonic()
    try:
        got = plan("Reply with 3 queries for: mellow evening jazz",
                   system="You answer with JSON only.", timeout=45.0)
    except WorkerError as e:
        out["ok"] = False
        out["detail"] += f" - but the plan call failed: {e.kind}: {e.detail}"[:200]
        out["notes"] = list(e.notes)
        return out
    out["ms"] = int((time.monotonic() - t0) * 1000)
    qs = (got.get("plan") or {}).get("queries") or []
    out["notes"] = list(got.get("notes") or [])
    if qs:
        out["detail"] += (f" - {len(qs)} queries in {out['ms']}ms via "
                          f"{got.get('model') or '?'}, e.g. {str(qs[0])[:40]}")
    else:
        out["ok"] = False
        out["detail"] += " - the planner answered but produced no queries"
    return out


def status_line() -> str:
    """One short line for the header pill: 'worker: ok' / 'worker: no URL'."""
    if not configured():
        return "worker: no URL"
    err = LAST_ERROR
    if err and not LAST_OK:
        return f"worker: {err.get('kind')}"
    if err and LAST_OK and float(err.get("at") or 0) > LAST_OK:
        return f"worker: {err.get('kind')}"
    info = health()
    if not info.get("ok"):
        err = info.get("error") if isinstance(info.get("error"), dict) else {}
        return f"worker: {err.get('kind', 'unreachable')}"
    return "worker: ok"
