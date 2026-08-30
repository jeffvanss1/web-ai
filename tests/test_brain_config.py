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
                       config.LLM_MODEL, config.LLM_TIMEOUT, brain._SHAPE_OK,
                       brain.SUGGESTED_MODEL)
        brain.LAST_ERRORS.clear()
        brain.NOTES.clear()
        brain._SHAPE_OK = 0          # the "which payload shape worked" cache leaks
        brain.SUGGESTED_MODEL = None # between tests unless reset here
        config.LLM_BASE_URL = ""

    def tearDown(self):
        for x in self._ps:
            x.stop()
        (config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL,
         config.LLM_TIMEOUT, brain._SHAPE_OK, brain.SUGGESTED_MODEL) = self._saved
        self._tmp.cleanup()


class EngineSelectionTests(_Iso):
    def test_key_alone_selects_gemini_even_with_blank_base(self):
        """The original silent failure: key set, base cleared -> 'offline'."""
        config.LLM_API_KEY = "k"
        config.LLM_BASE_URL = ""
        self.assertEqual(brain.configured_engine(), "gemini")

    def test_any_non_gemini_base_is_local_llm(self):
        for base in ("http://localhost:11434", "http://192.168.1.50:11434",
                     "http://host.docker.internal:11434", "http://ollama.local:11434"):
            config.LLM_BASE_URL = base
            config.LLM_API_KEY = ""
            self.assertEqual(brain.configured_engine(), "local-llm", base)

    def test_gemini_base_with_key_stays_gemini(self):
        config.LLM_BASE_URL = config.GEMINI_DEFAULT_URL
        config.LLM_API_KEY = "k"
        self.assertEqual(brain.configured_engine(), "gemini")

    def test_truly_nothing_configured_is_offline(self):
        config.LLM_API_KEY = ""
        config.LLM_BASE_URL = ""
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

    def test_every_gemini_attempt_uses_the_configured_budget(self):
        """The bug was a literal `timeout=25` at the urlopen call site."""
        config.LLM_TIMEOUT = 41
        config.LLM_API_KEY = "k"
        seen = []
        def fake(req, timeout=None):
            seen.append(timeout)
            raise urllib.error.URLError("boom")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            brain._gemini("x")
        self.assertTrue(seen, "expected the request to be attempted")
        for t_ in seen:
            self.assertEqual(t_, 41)

    def test_one_retry_then_give_up(self):
        config.LLM_TIMEOUT = 7
        config.LLM_API_KEY = "k"
        calls = []
        def fake(req, timeout=None):
            calls.append(1)
            raise urllib.error.URLError("timed out")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            out = brain._gemini("x")
        self.assertIsNone(out)
        self.assertEqual(len(calls), brain.RETRIES + 1)

    def test_bad_key_does_not_waste_a_retry(self):
        import io
        config.LLM_API_KEY = "bad"
        body = json.dumps({"error": {"message": "API key not valid. Please pass a valid API key.",
                                     "status": "API_KEY_INVALID"}}).encode()
        def fake(req, timeout=None):
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            brain._gemini("x")
        self.assertIn("key not valid", brain.LAST_ERRORS.get("gemini", "").lower())


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

    def test_a_key_error_costs_one_http_call(self):
        config.LLM_API_KEY = "k"
        config.LLM_MODEL = "gemini-3.5-flash"
        err = self._err(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.")
        calls = []

        def fake(req, timeout=None):
            calls.append(req.full_url)
            raise err
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertIsNone(brain._gemini("p"))
        self.assertEqual(len(calls), 1, f"a dead key must not iterate the ladder: {calls}")
        self.assertIn("key not valid", brain.LAST_ERRORS["gemini"])

    def test_a_retired_model_still_costs_only_the_ladder(self):
        config.LLM_API_KEY = "k"
        config.LLM_MODEL = "gemini-2.0-flash"
        err = self._err(404, "NOT_FOUND",
                        "This model models/gemini-2.0-flash is no longer available. "
                        "Please update your code to use models/gemini-3.6-flash for "
                        "the latest features and improvements.")
        calls = []

        class R:
            def __init__(self, d): self.d = d
            def read(self): return json.dumps(self.d).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=None):
            calls.append(req.full_url.split("/models/")[1])
            if "2.0-flash" in calls[-1]:
                raise err
            return R({"candidates": [{"content": {"parts": [{"text": '{"queries":["a b c"]}'}]},
                                      "finishReason": "STOP"}]})
        with mock.patch("urllib.request.urlopen", side_effect=fake), \
             mock.patch.object(config, "save_llm_config") as saved:
            out = brain._gemini("p")
        self.assertEqual(out["queries"], ["a b c"])
        self.assertEqual(calls[0], "gemini-2.0-flash:generateContent")
        self.assertIn("gemini-3.6-flash:generateContent", calls)
        saved.assert_called_once_with(LLM_MODEL="gemini-3.6-flash")


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
        """Whole real path, only the socket is faked: refused connection must
        reach the caller as a readable reason, not a silent 'offline'."""
        config.LLM_API_KEY = "k"
        config.LLM_BASE_URL = ""
        def boom(req, timeout=None):
            raise urllib.error.URLError("[Errno 111] Connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=boom):
            p = brain.plan("lofi")
        self.assertIn("gemini (fallback)", p["engine"])
        self.assertTrue(p.get("llm_error"), "plan() must expose why it fell back")
        self.assertIn("connection refused", p["llm_error"])
        # and it must NOT blame ollama for a Google endpoint
        self.assertNotIn("ollama serve", p["llm_error"])

    def test_plan_never_raises_on_a_weird_api_error(self):
        """An AttributeError from a malformed body must not kill the whole app."""
        config.LLM_API_KEY = "k"
        with mock.patch.object(brain, "_gemini", side_effect=AttributeError("'list' has no get")), \
             mock.patch.object(brain, "_openai_compat", side_effect=AttributeError("nope")):
            p = brain.plan("lofi")
        self.assertIn("fallback", p["engine"])
        self.assertTrue(p["queries"], "music must still be queued")

    def test_second_engine_saves_the_plan(self):
        """Gemini fails but a local model answers -> not reported as a failure."""
        config.LLM_API_KEY = "k"
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

    def test_no_key_means_no_error_noise(self):
        config.LLM_API_KEY = ""
        config.LLM_BASE_URL = ""
        p = brain.plan("dark techno")
        self.assertEqual(p["engine"], "offline")
        self.assertFalse(p.get("llm_error"))


class ResponseShapeTests(_Iso):
    """Gemini puts JSON in odd places; old code only read parts[0]['text']."""

    def test_joins_multiple_parts(self):
        data = {"candidates": [{"content": {"parts": [
            {"text": '{"queries":'}, {"text": '["lofi beats", "sleep low"]}'}]}}]}
        text, finish = brain._gemini_text(data)
        self.assertIn("lofi beats", json.loads(text)["queries"])

    def test_reports_truncation_instead_of_a_silent_none(self):
        data = {"candidates": [{"finishReason": "MAX_TOKENS",
                                "content": {"parts": [{"text": '{"queries": ["a"'}]}}]}
        text, finish = brain._gemini_text(data)
        self.assertEqual(finish, "MAX_TOKENS")

    def test_blocked_response_is_named(self):
        data = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        text, finish = brain._gemini_text(data)
        self.assertEqual(finish, "blocked:SAFETY")

    def test_url_has_no_key_and_auth_uses_the_header(self):
        """Docs use x-goog-api-key; a key in the URL leaks into logs/history."""
        config.LLM_BASE_URL = ""
        config.LLM_API_KEY = "kk"
        config.LLM_MODEL = "models/gemini-3.6-flash"
        url = brain._gemini_url()
        self.assertIn("/v1beta/models/gemini-3.6-flash:generateContent", url)
        self.assertNotIn("models/models", url)
        self.assertNotIn("kk", url)
        self.assertEqual(brain._gemini_headers(), {"x-goog-api-key": "kk"})

    def test_default_model_is_not_the_retired_2_0(self):
        self.assertNotIn("2.0", config.GEMINI_DEFAULT_MODEL)
        self.assertIn(config.GEMINI_DEFAULT_MODEL, brain._model_candidates())

    def test_payload_requests_json_the_documented_way(self):
        config.LLM_API_KEY = "k"
        captured = []
        class Resp:
            def read(self):
                return json.dumps({"candidates": []}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def fake(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            return Resp()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            brain._gemini("x")
        gc = captured[0]["generationConfig"]
        fmt = gc["responseFormat"]["text"]
        self.assertEqual(fmt["mimeType"], "application/json")
        self.assertEqual(fmt["schema"]["type"], "object")          # not "OBJECT"
        self.assertIn("queries", fmt["schema"]["properties"])
        self.assertGreaterEqual(gc["maxOutputTokens"], 2048)

    def test_shapes_include_the_legacy_spelling_as_fallback(self):
        names = [n for n, _ in brain._shapes("x")]
        self.assertEqual(names[0], "responseFormat")
        self.assertIn("responseMimeType", names)
        self.assertEqual(names[-1], "plain")                       # always accepted

    def test_json_split_mid_string_across_parts_is_glued(self):
        """Gemini can split one JSON answer across several parts."""
        config.LLM_API_KEY = "k"
        data = {"candidates": [{"content": {"parts": [
            {"text": '{"queries": ["neo soul velvet", "70s philly'},
            {"text": ' strings"], "avoid": ["remix"], "why": "gemini"}'}]},
            "finishReason": "STOP"}]}
        text, _finish = brain._gemini_text(data)
        self.assertIsNone(brain._extract_json(text, lenient=False))   # newline-joined is broken
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(data))):
            out = brain._gemini("x")
        self.assertEqual(out["queries"], ["neo soul velvet", "70s philly strings"])
        self.assertEqual(out["why"], "gemini")

    def test_queries_collapse_to_one_line(self):
        config.LLM_API_KEY = "k"
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
    """A retired model id used to mean 'offline forever'. Now it self-heals."""

    RETIRED = ("This model models/gemini-2.0-flash is no longer available. Please "
               "update your code to use models/gemini-3.6-flash for the latest features.")

    def test_404_switches_to_the_model_the_api_named(self):
        config.LLM_API_KEY = "k"
        config.LLM_BASE_URL = ""
        config.LLM_MODEL = "gemini-2.0-flash"
        urls, replies = [], [
            _http_err(404, self.RETIRED),
            {"candidates": [{"content": {"parts": [
                {"text": '{"queries":["lofi one","lofi two"]}'}]}, "finishReason": "STOP"}]},
        ]

        def fake(req, timeout=None):
            urls.append(req.full_url)
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return _FakeResp(json.dumps(r))
        with mock.patch("urllib.request.urlopen", side_effect=fake), \
             mock.patch.object(config, "save_llm_config", lambda **kw: saved.update(kw)):
            saved = {}
            out = brain._gemini("give me queries")
        self.assertEqual(out["queries"], ["lofi one", "lofi two"])
        self.assertIn("gemini-3.6-flash", urls[1])
        self.assertEqual(config.LLM_MODEL, "gemini-3.6-flash")
        self.assertEqual(saved.get("LLM_MODEL"), "gemini-3.6-flash")   # remembered
        self.assertTrue(any("switched the model" in n for n in brain.pop_notes()))
        self.assertEqual(brain.SUGGESTED_MODEL, "gemini-3.6-flash")

    def test_payload_rejection_walks_shapes_on_the_same_model(self):
        config.LLM_API_KEY = "k"
        config.LLM_MODEL = "gemini-3.5-flash"
        payloads, replies = [], [
            _http_err(400, 'Invalid JSON payload. Unknown name "responseFormat" at '
                           '"generationConfig.responseFormat"'),
            {"candidates": [{"content": {"parts": [
                {"text": '{"queries":["a b c"]}'}]}, "finishReason": "STOP"}]},
        ]

        def fake(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return _FakeResp(json.dumps(r))
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            out = brain._gemini("x")
        self.assertEqual(out["queries"], ["a b c"])
        self.assertIn("responseFormat", payloads[0]["generationConfig"])
        self.assertIn("responseMimeType", payloads[1]["generationConfig"])  # legacy spelling

    def test_a_bad_key_costs_exactly_one_request(self):
        """No model walking, no shape walking, no retry: auth is the problem."""
        config.LLM_API_KEY = "k"
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            raise _http_err(400, "API key not valid. Please pass a valid API key.")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertIsNone(brain._gemini("x"))
        self.assertEqual(len(calls), 1)

    def test_timeouts_are_bounded_and_explained(self):
        config.LLM_API_KEY = "k"
        config.LLM_TIMEOUT = 30
        calls = []

        def fake(req, timeout=None):
            calls.append(timeout)
            raise urllib.error.URLError("The read operation timed out")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertIsNone(brain._gemini("x"))
        self.assertLessEqual(len(calls), brain.MAX_CALLS)
        self.assertTrue(all(c == 30 for c in calls), calls)
        self.assertIn("timed out", brain.LAST_ERRORS["gemini"])

    def test_every_request_carries_the_timeout(self):
        """The old bug was a literal 25 with no way to change it."""
        config.LLM_API_KEY = "k"
        config.LLM_TIMEOUT = 12
        seen = []

        def fake(req, timeout=None):
            seen.append(timeout)
            raise _http_err(404, "no such model")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            brain._gemini("x")
        self.assertTrue(seen and all(v == 12 for v in seen), seen)


class ProbeTests(_Iso):
    def test_probe_reports_offline_as_ok(self):
        config.LLM_API_KEY = ""
        config.LLM_BASE_URL = ""
        r = brain.probe()
        self.assertTrue(r["ok"])
        self.assertEqual(r["engine"], "offline")

    def test_probe_never_raises(self):
        import io
        config.LLM_API_KEY = "k"
        body = json.dumps({"error": {"message": "API key not valid. Please pass a valid API key.",
                                     "status": "API_KEY_INVALID"}}).encode()
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(body))):
            r = brain.probe()
        self.assertFalse(r["ok"])
        self.assertIn("key", r["detail"].lower())

    def test_probe_measures_latency(self):
        config.LLM_API_KEY = "k"
        good = {"candidates": [{"content": {"parts": [{"text": '{"queries":["lofi x"]}'}]}}]}
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp(json.dumps(good))):
            r = brain.probe()
        self.assertTrue(r["ok"], r["detail"])
        self.assertGreaterEqual(r["ms"], 0)
        self.assertIn("1 queries", r["detail"])


class _FakeResp:
    def __init__(self, payload): self._p = payload
    def read(self): return self._p.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
