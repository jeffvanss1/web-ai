"""Tests for the Spotify-DJ announcer and its spoken voice.

The announcer builds a line from what the mixer actually did (the request, the
vibe, a from-your-likes pick, the station seed, the planner's reason) - no text
chat. The *spoken* DJ uses Gemini's speech generation (the Despina voice) when a key is
set; without one it stays silent (there is no offline/robotic fallback).
These tests exercise the line, the prompt, the Gemini-voice decode and the
no-audio no-op, all offline (the network calls are mocked).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import unittest
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap)

import agent
import brain
import config
import workerclient


def _dj():
    """A minimal DJ stand-in that carries the facts the narrator reads."""
    class Q:
        def __init__(self, rows):
            self.rows = rows
        def upcoming(self, n):
            return self.rows[:n]
    class State:
        pass

    dj = State()
    dj.request = "sad lofi"
    dj.station = "Boards of Canada"
    dj.info = {"engine": "gemini", "why": "dark ambient, fits the set",
               "vibe": "lofi tuesday night", "queries": ["sad lofi"]}
    dj.current = {"id": "v1", "title": "Royal Albert", "artist": "Oneohtrix",
                  "mixed": False}
    dj.queue = Q([{"id": "v2", "title": "Reach for the Dead", "artist": "Boards"}])
    return dj


class _Ctx:
    def __init__(self, dj):
        self.dj = dj


class SnapshotTests(unittest.TestCase):
    def test_snapshot_names_the_set(self):
        snap = agent.dj_snapshot(_Ctx(_dj()))
        self.assertEqual(snap["vibe"], "lofi tuesday night")
        self.assertEqual(snap["station"], "Boards of Canada")
        self.assertIn("Oneohtrix", snap["now"])
        self.assertIn("Boards", snap["next"])

    def test_why_mixes_the_request_and_the_planner_reason(self):
        dj = _dj()
        snap = agent.dj_snapshot(_Ctx(dj))
        self.assertIn("sad lofi", snap["why"])
        self.assertIn("dark ambient", snap["why"])

    def test_a_liked_pick_is_named_as_such(self):
        dj = _dj()
        dj.current["mixed"] = True
        snap = agent.dj_snapshot(_Ctx(dj))
        self.assertIn("from your likes", snap["why"])

    def test_station_is_mentioned_when_there_is_one(self):
        dj = _dj()
        snap = agent.dj_snapshot(_Ctx(dj))
        self.assertIn("Boards of Canada", snap["why"])

    def test_nothing_playing_is_a_blank_now(self):
        dj = _dj()
        dj.current = None
        snap = agent.dj_snapshot(_Ctx(dj))
        self.assertIn("nothing playing", snap["now"].lower())
        self.assertIn("Reach for the Dead", snap["next"])

    def test_snapshot_is_json_safe(self):
        import json
        json.dumps(agent.dj_snapshot(_Ctx(_dj())))


class NarrateTests(unittest.TestCase):
    def test_a_full_snapshot_reads_like_the_dj(self):
        line = agent.narrate(agent.dj_snapshot(_Ctx(_dj())))
        self.assertIn("Now playing", line)
        self.assertIn("Why:", line)
        self.assertIn("lofi tuesday night", line)
        self.assertIn("Up next:", line)

    def test_nothing_playing_tells_you_what_to_do(self):
        dj = _dj()
        dj.current = None
        dj.info = {}
        snap = agent.dj_snapshot(_Ctx(dj))
        line = agent.narrate(snap)
        self.assertIn("Nothing playing", line)
        self.assertIn("tell me a song or a mood", line)

    def test_empty_snapshot_still_returns_text(self):
        self.assertIsInstance(agent.narrate({}), str)
        self.assertIsInstance(agent.narrate({"now": "", "why": "", "next": ""}), str)

    def test_no_up_next_omits_the_phrase_not_a_hint(self):
        dj = _dj()
        dj.queue = type("Q", (), {"upcoming": lambda self, n: []})()
        line = agent.narrate(agent.dj_snapshot(_Ctx(dj)))
        self.assertNotIn("Up next:", line)

    def test_a_failed_snapshot_never_raises(self):
        class Bad:
            pass
        bad = Bad()
        with self.assertRaises(Exception):
            agent.dj_snapshot(_Ctx(bad))       # missing .dj facts *does* raise
        self.assertIsInstance(agent.narrate({}), str)


class OfflineByConstructionTests(unittest.TestCase):
    """The narrator must not reach a network, a model or a websocket."""

    def test_no_websocket_dependency_is_imported(self):
        import sys
        self.assertNotIn("websocket", [m for m in sys.modules if m.startswith("websocket")])

    def test_module_has_no_live_classes(self):
        self.assertFalse(hasattr(agent, "DJAgent"))
        self.assertFalse(hasattr(agent, "LiveConnection"))
        self.assertFalse(hasattr(agent, "run_turn"))
        self.assertFalse(hasattr(agent, "build_system_prompt"))

    def test_agent_imports_without_a_key(self):
        # config must never require LLM_API_KEY just to import the announcer
        self.assertTrue(callable(agent.narrate))


class SpeechTests(unittest.TestCase):
    """The DJ's spoken line (agent.dj_speech) and the voice trigger (djvoice)."""

    def test_speech_reads_like_a_dj_announcer(self):
        line = agent.dj_speech(_dj())
        # a DJ announcer signposts before naming the song, then hands over to next
        self.assertIn("here's Oneohtrix, Royal Albert.", line)
        self.assertIn("You asked for", line)          # capitalized for speech
        self.assertIn("Stay right here - up next Boards, Reach for the Dead.", line)

    def test_speech_nothing_playing_is_silent(self):
        dj = _dj()
        dj.current = None
        self.assertEqual(agent.dj_speech(dj), "")

    def test_speech_can_be_played_by_a_voice(self):
        # welcome the DJ line into the voice module without a real engine
        import djvoice
        self.assertTrue(callable(djvoice.speak_for))
        self.assertIsInstance(djvoice.enabled(), bool)

    def test_voice_defaults_on_and_respects_state_and_env(self):
        import djvoice
        dj = _dj()
        dj.state = {"voice": True}
        self.assertTrue(djvoice.enabled(dj))
        dj.state = {"voice": False}
        self.assertFalse(djvoice.enabled(dj))
        # a hard env override wins even when the state says on
        with mock.patch.dict(os.environ, {"SPOTUBE_DJ_VOICE": "off"}):
            dj.state = {"voice": True}
            self.assertFalse(djvoice.enabled(dj))

    def test_voice_defaults_to_despina(self):
        import djvoice
        # the gemini voice name is Despina by default, overridable per shell.
        # load_dj_voice reads the saved config first; a fresh/empty config means the
        # module default (Despina) is what actually talks.
        with mock.patch.object(config, "DJ_VOICE", "Despina"):
            with mock.patch.object(config, "load_llm_config", return_value={}):
                self.assertEqual(djvoice.voice_name(), "Despina")

    def test_voice_setting_rides_the_state(self):
        # a voice saved in Settings (config) is what the DJ uses, and it can be a
        # non-English one whose written language follows (Achernar -> Arabic).
        import djvoice
        self.assertEqual(config.voice_lang("Despina"), "English")
        self.assertEqual(config.voice_lang("Achernar"), "Arabic")
        with mock.patch.object(config, "load_llm_config",
                               return_value={"DJ_VOICE": "Achernar"}):
            self.assertEqual(djvoice.voice_name(), "Achernar")
        # a value already on disk is used as-is (the Settings endpoint is what
        # guards against an arbitrary name ever being written there; load trusts it)
        with mock.patch.object(config, "load_llm_config",
                               return_value={"DJ_VOICE": "No Such Voice"}):
            self.assertEqual(djvoice.voice_name(), "No Such Voice")
        # the catalog drives the dropdown and recognises names case-insensitively
        names = [v[0] for v in config.GEMINI_TTS_VOICES]
        self.assertIn("Despina", names)
        self.assertIn("Sulafat", names)
        self.assertEqual(config.voice_lang("sulafat"), "English")   # case-insensitive
        self.assertEqual(config.voice_lang("ACHE RNAR"), "English")  # unknown -> English

    def test_lead_time_is_bounded_and_configurable(self):
        import djvoice
        self.assertGreaterEqual(config.DJ_LEAD_SECS, 2.0)
        self.assertLessEqual(config.DJ_LEAD_SECS, 60.0)
        self.assertEqual(djvoice.lead_secs(), config.DJ_LEAD_SECS)

    def test_lead_in_wait_fires_near_the_end_and_aborts_on_a_skip(self):
        import djvoice
        dj = _dj()
        class P:
            def progress(self): return (24.0, 30.0)    # 6s remain < lead of 10
        dj.player = P()
        # the DJ is still on v1: with 6s left it is time to speak
        self.assertTrue(djvoice._wait_lead(dj, "v1"))
        # but a fast skipper who has moved on gets silence, never a stale line
        dj.current = {"id": "v9", "title": "Other", "artist": "Unknown"}
        self.assertFalse(djvoice._wait_lead(dj, "v1"))
        # a track with no player simply stays quiet
        dj.player = None
        self.assertFalse(djvoice._wait_lead(dj, "v9"))

    def test_lead_in_wait_does_not_bail_at_60s_on_a_normal_track(self):
        # The clip is pre-synthesized at track START, so on a normal 3-5 min song
        # _wait_lead must keep polling until the track nears its END (~lead secs
        # before it ends). The old fixed-60s cap gave up mid-song and the DJ never
        # spoke on anything longer than a minute. Advancing a 200s track one second
        # per poll, the lead window (<=10s left) only arrives after polling well past
        # 60s, and it must still fire.
        import djvoice
        from unittest import mock
        dj = _dj()
        clock = {"t": 0.0}
        pos = {"v": 0.0}
        def tick():
            clock["t"] += 1.0
            return clock["t"]
        def progress():
            pos["v"] = min(pos["v"] + 1.0, 200.0)
            return (pos["v"], 200.0)
        class P:
            def progress(self): return progress()
        dj.player = P()
        with mock.patch.object(djvoice, "time") as tm:
            tm.monotonic.side_effect = tick
            tm.sleep.return_value = None
            self.assertTrue(djvoice._wait_lead(dj, "v1"))
        # prove the clock really did advance well past 60s before the lead window
        self.assertGreater(clock["t"], 60.0)

    def test_lead_in_wait_speaks_when_mpv_reports_no_duration(self):
        # The DJ synthesis succeeded ("produced ... via live") but mpv was never
        # called on the 2nd+ hand-off. The player bar shows no total time, i.e.
        # mpv reports duration=0 for these streams. The old _wait_lead only fired
        # `remaining <= lead`, which needs a length, so it bailed at a fixed ~120s
        # deadline and the DJ went silent after the first (intro) line. With no
        # length the DJ must wait on the player's own EOF signal (finished()), the
        # same signal the engine advances on, and speak at the hand-off.
        import djvoice
        from unittest import mock
        dj = _dj()
        class P:
            def __init__(self):
                self.real_dur = 200.0
            def progress(self):
                pos = djvoice.time.monotonic()
                return (min(pos, self.real_dur), 0.0)   # no reported duration
            def finished(self):
                return djvoice.time.monotonic() >= self.real_dur
        dj.player = P()
        clock = {"t": 0.0}
        def tick():
            clock["t"] += 1.0
            return clock["t"]
        with mock.patch.object(djvoice, "time") as tm:
            tm.monotonic.side_effect = tick
            tm.sleep.return_value = None
            self.assertTrue(djvoice._wait_lead(dj, "v1"))
        # it waited the whole (duration-less) song to its real end, not just ~2min
        self.assertGreater(clock["t"], 120.0)

    def test_lead_in_wait_stays_silent_on_a_manual_skip_of_a_durationless_track(self):
        # A manual skip replaces the file before it can end, so finished() is False
        # and a stale line must not be played even when mpv reports no duration.
        import djvoice
        from unittest import mock
        dj = _dj()
        class P:
            def finished(self):
                return False
            def progress(self):
                return (0.0, 0.0)
        dj.player = P()
        with mock.patch.object(djvoice, "time") as tm:
            tm.monotonic.return_value = 5.0
            tm.sleep.return_value = None
            # the track has advanced on to v9 (user skipped): the line is stale
            dj.current = {"id": "v9", "title": "Other", "artist": "Unknown"}
            self.assertFalse(djvoice._wait_lead(dj, "v1"))

    def test_lead_line_beats_the_intro_when_handing_over(self):
        import djvoice
        dj = _dj()
        # keyless, pinned to English: the hand-over gets the \"up next\" template,
        # the intro the current one. The app's default is Indonesian, so set
        # DJ_LANG=English to compare the exact English fallback strings.
        with mock.patch.object(config, "WORKER_URL", ""):
            with mock.patch.object(config, "DJ_LANG", "English"):
                self.assertEqual(djvoice._creative_line(dj, agent, next_up=True),
                                 agent.lead_line(dj, "English"))
                self.assertEqual(djvoice._creative_line(dj, agent, next_up=False),
                                 agent.dj_speech(dj, "English"))
        # with nothing queued the lead-in is empty, so the DJ stays silent
        dj.queue = type("Q", (), {"upcoming": lambda self, n: []})()
        with mock.patch.object(config, "WORKER_URL", ""):
            self.assertEqual(djvoice._lead_line(dj, agent), "")

    def test_dj_speaks_indonesian_by_default_and_on_request(self):
        # the app's spoken default is Indonesian (the swap the user asked for), and
        # a saved DJ_LANG rides the config the same way DJ_VOICE does.
        self.assertEqual(config.load_dj_lang(), "Indonesian")
        with mock.patch.object(config, "load_llm_config", return_value={"DJ_LANG": "English"}):
            self.assertEqual(config.load_dj_lang(), "English")
        self.assertEqual(config.voice_lang("Despina"), "English")
        with mock.patch.object(config, "load_llm_config", return_value={"DJ_LANG": "Klingon"}):
            self.assertEqual(config.load_dj_lang(), "Indonesian")
        # the fallback spoken lines render in Bahasa when asked for it
        dj = _dj()
        line = agent.dj_speech(dj, "Indonesian")
        self.assertIn("Baiklah, berikutnya", line)
        self.assertIn("Reach for the Dead", line)
        nxt = agent.lead_line(dj, "Indonesian")
        self.assertIn("selanjutnya", nxt)
        self.assertIn("Sebentar lagi", nxt)
        # the creative-line path passes the configured language to the prompt writer
        import djvoice
        with mock.patch.object(config, "WORKER_URL", "https://dj.test"):
            with mock.patch.object(brain, "free_text") as ft:
                djvoice._creative_line(dj, agent, next_up=True)
                prompt = ft.call_args[0][0]
                self.assertIn("Write the line in Indonesian", prompt)
    def test_lead_prompt_and_line_hand_over_to_the_next_song(self):
        dj = _dj()
        prompt = agent.lead_prompt(dj, "English")
        self.assertIn("almost finished", prompt)
        self.assertIn("Reach for the Dead", prompt)
        self.assertIn("Write the line in English", prompt)
        line = agent.lead_line(dj)
        self.assertIn("Stay right here - up next Boards,", line)
        self.assertIn("Reach for the Dead", line)
        self.assertIn("Coming right up", line)
        # a set with nothing queued says nothing instead of inventing a track
        dj.queue = type("Q", (), {"upcoming": lambda self, n: []})()
        self.assertEqual(agent.lead_prompt(dj, "English"), "")
        self.assertEqual(agent.lead_line(dj), "")

    def test_dj_prompt_names_the_song_why_vibe_and_next(self):
        prompt = agent.dj_prompt(_dj())
        for frag in ("Oneohtrix", "sad lofi", "Boards of Canada", "lofi tuesday night",
                     "Reach for the Dead", "radio DJ"):
            self.assertIn(frag, prompt)
        self.assertIn("Reply with only the line", prompt)

    def test_creative_line_uses_gemini_when_written_else_the_template(self):
        import djvoice
        dj = _dj()
        # no key, pinned to English -> the offline template is used (no network, no crash)
        with mock.patch.object(config, "WORKER_URL", ""):
            with mock.patch.object(config, "DJ_LANG", "English"):
                self.assertEqual(djvoice._creative_line(dj, agent),
                                 agent.dj_speech(dj, "English"))
        # a key + a model that answers -> the generated line wins
        with mock.patch.object(config, "WORKER_URL", "https://dj.test"):
            with mock.patch.object(brain, "free_text",
                                   return_value="Ooh, we're going to Boards of Canada."):
                self.assertEqual(djvoice._creative_line(dj, agent),
                                 "Ooh, we're going to Boards of Canada.")

    def test_worker_speech_writes_the_wav_it_was_sent(self):
        """The Worker returns a complete WAV; this side must not re-encode it."""
        import djvoice
        import wave as wave_mod, tempfile
        from pathlib import Path
        pcm = b"\x00\x00\x01\x00" * 100
        wav = _wav_of(pcm, rate=24000)
        out = Path(tempfile.mkstemp(suffix=".wav")[1])
        try:
            config.WORKER_URL = "https://dj.test"
            with mock.patch.object(workerclient, "speech", return_value=wav) as sp:
                self.assertTrue(djvoice._worker_synth("hello", out))
            self.assertEqual(sp.call_args[0][0], "hello")
            self.assertEqual(sp.call_args[1]["voice"], "Despina")
            with wave_mod.open(str(out), "rb") as wf:
                self.assertEqual(wf.getframerate(), 24000)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getnchannels(), 1)
        finally:
            out.unlink(missing_ok=True)

    def test_a_non_wav_answer_is_refused_instead_of_played(self):
        """A truncated or HTML answer played as audio is a burst of static."""
        import djvoice
        import tempfile
        from pathlib import Path
        out = Path(tempfile.mkstemp(suffix=".wav")[1])
        try:
            config.WORKER_URL = "https://dj.test"
            with mock.patch.object(workerclient, "speech", return_value=b"ID3notawav"):
                self.assertFalse(djvoice._worker_synth("hello", out))
        finally:
            out.unlink(missing_ok=True)

    def test_no_worker_url_means_silence_and_no_request(self):
        import djvoice
        config.WORKER_URL = ""
        with mock.patch.object(workerclient, "speech", return_value=b"x") as sp:
            self.assertIsNone(djvoice._synth("hello"))
        sp.assert_not_called()

    def test_a_quota_failure_stays_silent_and_says_why(self):
        import djvoice
        notes = []
        djvoice.set_logger(notes.append)
        try:
            config.WORKER_URL = "https://dj.test"
            with mock.patch.object(workerclient, "speech",
                                   side_effect=workerclient.WorkerError("quota", "Quota exceeded")):
                self.assertIsNone(djvoice._synth("hello"))
            self.assertTrue(any("quota" in n for n in notes), notes)
        finally:
            djvoice.set_logger(None)

    def test_the_default_tts_model_is_the_rest_one(self):
        import djvoice
        # A Worker cannot open the Live API's WebSocket as a client, so the
        # default is the REST TTS model. A Live name is still accepted: the
        # Worker maps it to its TTS sibling rather than going silent.
        self.assertEqual(djvoice.tts_model(), "gemini-3.1-flash-tts-preview")
        self.assertNotIn("live", djvoice.tts_model())

    def test_engines_lists_the_worker_only_when_one_is_configured(self):
        import djvoice
        config.WORKER_URL = ""
        self.assertEqual(djvoice.engines(), [])
        config.WORKER_URL = "https://dj.test"
        self.assertEqual(djvoice.engines(), ["worker"])

    def test_a_clip_goes_to_the_browser_when_one_is_attached(self):
        """The sink wins over mpv, because that is where the music is heard."""
        import djvoice
        got = {}

        def sink(path, text):
            got["path"], got["text"] = path, text
            return True

        djvoice.set_sink(sink)
        try:
            with mock.patch.object(djvoice, "_play") as played:
                djvoice._play_clip("/tmp/clip.wav", "hello there")
            self.assertEqual(got["text"], "hello there")
            played.assert_not_called()
        finally:
            djvoice.set_sink(None)

    def test_without_a_browser_the_clip_goes_to_mpv(self):
        import djvoice
        djvoice.set_sink(None)
        with mock.patch.object(djvoice, "_play") as played:
            djvoice._play_clip("/tmp/clip.wav", "hello there")
        played.assert_called_once()

    def test_a_sink_that_cannot_publish_falls_back_to_mpv(self):
        """No tab attached (a headless --daemon) must still have a voice."""
        import djvoice
        djvoice.set_sink(lambda path, text: False)
        try:
            with mock.patch.object(djvoice, "_play") as played:
                djvoice._play_clip("/tmp/clip.wav", "hello there")
            played.assert_called_once()
        finally:
            djvoice.set_sink(None)


def _wav_of(pcm: bytes, rate: int = 24000) -> bytes:
    """A real WAV file around `pcm`, the way the Worker answers /v1/speech."""
    import io
    import wave as wave_mod
    buf = io.BytesIO()
    with wave_mod.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()
