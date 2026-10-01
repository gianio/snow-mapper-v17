"""Precipitation pattern at 1 km: how much more (or less) fell HERE than at
the weather points the virtual slopes were forced with.

The matrix runs SNOWPACK at weather points ~15 km apart and blends the three
nearest. Precipitation, though, changes over a few km -- the windward side of
a ridge, a dry inner-alpine valley, a convective cell -- and that is exactly
what decides how much new snow a slope got. Running the matrix on a much
denser set of points would cost ~4x the CPU. Instead, ICON-CH1's own 1 km
precipitation field gives, per map cell, the ratio

    R = P(cell) / P(blend of the weather points the cell is built from)

and the cell's new snow (powder depth, and the share of HS it makes up) is
scaled by R. The blend uses the same IDW as the matrix weights, so where the
field is smooth R is ~1 and nothing changes.

Where the field comes from: tools/ogd_extract.py saves TOT_PREC of the latest
ICON-CH1 run (`precip_ch1.npz`, cells inside the Swiss box). OGD has no
archive, so the past part is built up cycle by cycle: each live cycle keeps
the first 6 h of its run in the carried state (`precip_hist.npz`), and the
next cycles sum those. The forecast part comes from the current run. Without
a field (demo, archive dates, OGD down) R is simply not used.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

FIELD_FILE = "precip_ch1.npz"
HIST_FILE = "precip_hist.npz"
HIST_DAYS = 5              # past snowfall that still shapes the top of the pack
CYCLE_H = 6                # live cycles run every 6 h
MIN_MM = 3.0               # less than this at the weather points: no ratio
R_MIN, R_MAX = 0.5, 2.0
SMOOTH_CELLS = 5           # box filter on the 250 m grid (~1.25 km)
ISO = "%Y-%m-%dT%H:%M"


def load_field(ogd_dir):
    """{'lat','lon','lead','acc','ref'} from ogd_dir/precip_ch1.npz, or None.

    acc: (n_lead, n_cells) precipitation accumulated since the run start, mm.
    """
    if not ogd_dir:
        return None
    f = Path(ogd_dir) / FIELD_FILE
    if not f.exists():
        return None
    try:
        z = np.load(f, allow_pickle=False)
        fld = {"lat": z["lat"].astype(np.float64), "lon": z["lon"].astype(np.float64),
               "lead": z["lead"].astype(np.float64), "acc": z["acc"].astype(np.float32),
               "ref": datetime.strptime(str(z["ref"]), ISO)}
    except Exception as e:
        print(f"  precip field unreadable ({type(e).__name__}: {e})")
        return None
    if fld["acc"].ndim != 2 or fld["acc"].shape != (len(fld["lead"]), len(fld["lat"])):
        print("  precip field has an unexpected shape -- ignored")
        return None
    return fld


def _acc_at(fld, t):
    """Accumulation (mm) since the run start at time t, linear between leads."""
    h = (t - fld["ref"]).total_seconds() / 3600.0
    lead, acc = fld["lead"], fld["acc"]
    if h <= lead[0]:
        return acc[0]
    if h >= lead[-1]:
        return acc[-1]
    i = int(np.searchsorted(lead, h)) - 1
    w = (h - lead[i]) / (lead[i + 1] - lead[i])
    return (1.0 - w) * acc[i] + w * acc[i + 1]


def forecast_sum(fld, t0, t1):
    """mm per cell between t0 and t1 from the current run (>= 0)."""
    return np.maximum(_acc_at(fld, t1) - _acc_at(fld, t0), 0.0).astype(np.float32)


def load_history(state_dir):
    """{'times': [datetime], 'inc': (k, n) float32, 'n': n} or None."""
    if not state_dir:
        return None
    f = Path(state_dir) / HIST_FILE
    if not f.exists():
        return None
    try:
        z = np.load(f, allow_pickle=False)
        return {"times": [datetime.strptime(str(t), ISO) for t in z["times"]],
                "inc": z["inc"].astype(np.float32), "n": int(z["inc"].shape[1])}
    except Exception:
        return None


def past_sum(hist, fld, since):
    """mm per cell from earlier cycles' first hours, between `since` and the
    current run's start. Zeros (and a note) when there is no usable history."""
    n = len(fld["lat"])
    if not hist or hist["n"] != n:
        return np.zeros(n, np.float32), 0
    keep = [i for i, t in enumerate(hist["times"]) if since <= t < fld["ref"]]
    if not keep:
        return np.zeros(n, np.float32), 0
    return hist["inc"][keep].sum(axis=0).astype(np.float32), len(keep)


def update_history(hist, fld):
    """Add this run's first CYCLE_H hours; drop what is older than HIST_DAYS."""
    n = len(fld["lat"])
    inc = forecast_sum(fld, fld["ref"], fld["ref"] + timedelta(hours=CYCLE_H))
    times, rows = [], []
    if hist and hist["n"] == n:
        for t, r in zip(hist["times"], hist["inc"]):
            if fld["ref"] - t <= timedelta(days=HIST_DAYS) and t != fld["ref"]:
                times.append(t); rows.append(r)
    times.append(fld["ref"]); rows.append(inc)
    return {"times": times, "inc": np.stack(rows).astype(np.float32), "n": n}


