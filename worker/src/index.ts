/**
 * Edge-compatible Spotube DJ.
 *
 * This is deliberately a separate runtime from ../spotube_dj. Cloudflare Workers
 * cannot run the desktop Python process, mpv, or yt-dlp. The Worker plans a queue
 * from YouTube Music metadata and the browser plays the selected video through
 * YouTube's official IFrame Player API. D1 keeps the small taste profile alive
 * across Worker restarts.
 */

interface Env {
  DB?: D1Database;
  ASSETS?: Fetcher;
  APP_PASSWORD?: string;
  SESSION_SECRET?: string;
  GEMINI_API_KEY?: string;
  GEMINI_MODEL?: string;
}

type RepeatMode = "off" | "all" | "one";

type Track = {
  id: string;
  title: string;
  artist: string;
  channel: string;
  duration: number;
  thumbnail: string;
  url: string;
  query?: string;
  official?: boolean;
  score?: number;
};

type SkippedTrack = {
  id: string;
  artist: string;
  title: string;
  at: number;
};

type DJState = {
  request: string;
  now: Track | null;
  queue: Track[];
  liked: Track[];
  skipped: SkippedTrack[];
  artists: Record<string, number>;
  repeat: RepeatMode;
  shuffle: boolean;
  paused: boolean;
  position: number;
  duration: number;
  message: string;
  engine: string;
  updatedAt: number;
};

type SearchResult = Track & { reason?: string };

const STATE_ID = "default";
const YTM_ENDPOINT = "https://music.youtube.com/youtubei/v1/search";
const YTM_CLIENT = {
  clientName: "WEB_REMIX",
  clientVersion: "1.20250101.00.00",
  gl: "US",
  hl: "en",
};
const AUTH_COOKIE = "spotube_auth";
const MAX_QUEUE = 40;
const MAX_LIKES = 200;
const MAX_SKIPS = 200;

let memoryState: DJState | null = null;

function defaultState(): DJState {
  return {
    request: "",
    now: null,
    queue: [],
    liked: [],
    skipped: [],
    artists: {},
    repeat: "off",
    shuffle: false,
    paused: true,
    position: 0,
    duration: 0,
    message: "Tell the DJ what you want to hear.",
    engine: "Cloudflare Worker",
    updatedAt: Date.now(),
  };
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function json(data: unknown, status = 200, extra: HeadersInit = {}): Response {
  const headers = new Headers(extra);
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("cache-control", "no-store");
  return new Response(JSON.stringify(data), { status, headers });
}

function errorResponse(message: string, status = 400): Response {
  return json({ ok: false, error: message }, status);
}

function clean(value: unknown, max = 240): string {
  return String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, max);
}

function norm(value: unknown): string {
  return clean(value, 240).toLocaleLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function publicState(state: DJState): DJState {
  // The state contains only playback metadata and taste values. Keep the response
  // bounded even if a damaged database row contains unexpectedly large arrays.
  const out = clone(state);
  out.queue = out.queue.slice(0, MAX_QUEUE);
  out.liked = out.liked.slice(-MAX_LIKES);
  out.skipped = out.skipped.slice(-MAX_SKIPS);
  return out;
}

function normalizeTrack(value: unknown): Track | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const id = clean(row.id, 32);
  const title = clean(row.title, 180);
  if (!id || !title || !/^[A-Za-z0-9_-]+$/.test(id)) return null;
  const duration = Number(row.duration || 0);
  return {
    id,
    title,
    artist: clean(row.artist, 120),
    channel: clean(row.channel, 120),
    duration: Number.isFinite(duration) ? Math.max(0, Math.min(86400, duration)) : 0,
    thumbnail: clean(row.thumbnail, 500),
    url: `https://music.youtube.com/watch?v=${encodeURIComponent(id)}`,
    query: clean(row.query, 180) || undefined,
    official: Boolean(row.official),
    score: Number.isFinite(Number(row.score)) ? Number(row.score) : undefined,
  };
}

