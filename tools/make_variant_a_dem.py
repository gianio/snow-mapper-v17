#!/usr/bin/env python3
"""Build variant_a's national DEM from Copernicus, so CI does not need the
git-ignored original.

`variant_a/subregions.load_national_grid()` reads an ESRI ASCII grid at
`config.NATIONAL_DEM` (`variant_a/data/dem/ch_lv03_250m.dem`). That file is
git-ignored -- it is large, and the pipeline was written to run on a machine
that already had it. Without it nothing downstream can start, which is what
stopped the whole thing from running anywhere but one laptop.

`variant_a/config.py` already anticipates this: "The app's own LV95 Copernicus
DEM can be passed in instead via the runner." This does exactly that, and
pins the output to the SAME grid the committed subregion labels use --
`tile_meta.json` gives xll/yll/cs/nr/nc, and `tile_labels.npy` is indexed by
those cells, so any other alignment silently mismatches the subregions
(load_national_grid() would fall back to a single zero tile and the national
product would collapse to one subregion).

Output is LV03 / EPSG:21781, because that is what DEM_EPSG says and what
cell_to_lv03() assumes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NODATA = -9999.0


def build(out_path: Path, meta_path: Path, margin_m: float = 3000.0) -> Path:
    from pyproj import Transformer
    from data_connectors.copernicus_dem_loader import load_copernicus_dem

    meta = json.load(open(meta_path))
    xll, yll = float(meta["xll"]), float(meta["yll"])
    cs, nr, nc = float(meta["cs"]), int(meta["nr"]), int(meta["nc"])
    print(f"target grid {nr}x{nc} @ {cs:.0f} m, LV03 origin ({xll:.0f}, {yll:.0f})")

    # Cell CENTRES, matching cell_to_lv03(): north-up rows, row 0 at the top.
    cols = xll + (np.arange(nc) + 0.5) * cs
    rows = yll + (nr - 1 - np.arange(nr) + 0.5) * cs
    EX, NY = np.meshgrid(cols, rows)

    # LV03 -> LV95 is very nearly a constant 2 000 000 / 1 000 000 shift, but
    # go through pyproj rather than assume it.
    t_lv03_lv95 = Transformer.from_crs(21781, 2056, always_xy=True)
    e95, n95 = t_lv03_lv95.transform(EX.ravel(), NY.ravel())
    e95 = e95.reshape(EX.shape); n95 = n95.reshape(NY.shape)

    bounds = (float(np.nanmin(e95)) - margin_m, float(np.nanmin(n95)) - margin_m,
              float(np.nanmax(e95)) + margin_m, float(np.nanmax(n95)) + margin_m)
    t0 = time.time()
    dem = load_copernicus_dem(bounds, cs, "EPSG:2056")
    print(f"Copernicus DEM {dem.elevation.shape} in {time.time()-t0:.0f}s")

    # Nearest-neighbour sample onto the target cells. The source is already at
    # the same resolution, so interpolating would only blur it.
    e0, n0, e1, n1 = dem.bounds
    h, w = dem.elevation.shape
    col_i = np.floor((e95 - e0) / dem.res).astype(int)
    row_i = np.floor((n1 - n95) / dem.res).astype(int)
    ok = (col_i >= 0) & (col_i < w) & (row_i >= 0) & (row_i < h)
    z = np.full((nr, nc), NODATA, dtype="float64")
    z[ok] = dem.elevation[row_i[ok], col_i[ok]]
    z[~np.isfinite(z)] = NODATA

    inside = z != NODATA
    print(f"covered {100*inside.mean():.1f}% of cells, "
          f"elev {z[inside].min():.0f}-{z[inside].max():.0f} m")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        f.write(f"ncols {nc}\nnrows {nr}\nxllcorner {xll}\nyllcorner {yll}\n"
                f"cellsize {cs}\nNODATA_value {NODATA}\n")
        np.savetxt(f, z, fmt="%.1f")
    print(f"wrote {out_path} ({out_path.stat().st_size/1e6:.1f} MB)")
    return out_path


def main():
    from variant_a import config
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.NATIONAL_DEM))
    ap.add_argument("--meta", default=str(config.SUBREGION_DIR / "tile_meta.json"))
    a = ap.parse_args()
    build(Path(a.out), Path(a.meta))


if __name__ == "__main__":
    main()
