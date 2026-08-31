"""
tests/test_filters_cache.py - what counts as a song, and what makes it start now.

Two features the user asked for in plain words: "the content that being play not
even music, and like live event or anythings" (filters.py) and "make cache so
theres no waiting smooth playing in the queue" (audiocache.py). Both are pure
enough to test without a network, a player or a display; the one thing that
needs a seam is yt-dlp, so `fetch()` is tested against a stubbed subprocess that
writes the file the way yt-dlp would.

The filter cases are not made up: the live radio rows, the Ip Man fight scene,
the AI-generated "philly soul" and the 45-minute lofi room all came out of real
searches (tests/data/flat-sample.jsonl), and each one used to reach the speaker.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "spotube_dj"
for p in (str(PKG), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import audiocache                                     # noqa: E402
import bins                                           # noqa: E402
import covers                                         # noqa: E402
import filters                                        # noqa: E402
import thumbs                                         # noqa: E402

DATA = ROOT / "tests" / "data"


class FilterTests(unittest.TestCase):
    def test_live_broadcast_is_never_played(self):
        live = {"title": "Jazz Radio", "is_live": True, "live_status": "is_live",
                "duration": None, "concurrent_view_count": 1200}
        v = filters.decide(live)
        self.assertEqual(v["kind"], filters.UNPLAYABLE)
        self.assertIn("live right now", v["reasons"])

    def test_live_radio_with_no_telltale_title_is_still_caught(self):
        # the one that fooled every keyword list: no "24/7", no "live", just a
        # viewer count and no end.
        row = {"title": "Refreshing Spring Jazz Music To Stress Relief",
               "duration": None, "concurrent_view_count": 841, "is_live": None,
               "live_status": None}
        self.assertEqual(filters.decide(row)["kind"], filters.UNPLAYABLE)

    def test_a_broadcast_that_already_happened_is_an_event(self):
        row = {"title": "Samara Joy - ARTE Concert", "live_status": "was_live",
               "duration": 5400}
        self.assertEqual(filters.decide(row)["kind"], filters.EVENT)
        # but a short clip from the same broadcast is a listenable thing
        short = dict(row, duration=200)
        self.assertEqual(filters.decide(short)["kind"], filters.TRACK)

    def test_movie_clip_shape_is_refused(self):
        for title in ("Ip Man 3 (2016) - Elevator Fight Scene (6/10) | Movieclips",
                      "DONNIE YEN vs FAN SIU-WONG | IP MAN (2008)",
                      "Ip Man 4: The Finale | Wan Fight"):
            v = filters.decide({"title": title, "duration": 214,
                                "channel_is_verified": True})
            self.assertEqual(v["kind"], filters.NOTAUDIO, title)

    def test_a_song_called_alive_is_not_an_event(self):
        # regression guard: the rule that used to catch "A Live 2024" also ate
        # Pearl Jam - "Alive" is a song title, and a venue word only means
        # something with company.
        self.assertEqual(filters.decide({"title": "Pearl Jam - Alive",
                                         "duration": 340})["kind"], filters.TRACK)
        # ...and a whole broadcast of it is still refused
        self.assertEqual(filters.decide({"title": "Pearl Jam - Alive "
                                                   "(Live at Madison Square Garden, "
                                                   "Full Show)", "duration": 3400,
                                          "live_status": "was_live"})["kind"],
                         filters.EVENT)

    def test_a_song_called_radio_or_breakdown_survives(self):
        for title in ("Queen - Radio Ga Ga", "Tom Petty - Breakdown",
                      "R.E.M. - Analytical", "Kraftwerk - Radioactivity"):
            self.assertEqual(filters.decide({"title": title, "duration": 240})["kind"],
                             filters.TRACK, title)

    def test_long_uploads_are_last_resort_not_a_queue(self):
        for row in ({"title": "Best of 2019 mix", "duration": 3000},
                    {"title": "Quiet Morning", "duration": 1860},
                    {"title": "Soft Lofi Room - Chill Vibes for Peaceful Study",
                     "duration": 2700},
                    {"title": "Two hours of neo soul", "duration": 120}):
            self.assertEqual(filters.decide(row)["kind"], filters.LONGFORM,
                             row["title"])

    def test_a_genre_channel_does_not_condemn_its_own_uploads(self):
        # "Study Music" in a channel name describes the shelf, not the file
        v = filters.decide({"title": "Deep Focus LoFi",
                            "channel": "LO-FI BEATS, Study Music & Sounds, & Focus Music",
                            "duration": 0, "official": True, "endpoint": "ytmusic"})
        self.assertEqual(v["kind"], filters.TRACK)

    def test_a_title_that_states_its_runtime_is_a_set(self):
        # from the live complaint: this row came back as a YTM `Song` with no
        # length at all, so only the title admits it is an hour of music
        hour = ("Chill LoFi Beat for Relaxation & Study | Best Lo-Fi Hip Hop "
                "Music 1 Hour (Chill Vibes)")
        self.assertEqual(filters.decide({"title": hour, "duration": 0,
                                         "official": True})["kind"], filters.LONGFORM)
        # named lengths on rows that do have a song-shaped runtime stay songs
        for title, dur in [("7 Minutes in Heaven", 191), ("One More Hour", 210),
                           ("3 Minute Warning", 178)]:
            v = filters.decide({"title": title, "duration": dur, "artist": "X",
                                "official": True})
            self.assertEqual(v["kind"], filters.TRACK, title)
        # and a five-hour thing is refused on the length alone, title or not
        self.assertEqual(filters.decide({"title": "Lofi", "duration": 3600,
                                         "artist": "X"})["kind"], filters.LONGFORM)

    def test_ambience_is_refused_even_though_music_catalogs_call_it_a_song(self):
        # YouTube Music hands these back as `Song` rows with no length, so no
        # duration rule can see them and the old build queued them happily.
        for title, ch in [("Nature Sounds - 10 Hours of Rain and Thunder", "Sleep Zone"),
                          ("Zen Meditation Music, Nature Sounds, Relaxing Music", "Calm Radio"),
                          ("432 Hz Healing Music - Clear Subconscious Blockages", "Silence Mind"),
                          ("Soothing You to Sleep: A Perfect Way to Fall Asleep",
                           "Rising Higher Meditation")]:
            v = filters.decide({"title": title, "channel": ch, "artist": ch,
                                "duration": 0, "official": True, "endpoint": "ytmusic"})
            self.assertEqual(v["kind"], filters.LONGFORM, title)

    def test_a_real_song_may_still_sing_about_sleep(self):
        for title, dur in [("Sleep Now for the Night", 210), ("Lullaby", 198),
                           ("Better Sleep", 180), ("I Will Sleep When I'm Dead", 205)]:
            v = filters.decide({"title": title, "artist": "Some Band",
                                "duration": dur, "official": True})
            self.assertEqual(v["kind"], filters.TRACK, title)

    def test_a_set_says_so_even_when_the_catalog_calls_it_a_song(self):
        # YouTube Music returns these as ordinary `Song` rows with no length at
        # all, so duration can help: the title has to say what it is
        for title in ("40 Minutes of Brett\u2019s Relaxing Lofi Beat",
                      "One Hour of Focus Music",
                      "Two hours of neo soul"):
            v = filters.decide({"title": title, "official": True, "duration": 0})
            self.assertEqual(v["kind"], filters.LONGFORM, title)

    def test_a_mix_under_twenty_minutes_is_just_a_track(self):
        # "Quiet Mix" at 15 minutes is a radio edit of something, and a
        # continuous mix under that is still listenable in one sitting
        self.assertEqual(filters.decide({"title": "Quiet Mix", "duration": 900})["kind"],
                         filters.TRACK)

    def test_ai_and_sample_packs_are_refused(self):
        for row in ({"title": "Leather Jacket (AI Generated 70s Philly Soul)",
                     "duration": 200},
                    {"title": "Philly Soul Samples | Smooth Grooves", "duration": 300},
                    {"title": "赤いスイートピー / 松田聖子 – Philly Soul ver.",
                     "duration": 240},
                    {"title": "Harold Melvin - Royalty Free Beat Pack", "duration": 200}):
            self.assertNotEqual(filters.decide(row)["kind"], filters.TRACK,
                                row["title"])

    def test_official_audio_outranks_a_bootleg(self):
        # a fan re-upload, not a live take: a live take is refused outright now, and
        # the ranking has to be judged on the rows that are actually allowed through
        bootleg = {"title": "Nirvana - Smells Like Teen Spirit (Audio Remaster)",
                   "duration": 286, "channel": "ScottishTeeVee"}
        official = {"title": "Smells Like Teen Spirit", "duration": 302,
                    "artist": "Nirvana", "channel": "Nirvana", "official": True}
        a, b = filters.decide(bootleg), filters.decide(official)
        self.assertEqual(a["kind"], filters.TRACK)
        self.assertGreater(b["score"], a["score"])

    def test_live_takes_are_dropped_and_a_song_called_live_is_not(self):
        """The policy, changed on a listener's complaint.

        These used to be demoted by 0.8, which is to say: they still got played, and
        "theres LIVE music like performance like wtf? is this not been filter?" was a
        fair question. A mood mix is for the recording, so a title that *says* it is a
        performance now refuses itself - while a song with the word in its name is
        still only judged on being a song.
        """
        for title in ("Nirvana - The Man Who Sold The World (Live at Reading 1992)",
                      "Kanye West - Amazing (Live Performance)",
                      "Nirvana - Smells Like Teen Spirit - Live 1991",
                      "Alice In Chains - Nutshell (MTV Unplugged)",
                      "Fleetwood Mac - The Chain [Live at the BBC]",
                      "Megadeth - Symphony of ... (Live)"):
            with self.subTest(title=title):
                v = filters.decide({"title": title, "duration": 331})
                self.assertEqual(v["kind"], filters.EVENT)
                self.assertIn("a live take, not the recording", v["reasons"])
        whole = filters.decide({"title": "Nirvana - Live at Reading 1992 (Full Show)",
                                "duration": 331})
        self.assertEqual(whole["kind"], filters.EVENT)
        # the word alone is not evidence: these are songs called that
        for title in ("Oasis - Live Forever", "Oasis - Live Forever (Remastered)",
                      "Paul McCartney - Live and Let Die",
                      "Eagles - Love Will Keep Us Alive"):
            with self.subTest(title=title):
                self.assertEqual(filters.decide({"title": title, "duration": 300})["kind"],
                                 filters.TRACK)
        # a demotion is still what a bare "Live" inside a title costs, and the log
        # says so: judged, not hidden
        v = filters.decide({"title": "Oasis - Live Forever", "duration": 300})
        self.assertIn("live take", v["reasons"])
        self.assertLess(v["score"], filters.decide(
            {"title": "Oasis - Some Other Song", "duration": 300})["score"])
        # "(Live Music Hall)" is a venue, and a title naming one is a performance:
        # the row that reads like a club date goes, the one that reads like a song
        # does not
        venue = filters.decide({"title": "Tom Petty - Breakdown (Live Music Hall)",
                                "duration": 300})
        self.assertEqual(venue["kind"], filters.EVENT)

    def test_unplayable_availability(self):
        v = filters.decide({"title": "Song", "duration": 200, "availability": "private"})
        self.assertEqual(v["kind"], filters.UNPLAYABLE)

    def test_verdict_carries_the_entry_for_the_caller(self):
        row = {"title": "Song", "duration": 200}
        v = filters.decide(row)
        self.assertIs(v["entry"], row)
        self.assertEqual(v["title"], "Song")


class FilterParsers(unittest.TestCase):
    def test_parse_duration(self):
        for text, secs in (("5:31", 331), ("1:02:03", 3723), ("0:45", 45),
                           ("115K views", 0), ("", 0), ("None", 0), ("3:5", 0)):
            self.assertEqual(filters.parse_duration(text), secs, text)

    def test_parse_views(self):
        for text, n in (("62M views", 62_000_000), ("115K views", 115_000),
                        ("3B plays", 3_000_000_000), ("1,234 views", 1234),
                        ("no numbers", 0), ("", 0)):
            self.assertEqual(filters.parse_views(text), n, text)

    def test_summarise_reports_what_was_dropped(self):
        verdicts = [filters.decide({"title": "Ip Man (2010) - Fight Scene (3/10) | Movieclips",
                                    "duration": 214}),
                    filters.decide({"title": "Jazz 24/7 live stream", "is_live": True}),
                    filters.decide({"title": "Jazz 24/7 live stream", "is_live": True})]
        text = filters.summarise(verdicts)
        self.assertIn("notaudio in title", text)
        self.assertIn("2 live right now", text)
        self.assertEqual(filters.summarise([filters.decide({"title": "Song",
                                                             "duration": 200})]), "")

    def test_real_harvest_is_censused(self):
        """
        The 66 entries in flat-sample.jsonl came out of four real searches. What
        matters is not the exact count - YouTube changes - but that the obvious
        junk is not classified as a song, so this asserts on the known cases.
        """
        rows = [json.loads(l) for l in (DATA / "flat-sample.jsonl").read_text().splitlines()
                if l.strip()]
        self.assertGreater(len(rows), 40)
        by_id = {r.get("id"): filters.decide(r) for r in rows}
        kinds = {r.get("title"): by_id[r.get("id")]["kind"] for r in rows}
        self.assertEqual(kinds["Ip Man 3 (2016) - Elevator Fight Scene (6/10) | Movieclips"],
                         filters.NOTAUDIO)
        self.assertEqual(kinds["Nirvana - Interview in New York City (1993) [FULL]"],
                         filters.SPEECH)
        concert = [k for k in kinds if k.startswith("Dave Brubeck")][0]
        self.assertEqual(kinds[concert], filters.EVENT)
        # ...and a real studio recording by the same artist survives
        # an unplugged take is refused, not ranked: the harvest was censused before
        # the live rule became a drop, and the row is the reason it did
        self.assertEqual(kinds["Nirvana - The Man Who Sold The World (MTV Unplugged)"],
                         filters.EVENT)


class BinDiscoveryTests(unittest.TestCase):
    def setUp(self):
        bins.reset()
        self.addCleanup(bins.reset)

    def test_env_override_wins_and_is_the_escape_hatch(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "ffmpeg"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            with mock.patch.dict(os.environ, {"SPOTUBE_DJ_FFMPEG": str(exe)}):
                self.assertEqual(bins.find("ffmpeg"), str(exe))

    def test_finds_a_binary_the_path_cannot_see(self):
        """
        The whole reason cover art vanished: a .desktop launch has a PATH without
        ~/.local/bin, so shutil.which says "no ffmpeg" and this app used to say
        "no artwork" with it.
        """
        with tempfile.TemporaryDirectory() as home:
            bindir = Path(home) / ".local" / "bin"
            bindir.mkdir(parents=True)
            exe = bindir / "ffmpeg"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            with mock.patch.dict(os.environ, {"HOME": home}), \
                    mock.patch.object(bins.shutil, "which", return_value=None):
                self.assertEqual(bins.find("ffmpeg"), str(exe))

    def test_missing_stays_missing(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HOME": home}), \
                    mock.patch.object(bins.shutil, "which", return_value=None):
                self.assertIsNone(bins.find("ffmpeg"))
                self.assertFalse(bins.have("ffmpeg"))

    def test_describe_lists_every_helper(self):
        with mock.patch.object(bins, "find", return_value="/usr/bin/mpv"):
            self.assertIn("mpv", bins.describe())
            self.assertIn("ffmpeg", bins.describe())


class ArtWithoutFfmpegTests(unittest.TestCase):
    JPEG = b"\xff\xd8\xff\xe0" + b"0" * 4000        # baseline-ish: SOF0 follows
    PROGRESSIVE = b"\xff\xd8\xff\xc2" + b"0" * 4000
    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 4000

    def test_art_is_on_even_with_no_ffmpeg(self):
        with mock.patch.object(bins, "find", return_value=None):
            self.assertTrue(thumbs.enabled())
            self.assertTrue(covers.enabled())

    def test_art_can_still_be_switched_off(self):
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_ART": "off"}):
            self.assertFalse(thumbs.enabled())
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_COVERS": "0"}):
            self.assertFalse(covers.enabled())

    def test_raw_bytes_are_stored_when_nothing_can_scale_them(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "vid" / "abc-caa-220.png"
            with mock.patch.object(thumbs, "_fetch", return_value=self.JPEG), \
                    mock.patch.object(bins, "find", return_value=None):
                got = thumbs._store("http://x/y.jpg", out, 220)
            self.assertTrue(got.endswith(".jpg"), got)
            self.assertEqual(Path(got).read_bytes(), self.JPEG)

    def test_png_is_stored_as_png(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "vid" / "abc-caa-220.png"
            with mock.patch.object(thumbs, "_fetch", return_value=self.PNG), \
                    mock.patch.object(bins, "find", return_value=None):
                got = thumbs._store("http://x/y.png", out, 220)
            self.assertTrue(got.endswith(".png"), got)

    def test_webp_is_not_stored_because_tk_cannot_read_it(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "vid" / "abc-caa-220.png"
            with mock.patch.object(thumbs, "_fetch",
                                   return_value=b"RIFFxxxxWEBP" + b"0" * 4000), \
                    mock.patch.object(bins, "find", return_value=None):
                self.assertIsNone(thumbs._store("http://x/y.webp", out, 220))

    def test_ffmpeg_is_located_through_bins(self):
        """`ffmpeg` on PATH is not enough any more; the call must use the found path."""
        src = (PKG / "thumbs.py").read_text()
        self.assertIn('bins.find("ffmpeg")', src)
        self.assertNotIn('["ffmpeg", ', src)

    def test_scaled_file_wins_over_the_cached_raw_one(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(thumbs, "cache_dir", return_value=Path(d)):
                (Path(d) / "vid1-caa-72.jpg").write_bytes(self.JPEG)
                self.assertEqual(thumbs.cached_path({"id": "vid1", "cover_url":
                                                      "http://x/a.jpg"}, "row"),
                                  str(Path(d) / "vid1-caa-72.jpg"))


class AudioCacheTests(unittest.TestCase):
    def patch(self, patcher):
        """Start a patcher and be sure it is undone, even if the test errors."""
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def setUp(self):
        import config
        self.dir = tempfile.mkdtemp(prefix="dj-cache-")
        # APP_DIR is resolved at import time, so setting the env var would not
        # move it: patch the attribute the module actually reads. Everything
        # below is per-test because the cache is module-global by design.
        self.patch(mock.patch.object(config, "APP_DIR", Path(self.dir)))
        self.patch(mock.patch.dict(os.environ, {"SPOTUBE_DJ_CACHE": ""}))
        # enabled() wants an yt-dlp to run; the tests below stub the subprocess,
        # so only the discovery has to succeed (through bins, which is what the
        # module now uses, since a menu launch cannot see /usr/local/bin)
        self.patch(mock.patch.object(audiocache.bins, "find",
                                     lambda name: "/usr/bin/yt-dlp" if name == "yt-dlp" else None))
        self.patch(mock.patch.object(audiocache, "_stats", dict(audiocache._stats)))
        self.patch(mock.patch.object(audiocache, "_queue", []))
        self.patch(mock.patch.object(audiocache, "_inflight", set()))

    def touch(self, vid, size=5000, suffix=".m4a"):
        import config
        d = Path(config.APP_DIR) / "audio"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{vid}{suffix}"
        p.write_bytes(b"0" * size)
        return p

    def test_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_CACHE": "off"}):
            self.assertFalse(audiocache.enabled())
            self.assertEqual(audiocache.prefetch([{"id": "a"}]), 0)
            self.assertEqual(audiocache.lookup({"id": "a"}), (None, None))

    def test_path_and_url_for_a_cached_track(self):
        p = self.touch("vid9")
        self.assertEqual(audiocache.path_for("vid9"), str(p))
        path, url = audiocache.lookup({"id": "vid9"})
        self.assertEqual(path, str(p))
        self.assertTrue(url.startswith("file://"))
        self.assertIsNone(audiocache.path_for("nope"))
        self.assertEqual(audiocache.lookup({"id": "nope"}), (None, None))

    def test_a_partial_file_is_never_played(self):
        self.touch("tiny", size=100)          # a truncated download
        self.assertIsNone(audiocache.path_for("tiny"))

    def test_prefetch_skips_what_is_already_stored_or_queued(self):
        self.touch("have")
        tracks = [{"id": "have"}, {"id": "one"}, {"id": "two"}, {"id": "two"}, {}]
        with mock.patch.object(audiocache, "_start") as start:
            n = audiocache.prefetch(tracks, ahead=2)
            self.assertEqual(n, 2, audiocache._queue)
            self.assertEqual(audiocache._queue[:2], ["one", "two"])
            # asking again for the same rows adds nothing
            self.assertEqual(audiocache.prefetch(tracks, ahead=2), 0)
            audiocache._queue.clear()
        start.assert_called_once_with()

    def test_fetch_renames_only_a_completed_file(self):
        """yt-dlp writes <out>.part (and sometimes a different container); the
        cache must never expose a half-written track."""
        class R:
            returncode = 0

        def fake_run(cmd, **kw):
            for i, arg in enumerate(cmd):
                if arg == "-o":
                    part = Path(cmd[i + 1])
                    part.with_suffix(".m4a").write_bytes(b"0" * 6000)   # ".part.m4a"
            return R()

        with mock.patch.object(audiocache.subprocess, "run", fake_run), \
                mock.patch.object(audiocache, "enabled", return_value=True):
            got = audiocache.fetch("vid7")
        self.assertTrue(got and Path(got).stat().st_size == 6000, got)
        self.assertEqual(audiocache.path_for("vid7"), got)

    def test_failed_fetch_leaves_nothing_behind(self):
        class R:
            returncode = 1

        with mock.patch.object(audiocache.subprocess, "run", return_value=R()), \
                mock.patch.object(audiocache, "enabled", return_value=True):
            self.assertIsNone(audiocache.fetch("bad"))
        self.assertIsNone(audiocache.path_for("bad"))

    def test_prune_drops_the_oldest_first(self):
        import config
        d = Path(config.APP_DIR) / "audio"
        d.mkdir(parents=True, exist_ok=True)
        old, new = d / "old.m4a", d / "new.m4a"
        old.write_bytes(b"0" * 4000)
        new.write_bytes(b"0" * 4000)
        os.utime(old, (0, 0))
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_CACHE_MB": "0.004"}):
            self.assertEqual(audiocache.prune(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_stats_and_brief_never_raise_on_a_missing_dir(self):
        s = audiocache.stats()
        self.assertIn("files", s)
        self.assertIsInstance(audiocache.brief(), tuple)


class CacheCapTests(unittest.TestCase):
    """The two ceilings a listener set in plain words: covers 500 MB, audio 2 GB."""

    def test_art_cache_defaults_to_500mb(self):
        with mock.patch.object(thumbs, "_DEFAULT_ART_CAP_MB", 500):
            self.assertEqual(thumbs.art_cap_bytes(), 500 * 1024 * 1024)

    def test_art_cache_obeys_the_env_override(self):
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_ART_CACHE_MB": "250"}):
            self.assertEqual(thumbs.art_cap_bytes(), 250 * 1024 * 1024)

    def test_audio_cache_defaults_to_2gb(self):
        with mock.patch.object(audiocache, "_DEFAULT_CAP_MB", 2048):
            self.assertEqual(audiocache.cap_bytes(), 2048 * 1024 * 1024)

    def test_audio_cache_obeys_the_env_override(self):
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_CACHE_MB": "100"}):
            self.assertEqual(audiocache.cap_bytes(), 100 * 1024 * 1024)

    def test_prune_tracks_bytes_not_file_count(self):
        # the old cap was 400 *files*; now it is a byte ceiling, so a fresh HD rung
        # is evicted only when the cache really is over budget - and oldest first
        d = Path(self._tmp()) / "thumbs"
        d.mkdir(parents=True, exist_ok=True)
        old, new = d / "a-yt-max-256.jpg", d / "b-yt-max-256.jpg"
        old.write_bytes(b"0" * 6000)
        new.write_bytes(b"0" * 6000)
        os.utime(old, (1, 1))
        with mock.patch.object(thumbs, "cache_dir", return_value=str(d)), \
                mock.patch.object(thumbs, "art_cap_bytes", return_value=6000):
            thumbs._prune(d)
        self.assertFalse(old.exists(), "oldest evicted first")
        self.assertTrue(new.exists(), "kept while under the byte cap")

    def _tmp(self):
        d = tempfile.mkdtemp(prefix="dj-caps-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


if __name__ == "__main__":
    unittest.main()
