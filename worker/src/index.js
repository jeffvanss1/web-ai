/**
 * spotube-dj worker - the only path this app has to Gemini.
 *
 * Why it exists: the desktop app is a local Python process on a home network.
 * Talking straight to `generativelanguage.googleapis.com` from it means (a) the
 * API key lives on every machine that runs the player, (b) a retired model name
 * is a broken install until the user edits a config file, and (c) there is
 * nowhere for anything to be remembered between machines. One small Worker
 * fixes all three: the key is a `wrangler secret`, the model ladder is walked
 * server-side so a retirement costs one extra request instead of an outage, and
 * D1/R2 give the taste profile somewhere to live that is not a laptop.
 *
 * Routes (all under /v1, all JSON unless noted):
 *
 *   GET  /v1/health     what this Worker can do: key present, models, D1/R2 bound
 *   POST /v1/plan       -> {"queries": [...], "avoid": [...], "why": "..."}
 *   POST /v1/text       -> {"text": "..."}          one-shot creative text
 *   POST /v1/speech     -> audio/wav bytes          Gemini TTS, R2-cached
 *   GET  /v1/state      -> the taste snapshot for a profile (D1)
 *   PUT  /v1/state      <- upsert that snapshot
 *   POST /v1/events     <- append taste events (like/skip/dislike/play)
 *   GET  /v1/events     -> pull them since an id, for another machine
 *
 * Auth is two separate things and they are not interchangeable:
 *
 *   GEMINI_API_KEY   the Google key. A `wrangler secret`. If it is not set, a
 *                    client may send its own (`x-gemini-key` header or `key` in
 *                    the body) and this Worker will relay it unchanged. The
 *                    secret always wins when both are present.
 *   WORKER_TOKEN     a shared secret for THIS Worker, optional. When it is set
 *                    every /v1 route needs `Authorization: Bearer <token>` (or
 *                    `x-worker-token`). Set it: the D1 state routes are
 *                    otherwise writable by anyone who finds the URL.
 *
 * No npm dependencies, no build step: this file is the Worker.
 */

const VERSION = "1.0.0";

// Same ladder brain.py used to walk client-side. Ordered: the configured model
// first (from the request), then these, newest-cheapest first.
const DEFAULT_MODELS = [
  "gemini-3.5-flash",
  "gemini-3.1-flash-lite",
  "gemini-3.6-flash",
  "gemini-3.7-flash",
];

const DEFAULT_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta";
const DEFAULT_PLAN_MODEL = "gemini-3.5-flash";
const DEFAULT_TTS_MODEL = "gemini-3.1-flash-tts-preview";

// Spoken lines are short and get repeated a lot (the same "up next" phrasing,
// the same greeting). An R2 hit costs no quota, which is the whole reason the
// free tier used to 429 mid-set.
const CLIP_CACHE_TTL_NOTE = "audio is cached by sha256(text|voice|model)";

const PLAN_SCHEMA = {
  type: "object",
  properties: {
    queries: {
      type: "array",
      description: "5-8 YouTube Music search strings",
      items: { type: "string" },
    },
    avoid: {
      type: "array",
      description: "Words that mean 'not this'",
      items: { type: "string" },
    },
    why: { type: "string", description: "One short sentence on the reasoning" },
  },
  required: ["queries"],
};

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
  "Access-Control-Allow-Headers":
    "Content-Type, Authorization, X-Worker-Token, X-Gemini-Key",
  "Access-Control-Max-Age": "86400",
};

// --------------------------------------------------------------------- helpers

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...CORS_HEADERS,
      ...extra,
    },
  });
}

function fail(status, kind, detail, extra = {}) {
  return json({ ok: false, error: { kind, detail: String(detail || ""), ...extra } }, status);
}

/**
 * Constant-time compare. A token check that leaks length is a token check with
 * a head start; both strings are digested first so the timing does not depend
 * on where the first differing byte is.
 */
async function tokenMatches(got, want) {
  if (!got) return false;
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(got)),
    crypto.subtle.digest("SHA-256", enc.encode(want)),
  ]);
  const x = new Uint8Array(a);
  const y = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

