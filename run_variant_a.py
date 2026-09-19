"""Variant A — national representative-point ski-quality runner.

Offline pipeline inside snow-mapper-v17: select representative terrain points per
subregion -> per-point Open-Meteo forcing -> SNOWPACK -> 18-cat + simplified ski
classification (subregion-local KNN) + snow profiles -> export layer + profile data
for the app.

Examples
--------
    # Full pipeline for one subregion (needs SNOWPACK binary + network):
    python run_variant_a.py --date 2026-04-01 --only-tile 2

    # Whole Switzerland:
    python run_variant_a.py --date 2026-04-01

    # Verify the classify->grid->profile->export chain on EXISTING SNOWPACK runs
    # (no network / no SNOWPACK rerun), e.g. the sandbox national point runs:
    python run_variant_a.py --date 2026-04-01 --points-csv <points.csv> --runs-dir <dir>
"""
from __future__ import annotations
import argparse, csv
from datetime import datetime, timedelta

from variant_a import config, subregions, select_points, forcing, snowpack_runner
from variant_a import gridding, profiles, export, publish as publish_mod


def _points_from_csv(grid, path):
    """Load points from an external CSV (id,lat,lon,elev*,aspect*,slope*[,subregion])."""
    def num(r, *names, default=0.0):
        for n in names:
            if n in r and r[n] not in ("", None):
                return float(r[n])
        return default
    pts = []
    for r in csv.DictReader(open(path)):
        lat = num(r, "lat", "latitude"); lon = num(r, "lon", "longitude")
        rc = subregions.wgs84_to_cell(grid, lat, lon)
        if rc is None:
            continue
        row, col = rc
        pts.append({
            "id": r.get("id") or f"{r.get('subregion','p')}_{len(pts)}",
            "tile": int(grid.tile[row, col]), "row": row, "col": col,
            "elev": num(r, "elev_m", "elev", "elev_m", default=grid.elevation[row, col]),
            "aspect": num(r, "aspect_deg", "aspect", default=grid.aspect[row, col]),
            "slope": num(r, "slope_deg", "slope", default=grid.slope[row, col]),
            "lat": lat, "lon": lon,
        })
    return pts


