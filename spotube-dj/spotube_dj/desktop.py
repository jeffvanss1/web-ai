"""
Installing the GUI as a desktop application entry.

Why this file exists: the honest answer to "can this go in Spotube?" is no -
Spotube loads a web app and has no plugin slot, and the Spotify API this app
replaces is Premium-gated. What a free account *can* have is its own launcher:
one icon on the dock, one name in the app menu, and if you want Spotube beside
it, the GUI's Settings button is called "Send queue to Spotube". That is the
integration that actually works, so that is what this installs.

Everything is written under $HOME. No sudo, no system dirs, and `uninstall`
removes exactly the two files it created.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "spotube-dj.desktop"
ICON_NAME = "spotube-dj.png"
PKG_ROOT = Path(__file__).resolve().parent          # .../spotube_dj
REPO_ROOT = PKG_ROOT.parent

ENTRY = """[Desktop Entry]
Type=Application
Version=1.0
Name=Spotify DJ (free)
GenericName=Music DJ
Comment=Mood mixes and AI requests played from YouTube Music - no Premium
Exec={exec_line}
Path={workdir}
Icon={icon}
Terminal=false
Categories=AudioVideo;Audio;Music;Player;
Keywords=music;dj;radio;playlist;youtube;spotube;
StartupNotify=true
StartupWMClass=Spotube-dj
Actions=Search;

[Desktop Action Search]
Name=Search for a song
Exec={exec_line} --search "%u"
"""


def applications_dir() -> Path:
    """$XDG_DATA_HOME/applications, defaulting to ~/.local/share/applications."""
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "applications"


def icons_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "icons" / "hicolor" / "256x256" / "apps"


def launcher_path() -> Path:
    return applications_dir() / APP_ID


def icon_path() -> Path:
    return icons_dir() / ICON_NAME


def source_icon() -> Path:
    return REPO_ROOT / "docs" / "icon.png"


def user_shim() -> Path:
    """~/.local/bin/spotube-dj, where install.sh puts the launcher script."""
    return Path.home() / ".local" / "bin" / "spotube-dj"


def venv_python(root: Path | None = None) -> Path | None:
    p = (Path(root) if root else REPO_ROOT) / ".venv" / "bin" / "python"
    return p if p.exists() else None


def pick_launcher() -> tuple[str, str]:
    """
    (exec line, which one it is) for the .desktop file.

    The order matters, and this is the bug it exists to kill: `shutil.which`
    only sees the PATH of whoever ran --install-desktop, so installing with the
    system python while the app lives in .venv used to write an Exec that
    imported a package - or a Tkinter - that interpreter did not have. The window
    then never appeared and nothing said why, because a desktop launch has no
    terminal to read. So: the installed shim first (it knows the venv), the repo's
    own .venv second, and only then the running interpreter.
    """
    shim = user_shim()
    found = shutil.which("spotube-dj")
    if shim.exists() and os.access(shim, os.X_OK):
        return str(shim), "shim"
    if found and Path(found).exists():
        return str(found), "shim"
    py = venv_python()
    if py is not None:
        return f"{py} -m spotube_dj --web", "venv"
    exe = sys.executable or "python3"
    if " " in exe:                              # .desktop Exec is not shell-parsed
        alt = shutil.which("python3")
        exe = alt or f'"{exe}"'
    return f"{exe} -m spotube_dj --web", "system"


def exec_line() -> str:
    return pick_launcher()[0]


def render(workdir: Path | None = None) -> str:
    """The .desktop contents, with this checkout's paths filled in."""
    return ENTRY.format(exec_line=exec_line(),
                        workdir=str(workdir or REPO_ROOT),
                        icon=str(icon_path()))


def install(app_dir: Path | None = None, workdir: Path | None = None) -> list[Path]:
    """Write the launcher and the icon. Returns the files it created."""
    app_dir = Path(app_dir) if app_dir else applications_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    made = []
    tgt = app_dir / APP_ID
    tgt.write_text(render(workdir=workdir or REPO_ROOT), encoding="utf-8")
    # Some shells (GNOME's app grid among them) list a user .desktop file but
    # refuse to launch it unless it is executable; the write alone is not enough.
    try:
        os.chmod(tgt, 0o755)
    except OSError:
        pass
    made.append(tgt)

    # the icon lives in the repo (so a `git pull` refreshes it with the app), and
    # only a copy goes into the hicolor theme where the shell looks for it
    src = source_icon()
    if src.exists():
        idir = icons_dir()
        idir.mkdir(parents=True, exist_ok=True)
        dst = idir / ICON_NAME
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
        made.append(dst)
    refresh(app_dir)
    return made


