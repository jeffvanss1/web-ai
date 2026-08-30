"""
The DJ narrator: a Spotify-DJ-style announcer that says why a song is playing
and what is coming next.

This is *not* a chat and *not* a voice call. It is a short, always-on line the
skin shows alongside the Now Playing card, refreshed on every track:

    "Now playing Radiohead. Why: you asked for 'chill lofi'; it's one of your
     picks. It's part of the lofi tuesday night set. Up next: Boards of Canada."

It is computed locally from what the mixer actually did (the request, a
from-your-likes pick, the station seed, the planner's reason, the Daylist vibe)
and the front of the queue. There is no WebSocket and no Gemini key required, so
it cannot fail with a dropped connection, and nothing is invented that the mixer
did not record.
"""

from __future__ import annotations

import config
import taste


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
    """A compact read of the set the DJ is hosting, for the narrator and tests."""
    dj = ctx.dj
    np = dj.current or {}
    info = dj.info or {}
    upnext: list[dict] = []
    try:
        upnext = [dict(x) for x in dj.queue.upcoming(8)]
    except Exception:
        pass
    try:
        prof = config.load_state() or {}
        artists = sorted((prof.get("artists") or {}).items(), key=lambda kv: -kv[1])
        genres = sorted((prof.get("genres") or {}).items(), key=lambda kv: -kv[1])
        liked_artists = [str(k) for k, v in artists if v > 0][:5]
        moods = [str(k) for k, v in genres if v > 0][:5]
    except Exception:
        liked_artists, moods = [], []
    return {
        "now": (f"{np.get('artist') or '?'} - {np.get('title') or '?'}"
                if np else "nothing playing"),
        "why": _current_why(dj, info, np),
        "next": (f"{upnext[0].get('artist') or '?'} - {upnext[0].get('title') or '?'}"
                 if upnext else ""),
        "vibe": str(info.get("vibe") or ""),
        "request": str(dj.request or ""),
        "station": str(getattr(dj, "station", "") or ""),
        "liked_artists": liked_artists,
        "moods": moods,
    }


def narrate(snap: dict) -> str:
    """A short, warm DJ line for the set: why now + what's next. Never raises."""
    now = str(snap.get("now") or "").strip()
    why = str(snap.get("why") or "").strip()
    nxt = str(snap.get("next") or "").strip()
    vibe = str(snap.get("vibe") or "").strip()
    if not now or now.lower() == "nothing playing":
        return ("Nothing playing yet - tell me a song or a mood "
                "(type in the search box) and the mix builds itself.")
    parts = [f"Now playing {now}."]
    if why:
        parts.append(f"Why: {why}.")
    if vibe:
        parts.append(f"It's part of the {vibe} set.")
    if nxt:
        parts.append(f"Up next: {nxt}.")
    return " ".join(parts)
