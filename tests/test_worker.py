"""
The Worker layer, tested as a client: what this app sends, what it does with
the answer, and what it does when there is no answer.

The routes themselves are pinned in `worker/test/smoke.mjs` (`node --test test/`),
next to the code that serves them. This file is the other half - the app must
ask for the right thing, leak no key, and stay playable when the Worker is
missing, wrong or slow.
"""
from __future__ import annotations

import io
import json
import threading
import time
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap)

import cloudstate
import config
import taste
import web as web_mod
import webapp
import workerclient


class _Resp:
    def __init__(self, payload, headers=None):
        self._p = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = headers or {}

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Wire:
    """Patch `workerclient._urlopen` and record what went out."""

    HEALTH = {"ok": True, "key_source": "secret", "model": "gemini-3.5-flash",
              "d1": True, "clips": True, "token_required": False}

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):
        self.calls.append({"url": req.full_url, "timeout": timeout,
                           "headers": {k.title(): v for k, v in req.headers.items()},
                           "body": json.loads((req.data or b"{}").decode())})
        if req.full_url.rstrip("/").endswith("/v1/health"):
            return _Resp(self.HEALTH)
        r = self.replies.pop(0) if self.replies else _Resp({"ok": True, "plan": {"queries": ["a b"]}})
        if isinstance(r, Exception):
            raise r
        return r

    def __enter__(self):
        self._ps = mock.patch.object(workerclient, "_urlopen", self)
        self._ps.start()
        return self

    def __exit__(self, *a):
        self._ps.stop()
        return False


def _err(kind, detail, code=502, status=""):
    body = json.dumps({"ok": False, "error": {"kind": kind, "detail": detail,
                                              "status": status, "notes": []}}).encode()
    return urllib.error.HTTPError("https://dj.test/v1/x", code, "err", {}, io.BytesIO(body))


class _Iso(unittest.TestCase):
    def setUp(self):
        self._ps = [mock.patch.object(config, "WORKER_URL", "https://dj.test"),
                    mock.patch.object(config, "WORKER_TOKEN", ""),
                    mock.patch.object(config, "WORKER_PROFILE", "default"),
                    mock.patch.object(config, "WORKER_SYNC", "on"),
                    mock.patch.object(config, "LLM_API_KEY", "")]
        for p in self._ps:
            p.start()
        workerclient._health["data"] = None
        workerclient._health["at"] = 0.0
        workerclient.LAST_OK = None
        workerclient.LAST_ERROR = None

    def tearDown(self):
        for p in self._ps:
            p.stop()


# --------------------------------------------------------------- configuration

class ConfigTests(_Iso):
    def test_a_trailing_slash_does_not_become_a_double_slash(self):
        config.WORKER_URL = "https://dj.test/"
        self.assertEqual(workerclient.base_url(), "https://dj.test")
        with _Wire() as wire:
            workerclient.plan("x")
        self.assertEqual(wire.calls[-1]["url"], "https://dj.test/v1/plan")

    def test_no_url_means_offline_and_no_dialling(self):
        config.WORKER_URL = ""
        self.assertFalse(workerclient.configured())
        with _Wire() as wire:
            with self.assertRaises(workerclient.WorkerError) as cm:
                workerclient.plan("x")
        self.assertEqual(cm.exception.kind, "offline")
        self.assertEqual(wire.calls, [], "an unconfigured app must not dial out")
        self.assertIn("no Worker URL", cm.exception.detail)

    def test_the_env_var_wins_over_the_saved_file(self):
        with mock.patch.dict("os.environ", {"SPOTUBE_DJ_WORKER_URL": "https://env.test"}):
            self.assertEqual(workerclient.base_url(), "https://env.test")

    def test_sync_can_be_switched_off_with_the_url_still_set(self):
        self.assertTrue(cloudstate.enabled())
        config.WORKER_SYNC = "off"
        self.assertFalse(cloudstate.enabled())
        self.assertTrue(workerclient.configured(), "the planner still works")


# -------------------------------------------------------------------- security

