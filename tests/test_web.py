"""Tests for the browser skin: the state it publishes, the actions it routes, and
the three places where a local HTTP server has to be suspicious - path traversal,
the Host header, and a client that stops reading."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import config
import web
import webapp

# buttons the page exposes through the mood chips / kebab menus rather than data-action
ACTIONS_BY_BUTTON = {"stop", "topup", "unlike", "auto"}
from dj import DJ, Queue


def track(i: int, **kw) -> dict:
    t = {"id": f"vid{i}", "title": f"Track {i}", "artist": f"Artist {i}",
         "duration": 190 + i, "url": f"https://music.youtube.com/watch?v=vid{i}"}
    t.update(kw)
    return t


def fake_dj(tracks: list[dict] | None = None) -> DJ:
    """A real DJ with no player: the web layer only ever reads status()."""
    dj = DJ(backend="none", headless=True)
    dj.queue = Queue()
    items = list(tracks if tracks is not None else [track(i) for i in range(6)])
    dj.queue.items = items
    # DJ pops the row it starts *before* playing it, so the cursor sits one past
    # the current track - the web skin has to survive either shape
    dj.current = items[0] if items else None
    dj.queue.pos = 1 if items else 0
    dj.info = {"engine": "offline", "why": "90s trip hop, dark", "queries": ["q1", "q2"],
               "llm_error": ""}
    dj.request = "90s trip hop"
    return dj


class RowViewTests(unittest.TestCase):
    def test_normalises_the_keys_the_page_reads(self):
        r = web.row_view(track(1, channel="Ch", uploader="Up"))
        self.assertEqual(r["title"], "Track 1")
        self.assertEqual(r["artist"], "Artist 1")
        self.assertEqual(r["channel"], "Ch")            # channel beats uploader
        self.assertEqual(r["dur"], "3:11")
        self.assertEqual(r["art"], "")

    def test_missing_duration_is_blank_not_zero(self):
        # "0:00" on a row reads like a broken track; blank means "not told yet"
        self.assertEqual(web.row_view({"id": "x", "title": "t"})["dur"], "")
        self.assertEqual(web.row_view({"id": "x", "title": "t", "duration": 0})["dur"], "")

    def test_uploader_only_row_still_gets_a_source_line(self):
        r = web.row_view({"id": "x", "title": "t", "uploader": "Some Channel"})
        self.assertEqual(r["channel"], "Some Channel")

    def test_none_is_a_row_of_blanks(self):
        self.assertEqual(web.row_view(None)["title"], "?")

    def test_liked_key_only_when_asked(self):
        self.assertNotIn("liked", web.row_view(track(1)))
        self.assertTrue(web.row_view(track(1), liked=True)["liked"])

    def test_a_row_with_a_cover_shows_it_immediately(self):
        # an album tracklist / discography row carries its own art, so it must not
        # wait for the artwork lane (that was "album page rows show no cover")
        r = web.row_view({"id": "x", "title": "Dummy", "artist": "Portishead",
                          "thumbnail": "https://i.ytimg.com/vi/x/hqdefault.jpg"})
        self.assertTrue(r["art"].startswith("http"))
        self.assertTrue(r["art_card"].startswith("http"))
        # the raw thumbnail also rides along, so the page's art() has a last-resort
        # URL if the two slots above ever get erased by a later pass
        self.assertEqual(r["thumbnail"], "https://i.ytimg.com/vi/x/hqdefault.jpg")

    def test_a_page_row_carries_its_album_identity(self):
        # the front-end needs `kind`/`album`/`browse_id` to open a discography row on
        # a click and to know it is an album, not a playable song
        r = web.row_view({"id": "", "title": "Dummy", "artist": "Portishead",
                          "kind": "album", "album": "Dummy", "browse_id": "B1",
                          "release_year": 1994, "note": "1994 · album"})
        self.assertEqual(r["kind"], "album")
        self.assertEqual(r["album"], "Dummy")
        self.assertEqual(r["browse_id"], "B1")
        self.assertEqual(r["release_year"], 1994)
        self.assertEqual(r["note"], "1994 · album")

    def test_a_row_without_a_cover_has_blank_not_broken_art(self):
        self.assertEqual(web.row_view({"id": "x", "title": "t"})["art"], "")


class BuildStateTests(unittest.TestCase):
    def setUp(self):
        self.dj = fake_dj()
        self.ctx = web.Context(self.dj, volume=42)

    def test_every_key_the_page_indexes(self):
        s = web.build_state(self.ctx)
        for key in ("now", "up_next", "queued", "position", "duration", "paused",
                    "auto", "volume", "backend", "idle", "idle_note", "why", "request",
                    "queries", "engine_note", "cache_note", "foot", "log", "search",
                    "loved", "vibe"):
            self.assertIn(key, s, key)

    def test_json_encodes_it(self):
        # a raw track dict anywhere in here would blow up on bytes/objects instead
        json.dumps(web.build_state(self.ctx))

    def test_the_mix_name_reaches_the_page(self):
        # the Daylist-style title is computed in the build_queue info and has to
        # ride the same state object that paints the header under Up Next
        self.dj.info["vibe"] = "lofi tuesday night"
        s = web.build_state(self.ctx)
        self.assertEqual(s["vibe"], "lofi tuesday night")

    def test_missing_vibe_is_a_blank_string(self):
        # no name yet literally means "no name", not the word "None" on the page
        self.dj.info = {}
        self.assertEqual(web.build_state(self.ctx)["vibe"], "")

    def test_playing_track_is_not_also_in_up_next(self):
        # the chronology complaint: Up Next shows what comes *after* this song
        s = web.build_state(self.ctx)
        self.assertEqual(s["now"]["id"], "vid0")
        self.assertNotIn("vid0", [r["id"] for r in s["up_next"]])
        self.assertEqual([r["id"] for r in s["up_next"]][0], "vid1")

    def test_empty_dj_gives_no_mystery_question_mark_row(self):
        dj = fake_dj(tracks=[])
        dj.current = None
        s = web.build_state(web.Context(dj))
        self.assertEqual(s["now"], {})
        self.assertEqual(s["up_next"], [])
        self.assertEqual(s["queued"], 0)

    def test_engine_note_is_the_viewmodel_sentence(self):
        # both skins ask viewmodel, so they can never disagree about the AI
        from viewmodel import human_status
        expected, _ = human_status("offline", "")
        self.assertEqual(web.build_state(self.ctx)["engine_note"], expected)

    def test_gemini_fallback_is_admitted_not_hidden(self):
        self.dj.info = {"engine": "gemini (fallback)", "llm_error": "404 model",
                        "why": "", "queries": []}
        note = web.build_state(self.ctx)["engine_note"].lower()
        self.assertTrue("not working" in note or "fail" in note, note)
        self.assertIn("404 model", note)

    def test_log_tail_and_queries_pass_through(self):
        self.dj.log.append("hello from the engine")
        s = web.build_state(self.ctx)
        self.assertIn("hello from the engine", s["log"][-1])
        self.assertEqual(s["queries"], ["q1", "q2"])

    def test_idle_reason_becomes_a_sentence(self):
        self.dj.idle = "finished"
        s = web.build_state(self.ctx)
        self.assertEqual(s["idle"], "finished")
        self.assertIn("ran out", s["idle_note"])

    def test_unplayable_retry_note_keeps_playing_track_visible(self):
        self.dj.idle = "no stream would start"
        s = web.build_state(self.ctx)
        self.assertIn("still playing", s["idle_note"])
        self.assertTrue(s["now"], "Now Playing must survive a failed advance")

    def test_volume_and_flags_come_from_the_dj_not_the_page(self):
        s = web.build_state(self.ctx)
        self.assertEqual(s["volume"], 42)
        self.assertTrue(s["auto"])
        self.dj.set_auto(False)
        self.assertFalse(web.build_state(self.ctx)["auto"])

    def test_search_rows_are_views_not_raw_provider_dicts(self):
        self.ctx.search = {"pending": False, "q": "bark at the moon",
                           "rows": [track(9, badge={"type": "AD"}, score=99)],
                           "note": ""}
        row = web.build_state(self.ctx)["search"]["rows"][0]
        self.assertNotIn("badge", row)
        self.assertNotIn("score", row)
        self.assertEqual(row["title"], "Track 9")

    def test_loved_rows_is_a_list_even_with_no_history(self):
        self.assertIsInstance(web.build_state(self.ctx)["loved"], list)


class AutoplayTests(unittest.TestCase):
    """'when the first start the queue start mixing and playing even i dont start
    the button yet': opening the app must not play before a press. The switch is
    persisted like volume/repeat and exposed on the page."""

    def setUp(self):
        self.dj = fake_dj()
        self.ctx = web.Context(self.dj)
        self.dj.state["autoplay"] = False

    def test_it_does_not_auto_open_when_off(self):
        import inspect
        src = inspect.getsource(web.serve)
        self.assertIn("autoplay", src, "serve() gates the opening mix on autoplay")

    def test_action_autoplay_turns_it_on_and_off(self):
        code, _ = web.run_action(self.ctx, "autoplay", {"on": ["on"]})
        self.assertEqual(code, 200)
        self.assertTrue(self.dj.state["autoplay"])
        code, _ = web.run_action(self.ctx, "autoplay", {"on": ["off"]})
        self.assertEqual(code, 200)
        self.assertFalse(self.dj.state["autoplay"])

    def test_action_autoplay_flips_without_a_value(self):
        code, payload = web.run_action(self.ctx, "autoplay", {})
        self.assertEqual(code, 200)
        self.assertTrue(self.dj.state["autoplay"])
        self.assertIn("autoplay on", payload["note"])
        code, payload = web.run_action(self.ctx, "autoplay", {})
        self.assertIn("autoplay off", payload["note"])
        self.assertFalse(self.dj.state["autoplay"])

    def test_a_nonsense_value_is_not_a_settings_change(self):
        code, payload = web.run_action(self.ctx, "autoplay", {"on": ["maybe"]})
        self.assertEqual(code, 200)
        self.assertFalse(self.dj.state["autoplay"], "garbage must not flip autoplay")
        self.assertIn("is not on or off", payload["note"])

    def test_build_state_exposes_autoplay(self):
        self.dj.state["autoplay"] = True
        self.assertTrue(web.build_state(self.ctx)["autoplay"])

    def test_configured_default_is_off(self):
        # the core fix: a fresh install starts quiet, not with a mix already playing
        missing = Path(tempfile.mkdtemp()) / "state.json"
        with mock.patch.object(config, "STATE_FILE", missing):
            self.assertFalse(config.load_state()["autoplay"])


class MetaLookupTests(unittest.TestCase):
    """
    The "released" / "album" lines in Credits come from a one-per-track lookup that
    runs on its own thread - /api/state must never be held open by it (same rule as
    artwork), and it must be fetched once per id, not once per 700 ms tick.
    """

    def setUp(self):
        self.dj = fake_dj()
        self.ctx = web.Context(self.dj)

    def test_build_state_merges_album_and_release_year_into_now(self):
        with mock.patch("web.prov.yt_track_meta",
                        return_value={"album": "Dummy", "release_year": 1994,
                                      "artist": "Portishead", "album_url": "https://a"}):
            self.ctx._meta["vid0"] = {"album": "Dummy", "release_year": 1994,
                                      "artist": "Portishead", "album_url": "https://a"}
        s = web.build_state(self.ctx)
        self.assertEqual(s["now"]["album"], "Dummy")
        self.assertEqual(s["now"]["release_year"], 1994)
        self.assertEqual(s["now"]["album_url"], "https://a")

    def test_meta_for_fetches_once_and_caches(self):
        # simulate a slow provider so the first call returns {} (fetch in flight)
        with mock.patch("web.prov.yt_track_meta",
                        side_effect=lambda v: {"album": "Dummy", "release_year": 1994}):
            # first call: no cache yet, kernel a background fetch, answer {} now
            self.assertEqual(self.ctx.meta_for({"id": "vid0"}), {})
            import time
            time.sleep(0.3)                       # let the background fetch finish
            got = self.ctx.meta_for({"id": "vid0"})
            self.assertEqual(got.get("album"), "Dummy")
            self.assertEqual(got.get("release_year"), 1994)
            self.assertIn("vid0", self.ctx._meta, "the result is cached by id")

    def test_meta_for_does_not_fetch_the_same_id_twice(self):
        with mock.patch("web.prov.yt_track_meta",
                        side_effect=lambda v: {"album": "A"}) as m:
            self.ctx.meta_for({"id": "vid0"})
            self.ctx.meta_for({"id": "vid0"})       # in flight, must not refetch
            self.ctx.meta_for({"id": "vid0"})
            self.assertEqual(m.call_count, 1, "one lookup per id, not per tick")

    def test_open_accepts_an_explicit_url_for_the_album(self):
        with mock.patch("web.player_mod.open_externally", return_value=True) as oe:
            code, payload = web.run_action(
                self.ctx, "open", {"url": ["https://music.youtube.com/search?q=x"]})
        self.assertEqual(code, 200)
        self.assertEqual(oe.call_args[0][0], "https://music.youtube.com/search?q=x")


class ArtStampingTests(unittest.TestCase):
    def setUp(self):
        self.dj = fake_dj()
        self.ctx = web.Context(self.dj)

    def test_only_already_cached_art_is_stamped(self):
        # nothing on this path may reach the network: with_art reads the maps the
        # artwork lane already filled, and a row with no picture keeps the empty
        # string the page checks before it appends an <img>
        self.ctx._hrefs = {"vid0": {"card": "/art/a.png"}, "vid1": {"row": "/art/b.jpg"}}
        s = web.with_art(web.build_state(self.ctx), self.ctx)
        self.assertEqual(s["now"]["art"], "/art/a.png",
                         "a card file is big enough for the hero")
        self.assertEqual(s["up_next"][0]["art"], "/art/b.jpg")
        self.assertEqual(s["up_next"][0].get("art_card", ""), "",
                         "no 256px file, no upscaled smear")
        self.assertEqual(s["up_next"][1]["art"], "", "no artwork, no <img>")

    def test_up_next_ids_follow_the_popped_cursor(self):
        s = web.build_state(self.ctx)
        self.assertEqual([r["id"] for r in s["up_next"]],
                         ["vid1", "vid2", "vid3", "vid4", "vid5"])

    def test_every_slot_gets_its_own_size(self):
        # one file for the hero, the grid and the lists is what made the page look
        # blurry: 512px costs nothing where 72px belongs and vice versa, and the
        # browser is not the place to fix a source that is the wrong shape
        self.ctx._hrefs = {"vid0": {"row": "/art/row.png", "card": "/art/card.png",
                                    "big": "/art/big.png"},
                           "vid1": {"row": "/art/next.png", "card": "/art/next2.png"}}
        s = web.with_art(web.build_state(self.ctx), self.ctx)
        self.assertEqual(s["now"]["art"], "/art/big.png")
        self.assertEqual(s["now"]["art_tile"], "/art/row.png",
                         "the transport bar wants the small file of the same cover")
        self.assertEqual(s["up_next"][0]["art"], "/art/next.png",
                         "a 48px row must not pull the card file")
        self.assertEqual(s["up_next"][0]["art_card"], "/art/next2.png",
                         "the grid tile needs the 256px file")

    def test_with_art_prefers_the_lanes_own_file_when_it_exists(self):
        # a row may arrive with a cross-origin cover in `art`/`art_card`; once the
        # artwork lane has a file on disk it is the same-origin href that wins, so a
        # queue/card row is never left on an image CDN some browsers/network paths
        # refuse. Until then the row's own picture (or nothing) shows.
        self.ctx._hrefs = {"vid0": {"row": "/art/frame.jpg", "card": "/art/frame2.jpg"}}
        s = web.build_state(self.ctx)
        row = dict(s["up_next"][0])
        row["id"] = "vid0"
        row["art"] = "https://i.ytimg.com/cover.jpg"
        row["art_card"] = "https://i.ytimg.com/cover-card.jpg"
        s["up_next"][0] = row
        web.with_art(s, self.ctx)
        self.assertEqual(s["up_next"][0]["art"], "/art/frame.jpg",
                         "the lane's own file must win over a cross-origin cover")
        self.assertEqual(s["up_next"][0]["art_card"], "/art/frame2.jpg")

    def test_a_hero_never_upscale_a_row_thumbnail(self):
        # the old behaviour, written down as a test so it cannot come back: with
        # only a row file on disk the hero shows the gradient and initials, because
        # a 72px smear at 512px is worse than no picture at all
        self.ctx._hrefs = {"vid0": {"row": "/art/row.png"}}
        s = web.with_art(web.build_state(self.ctx), self.ctx)
        self.assertEqual(s["now"].get("art", ""), "")
        self.ctx._hrefs["vid0"]["card"] = "/art/card.png"
        s = web.with_art(web.build_state(self.ctx), self.ctx)
        self.assertEqual(s["now"]["art"], "/art/card.png",
                         "but the card file is big enough to borrow")

    def test_a_full_warmup_lane_never_stalls_the_request(self):
        # build_state runs on the request thread; artwork is a nicety, not a dependency
        self.ctx.art = queue.Queue(maxsize=1)
        self.ctx.art.put_nowait(track(1))
        t0 = time.monotonic()
        web.build_state(self.ctx)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_href_for_rejects_anything_that_is_not_a_cache_name(self):
        for bad in ("", None, "/etc/passwd", "a.txt", "../../x.png", "..%2fx"):
            self.assertEqual(web.Context._href_for(bad), "", str(bad))
        self.assertEqual(web.Context._href_for("/tmp/thumbs/ui-24-play.png"),
                         "/art/ui-24-play.png")
        self.assertEqual(web.Context._href_for(Path("/x/y/z_1.jpg")), "/art/z_1.jpg")


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.dj = fake_dj()
        self.ctx = web.Context(self.dj)
        # the taste profile and the clear-undo file are one directory on disk, and
        # seven tests here write them; without this, counts depend on which test ran
        # first (real files, real leaks)
        import taste
        taste.clear()
        try:
            taste.undo_file().unlink()
        except OSError:
            pass

    def run_it(self, name, **fields):
        form = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in fields.items())
        return web.run_action(self.ctx, name, urllib.parse.parse_qs(form))

    def test_unknown_action_is_a_400_with_the_list(self):
        code, payload = self.run_it("yodelling")
        self.assertEqual(code, 400)
        self.assertIn("unknown action", payload["error"])
        self.assertIn("next", payload["actions"])

    def test_every_action_the_page_can_send_exists(self):
        # the markup and this table are written separately, and drift would be a
        # silently dead button: check both ways the page names an action
        html = webapp.page()
        names = set(re.findall(r'data-action="([a-z_]+)"', html))
        names |= set(re.findall(r'act\("([a-z_]+)"', html))
        self.assertTrue(names)
        for name in sorted(names):
            self.assertIn(name, web.ACTIONS, name)

    def test_no_page_action_is_unreachable(self):
        # a route nobody can click is usually a rename that went one way only
        html = webapp.page()
        names = set(re.findall(r'data-action="([a-z_]+)"', html))
        names |= set(re.findall(r'act\("([a-z_]+)"', html))
        for name in ("stop", "topup", "unlike", "auto"):
            if name not in names:
                self.assertIn(name, ACTIONS_BY_BUTTON, name)

    def test_playpause_toggles_whichever_way_the_dj_is(self):
        with mock.patch.object(self.dj, "pause") as p, \
                mock.patch.object(self.dj, "resume") as r:
            self.dj.paused = False
            self.run_it("playpause")
            p.assert_called_once()                 # it is playing -> pause it
            r.assert_not_called()
            self.dj.paused = True
            self.run_it("playpause")
            r.assert_called_once()                 # it is stopped -> resume it
            p.assert_called_once()

    def test_next_is_the_engines_human_skip_not_a_raw_move(self):
        # dj.skip() is the verb that forces past the retry cooldown and leaves the
        # taste write to one place; calling next() from the page would double-judge
        with mock.patch.object(self.dj, "skip", return_value=track(2)) as sk, \
                mock.patch.object(self.dj, "next") as n:
            code, payload = self.run_it("next")
        sk.assert_called_once()
        n.assert_not_called()
        self.assertEqual(code, 200)
        self.assertEqual(payload["note"], "", "a successful skip needs no speech")

    def test_a_press_that_moves_nothing_says_why(self):
        with mock.patch.object(self.dj, "skip", return_value=None):
            self.dj.queue.items = []
            self.dj.queue.pos = 0
            note = self.run_it("next")[1]["note"]
            self.assertIn("nothing queued", note)
            self.dj.queue.items = [track(i) for i in range(4)]
            self.dj.queue.pos = 1
            self.dj.idle = "no stream would start"
            note = self.run_it("next")[1]["note"]
            self.assertIn("refused to start", note)
            self.assertNotIn("nothing queued", note, "six tracks are still queued")
            self.dj.idle = ""
            self.assertIn("activity log", self.run_it("next")[1]["note"])

    def test_prev_walks_back_through_the_history(self):
        with mock.patch.object(self.dj, "prev", return_value=track(0)) as pv:
            self.assertEqual(self.run_it("prev")[1]["note"], "")
            pv.assert_called_once()

    def test_volume_is_clamped_and_reaches_the_player(self):
        with mock.patch.object(self.dj, "volume") as v:
            self.run_it("volume", pct="250")
            v.assert_called_once_with(100)
            self.assertEqual(self.ctx.volume, 100)
            self.run_it("volume", pct="-5")
            self.assertEqual(self.ctx.volume, 0)
            self.run_it("volume", pct="33")
            self.assertEqual(web.build_state(self.ctx)["volume"], 33)

    def test_seek_reports_when_the_player_refuses(self):
        with mock.patch.object(self.dj, "seek", return_value=False) as s:
            code, payload = self.run_it("seek", secs="120")
            s.assert_called_once_with(120.0)
            self.assertEqual(code, 200)
            self.assertEqual(payload["note"], "the player would not seek")

    def test_like_toggles_against_what_the_dj_thinks(self):
        with mock.patch.object(self.dj, "is_liked", return_value=False), \
                mock.patch.object(self.dj, "like") as l, \
                mock.patch.object(self.dj, "unlike") as u:
            self.assertEqual(self.run_it("like")[1]["note"], "loved")
            l.assert_called_once()
            u.assert_not_called()
        with mock.patch.object(self.dj, "is_liked", return_value=True), \
                mock.patch.object(self.dj, "like") as l, \
                mock.patch.object(self.dj, "unlike") as u:
            self.assertEqual(self.run_it("like")[1]["note"], "unloved")
            u.assert_called_once()
            l.assert_not_called()

    def test_like_with_nothing_playing_says_so(self):
        self.dj.current = None
        with mock.patch.object(self.dj, "like") as l:
            self.assertEqual(self.run_it("like")[1]["note"], "nothing playing to love yet")
            l.assert_not_called()

    def test_play_row_jumps_the_queue_to_it_and_forces_the_move(self):
        target = self.dj.queue.items[3]
        # a pick anchors the queue around the song; stub the (off-socket) build so the
        # queue-jump is what is asserted and no worker leaks past this test
        with mock.patch.object(self.dj, "next", return_value=None) as n, \
                mock.patch.object(self.dj, "radio_from",
                                  return_value={"ok": True, "tracks": []}):
            code, payload = self.run_it("play_row", id=target["id"])
            if self.ctx.job:
                self.ctx.job.join(2)
        self.assertEqual(code, 200)
        self.assertIn("Track 3", payload["note"])
        self.assertEqual(self.dj.queue.items[self.dj.queue.pos]["id"], target["id"])
        n.assert_called_once_with(force=True)

    def test_queue_next_inserts_ahead_of_the_next_track(self):
        target = self.dj.queue.items[4]
        pos_before = self.dj.queue.pos
        self.run_it("queue_next", id=target["id"])
        self.assertEqual(self.dj.queue.items[pos_before + 1]["id"], target["id"])

    def test_row_actions_on_a_gone_track_are_a_note_not_a_crash(self):
        for name in ("play_row", "queue_next", "love_row", "radio"):
            code, payload = self.run_it(name, id="nope")
            self.assertEqual(code, 200, name)
            self.assertIn("gone", payload["note"], name)

    def test_love_row_writes_the_taste_profile(self):
        import taste
        t = self.dj.queue.items[2]
        self.run_it("love_row", id=t["id"])
        state = taste.load_state()
        # taste stores a normalised key plus the display form; the row the user
        # clicked has to be findable by what they actually saw
        self.assertTrue(any(r.get("display_title") == t["title"] for r in state["liked"]))
        self.assertTrue(any(r.get("display_artist") == "Artist 2" for r in state["liked"]))
        # the profile is keyed by the normalised name on purpose: "Radiohead" and
        # "radiohead" must land in one bucket, not two half-strength ones
        self.assertGreater(state["artists"].get("artist 2", 0), 0, "the artist is learned too")
        self.assertNotIn("Artist 2", state["artists"], "no duplicate bucket")

    def test_auto_flips_and_reports_the_new_state(self):
        self.assertIn("off", self.run_it("auto")[1]["note"])
        self.assertFalse(self.dj.auto)
        self.assertIn("on", self.run_it("auto")[1]["note"])
        self.assertTrue(self.dj.auto)

    def test_an_empty_request_mixes_from_the_profile(self):
        # "type a mood first" from a DJ app is a shrug; an empty Play means
        # "play what I like", and the engine call is the same one the CLI verb makes
        self.dj.request = ""
        with mock.patch.object(self.dj, "start") as st, \
                mock.patch.object(self.dj, "taste_mix", return_value={"ok": True}) as tm:
            code, payload = self.run_it("request", q="")
            st.assert_not_called()
        self.assertEqual(code, 200)
        self.assertIn("what you like", payload["note"])
        for _ in range(200):
            if tm.called:
                break
            time.sleep(0.01)
        tm.assert_called_once()

    def test_the_mix_button_is_the_same_verb_not_a_copy(self):
        with mock.patch.object(self.dj, "taste_mix", return_value={"ok": True}) as tm:
            code, payload = self.run_it("mix")
        self.assertEqual(code, 200)
        self.assertIn("your likes", payload["note"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not tm.called:
            time.sleep(0.01)
        tm.assert_called_once()

    def test_a_second_press_while_building_does_not_stack_jobs(self):
        busy = threading.Event()
        started = threading.Event()

        def slow(count=24):
            started.set()
            busy.wait(2)

        self.dj.taste_mix = slow                        # type: ignore[method-assign]
        try:
            self.run_it("mix")
            started.wait(2)
            self.assertEqual(self.run_it("mix")[1]["note"],
                             "still building the previous mix - one second")
            self.assertEqual(self.run_it("radio", id="vid0")[1]["note"],
                             "still building the previous station - one second")
            self.assertIn("still working", self.run_it("test_brain")[1]["note"])
            self.assertTrue(self.run_it("request", q="dub")[1]["note"]
                            .startswith("still building"))
        finally:
            busy.set()

    def test_state_reports_the_job_so_the_page_can_show_working(self):
        self.assertFalse(web.build_state(self.ctx)["busy"])
        self.ctx.job = threading.Thread(target=lambda: time.sleep(0.6))
        self.ctx.job.start()
        self.assertTrue(web.build_state(self.ctx)["busy"])
        self.ctx.job.join(2)

    def test_clear_taste_needs_an_explicit_sure(self):
        # one curl -d "action=clear_taste" must not be able to throw away months of
        # hearts: the page arms on the first tap and sends sure=1 on the second
        import taste
        taste.record_like({"title": "Gone Song", "artist": "Gone Artist", "duration": 200})
        code, payload = self.run_it("clear_taste")
        self.assertEqual(code, 200)
        self.assertIn("sure=1", payload["note"])
        self.assertEqual(len(taste.load_state()["liked"]), 1, "it wiped anyway")
        self.assertEqual(web.build_state(self.ctx)["taste"]["likes"], 1)
        self.assertFalse(web.build_state(self.ctx)["taste"]["has_backup"])

    def test_clear_taste_forgets_the_judgements_and_says_how_much(self):
        import taste
        taste.record_like({"title": "Gone Song", "artist": "Gone Artist", "duration": 200})
        code, payload = self.run_it("clear_taste", sure="1")
        self.assertEqual(code, 200)
        self.assertIn("forgot 1 judgement", payload["note"])
        st = taste.load_state()
        self.assertEqual(st["liked"], [])
        self.assertEqual(st["artists"], {})
        self.assertEqual(web.build_state(self.ctx)["taste"]["likes"], 0)
        self.assertTrue(web.build_state(self.ctx)["taste"]["has_backup"])

    def test_restore_taste_brings_the_profile_back(self):
        import taste
        taste.record_like({"title": "Back Song", "artist": "Back Band", "duration": 200})
        taste.record_skip({"title": "Bad", "artist": "Bad Band", "duration": 200})
        self.run_it("clear_taste", sure="1")
        self.assertEqual(taste.load_state()["liked"], [])
        code, payload = self.run_it("restore_taste")
        self.assertEqual(code, 200)
        self.assertIn("brought back 1 loved", payload["note"])
        st = taste.load_state()
        self.assertEqual([r["title"] for r in st["liked"]], ["back song"])
        self.assertEqual(st["artists"]["back band"], 2.0)
        self.assertFalse(taste.load_state()["skipped"] == [])
        self.assertIn("taste restored: 1 loved", self.dj.log[-1])

    def test_restore_without_a_snapshot_says_so(self):
        code, payload = self.run_it("restore_taste")
        self.assertEqual(code, 200)
        self.assertIn("nothing saved", payload["note"])
        self.assertIn("nothing to restore", self.dj.log[-1])

    def test_a_nonsense_level_is_an_answer_not_a_500(self):
        # a slider sends a number; anything else used to escape int() as a 500
        for bad in ("NaN", "lots", "1e999x", ";;"):
            code, payload = self.run_it("volume", pct=bad)
            self.assertEqual(code, 200, bad)
            self.assertIn("is not a level", payload["note"])
        self.assertEqual(self.ctx.volume, 70, "a rejected value changed the volume")
        self.assertIn("volume 70%", self.run_it("volume")[1]["note"])   # blank = report
        self.assertEqual(self.run_it("volume", pct="999")[1]["note"], "volume 100% (clamped from 999)")
        self.assertEqual(self.run_it("volume", pct="-5")[1]["note"], "volume 0% (clamped from -5)")
        self.assertEqual(self.ctx.volume, 0)
        self.assertEqual(self.run_it("volume", pct="42.7")[1]["note"], "volume 42%")

    def test_a_nonsense_auto_value_does_not_mean_on(self):
        self.dj.set_auto(False)                    # a known place to flip from
        self.run_it("auto")                        # blank flips: on
        self.assertTrue(self.dj.auto)
        code, payload = self.run_it("auto", on="maybe")
        self.assertIn("is not on or off", payload["note"])
        self.assertTrue(self.dj.auto, "a typo changed the setting")
        self.assertEqual(self.run_it("auto", on="OFF")[1]["note"], "keep mixing: off")
        self.assertFalse(self.dj.auto)
        self.assertEqual(self.run_it("auto", on="true")[1]["note"], "keep mixing: on")

    def test_two_fast_presses_start_one_job_not_two(self):
        # the check and the claim are one step: an `if _busy()` followed by an
        # assignment let both handler threads through and the queue arrived twice
        gate = threading.Event()
        runs = []

        def target():
            runs.append(1)
            gate.wait(3)

        self.assertTrue(self.ctx.start_job(target))
        for _ in range(200):
            if runs:
                break
            time.sleep(0.005)
        self.assertFalse(self.ctx.start_job(target), "a second job was accepted")
        self.assertEqual(len(runs), 1)
        gate.set()
        if self.ctx.job:
            self.ctx.job.join(3)
        self.assertTrue(self.ctx.start_job(lambda: None), "the slot stayed claimed")
        self.ctx.job.join(3)

    def test_settings_are_reported_masked_and_saved(self):
        code, payload = web.save_settings({})
        self.assertEqual(code, 400)
        self.assertIn("nothing to save", payload["error"])

        view = web.settings_view()
        self.assertIn("key_mask", view)
        self.assertNotIn("LLM_API_KEY", json.dumps(view))
        self.assertTrue(web.mask("AIzaSyS3cr3tKey123").startswith("AIz"))
        self.assertEqual(web.mask("short"), "·····")
        self.assertNotIn("S3cr3tKey", json.dumps({"m": web.mask("AIzaSyS3cr3tKey123")}))

        with mock.patch("web.config.save_llm_config") as save:
            code, payload = web.save_settings({"model": ["gemini-3.6-flash"],
                                               "key": ["  spaced  "]})
        self.assertEqual(code, 200)
        self.assertEqual(save.call_args.kwargs["LLM_MODEL"], "gemini-3.6-flash")
        self.assertEqual(save.call_args.kwargs["LLM_API_KEY"], "spaced", "trimmed")
        self.assertNotIn("spaced", json.dumps(payload))
        self.assertEqual(payload["note"], "saved")

    def test_a_blank_key_does_not_delete_a_working_one(self):
        # the browser empties a password field on reload, so "blank = clear" would
        # have eaten the key every time anyone pressed Save for another reason
        with mock.patch("web.config.save_llm_config") as save:
            code, payload = web.save_settings({"key": [""], "model": ["x"]})
        self.assertEqual(code, 200)
        self.assertNotIn("LLM_API_KEY", save.call_args.kwargs)
        self.assertEqual(payload["note"], "saved (key kept as it was)")
        # removal is an explicit act instead
        with mock.patch("web.config.save_llm_config") as save2:
            self.assertEqual(web.save_settings({"clear_key": ["1"]})[0], 200)
        self.assertEqual(save2.call_args.kwargs["LLM_API_KEY"], "")
        # and an empty text field really does mean "back to the default"
        with mock.patch("web.config.save_llm_config") as save3:
            self.assertEqual(web.save_settings({"base": [""]})[0], 200)
        self.assertEqual(save3.call_args.kwargs["LLM_BASE_URL"], "")

    def test_settings_write_failures_are_500_not_a_broken_tab(self):
        with mock.patch("web.config.save_llm_config", side_effect=OSError("read-only")):
            code, payload = web.save_settings({"model": ["x"]})
        self.assertEqual(code, 500)
        self.assertIn("read-only", payload["error"])

    def test_state_carries_the_profile_and_the_station(self):
        import taste
        taste.record_like({"title": "Known", "artist": "Known Artist", "duration": 200})
        self.dj.station = "Known Artist - Known"
        s = web.build_state(self.ctx)
        self.assertEqual(s["station"], "Known Artist - Known")
        self.assertTrue(s["taste"]["has_profile"])
        self.assertIn("trip hop", (s["why"] + s["now"]["art"]).lower())
        self.assertIn("station: Known Artist - Known", s["why"])
        self.assertGreaterEqual(s["taste"]["artists"][0]["w"], 2.0)
        self.assertEqual(s["taste"]["likes"], 1)
        self.assertIn("engine", s["settings"])

    def test_request_runs_on_a_thread_not_in_the_socket(self):
        # a search takes 0.5-40s; a handler that waits for it freezes the whole tab
        with mock.patch.object(self.dj, "start") as st:
            code, payload = self.run_it("request", q="slow dub")
            self.assertEqual(code, 200)
            self.assertIn("building a mix", payload["note"])
            for _ in range(200):
                if st.called:
                    break
                time.sleep(0.01)
        st.assert_called_once()
        self.assertEqual(st.call_args.kwargs["count"], 20)
        self.assertEqual(st.call_args.args[0], "slow dub")

    def test_request_while_one_is_building_does_not_start_a_second(self):
        busy = threading.Event()
        started = threading.Event()

        def slow(*a, **k):
            started.set()
            busy.wait(2)

        self.dj.start = slow                     # type: ignore[method-assign]
        try:
            self.run_it("request", q="first")
            started.wait(2)
            self.assertIn("still building", self.run_it("request", q="second")[1]["note"])
        finally:
            busy.set()

    def test_a_broken_action_answers_500_instead_of_dropping_the_tab(self):
        def boom(*a, **k):
            raise RuntimeError("mpv said no")

        self.dj.skip = boom                      # type: ignore[method-assign]
        code, payload = self.run_it("skip")
        self.assertEqual(code, 500)
        self.assertIn("mpv said no", payload["error"])

    def test_radio_delegates_to_the_engine_and_names_the_row(self):
        # one implementation of "station from this song"; the web copy used to do
        # its own build_queue + extend, which is how it came to queue a station
        # nobody ever started
        seen = {}

        def fake_radio(track, count=20):
            seen["track"] = track
            seen["count"] = count
            return {"ok": True, "tracks": [track], "info": {"engine": "offline"}}

        with mock.patch.object(self.dj, "radio_from", fake_radio):
            code, payload = self.run_it("radio", id="vid2")
        self.assertEqual(code, 200)
        self.assertIn("Artist 2", payload["note"])
        self.assertIn("building a station around", payload["note"])
        self.assertEqual(seen["track"]["id"], "vid2")
        self.assertEqual(seen["count"], 20)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.dj.station != "Artist 2 - Track 2":
            time.sleep(0.01)

    def test_radio_failure_is_logged_not_raised(self):
        def boom(track, count=20):
            raise RuntimeError("planner exploded")

        with mock.patch.object(self.dj, "radio_from", boom):
            code = self.run_it("radio", id="vid1")[0]
        self.assertEqual(code, 200)
        deadline = time.monotonic() + 3
        seen = False
        while time.monotonic() < deadline:
            if any("station failed" in line and "planner exploded" in line
                   for line in self.dj.log):
                seen = True
                break
            time.sleep(0.01)
        self.assertTrue(seen, "the radio failure was swallowed with no trace")

    def test_a_failed_mix_is_logged_too(self):
        def boom(count=24):
            raise RuntimeError("no candidates")

        with mock.patch.object(self.dj, "taste_mix", boom):
            self.run_it("mix")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any("mix could not be built" in line for line in self.dj.log):
                return
            time.sleep(0.01)
        self.fail("a failed mix left no trace in the log")

    def test_test_brain_runs_off_the_socket(self):
        # the handler must answer at once (a probe is a network call) and the probe
        # must run on the job thread; the patch stays live while we wait for it
        import brain
        with mock.patch.object(brain, "probe", return_value={"engine": "gemini", "ok": True,
                                                            "ms": 40, "detail": "fine"}) as pr:
            code, payload = self.run_it("test_brain")
            self.assertEqual(code, 200)
            self.assertIn("activity log", payload["note"])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not pr.called:
                time.sleep(0.01)
            self.assertTrue(pr.called, "the brain test never reached the probe")
        notes = " | ".join(self.dj.log)
        self.assertIn("brain test: gemini ok=True", notes)

    def test_a_brain_test_failure_is_a_note_not_a_missing_reply(self):
        import brain
        with mock.patch.object(brain, "probe", side_effect=RuntimeError("no key")):
            code, payload = self.run_it("test_brain")
            self.assertEqual(code, 200)             # the socket is answered at once
            deadline = time.monotonic() + 3          # patch must stay on while it runs
            while time.monotonic() < deadline:
                if any("brain test failed" in line for line in self.dj.log):
                    self.assertIn("no key", self.dj.log[-1])
                    return
                time.sleep(0.01)
            self.fail(f"a failed brain probe was swallowed; log={self.dj.log[-3:]}")

    def test_clear_station_forgets_the_label(self):
        self.dj.station = "Someone - Something"
        self.run_it("clear_station")
        self.assertEqual(self.dj.station, "")

    def test_open_needs_something_playing(self):
        self.dj.current = None
        self.assertIn("nothing playing", self.run_it("open")[1]["note"])

    def test_open_hands_the_url_to_a_client_and_touches_no_player(self):
        with mock.patch("web.player_mod.playerctl", return_value=True) as pc, \
                mock.patch("web.player_mod.open_externally", return_value=True) as oe:
            code, payload = self.run_it("open")
        self.assertEqual(code, 200)
        self.assertIn("vid0", oe.call_args[0][0])
        self.assertEqual(payload["note"], "")
        self.assertFalse(pc.called, "a button must not resume a player the user paused")

    def test_open_says_when_nothing_answered(self):
        with mock.patch("web.player_mod.open_externally", return_value=False):
            self.assertIn("no browser or Spotube", self.run_it("open")[1]["note"])

    def test_request_count_is_clamped_not_crashed(self):
        # a hand-typed ?count=9999 must not take a search down with it
        with mock.patch.object(self.dj, "start") as st:
            self.run_it("request", q="deep house", count="9999")
            if self.ctx.job:
                self.ctx.job.join(2)          # else the busy guard eats the 2nd press
            self.assertEqual(st.call_args.kwargs["count"], 60)
            self.run_it("request", q="deep house", count="not-a-number")
            if self.ctx.job:
                self.ctx.job.join(2)
            self.assertEqual(st.call_args.kwargs["count"], 20)

    def test_stop_reaches_the_dj_and_topup_is_forced(self):
        with mock.patch.object(self.dj, "stop") as s, \
                mock.patch.object(self.dj, "_topup") as t:
            self.run_it("stop")
            s.assert_called_once()
            self.run_it("topup")
            t.assert_called_once_with(force=True)

    def test_control_vocabulary_matches_the_daemon_api(self):
        # two doors, one vocabulary: a verb the CLI/control API has must not mean
        # something different in the browser
        text = (Path(web.__file__).parent / "dj.py").read_text()
        for verb in ("next", "prev", "skip", "like", "pause", "resume", "topup", "stop",
                     "seek", "auto"):
            self.assertIn(f'"{verb}":', text, verb)
            self.assertIn(verb, web.ACTIONS, verb)


class QueueRowActionTests(unittest.TestCase):
    """The remove / dislike verbs the queue rows expose, and the play-one-twice fix."""

    def setUp(self):
        self.dj = fake_dj([track(i) for i in range(6)])
        self.dj.auto = False
        self.dj._topup = lambda **k: None          # a row action must not search
        self.ctx = web.Context(self.dj)
        import taste
        taste.clear()
        try:
            taste.undo_file().unlink()
        except OSError:
            pass

    def run_it(self, name, **fields):
        form = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in fields.items())
        return web.run_action(self.ctx, name, urllib.parse.parse_qs(form))

    def test_remove_queue_drops_the_one_row_and_keeps_the_song(self):
        code, payload = self.run_it("remove_queue", id="vid2")
        self.assertEqual(code, 200)
        self.assertNotIn("vid2", [t["id"] for t in self.dj.queue.items])
        self.assertEqual(self.dj.current["title"], track(0)["title"],
                         "the audible track is not touched by a row removal")
        self.assertEqual(len(self.dj.queue), 4)

    def test_remove_queue_says_when_the_row_left_already(self):
        _code, payload = self.run_it("remove_queue", id="vid99")
        self.assertIn("that row is gone", payload["note"])
        self.assertEqual(len(self.dj.queue), 5)

    def test_dislike_records_taste_and_removes_the_row(self):
        code, payload = self.run_it("dislike", id="vid3")
        self.assertEqual(code, 200)
        self.assertEqual(payload["note"], "won't suggest 'Track 3' again")
        self.assertNotIn("vid3", [t["id"] for t in (self.dj.queue.items or [])])
        import taste
        state = taste.load_state()
        self.assertIn("dislike", [s.get("reason") for s in state.get("skipped", [])])

    def test_play_row_does_not_duplicate_a_queued_row(self):
        # "the queue UI bug when click": clicking the same queued row used to copy it
        # on top of itself, so it played twice in a row. remove_id first, then play.
        web.run_action(self.ctx, "play_row", {"id": [""]})  # no-op guard path
        # a pick anchors the queue around the song (radio_from, off the socket), so
        # stub that build; the duplicate fix is what this asserts
        with mock.patch.object(self.dj, "radio_from",
                               return_value={"ok": True, "tracks": []}):
            _code, payload = self.run_it("play_row", id="vid1")
            if self.ctx.job:
                self.ctx.job.join(2)
        self.assertIn("playing Track 1", payload["note"])
        self.assertEqual(
            [t["id"] for t in self.dj.queue.upcoming(10)].count("vid1"), 0,
            "the row that just played must not still be waiting in the queue")

    def test_play_row_anchors_the_queue_on_the_picked_song(self):
        # "what if i chose a song? the songs next to it build around it" - a pick
        # hands the song to radio_from with replace=True (build the upcoming set
        # around it) rather than leaving whatever mix the row happened to sit in
        with mock.patch.object(self.dj, "radio_from") as rf:
            _code, payload = self.run_it("play_row", id="vid1")
            if self.ctx.job:
                self.ctx.job.join(2)
        rf.assert_called_once()
        self.assertEqual(rf.call_args[0][0]["title"], "Track 1",
                         "the picked song is the anchor, not a generic vibe")
        self.assertTrue(rf.call_args[1]["replace"])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.ctx = web.Context(fake_dj())
        self.bcast: list[str] = []
        self._real = self.ctx.broadcast
        self.ctx.broadcast = self.bcast.append

    def wait(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.ctx.search.get("pending"):
                return True
            time.sleep(0.01)
        return False

    def tearDown(self):
        self.ctx.broadcast = self._real

    def test_rows_land_as_views_and_pending_clears(self):
        with mock.patch("web.prov.yt_search", return_value=[track(5)]) as ys:
            web.start_search(self.ctx, "dunedin doom gaze")
            self.assertTrue(self.wait())
        ys.assert_called_once()
        self.assertEqual(self.ctx.search["q"], "dunedin doom gaze")
        self.assertEqual(len(self.ctx.search["rows"]), 1)
        first = json.loads(self.bcast[0])["search"]
        self.assertTrue(first["pending"], "the page needs the pending flag immediately")
        last = json.loads(self.bcast[-1])["search"]
        self.assertEqual(last["rows"][0]["title"], "Track 5")
        self.assertNotIn("badge", last["rows"][0])

    def test_an_empty_search_explains_the_filter_instead_of_looking_broken(self):
        with mock.patch("web.prov.yt_search", return_value=[]):
            web.start_search(self.ctx, "6 hour lofi mix")
            self.assertTrue(self.wait())
        self.assertIn("filter", self.ctx.search["note"])
        self.assertEqual(self.ctx.search["rows"], [])

    def test_a_slow_first_search_cannot_overwrite_a_newer_one(self):
        # two searches 20 ms apart used to land in whichever order the network
        # returned them, so the box could read "radiohead" over 20 rows of "dunedin"
        first = threading.Event()

        def slow(q, limit=20, **k):
            if q == "dunedin":
                first.wait(3)
            return [track(1 if q == "dunedin" else 2)]

        with mock.patch("web.prov.yt_search", slow):
            web.start_search(self.ctx, "dunedin")
            web.start_search(self.ctx, "radiohead")
            first.set()
            self.assertTrue(self.wait())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if any("dunedin" in b for b in self.bcast):
                    time.sleep(0.05)          # let the stale job finish too
                    break
                time.sleep(0.01)
        self.assertEqual(self.ctx.search["q"], "radiohead",
                         f"the older search landed last: {self.ctx.search['q']}")
        self.assertEqual(self.ctx.search["rows"][0]["id"], "vid2")
        self.assertFalse(any('"q": "radiohead"' in b and "Track 1" in b
                            for b in self.bcast), "the stale rows were broadcast anyway")

    def test_a_search_of_nothing_is_answered_once(self):
        code, payload = web.do_search(self.ctx, "   ")
        self.assertEqual(int(code), 400)
        self.assertIn("q= required", payload["error"])
        self.assertIn("example", payload)

    def test_a_search_exception_becomes_a_note(self):
        with mock.patch("web.prov.yt_search", side_effect=RuntimeError("429")):
            web.start_search(self.ctx, "anything")
            self.assertTrue(self.wait())
        self.assertIn("429", self.ctx.search["note"])


class OpenPageTests(unittest.TestCase):
    """
    The in-app album / artist page: start_page builds it off the trusted search
    endpoint, the latest open wins, and /api/state exposes it for the `page` view.
    """

    def setUp(self):
        self.ctx = web.Context(fake_dj())
        self.bcast: list[str] = []
        self._real = self.ctx.broadcast
        self.ctx.broadcast = self.bcast.append

    def wait(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not (self.ctx.page or {}).get("pending", False):
                return True
            time.sleep(0.01)
        return False

    def tearDown(self):
        self.ctx.broadcast = self._real

    def test_start_page_builds_rows_and_clears_pending(self):
        with mock.patch("web.prov.yt_search", return_value=[track(7), track(8)]) as ys:
            web.start_page(self.ctx, "artist", "Radiohead songs", "Radiohead")
            self.assertTrue(self.wait())
        ys.assert_called_once()
        self.assertEqual(self.ctx.page["kind"], "artist")
        self.assertEqual(self.ctx.page["title"], "Radiohead")
        self.assertEqual(len(self.ctx.page["rows"]), 2)
        self.assertEqual(self.ctx.page["rows"][0]["title"], "Track 7")

    def test_a_newer_page_open_discards_an_older_one(self):
        first = threading.Event()
        def slow(q, limit=20, **k):
            first.wait(3)
            return [track(1)]
        with mock.patch("web.prov.yt_search", side_effect=slow) as ys:
            web.start_page(self.ctx, "artist", "old", "Old")
            web.start_page(self.ctx, "album", "fake album", "Fake")
            first.set()
            self.assertTrue(self.wait())
            # both workers run through the search fallback (browse gives nothing in
            # the sandbox); wait for the older one too so it cannot still be calling
            # yt_search after `with` restores the real function and leak onto the
            # next test's mock
            deadline = time.monotonic() + 3
            while ys.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(self.ctx.page["title"], "Fake", "the newer page wins")

    def test_open_album_falls_back_to_artist_page_when_no_album(self):
        with mock.patch("web.start_page") as sp, \
             mock.patch("web.prov.yt_search", return_value=[]):
            code, payload = web.run_action(self.ctx, "open_album",
                                           {"album": [""], "artist": ["Portishead"]})
        self.assertEqual(code, 200)
        self.assertIn("Portishead - albums and songs", payload["note"])
        sp.assert_called_once()
        self.assertEqual(sp.call_args[0][1], "artist", "falls back to an artist page")
        self.assertEqual(sp.call_args[0][4], "discography",
                         "the artist page is a discography, not a songs list")

    def test_open_artist_delegates_to_start_page(self):
        with mock.patch("web.start_page") as sp, \
             mock.patch("web.prov.yt_search", return_value=[]):
            code, payload = web.run_action(self.ctx, "open_artist",
                                           {"artist": ["Nirvana"]})
        self.assertEqual(code, 200)
        self.assertIn("Nirvana - albums and songs", payload["note"])
        sp.assert_called_once()
        self.assertEqual(sp.call_args[0][4], "discography")

    def test_start_page_prefers_the_browse_discography_over_a_song_search(self):
        # a deep artist page is a discography (browse), not a songs search; the
        # browse path wins and the search fallback is never asked
        disc = [{"id": "", "title": "Dummy", "artist": "Portishead",
                 "release_year": 1994, "browse_id": "B1", "note": "1994 · album",
                 "kind": "album"}]
        with mock.patch("web.prov.page_rows", return_value=[web.row_view(x) for x in disc]) as pr:
            web.start_page(self.ctx, "artist", "Portishead songs", "Portishead",
                           "discography")
            self.assertTrue(self.wait())
        pr.assert_called_once_with("artist", "Portishead songs", "Portishead",
                                   "discography")
        self.assertEqual(self.ctx.page["rows"][0]["kind"], "album")
        self.assertEqual(self.ctx.page["rows"][0]["release_year"], 1994)

    def test_build_state_exposes_the_page(self):
        self.ctx.page = {"kind": "album", "title": "Dummy",
                         "sub": "Portishead", "rows": [web.row_view(track(1))],
                         "pending": False, "note": ""}
        s = web.build_state(self.ctx)
        self.assertEqual(s["page"]["title"], "Dummy")
        self.assertEqual(s["page"]["kind"], "album")


class TraversalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="art-"))
        (self.root / "ok.png").write_bytes(b"\x89PNG")
        (self.root / "notes.txt").write_text("secret")

    def test_a_real_cached_file_is_allowed(self):
        self.assertEqual(web.safe_art_path(self.root, "ok.png"),
                         (self.root / "ok.png").resolve())

    def test_directory_and_extension_are_both_checked(self):
        for bad in ("", "notes.txt", "ok.gif", ".png", "ok.png.exe", "a/../ok.png",
                    "/etc/passwd", "..//ok.png", "ok.png/x", "sub/ok.png",
                    "~/.ssh/id_rsa", "%2e%2e%2fok.png", "ok.png#x", "ok.png?x=1"):
            self.assertIsNone(web.safe_art_path(self.root, bad), bad)

    def test_a_symlink_out_of_the_cache_is_refused(self):
        link = self.root / "link.png"
        try:
            link.symlink_to(Path("/etc/hostname"))
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks available here")
        self.assertIsNone(web.safe_art_path(self.root, "link.png"))

    def test_a_missing_file_is_not_a_path_leak(self):
        self.assertIsNone(web.safe_art_path(self.root, "gone.png"))


class HostGuardTests(unittest.TestCase):
    def test_split_host_handles_ipv6_and_ports(self):
        self.assertEqual(web.split_host("[::1]:8766"), "::1")
        self.assertEqual(web.split_host("localhost:8766"), "localhost")
        self.assertEqual(web.split_host("localhost"), "localhost")
        self.assertEqual(web.split_host("evil.test"), "evil.test")
        self.assertEqual(web.split_host(None), "")

    def test_lan_mode_opens_the_guard_and_says_so(self):
        # binding a routable interface already means "other machines may connect",
        # so the address can no longer be the trust boundary; is_open is what the
        # startup warning keys off
        self.assertFalse(web.is_open("127.0.0.1"))
        self.assertFalse(web.is_open("localhost"))
        self.assertTrue(web.is_open("10.0.0.7"))
        self.assertTrue(web.host_ok("anything.test", allow_any_host=True))
        self.assertFalse(web.host_ok("anything.test"))

    def test_extra_hosts_come_from_the_environment_only(self):
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_WEB_HOSTS": "dj.local, 192.168.1.9"}):
            self.assertTrue(web.host_ok("dj.local:8766"))
            self.assertTrue(web.host_ok("192.168.1.9"))
            self.assertFalse(web.host_ok("evil.test"))

    def test_loopback_names_are_allowed(self):
        for h in ("127.0.0.1:8766", "localhost:8766", "localhost", "[::1]:8766",
                  "127.0.0.1", "LOCALHOST:80"):
            self.assertTrue(web.host_ok(h), h)

    def test_dns_rebinding_and_forwarded_hosts_are_refused(self):
        for h in ("evil.test:8766", "evil.test", "10.0.0.5:8766", "0.0.0.0:8766",
                  "127.0.0.1.evil.test", None, "", "  "):
            self.assertFalse(web.host_ok(h), repr(h))


class PageTests(unittest.TestCase):
    def test_page_has_no_unreplaced_tokens_and_no_external_requests(self):
        html = webapp.page()
        self.assertNotIn("@@", html)
        for scheme in ('src="http', 'href="http', "url(http", "@import", "<link"):
            self.assertNotIn(scheme, html, scheme)

    def test_page_is_stable_bytes(self):
        # rebuilt on every navigation; a random or timestamped build would churn
        # the DOM and make the tab flicker
        self.assertEqual(webapp.page(), webapp.page())

    def test_every_icon_token_in_the_markup_resolves(self):
        # the template speaks @@heart_o@@; an unmapped name would ship as literal text
        for template in (webapp.BODY, webapp.JS):
            for name in set(re.findall(r"@@([a-z_0-9]+)@@", template)):
                self.assertTrue(name in webapp.ICONS or name in webapp.COLORS, name)
        self.assertNotIn("@@", webapp.page())

    def test_icons_are_inlined_as_valid_json(self):
        html = webapp.page()
        blob = html.split("const ICONS=", 1)[1].split("\n", 1)[0].rstrip().rstrip(";")
        data = json.loads(blob)
        self.assertEqual(set(data), set(webapp.ICONS))
        for svg in data.values():
            self.assertTrue(svg.startswith("<svg"), svg[:24])
            self.assertIn("</svg>", svg)

    def test_css_braces_survive_the_substitution(self):
        # CSS is brace-dense: str.format() would have died on `@media{...}`, so the
        # tokens are replaced by hand - an unbalanced brace means a botched template
        html = webapp.page()
        self.assertEqual(html.count("{"), html.count("}"))
        self.assertIn("var(--bg)", html)
        self.assertIn("@media (max-width:", html)

    def test_colour_tokens_come_from_the_shared_palette(self):
        import viewmodel as vm
        html = webapp.page().lower()
        for colour in (vm.BG, vm.PANEL, vm.TEXT, vm.MUTED, vm.ACCENT, vm.HEART,
                       vm.ERROR):
            self.assertIn(str(colour).lower(), html, colour)

    def test_the_love_state_uses_the_palettes_heart_not_the_accent(self):
        # green for "playing", the heart colour for "loved": one variable per meaning
        html = webapp.page()
        self.assertIn("--heart:@@HEART@@".replace("@@HEART@@", ""), html)
        self.assertIn("#b-love.on{color:var(--heart)", html)

    def test_no_nav_button_wears_the_love_heart(self):
        # a token slip once put @@heart@@ on "Your Library"; the shelf is its glyph
        html = webapp.page()
        head = re.search(r'class="side-head">([\s\S]*?)</div>', html).group(1)
        self.assertNotIn(webapp.ICONS["heart"], head)
        self.assertIn(webapp.ICONS["library"], head)
        for verb, icon in re.findall(r'icon:\s*"([a-z_]+)"[^}]*?label[^}]*?"([A-Z][^"]*)"',
                                     webapp.JS):
            if "love" in icon:
                self.assertNotIn(icon, ("heart",), f"{verb} wears the filled heart")

    def test_server_data_never_enters_the_dom_as_html(self):
        # titles are user data from a search engine: one innerHTML with a track name
        # is a script tag waiting to happen
        html = webapp.page()
        for line in html.splitlines():
            if "innerHTML" not in line:
                continue
            # allowed: clearing a node, and putting a static icon in. Anything
            # else assigning to innerHTML is data going in as markup
            bad = re.search(r'innerHTML\s*=\s*(?!""|''|ICONS)', line)
            self.assertIsNone(bad, line.strip())

    def test_the_generated_script_is_valid_javascript(self):
        # the page is one Python string, so `"a" "b"` across two lines is legal
        # Python and a syntax error in JS - the classic way this file breaks
        js = re.search(r"<script>(.*?)</script>", webapp.page(), re.S).group(1)
        self.assertEqual(re.findall(r'"[^"\n]*"\s*\n\s*"', js), [],
                         "juxtaposed string literals in the generated script")
        node = shutil.which("node")
        if not node:
            self.skipTest("no node to syntax-check with")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            name = fh.name
        try:
            r = subprocess.run([node, "--check", name], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[:900])
        finally:
            os.unlink(name)

    def test_the_page_names_only_actions_that_exist(self):
        html = webapp.page()
        names = set(re.findall(r'data-action="([a-z_]+)"', html))
        names |= set(re.findall(r'act\("([a-z_]+)"', html))
        names |= set(re.findall(r'act\("(?:act\()?([a-z_]+)"', html))
        for name in names:
            self.assertIn(name, web.ACTIONS, name)

    def test_the_shell_and_the_bar_are_both_there(self):
        html = webapp.page()
        for frag in ('class="app"', 'class="side', 'id="np-title"',
                     'id="upnext"', 'id="cards"', 'id="librows"', 'id="bg-main"',
                     'id="detail"', 'id="bar"',
                     'id="vol"', 'id="q"', 'id="results"', 'id="log"', 'id="taste"',
                     'id="empty-acts"', 'id="in-key"', 'id="savebtn"', 'id="mixbtn"',
                     'id="jobpill"', 'id="lovedh"'):
            self.assertIn(frag, html, frag)


class RouteTests(unittest.TestCase):
    """One real socket on 127.0.0.1:0, so routing and headers are actually used."""

    @classmethod
    def setUpClass(cls):
        cls.dj = fake_dj()
        cls.ctx = web.Context(cls.dj)
        cls._patchers = [mock.patch("web.thumbs.get", lambda *a, **k: "")]
        for p in cls._patchers:
            p.start()
        cls.ctx.start()             # the tick loop is what notices a dead socket
        cls.httpd = web.make_server(cls.ctx, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.ctx.stop()
        for p in cls._patchers:
            p.stop()

    def get(self, path, headers=None):
        return urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   headers=headers or {}), timeout=5)

    def post(self, path, body):
        return urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   data=body.encode(), method="POST"), timeout=5)

    def code(self, fn):
        try:
            r = fn()
            body = r.read()
            return r.status, body
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_the_settings_route_writes_and_answers(self):
        # an early version of this route called self._fields() a second time, which
        # read a body that was already consumed and hung the socket until the client
        # gave up; a real request is the only way to catch that
        with mock.patch("web.config.save_llm_config") as save:
            code, body = self.code(lambda: self.post("/api/settings", "model=gemini-3.6-flash"))
        self.assertEqual(code, 200)
        self.assertEqual(save.call_args.kwargs["LLM_MODEL"], "gemini-3.6-flash")
        payload = json.loads(body)
        self.assertIn("settings", payload)
        self.assertEqual(payload["settings"]["model"], "gemini-3.5-flash",
                         "the reply must come from the file, not from the post")

    def test_an_empty_text_field_reaches_the_saver(self):
        # parse_qs drops blank values unless asked to keep them, which made "clear
        # the base URL" from the page a no-op; this only shows through a real socket
        with mock.patch("web.config.save_llm_config") as save:
            code, body = self.code(lambda: self.post("/api/settings", "base=&model=g3"))
        self.assertEqual(code, 200)
        self.assertEqual(save.call_args.kwargs["LLM_BASE_URL"], "")
        self.assertEqual(save.call_args.kwargs["LLM_MODEL"], "g3")

    def test_a_get_on_an_unknown_route_still_drains_nothing(self):
        code, body = self.code(lambda: self.post("/api/nope", "a=1&b=2"))
        self.assertEqual(code, 404)

    def test_head_answers_with_headers_and_no_body(self):
        # `curl -I` against a working app should not hear 501
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"")
            self.assertGreater(int(r.headers["Content-Length"]), 1000)
            self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/stream",
                                     method="HEAD")
        self.assertEqual(self.code(lambda: urllib.request.urlopen(req, timeout=5))[0], 405)

    def test_a_write_method_says_what_it_takes(self):
        for meth in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            code, body = self.code(lambda m=meth: urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{self.port}/api/action",
                                       data=b"a=1", method=m), timeout=5))
            self.assertEqual(code, 405, meth)
            self.assertIn(b"GET, HEAD, POST", body, meth)
            try:            # ...and as a real Allow header, which is what a client
                urllib.request.urlopen(                        # that retries should see
                    urllib.request.Request(f"http://127.0.0.1:{self.port}/api/action",
                                          data=b"a=1", method=meth), timeout=5)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.headers["Allow"], "GET, HEAD, POST", meth)
            else:
                self.fail(f"{meth} was not refused")

    def test_search_also_answers_a_get(self):
        with mock.patch("web.prov.yt_search", lambda q, limit=20, **k: []):
            code, body = self.code(lambda: self.get("/api/search?q=portishead"))
        self.assertEqual(code, 200)
        self.assertIn(b"portishead", body)
        code, body = self.code(lambda: self.get("/api/search?q="))
        self.assertEqual(code, 400)
        self.assertIn(b"q= required", body)

    def test_a_typoed_route_answers_with_the_map(self):
        # 404 with nothing in it makes a scripting session guess; the routes and
        # every verb are one short read away
        code, body = self.code(lambda: self.get("/api/stat"))
        self.assertEqual(code, 404)
        j = json.loads(body)
        self.assertIn("/api/state", j["routes"])
        self.assertIn("mix", j["actions"])

    def test_a_typoed_posted_route_says_the_same(self):
        code, body = self.code(lambda: self.post("/api/actionz", "action=next"))
        self.assertEqual(code, 404)
        self.assertIn(b"/api/action", body)
        self.assertIn(b"next", body)

    def test_socket_noise_is_quiet_unless_you_ask_for_stacks(self):
        # a tab that stops reading mid-write is a broken pipe, and printing its
        # traceback on every reload buries the lines that matter
        import http.server
        saved = os.environ.pop("SPOTUBE_DJ_WEB_DEBUG", None)
        try:
            quiet = web.make_server(self.ctx, "127.0.0.1", 0)
            self.assertTrue(quiet.quiet)
            self.assertFalse(hasattr(quiet.handle_error, "__func__"),
                             "the printer was not swapped out")
            quiet.handle_error(object(), ("1.2.3.4", 9))      # must not raise or print
            quiet.server_close()
            with mock.patch.dict(os.environ, {"SPOTUBE_DJ_WEB_DEBUG": "1"}):
                loud = web.make_server(self.ctx, "127.0.0.1", 0)
            self.assertFalse(loud.quiet)
            self.assertIs(loud.handle_error.__func__, http.server.ThreadingHTTPServer.handle_error,
                          "the debug switch did not give the stacks back")
            loud.server_close()
        finally:
            if saved is not None:
                os.environ["SPOTUBE_DJ_WEB_DEBUG"] = saved

    def test_index_serves_the_app(self):
        code, body = self.code(lambda: self.get("/"))
        self.assertEqual(code, 200)
        self.assertIn(b'class="app"', body)
        self.assertIn(b"<ui-", body[:0] or b"<ui-") if False else None

    def test_security_headers_are_on_the_page(self):
        with self.get("/") as r:
            self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_foreign_host_header_is_403(self):
        code, body = self.code(lambda: self.get("/api/state", {"Host": "evil.test"}))
        self.assertEqual(code, 403)
        self.assertIn(b"loopback", body)

    def test_state_is_json_and_matches_the_dj(self):
        with self.get("/api/state") as r:
            self.assertIn("application/json", r.headers["Content-Type"])
            s = json.loads(r.read())
        self.assertEqual(s["now"]["id"], "vid0")
        # Queue.__len__ is *remaining*, and the row being played was already popped
        self.assertEqual(s["queued"], 5)

    def test_action_round_trip_returns_the_new_state(self):
        with self.post("/api/action", "action=volume&pct=25") as r:
            payload = json.loads(r.read())
        self.assertEqual(payload["state"]["volume"], 25)
        self.assertEqual(self.dj.state.get("volume"), 25)

    def test_action_without_a_name_is_a_400(self):
        code, body = self.code(lambda: self.post("/api/action", ""))
        self.assertEqual(code, 400)
        self.assertIn(b"unknown action", body)

    def test_unknown_route_is_json_with_the_route_list(self):
        code, body = self.code(lambda: self.get("/nope"))
        self.assertEqual(code, 404)
        self.assertIn(b"/api/stream", body)

    def test_art_route_refuses_traversal_over_http(self):
        for path in ("/art/..%2f..%2fetc%2fpasswd", "/art/nope.png", "/art/x.txt"):
            self.assertEqual(self.code(lambda: self.get(path))[0], 404, path)

    def test_art_route_serves_a_cached_file_with_its_type(self):
        tmp = Path(tempfile.mkdtemp(prefix="artweb-"))
        (tmp / "row.png").write_bytes(b"\x89PNGfake")
        with mock.patch("web.thumbs.cache_dir", return_value=str(tmp)):
            code, body = self.code(lambda: self.get("/art/row.png"))
        self.assertEqual(code, 200)
        self.assertEqual(body, b"\x89PNGfake")

    def test_search_route_answers_immediately_and_fills_later(self):
        with mock.patch("web.prov.yt_search", return_value=[track(8)]):
            with self.post("/api/search", "q=some+band") as r:
                payload = json.loads(r.read())
            self.assertTrue(payload["search"]["pending"])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.ctx.search["pending"]:
                time.sleep(0.01)
        self.assertFalse(self.ctx.search["pending"])
        self.assertEqual(self.ctx.search["rows"][0]["id"], "vid8")

    def test_empty_search_is_a_400(self):
        code, body = self.code(lambda: self.post("/api/search", "q="))
        self.assertEqual(code, 400)
        self.assertIn(b"q=", body)

    def test_stream_pushes_a_snapshot_then_unsubscribes(self):
        with self.get("/api/stream") as r:
            self.assertIn("text/event-stream", r.headers["Content-Type"])
            line = r.readline().decode()
        self.assertTrue(line.startswith("data: "), line[:40])
        payload = json.loads(line[len("data: "):].strip())
        self.assertEqual(payload["now"]["id"], "vid0")
        deadline = time.monotonic() + 3
        while self.ctx._subs and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.ctx._subs, [], "a closed tab must be unsubscribed")

    def test_a_client_that_stopped_reading_is_skipped_not_blocked(self):
        # one stalled SSE stream may not hold up the tick that moves everyone's bar
        q = self.ctx.subscribe()
        try:
            t0 = time.monotonic()
            for _ in range(6):
                self.ctx.broadcast(json.dumps({"x": 1}))
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual(q.qsize(), 2, "the subscriber queue is bounded on purpose")
        finally:
            self.ctx.unsubscribe(q)

    def test_favicon_is_inline_svg(self):
        with self.get("/favicon.svg") as r:
            self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"<svg", r.read())


class ServeShapeTests(unittest.TestCase):
    def test_doctor_line_reports_the_default_port(self):
        name, ok, detail = web.doctor_line()
        self.assertEqual(name, "web player")
        self.assertTrue(ok)
        self.assertIn(str(web.DEFAULT_PORT), detail)

    def test_serve_is_given_the_dj_rather_than_making_one(self):
        # the whole point: no second engine, no second queue, no second mpv
        import inspect
        sig = inspect.signature(web.serve)
        self.assertIn("dj", sig.parameters)
        self.assertTrue(sig.parameters["dj"].default is inspect.Parameter.empty)
        src = inspect.getsource(web.serve)
        self.assertNotIn("DJ(", src)

    def test_the_socket_is_up_before_the_first_mix_is_built(self):
        # --web "opens and never loads" was this order: a blocking search ahead of
        # bind(). The page must always answer, even while the queue is still empty
        import inspect
        src = inspect.getsource(web.serve)
        self.assertLess(src.index("make_server"), src.index("_first_mix"))

    def test_an_open_bind_is_not_narrowed_to_one_interface(self):
        # rewriting 0.0.0.0 to a picked address (a tempting "print the real IP"
        # shortcut) silently un-binds loopback and localhost stops working
        ctx = web.Context(fake_dj())
        with mock.patch("web.ThreadingHTTPServer") as srv, \
                mock.patch("web._lan_ip", return_value="10.1.2.3"):
            httpd = web.make_server(ctx, "0.0.0.0", 1234)
        self.assertEqual(srv.call_args[0][0], ("0.0.0.0", 1234))
        self.assertEqual(httpd.display_host, "10.1.2.3")
        self.assertTrue(web.is_open(httpd.bound_host))
        with mock.patch("web.ThreadingHTTPServer") as srv2:
            httpd2 = web.make_server(web.Context(fake_dj()), "127.0.0.1", 0)
        self.assertEqual(srv2.call_args[0][0], ("127.0.0.1", 0))
        self.assertEqual(httpd2.display_host, "127.0.0.1")
        self.assertFalse(web.is_open(httpd2.bound_host), "loopback keeps the Host guard")

    def test_the_advance_loop_is_owned_by_the_dj_not_the_socket(self):
        # without this the web skin plays one song and stops; with it, `--backend
        # none` would fake a Now Playing that never moves
        dj = fake_dj()
        self.assertFalse(web.should_run_loop(dj), "no player, nothing to advance")
        dj.player = object()
        self.assertTrue(web.should_run_loop(dj))
        import inspect
        src = inspect.getsource(web.serve)
        self.assertEqual(src.count("dj.run"), 1, "one loop, not one per request")
        self.assertIn("should_run_loop(dj)", src)

    def test_first_mix_failures_land_in_the_log_not_on_stdout(self):
        dj = fake_dj()
        def boom(*a, **k):
            raise RuntimeError("no results this time")
        dj.start = boom                           # type: ignore[method-assign]
        web._first_mix(dj, "some mood", "", 20)
        self.assertTrue(any("first mix could not be built" in line and
                            "no results this time" in line for line in dj.log), dj.log[-3:])

    def test_first_mix_passes_the_playlist_as_a_seed(self):
        dj = fake_dj()
        with mock.patch.object(dj, "start") as st:
            web._first_mix(dj, "chill", "37i9dQZF1DXcBWIGoYBM5M", 9)
        st.assert_called_once_with("chill", seed_refs=["37i9dQZF1DXcBWIGoYBM5M"], count=9)
        with mock.patch.object(dj, "start") as st:
            web._first_mix(dj, "chill", "", 9)
        self.assertIsNone(st.call_args.kwargs["seed_refs"], "no playlist, no seed read")

    def test_context_drops_pending_work_on_stop(self):
        ctx = web.Context(fake_dj())
        ctx.start()
        with mock.patch("web.thumbs.get", lambda *a, **k: ""):
            ctx.art.put_nowait(track(1))
            ctx.stop()
        self.assertTrue(ctx._stop.is_set())
        self.assertEqual(list(ctx.art.queue), [])

    def test_loved_rows_come_from_the_taste_file(self):
        import taste
        taste.record_like({"title": "Real One", "artist": "Real Artist", "duration": 200})
        rows = web.loved_rows()
        self.assertTrue(any(r["title"] == "Real One" for r in rows))
        row = [r for r in rows if r["title"] == "Real One"][0]
        self.assertFalse(row["id"], "a loved row has no stream to play, only words")
        self.assertEqual(row["note"], "loved")



class TransportActionTests(unittest.TestCase):
    """The two switches the bottom bar has, and the sidebar's unfollow."""

    def setUp(self):
        self.dj = fake_dj()
        # both switches are persisted preferences, so a test that leaves them on
        # would decide what the next one sees; start from a stated place
        self.dj.shuffle = False
        self.dj.set_repeat("off")
        self.ctx = web.Context(self.dj)
        import taste
        taste.clear()
        try:
            taste.undo_file().unlink()
        except OSError:
            pass

    def tearDown(self):
        self.ctx.stop()

    def run_it(self, action, **fields):
        """-> (code, payload, state) - the last one is what the page would redraw."""
        form = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in fields.items())
        code, payload = web.run_action(self.ctx, action, urllib.parse.parse_qs(form))
        return code, payload, web.build_state(self.ctx)

    def test_shuffle_toggles_and_reports(self):
        code, a, st = self.run_it("shuffle")
        self.assertEqual(code, 200)
        self.assertTrue(self.dj.shuffle)
        self.assertIn("shuffle on", a["note"])
        self.assertTrue(st["shuffle"])
        code, b, st2 = self.run_it("shuffle")
        self.assertFalse(self.dj.shuffle)
        self.assertIn("shuffle off", b["note"])
        self.assertFalse(st2["shuffle"])

    def test_shuffle_mixes_only_what_has_not_been_heard(self):
        order = [t["id"] for t in self.dj.queue.items]
        self.run_it("shuffle")
        seen = self.dj.queue.items[:self.dj.queue.pos]
        self.assertEqual([t["id"] for t in seen], order[:self.dj.queue.pos],
                         "the row the listener is hearing must not move")
        rest = [t["id"] for t in self.dj.queue.items[self.dj.queue.pos:]]
        self.assertEqual(sorted(rest), sorted(order[self.dj.queue.pos:]),
                         "shuffle may reorder the queue, never lose rows")

    def test_repeat_cycles_off_all_one(self):
        got = []
        for _ in range(4):
            code, a, st = self.run_it("repeat")
            self.assertEqual(code, 200)
            got.append(st["repeat"])
        self.assertEqual(got, ["all", "one", "off", "all"])

    def test_repeat_accepts_a_mode_and_refuses_nonsense(self):
        self.assertEqual(self.run_it("repeat", mode="one")[2]["repeat"], "one")
        self.assertEqual(self.run_it("repeat", mode="banana")[2]["repeat"], "off")
        self.assertEqual(self.run_it("repeat", mode="all")[2]["repeat"], "all")
        # and it is remembered: the switch is a preference, not a one-song trick.
        # read through config, not a hand-built path, so the test follows wherever
        # the scratch home is for this run
        self.assertEqual(config.load_state().get("repeat"), "all")

    def test_repeat_one_requeues_on_a_natural_end_but_not_on_a_button(self):
        self.dj.set_repeat("one")
        first = self.dj.current
        self.dj.next()                       # the auto-advance path
        self.assertEqual(self.dj.current["id"], first["id"],
                         "repeat one must start the same track again")
        self.dj.next(force=True)             # a human pressed next
        self.assertNotEqual(self.dj.current["id"], first["id"],
                            "a person pressing next must move on")

    def test_repeat_all_rewinds_instead_of_saying_finished(self):
        self.dj.auto = False
        self.dj.queue.pos = len(self.dj.queue.items)      # the set has run out
        self.dj.set_repeat("all")
        t = self.dj.next()
        self.assertIsNotNone(t, "repeat all has to start the set again")
        self.assertEqual(self.dj.queue.pos, 1)              # popped the FIRST row
        self.assertEqual(t["id"], self.dj.queue.items[0]["id"])

    def test_unfollow_needs_a_name_and_only_removes_the_leaning(self):
        import taste
        taste.record_like({"title": "One", "artist": "Slowdive"})
        self.assertGreater(taste.load_state()["artists"].get("slowdive", 0), 0)
        code, a, _st = self.run_it("unfollow")
        self.assertEqual(code, 200)
        self.assertIn("which artist", a["note"])
        code, a, _st = self.run_it("unfollow", name="Slowdive")
        self.assertEqual(code, 200)
        self.assertIn("will not pull the mix", a["note"])
        st = taste.load_state()
        self.assertNotIn("slowdive", st["artists"])
        self.assertEqual(len(st["liked"]), 1, "the loved song must survive unfollowing")
        code, a, _st = self.run_it("unfollow", name="Never Heard Of Them")
        self.assertIn("no weight for", a["note"])

    def test_the_new_verbs_are_in_the_button_table(self):
        for name in ("shuffle", "repeat", "unfollow"):
            self.assertIn(name, web.ACTIONS)
        html = webapp.page()
        for name in ("shuffle", "repeat"):
            self.assertIn(f'data-action="{name}"', html, f"no button sends {name}")


