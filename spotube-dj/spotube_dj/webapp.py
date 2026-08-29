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
--tiles:@@TILES@@;--tint:#1f1f1f;--r:8px}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:#000;color:var(--text);overflow:hidden;
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

.app{display:grid;grid-template-columns:300px minmax(0,1fr) 340px;gap:8px;min-height:0}
.panel{background:var(--bg);border-radius:var(--r);min-height:0;position:relative;
isolation:isolate;overflow:hidden}

/* ---- the blurred cover: the one thing a terminal skin could never do ---- */
.bg{position:absolute;inset:-12% -8%;z-index:-2;background-size:cover;
background-position:center top;filter:blur(64px) saturate(1.6);opacity:.62;
transform:scale(1.06);transition:opacity .45s ease}
/* with no cover yet the wash is the two palette colours that tile would have used,
   so the page is never the flat grey it looked before a thumbnail landed */
.bg.flat{background-image:none!important;opacity:.42}
.scrim{position:absolute;inset:0;z-index:-1;
background:linear-gradient(180deg,rgba(0,0,0,.28),rgba(18,18,18,.82) 42%,var(--bg) 78%)}

/* ---- left: your library ---- */
.side{background:var(--bg);display:flex;flex-direction:column;min-height:0}
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
.filters{display:flex;gap:8px;padding:8px 14px;overflow-x:auto;scrollbar-width:none}
.filters::-webkit-scrollbar{display:none}
.chip{background:var(--hover);border-radius:999px;padding:6px 13px;color:var(--text);
font-size:13px;white-space:nowrap}
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
.tile img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.tile.big{width:100%;height:100%;border-radius:6px;font-size:64px}
.empty{color:var(--faint);font-size:13px;padding:18px 14px;line-height:1.6}
.empty b{display:block;color:var(--text);font-size:14px;margin-bottom:6px}

/* ---- middle: content ---- */
/* the panel holds the wash, the scroller holds the page. They are separate on
   purpose: an absolutely positioned backdrop inside a scroll container is sized
   against the *content* (3 000 px on a long queue), so the cover was cropped to a
   slice and scrolled off, which is why the blur looked absent below the fold. */
