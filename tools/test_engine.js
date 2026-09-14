/**
 * Validates the standalone scoring engine (dist/engine.js, emitted by
 * pipeline/interactive_export.py::_engine_js).
 *
 * test_model.js checks the MATHS by slicing the model out of app.js. This
 * checks the PACKAGING: that the emitted module actually loads with no DOM and
 * no globals, exposes the documented surface, and returns the same answers as
 * the in-app code. That matters because this exact file is what a native iOS
 * app runs in JavaScriptCore — if it only worked inside the browser bundle, the
 * "one engine, two platforms" plan would be a fiction.
 *
 *   node tools/test_engine.js [path/to/engine.js]
 */
const fs = require('fs');
const path = require('path');

const enginePath = process.argv[2] || path.join(__dirname, '..', 'dist', 'engine.js');
if (!fs.existsSync(enginePath)) {
  console.error(`FAIL: ${enginePath} not found — run a --split build first.`);
  process.exit(1);
}

// Load it the way JavaScriptCore would: evaluate the file, take the global it
// defines. No require() of app internals, no DOM shims, nothing injected.
const src = fs.readFileSync(enginePath, 'utf8');
const sandbox = {};
let E;
try {
  // `globalThis` inside the module resolves to our sandbox object, so the UMD
  // wrapper's browser branch runs and assigns SnowEngine onto it.
  new Function('globalThis', 'module', 'exports', src)(sandbox, undefined, undefined);
  E = sandbox.SnowEngine;
} catch (e) {
  console.error('FAIL: engine.js threw while loading outside a browser:', e.message);
  process.exit(1);
}
if (!E) {
  console.error('FAIL: engine.js loaded but defined no SnowEngine global.');
  process.exit(1);
}

const EXPECTED = ['progCell', 'progEnvelope', 'progAspectMatch', 'progElevMatch',
  'progSlopeMatch', 'progRecency', 'progDistKm', 'progReportWeight',
  'progTrustOf', 'progTrustMap', 'progInvalidateTrust', 'setReports', 'getReports'];

const LAT = 46.80, LNG = 9.83;
const P = { type: 'powder', lat: LAT, lng: LNG, e0: 1700, e1: 2300, asp: 0, conc: 0.85, ageH: 2 };
const cell = (asp, elev, slope, lat, sel, oth) =>
  E.progCell(asp, elev, slope, lat == null ? LAT : lat, LNG, sel, oth || []);

const checks = [];
const add = (name, ok) => checks.push([name, !!ok]);

add('module loads with no DOM and no injected globals', true);
for (const fn of EXPECTED) add(`exports ${fn}()`, typeof E[fn] === 'function');

// progZones is app glue, not model — it must NOT be part of the surface, or a
// consumer would call it and get an app-state crash at runtime.
add('does NOT export progZones (app glue)', typeof E.progZones === 'undefined');

// Same scenarios as test_model.js, so a packaging change that altered the
// maths would show up here rather than being discovered on a phone.
const N = cell(0, 2000, 30, null, [P]);
const NE = cell(45, 2000, 30, null, [P]);
const S = cell(180, 2000, 30, null, [P]);
add('matching N slope scores strongly', N.like > 0.55);
add('adjacent sector scores lower than the match', NE.like < N.like && NE.like > 0.1);
add('opposite aspect is excluded', S.like < 0.05);
add('likelihood stays in 0..1', [N, NE, S].every(r => r.like >= 0 && r.like <= 1));
add('confidence stays in 0..100', [N, NE, S].every(r => r.conf >= 0 && r.conf <= 100));

const far = cell(0, 2000, 30, LAT + 40 / 111, [P]);
add('distance decays the score', far.like < N.like);

const outOfBand = cell(0, 2900, 30, null, [P]);
add('elevation outside the band collapses', outOfBand.like < N.like);

// The trust/credibility path is the one piece that reads the report set, so it
// has to work through setReports() rather than a global.
add('setReports() returns the count it stored', E.setReports([P]) === 1);
add('getReports() round-trips', E.getReports().length === 1);
add('progReportWeight() is finite for an unknown author',
  Number.isFinite(E.progReportWeight({ user_id: 'nobody' })));
E.setReports([]);
add('setReports([]) clears', E.getReports().length === 0);

// A conflicting report of a different type must pull confidence down.
const conflicted = cell(0, 2000, 30, null, [P], [Object.assign({}, P, { type: 'wet' })]);
add('a conflicting report lowers confidence', conflicted.conf < N.conf);

let failed = 0;
for (const [name, ok] of checks) {
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}`);
  if (!ok) failed++;
}
console.log(failed ? `\nENGINE FAILED (${failed}/${checks.length})` : '\nENGINE OK');
process.exit(failed ? 1 : 0);
