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
type SetSegment = "warm-up" | "late-night" | "focused" | "energetic" | "wind-down";

const SET_SEGMENTS: SetSegment[] = ["warm-up", "late-night", "focused", "energetic", "wind-down"];

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
  // Requester fields are descriptive metadata only. Playback always uses the
  // verified YouTube Music id and never a display name or client-supplied URL.
  requesterId?: string;
  requesterName?: string;
  requestedAt?: number;
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
  segment: SetSegment;
  engine: string;
  voiceEnabled: boolean;
  ttsProvider: TtsProvider;
  ttsVoice: string;
  djLang: string;
  djLead: string;
  updatedAt: number;
};

type User = { id: string; username: string; displayName: string };
type Plan = { queries: string[]; vibe: string; why: string; segment: SetSegment; engine: string; djLead: string; error?: string };
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
  autoplay: boolean;
  position: number;
  duration: number;
  repeat: RepeatMode;
  shuffle: boolean;
  vibe: string;
  why: string;
  segment: SetSegment;
  engine: string;
  message: string;
  djLead: string;
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
// Keep account creation inside the CPU budget of a free Worker while retaining a
// real salted work factor. New hashes carry their version so existing hashes from
// the earlier 100,000-iteration build can still be checked during migration.
const PASSWORD_ITERATIONS = 20_000;
const LEGACY_PASSWORD_ITERATIONS = 100_000;
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
const GEMINI_PLAN_MODELS = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-2.5-flash"];
const GEMINI_LIVE_MODELS = ["gemini-3.1-flash-live-preview", "gemini-2.5-flash-live-preview"];
const GEMINI_TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"];
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

function segmentOf(value: unknown, request = "", vibe = ""): SetSegment {
  const exact = clean(value, 40).toLocaleLowerCase() as SetSegment;
  if (SET_SEGMENTS.includes(exact)) return exact;
  const text = norm(`${clean(value, 80)} ${request} ${vibe}`);
  if (text.includes("late night") || text.includes("midnight") || text.includes("after dark")) return "late-night";
  if (text.includes("focus") || text.includes("study") || text.includes("deep work") || text.includes("concentrat")) return "focused";
  if (text.includes("energ") || text.includes("workout") || text.includes("gym") || text.includes("party") || text.includes("hype")) return "energetic";
  if (text.includes("wind down") || text.includes("wind-down") || text.includes("sleep") || text.includes("calm") || text.includes("relax") || text.includes("unwind")) return "wind-down";
  const hour = new Date().getHours();
  if (hour >= 22 || hour < 4) return "late-night";
  if (hour >= 18) return "wind-down";
  if (hour >= 11 && hour < 17) return "focused";
  return "warm-up";
}

function segmentLabel(segment: SetSegment, language = "English"): string {
  const lower = language.toLocaleLowerCase();
  if (lower === "indonesian") {
    return ({ "warm-up": "pemanasan", "late-night": "larut malam", focused: "fokus", energetic: "berenergi", "wind-down": "menurunkan tempo" } as Record<SetSegment, string>)[segment];
  }
  if (lower === "arabic") {
    return ({ "warm-up": "الإحماء", "late-night": "آخر الليل", focused: "التركيز", energetic: "الطاقة", "wind-down": "التهدئة" } as Record<SetSegment, string>)[segment];
  }
  return segment;
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

// The migration remains the source of truth for deploys. This idempotent fallback
// also makes a newly bound D1 usable when somebody deploys the Worker before
// running `wrangler d1 migrations apply`; the migration can still be applied later
// because every statement is guarded with IF NOT EXISTS.
let accountSchemaPromise: Promise<void> | null = null;

async function ensureAccountSchema(env: Env): Promise<void> {
  if (accountSchemaPromise) return accountSchemaPromise;
  const db = database(env);
  accountSchemaPromise = (async () => {
    const existing = await db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('users', 'sessions', 'profiles', 'rooms', 'room_members')").all<{ name: string }>();
    if ((existing.results || []).length !== 5) {
      await db.batch([
        db.prepare("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, password_salt TEXT NOT NULL, created_at INTEGER NOT NULL)"),
        db.prepare("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires_at INTEGER NOT NULL)"),
        db.prepare("CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id)"),
        db.prepare("CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at)"),
        db.prepare("CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, state_json TEXT NOT NULL, backup_json TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)"),
        db.prepare("CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, title TEXT NOT NULL, host_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"),
        db.prepare("CREATE TABLE IF NOT EXISTS room_members (room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, joined_at INTEGER NOT NULL, PRIMARY KEY(room_id, user_id))"),
        db.prepare("CREATE INDEX IF NOT EXISTS room_members_user_id ON room_members(user_id)"),
      ]);
    }
    const profileColumns = await db.prepare("PRAGMA table_info(profiles)").all<{ name: string }>();
    if ((profileColumns.results || []).some((column) => column.name === "user_id")) return;
    // Some earlier deployments created a single-profile table named `profiles`.
    // Keep that data under a legacy name, then install the user-keyed schema.
    const legacyName = `profiles_legacy_${Date.now().toString(36)}`;
    try {
      await db.batch([
        db.prepare(`ALTER TABLE profiles RENAME TO ${legacyName}`),
        db.prepare("CREATE TABLE profiles (user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, state_json TEXT NOT NULL, backup_json TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)"),
      ]);
    } catch (error) {
      // Another cold isolate may have repaired the table concurrently. Recheck
      // before surfacing a real D1 error to the signup request.
      const latest = await db.prepare("PRAGMA table_info(profiles)").all<{ name: string }>();
      if (!(latest.results || []).some((column) => column.name === "user_id")) throw error;
    }
  })().catch((error) => {
    accountSchemaPromise = null;
    throw error;
  });
  return accountSchemaPromise;
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

async function passwordHash(password: string, salt: string, iterations = PASSWORD_ITERATIONS): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    { name: "PBKDF2" },
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: hexToBytes(salt).buffer as ArrayBuffer, iterations, hash: "SHA-256" },
    key,
    256,
  );
  return bytesToHex(new Uint8Array(bits));
}

function storedPasswordHash(value: string): { digest: string; iterations: number } {
  const versioned = /^pbkdf2-sha256\$(\d+)\$([0-9a-f]+)$/i.exec(value);
  if (!versioned) return { digest: value, iterations: LEGACY_PASSWORD_ITERATIONS };
  const iterations = Number(versioned[1]);
  return Number.isSafeInteger(iterations) && iterations >= 1_000 && iterations <= LEGACY_PASSWORD_ITERATIONS
    ? { digest: versioned[2], iterations }
    : { digest: value, iterations: LEGACY_PASSWORD_ITERATIONS };
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
  await ensureAccountSchema(env);
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
  await ensureAccountSchema(env);
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
    const hash = await passwordHash(password, salt);
    await database(env).batch([
      database(env)
        .prepare("INSERT INTO users(id, username, display_name, password_hash, password_salt, created_at) VALUES(?1, ?2, ?3, ?4, ?5, ?6)")
        .bind(id, username, displayName || username, `pbkdf2-sha256$${PASSWORD_ITERATIONS}$${hash}`, salt, Date.now()),
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
  await ensureAccountSchema(env);
  const fields = await bodyFields(request);
  const username = clean(fields.username, 32).toLocaleLowerCase();
  const password = String(fields.password || "");
  const row = await database(env)
    .prepare("SELECT id, username, display_name AS displayName, password_hash AS passwordHash, password_salt AS passwordSalt FROM users WHERE username = ?1")
    .bind(username)
    .first<User & { passwordHash: string; passwordSalt: string }>();
  if (!row) return errorResponse("username or password is incorrect", 401);
  const stored = storedPasswordHash(row.passwordHash);
  const suppliedHash = await passwordHash(password, row.passwordSalt, stored.iterations);
  if (!safeEqual(suppliedHash, stored.digest)) return errorResponse("username or password is incorrect", 401);
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
    segment: "warm-up",
    engine: "offline parser",
    voiceEnabled: true,
    ttsProvider: "gemini",
    ttsVoice: "Despina",
    djLang: "Indonesian",
    djLead: "",
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
  const requesterId = clean(row.requesterId, 80);
  const requesterName = clean(row.requesterName, 60);
  const requestedAt = Number(row.requestedAt);
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
    official: row.official === true,
    score: Number.isFinite(Number(row.score)) ? Number(row.score) : undefined,
    requesterId: requesterId || undefined,
    requesterName: requesterName || undefined,
    requestedAt: Number.isFinite(requestedAt) && requestedAt > 0 ? requestedAt : undefined,
  };
}

type Requester = User | RoomMember;

function requesterTrack(track: Track, requester: Requester): Track {
  const name = "displayName" in requester ? requester.displayName : requester.name;
  return {
    ...clone(track),
    requesterId: clean(requester.id, 80) || undefined,
    requesterName: clean(name, 60) || "Listener",
    requestedAt: Date.now(),
  };
}

function normalizeProfile(value: unknown): ProfileState {
  const base = defaultProfile();
  if (!value || typeof value !== "object") return base;
  const raw = value as Record<string, unknown>;
  const queue = (Array.isArray(raw.queue) ? raw.queue : []).map(originalTrack).filter(Boolean) as Track[];
  const history = (Array.isArray(raw.history) ? raw.history : []).map(originalTrack).filter(Boolean) as Track[];
  const liked = (Array.isArray(raw.liked) ? raw.liked : []).map(originalTrack).filter(Boolean) as Track[];
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
    now: originalTrack(raw.now),
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
    segment: segmentOf(raw.segment, clean(raw.request, 240), clean(raw.vibe, 120)),
    engine: clean(raw.engine, 80) || base.engine,
    voiceEnabled: raw.voiceEnabled !== false,
    ttsProvider: provider,
    ttsVoice: clean(raw.ttsVoice, 80) || base.ttsVoice,
    djLang: lang,
    djLead: clean(raw.djLead, 280),
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
    // A saved provider, voice, language, and enabled flag are user data. Do not
    // reinterpret an old browser choice merely because a Worker secret was later
    // added; only a new profile gets defaultProfile() values.
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
  out.why = profile.now ? groundedWhy(profile) : clean(profile.why, 300);
  out.segment = segmentOf(profile.segment, profile.request, profile.vibe);
  out.djLine = djLine({ ...profile, why: out.why, segment: out.segment }, profile.djLang);
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

function thumbnailOf(row: Record<string, unknown>, id: string, official = false): string {
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
  const largest = found.sort((a, b) => b.area - a.area)[0];
  // Song rows often carry only a 60/120px artist avatar. Prefer a real
  // YouTube Music/video thumbnail for the large artwork card instead of
  // stretching that avatar across the now-playing panel.
  if (official && (!largest || largest.area < 160 * 160)) {
    return `https://i.ytimg.com/vi/${encodeURIComponent(id)}/hqdefault.jpg`;
  }
  return largest?.url || `https://i.ytimg.com/vi/${encodeURIComponent(id)}/hqdefault.jpg`;
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

const NON_ORIGINAL_TITLE = /\b(?:cover(?:s|ed)?|karaoke|tribute|remix(?:ed|es|ing)?|remake(?:s)?|reimagined|reimagining|rework(?:s|ed)?|reinterpret(?:ation|ed)?|re-record(?:ed|ing)?|rewrite|alternative\s+(?:version|take)|alt\s+version|originally\s+performed|slowed|(?:sped|speed)\s*[- ]?up|nightcore|8d|unplugged|rehearsal|demo)\b/i;
const LIVE_RECORDING_TITLE = /\b(?:live\s+(?:at|from|in|on|for|performance|performing|concert|version|take|recording|set|show|studio\s+session)|recorded\s+live|captured\s+live|filmed\s+live|performed\s+live|mtv\s+unplugged|in\s+concert|full\s+(?:concert|show|set)|live\s+\d{4})\b|[\(\[\{][^\)\]\}]{0,80}\b(?:live|unplugged|concert|performance|festival)\b[^\)\]\}]{0,80}[\)\]\}]/i;
const NON_MUSIC_TITLE = /\b(?:reaction|tutorial|review|lyrics?|podcast|episode|trailer|movie|film|fight(?:ing)?\s+scene|full\s+match|gameplay|how\s+to|unboxing|ambience|ambient\s+sounds?|white\s+noise|sleep\s+sounds?|study\s+with|focus\s+with|meditation|rain\s+sounds?|lofi\s+radio|radio\s+stream|live\s+stream|24\s*\/\s*7)\b/i;

