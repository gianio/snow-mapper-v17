#!/usr/bin/env python3
"""Publish a SNOWPACK export to the tile service (Cloudflare R2 + Worker).

    python tools/publish_tiles.py <export_dir> <run_id>

1. builds the overview PMTiles (zoom 5-9) per view and frame,
2. uploads what the Worker needs -- pack/, terrain/, overview/ -- to
   s3://$R2_BUCKET/runs/<run_id>/ (any S3-compatible store: R2, GCS, S3,
   MinIO; only the endpoint differs),
3. deletes all but the newest $TILES_KEEP_RUNS runs (default 3),
4. writes a "tiles" block into <export_dir>/manifest.json, so the app uses
   the service. Without it (no secrets, a failed upload) the app keeps
   rendering on the device -- nothing breaks.

Env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (or S3_ENDPOINT
for another provider), R2_BUCKET (default snowmapper-tiles), TILES_BASE_URL
(the Worker's public URL). Missing settings -> exit 0 with a note.
"""
from __future__ import annotations
import json, os, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _aws(args, env):
    cmd = ["aws", "s3"] + args + ["--endpoint-url", env["S3_ENDPOINT"], "--only-show-errors"]
    return subprocess.run(cmd, env={**os.environ, **env["AWS"]}, check=True,
                          capture_output=True, text=True)


def _clean(v):
    """Secrets pasted with quotes, spaces or line breaks."""
    return "".join((v or "").split()).strip("'\"")


def account_id(raw):
    """(id, problem). Accepts the bare 32-hex Account ID, or a pasted R2
    endpoint / dashboard URL containing it. `problem` describes the value
    WITHOUT echoing it (it is a secret and would be masked anyway)."""
    v = _clean(raw)
    if not v:
        return "", None
    m = re.search(r"([0-9a-fA-F]{32})", v)
    if m and (len(v) == 32 or "cloudflare" in v or "/" in v):
        return m.group(1).lower(), (None if len(v) == 32 else
                                    "R2_ACCOUNT_ID held a URL -- used the 32-character account id inside it")
    kind = ("looks like an API token" if len(v) > 32 else
            "is shorter than an account id" if len(v) < 32 else "is not hexadecimal")
    return "", (f"R2_ACCOUNT_ID {kind} ({len(v)} characters). It must be the 32-character Account ID "
                "(Cloudflare dashboard -> R2 -> right-hand side 'Account ID'), not a token or key.")


