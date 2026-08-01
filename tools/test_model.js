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

// The block spans report credibility (confirmations + author trust) through the
// terrain-similarity scoring, i.e. everything that turns reports into a map.
const START = '// --- Report credibility';
const END = 'function renderPrognosis';
const i = src.indexOf(START), j = src.indexOf(END, i);
if (i < 0 || j < 0) {
  console.error('FAIL: could not locate the model block in ' + appPath);
  process.exit(1);
}
const model = src.slice(i, j);
const EXPORTS = 'progCell,progAspectMatch,progElevMatch,progEnvelope,progReportWeight,progTrustOf';
// allReports is a free variable in the app; inject the fixture the test wants.
const mk = reports => new Function('allReports', model + '\nreturn {' + EXPORTS + '};')(reports || []);
const M = mk([]);

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

// --- Credibility weighting: confirmations on the post + author trust score ---
const rep = (id, uid, likes) => ({ id, userId: uid, likes });
// "anna" has 12 confirmations spread over her posts, "neu" is a new account.
const WORLD = [rep('a1', 'anna', 7), rep('a2', 'anna', 5), rep('n1', 'neu', 0)];
const Mw = mk(WORLD);
const wAnna = Mw.progReportWeight(rep('a1', 'anna', 7));
const wNeu  = Mw.progReportWeight(rep('n1', 'neu', 0));
const wNoOne = mk([]).progReportWeight({ likes: 0 });
// same terrain, same distance, same age - only credibility differs
const zw = w => Object.assign({}, P, { w });
const plain     = M.progCell(0, 2000, 30, LAT, LNG, [zw(1)], []);
const trusted   = M.progCell(0, 2000, 30, LAT, LNG, [zw(wAnna)], []);
// tug-of-war: a confirmed powder report against an unconfirmed 'wet' one
const conflict = t => M.progCell(0, 2000, 30, LAT, LNG,
  [Object.assign({}, P, { w: t })], [Object.assign({}, P, { type: 'wet', w: 1 })]);
const powderTrusted = conflict(wAnna), powderPlain = conflict(1);

console.log('');
console.log(`  weight: unknown author, 0 confirmations = ${wNoOne.toFixed(3)}`);
console.log(`  weight: new account, 0 confirmations    = ${wNeu.toFixed(3)}`);
console.log(`  weight: trusted author, 7 confirmations = ${wAnna.toFixed(3)}`);
console.log(`  trust score of "anna" = ${Mw.progTrustOf('anna')}, "neu" = ${Mw.progTrustOf('neu')}`);
console.log(`  conf: plain report ${plain.conf}%  ->  trusted report ${trusted.conf}%`);
console.log(`  vs a conflicting report: plain like ${powderPlain.like.toFixed(3)} -> trusted ${powderTrusted.like.toFixed(3)}`);

// --- The reported case: a slope that matches the report exactly, 2 km away ---
// "powder drawn N-facing 2000-2400 m; a north slope at 2000-2400 m two km off
// should read almost 100%". It used to read 79%, because a PERFECT aspect match
// was being discounted by the report's concentration.
const NEAR = { type:'powder', lat:LAT, lng:LNG, e0:2000, e1:2400, asp:0, conc:0.82, ageH:2 };
const twoKm = M.progCell(0, 2200, 32, LAT + 2/111, LNG, [NEAR], []);
const exactAsp = M.progAspectMatch(0, 0, 0.82);
console.log('');
console.log(`  exact aspect match (conc .82) = ${exactAsp.toFixed(3)}  (was 0.835)`);
console.log(`  matching N slope 2 km away    = ${(twoKm.like*100).toFixed(0)}% likely, ${twoKm.conf}% confident`);

checks.push(
  ['a perfect aspect match scores 1.0',        Math.abs(exactAsp - 1) < 1e-9],
  ['concentration widens tolerance, not peak', M.progAspectMatch(0,0,0.2) === M.progAspectMatch(0,0,0.95)],
  ['diffuse report tolerates more off-angle',  M.progAspectMatch(50,0,0.2) > M.progAspectMatch(50,0,0.95)],
  ['exact match 2 km away is near-certain',    twoKm.like >= 0.90 && twoKm.conf >= 90],
);

checks.push(
  ['unweighted report is exactly neutral (1.0)', wNoOne === 1],
  ['trust score sums confirmations per author',  Mw.progTrustOf('anna') === 12 && Mw.progTrustOf('neu') === 0],
  ['confirmed + trusted outweighs a new account', wAnna > wNeu],
  ['a new account is never penalised below 1',   wNeu >= 1],
  ['credibility is bounded (no runaway weight)', Mw.progReportWeight(rep('x', 'anna', 9999)) < 2.5],
  ['credibility raises confidence',              trusted.conf > plain.conf],
  ['credibility does not change terrain match',  Math.abs(trusted.like - plain.like) < 1e-9],
  ['credibility wins the agree/conflict tug',    powderTrusted.like > powderPlain.like],
);

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
