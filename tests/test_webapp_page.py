"""
The browser skin's document, checked as a file rather than as pixels.

There is no browser in this environment, so these are the assertions that actually
catch the mistakes a page like this makes: an id the script reaches for that the
markup never defines, a button wired to a verb the server does not have (or the
other way round, a feature nobody can press), a colour token that never got
substituted, markup built with innerHTML from server text, and - the one that
bit this file twice - two string literals written next to each other the way
Python allows and JavaScript does not.

`node --check` runs when node happens to be installed; the Python-side checks are
the ones the suite always has.
"""
from __future__ import annotations

import ast
import importlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401  (sys.path bootstrap)

import viewmodel as vm
import web as web_mod
import webapp


def _script(html: str) -> str:
    m = re.search(r"<script>([\s\S]*)</script>", html)
    assert m, "the page must carry exactly one inline script"
    return m.group(1)


# A naive `re.sub` of string literals does not survive this page: the SVG paths in
# ICONS carry escaped quotes, and a stray apostrophe in a path can make a
# single-quote regex swallow a whole function body, so a balanced script reads as
# unbalanced. Tokenize instead of regex: drop string literals (double, single and
# template), line and block comments, and regex literals, so the count below only
# ever sees real code, exactly the way `node --check` does.
_CODE_OPERAND = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$)]}")


