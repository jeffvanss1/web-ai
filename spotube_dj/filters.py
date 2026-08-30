"""
filters.py - decide what a YouTube result actually *is* before it goes near the
player.

Why this file exists: a search for "ip man" on plain YouTube returns Donnie Yen
fight scenes, movie reactions and "full movie in Hindi" uploads - and every one
of them is 20-100 minutes long, has a verified channel, and no live flag, so the
old keyword list in providers.py waved them through and the DJ happily queued a
karate scene as if it were a song. Searching YouTube Music instead (see
providers.YTM_SEARCH) removes most of that class by construction; this module
catches the rest, in one place, as a pure function so it can be tested with the
real search output in tests/data.

The rules use YouTube's own structured fields when they are present, because
those are facts and titles are only hints:

    is_live / live_status / concurrent_view_count   -> a broadcast, not a track
    was_live                                        -> recorded broadcast
    duration is None                                -> the shape of a 24/7 stream
    description                                     -> tickets, merch, tour dates
    channel_is_verified                             -> weak "probably official"

Anything here that *demotes* rather than drops does so with a score, because a song
with the word "Live" in its title ("Live Forever") is still the recording you asked
for. A title that says it *is* a performance is not: "(Live)", "(Live at Reading)",
"(MTV Unplugged)", "- Live 1991" are refused outright, where they used to be demoted
by 0.8 and played anyway - which is what "theres LIVE music like performace like wtf?
is this not been filter?" was about. A 40-minute concert film was never a track.
"""

from __future__ import annotations

import re

TRACK = "track"
LONGFORM = "longform"          # a mix/set/radio: last resort, never a queue
EVENT = "event"               # concert film, festival stream, ticketed show
SPEECH = "speech"             # interview, podcast, reaction, commentary, lesson
NOTAUDIO = "notaudio"         # movie clip, gameplay, ambience, soundboard
UNPLAYABLE = "unplayable"     # live right now, private, upcoming premiere

# Never listenable, whatever else is true about it.
_HARD = (
    (UNPLAYABLE, r"\b(?:24/7|live\s+stream|streaming\s+now|premiere[sd]?\s+(?:at|in)\b|"
                 r"\bupcoming\b|\bcountdown\s+to\b)\b"),
    (SPEECH, r"\b(?:podcast|episode\s*\d*\s*\||\bep\.?\s*\d+\b|interview|press\s+conference|"
              r"reaction|reacts?\s+to|commentary|review|roundtable|q\s*&\s*a|qa\b|"
              r"documentary|documental|behind\s+the\s+scenes|full\s+episode|"
              r"tutorial|how\s+to\b|\blesson\b|masterclass|covered\s+by|"
              r"lecture|sermon|debate|stand-?up|comedy\s+special|talk\s+about|"
              r"commentating|walkthrough\s+of)"),
    (NOTAUDIO, r"\b(?:movie\s?clip|film\s+scene|scene\s*\(\d+\s*/\s*\d+\)|full\s+movie|"
                r"movie\s+in\s+|pel[cí]cula\s+completa|pelicula\s+completa|film\s+complet|"
                r"trailer|teaser|clip\s*\d|short\s+film|\bgameplay\b|walkthrough|"
                r"playthrough|let'?s\s+play|speedrun|\bmod\b\s+\d|soundboard|"
                r"white\s+noise|rain\s+sounds|asmr|meditation\s+music\s+for\s+sleep|"
                r"ringtone|notification|album\s+art\s*(?:loop|only)?|loop\s*\d*\s*hours?)"),
    (EVENT, r"\b(?:full\s+concert|concert\s+film|"
             r"\bsetlist\b|\btour\s+dates\b|\bpresale\b|tickets?\s+(?:on\s+sale|available|here)|"
             r"merch(?:drop|andise)?\b|doors\s+(?:open|at)|vip\s+package|meet\s*[-&]\s*greet|"
             r"support\s+acts?|festival\s+\d+"
             r"|(?:arena|stadium)\b[^|]{0,18}?(?:live|concert|tour|20\d\d|full\s+show))"),
    # "… ver." at the end of a title is somebody else's take on it (the JP row
    # that used to come back for a Philly Soul search was "松田聖子 – Philly Soul ver.")
    (NOTAUDIO, r"(?:-|\u2013|\u2014|\|)\s*[^|]{0,30}?\b(?:ver|version|cover)\.?\s*$"),
    # things that are a *different recording of a song you already have*
    (NOTAUDIO, r"\b(?:karaoke|originally\s+performed|type\s+beat|instru?mental\s+cover|"
                r"cover\s+version|\b(?:acoustic|trip\s*hop|metal|piano|guitar|flute|music\s+box)\s+cover|"
                r"slowed(\s*\+\s*reverb)?|\bslowed\b|sped\s+up|nightcore|speed\s*up\s+cover|"
                r"\bw+\s*verse\b|8d\s+audio|reverb\s+only|slowed\s+and\s+reverb)"),
    # Not a recording of music at all: an AI render of a song that was never
    # recorded, or a pack of loops sold to producers. These flooded a "philly
    # soul" search once; the rules lived in providers.yt_search and had to come
    # back here when that function started asking this module instead.
    (NOTAUDIO, r"\b(?:ai\s*(?:generated|cover|version|remake|remix|song)|"
                r"generated\s+by\s+ai|\bsuno\b|\budio\b|sunodelic|"
                r"samples?\s*\||royalty[-\s]free|stock\s+music|sample\s+pack|"
                r"loopkit|one[-\s]shot|\bmidi\b|stem\s*pack)"),
)

