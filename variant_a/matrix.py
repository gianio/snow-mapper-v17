"""Weather-point matrix: the Disentis design, applied nationally.

Weather is sampled at a FEW points. Around each one SNOWPACK runs a full
matrix of virtual slopes -- every elevation band the local terrain spans, times
eight aspects, times a moderate and a steep slope, plus one flat run per band
-- and all of them share that point's weather. So within a weather point the
only differences between runs are height, aspect and slope angle, which is
exactly the signal the ski classes are about.

The grid is then filled by similarity rather than by nearest location: each
cell takes its values from the runs at the nearest weather points whose
height, aspect and slope bracket its own, blended trilinearly, and the weather
points themselves are blended by inverse distance. That blend is precomputed
once as a sparse (cells x runs) weight matrix, so each output frame is a single
sparse product.

This replaces the representative-point design (select_points), which picked
the best DEM cell for each height x aspect x slope anywhere in a ~50 km
subregion and gave each its own weather. There, "north vs south at 2400 m"
was also "weather cell A vs weather cell B".
"""
from __future__ import annotations
import glob, os, shutil, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.spatial import cKDTree

from . import config
from .subregions import NationalGrid, cell_to_lv03, lv03_to_wgs84, lv03_to_lv95

ASPECT_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
FLAT = 0                                  # slope-class index of the flat run


def band_values():
    """All candidate elevation bands, e.g. 1200, 1500, ... 3300."""
    return np.arange(config.MATRIX_ELEV_MIN, config.MATRIX_ELEV_MAX + 1e-6,
                     config.MATRIX_ELEV_STEP)


def slope_nodes():
    """Slope-class angles, flat first: (0, 20, 38) by default."""
    return (0.0,) + tuple(float(s) for s in config.MATRIX_SLOPES)


def weather_points(grid: NationalGrid, spacing_km=None, min_top_m=None):
    """Sample points on a regular grid over the alpine terrain.

    One per square of `spacing_km`, placed at the centroid of that square's
    valid (subregion) cells. A square is kept only if its terrain reaches
    `min_top_m` -- below that there is nothing to ski -- and it carries the
    elevation bands its own terrain spans (5th..98th percentile), padded by
    one band either side so every real cell is bracketed for interpolation.
    """
    spacing_km = spacing_km or config.WEATHER_SPACING_KM
    min_top_m = config.WEATHER_MIN_TOP_M if min_top_m is None else min_top_m
    step = max(1, int(round(spacing_km * 1000.0 / grid.cs)))
    bands_all = band_values()
    out = []
    for r0 in range(0, grid.nr, step):
        for c0 in range(0, grid.nc, step):
            blk = grid.elevation[r0:r0 + step, c0:c0 + step]
            tb = grid.tile[r0:r0 + step, c0:c0 + step]
            m = (tb > 0) & np.isfinite(blk)
            if m.sum() < 0.1 * step * step:
                continue
            v = blk[m]
            lo, hi = float(np.percentile(v, 5)), float(np.percentile(v, 98))
            if hi < min_top_m:
                continue
            pad = config.MATRIX_ELEV_STEP
            bands = [float(b) for b in bands_all if lo - pad <= b <= hi + pad]
            if not bands:
                continue
            rr, cc = np.nonzero(m)
            e, n = cell_to_lv03(grid, r0 + rr.mean(), c0 + cc.mean())
            lat, lon = lv03_to_wgs84(e, n)
            e95, n95 = lv03_to_lv95(e, n)
            vals, counts = np.unique(tb[m], return_counts=True)
            out.append({
                "id": f"w{len(out):03d}", "e": float(e), "n": float(n),
                "lat": round(float(lat), 6), "lon": round(float(lon), 6),
                "e_lv95": round(float(e95), 1), "n_lv95": round(float(n95), 1),
                "ref_elev": round(float(np.median(v)), 1),
                "terrain": [round(lo), round(hi)],
                "bands": bands, "tile": int(vals[np.argmax(counts)]),
            })
    return out


