"""Einstiegspunkt (CLI) fuer das Swiss Snow Model.

Beispiele
---------
    # Reproduzierbare Offline-Demo (synthetisches DEM + Wetter):
    python main.py --offline --plot

    # Echter Lauf mit Open-Meteo + eigenem swissALTI3D-GeoTIFF:
    python main.py --dem /pfad/swissalti3d_davos.tif --hours 24 --plot
"""
from __future__ import annotations

import argparse
from pathlib import Path

from config.settings import OUTPUT_DIR, AOI, RunConfig
from pipeline.run_pipeline import run


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Swiss Snow Model - Neuschneeraster")
    ap.add_argument("--dem", type=str, default=None, help="Pfad zu DEM-GeoTIFF (optional)")
    ap.add_argument("--real-dem", action="store_true",
                    help="Echtes Copernicus-DEM (30 m) ueber HTTP laden statt synthetisch")
    ap.add_argument("--hours", type=int, default=24, help="Akkumulationsfenster [h]")
    ap.add_argument("--date", type=str, default=None,
                    help="Zieldatum YYYY-MM-DD (sonst aktueller Forecast)")
    ap.add_argument("--offline", action="store_true", help="Synthetisches Wetter (kein API-Call)")
    ap.add_argument("--step-deg", type=float, default=0.03, help="Wetter-Gitterweite [Grad]")
    ap.add_argument("--idw-power", type=float, default=2.0, help="IDW-Exponent")
    ap.add_argument("--plot", action="store_true", help="Diagnose-Panel als PNG erzeugen")
    ap.add_argument("--overlay", action="store_true",
                    help="10-m-Overlay (WGS84-GeoTIFF + PNG + Leaflet-Karte) erzeugen")
    ap.add_argument("--no-html", action="store_true", help="Overlay ohne folium-HTML")
    ap.add_argument("--seed", type=int, default=42)
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    config = RunConfig(
        aoi=AOI(),
        forecast_hours=args.hours,
        date=args.date,
        weather_grid_step_deg=args.step_deg,
        idw_power=args.idw_power,
        dem_path=Path(args.dem) if args.dem else None,
        dem_source=("synthetic" if (args.offline and not args.real_dem) else "copernicus"),
        use_synthetic_weather=args.offline,
        random_seed=args.seed,
    )
    result = run(config)

    if args.plot:
        from viz.visualize import plot_layers

        png = plot_layers(
            result.layers,
            (config.aoi.east_min, config.aoi.north_min,
             config.aoi.east_max, config.aoi.north_max),
            OUTPUT_DIR / f"new_snow_{config.aoi.name.lower()}.png",
        )
        print(f"[OUT] Karte  : {png}")

    if args.overlay:
        from pipeline.overlay_export import export_overlay

        dem = result.dem
        ov = export_overlay(
            new_snow=result.layers["new_snow_cm"],
            transform=dem.transform,
            src_crs=dem.crs,
            bounds=dem.bounds,
            res=dem.res,
            out_dir=OUTPUT_DIR,
            tag=config.aoi.name.lower(),
            make_html=not args.no_html,
        )
        print(f"[OUT] Overlay-GeoTIFF (WGS84): {ov.geotiff_wgs84}")
        print(f"[OUT] Overlay-PNG (10 m)     : {ov.png}")
        if ov.html:
            print(f"[OUT] Leaflet-Karte          : {ov.html}")


if __name__ == "__main__":
    main()