function rejected(title: string, duration: number, official: boolean): string | null {
  // A YouTube Music Song catalog row is the provenance boundary. Ordinary
  // `Video`, `Episode`, and `Playlist` rows are deliberately not allowed to
  // become playable tracks, even when their title happens to look musical.
  if (!official) return "not an original YouTube Music Song";
  if (NON_ORIGINAL_TITLE.test(title) || LIVE_RECORDING_TITLE.test(title)) return "cover, remix, demo, or live recording";
  if (duration > 15 * 60 || /\b(?:dj\s+set|full\s+album|best\s+of|compilation|\d+\s*[- ]?hours?|long\s+mix)\b/i.test(title)) return "long-form upload";
  if (NON_MUSIC_TITLE.test(title)) return "not a song";
  return null;
}

function playableOriginal(track: Track): boolean {
  return track.official === true && !rejected(track.title, track.duration, true);
}

function originalTrack(value: unknown): Track | null {
  const track = normalizeTrack(value);
  return track && playableOriginal(track) ? track : null;
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
    const title = fields[0];
    const parts = (fields[1] || "").split(/[•·|]/).map((part) => clean(part, 120)).filter(Boolean);
    const official = Boolean(parts[0] && /^song\b/i.test(parts[0]));
    const duration = parts.reduce((longest, part) => Math.max(longest, durationOf(part)), 0);
    const artist = artistFrom(row, title, parts, official);
    const reason = rejected(title, duration, official);
    // Do not merely attach a rejection reason to arbitrary YouTube rows: a
    // `Video` row must never reach search, a generated mix, autoplay, or a
    // room queue. Only the catalog's Song rows cross this gate.
    if (reason) continue;
    seen.add(id);
    output.push({
      id,
      title,
      artist,
      channel: artist,
      duration,
      thumbnail: thumbnailOf(row, id, official),
      url: `https://music.youtube.com/watch?v=${encodeURIComponent(id)}`,
      official: true,
    });
    if (output.length >= limit * 3) break;
  }
  return output;
}

function randomChoice(values: string[]): string {
  if (!values.length) return "";
  const bytes = new Uint32Array(1);
  crypto.getRandomValues(bytes);
  return values[bytes[0] % values.length];
}

const DJ_LEADS_BY_SEGMENT: Record<SetSegment, string[]> = {
  "warm-up": [
    "Let the room settle for a second; I have a good first turn for this set.",
    "We are easing into the set with something that knows how to open a door.",
  ],
  "late-night": [
    "The lights are low now, so let us leave a little space around this one.",
    "This is the after-hours part of the set, where the small details get louder.",
  ],
  focused: [
    "For the focused stretch, I am keeping the edges clean and the pulse steady.",
    "A clear pocket is opening up here; this next choice can hold the line.",
  ],
  energetic: [
    "The set has found its stride, so I am giving the room another jolt of motion.",
    "We are turning the dial up without losing the thread; this is the next spark.",
  ],
  "wind-down": [
    "The edges are softening now, and I have a gentler handoff ready.",
    "We are winding down without switching off the glow; stay with me here.",
  ],
};
const DJ_LEADS_BY_SEGMENT_ID: Record<SetSegment, string[]> = {
  "warm-up": [
    "Kita beri ruang sebentar; aku punya pembuka yang pas untuk set ini.",
    "Kita mulai perlahan dengan lagu yang tahu cara membuka suasana.",
  ],
  "late-night": [
    "Lampunya sudah redup, jadi biarkan detail kecil lagu ini terdengar lebih dekat.",
    "Ini bagian larut malam, saat detail kecil terasa lebih jelas.",
  ],
  focused: [
    "Untuk bagian fokus, aku jaga tepinya tetap rapi dan nadinya tetap stabil.",
    "Ruang fokus sedang terbuka; pilihan berikutnya akan menjaga alurnya.",
  ],
  energetic: [
    "Set ini sudah menemukan langkahnya, jadi kita tambah sedikit tenaga.",
    "Kita naikkan energinya tanpa kehilangan benang merahnya; ini percikan berikutnya.",
  ],
  "wind-down": [
    "Tepinya mulai lembut, dan aku sudah siapkan perpindahan yang lebih tenang.",
    "Kita turunkan tempo tanpa mematikan cahayanya; tetap di sini.",
  ],
};

function creativeDjLead(request = "", vibe = "", language = "English", requestedSegment?: SetSegment): string {
  const segment = requestedSegment || segmentOf("", request, vibe);
  return randomChoice(language.toLocaleLowerCase() === "indonesian"
    ? DJ_LEADS_BY_SEGMENT_ID[segment]
    : DJ_LEADS_BY_SEGMENT[segment]);
}

function remoteDetail(body: string, max = 180): string {
  return clean(body
    .replace(/AIza[0-9A-Za-z_-]{20,}/g, "[redacted]")
    .replace(/Bearer\s+[A-Za-z0-9._-]+/gi, "Bearer [redacted]"), max);
}

function providerFailure(provider: string, status: number, body: string): Error {
  const detail = remoteDetail(body);
  return new Error(`${provider} planner returned HTTP ${status}${detail ? ` — ${detail}` : ""}`);
}

function artistLabel(state: ProfileState | RoomState, key: string): string {
  const wanted = norm(key);
  const tracks = [
    ...state.history,
    ...(("liked" in state) ? state.liked : []),
    ...(state.now ? [state.now] : []),
  ];
  const match = tracks.find((track) => norm(track.artist || track.channel) === wanted);
  if (match?.artist || match?.channel) return clean(match.artist || match.channel, 120);
  return clean(key, 120).replace(/\b\w/g, (letter) => letter.toLocaleUpperCase());
}

function recentArtist(state: ProfileState | RoomState): string {
  for (const track of [...state.history].reverse()) {
    const artist = clean(track.artist || track.channel, 120);
    if (artist) return artist;
  }
  return "";
}

function strongestArtist(state: ProfileState | RoomState): string {
  if (!("artists" in state)) return "";
  const entry = Object.entries(state.artists)
    .filter(([, weight]) => Number(weight) > 0)
    .sort((a, b) => b[1] - a[1])[0];
  return entry ? artistLabel(state, entry[0]) : "";
}