function normalizeState(value: unknown): DJState {
  const base = defaultState();
  if (!value || typeof value !== "object") return base;
  const raw = value as Record<string, unknown>;
  const queueValues = Array.isArray(raw.queue) ? raw.queue : [];
  const likedValues = Array.isArray(raw.liked) ? raw.liked : [];
  const tracks = queueValues.map(normalizeTrack).filter(Boolean) as Track[];
  const liked = likedValues.map(normalizeTrack).filter(Boolean) as Track[];
  const skipped = Array.isArray(raw.skipped)
    ? raw.skipped
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
        .map((item) => ({
          id: clean(item.id, 32),
          artist: clean(item.artist, 120),
          title: clean(item.title, 180),
          at: Number(item.at) || Date.now(),
        }))
        .filter((item) => item.id)
    : [];
  const artists: Record<string, number> = {};
  if (raw.artists && typeof raw.artists === "object") {
    for (const [name, weight] of Object.entries(raw.artists as Record<string, unknown>)) {
      const n = norm(name);
      const w = Number(weight);
      if (n && Number.isFinite(w)) artists[n] = Math.max(-20, Math.min(20, w));
    }
  }
  const repeat = raw.repeat === "all" || raw.repeat === "one" ? raw.repeat : "off";
  const now = normalizeTrack(raw.now);
  const position = Number(raw.position) || 0;
  const duration = Number(raw.duration) || now?.duration || 0;
  return {
    request: clean(raw.request, 240),
    now,
    queue: (tracks || []).slice(0, MAX_QUEUE),
    liked: (liked || []).slice(-MAX_LIKES),
    skipped: skipped.slice(-MAX_SKIPS),
    artists,
    repeat,
    shuffle: Boolean(raw.shuffle),
    paused: raw.paused !== false,
    position: Math.max(0, Math.min(86400, position)),
    duration: Math.max(0, Math.min(86400, duration)),
    message: clean(raw.message, 300) || base.message,
    engine: "Cloudflare Worker",
    updatedAt: Number(raw.updatedAt) || Date.now(),
  };
}

async function loadState(env: Env): Promise<DJState> {
  if (!env.DB) {
    if (!memoryState) memoryState = defaultState();
    return clone(memoryState);
  }
  try {
    const row = await env.DB
      .prepare("SELECT state_json FROM app_state WHERE id = ?1")
      .bind(STATE_ID)
      .first<{ state_json: string }>();
    if (row?.state_json) return normalizeState(JSON.parse(row.state_json));
  } catch (error) {
    console.error("D1 read failed", error);
  }
  return defaultState();
}

async function saveState(env: Env, state: DJState): Promise<void> {
  state.updatedAt = Date.now();
  const normalized = normalizeState(state);
  if (!env.DB) {
    memoryState = clone(normalized);
    return;
  }
  await env.DB
    .prepare(
      "INSERT INTO app_state(id, state_json, updated_at) VALUES(?1, ?2, ?3) " +
        "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at",
    )
    .bind(STATE_ID, JSON.stringify(normalized), normalized.updatedAt)
    .run();
}

function textOf(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const node = value as Record<string, unknown>;
  if (Array.isArray(node.runs)) {
    return node.runs
      .filter((run): run is Record<string, unknown> => Boolean(run && typeof run === "object"))
      .map((run) => String(run.text || ""))
      .join("");
  }
  if (typeof node.simpleText === "string") return node.simpleText;
  if (typeof node.text === "string") return node.text;
  return "";
}

function collectMusicRows(value: unknown, rows: Record<string, unknown>[]): void {
  if (Array.isArray(value)) {
    for (const item of value) collectMusicRows(item, rows);
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (key === "musicResponsiveListItemRenderer" && child && typeof child === "object") {
      rows.push(child as Record<string, unknown>);
    } else {
      collectMusicRows(child, rows);
    }
  }
}

function columns(row: Record<string, unknown>): string[] {
  const source = Array.isArray(row.flexColumns) ? row.flexColumns : [];
  return source
    .map((column) => {
      if (!column || typeof column !== "object") return "";
      const renderer = (column as Record<string, unknown>)
        .musicResponsiveListItemFlexColumnRenderer;
      return renderer && typeof renderer === "object"
        ? textOf((renderer as Record<string, unknown>).text)
        : "";
    })
    .map((value) => clean(value, 240))
    .filter(Boolean);
}

