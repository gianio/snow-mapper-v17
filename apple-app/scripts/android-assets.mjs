#!/usr/bin/env node
/**
 * Runs after every `cap copy` / `cap sync` (package.json "capacitor:copy:after"
 * hook), so it also applies when you sync from Android Studio. Android only.
 *
 * The web build ships the forecast twice: snowdata-*.json.gz and the plain
 * snowdata-*.json. Android's asset merger treats x.json.gz as the same asset
 * as x.json and stops with "Duplicate resources". Every Android WebView has
 * DecompressionStream, so the boot loader reads the .gz anyway -- the plain
 * file is only the fallback for iOS < 16.4. So Android bundles the .gz alone
 * (the same as the pipeline's gz-only build), which also halves the APK.
 */
import { readdirSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

if (process.env.CAPACITOR_PLATFORM_NAME !== 'android') process.exit(0);

const root = process.env.CAPACITOR_ROOT_DIR || process.cwd();
const data = resolve(root, 'android', 'app', 'src', 'main', 'assets', 'public', 'data');
if (!existsSync(data)) process.exit(0);

let removed = 0;
for (const f of readdirSync(data)) {
  if (/^snowdata-.*\.json$/.test(f) && existsSync(join(data, f + '.gz'))) {
    rmSync(join(data, f));
    removed++;
  }
}
// The bundled pointer must not name a file that is no longer there.
const latest = join(data, 'latest.json');
if (existsSync(latest)) {
  const lj = JSON.parse(readFileSync(latest, 'utf8'));
  if (lj.gz && lj.data) {
    delete lj.data;
    writeFileSync(latest, JSON.stringify(lj));
  }
}
console.log(`[android-assets] bundled forecast: .gz only (${removed} plain copy removed)`);
