"""National subregion segmentation + DEM terrain for Variant A.

Loads the earlier-defined Switzerland subregions (tile_labels.npy + tile_meta.json,
the valley/climate-region tiles from the sandbox) aligned to the national 250 m DEM,
and provides per-cell subregion id, elevation, slope and aspect. Coordinate helpers
convert the LV03 grid to LV95 (EPSG:2056, the app CRS) and WGS84.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
import numpy as np
from pyproj import Transformer

from . import config


@dataclass
class NationalGrid:
    elevation: np.ndarray        # (nr,nc) m, row 0 = north (LV03 grid)
    slope: np.ndarray            # deg
    aspect: np.ndarray           # deg, 0=N,90=E,180=S,270=W
    tile: np.ndarray             # (nr,nc) int subregion/tile id (0 = outside)
    tile_names: dict             # id -> name
    xll: float; yll: float; cs: float; nr: int; nc: int


def _read_dem(path):
    h = {}
    with open(path) as f:
        for _ in range(6):
            k, v = f.readline().split(); h[k.lower()] = float(v)
    z = np.loadtxt(path, skiprows=6)
    z[z == h.get("nodata_value", -999)] = np.nan
    return z, h


def _terrain(dem, cs):
    """Slope + aspect (0=N,90=E,180=S,270=W) from the DEM."""
    dR, dC = np.gradient(dem, cs)
    slope = np.degrees(np.arctan(np.hypot(dR, dC)))
    aspect = (np.degrees(np.arctan2(-dC, dR))) % 360.0
    slope[np.isnan(dem)] = np.nan
    aspect[np.isnan(dem)] = np.nan
    return slope, aspect


def load_national_grid() -> NationalGrid:
    """Assemble the national DEM + terrain + subregion tiles into one object."""
    dem, h = _read_dem(str(config.NATIONAL_DEM))
    nr, nc = dem.shape
    cs = h["cellsize"]
    slope, aspect = _terrain(dem, cs)
    meta = json.load(open(config.SUBREGION_DIR / "tile_meta.json"))
    tiles = np.load(config.SUBREGION_DIR / "tile_labels.npy")
    if tiles.shape != dem.shape:                       # guard: align if off
        tiles = np.zeros_like(dem, dtype=int)
    names = {int(k): v for k, v in meta.get("tiles", {}).items()}
    return NationalGrid(elevation=dem, slope=slope, aspect=aspect,
                        tile=tiles.astype(int), tile_names=names,
                        xll=h["xllcorner"], yll=h["yllcorner"], cs=cs, nr=nr, nc=nc)


def cell_to_lv03(grid: NationalGrid, row: int, col: int):
    """Cell (row,col) centre -> LV03 easting/northing."""
    e = grid.xll + (col + 0.5) * grid.cs
    n = grid.yll + (grid.nr - 1 - row + 0.5) * grid.cs
    return e, n


_T_LV03_WGS84 = Transformer.from_crs(config.DEM_EPSG, 4326, always_xy=True)
_T_LV03_LV95 = Transformer.from_crs(config.DEM_EPSG, 2056, always_xy=True)


def lv03_to_wgs84(e, n):
    lon, lat = _T_LV03_WGS84.transform(e, n); return lat, lon


def lv03_to_lv95(e, n):
    return _T_LV03_LV95.transform(e, n)


def wgs84_bounds(grid: NationalGrid):
    """(lat0,lon0,lat1,lon1) covering the grid (approx, corner transform)."""
    lo0, la0 = _T_LV03_WGS84.transform(grid.xll, grid.yll)
    lo1, la1 = _T_LV03_WGS84.transform(grid.xll + grid.nc * grid.cs,
                                       grid.yll + grid.nr * grid.cs)
    return la0, lo0, la1, lo1


def tile_ids(grid: NationalGrid):
    # int(), not the np.int64 that np.unique hands back. The id is carried on
    # every representative point as p["tile"] and ends up in profiles.json,
    # and json.dump refuses a numpy scalar -- which killed the export at the
    # very last step, after the whole model run.
    return [int(t) for t in sorted(np.unique(grid.tile)) if t != 0]


_T_WGS84_LV03 = Transformer.from_crs(4326, config.DEM_EPSG, always_xy=True)


def wgs84_to_cell(grid: NationalGrid, lat, lon):
    """WGS84 -> (row, col) on the national grid, or None if outside."""
    e, n = _T_WGS84_LV03.transform(lon, lat)
    col = int((e - grid.xll) / grid.cs)
    row = int(grid.nr - 1 - (n - grid.yll) / grid.cs)
    if 0 <= row < grid.nr and 0 <= col < grid.nc:
        return row, col
    return None