function durationOf(value: string): number {
  const parts = value.trim().split(":").map((part) => Number(part));
  if (!parts.length || parts.some((part) => !Number.isFinite(part))) return 0;
  if (parts.length === 2 && parts[0] >= 0 && parts[1] >= 0 && parts[1] < 60) {
    return parts[0] * 60 + parts[1];
  }
  if (parts.length === 3 && parts[0] >= 0 && parts[1] >= 0 && parts[2] >= 0 && parts[1] < 60 && parts[2] < 60) {
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }
  return 0;
}

function thumbnailOf(row: Record<string, unknown>, id: string): string {
  const found: Array<{ url: string; area: number }> = [];
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const item of value) visit(item);
      return;
    }
    if (!value || typeof value !== "object") return;
    const object = value as Record<string, unknown>;
    if (typeof object.url === "string" && /^https?:\/\//.test(object.url)) {
      found.push({
        url: object.url.split("?", 1)[0],
        area: Number(object.width || 0) * Number(object.height || 0),
      });
    }
    for (const child of Object.values(object)) visit(child);
  };
  visit(row.thumbnail);
  const largest = found.sort((a, b) => b.area - a.area)[0]?.url;
  return largest || `https://i.ytimg.com/vi/${encodeURIComponent(id)}/hqdefault.jpg`;
}

function artistFrom(row: Record<string, unknown>, title: string, parts: string[], official: boolean): string {
  if (official) {
    const candidate = parts.find(
      (part) => part.toLocaleLowerCase() !== "song" && !durationOf(part) && !/views?\b/i.test(part),
    );
    if (candidate) return clean(candidate, 120);
  }
  const overlayNode = row.overlay;
  const overlay = overlayNode && typeof overlayNode === "object"
    ? (overlayNode as Record<string, unknown>).musicItemThumbnailOverlayRenderer
    : undefined;
  const contentNode = overlay && typeof overlay === "object"
    ? (overlay as Record<string, unknown>).content
    : undefined;
  const content = contentNode && typeof contentNode === "object"
    ? (contentNode as Record<string, unknown>).musicPlayButtonRenderer
    : undefined;
  for (const key of ["accessibilityPlayData", "accessibilityPauseData"]) {
    const contentObject = content && typeof content === "object"
      ? content as Record<string, unknown>
      : undefined;
    const label = contentObject
      ? ((contentObject[key] as Record<string, unknown> | undefined)?.accessibilityData as Record<string, unknown> | undefined)?.label
      : undefined;
    if (typeof label !== "string") continue;
    const withoutVerb = label.replace(/^\s*(?:Play|Pause)\s+/i, "");
    const pieces = withoutVerb.split(/\s[-–—]\s/);
    const candidate = pieces.length > 1 ? pieces[pieces.length - 1].replace(/\s+from\s+.*$/i, "") : "";
    if (candidate && norm(candidate) !== norm(title)) return clean(candidate, 120);
  }
  return "";
}

function isRejected(title: string, duration: number, official: boolean): string | null {
  const value = title.toLocaleLowerCase();
  // Do not reject titles such as "Live Forever"; only phrases/tags that describe
  // the recording as a performance are considered live takes.
  if (
    /\((?:[^)]*\b(?:live|unplugged|concert|performance|festival)\b[^)]*)\)|\[(?:[^\]]*\b(?:live|unplugged|concert|performance|festival)\b[^\]]*)\]/i.test(title) ||
    /\b(?:live\s+(?:at|from|in|version|take|performance|concert)|recorded\s+live|mtv\s+unplugged|full\s+(?:concert|show|set))\b/i.test(title)
  ) {
    return "live performance";
  }
  if (duration > 15 * 60 || /\b(?:dj\s+set|full\s+album|best\s+of|compilation|\d+\s*[- ]?hours?)\b/i.test(value)) {
    return "long-form upload";
  }
  if (!official && /\b(?:cover|karaoke|tribute|reaction|tutorial|review|lyrics?|remix|reverb|slowed|sped\s*up|nightcore)\b/i.test(value)) {
    return "not an original music recording";
  }
  if (/\b(?:unboxing|podcast|trailer|movie|film|fight scene|full match|gameplay|how to)\b/i.test(value)) {
    return "not a song";
  }
  return null;
}