# A set, a radio, an hour of something. Kept for "nothing else matched", never
# mixed in with three-minute songs.
# Split in two on purpose: "dj set" and "2 hours of" are unambiguous, but
# "mix" and "radio" are also song titles (Queen - Radio Ga Ga, every "Megamix"
# edit), so those only mean longform when the length agrees.
_LONGFORM_HARD = re.compile(
    r"\b(?:dj\s+set|live\s+set|set\s*\d+\b|ambience|soundscape|full\s+album|"
    r"best\s+of|compilation|lofi\s+radio|radio\s+(?:station|fm|live|24)|"
    r"(?:\d+|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+"
    r"(?:hour|minute|min|sec|second)s?\s+(?:of|long)|"
    r"(?:hours?|minutes?|mins?)\s+of|\d+\s*-\s*hour)", re.I)

# Content sold as music but which is not a recording of anything being
# performed: weather, sine waves, hypnosis. These arrive from YouTube Music as
# perfectly ordinary `Song` rows with no length at all, so no duration rule can
# reach them, and a queue of rain noise is precisely what the user complained
# about. HARD = a phrase no real song title uses, refused whatever the length.
# SOFT = wording that also shows up in genuine song titles, so it is only
# trusted when there is no length to argue with (or the length is already long).
_AMBIENCE_HARD = re.compile(
    r"\b(?:nature\s+sounds?|rain\s+sound(?:s)?|thunder\s+sound(?:s)?|"
    r"ocean\s+sound(?:s)?|forest\s+sound(?:s)?|waves?\s+sound(?:s)?|"
    r"white\s+noise|brown\s+noise|pink\s+noise|asmr|binaural\s+beats?|"
    r"solfeggio|\d{3}\s*hz|delta\s+waves?|theta\s+waves?|alpha\s+waves?|"
    r"beta\s+waves?|insomnia|guided\s+meditation|meditation\s+(?:music|sounds?|track|guide)|"
    r"zen\s+meditation|spa\s+music|sleep\s+(?:music|sounds?|therapy)|"
    r"music\s+for\s+(?:sleep|babies|unborn|infants)|sounds?\s+for\s+sleep|"
    r"subconscious|affirmations?\b|breathwork|mindfulness)", re.I)

_AMBIENCE_SOFT = re.compile(
    r"\b(?:relaxing\s+music|calming\s+music|soothing\s+music|healing\s+music|"
    r"study\s+music|focus\s+music|sleep\s+music|deep\s+sleep|bedtime\s+music|"
    r"fall\s+asleep|soothing\s+you\s+to|lullaby|meditation\s+music|ambient\s+music|"
    r"soundscape|relax\s*&\s*(?:sleep|study)|music\s+to\s+(?:sleep|relax))\b", re.I)

# A title that states its own runtime is a set, whatever the catalog says. Only
# trusted when the length does not contradict it: a YTM `Song` row carries no
# duration at all, and "Best Lo-Fi Hip Hop Music 1 Hour" is exactly such a row.
# A real three-minute song called "7 Minutes in Heaven" has a duration, so it
# keeps its name and its place in the queue.
_DURATION_CLAIM = re.compile(
    r"\b(?:\d+|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve|half)"
    r"\s*-?\s*(?:hour|hours|hr|hrs|minute|minutes|min|mins|second|seconds|sec)\b",
    re.I)