def matrix_runs(wps):
    """Every virtual slope SNOWPACK has to run, flat first within each band."""
    runs = []
    for w in wps:
        for bi, band in enumerate(w["bands"]):
            base = {"wp": w["id"], "lat": w["lat"], "lon": w["lon"],
                    "e_lv95": w["e_lv95"], "n_lv95": w["n_lv95"],
                    "elev": band, "band_i": bi, "tile": w["tile"]}
            runs.append({**base, "id": f"{w['id']}_{int(band)}_F",
                         "slope": 0.0, "aspect": 0.0, "slope_c": FLAT, "aspect_k": -1})
            for sc, slope in enumerate(config.MATRIX_SLOPES, start=1):
                for k in range(config.MATRIX_ASPECTS):
                    az = k * 360.0 / config.MATRIX_ASPECTS
                    runs.append({**base,
                                 "id": f"{w['id']}_{int(band)}_{ASPECT_NAMES[k] if config.MATRIX_ASPECTS == 8 else k}_{int(slope)}",
                                 "slope": float(slope), "aspect": az,
                                 "slope_c": sc, "aspect_k": k})
    return runs


def _lookup(wps, runs):
    """LUT[wp, band, slope_class, aspect] -> global run index (-1 if absent).

    The flat class ignores aspect, so all eight aspect slots of it point at the
    same flat run; the trilinear weights then simply add up on that run.
    """
    wid = {w["id"]: i for i, w in enumerate(wps)}
    nb = max(len(w["bands"]) for w in wps)
    ns = len(slope_nodes())
    na = config.MATRIX_ASPECTS
    lut = np.full((len(wps), nb, ns, na), -1, dtype=np.int64)
    for ri, r in enumerate(runs):
        wi = wid[r["wp"]]
        if r["slope_c"] == FLAT:
            lut[wi, r["band_i"], FLAT, :] = ri
        else:
            lut[wi, r["band_i"], r["slope_c"], r["aspect_k"]] = ri
    return lut


def build_weights(grid: NationalGrid, wps, runs, neighbours=None, shade=None):
    """Sparse (cells x runs) weights: IDW across weather points, trilinear in
    elevation band, slope class and aspect sector within each one.

    `shade` (nr, nc, 0..1) is the share of the direct beam the surrounding
    terrain blocks (terrain.shade_fraction). A shaded cell is blended by that
    share towards the NORTH-facing runs of its own height and steepness, i.e.
    it is treated as a slope that the sun does not reach -- the virtual slopes
    themselves have no horizon.

    Returns (W, cell_index) where cell_index are the flat grid indices of the
    rows. Cells outside the subregions, or without a DEM value, get no row.
    """
    valid = (grid.tile > 0) & np.isfinite(grid.elevation)
    rows, cols = np.nonzero(valid)
    cell_index = rows * grid.nc + cols
    aspect = grid.aspect[rows, cols].astype(np.float64)
    aspect = np.where(np.isfinite(aspect), aspect, 0.0) % 360.0
    W = _weights(grid, wps, runs, rows, cols, aspect, neighbours)
    if shade is not None:
        s = np.clip(shade[rows, cols].astype(np.float64), 0.0, 1.0)
        if (s > 0.01).any():
            Wn = _weights(grid, wps, runs, rows, cols, np.zeros_like(aspect), neighbours)
            W = (sparse.diags(1.0 - s).dot(W) + sparse.diags(s).dot(Wn)).tocsr().astype(np.float32)
    return W, cell_index


