"""Geo-Hilfsfunktionen fuer die Pipeline (Koordinatentransformationen).

Brueckt zwischen dem DEM-CRS (z.B. LV95/EPSG:2056, Meter) und WGS84
(EPSG:4326), das die Wetter-API erwartet. Verwendet pyproj.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np

try:
    from pyproj import Transformer
    _HAS_PYPROJ = True
except Exception:  # pragma: no cover
    _HAS_PYPROJ = False


def _transformer(src: str, dst: str):
    if not _HAS_PYPROJ:
        raise RuntimeError("pyproj nicht installiert - CRS-Transformation noetig.")
    return Transformer.from_crs(src, dst, always_xy=True)


def bounds_to_wgs84(
    bounds: Tuple[float, float, float, float], crs: str
) -> Tuple[float, float, float, float]:
    """Transformiert eine Bounding Box ins WGS84 (lon/lat)."""
    if crs.upper() in ("EPSG:4326", "WGS84"):
        return bounds
    east_min, north_min, east_max, north_max = bounds
    tf = _transformer(crs, "EPSG:4326")
    lons, lats = tf.transform(
        [east_min, east_max, east_min, east_max],
        [north_min, north_max, north_max, north_min],
    )
    return (min(lons), min(lats), max(lons), max(lats))


def weather_sample_grid(
    bounds: Tuple[float, float, float, float], crs: str, step_deg: float
) -> Tuple[List[float], List[float], np.ndarray]:
    """Erzeugt ein duennes Wetter-Abfragegitter ueber der AOI.

    Returns
    -------
    (latitudes, longitudes, points_xy)
        Lat/Lon-Listen fuer die API und die zugehoerigen Punktkoordinaten im
        DEM-CRS (Meter, fuer die spaetere IDW-Interpolation).
    """
    lon_min, lat_min, lon_max, lat_max = bounds_to_wgs84(bounds, crs)
    # mindestens 2x2 Punkte, sonst gibt IDW kein sinnvolles Feld.
    lons = np.arange(lon_min, lon_max + step_deg, step_deg)
    lats = np.arange(lat_min, lat_max + step_deg, step_deg)
    if len(lons) < 2:
        lons = np.linspace(lon_min, lon_max, 2)
    if len(lats) < 2:
        lats = np.linspace(lat_min, lat_max, 2)

    grid_lon, grid_lat = np.meshgrid(lons, lats)
    flat_lon = grid_lon.ravel()
    flat_lat = grid_lat.ravel()

    if crs.upper() in ("EPSG:4326", "WGS84"):
        xs, ys = flat_lon, flat_lat
    else:
        tf = _transformer("EPSG:4326", crs)
        xs, ys = tf.transform(flat_lon, flat_lat)

    points_xy = np.column_stack([np.asarray(xs), np.asarray(ys)])
    # WICHTIG: (latitudes, longitudes) - genau diese Reihenfolge erwartet der
    # Aufrufer und die Open-Meteo-API.
    return flat_lat.tolist(), flat_lon.tolist(), points_xy
