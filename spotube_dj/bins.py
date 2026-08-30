"""
bins.py - find the helper programs, including when the PATH you get is a lie.

A GUI started from the app menu does not inherit your shell's PATH: it gets the
systemd/user-session one, which on most machines is just /usr/local/bin:/usr/bin:/bin.
So `shutil.which("ffmpeg")` returns None for the person who installed ffmpeg with
snap, brew on Apple Silicon, or into ~/.local/bin - and until now that silently
meant "no cover art" and "no mpv", with nothing said about it anywhere.

Resolution order for every binary:
    1. SPOTUBE_DJ_<NAME> environment override (an absolute path, for odd setups)
    2. the normal PATH
    3. the usual extra places a desktop launch cannot see
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

EXTRA_DIRS = (
    "~/.local/bin", "~/bin", "/snap/bin", "/usr/local/bin", "/opt/homebrew/bin",
    "/usr/bin", "/home/linuxbrew/.linuxbrew/bin", "~/.cargo/bin", "~/miniconda3/bin",
    "~/anaconda3/bin",
)

def _candidates(name: str):
    for d in EXTRA_DIRS:
        p = Path(os.path.expanduser(d)) / name
        yield p


_found: dict = {}


def find(name: str) -> str | None:
    """-> absolute path to `name`, or None. Cached: the art path calls it a lot."""
    if name in _found:
        return _found[name]
    hit = _find(name)
    _found[name] = hit
    return hit


def reset() -> None:
    """Forget the cache (tests, or after installing something mid-session)."""
    _found.clear()


def _find(name: str) -> str | None:
    env = os.environ.get(f"SPOTUBE_DJ_{name.upper()}")
    if env:
        p = Path(os.path.expanduser(env))
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    hit = shutil.which(name)
    if hit:
        return hit
    for p in _candidates(name):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def have(name: str) -> bool:
    return find(name) is not None


def describe() -> dict:
    """What the doctor prints: name -> path or None."""
    return {n: find(n) for n in ("mpv", "ffmpeg", "ffprobe", "playerctl")}