def _select_timestamps(series, step_h, target_date=None, window_h=None):
    """Output timestamps: every step_h hours inside the target window.

    The window matters. SNOWPACK is driven for a whole spin-up season, so the
    .pro of an externally supplied --runs-dir can span 120 days; exporting all
    of it would mean hundreds of PNG frames of last December's snow. Our own
    runs are already trimmed via PROF_START, but the filter has to hold either
    way, so it is applied here too.
    """
    allt = set()
    for s in series.values():
        allt.update(s.keys())
    ts = sorted(allt)
    if ts and target_date and window_h:
        end = datetime.strptime(target_date, "%Y-%m-%d")
        start = end - timedelta(hours=window_h)
        inside = [t for t in ts if start <= t.replace(tzinfo=None) <= end]
        # Never filter down to nothing: a .pro whose clock disagrees with the
        # target date is a reason to show everything and let the count speak,
        # not to fail with "no timestamps".
        if inside:
            ts = inside
    if not ts:
        return []
    t0 = ts[0]
    return [t for t in ts if int((t - t0).total_seconds() // 3600) % step_h == 0]


def main():
    ap = argparse.ArgumentParser(description="Variant A national ski-quality pipeline")
    ap.add_argument("--date", required=True, help="target date YYYY-MM-DD")
    ap.add_argument("--window", type=int, default=72,
                help="hours of output ending on --date")
    ap.add_argument("--only-tile", type=int, default=None)
    ap.add_argument("--points-csv", default=None, help="reuse an external point set")
    ap.add_argument("--runs-dir", default=None, help="reuse existing SNOWPACK .pro runs")
    ap.add_argument("--step-h", type=int, default=6, help="output timestep [h]")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of representative points (smoke tests). "
                         "Spread evenly over the selection so the sample still "
                         "spans elevations, aspects and subregions.")
    ap.add_argument("--model", default="best_match")
    ap.add_argument("--publish", action="store_true",
                    help="publish export to VARIANT_A_PUBLISH_DIR + Supabase (if configured)")
    args = ap.parse_args()
    datetime.strptime(args.date, "%Y-%m-%d")

    # Phase timings, printed in one machine-readable line at the end. The CI
    # job needs them separated: point selection scans the whole 920x1440
    # national grid and is independent of --limit and of the model, so folding
    # it into a per-point cost would make the national projection nonsense.
    import time as _time
    _t = {}
    _t0 = _time.time()

    grid = subregions.load_national_grid()
    _t["grid"] = _time.time() - _t0
    print(f"national grid {grid.nr}x{grid.nc}, tiles {subregions.tile_ids(grid)}")

    # 1) points
    if args.points_csv:
        points = _points_from_csv(grid, args.points_csv)
        print(f"{len(points)} points loaded from {args.points_csv}")
        runs_dir = args.runs_dir
    else:
        _ts = _time.time()
        _, points = select_points.select_national(grid, only_tile=args.only_tile)
        _t["select"] = _time.time() - _ts
        if args.limit and args.limit < len(points):
            # Stride rather than truncate: the selection is ordered by tile,
            # then elevation band, then aspect, so points[:N] would be one
            # subregion at one elevation -- useless as a sample and useless
            # for timing, since low flat points are the cheapest to run.
            step = len(points) / float(args.limit)
            points = [points[int(i * step)] for i in range(args.limit)]
            print(f"--limit {args.limit}: sampled every {step:.1f}th point")
        # export_all() creates OUTPUT_DIR, but that runs at the very END --
        # and this write happens first. On a dev machine the directory already
        # exists from an earlier run, so the gap never showed; in a fresh
        # checkout (CI) it is a FileNotFoundError seven minutes into the job.
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        select_points.write_csv(points, config.OUTPUT_DIR / "points.csv")
        print(f"{len(points)} representative points selected")
        # 2) forcing + 3) SNOWPACK
        _ts = _time.time()
        forcing.build_forcing(points, args.date, model=args.model)
        _t["forcing"] = _time.time() - _ts
        _ts = _time.time()
        runs_dir = snowpack_runner.run_points(points, args.date, workers=args.workers,
                                              window_h=args.window)
        _t["snowpack"] = _time.time() - _ts

    # 4) classify points
    series = gridding.classify_points(points, runs_dir)
    print(f"{len(series)} points classified from .pro")
    ts = _select_timestamps(series, args.step_h, args.date, args.window)
    print(f"{len(ts)} output timestamps (step {args.step_h}h)")
    if not ts:
        raise SystemExit("no timestamps — check SNOWPACK runs / --runs-dir")

    # 5) grid + 6) profiles
    grids_by_ts, idx, w, used = gridding.grid_timeseries(grid, points, series, ts)
    payload = profiles.build_payload(used, runs_dir, ts)

    # 7) export
    out_dir, manifest = export.export_all(grid, grids_by_ts, payload)
    prev = export.preview_html(out_dir, manifest, grid)
    print(f"exported -> {out_dir}")
    print(f"  manifest: {out_dir/'manifest.json'}  ({len(ts)} timestamps, "
          f"{len(payload['points'])} profile points)")
    print(f"  preview : {prev}")
    _t["total"] = _time.time() - _t0
    npts = len(points)
    per = (_t.get("snowpack", 0.0) / npts) if npts else 0.0
    print("[timing] " + " ".join(f"{k}={v:.1f}s" for k, v in _t.items())
          + f" points={npts} snowpack_per_point={per:.2f}s")

    # 8) publish to the app (static dir + Supabase, both optional/env-gated)
    if args.publish:
        publish_mod.publish(out_dir)


if __name__ == "__main__":
    main()
