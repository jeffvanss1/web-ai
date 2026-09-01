# Cloudflare Worker deployment

This directory is an edge-compatible deployment of the project for a **managed
Cloudflare Worker**, not a VPS or a computer that has to stay switched on.

It is intentionally a separate runtime from `spotube_dj/`:

- Cloudflare Workers cannot run this repository's `http.server`, background
  `threading`, Unix sockets, `subprocess`, `mpv`, or the `yt-dlp` executable.
- The Worker in `worker/src/index.ts` calls YouTube Music's metadata endpoint,
  keeps a small queue and taste profile in D1, and exposes JSON routes.
- The browser plays the selected YouTube video through the official YouTube
  IFrame Player API. The Worker does **not** download, extract, or proxy audio.
- The Python app remains available for the full local `mpv`/`yt-dlp` experience.

This is the right shape for an edge Worker. If exact parity with the Python app,
server-side stream resolution, or server-side audio is required, use a managed
container such as Cloud Run instead; that is not an edge Worker deployment.

## What is included

```text
worker/
  src/index.ts             Worker API, D1 state, YT Music search, auth
  public/index.html        browser UI and YouTube IFrame player
  migrations/0001_initial.sql
  wrangler.jsonc
  package.json
```

The deployment is a private, single-profile DJ. `APP_PASSWORD` protects the
queue and taste profile. Without it, the Worker is public and anybody with the
URL can change the queue.

## Deploy it

Install Node.js, then from the repository root:

```bash
cd worker
npm install
npx wrangler login
npx wrangler d1 create spotube-dj
```

Copy the `database_id` printed by Wrangler into `worker/wrangler.jsonc`, replacing
`REPLACE_WITH_THE_ID_FROM_WRANGLER_D1_CREATE`. Then create the table remotely:

```bash
npm run db:migrate:remote
```

Set a password and a separate signing secret. Wrangler stores these as encrypted
Worker secrets; do not put either value in `wrangler.jsonc` or commit `.dev.vars`.

```bash
npx wrangler secret put APP_PASSWORD
npx wrangler secret put SESSION_SECRET
# Optional; the Worker works without a Gemini key.
# npx wrangler secret put GEMINI_API_KEY
npm run deploy
```

Wrangler prints the public `workers.dev` URL. Open it in a browser, enter the
password, search, and press **Make a mix**. The YouTube player may require the
first Play click because browsers block unsolicited autoplay.

A custom domain can be attached later in the Cloudflare dashboard without adding
a reverse proxy or running a server yourself.

## Local Worker development

```bash
cd worker
cp .dev.vars.example .dev.vars
npm run db:migrate:local
npm run dev
```

Open the local URL Wrangler prints. The local D1 database is disposable; the
remote profile is unaffected. `npm run typecheck` checks the Worker without
connecting to Cloudflare.

## Runtime boundaries

The Worker intentionally does not use:

- `mpv`, `playerctl`, `ffmpeg`, `yt-dlp`, or a Unix audio device;
- signed `googlevideo` stream URLs or an audio proxy;
- the Python `web.py` server or its local JSON files;
- Spotify playback APIs.

The browser player uses YouTube's embedded player and the queue contains video
IDs and metadata only. This avoids pretending that an edge isolate is a Linux
machine and avoids storing or forwarding extracted audio. YouTube playback,
availability, ads, regional restrictions, and applicable YouTube terms still
apply.

D1 stores one shared state row for this private deployment. If several unrelated
users need separate profiles, add identity-based keys (Cloudflare Access or an
OIDC identity) and use a Durable Object for per-user queue serialization before
making the app multi-tenant.

The Worker performs metadata searches during an action request. It has no Python
background thread, so it refills when the user presses a control rather than
running the Python DJ's continuous server-side auto-advance loop. The browser's
`ENDED` event asks the Worker for the next track.

## API routes

- `GET /healthz` — unauthenticated health check
- `GET /api/auth` — whether password protection is enabled
- `POST /api/login` / `POST /api/logout`
- `GET /api/state`
- `GET` or `POST /api/search?q=...`
- `POST /api/action` with `request`, `play`, `pause`, `resume`, `next`, `ended`,
  `play_row`, `enqueue`, `like`, `unlike`, `remove`, `progress`, `shuffle`,
  `repeat`, or `clear_queue`

## Why not upload the Python folder to Workers?

Even though Cloudflare has a Python Workers runtime, this application depends on
features that are not available in that isolate: its HTTP server needs sockets,
its queue and cache use threads, and playback launches native programs. Porting it
means replacing the server and state model with Fetch/D1/Durable Objects and
replacing playback with a browser player. That replacement is what the `worker/`
scaffold provides; it is not a hidden attempt to run `spotube_dj` unchanged.