function strongestGenre(state: ProfileState | RoomState): string {
  if (!("genres" in state)) return "";
  const entry = Object.entries(state.genres)
    .filter(([, weight]) => Number(weight) > 0)
    .sort((a, b) => b[1] - a[1])[0];
  return entry ? clean(entry[0], 80) : "";
}

function groundedWhy(state: ProfileState | RoomState): string {
  const request = clean(state.request, 180);
  const heardArtist = recentArtist(state);
  const tasteArtist = strongestArtist(state);
  const genre = strongestGenre(state);
  if (request && heardArtist) return `You asked for ${request}, and you have been listening to ${heardArtist}, so I am keeping that thread in the set.`;
  if (request) return `You asked for ${request}, so I am shaping this set around that direction.`;
  if (heardArtist) return `I am bringing this in because you have been listening to ${heardArtist}, so I am keeping that thread moving.`;
  if (tasteArtist) return `I am staying close to ${tasteArtist}, one of the strongest signals in your taste profile.`;
  if (genre) return `Your taste leans toward ${genre}, so I am keeping that color in the room.`;
  return "I am following the shape of the set and keeping the choices close to the music.";
}

function planningSnapshot(profile: ProfileState): string {
  const artists = Object.entries(profile.artists)
    .filter(([, weight]) => Number(weight) > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([artist]) => `${artistLabel(profile, artist)} (${Number(profile.artists[artist]).toFixed(1)})`);
  const genres = Object.entries(profile.genres)
    .filter(([, weight]) => Number(weight) > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([genre]) => genre);
  const recent = profile.history.slice(-8).map((track) => `${track.title} by ${track.artist || track.channel}`).filter(Boolean);
  const current = profile.now ? `${profile.now.title} by ${profile.now.artist || profile.now.channel}` : "none";
  const queued = profile.queue.slice(0, 10).map((track) => `${track.title} by ${track.artist || track.channel}`).join(" | ");
  const skipped = profile.skipped.slice(-8).map((track) => `${track.title} by ${track.artist}`).join(" | ");
  return [
    `Current request: ${profile.request || "none"}`,
    `Current track: ${current}`,
    `Queue context, next first: ${queued || "none"}`,
    `Set context: segment ${profile.segment}; vibe ${profile.vibe || "unspecified"}`,
    `Taste-weighted artists: ${artists.join(", ") || "none"}`,
    `Taste-weighted genres: ${genres.join(", ") || "none"}`,
    `Recent listening history, newest last: ${recent.join(" | ") || "none"}`,
    `Loved songs: ${profile.liked.slice(-8).map((track) => `${track.title} by ${track.artist || track.channel}`).join(" | ") || "none"}`,
    `Recently skipped: ${skipped || "none"}`,
  ].join("\n");
}

function fallbackPlan(request: string, profile: ProfileState): Plan {
  const query = clean(request);
  const artists = Object.entries(profile.artists)
    .filter(([, weight]) => weight > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([artist]) => `${artistLabel(profile, artist)} music`);
  const genre = strongestGenre(profile);
  const queries = query ? [query] : artists.length ? artists : [genre ? `${genre} music` : "indie pop music"];
  const segment = segmentOf("", query, profile.vibe);
  const words = norm(query || genre || strongestArtist(profile) || "music").split(" ").filter(Boolean);
  const vibe = clean(words.slice(0, 4).join(" ") || `${segmentLabel(segment)} selections`, 120);
  const context = { ...profile, request: query };
  return {
    queries,
    vibe,
    why: groundedWhy(context),
    segment,
    engine: "offline parser",
    djLead: creativeDjLead(query, vibe, profile.djLang, segment),
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
  const vibe = clean(object.vibe, 120) || fallback.vibe;
  const segment = segmentOf(object.segment, fallback.queries.join(" "), vibe) || fallback.segment;
  const djLead = clean(object.djLead || object.dj_line || object.djLine, 280);
  // Keep the spoken reason deterministic. The model can suggest searches and
  // atmosphere, but it must not be able to claim a listening habit that is not
  // present in the profile snapshot supplied to it.
  return queries.length
    ? { queries, vibe, why: fallback.why, segment, engine, djLead: djLead || creativeDjLead(fallback.queries.join(" "), vibe, "English", segment) }
    : fallback;
}

async function geminiPlan(request: string, profile: ProfileState, env: Env, fallback: Plan): Promise<Plan> {
  if (!env.GEMINI_API_KEY) return fallback;
  const configured = clean(env.GEMINI_MODEL, 80);
  const models = [...new Set([configured, ...GEMINI_PLAN_MODELS].filter(Boolean))];
  let lastError: Error | null = null;
  for (const modelName of models) {
    const model = encodeURIComponent(modelName);
    const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify({ contents: [{ parts: [{ text: [
        "You are the creative planning brain for a contextual music radio host, not a metadata announcer.",
        "Return only one JSON object with exactly these fields: queries (string array), vibe (short phrase), segment (one of warm-up, late-night, focused, energetic, wind-down), why (short reason), djLead (short scene-setting radio intro).",
        "Use at most three concise YouTube Music searches. Search for original studio songs only: never ask for live recordings, covers, karaoke, tributes, remixes, reworks, slowed or sped-up versions, podcasts, tutorials, ambience, compilations, or long mixes.",
        "Ground every claim in the request or the supplied taste weights, loved songs, and recent listening history. Never invent an artist the listener heard. The deterministic host will write the final song handoff from the returned plan.",
        "Make vibe two to five words. Make why explain the real reason for this set. Make djLead vivid, warm, and lightly surprising in 12 to 28 words, with no emojis, markdown, stage directions, fake facts, song titles, artist names, or repeated Now playing phrasing.",
        `Listener request: ${request || "make a mix from these preferences"}`,
        planningSnapshot(profile),
      ].join("\n") }] }], generationConfig: { temperature: 0.85, responseMimeType: "application/json" } }),
    });
    const raw = await response.text();
    if (!response.ok) {
      lastError = providerFailure("Gemini", response.status, raw);
      const modelExpired = response.status === 404 || /model[^.]{0,100}(?:not found|not available|does not exist|does not support|unsupported)|not_found|invalid model/i.test(raw);
      if (modelExpired) continue;
      throw lastError;
    }
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      throw new Error(`Gemini planner returned invalid JSON from ${modelName}`);
    }
    const candidates = payload.candidates as Array<Record<string, unknown>> | undefined;
    const content = candidates?.[0]?.content as Record<string, unknown> | undefined;
    const parts = content?.parts as Array<Record<string, unknown>> | undefined;
    const text = parts?.map((part) => String(part.text || "")).join("") || "";
    const parsed = extractJson(text);
    const plan = planFromPayload(parsed, fallback, `Gemini · ${modelName}`);
    if (plan === fallback) throw new Error(`Gemini planner returned no usable plan from ${modelName}`);
    return plan;
  }
  throw lastError || new Error("Gemini planner has no available model");
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
      temperature: 0.85,
      messages: [{ role: "system", content: [
        "You are the creative planning brain for a contextual music radio host, not a metadata announcer.",
        "Return only JSON with queries, vibe, segment, why, and djLead. Segment must be warm-up, late-night, focused, energetic, or wind-down.",
        "Use at most three concise YouTube Music searches for original studio songs only. Never request live, cover, karaoke, tribute, remix, rework, slowed, sped-up, podcast, tutorial, ambience, compilation, or long-form results.",
        "Ground claims in the listener request and the supplied profile snapshot. Never invent listening history. djLead is only a vivid 12 to 28 word scene-setter: no emojis, markdown, stage directions, fake facts, song or artist names, or Now playing phrasing.",
      ].join(" ") }, { role: "user", content: `Request: ${request || "a mix from my likes"}.\n${planningSnapshot(profile)}` }],
    }),
  });
  const raw = await response.text();
  if (!response.ok) throw providerFailure("OpenAI-compatible", response.status, raw);
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    throw new Error("OpenAI-compatible planner returned invalid JSON");
  }
  const choices = payload.choices as Array<Record<string, unknown>> | undefined;
  const message = choices?.[0]?.message as Record<string, unknown> | undefined;
  const plan = planFromPayload(extractJson(String(message?.content || "")), fallback, "OpenAI-compatible");
  if (plan === fallback) throw new Error("OpenAI-compatible planner returned no usable plan");
  return plan;
}

