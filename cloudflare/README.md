# Spotube DJ on Cloudflare

This is the cloud/browser backend. It replaces the localhost Python HTTP server, `mpv`, and local profile files with Worker-compatible services:

- Cloudflare Worker API
- D1 for tracks, queue, likes, and history
- Optional Cloudflare Access identity (`cf-access-authenticated-user-email`); guests can use the UI without logging in
- browser `<audio>` playback
- an external **browser-playable audio resolver** configured with `AUDIO_RESOLVER_URL`

The Worker intentionally does not run `yt-dlp` or proxy audio itself. A resolver must return JSON in the form `{ "url": "https://..." }`, with a URL that supports browser CORS and HTTP range requests. This boundary keeps the Worker runtime-compatible and makes the provider replaceable.

## Local development

```sh
cd cloudflare
npm install
npx wrangler d1 create spotube-dj
# put the returned database_id in wrangler.toml
npm run db:migrate
npx wrangler dev --local
```

Authentication is optional for the public UI. Unauthenticated browsers receive a private guest cookie and get their own queue, likes, and history. Cloudflare Access can be placed in front of the Worker later; logged-in users then get an account-scoped identity instead of the guest cookie.

## Deploy

```sh
npx wrangler d1 migrations apply spotube-dj --remote
npx wrangler secret put AUDIO_RESOLVER_URL
npx wrangler deploy
```

Protect the Worker/Pages hostname with Cloudflare Access. Set `ALLOWED_ORIGIN` to the Pages URL. The current Python app remains available while this cloud surface is migrated; this folder is deliberately isolated so the desktop/local player cannot regress.

## API

- `GET /api/state` — current user queue and likes
- `GET /api/search?q=...` — YouTube Data API v3 metadata search via `fetch` (uses the `YOUTUBE_API_KEY` Worker secret)
- `POST /api/action` — `queue`, `remove`, `like`, `unlike`, `played`, `clear`
- `POST /api/resolve` — asks the configured resolver for a playable URL

The browser owns play/pause/seek and reports `played` back to the Worker. No server process, global queue, filesystem, or local audio device is required.
