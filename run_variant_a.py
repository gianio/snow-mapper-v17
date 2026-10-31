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
    ap.add_argument("--date", default=None, help="target date YYYY-MM-DD (live: today)")
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
    ap.add_argument("--mode", choices=("matrix", "points"), default="matrix",
                    help="matrix (default): weather points x height/aspect/slope "
                         "runs sharing each point's weather, the Disentis design. "
                         "points: the older one-DEM-cell-per-class selection.")
    ap.add_argument("--spacing-km", type=float, default=None,
                    help="matrix mode: weather point spacing (default config)")
    ap.add_argument("--live", action="store_true",
                    help="live cycle: carry the snowpack state between runs, "
                         "correct precipitation against IMIS, gate publishing. "
                         "--date defaults to today (UTC).")
    ap.add_argument("--state-dir", default=None,
                    help="where the previous cycle's state is (live)")
    ap.add_argument("--state-out", default=None,
                    help="where to write the new state (live; default --state-dir)")
    ap.add_argument("--community", action="store_true",
                    help="compare with community reports also outside --live")
    ap.add_argument("--imis", action="store_true",
                    help="compare with IMIS snow heights also outside --live")
    ap.add_argument("--ogd-dir", default=None,
                    help="MeteoSwiss OGD point series (tools/ogd_extract.py) to put "
                         "over the Open-Meteo forecast hours")
    ap.add_argument("--archive-dir", default=None,
                    help="our own MeteoSwiss archive (pipeline/wx_archive.py) to put "
                         "over the Open-Meteo hours of the past days")
    ap.add_argument("--advance-only", action="store_true",
                    help="live: only integrate the carried state to --date 00:00 "
                         "and save it -- no window run, no export. The season "
                         "replay workflow calls this in monthly steps.")
    ap.add_argument("--season-replay", action="store_true",
                    help="live: cold starts begin at the season start however "
                         "long ago that is (see config.SEASON_COLD_MAX_DAYS)")
    ap.add_argument("--write-points", default=None,
                    help="write the weather points as JSON (for ogd_extract) and exit")
    ap.add_argument("--no-shade", action="store_true",
                    help="skip the horizon-shading correction")
    ap.add_argument("--publish", action="store_true",
                    help="publish export to VARIANT_A_PUBLISH_DIR + Supabase (if configured)")
    args = ap.parse_args()
    if args.date is None:
        if not args.live:
            ap.error("--date is required unless --live")
        args.date = datetime.utcnow().strftime("%Y-%m-%d")
    datetime.strptime(args.date, "%Y-%m-%d")
    if args.live:
        args.state_dir = args.state_dir or str(config.CACHE_DIR / "state")
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

    if args.mode == "matrix":
        return _main_matrix(args, grid, win, _t, _t0, _time)

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


def _cold_start_plan(t0, replay=False):
    """(cold_from, bare, kind) for a run that cannot continue a carried state.

    'season': from bare ground at the season start -- a full season, the
    normal case. 'spinup': later in the winter that no longer fits a cycle,
    so the old short spin-up on the assumed base; the season replay workflow
    is what repairs it."""
    ss = config.season_start(t0)
    if replay or (t0 - ss).days <= config.SEASON_COLD_MAX_DAYS:
        return ss, True, "season"
    return t0 - timedelta(days=config.SPINUP_DAYS), False, "spinup"


def _origin(st, carried, n_cold, cold_from, bare, kind):
    """Where the snowpack in the state comes from, carried from cycle to cycle."""
    prev = (((st or {}).get("meta") or {}).get("origin")) if carried else None
    if carried and prev is None:
        prev = {"kind": "carried", "note": "started before the season was tracked"}
    if not n_cold:
        return prev
    new = {"kind": kind, "from": cold_from.strftime("%Y-%m-%d"), "bare": bool(bare),
           "runs": int(n_cold)}
    if prev is None:
        return new
    out = dict(prev)
    out["later_cold_starts"] = (list(prev.get("later_cold_starts") or []) + [new])[-5:]
    return out


def full_season(origin):
    """True when every slope integrates from the season start (or carried)."""
    if not origin:
        return False
    parts = [origin] + list(origin.get("later_cold_starts") or [])
    return all(p.get("kind") != "spinup" for p in parts)


def _state_meta(st, factors, origin, t, extra=None):
    m = ((st or {}).get("meta") or {})
    out = {"precip_factor": factors, "cycle": int(m.get("cycle", 0)) + 1,
           "season": config.season_label(t), "origin": origin}
    out.update(extra or {})
    return out


