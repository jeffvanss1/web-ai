/**
 * Route tests for the Worker, run with plain node: `node --test test/`
 *
 * There is no miniflare and no deployed Worker here, so this file supplies the
 * two things the routes actually touch: a fake generativelanguage endpoint and
 * a fake D1/R2. The point is to pin the behaviour the app depends on - the
 * model ladder, the payload shapes, the error kinds, the WAV header, the cache
 * hit, and the D1 round trip - not to re-test Cloudflare.
 */
import test from "node:test";
import assert from "node:assert/strict";

import worker from "../src/index.js";

// ------------------------------------------------------------- fake Google API

const state = {
  scenario: "ok", // ok | retired | quota | empty | audio | noaudio
  calls: [], // every request the Worker made, for assertions
};

function geminiTextReply(text) {
  return {
    candidates: [{ content: { parts: [{ text }] }, finishReason: "STOP" }],
  };
}

function fakeGemini(url, init) {
  const u = new URL(url);
  const body = JSON.parse(init.body || "{}");
  state.calls.push({ url: url, model: u.pathname, body });
  const model = u.pathname.split("/").pop().split(":")[0];

  if (state.scenario === "quota") {
    return new Response(
      JSON.stringify({
        error: { status: "RESOURCE_EXHAUSTED", message: "Quota exceeded for requests per minute" },
      }),
      { status: 429, headers: { "Content-Type": "application/json" } },
    );
  }
  if (state.scenario === "retired" && model === "gemini-3.5-flash") {
    return new Response(
      JSON.stringify({
        error: {
          status: "NOT_FOUND",
          message:
            "models/gemini-3.5-flash is no longer available. Please update your code to use models/gemini-3.6-flash instead.",
        },
      }),
      { status: 404, headers: { "Content-Type": "application/json" } },
    );
  }
  if (state.scenario === "empty") {
    return new Response(JSON.stringify({ candidates: [{ content: { parts: [{}] } }] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (state.scenario === "audio") {
    if (body.generationConfig && body.generationConfig.responseModalities) {
      const pcm = Buffer.alloc(200, 7); // 100 samples of silence-ish mono 16-bit
      return new Response(
        JSON.stringify({
          candidates: [
            {
              content: {
                parts: [
                  {
                    inlineData: {
                      mimeType: "audio/L16;codec=pcm;rate=24000",
                      data: pcm.toString("base64"),
                    },
                  },
                ],
              },
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify(geminiTextReply("not audio")), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (state.scenario === "noaudio") {
    return new Response(JSON.stringify({ candidates: [{ content: { parts: [{ text: "sorry" }] } }] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  // "ok": a plan when a schema was asked for, prose otherwise
  const asked = JSON.stringify(body);
  if (asked.includes("queries") || asked.includes("responseMimeType") || asked.includes("responseFormat")) {
    return new Response(JSON.stringify(geminiTextReply('{"queries": ["slowdive", "mbv"], "why": "you asked for it"}')), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  return new Response(
    JSON.stringify(geminiTextReply("Alright, here we go - Slowdive, and it is a whole mood.")),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

// --------------------------------------------------------------- fake D1 / R2

function makeD1() {
  const profiles = new Map();
  const events = [];
  let nextId = 1;
  const stmt = (sql, args) => ({
    async first() {
      if (sql.startsWith("SELECT state, updated_at")) {
        const row = profiles.get(args[0]);
        return row ? { state: row.state, updated_at: row.updated_at } : null;
      }
      if (sql.startsWith("SELECT COALESCE(MAX(id), 0)")) {
        const mine = events.filter((e) => e.profile === args[0]);
        return { id: mine.length ? mine[mine.length - 1].id : 0 };
      }
      return null;
    },
    async run() {
      if (sql.startsWith("INSERT INTO profiles")) {
        profiles.set(args[0], { state: args[1], updated_at: args[2] });
      }
      return { success: true };
    },
    async all() {
      if (sql.startsWith("SELECT id, ts, kind, payload")) {
        const [profile, since, limit] = args;
        return {
          results: events
            .filter((e) => e.profile === profile && e.id > since)
            .slice(0, limit)
            .map((e) => ({ id: e.id, ts: e.ts, kind: e.kind, payload: e.payload })),
        };
      }
      return { results: [] };
    },
    _sql: sql,
    _args: args,
  });
  return {
    prepare(sql) {
      return {
        bind(...args) {
          return stmt(sql, args);
        },
      };
    },
    async batch(stmts) {
      for (const s of stmts) {
        if (s._sql.startsWith("INSERT INTO events")) {
          const [profile, ts, kind, payload] = s._args;
          events.push({ id: nextId++, profile, ts, kind, payload });
        }
      }
      return [];
    },
    _events: events,
    _profiles: profiles,
  };
}

function makeR2() {
  const store = new Map();
  return {
    async get(key) {
      const hit = store.get(key);
      return hit ? { arrayBuffer: async () => hit.buffer.slice(hit.byteOffset, hit.byteOffset + hit.byteLength) } : null;
    },
    async put(key, body) {
      store.set(key, body);
      return {};
    },
    _store: store,
  };
}

// -------------------------------------------------------------------- harness

function envWith(over = {}) {
  return {
    GEMINI_API_KEY: "test-key",
    WORKER_TOKEN: "",
    GEMINI_URL: "https://generativelanguage.googleapis.com/v1beta",
    GEMINI_MODEL: "gemini-3.5-flash",
    DB: makeD1(),
    CLIPS: makeR2(),
    ...over,
  };
}

async function call(method, path, { body, env, headers = {} } = {}) {
  const init = { method, headers: { "Content-Type": "application/json", ...headers } };
  if (body !== undefined) init.body = JSON.stringify(body);
  const request = new Request(`https://worker.test${path}`, init);
  const realFetch = globalThis.fetch;
  globalThis.fetch = fakeGemini;
  try {
    return await worker.fetch(request, env || envWith(), { waitUntil: (p) => Promise.resolve(p).catch(() => {}) });
  } finally {
    globalThis.fetch = realFetch;
  }
}

test.beforeEach(() => {
  state.scenario = "ok";
  state.calls = [];
});

// ---------------------------------------------------------------------- tests

test("GET / answers with the route map", async () => {
  const res = await call("GET", "/");
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.match(text, /POST \/v1\/plan/);
  assert.match(text, /POST \/v1\/speech/);
});

test("OPTIONS is answered for the preflight a browser sends", async () => {
  const res = await call("OPTIONS", "/v1/plan");
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("access-control-allow-methods"), "GET, POST, PUT, OPTIONS");
});

test("GET /v1/health reports what this Worker can do", async () => {
  const res = await call("GET", "/v1/health");
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.key_source, "secret");
  assert.equal(body.d1, true);
  assert.equal(body.clips, true);
  assert.equal(body.token_required, false);
  assert.ok(body.models.includes("gemini-3.5-flash"));
});

test("WORKER_TOKEN is required on every /v1 route once it is set", async () => {
  const env = envWith({ WORKER_TOKEN: "shh" });
  const denied = await call("GET", "/v1/health", { env });
  assert.equal(denied.status, 401);
  assert.equal((await denied.json()).error.kind, "auth");

  const allowed = await call("GET", "/v1/health", { env, headers: { Authorization: "Bearer shh" } });
  assert.equal(allowed.status, 200);

  // wrong token, right shape
  const wrong = await call("GET", "/v1/health", { env, headers: { Authorization: "Bearer nope" } });
  assert.equal(wrong.status, 401);
});

test("POST /v1/plan returns the parsed plan and the model that answered", async () => {
  const res = await call("POST", "/v1/plan", { body: { prompt: "chill lofi for coding", system: "you are a DJ" } });
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.deepEqual(body.plan.queries, ["slowdive", "mbv"]);
  assert.equal(body.model, "gemini-3.5-flash");
  assert.equal(state.calls.length, 1);
  // the key goes in a header, never in the URL
  assert.ok(!state.calls[0].url.includes("key="));
  assert.equal(state.calls[0].body.contents[0].parts[0].text.includes("chill lofi for coding"), true);
});

test("a retired model is walked to the one the API named, and the switch is noted", async () => {
  state.scenario = "retired";
  const res = await call("POST", "/v1/plan", { body: { prompt: "mellow evening jazz" } });
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.model, "gemini-3.6-flash", "Google's own hint is tried before the ladder");
  assert.ok(
    body.notes.some((n) => n.includes("retired") && n.includes("gemini-3.6-flash")),
    `notes: ${JSON.stringify(body.notes)}`,
  );
  assert.equal(state.calls.length, 2);
});

test("a key failure is reported as 'key' and is never retried into a storm", async () => {
  const env = envWith({ GEMINI_API_KEY: "" });
  const res = await call("POST", "/v1/plan", { body: { prompt: "x", key: "" }, env });
  assert.equal(res.status, 400);
  const body = await res.json();
  assert.equal(body.error.kind, "key");
  assert.equal(state.calls.length, 0);
});

test("a client may send its own key when the Worker secret is not set", async () => {
  const env = envWith({ GEMINI_API_KEY: "" });
  const res = await call("POST", "/v1/plan", { body: { prompt: "x", key: "client-key" }, env });
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.source, "body");
  assert.equal(state.calls[0].url.includes("key="), false);
});

test("quota is 'quota', retried a couple of times, then reported", async () => {
  state.scenario = "quota";
  const res = await call("POST", "/v1/plan", { body: { prompt: "x", timeoutMs: 5 } });
  assert.equal(res.status, 429);
  const body = await res.json();
  assert.equal(body.error.kind, "quota");
  assert.ok(state.calls.length >= 2, "a burst is worth one retry");
  assert.ok(state.calls.length <= 6, "but not an unbounded one");
});

test("a model that answers with nothing is 'empty', not a crash", async () => {
  state.scenario = "empty";
  const res = await call("POST", "/v1/plan", { body: { prompt: "x" } });
  const body = await res.json();
  assert.equal(body.ok, false);
  assert.equal(body.error.kind, "empty");
});

test("POST /v1/text returns prose and walks the ladder to get it", async () => {
  const res = await call("POST", "/v1/text", {
    body: { prompt: "write a DJ line", maxChars: 400, system: "be warm" },
  });
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.match(body.text, /Slowdive/);
  assert.equal(body.text.length <= 400, true);
});

// /v1/text and /v1/plan hold the same model ladder, and for a while they held a
// *different copy* of it: handleText declared the list `const` and then
// reassigned it, which is a TypeError the moment the first model is retired.
// `node --check` cannot see it (it only parses) and the text tests below only
// ever ran the happy path, so it shipped - `wrangler deploy` was the first
// thing to notice, as a BuildFailure with no line number in the app's output.
test("a retired model is walked by /v1/text too, not just by /v1/plan", async () => {
  state.scenario = "retired";
  const res = await call("POST", "/v1/text", { body: { prompt: "write a DJ line" } });
  const body = await res.json();
  assert.equal(body.ok, true, JSON.stringify(body));
  assert.equal(body.model, "gemini-3.6-flash", "the API's own hint is tried first");
  assert.ok(
    body.notes.some((n) => n.includes("retired") && n.includes("gemini-3.6-flash")),
    `notes: ${JSON.stringify(body.notes)}`,
  );
  assert.equal(state.calls.length, 2);
});

test("POST /v1/speech returns a real WAV and caches it in R2", async () => {
  state.scenario = "audio";
  const env = envWith();
  const res = await call("POST", "/v1/speech", { body: { text: "Up next, Slowdive", voice: "Despina" }, env });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "audio/wav");
  assert.equal(res.headers.get("x-voice-cached"), "0");
  const buf = Buffer.from(await res.arrayBuffer());
  assert.equal(buf.subarray(0, 4).toString(), "RIFF");
  assert.equal(buf.subarray(8, 12).toString(), "WAVE");
  assert.equal(buf.readUInt32LE(24), 24000, "the sample rate is parsed out of the mimeType");
  assert.equal(buf.readUInt32LE(40), 200, "data chunk length is the PCM byte count");
  assert.equal(buf.length, 44 + 200);

  // second call: same text, same voice -> no request to Gemini at all
  state.calls = [];
  const again = await call("POST", "/v1/speech", { body: { text: "Up next, Slowdive", voice: "Despina" }, env });
  assert.equal(again.headers.get("x-voice-cached"), "1");
  assert.equal(state.calls.length, 0, "a cached line must not cost quota");
});

// The R2 binding is OFF by default (see wrangler.toml), so this is the shape
// most deployments actually run: no bucket, every line synthesized on demand.
// Every CLIPS use in the source is behind `if (env.CLIPS)`, and this is the
// test that keeps it that way - if a `.get` ever escapes its guard, the speech
// route throws instead of speaking.
test("POST /v1/speech works with no R2 binding at all", async () => {
  state.scenario = "audio";
  const env = envWith({ CLIPS: undefined });
  const res = await call("POST", "/v1/speech", {
    body: { text: "Up next, Slowdive", voice: "Despina" },
    env,
  });
  assert.equal(res.status, 200, "no bucket must not cost the listener the line");
  assert.equal(res.headers.get("content-type"), "audio/wav");
  assert.equal(res.headers.get("x-voice-cached"), "0");
  const buf = Buffer.from(await res.arrayBuffer());
  assert.equal(buf.subarray(0, 4).toString(), "RIFF");
  assert.equal(buf.length, 44 + 200);

  // and it is genuinely uncached: the same line asks Gemini again
  state.calls = [];
  const again = await call("POST", "/v1/speech", {
    body: { text: "Up next, Slowdive", voice: "Despina" },
    env,
  });
  assert.equal(again.status, 200);
  assert.equal(again.headers.get("x-voice-cached"), "0");
  assert.equal(state.calls.length, 1, "no bucket means no reuse - one call per line");
});

test("GET /v1/health reports clips: false when the bucket is not bound", async () => {
  const res = await call("GET", "/v1/health", { env: envWith({ CLIPS: undefined }) });
  const body = await res.json();
  assert.equal(body.clips, false);
  assert.equal(body.d1, true, "dropping R2 must not disturb D1");
});

test("a Live model name is mapped to its REST TTS sibling, and says so", async () => {
  state.scenario = "audio";
  const res = await call("POST", "/v1/speech", {
    body: { text: "hello", model: "gemini-3.1-flash-live-preview" },
  });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("x-voice-model"), /tts/);
  assert.match(res.headers.get("x-voice-notes") || "", /cannot open the Live socket/);
});

test("speech with no audio part is 'empty', not a 200 with silence", async () => {
  state.scenario = "noaudio";
  const res = await call("POST", "/v1/speech", { body: { text: "hello" } });
  assert.equal(res.status, 502);
  assert.equal((await res.json()).error.kind, "empty");
});

test("speech refuses to buffer an essay", async () => {
  const res = await call("POST", "/v1/speech", { body: { text: "x".repeat(4001) } });
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error.kind, "payload");
});

test("state round-trips through D1", async () => {
  const env = envWith();
  const put = await call("PUT", "/v1/state", {
    body: { profile: "laptop", state: { artists: [{ name: "Slowdive", w: 2.0 }], volume: 60 } },
    env,
  });
  assert.equal(put.status, 200);
  const got = await call("GET", "/v1/state?profile=laptop", { env });
  const body = await got.json();
  assert.equal(body.ok, true);
  assert.deepEqual(body.state.artists, [{ name: "Slowdive", w: 2.0 }]);
  assert.ok(body.updated_at > 0);

  // an upsert replaces it rather than creating a second row
  await call("PUT", "/v1/state", { body: { profile: "laptop", state: { volume: 30 } }, env });
  const again = await (await call("GET", "/v1/state?profile=laptop", { env })).json();
  assert.deepEqual(again.state, { volume: 30 });
});

test("an unknown profile is an empty 200, not a 404 the app has to special-case", async () => {
  const res = await call("GET", "/v1/state?profile=nobody");
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.state, null);
  assert.equal(body.updated_at, 0);
});

test("events append and can be pulled since the last id seen", async () => {
  const env = envWith();
  const post = await call("POST", "/v1/events", {
    body: {
      profile: "laptop",
      events: [
        { ts: 1, kind: "like", payload: { artist: "Slowdive", title: "Alison" } },
        { ts: 2, kind: "skip", payload: { artist: "Unknown" } },
      ],
    },
    env,
  });
  const posted = await post.json();
  assert.equal(posted.ok, true);
  assert.equal(posted.n, 2);
  assert.equal(posted.last_id, 2);

  const all = await (await call("GET", "/v1/events?profile=laptop", { env })).json();
  assert.equal(all.events.length, 2);
  assert.equal(all.events[0].kind, "like");

  const since = await (await call("GET", "/v1/events?profile=laptop&since=1", { env })).json();
  assert.equal(since.events.length, 1);
  assert.equal(since.events[0].kind, "skip");
  assert.deepEqual(since.events[0].payload, { artist: "Unknown" });
});

test("events with no kind are refused rather than stored as junk", async () => {
  const res = await call("POST", "/v1/events", { body: { profile: "x", events: [{ ts: 1 }] } });
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error.kind, "payload");
});

test("a Worker with no D1 says so instead of pretending to have saved", async () => {
  const env = envWith();
  delete env.DB;
  const res = await call("GET", "/v1/state", { env });
  assert.equal(res.status, 503);
  assert.equal((await res.json()).error.kind, "no_d1");
});

test("an unknown path answers with the map, and a wrong method says what it takes", async () => {
  const missing = await call("GET", "/v1/nope");
  assert.equal(missing.status, 404);
  assert.ok((await missing.json()).error.routes.length > 0);

  const wrong = await call("DELETE", "/v1/plan");
  assert.equal(wrong.status, 405);
  assert.equal((await wrong.json()).error.kind, "method");
});
