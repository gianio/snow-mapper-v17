"""Neuschnee fuer den ganzen Alpenraum (AT, IT, FR, DE, SI) -- nur Neuschnee.

Die eigentliche Pipeline rechnet ein Rechteck um die Schweiz mit allen Ebenen.
Ausserhalb davon zeigt die App nur den Neuschnee, und den aus diesem Modul:
dasselbe Neuschnee-Modell (compute_new_snow) wie in pipeline/interactive.py,
aber grob (ca. 3 km) und in 6-Stunden-Schritten, damit die Datei klein bleibt.

Ausgabe: <out_dir>/data/alps_ns.json
  {"v":1, "bounds":[lat_min,lon_min,lat_max,lon_max], "nx":..,"ny":..,
   "t0":"YYYY-MM-DDTHH:MM", "step_h":6, "n":..,"scale":0.5,
   "gz":"<base64 gzip uint8, n*ny*nx, Zeile 0 = Norden>"}
Ein Wert v bedeutet v*scale cm Neuschnee in diesem 6-h-Schritt.

Fehlschlaege (Open-Meteo-Limit, DEM nicht erreichbar) sind nie fatal: der
Deploy-Schritt laeuft mit continue-on-error und die App zeigt dann einfach
nur das Schweizer Rechteck.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
from pathlib import Path

import numpy as np

from config.settings import OPEN_METEO_MODEL, OPEN_METEO_URL, load_model_params
from model.interpolation import idw_direction_grid, idw_grid
from model.snow_model import WeatherGrid, compute_new_snow
from model.terrain_features import compute_terrain_features
from pipeline.interactive import (
    _elevation_on_grid, _vector_dir, _weather_points,
)

# Westalpen (Nizza/Ecrins) bis Ostalpen (Wien/Graz), Jura bis Apennin-Rand
ALPS_BBOX = {"lon_min": 5.0, "lat_min": 43.6, "lon_max": 16.4, "lat_max": 48.4}
GRID_DEG = 0.03          # ~3 km
WEATHER_DEG = 0.4        # Wetterpunkte (~35 km, ~390 Punkte): 0.3 deg (~660) lief
                         # im Deploy ins 12-min-Limit (Open-Meteo-Drosselung)
STEP_H = 6
SCALE = 0.5              # cm pro Einheit, uint8 -> bis 127 cm je 6 h


def build(out_dir: Path, past_days: int = 5, forecast_days: int = 5,
          synthetic: bool = False) -> Path:
    params = load_model_params()
    b = ALPS_BBOX
    lons = np.arange(b["lon_min"], b["lon_max"], GRID_DEG)
    lats = np.arange(b["lat_min"], b["lat_max"], GRID_DEG)
    glon, glat = np.meshgrid(lons, lats)
    glat = glat[::-1]                      # Zeile 0 = Norden
    ny, nx = glon.shape
    print(f"[ALPS] Raster {nx} x {ny} ({GRID_DEG} deg)")

    print("[ALPS] DEM ...", flush=True)
    elev = _elevation_on_grid(glon, glat, synthetic)
    elev = np.where(np.isfinite(elev), elev, 0.0)   # Meer/Luecken: Tiefland
    terrain = compute_terrain_features(elev, GRID_DEG * 111320.0 * 0.7)

    wlats, wlons = _weather_points(b, WEATHER_DEG)
    pts = np.column_stack([wlons, wlats])
    if synthetic:
        from data_connectors.synthetic_weather import synthetic_forecast
        from pipeline.interactive import _elevation_at_points
        fc = synthetic_forecast(wlats, wlons, _elevation_at_points(wlons, wlats, True),
                                hours=(past_days + forecast_days) * 24)
    else:
        from data_connectors.open_meteo_client import OpenMeteoClient
        cl = OpenMeteoClient(OPEN_METEO_URL, model=OPEN_METEO_MODEL, pause_between_chunks_s=2.0)
        print(f"[ALPS] Open-Meteo: {len(wlats)} Punkte ...", flush=True)
        fc = cl.fetch(wlats, wlons, past_days=past_days, forecast_days=forecast_days)
    print(f"[ALPS] Wetter: {len(fc)} Punkte")

    H = min(len(f.time) for f in fc)
    prec = np.array([f.precipitation[:H] for f in fc])
    snow = np.array([f.snowfall[:H] for f in fc])
    temp = np.array([f.temperature_2m[:H] for f in fc])
    wspd = np.array([f.wind_speed_10m[:H] for f in fc])
    wdir = np.array([f.wind_direction_10m[:H] for f in fc])
    pelev = np.array([f.elevation for f in fc])
    times = fc[0].time[:H]
    ref = idw_grid(pts, pelev, glon, glat, power=2.0)

    n = H // STEP_H
    out = np.zeros((n, ny, nx), dtype=np.uint8)
    for k in range(n):
        sl = slice(k * STEP_H, (k + 1) * STEP_H)
        wg = WeatherGrid(
            precipitation_mm=idw_grid(pts, np.nan_to_num(prec[:, sl]).sum(axis=1), glon, glat),
            snowfall_cm=idw_grid(pts, np.nan_to_num(snow[:, sl]).sum(axis=1), glon, glat),
            temperature_c_ref=idw_grid(pts, np.nanmean(temp[:, sl], axis=1), glon, glat),
            ref_elevation=ref,
            wind_speed_ms=idw_grid(pts, np.nanmean(wspd[:, sl], axis=1), glon, glat),
            wind_direction_deg=idw_direction_grid(pts, _vector_dir(wdir[:, sl], wspd[:, sl]), glon, glat),
        )
        inc = np.nan_to_num(compute_new_snow(terrain, wg, params)["new_snow_cm"])
        out[k] = np.clip(np.round(np.clip(inc, 0, None) / SCALE), 0, 255).astype(np.uint8)

    lat_max, lat_min = float(glat[0, 0]), float(glat[-1, 0])
    lon_min, lon_max = float(glon[0, 0]), float(glon[0, -1])
    meta = {"v": 1, "bounds": [lat_min - GRID_DEG / 2, lon_min - GRID_DEG / 2,
                               lat_max + GRID_DEG / 2, lon_max + GRID_DEG / 2],
            "nx": nx, "ny": ny, "t0": str(times[0])[:16], "step_h": STEP_H, "n": n,
            "scale": SCALE, "src": "Open-Meteo + Snowmapper-Neuschneemodell",
            "gz": base64.b64encode(gzip.compress(out.tobytes(), 9)).decode()}
    path = Path(out_dir) / "data" / "alps_ns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, separators=(",", ":")))
    print(f"[ALPS] {path} ({path.stat().st_size // 1024} KB, {n} x {STEP_H} h ab {meta['t0']})")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", default="dist")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--past-days", type=int, default=5)
    ap.add_argument("--forecast-days", type=int, default=5)
    a = ap.parse_args()
    build(Path(a.out_dir), a.past_days, a.forecast_days, a.synthetic)


if __name__ == "__main__":
    main()
