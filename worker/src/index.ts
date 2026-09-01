/**
 * Edge runtime for Spotube DJ.
 *
 * The desktop Python process and this Worker intentionally share the product
 * rules, not a process. Workers cannot run mpv, yt-dlp, subprocesses, Unix
 * sockets, or background Python threads. This runtime therefore keeps metadata,
 * taste, rooms, and control state at the edge while every browser plays its own
 * official YouTube embedded player.
 */

interface Env {
  DB?: D1Database;
  ASSETS?: Fetcher;
  ROOMS?: DurableObjectNamespace;
  GEMINI_API_KEY?: string;
  GEMINI_MODEL?: string;
  GEMINI_TTS_MODEL?: string;
  GEMINI_TTS_VOICE?: string;
  LLM_BASE_URL?: string;
  LLM_API_KEY?: string;
  LLM_MODEL?: string;
  TTS_BASE_URL?: string;
  TTS_API_KEY?: string;
  TTS_MODEL?: string;
  TTS_VOICE?: string;
}

type RepeatMode = "off" | "all" | "one";
type TtsProvider = "gemini" | "openai" | "browser" | "off";

type Track = {
  id: string;
  title: string;
  artist: string;
  channel: string;
  duration: number;
  thumbnail: string;
  url: string;
  album?: string;
  query?: string;
  official?: boolean;
  score?: number;
};

type SkippedTrack = { id: string; artist: string; title: string; at: number };

type ProfileState = {
  request: string;
  now: Track | null;
  queue: Track[];
  history: Track[];
  liked: Track[];
  skipped: SkippedTrack[];
  artists: Record<string, number>;
  genres: Record<string, number>;
  repeat: RepeatMode;
  shuffle: boolean;
  autoplay: boolean;
  paused: boolean;
  position: number;
  duration: number;
  message: string;
  vibe: string;
  why: string;
  engine: string;
  voiceEnabled: boolean;
  ttsProvider: TtsProvider;
  ttsVoice: string;
  djLang: string;
  updatedAt: number;
};

type User = { id: string; username: string; displayName: string };
type Plan = { queries: string[]; vibe: string; why: string; engine: string };
type SearchResult = Track & { reason?: string };

type RoomMember = { id: string; name: string; joinedAt: number };

type RoomState = {
  id: string;
  title: string;
  code?: string;
  hostId: string;
  hostName: string;
  members: Record<string, RoomMember>;
  request: string;
  now: Track | null;
  queue: Track[];
  history: Track[];
  playing: boolean;
  position: number;
  duration: number;
  repeat: RepeatMode;
  shuffle: boolean;
  vibe: string;
  why: string;
  engine: string;
  message: string;
  updatedAt: number;
};

const YTM_ENDPOINT = "https://music.youtube.com/youtubei/v1/search";
const YTM_CLIENT = {
  clientName: "WEB_REMIX",
  clientVersion: "1.20250101.00.00",
  gl: "US",
  hl: "en",
};
const SESSION_COOKIE = "spotube_session";
const MAX_QUEUE = 40;
const MAX_LIKES = 300;
const MAX_SKIPS = 300;
const MAX_ROOMS = 20;
const GEMINI_VOICES = [
  "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
  "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
  "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
  "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
  "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
];
const LANGUAGES = ["English", "Arabic", "Indonesian"];

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function clean(value: unknown, max = 240): string {
  return String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, max);
}

function norm(value: unknown): string {
  return clean(value).toLocaleLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
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

function database(env: Env): D1Database {
  if (!env.DB) throw new Error("D1 binding DB is missing");
  return env.DB;
}

function randomHex(length = 16): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function randomCode(length = 6): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return [...bytes].map((byte) => alphabet[byte % alphabet.length]).join("");
}

function bytesToHex(bytes: Uint8Array): string {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function hexToBytes(value: string): Uint8Array {
  const output = new Uint8Array(Math.floor(value.length / 2));
  for (let index = 0; index < output.length; index += 1) {
    output[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16) || 0;
  }
  return output;
}

async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return bytesToHex(new Uint8Array(bytes));
}

async function passwordHash(password: string, salt: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    { name: "PBKDF2" },
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: hexToBytes(salt).buffer as ArrayBuffer, iterations: 100000, hash: "SHA-256" },
    key,
    256,
  );
  return bytesToHex(new Uint8Array(bits));
}

function safeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function parseCookies(request: Request): Record<string, string> {
  const output: Record<string, string> = {};
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (!key) continue;
    try {
      output[key] = decodeURIComponent(rest.join("=") || "");
    } catch {
      output[key] = "";
    }
  }
  return output;
}

function sessionCookie(request: Request, token: string): string {
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `${SESSION_COOKIE}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000${secure}`;
}