#uph{display:flex;align-items:baseline;gap:10px}
#uph #clearq{margin-left:auto;width:28px;height:28px;opacity:.7}
#uph #clearq:hover{opacity:1}
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
.quick{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.qp{display:flex;align-items:center;gap:0;background:rgba(255,255,255,.09);border-radius:6px;
height:64px;overflow:hidden;position:relative}
.qp:hover{background:rgba(255,255,255,.18)}
.qp .qt{flex:1;min-width:0;padding:0 12px;font-weight:700;font-size:14px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.qp .tile{width:64px;height:64px;border-radius:0;flex:none;font-size:22px}
.qp .fab{position:absolute;right:74px;opacity:0;transform:translateY(6px)}
.qp:hover .fab{opacity:1;transform:none}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:16px}
.card{background:var(--card);border-radius:8px;padding:12px;position:relative}
.card:hover{background:#282828}
.card .cover{position:relative;aspect-ratio:1/1;border-radius:6px;overflow:hidden;
background:var(--input);box-shadow:0 8px 24px rgba(0,0,0,.5)}
.card .cover .tile{width:100%;height:100%;border-radius:6px;font-size:44px}
.card .t{margin-top:11px;font-weight:700;font-size:14.5px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.card .a{color:var(--muted);font-size:12.5px;margin-top:3px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.fab{width:44px;height:44px;border-radius:50%;background:var(--accent);color:#000;
display:grid;place-items:center;box-shadow:0 6px 16px rgba(0,0,0,.55)}
.fab svg{width:20px;height:20px}
.fab:hover{transform:scale(1.06);background:#1fdf64}
.fab:active{transform:scale(.98)}
.card .fab,.row .fab{position:absolute;right:14px;bottom:64px;opacity:0;
transform:translateY(8px);transition:opacity .16s ease,transform .16s ease}
.card:hover .fab{opacity:1;transform:none}
.row .fab{right:44px;bottom:50%;transform:translateY(50%) scale(.9);width:34px;height:34px}
.row .fab svg{width:15px;height:15px}
.row:hover .fab{opacity:1;transform:translateY(50%) scale(1)}

.rows{display:flex;flex-direction:column}
.row{display:grid;grid-template-columns:22px 40px minmax(0,1.6fr) minmax(0,1fr) 52px 28px;
gap:14px;align-items:center;padding:6px 10px;border-radius:6px;position:relative}
.row:hover{background:rgba(255,255,255,.08)}
.row .n{color:var(--muted);font-size:13.5px;text-align:right;font-variant-numeric:tabular-nums}
.row .tile{width:40px;height:40px;font-size:14px}
.row .ti{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14.5px}
.row .ar{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;display:flex;align-items:center;gap:7px}
.row .du{color:var(--muted);font-size:13px;text-align:right;font-variant-numeric:tabular-nums}
.row .more{opacity:0}
.row:hover .more,.row:focus-within .more{opacity:1}
.row.playing .ti{color:var(--accent)}
.row.playing .n{color:var(--accent)}
.eq{display:none;gap:2px;align-items:flex-end;height:13px;width:14px}
.row.playing .eq{display:flex}
.row.playing .n span{display:none}
.eq i{width:3px;background:var(--accent);height:4px;animation:eq .9s ease-in-out infinite}
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
.dbody{overflow-y:auto;padding:0 16px 18px;flex:1;min-height:0}
.sect{margin-top:16px}
.sect h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.6px;
color:var(--faint)}
.why{background:rgba(255,255,255,.06);border-radius:8px;padding:11px 12px;font-size:13px;
color:var(--muted);line-height:1.55}
.why b{color:var(--text)}
.kv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 14px;font-size:13px}
.kv dt{color:var(--faint)}
.kv dd{margin:0;color:var(--text);overflow-wrap:anywhere}
.mini{display:flex;flex-direction:column;gap:2px}
.mini .m{display:grid;grid-template-columns:26px minmax(0,1fr) auto;gap:10px;align-items:center;
padding:5px 6px;border-radius:6px;font-size:13px}
.mini .m:hover{background:var(--hover)}
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
.ctrls .iconbtn.on{color:var(--accent);position:relative}
.ctrls .iconbtn.on::after{content:"";position:absolute;bottom:-1px;left:50%;
transform:translateX(-50%);width:4px;height:4px;border-radius:50%;background:var(--accent)}
.play{width:36px;height:36px;border-radius:50%;background:#fff;color:#000;display:grid;
place-items:center}
.play svg{width:17px;height:17px}
.play:hover{transform:scale(1.07)}
.seek{display:flex;align-items:center;gap:10px;width:100%;max-width:620px}
.seek small{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums;min-width:34px}
.seekbar{flex:1;height:14px;display:flex;align-items:center;cursor:pointer;position:relative}
.seekbar .t{height:4px;width:100%;background:rgba(255,255,255,.22);border-radius:2px;
overflow:hidden}
.seekbar .f{height:100%;width:0;background:#fff;border-radius:2px}
.seekbar:hover .f,.seekbar.kb .f{background:var(--accent)}
.seekbar .k{position:absolute;top:50%;width:12px;height:12px;border-radius:50%;background:#fff;
transform:translate(-50%,-50%) scale(0);transition:transform .12s;pointer-events:none}
.seekbar:hover .k{transform:translate(-50%,-50%) scale(1)}
.right{display:flex;align-items:center;gap:10px;justify-content:flex-end}
.vol{display:flex;align-items:center;gap:8px}
input[type=range]{-webkit-appearance:none;appearance:none;height:4px;border-radius:2px;
background:rgba(255,255,255,.24);width:92px;outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;
border-radius:50%;background:#fff;opacity:0}
.vol:hover input[type=range]::-webkit-slider-thumb{opacity:1}
input[type=range]::-moz-range-thumb{width:12px;height:12px;border:0;border-radius:50%;
background:#fff}

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
@media (max-width:1300px){.app{grid-template-columns:280px minmax(0,1fr)}
.detail{display:none}.app.wide{grid-template-columns:280px minmax(0,1fr) 320px}
.app.wide .detail{display:flex}}
@media (max-width:1000px){.app{grid-template-columns:minmax(0,1fr)}.side{display:none}
.app.nav .side{display:flex;position:absolute;inset:8px auto 80px 8px;width:300px;z-index:20;
box-shadow:0 18px 60px rgba(0,0,0,.8)}.cards{grid-template-columns:repeat(auto-fill,
minmax(140px,1fr))}.row{grid-template-columns:22px 40px minmax(0,1fr) 52px 28px}
.row .ar{display:none}.right .vol input{width:60px}}
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
  <div class="bg" id="bg-main"></div><div class="scrim"></div>
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
    <h2 id="uph">Up next<span class="sub" id="upc"></span><button class="iconbtn"
     id="clearq" data-action="clear_queue" title="Clear the queue (the song keeps playing)">@@close@@</button></h2>
    <div class="rows" id="upnext"></div>
    <div class="acts" id="empty-acts"></div>
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
  </div>
   </div>
 </main>

 <aside class="detail panel" id="detail">
  <div class="bg" id="bg-side"></div>
  <div class="dhead">
   <b>Now playing</b>
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
    <button class="btn ghost" data-action="stop" id="np-stop">@@stop@@<span>Stop</span></button>
   </div>
   <div class="sect"><h3>Why this song</h3><div class="why" id="np-why">
    type a mood above, or pick something on the left</div></div>
   <div class="sect"><h3>Credits</h3><dl class="kv" id="credits"></dl></div>
   <div class="sect"><h3 id="simh">In your likes</h3><div class="mini" id="simil"></div></div>
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
           prog:{pos:0, dur:0, playing:false, at:0}, hist:[], hix:-1, q:"", vol:70,
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
function art(node, track, px){
  const tint = paint(node, seedOf(track), px);
  const url = track && track.art;
  if (url) {
    const img = el("img"); img.src = url; img.alt = ""; img.loading = "lazy";
    img.onerror = () => img.remove();
    node.appendChild(img);
  }
  return tint;
}
/* the blurred backdrop: the cover, blown up and out of focus, behind the content */
function backdrop(track, playing){
  const url = track && track.art ? track.art : "";
  const t = playing ? (track.id || track.title || "dj") : "dj";
  let h = 0;
  for (const ch of String(t)) h = (h * 131 + ch.charCodeAt(0)) >>> 0;
  const pal = getComputedStyle(document.documentElement).getPropertyValue("--tiles").split(",");
  const other = pal[(h >> 5) % pal.length];
  const tint = playing ? pal[h % pal.length] : "#1f1f1f";
  document.documentElement.style.setProperty("--tint", tint);
  ["bg-main", "bg-side"].forEach((id) => {
    const n = $(id); if (!n) return;
    n.classList.toggle("flat", !url);
    // the colour underneath is what shows while an href is still downloading, or
    // when the image host is unreachable: the wash is never "nothing"
    n.style.backgroundImage = url
      ? "linear-gradient(160deg," + tint + "00," + other + "00)," + "url(" + JSON.stringify(url) + ")"
      : "linear-gradient(160deg," + tint + "," + other + ")";
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
function trackMenu(t){
  return [
    {label:"Play now", icon:"play", fn:() => act("play_row", {id: t.id || ""})},
    {label:"Queue next", icon:"queue", fn:() => act("queue_next", {id: t.id || ""})},
    {label:"Love this", icon:"heart_o", fn:() => act("love_row", {id: t.id || ""})},
    {label:"Start a station", icon:"mix", fn:() => act("radio", {id: t.id || ""})},
    "-",
    {label:"Stop", icon:"stop", bad:true, fn:() => act("stop")},
  ];
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
  ar.appendChild(el("span", null, t.artist || t.channel || "unknown artist"));
  if (t.note) ar.appendChild(el("span", "badge", t.note));
  r.appendChild(ar);
  r.appendChild(el("div", "du", t.dur || (opts.ts || "")));
  const more = el("button", "iconbtn more");
  more.appendChild(svg("dots")); more.title = "More";
  more.addEventListener("click", (e) => { e.stopPropagation(); menu(more, trackMenu(t)); });
  r.appendChild(more);
  const fab = el("button", "fab"); fab.appendChild(svg("play"));
  fab.title = "Play now";
  fab.addEventListener("click", (e) => { e.stopPropagation(); act("play_row", {id: t.id || ""}); });
  r.appendChild(fab);
  r.addEventListener("dblclick", () => act("play_row", {id: t.id || ""}));
  if (opts.current) r.classList.add("playing");
  return r;
}
function cardNode(t, i){
  const c = el("div", "card");
  const cov = el("div", "cover"); const tile = el("div", "tile");
  art(tile, t, 44); cov.appendChild(tile); c.appendChild(cov);
  c.appendChild(el("div", "t", t.title || "?"));
  c.appendChild(el("div", "a", t.artist || t.channel || "unknown artist"));
  const fab = el("button", "fab"); fab.appendChild(svg("play"));
  fab.title = "Play now";
  fab.addEventListener("click", () => act("play_row", {id: t.id || ""}));
  c.appendChild(fab);
  c.addEventListener("dblclick", () => act("play_row", {id: t.id || ""}));
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
function setView(name, push){
  if (name === S.view) return;
  ((VM && VM.views) || ["home"]).forEach((v) => {
    $("view-" + v).hidden = v !== name;
  });
  S.view = name;
  if (push !== false) { S.hist = S.hist.slice(0, S.hix + 1); S.hist.push(name); S.hix = S.hist.length - 1; }
  $("nav-back").disabled = S.hix <= 0;
  $("nav-fwd").disabled = S.hix >= S.hist.length - 1;
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
    items.push({label:m.label, note:"mood", q:m.q, act:() => act("request", {q:m.q})});
  });
  if ((s.taste && s.taste.likes) || loves.length) {
    items.push({label:"Loved songs", note:"mix", q:"", act:() => act("mix")});
  }
  if (s.station) items.push({label:"Station: " + s.station, note:"playing set", q:"", act:() => setView("home")});
  const used = {};
  (((s.library || {}).moods) || []).forEach((m) => {
    if (!m || !m.q || used[String(m.q).toLowerCase()]) return;
    used[String(m.q).toLowerCase()] = 1;
    items.push({label:m.q, note:m.note || "last played", q:m.q,
                act:() => act("request", {q:m.q})});
  });
  MOODS.forEach((m) => {
    if (items.length >= 6 || used[m.toLowerCase()]) return;
    used[m.toLowerCase()] = 1;
    items.push({label:m, note:"mood", q:m, act:() => act("request", {q:m})});
  });
  redraw(box, sigOf(items.map((it) => [it.label, it.note, it.q])), (bx) => items.forEach((it) => {
    const b = el("button", "qp");
    b.appendChild(el("span", "qt", it.label));
    const fab = el("span", "fab"); fab.appendChild(svg("play")); b.appendChild(fab);
    const tile = el("span", "tile"); paint(tile, it.q || it.label, 24); b.appendChild(tile);
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
  setText(head, rows.length + " queued" + (s.request ? " from " + JSON.stringify(s.request) : ""));
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
    rows.forEach((t, i) => b.appendChild(rowNode(t, i, {current: playing && t.id === playing}))));
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
    b.appendChild(el("span", null, np.artist || np.channel || "unknown artist"));
    if (tick) b.appendChild(svg("verified"));
  });
  by.hidden = !np.title;
  art($("np-art"), np, 70);
  backdrop(np, !!np.title);
  setText($("np-why"), s.idle_note || "type a mood above, or pick something on the left");
  // credits are stable while a track plays; only the position moves, and that line
  // is left out of the signature so the list is not rebuilt (and re-scrolled) twice
  // a second for a number the progress bar already shows
  const dl = $("credits");
  const pairs = [["played by", "YouTube Music"],
                 ["channel", np.channel || ""],
                 ["found by", np.found || ""],
                 ["length", np.dur || (s.duration ? fmt(s.duration) : "")],
                 ["audio", np.cached ? "on this disk (cache)" : "streamed"],
                 ["source", s.backend || ""],
                 ["cache", (s.cache_note || "").replace("audio cache: ", "")]];
  redraw(dl, sigOf([pairs, !!np.title]), (b) => {
    pairs.forEach(([k, v]) => {
      if (!v && v !== 0) return;
      b.appendChild(el("dt", null, k)); b.appendChild(el("dd", null, String(v)));
    });
    if (!np.title) return;
    b.appendChild(el("dt", null, "position"));
    const pos = el("dd", null, ""); pos.id = "np-pos"; b.appendChild(pos);
  });
  const pos = $("np-pos");
  if (pos) setText(pos, s.position ? fmt(s.position) + " / " + fmt(s.duration) : "");
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
  art($("bar-art"), np, 16);
  $("cache").textContent = s.cache_note || "";
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
  eng.textContent = s.engine_note || "brain: offline parser";
  eng.classList.toggle("good", /gemini|ollama|llm/i.test(eng.textContent));
  S.prog = {pos: Number(s.position) || 0, dur: Number(s.duration) || 0,
            playing: !!(np.title && !s.paused), at: Date.now()};
  $("vol").value = String(s.volume === undefined ? S.vol : s.volume);
  S.vol = Number($("vol").value);
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
  region("drawTaste", () => drawTaste(s));
  region("drawSettings", () => drawSettings(s));
  region("drawRecents", () => drawRecents(s));
  region("drawLog", () => drawLog(s));
  region("drawEmptyActs", () => drawEmptyActs(s));
  region("upnext-visibility", () => {
    $("upnext").hidden = S.filter === "artists" || S.filter === "moods";
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
$("open-settings").addEventListener("click", () => setView("library"));
$("engine").addEventListener("click", () => setView("library"));
$("nav-back").addEventListener("click", () => {
  if (S.hix > 0) { S.hix--; setView(S.hist[S.hix], false); }
});
$("nav-fwd").addEventListener("click", () => {
  if (S.hix < S.hist.length - 1) { S.hix++; setView(S.hist[S.hix], false); }
});
$("mixbtn").addEventListener("click", () => act("mix"));
$("b-more").addEventListener("click", (e) => {
  // the menu the transport bar ends with: the verbs that are real but not a button
  // wide (pause vs resume, "not for me", unlove, leaving a station) belong here
  e.stopPropagation();
  const s = S.state || {};
  const items = [];
  if (s.paused) items.push({label: "Resume", icon: "play", fn: () => act("resume")});
  else items.push({label: "Pause", icon: "pause", fn: () => act("pause")});
  items.push({label: "This is not for me", icon: "next", fn: () => act("skip")});
  items.push({label: "Unlove this song", icon: "heart_o", fn: () => act("unlike")});
  if (s.station) items.push({label: "Leave the station", icon: "close",
                             fn: () => act("clear_station")});
  items.push("-");
  items.push({label: "Stop everything", icon: "stop", bad: true, fn: () => act("stop")});
  menu($("b-more"), items);
});
$("b-queue").addEventListener("click", () => {
  // the queue lives in the middle column, so this scrolls to it rather than opening
  // a second list that would drift out of sync with the first
  setView("home");
  const n = $("uph");
  if (n && n.scrollIntoView) n.scrollIntoView({behavior:"smooth", block:"start"});
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
  $("b-mute").innerHTML = "";
  $("b-mute").appendChild(svg(S.vol > 0 ? "vol" : "vol_mute"));
  clearTimeout(volT);
  volT = setTimeout(() => act("volume", {pct: String(S.vol)}), 220);
});
$("b-mute").addEventListener("click", () => {
  const to = S.vol > 0 ? 0 : (S.lastVol || 70);
  if (S.vol > 0) S.lastVol = S.vol;
  $("vol").value = String(to); S.vol = to;
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