async function authorized(request, env) {
  const want = env.WORKER_TOKEN;
  if (!want) return true; // deliberately open: the operator chose no token
  const auth = request.headers.get("authorization") || "";
  const bearer = /^bearer\s+/i.test(auth) ? auth.replace(/^bearer\s+/i, "").trim() : "";
  const got = bearer || (request.headers.get("x-worker-token") || "").trim();
  return tokenMatches(got, want);
}

function pickKey(request, body, env) {
  const bodyKey = body && typeof body.key === "string" ? body.key.trim() : "";
  const headerKey = (request.headers.get("x-gemini-key") || "").trim();
  if (env.GEMINI_API_KEY) return { key: env.GEMINI_API_KEY, source: "secret" };
  if (headerKey) return { key: headerKey, source: "header" };
  if (bodyKey) return { key: bodyKey, source: "body" };
  return { key: "", source: "" };
}

function geminiBase(env) {
  return String(env.GEMINI_URL || DEFAULT_GEMINI_URL).replace(/\/+$/, "");
}

function cleanModel(name) {
  return String(name || "").trim().split("/").pop().trim();
}

/**
 * Put the model Google named at the front of the queue, ahead of the ladder.
 *
 * The obvious version - "if the hint is already in the list, carry on" - walks
 * the ladder anyway and lands on a model the API never suggested, which is the
 * bug that cost four extra requests before finding the right one. The error is
 * authoritative for this account and key, so the hint goes next, always.
 */
function preferModel(models, index, hint) {
  const clean = cleanModel(hint);
  if (!clean || clean === models[index]) return models;
  const head = models.slice(0, index + 1);
  const tail = models.slice(index + 1).filter((m) => m !== clean);
  return head.concat([clean], tail);
}

function modelList(requested, env) {
  const asked = [cleanModel(requested)];
  const fromEnv = cleanModel(env.GEMINI_MODEL);
  if (fromEnv) asked.push(fromEnv);
  const ladder = String(env.GEMINI_MODEL_LADDER || "")
    .split(",")
    .map(cleanModel)
    .filter(Boolean);
  const pool = ladder.length ? ladder : DEFAULT_MODELS;
  const out = [];
  for (const m of [...asked, ...pool, DEFAULT_PLAN_MODEL]) {
    if (m && !out.includes(m)) out.push(m);
  }
  return out;
}

/**
 * Map Google's error envelope onto the same six kinds brain.py has always used,
 * so the Python side keeps its wording and its "is this worth a retry?" answer.
 */
function classify(status, detail, httpStatus) {
  const low = `${status} ${detail}`.toLowerCase();
  const st = String(status || "").toUpperCase();
  const keyPhrases = [
    "api key not valid",
    "invalid api key",
    "api key expired",
    "api key not provided",
    "invalid authentication",
    "request had no authentication",
  ];
  if (keyPhrases.some((w) => low.includes(w)) || st === "API_KEY_INVALID" || st === "UNAUTHENTICATED") {
    return "key";
  }
  if (
    st === "NOT_FOUND" ||
    httpStatus === 404 ||
    low.includes("no longer available") ||
    low.includes("not found for this project") ||
    low.includes("model is not found")
  ) {
    return "model";
  }
  if (st === "PERMISSION_DENIED" || httpStatus === 403 || low.includes("permission") || low.includes("access")) {
    return "access";
  }
  if (st === "RESOURCE_EXHAUSTED" || httpStatus === 429 || low.includes("quota") || low.includes("rate limit")) {
    return "quota";
  }
  if (st === "INVALID_ARGUMENT" || st === "FAILED_PRECONDITION" || httpStatus === 400) {
    return "payload";
  }
  return "other";
}

/**
 * Google's retirement notice names the replacement:
 *   '...gemini-2.0-flash is no longer available. Please update your code to use
 *    models/gemini-3.6-flash...'  ->  gemini-3.6-flash
 */
