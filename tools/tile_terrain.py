#!/usr/bin/env python3
"""Prototype: tile the STATIC terrain field into a z/x/y pyramid.

What this is testing
--------------------
"Tiling A" from docs/slf-parity-and-tiering.md: the fine terrain field has no
time dimension, so it can be tiled once per deploy instead of shipped whole.
The claim to check is that this makes 50 m terrain deliverable per viewport
rather than as one big download -- and the point of building it is to replace
an estimate with a measurement.

Encoding matches _elev_to_png_b64 exactly, so the client's existing decode
applies unchanged:

    (R * 256 + G) * _ELEV_Q = elevation [m]
    B                       = slope [deg]
    A                       = 255 where data exists

Tiles are Web Mercator (EPSG:3857) z/x/y PNGs -- what L.tileLayer expects.

Usage
-----
    python tools/tile_terrain.py --zooms 9 11 --out /tmp/tiles
    python tools/tile_terrain.py --bbox 2770000 1175000 2794000 1199000
"""
from __future__ import annotations

import argparse
import io
import json
import math
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import SWITZERLAND_BBOX_LV95 as CH   # noqa: E402
from pipeline.interactive_export import _ELEV_Q            # noqa: E402

TILE = 256
_ORIGIN = 20037508.342789244          # Web Mercator half-extent [m]


def _merc_bounds(z, x, y):
    """Tile bounds in EPSG:3857 metres."""
    span = 2 * _ORIGIN / (2 ** z)
    return (-_ORIGIN + x * span, _ORIGIN - (y + 1) * span,
            -_ORIGIN + (x + 1) * span, _ORIGIN - y * span)


def _lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _encode(elev, slope):
    """Same RGBA packing as the single-file terrain raster."""
    e = np.clip(np.round(np.nan_to_num(elev) / _ELEV_Q), 0, 65535).astype("uint32")
    sl = np.clip(np.round(np.nan_to_num(slope)), 0, 90).astype("uint8")
    rgba = np.zeros((*e.shape, 4), dtype="uint8")
    rgba[:, :, 0] = ((e >> 8) & 0xFF).astype("uint8")
    rgba[:, :, 1] = (e & 0xFF).astype("uint8")
    rgba[:, :, 2] = sl
    rgba[:, :, 3] = np.where(e > 0, 255, 0).astype("uint8")
    return rgba


def tile_terrain(dem, slope_deg, out_dir: Path, zooms, bbox_lonlat, quiet=False):
    """Write a z/x/y PNG pyramid. Returns per-zoom stats."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src_t, src_crs = dem.transform, "EPSG:2056"
    lon0, lat0, lon1, lat1 = bbox_lonlat
    stats = []

    for z in zooms:
        x0, y1 = _lonlat_to_tile(lon0, lat0, z)
        x1, y0 = _lonlat_to_tile(lon1, lat1, z)
        t0 = time.time()
        n = written = total = empty = 0
        biggest = 0
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                n += 1
                west, south, east, north = _merc_bounds(z, x, y)
                dst_t = from_origin(west, north,
                                    (east - west) / TILE, (north - south) / TILE)
                e = np.zeros((TILE, TILE), "float32")
                s = np.zeros((TILE, TILE), "float32")
                for src, dst in ((dem.elevation.astype("float32"), e),
                                 (slope_deg.astype("float32"), s)):
                    reproject(source=src, destination=dst, src_transform=src_t,
                              src_crs=src_crs, dst_transform=dst_t,
                              dst_crs="EPSG:3857", resampling=Resampling.bilinear)
                # A tile entirely off the DEM is not written at all -- the
                # client treats a 404 as "no data", which is cheaper than
                # shipping thousands of transparent PNGs.
                if not np.any(e > 0):
                    empty += 1
                    continue
                buf = io.BytesIO()
                Image.fromarray(_encode(e, s), "RGBA").save(
                    buf, format="PNG", optimize=True)
                data = buf.getvalue()
                p = out_dir / str(z) / str(x)
                p.mkdir(parents=True, exist_ok=True)
                (p / f"{y}.png").write_bytes(data)
                written += 1
                total += len(data)
                biggest = max(biggest, len(data))
        dt = time.time() - t0
        st = {"z": z, "candidates": n, "written": written, "empty": empty,
              "bytes": total, "biggest": biggest, "seconds": round(dt, 1),
              "m_per_px": round(156543.03 * math.cos(math.radians(46.8)) / 2 ** z, 1)}
        stats.append(st)
        if not quiet:
            print(f"  z{z:<3} {st['m_per_px']:>7.1f} m/px  "
                  f"{written:>6} tiles ({empty} empty skipped)  "
                  f"{total/1e6:>8.2f} MB  max {biggest/1024:>6.1f} KB  {dt:>6.1f}s")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zooms", type=int, nargs="+", default=[9, 10, 11])
    ap.add_argument("--out", default="/tmp/terrain-tiles")
    ap.add_argument("--res", type=float, default=None,
                    help="DEM resolution [m]; default = finest zoom's ground res")
    ap.add_argument("--bbox", type=float, nargs=4, default=None,
                    metavar=("EMIN", "NMIN", "EMAX", "NMAX"),
                    help="LV95 bbox; default = all of Switzerland")
    ap.add_argument("--keep", action="store_true", help="keep the tiles on disk")
    args = ap.parse_args()

    b = args.bbox or [CH["east_min"], CH["north_min"], CH["east_max"], CH["north_max"]]
    bounds = (b[0], b[1], b[2], b[3])
    res = args.res or (156543.03 * math.cos(math.radians(46.8)) / 2 ** max(args.zooms))

    from pyproj import Transformer
    from data_connectors.copernicus_dem_loader import load_copernicus_dem
    from model.terrain_features import compute_terrain_features

    print(f"domain {(b[2]-b[0])/1000:.0f} x {(b[3]-b[1])/1000:.0f} km | "
          f"DEM at {res:.0f} m | zooms {args.zooms}")
    t0 = time.time()
    dem = load_copernicus_dem(bounds, res, "EPSG:2056")
    tf = compute_terrain_features(
        np.nan_to_num(dem.elevation, nan=float(np.nanmean(dem.elevation))), dem.res)
    slope = np.degrees(tf.slope_rad)
    print(f"DEM {dem.elevation.shape} loaded + slope in {time.time()-t0:.0f}s")

    tr = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
    lon0, lat0 = tr.transform(b[0], b[1])
    lon1, lat1 = tr.transform(b[2], b[3])

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    stats = tile_terrain(dem, slope, out, sorted(args.zooms), (lon0, lat0, lon1, lat1))

    tw = sum(s["written"] for s in stats)
    tb = sum(s["bytes"] for s in stats)
    ts = sum(s["seconds"] for s in stats)
    print(f"\nTOTAL {tw} tiles, {tb/1e6:.2f} MB, {ts:.0f}s")
    (out / "stats.json").write_text(json.dumps(stats, indent=2))
    if not args.keep:
        print(f"(tiles left in {out})")


if __name__ == "__main__":
    main()
