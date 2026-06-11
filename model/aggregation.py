"""Zeitliche Aggregation der stuendlichen Wetterdaten (Feature Engineering).

Verdichtet die stuendlichen ``PointForecast``-Rohdaten auf ein Akkumulations-
fenster (z.B. 24h): Niederschlag/Schneefall werden summiert, Temperatur und
Windgeschwindigkeit gemittelt, die Windrichtung als Vektormittel (ueber u/v)
berechnet. Reine Mathematik, kein Datenzugriff.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import numpy as np


@dataclass
class PointSummary:
    """Aggregierte Wettergroessen eines Punkts ueber das Zeitfenster."""

    latitude: float
    longitude: float
    elevation: float
    precipitation_mm: float    # Summe [mm]
    snowfall_cm: float         # Summe [cm]
    temperature_c: float       # Mittel [degC]
    wind_speed_ms: float       # Mittel [m/s]
    wind_direction_deg: float  # Vektormittel [Grad, woher]


def aggregate_window(forecasts: Sequence, hours: int) -> List[PointSummary]:
    """Aggregiert die ersten ``hours`` Stunden jedes Punkts.

    Parameters
    ----------
    forecasts
        Sequenz von ``PointForecast`` (data_connectors.open_meteo_client).
    hours
        Laenge des Akkumulationsfensters.
    """
    summaries: List[PointSummary] = []
    for fc in forecasts:
        n = min(hours, len(fc.time))
        temp = np.asarray(fc.temperature_2m[:n], dtype="float64")
        precip = np.asarray(fc.precipitation[:n], dtype="float64")
        snow = np.asarray(fc.snowfall[:n], dtype="float64")
        wspd = np.asarray(fc.wind_speed_10m[:n], dtype="float64")
        wdir = np.asarray(fc.wind_direction_10m[:n], dtype="float64")

        summaries.append(
            PointSummary(
                latitude=fc.latitude,
                longitude=fc.longitude,
                elevation=fc.elevation,
                precipitation_mm=float(np.sum(precip)),
                snowfall_cm=float(np.sum(snow)),
                temperature_c=float(np.mean(temp)) if n else 0.0,
                wind_speed_ms=float(np.mean(wspd)) if n else 0.0,
                wind_direction_deg=_vector_mean_direction(wdir, wspd),
            )
        )
    return summaries


def _vector_mean_direction(direction_deg: np.ndarray, speed: np.ndarray) -> float:
    """Windgewichtetes Vektormittel der Richtung (woher), in Grad [0, 360)."""
    if direction_deg.size == 0:
        return 0.0
    rad = np.radians(direction_deg)
    w = speed if np.any(speed > 0) else np.ones_like(direction_deg)
    u = np.sum(w * np.sin(rad))
    v = np.sum(w * np.cos(rad))
    mean = np.degrees(np.arctan2(u, v)) % 360.0
    return float(mean)