async function makePlan(request: string, profile: ProfileState, env: Env): Promise<Plan> {
  const fallback = fallbackPlan(request, profile);
  if (!env.GEMINI_API_KEY && !env.LLM_BASE_URL) {
    return {
      ...fallback,
      engine: "offline parser",
      error: "No AI planner is configured; the request, taste, and set context were parsed locally.",
    };
  }
  try {
    if (env.GEMINI_API_KEY) return await geminiPlan(request, profile, env, fallback);
    if (env.LLM_BASE_URL) return await compatiblePlan(request, profile, env, fallback);
  } catch (error) {
    const provider = env.GEMINI_API_KEY ? "Gemini" : "OpenAI-compatible";
    const detail = error instanceof Error ? error.message : "unknown planner error";
    console.error("planner failed", detail);
    return { ...fallback, engine: `${provider} unavailable → offline parser`, error: detail };
  }
  return { ...fallback, error: "No AI planner is configured; the request was parsed locally." };
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
      if (result.reason || !playableOriginal(result)) continue;
      const track = normalizeTrack({ ...result, query: plan.queries[index] });
      if (!track || !playableOriginal(track)) continue;
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
  const history = new Map<string, Track>();
  for (const profile of profiles) {
    Object.entries(profile.artists).forEach(([name, weight]) => { artistTotals[name] = (artistTotals[name] || 0) + weight; });
    Object.entries(profile.genres).forEach(([name, weight]) => { genreTotals[name] = (genreTotals[name] || 0) + weight; });
    profile.liked.forEach((track) => liked.set(track.id, track));
    profile.history.forEach((track) => history.set(`${track.id}:${track.title}`, track));
    profile.skipped.forEach((track) => skipped.set(track.id, track));
  }
  blended.history = [...history.values()].slice(-40);
  blended.artists = Object.fromEntries(Object.entries(artistTotals).map(([key, value]) => [key, value / Math.max(1, profiles.length)]));
  blended.genres = Object.fromEntries(Object.entries(genreTotals).map(([key, value]) => [key, value / Math.max(1, profiles.length)]));
  blended.liked = [...liked.values()].slice(-MAX_LIKES);
  blended.skipped = [...skipped.values()].slice(-MAX_SKIPS);
  blended.shuffle = profiles.some((profile) => profile.shuffle);
  // The requester's language is used for the HTTP response/stream, not mixed
  // into the room's taste profile. Keep a deterministic value for the planner's
  // offline path when a room has members with different language preferences.
  blended.djLang = profiles[0]?.djLang || blended.djLang;
  return blended;
}

function roomPlanningProfile(blended: ProfileState, room: RoomState, request: string): ProfileState {
  const context = clone(blended);
  const roomHistory = new Map<string, Track>();
  for (const track of [...blended.history, ...room.history]) roomHistory.set(`${track.id}:${track.title}`, track);
  context.request = clean(request || room.request, 240);
  context.now = room.now ? clone(room.now) : null;
  context.queue = room.queue.map((track) => clone(track)).slice(0, MAX_QUEUE);
  context.history = [...roomHistory.values()].slice(-40);
  context.vibe = clean(room.vibe, 120) || context.vibe;
  context.segment = segmentOf(room.segment, context.request, context.vibe);
  context.message = clean(room.message, 300);
  return context;
}

function similarRequest(state: ProfileState | RoomState): string {
  const now = state.now;
  const artist = now ? clean(now.artist || now.channel, 100) : "";
  const current = now ? `similar original studio songs to ${clean(now.title, 150)}${artist ? ` by ${artist}` : ""}` : "similar original studio songs";
  const request = clean(state.request, 160);
  const vibe = clean(state.vibe, 80);
  const segment = segmentOf(state.segment, request, vibe);
  return clean([request ? `more like ${request}` : "", current, vibe ? `${vibe} vibe` : "", `${segment} set`].filter(Boolean).join(", "), 240);
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

async function advanceProfile(profile: ProfileState, env: Env, markSkip: boolean, requester?: User): Promise<void> {
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
  let generatedPlan: Plan | null = null;
  if (!next && profile.autoplay && (profile.request || current)) {
    const fallbackRequest = profile.request || similarRequest({ ...profile, now: current });
    const built = await buildMix(fallbackRequest, { ...profile, request: fallbackRequest }, env, 16);
    generatedPlan = built.plan;
    const blocked = new Set([current?.id || "", ...profile.history.slice(-8).map((track) => track.id)].filter(Boolean));
    const generated = built.tracks
      .filter((track) => !blocked.has(track.id))
      .map((track) => requester ? requesterTrack(track, requester) : track);
    next = generated.shift() || null;
    profile.queue.push(...generated);
    profile.vibe = built.plan.vibe;
    profile.why = built.plan.why;
    profile.segment = built.plan.segment;
    profile.engine = built.plan.engine;
  }
  profile.now = next;
  profile.position = 0;
  profile.duration = next?.duration || 0;
  profile.paused = !next;
  profile.djLead = generatedPlan?.djLead || creativeDjLead(profile.request, profile.vibe, profile.djLang, profile.segment);
  profile.message = generatedPlan?.error
    ? `AI brain unavailable; used the offline parser. ${generatedPlan.error}`
    : next ? `Up next: ${next.title}` : "The queue is empty. Make another mix to continue.";
}

function groundedWhyId(state: ProfileState | RoomState): string {
  const request = clean(state.request, 180);
  const heardArtist = recentArtist(state);
  const tasteArtist = strongestArtist(state);
  const genre = strongestGenre(state);
  if (request && heardArtist) return `Kamu minta ${request}, dan belakangan ini kamu sering mendengarkan ${heardArtist}, jadi benang itu tetap kita bawa.`;
  if (request) return `Kamu minta ${request}, jadi set ini kita arahkan ke sana.`;
  if (heardArtist) return `Belakangan ini kamu mendengarkan ${heardArtist}, jadi nuansanya kita teruskan.`;
  if (tasteArtist) return `Kita tetap dekat dengan ${tasteArtist}, salah satu sinyal terkuat dari selera musikmu.`;
  if (genre) return `Selera musikmu condong ke ${genre}, jadi warna itu tetap ada di ruangan.`;
  return "Aku mengikuti bentuk set ini dan membiarkan musiknya menjaga alurnya.";
}

function variantFor(seed: string, count: number): number {
  let value = 0;
  for (const character of seed) value = (value * 31 + character.charCodeAt(0)) >>> 0;
  return count ? value % count : 0;
}

function compactReason(state: ProfileState | RoomState, language: string): string {
  const request = clean(state.request, 90);
  const heardArtist = clean(recentArtist(state), 80);
  const tasteArtist = clean(strongestArtist(state), 80);
  const genre = clean(strongestGenre(state), 60);
  const lower = language.toLocaleLowerCase();
  if (lower === "indonesian") {
    if (request && heardArtist) return `Untuk ${request}, dengan benang dari ${heardArtist}.`;
    if (request) return `Untuk ${request}.`;
    if (heardArtist) return `Meneruskan warna ${heardArtist}.`;
    if (tasteArtist) return `Dekat dengan selera ${tasteArtist}.`;
    if (genre) return `Dengan warna ${genre}.`;
    return "Menjaga alur set ini.";
  }
  if (lower === "arabic") {
    if (request && heardArtist) return `لأجل ${request}، مع لمسة من ${heardArtist}.`;
    if (request) return `لأجل ${request}.`;
    if (heardArtist) return `نواصل أجواء ${heardArtist}.`;
    if (tasteArtist) return `قريب من ذوق ${tasteArtist}.`;
    if (genre) return `بلون ${genre}.`;
    return "نحافظ على مسار المجموعة.";
  }
  if (request && heardArtist) return `More like ${request}, with a thread from ${heardArtist}.`;
  if (request) return `For ${request}.`;
  if (heardArtist) return `Keeping a thread from ${heardArtist}.`;
  if (tasteArtist) return `Staying close to ${tasteArtist}.`;
  if (genre) return `Keeping a ${genre} color.`;
  return "Keeping the set moving.";
}

function compactLead(segment: SetSegment, language: string): string {
  const lower = language.toLocaleLowerCase();
  if (lower === "indonesian") {
    return ({
      "warm-up": "Kita mulai rapi.",
      "late-night": "Lampu diredupkan.",
      focused: "Kita tetap fokus.",
      energetic: "Set ini mulai bergerak.",
      "wind-down": "Kita turunkan tempo.",
    } as Record<SetSegment, string>)[segment];
  }
  if (lower === "arabic") {
    return ({
      "warm-up": "نبدأ بهدوء.",
      "late-night": "الأضواء خافتة.",
      focused: "نبقى في التركيز.",
      energetic: "المجموعة تتحرك.",
      "wind-down": "نهدئ الإيقاع.",
    } as Record<SetSegment, string>)[segment];
  }
  return ({
    "warm-up": "A clean start.",
    "late-night": "Lights low.",
    focused: "Locked in.",
    energetic: "The set is moving.",
    "wind-down": "Edges softening.",
  } as Record<SetSegment, string>)[segment];
}

function djLine(state: ProfileState | RoomState, language = "English"): string {
  const now = state.now;
  if (!now) return "";
  const next = state.queue[0];
  const segment = segmentOf(state.segment, state.request, state.vibe);
  const segmentText = segmentLabel(segment, language);
  const vibe = clean(state.vibe, 70);
  const artist = clean(now.artist || now.channel || "the next sound", 100);
  const title = clean(now.title, 150);
  const nextArtist = next ? clean(next.artist || next.channel || "the next sound", 100) : "";
  const nextTitle = next ? clean(next.title, 150) : "";
  const indonesian = language.toLocaleLowerCase() === "indonesian";
  const reason = compactReason(state, language);
  // Keep this deterministic and deliberately short. The planner supplies the
  // set context, while verified track/profile state supplies every factual
  // claim; the browser can therefore speak one quick handoff without stacking
  // a long intro, a repeated reason, and a second Now-playing sentence.
  if (indonesian) {
    const mood = vibe ? `Bagian ${segmentText}, nuansa ${vibe}.` : `Bagian ${segmentText}.`;
    const current = `Ini ${title} dari ${artist}.`;
    const handoff = next ? `Berikutnya ${nextTitle} dari ${nextArtist}.` : "Biarkan lagu ini mengalir.";
    return `${compactLead(segment, language)} ${reason} ${mood} ${current} ${handoff}`.replace(/\.\./g, ".");
  }
  if (language.toLocaleLowerCase() === "arabic") {
    const mood = vibe ? `أجواء ${segmentText} بطابع ${vibe}.` : `أجواء ${segmentText}.`;
    const current = `هذه ${title} لـ ${artist}.`;
    const handoff = next ? `التالي ${nextTitle} لـ ${nextArtist}.` : "دعوا هذه الأغنية تتنفس.";
    return `${compactLead(segment, language)} ${reason} ${mood} ${current} ${handoff}`;
  }
  const mood = vibe ? `${segmentText} set, ${vibe} mood.` : `${segmentText} set.`;
  const current = `${title} by ${artist}.`;
  const handoff = next ? `Next, ${nextTitle} by ${nextArtist}.` : "Let this one breathe.";
  return `${compactLead(segment, language)} ${reason} ${mood} ${current} ${handoff}`.replace(/\.\./g, ".");
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
    const generated = built.tracks.map((track) => requesterTrack(track, user));
    const nextMixTrack = generated.shift() || null;
    if (profile.now && nextMixTrack && profile.now.id !== nextMixTrack.id) {
      profile.history.push(clone(profile.now));
      profile.history = profile.history.slice(-40);
    }
    profile.now = nextMixTrack;
    profile.queue = generated.slice(0, MAX_QUEUE);
    profile.position = 0;
    profile.duration = profile.now?.duration || 0;
    profile.paused = true;
    profile.vibe = built.plan.vibe;
    profile.why = built.plan.why;
    profile.segment = built.plan.segment;
    profile.engine = built.plan.engine;
    profile.djLead = built.plan.djLead || creativeDjLead(request, profile.vibe, profile.djLang, profile.segment);
    profile.message = built.plan.error
      ? `AI brain unavailable; used the offline parser. ${built.plan.error}`
      : `Ready with ${profile.queue.length + (profile.now ? 1 : 0)} tracks. Press play.`;
    message = built.plan.error ? "mix ready with offline brain" : "mix ready";
  } else if (action === "play" || action === "resume" || action === "playpause") {
    if (!profile.now) await advanceProfile(profile, env, false, user);
    profile.paused = profile.now ? (action === "playpause" ? !profile.paused : false) : true;
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
        profile.djLead = creativeDjLead(profile.request, profile.vibe, profile.djLang);
        profile.message = `playing ${previous.title}`;
        message = profile.message;
      }
    }
  } else if (action === "next" || action === "skip" || action === "ended") {
    if (action === "ended") {
      const endedPosition = Number(fields.position);
      const endedDuration = Number(fields.duration);
      if (Number.isFinite(endedPosition)) profile.position = Math.max(0, Math.min(86400, endedPosition));
      if (Number.isFinite(endedDuration) && endedDuration > 0) profile.duration = Math.min(86400, endedDuration);
      addHeard(profile);
    }
    await advanceProfile(profile, env, action !== "ended", user);
    message = profile.now ? `playing ${profile.now.title}` : "the queue is empty";
  } else if (action === "play_row") {
    const supplied = fields.track ? normalizeTrack(JSON.parse(fields.track)) : null;
    const id = clean(fields.id, 32);
    const queued = profile.queue.find((track) => track.id === id);
    const selected = queued || (supplied ? requesterTrack(supplied, user) : null);
    if (!selected || !playableOriginal(selected)) throw new Error("that track is not an original YouTube Music song");
    profile.queue = profile.queue.filter((track) => track.id !== selected.id);
    if (profile.now && profile.now.id !== selected.id) {
      profile.history.push(clone(profile.now));
      profile.history = profile.history.slice(-40);
    }
    profile.now = selected;
    profile.position = 0;
    profile.duration = selected.duration;
    profile.paused = false;
    profile.djLead = creativeDjLead(profile.request, profile.vibe, profile.djLang);
    profile.message = `playing ${selected.title}`;
    message = profile.message;
  } else if (action === "enqueue" || action === "queue_next") {
    const supplied = fields.track ? normalizeTrack(JSON.parse(fields.track)) : null;
    const track = supplied ? requesterTrack(supplied, user) : null;
    if (!track || !playableOriginal(track)) throw new Error("only original YouTube Music songs can be queued");
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
    autoplay: true,
    position: 0,
    duration: 0,
    repeat: "off",
    shuffle: false,
    vibe: "",
    why: "",
    segment: "warm-up",
    engine: "offline parser",
    message: "Create a mix for the room.",
    djLead: "",
    updatedAt: Date.now(),
  };
  if (!value || typeof value !== "object") return base;
  const raw = value as Record<string, unknown>;
  const members: Record<string, RoomMember> = {};
  const memberEntries = Array.isArray(raw.members)
    ? raw.members.map((item) => [clean((item as Record<string, unknown> | null)?.id, 80), item] as [string, unknown])
    : raw.members && typeof raw.members === "object"
      ? Object.entries(raw.members as Record<string, unknown>)
      : [];
  for (const [memberId, item] of memberEntries) {
    if (!memberId || !item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    members[memberId] = { id: memberId, name: clean(row.name, 60) || "Listener", joinedAt: Number(row.joinedAt) || Date.now() };
  }
  return {
    ...base,
    id: clean(raw.id, 80) || id,
    title: clean(raw.title, 100) || base.title,
    hostId: clean(raw.hostId, 80),
    hostName: clean(raw.hostName, 60),
    members,
    request: clean(raw.request, 240),
    now: originalTrack(raw.now),
    queue: (Array.isArray(raw.queue) ? raw.queue : []).map(originalTrack).filter(Boolean).slice(0, MAX_QUEUE) as Track[],
    history: (Array.isArray(raw.history) ? raw.history : []).map(originalTrack).filter(Boolean).slice(-40) as Track[],
    playing: Boolean(raw.playing),
    autoplay: raw.autoplay !== false,
    position: Math.max(0, Math.min(86400, Number(raw.position) || 0)),
    duration: Math.max(0, Math.min(86400, Number(raw.duration) || 0)),
    repeat: raw.repeat === "all" || raw.repeat === "one" ? raw.repeat : "off",
    shuffle: Boolean(raw.shuffle),
    vibe: clean(raw.vibe, 120),
    why: clean(raw.why, 300),
    segment: segmentOf(raw.segment, clean(raw.request, 240), clean(raw.vibe, 120)),
    engine: clean(raw.engine, 80) || base.engine,
    message: clean(raw.message, 300) || base.message,
    djLead: clean(raw.djLead, 280),
    updatedAt: Number(raw.updatedAt) || Date.now(),
  };
}