class KeyHandlingTests(_Iso):
    def test_a_token_goes_in_a_bearer_header(self):
        config.WORKER_TOKEN = "shh"
        with _Wire() as wire:
            workerclient.plan("x")
        self.assertEqual(wire.calls[-1]["headers"].get("Authorization"), "Bearer shh")

    def test_the_key_is_not_sent_to_a_worker_that_has_its_own(self):
        """The point of the Worker: a machine holding a URL holds no secret."""
        config.LLM_API_KEY = "AIzaSECRET"
        with _Wire() as wire:
            workerclient.plan("x")
        sent = json.dumps(wire.calls[-1]["headers"])
        self.assertNotIn("AIzaSECRET", sent)
        self.assertNotIn("AIzaSECRET", wire.calls[-1]["url"])

    def test_the_key_is_sent_only_when_the_worker_asks_for_it(self):
        config.LLM_API_KEY = "AIzaSECRET"
        with mock.patch.object(workerclient, "health",
                               return_value={"ok": True, "key_source": "client (send x-gemini-key)"}):
            with _Wire() as wire:
                workerclient.plan("x")
        self.assertEqual(wire.calls[-1]["headers"].get("X-Gemini-Key"), "AIzaSECRET")

    def test_asking_whether_to_send_a_key_does_not_recurse(self):
        """health() -> _headers() -> wants_client_key() -> health() ..."""
        config.LLM_API_KEY = "AIzaSECRET"
        with _Wire() as wire:
            self.assertTrue(workerclient.health(force=True)["ok"])
        health_calls = [c for c in wire.calls if c["url"].endswith("/v1/health")]
        self.assertEqual(len(health_calls), 1, f"the probe looped: {wire.calls}")


# ----------------------------------------------------------------------- routes

class EdgeTests(_Iso):
    """Cloudflare answers some failures before the Worker ever sees them."""

    def test_every_request_looks_like_a_client_not_a_bot(self):
        """HTTP 403 "Error 1010: blocked based on your browser's signature".

        urllib's default User-Agent is "Python-urllib/3.x", which the browser
        integrity check refuses - the page loads in a browser and 403s from
        Python, which reads exactly like a broken deployment.
        """
        with _Wire([_Resp({"ok": True, "plan": {"queries": ["a b"]}})]) as wire:
            workerclient.plan("x")
        h = wire.calls[-1]["headers"]
        self.assertNotIn("Python-urllib", h.get("User-Agent", ""),
                         "the default UA is what gets this client blocked")
        self.assertIn("spotube-dj", h.get("User-Agent", ""))
        self.assertIn("application/json", h.get("Accept", ""))
        self.assertTrue(h.get("Accept-Language"))

    def test_a_cloudflare_1010_is_explained_not_dumped(self):
        page = {"type": "https://developers.cloudflare.com/support/troubleshooting/"
                        "http-status-codes/cloudflare-1xxx-errors/error-1010/",
                "title": "Error 1010: Access denied", "status": 403,
                "detail": "The site owner has blocked access based on your "
                          "browser's signature."}
        body = json.dumps(page).encode()
        err = urllib.error.HTTPError("https://dj.test/v1/plan", 403, "Forbidden",
                                     {}, io.BytesIO(body))
        with _Wire([err]):
            with self.assertRaises(workerclient.WorkerError) as cm:
                workerclient.plan("x")
        self.assertEqual(cm.exception.kind, "edge")
        self.assertIn("browser integrity check", cm.exception.detail)
        self.assertIn("Bot Fight Mode", cm.exception.detail,
                      "the fix has to be in the sentence, not in a doc")
        self.assertNotIn("Error 1010: Access denied', 'status'", cm.exception.detail)

    def test_the_edge_wording_survives_brain(self):
        """worker_error_text rewrites unknown kinds; this one must not be."""
        import brain
        err = workerclient.WorkerError("edge", "Cloudflare blocked this client "
                                       "before it reached the Worker.")
        self.assertIn("Cloudflare blocked", brain.worker_error_text(err))
        self.assertNotIn("the request failed", brain.worker_error_text(err))

    def test_an_unknown_cloudflare_code_still_says_cloudflare(self):
        page = {"type": "https://developers.cloudflare.com/x", "status": 521,
                "title": "Error 521: Web server is down", "detail": "nope"}
        err = urllib.error.HTTPError("https://dj.test/v1/plan", 521, "x", {},
                                     io.BytesIO(json.dumps(page).encode()))
        with _Wire([err]):
            with self.assertRaises(workerclient.WorkerError) as cm:
                workerclient.plan("x")
        self.assertEqual(cm.exception.kind, "edge")
        self.assertIn("Cloudflare", cm.exception.detail)

    def test_a_plain_json_error_is_not_mistaken_for_the_edge(self):
        """Our own 404s have no `type`/`title`, and must not be relabelled."""
        with _Wire([_err("model", "no such model", code=404)]):
            with self.assertRaises(workerclient.WorkerError) as cm:
                workerclient.plan("x")
        self.assertEqual(cm.exception.kind, "model")


