# Cloudflare Worker deployment

`worker/` is the edge/browser port of Spotube DJ. It is for a managed Cloudflare
Worker plus D1 and Durable Objects, so no VPS or personal computer has to remain
online.

The Python app and the Worker share the DJ behaviour at the product level, but
not the same process. Workers cannot run the Python app's `http.server`, mpv,
`yt-dlp` executable, subprocesses, Unix sockets, or background Python threads.
The Worker replaces those boundaries with Fetch handlers, D1, a Durable Object,
and the browser's official YouTube embedded player.

## What the Worker provides

- Multiple accounts with private password sessions.
- A personal queue, likes, skips, artist weights, repeat, shuffle, autoplay,
  previous/next, search, stations, taste reset/restore, and activity wording.
- Gemini or OpenAI-compatible planning, with an explicit offline query-parser
  fallback and diagnostics when the AI brain is unavailable.
- Gemini TTS, OpenAI-compatible TTS, and browser speech fallback for the DJ line
  and the up-next announcement. New profiles default to Gemini with Indonesian;
  saved provider, voice, language, and enabled/disabled choices remain authoritative.
  Announcements are compact and stale browser/audio requests are cancelled when
  playback changes.
- Mobile queue controls include an add-to-queue entry point, count, clear/remove
  actions, and requester labels. Room members and requested tracks carry sanitized
  display metadata separately from the verified playback id.
- Room planning blends member taste with the live room request, current track,
  history, queue, vibe, and set segment. An enabled room uses a guarded similar-song
  refill when its queue reaches the end; all refill results still pass the original
  YouTube Music Song filter.
- Private invite-code listen parties.
- Shared party controls: every member may play, pause, seek, enqueue, remove,
  shuffle, repeat, skip, and change the queue.
- A blended party mix: each member's liked artists and skipped songs contribute to
  the next room mix. A like/skip still belongs to the person who made it.
- Durable Object WebSocket updates so participants converge on the room's current
  track, play state, queue, and server position.
- A Spotify-like three-column browser UI with library, home/search views, now
  playing, queue, settings, voice controls, and party controls.

Every participant plays the same YouTube video independently in their browser.
The room broadcasts the video ID, play/pause state, and server position; clients
correct drift when it exceeds a small tolerance. The Worker never extracts,
downloads, or proxies audio.

## Deploy

Install Node.js, then from the repository root:

```bash
cd worker
npm install
npx wrangler login
npx wrangler d1 create spotube-dj
```

Copy the generated `database_id` into `worker/wrangler.jsonc`, replacing:

```text
REPLACE_WITH_THE_ID_FROM_WRANGLER_D1_CREATE
```

Apply both D1 migrations:

```bash
npm run db:migrate:remote
```

The Worker also has an idempotent first-request schema bootstrap as a safety net
for a newly bound D1, but migrations are still the normal deployment path and
should be applied before relying on production traffic.

Configure the planning/TTS providers as encrypted Wrangler secrets. At minimum,
none are required: the offline planner and browser speech fallback work without
AI credentials.

```bash
# Optional Gemini planner and TTS. Defaults: gemini-3.5-flash-lite and
# gemini-3.1-flash-live-preview, using the Live WebSocket API.
npx wrangler secret put GEMINI_API_KEY
# GEMINI_MODEL, GEMINI_TTS_MODEL, and GEMINI_TTS_VOICE can be added as Wrangler vars if desired.

# Optional OpenAI-compatible planner.
npx wrangler secret put LLM_BASE_URL
npx wrangler secret put LLM_API_KEY
npx wrangler secret put LLM_MODEL

# Optional OpenAI-compatible TTS. The default model is gpt-4o-mini-tts,
# which supports expressive delivery instructions; override for another server.
npx wrangler secret put TTS_BASE_URL
npx wrangler secret put TTS_API_KEY
npx wrangler secret put TTS_MODEL
npx wrangler secret put TTS_VOICE

npm run deploy
```

Do not put API keys or passwords in `wrangler.jsonc`, `.dev.vars`, or source
files. `.dev.vars` is ignored by git. The deployed app creates accounts in the
browser; each user gets a private profile. A room creator shares the generated
invite code or the copied invite link.

Wrangler prints the public `workers.dev` URL. Open it, create an account, make a
mix, then use **Create** in the Listen along box. Other people create their own
accounts and join with the code. Browser autoplay rules may require each person
to press Play once.

Open **Settings → Test AI brain** after deploying. It reports whether Gemini or
the OpenAI-compatible planner answered, including the model error when the Worker
falls back to its offline parser. For expressive speech, choose **Gemini TTS** or
an OpenAI-compatible endpoint using `gpt-4o-mini-tts`; Browser voice remains an
intentional fallback and can sound flatter.

A custom domain can be attached later in Cloudflare without adding a reverse
proxy or running a server yourself.

## Local Worker development

```bash
cd worker
cp .dev.vars.example .dev.vars
npm run db:migrate:local
npm run dev
```

Open the local URL Wrangler prints. The local D1 database is disposable; remote
accounts, profiles, and rooms are unaffected. Validate the bundle with:

```bash
npm run typecheck
npx wrangler deploy --dry-run
```

## Provider and playback notes

The Worker uses the same Gemini Live WebSocket protocol as the Python DJ for
`gemini-3.1-flash-live-preview`: it sends the DJ script in a Live session, collects
the returned 24 kHz PCM chunks at the edge, wraps them as WAV, and gives that
browser a playable audio response. The Gemini API key never reaches the browser
and the Worker never runs mpv. A configured `*-tts-preview` model is still
supported through Gemini's `generateContent` speech endpoint. For
OpenAI-compatible TTS, the configured endpoint must implement `POST
/v1/audio/speech` and return an audio body. If a remote provider is unavailable,
the browser uses its Web Speech API when available.

The embedded YouTube player is intentionally visible and controlled by YouTube.
Ads, regional restrictions, unavailable videos, and YouTube's applicable terms
still apply. This is not a direct audio stream or a server-side audio relay.

The current room implementation is private and invite-based. D1 stores users,
profiles, room membership, and room metadata; the Durable Object stores the
live room queue and synchronizes connected browsers. A member's like/skip is
personal, while a new room mix blends all current members' profiles.

If exact parity with the Python app's server-side stream resolution, mpv audio,
MusicBrainz cover cache, or every album-browse detail is more important than edge
execution, deploy the Python app in a managed container such as Cloud Run instead.
That is still not a VPS, but it is not a Cloudflare Worker.

## Routes

- `GET /healthz`
- `GET /api/auth`
- `POST /api/signup`, `/api/login`, `/api/logout`
- `GET /api/state`
- `GET`/`POST /api/search?q=...`
- `POST /api/action` for personal playback, taste, queue, and settings actions
- `GET`/`POST /api/settings`
- `POST /api/brain-test` to verify the configured planner and model
- `POST /api/tts`
- `GET`/`POST /api/rooms` to list/create rooms
- `POST /api/rooms/join`
- `GET /api/rooms/:id/state`
- `POST /api/rooms/:id/action`
- `GET /api/rooms/:id/stream` with WebSocket upgrade
- `POST /api/rooms/:id/leave`