def _weights(grid, wps, runs, rows, cols, aspect, neighbours=None):
    k_nb = min(neighbours or config.MATRIX_NEIGHBOURS, len(wps))
    ce = grid.xll + (cols + 0.5) * grid.cs
    cn = grid.yll + (grid.nr - 1 - rows + 0.5) * grid.cs
    elev = grid.elevation[rows, cols].astype(np.float64)
    slope = np.nan_to_num(grid.slope[rows, cols].astype(np.float64), nan=0.0)

    tree = cKDTree(np.array([[w["e"], w["n"]] for w in wps]))
    dist, widx = tree.query(np.column_stack([ce, cn]), k=k_nb)
    if k_nb == 1:
        dist, widx = dist[:, None], widx[:, None]
    # IDW with a softening length of half the spacing, so a cell exactly on a
    # weather point does not take it with weight 1 and flip at the midline.
    d0 = 0.5 * config.WEATHER_SPACING_KM * 1000.0
    wh = 1.0 / (dist + d0) ** 2
    wh /= wh.sum(axis=1, keepdims=True)

    lut = _lookup(wps, runs)
    b0 = np.array([w["bands"][0] for w in wps])
    nb = np.array([len(w["bands"]) for w in wps])

    # slope: piecewise-linear between the class nodes (0, 20, 38 deg)
    nodes = np.array(slope_nodes())
    s = np.clip(slope, nodes[0], nodes[-1])
    sc0 = np.clip(np.searchsorted(nodes, s, side="right") - 1, 0, len(nodes) - 2)
    ts = (s - nodes[sc0]) / (nodes[sc0 + 1] - nodes[sc0])
    # aspect: circular linear between neighbouring sectors
    na = config.MATRIX_ASPECTS
    fa = aspect / (360.0 / na)
    ak0 = np.floor(fa).astype(np.int64) % na
    ak1 = (ak0 + 1) % na
    ta = fa - np.floor(fa)

    R, C, V = [], [], []
    ridx = np.arange(len(rows))
    for j in range(k_nb):
        wi = widx[:, j]
        # elevation: fractional band position within THIS weather point
        fe = np.clip((elev - b0[wi]) / config.MATRIX_ELEV_STEP, 0.0, nb[wi] - 1.0)
        e0 = np.floor(fe).astype(np.int64)
        e1 = np.minimum(e0 + 1, nb[wi] - 1)
        te = fe - e0
        for ei, we in ((e0, 1.0 - te), (e1, te)):
            for sci, ws in ((sc0, 1.0 - ts), (sc0 + 1, ts)):
                for aki, wa in ((ak0, 1.0 - ta), (ak1, ta)):
                    run = lut[wi, ei, sci, aki]
                    w = wh[:, j] * we * ws * wa
                    ok = (w > 1e-9) & (run >= 0)
                    R.append(ridx[ok]); C.append(run[ok]); V.append(w[ok].astype(np.float32))
    W = sparse.coo_matrix((np.concatenate(V), (np.concatenate(R), np.concatenate(C))),
                          shape=(len(rows), len(runs))).tocsr()
    # Renormalise: a missing corner (a band a neighbour lacks) must not dilute
    # the sum toward zero.
    rs = np.asarray(W.sum(axis=1)).ravel()
    rs[rs == 0] = 1.0
    W = sparse.diags(1.0 / rs).dot(W).tocsr().astype(np.float32)
    return W


def output_times(win, step_h):
    """The instants SNOWPACK writes a profile at, window start onwards."""
    t, out = win[0], []
    while t <= win[1]:
        out.append(t)
        t += timedelta(hours=step_h)
    return out


# ── running the matrix: SNOWPACK then an immediate digest ──────────────────
# 14k runs at ~1 MB of .pro each is 14 GB -- more than a CI runner's disk.
# So every run is digested in its worker the moment it finishes: the metrics
# the ski classes need at each layer frame, and a compact profile at each
# profile step, and then its .pro and run directory are deleted.

