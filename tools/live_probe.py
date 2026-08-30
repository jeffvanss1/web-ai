#!/usr/bin/env python3
"""Minimal Gemini Live API probe - isolates the app from the API.

Run it on a machine that HAS network access to Google (and a Live-capable Gemini
API key). It opens the Live WebSocket, sends a reference-exact `setup`, sends the
text as realtimeInput, and prints every raw frame with a timestamp so we can see
exactly what the server does.

Usage:
    GEMINI_API_KEY=... python3 tools/live_probe.py                    # default model
    GEMINI_API_KEY=... python3 tools/live_probe.py gemini-3.1-flash-live-preview Despina

It never falls back or hides anything: the log is the raw wire traffic.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise EOFError("closed")
        buf += c
    return buf


def read_message(sock):
    first_op = None
    buf = b""
    while True:
        b1, b2 = read_exact(sock, 2)
        op = b1 & 0x0F
        fin = bool(b1 & 0x80)
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(read_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(read_exact(sock, 8), "big")
        key = read_exact(sock, 4) if masked else b""
        payload = read_exact(sock, length)
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if op == 0:
            if first_op is not None:
                buf += payload
        else:
            first_op = op
            buf = payload
        if fin:
            return first_op, buf


def send_frame(sock, opcode, payload, mask=True):
    length = len(payload)
    head = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        head.append((0x80 if mask else 0) | length)
    elif length < 65536:
        head.append((0x80 if mask else 0) | 126)
        head += length.to_bytes(2, "big")
    else:
        head.append((0x80 if mask else 0) | 127)
        head += length.to_bytes(8, "big")
    key = os.urandom(4) if mask else b""
    if mask:
        head += key
    body = bytes(b ^ key[i % 4] for i, b in enumerate(payload)) if mask else payload
    sock.sendall(bytes(head) + body)


def connect(url, timeout=30.0):
    u = urllib.parse.urlparse(url)
    host, port = u.hostname, u.port or 443
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    sock = ctx.wrap_socket(raw, server_hostname=host)
    sock.settimeout(timeout)
    nonce = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {nonce}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        c = sock.recv(1)
        if not c:
            raise EOFError("handshake")
        head += c
    status = head.decode("latin1").split("\r\n", 1)[0]
    if " 101 " not in status:
        raise OSError(f"handshake refused: {status}")
    print(f"[{time.strftime('%H:%M:%S')}] connected: {status}")
    return sock


def main():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        print("set GEMINI_API_KEY=... and run again")
        sys.exit(1)
    model = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.1-flash-live-preview"
    voice = sys.argv[2] if len(sys.argv) > 2 else "Despina"
    text = "Hi, this is Spotube DJ testing the Gemini Live voice."
    url = ("wss://generativelanguage.googleapis.com/ws/"
           "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
           "?key=" + urllib.parse.quote(key))

    sock = connect(url)

    # reference-exact setup (ai.google.dev/api/live): model + generationConfig
    setup = {
        "model": "models/" + model,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                "voiceName": voice}}},
        },
    }
    frame = json.dumps({"setup": setup})
    print(f"[{time.strftime('%H:%M:%S')}] -> setup: {frame}")
    send_frame(sock, 1, frame.encode())

    got_setup = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            op, data = read_message(sock)
        except socket.timeout:
            print(f"[{time.strftime('%H:%M:%S')}] (no frame for a while - still waiting)")
            continue
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] read error: {type(e).__name__}: {e}")
            break
        names = {0: "cont", 1: "text", 2: "binary", 8: "close", 9: "ping", 10: "pong"}
        print(f"[{time.strftime('%H:%M:%S')}] <- {names.get(op, op)} "
              f"({len(data)} bytes) {data[:120]!r}")
        if op == 8:
            code = int.from_bytes(data[:2], "big") if data else 0
            reason = data[2:].decode("utf-8", "replace")
            print(f"[{time.strftime('%H:%M:%S')}] CLOSE status {code}: {reason}")
            break
        if op == 9:
            send_frame(sock, 10, data)
        if op == 1:
            try:
                msg = json.loads(data)
            except Exception:
                continue
            if "setupComplete" in msg:
                got_setup = True
                print(f"[{time.strftime('%H:%M:%S')}] SETUP COMPLETE "
                      f"-> sending realtimeInput text")
                send_frame(sock, 1, json.dumps({"realtimeInput": {"text": text}}).encode())
            if msg.get("serverContent", {}).get("turnComplete"):
                print(f"[{time.strftime('%H:%M:%S')}] TURN COMPLETE")
                break
    print(f"[{time.strftime('%H:%M:%S')}] done; got_setup={got_setup}")
    try:
        sock.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
