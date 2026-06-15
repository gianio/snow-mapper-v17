"""Terrain-Feature-Berechnung aus dem DEM (reine Mathematik).

Leitet aus dem Hoehenraster ab:
    - slope_rad   : Hangneigung [rad]
    - aspect_deg  : Exposition [Grad, im Uhrzeigersinn von Nord; Richtung,
                    in die der Hang abfaellt -> "schaut"]
    - curvature   : normierte Kruemmung (z-Score, gekappt); positiv = konvex
                    (Grat/exponiert), negativ = konkav (Mulde/Lee-naehe)

Konvention np.gradient auf north-up-Array (Zeile 0 = Norden):
    grad[0] = Aenderung pro Zeile (nach Sueden)  -> gy_north = -grad[0]
    grad[1] = Aenderung pro Spalte (nach Osten)  -> gx_east  =  grad[1]
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TerrainFeatures:
    elevation: np.ndarray
    slope_rad: np.ndarray
    aspect_deg: np.ndarray
    curvature: np.ndarray  # normiert, dimensionslos


def _horn_gradients(z: np.ndarray, res: float):
    """Horn's method (3x3 weighted Sobel kernel) for dz/dx and dz/dy.

    Returns (dz_dx, dz_dy_row) where dz_dx is positive eastward and
    dz_dy_row is positive southward (for a north-up raster).
    """
    a = z[:-2, :-2]; b = z[:-2, 1:-1]; c = z[:-2, 2:]
    d = z[1:-1, :-2];                    f = z[1:-1, 2:]
    g = z[2:,   :-2]; h = z[2:,  1:-1]; i = z[2:,  2:]
    dz_dx = ((c + 2*f + i) - (a + 2*d + g)) / (8 * res)
    dz_dy = ((g + 2*h + i) - (a + 2*b + c)) / (8 * res)
    return dz_dx, dz_dy


def compute_terrain_features(elevation: np.ndarray, res: float) -> TerrainFeatures:
    """Berechnet Slope, Aspect und normierte Kruemmung aus dem DEM.

    Uses Horn's method (1981) for slope and aspect — the standard 3x3
    weighted gradient used by GDAL gdaldem, ArcGIS, and QGIS.
    """
    z = _fill_nan(elevation)

    dz_dx, dz_dy = _horn_gradients(z, res)
    # Pad back to original shape (replicate border)
    gx = np.pad(dz_dx, 1, mode='edge')   # east gradient
    gy = np.pad(dz_dy, 1, mode='edge')   # south gradient (north-up raster)

    slope_rad = np.arctan(np.hypot(gx, gy))

    # Aspect: downslope direction, clockwise from North.
    # For north-up: east component of downslope = -gx, north component = gy
    aspect_deg = np.degrees(np.arctan2(-gx, gy)) % 360.0
    aspect_deg = np.where(slope_rad < 1e-4, 0.0, aspect_deg)

    curvature = _normalized_curvature(z, res)

    return TerrainFeatures(
        elevation=z,
        slope_rad=slope_rad,
        aspect_deg=aspect_deg,
        curvature=curvature,
    )


def _normalized_curvature(z: np.ndarray, res: float) -> np.ndarray:
    """Diskreter Laplace-Operator -> Konvexitaetsindex (z-normiert, gekappt).

    Laplace(z) > 0 in Mulden (konkav), < 0 auf Graten (konvex). Wir invertieren
    das Vorzeichen, sodass POSITIV = konvex/exponiert (windexponiert), was
    intuitiver zur Wind-Erosion passt.
    """
    grad_row, grad_col = np.gradient(z, res)
    d2_row, _ = np.gradient(grad_row, res)
    _, d2_col = np.gradient(grad_col, res)
    laplace = d2_row + d2_col
    convexity = -laplace

    std = np.nanstd(convexity)
    if std < 1e-9:
        return np.zeros_like(convexity)
    z_score = (convexity - np.nanmean(convexity)) / std
    return np.clip(z_score, -3.0, 3.0)


def _fill_nan(z: np.ndarray) -> np.ndarray:
    """Ersetzt NaN durch den globalen Mittelwert (robust gegen Randluecken)."""
    if not np.any(np.isnan(z)):
        return z.astype("float64")
    z = z.astype("float64")
    z[np.isnan(z)] = np.nanmean(z)
    return z


def hillshade(elevation: np.ndarray, res: float, az_deg: float = 315.0,
              alt_deg: float = 45.0) -> np.ndarray:
    """Hillshade (0..1) using Horn's method gradients.

    az_deg : Sun azimuth (light source direction), alt_deg : Sun altitude.
    """
    z = _fill_nan(elevation)
    dz_dx, dz_dy = _horn_gradients(z, res)
    gx = np.pad(dz_dx, 1, mode='edge')
    gy = np.pad(dz_dy, 1, mode='edge')
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.radians(360.0 - az_deg + 90.0)
    alt = np.radians(alt_deg)
    shade = (np.sin(alt) * np.cos(slope) +
             np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    return np.clip(shade, 0.0, 1.0)


def roughness(elevation: np.ndarray) -> np.ndarray:
    """Terrain-Ruggedness-Index (TRI): mittlere |Hoehendifferenz| zu Nachbarn [m]."""
    z = _fill_nan(elevation)
    acc = np.zeros_like(z)
    cnt = np.zeros_like(z)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            sh = np.roll(np.roll(z, dr, axis=0), dc, axis=1)
            acc += np.abs(z - sh)
            cnt += 1.0
    return acc / np.maximum(cnt, 1.0)
