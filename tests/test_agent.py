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

    def test_lead_line_beats_the_intro_when_handing_over(self):
        import djvoice
        dj = _dj()
        # keyless: the hand-over gets the \"up next\" template, the intro the current one
        with mock.patch.object(config, "LLM_API_KEY", ""):
            self.assertEqual(djvoice._creative_line(dj, agent, next_up=True),
                             agent.lead_line(dj))
            self.assertEqual(djvoice._creative_line(dj, agent, next_up=False),
                             agent.dj_speech(dj))
        # with nothing queued the lead-in is empty, so the DJ stays silent
        dj.queue = type("Q", (), {"upcoming": lambda self, n: []})()
        with mock.patch.object(config, "LLM_API_KEY", ""):
            self.assertEqual(djvoice._lead_line(dj, agent), "")

    def test_lead_prompt_and_line_hand_over_to_the_next_song(self):
        dj = _dj()
        prompt = agent.lead_prompt(dj, "English")
        self.assertIn("almost finished", prompt)
        self.assertIn("Reach for the Dead", prompt)
        self.assertIn("Write the line in English", prompt)
        line = agent.lead_line(dj)
        self.assertIn("Up next Boards,", line)
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

    def test_live_model_defaults_to_the_live_native_audio_model(self):
        import djvoice
        # the default model speaks over the Live API WebSocket (not generateContent)
        self.assertEqual(djvoice.tts_model(), "gemini-3.1-flash-live-preview")
        self.assertTrue(djvoice._is_live_model(djvoice.tts_model()))
        self.assertFalse(djvoice._is_live_model("gemini-3.1-flash-tts-preview"))
        self.assertTrue(djvoice._is_live_model("gemini-2.5-flash-live-preview"))
        self.assertIn("BidiGenerateContent", djvoice._live_url())

    def test_live_synth_writes_a_wav_from_the_websocket_audio(self):
        import djvoice
        import base64, json, wave as wave_mod, tempfile
        from pathlib import Path
        pcm = b"\x00\x00\x01\x00" * 100
        b64 = base64.b64encode(pcm).decode()
        frames = [
            (1, b'{"setupComplete":{}}'),
            (1, ('{"serverContent":{"modelTurn":{"parts":[{"inlineData":{"data":"%s",'
                 '"mimeType":"audio/L16;codec=pcm;rate=24000"}}]}}}' % b64).encode()),
            (1, b'{"serverContent":{"turnComplete":true}}'),
        ]
        sent = []
        class FakeSock:
            def close(self):
                pass
        def fake_send(sock, opcode, payload, mask=True):
            sent.append(json.loads(payload))
        fake_iter = iter(frames)
        with mock.patch.object(config, "LLM_API_KEY", "x"):
            with mock.patch.object(djvoice, "tts_model",
                                   return_value="gemini-3.1-flash-live-preview"):
                with mock.patch.object(djvoice, "_ws_connect", return_value=FakeSock()):
                    with mock.patch.object(djvoice, "_ws_read_frame",
                                           side_effect=lambda *a: next(fake_iter)):
                        with mock.patch.object(djvoice, "_ws_send_frame",
                                               side_effect=fake_send):
                            out = Path(tempfile.mkstemp(suffix=".wav")[1])
                            self.assertTrue(djvoice._gemini_live_synth("hello", out))
                            with wave_mod.open(str(out), "rb") as wf:
                                self.assertEqual(wf.getframerate(), 24000)
                                self.assertEqual(wf.getsampwidth(), 2)
                                self.assertEqual(wf.getnchannels(), 1)
                            out.unlink(missing_ok=True)
        # first frame is the Live setup with the voice; second is the text input
        self.assertEqual(sent[0]["setup"]["model"], "models/gemini-3.1-flash-live-preview")
        self.assertEqual(sent[0]["setup"]["generationConfig"]["speechConfig"]
                         ["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Despina")
        self.assertEqual(sent[1]["realtimeInput"]["text"], "hello")

    def test_live_synth_writes_a_container_as_is(self):
        # the Live model may return ogg/opus rather than PCM; those bytes are
        # written untouched (mpv sniffs the container, not the .wav temp name)
        import djvoice
        import base64, tempfile
        from pathlib import Path
        ogg_header = b"OggS" + b"\x00" * 100
        b64 = base64.b64encode(ogg_header).decode()
        frames = [
            (1, b'{"setupComplete":{}}'),
            (1, ('{"serverContent":{"modelTurn":{"parts":[{"inlineData":{"data":"%s",'
                 '"mimeType":"audio/ogg;codecs=opus"}}]}}}' % b64).encode()),
            (1, b'{"serverContent":{"turnComplete":true}}'),
        ]
        fake_iter = iter(frames)
        with mock.patch.object(config, "LLM_API_KEY", "x"):
            with mock.patch.object(djvoice, "tts_model",
                                   return_value="gemini-3.1-flash-live-preview"):
                with mock.patch.object(djvoice, "_ws_connect",
                                       return_value=type("S", (), {"close": lambda s: None})()):
                    with mock.patch.object(djvoice, "_ws_read_frame",
                                           side_effect=lambda *a: next(fake_iter)):
                        with mock.patch.object(djvoice, "_ws_send_frame"):
                            out = Path(tempfile.mkstemp(suffix=".wav")[1])
                            self.assertTrue(djvoice._gemini_live_synth("hello", out))
                            self.assertEqual(out.read_bytes()[:4], b"OggS")
                            out.unlink(missing_ok=True)

    def test_live_synth_is_skipped_for_a_tts_model(self):
        import djvoice
        from pathlib import Path
        with mock.patch.object(config, "LLM_API_KEY", "x"):
            with mock.patch.object(djvoice, "tts_model",
                                   return_value="gemini-3.1-flash-tts-preview"):
                with mock.patch.object(djvoice, "_ws_connect") as wc:
                    self.assertFalse(djvoice._gemini_live_synth("hi", Path("/tmp/x.wav")))
        wc.assert_not_called()

    def test_websocket_frames_round_trip(self):
        # the RFC 6455 framing the Live client uses: masked client text, unmasked
        # server payload, ping opcode - all decoded by the same reader
        import djvoice
        import socket
        a, b = socket.socketpair()
        try:
            msg = '{"setup":{"model":"m"}}'
            djvoice._ws_send_frame(a, 1, msg.encode(), mask=True)
            op, payload = djvoice._ws_read_frame(b)
            self.assertEqual((op, payload.decode()), (1, msg))
            b.sendall(bytes([0x82, 0x05]) + b"HELLO")
            self.assertEqual(djvoice._ws_read_frame(a), (2, b"HELLO"))
            b.sendall(bytes([0x89, 0x00]))
            self.assertEqual(djvoice._ws_read_frame(a)[0], 9)
        finally:
            a.close()
            b.close()
