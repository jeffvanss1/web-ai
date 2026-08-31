# The Spotube DJ Worker

The app's only route to Gemini, and the only place its taste profile leaves the
machine it was learned on.

```
spotube_dj/webapp.py  ──▶  static/index.html · app.css · app.js
spotube_dj/brain.py   ──▶  POST /v1/plan     (query planning)
spotube_dj/agent.py   ──▶  POST /v1/text     (the DJ's own words)
spotube_dj/djvoice.py ──▶  POST /v1/speech   (the spoken DJ, audio/wav)
spotube_dj/web.py     ──▶  GET/PUT /v1/state, POST/GET /v1/events  (D1)
```

## Why there is one

The player is a local Python process. Pointing it straight at
`generativelanguage.googleapis.com` meant three things, and all three were the
same bug wearing a different hat:

* **the key lived on every machine.** Paste it into a laptop, a desktop and a
  phone-ish layout and you now have three copies of a secret, in three
  `config.json`s, chmod 0600 or not.
* **a retired model was an outage.** Google retires `gemini-*` names regularly.
  Client-side, every install had to notice the 404 and edit a field. Now the
  ladder is walked here: a retirement costs one extra request, once, for
  everybody, and `GEMINI_MODEL_LADDER` in `wrangler.toml` is the whole fix.
* **taste was trapped on one disk.** The profile is learned from what you skip.
  D1 gives it somewhere to live that survives a reinstall and can be read by a
  second machine.

## Check before you deploy

```bash
npm run check     # node --check, then a real `wrangler deploy --dry-run`
npm test          # 26 route tests
```

`node --check` alone is **not** enough and has already missed one deploy-blocking
bug: it only parses, so `models = f(models)` against a `const models` sails
through it and through the test suite, and only fails when esbuild builds the
bundle (`errorType: BuildFailure` in wrangler's output, with no line number).
The dry-run needs no Cloudflare account - it builds locally and exits.

## Deploy

```bash
cd worker
npm install                                  # just wrangler
npx wrangler login
npx wrangler d1 create spotube-dj            # paste the id into wrangler.toml
npx wrangler d1 execute spotube-dj --file=schema.sql --remote
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put WORKER_TOKEN        # set this one
npx wrangler deploy
```

Then point the app at it, once, from Settings → Worker (or `~/.spotube-dj/config.json`):

```json
{ "WORKER_URL": "https://spotube-dj.<your-subdomain>.workers.dev",
  "WORKER_TOKEN": "the same WORKER_TOKEN",
  "WORKER_PROFILE": "default" }
```

If `wrangler deploy` fails with `BuildFailure` and no useful message, run
`npx wrangler deploy --dry-run` - the dry-run prints the file and line.

`spotube-dj --doctor` prints a `worker` line, and the page's header pill says
which planner is live. `Test the worker` in Settings runs `/v1/health` and the
real `/v1/plan` probe and prints the answer in the log drawer.

## Two secrets, not one

| Secret | What it guards | What happens without it |
|---|---|---|
| `GEMINI_API_KEY` | the Google key | clients may send their own with `x-gemini-key` and the Worker relays it |
| `WORKER_TOKEN` | this Worker's routes | **every route is open to anyone who learns the URL** — including `PUT /v1/state`, which can overwrite your taste profile |

The secret wins when both are present, so you can run a shared Worker with a
paid key and let a friend's install piggyback on it without learning it.

## Routes

| Route | Body | Returns |
|---|---|---|
| `GET /v1/health` | – | what this Worker can do: key source, model ladder, D1/R2 bound, token required |
| `POST /v1/plan` | `{prompt, system?, model?, timeoutMs?}` | `{ok, plan:{queries,avoid,why}, model, notes}` |
| `POST /v1/text` | `{prompt, system?, model?, maxChars?, temperature?}` | `{ok, text, model}` |
| `POST /v1/speech` | `{text, voice?, model?}` | `audio/wav` bytes; `X-Voice-Cached`, `X-Voice-Model` |
| `GET /v1/state` | `?profile=` | `{ok, state, updated_at}` (`state: null` when new) |
| `PUT /v1/state` | `{profile, state}` | `{ok, updated_at}` |
| `POST /v1/events` | `{profile, events:[{ts,kind,payload}]}` | `{ok, n, last_id}` |
| `GET /v1/events` | `?profile=&since=&limit=` | `{ok, events:[{id,ts,kind,payload}]}` |

Failures are JSON with the same six `kind`s `brain.py` has always used, so the
app's wording never had to change:

```json
{"ok": false, "error": {"kind": "model", "detail": "...gemini-3.5-flash is no longer available...",
                        "status": "NOT_FOUND", "notes": ["...trying gemini-3.6-flash"]}}
```

`key` · `access` · `model` · `quota` · `payload` · `timeout` · `network` ·
`empty` · `parse` · `auth` · `no_d1` · `crash`.

## The spoken line, and why it is not the Live API

The desktop app used to speak over Gemini's **Live** API WebSocket
(`gemini-*-live-preview`), because the REST TTS endpoint rate-limits on the free
tier. A Worker cannot open a WebSocket as a client — it can only proxy one it
was handed — so `/v1/speech` uses the REST TTS sibling instead and answers the
quota problem the other way: **the clip is cached**. `sha256(text|voice|model)`
in R2, so "Stay right here — up next Radiohead" is synthesized once and never
again. **The bucket is off by default** — one less thing to create, pay for and
think about, and the spoken DJ works either way. Bind `CLIPS` (R2) and
uncomment the `[[r2_buckets]]` block in `wrangler.toml` to turn it on; without
it the Worker still speaks,
it just asks Google every time and can 429 mid-set like the old client did.

A `*-live-*` model name is mapped to its `-tts-` sibling rather than rejected,
and the substitution is reported in the response's `X-Voice-Notes`, so the
setting in the app does not have to change.

## Local dev, without deploying

```bash
cd worker
cp .dev.vars.example .dev.vars     # then fill it in
npm run dev                        # http://127.0.0.1:8787
node --test test/                  # route tests against a fake Gemini
```

`npm run dev` runs the real Worker against a local D1 replica. Apply the schema
to it with `npx wrangler d1 execute spotube-dj --file=schema.sql` (no
`--remote`). Point the app at `http://127.0.0.1:8787` and the whole loop runs on
one machine with nothing deployed.
