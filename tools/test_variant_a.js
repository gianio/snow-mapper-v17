// Variant A consumer: the KNN profile interpolation and the manifest-driven
// legend, exercised against a payload shaped exactly like what
// variant_a/export.py and variant_a/profiles.py write.
//
// Those artifacts cannot be produced here (SNOWPACK is an external binary and
// its DEM is not in the repo), so this is a CONTRACT test: it pins the key
// names and shapes the client assumes, so a change on either side shows up
// as a failure rather than an empty layer in production.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2] || 'dist/app.js', 'utf8');

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found in app.js');
  let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (!d) return src.slice(i, k + 1); }
  }
  throw new Error(name + ' unbalanced');
}

let fails = [];
const check = (n, c, d='') => { console.log((c?'  [PASS] ':'  [FAIL] ')+n+(d?'  '+d:'')); if(!c) fails.push(n); };

// --- payload shaped per profiles.build_payload() --------------------------
const NB = 28;
const GRAIN = {0:["-",[220,220,220]],1:["PP",[168,216,240]],2:["DF",[150,220,150]],
  3:["RG",[116,196,118]],4:["FC",[246,215,75]],5:["DH",[253,141,60]],
  6:["SH",[227,119,194]],7:["MF",[148,103,189]],8:["IF",[99,99,99]],9:["FCxr",[200,150,60]]};
const mkProf = (dens, grain, hs) => ({hs, db: Array(NB).fill(dens), gb: Array(NB).fill(grain)});
const vaProf = {
  nb: NB, grain: GRAIN,
  labels: ['2026-03-26T00:00', '2026-03-26T12:00'],
  points: [
    {id:'p1', lat:46.80, lon:9.83, elev:2400, aspect:0,   slope:30, tile:'6'},
    {id:'p2', lat:46.81, lon:9.84, elev:2400, aspect:180, slope:30, tile:'6'},
    {id:'p3', lat:47.50, lon:7.50, elev:1000, aspect:0,   slope:25, tile:'2'},
    {id:'p4', lat:46.80, lon:9.83, elev:2400, aspect:0,   slope:30, tile:'6'},
  ],
  profiles: [
    [mkProf(200,3,120), mkProf(400,5,80), mkProf(150,1,30), mkProf(210,3,118)],
    [mkProf(220,4,115), mkProf(410,5,75), mkProf(160,1,25), mkProf(230,4,113)],
  ],
};

// stubs for the app state the functions read
let tagIdx = 0;
const sandbox = {
  vaProf, vaTagIndex: () => tagIdx,
  vaProfAvailable: () => !!(vaProf && vaProf.points && vaProf.points.length),
  escapeHtml: s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
  Float64Array, Math, Object, String, Array, isFinite, console,
};
const fn = new Function(...Object.keys(sandbox),
  grab('vaProfileAt') + '\n' + grab('vaProfileHTML') + '\nreturn {vaProfileAt, vaProfileHTML};');
const { vaProfileAt, vaProfileHTML } = fn(...Object.values(sandbox));

console.log('exact-point match');
let r = vaProfileAt(46.80, 9.83, 2400, 0);
check('returns a profile', !!r);
check('28 density bins', r.dens.length === NB, String(r.dens.length));
check('28 grain bins', r.grain.length === NB);
check('flagged as exact, not interpolated', r.exact === true);
// p1 and p4 sit on the same spot; the mean of 200 and 210 should dominate.
check('density near the co-located points', r.dens[0] > 195 && r.dens[0] < 235, r.dens[0].toFixed(1));
check('HS near those points', r.hs > 110 && r.hs < 125, String(r.hs));

console.log('\naspect matters');
const north = vaProfileAt(46.805, 9.835, 2400, 0);
const south = vaProfileAt(46.805, 9.835, 2400, 180);
check('north and south differ', Math.abs(north.dens[0] - south.dens[0]) > 20,
      `N=${north.dens[0].toFixed(0)} S=${south.dens[0].toFixed(0)}`);
check('south leans to the denser south point', south.dens[0] > north.dens[0],
      `S=${south.dens[0].toFixed(0)} > N=${north.dens[0].toFixed(0)}`);

console.log('\nelevation matters');
const high = vaProfileAt(47.00, 8.60, 2400, 0);
const low  = vaProfileAt(47.00, 8.60, 1000, 0);
check('elevation changes the result', Math.abs(high.dens[0] - low.dens[0]) > 1,
      `2400m=${high.dens[0].toFixed(0)} 1000m=${low.dens[0].toFixed(0)}`);

console.log('\ntimestep follows the timeline');
tagIdx = 1;
const later = vaProfileAt(46.80, 9.83, 2400, 0);
check('a different tag gives different values', Math.abs(later.dens[0] - r.dens[0]) > 5,
      `t0=${r.dens[0].toFixed(0)} t1=${later.dens[0].toFixed(0)}`);
check('tag index is clamped, not out of range', !!vaProfileAt(46.8, 9.83, 2400, 0));
tagIdx = 99;
check('out-of-range tag still returns something', !!vaProfileAt(46.8, 9.83, 2400, 0));
tagIdx = 0;

console.log('\nsnow-free points are skipped');
const saved = vaProf.profiles[0];
vaProf.profiles[0] = saved.map(p => ({...p, hs: 0}));
check('all-bare step yields null, not zeros', vaProfileAt(46.8, 9.83, 2400, 0) === null);
vaProf.profiles[0] = saved;

console.log('\ndegenerate input');
check('missing aspect is tolerated', !!vaProfileAt(46.8, 9.83, 2400, null));
check('missing elevation is tolerated', !!vaProfileAt(46.8, 9.83, null, 0));

console.log('\nHTML output');
const html = vaProfileHTML(vaProfileAt(46.80, 9.83, 2400, 0));
check('renders a section', html.includes('insp-sec') && html.includes('va-prof'));
check('shows HS', /HS \d+ cm/.test(html), (html.match(/HS \d+ cm/)||[''])[0]);
check('draws a density path', html.includes('<path d="M'));
check('draws grain bars', (html.match(/<rect /g)||[]).length === NB,
      String((html.match(/<rect /g)||[]).length));
check('grain legend uses the payload labels', html.includes('RG'));
check('density axis labels present', html.includes('450 kg/m'));
check('null profile renders nothing', vaProfileHTML(null) === '');

console.log('\n' + (fails.length ? `FAILED: ${fails}` : 'VARIANT A OK'));
process.exit(fails.length ? 1 : 0);