def _digest(pro_path, layer_ts, prof_ts):
    from . import classify, profiles
    from .gridding import METS
    prs = [t for t in classify.parse_pro(pro_path) if t.get("n")]
    if not prs:
        return None
    by_dt = {t["dt"]: t for t in prs}
    keys = list(by_dt)

    def near(dt):
        t = by_dt.get(dt)
        return t if t is not None else by_dt[min(keys, key=lambda x: abs((x - dt).total_seconds()))]

    met = np.zeros((len(layer_ts), len(METS)), np.float32)
    for i, dt in enumerate(layer_ts):
        q = classify.assess_ski_quality(near(dt))
        met[i] = [q.get(k, 0.0) for k in METS]
    nb = profiles.NB
    hs = np.zeros(len(prof_ts), np.int16)
    db = np.zeros((len(prof_ts), nb), np.int16)
    gb = np.zeros((len(prof_ts), nb), np.int8)
    for i, dt in enumerate(prof_ts):
        r = profiles.resample(near(dt), top_cm=config.PROFILE_TOP_CM)
        if r is not None:
            db[i] = np.asarray(r[0], np.float64).round().astype(np.int16)
            gb[i] = np.asarray(r[1], np.int64).astype(np.int8)
            hs[i] = int(round(r[2]))
    return met, hs, db, gb


def _run_and_digest(args):
    ini, end, layer_ts, prof_ts = args
    from . import snowpack_runner
    name, rc, err = snowpack_runner._run_one((ini, end))
    pid = os.path.splitext(os.path.basename(ini))[0]
    run_dir = os.path.join(os.path.dirname(os.path.dirname(ini)), "runs", pid)
    out = None
    if rc == 0:
        pro = glob.glob(os.path.join(run_dir, "*.pro"))
        try:
            out = _digest(pro[0], layer_ts, prof_ts) if pro else None
        except Exception as e:                    # a corrupt .pro fails one run, not all
            rc, err = -2, f"digest failed: {type(e).__name__}: {e}"
    shutil.rmtree(run_dir, ignore_errors=True)
    return pid, rc, err, out


def run_matrix(runs, target_date, win, step_h, prof_step_h, workers=None, spinup_days=None,
               sno_src: Path | None = None):
    """Write .sno/.ini for every run, execute, digest. Returns {run_id: digest}.

    `sno_src`: a directory of <run_id>.sno valid at the window start (live
    mode, see state.py). Those runs start there and write profiles from the
    first step; runs without one fall back to the synthetic base + spin-up.
    """
    from . import snowpack_runner
    spinup_days = spinup_days or config.SPINUP_DAYS
    base = config.WORK_DIR
    sno_dir, ini_dir, runs_dir, meteo_dir = (base / "sno", base / "ini",
                                             base / "runs", base / "meteo")
    for d in (sno_dir, ini_dir, runs_dir):
        d.mkdir(parents=True, exist_ok=True)
    sim_start = (win[0] - timedelta(days=spinup_days)).date().isoformat()
    n_state = 0
    for r in runs:
        src = (Path(sno_src) / f"{r['id']}.sno") if sno_src else None
        if src is not None and src.exists():
            shutil.copy2(src, sno_dir / f"{r['id']}.sno")
            prof_start = 0.0
            n_state += 1
        else:
            snowpack_runner.write_sno(r, sno_dir, sim_start)
            prof_start = float(spinup_days)
        snowpack_runner.write_ini(r, ini_dir, sno_dir, meteo_dir, runs_dir,
                                  prof_start, step_h)
    if sno_src:
        print(f"  {n_state}/{len(runs)} runs continue from the carried state, "
              f"{len(runs) - n_state} cold-start")
    layer_ts = output_times(win, step_h)
    prof_ts = output_times(win, max(step_h, prof_step_h))
    end = win[1].strftime("%Y-%m-%dT%H:%M")
    jobs = [(str(ini_dir / f"{r['id']}.ini"), end, layer_ts, prof_ts) for r in runs]
    workers = workers or (os.cpu_count() or 4)
    results, fail = {}, []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_run_and_digest, j) for j in jobs]
        for k, fu in enumerate(as_completed(futs), 1):
            pid, rc, err, out = fu.result()
            if rc == 0 and out is not None:
                results[pid] = out
            else:
                fail.append((pid, err))
            if k % 250 == 0 or k == len(jobs):
                el = time.time() - t0
                print(f"  snowpack {k}/{len(jobs)} ({len(results)} ok)  "
                      f"{el:.0f}s elapsed, ~{el / k * (len(jobs) - k):.0f}s left", flush=True)
    print(f"SNOWPACK {len(results)}/{len(jobs)} runs in {time.time() - t0:.0f}s")
    for pid, e in fail[:5]:
        print("  FAIL", pid, (e or "").strip()[:160])
    return results, layer_ts, prof_ts


