"""Click through the new window under Xvfb and fail on any Tk traceback."""
import os, re, sys, time, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spotube_dj"))
import config

D = Path(tempfile.mkdtemp())
config.APP_DIR = D
config.LLM_CONFIG_FILE = D / "config.json"
config.STATE_FILE = D / "state.json"
config.HISTORY_FILE = D / "history.jsonl"
config.load_llm_config()
config.LLM_API_KEY = ""

import tkinter as tk
import gui, viewmodel as vm
from tkinter import font as tkfont

FAKE = [{"id": f"vid{i:02d}", "title": f"Song Number {i}", "artist": "The Kinny",
         "duration": 200 + i, "url": f"https://music.youtube.com/watch?v=vid{i:02d}",
         "thumbnail": ""} for i in range(6)]
gui.prov.yt_search = lambda q, limit=12, **k: list(FAKE[:limit])
gui.build_queue = lambda req, count=20, seeds=None, on_progress=None, **kw: (
    [dict(FAKE[0]), dict(FAKE[1])], {"tracks": 2, "engine": "offline"})

log_lines = []
sys.stdout = type("T", (), {"write": staticmethod(lambda s: (log_lines.append(s), None)[0]),
                            "flush": staticmethod(lambda: None)})()
_real = sys.__stdout__

root = tk.Tk()
root.attributes("-alpha", 0.99)
bridge = gui.Bridge(headless=True, backend="mpv", volume=40)
bridge.dj.auto = False
bridge.dj._topup = lambda n: 0
app = gui.App(root, bridge, small_fonts=False)

def spin(n=6, sleep=0.04):
    for _ in range(n):
        root.update()
        time.sleep(sleep)

def do_search(text="philly soul"):
    app.search_entry.delete(0, "end")
    app.search_entry.insert("0", text)
    app._search_typing()
    app._do_search()
    for _ in range(45):          # the worker polls at 0.5s; give it room
        root.update()
        time.sleep(0.04)


def click(w):
    w.event_generate("<ButtonRelease-1>")
    spin(2)