def _strip_non_code(js: str) -> str:
    out: list[str] = []
    i = 0
    n = len(js)
    last = ""
    while i < n:
        c = js[i]
        if c in ('"', "'"):
            q = c
            i += 1
            while i < n and js[i] != q:
                i += 2 if js[i] == "\\" else 1
            i += 1
            out.append('""' if c == '"' else "''")
            last = '"'
        elif c == "`":
            i += 1
            while i < n and js[i] != "`":
                i += 2 if js[i] == "\\" else 1
            i += 1
            out.append("``")
            last = "`"
        elif c == "/" and i + 1 < n and js[i + 1] == "/":
            i += 2
            while i < n and js[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and js[i + 1] == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                i += 1
            i += 2
        elif c == "/" and i + 1 < n and last not in _CODE_OPERAND:
            # a division operator could follow an operand; a regex literal cannot
            i += 1
            in_class = False
            closed = False
            while i < n:
                c2 = js[i]
                if c2 == "\\":
                    i += 2
                    continue
                if c2 == "[":
                    in_class = True
                elif c2 == "]":
                    in_class = False
                elif c2 == "/" and not in_class:
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                i -= 1
                out.append("/")
                last = "/"
                continue
            while i < n and js[i] in "gimsuy":
                i += 1
            out.append("/ /")
            last = '"'
        else:
            out.append(c)
            if not c.isspace():
                last = c
            i += 1
    return "".join(out)


class PageShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = webapp.page()
        cls.js = _script(cls.html)

    def test_nothing_is_left_unsubstituted(self):
        self.assertNotIn("@@", self.html)
        for tok in webapp.COLORS:
            self.assertNotIn(tok, self.html, f"{tok} never reached the palette map")

    def test_page_is_deterministic_and_self_contained(self):
        self.assertEqual(webapp.page(), self.html)
        # no tag may reach off the machine: no <link>, no <script src>, no <img src=http
        self.assertNotIn("<link", self.html)
        self.assertNotIn('src="http', self.html)
        self.assertNotIn("url(http", self.html)
        self.assertNotIn("@import", self.html)

    def test_page_size_is_sane(self):
        # generated inline CSS+JS+SVG: big enough to be the app, small enough that a
        # first paint on a slow laptop is not a wait
        self.assertGreater(len(self.html), 20000, "the page lost most of itself")
        self.assertLess(len(self.html), 160000, "the page is bloating")

    def test_the_three_panels_and_the_player_are_all_present(self):
        body = self.html.split("<body>", 1)[1]
        for cls in ("app", "side", "main", "detail", "player"):
            self.assertIn(f'class="{cls}', body, f"the {cls} panel is gone")
        self.assertIn("Your Library", body)
        self.assertIn("What do you want to play?", body)
        self.assertIn("Now playing", body)

    def test_icons_are_svg_not_characters(self):
        self.assertIn("<svg", self.html)
        for name in ("play", "pause", "next", "prev", "shuffle", "repeat", "heart",
                     "queue", "search", "expand", "sparkle", "clock", "dots", "home"):
            self.assertIn(name, webapp.ICONS, f"the transport lost the {name} icon")
        for name, svg in webapp.ICONS.items():
            self.assertTrue(svg.startswith("<svg"), name)
            self.assertIn("</svg>", svg, name)
            self.assertNotIn("@@", svg, name)

    def test_the_ai_dj_chat_panel_is_present_and_posts_text(self):
        body = self.html.split("<body>", 1)[1]
        for ident in ("chat-open", "chat", "chat-log", "chat-in", "chat-send", "chat-sub"):
            self.assertIn(f'id="{ident}"', body, f"the chat is missing {ident}")
        # messages land as text, never as HTML from the server (the DJ reply could
        # contain markup-shaped text)
        self.assertIn('"msg " + cls', self.js)
        self.assertNotIn("chat-log\").innerHTML", self.js)
        # the send path is the /api/chat round-trip
        self.assertIn("post(\"/api/chat\", {q})", self.js)

    def test_every_icon_the_markup_asks_for_exists(self):
        # `svg("play")`, ICONS.play, ICONS["play"], the icon a menu item asks for,
        # and every `@@token@@` in the markup. The helper's own definition
        # (`function svg(name)` / `ICONS[name]`) is not a use of an icon.
        js = "\n".join(l for l in self.js.split("\n") if "function svg(" not in l
                       and "ICONS[name]" not in l)
        wanted = set(re.findall(r"@@([a-z_]+)@@", webapp.BODY))
        for pat in (r'svg\("([a-z_]+)"\)', r'ICONS\.([a-z_]+)', r'ICONS\[([a-z_]+)\]',
                    r'"icon":\s*"([a-z_]+)"', r'icons\.([a-z_]+)'):
            wanted |= set(re.findall(pat, js))
        missing = sorted(w for w in wanted if w not in webapp.ICONS)
        self.assertFalse(missing, f"BODY/JS ask for icons nobody draws: {missing}")

    def test_the_blurred_cover_layer_is_real_css(self):
        css = re.search(r"<style>([\s\S]*)</style>", self.html).group(1)
        self.assertIn(".bg{", css, "the backdrop layer has no rules")
        self.assertIn("filter:blur(", css)
        self.assertIn("saturate(", css)
        self.assertIn(".scrim{", css, "the backdrop has nothing darkening it")
        self.assertIn("backgroundImage", self.js, "nothing ever sets the backdrop image")

    def test_the_playing_color_drifts_slowly_instead_of_holding_still(self):
        # the cover-derived tint should be alive, not a fixed gradient: a slow
        # keyframes animation moves the wash across the page. The drift must be a
        # pure `transform` animation - animating filter/background-position on a
        # blurred layer re-rasterizes the blur every frame and is the source of jank.
        css = re.search(r"<style>([\s\S]*)</style>", self.html).group(1)
        self.assertIn("@keyframes tintdrift", css,
                      "the backdrop has no slow colour drift animation")
        self.assertIn("animation:tintdrift", css,
                      "the backdrop layer never runs the tint animation")
        # "slow" is a stated requirement: a 1-2s loop reads as a busy page, a 20s+ one as ambient
        m = re.search(r"animation:tintdrift\s+([\d.]+)s", css)
        self.assertIsNotNone(m, "the tint animation has no duration")
        self.assertGreaterEqual(float(m.group(1)), 8,
                                f"the tint animation cycles every {m.group(1)}s "
                                "- far too fast to feel ambient")
        # smoothness: the keyframes move only `transform`, never `filter`/hue-rotate
        # (a static hue tint is allowed on the base layer; an animated one is not)
        kf = re.search(r"@keyframes tintdrift\{([\s\S]*?)\}\s*$", css)
        self.assertIsNotNone(kf, "the tintdrift keyframes block is missing")
        kf_body = kf.group(1)
        self.assertIn("transform:", kf_body, "the drift does not move the backdrop")
        self.assertNotIn("hue-rotate(", kf_body,
                         "the keyframes animate filter/hue-rotate - that re-blurs "
                         "the 72px backdrop every frame and causes lag")
        self.assertNotIn("--gx", kf_body,
                         "the keyframes animate custom gradient properties - that "
                         "re-blurs the backdrop every frame and causes lag")

    def test_credits_carry_only_the_record_facts(self):
        # the transport bar already shows progress, length and cache state, so the
        # Credits block should name the record, not re-print those three
        self.assertIn('"released", np.release_year', self.js,
                      "Credits lost the release year")
        for banned in ('["length"', '["audio"', 'el("dt", null, "position")'):
            self.assertNotIn(banned, self.js,
                             f"Credits still re-presents the transport: {banned}")

    def test_the_queued_header_can_carry_the_mix_name(self):
        # the Daylist-style title ("lofi tuesday night") must be readable in the
        # Up Next header, not just carried in state nobody paints. The page builds
        # that line from s.vibe, prefixing it to the count when present.
        self.assertIn("(s.vibe || \"\")", self.js,
                      "the header never reads the mix name the engine computed")
        self.assertIn('"").trim()', self.js,
                      "a blank vibe must not render the word 'undefined'")

    def test_a_row_with_only_a_thumbnail_still_gets_a_cover(self):
        # `art` used to read `track.art` alone, so a queue row that only carried
        # `art_card`, or an album/discography row that only carried the raw
        # `thumbnail`, was drawn as a tinted initial even though the art was there
        js = self.js
        self.assertIn("track.art_card", js, "art() never falls back to the card picture")
        self.assertIn("track.thumbnail", js, "art() never falls back to the raw thumbnail")
        self.assertIn("function cover(", js,
                      "there is no onerror retry for a blank thumbnail")
        self.assertIn("hqdefault.jpg", js,
                      "the fallback never tries the reliably-served frame")
        self.assertIn('img.loading = "eager"', js,
                      "a small row tile must not wait for a scroll event that never comes")
        self.assertIn("track.id ? \"https://i.ytimg.com/vi/\" + track.id", js,
                      "art() has no id-derived last-resort hqdefault when every field is blank")

    def test_the_cache_pill_is_never_squeezed_into_an_ellipsis(self):
        # it said "0 stored, 0 d…" because flex let the icons beside it take the
        # room; a number that cannot be read is worse than no number
        css = re.search(r"<style>([\s\S]*)</style>", self.html).group(1)
        self.assertIn(".right .pill{flex:0 0 auto;max-width:none}", css)
        self.assertIn("@media (max-width:1000px){.right .pill{display:none}}", css)

    def test_a_pill_shows_its_numbers_not_its_caption(self):
        # "audio cache: 1 stored, 2 downloading" in a 30ch box came out as
        # "audio cache: ..." - the caption is what the box has no room for
        self.assertIn('replace(/^audio cache:\\s*/i, "")', self.js)
        self.assertIn('setText($("cache")', self.js)

    def test_the_hover_states_a_listener_expects_are_declared(self):
        css = re.search(r"<style>([\s\S]*)</style>", self.html).group(1)
        for rule in (".row:hover", ".card:hover", ".lib:hover", ".chip.on", ".fab",
                     "@keyframes eq", "input[type=range]"):
            self.assertIn(rule, css, f"{rule} is gone: the page stopped reacting to the mouse")
        self.assertIn("@media", css, "the layout is not responsive at all")

    def test_progress_is_smooth_without_polling_harder(self):
        # the bar advances from a local clock between state ticks
        self.assertIn("setInterval(drawProgress", self.js)
        self.assertIn("Date.now()", self.js)
        self.assertLessEqual(len(re.findall(r"setTimeout\(poll", self.js)), 1,
                             "two poll loops means two requests per tick")


class IdContractTests(unittest.TestCase):
    """Every `$("id")` the script reaches for must exist in the markup."""

    @classmethod
    def setUpClass(cls):
        cls.html = webapp.page()
        cls.js = _script(cls.html)

    def test_script_ids_exist(self):
        have = set(re.findall(r'id="([A-Za-z0-9_-]+)"', self.html))
        # a node inside a region the script rebuilds gets its id at runtime; that
        # counts as "the page builds it", as long as the same script assigns it
        have |= set(re.findall(r'\.id = "([A-Za-z0-9_-]+)"', self.js))
        want = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', self.js))
        missing = sorted(w for w in want if w not in have)
        self.assertFalse(missing, f"the script reaches for ids the page does not build: {missing}")

    def test_every_view_has_a_section(self):
        for view in vm.VIEWS:
            self.assertIn(f'id="view-{view}"', self.html, f"{view} has no section")

    def test_nav_labels_all_have_a_target(self):
        for view, icon, label in vm.NAV:
            self.assertIn(view, self.html, label)
            self.assertIn(icon, webapp.ICONS, f"{label} wants a {icon} icon nobody draws")

    def test_unreferenced_sections_would_be_dead_weight(self):
        have = set(re.findall(r'id="view-([a-z]+)"', self.html))
        # "page" is the dynamic album/artist view: reachable via setView("page"),
        # but not a permanent nav destination, so it is the one allowed extra.
        self.assertEqual(have, set(vm.VIEWS) | {"page"},
                         "the page has a section no view can reach, or a view with no section")


class VerbContractTests(unittest.TestCase):
    """Buttons and verbs must agree in both directions, or a click is a 400."""

    @classmethod
    def setUpClass(cls):
        cls.html = webapp.page()
        cls.js = _script(cls.html)

    def _verbs(self):
        found = set(re.findall(r'data-action="([a-z_]+)"', self.html))
        found |= set(re.findall(r'act\("([a-z_]+)"', self.js))
        return found

    def test_every_button_sends_a_real_verb(self):
        verbs = self._verbs()
        unknown = sorted(v for v in verbs if v not in web_mod.ACTIONS)
        self.assertFalse(unknown, f"the page calls verbs the server does not have: {unknown}")

    def test_every_server_verb_is_reachable(self):
        verbs = self._verbs()
        unreachable = sorted(v for v in web_mod.ACTIONS if v not in verbs)
        self.assertFalse(unreachable,
                         f"the engine can do these but nothing on the page asks: {unreachable}")

    def test_the_transport_verbs_are_the_ones_a_listener_expects(self):
        for verb in ("playpause", "next", "prev", "like", "shuffle", "repeat", "seek",
                     "volume", "auto", "topup", "radio", "request", "mix", "stop"):
            self.assertIn(verb, self._verbs(), verb)

    def test_data_view_targets_are_real_views(self):
        for view in re.findall(r'data-view="([a-z]+)"', self.html):
            self.assertIn(view, vm.VIEWS, f"data-view={view} has nowhere to go")

    def test_the_back_button_has_a_target_from_the_first_navigation(self):
        # the initial view must be the first history entry, or the first push leaves
        # hist=["search"] hix=0 and nav-back is born disabled: "page loads -> search,
        # back does nothing". The history must seed with "home" so back always works.
        self.assertIn('hist:["home"], hix:0', self.js,
                      "the view history does not seed with the initial view")
        self.assertIn("navSync()", self.js,
                      "the nav states are set at boot and after every view change")


class SafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = webapp.page()
        cls.js = _script(cls.html)

    def test_server_text_never_enters_the_dom_as_html(self):
        bad = []
        for m in re.finditer(r"\.innerHTML\s*=\s*([^;\n]*)", self.js):
            rhs = m.group(1).strip()
            # allowed: clearing a node, and the static icon strings
            if rhs in ('""', "''") or rhs.startswith("ICONS[") or rhs == "ICONS.icon":
                continue
            bad.append(rhs)
        self.assertFalse(bad, f"innerHTML assigned something dynamic: {bad}")

    def test_track_titles_go_in_as_text(self):
        self.assertIn("n.textContent = txt", self.js.replace("  ", " "))
        for fn in ("rowNode", "cardNode", "libRow", "drawDetail"):
            body = self.js.split(f"function {fn}(", 1)[1][:1600]
            self.assertTrue("el(" in body or "textContent" in body, fn)
            self.assertNotIn("innerHTML", body.replace('innerHTML = "";', ""),
                            f"{fn} builds rows with innerHTML")

    def test_art_urls_stay_local(self):
        # the page never builds a cover URL: the server stamps `art` (a /art/<id>
        # path on this process) and the page only reads it. A cover that needs the
        # internet is a broken cover, and a remote image is a tracking pixel.
        self.assertIn("track.art", self.js)
        # nothing in the document reaches off the machine, whatever the copy says
        for bad in ('src="http', "src='http", 'href="http', "url(http", "fetch('http",
                    'fetch("http', "@import", "<link"):
            self.assertNotIn(bad, self.html, f"{bad} is a remote reference")

    def test_the_page_is_utf8_safe(self):
        self.html.encode("utf-8").decode("utf-8")
        self.assertIn('<meta charset=utf-8>', self.html)


class JsSyntaxTests(unittest.TestCase):
    def test_no_juxtaposed_string_literals(self):
        # Python glues "a" "b" into "ab"; JavaScript treats it as a syntax error
        js = _script(webapp.page())
        hits = [i + 1 for i, line in enumerate(js.split("\n"))
                if re.search(r'"(\s*\\?)$', line.rstrip())
                and re.match(r'^\s*"', (js.split("\n") + [""])[i + 1].lstrip())
                and not re.search(r'[+,=(]\s*$', line.rstrip())]
        self.assertFalse(hits, f"JS lines {hits}: string literals sitting next to each other")

    def test_balanced_braces_and_parens(self):
        js = _script(webapp.page())
        # strings, comments and regex literals could each hold a brace, so strip
        # them with a tokenizer and count only the code that remains. This is the
        # same set of braces `node --check` balances.
        stripped = _strip_non_code(js)
        for open_c, close_c in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(stripped.count(open_c), stripped.count(close_c),
                             f"unbalanced {open_c}{close_c} in the script")

    @unittest.skipUnless(shutil.which("node"), "node is not installed here")
    def test_node_accepts_the_script(self):
        js = _script(webapp.page())   # already declares ICONS and VM
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[:2000])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_the_constants_the_page_needs_are_the_ones_the_app_ships(self):
        c = webapp.app_constants()
        self.assertEqual([m["q"] for m in c["moods"]], [q for _l, q in vm.MOODS])
        self.assertEqual(c["views"], list(vm.VIEWS))
        self.assertEqual([g[2] for g in c["greeting"]], [g[2] for g in vm.GREETING])
        self.assertIn("const VM=", webapp.page())