class RouteTests(_Iso):
    def test_plan_sends_the_prompt_and_returns_the_plan(self):
        reply = {"ok": True, "plan": {"queries": ["slowdive", "mbv"], "why": "you asked"},
                 "model": "gemini-3.6-flash", "notes": ["a note"]}
        with _Wire([_Resp(reply)]) as wire:
            got = workerclient.plan("chill lofi", system="be a DJ")
        body = wire.calls[-1]["body"]
        self.assertEqual(body["prompt"], "chill lofi")
        self.assertEqual(body["system"], "be a DJ")
        self.assertEqual(got["plan"]["queries"], ["slowdive", "mbv"])
        self.assertEqual(got["model"], "gemini-3.6-flash")
        self.assertEqual(got["notes"], ["a note"])

    def test_text_returns_the_line(self):
        with _Wire([_Resp({"ok": True, "text": "Alright, here we go."})]) as wire:
            self.assertEqual(workerclient.text("write a DJ line"), "Alright, here we go.")
        self.assertEqual(wire.calls[-1]["body"]["maxChars"], 600)

    def test_speech_returns_the_wav_bytes_untouched(self):
        wav = b"RIFF" + b"\x00" * 100
        with _Wire([_Resp(wav)]) as wire:
            got = workerclient.speech("hello", voice="Despina", model="gemini-3.1-flash-tts-preview")
        self.assertEqual(got, wav)
        self.assertEqual(wire.calls[-1]["url"], "https://dj.test/v1/speech")
        self.assertEqual(wire.calls[-1]["body"]["voice"], "Despina")

    def test_state_round_trip_uses_the_profile_name(self):
        with _Wire([_Resp({"ok": True, "state": {"volume": 30}, "updated_at": 111})]) as wire:
            self.assertEqual(workerclient.state_get("laptop"), {"volume": 30})
        self.assertIn("profile=laptop", wire.calls[-1]["url"])

    def test_state_put_sends_the_whole_state(self):
        with _Wire([_Resp({"ok": True, "updated_at": 222})]) as wire:
            self.assertEqual(workerclient.state_put({"volume": 30}), 222)
        self.assertEqual(wire.calls[-1]["body"]["state"], {"volume": 30})

    def test_events_post_and_get(self):
        with _Wire([_Resp({"ok": True, "n": 2, "last_id": 7})]) as wire:
            self.assertEqual(workerclient.events_post([{"ts": 1, "kind": "like"},
                                                       {"ts": 2, "kind": "skip"}]), 7)
        self.assertEqual(wire.calls[-1]["body"]["events"][0]["kind"], "like")
        with _Wire([_Resp({"ok": True, "events": [{"id": 8, "ts": 3, "kind": "mix",
                                                   "payload": {}}]})]) as wire:
            rows = workerclient.events_get(since=7)
        self.assertEqual(rows[0]["kind"], "mix")
        self.assertIn("since=7", wire.calls[-1]["url"])

    def test_an_empty_event_list_costs_no_request(self):
        with _Wire() as wire:
            self.assertEqual(workerclient.events_post([]), 0)
        self.assertEqual(wire.calls, [])


