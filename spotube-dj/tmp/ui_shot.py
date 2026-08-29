"""Real engine, real searches, real art: the three README screenshots."""
import os, sys, time, tempfile, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "spotube_dj"))
import config
D = Path(tempfile.mkdtemp()); config.APP_DIR = D
config.LLM_CONFIG_FILE = D / "config.json"; config.STATE_FILE = D / "state.json"
config.HISTORY_FILE = D / "history.jsonl"
config.load_llm_config(); config.LLM_API_KEY = ""      # offline planner, as shipped
import tkinter as tk, gui

# a real mpv on the null audio driver: this is not a staged screenshot, the
# player is genuinely playing (no sound) so the progress bar and the marked row
# come from the same code path a listener sees
os.environ.setdefault("SPOTUBE_DJ_AO", "null")
bridge = gui.Bridge(headless=False, backend="mpv", volume=45)
bridge.dj._topup = lambda n=0: 0        # one build is enough for a screenshot
root = tk.Tk()
app = gui.App(root, bridge)

def spin(n=8, s=0.05):
    for _ in range(n):
        root.update(); time.sleep(s)

def settle(pred, secs=150):
    t0 = time.time()
    while time.time() - t0 < secs:
        spin(4, 0.05)
        if pred():
            return True
    return False

def shot(name, extra=6):
    for _ in range(extra):
        root.update_idletasks(); root.update(); time.sleep(0.04)
    subprocess.run(["import", "-window", "root", str(DOOM / name)], capture_output=True)
    print("wrote", name)

DOOM = ROOT / "docs"
DOOM.mkdir(exist_ok=True)
try:
    # 1. a real mix, built by the offline planner
    app._show("home"); spin(10)
    app._use_mood("Chill evening", "warm chill soul, no podcasts, no live versions")
    ok = settle(lambda: len(bridge.dj.queue) > 3)
    print("queue built:", ok, "len:", len(bridge.dj.queue))
    spin(20, 0.05)
    app._redraw(app.recent_list) if hasattr(app, "_redraw") else None
    shot("screenshot.png")
    print("now playing:", (bridge.dj.current or {}).get("title"))
    # 2. real search results, with the art loader given time to land
    app.search_entry.delete(0, "end")
    app.search_entry.insert("0", "tame impala")
    app._do_search()
    got = settle(lambda: len(app.search_list.rows) > 2, 120)
    print("search rows:", len(app.search_list.rows), "ok:", got)
    spin(30, 0.05)                      # let art arrive and repaint
    shot("screenshot-search.png")
    # 3. library, with the rows that just played
    for _ in range(2):                 # skip a couple so history has real rows
        bridge.submit("skip"); spin(20, 0.1)
        bridge.dj._hold_until = 0.0
    app._lib_tab_set("recent"); app._show("library")
    settle(lambda: len(app.lib_list.rows) > 0, 20)
    shot("screenshot-library.png", extra=10)
    print("lib rows:", len(app.lib_list.rows), "| hidden:", len(app._hidden))
    print("log tail:", " | ".join(app.activity[-3:]))
finally:
    bridge.stop()
    try:
        root.destroy()
    except Exception:
        pass
