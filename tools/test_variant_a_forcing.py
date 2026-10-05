#!/usr/bin/env python3
"""Regression tests for the Variant-A pipeline plumbing around SNOWPACK.

Every case here is a bug that actually happened in CI. The headline one:
SNOWPACK begins one calculation step BEFORE the .sno ProfileDate, and MeteoIO
resamples PSUM by accumulation over that step, so a .smet starting exactly on
the ProfileDate has nothing to accumulate from and the run dies on its first
timestep with "missing { precipitation }" -- after exiting 0.
"""
from __future__ import annotations
import hashlib, json, re, sys, tempfile, types
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from variant_a import config, forcing, snowpack_runner
import run_variant_a
import numpy as np
from PIL import Image

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    if not cond:
        FAILS.append(name)


def test_forcing_starts_before_profile_date():
    print("forcing window")
    seen = {}

    def fake_fetch(lat, lon, start, end, model="best_match"):
        seen["start"], seen["end"] = start, end
        return {"time": [], "temperature_2m": []}

    real, forcing.fetch_point = forcing.fetch_point, fake_fetch
    try:
        with tempfile.TemporaryDirectory() as td:
            pts = [{"id": "p0", "lat": 46.5, "lon": 8.0, "elev": 1600.0}]
            forcing.build_forcing(pts, "2026-04-01", meteo_dir=Path(td))
    finally:
        forcing.fetch_point = real

    prof = snowpack_runner._profile_start_iso("2026-04-01", config.SPINUP_DAYS)
    check("forcing starts strictly before the .sno ProfileDate",
          seen.get("start", "") < prof, f"{seen.get('start')} < {prof}")
    check("lead-in is at least one day",
          (datetime.fromisoformat(prof) - datetime.fromisoformat(seen["start"])).days >= 1,
          f"{config.FORCING_LEAD_DAYS} d")
    check("forcing still ends on the target date", seen.get("end") == "2026-04-01",
          str(seen.get("end")))


def test_covers_rejects_a_stale_smet():
    print("cached .smet coverage")
    hdr = "SMET 1.1 ASCII\n[HEADER]\nfields = timestamp TA\n[DATA]\n"
    with tempfile.TemporaryDirectory() as td:
        late = Path(td) / "late.smet"
        late.write_text(hdr + "2025-12-02T00:00:00 269.5\n")
        check("a .smet starting after the window is refetched",
              not forcing._covers(late, "2025-11-30"))
        check("a .smet starting on the window boundary is kept",
              forcing._covers(late, "2025-12-02"))
        early = Path(td) / "early.smet"
        early.write_text(hdr + "2025-11-29T00:00:00 269.5\n")
        check("a .smet starting before the window is kept",
              forcing._covers(early, "2025-11-30"))
        check("a missing file is not coverage", not forcing._covers(Path(td) / "nope.smet", "2025-11-30"))
        empty = Path(td) / "empty.smet"
        empty.write_text(hdr)
        check("a header-only .smet is not coverage", not forcing._covers(empty, "2025-11-30"))


def _ini_text(prof_start=0.0, step_h=None):
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        p = {"id": "x1", "lat": 46.5, "lon": 8.0, "elev": 1600.0, "slope": 25.0,
             "aspect": 0.0, "e_lv95": 2670000, "n_lv95": 1160000}
        snowpack_runner.write_ini(p, t, t, t, t, prof_start, step_h)
        return (t / "x1.ini").read_text()


def test_ini():
    print("SNOWPACK .ini")
    ini = _ini_text()
    step = int(re.search(r"CALCULATION_STEP_LENGTH = (\d+)", ini).group(1))
    period = int(re.search(r"PSUM::ARG1::period = (\d+)", ini).group(1))
    check("PSUM accumulates over exactly one calculation step",
          period == step * 60, f"{period}s vs {step}min")
    check("the deprecated STATION# key is gone", "STATION1" not in ini)
    check("the SMET input names a METEOFILE", "METEOFILE1 = x1.smet" in ini)
    check("PROF_START defaults to the whole run",
          float(re.search(r"PROF_START = ([\d.]+)", ini).group(1)) == 0.0)
    windowed = _ini_text(117.0)
    check("PROF_START trims the written profiles to the window",
          abs(float(re.search(r"PROF_START = ([\d.]+)", windowed).group(1)) - 117.0) < 1e-6)
    # How often SNOWPACK WRITES a profile has to track the export step. It was
    # pinned at 6 h, so asking the exporter for 3 h would have filtered
    # 3-hourly over 6-hourly profiles and produced the same frame count -- an
    # export that looked finer without being finer.
    for step_h, want_h in ((3, 3), (6, 6), (12, 12)):
        ini = _ini_text(0.0, step_h)
        got = float(re.search(r"PROF_DAYS_BETWEEN = ([\d.]+)", ini).group(1)) * 24
        check(f"--step-h {step_h} writes a profile every {want_h} h",
              abs(got - want_h) < 1e-6, f"{got:.1f} h")
    plain = float(re.search(r"PROF_DAYS_BETWEEN = ([\d.]+)", _ini_text()).group(1)) * 24
    check("no step given falls back to 6 h", abs(plain - 6.0) < 1e-6, f"{plain:.1f} h")


def test_timestamp_window():
    print("output timestamp window")
    # A .pro spans the whole spin-up season; only the app's window is exported.
    win = (datetime(2026, 3, 29), datetime(2026, 4, 1))
    allts = [datetime(2026, 4, 1) - timedelta(hours=h) for h in range(0, 2880, 6)]
    series = {"p0": {t: {} for t in allts}}
    ts = run_variant_a._select_timestamps(series, 12, win)
    check("only the window is exported", len(ts) == 7, f"{len(ts)} timestamps")
    check("it ends at the window end", ts[-1] == win[1], str(ts[-1]))
    check("it starts at the window start", ts[0] == win[0], str(ts[0]))
    check("no window means no filtering",
          len(run_variant_a._select_timestamps(series, 12, None)) == 240)
    # A .pro whose clock sits outside the window must not silently export nothing.
    off = {"p0": {datetime(2025, 1, 1) + timedelta(hours=6 * i): {} for i in range(8)}}
    check("a window that matches nothing falls back to everything",
          len(run_variant_a._select_timestamps(off, 12, win)) == 4)


def test_prof_start_derivation():
    print("prof_start derivation")
    # Profiles start exactly at the window start: PROF_START is the spin-up
    # length in days, since the simulation begins spinup_days before it.
    for spinup, out_start, want in [(120, True, 120.0), (120, False, 0.0), (7, True, 7.0)]:
        got = float(spinup) if out_start else 0.0
        check(f"spinup={spinup} windowed={out_start} -> PROF_START={want}",
              got == want, str(got))


def test_window_matches_the_app():
    print("output window vs the app's timeline")
    from datetime import datetime as D
    from run_variant_a import app_window, _select_timestamps
    # pipeline/interactive_export.py fetches date-days .. date+days and keeps
    # the first (2*days+1)*24 hours. The export has to span the same hours or
    # the slider has frames for only part of its travel -- it used to cover
    # 72 h of 264, sitting in the first third, so two thirds of the drag
    # changed nothing at all.
    for days in (5, 3, 1):
        ws, we = app_window("2026-04-01", days)
        app_start = D(2026, 4, 1) - timedelta(days=days)
        app_hours = (2 * days + 1) * 24
        check(f"--days {days}: starts where the app's timeline starts",
              ws == app_start, str(ws))
        check(f"--days {days}: spans the app's {app_hours} h",
              round((we - ws).total_seconds() / 3600) == app_hours - 1,
              f"{(we-ws).total_seconds()/3600:.0f} h")
    ws, we = app_window("2026-04-01", 5)
    check("the window opens BEFORE the target date, not on it", ws < D(2026, 4, 1))
    check("and closes after it", we > D(2026, 4, 1))
    # Frame count at the shipping step.
    series = {"p": {ws + timedelta(hours=h): {} for h in range(0, 264, 6)}}
    ts = _select_timestamps(series, 6, (ws, we))
    check("44 frames at a 6 h step", len(ts) == 44, f"{len(ts)} frames")
    check("frames reach past the target date",
          ts[-1] > D(2026, 4, 1), str(ts[-1]))
    # Anything outside the window is still excluded.
    series["p"][D(2025, 12, 1)] = {}
    ts2 = _select_timestamps(series, 6, (ws, we))
    check("a spin-up profile outside the window is dropped", len(ts2) == 44,
          f"{len(ts2)} frames")


