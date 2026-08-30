"""
The browser skin's document: one HTML file, inlined CSS and JS, no CDN, no network.

This is the whole front end now. The Tk window it replaced is gone (see README): a
browser gives real vector icons, real fonts, hover states, an animation loop, and
a backdrop that can be blurred, none of which Tk can do, and the app only has to
be written once. The layout is deliberately the one everybody already knows -
library on the left, content in the middle, now-playing on the right, transport
pinned at the bottom - because a free DJ that needs a manual is a DJ nobody uses.

Why it is generated instead of shipped as a static asset: the palette comes from
`viewmodel.py`, the same constants every other surface reads, so a colour change is
one edit and cannot leave two skins disagreeing. `@@TOKEN@@` replacement (not
str.format) because CSS and JS are full of braces.

Everything is offline-safe: system fonts, inline SVG paths, no request that leaves
the machine except the local API. No server data is ever assigned with innerHTML -
titles and artist names go in as textContent, so a track called
`<img onerror=alert(1)>` is a song title and nothing else.
"""
from __future__ import annotations

import json

import viewmodel as vm

ICONS = {
    "prev": '<svg viewBox="0 0 24 24"><path d="M7 5h2v14H7zM19 5v14L9.5 12z"/></svg>',
    "next": '<svg viewBox="0 0 24 24"><path d="M15 5h2v14h-2zM5 5v14l9.5-7z"/></svg>',
    "play": '<svg viewBox="0 0 24 24"><path d="M7 4.5v15L20 12z"/></svg>',
    "pause": ('<svg viewBox="0 0 24 24"><path d="M7 5h3.4v14H7zM13.6 5H17v14h-3.4z"/>'
              '</svg>'),
    "stop": '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
    "heart": ('<svg viewBox="0 0 24 24"><path d="M12 20.6 3.4 12.4A5 5 0 0 1 12 6.2a5 5 0 '
              '0 1 8.6 6.2z"/></svg>'),
    "library": ('<svg viewBox="0 0 24 24"><path d="M4 4h2.6v16H4zM8.4 4H11v16H8.4zM13.2 '
                '4.3l2.5.7-3.4 15.6-2.5-.7z"/></svg>'),
    "heart_o": ('<svg viewBox="0 0 24 24"><path d="M12 18.9 5 12.2a3.6 3.6 0 0 1 5.1-5.1l1.9 '
                '1.8 1.9-1.8A3.6 3.6 0 0 1 19 12.2z" fill="none" stroke="currentColor" '
                'stroke-width="1.7"/></svg>'),
    "thumb_down": ('<svg viewBox="0 0 24 24"><path d="M6.5 4H5a2 2 0 0 0-1.4.6A2 2 0 0 0 3 '
                   '6v5a2 2 0 0 0 2 2h2.3l-1 3.9A1.7 1.7 0 0 0 8 18.7a1.7 1.7 0 0 0 '
                   '1.6-1.3l1-3.4h4.3a2.1 2.1 0 0 0 2-2.7L15.3 5.6A2 2 0 0 0 13.4 4H6.5z'
                   'M5 6h1.6L9 12.7 8 16l-1.2-.9 1.1-4.1H5V6zm11 9.5v-1.7h1.5V6h1.6v7.8z" '
                   'fill="none" stroke="currentColor" stroke-width="1.6"/></svg>'),
    "queue": ('<svg viewBox="0 0 24 24"><path d="M4 6h11v2H4zM4 11h11v2H4zM4 16h7v2H4zM18 '
              '10v6.2a2.6 2.6 0 1 1-1.6-2.4V10z"/></svg>'),
    "shuffle": ('<svg viewBox="0 0 24 24"><path d="M3 6h4.5l9 12H21v-2h-3l-2.6-3.5 1.5-2H21V8'
                'h-4.5l-1.4 1.9L6.9 6H3zM3 16h3.9l1.5-2 2.6 3.5L6.9 18H3zM17 4l3 3-3 3z"/>'
                '</svg>'),
    "repeat": ('<svg viewBox="0 0 24 24"><path d="M7 6.5h9V4l4 3.8-4 3.8V9.3H6v-2h1V6.5zM17 '
               '17.5H8v2.5l-4-3.8 4-3.8v2.3h10v2zM6 14.7h1v2H6z"/></svg>'),
    "repeat_one": ('<svg viewBox="0 0 24 24"><path d="M7 6.5h9V4l4 3.8-4 3.8V9.3H6v-2h1V6.5z'
                   'M17 17.5H8v2.5l-4-3.8 4-3.8v2.3h10v2zM11.6 12.4h1.3v5.2h-1.1l-1.3-.8v-1'
                   'h1.1z"/></svg>'),
    "search": ('<svg viewBox="0 0 24 24"><path d="M10.5 3a7.5 7.5 0 1 0 4.6 13.5l4.3 4.3 '
               '1.4-1.4-4.3-4.3A7.5 7.5 0 0 0 10.5 3m0 2a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 '
               '0-11" fill="currentColor" stroke="none"/></svg>'),
    "open": ('<svg viewBox="0 0 24 24"><path d="M12 3v2h5.6L10 12.6 11.4 14 19 6.4V12h2V3zM5 '
            '5v14h14v-4h-2v2H7V7h2V5z"/></svg>'),
    "mix": '<svg viewBox="0 0 24 24"><path d="M4 5h2v14H4zM18 5h2v14h-2zM9.5 7l5 5-5 5z"/></svg>',
    "vol": ('<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9zM16 8.5a4 4 0 0 1 0 7v-2a2 '
            '2 0 0 0 0-3zM18.5 6a7 7 0 0 1 0 12v-2.1a4.9 4.9 0 0 0 0-7.8z"/></svg>'),
    "vol_mute": '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9zM15.4 9.6 16.8 8l5 5-1.4 '
                '1.4zM20.4 8 21.8 9.4l-5 5-1.4-1.4z"/></svg>',
    "dot": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/></svg>',
    "home": ('<svg viewBox="0 0 24 24"><path d="M12 3.1 2.8 11l1.3 1.5L5 11.8V21h5v-6h4v6h5'
             'v-9.2l.9.7 1.3-1.5z"/></svg>'),
    "plus": '<svg viewBox="0 0 24 24"><path d="M11 4.5h2V11h6.5v2H13v6.5h-2V13H4.5v-2H11z"/></svg>',
    "chev_l": '<svg viewBox="0 0 24 24"><path d="M15.4 4.6 7.9 12l7.5 7.4 1.5-1.5L10.8 12l6.1'
              '-5.9z"/></svg>',
    "chev_r": '<svg viewBox="0 0 24 24"><path d="M8.6 4.6 16.1 12l-7.5 7.4-1.5-1.5 6.1-5.9'
              '-6.1-5.9z"/></svg>',
    "close": '<svg viewBox="0 0 24 24"><path d="M6.4 5 12 10.6 17.6 5 19 6.4 13.4 12 19 17.6'
             ' 17.6 19 12 13.4 6.4 19 5 17.6 10.6 12 5 6.4z"/></svg>',
    "expand": '<svg viewBox="0 0 24 24"><path d="M4 4h6.5v2H6v4.5H4zM20 4v6.5h-2V6h-4.5V4zM4'
              ' 13.5h2V18h4.5v2H4zM20 13.5V20h-6.5v-2H18v-4.5z"/></svg>',
    "clock": ('<svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18m0 2a7 7 '
              '0 1 1 0 14 7 7 0 0 1 0-14M11 7v5.6l4.2 2.4 1-1.7-3.2-1.8V7z"/></svg>'),
    "dots": ('<svg viewBox="0 0 24 24"><circle cx="5.5" cy="12" r="1.6"/><circle cx="12" '
             'cy="12" r="1.6"/><circle cx="18.5" cy="12" r="1.6"/></svg>'),
    "sparkle": ('<svg viewBox="0 0 24 24"><path d="M11 2.6 12.7 7 17 8.7 12.7 10.4 11 14.8 '
                '9.3 10.4 5 8.7 9.3 7zM18 14l1.1 2.9 2.9 1.1-2.9 1.1L18 22l-1.1-2.9-2.9-1.1 '
                '2.9-1.1z"/></svg>'),
    "lyrics": ('<svg viewBox="0 0 24 24"><path d="M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 '
               '0 0 1 3-3m-6 8a6 6 0 0 0 5 5.9V20H8v2h8v-2h-3v-3.1A6 6 0 0 0 18 11h-2a4 4 0 '
               '0 1-8 0z"/></svg>'),
    "devices": ('<svg viewBox="0 0 24 24"><path d="M3 5h12v8H3zm2 2v4h8V7zm10 2h6v9h-6zm2 '
                '1.6v5.8h2.8V8.6z"/></svg>'),
    "sort": '<svg viewBox="0 0 24 24"><path d="M4 6h16v2H4zm0 5h11v2H4zm0 5h7v2H4z"/></svg>',
    "check": '<svg viewBox="0 0 24 24"><path d="M9.6 16.2 5.4 12 4 13.4l5.6 5.6 12-12L20.2 '
             '6z"/></svg>',
    "verified": ('<svg viewBox="0 0 24 24"><path d="M12 2.2l2.3 1.9 3-.1.8 2.9 2.4 1.8-1.1 '
                 '2.8 1.1 2.8-2.4 1.8-.8 2.9-3-.1L12 21.8l-2.3-1.9-3 .1-.8-2.9L3.5 15.3l1.1'
                 '-2.8-1.1-2.8 2.4-1.8.8-2.9 3 .1z"/><path d="M10.8 15.4 7.6 12.2 9 10.8l1.8 '
                 '1.8 4.2-4.2 1.4 1.4z" fill="#121212"/></svg>'),
}

COLORS = {
    "@@BG@@": getattr(vm, "BG", "#121212"),
    "@@PANEL@@": getattr(vm, "PANEL", "#181818"),
    "@@CARD@@": getattr(vm, "CARD", "#232323"),
    "@@EDGE@@": vm.PANEL_EDGE,
    "@@HOVER@@": vm.HOVER,
    "@@INPUT@@": vm.INPUT_BG,
    "@@TEXT@@": vm.TEXT,
    "@@MUTED@@": vm.MUTED,
    "@@FAINT@@": vm.MUTED_DK,
    "@@ACCENT@@": vm.ACCENT,
    "@@ACCENT_DK@@": vm.ACCENT_DK,
    "@@PLAYING@@": vm.PLAYING_BG,
    "@@ERROR@@": vm.ERROR,
    "@@HEART@@": getattr(vm, "HEART", "#1ed760"),
    "@@TILES@@": ",".join(vm.TILE_PALETTE),
}

