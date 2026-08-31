"""
Regressions for the "Gemini shows 0% success / looks like a timeout" class of
bug: swallowed errors, hard-coded timeouts, non-persisted keys, and the engine
selector that labelled a working local model "offline".
"""
from __future__ import annotations

import io
import json
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401

import brain
import config
import dj
import workerclient


class QueryHygieneTests(unittest.TestCase):
    """`search_query` is what keeps "to relax" from becoming a search for rain."""

    def test_activity_words_are_trimmed_off_the_query(self):
        self.assertEqual(brain.search_query("lofi beats to relax"), "lofi beats")
        self.assertEqual(brain.search_query("chill hiphop for studying"), "chill hiphop")
        self.assertEqual(brain.search_query("80s hits to drive to"), "80s hits")
        self.assertEqual(brain.search_query("soft jazz for dinner"), "soft jazz")

    def test_a_query_that_is_only_an_activity_is_dropped(self):
        for phrase in ("to relax", "music for studying", "songs to fall asleep to",
                       "playlist", "relaxing music for sleep", "background music"):
            self.assertEqual(brain.search_query(phrase), "", phrase)

    def test_a_band_or_song_name_survives_untouched(self):
        for phrase in ("nirvana nevermind", "sleeping with the enemy",
                       "relax by Frankie Goes to Hollywood", "some 90s trip hop"):
            self.assertEqual(brain.search_query(phrase), phrase.lower(), phrase)

    def test_build_queue_searches_the_trimmed_form(self):
        # the plan still mentions the mood, only the search string changes
        seen = []

        def fake_yt_search(query, limit=12, **kw):
            seen.append(query)
            return []

        with mock.patch.object(dj.prov, "yt_search", side_effect=fake_yt_search), \
                mock.patch.object(dj.prov.Spotify, "liked", return_value=[]), \
                mock.patch.object(dj.prov.Spotify, "recently_played", return_value=[]):
            dj.build_queue("lofi beats to relax", count=5)
        self.assertTrue(seen, "no search was issued")
        for s in seen:
            self.assertNotIn("relax", s)