async function ytmSearch(query: string, limit = 12): Promise<SearchResult[]> {
  const response = await fetch(YTM_ENDPOINT, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "user-agent": "Mozilla/5.0 spotube-dj-worker",
    },
    body: JSON.stringify({ context: { client: YTM_CLIENT }, query }),
  });
  if (!response.ok) throw new Error(`YouTube Music returned HTTP ${response.status}`);
  const payload = (await response.json()) as unknown;
  const rows: Record<string, unknown>[] = [];
  collectMusicRows(payload, rows);
  const results: SearchResult[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    const id = clean((row.playlistItemData as Record<string, unknown> | undefined)?.videoId, 32);
    const fields = columns(row);
    if (!id || !fields.length || seen.has(id)) continue;
    seen.add(id);
    const title = fields[0];
    const parts = (fields[1] || "")
      .split(/[•·|]/)
      .map((part) => clean(part, 120))
      .filter(Boolean);
    const official = Boolean(parts[0] && /^song\b/i.test(parts[0]));
    const duration = parts.reduce((longest, part) => Math.max(longest, durationOf(part)), 0);
    const artist = artistFrom(row, title, parts, official);
    const channel = official ? artist : parts[0] || "";
    const reason = isRejected(title, duration, official);
    results.push({
      id,
      title,
      artist,
      channel,
      duration,
      thumbnail: thumbnailOf(row, id),
      url: `https://music.youtube.com/watch?v=${encodeURIComponent(id)}`,
      official,
      reason: reason || undefined,
    });
    if (results.length >= limit * 3) break;
  }
  return results;
}

function seedQueries(request: string, state: DJState): string[] {
  if (request) return [request];
  const artists = Object.entries(state.artists)
    .filter(([, weight]) => weight > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([artist]) => `${artist} music`);
  if (artists.length) return artists;
  return ["indie pop music"];
}

async function plannedQueries(request: string, state: DJState, env: Env): Promise<string[]> {
  const fallback = seedQueries(request, state);
  if (!env.GEMINI_API_KEY || !request) return fallback;
  try {
    const model = encodeURIComponent(env.GEMINI_MODEL || "gemini-2.5-flash");
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-goog-api-key": env.GEMINI_API_KEY,
        },
        body: JSON.stringify({
          contents: [{
            parts: [{
              text: [
                "You are a music search planner.",
                "Return only a JSON array of at most three short YouTube Music search queries.",
                "Do not include explanations, markdown, live recordings, covers, remixes, or podcasts.",
                `Listener request: ${request}`,
              ].join("\\n"),
            }],
          }],
          generationConfig: { temperature: 0.2, responseMimeType: "application/json" },
        }),
      },
    );
    if (!response.ok) return fallback;
    const payload = (await response.json()) as Record<string, unknown>;
    const candidates = payload.candidates as Array<Record<string, unknown>> | undefined;
    const content = candidates?.[0]?.content as Record<string, unknown> | undefined;
    const parts = content?.parts as Array<Record<string, unknown>> | undefined;
    const text = parts?.map((part) => String(part.text || "")).join("") || "";
    const parsed = JSON.parse(text) as unknown;
    if (!Array.isArray(parsed)) return fallback;
    const queries = parsed
      .filter((item): item is string => typeof item === "string")
      .map((item) => clean(item, 180))
      .filter(Boolean)
      .slice(0, 3);
    return queries.length ? queries : fallback;
  } catch {
    // Search still works when the optional planner is unavailable, rate limited,
    // or returns malformed JSON.
    return fallback;
  }
}

