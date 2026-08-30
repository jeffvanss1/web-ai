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


def del_queue():
    """The lane's pending work is a plain list; keep one test's item out of the next."""
    del covers._queue[:]


def restore_stop(stopped: bool):
    if stopped:
        covers._stop.set()


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

    def _release_for_status(self, status: str) -> str:
        """The mbid a recording-only lookup would pick, with that release status."""
        rel, rec = "6f6b1a9c-2b1e-4a55-9b0a-1f2c3d4e5f60", \
                   "11111111-2222-3333-4444-555555555555"

        def answer(url, host):
            if "inc=releases" in url:
                return {"releases": [{"id": rel, "status": status}]}
            if "recording?" in url:
                return {"recordings": [{"id": rec}]}
            return {}
        with self.open_get(answer):
            return covers._recording_release_mbid("kanye west", "I Wonder")

    def test_a_promo_sampler_is_never_worn_as_the_cover(self):
        # measured on Kanye West's "I Wonder": the only release MusicBrainz lists for
        # that recording is "Lollapalooza 2008 (Chrysalis Music Sampler)", status
        # Promotion - so the hero was dressed in a festival flyer. A song that only
        # appears on promos keeps the frame it was found in, because a wrong cover in
        # a box this large costs more trust than no cover does
        self.assertEqual(self._release_for_status("Promotion"), "")
        self.assertEqual(self._release_for_status("Bootleg"), "")
        self.assertEqual(self._release_for_status("Pseudo-Release"), "")
        rel = "6f6b1a9c-2b1e-4a55-9b0a-1f2c3d4e5f60"
        self.assertEqual(self._release_for_status("Official"), rel,
                         "a release of the artist's own must still be used")
        self.assertEqual(self._release_for_status(""), rel,
                         "no status stated is not a demerit")

    def test_a_frame_is_stored_at_the_size_of_its_box(self):
        # a 256 px card used to ship the CDN's whole 1280x720 JPEG, 46 KB, per row.
        # ffmpeg is the tool that used to fix that and nobody installs a 60 MB
        # dependency for a thumbnail, so Pillow does it - and only when installed,
        # in which case the old pass-through stays exactly as it was.
        try:
            from PIL import Image
        except Exception:
            self.skipTest("no Pillow: the raw CDN bytes are the contract")
        blob = io.BytesIO()
        Image.effect_noise((1280, 720), 70).convert("RGB").save(
            blob, "JPEG", quality=95)
        blob = blob.getvalue()
        self.assertGreater(len(blob), 20_000, "the fixture must be a fat frame")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "vid1-yt-max-256.jpg"
            with mock.patch.object(thumbs, "_fetch", lambda url: blob):
                got = thumbs._store("https://i.ytimg.com/vi/vid1/maxresdefault.jpg",
                                    out, 256)
            self.assertEqual(got, str(out))
            with Image.open(out) as stored:
                side = min(stored.size)
            self.assertEqual(side, 256,
                             "the edge cover-crops from must land on the slot, not under it")
            self.assertLess(out.stat().st_size, len(blob), "smaller file, same pixels")
        self.assertFalse(thumbs._shrink(blob, 2048), "sizing to a box never upscales")
        small = io.BytesIO()
        Image.effect_noise((320, 180), 70).convert("RGB").save(small, "JPEG", quality=90)
        self.assertEqual(thumbs._shrink(small.getvalue(), 256), b"",
                         "an mqdefault frame is already slot-sized - store it as it came")

    def test_the_lookup_queue_carries_both_names(self):
        # `_resolve` wants the album AND the song: the album is what makes a
        # release-group hit trustworthy, the song is the rescue. Dropping either
        # field used to fail inside the worker's except, where it looked exactly
        # like "this track has no cover" - so the shape of this tuple is pinned.
        del covers._queue[:]
        self.addCleanup(del_queue)
        covers._enqueue({"id": "vid1", "title": "Runaway (feat. Pusha T)",
                         "artist": "kanye west", "album": "Late Registration"})
        self.assertEqual(len(covers._queue), 1)
        key, artist, what, album, vid = covers._queue.pop(0)
        self.assertEqual(artist, "kanye west")
        self.assertEqual(album, "Late Registration")
        self.assertEqual(what, "Late Registration", "an album name beats the song")
        self.assertEqual(vid, "vid1")
        self.assertEqual(key, covers.key_for("kanye west", "Late Registration"))
        # with no album, the cleaned song is what gets asked for
        covers._enqueue({"id": "vid2", "title": "Runaway (feat. Pusha T)",
                         "artist": "kanye west"})
        _key, _artist, what2, album2, _vid = covers._queue.pop(0)
        self.assertEqual((what2, album2), ("Runaway", ""))

    def test_a_worker_crash_is_named_not_swallowed(self):
        # "no cover exists" and "our code broke" have to read differently, or a bug
        # in this file hides behind an empty picture box forever
        def boom(*a, **k):
            raise NameError("simulated typo")
        was_stopped = covers._stop.is_set()
        covers._stop.clear()
        del covers._queue[:]
        self.addCleanup(restore_stop, was_stopped)
        self.addCleanup(del_queue)
        with mock.patch.object(covers, "_resolve", boom):
            covers._enqueue({"id": "vid3", "title": "T", "artist": "A"})
            covers._start()
            for _ in range(60):
                time.sleep(0.05)
                if covers.last_error().startswith("lookup crashed"):
                    break
        self.assertTrue(covers.last_error().startswith("lookup crashed"),
                        f"last_error was {covers.last_error()[:90]!r}")
        self.assertIn("simulated typo", covers.last_error())

    def test_a_store_suffix_does_not_hide_the_record(self):
        # a playlist is mostly featuring tracks, and every one of them is a lookup
        # that misses because of a bracket MusicBrainz has never heard of
        self.assertEqual(covers.song_title("Runaway (feat. Pusha T & Lil Wayne)"),
                         "Runaway")
        self.assertEqual(covers.song_title("Power (Album Version)"), "Power")
        self.assertEqual(covers.song_title("Gold - 2010 Remaster"), "Gold")
        self.assertEqual(covers.song_title("All Of The Lights (Remastered)"),
                         "All Of The Lights")
        for keep in ("Live and Let Die", "Skit #2", "Take On Me",
                     "Don't You (Forget About Me)", "Umbrella"):
            self.assertEqual(covers.song_title(keep), keep, "a real title is not a suffix")

    def test_the_index_key_is_the_cleaned_song(self):
        # keyed on the raw store title and all four variants of one song would ask
        # the rate-limited API the same question four times
        track = {"title": "Runaway (feat. Pusha T)", "artist": "kanye west"}
        self.assertEqual(covers.what_for(track), "Runaway")
        self.assertEqual(covers.key_for("kanye west", covers.what_for(track)),
                         covers.key_for("kanye west", "Runaway"))

    def test_a_near_title_is_not_the_record_we_asked_for(self):
        # measured live: release-group:"Champion" AND artist:"kanye west" answers
        # with the 2016 single "Champions". The picker used to take anything of a
        # believable shape, which is how the hero wore a festival flyer.
        def answer(url, host):
            return {"release-groups": [{"id": "wrong", "title": "Champions",
                                       "primary-type": "Single"}]}
        with self.open_get(answer):
            self.assertEqual(covers._release_group_mbid("kanye west", "Champion"), "")
            self.assertEqual(len(self.calls), 1, "a refusal is one call, not a retry")

    def test_an_edition_suffix_is_still_the_same_record(self):
        # "Graduation (Deluxe Edition)" is the album, and stores write it that way:
        # the suffix is stripped before comparing, but nothing else is forgiven
        def answer(url, host):
            return {"release-groups": [{"id": "hits", "title": "Greatest Hits",
                                       "primary-type": "Album"},
                                      {"id": "deluxe", "title": "Graduation [Deluxe Edition]",
                                       "primary-type": "Album"}]}
        with self.open_get(answer):
            self.assertEqual(covers._release_group_mbid("kanye west", "graduation"),
                             "deluxe")

    def test_a_promo_answer_is_rescued_by_the_single(self):
        # the recording path found only a promo sampler (so it says "no cover");
        # before giving up, ask MusicBrainz for the single of that name - which is
        # where "Runaway", "Power" and friends keep their own artwork
        group = "9b3a5a10-1234-4abc-8def-0123456789ab"

        def answer(url, host):
            if "release-group?" in url:
                return {"release-groups": [{"id": group, "title": "Runaway",
                                           "primary-type": "Single"}]}
            if "inc=releases" in url:
                return {"releases": [{"id": "promo", "status": "Promotion"}]}
            if "recording?" in url:
                return {"recordings": [{"id": "11111111-2222-3333-4444-555555555555"}]}
            return {}
        with self.open_get(answer):
            self.assertEqual(covers._resolve("kanye west", "Runaway", "", fresh=True),
                             (group, "group"))
            self.assertEqual(len(self.calls), 3,
                             "recording, its releases, then one call for the single")

    def test_the_answered_lookup_still_costs_two_calls(self):
        # the quota is one call per second per the docs, so a row that MusicBrainz
        # can answer straight away must not shop for a single as well
        def answer(url, host):
            if "inc=releases" in url:
                return {"releases": [{"id": "rel-1", "status": "Official"}]}
            if "recording?" in url:
                return {"recordings": [{"id": "11111111-2222-3333-4444-555555555555"}]}
            if "release-group?" in url:
                raise AssertionError("no single hunt when the recording already answered")
            return {}
        with self.open_get(answer):
            self.assertEqual(covers._resolve("kanye west", "I Wonder", "", fresh=True),
                             ("rel-1", "release"))
            self.assertEqual(len(self.calls), 2, str(self.calls))

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
        self.assertEqual(thumbs.source_of(yt, "row")[0], "yt-mq")
        self.assertEqual(thumbs.source_of(yt, "big")[0], "yt-max")
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
    `fit_size` decides whether a card shows a cover or a smear.

    The page has three slots a picture goes into - a 40 px row, a 190 px card and a
    300 px hero - and one 64 px file for all three is what made the grid look like a
    mosaic of blown-up thumbnails. Each slot now asks YouTube's CDN for the rung that
    actually fits it, and the ladder has a step below for when a rung 404s.
    """

    def setUp(self):
        import thumbs
        self.thumbs = thumbs

    def test_youtube_gets_the_rung_that_fits_the_slot(self):
        t = {"id": "dQw4w9WgXcQ"}
        self.assertEqual(self.thumbs.art_url(t, "row"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg")
        self.assertEqual(self.thumbs.art_url(t, "card"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg")
        self.assertEqual(self.thumbs.art_url(t, "big"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
                         "1280x720 is the only HD rung YouTube serves unletterboxed")

    def test_the_padded_rungs_are_never_asked_for(self):
        # measured on three live videos: every corner of sddefault.jpg and
        # hqdefault.jpg is (0,0,0) - YouTube pads the 4:3 rungs itself - so the
        # bars arrive baked into the file and a square card is left wearing them
        t = {"id": "x1"}
        for slot in ("card", "big"):
            url = self.thumbs.art_url(t, slot)
            self.assertNotIn("hqdefault", url)
            self.assertNotIn("sddefault", url)

    def test_smaller_rung_walks_down_the_ladder(self):
        rung = self.thumbs.smaller_rung
        # maxres 404s on some uploads, and the step below it is a padded 4:3 frame:
        # straight to mqdefault, which is smaller but has no bars
        self.assertEqual(rung("https://i.ytimg.com/vi/x/maxresdefault.jpg"),
                         "https://i.ytimg.com/vi/x/mqdefault.jpg")
        self.assertEqual(rung("https://i.ytimg.com/vi/x/sddefault.jpg"),
                         "https://i.ytimg.com/vi/x/mqdefault.jpg")
        self.assertEqual(rung("https://i.ytimg.com/vi/x/hqdefault.jpg"),
                         "https://i.ytimg.com/vi/x/mqdefault.jpg")
        self.assertEqual(rung("https://coverartarchive.org/release/x/front-500"), "",
                         "an archive URL has no rungs to walk down")
        self.assertEqual(rung(""), "")

    def test_the_sizes_match_the_slots_the_page_draws(self):
        sizes = self.thumbs.SIZES
        self.assertEqual(sorted(sizes), ["big", "card", "row"])
        self.assertGreaterEqual(sizes["row"], 64, "a 40px row at 2x still wants 72")
        self.assertGreaterEqual(sizes["card"], 220, "cards are drawn ~190px wide")
        self.assertGreaterEqual(sizes["big"], 480, "the hero is 300px and blurred at 2x")

    def test_an_offered_thumbnail_is_resized_by_the_host_not_by_us(self):
        t = {"thumbnail": "https://i.ytimg.com/vi/abc123/hqdefault.jpg"}
        self.assertEqual(self.thumbs.art_url(t, "row"),
                         "https://i.ytimg.com/vi/abc123/mqdefault.jpg")
        g = {"thumbnail": "https://yt3.googleusercontent.com/hashxyz"}
        self.assertEqual(self.thumbs.fit_size(g["thumbnail"], 64),
                         "https://yt3.googleusercontent.com/hashxyz=s128-c")
        self.assertEqual(self.thumbs.fit_size(g["thumbnail"] + "=s0-c", 220),
                         "https://yt3.googleusercontent.com/hashxyz=s440-c")

    def test_other_hosts_are_left_alone(self):
        url = "https://coverartarchive.org/release/abc/front-500"
        self.assertEqual(self.thumbs.fit_size(url, 64), url)
        self.assertEqual(self.thumbs.fit_size("", 64), "")

    def test_each_size_gets_its_own_cache_file(self):
        paths = [str(self.thumbs.path_for("vid1", slot, "yt"))
                 for slot in ("row", "card", "big")]
        self.assertEqual(len(set(paths)), 3, paths)

    def test_source_of_uses_the_size(self):
        yt = {"id": "abc123"}
        # the tag carries the rung, so a ladder fix can never be shadowed by the
        # barred image cached before it
        self.assertEqual(self.thumbs.source_of(yt, "row"),
                         ("yt-mq", "https://i.ytimg.com/vi/abc123/mqdefault.jpg"))
        self.assertEqual(self.thumbs.source_of(yt, "card"),
                         ("yt-max", "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"))
        caa = {"id": "abc123", "cover_url": "https://coverartarchive.org/x/front"}
        self.assertEqual(self.thumbs.source_of(caa, "row")[0], "caa")

    def test_a_small_slot_borrows_the_bigger_file_instead_of_downloading(self):
        # 20 rows x 3 sizes was 60 CDN round trips for one page; a row that can use
        # the card file it already has is the same picture at the same sharpness
        d = Path(self.thumbs.cache_dir())
        d.mkdir(parents=True, exist_ok=True)
        big = d / "borrowvid-yt-max-256.jpg"
        big.write_bytes(b"\xff\xd8\xff" + b"0" * 400)
        calls = []
        real = self.thumbs._store
        self.thumbs._store = lambda url, out, px: (calls.append(url), None)[1]
        try:
            # the tag of the row's own rung (yt-mq) is different and it still uses
            # the card's file: the borrow is about pixels, not filenames
            self.assertEqual(self.thumbs.get({"id": "borrowvid"}, "row"), str(big))
            self.assertEqual(calls, [], "a slot with a bigger file must not fetch")
            # and the borrow only goes one way: a 256 file is not the hero's 512
            self.assertIsNone(self.thumbs.get({"id": "borrowvid"}, "big"))
            self.assertEqual(len(calls), 1, "the hero asks the CDN for its own rung")
            self.assertIn("maxresdefault", calls[0],
                         "the hero must ask for the clean HD rung")
        finally:
            self.thumbs._store = real
            big.unlink(missing_ok=True)
            for extra in ("borrowvid-yt-mq-72.jpg", "borrowvid-yt-max-512.jpg",
                          "borrowvid-yt-max-256.jpg.part"):
                (d / extra).unlink(missing_ok=True)

    def test_one_archive_file_serves_every_slot_it_is_bigger_than(self):
        # the archive gives one URL per rung and covers.py asks for the biggest, so
        # a row at 72px is a local downscale of the same 500px bytes: no extra calls
        caa = {"id": "abc123", "cover_url": "https://coverartarchive.org/r/123/front-500"}
        for slot in ("row", "card", "big"):
            self.assertEqual(self.thumbs.source_of(caa, slot)[1],
                             "https://coverartarchive.org/r/123/front-500")
