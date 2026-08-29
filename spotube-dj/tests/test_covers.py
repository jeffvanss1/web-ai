"""
Cover art: the MusicBrainz lookup + Cover Art Archive fetch, and the plumbing
that hands the finished image to the GUI.

No network here. The API is faked at its single seam (`covers._get_json`, and
`urllib.request.urlopen` for the error paths) and the image fetch at
`thumbs.download_url`, so what these tests hold is the rules the provider's own
documentation states:

  * /release/{mbid}/front-(250|500|1200) and /release-group/{mbid}/front[-size]
  * "never make more than ONE call per second" against musicbrainz.org, with a
    meaningful User-Agent (an empty one measured 403, not a warning)
  * a 503 comes from a search bucket this app merely borrows, so it must be
    waited out - never remembered as "this album has no cover art"
"""
from __future__ import annotations

import inspect
import io
import json
import os
import tempfile
import time
from pathlib import Path
import unittest
import urllib.error
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap + scratch SPOTUBE_DJ_HOME)

import config
import covers
import shutil
import thumbs


def fake_json(payload):
    """Stand in for urllib: a 200 whose body is this JSON."""
    def _open(req, *a, **k):
        class R:
            def read(self, *_):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False
        return R()
    return _open


def http_error(code, headers=None):
    def _open(req, *a, **k):
        raise urllib.error.HTTPError("https://musicbrainz.org/ws/2/x", code, "nope",
                                     headers or {}, io.BytesIO(b'{"error":"x"}'))
    return _open