class _Iso(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._ps = [mock.patch.object(config, "APP_DIR", d),
                    mock.patch.object(config, "LLM_CONFIG_FILE", d / "config.json"),
                    mock.patch.object(config, "STATE_FILE", d / "state.json"),
                    mock.patch.object(config, "HISTORY_FILE", d / "history.jsonl")]
        for x in self._ps:
            x.start()
        self._saved = (config.LLM_API_KEY, config.LLM_BASE_URL,
                       config.LLM_MODEL, config.LLM_TIMEOUT,
                       config.WORKER_URL, config.WORKER_TOKEN)
        brain.LAST_ERRORS.clear()
        brain.NOTES.clear()
        config.LLM_BASE_URL = ""
        # every test starts unconfigured unless it says otherwise: the Worker is
        # the Gemini path now, so a URL left over from another test is exactly
        # the kind of leak that makes a suite pass on one machine only
        config.WORKER_URL = ""
        config.WORKER_TOKEN = ""
        workerclient._health["data"] = None
        workerclient._health["at"] = 0.0
        workerclient.LAST_OK = None
        workerclient.LAST_ERROR = None

    def tearDown(self):
        for x in self._ps:
            x.stop()
        (config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL,
         config.LLM_TIMEOUT, config.WORKER_URL, config.WORKER_TOKEN) = self._saved
        self._tmp.cleanup()


class EngineSelectionTests(_Iso):
    """The engine selector: the Worker first, then a local model, then offline."""

    def test_a_worker_url_wins_over_a_local_base_and_a_key(self):
        """The Worker is an explicit 'get Gemini from there', so it takes precedence."""
        config.WORKER_URL = "https://spotube-dj.example.workers.dev"
        config.LLM_BASE_URL = "http://localhost:11434"
        config.LLM_API_KEY = "k"
        self.assertEqual(brain.configured_engine(), "worker")

    def test_any_non_gemini_base_is_local_llm(self):
        for base in ("http://localhost:11434", "http://192.168.1.50:11434",
                     "http://host.docker.internal:11434", "http://ollama.local:11434"):
            config.WORKER_URL = ""
            config.LLM_BASE_URL = base
            config.LLM_API_KEY = ""
            self.assertEqual(brain.configured_engine(), "local-llm", base)

    def test_a_gemini_key_on_its_own_is_no_engine(self):
        """The old silent failure, inverted.

        A key used to mean 'gemini' even with nowhere to send it, which is how
        a machine with a key and no network spent 45s per track discovering it
        was offline. The key is only worth something to a Worker that asked for
        it, so an install with a key and no Worker URL is offline, and says so.
        """
        config.WORKER_URL = ""
        config.LLM_API_KEY = "AIzaXYZ"
        config.LLM_BASE_URL = ""
        self.assertEqual(brain.configured_engine(), "offline")

    def test_a_google_base_url_is_not_a_local_model(self):
        """A Google base URL is how you used to point at Gemini directly. It is
        not a local endpoint, so it must not be classed local-llm either."""
        config.WORKER_URL = ""
        config.LLM_BASE_URL = config.GEMINI_DEFAULT_URL
        config.LLM_API_KEY = "k"
        self.assertEqual(brain.configured_engine(), "offline")

    def test_truly_nothing_configured_is_offline(self):
        config.LLM_API_KEY = ""
        config.LLM_BASE_URL = ""
        config.WORKER_URL = ""
        self.assertEqual(brain.configured_engine(), "offline")


class PersistenceTests(_Iso):
    def test_saved_key_survives_a_fresh_apply(self):
        config.save_llm_config(LLM_API_KEY="AIzaXYZ", LLM_MODEL="gemini-2.5-flash")
        config.LLM_API_KEY = ""          # simulate process restart
        config.LLM_MODEL = ""
        config.apply_llm_overrides()
        self.assertEqual(config.LLM_API_KEY, "AIzaXYZ")
        self.assertEqual(config.LLM_MODEL, "gemini-2.5-flash")

    def test_key_is_written_mode_600(self):
        import os
        config.save_llm_config(LLM_API_KEY="secret")
        mode = os.stat(config.LLM_CONFIG_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_env_overrides_the_file(self):
        config.save_llm_config(LLM_API_KEY="from-file")
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "from-env"}):
            self.assertEqual(config.load_llm_config()["LLM_API_KEY"], "from-env")

    def test_blank_base_is_not_refilled_with_the_gemini_default(self):
        """Settings must be able to CLEAR a field; `or default` made it impossible."""
        config.save_llm_config(LLM_BASE_URL="")
        self.assertEqual(config.load_llm_config().get("LLM_BASE_URL", ""), "")

    def test_a_poked_at_state_file_cannot_stop_the_app(self):
        # state.json is the one file a listener is likely to open and edit, and
        # every one of these shapes used to be an AttributeError at startup
        shapes = (
            {"artists": [], "genres": ["portishead"]},
            {"artists": {"a": None, "b": "2.5", 7: "x"}, "genres": "nope"},
            {"liked": "not a list", "skipped": [None, "junk", {"title": "ok"}]},
            {"volume": "loud", "autoplay": "yes", "last_request": None,
             "player": "vlc"},
            [1, 2, 3],
            "just a string",
        )
        for shape in shapes:
            config.STATE_FILE.write_text(json.dumps(shape))
            st = config.load_state()                      # must not raise
            self.assertIsInstance(st["artists"], dict, shape)
            self.assertIsInstance(st["genres"], dict, shape)
            self.assertIsInstance(st["liked"], list, shape)
            self.assertIsInstance(st["skipped"], list, shape)
            self.assertIn(st["player"], ("mpv", "spotube"), shape)
            self.assertIsInstance(st["volume"], int, shape)
            self.assertTrue(0 <= st["volume"] <= 100, shape)
            self.assertEqual(len(st["skipped"]), 0 if not isinstance(shape, dict)
                             or "skipped" not in shape else 1, shape)

    def test_a_name_list_is_not_invented_into_a_weight(self):
        # a bare list of names carries no strength; guessing one is how the DJ
        # starts recommending something you never actually favoured
        config.STATE_FILE.write_text(json.dumps({"artists": ["portishead", ["x", 3]]}))
        st = config.load_state()
        self.assertEqual(st["artists"], {"x": 3.0})

    def test_save_state_survives_a_partial_dict(self):
        config.save_state({"liked": [{"title": "t", "artist": "a"}]})   # no skipped/weights
        st = config.load_state()
        self.assertEqual(st["skipped"], [])
        self.assertEqual(st["artists"], {})

    def test_state_cache_is_a_fresh_copy_and_sees_a_rewrite(self):
        # /api/state calls load_state once a second; the cache must hand back an
        # independent copy (taste mutates what it gets and saves it) and must
        # invalidate the moment save_state writes a new file.
        config.save_state({"liked": [{"title": "t", "artist": "a"}], "volume": 70})
        first = config.load_state()
        second = config.load_state()
        self.assertEqual(first, second)              # same snapshot ...
        self.assertIsNot(first, second)              # ... but not the same object
        self.assertIsNot(first["liked"], second["liked"])
        first["liked"].append({"title": "mut", "artist": "b"})   # mutate the copy
        self.assertEqual(len(config.load_state()["liked"]), 1)   # cache unpolluted
        config.save_state({"liked": [{"title": "two", "artist": "c"}], "volume": 90})
        fresh = config.load_state()
        self.assertEqual(len(fresh["liked"]), 1)
        self.assertEqual(fresh["liked"][0]["title"], "two")
        self.assertEqual(fresh["volume"], 90)

    def test_state_cache_not_poisoned_by_a_previous_temp_home(self):
        # the cache identity includes the path, so a test that re-points STATE_FILE
        # at a fresh temp dir must not read a prior temp dir's snapshot
        config.save_state({"liked": [{"title": "this-dir"}]})
        self.assertEqual(config.load_state()["liked"][0]["title"], "this-dir")
        d2 = Path(self._tmp.name) / "other"
        d2.mkdir()
        with mock.patch.object(config, "STATE_FILE", d2 / "state.json"):
            config.save_state({"liked": [{"title": "other-dir"}]})
            self.assertEqual(config.load_state()["liked"][0]["title"], "other-dir")

    def test_timeout_floor_and_garbage(self):
        config.save_llm_config(LLM_TIMEOUT=1)
        self.assertGreaterEqual(config.load_llm_config()["LLM_TIMEOUT"], 5)
        config.save_llm_config(LLM_TIMEOUT="nope")
        self.assertNotIn("LLM_TIMEOUT", config.load_llm_config())


