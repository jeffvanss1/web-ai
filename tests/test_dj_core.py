"""
Unit tests for the pieces that don't need the network: the offline brain,
the taste model, dedupe/interleave, and m3u export.

Run:  python3 -m unittest discover -s tests -v      (from the repo root)
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap)

PKG_DIR = tests.PKG

import brain
import config
import filters
import providers as prov
import taste
import dj as dj_mod
from dj import DJ, Queue, _interleave, build_queue


def _load_cli():
    """Load spotube_dj/__main__.py as a module named other than __main__.

    Inside `python -m unittest`, the name __main__ already refers to the test
    runner, so a plain `import __main__` would grab the wrong file.
    """
    import importlib.util
    path = Path(tests.PKG / "__main__.py")
    spec = importlib.util.spec_from_file_location("spotube_dj_cli", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("spotube_dj_cli", mod)
    spec.loader.exec_module(mod)
    return mod


class _TmpHome(unittest.TestCase):
    """Every test gets its own ~/.spotube-dj so state can't leak."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._p1 = mock.patch.object(config, "APP_DIR", self._dir)
        self._p2 = mock.patch.object(config, "STATE_FILE", self._dir / "state.json")
        self._p3 = mock.patch.object(config, "HISTORY_FILE", self._dir / "history.jsonl")
        for p in (self._p1, self._p2, self._p3):
            p.start()
        config.ensure_dirs()
        # no LLM: force the offline parser
        self._p4 = mock.patch.object(brain.config, "LLM_API_KEY", "")
        self._p5 = mock.patch.object(brain.config, "LLM_BASE_URL", "")
        self._p4.start()
        self._p5.start()

    def tearDown(self) -> None:
        for p in (self._p4, self._p5, self._p3, self._p2, self._p1):
            p.stop()
        self._tmp.cleanup()


# -------------------------------------------------------------------- brain
class BrainTests(unittest.TestCase):
    def test_engine_is_offline_without_keys(self):
        with mock.patch.object(brain.config, "LLM_API_KEY", ""), \
             mock.patch.object(brain.config, "LLM_BASE_URL", ""):
            self.assertEqual(brain.configured_engine(), "offline")

    def test_engine_gemini_with_key(self):
        with mock.patch.object(brain.config, "LLM_API_KEY", "x"), \
             mock.patch.object(brain.config, "LLM_BASE_URL",
                               "https://generativelanguage.googleapis.com/v1beta"):
            self.assertEqual(brain.configured_engine(), "gemini")

    def test_strips_filler_keeps_signal(self):
        r = brain._heuristic("play some lofi hip hop for studying")["queries"]
        joined = " ".join(r)
        self.assertIn("lofi", joined)
        self.assertIn("study", joined)
        for junk in ("play", "some", "please"):
            self.assertNotIn(junk, max(r, key=len).split()[:1], junk)

    def test_negative_wish_becomes_avoid_not_a_query(self):
        out = brain._heuristic("fast aggressive workout metal, no pop please")
        self.assertTrue(out["avoid"], "expected an avoid list")
        self.assertTrue(any("pop" in a for a in out["avoid"]), out["avoid"])
        self.assertFalse(any(q.strip().startswith("no pop") for q in out["queries"]),
                         out["queries"])

    def test_comma_facets_split(self):
        out = brain._heuristic("dark trip hop, 90s massive attack style, slow")
        self.assertGreaterEqual(len(out["queries"]), 2, out["queries"])

    def test_always_returns_something(self):
        for raw in ("", "   ", "play", "music", "?!"):
            r = brain._heuristic(raw)
            self.assertIsInstance(r["queries"], list)
            self.assertIn("why", r)

    def test_extract_json_handles_fences_and_prose(self):
        self.assertEqual(brain._extract_json('```json\n{"queries": ["a b c"]}\n```'),
                         {"queries": ["a b c"]})
        self.assertEqual(brain._extract_json('Sure! {"queries": ["x y"], "why": "z"} done'),
                         {"queries": ["x y"], "why": "z"})
        self.assertIsNone(brain._extract_json("no json here at all"))

    def test_plan_falls_back_when_llm_returns_garbage(self):
        with mock.patch.object(brain, "configured_engine", return_value="gemini"), \
             mock.patch.object(brain, "_gemini", return_value={"queries": []}), \
             mock.patch.object(brain, "_openai_compat", return_value=None), \
             mock.patch.object(brain, "_heuristic",
                               return_value={"queries": ["lofi beats"], "avoid": [], "why": "x"}):
            p = brain.plan("lofi")
            self.assertEqual(p["engine"], "gemini (fallback)")
            self.assertEqual(p["queries"], ["lofi beats"])

    def test_plan_merges_avoid_from_parser(self):
        with mock.patch.object(brain, "configured_engine", return_value="gemini"), \
             mock.patch.object(brain, "_gemini",
                               return_value={"queries": ["a b"], "why": "w"}), \
             mock.patch.object(brain, "_heuristic",
                               return_value={"queries": ["zz"], "avoid": ["pop"], "why": "h"}):
            self.assertEqual(brain.plan("no pop please")["avoid"], ["pop"])

    def test_llm_network_failure_never_raises(self):
        import urllib.error
        with mock.patch.object(brain, "configured_engine", return_value="gemini"), \
             mock.patch.object(brain, "_post",
                               side_effect=urllib.error.URLError("no route")), \
             mock.patch.object(brain.config, "LLM_API_KEY", "k"):
            p = brain.plan("techno")
            self.assertIn("queries", p)
            self.assertTrue(p["queries"])


# -------------------------------------------------------------------- taste
class TasteTests(_TmpHome):
    def test_like_builds_artist_weight(self):
        taste.record_like({"title": "Neon Rain", "artist": "Boards of Canada"})
        st = config.load_state()
        self.assertGreater(st["artists"]["boards of canada"], 0)

    def test_early_skip_is_penalised_harder_than_late(self):
        t = {"title": "x", "artist": "Hated Artist"}
        taste.record_skip(t, "early-skip")
        early = config.load_state()["artists"]["hated artist"]
        self.assertLess(early, 0)

        # a *late* skip of a different artist should hurt strictly less
        taste.record_skip({"title": "y", "artist": "Mostly Fine Artist"}, "partial")
        late = config.load_state()["artists"]["mostly fine artist"]
        self.assertLess(late, 0)
        self.assertGreater(late, early, f"partial={late} should be milder than early={early}")

    def test_dislike_is_stronger_than_any_skip_and_is_remembered(self):
        # the 👎 on a queue row is a verdict, not a "done" skip: it must cost the
        # artist more than even an early skip, and it has to be visible as a reason
        taste.record_skip({"title": "p", "artist": "Skip Artist"}, "early-skip")
        early = config.load_state()["artists"]["skip artist"]
        taste.record_dislike({"title": "q", "artist": "Hated Artist"})
        st = config.load_state()
        self.assertEqual(st["artists"]["hated artist"], -2.4)
        self.assertLess(st["artists"]["hated artist"], early)
        self.assertIn("dislike", [s.get("reason") for s in st["skipped"]])

    def test_fingerprint_merges_the_same_song_from_two_channels(self):
        a = taste.fingerprint("Joji - SLOW DANCING IN THE DARK")
        b = taste.fingerprint("SLOW DANCING IN THE DARK (Official Video)")
        c = taste.fingerprint("Joji - Slow Dancing In The Dark [Lyrics]")
        self.assertEqual(a, b, f"{a!r} vs {b!r}")
        self.assertEqual(a, c, f"{a!r} vs {c!r}")
        self.assertNotEqual(a, taste.fingerprint("Joji - Glimpse of Us"))

    def test_fingerprint_keeps_distinct_versions_apart(self):
        # a live take is a different thing to hear than the album cut
        self.assertNotEqual(taste.fingerprint("Wonderwall (Live at BBC)"),
                            taste.fingerprint("Wonderwall"))

    def test_preference_context_with_populated_profile(self):
        """Regression: this crashed with UnboundLocalError once skips existed."""
        taste.record_like({"title": "lofi beats", "artist": "Boards of Canada"})
        taste.record_like({"title": "ambient works", "artist": "Brian Eno"})
        for _ in range(3):
            taste.record_skip({"title": "x", "artist": "Nickelback"}, "early-skip")
        config.touch_last_request("chill night stuff")
        ctx = taste.preference_context()
        self.assertIn("boards of canada", ctx)
        self.assertIn("nickelback", ctx)
        self.assertIn("avoid", ctx)
        self.assertIn("chill night stuff", ctx)

    def test_score_tracks_tolerates_sparse_track_dicts(self):
        taste.record_skip({"title": "t", "artist": "a"}, "early-skip")
        ranked = taste.score_tracks([{"id": "1"}, {"title": None, "artist": None, "id": "2"}],
                                    avoid=None)
        self.assertEqual(len(ranked), 2)

    def test_scoring_prefers_loved_artist_and_penalises_avoid(self):
        taste.record_like({"title": "lfo tape loops", "artist": "Autechre"})
        cands = [
            {"id": "1", "title": "ambient tape loops", "artist": "Random Nobody", "duration": 200},
            {"id": "2", "title": "lfo exercise", "artist": "Autechre", "duration": 210},
            {"id": "3", "title": "best pop hits 2024", "artist": "Pop Station", "duration": 205},
        ]
        ranked = taste.score_tracks(cands, avoid=["pop"])
        self.assertEqual(ranked[0]["id"], "2", ranked)
        self.assertEqual(ranked[-1]["id"], "3", ranked)

    def test_long_mixes_are_penalised(self):
        cands = [{"id": "mix", "title": "lofi radio 6 hour mix", "artist": "Chan", "duration": 22258},
                 {"id": "song", "title": "lofi beat", "artist": "Chan", "duration": 180}]
        ranked = taste.score_tracks(cands)
        self.assertEqual(ranked[0]["id"], "song", "6h mixes must not anchor a DJ queue")

    def test_repeated_skips_of_same_title_decay(self):
        for _ in range(3):
            taste.record_skip({"title": "Annoying Track", "artist": "X"}, "early-skip")
        ranked = taste.score_tracks([
            {"id": "1", "title": "Annoying Track", "artist": "Other", "duration": 200},
            {"id": "2", "title": "Fresh Song", "artist": "New", "duration": 200}])
        self.assertEqual(ranked[0]["id"], "2")

    def test_weights_are_capped_and_pruned(self):
        for _ in range(50):
            taste.record_like({"title": "lofi", "artist": "Spam Artist"})
        st = config.load_state()
        self.assertLessEqual(abs(st["artists"]["spam artist"]), 12.0)

    def test_preference_context_survives_empty_state(self):
        self.assertIn("no listening history", taste.preference_context())

    def test_a_like_keeps_the_id_the_page_needs(self):
        # art and "play this again" both key off the id; a like that stored only
        # words left the loved list as a column of letters forever
        taste.record_like({"title": "Neon Rain", "artist": "BoC", "id": "abc123DEF01"})
        rows = taste.load_state()["liked"]
        self.assertEqual(rows[-1]["id"], "abc123DEF01")
        taste.record_like({"title": "No Id", "artist": "Anon"})
        self.assertEqual(taste.load_state()["liked"][-1]["id"], "",
                         "a missing id must be an empty string, never a KeyError")

    def test_corrupt_state_file_recovers(self):
        config.STATE_FILE.write_text("{not json")
        st = config.load_state()
        self.assertEqual(st["liked"], [])
        taste.record_like({"title": "a", "artist": "b"})  # must not explode


# ------------------------------------------------------- queue + interleave


class HoldTests(_TmpHome):
    """
    The retry cooldown exists so the machine stops hammering a rate-limited
    resolver. It must never make a *button* dead: skip()/prev() force past it,
    the auto-advance does not. Also status() is a query and must not move the
    bookkeeping the 'heard N%' judgement reads.
    """

    class DeadPlayer:
        """mpv that accepts the load but cannot start the stream."""

        log_path = "/tmp/test-mpv.log"

        def __init__(self, works=False):
            self.started = 0
            self.works = works

        def play_url(self, url):
            self.started += 1
            return self.works

        def progress(self):
            return (4.0, 200.0)

        def alive(self):
            return True

        def volume(self, v):
            pass

        def quit(self):
            pass

    def _dj(self, n=6, works=False):
        """
        The player is injected, never spawned: DJ(backend="mpv") starts a real
        mpv and blocks on its IPC socket, which made this whole class hang for
        the socket timeout on a loaded box. HAS_MPV is forced on so the "mpv is
        down" path is exercised on machines that have no mpv at all, instead of
        quietly short-circuiting _try_start() and asserting nothing.
        """
        stub = self.DeadPlayer(works=works)
        with mock.patch.object(dj_mod.player_mod, "HAS_MPV", True), \
             mock.patch.object(dj_mod.player_mod, "MPVPlayer", lambda **kw: stub):
            d = DJ(backend="mpv", headless=False)
        d._topup = lambda **k: None
        d._resolve = lambda t: "http://stream"
        d.add([{"id": f"k{i}", "title": f"T{i}", "artist": "a", "duration": 200,
                "url": f"u{i}", "query": "q"} for i in range(n)])
        return d

    def test_auto_advance_waits_out_the_hold(self):
        d = self._dj()
        self.assertIsNone(d.next(), "all streams failed -> no track, and holding")
        self.assertGreater(d._hold_until, time.time())
        pos, n = d.queue.pos, len(d.queue)
        self.assertIsNone(d.next(), "the machine must back off")
        self.assertEqual((d.queue.pos, len(d.queue)), (pos, n),
                         "a held auto-advance must not consume the queue")

    def test_manual_skip_breaks_the_hold_and_plays(self):
        d = self._dj()
        self.assertIsNone(d.next())                     # engages the hold
        d.player.works = True                           # resolver/player recovered
        self.assertIsNone(d.next(), "the auto path still cools down")
        t = d.skip()                                    # a click is not the machine
        self.assertIsNotNone(t, "a manual skip must never be swallowed by the hold")
        self.assertEqual(d._hold_until, 0.0, "the forced call must clear the hold")
        self.assertEqual(t["title"], "T0")

    def test_a_forced_skip_that_still_fails_re_holds(self):
        d = self._dj()
        self.assertIsNone(d.next())
        held = d._hold_until
        self.assertIsNone(d.skip(), "player is still dead")
        self.assertGreaterEqual(d._hold_until, held, "must go back to holding")
        self.assertEqual(d.queue.pos, 0, "queue still intact after the forced attempt")

    def test_prev_also_escapes_the_hold(self):
        d = self._dj(works=True)
        d.next()
        d.next()
        d._hold_until = time.time() + 30
        self.assertIsNotNone(d.prev(), "prev during a hold must still move")
        self.assertEqual(d._hold_until, 0.0)

    def test_a_new_request_starts_even_while_holding(self):
        """build_queue() and the control server pass force=True: a cooldown from
        the previous request must not swallow the track the user just asked for."""
        d = self._dj()
        self.assertIsNone(d.next())                      # engages the hold
        d.player.works = True                            # player recovered
        d._hold_until = time.time() + 60                  # still cooling down
        self.assertIsNone(d.next(), "the auto path respects it")
        t = d.next(force=True)
        self.assertIsNotNone(t, "a forced start must break through")
        self.assertEqual(d._hold_until, 0.0)

    def test_status_is_read_only(self):
        d = self._dj(works=True)
        d.next()
        d.last_pos = 12.0
        st = d.status()
        self.assertEqual(d.last_pos, 12.0,
                         f"status() mutated the heard-position bookkeeping: {d.last_pos}")
        self.assertIn("position", st)


class QueueTests(unittest.TestCase):
    def test_pop_and_len(self):
        q = Queue()
        q.extend([{"id": "a"}, {"id": "b"}])
        self.assertEqual(len(q), 2)
        self.assertEqual(q.pop()["id"], "a")
        self.assertEqual(len(q), 1)
        q.pop()
        self.assertIsNone(q.pop())

    def test_upcoming_window(self):
        q = Queue()
        q.extend([{"id": str(i)} for i in range(5)])
        q.pop()
        self.assertEqual([t["id"] for t in q.upcoming(2)], ["1", "2"])

    def test_remove_id_drops_only_the_one_queued_row(self):
        q = Queue()
        q.extend([{"id": "a"}, {"id": "b"}, {"id": "c"}])
        q.pop()                                   # "a" is now "history"
        self.assertEqual(q.remove_id("b")["id"], "b")
        self.assertEqual([t["id"] for t in q.items], ["a", "c"])
        self.assertEqual(len(q), 1, "only the still-queued rows count")

    def test_remove_id_will_not_touch_rows_already_popped(self):
        q = Queue()
        q.extend([{"id": "a"}, {"id": "b"}])
        q.pop()                                   # the cursor now sits past "a"
        self.assertIsNone(q.remove_id("a"), "a row behind the cursor is not queued")
        self.assertEqual(len(q), 1)

    def test_remove_id_returns_none_for_an_unknown_or_empty_id(self):
        q = Queue()
        q.extend([{"id": "a"}])
        q.pop()
        self.assertIsNone(q.remove_id("nope"))
        self.assertIsNone(q.remove_id(""))

    def test_interleave_rotates_buckets_and_caps_artists(self):
        a1 = {"id": "a1", "artist": "Same", "title": "one", "score": 9}
        a2 = {"id": "a2", "artist": "Same", "title": "two", "score": 8}
        a3 = {"id": "a3", "artist": "Same", "title": "three", "score": 7}
        b1 = {"id": "b1", "artist": "Other", "title": "four", "score": 6}
        buckets = [[a1, a2, a3], [b1]]
        out = _interleave([a1, a2, a3, b1], buckets, count=10)
        self.assertEqual([t["id"] for t in out], ["a1", "b1", "a2"])

    def test_interleave_empties_gracefully(self):
        self.assertEqual(_interleave([], [], 10), [])


