"""Zentrale Konfiguration: Pfade, Region (AOI), Modellparameter.

Enthaelt KEINE Modell- oder Datenlogik, nur Konfigurationswerte und das
Einlesen von ``model_params.yaml`` in typisierte Datenklassen.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml

# --------------------------------------------------------------------------- #
# Pfade
# --------------------------------------------------------------------------- #
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
CONFIG_DIR: Path = PROJECT_ROOT / "config"
OUTPUT_DIR: Path = PROJECT_ROOT / "outputs"
DATA_DIR: Path = PROJECT_ROOT / "data"
MODEL_PARAMS_FILE: Path = CONFIG_DIR / "model_params.yaml"

# --------------------------------------------------------------------------- #
# Geografische Referenz
# --------------------------------------------------------------------------- #
# Bounding Box der Schweiz in WGS84 (EPSG:4326) - nur als Leitplanke.
SWITZERLAND_BBOX_WGS84: Dict[str, float] = {
    "lon_min": 5.9,
    "lat_min": 45.8,
    "lon_max": 10.5,
    "lat_max": 47.9,
}

# Landesweite Ausdehnung in LV95 (EPSG:2056, Meter) - deckt die ganze Schweiz ab.
SWITZERLAND_BBOX_LV95: Dict[str, float] = {
    "east_min": 2485000.0,
    "north_min": 1075000.0,
    "east_max": 2834000.0,
    "north_max": 1296000.0,
}

# Default-Aufloesung fuer landesweite Demo-Laeufe [m]. 10 m fuer die ganze
# Schweiz = ~771 Mio. Zellen und erfordert echtes DEM + Tiling (siehe docs).
NATIONAL_PREVIEW_RES_M: float = 200.0
# Schwelle, ab der der Schweiz-Orchestrator kachelt statt ein Einzelraster zu rechnen.
MAX_SINGLE_GRID_CELLS: int = 15_000_000
# Kachelgroesse fuer gekachelte Laeufe [m].
TILE_SIZE_M: float = 20000.0

# Open-Meteo Endpoint (kein API-Key noetig fuer nicht-kommerzielle Nutzung).
OPEN_METEO_URL: str = os.environ.get(
    "OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast"
)
# Modellwahl: "best_match" oder z.B. "meteoswiss_icon_ch1" / "icon_d2".
OPEN_METEO_MODEL: str = os.environ.get("OPEN_METEO_MODEL", "best_match")
# Archiv-Endpoint (ERA5) fuer historische Daten ausserhalb des Forecast-Fensters.
OPEN_METEO_ARCHIVE_URL: str = os.environ.get(
    "OPEN_METEO_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive"
)
# Ab dieser Anzahl Tagen in der Vergangenheit wird automatisch das Archiv genutzt.
ARCHIVE_THRESHOLD_DAYS: int = 80


@dataclass(frozen=True)
class AOI:
    """Area of Interest im DEM-Koordinatensystem.

    Standard: ein 14x14 km Kachelausschnitt um Davos (GR) in LV95 (EPSG:2056),
    da das Modell die ganze Schweiz auf 10 m nicht in einem Rutsch rechnen kann
    (~770 Mio. Zellen). Fuer flaechendeckende Laeufe wird gekachelt.
    """

    name: str = "Davos"
    crs: str = "EPSG:2056"          # LV95, Einheit Meter
    east_min: float = 2778000.0
    north_min: float = 1180000.0
    east_max: float = 2792000.0
    north_max: float = 1194000.0
    resolution: float = 10.0        # Rasterweite in Metern

    @property
    def width(self) -> int:
        return int(round((self.east_max - self.east_min) / self.resolution))

    @property
    def height(self) -> int:
        return int(round((self.north_max - self.north_min) / self.resolution))

    @property
    def n_cells(self) -> int:
        return self.width * self.height


@dataclass
class RunConfig:
    """Laufzeit-Konfiguration fuer eine einzelne Pipeline-Ausfuehrung."""

    aoi: AOI = field(default_factory=AOI)
    # Zeitfenster der Akkumulation in Stunden (24 oder 72).
    forecast_hours: int = 24
    # Zieldatum (YYYY-MM-DD, UTC). None -> aktueller Forecast (ab heute).
    date: str | None = None
    # Aufloesung des Wetter-Sample-Gitters in Grad (~0.03 deg ~ 3 km).
    weather_grid_step_deg: float = 0.03
    # IDW-Parameter fuer Wetter -> DEM-Raster.
    idw_power: float = 2.0
    # DEM-Quelle: Pfad zu lokalem GeoTIFF; None -> dem_source entscheidet.
    dem_path: Path | None = None
    # "synthetic" (Demo, ohne Download) oder "copernicus" (echtes 30-m-DEM via HTTP).
    dem_source: str = "synthetic"
    # Offline-Modus: synthetisches Wetter statt API-Call (reproduzierbare Demo).
    use_synthetic_weather: bool = False
    random_seed: int = 42


def national_aoi(resolution: float = NATIONAL_PREVIEW_RES_M) -> AOI:
    """AOI ueber die gesamte Schweiz (LV95) bei gegebener Aufloesung."""
    b = SWITZERLAND_BBOX_LV95
    return AOI(
        name="switzerland",
        crs="EPSG:2056",
        east_min=b["east_min"],
        north_min=b["north_min"],
        east_max=b["east_max"],
        north_max=b["north_max"],
        resolution=resolution,
    )


def load_model_params(path: Path | None = None) -> Dict[str, Any]:
    """Liest die Modellparameter aus YAML.

    Returns
    -------
    dict
        Verschachteltes Dictionary mit allen Koeffizienten.
    """
    path = path or MODEL_PARAMS_FILE
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
