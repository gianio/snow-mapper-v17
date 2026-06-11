"""Copernicus DEM (GLO-30) Loader - ECHTES Terrain ohne Login/Download-Schritt.

Liest die frei zugaenglichen Copernicus-DEM-30m-Kacheln (1-Grad-COGs) direkt
ueber HTTP (AWS Open Data) per GDAL ``/vsicurl/``, mosaikiert die fuer die AOI
benoetigten Kacheln und reprojiziert/resampelt sie auf das AOI-Raster (LV95).

Damit liefert das System reale Hoehen (statt des synthetischen Demo-DEM), ohne
dass der Nutzer manuell Gigabyte herunterladen muss. Fuer ECHTE 10-m-Aufloesung
ist swissALTI3D noetig (Copernicus ist eine 30-m-Quelle); fuer landesweite
Uebersichten (z.B. 100-200 m) ist Copernicus ideal.

Hinweis: nur N/E-Hemisphaere (Schweiz). Quelle:
https://registry.opendata.aws/copernicus-dem/
"""
from __future__ import annotations

import math
import os
from typing import List, Tuple

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from pyproj import Transformer

from config.settings import DATA_DIR
from .dem_loader import DEM

_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
_NODATA = -9999.0


def _tile_name(lat: int, lon: int) -> str:
    return f"Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM"


def _tile_url(lat: int, lon: int) -> str:
    name = _tile_name(lat, lon)
    return f"/vsicurl/{_BASE}/{name}/{name}.tif"


def _required_tiles(
    lon_min: float, lat_min: float, lon_max: float, lat_max: float
) -> List[Tuple[int, int]]:
    tiles = []
    for lat in range(math.floor(lat_min), math.floor(lat_max) + 1):
        for lon in range(math.floor(lon_min), math.floor(lon_max) + 1):
            tiles.append((lat, lon))
    return tiles


def load_copernicus_dem(
    bounds: Tuple[float, float, float, float],
    res: float,
    crs: str = "EPSG:2056",
) -> DEM:
    """Laedt reales Copernicus-DEM fuer die AOI und reprojiziert es auf das Raster.

    Robust gegen Haenger: harte HTTP-Timeouts + dezimiertes Lesen ueber die
    COG-Overviews (es wird NUR die zur Zielaufloesung passende grobe Stufe
    uebertragen, nicht die volle 30-m-Kachel). Damit ist auch ein landesweiter
    200-m-Lauf in Sekunden statt Minuten fertig und blockiert nicht.

    Parameters
    ----------
    bounds : (east_min, north_min, east_max, north_max) im Ziel-CRS (LV95).
    res    : Ziel-Rasterweite [m].
    crs    : Ziel-CRS (Default EPSG:2056).
    """
    from rasterio import Affine

    east_min, north_min, east_max, north_max = bounds
    width = int(round((east_max - east_min) / res))
    height = int(round((north_max - north_min) / res))
    dst_transform = from_origin(east_min, north_max, res, res)

    # --- Disk-Cache: identische AOI/Aufloesung wird nur einmal geladen. ------ #
    cache_dir = DATA_DIR / "dem_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = (f"cop_{int(east_min)}_{int(north_min)}_{int(east_max)}_"
           f"{int(north_max)}_{int(res)}.tif")
    cache_path = cache_dir / key
    if cache_path.exists():
        try:
            with rasterio.open(cache_path) as src:
                elev = src.read(1).astype("float64")
            elev = np.where(elev == _NODATA, np.nan, elev)
            print(f"[DEM] Copernicus-DEM aus Cache: {cache_path.name}")
            return DEM(elevation=elev, transform=dst_transform, crs=crs,
                       res=res, bounds=bounds)
        except Exception:
            pass  # defekter Cache -> neu laden

    tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lons, lats = tf.transform(
        [east_min, east_max, east_min, east_max],
        [north_min, north_min, north_max, north_max],
    )
    tiles = _required_tiles(min(lons), min(lats), max(lons), max(lats))

    dst = np.full((height, width), _NODATA, dtype="float32")

    # Dezimationsfaktor: Copernicus ~30 m Quelle -> grob auf Zielaufloesung.
    decim = max(1, int(res // 30))

    gdal_env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_USE_HEAD": "NO",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_TIMEOUT": os.environ.get("GDAL_HTTP_TIMEOUT", "30"),
        "GDAL_HTTP_CONNECTTIMEOUT": os.environ.get("GDAL_HTTP_CONNECTTIMEOUT", "15"),
        "GDAL_HTTP_MAX_RETRY": "2",
        "GDAL_HTTP_RETRY_DELAY": "1",
        "VSI_CACHE": "TRUE",
        # Sicheres SSL; nur falls per Umgebung explizit deaktiviert (MITM-Proxy).
        "GDAL_HTTP_UNSAFESSL": os.environ.get("GDAL_HTTP_UNSAFESSL", "NO"),
        "AWS_NO_SIGN_REQUEST": "YES",
    }

    loaded = 0
    with rasterio.Env(**gdal_env):
        for i, (lat, lon) in enumerate(tiles):
            url = _tile_url(lat, lon)
            print(f"[DEM] Copernicus-Kachel {i + 1}/{len(tiles)} N{lat}E{lon} ...")
            try:
                with rasterio.open(url) as src:
                    out_h = max(1, src.height // decim)
                    out_w = max(1, src.width // decim)
                    data = src.read(
                        1, out_shape=(out_h, out_w), resampling=Resampling.average
                    ).astype("float32")
                    # Transform der dezimierten Quelle.
                    sx = src.width / out_w
                    sy = src.height / out_h
                    src_transform_dec = src.transform * Affine.scale(sx, sy)
                    reproject(
                        source=data,
                        destination=dst,
                        src_transform=src_transform_dec,
                        src_crs=src.crs,
                        dst_transform=dst_transform,
                        dst_crs=crs,
                        dst_nodata=_NODATA,
                        resampling=Resampling.bilinear,
                        init_dest_nodata=False,
                    )
                    loaded += 1
            except Exception as exc:  # einzelne Kachel nicht fatal
                print(f"[DEM] Kachel N{lat}E{lon} uebersprungen: {exc!r}")

    if loaded == 0:
        raise RuntimeError(
            "Keine Copernicus-DEM-Kachel ladbar (Netzwerk/Proxy/Timeout?). "
            "Optionen: erneut versuchen, --offline (synthetisch, nicht geo-korrekt) "
            "oder eigenes GeoTIFF via --dem."
        )

    elevation = np.where(dst == _NODATA, np.nan, dst).astype("float64")
    # In den Cache schreiben (nur das fertige AOI-DEM, klein).
    try:
        with rasterio.open(
            cache_path, "w", driver="GTiff", height=height, width=width, count=1,
            dtype="float32", crs=crs, transform=dst_transform, nodata=_NODATA,
            compress="deflate",
        ) as out:
            out.write(dst, 1)
        print(f"[DEM] Copernicus-DEM gecacht: {cache_path.name}")
    except Exception:
        pass

    return DEM(
        elevation=elevation,
        transform=dst_transform,
        crs=crs,
        res=res,
        bounds=bounds,
    )