class TimeoutTests(_Iso):
    def test_timeout_is_not_hard_coded_25s_anymore(self):
        config.LLM_TIMEOUT = 45
        self.assertEqual(brain._timeout(), 45)
        config.LLM_TIMEOUT = 0            # absurd value gets floored, not honoured
        self.assertEqual(brain._timeout(), 5.0)
        config.LLM_TIMEOUT = "junk"
        self.assertEqual(brain._timeout(), 45.0)

    def test_timeout_is_passed_to_urlopen(self):
        seen = {}
        class Resp:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def fake(req, timeout=None):
            seen["timeout"] = timeout
            return Resp()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            brain._post("http://x", {}, {}, timeout=33)
        self.assertEqual(seen["timeout"], 33)

    def test_the_worker_call_carries_the_configured_budget(self):
        """The bug was a literal `timeout=25` at the urlopen call site."""
        config.WORKER_URL = "https://dj.test"
        config.LLM_TIMEOUT = 41
        with _Capture([urllib.error.URLError("boom")]) as cap:
            brain._gemini("x")
        self.assertTrue(cap.hits("/v1/plan"), "expected the request to be attempted")
        for hit in cap.hits("/v1/plan"):
            self.assertEqual(hit["timeout"], 41)

    def test_no_worker_url_costs_no_request_and_says_why(self):
        config.WORKER_URL = ""
        with _Capture([]) as cap:
            out = brain._gemini("x")
        self.assertIsNone(out)
        self.assertEqual(cap.seen, [], "an unconfigured app must not dial out at all")
        self.assertIn("no Worker URL", brain.LAST_ERRORS["worker"])

    def test_a_rejected_key_does_not_waste_a_retry(self):
        """Auth is the problem; hammering it is how you trip a rate limit."""
        config.WORKER_URL = "https://dj.test"
        err = _worker_err("key", "API key not valid. Please pass a valid API key.",
                          status="API_KEY_INVALID", code=401)
        with _Capture([err, _FakeResp(_worker_plan())]) as cap:
            brain._gemini("x")
        self.assertEqual(len(cap.hits("/v1/plan")), 1,
                          f"a dead key must not be retried: {cap.seen}")
        self.assertIn("key not valid", brain.LAST_ERRORS["worker"].lower())