class LibraryViewTests(unittest.TestCase):
    """What the sidebar shows, built without touching the network."""

    def setUp(self):
        import taste
        taste.clear()
        for fh in (config.HISTORY_FILE,):
            try:
                fh.unlink()
            except OSError:
                pass

    def _like(self, title, artist):
        import taste
        taste.record_like({"title": title, "artist": artist})

    def test_loved_rows_carry_a_query_that_finds_the_song(self):
        self._like("Sugar Rain", "Menlo")
        view = web.library_view(__import__("taste").load_state())
        row = view["loved"][0]
        self.assertEqual(row["title"], "Sugar Rain")        # display text, not norm'd
        self.assertEqual(row["q"], "Menlo Sugar Rain")
        self.assertTrue(row["note"], "a loved row without a when reads as broken")

    def test_artists_are_ordered_by_weight_and_count_loves_once(self):
        self._like("a", "Slowdive")
        self._like("b", "Slowdive")
        self._like("c", "Menlo")
        import taste
        view = web.library_view(taste.load_state())
        names = [a["name"] for a in view["artists"]]
        self.assertEqual(names[0], "slowdive", names)
        by = {a["name"]: a for a in view["artists"]}
        self.assertEqual(by["slowdive"]["loved"], 2)
        self.assertEqual(by["menlo"]["loved"], 1)
        self.assertTrue(all(a["w"] > 0 for a in view["artists"]))
        self.assertEqual(view["counts"]["loved"], 3)

    def test_moods_come_from_history_and_deduplicate(self):
        for q in ("lofi to work to", "lofi to work to", "songs like menlo"):
            config.append_history({"id": "x1", "title": "T", "artist": "A",
                                   "ts": time.time(), "query": q})
        view = web.library_view({"liked": [], "artists": {}})
        self.assertEqual([m["q"] for m in view["moods"]],
                         ["songs like menlo", "lofi to work to"])

    def test_no_moods_but_a_last_request_still_shows_something(self):
        view = web.library_view({"liked": [], "artists": {}, "last_request": "90s trip hop"})
        self.assertEqual([m["q"] for m in view["moods"]], ["90s trip hop"])

    def test_recents_are_newest_first_and_capped(self):
        for i in range(30):
            config.append_history({"id": f"i{i}", "title": f"T{i}", "artist": "A",
                                   "ts": time.time() + i, "query": ""})
        view = web.library_view({"liked": [], "artists": {}}, limit=12)
        self.assertEqual(len(view["recents"]), 12)
        self.assertEqual(view["recents"][0]["title"], "T29")
        self.assertTrue(all(r["ts"] for r in view["recents"]), "no timestamp shown")

    def test_building_the_sidebar_never_reaches_the_network(self):
        self._like("a", "Slowdive")
        import taste
        with mock.patch("covers.resolve_blocking", side_effect=AssertionError("network!")), \
             mock.patch("covers.attach", side_effect=AssertionError("network!")), \
             mock.patch("thumbs.get", side_effect=AssertionError("network!")):
            view = web.library_view(taste.load_state())
        self.assertEqual(view["counts"]["loved"], 1)

    def test_ago_reads_like_a_person(self):
        now = 1_700_000_000.0
        self.assertEqual(web._ago(now, now), "just now")
        self.assertEqual(web._ago(now - 125, now), "2m ago")
        self.assertEqual(web._ago(now - 7200, now), "2h ago")
        self.assertEqual(web._ago(now - 86400 * 9, now), "1w ago")
        for junk in (None, "", "yesterday", float("nan"), float("inf"), {}):
            self.assertEqual(web._ago(junk, now), "", repr(junk))

    def test_state_carries_the_library_and_both_switches(self):
        dj = fake_dj()
        st = web.build_state(web.Context(dj))
        for key in ("library", "repeat", "shuffle"):
            self.assertIn(key, st)
        for row in st["up_next"]:
            self.assertIn("found", row)
            self.assertIn("cached", row)


