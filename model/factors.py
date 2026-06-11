"""Physikalisch motivierte Faktorfunktionen des Neuschneemodells.

Jede Funktion ist eine reine, vektorisierte numpy-Transformation und liest
ihre Koeffizienten aus dem ``params``-Dictionary (config/model_params.yaml).
Keine Datenzugriffe, keine versteckten Konstanten.

Grundformel (siehe snow_model.py):
    Snow_new = base_precip x temp_factor x altitude_factor
               x orographic_factor x wind_factor
mit zusaetzlicher Schnee-zu-Wasser-Umrechnung (SLR) im "precip"-Modus.
"""
from __future__ import annotations

import numpy as np


# --------------------------------------------------------------------------- #
# 1. Temperatur -> Schneeanteil (Regen/Schnee-Trennung)
# --------------------------------------------------------------------------- #
def temperature_factor(temp_c: np.ndarray, params: dict) -> np.ndarray:
    """Anteil des Niederschlags, der als Schnee faellt (logistisch um T_center).

    snow_fraction = 1 / (1 + exp(k * (T - T_center)))   in [0, 1]
    """
    p = params["temperature"]
    x = p["k"] * (temp_c - p["t_center_c"])
    frac = 1.0 / (1.0 + np.exp(np.clip(x, -50, 50)))
    return np.clip(frac, 0.0, 1.0)


# --------------------------------------------------------------------------- #
# 2. Hoehenkorrektur (Temperatur via Lapse-Rate, Niederschlag via Enhancement)
# --------------------------------------------------------------------------- #
def lapse_adjust_temperature(
    temp_ref_c: np.ndarray,
    elevation: np.ndarray,
    ref_elevation: np.ndarray,
    params: dict,
) -> np.ndarray:
    """Korrigiert die Referenztemperatur auf die DEM-Zellenhoehe."""
    lapse = params["altitude"]["temp_lapse_k_per_m"]
    return temp_ref_c + lapse * (elevation - ref_elevation)


def altitude_precip_factor(
    elevation: np.ndarray,
    ref_elevation: np.ndarray,
    params: dict,
) -> np.ndarray:
    """Linearer Niederschlags-Enhancement mit der Hoehe (orograf. Gradient)."""
    p = params["altitude"]
    factor = 1.0 + p["precip_gamma_per_m"] * (elevation - ref_elevation)
    return np.clip(factor, p["precip_factor_min"], p["precip_factor_max"])


# --------------------------------------------------------------------------- #
# 3. Schnee-zu-Wasser-Verhaeltnis (SLR), temperaturabhaengig
# --------------------------------------------------------------------------- #
def snow_to_liquid_ratio(temp_c: np.ndarray, params: dict) -> np.ndarray:
    """cm Schnee pro cm Schmelzwasseraequivalent; kaelter -> lockerer."""
    p = params["slr"]
    slr = p["slr_intercept"] + p["slr_slope"] * temp_c
    return np.clip(slr, p["slr_min"], p["slr_max"])


# --------------------------------------------------------------------------- #
# 4. Orographischer Faktor (Luv-Verstaerkung / Lee-Abschwaechung)
# --------------------------------------------------------------------------- #
def orographic_factor(
    slope_rad: np.ndarray,
    aspect_deg: np.ndarray,
    wind_from_deg: np.ndarray,
    wind_speed_ms: np.ndarray,
    params: dict,
) -> np.ndarray:
    """Verstaerkt Niederschlag an windzugewandten (Luv-)Haengen.

    alignment = cos(aspect - wind_from)  (+1 Luv, -1 Lee)
    factor    = 1 + k_oro * sin(slope) * wind_norm * alignment
    """
    p = params["orographic"]
    alignment = np.cos(np.radians(aspect_deg - wind_from_deg))
    wind_norm = np.clip(wind_speed_ms / p["wind_ref_ms"], 0.0, 2.0)
    factor = 1.0 + p["k_oro"] * np.sin(slope_rad) * wind_norm * alignment
    return np.clip(factor, p["factor_min"], p["factor_max"])


# --------------------------------------------------------------------------- #
# 5. Wind-Umverteilung (Erosion an Kaemmen / Akkumulation im Lee)
# --------------------------------------------------------------------------- #
def wind_redistribution_factor(
    curvature: np.ndarray,
    slope_rad: np.ndarray,
    aspect_deg: np.ndarray,
    wind_from_deg: np.ndarray,
    wind_speed_ms: np.ndarray,
    params: dict,
) -> np.ndarray:
    """Heuristische Umverteilung bereits gefallenen Schnees durch Wind.

    - Erosion  ~ Konvexitaet (positive Kruemmung, exponierte Grate)
    - Deposition ~ Konkavitaet (negative Kruemmung) + Lee-Hanglage
    """
    p = params["wind_redistribution"]
    wind_norm = np.clip(wind_speed_ms / p["wind_ref_ms"], 0.0, 2.0)

    convex = np.clip(curvature, 0.0, None)          # >0 nur Grate
    concave = np.clip(-curvature, 0.0, None)         # >0 nur Mulden
    lee = np.clip(-np.cos(np.radians(aspect_deg - wind_from_deg)), 0.0, None)

    erosion = p["k_erode"] * wind_norm * convex
    deposition = p["k_deposit"] * wind_norm * (concave + 0.5 * lee * np.sin(slope_rad))

    factor = 1.0 - erosion + deposition
    return np.clip(factor, p["factor_min"], p["factor_max"])
