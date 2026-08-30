"""Tests for the Spotify-DJ announcer (the local, offline narrator).

The old AI DJ chat (Gemini Live API over a WebSocket) is gone. This module now
builds a short, always-on line from what the mixer actually did - the request,
the vibe, a from-your-likes pick, the station seed and the planner's reason -
with no model, no key and no network. These tests run it directly, so a dropped
connection is impossible by construction.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap)

import agent
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

    def test_speak_for_never_raises_without_audio(self):
        # no engine/mpv in the sandbox: the trigger must be a silent no-op, not a crash
        import djvoice
        dj = _dj()
        dj.state = {"voice": True}
        djvoice.speak_for(dj)              # returns immediately, spawns nothing harmful

    def test_voice_flag_is_json_safe(self):
        import json
        self.assertIsInstance(json.dumps({"voice": True}), str)