def advance_state(runs, carried, st_sno, st_time, to_time, out_dir: Path,
                  workers=None, spinup_days=None, bare=False):
    """Integrate every run to `to_time` and write its snowpack there.

    `carried` are the run ids that continue from `st_sno` (valid at
    `st_time`); every other run cold-starts `spinup_days` before `to_time`
    from the synthetic base (or from bare ground, `bare`). Nothing is written
    but the final .sno: out_dir/<run_id>.sno. Returns the ids that made it.
    """
    from . import snowpack_runner
    spinup_days = spinup_days or config.SPINUP_DAYS
    base = config.WORK_DIR / "advance"
    sno_dir, ini_dir, runs_dir = base / "sno", base / "ini", base / "runs"
    raw = base / "out"
    for d in (sno_dir, ini_dir, runs_dir, raw, Path(out_dir)):
        d.mkdir(parents=True, exist_ok=True)
    meteo_dir = config.WORK_DIR / "meteo"
    cold_start = (to_time - timedelta(days=spinup_days)).date().isoformat()
    jobs = []
    n_cold = 0
    for r in runs:
        if r["id"] in carried and st_sno is not None and st_time < to_time:
            shutil.copy2(Path(st_sno) / f"{r['id']}.sno", sno_dir / f"{r['id']}.sno")
        elif r["id"] in carried:
            # state already at to_time: nothing to integrate
            shutil.copy2(Path(st_sno) / f"{r['id']}.sno", Path(out_dir) / f"{r['id']}.sno")
            continue
        else:
            snowpack_runner.write_sno(r, sno_dir, cold_start, bare=bare)
            n_cold += 1
        snowpack_runner.write_ini(r, ini_dir, sno_dir, meteo_dir, runs_dir, 0.0, 24,
                                  snow_out=raw, prof_write=False)
        jobs.append((str(ini_dir / f"{r['id']}.ini"), to_time.strftime("%Y-%m-%dT%H:%M"),
                     str(raw / f"{r['id']}_va.sno")))
    print(f"  state advance to {to_time:%Y-%m-%d %H:%M}: {len(jobs) - n_cold} carried, "
          f"{n_cold} cold-start{' (bare ground)' if bare and n_cold else ''}")
    ok, fail = set(), []
    t0 = time.time()
    workers = workers or (os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(snowpack_runner._run_one, j): j for j in jobs}
        for fu in as_completed(futs):
            name, rc, err = fu.result()
            rid = os.path.splitext(name)[0]
            if rc == 0:
                shutil.move(str(raw / f"{rid}_va.sno"), str(Path(out_dir) / f"{rid}.sno"))
                ok.add(rid)
            else:
                fail.append((rid, err))
            shutil.rmtree(runs_dir / rid, ignore_errors=True)
    ok |= {p.stem for p in Path(out_dir).glob("*.sno")}
    print(f"  state advance: {len(ok)}/{len(runs)} ok in {time.time() - t0:.0f}s")
    for rid, e in fail[:3]:
        print("  ADVANCE FAIL", rid, (e or "").strip()[:160])
    return ok


# ── gridding: one sparse product per frame ─────────────────────────────────

