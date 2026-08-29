"""
viewmodel.py - pure presentation logic for the GUI, no Tk.

Everything here is a plain function so the formatting/state rules can be unit
tested without a display. gui.py is deliberately dumb and calls into this.
"""

from __future__ import annotations

import math
import re

# ------------------------------------------------------------------ palette
# The layout people already know: Spotify's greys with its green accent, so a
# new user reads "music app" before reading a word of it. Everything is a flat
# grey ramp - the old near-black + cyan boxed look read like a terminal, which
# is what made the first version hard to scan.
BG          = "#121212"   # window
SIDEBAR     = "#000000"   # left rail, as in Spotify
PANEL       = "#181818"   # cards / now playing
PANEL_EDGE  = "#2a2a2a"
CARD        = "#1c1c1c"
ROW_ALT     = "#1f1f1f"
HOVER       = "#2a2a2a"
INPUT_BG    = "#242424"
ACCENT      = "#1db954"
ACCENT_DK   = "#169c46"
ACCENT_TXT  = "#000000"    # text on the green pill
NAV_SEL_BG    = HOVER           # selected nav row / mood chip background
NAV_SEL_FG    = ACCENT          # ... and its text colour
PLAYING_BG    = "#123a20"       # the row that is currently on the air
MENU_BG       = "#0f0f0f"       # right-click menus: darker than everything else
ROW_PLAYED_BG = "#181818"       # a row this person has already heard
TEXT        = "#ffffff"
MUTED       = "#a7a7a7"
MUTED_DK    = "#6a6a6a"
SUCCESS     = "#1ed760"
ERROR       = "#e2445c"
HEART       = "#1ed760"    # Spotify's loved-heart is green, not pink

# Deterministic "no artwork" tiles: a coloured initial, exactly like the
# placeholder a streaming app shows before art loads.
TILE_PALETTE = ("#5b3a8f", "#8f4a3a", "#3a6f8f", "#3a8f5d", "#8f3a6a",
                "#6a6a3a", "#3a8f8a", "#7a4d5d", "#4d5d8f", "#4d8f7a")

# DejaVu Sans is the closest thing every Linux box has to the UI sans people
# expect; gui.py swaps in a family that actually exists on the machine.
UI_FAMILY = "DejaVu Sans"

FONTS = {
    "title":   (UI_FAMILY, 19, "bold"),
    "big":     (UI_FAMILY, 15, "bold"),
    "label":   (UI_FAMILY, 9, "bold"),
    "body":    (UI_FAMILY, 11),
    "button":  (UI_FAMILY, 12, "bold"),
    "track":   (UI_FAMILY, 11, "bold"),
    "artist":  (UI_FAMILY, 10),
    "small":   (UI_FAMILY, 9),
    "log":     ("DejaVu Sans Mono", 9),
    "nav":     (UI_FAMILY, 11, "bold"),
}
SMALL_FONTS = {k: (v[0], max(7, v[1] - 2)) + tuple(v[2:]) if len(v) > 2
               else (v[0], max(7, v[1] - 2)) for k, v in FONTS.items()}


def font_set(small: bool, family: str | None = None) -> dict:
    """
    Font table for the app. `family` lets the GUI substitute a font that is
    actually installed (a missing family silently falls back to a serif, which
    looks broken); the mono log keeps its own family.
    """
    base = dict(SMALL_FONTS if small else FONTS)
    if family:
        base = {k: ((family if v[0] == UI_FAMILY else v[0],) + v[1:])
                for k, v in base.items()}
    return base


def pick_family(available) -> str | None:
    """
    First UI font present on this machine, or None to keep the default table.
    `available` is any container of family names (tkfont.families()).
    """
    have = {str(a).lower() for a in (available or [])}
    for cand in (UI_FAMILY, "Liberation Sans", "Noto Sans", "Ubuntu",
                 "Cantarell", "Segoe UI", "Helvetica", "Arial", "Verdana"):
        if cand.lower() in have:
            return cand
    return None


