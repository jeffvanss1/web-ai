/**
 * Resolving a playable audio URL without yt-dlp.
 *
 * yt-dlp is Python and cannot run in a Worker, so this asks YouTube's own
 * InnerTube `/player` endpoint the way yt-dlp does internally and picks an
 * audio-only stream out of the answer. That is the one piece of the Python app
 * that does not have an obvious Worker equivalent, which is why it gets its own
 * file and its own probe route: if this works from Cloudflare's IPs, the rest
 * of the app is portable, and if it does not, no amount of porting helps.
 */

const PLAYER = "https://www.youtube.com/youtubei/v1/player";

// Public InnerTube keys - these ship in YouTube's own web client, not secrets.
// Ordered by how likely each is to hand back a ready-to-fetch URL: the phone
// clients usually do, the web client usually wants the `n` parameter
// descrambled first (which needs the JS interpreter yt-dlp has and we don't).
const CLIENTS = [
  {
    name: "ANDROID",
    key: "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
    client: { clientName: "ANDROID", clientVersion: "19.09.37",
              androidSdkVersion: 30, hl: "en", gl: "US" },
  },
  {
    name: "IOS",
    key: "AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc",
    client: { clientName: "IOS", clientVersion: "19.09.3",
              deviceModel: "iPhone14,3", hl: "en", gl: "US" },
  },
  {
    name: "WEB",
    key: "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    client: { clientName: "WEB", clientVersion: "2.20240726.00.00",
              hl: "en", gl: "US" },
  },
];

const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

function audioFormats(data) {
  const sd = data && data.streamingData;
  if (!sd) return [];
  const all = [].concat(sd.adaptiveFormats || [], sd.formats || []);
  return all
    .filter((f) => f && typeof f.mimeType === "string" && f.mimeType.startsWith("audio/"))
    .filter((f) => !f.signatureCipher)      // a ciphered URL needs a descrambler
    .filter((f) => typeof f.url === "string" && f.url.startsWith("http"))
    .sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
}

/**
 * -> {ok, url, mimeType, bitrate, client, expiry, status}
 *    or {ok: false, why, status, detail, sawCiphered}
 *
 * `sawCiphered` is the interesting failure: it means the phone clients are
 * being throttled, which is what datacenter IPs get.
 */
export async function resolveAudio(videoId) {
  if (!VIDEO_ID.test(String(videoId || ""))) {
    return { ok: false, why: "not a video id", status: 0, detail: "" };
  }
  const notes = [];
  let sawCiphered = false;
  let last = { ok: false, why: "no client answered", status: 0, detail: "" };

  for (const c of CLIENTS) {
    let res;
    try {
      res = await fetch(`${PLAYER}?key=${c.key}&prettyPrint=false`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   "User-Agent": `${c.name} client`,
                   "X-Youtube-Client-Name": String(c.client.clientName) },
        body: JSON.stringify({
          videoId,
          context: { client: c.client },
          contentCheckOk: true,
          racyCheckOk: true,
        }),
      });
    } catch (e) {
      last = { ok: false, why: "network", status: 0, detail: String(e) };
      notes.push(`${c.name}: ${String(e)}`);
      continue;
    }
    const text = await res.text();
    if (!res.ok) {
      last = { ok: false, why: "http", status: res.status, detail: text.slice(0, 200) };
      notes.push(`${c.name}: HTTP ${res.status}`);
      continue;
    }
    let data;
    try { data = JSON.parse(text); } catch {
      last = { ok: false, why: "parse", status: res.status, detail: text.slice(0, 200) };
      continue;
    }
    const status = (data.playabilityStatus && data.playabilityStatus.status) || "";
    const reason = (data.playabilityStatus && data.playabilityStatus.reason) || "";
    if (status && status !== "OK") {
      last = { ok: false, why: status, status: res.status, detail: reason };
      notes.push(`${c.name}: ${status} ${reason}`);
      continue;
    }
    const formats = audioFormats(data);
    if (!formats.length) {
      const sd = data.streamingData || {};
      const ciphered = [].concat(sd.adaptiveFormats || [], sd.formats || [])
        .filter((f) => f && f.signatureCipher).length;
      if (ciphered) sawCiphered = true;
      last = { ok: false, why: "no audio format", status: res.status, detail: reason };
      notes.push(`${c.name}: ${ciphered} ciphered, 0 usable`);
      continue;
    }
    const best = formats[0];
    return {
      ok: true,
      url: best.url,
      mimeType: String(best.mimeType || "audio/mp4").split(";")[0],
      bitrate: best.bitrate || 0,
      approxBytes: Number(best.contentLength || 0),
      client: c.name,
      title: (data.videoDetails && data.videoDetails.title) || "",
      seconds: Number((data.videoDetails || {}).lengthSeconds || 0),
      notes,
    };
  }
  return Object.assign({}, last, { sawCiphered, notes });
}

/**
 * Stream a resolved URL to the browser, passing Range through so <audio>
 * seeking works. The whole point: the browser cannot fetch YouTube's CDN
 * itself (CORS + signed URL), so the Worker is the one thing that can.
 */
export async function streamAudio(url, rangeHeader) {
  const headers = {};
  if (rangeHeader) headers.Range = rangeHeader;
  const upstream = await fetch(url, { headers });
  if (!upstream.ok && upstream.status !== 206) {
    return new Response(`upstream answered ${upstream.status}`, { status: 502 });
  }
  const out = new Headers({
    "Content-Type": upstream.headers.get("content-type") || "audio/mp4",
    "Accept-Ranges": "bytes",
    "Cache-Control": "private, max-age=300",
  });
  for (const h of ["content-range", "content-length"]) {
    const v = upstream.headers.get(h);
    if (v) out.set(h, v);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