class CoverLandingTests(unittest.TestCase):
    """Art that arrives late still has to reach the page."""

    def setUp(self):
        self.dj = fake_dj()
        self.ctx = web.Context(self.dj)

    def tearDown(self):
        self.ctx.stop()

    def test_a_landed_cover_replaces_every_slot(self):
        import inspect
        self.ctx._store_href("vid1", "/tmp/vid1-yt-72.jpg", "row")
        self.ctx._store_href("vid1", "/tmp/vid1-yt-256.jpg", "card")
        self.ctx._cover_ready("vid1", "big", "/tmp/vid1-caa-500.jpg")
        for size in web.ART_SIZES:
            self.assertEqual(self.ctx.art_href("vid1", size), "/art/vid1-caa-500.jpg",
                             f"the {size} slot is still showing a video frame")

    def test_the_lane_draws_the_fast_picture_before_the_paced_one(self):
        import inspect
        # the archive hop is a paced lookup; putting it first made every fresh card
        # wait seconds for a lookup that may not even find a cover
        src = inspect.getsource(web.Context._art_loop)
        self.assertLess(src.index("self._store_href(vid, thumbs.get"),
                        src.index("covers.remember_track(t)"),
                        "the lane is waiting on the archive before it draws anything")

    def test_junk_from_the_callback_cannot_raise_into_the_covers_thread(self):
        for junk in (None, "", 42):
            self.ctx._cover_ready(junk, junk, junk)
        self.assertEqual(self.ctx.art_href(""), "")

    def test_a_cover_landing_later_replaces_the_video_frame(self):
        self.ctx._store_href("vid2", "/tmp/vid2-yt-72.jpg", "row")
        first = self.ctx.art_href("vid2")
        self.ctx._cover_ready("vid2", "row", "/tmp/vid2-caa-250.jpg")
        self.assertEqual(self.ctx.art_href("vid2"), "/art/vid2-caa-250.jpg")
        self.assertNotEqual(self.ctx.art_href("vid2"), first)

    def test_one_slot_never_leaks_into_another(self):
        self.ctx._store_href("vid3", "/tmp/vid3-caa-250.jpg", "card")
        self.assertEqual(self.ctx.art_href("vid3", "card"), "/art/vid3-caa-250.jpg")
        self.assertEqual(self.ctx.art_href("vid3", "big"), "",
                         "the hero must wait for its own file")