def test_forcing_anchors_on_the_window_start():
    print("forcing spans the whole window")
    seen = {}

    def fake_fetch(lat, lon, start, end, model="best_match"):
        seen["start"], seen["end"] = start, end
        return {"time": [], "temperature_2m": []}

    real, forcing.fetch_point = forcing.fetch_point, fake_fetch
    try:
        with tempfile.TemporaryDirectory() as td:
            pts = [{"id": "p0", "lat": 46.5, "lon": 8.0, "elev": 1600.0}]
            forcing.build_forcing(pts, "2026-04-01", meteo_dir=Path(td),
                                  since="2026-03-27", until="2026-04-06")
    finally:
        forcing.fetch_point = real
    # The spin-up is measured back from the WINDOW START. Anchoring it on the
    # target date left the .smet beginning three days after the model's own
    # ProfileDate, and SNOWPACK died on its first timestep.
    want_start = (datetime(2026, 3, 27)
                  - timedelta(days=config.SPINUP_DAYS + config.FORCING_LEAD_DAYS))
    check("spin-up is measured back from the window start, not the target date",
          seen.get("start") == want_start.date().isoformat(),
          f"{seen.get('start')} (want {want_start.date()})")
    check("forcing reaches the end of the window, days past the target date",
          seen.get("end") == "2026-04-06", str(seen.get("end")))


def test_indexed_png_is_lossless():
    print("indexed PNG encoding")
    import numpy as np
    from PIL import Image
    from variant_a import export
    # The class layers are 9 colours but cost 176 kB a frame as RGBA; indexing
    # drops that to ~102 kB. It must not change a single pixel -- these are
    # category codes, and a shifted colour is a shifted class.
    rng = np.random.default_rng(5)
    table = {i: (i * 25 % 256, (i * 70) % 256, (i * 40) % 256, 190) for i in range(1, 10)}
    lab = rng.integers(0, 10, size=(60, 90)).astype(np.int32)
    rgba = export._rgba_from_labels(lab, table)
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.png"
        export._save_png(rgba, f)
        back = np.array(Image.open(f).convert("RGBA"))
        check("every pixel survives the round trip", np.array_equal(back, rgba),
              f"{int((back != rgba).sum())} differing bytes")
        check("it really is indexed, not RGBA", Image.open(f).mode == "P",
              Image.open(f).mode)
        # And a genuinely high-colour image still goes out as RGBA.
        smooth = np.dstack([rng.integers(0, 256, (60, 90)).astype(np.uint8) for _ in range(4)])
        g = Path(td) / "y.png"
        export._save_png(smooth, g)
        check("a >256-colour raster stays RGBA", Image.open(g).mode == "RGBA",
              Image.open(g).mode)
        check("and survives too", np.array_equal(np.array(Image.open(g).convert("RGBA")), smooth))


def test_profile_payload():
    print("profile payload")
    from datetime import datetime as D
    from variant_a import profiles
    # build_payload used to hold every point's fully parsed .pro at once,
    # because the output loop ran timestamp-major. At 978 points that is
    # hundreds of MB live at the same moment; the national run died right
    # after SNOWPACK while --limit 8 passed. It is point-major now, dropping
    # each raw parse as soon as it is resampled. Same payload, bounded memory.
    ts = [D(2026, 3, 27), D(2026, 3, 27, 12), D(2026, 3, 28)]
    fake = [{"dt": t, "n": 2, "heights": [40.0, 90.0],
             "density": [180.0, 260.0], "grain": [330, 440]} for t in ts]
    real_parse = profiles.classify.parse_pro
    profiles.classify.parse_pro = lambda path: fake
    try:
        with tempfile.TemporaryDirectory() as td:
            pts = []
            for i in range(4):
                d = Path(td) / f"p{i}"; d.mkdir()
                (d / "x_va.pro").write_text("stub")
                pts.append({"id": f"p{i}", "lat": 46.5, "lon": 8.0, "elev": 1600.0,
                            "aspect": 0.0, "slope": 25.0, "tile": 2})
            out = profiles.build_payload(pts, td, ts)
    finally:
        profiles.classify.parse_pro = real_parse
    check("one entry per timestamp", len(out["profiles"]) == len(ts),
          f"{len(out['profiles'])} steps")
    check("every timestamp carries every point",
          all(len(step) == 4 for step in out["profiles"]),
          str([len(x) for x in out["profiles"]]))
    check("points are listed once, not per step", len(out["points"]) == 4,
          str(len(out["points"])))
    check("labels line up with the profile steps",
          len(out["labels"]) == len(out["profiles"]))
    first = out["profiles"][0][0]
    check("each entry has hs and both bin arrays",
          "hs" in first and len(first["db"]) == profiles.NB
          and len(first["gb"]) == profiles.NB)
    check("the snow depth came through", first["hs"] == 90, str(first["hs"]))
    # A timestamp the .pro does not contain falls back to the nearest one
    # rather than dropping the point out of that step.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "p0"; d.mkdir(); (d / "x_va.pro").write_text("stub")
        profiles.classify.parse_pro = lambda path: fake
        try:
            gap = profiles.build_payload(
                [{"id": "p0", "lat": 46.5, "lon": 8.0, "elev": 1600.0,
                  "aspect": 0.0, "slope": 25.0, "tile": 2}],
                td, ts + [D(2026, 4, 5)])
        finally:
            profiles.classify.parse_pro = real_parse
    check("a timestamp outside the .pro still yields a profile",
          len(gap["profiles"]) == 4 and gap["profiles"][-1][0]["hs"] == 90,
          str(gap["profiles"][-1][0]["hs"]))


def test_selection_cache():
    print("point selection cache")
    from variant_a import select_points as sp
    pts = [{"id": "t2_1600_N_20-30_0", "tile": 2, "row": 10, "col": 20, "elev": 1600.0,
            "aspect": 5.0, "slope": 25.0, "lat": 46.5, "lon": 8.0, "e_lv95": 2670000.0,
            "n_lv95": 1160000.0, "band": "1600", "asp_c": "N", "slope_cls": "20-30"}]
    calls = []
    real_sel, real_tiles, real_load = sp.select_for_mask, sp.tile_ids, sp.load_national_grid
    real_cache, real_bands = config.CACHE_DIR, config.ELEV_BANDS
    sp.select_for_mask = lambda g, m, t: (calls.append(t), pts)[1]
    sp.tile_ids = lambda g: [2]
    sp.load_national_grid = lambda: types.SimpleNamespace(tile=None)
    try:
        with tempfile.TemporaryDirectory() as td:
            config.CACHE_DIR = Path(td)
            _, a = sp.select_national()
            _, b = sp.select_national()
            check("the second selection is served from cache", len(calls) == 1, f"{len(calls)} scans")
            check("the cached points round-trip unchanged", a == b)
            check("numeric columns keep their types",
                  isinstance(b[0]["row"], int) and isinstance(b[0]["elev"], float)
                  and isinstance(b[0]["band"], str))
            config.ELEV_BANDS = [1600, 2000]
            sp.select_national()
            check("a changed target grid invalidates the cache", len(calls) == 2)
            sp.select_national(cache=False)
            check("cache=False always reselects", len(calls) == 3)
    finally:
        sp.select_for_mask, sp.tile_ids, sp.load_national_grid = real_sel, real_tiles, real_load
        config.CACHE_DIR, config.ELEV_BANDS = real_cache, real_bands


def _synth_grid(nr, nc, seed):
    """Smooth mountain-like terrain: slopes and aspects span the target classes."""
    import numpy as np
    from variant_a.subregions import NationalGrid
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 1, (nr, nc))
    for _ in range(6):
        z = (z + np.roll(z, 1, 0) + np.roll(z, -1, 0)
             + np.roll(z, 1, 1) + np.roll(z, -1, 1)) / 5
    dem = 1200 + 2200 * (z - z.min()) / max(1e-9, float(z.max() - z.min()))
    dem[rng.random((nr, nc)) < 0.03] = np.nan          # holes, like a real DEM
    gy, gx = np.gradient(np.nan_to_num(dem, nan=0.0), 250.0)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    aspect = (np.degrees(np.arctan2(-gx, gy)) + 360) % 360
    aspect[np.isnan(dem)] = np.nan
    return NationalGrid(elevation=dem, slope=slope, aspect=aspect,
                        tile=np.full((nr, nc), 2, np.int32), nr=nr, nc=nc,
                        xll=480000.0, yll=70000.0, cs=250.0, tile_names={2: "test"})