class BuildQueueTests(_TmpHome):
    def _fake_search(self, n=3):
        def fake(q, limit=8, min_dur=60, max_dur=900):
            return [{"id": f"{q}-{i}", "title": f"track {i} {q}", "artist": f"artist {i}",
                     "duration": 200 + i, "url": f"https://music.youtube.com/watch?v={q}-{i}",
                     "source": "youtube-music"} for i in range(min(n, limit))]
        return fake

    def test_dedupes_against_history(self):
        config.append_history({"id": "seed-0", "title": "track 0 seed", "artist": "artist 0"})
        with mock.patch.object(prov, "yt_search", self._fake_search()), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            tracks, info = build_queue("seed", count=10)
        self.assertNotIn("seed-0", [t["id"] for t in tracks], "already-played track replayed")

    def test_empty_searches_do_not_crash(self):
        with mock.patch.object(prov, "yt_search", lambda *a, **k: []), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            tracks, info = build_queue("obscure noise", count=5)
        self.assertEqual(tracks, [])
        self.assertIn("empty_searches", info)

    def test_same_song_from_two_channels_is_queued_once(self):
        def fake(q, limit=8, min_dur=60, max_dur=900):
            return [
                {"id": "chanA-1", "title": "ArtistX - One Song", "artist": "ArtistX",
                 "duration": 200, "url": "u"},
                {"id": "chanB-9", "title": "One Song (Official Video)", "artist": "ArtistX",
                 "duration": 200, "url": "u"},
            ]
        with mock.patch.object(prov, "yt_search", fake), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            tracks, _ = build_queue("mood a, mood b", count=10)
        titles = [t["id"] for t in tracks]
        self.assertNotIn("chanB-9", titles, f"duplicate leaked: {titles}")

    def test_radio_stream_cannot_pollute_a_multi_query_set(self):
        """yt_search's last-resort fallback must not become 'track 1 of 20'."""
        def fake(q, limit=8, min_dur=60, max_dur=900):
            if q == "lofi radio":
                # on-topic (shares "lofi") but a stream, so dropped at queue level
                return [{"id": "LIVE", "title": "lofi hip hop radio 24/7", "artist": "Lofi Girl",
                         "duration": 0, "url": "u"}]
            return [{"id": f"ok-{q}", "title": f"real {q} song", "artist": "A",
                     "duration": 200, "url": "u"}]
        with mock.patch.object(prov, "yt_search", fake), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            tracks, info = build_queue("mood one, mood two", count=8,
                                      extra_queries=["lofi radio"])
        self.assertNotIn("LIVE", [x["id"] for x in tracks])
        self.assertGreaterEqual(info["streams_dropped"], 1)

    def test_a_tagged_longform_fallback_is_dropped_too(self):
        """A 40-minute "set" has a duration, so the live-stream regex cannot see
        it. providers tags what it demotes and the queue builder drops the tag."""
        def fake(q, limit=8, min_dur=60, max_dur=900):
            if q == "chill soul":
                return [{"id": "SET40", "title": "R&B Soul | Chill Soul Music with Warm Vocals",
                         "artist": "DJ Otis", "duration": 2400, "url": "u", "longform": True}]
            return [{"id": f"ok-{q}", "title": f"real {q} song", "artist": "A",
                     "duration": 200, "url": "u"}]
        with mock.patch.object(prov, "yt_search", fake), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            tracks, info = build_queue("mood one, mood two", count=8,
                                      extra_queries=["chill soul"])
        self.assertNotIn("SET40", [x["id"] for x in tracks])
        self.assertGreaterEqual(info["streams_dropped"], 1)

    def test_query_count_is_bounded(self):
        with mock.patch.object(prov, "yt_search", self._fake_search(30)), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            tracks, info = build_queue("lofi, jazz, techno, ambient, dnb", count=8)
        self.assertLessEqual(len(tracks), 8)
        self.assertLessEqual(len(info["queries"]), 10)

    def test_explicit_queries_are_merged(self):
        seen = []

        def fake(q, limit=8, min_dur=60, max_dur=900):
            seen.append(q)
            return [{"id": f"x{len(seen)}", "title": f"t{len(seen)}", "artist": "a",
                     "duration": 200, "url": "u"}]
        with mock.patch.object(prov, "yt_search", fake), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            build_queue("lofi", count=5, extra_queries=["handpicked query"])
        self.assertIn("handpicked query", seen)

    def test_playlist_seed_generates_artist_queries(self):
        seen = []

        def fake(q, limit=8, min_dur=60, max_dur=900):
            seen.append(q)
            return [{"id": f"y{len(seen)}", "title": f"tt{len(seen)}", "artist": "Bibio",
                     "duration": 200, "url": "u"}]
        with mock.patch.object(prov, "yt_search", fake), \
             mock.patch.object(prov.Spotify, "playlist_seed",
                               return_value=[{"id": "p1", "title": "Lovesong",
                                              "artist": "Bibio, Hannah Reid", "duration": 200}]):
            build_queue("whatever", seed_refs=["37i9dQZF1DXcBWIGoYBM5M"], count=5)
        self.assertTrue(any("bibio" in s for s in seen), seen)


# ---------------------------------------------------------------- providers

class SeedTrackTests(_TmpHome):
    """`build_queue(seeds=[...])` - the shape "start a station from this song" needs.

    The Tk radio button called it with `seeds=` for as long as it existed, and
    build_queue had no such parameter, so the click raised a TypeError inside a
    worker thread and the station just never appeared. Both halves are pinned here:
    the keyword, and the fact that an explicit seed must not be widened into your
    whole liked library by the Spotify fallback.
    """

    def test_explicit_seeds_reach_the_planner(self):
        seen = {}

        def plan(request, seeds=None, **kw):
            seen["request"] = request
            seen["seeds"] = seeds
            return {"queries": ["seed artist"], "why": "station", "filters": {}}

        def fake(q, limit=8, min_dur=60, max_dur=900):
            return [{"id": "s1", "title": "Another Song", "artist": "Seed Artist",
                     "duration": 200, "url": "u"}]

        with mock.patch.object(brain, "plan", plan), \
             mock.patch.object(prov, "yt_search", fake), \
             mock.patch.object(prov, "CLIENT_ID", "id"), \
             mock.patch.object(prov, "CLIENT_SECRET", "s"), \
             mock.patch.object(prov.Spotify, "liked",
                               side_effect=AssertionError("the library leaked into a station")):
            tracks, info = build_queue("more like Seed Artist - Seed Song", count=5,
                                       seeds=[{"title": "Seed Song",
                                               "artist": "Seed Artist", "url": ""}])
        self.assertEqual(seen["seeds"][0]["artist"], "Seed Artist")
        self.assertEqual(tracks[0]["id"], "s1")
        self.assertIn("station", info["why"])

    def test_a_track_shaped_seed_is_accepted_and_empty_ones_ignored(self):
        captured = {}

        def plan(request, seeds=None, **kw):
            captured["seeds"] = seeds
            return {"queries": ["q one"], "why": "", "filters": {}}

        with mock.patch.object(brain, "plan", plan), \
             mock.patch.object(prov, "yt_search",
                               return_value=[{"id": "x", "title": "T", "artist": "A",
                                              "duration": 200, "url": "u"}]):
            build_queue("mood", count=5,
                        seeds=[{}, None, {"title": "Real", "artist": "Artist"}])
        self.assertEqual(len(captured["seeds"]), 1, "junk seeds are not passed on")
        self.assertEqual(captured["seeds"][0]["title"], "Real")

    def test_seeds_and_a_playlist_reference_both_contribute(self):
        captured = {}

        def plan(request, seeds=None, **kw):
            captured["seeds"] = list(seeds or [])
            return {"queries": ["q one"], "why": "", "filters": {}}

        got = [{"id": "", "title": "From Playlist", "artist": "PL Artist", "duration": 200}]
        with mock.patch.object(brain, "plan", plan), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=got), \
             mock.patch.object(prov, "yt_search",
                               return_value=[{"id": "y", "title": "T", "artist": "A",
                                              "duration": 200, "url": "u"}]):
            build_queue("mood", count=5, seed_refs=["37i9dQZF1DXcBWIGoYBM5M"],
                        seeds=[{"title": "Clicked", "artist": "Row Artist"}])
        artists = [s["artist"] for s in captured["seeds"]]
        self.assertIn("Row Artist", artists)
        self.assertIn("PL Artist", artists)

    def test_the_gui_radio_call_shape_is_the_supported_one(self):
        # gui.do_radio's exact kwargs, so the two cannot drift apart again
        import inspect
        sig = inspect.signature(build_queue)
        self.assertIn("seeds", sig.parameters)
        call = {"request": "more like A - B", "count": 20,
                "seeds": [{"title": "B", "artist": "A", "url": ""}]}
        with mock.patch.object(prov, "yt_search", return_value=[]), \
             mock.patch.object(prov, "ytm_search", return_value=[]):
            tracks, info = build_queue(**call)
        self.assertEqual(tracks, [])
        self.assertIn("engine", info)


class RelevanceTests(_TmpHome):
    def test_ad_review_video_is_rejected(self):
        from dj import _relevant
        t = {"title": "I Tried the NEW Zenni Night Driving Glasses", "artist": "Tech Bro"}
        self.assertFalse(_relevant(t, "dark synthwave for night driving"),
                         "an unboxing must never be queued by a music DJ")

    def test_real_match_is_accepted(self):
        from dj import _relevant
        t = {"title": "Dark Synthwave Night Drive Mixtape", "artist": "Neon Pulse"}
        self.assertTrue(_relevant(t, "dark synthwave for night driving"))

    def test_vague_query_does_not_reject_everything(self):
        from dj import _relevant
        self.assertTrue(_relevant({"title": "anything", "artist": "x"}, "for me please"))

    def test_ad_signal_blocks_even_with_token_overlap(self):
        from dj import _relevant
        t = {"title": "synthwave gear review - my whole setup", "artist": "Reviewer"}
        self.assertFalse(_relevant(t, "synthwave"))

    def test_build_queue_reports_filtered_count(self):
        def fake(q, limit=8, min_dur=60, max_dur=900):
            # ids/titles vary per query so the run's own dedupe doesn't mask counts
            return [{"id": f"ad-{q}", "title": f"{q} glasses unboxing", "artist": "Ads R Us",
                     "duration": 200, "url": "u"},
                    {"id": f"ok-{q}", "title": f"dark {q} highway", "artist": "Neon",
                     "duration": 200, "url": "u"}]
        with mock.patch.object(prov, "yt_search", fake), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            tracks, info = build_queue("dark synthwave for night driving", count=5)
        ids = [x["id"] for x in tracks]
        self.assertTrue(ids and all("ad-" not in i for i in ids), f"ads leaked: {ids}")
        self.assertGreaterEqual(info["off_topic_filtered"], 1)


class ProviderTests(unittest.TestCase):
    def test_track_meta_extracts_album_and_release_year(self):
        raw = json.dumps({"id": "v", "title": "Roads", "album": "Dummy",
                          "artist": "Portishead", "release_date": "19940822"})
        with mock.patch.object(prov, "_run", return_value=raw):
            m = prov.yt_track_meta("v")
        self.assertEqual(m.get("album"), "Dummy")
        self.assertEqual(m.get("release_year"), 1994, "pulled out of the date string")
        self.assertIn("album_url", m)
        self.assertIn("music.youtube.com", m["album_url"])

    def test_track_meta_is_blank_on_no_output_or_bad_video(self):
        with mock.patch.object(prov, "_run", return_value=""):
            self.assertEqual(prov.yt_track_meta("v"), {})
        with mock.patch.object(prov, "_run", return_value="not json\n"):
            self.assertEqual(prov.yt_track_meta("v"), {})

    def test_track_meta_ignores_lines_that_are_not_json(self):
        raw = "noise line\n" + json.dumps({"id": "v", "album": "A", "release_year": 1999})
        with mock.patch.object(prov, "_run", return_value=raw):
            m = prov.yt_track_meta("v")
        self.assertEqual(m.get("album"), "A")
        self.assertEqual(m.get("release_year"), 1999)

    def test_playlist_id_parsing(self):
        sp = prov.Spotify()
        for ref in ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc",
                    "37i9dQZF1DXcBWIGoYBM5M", "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"):
            self.assertEqual(sp.playlist_id(ref), "37i9dQZF1DXcBWIGoYBM5M", ref)
        self.assertIsNone(sp.playlist_id("not a playlist"))

    def test_search_uses_items_not_removed_tracks_endpoint(self):
        """GET /playlists/{id}/tracks was removed in Feb 2026 - we must use /items."""
        src = Path(PKG_DIR / "providers.py").read_text()
        self.assertIn("/items", src)
        for removed in ('"/tracks"', "/browse/new-releases", "/artists/{id}/top-tracks",
                        "/me/player/play", "/me/player/pause", "/me/player/next"):
            self.assertNotIn(removed, src, f"uses removed/Premium-only endpoint {removed}")

    def test_m3u_roundtrip(self):
        tracks = [{"id": "v1", "title": "Song A", "artist": "Artist", "duration": 212,
                   "url": "https://music.youtube.com/watch?v=v1"},
                  {"id": "v2", "title": "Song B", "artist": "Artist 2", "duration": 0,
                   "stream": "https://googlevideo/x"}]
        with tempfile.TemporaryDirectory() as d:
            p = prov.write_m3u(tracks, Path(d) / "out.m3u8", title="T")
            text = p.read_text()
        self.assertTrue(text.startswith("#EXTM3U"))
        self.assertIn("#EXTINF:212,Artist - Song A", text)
        # explicit stream wins over the page URL
        self.assertIn("https://googlevideo/x", text)
        self.assertEqual(len([l for l in text.splitlines()
                             if l and not l.startswith("#")]), 2)

    def test_player_api_never_used(self):
        """The whole point: no /me/player control calls anywhere in the project."""
        for f in sorted(Path(PKG_DIR).glob("*.py")):
            body = f.read_text()
            for endpoint in ("/me/player/play", "/me/player/pause", "/me/player/next",
                             "/me/player/previous", "/me/player/volume", "/me/player/seek",
                             "/me/player/queue", "start_playback", "pause_playback",
                             "next_track", "previous_track", "transfer_playback",
                             "devices()", "currently_playing()"):
                self.assertNotIn(endpoint, body, f"{f.name} touches {endpoint}")

    def test_yt_search_filters_six_hour_mixes(self):
        rows = [
            {"id": "LONG", "title": "Best of lofi 2021 [mix] 6 hours",
             "channel": "Lofi Girl", "duration": 22258},
            {"id": "RADIO", "title": "lofi hip hop radio", "channel": "Lofi Girl",
             "duration": 86400},
            {"id": "LIVE", "title": "lofi hip hop radio 24/7", "channel": "Lofi Girl",
             "duration": None},
            {"id": "OK1", "title": "a real song", "channel": "SomeArtist - Topic",
             "duration": 195},
        ]
        payload = "\n".join(json.dumps(r) for r in rows)
        with mock.patch.object(prov, "_run", return_value=payload):
            out = prov.yt_search("lofi", limit=5, max_dur=900)
        ids = [t["id"] for t in out]
        self.assertEqual(ids, ["OK1"], f"expected only the real song, got {ids}")

    def test_yt_search_falls_back_when_everything_is_long_form(self):
        # 25 min: over our preferred window, but still a listenable single
        # item, so it may be offered as a last resort.
        rows = [{"id": "LONG", "title": "only a long mix exists", "channel": "X",
                 "duration": 1500}]
        payload = "\n".join(json.dumps(r) for r in rows)
        with mock.patch.object(prov, "_run", return_value=payload):
            out = prov.yt_search("obscure", limit=5, max_dur=900)
        self.assertEqual([t["id"] for t in out], ["LONG"],
                         "better a long mix than silence - but it must be last resort")

    def test_yt_search_rejects_karaoke_and_tutorials(self):
        rows = [
            {"id": "KAR", "title": "Hit The Button Karaoke Slow Dancing (Originally Performed By Joji)",
             "channel": "x", "duration": 200},
            {"id": "TUT", "title": "What is FILM SCORING? | Most important concepts",
             "channel": "y", "duration": 300},
            {"id": "SNIP", "title": "real song", "channel": "z", "duration": 12},
            {"id": "REAL", "title": "Joji - SLOW DANCING IN THE DARK", "channel": "88rising",
             "duration": 220},
        ]
        payload = "\n".join(json.dumps(r) for r in rows)
        with mock.patch.object(prov, "_run", return_value=payload):
            out = prov.yt_search("dark cello", limit=5, min_dur=60, max_dur=900)
        self.assertEqual([t["id"] for t in out], ["REAL"], out)

    def test_24_7_livestream_is_never_used_even_as_fallback(self):
        rows = [
            {"id": "LIVE", "title": "lofi hip hop radio - beats to relax", "channel": "Lofi Girl",
             "duration": 3674},
            {"id": "FOREVER", "title": "chill stream", "channel": "x", "duration": 70000},
            {"id": "SHORTMIX", "title": "a lofi mix", "channel": "y", "duration": 2400},
        ]
        payload = "\n".join(json.dumps(r) for r in rows)
        with mock.patch.object(prov, "_run", return_value=payload):
            out = prov.yt_search("lofi", limit=5, max_dur=900)
        ids = [t["id"] for t in out]
        self.assertNotIn("FOREVER", ids, "a 19h upload must never be offered")
        self.assertEqual(ids, ["SHORTMIX"], f"should fall back to the shortest long-form: {ids}")

    def test_yt_search_rejects_ai_and_samplepack_noise(self):
        rows = [
            {"id": "AI", "title": "Leather Jacket (AI Generated 70s Philly Soul)",
             "channel": "ALL DAI RECORDS", "duration": 200},
            {"id": "PACK", "title": "Philly Soul Samples | Smooth Grooves", "channel": "x",
             "duration": 300},
            {"id": "JP", "title": "\u8d64\u3044\u30b9\u30a4\u30fc\u30c8\u30d4\u30fc / \u677e\u7530\u8056\u5b50 \u2013 Philly Soul ver.",
             "channel": "Funny J-POP", "duration": 240},
            {"id": "REAL", "title": "The Love I Lost", "channel": "Harold Melvin",
             "duration": 380},
        ]
        payload = "\n".join(json.dumps(r) for r in rows)
        with mock.patch.object(prov, "_run", return_value=payload):
            out = prov.yt_search("philly soul 70s", limit=5)
        self.assertEqual([t["id"] for t in out], ["REAL"], out)

    def test_yt_search_drops_unparseable_lines(self):
        """yt-dlp sometimes emits warnings into stdout; junk must not crash us."""
        payload = "WARNING: something\n" + json.dumps(
            {"id": "good", "title": "ok song", "channel": "c", "duration": 180}) + "\n[gibberish"
        with mock.patch.object(prov, "_run", return_value=payload):
            out = prov.yt_search("q", limit=5)
        self.assertEqual([t["id"] for t in out], ["good"])


