"""Generic representative-point selector (ported from sandbox 70_select_terrain_points.py).

For each subregion and each target (elevation band x aspect x slope class) pick the
best real DEM cell: representativeness-scored (broad matching neighbourhood) + closeness
to the target, with progressive aspect tolerance. Points carry real elev/aspect/slope
and location, so downstream SNOWPACK runs get real terrain (slope + horizon shading).
"""
from __future__ import annotations
import csv, hashlib, json
from pathlib import Path
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import config
from .subregions import NationalGrid, load_national_grid, cell_to_lv03, lv03_to_wgs84, lv03_to_lv95, tile_ids

_ELEV_TOL = 200.0
_ASP_TOLS = (12, 20, 30, 45, 60)
_WIN_BIG = 5
_MIN_CANDS = 3
_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _compass(deg):
    return _DIRS[int(((deg + 22.5) % 360) // 45)]


def _asp_dist(aspect, target):
    raw = np.abs(aspect - target) % 360
    return np.where(np.isnan(aspect), 999.0, np.minimum(raw, 360 - raw))


def _win_count(mask, hw):
    w = 2 * hw + 1
    p = np.pad(mask.astype(np.float32), hw, mode="constant")
    return sliding_window_view(p, (w, w)).sum(axis=(-1, -2))


def select_for_mask(grid: NationalGrid, mask, tile_id):
    """Select points within a boolean subregion mask. Returns list of dicts."""
    dem, slope, aspect = grid.elevation, grid.slope, grid.aspect
    aspects = [i * 360.0 / config.N_ASPECTS for i in range(config.N_ASPECTS)]
    used = np.zeros(dem.shape, bool)
    pts = []
    for be in config.ELEV_BANDS:
        elev_ok = mask & (np.abs(dem - be) <= _ELEV_TOL) & ~np.isnan(dem)
        for slabel, slo, shi in config.SLOPE_CLASSES:
            slope_ok = elev_ok & (slope >= slo) & (slope < shi)
            for ad in aspects:
                chosen = None
                for tol in _ASP_TOLS:
                    cand = slope_ok & (_asp_dist(aspect, ad) <= tol) & ~used
                    if cand.sum() < _MIN_CANDS and tol != _ASP_TOLS[-1]:
                        continue
                    if cand.sum() == 0:
                        continue
                    big = _win_count(cand, _WIN_BIG).astype(float)
                    de = np.abs(dem - be) / _ELEV_TOL
                    da = _asp_dist(aspect, ad) / 180.0
                    dsl = np.abs(slope - (slo + shi) / 2) / max(shi - slo, 1)
                    score = big - 2.0 * (de + da + dsl)
                    score[~cand] = -1e9
                    r, c = np.unravel_index(int(np.argmax(score)), score.shape)
                    chosen = (r, c); break
                if chosen is None:
                    continue
                r, c = chosen
                used[max(0, r - 2):r + 3, max(0, c - 2):c + 3] = True
                e, n = cell_to_lv03(grid, r, c)
                lat, lon = lv03_to_wgs84(e, n)
                e95, n95 = lv03_to_lv95(e, n)
                pts.append({
                    "id": f"t{tile_id}_{be}_{_compass(aspect[r, c])}_{slabel}_{len(pts)}",
                    "tile": tile_id, "row": r, "col": c,
                    "elev": round(float(dem[r, c]), 1),
                    "aspect": round(float(aspect[r, c]), 1),
                    "slope": round(float(slope[r, c]), 1),
                    "lat": round(lat, 6), "lon": round(lon, 6),
                    "e_lv95": round(e95, 1), "n_lv95": round(n95, 1),
                    "band": be, "asp_c": _compass(aspect[r, c]), "slope_cls": slabel,
                })
    return pts


_NUM = {"tile": int, "row": int, "col": int, "elev": float, "aspect": float,
        "slope": float, "lat": float, "lon": float, "e_lv95": float, "n_lv95": float}


def _cache_key(only_tile):
    """Fingerprint every input the selection depends on.

    The DEM is hashed by content rather than mtime: CI restores it from a cache,
    which does not preserve timestamps, so an mtime key would miss every time.
    """
    h = hashlib.sha256()
    dem = Path(config.NATIONAL_DEM)
    if dem.exists():
        h.update(dem.read_bytes())
    lab = config.SUBREGION_DIR / "tile_labels.npy"
    if lab.exists():
        h.update(lab.read_bytes())
    h.update(json.dumps([config.ELEV_BANDS, config.N_ASPECTS, config.SLOPE_CLASSES,
                         _ELEV_TOL, list(_ASP_TOLS), _WIN_BIG, _MIN_CANDS,
                         only_tile], sort_keys=True).encode())
    return h.hexdigest()[:16]


def read_csv(path):
    """Inverse of write_csv, with the numeric columns restored."""
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append({k: (_NUM[k](v) if k in _NUM else v) for k, v in r.items()})
    return out


def select_national(grid: NationalGrid | None = None, only_tile: int | None = None,
                    cache: bool = True):
    """Select representative points for every subregion (or a single one).

    Cached on disk because this is the dominant cost of a small run: it scans
    the whole 920x1440 national grid and takes ~7 min, regardless of how many
    points are actually modelled afterwards. The result is a pure function of
    the DEM, the subregion labels and the target grid, so a content hash of
    those is a safe key.
    """
    if grid is None:
        grid = load_national_grid()
    cpath = None
    if cache:
        cpath = Path(config.CACHE_DIR) / "points" / f"{_cache_key(only_tile)}.csv"
        if cpath.exists():
            try:
                pts = read_csv(cpath)
                if pts:
                    print(f"  [select] reusing {len(pts)} cached points ({cpath.name})")
                    return grid, pts
            except Exception as e:
                print(f"  [select] cache unreadable ({e}) — reselecting")
    out = []
    for t in tile_ids(grid):
        if only_tile is not None and t != only_tile:
            continue
        out.extend(select_for_mask(grid, grid.tile == t, t))
    if cpath is not None and out:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            write_csv(out, cpath)
        except Exception as e:
            print(f"  [select] could not cache selection: {e}")
    return grid, out


def write_csv(points, path):
    cols = ["id", "tile", "row", "col", "elev", "aspect", "slope",
            "lat", "lon", "e_lv95", "n_lv95", "band", "asp_c", "slope_cls"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for p in points:
            w.writerow({k: p[k] for k in cols})
    return path