# --------------------------------------------------------------------- failures

class FailureTests(_Iso):
    def test_a_json_error_keeps_its_kind(self):
        for kind, code in (("key", 401), ("quota", 429), ("model", 404),
                           ("auth", 401), ("no_d1", 503)):
            with _Wire([_err(kind, "boom", code=code)]) as _:
                with self.assertRaises(workerclient.WorkerError) as cm:
                    workerclient.plan("x")
            self.assertEqual(cm.exception.kind, kind, f"{kind} arrived as {cm.exception.kind}")

    def test_a_non_json_failure_is_still_a_kind(self):
        err = urllib.error.HTTPError("https://dj.test/v1/plan", 502, "bad", {},
                                     io.BytesIO(b"<html>bad gateway</html>"))
        with _Wire([err]):
            with self.assertRaises(workerclient.WorkerError) as cm:
                workerclient.plan("x")
        self.assertEqual(cm.exception.kind, "other")
        self.assertIn("bad gateway", cm.exception.detail)

    def test_a_refused_connection_names_the_deployment(self):
        with _Wire([urllib.error.URLError("[Errno 111] Connection refused")]):
            with self.assertRaises(workerclient.WorkerError) as cm:
                workerclient.plan("x")
        self.assertEqual(cm.exception.kind, "network")
        self.assertIn("wrangler dev", cm.exception.detail)

    def test_a_timeout_says_how_long_it_waited(self):
        config.WORKER_URL = "https://dj.test"
        with _Wire([urllib.error.URLError("The read operation timed out")]):
            with self.assertRaises(workerclient.WorkerError) as cm:
                workerclient.plan("x")
        self.assertIn("timed out", cm.exception.detail)

    def test_the_last_error_is_remembered_for_the_pill(self):
        with _Wire([_err("quota", "Quota exceeded", code=429)]):
            with self.assertRaises(workerclient.WorkerError):
                workerclient.plan("x")
        self.assertEqual(workerclient.LAST_ERROR["kind"], "quota")
        self.assertIn("quota", workerclient.status_line())

    def test_probe_says_what_it_found(self):
        with _Wire([_Resp({"ok": True, "plan": {"queries": ["jazz a", "jazz b"]},
                           "model": "gemini-3.5-flash"})]):
            r = workerclient.probe()
        self.assertTrue(r["ok"], r["detail"])
        self.assertIn("D1 bound", r["detail"])
        self.assertIn("2 queries", r["detail"])

    def test_probe_with_no_url_is_not_ok(self):
        config.WORKER_URL = ""
        r = workerclient.probe()
        self.assertFalse(r["ok"])
        self.assertIn("no Worker URL", r["detail"])


# ------------------------------------------------------------------- cloudstate

