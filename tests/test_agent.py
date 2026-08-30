"""Tests for the AI DJ agent (the Gemini Live API text chat + tool loop).

These run the protocol without a network: the Live connection is a fake object
that returns scripted server messages, so we assert the *ours* side of the
handshake (setup -> clientContent -> serverContent / toolCall -> toolResponse)
and the DJ tool dispatch, not Google's servers.
"""
from __future__ import annotations

import unittest
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap)

import agent
import config


class _Conn:
    """A scripted WebSocket stand-in: returns its queue of messages, records sends."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def recv(self):
        if not self.messages:
            raise AssertionError("ran out of scripted server messages")
        return self.messages.pop(0)

    def close(self):
        pass


class UrlSetupTests(unittest.TestCase):
    def test_ws_url_is_a_wss_endpoint_with_a_the_key(self):
        url = agent.live_ws_url("abc123")
        self.assertTrue(url.startswith("wss://"))
        self.assertIn("/ws/google.ai.generativelanguage.v1beta.GenerativeService."
                      "BidiGenerateContent", url)
        self.assertIn("key=abc123", url)

    def test_ws_url_refuses_a_missing_key(self):
        with self.assertRaises(agent.LiveUnavailable):
            agent.live_ws_url("")

    def test_setup_names_a_live_model_and_text_modality(self):
        setup = agent.build_setup("gemini-live", "You are a DJ.",
                                  agent.tool_declarations())
        body = setup["setup"]
        self.assertEqual(body["model"], "models/gemini-live")
        self.assertEqual(body["generationConfig"]["responseModalities"], ["TEXT"])
        self.assertIn("You are a DJ.", body["systemInstruction"]["parts"][0]["text"])
        names = [t["name"] for t in body["tools"][0]["functionDeclarations"]]
        self.assertIn("play", names)
        self.assertIn("skip", names)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_names_the_set(self):
        dj = _dj()
        ctx = _ctx(dj)
        snap = agent.dj_snapshot(ctx)
        self.assertEqual(snap["now"], "Radiohead - Everything In Its Right Place")
        self.assertEqual(snap["vibe"], "lofi tuesday night")
        self.assertEqual(snap["queued"], 3)

    def test_prompt_carries_the_persona_and_the_set(self):
        snap = agent.dj_snapshot(_ctx(_dj()))
        prompt = agent.build_system_prompt(snap)
        self.assertIn("DJ inside Spotube DJ", prompt)
        self.assertIn("Radiohead", prompt)
        self.assertIn("lofi tuesday night", prompt)
        self.assertIn("play (a song/artist/mood)", prompt)


class RunTurnTests(unittest.TestCase):
    def test_a_text_turn_returns_the_assembled_reply(self):
        conn = _Conn([
            {"setupComplete": {}},
            {"serverContent": {"modelTurn": {"parts": [{"text": "Hey, "}]}}},
            {"serverContent": {"modelTurn": {"parts": [{"text": "that's a great one."}]}}},
            {"serverContent": {"turnComplete": True}},
        ])
        got = []
        reply = agent.run_turn(conn,
                               setup=agent.build_setup("m", "p", agent.tool_declarations()),
                               history=[{"role": "user", "text": "old"}],
                               user_text="play a song",
                               executor=lambda n, a: {"note": "ok"}, on_text=got.append)
        self.assertEqual(reply, "Hey, that's a great one.")
        self.assertEqual(got, ["Hey, ", "that's a great one."])
        # setup first, then a clientContent turn
        self.assertIn("setup", conn.sent[0])
        self.assertTrue(any("clientContent" in s for s in conn.sent))

    def test_a_tool_call_is_executed_and_the_turn_continues(self):
        conn = _Conn([
            {"setupComplete": {}},
            {"serverContent": {"modelTurn": {"parts": [{"text": "I queued "}]}}},
            {"toolCall": {"functionCalls": [
                {"name": "play", "id": "c1", "args": {"query": "radiohead"}}]}},
            {"serverContent": {"modelTurn": {"parts": [{"text": "a Radiohead mix."}]}}},
            {"serverContent": {"turnComplete": True}},
        ])
        calls = []
        reply = agent.run_turn(conn,
                               setup=agent.build_setup("m", "p", agent.tool_declarations()),
                               history=[], user_text="play radiohead",
                               executor=lambda n, a: (calls.append((n, a)) or {"note": "ok"}))
        self.assertEqual(reply, "I queued a Radiohead mix.")
        self.assertEqual(calls, [("play", {"query": "radiohead"})])
        # the tool result was sent back to resume generation
        tool = [s for s in conn.sent if "toolResponse" in s]
        self.assertTrue(tool)
        self.assertEqual(tool[0]["toolResponse"]["functionResponses"][0]["name"], "play")


class AgentChatTests(unittest.TestCase):
    def _agent(self, messages):
        dj = _dj()
        ctx = _ctx(dj)
        a = agent.DJAgent(ctx, connect=lambda url, timeout: _Conn(messages))
        # leave history as-is; the agent owns it
        return a

    def test_chat_needs_a_key(self):
        a = self._agent([{"setupComplete": {}}, {"serverContent": {"turnComplete": True}}])
        with mock.patch.object(agent, "api_key", return_value=""):
            with self.assertRaises(agent.LiveUnavailable):
                a.chat("hi")

    def test_chat_returns_the_reply_and_keeps_history(self):
        a = self._agent([
            {"setupComplete": {}},
            {"serverContent": {"modelTurn": {"parts": [{"text": "Hello!"}]}}},
            {"serverContent": {"turnComplete": True}},
        ])
        with mock.patch.object(agent, "api_key", return_value="k"):
            reply = a.chat("hello there")
        self.assertEqual(reply, "Hello!")
        roles = [m["role"] for m in a.history]
        self.assertEqual(roles, ["user", "model"])

    def test_an_empty_message_is_a_noop(self):
        a = self._agent([])
        with mock.patch.object(agent, "api_key", return_value="k"):
            self.assertEqual(a.chat("   "), "")


class ExecuteToolTests(unittest.TestCase):
    def test_play_starts_a_job(self):
        ctx = _ctx(_dj())
        with mock.patch.object(ctx, "start_job") as sj:
            out = agent.execute_tool(ctx, "play", {"query": "sad lofi"})
        sj.assert_called_once()
        self.assertIn("mix", out.get("note", ""))

    def test_volume_clamps(self):
        ctx = _ctx(_dj())
        self.assertEqual(agent.execute_tool(ctx, "volume", {"level": 150})["note"],
                         "volume 100%")
        self.assertEqual(agent.execute_tool(ctx, "volume", {"level": "x"})["note"],
                         "volume needs a number 0-100")

    def test_like_when_nothing_playing(self):
        dj = _dj()
        dj.current = None
        out = agent.execute_tool(_ctx(dj), "like", {})
        self.assertIn("nothing playing", out["note"])

    def test_dislike_records_a_verdict_and_moves_on(self):
        # the tool goes through `taste.record_dislike`, so the import must be live
        dj = _dj()
        ctx = _ctx(dj)
        with mock.patch("agent.taste.record_dislike") as record, \
             mock.patch.object(dj, "skip", return_value={"title": "next"}):
            out = agent.execute_tool(ctx, "dislike", {})
        record.assert_called_once()
        self.assertIn("won't play that again", out["note"])


def _dj():
    class _Queue:
        def upcoming(self, n):
            return [{"artist": "Boards of Canada", "title": "lofi beat", "duration": 200}] * 3

    class _DJ:
        def __init__(self):
            self.current = {"artist": "Radiohead", "title": "Everything In Its Right Place"}
            self.info = {"vibe": "lofi tuesday night"}
            self.request = "chill lofi"
            self.station = ""
            self.paused = False
            self.queue = _Queue()

        def start(self, q): return None
        def taste_mix(self): return None
        def skip(self): return {"title": "x"}
        def like(self): return None
        def unlike(self): return None
        def is_liked(self, t): return False
        def pause(self): return None
        def resume(self): return None
        def volume(self, pct): return None

    return _DJ()


def _ctx(dj):
    class _Ctx:
        def __init__(self, d):
            self.dj = d
            self.volume = 70

        def start_job(self, target):
            target()
            return True

    return _Ctx(dj)


if __name__ == "__main__":
    unittest.main()