class OriginTests(unittest.TestCase):
    """
    "filter only the original songs appear" - a cover or a remix of a song is not
    the recording a mood mix is for. decide() refuses a non-original title outright
    (an official remix single by the artist still shows), and yt_search drops a fan
    re-upload once the artist's own row is present, so the queue and the search tab
    both read like Apple Music rather than a reaction channel.
    """

    @staticmethod
    def _official(vid, title="Smells Like Teen Spirit"):
        return {"id": vid, "title": title, "artist": "Nirvana", "channel": "Nirvana",
                "official": True, "duration": 302, "url": "u"}

    @staticmethod
    def _fan(vid, title="Smells Like Teen Spirit", channel="ScottishTeeVee"):
        return {"id": vid, "title": title, "artist": "", "channel": channel,
                "official": False, "duration": 286, "url": "u"}

    def test_prefer_originals_keeps_only_the_artist_when_any_exists(self):
        rows = [self._official("o1"), self._fan("f1"),
                {"id": "o2", "title": "Come as You Are", "artist": "Nirvana",
                 "channel": "Nirvana - Topic", "duration": 219, "url": "u"}]
        got = prov._prefer_originals(rows)
        self.assertEqual([t["id"] for t in got], ["o1", "o2"],
                         "a fan re-upload survived when the original was present")
        # with no original at all, the fan rows are kept rather than going silent
        solo = [self._fan("f1"), self._fan("f2", title="Come as You Are")]
        self.assertEqual([t["id"] for t in prov._prefer_originals(solo)], ["f1", "f2"])

    def test_cover_and_remix_by_a_fan_are_refused_but_an_official_one_is_not(self):
        for title in ("Smells Like Teen Spirit (cover by Delicious Rock)",
                      "Smells Like Teen Spirit - Remix",
                      "Wonderwall (Acoustic Cover)",
                      "Song (Reimagined)"):
            with self.subTest(title=title):
                v = filters.decide({"title": title, "duration": 250,
                                    "channel": "Some Fan"})
                self.assertEqual(v["kind"], filters.NOTAUDIO, title)
        # an official remix single by the artist is a release, not a bootleg
        official = filters.decide({"title": "Smells Like Teen Spirit (Remix)",
                                   "duration": 250, "channel": "Nirvana",
                                   "official": True})
        self.assertEqual(official["kind"], filters.TRACK)

    def test_yt_search_drops_a_fan_upload_when_the_original_answer_is_there(self):
        fan, official = self._fan("fan1"), self._official("off1")
        with mock.patch.object(prov, "ytm_enabled", return_value=True), \
             mock.patch.object(prov, "ytm_search", return_value=[fan, official]):
            rows = prov.yt_search("nirvana songs", limit=8)
        self.assertEqual([t["id"] for t in rows], ["off1"],
                         "the fan re-upload outranked the artist's own recording")

    def test_yt_search_keeps_fan_uploads_when_no_original_exists(self):
        fan1, fan2 = self._fan("fan1"), self._fan("fan2", title="Come as You Are")
        with mock.patch.object(prov, "ytm_enabled", return_value=True), \
             mock.patch.object(prov, "ytm_search", return_value=[fan1, fan2]):
            rows = prov.yt_search("a very obscure local band", limit=8)
        self.assertEqual(set(t["id"] for t in rows), {"fan1", "fan2"},
                         "no original was found; the uploads are still music")


class BrowseTests(unittest.TestCase):
    """
    The deep Artist / Album page data comes off YouTube Music's *browse* endpoint,
    not the search the queue uses. These feed synthetic InnerTube responses through
    the parser (the same walkers search uses) so the rows a page will draw are
    pinned here without the network. A response the parser does not recognise must
    yield [] - the caller falls back to an ordinary search, never to a broken page.
    """

    SEARCH_ALBUM = {"contents": {"sectionListRenderer": {"contents": [
        {"musicShelfRenderer": {"contents": [{
            "musicTwoRowItemRenderer": {
                "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails":
                    [{"url": "https://i.ytimg.com/vi/dummy/hqdefault.jpg",
                      "width": 544, "height": 544}]}}},
                "title": {"runs": [{"text": "Dummy"}]},
                "subtitle": {"runs": [{"text": "Album • Portishead • 1994"}]},
                "navigationEndpoint": {"browseEndpoint": {"browseId": "BDEADBEEF"}},
            }}]}}]}}}

    BROWSE_ALBUM = {"contents": {"sectionListRenderer": {"contents": [
        {"musicShelfRenderer": {"contents": [
            {"musicResponsiveListItemRenderer": {
                "playlistItemData": {"videoId": "roads1"},
                "flexColumns": [
                    {"musicResponsiveListItemFlexColumnRenderer": {"text":
                        {"runs": [{"text": "Roads"}]}}},
                    {"musicResponsiveListItemFlexColumnRenderer": {"text":
                        {"runs": [{"text": "Portishead • Dummy"}]}}},
                    {"musicResponsiveListItemFlexColumnRenderer": {"text":
                        {"runs": [{"text": "5:03"}]}}}],
                "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails":
                    [{"url": "https://i.ytimg.com/vi/roads1/mqdefault.jpg",
                      "width": 60, "height": 60}]}}}}},
            {"musicResponsiveListItemRenderer": {
                "playlistItemData": {"videoId": "roads2"},
                "flexColumns": [
                    {"musicResponsiveListItemFlexColumnRenderer": {"text":
                        {"runs": [{"text": "Sour Times"}]}}},
                    {"musicResponsiveListItemFlexColumnRenderer": {"text":
                        {"runs": [{"text": "Portishead • Dummy • 3:58"}]}}}]}}
        ]}}]}}}

    SEARCH_ARTIST = {"contents": {"sectionListRenderer": {"contents": [
        {"musicShelfRenderer": {"contents": [{
            "musicTwoRowItemRenderer": {
                "title": {"runs": [{"text": "Portishead"}]},
                "subtitle": {"runs": [{"text": "Artist"}]},
                "navigationEndpoint": {"browseEndpoint": {"browseId": "UCabc123"}},
            }}]}}]}}}

    BROWSE_ARTIST = {"contents": {"sectionListRenderer": {"contents": [
        {"musicShelfRenderer": {"contents": [
            {"musicTwoRowItemRenderer": {
                "title": {"runs": [{"text": "Dummy"}]},
                "subtitle": {"runs": [{"text": "Album • Portishead • 1994 • 10 songs"}]},
                "navigationEndpoint": {"browseEndpoint": {"browseId": "BDEADBEEF"}}}},
            {"musicTwoRowItemRenderer": {
                "title": {"runs": [{"text": "Portishead"}]},
                "subtitle": {"runs": [{"text": "Album • Portishead • 1997 • 12 songs"}]},
                "navigationEndpoint": {"browseEndpoint": {"browseId": "B2"}}}},
            {"musicTwoRowItemRenderer": {
                "title": {"runs": [{"text": "Roseland NYC Live"}]},
                "subtitle": {"runs": [{"text": "Album • Portishead • 1998 • 14 songs"}]},
                "navigationEndpoint": {"browseEndpoint": {"browseId": "B3"}}}},
        ]}}]}}}

    def _browse_json(self, *, search=None, browse=None, search_url=prov.YTM_ENDPOINT):
        """A _http_json double that answers the search then the browse call."""
        def fake(url, body, timeout=20):
            if url == search_url:
                return search if search is not None else None
            if url == prov.YTM_BROWSE:
                return browse if browse is not None else None
            return None
        return fake

    def test_album_tracklist_uses_the_browse_endpoint(self):
        fake = self._browse_json(search=self.SEARCH_ALBUM, browse=self.BROWSE_ALBUM)
        with mock.patch.object(prov, "_http_json", side_effect=fake):
            rows = prov.ytm_album_tracklist("Dummy", artist="Portishead")
        self.assertEqual([t["id"] for t in rows], ["roads1", "roads2"])
        self.assertEqual(rows[0]["title"], "Roads")
        self.assertEqual(rows[0]["album"], "Dummy", "the record name must travel")
        self.assertEqual(rows[0]["release_year"], 1994, "year read off the card")
        self.assertIn("Dummy", rows[0].get("note", ""))
        self.assertEqual(rows[0]["url"],
                         "https://music.youtube.com/watch?v=roads1")

    def test_album_tracklist_returns_empty_when_no_album_card(self):
        # a search with no Album card (e.g. a plain song result) must not crash
        fake = self._browse_json(search={"contents": {"sectionListRenderer": {"contents":
            [{"musicShelfRenderer": {"contents": []}}]}}})
        with mock.patch.object(prov, "_http_json", side_effect=fake):
            rows = prov.ytm_album_tracklist("No Such Record")
        self.assertEqual(rows, [])

    def test_artist_discography_reads_albums_and_years(self):
        fake = self._browse_json(search=self.SEARCH_ARTIST, browse=self.BROWSE_ARTIST)
        with mock.patch.object(prov, "_http_json", side_effect=fake):
            albums = prov.ytm_artist_discography("Portishead")
        titles = [a["title"] for a in albums]
        self.assertEqual(titles, ["Dummy", "Portishead", "Roseland NYC Live"])
        self.assertEqual(albums[0]["release_year"], 1994)
        self.assertEqual(albums[0]["kind"], "album")
        self.assertEqual(albums[0]["browse_id"], "BDEADBEEF")
        self.assertIn("1994", albums[0]["note"])
        # a discography entry is not a playable song: it has no video id
        self.assertEqual(albums[0]["id"], "")

    def test_artist_discography_returns_empty_on_unrecognisable_page(self):
        fake = self._browse_json(search=self.SEARCH_ARTIST, browse={"contents": {}})
        with mock.patch.object(prov, "_http_json", side_effect=fake):
            albums = prov.ytm_artist_discography("Portishead")
        self.assertEqual(albums, [])

    def test_discography_prefers_albums_over_the_singles_shelf(self):
        # an artist page lists Albums then Singles & EPs; "why its show all single"
        # was the singles dominating - so albums are the discography, singles only
        # when an artist never cut an album
        page = {"contents": {"sectionListRenderer": {"contents": [
            {"musicShelfRenderer": {"contents": [
                {"musicTwoRowItemRenderer": {
                    "title": {"runs": [{"text": "The Singles EP"}]},
                    "subtitle": {"runs": [{"text": "Single • Portishead • 1998"}]},
                    "navigationEndpoint": {"browseEndpoint": {"browseId": "B_single"}}}},
                {"musicTwoRowItemRenderer": {
                    "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails":
                        [{"url": "https://i.ytimg.com/vi/dummy/hqdefault.jpg",
                          "width": 544, "height": 544}]}}},
                    "title": {"runs": [{"text": "Dummy"}]},
                    "subtitle": {"runs": [{"text": "Album • Portishead • 1994"}]},
                    "navigationEndpoint": {"browseEndpoint": {"browseId": "B_album"}}}},
            ]}}]}}}
        fake = self._browse_json(search=self.SEARCH_ARTIST, browse=page)
        with mock.patch.object(prov, "_http_json", side_effect=fake):
            rows = prov.ytm_artist_discography("Portishead")
        self.assertEqual([r["title"] for r in rows], ["Dummy"],
                         "the singles shelf outranked the album")
        self.assertEqual(rows[0]["browse_id"], "B_album")
        self.assertEqual(rows[0]["release_year"], 1994)
        self.assertTrue(rows[0]["thumbnail"].startswith("http"),
                        "the discography entry carries the album cover")

    def test_discography_falls_back_to_singles_for_an_artist_with_no_album(self):
        page = {"contents": {"sectionListRenderer": {"contents": [
            {"musicShelfRenderer": {"contents": [
                {"musicTwoRowItemRenderer": {
                    "title": {"runs": [{"text": "Only Single"}]},
                    "subtitle": {"runs": [{"text": "Single • Artist • 2020"}]},
                    "navigationEndpoint": {"browseEndpoint": {"browseId": "B1"}}}},
            ]}}]}}}
        fake = self._browse_json(search=self.SEARCH_ARTIST, browse=page)
        with mock.patch.object(prov, "_http_json", side_effect=fake):
            rows = prov.ytm_artist_discography("Portishead")
        self.assertEqual([r["title"] for r in rows], ["Only Single"])

    def test_clean_artist_strips_the_query_trailing_words(self):
        self.assertEqual(prov._clean_artist("Portishead songs"), "Portishead")
        self.assertEqual(prov._clean_artist("radiohead top songs"), "radiohead")
        self.assertEqual(prov._clean_artist("Miles Davis"), "Miles Davis")

    def test_page_rows_prefers_browse_and_falls_back_to_search(self):
        # browse has a discography -> it wins, the song search is never asked
        fake = self._browse_json(search=self.SEARCH_ARTIST, browse=self.BROWSE_ARTIST)
        with mock.patch.object(prov, "_http_json", side_effect=fake), \
             mock.patch.object(prov, "yt_search", return_value=[{"id": "song"}]) as ys:
            rows = prov.page_rows("artist", "Portishead songs", "Portishead", "")
        self.assertTrue(all(r["kind"] == "album" for r in rows), rows)
        ys.assert_not_called()
        # browse gives nothing -> the fallback search fills the page
        fake = self._browse_json(search=None)
        with mock.patch.object(prov, "_http_json", side_effect=fake), \
             mock.patch.object(prov, "yt_search", return_value=[{"id": "song"}]) as ys:
            rows = prov.page_rows("artist", "Portishead songs", "Portishead", "")
        self.assertEqual(rows[0]["id"], "song")
        ys.assert_called_once()


