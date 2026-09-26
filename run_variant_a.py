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


def app_window(target_date, days):
    """The window the APP's timeline shows, which the export has to cover.

    pipeline/interactive_export.py fetches `date - days` .. `date + days` and
    keeps the first (2*days+1)*24 hours, so its slider runs from midnight
    `days` before the target date for 11 days at the default of 5.

    Getting this wrong is why the slider appeared dead: the export used to be
    72 h ENDING on the target date, which is 27% of that slider sitting in its
    first third. Every position past the target date snapped to the same last
    frame, so two thirds of the drag changed nothing.
    """
    start = datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=days)
    return start, start + timedelta(hours=(2 * days + 1) * 24 - 1)


def _select_timestamps(series, step_h, win=None):
    """Output timestamps: every step_h hours inside the app's window."""
    allt = set()
    for s in series.values():
        allt.update(s.keys())
    ts = sorted(allt)
    if ts and win:
        start, end = win
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
    ap.add_argument("--days", type=int, default=5,
                    help="half-width of the output window in days, matching "
                         "run_interactive.py --days. The export then spans the "
                         "same hours the app's timeline shows.")
    ap.add_argument("--profile-step-h", type=int, default=12,
                    help="timestep for the per-point snow profiles. Coarser "
                         "than --step-h on purpose: the raster is what gets "
                         "scrubbed, a profile is a point read, and profiles.json "
                         "costs ~300 kB per step against ~100 kB for a frame.")
    ap.add_argument("--only-tile", type=int, default=None)
    ap.add_argument("--points-csv", default=None, help="reuse an external point set")
    ap.add_argument("--runs-dir", default=None, help="reuse existing SNOWPACK .pro runs")
    ap.add_argument("--step-h", type=int, default=6,
                    help="timestep for the layer PNGs [h]")
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
    win = app_window(args.date, args.days)
    print(f"output window {win[0]:%Y-%m-%d %H:%M} .. {win[1]:%Y-%m-%d %H:%M} "
          f"({(win[1]-win[0]).total_seconds()/3600:.0f} h, matches the app's "
          f"timeline at --days {args.days})")

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
        # 2) forcing + 3) SNOWPACK -- both have to reach the END of the
        #    app's window, not just the target date.
        _ts = _time.time()
        forcing.build_forcing(points, args.date, model=args.model,
                              since=win[0].date().isoformat(),
                              until=win[1].date().isoformat())
        _t["forcing"] = _time.time() - _ts
        _ts = _time.time()
        runs_dir = snowpack_runner.run_points(points, args.date, workers=args.workers,
                                              out_start=win[0], out_end=win[1],
                                              step_h=args.step_h)
        _t["snowpack"] = _time.time() - _ts

    # 4) classify points
    series = gridding.classify_points(points, runs_dir)
    print(f"{len(series)} points classified from .pro")
    ts = _select_timestamps(series, args.step_h, win)
    print(f"{len(ts)} layer timestamps (step {args.step_h}h) "
          f"covering {win[0]:%Y-%m-%d %H:%M} .. {win[1]:%Y-%m-%d %H:%M}")
    if not ts:
        raise SystemExit("no timestamps — check SNOWPACK runs / --runs-dir")
    # The profiles ride a coarser axis of their own; see --profile-step-h.
    pts_ts = _select_timestamps(series, max(args.step_h, args.profile_step_h), win)
    print(f"{len(pts_ts)} profile timestamps (step {args.profile_step_h}h)")

    # 5) grid + 6) profiles
    grids_by_ts, idx, w, used = gridding.grid_timeseries(grid, points, series, ts)
    payload = profiles.build_payload(used, runs_dir, pts_ts)

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
