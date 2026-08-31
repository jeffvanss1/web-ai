export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  USER_QUEUE: DurableObjectNamespace;
  AUDIO_RESOLVER_URL?: string;
  YOUTUBE_API_KEY?: string;
  ALLOWED_ORIGIN?: string;
  AUTH_MODE?: string;
}

type Track = { id: string; title: string; artist: string; duration: number; thumbnail: string; source: string };
type User = { id: string; email?: string };

const json = (data: unknown, status = 200, headers: HeadersInit = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers } });
const now = () => Math.floor(Date.now() / 1000);

function userFrom(request: Request, env: Env): User | Response {
  // Cloudflare Access authenticates before the Worker and supplies this identity.
  const email = request.headers.get("cf-access-authenticated-user-email");
  const subject = request.headers.get("cf-access-jwt-assertion");
  if (email) return { id: email.toLowerCase(), email };
  // Local development only. Never enable this mode on a public deployment.
  if (env.AUTH_MODE === "dev") return { id: request.headers.get("x-dev-user") || "local-dev" };
  return json({ error: "authentication required" }, 401);
}

async function readBody(request: Request): Promise<Record<string, any>> {
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) return await request.json();
  const form = await request.formData();
  const out: Record<string, any> = {};
  form.forEach((value, key) => { out[key] = value; });
  return out;
}

function cors(request: Request, env: Env): Headers {
  const origin = request.headers.get("origin");
  // Never reflect arbitrary origins in production. Local dev may use the origin
  // because Wrangler serves the asset and API from the same dev process.
  const allowed = env.ALLOWED_ORIGIN || (env.AUTH_MODE === "dev" ? origin : "");
  const h = new Headers({ "access-control-allow-credentials": "true", "access-control-allow-headers": "content-type", "access-control-allow-methods": "GET,POST,OPTIONS" });
  if (allowed) h.set("access-control-allow-origin", allowed);
  return h;
}

function durationSeconds(value: string): number {
  const match = /^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/.exec(value || "");
  if (!match) return 0;
  return Number(match[1] || 0) * 3600 + Number(match[2] || 0) * 60 + Number(match[3] || 0);
}

async function search(env: Env, query: string): Promise<Track[]> {
  const key = env.YOUTUBE_API_KEY;
  if (!key) throw new Error("YOUTUBE_API_KEY is not configured");
  const params = new URLSearchParams({ key, part: "snippet", q: query, type: "video", videoCategoryId: "10", maxResults: "20", safeSearch: "none" });
  const response = await fetch(`https://www.googleapis.com/youtube/v3/search?${params}`);
  if (!response.ok) throw new Error(`YouTube API search returned ${response.status}`);
  const body: any = await response.json();
  const ids = (body.items || []).map((item: any) => item.id?.videoId).filter(Boolean);
  if (!ids.length) return [];
  const details = new URLSearchParams({ key, part: "contentDetails", id: ids.join(",") });
  const detailResponse = await fetch(`https://www.googleapis.com/youtube/v3/videos?${details}`);
  if (!detailResponse.ok) throw new Error(`YouTube API details returned ${detailResponse.status}`);
  const durations = new Map<string, number>((((await detailResponse.json()) as any).items || []).map((item: any) => [item.id, durationSeconds(item.contentDetails?.duration)]));
  return (body.items || []).map((item: any): Track => ({
    id: item.id.videoId,
    title: item.snippet?.title || "Untitled",
    artist: item.snippet?.channelTitle || "",
    duration: durations.get(item.id.videoId) || 0,
    thumbnail: item.snippet?.thumbnails?.high?.url || item.snippet?.thumbnails?.medium?.url || `https://i.ytimg.com/vi/${item.id.videoId}/hqdefault.jpg`,
    source: "youtube"
  }));
}

async function saveTracks(env: Env, tracks: Track[]): Promise<void> {
  if (!tracks.length) return;
  const statements = tracks.map(t => env.DB.prepare("INSERT INTO tracks (id,title,artist,duration,thumbnail,source,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, artist=excluded.artist, thumbnail=excluded.thumbnail").bind(t.id, t.title, t.artist, t.duration, t.thumbnail, t.source, now()));
  await env.DB.batch(statements);
}