# ---------------------------------------------------------------------- cli
class CliTests(unittest.TestCase):
    def test_doctor_runs_and_reports(self):
        cli = _load_cli()
        with mock.patch.object(prov, "yt_search",
                               lambda *a, **k: [{"title": "x", "id": "1",
                                                 "artist": "a", "duration": 180,
                                                 "url": "u"}]), \
             mock.patch.object(prov, "yt_stream_url", lambda v: "https://ok"):
            rc = cli.main(["--doctor"])
        self.assertEqual(rc, 0)

    def test_list_mode_without_network(self):
        cli = _load_cli()
        with mock.patch.object(prov, "yt_search",
                               lambda q, limit=8, min_dur=60, max_dur=900: [
                                   {"id": f"z{q}", "title": f"t {q}", "artist": "a",
                                    "duration": 200, "url": "u"}]), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            rc = cli.main(["lofi beats", "--list", "--count", "3"])
        self.assertEqual(rc, 0)

    def test_bare_invocation_opens_the_player(self):
        # `spotube-dj` with nothing else is what a desktop icon runs, so it starts
        # the app instead of printing a wall of flags at the person who clicked it
        cli = _load_cli()
        with mock.patch.object(cli, "cmd_web", return_value=0) as w:
            self.assertEqual(cli.main([]), 0)
            w.assert_called_once()

    def test_help_still_lists_everything(self):
        cli = _load_cli()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "argv", ["spotube-dj", "--help"]):
            with self.assertRaises(SystemExit) as e:
                cli.main(["--help"])
            self.assertEqual(e.exception.code, 0)
        text = out.getvalue()
        for flag in ("--web", "--no-browser", "--search", "--gui"):
            self.assertIn(flag, text, f"{flag} fell off the help")

    def test_bare_verbs_route_to_commands_not_to_the_dj(self):
        """`spotube-dj taste` must not be treated as a request for music."""
        cli = _load_cli()
        with mock.patch("taste.summarize", return_value="likes: 0") as sm, \
             mock.patch("dj.build_queue", side_effect=AssertionError("queried the DJ!")):
            self.assertEqual(cli.main(["taste"]), 0)
            sm.assert_called_once()
        with mock.patch("taste.summarize", return_value="x"):
            self.assertEqual(cli.main(["taste", "clear"]), 0)

    def test_preprocess_table(self):
        cli = _load_cli()
        self.assertEqual(cli._preprocess(["sync"]), ["--sync"])
        self.assertEqual(cli._preprocess(["next"]), ["--next"])
        self.assertEqual(cli._preprocess(["next", "--port", "9"]), ["--next", "--port", "9"])
        self.assertEqual(cli._preprocess(["lofi", "beats"]), ["lofi", "beats"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AdvanceAndLearningTests(unittest.TestCase):
    """
    The 'now playing' bugs: an exhausted queue used to re-judge the same track
    every poll (likes flooded the taste profile) and left it on screen as if it
    were still playing; a run of unresolvable streams burned the whole queue in
    one click.
    """

    class _OKPlayer:
        log_path = "/tmp/t"
        def play_url(self, u): return True
        def alive(self): return True
        def progress(self): return (0.0, 200.0)
        def volume(self, v): pass
        def quit(self): pass

    def _dj(self, n=8, resolve=None, headless=True):
        """
        `resolve=` only takes effect on the mpv path, so asking for one has to
        build that path: DJ(backend="none") short-circuits straight to "started".
        """
        d = DJ(backend="none")
        d.headless = headless
        d.request = ""                      # no auto-topup during the test
        d.info = {}
        d.add([{"id": f"x{i}", "title": f"t{i}", "artist": "a", "duration": 200,
                "url": f"u{i}", "query": "q"} for i in range(n)])
        if resolve is not None:
            d.backend = "mpv"
            d.headless = False
            d.player = self._OKPlayer()
            d._resolve = resolve
        return d

    def test_learn_from_heard_once_per_track(self):
        import taste
        d = self._dj(3)
        d.current = {"id": "solo", "title": "solo", "artist": "zz", "duration": 100}
        d.last_pos = 95.0
        with mock.patch.object(taste, "record_like") as like, \
             mock.patch.object(taste, "record_skip") as skip:
            d._learn_from_heard()
            d._learn_from_heard()
            d._learn_from_heard()
        self.assertEqual(like.call_count, 1, "one judgement per track, not one per poll")
        self.assertEqual(skip.call_count, 0)

    def test_heard_ratio_is_clamped(self):
        d = self._dj(2)
        d.current = {"id": "c", "title": "c", "artist": "a", "duration": 100}
        d.last_pos = 292.0                       # a clock that ran past the end
        notes = []
        d._note = lambda m: notes.append(m)
        import taste
        with mock.patch.object(taste, "record_like"):
            d._learn_from_heard()
        self.assertTrue(any("heard 100%" in n for n in notes), notes)

    def test_exhausted_queue_clears_now_playing(self):
        d = self._dj(1)
        d.next()                                  # plays x0
        self.assertIsNotNone(d.current)
        self.assertIsNone(d.next())                # nothing left
        self.assertIsNone(d.current, "the UI must stop showing the finished track")
        self.assertEqual(d.last_pos, 0.0)

    def test_a_failed_move_keeps_the_title_while_the_track_is_audible(self):
        # the bug the user reported: music was still playing and the panel said
        # "Nothing playing" - because a failed skip cleared `current`, and the
        # Up Next list is built as [now_playing] + up_next, so the whole
        # chronology on screen shifted one row ahead of reality
        class _Playing(AdvanceAndLearningTests._OKPlayer):
            def is_playing(self):
                return True

        d = self._dj(1, resolve=lambda t: None)
        d.player, d.backend, d.headless = _Playing(), "mpv", False
        self.assertIsNone(d.next())                # the only track would not start
        self.assertIsNotNone(d.current, "still audible, so still shown")
        self.assertIn(d.idle, ("finished", "no stream would start"))
        self.assertEqual(d.status()["idle"], d.idle)

    def test_unresolvable_streams_do_not_eat_the_queue(self):
        d = self._dj(8, resolve=lambda t: None)
        self.assertIsNone(d.next())
        self.assertLessEqual(d.queue.pos, DJ.SKIP_LIMIT + 1)
        self.assertGreater(len(d.queue), 0, "unplayed tracks must be given back")
        self.assertIsNone(d.current)
        self.assertGreater(d._hold_until, time.time(), "must hold, not spin")

    def test_hold_blocks_further_burning_then_expires(self):
        d = self._dj(8, resolve=lambda t: None)
        d.next()
        pos_after = d.queue.pos
        for _ in range(5):
            self.assertIsNone(d.next())            # while held: no popping at all
        self.assertEqual(d.queue.pos, pos_after)
        d._hold_until = 0.0                        # cooldown over
        d._resolve = lambda t: "http://ok"
        self.assertIsNotNone(d.next(), "it must resume by itself")

    def test_player_that_refuses_to_start_also_holds(self):
        class Dead:
            log_path = "/tmp/x"
            def play_url(self, u): return False
            def alive(self): return True
            def progress(self): return (0.0, 0.0)
            def volume(self, v): pass
            def quit(self): pass
        d = self._dj(6, resolve=lambda t: "http://u", headless=False)
        d.backend = "mpv"
        d.player = Dead()
        self.assertIsNone(d.next())
        self.assertGreater(len(d.queue), 0)
        self.assertLessEqual(d.queue.pos, DJ.SKIP_LIMIT + 1)

    def test_mpv_that_dies_raises_instead_of_looping(self):
        class Gone:
            log_path = "/tmp/x"
            def play_url(self, u): return False
            def alive(self): return False
            def progress(self): return (0.0, 0.0)
            def volume(self, v): pass
            def quit(self): pass
        d = self._dj(3, resolve=lambda t: "http://u", headless=False)
        d.backend = "mpv"
        d.player = Gone()
        import player as player_mod
        with self.assertRaises(player_mod.PlayerError):
            d.next()

    def test_queue_rewind_and_insert_at(self):
        q = Queue()
        q.extend([{"id": str(i)} for i in range(4)])
        q.pop(); q.pop()
        q.rewind(2)
        self.assertEqual(q.pos, 0)
        q.insert_at(0, {"id": "back"})
        self.assertEqual([t["id"] for t in q.items], ["back", "0", "1", "2", "3"])


class SearchShapeTests(unittest.TestCase):
    """
    providers.yt_search is the thing that decides what counts as a song. A
    40-minute "R&B Soul | Chill ... Grooves" upload once ended up as a track in
    the middle of a mix, so the shape rules are pinned here against a stubbed
    yt-dlp call - no network, no re-implementation of the filter.
    """
    ENTRIES = [
        {"id": "good", "title": "Real Song", "channel": "Artist", "duration": 210},
        {"id": "set_long", "title": "R&B Soul | Chill Soul Music with Warm Vocals",
         "duration": 2400},
        {"id": "set_nodur", "title": "Lo-Fi Beats | study music to relax"},
        {"id": "mix", "title": "Best of 2019 mix", "duration": 3000},
        {"id": "hours", "title": "Two hours of neo soul", "duration": 120},
        {"id": "kar", "title": "Real Song karaoke", "duration": 200},
        {"id": "short_pipe", "title": "Brick | Lada", "duration": 180},   # a real song
    ]

    def _search(self, entries, **kw):
        payload = "\n".join(json.dumps(e) for e in entries)
        with mock.patch.object(prov, "_run", return_value=payload):
            return prov.yt_search("anything", limit=kw.pop("limit", 5), **kw)

    def test_a_song_survives_and_the_sets_do_not(self):
        ids = [r["id"] for r in self._search(self.ENTRIES)]
        self.assertEqual(ids, ["good", "short_pipe"], ids)

    def test_a_short_pipe_title_is_still_a_song(self):
        ids = [r["id"] for r in self._search([{"id": "x", "title": "Brick | Lada",
                                               "duration": 200}])]
        self.assertEqual(ids, ["x"])

    def test_when_only_sets_matched_exactly_one_is_offered(self):
        # better one long mix than silence - but never 20 of them
        rows = self._search([e for e in self.ENTRIES if e["id"].startswith("set")])
        self.assertLessEqual(len(rows), 1)
        self.assertTrue(rows, "a last-resort fallback must still return something")
        self.assertTrue(rows[0].get("longform"), "the fallback has to say what it is")

    def test_a_livestream_with_no_duration_loses_to_a_known_length(self):
        # no duration is how a 24/7 broadcast looks: never offer it over something
        # whose length we can see and judge
        rows = self._search([{"id": "nolength", "title": "Chill Radio | live", "channel": "C"},
                             {"id": "known", "title": "Quiet Mix", "duration": 900}])
        self.assertEqual([r["id"] for r in rows], ["known"], rows)

    def test_a_half_hour_of_lofi_is_not_a_song_even_without_a_pipe(self):
        # the two shapes that got past every keyword: a long runtime, and a title
        # that sells an activity instead of naming a track
        rows = self._search([
            {"id": "lofi45", "title": "Soft Lofi Room - Chill Vibes for Peaceful Study",
             "duration": 2700},
            {"id": "long31", "title": "Quiet Morning", "duration": 1860},
            {"id": "song", "title": "Elephant", "duration": 180},
        ])
        self.assertEqual([r["id"] for r in rows], ["song"], rows)

    def test_thumbnail_is_carried_for_the_gui_tile(self):
        rows = self._search([{"id": "g", "title": "T", "duration": 200,
                              "thumbnail": "https://i.ytimg.com/vi/g/hqdefault.jpg"}])
        self.assertEqual(rows[0]["thumbnail"], "https://i.ytimg.com/vi/g/hqdefault.jpg")


def _ytm_payload(rows) -> dict:
    """Wrap (videoId, [column texts]) tuples in the InnerTube shape we parse."""
    out = []
    for vid, cols in rows:
        # YouTube nests the row *inside* its renderer key; the walker looks for
        # that key, so the fixture has to wrap it the same way
        row = {
            "playlistItemData": {"videoId": vid} if vid else {},
            "flexColumns": [{"musicResponsiveListItemFlexColumnRenderer":
                             {"text": {"runs": [{"text": c}]}}} for c in cols],
        }
        if vid:
            row["thumbnail"] = {"musicThumbnailRenderer": {"thumbnail": {"sources": [
                {"url": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"}]}}}
        out.append({"musicResponsiveListItemRenderer": row})
    return {"contents": {"sectionListRenderer": {"contents": [
        {"musicShelfRenderer": {"contents": out}}]}}}


class YtmSearchTests(unittest.TestCase):
    """
    providers.ytm_search is what "not even music" was about: YouTube Music's own
    search endpoint can only answer with music, so a fight scene from a movie or
    a 24/7 radio loop is not filtered out afterwards - it is never a candidate.
    """
    ROWS = [
        ("GOOD0000001", ["Smells Like Teen Spirit", "Song • Nirvana", "3B plays"]),
        ("LIVE0000001", ["Nirvana - Live at Reading (Full Concert)",
                         "Nirvana • 62M views • 1:58:31"]),
        ("COVER00001", ["Smells Like Teen Spirit [cover by Delicious Rock]",
                        "Delicious Rock • 115K views • 4:50"]),
        ("NODET00001", ["Heart-Shaped Box", "Song • Nirvana"]),
        ("NOID000001", ["Some Artist", "Artist • 12 subscribers"]),
    ]

    def _patch(self, rows=None, enabled=True):
        payload = _ytm_payload(rows if rows is not None else self.ROWS)
        return [mock.patch.dict(__import__("os").environ,
                                {"SPOTUBE_DJ_YTM": "" if enabled else "off"}),
                mock.patch.object(prov, "_http_json", return_value=payload)]

    def test_song_rows_are_recognised_and_tagged(self):
        p1, p2 = self._patch()
        with p1, p2:
            rows = prov.ytm_search("nirvana", limit=10)
        self.assertEqual([r["id"] for r in rows],
                         ["GOOD0000001", "LIVE0000001", "COVER00001",
                          "NODET00001", "NOID000001"])
        song = rows[0]
        self.assertTrue(song["official"], "a 'Song •' row is the artist's own recording")
        self.assertEqual(song["artist"], "Nirvana")
        self.assertEqual(song["channel"], "Nirvana")
        # the row's own artwork, with YouTube's signature stripped off (a signed
        # URL expires, and the thumbs cache keeps the URL it was fetched from)
        self.assertEqual(song["thumbnail"],
                         "https://i.ytimg.com/vi/GOOD0000001/mqdefault.jpg")

    def test_a_tiny_row_tile_gives_way_to_the_video_id(self):
        # 60/120 px tiles on a Song row are the artist avatar - the same URL for
        # every song by that artist - so they are not cover art
        def inner(*sizes):
            thumbs = [{"url": f"https://x/{s}.jpg?sqp=y", "width": s, "height": s}
                      for s in sizes]
            return {"thumbnail": {"musicThumbnailRenderer":
                                  {"thumbnail": {"thumbnails": thumbs}}}}

        row = {"playlistItemData": {"videoId": "abc123"}}
        row.update(inner(60, 120))
        self.assertEqual(prov._ytm_thumb(row, "abc123"),
                         "https://i.ytimg.com/vi/abc123/hqdefault.jpg")
        row = {"playlistItemData": {"videoId": "abc123"}}
        row.update(inner(60, 544))
        self.assertEqual(prov._ytm_thumb(row, "abc123"), "https://x/544.jpg")

    def test_the_artist_survives_a_row_that_only_says_song(self):
        # InnerTube sometimes puts "Song \u2022 5:02" in the visible column with
        # the name only in the play button's accessibility label, and an artist
        # of nothing is an artist a like cannot learn from
        label = "Play Come As You Are - Nirvana from Nevermind"
        node = {"label": label}
        for key in ("accessibilityData", "accessibilityPlayData",
                    "musicPlayButtonRenderer", "content",
                    "musicItemThumbnailOverlayRenderer", "overlay"):
            node = {key: node}
        self.assertEqual(prov._ytm_artist(node, "Come As You Are"), "Nirvana")
        # and a label that is only the title must not invent an artist
        node = {"label": "Play Nevermind"}
        for key in ("accessibilityData", "accessibilityPlayData",
                    "musicPlayButtonRenderer", "content",
                    "musicItemThumbnailOverlayRenderer", "overlay"):
            node = {key: node}
        self.assertEqual(prov._ytm_artist(node, "Nevermind"), "")

    def test_duration_and_views_come_out_of_the_subtitle(self):
        p1, p2 = self._patch()
        with p1, p2:
            rows = {r["id"]: r for r in prov.ytm_search("nirvana", limit=10)}
        self.assertEqual(rows["COVER00001"]["duration"], 290)
        self.assertEqual(rows["COVER00001"]["view_count"], 115_000)
        self.assertEqual(rows["NODET00001"]["duration"], 0)
        # a video row's subtitle is the *uploader*: Delicious Rock covered it, so
        # naming them the artist would be right here and dead wrong for a live
        # bootleg. The uploader goes on the row and the card; the *artist* field
        # is left empty rather than filled with the query, because "nirvana" as
        # an artist name is what a like would then teach the taste profile.
        self.assertEqual(rows["COVER00001"]["channel"], "Delicious Rock")
        self.assertEqual(rows["COVER00001"]["artist"], "")
        # ...and every row still carries a picture, which is what the panel needs
        self.assertTrue(rows["COVER00001"]["thumbnail"].startswith("http"),
                        rows["COVER00001"]["thumbnail"])

    def test_yt_search_prefers_the_music_surface_and_refuses_the_concert(self):
        p1, p2 = self._patch()
        with p1, p2, mock.patch.object(prov, "_run",
                                       side_effect=AssertionError("must not be used")) as run:
            out = prov.yt_search("nirvana", limit=5)
        self.assertNotIn("LIVE0000001", [t["id"] for t in out])
        self.assertIn("GOOD0000001", [t["id"] for t in out])
        self.assertEqual(run.call_count, 0)
        self.assertTrue(all(t["endpoint"] == "music-search" for t in out))
        self.assertTrue(all(t["source"] == "youtube-music" for t in out))

    def test_rows_without_a_video_id_are_not_tracks(self):
        # an artist card inside the same response: no playlistItemData, no song
        p1, p2 = self._patch([(None, ["Nirvana", "Artist \u2022 30M subscribers"])])
        with p1, p2, mock.patch.object(prov, "_run",
                                       side_effect=AssertionError("no net in tests")) as run:
            rows = prov.ytm_search("nirvana", limit=5)
        self.assertEqual(rows, [])
        self.assertEqual(run.call_count, 0)

    def test_falls_back_to_yt_dlp_when_the_endpoint_is_unusable(self):
        payload = json.dumps({"id": "OLD0000001", "title": "Nirvana - Come As You Are",
                              "channel": "Nirvana - Topic", "duration": 219})
        with mock.patch.object(prov, "_http_json", return_value=None), \
                mock.patch.object(prov, "_run", return_value=payload):
            out = prov.yt_search("nirvana", limit=5)
        self.assertEqual([t["id"] for t in out], ["OLD0000001"])
        self.assertEqual(out[0]["endpoint"], "ytsearch")

    def test_env_var_switches_the_surface_off(self):
        p1, p2 = self._patch(enabled=False)
        with p1, p2, mock.patch.object(prov, "_run", return_value="") as run:
            prov.yt_search("nirvana", limit=5)
        self.assertEqual(run.call_count, 1)

    def test_real_innerTube_response_parses(self):
        """The saved fixture is a genuine search response - if the walk stops
        finding rows, YouTube changed the tree and this is where we learn."""
        path = Path(tests.ROOT) / "tests" / "data" / "ytm-search.json"
        data = json.loads(path.read_text())
        with mock.patch.object(prov, "_http_json", return_value=data):
            rows = prov.ytm_search("nirvana smells like teen spirit", limit=20)
        self.assertGreaterEqual(len(rows), 10, "the parser found almost nothing")
        titles = [r["title"] for r in rows]
        self.assertTrue(any("Smells Like Teen Spirit" in t for t in titles), titles)
        self.assertTrue(all(r["id"] for r in rows))

    def test_walker_only_collects_the_row_renderer(self):
        data = {"x": [{"y": {"musicResponsiveListItemRenderer": {"a": 1}}},
                      {"musicResponsiveListItemRenderer": {"b": 2}}],
                "z": {"musicResponsiveListItemRenderer": {"c": 3}}}
        self.assertEqual(len(prov._ytm_rows(data)), 3)


class SmoothPlaybackTests(unittest.TestCase):
    """audiocache + the mixer, as the DJ uses them."""

    class Player:
        def __init__(self):
            self.urls = []
        def play_url(self, url):
            self.urls.append(url)
            return True
        def alive(self):
            return True
        def progress(self):
            return (10.0, 200.0)
        def volume(self, v):
            pass
        def quit(self):
            pass

    def _dj(self, n=4):
        d = DJ(backend="none")
        d.backend = "mpv"
        d.headless = False
        d.player = self.Player()
        d.request = ""
        d.info = {}
        d._resolve = lambda t: "http://stream/" + t["id"]
        d.add([{"id": f"x{i}", "title": f"t{i}", "artist": "a", "duration": 200,
                "url": f"u{i}", "query": "q"} for i in range(n)])
        return d

    def test_cached_track_starts_from_disk_without_resolving(self):
        import audiocache
        d = self._dj()
        d._topup = lambda **k: None
        hit = ("/home/x/.spotube-dj/audio/x0.m4a", "file:///home/x/.spotube-dj/audio/x0.m4a")
        with mock.patch.object(audiocache, "lookup", return_value=hit), \
             mock.patch.object(d, "_resolve",
                               side_effect=AssertionError("must not resolve")):
            ok, why = d._try_start({"id": "x0", "title": "t", "artist": "a"})
        self.assertTrue(ok, why)
        self.assertEqual(d.player.urls, [hit[1]])

    def test_uncached_track_still_resolves(self):
        import audiocache
        d = self._dj()
        d._topup = lambda **k: None
        with mock.patch.object(audiocache, "lookup", return_value=(None, None)):
            ok, why = d._try_start({"id": "x1", "title": "t", "artist": "a"})
        self.assertTrue(ok, why)
        self.assertEqual(d.player.urls, ["http://stream/x1"])

    def test_next_prefetches_the_following_tracks(self):
        import audiocache
        d = self._dj(5)
        d._topup = lambda **k: None
        with mock.patch.object(audiocache, "lookup", return_value=(None, None)), \
             mock.patch.object(audiocache, "prefetch") as pre:
            t = d.next()
        self.assertEqual(t["id"], "x0")
        pre.assert_called_once()
        ahead = pre.call_args[0][0]
        self.assertEqual([x["id"] for x in ahead], ["x1", "x2", "x3"])
        self.assertEqual(pre.call_args[1]["ahead"], 2)

    def test_a_topup_refills_and_never_repeats_a_queued_id(self):
        d = self._dj(3)
        d.request = "90s trip hop"
        d.seed_refs = None
        d.auto = False
        d.info = {"queries": []}
        fresh = [{"id": "x0", "title": "dup", "artist": "a", "duration": 200},
                 {"id": "new1", "title": "n", "artist": "a", "duration": 200}]
        before = len(d.queue)
        with mock.patch.object(dj_mod, "build_queue", return_value=(fresh, {"queries": ["q"]})):
            d._topup(keep=10)        # keep above the queue length, or it stays put
        ids = [t["id"] for t in d.queue.items[d.queue.pos:]]
        self.assertNotIn("x0", ids[1:], ids)       # the already-queued one not doubled
        self.assertIn("new1", ids)
        self.assertEqual(len(d.queue), before + 1)

    def test_force_topup_works_even_with_a_full_queue(self):
        # the daemon's "topup" action used to call _topup(0), which the length
        # guard turned into a silent no-op: pressing it did nothing at all
        d = self._dj(3)
        d.request = "x"
        d.seed_refs = None
        d.auto = False
        d.info = {}
        seen = []
        def fake_build(*a, **k):
            seen.append(k)
            return ([{"id": "brandnew", "title": "n", "artist": "a", "duration": 200}],
                    {"queries": ["q"]})
        before = len(d.queue)
        with mock.patch.object(dj_mod, "build_queue", side_effect=fake_build):
            d._topup(force=True)              # "top up now", whatever the length
            d._topup(keep=1)                  # already at least one -> no search
        self.assertEqual(len(seen), 1, "only the forced call should search")
        self.assertEqual(len(d.queue), before + 1)

    def test_auto_mix_asks_taste_and_marks_the_rows(self):
        rows = [{"id": "m1", "title": "One", "artist": "Björk", "duration": 200},
                {"id": "m2", "title": "Two", "artist": "Björk", "duration": 200},
                {"id": "m3", "title": "Three", "artist": "Portishead", "duration": 200}]
        d = self._dj(2)
        with mock.patch.object(taste, "next_queries", return_value=["bjork best songs"]) as nq, \
                mock.patch.object(prov, "yt_search", return_value=rows) as search, \
                mock.patch.object(taste, "score_tracks", side_effect=lambda x, **k: x):
            picked = d._auto_mix(keep=6)
        nq.assert_called_once()
        self.assertEqual(search.call_args[0][0], "bjork best songs")
        self.assertEqual([t["id"] for t in picked], ["m1", "m3"],
                         "one track per artist, or the mix becomes that artist only")
        self.assertTrue(all(t["mixed"] and t["query"] == "bjork best songs" for t in picked))

    def test_a_query_is_never_reissued(self):
        rows = [{"id": "m1", "title": "One", "artist": "Bj\u00f6rk", "duration": 200}]
        d = self._dj(2)
        d._mix_used = set()
        calls = []

        def fake_next(avoid=None, limit=3):
            calls.append(list(avoid or []))
            return [] if "bjork best songs" in (avoid or []) else ["bjork best songs"]

        with mock.patch.object(taste, "next_queries", side_effect=fake_next), \
                mock.patch.object(prov, "yt_search", return_value=rows), \
                mock.patch.object(taste, "score_tracks", side_effect=lambda x, **k: x):
            first = d._auto_mix(keep=6)
            second = d._auto_mix(keep=6)
        self.assertEqual([t["id"] for t in first], ["m1"])
        self.assertEqual(second, [], "the same query must not be searched twice")
        self.assertIn("bjork best songs", calls[1], calls)

    def test_auto_mix_off_means_no_extra_searches(self):
        d = self._dj(2)
        d.auto = False
        d.request = "x"
        d.info = {}
        with mock.patch.object(dj_mod, "build_queue", return_value=([], {})), \
                mock.patch.object(d, "_auto_mix",
                                  side_effect=AssertionError("must not mix")):
            d._topup(keep=1)

    def test_search_failure_does_not_break_the_topup(self):
        d = self._dj(2)
        d._topup_hook = None
        with mock.patch.object(taste, "next_queries", return_value=["q1"]), \
                mock.patch.object(prov, "yt_search", side_effect=RuntimeError("blocked")):
            self.assertEqual(d._auto_mix(keep=6), [])
        self.assertIn("q1", d._mix_used)


class StatusCacheFieldsTests(unittest.TestCase):
    def test_upnext_rows_say_what_is_already_downloaded(self):
        import audiocache
        d = DJ(backend="none")
        d.headless = True
        d.add([{"id": "cached1", "title": "a", "artist": "b", "duration": 200},
               {"id": "not1", "title": "c", "artist": "d", "duration": 200}])
        with mock.patch.object(audiocache, "path_for",
                              side_effect=lambda vid: "/x/1.m4a" if vid == "cached1" else None), \
             mock.patch.object(audiocache, "brief", return_value=(0, 0)):
            st = d.status()
        self.assertEqual([t["cached"] for t in st["up_next"]], [True, False])


class QueueOverlapTests(unittest.TestCase):
    """
    A top-up issues a *different* search, so it can bring back a song that is
    already queued as another video. Deduping on the id alone misses that and
    the listener sees the same title twice in five rows.
    """

    def _dj(self, titles):
        d = DJ(backend="none")
        d.request = ""
        d.info = {}
        d.add([{"id": f"x{i}", "title": t, "artist": "a", "duration": 200,
                "url": f"u{i}", "query": "q"} for i, t in enumerate(titles)])
        return d

    def test_a_topup_drops_another_video_of_a_queued_song(self):
        d = self._dj(["Touch Me Softly"])
        fresh = d._fresh([{"id": "other1", "title": "Touch Me Softly", "artist": "a"},
                          {"id": "other2", "title": "Something Else", "artist": "a"}])
        self.assertEqual([t["id"] for t in fresh], ["other2"])

    def test_the_title_of_the_playing_track_blocks_it_too(self):
        d = self._dj(["Alpha Song"])
        d.current = {"id": "now",
                     "title": "Lowdii Beats - Touch Me Softly (Official Audio)"}
        self.assertEqual(d._fresh([{"id": "z", "title": "Touch Me Softly",
                                    "artist": "Lowdii Beats"}]), [])

    def test_taste_queries_lose_a_dangling_preposition(self):
        # the tag came out of the request "lofi beats to relax", so it ended in
        # "to"; "lofi beats to music" is a query no upload answers to
        before = taste.load_state()
        self.addCleanup(taste.save_state, before)
        taste.save_state({"artists": {}, "genres": {"lofi beats to": 1.0},
                          "liked": [], "skipped": [], "last_request": ""})
        self.assertEqual(taste.next_queries(avoid=[], limit=2), ["lofi beats"])

    def test_a_tag_that_only_repeats_the_request_is_not_searched_again(self):
        # "lofi beats to" cleans to "lofi beats", which is the query the request
        # already used; re-running it is a wasted search and the same eight songs
        before = taste.load_state()
        self.addCleanup(taste.save_state, before)
        taste.save_state({"artists": {}, "genres": {"lofi beats to": 1.0},
                          "liked": [], "skipped": [], "last_request": ""})
        self.assertEqual(taste.next_queries(avoid=["lofi beats"], limit=2), [])

    def test_the_nothing_liked_yet_fallback_searches_a_real_query(self):
        before = taste.load_state()
        self.addCleanup(taste.save_state, before)
        taste.save_state({"artists": {}, "genres": {}, "liked": [], "skipped": [],
                          "last_request": "lofi beats to relax for studying"})
        self.assertEqual(taste.next_queries(avoid=[], limit=2), ["lofi beats"])

    def test_a_plain_mood_tag_still_gets_a_noun(self):
        before = taste.load_state()
        self.addCleanup(taste.save_state, before)
        taste.save_state({"artists": {}, "genres": {"sad piano for": 1.0},
                          "liked": [], "skipped": [], "last_request": ""})
        self.assertEqual(taste.next_queries(avoid=[], limit=2), ["sad piano music"])


class SelfFillTests(_TmpHome):
    """
    "The queue stays empty unless I type something and press Play" and "radio
    doesn't work" were the same bug seen twice: `DJ` had two entry points that
    both required a typed request, and the skins each carried their own copy of
    the station code. These tests pin the contract the fix shipped with - the
    queue refills itself from the profile, and every engine answer arrives as a
    sentence through `DJ.progress`.
    """

    def _dj(self, **kw):
        d = DJ(backend="none", headless=True, **kw)
        d.queue = Queue()
        return d

    @staticmethod
    def _row(i, artist="Someone"):
        return {"id": f"v{i}", "title": f"Song {i}", "artist": artist, "duration": 210,
                "url": f"https://music.youtube.com/watch?v=v{i}"}

    def _fake_build(self, rows, ret=None):
        """A stand-in for build_queue that records how it was asked."""
        seen = {}

        def fake(request, **kw):
            # first call wins: a refill triggered by the playback this starts also
            # goes through build_queue, and the question here is what the *mix* asked
            if "request" not in seen:
                seen["request"] = request
                seen["kw"] = kw
            return (ret if ret is not None else rows), {"engine": "offline", "why": "",
                                                         "queries": [request],
                                                         "candidates": len(rows),
                                                         "searched": 1,
                                                         "off_topic_filtered": 0,
                                                         "llm_notes": [], "llm_error": ""}
        fake.seen = seen
        return fake

    # ------------------------------------------------------------------ top-up
    def test_topup_refills_from_the_last_request_without_a_typed_one(self):
        # the root cause of the empty queue: `if not self.request: return`
        config.touch_last_request("90s trip hop")
        dj = self._dj()
        dj.request = ""
        fake = self._fake_build([self._row(i) for i in range(5)])
        with mock.patch.object(dj_mod, "build_queue", fake):
            dj._topup(force=True)
        self.assertEqual(len(dj.queue.items), 5, "the queue did not refill")
        self.assertEqual(fake.seen["request"], "90s trip hop", "last_request not used")
        self.assertIn("queue topped up: +5 tracks", dj.log[-1])

    def test_topup_without_a_request_or_a_profile_says_why(self):
        dj = self._dj()
        calls = []
        with mock.patch.object(dj_mod, "build_queue",
                               lambda *a, **k: (calls.append(1) or ([], {}))):
            dj._topup(force=True)
        self.assertEqual(calls, [], "a search ran with nothing to search for")
        self.assertEqual(dj.queue.items, [])
        self.assertIn("press the heart on a song", dj.log[-1])

    def test_topup_that_finds_only_heard_music_says_that(self):
        config.touch_last_request("post rock")
        dj = self._dj()
        dj.queue.items = [self._row(1)]
        dj.queue.pos = 0
        fake = self._fake_build([self._row(1)])
        with mock.patch.object(dj_mod, "build_queue", fake):
            dj._topup(force=True)
        self.assertEqual(len(dj.queue.items), 1, "a duplicate leaked into the queue")
        self.assertIn("no new tracks to add", dj.log[-1])

    def test_auto_mix_rows_land_inside_the_set_not_behind_it(self):
        # appended, the "learned from your likes" rows were 15-20 deep and the
        # listener concluded the heart button did nothing
        config.touch_last_request("trip hop")
        dj = self._dj()
        dj.auto = True
        plain = [self._row(i) for i in range(9)]
        mixed = [dict(self._row(100 + i), artist="Loved Band", mixed=True) for i in range(3)]
        fake = self._fake_build(plain)

        def auto(keep=12):
            return mixed
        dj._auto_mix = auto                                # type: ignore[assignment]
        with mock.patch.object(dj_mod, "build_queue", fake):
            dj._topup(force=True)
        got = dj.queue.items
        self.assertEqual(len(got), 12)
        first_mixed = min(i for i, t in enumerate(got) if t.get("mixed"))
        self.assertLess(first_mixed, 5, f"taste picks buried at {first_mixed}")
        self.assertIn("(3 from what you like)", dj.log[-1])

    # -------------------------------------------------------------- taste_mix
    def test_taste_mix_needs_no_words_and_seeds_the_planner(self):
        taste.record_like({"title": "Glory Box", "artist": "Portishead", "duration": 300})
        dj = self._dj()
        rows = [self._row(i, artist="Go Team") for i in range(4)]
        fake = self._fake_build(rows)
        with mock.patch.object(dj_mod, "build_queue", fake):
            r = dj.taste_mix(count=9)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(dj.queue.items), 4)
        self.assertIn("songs like", fake.seen["request"])
        self.assertEqual(fake.seen["kw"]["seeds"][0]["title"], "Glory Box",
                         "the loved record never reached the planner as a seed")
        self.assertEqual(fake.seen["kw"]["count"], 9)
        self.assertIn("mixing from your likes: 1 loved record", dj.log[0])
        self.assertIn("mix ready: 4 tracks from what you like", dj.log[1],
                      "a mix that filled the queue never said so")

    def test_taste_mix_without_a_profile_is_an_answer_not_a_failure(self):
        dj = self._dj()
        calls = []
        with mock.patch.object(dj_mod, "build_queue",
                               lambda *a, **k: (calls.append(1) or ([], {}))):
            r = dj.taste_mix()
        self.assertEqual(calls, [], "an empty profile still went searching")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no likes yet")
        self.assertIn("press the heart", dj.log[-1])

    def test_taste_mix_that_leads_nowhere_says_so(self):
        taste.record_like({"title": "Known", "artist": "Known Artist", "duration": 200})
        dj = self._dj()
        dj.queue.items = [self._row(7, artist="Known Artist")]
        dj.queue.pos = 0
        fake = self._fake_build([self._row(7, artist="Known Artist")])
        with mock.patch.object(dj_mod, "build_queue", fake):
            r = dj.taste_mix()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "nothing new found")
        self.assertIn("already heard", dj.log[-1])

    def _dj_with_player(self):
        # backend "none" *implies* headless (see DJ.__init__), and headless is the
        # one thing that must not touch playback - so this test needs a DJ that
        # believes it has a player without needing libmpv installed
        d = DJ(backend="mpv", headless=False)
        d.headless = False
        d.queue = Queue()
        d.player = object()
        return d

    def test_a_mix_starts_playback_only_when_nothing_is_playing(self):
        taste.record_like({"title": "G", "artist": "Portishead", "duration": 300})
        d = self._dj_with_player()
        for current, expected in ((None, 1), (self._row(1), 0)):
            d.current = current
            d.queue.items, d.queue.pos = [], 0
            fake = self._fake_build([self._row(50 + len(d.log))])
            with mock.patch.object(dj_mod, "build_queue", fake), \
                    mock.patch.object(d, "next") as nx:      # no real playback in a test
                r = d.taste_mix()
            self.assertTrue(r["ok"], r)
            self.assertEqual(nx.call_count, expected,
                             f"current={current!r} advanced {nx.call_count} times")
            if current is None:
                nx.assert_called_once_with(force=True)

    def test_notes_reach_the_skin_hook(self):
        dj = self._dj()
        got = []
        dj.progress = got.append
        dj._note("mixing from what you like")
        self.assertEqual(got, ["mixing from what you like"])

    def test_a_broken_sink_never_breaks_the_player(self):
        dj = self._dj()

        def boom(msg):
            raise RuntimeError("window closed")
        dj.progress = boom
        dj._note("still fine")
        self.assertIn("still fine", dj.log[-1])

    # ------------------------------------------------------------------ radio
    def test_radio_from_seeds_the_row_and_starts_when_idle(self):
        d = self._dj_with_player()
        row = self._row(3, artist="Portishead")
        rows = [self._row(i, artist=f"Similar {i}") for i in range(6)]
        fake = self._fake_build(rows)
        with mock.patch.object(dj_mod, "build_queue", fake), \
                mock.patch.object(d, "next") as nx:          # no real playback in a test
            r = d.radio_from(row, count=11)
        self.assertTrue(r["ok"], r)
        self.assertEqual(fake.seen["kw"]["seeds"][0]["title"], "Song 3",
                         "the station was not built around the row that was clicked")
        self.assertIn("more like Portishead - Song 3", fake.seen["request"])
        self.assertEqual(fake.seen["kw"]["count"], 11)
        self.assertEqual(d.station, "Portishead - Song 3")
        self.assertIn("station ready: 6 tracks around Portishead - Song 3", d.log[-1])
        nx.assert_called_once_with(force=True)      # idle -> the station starts

    def test_radio_from_a_bare_row_answers_instead_of_searching(self):
        dj = self._dj()
        calls = []
        with mock.patch.object(dj_mod, "build_queue",
                               lambda *a, **k: (calls.append(1) or ([], {}))):
            r = dj.radio_from({})
        self.assertEqual(calls, [])
        self.assertFalse(r["ok"])
        self.assertIn("no song to build from", r["reason"])

    def test_radio_from_survives_a_planner_that_raises(self):
        dj = self._dj()

        def boom(request, **kw):
            raise RuntimeError("provider exploded")
        with mock.patch.object(dj_mod, "build_queue", boom):
            r = dj.radio_from(self._row(1))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "RuntimeError")
        self.assertIn("station could not be built: RuntimeError: provider exploded",
                      dj.log[-1])

    def test_a_station_is_sparser_than_a_top_up(self):
        # a station is "more like this one song", so it caps at two per artist;
        # a refill of a whole set tolerates three
        caps = []
        real = dj_mod._spread

        def spy(rows, cap=3):
            caps.append(cap)
            return real(rows, cap=cap)
        dj = self._dj()
        with mock.patch.object(dj_mod, "build_queue",
                               self._fake_build([self._row(i) for i in range(6)])), \
                mock.patch.object(dj_mod, "_spread", spy):
            dj.radio_from(self._row(90, artist="Seed Artist"), count=6)
        self.assertEqual(caps[-1], 2, "the station lost its tighter cap")

    def test_a_station_becomes_the_mood_when_there_was_none(self):
        dj = self._dj()
        fake = self._fake_build([self._row(1)])
        with mock.patch.object(dj_mod, "build_queue", fake):
            dj.radio_from(self._row(0, artist="X"))
        self.assertIn("more like", dj.request)
        self.assertEqual(dj.status()["station"], "X - Song 0")
        dj.request = "existing mood"
        with mock.patch.object(dj_mod, "build_queue", self._fake_build([self._row(2)])):
            dj.radio_from(self._row(3, artist="Y"))
        self.assertEqual(dj.request, "existing mood", "a station overwrote the request")

    # --------------------------------------------------------------- helpers
    def test_spread_keeps_everything_but_orders_it(self):
        rows = [self._row(i, artist="One Band") for i in range(9)] + \
               [self._row(50 + i, artist="Other") for i in range(2)]
        out = dj_mod._spread(rows, cap=3)
        self.assertEqual(len(out), 11, "rows were dropped instead of pushed back")
        self.assertEqual(sum(1 for t in out[:4] if t["artist"] == "One Band"), 3)
        self.assertEqual([t["artist"] for t in out],
                         ["One Band"] * 3 + ["Other"] * 2 + ["One Band"] * 6)

    def test_weave_without_taste_rows_is_a_no_op(self):
        rows = [self._row(i) for i in range(4)]
        self.assertEqual(dj_mod._weave(rows, []), rows)

    def test_auto_mix_stops_at_its_budget_and_caps_by_weight(self):
        st = taste.load_state()
        st["artists"] = {"heavy band": 8.0, "light band": 1.0}
        st["liked"] = [{"title": "H", "artist": "Heavy Band", "display_title": "H",
                        "display_artist": "Heavy Band"}]
        config.save_state(st)
        dj = self._dj()
        dj.auto = True
        qs = ["q1", "q2", "q3", "q4"]
        calls = []

        def fake_search(q, limit=8, min_dur=60, max_dur=900):
            calls.append(q)
            return [{"id": f"{q}-{i}", "title": f"Track {q} {i}",
                     "artist": ("Heavy Band" if i % 2 == 0 else "Light Band"),
                     "duration": 200, "url": "u"} for i in range(6)]

        with mock.patch.object(taste, "next_queries", return_value=qs), \
                mock.patch.object(dj_mod.prov, "yt_search", fake_search):
            out = dj._auto_mix(keep=12)
        self.assertLessEqual(len(calls), 4, "one query per search it ever planned")
        # four queries all offering the same two names: the cap, not the count, is
        # what decides here, so the mix is exactly 3 + 1 rows wide
        self.assertEqual(sum(1 for t in out if t["artist"] == "Heavy Band"), 3)
        self.assertEqual(sum(1 for t in out if t["artist"] == "Light Band"), 1)
        heavy = sum(1 for t in out if t["artist"] == "Heavy Band")
        light = sum(1 for t in out if t["artist"] == "Light Band")
        # the cap is for the whole refill, not per query: 3 rows of an artist per
        # search over 4 searches is still "12 songs by the last thing you loved"
        self.assertLessEqual(heavy, 3, "an artist loved 4x took over the mix")
        self.assertLessEqual(light, 2, "a lightly-liked artist got the same room "
                                       "as a heavily-liked one")
        self.assertGreater(heavy, light, "weights did not change the caps")
        self.assertTrue(all(t.get("mixed") for t in out))

    def test_auto_mix_stops_when_one_query_was_enough(self):
        st = taste.load_state()
        st["artists"] = {"a band": 4.0}
        st["liked"] = [{"title": "T", "artist": "A Band", "display_title": "T",
                        "display_artist": "A Band"}]
        config.save_state(st)
        dj = self._dj()
        dj.auto = True
        calls = []

        def fake_search(q, limit=8, min_dur=60, max_dur=900):
            calls.append(q)
            # 6 different artists, so nothing is capped and one query fills the bill
            return [{"id": f"{q}-{i}", "title": f"Track {q} {i}", "artist": f"Band {i}",
                     "duration": 200, "url": "u"} for i in range(6)]

        with mock.patch.object(taste, "next_queries", return_value=["q1", "q2", "q3"]), \
                mock.patch.object(dj_mod.prov, "yt_search", fake_search):
            out = dj._auto_mix(keep=12)
        self.assertEqual(len(calls), 1, "it searched three ways for a six-track refill")
        self.assertEqual(len(out), 6)

    def test_auto_mix_reports_a_query_that_failed(self):
        st = taste.load_state()
        st["artists"] = {"a band": 4.0}
        st["liked"] = [{"title": "T", "artist": "A Band", "display_title": "T",
                        "display_artist": "A Band"}]
        config.save_state(st)
        dj = self._dj()
        dj.auto = True

        def fake_search(q, limit=8, min_dur=60, max_dur=900):
            raise OSError("429")
        with mock.patch.object(taste, "next_queries", return_value=["one q"]), \
                mock.patch.object(dj_mod.prov, "yt_search", fake_search):
            out = dj._auto_mix(keep=6)
        self.assertEqual(out, [])
        self.assertIn("auto-mix: 'one q' failed (OSError)", dj.log[-1])

    def test_run_keeps_the_queue_full_when_somebody_else_plays(self):
        # --daemon used to play one track and idle forever: no player means no
        # advancing, but the refill must still happen
        dj = self._dj()
        dj.current = self._row(0)
        hits = []
        dj._topup = lambda keep=12, force=False: hits.append(1)   # type: ignore
        t = threading.Thread(target=dj.run, daemon=True)
        t.start()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not hits:
            time.sleep(0.05)
        dj.stop()
        t.join(3)
        self.assertTrue(hits, "the loop never topped the queue up")
        self.assertFalse(t.is_alive(), "run() did not return after stop()")

    # -------------------------------------------------------------- forget it
    def test_forget_taste_reports_and_resyncs(self):
        taste.record_like({"title": "Old", "artist": "Old Band", "duration": 200})
        taste.record_skip({"title": "Bad", "artist": "Bad Band", "duration": 200})
        dj = self._dj()
        dj.state = config.load_state()
        self.assertEqual(len(dj.state["liked"]), 1)
        gone = dj.forget_taste()
        self.assertEqual(gone["liked"], 1)
        self.assertEqual(gone["skipped"], 1)
        self.assertEqual(dj.state["liked"], [], "the DJ still holds the old profile")
        # two artist bumps: record_like and record_skip each touch one
        self.assertIn("taste cleared: 1 loved, 1 refused, 2 artists and 0 tags forgotten",
                      dj.log[-1])

    def test_forget_taste_leaves_the_settings_alone(self):
        config.touch_last_request("some mood")
        dj = self._dj()
        gone = dj.forget_taste()
        self.assertEqual(sum(gone.values()), 0)
        st = config.load_state()
        self.assertEqual(st["last_request"], "some mood", "clearing taste lost the mood")
        self.assertIn("taste cleared: 0 loved, 0 refused", dj.log[-1])


class StationRefillTests(_TmpHome):
    """
    "if i station the queue for certain music, the next queue must be similar or
    related to the vibes, not general vibes." A station refill used to ask the
    *previous* typed request plus the whole-profile auto-mix, so a "Portishead -
    Roads" station drifted back to "sad lofi" and the library's general sound.
    The refill must stay on the seed while the station is live.
    """

    def _dj(self):
        d = DJ(backend="none", headless=True)
        d.request = ""
        d.info = {}
        return d

    @staticmethod
    def _row(i, artist="Someone"):
        return {"id": f"v{i}", "title": f"Song {i}", "artist": artist, "duration": 210,
                "url": f"u{i}"}

    def test_a_station_refills_toward_the_seed_not_the_last_request(self):
        d = self._dj()
        d.station = "Portishead - Roads"
        d.station_seed = {"title": "Roads", "artist": "Portishead"}
        d.request = "sad lofi"          # stale: the mood that was typed before the station
        d.auto = False
        got = {}
        def fake(request, **kw):
            got["request"], got["kw"] = request, kw
            return ([self._row(1, "Portishead")], {"queries": ["portishead top songs"]})
        with mock.patch.object(dj_mod, "build_queue", fake):
            d._topup(force=True)
        self.assertIn("more like Portishead - Roads", got["request"], got)
        self.assertEqual(got["kw"]["seeds"][0]["title"], "Roads",
                         "the seed never reached the station's refill")
        self.assertNotIn("sad lofi", got["request"],
                         "the stale previous mood hijacked the station")
        self.assertEqual([t["id"] for t in d.queue.items], ["v1"])

    def test_auto_mix_prefers_station_queries_over_the_profile(self):
        d = self._dj()
        d.station = "Nirvana - Come as You Are"
        d.station_seed = {"title": "Come as You Are", "artist": "Nirvana"}
        rows = [{"id": "m1", "title": "Breed", "artist": "Nirvana", "duration": 200},
                {"id": "m2", "title": "S/T", "artist": "Some", "duration": 200}]
        with mock.patch.object(d, "_station_queries",
                               return_value=["radio Nirvana"]) as sq, \
                mock.patch.object(taste, "next_queries",
                                  side_effect=AssertionError("the profile leaked in")) as nq, \
                mock.patch.object(prov, "yt_search", return_value=rows), \
                mock.patch.object(taste, "score_tracks", side_effect=lambda x, **k: x):
            picked = d._auto_mix(keep=6)
        sq.assert_called_once()
        nq.assert_not_called()
        self.assertTrue(picked, "a station should still auto-fill")
        self.assertEqual(picked[0]["query"], "radio Nirvana")

    def test_a_typed_search_ends_the_station(self):
        # a new mood is its own vibe; the old station's seed must not steer it
        d = self._dj()
        d.station = "Portishead - Roads"
        d.station_seed = {"title": "Roads", "artist": "Portishead"}
        fake = lambda request, **kw: ([self._row(1)], {"queries": ["q"], "engine": "",
                                     "why": "", "candidates": 1, "searched": 1,
                                     "off_topic_filtered": 0, "llm_notes": [], "llm_error": ""})
        with mock.patch.object(dj_mod, "build_queue", fake):
            d.start("high energy")
        self.assertEqual(d.station, "")
        self.assertIsNone(d.station_seed)

    def test_make_a_mix_ends_the_station(self):
        d = self._dj()
        d.station = "Portishead - Roads"
        d.station_seed = {"title": "Roads", "artist": "Portishead"}
        taste.record_like({"title": "Glory Box", "artist": "Portishead", "duration": 300})
        fake = lambda request, **kw: ([self._row(1)], {"queries": ["q"], "engine": "",
                                     "why": "", "candidates": 1, "searched": 1,
                                     "off_topic_filtered": 0, "llm_notes": [], "llm_error": ""})
        with mock.patch.object(dj_mod, "build_queue", fake):
            d.taste_mix(count=9)
        self.assertEqual(d.station, "")
        self.assertIsNone(d.station_seed)

    def test_station_queries_widen_and_never_repeat(self):
        d = self._dj()
        d.station_seed = {"title": "Roads", "artist": "Portishead"}
        d._mix_used = {"portishead essential songs"}
        qs = d._station_queries(limit=4)
        self.assertTrue(qs)
        self.assertNotIn("portishead essential songs", [q.lower() for q in qs])
        self.assertIn("radio portishead", [q.lower() for q in qs])
        self.assertTrue(any("more like roads" in q.lower() for q in qs))

    def test_station_with_no_seed_falls_back_to_the_profile(self):
        # an obscure one-off with no artist on the seed must still fill, not stall
        d = self._dj()
        d.station = "untitled ambient"
        d.station_seed = {"title": "untitled", "artist": ""}
        with mock.patch.object(d, "_station_queries", return_value=[]) as sq, \
                mock.patch.object(taste, "next_queries",
                                  return_value=["ambient music"]) as nq, \
                mock.patch.object(prov, "yt_search",
                                  return_value=[{"id": "m1", "title": "A", "artist": "X",
                                                 "duration": 200}]), \
                mock.patch.object(taste, "score_tracks", side_effect=lambda x, **k: x):
            picked = d._auto_mix(keep=6)
        sq.assert_called_once()
        nq.assert_called_once()
        self.assertEqual(picked[0]["artist"], "X")


class VibeSubstitutionTests(_TmpHome):
    """
    "each search should be a genuinely distinct mix" - the reason a second search
    used to sound like a blend of the last two is that `start()` *appended* to the
    queue, so the rows of the earlier vibe were still sitting after the cursor. These
    tests pin the contract: a new request replaces the previous one's queued rows
    (never the audible song), the same request again refines it instead of clearing,
    and a search that finds nothing never wipes a queue someone is still enjoying.
    """

    def _dj(self, **kw):
        d = DJ(backend="none", headless=True, **kw)
        d.queue = Queue()
        return d

    @staticmethod
    def _row(i, **kw):
        base = {"id": f"v{i}", "title": f"Song {i}", "artist": "Someone",
                "duration": 210, "url": f"https://music.youtube.com/watch?v=v{i}"}
        base.update(kw)
        return base

    def _fake_build(self, rows, engine="offline"):
        seen = {"calls": []}

        def fake(request, **kw):
            seen["calls"].append(request)
            return list(rows), {"engine": engine, "why": "", "queries": [request],
                                "candidates": len(rows), "searched": 1,
                                "off_topic_filtered": 0, "llm_notes": [],
                                "llm_error": "", "avoid": []}
        fake.seen = seen
        return fake

    def test_a_new_search_replaces_the_previous_vibes_queued_rows(self):
        dj = self._dj()
        # first search plays and leaves a queue; the audible row is the "now"
        with mock.patch.object(dj_mod, "build_queue",
                               self._fake_build([self._row(i) for i in range(3)])):
            dj.start("sad lofi", count=6, on_progress=lambda *_: None)
        dj.queue.pop()                      # simulate the first row playing
        first_ids = [t["id"] for t in dj.queue.items[dj.queue.pos:]]
        self.assertGreater(len(first_ids), 0, "the first search queued nothing")

        with mock.patch.object(dj_mod, "build_queue",
                               self._fake_build([self._row(200, artist="High Energy")])):
            dj.start("high energy", count=6, on_progress=lambda *_: None)

        remaining = [t["id"] for t in dj.queue.items[dj.queue.pos:]]
        self.assertEqual(remaining, [self._row(200)["id"]],
                         f"the old vibe survived: {remaining}")
        self.assertNotIn(first_ids[0], remaining,
                         "a row from the previous search was still queued")

    def test_a_new_search_never_touches_the_audible_row_or_history(self):
        dj = self._dj()
        with mock.patch.object(dj_mod, "build_queue",
                               self._fake_build([self._row(i) for i in range(3)])):
            dj.start("sad lofi", count=6, on_progress=lambda *_: None)
        playing = dj.queue.pop()            # what is audible
        with mock.patch.object(dj_mod, "build_queue", self._fake_build([self._row(300)])):
            dj.start("loud rock", count=6, on_progress=lambda *_: None)
        # the audible row is before the cursor now (history), and the clear only
        # ever touched rows *after* it - so it is still there
        self.assertEqual(dj.queue.items[0]["id"], playing["id"],
                         "the audible track was removed by the new search")
        self.assertLessEqual(len(dj.queue.items), 2,
                             "history rows or the audible row were cleared")

    def test_searching_the_same_request_again_refines_instead_of_clearing(self):
        dj = self._dj()
        fake = self._fake_build([self._row(i) for i in range(3)])
        with mock.patch.object(dj_mod, "build_queue", fake):
            dj.start("sad lofi", count=6, on_progress=lambda *_: None)
        first_count = len(dj.queue.items)
        with mock.patch.object(dj_mod, "build_queue", fake):
            dj.start("sad lofi", count=6, on_progress=lambda *_: None)
        self.assertEqual(len(dj.queue.items), first_count + 3,
                         "re-searching the same phrase should widen that vibe")

    def test_a_search_that_finds_nothing_never_wipes_the_queue(self):
        dj = self._dj()
        with mock.patch.object(dj_mod, "build_queue",
                               self._fake_build([self._row(i) for i in range(3)])):
            dj.start("sad lofi", count=6, on_progress=lambda *_: None)
        before = len(dj.queue.items)
        with mock.patch.object(dj_mod, "build_queue", self._fake_build([])):
            r = dj.start("obscure rabbit hole", count=6, on_progress=lambda *_: None)
        self.assertFalse(r["ok"])
        self.assertEqual(len(dj.queue.items), before,
                         "a dead search cleared the previous mix")

    def test_a_make_a_mix_replaces_the_previous_searches_queued_rows(self):
        taste.record_like({"title": "Glory Box", "artist": "Portishead", "duration": 300})
        dj = self._dj()
        with mock.patch.object(dj_mod, "build_queue",
                               self._fake_build([self._row(i) for i in range(3)])):
            dj.start("sad lofi", count=6, on_progress=lambda *_: None)
        # the fact that the request was typed does not mean a mix keeps its rows
        fresh = [self._row(i, artist="Portishead") for i in range(500, 504)]
        with mock.patch.object(dj_mod, "build_queue", self._fake_build(fresh)):
            r = dj.taste_mix(count=9)
        self.assertTrue(r["ok"])
        remaining = [t["id"] for t in dj.queue.items[dj.queue.pos:]]
        self.assertGreater(len(remaining), 0)
        self.assertTrue(all(tid.startswith("v5") for tid in remaining),
                        f"a mix kept the old search's rows: {remaining}")


class HardeningTests(_TmpHome):
    """
    The things you only notice by poking at a running app with one hand: a list
    that never stops growing, a button pressed twice, a song liked four times, a
    destructive verb that takes no asking.
    """

    def test_the_log_stops_growing(self):
        d = DJ(backend="none", headless=True)
        for i in range(d.LOG_LINES + 120):
            d._note(f"noise {i}")
        self.assertEqual(len(d.log), d.LOG_LINES, "the week-long daemon ate RAM")
        self.assertIn("noise 519", d.log[-1])
        self.assertEqual(len(d.log[-40:]), 40, "the slice the skins use broke")

    def test_a_repeated_like_is_one_row_that_moves_to_the_front(self):
        for i in range(4):
            taste.record_like({"title": "Angel", "artist": "Massive Attack",
                               "duration": 200})
        taste.record_like({"title": "Other", "artist": "Other Band", "duration": 200})
        taste.record_like({"title": "Angel", "artist": "Massive Attack", "duration": 200})
        st = taste.load_state()
        self.assertEqual([r["title"] for r in st["liked"]], ["other", "angel"],
                         f"the loved list grew copies: {st['liked']}")
        self.assertEqual(st["artists"]["massive attack"], 10.0,
                         "the hearts stopped counting, which is not the same thing")

    def test_a_repeated_skip_is_kept(self):
        for _ in range(3):
            taste.record_skip({"title": "Bad", "artist": "Bad Band"}, reason="early-skip")
        self.assertEqual(len(taste.load_state()["skipped"]), 3,
                         "a skip that keeps happening is information")

    def test_clear_leaves_one_snapshot_and_restore_puts_it_back(self):
        taste.record_like({"title": "Undo Me", "artist": "Undo Band", "duration": 200})
        taste.record_skip({"title": "Nope", "artist": "Nope Band", "duration": 200})
        self.assertFalse(taste.has_backup(), "an undo file appeared out of nowhere")
        gone = taste.clear()
        self.assertEqual(gone["liked"], 1)
        self.assertTrue(taste.has_backup())
        back = taste.restore()
        self.assertEqual(back["liked"], 1)
        st = taste.load_state()
        self.assertEqual([r["title"] for r in st["liked"]], ["undo me"])
        self.assertEqual(st["artists"]["undo band"], 2.0)
        self.assertEqual(st["skipped"][0]["title"], "nope")

    def test_restore_is_safe_when_there_is_nothing_to_restore(self):
        self.assertEqual(taste.restore(), {})

    def test_the_cli_can_undo_its_own_clear(self):
        import contextlib
        import io
        cli = _load_cli()
        taste.record_like({"title": "Terminal Cut", "artist": "Terminal Band",
                           "duration": 200})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["taste", "clear"]), 0)
        self.assertIn("taste restore", buf.getvalue(), "no hint about the way back")
        self.assertEqual(taste.load_state()["liked"], [])
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["taste", "restore"]), 0)
        self.assertIn("restored", buf.getvalue())
        self.assertEqual([r["title"] for r in taste.load_state()["liked"]],
                         ["terminal cut"])

    def test_forget_and_restore_on_the_dj_keep_the_object_in_sync(self):
        taste.record_like({"title": "Live", "artist": "Live Band", "duration": 200})
        d = DJ(backend="none", headless=True)
        d.state = config.load_state()
        d.forget_taste()
        self.assertEqual(d.state["liked"], [])
        d.restore_taste()
        self.assertEqual(len(d.state["liked"]), 1, "the DJ still thought it was empty")
        self.assertIn("taste restored: 1 loved", d.log[-1])