class CoverTests(unittest.TestCase):
    STATE = ("_index", "_index_mtime", "_rate_limited_until", "_retry_after",
             "_last_error", "_notifier", "_last_call", "_seen", "_queue", "_stats",
             "_answered")

    def setUp(self):
        self.saved = {k: getattr(covers, k) for k in self.STATE}
        covers._index, covers._index_mtime = {}, 0.0
        covers._rate_limited_until = covers._retry_after = 0.0
        covers._last_error = ""
        covers._notifier = None
        covers._last_call = {}
        covers._seen, covers._queue = {}, []
        covers._answered = True
        covers._stats = dict.fromkeys(covers._stats, 0)
        self._home = Path(tempfile.mkdtemp(prefix="covers-test-"))
        config.APP_DIR = self._home
        covers._index = {}
        covers._index_mtime = 0.0
        # no ffmpeg patch any more: artwork used to be gated on `which("ffmpeg")`
        # which is how a desktop launch silently lost every cover. Tk decodes
        # PNG/GIF/JPEG itself, so enabled() no longer asks (see
        # tests/test_filters_cache.py for the new contract).
        self.addCleanup(shutil.rmtree, self._home, True)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(covers, k, v)

    def open_get(self, payload):
        """
        `_get_json` replaced by canned answers, counted. Keyed on the URL,
        because a search and a lookup genuinely return different shapes and one
        payload for both silently turns into "no releases", which tests nothing.
        """
        self.calls = []

        def _fake(url, host):
            self.calls.append((url, host))
            if callable(payload):
                return payload(url, host)
            return payload
        return mock.patch.object(covers, "_get_json", _fake)

    @staticmethod
    def recording_then_release(mbid, rec="11111111-2222-3333-4444-555555555555"):
        """A recording search, then the lookup that names its release."""
        def answer(url, host):
            if "inc=releases" in url:
                return {"releases": [{"id": mbid}]}
            if "recording?" in url:
                return {"recordings": [{"id": rec}]}
            return {}
        return answer

    # ------------------------------------------------------------------ urls
    def test_urls_follow_the_documented_shapes(self):
        m = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(covers.caa_url(m, "row"),
                         f"https://coverartarchive.org/release/{m}/front-250")
        self.assertEqual(covers.caa_url(m, "big"),
                         f"https://coverartarchive.org/release/{m}/front-500")
        self.assertEqual(sorted(covers.SIZE_BY_KIND.values()), [250, 500],
                         "only the sizes the Archive lists may be requested")
        self.assertEqual(covers.caa_url(m, "big", group=True),
                         f"https://coverartarchive.org/release-group/{m}/front-500")

    def test_a_query_string_cannot_break_the_search(self):
        out = covers._lucene("Why? (Don't Buy It) [live] - Part 1")
        for bad in "?()[":
            self.assertNotIn(bad, out)
        self.assertIn("Why", out)
        self.assertIn("live", out)

    # -------------------------------------------------------------- choosing
    def test_an_earlier_official_release_wins(self):
        rels = [{"id": "late", "title": "Greatest Hits", "date": "2005-01-01",
                 "status": "Official"},
                {"id": "orig", "title": "Single", "date": "1972-06-01",
                 "status": "Official"},
                {"id": "boot", "title": "Something", "date": "1970-01-01",
                 "status": "Bootleg"},
                {"id": "undated", "title": "Unknown"}]
        self.assertEqual([r["id"] for r in covers._pick_release(rels)],
                         ["orig", "late", "undated", "boot"])

    def test_the_exact_album_beats_the_search_score(self):
        groups = {"release-groups": [
            {"id": "collab", "title": "Random Access Memories: The Collaborators",
             "primary-type": "Album"},
            {"id": "ram", "title": "Random Access Memories", "primary-type": "Album"}]}
        with self.open_get(groups):
            self.assertEqual(covers._release_group_mbid("Daft Punk",
                                                        "Random Access Memories"),
                             "ram")
        url, host = self.calls[0]
        self.assertEqual(host, "musicbrainz.org")
        self.assertIn("artist%3A", url)          # artist:, not artist-name: (that 503s)
        self.assertNotIn("type=album%7C", url)   # no undocumented filter syntax

    # -------------------------------------------------------------- the index
    def test_a_lookup_is_remembered_and_reused_without_a_call(self):
        with self.open_get(self.recording_then_release("r1")):
            self.assertEqual(covers._resolve("America", "Soft Focus", ""),
                             ("r1", "release"))
            after_first = len(self.calls)
            self.assertEqual(covers._resolve("America", "Soft Focus", ""),
                             ("r1", "release"))
        self.assertEqual(len(self.calls), after_first,
                         "the second lookup hit the network again")
        # a title lookup is a search plus a lookup; that is the documented cost
        self.assertEqual(after_first, 2, str(self.calls))

    def test_a_real_miss_is_cached_but_only_briefly(self):
        with self.open_get({"recordings": []}):
            self.assertEqual(covers._resolve("Nobody", "Nothing At All", ""),
                             ("", "release"))
        entry = covers._load_index()[covers.key_for("Nobody", "Nothing At All")]
        self.assertEqual(entry["mbid"], "")
        self.assertFalse(entry.get("throttled"))
        self.assertLess(covers.TTL_MISS, covers.TTL,
                        "a miss must not outlive the release it was about")

    def test_a_throttle_is_not_cached_as_a_missing_cover(self):
        with mock.patch.object(covers.urllib.request, "urlopen",
                               http_error(503, {"Retry-After": "7",
                                                "X-Ratelimit-Remaining": "12"})):
            out = covers._get_json("https://musicbrainz.org/ws/2/x", "musicbrainz.org")
        self.assertIsNone(out)
        self.assertIn("503", covers.last_error())
        self.assertIn("12", covers.last_error(), "the reason should say what happened")
        self.assertAlmostEqual(covers._rate_limited_until - time.time(), 7.0, delta=1.5,
                              msg="the server's Retry-After must be honoured")
        self.assertEqual(covers.stats()["throttled"], 1)

        # now the real _get_json, told to cool off: it must not ask, and must not
        # let "we did not ask" be filed as "there is no such album"
        covers._rate_limited_until = time.time() + 30
        with mock.patch.object(covers.urllib.request, "urlopen",
                               fake_json({"recordings": []})):
            self.assertEqual(covers.resolve("America", "Some Song"), "")
        self.assertNotIn(covers.key_for("America", "Some Song"), covers._load_index(),
                         "a call we never made must not become a verdict")
        self.assertFalse(covers.answered(), "a paced-off call is not an answer")

    def test_a_refused_call_says_so_instead_of_looking_like_an_empty_answer(self):
        covers._rate_limited_until = time.time() + 30
        with mock.patch.object(covers.urllib.request, "urlopen", fake_json({"x": 1})):
            self.assertIsNone(covers._get_json("u", "musicbrainz.org"))
        self.assertTrue(covers.last_error().startswith("not asked"),
                        covers.last_error())

    def test_an_index_entry_from_the_old_shape_still_loads(self):
        covers._index = {"a|x": {"release": "deadbeef", "seen": int(time.time())}}
        self.assertEqual(covers._entry_mbid(covers._index["a|x"]), "deadbeef")
        self.assertEqual(covers._entry_flavour(covers._index["a|x"]), "release")
        self.assertEqual(covers._known_entry({"artist": "a", "title": "x"}),
                         covers._index["a|x"])

    def test_a_throttled_entry_expires_instead_of_blocking_the_row(self):
        key = covers.key_for("America", "Soft Focus")
        covers._remember(key, "", "release", throttled=True)
        self.assertIsNone(covers._known_entry({"artist": "America",
                                              "title": "Soft Focus"}))
        data = json.loads(covers.index_path().read_text())
        data[key]["seen"] = int(time.time() - covers.TTL_THROTTLE - 1)
        covers.index_path().write_text(json.dumps(data))
        covers._index_mtime = 0.0
        with self.open_get(self.recording_then_release("later")):
            self.assertEqual(covers._resolve("America", "Soft Focus", ""),
                             ("later", "release"))
        self.assertFalse(covers._index[key].get("throttled"),
                         "the retry must replace the cool-off marker")

    # ----------------------------------------------------------- politeness
    def test_musicbrainz_is_called_at_most_once_per_second(self):
        covers._last_call = {"musicbrainz.org": time.monotonic()}
        t0 = time.monotonic()
        self.assertTrue(covers._pace("musicbrainz.org"))
        self.assertGreaterEqual(time.monotonic() - t0, covers.MIN_INTERVAL * 0.5,
                               "two calls inside the same second is how you get banned")
        covers._last_call = {"musicbrainz.org": 0.0}
        t0 = time.monotonic()
        self.assertTrue(covers._pace("musicbrainz.org"))
        self.assertLess(time.monotonic() - t0, 0.2, "pacing slept when it need not have")

    def test_the_cooldown_silences_every_host_but_the_pacer_is_per_host(self):
        covers._rate_limited_until = time.time() + 5
        self.assertFalse(covers._pace("musicbrainz.org"))
        covers._rate_limited_until = 0.0
        covers._last_call = {"coverartarchive.org": time.monotonic()}
        t0 = time.monotonic()
        covers._pace("coverartarchive.org")
        self.assertLess(time.monotonic() - t0, covers.MIN_INTERVAL,
                        "the Archive states no rate limit; only the search is paced")

    def test_the_user_agent_is_meaningful(self):
        # measured: an empty UA is a 403, and the docs require a real string
        self.assertGreaterEqual(len(covers.USER_AGENT), 10)
        self.assertIn("spotube-dj", covers.USER_AGENT)

    # ----------------------------------------------------------- the GUI seam
    def test_attach_uses_a_known_answer_and_otherwise_only_queues(self):
        started, queued = [], []
        track = {"id": "vid1", "artist": "America", "title": "Soft Focus"}
        with mock.patch.object(covers, "_start", lambda: started.append(1)), \
                mock.patch.object(covers, "_enqueue", lambda t: queued.append(t["id"])):
            self.assertEqual(covers.attach(track), "")
        self.assertEqual(queued, ["vid1"], "an unknown track must be queued, not awaited")

        covers._index = {covers.key_for("America", "Soft Focus"):
                         {"mbid": "m1", "flavour": "release", "seen": int(time.time())}}
        second = {"id": "vid2", "artist": "America", "title": "Soft Focus"}
        with mock.patch.object(covers, "_start", lambda: started.append(1)), \
                mock.patch.object(covers, "_enqueue", lambda t: queued.append(t["id"])):
            covers.attach(second)
        self.assertEqual(second["cover_url"], covers.caa_url("m1", "big"))
        self.assertEqual(queued, ["vid1"], "a cached answer must not re-queue")
        self.assertTrue(started, "resolved art was never scheduled for download")

    def test_one_album_serves_every_row_but_each_row_keeps_its_own_file(self):
        # two tracks from one album: the notifier must report each track's own id,
        # or every row of that album ends up showing the first row's artwork
        a = {"id": "va", "artist": "America", "title": "One"}
        b = {"id": "vb", "artist": "America", "title": "Two"}
        key = covers.key_for("America", "One")
        covers._seen[key] = [a, b]
        events, files = [], []
        covers.set_notifier(lambda vid, kind, path: events.append((vid, kind, path)))

        def fake_download(url, t, kind):
            files.append((url, t["id"], kind))
            return f"/tmp/{t['id']}-{kind}.png"

        with mock.patch.object(thumbs, "download_url", fake_download), \
                mock.patch.object(covers, "_start", lambda: None):
            done = covers._dress(key, "m", "release")
        self.assertEqual(done, 4)                              # 2 tracks x 2 sizes
        self.assertEqual(sorted({t for _u, t, _k in files}), ["va", "vb"])
        self.assertEqual(sorted({v for v, _k, _p in events}), ["va", "vb"],
                         "art was announced under the wrong track id")
        self.assertEqual(a["cover_flavour"], "release")

    def test_the_player_lane_actually_calls_covers(self):
        import web
        src = inspect.getsource(web.Context._art_loop)
        self.assertIn("covers.attach", src)
        self.assertIn("covers.remember_track", src)
        self.assertIn("covers.row_mode", src)
        self.assertIn("covers.set_notifier", inspect.getsource(web.Context.__init__))
        self.assertIn("covers.stop", inspect.getsource(web.Context.stop))

    def test_no_lookup_is_ever_started_from_the_request_thread(self):
        # a search can take seconds of polite waiting; a GET /api/state must never
        # wait on it, or the whole page stalls behind MusicBrainz's one-call rule
        import web
        for fn in (web.build_state, web.with_art, web.Context.art_href,
                   web.Handler.do_GET):
            self.assertNotIn("covers.attach", inspect.getsource(fn))
            self.assertNotIn("covers.resolve", inspect.getsource(fn))
            self.assertNotIn("covers.attach", inspect.getsource(fn))

    def test_the_cover_and_the_frame_never_share_a_cache_slot(self):
        vid = "abc123"
        yt = {"id": vid, "thumbnail": "https://i.ytimg.com/vi/abc123/mqdefault.jpg"}
        caa = dict(yt, cover_url="https://coverartarchive.org/release/m/front-500")
        self.assertEqual(thumbs.source_of(yt)[0], "yt")
        self.assertEqual(thumbs.source_of(caa), ("caa", caa["cover_url"]))
        self.assertNotEqual(str(thumbs.path_for(vid, "big", "yt")),
                            str(thumbs.path_for(vid, "big", "caa")))
        self.assertIsNone(thumbs.cached_path(caa, "big"))     # nothing fetched yet

    def test_switches(self):
        self.assertTrue(covers.enabled())
        self.assertFalse(covers.row_mode())
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_COVERS": "off"}):
            self.assertFalse(covers.enabled())
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_COVERS": "rows"}):
            self.assertTrue(covers.row_mode())
            self.assertTrue(covers.enabled())
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_ART": "0"}):
            self.assertFalse(covers.enabled())

    def test_the_doctor_reports_the_feature_in_its_own_shape(self):
        label, ok, detail = covers.doctor_check()
        self.assertIn("Cover Art Archive", label)
        self.assertIsInstance(ok, bool)
        self.assertTrue(detail)
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_COVERS": "off"}):
            label2, ok2, detail2 = covers.doctor_check()
        self.assertFalse(ok2)
        self.assertIn("off", detail2)