class CloudStateTests(_Iso):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._ps2 = [mock.patch.object(config, "STATE_FILE", d / "state.json"),
                     mock.patch.object(config, "APP_DIR", d),
                     mock.patch.object(config, "LLM_CONFIG_FILE", d / "config.json")]
        for p in self._ps2:
            p.start()
        cloudstate._events.clear()
        cloudstate._last_push = 0.0
        cloudstate._last_signature = ""
        cloudstate._last_event_id = 0
        cloudstate._status.update({"ok": None, "detail": "", "pushed_at": 0,
                                   "events": 0, "last_event_id": 0})

    def tearDown(self):
        for p in self._ps2:
            p.stop()
        self._tmp.cleanup()
        super().tearDown()

    def test_push_sends_the_profile_and_records_when(self):
        config.save_state({"liked": [{"title": "Alison", "artist": "Slowdive"}], "artists": {}})
        with _Wire([_Resp({"ok": True, "updated_at": 999})]):
            self.assertEqual(cloudstate.push_now(), 999)
        self.assertTrue(cloudstate.status()["pushed_at"] > 0)
        self.assertTrue(cloudstate.status()["ok"])

    def test_a_failed_push_is_a_status_not_a_crash(self):
        with _Wire([_err("auth", "bad token", code=401)]):
            self.assertEqual(cloudstate.push_now(), 0)
        self.assertIn("auth", cloudstate.status()["detail"])

    def test_adopt_refuses_to_overwrite_a_profile_that_has_learned(self):
        config.save_state({"liked": [{"title": "Alison", "artist": "Slowdive"}], "artists": {}})
        remote = {"liked": [{"title": "Remote only", "artist": "Nobody"}], "artists": {}}
        with _Wire([_Resp({"ok": True, "state": remote, "updated_at": 1})]):
            note = cloudstate.adopt()
        self.assertIn("already has a taste profile", note)
        self.assertNotIn("Remote only", json.dumps(config.load_state()))

    def test_adopt_takes_the_cloud_copy_on_a_fresh_install(self):
        remote = {"liked": [{"title": "Remote only", "artist": "Nobody"}], "artists": {}}
        with _Wire([_Resp({"ok": True, "state": remote, "updated_at": 1})]):
            note = cloudstate.adopt()
        self.assertIn("took the cloud profile", note)
        self.assertEqual(config.load_state()["liked"][0]["title"], "Remote only")

    def test_adopt_force_takes_it_and_leaves_a_backup(self):
        config.save_state({"liked": [{"title": "Local", "artist": "Here"}], "artists": {}})
        remote = {"liked": [{"title": "Remote", "artist": "There"}], "artists": {}}
        with _Wire([_Resp({"ok": True, "state": remote, "updated_at": 1})]):
            cloudstate.adopt(force=True)
        self.assertEqual(config.load_state()["liked"][0]["title"], "Remote")
        self.assertTrue(taste.has_backup(), "an overwrite has to be undoable")

    def test_events_are_queued_and_flushed(self):
        cloudstate.record("like", {"title": "Alison", "artist": "Slowdive"})
        self.assertEqual(cloudstate.status()["pending"], 1)
        with _Wire([_Resp({"ok": True, "n": 1, "last_id": 5})]) as wire:
            self.assertEqual(cloudstate.flush(), 1)
        self.assertEqual(cloudstate.status()["pending"], 0)
        self.assertEqual(wire.calls[-1]["body"]["events"][0]["kind"], "like")

    def test_replay_applies_a_remote_like_to_the_local_profile(self):
        with _Wire([_Resp({"ok": True, "events": [
            {"id": 3, "ts": 1, "kind": "like",
             "payload": {"title": "Alison", "artist": "Slowdive", "id": "v1"}}]})]):
            n = cloudstate.replay()
        self.assertEqual(n, 1)
        self.assertTrue(taste.is_liked({"title": "Alison", "artist": "Slowdive"}))
        self.assertEqual(cloudstate._last_event_id, 3)

    def test_replay_ignores_events_it_does_not_know(self):
        with _Wire([_Resp({"ok": True, "events": [
            {"id": 4, "ts": 1, "kind": "exploded", "payload": {}}]})]):
            self.assertEqual(cloudstate.replay(), 0)

    def test_status_line_says_what_is_wrong_in_english(self):
        self.assertIn("not saved yet", cloudstate.status_line())
        config.WORKER_URL = ""
        self.assertIn("stays on this machine", cloudstate.status_line())
        config.WORKER_URL = "https://dj.test"
        config.WORKER_SYNC = "off"
        self.assertIn("WORKER_SYNC=off", cloudstate.status_line())


# --------------------------------------------------------- the static front end

