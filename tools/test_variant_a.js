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

// The pictogram table is a const object, not a function, so it needs its own
// extractor -- and pulling it from the real source is the point: a symbol
// added in the app without a test update should not silently pass.
function grabConst(name) {
  const i = src.indexOf('const ' + name + '=');
  if (i < 0) throw new Error(name + ' not found in app.js');
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (!d) return src.slice(i, k + 1) + ';'; }
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

// stubs for the app state the functions read. appState is shared by
// reference so a test can move the timeline and re-ask for the note.
let tagIdx = 0;
const appState = {times: ['2026-03-26T00:00', '2026-03-26T12:00']};
const sandbox = {
  vaProf, vaTagIndex: () => tagIdx,
  vaProfAvailable: () => !!(vaProf && vaProf.points && vaProf.points.length),
  // For the model-date stamp: a manifest whose window sits next to the
  // timeline, and the timeline state (M.times / b) it is compared against.
  vaMan: {timestamps: ['2026-03-26T00:00', '2026-03-26T12:00'],
          tags: ['2026-03-26T0000', '2026-03-26T1200']},
  vaAvailable: () => true,
  M: appState, b: 1,
  Date, JSON,
  escapeHtml: s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
  Float64Array, Math, Object, String, Array, isFinite, console,
};
// vaProfileHTML stamps the model-run date on the profile via vaNoteHTML, so
// both it and vaDataNote come along. They read vaMan/M/b, which the sandbox
// supplies below.
const fn = new Function(...Object.keys(sandbox),
  grab('vaProfileAt') + '\n' + grab('vaProfileHTML') + '\n'
  + grab('vaDataNote') + '\n' + grab('vaNoteHTML') + '\n'
  + grabConst('VA_GRAIN_ICON') + '\n' + grab('vaGrainIcon') + '\n'
  + grab('vaProfIndex')
  + '\nreturn {vaProfileAt, vaProfileHTML, vaDataNote, vaNoteHTML, vaGrainIcon,'
  + ' VA_GRAIN_ICON, vaProfIndex};');
const { vaProfileAt, vaProfileHTML, vaDataNote, vaNoteHTML, vaGrainIcon,
        VA_GRAIN_ICON, vaProfIndex } = fn(...Object.values(sandbox));

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

console.log('\nprofile axis is independent of the layer axis');
// Layers run at a fine step so the slider has frames to move through;
// profiles run coarser because each of those steps costs ~300 kB against
// ~100 kB for a frame. Reusing the layer index would read the wrong profile,
// or index past the end of the array.
{
  const saveMan = sandbox.vaMan;
  // 5 layer frames, 2 profile steps -- exactly the mismatch that breaks.
  sandbox.vaMan.timestamps = ['2026-03-26T00:00','2026-03-26T06:00','2026-03-26T12:00',
                              '2026-03-26T18:00','2026-03-27T00:00'];
  sandbox.vaMan.tags = ['2026-03-26T0000','2026-03-26T0600','2026-03-26T1200',
                        '2026-03-26T1800','2026-03-27T0000'];
  const n = vaProf.profiles.length;
  check('profile payload has fewer steps than the layer axis',
        vaProf.labels.length === n && n < sandbox.vaMan.tags.length,
        n + ' profiles vs ' + sandbox.vaMan.tags.length + ' frames');
  for (let i = 0; i < sandbox.vaMan.tags.length; i++) {
    tagIdx = i;
    const pi = vaProfIndex();
    if (pi < 0 || pi >= n) { check('layer frame ' + i + ' maps inside the profile array', false, 'got ' + pi); break; }
  }
  tagIdx = 4;
  check('every layer frame maps inside the profile array', vaProfIndex() < n,
        'frame 4 -> profile ' + vaProfIndex());
  tagIdx = 0;
  check('the earliest frame picks the earliest profile', vaProfIndex() === 0,
        String(vaProfIndex()));
  tagIdx = 4;
  check('a late frame picks the nearest later profile', vaProfIndex() === n - 1,
        String(vaProfIndex()));
  // And a profile read still works at a frame index the profile array lacks.
  tagIdx = 3;
  check('a profile is returned for an in-between frame', !!vaProfileAt(46.80, 9.83, 2400, 0));
  sandbox.vaMan = saveMan;
  tagIdx = 0;
}

console.log('\ngrain pictograms');
// Every grain class profiles.py can emit needs its own symbol, and they have
// to be distinguishable -- a shared glyph would be worse than none, because
// it looks like information.
{
  const codes = Object.keys(GRAIN).map(Number);
  const missing = codes.filter(c => !VA_GRAIN_ICON[c]);
  check('every grain class in the payload has a pictogram', missing.length === 0,
        missing.length ? 'missing ' + missing.join(',') : codes.length + ' classes');
  const shapes = codes.map(c => VA_GRAIN_ICON[c]);
  check('no two classes share a glyph', new Set(shapes).size === shapes.length,
        new Set(shapes).size + ' distinct of ' + shapes.length);
  const svg = vaGrainIcon(1);
  check('renders an inline svg', /^<svg /.test(svg) && /<\/svg>$/.test(svg));
  check('inherits colour so it works in both themes', /currentColor/.test(svg));
  check('hidden from screen readers (the code beside it carries the name)',
        /aria-hidden/.test(svg));
  check('an unknown class falls back rather than breaking',
        /^<svg /.test(vaGrainIcon(42)));
  // And the legend actually uses them.
  const html = vaProfileHTML(vaProfileAt(46.80, 9.83, 2400, 0));
  check('the profile legend shows pictogram and code together',
        /<svg class="va-gi"[\s\S]*?<\/svg>RG/.test(html));
}

console.log('\nmodel-run date stamp');
// The layer is offered in live mode too, where the exported window can be an
// entire season away from what the timeline shows. That has to be stated, not
// implied -- so the note carries the date and flags the gap.
{
  const near = vaDataNote();
  check('reports the exported model timestamp', !!near && /2026/.test(near.label), near && near.label);
  check('a window next to the timeline is not stale', near && near.stale === false,
        near && (near.days + 'd'));
  check('the note names the model run', /Modelllauf/.test(vaNoteHTML()));
  check('a fresh note carries no warning', !/Tage neben/.test(vaNoteHTML()));
}
{
  // Same export, but the app is showing a date months later.
  const saveT = appState.times;
  appState.times = ['2026-09-20T00:00', '2026-09-20T12:00'];
  const far = vaDataNote();
  check('a season-old window is flagged stale', !!far && far.stale === true, far && (far.days + 'd'));
  check('the gap is stated in days', /Tage neben der Zeitleiste/.test(vaNoteHTML()));
  appState.times = saveT;
}

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
