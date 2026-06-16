"""Overlay-Export: 10-m-Pixel-Ebene als Layer ueber einer Schweizer Karte.

Drei Artefakte:
    1. WGS84-GeoTIFF (EPSG:4326) - Standard-Layer fuer GIS (QGIS/swisstopo).
    2. RGBA-PNG-Kachel - schneefreie Flaechen transparent, kolorierter Neuschnee;
       inkl. JSON-Sidecar mit den Lat/Lon-Eckkoordinaten.
    3. Interaktive folium/Leaflet-Karte (HTML) mit Schweizer Basemap
       (swisstopo Pixelkarte + OpenStreetMap), auf der die PNG-Ebene exakt
       georeferenziert ueberlagert wird.

Reproduzierbare Reprojektion via rasterio.warp; reine Ausgabelogik.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import numpy as np

import rasterio
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

import matplotlib.colors as mcolors
from PIL import Image

# SLF-aehnliche, FESTE Klassen fuer Neuschnee [cm] - identisch fuer jede Karte,
# damit das Ergebnis direkt mit der SLF-Neuschneekarte vergleichbar ist.
SLF_BOUNDS = [1, 5, 10, 20, 30, 50, 75, 100, 150, 300]  # cm-Grenzen (SLF-nah)
SLF_COLORS = [
    "#e8f5e9",  # 1-5    sehr hell gruen
    "#a5d6a7",  # 5-10   hellgruen
    "#66bb6a",  # 10-20  gruen
    "#42a5f5",  # 20-30  blau
    "#1e88e5",  # 30-50  mittelblau
    "#1565c0",  # 50-75  dunkelblau
    "#7b1fa2",  # 75-100 violett
    "#e91e63",  # 100-150 pink/magenta
    "#b71c1c",  # >=150  dunkelrot
]
_NODATA = -9999.0


@dataclass
class OverlayResult:
    geotiff_wgs84: Path
    png: Path
    bounds_latlon: Tuple[float, float, float, float]  # (lat_min, lon_min, lat_max, lon_max)
    html: Path | None


def reproject_to_wgs84(
    array: np.ndarray, transform, src_crs: str, bounds, res: float
) -> tuple[np.ndarray, object, Tuple[float, float, float, float]]:
    """Reprojiziert ein Raster nach EPSG:4326 (bilinear).

    Returns
    -------
    (data_4326, dst_transform, (lat_min, lon_min, lat_max, lon_max))
    """
    east_min, north_min, east_max, north_max = bounds
    height, width = array.shape
    dst_crs = "EPSG:4326"

    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs, dst_crs, width, height, east_min, north_min, east_max, north_max
    )
    dst = np.full((dst_h, dst_w), _NODATA, dtype="float32")
    reproject(
        source=array.astype("float32"),
        destination=dst,
        src_transform=transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        src_nodata=_NODATA,
        dst_nodata=_NODATA,
        resampling=Resampling.bilinear,
    )
    left, bottom, right, top = array_bounds(dst_h, dst_w, dst_transform)
    return dst, dst_transform, (bottom, left, top, right)


def write_wgs84_geotiff(data: np.ndarray, transform, path: Path) -> Path:
    with rasterio.open(
        path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
        count=1, dtype="float32", crs="EPSG:4326", transform=transform,
        nodata=_NODATA, compress="deflate",
    ) as dst:
        dst.write(data, 1)
    return path


def render_rgba_png(
    data: np.ndarray,
    path: Path,
    min_visible_cm: float = 1.0,
    opacity: float = 1.0,
) -> Path:
    """Rendert das Raster als RGBA-PNG in den FESTEN SLF-Klassen.

    < ``min_visible_cm`` (1 cm, wie SLF) und NoData werden transparent.
    """
    valid = (data != _NODATA) & np.isfinite(data)
    cmap = mcolors.ListedColormap(SLF_COLORS)
    cmap.set_under((0, 0, 0, 0))  # unter 1 cm: transparent
    norm = mcolors.BoundaryNorm(SLF_BOUNDS, cmap.N, clip=False)

    filled = np.where(valid, data, -1.0)  # ungueltig -> unter erste Grenze
    rgba = cmap(norm(filled))  # (H, W, 4)
    transparent = (~valid) | (data < min_visible_cm)
    rgba[..., 3] = np.where(transparent, 0.0, opacity)

    Image.fromarray((rgba * 255).astype("uint8"), mode="RGBA").save(path)
    path.with_suffix(".json").write_text(
        json.dumps({"bounds_cm": SLF_BOUNDS, "unit": "cm", "scale": "SLF"}),
        encoding="utf-8",
    )
    return path


def build_folium_map(
    png_path: Path,
    bounds_latlon: Tuple[float, float, float, float],
    out_html: Path,
    opacity: float = 0.8,
) -> Path:
    """Baut eine Leaflet-Karte mit Schweizer Basemap + georeferenziertem Overlay.

    Legende: feste SLF-Klassen (StepColormap), identisch fuer jede Karte.
    """
    import folium
    from branca.colormap import StepColormap

    lat_min, lon_min, lat_max, lon_max = bounds_latlon
    center = [(lat_min + lat_max) / 2.0, (lon_min + lon_max) / 2.0]

    m = folium.Map(location=center, zoom_start=12, tiles=None, control_scale=True)

    folium.TileLayer(
        tiles="https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/"
        "default/current/3857/{z}/{x}/{y}.jpeg",
        attr="© swisstopo",
        name="swisstopo Pixelkarte",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap", overlay=False).add_to(m)

    folium.raster_layers.ImageOverlay(
        image=str(png_path),
        bounds=[[lat_min, lon_min], [lat_max, lon_max]],
        opacity=opacity,
        name="Neuschnee (SLF-Skala)",
        interactive=True,
        cross_origin=False,
        zindex=10,
    ).add_to(m)

    # Feste SLF-Klassenlegende.
    colormap = StepColormap(
        colors=SLF_COLORS,
        index=SLF_BOUNDS[:-1],
        vmin=SLF_BOUNDS[0],
        vmax=SLF_BOUNDS[-1],
        caption="Neuschnee [cm] - SLF-Klassen",
    )
    colormap.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(out_html))
    return out_html


def export_overlay(
    new_snow: np.ndarray,
    transform,
    src_crs: str,
    bounds,
    res: float,
    out_dir: Path,
    tag: str,
    make_html: bool = True,
) -> OverlayResult:
    """End-to-end: Reprojektion -> WGS84-GeoTIFF + PNG-Kachel + folium-Karte.

    Die Farbskala ist FEST (SLF-Klassen), damit jede Karte direkt mit der
    SLF-Neuschneekarte vergleichbar ist.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    data_4326, transform_4326, bounds_latlon = reproject_to_wgs84(
        new_snow, transform, src_crs, bounds, res
    )
    tif = write_wgs84_geotiff(data_4326, transform_4326, out_dir / f"new_snow_{tag}_wgs84.tif")
    png = render_rgba_png(data_4326, out_dir / f"new_snow_{tag}_overlay.png")

    html = None
    if make_html:
        html = build_folium_map(png, bounds_latlon, out_dir / f"map_{tag}.html")
    return OverlayResult(geotiff_wgs84=tif, png=png, bounds_latlon=bounds_latlon, html=html)
