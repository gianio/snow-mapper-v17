"""Synthetische Wetterquelle (Offline-Demo / Tests).

Erzeugt ``PointForecast``-Objekte mit derselben Struktur wie der
Open-Meteo-Client, sodass die Pipeline ohne Netzwerk reproduzierbar laeuft.
Bewusst als Datenquelle (Connector) gefuehrt, nicht als Modelllogik.
"""
from __future__ import annotations

from typing import List, Sequence

import numpy as np

from .open_meteo_client import PointForecast


def synthetic_forecast(
    latitudes: Sequence[float],
    longitudes: Sequence[float],
    elevations: Sequence[float],
    hours: int = 48,
    seed: int = 42,
) -> List[PointForecast]:
    """Plausibles Winter-Schneefallereignis ueber ``hours`` Stunden.

    Kalte Luft, anhaltender Niederschlag und Nordwestwind - typische
    Staulage-Situation. Werte variieren leicht raeumlich.
    """
    rng = np.random.default_rng(seed)
    times = [f"2025-01-15T{h % 24:02d}:00" for h in range(hours)]
    out: List[PointForecast] = []

    for lat, lon, elev in zip(latitudes, longitudes, elevations):
        # Temperatur sinkt mit Hoehe; Tagesgang ueberlagert.
        t_base = 2.0 - 0.0065 * (elev - 1000.0)
        diurnal = 2.5 * np.sin(np.linspace(0, hours / 24 * 2 * np.pi, hours))
        temp = t_base + diurnal + rng.normal(0, 0.4, hours)

        # Niederschlagsschub in der Mitte des Fensters.
        precip = np.clip(
            1.2 * np.exp(-((np.arange(hours) - hours / 2) ** 2) / (2 * 8.0 ** 2))
            + rng.normal(0, 0.05, hours),
            0,
            None,
        )
        snowfall = precip * 0.9  # grobe cm-Naeherung der Quelle
        wind_speed = np.clip(6.0 + rng.normal(0, 1.2, hours), 0, None)
        # Windrichtung variiert raeumlich (WNW im Westen -> NW im Osten),
        # damit das orografische Banding nicht ueberall identisch ausfaellt.
        base_dir = 290.0 + 55.0 * np.clip((lon - 5.9) / (10.5 - 5.9), 0, 1)
        wind_dir = (base_dir + rng.normal(0, 12.0, hours)) % 360

        # Sonnenschein: Tagesgang (0 nachts), reduziert bei Niederschlag.
        hod = np.arange(hours) % 24
        day = np.clip(np.sin((hod - 6) / 24 * 2 * np.pi), 0, None)
        sun = np.clip(day * 3600 * (1 - np.clip(precip * 2, 0, 1)), 0, 3600)

        out.append(
            PointForecast(
                latitude=float(lat),
                longitude=float(lon),
                elevation=float(elev),
                time=times,
                temperature_2m=temp.tolist(),
                precipitation=precip.tolist(),
                snowfall=snowfall.tolist(),
                wind_speed_10m=wind_speed.tolist(),
                wind_direction_10m=wind_dir.tolist(),
                sunshine_duration=sun.tolist(),
            )
        )
    return out
