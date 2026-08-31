"""
cloudstate.py - the taste profile's second home: D1, through the Worker.

The profile is learned from what you skip, which makes it the most valuable
thing in `~/.spotube-dj` and the only thing a reinstall loses for good. This
module mirrors it to the Worker's D1 database and back, in two shapes:

* **a snapshot** - the whole `state.json`, upserted when it changes. That is a
  backup and a second machine's starting point.
* **an event log** - `like` / `unlike` / `skip` / `dislike` / `request` / `mix`,
  appended as they happen and replayed on another machine. Events are the part
  that genuinely merges: two machines can each love three songs overnight and
  neither loses the other's three.

Two rules keep this from being a sync engine, which it has no business being:

1. **local disk is authoritative.** A remote snapshot is adopted only when the
   local profile is empty (a fresh install on a second machine) or when the
   listener explicitly asks (*Pull my taste*). A background thread that
   silently overwrote tonight's listening with last week's cloud copy would be
   worse than no sync at all.
2. **nothing here runs on the request path.** Every call is either a background
   thread or a best-effort fire-and-forget. A dead Worker, a wrong token or a
   rate limit costs a line in the log drawer and nothing else - the player never
   waits on D1.

With no `WORKER_URL` (or sync off) every function here is a no-op that says so.
"""
from __future__ import annotations

import json
import threading
import time

import config
import taste
import workerclient

# How recently-pushed state is considered current, and how long to wait before
# pushing again after a change. A mix touches the profile a dozen times in a few
# seconds; that is one push, not twelve.
PUSH_DEBOUNCE = 20.0
FLUSH_EVERY = 15.0
MAX_EVENT_BATCH = 200

_lock = threading.RLock()
_events: list[dict] = []          # outbox, oldest first
_last_push = 0.0
_last_signature = ""
_last_event_id = 0
_status = {"ok": None, "detail": "", "pushed_at": 0, "pulled_at": 0,
           "events": 0, "last_event_id": 0, "pending": 0}
_thread: threading.Thread | None = None
_stop = threading.Event()
_dj = None


# ------------------------------------------------------------------ the switch

def enabled() -> bool:
    """Sync runs when a Worker is configured and the listener did not turn it off."""
    return workerclient.configured() and workerclient.sync_on()


# -------------------------------------------------------------------- snapshots

def _signature(state: dict) -> str:
    """A cheap identity for the profile, so an unchanged file costs no push."""
    try:
        return json.dumps(state, sort_keys=True, default=str)
    except Exception:
        return str(time.time())


def push_now(state: dict | None = None, *, note=None) -> int:
    """Upsert the profile. Returns `updated_at` (epoch ms), or 0 if it did not go."""
    global _last_push, _last_signature
    if not enabled():
        _set(ok=False, detail="sync is off")
        return 0
    try:
        state = state if state is not None else taste.load_state()
        updated = workerclient.state_put(state)
    except workerclient.WorkerError as e:
        _set(ok=False, detail=f"{e.kind}: {e.detail}"[:200])
        if note:
            note(f"[warn] taste not saved to the cloud: {e.kind} - {e.detail[:120]}")
        return 0
    except Exception as e:                                   # noqa: BLE001
        _set(ok=False, detail=f"{e.__class__.__name__}: {e}"[:200])
        return 0
    _last_push = time.time()
    _last_signature = _signature(state)
    _set(ok=True, detail="", pushed_at=int(_last_push * 1000))
    return updated


def pull(*, note=None) -> dict | None:
    """The remote snapshot, or None. Never touches the local profile."""
    if not enabled():
        return None
    try:
        state = workerclient.state_get()
    except workerclient.WorkerError as e:
        _set(ok=False, detail=f"{e.kind}: {e.detail}"[:200])
        if note:
            note(f"[warn] could not read the cloud profile: {e.kind} - {e.detail[:120]}")
        return None
    _set(ok=True, detail="", pulled_at=int(time.time() * 1000))
    return state if isinstance(state, dict) else None


def _profile_is_empty() -> bool:
    try:
        state = taste.load_state()
    except Exception:
        return False
    return not (state.get("liked") or []) and not any(
        float(w or 0) for w in (state.get("artists") or {}).values())


def adopt(force: bool = False, *, note=None) -> str:
    """
    Take the remote profile as this machine's, when that is the helpful thing.

    Only on a fresh install (`force` false and nothing learned locally) or on an
    explicit request. Returns a sentence for the log drawer.
    """
    if not enabled():
        return "cloud sync is off - nothing to pull"
    remote = pull(note=note)
    if not remote:
        return "no cloud profile saved yet under this profile name"
    if not force and not _profile_is_empty():
        return ("this machine already has a taste profile - the cloud copy was "
                "left alone (press Pull my taste again to overwrite it)")
    try:
        current = taste.load_state()
        merged = dict(current)
        merged.update(remote)
        taste.backup()                      # an overwrite is undoable, like clear
        config.save_state(merged)
    except Exception as e:                  # noqa: BLE001 - a pull must not crash
        return f"could not write the cloud profile here: {e.__class__.__name__}: {e}"
    n = len(remote.get("liked") or [])
    return f"took the cloud profile: {n} loved song(s) and its artist leanings"


