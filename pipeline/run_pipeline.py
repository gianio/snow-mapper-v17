"""Pipeline-Orchestrierung.

Verbindet die Bausteine in fester Reihenfolge und enthaelt selbst KEINE
mathematische Modelllogik:

    1. DEM laden (echt oder synthetisch) -> Terrain-Features berechnen
    2. Wetter holen (Open-Meteo oder synthetisch) -> auf Fenster aggregieren
    3. Wetterpunkte auf das DEM-Raster interpolieren
    4. Kernmodell flaechig ausfuehren
    5. GeoTIFF + CSV exportieren
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

import numpy as np

from config.settings import (
    ARCHIVE_THRESHOLD_DAYS,
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_MODEL,
    OPEN_METEO_URL,
    OUTPUT_DIR,
    RunConfig,
    load_model_params,
)
from data_connectors.dem_loader import DEM, load_dem_geotiff, synthetic_dem
from data_connectors.open_meteo_client import OpenMeteoClient
from data_connectors.synthetic_weather import synthetic_forecast
from model.aggregation import aggregate_window
from model.raster_engine import (
    build_grid_coordinates,
    interpolate_weather,
    run_raster_model,
)
from model.terrain_features import compute_terrain_features
from pipeline.geo_utils import weather_sample_grid
from pipeline.io_export import write_csv_summary, write_geotiff


@dataclass
class PipelineResult:
    layers: Dict[str, np.ndarray]
    dem: DEM
    grid_x: np.ndarray
    grid_y: np.ndarray
    geotiff_path: Path
    csv_path: Path


def run(config: RunConfig) -> PipelineResult:
    """Fuehrt die komplette Pipeline aus und gibt die Resultatschichten zurueck."""
    params = load_model_params()
    aoi = config.aoi
    bounds = (aoi.east_min, aoi.north_min, aoi.east_max, aoi.north_max)

    # --- 1. DEM + Terrain ------------------------------------------------- #
    if config.dem_path is not None and Path(config.dem_path).exists():
        dem = load_dem_geotiff(str(config.dem_path), bounds, aoi.crs, aoi.resolution)
        print(f"[DEM] GeoTIFF geladen: {config.dem_path}")
    elif config.dem_source == "copernicus":
        from data_connectors.copernicus_dem_loader import load_copernicus_dem

        print("[DEM] Lade echtes Copernicus-DEM (30 m) ueber HTTP ...")
        dem = load_copernicus_dem(bounds, aoi.resolution, aoi.crs)
        print("[DEM] Copernicus-DEM geladen.")
    else:
        dem = synthetic_dem(bounds, aoi.resolution, aoi.crs, seed=config.random_seed)
        print("[DEM] Synthetisches Hoehenmodell (Demo) verwendet.")
    print(f"[DEM] Raster {dem.elevation.shape}, {dem.res} m, CRS {dem.crs}")

    terrain = compute_terrain_features(dem.elevation, dem.res)

    # --- 2. Wetter -------------------------------------------------------- #
    lats, lons, points_xy = weather_sample_grid(
        bounds, aoi.crs, config.weather_grid_step_deg
    )
    print(f"[WX] {len(lats)} Wetter-Abfragepunkte.")

    if config.use_synthetic_weather:
        # Punkthoehen aus dem DEM ableiten (naechste Zelle), damit die
        # synthetische Quelle hoehenkonsistente Temperaturen liefert.
        elevations = _sample_elevations(dem, points_xy)
        forecasts = synthetic_forecast(
            lats, lons, elevations, hours=config.forecast_hours, seed=config.random_seed
        )
        print("[WX] Synthetisches Wetter (Offline-Modus).")
    else:
        start_date, end_date = _date_window(config.date, config.forecast_hours)
        base_url, use_archive = _select_endpoint(config.date)
        client = OpenMeteoClient(
            base_url, model=OPEN_METEO_MODEL, send_model=True
        )
        forecasts = client.fetch(
            lats, lons, start_date=start_date, end_date=end_date
        )
        src = "Archiv (ERA5)" if use_archive else f"Forecast ({OPEN_METEO_MODEL})"
        when = f"{start_date}..{end_date}" if start_date else "aktueller Forecast"
        print(f"[WX] Open-Meteo {src} geladen, {when}.")

    summaries = aggregate_window(forecasts, config.forecast_hours)

    # --- 3. Interpolation auf das Raster ---------------------------------- #
    grid_x, grid_y = build_grid_coordinates(bounds, dem.res, dem.elevation.shape)
    weather_grid = interpolate_weather(
        summaries, points_xy, grid_x, grid_y, power=config.idw_power
    )

    # --- 4. Modell -------------------------------------------------------- #
    layers = run_raster_model(terrain, weather_grid, params)
    # Terrain-Schichten fuer Diagnose/Export anhaengen.
    layers.update(
        {
            "elevation": terrain.elevation,
            "slope_deg": np.degrees(terrain.slope_rad),
            "aspect_deg": terrain.aspect_deg,
        }
    )

    # --- 5. Export -------------------------------------------------------- #
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = aoi.name.lower()
    geotiff_path = write_geotiff(
        layers["new_snow_cm"], dem.transform, dem.crs, OUTPUT_DIR / f"new_snow_{tag}.tif"
    )
    csv_path = write_csv_summary(
        {k: layers[k] for k in ("new_snow_cm", "elevation", "slope_deg", "snow_fraction")},
        grid_x,
        grid_y,
        OUTPUT_DIR / f"new_snow_{tag}_summary.csv",
    )
    print(f"[OUT] GeoTIFF: {geotiff_path}")
    print(f"[OUT] CSV    : {csv_path}")
    _print_stats(layers["new_snow_cm"])

    return PipelineResult(
        layers=layers,
        dem=dem,
        grid_x=grid_x,
        grid_y=grid_y,
        geotiff_path=geotiff_path,
        csv_path=csv_path,
    )


def _select_endpoint(date: str | None) -> tuple[str, bool]:
    """Waehlt Forecast- oder Archiv-Endpoint je nach Alter des Datums.

    Der Forecast-Endpoint deckt nur ~92 Tage Vergangenheit bis ~16 Tage Zukunft
    ab; aeltere Daten kommen aus dem ERA5-Archiv.

    Returns
    -------
    (base_url, use_archive)
    """
    if not date:
        return OPEN_METEO_URL, False
    from datetime import date as date_cls
    from datetime import datetime

    target = datetime.strptime(date, "%Y-%m-%d").date()
    age_days = (date_cls.today() - target).days
    if age_days > ARCHIVE_THRESHOLD_DAYS:
        return OPEN_METEO_ARCHIVE_URL, True
    return OPEN_METEO_URL, False


def _date_window(date: str | None, hours: int) -> tuple[str | None, str | None]:
    """Leitet (start_date, end_date) fuer Open-Meteo aus Datum + Fenster ab.

    24h -> ein Tag; 72h -> drei Tage (start..start+2). Ohne Datum None
    (dann nutzt der Client den aktuellen Forecast ab heute).
    """
    if not date:
        return None, None
    from datetime import datetime, timedelta

    start = datetime.strptime(date, "%Y-%m-%d").date()
    days = max(1, hours // 24)
    end = start + timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _sample_elevations(dem: DEM, points_xy: np.ndarray) -> list[float]:
    """Liest DEM-Hoehen an den (Ost, Nord)-Punkten (naechste Zelle)."""
    east_min, north_min, east_max, north_max = dem.bounds
    h, w = dem.elevation.shape
    out = []
    for ex, ny in points_xy:
        col = int(np.clip((ex - east_min) / dem.res, 0, w - 1))
        row = int(np.clip((north_max - ny) / dem.res, 0, h - 1))
        out.append(float(dem.elevation[row, col]))
    return out


def _print_stats(arr: np.ndarray) -> None:
    finite = arr[np.isfinite(arr)]
    if finite.size:
        print(
            f"[STAT] Neuschnee cm  min={finite.min():.1f}  "
            f"mean={finite.mean():.1f}  max={finite.max():.1f}"
        )