function clearSessionCookie(request: Request): string {
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0${secure}`;
}

async function currentUser(request: Request, env: Env): Promise<User | null> {
  const token = parseCookies(request)[SESSION_COOKIE] || "";
  if (!token) return null;
  const row = await database(env)
    .prepare(
      "SELECT u.id, u.username, u.display_name AS displayName " +
        "FROM sessions s JOIN users u ON u.id = s.user_id " +
        "WHERE s.token_hash = ?1 AND s.expires_at > ?2",
    )
    .bind(await digest(token), Date.now())
    .first<User>();
  return row || null;
}

async function createSession(request: Request, env: Env, user: User): Promise<Response> {
  const token = randomHex(32);
  await database(env)
    .prepare("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?1, ?2, ?3)")
    .bind(await digest(token), user.id, Date.now() + 30 * 24 * 60 * 60 * 1000)
    .run();
  return json({ ok: true, user }, 200, { "set-cookie": sessionCookie(request, token) });
}

async function signup(request: Request, env: Env): Promise<Response> {
  const fields = await bodyFields(request);
  const username = clean(fields.username, 32).toLocaleLowerCase();
  const displayName = clean(fields.displayName || username, 60);
  const password = String(fields.password || "");
  if (!/^[a-z0-9_.-]{3,32}$/.test(username)) {
    return errorResponse("username must be 3-32 letters, numbers, dot, dash, or underscore");
  }
  if (password.length < 8 || password.length > 200) return errorResponse("password must be 8-200 characters");
  const id = crypto.randomUUID();
  const salt = randomHex(16);
  try {
    await database(env).batch([
      database(env)
        .prepare("INSERT INTO users(id, username, display_name, password_hash, password_salt, created_at) VALUES(?1, ?2, ?3, ?4, ?5, ?6)")
        .bind(id, username, displayName || username, await passwordHash(password, salt), salt, Date.now()),
      database(env)
        .prepare("INSERT INTO profiles(user_id, state_json, backup_json, updated_at) VALUES(?1, ?2, ?3, ?4)")
        .bind(id, JSON.stringify(defaultProfile()), "", Date.now()),
    ]);
  } catch (error) {
    const text = String(error || "");
    const lower = text.toLocaleLowerCase();
    console.error("signup failed", error);
    if (lower.includes("unique")) return errorResponse("that username is already taken", 409);
    if (lower.includes("no such table") || lower.includes("no such column") || lower.includes("d1 binding db is missing")) {
      return errorResponse("account storage is not initialized; apply D1 migrations 0001 and 0002, then redeploy", 503);
    }
    return errorResponse("could not create account; check the Worker logs", 503);
  }
  return createSession(request, env, { id, username, displayName: displayName || username });
}

async function login(request: Request, env: Env): Promise<Response> {
  const fields = await bodyFields(request);
  const username = clean(fields.username, 32).toLocaleLowerCase();
  const password = String(fields.password || "");
  const row = await database(env)
    .prepare("SELECT id, username, display_name AS displayName, password_hash AS passwordHash, password_salt AS passwordSalt FROM users WHERE username = ?1")
    .bind(username)
    .first<User & { passwordHash: string; passwordSalt: string }>();
  if (!row) return errorResponse("username or password is incorrect", 401);
  const suppliedHash = await passwordHash(password, row.passwordSalt);
  if (!safeEqual(suppliedHash, row.passwordHash)) return errorResponse("username or password is incorrect", 401);
  return createSession(request, env, { id: row.id, username: row.username, displayName: row.displayName });
}

function unauthorized(): Response {
  return json({ ok: false, error: "sign in required" }, 401, { "www-authenticate": "Cookie" });
}

async function requireUser(request: Request, env: Env): Promise<User | Response> {
  const user = await currentUser(request, env);
  return user || unauthorized();
}

function defaultProfile(): ProfileState {
  return {
    request: "",
    now: null,
    queue: [],
    history: [],
    liked: [],
    skipped: [],
    artists: {},
    genres: {},
    repeat: "off",
    shuffle: false,
    autoplay: true,
    paused: true,
    position: 0,
    duration: 0,
    message: "Tell the DJ what you want to hear.",
    vibe: "",
    why: "",
    engine: "offline parser",
    voiceEnabled: true,
    ttsProvider: "browser",
    ttsVoice: "Despina",
    djLang: "English",
    updatedAt: Date.now(),
  };
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
    album: clean(row.album, 160) || undefined,
    query: clean(row.query, 180) || undefined,
    official: Boolean(row.official),
    score: Number.isFinite(Number(row.score)) ? Number(row.score) : undefined,
  };
}

function normalizeProfile(value: unknown): ProfileState {
  const base = defaultProfile();
  if (!value || typeof value !== "object") return base;
  const raw = value as Record<string, unknown>;
  const queue = (Array.isArray(raw.queue) ? raw.queue : []).map(normalizeTrack).filter(Boolean) as Track[];
  const history = (Array.isArray(raw.history) ? raw.history : []).map(normalizeTrack).filter(Boolean) as Track[];
  const liked = (Array.isArray(raw.liked) ? raw.liked : []).map(normalizeTrack).filter(Boolean) as Track[];
  const skipped = (Array.isArray(raw.skipped) ? raw.skipped : [])
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
    .map((item) => ({ id: clean(item.id, 32), artist: clean(item.artist, 120), title: clean(item.title, 180), at: Number(item.at) || Date.now() }))
    .filter((item) => item.id)
    .slice(-MAX_SKIPS);
  const weights = (valueToRead: unknown): Record<string, number> => {
    const output: Record<string, number> = {};
    if (!valueToRead || typeof valueToRead !== "object") return output;
    for (const [key, rawWeight] of Object.entries(valueToRead as Record<string, unknown>)) {
      const weight = Number(rawWeight);
      if (norm(key) && Number.isFinite(weight)) output[norm(key)] = Math.max(-20, Math.min(20, weight));
    }
    return output;
  };
  const provider = raw.ttsProvider === "gemini" || raw.ttsProvider === "openai" || raw.ttsProvider === "browser" || raw.ttsProvider === "off"
    ? raw.ttsProvider : base.ttsProvider;
  const lang = LANGUAGES.find((item) => item.toLocaleLowerCase() === String(raw.djLang || "").toLocaleLowerCase()) || base.djLang;
  return {
    request: clean(raw.request, 240),
    now: normalizeTrack(raw.now),
    queue: queue.slice(0, MAX_QUEUE),
    history: history.slice(-40),
    liked: liked.slice(-MAX_LIKES),
    skipped,
    artists: weights(raw.artists),
    genres: weights(raw.genres),
    repeat: raw.repeat === "all" || raw.repeat === "one" ? raw.repeat : "off",
    shuffle: Boolean(raw.shuffle),
    autoplay: raw.autoplay !== false,
    paused: raw.paused !== false,
    position: Math.max(0, Math.min(86400, Number(raw.position) || 0)),
    duration: Math.max(0, Math.min(86400, Number(raw.duration) || 0)),
    message: clean(raw.message, 300) || base.message,
    vibe: clean(raw.vibe, 120),
    why: clean(raw.why, 300),
    engine: clean(raw.engine, 80) || base.engine,
    voiceEnabled: raw.voiceEnabled !== false,
    ttsProvider: provider,
    ttsVoice: clean(raw.ttsVoice, 80) || base.ttsVoice,
    djLang: lang,
    updatedAt: Number(raw.updatedAt) || Date.now(),
  };
}

async function loadProfile(env: Env, userId: string): Promise<ProfileState> {
  const row = await database(env).prepare("SELECT state_json FROM profiles WHERE user_id = ?1").bind(userId).first<{ state_json: string }>();
  if (!row?.state_json) {
    const profile = defaultProfile();
    await saveProfile(env, userId, profile);
    return profile;
  }
  try {
    return normalizeProfile(JSON.parse(row.state_json));
  } catch {
    return defaultProfile();
  }
}

async function saveProfile(env: Env, userId: string, profile: ProfileState, backup = ""): Promise<void> {
  profile.updatedAt = Date.now();
  await database(env)
    .prepare("INSERT INTO profiles(user_id, state_json, backup_json, updated_at) VALUES(?1, ?2, COALESCE((SELECT backup_json FROM profiles WHERE user_id = ?1), ?3), ?4) ON CONFLICT(user_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at")
    .bind(userId, JSON.stringify(normalizeProfile(profile)), backup, profile.updatedAt)
    .run();
}

function publicProfile(profile: ProfileState, user: User): Record<string, unknown> {
  const out = clone(profile) as ProfileState & Record<string, unknown>;
  out.queue = out.queue.slice(0, MAX_QUEUE);
  out.liked = out.liked.slice(-MAX_LIKES);
  out.skipped = out.skipped.slice(-MAX_SKIPS);
  out.user = user;
  out.likedIds = out.liked.map((track) => track.id);
  out.djLine = djLine(profile, profile.djLang);
  return out;
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
    if (key === "musicResponsiveListItemRenderer" && child && typeof child === "object") rows.push(child as Record<string, unknown>);
    else collectMusicRows(child, rows);
  }
}

function columns(row: Record<string, unknown>): string[] {
  return (Array.isArray(row.flexColumns) ? row.flexColumns : [])
    .map((column) => {
      if (!column || typeof column !== "object") return "";
      const renderer = (column as Record<string, unknown>).musicResponsiveListItemFlexColumnRenderer;
      return renderer && typeof renderer === "object" ? textOf((renderer as Record<string, unknown>).text) : "";
    })
    .map((value) => clean(value, 240))
    .filter(Boolean);
}

function durationOf(value: string): number {
  const parts = value.trim().split(":").map(Number);
  if (parts.some((part) => !Number.isFinite(part))) return 0;
  if (parts.length === 2 && parts[1] >= 0 && parts[1] < 60) return parts[0] * 60 + parts[1];
  if (parts.length === 3 && parts[1] >= 0 && parts[1] < 60 && parts[2] >= 0 && parts[2] < 60) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return 0;
}

function thumbnailOf(row: Record<string, unknown>, id: string): string {
  const found: Array<{ url: string; area: number }> = [];
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!value || typeof value !== "object") return;
    const object = value as Record<string, unknown>;
    if (typeof object.url === "string" && /^https?:\/\//.test(object.url)) {
      found.push({ url: object.url.split("?", 1)[0], area: Number(object.width || 0) * Number(object.height || 0) });
    }
    Object.values(object).forEach(visit);
  };
  visit(row.thumbnail);
  return found.sort((a, b) => b.area - a.area)[0]?.url || `https://i.ytimg.com/vi/${encodeURIComponent(id)}/hqdefault.jpg`;
}

function artistFrom(row: Record<string, unknown>, title: string, parts: string[], official: boolean): string {
  if (official) {
    const candidate = parts.find((part) => part.toLocaleLowerCase() !== "song" && !durationOf(part) && !/views?\b/i.test(part));
    if (candidate) return clean(candidate, 120);
  }
  const overlayNode = row.overlay;
  const overlay = overlayNode && typeof overlayNode === "object" ? (overlayNode as Record<string, unknown>).musicItemThumbnailOverlayRenderer : undefined;
  const contentNode = overlay && typeof overlay === "object" ? (overlay as Record<string, unknown>).content : undefined;
  const content = contentNode && typeof contentNode === "object" ? (contentNode as Record<string, unknown>).musicPlayButtonRenderer : undefined;
  for (const key of ["accessibilityPlayData", "accessibilityPauseData"]) {
    const object = content && typeof content === "object" ? content as Record<string, unknown> : undefined;
    const label = object ? ((object[key] as Record<string, unknown> | undefined)?.accessibilityData as Record<string, unknown> | undefined)?.label : undefined;
    if (typeof label !== "string") continue;
    const pieces = label.replace(/^\s*(?:Play|Pause)\s+/i, "").split(/\s[-–—]\s/);
    const candidate = pieces.length > 1 ? pieces[pieces.length - 1].replace(/\s+from\s+.*$/i, "") : "";
    if (candidate && norm(candidate) !== norm(title)) return clean(candidate, 120);
  }
  return "";
}

function rejected(title: string, duration: number, official: boolean): string | null {
  if (/\((?:[^)]*\b(?:live|unplugged|concert|performance|festival)\b[^)]*)\)|\[(?:[^\]]*\b(?:live|unplugged|concert|performance|festival)\b[^\]]*)\]/i.test(title) || /\b(?:live\s+(?:at|from|in|version|take|performance|concert)|recorded\s+live|mtv\s+unplugged|full\s+(?:concert|show|set))\b/i.test(title)) return "live performance";
  if (duration > 15 * 60 || /\b(?:dj\s+set|full\s+album|best\s+of|compilation|\d+\s*[- ]?hours?)\b/i.test(title)) return "long-form upload";
  if (!official && /\b(?:cover|karaoke|tribute|reaction|tutorial|review|lyrics?|remix|reverb|slowed|sped\s*up|nightcore)\b/i.test(title)) return "not an original music recording";
  if (/\b(?:unboxing|podcast|trailer|movie|film|fight scene|full match|gameplay|how to)\b/i.test(title)) return "not a song";
  return null;
}

