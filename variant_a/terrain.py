"""Terrain effects a 1-D virtual slope cannot see: horizon shading and forest.

Horizon shading
    A virtual slope at 2400 m S 38 deg gets the full sun of an open south
    face. A real south face in a deep valley, or under a higher ridge, loses
    hours of it -- exactly the hours that make melt-freeze crusts. So each
    grid cell gets a shade fraction s: the share of the day's clear-sky
    direct beam that the surrounding terrain blocks, for the date being
    modelled. The interpolation then blends the cell between its own aspect
    and the north-facing run of the same height and steepness by s, i.e. a
    fully shaded south slope behaves like a slope without direct sun.

Forest
    The model has no canopy. Below treeline a forested cell is not "powder"
    or "crust" the way the open slope next to it is, so forest is carried as
    a fraction per cell from ESA WorldCover 10 m (tree cover class 10), and
    the layers are dimmed there instead of being presented as open terrain.
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import config
from .subregions import NationalGrid

N_AZ = 16                 # horizon sectors
MAX_DIST_M = 15000.0      # how far a ridge can shade from


def horizon_tan(grid: NationalGrid, n_az=N_AZ, max_dist=MAX_DIST_M):
    """(n_az, nr, nc) tangent of the horizon elevation angle per azimuth sector.

    Ray-marched on the grid itself: for each sector, step outward one cell at
    a time and keep the steepest rise seen. Sector k looks towards azimuth
    k*360/n_az (0 = north, clockwise)."""
    z = np.nan_to_num(grid.elevation.astype(np.float32), nan=0.0)
    nr, nc = z.shape
    steps = int(max_dist // grid.cs)
    out = np.zeros((n_az, nr, nc), np.float32)
    for k in range(n_az):
        az = math.radians(k * 360.0 / n_az)
        best = np.zeros((nr, nc), np.float32)
        seen = set()
        for s in range(1, steps + 1):
            dc = int(round(s * math.sin(az)))       # east = +col
            dr = int(round(-s * math.cos(az)))      # north = -row
            if (dr, dc) in seen:
                continue
            seen.add((dr, dc))
            dist = math.hypot(dr, dc) * grid.cs
            shifted = np.full((nr, nc), -1e4, np.float32)
            r0, r1 = max(0, -dr), min(nr, nr - dr)
            c0, c1 = max(0, -dc), min(nc, nc - dc)
            if r0 >= r1 or c0 >= c1:
                break
            shifted[r0:r1, c0:c1] = z[r0 + dr:r1 + dr, c0 + dc:c1 + dc]
            np.maximum(best, (shifted - z) / dist, out=best)
        out[k] = best
    return out


def sun_path(date: datetime, lat=46.7, lon=8.2, step_min=15):
    """[(elevation_deg, azimuth_deg)] for the sun over one UTC day (NOAA)."""
    out = []
    t = datetime(date.year, date.month, date.day)
    doy = t.timetuple().tm_yday
    for m in range(0, 24 * 60, step_min):
        hr = m / 60.0
        g = 2 * math.pi / 365 * (doy - 1 + (hr - 12) / 24)
        decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
                - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
                - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
        eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
        tst = hr * 60 + eqt + 4 * lon
        ha = math.radians(tst / 4 - 180)
        la = math.radians(lat)
        cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
        zen = math.acos(max(-1.0, min(1.0, cz)))
        el = 90 - math.degrees(zen)
        if el <= 0:
            continue
        az = math.degrees(math.atan2(math.sin(ha),
                                     math.cos(ha) * math.sin(la) - math.tan(decl) * math.cos(la))) + 180
        out.append((el, az % 360))
    return out


def shade_fraction(grid: NationalGrid, date: datetime, htan=None):
    """(nr, nc) share 0..1 of the day's direct beam ON THE SLOPE blocked by
    surrounding terrain.

    Weighted by the beam's incidence on the cell's own slope, and counted only
    while the sun is in front of that slope. When the sun is behind the slope
    plane SNOWPACK already gives the virtual slope no direct beam -- counting
    that as "shade" too would darken every east and west face twice.
    """
    htan = horizon_tan(grid) if htan is None else htan
    n_az = htan.shape[0]
    sl = np.radians(np.nan_to_num(grid.slope, nan=0.0)).astype(np.float32)
    asp = np.radians(np.nan_to_num(grid.aspect, nan=0.0)).astype(np.float32)
    cs, ss = np.cos(sl), np.sin(sl)
    blocked = np.zeros(grid.elevation.shape, np.float32)
    total = np.zeros(grid.elevation.shape, np.float32)
    for el, az in sun_path(date):
        e = math.radians(el)
        inc = cs * math.sin(e) + ss * math.cos(e) * np.cos(math.radians(az) - asp)
        w = np.maximum(inc, 0.0)
        f = az / (360.0 / n_az)
        k0 = int(math.floor(f)) % n_az
        k1 = (k0 + 1) % n_az
        t = f - math.floor(f)
        h = htan[k0] * (1 - t) + htan[k1] * t
        blocked += w * (h > math.tan(e))
        total += w
    s = np.where(total > 1e-6, blocked / np.maximum(total, 1e-6), 0.0).astype(np.float32)
    s[np.isnan(grid.elevation)] = 0.0
    return s


def shade_for_window(grid: NationalGrid, win, cache_dir: Path | None = None):
    """Shade fraction for the window's middle day, cached per date."""
    mid = win[0] + (win[1] - win[0]) / 2
    cache_dir = cache_dir or (config.CACHE_DIR / "terrain")
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"shade_{mid:%Y-%m-%d}_{grid.nr}x{grid.nc}.npy"
    if f.exists():
        return np.load(f)
    hf = cache_dir / f"horizon_{grid.nr}x{grid.nc}_{N_AZ}.npy"
    if hf.exists():
        htan = np.load(hf)
    else:
        htan = horizon_tan(grid)
        np.save(hf, htan)
    s = shade_fraction(grid, mid, htan)
    np.save(f, s)
    return s