class _LoopPlayer:
    """
    A stand-in mpv, and deliberately not a re-implementation of one.

    `finished()` is the *shipped* MPVPlayer method bound to this object, so a change
    to how the real player decides "this song is over" moves these tests. Everything
    else here is the surface mpv exposes: three properties, a progress pair, and
    `play_url`.
    """

    def __init__(self, pos=10.0, dur=60.0, eof=False, idle=False, alive=True,
                 playing=True, played=True, load_at=None, broken=False):
        self.pos, self.dur = pos, dur
        self.eof_, self.idle = eof, idle
        self.alive_, self.playing = alive, playing
        self._played = played
        self._load_at = time.time() if load_at is None else load_at
        self.broken = broken
        self.NEVER_STARTED_SECONDS = 25.0
        self.loaded: list[str] = []

    # -- what dj.py calls
    def progress(self):
        if self.broken:
            raise OSError("ipc socket went away")
        return (self.pos, self.dur) if self.alive_ else (0.0, 0.0)

    def get_property(self, name):
        if not self.alive_ or self.broken:
            return None
        return {"eof-reached": self.eof_, "idle-active": self.idle,
                "pause": not self.playing}.get(name)

    def alive(self):
        return self.alive_

    def is_playing(self):
        return bool(self.alive_ and self.playing and self.eof_ is not True)

    def eof(self):
        return self.eof_

    def finished(self):
        return dj_mod.player_mod.MPVPlayer.finished(self)

    def play_url(self, url):
        self.loaded.append(url)
        return True

    def pause(self):
        self.playing = False

    def resume(self):
        self.playing = True

    def stop(self):
        pass

    def quit(self):
        pass

    def seek(self, seconds):
        self.pos = float(seconds)

    def volume(self, pct):
        pass