class StaticFrontEndTests(unittest.TestCase):
    def test_the_static_dir_holds_the_three_files(self):
        for name in ("index.html", "app.css", "app.js", "icons.json"):
            self.assertTrue((webapp.STATIC_DIR / name).is_file(), name)

    def test_page_is_one_self_contained_document(self):
        html = webapp.page()
        self.assertNotIn("<link", html, "the one-document form must link nothing")
        self.assertEqual(html.count("<script>"), 1)
        self.assertIn("<style>", html)

    def test_the_site_is_three_linked_files(self):
        files = webapp.static_files()
        self.assertEqual(sorted(files), ["app.css", "app.js", "favicon.svg", "index.html"])
        index = files["index.html"].decode()
        self.assertIn('href="app.css"', index)
        self.assertIn('src="app.js"', index)

    def test_neither_form_ships_an_unreplaced_token(self):
        for text in (webapp.page(), webapp.shell(), webapp.app_css(),
                     webapp.static_files()["index.html"].decode()):
            self.assertNotRegex(text, r"@@[A-Za-z_]+@@",
                                "a token reached the browser unsubstituted")

    def test_both_forms_come_from_the_same_assets(self):
        """The one-document page and the deployable site cannot drift."""
        page, files = webapp.page(), webapp.static_files()
        self.assertIn(files["app.css"].decode(), page)
        self.assertIn(files["app.js"].decode(), page)

    def test_the_palette_is_substituted_not_hard_coded(self):
        import viewmodel as vm
        self.assertIn(vm.ACCENT, webapp.app_css())
        self.assertNotIn("@@ACCENT@@", webapp.app_css())

    def test_asset_serves_one_file_at_a_time_with_a_type(self):
        body, ctype = webapp.asset("/app.css")
        self.assertIn("text/css", ctype)
        self.assertTrue(body.startswith(b":root{"))
        body, ctype = webapp.asset("/app.js")
        self.assertIn("javascript", ctype)
        self.assertIsNone(webapp.asset("/app.py"))

    def test_build_static_writes_the_site(self):
        with tempfile.TemporaryDirectory() as d:
            written = webapp.build_static(d)
            self.assertIn("index.html", written)
            for name in written:
                self.assertTrue((Path(d) / name).is_file(), name)

    def test_an_edit_on_disk_is_picked_up(self):
        """The point of assets living in files: edit, reload, done."""
        path = webapp.STATIC_DIR / "app.css"
        original = path.read_text()
        try:
            path.write_text(original + "\n/* edited */\n")
            webapp.forget_assets()
            self.assertIn("edited", webapp.app_css())
        finally:
            path.write_text(original)
            webapp.forget_assets()

    def test_the_old_names_still_read_the_assets(self):
        self.assertIn('class="app"', webapp.BODY)
        self.assertIn(":root{", webapp.CSS)
        self.assertIn("function draw", webapp.JS)
        self.assertTrue(len(webapp.ICONS) > 20)


# ------------------------------------------------------ the server around them