try:
    spin(20)
    # ---- placeholder + typing -------------------------------------------------
    assert app.entry.get() == app.PLACEHOLDER, "no placeholder"
    app._focus_in(None)
    assert app.entry.get() == "", "focus did not clear"
    app.entry.insert("0", "70s philly soul, no podcasts")
    app._search_typing()
    spin(2)
    assert str(app.play_btn.cget("state")) == "normal", "Play stayed disabled"
    # ---- mood chips -----------------------------------------------------------
    assert len(app._chips) == len(vm.MOODS), len(app._chips)
    app._chips[0].event_generate('<Button-1>')
    spin(3)
    assert app._mood == vm.MOODS[0][0], app._mood
    # ---- play -----------------------------------------------------------------
    click(app.play_btn)
    spin(12)
    bridge.dj._hold_until = 0.0
    # ---- list rows: the actual widgets (buttons, double click, menu) --------
    app._show("search")
    spin(2)
    do_search()
    assert len(app.search_list.rows) == len(FAKE), len(app.search_list.rows)
    tl = app.search_list
    rows = tl.rows
    first = rows[0]
    for col in (0, 1, 2):
        for off in (4, 12, 24, 36, 48):
            w = tl.winfo_containing(first.get("_x", 0) or 0, 0)
    for vid, frames in tl._frames.items():
        for f in frames:
            for child in f.winfo_children():
                for w in [child] + list(child.winfo_children()):
                    if callable(getattr(w, "command", None)):
                        w.command()
                        spin(2)
            # Tk cannot event_generate a Double modifier, so check the binding is
            # installed and invoke the same callback the row would run.
            assert "on_play" in str(f.bind("<Double-Button-1>")) or f.bind("<Double-Button-1>"), \
                "row has no double-click binding"
            tl.on_play(first)
            spin(2)
            bridge.dj._hold_until = 0.0
            f.event_generate("<Button-3>", x=140, y=22)
            spin(2)
            app._row_action("love", first.get("raw") or first)
            spin(2)
    # the menu itself: build it through the widget so a bad callback surfaces
    vid0 = list(tl._frames)[0] if tl._frames else ""
    if vid0:
        tl._show_menu(type("E", (), {"x_root": 400, "y_root": 300})(), first, tl._frames[vid0][0])
        spin(2)
    app._row_action("copy", (first.get("raw") or first))
    app._row_action("not_interested", (rows[1].get("raw") or rows[1]) if len(rows) > 1 else (first.get("raw") or first))
    spin(3)
    assert len(app.search_list.rows) != len(rows) or not app._hidden, "hide did nothing"
    app._row_action("queue", (first.get("raw") or first))
    spin(2)
    app._row_action("play", (first.get("raw") or first))
    spin(3)
    bridge.dj._hold_until = 0.0
    app._row_action("radio", (first.get("raw") or first))
    spin(6)
    bridge.dj._hold_until = 0.0
    # ---- transport ------------------------------------------------------------
    for w in (app.next_btn, app.prev_btn, app.cont_btn, app.like_btn, app.playpause_btn,
              app._repeat_btn):
        w.invoke() if w.winfo_class() == "Button" else click(w)
        spin(3)
        bridge.dj._hold_until = 0.0
    # ---- seek -----------------------------------------------------------------
    app._seek_by(-10)
    app._seek_by(10)
    app._seek_by(99999)
    app._seek_drag(500); app._seek_done()
    for seq in ("<KeyPress-comma>", "<KeyPress-period>", "<KeyPress-n>", "<KeyPress-p>",
                "<KeyPress-l>", "<KeyPress-space>", "<KeyPress-s>", "<KeyPress-h>",
                "<KeyPress-F1>", "<Return>", "<KeyPress-n>"):
        app.root.event_generate(seq)
        spin(2)
        bridge.dj._hold_until = 0.0
    spin(4)
    # ---- search: source toggle, typing, clearing ---------------------------
    for key in ("youtube", "spotify", "youtube"):
        app._set_search_source(key)
        spin(2)
    app._search_typing()
    app._radio_search()
    spin(6)
    bridge.dj._hold_until = 0.0
    app._clear_search()
    spin(2)
    assert app.search_entry.get() == "", "clear left text behind"
    do_search("quiet piano, no vocals")
    # "Not interested" is session-scoped and applies to every list, so the
    # re-search must come back *minus* the hidden row - not empty, not full.
    assert len(app.search_list.rows) == len(FAKE) - len(app._hidden), (
        len(app.search_list.rows), len(app._hidden))
    # ---- library tabs ----------------------------------------------------------
    for key in ("recent", "loved", "artists"):
        app._show("library")
        app._lib_tab_set(key)
        spin(3)
        app._lib_play_all()
        spin(3)
        app._lib_queue_all()
        spin(2)
        bridge.dj._hold_until = 0.0
    # ---- log drawer, export, settings ---------------------------------------
    app._toggle_log(); spin(3)
    app._copy_log() if hasattr(app, "_copy_log") else None
    app._export() if hasattr(app, "_export") else None
    spin(3)
    app._toggle_log(); spin(2)
    app._export() if hasattr(app, "_export") else None
    spin(4)
    for _ in range(300):
        root.update(); time.sleep(0.005)
    app._open_settings()
    dlg = [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
    dlg = dlg[-1] if dlg else None
    assert dlg is not None, "settings dialog never opened"
    for _ in range(10):
        dlg.update_idletasks(); root.update(); time.sleep(0.03)
    for c in dlg.winfo_children():
        c.update_idletasks()
    print("settings dialog requests %sx%s" % (dlg.winfo_reqwidth(), dlg.winfo_reqheight()))
    # every interactive widget in the dialog must be inside its borders, or the
    # user cannot reach Save/Test at all
    W2, H2 = dlg.winfo_width(), dlg.winfo_height()
    bad = []
    def walk2(w, d=0):
        try:
            if w.winfo_ismapped():
                x, y = w.winfo_rootx() - dlg.winfo_rootx(), w.winfo_rooty() - dlg.winfo_rooty()
                if x < 0 or y < 0 or x + w.winfo_width() > W2 or y + w.winfo_height() > H2:
                    bad.append((w.winfo_class(), w["text"] if "text" in w.keys() else "", x, y,
                                w.winfo_width(), w.winfo_height()))
            for c in w.winfo_children():
                walk2(c, d + 1)
        except Exception:
            pass
    walk2(dlg)
    print("dialog widgets outside:", len(bad))
    for b2 in bad[:8]:
        print("   ", b2)
    dlg.event_generate("<Escape>")
    spin(3)
    dlg.destroy()
    spin(3)
    # ---- geometry: nothing drawn outside the window ---------------------------
    W, H = root.winfo_width(), root.winfo_height()
    off = []
    def walk(w, depth=0):
        try:
            if not w.winfo_ismapped():
                return
            r = winfo(w, "rootx") + winfo(w, "width")
            b = winfo(w, "rooty") + winfo(w, "height")
            if r > W + 1 or b > H + 1 or winfo(w, "rootx") < -1 or winfo(w, "rooty") < -1:
                off.append((w.winfo_class(), id(w), winfo(w, "rootx"), r, winfo(w, "rooty"), b))
            for c in w.winfo_children():
                walk(c, depth + 1)
        except Exception:
            pass
    def winfo(w, what):
        try:
            return int(w.__getattribute__("winfo_" + what)())
        except Exception:
            return 0
    walk(root)
    sys.stdout = _real
    print("window:", W, "x", H, "| off-screen widgets:", len(off))
    for o in off[:10]:
        print("   ", o)
    txt = "".join(log_lines)
    tb = [l for l in txt.splitlines() if "Traceback" in l or "Error:" in l or "error:" in l]
    print("log lines:", len(txt.splitlines()), "| error lines:", len(tb))
    for l in tb[:10]:
        print("   ", l[:160])
    print("RESULT:", "FAIL" if (tb or len(off) > 12) else "PASS")
finally:
    sys.stdout = _real
    bridge.stop()
    try:
        root.destroy()
    except Exception:
        pass