async function ytmSearch(query: string, limit = 18): Promise<SearchResult[]> {
  const response = await fetch(YTM_ENDPOINT, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": "Mozilla/5.0 spotube-dj-worker" },
    body: JSON.stringify({ context: { client: YTM_CLIENT }, query }),
  });
  if (!response.ok) throw new Error(`YouTube Music returned HTTP ${response.status}`);
  const payload = (await response.json()) as unknown;
  const rows: Record<string, unknown>[] = [];
  collectMusicRows(payload, rows);
  const output: SearchResult[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    const data = row.playlistItemData as Record<string, unknown> | undefined;
    const id = clean(data?.videoId, 32);
    const fields = columns(row);
    if (!id || !fields.length || seen.has(id)) continue;
    seen.add(id);
    const title = fields[0];
    const parts = (fields[1] || "").split(/[•·|]/).map((part) => clean(part, 120)).filter(Boolean);
    const official = Boolean(parts[0] && /^song\b/i.test(parts[0]));
    const duration = parts.reduce((longest, part) => Math.max(longest, durationOf(part)), 0);
    const artist = artistFrom(row, title, parts, official);
    output.push({
      id,
      title,
      artist,
      channel: official ? artist : parts[0] || "",
      duration,
      thumbnail: thumbnailOf(row, id),
      url: `https://music.youtube.com/watch?v=${encodeURIComponent(id)}`,
      official,
      reason: rejected(title, duration, official) || undefined,
    });
    if (output.length >= limit * 3) break;
  }
  return output;
}

function fallbackPlan(request: string, profile: ProfileState): Plan {
  const artists = Object.entries(profile.artists).filter(([, weight]) => weight > 0).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([artist]) => `${artist} music`);
  const query = clean(request);
  const queries = query ? [query] : artists.length ? artists : ["indie pop music"];
  const words = norm(query || artists[0] || "music").split(" ").filter(Boolean);
  const mood = words.slice(0, 3).join(" ") || "eclectic favourites";
  return {
    queries,
    vibe: `${mood} ${new Date().toLocaleDateString("en", { weekday: "long" }).toLocaleLowerCase()}`,
    why: query ? `you asked for ${query}` : "it leans into the room's loved music",
    engine: "offline parser",
  };
}

function extractJson(text: string): unknown {
  const cleaned = text.replace(/```json|```/gi, "").trim();
  const start = Math.min(...[cleaned.indexOf("{"), cleaned.indexOf("[")].filter((index) => index >= 0));
  const end = Math.max(cleaned.lastIndexOf("}"), cleaned.lastIndexOf("]"));
  try {
    return JSON.parse(start >= 0 && end >= start ? cleaned.slice(start, end + 1) : cleaned);
  } catch {
    return null;
  }
}

function planFromPayload(value: unknown, fallback: Plan, engine: string): Plan {
  if (Array.isArray(value)) {
    const queries = value.filter((item): item is string => typeof item === "string").map((item) => clean(item, 180)).filter(Boolean).slice(0, 3);
    return queries.length ? { ...fallback, queries, engine } : fallback;
  }
  if (!value || typeof value !== "object") return fallback;
  const object = value as Record<string, unknown>;
  const queries = (Array.isArray(object.queries) ? object.queries : []).filter((item): item is string => typeof item === "string").map((item) => clean(item, 180)).filter(Boolean).slice(0, 3);
  return queries.length ? { queries, vibe: clean(object.vibe, 120) || fallback.vibe, why: clean(object.why, 300) || fallback.why, engine } : fallback;
}

async function geminiPlan(request: string, profile: ProfileState, env: Env, fallback: Plan): Promise<Plan> {
  if (!env.GEMINI_API_KEY) return fallback;
  const model = encodeURIComponent(env.GEMINI_MODEL || "gemini-2.5-flash");
  const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
    body: JSON.stringify({ contents: [{ parts: [{ text: [
      "You are the planning brain for a music DJ.",
      "Return only JSON: {queries: string[], vibe: string, why: string}.",
      "Use at most three concise YouTube Music searches. Never ask for live recordings, covers, remixes, podcasts, tutorials, or long mixes.",
      `Listener request: ${request || "make a mix from these preferences"}`,
      `Loved artists: ${Object.keys(profile.artists).slice(0, 8).join(", ") || "none yet"}`,
    ].join("\n") }] }], generationConfig: { temperature: 0.35 } }),
  });
  if (!response.ok) return fallback;
  const payload = (await response.json()) as Record<string, unknown>;
  const candidates = payload.candidates as Array<Record<string, unknown>> | undefined;
  const content = candidates?.[0]?.content as Record<string, unknown> | undefined;
  const parts = content?.parts as Array<Record<string, unknown>> | undefined;
  const text = parts?.map((part) => String(part.text || "")).join("") || "";
  return planFromPayload(extractJson(text), fallback, "Gemini");
}

function openAiEndpoint(base: string, path: string): string {
  const cleanBase = base.replace(/\/$/, "");
  if (cleanBase.endsWith(path)) return cleanBase;
  if (cleanBase.endsWith("/v1")) return `${cleanBase}${path}`;
  return `${cleanBase}/v1${path}`;
}

async function compatiblePlan(request: string, profile: ProfileState, env: Env, fallback: Plan): Promise<Plan> {
  if (!env.LLM_BASE_URL) return fallback;
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (env.LLM_API_KEY) headers.authorization = `Bearer ${env.LLM_API_KEY}`;
  const response = await fetch(openAiEndpoint(env.LLM_BASE_URL, "/chat/completions"), {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: env.LLM_MODEL || "llama3.2",
      temperature: 0.35,
      messages: [{ role: "system", content: "Return only JSON with queries, vibe, and why for a music DJ. No live, cover, remix, podcast, tutorial, or long-form results." }, { role: "user", content: `Request: ${request || "a mix from my likes"}. Loved artists: ${Object.keys(profile.artists).slice(0, 8).join(", ")}` }],
    }),
  });
  if (!response.ok) return fallback;
  const payload = (await response.json()) as Record<string, unknown>;
  const choices = payload.choices as Array<Record<string, unknown>> | undefined;
  const message = choices?.[0]?.message as Record<string, unknown> | undefined;
  return planFromPayload(extractJson(String(message?.content || "")), fallback, "OpenAI-compatible");
}

async function makePlan(request: string, profile: ProfileState, env: Env): Promise<Plan> {
  const fallback = fallbackPlan(request, profile);
  try {
    if (env.GEMINI_API_KEY) return await geminiPlan(request, profile, env, fallback);
    if (env.LLM_BASE_URL) return await compatiblePlan(request, profile, env, fallback);
  } catch (error) {
    console.error("planner failed", error);
  }
  return fallback;
}

function scoreTrack(track: Track, query: string, profile: ProfileState): number {
  const terms = norm(query).split(" ").filter((term) => term.length > 2);
  const haystack = `${norm(track.title)} ${norm(track.artist)} ${norm(track.channel)}`;
  let score = track.official ? 3 : 0;
  score += terms.reduce((sum, term) => sum + (haystack.includes(term) ? 1.5 : 0), 0);
  score += profile.artists[norm(track.artist)] || 0;
  if (profile.liked.some((liked) => liked.id === track.id)) score += 4;
  if (profile.skipped.some((skipped) => skipped.id === track.id)) score -= 8;
  return score;
}

async function buildMix(request: string, profile: ProfileState, env: Env, limit = 24): Promise<{ tracks: Track[]; plan: Plan }> {
  const plan = await makePlan(clean(request), profile, env);
  const pages = await Promise.all(plan.queries.slice(0, 3).map((query) => ytmSearch(query, Math.max(10, limit))));
  const unique = new Map<string, Track>();
  for (let index = 0; index < pages.length; index += 1) {
    for (const result of pages[index]) {
      if (result.reason) continue;
      const track = normalizeTrack({ ...result, query: plan.queries[index] });
      if (!track) continue;
      track.score = scoreTrack(track, clean(request) || plan.queries[index], profile);
      const old = unique.get(track.id);
      if (!old || (track.score || 0) > (old.score || 0)) unique.set(track.id, track);
    }
  }
  let tracks = [...unique.values()].sort((a, b) => (b.score || 0) - (a.score || 0));
  if (profile.shuffle) tracks = tracks.sort(() => Math.random() - 0.5);
  return { tracks: tracks.slice(0, Math.max(1, Math.min(limit, MAX_QUEUE))), plan };
}

