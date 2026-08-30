"""
The shared view-model, tested without a display.

`viewmodel.py` holds the rules both the browser skin and the CLI read - how a
duration is formatted, which rows still need artwork, when a list may skip a
rebuild, what the transport buttons mean - so it gets tested directly rather than
through a window. The old `test_gui_core.py` mixed these with Tk plumbing (posted
events, `after()` pacing, font measuring); that half went away with the Tk front
end, and the rules here are the part the web page still depends on.
"""
from __future__ import annotations

import unittest

import tests  # noqa: F401  (sys.path bootstrap)

import viewmodel as vm


class FormatTests(unittest.TestCase):
    def test_mmss(self):
        self.assertEqual(vm.mmss(0), "0:00")
        self.assertEqual(vm.mmss(90), "1:30")
        self.assertEqual(vm.mmss(3725), "62:05")     # no hours form: a song is not a film

    def test_mmss_survives_junk_without_lying(self):
        # json can carry `Infinity` and a state file can carry `-30`; a duration
        # that is not a duration reads as "0:00", never as "-1:55" and never as a
        # traceback in the middle of drawing the queue
        for junk in (None, "", "abc", float("nan"), float("inf"), -30, 9e99, 90000, {}, []):
            self.assertEqual(vm.mmss(junk), "0:00", repr(junk))

    def test_now_playing_line_shows_progress_only_with_a_duration(self):
        self.assertIn("1:05/3:20", vm.now_playing_line("A", "B", 30, 65, 200))
        self.assertNotIn("/", vm.now_playing_line("A", "B", 30, 0, 0))

    def test_track_line_never_exceeds_the_width(self):
        line = vm.track_line("Forge Ahead", "Midlake", 18)
        self.assertIn("Forge Ahead", line)
        self.assertLessEqual(len(line), 18)
        wide = vm.track_line("Forge Ahead", "Midlake", 40)
        self.assertIn("Midlake", wide)
        self.assertIn("—", wide)
        tight = vm.track_line("x" * 90, "y" * 90, 30)
        self.assertLessEqual(len(tight), 30, tight)
        self.assertTrue(tight.endswith("…"), tight)

    def test_ellipsize_keeps_short_text_intact(self):
        self.assertEqual(vm.ellipsize("short", 40), "short")

    def test_time_line_and_results_note(self):
        self.assertEqual(vm.time_line(0, 0), "0:00")           # nothing to compare to
        self.assertEqual(vm.time_line(30, 200), "0:30 / 3:20")
        note = vm.results_note("slowdive", 18, 3, 2.1)
        self.assertIn("18", note)
        self.assertIn("slowdive", note)
        self.assertIn("skipped", note)

    def test_wrap_lines_respects_the_budget(self):
        text = "one two three four five six seven eight"
        out = vm.wrap_lines(text, 12, 2)
        self.assertLessEqual(len(out.split("\n")), 2, out)
        self.assertLessEqual(len(out), 12 * 2 + 1, out)         # the budget is the area
        self.assertEqual(vm.wrap_lines("short", 12, 2), "short")
        self.assertEqual(vm.wrap_lines("", 12, 2), "")
        self.assertEqual(vm.wrap_lines(None, 12, 2), "")