class FrontEndTests(unittest.TestCase):
    """The Tk window is gone, and the code must say so in the places that matter."""

    def test_the_tk_module_is_actually_gone(self):
        pkg = Path(webapp.__file__).parent
        self.assertFalse((pkg / "gui.py").exists(), "spotube_dj/gui.py is still shipped")
        self.assertEqual([p.stem for p in pkg.glob("gui*.py")], [],
                         "a Tk module survived the move")
        self.assertNotIn("gui", [p.stem for p in pkg.glob("*.py")])

    def test_the_cli_no_longer_imports_it(self):
        src = (Path(webapp.__file__).parent / "__main__.py").read_text()
        self.assertNotIn("import gui", src)
        self.assertNotIn("gui_settings", src)
        self.assertIn("def cmd_web", src)

    def test_gui_flag_is_a_benign_alias(self):
        src = (Path(webapp.__file__).parent / "__main__.py").read_text()
        self.assertIn('"--gui"', src)
        self.assertIn("deprecated alias", src)
        tree = ast.parse(src)
        flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', src))
        self.assertIn("--web", flags)
        self.assertIn("--no-browser", flags)
        self.assertNotIn("--gui-settings", flags)
        self.assertNotIn("--small-fonts", flags)

    def test_the_desktop_launcher_launches_the_player(self):
        desktop = importlib.import_module("desktop")
        src = Path(desktop.__file__).read_text()
        self.assertIn('-m spotube_dj --web", "venv"', src)
        self.assertIn('-m spotube_dj --web", "system"', src)
        self.assertNotIn("--gui", src, "the launcher still asks for the Tk window")
        self.assertNotIn("--gui-settings", desktop.ENTRY)
        self.assertIn('--search "%u"', desktop.ENTRY)


