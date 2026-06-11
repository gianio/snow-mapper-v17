"""Raster-Engine: verknuepft Terrain-Features und interpolierte Wetterfelder.

Baut aus den punktweisen Wetter-Summaries (``PointSummary``) die auf das
DEM-Raster interpolierten Felder (``WeatherGrid``) und ruft das Kernmodell auf.
Reine Berechnung - keine Datei-/Netzwerkzugriffe.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np

from .aggregation import PointSummary
from .interpolation import idw_direction_grid, idw_grid
from .snow_model import WeatherGrid, compute_new_snow
from .terrain_features import TerrainFeatures


def build_grid_coordinates(
    bounds: Tuple[float, float, float, float], res: float, shape: Tuple[int, int]
) -> Tuple[np.ndarray, np.ndarray]:
    """Erzeugt Ost/Nord-Koordinaten der Zellmittelpunkte (north-up)."""
    east_min, north_min, east_max, north_max = bounds
    height, width = shape
    xs = east_min + (np.arange(width) + 0.5) * res
    ys = north_max - (np.arange(height) + 0.5) * res  # Zeile 0 = Norden
    return np.meshgrid(xs, ys)


def interpolate_weather(
    summaries: Sequence[PointSummary],
    points_xy: np.ndarray,
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    power: float = 2.0,
) -> WeatherGrid:
    """Interpoliert alle Wettergroessen per IDW auf das Raster."""
    precip = np.array([s.precipitation_mm for s in summaries])
    snow = np.array([s.snowfall_cm for s in summaries])
    temp = np.array([s.temperature_c for s in summaries])
    elev = np.array([s.elevation for s in summaries])
    wspd = np.array([s.wind_speed_ms for s in summaries])
    wdir = np.array([s.wind_direction_deg for s in summaries])

    return WeatherGrid(
        precipitation_mm=idw_grid(points_xy, precip, grid_x, grid_y, power),
        snowfall_cm=idw_grid(points_xy, snow, grid_x, grid_y, power),
        temperature_c_ref=idw_grid(points_xy, temp, grid_x, grid_y, power),
        ref_elevation=idw_grid(points_xy, elev, grid_x, grid_y, power),
        wind_speed_ms=idw_grid(points_xy, wspd, grid_x, grid_y, power),
        wind_direction_deg=idw_direction_grid(points_xy, wdir, grid_x, grid_y, power),
    )


def run_raster_model(
    terrain: TerrainFeatures,
    weather: WeatherGrid,
    params: dict,
) -> Dict[str, np.ndarray]:
    """Fuehrt das Kernmodell flaechig aus (Delegation an snow_model)."""
    return compute_new_snow(terrain, weather, params)
