"""
Test bootstrap: put the app modules on sys.path for any runner, and point the
app's whole state directory at a scratch dir.

That second half matters. config.py reads SPOTUBE_DJ_HOME at import time and
every module below it caches the resulting paths, so a test that plays a fake
track used to append "t0/t1" rows to the *real* ~/.spotube-dj/history.jsonl -
which is the user's Library, and the taste profile is built from it. Tests must
not be able to reach a person's data, so the env var is set before anything is
importable, and no test has to remember to patch.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "spotube_dj"
for _p in (str(PKG), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_HOME = Path(tempfile.mkdtemp(prefix="spotube-dj-tests-"))
os.environ["SPOTUBE_DJ_HOME"] = str(_HOME)

# And no test may reach the network. providers.yt_search tries YouTube Music's own
# search endpoint first (it is fast and answers with music only), which would
# otherwise mean 266 tests firing real HTTP and asserting on whatever YouTube
# happened to rank that day. Tests that stub providers._run get the yt-dlp path;
# tests for the InnerTube path patch providers._http_json to a saved fixture.
os.environ.setdefault("SPOTUBE_DJ_YTM", "off")

# Same reasoning for the audio cache: DJ.next() prefetches the next tracks, and a
# test that plays a fake track must not be able to start a real yt-dlp download.
# audiocache's own tests below patch enabled() where they need it on.
os.environ.setdefault("SPOTUBE_DJ_CACHE", "off")
TEST_HOME = _HOME