class RebuildGuardTests(unittest.TestCase):
    """A state tick every 700 ms must not repaint what has not changed.

    The removed Tk window had a `Bridge` that refused identical rows; the browser
    page needs the same rule or a scrolled sidebar jumps to the top twice a second.
    These run the shipped helpers under node against a fake element, so it is the
    page's own logic being tested, not a copy of it.
    """

    @staticmethod
    def _helpers() -> str:
        js = _script(webapp.page())
        start = js.index("const sigOf")
        end = js.index("/* ---------- art:")
        return js[start:end]

    def _run(self, body: str) -> dict:
        if not shutil.which("node"):
            self.skipTest("node is not installed here")
        program = self._helpers() + "\n" + body
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(program)
            path = fh.name
        try:
            r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, (r.stderr or "")[-1500:])
            return __import__("json").loads(r.stdout.strip().splitlines()[-1])
        finally:
            Path(path).unlink(missing_ok=True)

    _BOX = """
function box0(){
  const b = {dataset:{}, scrollTop:0, children:[], appendChild(n){ b.children.push(n); }};
  Object.defineProperty(b, "textContent", {
    get(){ return ""; },
    set(v){ b.children.length = 0; }});
  return b;
}
"""

    def test_a_region_rebuilds_only_when_its_signature_changes(self):
        out = self._run(self._BOX + """
const b = box0();
let builds = 0;
const mk = (x) => { builds++; x.scrollTop = 0; x.appendChild(1); };
b.scrollTop = 40;
redraw(b, "A", mk);
const after_first = [builds, b.scrollTop];
b.scrollTop = 73;
redraw(b, "A", mk);
const after_same = [builds, b.scrollTop];
redraw(b, "B", mk);
console.log(JSON.stringify({after_first, after_same,
                            after_new: [builds, b.scrollTop], sig: b.dataset.sig}));
""")
        self.assertEqual(out["after_first"], [1, 40], "first paint must build once and keep the scroll")
        self.assertEqual(out["after_same"], [1, 73], "an unchanged region must not be rebuilt at all")
        self.assertEqual(out["after_new"], [2, 73], "a changed region rebuilds, scroll still restored")
        self.assertEqual(out["sig"], "B")

    def test_set_text_writes_only_when_the_text_is_different(self):
        out = self._run("""
const seen = [];
function node0(v){
  const o = {writes:0, _v:v};
  Object.defineProperty(o, "textContent", {get(){ return o._v; },
                                           set(x){ o._v = x; o.writes++; }});
  return o;
}
const t = node0("same");
setText(t, "same");
seen.push(t.writes);
setText(t, "new");
seen.push(t.writes, t._v);
setText(null, "ignored");
seen.push("alive");
console.log(JSON.stringify({seen}));
""")
        self.assertEqual(out["seen"][0], 0, "unchanged text must not touch the DOM")
        self.assertEqual(out["seen"][1], 1)
        self.assertEqual(out["seen"][2], "new")
        self.assertEqual(out["seen"][3], "alive", "a missing node must be tolerated, not thrown on")

    def test_sig_of_survives_what_json_cannot_hold(self):
        out = self._run("""
const cyclic = {}; cyclic.self = cyclic;
console.log(JSON.stringify({cyc: typeof sigOf(cyclic),
                            arr: sigOf([1, "a", null]),
                            same: sigOf([{a:1}]) === sigOf([{a:1}])}));
""")
        self.assertEqual(out["cyc"], "string", "a cycle must degrade to a string, not throw")
        self.assertEqual(out["arr"], "[1,\"a\",null]")
        self.assertTrue(out["same"], "equal payloads need equal signatures")

    def test_every_list_the_tick_touches_goes_through_the_guard(self):
        js = _script(webapp.page())
        for region in ("librows", "cards", "upnext", "results", "recents", "taste",
                       "quick", "empty-acts", "filters"):
            self.assertTrue('redraw($("%s")' % region in js or
                            '$("%s");' % region in js, "no guard for " + region)
        # and the clearing that used to happen in each draw function now happens once
        self.assertLessEqual(js.count('.textContent = "";'), 4,
                             "draw functions are clearing boxes again")

    def test_a_typed_settings_box_is_not_overwritten_by_the_tick(self):
        js = _script(webapp.page())
        self.assertIn("document.activeElement !== node", js,
                      "the settings inputs are written on every tick again")