class ColourAndStatusTests(unittest.TestCase):
    def test_log_colour_maps_levels(self):
        self.assertEqual(vm.log_colour("[error] boom"), vm.ERROR)
        self.assertEqual(vm.log_colour("playing: nice song"), vm.SUCCESS)
        self.assertEqual(vm.log_colour("offline parse"), vm.ACCENT)
        self.assertEqual(vm.log_colour("plain note"), vm.TEXT)

    def test_status_dot(self):
        self.assertEqual(vm.status_dot(False), ("dot_off", vm.MUTED))
        self.assertEqual(vm.status_dot(True), ("dot_on", vm.SUCCESS))
        self.assertEqual(vm.status_dot(True, busy=True), ("dot_busy", vm.ACCENT))

    def test_engine_badge_flags_fallback_as_error(self):
        self.assertEqual(vm.engine_badge("gemini")[1], vm.SUCCESS)
        self.assertEqual(vm.engine_badge("offline")[1], vm.ACCENT)
        self.assertEqual(vm.engine_badge("gemini (fallback)")[1], vm.ERROR)
        self.assertIn("OFFLINE", vm.engine_badge(None)[0])

    def test_human_status_says_something_for_every_state(self):
        for engine in (None, "offline", "gemini", "gemini (fallback)"):
            for err in ("", "429 too many requests"):
                for key in (False, True):
                    text, colour = vm.human_status(engine, err, key)
                    self.assertTrue(text.strip(), (engine, err, key))
                    self.assertTrue(str(colour).startswith("#"), text)

    def test_a_failing_planner_does_not_claim_to_be_fine(self):
        # the point of the strip: an engine that is named but erroring must not
        # read as "AI planner on" in green
        text, colour = vm.human_status("gemini", "429 too many requests", True)
        self.assertIn("429", text)
        self.assertEqual(colour, vm.ERROR)
        self.assertEqual(vm.human_status("gemini", "", True)[1], vm.SUCCESS)

    def test_engine_chip_label_matches_the_engine_in_use(self):
        # short fixed labels - the pill sits next to a button, not a paragraph
        self.assertEqual(vm.engine_chip("gemini", "", True)[0], "smart search: on")
        self.assertEqual(vm.engine_chip("offline", "", False)[0], "built-in search")
        self.assertEqual(vm.engine_chip(None, "", False)[0], "built-in search")
        bad = vm.engine_chip("gemini (fallback)", "", True)
        self.assertEqual(bad[0], "smart search: failed")
        self.assertEqual(bad[1], vm.ERROR)
        self.assertIsInstance(bad[2], str)

    def test_palette_colours_are_hex(self):
        for name in ("BG", "PANEL", "CARD", "TEXT", "MUTED", "ACCENT", "HEART", "ERROR"):
            self.assertRegex(getattr(vm, name), r"^#[0-9a-fA-F]{6}$", name)

    def test_tile_colour_is_stable_and_palette_born(self):
        for seed in ("Slowdive", "JAY-Z", "", None, "x" * 200):
            colour = vm.tile_colour(seed)
            self.assertIn(colour, vm.TILE_PALETTE, repr(seed))
            self.assertEqual(colour, vm.tile_colour(seed))      # no flicker on redraw

    def test_tile_letter_is_a_single_upper_character(self):
        self.assertEqual(vm.tile_letter({"title": "wolf in sheep's clothing"})[0], "W")
        self.assertTrue(vm.tile_letter({}).strip())


class RowPolicyTests(unittest.TestCase):
    def _t(self, i, **kw):
        row = {"id": f"t{i}", "title": f"title {i}", "artist": f"artist {i}",
               "duration": 200 + i}
        row.update(kw)
        return row

    def test_rows_needing_art_asks_for_the_unpictureed_rows_in_order(self):
        # `have` is a probe, not a set: the caller owns the cache, this only sorts
        tracks = [self._t(i) for i in range(30)]
        got = vm.rows_needing_art(tracks, lambda t: t["id"] in {"t1", "t3"}, limit=4)
        self.assertEqual([t["id"] for t in got], ["t0", "t2", "t4", "t5"])

    def test_rows_needing_art_skips_rows_without_an_id(self):
        tracks = [{"title": "no id"}, self._t(1)]
        self.assertEqual([t["id"] for t in vm.rows_needing_art(tracks, lambda t: False)], ["t1"])

    def test_rows_needing_art_survives_a_broken_probe_and_garbage(self):
        def boom(t):
            raise RuntimeError("cache is on fire")
        tracks = [self._t(0), self._t(1)]
        self.assertEqual(vm.rows_needing_art(tracks, boom), tracks)   # hide nothing
        self.assertEqual(vm.rows_needing_art(None, boom), [])
        self.assertEqual(vm.rows_needing_art([None, "x", {}], boom), [])

    def test_queue_preview_caps_rows_and_shows_the_djs_initiative(self):
        tracks = [self._t(0, cached=True), self._t(1, mixed=True),
                  self._t(2), self._t(3), self._t(4), self._t(5)]
        rows = vm.queue_preview(tracks, 5, 40)
        self.assertEqual(len(rows), 5)
        self.assertIn("cached", rows[0])
        self.assertIn("from your likes", rows[1])
        self.assertNotIn("\u00b7", rows[2], "a plain row must not gain a tail")

    def test_queue_preview_of_nothing_is_nothing(self):
        self.assertEqual(vm.queue_preview([], 6, 40), [])
        self.assertEqual(vm.queue_preview(None, 6, 40), [])

    def test_track_rows_truncate_to_the_pixel_budget(self):
        rows = vm.track_rows([self._t(i, title="x" * 300) for i in range(3)],
                             width_px=240, char_px=8)
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertTrue(isinstance(r, tuple) or isinstance(r, dict))

    def test_queue_rows_mark_the_playing_one(self):
        cur = self._t(0)
        rows = vm.queue_rows(cur, [self._t(1), self._t(2)], 240, 8)
        self.assertEqual(len(rows), 3)
        self.assertTrue(any("Now playing" in str(r) or "playing" in str(r).lower() for r in rows))

    def test_artist_rows_give_a_plain_language_verdict(self):
        rows = vm.artist_rows({"slowdive": 8.0, "jAY-z": 1.0, "hater": -2.0, "": 5.0},
                              width_px=200, char_px=8, limit=5)
        self.assertEqual([r["name"] for r in rows], ["slowdive", "jAY-z", "hater"])
        self.assertEqual(rows[0]["verdict"], "you love this")
        self.assertEqual(rows[0]["colour"], vm.SUCCESS)
        self.assertIn("skipped", rows[2]["verdict"])
        self.assertLessEqual(len(vm.artist_rows({"a": 1, "b": 1, "c": 1}, 200, 8, limit=2)), 2)
        self.assertEqual(vm.artist_rows({}, 200, 8), [])
        self.assertEqual(vm.artist_rows(None, 200, 8), [])