class BackgroundRefillTests(unittest.TestCase):
    """
    "while the AI processing the queue everything is freeze like the audio doesnt
    advance" - the engine/hang report. `next()` used to run `_topup()` ->
    `build_queue` -> the LLM synchronously on the DJ loop, so a low queue made a
    track change wait seconds-to-minutes for the planner and the player sat silent.
    These pin that `next()` returns immediately and the refill happens behind it.
    """

    def _dj(self, n=2):
        d = DJ(backend="none", headless=True)
        d.auto = False
        d.request = ""                       # stop the real refill adding things
        d._resolve = lambda t: "http://stream"
        d.add([{"id": f"a{i}", "title": f"T{i}", "artist": "X", "duration": 60,
                "url": f"http://y/{i}", "query": "q"} for i in range(n)])
        return d

    def test_next_does_not_wait_for_a_slow_topup(self):
        d = self._dj(2)
        done = []
        def slow_topup(keep=12, force=False):
            done.append("start")
            import time
            time.sleep(0.6)                  # a stand-in for an LLM/network call
            done.append("end")
        d._topup = slow_topup
        t0 = time.time()
        got = d.next(force=True)
        elapsed = time.time() - t0
        self.assertIsNotNone(got, "a queued row must start even while refilling")
        self.assertLess(elapsed, 0.4, "next() must not block on the planner")
        self.assertEqual(done, ["start"], "the refill runs behind the play start")

    def test_the_refill_thread_finishes_in_the_background(self):
        d = self._dj(1)
        done = []
        def slow_topup(keep=12, force=False):
            import time
            time.sleep(0.5)
            done.append("end")
        d._topup = slow_topup
        d.next(force=True)
        self.assertEqual(done, [], "the background filler may still be running")
        import time
        time.sleep(0.9)
        self.assertEqual(done, ["end"], "the background refill completes on its own")

    def test_only_one_advance_is_made_when_two_calls_race(self):
        d = self._dj(4)
        gate = __import__("threading").Event()
        orig = d._try_start
        def slow_start(t):
            gate.wait(1.0)                   # both threads press at once
            return orig(t)
        d._try_start = slow_start
        got = []
        def press():
            got.append(d.next(force=True))
        t1 = __import__("threading").Thread(target=press)
        t2 = __import__("threading").Thread(target=press)
        t1.start(); t2.start()
        import time; time.sleep(0.25)
        gate.set()
        t1.join(); t2.join()
        started = [t["id"] for t in got if t]
        self.assertEqual(len(started), 1, "a race must not double-start a track")


