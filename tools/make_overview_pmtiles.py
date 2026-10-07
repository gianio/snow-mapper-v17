#!/usr/bin/env python3
"""SNOWPACK overview tiles: one PMTiles archive per view and frame.

The app's zoomed-out SNOWPACK picture used to be one ~1600x700 PNG per frame
over the whole country. As web-map tiles (zoom 5-9) in a PMTiles archive the
app only loads the tiles in view, at the size it needs, from any static
storage that answers byte-range requests (here: R2 via the tile Worker).

    python tools/make_overview_pmtiles.py <export_dir> <out_dir> [--zmax 9]

writes <out_dir>/<view>/<tag>.pmtiles for every view in the manifest.
Sharp tiles (zoom 10-12) are NOT made here -- the Worker renders those on
demand (tiles/worker).
"""
from __future__ import annotations
import argparse, io, json, math
from pathlib import Path

import numpy as np
from PIL import Image

ZMIN = 5


def _tile_range(bounds, z):
    (s, w), (n, e) = bounds
    def xy(lat, lon):
        k = 2 ** z
        x = (lon + 180.0) / 360.0 * k
        lr = math.radians(lat)
        y = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * k
        return x, y
    x0, y0 = xy(n, w)
    x1, y1 = xy(s, e)
    return int(x0), int(x1), int(y0), int(y1)


def _render(img, bounds, z, x, y, size=256):
    """Nearest-sample the WGS84 frame (linear in lat/lon) into tile z/x/y."""
    (s, w), (n, e) = bounds
    H, W = img.shape[:2]
    k = 2 ** z
    px = (x + (np.arange(size) + 0.5) / size) / k * 360.0 - 180.0
    py = y + (np.arange(size) + 0.5) / size
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * py / k))))
    cols = np.floor((px - w) / (e - w) * W).astype(int)
    rows = np.floor((n - lat) / (n - s) * H).astype(int)
    inside_c, inside_r = (cols >= 0) & (cols < W), (rows >= 0) & (rows < H)
    out = np.zeros((size, size, 4), np.uint8)
    rr, cc = np.meshgrid(np.clip(rows, 0, H - 1), np.clip(cols, 0, W - 1), indexing="ij")
    out[:] = img[rr, cc]
    out[~(inside_r[:, None] & inside_c[None, :])] = 0
    return out


def build(export_dir: Path, out_dir: Path, zmax: int = 9, views=None, tags=None):
    from pmtiles.writer import Writer
    from pmtiles.tile import zxy_to_tileid, TileType, Compression
    man = json.loads((export_dir / "manifest.json").read_text())
    bounds = man["bounds"]
    views = views or [v for v in man["layers"] if v in ("ski6", "powder", "wind", "density", "ski18", "simple")]
    tags = tags or man["tags"]
    (s, w), (n, e) = bounds
    written = 0
    for view in views:
        tmpl = man["layers"][view]["file"]
        (out_dir / view).mkdir(parents=True, exist_ok=True)
        for tag in tags:
            src = export_dir / tmpl.replace("{tag}", tag)
            if not src.exists():
                continue
            img = np.asarray(Image.open(src).convert("RGBA"))
            path = out_dir / view / f"{tag}.pmtiles"
            with open(path, "wb") as f:
                wr = Writer(f)
                n_t = 0
                for z in range(ZMIN, zmax + 1):
                    x0, x1, y0, y1 = _tile_range(bounds, z)
                    for x in range(x0, x1 + 1):
                        for y in range(y0, y1 + 1):
                            t = _render(img, bounds, z, x, y)
                            if not t[..., 3].any():
                                continue
                            buf = io.BytesIO()
                            Image.fromarray(t, "RGBA").save(buf, "PNG", optimize=True)
                            wr.write_tile(zxy_to_tileid(z, x, y), buf.getvalue())
                            n_t += 1
                wr.finalize({
                    "tile_type": TileType.PNG, "tile_compression": Compression.NONE,
                    "min_lon_e7": int(w * 1e7), "min_lat_e7": int(s * 1e7),
                    "max_lon_e7": int(e * 1e7), "max_lat_e7": int(n * 1e7),
                    "center_zoom": 7, "center_lon_e7": int((w + e) / 2 * 1e7),
                    "center_lat_e7": int((s + n) / 2 * 1e7),
                }, {"name": f"snowpack {view} {tag}", "attribution": "SNOWPACK / SLF model chain"})
            written += 1
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir"); ap.add_argument("out_dir")
    ap.add_argument("--zmax", type=int, default=9)
    a = ap.parse_args()
    n = build(Path(a.export_dir), Path(a.out_dir), a.zmax)
    print(f"overview: {n} PMTiles archives -> {a.out_dir}")


if __name__ == "__main__":
    main()