class TransportTests(unittest.TestCase):
    def test_progress_frac_never_divides_by_zero(self):
        self.assertEqual(vm.progress_frac(10, 0), 0.0)
        self.assertEqual(vm.progress_frac(120, 60), 1.0)
        self.assertEqual(vm.progress_frac(None, None), 0.0)
        self.assertEqual(vm.progress_frac(-5, 60), 0.0)

    def test_seek_target_stays_inside_the_track(self):
        self.assertAlmostEqual(vm.seek_target(0.5, 200), 100.0)
        self.assertEqual(vm.seek_target(3, 200), 200.0)      # a wild fraction cannot
        self.assertEqual(vm.seek_target(-1, 200), 0.0)       # seek past the end
        self.assertEqual(vm.seek_target("junk", 200), 0.0)
        self.assertEqual(vm.seek_target(float("inf"), 200), 0.0)
        self.assertEqual(vm.seek_target(0.5, 0), 0.0)

    def test_seek_by_clamps_both_ways(self):
        frac, secs = vm.seek_by(10, 200, 5)
        self.assertEqual(secs, 15)
        self.assertAlmostEqual(frac, 15 / 200)
        self.assertEqual(vm.seek_by(0, 200, -30)[1], 0.0)
        self.assertEqual(vm.seek_by(195, 200, 30)[1], 200.0)

    def test_transport_state_words(self):
        st = vm.transport_state(playing=True, paused=False, busy=False)
        self.assertEqual(st["playpause"], "pause")
        self.assertTrue(st["seek_enabled"])
        self.assertEqual(vm.transport_state(True, True, False)["playpause"], "play")
        self.assertEqual(vm.transport_state(False, False, False)["hint"], "nothing loaded")
        self.assertEqual(vm.transport_state(False, False, True)["playpause"], "\u2026")
        self.assertEqual(vm.transport_state(True, False, False, liked=True)["heart"], "liked")
        self.assertFalse(vm.transport_state(False, False, False)["next_enabled"])

    def test_button_states(self):
        st = vm.button_states(busy=True, has_request=True, has_last_request=True)
        self.assertFalse(st["play_enabled"])
        self.assertFalse(st["continue_enabled"])
        st = vm.button_states(False, True, False)
        self.assertTrue(st["play_enabled"])
        self.assertIn("Play", str(st["play_text"]))

    def test_playlock_is_single_holder_and_idempotent(self):
        lk = vm.PlayLock()
        self.assertTrue(lk.acquire("play"))
        self.assertFalse(lk.acquire("again"))
        lk.release()
        self.assertFalse(lk.busy)
        lk.release()
        self.assertTrue(lk.acquire("play"))

    def test_row_menu_covers_the_verbs_the_page_offers(self):
        items = vm.row_menu({"title": "T", "artist": "A"}, playing=True, queued=False)
        self.assertTrue(all(set(i) >= {"action", "label", "enabled"} for i in items), items)
        labels = " ".join(str(i.get("label", "")) for i in items)
        for word in ("Love", "Add to queue", "station"):
            self.assertIn(word, labels, "the right-click menu lost a verb the page has")
        self.assertTrue(any(i.get("separator_after") or i.get("separator_before")
                            for i in items), "a menu with no separators reads as one lump")