function blendProfiles(profiles: ProfileState[]): ProfileState {
  const blended = defaultProfile();
  const artistTotals: Record<string, number> = {};
  const genreTotals: Record<string, number> = {};
  const liked = new Map<string, Track>();
  const skipped = new Map<string, SkippedTrack>();
  for (const profile of profiles) {
    Object.entries(profile.artists).forEach(([name, weight]) => { artistTotals[name] = (artistTotals[name] || 0) + weight; });
    Object.entries(profile.genres).forEach(([name, weight]) => { genreTotals[name] = (genreTotals[name] || 0) + weight; });
    profile.liked.forEach((track) => liked.set(track.id, track));
    profile.skipped.forEach((track) => skipped.set(track.id, track));
  }
  blended.artists = Object.fromEntries(Object.entries(artistTotals).map(([key, value]) => [key, value / Math.max(1, profiles.length)]));
  blended.genres = Object.fromEntries(Object.entries(genreTotals).map(([key, value]) => [key, value / Math.max(1, profiles.length)]));
  blended.liked = [...liked.values()].slice(-MAX_LIKES);
  blended.skipped = [...skipped.values()].slice(-MAX_SKIPS);
  blended.shuffle = profiles.some((profile) => profile.shuffle);
  return blended;
}

function addLike(profile: ProfileState, track: Track): void {
  if (!profile.liked.some((item) => item.id === track.id)) {
    profile.liked.push(clone(track));
    profile.liked = profile.liked.slice(-MAX_LIKES);
    const artist = norm(track.artist);
    if (artist) profile.artists[artist] = Math.min(20, (profile.artists[artist] || 0) + 2.4);
  }
}

function addSkip(profile: ProfileState, track: Track): void {
  profile.skipped.push({ id: track.id, artist: track.artist, title: track.title, at: Date.now() });
  profile.skipped = profile.skipped.slice(-MAX_SKIPS);
  const artist = norm(track.artist);
  if (artist) profile.artists[artist] = Math.max(-20, (profile.artists[artist] || 0) - 2.4);
}

function addHeard(profile: ProfileState): void {
  if (!profile.now || !profile.duration || profile.position / profile.duration < 0.72) return;
  addLike(profile, profile.now);
}

async function advanceProfile(profile: ProfileState, env: Env, markSkip: boolean): Promise<void> {
  const current = profile.now;
  if (current && markSkip) addSkip(profile, current);
  if (current && profile.repeat === "one") {
    profile.position = 0;
    profile.paused = false;
    profile.message = `Repeating ${current.title}`;
    return;
  }
  if (current) {
    profile.history.push(clone(current));
    profile.history = profile.history.slice(-40);
  }
  if (current && profile.repeat === "all") profile.queue.push(current);
  let next = profile.queue.shift() || null;
  if (!next && profile.autoplay && profile.request) {
    const built = await buildMix(profile.request, profile, env, 16);
    next = built.tracks.shift() || null;
    profile.queue.push(...built.tracks);
    profile.vibe = built.plan.vibe;
    profile.why = built.plan.why;
    profile.engine = built.plan.engine;
  }
  profile.now = next;
  profile.position = 0;
  profile.duration = next?.duration || 0;
  profile.paused = !next;
  profile.message = next ? `Up next: ${next.title}` : "The queue is empty. Make another mix to continue.";
}

function djLine(state: ProfileState | RoomState, language = "English"): string {
  const now = state.now;
  if (!now) return "";
  const next = state.queue[0];
  const why = state.why || (state.request ? `you asked for ${state.request}` : "it fits the blended taste");
  const vibe = state.vibe ? ` It is part of the ${state.vibe} set.` : "";
  const upcoming = next ? ` Up next is ${next.artist || next.channel}, ${next.title}.` : "";
  if (language.toLocaleLowerCase() === "indonesian") {
    return `Baiklah, ini ${now.artist || now.channel}, ${now.title}. ${why}.${vibe.replace("It is", "Ini")}${upcoming.replace("Up next is", "Selanjutnya")}`.replace(/\.\./g, ".");
  }
  return `Coming up, ${now.artist || now.channel}, ${now.title}. ${why}.${vibe}${upcoming}`.replace(/\.\./g, ".");
}

async function runPersonalAction(user: User, fields: Record<string, string>, env: Env): Promise<{ state: Record<string, unknown>; message: string }> {
  const profile = await loadProfile(env, user.id);
  const action = clean(fields.action, 50).toLocaleLowerCase();
  let message = "done";
  if (action === "request" || action === "mix" || action === "radio") {
    const request = clean(fields.q || profile.request || (action === "radio" ? `more like ${fields.title || "this song"} ${fields.artist || ""}` : ""));
    const built = await buildMix(request, profile, env, 24);
    if (!built.tracks.length) throw new Error("YouTube Music returned no playable songs");
    profile.request = request;
    const nextMixTrack = built.tracks.shift() || null;
    if (profile.now && nextMixTrack && profile.now.id !== nextMixTrack.id) {
      profile.history.push(clone(profile.now));
      profile.history = profile.history.slice(-40);
    }
    profile.now = nextMixTrack;
    profile.queue = built.tracks.slice(0, MAX_QUEUE);
    profile.position = 0;
    profile.duration = profile.now?.duration || 0;
    profile.paused = true;
    profile.vibe = built.plan.vibe;
    profile.why = built.plan.why;
    profile.engine = built.plan.engine;
    profile.message = `Ready with ${profile.queue.length + (profile.now ? 1 : 0)} tracks. Press play.`;
    message = "mix ready";
  } else if (action === "play" || action === "resume" || action === "playpause") {
    if (!profile.now) await advanceProfile(profile, env, false);
    profile.paused = action === "playpause" ? !profile.paused : false;
    message = profile.paused ? "paused" : "playing";
  } else if (action === "pause") {
    profile.paused = true;
    message = "paused";
  } else if (action === "prev") {
    if (profile.now && profile.position > 5) {
      profile.position = 0;
      message = `restarted ${profile.now.title}`;
    } else {
      const previous = profile.history.pop();
      if (!previous) {
        profile.position = 0;
        message = profile.now ? `restarted ${profile.now.title}` : "there is no previous song";
      } else {
        if (profile.now) profile.queue.unshift(profile.now);
        profile.now = previous;
        profile.position = 0;
        profile.duration = previous.duration;
        profile.paused = false;
        profile.message = `playing ${previous.title}`;
        message = profile.message;
      }
    }
  } else if (action === "next" || action === "skip" || action === "ended") {
    if (action === "ended") addHeard(profile);
    await advanceProfile(profile, env, action !== "ended");
    message = profile.now ? `playing ${profile.now.title}` : "the queue is empty";
  } else if (action === "play_row") {
    const supplied = fields.track ? normalizeTrack(JSON.parse(fields.track)) : null;
    const id = clean(fields.id, 32);
    const selected = profile.queue.find((track) => track.id === id) || supplied;
    if (!selected) throw new Error("that track is no longer available");
    profile.queue = profile.queue.filter((track) => track.id !== selected.id);
    if (profile.now && profile.now.id !== selected.id) {
      profile.history.push(clone(profile.now));
      profile.history = profile.history.slice(-40);
    }
    profile.now = selected;
    profile.position = 0;
    profile.duration = selected.duration;
    profile.paused = false;
    profile.message = `playing ${selected.title}`;
    message = profile.message;
  } else if (action === "enqueue" || action === "queue_next") {
    const track = fields.track ? normalizeTrack(JSON.parse(fields.track)) : null;
    if (!track) throw new Error("a valid track is required");
    profile.queue = [track, ...profile.queue.filter((item) => item.id !== track.id)].slice(0, MAX_QUEUE);
    message = `queued ${track.title}`;
  } else if (action === "like") {
    if (!profile.now) throw new Error("nothing is playing");
    const existed = profile.liked.some((track) => track.id === profile.now?.id);
    addLike(profile, profile.now);
    message = existed ? `already loved ${profile.now.title}` : `loved ${profile.now.title}`;
  } else if (action === "unlike") {
    const id = clean(fields.id, 32) || profile.now?.id;
    const removed = profile.liked.find((track) => track.id === id);
    profile.liked = profile.liked.filter((track) => track.id !== id);
    if (removed && profile.artists[norm(removed.artist)]) profile.artists[norm(removed.artist)] -= 2.4;
    message = removed ? "love removed" : "that song was not loved";
  } else if (action === "dislike") {
    const track = profile.queue.find((item) => item.id === clean(fields.id, 32));
    if (track) {
      addSkip(profile, track);
      profile.queue = profile.queue.filter((item) => item.id !== track.id);
      message = `noted: ${track.title} is not for you`;
    } else message = "that row is no longer queued";
  } else if (action === "remove") {
    profile.queue = profile.queue.filter((track) => track.id !== clean(fields.id, 32));
    message = "removed from queue";
  } else if (action === "progress") {
    const position = Number(fields.position);
    const duration = Number(fields.duration);
    if (Number.isFinite(position)) profile.position = Math.max(0, Math.min(86400, position));
    if (Number.isFinite(duration) && duration > 0) profile.duration = Math.min(86400, duration);
    message = "progress saved";
  } else if (action === "shuffle") {
    profile.shuffle = !profile.shuffle;
    if (profile.shuffle) profile.queue.sort(() => Math.random() - 0.5);
    message = profile.shuffle ? "shuffle on" : "shuffle off";
  } else if (action === "repeat") {
    profile.repeat = fields.mode === "off" || fields.mode === "all" || fields.mode === "one" ? fields.mode : profile.repeat === "off" ? "all" : profile.repeat === "all" ? "one" : "off";
    message = `repeat ${profile.repeat}`;
  } else if (action === "autoplay") {
    profile.autoplay = fields.on !== "off" && fields.on !== "0" && fields.on !== "false";
    message = profile.autoplay ? "keep mixing on" : "keep mixing off";
  } else if (action === "clear_queue") {
    profile.queue = [];
    message = "queue cleared";
  } else if (action === "clear_taste") {
    const row = await database(env).prepare("SELECT state_json FROM profiles WHERE user_id = ?1").bind(user.id).first<{ state_json: string }>();
    await database(env).prepare("UPDATE profiles SET backup_json = ?1 WHERE user_id = ?2").bind(row?.state_json || JSON.stringify(profile), user.id).run();
    profile.liked = [];
    profile.skipped = [];
    profile.artists = {};
    profile.genres = {};
    message = "taste profile cleared; restore is available";
  } else if (action === "restore_taste") {
    const row = await database(env).prepare("SELECT backup_json FROM profiles WHERE user_id = ?1").bind(user.id).first<{ backup_json: string }>();
    if (row?.backup_json) {
      const restored = normalizeProfile(JSON.parse(row.backup_json));
      profile.liked = restored.liked;
      profile.skipped = restored.skipped;
      profile.artists = restored.artists;
      profile.genres = restored.genres;
      message = "taste profile restored";
    } else message = "there is no taste backup to restore";
  } else if (action === "forget_artist") {
    const artist = norm(fields.name);
    delete profile.artists[artist];
    message = artist ? `${artist} will not pull the mix anymore` : "which artist should be forgotten?";
  } else {
    throw new Error(`unknown action ${action || ""}`);
  }
  if (action === "progress") {
    // Progress is transient telemetry. Avoid turning a seek bar into a continuous
    // D1 write stream; the next durable action persists the queue and taste.
  } else {
    await saveProfile(env, user.id, profile);
  }
  return { state: publicProfile(profile, user), message };
}