def _carry_files(state_dir):
    """Side files of the old state that travel on unchanged (advance-only)."""
    from pathlib import Path as _P
    from variant_a import precip as precip_mod, drift
    if not state_dir:
        return []
    return [f for f in (_P(state_dir) / precip_mod.HIST_FILE, _P(state_dir) / drift.HIST_FILE)
            if f.exists()]


def _main_matrix(args, grid, win, _t, _t0, _time):
    """Weather points -> a height x aspect x slope matrix of SNOWPACK runs that
    share each point's weather -> grid by similar exposure.

    --live additionally carries the snowpack between runs (state.py), corrects
    precipitation against IMIS snow heights (imis.py) and gates publishing
    (gates.py)."""
    import json as _json
    from pathlib import Path as _P
    from variant_a import matrix, terrain, wind, gates, imis, state as state_mod
    from variant_a.gridding import METS
    _ts = _time.time()
    wps = matrix.weather_points(grid, spacing_km=args.spacing_km)
    if args.only_tile:
        wps = [w for w in wps if w["tile"] == args.only_tile]
    # the size of the unlimited run, so a smoke test can project the full cost
    n_full = len(matrix.matrix_runs(wps))
    if args.limit and args.limit < len(wps):
        # --limit counts WEATHER POINTS here, strided so a smoke test still
        # spans the country rather than one corner of it.
        step = len(wps) / float(args.limit)
        wps = [wps[int(i * step)] for i in range(args.limit)]
        print(f"--limit {args.limit}: every {step:.1f}th weather point")
    if args.write_points:
        _json.dump([{"id": w["id"], "lat": w["lat"], "lon": w["lon"],
                     "ref_elev": w["ref_elev"]} for w in wps],
                   open(args.write_points, "w"))
        print(f"{len(wps)} weather points -> {args.write_points}")
        return
    runs = matrix.matrix_runs(wps)
    _t["select"] = _time.time() - _ts
    print(f"{len(wps)} weather points, {len(runs)} SNOWPACK runs "
          f"(up to {len(matrix.band_values())} bands x {config.MATRIX_ASPECTS} aspects x "
          f"{len(config.MATRIX_SLOPES)} slopes + flat)")
    print(f"{len(runs)} representative points selected")    # parsed by the CI timing step

    # Terrain effects the 1-D runs cannot see: horizon shading for this
    # window's dates, and the forest mask.
    _ts = _time.time()
    if args.advance_only:
        if not args.live:
            raise SystemExit("--advance-only needs --live")
        shade, forest = None, None
    else:
        shade = None if args.no_shade else terrain.shade_for_window(grid, win)
        forest = terrain.load_forest(grid)
    _t["terrain"] = _time.time() - _ts
    # Where this call integrates the carried state to: the start of the
    # output window, or --date itself for a pure state advance.
    adv_to = datetime.strptime(args.date, "%Y-%m-%d") if args.advance_only else win[0]

    # Live: what can continue from the carried snowpack?
    st, carried, factors = None, set(), {}
    since = None
    cold_from, bare, origin, n_cold_plan = None, False, None, 0
    if args.live:
        st = state_mod.load(args.state_dir)
        carried = state_mod.available(st, {r["id"] for r in runs}, adv_to)
        # precipitation factors belong to the season they were learnt in
        same_season = st is not None and config.season_start(st["time"]) == config.season_start(adv_to)
        factors = dict((st or {}).get("meta", {}).get("precip_factor") or {}) if same_season else {}
        n_cold_plan = len(runs) - len(carried)
        cold_from, bare, kind = _cold_start_plan(adv_to, args.season_replay)
        since = cold_from if n_cold_plan else st["time"]
        if st is not None and carried:
            since = min(since, st["time"])
        origin = _origin(st, carried, n_cold_plan, cold_from, bare, kind)
        print(f"live: season {config.season_label(adv_to)}, state "
              f"{'valid at ' + st['time'].strftime('%Y-%m-%d %H:%M') if st else 'none'}"
              f", {len(carried)}/{len(runs)} runs carried, "
              f"{sum(1 for v in factors.values() if v != 1.0)} precip corrections"
              + (f"; {n_cold_plan} cold starts from {cold_from:%Y-%m-%d} ({kind}"
                 f"{', bare ground' if bare else ', assumed base'})" if n_cold_plan else ""))

    # Live: ICON-CH1 2 m temperature from cells at each band's own height
    # (forecast hours); the other hours use the model's vertical profile.
    from variant_a import elevtemp
    band_t, band_summ = elevtemp.build(grid, wps, args.ogd_dir) if args.ogd_dir else (None, {"used": False})

    _ts = _time.time()
    used = forcing.build_forcing_matrix(wps, runs, args.date,
                                        (adv_to, adv_to) if args.advance_only else win,
                                        since=since,
                                        precip_factor=factors, ogd_dir=args.ogd_dir,
                                        archive_dir=args.archive_dir, band_temps=band_t)
    _t["forcing"] = _time.time() - _ts
    wps = [w for w in wps if w["id"] in used]
    keep = {w["id"] for w in wps}
    runs = [r for r in runs if r["wp"] in keep]
    if not runs:
        raise SystemExit("no weather point got forcing -- nothing to run")

    sno_src = None
    if args.live:
        _ts = _time.time()
        sno_src = config.WORK_DIR / "state_new" / "sno"
        matrix.advance_state(runs, carried, st["sno"] if st else None,
                             st["time"] if st else None, adv_to, sno_src,
                             workers=args.workers, bare=bare, cold_from=cold_from)
        _t["advance"] = _time.time() - _ts
        if args.advance_only:
            meta = state_mod.save(args.state_out or args.state_dir, adv_to, sno_src,
                                  _state_meta(st, factors, origin, adv_to),
                                  files=_carry_files(args.state_dir))
            _t["total"] = _time.time() - _t0
            print(f"  state saved: {meta['runs']} runs valid at {meta['time']}, "
                  f"season {meta['season']}, origin {meta['origin']}")
            print("[timing] " + " ".join(f"{k}={v:.1f}s" for k, v in _t.items()))
            return

    _ts = _time.time()
    results, layer_ts, prof_ts = matrix.run_matrix(
        runs, args.date, win, args.step_h, args.profile_step_h, workers=args.workers,
        sno_src=sno_src)
    _t["snowpack"] = _time.time() - _ts
    if not results:
        raise SystemExit("no SNOWPACK run produced output")

    # Wind drift / scour from each weather point's own wind.
    wp_hourly = {}
    for w in wps:
        f = config.WORK_DIR / "meteo" / f"{w['id']}.json"
        try:
            wp_hourly[w["id"]] = _json.loads(f.read_text())["hourly"]
        except Exception:
            pass
    wind.apply(runs, results, layer_ts, wp_hourly, METS)

    # IMIS: validation always when the window is recent enough for the API
    # (7 days back), correction only in live mode.
    rows, summary, new_factors = [], {"stations": 0}, factors
    collected = {}
    recent = (datetime.utcnow() - win[0]).days <= 7 and win[0] <= datetime.utcnow()
    if (args.live or args.imis) and recent:
        _ts = _time.time()
        try:
            stations = imis.fetch()
            rows, summary = imis.compare(stations, wps, runs, results, layer_ts, METS,
                                         collect=collected)
            if args.live:
                new_factors = imis.update_factors(rows, factors)
            print(f"IMIS: {summary}")
        except Exception as e:
            print(f"IMIS unavailable: {type(e).__name__}: {e}")
        _t["imis"] = _time.time() - _ts

    # Community reports: compared every live cycle; the precipitation step
    # they suggest is only applied with COMMUNITY_APPLY=1 and when the
    # held-out reports confirm it helps (community.py).
    comm_rows, comm_summary = [], {"observations": 0}
    if (args.live or args.community) and recent:
        _ts = _time.time()
        try:
            from variant_a import community
            new_factors, comm_rows, comm_summary = community.cycle(
                wps, runs, results, layer_ts, METS, new_factors)
            print(f"COMMUNITY: {comm_summary}")
        except Exception as e:
            print(f"community reports unavailable: {type(e).__name__}: {e}")
        _t["community"] = _time.time() - _ts

    # Drift: this cycle's station comparison joins the season's history,
    # and the history is judged (drift.py; drift.html is published with
    # the export, the workflow puts the verdict into the job summary).
    drift_res, drift_hist_path = None, None
    if args.live:
        from variant_a import drift
        dh = drift.load(args.state_dir, config.season_label(win[0]))
        n_new = drift.update(dh, collected)
        drift_res = drift.evaluate(dh, full_season=full_season(origin), origin=origin)
        drift_hist_path = drift.save(config.WORK_DIR / "drift" / drift.HIST_FILE, dh)
        print(f"DRIFT: {drift_res['status']} ({n_new} station-days added); "
              f"independent {drift_res['independent'].get('status')} "
              f"{drift_res['independent'].get('bias_cm', '')}, all {drift_res['all'].get('status')} "
              f"{drift_res['all'].get('bias_cm', '')}")

    # 1 km precipitation pattern (ICON-CH1 via OGD): new snow per cell
    # scaled by how much more/less fell there than at its weather points.
    from variant_a import precip as precip_mod
    p_ratio, p_hist, p_summ = precip_mod.build(grid, wps, args.ogd_dir,
                                               args.state_dir if args.live else None,
                                               win, args.live)

    _ts = _time.time()
    W, cell_index = matrix.build_weights(grid, wps, runs, shade=shade)
    frames = matrix.grid_frames(grid, runs, results, layer_ts, W, cell_index,
                                precip_ratio=p_ratio)
    extra = {
        "live": bool(args.live),
        "terrain": {"shade": shade is not None, "forest": bool(forest.any())},
        "validation": {"imis": summary, "stations": rows[:300],
                       "community": comm_summary, "community_rows": comm_rows[:500]},
        "precip_pattern": p_summ,
        "temperature_at_height": band_summ,
    }
    if drift_res is not None:
        extra["drift"] = {k: drift_res[k] for k in ("status", "season", "full_season")}
        extra["drift"].update({k: {kk: vv for kk, vv in drift_res[k].items()
                                   if kk not in ("worst", "by_subregion")}
                               for k in ("independent", "all")})
    if args.live:
        extra["state"] = {"from": st["time"].strftime("%Y-%m-%dT%H:%M") if st else None,
                          "carried": len(carried), "runs": len(runs),
                          "to": win[0].strftime("%Y-%m-%dT%H:%M"),
                          "season": config.season_label(win[0]), "origin": origin,
                          "full_season": full_season(origin)}
        extra["precip_factor"] = new_factors
    out_dir, manifest = export.export_matrix(grid, frames, wps, runs, results, prof_ts,
                                             forcing_models=used, forest=forest,
                                             extra=extra)
    export.export_pack(out_dir, runs, results, layer_ts, wps, grid, shade, METS,
                       precip=p_ratio)
    manifest["pack"] = "pack/index.json"
    gate = gates.check(len(runs), results, runs, wps, len(layer_ts), len(manifest["tags"]),
                       manifest.get("_frame_stats", []), summary,
                       n_cold=(len(runs) - len(carried)) if args.live else 0)
    if args.live:
        if not full_season(origin):
            gate["soft"].append("not a full season: some slopes started on the short spin-up "
                                "-- run the 'Variant A season replay' workflow")
        if drift_res is not None:
            gate["soft"].append(f"drift: {drift_res['status']}")
            from variant_a import drift as _drift
            (out_dir / "drift.json").write_text(_json.dumps(drift_res, indent=1, default=str))
            (out_dir / "drift.html").write_text(_drift.render_html(
                drift_res, _json.loads(drift_hist_path.read_text())))
    manifest["gates"] = gate
    with open(out_dir / "manifest.json", "w") as f:
        _json.dump({k: v for k, v in manifest.items() if not k.startswith("_")}, f,
                   indent=2, default=export._jsonable)
    (out_dir / "gates.json").write_text(_json.dumps(gate, indent=1))
    _t["export"] = _time.time() - _ts
    print(f"exported -> {out_dir}")
    print(f"  manifest: {out_dir/'manifest.json'}  ({len(manifest['tags'])} frames, "
          f"{manifest['weather_points']} weather points, {manifest['runs']} runs, "
          f"models {manifest['forcing_models']})")
    print(f"  gates: {'PASSED' if gate['passed'] else 'FAILED'}"
          + "".join(f"\n    HARD {h}" for h in gate["hard"])
          + "".join(f"\n    soft {x}" for x in gate["soft"]))

    if args.live:
        hist_files = []
        if p_hist is not None:
            hp = config.WORK_DIR / "precip_hist" / precip_mod.HIST_FILE
            precip_mod.save_history(hp.parent, p_hist)
            hist_files.append(hp)
        if drift_hist_path is not None:
            hist_files.append(drift_hist_path)
        meta = state_mod.save(args.state_out or args.state_dir, win[0], sno_src,
                              _state_meta(st, new_factors, origin, win[0], {
                                  "imis": summary,
                                  "community": {k: v for k, v in comm_summary.items() if k != "steps"},
                                  "drift": (drift_res or {}).get("status")}),
                              files=hist_files)
        print(f"  state saved: {meta['runs']} runs valid at {meta['time']}, cycle {meta['cycle']}, "
              f"season {meta['season']}, full season {full_season(origin)}")

    _t["total"] = _time.time() - _t0
    n = len(runs)
    per = (_t.get("snowpack", 0.0) / n) if n else 0.0
    print("[timing] " + " ".join(f"{k}={v:.1f}s" for k, v in _t.items())
          + f" points={n} snowpack_per_point={per:.2f}s national_runs={n_full}")
    if args.publish and gate["passed"]:
        publish_mod.publish(out_dir)
    if not gate["passed"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