_LONGFORM_SOFT = re.compile(r"\b(?:mix|mixtape|radio|playlist|megamix|medley)\b", re.I)

_ACTIVITY = re.compile(
    r"\bfor\s+(?:studying|study|relaxing|relaxation|sleep(?:ing)?|working|work|driving|"
    r"reading|concentration|focus|meditation|yoga|cleaning|cooking|eating|walking|"
    r"programming|reading)\b|\b(?:vibes?|music)\s+for\b", re.I)

# An essay about a song, not the song. These words are also song titles
# ("Breakdown", "Analysis"), so they count only next to a qualifier ("lyrics
# meaning explained") or when the title is formatted like a heading.
_ESAY = re.compile(r"(?:song|track|album|lyric|lyrics|meaning|chord|guitar|piano)"
                   r"[^|]{0,26}?\b(?:review|analysis|breakdown|explained|explanation|"
                   r"summary|recap|ranking|reaction|meaning)\b"
                   r"|\b(?:review|analysis|breakdown|explained|summary|recap|reaction)"
                   r"\s*[:|\u2022]\s", re.I)

_EVENT_DESC = re.compile(
    r"(?:ticket|on\s+sale|presale|merch|tour\s+dates|venue|doors\s+open|vip\s+"
    r"package|meet\s*and\s*greet|supporting\s+acts|age\s*(?:18|21)\+|seated|"
    r"general\s+admission)", re.I)

_OFFICIAL_TITLE = re.compile(r"\b(?:official\s+(?:video|audio|visualizer|"
                             r"lyric\s*video|mv|hd)|official\s*\|\s*vevo)\b", re.I)

_TOP_CHANNEL = re.compile(r"\s[-–]\s*topic$", re.I)

# Film/broadcast vocabulary. Each of these is also a song word ("Fight",
# "Battle"), so it only means "not a song" in a title formatted like a clip
# listing - a pipe, a year in brackets, a scene number, the words movie/film.
# ("Ip Man 4: The Finale | Wan Fight", "DONNIE YEN vs FAN SIU-WONG | IP MAN
# (2008)") - which is exactly what plain YouTube search hands back for a
# non-music query.
# "Live at Reading 1992" is a take of a song; "Live at Reading - Full Concert"
# is the whole show. The venue phrase only means an event next to a word that
# says you are looking at an entire broadcast.
_LIVE_VENUE = re.compile(r"\blive\s+(?:at|from|in|for)\b", re.I)
_SHOW_WORDS = re.compile(r"\b(?:full|complete|entire)\b|\bconcert\b|\bshow\b|"
                         r"\bset\b|\bgig\b|\bfestival\b|\btour\b|\bstream\b|"
                         r"\bbroadcast\b|\bsession\b|\bunplugged\b|\barena\b|"
                         r"\bstadium\b|\barte\b", re.I)

# A live take of the right song is still not the recording a mood mix is for, and on
# YouTube it is the single most common thing filed under a studio track's name - the
# complaint was "theres LIVE music like performance like wtf? is this not been
# filter?", and the honest answer was: demoted by 0.8, so it came back anyway. Two
# shapes are now refused outright: a phrase that can only mean a performance, and a
# bracketed tag that says so ("Amazing (Live)", "Song [Live at the BBC]"). A bare
# "Live" inside a title stays a demotion, because "Live Forever" is a song and
# dropping Oasis would be the other kind of wrong.
_LIVE_TAKE = re.compile(
    r"\b(?:live\s+(?:performance|performing|concert|version|take|recording|set|show|"
    r"studio\s+session|at|from|in|on|for)\b|(?:recorded|captured|filmed|performed)\s+live|"
    r"mtv\s+unplugged|\bunplugged\b|in\s+concert|full\s+(?:concert|show|set)|"
    r"concert\s+(?:film|footage)|live\s+\d{4})\b", re.I)
_LIVE_TAG = re.compile(
    r"[\(\[\{][^)\]}]{0,60}?\b(?:live|unplugged|concert|performance|performing|tour|gig|"
    r"arena|stadium|festival|broadcast)[^)\]]{0,60}?[\)\]\}]", re.I)