# --------------------------------------------------------------------- stylesheet
# The grid the screenshot everybody knows: body 8px padding, three black rounded
# panels, player pinned at 72px. `--tint` is set from JS to the palette colour of
# whatever is playing, so the whole window goes quietly grey-blue for one artist and
# warm for the next, the way an album artwork lights a room.
CSS = """
:root{--bg:@@BG@@;--panel:@@PANEL@@;--card:@@CARD@@;--edge:@@EDGE@@;--hover:@@HOVER@@;
--input:@@INPUT@@;--text:@@TEXT@@;--muted:@@MUTED@@;--faint:@@FAINT@@;--accent:@@ACCENT@@;
--accent-dk:@@ACCENT_DK@@;--playing:@@PLAYING@@;--error:@@ERROR@@;--heart:@@HEART@@;
--tiles:@@TILES@@;--tint:#1db954;--tint2:#169c46;--glass:rgba(255,255,255,.05);
--r:14px;--rax:22px}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);overflow:hidden;
font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,"Cantarell","DejaVu Sans",sans-serif;
display:grid;grid-template-rows:1fr 72px;padding:8px;gap:8px}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
input{font:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
svg{fill:currentColor;display:block}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-thumb{background:#4d4d4d;border:3px solid transparent;
background-clip:padding-box;border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:#7a7a7a;background-clip:padding-box}
::-webkit-scrollbar-track{background:transparent}

.app{display:grid;grid-template-columns:300px minmax(0,1fr) 400px;gap:10px;min-height:0;
position:relative}
/* a liquid-glass card: a translucent shell with a soft edge and an inner highlight,
   so the blurred cover behind it reads as frosted glass. The right panel must NEVER
   read as a transparent box over content - the report "give a transparent box" was
   the panel's own backdrop letting the middle column bleed through - so it gets a
   real, mostly-opaque base that keeps the frosted look without the see-through. */
.panel{background:linear-gradient(165deg,rgba(30,30,36,.94),rgba(14,14,17,.97));
border:1px solid rgba(255,255,255,.07);border-radius:var(--r);min-height:0;
position:relative;isolation:isolate;overflow:hidden;
box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 24px 70px rgba(0,0,0,.55);
backdrop-filter:blur(30px) saturate(1.35)}

/* ---- the blurred cover: the one thing a terminal skin could never do ---- */
/* two layers so a track change is a crossfade, not a cut: the base keeps the
   outgoing cover while the float carries the incoming one and they trade opacity.
   CRITICAL for smoothness: the drift animation runs on `transform` ONLY, never on
   `filter` or `background`. `filter:blur(72px)` is expensive to rasterize, and if a
   keyframe also animates filter/hue-rotate or a gradient's `at var()` position, the
   browser has to re-blur this full-screen layer every frame - that is the jank. A
   plain transform just slides the already-blurred texture in the compositor for free. */
.bg{position:absolute;inset:-14% -10%;z-index:-2;background-size:cover;
background-position:center top;filter:blur(72px) saturate(1.9) hue-rotate(-7deg);
opacity:.66;transform:scale(1.08);transition:opacity .5s ease;will-change:transform;
animation:tintdrift 14s ease-in-out infinite alternate}
/* the float layer is a second copy of the same box: identical size and blur, so
   the crossfade below never changes the look, only which cover wins. It sits
   above the base (later in the DOM at the same z-index) and is hidden until a
   track changes, at which point it fades in with the new cover over the old one. */
.bg2{position:absolute;inset:-14% -10%;z-index:-2;background-size:cover;
background-position:center top;filter:blur(72px) saturate(1.9) hue-rotate(-7deg);
opacity:0;transform:scale(1.08);transition:opacity .45s ease;will-change:transform;
animation:tintdrift 14s ease-in-out infinite alternate}
/* the coloured glow lives on a STATIC layer (fixed gradient points, no animated
   filter / no animated custom properties). It moves only because the blurred layer
   above it is slid around by transform, so the two tints appear to travel across
   the page without ever re-blurring. */
.bg::after,.bg2::after{content:"";position:absolute;inset:-18% -14%;
background:radial-gradient(120% 90% at 30% 20%,var(--tint) 0%,transparent 55%),
radial-gradient(110% 80% at 80% 85%,var(--tint2) 0%,transparent 55%);
mix-blend-mode:screen;opacity:.62}
/* the playing colour breathes: the cover-derived tint drifts slowly across the
   screen. 14s a side reads as ambient, not a busy page; and because only
   `transform` is animated (translate3d + a slight scale), the compositor slides
   the blurred wash and the glow around without re-computing the blur. */
@keyframes tintdrift{
0%{transform:translate3d(-2.5%,1.5%,0) scale(1.06)}
100%{transform:translate3d(2.5%,-1.5%,0) scale(1.1)}}
/* with no cover yet the wash is the two palette colours that tile would have used,
   so the page is never the flat grey it looked before a thumbnail landed. We only
   drop the cover image, not the glow: the tinted wash behind it stays and moves. */
.bg.flat,.bg2.flat{background-image:none!important;opacity:.62}
.scrim{position:absolute;inset:0;z-index:-1;
background:linear-gradient(180deg,rgba(0,0,0,.26),rgba(18,18,18,.8) 42%,rgba(14,14,16,.96) 78%)}

/* ---- left: your library ---- */
.side{background:linear-gradient(165deg,rgba(28,28,34,.9),rgba(14,14,16,.96));
display:flex;flex-direction:column;min-height:0}
.side-head{display:flex;align-items:center;justify-content:space-between;padding:14px 14px 6px}
.side-head .lt{display:flex;align-items:center;gap:10px;font-weight:700;font-size:15.5px}
.side-head .lt svg{width:22px;height:22px;color:var(--muted)}
.iconbtn{width:32px;height:32px;border-radius:999px;display:grid;place-items:center;
color:var(--muted)}
.iconbtn svg{width:17px;height:17px}
.iconbtn:hover{color:var(--text);background:var(--hover)}
.iconbtn.on{color:var(--accent)}
/* a loved song wears the palette's heart, not the green of a button */
#b-love.on{color:var(--heart)}
.card .a svg,.row .ar svg{width:13px;height:13px;color:#3ea4f5;flex:none}
.iconbtn.big{width:36px;height:36px}
.iconbtn.big svg{width:20px;height:20px}
.filters{display:flex;gap:8px;padding:8px 14px;flex-wrap:wrap;scrollbar-width:none}
.filters::-webkit-scrollbar{display:none}
.chip{background:var(--hover);border-radius:999px;padding:6px 13px;color:var(--text);
font-size:13px;white-space:nowrap;flex:none}
.chip:hover{background:#2a2a2a}
.chip.on{background:#fff;color:#000;font-weight:600}
.libsort{display:flex;align-items:center;gap:6px;padding:2px 14px 8px;color:var(--muted);
font-size:12.5px}
.libsort button{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12.5px}
.libsort button:hover{color:var(--text)}
.libsort svg{width:14px;height:14px}
.librows{overflow-y:auto;padding:0 8px 14px;display:flex;flex-direction:column;gap:2px;
min-height:0;flex:1}
.lib{display:grid;grid-template-columns:48px minmax(0,1fr) 26px;gap:12px;align-items:center;
padding:7px 6px;border-radius:6px}
.lib:hover{background:var(--hover)}
.lib .name{color:var(--text);font-size:14px;font-weight:500;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.lib .meta{color:var(--muted);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;display:flex;align-items:center;gap:6px}
.lib .meta svg{width:13px;height:13px;color:#3ea4f5;flex:none}
.lib.sel{background:var(--hover)}
.lib .go{opacity:0;transition:opacity .12s}
.lib:hover .go{opacity:1}
.tile{width:48px;height:48px;border-radius:5px;overflow:hidden;flex:none;display:grid;
place-items:center;font-weight:800;color:#0a0a0a;background:var(--card);position:relative}
.tile.sq{border-radius:50%}
.tile img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:1}
/* any box that holds a cover is a box the picture has to fill. The hero and the
   detail cover are .cover, not .tile, and had no rule of their own: the img sat at
   its natural size in the corner on top of the gradient, which is the "what is that
   little square" look. Letters sit behind it (z-index) so they only show alone. */
.cover{position:relative;overflow:hidden}
.cover img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
display:block;z-index:1}
.tile.big{width:100%;height:100%;border-radius:6px;font-size:64px}
.empty{color:var(--faint);font-size:13px;padding:18px 14px;line-height:1.6}
.empty b{display:block;color:var(--text);font-size:14px;margin-bottom:6px}

/* ---- middle: content ---- */
/* the panel holds the wash, the scroller holds the page. They are separate on
   purpose: an absolutely positioned backdrop inside a scroll container is sized
   against the *content* (3 000 px on a long queue), so the cover was cropped to a
   slice and scrolled off, which is why the blur looked absent below the fold. */
#uph{display:flex;align-items:baseline;gap:6px}
.main{display:flex;flex-direction:column;min-height:0}
.scroller{overflow-y:auto;padding:0 0 22px;flex:1;min-height:0;display:flex;
flex-direction:column}
.top{display:flex;align-items:center;gap:12px;padding:10px 16px;position:sticky;top:0;z-index:6;
background:linear-gradient(180deg,rgba(0,0,0,.42),rgba(0,0,0,0))}
.main.up .top{background:rgba(10,10,10,.82);backdrop-filter:blur(14px);
box-shadow:0 1px 0 rgba(255,255,255,.06)}
.navb{display:flex;gap:8px}
.navb .iconbtn{background:rgba(0,0,0,.5)}
.search{flex:1;display:flex;align-items:center;gap:10px;background:var(--input);
border:1px solid transparent;border-radius:999px;padding:0 14px;max-width:520px;height:42px}
.search:focus-within{border-color:#fff;background:#2a2a2a}
.search svg{width:18px;height:18px;color:var(--faint);flex:none}
.search input{flex:1;background:none;border:0;color:var(--text);outline:none;font:inherit;
font-size:14.5px;min-width:0}
.search input::placeholder{color:var(--faint)}
.topright{display:flex;align-items:center;gap:8px;margin-left:auto}
.pill{border:1px solid var(--edge);border-radius:999px;padding:5px 11px;font-size:12px;
color:var(--muted);white-space:nowrap;max-width:30ch;overflow:hidden;text-overflow:ellipsis;
background:rgba(0,0,0,.35)}
.pill.warn{border-color:var(--error);color:#ff8a8a}
.pill.good{border-color:var(--accent);color:var(--accent)}
.avatar{width:32px;height:32px;border-radius:50%;background:var(--accent);color:#000;
font-weight:800;display:grid;place-items:center;font-size:13.5px}
.avatar:hover{transform:scale(1.06)}
.content{padding:0 16px}
.greet{font-size:26px;font-weight:800;margin:6px 0 14px;letter-spacing:-.4px;
display:flex;align-items:center;gap:12px}
.greet>span:first-child{flex:1;text-align:left}
h2{font-size:19px;font-weight:700;margin:22px 0 12px;letter-spacing:-.2px}
h2 .sub{color:var(--faint);font-size:12.5px;font-weight:400;margin-left:8px}
.quick{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:10px}
.qp{display:flex;align-items:center;gap:0;background:rgba(255,255,255,.09);border-radius:6px;
height:56px;overflow:hidden;position:relative;transition:background .12s}
.qp:hover{background:rgba(255,255,255,.18)}
.qp .qt{flex:1;min-width:0;padding:0 10px;font-weight:700;font-size:14px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* the right-hand chip: a 30px round mark, tinted from the same seed the tiles use.
   A 64px gradient block with one letter in it is a placeholder, and six of them in
   a row is what made the top of this page look unfinished next to the covers. */
.qp .qi{position:relative;width:30px;height:30px;border-radius:50%;flex:none;
margin-right:12px;display:grid;place-items:center;transition:opacity .12s}
.qp .qi svg{width:14px;height:14px;color:#fff;opacity:.92}
/* on hover the play control takes the chip's place, so the tile never grows */
.qp .fab{position:absolute;right:11px;width:30px;height:30px;opacity:0;
transform:translateY(4px);transition:opacity .12s,transform .12s}
.qp:hover .qi{opacity:0}
.qp:hover .fab{opacity:1;transform:none}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:18px}
.card{background:var(--card);border-radius:8px;padding:12px;position:relative}
.card:hover{background:#282828}
/* while the artwork lane is still fetching this row's file, sweep a highlight
   across the box: "coming" instead of "nothing here" */
.card .cover.pending::after{content:"";position:absolute;inset:0;z-index:2;
background:linear-gradient(100deg,transparent 22%,rgba(255,255,255,.07) 50%,
transparent 78%);animation:sweep 1.5s ease-in-out infinite}
@keyframes sweep{0%{transform:translateX(-70%)}100%{transform:translateX(70%)}}
.card .cover{position:relative;aspect-ratio:1/1;border-radius:6px;overflow:hidden;
background:var(--input);box-shadow:0 8px 24px rgba(0,0,0,.5)}
.card .cover .tile{width:100%;height:100%;border-radius:6px;font-size:44px}
.card .t{margin-top:11px;font-weight:700;font-size:14.5px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.card .a{color:var(--muted);font-size:12.5px;margin-top:3px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.fab{width:44px;height:44px;border-radius:50%;background:var(--tint);color:#fff;
display:grid;place-items:center;box-shadow:0 8px 22px rgba(0,0,0,.5);
transition:background .5s ease,transform .16s ease,box-shadow .16s ease}
.fab svg{width:20px;height:20px}
.fab:hover{transform:scale(1.06);background:var(--tint2);box-shadow:0 10px 26px rgba(0,0,0,.55)}
.fab:active{transform:scale(.98)}
.card .fab,.row .fab{position:absolute;right:14px;bottom:64px;opacity:0;
transform:translateY(8px);transition:opacity .16s ease,transform .16s ease}
.card:hover .fab{opacity:1;transform:none}
.row .fab{right:44px;bottom:50%;transform:translateY(50%) scale(.9);width:34px;height:34px}
.row .fab svg{width:15px;height:15px}
.row:hover .fab{opacity:1;transform:translateY(50%) scale(1)}

.rows{display:flex;flex-direction:column}
.row{display:grid;grid-template-columns:22px 40px minmax(0,1.6fr) minmax(0,1fr) 52px auto;
gap:14px;align-items:center;padding:6px 10px;border-radius:10px;position:relative}
.row:hover{background:rgba(255,255,255,.08)}
.row .n{color:var(--muted);font-size:13.5px;text-align:right;font-variant-numeric:tabular-nums}
.row .tile{width:40px;height:40px;font-size:14px}
.row .ti{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14.5px}
.row .ar{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;display:flex;align-items:center;gap:7px}
.row .du{color:var(--muted);font-size:13px;text-align:right;font-variant-numeric:tabular-nums}
/* the trailing action cluster (dislike, remove, more) slides in on hover, the
   same place a listener expects controls on a queue row in the apps they know */
.rowacts{display:flex;align-items:center;gap:2px;opacity:0;justify-content:flex-end;
transition:opacity .14s ease}
.row:hover .rowacts,.row:focus-within .rowacts{opacity:1}
.rowacts .iconbtn{width:26px;height:26px;border-radius:8px}
.rowacts .iconbtn svg{width:14px;height:14px}
.rowacts .grow{flex:0 0 12px}
.row .more{width:26px;height:26px}
.row.playing .ti{color:var(--tint)}
.row.playing .n{color:var(--tint)}
.eq{display:none;gap:2px;align-items:flex-end;height:13px;width:14px}
.row.playing .eq{display:flex}
.row.playing .n span{display:none}
.eq i{width:3px;background:var(--tint);height:4px;animation:eq .9s ease-in-out infinite;
border-radius:1px;transition:background .5s ease}
.eq i:nth-child(2){animation-delay:.18s}
.eq i:nth-child(3){animation-delay:.36s}
@keyframes eq{0%,100%{height:4px}30%{height:13px}60%{height:7px}}
.badge{display:inline-block;background:rgba(255,255,255,.12);border-radius:4px;padding:1px 6px;
margin-left:7px;font-size:11px;color:var(--muted)}

/* ---- right: now playing ---- */
.detail{display:flex;flex-direction:column;min-height:0}
.dhead{display:flex;align-items:center;justify-content:space-between;padding:12px 14px 0}
.dhead b{font-size:14px}
.dhero{padding:10px 16px 14px}
.dhero .cover{position:relative;aspect-ratio:1/1;max-height:300px;border-radius:8px;
overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.6);background:var(--input)}
.dhero .cover .tile{width:100%;height:100%;border-radius:8px;font-size:70px}
.dtitle{font-size:22px;font-weight:800;line-height:1.25;margin:14px 0 6px;
word-break:break-word}
.dartist{display:flex;align-items:center;gap:6px;color:var(--text);font-size:14px;font-weight:600}
.dartist svg{width:15px;height:15px;color:#3ea4f5}
/* the Now Playing artist as a link to the in-app artist page: a button, but one
   that reads as the name, and only grows a line on hover so it never shouts */
.plink{font:inherit;color:var(--text);padding:0;border-bottom:1px solid transparent;
transition:border-color .12s}
.plink:hover{color:#fff;border-bottom-color:rgba(255,255,255,.4)}
/* the right panel's content is one safe scroll container: `overflow-y:auto` plus a
   definite height (flex:1;min-height:0) so it always scrolls inside the panel and
   never walks the page. A thin visible scrollbar keeps it obvious you can get back
   up to Now Playing, which is what "I can't scroll up anymore" was about. */
.dbody{overflow-y:auto;overflow-x:hidden;padding:0 16px 18px;flex:1;min-height:0;
scrollbar-width:thin;overscroll-behavior:contain}
.sect{margin-top:16px}
/* the panel's microcopy is read against a blurred cover, so it gets a real colour
   and a wider track instead of the faint grey that disappeared behind the art */
.sect h3{margin:0 0 8px;font-size:11.5px;text-transform:uppercase;letter-spacing:.14em;
color:var(--muted);font-weight:700}
.why{background:rgba(0,0,0,.42);border-radius:8px;padding:11px 12px;font-size:13px;
line-height:1.55;color:#e8e8e8}
.why b{color:var(--text)}
/* Credits sits over a blurred cover that is usually near-black, so a faint grey
   label ("album", "released") disappears into it. Make every line white so the
   block reads on any background; the values stay a touch softer than the labels
   so the pair is still legible as label-vs-value rather than one white wall. */
.kv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 14px;font-size:13px}
.kv dt{color:var(--text);font-weight:600}
.kv dd{margin:0;color:#fff;overflow-wrap:anywhere;opacity:.92}
.mini{display:flex;flex-direction:column;gap:2px}
.mini .m{display:grid;grid-template-columns:26px minmax(0,1fr) auto;gap:10px;align-items:center;
padding:5px 6px;border-radius:6px;font-size:13px}
.mini .m:hover{background:var(--hover)}
/* the queue section that now lives in the right panel: a header with the track
   count on the left and the clear button on the right, above the row list */
.qh{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.qh h3{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.14em;
color:var(--muted);font-weight:700}
.qh .sub{color:var(--faint);font-size:12px;font-weight:400;letter-spacing:0;
text-transform:none;margin-left:2px}
.qh .iconbtn{margin-left:auto;width:26px;height:26px}
.qh .iconbtn svg{width:14px;height:14px}
#queue-sect .rows{display:flex;flex-direction:column;gap:1px}
/* the queue sits in a 400px panel, so its rows drop the row number and use the
   room for the song and a single actions column - the same shape Spotify's queue
   uses, where the title is the thing a person scans for. The title gets the lion's
   share (2.4fr) and the artist a quarter of it, so a song name reads without being
   chopped a few characters in; the column gap is a little wider so the title is
   not mashing into the artist's name. Each row is its own faint tile so the queue
   reads as a list of entries against the tinted backdrop instead of text floating
   on the blur. */
#upnext .row{grid-template-columns:40px minmax(0,2.4fr) minmax(0,1fr) 40px auto;gap:14px;
background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.05);
border-radius:10px;margin-bottom:6px;padding:7px 10px}
#upnext .row:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.12)}
#upnext .row .ti{font-weight:600}
#upnext .row .ar{color:var(--muted)}
#upnext .row .n{display:none}
#upnext .row .du{color:var(--muted);font-size:12px;text-align:right}
#upnext .row .fab{right:44px}
#upnext .row .rowacts .iconbtn{width:24px;height:24px}
.acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.btn{display:inline-flex;align-items:center;gap:8px;background:var(--hover);
border-radius:999px;padding:9px 16px;font-weight:700;font-size:13px}
.btn:hover{background:#2f2f2f}
.btn.prim{background:var(--accent);color:#000}
.btn.prim:hover{background:#1fdf64}
.btn svg{width:15px;height:15px}
.btn.on{color:var(--accent)}
.btn.ghost{border:1px solid var(--edge);background:none}
.btn.ghost:hover{border-color:var(--text);background:var(--hover)}
.bar2{height:4px;background:rgba(255,255,255,.16);border-radius:2px;overflow:hidden}
.bar2 i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--heart))}
.logbox{margin-top:12px;background:rgba(0,0,0,.35);border-radius:8px;padding:10px 12px;
font:11.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--muted);
max-height:220px;overflow:auto;white-space:pre-wrap}

/* ---- bottom transport ---- */
.player{display:grid;grid-template-columns:minmax(180px,1fr) minmax(320px,2fr)
minmax(180px,1fr);gap:14px;align-items:center;padding:0 14px}
.track{display:flex;align-items:center;gap:12px;min-width:0}
.track .ti{font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.track .ar{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.center{display:flex;flex-direction:column;align-items:center;gap:6px}
.ctrls{display:flex;align-items:center;gap:16px}
.ctrls .iconbtn.on{color:var(--tint);position:relative}
.ctrls .iconbtn.on::after{content:"";position:absolute;bottom:-1px;left:50%;
transform:translateX(-50%);width:4px;height:4px;border-radius:50%;background:var(--tint)}
.play{width:36px;height:36px;border-radius:50%;background:#fff;color:#000;display:grid;
place-items:center}
.play svg{width:17px;height:17px}
.play:hover{transform:scale(1.07)}
.seek{display:flex;align-items:center;gap:10px;width:100%;max-width:620px}
.seek small{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums;min-width:34px}
.seekbar{flex:1;height:14px;display:flex;align-items:center;cursor:pointer;position:relative}
.seekbar .t{height:4px;width:100%;background:rgba(255,255,255,.22);border-radius:2px;
overflow:hidden}
.seekbar .f{height:100%;width:0;background:linear-gradient(90deg,var(--tint),var(--tint2));
border-radius:2px;transition:background .5s ease}
.seekbar:hover .f,.seekbar.kb .f{background:linear-gradient(90deg,var(--tint),#fff)}
.seekbar .k{position:absolute;top:50%;width:12px;height:12px;border-radius:50%;background:#fff;
transform:translate(-50%,-50%) scale(0);transition:transform .12s;pointer-events:none}
.seekbar:hover .k{transform:translate(-50%,-50%) scale(1)}
.right{display:flex;align-items:center;gap:10px;justify-content:flex-end}
/* a pill that reads "0 stored, 0 d…" tells you nothing. The cache count is sized to
   its content and the icons beside it give way; under 1000px the bar is too tight for
   a number nobody acts on, so it steps aside entirely rather than truncating. */
.right .pill{flex:0 0 auto;max-width:none}
@media (max-width:1000px){.right .pill{display:none}}
.vol{display:flex;align-items:center;gap:8px}
/* a real volume slider: a track you can see and a thumb you can grab. The old
   styling only set a background on the input and hid the thumb until hover, so
   on Chromium there was nothing but a 4px grey line with an invisible knob. */
input[type=range]{-webkit-appearance:none;appearance:none;height:18px;width:92px;
background:transparent;outline:none;cursor:pointer}
input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:999px;
background:linear-gradient(90deg,var(--tint) var(--vol,70%),rgba(255,255,255,.2) var(--vol,70%))}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:13px;
height:13px;border-radius:50%;background:#fff;margin-top:-4.5px;
box-shadow:0 0 0 3px rgba(0,0,0,.35);opacity:1;transition:transform .12s}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.15)}
input[type=range]::-moz-range-track{height:4px;border-radius:999px;background:rgba(255,255,255,.2)}
input[type=range]::-moz-range-progress{height:4px;border-radius:999px;background:var(--tint)}
input[type=range]::-moz-range-thumb{width:12px;height:12px;border:0;border-radius:50%;
background:#fff;box-shadow:0 0 0 3px rgba(0,0,0,.35)}

/* ---- menus, toasts, misc ---- */
.menu{position:fixed;z-index:60;min-width:196px;background:#282828;border:1px solid rgba(0,0,0,.6);
border-radius:6px;padding:4px;box-shadow:0 10px 34px rgba(0,0,0,.7)}
.menu button{display:flex;align-items:center;gap:10px;width:100%;padding:9px 11px;
border-radius:4px;color:var(--muted);font-size:13.5px;text-align:left}
.menu button:hover{color:var(--text);background:rgba(255,255,255,.1)}
.menu button svg{width:15px;height:15px;flex:none}
.menu hr{border:0;border-top:1px solid rgba(255,255,255,.1);margin:4px 6px}
.menu .bad{color:#ff8a8a}
.toast{position:fixed;left:50%;bottom:96px;transform:translateX(-50%) translateY(12px);
background:#fff;color:#000;padding:10px 16px;border-radius:8px;font-size:13.5px;font-weight:600;
opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;z-index:70;max-width:80vw}
.toast.show{opacity:1;transform:translateX(-50%)}
.card.set{background:rgba(255,255,255,.04);border-radius:8px;padding:14px;margin-top:14px}
.sech{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--faint);
margin-bottom:10px}
.setrow{display:grid;grid-template-columns:104px minmax(0,1fr);gap:12px;align-items:center;
margin-top:10px}
.setrow span{color:var(--faint);font-size:12.5px}
.setrow input{background:var(--input);border:1px solid var(--edge);border-radius:6px;
padding:9px 11px;color:var(--text);min-width:0;outline:none;width:100%}
.setrow input:focus{border-color:#fff;background:#2a2a2a}
.taste{display:flex;flex-wrap:wrap;gap:7px}
.tk{display:inline-flex;align-items:center;gap:7px;background:rgba(255,255,255,.07);
border-radius:999px;padding:5px 11px;font-size:12.5px;color:var(--muted);max-width:100%}
.tk b{color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tk i{display:block;height:4px;width:34px;background:var(--accent);border-radius:2px;
opacity:.85;flex:none}
.tk.neg i{background:var(--error)}
body.busy .btn.prim{opacity:.55}
[hidden]{display:none !important}
/* a window too narrow to fit the three panels keeps the detail tucked away; the
   queue button reveals it as an overlay that slides over the content (the same
   small-screen shape the sidebar takes), so it never forces a 3-column grid at a
   width that cannot hold one and never leaves a cut-off, unscrollable panel. */
@media (max-width:1300px){.app{grid-template-columns:280px minmax(0,1fr)}
.detail{display:none}.app.wide .detail{display:flex;position:absolute;top:0;right:0;
bottom:0;width:400px;max-width:92vw;z-index:30;box-shadow:-18px 0 60px rgba(0,0,0,.6)}}
@media (max-width:1000px){.app{grid-template-columns:minmax(0,1fr)}.side{display:none}
.app.nav .side{display:flex;position:absolute;inset:8px auto 80px 8px;width:300px;z-index:20;
box-shadow:0 18px 60px rgba(0,0,0,.8)}.cards{grid-template-columns:repeat(auto-fill,
minmax(140px,1fr))}.row{grid-template-columns:22px 40px minmax(0,1fr) 52px auto}
.row .ar{display:none}.right .vol input{width:60px} .app.wide .detail{width:min(400px,100%)}}
@media (max-width:760px){.center{gap:2px}.player{grid-template-columns:1fr auto}
.right .iconbtn:nth-child(-n+2){display:none}.quick{grid-template-columns:1fr}}
"""

