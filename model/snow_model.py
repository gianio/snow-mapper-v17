"""Neuschnee-Kernmodell - Kombination aller Faktoren.

Implementiert die dokumentierte Grundformel und gibt fuer volle
Nachvollziehbarkeit alle Zwischenschichten zurueck. Reine Mathematik.

Modus "precip" (Standard):
    T_cell     = lapse_adjust(T_ref, z_cell, z_ref)
    P_cell     = P_ref [mm] * altitude_factor * orographic_factor
    snow_frac  = temperature_factor(T_cell)
    SLR        = snow_to_liquid_ratio(T_cell)
    snow_pre   = P_cell * snow_frac * SLR / 10            [cm]
    Snow_new   = snow_pre * terrain_factor               [cm]
    terrain_factor = orographic x wind, lokal auf Mittel 1 normiert
    snow_frac  aus der Feuchtkugeltemperatur, wenn Feuchte vorliegt
    SLR        = 1000 / rho_new(T, Wind)  (Crocus)

Modus "snowfall":
    Basis ist die von Open-Meteo gelieferte snowfall [cm]; Phase ist bereits
    enthalten, daher nur Terrain-/Wind-Modifikation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

from . import factors
from .terrain_features import TerrainFeatures


@dataclass
class WeatherGrid:
    """Auf das DEM-Raster interpolierte Wettergroessen (alle 2D, gleiche Form)."""

    precipitation_mm: np.ndarray
    snowfall_cm: np.ndarray
    temperature_c_ref: np.ndarray   # an der Wetter-Referenzhoehe
    ref_elevation: np.ndarray       # Modellhoehe der Wetterquelle [m]
    wind_speed_ms: np.ndarray
    wind_direction_deg: np.ndarray  # woher
    # relative Feuchte [%] an der Referenzhoehe; None = Phase nur aus T
    rel_humidity_pct: Optional[np.ndarray] = None


def compute_new_snow(
    terrain: TerrainFeatures,
    weather: WeatherGrid,
    params: dict,
) -> Dict[str, np.ndarray]:
    """Berechnet den Neuschnee je Zelle und alle Zwischenschichten.

    Returns
    -------
    dict[str, np.ndarray]
        Schluessel u.a.: ``new_snow_cm`` (Hauptoutput), ``temp_cell_c``,
        ``snow_fraction``, ``altitude_factor``, ``orographic_factor``,
        ``slr``, ``wind_factor``.
    """
    mode = params.get("mode", {}).get("base", "precip")

    temp_cell = factors.lapse_adjust_temperature(
        weather.temperature_c_ref, terrain.elevation, weather.ref_elevation, params
    )
    alt_factor = factors.altitude_precip_factor(
        terrain.elevation, weather.ref_elevation, params
    )
    oro_factor = factors.orographic_factor(
        terrain.slope_rad,
        terrain.aspect_deg,
        weather.wind_direction_deg,
        weather.wind_speed_ms,
        params,
    )
    wind_factor = factors.wind_redistribution_factor(
        terrain.curvature,
        terrain.slope_rad,
        terrain.aspect_deg,
        weather.wind_direction_deg,
        weather.wind_speed_ms,
        params,
    )

    # Luv/Lee und Wind verlagern Schnee, sie erfinden keinen: der Faktor
    # wird auf seinen Mittelwert in der Umgebung (~1.5 km) normiert. Die
    # Stauniederschlaege der Gebirgsketten liefert schon das Wettermodell.
    terrain_factor = conserve_locally(oro_factor * wind_factor, terrain, params)

    if mode == "snowfall":
        snow_fraction = np.ones_like(temp_cell)
        slr = np.full_like(temp_cell, np.nan)
        snow_pre = weather.snowfall_cm * alt_factor
    else:  # "precip"
        snow_fraction = factors.temperature_factor(temp_cell, params, weather.rel_humidity_pct, terrain.elevation)
        slr = factors.snow_to_liquid_ratio(temp_cell, params, weather.wind_speed_ms)
        precip_cell = weather.precipitation_mm * alt_factor
        snow_pre = precip_cell * snow_fraction * slr / 10.0

    new_snow = snow_pre * terrain_factor
    new_snow = np.clip(new_snow, 0.0, params["limits"]["max_new_snow_cm"])

    return {
        "new_snow_cm": new_snow,
        "terrain_factor": terrain_factor,
        "temp_cell_c": temp_cell,
        "snow_fraction": snow_fraction,
        "altitude_factor": alt_factor,
        "orographic_factor": oro_factor,
        "wind_factor": wind_factor,
        "slr": slr,
    }


def conserve_locally(factor: np.ndarray, terrain: TerrainFeatures, params: dict) -> np.ndarray:
    """Teilt einen Umverteilungsfaktor durch sein gleitendes Mittel, so dass
    er ueber ``conserve_radius_m`` im Mittel 1 bleibt (Schnee wird verlagert,
    nicht erzeugt). Ohne Rasterweite oder Radius bleibt er, wie er ist."""
    p = params.get("wind_redistribution", {})
    radius = float(p.get("conserve_radius_m", 0.0) or 0.0)
    res = float(getattr(terrain, "res", 0.0) or 0.0)
    if radius <= 0 or res <= 0 or factor.ndim != 2:
        return factor
    from scipy.ndimage import gaussian_filter
    f = np.nan_to_num(factor, nan=1.0)
    mean = gaussian_filter(f, sigma=max(0.5, radius / res), mode="nearest")
    out = f / np.maximum(mean, 1e-3)
    return np.clip(out, p.get("total_factor_min", 0.2), p.get("total_factor_max", 2.5))