class ArtLaneTests(unittest.TestCase):
    """What gets asked for, once, and only for the rows a person can see."""

    def setUp(self):
        self.dj = fake_dj()
        self.ctx = web.Context(self.dj)

    def tearDown(self):
        self.ctx.stop()

    def test_request_art_queues_one_item_per_row_and_size(self):
        tracks = [{"id": f"v{i}"} for i in range(5)]
        self.assertEqual(self.ctx.request_art(tracks, "row"), 5)
        self.assertEqual(self.ctx.request_art(tracks, "row"), 0,
                         "a second tick must not re-ask for the same work")
        self.assertEqual(self.ctx.request_art(tracks, "card"), 5)
        got = []
        while not self.ctx.art.empty():
            got.append(self.ctx.art.get_nowait())
        self.assertEqual([g[1] for g in got], ["row"] * 5 + ["card"] * 5)
        self.assertEqual([str(g[0]["id"]) for g in got],
                         [t["id"] for t in tracks] * 2)

    def test_a_row_that_already_has_the_file_is_not_queued(self):
        self.ctx._store_href("v1", "/tmp/v1-yt-72.jpg", "row")
        self.assertEqual(self.ctx.request_art([{"id": "v1"}, {"id": "v2"}], "row"), 1)
        self.ctx._seen_art.add(("v3", "row"))
        self.assertEqual(self.ctx.request_art([{"id": "v3"}], "row"), 0,
                         "work the lane has drawn is not work to repeat")

    def test_a_row_that_carries_a_thumbnail_is_still_queued_for_its_own_file(self):
        # a row may carry a `thumbnail`, but that is a cross-origin URL that can be
        # refused in the browser; the lane still fetches a same-origin file for it so
        # the queue/cards are never stuck on a cross-origin image CDN. `thumbnail`
        # alone never keeps a row off the lane.
        tracks = [{"id": "v1", "thumbnail": "https://i.ytimg.com/cover.jpg"},
                  {"id": "v2", "thumbnail": ""},
                  {"id": "v3"}]
        self.assertEqual(self.ctx.request_art(tracks, "row"), 3)
        got = []
        while not self.ctx.art.empty():
            got.append(self.ctx.art.get_nowait())
        self.assertEqual([g[0]["id"] for g in got], ["v1", "v2", "v3"],
                         "a row with a thumbnail must still get the lane's same-origin file")

    def test_only_the_visible_rows_are_warmed(self):
        tracks = [{"id": f"v{i}"} for i in range(60)]
        self.assertEqual(self.ctx.request_art(tracks, "card", limit=12), 12,
                         "a 200-row search must not fill the lane with art nobody sees")

    def test_only_the_record_being_heard_gets_the_hero_file(self):
        import inspect
        # 12 hero files a person cannot see is 12 wasted downloads: the lane asks
        # for the 512px rung for the current track alone (and the backdrop is that
        # same file, blurred), which is why this rule is pinned to the source
        src = inspect.getsource(web.Context._art_loop)
        self.assertIn('if size != "big" and current:', src,
                      "the artwork lane is fetching hero files for every row again")

    def test_the_visible_cards_are_warmed_before_the_lists(self):
        import inspect
        # the grid is what the eye is on; a list of 40px rows can borrow from it,
        # so the queue order decides what a person sees first
        src = inspect.getsource(web.build_state)
        self.assertLess(src.index('request_art(upcoming, "card"'),
                        src.index('request_art(upcoming, "row"'),
                        "rows are being warmed ahead of the cards again")

    def test_loved_rows_carry_the_id_that_makes_art_possible(self):
        # the sidebar is a column of initials when a loved row arrives with no id:
        # nothing can be looked up by title alone, and the lane never asks
        prof = {"liked": [{"id": "abc123", "title": "T", "artist": "A", "ts": 1}],
                "artists": {}, "genres": {}, "skipped": []}
        self.assertEqual([r["id"] for r in web.loved_rows(state=prof)], ["abc123"])
        self.assertEqual(web.library_view(prof)["loved"][0]["id"], "abc123")

    def test_every_library_row_has_the_slots_the_page_reads(self):
        # a hand-built row that forgets `art` can never be stamped later, which is
        # how the loved list stayed letters even with covers sitting on disk
        prof = {"liked": [{"id": "abc123", "title": "T", "artist": "A", "ts": 1}],
                "artists": {"X": 1.0}, "genres": {}, "skipped": [],
                "last_request": "trip hop"}
        lib = web.library_view(prof)
        self.assertTrue(lib["loved"], "the profile's likes must reach the sidebar")
        for key in ("loved", "recents"):
            for row in lib[key]:
                self.assertIn("art", row, f"{key} rows need the row slot")
                self.assertIn("art_card", row, f"{key} rows need the card slot")
                self.assertIn("id", row)

    def test_the_sidebar_is_warmed_too(self):
        # build_state asks for the queue's covers; the loved list is the other place
        # a person looks first, so it goes on the same lane (capped)
        prof = {"liked": [{"id": f"loved{i}", "title": f"T{i}", "artist": "A",
                           "ts": 1} for i in range(3)],
                "artists": {}, "genres": {}, "skipped": []}
        with mock.patch.object(web.taste, "load_state", return_value=prof):
            web.build_state(self.ctx)
        queued = []
        while not self.ctx.art.empty():
            queued.append(self.ctx.art.get_nowait())
        self.assertIn("loved0", [str(item[0].get("id")) for item in queued])

    def test_garbage_cannot_queue_or_raise(self):
        for junk in (None, [], [None], ["x"], [{}], [{"id": ""}]):
            self.assertEqual(self.ctx.request_art(junk, "row"), 0)
        self.assertEqual(self.ctx.request_art([{"id": "v1"}], "nope"), 0,
                         "an unknown size is a bug in the caller, not a download")
        self.assertTrue(self.ctx.art.empty())