BODY = """
<div class="app" id="app">
 <aside class="side panel">
  <div class="side-head">
   <span class="lt">@@library@@<span>Your Library</span></span>
   <button class="iconbtn" id="lib-add" data-action="mix"
    title="Make a mix from the songs you loved">@@plus@@</button>
  </div>
  <div class="filters" id="filters"></div>
  <div class="libsort">
   <button id="lib-sort" title="Change the order">@@sort@@<span id="lib-sort-l">Recents</span></button>
   <span id="lib-count"></span>
  </div>
  <div class="librows" id="librows"></div>
 </aside>

 <main class="main panel" id="main">
  <div class="bg" id="bg-main"></div><div class="bg bg2" id="bg-main2"></div>
  <div class="scrim"></div>
  <div class="scroller" id="scroller">
  <div class="top">
   <div class="navb">
    <button class="iconbtn" id="nav-back" title="Back">@@chev_l@@</button>
    <button class="iconbtn" id="nav-fwd" title="Forward">@@chev_r@@</button>
   </div>
   <label class="search">@@search@@
    <input id="q" placeholder="What do you want to play?"
     autocomplete="off" spellcheck="false"></label>
   <div class="topright">
    <span class="pill" id="jobpill" hidden>building</span>
    <button class="pill" id="engine" data-view="library" title="How this mix is planned">brain</button>
    <button class="avatar" id="open-settings" title="DJ settings">DJ</button>
   </div>
  </div>
  <div class="content">
   <section id="view-home">
    <div class="greet">
     <span id="greet">Good evening</span>
     <button class="btn prim" id="playmix" data-action="playpause"
      title="Play / pause (space)">@@play@@<span id="playmixt">Play</span></button>
     <button class="btn ghost" id="topup" data-action="topup"
      title="Refill the queue from your request and your likes">@@shuffle@@<span>Refill</span></button>
    </div>
    <div class="quick" id="quick"></div>
    <h2 id="mixh">Made for you<span class="sub" id="mixs"></span></h2>
    <div class="cards" id="cards"></div>
   </section>
   <section id="view-search" hidden>
    <h2 id="search-h">Search<span class="sub" id="search-s"></span></h2>
    <div class="acts"><button class="btn prim" id="mixfromq">@@sparkle@@<span>Mix from this</span></button></div>
    <div class="rows" id="results"></div>
   </section>
   <section id="view-library" hidden>
    <h2>Your Library<span class="sub">what the DJ knows, and how it thinks</span></h2>
    <div class="card set">
     <div class="sech">taste profile</div>
     <div class="taste" id="taste"></div>
     <div class="sub" id="tastenote"></div>
     <div class="acts">
      <button class="btn prim" id="mixbtn">@@shuffle@@<span>Make a mix from this</span></button>
      <button class="btn ghost" id="wipebtn">@@stop@@<span>Forget my taste</span></button>
      <button class="btn ghost" id="undobtn" hidden>@@queue@@<span>Bring my taste back</span></button>
     </div>
    </div>
    <h2 id="lovedh">Loved songs</h2>
    <div class="rows" id="loved"></div>
    <h2>Search &amp; AI</h2>
    <div class="card set">
     <div class="sub" id="engine2"></div>
     <label class="setrow"><span>Gemini key</span><input id="in-key" type="password"
      placeholder="paste to save, leave blank to keep" autocomplete="off" spellcheck="false"></label>
     <label class="setrow"><span>Base URL</span><input id="in-base" type="text"
      placeholder="blank = Google; or http://localhost:11434/v1 for a local model"
      autocomplete="off" spellcheck="false"></label>
     <label class="setrow"><span>Model</span><input id="in-model" type="text"
      placeholder="gemini-3.5-flash" autocomplete="off" spellcheck="false"></label>
     <div class="acts">
      <button class="btn prim" id="savebtn">@@check@@<span>Save</span></button>
      <button class="btn ghost" data-action="test_brain">@@sparkle@@<span>Test the planner</span></button>
      <button class="btn ghost" id="unkeybtn" hidden>@@close@@<span>Remove the key</span></button>
     </div>
     <div class="sub" id="setnote"></div>
    </div>
    <h2>Activity</h2>
    <div class="logbox" id="log"></div>
   </section>
   <section id="view-history" hidden>
    <h2>Recently played<span class="sub">what this machine actually heard</span></h2>
    <div class="rows" id="recents"></div>
    <h2>The queue as the DJ built it</h2>
    <div class="logbox" id="log2"></div>
   </section>
   <section id="view-page" hidden>
    <div class="greet">
     <span id="page-title">Loading</span>
     <button class="btn ghost" id="page-back" title="Back to Now Playing">@@chev_l@@<span>Back</span></button>
    </div>
    <div class="sub" id="page-sub"></div>
    <div class="rows" id="page-rows"></div>
   </section>
  </div>
   </div>
 </main>

 <aside class="detail panel" id="detail">
  <div class="bg" id="bg-side"></div><div class="bg bg2" id="bg-side2"></div>
  <div class="dhead">
   <b>Now playing · Discover</b>
   <button class="iconbtn" id="detail-close" title="Hide">@@close@@</button>
  </div>
  <div class="dbody">
   <div class="dhero">
    <div class="cover" id="np-art"></div>
    <div class="dtitle" id="np-title">Nothing playing</div>
    <div class="dartist" id="np-by" hidden></div>
   </div>
   <div class="acts">
    <button class="btn ghost" data-action="radio" id="np-station">@@mix@@<span>Station</span></button>
    <button class="btn ghost" data-action="open" id="np-open">@@open@@<span>Spotube</span></button>
    <button class="btn ghost" id="np-album" title="Open this album in YouTube Music">@@devices@@<span>See album</span></button>
    <button class="btn ghost" data-action="stop" id="np-stop">@@stop@@<span>Stop</span></button>
   </div>
   <div class="sect"><h3>Why this song</h3><div class="why" id="np-why">
    type a mood above, or pick something on the left</div></div>
   <div class="sect"><h3>Credits</h3><dl class="kv" id="credits"></dl></div>
   <div class="sect"><h3 id="simh">In your likes</h3><div class="mini" id="simil"></div></div>
   <div class="sect" id="queue-sect">
    <div class="qh"><h3 id="uph">Queue<span class="sub" id="upc"></span></h3>
     <button class="iconbtn" id="clearq" data-action="clear_queue"
      title="Clear the queue (the song keeps playing)">@@close@@</button></div>
    <div class="rows" id="upnext"></div>
    <div class="acts" id="empty-acts"></div>
   </div>
  </div>
 </aside>
</div>

<footer class="player">
 <div class="track">
  <div class="tile" id="bar-art"></div>
  <div style="min-width:0">
   <div class="ti" id="bar-title">Not playing</div>
   <div class="ar" id="bar-artist"></div>
  </div>
  <button class="iconbtn" id="b-love" data-action="like" title="Love this song (l)">@@heart_o@@</button>
  <button class="iconbtn" id="b-more" title="More">@@dots@@</button>
 </div>
 <div class="center">
  <div class="ctrls">
   <button class="iconbtn" id="b-shuffle" data-action="shuffle" title="Shuffle (s)">@@shuffle@@</button>
   <button class="iconbtn" data-action="prev" title="Previous (p)">@@prev@@</button>
   <button class="play" id="b-play" data-action="playpause" title="Play / pause (space)">@@play@@</button>
   <button class="iconbtn" data-action="next" title="Next (n)">@@next@@</button>
   <button class="iconbtn" id="b-repeat" data-action="repeat" title="Repeat (r)">@@repeat@@</button>
  </div>
  <div class="seek">
   <small id="t0">0:00</small>
   <div class="seekbar" id="bar" role="slider" aria-label="Seek" tabindex="0">
    <div class="t"><div class="f" id="fill"></div></div><div class="k" id="knob"></div>
   </div>
   <small id="t1">0:00</small>
  </div>
 </div>
 <div class="right">
  <span class="pill" id="cache"></span>
  <button class="iconbtn" id="b-auto" data-action="auto" title="Keep mixing when the queue runs dry">@@sparkle@@</button>
  <button class="iconbtn" id="b-queue" title="Queue">@@queue@@</button>
  <button class="iconbtn" id="b-open" data-action="open" title="Open in Spotube">@@devices@@</button>
  <span class="vol">
   <button class="iconbtn" id="b-mute" title="Mute (m)">@@vol@@</button>
   <input type="range" id="vol" min="0" max="100" value="70" title="Volume">
  </span>
  <button class="iconbtn" id="b-full" title="Fullscreen (f)">@@expand@@</button>
 </div>
</footer>
<div class="toast" id="toast"></div>
"""