function scoreTrack(track: Track, query: string, state: DJState): number {
  const terms = norm(query).split(" ").filter((term) => term.length > 2);
  const haystack = `${norm(track.title)} ${norm(track.artist)} ${norm(track.channel)}`;
  let score = track.official ? 3 : 0;
  score += terms.reduce((sum, term) => sum + (haystack.includes(term) ? 1.5 : 0), 0);
  score += state.artists[norm(track.artist)] || 0;
  if (state.liked.some((liked) => liked.id === track.id)) score += 4;
  if (state.skipped.some((skipped) => skipped.id === track.id)) score -= 8;
  return score;
}

async function buildMix(request: string, state: DJState, env: Env, limit = 20): Promise<Track[]> {
  const queries = (await plannedQueries(clean(request), state, env)).slice(0, 3);
  const pages = await Promise.all(queries.map((query) => ytmSearch(query, Math.max(8, limit))));
  const unique = new Map<string, Track>();
  for (let index = 0; index < pages.length; index += 1) {
    for (const row of pages[index]) {
      if (row.reason) continue;
      const track = normalizeTrack({ ...row, query: queries[index] });
      if (!track) continue;
      track.score = scoreTrack(track, request || queries[index], state);
      const existing = unique.get(track.id);
      if (!existing || (track.score || 0) > (existing.score || 0)) unique.set(track.id, track);
    }
  }
  let tracks = [...unique.values()].sort((a, b) => (b.score || 0) - (a.score || 0));
  if (state.shuffle) tracks = tracks.sort(() => Math.random() - 0.5);
  return tracks.slice(0, Math.max(1, Math.min(limit, MAX_QUEUE)));
}

function fieldsFromUrl(url: URL): Record<string, string> {
  const out: Record<string, string> = {};
  url.searchParams.forEach((value, key) => { out[key] = value; });
  return out;
}

async function bodyFields(request: Request): Promise<Record<string, string>> {
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    const value = (await request.json()) as unknown;
    if (!value || typeof value !== "object") return {};
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, val]) => [key, String(val ?? "")]),
    );
  }
  const params = new URLSearchParams(await request.text());
  const out: Record<string, string> = {};
  params.forEach((value, key) => { out[key] = value; });
  return out;
}

function trackFromFields(fields: Record<string, string>): Track | null {
  if (!fields.track) return null;
  try {
    return normalizeTrack(JSON.parse(fields.track));
  } catch {
    return null;
  }
}

function artistBump(state: DJState, artist: string, amount: number): void {
  const key = norm(artist);
  if (!key) return;
  state.artists[key] = Math.max(-20, Math.min(20, (state.artists[key] || 0) + amount));
}

async function advance(state: DJState, env: Env, markSkipped: boolean): Promise<void> {
  const current = state.now;
  if (current && markSkipped) {
    state.skipped.push({ id: current.id, artist: current.artist, title: current.title, at: Date.now() });
    state.skipped = state.skipped.slice(-MAX_SKIPS);
    artistBump(state, current.artist, -2.4);
  }
  if (current && state.repeat === "one") {
    state.position = 0;
    state.paused = false;
    state.message = `Repeating ${current.title}`;
    return;
  }
  if (current && state.repeat === "all") state.queue.push(current);
  let next = state.queue.shift() || null;
  if (!next && state.request) {
    const refill = await buildMix(state.request, state, env, 16);
    next = refill.shift() || null;
    state.queue.push(...refill);
  }
  state.now = next;
  state.position = 0;
  state.duration = next?.duration || 0;
  state.paused = !next;
  state.message = next ? `Up next: ${next.title}` : "The queue is empty. Search for a mix to continue.";
}

