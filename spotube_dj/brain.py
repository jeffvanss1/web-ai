"""
brain.py - turns a natural-language request into YouTube-Music search queries.

Three engines, in priority order:
  1. Gemini (same key SpotifyDJ uses)
  2. Any OpenAI-compatible endpoint -> Ollama / LM Studio / Open WebUI
  3. Offline heuristic parser (always works, no key, no network)

Only #3 is required, so the app is usable out of the box.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

import config

try:                                  # pick up Settings -> Save values
    config.apply_llm_overrides()
except Exception:
    pass

# Set by the most recent LLM attempt so the UI can say *why* it fell back
# instead of quietly showing "engine: offline". {engine: reason}
LAST_ERRORS: dict[str, str] = {}

RETRIES = 1          # one retry: a cold local model or a blip is worth a second try
DEFAULT_TIMEOUT = 45.0   # Gemini flash cold-start + a thinking model clears 25s often
MIN_TIMEOUT = 5.0


def _fail(engine: str, exc: Exception | str) -> None:
    """Record a human-quotable reason. This is the piece that was missing."""
    if isinstance(exc, str):
        msg = exc
    elif isinstance(exc, urllib.error.HTTPError):
        msg = _http_error_text(exc)
    elif isinstance(exc, (TimeoutError, __import__("socket").timeout)):
        msg = f"timed out after {int(_timeout())}s"
    elif isinstance(exc, urllib.error.URLError):
        msg = _net_error_text(exc, "")
    else:
        msg = f"{exc.__class__.__name__}: {exc}"
    LAST_ERRORS[engine] = msg[:300]


def why_offline() -> str:
    """Best one-line explanation of why the LLM was not used."""
    if not LAST_ERRORS:
        return ""
    for engine, msg in LAST_ERRORS.items():
        if msg:
            return f"{engine}: {msg}"
    return ""


SYSTEM = (
    "You are a music DJ assistant. Given a listener's request, produce a list of "
    "search queries to run against YouTube Music. Rules:\n"
    "- Each query must be 'artist song' or 'artist'-style, lowercase, 2-6 words.\n"
    "- Prefer real, findable recordings. Never invent song titles for obscure artists.\n"
    "- Mix 2 well-known anchors with 3 deeper cuts so the DJ feels curated, not generic.\n"
    "- Respect the taste profile: lean toward loved artists/moods, avoid disliked ones.\n"
    "- If the listener says they DON'T want something, put it in 'avoid', not in a query.\n"
    'Reply with JSON only: {"queries": ["...", "..."], "avoid": ["..."], '
    '"why": "one short sentence"}'
)

# ---------------------------------------------------------------- heuristic
# The offline fallback has to be decent, because that's what runs with no key.
# Strategy: clean the sentence, split it into facets on commas/"but", drop
# negative wishes (they become an avoid-list instead), and keep whole phrases -
# YouTube Music search is semantic enough that "slow dark cello pieces" works.
_FILLER = re.compile(
    r"\b(?:play|put\s+on|queue(?:\s+up)?|drop|give\s+me|listen\s+to|start|hit\s+play|some|"
    r"please|pls|me|kinda|sorta|type\s+of|kind\s+of|stuff|things|want(?:s)?\s+to\s+hear|"
    r"i\s+want|i'd\s+like|i\s+need|something|anything|anything\s+else)\b", re.I)
_NOISE = re.compile(
    r"\b(?:music|songs?|tracks?|tunes?|that|which|like|about|around|between)\b", re.I)
_CONNECTOR = re.compile(r"\b(?:for|to|of|with|and|the|a|an|my|our|now|right|away)\b", re.I)
_NEGATION = re.compile(
    r"(?:\b(?:no|not|not\s+any|without|none\s+of|avoid|skip|don't\s+(?:play|like)\s+|"
    r"hate\s+)\s+)(?:any\s+)?([a-z0-9][a-z0-9 '&,\-]{1,40}?)"
    r"(?=\s*(?:please|pls|pls\.|thanks|kthx|,|\.$|!$|$))", re.I)


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().strip(",;:").strip()
    s = re.sub(r"[?!.]+$", "", s).strip()
    return s


def _facets(core: str) -> tuple[list[str], list[str]]:
    """-> (positive query facets, negative terms to keep away from)."""
    neg: list[str] = []
    for m in _NEGATION.finditer(core):
        for part in re.split(r"\s*(?:,|\band\b|\bor\b)\s*", m.group(1)):
            part = _CONNECTOR.sub(" ", _clean(part)).strip(" ")
            part = re.sub(r"\s{2,}", " ", part)
            if len(part) > 2:
                neg.append(part.lower())
    pos_src = _NEGATION.sub(" ", core)

    pieces = [p for p in re.split(r"\s*(?:,|;|\bbut\b|\band\s+also\b|\bplus\b)\s*", pos_src) if p.strip()]
    if len(pieces) == 1:
        pieces = [p for p in re.split(r"\s+and\s+|\s*&\s*", pos_src) if p.strip()]
    out: list[str] = []
    for p in pieces:
        p = _FILLER.sub(" ", p)
        p = _NOISE.sub(" ", p)
        p = _clean(re.sub(r"\s{2,}", " ", p))
        # strip leading/trailing leftover connectives
        p = re.sub(r"^(?:for|to|with|the|a|an|and|of)\s+", "", p, flags=re.I)
        p = re.sub(r"\s+(?:for|to|with|the|and|of)$", "", p, flags=re.I)
        p = _clean(re.sub(r"\s{2,}", " ", p))
        if len(p) > 2:
            out.append(p.lower())
    return out, neg


# What YouTube Music actually gets to see. "to relax" and "for studying" are
# real taste signals but terrible search strings: they return eight-hour sleep
# mixes and rain loops rather than songs. So the activity phrasing is trimmed
# off the query (it still lives in `avoid`/taste), and a query that is nothing
# *but* an activity is dropped.
_ACTIVITY_TAIL = re.compile(
    r"\s*(?:to|for|that\s+helps?\s+(?:me|you)|help(?:s)?\s+(?:me\s+|you\s+)?)?\s*"
    r"(?:relax(?:ing)?|relaxation|chill\s+out|study|studying|sleep(?:ing)?|"
    r"fall\s+asleep|sleep\s+tight|focus|concentrat(?:e|ion)|meditat(?:e|ion)|"
    r"meditate|yoga|spa|work\s*out|workout|gym|run|running|jog(?:ging)?|"
    r"cycle|cycling|clean(?:ing)?|cook(?:ing)?|dinner|lunch|commute|read(?:ing)?|"
    r"write|writing|code|programming|drive|driving|travel(?:ling)?|relax)"
    r"\s*(?:at\s+night|in\s+the\s+(?:morning|evening|shower)|now)?$")

_ACTIVITY_ONLY = re.compile(
    r"^(?:\s*(?:to|for|a|an|some|the|my|any|me|you|your|music|songs?|tracks?|"
    r"tunes|playlist|mix|stuff|things|vibes?|beats|background|listening)\b)+[\s,]*$",
    re.I)


def search_query(text: str) -> str:
    """-> the part of a phrase worth searching for, or "" if there is none."""
    q = _clean(str(text or "")).lower()
    prev = None
    while prev != q:
        prev = q
        q = _ACTIVITY_TAIL.sub("", q).strip()
        q = re.sub(r"\s+(?:for|to|with|a|an|the|my|some)$", "", q).strip()
        q = re.sub(r"\s{2,}", " ", q).strip(" ,;:")
    if not q or _ACTIVITY_ONLY.match(q) or len(q) < 3:
        return ""
    # "relaxing music for sleep" is the same query twice over: once the activity
    # at the end is gone, the adjective at the front is describing that activity
    bare = re.sub(r"^(?:relaxing|relaxed|calming|soothing|gentle|soft|quiet|deep|"
                  r"heavy|ultimate|best|top|free|ambient|background|sleepy)\s+", "", q)
    if _ACTIVITY_ONLY.match(bare):
        return ""
    return q[:80].strip()


def _heuristic(request: str) -> dict:
    raw = _clean(request.strip().strip('"'))
    core = _FILLER.sub(" ", raw)
    core = _clean(re.sub(r"\s{2,}", " ", core))
    facets, neg = _facets(core)

    # broad phrase (whole cleaned request minus fillers) works well on YT Music
    broad = _clean(_NOISE.sub(" ", core))
    broad = re.sub(r"\s{2,}", " ", broad).lower()

    queries: list[str] = []
    if facets:
        queries.extend(facets)
    if broad and len(broad) > 2:
        queries.append(broad)
    # split compound facets into 2-word moods so search never comes back empty
    extra: list[str] = []
    for f in facets:
        ws = f.split()
        if len(ws) >= 4:
            extra.append(" ".join(ws[:2]))
            extra.append(" ".join(ws[-2:]))
    queries.extend(extra)

    seen, out = set(), []
    for q in queries:
        k = re.sub(r"\s{2,}", " ", q).strip()
        if len(k) > 2 and k not in seen:
            seen.add(k)
            out.append(k)
    why = f"offline parse: {len(out)} facets"
    if neg:
        why += f", avoiding {', '.join(neg[:3])}"
    return {"queries": out[:6], "why": why, "avoid": neg}


# ------------------------------------------------------------------- llm io
def _timeout() -> float:
    """
    Gemini free-tier 'flash' with a cold start plus a thinking model can sit
    well past 25s. The old hard-coded 25s is why a valid key looked like "0%
    success": every call was abandoned mid-flight and silently fell back.
    """
    try:
        val = float(getattr(config, "LLM_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, val)


def _read_body(err: urllib.error.HTTPError) -> str:
    try:
        return (err.read() or b"").decode("utf-8", "replace")
    except Exception:
        return ""


_KEY_PHRASES = ("api key not valid", "invalid api key", "api key expired",
                "api key not provided", "invalid authentication",
                "request had no authentication", "make sure that you have")

# a rejected key and a malformed key need different advice
_KEY_HINTS_BY_STATUS = {
    "UNAUTHENTICATED": "key rejected - regenerate it in Google AI Studio",
    "API_KEY_EXPIRED": "key expired - regenerate it in Google AI Studio",
    "API_KEY_INVALID": "API key not valid (check the whole key was pasted, no spaces)",
}

_KIND_HINTS = {
    "key": "API key not valid (check the whole key was pasted, no spaces)",
    "access": "key has no access to this model/API (region or project restriction)",
    "model": "the API has no such model - check or update the Model field in Settings",
    "quota": "quota exceeded on the free tier - wait, or use a local model",
    "payload": "the endpoint rejected this request config",
    "other": "the request failed",
}


def http_error_parts(err: urllib.error.HTTPError) -> tuple[str, str]:
    """-> (status, message) pulled out of the JSON error body, if it has one."""
    body = _read_body(err)
    status, detail = "", ""
    try:
        j = json.loads(body)
        e = j.get("error") if isinstance(j, dict) else None
        if isinstance(e, dict):
            status = str(e.get("status") or "")
            detail = str(e.get("message") or "")
        elif e:
            detail = str(e)
    except Exception:
        detail = body[:220]
    if not detail:
        detail = body[:220]
    return status, " ".join((detail or "").split())


def _classify(status: str, detail: str, code: int) -> str:
    low = f"{status} {detail}".lower()
    st = (status or "").upper()
    if any(w in low for w in _KEY_PHRASES) or st in ("API_KEY_INVALID", "UNAUTHENTICATED"):
        return "key"
    if st == "NOT_FOUND" or code == 404 or "no longer available" in low \
            or "not found for this project" in low or "model is not found" in low:
        return "model"
    if st == "PERMISSION_DENIED" or code == 403 or "permission" in low or "access" in low:
        return "access"
    if st == "RESOURCE_EXHAUSTED" or code == 429 or "quota" in low or "rate limit" in low:
        return "quota"
    if st in ("INVALID_ARGUMENT", "FAILED_PRECONDITION") or code == 400:
        return "payload"
    return "other"


def http_info(err: urllib.error.HTTPError) -> tuple[str, str, str]:
    """
    -> (kind, status, api_message), reading the error body exactly ONCE.

    HTTPError.fp is a stream: a second read returns b'' and the caller silently
    loses the API's message (which is how "API key not valid" turned into a
    generic 'rejected this request config' the first time I split these helpers).
    """
    cached = getattr(err, "_spotube_http_info", None)
    if cached is not None:
        return cached
    status, detail = http_error_parts(err)
    info = (_classify(status, detail, err.code), status, detail)
    try:
        err._spotube_http_info = info
    except Exception:
        pass
    return info


def classify_http(err: urllib.error.HTTPError) -> tuple[str, str]:
    """(kind, api_message); see http_info() for why the body is read once."""
    kind, _status, detail = http_info(err)
    return kind, detail


def _http_error_text(err: urllib.error.HTTPError) -> str:
    """One actionable sentence, built from the same classification the retry uses."""
    kind, status, detail = http_info(err)
    hint = _KIND_HINTS.get(kind, _KIND_HINTS["other"])
    if kind == "key":
        hint = _KEY_HINTS_BY_STATUS.get((status or "").upper(), hint)
    if kind == "model" and detail:
        # the API usually names the replacement, which is the useful half
        hint = f"{hint} - the API said: {detail[:150]}"
    # repeat the API's words only when they add something the hint lacks
    lead = " ".join(detail.lower().split()[:3])
    tail = "" if not lead or lead in hint.lower() else f" {detail}"
    out = f"HTTP {err.code} {status} {hint}{tail}"
    return " ".join(out.split())[:400]


def _post(url: str, payload: dict, headers: dict, timeout: float | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout or _timeout()) as resp:
        return json.loads(resp.read().decode())


def _net_error_text(exc: Exception, where: str) -> object:
    """'URLError: <urlopen error [Errno 111]...>' is not a user-facing string."""
    reason = ""
    if isinstance(exc, urllib.error.URLError):
        reason = str(exc.reason)
    else:
        reason = str(exc) or exc.__class__.__name__
    low = reason.lower()
    where_low = (where or "").lower()
    local = ("localhost" in where_low or "127.0.0.1" in where_low
             or "::1" in where_low or "0.0.0.0" in where_low)
    if "refused" in low:
        if local:
            return (f"nothing listening at {where} - start the server "
                    f"(ollama serve) or fix Settings -> Base URL")
        return (f"connection refused by {where} - that host is not serving "
                f"the API (check the URL, and whether a firewall/VPN is blocking it)")
    if "timed out" in low or isinstance(exc, (TimeoutError, OSError)) and "timed out" in low:
        return f"timed out after {int(_timeout())}s talking to {where}"
    if "temporary failure in name resolution" in low or "nodename" in low:
        return f"cannot resolve the host in {where} (typo, or no network?)"
    if isinstance(exc, TimeoutError):
        return f"timed out after {int(_timeout())}s"
    return f"{reason} ({where})"[:300]


def _extract_json(text: str, lenient: bool = True) -> dict | None:
    """
    Accept a bare object, a fenced block, prose around it, OR a bare list of
    query strings (models do that constantly when told 'reply with queries').
    """
    text = (text or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start == -1 or end <= start:
            continue
        try:
            parsed = json.loads(text[start:end + 1])
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed:
            return {"queries": [str(x) for x in parsed]}
    if not lenient:
        return None
    # A reply cut off by maxOutputTokens is broken JSON but still contains the
    # queries. Salvaging it beats falling back to the offline parser.
    m = re.search(r'"queries"\s*:\s*\[(.*)', text, re.S)
    if m:
        region = m.group(1)
        close = region.find("]")
        if close != -1:
            region = region[:close]          # never reach past the array itself,
        items = []                           # or "avoid"/"why" become search strings
        for mt in re.finditer(r'"([^"]{3,})"(\s*:?)', region):
            if mt.group(2).strip():
                continue                     # that token is a key, not a value
            items.append(mt.group(1).strip())
        items = [i for i in items if i and not i.startswith("http")][:8]
        if items:
            return {"queries": items, "why": "recovered from a truncated reply",
                    "truncated": True}
    return None


GEMINI_MODEL_LADDER = ("gemini-3.5-flash", "gemini-3.1-flash-lite",
                       "gemini-3.6-flash", "gemini-3.7-flash")
SUGGESTED_MODEL: str | None = None      # the name Google's own error told us to use
MAX_CALLS = 4                           # hard cap on HTTP calls per plan()
_SHAPE_OK = 0                           # which payload shape worked last time
NOTES: list[str] = []                   # one-off "what I did and why" lines for the UI


def pop_notes() -> list[str]:
    out, NOTES[:] = list(NOTES), []
    return out


def _note(msg: str) -> None:
    if msg and msg not in NOTES:
        NOTES.append(msg)


def _clean_model(name: str) -> str:
    return (name or "").strip().split("/")[-1].strip()


def _model_candidates() -> list[str]:
    """
    Configured model first, then the model the API suggested, then the ladder.
    A retired default must not be a permanent failure, so we walk a few.
    """
    pref = [_clean_model(config.LLM_MODEL), _clean_model(config.GEMINI_DEFAULT_MODEL),
            SUGGESTED_MODEL or ""]
    out: list[str] = []
    for m in [p for p in pref if p] + list(GEMINI_MODEL_LADDER):
        if m and m not in out:
            out.append(m)
    return out


def _gemini_url(model: str | None = None) -> str:
    """
    Gemini lives on its own host, so a localhost base URL never hijacks it.
    The key goes in the x-goog-api-key header (the documented way) so it cannot
    leak into a URL, a proxy log or a shell history line.
    """
    base = (config.LLM_BASE_URL or "").rstrip("/")
    if not base or "generativelanguage" not in base:
        base = config.GEMINI_DEFAULT_URL
    model = _clean_model(model or config.LLM_MODEL or config.GEMINI_DEFAULT_MODEL)
    return f"{base}/models/{model}:generateContent"


def _gemini_headers() -> dict:
    return {"x-goog-api-key": config.LLM_API_KEY} if config.LLM_API_KEY else {}


def _suggested_model(text: str) -> str:
    """
    Google's retirement notice names the replacement:
    '...gemini-2.0-flash is no longer available. Please update your code to use
    models/gemini-3.6-flash...' -> 'gemini-3.6-flash'. Prefer the name after
    'use'; otherwise the last model id mentioned (the first one is the dead
    model we already tried).
    """
    t = text or ""
    m = re.search(r'use\s+models/(gemini-[A-Za-z0-9._-]+)', t)
    if m:
        return _clean_model(m.group(1))
    seen = re.findall(r'(gemini-[A-Za-z0-9._-]+)', t)
    return _clean_model(seen[-1]) if len(seen) > 1 else ""


def _gemini_text(data: dict) -> tuple[str, str]:
    """
    Returns (text, finish_reason). Thinking models emit several parts and the
    JSON is often NOT in part[0]; old code read [0] only and threw the answer
    away, which looks identical to 'the AI is broken'.
    """
    try:
        cand = (data.get("candidates") or [{}])[0]
    except (IndexError, TypeError):
        return "", ""
    finish = str(cand.get("finishReason") or "")
    parts = ((cand.get("content") or {}).get("parts")) or []
    chunks = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
    text = "\n".join(c for c in chunks if c).strip()
    if not text and isinstance(cand.get("content"), dict):
        text = str(cand["content"].get("text") or "").strip()
    if not text:
        blob = (data.get("promptFeedback") or {}).get("blockReason", "")
        return "", f"blocked:{blob}" if blob else "empty"
    return text, finish


def _gemini_parts(data: dict) -> list[str]:
    try:
        cand = (data.get("candidates") or [{}])[0]
    except (IndexError, TypeError):
        return []
    parts = ((cand.get("content") or {}).get("parts")) or []
    return [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]


def _gemini(prompt: str) -> dict | None:
    """
    Ask Gemini for the query plan.

    Three things this has to survive, all of which used to show up as a silent
    "engine: offline": a retired model name (404 - the message itself names the
    replacement), a generationConfig spelling this endpoint does not accept
    (400), and a cold first call (timeout). So: walk the model ladder, walk the
    payload shapes, retry once on transport trouble - under a hard wall-clock
    budget so a dead endpoint cannot hang the queue forever.
    """
    global SUGGESTED_MODEL, _SHAPE_OK
    if not config.LLM_API_KEY:
        _fail("gemini", "no API key configured")
        return None
    models = _model_candidates()
    shapes = _shapes(prompt)
    si = min(_SHAPE_OK, len(shapes) - 1)
    retries, calls = RETRIES, 0
    deadline = time.monotonic() + _timeout() * (1 + RETRIES)
    last: object = "no attempt made"
    mi = 0
    while mi < len(models) and calls < MAX_CALLS:
        model = models[mi]
        shape_name, payload = shapes[si]
        url = _gemini_url(model)
        if time.monotonic() > deadline:
            last = (f"gave up after {calls} attempt(s) - the endpoint is slower "
                    f"than the {int(_timeout())}s LLM timeout; raise it in Settings "
                    f"or pick a smaller model")
            break
        calls += 1
        try:
            data = _post(url, payload, _gemini_headers())
            text, finish = _gemini_text(data)
            if not text:
                last = f"{model} returned no text ({finish or 'unknown'})"
                break                       # reachable and authenticated: nothing to switch
            glued = "".join(_gemini_parts(data))
            parsed = (_extract_json(text, lenient=False)
                      or _extract_json(glued, lenient=False)
                      or _extract_json(text))
            if parsed is None:
                cut = " - answer was truncated mid-JSON" if finish.upper() == "MAX_TOKENS" else ""
                last = f"could not parse {model}'s output{cut}: {text[:120]!r}"
                break
            _adopt_model(model, models[0])
            if si != _SHAPE_OK:
                _SHAPE_OK = si
                _note(f"gemini: this endpoint wants the {shape_name!r} response config")
            LAST_ERRORS.pop("gemini", None)
            return parsed
        except urllib.error.HTTPError as e:
            kind, detail = classify_http(e)
            last = _http_error_text(e)      # reuses the cached body read
            if kind == "model":
                hint = _suggested_model(detail)          # the API's words only
                if hint and hint != model:
                    # try what Google named *next*, not after walking the ladder:
                    # the error is authoritative for this account and key.
                    models = models[:mi + 1] + [m for m in models[mi + 1:] if m != hint]
                    if hint not in models:
                        models.insert(mi + 1, hint)
                    SUGGESTED_MODEL = hint
                    _note(f"gemini: {model} is retired - the API says to use {hint}, "
                          f"trying that")
                else:
                    _note(f"gemini: the API has no model {model}, trying the next "
                          f"one on the list")
                mi += 1                       # never stop here: another model may work
                continue
            if kind == "payload" and si + 1 < len(shapes) and detail:
                si += 1
                _note(f"gemini: {model} rejected the {shape_name!r} config, trying the "
                      f"{shapes[si][0]!r} one")
                continue
            break                            # bad key / no access / quota: a retry is pointless
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = _net_error_text(e, url)
            if retries:
                retries -= 1
                _note("gemini: first call failed, retrying once")
                continue
            break
    if "not found" in str(last).lower() and len(models) > 1:
        _note("gemini: none of " + ", ".join(models[:4]) + " answered - set the Model "
              "field in Settings to a current one (https://ai.google.dev/gemini-api/docs/models)")
    _fail("gemini", last)
    return None


def _adopt_model(used: str, preferred: str) -> None:
    """
    Remember the model that actually answered. A retired default should cost one
    wasted request, not every request from now on - and the switch is logged,
    never silent.
    """
    want = _clean_model(used)
    if not want:
        return
    if want != _clean_model(preferred):
        _note(f"gemini: switched the model to {want} - {preferred or 'the configured one'} "
              f"was refused by the API (saved to config.json)")
    if _clean_model(config.LLM_MODEL) == want:
        return
    config.LLM_MODEL = want
    try:
        config.save_llm_config(LLM_MODEL=want)
    except Exception:
        pass          # a read-only home dir must not stop the music


# ---------------------------------------------------------------------------
# Restored after a bad edit anchor deleted this region - keep the ordering:
# schema -> payload shapes -> local endpoint -> engine choice -> plan().
# ---------------------------------------------------------------------------
_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "description": "5-8 YouTube Music search strings",
                    "items": {"type": "string"}},
        "avoid": {"type": "array", "description": "Words that mean 'not this'",
                  "items": {"type": "string"}},
        "why": {"type": "string", "description": "One short sentence on the reasoning"},
    },
    "required": ["queries"],
}


def _shapes(prompt: str) -> list[tuple[str, dict]]:
    """
    Payload variants, newest documented shape first. A 3.x endpoint wants
    generationConfig.responseFormat.text; the 2.x spelling was
    responseMimeType/responseSchema. Trying them in order means a stale shape
    degrades instead of failing, and the last entry always works.
    """
    contents = [{"role": "user", "parts": [{"text": f"{SYSTEM}\n\n{prompt}"}]}]
    return [
        ("responseFormat", {"contents": contents,
                            "generationConfig": {"maxOutputTokens": 8192,
                                                 "responseFormat": {"text": {
                                                     "mimeType": "application/json",
                                                     "schema": _SCHEMA}}}}),
        ("responseMimeType", {"contents": contents,
                              "generationConfig": {"maxOutputTokens": 8192,
                                                   "responseMimeType": "application/json",
                                                   "responseSchema": _SCHEMA}}),
        ("plain", {"contents": contents}),
    ]


def _openai_compat(prompt: str) -> dict | None:
    """Ollama / LM Studio / Open WebUI - anything speaking /v1/chat/completions."""
    base = (config.LLM_BASE_URL or "").rstrip("/")
    if not base or "generativelanguage" in base:
        _fail("local-llm", "no local base URL set (Settings -> Base URL, "
                           "e.g. http://localhost:11434)")
        return None
    url = base if url_is_chat(base) else f"{base}/v1/chat/completions"
    headers = {}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    body = {
        "model": config.LLM_MODEL or "llama3.2",
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.85,
        "max_tokens": 2048,
    }
    last: object = "no attempt made"
    deadline = time.monotonic() + _timeout() * (1 + RETRIES)
    for attempt in range(RETRIES + 1):
        if time.monotonic() > deadline:
            last = (f"gave up - the endpoint is slower than the {int(_timeout())}s "
                    f"LLM timeout; raise it in Settings or pick a smaller model")
            break
        try:
            data = _post(url, body, headers)
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            text = msg.get("content") or msg.get("reasoning_content") or ""
            parsed = _extract_json(text if isinstance(text, str) else str(text))
            if parsed is None:
                last = f"no JSON in model reply: {str(text)[:120]!r}"
                continue
            LAST_ERRORS.pop("local-llm", None)
            return parsed
        except urllib.error.HTTPError as e:
            kind, detail = classify_http(e)
            last = _http_error_text(e)
            if e.code in (408, 429, 500, 502, 503, 504):
                continue                      # busy/unavailable may clear on a retry
            break                             # 404 model, 401 key: a retry is pointless
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = _net_error_text(e, base)
            if attempt < RETRIES:
                _note("local model: first call failed, retrying once")
                continue
            break
    _fail("local-llm", last)
    return None


def url_is_chat(base: str) -> bool:
    return base.endswith("/chat/completions") or base.endswith("chat/completions")


def configured_engine() -> str:
    """
    Which brain will be used. A key is enough for Gemini even when Base URL is
    blank (that was the old silent failure: a key + the default localhost URL,
    or a cleared URL, meant 'offline' forever).
    """
    base = (config.LLM_BASE_URL or "").lower().rstrip("/")
    # ANY non-Gemini base URL counts as an OpenAI-compatible endpoint. The old
    # check only matched localhost/127.0.0.1, so Ollama on a LAN box or a
    # http://host.docker.internal URL was classed "offline" and never tried.
    if base and "generativelanguage" not in base:
        return "local-llm"
    if config.LLM_API_KEY:
        return "gemini"
    return "offline"


def plan(request: str, seeds: list[dict] | None = None) -> dict:
    """
    -> {"queries": [...], "avoid": [...], "why": str, "engine": str}
    `seeds` are tracks (from a liked list / playlist) used to steer the DJ.
    Never raises: falls back to the offline parser on any LLM trouble.
    """
    engine = configured_engine()
    taste = __import__("taste").preference_context()
    fallback = _heuristic(request)

    seed_block = ""
    if seeds:
        names = "; ".join(f"{s.get('artist', '?')} - {s.get('title', '?')}" for s in seeds[:12])
        seed_block = f"\nSeed tracks the listener enjoys (find things nearby this):\n{names}\n"

    prompt = (f"Taste profile:\n{taste}\n{seed_block}\nListener request: {request!r}\n\n"
              "Return 5-8 queries.")
    LAST_ERRORS.clear()
    NOTES.clear()
    plan_json = None

    def _ask(which: str):
        """plan() is documented never to raise: the offline parser must still
        get you music even if the API layer misbehaves on an unexpected shape."""
        try:
            return _gemini(prompt) if which == "gemini" else _openai_compat(prompt)
        except Exception as e:                     # noqa: BLE001 - last line of defence
            _fail(which, f"unexpected {e.__class__.__name__}: {str(e)[:160]}")
            return None

    if engine == "gemini":
        plan_json = _ask("gemini")
    elif engine == "local-llm":
        plan_json = _ask("local-llm")
    if plan_json is None and engine != "offline":
        # second engine only if the first one had nothing configured at all
        plan_json = _ask("local-llm" if engine == "gemini" else "gemini")

    if plan_json and isinstance(plan_json.get("queries"), list):
        # collapse internal whitespace: a query split across response parts can
        # arrive as "neo soul\nvelvet", a worse search string than one line
        qs = [" ".join(str(q).split()).lower() for q in plan_json["queries"] if str(q).strip()]
        qs = [q for q in dict.fromkeys(qs) if len(q) > 2][:8]
        if qs:
            avoid = [str(a).strip().lower() for a in (plan_json.get("avoid") or [])
                     if str(a).strip()]
            # merge in negatives the offline parser caught, so "no X" is honoured
            # even when the model forgot to report it.
            return {"queries": qs,
                    "avoid": list(dict.fromkeys(avoid + fallback.get("avoid", []))),
                    "why": str(plan_json.get("why", ""))[:200], "engine": engine,
                    "llm_notes": pop_notes()}

    reason = why_offline()
    if engine == "offline":
        fallback["engine"] = "offline"
    else:
        fallback["engine"] = f"{engine} (fallback)"
        fallback["llm_error"] = reason
    fallback["llm_notes"] = pop_notes()      # "switched model", "retrying", ...
    return fallback


if __name__ == "__main__":
    import sys
    print(json.dumps(plan(" ".join(sys.argv[1:]) or "chill lofi for coding"), indent=2))


def probe() -> dict:
    """
    Actually talk to the configured brain and report what happened. Used by
    Settings -> Test and by --doctor, because 'engine: offline' told you
    nothing about *why*, which is how a bad key looked like a timeout.
    """
    engine = configured_engine()
    out = {"engine": engine, "ok": False, "detail": "", "ms": 0}
    if engine == "offline":
        out["detail"] = ("no API key and no local base URL - using the offline "
                         "parser (works, just dumber)")
        out["ok"] = True
        return out
    t0 = __import__("time").monotonic()
    LAST_ERRORS.clear()
    plan = None
    try:
        plan = (_gemini("Reply with 3 queries for: mellow evening jazz")
                if engine == "gemini" else
                _openai_compat("Reply with 3 queries for: mellow evening jazz"))
    except Exception as e:                      # never let a probe raise
        out["detail"] = f"{e.__class__.__name__}: {e}"[:260]
        out["notes"] = pop_notes()
        return out
    out["ms"] = int((time.monotonic() - t0) * 1000)
    out["notes"] = pop_notes()
    if plan and isinstance(plan.get("queries"), list) and plan["queries"]:
        out["ok"] = True
        model = config.LLM_MODEL if engine == "gemini" else (config.LLM_MODEL or "local model")
        out["detail"] = (f"{len(plan['queries'])} queries in {out['ms']}ms via "
                         f"{model or config.GEMINI_DEFAULT_MODEL}, "
                         f"e.g. {str(plan['queries'][0])[:40]}")
    else:
        out["detail"] = (why_offline() or "the model replied but no usable JSON came back"
                         ).replace(f"{engine}: ", "")
    return out
