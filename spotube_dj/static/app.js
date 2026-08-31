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
  // DJ voice dropdown: only rebuild its options when the effective voice (or the
  // catalog) changes, so a mid-click selection is never overwritten by the tick.
  const sel = $("in-voice");
  if (sel && (sel.dataset.current !== st.dj_voice)) {
    const current = st.dj_voice || "Despina";
    sel.options.length = 0;
    for (const v of (st.dj_voices || [])) {
      const o = document.createElement("option");
      o.value = v.name;
      o.textContent = v.name + (v.trait ? " · " + v.trait : "");
      if (v.lang) o.textContent += " · " + v.lang;
      if (v.name === current) o.selected = true;
      sel.appendChild(o);
    }
    sel.dataset.current = current;
  }
  // DJ language dropdown. Rebuilt only when the effective language (or the choice
  // list) changes, so an in-flight selection is not clobbered by the tick.
  const lsel = $("in-lang");
  if (lsel && (lsel.dataset.current !== (st.dj_lang || "Indonesian"))) {
    const current = st.dj_lang || "Indonesian";
    lsel.options.length = 0;
    for (const l of (st.dj_langs || ["Indonesian", "English", "Arabic"])) {
      const o = document.createElement("option");
      o.value = l;
      o.textContent = l;
      if (l === current) o.selected = true;
      lsel.appendChild(o);
    }
    lsel.dataset.current = current;
  }
  $("voice-lang").textContent = (st.dj_voice || "Despina") + " announces in " +
    (st.dj_lang || "Indonesian");
  $("unkeybtn").hidden = !st.key_set;
  drawWorker(s);
  $("engine2").textContent = st.key_set
    ? "Planning goes through " + (st.model || "the default model") + " at " +
      (st.base || "generativelanguage.googleapis.com") + ", and the DJ's voice is " +
      (st.dj_voice || "Despina") + ". The key is only ever sent back as "
      + (st.key_mask || "a mask") + "."
    : "No key saved, so a small offline parser plans the searches and the DJ voice stays " +
      "quiet/robotic. Paste a Gemini key (or a local model URL) and the DJ speaks in a real voice.";
  $("setnote").textContent = st.note || "";
}
/* The Worker card. It says which of the four things is true - no URL, a URL that
   does not answer, one that answers but has no D1, or one that works - because
   "engine: offline" is the same sentence for all four and they have four
   different fixes. */
function drawWorker(s){
  const w = s.worker || {};
  const fillIn = (node, v) => { if (node && document.activeElement !== node) node.value = v; };
  const st = s.settings || {};
  fillIn($("in-wurl"), st.worker_url || "");
  fillIn($("in-wprofile"), st.worker_profile || "default");
  const ws = $("in-wsync");
  if (ws && document.activeElement !== ws) ws.value = (st.worker_sync === "off") ? "off" : "on";
  const bits = [];
  if (!w.configured) {
    bits.push("No Worker URL, so the planner is the offline parser and the DJ does "
      + "not speak. Deploy worker/ (see worker/README.md) and paste the URL here.");
  } else if (!w.on) {
    bits.push("Cloud sync is off: the Worker still plans and speaks, but nothing is "
      + "saved to D1.");
  } else if (w.ok === false && w.detail) {
    bits.push("The Worker is not answering: " + w.detail);
  } else {
    bits.push("Profile '" + (w.profile || "default") + "' is mirrored to " + (w.url || "")
      + (w.pushed_at ? " · last saved " + hhmm(w.pushed_at) : " · not saved yet"));
    if (w.pending) bits.push(w.pending + " taste event(s) queued");
  }
  setText($("worker-note"), bits.join(" "));
  const sub = [];
  if (st.worker_note) sub.push(st.worker_note);
  if (st.worker_token_set) sub.push("a Worker token is saved (never shown back)");
  setText($("worker-sub"), sub.join(" · "));
}
function hhmm(ms){
  try { const d = new Date(Number(ms));
    return String(d.getHours()).padStart(2,"0") + ":" + String(d.getMinutes()).padStart(2,"0");
  } catch (e) { return ""; }
}