def grid_frames(grid: NationalGrid, runs, results, layer_ts, W, cell_index, precip_ratio=None):
    """Yield (dt, {ski18, simple, density, drift, scour}) per layer frame.

    A generator, so frames are written as they are made -- 88 frames of three
    full national grids held at once would be over a gigabyte.
    """
    from . import classify
    from .gridding import METS
    nm = len(METS)
    ix = {k: i for i, k in enumerate(METS)}
    # Runs that failed get zero weight: drop their columns and renormalise.
    ok = np.array([r["id"] in results for r in runs])
    if not ok.all():
        W = W.tocsc()
        W = W[:, np.nonzero(ok)[0]].tocsr()
        rs = np.asarray(W.sum(axis=1)).ravel(); rs[rs == 0] = 1.0
        W = sparse.diags(1.0 / rs).dot(W).tocsr()
    kept = [r for r, o in zip(runs, ok) if o]
    stack = np.stack([results[r["id"]][0] for r in kept])      # (runs, frames, METS)
    # Presence-gated quantities: a crust, a buried weak layer or surface hoar
    # exists on a slope or it does not, so each is kept only where at least
    # CRUST_GATE of the cell's weight comes from runs that actually have it --
    # otherwise one crusted slope smears a trace of crust over every cell.
    gated = ("crust_thick_cm", "weak_below_cm", "weak_layer_depth_cm", "sh_surface")
    valid = np.zeros(grid.nr * grid.nc, bool); valid[cell_index] = True
    valid = valid.reshape(grid.nr, grid.nc)
    for f, dt in enumerate(layer_ts):
        T = stack[:, f, :]
        extra = np.column_stack([(T[:, ix[k]] > 0) for k in gated]).astype(np.float32)
        G = W.dot(np.column_stack([T, extra]))                  # (cells, METS + gates)
        full = np.zeros((nm + len(gated), grid.nr * grid.nc), np.float32)
        full[:, cell_index] = G.T
        g = full.reshape(nm + len(gated), grid.nr, grid.nc)
        m = {k: g[i] for k, i in ix.items()}
        for j, k in enumerate(gated):
            m[k] = np.where(g[nm + j] >= classify.CRUST_GATE, m[k], 0.0)
        if precip_ratio is not None:
            from . import precip
            m = precip.apply(m, precip_ratio, valid)
        ski18 = classify.classify_grid_metrics(
            m["total_hs_cm"], m["powder_depth_cm"], m["crust_thick_cm"], m["powder_dd"],
            m["powder_lw"], m["surface_density"], m["surface_hardness"], m["surface_lw"],
            m["weak_below_cm"], sh=m["sh_surface"], drift=m["drift_load"],
            scour=m["wind_scour"])
        simple = classify.classify_simple(
            m["total_hs_cm"], m["powder_depth_cm"], m["crust_thick_cm"],
            m["surface_density"], m["surface_lw"], sh=m["sh_surface"],
            drift=m["drift_load"], scour=m["wind_scour"])
        ski6 = classify.classify_ski6(m["total_hs_cm"], m["powder_depth_cm"], m["crust_thick_cm"],
                                      m["surface_density"], m["surface_lw"])
        windc = classify.classify_wind(m["total_hs_cm"], m["drift_load"], m["wind_scour"])
        powderc = classify.classify_powder(m["total_hs_cm"], m["powder_depth_cm"], m["surface_lw"])
        powderc[~valid] = 0
        ski18[~valid] = 0
        simple[~valid] = 0
        ski6[~valid] = 0
        windc[~valid] = 0
        density = np.where(valid, m["surface_density"], np.nan)
        hsv = m["total_hs_cm"][valid]
        yield dt, {"ski18": ski18, "simple": simple, "ski6": ski6, "wind": windc, "powder": powderc,
                   "density": density,
                   "hs_max": float(hsv.max()) if hsv.size else 0.0,
                   "finite": bool(np.isfinite(G).all())}
