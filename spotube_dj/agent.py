"""
The DJ narrator: a Spotify-DJ-style announcer that says why a song is playing
and what is coming next.

It is *not* a chat box and *not* a network AI. It is a short, always-on line the
skin shows alongside the Now Playing card, refreshed on every track:

    "Now playing Radiohead. Why: you asked for 'chill lofi'; it's one of your
     picks. It's part of the lofi tuesday night set. Up next: Boards of Canada."

It is computed locally from what the mixer actually did (the request, a
from-your-likes pick, the station seed, the planner's reason, the Daylist vibe)
and the front of the queue. There is no WebSocket and no Gemini key required, so
it cannot fail with a dropped connection, and nothing is invented that the mixer
did not record.

The same line can be *spoken* aloud on each new track (see `spotube_dj.djvoice`)
so the DJ reads "why this song / what's next" over the music, like Spotify DJ.
`dj_speech` produces the voice-friendly sentence; `narrate` the on-screen one.
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


def snapshot_of(dj) -> dict:
    """A compact read of the set the DJ is hosting, from the DJ object itself."""
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


def dj_snapshot(ctx) -> dict:
    """A compact read of the set the DJ is hosting, for the narrator and tests.

    Accepts either the Context the web layer passes, or a bare DJ object, so a
    caller that only has a DJ (e.g. the spoken-voice trigger) can use it too.
    """
    return snapshot_of(getattr(ctx, "dj", ctx))


def dj_speech(dj) -> str:
    """A spoken-friendly DJ announcement: why now + what's next, read aloud.

    differs from `narrate` in that it is built for a text-to-speech voice, not an
    on-screen card: no "Why:" label, no parentheses, artist and title separated by
    a comma so a synthesizer reads them as two nouns rather than a "minus" sign.
    """
    snap = snapshot_of(dj)
    now = str(snap.get("now") or "").strip()
    if not now or now.lower() == "nothing playing":
        return ""                       # nothing to announce; stay quiet
    artist, sep, title = now.partition(" - ")
    # a DJ announcer signposts before naming the song, then leads into what's next.
    bits = [f"Alright, coming up next - here's {artist}," if sep
            else f"Alright, coming up next - here's {now}."]
    if sep and title:
        bits.append(f"{title}.")
    why = str(snap.get("why") or "").strip()
    if why:
        why = why[0].upper() + why[1:]          # a spoken sentence starts with a capital
        bits.append(f"{why}.")
    vibe = str(snap.get("vibe") or "").strip()
    if vibe:
        bits.append(f"It's all part of the {vibe} set.")
    nxt = str(snap.get("next") or "").strip()
    if nxt:
        a, s2, t = nxt.partition(" - ")
        bits.append(f"Stay right here - up next {a}," if s2
                    else f"Stay right here - up next {nxt}.")
        if s2 and t:
            bits.append(f"{t}.")
    return " ".join(bits)


def dj_prompt(dj, lang: str = "English") -> str:
    """A personality prompt that asks a Gemini model to write a *creative* DJ line.

    The spoken line is not a canned read: we hand the model the real facts (what
    is playing, why, the set's name, what's next) and ask it to say them the way
    a warm, playful radio DJ would - so every track gets its own turn of phrase.
    `lang` is the written language for the chosen voice (e.g. 'English', 'Arabic').
    `dj_speech` remains the keyless fallback when there is no key/network.
    """
    snap = snapshot_of(dj)
    now = str(snap.get("now") or "nothing playing")
    why = str(snap.get("why") or "").strip()
    nxt = str(snap.get("next") or "").strip()
    vibe = str(snap.get("vibe") or "").strip()
    if not now or now.lower() == "nothing playing":
        return ""
    facts = f"the song now playing is {now}"
    if why:
        facts += f"; the reason it's on is {why}"
    if vibe:
        facts += f"; it's part of the {vibe} set"
    if nxt:
        facts += f"; up next is {nxt}"
    return (
        "You are a live radio DJ announcer, on-air and full of energy - warm, "
        "smooth and a little playful. Introduce the song that is starting exactly "
        "the way a real DJ host would say it over the music. Say ONE short spoken "
        "line: open with a natural signpost (something like 'Alright, here we go', "
        "'Keeping it moving now', 'Coming up right here', 'This one's a vibe'), "
        "name the track and artist, weave in why it's on and the set's mood, and "
        "tease what's up next - all in your own words, not a list. Sound like "
        "you're on air, not reading a screen; never mention that you are an AI. "
        "Keep it to one or two sentences (about 15 to 35 words). Write the line in "
        + lang + ". Facts: "
        + facts + "."
        " Reply with only the line to speak - no labels, no quotes, no preamble."
    )


def lead_prompt(dj, lang: str = "English") -> str:
    """A Gemini prompt for a short *lead-in* to the song coming up next.

    Used ~lead seconds before the current track ends, so the DJ hands over to the
    next song instead of talking after it has already started.
    """
    snap = snapshot_of(dj)
    nxt = str(snap.get("next") or "").strip()
    if not nxt:
        return ""
    vibe = str(snap.get("vibe") or "").strip()
    why = str(snap.get("why") or "").strip()
    context = f"It's part of the {vibe} set." if vibe else ""
    if why and not vibe:
        context = why[0].upper() + why[1:] + "."
    return (
        "You are a warm, energetic radio DJ announcer, live on air. The song "
        "currently playing is almost finished. Give a short spoken lead-in that "
        "hands over to the next song exactly like a DJ host announcing it on the "
        "radio: a quick energetic signpost (something like 'Up next', 'Stay right "
        "here', 'Next up, we've got', 'Right after this'), the next track and "
        "artist, and a hint of the set's mood. Keep it to one or two sentences "
        "(about 10 to 25 words), friendly and energetic. The next song is "
        + nxt + "."
        + ((" " + context) if context else "")
        + " Write the line in " + lang + ". "
        "Reply with only the line to speak - no labels, no quotes, no preamble."
    )


def lead_line(dj) -> str:
    """A keyless template for the up-next lead-in (used when there is no key)."""
    snap = snapshot_of(dj)
    nxt = str(snap.get("next") or "").strip()
    if not nxt:
        return ""
    a, s, t = nxt.partition(" - ")
    vibe = str(snap.get("vibe") or "").strip()
    line = f"Stay right here - up next {a}," if s else f"Stay right here - up next {nxt}."
    if s and t:
        line += f" {t}."
    if vibe:
        line += f" It's all part of the {vibe} set."
    return line + " Coming right up."


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