function normalizeRoom(value: unknown, id = "room"): RoomState {
  const base: RoomState = {
    id,
    title: "Listen party",
    hostId: "",
    hostName: "",
    members: {},
    request: "",
    now: null,
    queue: [],
    history: [],
    playing: false,
    position: 0,
    duration: 0,
    repeat: "off",
    shuffle: false,
    vibe: "",
    why: "",
    engine: "offline parser",
    message: "Create a mix for the room.",
    updatedAt: Date.now(),
  };
  if (!value || typeof value !== "object") return base;
  const raw = value as Record<string, unknown>;
  const members: Record<string, RoomMember> = {};
  if (raw.members && typeof raw.members === "object") {
    for (const [memberId, item] of Object.entries(raw.members as Record<string, unknown>)) {
      if (!item || typeof item !== "object") continue;
      const row = item as Record<string, unknown>;
      members[memberId] = { id: memberId, name: clean(row.name, 60), joinedAt: Number(row.joinedAt) || Date.now() };
    }
  }
  return {
    ...base,
    id: clean(raw.id, 80) || id,
    title: clean(raw.title, 100) || base.title,
    hostId: clean(raw.hostId, 80),
    hostName: clean(raw.hostName, 60),
    members,
    request: clean(raw.request, 240),
    now: normalizeTrack(raw.now),
    queue: (Array.isArray(raw.queue) ? raw.queue : []).map(normalizeTrack).filter(Boolean).slice(0, MAX_QUEUE) as Track[],
    history: (Array.isArray(raw.history) ? raw.history : []).map(normalizeTrack).filter(Boolean).slice(-40) as Track[],
    playing: Boolean(raw.playing),
    position: Math.max(0, Math.min(86400, Number(raw.position) || 0)),
    duration: Math.max(0, Math.min(86400, Number(raw.duration) || 0)),
    repeat: raw.repeat === "all" || raw.repeat === "one" ? raw.repeat : "off",
    shuffle: Boolean(raw.shuffle),
    vibe: clean(raw.vibe, 120),
    why: clean(raw.why, 300),
    engine: clean(raw.engine, 80) || base.engine,
    message: clean(raw.message, 300) || base.message,
    updatedAt: Number(raw.updatedAt) || Date.now(),
  };
}

function publicRoom(room: RoomState, language = "English"): Record<string, unknown> {
  const output = clone(room) as RoomState & Record<string, unknown>;
  output.queue = output.queue.slice(0, MAX_QUEUE);
  output.members = Object.values(room.members) as unknown as Record<string, RoomMember>;
  output.playing = room.playing;
  output.djLine = djLine(room, language);
  return output;
}

function roomBinding(env: Env, id: string): DurableObjectStub {
  if (!env.ROOMS) throw new Error("Durable Object binding ROOMS is missing");
  return env.ROOMS.get(env.ROOMS.idFromName(id));
}

async function roomFetch(env: Env, roomId: string, path: string, user: User, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers || {});
  headers.set("x-spotube-user-id", user.id);
  headers.set("x-spotube-user-name", user.displayName);
  return roomBinding(env, roomId).fetch(new Request(`https://room.internal${path}`, { ...init, headers }));
}

async function roomMembers(env: Env, roomId: string): Promise<string[]> {
  const response = await roomBinding(env, roomId).fetch("https://room.internal/members");
  if (!response.ok) return [];
  const value = (await response.json()) as { members?: string[] };
  return value.members || [];
}

async function isRoomMember(env: Env, roomId: string, userId: string): Promise<boolean> {
  const row = await database(env).prepare("SELECT 1 AS ok FROM room_members WHERE room_id = ?1 AND user_id = ?2").bind(roomId, userId).first<{ ok: number }>();
  return Boolean(row?.ok);
}

async function createRoom(request: Request, env: Env, user: User): Promise<Response> {
  const fields = await bodyFields(request);
  const title = clean(fields.title || `${user.displayName}'s listen party`, 100);
  let code = "";
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const candidate = randomCode();
    const exists = await database(env).prepare("SELECT 1 AS ok FROM rooms WHERE code = ?1").bind(candidate).first<{ ok: number }>();
    if (!exists) { code = candidate; break; }
  }
  if (!code) return errorResponse("could not create an invite code", 503);
  const id = crypto.randomUUID();
  await database(env).batch([
    database(env).prepare("INSERT INTO rooms(id, code, title, host_user_id, created_at, updated_at) VALUES(?1, ?2, ?3, ?4, ?5, ?6)").bind(id, code, title, user.id, Date.now(), Date.now()),
    database(env).prepare("INSERT INTO room_members(room_id, user_id, joined_at) VALUES(?1, ?2, ?3)").bind(id, user.id, Date.now()),
  ]);
  await roomFetch(env, id, "/init", user, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id, title, code, hostId: user.id, hostName: user.displayName }) });
  const stateResponse = await roomFetch(env, id, "/state", user);
  const state = await stateResponse.json();
  return json({ ok: true, room: { id, code, title, hostId: user.id }, state });
}

async function joinRoom(request: Request, env: Env, user: User): Promise<Response> {
  const fields = await bodyFields(request);
  const code = clean(fields.code, 12).toLocaleUpperCase();
  const room = await database(env).prepare("SELECT id, code, title, host_user_id AS hostId FROM rooms WHERE code = ?1").bind(code).first<{ id: string; code: string; title: string; hostId: string }>();
  if (!room) return errorResponse("invite code not found", 404);
  await database(env).prepare("INSERT OR IGNORE INTO room_members(room_id, user_id, joined_at) VALUES(?1, ?2, ?3)").bind(room.id, user.id, Date.now()).run();
  const response = await roomFetch(env, room.id, "/join", user, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id: user.id, name: user.displayName }) });
  if (!response.ok) return errorResponse("could not join room", 502);
  const stateResponse = await roomFetch(env, room.id, "/state", user);
  const state = await stateResponse.json() as Record<string, unknown>;
  const hostId = clean(state.hostId, 80);
  if (hostId && hostId !== room.hostId) await database(env).prepare("UPDATE rooms SET host_user_id = ?1, updated_at = ?2 WHERE id = ?3").bind(hostId, Date.now(), room.id).run();
  return json({ ok: true, room: { ...room, hostId: hostId || room.hostId }, state });
}