class AutoAdvanceTests(unittest.TestCase):
    """
    "auto advance doesnt works for some reaseon, even the song's over it doesnt
    next and stuck" - the report, quoted because it was exactly right.

    The loop only advanced on `eof-reached` or `time-pos >= duration - 1`. A real
    mpv started without `--keep-open` clears the flag as soon as it sets it and
    reports no position at all once the file unloads, so neither test could be true
    when the DJ looked. These pin the four ways a song can end and the one way a
    tick must not be allowed to die.
    """

    def _dj(self, player, n=4):
        d = DJ(backend="none", headless=True)
        d.backend, d.headless, d.player = "mpv", False, player
        d._topup = lambda **k: None
        d._resolve = lambda t: "http://stream"
        d.add([{"id": f"a{i}", "title": f"T{i}", "artist": "Someone", "duration": 60,
                "url": f"http://y/{i}", "query": "q"} for i in range(n)])
        d.next()
        d._stall, d._tick_pos = 0.0, None
        return d

    # ------------------------------------------------------------ end of song
    def test_eof_reached_moves_to_the_next_track(self):
        d = self._dj(_LoopPlayer(eof=True))
        self.assertEqual(d.current["title"], "T0")
        d._tick()
        self.assertEqual(d.current["title"], "T1")

    def test_a_position_at_the_end_moves_even_with_no_eof_flag(self):
        d = self._dj(_LoopPlayer(pos=59.6, dur=60.0))
        d._tick()
        self.assertEqual(d.current["title"], "T1")

    def test_the_mpv_that_unloads_at_eof_moves(self):
        """No --keep-open: idle again, no position, no eof flag - the reported hang."""
        d = self._dj(_LoopPlayer(pos=0.0, dur=0.0, idle=True, played=True))
        d._tick()
        self.assertEqual(d.current["title"], "T1", "the DJ sat at the end of a dead file")

    def test_a_dead_player_moves_instead_of_waiting(self):
        d = self._dj(_LoopPlayer(alive=False, pos=0.0, dur=0.0))
        d._tick()
        self.assertEqual(d.current["title"], "T1")

    # -------------------------------------------------------- must NOT advance
    def test_a_song_in_the_middle_is_left_alone(self):
        p = _LoopPlayer(pos=20.0, dur=60.0)
        d = self._dj(p)
        for _ in range(40):
            p.pos = min(p.pos + 0.75, p.dur - 1.5)
            d._tick()
        self.assertEqual(d.current["title"], "T0")
        self.assertEqual(d.queue.pos, 1)
        self.assertLess(d._stall, d.STALL_SECONDS)

    def test_a_paused_song_is_never_counted_as_stalled(self):
        p = _LoopPlayer(pos=20.0, dur=600.0, playing=False)
        d = self._dj(p)
        d.STALL_SECONDS = 1.0
        for _ in range(10):
            d._tick()
        self.assertEqual(d.current["title"], "T0", "a paused track was skipped")

    def test_the_watchdog_skips_a_stream_that_never_advances(self):
        p = _LoopPlayer(pos=20.0, dur=600.0)
        d = self._dj(p)
        d.STALL_SECONDS = 1.5
        for _ in range(3):
            d._tick()
        self.assertEqual(d.current["title"], "T1", "a frozen stream held the queue")
        self.assertTrue(any("sat still for" in line for line in d.log),
                        "the skip was silent, so it looked arbitrary")

    def test_silence_from_the_player_is_not_read_as_the_end_of_a_song(self):
        """Every property reading None - a busy or half-dead socket - is "unknown".

        Advancing on that would skip a perfectly good song, which is a worse bug than
        waiting one more tick, so each signal here needs a True, not a missing value.
        """
        p = _LoopPlayer(pos=30.0, dur=60.0)
        p.get_property = lambda name: None
        d = self._dj(p)
        d.STALL_SECONDS = 1e9          # only the reads are broken, not the clock
        d._tick()
        self.assertEqual(d.current["title"], "T0")
        self.assertEqual(d._track_ended(30.0, 60.0), "")

    def test_a_file_that_never_started_is_given_time_then_dropped(self):
        fresh = _LoopPlayer(pos=0.0, dur=0.0, played=False)
        self.assertFalse(fresh.finished(), "a loading track is not a finished one")
        stale = _LoopPlayer(pos=0.0, dur=0.0, played=False,
                            load_at=time.time() - 40.0)
        self.assertTrue(stale.finished(), "25 s without a position is a hang")

    def test_paused_skips_the_tick_entirely(self):
        d = self._dj(_LoopPlayer(eof=True))
        d.paused = True
        self.assertEqual(d._tick(), 0.5)
        self.assertEqual(d.current["title"], "T0")

    # ------------------------------------------------------------------ the loop
    def test_one_bad_tick_does_not_end_auto_advance(self):
        d = DJ(backend="none", headless=True)
        d._topup = lambda **k: None
        calls = []

        def tick():
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("socket died")
            d._stop.set()
            return 0.0

        d._tick = tick
        d.run()
        self.assertEqual(len(calls), 2, "the loop died on the first exception")
        self.assertTrue(any("carrying on" in line for line in d.log),
                        "it failed silently, which is the whole complaint")

    def test_the_stop_event_ends_the_wait_early(self):
        d = DJ(backend="none", headless=True)
        d._tick = lambda: 30.0
        d._stop.set()
        start = time.time()
        d.run()
        self.assertLess(time.time() - start, 1.0, "stopping waited out the tick")

    # ------------------------------------------------------------- handoff mode
    def test_handoff_does_not_advance_and_says_why_once(self):
        d = DJ(backend="none", headless=True)
        d.backend, d.player = "spotube", None
        d._topup = lambda **k: None
        d._resolve = lambda t: "http://stream"
        d.add([{"id": f"h{i}", "title": f"H{i}", "artist": "a", "duration": 60,
                "url": "u", "query": "q"} for i in range(3)])
        d.next()
        first = d.current["title"]
        d.started_at = time.time() - 30.0
        for _ in range(3):
            d._tick()
        self.assertEqual(d.current["title"], first, "the DJ advanced a track it cannot hear")
        said = [line for line in d.log if "will not advance itself" in line]
        self.assertEqual(len(said), 1, f"expected one honest note, got {len(said)}")

    def test_handoff_says_nothing_before_the_grace_period(self):
        d = DJ(backend="none", headless=True)
        d.backend, d.player = "spotube", None
        d._topup = lambda **k: None
        d._resolve = lambda t: "http://stream"
        d.add([{"id": "h0", "title": "H0", "artist": "a", "duration": 60,
                "url": "u", "query": "q"}])
        d.next()
        d._tick()
        self.assertFalse(any("will not advance itself" in line for line in d.log),
                         "a track two seconds old is not a stuck one")

    # ------------------------------------------------------------------ mpv argv
    def test_mpv_is_started_so_the_end_of_file_survives(self):
        pm = dj_mod.player_mod
        with mock.patch.object(pm.bins, "find", return_value="/usr/bin/mpv"), \
             mock.patch.object(pm.subprocess, "Popen") as popen, \
             mock.patch.object(pm.MPVPlayer, "_wait_socket", lambda self: None):
            pm.MPVPlayer()
        cmd = popen.call_args[0][0]
        self.assertIn("--keep-open=yes", cmd,
                      "without it mpv clears eof-reached the instant it sets it")
        self.assertIn("--idle=yes", cmd, "mpv must not exit between tracks")