def remove(app_dir: Path | None = None) -> list[Path]:
    """Delete what install() made. Missing files are not an error."""
    removed = []
    for path in ((Path(app_dir or applications_dir()) / APP_ID), icon_path()):
        try:
            if path.exists():
                path.unlink()
                removed.append(path)
        except OSError:
            pass
    refresh(Path(app_dir) if app_dir else applications_dir())
    return removed


def refresh(target: Path | None = None) -> None:
    """
    Nudge the caches, if this machine has them. Every step is optional and
    non-fatal: a desktop without desktop-file-utils still gets a .desktop file
    that works, it just may need a logout before the menu notices.
    """
    apps = Path(target) if target else applications_dir()
    for exe, args in (("update-desktop-database", [str(apps)]),
                      ("gtk-update-icon-cache", ["-qtf", str(icons_dir().parents[2])])):
        path = shutil.which(exe)
        if not path:
            continue
        try:
            subprocess.run([path, *args], capture_output=True, timeout=30)
        except Exception:
            pass        # a cache we cannot touch is not a reason to fail the install


def doctor_lines() -> list[str]:
    """What `--doctor` should report about the launcher."""
    tgt = launcher_path()
    out = [f"launcher: {tgt} ({'present' if tgt.exists() else 'not installed'})"]
    src = source_icon()
    out.append(f"icon: {icon_path()} ({'present' if icon_path().exists() else 'missing'})"
               + ("" if src.exists() else "  (no docs/icon.png in this checkout)"))
    if shutil.which("update-desktop-database"):
        out.append("update-desktop-database: available")
    else:
        out.append("update-desktop-database: not installed (the .desktop file still works)")
    return out


def self_test() -> tuple[bool, list[str]]:
    """
    Ask the launcher the questions a silent failure answers with "nothing came
    up". Run at install time and by --doctor, since a desktop launch has no
    terminal to read a traceback from.
    """
    lines: list[str] = []
    ok = True
    tgt = launcher_path()
    if not tgt.exists():
        return False, ["launcher not installed (spotube-dj --install-desktop)"]
    mode = tgt.stat().st_mode
    if not (mode & 0o111):
        ok = False
        lines.append(f"NOT executable (mode {oct(mode)[-3:]}) - re-run --install-desktop, "
                     "or: chmod +x " + str(tgt))
    cp = configparser.RawConfigParser()
    cp.optionxform = str
    cp.read_string(tgt.read_text(encoding="utf-8"))
    main = dict(cp["Desktop Entry"])
    py = (main.get("Exec") or "").split()[0].strip('"')
    if main.get("Exec", "").startswith('"'):
        py = main["Exec"].split('"')[1]
    if not Path(py).exists() and not shutil.which(py):
        ok = False
        lines.append(f"the interpreter in Exec does not exist: {py}")
    work = Path(main.get("Path") or REPO_ROOT)
    if not work.is_dir():
        ok = False
        lines.append(f"Path= does not exist: {work} (the repo moved? re-install)")
    # the real question: can that interpreter, from that directory, serve the page?
    # It used to ask about tkinter, which is exactly why a missing python3-tk could
    # break a menu entry. A browser is the only GUI dependency now, and every desktop
    # has one - so this checks the module and the port instead.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.setdefault("DISPLAY", os.environ.get("DISPLAY", ""))
    try:
        r = subprocess.run([py, "-m", "spotube_dj", "--help"],
                           cwd=str(work), env=env, capture_output=True, text=True, timeout=45)
        if r.returncode == 0:
            lines.append(f"starts ok (help printed, {len(r.stdout or '')} lines) via {py}")
        else:
            ok = False
            err = (r.stderr or r.stdout or "").strip().splitlines()
            lines.append("that interpreter cannot run the player: "
                         + (err[-1][:150] if err else f"exit {r.returncode}"))
    except FileNotFoundError:
        ok = False
        lines.append(f"cannot run {py} at all")
    except Exception as e:
        lines.append(f"self-test skipped: {e.__class__.__name__}: {e}")
    if not env["DISPLAY"]:
        lines.append("no DISPLAY here, so the browser could not be proven to open - the "
                     "player still runs headless: start it with --no-browser and open the URL")
    return ok, lines