function suggestedModel(text) {
  const t = String(text || "");
  const m = t.match(/use\s+models\/(gemini-[A-Za-z0-9._-]+)/);
  if (m) return cleanModel(m[1]);
  const seen = t.match(/gemini-[A-Za-z0-9._-]+/g) || [];
  return seen.length > 1 ? cleanModel(seen[seen.length - 1]) : "";
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * One generateContent call. Never throws: returns {ok, data} or
 * {ok:false, kind, status, detail, http}.
 */
async function generate(env, key, model, payload, timeoutMs) {
  const url = `${geminiBase(env)}/models/${model}:generateContent`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": key },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    const text = await res.text();
    let data = null;
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
    if (!res.ok) {
      const err = (data && data.error) || {};
      const status = String(err.status || "");
      const detail = String(err.message || text || "").slice(0, 500);
      return {
        ok: false,
        http: res.status,
        kind: classify(status, detail, res.status),
        status,
        detail,
      };
    }
    return { ok: true, data: data || {} };
  } catch (e) {
    const msg = String((e && e.message) || e);
    const timedOut = /abort/i.test(msg) || e instanceof DOMException;
    return {
      ok: false,
      http: 0,
      kind: timedOut ? "timeout" : "network",
      status: "",
      detail: timedOut ? `timed out after ${Math.round(timeoutMs / 1000)}s` : msg.slice(0, 300),
    };
  } finally {
    clearTimeout(timer);
  }
}

function candidateText(data) {
  try {
    const cand = (data.candidates || [])[0] || {};
    const parts = ((cand.content || {}).parts) || [];
    const chunks = parts
      .filter((p) => p && typeof p.text === "string")
      .map((p) => p.text)
      .filter(Boolean);
    if (chunks.length) return { text: chunks.join("\n").trim(), finish: String(cand.finishReason || "") };
    if (cand.content && typeof cand.content.text === "string" && cand.content.text.trim()) {
      return { text: cand.content.text.trim(), finish: String(cand.finishReason || "") };
    }
    const blocked = (data.promptFeedback || {}).blockReason;
    return { text: "", finish: blocked ? `blocked:${blocked}` : "empty" };
  } catch {
    return { text: "", finish: "empty" };
  }
}

/**
 * Accept a bare object, a fenced block, prose around it, a bare list of query
 * strings (models do that constantly), or a reply cut off by maxOutputTokens.
 * Ported from brain._extract_json so the salvage rules cannot drift apart.
 */
function extractJson(text, lenient = true) {
  let t = String(text || "").trim();
  if (!t) return null;
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) t = fence[1].trim();
  for (const [open, close] of [["{", "}"], ["[", "]"]]) {
    const start = t.indexOf(open);
    const end = t.lastIndexOf(close);
    if (start === -1 || end <= start) continue;
    try {
      const parsed = JSON.parse(t.slice(start, end + 1));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
      if (Array.isArray(parsed) && parsed.length) return { queries: parsed.map((x) => String(x)) };
    } catch {
      /* fall through to the salvage pass */
    }
  }
  if (!lenient) return null;
  const m = t.match(/"queries"\s*:\s*\[([\s\S]*)/);
  if (m) {
    let region = m[1];
    const close = region.indexOf("]");
    if (close !== -1) region = region.slice(0, close); // never reach past the array,
    const items = []; // or "avoid"/"why" become search strings
    for (const mt of region.matchAll(/"([^"]{3,})"(\s*:?)/g)) {
      if (String(mt[2] || "").trim()) continue; // that token is a key, not a value
      items.push(String(mt[1]).trim());
    }
    const kept = items.filter((i) => i && !i.startsWith("http")).slice(0, 8);
    if (kept.length) return { queries: kept, why: "recovered from a truncated reply", truncated: true };
  }
  return null;
}

// ------------------------------------------------------------------ /v1/plan

function planShapes(prompt, system) {
  const contents = [
    { role: "user", parts: [{ text: `${system || ""}\n\n${prompt}` }] },
  ];
  return [
    [
      "responseFormat",
      {
        contents,
        generationConfig: {
          maxOutputTokens: 8192,
          responseFormat: { text: { mimeType: "application/json", schema: PLAN_SCHEMA } },
        },
      },
    ],
    [
      "responseMimeType",
      {
        contents,
        generationConfig: {
          maxOutputTokens: 8192,
          responseMimeType: "application/json",
          responseSchema: PLAN_SCHEMA,
        },
      },
    ],
    ["plain", { contents }],
  ];
}

