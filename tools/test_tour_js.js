// Tour scoring in the ENGINE build -- the pure half that the native app shares.
// Mirrors tools/test_tour_score.py; the point of having both is that the JS and
// Python halves must agree, since the same route can be scored in either.
const fs = require('fs');
const path = process.argv[2] || 'dist/engine.js';
if (!fs.existsSync(path)) { console.error(`FAIL: ${path} not found — run a --split build first.`); process.exit(1); }
const E = require(require('path').resolve(path));

let fails = [];
const check = (name, cond, detail='') => {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name + (detail ? '  ' + detail : ''));
  if (!cond) fails.push(name);
};

console.log('exports');
for (const f of ['tourResample','tourAggregate','tourVerdict','tourDistM','tourIsDescent'])
  check(`${f} is exported`, typeof E[f] === 'function', typeof E[f]);
// App glue must NOT leak into the engine: it reads M, the rasters and the bulletin.
for (const f of ['tourSampleSeg','tourScoreRoute','tourOpen','tourBuildLayer'])
  check(`${f} is NOT in the engine (app glue)`, E[f] === undefined);

console.log('\ndistance');
const d = E.tourDistM(7.0, 46.5, 7.0, 46.6);
check('0.1 deg latitude is ~11.1 km', d > 11000 && d < 11200, `${d.toFixed(0)} m`);
check('identical points give zero', E.tourDistM(7,46,7,46) === 0);

console.log('\nresampling');
const segs = E.tourResample([[7.0,46.5],[7.02,46.5]], 100);
check('resampled to even spacing', segs.length >= 15, `${segs.length} segments`);
const gaps = segs.slice(1,-1).map((s,i) => Math.round(s.dist - segs[i].dist));
check('spacing uniform', new Set(gaps).size <= 1, `gaps ${[...new Set(gaps)]}`);
check('starts at zero', segs[0].dist === 0);
const dense = E.tourResample([[7.0,46.5],[7.0001,46.5],[7.0002,46.5],[7.02,46.5]], 100);
check('dense digitising does not inflate count', Math.abs(dense.length - segs.length) <= 1,
      `${dense.length} vs ${segs.length}`);
check('single point does not crash', E.tourResample([[7,46]], 100).length === 1);
check('empty does not crash', E.tourResample([], 100).length === 0);
check('garbage coords are filtered', E.tourResample([['a','b'],[null],[7,46]], 100).length <= 1);

console.log('\ndescent band');
check('22 deg is descent', E.tourIsDescent({slope:22}));
check('35 deg is descent', E.tourIsDescent({slope:35}));
check('5 deg is not (approach track)', !E.tourIsDescent({slope:5}));
check('60 deg is not (steep terrain)', !E.tourIsDescent({slope:60}));
check('missing slope is not', !E.tourIsDescent({slope:null}));

const mk = (qualities, slope=32, core=false, n=80) => {
  const out = [];
  for (let i = 0; i < n; i++) {
    const q = qualities[i % qualities.length];
    out.push({lon:7+i*0.001, lat:46.5, dist:i*100, elev:2000+i*5,
              slope, aspect:0, quality:q, powdered:q === 'powder', core});
  }
  return out;
};

console.log('\ndistribution, not a mean');
const r = E.tourAggregate(mk(['powder','powder','windpressed','suncrust']));
check('all three qualities present', Object.keys(r.distribution).sort().join(',') ===
      'powder,suncrust,windpressed', Object.keys(r.distribution).join(','));
const sum = Object.values(r.distribution).reduce((x,y)=>x+y,0);
check('shares sum to 1', Math.abs(sum - 1) < 1e-9, sum.toFixed(6));
check('powder share ~50 %', r.powderShare > 0.45 && r.powderShare < 0.55, r.powderShare.toFixed(2));
check('distribution ordered by share',
      JSON.stringify(Object.values(r.distribution)) ===
      JSON.stringify([...Object.values(r.distribution)].sort((x,y)=>y-x)));
check('elevation gain computed', r.gain > 0, `${r.gain} m`);

console.log('\nflat approach cannot dominate');
const flat = E.tourAggregate(mk(['powder'], 5));
check('flat yields no descent length', flat.descentM === 0, `${flat.descentM}`);
check('flat yields empty distribution', Object.keys(flat.distribution).length === 0);
check('flat says so', flat.verdict === 'keine Abfahrtsbewertung', flat.verdict);
check('very steep excluded too', E.tourAggregate(mk(['powder'], 60)).descentM === 0);

console.log('\nverdicts scale with powder share');
const cases = [[['powder'],'überwiegend Powder'],
               [['powder','windpressed'],'teilweise Powder'],
               [['powder','c','c','c','c'],'vereinzelt Powder'],
               [['crust'],'kein Powder erwartet']];
for (const [qs, want] of cases) {
  const got = E.tourAggregate(mk(qs)).verdict;
  check(`${qs.length} qualities -> ${want}`, got === want, `got ${got}`);
}

console.log('\nTHE SAFETY PROPERTY: core zone clamps the verdict');
const danger = E.tourAggregate(mk(['powder'], 32, true));
check('perfect powder in the core zone is NOT praised',
      danger.verdict === 'Kernzone betroffen – Bulletin zuerst', `got '${danger.verdict}'`);
check('clamped flag set', danger.clamped === true);
check('core share reported', Math.abs(danger.coreShare - 1) < 1e-9, danger.coreShare.toFixed(2));
check('powder share still reported honestly, not hidden', danger.powderShare > 0.9,
      danger.powderShare.toFixed(2));
check('caveat names the core zone', danger.caveats.some(c => c.includes('Kernzone')));
check('caveat always disclaims avalanche assessment',
      danger.caveats.some(c => c.includes('keine Lawinenbeurteilung')));
check('verdict helper clamps directly too',
      E.tourVerdict(1.0, 5000, true) === 'Kernzone betroffen – Bulletin zuerst');

console.log('\nclamp threshold');
const partial = share => mk(['powder']).map((s,i) => ({...s, core: i < Math.round(share*80)}));
const low = E.tourAggregate(partial(0.05)), high = E.tourAggregate(partial(0.40));
check('5 % does not clamp', !low.clamped, `share ${low.coreShare.toFixed(2)}`);
check('40 % clamps', high.clamped, `share ${high.coreShare.toFixed(2)}`);
check('unclamped tour still praises good snow', low.verdict === 'überwiegend Powder', low.verdict);

console.log('\ndegenerate input');
for (const [name, v] of [['null', null], ['empty', []]]) {
  const g = E.tourAggregate(v);
  check(`${name} -> safe default`, g && g.descentM === 0 && g.verdict === 'keine Abfahrtsbewertung');
}

console.log('\n' + (fails.length ? `FAILED: ${fails}` : 'TOUR JS OK'));
process.exit(fails.length ? 1 : 0);