async function runAction(fields: Record<string, string>, env: Env): Promise<{ state: DJState; message: string }> {
  const state = await loadState(env);
  const action = clean(fields.action, 40).toLocaleLowerCase();
  let message = "done";
  if (action === "request" || action === "mix") {
    const request = clean(fields.q || state.request);
    const tracks = await buildMix(request, state, env, 24);
    if (!tracks.length) throw new Error("YouTube Music returned no playable songs");
    // Keep a useful refill seed even when the user pressed Make a mix with an
    // empty box. The first build still uses the whole taste profile; subsequent
    // refills use its strongest artist or a safe generic fallback.
    state.request = request || seedQueries("", state)[0];
    state.now = tracks.shift() || null;
    state.queue = tracks.slice(0, MAX_QUEUE);
    state.position = 0;
    state.duration = state.now?.duration || 0;
    state.paused = true;
    state.message = `Ready with ${state.queue.length + (state.now ? 1 : 0)} tracks. Press play.`;
    message = "mix ready";
  } else if (action === "play" || action === "resume" || action === "playpause") {
    if (!state.now) await advance(state, env, false);
    state.paused = action === "playpause" ? !state.paused : false;
    message = state.paused ? "paused" : "playing";
  } else if (action === "pause") {
    state.paused = true;
    message = "paused";
  } else if (action === "next" || action === "skip" || action === "ended") {
    await advance(state, env, action !== "ended");
    message = state.now ? `playing ${state.now.title}` : "the queue is empty";
  } else if (action === "play_row") {
    const track = trackFromFields(fields);
    const id = clean(fields.id, 32);
    const found = state.queue.find((row) => row.id === id) || track;
    if (!found) throw new Error("that track is no longer available");
    state.queue = state.queue.filter((row) => row.id !== found.id);
    state.now = found;
    state.position = 0;
    state.duration = found.duration;
    state.paused = false;
    state.message = `playing ${found.title}`;
    message = state.message;
  } else if (action === "queue_next" || action === "enqueue") {
    const track = trackFromFields(fields);
    if (!track) throw new Error("a valid track is required");
    state.queue = [track, ...state.queue.filter((row) => row.id !== track.id)].slice(0, MAX_QUEUE);
    message = `queued ${track.title}`;
  } else if (action === "like") {
    if (!state.now) throw new Error("nothing is playing");
    const alreadyLiked = state.liked.some((row) => row.id === state.now?.id);
    if (!alreadyLiked) {
      state.liked.push(clone(state.now));
      artistBump(state, state.now.artist, 2.4);
    }
    state.liked = state.liked.slice(-MAX_LIKES);
    message = alreadyLiked ? `already loved ${state.now.title}` : `loved ${state.now.title}`;
  } else if (action === "unlike") {
    const id = clean(fields.id, 32) || state.now?.id;
    const removed = state.liked.find((row) => row.id === id);
    state.liked = state.liked.filter((row) => row.id !== id);
    if (removed) artistBump(state, removed.artist, -2.4);
    message = removed ? "love removed" : "that song was not loved";
  } else if (action === "remove") {
    const id = clean(fields.id, 32);
    state.queue = state.queue.filter((row) => row.id !== id);
    message = "removed from queue";
  } else if (action === "progress") {
    const position = Number(fields.position);
    const duration = Number(fields.duration);
    if (Number.isFinite(position)) state.position = Math.max(0, Math.min(86400, position));
    if (Number.isFinite(duration) && duration > 0) state.duration = Math.min(86400, duration);
    message = "progress saved";
  } else if (action === "shuffle") {
    state.shuffle = !state.shuffle;
    if (state.shuffle) state.queue.sort(() => Math.random() - 0.5);
    message = state.shuffle ? "shuffle on" : "shuffle off";
  } else if (action === "repeat") {
    state.repeat = fields.mode === "off" || fields.mode === "all" || fields.mode === "one"
      ? fields.mode
      : state.repeat === "off" ? "all" : state.repeat === "all" ? "one" : "off";
    message = `repeat ${state.repeat}`;
  } else if (action === "clear_queue") {
    state.queue = [];
    message = "queue cleared";
  } else {
    throw new Error(`unknown action ${action || ""}`);
  }
  // Progress is live UI telemetry. Persisting it to D1 once or twice per second
  // would turn a harmless seek bar into a steady database write stream. The next
  // durable action saves the rest of the profile and queue; a reload simply starts
  // the current video from zero.
  if (action === "progress") {
    if (!env.DB) memoryState = clone(state);
  } else {
    await saveState(env, state);
  }
  return { state: publicState(state), message };
}