class MPVFinishedTests(unittest.TestCase):
    """`MPVPlayer.finished()` on its own, driven by the properties it reads."""

    def _player(self, **kw):
        return _LoopPlayer(**kw)

    def test_each_signal_alone_is_enough(self):
        for kw in ({"eof": True},
                   {"pos": 100.0, "dur": 100.0},
                   {"pos": 0.0, "dur": 0.0, "idle": True},
                   {"alive": False}):
            with self.subTest(**kw):
                self.assertTrue(self._player(**kw).finished())

    def test_no_signal_is_enough_on_its_own_while_a_song_plays(self):
        p = self._player(pos=41.0, dur=100.0)
        self.assertFalse(p.finished())
        p = self._player(pos=0.0, dur=0.0, idle=False)
        self.assertFalse(p.finished(), "idle-active False must not read as finished")


class ClearQueueTests(unittest.TestCase):
    """"why theres no clear queue list button" - now there is a verb for it."""

    def _dj(self, n=4, auto=True):
        d = DJ(backend="none", headless=True)
        d._resolve = lambda t: "http://stream"
        d._topup = lambda **k: None
        d.auto = auto
        d.add([{"id": f"q{i}", "title": f"Q{i}", "artist": "a", "duration": 200,
                "url": "u", "query": "q"} for i in range(n)])
        d.next()
        return d

    def test_clear_ahead_keeps_the_cursor_and_counts_what_went(self):
        q = Queue()
        q.extend([{"id": str(i)} for i in range(5)])
        q.pos = 2
        self.assertEqual(q.clear_ahead(), 3)
        self.assertEqual(len(q), 0)
        self.assertEqual(q.pos, 2, "the cursor walked off the end of the list")
        self.assertEqual(len(q.items), 2, "rows already heard were deleted too")

    def test_clear_ahead_on_an_empty_queue_is_a_no_op(self):
        q = Queue()
        self.assertEqual(q.clear_ahead(), 0)
        q.pos = 9                       # a cursor past the end must not crash it
        self.assertEqual(q.clear_ahead(), 0)

    def test_the_playing_song_survives_a_clear(self):
        d = self._dj()
        self.assertIn("3 tracks dropped", d.clear_queue())
        self.assertEqual(d.current["title"], "Q0")
        self.assertEqual(len(d.queue), 0)

    def test_the_note_says_whether_it_will_refill(self):
        d = self._dj(auto=True)
        note = d.clear_queue()
        self.assertIn("3 tracks dropped", d.log[-1])
        self.assertIn("the list fills again shortly", note)
        # and the verb itself must not go looking for tracks: that is a search, and
        # a button that clears a list is not allowed to be the thing that blocks
        self.assertNotIn("build_queue", str(d._topup))
        d2 = self._dj(auto=False)
        d2.clear_queue()
        self.assertIn("nothing refills it while keep mixing is off", d2.log[-1])

    def test_one_row_left_uses_the_singular(self):
        d = self._dj(n=2)
        d.clear_queue()
        self.assertIn("1 track dropped", d.log[-1])


class CacheRaceTests(unittest.TestCase):
    """
    "the cache system download doesnt fast enough, they still a delay if user
    active skipper" - three separate latencies, all fixed at the lane.
    """

    def setUp(self):
        self.ac = dj_mod.audiocache
        self.ac.stop()
        # four module attributes are swapped in these tests, and the scratch state
        # directory is shared: everything taken here is put back in tearDown, or the
        # next test in the file runs against a lane with a fake `_start`
        self._was = (self.ac.enabled, self.ac._start, self.ac._peek_url, self.ac.fetch)
        self.ac.enabled = lambda: True
        self.ac._start = lambda: None       # inspect the lane, do not run a thread

    def tearDown(self):
        self.ac.enabled, self.ac._start, self.ac._peek_url, self.ac.fetch = self._was
        self.ac._stop.set()
        del self.ac._queue[:]
        self.ac._resolved.clear()

    def _row(self, vid):
        return {"id": vid, "title": vid, "artist": "a", "url": "u"}

    def test_priority_lands_at_the_front_in_the_order_given(self):
        ac = self.ac
        self.assertEqual(ac.prefetch([self._row("a"), self._row("b")]), 2)
        self.assertEqual(list(ac._queue), ["a", "b"])
        ac.prefetch([self._row("c"), self._row("d")], priority=True)
        self.assertEqual(list(ac._queue), ["c", "d", "a", "b"],
                         "the row about to be heard must be first, not last")

    def test_promote_moves_an_unstarted_download(self):
        ac = self.ac
        self.assertEqual(ac.prefetch([self._row("a"), self._row("b"), self._row("soon")],
                                     ahead=3), 3)
        self.assertTrue(ac.promote("soon"))
        self.assertEqual(list(ac._queue)[0], "soon")
        self.assertFalse(ac.promote("never-queued"))
        self.assertFalse(ac.promote(""))

    def test_a_resolved_url_is_shared_between_the_lane_and_the_player(self):
        ac = self.ac
        vid = "abc"
        self.assertIsNone(ac.resolved(vid))
        ac.remember(vid, "http://stream/x")
        self.assertEqual(ac.resolved(vid), "http://stream/x")
        self.assertEqual(ac.lookup(self._row(vid)), (None, "http://stream/x"))
        ac._resolved[vid] = ("http://stream/x", time.time() - ac.RESOLVED_TTL - 1)
        self.assertIsNone(ac.resolved(vid), "a signed URL outlives its usefulness")

    def test_the_lane_learns_the_url_before_it_downloads(self):
        ac = self.ac
        seen = []
        ac._stop.clear()
        ac._queue.append("vid1")
        ac._peek_url = lambda vid: seen.append(vid) or "http://peeked"
        def fake_fetch(vid):
            self.assertEqual(ac.resolved(vid), "http://peeked")
            ac._stop.set()
            return None
        ac.fetch = fake_fetch
        ac._worker()
        self.assertEqual(seen, ["vid1"])

    def test_starting_a_row_that_is_only_resolved_skips_the_resolver(self):
        d = DJ(backend="mpv", headless=False)
        d._topup = lambda **k: None
        calls = []
        d._resolve = lambda t: calls.append(1) or "http://resolved"
        played = []
        d.player = type("P", (), {"play_url": lambda self, u: played.append(u) or True,
                                  "alive": lambda self: True,
                                  "progress": lambda self: (1.0, 60.0),
                                  "log_path": "/tmp/x"})()
        with mock.patch.object(dj_mod.audiocache, "lookup",
                               return_value=(None, "http://stashed")):
            ok, why = d._try_start({"id": "z", "title": "Z", "artist": "a", "url": "u"})
        self.assertTrue(ok, why)
        self.assertEqual(played, ["http://stashed"])
        self.assertEqual(calls, [], "the stash was ignored, so the skip still waits")

    def test_a_refused_stash_is_re_resolved_once(self):
        d = DJ(backend="mpv", headless=False)
        d._topup = lambda **k: None
        tries = []
        d.player = type("P", (), {
            "play_url": lambda self, u: (tries.append(u), len(tries) > 1)[1],
            "alive": lambda self: True,
            "progress": lambda self: (1.0, 60.0),
            "log_path": "/tmp/x"})()
        d._resolve = lambda t: "http://fresh"
        with mock.patch.object(dj_mod.audiocache, "lookup",
                               return_value=(None, "http://expired")), \
             mock.patch.object(dj_mod.audiocache, "remember") as rem:
            ok, why = d._try_start({"id": "z", "title": "Z", "artist": "a", "url": "u"})
        self.assertTrue(ok, why)
        self.assertEqual(tries, ["http://expired", "http://fresh"])
        rem.assert_called_once_with("z", "http://fresh")

    def test_a_fresh_advance_puts_the_next_rows_at_the_front(self):
        d = DJ(backend="none", headless=True)
        d.backend, d.headless = "mpv", False
        d.player = type("P", (), {"play_url": lambda self, u: True,
                                  "alive": lambda self: True,
                                  "progress": lambda self: (1.0, 60.0),
                                  "log_path": "/tmp/x"})()
        d._resolve = lambda t: "http://stream"
        d._topup = lambda **k: None
        d.add([{"id": f"p{i}", "title": f"P{i}", "artist": "a", "duration": 200,
                "url": "u", "query": "q"} for i in range(6)])
        promoted = []
        with mock.patch.object(dj_mod.audiocache, "promote",
                               side_effect=lambda v: promoted.append(v) or True), \
             mock.patch.object(dj_mod.audiocache, "prefetch") as pre:
            d.next()
        pre.assert_called_once()
        self.assertEqual(pre.call_args.kwargs.get("priority"), True)
        self.assertEqual(promoted, ["p1"], "the row right after this one is the priority")


class UnreadablePlayerTests(unittest.TestCase):
    """A player that cannot be asked is not a player that says "the song is over"."""

    def _dj(self, player):
        d = DJ(backend="none", headless=True)
        d.backend, d.headless, d.player = "mpv", False, player
        d._topup = lambda **k: None
        d._resolve = lambda t: "http://stream"
        d.add([{"id": f"u{i}", "title": f"U{i}", "artist": "a", "duration": 60,
                "url": "u", "query": "q"} for i in range(3)])
        d.next()
        d._stall, d._tick_pos, d._ended_errors = 0.0, None, 0
        return d

    def test_a_broken_player_is_named_once_and_the_song_keeps_playing(self):
        calls = []

        class Broken:
            def progress(self):
                calls.append(1)
                return 10.0, 60.0

            def finished(self):
                raise TypeError("the player object is not a player")

            def play_url(self, url):
                return True

        d = self._dj(Broken())
        for _ in range(20):
            d._tick()
        self.assertEqual(d.current["title"], "U0", "a broken read became a skip")
        said = [line for line in d.log if "cannot read the player" in line]
        self.assertEqual(len(said), 1, f"the same complaint was logged {len(said)} times")
        self.assertIn("TypeError", said[0])

    def test_the_counter_clears_when_the_player_answers_again(self):
        class Flaky:
            def __init__(self):
                self.broken = True

            def progress(self):
                return 10.0, 60.0

            def finished(self):
                if self.broken:
                    raise RuntimeError("busy")
                return False

            def play_url(self, url):
                return True

        p = Flaky()
        d = self._dj(p)
        d._tick()
        self.assertEqual(d._ended_errors, 1)
        p.broken = False
        d._tick()
        self.assertEqual(d._ended_errors, 0, "a recovered player is still on the watch list")