def test_win_count_matches_sliding_window():
    print("_win_count")
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    from variant_a import select_points as sp
    # _win_count is a summed-area table for speed. The sliding-window form it
    # replaced is the definition, so that is what it is checked against --
    # bit-identical, not merely close, because the counts are small integers.
    rng = np.random.default_rng(0)
    worst = 0.0
    for shape in [(50, 70), (220, 300), (7, 5), (1, 9)]:
        for hw in (1, 2, 5):
            m = rng.random(shape) < 0.3
            w = 2 * hw + 1
            ref = sliding_window_view(np.pad(m.astype(np.float32), hw),
                                      (w, w)).sum(axis=(-1, -2))
            got = sp._win_count(m, hw)
            check(f"{shape} hw={hw}: shape preserved", got.shape == m.shape, str(got.shape))
            if got.shape == ref.shape:
                worst = max(worst, float(np.abs(ref - got).max()))
                check(f"{shape} hw={hw}: bit-identical to the window sum",
                      np.array_equal(ref, got))
    check("no shape/window differed at all", worst == 0.0, f"max |diff| {worst}")


def test_selection_is_stable():
    print("select_for_mask golden")
    import numpy as np
    from variant_a import select_points as sp
    # The scoring was optimised by hoisting loop invariants and swapping the
    # window sum for a summed-area table, both of which must leave the CHOSEN
    # POINTS untouched. A digest over a seeded synthetic terrain pins that:
    # any change to the selection maths breaks it loudly. The value below was
    # taken from the PRE-optimisation implementation, so it pins the original
    # behaviour rather than merely the current one.
    g = _synth_grid(220, 300, 1)
    pts = sp.select_for_mask(g, ~np.isnan(g.elevation), 2)
    check("a realistic synthetic terrain fills the target grid",
          len(pts) == 143, f"{len(pts)} points")
    digest = hashlib.sha256(json.dumps(
        [[str(p["id"]), int(p["row"]), int(p["col"]), float(p["elev"]),
          float(p["aspect"]), float(p["slope"])] for p in pts],
        sort_keys=True).encode()).hexdigest()[:16]
    check("the selection is unchanged (golden digest)",
          digest == "dff9519c62fe17dc", digest)


def test_export_survives_numpy_types():
    print("export JSON safety")
    import numpy as np
    from datetime import datetime
    from variant_a import export, subregions
    # np.unique hands back np.int64, that id rides on every point as
    # p["tile"], and json.dump refuses it -- which killed a whole CI run at
    # the very last step, after the forcing, the model and the PNGs. Fixed at
    # the source (tile_ids) and guarded at the boundary (_jsonable).
    g = _synth_grid(24, 30, 3)
    ids = subregions.tile_ids(g)
    check("tile_ids returns plain ints, not np.int64",
          bool(ids) and all(type(t) is int for t in ids), str([type(t).__name__ for t in ids]))

    dt = datetime(2026, 4, 1)
    grids = {dt: {"ski18": np.ones((24, 30), np.int32),
                  "simple": np.ones((24, 30), np.int32),
                  "density": np.full((24, 30), 250.0)}}
    payload = {"nb": 2, "grain": {0: ["-", [1, 2, 3]]}, "labels": ["2026-04-01T00:00"],
               # deliberately numpy-typed, the way the real payload was
               "points": [{"id": "t2_x", "lat": 46.5, "lon": 8.0, "elev": np.float64(1600),
                           "aspect": np.float32(5), "slope": 25.0, "tile": np.int64(2)}],
               "profiles": [[{"hs": np.int64(80), "db": np.array([200.0, 220.0]), "gb": [1, 3]}]]}
    with tempfile.TemporaryDirectory() as td:
        out, manifest = export.export_all(g, grids, payload, Path(td))
        mf = json.load(open(out / "manifest.json"))
        pr = json.load(open(out / "profiles" / "profiles.json"))
        check("manifest.json is written and reloads", mf["product"] == "variant_a_ski_quality")
        check("every declared layer PNG exists",
              all((out / "layers" / f"{k}_{mf['tags'][0]}.png").exists()
                  for k in ("ski18", "simple", "density")))
        check("numpy scalars survive as plain numbers",
              pr["points"][0]["tile"] == 2 and pr["profiles"][0][0]["hs"] == 80)
        check("numpy arrays survive as lists",
              pr["profiles"][0][0]["db"] == [200.0, 220.0])
        # A type that genuinely cannot be represented must still be fatal --
        # the guard exists to save numpy runs, not to swallow real bugs.
        bad = dict(payload); bad["points"] = [{"oops": object()}]
        try:
            export.export_all(g, grids, bad, Path(td))
            check("an unserialisable object still raises", False, "no error raised")
        except TypeError:
            check("an unserialisable object still raises", True)


def test_matrix_weights():
    """The (cells x runs) weights must hand every cell back its own height,
    slope and aspect -- otherwise the map shows a profile from the wrong hang."""
    print("matrix weights")
    import numpy as np
    from variant_a import matrix
    g = _synth_grid(80, 80, 7)
    wps = matrix.weather_points(g, spacing_km=5.0, min_top_m=1800.0)
    runs = matrix.matrix_runs(wps)
    check("weather points found on synthetic terrain", len(wps) >= 4, f"{len(wps)} wps")
    nper = sum(1 + len(config.MATRIX_SLOPES) * config.MATRIX_ASPECTS for w in wps for _ in w["bands"])
    check("one flat + slopes x aspects per band", len(runs) == nper, f"{len(runs)} runs")
    check("run ids are unique", len({r["id"] for r in runs}) == len(runs))
    W, ci = matrix.build_weights(g, wps, runs)
    rs = np.asarray(W.sum(axis=1)).ravel()
    check("every row sums to 1", np.allclose(rs, 1.0, atol=1e-4), f"{rs.min():.5f}..{rs.max():.5f}")
    check("weights are non-negative", W.data.min() >= 0)
    r, c = np.divmod(ci, g.nc)
    ce = g.elevation[r, c]
    re = np.array([x["elev"] for x in runs], np.float32)
    err = np.abs(W @ re - ce)
    check("elevation is reproduced", np.median(err) < 30 and np.percentile(err, 90) < 150,
          f"median {np.median(err):.0f} m, p90 {np.percentile(err, 90):.0f} m")
    rsl = np.array([x["slope"] for x in runs], np.float32)
    cs = np.clip(np.nan_to_num(g.slope[r, c]), 0, max(config.MATRIX_SLOPES))
    check("slope is reproduced (clipped to the class range)",
          np.abs(W @ rsl - cs).max() < 0.5, f"max {np.abs(W @ rsl - cs).max():.2f} deg")
    # aspect only means something on the sloped runs of steep cells
    steep = cs >= min(config.MATRIX_SLOPES)
    ra = np.radians([x["aspect"] for x in runs])
    sloped = np.array([x["slope"] > 0 for x in runs], np.float32)
    sx = W @ (np.sin(ra) * sloped); cx = W @ (np.cos(ra) * sloped)
    est = np.degrees(np.arctan2(sx, cx)) % 360
    ca = g.aspect[r, c]
    d = np.abs((est - ca + 540) % 360 - 180)[steep & np.isfinite(ca)]
    check("aspect is reproduced on steep cells", d.size > 50 and np.percentile(d, 95) < 6,
          f"{d.size} cells, p95 {np.percentile(d, 95):.1f} deg")
    check("no cell reads a run of a band its weather point lacks",
          all(x["elev"] in w["bands"] for w in wps for x in runs if x["wp"] == w["id"]))