class ErrorClassificationTests(_Iso):
    """
    Caught against the real generativelanguage.googleapis.com endpoint: a bad
    key comes back as 400 INVALID_ARGUMENT (not 401, not API_KEY_INVALID), and
    HTTPError.fp can only be read once. Both broke an "obviously right" first
    version of the classifier.
    """

    def _err(self, code, status, message):
        import io
        body = json.dumps({"error": {"code": code, "message": message,
                                     "status": status}}).encode()
        return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))

    def test_google_reports_a_bad_key_as_400_invalid_argument(self):
        e = self._err(400, "INVALID_ARGUMENT",
                      "API key not valid. Please pass a valid API key.")
        self.assertEqual(brain.classify_http(e)[0], "key")
        text = brain._http_error_text(e)
        self.assertIn("key not valid", text)
        self.assertNotIn("bad model name", text)

    def test_the_message_survives_being_read_twice(self):
        e = self._err(403, "PERMISSION_DENIED", "Permission denied on this project.")
        first = brain._http_error_text(e)
        again = brain._http_error_text(e)
        self.assertEqual(first, again)
        self.assertIn("Permission denied", first)

class ErrorSurfacingTests(_Iso):
    def _err(self, code, status, msg):
        import io
        body = json.dumps({"error": {"code": code, "message": msg, "status": status}}).encode()
        return urllib.error.HTTPError("u", code, msg[:40], {}, io.BytesIO(body))

    def test_http_error_text_is_actionable_per_status(self):
        cases = [(400, "API_KEY_INVALID", "API key not valid. Please pass a valid API key.", "key not valid"),
                 (401, "UNAUTHENTICATED", "Invalid Authentication", "regenerate"),
                 (403, "PERMISSION_DENIED", "Permission denied on project", "access"),
                 (404, "NOT_FOUND", "model not found", "model not found"),
                 (429, "RESOURCE_EXHAUSTED", "Quota exceeded", "quota"),
                 (400, "INVALID_ARGUMENT", 'Unknown name "responseFormat"', "responseformat")]
        for code, status, msg, needle in cases:
            text = brain._http_error_text(self._err(code, status, msg)).lower()
            self.assertIn(needle, text, f"{code}/{status}: {text}")

    def test_a_400_says_key_only_when_the_api_says_key(self):
        """The old code hard-coded 'API key not valid' for every 400, which hid
        the real fault (retired model, unsupported field) behind a wrong advice."""
        t = brain._http_error_text(self._err(400, "INVALID_ARGUMENT",
                                            'Unknown name "responseFormat" at "generationConfig"'))
        self.assertNotIn("key not valid", t.lower())
        self.assertIn("responseFormat", t)

    def test_connection_refused_says_start_the_server(self):
        import urllib.error
        msg = brain._net_error_text(urllib.error.URLError(
            "[Errno 111] Connection refused"), "http://localhost:11434")
        self.assertIn("nothing listening", msg)
        self.assertIn("ollama serve", msg)

    def test_refused_on_a_remote_host_does_not_blame_ollama(self):
        msg = brain._net_error_text(urllib.error.URLError(
            "[Errno 111] Connection refused"),
            "https://generativelanguage.googleapis.com/v1beta/models")
        self.assertIn("connection refused", msg)
        self.assertNotIn("ollama serve", msg)

    def test_refused_on_a_lan_host_still_sounds_local(self):
        msg = brain._net_error_text(urllib.error.URLError(
            "[Errno 111] Connection refused"), "http://192.168.1.50:11434")
        self.assertIn("connection refused", msg)

    def test_plan_carries_the_reason_through(self):
        """Whole real path, only the socket is faked: a refused connection must
        reach the caller as a readable reason, not a silent 'offline'."""
        config.WORKER_URL = "https://dj.test"
        with _Capture([urllib.error.URLError("[Errno 111] Connection refused")]):
            p = brain.plan("lofi")
        self.assertIn("worker (fallback)", p["engine"])
        self.assertTrue(p.get("llm_error"), "plan() must expose why it fell back")
        self.assertIn("nothing listening", p["llm_error"])

    def test_plan_never_raises_on_a_weird_api_error(self):
        """An AttributeError from a malformed body must not kill the whole app."""
        config.WORKER_URL = "https://dj.test"
        with mock.patch.object(brain, "_gemini", side_effect=AttributeError("'list' has no get")), \
             mock.patch.object(brain, "_openai_compat", side_effect=AttributeError("nope")):
            p = brain.plan("lofi")
        self.assertIn("fallback", p["engine"])
        self.assertTrue(p["queries"], "music must still be queued")

    def test_second_engine_saves_the_plan(self):
        """The Worker fails but a local model answers -> not reported as a failure."""
        config.WORKER_URL = "https://dj.test"
        with mock.patch.object(brain, "_gemini", return_value=None), \
             mock.patch.object(brain, "_openai_compat",
                               return_value={"queries": ["lofi study beats"],
                                             "why": "local model", "avoid": []}):
            p = brain.plan("lofi")
        self.assertFalse(p.get("llm_error"), "a working second engine is not a failure")
        self.assertEqual(p["queries"], ["lofi study beats"])

    def test_build_queue_does_not_drop_the_reason(self):
        import dj, providers as prov
        with mock.patch.object(brain, "plan", return_value={"queries": ["a b"], "why": "w",
                                                            "engine": "gemini (fallback)",
                                                            "llm_error": "HTTP 400 key bad"}), \
             mock.patch.object(prov, "yt_search",
                               lambda *x, **k: [{"id": "1", "title": "t a b",
                                                 "artist": "x", "duration": 200, "url": "u"}]), \
             mock.patch.object(prov.Spotify, "playlist_seed", return_value=[]):
            _, info = dj.build_queue("lofi")
        self.assertEqual(info["llm_error"], "HTTP 400 key bad")

    def test_nothing_configured_means_no_error_noise(self):
        config.WORKER_URL = ""
        config.LLM_API_KEY = ""
        config.LLM_BASE_URL = ""
        p = brain.plan("dark techno")
        self.assertEqual(p["engine"], "offline")
        self.assertFalse(p.get("llm_error"))