_CLIPISH = re.compile(r"\b(?:fight(?:ing)?|battle|duel|scene|clip|trailer|"
                      r"bloopers|movie|film|wushu|kung\s*fu|gun\s*fu|vs\.?|"
                      r"versus|choreograph\w*|extended)\b", re.I)

_CLIP_SHAPE = re.compile(r"\||\((?:19|20)\d\d\)|\(\d+\s*/\s*\d+\)|\bmovie\b|\bfilm\b|"
                         r"\bscene\s*\d|clip\s*\d", re.I)

_MUSIC_CHANNEL = re.compile(r"\b(?:topic|vevo|records?|recording|music|entertainment|"
                            r"album|singles?)\b", re.I)

# `Artist - Title`, `Title | Artist`, `Artist – Title`
_SHAPE = re.compile(r"\s[-–—]\s|\s[|\u2022]\s")


def _secs(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_duration(text: str) -> int:
    """`4:12`, `1:02:03` or `5,31` from a YouTube subtitle -> seconds (0 if none)."""
    s = (text or "").strip()
    m = re.search(r"(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\Z", s)
    if not m:
        return 0
    if m.group(3):                       # h:mm:ss
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_views(text: str) -> int:
    """`62M views`, `115K views`, `3B plays` -> a count; 0 when absent."""
    m = re.search(r"([\d.,]+)\s*([KMB]?)\s*(?:views?|plays?)", text or "", re.I)
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(n * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()])


