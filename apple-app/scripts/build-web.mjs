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

console.log(`[build:web] ${python} run_interactive.py ${args.join(' ')}`);
const res = spawnSync(python, ['run_interactive.py', ...args], {
  cwd: repoRoot,
  stdio: 'inherit',
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