async function listRooms(env: Env, user: User): Promise<Record<string, unknown>[]> {
  const result = await database(env).prepare("SELECT r.id, r.code, r.title, r.host_user_id AS hostId, r.updated_at AS updatedAt FROM rooms r JOIN room_members m ON m.room_id = r.id WHERE m.user_id = ?1 ORDER BY r.updated_at DESC LIMIT ?2").bind(user.id, MAX_ROOMS).all<Record<string, unknown>>();
  return result.results || [];
}

async function roomAction(request: Request, env: Env, user: User, roomId: string, fields: Record<string, string>): Promise<Response> {
  if (!(await isRoomMember(env, roomId, user.id))) return errorResponse("join the room first", 403);
  const action = clean(fields.action, 50).toLocaleLowerCase();
  const outgoing = { ...fields };
  if (action === "request" || action === "mix" || action === "radio") {
    const memberIds = await roomMembers(env, roomId);
    const profiles = await Promise.all(memberIds.map((id) => loadProfile(env, id)));
    const blended = blendProfiles(profiles);
    const requestText = clean(fields.q || (action === "radio" ? `more like ${fields.title || "this song"} ${fields.artist || ""}` : ""));
    const built = await buildMix(requestText, blended, env, 24);
    if (!built.tracks.length) return errorResponse("YouTube Music returned no playable songs", 502);
    outgoing.q = requestText;
    outgoing.tracks = JSON.stringify(built.tracks);
    outgoing.vibe = built.plan.vibe;
    outgoing.why = built.plan.why;
    outgoing.engine = built.plan.engine;
  }
  if (action === "like" || action === "unlike" || action === "skip" || action === "dislike") {
    const stateResponse = await roomFetch(env, roomId, "/state", user);
    const room = (await stateResponse.json()) as RoomState;
    const profile = await loadProfile(env, user.id);
    const target = action === "dislike" && fields.id ? room.queue.find((track) => track.id === clean(fields.id, 32)) || room.now : room.now;
    if (action === "like" && target) addLike(profile, target);
    if (action === "unlike" && target) profile.liked = profile.liked.filter((track) => track.id !== target?.id);
    if ((action === "skip" || action === "dislike") && target) addSkip(profile, target);
    await saveProfile(env, user.id, profile);
    if (action === "like" || action === "unlike" || action === "dislike") {
      const roomActionName = action === "dislike" ? (fields.id && target?.id === clean(fields.id, 32) ? "remove" : "next") : "progress";
      const response = await roomFetch(env, roomId, "/action", user, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action: roomActionName, id: fields.id, position: room.position, duration: room.duration }) });
      const updated = await roomFetch(env, roomId, "/state", user);
      await database(env).prepare("UPDATE rooms SET updated_at = ?1 WHERE id = ?2").bind(Date.now(), roomId).run();
      return json({ ok: true, message: action === "like" ? "loved this song for your profile" : action === "unlike" ? "love removed" : "noted for your profile", state: await updated.json(), personal: publicProfile(profile, user) }, response.status);
    }
  }
  const response = await roomFetch(env, roomId, "/action", user, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(outgoing) });
  const payload = await response.json();
  if (!response.ok) return json(payload, response.status);
  const roomPayload = payload as Record<string, unknown>;
  const liveHostId = roomPayload.state && typeof roomPayload.state === "object" ? clean((roomPayload.state as Record<string, unknown>).hostId, 80) : "";
  if (liveHostId) await database(env).prepare("UPDATE rooms SET host_user_id = ?1, updated_at = ?2 WHERE id = ?3").bind(liveHostId, Date.now(), roomId).run();
  else await database(env).prepare("UPDATE rooms SET updated_at = ?1 WHERE id = ?2").bind(Date.now(), roomId).run();
  const profile = await loadProfile(env, user.id);
  return json({ ...(payload as Record<string, unknown>), personal: publicProfile(profile, user) }, response.status);
}

async function roomState(request: Request, env: Env, user: User, roomId: string): Promise<Response> {
  if (!(await isRoomMember(env, roomId, user.id))) return errorResponse("join the room first", 403);
  const response = await roomFetch(env, roomId, "/state", user);
  if (!response.ok) return response;
  const room = (await response.json()) as RoomState;
  const profile = await loadProfile(env, user.id);
  return json({ ...(room as unknown as Record<string, unknown>), personal: publicProfile(profile, user) });
}

async function roomStream(request: Request, env: Env, user: User, roomId: string): Promise<Response> {
  if (!(await isRoomMember(env, roomId, user.id))) return errorResponse("join the room first", 403);
  const headers = new Headers({ Upgrade: "websocket", "x-spotube-user-id": user.id, "x-spotube-user-name": user.displayName });
  return roomBinding(env, roomId).fetch(new Request("https://room.internal/stream", { headers }));
}

function settingsView(profile: ProfileState, env: Env): Record<string, unknown> {
  const engine = env.GEMINI_API_KEY ? "Gemini" : env.LLM_BASE_URL && env.LLM_API_KEY ? "OpenAI-compatible" : "offline parser";
  return {
    voiceEnabled: profile.voiceEnabled,
    autoplay: profile.autoplay,
    ttsProvider: profile.ttsProvider,
    ttsVoice: profile.ttsVoice,
    djLang: profile.djLang,
    languages: LANGUAGES,
    geminiVoices: GEMINI_VOICES,
    ttsProviders: ["gemini", "openai", "browser", "off"],
    planner: engine,
    geminiConfigured: Boolean(env.GEMINI_API_KEY),
    compatibleConfigured: Boolean(env.LLM_BASE_URL),
    compatibleTtsConfigured: Boolean(env.TTS_BASE_URL || env.LLM_BASE_URL),
    ttsConfigured: Boolean(env.GEMINI_API_KEY || env.TTS_BASE_URL || env.LLM_BASE_URL),
    ttsModel: env.GEMINI_TTS_MODEL || "gemini-2.5-flash-preview-tts",
  };
}

async function saveSettings(request: Request, env: Env, user: User): Promise<Response> {
  const profile = await loadProfile(env, user.id);
  const fields = await bodyFields(request);
  if (fields.autoplay !== undefined) profile.autoplay = fields.autoplay !== "off" && fields.autoplay !== "0" && fields.autoplay !== "false";
  if (fields.voiceEnabled !== undefined) profile.voiceEnabled = fields.voiceEnabled !== "off" && fields.voiceEnabled !== "0" && fields.voiceEnabled !== "false";
  if (["gemini", "openai", "browser", "off"].includes(fields.ttsProvider)) profile.ttsProvider = fields.ttsProvider as TtsProvider;
  if (fields.ttsVoice) profile.ttsVoice = clean(fields.ttsVoice, 80);
  if (fields.djLang) profile.djLang = LANGUAGES.find((item) => item.toLocaleLowerCase() === fields.djLang.toLocaleLowerCase()) || profile.djLang;
  await saveProfile(env, user.id, profile);
  return json({ ok: true, settings: settingsView(profile, env), state: publicProfile(profile, user) });
}

function base64Bytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function pcmWave(data: Uint8Array, rate: number, channels = 1, bits = 16): Uint8Array {
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  const write = (offset: number, text: string) => [...text].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  const block = channels * bits / 8;
  write(0, "RIFF"); view.setUint32(4, 36 + data.length, true); write(8, "WAVE"); write(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, channels, true); view.setUint32(24, rate, true); view.setUint32(28, rate * block, true); view.setUint16(32, block, true); view.setUint16(34, bits, true); write(36, "data"); view.setUint32(40, data.length, true);
  const output = new Uint8Array(44 + data.length); output.set(new Uint8Array(header)); output.set(data, 44); return output;
}

async function geminiSpeech(text: string, profile: ProfileState, env: Env): Promise<Response> {
  if (!env.GEMINI_API_KEY) return errorResponse("Gemini TTS is not configured", 503);
  const model = encodeURIComponent(env.GEMINI_TTS_MODEL || "gemini-2.5-flash-preview-tts");
  const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
    body: JSON.stringify({
      contents: [{ parts: [{ text }] }],
      generationConfig: { responseModalities: ["AUDIO"], speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: profile.ttsVoice || env.GEMINI_TTS_VOICE || "Despina" } } } },
    }),
  });
  if (!response.ok) return errorResponse(`Gemini TTS returned HTTP ${response.status}`, 502);
  const payload = (await response.json()) as Record<string, unknown>;
  const findAudio = (value: unknown): { data: string; mime: string } | null => {
    if (Array.isArray(value)) { for (const item of value) { const found = findAudio(item); if (found) return found; } return null; }
    if (!value || typeof value !== "object") return null;
    const object = value as Record<string, unknown>;
    if (typeof object.data === "string" && typeof object.mimeType === "string" && object.mimeType.toLocaleLowerCase().includes("audio")) return { data: object.data, mime: object.mimeType };
    for (const child of Object.values(object)) { const found = findAudio(child); if (found) return found; }
    return null;
  };
  const audio = findAudio(payload);
  if (!audio) return errorResponse("Gemini returned no audio", 502);
  const bytes = base64Bytes(audio.data);
  const sample = /rate=(\d+)/i.exec(audio.mime)?.[1];
  const isPcm = audio.mime.toLocaleLowerCase().includes("pcm");
  const body = isPcm ? pcmWave(bytes, Number(sample || 24000)) : bytes;
  return new Response(body.buffer as ArrayBuffer, { headers: { "content-type": isPcm ? "audio/wav" : audio.mime, "cache-control": "no-store" } });
}

