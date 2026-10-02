// A tiny stand-in for an R2 bucket binding, backed by a directory (reads)
// and a Map (writes). `runs/<run>/...` maps onto exportDir for one run id.
import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

export function fsBucket(run, exportDir, extra = {}) {
  const mem = new Map();
  const file = (key) => {
    const p = `runs/${run}/`;
    if (!key.startsWith(p)) return null;
    const rel = key.slice(p.length);
    // extra: {"overview/": dir} serves a sub-path from another directory
    for (const [pre, dir] of Object.entries(extra)) {
      if (rel.startsWith(pre)) { const f = join(dir, rel.slice(pre.length)); return existsSync(f) ? readFileSync(f) : null; }
    }
    const f = join(exportDir, rel);
    return existsSync(f) ? readFileSync(f) : null;
  };
  const obj = (buf, opt) => {
    const u8 = new Uint8Array(buf);
    let part = u8, range;
    if (opt && opt.range) {
      const off = opt.range.offset || 0, len = opt.range.length ?? (u8.length - off);
      part = u8.slice(off, off + len); range = { offset: off, length: part.length };
    }
    return {
      size: u8.length, range, httpEtag: '"t"', body: part,
      arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
    };
  };
  return {
    mem,
    async get(key, opt) {
      if (mem.has(key)) return obj(mem.get(key), opt);
      const b = file(key); return b ? obj(b, opt) : null;
    },
    async put(key, data) { mem.set(key, data instanceof Uint8Array ? data : new Uint8Array(data)); },
  };
}