if __name__ == "__main__":
    unittest.main()


class ThumbSizeTests(unittest.TestCase):
    """
    `fit_size` is the difference between a 21 KB 480x360 decode per row and a
    2.9 KB 120x90 one, which is what a list of covers used to cost the UI thread.
    """

    def setUp(self):
        import thumbs
        self.thumbs = thumbs

    def test_youtube_gets_the_smallest_variant_that_fits(self):
        t = {"id": "dQw4w9WgXcQ"}
        self.assertEqual(self.thumbs.art_url(t, "row"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/default.jpg")
        self.assertEqual(self.thumbs.art_url(t, "big"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")

    def test_an_offered_thumbnail_is_resized_by_the_host_not_by_us(self):
        t = {"thumbnail": "https://i.ytimg.com/vi/abc123/mqdefault.jpg"}
        self.assertEqual(self.thumbs.art_url(t, "row"),
                         "https://i.ytimg.com/vi/abc123/default.jpg")
        g = {"thumbnail": "https://yt3.googleusercontent.com/hashxyz"}
        self.assertEqual(self.thumbs.fit_size(g["thumbnail"], 64),
                         "https://yt3.googleusercontent.com/hashxyz=s128-c")
        self.assertEqual(self.thumbs.fit_size(g["thumbnail"] + "=s0-c", 220),
                         "https://yt3.googleusercontent.com/hashxyz=s440-c")

    def test_other_hosts_are_left_alone(self):
        url = "https://coverartarchive.org/release/abc/front-500"
        self.assertEqual(self.thumbs.fit_size(url, 64), url)
        self.assertEqual(self.thumbs.fit_size("", 64), "")

    def test_the_two_sizes_never_share_a_cache_file(self):
        a = str(self.thumbs.path_for("vid1", "row", "yt"))
        b = str(self.thumbs.path_for("vid1", "big", "yt"))
        self.assertNotEqual(a, b)

    def test_source_of_uses_the_size(self):
        yt = {"id": "abc123"}
        self.assertEqual(self.thumbs.source_of(yt, "row"),
                         ("yt", "https://i.ytimg.com/vi/abc123/default.jpg"))
        caa = {"id": "abc123", "cover_url": "https://coverartarchive.org/x/front"}
        self.assertEqual(self.thumbs.source_of(caa, "row")[0], "caa")