async function compatibleSpeech(text: string, profile: ProfileState, env: Env): Promise<Response> {
  const base = env.TTS_BASE_URL || env.LLM_BASE_URL;
  const key = env.TTS_API_KEY || env.LLM_API_KEY;
  if (!base) return errorResponse("OpenAI-compatible TTS is not configured", 503);
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (key) headers.authorization = `Bearer ${key}`;
  const response = await fetch(openAiEndpoint(base, "/audio/speech"), {
    method: "POST",
    headers,
    body: JSON.stringify({ model: env.TTS_MODEL || "tts-1", voice: profile.ttsVoice || env.TTS_VOICE || "alloy", input: text, response_format: "mp3" }),
  });
  if (!response.ok) return errorResponse(`TTS provider returned HTTP ${response.status}`, 502);
  return new Response(response.body, { status: 200, headers: { "content-type": response.headers.get("content-type") || "audio/mpeg", "cache-control": "no-store" } });
}

async function tts(request: Request, env: Env, user: User): Promise<Response> {
  const fields = await bodyFields(request);
  const profile = await loadProfile(env, user.id);
  const text = clean(fields.text, 1500);
  if (!text) return errorResponse("text is required");
  const provider = (["gemini", "openai", "browser", "off"].includes(fields.provider) ? fields.provider : profile.ttsProvider) as TtsProvider;
  if (provider === "browser" || provider === "off") return json({ ok: true, provider, browser: provider === "browser" });
  return provider === "gemini" ? geminiSpeech(text, profile, env) : compatibleSpeech(text, profile, env);
}

async function bodyFields(request: Request): Promise<Record<string, string>> {
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    const value = await request.json() as unknown;
    if (!value || typeof value !== "object") return {};
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, val]) => [key, typeof val === "string" ? val : JSON.stringify(val)]));
  }
  const params = new URLSearchParams(await request.text());
  const output: Record<string, string> = {};
  params.forEach((value, key) => { output[key] = value; });
  return output;
}

function fieldsFromUrl(url: URL): Record<string, string> {
  const output: Record<string, string> = {};
  url.searchParams.forEach((value, key) => { output[key] = value; });
  return output;
}

