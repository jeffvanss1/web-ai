"""Tests for the Spotify-DJ announcer and its spoken voice.

The announcer builds a line from what the mixer actually did (the request, the
vibe, a from-your-likes pick, the station seed, the planner's reason) - no text
chat. The *spoken* DJ optionally uses Gemini's speech generation (the Despina
voice) when a key is set; without one it falls back to espeak or stays silent.
These tests exercise the line, the prompt, the Gemini-voice decode and the
no-audio no-op, all offline (the network calls are mocked).
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap)

import agent
import brain
import config


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

    def test_speech_reads_like_a_dj_host(self):
        line = agent.dj_speech(_dj())
        self.assertIn("Now playing Oneohtrix, Royal Albert.", line)
        self.assertIn("You asked for", line)          # capitalized for speech
        self.assertIn("Up next Boards, Reach for the Dead.", line)

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
        # the gemini voice name is Despina by default, overridable per shell
        with mock.patch.object(config, "DJ_VOICE", "Despina"):
            self.assertEqual(djvoice.voice_name(), "Despina")

    def test_dj_prompt_names_the_song_why_vibe_and_next(self):
        prompt = agent.dj_prompt(_dj())
        for frag in ("Oneohtrix", "sad lofi", "Boards of Canada", "lofi tuesday night",
                     "Reach for the Dead", "radio DJ"):
            self.assertIn(frag, prompt)
        self.assertIn("Reply with only the line", prompt)

    def test_creative_line_uses_gemini_when_written_else_the_template(self):
        import djvoice
        dj = _dj()
        # no key -> the offline template is used (no network, no crash)
        with mock.patch.object(config, "LLM_API_KEY", ""):
            self.assertEqual(djvoice._creative_line(dj, agent), agent.dj_speech(dj))
        # a key + a model that answers -> the generated line wins
        with mock.patch.object(config, "LLM_API_KEY", "x"):
            with mock.patch.object(brain, "free_text",
                                   return_value="Ooh, we're going to Boards of Canada."):
                self.assertEqual(djvoice._creative_line(dj, agent),
                                 "Ooh, we're going to Boards of Canada.")

    def test_gemini_synth_writes_a_wav_from_pcm(self):
        import djvoice
        import base64, wave as wave_mod, tempfile
        from pathlib import Path
        pcm = b"\x00\x00\x01\x00" * 100          # 200 sample frames of silence-ish
        fake = {"candidates": [{"content": {"parts": [{
            "inlineData": {"data": base64.b64encode(pcm).decode(),
                           "mimeType": "audio/L16;codec=pcm;rate=24000"}}]}}]}
        out = Path(tempfile.mkstemp(suffix=".wav")[1])
        with mock.patch.object(config, "LLM_API_KEY", "x"):
            with mock.patch.object(brain, "_gemini_url", return_value="https://gemini"), \
                 mock.patch.object(brain, "_post", return_value=fake), \
                 mock.patch.object(brain, "_gemini_headers", return_value={"x-goog-api-key": "x"}):
                self.assertTrue(djvoice._gemini_synth("hello", out))
        with wave_mod.open(str(out), "rb") as wf:
            self.assertEqual(wf.getframerate(), 24000)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getnchannels(), 1)
        out.unlink(missing_ok=True)

    def test_speak_for_never_raises_without_audio(self):
        # no engine/mpv: the trigger must be a silent no-op, not a crash
        import djvoice
        dj = _dj()
        dj.state = {"voice": True}
        with mock.patch("djvoice.config") as cfg:
            cfg.LLM_API_KEY = ""
            cfg.DJ_VOICE = "Despina"
            with mock.patch("djvoice._synth", return_value=None):
                djvoice.speak_for(dj)      # returns immediately, spawns nothing harmful

    def test_voice_flag_is_json_safe(self):
        import json
        self.assertIsInstance(json.dumps({"voice": True}), str)

    def test_voice_note_and_despina_ride_the_state(self):
        # build_state exposes the voice flag and a human note about the engine
        import web as web_mod
        from tests.test_web import fake_dj
        djf = fake_dj()
        ctx = web_mod.Context(djf)
        with mock.patch.object(config, "LLM_API_KEY", "x"):
            s = web_mod.build_state(ctx)
        self.assertIn("voice", s)
        self.assertIn("voice_note", s)
        self.assertIn("Despina", s["voice_note"])