function publicRoom(room: RoomState, language = "English"): Record<string, unknown> {
  const output = clone(room) as RoomState & Record<string, unknown>;
  output.queue = output.queue.slice(0, MAX_QUEUE);
  output.members = Object.values(room.members) as unknown as Record<string, RoomMember>;
  output.playing = room.playing;
  output.segment = segmentOf(room.segment, room.request, room.vibe);
  output.djLang = LANGUAGES.find((item) => item.toLocaleLowerCase() === language.toLocaleLowerCase()) || "English";
  output.djLine = djLine({ ...room, segment: output.segment }, String(output.djLang));
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

function localizedRoomState(value: unknown, language: string, roomId: string): Record<string, unknown> {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const room = normalizeRoom(raw, roomId);
  return { ...raw, djLang: language, djLine: djLine(room, language) };
}

async function roomAction(request: Request, env: Env, user: User, roomId: string, fields: Record<string, string>): Promise<Response> {
  if (!(await isRoomMember(env, roomId, user.id))) return errorResponse("join the room first", 403);
  const action = clean(fields.action, 50).toLocaleLowerCase();
  const outgoing = { ...fields };
  let currentRoom: RoomState | null = null;
  const readRoom = async (): Promise<RoomState> => {
    if (currentRoom) return currentRoom;
    const stateResponse = await roomFetch(env, roomId, "/state", user);
    currentRoom = stateResponse.ok ? normalizeRoom(await stateResponse.json(), roomId) : normalizeRoom(null, roomId);
    return currentRoom;
  };

  if (action === "request" || action === "mix" || action === "radio") {
    const room = await readRoom();
    const memberIds = await roomMembers(env, roomId);
    const profiles = await Promise.all(memberIds.map((id) => loadProfile(env, id)));
    const blended = blendProfiles(profiles);
    const requestText = clean(
      fields.q ||
        (action === "radio" ? `more like ${fields.title || room.now?.title || "this song"} ${fields.artist || room.now?.artist || ""}` : "") ||
        room.request ||
        "a blended set from the room's listening history",
    );
    // Include the room's live request, current song, queue, history, vibe, and
    // segment in the planner profile. Previously only member taste weights were
    // sent, so a room brain could silently plan against the wrong context.
    const planningProfile = roomPlanningProfile(blended, room, requestText);
    const built = await buildMix(requestText, planningProfile, env, 24);
    if (!built.tracks.length) return errorResponse("YouTube Music returned no playable songs", 502);
    outgoing.q = requestText;
    outgoing.tracks = JSON.stringify(built.tracks.map((track) => requesterTrack(track, user)));
    outgoing.vibe = built.plan.vibe;
    outgoing.why = built.plan.why;
    outgoing.segment = built.plan.segment;
    outgoing.engine = built.plan.engine;
    const memberIndex = memberIds.indexOf(user.id);
    const memberProfile = (memberIndex >= 0 ? profiles[memberIndex] : null) || await loadProfile(env, user.id);
    outgoing.djLead = built.plan.djLead || creativeDjLead(requestText, built.plan.vibe, memberProfile.djLang, built.plan.segment);
    outgoing.brainError = built.plan.error || "";
  }

  // Never trust requester text sent by a browser. Re-normalize the verified
  // song row and attach the authenticated member's identity at the edge.
  if (action === "enqueue" || action === "queue_next" || action === "play_row") {
    let supplied: Track | null = null;
    try { supplied = normalizeTrack(JSON.parse(fields.track || "null")); } catch { supplied = null; }
    if (!supplied || !playableOriginal(supplied)) return errorResponse("only original YouTube Music songs can be queued");
    outgoing.track = JSON.stringify(requesterTrack(supplied, user));
    outgoing.id = supplied.id;
  }

  // A shared room has no local audio process to refill its queue. When playback
  // is active and the last track is skipped/ended, ask the same blended planner
  // for a guarded similar-song refill before the Durable Object advances to an
  // empty state. This is a separate action so it cannot recurse through `next`.
  if (action === "next" || action === "skip" || action === "ended") {
    const room = await readRoom();
    if (room.autoplay && room.playing && room.now && room.repeat !== "one" && room.repeat !== "all" && !room.queue.length) {
      try {
        const memberIds = await roomMembers(env, roomId);
        const profiles = await Promise.all(memberIds.map((id) => loadProfile(env, id)));
        const blended = blendProfiles(profiles);
        const requestText = similarRequest(room);
        const planningProfile = roomPlanningProfile(blended, room, requestText);
        const built = await buildMix(requestText, planningProfile, env, 16);
        const blocked = new Set([room.now.id, ...room.history.slice(-8).map((track) => track.id)]);
        const generated = built.tracks.filter((track) => !blocked.has(track.id));
        if (generated.length) {
          outgoing.action = "autoplay_next";
          outgoing.baseId = room.now.id;
          outgoing.q = room.request || requestText;
          outgoing.tracks = JSON.stringify(generated.map((track) => requesterTrack(track, user)));
          outgoing.vibe = built.plan.vibe;
          outgoing.why = built.plan.why;
          outgoing.segment = built.plan.segment;
          outgoing.engine = built.plan.engine;
          const memberIndex = memberIds.indexOf(user.id);
          const memberProfile = (memberIndex >= 0 ? profiles[memberIndex] : null) || await loadProfile(env, user.id);
          outgoing.djLead = built.plan.djLead || creativeDjLead(outgoing.q, built.plan.vibe, memberProfile.djLang, built.plan.segment);
          outgoing.brainError = built.plan.error || "";
        } else {
          outgoing.autoplayError = built.plan.error || "the similar-song search returned no new original songs";
        }
      } catch (error) {
        outgoing.autoplayError = clean(error instanceof Error ? error.message : "the similar-song search failed", 180);
      }
    }
  }

  if (action === "like" || action === "unlike" || action === "skip" || action === "dislike") {
    const room = await readRoom();
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
      const updatedState = await updated.json();
      return json({ ok: true, message: action === "like" ? "loved this song for your profile" : action === "unlike" ? "love removed" : "noted for your profile", state: localizedRoomState(updatedState, profile.djLang, roomId), personal: publicProfile(profile, user) }, response.status);
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
  if (roomPayload.state && typeof roomPayload.state === "object") roomPayload.state = localizedRoomState(roomPayload.state, profile.djLang, roomId);
  return json({ ...roomPayload, personal: publicProfile(profile, user) }, response.status);
}

async function roomState(request: Request, env: Env, user: User, roomId: string): Promise<Response> {
  if (!(await isRoomMember(env, roomId, user.id))) return errorResponse("join the room first", 403);
  const response = await roomFetch(env, roomId, "/state", user);
  if (!response.ok) return response;
  const room = await response.json();
  const profile = await loadProfile(env, user.id);
  return json({ ...localizedRoomState(room, profile.djLang, roomId), personal: publicProfile(profile, user) });
}

async function roomStream(request: Request, env: Env, user: User, roomId: string): Promise<Response> {
  if (!(await isRoomMember(env, roomId, user.id))) return errorResponse("join the room first", 403);
  const profile = await loadProfile(env, user.id);
  const headers = new Headers({ Upgrade: "websocket", "x-spotube-user-id": user.id, "x-spotube-user-name": user.displayName, "x-spotube-dj-language": profile.djLang });
  return roomBinding(env, roomId).fetch(new Request("https://room.internal/stream", { headers }));
}

function settingsView(profile: ProfileState, env: Env): Record<string, unknown> {
  const engine = env.GEMINI_API_KEY ? "Gemini" : env.LLM_BASE_URL ? "OpenAI-compatible" : "offline parser";
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
    plannerModel: env.GEMINI_API_KEY ? env.GEMINI_MODEL || GEMINI_PLAN_MODELS[0] : env.LLM_MODEL || "llama3.2",
    geminiConfigured: Boolean(env.GEMINI_API_KEY),
    compatibleConfigured: Boolean(env.LLM_BASE_URL),
    compatibleTtsConfigured: Boolean(env.TTS_BASE_URL || env.LLM_BASE_URL),
    ttsConfigured: Boolean(env.GEMINI_API_KEY || env.TTS_BASE_URL || env.LLM_BASE_URL),
    ttsModel: env.GEMINI_TTS_MODEL || env.TTS_MODEL || "gemini-3.1-flash-live-preview",
  };
}

async function testBrain(env: Env, user: User): Promise<Response> {
  if (!env.GEMINI_API_KEY && !env.LLM_BASE_URL) {
    return json({ ok: false, configured: false, engine: "offline parser", error: "No AI planner is configured. Add GEMINI_API_KEY or LLM_BASE_URL in Wrangler secrets." });
  }
  const profile = await loadProfile(env, user.id);
  const plan = await makePlan("a creative warm-up set with a little surprise", profile, env);
  return json({ ok: !plan.error, configured: true, engine: plan.engine, error: plan.error || "", plan: { queries: plan.queries, vibe: plan.vibe, segment: plan.segment, why: plan.why, djLead: plan.djLead } });
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

function isGeminiLiveModel(model: string): boolean {
  return /(?:^|-)live(?:-|$)/i.test(model);
}

function geminiModelUnavailable(detail: string): boolean {
  return /(?:model|models\/)[^.\n]{0,120}(?:not found|not available|does not exist|does not support|only supports|unsupported|invalid)|not_found|invalid model/i.test(detail);
}

async function liveSocketData(value: unknown): Promise<{ bytes: Uint8Array; text: string }> {
  if (typeof value === "string") {
    return { bytes: new TextEncoder().encode(value), text: value };
  }
  if (value instanceof Blob) {
    const bytes = new Uint8Array(await value.arrayBuffer());
    return { bytes, text: new TextDecoder().decode(bytes) };
  }
  if (value instanceof ArrayBuffer) {
    const bytes = new Uint8Array(value);
    return { bytes, text: new TextDecoder().decode(bytes) };
  }
  if (ArrayBuffer.isView(value)) {
    const bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    return { bytes, text: new TextDecoder().decode(bytes) };
  }
  return { bytes: new Uint8Array(), text: "" };
}

function liveEndpoint(apiKey: string): string {
  const key = encodeURIComponent(apiKey);
  return `https://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${key}`;
}

async function geminiLiveTurn(text: string, profile: ProfileState, env: Env, modelName: string): Promise<{ data: Uint8Array; mime: string; rate: number }> {
  let response: Response;
  try {
    // Workers uses an HTTPS fetch with Upgrade for an outbound WebSocket. Do not
    // use generateContent here: *-live-* models only accept BidiGenerateContent.
    response = await fetch(liveEndpoint(env.GEMINI_API_KEY || ""), { headers: { Upgrade: "websocket" } });
  } catch (error) {
    const detail = clean(error instanceof Error ? error.message : error, 180);
    throw Object.assign(new Error(`Gemini Live model ${modelName} WebSocket connection failed${detail ? ` — ${detail}` : ""}`), { modelUnavailable: false });
  }
  if (!response.webSocket) {
    const raw = await response.text();
    const detail = remoteDetail(raw);
    throw Object.assign(new Error(`Gemini Live model ${modelName} WebSocket handshake returned HTTP ${response.status}${detail ? ` — ${detail}` : ""}`), { modelUnavailable: geminiModelUnavailable(raw) });
  }
  const socket = response.webSocket;
  socket.binaryType = "arraybuffer";
  try {
    socket.accept();
  } catch (error) {
    const detail = clean(error instanceof Error ? error.message : error, 180);
    throw Object.assign(new Error(`Gemini Live model ${modelName} WebSocket could not be accepted${detail ? ` — ${detail}` : ""}`), { modelUnavailable: false });
  }

  return new Promise((resolve, reject) => {
    const chunks: Uint8Array[] = [];
    let size = 0;
    let rate = 24000;
    let mime = "audio/pcm;rate=24000";
    let settled = false;
    let setupSent = false;
    let fallbackSent = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const close = () => {
      try {
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) socket.close();
      } catch {
        // The upstream may already have closed while the response is completing.
      }
    };
    const clearTimer = () => {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
    };
    const removeListeners = () => {
      socket.removeEventListener("message", onMessage);
      socket.removeEventListener("error", onError);
      socket.removeEventListener("close", onClose);
    };
    const output = () => {
      const data = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        data.set(chunk, offset);
        offset += chunk.length;
      }
      return { data, mime, rate };
    };
    const done = () => {
      if (settled) return;
      if (!size) {
        fail(`Gemini Live model ${modelName} returned no audio`, false);
        return;
      }
      settled = true;
      clearTimer();
      removeListeners();
      close();
      resolve(output());
    };
    const fail = (message: string, modelUnavailable = false) => {
      if (settled) return;
      settled = true;
      clearTimer();
      removeListeners();
      close();
      reject(Object.assign(new Error(message), { modelUnavailable }));
    };
    const addAudio = (data: Uint8Array, audioMime = "") => {
      if (!data.length) return;
      chunks.push(data);
      size += data.length;
      if (audioMime) {
        mime = audioMime;
        const foundRate = /rate[=:](\d{4,6})/i.exec(audioMime);
        if (foundRate) rate = Number(foundRate[1]);
      }
    };
    const armTurnTimer = () => {
      clearTimer();
      timer = setTimeout(() => {
        // Match the Python client: try realtimeInput text first, then use the
        // canonical clientContent turn if a backend does not complete that path.
        if (size) {
          done();
        } else if (!fallbackSent) {
          fallbackSent = true;
          try {
            socket.send(JSON.stringify({ clientContent: { turns: [{ role: "user", parts: [{ text }] }], turnComplete: true } }));
            armTurnTimer();
          } catch (error) {
            const detail = clean(error instanceof Error ? error.message : error, 160);
            fail(`Gemini Live model ${modelName} could not send the text turn${detail ? ` — ${detail}` : ""}`);
          }
        } else {
          fail(`Gemini Live model ${modelName} timed out waiting for audio`, false);
        }
      }, 12000);
    };
    const sendText = () => {
      if (setupSent || settled) return;
      setupSent = true;
      try {
        socket.send(JSON.stringify({
          setup: {
            model: `models/${modelName}`,
            systemInstruction: { parts: [{ text: "You are a warm, smooth, energetic radio DJ announcer on air. The text you are sent is the announcement to SAY OUT LOUD word for word: name the track, the reason it is playing and what is up next. Read it exactly as written. Do not ask the listener anything, do not add a question, and do not carry on a conversation - speak the script and stop." }] },
            generationConfig: {
              responseModalities: ["AUDIO"],
              speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: profile.ttsVoice || env.GEMINI_TTS_VOICE || "Despina" } } },
            },
          },
        }));
        timer = setTimeout(() => fail(`Gemini Live model ${modelName} timed out waiting for setupComplete`, false), 10000);
      } catch (error) {
        const detail = clean(error instanceof Error ? error.message : error, 160);
        fail(`Gemini Live model ${modelName} could not send setup${detail ? ` — ${detail}` : ""}`);
      }
    };
    const onError = (event: Event) => {
      const detail = clean((event as ErrorEvent).message, 180);
      fail(`Gemini Live model ${modelName} WebSocket error${detail ? ` — ${detail}` : ""}`, false);
    };
    const onClose = (event: Event) => {
      const closed = event as CloseEvent;
      if (size) done();
      else {
        const reason = clean(closed.reason, 180);
        const detail = `closed with code ${closed.code}${reason ? ` — ${reason}` : ""}`;
        fail(`Gemini Live model ${modelName} ${detail}`, geminiModelUnavailable(reason));
      }
    };
    const onMessage = (event: MessageEvent) => {
      void (async () => {
        const incoming = await liveSocketData(event.data);
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(incoming.text) as Record<string, unknown>;
        } catch {
          // The official protocol carries JSON in binary WebSocket frames. Keep
          // accepting a raw binary PCM frame as the original Python client does.
          if (incoming.bytes.length && typeof event.data !== "string") addAudio(incoming.bytes);
          return;
        }
        const remoteError = message.error;
        if (remoteError) {
          const detail = remoteDetail(JSON.stringify(remoteError), 260);
          fail(`Gemini Live model ${modelName} returned an error${detail ? ` — ${detail}` : ""}`, geminiModelUnavailable(detail));
          return;
        }
        if (message.setupComplete) {
          clearTimer();
          try {
            // The original implementation sends realtimeInput text and retains
            // clientContent as a compatibility backstop for Live API versions.
            socket.send(JSON.stringify({ realtimeInput: { text } }));
            armTurnTimer();
          } catch (error) {
            const detail = clean(error instanceof Error ? error.message : error, 160);
            fail(`Gemini Live model ${modelName} could not send the text turn${detail ? ` — ${detail}` : ""}`);
          }
          return;
        }
        const serverContent = (message.serverContent || {}) as Record<string, unknown>;
        const modelTurn = (serverContent.modelTurn || {}) as Record<string, unknown>;
        const parts = Array.isArray(modelTurn.parts) ? modelTurn.parts : [];
        for (const part of parts) {
          if (!part || typeof part !== "object") continue;
          const inline = (part as Record<string, unknown>).inlineData;
          if (!inline || typeof inline !== "object") continue;
          const audio = inline as Record<string, unknown>;
          if (typeof audio.data !== "string") continue;
          try {
            addAudio(base64Bytes(audio.data), typeof audio.mimeType === "string" ? audio.mimeType : "");
          } catch {
            fail(`Gemini Live model ${modelName} returned invalid base64 audio`, false);
            return;
          }
        }
        if (serverContent.interrupted) {
          if (size) done();
          else fail(`Gemini Live model ${modelName} interrupted the audio turn`, false);
          return;
        }
        if (serverContent.turnComplete) done();
      })().catch((error) => {
        const detail = clean(error instanceof Error ? error.message : error, 180);
        fail(`Gemini Live model ${modelName} message handling failed${detail ? ` — ${detail}` : ""}`, false);
      });
    };

    socket.addEventListener("message", onMessage);
    socket.addEventListener("error", onError);
    socket.addEventListener("close", onClose);
    if (socket.readyState === WebSocket.OPEN) sendText();
    else socket.addEventListener("open", sendText, { once: true });
  });
}