class WorkerContractTests(_Iso):
    """
    What the client actually puts on the wire.

    The parsing of a Gemini reply (multi-part text, fences, a truncated array)
    lives in `worker/src/index.js` now and is tested there; this file's job is
    the other half of the contract - that this app asks for the right thing,
    sends no key it was not asked for, and adopts the model that answered.
    """

    def test_the_plan_call_posts_the_prompt_and_the_system_prompt(self):
        config.WORKER_URL = "https://dj.test"
        with _Capture([]) as cap:
            out = brain._gemini("chill lofi for coding")
        self.assertEqual(out["queries"], ["lofi one", "lofi two"])
        hit = cap.hits("/v1/plan")[0]
        self.assertEqual(hit["url"], "https://dj.test/v1/plan")
        self.assertIn("chill lofi for coding", hit["body"]["prompt"])
        self.assertTrue(hit["body"]["system"], "the planner's system prompt must travel")
        self.assertGreater(hit["body"]["timeoutMs"], 0)

    def test_no_key_ever_leaves_on_the_wire(self):
        """A key in a URL lands in a shell history; one in a header lands in a log."""
        config.WORKER_URL = "https://dj.test"
        config.LLM_API_KEY = "AIzaSECRET"
        with _Capture([]) as cap:
            brain._gemini("x")
        hit = cap.hits("/v1/plan")[0]
        headers = {k.title(): v for k, v in hit["headers"].items()}
        self.assertNotIn("AIzaSECRET", hit["url"])
        self.assertNotIn("key=", hit["url"])
        self.assertNotIn("AIzaSECRET", json.dumps(hit["body"]))
        self.assertNotIn("AIzaSECRET", json.dumps(headers))

    def test_the_machine_key_is_only_sent_to_a_worker_that_asked_for_it(self):
        config.WORKER_URL = "https://dj.test"
        config.LLM_API_KEY = "AIzaSECRET"
        with mock.patch.object(workerclient, "wants_client_key", return_value=True):
            with _Capture([]) as cap:
                brain._gemini("x")
        headers = {k.title(): v for k, v in cap.hits("/v1/plan")[0]["headers"].items()}
        self.assertEqual(headers.get("X-Gemini-Key"), "AIzaSECRET")

    def test_the_token_goes_in_a_bearer_header(self):
        config.WORKER_URL = "https://dj.test"
        config.WORKER_TOKEN = "shh"
        with _Capture([]) as cap:
            brain._gemini("x")
        headers = {k.title(): v for k, v in cap.hits("/v1/plan")[0]["headers"].items()}
        self.assertEqual(headers.get("Authorization"), "Bearer shh")

    def test_the_model_that_answered_is_remembered(self):
        """A Worker-side switch costs one request, not every request forever."""
        config.WORKER_URL = "https://dj.test"
        config.LLM_MODEL = "gemini-2.0-flash"
        with _Capture([_FakeResp(_worker_plan(model="gemini-3.6-flash"))]), \
             mock.patch.object(config, "save_llm_config") as saved:
            brain._gemini("x")
        self.assertEqual(config.LLM_MODEL, "gemini-3.6-flash")
        saved.assert_called_once_with(LLM_MODEL="gemini-3.6-flash")
        self.assertTrue(any("switched the model" in n for n in brain.pop_notes()))

    def test_the_workers_notes_reach_the_log_drawer(self):
        config.WORKER_URL = "https://dj.test"
        notes = ["gemini-2.0-flash is retired - the API says to use gemini-3.6-flash"]
        with _Capture([_FakeResp(_worker_plan(model="gemini-3.6-flash", notes=notes))]):
            brain._gemini("x")
        self.assertTrue(any("retired" in n for n in brain.pop_notes()))

    def test_a_failure_kind_is_worded_for_a_listener(self):
        config.WORKER_URL = "https://dj.test"
        err = _worker_err("quota", "Quota exceeded for requests per minute",
                          status="RESOURCE_EXHAUSTED", code=429)
        with _Capture([err]):
            self.assertIsNone(brain._gemini("x"))
        self.assertIn("quota", brain.LAST_ERRORS["worker"].lower())

    def test_an_unreachable_worker_names_the_deployment(self):
        """'is it deployed?' is the question a listener can actually act on."""
        config.WORKER_URL = "https://dj.test"
        with _Capture([urllib.error.URLError("[Errno 111] Connection refused")]):
            self.assertIsNone(brain._gemini("x"))
        self.assertIn("wrangler dev", brain.LAST_ERRORS["worker"])

    def test_queries_collapse_to_one_line(self):
        config.WORKER_URL = "https://dj.test"
        with mock.patch.object(brain, "_gemini",
                               return_value={"queries": ["neo   soul\nvelvet", " x "]},
                               ):
            p = brain.plan("anything")
        self.assertEqual(p["queries"], ["neo soul velvet"])   # " x " is too short, dropped

    def test_truncated_reply_is_salvaged_not_discarded(self):
        """maxOutputTokens cuts mid-array; the queries are still in there and
        are better than falling back to the offline parser."""
        cut = '{"queries": ["neo soul velvet", "slow jam philly", "70s str'
        out = brain._extract_json(cut)
        self.assertEqual(out["queries"], ["neo soul velvet", "slow jam philly"])
        self.assertTrue(out.get("truncated"))

    def test_salvage_does_not_leak_other_keys(self):
        """"avoid"/"why" are not search strings; a greedy salvage turned them
        into queries, which searches YouTube for the literal word "remix"."""
        broken = ('{"queries": ["slow jam", "neo soul"], '
                  '"avoid": ["remix", "live"], "why": "model rambled on')
        out = brain._extract_json(broken)
        self.assertEqual(out["queries"], ["slow jam", "neo soul"])

    def test_extract_json_accepts_a_bare_list(self):
        self.assertEqual(brain._extract_json('["a b", "c d"]'), {"queries": ["a b", "c d"]})
        self.assertEqual(brain._extract_json('```json\n{"queries":["x y"]}\n```')["queries"], ["x y"])
        self.assertIsNone(brain._extract_json("no json at all"))