class WebRouteTests(unittest.TestCase):
    """One real socket on 127.0.0.1:0, so routing and headers are actually used."""
    port: int
    httpd: object
    ctx: web_mod.Context

    @classmethod
    def setUpClass(cls):
        from tests.test_web import fake_dj
        cls.ctx = web_mod.Context(fake_dj())
        cls.ctx.start()
        cls.httpd = web_mod.make_server(cls.ctx, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.ctx.stop()

    def get(self, path):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{self.port}{path}"), timeout=5)
            return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_the_assets_are_served_from_the_same_origin(self):
        for path, needle, ctype in (("/app.css", b":root{", "text/css"),
                                    ("/app.js", b"function draw", "javascript")):
            code, headers, body = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertIn(ctype, headers["Content-Type"])
            self.assertIn(needle, body)
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_an_unknown_asset_is_not_a_page(self):
        code, _h, _b = self.get("/app.py")
        self.assertEqual(code, 404)

    def test_the_routes_map_names_the_assets(self):
        for path in ("/app.css", "/app.js", "/voice/<clip>.wav"):
            self.assertIn(path, web_mod.ROUTES)

    def test_the_voice_route_refuses_a_name_that_is_not_a_clip(self):
        for bad in ("../../state.json", "nope.wav", "a" * 200):
            code, _h, _b = self.get("/voice/" + bad)
            self.assertEqual(code, 404, bad)

    def test_a_published_clip_is_reachable(self):
        ctx = self.ctx
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ctx, "_voice_dir", Path(d)):
                q = ctx.subscribe()                   # a tab is listening
                try:
                    src = Path(d) / "src.wav"
                    src.write_bytes(b"RIFF" + b"\x00" * 20)
                    self.assertTrue(ctx.publish_voice(str(src), "hello"))
                    clip = ctx.voice_clip()
                    code, headers, body = self.get(clip["url"])
                    self.assertEqual(code, 200)
                    self.assertEqual(headers["Content-Type"], "audio/wav")
                    self.assertEqual(body[:4], b"RIFF")
                    self.assertEqual(clip["text"], "hello")
                finally:
                    ctx.unsubscribe(q)

    def test_only_the_last_few_clips_are_kept(self):
        ctx = self.ctx
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ctx, "_voice_dir", Path(d)):
                q = ctx.subscribe()
                try:
                    for i in range(9):
                        src = Path(d) / f"src{i}.wav"
                        src.write_bytes(b"RIFF" + b"\x00" * 20)
                        (Path(d) / f"src{i}.wav").touch()
                        self.assertTrue(ctx.publish_voice(str(src), f"line {i}"))
                        time.sleep(0.01)             # distinct mtimes, ordered
                    self.assertEqual(ctx.voice_clip()["text"], "line 8")
                    left = sorted(p.name for p in Path(d).glob("*.wav")
                                  if p.name.startswith("1"))
                    self.assertLessEqual(len(left), ctx._voice_keep)
                finally:
                    ctx.unsubscribe(q)

    def test_with_no_tab_the_sink_declines_so_mpv_takes_over(self):
        ctx = self.ctx
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ctx, "_voice_dir", Path(d)):
                for q in list(ctx._subs):
                    ctx.unsubscribe(q)
                src = Path(d) / "src.wav"
                src.write_bytes(b"RIFF" + b"\x00" * 20)
                self.assertFalse(ctx.publish_voice(str(src), "line"))

    def test_the_worker_block_rides_in_the_state(self):
        st = web_mod.build_state(self.ctx)
        self.assertIn("worker", st)
        self.assertIn("configured", st["worker"])
        self.assertIn("profile", st["worker"])
        self.assertIn("voice_clip", st)
        self.assertIn("worker_url", st["settings"])
        self.assertNotIn("worker_token", st["settings"], "the token must not reach the page")

    def test_the_three_worker_verbs_exist(self):
        for verb in ("test_worker", "worker_sync", "worker_pull"):
            self.assertIn(verb, web_mod.ACTIONS)

    def test_pull_asks_twice_before_it_overwrites(self):
        with mock.patch.object(config, "WORKER_URL", "https://dj.test"):
            out = web_mod.action_worker_pull(self.ctx, {})
        self.assertIn("sure=1", out)
        self.assertIn("replaces", out)

    def test_sync_refuses_when_there_is_no_worker(self):
        with mock.patch.object(config, "WORKER_URL", ""):
            self.assertIn("no Worker URL", web_mod.action_worker_sync(self.ctx, {}))
            self.assertIn("no Worker URL", web_mod.action_worker_pull(self.ctx, {"sure": ["1"]}))

    def test_settings_save_the_worker_fields_and_keep_a_blank_token(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(config, "LLM_CONFIG_FILE", Path(d) / "config.json"):
                code, _out = web_mod.save_settings({"worker_url": ["https://dj.test"],
                                                    "worker_profile": ["laptop"],
                                                    "worker_token": ["tok"]})
                self.assertEqual(code, 200)
                saved = config.load_llm_config()
                self.assertEqual(saved["WORKER_URL"], "https://dj.test")
                self.assertEqual(saved["WORKER_PROFILE"], "laptop")
                self.assertEqual(saved["WORKER_TOKEN"], "tok")
                # a blank token field is the browser emptying it on reload, not
                # a request to un-authenticate the machine
                web_mod.save_settings({"worker_url": ["https://dj.test"]})
                self.assertEqual(config.load_llm_config()["WORKER_TOKEN"], "tok")
                web_mod.save_settings({"clear_worker_token": ["1"]})
                self.assertEqual(config.load_llm_config()["WORKER_TOKEN"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