/**
 * Ask for a query plan. Walks the model ladder and the payload shapes the way
 * brain.py did, but server-side, so a retired model costs one extra request
 * here instead of a broken install on every machine.
 */
async function handlePlan(request, env, body) {
  const { key, source } = pickKey(request, body, env);
  if (!key) {
    return fail(400, "key", "no Gemini API key: set the GEMINI_API_KEY secret, or send x-gemini-key");
  }
  const prompt = String(body.prompt || "").trim();
  if (!prompt) return fail(400, "payload", "prompt is required");

  const timeoutMs = Math.max(5, Math.min(Number(body.timeoutMs) || 45, 120)) * 1000;
  let models = modelList(body.model, env);
  const shapes = planShapes(prompt, body.system);
  const notes = [];
  const deadline = Date.now() + timeoutMs * (1 + 2);
  let shapeIndex = 0;
  let calls = 0;
  let last = null;

  for (let mi = 0; mi < models.length && calls < 6; mi++) {
    const model = models[mi];
    for (let attempt = 0; attempt < 3; attempt++) {
      if (Date.now() > deadline) {
        return fail(
          504,
          "timeout",
          `gave up after ${calls} attempt(s) - slower than the ${Math.round(timeoutMs / 1000)}s budget`,
          { model, notes },
        );
      }
      const [shapeName, payload] = shapes[shapeIndex];
      calls += 1;
      const res = await generate(env, key, model, payload, timeoutMs);
      if (res.ok) {
        const { text, finish } = candidateText(res.data);
        if (!text) {
          last = { kind: "empty", detail: `${model} returned no text (${finish || "unknown"})`, model };
          break; // reachable and authenticated: nothing to switch
        }
        const parsed = extractJson(text, false) || extractJson(text);
        if (!parsed) {
          const cut = String(finish).toUpperCase() === "MAX_TOKENS" ? " - answer was truncated mid-JSON" : "";
          last = {
            kind: "parse",
            detail: `could not parse ${model}'s output${cut}: ${JSON.stringify(text.slice(0, 120))}`,
            model,
          };
          break;
        }
        if (shapeIndex !== 0) notes.push(`this endpoint wants the '${shapeName}' response config`);
        return json({ ok: true, plan: parsed, model, source, notes, calls });
      }
      last = { kind: res.kind, detail: res.detail, status: res.status, model, http: res.http };
      if (res.kind === "model") {
        const hint = suggestedModel(res.detail);
        const before = models[mi + 1];
        models = preferModel(models, mi, hint);
        if (hint && models[mi + 1] === hint && before !== hint) {
          notes.push(`${model} is retired - the API says to use ${hint}, trying that`);
        } else {
          notes.push(`the API has no model ${model}, trying the next one on the list`);
        }
        break; // next model
      }
      if (res.kind === "payload" && shapeIndex + 1 < shapes.length) {
        shapeIndex += 1;
        notes.push(`${model} rejected the '${shapeName}' config, trying the '${shapes[shapeIndex][0]}' one`);
        continue; // same model, next shape
      }
      if (res.kind === "quota" || res.kind === "network" || res.kind === "timeout" || res.http >= 500) {
        if (attempt < 2 && Date.now() < deadline) {
          await sleep(1500 * (attempt + 1));
          continue;
        }
      }
      break; // bad key / no access / out of retries: a retry is pointless
    }
  }
  const status = last && (last.kind === "key" || last.kind === "access") ? 401 : last && last.kind === "quota" ? 429 : 502;
  return fail(status, (last && last.kind) || "other", (last && last.detail) || "no attempt made", {
    model: last && last.model,
    status: last && last.status,
    notes,
  });
}

// ------------------------------------------------------------------ /v1/text

