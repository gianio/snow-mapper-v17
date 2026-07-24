/**
 * Validates the snow-prognosis terrain-similarity model with synthetic test
 * data. The pure scoring functions are extracted straight out of the built
 * app.js, so this tests exactly what ships — no browser required.
 *
 *   node tools/test_model.js [path/to/app.js]
 */
const fs = require('fs');
const path = require('path');

const appPath = process.argv[2] || path.join(__dirname, '..', 'dist', 'app.js');
const src = fs.readFileSync(appPath, 'utf8');

const START = '// --- Terrain-similarity model';
const END = 'function renderPrognosis';
const i = src.indexOf(START), j = src.indexOf(END, i);
if (i < 0 || j < 0) {
  console.error('FAIL: could not locate the model block in ' + appPath);
  process.exit(1);
}
const model = src.slice(i, j);
const M = new Function(model + '\nreturn {progCell,progAspectMatch,progElevMatch,progEnvelope};')();

const LAT = 46.80, LNG = 9.83;
// the reference report: powder, N-facing, 1700-2300 m, 2 h old
const P = { type: 'powder', lat: LAT, lng: LNG, e0: 1700, e1: 2300, asp: 0, conc: 0.85, ageH: 2 };
const cell = (asp, elev, slope, lat, lng, sel, oth) =>
  M.progCell(asp, elev, slope, lat == null ? LAT : lat, lng == null ? LNG : lng, sel, oth);
const dLat = km => LAT + km / 111;

const R = {
  aspect_N:  cell(0,   2000, 30, null, null, [P], []),
  aspect_NE: cell(45,  2000, 30, null, null, [P], []),
  aspect_E:  cell(90,  2000, 30, null, null, [P], []),
  aspect_S:  cell(180, 2000, 30, null, null, [P], []),
  elev_in:   cell(0, 2000, 30, null, null, [P], []),
  elev_p200: cell(0, 2500, 30, null, null, [P], []),
  elev_p600: cell(0, 2900, 30, null, null, [P], []),
  dist_0:    cell(0, 2000, 30, dLat(0),  null, [P], []),
  dist_5:    cell(0, 2000, 30, dLat(5),  null, [P], []),
  dist_15:   cell(0, 2000, 30, dLat(15), null, [P], []),
  dist_40:   cell(0, 2000, 30, dLat(40), null, [P], []),
  single:        cell(0, 2000, 30, null, null, [P], []),
  twoSupportive: cell(0, 2000, 30, null, null, [P, Object.assign({}, P, { lat: dLat(4) })], []),
  withConflict:  cell(0, 2000, 30, null, null, [P], [Object.assign({}, P, { type: 'wet' })]),
  fresh:         cell(0, 2000, 30, null, null, [P], []),
  old:           cell(0, 2000, 30, null, null, [Object.assign({}, P, { ageH: 120 })], []),
  flatWrong:     cell(180, 2000, 5,  null, null, [P], []),
  steepWrong:    cell(180, 2000, 30, null, null, [P], []),
};
const L = k => R[k].like, C = k => R[k].conf;

const checks = [
  ['N-facing slope matches strongly',         L('aspect_N') > 0.55],
  ['adjacent sector (NE) partial, below N',   L('aspect_NE') < L('aspect_N') && L('aspect_NE') > 0.1],
  ['two sectors off (E) is weak',             L('aspect_E') < 0.25],
  ['opposite aspect (S) is excluded',         L('aspect_S') < 0.02],
  ['inside elevation band = full match',      M.progElevMatch(2000, 1700, 2300) === 1],
  ['200 m outside band is reduced',           L('elev_p200') < L('elev_in')],
  ['600 m outside band collapses',            L('elev_p600') < 0.1],
  ['likelihood falls with distance',          L('dist_0') > L('dist_5') && L('dist_5') > L('dist_15') && L('dist_15') > L('dist_40')],
  ['confidence falls with distance',          C('dist_0') >= C('dist_5') && C('dist_5') > C('dist_15') && C('dist_15') >= C('dist_40')],
  ['40 km away is negligible',                L('dist_40') < 0.05],
  ['2nd supportive report raises confidence', C('twoSupportive') > C('single')],
  ['conflicting report lowers confidence',    C('withConflict') < C('single')],
  ['fresh report outweighs a 5-day-old one',  C('fresh') > C('old')],
  ['flat ground is aspect-neutral',           L('flatWrong') > L('steepWrong')],
  ['confidence stays within 0-100',           Object.keys(R).every(k => C(k) >= 0 && C(k) <= 100)],
  ['likelihood stays within 0-1',             Object.keys(R).every(k => L(k) >= 0 && L(k) <= 1)],
];

for (const k of Object.keys(R)) {
  console.log(`  ${k.padEnd(14)} like=${L(k).toFixed(3)} conf=${C(k)}%`);
}
console.log('');
let ok = true;
for (const [name, good] of checks) {
  ok = ok && good;
  console.log(`  [${good ? 'PASS' : 'FAIL'}] ${name}`);
}
console.log('\nMODEL ' + (ok ? 'OK' : 'FAIL'));
process.exit(ok ? 0 : 1);