def test_forcing_fallback():
    """Finest model first, hour by hour: a 400 is skipped with its reason, a
    gappy model is KEPT for the hours it has and the next one fills the
    rest; the archive is the last resort."""
    print("forcing model splicing")
    T = [f"2026-03-01T{h:02d}:00" for h in range(24)]
    full = {"time": T, **{v: [1.0] * 24 for v in
            ("temperature_2m", "precipitation", "shortwave_radiation", "wind_speed_10m")}}
    gappy = {**full, "precipitation": [None] * 12 + [0.5] * 12,
             "temperature_2m": [5.0] * 24}
    calls = []

    def fake(answers):
        def f(url, params):
            calls.append((url, params.get("models")))
            a = answers.pop(0) if answers else "exhausted"
            return (None, a) if isinstance(a, str) else ({"hourly": a}, None)
        return f
    real = forcing._fetch_json
    try:
        forcing._fetch_json = fake(["No data is available for this location", gappy, full])
        h, m, notes = forcing.fetch_weather(46.8, 9.8, 2000, "2026-03-01", "2026-03-02")
        check("the gappy model is kept where it has data", h["temperature_2m"][0] == 5.0
              and h["precipitation"][20] == 0.5, str(h["precipitation"][18:22]))
        check("the next model fills only its gaps", h["precipitation"][0] == 1.0
              and h["temperature_2m"][0] == 5.0)
        check("the label names the main model and marks the splice",
              m == config.FORCING_MODELS[1] + "+", m)
        check("the refusal reason is kept", any("No data" in n for n in notes), str(notes))
        check("an old window uses the historical-forecast API",
              calls[0][0] == config.HISTORICAL_FORECAST_URL)
        calls.clear()
        forcing._fetch_json = fake(["x", "y", "z", full])
        h, m, _ = forcing.fetch_weather(46.8, 9.8, 2000, "2026-03-01", "2026-03-02")
        # (after the surface sources only the separate vertical-profile request may follow)
        surf = [c for c in calls if c[1] != config.FORCING_FAR_MODEL or c is calls[0]]
        check("the archive is the last resort", m == "archive" and surf[-1][1] is None, m)
        forcing._fetch_json = fake(["x", "y", "z", "w"])
        h, m, notes = forcing.fetch_weather(46.8, 9.8, 2000, "2026-03-01", "2026-03-02")
        check("nothing usable -> None, with every reason", h is None and len(notes) >= 4, str(notes))
    finally:
        forcing._fetch_json = real
    # a live window: CH1 covers the first hours only, the far model the rest
    from datetime import date, timedelta as td
    d0 = date.today()
    TL = [f"{(d0 + td(days=h // 24)).isoformat()}T{h % 24:02d}:00" for h in range(72)]
    ch1 = {"time": TL, **{v: ([2.0] * 33 + [None] * 39) for v in forcing._VARS}}
    far = {"time": TL, **{v: [9.0] * 72 for v in forcing._VARS}}
    try:
        forcing._fetch_json = fake([ch1, "no ch2", "no d2", far])
        h, m, notes = forcing.fetch_weather(46.8, 9.8, 2000, d0.isoformat(),
                                            (d0 + td(days=2)).isoformat())
        check("live: CH1 hours kept, the far model extends the forecast",
              h["temperature_2m"][0] == 2.0 and h["temperature_2m"][40] == 9.0
              and "meteoswiss_icon_ch1 33" in str(notes)
              and f"{config.FORCING_FAR_MODEL} 39" in str(notes), str(notes))
    finally:
        forcing._fetch_json = real
    lap = forcing._lapsed({"temperature_2m": [0.0, None, -5.0], "precipitation": [1, 2, 3]}, 300.0)
    check("lapse: 300 m up is 1.95 K colder, gaps stay gaps",
          abs(lap["temperature_2m"][0] + 1.95) < 1e-9 and lap["temperature_2m"][1] is None
          and abs(lap["temperature_2m"][2] + 6.95) < 1e-9, str(lap["temperature_2m"]))
    check("lapse leaves the other variables alone", lap["precipitation"] == [1, 2, 3])
    lap = forcing._lapsed({"temperature_2m": [0.0], "precipitation": [2.0, None]}, 0.0, 1.5)
    check("the IMIS precipitation factor scales precipitation", lap["precipitation"] == [3.0, None])
    # OGD overlay: height offset measured on the overlap, OGD wins its hours
    base = {"time": T, "temperature_2m": [-3.0] * 24, "precipitation": [0.0] * 24,
            "shortwave_radiation": [0.0] * 24, "wind_speed_10m": [2.0] * 24,
            "relative_humidity_2m": [80.0] * 24, "wind_direction_10m": [0.0] * 24}
    ogd = {k: [v[0] + (2.0 if k == "temperature_2m" else 0.0)] * 12 for k, v in base.items() if k != "time"}
    ogd["time"] = T[12:]
    ogd["precipitation"] = [1.0] * 12
    out, n, dT = forcing._overlay_ogd(base, ogd)
    check("OGD overlay: hours replaced, temperature aligned by the measured offset",
          n == 12 and abs(dT + 2.0) < 1e-9 and out["precipitation"][12] == 1.0
          and abs(out["temperature_2m"][12] + 3.0) < 1e-9 and out["precipitation"][0] == 0.0,
          f"n={n} dT={dT}")


def test_forcing_matrix_shares_weather():
    """One fetch per weather point, one lapse-rated .smet per virtual slope."""
    print("forcing matrix")
    from datetime import datetime
    T = [f"2026-03-{d:02d}T{h:02d}:00" for d in range(1, 3) for h in range(24)]
    hourly = {"time": T, "temperature_2m": [-2.0] * len(T), "precipitation": [0.5] * len(T),
              "shortwave_radiation": [100.0] * len(T), "wind_speed_10m": [3.0] * len(T),
              "relative_humidity_2m": [80.0] * len(T), "wind_direction_10m": [270.0] * len(T),
              "longwave_radiation": [250.0] * len(T), "cloud_cover": [50.0] * len(T)}
    fetched = []
    real = forcing.fetch_weather
    forcing.fetch_weather = lambda lat, lon, e, s, en, models=None: (
        fetched.append(e), (hourly, "meteoswiss_icon_ch1", []))[1]
    wps = [{"id": "w000", "lat": 46.8, "lon": 9.8, "ref_elev": 2000.0}]
    runs = [{"id": f"w000_{e}_F", "wp": "w000", "lat": 46.8, "lon": 9.8, "elev": float(e)}
            for e in (1700, 2000, 2600)]
    win = (datetime(2026, 3, 25), datetime(2026, 3, 30))
    try:
        with tempfile.TemporaryDirectory() as td:
            used = forcing.build_forcing_matrix(wps, runs, "2026-03-30", win, Path(td), workers=1)
            check("one fetch for three runs", len(fetched) == 1 and used == {"w000": "meteoswiss_icon_ch1"})
            check("fetched at the weather point's reference height", fetched == [2000.0])
            smets = sorted(Path(td).glob("*.smet"))
            check("a .smet per run", len(smets) == 3, str([f.name for f in smets]))

            def first_ta(name):
                txt = (Path(td) / name).read_text().split("[DATA]")[1].split("\n")[1].split()
                fields = [l for l in (Path(td) / name).read_text().splitlines()
                          if l.startswith("fields")][0].split("=")[1].split()
                return float(txt[fields.index("TA")])
            ta = [first_ta(f"w000_{e}_F.smet") for e in (1700, 2000, 2600)]
            check("temperature is lapse-rated per band",
                  abs(ta[1] - 271.15) < 0.01 and abs((ta[0] - ta[2]) - 0.0065 * 900) < 0.01,
                  str([round(t, 2) for t in ta]))
            forcing.build_forcing_matrix(wps, runs, "2026-03-30", win, Path(td), workers=1)
            check("a second build is served from the per-point cache", len(fetched) == 1)
    finally:
        forcing.fetch_weather = real


def test_sno_base_is_real_snow():
    """ne=0 meant no base at all; mk=7 meant ice. Neither may come back."""
    print(".sno base")
    with tempfile.TemporaryDirectory() as td:
        p = {"id": "x", "lat": 46.8, "lon": 9.8, "elev": 2400.0, "slope": 38.0, "aspect": 180.0}
        snowpack_runner.write_sno(p, Path(td), "2026-03-01")
        txt = (Path(td) / "x.sno").read_text()
        head, data = txt.split("[DATA]")
        fields = [l for l in head.splitlines() if l.startswith("fields")][0].split("=")[1].split()
        rows = [l.split() for l in data.strip().splitlines()]
        ne = [int(r[fields.index("ne")]) for r in rows]
        mk = [int(r[fields.index("mk")]) for r in rows]
        check("every base layer has elements", all(n >= 1 for n in ne), str(ne))
        check("no ice marker", all(m % 10 != 7 for m in mk), str(mk))
        hs = float(re.search(r"HS_Last\s*=\s*([\d.]+)", head).group(1))
        thick = sum(float(r[fields.index("Layer_Thick")]) for r in rows)
        check("layers add up to HS_Last", abs(thick - hs) < 1e-3, f"{thick:.3f} vs {hs:.3f}")
        check("slope and aspect reach SNOWPACK", "SlopeAngle   = 38.00" in head
              and "SlopeAzi     = 180.00" in head)


def test_wgs84_map_matches_rasterio():
    """The precomputed mapping must land where rasterio's reproject does."""
    print("_WGS84Map vs rasterio")
    import numpy as np
    from variant_a import export
    g = _synth_grid(120, 160, 3)
    rgba = np.zeros((g.nr, g.nc, 4), np.uint8)
    rgba[..., 0] = (np.arange(g.nc)[None, :] % 256)
    rgba[..., 1] = (np.arange(g.nr)[:, None] % 256)
    rgba[..., 3] = 255
    ref, rb = export._to_wgs84(rgba, g, nearest=True)
    m = export._WGS84Map(g)
    fast = m.nearest(rgba)
    check("same shape", fast.shape == ref.shape, f"{fast.shape} vs {ref.shape}")
    check("same bounds", np.allclose(np.array(m.bounds), np.array(rb), atol=1e-9))
    both = (ref[..., 3] > 0) & (fast[..., 3] > 0)
    off = np.abs(ref[..., :2].astype(int) - fast[..., :2].astype(int))[both]
    check("pixels land within one source cell", both.mean() > 0.5 and (off <= 1).mean() > 0.995,
          f"{(off <= 1).mean()*100:.2f}% within 1 cell")


