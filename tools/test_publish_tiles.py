#!/usr/bin/env python3
"""publish_tiles: uploads the right files, keeps runs per kind, writes the
manifest block -- against a fake `aws` that stores into a directory."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FAILS = []
def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

FAKE_AWS = r'''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
B = Path(os.environ["FAKE_S3"])
a = [x for x in sys.argv[1:] if x not in ("--only-show-errors",)]
if "--endpoint-url" in a:
    i = a.index("--endpoint-url"); del a[i:i + 2]
assert a[0] == "s3"
def p(u): return B / u[len("s3://"):]
if a[1] == "sync":
    shutil.copytree(a[2], p(a[3]), dirs_exist_ok=True)
elif a[1] == "ls":
    d = p(a[2])
    for c in sorted(d.iterdir()) if d.exists() else []:
        print(f"                           PRE {c.name}/")
elif a[1] == "rm":
    shutil.rmtree(p(a[2]), ignore_errors=True)
'''


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bindir = td / "bin"; bindir.mkdir()
        (bindir / "aws").write_text(FAKE_AWS); (bindir / "aws").chmod(0o755)
        s3 = td / "s3"; (s3 / "bkt" / "runs").mkdir(parents=True)
        for old in ("live-1", "live-2", "live-3", "demo-1"):
            (s3 / "bkt" / "runs" / old).mkdir()
        exp = td / "exp"
        for sub in ("pack", "terrain", "layers"):
            (exp / sub).mkdir(parents=True)
        (exp / "pack" / "index.json").write_text("{}")
        (exp / "terrain" / "shade.png").write_bytes(b"x")
        img = np.zeros((20, 40, 4), np.uint8); img[:, :, 3] = 255; img[:, :, 0] = 200
        Image.fromarray(img, "RGBA").save(exp / "layers" / "ski6_2026-03-30T1200.png")
        (exp / "manifest.json").write_text(json.dumps({
            "bounds": [[45.8, 5.9], [47.8, 10.5]], "tags": ["2026-03-30T1200"],
            "layers": {"ski6": {"file": "layers/ski6_{tag}.png"}, "density": {"file": "layers/d_{tag}.png"}}}))
        base_env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_S3": str(s3)}

        r = subprocess.run([sys.executable, str(ROOT / "tools" / "publish_tiles.py"), str(exp), "live-9"],
                           env={k: v for k, v in base_env.items() if not k.startswith(("R2_", "TILES_"))},
                           capture_output=True, text=True)
        check("not configured -> exit 0, manifest untouched",
              r.returncode == 0 and "tiles" not in json.loads((exp / "manifest.json").read_text()), r.stdout.strip())

        acc = "0123456789abcdef0123456789abcdef"
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "publish_tiles.py"), str(exp), "live-9"],
                           env={**base_env, "R2_ACCOUNT_ID": "x" * 40, "R2_ACCESS_KEY_ID": "k",
                                "R2_SECRET_ACCESS_KEY": "s", "TILES_BASE_URL": "https://t"},
                           capture_output=True, text=True)
        check("a token in R2_ACCOUNT_ID is named as such, without printing it",
              "looks like an API token" in r.stdout and "x" * 40 not in r.stdout, r.stdout.strip()[:160])
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "publish_tiles.py"), str(exp), "live-9"],
                           env={**base_env, "R2_ACCOUNT_ID": acc, "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
                                "TILES_BASE_URL": "https://pub-0123.r2.dev"}, capture_output=True, text=True)
        check("a bucket address as TILES_BASE_URL is refused with the reason",
              "must be the Worker's address" in r.stdout and "tiles" not in json.loads((exp / "manifest.json").read_text()),
              r.stdout.strip()[:120])
        # a pasted endpoint URL (with a line break) still works
        env = {**base_env, "R2_ACCOUNT_ID": f"https://{acc}.r2.cloudflarestorage.com\n", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
               "R2_BUCKET": "bkt", "TILES_BASE_URL": "https://tiles.example/", "TILES_KEEP_RUNS": "2"}
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "publish_tiles.py"), str(exp), "live-9"],
                           env=env, capture_output=True, text=True)
        print(r.stdout.strip()); print(r.stderr.strip()[-500:])
        run = s3 / "bkt" / "runs" / "live-9"
        check("pack + terrain uploaded", (run / "pack" / "index.json").exists() and (run / "terrain" / "shade.png").exists())
        check("overview archive uploaded", (run / "overview" / "ski6" / "2026-03-30T1200.pmtiles").exists())
        check("overview not left inside the export (it is committed for the demo)", not (exp / "overview").exists())
        left = sorted(p.name for p in (s3 / "bkt" / "runs").iterdir())
        check("old live runs pruned to 2, the demo kept", left == ["demo-1", "live-3", "live-9"], str(left))
        m = json.loads((exp / "manifest.json").read_text())
        t = m.get("tiles") or {}
        check("manifest tiles block", t.get("base") == "https://tiles.example" and t.get("run") == "live-9"
              and t.get("views") == ["ski6", "density"] and t.get("zmax") == 12, str(t))
    print("\nPUBLISH TILES " + ("OK" if not FAILS else f"FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
