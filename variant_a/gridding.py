"""Subregion-local KNN interpolation of representative-point ski metrics to the
national raster (ported from sandbox 08_ski_quality_national.py).

Each cell is classified from the K nearest points in (x, y, elevation, aspect) space
— horizontal distance dominates, so a cell is driven by points from its own subregion
at its own elevation/aspect. Produces the 18-category ski grid, the simplified skier
grid, and the mean-density-top-30cm field per timestep.
"""
from __future__ import annotations
import glob, os
import numpy as np
from scipy.spatial import cKDTree

from . import config, classify
from .subregions import NationalGrid

METS = ["powder_depth_cm", "crust_thick_cm", "powder_dd", "powder_lw",
        "surface_density", "surface_hardness", "surface_lw", "total_hs_cm", "weak_below_cm"]


def _point_en(grid: NationalGrid, p):
    e = grid.xll + (p["col"] + 0.5) * grid.cs
    n = grid.yll + (grid.nr - 1 - p["row"] + 0.5) * grid.cs
    return e, n


def _find_pro(runs_dir, pid):
    g = glob.glob(os.path.join(runs_dir, pid, "*.pro"))
    return g[0] if g else None


def classify_points(points, runs_dir):
    """Parse each point's .pro -> {id: {dt: metrics dict}} via assess_ski_quality."""
    series = {}
    for p in points:
        pro = p.get("pro") or _find_pro(runs_dir, p["id"])
        if not pro or not os.path.exists(pro):
            continue
        ts = classify.parse_pro(pro)
        series[p["id"]] = {t["dt"]: classify.assess_ski_quality(t) for t in ts}
    return series


def _knn(grid: NationalGrid, points):
    feats = []
    for p in points:
        e, n = _point_en(grid, p); az = np.radians(p["aspect"])
        feats.append([e / config.KNN_DX, n / config.KNN_DX, p["elev"] / config.KNN_DE,
                      np.cos(az) * config.KNN_WASP, np.sin(az) * config.KNN_WASP])
    tree = cKDTree(np.array(feats, np.float32))
    cols = np.arange(grid.nc); rows = np.arange(grid.nr)
    E = grid.xll + (cols + 0.5) * grid.cs
    N = grid.yll + (grid.nr - 1 - rows + 0.5) * grid.cs
    EE, NN = np.meshgrid(E, N)
    aq = np.radians(np.where(np.isnan(grid.aspect), 0.0, grid.aspect))
    gf = np.stack([EE.ravel() / config.KNN_DX, NN.ravel() / config.KNN_DX,
                   np.nan_to_num(grid.elevation, nan=0.0).ravel() / config.KNN_DE,
                   np.cos(aq).ravel() * config.KNN_WASP, np.sin(aq).ravel() * config.KNN_WASP], 1).astype(np.float32)
    dist, idx = tree.query(gf, k=min(config.KNN_K, len(points)))
    if idx.ndim == 1:
        idx = idx[:, None]; dist = dist[:, None]
    w = 1.0 / (dist + 0.05) ** 2
    w /= w.sum(1, keepdims=True)
    return idx, w.astype(np.float32)


def grid_timeseries(grid: NationalGrid, points, series, timestamps):
    """Return per-timestamp dict {ski18, simple, density} of (nr,nc) arrays + idx/w."""
    pts = [p for p in points if p["id"] in series and series[p["id"]]]
    if not pts:
        raise RuntimeError("no classified points")
    idx, w = _knn(grid, pts)
    nr, nc = grid.nr, grid.nc
    dem = grid.elevation
    out = {}
    for dt in timestamps:
        M = np.zeros((len(pts), len(METS)), np.float32)
        for pi, p in enumerate(pts):
            av = series[p["id"]]
            q = av.get(dt) or av[min(av, key=lambda x: abs((x - dt).total_seconds()))]
            M[pi] = [q[k] for k in METS]
        gm = (M[idx] * w[..., None]).sum(1)              # (ncell, nmet)
        g = gm.T.reshape(len(METS), nr, nc)
        powd, crust, pdd, plw, sdens, shard, slw, hs, weak = g
        crusted = (M[:, 1] > 0).astype(np.float32)
        cwt = (crusted[idx] * w).sum(1).reshape(nr, nc)
        crust = np.where(cwt >= classify.CRUST_GATE, crust, 0.0)
        wkp = (M[:, 8] > 0).astype(np.float32)
        wwt = (wkp[idx] * w).sum(1).reshape(nr, nc)
        weak = np.where(wwt >= classify.CRUST_GATE, weak, 0.0)
        ski18 = classify.classify_grid_metrics(hs, powd, crust, pdd, plw, sdens, shard, slw, weak)
        simple = classify.classify_simple(hs, powd, crust, sdens, slw)
        # mean density top 30 cm ~ interpolated surface_density (RG proxy) field
        density = np.where(np.isnan(dem), np.nan, sdens)
        ski18[np.isnan(dem)] = 0; simple[np.isnan(dem)] = 0
        out[dt] = {"ski18": ski18, "simple": simple, "density": density}
    return out, idx, w, pts