def settings():
    acc, problem = account_id(os.environ.get("R2_ACCOUNT_ID", ""))
    if problem:
        print("tiles: " + problem)
    ep = _clean(os.environ.get("S3_ENDPOINT", "")) or (f"https://{acc}.r2.cloudflarestorage.com" if acc else "")
    if ep and not ep.startswith("http"):
        ep = "https://" + ep
    key, sec = _clean(os.environ.get("R2_ACCESS_KEY_ID", "")), _clean(os.environ.get("R2_SECRET_ACCESS_KEY", ""))
    base = _clean(os.environ.get("TILES_BASE_URL", "")).rstrip("/")
    if base and not base.startswith("http"):
        base = "https://" + base
    # The app asks the WORKER for /v1/<run>/...; a bucket address (r2.dev or
    # the S3 endpoint) has no such paths and the app would get no tile.
    if base and (".r2.dev" in base or "r2.cloudflarestorage.com" in base):
        print("tiles: TILES_BASE_URL is the R2 bucket's address. It must be the Worker's address "
              "(https://snowmapper-tiles.<your-subdomain>.workers.dev, shown at the end of the "
              "'Cloudflare tiles' run with deploy_worker, or your own domain on the Worker).")
        base = ""
    missing = [n for n, v in (("R2_ACCOUNT_ID or S3_ENDPOINT", ep), ("R2_ACCESS_KEY_ID", key),
                              ("R2_SECRET_ACCESS_KEY", sec), ("TILES_BASE_URL", base)) if not v]
    # an unset GitHub variable arrives as "" -- that means "the default" too
    return {"S3_ENDPOINT": ep, "BASE": base, "BUCKET": _clean(os.environ.get("R2_BUCKET", "")) or "snowmapper-tiles",
            "KEEP": int(os.environ.get("TILES_KEEP_RUNS", "3") or 3),
            "AWS": {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": sec, "AWS_DEFAULT_REGION": "auto"}}, missing


def tiles_block(man, base, run, sharp=None, on_demand=False):
    """on_demand: the Worker may render missing tiles (Workers Paid). Without
    it the app asks the service only for the frames in `sharp` (pre-rendered
    in CI) and renders every other zoomed-in frame on the device."""
    views = [v for v in ("ski6", "wind", "density", "ski18") if v in man.get("layers", {})]
    return {"base": base, "run": str(run), "views": views, "zmin": 10, "zmax": 12,
            "overview": {"zmin": 5, "zmax": 9}, "on_demand": bool(on_demand),
            "sharp": sharp or {}}


def main():
    exp, run = Path(sys.argv[1]), sys.argv[2]
    env, missing = settings()
    if missing:
        print(f"tiles: not configured ({', '.join(missing)} missing) -- app renders on the device")
        return 0
    man_f = exp / "manifest.json"
    man = json.loads(man_f.read_text())
    import make_overview_pmtiles, tempfile
    # built outside the export: the demo export is committed to git, the
    # archives only go to the bucket
    ov = Path(tempfile.mkdtemp(prefix="overview_"))
    n = make_overview_pmtiles.build(exp, ov)
    print(f"tiles: {n} overview archives")
    dest = f"s3://{env['BUCKET']}/runs/{run}"
    # pre-rendered sharp tiles (tiles/worker/prerender.mjs), if any
    cache = Path(os.environ["TILES_CACHE_DIR"]) if os.environ.get("TILES_CACHE_DIR") else None
    sharp = {}
    if cache and (cache / "sharp.json").exists():
        sharp = json.loads((cache / "sharp.json").read_text())
    for sub, src in (("pack", exp / "pack"), ("terrain", exp / "terrain"), ("overview", ov),
                     ("cache", cache if sharp else None)):
        if src is None:
            continue
        if src.is_dir():
            _aws(["sync", str(src), f"{dest}/{sub}"], env)
            print(f"tiles: uploaded {sub}/")
    # keep the newest runs only -- per kind (live-*, demo-*), so live cycles
    # never delete the demo the app may still be pointing at
    kind = run.split("-")[0] + "-"
    try:
        out = _aws(["ls", f"s3://{env['BUCKET']}/runs/"], env).stdout
        runs = [l.split()[-1].rstrip("/") for l in out.splitlines() if l.strip().startswith("PRE")]
        runs = [r for r in runs if r.startswith(kind)]
        runs.sort(key=lambda r: (len(r), r))
        for old in [r for r in runs if r != run][: max(0, len(runs) - env["KEEP"])]:
            _aws(["rm", f"s3://{env['BUCKET']}/runs/{old}/", "--recursive"], env)
            print(f"tiles: removed old run {old}")
    except subprocess.CalledProcessError as e:
        print(f"tiles: cleanup skipped ({e.stderr.strip()[:200]})")
    on_demand = os.environ.get("TILES_ON_DEMAND", "").strip().lower() in ("1", "true", "yes")
    man["tiles"] = tiles_block(man, env["BASE"], run, sharp, on_demand)
    man_f.write_text(json.dumps(man, indent=2))
    print(f"tiles: manifest -> {env['BASE']}/v1/{run}/...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        # an upload problem must not take the cycle down: no tiles block,
        # the app renders on the device as before
        print(f"tiles: upload failed -- {e.stderr.strip()[:300] if e.stderr else e}")
        sys.exit(0)