class CliNoteTests(_Iso):
    """The CLI must SAY when it skipped the brain - but on stderr, because
    `--json | jq` must stay parseable."""

    def setUp(self):
        super().setUp()
        # `import __main__` here is the unittest runner, not our CLI - load it
        # by path like a real module instead.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "spotube_dj_cli", str(Path(__file__).resolve().parents[1]
                                  / "spotube_dj" / "__main__.py"))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_note_goes_to_stderr_not_stdout(self):
        m = self.m
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            m._brain_note({"llm_error": "gemini: HTTP 400 API key not valid"})
        self.assertEqual(buf_out.getvalue(), "")
        self.assertIn("HTTP 400", buf_err.getvalue())
        self.assertIn("--test-brain", buf_err.getvalue())

    def test_no_note_when_nothing_failed(self):
        m = self.m
        buf_out, buf_err = io.StringIO(), io.StringIO()
        for info in ({}, {"engine": "offline"}, {"engine": "gemini", "llm_error": ""}):
            with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
                m._brain_note(info)
        self.assertEqual(buf_out.getvalue() + buf_err.getvalue(), "")


_STATUS_BY_CODE = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
                   404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL"}


def _http_err(code: int, message: str, status: str | None = None):
    """
    A believable Google error body. The status has to match the code: a fixture
    that stamps NOT_FOUND on a 400 makes a config rejection look like a retired
    model, and the product (correctly) walks the model ladder instead of the
    payload shapes - which is what this helper used to hide.
    """
    import io
    body = json.dumps({"error": {"code": code, "message": message,
                                 "status": status or _STATUS_BY_CODE.get(code, "UNKNOWN")}}).encode()
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


