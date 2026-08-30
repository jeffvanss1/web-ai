"""
The desktop launcher: written under $XDG_DATA_HOME in a temp dir, parsed back
with RawConfigParser, and taken away again. No system state is touched, and no
tool has to exist on the machine - `refresh()` degrades silently by design.
"""
from __future__ import annotations

import configparser
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (sys.path bootstrap)

import desktop


class _Home:
    """Point $XDG_DATA_HOME (and the icon's source) at a temp tree."""

    def __init__(self, with_icon=True):
        self.dir = tempfile.mkdtemp()
        self.with_icon = with_icon

    def __enter__(self):
        self.patches = [
            mock.patch.dict("os.environ", {"XDG_DATA_HOME": self.dir}),
            mock.patch.object(desktop, "source_icon",
                              return_value=Path(self.dir) / ("icon.png" if self.with_icon
                                                              else "absent.png")),
        ]
        for p in self.patches:
            p.start()
        src = Path(self.dir) / "icon.png"
        if self.with_icon:
            src.write_bytes(b"\x89PNG fake")
        return Path(self.dir) / "applications"

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        return False


class DesktopEntryTests(unittest.TestCase):
    def test_install_writes_a_parsable_entry_and_the_icon(self):
        with _Home() as apps:
            made = desktop.install(app_dir=apps)
            tgt = apps / desktop.APP_ID
            self.assertTrue(tgt.exists(), made)
            cp = configparser.RawConfigParser()
            cp.optionxform = str            # .desktop keys are case-sensitive
            cp.read_string(tgt.read_text())
            main = dict(cp["Desktop Entry"])
            self.assertEqual(main["Type"], "Application")
            self.assertIn("--web", main["Exec"])
            self.assertTrue(main["Name"])
            self.assertTrue(main["Categories"].endswith(";"))
            self.assertTrue(main["Icon"].endswith("spotube-dj.png"))
            # the copy in the hicolor theme is what a shell actually finds
            self.assertTrue(any("spotube-dj.png" in str(x) for x in made), made)

    def test_the_workdir_is_the_checkout_so_imports_resolve(self):
        with _Home() as apps:
            desktop.install(app_dir=apps)
            cp = configparser.RawConfigParser()
            cp.optionxform = str
            cp.read_string((apps / desktop.APP_ID).read_text())
            self.assertEqual(Path(dict(cp["Desktop Entry"])["Path"]), desktop.REPO_ROOT)

    def test_remove_is_what_install_made_and_survives_absence(self):
        with _Home() as apps:
            desktop.install(app_dir=apps)
            gone = desktop.remove(apps)
            self.assertFalse((apps / desktop.APP_ID).exists())
            self.assertTrue(gone)
            self.assertEqual(desktop.remove(apps), [], "second removal must be a no-op")

    def test_no_icon_file_is_not_an_error(self):
        with _Home(with_icon=False) as apps:
            made = desktop.install(app_dir=apps)
            self.assertEqual([x.name for x in made], [desktop.APP_ID])

    def test_a_missing_cache_tool_does_not_break_install(self):
        with _Home() as apps, mock.patch("desktop.shutil.which", return_value=None):
            desktop.install(app_dir=apps)
            self.assertTrue((apps / desktop.APP_ID).exists())

    def test_cache_tools_are_only_run_when_present(self):
        calls = []
        with mock.patch("desktop.shutil.which",
                       side_effect=lambda n: f"/usr/bin/{n}"), \
             mock.patch("desktop.subprocess.run",
                        side_effect=lambda a, **k: calls.append(a)):
            desktop.refresh()
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0][1].endswith("applications"), calls)
        self.assertIn("icons", calls[1][-1], calls[1])

    def test_a_console_script_is_preferred_over_the_module(self):
        # a shim is only usable if it is really there: writing an Exec that points
        # at a deleted script is exactly how "the icon shows but nothing comes up"
        with tempfile.TemporaryDirectory() as td:
            shim = Path(td) / "spotube-dj"
            shim.write_text("#!/bin/sh\nexec python3 -m spotube_dj \"$@\"\n")
            shim.chmod(0o755)
            with mock.patch.object(desktop, "user_shim", return_value=shim), \
                 mock.patch("desktop.shutil.which", return_value=None):
                self.assertEqual(desktop.exec_line(), str(shim))

    def test_a_dangling_shim_is_not_used(self):
        with mock.patch.object(desktop, "user_shim",
                              return_value=Path("/nonexistent/spotube-dj")), \
             mock.patch("desktop.shutil.which",
                       side_effect=lambda n: "/nonexistent/spotube-dj"
                       if n == "spotube-dj" else None), \
             mock.patch.object(desktop, "venv_python", return_value=None), \
             mock.patch.object(desktop.sys, "executable", "/opt/py/bin/python3"):
            line, kind = desktop.pick_launcher()
        self.assertEqual(kind, "system")
        self.assertEqual(line, "/opt/py/bin/python3 -m spotube_dj --web")

    def test_the_venv_next_to_the_repo_wins_over_the_running_interpreter(self):
        # install.sh puts yt-dlp in .venv; if the launcher ran the *system* python
        # instead, the window opens and every search fails (or it dies on import)
        with tempfile.TemporaryDirectory() as td:
            py = Path(td) / ".venv" / "bin" / "python"
            py.parent.mkdir(parents=True)
            py.write_text("")
            with mock.patch.object(desktop, "user_shim",
                                  return_value=Path("/nope/spotube-dj")), \
                 mock.patch("desktop.shutil.which", return_value=None), \
                 mock.patch.object(desktop, "venv_python",
                                   side_effect=lambda root=None: py), \
                 mock.patch.object(desktop.sys, "executable", "/usr/bin/python3"):
                line, kind = desktop.pick_launcher()
            self.assertEqual(kind, "venv")
            self.assertEqual(line, f"{py} -m spotube_dj --web")

    def test_an_interpreter_path_with_spaces_stays_launchable(self):
        # a .desktop Exec is parsed by the spec, not by a shell: quoting is not
        # honoured everywhere, so walk off to a real absolute python3 instead
        with mock.patch.object(desktop, "user_shim", return_value=Path("/nope/x")), \
             mock.patch.object(desktop, "venv_python", return_value=None), \
             mock.patch.object(desktop.sys, "executable", "/home/u/My Pro/bin/python"), \
             mock.patch("desktop.shutil.which",
                       side_effect=lambda n: "/usr/bin/python3" if n == "python3" else None):
            self.assertEqual(desktop.exec_line(), "/usr/bin/python3 -m spotube_dj --web")

    def test_doctor_reports_the_launcher_state(self):
        with _Home() as apps, mock.patch.object(desktop, "applications_dir",
                                                 return_value=apps):
            self.assertIn("not installed", "\n".join(desktop.doctor_lines()))
            desktop.install(app_dir=apps)
            self.assertIn("present", "\n".join(desktop.doctor_lines()))


