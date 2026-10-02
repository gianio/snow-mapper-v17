// Local stand-in for the deployed Worker: same handler, the bucket is an
// export directory on disk.
//
//   node test/dev_server.mjs <exportDir> [overviewDir] [port=8787] [run=r1]
//
// Then put  "tiles": {"base": "http://127.0.0.1:8787", "run": "r1", ...}
// into the export's manifest.json (tools/publish_tiles.py writes that block).
import http from 'node:http';
import { execFileSync } from 'node:child_process';
import { handle } from '../src/index.js';
import { fsBucket } from './r2_fs.mjs';

const [exp, ov, port = '8787', run = 'r1'] = process.argv.slice(2);
const env = { BUCKET: fsBucket(run, exp, ov ? { 'overview/': ov } : {}) };
const realFetch = globalThis.fetch;
// Terrarium via curl, so the sandbox proxy is honoured
globalThis.fetch = async (u, o) => String(u).includes('elevation-tiles-prod')
  ? new Response(execFileSync('curl', ['-sf', '-m', '30', String(u)]), { status: 200 })
  : realFetch(u, o);

http.createServer(async (req, res) => {
  try {
    const r = await handle(new Request('http://127.0.0.1:' + port + req.url,
      { method: req.method, headers: req.headers }), env, null);
    res.writeHead(r.status, Object.fromEntries(r.headers));
    res.end(Buffer.from(await r.arrayBuffer()));
  } catch (e) { res.writeHead(500); res.end(String(e)); }
}).listen(+port, () => console.log(`tiles dev server on :${port} (run ${run})`));
