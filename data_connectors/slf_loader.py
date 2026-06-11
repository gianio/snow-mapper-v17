"""SLF-/IMIS-Stationsloader (optional, nur fuer Bias-Kalibrierung).

REINER Datenzugriff: liest gemessene Neuschnee-/Schneehoehenwerte des
SLF-Messnetzes (IMIS-Stationen) aus einer lokalen CSV oder JSON. Diese Daten
fliessen NICHT direkt ins Raster ein, sondern dienen ausschliesslich der
nachgelagerten Bias-Korrektur und Validierung (siehe validation/).

Erwartete Spalten (CSV) bzw. Keys (JSON):
    station_id, longitude, latitude, elevation, new_snow_cm
``new_snow_cm`` ist der gemessene Neuschnee im selben Zeitfenster wie das Modell.

Offizielle Quellen siehe docs/README.md (SLF Open Data / measurement-api.slf.ch).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = ["station_id", "longitude", "latitude", "elevation", "new_snow_cm"]


def load_slf_stations(path: str | Path) -> pd.DataFrame:
    """Liest SLF-Stationsmessungen aus CSV oder JSON.

    Returns
    -------
    pandas.DataFrame
        Mit garantierten Spalten ``REQUIRED_COLUMNS``.
    """
    path = Path(path)
    if path.suffix.lower() == ".json":
        df = pd.read_json(path)
    else:
        df = pd.read_csv(path)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"SLF-Datei fehlen Spalten: {missing}")
    return df[REQUIRED_COLUMNS].copy()