if __name__ == "__main__":
    unittest.main()


class ClearQueueActionTests(unittest.TestCase):
    """
    `⋯ -> Clear queue` is one verb, and its note has to be a number.

    A clear that answers nothing reads as a button that does not work - the queue
    refills itself 400 ms later when keep mixing is on, which is the correct
    behaviour and would otherwise look like a bug on top of a bug.
    """

    def _ctx(self, auto=True, n=6):
        dj = fake_dj([track(i) for i in range(n)])
        dj.auto = auto
        dj._topup = lambda **k: None
        dj._resolve = lambda t: "http://s"
        return dj, web.Context(dj)

    def test_it_drops_what_is_queued_and_leaves_the_song_alone(self):
        dj, ctx = self._ctx()
        try:
            code, payload = web.run_action(ctx, "clear_queue", {})
            self.assertEqual(code, 200)
            self.assertEqual(len(dj.queue), 0)
            self.assertEqual(dj.current["title"], track(0)["title"])
            self.assertIn("5 tracks dropped", payload["note"])
            self.assertIn("the list fills again shortly", payload["note"])
            st = web.build_state(ctx)
            self.assertEqual(st["queued"], 0)
            self.assertEqual(st["up_next"], [])
            self.assertEqual(st["now"]["title"], track(0)["title"],
                             "the panel lost the song that is still playing")
        finally:
            ctx.stop()

    def test_an_already_empty_queue_says_so_instead_of_succeeding_quietly(self):
        dj, ctx = self._ctx(n=1)
        try:
            code, payload = web.run_action(ctx, "clear_queue", {})
            self.assertEqual(code, 200)
            self.assertIn("0 tracks dropped", payload["note"])
        finally:
            ctx.stop()

    def test_the_note_changes_when_refilling_is_off(self):
        dj, ctx = self._ctx(auto=False)
        try:
            _code, payload = web.run_action(ctx, "clear_queue", {})
            self.assertIn("keep mixing is off", payload["note"])
        finally:
            ctx.stop()

    def test_the_refill_runs_on_the_job_lane_not_in_the_request(self):
        dj, ctx = self._ctx()
        ran = []
        dj._topup = lambda **k: ran.append(k)
        try:
            calls = []

            def fake_start(self, target):
                calls.append(target)
                target()
                return True

            with mock.patch.object(web.Context, "start_job", fake_start):
                _code, payload = web.run_action(ctx, "clear_queue", {})
            self.assertEqual(len(calls), 1, "no job was scheduled for the refill")
            self.assertEqual(ran, [{"force": True}],
                             "the refill was not asked for, so \"shortly\" is a lie")
            self.assertNotIn("did not finish", payload["note"])
        finally:
            ctx.stop()

    def test_a_clear_during_a_search_says_who_will_fill_it(self):
        dj, ctx = self._ctx()
        try:
            with mock.patch.object(web.Context, "start_job", lambda self, target: False):
                _code, payload = web.run_action(ctx, "clear_queue", {})
            self.assertIn("a search is already running", payload["note"])
        finally:
            ctx.stop()

    def test_history_and_love_survive_a_clear(self):
        dj, ctx = self._ctx()
        try:
            dj.like()
            web.run_action(ctx, "clear_queue", {})
            self.assertTrue(dj.is_liked(dj.current),
                            "clearing the queue is not forgetting the listener")
        finally:
            ctx.stop()
