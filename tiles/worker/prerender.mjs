// Pre-render sharp SNOWPACK tiles in CI (GitHub Actions), so the Worker
// only has to SERVE them -- which fits the Cloudflare Workers free plan.
//
//   node prerender.mjs <exportDir> <outDir> --views ski6 --hours 24 [--procs 4]
//
// Picks the frames on the app's 2 h grid within +-hours/2 of now (or of the
// export's middle when "now" is outside it), the zoom 10-12 tiles that touch
// snow in that frame's overview PNG, and writes
//   <outDir>/<view>/<tag>/<z>/<x>/<y>.png
// plus <outDir>/sharp.json = {view: [tags]}  (what the app may ask for).
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fork, execFileSync } from 'node:child_process';
import { decode } from 'fast-png';
import { createRenderer, ZMIN, ZMAX } from './src/render.js';

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf('--' + k); return i >= 0 ? args[i + 1] : d; };
const [EXP, OUT] = args;
const VIEWS = opt('views', 'ski6').split(',').filter(Boolean);
const HOURS = +opt('hours', '24'), PROCS = +opt('procs', '4'), STEP_H = 2;

const man = JSON.parse(readFileSync(join(EXP, 'manifest.json'), 'utf8'));

function pickTags() {
  const ts = man.timestamps.map(t => Date.parse(t.slice(0, 16) + ':00Z'));
  const now = Date.now(), lo = ts[0], hi = ts[ts.length - 1];
  const anchor = now >= lo && now <= hi ? now : (lo + hi) / 2;
  const half = HOURS / 2 * 3600e3;
  return man.tags.filter((tag, i) => Math.abs(ts[i] - anchor) <= half
    && new Date(ts[i]).getUTCHours() % STEP_H === 0);
}
// zoom 10-12 tiles touching non-transparent pixels of the frame overview PNG
function tilesFor(view, tag) {
  const f = join(EXP, man.layers[view].file.replace('{tag}', tag));
  if (!existsSync(f)) return [];
  const im = decode(readFileSync(f)), W = im.width, H = im.height, ch = im.channels;
  const [[s, w], [n, e]] = man.bounds;
  const lonOf = c => w + (c + 0.5) / W * (e - w), latOf = r => n - (r + 0.5) / H * (n - s);
  const set = new Set();
  for (let r = 0; r < H; r += 1) for (let c = 0; c < W; c += 1) {
    if (ch === 4 && !im.data[(r * W + c) * 4 + 3]) continue;
    const lat = latOf(r), lon = lonOf(c), lr = lat * Math.PI / 180;
    for (let z = ZMIN; z <= ZMAX; z++) {
      const k = 2 ** z, x = (lon + 180) / 360 * k, y = (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * k;
      // one frame pixel is ~300 m: also take the neighbour tile it may reach into
      for (const dx of [-0.02, 0.02]) for (const dy of [-0.02, 0.02])
        set.add(z + '/' + Math.floor(x + dx * k / 4096) + '/' + Math.floor(y + dy * k / 4096));
    }
  }
  return [...set].map(s => s.split('/').map(Number));
}

async function work(jobs) {
  const bucketGet = async (key) => {
    const rel = key.replace(/^runs\/[^/]+\//, '');
    const f = join(EXP, rel);
    return existsSync(f) ? readFileSync(f) : null;
  };
  const fetchTerrain = async (key) => {
    const u = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/' + key + '.png';
    if (process.env.TERRAIN_CURL) return execFileSync('curl', ['-sf', '-m', '30', u]);
    const r = await fetch(u); if (!r.ok) throw new Error('terrain ' + r.status); return r.arrayBuffer();
  };
  const R = createRenderer({ getObject: bucketGet, fetchTerrain });
  let n = 0, empty = 0;
  for (const [view, tag, z, x, y] of jobs) {
    const dir = join(OUT, view, tag, String(z), String(x));
    const out = join(dir, y + '.png');
    if (existsSync(out)) continue;
    try {
      const { png, painted } = await R.renderTile('pre', view, tag, z, x, y);
      mkdirSync(dir, { recursive: true });
      writeFileSync(out, png);
      n++; if (!painted) empty++;
    } catch (e) { console.error(`  ${view} ${tag} ${z}/${x}/${y}: ${e.message}`); }
  }
  return { n, empty };
}

if (process.env.PRERENDER_CHILD) {
  process.on('message', async (jobs) => { process.send(await work(jobs)); process.exit(0); });
} else {
  const tags = pickTags();
  const jobs = [], sharp = {};
  for (const view of VIEWS) {
    if (!man.layers[view]) { console.log(`  ${view}: not in this export, skipped`); continue; }
    sharp[view] = tags;
    for (const tag of tags) for (const [z, x, y] of tilesFor(view, tag)) jobs.push([view, tag, z, x, y]);
  }
  console.log(`prerender: ${tags.length} frames (${tags[0]} .. ${tags[tags.length - 1]}), views ${Object.keys(sharp)}, ${jobs.length} tiles, ${PROCS} processes`);
  const t0 = Date.now();
  // group by terrain tile so each process reuses its decoded terrain
  jobs.sort((a, b) => (a[2] - b[2]) || (a[3] - b[3]) || (a[4] - b[4]));
  const parts = Array.from({ length: PROCS }, () => []);
  jobs.forEach((j, i) => parts[Math.floor(i * PROCS / jobs.length)].push(j));
  const res = await Promise.all(parts.map(p => new Promise((ok) => {
    if (!p.length) return ok({ n: 0, empty: 0 });
    const ch = fork(new URL(import.meta.url).pathname, args, { env: { ...process.env, PRERENDER_CHILD: '1' } });
    ch.on('message', ok); ch.send(p);
  })));
  const n = res.reduce((s, r) => s + r.n, 0), empty = res.reduce((s, r) => s + r.empty, 0);
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, 'sharp.json'), JSON.stringify(sharp));
  console.log(`prerender: ${n} tiles written (${empty} empty) in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
}