async function fetchAsset(env: Env, request: Request): Promise<Response> {
  if (!env.ASSETS) return new Response("Worker assets binding is missing", { status: 500 });
  const response = await env.ASSETS.fetch(request);
  const headers = new Headers(response.headers);
  headers.set("x-content-type-options", "nosniff");
  headers.set("referrer-policy", "no-referrer");
  headers.set("x-frame-options", "DENY");
  headers.set("content-security-policy", "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://www.youtube.com; img-src 'self' data: https://i.ytimg.com https://*.googleusercontent.com https://*.ggpht.com; frame-src https://www.youtube.com https://www.youtube-nocookie.com; connect-src 'self' https://www.youtube.com");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

export class ListenRoom {
  private readonly state: DurableObjectState;
  private room: RoomState | null = null;
  private readonly sockets = new Map<WebSocket, RoomMember>();

  constructor(state: DurableObjectState) {
    this.state = state;
  }

  private async load(id = "room"): Promise<RoomState> {
    if (!this.room) this.room = normalizeRoom(await this.state.storage.get<unknown>("room"), id);
    return this.room;
  }

  private async persist(): Promise<void> {
    if (this.room) await this.state.storage.put("room", this.room);
  }

  private member(request: Request): RoomMember {
    return { id: clean(request.headers.get("x-spotube-user-id"), 80), name: clean(request.headers.get("x-spotube-user-name"), 60) || "Listener", joinedAt: Date.now() };
  }

  private allowed(request: Request, room: RoomState): RoomMember | null {
    const id = clean(request.headers.get("x-spotube-user-id"), 80);
    return id && room.members[id] ? room.members[id] : null;
  }

  private snapshot(): Record<string, unknown> {
    return this.room ? publicRoom(this.room) : {};
  }

  private broadcast(): void {
    const message = JSON.stringify({ type: "state", state: this.snapshot() });
    for (const socket of this.sockets.keys()) {
      try { socket.send(message); } catch { this.sockets.delete(socket); }
    }
  }

  private async roomAction(request: Request, fields: Record<string, string>, member: RoomMember): Promise<Response> {
    const room = await this.load();
    if (!room.members[member.id]) return errorResponse("not a member", 403);
    const action = clean(fields.action, 50).toLocaleLowerCase();
    if (action === "request" || action === "mix" || action === "radio") {
      let tracks: Track[] = [];
      try { tracks = (JSON.parse(fields.tracks || "[]") as unknown[]).map(normalizeTrack).filter(Boolean) as Track[]; } catch { tracks = []; }
      if (!tracks.length) return errorResponse("the room mix has no playable songs", 502);
      room.request = clean(fields.q, 240);
      const nextMixTrack = tracks.shift() || null;
      if (room.now && nextMixTrack && room.now.id !== nextMixTrack.id) {
        room.history.push(clone(room.now));
        room.history = room.history.slice(-40);
      }
      room.now = nextMixTrack;
      room.queue = tracks.slice(0, MAX_QUEUE);
      room.position = 0;
      room.duration = room.now?.duration || 0;
      room.playing = false;
      room.vibe = clean(fields.vibe, 120);
      room.why = clean(fields.why, 300);
      room.engine = clean(fields.engine, 80) || "offline parser";
      room.message = `Ready with ${room.queue.length + (room.now ? 1 : 0)} tracks. Press play.`;
    } else if (action === "play" || action === "resume" || action === "playpause") {
      room.playing = action === "playpause" ? !room.playing : true;
    } else if (action === "pause") {
      room.playing = false;
    } else if (action === "prev") {
      if (room.now && room.position > 5) {
        room.position = 0;
      } else {
        const previous = room.history.pop();
        if (previous) {
          if (room.now) room.queue.unshift(room.now);
          room.now = previous;
          room.position = 0;
          room.duration = previous.duration;
          room.playing = true;
          room.message = `playing ${previous.title}`;
        }
      }
    } else if (action === "next" || action === "skip" || action === "ended") {
      if (room.now && room.repeat === "one") {
        room.position = 0;
        room.playing = true;
      } else {
        if (room.now) {
          room.history.push(clone(room.now));
          room.history = room.history.slice(-40);
        }
        if (room.now && room.repeat === "all") room.queue.push(room.now);
        const next = room.queue.shift() || null;
        room.now = next;
        room.position = 0;
        room.duration = next?.duration || 0;
        room.playing = Boolean(next);
        room.message = next ? `playing ${next.title}` : "The room queue is empty. Make another blended mix.";
      }
    } else if (action === "play_row") {
      let selected = room.queue.find((track) => track.id === clean(fields.id, 32)) || null;
      try { selected ||= normalizeTrack(JSON.parse(fields.track || "null")); } catch { /* invalid client row */ }
      if (!selected) return errorResponse("that track is no longer available");
      room.queue = room.queue.filter((track) => track.id !== selected?.id);
      if (room.now && room.now.id !== selected.id) {
        room.history.push(clone(room.now));
        room.history = room.history.slice(-40);
      }
      room.now = selected;
      room.position = 0;
      room.duration = selected.duration;
      room.playing = true;
      room.message = `playing ${selected.title}`;
    } else if (action === "enqueue" || action === "queue_next") {
      let track: Track | null = null;
      try { track = normalizeTrack(JSON.parse(fields.track || "null")); } catch { track = null; }
      if (!track) return errorResponse("a valid track is required");
      room.queue = [track, ...room.queue.filter((item) => item.id !== track?.id)].slice(0, MAX_QUEUE);
    } else if (action === "remove") {
      room.queue = room.queue.filter((track) => track.id !== clean(fields.id, 32));
    } else if (action === "clear_queue") {
      room.queue = [];
    } else if (action === "progress" || action === "seek") {
      const position = Number(fields.position || fields.secs);
      const duration = Number(fields.duration);
      if (Number.isFinite(position)) room.position = Math.max(0, Math.min(86400, position));
      if (Number.isFinite(duration) && duration > 0) room.duration = Math.min(86400, duration);
    } else if (action === "shuffle") {
      room.shuffle = !room.shuffle;
      if (room.shuffle) room.queue.sort(() => Math.random() - 0.5);
    } else if (action === "repeat") {
      room.repeat = fields.mode === "off" || fields.mode === "all" || fields.mode === "one" ? fields.mode : room.repeat === "off" ? "all" : room.repeat === "all" ? "one" : "off";
    } else if (action === "leave") {
      delete room.members[member.id];
      for (const [socket, connected] of this.sockets.entries()) {
        if (connected.id !== member.id) continue;
        this.sockets.delete(socket);
        try { socket.close(1000, "left room"); } catch { /* already closed */ }
      }
      if (room.hostId === member.id) {
        const nextHost = Object.values(room.members)[0];
        room.hostId = nextHost?.id || "";
        room.hostName = nextHost?.name || "";
      }
    } else if (action === "like" || action === "unlike" || action === "dislike") {
      // Likes are personal and are saved by the Worker. The room action exists so
      // every client receives an immediate state-shaped response.
    } else {
      return errorResponse(`unknown room action ${action || ""}`);
    }
    room.updatedAt = Date.now();
    await this.persist();
    this.broadcast();
    return json({ ok: true, state: this.snapshot(), message: room.message });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const room = await this.load(clean(request.headers.get("x-spotube-room-id"), 80) || "room");
    if (url.pathname === "/init" && request.method === "POST") {
      const fields = await bodyFields(request);
      if (!room.hostId) {
        room.id = clean(fields.id, 80) || room.id;
        room.title = clean(fields.title, 100) || room.title;
        room.code = clean(fields.code, 12);
        room.hostId = clean(fields.hostId, 80);
        room.hostName = clean(fields.hostName, 60);
        room.members[room.hostId] = { id: room.hostId, name: room.hostName, joinedAt: Date.now() };
        await this.persist();
      }
      return json({ ok: true, state: this.snapshot() });
    }
    if (url.pathname === "/join" && request.method === "POST") {
      const fields = await bodyFields(request);
      const id = clean(fields.id, 80);
      if (!id) return errorResponse("member id required");
      const name = clean(fields.name, 60) || "Listener";
      room.members[id] = { id, name, joinedAt: Date.now() };
      if (!room.hostId) {
        room.hostId = id;
        room.hostName = name;
      }
      await this.persist();
      this.broadcast();
      return json({ ok: true, state: this.snapshot() });
    }
    if (url.pathname === "/members" && request.method === "GET") return json({ members: Object.keys(room.members) });
    if (url.pathname === "/state" && request.method === "GET") {
      if (!this.allowed(request, room)) return errorResponse("not a member", 403);
      return json(this.snapshot());
    }
    if (url.pathname === "/action" && request.method === "POST") {
      const member = this.allowed(request, room);
      if (!member) return errorResponse("not a member", 403);
      return this.roomAction(request, await bodyFields(request), member);
    }
    if (url.pathname === "/stream" && request.headers.get("Upgrade")?.toLocaleLowerCase() === "websocket") {
      const member = this.allowed(request, room);
      if (!member) return errorResponse("not a member", 403);
      const pair = new WebSocketPair();
      const client = pair[0];
      const server = pair[1];
      server.accept();
      this.sockets.set(server, member);
      server.addEventListener("close", () => this.sockets.delete(server));
      server.addEventListener("error", () => this.sockets.delete(server));
      server.addEventListener("message", (event) => {
        void (async () => {
          try {
            const raw = typeof event.data === "string" ? event.data : new TextDecoder().decode(event.data as ArrayBuffer);
            const value = JSON.parse(raw) as unknown;
            if (!value || typeof value !== "object") throw new Error("message must be an object");
            const fields = Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, typeof item === "string" ? item : JSON.stringify(item)]));
            const response = await this.roomAction(new Request("https://room.internal/action", { method: "POST" }), fields, member);
            const payload = await response.text();
            server.send(JSON.stringify({ type: "ack", ok: response.ok, ...(JSON.parse(payload) as Record<string, unknown>) }));
          } catch (error) {
            try { server.send(JSON.stringify({ type: "error", error: error instanceof Error ? error.message : "invalid room message" })); } catch { this.sockets.delete(server); }
          }
        })();
      });
      server.send(JSON.stringify({ type: "state", state: this.snapshot() }));
      return new Response(null, { status: 101, webSocket: client });
    }
    return errorResponse("room route not found", 404);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path === "/healthz" && request.method === "GET") return json({ ok: true, service: "spotube-dj-worker" });
      if (path === "/api/auth" && request.method === "GET") return json({ authenticated: Boolean(await currentUser(request, env)), user: await currentUser(request, env) });
      if (path === "/api/signup" && request.method === "POST") return signup(request, env);
      if (path === "/api/login" && request.method === "POST") return login(request, env);
      if (path === "/api/logout" && request.method === "POST") return json({ ok: true }, 200, { "set-cookie": clearSessionCookie(request) });
      if (path.startsWith("/api/")) {
        const checked = await requireUser(request, env);
        if (checked instanceof Response) return checked;
        const user = checked;
        if (path === "/api/state" && request.method === "GET") return json(publicProfile(await loadProfile(env, user.id), user));
        if (path === "/api/search" && (request.method === "GET" || request.method === "POST")) {
          const fields = request.method === "GET" ? fieldsFromUrl(url) : await bodyFields(request);
          const query = clean(fields.q, 240);
          if (!query) return errorResponse("q is required");
          try { return json({ ok: true, rows: (await ytmSearch(query, 18)).filter((row) => !row.reason).slice(0, 18) }); }
          catch (error) { return errorResponse(error instanceof Error ? error.message : "search failed", 502); }
        }
        if (path === "/api/action" && request.method === "POST") {
          try { const result = await runPersonalAction(user, await bodyFields(request), env); return json({ ok: true, message: result.message, state: result.state }); }
          catch (error) { return errorResponse(error instanceof Error ? error.message : "action failed", 502); }
        }
        if (path === "/api/settings" && request.method === "GET") {
          const profile = await loadProfile(env, user.id);
          return json({ settings: settingsView(profile, env), state: publicProfile(profile, user) });
        }
        if (path === "/api/settings" && request.method === "POST") return saveSettings(request, env, user);
        if (path === "/api/tts" && request.method === "POST") return tts(request, env, user);
        if (path === "/api/rooms" && request.method === "GET") return json({ rooms: await listRooms(env, user) });
        if (path === "/api/rooms" && request.method === "POST") return createRoom(request, env, user);
        if (path === "/api/rooms/join" && request.method === "POST") return joinRoom(request, env, user);
        const roomMatch = path.match(/^\/api\/rooms\/([^/]+)\/(state|stream|action|leave)$/);
        if (roomMatch) {
          const roomId = decodeURIComponent(roomMatch[1]);
          const operation = roomMatch[2];
          if (operation === "state" && request.method === "GET") return roomState(request, env, user, roomId);
          if (operation === "stream" && request.headers.get("Upgrade")?.toLocaleLowerCase() === "websocket") return roomStream(request, env, user, roomId);
          if (operation === "action" && request.method === "POST") {
            try { return roomAction(request, env, user, roomId, await bodyFields(request)); }
            catch (error) { return errorResponse(error instanceof Error ? error.message : "room action failed", 502); }
          }
          if (operation === "leave" && request.method === "POST") {
            if (!(await isRoomMember(env, roomId, user.id))) return errorResponse("you are not in that room", 403);
            const response = await roomFetch(env, roomId, "/action", user, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action: "leave" }) });
            const payload = await response.json() as Record<string, unknown>;
            if (!response.ok) return json(payload, response.status);
            const state = payload.state && typeof payload.state === "object" ? payload.state as Record<string, unknown> : {};
            const hostId = clean(state.hostId, 80);
            await database(env).prepare("DELETE FROM room_members WHERE room_id = ?1 AND user_id = ?2").bind(roomId, user.id).run();
            if (hostId) await database(env).prepare("UPDATE rooms SET host_user_id = ?1, updated_at = ?2 WHERE id = ?3").bind(hostId, Date.now(), roomId).run();
            else await database(env).prepare("UPDATE rooms SET updated_at = ?1 WHERE id = ?2").bind(Date.now(), roomId).run();
            return json(payload, response.status);
          }
        }
        return errorResponse("route not found", 404);
      }
      if (request.method !== "GET" && request.method !== "HEAD") return errorResponse("method not allowed", 405);
      return fetchAsset(env, request);
    } catch (error) {
      console.error("request failed", error);
      return errorResponse(error instanceof Error ? error.message : "internal error", 500);
    }
  },
};