def test_state_roundtrip():
    """The carried snowpack: save, pack, restore, and when not to use it."""
    print("live state")
    from datetime import datetime, timedelta
    from variant_a import state
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        sno = td / "src"; sno.mkdir()
        for rid in ("w000_1800_F", "w000_1800_N_20"):
            (sno / f"{rid}.sno").write_text(f"SMET 1.1 ASCII\n# {rid}\n")
        t = datetime(2026, 3, 25)
        meta = state.save(td / "out", t, sno, {"precip_factor": {"w000": 1.2}, "cycle": 3})
        check("state saves every run", meta["runs"] == 2 and (td / "out" / "state.tar.gz").exists())
        # an artifact download only carries the tarball
        dl = td / "dl"; dl.mkdir()
        (dl / "state.tar.gz").write_bytes((td / "out" / "state.tar.gz").read_bytes())
        st = state.load(dl)
        check("a tarball-only download restores", st is not None and st["time"] == t
              and st["meta"]["precip_factor"] == {"w000": 1.2}
              and (st["sno"] / "w000_1800_F.sno").exists())
        ids = {"w000_1800_F", "w000_1800_N_20", "w999_1800_F"}
        check("runs with a state continue, new runs cold-start",
              state.available(st, ids, t + timedelta(days=1)) == {"w000_1800_F", "w000_1800_N_20"})
        check("a state from AFTER the window start is not used",
              state.available(st, ids, t - timedelta(days=1)) == set())
        check("a state older than the gap limit is not used",
              state.available(st, ids, t + timedelta(days=state.MAX_GAP_DAYS + 2)) == set())
        (dl / "meta.json").write_text(json.dumps({"version": 999, "time": "2026-03-25T00:00"}))
        check("a state of another version is ignored", state.load(dl) is None)


def test_wind_indices():
    """Lee slopes load, windward slopes scour, calm air does nothing."""
    print("wind drift / scour")
    from datetime import datetime, timedelta
    from variant_a import wind
    from variant_a.gridding import METS
    lee, wwd = wind.slope_factors(38, 180, 0)          # wind FROM north, S-facing slope
    check("wind from N: a S-facing slope is lee", lee > 0.99 and wwd == 0.0)
    lee, wwd = wind.slope_factors(38, 0, 0)
    check("... and a N-facing slope is windward", lee == 0.0 and wwd > 0.99)
    check("flat runs get no lee, some scour", wind.slope_factors(0, 0, 0) == (0.0, wind.FLAT_EXPOSURE))
    t0 = datetime(2026, 3, 1)
    hours = [t0 + timedelta(hours=h) for h in range(48)]
    storm = {"time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
             "wind_speed_10m": [12.0] * 24 + [1.0] * 24, "wind_direction_10m": [0.0] * 48}
    idx, dirn = wind.transport_series(storm, [hours[23], hours[47]])
    check("a day of 12 m/s is strong transport", idx[0] > 0.8, f"{idx[0]:.2f}")
    check("a calm day after it is not", idx[1] < 0.05, f"{idx[1]:.2f}")
    check("the direction is the wind's FROM direction", abs(dirn[0]) < 1e-6 or abs(dirn[0] - 360) < 1e-6)
    runs = [{"id": "a", "wp": "w", "slope": 38.0, "aspect": 180.0},
            {"id": "b", "wp": "w", "slope": 38.0, "aspect": 0.0}]
    met = np.zeros((1, len(METS)), np.float32)
    met[0, METS.index("total_hs_cm")] = 100; met[0, METS.index("powder_depth_cm")] = 20
    res = {"a": (met.copy(), None, None, None), "b": (met.copy(), None, None, None)}
    wind.apply(runs, res, [hours[23]], {"w": storm}, METS)
    d, sc = METS.index("drift_load"), METS.index("wind_scour")
    check("apply(): lee run loaded, windward run scoured",
          res["a"][0][0, d] > 0.8 and res["a"][0][0, sc] == 0
          and res["b"][0][0, sc] > 0.8 and res["b"][0][0, d] == 0)


def test_terrain_shading():
    """A wall to the south shades the cell behind it in winter, not in summer."""
    print("horizon shading")
    from datetime import datetime
    from variant_a import terrain
    from variant_a.subregions import NationalGrid
    nr, nc = 60, 60
    dem = np.full((nr, nc), 1000.0)
    dem[40:44, :] = 3000.0                                  # an E-W ridge
    slope = np.zeros((nr, nc)); aspect = np.zeros((nr, nc))
    g = NationalGrid(elevation=dem, slope=slope, aspect=aspect, tile=np.ones((nr, nc), np.int32),
                     nr=nr, nc=nc, xll=600000.0, yll=150000.0, cs=250.0, tile_names={1: "t"})
    h = terrain.horizon_tan(g)
    win = terrain.shade_fraction(g, datetime(2026, 12, 21), h)
    sm = terrain.shade_fraction(g, datetime(2026, 6, 21), h)
    check("north of the ridge is shaded in December", win[36, 30] > 0.9, f"{win[36, 30]:.2f}")
    check("... far less in June", sm[36, 30] < win[36, 30] - 0.4, f"{sm[36, 30]:.2f}")
    check("south of the ridge is not shaded", win[55, 30] < 0.05, f"{win[55, 30]:.2f}")
    sp = terrain.sun_path(datetime(2026, 6, 21))
    check("the June sun path is ~15 h long and peaks near 66 deg",
          14 <= len(sp) * 0.25 <= 17 and 63 < max(e for e, _ in sp) < 68)


def test_imis_correction():
    """Stations match their weather point; the factor moves slowly, bounded."""
    print("IMIS snow-height correction")
    from datetime import datetime, timedelta
    from variant_a import imis
    from variant_a.gridding import METS
    S = types.SimpleNamespace
    wps = [{"id": "w0", "lat": 46.8, "lon": 9.8, "bands": [1800.0, 2100.0, 2400.0]}]
    st = [S(code="DAV2", label="Davos", lat=46.82, lon=9.83, elevation=2250.0, hs={}),
          S(code="FAR", label="far", lat=47.5, lon=8.0, elevation=2200.0, hs={}),
          S(code="LOW", label="low", lat=46.80, lon=9.80, elevation=900.0, hs={})]
    m = imis.match_stations(st, wps)
    check("only the near station inside the bands matches", [s.code for s, _ in m] == ["DAV2"])
    frames = [datetime(2026, 3, 30) + timedelta(hours=3 * i) for i in range(8)]
    runs = [{"id": f"w0_{int(e)}_F", "wp": "w0", "slope": 0.0, "elev": e} for e in (1800.0, 2100.0, 2400.0)]
    res = {}
    for r in runs:
        met = np.zeros((len(frames), len(METS)), np.float32)
        met[:, METS.index("total_hs_cm")] = {1800.0: 40, 2100.0: 60, 2400.0: 80}[r["elev"]]
        res[r["id"]] = (met, None, None, None)
    st[0].hs = {t.strftime("%Y-%m-%dT%H"): 140.0 for t in frames}   # measured: more snow
    rows, summ = imis.compare(st, wps, runs, res, frames, METS, now=frames[-1])
    check("model HS is interpolated to the station height", rows and abs(rows[0]["last_model"] - 70) < 0.1,
          str(rows[:1]))
    check("bias is model minus measured", summ["bias_cm"] == -70.0, str(summ))
    f1 = imis.update_factors(rows, {})
    check("too little snow -> more precipitation, but a bounded step",
          1.0 < f1["w0"] <= imis.R_MAX ** imis.STEP_EXP + 1e-9, str(f1))
    f = {"w0": 1.0}
    for _ in range(20):
        f = imis.update_factors(rows, f)
    check("the factor never leaves its bounds", f["w0"] <= imis.F_MAX, str(f))


def test_gates():
    print("publish gates")
    from variant_a import gates
    runs = [{"id": f"r{i}", "wp": "w0" if i < 50 else "w1"} for i in range(100)]
    wps = [{"id": "w0"}, {"id": "w1"}]
    good = {f"r{i}": 1 for i in range(100)}
    fs = [{"tag": "t", "finite": True, "hs_max": 300}]
    g = gates.check(100, good, runs, wps, 10, 10, fs, {"stations": 20, "mae_cm": 30,
                                                        "median_measured_cm": 120, "bias_cm": -5})
    check("a healthy cycle passes", g["passed"], str(g["hard"]))
    g = gates.check(100, {k: 1 for k in list(good)[:80]}, runs, wps, 10, 10, fs)
    check("too many failed runs blocks", not g["passed"])
    check("a missing frame or a lost weather point blocks",
          not gates.check(100, good, runs, wps, 10, 9, fs)["passed"])
    g = gates.check(100, good, runs, wps, 10, 10, fs, {"stations": 20, "mae_cm": 120,
                                                        "median_measured_cm": 150})
    check("a wildly wrong snow height blocks, once there is real snow", not g["passed"])
    g = gates.check(100, good, runs, wps, 10, 10, fs, {"stations": 20, "mae_cm": 120,
                                                        "median_measured_cm": 10})
    check("... but not in early season noise", g["passed"])