function parseCookies(request: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of (request.headers.get("cookie") || "").split(";")) {
    const [key, ...rest] = item.trim().split("=");
    if (!key) continue;
    try {
      out[key] = decodeURIComponent(rest.join("=") || "");
    } catch {
      out[key] = "";
    }
  }
  return out;
}

async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function safeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

async function sessionToken(env: Env): Promise<string> {
  return digest(`spotube-dj:${env.APP_PASSWORD || ""}:${env.SESSION_SECRET || env.APP_PASSWORD || ""}`);
}

async function isAuthorized(request: Request, env: Env): Promise<boolean> {
  if (!env.APP_PASSWORD) return true;
  const token = parseCookies(request)[AUTH_COOKIE] || "";
  return safeEqual(token, await sessionToken(env));
}

function loginCookie(request: Request, token: string): string {
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `${AUTH_COOKIE}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax${secure}; Max-Age=2592000`;
}

function logoutCookie(request: Request): string {
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `${AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax${secure}; Max-Age=0`;
}

function unauthorized(): Response {
  return json({ ok: false, error: "authentication required" }, 401, {
    "www-authenticate": "Cookie",
  });
}

function assetHeaders(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set("x-content-type-options", "nosniff");
  headers.set("referrer-policy", "no-referrer");
  headers.set("x-frame-options", "DENY");
  headers.set(
    "content-security-policy",
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; " +
      "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://www.youtube.com; " +
      "img-src 'self' data: https://i.ytimg.com https://*.googleusercontent.com https://*.ggpht.com; " +
      "frame-src https://www.youtube.com https://www.youtube-nocookie.com; connect-src 'self' https://www.youtube.com",
  );
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

async function fetchAsset(env: Env, request: Request): Promise<Response> {
  if (!env.ASSETS) return new Response("Worker assets binding is missing", { status: 500 });
  return assetHeaders(await env.ASSETS.fetch(request));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/healthz" && request.method === "GET") {
      return json({ ok: true, service: "spotube-dj-worker" });
    }
    if (path === "/api/auth" && request.method === "GET") {
      return json({ required: Boolean(env.APP_PASSWORD), authenticated: await isAuthorized(request, env) });
    }
    if (path === "/api/login" && request.method === "POST") {
      if (!env.APP_PASSWORD) return json({ ok: true, authenticated: true });
      const fields = await bodyFields(request);
      const supplied = clean(fields.password, 500);
      const suppliedHash = await digest(supplied);
      const expectedHash = await digest(env.APP_PASSWORD);
      if (!safeEqual(suppliedHash, expectedHash)) return errorResponse("incorrect password", 401);
      return json({ ok: true, authenticated: true }, 200, {
        "set-cookie": loginCookie(request, await sessionToken(env)),
      });
    }
    if (path === "/api/logout" && request.method === "POST") {
      return json({ ok: true }, 200, { "set-cookie": logoutCookie(request) });
    }

    if (path.startsWith("/api/")) {
      if (!(await isAuthorized(request, env))) return unauthorized();
      if (path === "/api/state" && request.method === "GET") {
        return json(publicState(await loadState(env)));
      }
      if (path === "/api/search" && (request.method === "GET" || request.method === "POST")) {
        const fields = request.method === "GET" ? fieldsFromUrl(url) : await bodyFields(request);
        const query = clean(fields.q, 240);
        if (!query) return errorResponse("q is required");
        try {
          const rows = (await ytmSearch(query, 16)).filter((row) => !row.reason).slice(0, 16);
          return json({ ok: true, rows });
        } catch (error) {
          return errorResponse(error instanceof Error ? error.message : "search failed", 502);
        }
      }
      if (path === "/api/action" && request.method === "POST") {
        try {
          const result = await runAction(await bodyFields(request), env);
          return json({ ok: true, message: result.message, state: result.state });
        } catch (error) {
          return errorResponse(error instanceof Error ? error.message : "action failed", 502);
        }
      }
      return errorResponse("route not found", 404);
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return errorResponse("method not allowed", 405);
    }
    return fetchAsset(env, request);
  },
};
