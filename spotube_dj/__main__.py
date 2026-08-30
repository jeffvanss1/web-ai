#!/usr/bin/env python3
"""
spotube-dj - an AI DJ that works with Spotify FREE (no Premium, no /me/player).

  python3 -m spotube_dj "dark techno for late night coding"     # play locally
  python3 -m spotube_dj "indie pop" --export --to-spotube        # hand to Spotube
  python3 -m spotube_dj "lofi" --daemon                           # control API + auto-DJ
  python3 -m spotube_dj next | like | status | taste | sync | doctor
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import providers as prov
import taste
from dj import DJ, build_queue

CTRL_PORT_DEFAULT = 8765
ENVFILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env() -> None:
    """Pick up SPOTIPY_* / GEMINI_API_KEY from ./.env if the user keeps one there."""
    if not ENVFILE.exists():
        return
    for ln in ENVFILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------- remote ctl
def _ctrl(port: int, method: str, path: str) -> dict | None:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[error] nothing answering on 127.0.0.1:{port} ({e.__class__.__name__}).\n"
              f"        start the DJ first:  python3 -m spotube_dj \"<request>\" --daemon")
        return None


def _mmss(s: float) -> str:
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


def _bar(pos: float, dur: float, width: int = 22) -> str:
    if not dur:
        return "-" * width
    fill = int(width * max(0.0, min(1.0, pos / dur)))
    return "#" * fill + "." * (width - fill)


def cmd_status(a) -> int:
    st = _ctrl(a.port, "GET", "/status")
    if not st:
        return 1
    np = st.get("now_playing") or {}
    print(f"request   : {st.get('request') or '-'}")
    print(f"engine    : {st.get('engine')}   backend: {st.get('backend')}   "
          f"{'PAUSED' if st.get('paused') else 'playing'}")
    print(f"now       : {np.get('artist', '?')} - {np.get('title', '?')}")
    pos, dur = st.get("position", 0), st.get("duration", 0)
    if dur:
        print(f"progress  : {_bar(pos, dur)} {_mmss(pos)}/{_mmss(dur)}")
    print(f"queued    : {st.get('queued')} tracks left")
    for i, t in enumerate(st.get("up_next", [])[:5], 1):
        print(f"  {i}. {str(t.get('artist', '?'))[:20]:20} {str(t.get('title', ''))[:52]}")
    qs = st.get("queries") or []
    if qs:
        print(f"queries   : {', '.join(qs[:6])}")
    return 0


def cmd_action(a, what: str) -> int:
    r = _ctrl(a.port, "POST", f"/control?action={what}")
    if not r:
        return 1
    if what in ("next", "skip", "prev"):
        np = r.get("now_playing") or {}
        print(f"-> {np.get('artist', '?')} - {np.get('title', '?')}")
    else:
        print(f"{what} ok (queued={r.get('queued')})")
    return 0


def cmd_retarget(a) -> int:
    q = urllib.parse.quote(a.request if isinstance(a.request, str)
                           else " ".join(a.request or []))
    r = _ctrl(a.port, "POST", f"/request?q={q}&count={a.count}")
    if not r:
        return 1
    print(f"ok={r.get('ok')}")
    print(json.dumps(r.get("info", {}), ensure_ascii=False, indent=2)[:800])
    return 0


def _brain_note(info: dict) -> None:
    """Say out loud when the LLM was skipped - silence here is what made a dead
    key look like 'the AI times out'. Goes to stderr so --json stays parseable."""
    for n in (info or {}).get("llm_notes") or []:
        print(f"[brain] {n}", file=sys.stderr)
    err = (info or {}).get("llm_error")
    if err:
        print(f"[warn] brain not used: {err}", file=sys.stderr)
        print("[warn] the offline parser queued this instead; "
              "check with: --test-brain", file=sys.stderr)


def cmd_list(a) -> int:
    """No daemon needed: show what the DJ would queue right now."""
    tracks, info = build_queue(a.request or "", seed_refs=[a.playlist] if a.playlist else None,
                               count=a.count, extra_queries=a.query)
    for i, t in enumerate(tracks, 1):
        print(f"{i:2}. {str(t.get('artist', '?'))[:22]:22} {str(t['title'])[:54]:54} "
              f"{t.get('score', 0):+5.1f} {t['url']}")
    print(f"\nengine={info.get('engine')} candidates={info.get('candidates')} "
          f"spotify={info.get('spotify')}")
    if info.get("vibe"):
        print(f"mix: {info['vibe']}")   # Daylist-style name, e.g. "lofi tuesday night"
    print(f"why: {info.get('why') or '-'}")
    _brain_note(info)
    return 0


# ----------------------------------------------------------------- spotube
def _launch_spotube(playlist: Path) -> None:
    if shutil.which("spotube"):
        subprocess.Popen(["spotube", str(playlist)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        print("launched Spotube")
        return
    if shutil.which("flatpak"):
        subprocess.Popen(["flatpak", "run", "com.github.KRTirtho.Spotube", f"--file={playlist}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("launched Spotube (flatpak)")
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Spotube", str(playlist)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        print("asked macOS to open Spotube")
        return
    print("Spotube not found on PATH - open the file above manually (Settings -> Local files, or double-click).")


def cmd_export(a) -> int:
    if a.playlist:
        sp = prov.Spotify()
        seeds = sp.playlist_seed(a.playlist)
        if not seeds:
            print(f"[warn] Spotify returned no items for that playlist"
                  f"{' - ' + sp.reason_unavailable if sp.reason_unavailable else ''}")
            print("       (since Feb 2026 items are only readable for playlists you own/collaborate on)")
        tracks = []
        print(f"mapping {len(seeds)} Spotify tracks -> YouTube Music ...")
        for s in seeds[: a.count]:
            m = prov.yt_search(f"{s['artist']} {s['title']} audio", limit=1, max_dur=1500)
            if m:
                m[0]["spotify_uri"] = s.get("uri", "")
                tracks.append(m[0])
        info = {"engine": "spotify-playlist", "queries": [a.playlist], "candidates": len(tracks)}
    else:
        tracks, info = build_queue(a.request or "", seed_refs=[a.playlist] if a.playlist else None,
                                   count=a.count, extra_queries=a.query)
    if not tracks:
        print("nothing to export")
        return 1
    out = Path(a.out) if a.out else config.M3U_OUT
    if a.streams:
        print("resolving direct stream URLs (players that can't speak YouTube need these) ...")
        for t in tracks:
            s = prov.yt_stream_url(t["id"])
            if s:
                t["stream"] = s
    p = prov.write_m3u(tracks, out, title=(a.request or "Spotube DJ") if isinstance(a.request, str) else "Spotube DJ")
    print(f"wrote {len(tracks)} tracks -> {p}")
    print(f"engine: {info.get('engine')}  queries: {', '.join(info.get('queries', [])[:5])}")
    _brain_note(info)
    if a.streams:
        print("note: signed stream URLs expire after a few hours - re-export if stale.")
    if a.open_spotube:
        _launch_spotube(p)
    return 0


# --------------------------------------------------------------------- taste
def cmd_taste(a) -> int:
    if getattr(a, "restore_taste", False):
        back = taste.restore()
        print("taste profile restored: " + ", ".join(f"{v} {k}" for k, v in back.items()
                                                    if v)
              if back else "nothing saved to bring back (only the last wipe is kept)")
        return 0
    if getattr(a, "clear", False):
        gone = taste.clear()
        if not any(gone.values()):
            print("there was nothing to clear")
            return 0
        detail = ", ".join(f"{v} {k}" for k, v in gone.items() if v)
        print(f"taste profile cleared: {detail}"
              "   (`spotube-dj taste restore` brings it back)")
        return 0
    print(taste.summarize())
    return 0


def cmd_cache(a) -> int:
    """Report (or empty) the downloaded-audio cache."""
    import audiocache
    if getattr(a, "clear", False):
        n = audiocache.clear()
        print(f"removed {n} cached track(s) from {audiocache.cache_dir()}")
        return 0
    s = audiocache.stats()
    print(f"audio cache: {s['dir']}")
    print(f"  enabled   : {audiocache.enabled()} "
          f"({'yt-dlp found' if audiocache.enabled() else 'needs yt-dlp, or SPOTUBE_DJ_CACHE=off'})")
    print(f"  files     : {s['files']}  (cap {s['cap_mb']} MB, oldest pruned)")
    print(f"  fetched   : {s['stored']} ahead of playback, {s['failed']} failed")
    print(f"  instant   : {s['hit']} tracks started straight from disk")
    print(f"  in flight : {s['pending']}")
    return 0


def cmd_why(a) -> int:
    """
    Show what the search surface and the filter actually decided, for one
    request. This is the tool for "why did it play a fight scene / a rain
    loop": it prints every candidate the search returned - kept *and* refused -
    with the rule that fired, so the answer is never a guess.

    It runs the same queries the queue would run, so what you see here is what
    a play request does rather than a paraphrase of it.
    """
    import brain
    import filters
    import providers as prov

    seen: list = []
    rows: list = []
    # the same trimming build_queue applies, or this audit describes a search
    # nobody actually issued
    queries = list(dict.fromkeys(
        q for q in (brain.search_query(x) for x in
                    (brain.plan(a.why, seeds=None).get("queries") or []))
        if q and len(q) > 2))[:4] or [a.why]
    for q in queries:
        got = prov.yt_search(q, limit=max(8, a.count or 8), verdicts=seen)
        print(f"search {q!r} -> {len(got)} of {len(seen)} kept")
        rows.extend(got)
    kept = {id(v.get("entry")) for v in seen if v.get("kind") == filters.TRACK}
    for v in seen:
        e = v.get("entry") or {}
        mark = "ok  " if id(e) in kept else "no  "
        print(f"  {mark}[{v['kind']:10}] {str(e.get('title') or '')[:58]}")
        print(f"          {str(e.get('artist') or '')[:40]:40} "
              f"{int(e.get('duration') or 0):>5}s  {' / '.join(v.get('reasons') or ['-'])}")
    if not seen:
        print("  (the search returned nothing at all - blocked, offline, or "
              "yt-dlp needs updating)")
    elif not rows:
        print("  (nothing survived as a 3-8 minute track; see the refusals above)")
    return 0
def cmd_sync(a) -> int:
    """Learn from what you already listen to (Spotify FREE endpoints only)."""
    sp = prov.Spotify()
    if not prov.CLIENT_ID or not prov.CLIENT_SECRET:
        print("[error] needs SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET (env or ./.env).")
        print("        Metadata-only: this never touches /me/player, so free is fine.")
        return 1
    if not (sp.login() or sp.client_token()):
        print(f"[error] Spotify auth failed: {sp.reason_unavailable}")
        print("        If your app was created after 2026-02-11 you need Premium just to use")
        print("        Dev Mode. In that case skip sync - the DJ still works off YouTube Music.")
        return 1
    added = 0
    for t in sp.liked(limit=50) + sp.recently_played(limit=25):
        taste.record_like({"title": t["title"], "artist": t["artist"],
                           "duration": t.get("duration", 0)})
        added += 1
    for i, name in enumerate(sp.top_artists(limit=15)):
        taste._bump((st := config.load_state())["artists"], name, 3.0 - i * 0.12)
        config.save_state(st)
    print(f"learned from {added} saved/recent tracks + {len(sp.top_artists(limit=15))} top artists")
    print(taste.summarize())
    return 0


def cmd_web(a, req: str) -> int:
    """
    The player: the same DJ object, served on 127.0.0.1 and opened in a browser.

    This is the front end now. It owns the DJ the same way the removed Tk window
    did - one process, one queue, one taste profile - and adds the things a browser
    gives for free: vector icons, a real font stack, hover states, and a cover you
    can put behind the window and blur.
    """
    import web as web_mod
    dj = DJ(backend=a.backend, volume=a.volume, headless=a.headless)
    dj.serve(a.port)                      # the control API the CLI verbs talk to
    return web_mod.serve(dj, host=a.web_host, port=a.web_port or web_mod.DEFAULT_PORT,
                         request=req, count=a.count,
                         playlist=a.playlist or "",
                         search_for=getattr(a, "search", "") or "",
                         open_browser=not a.no_browser)


# -------------------------------------------------------------------- doctor
def cmd_doctor(a) -> int:  # noqa: C901  (a checklist reads better than helpers)
    _load_env()
    print("spotube-dj doctor")
    print("=" * 60)
    checks: list[tuple[str, bool, str]] = []

    checks.append(("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
    ver = ""
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, text=True, timeout=40)
        ok = r.returncode == 0
        ver = r.stdout.strip()
    except Exception:
        ok = False
    checks.append(("yt-dlp  (the audio source)", ok, ver or "missing: pip install -U yt-dlp"))

    # bins.find, not shutil.which: launched from the app menu the PATH is not your
    # shell's, and "ffmpeg missing" used to be reported for a machine that had it
    # in ~/.local/bin - which is also why cover art showed nothing.
    import bins
    for name, have, hint in (
        ("mpv  (local playback)", bins.find("mpv"), "optional - else use --backend spotube"),
        ("playerctl  (drives Spotube MPRIS)", bins.find("playerctl"),
         "optional - lets 'next'/'prev' hit Spotube itself"),
        ("ffmpeg  (art scaling / stream merge)", bins.find("ffmpeg"),
         "optional - the page reads PNG/GIF/JPEG itself; without it art is not resized"),
    ):
        checks.append((name, bool(have), have or hint))

    # Distinguish "YouTube is unreachable" from "YouTube answered but every
    # hit was a 6-hour mix we deliberately drop" - very different fixes.
    try:
        raw = prov.yt_search("never gonna give you up rick astley", limit=5,
                             min_dur=0, max_dur=999999)
        tidy = prov.yt_search("never gonna give you up rick astley", limit=5)
        if not raw:
            checks.append(("YouTube Music search", False,
                           "0 results - network/region/PO block, or yt-dlp needs updating"))
        else:
            how = ("YouTube Music catalog" if (raw[0].get("endpoint") or "") == "music-search"
                   else "plain YouTube + filter")
            checks.append(("YouTube Music search", True,
                           f"{len(raw)} raw hits, {len(tidy)} kept as songs, via {how}, "
                           f"e.g. {raw[0]['title'][:24]}"))
    except Exception as e:
        checks.append(("YouTube Music search", False, e.__class__.__name__))

    try:
        s = prov.yt_search("never gonna give you up", limit=1, min_dur=0, max_dur=999999)
        url = prov.yt_stream_url(s[0]["id"]) if s else None
        checks.append(("stream URL resolution", bool(url),
                       "signed googlevideo URL ok" if url else
                       "blocked - retry, or export --streams and play in Spotube/mpv"))
    except Exception as e:
        checks.append(("stream URL resolution", False, e.__class__.__name__))

    # ---- the brain itself. This is what "engine: offline" used to hide.
    import brain as _brain
    _brain.config.apply_llm_overrides()
    eng = _brain.configured_engine()
    if eng == "offline":
        checks.append(("AI brain", True, "offline parser (no key) - works, less clever"))
    else:
        r = _brain.probe()
        checks.append((f"AI brain ({eng})", r["ok"], r["detail"][:70]))

    cid = prov.CLIENT_ID
    if not cid:
        checks.append(("Spotify app (OPTIONAL metadata)", True,
                       "not configured - the DJ works fully without it"))
    else:
        sp = prov.Spotify()
        ok = sp.client_token()
        checks.append(("Spotify Web API", ok, "client-credentials ok" if ok else
                       f"{sp.reason_unavailable or 'blocked'}"))
        if ok:
            me = sp.me()
            checks.append(("Spotify user OAuth", bool(me),
                           f"as {me['display_name']}" if me and me.get("display_name") else
                           "run: python3 -m spotube_dj sync"))

    print()
    try:
        import desktop
        tgt = desktop.launcher_path()
        if not tgt.exists():
            checks.append(("desktop launcher (the GUI in your app menu)", False,
                           "optional: python3 -m spotube_dj --install-desktop"))
        else:
            # not "the file is there" - that was the useless answer. Does the
            # interpreter the launcher points at actually have this package and
            # a Tk to draw with? That is what "it shows but nothing comes up"
            # turns out to be.
            ok, why = desktop.self_test()
            checks.append(("desktop launcher launches (self-test)", ok,
                           "; ".join(why) if why else str(tgt)))
    except Exception as e:
        checks.append(("desktop launcher", False, f"could not look: {e}"))

    try:
        import covers
        checks.append(covers.doctor_check())
    except Exception as e:
        checks.append(("cover art", False, f"could not look: {e}"))

    # the browser skin has no dependencies to check, so this line answers the
    # question a user actually has: "will --web work on this machine at all"
    try:
        import web as web_mod
        checks.append(web_mod.doctor_line())
    except Exception as e:
        checks.append(("web player", False,
                       f"could not build the page: {e.__class__.__name__}"))

    for line in _cache_doctor():
        checks.append(line)

    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else '--'}] {name:36} {str(detail)[:70]}")

    print("\nWhy this does not need Premium")
    print("-" * 60)
    print("  * /v1/me/player/* (play, pause, skip, volume, devices) is Premium-only:")
    print("    403 PREMIUM_REQUIRED. That is the exact wall schultz-dev0/SpotifyDJ hits.")
    print("  * Since 2026-02-11 NEW Dev Mode client ids also require the *owner* to have")
    print("    Premium, 1 client id, 5 users max - so the API itself is now premium-gated")
    print("    for fresh apps. Endpoint removals for EXISTING apps were postponed 2026-03-09.")
    print("  * Dropped: batch GET /tracks, /browse/new-releases, /browse/categories,")
    print("    /artists/{id}/top-tracks, GET /users/{id}, /markets; playlist 'tracks' ->")
    print("    'items' (own/collab only); search limit 50 -> 10 (25 from July 2026);")
    print("    track.popularity + user.product fields removed.")
    print("  * Kept here: audio = YouTube Music via yt-dlp; taste = local model; Spotify =")
    print("    metadata only (liked/recently-played/search), never a player call.")
    return 0


# -------------------------------------------------------------------- daemon
def cmd_daemon(a, req: str) -> int:
    dj = DJ(backend=a.backend, volume=a.volume)
    dj.serve(a.port)
    if req or a.playlist:
        dj.start(req, seed_refs=[a.playlist] if a.playlist else None,
                 count=a.count, extra_queries=a.query)
        _brain_note(dj.info)
    else:
        # "daemon up, now type something" made the thing look unfinished. With a
        # profile it can already be a DJ: like a few songs and it has a repertoire.
        r = dj.taste_mix(count=a.count)
        if not r.get("ok"):
            print("daemon up - " + str(r.get("reason") or "no mix yet")
                  + ". POST /request?q=... (or `spotube-dj mix`) to start music")
        else:
            print(f"daemon up - {len(r.get('tracks') or [])} tracks mixed from your likes; "
                  "POST /request?q=... to retarget")
    if a.headless:
        print("headless: queue + control API only, no audio")
    dj.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    One flat parser, no argparse subparsers.

    `request` is nargs='*', and argparse cannot mix a greedy positional with
    subparsers (the subparser action swallows the words and then errors on the
    first flag). So verbs are flags, and bare `next`/`like`/`status` words are
    translated to those flags in main() before parsing.
    """
    p = argparse.ArgumentParser(prog="spotube-dj", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("request", nargs="*", help='e.g. "90s trip hop, dark and slow"')
    p.add_argument("--playlist", help="Spotify playlist URL/id, or artist words to seed from")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--query", action="append", help="extra forced search query (repeatable)")
    p.add_argument("--backend", choices=["mpv", "spotube", "none"], default="mpv")
    p.add_argument("--volume", type=int, default=None)
    p.add_argument("--daemon", action="store_true", help="auto-DJ forever + control API")
    p.add_argument("--headless", action="store_true", help="no audio (testing / server)")
    p.add_argument("--port", type=int, default=CTRL_PORT_DEFAULT)
    p.add_argument("--list", action="store_true", help="just print the queue, don't play")
    p.add_argument("--export", action="store_true", help="write an m3u8 Spotube can open")
    p.add_argument("--to-spotube", dest="open_spotube", action="store_true",
                   help="write the m3u8 and open it in Spotube")
    p.add_argument("--streams", action="store_true", help="bake signed stream URLs into the m3u8")
    p.add_argument("--out", help="path for --export")
    p.add_argument("--json", action="store_true", help="dump plan+tracks as JSON")
    # --gui is a kept alias, not a kept feature: the Tk window is gone (see README),
    # and a launcher or a shell history entry that still passes it must open the
    # player rather than die on "unrecognized arguments"
    p.add_argument("--gui", action="store_true",
                   help="deprecated alias for --web (the Tk window was removed)")
    p.add_argument("--search", metavar="TEXT", default="",
                   help="with --web: run this search as soon as the page opens")
    p.add_argument("--web", action="store_true",
                   help="the browser player (this is the default front end)")
    p.add_argument("--web-port", dest="web_port", type=int, default=0,
                   help="with --web: port to serve on (default 8766)")
    p.add_argument("--web-host", dest="web_host", default="127.0.0.1",
                   help="with --web: interface to bind (default loopback only; "
                        "0.0.0.0 lets a phone on the same wifi drive the DJ)")
    p.add_argument("--no-browser", dest="no_browser", action="store_true",
                   help="with --web: print the URL instead of opening a browser")
    p.add_argument("--install-desktop", dest="install_desktop", action="store_true",
                   help="add the app to the desktop menu (writes under ~/.local, no sudo)")
    p.add_argument("--uninstall-desktop", dest="uninstall_desktop", action="store_true",
                   help="remove that launcher and icon")
    p.add_argument("--doctor", action="store_true", help="check deps/sources, then exit")
    p.add_argument("--cover", nargs="+", metavar="WORD",
                   help='fetch real release art: --cover "ARTIST" "TITLE" ["ALBUM"]')
    p.add_argument("--set-key", dest="set_key", metavar="KEY",
                   help="save a Gemini API key to ~/.spotube-dj/config.json")
    p.add_argument("--set-base", dest="set_base", metavar="URL",
                   help="save an OpenAI-compatible base URL (e.g. http://localhost:11434)")
    p.add_argument("--set-model", dest="set_model", metavar="NAME", help="save the model name")
    p.add_argument("--clear-key", action="store_true", help="delete the saved brain config")
    p.add_argument("--test-brain", action="store_true", help="ping the configured LLM once")
    p.add_argument("--taste", action="store_true", help="show the learned taste profile")
    p.add_argument("--cache", action="store_true",
                   help="show the audio cache (files, size, what was fetched ahead)")
    p.add_argument("--clear-cache", dest="clear_cache", action="store_true",
                   help="delete every downloaded track from the audio cache")
    p.add_argument("--no-cache", dest="no_cache", action="store_true",
                   help="never download ahead (same as SPOTUBE_DJ_CACHE=off)")
    p.add_argument("--why", metavar="TEXT",
                   help='audit the filter: run one search and print every hit with the '
                        'reason it was kept or refused')
    p.add_argument("--clear-taste", action="store_true", help="wipe the taste profile")
    p.add_argument("--restore-taste", action="store_true",
                   help="undo the last taste wipe (one snapshot is kept)")
    p.add_argument("--sync", action="store_true",
                   help="learn from Spotify liked/recent (free endpoints only)")
    for verb in _REMOTE_VERBS:
        p.add_argument(f"--{verb}", dest=f"verb_{verb}", action="store_true",
                       help=f"send '{verb}' to a running --daemon and exit")
    return p


_REMOTE_VERBS = ("next", "prev", "skip", "like", "pause", "resume", "status", "retarget",
                 "mix")


_LOCAL_VERBS = {"doctor": "--doctor", "taste": "--taste", "cache": "--cache",
                "sync": "--sync", "clear": "--clear-taste"}


def _preprocess(argv: list[str]) -> list[str]:
    """Allow the friendly verb words: `spotube-dj next` == `spotube-dj --next`."""
    if argv and argv[0] in _LOCAL_VERBS:
        flag = _LOCAL_VERBS[argv[0]]
        rest = [a for a in argv[1:] if not a.startswith("-")]
        if argv[0] == "taste" and rest and rest[0] in ("clear", "restore"):
            return ["--clear-taste" if rest[0] == "clear" else "--restore-taste"]
        return [flag] + argv[1:]
    if argv and argv[0] in _REMOTE_VERBS:
        verb = argv[0]
        rest = argv[1:]
        if verb == "retarget":
            return ["--retarget"] + (list(rest) if rest and not rest[0].startswith("-") else []) + \
                   [x for x in rest if x.startswith("-")]
        return [f"--{verb}"] + rest
    return argv


def _cache_doctor() -> list:
    try:
        import audiocache
    except Exception as e:
        return [("audio cache", False, f"could not look: {e}")]
    s = audiocache.stats()
    if not audiocache.enabled():
        return [("audio cache  (no waiting between tracks)", False,
                 "off - set SPOTUBE_DJ_CACHE=off, or install yt-dlp")]
    return [("audio cache  (no waiting between tracks)", True,
             f"{s['files']} files, {s['stored']} fetched ahead, "
             f"{s['hit']} instant starts, cap {s['cap_mb']} MB in {s['dir']}")]


def main(argv: list[str] | None = None) -> int:
    _load_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    a = build_parser().parse_args(_preprocess(argv))
    config.ensure_dirs()
    if getattr(a, "no_cache", False):
        os.environ["SPOTUBE_DJ_CACHE"] = "off"
    req = " ".join(a.request) if a.request else ""
    if req:
        a.request = req

    active_verb = next((v for v in _REMOTE_VERBS if getattr(a, f"verb_{v}", False)), None)

    if getattr(a, "clear_key", False):
        config.save_llm_config(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL="")
        print("cleared the saved brain config")
        import brain
        print(f"brain is now: {brain.configured_engine()}")
        return 0
    if a.set_key or a.set_base or a.set_model:
        vals = {}
        if a.set_key is not None:
            vals["LLM_API_KEY"] = a.set_key.strip()
        if a.set_base is not None:
            vals["LLM_BASE_URL"] = a.set_base.strip()
        if a.set_model is not None:
            vals["LLM_MODEL"] = a.set_model.strip()
        config.save_llm_config(**vals)
        print(f"saved to {config.LLM_CONFIG_FILE}: {', '.join(vals)}")
        import brain
        print(f"brain is now: {brain.configured_engine()}")
        return 0
    if a.test_brain:
        import brain
        config.apply_llm_overrides()
        r = brain.probe()
        print(f"engine: {r['engine']}   ok: {r['ok']}   {r['ms']}ms")
        print(f"        {r['detail']}")
        for n in r.get("notes") or []:
            print(f"        note: {n}")
        return 0 if r["ok"] else 1
    if a.doctor:
        return cmd_doctor(a)
    if a.taste or a.clear_taste or a.restore_taste:
        # the namespace is rebuilt because cmd_taste is also the `--taste` viewer;
        # a new flag that is not passed through here parses fine and then does
        # nothing at all, which is the worst kind of dead button
        return cmd_taste(argparse.Namespace(clear=a.clear_taste,
                                            restore_taste=a.restore_taste))
    if getattr(a, "cache", False):
        return cmd_cache(argparse.Namespace(clear=False))
    if getattr(a, "clear_cache", False):
        return cmd_cache(argparse.Namespace(clear=True))
    if getattr(a, "why", ""):
        return cmd_why(a)
    if a.sync:
        return cmd_sync(a)
    if active_verb:
        return cmd_status(a) if active_verb == "status" else cmd_action(a, active_verb)
    if a.install_desktop or a.uninstall_desktop:
        import desktop
        if a.uninstall_desktop:
            gone = desktop.remove()
            print("removed: " + (", ".join(str(x) for x in gone) if gone
                                 else "nothing was installed"))
            return 0
        made = desktop.install()
        print("launcher: " + str(desktop.launcher_path()))
        for x in made:
            print("  wrote " + str(x))
        ok, why = desktop.self_test()
        for line in why:
            print("  " + ("[ok] " if line.startswith("imports ok") else "[!!] ") + line)
        if not ok:
            print("The launcher is written, but it would not open a window as it is - "
                  "fix the line above, then re-run --install-desktop.")
        print("It appears in your app menu as \"Spotify DJ (free)\". Log out and in "
              "again if the shell has not noticed it yet.")
        return 0
    if a.cover:
        import covers
        import thumbs
        parts = [x for x in a.cover if x.strip()]
        artist = parts[0]
        title = parts[1] if len(parts) > 1 else ""
        album = parts[2] if len(parts) > 2 else ""
        print(f"cover art: {artist} - {album or title}")
        if not covers.enabled():
            print("  [!!] disabled: the Archive serves JPEG and the cache needs "
                  "something to decode it (ffmpeg or Pillow), or SPOTUBE_DJ_COVERS=off "
                  "is set")
            return 1
        mbid, flavour = covers.resolve_blocking(artist, title, album)
        why = covers.last_error()
        print(f"  {flavour} mbid: " + (mbid or (f"(no answer: {why})" if why
                                              else "(MusicBrainz has no album by that name)")))
        if not mbid:
            return 1
        url = covers.caa_url(mbid, "big", group=flavour == "group")
        print("  cover url   : " + url)
        path = thumbs.download_url(url, {"id": mbid, "artist": artist,
                                        "title": title}, "big")
        print(("  image       : " + str(path)) if path
              else "  image       : (the Archive had no front image at that size)")
        print("  stats       : " + str(covers.stats()))
        return 0 if path else 1

    if a.gui:
        print("[note] there is no Tk window any more - the browser player is the app, "
              "and --gui now means --web", file=sys.stderr)

    # Which surface answers. The text modes are the ones a person names on purpose
    # (`--list`, `--export`, `--json`, `--daemon`, `--headless`, `--cover`,
    # `--doctor`); anything else - a bare launch from a menu, or a request with no
    # mode at all - is the app, and opens the player on 127.0.0.1.
    terminal_mode = (a.list or a.export or a.open_spotube or a.json or a.daemon
                     or a.headless or a.cover or a.doctor)
    if a.web or a.gui or not terminal_mode:
        return cmd_web(a, req)

    if a.json:
        tracks, info = build_queue(req, seed_refs=[a.playlist] if a.playlist else None,
                                   count=a.count, extra_queries=a.query)
        print(json.dumps({"info": info, "tracks": tracks}, indent=2, ensure_ascii=False))
        return 0
    if a.list:
        return cmd_list(a)
    if a.export or a.open_spotube:
        return cmd_export(a)
    if a.daemon:
        return cmd_daemon(a, req)

    dj = DJ(backend=a.backend, volume=a.volume)
    res = dj.start(req, seed_refs=[a.playlist] if a.playlist else None,
                   count=a.count, extra_queries=a.query)
    if not res["ok"]:
        return 1
    if dj.headless or a.backend == "none":
        for i, t in enumerate(res["tracks"], 1):
            print(f"{i:2}. {str(t.get('artist', '?'))[:22]:22} {str(t['title'])[:54]:54} "
                  f"{t.get('score', 0):+5.1f}")
        return 0
    try:
        dj.run()
    finally:
        dj.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