async function handleText(request, env, body) {
  const { key, source } = pickKey(request, body, env);
  if (!key) {
    return fail(400, "key", "no Gemini API key: set the GEMINI_API_KEY secret, or send x-gemini-key");
  }
  const prompt = String(body.prompt || "").trim();
  if (!prompt) return fail(400, "payload", "prompt is required");

  const timeoutMs = Math.max(5, Math.min(Number(body.timeoutMs) || 45, 120)) * 1000;
  const maxChars = Math.max(1, Math.min(Number(body.maxChars) || 600, 8000));
  const models = modelList(body.model, env);
  const payload = {
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: {
      temperature: Number.isFinite(Number(body.temperature)) ? Number(body.temperature) : 0.9,
      maxOutputTokens: Math.max(64, Math.min(Number(body.maxOutputTokens) || 400, 8192)),
    },
  };
  if (body.system) {
    payload.systemInstruction = { parts: [{ text: String(body.system) }] };
  }

  const notes = [];
  let last = null;
  for (let mi = 0; mi < models.length; mi++) {
    const model = models[mi];
    const res = await generate(env, key, model, payload, timeoutMs);
    if (res.ok) {
      const { text } = candidateText(res.data);
      const joined = text.replace(/\s+/g, " ").trim();
      if (joined) return json({ ok: true, text: joined.slice(0, maxChars), model, source, notes });
      last = { kind: "empty", detail: `${model} returned no text`, model };
      continue;
    }
    last = { kind: res.kind, detail: res.detail, status: res.status, model };
    if (res.kind === "model") {
      const hint = suggestedModel(res.detail);
      const before = models[mi + 1];
      models = preferModel(models, mi, hint);
      if (hint && models[mi + 1] === hint && before !== hint) {
        notes.push(`${model} is retired - the API says to use ${hint}, trying that`);
      } else {
        notes.push(`${model} is unavailable, trying ${models[mi + 1] || "the next one"}`);
      }
      continue;
    }
    if (res.kind === "quota" || res.kind === "network" || res.kind === "timeout" || res.http >= 500) {
      if (mi < models.length - 1) {
        await sleep(1200);
        continue;
      }
    }
    break;
  }
  const status = last && (last.kind === "key" || last.kind === "access") ? 401 : last && last.kind === "quota" ? 429 : 502;
  return fail(status, (last && last.kind) || "other", (last && last.detail) || "no attempt made", {
    model: last && last.model,
    status: last && last.status,
    notes,
  });
}

// ---------------------------------------------------------------- /v1/speech

function wavFromPcm(pcm, rate) {
  const header = new ArrayBuffer(44);
  const dv = new DataView(header);
  const tag = (off, s) => {
    for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i));
  };
  tag(0, "RIFF");
  dv.setUint32(4, 36 + pcm.length, true);
  tag(8, "WAVE");
  tag(12, "fmt ");
  dv.setUint32(16, 16, true); // PCM chunk size
  dv.setUint16(20, 1, true); // format = PCM
  dv.setUint16(22, 1, true); // channels = mono
  dv.setUint32(24, rate, true);
  dv.setUint32(28, rate * 2, true); // byte rate: mono, 16-bit
  dv.setUint16(32, 2, true); // block align
  dv.setUint16(34, 16, true); // bits per sample
  tag(36, "data");
  dv.setUint32(40, pcm.length, true);
  const out = new Uint8Array(44 + pcm.length);
  out.set(new Uint8Array(header), 0);
  out.set(pcm, 44);
  return out;
}

function base64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToBase64(bytes) {
  let s = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(s);
}

