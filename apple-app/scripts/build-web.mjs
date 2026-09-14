#!/usr/bin/env node
/**
 * Build the Snow Model web app into ./www so Capacitor can bundle it.
 *
 * This shells out to the SAME pipeline that produces the GitHub Pages web app
 * (../pipeline/interactive_export.py via run_interactive.py --split), so the
 * iOS app and the web app stay byte-for-byte identical. The repo is never
 * modified — only apple-app/www is (re)generated.
 *
 * Env vars:
 *   SNOW_BUILD_ARGS  extra args passed to run_interactive.py
 *                    (default: "--split --res 3000")
 *   SNOW_OFFLINE=1   append "--offline" to use synthetic weather instead of
 *                    fetching live Open-Meteo data (useful with no network).
 *   SNOW_REMOTE_DATA_BASE
 *                    Origin the SHIPPED APP fetches fresh forecast data from at
 *                    launch, falling back to the copy bundled here when offline.
 *                    MUST be set for a release build, or the app ships with the
 *                    forecast frozen at build time (see README "Data delivery").
 *                    e.g. https://gianio.github.io/snow-mapper-v17
 */
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { rmSync, mkdirSync, existsSync, writeFileSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));      // apple-app/scripts
const appRoot = resolve(here, '..');                        // apple-app
const repoRoot = resolve(appRoot, '..');                    // repo root
const www = resolve(appRoot, 'www');

const python = process.env.PYTHON || 'python3';
let args = (process.env.SNOW_BUILD_ARGS || '--split --res 3000').split(/\s+/).filter(Boolean);
if (process.env.SNOW_OFFLINE === '1') args.push('--offline');
args.push('--out-dir', www);

// Clean the previous build (keep the folder + .gitkeep).
if (existsSync(www)) rmSync(www, { recursive: true, force: true });
mkdirSync(www, { recursive: true });
writeFileSync(resolve(www, '.gitkeep'), ''); // keep the folder tracked after cleans

const remote = process.env.SNOW_REMOTE_DATA_BASE || '';
if (remote) {
  console.log(`[build:web] fresh data at launch from: ${remote}`);
} else {
  console.warn('[build:web] WARNING: SNOW_REMOTE_DATA_BASE is not set — the app ' +
    'will ship with the forecast frozen at build time and cannot refresh it ' +
    'without a new release. Fine for a smoke test, not for TestFlight.');
}

console.log(`[build:web] ${python} run_interactive.py ${args.join(' ')}`);
const res = spawnSync(python, ['run_interactive.py', ...args], {
  cwd: repoRoot,
  stdio: 'inherit',
  env: { ...process.env, SNOW_REMOTE_DATA_BASE: remote },
});

if (res.status !== 0) {
  console.error('\n[build:web] FAILED. Is Python + the pipeline deps installed? ' +
    'Try `pip install -r requirements.txt` in the repo root, or set SNOW_OFFLINE=1.');
  process.exit(res.status || 1);
}

// Sanity check the expected split-build outputs.
for (const f of ['index.html', 'app.js']) {
  if (!existsSync(resolve(www, f))) {
    console.error(`[build:web] expected ${f} in www/ — build looks incomplete.`);
    process.exit(1);
  }
}
console.log('[build:web] OK — web assets written to apple-app/www');
