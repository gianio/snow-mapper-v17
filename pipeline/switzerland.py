"""Landesweiter Orchestrator: Neuschnee fuer die GANZE Schweiz.

Strategie (automatisch gewaehlt):
    - Grobe Aufloesung (z.B. 200 m Preview): EIN Raster ueber die ganze Schweiz.
    - Feine Aufloesung (Richtung 10 m): KACHELUNG ueber die Landesflaeche, je
      Kachel ein Pipeline-Lauf, anschliessend Mosaik. Echte 10-m-Laeufe
      erfordern ein landesweites DEM (z.B. swissALTI3D) via ``dem_path``.

Erzeugt am Ende immer die Overlay-Ebene (WGS84-GeoTIFF + PNG + Leaflet-Karte),
die sich ueber die Schweizer Karte legt.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import numpy as np

from config.settings import (
    MAX_SINGLE_GRID_CELLS,
    OUTPUT_DIR,
    SWITZERLAND_BBOX_LV95,
    TILE_SIZE_M,
    AOI,
    RunConfig,
    national_aoi,
)
from pipeline.overlay_export import OverlayResult, export_overlay
from pipeline.run_pipeline import run


@dataclass
class SwitzerlandResult:
    geotiff_lv95: Path
    overlay: OverlayResult
    resolution_m: float
    n_cells: int
    tiled: bool


def run_switzerland(
    date: str | None,
    window_hours: int,
    resolution_m: float = 200.0,
    use_synthetic_weather: bool = False,
    dem_path: Path | None = None,
    dem_source: str = "synthetic",
    tile_size_m: float = TILE_SIZE_M,
    weather_step_deg: float = 0.2,
    make_html: bool = True,
) -> SwitzerlandResult:
    """Berechnet den Neuschnee fuer die ganze Schweiz und exportiert das Overlay."""
    aoi = national_aoi(resolution_m)
    n_cells = aoi.n_cells
    tiled = n_cells > MAX_SINGLE_GRID_CELLS

    print(f"=== Schweiz-Lauf | {date or 'aktueller Forecast'} | {window_hours}h "
          f"| {resolution_m:.0f} m | {aoi.width}x{aoi.height} = {n_cells:,} Zellen "
          f"| DEM: {'GeoTIFF' if dem_path else dem_source} ===")

    if not tiled:
        new_snow, transform, crs, bounds = _run_single(
            aoi, date, window_hours, weather_step_deg, dem_path, dem_source,
            use_synthetic_weather
        )
    else:
        print(f"[CH] Zu gross fuer Einzelraster -> Kachelung ({tile_size_m/1000:.0f} km).")
        new_snow, transform, crs, bounds = _run_tiled(
            resolution_m, date, window_hours, weather_step_deg, dem_path, dem_source,
            use_synthetic_weather, tile_size_m
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    geotiff_lv95 = OUTPUT_DIR / f"new_snow_switzerland_{window_hours}h.tif"
    from pipeline.io_export import write_geotiff
    write_geotiff(new_snow, transform, crs, geotiff_lv95)

    overlay = export_overlay(
        new_snow=new_snow,
        transform=transform,
        src_crs=crs,
        bounds=bounds,
        res=resolution_m,
        out_dir=OUTPUT_DIR,
        tag=f"switzerland_{window_hours}h",
        make_html=make_html,
    )
    print(f"[OUT] LV95-GeoTIFF : {geotiff_lv95}")
    print(f"[OUT] WGS84-GeoTIFF: {overlay.geotiff_wgs84}")
    print(f"[OUT] Overlay-PNG  : {overlay.png}")
    if overlay.html:
        print(f"[OUT] Leaflet-Karte: {overlay.html}")

    return SwitzerlandResult(
        geotiff_lv95=geotiff_lv95,
        overlay=overlay,
        resolution_m=resolution_m,
        n_cells=n_cells,
        tiled=tiled,
    )


def _run_single(aoi, date, window_hours, weather_step_deg, dem_path, dem_source, offline):
    config = RunConfig(
        aoi=aoi,
        forecast_hours=window_hours,
        date=date,
        weather_grid_step_deg=weather_step_deg,
        dem_path=dem_path,
        dem_source=dem_source,
        use_synthetic_weather=offline,
    )
    result = run(config)
    dem = result.dem
    return result.layers["new_snow_cm"], dem.transform, dem.crs, dem.bounds


def _run_tiled(
    resolution_m, date, window_hours, weather_step_deg, dem_path, dem_source,
    offline, tile_size_m
):
    import rasterio
    from rasterio.merge import merge as rio_merge

    b = SWITZERLAND_BBOX_LV95
    tiles = _tile_bounds(
        (b["east_min"], b["north_min"], b["east_max"], b["north_max"]), tile_size_m
    )
    print(f"[CH] {len(tiles)} Kacheln.")

    tile_paths: List[Path] = []
    tmp_dir = OUTPUT_DIR / "_tiles"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for idx, (emin, nmin, emax, nmax) in enumerate(tiles):
        tile_aoi = AOI(
            name=f"ch_tile_{idx:04d}",
            crs="EPSG:2056",
            east_min=emin, north_min=nmin, east_max=emax, north_max=nmax,
            resolution=resolution_m,
        )
        config = RunConfig(
            aoi=tile_aoi,
            forecast_hours=window_hours,
            date=date,
            weather_grid_step_deg=weather_step_deg,
            dem_path=dem_path,
            dem_source=dem_source,
            use_synthetic_weather=offline,
        )
        print(f"[CH] Kachel {idx + 1}/{len(tiles)} ...")
        result = run(config)
        from pipeline.io_export import write_geotiff
        tp = tmp_dir / f"{tile_aoi.name}.tif"
        write_geotiff(result.layers["new_snow_cm"], result.dem.transform,
                      result.dem.crs, tp)
        tile_paths.append(tp)

    datasets = [rasterio.open(p) for p in tile_paths]
    mosaic, out_transform = rio_merge(datasets, nodata=-9999.0)
    crs = datasets[0].crs
    for ds in datasets:
        ds.close()

    new_snow = mosaic[0]
    h, w = new_snow.shape
    from rasterio.transform import array_bounds
    left, bottom, right, top = array_bounds(h, w, out_transform)
    return new_snow, out_transform, str(crs), (left, bottom, right, top)


def _tile_bounds(
    national: Tuple[float, float, float, float], tile: float
) -> List[Tuple[float, float, float, float]]:
    """Zerlegt die Landesflaeche in Kacheln (letzte Reihe/Spalte beschnitten)."""
    emin, nmin, emax, nmax = national
    out = []
    e = emin
    while e < emax:
        n = nmin
        e2 = min(e + tile, emax)
        while n < nmax:
            n2 = min(n + tile, nmax)
            out.append((e, n, e2, n2))
            n = n2
        e = e2
    return out