class ModelLadderTests(_Iso):
    """
    The model ladder itself is tested in `worker/test/smoke.mjs`, next to the
    code that walks it. What has to be true here is the other half: this app
    sends the model it was configured with, adopts whatever the Worker says
    answered, and never re-implements the walk on the client.
    """

    def test_the_configured_model_is_sent_but_the_answer_is_what_counts(self):
        config.WORKER_URL = "https://dj.test"
        config.LLM_MODEL = "gemini-2.0-flash"
        with _Capture([_FakeResp(_worker_plan(model="gemini-3.6-flash"))]) as cap:
            out = brain._gemini("x")
        self.assertEqual(cap.hits("/v1/plan")[0]["body"]["model"], "gemini-2.0-flash")
        self.assertEqual(out["queries"], ["lofi one", "lofi two"])

    def test_a_retired_model_is_not_walked_twice(self):
        """The ladder is the Worker's job; the client must not also walk one.

        A `model` error from the Worker means it already tried the ladder and
        the model Google named, so a second call from here would be four more
        requests for the same answer.
        """
        config.WORKER_URL = "https://dj.test"
        err = _worker_err("model",
                          "models/gemini-2.0-flash is no longer available. "
                          "Please update your code to use models/gemini-3.6-flash.",
                          status="NOT_FOUND", code=404,
                          notes=["gemini-2.0-flash is retired - trying gemini-3.6-flash"])
        with _Capture([err, _FakeResp(_worker_plan(model="gemini-3.6-flash"))]) as cap:
            self.assertIsNone(brain._gemini("x"))
        self.assertEqual(len(cap.hits("/v1/plan")), 1)
        self.assertTrue(any("retired" in n for n in brain.pop_notes()))

    def test_a_bad_key_costs_exactly_one_request(self):
        """No model walking, no shape walking, no retry: auth is the problem."""
        config.WORKER_URL = "https://dj.test"
        with _Capture([_worker_err("key", "API key not valid. Please pass a valid API key.",
                                   status="API_KEY_INVALID", code=401)]) as cap:
            self.assertIsNone(brain._gemini("x"))
        self.assertEqual(len(cap.hits("/v1/plan")), 1)

    def test_timeouts_are_explained_not_swallowed(self):
        config.WORKER_URL = "https://dj.test"
        config.LLM_TIMEOUT = 30
        with _Capture([urllib.error.URLError("The read operation timed out")]) as cap:
            self.assertIsNone(brain._gemini("x"))
        self.assertEqual(len(cap.hits("/v1/plan")), 1)
        self.assertIn("timed out", brain.LAST_ERRORS["worker"])

    def test_plan_falls_back_to_the_offline_parser_with_the_reason(self):
        config.WORKER_URL = "https://dj.test"
        with _Capture([_worker_err("quota", "Quota exceeded",
                                   status="RESOURCE_EXHAUSTED", code=429)]):
            p = brain.plan("mellow evening jazz")
        self.assertIn("fallback", p["engine"])
        self.assertTrue(p["queries"], "the offline parser must still queue music")
        self.assertIn("quota", p["llm_error"])