/* The spoken DJ: the server publishes a clip, the page plays it. An <audio>
   element rather than Audio() so the element survives a redraw, and the play()
   promise is caught because a browser that has never seen a gesture refuses it -
   a rejected promise here used to throw into the tick and blank the pill. */
let voiceEl = null, voiceId = "";
function drawVoice(s){
  const clip = s.voice_clip;
  if (!clip || !clip.url) return;
  if (clip.id === voiceId) return;                 // already playing this one
  voiceId = clip.id;
  if (!voiceEl) { voiceEl = new Audio(); voiceEl.volume = 0.9; }
  voiceEl.src = clip.url;
  const p = voiceEl.play();
  if (p && p.catch) p.catch(() => {
    // autoplay blocked: the next click on the page will let it through, so say
    // so once instead of leaving the listener wondering why the DJ went quiet
    toast("the browser blocked audio - click anywhere to hear the DJ");
  });
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
  region("drawVoice", () => drawVoice(s));
  region("drawRecents", () => drawRecents(s));
  region("drawLog", () => drawLog(s));
  region("drawEmptyActs", () => drawEmptyActs(s));
  region("drawDJ", () => drawDJ(s));
  region("upnext-visibility", () => {
    // the queue is its own right-side panel now; the sidebar chips only filter what
    // the queue *holds* (drawUpNext), so it is never hidden by them
    const n = $("upnext"); if (n) n.hidden = false;
  });
  if (S.broken) { S.broken = ""; const p = $("jobpill"); if (p) p.classList.remove("warn"); }
}

/* ---------- the DJ, Spotify-style (automatic, no box to type into) ---------- */
function drawDJ(s){
  const t = $("djtext");
  if (!t) return;
  // one short line that says why this song is playing and what's coming next. It
  // is built by the server from what the mixer actually did, so it is always on
  // and needs no Gemini key, no websocket and no chat.
  const line = (s && s.dj_line) || "";
  if (line) t.textContent = line;
  else t.textContent = "Nothing playing yet - tell me a song or a mood.";
  // the voice toggle: on by default, reflects the server's persisted `voice` flag
  const v = $("djvoice");
  if (v) {
    const on = s && s.voice;
    v.classList.toggle("on", !!on);
    const vt = $("djvoicet");
    if (vt) vt.textContent = on ? "on" : "off";
  }
  // say which voice is behind the button (Gemini/Despina when a key is set)
  const vn = $("djvnote");
  if (vn) vn.textContent = (s && s.voice_note) || "";
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
  const v = $("in-voice");
  if (v && v.value) f.voice = v.value;
  const l = $("in-lang");
  if (l && l.value) f.lang = l.value;
  try {
    const j = await post("/api/settings", f);
    draw(j.state); $("in-key").value = "";
    toast(j.note || "saved");
  } catch (e) { toast(e.message); }
});
$("wsavebtn").addEventListener("click", async () => {
  const f = {worker_url: $("in-wurl").value.trim(),
             worker_profile: $("in-wprofile").value.trim(),
             worker_sync: $("in-wsync").value};
  const t = $("in-wtoken").value.trim();
  if (t) f.worker_token = t;
  try {
    const j = await post("/api/settings", f);
    draw(j.state); $("in-wtoken").value = "";
    toast(j.note || "saved");
  } catch (e) { toast(e.message); }
});
let pullArm = 0;
document.querySelectorAll('[data-action="worker_pull"]').forEach((b) => {
  b.addEventListener("click", (e) => {
    // this one can overwrite tonight's listening with the cloud copy, so it
    // asks on the page the way clear_taste asks on the wire
    if (Date.now() - pullArm > 4000) {
      e.stopPropagation();
      pullArm = Date.now();
      b.querySelector("span").textContent = "Tap again to replace this taste";
      return;
    }
    pullArm = 0;
    b.querySelector("span").textContent = "Pull my taste";
  });
});
$("in-voice").addEventListener("change", () => {
  const v = $("in-voice");
  const l = $("in-lang");
  $("voice-lang").textContent = v.value + " announces in " +
    ((l && l.value) ? l.value : "Indonesian");
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