def save_history(state_dir, hist):
    if not state_dir or not hist:
        return
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    # float16 keeps a week of 6-hourly 1 km fields at a few MB in the state
    np.savez_compressed(Path(state_dir) / HIST_FILE,
                        times=np.array([t.strftime(ISO) for t in hist["times"]]),
                        inc=hist["inc"].astype(np.float16))


def _to_lv03(lat, lon):
    from pyproj import Transformer
    from . import config
    tr = Transformer.from_crs(4326, config.DEM_EPSG, always_xy=True)
    e, n = tr.transform(np.asarray(lon), np.asarray(lat))
    return np.asarray(e, np.float64), np.asarray(n, np.float64)


def _box(a, k):
    """k x k mean (edges use what is there)."""
    from scipy.ndimage import uniform_filter
    return uniform_filter(a, size=k, mode="nearest")


def ratio_grid(grid, wps, ce, cn, P, neighbours=None):
    """R on the national grid (nr, nc), 1.0 where it is undefined.

    ce, cn: LV03 coordinates of the precipitation cells; P: mm per cell.
    """
    from scipy.spatial import cKDTree
    from . import config
    R = np.ones((grid.nr, grid.nc), np.float32)
    ok = np.isfinite(P)
    if ok.sum() < 10 or not wps:
        return R
    tree = cKDTree(np.column_stack([ce[ok], cn[ok]]))
    Pk = P[ok].astype(np.float64)
    valid = (grid.tile > 0) & np.isfinite(grid.elevation)
    rows, cols = np.nonzero(valid)
    if rows.size == 0:
        return R
    ge = grid.xll + (cols + 0.5) * grid.cs
    gn = grid.yll + (grid.nr - 1 - rows + 0.5) * grid.cs
    # local precipitation: nearest 1 km cell, then a ~1 km box so the 1 km
    # mesh does not print through as squares
    loc = np.zeros((grid.nr, grid.nc), np.float64)
    loc[rows, cols] = Pk[tree.query(np.column_stack([ge, gn]))[1]]
    # normalised box: only cells inside the model count, so the edge of the
    # model area (and DEM holes) does not pull the mean towards zero
    msk = valid.astype(np.float64)
    loc = (_box(loc, SMOOTH_CELLS) / np.maximum(_box(msk, SMOOTH_CELLS), 1e-9))[rows, cols]
    # what the matrix has: the weather points' own precipitation, blended
    # exactly like matrix._weights does it (IDW over the nearest k)
    wxy = np.array([[w["e"], w["n"]] for w in wps])
    pw = Pk[tree.query(wxy)[1]]
    k = min(neighbours or config.MATRIX_NEIGHBOURS, len(wps))
    d, wi = cKDTree(wxy).query(np.column_stack([ge, gn]), k=k)
    if k == 1:
        d, wi = d[:, None], wi[:, None]
    d0 = 0.5 * config.WEATHER_SPACING_KM * 1000.0
    wt = 1.0 / (d + d0) ** 2
    blend = (wt * pw[wi]).sum(axis=1) / wt.sum(axis=1)
    r = np.where(blend >= MIN_MM, loc / np.maximum(blend, 1e-6), 1.0)
    R[rows, cols] = np.clip(r, R_MIN, R_MAX).astype(np.float32)
    return R


def build(grid, wps, ogd_dir, state_dir, win, live):
    """(R or None, new_history or None, summary dict). Never raises."""
    try:
        fld = load_field(ogd_dir)
        if fld is None:
            return None, None, {"used": False, "why": "no ICON-CH1 field"}
        ce, cn = _to_lv03(fld["lat"], fld["lon"])
        hist = load_history(state_dir) if live else None
        past, n_past = past_sum(hist, fld, max(win[0], fld["ref"] - timedelta(days=HIST_DAYS)))
        P = past + forecast_sum(fld, fld["ref"], win[1])
        R = ratio_grid(grid, wps, ce, cn, P)
        new_hist = update_history(hist, fld) if live else None
        v = R[(grid.tile > 0) & np.isfinite(grid.elevation)]
        summ = {"used": True, "ref": fld["ref"].strftime(ISO), "past_cycles": n_past,
                "p_max_mm": round(float(np.nanmax(P)), 1),
                "r_p10": round(float(np.percentile(v, 10)), 2) if v.size else 1.0,
                "r_p90": round(float(np.percentile(v, 90)), 2) if v.size else 1.0}
        print(f"  precip pattern: {summ}")
        return R, new_hist, summ
    except Exception as e:
        print(f"  precip pattern unavailable: {type(e).__name__}: {e}")
        return None, None, {"used": False, "why": f"{type(e).__name__}: {e}"}


def apply(m, R, valid):
    """Scale new snow in the gridded metrics `m` (dict of (nr, nc)) by R."""
    if R is None:
        return m
    pd = m["powder_depth_cm"]
    Rv = np.where(valid, R, 1.0)
    m["total_hs_cm"] = np.maximum(m["total_hs_cm"] + (Rv - 1.0) * pd, 0.0)
    m["powder_depth_cm"] = pd * Rv
    return m