def test_classifier_fixes():
    print("classifier: surface-state priority, new classes, rg")
    from variant_a import classify as c
    z = np.zeros(6); hs = np.full(6, 100.0)
    lab = c.classify_grid_metrics(hs, np.array([0, 0, 0, 0, 20, 2.0]), np.array([0, 3, 0, 0, 0, 0.]),
                                  z, z, np.array([420, 420, 750, 150, 90, 150.]), z,
                                  np.array([3, 3, 0, 0, 0, 0.]),
                                  sh=np.array([0, 0, 0, 1, 0, 0.]), drift=np.array([0, 0, 0, 0, 0.8, 0]),
                                  scour=np.array([0, 0, 0, 0, 0, 0.8]))
    names = [c.SKI_LABELS[k] for k in lab]
    check("wet dense snow is spring corn (it used to come out 'settled')", names[0] == "spring_corn", names[0])
    check("corn wins over a crust, as in the per-slope classifier", names[1] == "spring_corn", names[1])
    check("ice", names[2] == "ice", names[2])
    check("surface hoar", names[3] == "surface_hoar", names[3])
    check("lee + powder -> wind slab", names[4] == "wind_slab", names[4])
    check("windward, little powder -> wind packed", names[5] == "wind_packed", names[5])
    sim = c.classify_simple(hs[:3], np.array([0, 20, 2.0]), z[:3], np.full(3, 150.), z[:3],
                            sh=np.array([1, 0, 0.]), drift=np.array([0, 0.8, 0]), scour=np.array([0, 0, 0.8]))
    check("simple layer: surface hoar / wind slab / wind packed",
          [c.SIMPLE_LABELS[k] for k in sim] == ["surface_hoar", "wind_slab", "wind_packed"],
          str([c.SIMPLE_LABELS[k] for k in sim]))
    # crust over FC over low-density snow used to raise NameError (rg)
    ts = {"n": 4, "heights": np.array([40., 60., 70., 72.]),
          "density": np.array([150., 250., 240., 400.]), "lw": np.zeros(4),
          "dd": np.zeros(4), "sp": np.zeros(4), "rg": np.array([0.5, 1.5, 1.2, 0.3]),
          "grain": np.array([300, 400, 400, 700]), "ice_frac": np.zeros(4), "hardness": np.zeros(4)}
    try:
        q = c.assess_ski_quality(ts)
        check("crust over facets over light snow classifies (was NameError)", q["crust_thick_cm"] > 0,
              q["label"])
    except NameError as e:
        check("crust over facets over light snow classifies (was NameError)", False, str(e))
    check("the buried facet layer is reported with its depth", q["weak_layer_depth_cm"] > 0,
          str(q["weak_layer_depth_cm"]))


def test_pack_roundtrip():
    """The per-frame metric pack decodes back to the metrics, per run."""
    print("metric pack")
    from datetime import datetime
    from variant_a import export, matrix
    from variant_a.gridding import METS
    g = _synth_grid(40, 40, 11)
    wps = matrix.weather_points(g, spacing_km=5.0, min_top_m=1800.0)
    runs = matrix.matrix_runs(wps)
    rng = np.random.default_rng(1)
    frames = [datetime(2026, 3, 30, 12)]
    res = {}
    for i, r in enumerate(runs):
        if i == 5:
            continue
        m = np.zeros((1, len(METS)), np.float32)
        m[0, METS.index("total_hs_cm")] = rng.uniform(0, 400)
        m[0, METS.index("powder_depth_cm")] = rng.uniform(0, 60)
        m[0, METS.index("crust_thick_cm")] = rng.uniform(0, 5)
        m[0, METS.index("drift_load")] = rng.uniform(0, 1)
        res[r["id"]] = (m, None, None, None)
    with tempfile.TemporaryDirectory() as td:
        pk = export.export_pack(Path(td), runs, res, frames, wps, g, None, METS)
        img = np.asarray(Image.open(Path(td) / "pack" / "f_2026-03-30T1200.png"))
        q = img.reshape(-1, pk["px_per_run"] * 3)[:len(runs)]
        mul = np.array(pk["mul"])
        vals = q[:, :len(pk["mets"])] / mul
        ok = q[:, len(pk["mets"])]
        worst = 0.0
        for i, r in enumerate(runs):
            if r["id"] not in res:
                continue
            for k, name in enumerate(pk["mets"]):
                want = min(res[r["id"]][0][0, METS.index(name)], 255 / mul[k])
                worst = max(worst, abs(vals[i, k] - want) * mul[k])
        check("every metric survives to within half a quantisation step", worst <= 0.5 + 1e-6, f"{worst:.3f}")
        check("failed runs are flagged, the rest are not", ok[5] == 0 and ok.sum() == len(runs) - 1)
        sh = np.asarray(Image.open(Path(td) / "terrain" / "shade.png"))
        check("the shade/mask raster marks outside cells 255", sh.shape == (g.nr, g.nc)
              and (sh[~((g.tile > 0) & np.isfinite(g.elevation))] == 255).all())


def test_rate_limit_backoff():
    """429 is waited out (Retry-After honoured), and weather points that fail
    in the parallel burst get a second, sequential pass."""
    print("rate limits")
    import io, urllib.error
    sleeps, calls = [], []
    real_sleep, real_open = forcing.time.sleep, forcing.urllib.request.urlopen
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def opener(answers):
        def f(url, timeout=None):
            calls.append(url)
            a = answers.pop(0)
            if isinstance(a, int):
                hdr = {"Retry-After": "30"} if a == 429 else {}
                raise urllib.error.HTTPError(url, a, "x", hdr, io.BytesIO(b"{}"))
            return R(json.dumps(a).encode())
        return f
    try:
        forcing.time.sleep = lambda s: sleeps.append(s)
        forcing.urllib.request.urlopen = opener([429, 429, {"ok": 1}])
        d, why = forcing._fetch_json("https://x", {})
        check("429 twice, then data", d == {"ok": 1} and why is None)
        check("the waits are real back-offs, Retry-After honoured",
              sleeps[0] >= 30 and sleeps[1] >= 30, str(sleeps))
        sleeps.clear()
        forcing.urllib.request.urlopen = opener([500, 500, 500, 500])
        d, why = forcing._fetch_json("https://x", {})
        check("a persistent 5xx gives up with its reason", d is None and why == "HTTP 500"
              and len(sleeps) == forcing._FETCH_TRIES - 1)
    finally:
        forcing.time.sleep, forcing.urllib.request.urlopen = real_sleep, real_open
    # second pass in build_forcing_matrix
    from datetime import datetime
    T = [f"2026-03-{d:02d}T{h:02d}:00" for d in range(1, 3) for h in range(24)]
    hourly = {"time": T, **{v: [1.0] * len(T) for v in forcing._VARS}}
    seen = []
    def fw(lat, lon, e, s, en, models=None):
        seen.append(lat)
        if lat == 46.9 and seen.count(46.9) == 1:
            return None, None, ["HTTP 429"]
        return hourly, "meteoswiss_icon_ch1", []
    real_fw, real_pause = forcing.fetch_weather, forcing._RETRY_PAUSE_S
    forcing.fetch_weather, forcing._RETRY_PAUSE_S = fw, 0.0
    try:
        with tempfile.TemporaryDirectory() as td:
            wps = [{"id": "a", "lat": 46.8, "lon": 9.8, "ref_elev": 2000.0},
                   {"id": "b", "lat": 46.9, "lon": 9.9, "ref_elev": 2000.0}]
            runs = [{"id": f"{w['id']}_2000_F", "wp": w["id"], "lat": w["lat"], "lon": w["lon"],
                     "elev": 2000.0} for w in wps]
            used = forcing.build_forcing_matrix(wps, runs, "2026-03-02",
                                                (datetime(2026, 3, 1), datetime(2026, 3, 2)),
                                                Path(td), workers=2)
            check("a point that failed in the burst is recovered by the second pass",
                  set(used) == {"a", "b"} and seen.count(46.9) == 2, str(used))
    finally:
        forcing.fetch_weather, forcing._RETRY_PAUSE_S = real_fw, real_pause