class CopyTests(unittest.TestCase):
    def test_greeting_boundaries(self):
        from datetime import datetime
        self.assertEqual(vm.greeting(datetime(2026, 1, 1, 4)), "Still up?")
        self.assertEqual(vm.greeting(datetime(2026, 1, 1, 9)), "Good morning")
        self.assertEqual(vm.greeting(datetime(2026, 1, 1, 13)), "Good afternoon")
        self.assertEqual(vm.greeting(datetime(2026, 1, 1, 23)), "Good evening")

    def test_greeting_table_is_what_the_page_ships(self):
        # webapp inlines vm.GREETING rather than restating the hours
        self.assertTrue(vm.GREETING and all(len(g) == 3 for g in vm.GREETING))
        self.assertEqual(vm._GREETING, vm.GREETING)

    def test_mood_at_wraps(self):
        self.assertEqual(vm.mood_at(0), vm.MOODS[0])
        self.assertEqual(vm.mood_at(-1), vm.MOODS[-1])
        self.assertEqual(vm.mood_at(len(vm.MOODS) * 3 + 2), vm.MOODS[2])

    def test_fmt_count(self):
        self.assertEqual(vm.fmt_count(1, "like"), "1 like")
        self.assertEqual(vm.fmt_count(3, "like"), "3 likes")
        self.assertEqual(vm.fmt_count(0, "track", "tracks"), "0 tracks")

    def test_empty_state_has_copy_for_every_list_that_starts_blank(self):
        for view in ("search", "loved", "recent", "library", "queue"):
            text = vm.empty_state(view)
            self.assertTrue(text.strip(), view)
            self.assertGreater(len(text), 40, f"{view}'s empty state is a shrug")
        # has_data means "there IS data", so the copy must step aside
        self.assertEqual(vm.empty_state("search", has_data=True), "")
        self.assertEqual(vm.empty_state("unknown-view"), "")

    def test_home_subtitle_counts_only_what_there_is(self):
        fresh = vm.home_subtitle(0, 0)
        self.assertIn("mood", fresh.lower())
        self.assertNotIn("0 ", fresh)
        text = vm.home_subtitle(12, 30)
        self.assertIn("12 loved song", text)
        self.assertIn("30 tracks played", text)
        self.assertIn("no Premium", text)

    def test_album_mode_detection(self):
        self.assertEqual(vm.detect_album_mode("play the ost from destiny 1"), "destiny 1")
        self.assertEqual(vm.detect_album_mode("the ost from destiny 2"), "destiny 2")
        self.assertEqual(vm.detect_album_mode("Destiny 2 OST"), "destiny 2")
        self.assertIsNone(vm.detect_album_mode("lofi for coding"))
        self.assertIsNone(vm.detect_album_mode(""))
        self.assertIsNone(vm.detect_album_mode(None))

    def test_album_queries_target_albums_not_radio(self):
        qs = vm.album_queries("destiny 2")
        self.assertTrue(all(("ost" in q or "soundtrack" in q or "album" in q) for q in qs))
        self.assertTrue(any("full album" in q for q in qs))
        self.assertIn("album", vm.album_mode_note("destiny 2").lower())

    def test_settings_note_explains_the_backend(self):
        self.assertIn("mpv", vm.settings_note("mpv", False))
        self.assertIn("No audio", vm.settings_note("mpv", True))
        self.assertIn("cannot receive a queue", vm.settings_note("spotube", False))

    def test_format_activity_caps(self):
        out = vm.format_activity([f"line {i}" for i in range(900)], cap=40)
        rows = out.split("\n")
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[-1], "line 899")


class ContractTests(unittest.TestCase):
    """The tables the browser skin is built from, so the page cannot drift."""

    def test_nav_covers_every_view(self):
        self.assertEqual({v for v, _i, _l in vm.NAV}, set(vm.VIEWS))

    def test_moods_are_label_and_query(self):
        for label, q in vm.MOODS:
            self.assertTrue(label.strip() and q.strip())
            self.assertNotEqual(label, q, "the chip text and the search text should differ")

    def test_font_set_small_keeps_the_same_keys(self):
        for small in (False, True):
            fs = vm.font_set(small)
            self.assertEqual(set(fs), set(vm.FONTS))
            for spec in fs.values():
                self.assertGreaterEqual(spec[1], 7, "no font below 7pt")

    def test_pick_family_returns_none_for_no_match(self):
        self.assertIsNone(vm.pick_family([]))
        self.assertIn(vm.pick_family([vm.UI_FAMILY]), (vm.UI_FAMILY, None))


if __name__ == "__main__":
    unittest.main()