async function state(env: Env, user: User) {
  const queue = await env.DB.prepare("SELECT q.position, t.* FROM queue q JOIN tracks t ON t.id=q.track_id WHERE q.user_id=? ORDER BY q.position").bind(user.id).all<Track & { position: number }>();
  const liked = await env.DB.prepare("SELECT t.* FROM likes l JOIN tracks t ON t.id=l.track_id WHERE l.user_id=? ORDER BY l.created_at DESC LIMIT 100").bind(user.id).all<Track>();
  return { user: { id: user.id, email: user.email || null }, current: null, queue: queue.results, liked: liked.results, playing: false };
}

async function action(request: Request, env: Env, user: User) {
  const body = await readBody(request); const name = String(body.action || "");
  if (name === "queue") {
    const track = body.track as Track; if (!track?.id || !track.title) return json({ error: "track is required" }, 400);
    await saveTracks(env, [track]);
    const last = await env.DB.prepare("SELECT COALESCE(MAX(position), -1) AS position FROM queue WHERE user_id=?").bind(user.id).first<{ position: number }>();
    await env.DB.prepare("INSERT INTO queue (user_id,position,track_id,added_at) VALUES (?,?,?,?)").bind(user.id, (last?.position ?? -1) + 1, track.id, now()).run();
  } else if (name === "remove") {
    await env.DB.prepare("DELETE FROM queue WHERE user_id=? AND position=?").bind(user.id, Number(body.position)).run();
  } else if (name === "like" || name === "unlike") {
    const id = String(body.track_id || "");
    if (name === "like") await env.DB.prepare("INSERT OR IGNORE INTO likes (user_id,track_id,created_at) VALUES (?,?,?)").bind(user.id, id, now()).run();
    else await env.DB.prepare("DELETE FROM likes WHERE user_id=? AND track_id=?").bind(user.id, id).run();
  } else if (name === "played") {
    await env.DB.prepare("INSERT INTO history (user_id,track_id,played_at) VALUES (?,?,?)").bind(user.id, String(body.track_id), now()).run();
  } else if (name === "clear") {
    await env.DB.prepare("DELETE FROM queue WHERE user_id=?").bind(user.id).run();
  } else return json({ error: `unknown action: ${name}` }, 400);
  return json(await state(env, user));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(request, env) });
    if (url.pathname.startsWith("/api/")) {
      const auth = userFrom(request, env); if (auth instanceof Response) return auth;
      try {
        let result: Response;
        if (url.pathname === "/api/state" && request.method === "GET") result = json(await state(env, auth));
        else if (url.pathname === "/api/search" && request.method === "GET") result = json({ rows: await search(env, url.searchParams.get("q") || "") });
        else if (url.pathname === "/api/action" && request.method === "POST") result = await action(request, env, auth);
        else if (url.pathname === "/api/resolve" && request.method === "POST") {
          if (!env.AUDIO_RESOLVER_URL) result = json({ error: "audio resolver is not configured" }, 503);
          else { const body = await readBody(request); const upstream = await fetch(env.AUDIO_RESOLVER_URL, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id: body.id }) }); result = new Response(upstream.body, { status: upstream.status, headers: { "content-type": "application/json", "cache-control": "private, max-age=30" } }); }
        } else result = json({ error: "not found" }, 404);
        const crossOrigin = cors(request, env);
        crossOrigin.forEach((value, key) => result.headers.set(key, value));
        return result;
      } catch (error) { return json({ error: error instanceof Error ? error.message : "request failed" }, 502, cors(request, env)); }
    }
    return env.ASSETS.fetch(request);
  }
};

// Reserved for per-user real-time coordination in the next migration. Keeping the
// class in the Worker now makes the queue boundary explicit and avoids global state.
export class UserQueue { constructor(private state: DurableObjectState, private env: Env) {} async fetch() { return new Response("ok"); } }