async function geminiLiveSpeech(text: string, profile: ProfileState, env: Env): Promise<Response> {
  const configured = clean(env.GEMINI_TTS_MODEL, 100) || GEMINI_LIVE_MODELS[0];
  const models = [...new Set([configured, ...GEMINI_LIVE_MODELS].filter(isGeminiLiveModel))];
  let lastError = "Gemini Live TTS did not return audio";
  for (const modelName of models) {
    try {
      const audio = await geminiLiveTurn(text, profile, env, modelName);
      const isPcm = audio.mime.toLocaleLowerCase().includes("pcm") || audio.mime.toLocaleLowerCase().includes("l16");
      const body = isPcm ? pcmWave(audio.data, audio.rate) : audio.data;
      return new Response(body.buffer as ArrayBuffer, { headers: { "content-type": isPcm ? "audio/wav" : audio.mime, "cache-control": "no-store" } });
    } catch (error) {
      lastError = error instanceof Error ? error.message : clean(error, 280);
      if (!(error as Error & { modelUnavailable?: boolean }).modelUnavailable) return errorResponse(lastError, 502);
    }
  }
  return errorResponse(lastError, 502);
}

async function geminiGenerateContentSpeech(text: string, profile: ProfileState, env: Env): Promise<Response> {
  const configured = clean(env.GEMINI_TTS_MODEL, 100);
  const models = [...new Set([configured, ...GEMINI_TTS_MODELS].filter(Boolean))];
  const findAudio = (value: unknown): { data: string; mime: string } | null => {
    if (Array.isArray(value)) { for (const item of value) { const found = findAudio(item); if (found) return found; } return null; }
    if (!value || typeof value !== "object") return null;
    const object = value as Record<string, unknown>;
    if (typeof object.data === "string" && typeof object.mimeType === "string" && object.mimeType.toLocaleLowerCase().includes("audio")) return { data: object.data, mime: object.mimeType };
    for (const child of Object.values(object)) { const found = findAudio(child); if (found) return found; }
    return null;
  };
  let lastError = "Gemini TTS did not return audio";
  for (const modelName of models) {
    const model = encodeURIComponent(modelName);
    const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY || "" },
      body: JSON.stringify({
        model: modelName,
        contents: [{ parts: [{ text }] }],
        generationConfig: { responseModalities: ["AUDIO"], speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: profile.ttsVoice || env.GEMINI_TTS_VOICE || "Despina" } } } },
      }),
    });
    const raw = await response.text();
    if (!response.ok) {
      lastError = `Gemini TTS model ${modelName} returned HTTP ${response.status}${remoteDetail(raw) ? ` — ${remoteDetail(raw)}` : ""}`;
      if (response.status === 404 || geminiModelUnavailable(raw)) continue;
      return errorResponse(lastError, 502);
    }
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return errorResponse(`Gemini TTS model ${modelName} returned invalid JSON`, 502);
    }
    const audio = findAudio(payload);
    if (!audio) {
      const finish = remoteDetail(JSON.stringify((payload.candidates as Array<Record<string, unknown>> | undefined)?.[0]?.finishReason || ""), 80);
      return errorResponse(`Gemini TTS returned no audio${finish ? ` (${finish})` : ""}`, 502);
    }
    const bytes = base64Bytes(audio.data);
    const sample = /(\d{4,6})/i.exec(audio.mime)?.[1];
    const isPcm = audio.mime.toLocaleLowerCase().includes("pcm") || audio.mime.toLocaleLowerCase().includes("l16");
    const body = isPcm ? pcmWave(bytes, Number(sample || 24000)) : bytes;
    return new Response(body.buffer as ArrayBuffer, { headers: { "content-type": isPcm ? "audio/wav" : audio.mime, "cache-control": "no-store" } });
  }
  return errorResponse(lastError, 502);
}

