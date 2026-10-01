// Node side of tools/test_va_engine_parity.py: runs the app's own
// vaHiEngine on the fixture's cells and compares with the Python classes.
const fs = require('fs');
const fx = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const src = fs.readFileSync(process.argv[3] || 'dist/app.js', 'utf8');
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found in app.js');
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (!d) return src.slice(i, k + 1); }
  }
  throw new Error(name + ' unbalanced');
}
const vaHiEngine = new Function(grab('vaHiEngine') + '\nreturn vaHiEngine;')();
let fails = [];
const check = (n, c, d = '') => { console.log((c ? '  [PASS] ' : '  [FAIL] ') + n + (d ? '  ' + d : '')); if (!c) fails.push(n); };

const E = vaHiEngine();
const sh = { w: fx.shade.w, h: fx.shade.h, data: Uint8Array.from(fx.shade.data) };
E.init(fx.pack, sh, null);
E.setFrame(Float32Array.from(fx.vals), Uint8Array.from(fx.ok));
const m = new Float64Array(E.nm);
let nOk = 0, ski = 0, sim = 0, miss = [];
const t0 = Date.now();
for (const s of fx.samples) {
  const [e, n, elev, slope, aspect, shade, pySki, pySim] = s;
  const cand = E.candidates(e, n, 60000);
  if (!E.evalAt(e, n, elev, slope, aspect, shade, cand, m)) continue;
  nOk++;
  const a = E.clsSki(m), b = E.clsSimple(m);
  if (a === pySki) ski++; else if (miss.length < 5) miss.push(`ski ${a} vs py ${pySki}`);
  if (b === pySim) sim++;
}
const N = fx.samples.length;
console.log('engine parity (' + N + ' cells, ' + (Date.now() - t0) + ' ms)');
check('every modelled cell evaluates', nOk === N, nOk + '/' + N);
check('ski18 class agrees with the pipeline', ski / N >= 0.985, (100 * ski / N).toFixed(1) + '% ' + miss.join('; '));
check('simple class agrees with the pipeline', sim / N >= 0.985, (100 * sim / N).toFixed(1) + '%');

// render(): one tile over the fixture grid, flat 2000 m terrain
{
  const G = fx.pack.grid;
  // grid centre -> WGS84 by inverting E.wgs2lv03 numerically
  const ce = G.xll + G.nc * G.cs / 2, cn = G.yll + G.nr * G.cs / 2;
  let lat = 46.5, lon = 7.5;
  for (let it = 0; it < 50; it++) {
    const [e, n] = E.wgs2lv03(lat, lon);
    lon += (ce - e) / 76000; lat += (cn - n) / 111000;
  }
  const z = 12, nn = 2 ** z;
  const x = Math.floor((lon + 180) / 360 * nn);
  const y = Math.floor((1 - Math.log(Math.tan(lat * Math.PI / 180) + 1 / Math.cos(lat * Math.PI / 180)) / Math.PI) / 2 * nn);
  const tb = new Uint8ClampedArray(256 * 256 * 4);
  const v = 2000 + 32768;
  for (let i = 0; i < 256 * 256; i++) { tb[i * 4] = v >> 8; tb[i * 4 + 1] = v & 255; tb[i * 4 + 3] = 255; }
  const t1 = Date.now();
  const px = E.render({ z, x, y, size: 256, layer: 'ski18', tkey: 'k', tz: z, tx: x, ty: y, tbytes: tb });
  const ms = Date.now() - t1;
  let painted = 0; for (let i = 3; i < px.length; i += 4) if (px[i]) painted++;
  check('render() paints a tile inside the model area', painted > 1000, painted + ' px in ' + ms + ' ms');
  const px2 = E.render({ z, x, y, size: 256, layer: 'density', tkey: 'k', tz: z, tx: x, ty: y, tbytes: tb });
  let p2 = 0; for (let i = 3; i < px2.length; i += 4) if (px2[i]) p2++;
  check('density layer renders too', p2 > 1000, p2 + ' px');
}
// precipitation pattern: same scaling as variant_a/precip.apply()
{
  const G = fx.pack.grid, x = E.mi;
  const e = G.xll + 10.5 * G.cs, n = G.yll + (G.nr - 10.5) * G.cs;   // centre of cell (10, 10)
  const m2 = new Float64Array(E.nm);
  m2[x.powder_depth_cm] = 20; m2[x.total_hs_cm] = 100;
  E.adjPrecip(m2, e, n);
  check('no precip raster -> nothing changes', m2[x.powder_depth_cm] === 20 && m2[x.total_hs_cm] === 100);
  const data = new Uint8Array(G.nr * G.nc).fill(255);
  for (let r = 8; r <= 12; r++) for (let c = 8; c <= 12; c++) data[r * G.nc + c] = 150;
  E.precip = { w: G.nc, h: G.nr, data }; E.pk.precip = { file: 'x', cs: G.cs };
  E.adjPrecip(m2, e, n);
  check('R = 1.5 scales powder x1.5 and moves HS by the same', Math.abs(m2[x.powder_depth_cm] - 30) < 1e-6
        && Math.abs(m2[x.total_hs_cm] - 110) < 1e-6, m2[x.powder_depth_cm] + ' / ' + m2[x.total_hs_cm]);
  const m3 = new Float64Array(E.nm); m3[x.powder_depth_cm] = 20; m3[x.total_hs_cm] = 100;
  E.adjPrecip(m3, G.xll + 30.5 * G.cs, G.yll + (G.nr - 30.5) * G.cs);
  check('255 (no correction) leaves the cell alone', m3[x.powder_depth_cm] === 20);
  E.precip = null; E.pk.precip = undefined;
}
console.log('\n' + (fails.length ? 'FAILED: ' + fails : 'VA ENGINE OK'));
process.exit(fails.length ? 1 : 0);
