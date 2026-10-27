// Cloudflare Worker: SNOWPACK tiles for the app.
//
//   GET /v1/<run>/<view>/<tag>/<z>/<x>/<y>.png   sharp tile, zoom 10-12
//   GET /v1/<run>/overview/<view>/<tag>.pmtiles  overview archive (ranges)
//   GET /health
//
// A sharp tile is rendered once, on its first request, with the app's own
// engine (render.js), then kept in R2 and in Cloudflare's edge cache. Every
// later request -- by anyone -- is a cache hit. Runs are immutable (the run
// id is part of the URL), so everything is cached for a year.
//
// Pre-rendered frames (CI, tiles/worker/prerender.mjs) upload only the tiles
// with snow on them; the frames are listed in runs/<run>/cache/sharp.json. A
// tile of such a frame that is not in the bucket is empty: it is answered
// with a transparent tile, never rendered (the free plan could not).
//
// Bindings (wrangler.toml): BUCKET = the R2 bucket the pipeline uploads to.
import { encode } from 'fast-png';
import { createRenderer, SIZE, ZMIN, ZMAX } from './render.js';

const TERRAIN = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/';
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Range, If-None-Match',
  'Access-Control-Expose-Headers': 'Content-Range, Content-Length, ETag, X-Tile',
};
const IMMUTABLE = 'public, max-age=31536000, immutable';
let renderer = null;

function rendererFor(env) {
  if (renderer) return renderer;
  renderer = createRenderer({
    getObject: async (key) => { const o = await env.BUCKET.get(key); return o ? o.arrayBuffer() : null; },
    // Terrarium tiles never change: let Cloudflare cache them too.
    fetchTerrain: async (key) => {
      const r = await fetch(TERRAIN + key + '.png', { cf: { cacheTtl: 2592000, cacheEverything: true } });
      if (!r.ok) throw new Error('terrain ' + r.status);
      return r.arrayBuffer();
    },
  });
  return renderer;
}

let blank = null;
function blankPng() {
  return blank || (blank = encode({ width: SIZE, height: SIZE, data: new Uint8Array(SIZE * SIZE * 4), channels: 4, depth: 8 }));
}

// run -> {view: [tags]} of its pre-rendered frames. Runs are immutable, so a
// found list is kept; a missing one only briefly (the upload may be under way).
const sharpOf = new Map();
async function sharpFrames(env, run) {
  const c = sharpOf.get(run);
  if (c && (c.list || Date.now() - c.at < 60e3)) return c.list;
  let list = null;
  try { const o = await env.BUCKET.get(`runs/${run}/cache/sharp.json`); if (o) list = JSON.parse(await o.text()); } catch (e) {}
  sharpOf.set(run, { list, at: Date.now() });
  return list;
}

function resp(body, status, headers) {
  return new Response(body, { status, headers: { ...CORS, ...headers } });
}

// Range requests for the PMTiles archives (the client reads a header, then
// single tiles out of the file).
async function pmtiles(env, key, request) {
  const range = request.headers.get('Range');
  let opt = {};
  const m = range && /^bytes=(\d+)-(\d*)$/.exec(range);
  if (m) {
    const off = +m[1], end = m[2] ? +m[2] : undefined;
    opt = { range: end !== undefined ? { offset: off, length: end - off + 1 } : { offset: off } };
  }
  const o = await env.BUCKET.get(key, opt);
  if (!o) return resp('not found', 404, {});
  const h = { 'Content-Type': 'application/octet-stream', 'Cache-Control': IMMUTABLE, 'ETag': o.httpEtag };
  if (m && o.range) {
    const off = o.range.offset || 0, len = o.range.length !== undefined ? o.range.length : o.size - off;
    h['Content-Range'] = `bytes ${off}-${off + len - 1}/${o.size}`;
    return resp(o.body, 206, h);
  }
  return resp(o.body, 200, h);
}

export async function handle(request, env, ctx) {
  if (request.method === 'OPTIONS') return resp(null, 204, {});
  if (request.method !== 'GET' && request.method !== 'HEAD') return resp('method', 405, {});
  const url = new URL(request.url);
  if (url.pathname === '/health') return resp('ok', 200, { 'Content-Type': 'text/plain' });

  let m = /^\/v1\/([\w.-]+)\/overview\/(\w+)\/([\w-]+)\.pmtiles$/.exec(url.pathname);
  if (m) return pmtiles(env, `runs/${m[1]}/overview/${m[2]}/${m[3]}.pmtiles`, request);

  m = /^\/v1\/([\w.-]+)\/(\w+)\/([\w-]+)\/(\d+)\/(\d+)\/(\d+)\.png$/.exec(url.pathname);
  if (!m) return resp('not found', 404, {});
  const [, run, view, tag] = m, z = +m[4], x = +m[5], y = +m[6];

  const cache = (typeof caches !== 'undefined') ? caches.default : null;
  const ckey = new Request(url.origin + url.pathname);
  if (cache) { const hit = await cache.match(ckey); if (hit) return hit; }

  const rkey = `runs/${run}/cache/${view}/${tag}/${z}/${x}/${y}.png`;
  const hdr = { 'Content-Type': 'image/png', 'Cache-Control': IMMUTABLE };
  const stored = await env.BUCKET.get(rkey);
  if (stored) {
    const r = resp(stored.body, 200, { ...hdr, 'X-Tile': 'r2' });
    if (cache && ctx) ctx.waitUntil(cache.put(ckey, r.clone()));
    return r;
  }
  const pre = await sharpFrames(env, run);
  if (pre && (pre[view] || []).includes(tag) && z >= ZMIN && z <= ZMAX) {
    const r = resp(blankPng(), 200, { ...hdr, 'X-Tile': 'empty' });
    if (cache && ctx) ctx.waitUntil(cache.put(ckey, r.clone()));
    return r;
  }
  try {
    const { png } = await rendererFor(env).renderTile(run, view, tag, z, x, y);
    const r = resp(png, 200, { ...hdr, 'X-Tile': 'rendered' });
    const save = Promise.all([
      env.BUCKET.put(rkey, png, { httpMetadata: { contentType: 'image/png' } }),
      cache ? cache.put(ckey, r.clone()) : null,
    ]);
    if (ctx) ctx.waitUntil(save); else await save;
    return r;
  } catch (e) {
    // The app falls back to rendering on the device on any error here.
    return resp(String(e && e.message || e), e && e.status || 503,
                { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
  }
}

// Punctual live cycles. GitHub's own cron starts the Variant A live workflow
// up to hours late, and sometimes not at all; this Cron Trigger (wrangler.toml,
// free plan) starts it on the minute instead, with cron=true so it counts as
// a scheduled cycle. The workflow's guard job stands the later of the two
// down. Needs the secret GH_DISPATCH_TOKEN (a fine-grained token for this
// repository with Actions: read and write); without it nothing happens.
async function scheduled(event, env) {
  if (!env.GH_DISPATCH_TOKEN) return;
  const repo = env.GH_REPO || 'gianio/snow-mapper-v17';
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/variant-a-live.yml/dispatches`, {
    method: 'POST',
    headers: {
      Authorization: 'Bearer ' + env.GH_DISPATCH_TOKEN, Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'snowmapper-tiles-cron',
    },
    body: JSON.stringify({ ref: env.GH_REF || 'main', inputs: { cron: 'true' } }),
  });
  if (!r.ok) console.log('dispatch failed', r.status, (await r.text()).slice(0, 300));
}

export default { fetch: handle, scheduled };