def decide(entry: dict) -> dict:
    """
    -> {"kind": str, "reasons": [str], "score": float}

    `score` only ever orders the survivors: YouTube's own ranking is a mess for
    "who actually owns this recording", so official audio is pushed up and
    low-effort re-uploads down. Nothing is dropped for a low score.
    """
    entry = entry or {}
    title = str(entry.get("title") or "")
    low = title.lower()
    desc = str(entry.get("description") or "")
    dur = _secs(entry.get("duration"))
    ch0 = str(entry.get("channel") or entry.get("uploader") or "")
    reasons: list[str] = []
    kind = TRACK
    score = 0.0

    # ---- facts from the API first -------------------------------------------
    status = str(entry.get("live_status") or "")
    if entry.get("is_live") or status in ("is_live", "is_upcoming"):
        kind, reasons = UNPLAYABLE, ["live right now"]
    elif status == "was_live" and (not dur or dur > 900):
        kind, reasons = EVENT, ["a broadcast that already happened"]
    elif entry.get("concurrent_view_count") and not dur:
        kind, reasons = UNPLAYABLE, ["live viewer count, no length: a stream"]
    elif str(entry.get("availability") or "") in ("private", "unlisted_region_blocked",
                                                 "premium_only"):
        kind, reasons = UNPLAYABLE, [f"not playable ({entry.get('availability')})"]

    if kind == TRACK:
        for name, pat in _HARD:
            if re.search(pat, low, re.I):
                kind, reasons = name, [f"{name.lower()} in title"]
                break

    if kind == TRACK and _LIVE_VENUE.search(low) and _SHOW_WORDS.search(low):
        kind, reasons = EVENT, ["a whole show, not a song"]

    if kind == TRACK and (_LIVE_TAKE.search(title) or _LIVE_TAG.search(title)):
        # `title` only: a channel called "Live Nation" or a description that says
        # "filmed live in 2019" must not be able to refuse every song it hosts
        kind, reasons = EVENT, ["a live take, not the recording"]

    if kind == TRACK and _ESAY.search(title):
        kind, reasons = SPEECH, ["written about a song, not the song"]

    if kind == TRACK and desc and _EVENT_DESC.search(desc[:1500]):
        kind, reasons = EVENT, ["event copy in the description"]

    # ---- length as evidence --------------------------------------------------
    if kind == TRACK:
        if dur and dur < 20:
            kind, reasons = NOTAUDIO, [f"{dur}s is not a song"]
        elif dur > 5400:
            kind, reasons = UNPLAYABLE, [f"{dur // 60} min: not a listenable unit"]
        elif dur > 960:
            # past about 16 minutes this is not a track any more whatever the
            # title claims: a set, a lecture, a full album rip, a 24/7 loop that
            # happens to have a start. "Thick as a Brick" fans lose; a live
            # broadcast reaching the speaker is the worse failure.
            kind, reasons = LONGFORM, [f"{dur // 60} min: not a single track"]
        elif not dur and not entry.get("official"):
            # yt-dlp reports a length for every ordinary upload. A missing one is
            # how a 24/7 broadcast or an unstarted premiere looks - the case a
            # keyword list can never catch, because those titles say "jazz".
            kind, reasons = LONGFORM, ["no length reported: broadcast or premiere"]
        elif _LONGFORM_HARD.search(low) \
                or (dur > 1500 and _LONGFORM_SOFT.search(low)) \
                or (_ACTIVITY.search(low) and dur > 900):
            kind, reasons = LONGFORM, ["mix/set/radio"]
        elif "|" in title and dur > 900:
            kind, reasons = LONGFORM, ["described like a set"]
        elif not dur and re.search(r"\b(?:vibes?\s+for|music\s+for|hours)\b", low):
            kind, reasons = LONGFORM, ["no length + ambience title"]
        elif _DURATION_CLAIM.search(low) and not (20 < dur <= 600):
            kind, reasons = LONGFORM, ["says how long it is: a set"]
        elif _AMBIENCE_HARD.search(low) or _AMBIENCE_HARD.search(ch0):
            kind, reasons = LONGFORM, ["ambience, not a recording"]
        elif (not dur or dur > 600) and _AMBIENCE_SOFT.search(low):
            # the *title* only: a lofi channel called "Study Music & Sounds"
            # uploads three-minute songs too, and every one of them would go if
            # the channel name were enough evidence
            kind, reasons = LONGFORM, ["sounds like a sleep aid"]

    # ---- demotions that are still music -------------------------------------
    if kind == TRACK:
        if re.search(r"\blive\b", low):
            score -= 0.8
            reasons.append("live take")
        if re.search(r"\b(?:cover|ver\.?)\b", low):
            score -= 1.5
            reasons.append("someone else's cover")
        if "remaster" in low:
            score += 0.3
        ch = str(entry.get("channel") or entry.get("uploader") or "")
        if _TOP_CHANNEL.search(ch):
            score += 3.0                        # the artist's own audio channel
            reasons.append("artist Topic channel")
        if entry.get("official"):
            score += 2.5
            reasons.append("YTM song row")
        if _OFFICIAL_TITLE.search(title):
            score += 1.6
        if entry.get("channel_is_verified"):
            score += 0.9
        views = _secs(entry.get("view_count"))
        if views:
            if views < 500:
                score -= 1.2
                reasons.append("almost no views")
            elif views > 5_000_000_000:
                score -= 0.6                    # 5B views on a B-side = a re-upload farm
        if 90 <= dur <= 480:
            score += 0.5                        # the shape of a pop song
        elif dur and (dur < 60 or dur > 900):
            score -= 0.7

    if kind == TRACK and _CLIPISH.search(title) and _CLIP_SHAPE.search(title):
        kind, reasons = NOTAUDIO, ["a clip from a film"]

    # ---- provenance ----------------------------------------------------------
    # On plain YouTube search the surface is *video*: a fight scene from a movie
    # and a band's official audio sit in the same result list, and no keyword
    # list fixes that reliably. So demand something that says "music upload" -
    # the artist's own channel, a YTM Song row, or `Artist - Title` at a
    # song-length runtime - and demote everything else instead of trusting it.
    if kind == TRACK and not entry.get("official"):
        own_channel = bool(_MUSIC_CHANNEL.search(
            str(entry.get("channel") or entry.get("uploader") or "")))
        shaped = bool(_SHAPE.search(title)) or 60 <= (dur or 0) <= 1200
        if not (own_channel or shaped):
            score -= 2.0
            reasons.append("not shaped like a track")

    return {"kind": kind, "reasons": reasons, "score": round(score, 2),
            "entry": entry, "title": title}


def summarise(verdicts: list[dict]) -> str:
    """`kept 8, dropped 2 live right now, 1 movie clip, 1 karaoke` - for the log."""
    from collections import Counter
    c = Counter()
    for v in verdicts or []:
        if v.get("kind") in (TRACK, None):
            continue
        for why in (v.get("reasons") or ["?"]):
            c[why] += 1
    if not c:
        return ""
    parts = ", ".join(f"{n} {why}" for why, n in c.most_common(4))
    return parts
