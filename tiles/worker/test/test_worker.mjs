// Worker test against a real export directory and real Terrarium tiles.
//
//   node test/test_worker.mjs [exportDir]     (default: ../../variant_a_export)
//
// Checks: routing, a rendered tile is a 256 px PNG with snow classes on it,
// the second request comes from the bucket, the pixels equal the app
// engine's own render, bad input is refused, PMTiles ranges work.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { decode } from 'fast-png';
import { handle } from '../src/index.js';
import { vaHiEngine } from '../src/va_engine.js';
import { fsBucket } from './r2_fs.mjs';

const EXP = resolve(process.argv[2] || new URL('../../../variant_a_export', import.meta.url).pathname);
const fails = [];
const check = (n, c, d = '') => { console.log((c ? '  [PASS] ' : '  [FAIL] ') + n + (d ? '  ' + d : '')); if (!c) fails.push(n); };

// Terrarium through curl (the sandbox proxy); the Worker itself uses fetch().
const realFetch = globalThis.fetch;
globalThis.fetch = async (u, opt) => {
  const s = String(u);
  if (s.includes('elevation-tiles-prod')) {
    const b = execFileSync('curl', ['-sf', '-m', '30', s]);
    return new Response(b, { status: 200 });
  }
  return realFetch(u, opt);
};

const man = JSON.parse(readFileSync(`${EXP}/manifest.json`, 'utf8'));
const tag = man.tags[Math.floor(man.tags.length / 2)];
const env = { BUCKET: fsBucket('r1', EXP) };
const get = (p, h = {}) => handle(new Request('https://tiles.test' + p, { headers: h }), env, null);

// Bernese Oberland, zoom 12
function tileOf(lat, lon, z) {
  const n = 2 ** z, lr = lat * Math.PI / 180;
  return [Math.floor((lon + 180) / 360 * n), Math.floor((1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * n)];
}
const z = 12, [x, y] = tileOf(46.56, 8.0, z);

console.log('routing');
check('health', (await get('/health')).status === 200);
check('unknown path -> 404', (await get('/nope')).status === 404);

console.log('render on demand');
const views = (man.layers.ski6 ? ['ski6', 'wind'] : []).concat(['powder', 'ski18', 'density']);
for (const view of views) {
  const t0 = Date.now();
  const r = await get(`/v1/r1/${view}/${tag}/${z}/${x}/${y}.png`);
  const ms = Date.now() - t0;
  const buf = new Uint8Array(await r.arrayBuffer());
  let im = null; try { im = decode(buf); } catch (e) {}
  let painted = 0; if (im) for (let i = 3; i < im.data.length; i += 4) if (im.data[i]) painted++;
  check(`${view}: 200, 256 px PNG with classes on it`, r.status === 200 && im && im.width === 256 && painted > 1000,
        `${r.status} ${buf.length} B, ${painted} px painted, ${ms} ms`);
  check(`${view}: cache headers`, /immutable/.test(r.headers.get('Cache-Control') || '') && r.headers.get('Access-Control-Allow-Origin') === '*');
}
const again = await get(`/v1/r1/ski18/${tag}/${z}/${x}/${y}.png`);
check('second request comes from the bucket, not a render', again.headers.get('X-Tile') === 'r2', again.headers.get('X-Tile'));

console.log('same pixels as the app engine');
{
  const r = await get(`/v1/r1/ski18/${tag}/11/${x >> 1}/${y >> 1}.png`);
  const im = decode(new Uint8Array(await r.arrayBuffer()));
  // the app's path: same engine, same inputs
  const pk = JSON.parse(readFileSync(`${EXP}/pack/index.json`, 'utf8'));
  const g = (f) => { const d = decode(readFileSync(`${EXP}/${f}`)); const n = d.width * d.height, o = new Uint8Array(n);
    for (let i = 0; i < n; i++) o[i] = d.data[i * d.channels]; return { w: d.width, h: d.height, data: o }; };
  const E = vaHiEngine();
  E.init(pk, g(pk.shade), pk.forest ? g(pk.forest.file) : null, pk.precip ? g(pk.precip.file) : null);
  const fr = decode(readFileSync(`${EXP}/${pk.file.replace('{tag}', tag)}`));
  const n = pk.runs.length, nm = pk.mets.length, per = pk.px_per_run, rp = pk.runs_per_row;
  const vals = new Float32Array(n * nm), ok = new Uint8Array(n);
  for (let k = 0; k < n; k++) {
    const p0 = (Math.floor(k / rp) * fr.width + (k % rp) * per) * fr.channels, bb = [];
    for (let p = 0; p < per; p++) for (let c = 0; c < 3; c++) bb.push(fr.data[p0 + p * fr.channels + c]);
    for (let m = 0; m < nm; m++) vals[k * nm + m] = bb[m] / pk.mul[m];
    ok[k] = bb[nm] ? 1 : 0;
  }
  E.setFrame(vals, ok);
  const tz = 11, tkey = `${tz}/${x >> 1}/${y >> 1}`;
  const tp = decode(execFileSync('curl', ['-sf', '-m', '30', `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${tkey}.png`]));
  const tb = new Uint8ClampedArray(256 * 256 * 4);
  for (let i = 0; i < 256 * 256; i++) { for (let c = 0; c < 3; c++) tb[i * 4 + c] = tp.data[i * tp.channels + c]; tb[i * 4 + 3] = 255; }
  const px = E.render({ z: 11, x: x >> 1, y: y >> 1, size: 256, layer: 'ski18', tkey, tz, tx: x >> 1, ty: y >> 1, tbytes: tb });
  let diff = 0; for (let i = 0; i < px.length; i++) if (px[i] !== im.data[i]) diff++;
  check('server tile == app engine render, byte for byte', diff === 0, diff + ' bytes differ');
}

console.log('bad input');
check('zoom outside 10-12 -> 400', (await get(`/v1/r1/ski18/${tag}/9/${x >> 3}/${y >> 3}.png`)).status === 400);
check('unknown view -> 400', (await get(`/v1/r1/nope/${tag}/${z}/${x}/${y}.png`)).status === 400);
check('unknown run -> 404', (await get(`/v1/zz/ski18/${tag}/${z}/${x}/${y}.png`)).status === 404);
check('errors are not cached', /no-store/.test((await get(`/v1/zz/ski18/${tag}/${z}/${x}/${y}.png`)).headers.get('Cache-Control')));

console.log('pmtiles ranges');
{
  await env.BUCKET.put('runs/r1/overview/ski18/' + tag + '.pmtiles', new TextEncoder().encode('0123456789abcdef'));
  const r = await get(`/v1/r1/overview/ski18/${tag}.pmtiles`, { Range: 'bytes=4-7' });
  const t = await r.text();
  check('range request -> 206 with the slice', r.status === 206 && t === '4567' && r.headers.get('Content-Range') === 'bytes 4-7/16',
        `${r.status} "${t}" ${r.headers.get('Content-Range')}`);
  check('whole file without a range', (await (await get(`/v1/r1/overview/ski18/${tag}.pmtiles`)).text()).length === 16);
}

console.log('\n' + (fails.length ? 'FAILED: ' + fails.join(', ') : 'TILE WORKER OK'));
process.exit(fails.length ? 1 : 0);