# ── forest ───────────────────────────────────────────────────────────────────

FOREST_FILE = config.DATA_DIR / "forest_250m.npz"
FOREST_PNG = config.DATA_DIR / "forest_100m.png"   # client copy: 100 m, 4 levels
WORLDCOVER = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
              "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
WC_TILES = ("N45E003", "N45E006", "N45E009")


def build_forest(grid: NationalGrid, res_m=None, oversample=5):
    """Forest fraction on the LV03 grid (or a finer one at `res_m`), 0..1.

    Reads WorldCover over HTTP (cloud-optimised GeoTIFF, via GDAL's
    /vsicurl), at an overview close to the target resolution, and averages
    the tree-cover class over each target cell."""
    import rasterio
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import from_origin
    res = res_m or grid.cs
    nr = int(round(grid.nr * grid.cs / res)); nc = int(round(grid.nc * grid.cs / res))
    top = grid.yll + grid.nr * grid.cs
    # oversample the tree mask, then average down: a proper area fraction
    fine = np.zeros((nr * oversample, nc * oversample), np.float32)
    ft = from_origin(grid.xll, top, res / oversample, res / oversample)
    for tile in WC_TILES:
        url = "/vsicurl/" + WORLDCOVER.format(tile=tile)
        with rasterio.open(url) as src:
            # pick the overview closest to the oversampled resolution
            ovs = src.overviews(1) or [1]
            want = (res / oversample) / 10.0
            fac = min(ovs, key=lambda o: abs(o - want))
            h, w = src.height // fac, src.width // fac
            data = src.read(1, out_shape=(h, w), resampling=Resampling.mode)
            t = src.transform * src.transform.scale(src.width / w, src.height / h)
            tree = (data == 10).astype(np.float32)
            dst = np.full(fine.shape, np.nan, np.float32)
            reproject(tree, dst, src_transform=t, src_crs=src.crs,
                      dst_transform=ft, dst_crs=f"EPSG:{config.DEM_EPSG}",
                      resampling=Resampling.nearest, src_nodata=None, dst_nodata=np.nan)
            ok = ~np.isnan(dst)
            fine[ok] = np.maximum(fine[ok], dst[ok])
    return fine.reshape(nr, oversample, nc, oversample).mean(axis=(1, 3))


def load_forest(grid: NationalGrid):
    """Forest fraction on the national grid; zeros when it was never built."""
    if FOREST_FILE.exists():
        f = np.load(FOREST_FILE)["forest"]
        if f.shape == grid.elevation.shape:
            return f.astype(np.float32) / 255.0
    return np.zeros(grid.elevation.shape, np.float32)


if __name__ == "__main__":
    # Rebuild the committed forest rasters (needs network for WorldCover):
    #   python -m variant_a.terrain
    from PIL import Image
    from .subregions import load_national_grid
    g = load_national_grid()
    f = build_forest(g)
    np.savez_compressed(FOREST_FILE, forest=(np.clip(f, 0, 1) * 255).round().astype(np.uint8))
    f100 = build_forest(g, res_m=100, oversample=4)
    rr = (np.arange(f100.shape[0]) * 100 / g.cs).astype(int).clip(0, g.nr - 1)
    cc = (np.arange(f100.shape[1]) * 100 / g.cs).astype(int).clip(0, g.nc - 1)
    q = (np.round(np.clip(f100, 0, 1) * 3) * 85).astype(np.uint8)
    q[~(g.tile[rr][:, cc] > 0)] = 0
    Image.fromarray(q, "L").save(FOREST_PNG, optimize=True)
    print(f"forest: {FOREST_FILE}, {FOREST_PNG}")