# -------------------------------------------------------------- text layout
def ellipsize(s: str, width: int) -> str:
    """Truncate like the reference bar does: 'Forge Ahead  —  Mi…'."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    if width <= 1 or len(s) <= width:
        return s
    return s[: max(1, width - 1)].rstrip() + "…"


def track_line(title: str, artist: str, width: int) -> str:
    """
    One row: 'title  —  artist', trimmed to fit `width` chars.
    The *title* is what a listener scans for, so it gets the lion's share of the
    space (min 55%) and only shrinks once the row is truly out of room; the
    artist is truncated first.
    """
    title = re.sub(r"\s+", " ", (title or "?").strip())
    artist = re.sub(r"\s+", " ", (artist or "unknown").strip())
    full = f"{title}  —  {artist}"
    if len(full) <= width:
        return full
    sep = "  —  "
    title_room = max(1, min(len(title), int(width * 0.62)))
    keep_title = ellipsize(title, title_room)
    room = width - len(keep_title) - len(sep)
    if room < 3:
        # row is narrower than the separator alone: title-only
        return ellipsize(title, max(3, width))
    keep_artist = ellipsize(artist, room)
    return f"{keep_title}{sep}{keep_artist}"


def mmss(seconds) -> str:
    """
    '3:20' for a duration, '0:00' for anything that is not one.

    A state file can hold `-30`, `Infinity` (json accepts it) or a string, and this
    runs for every visible row, so nothing here may raise and nothing here prints
    '-1:55' as if it were a time.
    """
    try:
        s = int(float(seconds or 0))
    except (TypeError, ValueError, OverflowError):
        return "0:00"
    if s < 0 or s > 86400:        # a day is not a song: it is garbage or a livestream
        return "0:00"
    return f"{s // 60}:{s % 60:02d}"


def now_playing_line(title: str, artist: str, width: int,
                     elapsed: float = 0.0, duration: float = 0.0) -> str:
    base = track_line(title, artist, width)
    if duration:
        return f"{base}   {mmss(elapsed)}/{mmss(duration)}"
    return base


# ------------------------------------------------------------- activity log
_LEVEL_RE = [
    (re.compile(r"^\s*(\[error\]|error|failed|couldn|could not|blocked|no results|nothing)", re.I), ERROR),
    (re.compile(r"(playing:|queued|topped up|learned|liked|saved|exported|found)", re.I), SUCCESS),
    (re.compile(r"(warn|falling back|fallback|skipping|empty|not found|offline)", re.I), ACCENT),
]


def log_colour(line: str) -> str:
    for rx, colour in _LEVEL_RE:
        if rx.search(line or ""):
            return colour
    return TEXT


def format_activity(lines: list[str], cap: int = 400) -> str:
    """Join with newlines, keeping only the last `cap` entries."""
    return "\n".join(lines[-cap:])


def status_dot(playing: bool, busy: bool = False) -> tuple[str, str]:
    """(text, colour) for the little indicator before the track name."""
    if busy:
        return "dot_busy", ACCENT
    if playing:
        return "dot_on", SUCCESS
    return "dot_off", MUTED


# ----------------------------------------------------------------- UI state
class PlayLock:
    """
    Guards the Play button. A search round takes several seconds; without this a
    second click stacks a second engine run and the two fight over the queue.
    `Continue` is allowed while playing (that's its purpose) but not while busy.
    """

    def __init__(self) -> None:
        self._busy = False
        self.held_by = ""

    @property
    def busy(self) -> bool:
        return self._busy

    def acquire(self, who: str) -> bool:
        if self._busy:
            return False
        self._busy, self.held_by = True, who
        return True

    def release(self) -> None:
        self._busy, self.held_by = False, ""


def button_states(busy: bool, has_request: bool, has_last_request: bool) -> dict:
    """Which of Play / Continue are live, and their labels."""
    return {
        "play_text": "Working…" if busy else "Play",
        "play_enabled": (not busy) and has_request,
        "continue_enabled": (not busy) and has_last_request,
    }


def engine_badge(engine: str | None) -> tuple[str, str]:
    """(label, colour) showing which brain is actually driving the queries."""
    e = (engine or "").lower()
    if "gemini" in e and "fallback" not in e:
        return "BRAIN: GEMINI", SUCCESS
    if "local" in e and "fallback" not in e:
        return "BRAIN: LOCAL LLM", SUCCESS
    if "fallback" in e:
        return "BRAIN: OFFLINE (LLM FAILED)", ERROR
    return "BRAIN: OFFLINE", ACCENT


def rows_needing_art(tracks, have, limit: int = 14) -> list[dict]:
    """
    Which rows to ask the artwork thread for: the ones with no picture yet.

    Taking the first N rows of a list meant rows 15+ of a search result never
    got a cover at all (nothing re-asks when you scroll), which is the "only some
    songs have artwork" report. `have` is the caller's cheap cached? probe, passed
    in so this file stays free of the cache layer and stays testable.
    """
    out = []
    for t in tracks or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        try:
            if have(t):
                continue
        except Exception:
            pass                      # a broken probe must not hide every picture
        out.append(t)
        if len(out) >= limit:
            break
    return out


def now_artist(artist, idle: str = "") -> str:
    """
    The Now Playing subtitle: who it is by, plus *why nothing new is coming*.

    A blank panel reads as a crash. The track is still audible when a skip could
    not be honoured (no stream would start) or when the queue ran out, so the
    title stays and the reason goes here: `still audible - the queue ran out`
    rather than `Nothing playing` over the sound of the last chorus.
    """
    who = str(artist or "unknown")
    why = {"finished": "still audible - the queue ran out",
           "no stream would start": "still audible - no stream would start"}.get(
               str(idle or ""), "")
    return f"{who}   {why}" if why else who


def queue_preview(tracks: list[dict], rows: int, width: int) -> list[str]:
    """
    The Up Next lines. Two of them are worth saying out loud: a track that is
    already on disk ("no waiting between songs" is a promise you can only trust
    once you have seen it), and a track the taste profile added rather than
    something the request asked for - the DJ's initiative should be visible, not
    mysterious.
    """
    out = []
    for t in (tracks or [])[:rows]:
        line = track_line(t.get("title", ""), t.get("artist", ""), width)
        notes = []
        if t.get("cached"):
            notes.append("cached")
        if t.get("mixed"):
            notes.append("from your likes")
        if notes:
            tail = " · " + ", ".join(notes)
            line = ellipsize(line, max(8, width - len(tail))) + tail
        out.append(line)
    return out


# ------------------------------------------------- request -> album/OST mode
_OST_HINT = re.compile(
    r"\b(?:ost|o\.s\.t\.?|soundtrack|score|game\s+music|anime\s+ost|"
    r"film\s+score|score\s+from|music\s+from)\b", re.I)
# NB: do not put \d in the lookahead - a lazy {2,40}? then stops one char
# early and "destiny 2" silently degrades to "destiny".
_FROM_TO = re.compile(
    r"\b(?:from|of|for|in)\s+([a-z0-9][a-z0-9 '&:,-]{1,40}?)"
    r"(?=\s*(?:\bthe\b|ost|soundtrack|score|music|please|for me|played|"
    r"\d\d:\d\d|$|[,.!?]))", re.I)


def detect_album_mode(request: str) -> str | None:
    """
    'play the ost from destiny 1' -> 'destiny 1'.
    Returns the subject to search as an *album*, or None for normal mode.
    Soundtracks only come back reliably when you search the album title, not
    a vibes phrase - that's why this exists.
    """
    if not request or not _OST_HINT.search(request):
        return None
    m = _FROM_TO.search(request)
    if m:
        subject = m.group(1)
    else:
        subject = _OST_HINT.sub(" ", request)
    # strip request verbs/qualifiers but NEVER a trailing numeral: sequels are
    # the whole point of an OST request ("destiny 2" != "destiny").
    subject = re.sub(r"\b(?:the|play|put on|some|any|ost|o\.s\.t\.?|soundtrack|"
                     r"score|music|from|of|for|game|album|full|please)\b", " ",
                     subject, flags=re.I)
    subject = re.sub(r"[^\w\s]", " ", subject)
    subject = re.sub(r"\s+", " ", subject).strip().lower()
    return subject if len(subject) > 2 else None


def album_queries(subject: str) -> list[str]:
    """Search strings that land on real soundtrack tracks for `subject`."""
    s = subject.strip()
    return [f"{s} ost", f"{s} soundtrack", f"{s} ost full album"]


def album_mode_note(subject: str) -> str:
    return (f"album mode: '{subject}' is a soundtrack request - searching "
            f"album titles so we get the actual OST tracks, not a radio mix")


# ============================================================= new GUI layer
# Everything the reworked window needs to decide *what to say*. Kept here, not
# in gui.py, because this is the part users actually read - and it is all testable
# without a display.

# The views a front end can show. This table is the contract: the page's section
# ids are `view-<name>`, so a view listed here without a section is a blank screen,
# and a section with no entry here cannot be reached.
VIEWS = ("home", "search", "library", "history")

NAV = (
    ("home",    "home",    "Home"),
    ("search",  "search",  "Search"),
    ("library", "library", "Your Library"),
    ("history", "clock",   "Recently played"),
)

# Mood chips on the home screen: a beginner gets one-click music without having
# to phrase a request, and each one is just the text the box would have taken.
MOODS = (
    ("Deep focus",     "mellow instrumental beats for deep focus"),
    ("Chill evening",  "warm soul and soft rock for a chill evening"),
    ("Late night",     "quiet neo soul and jazz for late at night"),
    ("Workout",        "high energy workout music, drums and bass"),
    ("Throwback",      "70s 80s classics, no remixes"),
    ("Rainy day",      "slow acoustic songs for a rainy day"),
    ("Party",          "upbeat funk and disco party tracks"),
    ("Road trip",      "classic rock road trip singalong"),
)

# (start hour, end hour, words). Public because the browser skin ships the same
# table to its own greeting rather than inventing a second set of boundaries.
GREETING = ((5, 12, "Good morning"), (12, 18, "Good afternoon"), (18, 24, "Good evening"),
            (0, 5, "Still up?"))
_GREETING = GREETING


def greeting(now=None) -> str:
    hour = (now or __import__("datetime").datetime.now()).hour
    for lo, hi, text in _GREETING:
        if lo <= hour < hi:
            return text
    return "Good listening"


def mood_at(index: int) -> tuple[str, str]:
    """Wrap-around lookup so the shuffle button can pick any mood safely."""
    i = int(index) % len(MOODS)
    return MOODS[i]


def fmt_count(n, singular: str, plural: str | None = None) -> str:
    n = int(n or 0)
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def human_status(engine: str | None, llm_error: str = "", key_set: bool = False) -> tuple[str, str]:
    """
    (sentence, colour) for the one-line "is my AI working" strip.

    Deliberately plain language: the old `BRAIN: OFFLINE (LLM FAILED)` badge
    scared people who had nothing wrong with them (no key = offline parser is the
    normal, working state), and it said nothing actionable to anyone else.
    """
    e = (engine or "").lower()
    err = " ".join((llm_error or "").split())
    if "gemini" in e and "fallback" not in e:
        return ("AI planner on (Gemini)" + (f" - last call failed: {err[:90]}" if err else ""),
                ERROR if err else SUCCESS)
    if "local" in e and "fallback" not in e:
        return ("AI planner on (local model)"
                + (f" - last call failed: {err[:90]}" if err else ""), ERROR if err else SUCCESS)
    if "fallback" in e:
        return ("AI planner is not working, using the built-in one"
                + (f" - {err[:110]}" if err else ""), ERROR)
    if key_set:
        return ("A key is saved but the planner is off - open Search & AI to test it",
                MUTED)
    return ("Built-in planner (works with no account). "
            "Add a free Gemini key in Search & AI for smarter mixes", MUTED)


def engine_chip(engine: str | None, llm_error: str = "", key_set: bool = False) -> tuple[str, str, str]:
    """
    (label, colour, tooltip-ish detail) for the small pill in the header. The
    label stays short because it sits next to the Settings button; the detail
    belongs in the log, which is where the full reason is printed.
    """
    detail, colour = human_status(engine, llm_error, key_set)
    # short, fixed label - the header pill is next to a button, not a paragraph
    e = (engine or "").lower()          # was missing: this function used
    label = {"gemini": "smart search: on", "local-llm": "smart search: on",
             "offline": "built-in search"}.get(e, "")
    if not label:                        # human_status's `e` and died with a
        label = ("smart search: failed" if "fallback" in e else "built-in search")
    return label, colour, detail         # NameError on "gemini (fallback)"


def tile_colour(seed: str) -> str:
    """Stable colour for a track with no artwork, so rows don't flicker."""
    h = 0
    for ch in (seed or "?"):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return TILE_PALETTE[h % len(TILE_PALETTE)]


def tile_letter(track: dict) -> str:
    t = (track or {}).get("title") or "?"
    t = str(t).strip()
    # skip a leading article: "The Wall" should be W, like a real library sort
    for art in ("the ", "a ", "an "):
        if t.lower().startswith(art) and len(t) > len(art) + 1:
            t = t[len(art):]
            break
    return (t[:1] or "?").upper()


def track_rows(tracks: list[dict], width_px: int, char_px: int,
               show_duration: bool = True, two_line: bool = False) -> list[dict]:
    """
    Rows for the list widget: title/artist pre-truncated to the space left after
    the fixed columns (art 44 + actions ~96 + duration 46). Truncating here keeps
    gui.py free of layout maths and makes the clipping testable.
    """
    fixed = 44 + 96 + (52 if show_duration else 0) + 28
    avail = max(24, width_px - fixed)
    if two_line:
        # title and artist are stacked rows, so each gets the full column
        title_chars = max(8, int(avail * 0.98) // max(1, char_px))
        artist_chars = title_chars
    else:
        # one line: the title is what a listener scans for, so it gets 60%
        title_chars = max(6, int(avail * 0.60) // max(1, char_px))
        artist_chars = max(6, int(avail * 0.40) // max(1, char_px))
    out = []
    for t in (tracks or []):
        out.append({
            "id": t.get("id"),
            "title": ellipsize(str(t.get("title") or "?"), title_chars),
            "artist": ellipsize(str(t.get("artist") or t.get("channel") or "unknown"),
                                 artist_chars),
            "duration": mmss(t.get("duration") or 0) if show_duration else "",
            "raw": t,
            "played": bool(t.get("played")),
            "loved": bool(t.get("loved")),
        })
    return out


def queue_rows(current: dict | None, upcoming: list[dict], width_px: int,
               char_px: int) -> list[dict]:
    """Queue panel rows: the playing track first (marked), then what is next."""
    rows = []
    if current:
        rows.append(dict(current, _state="Playing"))
    rows.extend(dict(t, _state="") for t in (upcoming or [])[:12])
    out = []
    for i, t in enumerate(rows, 1):
        state = t.get("_state") or ""
        out.append({
            "n": i,
            "title": ellipsize(str(t.get("title") or "?"),
                               max(6, int((width_px - 66) * 0.58) // max(1, char_px))),
            "artist": ellipsize(str(t.get("artist") or "unknown"),
                                max(6, int((width_px - 66) * 0.34) // max(1, char_px))),
            "state": state,
            "raw": t,
        })
    return out


def transport_state(playing: bool, paused: bool, busy: bool,
                    liked: bool = False) -> dict:
    """
    What the bottom bar shows. `playing` means "a track is loaded and audible";
    paused/busy change the glyph only. Kept separate from button_states() because
    Play (build the queue) and Play/Pause (transport) are different things, and
    conflating them is how the old UI ended up with a button that did two jobs.
    """
    if busy:
        label, hint = "…", "working"
    elif not playing:
        label, hint = "play", "nothing loaded"
    else:
        label, hint = ("play", "paused") if paused else ("pause", "playing")
    return {"playpause": label, "hint": hint,
            "heart": "liked" if liked else "like",
            "seek_enabled": bool(playing),
            "prev_enabled": bool(playing), "next_enabled": bool(playing)}


def progress_frac(pos, dur) -> float:
    """0..1 for the seek bar; never divides by zero and never exceeds 1."""
    try:
        pos, dur = float(pos or 0), float(dur or 0)
    except (TypeError, ValueError):
        return 0.0
    if dur <= 0:
        return 0.0
    return max(0.0, min(1.0, pos / dur))


def seek_target(frac, dur) -> float:
    """Where a drag at `frac` of the bar should land, in seconds (clamped to the track)."""
    try:
        secs = float(frac) * float(dur or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(secs):          # inf * a duration is not a seek position
        return 0.0
    return min(max(0.0, secs), max(0.0, float(dur or 0)))


def time_line(pos, dur) -> str:
    return f"{mmss(pos)} / {mmss(dur)}" if dur else mmss(pos)


def results_note(query: str, found: int, filtered: int = 0, secs: float = 0.0) -> str:
    """'18 results for "x" - 3 long mixes skipped (2.1s)' in one honest line."""
    if not query:
        return "Type what you feel like hearing - a song, an artist, or a mood."
    if not found:
        return (f'Nothing found for "{query}" - try an artist name, or a '
                f"broader mood like \"70s soul\".")
    note = f'{fmt_count(found, "result")} for "{query}"'
    if filtered:
        note += " - " + fmt_count(filtered, "long mix", "long mixes") + " skipped"
    if secs:
        note += f" ({secs:.1f}s)"
    return note


def empty_state(view: str, has_data: bool = False) -> str:
    """Friendly first-run copy for each list, so no screen is ever just blank."""
    if has_data:
        return ""
    return {
        "search": ("Nothing searched yet. Search a song, an artist, an album, or a "
                   "mood - results come from YouTube Music, so nearly anything "
                   "exists here."),
        "loved": ("You have not loved anything yet. Press the heart on a song "
                  "(or the L key) and it shows up here and teaches the DJ."),
        "recent": ("Nothing played yet. Pick a mood on the Home tab, or search for "
                   "a song and press play."),
        "library": ("Your liked songs, what you have played, and the artists this "
                    "app has learned you like all live here."),
        "queue": ("Nothing queued. Press play on any result, or let the DJ plan a "
                  "set from the Home tab."),
    }.get(view, "")


def home_subtitle(n_liked: int, n_recent: int, engine: str | None = None) -> str:
    """One line under the greeting: what this app knows about you, in words."""
    bits = []
    if n_recent:
        bits.append(f"{fmt_count(n_recent, 'track')} played so far")
    if n_liked:
        bits.append(f"{fmt_count(n_liked, 'loved song')}")
    if not bits:
        return ("Pick a mood below, or search for anything. It plays locally through "
                "mpv, so no Premium account is involved anywhere.")
    return " - ".join(bits) + ". It plays locally through mpv, no Premium needed."


def artist_rows(artists: dict, width_px: int, char_px: int, limit: int = 12) -> list[dict]:
    """Taste profile as rows (name, plain-language verdict), for the Library tab."""
    out = []
    for name, score in sorted((artists or {}).items(), key=lambda kv: -kv[1])[:limit]:
        if not name:
            continue
        s = float(score)
        if s >= 6:
            verdict, colour = "you love this", SUCCESS
        elif s >= 1.5:
            verdict, colour = "you like this", SUCCESS
        elif s <= -3:
            verdict, colour = "you skip this", ERROR
        elif s <= -0.5:
            verdict, colour = "often skipped", MUTED
        else:
            verdict, colour = "a few plays", MUTED
        out.append({"name": ellipsize(name, max(8, int(width_px * 0.55) // max(1, char_px))),
                    "verdict": verdict, "colour": colour, "score": round(s, 1)})
    return out


def settings_note(backend: str, headless: bool) -> str:
    """Explains the one thing people ask: where is the sound coming from."""
    if headless or backend == "none":
        return "No audio: this instance only plans and queues. Remove --headless to play."
    if backend == "spotube":
        return ("Handing each track to Spotube over MPRIS (playerctl). Spotube must be "
                "running; it cannot receive a queue, only the next track.")
    return "Playing through mpv on this machine. Volume, seek and skip are all local."

# ----------------------------------------------------------- row context menu
# A right-click menu is the only place a list row can offer anything beyond
# "play", and its wording has to be honest about state: "Add to queue" beside a
# track that is already queued, or "Play now" beside the one that is playing, are
# both lies a listener can see through. Built here (no Tk) so the wording is
# testable and the GUI only has to map action -> verb.
ROW_ACTIONS = ("play", "queue", "love", "not_interested", "radio", "copy")


def row_menu(track: dict, playing: bool = False, queued: bool = False,
             played: bool = False, loved: bool = False) -> list[dict]:
    """Menu items for one row: {action, label, enabled, separator_after}."""
    track = track or {}
    items = [
        {"action": "play",
         "label": "Play now" if not playing else "Play now  -  this is what is playing",
         "enabled": not playing},
        {"action": "queue",
         "label": "Add to queue" if not queued else "Add to queue  -  already queued",
         "enabled": not queued},
        {"action": "love",
         "label": "Love this" if not loved else "Love this  -  already loved",
         "enabled": not loved},
        {"action": "not_interested", "label": "Not interested", "enabled": True},
        {"action": "radio", "label": "Start a station from this song",
         "enabled": True, "separator_before": True},
        {"action": "copy",
         "label": "Copy link to song" if track.get("url") else "Copy link  -  no link to copy",
         "enabled": bool(track.get("url"))},
    ]
    if played:
        # the row is greyed by the caller; the menu says why instead of the
        # list quietly dropping something the user can still see
        items.insert(0, {"action": "played", "label": "Already played  -  click it anyway",
                         "enabled": False})
    return items


def seek_by(pos, dur, delta: float) -> tuple[float, float]:
    """
    (fraction, seconds) after nudging `pos` by `delta` seconds. Clamped inside
    the track, so ',' at 0:02 walks the bar to the start instead of wrapping it
    to the end - the bug that makes a keyboard seek feel broken.
    """
    try:
        dur = float(dur or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        return 0.0, 0.0
    try:
        pos = float(pos or 0) + float(delta)
    except (TypeError, ValueError):
        pos = 0.0
    pos = min(max(0.0, pos), dur)
    return pos / dur, pos


def wrap_lines(text: str, chars: int, lines: int = 3) -> str:
    """
    Fit `text` into `lines` lines of `chars` characters, for a label that wraps
    but cannot cap how many lines it uses. Ellipsizing the whole string first is
    what produced "…(Official Audi…" on a panel with room for two more lines: the
    cut has to happen at the last line the widget can actually show.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    chars = max(4, int(chars or 0))
    lines = max(1, int(lines or 1))
    if len(text) <= chars:
        return text
    room = chars * lines
    if len(text) <= room:
        # let the widget wrap it naturally; only join with explicit breaks past
        # the first line so the shape is identical either way
        out, rest = [], text
        for _ in range(lines):
            cut = rest[:chars]
            if " " in cut and len(rest) > chars:
                cut = rest[:rest.rfind(" ", 0, chars)] or cut
            out.append(cut.strip())
            rest = rest[len(cut):].strip()
            if not rest:
                break
        return "\n".join(out)
    keep = text[: room - 1]
    if " " in keep[chars * (lines - 1):]:
        keep = keep[:keep.rfind(" ")]
    return keep.rstrip(" -,;") + "\u2026"