if __name__ == "__main__":
    unittest.main()


class LauncherSanityTests(unittest.TestCase):
    """The two things that make an app-menu entry silently refuse to work."""

    def test_the_file_is_marked_executable(self):
        with _Home() as apps:
            tgt = apps / desktop.APP_ID
            desktop.install(app_dir=tgt.parent)
            self.assertTrue(tgt.stat().st_mode & 0o111,
                            "GNOME's app grid lists a non-executable .desktop but "
                            "will not launch it")

    def test_the_entry_names_the_app_the_same_way_everywhere(self):
        # the shell pairs a launched window with its entry by WM_CLASS, and finds
        # the icon by `Icon=`: both strings have to keep agreeing with the file the
        # installer writes, which is the bug this test used to catch
        entry = desktop.ENTRY
        claimed = [l.split("=", 1)[1] for l in entry.splitlines()
                   if l.startswith("StartupWMClass")]
        self.assertEqual(claimed, ["Spotube-dj"])
        self.assertTrue([l for l in entry.splitlines() if l.startswith("Icon=")],
                        "no Icon= line: the app grid shows a generic square")
        self.assertEqual(desktop.icon_path().name, desktop.ICON_NAME,
                         "the icon is written somewhere the entry will not look")
        self.assertNotIn("Actions=Search;Settings;", entry,
                         "a second menu entry points at a window that no longer exists")

    def test_self_test_refuses_to_pretend_a_broken_launcher_is_fine(self):
        with _Home() as apps:
            desktop.install(app_dir=apps)
            tgt = apps / desktop.APP_ID
            ok, why = desktop.self_test() if apps == desktop.applications_dir() else (
                None, None)
            # applications_dir is patched by _Home, so self_test sees this file
            ok, why = desktop.self_test()
            self.assertTrue(ok, why)
            self.assertTrue(any("starts ok" in x for x in why), why)
            tgt.chmod(0o644)
            ok, why = desktop.self_test()
            self.assertFalse(ok, why)
            self.assertTrue(any("NOT executable" in x for x in why), why)
            body = tgt.read_text().replace("/usr/local/bin/python3", "/nope/python3")
            tgt.write_text(body)
            ok, why = desktop.self_test()
            self.assertFalse(ok, why)
            self.assertTrue(any("does not exist" in x for x in why), why)

    def test_a_missing_launcher_is_reported_as_not_installed(self):
        with _Home() as apps, mock.patch.object(desktop, "applications_dir",
                                                 return_value=apps):
            ok, why = desktop.self_test()
        self.assertFalse(ok)
        self.assertIn("not installed", why[0])