async function cacheKey(parts) {
  const enc = new TextEncoder().encode(parts.join("|"));
  const digest = await crypto.subtle.digest("SHA-256", enc);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

/**
 * The Live API speaks over a WebSocket, which a Worker cannot open as a client.
 * A `*-live-*` model name is therefore mapped onto its REST TTS sibling - the
 * same family, the same voices, reachable with fetch(). Quota, which is why the
 * live path existed at all, is answered by the R2 clip cache instead.
 */
function ttsModelName(requested, env) {
  let m = cleanModel(requested) || cleanModel(env.GEMINI_TTS_MODEL) || DEFAULT_TTS_MODEL;
  if (/-live/.test(m) || /live-preview$/.test(m)) m = m.replace(/-live-/, "-tts-").replace(/-live$/, "-tts");
  if (!/-tts/.test(m)) m = `${m}-tts-preview`;
  return m;
}

async function synthesize(env, key, model, text, voice) {
  const payload = {
    contents: [{ role: "user", parts: [{ text }] }],
    generationConfig: {
      responseModalities: ["AUDIO"],
      speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: voice } } },
    },
  };
  const res = await generate(env, key, model, payload, 60000);
  if (!res.ok) return { ok: false, ...res };
  try {
    const cand = (res.data.candidates || [])[0] || {};
    const parts = ((cand.content || {}).parts) || [];
    for (const p of parts) {
      const inline = p && p.inlineData;
      if (!inline || !inline.data) continue;
      const mime = String(inline.mimeType || "audio/L16;rate=24000");
      const rateMatch = mime.match(/rate=(\d+)/);
      const rate = rateMatch ? Number(rateMatch[1]) : 24000;
      const bytes = base64ToBytes(inline.data);
      if (!bytes.length) continue;
      // A container (ogg/mp3) already has its own header; PCM needs one written.
      const wav = /wav|L16|pcm/i.test(mime) ? wavFromPcm(bytes, rate) : bytes;
      return { ok: true, audio: wav, rate, mime };
    }
    return { ok: false, kind: "empty", detail: "the model replied with no audio part", http: 502 };
  } catch (e) {
    return { ok: false, kind: "parse", detail: `could not read the audio: ${e && e.message}`, http: 502 };
  }
}

async function handleSpeech(request, env, body, ctx) {
  const { key, source } = pickKey(request, body, env);
  if (!key) {
    return fail(400, "key", "no Gemini API key: set the GEMINI_API_KEY secret, or send x-gemini-key");
  }
  const text = String(body.text || "").trim();
  if (!text) return fail(400, "payload", "text is required");
  if (text.length > 4000) return fail(400, "payload", `text is ${text.length} chars; the limit is 4000`);

  const voice = String(body.voice || env.DJ_VOICE || "Despina").trim();
  const model = ttsModelName(body.model, env);
  const notes = [];
  if (cleanModel(body.model) && cleanModel(body.model) !== model) {
    notes.push(`${cleanModel(body.model)} is a Live model - a Worker cannot open the Live socket, so ${model} spoke it instead`);
  }

  const key32 = await cacheKey([text, voice, model]);
  if (env.CLIPS) {
    try {
      const hit = await env.CLIPS.get(`clips/${key32}.wav`);
      if (hit) {
        const bytes = new Uint8Array(await hit.arrayBuffer());
        if (bytes.length) {
          return new Response(bytes, {
            status: 200,
            headers: {
              "Content-Type": "audio/wav",
              "Cache-Control": "private, max-age=86400",
              "X-Voice-Model": model,
              "X-Voice-Cached": "1",
              ...CORS_HEADERS,
            },
          });
        }
      }
    } catch {
      /* a missing bucket must not cost the listener the announcement */
    }
  }

  let res = await synthesize(env, key, model, text, voice);
  // One bounded retry: the REST TTS endpoint rate-limits in bursts, and the
  // announcement is pre-synthesized at track start alongside the plan call.
  if (!res.ok && res.kind === "quota") {
    await sleep(2500);
    res = await synthesize(env, key, model, text, voice);
    if (res.ok) notes.push("the first speech call hit the quota; the retry spoke it");
  }
  if (!res.ok) {
    const status = res.kind === "quota" ? 429 : res.kind === "key" || res.kind === "access" ? 401 : 502;
    return fail(status, res.kind, res.detail, { model, status: res.status, notes, source });
  }
  notes.push(`${res.audio.length} bytes @ ${res.rate} Hz as ${res.mime}`);

  if (env.CLIPS && ctx) {
    ctx.waitUntil(
      env.CLIPS.put(`clips/${key32}.wav`, res.audio, {
        httpMetadata: { contentType: "audio/wav" },
      }).catch(() => {}),
    );
  }
  return new Response(res.audio, {
    status: 200,
    headers: {
      "Content-Type": "audio/wav",
      "Cache-Control": "private, max-age=86400",
      "X-Voice-Model": model,
      "X-Voice-Cached": "0",
      "X-Voice-Notes": notes.join("; ").slice(0, 300),
      ...CORS_HEADERS,
    },
  });
}