class ProbeTests(_Iso):
    def test_probe_reports_offline_as_ok(self):
        config.WORKER_URL = ""
        config.LLM_BASE_URL = ""
        r = brain.probe()
        self.assertTrue(r["ok"])
        self.assertEqual(r["engine"], "offline")

    def test_probe_never_raises(self):
        config.WORKER_URL = "https://dj.test"
        err = _worker_err("key", "API key not valid. Please pass a valid API key.",
                          status="API_KEY_INVALID", code=401)
        with _Capture([err]):
            r = brain.probe()
        self.assertFalse(r["ok"])
        self.assertIn("key", r["detail"].lower())

    def test_probe_measures_latency(self):
        config.WORKER_URL = "https://dj.test"
        with _Capture([_FakeResp(_worker_plan(plan={"queries": ["lofi x"]}))]):
            r = brain.probe()
        self.assertTrue(r["ok"], r["detail"])
        self.assertGreaterEqual(r["ms"], 0)
        self.assertIn("1 queries", r["detail"])

    def test_probe_says_when_there_is_no_worker_at_all(self):
        """Offline is a valid configuration, so it is reported as ok - but it
        has to *name itself*, or the reader assumes a working planner."""
        config.WORKER_URL = ""
        config.LLM_BASE_URL = ""
        r = brain.probe()
        self.assertEqual(r["engine"], "offline")
        self.assertIn("no Worker URL", r["detail"])


class _FakeResp:
    def __init__(self, payload, headers=None):
        self._p = payload
        self.headers = headers or {}

    def read(self):
        return self._p.encode() if isinstance(self._p, str) else self._p

    def __enter__(self): return self
    def __exit__(self, *a): return False


def _worker_err(kind, detail, status="", code=502, notes=None):
    """A believable Worker error envelope, as an HTTPError."""
    body = json.dumps({"ok": False, "error": {"kind": kind, "detail": detail,
                                              "status": status,
                                              "notes": notes or []}}).encode()
    return urllib.error.HTTPError("https://dj.test/v1/plan", code, "err", {},
                                  io.BytesIO(body))


def _worker_plan(plan=None, model="gemini-3.5-flash", notes=None):
    """The JSON a successful /v1/plan answers with."""
    return json.dumps({"ok": True, "plan": plan if plan is not None
                       else {"queries": ["lofi one", "lofi two"]},
                       "model": model, "notes": notes or [], "source": "secret"})


class _Capture:
    """Patch `workerclient._urlopen` and remember what was asked for.

    /v1/health is answered from a canned reply rather than from the queue:
    the client asks it to decide whether to send a key, so a test that wants
    to script the *plan* call must not have its reply eaten by the probe.
    """

    HEALTH = {"ok": True, "service": "spotube-dj-worker", "key_source": "secret",
              "model": "gemini-3.5-flash", "d1": True, "clips": True,
              "token_required": False}

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.seen.append({"url": url, "timeout": timeout,
                          "headers": dict(req.headers),
                          "body": json.loads((req.data or b"{}").decode())})
        if url.rstrip("/").endswith("/v1/health"):
            return _FakeResp(json.dumps(self.HEALTH))
        r = self.replies.pop(0) if self.replies else _FakeResp(_worker_plan())
        if isinstance(r, Exception):
            raise r
        return r

    def hits(self, path: str) -> list[dict]:
        """Only the calls to one route - a test should not care that health ran."""
        return [h for h in self.seen if h["url"].rstrip("/").endswith(path)]

    def __enter__(self):
        self._ps = mock.patch.object(workerclient, "_urlopen", self)
        self._ps.start()
        return self

    def __exit__(self, *a):
        self._ps.stop()
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
