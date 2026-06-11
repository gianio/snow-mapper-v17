"""Validierung gegen SLF-Stationen + Bias-Korrektur (optional).

Vergleicht modellierten Neuschnee an den Stationskoordinaten mit den
gemessenen SLF-Werten, berechnet Bias/RMSE und leitet einen multiplikativen
Bias-Korrekturfaktor ab. Die SLF-Daten fliessen NICHT ins Raster ein, sondern
dienen nur dieser nachgelagerten Bewertung/Korrektur.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class ValidationResult:
    n: int
    bias_cm: float          # mittlerer Fehler (Modell - Messung)
    rmse_cm: float
    mae_cm: float
    correction_factor: float  # multiplikativ: Messung/Modell (robust, Median)
    table: pd.DataFrame


def sample_model_at_stations(
    new_snow: np.ndarray,
    bounds: tuple[float, float, float, float],
    res: float,
    stations_xy: np.ndarray,
) -> np.ndarray:
    """Liest Modellwerte an den Stationspunkten (Ost, Nord; naechste Zelle)."""
    east_min, north_min, east_max, north_max = bounds
    h, w = new_snow.shape
    out = np.full(len(stations_xy), np.nan)
    for i, (ex, ny) in enumerate(stations_xy):
        col = int(np.clip((ex - east_min) / res, 0, w - 1))
        row = int(np.clip((north_max - ny) / res, 0, h - 1))
        out[i] = new_snow[row, col]
    return out


def validate(
    modeled_cm: np.ndarray,
    measured_cm: np.ndarray,
    station_ids: np.ndarray,
) -> ValidationResult:
    """Berechnet Fehlermetriken und einen robusten Bias-Korrekturfaktor."""
    mask = np.isfinite(modeled_cm) & np.isfinite(measured_cm)
    m, o = modeled_cm[mask], measured_cm[mask]
    diff = m - o

    valid = m > 1e-6
    ratios = o[valid] / m[valid]
    correction = float(np.median(ratios)) if ratios.size else 1.0

    table = pd.DataFrame(
        {
            "station_id": station_ids[mask],
            "measured_cm": o,
            "modeled_cm": m,
            "error_cm": diff,
        }
    )
    return ValidationResult(
        n=int(mask.sum()),
        bias_cm=float(np.mean(diff)) if diff.size else float("nan"),
        rmse_cm=float(np.sqrt(np.mean(diff ** 2))) if diff.size else float("nan"),
        mae_cm=float(np.mean(np.abs(diff))) if diff.size else float("nan"),
        correction_factor=correction,
        table=table,
    )
