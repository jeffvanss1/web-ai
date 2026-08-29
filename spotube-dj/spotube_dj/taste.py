"""
Taste profile: learns from likes / skips, scores candidate tracks.

Deliberately dependency-free (no sentence-transformers) so it runs anywhere.
Same idea as SpotifyDJ's preferences.py, but keyed on YouTube-Music
artist/title tokens instead of Spotify ids, since that's what we can read
without Premium.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

import config
from config import load_state, save_state

_STOP = {
    "official", "video", "audio", "lyrics", "lyric", "visualizer", "visualizer",
    "mv", "hd", "hq", "4k", "1080p", "remastered", "remaster", "explicit",
    "version", "feat", "ft", "with", "the", "a", "an", "of", "and", "mix",
    "playlist", "radio", "live", "acoustic", "cover", "karaoke", "full",
}

# Descriptive tags we scrape out of YT titles to guess genre/mood.
_HINTS = {
    "lofi": 9.0, "lo-fi": 9.0, "chill": 7.0, "study": 6.0, "sleep": 6.0,
    "jazz": 7.0, "hip hop": 7.0, "hip-hop": 7.0, "rap": 6.0, "r&b": 6.0,
    "techno": 7.0, "house": 7.0, "trance": 6.0, "edm": 6.0, "dubstep": 7.0,
    "drum and bass": 8.0, "dnb": 7.0, "ambient": 7.0, "cinematic": 7.0,
    "orchestral": 7.0, "classical": 7.0, "metal": 7.0, "rock": 5.0,
    "punk": 6.0, "indie": 5.0, "pop": 3.0, "acoustic": 5.0, "folk": 6.0,
    "soul": 6.0, "funk": 6.0, "reggae": 6.0, "samba": 6.0, "bossa": 6.0,
    "gamel": 0.0, "video game": 6.0, "anime": 6.0, "phonk": 7.0,
    "synthwave": 7.0, "vaporwave": 7.0, "trap": 6.0, "drill": 6.0,
    "k-pop": 6.0, "kpop": 6.0, "j-pop": 6.0, "jpop": 6.0, "gufeng": 6.0,
    "dangdut": 8.0, "campursari": 8.0, "pop indonesia": 7.0, "indonesia": 4.0,
    "slow": 4.0, "fast": 4.0, "aggressive": 5.0, "relaxing": 5.0,
    "instrumental": 4.0, "piano": 5.0, "guitar": 4.0,
    "focus": 5.0, "workout": 5.0, "party": 5.0, "sad": 5.0, "happy": 5.0,
    "dark": 5.0, "dreamy": 5.0, "retrowave": 6.0, "bebop": 6.0,
    "bossa nova": 6.0, "trip hop": 6.0, "trip-hop": 6.0, "ska": 5.0,
    "blues": 6.0, "country": 6.0, "gospel": 5.0, "opera": 6.0,
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def tokens(s: str) -> list[str]:
    words = re.findall(r"[a-z0-9&']+", norm(s))
    return [w for w in words if w not in _STOP and len(w) > 1]


_PAREN = re.compile(r"[\(\[\{].*?[\)\]\}]")
# Version tags that mean a genuinely different recording: keep them, so a live
# take is not deduped away against the album cut.
_MEANINGFUL = re.compile(
    r"\b(?:live|unplugged|acoustic|stripped|remix|edit|slowed|sped|"
    r"instrumental|inst|demo|rough|choir|orchestral|mashup|bootleg|"
    r"radio\s+edit|extended|mtv)\b", re.I)
# " - Official Video" / " | Lyrics" trailing junk that is NOT a new recording.
_SUFFIX_NOISE = re.compile(
    r"\s*[-|–]\s*(official\s+(music\s+)?video|official\s+audio|audio|lyrics?|"
    r"visualizer|hd|hq|4k|explicit|mv)\b.*$", re.I)


def _keep_tag(text: str) -> str:
    """Keep a parenthetical only if it names a different recording."""
    return " " + text if _MEANINGFUL.search(text) else " "


def fingerprint(title: str) -> str:
    """
    Canonical song key, so the same recording uploaded by two channels isn't
    queued twice. "Joji - Slow Dancing (Official Video)" and "Slow Dancing
    (Lyrics)" fold together; "... (Live at BBC)" does NOT fold into the
    album cut, because that is a different thing to hear.
    """
    t = title or ""
    t = _PAREN.sub(lambda m: _keep_tag(m.group(0)), t)
    t = _SUFFIX_NOISE.sub("", t)
    if re.search(r"\s+[-|–]\s+", t):
        t = re.split(r"\s+[-|–]\s+", t, maxsplit=1)[-1]
    t = re.sub(r"[^\w\s]", " ", t.lower())
    t = re.sub(r"\s*(?:official|audio|lyrics?|visualizer|video|hd|hq|4k|"
               r"remaster(?:ed)?|soundtrack|from the|track)\b", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def title_hints(title: str) -> list[str]:
    t = norm(title)
    return [tag for tag in _HINTS if tag in t and _HINTS[tag] > 0]


def _bump(table: dict, key: str, delta: float, cap: float = 12.0) -> None:
    if not key:
        return
    key = norm(key)
    table[key] = max(-cap, min(cap, table.get(key, 0.0) + delta))


def _remember(state: dict, key: str, row: dict, dedupe: bool = True) -> None:
    """
    Add one judgement to a list whose size `config.save_state` owns (it keeps the
    newest 200 of each and prunes weights that have decayed to nothing).

    Re-hearting a song you already loved moves the row to the end instead of
    appending a copy: the same judgement twice is not twice the signal, and the
    "loved songs" list is read by a person, who would rightly ask why Angel
    appears four times. Skips are kept even when repeated, because "it came back
    and I skipped it again" is information the mixer should have.
    """
    rows = state.setdefault(key, [])
    if dedupe:
        key_pair = (row["title"], row["artist"])
        for i in range(len(rows) - 1, -1, -1):
            if (rows[i].get("title"), rows[i].get("artist")) == key_pair:
                rows.pop(i)
                break
    rows.append(row)


def record_like(track: dict) -> None:
    state = load_state()
    _remember(state, "liked", {
        "title": norm(track.get("title", "")),
        "artist": norm(track.get("artist", "")),
        # the GUI lists these rows, and norm() is a matching key, not something
        # you want to show a listener (it lowercases and strips punctuation).
        "display_title": (track.get("title") or "").strip(),
        "display_artist": (track.get("artist") or "").strip(),
        "ts": time.time(),
    })
    _bump(state["artists"], track.get("artist", ""), 2.0)
    for tag in title_hints(track.get("title", "")):
        _bump(state["genres"], tag, 1.0)
    save_state(state)


def forget_like(track: dict) -> int:
    """Remove matching liked rows (the title/artist pair, norm'd). -> count."""
    key = (norm(track.get("title", "")), norm(track.get("artist", "")))
    state = load_state()
    keep = [r for r in state["liked"]
            if (r.get("title"), r.get("artist")) != key]
    removed = len(state["liked"]) - len(keep)
    if removed:
        state["liked"] = keep
        save_state(state)
    return removed


def forget_artist(name: str) -> int:
    """
    Drop one artist from the profile (the sidebar's "unfollow"). -> count removed.

    The loved songs stay: they are records of what you liked, and deleting them
    because you stopped wanting more of an artist would be a strange reading of
    "not now". Only the weight goes, so the mix stops leaning on the name until a
    new love brings it back.
    """
    state = load_state()
    key = norm(name)
    artists = state.get("artists") or {}
    hits = [k for k in list(artists) if k == key or norm(k) == key]
    if not hits:
        return 0
    for k in hits:
        artists.pop(k, None)
    state["artists"] = artists
    save_state(state)
    return len(hits)


def is_liked(track: dict) -> bool:
    key = (norm(track.get("title", "")), norm(track.get("artist", "")))
    return any((r.get("title"), r.get("artist")) == key for r in load_state()["liked"])


def liked_rows(limit: int = 200) -> list[dict]:
    """Liked tracks, newest first, in the shape the track lists want."""
    rows = []
    for r in reversed(load_state()["liked"]):
        rows.append({"id": f"liked-{len(rows)}",
                     "title": r.get("display_title") or r.get("title") or "?",
                     "artist": r.get("display_artist") or r.get("artist") or "unknown",
                     "duration": 0, "url": "", "loved": True})
        if len(rows) >= limit:
            break
    return rows


def record_skip(track: dict, reason: str = "skip") -> None:
    state = load_state()
    # A skip near the start is a real dislike; late skips mostly mean "done".
    delta = -1.6 if reason == "early-skip" else -0.6
    _remember(state, "skipped", {
        "title": norm(track.get("title", "")),
        "artist": norm(track.get("artist", "")),
        "ts": time.time(),
        "reason": reason,
    }, dedupe=False)
    _bump(state["artists"], track.get("artist", ""), delta)
    for tag in title_hints(track.get("title", "")):
        _bump(state["genres"], tag, delta / 2.0)
    save_state(state)


def preference_context(max_items: int = 8) -> str:
    """Short text block fed to the LLM so it writes better queries."""
    state = load_state()
    artists = state["artists"]
    liked = sorted(artists.items(), key=lambda kv: -kv[1])[:max_items]
    hated = sorted(((k, v) for k, v in artists.items() if v < 0),
                   key=lambda kv: kv[1])[:max_items]
    genres = sorted(state["genres"].items(), key=lambda kv: -kv[1])[:max_items]
    parts = []
    if liked and liked[0][1] > 0:
        parts.append("artists the user loves: " + ", ".join(k for k, _ in liked if k))
    if genres:
        parts.append("moods/genres they favour: " + ", ".join(k for k, _ in genres if k))
    if hated:
        parts.append("artists to avoid: " + ", ".join(k for k, _ in hated if k))
    if state.get("last_request"):
        parts.append(f"previous request: {state['last_request']}")
    return "\n".join(parts) if parts else "no listening history yet"


def score_tracks(tracks: list[dict], avoid: list[str] | None = None) -> list[dict]:
    """
    Rank candidates. Score = artist affinity + tag affinity + freshness of
    the request + a small popularity nudge, minus recent-repeat penalty.
    `avoid` are terms the listener said they don't want ("no pop please").
    Mutates nothing; returns a new sorted list with 'score' set.
    """
    state = load_state()
    artists, genres = state["artists"], state["genres"]
    recent = Counter(norm(t.get("title", "")) for t in state["skipped"][-40:])
    avoid = [norm(a) for a in (avoid or []) if a]

    out = []
    for t in tracks:
        sc = 0.0
        hay = f"{norm(t.get('artist', ''))} {norm(t.get('title', ''))}"
        for term in avoid:
            if term and term in hay:
                sc -= 9.0
        sc += 1.35 * artists.get(norm(t.get("artist", "")), 0.0)
        for tag in title_hints(t.get("title", "")):
            sc += 0.55 * genres.get(tag, 0.0)
        # Long "mix"/"compilation" uploads are bad DJ units - one song, not 6h.
        dur = t.get("duration") or 0
        if dur > 600:
            sc -= 8.0
        elif 90 <= dur <= 420:
            sc += 1.5
        # Penalise titles we just skipped repeatedly (auto-DJ loop protection).
        sc -= 1.5 * min(recent.get(norm(t.get("title", "")), 0), 3)
        # Prefer "Topic"/"Auto-generated" YT Music uploads slightly: cleaner audio.
        ch = norm(t.get("artist_channel") or t.get("channel") or "")
        if "topic" in ch or "auto-generated" in ch:
            sc += 0.8
        t = dict(t)
        t["score"] = round(sc, 3)
        out.append(t)

    out.sort(key=lambda x: -x.get("score", 0.0))
    return out


def undo_file() -> Path:
    """Where the last cleared profile waits, in case the tap was a mistake."""
    return config.APP_DIR / "taste-undo.json"


def backup() -> dict:
    """Copy the learned profile to one undo file before it is wiped."""
    st = load_state()
    snap = {k: st.get(k) for k in ("liked", "skipped", "artists", "genres")}
    try:
        config.ensure_dirs()
        undo = undo_file()
        undo.write_text(json.dumps(snap, indent=1), encoding="utf-8")
        undo.chmod(0o600)
    except OSError:
        return {}
    return snap


def has_backup() -> bool:
    """
    Is there a snapshot to undo to? Existence only, no reading: the browser skin
    asks this on every 0.7 s tick, and parsing the file to answer a yes/no is how a
    status endpoint quietly becomes the slowest thing on the machine.
    """
    try:
        return undo_file().exists()
    except OSError:
        return False


def restore() -> dict:
    """
    Put a cleared profile back. -> what came back, or {} when there is nothing.

    One undo file, not a history: this exists so that "Forget my taste" can be a
    button instead of a scary one, not to be a version control system.
    """
    try:
        snap = json.loads(undo_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(snap, dict):
        return {}
    st = load_state()
    st.update({k: snap.get(k) or st.get(k) or ([] if k in ("liked", "skipped") else {})
               for k in ("liked", "skipped", "artists", "genres")})
    save_state(st)
    return {k: len(st.get(k) or []) for k in ("liked", "skipped", "artists", "genres")}


def clear() -> dict:
    """
    Wipe the learned profile (likes, skips, artist and tag weights).

    Returns what it removed, because "cleared" on its own makes a listener wonder
    whether they just lost something they cared about. Settings (volume, the last
    request, the API key) are untouched: this forgets *taste*, not the app.
    """
    st = load_state()
    gone = {"liked": len(st.get("liked") or []), "skipped": len(st.get("skipped") or []),
            "artists": len(st.get("artists") or {}), "genres": len(st.get("genres") or {})}
    backup()                        # clear is the one action here you cannot redo
    st.update({"liked": [], "skipped": [], "artists": {}, "genres": {}})
    save_state(st)
    return gone


def summarize() -> str:
    state = load_state()
    lines = [f"likes: {len(state['liked'])}   skips: {len(state['skipped'])}"]
    top_a = sorted(state["artists"].items(), key=lambda kv: -kv[1])[:5]
    top_g = sorted(state["genres"].items(), key=lambda kv: -kv[1])[:5]
    if top_a:
        lines.append("top artists: " + ", ".join(f"{k} ({v:+.1f})" for k, v in top_a))
    if top_g:
        lines.append("top moods:   " + ", ".join(f"{k} ({v:+.1f})" for k, v in top_g))
    neg_a = [kv for kv in sorted(state["artists"].items(), key=lambda kv: kv[1])[:3] if kv[1] < 0]
    if neg_a:
        lines.append("avoiding:    " + ", ".join(f"{k} ({v:+.1f})" for k, v in neg_a))
    return "\n".join(lines)


def _positive(table: dict) -> list[tuple[str, float]]:
    """Loved keys, strongest first, ignoring anything the weight is not positive."""
    return sorted(((k, float(v)) for k, v in (table or {}).items()
                   if k and float(v) > 0), key=lambda kv: -kv[1])


# connectives and ask-words: they are how a request is phrased, not what a
# recording is called, so they never belong in a search string
_STOP = {"to", "for", "and", "with", "the", "a", "an", "of", "in", "on", "at",
         "some", "any", "my", "me", "i", "you", "your", "we", "our", "us",
         "want", "need", "give", "play", "put", "make", "get", "songs", "song",
         "tracks", "track", "playlist", "stuff", "things", "please", "like"}


# words that already name a kind of music, so a query built from them needs no
# noun appended
_GENREISH = re.compile(
    r"\b(?:music|songs?|tracks?|beats?|mix|playlist|lofi|lo-fi|hip\s*hop|jazz|rock|"
    r"pop|metal|punk|soul|funk|blues|reggae|ambient|classical|trip\s*hop|folk|"
    r"country|rap|edm|house|techno|dnb|drum\s*and\s*bass|shoegaze|synthwave)\b", re.I)


def next_queries(avoid=None, limit: int = 3) -> list[str]:
    """
    What to search next because of what the listener has actually liked.

    This is the "keep playing related music" half of a DJ: when a queue runs
    down, re-running the original request gives you the same eight tracks again,
    whereas a profile of real likes knows you also like a lot of one artist and
    three tags you never asked for by name.

    Two shapes, both from the tables record_like() writes:
      * the artist's own deeper cuts - the single thing a listener most often
        wants next, and something the original mood query will never surface;
      * the favoured mood/genre on its own, which is how a "similar artist" list
        gets built without a similarity API.

    There is deliberately no "fans also like" lookup: YouTube Music exposes it
    only through an internal browse endpoint with no documented contract, and a
    DJ that quietly stops working every time Google reshuffles a JSON tree is
    worse than one with a plainer rule. `avoid` is the list of queries already
    used, so this never re-asks for what is in the queue.
    """
    state = load_state()
    used = {norm(a) for a in (avoid or []) if a}
    used.add(norm(state.get("last_request") or ""))
    liked_artists = _positive(state.get("artists"))
    liked_tags = _positive(state.get("genres"))
    out: list[str] = []

    def add(q: str) -> None:
        # a tag inherits the wording of the request it was born from, which can
        # end in a dangling preposition ("lofi beats to") - and a query like
        # "lofi beats to music" searches for nothing useful
        q = re.sub(r"\s+(?:to|for|with|and|of|the|a|an|in|on)$", "",
                   " ".join((q or "").split()), flags=re.I)
        if q and norm(q) not in used and len(out) < limit:
            used.add(norm(q))
            out.append(q)

    # an artist you liked enough to press the heart is worth a whole set of
    names = [k for k, w in liked_artists[: max(1, limit - 1)] if w >= 1.0]
    for name in names:
        add(f"{name} best songs")
    for tag, w in liked_tags[: limit]:
        if w >= 1.0:
            # clean the tag *before* it is composed into a query: " music"
            # appended after a dangling "to" hides it from the strip in add()
            tag = re.sub(r"\s+(?:to|for|with|and|of|the|a|an|in|on)$", "",
                         " ".join(str(tag or "").split()), flags=re.I).strip()
            if not tag:
                continue
            # " music" only when the tag is not already a genre name; "lofi
            # beats music" is not a thing anybody has uploaded
            add(tag if _GENREISH.search(tag) else f"{tag} music")
    if not out:
        # Nothing liked yet: fall back to the request's own strongest words so a
        # second round at least widens rather than repeating. Connectives and
        # activity words are dropped first - three raw tokens of "lofi beats to
        # relax for studying" used to produce "lofi beats to music", a query
        # that answers to nothing.
        words = [w for w in tokens(state.get("last_request") or "")
                 if w not in _STOP]
        if words:
            q = re.sub(r"\s+(?:relax(?:ing)?|relaxation|study|studying|sleep(?:ing)?|"
                       r"chill|focus|concentrate|meditat(?:e|ion)|workout|gym|"
                       r"background|vibes?)$",
                       "", " ".join(words[:3]), flags=re.I)
            q = q.strip()
            if q:
                add(q if _GENREISH.search(q) else f"{q} music")
    return out