# ----------------------------------------------------------------------- events

def record(kind: str, payload: dict | None = None) -> None:
    """Queue one taste event. Cheap, synchronous, never raises."""
    if not enabled():
        return
    with _lock:
        _events.append({"ts": int(time.time() * 1000), "kind": str(kind or "").strip(),
                        "payload": payload or {}})
        if len(_events) > 5000:            # a machine that never syncs must not grow
            del _events[:2000]


def flush(note=None) -> int:
    """Send the queued events. Returns how many were stored."""
    global _last_event_id
    if not enabled():
        return 0
    with _lock:
        if not _events:
            return 0
        batch = _events[:MAX_EVENT_BATCH]
    try:
        last = workerclient.events_post(batch)
    except workerclient.WorkerError as e:
        _set(ok=False, detail=f"{e.kind}: {e.detail}"[:200])
        if note:
            note(f"[warn] taste events not saved: {e.kind} - {e.detail[:120]}")
        return 0
    except Exception:                                        # noqa: BLE001
        return 0
    with _lock:
        del _events[:len(batch)]
        _last_event_id = max(_last_event_id, int(last or 0))
    _set(ok=True, detail="", events=int(_status.get("events") or 0) + len(batch),
         last_event_id=_last_event_id)
    return len(batch)


def _apply(event: dict) -> bool:
    """Replay one remote event into the local profile. Never raises."""
    kind = str((event or {}).get("kind") or "")
    payload = (event or {}).get("payload") or {}
    try:
        if kind == "like":
            taste.record_like(payload)
        elif kind == "unlike":
            taste.forget_like(payload)
        elif kind == "dislike":
            taste.record_dislike(payload)
        elif kind == "skip":
            taste.record_skip(payload, reason=str(payload.get("reason") or "skip"))
        elif kind == "forget_artist":
            taste.forget_artist(str(payload.get("artist") or ""))
        else:
            return False
    except Exception:                                        # noqa: BLE001
        return False
    return True


def replay(*, note=None) -> int:
    """Pull events newer than the last one seen and apply them. -> how many."""
    global _last_event_id
    if not enabled():
        return 0
    try:
        rows = workerclient.events_get(since=_last_event_id)
    except workerclient.WorkerError as e:
        _set(ok=False, detail=f"{e.kind}: {e.detail}"[:200])
        return 0
    applied = 0
    for row in rows:
        if _apply(row):
            applied += 1
        _last_event_id = max(_last_event_id, int(row.get("id") or 0))
    if applied and note:
        note(f"merged {applied} taste event(s) from another machine")
    _set(last_event_id=_last_event_id)
    return applied


# ------------------------------------------------------------------- lifecycle

def _set(**kw) -> None:
    with _lock:
        _status.update(kw)
        _status["pending"] = len(_events)


def status() -> dict:
    """What the page and --doctor show: one dict, no side effects."""
    cfg = workerclient.settings()
    with _lock:
        out = dict(_status)
        out["pending"] = len(_events)
    out.update({"configured": workerclient.configured(),
                "on": enabled(),
                "profile": str(cfg.get("profile") or "default"),
                # the host, never the token: this dict is drawn into a page
                "url": str(cfg.get("url") or "")})
    return out


def status_line() -> str:
    """One short sentence for --doctor."""
    if not workerclient.configured():
        return "off (no Worker URL - the profile stays on this machine)"
    if not enabled():
        return "off (WORKER_SYNC=off)"
    st = status()
    if st.get("ok") is False and st.get("detail"):
        return f"on, last push failed: {st['detail']}"
    if st.get("pushed_at"):
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(int(st["pushed_at"]) / 1000)
        return f"on - profile '{st['profile']}' last saved {when:%H:%M}"
    return f"on - profile '{st['profile']}' not saved yet"


def startup(dj=None, *, note=None) -> None:
    """Called once when the player opens: catch up, then start the mirror thread."""
    global _dj
    _dj = dj
    if not enabled():
        return
    threading.Thread(target=_catch_up, args=(note,), daemon=True).start()
    start()


def _catch_up(note=None) -> None:
    # order matters: replay the small stuff first, then consider the snapshot.
    # A second machine's overnight likes should land even if its snapshot push
    # never happened (it crashed, it was offline, sync was off for a week).
    replay(note=note)
    if _profile_is_empty():
        got = adopt(note=note)
        if note and "took the cloud profile" in got:
            note(got)


def start() -> None:
    """Start the background mirror: push on change, flush events on a timer."""
    global _thread
    if not enabled():
        return
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()


def _loop() -> None:
    last_flush = 0.0
    while not _stop.wait(5.0):
        if not enabled():
            continue
        now = time.time()
        try:
            state = taste.load_state()
            sig = _signature(state)
            if sig != _last_signature and now - _last_push >= PUSH_DEBOUNCE:
                push_now(state, note=(_dj._note if _dj else None))
        except Exception:                                    # noqa: BLE001
            pass
        if now - last_flush >= FLUSH_EVERY:
            last_flush = now
            try:
                flush(note=(_dj._note if _dj else None))
            except Exception:                                # noqa: BLE001
                pass
