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
def temperature_factor(temp_c: np.ndarray, params: dict, rh_pct: np.ndarray | None = None,
                       elevation_m: np.ndarray | float = 1500.0) -> np.ndarray:
    """Anteil des Niederschlags, der als Schnee faellt (logistisch).

    snow_fraction = 1 / (1 + exp(k * (T - T_center)))   in [0, 1]

    Mit relativer Feuchte wird auf der Feuchtkugeltemperatur getrennt: in
    trockener Luft kuehlt verdunstender Niederschlag die Flocken, Schnee
    faellt dann bis +2..+4 degC Lufttemperatur (Ding et al. 2014; Sims & Liu
    2015). Ohne Feuchte bleibt die Trennung auf der Lufttemperatur.
    """
    p = params["temperature"]
    if rh_pct is not None and "wetbulb_center_c" in p:
        t, tc, k = wet_bulb_c(temp_c, rh_pct, elevation_m), p["wetbulb_center_c"], p.get("wetbulb_k", p["k"])
    else:
        t, tc, k = temp_c, p["t_center_c"], p["k"]
    x = k * (t - tc)
    frac = 1.0 / (1.0 + np.exp(np.clip(x, -50, 50)))
    return np.clip(frac, 0.0, 1.0)


def wet_bulb_c(temp_c: np.ndarray, rh_pct: np.ndarray, elevation_m: np.ndarray | float = 1500.0) -> np.ndarray:
    """Feuchtkugeltemperatur [degC] aus der Psychrometergleichung
    e = e_s(Tw) - gamma * p * (T - Tw), mit Magnus-Saettigungsdruck (ueber
    Wasser) und dem Luftdruck der Hoehe (Standardatmosphaere); einige
    Newton-Schritte genuegen (< 0.01 K). In duenner Hoehenluft ist die
    Abkuehlung bei gleicher Feuchte etwas groesser als im Tal."""
    t = np.asarray(temp_c, dtype="float64")
    rh = np.clip(np.asarray(rh_pct, dtype="float64"), 1.0, 100.0)
    z = np.clip(np.asarray(elevation_m, dtype="float64"), -500.0, 9000.0)
    p = 1013.25 * (1.0 - 2.25577e-5 * z) ** 5.25588          # hPa
    gam = 6.53e-4                                              # 1/K, belueftet

    def es(x):
        return 6.112 * np.exp(17.62 * x / (243.12 + x))

    e = rh / 100.0 * es(t)
    tw = t.copy() if t.ndim else np.array(float(t))
    for _ in range(6):
        f = es(tw) - gam * p * (t - tw) - e
        df = es(tw) * 17.62 * 243.12 / (243.12 + tw) ** 2 + gam * p
        tw = tw - f / df
    return np.minimum(tw, t)


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
def snow_to_liquid_ratio(temp_c: np.ndarray, params: dict, wind_ms: np.ndarray | None = None) -> np.ndarray:
    """cm Schnee pro cm Schmelzwasseraequivalent; kaelter -> lockerer,
    windiger -> dichter.

    method "crocus": Neuschneedichte wie im Schneedeckenmodell Crocus
    (Vionnet et al. 2012, GMD 5):  rho = a + b*T + c*sqrt(U)  [kg/m3],
    SLR = 1000 / rho. Wind bricht die Kristalle beim Fallen und packt sie
    ab, in den Alpen der wichtigste Grund fuer dichten Neuschnee.
    Sonst die lineare Temperaturformel.
    """
    p = params["slr"]
    if p.get("method") == "crocus":
        u = np.zeros_like(np.asarray(temp_c, dtype="float64")) if wind_ms is None else np.clip(wind_ms, 0.0, None)
        rho = p["rho_a"] + p["rho_b"] * np.asarray(temp_c, dtype="float64") + p["rho_c"] * np.sqrt(u)
        rho = np.clip(rho, p["rho_min"], p["rho_max"])
        return 1000.0 / rho
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