// ----------------------------------------------------------------- D1 routes

function hasD1(env) {
  return !!(env.DB && typeof env.DB.prepare === "function");
}

async function handleStateGet(request, env, url) {
  if (!hasD1(env)) return fail(503, "no_d1", "this Worker has no D1 binding (see worker/README.md)");
  const profile = String(url.searchParams.get("profile") || "default").slice(0, 120);
  const row = await env.DB.prepare(
    "SELECT state, updated_at FROM profiles WHERE profile = ?",
  )
    .bind(profile)
    .first();
  if (!row) return json({ ok: true, profile, state: null, updated_at: 0 });
  let state = null;
  try {
    state = JSON.parse(row.state);
  } catch {
    state = null;
  }
  return json({ ok: true, profile, state, updated_at: Number(row.updated_at || 0) });
}

async function handleStatePut(request, env, body) {
  if (!hasD1(env)) return fail(503, "no_d1", "this Worker has no D1 binding (see worker/README.md)");
  const profile = String(body.profile || "default").slice(0, 120);
  if (body.state === undefined || body.state === null) {
    return fail(400, "payload", "state is required (the whole state.json object)");
  }
  const now = Date.now();
  await env.DB.prepare(
    `INSERT INTO profiles (profile, state, updated_at)
     VALUES (?, ?, ?)
     ON CONFLICT(profile) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at`,
  )
    .bind(profile, JSON.stringify(body.state), now)
    .run();
  return json({ ok: true, profile, updated_at: now });
}

async function handleEventsPost(request, env, body) {
  if (!hasD1(env)) return fail(503, "no_d1", "this Worker has no D1 binding (see worker/README.md)");
  const profile = String(body.profile || "default").slice(0, 120);
  const events = Array.isArray(body.events) ? body.events.slice(0, 500) : null;
  if (!events) return fail(400, "payload", "events must be an array");
  const now = Date.now();
  const stmts = [];
  let n = 0;
  for (const e of events) {
    const kind = String((e && e.kind) || "").slice(0, 40);
    if (!kind) continue;
    const ts = Number(e.ts) > 0 ? Number(e.ts) : now;
    stmts.push(
      env.DB.prepare("INSERT INTO events (profile, ts, kind, payload) VALUES (?, ?, ?, ?)").bind(
        profile,
        ts,
        kind,
        JSON.stringify(e.payload === undefined ? {} : e.payload),
      ),
    );
    n += 1;
  }
  if (!n) return fail(400, "payload", "no usable events (each one needs a kind)");
  await env.DB.batch(stmts);
  const last = await env.DB.prepare(
    "SELECT COALESCE(MAX(id), 0) AS id FROM events WHERE profile = ?",
  )
    .bind(profile)
    .first();
  return json({ ok: true, n, last_id: Number((last && last.id) || 0) });
}

async function handleEventsGet(request, env, url) {
  if (!hasD1(env)) return fail(503, "no_d1", "this Worker has no D1 binding (see worker/README.md)");
  const profile = String(url.searchParams.get("profile") || "default").slice(0, 120);
  const since = Number(url.searchParams.get("since") || 0) || 0;
  const limit = Math.max(1, Math.min(Number(url.searchParams.get("limit") || 500), 2000));
  const rows = await env.DB.prepare(
    "SELECT id, ts, kind, payload FROM events WHERE profile = ? AND id > ? ORDER BY id ASC LIMIT ?",
  )
    .bind(profile, since, limit)
    .all();
  const events = (rows.results || []).map((r) => {
    let payload = {};
    try {
      payload = JSON.parse(r.payload);
    } catch {
      payload = {};
    }
    return { id: Number(r.id), ts: Number(r.ts), kind: String(r.kind), payload };
  });
  return json({ ok: true, profile, events });
}