class TickTests(unittest.TestCase):
    """
    "the current UI doesnt change when the song changed, terminal says playing ...
    but UI says playing ..." - one frozen panel must not be able to look like a
    player that stopped, so the tick is isolated per region and errors are named.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = webapp.page()
        cls.js = _script(cls.html)

    def _helpers(self):
        start = self.js.index("function fail(where, e){")
        end = self.js.index("function draw(s){")
        return self.js[start:end]

    def _run(self, body):
        if not shutil.which("node"):
            self.skipTest("node is not installed here")
        shim = """
const S = {};
const pill = {textContent: "", cls: [], classList: {
  add(c) { if (!pill.cls.includes(c)) pill.cls.push(c); },
  remove(c) { pill.cls = pill.cls.filter((x) => x !== c); }},
};
const $ = (id) => (id === "jobpill" ? pill : null);
const report = [];
console.error = () => {};
"""
        program = shim + self._helpers() + "\n" + body
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(program)
            path = fh.name
        try:
            r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, (r.stderr or "")[-1500:])
            return __import__("json").loads(r.stdout.strip().splitlines()[-1])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_a_throwing_region_is_caught_and_named_on_the_pill(self):
        out = self._run("""
region("drawDetail", () => { throw new Error("kaboom"); });
console.log(JSON.stringify({broken: S.broken, pill: pill.textContent,
                           warn: pill.cls.includes("warn")}));
