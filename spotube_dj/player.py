"""
player.py - local audio playback (no Spotify player API, no Premium).

Primary backend: mpv over a JSON IPC socket, so next/prev/seek/volume and
"did they finish it or skip it?" all work locally.
Secondary: hand a resolved stream URL to Spotube / the default browser.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import bins
import config

HAS_MPV = bins.find("mpv") is not None


class PlayerError(RuntimeError):
    pass


class MPVPlayer:
    """Small mpv wrapper: queue of stream URLs + skip/seek + progress polling."""

    def __init__(self, volume: int = 70, video: bool = False,
                 log_path: str | None = None) -> None:
        self.exe = bins.find("mpv")
        if not self.exe:
            raise PlayerError("mpv not found - install it, or run with --backend spotube")
        self.sock_path = Path(f"/tmp/spotube-dj-{uuid.uuid4().hex[:8]}.sock")
        if self.sock_path.exists():
            self.sock_path.unlink()
        self.video = video
        self.log_path = Path(log_path) if log_path else Path(
            os.environ.get("SPOTUBE_DJ_MPV_LOG", config.APP_DIR / "mpv.log"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("ab", buffering=0)
        # `--keep-open=yes` is the one that matters for a DJ: without it mpv unloads
        # the file the moment it ends, and the manual warns that `eof-reached` is then
        # "cleared immediately after it's set" - a poll 750 ms later cannot see it, and
        # the loop waits for an end-of-file that has already been forgotten. Kept open,
        # the last frame (and `time-pos`) stays readable and the flag survives.
        cmd = [self.exe, "--idle=yes", "--keep-open=yes", f"--volume={volume}",
               "--no-terminal", f"--input-ipc-server={self.sock_path}", "--really-quiet"]
        if not video:
            cmd += ["--force-window=no", "--no-video"]
        if os.environ.get("SPOTUBE_DJ_AO"):
            cmd += [f"--ao={os.environ['SPOTUBE_DJ_AO']}"]
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=self.log)
        self._wait_socket()
        self._now = ""
        # `_played`: this file produced a position at least once. `_load_at`: when we
        # asked for it. Together they are how "the song ended" is told apart from "the
        # song never started", which is the difference between advancing and waiting.
        self._played = False
        self._load_at = time.time()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _wait_socket(self, timeout: float = 8.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.sock_path.exists():
                return
            if self.proc.poll() is not None:
                raise PlayerError(f"mpv exited immediately (code {self.proc.returncode})")
            time.sleep(0.1)
        raise PlayerError("mpv IPC socket never appeared")

    # -- ipc
    def _cmd(self, *args, timeout: float = 3.0):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(str(self.sock_path))
                s.sendall((json.dumps({"command": list(args)}) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                for line in buf.decode(errors="ignore").splitlines():
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if "error" in msg:
                        return msg.get("data")
                return None
        except Exception:
            return None

    def set_property(self, name: str, value) -> None:
        self._cmd("set_property", name, value)

    def get_property(self, name: str):
        return self._cmd("get_property", name)

    # -- transport
    def load(self, url: str, wait: float = 8.0) -> bool:
        """loadfile then wait until mpv stops being idle (or give up)."""
        self._played = False
        self._load_at = time.time()
        self._cmd("loadfile", url, "replace")
        self._cmd("set_property", "pause", False)
        end = time.time() + wait
        while time.time() < end:
            if self.get_property("idle-active") is False:
                self._played = True
                return True
            if not self.alive():
                return False
            time.sleep(0.2)
        return False

    def play_url(self, url: str) -> bool:
        return self.load(url)

    def pause(self) -> None:
        self.set_property("pause", True)

    def resume(self) -> None:
        self.set_property("pause", False)

    def stop(self) -> None:
        self._cmd("stop")

    def seek(self, seconds: float) -> None:
        self._cmd("seek", seconds, "absolute")

    def volume(self, pct: int) -> None:
        self.set_property("volume", max(0, min(130, pct)))

    def progress(self) -> tuple[float, float]:
        pos = self.get_property("time-pos") or 0
        dur = self.get_property("duration") or 0
        try:
            pos, dur = float(pos), float(dur)
        except Exception:
            return 0.0, 0.0
        if pos > 0.5:
            self._played = True     # it is audible, so silence later means "finished"
        return pos, dur

    # a file that has not reported a position by now is not going to: the resolver
    # already ran, mpv had 8 s to start it, and a stream that stalls forever is what
    # "auto-advance does nothing" looks like from the couch
    NEVER_STARTED_SECONDS = 25.0

    def finished(self) -> bool:
        """
        Whether the file that is loaded is over.

        Three signals, each needing *positive* confirmation, because one of them
        always fails somewhere: `eof-reached` is the honest answer but only survives
        without `--keep-open`; `time-pos` against `duration` works for anything that
        reports a length; and "we heard it, and now mpv is idle again" is what an
        older mpv (or one with the flag overridden) leaves behind at EOF. A property
        read that fails returns None, not True, so a busy socket or a dead mpv cannot
        be misread as "the song ended" - except a *dead* mpv, which is: waiting for a
        player that is not running is how the DJ used to hang between tracks.
        """
        if not self.alive():
            return True
        if self.get_property("eof-reached") is True:
            return True
        pos, dur = self.progress()
        if dur and pos >= dur - 1.5:
            return True
        if not pos and not dur:
            if self._played and self.get_property("idle-active") is True:
                return True                         # unloaded at EOF
            if not self._played:
                return time.time() - self._load_at > self.NEVER_STARTED_SECONDS
        return False

    def is_playing(self) -> bool:
        if not self.alive():
            return False
        if self.get_property("pause") is not False:
            return False
        # with --keep-open the file stays loaded at EOF and pause flips on there, so
        # "loaded and unpaused" is only "playing" if the end has not been reached
        return self.get_property("eof-reached") is not True

    def eof(self) -> bool:
        return self.get_property("eof-reached") is True

    def quit(self) -> None:
        if self.alive():
            self._cmd("quit")
        try:
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        try:
            self.sock_path.unlink()
        except Exception:
            pass
        try:
            self.log.close()
        except Exception:
            pass


def open_externally(url: str) -> bool:
    """Last-resort handoff: let the OS/Spotube's browser path deal with it."""
    candidates = []
    if os.environ.get("FLATPAK_ID"):
        candidates.append(["flatpak", "run", "--command=sh", "com.github.KRTirtho.Spotube", "-c",
                           f"xdg-open {url!r}"])
    if shutil.which("xdg-open"):
        candidates.append(["xdg-open", url])
    if sys.platform == "darwin":
        candidates.append(["open", url])
    if os.name == "nt":
        candidates.append(["cmd", "/c", "start", "", url])
    for c in candidates:
        try:
            subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def playerctl(action: str) -> bool:
    """Drive any MPRIS player - Spotube registers one via audio_service_mpris."""
    exe = shutil.which("playerctl")
    if not exe:
        return False
    player = os.environ.get("SPOTUBE_DJ_MPRIS_PLAYER", "spotube")
    try:
        subprocess.run([exe, "-p", player, action], capture_output=True, timeout=6)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    print("mpv available:", HAS_MPV)
    print("playerctl available:", shutil.which("playerctl") is not None)