// ------------------------------------------------------------------- routing

async function readBody(request) {
  const type = request.headers.get("content-type") || "";
  if (!type.includes("json")) {
    const form = await request.text();
    const out = {};
    for (const [k, v] of new URLSearchParams(form)) out[k] = v;
    return out;
  }
  try {
    return await request.json();
  } catch {
    return null;
  }
}

/**
 * The dispatch table. Written as data so that "no such route" and "right route,
 * wrong verb" are two different answers - a 405 for a typo'd path sends the
 * reader looking at their HTTP client instead of at their spelling.
 */
const ROUTES = {
  "/v1/health": {
    GET: (request, env, body, ctx, url) => json({
      ok: true,
      service: "spotube-dj-worker",
      version: VERSION,
      key_source: env.GEMINI_API_KEY ? "secret" : "client (send x-gemini-key)",
      model: DEFAULT_PLAN_MODEL,
      models: modelList(env.GEMINI_MODEL, env),
      tts_model: ttsModelName(env.GEMINI_TTS_MODEL, env),
      voice: env.DJ_VOICE || "Despina",
      d1: hasD1(env),
      clips: !!env.CLIPS,
      token_required: !!env.WORKER_TOKEN,
    }),
  },
  "/v1/plan": { POST: (request, env, body) => handlePlan(request, env, body) },
  "/v1/text": { POST: (request, env, body) => handleText(request, env, body) },
  "/v1/speech": { POST: (request, env, body, ctx) => handleSpeech(request, env, body, ctx) },
  "/v1/state": {
    GET: (request, env, body, ctx, url) => handleStateGet(request, env, url),
    PUT: (request, env, body) => handleStatePut(request, env, body),
    POST: (request, env, body) => handleStatePut(request, env, body),
  },
  "/v1/events": {
    GET: (request, env, body, ctx, url) => handleEventsGet(request, env, url),
    POST: (request, env, body) => handleEventsPost(request, env, body),
  },
};

function allowedMethods(path) {
  return Object.keys(ROUTES[path] || {}).join(", ");
}

const ROUTE_MAP = `GET  /v1/health
POST /v1/plan     {prompt, system?, model?, timeoutMs?}
POST /v1/text     {prompt, system?, model?, maxChars?, temperature?}
POST /v1/speech   {text, voice?, model?}  -> audio/wav
GET  /v1/state    ?profile=
PUT  /v1/state    {profile, state}
POST /v1/events   {profile, events:[{ts, kind, payload}]}
GET  /v1/events   ?profile=&since=&limit=`;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (path === "/" || path === "") {
      return new Response(`spotube-dj worker ${VERSION}\n\n${ROUTE_MAP}\n${CLIP_CACHE_TTL_NOTE}\n`, {
        headers: { "Content-Type": "text/plain; charset=utf-8", ...CORS_HEADERS },
      });
    }
    if (!path.startsWith("/v1/")) {
      return fail(404, "not_found", `no route ${path}`, { routes: ROUTE_MAP.split("\n") });
    }
    if (!(await authorized(request, env))) {
      return fail(401, "auth", "this Worker needs a token (Authorization: Bearer <WORKER_TOKEN>)");
    }

    const route = ROUTES[path];
    if (!route) {
      return fail(404, "not_found", `no route ${path}`, { routes: ROUTE_MAP.split("\n") });
    }
    const handler = route[request.method];
    if (!handler) {
      return fail(405, "method", `${request.method} is not allowed on ${path}`, {
        allow: allowedMethods(path),
        routes: ROUTE_MAP.split("\n"),
      });
    }
    const needsBody = request.method !== "GET";
    const body = needsBody ? (await readBody(request)) || {} : {};
    try {
      return await handler(request, env, body, ctx, url);
    } catch (e) {
      return fail(500, "crash", `${e && e.name}: ${e && e.message}`.slice(0, 300));
    }
  },
};
