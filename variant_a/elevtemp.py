"""Temperature at the height of each virtual slope, from the model itself.

The matrix runs every weather point's slopes at heights 1200-3300 m. The
forcing comes from one place (the weather point) and was moved to each band
with a lapse rate. ICON-CH1 (1 km) has cells at all those heights around a
weather point -- valley floors, flanks, ridges -- each with its own 2 m
temperature, which carries the inversions, warm layers and cold pools the
model knows about. So for every weather point and band we take the cells
within RADIUS_KM whose model terrain lies within MAX_DZ of the band, average
the (up to) N_CELLS closest in height, and use that series for the hours it
covers. The few metres left between cell and band are bridged with the
model's own profile (forcing._lapsed).

Source: tools/ogd_extract.py writes t2m_ch1.npz (all cells over Switzerland,
hourly, latest run; HSURF when OGD offers it, else the heights come from our
DEM smoothed to the model's ~1 km). OGD has no archive, so this covers the
forecast hours of a live cycle; earlier hours use the profile lapse rate.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

FIELD_FILE = "t2m_ch1.npz"
RADIUS_KM = 10.0
MAX_DZ = 150.0
N_CELLS = 3
ISO = "%Y-%m-%dT%H:%M"


def load_field(ogd_dir):
    if not ogd_dir:
        return None
    f = Path(ogd_dir) / FIELD_FILE
    if not f.exists():
        return None
    try:
        z = np.load(f, allow_pickle=False)
        fld = {"lat": z["lat"].astype(np.float64), "lon": z["lon"].astype(np.float64),
               "lead": z["lead"].astype(np.float64), "t": z["t"].astype(np.float32),
               "ref": datetime.strptime(str(z["ref"]), ISO),
               "hsurf": z["hsurf"].astype(np.float64) if "hsurf" in z.files else None}
    except Exception as e:
        print(f"  elevtemp: field unreadable ({type(e).__name__}: {e})")
        return None
    if fld["t"].shape != (len(fld["lead"]), len(fld["lat"])):
        print("  elevtemp: field has an unexpected shape -- ignored")
        return None
    return fld


def dem_heights(grid, ce, cn, box=4):
    """Model-like terrain height at (ce, cn): our DEM averaged over ~1 km."""
    from .precip import _box
    el = grid.elevation.astype(np.float64)
    ok = np.isfinite(el)
    sm = _box(np.where(ok, el, 0.0), box) / np.maximum(_box(ok.astype(np.float64), box), 1e-9)
    c = np.floor((ce - grid.xll) / grid.cs).astype(int)
    r = np.floor((grid.yll + grid.nr * grid.cs - cn) / grid.cs).astype(int)
    inside = (c >= 0) & (c < grid.nc) & (r >= 0) & (r < grid.nr)
    out = np.full(ce.shape, np.nan)
    out[inside] = sm[r[inside], c[inside]]
    out[inside & ~ok[np.clip(r, 0, grid.nr - 1), np.clip(c, 0, grid.nc - 1)]] = np.nan
    return out


def band_temps(fld, wps, ce, cn, heights, radius_km=RADIUS_KM, max_dz=MAX_DZ, n_cells=N_CELLS):
    """{wp_id: {band: {time: (T degC, band - cell height)}}}, and counts."""
    from scipy.spatial import cKDTree
    ok = np.isfinite(heights)
    idx_all = np.nonzero(ok)[0]
    if not len(idx_all):
        return {}, {"bands": 0, "covered": 0}
    tree = cKDTree(np.column_stack([ce[idx_all], cn[idx_all]]))
    times = [(fld["ref"] + timedelta(hours=float(h))).strftime(ISO) for h in fld["lead"]]
    out, n_b, n_cov = {}, 0, 0
    for w in wps:
        near = idx_all[tree.query_ball_point([w["e"], w["n"]], radius_km * 1000.0)]
        per = {}
        for band in w.get("bands", []):
            n_b += 1
            if not len(near):
                continue
            dz = heights[near] - float(band)
            sel = near[np.abs(dz) <= max_dz]
            if not len(sel):
                continue
            sel = sel[np.argsort(np.abs(heights[sel] - float(band)))][:n_cells]
            t = np.nanmean(fld["t"][:, sel], axis=1)
            dzc = float(band) - float(np.mean(heights[sel]))
            per[int(band)] = {times[k]: (round(float(t[k]), 2), round(dzc, 1))
                              for k in range(len(times)) if np.isfinite(t[k])}
            n_cov += 1
        if per:
            out[w["id"]] = per
    return out, {"bands": n_b, "covered": n_cov}


def build(grid, wps, ogd_dir):
    """(band_temps or None, summary). Never raises."""
    try:
        fld = load_field(ogd_dir)
        if fld is None:
            return None, {"used": False, "why": "no ICON-CH1 temperature field"}
        from .precip import _to_lv03
        ce, cn = _to_lv03(fld["lat"], fld["lon"])
        h = fld["hsurf"] if fld["hsurf"] is not None else dem_heights(grid, ce, cn)
        bt, cnt = band_temps(fld, wps, ce, cn, h)
        summ = {"used": True, "ref": fld["ref"].strftime(ISO),
                "heights": "HSURF" if fld["hsurf"] is not None else "DEM ~1 km",
                "bands_covered": f"{cnt['covered']}/{cnt['bands']}"}
        print(f"  temperature at band height: {summ}")
        return bt, summ
    except Exception as e:
        print(f"  temperature at band height unavailable: {type(e).__name__}: {e}")
        return None, {"used": False, "why": f"{type(e).__name__}: {e}"}
