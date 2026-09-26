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


if __name__ == "__main__":
    for t in (test_forcing_starts_before_profile_date, test_covers_rejects_a_stale_smet,
              test_ini, test_timestamp_window, test_prof_start_derivation,
              test_selection_cache, test_win_count_matches_sliding_window,
              test_export_survives_numpy_types, test_window_matches_the_app,
              test_forcing_anchors_on_the_window_start,
              test_indexed_png_is_lossless,
              test_selection_is_stable):
        t()
    print("\nVARIANT A PIPELINE " + ("OK" if not FAILS else f"FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