JS = r"""
"use strict";
const S = {state:null, view:"home", filter:"all", sort:"recent", menu:null,
           prog:{pos:0, dur:0, playing:false, at:0},
           // the initial view is the first history entry. Without it the first
           // navigation started hist=[], so "home" was never recorded and the back
           // button was born disabled (hix=0 after one push) - "jump to search,
           // back does nothing". Start with home in the stack so back always has a
           // target from the very first move.
           hist:["home"], hix:0, q:"", vol:70,
           dragging:false, sureWipe:0};
const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined && txt !== null) n.textContent = txt;
  return n;
};
function svg(name){ const s = document.createElement("span"); s.innerHTML = ICONS[name] || ""; return s.firstChild; }
function toast(msg){
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), 2800);
}
function fmt(sec){
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
}
async function post(path, fields){
  const r = await fetch(path, {method:"POST", body:new URLSearchParams(fields || {}),
    headers:{"X-Requested-With":"spotube-dj"}});
  const text = await r.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {error:text.slice(0,200)}; }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
async function act(action, extra){
  const f = Object.assign({action}, extra || {});
  try {
    const j = await post("/api/action", f);
    if (j.state) draw(j.state);
    if (j.note) toast(j.note);
  } catch (e) { toast(e.message || "that did not work"); }
}

/* ---------- one rule for every list the tick could repaint ----------
   /api/state arrives every 700 ms. A list rebuilt on all of them loses your hover,
   your scroll position and, in a sidebar, the pointer's interest - so each region
   carries the signature of what it holds, and only a *change* rebuilds it. */
const sigOf = (v) => { try { return JSON.stringify(v); } catch (e) { return String(v); } };
function redraw(box, sig, make){
  if (!box || box.dataset.sig === sig) return false;
  const top = box.scrollTop;
  box.dataset.sig = sig;
  box.textContent = "";
  make(box);
  box.scrollTop = top;
  return true;
}
function setText(node, txt){ if (node && node.textContent !== txt) node.textContent = txt; }

/* ---------- art: a real cover when there is one, a tinted tile when not ---------- */
function paint(node, seed, px){
  const s = String(seed || "?").trim();
  let h = 0;
  for (const ch of s) h = (h * 131 + ch.charCodeAt(0)) >>> 0;
  const pal = getComputedStyle(document.documentElement).getPropertyValue("--tiles").split(",");
  node.textContent = "";
  node.style.background = "linear-gradient(140deg," + pal[h % pal.length] + "," +
    pal[((h >> 3) >>> 0) % pal.length] + ")";
  node.style.fontSize = Math.max(11, Math.round((px || 40) * 0.4)) + "px";
  node.appendChild(el("span", null, (s[0] || "?").toUpperCase()));
  return pal[h % pal.length];
}
function seedOf(t){
  t = t || {};
  return t.id || t.title || t.name || t.q || "dj";
}
/* one number per label: the same hash the tiles are painted from, so a shortcut's
   colour is stable across reloads and matches the art it points at */
function hueOf(seed){
  let h = 0;
  for (const ch of String(seed || "?")) h = (h * 131 + ch.charCodeAt(0)) >>> 0;
  return h;
}
/* the playing colour should be the cover's majority colour, not a hash into the
   palette. Every <img> that loads (a tile, a card, the hero) has its dominant
   colours pulled out here and cached under the video id, so `backdrop` can tint the
   page from the artwork itself - once, not on every tick. Reading pixels requires a
   same-origin (or `data:`) image; a cross-origin one taints the canvas and the read
   throws, which we swallow and leave the palette fallback in place (the artwork
   lane dresses rows to a same-origin /art/ file, so by the time a row re-renders it
   is readable). */
const coverColors = {};         // vid -> {main, alt}
function normColor(r, g, b){
  // clamp into a usable mid accent: a near-black cover must not give an invisible
  // tint and a near-white one must not wash the buttons out
  const L = 0.2126*r + 0.7152*g + 0.0722*b;
  if (L < 42)  { const f = 42/Math.max(1, L); r = Math.min(255, r*f); g = Math.min(255, g*f); b = Math.min(255, b*f); }
  else if (L > 208) { const f = 208/L; r *= f; g *= f; b *= f; }
  // hex, so it composes with the existing `tint + "00"` alpha shorthand in the wash
  const hx = (v) => ("0" + Math.round(v).toString(16)).slice(-2);
  return "#" + hx(r) + hx(g) + hx(b);
}
function dominantColor(img, vid){
  if (!vid || !img || coverColors[vid]) return;
  try {
    const s = 30;
    const cv = document.createElement("canvas");
    cv.width = cv.height = s;
    const cx = cv.getContext("2d", {willReadFrequently:true});
    if (!cx) return;
    const iw = img.naturalWidth, ih = img.naturalHeight;
    if (!iw || !ih) return;
    const scale = Math.max(s/iw, s/ih);
    const w = iw*scale, h = ih*scale;
    cx.drawImage(img, (s - w)/2, (s - h)/2, w, h);
    const d = cx.getImageData(0, 0, s, s).data;
    const buckets = {};
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] < 125) continue;                    // skip transparent
      const kr = d[i] >> 5, kg = d[i + 1] >> 5, kb = d[i + 2] >> 5;
      const key = (kr << 10) | (kg << 5) | kb;
      const b = buckets[key] || (buckets[key] = {r:0, g:0, b:0, n:0});
      b.r += d[i]; b.g += d[i + 1]; b.b += d[i + 2]; b.n++;
    }
    const top = Object.values(buckets).sort((a, b) => b.n - a.n);
    if (!top.length) return;
    const a = top[0];
    const main = normColor(a.r/a.n, a.g/a.n, a.b/a.n);
    // a real second colour from the cover if there is one, else a hue flip
    let alt;
    if (top[1] && top[1].n > 0) alt = normColor(top[1].r/top[1].n, top[1].g/top[1].n, top[1].b/top[1].n);
    else                       alt = normColor(a.b/a.n, a.g/a.n, a.r/a.n);
    coverColors[vid] = {main, alt};
  } catch (e) { /* tainted canvas: leave the palette fallback in place */ }
}
function art(node, track, px, field){
  const tint = paint(node, seedOf(track), px);
  track = track || {};
  // `field` lets one row carry two pictures: `art` for the 40px lists and
  // `art_card` for the 190px grid tile, each at the size it is drawn at. But a row
  // can carry its picture in only one of those slots, or only as the raw InnerTube
  // `thumbnail`, so fall through them all - a queue row that only has `art_card`,
  // or an album/discography row that arrived with `thumbnail`, must still be dressed
  // rather than drawn as a tinted initial. Any real picture wins over the initial,
  // and if every field is blank but the row has an id, the video's always-served
  // `hqdefault` frame is the last resort - so a row is never a bare letter when a
  // real image could exist.
  const url = (field && track[field]) || track.art || track.art_card || track.thumbnail ||
              (track.id ? "https://i.ytimg.com/vi/" + track.id + "/hqdefault.jpg" : "");
  if (url) cover(node, url, track.id || "");
  return tint;
}
/* one <img> per slot, with a fallback a bare `img.src=` never had: a blank tile is
   most often a `maxresdefault` the upload never rendered. Retry the same video's
   `hqdefault` (which is always served) before falling back to the coloured initial,
   so a row is never silently empty when a smaller, still-real frame exists. */
function cover(node, url, vid, tried){
  const m = /\/vi\/([^/]+)/.exec(url) || (vid ? [undefined, String(vid)] : null);
  const hq = m ? "https://i.ytimg.com/vi/" + m[1] + "/hqdefault.jpg" : "";
  const finalUrl = (m && /maxresdefault/.test(url) && hq !== url && !tried) ? hq : url;
  // a row tile is 40-70 px, so eager beats lazy here: `loading="lazy"` on a small
  // absolute image inside a custom scroll container can sit unloaded (a browser
  // has no scroll event to react to) and leave the tinted tile a cover should have
  // covered. Eager pictures in an always-visible list cost nothing meaningful.
  const img = el("img"); img.src = finalUrl; img.alt = ""; img.loading = "eager";
  img.onload = () => dominantColor(img, vid);
  img.onerror = () => {
    img.remove();
    if (!tried && hq && hq !== finalUrl) cover(node, hq, vid, true);
  };
  node.appendChild(img);
}
/* the blurred backdrop: the cover, blown up and out of focus, behind the content.
   Two layers: the base holds the outgoing cover and the float holds the incoming
   one, so a track change is a 500 ms crossfade instead of a cut. `--tint` is also
   what the equaliser bars, the play buttons and the progress bar read, so the
   "playing" colour follows the artwork. */
function backdrop(track, playing){
  const url = track && track.art ? track.art : "";
  const t = playing ? (track.id || track.title || "dj") : "dj";
  let h = 0;
  for (const ch of String(t)) h = (h * 131 + ch.charCodeAt(0)) >>> 0;
  const pal = getComputedStyle(document.documentElement).getPropertyValue("--tiles").split(",");
  const other = pal[(h >> 5) % pal.length];
  // the playing colour is the cover's majority colour once that picture has been
  // read (see dominantColor). Until it lands we fall back to the seed's palette
  // pair so the wash is never a static grey and never jumps from nothing.
  const c = (playing && track && track.id && coverColors[track.id]) || null;
  const tint = c ? c.main : pal[h % pal.length];
  const tint2 = c ? c.alt : pal[((h >> 3) >>> 0) % pal.length];
  document.documentElement.style.setProperty("--tint", tint);
  document.documentElement.style.setProperty("--tint2", tint2);
  /* the wash underneath a still-loading (or unavailable) cover is the two palette
     colours the tile would have used, so the page is never "nothing" */
  const art = url
    ? "linear-gradient(160deg," + tint + "00," + other + "00),url(" + JSON.stringify(url) + ")"
    : "linear-gradient(160deg," + tint + "," + other + ")";
  ["bg-main", "bg-side"].forEach((id) => {
    const n = $(id); if (!n) return;
    const f = $(id + "2");
    // one guard for the whole function: `drawDetail` runs on every 700 ms tick, so
    // without this a still-playing track restarts the crossfade each tick and looks
    // like it is stuttering. Only act when the picture (or its tint) moves.
    if (n._art === art) return;
    n._art = art;
    if (!f) {
      // a panel with no float layer just takes the picture directly
      n.classList.toggle("flat", !url);
      n.style.backgroundImage = art;
      return;
    }
    if (!n._set) {
      // first paint: nothing to crossfade from, set the base and hide the float
      n._set = true;
      n.classList.toggle("flat", !url);
      n.style.backgroundImage = art;
      f.style.opacity = "0";
      return;
    }
    if (!url) {
      // lost the cover: drop back to the flat two-colour wash, no float needed
      clearTimeout(f._t);
      n.classList.add("flat");
      n.style.backgroundImage = art;
      f.style.opacity = "0";
      return;
    }
    // real crossfade: the base keeps the outgoing cover, the float fades in with the
    // incoming one on top, then the base adopts it and the float clears. A track
    // change reads as one cover melting into the next instead of a hard cut between
    // two different album colours.
    f.classList.remove("flat");
    f.style.backgroundImage = art;
    void f.offsetWidth;
    f.style.opacity = ".66";
    clearTimeout(f._t);
    f._t = setTimeout(() => {
      n.classList.remove("flat");
      n.style.backgroundImage = f.style.backgroundImage;
      f.style.opacity = "0";
    }, 470);
  });
}

/* ---------- menus ---------- */
function closeMenu(){ if (S.menu) { S.menu.remove(); S.menu = null; } }
function menu(anchor, items){
  closeMenu();
  const m = el("div", "menu");
  items.forEach((it) => {
    if (it === "-") { m.appendChild(el("hr")); return; }
    const b = el("button", it.bad ? "bad" : null);
    b.appendChild(svg(it.icon || "dot"));
    b.appendChild(el("span", null, it.label));
    b.addEventListener("click", () => { closeMenu(); it.fn(); });
    m.appendChild(b);
  });
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const w = m.offsetWidth, hh = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
  m.style.top = (r.bottom + hh + 10 > window.innerHeight ? r.top - hh - 6 : r.bottom + 6) + "px";
  S.menu = m;
}
document.addEventListener("click", (e) => { if (S.menu && !S.menu.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
window.addEventListener("resize", closeMenu);
$("scroller").addEventListener("scroll", () => {
  const m = $("main");
  m.classList.toggle("up", $("scroller").scrollTop > 8);
});

/* ---------- rows and cards ---------- */
function trackMenu(t, opts){
  opts = opts || {};
  const items = [
    {label:"Play now", icon:"play", fn:() => act("play_row", {id: t.id || ""})},
    {label:"Queue next", icon:"queue", fn:() => act("queue_next", {id: t.id || ""})},
    {label:"Love this", icon:"heart_o", fn:() => act("love_row", {id: t.id || ""})},
  ];
  if (opts.queued) {
    items.push({label:"Remove from queue", icon:"close", fn:() => act("remove_queue", {id: t.id || ""})});
  }
  items.push({label:"Not for me (dislike)", icon:"thumb_down", fn:() => act("dislike", {id: t.id || ""})});
  items.push({label:"Start a station", icon:"mix", fn:() => act("radio", {id: t.id || ""})});
  items.push("-");
  items.push({label:"Stop", icon:"stop", bad:true, fn:() => act("stop")});
  return items;
}
function rowNode(t, i, opts){
  opts = opts || {};
  const r = el("div", "row");
  if (opts.current) r.setAttribute("aria-current", "true");
  const n = el("div", "n"); n.appendChild(el("span", null, String(i + 1)));
  const eq = el("div", "eq"); for (let k = 0; k < 3; k++) eq.appendChild(el("i"));
  n.appendChild(eq); r.appendChild(n);
  const a = el("div", "tile"); art(a, t, 40); r.appendChild(a);
  const ti = el("div", "ti", t.title || "?"); r.appendChild(ti);
  const ar = el("div", "ar");
  const who = t.artist || t.channel || "";
  // the artist is a live link to the in-app artist page, wherever the row shows
  // (search results, a page, a queue row) - not a dead line of text
  if (who) {
    const name = el("button", "plink", who);
    name.title = "Songs by this artist";
    name.addEventListener("click", (e) => {
      e.stopPropagation();
      setView("page");
      act("open_artist", {artist: who});
    });
    ar.appendChild(name);
  } else {
    ar.appendChild(el("span", null, "unknown artist"));
  }
  if (t.note) ar.appendChild(el("span", "badge", t.note));
  r.appendChild(ar);
  r.appendChild(el("div", "du", t.dur || (opts.ts || "")));
  // the trailing cluster: for a queued row a Remove and a "not for me" sit next to
  // the kebab, so a row in the Up Next panel can be acted on without a click on the
  // ⋮ first - both feed the taste model and both stopPropagation so they do not
  // also fire the row own play-on-click.
  const acts = el("div", "rowacts");
  if (opts.queued) {
    const rem = el("button", "iconbtn");
    rem.appendChild(svg("close")); rem.title = "Remove from queue";
    rem.addEventListener("click", (e) => { e.stopPropagation(); act("remove_queue", {id: t.id || ""}); });
    acts.appendChild(rem);
    const dis = el("button", "iconbtn");
    dis.appendChild(svg("thumb_down")); dis.title = "Not for me - never suggest this again";
    dis.addEventListener("click", (e) => { e.stopPropagation(); act("dislike", {id: t.id || ""}); });
    acts.appendChild(dis);
  }
  const isAlbum = t.kind === "album";
  // a discography entry is not a playable song, so the track menu (play/queue/love)
  // does not apply to it; the row itself opens the album
  if (!isAlbum) {
    const more = el("button", "iconbtn more");
    more.appendChild(svg("dots")); more.title = "More";
    more.addEventListener("click", (e) => { e.stopPropagation(); menu(more, trackMenu(t, opts)); });
    acts.appendChild(more);
  }
  r.appendChild(acts);
  // a discography row is an *album*: the row opens the album tracklist on a click,
  // and it has no play button
  if (isAlbum) {
    const openAlbum = (e) => {
      e.stopPropagation();
      act("open_album", {album: t.album || t.title || "", artist: t.artist || ""});
    };
    r.addEventListener("click", openAlbum);
    r.addEventListener("dblclick", openAlbum);
    r.classList.add("albumrow");
  } else {
    const fab = el("button", "fab"); fab.appendChild(svg("play"));
    fab.title = "Play now";
    fab.addEventListener("click", (e) => { e.stopPropagation(); playOnce(t.id || ""); });
    r.appendChild(fab);
    // "the queue UI bug when click": a click on an Up Next row used to do nothing
    // (only a double-click played it), so a row looked clickable but was not. A single
    // click on a queued row now plays it - the action buttons above stop the event.
    // `playOnce` also squashes the second click of a double-tap, so it never stacks two
    // play_row calls (that was the "plays twice" bug: one click, then the dblclick that
    // follows it, both firing).
    r.addEventListener("click", () => { if (opts.queued) playOnce(t.id || ""); });
    r.addEventListener("dblclick", () => { if (!opts.queued) playOnce(t.id || ""); });
    if (opts.current) r.classList.add("playing");
  }
  return r;
}

/* one play per row per gesture: a double-click fires click, click, dblclick, and
   each of those used to restart the song. Lock out the row's id for a beat so a
   quick pair of presses is one play, not two. */
function playOnce(id){
  const now = Date.now();
  if (playOnce._id === id && now - (playOnce._t || 0) < 320) return;
  playOnce._id = id; playOnce._t = now;
  act("play_row", {id: id || ""});
}
function cardNode(t, i){
  const c = el("div", "card");
  const cov = el("div", "cover"); const tile = el("div", "tile");
  art(tile, t, 44, "art_card"); cov.appendChild(tile); c.appendChild(cov);
  if (!(t.art_card || t.art)) cov.classList.add("pending");
  c.appendChild(el("div", "t", t.title || "?"));
  c.appendChild(el("div", "a", t.artist || t.channel || "unknown artist"));
  const fab = el("button", "fab"); fab.appendChild(svg("play"));
  fab.title = "Play now";
  fab.addEventListener("click", () => playOnce(t.id || ""));
  c.appendChild(fab);
  c.addEventListener("dblclick", () => playOnce(t.id || ""));
  if (t.id && S.state && S.state.now && t.id === S.state.now.id) c.classList.add("on");
  return c;
}

/* ---------- sidebar ---------- */
const FILTERS = [["all", "All"], ["music", "Music"], ["artists", "Artists"],
                 ["moods", "Moods"], ["loved", "Loved"]];
function drawFilters(){
  const f = $("filters");
  redraw(f, String(S.filter), (box) => FILTERS.forEach(([key, label]) => {
    const b = el("button", "chip" + (S.filter === key ? " on" : ""), label);
    b.addEventListener("click", () => { S.filter = key; drawFilters(); drawLibrary(S.state); });
    box.appendChild(b);
  }));
}
function libRow(item, kind){
  const r = el("div", "lib");
  const tile = el("div", "tile" + (kind === "artist" ? " sq" : ""));
  art(tile, item.art || item, kind === "artist" ? 20 : 18);
  r.appendChild(tile);
  const mid = el("div"); mid.style.minWidth = "0";
  mid.appendChild(el("div", "name", item.title || item.name || "?"));
  const meta = el("div", "meta");
  if (kind === "artist") {
    meta.appendChild(el("span", null, "artist · " + (item.loved || 0) + " loved · leans " + item.w));
    if ((item.loved || 0) >= 2) { const v = svg("verified"); v.title = "you keep coming back to this artist"; meta.appendChild(v); }
  } else if (kind === "mood") {
    meta.appendChild(el("span", null, "mix · " + (item.q || "")));
  } else {
    meta.appendChild(el("span", null, (item.artist || "loved") + (item.note ? " · " + item.note : "")));
  }
  mid.appendChild(meta); r.appendChild(mid);
  const go = el("button", "iconbtn go");
  go.appendChild(svg("play")); go.title = "Play this";
  go.addEventListener("click", (e) => { e.stopPropagation();
    // `request` is the honest verb here: it queues that exact song first and then
    // mixes around it, which is what clicking a loved row should do
    act("request", {q: item.q || item.title || item.name || ""});
  });
  r.appendChild(go);
  const more = el("button", "iconbtn more");
  more.appendChild(svg("dots")); more.title = "More";
  more.addEventListener("click", (e) => { e.stopPropagation();
    menu(more, [
      {label: kind === "artist" ? "Songs like this artist" : "Mix from this", icon:"sparkle",
       fn: () => act("request", {q: item.q || item.name || item.title || ""})},
      {label: "Search for it", icon:"search",
       fn: () => { $("q").value = item.q || item.name || item.title || ""; runSearch(); }},
      "-",
      {label: "Forget this artist", icon:"close", bad:true,
       fn: () => act("unfollow", {name: item.name || ""})},
    ]);
  });
  r.appendChild(more);
  r.addEventListener("click", () => {
    $("q").value = item.q || item.title || "";
    runSearch();
  });
  return r;
}
function drawLibrary(s){
  s = s || S.state || {};
  const lib = s.library || {loved:[], artists:[], moods:[], recents:[]};
  const box = $("librows");
  const want = (kind) => S.filter === "all" || S.filter === kind;
  const rows = [];
  if (want("music")) {
    (lib.recents || []).forEach((t) => rows.push({row: t, kind:"track"}));
  }
  if (want("artists")) {
    (lib.artists || []).forEach((a) => rows.push({row: a, kind:"artist"}));
  }
  if (want("moods")) {
    (lib.moods || []).forEach((m) => rows.push({row: m, kind:"mood"}));
  }
  if (want("loved")) {
    (lib.loved || []).forEach((l) => rows.push({row: l, kind:"loved"}));
  }
  function order(kind){ return {track:0, loved:1, artist:2, mood:3}[kind]; }
  const byName = (a, b) => String(a.row.title || a.row.name || "").localeCompare(
    String(b.row.title || b.row.name || ""));
  const none = S.filter === "all" && !(lib.recents || []).length && !(lib.artists || []).length;
  const c = (lib.counts) || {};
  redraw(box, sigOf([S.filter, S.sort, none, rows]), (b) => {
    if (none) {
      const e = el("div", "empty");
      e.appendChild(el("b", null, "Nothing here yet"));
      e.appendChild(el("div", null, "Love a few songs and this fills up: artists you keep " +
        "coming back to, the moods you asked for, and what you played recently."));
      b.appendChild(e);
      return;
    }
    rows.sort(S.sort === "alpha" ? byName : (x, y) => order(x.kind) - order(y.kind));
    rows.slice(0, 60).forEach((it) => b.appendChild(
      it.kind === "track" ? trackRow2(it.row) : libRow(it.row, it.kind)));
  });
  setText($("lib-count"), none ? "" :
    (c.loved || 0) + " loved · " + (c.artists || 0) + " artists");
}
/* recents in the sidebar want the compact tile row, not the full queue row */
function trackRow2(t){
  const r = el("div", "lib");
  const tile = el("div", "tile"); art(tile, t, 18); r.appendChild(tile);
  const mid = el("div"); mid.style.minWidth = "0";
  mid.appendChild(el("div", "name", t.title || "?"));
  const meta = el("div", "meta");
  meta.appendChild(el("span", null, (t.artist || "unknown") + (t.ts ? " · " + t.ts : "")));
  mid.appendChild(meta); r.appendChild(mid);
  const go = el("button", "iconbtn go"); go.appendChild(svg("play")); go.title = "Play this";
  go.addEventListener("click", (e) => { e.stopPropagation(); act("request", {q: (t.artist || "") + " " + (t.title || "")}); });
  r.appendChild(go);
  r.addEventListener("click", () => { act("request", {q: (t.artist || "") + " " + (t.title || "")}); });
  return r;
}

/* ---------- views ---------- */
function navSync(){
  $("nav-back").disabled = S.hix <= 0;
  $("nav-fwd").disabled = S.hix >= S.hist.length - 1;
}
function setView(name, push){
  if (name === S.view) return;
  // "page" is a dynamic view (album / artist) not in the static NAV list, so it is
  // toggled alongside the fixed ones.
  const views = ((VM && VM.views) || ["home"]).concat(["page"]);
  views.forEach((v) => {
    const elv = $("view-" + v);
    if (elv) elv.hidden = v !== name;
  });
  S.view = name;
  if (push !== false) { S.hist = S.hist.slice(0, S.hix + 1); S.hist.push(name); S.hix = S.hist.length - 1; }
  navSync();
  $("scroller").scrollTop = 0;
  drawLibrary(S.state);
}
function greeting(){
  const h = new Date().getHours();
  const table = (VM && VM.greeting) || [];
  for (const row of table) if (row[0] <= h && h < row[1]) return row[2];
  return "Good listening";
}
function drawQuick(s){
  const box = $("quick");
  const items = [];
  const loves = ((s.library || {}).loved || []);
  ((VM && VM.moods) || []).slice(0, 4).forEach((m) => {
    items.push({label:m.label, note:"mood", q:m.q, icon:"sparkle",
                act:() => act("request", {q:m.q})});
  });
  if ((s.taste && s.taste.likes) || loves.length) {
    items.push({label:"Loved songs", note:"mix", q:"", icon:"heart", act:() => act("mix")});
  }
  if (s.station) items.push({label:"Station: " + s.station, note:"playing set", q:"",
                             icon:"mix", act:() => setView("home")});
  const used = {};
  (((s.library || {}).moods) || []).forEach((m) => {
    if (!m || !m.q || used[String(m.q).toLowerCase()]) return;
    used[String(m.q).toLowerCase()] = 1;
    items.push({label:m.q, note:m.note || "last played", q:m.q, icon:"search",
                act:() => act("request", {q:m.q})});
  });
  MOODS.forEach((m) => {
    if (items.length >= 6 || used[m.toLowerCase()]) return;
    used[m.toLowerCase()] = 1;
    items.push({label:m, note:"mood", q:m, icon:"sparkle",
                act:() => act("request", {q:m})});
  });
  redraw(box, sigOf(items.map((it) => [it.label, it.note, it.q])), (bx) => items.forEach((it) => {
    const b = el("button", "qp");
    b.appendChild(el("span", "qt", it.label));
    const fab = el("span", "fab"); fab.appendChild(svg("play")); b.appendChild(fab);
    // one tinted mark per shortcut, from the same seed the tiles use, so the row
    // reads as designed rather than as six empty grey boxes
    const chip = el("span", "qi");
    chip.appendChild(svg(it.icon || "play"));
    chip.style.background = "hsl(" + (hueOf(it.q || it.label) % 360) + " 34% 30%)";
    b.appendChild(chip);
    b.addEventListener("click", it.act);
    b.title = it.q ? ("play: " + it.q) : "make a mix from your likes";
    bx.appendChild(b);
  }));
}
const MOODS = ((VM && VM.moods) || []).map((m) => m.q);
function drawChips(s){
  const box = $("cards");
  const rows = (s.up_next || []);
  const head = $("mixs");
  if (!rows.length) {
    setText(head, s.taste && s.taste.likes ? "press play - the mix builds itself"
                                           : "love a few songs first");
    box.style.display = "block";
    redraw(box, "empty:" + ((s.taste || {}).likes ? 1 : 0), (b) => {
      const e = el("div", "empty");
      e.appendChild(el("b", null, "The queue is empty"));
      e.appendChild(el("div", null, "Tell the DJ what you feel, or press the green button. " +
        "It searches YouTube Music for you, keeps what you love, and drops what you skip."));
      b.appendChild(e);
    });
    return;
  }
  box.style.display = "";
  // a Daylist-style name for the set, when the engine gave one ("lofi tuesday
  // night"): it reads as a tuned radio station rather than a raw search
  const vibe = (s.vibe || "").trim();
  setText(head, (vibe ? vibe + " · " : "") + rows.length + " queued" +
    (s.request ? " from " + JSON.stringify(s.request) : ""));
  // the row being heard is marked, so the current id is part of what changed
  redraw(box, sigOf([rows.slice(0, 12), (s.now || {}).id || ""]), (b) =>
    rows.slice(0, 12).forEach((t, i) => b.appendChild(cardNode(t, i))));
}
function drawUpNext(s){
  const box = $("upnext");
  const rows = (s.up_next || []).slice();
  setText($("upc"), (s.queued || rows.length) + " tracks");
  const playing = (s.now || {}).id || "";
  if (S.filter === "loved") {
    rows.splice(0, rows.length, ...rows.filter((t) => t.note === "from your likes"));
  } else if (S.filter === "moods") {
    rows.splice(0, rows.length, ...rows.filter((t) => t.note !== "from your likes"));
  }
  if (!rows.length) {
    setText($("empty-acts"), "");
    $("uph").hidden = true;
    redraw(box, "none", () => {});
    return;
  }
  $("uph").hidden = false;
  const cq = $("clearq"); if (cq) cq.hidden = !(s.queued || rows.length);
  redraw(box, sigOf([rows, playing, S.filter]), (b) =>
    rows.forEach((t, i) => b.appendChild(rowNode(t, i,
      {current: playing && t.id === playing, queued: true}))));
}
function drawDetail(s){
  const np = s.now || {};
  setText($("np-title"), np.title || "Nothing playing");
  const arts = (s.library || {}).artists || [];
  const mine = arts.filter((a) => a.name && np.artist &&
    a.name.toLowerCase() === String(np.artist).toLowerCase());
  const tick = !!(np.title && mine.length && (mine[0].loved || 0) >= 2);
  const by = $("np-by");
  redraw(by, sigOf([np.title || "", np.artist || "", np.channel || "", tick]), (b) => {
    if (!np.title) return;
    // the artist is a live link to the in-app artist page, not a dead line of text
    const name = el("button", "plink", np.artist || np.channel || "unknown artist");
    name.title = "Songs by this artist";
    name.addEventListener("click", (e) => {
      e.stopPropagation();
      setView("page");
      act("open_artist", {artist: np.artist || np.channel || ""});
    });
    b.appendChild(name);
    if (tick) b.appendChild(svg("verified"));
  });
  by.hidden = !np.title;
  art($("np-art"), np, 70);
  backdrop(np, !!np.title);
  setText($("np-why"), s.idle_note || "type a mood above, or pick something on the left");
  // credits are the *record's* facts and stay stable while a track plays. The
  // transport bar already carries the progress, the length and the cache state, so
  // repeating those here just eats the panel: what sits beside the cover should say
  // what the record is, not how far into it a person has got. No "source: mpv", no
  // length, no audio/cache line, no ticking position row.
  const dl = $("credits");
  const pairs = [["played by", "YouTube Music"],
                 ["channel", np.channel || ""],
                 ["album", np.album || ""],
                 ["released", np.release_year || ""],
                 ["found by", np.found || ""]];
  redraw(dl, sigOf([pairs, !!np.title]), (b) => {
    pairs.forEach(([k, v]) => {
      if (!v && v !== 0) return;
      b.appendChild(el("dt", null, k)); b.appendChild(el("dd", null, String(v)));
    });
  });
  const loved = (s.library || {}).loved || [];
  const same = np.artist ? loved.filter((l) => l.artist &&
    String(l.artist).toLowerCase().indexOf(String(np.artist).toLowerCase().split(" ")[0]) >= 0) : [];
  const pick = (same.length ? same : loved).slice(0, 5);
  setText($("simh"), same.length ? "More by " + np.artist : "From your likes");
  const sim = $("simil");
  redraw(sim, sigOf([pick, np.artist || ""]), (b) => {
    if (!pick.length) {
      b.appendChild(el("div", "empty", "Nothing loved yet - press the heart on anything " +
        "you like and the next mix tilts towards it."));
    }
    pick.forEach((t) => {
      const m = el("div", "m");
      const tile = el("div", "tile"); tile.style.width = tile.style.height = "26px";
      paint(tile, seedOf(t), 12); m.appendChild(tile);
      m.appendChild(el("span", null, t.title || "?"));
      const go = el("button", "iconbtn"); go.appendChild(svg("play")); go.title = "Play this";
      go.addEventListener("click", () => act("request", {q: t.q || t.title || ""}));
      m.appendChild(go);
      b.appendChild(m);
    });
  });
  ["np-station", "np-open", "np-stop"].forEach((id) => { $(id).disabled = !np.title; });
  const alb = $("np-album");
  if (alb) {
    alb.hidden = !(np.title && np.album_url);
    alb.disabled = !np.title;
  }
  const love = $("b-love");
  love.innerHTML = ""; love.appendChild(svg(np.liked ? "heart" : "heart_o"));
  love.classList.toggle("on", !!np.liked);
  $("b-play").innerHTML = "";
  $("b-play").appendChild(svg(s.paused || !np.title ? "play" : "pause"));
}
function drawPlayer(s){
  const np = s.now || {};
  $("bar-title").textContent = np.title || "Not playing";
  $("bar-artist").textContent = np.title ? (np.artist || np.channel || "") : "";
  art($("bar-art"), np.art_tile ? {art: np.art_tile} : np, 16);
  // the pill is a fixed-width box: the "audio cache: " it repeats every tick was
  // what made it read "audio cache: ..." instead of the numbers that matter
  const cach = (s.cache_note || "").replace(/^audio cache:\s*/i, "");
  setText($("cache"), cach || "cache idle");
  $("cache").title = s.cache_note || "";
  $("b-auto").classList.toggle("on", !!s.auto);
  $("b-shuffle").classList.toggle("on", !!s.shuffle);
  const rep = $("b-repeat");
  rep.innerHTML = "";
  rep.appendChild(svg(s.repeat === "one" ? "repeat_one" : "repeat"));
  rep.classList.toggle("on", s.repeat !== "off");
  rep.title = "Repeat: " + (s.repeat || "off") + " (r)";
  document.body.classList.toggle("busy", !!s.busy);
  const pill = $("jobpill");
  pill.hidden = !s.busy;
  if (s.busy) pill.textContent = s.job_note || "building the mix";
  const eng = $("engine");
  // the pill is three words; the sentence belongs in its tooltip. The full note was
  // being clipped to "Built-in planner (works with no ..." in a 24px-tall box, which
  // looks like a bug in the app rather than in the label
  const engNote = s.engine_note || "brain: offline parser";
  setText(eng, s.engine_pill || "brain");
  eng.title = engNote;
  eng.classList.toggle("good", /gemini|ollama|llm/i.test(eng.textContent));
  S.prog = {pos: Number(s.position) || 0, dur: Number(s.duration) || 0,
            playing: !!(np.title && !s.paused), at: Date.now()};
  $("vol").value = String(s.volume === undefined ? S.vol : s.volume);
  S.vol = Number($("vol").value);
  $("vol").style.setProperty("--vol", (S.vol * 100) / 100 + "%");
  const mute = $("b-mute");
  mute.innerHTML = "";
  mute.appendChild(svg(S.vol > 0 ? "vol" : "vol_mute"));
  drawProgress();
}
function drawProgress(){
  const p = S.prog;
  const live = p.playing ? p.pos + (Date.now() - p.at) / 1000 : p.pos;
  const frac = p.dur > 0 ? Math.max(0, Math.min(1, live / p.dur)) : 0;
  $("fill").style.width = (frac * 100).toFixed(2) + "%";
  $("knob").style.left = (frac * 100).toFixed(2) + "%";
  $("t0").textContent = fmt(p.dur ? live : 0);
  $("t1").textContent = fmt(p.dur || 0);
}
function drawResults(s){
  const box = $("results");
  const sc = s.search || {};
  const rows = sc.rows || [];
  setText($("search-s"), sc.q ? (rows.length + " for " + JSON.stringify(sc.q)) : "");
  const state = sc.pending ? "pending" : (rows.length ? "rows" : "empty");
  redraw(box, sigOf([state, sc.note || "", rows]), (b) => {
    if (sc.pending) { b.appendChild(el("div", "empty", "searching YouTube Music...")); return; }
    if (!rows.length) {
      b.appendChild(el("div", "empty",
        (sc.note || "type in the box above - artist, song, or a mood")));
      return;
    }
    rows.forEach((t, i) => b.appendChild(rowNode(t, i, {})));
  });
}
function drawPage(s){
  const p = s.page || null;
  if (!p) return;
  const title = $("page-title"), sub = $("page-sub"), box = $("page-rows");
  if (!title || !box) return;
  setText(title, p.title || (p.kind === "artist" ? "Artist" : "Album"));
  setText(sub, p.sub || (p.pending ? "" : p.note || ""));
  if (p.pending) {
    redraw(box, "pending", (b) => b.appendChild(
      el("div", "empty", "looking that up...")));
    return;
  }
  const rows = p.rows || [];
  redraw(box, sigOf([rows, p.kind, p.note]), (b) => {
    if (!rows.length) {
      b.appendChild(el("div", "empty", p.note || "no tracks back for that yet"));
      return;
    }
    rows.forEach((t, i) => b.appendChild(rowNode(t, i, {})));
  });
}
function drawTaste(s){
  const t = s.taste || {};
  const box = $("taste");
  const items = ((t.artists || []).map((a) => ({n:a.name, w:a.w, tag:"artist"})))
    .concat((t.tags || []).map((a) => ({n:a.name, w:a.w, tag:"tag"})));
  redraw(box, sigOf(items), (b) => {
    if (!items.length) {
      b.appendChild(el("div", "empty", "No profile yet. Love or skip a few songs and the DJ " +
        "starts leaning."));
    }
    items.forEach((it) => {
      const chip = el("button", "tk" + (it.w < 0 ? " neg" : ""));
      chip.appendChild(el("span", null, it.tag));
      chip.appendChild(el("b", null, it.n));
      const bar = el("i");
      bar.style.width = Math.min(60, 10 + Math.abs(it.w) * 6) + "px";
      chip.appendChild(bar);
      chip.title = it.tag + " weight " + it.w;
      chip.addEventListener("click", () => {
        $("q").value = it.tag === "artist" ? "songs like " + it.n : it.n;
        runSearch();
      });
      b.appendChild(chip);
    });
  });
  setText($("tastenote"), (t.likes || 0) + " loved, " + (t.skips || 0) +
    " skipped - every love is +2 and every early skip is -1.6, and that is the whole model." +
    (t.last_request ? " Last mix: " + t.last_request : ""));
  $("undobtn").hidden = !t.has_backup;
  $("mixbtn").disabled = !(t.likes || (t.artists || []).length);
}
function drawSettings(s){
  const st = s.settings || {};
  // never overwrite a box that is being typed in: the tick that follows a keystroke
  // used to put the saved value back, so a base URL could not be typed at all
  const fillIn = (node, v) => { if (node && document.activeElement !== node) node.value = v; };
  fillIn($("in-base"), st.base || "");
  fillIn($("in-model"), st.model || "");
  $("unkeybtn").hidden = !st.has_key;
  $("engine2").textContent = st.has_key
    ? "Planning goes through " + (st.model || "the default model") + " at " +
      (st.base || "generativelanguage.googleapis.com") + ". The key is only ever sent back as "
      + (st.key_hint || "a mask") + "."
    : "No key saved, so a small offline parser plans the searches. Paste a Gemini key (or a " +
      "local model URL) and the DJ writes its own query plan instead - it still never touches " +
      "a Spotify API.";
  $("setnote").textContent = st.note || "";
}
function drawRecents(s){
  const box = $("recents");
  const rows = ((s.library || {}).recents || []);
  redraw(box, sigOf(rows), (b) => {
    if (!rows.length) { b.appendChild(el("div", "empty", "Nothing heard yet.")); return; }
    rows.forEach((t, i) => b.appendChild(rowNode(t, i, {ts: t.ts})));
  });
}
function drawLog(s){
  const txt = (s.log || []).slice(-40).join("\n");
  setText($("log"), txt);
  setText($("log2"), txt);
}
function drawEmptyActs(s){
  const acts = $("empty-acts");
  if ((s.up_next || []).length) { acts.hidden = true; }
  else { acts.hidden = false;
  const picks = [(s.taste || {}).likes ? "mix" : "search"].concat(
    MOODS.slice(0, 3).map((m) => "q:" + m));
  redraw(acts, sigOf(picks), (box) => {
  const mk = (label, icon, run, prim) => {
    const b = el("button", "btn" + (prim ? " prim" : ""));
    b.appendChild(svg(icon)); b.appendChild(el("span", null, label));
    b.addEventListener("click", run);
    box.appendChild(b);
  };
    if ((s.taste || {}).likes) mk("Make a mix from my likes", "sparkle", () => act("mix"), true);
    else mk("Search for something", "search", () => $("q").focus(), true);
    MOODS.slice(0, 3).forEach((m) => mk(m, "play", () => act("request", {q: m})));
  });
  }
  // the transport button is the one the eye is on, so its label carries the state
  $("playmixt").textContent = (s.now && s.now.title) ? (s.paused ? "Resume" : "Pause") : "Play";
  $("topup").disabled = !!s.busy;
}

/* ---------- one draw ---------- */
/* One broken panel must not cost you the other twelve.
   "the current UI doesnt change when the song changed" was that shape: a throw in an
   early region left the later ones - Now playing, the transport bar - holding the
   previous track, the tick kept firing, and nothing on screen admitted any of it. So
   every region is called through `region()`, which catches, names the culprit on the
   pill, and lets the rest of the page draw. */
function fail(where, e){
  const line = where + ": " + ((e && e.message) ? e.message : String(e));
  if (S.broken === line) return;              // once per distinct fault, not per tick
  S.broken = line;
  const p = $("jobpill");
  if (p) { p.textContent = "panel broke - " + line; p.classList.add("warn"); }
  console.error("[spotube-dj] " + line, e);
}
function region(name, fn){ try { fn(); } catch (e) { fail(name, e); } }

function draw(s){
  if (!s) return;
  S.state = s;
  S.online = true;
  const np = s.now || {};
  document.title = (np.title ? np.title + " · " : "") + "Spotube DJ";
  region("greeting", () => { $("greet").textContent = greeting(); });
  region("drawFilters", () => drawFilters());
  region("drawQuick", () => drawQuick(s));
  region("drawChips", () => drawChips(s));
  region("drawUpNext", () => drawUpNext(s));
  region("drawLibrary", () => drawLibrary(s));
  region("drawDetail", () => drawDetail(s));
  region("drawPlayer", () => drawPlayer(s));
  region("drawResults", () => drawResults(s));
  region("drawPage", () => drawPage(s));
  region("drawTaste", () => drawTaste(s));
  region("drawSettings", () => drawSettings(s));
  region("drawRecents", () => drawRecents(s));
  region("drawLog", () => drawLog(s));
  region("drawEmptyActs", () => drawEmptyActs(s));
  region("upnext-visibility", () => {
    // the queue is its own right-side panel now; the sidebar chips only filter what
    // the queue *holds* (drawUpNext), so it is never hidden by them
    const n = $("upnext"); if (n) n.hidden = false;
  });
  if (S.broken) { S.broken = ""; const p = $("jobpill"); if (p) p.classList.remove("warn"); }
}

/* ---------- search ---------- */
async function runSearch(){
  const q = $("q").value.trim();
  if (!q) return;
  setView("search");
  S.q = q;
  $("results").textContent = "";
  $("results").appendChild(el("div", "empty", "searching YouTube Music for " + JSON.stringify(q) + "..."));
  try { await post("/api/search", {q}); } catch (e) { toast(e.message); }
  try { draw(await (await fetch("/api/state")).json()); } catch (e) {}
}
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runSearch(); }
  else if (e.key === "Escape") { e.target.value = ""; }
});
$("mixfromq").addEventListener("click", () => act("request", {q: S.q || $("q").value.trim()}));
$("lib-sort").addEventListener("click", () => {
  S.sort = S.sort === "recent" ? "alpha" : "recent";
  $("lib-sort-l").textContent = S.sort === "recent" ? "Recents" : "Name";
  drawLibrary(S.state);
});
$("detail-close").addEventListener("click", () => $("app").classList.remove("wide"));
$("np-album").addEventListener("click", () => {
  const np = (S.state || {}).now || {};
  // open the album as an in-app page (it needs the album + artist names, which the
  // metadata fetch already put on the track). If the album is still resolving, the
  // action falls back to "songs by <artist>" rather than dead-clicking.
  setView("page");
  act("open_album", {album: np.album || "", artist: np.artist || ""});
});
$("open-settings").addEventListener("click", () => setView("library"));
$("engine").addEventListener("click", () => setView("library"));
$("nav-back").addEventListener("click", () => {
  if (S.hix > 0) { S.hix--; setView(S.hist[S.hix], false); }
});
$("nav-fwd").addEventListener("click", () => {
  if (S.hix < S.hist.length - 1) { S.hix++; setView(S.hist[S.hix], false); }
});
$("mixbtn").addEventListener("click", () => act("mix"));
$("page-back").addEventListener("click", () => setView("home"));
$("b-more").addEventListener("click", (e) => {
  // the menu the transport bar ends with: the verbs that are real but not a button
  // wide (pause vs resume, "not for me", unlove, leaving a station) belong here
  e.stopPropagation();
  const s = S.state || {};
  const items = [];
  if (s.paused) items.push({label: "Resume", icon: "play", fn: () => act("resume")});
  else items.push({label: "Pause", icon: "pause", fn: () => act("pause")});
  items.push({label: "This is not for me", icon: "next", fn: () => act("skip")});
  items.push({label: (s.autoplay ? "Autoplay on open: on" : "Autoplay on open: off"),
              icon: (s.autoplay ? "check" : "sparkle"),
              fn: () => act("autoplay", {on: s.autoplay ? "off" : "on"})});
  items.push({label: "Unlove this song", icon: "heart_o", fn: () => act("unlike")});
  if (s.station) items.push({label: "Leave the station", icon: "close",
                             fn: () => act("clear_station")});
  items.push("-");
  items.push({label: "Stop everything", icon: "stop", bad: true, fn: () => act("stop")});
  menu($("b-more"), items);
});
/* the queue now lives in the right Now Playing panel. A toggle, not a one-shot
   scroll-into-view: `scrollIntoView` walks *every* scrollable ancestor, so on a
   narrow window it jumped the page to the bottom and left the panel stranded with
   no way back up. This scrolls only the panel's own `.dbody`; the second press
   brings you back up to Now Playing. The panel itself is always opaque and its
   content always scrolls, so there is never a transparent dead box. */
$("b-queue").addEventListener("click", () => {
  const dbody = $("detail") && $("detail").querySelector(".dbody");
  const q = $("uph");
  if (!dbody || !q) return;
  const narrow = window.matchMedia("(max-width:1300px)").matches;
  // pressing the queue button again while it is showing scrolls back up to the top
  if (S._qback) { S._qback = false; dbody.scrollTo({top: 0, behavior: "smooth"}); return; }
  if (narrow) $("app").classList.add("wide");
  dbody.scrollTo({top: Math.max(0, q.offsetTop - 10), behavior: "smooth"});
  S._qback = true;
});
$("unkeybtn").addEventListener("click", async () => {
  try { draw((await post("/api/settings", {clear_key:"1"})).state); toast("key removed"); }
  catch (e) { toast(e.message); }
});
$("savebtn").addEventListener("click", async () => {
  const f = {base: $("in-base").value.trim(), model: $("in-model").value.trim()};
  const k = $("in-key").value.trim();
  if (k) f.key = k;
  try {
    const j = await post("/api/settings", f);
    draw(j.state); $("in-key").value = "";
    toast(j.note || "saved");
  } catch (e) { toast(e.message); }
});
let wipeArm = 0;
$("wipebtn").addEventListener("click", () => {
  if (Date.now() - wipeArm > 4000) {
    wipeArm = Date.now();
    $("wipebtn").querySelector("span").textContent = "Tap again to forget everything";
    return;
  }
  wipeArm = 0;
  $("wipebtn").querySelector("span").textContent = "Forget my taste";
  act("clear_taste", {sure: "1"});
});
$("undobtn").addEventListener("click", () => act("restore_taste"));

/* seek by click or drag; the bar is a slider, so the keyboard works on it too */
function seekTo(node, e){
  const r = node.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  const secs = (S.prog.dur || 0) * frac;
  $("fill").style.width = (frac * 100) + "%";
  $("knob").style.left = (frac * 100) + "%";
  $("t0").textContent = fmt(secs);
  return secs;
}
const bar = $("bar");
bar.addEventListener("pointerdown", (e) => {
  S.dragging = true; bar.classList.add("kb");
  S._secs = seekTo(bar, e);
  bar.setPointerCapture(e.pointerId);
});
bar.addEventListener("pointermove", (e) => { if (S.dragging) seekTo(bar, e); });
bar.addEventListener("pointerup", async (e) => {
  if (!S.dragging) return;
  S.dragging = false; bar.classList.remove("kb");
  const secs = seekTo(bar, e);
  try { draw((await post("/api/action", {action:"seek", secs:String(Math.round(secs))})).state); }
  catch (err) { toast(err.message); }
});
bar.addEventListener("keydown", (e) => {
  const step = e.key === "ArrowRight" ? 5 : e.key === "ArrowLeft" ? -5 : 0;
  if (!step) return;
  e.preventDefault();
  act("seek", {secs: String(Math.round(S.prog.pos + step))});
});
let volT = 0;
$("vol").addEventListener("input", (e) => {
  S.vol = Number(e.target.value);
  $("vol").style.setProperty("--vol", (S.vol) + "%");
  $("b-mute").innerHTML = "";
  $("b-mute").appendChild(svg(S.vol > 0 ? "vol" : "vol_mute"));
  clearTimeout(volT);
  volT = setTimeout(() => act("volume", {pct: String(S.vol)}), 220);
});
$("b-mute").addEventListener("click", () => {
  const to = S.vol > 0 ? 0 : (S.lastVol || 70);
  if (S.vol > 0) S.lastVol = S.vol;
  $("vol").value = String(to); S.vol = to;
  $("vol").style.setProperty("--vol", to + "%");
  act("volume", {pct: String(to)});
});
$("b-full").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => toast("the browser said no"));
});

/* ---------- the controls the page shares with the keyboard ---------- */
document.addEventListener("click", (e) => {
  const b = e.target.closest ? e.target.closest("[data-action],[data-view]") : null;
  if (!b) return;
  if (b.dataset.view) { setView(b.dataset.view); return; }
  const a = b.dataset.action;
  if (!a) return;
  if (b.disabled) return;
  act(a, b.dataset.id ? {id: b.dataset.id} : null);
});
document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === " ") { e.preventDefault(); act("playpause"); }
  else if (k === "n") act("next");
  else if (k === "p") act("prev");
  else if (k === "l") act("like");
  else if (k === "s") act("shuffle");
  else if (k === "r") act("repeat");
  else if (k === "m") $("b-mute").click();
  else if (k === "f") $("b-full").click();
  else if (k === "/") { e.preventDefault(); $("q").focus(); }
  else if (k === "arrowright") act("seek", {secs: String(Math.round(S.prog.pos + 10))});
  else if (k === "arrowleft") act("seek", {secs: String(Math.round(S.prog.pos - 10))});
});

/* ---------- the tick: pushed where it can, polled where it must ---------- */
/* /api/stream pushes one snapshot per server tick, which is what makes the row change
   the instant a track does. The poll stays on regardless - a proxy that buffers the
   stream, a browser that closes it, a laptop that sleeps: in every one of those cases
   the page must still be right a second later rather than frozen on the last song it
   saw. So: push for immediacy, poll for correctness, and drop to 3 s only while the
   push is demonstrably arriving. */
let streamed = false;
function subscribe(){
  if (!window.EventSource) return;
  let es = null;
  try { es = new EventSource("/api/stream"); } catch (e) { return; }
  es.onmessage = (m) => {
    streamed = true;
    try { draw(JSON.parse(m.data)); } catch (e) { fail("stream", e); }
  };
  es.onerror = () => { streamed = false; };    // the poll takes over by itself
}

let offlineAt = 0;
async function poll(){
  try {
    const r = await fetch("/api/state", {headers:{"X-Requested-With":"spotube-dj"}});
    if (r.ok) draw(await r.json());
    else fail("state", new Error("the server answered " + r.status));
  } catch (e) {
    // the difference between this and `catch (e) {}` is a sentence on screen: a tab
    // that quietly stops updating is indistinguishable from a player that stopped
    if (!offlineAt) {
      offlineAt = Date.now();
      const p = $("jobpill");
      if (p) { p.textContent = "the DJ stopped answering"; p.classList.add("warn"); }
    }
  }
  setTimeout(poll, document.hidden ? 4000 : (streamed ? 3000 : 900));
}
subscribe();
navSync();                     // home is the sole entry on load: back/forward both off
setInterval(drawProgress, 200);
if (!window.__noPoll) poll();
"""