async function geminiSpeech(text: string, profile: ProfileState, env: Env): Promise<Response> {
  if (!env.GEMINI_API_KEY) return errorResponse("Gemini TTS is not configured", 503);
  const model = clean(env.GEMINI_TTS_MODEL, 100) || "gemini-3.1-flash-live-preview";
  return isGeminiLiveModel(model) ? geminiLiveSpeech(text, profile, env) : geminiGenerateContentSpeech(text, profile, env);
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
    body: JSON.stringify({
      model: env.TTS_MODEL || "gpt-4o-mini-tts",
      voice: profile.ttsVoice || env.TTS_VOICE || "coral",
      input: text,
      response_format: "mp3",
      instructions: "Perform as a warm, playful, expressive late-night radio DJ. Use lively intonation, varied pacing, natural pauses, and a little delighted surprise. Do not sound like a flat newsreader.",
    }),
  });
  if (!response.ok) return errorResponse(`TTS provider returned HTTP ${response.status}`, 502);
  return new Response(response.body, { status: 200, headers: { "content-type": response.headers.get("content-type") || "audio/mpeg", "cache-control": "no-store" } });
}

async function tts(request: Request, env: Env, user: User): Promise<Response> {
  const fields = await bodyFields(request);
  const profile = await loadProfile(env, user.id);
  const text = clean(fields.text, 1500);
  if (!text) return errorResponse("text is required");
  // Normal announcements are deliberately profile-authoritative. Provider/voice
  // fields from the browser are ignored so a stale tab, room state, or crafted
  // request cannot silently change the user's saved voice. Settings preview uses
  // the same saved profile after the user presses Save.
  if (!profile.voiceEnabled) return json({ ok: true, provider: "off", browser: false, suppressed: true });
  const provider = profile.ttsProvider;
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

type RoomConnection = { member: RoomMember; language: string };

export class ListenRoom {
  private readonly state: DurableObjectState;
  private room: RoomState | null = null;
  private readonly sockets = new Map<WebSocket, RoomConnection>();

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

  private snapshot(language = "English"): Record<string, unknown> {
    return this.room ? publicRoom(this.room, language) : {};
  }

  private broadcast(): void {
    for (const [socket, connection] of this.sockets.entries()) {
      try { socket.send(JSON.stringify({ type: "state", state: this.snapshot(connection.language) })); } catch { this.sockets.delete(socket); }
    }
  }

  private async roomAction(request: Request, fields: Record<string, string>, member: RoomMember): Promise<Response> {
    const room = await this.load();
    if (!room.members[member.id]) return errorResponse("not a member", 403);
    const action = clean(fields.action, 50).toLocaleLowerCase();
    if (action === "autoplay_next" && clean(fields.baseId, 32) && room.now?.id !== clean(fields.baseId, 32)) {
      return json({ ok: true, state: this.snapshot(request.headers.get("x-spotube-dj-language") || "English"), message: "another listener advanced the room" });
    }
    if (action === "request" || action === "mix" || action === "radio" || action === "autoplay_next") {
      let tracks: Track[] = [];
      try {
        tracks = (JSON.parse(fields.tracks || "[]") as unknown[])
          .map(normalizeTrack)
          .filter((track): track is Track => track !== null && playableOriginal(track))
          .map((track) => requesterTrack(track, member));
      } catch { tracks = []; }
      if (!tracks.length) return errorResponse("the room mix has no original songs", 502);
      const replacingMix = action === "request" || action === "mix" || action === "radio";
      if (replacingMix) {
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
        room.segment = segmentOf(fields.segment, room.request, room.vibe);
        room.engine = clean(fields.engine, 80) || "offline parser";
        room.djLead = clean(fields.djLead, 280) || creativeDjLead(room.request, room.vibe);
        room.message = fields.brainError
          ? `AI brain unavailable; used the offline parser. ${clean(fields.brainError, 180)}`
          : `Ready with ${room.queue.length + (room.now ? 1 : 0)} tracks. Press play.`;
      } else {
        // The Worker has already chosen a new, filtered track from the blended
        // room context. Advance exactly once; never call the normal `next` path
        // from here, otherwise an empty queue can recurse forever.
        room.request = clean(fields.q, 240) || room.request;
        if (room.now) {
          room.history.push(clone(room.now));
          room.history = room.history.slice(-40);
        }
        room.now = tracks.shift() || null;
        room.queue = tracks.slice(0, MAX_QUEUE);
        room.position = 0;
        room.duration = room.now?.duration || 0;
        room.playing = Boolean(room.now);
        room.vibe = clean(fields.vibe, 120) || room.vibe;
        room.why = clean(fields.why, 300) || room.why;
        room.segment = segmentOf(fields.segment, room.request, room.vibe);
        room.engine = clean(fields.engine, 80) || room.engine;
        room.djLead = clean(fields.djLead, 280) || creativeDjLead(room.request, room.vibe);
        room.message = fields.brainError
          ? `AI brain unavailable; used the offline parser. ${clean(fields.brainError, 180)}`
          : room.now ? `Playing a similar song: ${room.now.title}` : "The similar-song queue was empty.";
      }
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
          room.djLead = creativeDjLead(room.request, room.vibe);
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
        room.djLead = creativeDjLead(room.request, room.vibe);
        room.message = next ? `playing ${next.title}` : fields.autoplayError
          ? `Autoplay fallback unavailable; ${clean(fields.autoplayError, 180)}`
          : "The room queue is empty. Make another blended mix.";
      }
    } else if (action === "play_row") {
      const queued = room.queue.find((track) => track.id === clean(fields.id, 32)) || null;
      let supplied: Track | null = null;
      try { supplied = normalizeTrack(JSON.parse(fields.track || "null")); } catch { /* invalid client row */ }
      const selected = queued || (supplied ? requesterTrack(supplied, member) : null);
      if (!selected || !playableOriginal(selected)) return errorResponse("that track is not an original YouTube Music song");
      room.queue = room.queue.filter((track) => track.id !== selected?.id);
      if (room.now && room.now.id !== selected.id) {
        room.history.push(clone(room.now));
        room.history = room.history.slice(-40);
      }
      room.now = selected;
      room.position = 0;
      room.duration = selected.duration;
      room.playing = true;
      room.djLead = creativeDjLead(room.request, room.vibe);
      room.message = `playing ${selected.title}`;
    } else if (action === "enqueue" || action === "queue_next") {
      let track: Track | null = null;
      try { track = normalizeTrack(JSON.parse(fields.track || "null")); } catch { track = null; }
      if (!track || !playableOriginal(track)) return errorResponse("only original YouTube Music songs can be queued");
      track = requesterTrack(track, member);
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
    } else if (action === "autoplay") {
      room.autoplay = fields.on !== "off" && fields.on !== "0" && fields.on !== "false";
    } else if (action === "leave") {
      delete room.members[member.id];
      for (const [socket, connected] of this.sockets.entries()) {
        if (connected.member.id !== member.id) continue;
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
    return json({ ok: true, state: this.snapshot(request.headers.get("x-spotube-dj-language") || "English"), message: room.message });
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
      return json({ ok: true, state: this.snapshot(request.headers.get("x-spotube-dj-language") || "English") });
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
      return json({ ok: true, state: this.snapshot(request.headers.get("x-spotube-dj-language") || "English") });
    }
    if (url.pathname === "/members" && request.method === "GET") return json({ members: Object.keys(room.members) });
    if (url.pathname === "/state" && request.method === "GET") {
      if (!this.allowed(request, room)) return errorResponse("not a member", 403);
      return json(this.snapshot(request.headers.get("x-spotube-dj-language") || "English"));
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
      const language = LANGUAGES.find((item) => item.toLocaleLowerCase() === (request.headers.get("x-spotube-dj-language") || "").toLocaleLowerCase()) || "English";
      this.sockets.set(server, { member, language });
      server.addEventListener("close", () => this.sockets.delete(server));
      server.addEventListener("error", () => this.sockets.delete(server));
      server.addEventListener("message", (event) => {
        void (async () => {
          try {
            const raw = typeof event.data === "string" ? event.data : new TextDecoder().decode(event.data as ArrayBuffer);
            const value = JSON.parse(raw) as unknown;
            if (!value || typeof value !== "object") throw new Error("message must be an object");
            const fields = Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, typeof item === "string" ? item : JSON.stringify(item)]));
            const response = await this.roomAction(new Request("https://room.internal/action", { method: "POST", headers: { "x-spotube-dj-language": language } }), fields, member);
            const payload = await response.text();
            server.send(JSON.stringify({ type: "ack", ok: response.ok, ...(JSON.parse(payload) as Record<string, unknown>) }));
          } catch (error) {
            try { server.send(JSON.stringify({ type: "error", error: error instanceof Error ? error.message : "invalid room message" })); } catch { this.sockets.delete(server); }
          }
        })();
      });
      server.send(JSON.stringify({ type: "state", state: this.snapshot(language) }));
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
        if (path === "/api/brain-test" && request.method === "POST") return testBrain(env, user);
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
