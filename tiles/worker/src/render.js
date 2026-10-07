// SNOWPACK map tiles, rendered on the server with the app's own engine.
//
// Platform-free on purpose: the Cloudflare Worker (index.js), the Node test
// and the local dev server all hand in two functions --
//   getObject(key)    -> ArrayBuffer | null   (a file of an uploaded export)
//   fetchTerrain(key) -> ArrayBuffer          (a Terrarium PNG "z/x/y")
// -- so moving to Cloud Run or a VM later only means another small adapter.
import { decode, encode } from 'fast-png';
import { vaHiEngine } from './va_engine.js';

export const VIEWS = ['ski6', 'powder', 'wind', 'density', 'ski18'];
export const ZMIN = 10, ZMAX = 12, TERR_Z = 12, SIZE = 256;

// PNG -> {w, h, ch, data} (channels as stored: 1 gray, 3 RGB, 4 RGBA)
function png(buf) {
  const im = decode(new Uint8Array(buf));
  return { w: im.width, h: im.height, ch: im.channels, data: im.data };
}
function gray(im) {
  if (im.ch === 1) return { w: im.w, h: im.h, data: im.data };
  const n = im.w * im.h, d = new Uint8Array(n);
  for (let i = 0; i < n; i++) d[i] = im.data[i * im.ch];
  return { w: im.w, h: im.h, data: d };
}
function rgba(im) {
  if (im.ch === 4) return im.data;
  const n = im.w * im.h, d = new Uint8ClampedArray(n * 4);
  for (let i = 0; i < n; i++) {
    for (let c = 0; c < 3; c++) d[i * 4 + c] = im.data[i * im.ch + Math.min(c, im.ch - 1)];
    d[i * 4 + 3] = 255;
  }
  return d;
}
// Same unpacking as the app's vaPkFrame(): mets as bytes, then an ok flag.
function unpackFrame(pk, im) {
  const n = pk.runs.length, nm = pk.mets.length, per = pk.px_per_run, rp = pk.runs_per_row;
  const vals = new Float32Array(n * nm), ok = new Uint8Array(n), b = new Uint8Array(per * 3);
  for (let r = 0; r < n; r++) {
    const p0 = (Math.floor(r / rp) * im.w + (r % rp) * per) * im.ch;
    for (let p = 0; p < per; p++) {
      const o = p0 + p * im.ch;
      b[p * 3] = im.data[o]; b[p * 3 + 1] = im.data[o + 1]; b[p * 3 + 2] = im.data[o + 2];
    }
    for (let m = 0; m < nm; m++) vals[r * nm + m] = b[m] / pk.mul[m];
    ok[r] = b[nm] ? 1 : 0;
  }
  return { vals, ok };
}

class LRU {
  constructor(n) { this.n = n; this.m = new Map(); }
  get(k) { if (!this.m.has(k)) return undefined; const v = this.m.get(k); this.m.delete(k); this.m.set(k, v); return v; }
  set(k, v) { this.m.delete(k); this.m.set(k, v); while (this.m.size > this.n) this.m.delete(this.m.keys().next().value); }
}

export function createRenderer({ getObject, fetchTerrain }) {
  // Per instance (a Worker isolate lives for many requests): one engine per
  // export run, a few decoded frames and terrain tiles.
  const runs = new LRU(2), frames = new LRU(6), terr = new LRU(24);

  async function need(key) {
    const b = await getObject(key);
    if (!b) throw Object.assign(new Error('missing ' + key), { status: 404 });
    return b;
  }
  async function runCtx(run) {
    let c = runs.get(run);
    if (c) return c;
    c = (async () => {
      const pk = JSON.parse(new TextDecoder().decode(await need(`runs/${run}/pack/index.json`)));
      const sh = gray(png(await need(`runs/${run}/${pk.shade}`)));
      const opt = async (f) => { if (!f) return null; const b = await getObject(`runs/${run}/${f}`); return b ? gray(png(b)) : null; };
      const fo = await opt(pk.forest && pk.forest.file), pr = await opt(pk.precip && pk.precip.file);
      const E = vaHiEngine();
      E.init(pk, sh, fo, pr);
      return { pk, E };
    })();
    runs.set(run, c);
    try { return await c; } catch (e) { runs.m.delete(run); throw e; }
  }
  async function frame(run, pk, tag) {
    const k = run + '|' + tag;
    let f = frames.get(k);
    if (!f) {
      f = need(`runs/${run}/${pk.file.replace('{tag}', tag)}`).then(b => unpackFrame(pk, png(b)));
      frames.set(k, f);
    }
    try { return await f; } catch (e) { frames.m.delete(k); throw e; }
  }
  async function terrain(key) {
    let t = terr.get(key);
    if (!t) { t = fetchTerrain(key).then(b => rgba(png(b))); terr.set(key, t); }
    try { return await t; } catch (e) { terr.m.delete(key); throw e; }
  }

  // -> {png: Uint8Array, painted: n} or throws ({status} on bad input)
  async function renderTile(run, view, tag, z, x, y) {
    if (!VIEWS.includes(view)) throw Object.assign(new Error('view'), { status: 400 });
    if (!(z >= ZMIN && z <= ZMAX)) throw Object.assign(new Error('zoom'), { status: 400 });
    if (!/^\d{4}-\d{2}-\d{2}T\d{4}$/.test(tag)) throw Object.assign(new Error('tag'), { status: 400 });
    const { pk, E } = await runCtx(run);
    const tz = Math.min(z, TERR_Z), f = 2 ** (z - tz), tx = Math.floor(x / f), ty = Math.floor(y / f);
    const tkey = `${tz}/${tx}/${ty}`;
    const [fr, tb] = await Promise.all([frame(run, pk, tag), terrain(tkey)]);
    E.setFrame(fr.vals, fr.ok);
    const px = E.render({ z, x, y, size: SIZE, layer: view, tkey, tz, tx, ty, tbytes: tb });
    let painted = 0;
    for (let i = 3; i < px.length; i += 4) if (px[i]) painted++;
    const out = encode({ width: SIZE, height: SIZE, data: px, channels: 4, depth: 8 });
    return { png: out, painted };
  }
  return { renderTile };
}