def _icons_js() -> str:
    """The same SVG strings, for the buttons the JS builds at runtime."""
    return json.dumps(ICONS)


def app_constants() -> dict:
    """
    The small tables the page shares with the rest of the app, as JSON-ready data.

    Moods, views and the greeting ranges live in `viewmodel.py`; inlining them here
    is what stops the browser skin from keeping a second copy of a list that is
    supposed to be the product's opinion about what people want to listen to.
    """
    return {"moods": [{"label": label, "q": q} for label, q in vm.MOODS],
            "views": list(vm.VIEWS),
            "nav": [{"view": v, "icon": i, "label": l} for v, i, l in vm.NAV],
            "greeting": [list(g) for g in vm.GREETING]}


def page() -> str:
    """The finished document: palette substituted, self-contained, no outbound requests."""
    body = BODY
    for key, svg in ICONS.items():
        body = body.replace(f"@@{key}@@", svg)
    html = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta name="color-scheme" content="dark">',
            '<meta name="referrer" content="no-referrer">',
            "<title>Spotube DJ</title>",
            "<style>" + CSS + "</style></head><body>", body, "<script>"]
    # the script is emitted after the body so `ICONS` exists for the first draw
    html.append("const ICONS=" + _icons_js() + ";\n"
                + "const VM=" + json.dumps(app_constants()) + ";\n" + JS)
    html.append("</script></body></html>")
    out = "".join(html)
    for tok, val in COLORS.items():
        out = out.replace(tok, val)
    return out


if __name__ == "__main__":            # python3 -m webapp > /tmp/ui.html
    print(page())
