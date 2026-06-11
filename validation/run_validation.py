"""Validierungslauf gegen SLF-Stationen (optionaler Bonus).

Fuehrt die Pipeline aus, tastet den modellierten Neuschnee an den SLF-
Stationskoordinaten ab, berechnet Bias/RMSE/MAE und einen robusten
multiplikativen Bias-Korrekturfaktor.

Beispiel:
    python -m validation.run_validation --offline \
        --stations data/slf_stations_example.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from pyproj import Transformer

from config.settings import AOI, RunConfig
from data_connectors.slf_loader import load_slf_stations
from pipeline.run_pipeline import run
from validation.validate_slf import sample_model_at_stations, validate


def main() -> None:
    ap = argparse.ArgumentParser(description="SLF-Validierung")
    ap.add_argument("--stations", required=True, help="CSV mit SLF-Stationen")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--dem", type=str, default=None)
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    config = RunConfig(
        aoi=AOI(),
        forecast_hours=args.hours,
        dem_path=Path(args.dem) if args.dem else None,
        use_synthetic_weather=args.offline,
    )
    result = run(config)
    aoi = config.aoi

    stations = load_slf_stations(args.stations)
    tf = Transformer.from_crs("EPSG:4326", aoi.crs, always_xy=True)
    ex, ny = tf.transform(stations["longitude"].values, stations["latitude"].values)
    stations_xy = np.column_stack([ex, ny])

    modeled = sample_model_at_stations(
        result.layers["new_snow_cm"],
        (aoi.east_min, aoi.north_min, aoi.east_max, aoi.north_max),
        aoi.resolution,
        stations_xy,
    )
    vr = validate(modeled, stations["new_snow_cm"].values, stations["station_id"].values)

    print("\n=== SLF-Validierung ===")
    print(vr.table.to_string(index=False))
    print(f"\nn={vr.n}  Bias={vr.bias_cm:+.1f} cm  RMSE={vr.rmse_cm:.1f} cm  "
          f"MAE={vr.mae_cm:.1f} cm")
    print(f"Empfohlener Bias-Korrekturfaktor (Median Messung/Modell): "
          f"{vr.correction_factor:.3f}")
    print("-> Anwendung: new_snow_corrected = new_snow * correction_factor")


if __name__ == "__main__":
    main()