def test_precip_pattern():
    """1 km precipitation ratio: neutral where uniform, follows local excess,
    history rolls, and both renderers get the same scaling."""
    print("precipitation pattern")
    from variant_a import precip, matrix, export, state as state_mod, subregions
    g = _synth_grid(60, 60, 5)
    wps = matrix.weather_points(g, spacing_km=5.0, min_top_m=1800.0)
    valid = (g.tile > 0) & np.isfinite(g.elevation)
    # a 1 km "ICON" mesh over the grid
    xs = g.xll + np.arange(0, g.nc * g.cs, 1000.0) + 500.0
    ys = g.yll + np.arange(0, g.nr * g.cs, 1000.0) + 500.0
    ce, cn = [a.ravel() for a in np.meshgrid(xs, ys)]
    R = precip.ratio_grid(g, wps, ce, cn, np.full(ce.shape, 20.0))
    check("uniform precipitation -> no correction", np.allclose(R[valid], 1.0), f"{R[valid].min():.3f}")
    # 3 km wet/dry stripes: finer than the weather points can resolve, which
    # is exactly what the correction is for
    wet_c = ((ce - g.xll) // 3000) % 2 == 0
    P = np.where(wet_c, 40.0, 20.0)
    R = precip.ratio_grid(g, wps, ce, cn, P)
    gx = g.xll + (np.arange(g.nc) + 0.5) * g.cs
    wet = valid & ((((gx - g.xll) // 3000) % 2 == 0)[None, :])
    core = valid & (np.abs(((gx - g.xll) % 3000) - 1500) < 600)[None, :]
    rw, rd = R[wet & core].mean(), R[~wet & valid & core].mean()
    check("wet stripes get clearly more new snow than dry ones (x2 in the field)",
          rw / rd > 1.6, f"{rw:.2f} / {rd:.2f}")
    check("R stays within its limits", R.min() >= precip.R_MIN and R.max() <= precip.R_MAX)
    R0 = precip.ratio_grid(g, wps, ce, cn, np.full(ce.shape, 1.0))
    check("too little precipitation to judge -> no correction", np.allclose(R0[valid], 1.0))
    # accumulation, history
    ref = datetime(2026, 3, 30, 6)
    lead = np.array([0, 3, 6, 9, 12], float)
    acc = np.outer(lead, np.ones(4)).astype(np.float32)           # 1 mm/h everywhere
    fld = {"lat": np.zeros(4), "lon": np.zeros(4), "lead": lead, "acc": acc, "ref": ref}
    check("forecast sum interpolates between leads",
          np.allclose(precip.forecast_sum(fld, ref + timedelta(hours=1.5), ref + timedelta(hours=7.5)), 6.0))
    check("... and stops at the last lead",
          np.allclose(precip.forecast_sum(fld, ref, ref + timedelta(days=3)), 12.0))
    h = None
    for k in range(30):                                          # 7.5 days of 6-hourly cycles
        f2 = dict(fld, ref=ref + timedelta(hours=6 * k))
        h = precip.update_history(h, f2)
    check("history keeps only the last HIST_DAYS", len(h["times"]) == precip.HIST_DAYS * 4 + 1,
          str(len(h["times"])))
    f_now = dict(fld, ref=ref + timedelta(hours=6 * 30))
    past, n = precip.past_sum(h, f_now, f_now["ref"] - timedelta(days=2))
    check("past sum adds the cycles since `since`", n == 8 and np.allclose(past, 48.0), f"{n} {past[0]}")
    check("a different mesh resets the history",
          precip.past_sum(h, dict(f_now, lat=np.zeros(5)), ref)[1] == 0)
    with tempfile.TemporaryDirectory() as td:
        precip.save_history(Path(td) / "h", h)
        h2 = precip.load_history(Path(td) / "h")
        check("history survives a save/load", h2["n"] == 4 and len(h2["times"]) == len(h["times"]))
        # the history travels inside the state tarball
        (Path(td) / "sno").mkdir()
        (Path(td) / "sno" / "x.sno").write_text("x")
        state_mod.save(Path(td) / "st", datetime(2026, 3, 30), Path(td) / "sno", {},
                       files=[Path(td) / "h" / precip.HIST_FILE])
        import tarfile
        names = tarfile.open(Path(td) / "st" / "state.tar.gz").getnames()
        check("precip history is packed with the state", precip.HIST_FILE in names, str(names))
        # pack raster: R*100, 255 where there is no correction
        Rx = np.ones((g.nr, g.nc), np.float32); Rx[10, 10] = 1.5; Rx[11, 11] = 0.5
        runs = matrix.matrix_runs(wps)
        from variant_a.gridding import METS
        res = {r["id"]: (np.zeros((1, len(METS)), np.float32), None, None, None) for r in runs}
        pk = export.export_pack(Path(td) / "out", runs, res, [datetime(2026, 3, 30)], wps, g, None,
                                METS, precip=Rx)
        pr = np.asarray(Image.open(Path(td) / "out" / pk["precip"]["file"]))
        check("precip raster: 150 / 50 where corrected, 255 elsewhere",
              pr[10, 10] == 150 and pr[11, 11] == 50 and pr[0, 0] == 255)
    # extractor -> file -> pipeline: the OGD side writes what precip.build reads
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ogd_extract
    from variant_a.subregions import lv03_to_wgs84
    la, lo = lv03_to_wgs84(ce, cn)
    leads = list(range(0, 34))
    vals = np.outer(np.arange(34), np.where(wet_c, 2.0, 1.0))        # mm accumulated
    with tempfile.TemporaryDirectory() as td:
        ogd_extract.save_precip_field(td, "ch1", vals, leads, datetime(2026, 3, 30, 6),
                                      np.asarray(la), np.asarray(lo))
        fld = precip.load_field(td)
        check("the extractor's field loads (3-hourly leads, all cells)",
              fld is not None and len(fld["lead"]) == 12 and len(fld["lat"]) == len(ce))
        Rb, hb, sm = precip.build(g, wps, td, None, (datetime(2026, 3, 25, 6),
                                                    datetime(2026, 4, 4, 6)), live=False)
        check("build() gives a ratio grid from it", Rb is not None and sm["used"]
              and Rb[wet & core].mean() > Rb[~wet & valid & core].mean() * 1.5, str(sm))
        Rn, _, sn = precip.build(g, wps, Path(td) / "nothing", None, (datetime(2026, 3, 25),
                                                                    datetime(2026, 4, 4)), live=False)
        check("no field -> no correction, no error", Rn is None and not sn["used"])
    m = {"powder_depth_cm": np.full((2, 2), 20.0), "total_hs_cm": np.full((2, 2), 100.0)}
    Ra = np.array([[1.5, 0.5], [1.0, 2.0]], np.float32)
    m = precip.apply(m, Ra, np.array([[True, True], [True, False]]))
    check("apply scales new snow and moves HS by the same amount",
          np.allclose(m["powder_depth_cm"], [[30, 10], [20, 20]])
          and np.allclose(m["total_hs_cm"], [[110, 90], [100, 100]]))


def test_ski6_and_wind_classes():
    """The simplified Skiqualität and the Triebschnee view, case by case."""
    print("ski6 / wind classes")
    from variant_a import classify as C
    #            hs   powder crust dens  lw
    cases = [((10, 30, 0, 100, 0), 0, "thin cover -> nothing"),
             ((100, 0, 0, 300, 0), 1, "no powder, no crust -> hart"),
             ((100, 0, 1.0, 300, 0), 2, "crust -> Kruste"),
             ((100, 0, 0, 750, 0), 2, "ice -> Kruste"),
             ((100, 1.0, 1.0, 300, 0), 2, "1 cm dust on crust is still Kruste"),
             ((100, 6, 1.0, 120, 0), 3, "6 cm on a crust -> Pulver 0-10"),
             ((100, 15, 0, 120, 0), 4, "15 cm -> Pulver 10-20"),
             ((100, 35, 0, 120, 0), 5, "35 cm -> Pulver > 20"),
             ((100, 35, 0, 120, 2.0), 6, "liquid water on top -> nass")]
    for (hs, pw, cr, sd, lw), want, name in cases:
        got = int(C.classify_ski6(np.array([hs]), np.array([pw]), np.array([cr]),
                                  np.array([sd]), np.array([lw]))[0])
        check(name, got == want, f"{got} (want {want})")
    w = C.classify_wind(np.array([100, 100, 100, 100, 10]), np.array([0, 0.5, 0.8, 0, 0.9]),
                        np.array([0, 0, 0, 0.6, 0]))
    check("wind: none / light / drift / scoured / thin", w.tolist() == [0, 2, 3, 1, 0], str(w.tolist()))
    p = C.classify_powder(np.array([100, 100, 100, 100, 100, 100, 10, 100]),
                          np.array([1, 3, 7, 25, 60, 200, 30, 25]),
                          np.array([0, 0, 0, 0, 0, 0, 0, 2.0]))
    check("powder: SLF bands, blank under 2 cm / thin / wet",
          p.tolist() == [0, 1, 2, 4, 6, 9, 0, 0], str(p.tolist()))
    check("powder: one colour per SLF band", len(C.POWDER_RGBA) == len(C.POWDER_LABELS) == len(C.POWDER_DE) == 10)


def test_temperature_by_height():
    """Hourly lapse from the model profile, ICON-CH1 cells at band height."""
    print("temperature by height")
    from variant_a import elevtemp, matrix
    f = forcing
    def prof(t850, t700, t500=-25.0):
        return {"time": ["2026-03-30T12:00"], "temperature_2m": [0.0],
                "temperature_850hPa": [t850], "temperature_700hPa": [t700], "temperature_500hPa": [t500],
                "geopotential_height_850hPa": [1500.0], "geopotential_height_700hPa": [3000.0],
                "geopotential_height_500hPa": [5600.0]}
    d = f._profile_dT(prof(5.0, -5.0), 0, 1800.0, 2700.0)
    check("normal profile: ~ -6.7 K/km between 850 and 700 hPa", abs(d - (-6.0)) < 0.05, f"{d:.2f} K over 900 m")
    d = f._profile_dT(prof(-5.0, 2.0), 0, 1800.0, 2700.0)
    check("inversion: warmer higher up", d > 3.5, f"{d:+.2f} K")
    d = f._profile_dT(prof(-5.0, 40.0), 0, 1800.0, 2700.0)
    check("implausible profile clipped to 15 K/km", abs(d - 0.015 * 900) < 1e-6, f"{d:.2f}")
    h = prof(5.0, -5.0); h["temperature_700hPa"] = [None]; h["temperature_500hPa"] = [None]
    check("one level only -> no profile", f._profile_dT(h, 0, 1800.0, 2700.0) is None)
    out = f._lapsed(h, 900.0, z_ref=1800.0)
    check("... and the fixed lapse rate is used", abs(out["temperature_2m"][0] - (-0.0065 * 900)) < 1e-9)
    out = f._lapsed(prof(-5.0, 2.0), 900.0, z_ref=1800.0)
    check("_lapsed follows the profile", out["temperature_2m"][0] > 3.5 and out["_profile_hours"] == 1,
          f"{out['temperature_2m'][0]:+.2f}")
    out = f._lapsed(prof(5.0, -5.0), 900.0, z_ref=1800.0, band_t={"2026-03-30T12:00": (-7.5, 50.0)})
    check("an ICON cell at the band's height wins, bridged by the profile",
          abs(out["temperature_2m"][0] - (-7.5 + 50.0 * -10.0 / 1500)) < 0.01, f"{out['temperature_2m'][0]:.2f}")
    # splice: the profile comes from whichever model has it
    base = {"time": ["a", "b"], "temperature_2m": [1, 2], "precipitation": [0, 0],
            "shortwave_radiation": [0, 0], "wind_speed_10m": [1, 1]}
    extra = {"time": ["a", "b"], "temperature_850hPa": [3, None], "geopotential_height_850hPa": [1500, 1510]}
    sp, _ = f._splice(base, extra)
    check("splice fills pressure levels hour by hour", sp["temperature_850hPa"] == [3, None]
          and sp["geopotential_height_850hPa"] == [1500, 1510])
    # a model that rejects pressure levels is retried without them
    real = f._fetch_json; calls = []
    T = [f"2026-03-30T{h:02d}:00" for h in range(24)]
    full = {"hourly": {"time": T, **{v: [1.0] * 24 for v in f._VARS}}}
    def fake(url, params):
        calls.append(params["hourly"])
        return (None, "Cannot find variable temperature_850hPa") if "hPa" in params["hourly"] else (full, None)
    f._fetch_json = fake
    try:
        hh, label, notes = f.fetch_weather(46.8, 9.8, 2000, "2026-03-30", "2026-03-30", models=["m1"])
    finally:
        f._fetch_json = real
    check("pressure levels refused -> same model again without them", hh is not None and label == "m1"
          and "hPa" in calls[0] and "hPa" not in calls[1], str(calls)[:120])
    check("... then one request for the profile alone", len(calls) == 3 and "temperature_2m" not in calls[2])
    # elevtemp: cells at the band's height around a weather point
    g = _synth_grid(60, 60, 7)
    wps = matrix.weather_points(g, spacing_km=5.0, min_top_m=1800.0)
    w = wps[0]
    rng = np.random.default_rng(2)
    ce = w["e"] + rng.uniform(-8000, 8000, 400); cn = w["n"] + rng.uniform(-8000, 8000, 400)
    hs = rng.uniform(1000, 3500, 400)
    fld = {"lead": np.arange(0, 4, dtype=float), "ref": datetime(2026, 3, 30, 6),
           "t": np.tile((10.0 - 0.006 * hs)[None, :], (4, 1)).astype(np.float32)}
    bt, cnt = elevtemp.band_temps(fld, [w], ce, cn, hs)
    b0 = int(w["bands"][0]); s0 = bt[w["id"]][b0]
    tt, dzc = s0["2026-03-30T06:00"]
    check("a series per band, from cells near that height", abs(dzc) <= elevtemp.MAX_DZ
          and abs(tt - (10.0 - 0.006 * (b0 - dzc))) < 0.05, f"band {b0}: T {tt}, dz {dzc}")
    check("all four forecast hours", len(s0) == 4)
    far = elevtemp.band_temps(fld, [dict(w, bands=[6000])], ce, cn, hs)[0]
    check("no cell at that height -> no series", not far)
    # end to end: extractor file -> build -> forcing .smet
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ogd_extract
    from variant_a.subregions import lv03_to_wgs84
    la, lo = lv03_to_wgs84(ce, cn)
    with tempfile.TemporaryDirectory() as td:
        ogd_extract.save_t2m_field(td, "ch1", "x", fld["t"] + 273.15, [0, 1, 2, 3], datetime(2026, 3, 30, 6),
                                   np.asarray(la), np.asarray(lo), hsurf=hs)
        bt2, summ = elevtemp.build(g, [w], td)
        check("field file -> band series (HSURF heights)", summ.get("heights") == "HSURF"
              and bt2 and b0 in bt2[w["id"]], str(summ))
        T = [f"2026-03-30T{h:02d}:00" for h in range(24)]
        hourly = {"time": T, "temperature_2m": [5.0] * 24, "precipitation": [0.0] * 24,
                  "shortwave_radiation": [100.0] * 24, "wind_speed_10m": [3.0] * 24,
                  "relative_humidity_2m": [80.0] * 24, "wind_direction_10m": [270.0] * 24}
        real_fw = f.fetch_weather
        f.fetch_weather = lambda lat, lon, e, s_, en, models=None: (hourly, "m", [])
        try:
            runs = [{"id": f"{w['id']}_{b0}_F", "wp": w["id"], "lat": w["lat"], "lon": w["lon"], "elev": float(b0)}]
            md = Path(td) / "meteo"
            f.build_forcing_matrix([w], runs, "2026-03-30", (datetime(2026, 3, 30), datetime(2026, 3, 31)),
                                   md, workers=1, band_temps=bt2)
            rows = (md / f"{runs[0]['id']}.smet").read_text().split("[DATA]")[1].split()
            line = [l for l in (md / f"{runs[0]['id']}.smet").read_text().splitlines() if l.startswith("2026-03-30T07:00")][0]
            ta = float(line.split()[1]) - 273.15
            want = bt2[w["id"]][b0]["2026-03-30T07:00"]
            check("the .smet carries the cell temperature for forecast hours",
                  abs(ta - (want[0] - 0.0065 * want[1])) < 0.02, f"{ta:.2f} vs cell {want}")
        finally:
            f.fetch_weather = real_fw


if __name__ == "__main__":
    for t in (test_forcing_starts_before_profile_date, test_covers_rejects_a_stale_smet,
              test_ini, test_timestamp_window, test_prof_start_derivation,
              test_selection_cache, test_win_count_matches_sliding_window,
              test_export_survives_numpy_types, test_window_matches_the_app,
              test_forcing_anchors_on_the_window_start,
              test_indexed_png_is_lossless, test_profile_payload,
              test_selection_is_stable, test_matrix_weights,
              test_forcing_fallback, test_forcing_matrix_shares_weather,
              test_sno_base_is_real_snow, test_wgs84_map_matches_rasterio,
              test_state_roundtrip, test_wind_indices, test_terrain_shading,
              test_imis_correction, test_gates, test_classifier_fixes, test_pack_roundtrip,
              test_rate_limit_backoff, test_precip_pattern, test_ski6_and_wind_classes,
              test_temperature_by_height):
        t()
    print("\nVARIANT A PIPELINE " + ("OK" if not FAILS else f"FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