""")
        self.assertEqual(out["broken"], "drawDetail: kaboom")
        self.assertIn("panel broke", out["pill"])
        self.assertTrue(out["warn"], "the pill does not say anything is wrong")

    def test_the_regions_after_a_failure_still_draw(self):
        out = self._run("""
const order = [];
region("a", () => { order.push("a"); throw new Error("x"); });
region("b", () => order.push("b"));
region("c", () => order.push("c"));
console.log(JSON.stringify({order}));
""")
        self.assertEqual(out["order"], ["a", "b", "c"],
                         "one bad panel still costs the page the rest")

    def test_the_same_fault_is_reported_once_not_twice_a_second(self):
        out = self._run("""
const seen = [];
const spy = {textContent: "", classList: {add(c) { seen.push(c); }, remove() {}}};
for (let i = 0; i < 5; i++) region("drawPlayer", () => { throw new Error("same"); });
console.log(JSON.stringify({warn: 1, broken: S.broken, ticks: seen.length}));
""")
        self.assertEqual(out["broken"], "drawPlayer: same")
        self.assertEqual(out["warn"], 1)

    def test_every_region_the_tick_calls_is_wrapped(self):
        body = self.js[self.js.index("function draw(s){"):]
        body = body[:body.index("\n}\n") + 3]
        called = re.findall(r'region\("([a-zA-Z-]+)", \(\) => ([A-Za-z]+)\(', body)
        names = [fn for _label, fn in called]
        for fn in ("drawQuick", "drawChips", "drawUpNext", "drawLibrary", "drawDetail",
                   "drawPlayer", "drawResults", "drawTaste", "drawSettings",
                   "drawRecents", "drawLog", "drawEmptyActs"):
            self.assertIn(fn, names, fn + " is drawn outside the guard")
        bare = [m for m in re.findall(r"^  (draw[A-Z][A-Za-z]*)\(", body, re.M)]
        self.assertEqual(bare, [], "unwrapped draw calls are back: " + ", ".join(bare))

    def test_the_page_listens_and_still_polls(self):
        self.assertIn('new EventSource("/api/stream")', self.js,
                      "the push channel the server offers is unused")
        self.assertIn("es.onerror", self.js, "a dead stream must fall back, not freeze")
        poll = self.js[self.js.index("async function poll(){"):]
        poll = poll[:poll.index("\n}") + 3]
        self.assertIn("\n  setTimeout(poll,", poll,
                      "the reschedule moved inside the try: one failed fetch and the "
                      "page stops updating forever")
        self.assertIn("the DJ stopped answering", poll,
                      "a server that stops answering must be said out loud")

    def test_the_queue_header_can_clear_the_list(self):
        self.assertIn('data-action="clear_queue"', self.html)
        self.assertIn('id="clearq"', self.html)
        self.assertIn('cq.hidden = !(s.queued || rows.length)', self.js,
                      "the button sits there offering nothing on an empty queue")


class ArtSlotTests(unittest.TestCase):
    """
    "the ui is a mess ... the cover not HD its fucking pixelated" - the two ways a
    page like this fails artwork are a file too small for the box it is put in, and
    a box with no rule making the picture fill it. Both are cheap to lose again, so
    they are pinned in the one place that can see them without a browser.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = webapp.page()
        cls.js = _script(cls.html)
        cls.css = re.search(r"<style>([\s\S]*)</style>", cls.html).group(1)

    def rule(self, selector):
        m = re.search(re.escape(selector) + r"\{([^}]*)\}", self.css)
        self.assertTrue(m, f"{selector} is not in the stylesheet at all")
        return m.group(1)

    def test_every_box_that_holds_a_cover_covers_it(self):
        for sel in (".tile img", ".cover img"):
            block = self.rule(sel)
            for want in ("position:absolute", "inset:0", "width:100%", "height:100%",
                         "object-fit:cover"):
                self.assertIn(want, block, f"{sel} leaves the picture {want} unset")
        for sel in (".tile", ".cover"):
            block = self.rule(sel)
            self.assertIn("position:relative", block,
                          f"{sel} lets its image escape to some other box")
            self.assertIn("overflow:hidden", block, f"{sel} lets the corners show")

    def test_the_picture_covers_the_letter_it_replaces(self):
        for sel in (".tile img", ".cover img"):
            self.assertIn("z-index", self.rule(sel),
                          f"{sel} draws under the initial, so both show at once")

    def test_a_card_asks_for_the_card_sized_file(self):
        self.assertIn("function art(node, track, px, field)", self.js)
        self.assertIn("art(tile, t, 44, \"art_card\")", self.js,
                      "the grid tile is back to borrowing the row thumbnail")
        self.assertIn("track[field]", self.js)

    def test_the_sidebar_filter_row_wraps_instead_of_clipping(self):
        self.assertIn("flex-wrap:wrap", self.rule(".filters"))
        self.assertIn("white-space:nowrap", self.rule(".chip"),
                      "a chip that breaks across two lines is unreadable")

    def test_the_shortcut_tiles_are_marks_not_letter_blocks(self):
        # a 64px gradient square with one letter in it is a placeholder, and a row of
        # them out-shouts every cover on the page; the chip is a 30px tinted mark
        chip = self.rule(".qp .qi")
        self.assertIn("width:30px", chip)
        self.assertIn("border-radius:50%", chip)
        self.assertNotIn(".qp .tile{", self.css,
                         "the letter block is back on top of the shortcut row")

    def test_panel_microcopy_survives_the_blurred_cover_behind_it(self):
        # the detail panel sits on the backdrop, so its labels need a colour of
        # their own: --faint on a blurred album cover is the washed-out look
        self.assertNotIn("var(--faint)", self.rule(".sect h3"))
        self.assertIn("color:var(--muted)", self.rule(".sect h3"))
        self.assertIn("letter-spacing", self.rule(".sect h3"))
        why = self.rule(".why")
        self.assertIn("color:#e8e8e8", why)
        self.assertIn("line-height", why)

    def test_the_playing_colour_comes_from_the_cover_not_a_palette_hash(self):
        # "match the color with majority color every single cover": the page must
        # read the cover's dominant colours and feed --tint/--tint2 from them, with
        # the palette only as a fallback while a picture has not yet been decoded.
        self.assertIn("const coverColors = {}", self.js,
                      "there is no per-video cover colour cache")
        self.assertIn("function dominantColor(img, vid)", self.js,
                      "the cover's dominant colour is never extracted")
        self.assertIn("function normColor", self.js,
                      "the extracted colour is never normalised to a usable accent")
        self.assertIn("img.onload = () => dominantColor(img, vid)", self.js,
                      "a loaded cover never samples its own colour")
        self.assertIn("coverColors[track.id]", self.js,
                      "backdrop() does not read the cover's own colour")
        self.assertNotIn("dominantColor(img, vid) ;", self.js)
        # the wash gradient composes tint+alpha as 8-digit hex, so the colour must be
        # hex (an rgb() string would make `tint+"00"` invalid)
        self.assertIn('"#" + hx(r) + hx(g) + hx(b)', self.js,
                      "the dominant colour is not emitted as hex, so the wash breaks")


if __name__ == "__main__":
    unittest.main()
