"""Interaktiver Export (wepowder-Stil), Vollausbau v2.

Aenderungen ggue. v1:
- Hangneigung & Schummerung: hochaufgeloeste swisstopo-WMTS-Layer
  (ch.swisstopo.hangneigung-ueber_30 [Klassen], ...reliefschattierung_monodirektional).
- Exposition & Rauigkeit: fein gerechnet (~250 m) und als scharfe PNG-Overlays.
- Wind: dichter (3 km) + topografische Exposition (Ridge schneller, Mulde/Lee
  langsamer); Sub-Layer "Windschwach/Lee" (<10 km/h konsistent).
- Temperatur: zusaetzliche Modi "Stunden<0" und "Max 0-5 degC"; Klassen-Isolinien.
- Sonne: Summe der Sonnenstunden ueber das Fenster.
- Stations-Fix (Layer-Gruppen werden zur Karte hinzugefuegt; Schneemarker mit
  HS-Fallback); Hover-Legende ueber den Layer-Buttons.
"""
from __future__ import annotations

import base64
import io
import json
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.transform import array_bounds, from_origin
from pyproj import Transformer
from scipy.spatial import cKDTree

from config.settings import (
    OPEN_METEO_MODEL, OPEN_METEO_URL, OUTPUT_DIR, SWITZERLAND_BBOX_LV95,
    AOI, load_model_params,
)
from data_connectors.open_meteo_client import OpenMeteoClient
from data_connectors.synthetic_weather import synthetic_forecast
from model.snow_model import WeatherGrid, compute_new_snow
from model.terrain_features import compute_terrain_features, roughness
from model.raster_engine import build_grid_coordinates
from pipeline.geo_utils import weather_sample_grid
from pipeline.overlay_export import SLF_BOUNDS, SLF_COLORS

_SNOW_SCALE = 0.2
_TEMP_OFF, _TEMP_MUL = 60.0, 2.0
_SPD_MUL = 5.0
_DIR_DIV = 2.0
_SUN_MUL = 100.0
_PREC_MUL = 5.0
_ELEV_SCALE = 20.0
_WIND_STEP = 9000.0
_FINE_RES = 60.0        # swissALTIRegio (feiner; speicher-sorgsam verarbeitet)
_PNG_W = 2000
_RAD_RES = 1000.0
_RAD_K = 12
_ROUGH_GRID_SCALE = 5.0


def _idw_weights(points_xy, targets_xy, k=12, power=2.0, eps=1e-6):
    tree = cKDTree(points_xy)
    k = int(min(k, len(points_xy)))
    dist, idx = tree.query(targets_xy, k=k)
    if k == 1:
        dist, idx = dist[:, None], idx[:, None]
    w = 1.0 / np.power(dist + eps, power)
    w /= w.sum(axis=1, keepdims=True)
    return idx, w


def _apply(v, idx, w):
    return (w * v[idx]).sum(axis=1)


def _hourly(fc, hours):
    T = min(hours, min(len(f.time) for f in fc))
    g = lambda a: np.array([getattr(f, a)[:T] for f in fc], dtype="float64")
    return (fc[0].time[:T], g("temperature_2m"), g("precipitation"), g("snowfall"),
            g("wind_speed_10m"), g("wind_direction_10m"), g("sunshine_duration"),
            np.array([f.elevation for f in fc], dtype="float64"))


def _reproj_frame(arr, src_t, src_crs, dst_t, dh, dw, rs=Resampling.bilinear):
    out = np.zeros((dh, dw), dtype="float32")
    reproject(source=arr.astype("float32"), destination=out, src_transform=src_t,
              src_crs=src_crs, dst_transform=dst_t, dst_crs="EPSG:4326", resampling=rs)
    return out


def _reproj_cube(cube, src_t, src_crs, dst_t, dh, dw):
    out = np.zeros((cube.shape[0], dh, dw), dtype="float32")
    for t in range(cube.shape[0]):
        out[t] = _reproj_frame(cube[t], src_t, src_crs, dst_t, dh, dw)
    return out


_ASPECT_COLORS = {
    0: (0x9E, 0x9E, 0x9E, 178),  # flat  grey
    1: (0x4A, 0x90, 0xD9, 255),  # N     blue
    2: (0x66, 0xBB, 0x6A, 255),  # E     green
    3: (0xEF, 0x53, 0x50, 255),  # S     red/orange
    4: (0xFF, 0xC1, 0x07, 255),  # W     yellow
}


def _aspect_classify(aspect_deg, slope_deg):
    """Classify aspect into integer quadrant index (0=flat,1=N,2=E,3=S,4=W).

    Done BEFORE reprojection so nearest-neighbor on the integer index
    is guaranteed to produce no blending.
    """
    cls = np.zeros(aspect_deg.shape, dtype="uint8")
    a = aspect_deg % 360
    flat = slope_deg < 5.0
    cls[(~flat) & ((a >= 315) | (a < 45))] = 1   # N
    cls[(~flat) & (a >= 45) & (a < 135)] = 2      # E
    cls[(~flat) & (a >= 135) & (a < 225)] = 3     # S
    cls[(~flat) & (a >= 225) & (a < 315)] = 4     # W
    return cls


def _class_to_png_b64(cls_lv95, src_t, src_crs, bounds, png_w=3000):
    """Reproject integer class raster -> WGS84, then map to RGBA PNG."""
    h, w = cls_lv95.shape
    dst_t, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h, *bounds)
    scale = max(1.0, dw / png_w)
    dw2, dh2 = int(dw / scale), int(dh / scale)
    dst_t2 = from_origin(dst_t.c, dst_t.f, (dst_t.a * dw) / dw2, (-dst_t.e * dh) / dh2)
    cls_wgs = np.zeros((dh2, dw2), dtype="uint8")
    reproject(source=cls_lv95, destination=cls_wgs, src_transform=src_t,
              src_crs=src_crs, dst_transform=dst_t2, dst_crs="EPSG:4326",
              resampling=Resampling.nearest)
    rgba = np.zeros((dh2, dw2, 4), dtype="uint8")
    for idx, (r, g, b, a) in _ASPECT_COLORS.items():
        mask = cls_wgs == idx
        rgba[mask] = [r, g, b, a]
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    left, bottom, right, top = array_bounds(dh2, dw2, dst_t2)
    return base64.b64encode(buf.getvalue()).decode(), (bottom, left, top, right)


def _rough_rgba(rough, vmax):
    x = np.clip(rough / max(1e-6, vmax), 0, 1)
    r = 0.55 + 0.35 * x
    g = 0.5 - 0.35 * x
    b = 0.45 - 0.4 * x
    a = np.where(x > 0.06, np.clip(0.25 + x, 0, 0.9), 0.0)
    return np.dstack([np.clip(r, 0, 1), np.clip(g, 0, 1), np.clip(b, 0, 1), a])


def _rgba_to_png_b64(rgba_lv95, src_t, src_crs, bounds, resampling=Resampling.bilinear, png_w=None):
    """RGBA (LV95) -> WGS84 reprojizieren -> PNG (b64) + lat/lon-Bounds."""
    png_w = png_w or _PNG_W
    h, w = rgba_lv95.shape[:2]
    dst_t, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h, *bounds)
    scale = max(1.0, dw / png_w)
    dw2, dh2 = int(dw / scale), int(dh / scale)
    dst_t2 = from_origin(dst_t.c, dst_t.f, (dst_t.a * dw) / dw2, (-dst_t.e * dh) / dh2)
    bands = []
    for k in range(4):
        out = np.zeros((dh2, dw2), "float32")
        reproject(source=rgba_lv95[:, :, k].copy(), destination=out, src_transform=src_t,
                  src_crs=src_crs, dst_transform=dst_t2, dst_crs="EPSG:4326",
                  resampling=resampling)
        bands.append(out)
    rgba = np.clip(np.dstack(bands) * 255, 0, 255).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    left, bottom, right, top = array_bounds(dh2, dw2, dst_t2)
    return base64.b64encode(buf.getvalue()).decode(), (bottom, left, top, right)


class _DEM:
    def __init__(self, elevation, transform, res, bounds, crs):
        self.elevation, self.transform, self.res = elevation, transform, res
        self.bounds, self.crs = bounds, crs


def _load_altiregio(bounds, res):
    """swissALTIRegio (nationales swisstopo-10-m-DEM, EPSG:2056) dekimiert lesen."""
    from rasterio.windows import from_bounds as win_from_bounds
    url = ("/vsicurl/https://data.geo.admin.ch/ch.swisstopo.swissaltiregio/"
           "swissaltiregio/swissaltiregio_2056_5728.tif")
    e0, n0, e1, n1 = bounds
    ow, oh = int((e1 - e0) / res), int((n1 - n0) / res)
    import rasterio
    with rasterio.open(url) as ds:
        win = win_from_bounds(e0, n0, e1, n1, ds.transform)
        arr = ds.read(1, window=win, out_shape=(oh, ow),
                      resampling=Resampling.average, boundless=True, fill_value=np.nan)
    arr = np.where(arr < -100, np.nan, arr).astype("float32")
    return _DEM(arr, from_origin(e0, n1, res, res), res, bounds, "EPSG:2056")


def _corrected_aspect(z, res):
    """Aspect [deg, clockwise from N] + slope [deg] using Horn's method.

    Horn (1981) 3x3 weighted gradient — same algorithm as GDAL gdaldem.
    Convention: N=0, E=90, S=180, W=270 (downslope direction).
    """
    zf = np.where(np.isnan(z), np.nanmean(z), z).astype("float64")
    # Horn's 3x3 kernel
    a = zf[:-2, :-2]; b = zf[:-2, 1:-1]; c = zf[:-2, 2:]
    d = zf[1:-1, :-2];                    f = zf[1:-1, 2:]
    g = zf[2:,   :-2]; h = zf[2:,  1:-1]; i = zf[2:,  2:]
    dz_dx = ((c + 2*f + i) - (a + 2*d + g)) / (8 * res)
    dz_dy = ((g + 2*h + i) - (a + 2*b + c)) / (8 * res)
    # Pad to original shape
    gx = np.pad(dz_dx, 1, mode='edge')
    gy = np.pad(dz_dy, 1, mode='edge')
    slope = np.degrees(np.arctan(np.hypot(gx, gy))).astype("float32")
    aspect = (np.degrees(np.arctan2(-gx, gy)) % 360.0).astype("float32")
    aspect = np.where(slope < 1.5, np.nan, aspect)
    return aspect, slope


def _fine_terrain(bounds, aoi, use_synthetic):
    """Feines Terrain (swissALTIRegio): Exposition-PNG (hochaufgeloest) +
    Rauigkeit-PNG + Wind-Expositionsindex. Speicher-sorgsam (Aspect zuerst
    rechnen & freigeben, Rauigkeit/TPI aus dezimiertem DEM).
    """
    if use_synthetic:
        from data_connectors.dem_loader import synthetic_dem
        dem = synthetic_dem(bounds, _FINE_RES, aoi.crs)
    else:
        print(f"[INT] swissALTIRegio ({_FINE_RES:.0f} m) fuer Exposition ...")
        dem = _load_altiregio(bounds, _FINE_RES)
    transform, res = dem.transform, dem.res

    # --- Exposition: classify into integer quadrant, then reproject index ---
    aspect_deg, slope_deg = _corrected_aspect(dem.elevation, res)
    aspect_cls = _aspect_classify(np.nan_to_num(aspect_deg), slope_deg)
    del aspect_deg, slope_deg
    aspect_png, png_b = _class_to_png_b64(aspect_cls, transform, aoi.crs, bounds, png_w=5000)
    del aspect_cls

    # --- Rauigkeit & TPI aus dezimiertem DEM (4x groeber) ---
    zc = np.nan_to_num(dem.elevation, nan=float(np.nanmean(dem.elevation)))[::4, ::4]
    del dem
    rough = roughness(zc)
    vmax = float(np.nanpercentile(rough, 98))
    ct = from_origin(bounds[0], bounds[3], res * 4, res * 4)
    rough_png, _ = _rgba_to_png_b64(_rough_rgba(rough, vmax), ct, aoi.crs, bounds, png_w=1400)
    del rough

    from scipy.ndimage import uniform_filter
    radc = max(3, int(2000.0 / (res * 4)))
    tpi = zc - uniform_filter(zc, size=2 * radc + 1, mode="nearest")
    tpi_n = np.clip(tpi / 120.0, -1.0, 1.0)
    e0, n0, e1, n1 = bounds
    hh, ww = zc.shape
    cres = res * 4

    def expo_at(xy):
        col = np.clip(((xy[:, 0] - e0) / cres).astype(int), 0, ww - 1)
        row = np.clip(((n1 - xy[:, 1]) / cres).astype(int), 0, hh - 1)
        t = tpi_n[row, col]
        return 1.0 + 0.45 * t, t

    return {"aspect_png": aspect_png, "rough_png": rough_png, "png_bounds": png_b,
            "expo_at": expo_at}


def _radiation_inputs(bounds, aoi, use_synthetic):
    """Terrain-Inputs fuer das (im Browser gerechnete) Solarmodell:
    Hangneigung, Exposition und Horizont-Hoehenwinkel je Azimutsektor
    (Geländeschattierung durch umliegende Berge). Alles auf WGS84-Gitter.
    """
    if use_synthetic:
        from data_connectors.dem_loader import synthetic_dem
        dem = synthetic_dem(bounds, _RAD_RES, aoi.crs)
    else:
        from data_connectors.copernicus_dem_loader import load_copernicus_dem
        print(f"[INT] DEM ({_RAD_RES:.0f} m) fuer Strahlungsmodell ...")
        dem = load_copernicus_dem(bounds, _RAD_RES, aoi.crs)
    terr = compute_terrain_features(dem.elevation, dem.res)
    slope = np.degrees(terr.slope_rad)
    aspect = terr.aspect_deg
    z = np.nan_to_num(dem.elevation, nan=float(np.nanmean(dem.elevation)))
    res = dem.res
    rows, cols = z.shape

    # Horizont-Hoehenwinkel je Azimut (Schattenwurf umliegender Berge).
    from scipy.ndimage import map_coordinates
    yy, xx = np.mgrid[0:rows, 0:cols].astype("float64")
    horizon = np.zeros((_RAD_K, rows, cols), "float32")
    dists = list(range(1, 26))  # bis ~ 25 Zellen entfernt
    for a in range(_RAD_K):
        az = 2 * np.pi * a / _RAD_K          # 0=N, im Uhrzeigersinn
        ddx, ddy = np.sin(az), -np.cos(az)   # Norden = Zeilen aufwaerts
        maxslope = np.zeros((rows, cols), "float64")
        for d in dists:
            zs = map_coordinates(z, [yy + ddy * d, xx + ddx * d], order=1, mode="nearest")
            maxslope = np.maximum(maxslope, (zs - z) / (d * res))
        horizon[a] = np.degrees(np.arctan(maxslope))

    dst_t, dw, dh = calculate_default_transform(aoi.crs, "EPSG:4326", cols, rows, *bounds)
    slope_w = _reproj_frame(slope, dem.transform, aoi.crs, dst_t, dh, dw)
    aspect_w = _reproj_frame(aspect, dem.transform, aoi.crs, dst_t, dh, dw, rs=Resampling.nearest)
    hor_w = np.zeros((_RAD_K, dh, dw), "float32")
    for a in range(_RAD_K):
        hor_w[a] = _reproj_frame(horizon[a], dem.transform, aoi.crs, dst_t, dh, dw)
    left, bottom, right, top = array_bounds(dh, dw, dst_t)
    return {"slope": slope_w, "aspect": aspect_w, "horizon": hor_w,
            "width": dw, "height": dh, "K": _RAD_K, "bounds": (bottom, left, top, right)}


def build_interactive_data(center_date, days_each_side, resolution_m, use_synthetic,
                           weather_step_deg, n_stations=40):
    params = load_model_params()
    b = SWITZERLAND_BBOX_LV95
    aoi = AOI(name="switzerland", crs="EPSG:2056", east_min=b["east_min"],
              north_min=b["north_min"], east_max=b["east_max"], north_max=b["north_max"],
              resolution=resolution_m)
    bounds = (aoi.east_min, aoi.north_min, aoi.east_max, aoi.north_max)

    if use_synthetic:
        from data_connectors.dem_loader import synthetic_dem
        dem = synthetic_dem(bounds, resolution_m, aoi.crs)
    else:
        from data_connectors.copernicus_dem_loader import load_copernicus_dem
        print(f"[INT] DEM (Copernicus, {resolution_m:.0f} m) ...")
        dem = load_copernicus_dem(bounds, resolution_m, aoi.crs)
    terrain = compute_terrain_features(dem.elevation, dem.res)
    grid_x, grid_y = build_grid_coordinates(bounds, dem.res, dem.elevation.shape)
    shape = dem.elevation.shape
    targets = np.column_stack([grid_x.ravel(), grid_y.ravel()])

    lats, lons, pts = weather_sample_grid(bounds, aoi.crs, weather_step_deg)
    horizon = (2 * days_each_side + 1) * 24
    if use_synthetic:
        elevs = _sample(dem, pts, shape)
        fc = synthetic_forecast(lats, lons, elevs, hours=horizon)
        print("[INT] Synthetisches Wetter.")
    else:
        if center_date:
            from pipeline.run_pipeline import _select_endpoint
            url, arch = _select_endpoint(center_date)
            c = datetime.strptime(center_date, "%Y-%m-%d").date()
            cl = OpenMeteoClient(url, model=OPEN_METEO_MODEL)
            fc = cl.fetch(lats, lons,
                          start_date=(c - timedelta(days=days_each_side)).isoformat(),
                          end_date=(c + timedelta(days=days_each_side)).isoformat())
            print(f"[INT] Open-Meteo {'Archiv' if arch else 'Forecast'}, {len(lats)} Punkte.")
        else:
            cl = OpenMeteoClient(OPEN_METEO_URL, model=OPEN_METEO_MODEL)
            fc = cl.fetch(lats, lons, past_days=days_each_side, forecast_days=days_each_side + 1)
            print(f"[INT] Open-Meteo Forecast (heute +/-{days_each_side}d), {len(lats)} Punkte.")

    times, temp_m, prec_m, snow_m, wspd_m, wdir_m, sun_m, elev_pt = _hourly(fc, horizon)
    T = len(times)
    idx, w = _idw_weights(pts, targets)
    ref_elev = _apply(elev_pt, idx, w).reshape(shape)
    sin_d, cos_d = np.sin(np.radians(wdir_m)), np.cos(np.radians(wdir_m))

    fine = _fine_terrain(bounds, aoi, use_synthetic)

    wind = _point_grid(aoi, _WIND_STEP)
    p_idx, p_w = _idw_weights(pts, wind["xy"])
    expo_mult, expo_tpi = fine["expo_at"](wind["xy"])
    P = len(wind["lat"])

    print(f"[INT] Modelliere {T} Stunden ...")
    snow_c = np.zeros((T, *shape), "float32")
    temp_c = np.zeros((T, *shape), "float32")
    sun_c = np.zeros((T, *shape), "float32")
    wind_c = np.zeros((T, *shape), "float32")
    prec_c = np.zeros((T, *shape), "float32")
    p_spd = np.zeros((T, P), "float32")
    p_dir = np.zeros((T, P), "float32")
    lapse = params["altitude"]["temp_lapse_k_per_m"]
    for t in range(T):
        temp_g = _apply(temp_m[:, t], idx, w).reshape(shape)
        ws_g = _apply(wspd_m[:, t], idx, w).reshape(shape)
        prec_g = _apply(prec_m[:, t], idx, w).reshape(shape)
        wg = WeatherGrid(
            precipitation_mm=prec_g,
            snowfall_cm=_apply(snow_m[:, t], idx, w).reshape(shape),
            temperature_c_ref=temp_g, ref_elevation=ref_elev,
            wind_speed_ms=ws_g,
            wind_direction_deg=(np.degrees(np.arctan2(
                _apply(sin_d[:, t], idx, w), _apply(cos_d[:, t], idx, w))) % 360).reshape(shape))
        snow_c[t] = compute_new_snow(terrain, wg, params)["new_snow_cm"]
        temp_c[t] = temp_g + lapse * (terrain.elevation - ref_elev)
        sun_c[t] = np.clip(_apply(sun_m[:, t], idx, w).reshape(shape) / 3600.0, 0, 1)
        wind_c[t] = ws_g
        prec_c[t] = prec_g
        p_spd[t] = _apply(wspd_m[:, t], p_idx, p_w) * expo_mult  # topografisch moduliert
        p_dir[t] = np.degrees(np.arctan2(_apply(sin_d[:, t], p_idx, p_w),
                                         _apply(cos_d[:, t], p_idx, p_w))) % 360

    dst_t, dw, dh = calculate_default_transform(aoi.crs, "EPSG:4326", shape[1], shape[0], *bounds)
    snow_w = _reproj_cube(snow_c, dem.transform, aoi.crs, dst_t, dh, dw)
    temp_w = _reproj_cube(temp_c, dem.transform, aoi.crs, dst_t, dh, dw)
    sun_w = _reproj_cube(sun_c, dem.transform, aoi.crs, dst_t, dh, dw)
    wind_w = _reproj_cube(wind_c, dem.transform, aoi.crs, dst_t, dh, dw)
    prec_w = _reproj_cube(prec_c, dem.transform, aoi.crs, dst_t, dh, dw)
    aspect_main = _reproj_frame(terrain.aspect_deg.astype("float32"),
                                dem.transform, aoi.crs, dst_t, dh, dw,
                                rs=Resampling.nearest)
    slope_main = _reproj_frame(np.degrees(terrain.slope_rad).astype("float32"),
                               dem.transform, aoi.crs, dst_t, dh, dw)
    elev_main = _reproj_frame(np.nan_to_num(terrain.elevation, nan=0).astype("float32"),
                              dem.transform, aoi.crs, dst_t, dh, dw)
    rough_main = roughness(np.nan_to_num(dem.elevation, nan=0).astype("float64"))
    rough_w = _reproj_frame(rough_main.astype("float32"),
                            dem.transform, aoi.crs, dst_t, dh, dw)
    hourly_snow = [float(np.mean(snow_w[t])) for t in range(T)]
    left, bottom, right, top = array_bounds(dh, dw, dst_t)

    rad = _radiation_inputs(bounds, aoi, use_synthetic)

    stations = []
    if not use_synthetic:
        stations = _slf_stations(times, n_stations, days_each_side)

    return {
        "times": times, "T": T, "width": dw, "height": dh,
        "bounds": (bottom, left, top, right), "today_index": _today_idx(times),
        "snow": snow_w, "temp": temp_w, "sun": sun_w, "wind_grid": wind_w,
        "prec": prec_w, "main_aspect": aspect_main, "main_slope": slope_main,
        "main_elev": elev_main,
        "wind": {"lat": wind["lat"], "lon": wind["lon"], "nx": wind["nx"], "ny": wind["ny"],
                 "spd": p_spd, "dir": p_dir, "tpi": expo_tpi},
        "aspect_png": fine["aspect_png"], "rough_png": fine["rough_png"],
        "png_bounds": fine["png_bounds"], "rad": rad, "stations": stations,
        "rough_grid": rough_w, "hourly_snow": hourly_snow,
    }


def _point_grid(aoi, step):
    xs = np.arange(aoi.east_min + step / 2, aoi.east_max, step)
    ys = np.arange(aoi.north_min + step / 2, aoi.north_max, step)
    X, Y = np.meshgrid(xs, ys)
    xy = np.column_stack([X.ravel(), Y.ravel()])
    tf = Transformer.from_crs(aoi.crs, "EPSG:4326", always_xy=True)
    lon, lat = tf.transform(xy[:, 0], xy[:, 1])
    return {"lat": np.asarray(lat), "lon": np.asarray(lon), "xy": xy,
            "nx": len(xs), "ny": len(ys)}


def _sample(dem, pts, shape):
    e0, n0, e1, n1 = dem.bounds
    h, wdt = shape
    out = []
    for ex, ny in pts:
        col = min(wdt - 1, max(0, int((ex - e0) / dem.res)))
        row = min(h - 1, max(0, int((n1 - ny) / dem.res)))
        out.append(float(dem.elevation[row, col]))
    return out


def _slf_stations(times, n_stations, period_days):
    from data_connectors.slf_stations import get_stations, attach_latest, fetch_hs_series
    print("[INT] SLF/IMIS-Stationen ...")
    try:
        stns = get_stations()
        attach_latest(stns)
    except Exception as e:
        print(f"[INT] SLF nicht erreichbar: {e!r}")
        return []
    # Schnee- und Windstationen GETRENNT waehlen, sonst verdraengen die hohen
    # Windstationen (ohne Schneesensor) die Schneestationen.
    snow = sorted([s for s in stns if s.hs_now is not None], key=lambda s: -s.elevation)
    snow = snow[: int(n_stations * 0.6)]
    snow_codes = {s.code for s in snow}
    wind = sorted([s for s in stns if s.vw is not None and s.code not in snow_codes],
                  key=lambda s: -s.elevation)
    wind = wind[: n_stations - len(snow)]
    sel = snow + wind
    keys = [t[:13] for t in times]
    out = []
    for s in sel:
        series = fetch_hs_series(s.code, max(2, period_days + 1)) if s.hs_now is not None else {}
        hs = [series.get(k) for k in keys]
        out.append({"code": s.code, "label": s.label, "lat": round(s.lat, 4),
                    "lon": round(s.lon, 4), "elev": round(s.elevation),
                    "ta": s.ta, "tss": s.tss, "hs_now": s.hs_now,
                    "vw": s.vw, "dw": s.dw,
                    "hs": hs if any(v is not None for v in hs) else None})
    nhs = sum(1 for o in out if o["hs"] is not None)
    nv = sum(1 for o in out if o["vw"] is not None)
    print(f"[INT] {len(out)} SLF-Stationen (HS-Reihe: {nhs}, Wind: {nv}).")
    return out


def _today_idx(times):
    today = date_cls.today().isoformat()
    for i, t in enumerate(times):
        if t[:10] == today:
            return i
    return max(0, len(times) // 2)


def _now_idx(times):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H")
    for i, t in enumerate(times):
        if t[:13] == now:
            return i
    return _today_idx(times)


def _build_meta_blobs(data):
    """Encode every raster cube to base64 and assemble the client meta dict.
    Returns (meta, blobs) where blobs maps the client key -> base64 string.
    Shared by the single-file and split exporters so the encoding lives once."""
    def u8(a, fn):
        return base64.b64encode(np.clip(fn(a), 0, 255).astype("uint8").tobytes()).decode()
    rad = data["rad"]
    blobs = {
        "SNOW": u8(data["snow"], lambda a: np.round(a / _SNOW_SCALE)),
        "TEMP": u8(data["temp"], lambda a: np.round((a + _TEMP_OFF) * _TEMP_MUL)),
        "SUN": u8(data["sun"], lambda a: np.round(a * _SUN_MUL)),
        "SPD": u8(data["wind"]["spd"], lambda a: np.round(a * _SPD_MUL)),
        "DIR": u8(data["wind"]["dir"], lambda a: np.round(a / _DIR_DIV)),
        "WINDG": u8(data["wind_grid"], lambda a: np.round(np.clip(a, 0, 51) * _SPD_MUL)),
        "PREC": u8(data["prec"], lambda a: np.round(a * _PREC_MUL)),
        "MASPECT": u8(data["main_aspect"], lambda a: np.round(a / _DIR_DIV)),
        "MSLOPE": u8(data["main_slope"], lambda a: np.round(np.clip(a, 0, 90))),
        "MELEV": u8(data["main_elev"], lambda a: np.round(np.clip(a / _ELEV_SCALE, 0, 255))),
        "ROUGHGRID": u8(data["rough_grid"], lambda a: np.round(np.clip(a / _ROUGH_GRID_SCALE, 0, 255))),
        "RSLOPE": u8(rad["slope"], lambda a: np.round(np.clip(a, 0, 90))),
        "RASPECT": u8(rad["aspect"], lambda a: np.round(a / _DIR_DIV)),
        "RHOR": u8(rad["horizon"], lambda a: np.round(np.clip(a, 0, 90))),
        "ASPECTPNG": data["aspect_png"],
        "ROUGHPNG": data["rough_png"],
    }
    meta = {
        "T": data["T"], "width": data["width"], "height": data["height"],
        "bounds": data["bounds"], "times": data["times"], "today_index": data["today_index"],
        "snow_scale": _SNOW_SCALE, "temp_off": _TEMP_OFF, "temp_mul": _TEMP_MUL,
        "spd_mul": _SPD_MUL, "dir_div": _DIR_DIV, "sun_mul": _SUN_MUL, "prec_mul": _PREC_MUL,
        "elev_scale": _ELEV_SCALE,
        "slf_bounds": SLF_BOUNDS, "slf_colors": SLF_COLORS,
        "png_bounds": data["png_bounds"],
        "rad": {"width": rad["width"], "height": rad["height"], "K": rad["K"],
                "bounds": rad["bounds"]},
        "wind": {"lat": [round(x, 4) for x in data["wind"]["lat"].tolist()],
                 "lon": [round(x, 4) for x in data["wind"]["lon"].tolist()],
                 "nx": data["wind"]["nx"], "ny": data["wind"]["ny"],
                 "tpi": [round(float(x), 2) for x in data["wind"]["tpi"].tolist()]},
        "stations": data["stations"],
        "hourly_snow": [round(x, 3) for x in data["hourly_snow"]],
        "now_index": _now_idx(data["times"]),
    }
    return meta, blobs


def _app_js() -> str:
    """The application script, extracted from the template between the
    /*__APP_START__*/ and /*__APP_END__*/ sentinels (used for the split build)."""
    return _HTML.split("/*__APP_START__*/", 1)[1].split("/*__APP_END__*/", 1)[0]


def export_interactive_html(data, out_html: Path) -> Path:
    """Single self-contained file with the data inlined (default, local use,
    openable via file://)."""
    meta, blobs = _build_meta_blobs(data)
    boot = "<script>window.__D=" + json.dumps({"meta": meta, "b": blobs}) + ";</script>"
    html = _HTML.replace("<!--__BOOT__-->", boot)
    out_html.write_text(html, encoding="utf-8")
    _write_pwa_assets(out_html.parent)
    return out_html


def export_split_app(data, out_dir: Path) -> Path:
    """App-shell + external data blob + latest.json pointer. The shell/binary is
    stable and small; forecast data updates independently (for GitHub Pages and
    the iOS app). Must be served over HTTP(S) — not file:// (fetch is blocked)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ddir = out_dir / "data"; ddir.mkdir(exist_ok=True)
    meta, blobs = _build_meta_blobs(data)
    stamp = datetime.now().strftime("%Y%m%d%H%M")
    dataname = "snowdata-%s.json" % stamp
    (ddir / dataname).write_text(json.dumps({"meta": meta, "b": blobs}), encoding="utf-8")
    (ddir / "latest.json").write_text(json.dumps(
        {"version": stamp, "data": dataname,
         "generated": datetime.now().replace(microsecond=0).isoformat() + "Z"}),
        encoding="utf-8")
    # keep the last few snapshots for rollback, prune older ones
    for old in sorted(ddir.glob("snowdata-*.json"))[:-4]:
        try:
            old.unlink()
        except OSError:
            pass
    (out_dir / "app.js").write_text(_app_js(), encoding="utf-8")
    shell = _HTML.split('<script id="appjs">', 1)[0].replace("<!--__BOOT__-->", _BOOT_SPLIT) + "</body></html>"
    (out_dir / "index.html").write_text(shell, encoding="utf-8")
    _write_pwa_assets(out_dir)
    return out_dir / "index.html"


# Async loader for the split build: fetch latest.json -> data blob (with a real
# progress bar on the intro), set window.__D, then inject the app script.
_BOOT_SPLIT = r"""<script>
(function(){
  function bar(){return document.querySelector('#intro .bar');}
  function prog(f){var b=bar();if(b){b.style.animation='none';b.style.transformOrigin='left';b.style.transform='scaleX('+Math.max(0.03,Math.min(1,f))+')';}}
  function fail(m){var i=document.getElementById('intro');if(!i)return;
    var s=i.querySelector('.sub');if(s)s.textContent=m;
    if(!i.querySelector('.retry')){var b=document.createElement('button');b.className='retry';b.textContent='Erneut versuchen';
      b.style.cssText='margin-top:14px;padding:11px 22px;border-radius:12px;border:1px solid rgba(0,0,0,.15);background:#0a0a0c;color:#fff;font:700 14px Inter,system-ui;cursor:pointer';
      b.onclick=function(){location.reload();};i.appendChild(b);}
    try{if(window.Sentry)window.Sentry.captureMessage(m);}catch(e){}}
  function fetchT(url,opts,ms){var c=('AbortController'in window)?new AbortController():null;
    if(c){opts=opts||{};opts.signal=c.signal;setTimeout(function(){c.abort();},ms||30000);}
    return fetch(url,opts);}
  // app.js parallel zum Datenblob laden (vorher: sequenziell danach)
  var appJsP=fetchT('app.js',{cache:'no-cache'},30000).then(function(r){if(!r.ok)throw new Error('app.js HTTP '+r.status);return r.text();});
  function waitLeaflet(){return new Promise(function(res,rej){var t0=Date.now();
    (function poll(){if(window.L)return res();if(Date.now()-t0>20000)return rej(new Error('Leaflet timeout'));setTimeout(poll,150);})();});}
  (async function(){
    try{
      var lj=await (await fetchT('data/latest.json',{cache:'no-cache'},15000)).json();
      var res=await fetchT('data/'+lj.data,{cache:'force-cache'},60000);
      if(!res.ok)throw new Error('HTTP '+res.status);
      var total=+(res.headers.get('Content-Length')||0),text;
      if(res.body&&total&&window.ReadableStream){
        var reader=res.body.getReader(),chunks=[],got=0,r;
        while(!(r=await reader.read()).done){chunks.push(r.value);got+=r.value.length;prog(got/total);}
        var buf=new Uint8Array(got),off=0;chunks.forEach(function(c){buf.set(c,off);off+=c.length;});
        text=new TextDecoder().decode(buf);
      }else{text=await res.text();prog(1);}
      window.__D=JSON.parse(text);text=null;
      var code=await appJsP;
      try{await waitLeaflet();}catch(e){fail('Karten-Bibliothek nicht erreichbar — Verbindung prüfen.');return;}
      await new Promise(function(res){var t0=Date.now();(function poll(){if(window.supabase||Date.now()-t0>4000)return res();setTimeout(poll,120);})();});
      var sc=document.createElement('script');sc.textContent=code;document.body.appendChild(sc);
      setTimeout(function(){if(!window.__APP_OK&&window.__introStuck)window.__introStuck('App-Start fehlgeschlagen — bitte erneut versuchen.');},6000);
    }catch(e){fail('Daten konnten nicht geladen werden — Verbindung prüfen.');try{if(window.Sentry)window.Sentry.captureException(e);}catch(_){}}
  })();
})();
</script>"""


def _snowflake_icon(size: int) -> Image.Image:
    """Simple branded PWA icon: navy rounded square + white 6-arm snowflake."""
    import math
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=rad, fill=(8, 8, 10, 255))
    cx = cy = size / 2
    arm = size * 0.34
    lw = max(2, int(size * 0.035))
    white = (255, 255, 255, 235)
    for k in range(6):
        a = math.radians(60 * k)
        ex, ey = cx + arm * math.cos(a), cy + arm * math.sin(a)
        d.line([cx, cy, ex, ey], fill=white, width=lw)
        # two side branches per arm
        for frac, blen in ((0.55, 0.16), (0.8, 0.12)):
            bx, by = cx + arm * frac * math.cos(a), cy + arm * frac * math.sin(a)
            for da in (math.radians(35), -math.radians(35)):
                d.line([bx, by,
                        bx + size * blen * math.cos(a + da),
                        by + size * blen * math.sin(a + da)],
                       fill=white, width=max(1, lw - 1))
    return img


def _write_pwa_assets(out_dir: Path) -> None:
    """Emit manifest, service worker and icons next to index.html so the app is
    installable (Add to Home Screen) and loads offline with the last data."""
    for px, name in [(180, "icon-180.png"), (192, "icon-192.png"), (512, "icon-512.png")]:
        try:
            _snowflake_icon(px).save(out_dir / name)
        except Exception:  # icon is cosmetic; never fail the build over it
            pass
    manifest = {
        "name": "Swiss Snow Model",
        "short_name": "SnowModel",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "description": "Interaktive Neuschnee-, Pulver- und Skitauglichkeits-Karte für die Schweiz.",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (out_dir / "manifest.webmanifest").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "sw.js").write_text(_SERVICE_WORKER, encoding="utf-8")


_SERVICE_WORKER = r"""// Swiss Snow Model — service worker (installable + offline last-data).
// Network-first for all same-origin GETs (shell, app.js, data blob, icons) so
// online users always get the latest, and offline falls back to the last cache.
const C='ssm-v6';
const CORE=['./','./index.html','./app.js','./manifest.webmanifest','./icon-192.png','./icon-512.png','./icon-180.png'];
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.addAll(CORE).catch(()=>{})));});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;
  const url=new URL(r.url);
  if(url.origin!==location.origin)return; // map tiles / Supabase / CDNs -> network
  // Content-stamped data blobs are immutable -> cache-first (repeat loads use 0 network),
  // and stale stamps are purged whenever a new one is stored.
  if(url.pathname.includes('/data/snowdata-')){
    e.respondWith(caches.match(r).then(m=>m||fetch(r).then(resp=>{
      const cp=resp.clone();
      caches.open(C).then(async c=>{const ks=await c.keys();
        ks.filter(k=>new URL(k.url).pathname.includes('/data/snowdata-')).forEach(k=>c.delete(k));
        c.put(r,cp);}).catch(()=>{});
      return resp;})));
    return;
  }
  const fresh=(r.mode==='navigate'||url.pathname.endsWith('/app.js')||url.pathname.endsWith('/index.html'));
  e.respondWith(
    fetch(r,fresh?{cache:'no-cache'}:undefined).then(resp=>{const cp=resp.clone();caches.open(C).then(c=>c.put(r,cp)).catch(()=>{});return resp;})
      .catch(()=>caches.match(r).then(m=>m||(r.mode==='navigate'?caches.match('./index.html'):undefined)))
  );
});
"""


_HTML = r"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1, user-scalable=no"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="theme-color" content="#ffffff"/>
<meta name="mobile-web-app-capable" content="yes"/>
<title>Swiss Snow Model</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAG8UlEQVR4nO2XW2xU1xWGv7X3uczFNhgDMXdMkxBABBFupcQBhKo2hEaKImibtEimShu1aslD26BKwXEfCC+VkkolLVWrVmmlJpaaCtK+JFCcmKQJSi9OioAgCCBibLDB2B57xnPOrvaeGdvjC+UhUl+6pJHnjNf+17//tfba68D/2GTyfzUqNqI+lSgtxNAU3/6C7a/oTyXwbWDKJL8Zlu67k+SUjUg0FxNXYFCY2IAYMIIxoj2xX01kf7ZmjLjlTjc1hKg+Yj4m6jlO2zPn/xsBobFRaGoyrH5hPyrxPXSQQGSY04ijDSKYbL7wHHoQx5gRl1FmIMoOSJQ7aCq6vs8mYhejCChlEjXviFj9/H5SM58md8NgiMbRtZ/IQCbHipWzMcbQ9s92SAagrSITblMTVIsMdB4wJ3Z/ZzgWwwQalSuS1c8tIpz6EaIMJhZEVAlRnBJg8jFzZlUSZvM8cP9Ct5E3Wy+QDTSXr/QhXqFuLbEREeyDikSUlxrKrOh/96k2GhsVTU2x5xxstdtKDdJbSKQVUSYPyhWNEsEgmCgCpZz8HoYffXsds6elXYbq753Fs786gXiCsasig/jariJ2ZaOE2GCCJFmVfRBo45irlLj8mAXUEWqwxWWpBUIcRZgoT830FGFKQ0Jz4XqGb+47Snd/luv9OZ7Yd4RLNzKQUIQpj5oZKbfGriVUw1j4gvhq3uiQXhkBX1daJ1dmdue5iDUrZ9FxpZcNK+cwOBRx6MhH+FOT5LqGOPTeRTybdzEkqgJyNwbYWl9HOunRcuIStbOqOHGyAwl1IQueoJVOD9lYM5e5HKlyAsZzBDzH1EkaBoo9O1exbd18pk1NEGVzqFC53a1aPIPPLrmDOI4Kv2Vz1FQn2LZ2Pnt2riYRWAyQIqZTVNujOqkCqiAVyrYuVODTerKdqrTPb3ZvYs70NF09Gf509AwNX13JtjXzXI088bVV/LL5H2z74j08vvEzLJtXTcNPW3jrZDuqMijUga13iz8m617hzyagCQIVOyejHHCcy3PP3TP4wtoFrN/7GiaG725dyq4ti6mbPYVdB4+Ty8f89sn7eWjFHM5dz7DrF60ufbu3LuN8zwAnL3SjQq9AIrAEZAICm1y/Bk/lnZMRpwCez6muXnb/+m2wzaZ3kCMn29n5ZD0rnznM779Vj6+Fh1/4K6//8PM8d+DfnG+/4XqCW5PyIe0T26NslbepiCZUoGiBNgWWalSLEoJkkvzAEOvWLuCPT21m6d7DbF5ay49fa2P53KmsWFDNir2HaKi/izPXerk5lEfSPvk4Lu93FjsqvxK88TVQkmlEqsgeYzE8/9ganv3zB5y92MXNfERndx+vn/oEHfpuYz9rOY2X9IlKta1H7daUCBRxtwPNYwkktFUBYsuy6OjuG8H2B5uWB+6ayc9nVNB5vc+mzAWJtOCFvnPPRzHoCS4+ewwt9i1TkNBCUuPaWbH1FkgIogIamt+jeecGLu5/lL+dv8obZztp/fgabR093OwddGRUqlhwo9eXCCRLmytJwDgC2hYNYndTAjBu55LyONWbYfmBN3hkyWy+saqOpgeXE2hFV3+Wv5xu5ydvn+VfHTeQwC+/C4bxfYj9WyhQERQ+ahSB0lVvBwJb0cbw6rkrvHq2nSmVSR6+8w72bVzC1+9byKPL57H+d6180NmD8rXzLTN7Y3JLAj5UWgLWcbw5JWyDqgrdc09seOnMZd7vHeDv29eT8jU77p1L2/E+dKrUgEalIG1xg0n6QBPWwWDBJRydgTKzSlgi2taFPd6BsGHhdNcPrF21gSxGInBDyogJpEPEhLdQoDIcdCkwEytAMXBkDPl8RKXvsee+RTy9eK7rnN25PC9fu45Up9zRLSNvv1eGSORn3fOMYzJBEQadVISGyKahBFBEsVVtDFEUobWmobaGH9TVcncq4f6djWO+/OE5riiDqkyMz799rAiN5PyrE6RgU0Grge43UTVCRVicYkbOkv1ixfvKtCqaFtQy30pctKtDeXacvsix/gy6KuUUGmcGIwktifzgWwNu0VUzQkDETpQKkXel9f1mU7doOx0dQ4XptzCNWcwKrZGET1NnN335wrjoifBO/wDnM4PoiuREwd1AxfSZfnD5Usv2w384erAQa/RMSHGkhikvvji193ObXzI10x8ytqO5fj48doPtdGNNKVRh6hozZ9vpWaGiiLCnp2V+24ePnX78kU+I3bw5ZioukRBx2lcde+dLuerqLQgLBZOKYzsmGfEQVx5uUC8Ss8N5ZNyZGB7htahYi8noyFxI9PUe66hfd8jehaUYk7+YmOLEMiHD8kUTvgaMhRuLPSr4ZPglZzXujeT2Yo7FLG2o/B3j/0bB/gOG6axjOF75uQAAAABJRU5ErkJggg=="/>
<link rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,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"/>
<link rel="preconnect" href="https://unpkg.com" crossorigin>
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script defer src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
<!-- maplibre-gl (3D, ~800 KB) wird erst beim ersten 3D-Klick geladen -->
<link rel="manifest" href="manifest.webmanifest"/>
<link rel="apple-touch-icon" href="icon-180.png"/>
<!-- Sentry error tracking: paste your browser DSN into SENTRY_DSN to enable. -->
<script>
(function(){
  function stuck(msg){var i=document.getElementById('intro');if(!i)return;
    var s=i.querySelector('.sub');if(s)s.textContent=msg;
    var f=i.querySelector('.flake');if(f)f.style.animationPlayState='paused';
    if(!i.querySelector('.retry')){var b=document.createElement('button');b.className='retry';b.textContent='Erneut versuchen';
      b.style.cssText='margin-top:14px;padding:11px 22px;border-radius:12px;border:1px solid rgba(0,0,0,.15);background:#0a0a0c;color:#fff;font:700 14px Inter,system-ui;cursor:pointer';
      b.onclick=function(){location.reload();};i.appendChild(b);}}
  window.__introStuck=stuck;
  window.addEventListener('error',function(e){
    if(document.getElementById('intro')&&!window.__APP_OK)
      stuck('Fehler beim Start: '+String((e&&e.message)||'unbekannt').slice(0,120));});
  setTimeout(function(){
    if(document.getElementById('intro')&&!window.__APP_OK)
      stuck('Der Start dauert ungewöhnlich lange — Verbindung prüfen.');},45000);
})();
</script>
<script>window.SENTRY_DSN='';(function(){if(!window.SENTRY_DSN)return;var s=document.createElement('script');s.src='https://browser.sentry-cdn.com/7.120.0/bundle.tracing.min.js';s.crossOrigin='anonymous';s.onload=function(){try{window.Sentry.init({dsn:window.SENTRY_DSN,tracesSampleRate:0,release:'snow-mapper'});}catch(e){}};document.head.appendChild(s);window.addEventListener('error',function(e){try{if(window.Sentry&&e.error)window.Sentry.captureException(e.error);}catch(_){}});window.addEventListener('unhandledrejection',function(e){try{if(window.Sentry)window.Sentry.captureException(e.reason);}catch(_){}});})();</script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
 :root{--fg:#16152e;--fg2:#39365e;--mut:#77749a;--acc:#141419;--acc2:#000;--bd:rgba(20,20,25,.10);--glass:rgba(255,255,255,.82);--glass2:rgba(248,251,255,.92);--glow:rgba(20,20,25,.10);--panel-h:52px;--r:14px;--r-lg:18px;
   /* design-system extensions (2026 refresh) */
   --ok:#1a9e6a;--warn:#d98a1f;--danger:#e0483c;--danger-tint:rgba(224,72,60,.12);
   --sp1:4px;--sp2:8px;--sp3:12px;--sp4:16px;--sp5:24px;--r-sm:10px;--r-xl:24px;
   --elev1:0 1px 3px rgba(22,21,46,.10);--elev2:0 6px 20px rgba(22,21,46,.13);--elev3:0 18px 46px rgba(22,21,46,.22);
   --blur:blur(24px) saturate(1.5);--dur:.22s;--ease:cubic-bezier(.4,0,.2,1);--ease-spring:cubic-bezier(.34,1.4,.64,1);
   --card:#ffffff;--fill:#f4f4f5;--fill2:#ebebed;--page:#fafafa;--ring:rgba(20,20,25,.30);--hair:rgba(22,21,46,.07)}
 /* Respect reduced-motion: kill non-essential animation/transition globally */
 @media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important;scroll-behavior:auto!important}}
 /* Higher-contrast borders/text for snow-glare / accessibility */
 @media (prefers-contrast:more){:root{--mut:#4a5a6e;--bd:rgba(20,20,25,.28);--hair:rgba(22,21,46,.18)}}
 /* Solid surfaces for users who reduce transparency (a11y) */
 @media (prefers-reduced-transparency:reduce){.feed-nav,.cmt-sheet,.prof-sheet,.loc-picker-sheet,.groups-sheet,.report-sheet,.auth-modal,#coachCard,#disc .sheet,.toast,.insp-panel{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;background:#fff!important}}
 /* --- Toast notifications (errors are no longer silent) --- */
 #toastWrap{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(env(safe-area-inset-bottom,0px) + 92px);z-index:9500;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;width:max-content;max-width:calc(100vw - 32px)}
 .toast{pointer-events:auto;display:flex;align-items:center;gap:9px;padding:11px 16px;border-radius:var(--r);font-size:13.5px;font-weight:600;color:var(--fg);background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border:1px solid rgba(255,255,255,.6);box-shadow:var(--elev2);max-width:100%;animation:toastIn .3s var(--ease-spring)}
 .toast.out{animation:toastOut .25s var(--ease) forwards}
 .toast .ic{width:18px;height:18px;flex-shrink:0}
 .toast.err{color:#7a1a12}.toast.err .ic{color:var(--danger)}
 .toast.ok{color:#0d5a3a}.toast.ok .ic{color:var(--ok)}
 @keyframes toastIn{from{opacity:0;transform:translateY(14px) scale(.96)}to{opacity:1;transform:translateY(0) scale(1)}}
 @keyframes toastOut{to{opacity:0;transform:translateY(8px) scale(.97)}}
 /* Inline icon glyphs (custom SVG set, replaces emoji) */
 .ic-i{width:1em;height:1em;display:inline-block;vertical-align:-0.125em;flex-shrink:0}
 /* Edge-to-edge: tinted glass band under the device status bar (safe-area),
    so the app visually reaches the physical screen edge like a native app.
    Height is 0 in normal browser windows; theme-color handles those. */
 #statusScrimB{position:fixed;bottom:0;left:0;right:0;height:env(safe-area-inset-bottom, 0px);z-index:2900;pointer-events:none;background:linear-gradient(0deg,var(--edge-tint,rgba(248,251,255,.92)),rgba(248,251,255,.4));backdrop-filter:blur(14px) saturate(1.5);-webkit-backdrop-filter:blur(14px) saturate(1.5)}
 #statusScrim{position:fixed;top:0;left:0;right:0;height:env(safe-area-inset-top,0px);z-index:2900;pointer-events:none;background:linear-gradient(180deg,var(--edge-tint,rgba(248,251,255,.92)),rgba(248,251,255,.55));backdrop-filter:blur(14px) saturate(1.5);-webkit-backdrop-filter:blur(14px) saturate(1.5)}
 /* --- Skeleton shimmer for loading feed/cards --- */
 .skel{position:relative;overflow:hidden;background:rgba(22,21,46,.06);border-radius:var(--r-sm)}
 .skel::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent);transform:translateX(-100%);animation:shimmer 1.3s infinite}
 @keyframes shimmer{100%{transform:translateX(100%)}}
 *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
 html{background:#ffffff}
 html,body{margin:0;padding:0;height:100%;height:100dvh;width:100%;overflow:hidden;font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--fg);overscroll-behavior:none;background:#ffffff;position:fixed;inset:0;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 #map{position:fixed;top:0;left:0;right:0;bottom:var(--btm-h,0px);background:#fff}
 .leaflet-control-scale{margin-right:92px!important;margin-bottom:10px!important}
 #flow{position:fixed;top:0;left:0;right:0;bottom:var(--btm-h,0px);z-index:450;pointer-events:none}
 #modeGlow{display:none}
 .rail-btn svg,#legendBtn svg{color:var(--topic-accent,#141419);transition:color .45s ease}
 /* --- Layer selector: one elegant floating glass card --- */
 #layerBar{position:absolute;z-index:1000;top:calc(env(safe-area-inset-top,0px) + 12px);left:12px;max-width:calc(100vw - 112px);display:inline-flex;flex-direction:column;padding:6px;background:rgba(255,255,255,.62);backdrop-filter:blur(24px) saturate(1.5);-webkit-backdrop-filter:blur(24px) saturate(1.5);border:1px solid rgba(255,255,255,.6);border-radius:20px;box-shadow:0 10px 34px rgba(22,21,46,.13),0 1px 0 rgba(255,255,255,.6) inset;--topic-accent:#141419;--topic-tint:rgba(20,20,25,.13)}
 #topics{display:flex;gap:2px;align-items:center;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
 #topics::-webkit-scrollbar{display:none}
 #topics button{display:inline-flex;align-items:center;gap:7px;border:none;background:transparent;border-radius:13px;padding:9px 14px;cursor:pointer;font-size:14px;font-weight:650;min-height:40px;color:var(--mut);transition:all .22s cubic-bezier(.4,0,.2,1);flex-shrink:0;white-space:nowrap;letter-spacing:-.01em;font-family:inherit}
 #topics button svg{width:18px;height:18px;flex-shrink:0;transition:color .2s}
 #topics button:hover{color:var(--fg)}
 #topics button.active{background:#fff;color:var(--fg);box-shadow:0 2px 9px rgba(22,21,46,.13),0 0 0 1px rgba(22,21,46,.04)}
 #topics button.active svg{color:var(--topic-accent)}
 #moreTopics{color:var(--mut);padding:9px 12px;gap:5px}
 #moreTopics .chev{width:13px!important;height:13px!important;transition:transform .25s;opacity:.65}
 #moreTopics.sel{background:#fff;color:var(--fg);box-shadow:0 2px 9px rgba(22,21,46,.13)}
 #moreTopics.sel>svg:first-child{color:var(--topic-accent)}
 #topics.expanded #moreTopics{background:rgba(22,21,46,.06);color:var(--fg)}
 #topics.expanded #moreTopics .chev{transform:rotate(180deg)}
 /* More dropdown: iconed, described (position set by JS) */
 #topicsMore{position:static;display:none;flex-direction:column;gap:1px;background:transparent;border:none;border-top:1px solid rgba(22,21,46,.08);border-radius:0;padding:6px 0 0;margin-top:6px;box-shadow:none;min-width:0;width:100%;animation:moreIn .18s cubic-bezier(.34,1.4,.64,1)}
 #topicsMore::-webkit-scrollbar{display:none}
 @keyframes moreIn{from{opacity:0;transform:translateY(-8px) scale(.97)}to{opacity:1;transform:translateY(0) scale(1)}}
 #layerBar.expanded #topicsMore{display:flex}
 #topicsMore button{display:flex;align-items:center;gap:12px;width:100%;justify-content:flex-start;background:transparent;border:none;border-radius:12px;padding:9px 10px;min-height:auto;cursor:pointer;font-family:inherit;text-align:left;transition:background .15s;color:var(--fg)}
 #topicsMore .mt-ic{width:36px;height:36px;border-radius:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
 #topicsMore .mt-ic svg{width:20px;height:20px}
 #topicsMore .mt-tx{display:flex;flex-direction:column;gap:1px;min-width:0}
 #topicsMore .mt-tx b{font-size:14px;font-weight:700;color:var(--fg);letter-spacing:-.01em}
 #topicsMore .mt-tx span{font-size:11.5px;color:var(--mut);font-weight:500;white-space:nowrap}
 #topicsMore button:hover{background:rgba(22,21,46,.045)}
 #topicsMore button.active{background:rgba(22,21,46,.06)}
 /* Sublayers: connected secondary segment row */
 #sublayers{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;margin-top:6px;padding-top:6px;border-top:1px solid rgba(22,21,46,.07)}
 #sublayers:empty{display:none}
 #sublayers::-webkit-scrollbar{display:none}
 #sublayers button{border:none;background:rgba(22,21,46,.05);border-radius:9px;padding:6px 12px;cursor:pointer;font-size:12.5px;font-weight:650;min-height:32px;color:var(--fg2);transition:all .18s;flex-shrink:0;white-space:nowrap;font-family:inherit;letter-spacing:-.01em}
 #sublayers button:hover{background:rgba(22,21,46,.09);color:var(--fg)}
 #sublayers button.active{background:var(--topic-tint);color:var(--topic-accent)}
 #bottomPanel{position:absolute;z-index:1000;bottom:0;left:0;right:0;
   background:rgba(255,255,255,.8);backdrop-filter:blur(22px) saturate(1.4);-webkit-backdrop-filter:blur(22px) saturate(1.4);border-top:1px solid rgba(255,255,255,.5);box-shadow:0 -1px 0 var(--bd),0 -4px 20px rgba(0,0,0,.06);transition:none;padding-bottom:max(env(safe-area-inset-bottom, 0px) - 18px, 4px);overflow:hidden}
 #btmMain{padding:6px 14px 2px}
 #timeline{display:block;border:1px solid var(--bd);background:rgba(237,242,248,.5);border-radius:var(--r)}
 #presets::-webkit-scrollbar{display:none}
 #presets.can-scroll{-webkit-mask-image:linear-gradient(90deg,#000 86%,transparent);mask-image:linear-gradient(90deg,#000 86%,transparent)}
 .winlbl{font-size:13px;font-weight:700;color:var(--fg);letter-spacing:-.01em}
 #tlHead{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:10px}
 #tlModeToggle{display:inline-flex;background:rgba(0,0,0,.05);border-radius:999px;padding:3px;flex-shrink:0}
 #tlModeToggle button{border:none;background:none;padding:5px 13px;border-radius:999px;font-size:12px;font-weight:600;color:var(--mut);cursor:pointer;font-family:inherit;transition:.15s}
 #tlModeToggle button.active{background:#fff;color:var(--fg);box-shadow:0 1px 3px rgba(0,0,0,.12)}
 #tlDetail{display:none}
 #bottomPanel.detail #tlDetail{display:block}
 #bottomPanel.collapsed #tlDetail{display:none}
 #bottomPanel.collapsed #tlModeToggle{opacity:.5;pointer-events:none}
 #tlExtended{margin-top:8px}
 #tlLen{width:100%;accent-color:var(--acc);margin:8px 0 2px;cursor:pointer}
 .tl-steprow{display:flex;gap:6px}
 .tl-step{border:1px solid var(--bd);background:rgba(255,255,255,.7);border-radius:10px;padding:8px 10px;font-size:12px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;transition:.15s;white-space:nowrap}
 .tl-step:hover{background:#fff;color:var(--fg)}
 #tlPrev,#tlNext,#tlNow{flex:1}
 .tl-len{flex-shrink:0}
 .tl-len b{color:var(--acc2)}
 .tl-range{margin-top:8px;font-size:11px;color:var(--mut);text-align:center;font-weight:500}
 .seg{display:flex;flex-wrap:wrap;gap:5px}
 .seg button{border:1.5px solid transparent;background:rgba(255,255,255,.6);border-radius:var(--r);padding:8px 14px;cursor:pointer;font-size:14px;font-weight:500;min-height:40px;color:var(--fg2);transition:all .2s cubic-bezier(.4,0,.2,1);flex-shrink:0}
 .seg button:hover{background:rgba(255,255,255,.95)}
 .seg button.active{background:var(--fg);color:#fff;border-color:var(--fg);font-weight:600;box-shadow:0 2px 8px rgba(22,21,46,.15)}
 #tlToggle{position:absolute;top:5px;left:50%;transform:translateX(-50%);width:32px;height:4px;border-radius:2px;background:rgba(0,0,0,.12);cursor:ns-resize;z-index:1;touch-action:none}
 .sec{margin-top:12px}
 .cap{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);margin-bottom:6px}
 .ck{display:flex;align-items:center;gap:9px;margin-top:12px;font-size:13px;cursor:pointer;color:var(--fg2)}
 .ck input{width:18px;height:18px;accent-color:var(--acc)}
 #three-wrap{position:absolute;inset:0;z-index:2000;display:none;background:#e8eef4}
 #three-wrap .maplibregl-canvas{outline:none}
 #three-wrap .maplibregl-map{width:100%;height:100%}
 #btn3dClose{position:absolute;top:calc(14px + env(safe-area-inset-top,0px));right:14px;z-index:2001;padding:10px 18px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);color:var(--fg);cursor:pointer;font-size:16px;font-weight:600;backdrop-filter:blur(10px)}
 #three-wrap .ctrl3d{position:absolute;bottom:calc(30px + env(safe-area-inset-bottom,0px));left:50%;transform:translateX(-50%);z-index:2001;display:flex;gap:10px;flex-wrap:wrap;justify-content:center;max-width:calc(100vw - 24px)}
 #three-wrap .ctrl3d button,#three-wrap .ctrl3d label,#three-wrap .ctrl3d select{padding:10px 16px;border-radius:14px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);cursor:pointer;font-size:16px;min-height:46px;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);touch-action:manipulation}
 #three-wrap .ctrl3d button:hover{border-color:var(--acc);color:var(--fg)}
 @media (max-width:560px){#three-wrap .ctrl3d{bottom:calc(16px + env(safe-area-inset-bottom,0px));gap:5px}#three-wrap .ctrl3d button,#three-wrap .ctrl3d label,#three-wrap .ctrl3d select{padding:10px 14px;font-size:15px;min-height:46px;border-radius:12px}#btn3dClose{top:calc(8px + env(safe-area-inset-top,0px));right:8px;padding:10px 18px;font-size:16px;border-radius:14px}}
 .sub{font-size:12px;color:var(--mut)}
 .asp-crisp img{image-rendering:pixelated;image-rendering:crisp-edges}
 .legend{position:absolute;z-index:950;bottom:calc(var(--btm-h,80px) + 40px);left:12px;background:rgba(255,255,255,.85);backdrop-filter:blur(16px) saturate(1.4);-webkit-backdrop-filter:blur(16px) saturate(1.4);border:1px solid rgba(255,255,255,.5);padding:10px 12px;border-radius:var(--r);box-shadow:0 2px 12px rgba(0,0,0,.08);font-size:12px;max-width:220px;line-height:1.5;color:var(--fg2);display:none}
 .legend.show{display:block}
 #legendBtn{position:absolute;z-index:960;bottom:var(--btm-h,80px);left:12px;width:40px;height:40px;border-radius:14px;border:1px solid rgba(255,255,255,.55);background:rgba(255,255,255,.72);backdrop-filter:blur(16px) saturate(1.4);-webkit-backdrop-filter:blur(16px) saturate(1.4);color:var(--fg2);cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.09);transition:all .18s cubic-bezier(.34,1.56,.64,1)}
 #legendBtn svg{width:20px;height:20px}
 #legendBtn:active{transform:scale(.92)}
 #legendBtn:hover,#legendBtn.active{color:var(--acc);background:rgba(255,255,255,.92)}
 .legend i{display:inline-block;width:12px;height:12px;margin-right:5px;vertical-align:-2px;border-radius:2px}
 .stn{background:rgba(255,255,255,.9);border:1.5px solid var(--acc);border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700;color:var(--acc2);text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.12);white-space:nowrap}
 .stn-dot{width:12px;height:12px;border-radius:50%;background:var(--acc);border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.25);cursor:pointer;transition:transform .12s}
 .stn-dot:hover{transform:scale(1.35)}
 .rpt-dot{width:14px;height:14px;border-radius:50%;border:2.5px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.3);cursor:pointer;transition:transform .12s}
 .rpt-dot:hover{transform:scale(1.35)}
 .scl{position:relative;width:110px;height:56px;font-size:10px;font-weight:700;text-align:center;pointer-events:none}
 .scl>div{position:absolute;left:0;right:0;white-space:nowrap}
 .scl .s-pill{display:inline-block;background:rgba(255,255,255,.8);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);border-radius:6px;padding:1px 5px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
 .scl .s-t{top:-2px;color:#0070b8}.scl .s-b{bottom:-2px;color:#d04040}
 .scl .s-row{top:17px;display:flex;align-items:center;justify-content:center;gap:4px}
 .scl .s-l{color:#4a6a8a}.scl .s-r{color:#2a8a4a}
 .scl .s-c{pointer-events:auto;background:rgba(255,255,255,.9);border:2px solid var(--acc);border-radius:10px;color:var(--acc2);padding:1px 7px;font-size:14px;font-weight:800;box-shadow:0 1px 6px rgba(0,0,0,.12),0 0 0 3px var(--glow);cursor:pointer;letter-spacing:.02em}
 .scard{font:15px system-ui;line-height:1.6;min-width:180px;color:var(--fg)}
 .scard b{font-size:16px}
 .scard .g{display:grid;grid-template-columns:auto auto;gap:3px 16px;margin-top:6px}
 .scard .k{color:var(--mut)}
 .wn{font-size:10px;color:#2a4a6a;font-weight:700;display:inline-block;background:rgba(0,90,160,.08);border-radius:5px;padding:0 4px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
 .icard{font:15px system-ui;line-height:1.5;min-width:230px;max-width:320px;color:var(--fg)}
 .icard b{font-size:16px}
 .icard .ig{display:grid;grid-template-columns:auto auto;gap:2px 14px;margin-top:6px}
 .icard .ik{color:var(--mut);white-space:nowrap}
 .icard .isep{grid-column:1/-1;border-top:1px solid var(--bd);margin:4px 0}
 .icard .ipow{margin-top:6px;padding:4px 8px;border-radius:6px;font-weight:600;font-size:12px;text-align:center}
 .icard .ipow.yes{background:rgba(0,160,80,.12);color:#0a7a3a}
 .icard .ipow.no{background:rgba(220,60,60,.1);color:#c03030}
 .itabs{display:flex;gap:0;border-bottom:1px solid var(--bd);margin:8px 0 6px}
 .itab{flex:1;padding:10px 6px;text-align:center;font-size:15px;font-weight:600;color:var(--mut);cursor:pointer;border-bottom:2px solid transparent;transition:.15s;white-space:nowrap}
 .itab.active{color:var(--acc);border-bottom-color:var(--acc)}
 .ipane{display:none}
 .ipane.active{display:block}
 /* Map-click inspect panel */
 /* Super-transparent glass: the map stays clearly visible through the panel,
    blur+saturate keeps the content readable. Draggable via the header. */
 body.insp-open #mapQr,body.insp-open #mapFab{opacity:0;pointer-events:none;transition:opacity .2s}
 .insp-panel{position:fixed;z-index:2600;background:rgba(255,255,255,.38);backdrop-filter:blur(10px) saturate(1.7);-webkit-backdrop-filter:blur(10px) saturate(1.7);display:none;flex-direction:column;box-shadow:var(--elev3);overflow:hidden;border:1px solid rgba(255,255,255,.55)}
 .insp-panel::before{content:'';flex-shrink:0;width:38px;height:4px;border-radius:2px;background:rgba(22,21,46,.28);margin:7px auto -3px}
 .insp-panel .insp-head{cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none}
 .insp-panel.dragging .insp-head{cursor:grabbing}
 .insp-chip{background:rgba(255,255,255,.62)}
 .insp-tile{background:rgba(255,255,255,.5);border-color:rgba(255,255,255,.55)}
 .insp-head button{background:rgba(255,255,255,.6)}
 .insp-panel.open{display:flex;animation:inspIn .3s cubic-bezier(.32,.72,.42,1)}
 @media(min-width:561px){.insp-panel{top:auto;right:16px;bottom:calc(var(--btm-h,0px) + 16px);width:320px;height:320px;border-radius:var(--r-xl)}@keyframes inspIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}}
 @media(max-width:560px){.insp-panel{left:auto;right:12px;bottom:calc(var(--btm-h,0px) + 12px);width:min(60vw,260px);height:min(60vw,260px);border-radius:var(--r-xl)}@keyframes inspIn{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:translateY(0)}}}
 .insp-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:16px 16px 12px;border-bottom:1px solid var(--hair);flex-shrink:0}
 .insp-head .insp-t{min-width:0}
 .insp-head .insp-t b{font-size:15px;color:var(--fg);font-weight:800;letter-spacing:-.01em}
 .insp-chips{display:flex;gap:6px;margin-top:8px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding-right:12px;-webkit-mask-image:linear-gradient(90deg,#000 91%,transparent);mask-image:linear-gradient(90deg,#000 91%,transparent)}
 .insp-chips::-webkit-scrollbar{display:none}
 .insp-chip{font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;background:var(--fill);color:var(--fg2);display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
 .insp-chip.accent{background:rgba(20,20,25,.12);color:var(--acc2)}
 .insp-head button{background:var(--fill);border:none;width:34px;height:34px;border-radius:11px;color:var(--fg2);font-size:16px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:.15s}
 .insp-head button:hover{background:var(--fill2);color:var(--fg)}
 .insp-body{overflow-y:auto;-webkit-overflow-scrolling:touch;padding:4px 18px calc(env(safe-area-inset-bottom,0px) + 16px)}
 .insp-tiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:14px 0 6px}
 .insp-tile{background:var(--fill);border-radius:14px;padding:11px 13px;border:1px solid var(--hair)}
 .insp-tile .tl{font-size:10.5px;font-weight:800;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 .insp-tile .tv{font-size:21px;font-weight:800;color:var(--fg);letter-spacing:-.02em;margin-top:3px;line-height:1}
 .insp-tile .tv small{font-size:12px;font-weight:700;color:var(--mut)}
 .insp-tile.accent .tv{color:var(--acc2)}
 .insp-sec{padding:11px 0;border-bottom:1px solid var(--hair)}
 .insp-sec:last-child{border-bottom:none}
 .insp-sec h4{margin:0 0 9px;font-size:13px;font-weight:800;color:var(--fg);letter-spacing:-.01em;display:flex;justify-content:space-between;align-items:baseline}
 .insp-sec h4 em{font-style:normal;font-weight:700;color:var(--acc2);font-size:12.5px}
 .insp-sec canvas{display:block;width:100%}
 .insp-hscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:thin;flex:1;min-width:0}
 .insp-hint{font-size:10.5px;color:var(--mut);text-align:center;margin-top:5px}
 .insp-radwrap{display:flex;align-items:center;justify-content:center;gap:18px}
 .insp-radring{position:relative;width:78px;height:78px;flex-shrink:0;border-radius:50%}
 .insp-radring .rv{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:19px;font-weight:800;color:var(--fg)}
 .insp-radinfo{font-size:12.5px;color:var(--fg2);line-height:1.5}
 .insp-radinfo b{color:var(--fg)}
 .insp-xmark{position:relative;width:30px;height:30px;display:flex;align-items:center;justify-content:center}
 .insp-xmark svg{width:22px;height:22px;filter:drop-shadow(0 1px 3px rgba(0,0,0,.4));position:relative;z-index:1}
 .insp-xmark::before{content:'';position:absolute;inset:0;border-radius:50%;border:2.5px solid #e0245e;box-shadow:0 0 0 3px rgba(224,36,94,.18);animation:xpulse 1.8s ease-out infinite}
 @keyframes xpulse{0%{transform:scale(.7);opacity:1}70%{transform:scale(1.35);opacity:0}100%{transform:scale(1.35);opacity:0}}
 .leaflet-popup-content-wrapper{background:rgba(255,255,255,.9)!important;backdrop-filter:blur(16px) saturate(1.4)!important;-webkit-backdrop-filter:blur(16px) saturate(1.4)!important;border:1px solid rgba(255,255,255,.6)!important;color:var(--fg)!important;box-shadow:0 4px 24px rgba(0,0,0,.1),0 1px 0 rgba(255,255,255,.5) inset!important;border-radius:var(--r-lg)!important}
 .leaflet-popup-content{margin:12px 14px!important;font-size:14px!important}
 .leaflet-popup-tip{background:rgba(255,255,255,.9)!important}
 .leaflet-popup-close-button{color:var(--mut)!important;font-size:18px!important;width:28px!important;height:28px!important;line-height:28px!important}
 .leaflet-popup-close-button:hover{color:var(--fg)!important}
 /* Attribution: tiny, faint, unobtrusive (expand on hover) */
 .leaflet-control-attribution{background:rgba(255,255,255,.4)!important;color:rgba(80,100,120,.55)!important;font-size:9px!important;padding:1px 6px!important;border-radius:6px 0 0 0!important;box-shadow:none!important;backdrop-filter:blur(4px);max-width:22px;overflow:hidden;white-space:nowrap;transition:max-width .3s,background .3s}
 .leaflet-control-attribution:hover{max-width:80vw;background:rgba(255,255,255,.85)!important;color:var(--mut)!important}
 .leaflet-control-attribution a{color:inherit!important}
 .leaflet-control-scale-line{background:rgba(255,255,255,.6)!important;border-color:rgba(22,21,46,.35)!important;color:var(--fg2)!important;font-family:'Inter',system-ui!important;font-size:10px!important;font-weight:600!important;backdrop-filter:blur(4px)}
 .leaflet-control-scale{margin-bottom:6px!important;margin-right:8px!important}
 #searchWrap{position:absolute;z-index:1100;top:calc(env(safe-area-inset-top,0px) + 116px);left:12px;width:230px;max-width:calc(100vw - 24px)}
 #searchWrap input{width:100%;padding:10px 12px 10px 34px;border-radius:12px;border:1px solid rgba(255,255,255,.55);background:rgba(255,255,255,.72);backdrop-filter:blur(16px) saturate(1.4);-webkit-backdrop-filter:blur(16px) saturate(1.4);color:var(--fg);font-size:14px;font-weight:500;outline:none;box-shadow:0 2px 8px rgba(0,0,0,.07);font-family:inherit}
 #searchWrap input::placeholder{color:var(--mut);font-weight:400}
 #searchWrap input:focus{border-color:var(--acc);background:#fff;box-shadow:0 0 0 3px var(--glow)}
 #searchWrap .icn{position:absolute;left:12px;top:50%;transform:translateY(-50%);pointer-events:none;color:var(--mut);font-size:15px}
 /* Right-side control rail */
 #ctrlRail{position:absolute;z-index:1050;right:12px;top:calc(env(safe-area-inset-top,0px) + 12px);display:flex;flex-direction:column;gap:10px}
 .rail-btn{position:relative;width:46px;height:46px;border-radius:14px;border:1px solid rgba(255,255,255,.55);background:rgba(255,255,255,.72);backdrop-filter:blur(16px) saturate(1.4);-webkit-backdrop-filter:blur(16px) saturate(1.4);color:var(--fg2);display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.09);transition:all .18s cubic-bezier(.34,1.56,.64,1)}
 .rail-btn:hover{background:#fff;color:var(--fg);transform:translateY(-1px)}
 .rail-btn:active{transform:scale(.92)}
 .rail-btn.active{background:var(--fg);color:#fff;border-color:var(--fg)}
 #btn3dFloat{background:rgba(20,20,25,.16);color:var(--acc2);border-color:rgba(20,20,25,.28)}
 .rail-btn.feed-accent{background:linear-gradient(150deg,rgba(20,20,25,.2),rgba(155,140,255,.12));color:var(--acc2);border-color:rgba(20,20,25,.3)}
 #accountBtn.signed{color:var(--acc2)}
 #railToggles.active{color:var(--acc2);background:rgba(20,20,25,.14);border-color:rgba(20,20,25,.3)}
 .rail-note{position:absolute;right:56px;padding:6px 11px;border-radius:10px;background:var(--fg);color:#fff;font-size:12px;font-weight:700;white-space:nowrap;opacity:0;transform:translateX(6px);transition:.25s var(--ease);pointer-events:none}
 .rail-note.show{opacity:1;transform:translateX(0)}
 .me-dot{position:relative;width:18px;height:18px;border-radius:50%;background:#141419;border:3px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.35)}
 .me-dot::after{content:'';position:absolute;inset:-7px;border-radius:50%;border:2px solid rgba(20,20,25,.5);animation:xpulse 2s ease-out infinite}
 .acct-img{display:block;width:34px;height:34px;border-radius:50%;background-size:cover;background-position:center;box-shadow:0 0 0 2px rgba(255,255,255,.9)}
 #userBar{display:none!important} /* replaced by the rail account button */
 .rail-btn.feed-accent:hover{color:var(--acc)}
 .feed-dot{position:absolute;top:8px;right:8px;width:8px;height:8px;border-radius:50%;background:var(--danger);border:1.5px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.2);display:none}
 .feed-dot.on{display:block}
 #btn3dFloat:hover{background:rgba(20,20,25,.26);color:var(--acc2)}
 #btn3dFloat.active{background:var(--acc);color:#fff;border-color:var(--acc)}
 .rail-btn svg{width:21px;height:21px}
 #togglePop{position:absolute;right:56px;top:0;background:rgba(255,255,255,.96);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--bd);border-radius:14px;padding:10px 12px;box-shadow:0 6px 26px rgba(0,0,0,.14);display:none;flex-direction:column;gap:10px;white-space:nowrap}
 #togglePop.show{display:flex}
 #togglePop label{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer}
 #togglePop input{width:16px;height:16px;accent-color:var(--acc)}
 #searchRes{position:absolute;top:100%;left:0;right:0;margin-top:4px;background:var(--glass2);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--bd);border-radius:12px;overflow:hidden;display:none;max-height:260px;overflow-y:auto;box-shadow:0 4px 16px rgba(0,0,0,.1)}
 #searchRes .sr{padding:10px 14px;cursor:pointer;font-size:15px;color:var(--fg2);border-bottom:1px solid rgba(0,0,0,.05);transition:.12s}
 #searchRes .sr:last-child{border-bottom:none}
 #searchRes .sr:hover,#searchRes .sr.sel{background:rgba(0,112,184,.08);color:var(--fg)}
 #searchRes .sr .sub{font-size:11px;color:var(--mut);margin-top:2px}
 #intro{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:18px;background:#ffffff;transition:opacity .24s ease}
 #intro.hide{opacity:0;pointer-events:none}
 #intro .fall{animation:flakeFall 2.9s ease-in-out infinite}
 #intro .flake{display:block;width:74px;height:74px;color:#0a0a0c;animation:flakeSpin 6.5s linear infinite;filter:drop-shadow(0 10px 18px rgba(0,0,0,.14))}
 @keyframes flakeSpin{to{transform:rotate(360deg)}}
 @keyframes flakeFall{0%{transform:translate(0,-12px)}25%{transform:translate(5px,-4px)}50%{transform:translate(-4px,4px)}75%{transform:translate(4px,9px)}100%{transform:translate(0,-12px)}}
 @media (prefers-reduced-motion:reduce){#intro .fall,#intro .flake{animation:none}}
 #intro .track{width:132px;height:3px;border-radius:2px;background:rgba(22,21,46,.10);overflow:hidden}
 #intro .bar{height:100%;width:100%;border-radius:2px;background:linear-gradient(90deg,#141419,#3c3c42);transform-origin:left;transform:scaleX(.12);animation:introLoad 1.15s ease-in-out infinite alternate}
 @keyframes introLoad{from{transform:scaleX(.12)}to{transform:scaleX(1)}}
 #intro .sub{font-size:12px;color:var(--mut);letter-spacing:.02em;min-height:14px}
 /* --- First-run legal / safety disclaimer gate --- */
 #disc{position:fixed;inset:0;z-index:9800;display:none;align-items:flex-end;justify-content:center;background:rgba(6,14,26,.55);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
 #disc.show{display:flex;animation:discBg .3s ease}
 @keyframes discBg{from{opacity:0}to{opacity:1}}
 #disc .sheet{width:100%;max-width:520px;background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border:1px solid rgba(255,255,255,.6);border-radius:var(--r-xl) var(--r-xl) 0 0;box-shadow:var(--elev3);padding:24px 22px calc(env(safe-area-inset-bottom,0px) + 22px);animation:discUp .4s var(--ease-spring)}
 @media(min-width:560px){#disc{align-items:center}#disc .sheet{border-radius:var(--r-xl)}}
 @keyframes discUp{from{transform:translateY(40px);opacity:0}to{transform:translateY(0);opacity:1}}
 #disc .ic{width:44px;height:44px;border-radius:14px;display:flex;align-items:center;justify-content:center;background:var(--danger-tint);color:var(--danger);margin-bottom:14px}
 #disc .ic svg{width:24px;height:24px}
 #disc h2{margin:0 0 8px;font-size:20px;font-weight:800;letter-spacing:-.02em;color:var(--fg)}
 #disc p{margin:0 0 12px;font-size:14px;line-height:1.5;color:var(--fg2)}
 #disc .fine{font-size:12px;color:var(--mut);line-height:1.45}
 #disc a{color:var(--acc);font-weight:600;text-decoration:none}
 #disc .chk{display:flex;gap:10px;align-items:flex-start;margin:14px 0;font-size:13px;color:var(--fg2);cursor:pointer}
 #disc .chk input{margin-top:1px;width:18px;height:18px;accent-color:var(--acc);flex-shrink:0}
 #disc button.accept{width:100%;border:none;border-radius:var(--r);padding:14px;font-size:15px;font-weight:700;font-family:inherit;color:#fff;background:var(--acc);cursor:pointer;transition:.2s;box-shadow:var(--elev1)}
 #disc button.accept:disabled{opacity:.45;cursor:not-allowed}
 #disc button.accept:not(:disabled):active{transform:scale(.98)}
 /* --- Add-to-Home-Screen sheet --- */
 #a2hs{position:fixed;inset:0;z-index:9700;display:none;align-items:flex-end;justify-content:center;background:rgba(10,9,26,.5);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
 #a2hs.show{display:flex;animation:discBg .3s ease}
 #a2hs .sheet{width:100%;max-width:480px;background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border:1px solid rgba(255,255,255,.6);border-radius:var(--r-xl) var(--r-xl) 0 0;box-shadow:var(--elev3);padding:22px 22px calc(env(safe-area-inset-bottom, 0px) + 20px)}
 @media(min-width:560px){#a2hs{align-items:center}#a2hs .sheet{border-radius:var(--r-xl)}}
 #a2hs .sheet{animation:discUp .4s var(--ease-spring)}
 .a2hs-ic{width:52px;height:52px;border-radius:15px;display:flex;align-items:center;justify-content:center;background:linear-gradient(140deg,#0c0c0f,#26262c);color:#fff;margin-bottom:12px;box-shadow:var(--elev2)}
 .a2hs-ic svg{width:28px;height:28px}
 #a2hs h2{margin:0 0 6px;font-size:19px;font-weight:800;letter-spacing:-.02em;color:var(--fg)}
 #a2hs p{margin:0 0 14px;font-size:13.5px;line-height:1.5;color:var(--fg2)}
 .a2hs-step{display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:14px;background:var(--fill);border:1px solid var(--hair);margin-bottom:8px;font-size:14px;font-weight:650;color:var(--fg)}
 .a2hs-step .n{width:24px;height:24px;border-radius:50%;background:var(--acc);color:#fff;font-size:12.5px;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0}
 .a2hs-step svg{width:21px;height:21px;color:var(--acc2);flex-shrink:0}
 #a2hs button.accept{width:100%;margin-top:6px;border:none;border-radius:var(--r);padding:14px;font-size:15px;font-weight:800;font-family:inherit;color:#fff;background:var(--acc);cursor:pointer;box-shadow:var(--elev1);transition:.2s}
 #a2hs button.accept:active{transform:scale(.98)}
 #a2hs .later{width:100%;margin-top:8px;border:none;background:none;color:var(--mut);font-size:13.5px;font-weight:700;font-family:inherit;cursor:pointer;padding:9px}
 .a2hs-hint{font-size:12.5px;color:var(--mut);line-height:1.45;padding:10px 12px;background:var(--fill);border-radius:12px;margin-bottom:6px}
 body.standalone #profInstall{display:none}
 .sf{position:absolute;top:-10px;width:6px;height:6px;background:white;border-radius:50%;opacity:.6;animation:sfDrop linear infinite}
 @keyframes sfDrop{0%{transform:translateY(0) translateX(0)}25%{transform:translateY(25vh) translateX(15px)}50%{transform:translateY(50vh) translateX(-10px)}75%{transform:translateY(75vh) translateX(20px)}100%{transform:translateY(110vh) translateX(5px)}}
 /* --- Onboarding coach marks --- */
 #coach{position:fixed;inset:0;z-index:8000}
 #coachSpot{position:absolute;border-radius:14px;box-shadow:0 0 0 9999px rgba(11,17,32,.6);transition:all .35s cubic-bezier(.4,0,.2,1);pointer-events:none}
 #coachCard{position:absolute;width:min(300px,calc(100vw - 32px));background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border:1px solid rgba(255,255,255,.6);border-radius:var(--r-lg);padding:18px 20px;box-shadow:var(--elev3);transition:top .35s var(--ease),left .35s var(--ease);opacity:0;animation:coachIn .3s .1s ease forwards}
 @keyframes coachIn{to{opacity:1}}
 #coachText{font-size:15px;line-height:1.5;color:var(--fg2);font-weight:500}
 #coachText b{font-weight:800;color:var(--fg)}
 #coachNav{display:flex;align-items:center;gap:10px;margin-top:16px}
 #coachDots{display:flex;gap:6px;flex:1}
 #coachDots i{width:6px;height:6px;border-radius:50%;background:rgba(0,0,0,.15);transition:.2s}
 #coachDots i.on{background:var(--fg);width:16px;border-radius:3px}
 #coachSkip{background:none;border:none;color:var(--mut);font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
 #coachNext{background:var(--fg);color:#fff;border:none;border-radius:11px;padding:9px 18px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;transition:.15s}
 #coachNext:hover{background:#000}
 @media (max-width:560px){
   #layerBar{max-width:calc(100vw - 98px);padding:5px}
   #topics button{padding:9px 13px;font-size:14px;min-height:40px}
   #topics button span{display:inline}
   #sublayers button{padding:6px 12px;font-size:12.5px;min-height:32px}
   #searchWrap{top:calc(env(safe-area-inset-top,0px) + 118px);left:8px;right:auto;width:calc(100vw - 84px);max-width:230px}
   .icard{font-size:14px;max-width:calc(100vw - 50px);min-width:200px}
   .scard{font-size:14px;min-width:160px}
   .leaflet-popup-content-wrapper{max-width:calc(100vw - 32px)!important}
   .legend{max-width:180px;font-size:12px}
   #ctrlRail{top:calc(env(safe-area-inset-top,0px) + 12px);right:8px;gap:11px}
   .rail-btn{width:48px;height:48px}
   #legendBtn{width:40px;height:40px;font-size:18px}
   .seg button{padding:9px 14px;font-size:15px;min-height:44px}
   .itab{padding:10px 6px;font-size:14px}
 }
 @media (max-width:380px){
   #searchWrap{max-width:160px}
   .legend{max-width:140px;font-size:11px}
 }
 /* --- Auth & Reports --- */
 #userBar{position:fixed;top:calc(env(safe-area-inset-top,0px) + 18px);right:12px;z-index:1100;display:flex;gap:8px;align-items:center}
 .login-btn{min-height:40px;padding:9px 18px;border-radius:999px;border:1.5px solid rgba(22,21,46,.12);background:rgba(255,255,255,.85);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--fg);font-size:14px;font-weight:600;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.06);letter-spacing:-.01em}
 .login-btn:hover{background:#fff}
 .user-pill{display:flex;align-items:center;gap:8px;min-height:40px;padding:5px 14px 5px 5px;border-radius:999px;border:1.5px solid rgba(22,21,46,.08);background:rgba(255,255,255,.85);backdrop-filter:blur(12px);cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 .user-avatar{width:26px;height:26px;border-radius:50%;background:var(--fg);color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;letter-spacing:.02em}
 .user-name{font-size:13px;font-weight:600;color:var(--fg2);max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .auth-overlay{position:fixed;inset:0;z-index:5000;background:rgba(11,17,32,.5);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:16px}
 .auth-modal{position:relative;background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-radius:var(--r-xl);padding:36px 28px 28px;width:100%;max-width:360px;box-shadow:var(--elev3),0 1px 0 rgba(255,255,255,.5) inset}
 .auth-modal h2{margin:0 0 4px;font-size:24px;font-weight:800;color:var(--fg);letter-spacing:-.03em}
 .auth-note{font-size:12px;color:var(--mut);margin:4px 0 10px;line-height:1.5}
 .auth-sub{font-size:13px;color:var(--mut);margin:0 0 24px}
 .auth-modal form{display:flex;flex-direction:column;gap:10px}
 .auth-modal input[type="text"],.auth-modal input[type="email"],.auth-modal input[type="password"]{width:100%;padding:13px 16px;border:1.5px solid rgba(0,0,0,.08);border-radius:var(--r);font-size:15px;font-family:inherit;color:var(--fg);background:var(--fill);outline:none;box-sizing:border-box;transition:border-color .15s,box-shadow .15s}
 .auth-modal input:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--glow);background:var(--card)}
 .auth-err{color:#d03030;font-size:13px;padding:8px 12px;background:rgba(220,60,60,.06);border-radius:10px;min-height:0}
 .auth-btn{padding:13px;border-radius:var(--r);border:none;font-size:15px;font-weight:700;cursor:pointer;width:100%;font-family:inherit;letter-spacing:-.01em;transition:all .15s}
 .auth-btn.primary{background:var(--fg);color:#fff}.auth-btn.primary:hover{background:#000}
 .auth-btn.ghost{background:none;color:var(--mut);margin-top:8px}
 .auth-btn.ghost:hover{background:rgba(0,0,0,.04);color:var(--fg)}
 .auth-code{width:100%;box-sizing:border-box;text-align:center;font-size:30px!important;font-weight:800;letter-spacing:12px;padding:14px 8px!important;border:1.5px solid rgba(0,0,0,.1)!important;border-radius:14px;background:var(--fill);color:var(--fg);outline:none;margin-bottom:10px;font-family:inherit}
 .auth-code:focus{border-color:var(--acc)!important;background:var(--card);box-shadow:0 0 0 3px var(--glow)}
 .auth-paste{width:100%;display:flex;align-items:center;justify-content:center;gap:8px;padding:12px;border:1.5px dashed rgba(20,20,25,.3);border-radius:12px;background:rgba(20,20,25,.05);color:var(--acc2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;margin-bottom:10px}
 .auth-paste svg{width:17px;height:17px}
 .auth-paste:hover{background:rgba(20,20,25,.1)}
 .auth-bio-ic{width:64px;height:64px;margin:6px auto 12px;border-radius:20px;background:rgba(20,20,25,.1);color:var(--acc);display:flex;align-items:center;justify-content:center}
 .auth-bio-ic svg{width:36px;height:36px}
 .bio-lock{position:fixed;inset:0;z-index:8500;background:linear-gradient(160deg,#0e0c22,#0c0c0f 60%,#26262c);display:flex;align-items:center;justify-content:center;padding:24px}
 .bio-lock-card{text-align:center;color:#fff;max-width:300px;width:100%}
 .bio-lock-card .auth-bio-ic{background:rgba(255,255,255,.12);color:#fff;width:76px;height:76px}
 .bio-lock-t{font-size:22px;font-weight:800;letter-spacing:-.02em}
 .bio-lock-s{font-size:14px;color:rgba(255,255,255,.7);margin:4px 0 22px}
 .bio-lock-card .auth-btn.primary{background:#fff;color:var(--fg)}
 .bio-lock-out{background:none;border:none;color:rgba(255,255,255,.6);font-size:14px;font-weight:600;cursor:pointer;margin-top:14px;font-family:inherit}
 .auth-close{position:absolute;top:16px;right:16px;background:none;border:none;font-size:20px;color:var(--mut);cursor:pointer;width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center}
 .auth-close:hover{background:rgba(0,0,0,.05)}
 .auth-switch{text-align:center;margin-top:20px;font-size:13px;color:var(--mut)}
 .auth-switch button{background:none;border:none;color:var(--acc);font-weight:600;cursor:pointer;font-size:13px}
 .email-banner{position:fixed;top:calc(env(safe-area-inset-top,0px) + 56px);left:12px;right:12px;z-index:1200;display:none;align-items:center;justify-content:space-between;gap:12px;padding:10px 16px;background:rgba(255,240,220,.95);border:1px solid rgba(200,150,50,.2);border-radius:12px;font-size:13px;color:#6a4a10;box-shadow:0 2px 8px rgba(0,0,0,.06)}
 .email-banner button{background:none;border:none;color:#0070b8;font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}
 /* --- Report FAB (primary action) --- */
 /* reportFab entfernt — der + FAB (mapFab) ist der einzige Melde-Einstieg auf der Karte */
 /* --- Radial quick-pick --- */
 .radial-wrap{position:fixed;inset:0;z-index:4000;touch-action:none;display:none}
 .radial-bg{position:absolute;inset:0;background:rgba(11,17,32,.6);backdrop-filter:blur(8px)}
 .radial-ring{position:absolute;width:280px;height:280px;border-radius:50%}
 .radial-seg{position:absolute;width:72px;height:72px;border-radius:50%;background:rgba(20,30,51,.85);border:2px solid rgba(94,200,255,.15);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:13px;font-weight:600;color:#E8EEF7;transition:transform .15s cubic-bezier(.34,1.56,.64,1),border-color .15s,background .15s;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
 .radial-seg .re{font-size:28px}
 .radial-seg.hover{transform:scale(1.18);border-color:#5EC8FF;background:rgba(94,200,255,.15);box-shadow:0 0 24px rgba(94,200,255,.3),inset 0 1px 0 rgba(255,255,255,.1)}
 .radial-center{position:absolute;width:48px;height:48px;border-radius:50%;background:rgba(94,200,255,.2);border:2px solid rgba(94,200,255,.3);display:flex;align-items:center;justify-content:center;color:#5EC8FF;font-size:20px}
 /* --- Report sheet (Alpenglühen dark) --- */
 /* --- Report wizard: centered light modal --- */
 .report-overlay{position:fixed;inset:0;z-index:3000;display:flex;align-items:center;justify-content:center;padding:16px}
 .report-overlay .ro-bg{position:absolute;inset:0;background:rgba(11,17,32,.45);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
 .report-sheet{position:relative;width:100%;max-width:440px;max-height:92vh;overflow-y:auto;background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-radius:26px;box-shadow:var(--elev3);-webkit-overflow-scrolling:touch;animation:rpSheetIn .28s cubic-bezier(.34,1.4,.64,1)}
 @keyframes rpSheetIn{from{opacity:0;transform:translateY(16px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
 .report-sheet .sh{display:none}
 .cat-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
 .cat-chip{display:flex;flex-direction:column;align-items:center;gap:7px;padding:14px 4px;border-radius:16px;border:1.5px solid rgba(22,21,46,.08);background:var(--fill);cursor:pointer;font-size:12px;font-weight:650;color:var(--mut);transition:all .2s cubic-bezier(.34,1.56,.64,1);-webkit-tap-highlight-color:transparent}
 .cat-chip:active{transform:scale(.94)}
 .cat-chip.active{border-color:currentColor;background:#fff;box-shadow:0 4px 16px rgba(22,21,46,.1)}
 .cat-chip .cat-ico-w{width:30px;height:30px;display:flex;align-items:center;justify-content:center}
 .cat-chip .cat-ico-w svg{width:26px;height:26px;stroke:currentColor}
 .sub-chips{display:flex;flex-wrap:wrap;gap:8px}
 .sub-chip{padding:11px 16px;border-radius:999px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);cursor:pointer;font-size:14px;font-weight:600;color:var(--fg2);transition:all .15s;-webkit-tap-highlight-color:transparent}
 .sub-chip:active{transform:scale(.95)}
 .sub-chip.active{background:var(--acc);border-color:var(--acc);color:#fff}
 .bucket-track{display:flex;gap:8px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;padding:4px 0}
 .bucket-track::-webkit-scrollbar{display:none}
 .bucket{min-width:64px;padding:14px 8px;border-radius:15px;border:1.5px solid rgba(22,21,46,.08);background:var(--fill);cursor:pointer;font-size:16px;font-weight:700;color:var(--fg2);text-align:center;flex-shrink:0;transition:all .2s cubic-bezier(.34,1.56,.64,1);-webkit-tap-highlight-color:transparent}
 .bucket:active{transform:scale(.95)}
 .bucket.active{background:var(--acc);border-color:var(--acc);color:#fff;box-shadow:0 4px 14px rgba(20,20,25,.3)}
 .bucket-val{font-size:12px;color:var(--mut);text-align:center;margin-top:8px;min-height:16px;font-weight:600}
 /* detail follow-up groups */
 .rp-fu{margin-bottom:16px}
 .rp-fu:last-child{margin-bottom:0}
 .rp-fu-lbl{font-size:13px;font-weight:700;color:var(--fg2);margin-bottom:9px}
 .rp-fu-opts{display:flex;flex-wrap:wrap;gap:8px}
 .rp-fu-multi{color:var(--mut);font-weight:600;font-size:11.5px}
 .rp-fu-opts button{padding:11px 15px;border-radius:14px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);cursor:pointer;font-size:14px;font-weight:600;color:var(--fg2);font-family:inherit;transition:all .15s}
 .rp-fu-opts button:active{transform:scale(.95)}
 .rp-fu-opts button.active{background:var(--acc);border-color:var(--acc);color:#fff}
 /* stars */
 .rp-stars{display:flex;justify-content:center;gap:8px;padding:14px 0 4px}
 .rp-stars button{background:none;border:none;cursor:pointer;padding:2px;color:rgba(22,21,46,.16);transition:transform .12s,color .12s}
 .rp-stars button svg{width:42px;height:42px}
 .rp-stars button.on{color:#f5a623}
 .rp-stars button.on svg{fill:#f5a623}
 .rp-stars button:active{transform:scale(1.2)}
 .rp-stars-lbl{text-align:center;font-size:14px;font-weight:700;color:var(--fg2);margin-top:8px;min-height:20px}
 .rp-voice-btn{width:100%;padding:14px;border-radius:14px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);cursor:pointer;font-size:14px;font-weight:600;color:var(--fg2);display:flex;align-items:center;justify-content:center;gap:8px;transition:all .15s;-webkit-tap-highlight-color:transparent}
 .rp-voice-btn:active,.rp-voice-btn.recording{background:rgba(20,20,25,.1);border-color:var(--acc);color:var(--acc)}
 .rp-caption{width:100%;padding:14px;border:1.5px solid rgba(22,21,46,.1);border-radius:14px;font-size:15px;font-family:inherit;resize:none;outline:none;background:var(--fill);color:var(--fg);box-sizing:border-box;min-height:64px;margin-top:10px}
 .rp-caption::placeholder{color:var(--mut)}
 .rp-caption:focus{border-color:var(--acc);background:#fff;box-shadow:0 0 0 3px var(--glow)}
 .rp-wiz-head{display:flex;align-items:center;gap:12px;padding:16px 18px 0}
 .rp-nav-btn{width:36px;height:36px;border-radius:11px;border:none;background:rgba(22,21,46,.05);color:var(--fg2);display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:.15s}
 .rp-nav-btn svg{width:20px;height:20px}
 .rp-nav-btn:hover{background:rgba(22,21,46,.1);color:var(--fg)}
 .rp-nav-btn:disabled{opacity:.3;cursor:default}
 .rp-progress{flex:1;display:flex;gap:6px}
 .rp-progress i{flex:1;height:4px;border-radius:2px;background:rgba(22,21,46,.1);transition:background .25s}
 .rp-progress i.done{background:var(--acc)}
 .rp-progress i.cur{background:var(--acc);box-shadow:0 0 0 3px var(--glow)}
 .rp-step-title{font-size:21px;font-weight:800;color:var(--fg);letter-spacing:-.02em;padding:14px 20px 2px}
 .rp-step-sub{font-size:13px;color:var(--mut);padding:0 20px 4px}
 .rp-pane{padding:14px 20px 0;animation:rpPaneIn .25s ease}
 @keyframes rpPaneIn{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:translateX(0)}}
 .rp-photo-big{width:100%;aspect-ratio:4/3;max-height:40vh;border-radius:18px;background:var(--fill);border:2px dashed rgba(22,21,46,.14);cursor:pointer;overflow:hidden;display:flex;align-items:center;justify-content:center;transition:border-color .2s}
 .rp-photo-big:active{border-color:var(--acc)}
 .rp-photo-big.has-img{border:none;background:#eef2f6}
 .rp-photo-big img{width:100%;height:100%;object-fit:cover}
 .rp-photo-placeholder{display:flex;flex-direction:column;align-items:center;gap:12px;color:var(--acc)}
 .rp-photo-placeholder svg{width:52px;height:52px;opacity:.9}
 .rp-photo-placeholder span{font-size:15px;font-weight:700}
 .rp-loc-card{display:flex;align-items:center;gap:8px;padding:12px 14px;border-radius:14px;background:var(--fill);color:var(--fg2);font-size:13.5px;font-weight:600;margin-bottom:10px}
 .rp-loc-switch{margin-left:auto;background:none;border:none;color:var(--acc);font-weight:700;font-size:12.5px;cursor:pointer;font-family:inherit;flex-shrink:0;white-space:nowrap}
 .rp-peak{margin-bottom:12px;padding:14px 16px;border-radius:16px;background:rgba(20,20,25,.06);border:1.5px solid rgba(20,20,25,.18)}
 .rp-peak-q{font-size:15px;color:var(--fg);font-weight:700;margin-bottom:10px}
 .rp-peak-btns{display:flex;gap:8px}
 .rp-peak-btns button{flex:1;padding:11px;border-radius:11px;border:1.5px solid rgba(22,21,46,.12);background:#fff;color:var(--fg2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}
 .rp-peak-btns button.yes{background:var(--acc);color:#fff;border-color:var(--acc)}
 .rp-peak.confirmed{border-color:#25a35a;background:rgba(37,163,90,.08)}
 .rp-peak.confirmed .rp-peak-q{color:#1c7a44}
 .rp-group{display:flex;align-items:center;gap:10px;margin-top:12px}
 .rp-group-lbl{font-size:14px;color:var(--fg2);font-weight:650;flex-shrink:0}
 .rp-group-sel{flex:1;padding:11px 14px;border-radius:12px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);color:var(--fg);font-size:14px;font-family:inherit;outline:none;-webkit-appearance:none;appearance:none}
 .rp-summary{margin-top:14px;display:flex;flex-wrap:wrap;gap:6px}
 .rp-summary .rp-tag{padding:5px 11px;border-radius:999px;background:rgba(22,21,46,.05);color:var(--fg2);font-size:12.5px;font-weight:650;display:inline-flex;align-items:center;gap:5px}
 .rp-summary .rp-tag svg{width:13px;height:13px;stroke:var(--acc)}
 .rp-wiz-nav{display:flex;align-items:center;gap:12px;margin-top:14px;padding:16px 20px calc(env(safe-area-inset-bottom,0px) + 26px);position:sticky;bottom:0;background:linear-gradient(180deg,rgba(255,255,255,0),#fff 34%);border-top:1px solid rgba(22,21,46,.05)}
 .rp-back-btn{display:inline-flex;align-items:center;gap:4px;background:rgba(22,21,46,.05);border:none;color:var(--fg2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;padding:12px 15px;border-radius:14px;flex-shrink:0}
 .rp-back-btn svg{width:17px;height:17px}
 .rp-back-btn:hover{background:rgba(22,21,46,.1);color:var(--fg)}
 .rp-skip{background:none;border:none;color:var(--mut);font-size:15px;font-weight:650;cursor:pointer;font-family:inherit;padding:12px}
 .rp-next{flex:1;padding:15px;border-radius:15px;border:none;background:var(--fg);color:#fff;font-size:16px;font-weight:800;cursor:pointer;font-family:inherit;letter-spacing:-.01em;transition:.15s}
 .rp-next:hover{background:#000}
 .rp-next:disabled{opacity:.35;cursor:default}
 .rp-next.post{background:var(--acc)}
 .rp-next.post:hover{background:var(--acc2)}
 /* --- SLF-style observation wizard --- */
 .obs-body{padding:14px 20px 26px}
 .obs-body:empty{padding:0}
 .obs-types{display:flex;flex-direction:column;gap:10px}
 /* Immersive type-selection cluster ("explosion" entrance, scattered cards) */
 .obs-types.cluster{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:8px 2px 14px}
 .obs-types.cluster .obs-type{flex-direction:column;align-items:flex-start;gap:10px;padding:15px 14px 13px;border-radius:20px;border:1px solid var(--hair);background:linear-gradient(155deg,var(--tt),rgba(255,255,255,.92) 72%);transform:translate(var(--dx),var(--dy)) rotate(var(--rot));animation:obsPop .55s var(--ease-spring) both;animation-delay:calc(var(--i)*70ms);box-shadow:var(--elev1)}
 .obs-types.cluster .obs-type:hover{box-shadow:var(--elev2);background:linear-gradient(155deg,var(--tt),#fff 72%)}
 .obs-types.cluster .obs-type:active{transform:translate(var(--dx),var(--dy)) rotate(var(--rot)) scale(.95)}
 .obs-types.cluster .obs-type:last-child{grid-column:1/-1;flex-direction:row;align-items:center}
 .obs-types.cluster .obs-type-ic{width:52px;height:52px;border-radius:16px;background:var(--tc);color:#fff;box-shadow:0 6px 14px var(--tt)}
 .obs-types.cluster .obs-type-ic svg{width:28px;height:28px}
 .obs-types.cluster .obs-type-tx b{font-size:15.5px}
 @keyframes obsPop{from{opacity:0;transform:translate(0,30px) scale(.35) rotate(0deg)}60%{opacity:1}to{opacity:1;transform:translate(var(--dx),var(--dy)) rotate(var(--rot)) scale(1)}}
 .obs-type{display:flex;align-items:center;gap:14px;padding:16px;border-radius:18px;border:1.5px solid rgba(22,21,46,.08);background:var(--fill);cursor:pointer;text-align:left;font-family:inherit;transition:all .16s cubic-bezier(.34,1.56,.64,1)}
 .obs-type:active{transform:scale(.98)}
 .obs-type:hover{background:#fff;box-shadow:0 4px 16px rgba(22,21,46,.08)}
 .obs-type-ic{width:46px;height:46px;border-radius:14px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
 .obs-type-ic svg{width:26px;height:26px}
 .obs-type-tx b{display:block;font-size:16px;font-weight:800;color:var(--fg);letter-spacing:-.01em}
 .obs-type-tx span{font-size:12.5px;color:var(--mut);font-weight:500}
 .obs-media-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
 .obs-media-tile{aspect-ratio:1;border-radius:14px;overflow:hidden;position:relative;background:#eef2f6}
 .obs-media-tile img{width:100%;height:100%;object-fit:cover}
 .obs-media-tile .rm{position:absolute;top:4px;right:4px;width:22px;height:22px;border-radius:50%;background:rgba(11,17,32,.6);color:#fff;border:none;font-size:13px;cursor:pointer;display:flex;align-items:center;justify-content:center}
 .obs-media-add{aspect-ratio:1;border-radius:14px;border:2px dashed rgba(22,21,46,.16);background:var(--fill);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;cursor:pointer;color:var(--acc);font-family:inherit}
 .obs-media-add svg{width:26px;height:26px}
 .obs-media-add span{font-size:11px;font-weight:700}
 .obs-hint{font-size:12.5px;color:var(--mut);line-height:1.45;margin:2px 0 12px;padding:10px 12px;background:rgba(20,20,25,.06);border-radius:12px}
 .obs-enum{display:flex;flex-direction:column;gap:8px}
 .obs-enum button{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:15px 16px;border-radius:14px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);cursor:pointer;font-size:15px;font-weight:600;color:var(--fg);font-family:inherit;text-align:left;transition:.14s}
 .obs-enum button .ct{display:block;font-size:12px;color:var(--mut);font-weight:500;margin-top:1px}
 .obs-enum button .rd{width:22px;height:22px;border-radius:50%;border:2px solid rgba(22,21,46,.2);flex-shrink:0}
 .obs-enum button.active{border-color:var(--acc);background:rgba(20,20,25,.06)}
 .obs-enum button.active .rd{border-color:var(--acc);background:radial-gradient(circle,var(--acc) 42%,transparent 46%)}
 .obs-card{border:1.5px solid rgba(22,21,46,.08);border-radius:16px;margin-bottom:10px;overflow:hidden;background:#fff}
 .obs-card-h{display:flex;align-items:center;gap:10px;padding:14px 16px;cursor:pointer;font-weight:700;color:var(--fg);font-size:15px}
 .obs-card-h .chev{margin-left:auto;transition:transform .2s}
 .obs-card-h .chev svg{width:18px;height:18px}
 .obs-card.open .obs-card-h .chev{transform:rotate(180deg)}
 .obs-card-b{display:none;padding:0 16px 16px}
 .obs-card.open .obs-card-b{display:block}
 .obs-card-sum{margin-left:auto;font-size:12px;color:var(--acc2);font-weight:700}
 .obs-fld{margin-top:12px}
 .obs-fld-l{font-size:13px;font-weight:700;color:var(--fg2);margin-bottom:8px}
 .obs-chips{display:flex;flex-wrap:wrap;gap:7px}
 .obs-chips button{padding:9px 13px;border-radius:12px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);cursor:pointer;font-size:13.5px;font-weight:600;color:var(--fg2);font-family:inherit;transition:.14s}
 .obs-chips button.active{background:var(--acc);border-color:var(--acc);color:#fff}
 .obs-toggle{display:flex;align-items:center;justify-content:space-between;margin-top:12px;font-size:14px;font-weight:600;color:var(--fg)}
 .obs-sw{width:48px;height:28px;border-radius:999px;border:none;background:rgba(22,21,46,.15);position:relative;cursor:pointer;flex-shrink:0}
 .obs-sw span{position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:left .18s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
 .obs-sw.on{background:var(--acc)}.obs-sw.on span{left:23px}
 .obs-size-vis{display:flex;align-items:center;gap:14px;margin:6px 0 12px;padding:14px;border-radius:16px;background:var(--fill)}
 .obs-size-obj{font-size:44px;line-height:1;flex-shrink:0}
 .obs-size-cons{font-size:13.5px;color:var(--fg2);font-weight:600;line-height:1.4}
 .obs-person{border:1.5px solid rgba(22,21,46,.1);border-radius:14px;padding:12px 14px;margin-bottom:10px;position:relative}
 .obs-person .rm{position:absolute;top:8px;right:8px;background:none;border:none;color:#d03050;font-size:13px;font-weight:700;cursor:pointer}
 .obs-add-btn{width:100%;padding:12px;border-radius:12px;border:1.5px dashed rgba(20,20,25,.3);background:rgba(20,20,25,.05);color:var(--acc2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}
 .obs-loc-map{width:100%;height:170px;border-radius:16px;overflow:hidden;margin-bottom:10px;border:1px solid rgba(22,21,46,.1)}
 .obs-loc-wrap{position:relative}
 .obs-pin{position:absolute;left:50%;top:50%;transform:translate(-50%,-100%);z-index:600;color:#e0245e;pointer-events:none;filter:drop-shadow(0 3px 5px rgba(0,0,0,.3))}
 .obs-pin svg{width:34px;height:34px;display:block}
 .mm-tools{position:absolute;right:8px;top:8px;display:flex;flex-direction:column;gap:6px;z-index:600}
 .mm-tools button{width:36px;height:36px;border-radius:11px;border:1px solid var(--hair);background:rgba(255,255,255,.9);backdrop-filter:blur(8px);color:var(--fg);cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:var(--elev1)}
 .mm-tools button svg{width:17px;height:17px}
 .obs-pin-hint{position:absolute;left:50%;bottom:8px;transform:translateX(-50%);z-index:600;pointer-events:none;font-size:10.5px;font-weight:700;color:#fff;background:rgba(11,17,32,.62);padding:4px 10px;border-radius:999px;white-space:nowrap}
 .obs-loc-row{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--fg2);font-weight:600;margin-bottom:10px;flex-wrap:wrap}
 .obs-loc-src{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;background:rgba(20,20,25,.12);color:var(--acc2)}
 .obs-dt{width:100%;box-sizing:border-box;padding:12px 14px;border:1.5px solid rgba(22,21,46,.1);border-radius:12px;font-size:14px;font-family:inherit;color:var(--fg);background:var(--fill);margin-bottom:10px}
 .obs-warn{font-size:12.5px;color:#b06a00;background:rgba(245,158,11,.12);border-radius:10px;padding:8px 11px;margin-bottom:10px;font-weight:600}
 .obs-cc{font-size:11px;color:var(--mut);text-align:right;margin-top:4px}
 .obs-summary{margin-top:14px;display:flex;flex-wrap:wrap;gap:6px}
 .obs-summary .rp-tag{padding:5px 11px;border-radius:999px;background:rgba(22,21,46,.05);color:var(--fg2);font-size:12.5px;font-weight:650}
 /* --- Snow-condition inputs --- */
 .snow-kinds{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
 .snow-kind{padding:10px 14px;border-radius:12px;border:1.5px solid var(--hair);border-left:4px solid var(--k);background:var(--fill);font-family:inherit;font-size:14px;font-weight:700;color:var(--fg2);cursor:pointer;transition:.15s var(--ease)}
 .snow-kind:hover{background:var(--card)}
 .snow-kind.active{background:var(--k);border-color:var(--k);color:#fff}
 .snow-fields{margin-top:4px}
 .snow-val{color:var(--acc2);font-weight:800;font-size:13px;float:right}
 .obs-range{width:100%;-webkit-appearance:none;appearance:none;height:6px;border-radius:3px;background:var(--fill2);outline:none;margin:8px 0 2px}
 .obs-range::-webkit-slider-thumb{-webkit-appearance:none;width:24px;height:24px;border-radius:50%;background:var(--acc);border:3px solid #fff;box-shadow:var(--elev1);cursor:pointer}
 .obs-range::-moz-range-thumb{width:24px;height:24px;border-radius:50%;background:var(--acc);border:3px solid #fff;box-shadow:var(--elev1);cursor:pointer}
 .obs-range-row{display:flex;align-items:center;gap:10px}
 .obs-range-row span{font-size:12px;font-weight:700;color:var(--mut);width:26px;flex-shrink:0;text-align:right}
 .rose-wrap{display:flex;justify-content:center;margin:8px 0 4px}
 .rose-svg{width:184px;height:184px}
 .rose-w{fill:var(--fill2);stroke:var(--card);stroke-width:2;cursor:pointer;transition:fill .14s}
 .rose-w:hover{fill:var(--fill)}
 .rose-w.on{fill:var(--acc)}
 .rose-t{fill:var(--fg2);font:800 12px Inter,system-ui;text-anchor:middle;pointer-events:none}
 .rose-t.on{fill:#fff}
 .rose-presets{justify-content:center;margin-top:2px}
 /* dual-handle altitude band (timeslider-style) */
 .alt-band{position:relative;height:34px;margin:26px 4px 2px;border-radius:6px;background:var(--fill2)}
 .ab-fill{position:absolute;top:0;bottom:0;background:rgba(20,20,25,.28);border-radius:6px}
 .ab-h{position:absolute;top:50%;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;background:var(--acc);border:3px solid #fff;box-shadow:var(--elev1);cursor:ew-resize;touch-action:none}
 .ab-h span{position:absolute;top:-24px;left:50%;transform:translateX(-50%);font-size:11px;font-weight:800;color:var(--acc2);background:rgba(255,255,255,.9);padding:1px 6px;border-radius:7px;white-space:nowrap}
 .ab-scale{display:flex;justify-content:space-between;font-size:10px;font-weight:700;color:var(--mut);margin-top:4px;padding:0 4px}
 .cat-chip .cat-ico-w{width:30px;height:30px;display:flex;align-items:center;justify-content:center}
 .cat-chip .cat-ico-w svg{width:26px;height:26px;stroke:currentColor}
 /* --- Undo snackbar --- */
 .undo-bar{position:fixed;bottom:calc(env(safe-area-inset-bottom,0px) + 20px);left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:999px;background:rgba(20,30,51,.95);backdrop-filter:blur(12px);color:#E8EEF7;font-size:14px;font-weight:600;display:flex;align-items:center;gap:12px;box-shadow:0 4px 20px rgba(0,0,0,.3);z-index:5000;animation:undoIn .3s cubic-bezier(.34,1.56,.64,1)}
 .undo-bar button{background:none;border:none;color:#FF8A5B;font-weight:700;font-size:14px;cursor:pointer}
 @keyframes undoIn{from{opacity:0;transform:translateX(-50%) translateY(20px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
 /* --- Feed (full-page, Instagram-style) --- */
 .feed-page{position:fixed;inset:0;z-index:3000;background:var(--page);transform:translateX(100%);transition:transform .35s cubic-bezier(.32,.72,.42,1);display:flex;flex-direction:column;will-change:transform}
 .feed-page.open{transform:translateX(0)}
 .feed-nav{display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-bottom:1px solid var(--hair);position:sticky;top:0;z-index:2;padding-top:calc(12px + env(safe-area-inset-top,0px))}
 .feed-back{background:none;border:none;cursor:pointer;padding:6px;display:flex;align-items:center;justify-content:center;color:var(--fg);border-radius:8px}
 .feed-back:hover{background:rgba(0,0,0,.04)}
 .feed-title{font-size:22px;font-weight:800;color:var(--fg);letter-spacing:-.04em;flex:1}
 .feed-filter{display:flex;gap:6px;padding:10px 16px;overflow-x:auto;scrollbar-width:none;background:transparent;border-bottom:none}
 .feed-filter::-webkit-scrollbar{display:none}
 .feed-filter button{padding:7px 16px;border-radius:999px;border:1px solid var(--hair);background:var(--card);font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;white-space:nowrap;flex-shrink:0;transition:all .15s var(--ease);font-family:inherit;display:flex;align-items:center;gap:6px;box-shadow:var(--elev1)}
 .feed-filter button .cat-ico{width:16px;height:16px;display:flex;align-items:center;justify-content:center}
 .feed-filter button .cat-ico svg{width:14px;height:14px}
 .feed-filter button.active{background:var(--fg);color:#fff;border-color:var(--fg)}
 .feed-filter button.active .cat-ico svg{stroke:#fff}
 .feed-loc{display:flex;gap:8px;padding:0 16px 12px;overflow-x:auto;scrollbar-width:none;background:transparent}
 .feed-loc::-webkit-scrollbar{display:none}
 .feed-loc-btn,.feed-loc-sel,.feed-loc-clear{flex-shrink:0;padding:8px 14px;border-radius:999px;border:1.5px solid rgba(0,0,0,.08);background:#fff;font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px}
 .feed-loc-btn svg{width:15px;height:15px}
 .feed-loc-btn.active{background:var(--fg);color:#fff;border-color:var(--fg)}
 .feed-loc-sel{-webkit-appearance:none;appearance:none;padding-right:16px}
 .feed-loc-sel.active{background:var(--acc);color:#fff;border-color:var(--acc)}
 .feed-loc-clear{background:rgba(255,84,112,.1);color:#d03050;border-color:transparent}
 .feed-anchor-bar{padding:0 16px 12px;background:#fff;font-size:13px;color:var(--fg2);font-weight:600;display:flex;align-items:center;gap:6px}
 .feed-anchor-bar b{color:var(--acc2)}
 .feed-card-dist{font-size:11px;font-weight:700;color:var(--acc2);background:rgba(20,20,25,.1);padding:2px 8px;border-radius:999px;margin-left:auto;flex-shrink:0}
 /* Feed scope segmented control */
 .feed-scope{display:flex;gap:6px;padding:10px 16px 10px;overflow-x:auto;scrollbar-width:none;background:transparent}
 .feed-scope::-webkit-scrollbar{display:none}
 .feed-scope button{flex:1;min-width:max-content;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:9px 12px;border-radius:12px;border:none;background:var(--fill);font-size:13px;font-weight:700;color:var(--mut);cursor:pointer;font-family:inherit;white-space:nowrap;transition:.18s var(--ease)}
 .feed-scope button svg{width:16px;height:16px}
 .feed-scope button.active{background:var(--fg);color:#fff}
 /* Feed group chips row */
 .feed-groups{display:flex;gap:8px;padding:0 16px 12px;overflow-x:auto;scrollbar-width:none;background:transparent}
 .feed-groups::-webkit-scrollbar{display:none}
 .feed-groups button{flex-shrink:0;padding:8px 14px;border-radius:999px;border:1.5px solid rgba(0,0,0,.08);background:#fff;font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px}
 .feed-groups button.active{background:var(--acc);color:#fff;border-color:var(--acc)}
 .feed-groups button.manage{background:rgba(0,0,0,.04);border-color:transparent;color:var(--fg2)}
 /* Like + follow on cards */
 .endorse-btn .endorse-lbl{font-weight:700}
 .feed-card-actions button.endorsed{color:#0a8f4f}
 .feed-card-actions button.endorsed svg{stroke:#0a8f4f}
 .feed-follow{margin-left:auto;flex-shrink:0;padding:5px 12px;border-radius:999px;border:1.5px solid var(--acc);background:none;color:var(--acc);font-size:12px;font-weight:700;cursor:pointer;font-family:inherit}
 .feed-follow.following{background:var(--acc);color:#fff}
 .feed-card-group{font-size:11px;font-weight:700;color:#7b1fa2;background:rgba(156,39,176,.1);padding:2px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:4px}
 /* Groups modal */
 .groups-modal{position:fixed;inset:0;z-index:3600;background:rgba(11,17,32,.5);backdrop-filter:blur(4px);display:flex;flex-direction:column;justify-content:flex-end}
 .groups-sheet{background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:80vh;display:flex;flex-direction:column;padding-bottom:calc(env(safe-area-inset-bottom,0px) + 12px);box-shadow:var(--elev3)}
 .groups-head{display:flex;align-items:center;justify-content:space-between;padding:18px 20px 12px;font-size:19px;font-weight:800;color:var(--fg)}
 .groups-head button{background:none;border:none;font-size:20px;color:var(--mut);cursor:pointer}
 .groups-new{display:flex;gap:8px;padding:0 20px 14px}
 .groups-new input{flex:1;padding:12px 14px;border:1.5px solid rgba(0,0,0,.1);border-radius:12px;font-size:14px;font-family:inherit;outline:none}
 .groups-new input:focus{border-color:var(--acc)}
 .groups-new button{padding:12px 16px;border-radius:12px;border:none;background:var(--fg);color:#fff;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}
 .groups-list{overflow-y:auto;padding:0 20px 8px}
 .groups-row{display:flex;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid rgba(0,0,0,.05)}
 .groups-row .gr-name{flex:1;font-size:15px;font-weight:600;color:var(--fg)}
 .groups-row .gr-meta{font-size:12px;color:var(--mut);font-weight:500}
 .groups-row button{padding:7px 14px;border-radius:999px;border:1.5px solid var(--acc);background:none;color:var(--acc);font-size:13px;font-weight:700;cursor:pointer;font-family:inherit}
 .groups-row button.joined{background:var(--acc);color:#fff}
 .groups-empty{text-align:center;color:var(--mut);padding:30px;font-size:14px}
 /* Comments sheet */
 .cmt-modal{position:fixed;inset:0;z-index:3900;background:rgba(11,17,32,.5);backdrop-filter:blur(4px);display:flex;flex-direction:column;justify-content:flex-end}
 .cmt-sheet{background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:82vh;display:flex;flex-direction:column;box-shadow:var(--elev3)}
 .sheet-grab::before,.cmt-sheet::before,.prof-sheet::before,.loc-picker-sheet::before{content:'';display:block;width:38px;height:4px;border-radius:2px;background:rgba(22,21,46,.16);margin:8px auto 0;flex-shrink:0}
 @media(min-width:561px){.prof-sheet::before{display:none}}
 .cmt-head{display:flex;align-items:center;justify-content:space-between;padding:12px 20px 12px;font-size:18px;font-weight:800;color:var(--fg);border-bottom:1px solid var(--hair)}
 .cmt-head button{background:none;border:none;font-size:19px;color:var(--mut);cursor:pointer}
 .cmt-list{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:12px 16px;min-height:120px}
 .cmt-row{display:flex;gap:10px;padding:8px 0}
 .cmt-av{width:32px;height:32px;border-radius:50%;background:var(--fg);color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0}
 .cmt-b{flex:1;min-width:0}
 .cmt-u{font-size:13px;font-weight:700;color:var(--fg)}
 .cmt-t{font-size:14px;color:var(--fg2);line-height:1.4}
 .cmt-time{font-size:11px;color:var(--mut);margin-top:2px;font-weight:500}
 .cmt-empty{text-align:center;color:var(--mut);padding:30px 20px;font-size:14px}
 .cmt-input{display:flex;gap:8px;padding:12px 16px calc(env(safe-area-inset-bottom,0px) + 12px);border-top:1px solid var(--hair);background:transparent}
 .cmt-input input{flex:1;padding:12px 16px;border:1.5px solid var(--hair);border-radius:999px;font-size:15px;font-family:inherit;outline:none;background:var(--fill)}
 .cmt-input input:focus{border-color:var(--acc);background:var(--card);box-shadow:0 0 0 3px var(--glow)}
 .cmt-input button{width:46px;height:46px;border-radius:50%;border:none;background:var(--acc);color:#fff;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center}
 .cmt-input button svg{width:20px;height:20px}
 .cmt-input button:active{transform:scale(.92)}
 /* Profile sheet */
 .prof-modal{position:fixed;inset:0;z-index:5200;background:rgba(11,17,32,.5);backdrop-filter:blur(6px);display:flex;flex-direction:column;justify-content:flex-end}
 @media(min-width:561px){.prof-modal{justify-content:center;align-items:center;padding:16px}}
 .prof-sheet{background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:88vh;overflow-y:auto;box-shadow:var(--elev3)}
 @media(min-width:561px){.prof-sheet{border-radius:var(--r-xl);width:100%;max-width:420px}}
 .prof-head{display:flex;align-items:center;justify-content:space-between;padding:16px 20px 8px;font-size:19px;font-weight:800;color:var(--fg)}
 .prof-head button{background:none;border:none;font-size:19px;color:var(--mut);cursor:pointer}
 .prof-body{padding:14px 24px calc(env(safe-area-inset-bottom,0px) + 30px)}
 .prof-top{display:flex;align-items:center;gap:16px;margin-bottom:18px}
 .prof-av{position:relative;width:76px;height:76px;border-radius:50%;background:var(--fg);color:#fff;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:800;cursor:pointer;flex-shrink:0;overflow:hidden;background-size:cover;background-position:center}
 .prof-av .prof-av-edit{position:absolute;right:-2px;bottom:-2px;width:28px;height:28px;border-radius:50%;background:var(--acc);color:#fff;display:flex;align-items:center;justify-content:center;border:2.5px solid #fff}
 .prof-av .prof-av-edit svg{width:14px;height:14px}
 .prof-av.has-img span{display:none}
 .prof-av.has-img .prof-av-edit{display:none}
 .prof-meta{min-width:0}
 .prof-name{font-size:20px;font-weight:800;color:var(--fg);letter-spacing:-.02em}
 .prof-endo{font-size:14px;color:var(--acc2);font-weight:700;margin-top:2px}
 .prof-lbl{display:flex;justify-content:space-between;font-size:13px;font-weight:700;color:var(--fg2);margin-bottom:6px}
 .prof-cnt{color:var(--mut);font-weight:600}
 .prof-cnt.over{color:#d03050}
 .prof-bio{width:100%;box-sizing:border-box;padding:12px 14px;border:1.5px solid var(--hair);border-radius:14px;font-size:15px;font-family:inherit;resize:none;outline:none;background:var(--fill);color:var(--fg);min-height:96px}
 .prof-bio:focus{border-color:var(--acc);background:var(--card);box-shadow:0 0 0 3px var(--glow)}
 .prof-row{display:flex;align-items:center;justify-content:space-between;margin-top:16px;font-size:15px;font-weight:600;color:var(--fg)}
 .prof-toggle{width:50px;height:30px;border-radius:999px;border:none;background:rgba(22,21,46,.15);cursor:pointer;position:relative;transition:background .2s;flex-shrink:0}
 .prof-toggle span{position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:#fff;transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
 .prof-toggle.on{background:var(--acc)}
 .prof-toggle.on span{left:23px}
 .prof-save{width:100%;margin-top:20px;padding:15px;border-radius:15px;border:none;background:var(--acc);color:#fff;font-size:16px;font-weight:800;cursor:pointer;font-family:inherit}
 .prof-save:hover{background:var(--acc2)}
 .prof-feedback{width:100%;margin-top:10px;padding:13px;border-radius:14px;border:1px solid var(--bd);background:none;color:var(--acc2);font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}
 .prof-signout{width:100%;margin-top:10px;padding:13px;border-radius:14px;border:none;background:none;color:#d03050;font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}
 .prof-bioview{font-size:14px;color:var(--fg2);line-height:1.55;margin-top:4px;white-space:pre-wrap;min-height:12px}
 /* --- Public user profile (hero layout) --- */
 .uv-sheet{overflow:hidden}
 #userViewModal{justify-content:stretch;align-items:stretch;padding:0}
 #userViewModal .prof-sheet{width:100%;max-width:none;height:100%;max-height:none;border-radius:0;display:flex;flex-direction:column}
 #userViewModal .uv-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch}
 .uv-posts{margin-top:18px;text-align:left}
 .uv-posts h3{font-size:13px;font-weight:800;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px}
 .uv-post{background:var(--card);border:1px solid var(--hair);border-radius:14px;margin-bottom:10px;overflow:hidden;cursor:pointer;box-shadow:var(--elev1)}
 .uv-post img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block}
 .uv-post .b{padding:10px 12px;font-size:13px;color:var(--fg2)}
 .uv-post .b b{color:var(--fg)}
 .uv-post .t{font-size:11px;color:var(--mut);font-weight:600;margin-top:3px}
 .uv-hero{position:relative;height:86px;padding-top:env(safe-area-inset-top,0px);background:linear-gradient(140deg,#0c0c0f 0%,#26262c 45%,#26262c 80%,#3c3c42 100%)}
 .uv-hero::after{content:'';position:absolute;inset:0;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 90'%3E%3Cpath d='M0 90 60 42 110 68 180 22 240 58 300 30 400 74 400 90Z' fill='rgba(255,255,255,.10)'/%3E%3C/svg%3E") bottom/cover no-repeat}
 .uv-close{position:absolute;top:calc(env(safe-area-inset-top,0px) + 10px);right:10px;z-index:1;width:30px;height:30px;border-radius:10px;border:none;background:rgba(255,255,255,.22);color:#fff;font-size:15px;cursor:pointer;backdrop-filter:blur(4px)}
 .uv-body{text-align:center;padding-top:0!important}
 .uv-av{margin:-38px auto 10px;width:84px;height:84px;font-size:30px;box-shadow:0 0 0 4px var(--glass2),var(--elev2)}
 .uv-name{font-size:21px;font-weight:800;color:var(--fg);letter-spacing:-.02em}
 .uv-bio{font-size:14px;color:var(--fg2);line-height:1.5;margin:6px auto 0;max-width:290px;white-space:pre-wrap}
 .uv-bio:empty{display:none}
 .uv-stats{display:flex;justify-content:center;gap:8px;margin:16px 0 4px}
 .uv-stat{flex:1;max-width:110px;background:var(--fill);border:1px solid var(--hair);border-radius:14px;padding:10px 6px}
 .uv-stat b{display:block;font-size:19px;font-weight:800;color:var(--fg);letter-spacing:-.02em}
 .uv-stat span{font-size:10.5px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 .uv-report{width:100%;margin-top:10px;border:none;background:none;color:var(--mut);font-size:13px;font-weight:700;font-family:inherit;cursor:pointer;padding:9px;text-decoration:underline;text-underline-offset:3px}
 /* --- Settings items (own profile) --- */
 .prof-view{animation:qrFade .22s var(--ease) both}
 .prof-item .chev{margin-left:auto;color:var(--mut);font-size:17px;font-weight:600}
 .prof-back{border:none;background:none;color:var(--fg2);font-size:14px;font-weight:750;font-family:inherit;cursor:pointer;padding:8px 0 2px;margin-left:-2px;display:block}
 .prof-hint{font-size:12px;color:var(--mut);margin-top:12px;line-height:1.55}
 .prof-seg{display:flex;background:var(--fill);border:1px solid var(--hair);border-radius:13px;padding:3px;gap:3px;margin-bottom:4px}
 .prof-seg button{flex:1;border:none;background:none;padding:10px 6px;border-radius:10px;font-size:13px;font-weight:700;color:var(--mut);font-family:inherit;cursor:pointer;transition:.15s}
 .prof-seg button.active{background:#fff;color:var(--fg);box-shadow:var(--elev1)}
 .prof-sec-title{margin:24px 2px 10px;font-size:12px;font-weight:800;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
 .prof-item{width:100%;display:flex;align-items:center;gap:11px;padding:13px 14px;margin-bottom:8px;border-radius:14px;border:1px solid var(--hair);background:var(--fill);color:var(--fg);font-size:14.5px;font-weight:650;font-family:inherit;cursor:pointer;text-align:left;transition:.15s}
 .prof-item svg{width:19px;height:19px;flex-shrink:0;color:var(--acc2)}
 .prof-item:hover{background:var(--card)}
 .prof-item.danger{color:#b3261e}
 .prof-item.danger svg{color:var(--danger)}
 .prof-save.following{background:var(--fg)}
 .feed-card-avatar,.feed-card-user{cursor:pointer}
 /* Location search picker (Gipfel / Gebiet) */
 .loc-picker{position:fixed;inset:0;z-index:3800;background:rgba(11,17,32,.5);backdrop-filter:blur(4px);display:flex;flex-direction:column;justify-content:flex-end}
 .loc-picker-sheet{background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:80vh;display:flex;flex-direction:column;padding-bottom:calc(env(safe-area-inset-bottom,0px) + 8px);box-shadow:var(--elev3)}
 .loc-picker-head{display:flex;align-items:center;gap:10px;padding:12px 18px 12px;border-bottom:1px solid var(--hair)}
 .loc-picker-head svg{width:20px;height:20px;color:var(--mut);flex-shrink:0}
 .loc-picker-head input{flex:1;border:none;outline:none;font-size:17px;font-family:inherit;color:var(--fg);background:none}
 .loc-picker-head input::placeholder{color:var(--mut)}
 .loc-picker-head button{background:none;border:none;font-size:19px;color:var(--mut);cursor:pointer;width:30px;flex-shrink:0}
 .loc-picker-list{overflow-y:auto;-webkit-overflow-scrolling:touch;padding:6px}
 .loc-picker-list button{display:flex;align-items:center;width:100%;gap:10px;padding:13px 14px;border:none;background:none;border-radius:12px;cursor:pointer;font-family:inherit;font-size:15px;font-weight:600;color:var(--fg);text-align:left}
 .loc-picker-list button:hover{background:rgba(22,21,46,.05)}
 .loc-picker-list button .lp-e{margin-left:auto;font-size:12.5px;font-weight:600;color:var(--mut)}
 .loc-picker-list .lp-empty{text-align:center;color:var(--mut);padding:30px;font-size:14px}
 .feed-scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding-bottom:env(safe-area-inset-bottom,0px)}
 .feed-grid{max-width:600px;margin:0 auto;padding:10px 0 120px}
 .feed-card{background:var(--card);margin:0 14px 20px;border-radius:22px;border:1px solid var(--hair);box-shadow:var(--elev1);overflow:hidden;cursor:pointer;transition:box-shadow .25s var(--ease),transform .25s var(--ease)}
 .feed-card:hover{box-shadow:var(--elev2)}
 @media(hover:none){.feed-card:active{transform:scale(.992)}}
 .feed-card.enter{animation:cardIn .5s var(--ease) both}
 @keyframes cardIn{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
 .feed-card.flash{box-shadow:0 0 0 3px var(--acc) inset;animation:cardFlash 1.8s ease}
 @keyframes cardFlash{0%,100%{background:#fff}30%{background:rgba(20,20,25,.08)}}
 .feed-card-head{display:flex;align-items:center;gap:11px;padding:14px 16px 10px}
  .feed-card-avatar{width:42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;flex-shrink:0;letter-spacing:.02em;color:#fff;box-shadow:0 0 0 2px var(--card),0 0 0 3.5px var(--ring)}
 .feed-card-info{flex:1;min-width:0}
 .feed-card-user{font-size:14px;font-weight:700;color:var(--fg);letter-spacing:-.01em;display:block}
 .feed-card-loc{font-size:12px;color:var(--mut);font-weight:500;display:flex;align-items:center;gap:3px}
 .feed-card-time{font-size:12px;color:var(--mut);font-weight:500;flex-shrink:0}
 .feed-card-visual{margin:2px 14px 0;aspect-ratio:4/3;position:relative;overflow:hidden;background:var(--fill2);border-radius:16px}
 .feed-card-visual img{width:100%;height:100%;object-fit:cover}
 .feed-card-visual .card-placeholder{width:100%;height:100%;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px}
 .feed-card-visual .card-placeholder>span:first-child svg{width:56px;height:56px;opacity:.5}
 .feed-card-visual .card-placeholder>span:last-child{font-size:15px;font-weight:700;opacity:.45;letter-spacing:.02em}
 .feed-card-body{padding:12px 18px 4px}
 .feed-card-badges{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
 .feed-badge{padding:5px 13px;border-radius:999px;font-size:12.5px;font-weight:650;display:flex;align-items:center;gap:6px}
 .feed-badge .cat-ico{width:14px;height:14px;display:flex;align-items:center;justify-content:center}
 .feed-badge .cat-ico svg{width:13px;height:13px}
 .feed-badge.cat-snow{background:rgba(56,152,236,.1);color:#141419}
 .feed-badge.cat-snow svg{stroke:#141419}
 .feed-badge.cat-route{background:rgba(76,175,80,.1);color:#2e7d32}
 .feed-badge.cat-route svg{stroke:#2e7d32}
 .feed-badge.cat-danger{background:rgba(255,84,112,.1);color:#d03050}
 .feed-badge.cat-danger svg{stroke:#d03050}
 .feed-badge.cat-tour{background:rgba(156,39,176,.1);color:#7b1fa2}
 .feed-badge.cat-tour svg{stroke:#7b1fa2}
 .feed-badge.cat-info{background:rgba(255,152,0,.1);color:#e65100}
 .feed-badge.cat-info svg{stroke:#e65100}
 .feed-badge.cat-avalanche{background:rgba(208,48,80,.1);color:#d03050}.feed-badge.cat-avalanche svg{stroke:#d03050}
 .feed-badge.cat-whumpf{background:rgba(232,89,12,.1);color:#e8590c}.feed-badge.cat-whumpf svg{stroke:#e8590c}
 .feed-badge.cat-wind_slab{background:rgba(13,148,136,.1);color:#0d9488}.feed-badge.cat-wind_slab svg{stroke:#0d9488}
 .feed-badge.cat-other{background:rgba(20,20,25,.1);color:#141419}.feed-badge.cat-other svg{stroke:#141419}
 .feed-card-caption{font-size:14.5px;color:var(--fg2);line-height:1.55;margin-top:2px}
 .feed-card-caption b{color:var(--fg);font-weight:700}
 .feed-card-actions{display:flex;align-items:center;gap:20px;padding:12px 18px 13px;border-top:1px solid var(--hair);margin-top:10px}
 .feed-card-actions button{background:none;border:none;cursor:pointer;padding:4px;color:var(--fg2);display:flex;align-items:center;gap:5px;font-size:13px;font-weight:600;font-family:inherit}
 .feed-card-actions button svg{width:20px;height:20px}
 .feed-card-actions button:hover{color:var(--fg)}
 .feed-card-actions .flag-btn{margin-left:auto;color:var(--mut)}
 .feed-card-actions .flag-btn:hover{color:var(--danger)}
 .feed-card-actions .flag-btn.flagged{color:var(--danger)}
 .feed-card-actions .cond-btn.rated{color:var(--acc2)}
 .feed-card-actions .cond-btn.rated svg{color:var(--acc)}
 .cond-chip{font-size:12px;font-weight:800;color:var(--acc2);background:rgba(20,20,25,.1);padding:3px 10px;border-radius:999px;display:inline-flex;align-items:center;gap:4px}
 .cond-chip svg{width:13px;height:13px;fill:#f5a623}
 /* condition rating modal */
 .cond-modal{position:fixed;inset:0;z-index:3950;background:rgba(11,17,32,.5);backdrop-filter:blur(4px);display:flex;flex-direction:column;justify-content:flex-end}
 @media(min-width:561px){.cond-modal{justify-content:center;align-items:center;padding:16px}}
 .cond-sheet{background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-radius:var(--r-xl) var(--r-xl) 0 0;box-shadow:var(--elev3);padding-bottom:calc(env(safe-area-inset-bottom,0px) + 18px)}
 @media(min-width:561px){.cond-sheet{border-radius:var(--r-xl);width:100%;max-width:380px}}
 .cond-sheet::before{content:'';display:block;width:38px;height:4px;border-radius:2px;background:rgba(22,21,46,.16);margin:8px auto 0}
 .cond-head{display:flex;align-items:center;justify-content:space-between;padding:8px 20px 0}
 .cond-head b{font-size:18px;font-weight:800;color:var(--fg);letter-spacing:-.01em}
 .cond-head button{background:none;border:none;font-size:19px;color:var(--mut);cursor:pointer}
 .cond-body{padding:2px 20px 4px}
 .cond-lbl{font-size:13px;font-weight:700;color:var(--fg2);margin:16px 0 8px}
 .cond-stars{display:flex;gap:8px;justify-content:center}
 .cond-stars button{background:none;border:none;cursor:pointer;padding:2px}
 .cond-stars button svg{width:40px;height:40px;fill:var(--fill2);stroke:none;transition:transform .12s var(--ease-spring),fill .12s}
 .cond-stars button.on svg{fill:#f5a623}
 .cond-stars button:active svg{transform:scale(.88)}
 .cond-pow{display:flex;gap:10px}
 .cond-pow button{flex:1;padding:13px;border-radius:14px;border:1.5px solid var(--hair);background:var(--fill);font-family:inherit;font-size:15px;font-weight:700;color:var(--fg2);cursor:pointer;transition:.14s}
 .cond-pow button.on{background:var(--acc);border-color:var(--acc);color:#fff}
 .cond-save{width:100%;margin-top:18px;padding:15px;border-radius:15px;border:none;background:var(--acc);color:#fff;font-size:16px;font-weight:800;cursor:pointer;font-family:inherit;transition:.15s}
 .cond-save:hover{background:var(--acc2)}
 .cond-save:disabled{opacity:.4;cursor:default}
 /* Quick-Check entry points */
 .qr-padwrap{display:flex;gap:6px;margin-top:12px;align-items:stretch}
 .qr-ylab{display:flex;align-items:center;justify-content:center;flex-shrink:0;width:20px;font-size:11px;font-weight:800;color:var(--mut);letter-spacing:.05em;writing-mode:vertical-rl;transform:rotate(180deg);text-transform:uppercase;white-space:nowrap}
 .qr-pad{position:relative;flex:1;aspect-ratio:1.18;border-radius:16px;border:1px solid var(--hair);background:linear-gradient(to right,rgba(20,20,25,.10),rgba(20,20,25,.03) 55%,#fff);cursor:crosshair;overflow:hidden}
 .qr-qline{position:absolute;top:0;bottom:0;width:0;border-left:2px dashed rgba(20,20,25,.30);pointer-events:none}
 .qr-xbands{display:flex;margin-top:8px;padding-left:24px}
 .qr-xband{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;text-align:center;font-size:10.5px;font-weight:800;line-height:1.18;letter-spacing:.01em}
 .qr-xband svg{width:21px;height:21px}
 .qr-xband:nth-child(1){color:#9a9aa2}
 .qr-xband:nth-child(2){color:#6e6e76}
 .qr-xband:nth-child(3){color:#3c3c42}
 .qr-xband:nth-child(4){color:#000}
 .qr-corner{position:absolute;font-size:10.5px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;color:var(--mut);pointer-events:none}
 .qr-corner.tr{top:8px;right:10px;color:var(--acc2)}
 .qr-corner.bl{bottom:8px;left:10px}
 .qr-pad-dot{position:absolute;width:24px;height:24px;margin:-12px 0 0 -12px;border-radius:50%;background:var(--acc);border:3.5px solid #fff;box-shadow:var(--elev2);pointer-events:none;transition:left .05s linear,top .05s linear;z-index:2}
 .qr-dot-cm{position:absolute;left:50%;bottom:calc(100% + 7px);transform:translateX(-50%);background:#16152e;color:#fff;font-size:10.5px;font-weight:800;padding:2px 7px;border-radius:8px;white-space:nowrap;box-shadow:var(--elev1)}
 .qr-dot-cm::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:4px solid transparent;border-top-color:#16152e}
 .qr-pad-dot.below .qr-dot-cm{bottom:auto;top:calc(100% + 7px)}
 .qr-pad-dot.below .qr-dot-cm::after{top:auto;bottom:100%;border-top-color:transparent;border-bottom-color:#16152e}
 .qr-read{margin-top:8px;font-size:13px;font-weight:800;color:var(--fg2);text-align:center;min-height:18px}
 .qr-step{animation:qrFade .35s var(--ease) both}
 @keyframes qrFade{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
 .qr-cmline{position:absolute;left:0;right:0;height:0;border-top:1.5px dashed rgba(20,20,25,.15);pointer-events:none}
 .qr-cmline i{position:absolute;top:-12px;right:5px;font-style:normal;font-size:9px;font-weight:800;color:var(--mut)}
 .qr-axis-x{display:flex;align-items:center;justify-content:center;gap:8px;font-size:11.5px;font-weight:800;color:var(--fg2);margin:8px 0 2px;letter-spacing:.02em}
 .qr-axis-x svg{color:var(--mut)}
 .qr-photo-one{display:flex;align-items:center;justify-content:center;gap:10px;padding:20px 12px;border-radius:14px;border:1.5px dashed rgba(20,20,25,.4);background:rgba(20,20,25,.06);color:var(--acc2);font-size:14.5px;font-weight:750;cursor:pointer;margin-top:8px}
 .qr-photo-one svg{width:26px;height:26px}
 .qr-photo-row{display:flex;gap:10px;margin-top:12px}
 .qr-photo-btn{flex:1;display:flex;flex-direction:column;align-items:center;gap:7px;padding:16px 10px;border-radius:14px;border:1.5px dashed rgba(20,20,25,.4);background:rgba(20,20,25,.06);color:var(--acc2);font-size:13px;font-weight:750;cursor:pointer;text-align:center}
 .qr-photo-btn svg{width:24px;height:24px}
 .qr-photo-btn.lib{border-style:solid;border-color:var(--hair);background:var(--fill);color:var(--fg2)}
 .qr-prev{width:100%;max-height:150px;object-fit:cover;border-radius:14px;margin-top:10px}
 .qr-adj{display:block;margin:12px auto 0;border:none;background:none;color:var(--acc2);font-size:13px;font-weight:750;font-family:inherit;cursor:pointer}
 .qr-skip{width:100%;margin-top:8px;border:none;background:none;color:var(--mut);font-size:13.5px;font-weight:700;font-family:inherit;cursor:pointer;padding:9px}
 #mapFab{position:fixed;left:auto;right:14px;transform:none;bottom:calc(var(--btm-h,90px) + 14px);width:56px;height:56px;z-index:900}
 #mapFab:active{transform:scale(.9)}
 #mapFab span{display:none}
 #mapQr{position:fixed;left:auto;right:14px;transform:none;bottom:calc(var(--btm-h,90px) + 94px);z-index:900}
 #mapQr:active{transform:scale(.9)}
 .feed-qr{position:absolute;bottom:calc(env(safe-area-inset-bottom, 0px) + 108px);left:auto;right:16px;transform:none;width:56px;height:56px;padding:0;border-radius:50%;border:1.5px solid rgba(10,10,12,.9);background:#fff;color:#0a0a0c;display:flex;align-items:center;justify-content:center;cursor:pointer;font-family:inherit;box-shadow:0 10px 26px rgba(0,0,0,.22);z-index:5;transition:transform .18s cubic-bezier(.34,1.56,.64,1)}
 .feed-qr svg{width:24px;height:24px}
 .feed-qr span{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);font-size:10px;font-weight:800;letter-spacing:.03em;color:var(--fg2);text-shadow:0 1px 2px rgba(255,255,255,.85);white-space:nowrap}
 .feed-qr:active{transform:scale(.92)}
 .qr-loc{font-size:12.5px;font-weight:650;color:var(--fg2);background:var(--fill);border:1px solid var(--hair);border-radius:10px;padding:8px 11px;margin-top:12px}
 .rail-btn.qr-accent{background:linear-gradient(150deg,rgba(245,166,35,.24),rgba(245,166,35,.1));color:#b06a00;border-color:rgba(245,166,35,.4)}
 .feed-quick{margin-left:auto;display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:999px;border:1px solid rgba(245,166,35,.45);background:linear-gradient(150deg,rgba(245,166,35,.2),rgba(245,166,35,.08));color:#8a5200;font-size:13px;font-weight:800;font-family:inherit;cursor:pointer;transition:.15s}
 .feed-quick svg{width:15px;height:15px}
 .feed-quick:active{transform:scale(.95)}
 .obs-quick{width:100%;display:flex;align-items:center;gap:12px;padding:13px 15px;margin:2px 0 4px;border-radius:16px;border:1px solid rgba(245,166,35,.4);background:linear-gradient(140deg,rgba(245,166,35,.18),rgba(255,255,255,.85) 70%);font-family:inherit;cursor:pointer;text-align:left;box-shadow:var(--elev1);transition:.15s;animation:obsPop .5s var(--ease-spring) both}
 .obs-quick svg{width:24px;height:24px;color:#b06a00;flex-shrink:0}
 .obs-quick b{display:block;font-size:15px;font-weight:800;color:var(--fg)}
 .obs-quick em{font-style:normal;font-size:12px;color:var(--mut);font-weight:600}
 .obs-quick:hover{box-shadow:var(--elev2)}
 /* first-time explainer tooltip */
 .cond-hint{position:fixed;z-index:4200;max-width:236px;background:var(--fg);color:#fff;font-size:12.5px;font-weight:600;line-height:1.45;padding:11px 13px;border-radius:14px;box-shadow:var(--elev3);animation:toastIn .3s var(--ease-spring)}
 .cond-hint b{color:#7cc4ff}
 .cond-hint::after{content:'';position:absolute;bottom:-6px;left:50%;margin-left:-7px;width:14px;height:14px;background:var(--fg);transform:rotate(45deg);border-radius:2px}
 .cond-hint.below::after{bottom:auto;top:-6px}
 .feed-divider{height:1px;background:rgba(0,0,0,.06)}
 .feed-card-visual{position:relative}
 .dbl-heart{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;animation:dblHeart .72s var(--ease) both}
 .dbl-heart svg{width:84px;height:84px;filter:drop-shadow(0 4px 16px rgba(0,0,0,.35))}
 @keyframes dblHeart{0%{opacity:0;transform:scale(.4)}25%{opacity:1;transform:scale(1.12)}55%{opacity:1;transform:scale(1)}100%{opacity:0;transform:scale(1.15)}}
 .feed-card-actions .save-btn.saved{color:var(--fg)}
 .us-list{max-height:52vh;overflow-y:auto;-webkit-overflow-scrolling:touch;margin-top:8px}
 .us-row{display:flex;align-items:center;gap:12px;padding:10px 4px;border-bottom:1px solid var(--hair);cursor:pointer}
 .us-row .av{width:42px;height:42px;border-radius:50%;background:#16152e center/cover;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:16px;flex-shrink:0}
 .us-row b{flex:1;font-size:15px;color:var(--fg)}
 .us-row .feed-follow{flex-shrink:0}
 .us-empty{color:var(--mut);font-size:13px;text-align:center;padding:18px 0}
 .feed-empty{text-align:center;padding:80px 20px;color:var(--mut);font-size:15px}
 /* Instagram-style centered create button at the very bottom of the feed */
 .feed-fab{position:absolute;bottom:calc(env(safe-area-inset-bottom, 0px) + 18px);left:auto;right:16px;transform:none;width:56px;height:56px;padding:0;border-radius:50%;border:none;background:linear-gradient(140deg,#3c3c42,#141419 55%,#000000);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;font-family:inherit;box-shadow:0 10px 28px rgba(20,20,25,.45),0 0 0 5px rgba(255,255,255,.75);z-index:5;transition:transform .18s cubic-bezier(.34,1.56,.64,1),box-shadow .18s}
 .feed-fab svg{width:24px;height:24px}
 .feed-fab span{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);font-size:10px;font-weight:800;letter-spacing:.03em;color:var(--fg2);text-shadow:0 1px 2px rgba(255,255,255,.8)}
 .feed-fab:active{transform:scale(.9)}
 .feed-fab:hover{box-shadow:0 12px 32px rgba(20,20,25,.55),0 0 0 5px rgba(255,255,255,.85)}
 /* Text-only posts: first-class "quote" cards with a category tint wash */
 .feed-tx{position:relative;margin:2px 16px 0;padding:16px 18px 15px 22px;border-radius:16px;font-size:17px;line-height:1.5;font-weight:550;color:var(--fg);letter-spacing:-.012em;display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}
 .feed-tx-bar{position:absolute;left:8px;top:14px;bottom:14px;width:4px;border-radius:2px;opacity:.75}
 .feed-card.text .feed-card-actions{margin-top:8px}
 @media(min-width:561px){.feed-grid{padding:18px 0 120px}.feed-card{margin:0 0 20px}}
 /* --- Report markers --- */
 .rpt-marker{width:36px;height:36px;border-radius:50%;background:#fff;border:2.5px solid currentColor;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.15);cursor:pointer;transition:transform .15s}
 .rpt-marker svg{width:18px;height:18px}
 .rpt-marker:hover{transform:scale(1.15)}
 @media(prefers-reduced-motion:reduce){.cat-chip,.sub-chip,.bucket,.slide-knob,.radial-seg{transition:none!important}}
</style></head><body>
<div id="intro"><div class="fall"><svg class="flake" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M24 3v42M5.8 13.5l36.4 21M42.2 13.5L5.8 34.5"/><path d="M24 9l4.5-4.5M24 9l-4.5-4.5M24 39l4.5 4.5M24 39l-4.5 4.5"/><path d="M10 16.9l-6.2-1.2M10 16.9l1.2-6.2M38 31.1l6.2 1.2M38 31.1l-1.2 6.2"/><path d="M38 16.9l6.2-1.2M38 16.9l-1.2-6.2M10 31.1l-6.2 1.2M10 31.1l1.2 6.2"/><circle cx="24" cy="24" r="3.2"/><path d="M24 15.5l3 3M24 15.5l-3 3M24 32.5l3-3M24 32.5l-3-3M16.6 19.7l1.1 4.1M31.4 19.7l-1.1 4.1M16.6 28.3l4.1-1.1M31.4 28.3l-4.1 1.1"/></svg></div><div class="track"><div class="bar"></div></div><div class="sub"></div></div>
<div id="toastWrap" aria-live="polite"></div>
<div id="disc"><div class="sheet" role="dialog" aria-modal="true" aria-labelledby="discH"><div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></div><h2 id="discH">Bevor du startest</h2><p><b>Experimentelle Modelldaten.</b> Diese App zeigt modellierte Schnee-, Pulver- und Skitauglichkeits-Sch&auml;tzungen. Sie ist <b>kein Lawinenbulletin</b> und ersetzt nicht die offizielle Beurteilung des <a href="https://www.slf.ch/de/lawinenbulletin-und-schneesituation.html" target="_blank" rel="noopener">SLF</a> bzw. <a href="https://whiterisk.ch" target="_blank" rel="noopener">White Risk</a>. Entscheidungen im Gel&auml;nde triffst du auf <b>eigenes Risiko</b>.</p><p class="fine">Datenschutz: F&uuml;r Konto, Meldungen und Fotos werden E-Mail, Standort und Bilddaten bei Supabase (EU) gespeichert. Du kannst Konto und Beitr&auml;ge jederzeit l&ouml;schen.</p><label class="chk"><input type="checkbox" id="discChk" onchange="var b=document.getElementById('discBtn');if(b)b.disabled=!this.checked"><span>Ich habe verstanden, dass dies experimentelle Daten sind und kein Lawinenbulletin ersetzt.</span></label><button class="accept" id="discBtn" disabled onclick="acceptDisc()">Verstanden &ndash; loslegen</button></div></div>
<div id="a2hs" onclick="if(event.target===this)a2hsLater()"><div class="sheet" role="dialog" aria-modal="true">
  <div class="a2hs-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="3" x2="12" y2="21"/><line x1="4.2" y1="7.5" x2="19.8" y2="16.5"/><line x1="4.2" y1="16.5" x2="19.8" y2="7.5"/></svg></div>
  <h2>Snow Mapper aufs Home-Screen</h2>
  <p>Als App installiert läuft Snow Mapper im Vollbild (randlos), startet offline mit den letzten Daten und ist einen Fingertipp entfernt.</p>
  <div id="a2hsIos" style="display:none">
    <div class="a2hs-step"><span class="n">1</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg><span>Tippe unten auf das <b>Teilen</b>-Symbol</span></div>
    <div class="a2hs-step"><span class="n">2</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg><span>Wähle <b>Zum Home-Bildschirm</b></span></div>
  </div>
  <div id="a2hsAndroid" style="display:none">
    <div class="a2hs-hint" id="a2hsFallback" style="display:none">Öffne das Browser-Menü (⋮) und wähle <b>App installieren</b>.</div>
    <button class="accept" id="a2hsInstallBtn" onclick="a2hsInstall()">App installieren</button>
  </div>
  <button class="later" onclick="a2hsLater()">Später</button>
</div></div>
<div id="map"></div>
<canvas id="flow"></canvas>
<div id="modeGlow"></div>
<div id="statusScrim"></div>
<div id="statusScrimB"></div>
<div id="inspPanel" class="insp-panel"></div>
<div id="layerBar">
  <div id="topics">
    <button data-t="ski"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M5 20l5-11 4 6 2.5-4L21 20"/><circle cx="15.5" cy="4.5" r="1.5"/></svg><span>Ski</span></button>
    <button data-t="snow" class="active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><line x1="12" y1="3" x2="12" y2="21"/><line x1="5" y1="7.5" x2="19" y2="16.5"/><line x1="5" y1="16.5" x2="19" y2="7.5"/></svg><span>Snow</span></button>
    <button id="moreTopics" title="Weitere Ebenen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h16M4 6h16M4 18h16"/></svg><span>Mehr</span><svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button>
    
  </div>
  <span id="topicsMore">
      <button data-t="temp"><span class="mt-ic" style="background:rgba(232,89,12,.12);color:#e8590c"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 14.76V4a2 2 0 1 0-4 0v10.76a4 4 0 1 0 4 0z"/></svg></span><span class="mt-tx"><b>Temperatur</b><span>Luft &amp; Oberfläche</span></span></button>
      <button data-t="wind"><span class="mt-ic" style="background:rgba(13,148,136,.12);color:#0d9488"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.6 4.6A2 2 0 1 1 11 8H2M12.6 19.4A2 2 0 1 0 14 16H2M17.6 7.6A2.5 2.5 0 1 1 19 12H2"/></svg></span><span class="mt-tx"><b>Wind</b><span>Geschwindigkeit &amp; Böen</span></span></button>
      <button data-t="qpr"><span class="mt-ic" style="background:rgba(30,58,138,.12);color:#1e3a8a"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 2L4.5 13.2c-.4.5 0 1.3.6 1.3H11l-1.4 7.2c-.1.7.8 1.1 1.2.5L20 11.5c.4-.5 0-1.3-.6-1.3H13l1.3-7.7c.1-.7-.8-1.1-1.3-.5z"/></svg></span><span class="mt-tx"><b>Powder-Reports</b><span>Heatmap der Community</span></span></button>
      </span>
  <div id="sublayers"></div>
</div>
<div id="searchWrap"><span class="icn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" style="width:15px;height:15px;display:block"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.2" y2="16.2"/></svg></span><input id="searchIn" type="text" placeholder="Ort suchen…" autocomplete="off"/><div id="searchRes"></div></div>
<div id="ctrlRail">
  <button class="rail-btn" id="accountBtn" onclick="accountTap()" title="Anmelden" aria-label="Konto"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg></button>
  <button class="rail-btn feed-accent" id="feedBtn" onclick="feedOpen()" title="Community-Feed" aria-label="Community-Feed"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg><span class="feed-dot"></span></button>
  <button class="rail-btn active" id="railToggles" onclick="toggleStations()" title="Messstationen ein/aus" aria-label="Messstationen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="21" x2="12" y2="10"/><path d="M8 21h8"/><circle cx="12" cy="7.5" r="2.5"/><path d="M7 4.5a7 7 0 0 1 10 0M9 7a4 4 0 0 1 6 0" stroke-dasharray="0"/></svg></button>
  <button class="rail-btn" id="btn3dFloat" title="3D-Ansicht" aria-label="3D-Ansicht"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2.5" y="5" width="19" height="14" rx="3.5"/><text x="12" y="15.6" text-anchor="middle" font-family="Inter,system-ui,sans-serif" font-size="8.6" font-weight="800" fill="currentColor" stroke="none">3D</text></svg></button>
  <button class="rail-btn" id="locBtn" onclick="flyToMe()" title="Zu meinem Standort" aria-label="Zu meinem Standort"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 2v3.5M12 18.5V22M2 12h3.5M18.5 12H22"/></svg></button>
</div>
<button class="feed-qr" id="mapQr" onclick="qrOpen(event)" title="Quick Powder Report"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 2L4.5 13.2c-.4.5 0 1.3.6 1.3H11l-1.4 7.2c-.1.7.8 1.1 1.2.5L20 11.5c.4-.5 0-1.3-.6-1.3H13l1.3-7.7c.1-.7-.8-1.1-1.3-.5z"/></svg><span>Powder</span></button>
<button class="feed-fab" id="mapFab" onclick="obsOpen()" title="Beobachtung melden"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg><span>Melden</span></button>
<div id="bottomPanel" class="detail">
  <div id="tlToggle"></div>
  <div id="btmMain">
    <div id="tlHead">
      <span class="winlbl" id="window"></span>
      <div id="tlModeToggle">
        <button data-m="simple">Einfach</button>
        <button data-m="detail" class="active">Detail</button>
      </div>
    </div>
    <div class="seg" id="presets" style="gap:5px;margin-top:2px;overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;-webkit-overflow-scrolling:touch">
      <button data-d="24">24h</button>
      <button data-d="48" class="active">48h</button>
      <button data-d="72">72h</button>
      <button data-d="120">120h</button>
      <button id="btnSinceSnow">Letzter Schnee</button>
      <button data-r="tomorrow">Bis morgen</button>
    </div>
    <div id="tlDetail">
      <canvas id="timeline" width="900" height="94" style="width:100%;height:94px;border-radius:10px;cursor:default;margin-top:8px"></canvas>
      <div id="tlExtended">
        <div class="tl-steprow">
          <button id="tlPrev" class="tl-step">◀ Früher</button>
          <button id="tlNow" class="tl-step">Jetzt</button>
          <button id="tlNext" class="tl-step">Später ▶</button>
          <button id="tlLenBtn" class="tl-step tl-len">Fenster: <b id="tlLenVal">48h</b></button>
        </div>
        <input type="range" id="tlLen" min="6" max="168" step="6" value="48" style="display:none"/>
        <div id="tlRange" class="tl-range"></div>
      </div>
    </div>
  </div>
</div>
<button id="legendBtn" title="Legende" aria-label="Legende"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="11" x2="12" y2="16.5"/><circle cx="12" cy="7.6" r="1" fill="currentColor" stroke="none"/></svg></button><div class="legend" id="legend"></div>
<div id="three-wrap"><div id="map3d" style="width:100%;height:100%"></div><button id="btn3dClose">✕ 2D</button>
<div class="ctrl3d"><label style="display:flex;align-items:center;gap:8px;color:var(--fg2);font-size:16px">Relief <input id="mapOpac3d" type="range" min="0" max="100" value="30" style="width:100px;accent-color:var(--acc)"> Map <span id="mapOpacLbl">30%</span></label>
<button id="btn3dExag">×1.5</button>
<select id="overlay3d" style="padding:10px 14px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);font-size:16px;backdrop-filter:blur(10px);min-height:46px"><option value="none">No overlay</option><option value="snow">Snow</option><option value="temp">Temperature</option><option value="wind">Wind</option><option value="depth">Schneehöhe</option><option value="powder">Powder</option></select>
<span id="keys3d" style="font-size:12px;color:var(--mut);opacity:.7;padding:6px 10px;display:none">WASD/Arrows: rotate · +/-: zoom · R: reset</span></div>
</div>
<!-- Auth & Reports UI -->
<div id="userBar"><button class="login-btn" id="btnLogin" onclick="authShow()">Anmelden</button></div>
<div class="email-banner" id="emailBanner"><span>Please confirm your email to post reports.</span><button onclick="authResend()">Resend</button></div>
<div class="radial-wrap" id="radialWrap"><div class="radial-bg"></div><div class="radial-ring" id="radialRing"></div><div class="radial-center" id="radialCenter">✕</div></div>
<div class="auth-overlay" id="authOverlay" style="display:none" onclick="authHide()">
<div class="auth-modal" onclick="event.stopPropagation()">
<button class="auth-close" id="authClose" onclick="authHide()">&times;</button>
<h2 id="authTitle">Anmelden</h2>
<p class="auth-sub" id="authSub">Anmelden für Community &amp; Meldungen</p>
<p class="auth-note">Karte &amp; Prognosen funktionieren ohne Konto — du brauchst es nur zum Melden, für den Feed und dein Profil.</p>
<form id="authForm" onsubmit="authSubmit(event)">
<input type="text" id="authUser" placeholder="Username" autocomplete="username" style="display:none"/>
<input type="email" id="authEmail" placeholder="E-Mail" autocomplete="email" required/>
<input type="password" id="authPass" placeholder="Passwort" autocomplete="current-password" required minlength="8"/>
<div class="auth-err" id="authErr"></div>
<button class="auth-btn primary" type="submit" id="authSubmitBtn">Anmelden</button>
</form>
<div id="authCodeBox" style="display:none">
  <p class="auth-sub" id="authCodeSub">Wir haben dir einen 6-stelligen Code geschickt.</p>
  <input type="text" id="authCode" class="auth-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="––––––" oninput="authCodeInput()"/>
  <button class="auth-paste" type="button" onclick="authPasteCode()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Code aus E-Mail einfügen</button>
  <div class="auth-err" id="authCodeErr"></div>
  <button class="auth-btn primary" id="authCodeBtn" onclick="authVerifyCode()">Bestätigen</button>
  <div class="auth-switch">Kein Code erhalten? <button onclick="authResendCode()">Erneut senden</button> · <button onclick="authBackToForm()">Zurück</button></div>
</div>
<div id="authBioBox" style="display:none">
  <div class="auth-bio-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><path d="M9 10a3 3 0 0 1 6 0M8.5 15.5c1 1 5 1 7 0M12 10v3.5"/></svg></div>
  <p class="auth-sub" style="text-align:center;margin-bottom:14px">Nächstes Mal schneller & sicher mit <b id="authBioName">Biometrie</b> anmelden?</p>
  <button class="auth-btn primary" onclick="authEnableBio()">Aktivieren</button>
  <button class="auth-btn ghost" onclick="authSkipBio()">Später</button>
</div>
<div class="auth-switch" id="authSwitch">Kein Account? <button onclick="authToggle()">Registrieren</button></div>
</div>
</div>
<div class="bio-lock" id="bioLock" style="display:none">
  <div class="bio-lock-card">
    <div class="auth-bio-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><path d="M9 10a3 3 0 0 1 6 0M8.5 15.5c1 1 5 1 7 0M12 10v3.5"/></svg></div>
    <div class="bio-lock-t">Swiss Snow Model</div>
    <div class="bio-lock-s">Entsperre mit <b id="bioLockName">Biometrie</b></div>
    <button class="auth-btn primary" onclick="haptic(6);bioUnlock()">Mit <span id="bioLockName2">Face ID</span> entsperren</button>
    <button class="bio-lock-out" onclick="bioSignOut()">Abmelden</button>
  </div>
</div>
<div class="report-overlay" id="reportOverlay" style="display:none">
<div class="ro-bg" onclick="obsClose()"></div>
<div class="report-sheet obs-sheet" onclick="event.stopPropagation()">
  <div class="rp-wiz-head">
    <button class="rp-nav-btn" id="obsBack" onclick="obsBack()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg></button>
    <div class="rp-progress" id="obsProgress"></div>
    <button class="rp-nav-btn" onclick="obsClose()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
  </div>
  <div class="rp-step-title" id="obsTitle">Beobachtung melden</div>
  <div class="rp-step-sub" id="obsSub">Was hast du gesehen?</div>
  <div class="obs-body" id="obsBody"></div>
  <div class="rp-wiz-nav" id="obsNav"><button class="rp-next" id="obsNext" onclick="obsNext()">Weiter</button></div>
</div>
</div>
<input type="file" id="obsFile" accept="image/*" multiple onchange="obsAddMedia(this)" hidden/>
<div class="feed-page" id="feedPage">
<div class="feed-nav">
<button class="feed-back" onclick="feedClose()"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>
<span class="feed-title">Community</span>
<button class="feed-back" style="margin-left:auto" title="Leute finden" aria-label="Leute finden" onclick="usOpen()"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.2" y2="16.2"/></svg></button>
</div>
<div class="feed-scope" id="feedScope"></div>
<div class="feed-filter" id="feedFilter"></div>
<div class="feed-loc" id="feedLoc" style="display:none">
  <button class="feed-loc-btn" id="feedNear"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg> In der Nähe</button>
  <button class="feed-loc-btn" id="feedPeakBtn" onclick="openLocPicker('peak')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px"><path d="M3 20L9 8l3.5 6L15 10l6 10z"/></svg> Gipfel</button>
  <button class="feed-loc-btn" id="feedDestBtn" onclick="openLocPicker('dest')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px"><circle cx="15.5" cy="4.5" r="1.6"/><path d="M5 20l5-11 4 6 2.5-4L21 17"/><path d="M3 21h18"/></svg> Gebiet</button>
  <button class="feed-loc-clear" id="feedAnchorClear" style="display:none">✕ Filter</button>
</div>
<div class="feed-anchor-bar" id="feedAnchorBar" style="display:none"></div>
<div class="feed-scroll"><div class="feed-grid" id="feedList"><div class="feed-empty">Lade Beiträge…</div></div></div>
<button class="feed-qr" onclick="qrOpen(event)" title="Quick Powder Report"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 2L4.5 13.2c-.4.5 0 1.3.6 1.3H11l-1.4 7.2c-.1.7.8 1.1 1.2.5L20 11.5c.4-.5 0-1.3-.6-1.3H13l1.3-7.7c.1-.7-.8-1.1-1.3-.5z"/></svg><span>Powder</span></button>
<button class="feed-fab" id="feedFab" onclick="feedCreatePost()" title="Bedingungen melden"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg><span>Melden</span></button>
</div>
<div class="loc-picker" id="locPicker" style="display:none" onclick="if(event.target===this)locPickerClose()">
  <div class="loc-picker-sheet">
    <div class="loc-picker-head">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
      <input id="locPickerInput" type="text" placeholder="Suchen…" autocomplete="off" oninput="locPickerFilter(this.value)"/>
      <button onclick="locPickerClose()">✕</button>
    </div>
    <div class="loc-picker-list" id="locPickerList"></div>
  </div>
</div>
<div class="cmt-modal" id="cmtModal" style="display:none" onclick="if(event.target===this)commentsClose()">
  <div class="cmt-sheet">
    <div class="cmt-head"><span>Kommentare</span><button onclick="commentsClose()">✕</button></div>
    <div class="cmt-list" id="cmtList"></div>
    <div class="cmt-input"><input id="cmtInput" type="text" placeholder="Kommentar schreiben…" maxlength="500" onkeydown="if(event.key==='Enter')addComment()"/><button onclick="addComment()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg></button></div>
  </div>
</div>
<div class="cond-modal" id="condModal" style="display:none" onclick="if(event.target===this)condClose()">
  <div class="cond-sheet">
    <div class="cond-head"><b>Wie sind die Bedingungen?</b><button onclick="condClose()">✕</button></div>
    <div class="cond-body">
      <div class="cond-lbl">Gesamteindruck</div>
      <div class="cond-stars" id="condStars"></div>
      <div class="cond-lbl">Powder?</div>
      <div class="cond-pow"><button id="condPowY" onclick="condSetPowder(true)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:15px;height:15px;vertical-align:-2px"><line x1="12" y1="3" x2="12" y2="21"/><line x1="4.2" y1="7.5" x2="19.8" y2="16.5"/><line x1="4.2" y1="16.5" x2="19.8" y2="7.5"/></svg> Ja</button><button id="condPowN" onclick="condSetPowder(false)">Nein</button></div>
      <button class="cond-save" id="condSaveBtn" onclick="condSave()" disabled>Speichern</button>
    </div>
  </div>
</div>
<div class="cond-modal" id="qrModal" style="display:none" onclick="if(event.target===this)qrClose()">
  <div class="cond-sheet">
    <div class="cond-head"><b><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:19px;height:19px;vertical-align:-3px;color:#b06a00"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Quick Powder Report</b><button onclick="qrClose()">✕</button></div>
    <div class="cond-body">
      <div class="qr-loc" id="qrLoc"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;vertical-align:-2px;color:var(--acc2)"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg> <span id="qrLocTxt">Standort wird ermittelt…</span></div>
      <div class="qr-step" id="qrStep0">
        <div class="cond-lbl">1 · Standort — Karte unter dem Pin verschieben</div>
        <div class="obs-loc-wrap"><div class="obs-loc-map" id="qrMap" style="height:180px"></div></div>
        <button class="cond-save" onclick="qrToStep(1)">Weiter</button>
      </div>
      <div class="qr-step" id="qrStep1" style="display:none">
        <div class="cond-lbl">2 · Wie viel &amp; wie gut?</div>
        <div class="qr-padwrap">
          <div class="qr-ylab">wenig ← Menge (cm) → viel</div>
          <div class="qr-pad" id="qrPad">
            <div class="qr-qline" style="left:25%"></div>
            <div class="qr-qline" style="left:50%"></div>
            <div class="qr-qline" style="left:75%"></div>
            <div class="qr-cmline" style="top:16.7%"><i>50</i></div>
            <div class="qr-cmline" style="top:33.3%"><i>40</i></div>
            <div class="qr-cmline" style="top:50%"><i>30</i></div>
            <div class="qr-cmline" style="top:66.7%"><i>20</i></div>
            <div class="qr-cmline" style="top:83.3%"><i>10</i></div>
            <div class="qr-pad-dot" id="qrPadDot" style="display:none"><span class="qr-dot-cm" id="qrDotCm">0 cm</span></div>
          </div>
        </div>
        <div class="qr-xbands">
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 8h16M4 12h16M4 16h16"/></svg><span>Kompakt /<br>gedeckelt</span></div>
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M12 3v11M7 9.5l5 4 5-4"/><circle cx="12" cy="19" r="1.5" fill="currentColor" stroke="none"/></svg><span>Gealterter<br>Pulver</span></div>
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M12 2v20M3.5 7l17 10M20.5 7l-17 10"/></svg><span>Pulver</span></div>
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3l1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6z"/></svg><span>Pulver<br>fluffy</span></div>
        </div>
        <div class="qr-read" id="qrRead">Tippe oder ziehe auf dem Feld</div>
        <button class="cond-save" id="qrNextBtn" onclick="qrToStep(2)" disabled>Weiter</button>
      </div>
      <div class="qr-step" id="qrStep2" style="display:none">
        <div class="cond-lbl">3 · Foto (optional)</div>
        <label class="qr-photo-one" for="qrLib"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg><span>Foto aufnehmen oder wählen</span></label>
        <input id="qrLib" type="file" accept="image/*" hidden onchange="qrPhotoPick(this)">
        <img id="qrPrev" class="qr-prev" style="display:none" alt=""/>
        <button class="cond-save" id="qrSaveBtn" onclick="qrSubmit()">Posten</button>
        <button class="qr-skip" onclick="qrSkip()">Ohne Foto posten</button>
      </div>
    </div>
  </div>
</div>
<div class="prof-modal" id="profModal" style="display:none" onclick="if(event.target===this)profClose()">
  <div class="prof-sheet">
    <div class="prof-head"><span>Profil</span><button onclick="profClose()">✕</button></div>
    <div class="prof-body">
      <div class="prof-top">
        <div class="prof-av" id="profAv" onclick="document.getElementById('profAvFile').click()"><span id="profAvInitial"></span><div class="prof-av-edit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg></div></div>
        <input type="file" id="profAvFile" accept="image/*" hidden onchange="profSetAvatar(this)"/>
        <div class="prof-meta"><div class="prof-name" id="profName">User</div><div class="prof-endo" id="profEndo">– Endorsements</div></div>
      </div>
      <div class="prof-view" id="profViewMain">
        <div class="prof-sec-title">Einstellungen</div>
        <button class="prof-item nav" onclick="profNav('pers')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>Personalisieren<span class="chev">›</span></button>
        <button class="prof-item nav" onclick="profNav('priv')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>Privatsphäre &amp; Daten<span class="chev">›</span></button>
        <button class="prof-item nav" onclick="profNav('notif')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>Benachrichtigungen<span class="chev">›</span></button>
        <button class="prof-item nav" onclick="profNav('app')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="3"/><line x1="9" y1="18" x2="15" y2="18"/></svg>App &amp; Rechtliches<span class="chev">›</span></button>
        <button class="prof-signout" onclick="profSignOut()">Abmelden</button>
      </div>
      <div class="prof-view" id="profViewPers" style="display:none">
        <button class="prof-back" onclick="profNav('main')">‹ Zurück</button>
        <div class="prof-sec-title">Personalisieren</div>
        <label class="prof-lbl">Über dich <span class="prof-cnt" id="profBioCnt">0/200</span></label>
        <textarea class="prof-bio" id="profBio" maxlength="1400" placeholder="Kurze Beschreibung (max. 200 Wörter, keine Links)…" oninput="profBioInput()"></textarea>
        <button class="prof-save" id="profSave" onclick="profSave()">Speichern</button>
        <div class="prof-hint">Tippe oben auf dein Profilbild, um es zu ändern. Dein Name und deine Beschreibung erscheinen auf deinem öffentlichen Profil.</div>
      </div>
      <div class="prof-view" id="profViewPriv" style="display:none">
        <button class="prof-back" onclick="profNav('main')">‹ Zurück</button>
        <div class="prof-sec-title">Wer darf mein Profil &amp; meine Beiträge sehen?</div>
        <div class="prof-seg" id="profVis">
          <button data-v="me" onclick="profSetVis('me')">Nur ich</button>
          <button data-v="friends" onclick="profSetVis('friends')">Freunde</button>
          <button data-v="all" class="active" onclick="profSetVis('all')">Alle</button>
        </div>
        <div class="prof-hint" style="margin-top:8px">«Freunde» heisst: ihr folgt euch gegenseitig. Die Einstellung gilt für deine Beiträge auf der Karte und im Feed.</div>
        <div class="prof-sec-title">Meine Daten</div>
        <button class="prof-item" onclick="profExportData(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Meine Daten exportieren (JSON)</button>
        <button class="prof-item" onclick="profDeleteContent(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/><path d="M5 6l1 14a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-14"/></svg>Alle meine Beiträge löschen</button>
        <button class="prof-item danger" onclick="profDeleteAccount(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>Konto &amp; Daten löschen</button>
        <div class="prof-hint">Export und Löschung entsprechen deinen Rechten nach DSG/DSGVO. Das Löschen des Kontos entfernt alle Inhalte unwiderruflich.</div>
      </div>
      <div class="prof-view" id="profViewNotif" style="display:none">
        <button class="prof-back" onclick="profNav('main')">‹ Zurück</button>
        <div class="prof-sec-title">Benachrichtigungen</div>
        <div class="prof-row"><span>Push-Benachrichtigungen</span><button class="prof-toggle" id="profPush" onclick="profTogglePush()"><span></span></button></div>
        <div class="prof-hint">Push-Benachrichtigungen werden mit einem der nächsten Updates aktiviert — deine Einstellung wird dann automatisch übernommen.</div>
      </div>
      <div class="prof-view" id="profViewApp" style="display:none">
        <button class="prof-back" onclick="profNav('main')">‹ Zurück</button>
        <div class="prof-sec-title">App &amp; Rechtliches</div>
        <button class="prof-item" id="profBioBtn" style="display:none" onclick="profEnableBio()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><path d="M9 10a3 3 0 0 1 6 0M8.5 15.5c1 1 5 1 7 0M12 10v3.5"/></svg>Biometrisches Login aktivieren</button>
        <button class="prof-item" id="profInstall" onclick="profClose();a2hsShow()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="3"/><line x1="12" y1="7" x2="12" y2="13"/><polyline points="9.5 10.5 12 13 14.5 10.5"/><line x1="9" y1="17" x2="15" y2="17"/></svg>App installieren</button>
        <button class="prof-item" onclick="profShowLegal()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>Datenschutz &amp; Haftung anzeigen</button>
        <button class="prof-item" onclick="sendFeedback()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.8-.8L3 21l1.9-5.2A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/></svg>Feedback senden</button>
      </div>
    </div>
  </div>
</div>
<div class="prof-modal" id="usModal" style="display:none" onclick="if(event.target===this)usClose()">
  <div class="prof-sheet">
    <div class="prof-head"><span>Leute finden</span><button onclick="usClose()">✕</button></div>
    <div class="prof-body" style="padding-top:10px">
      <input class="prof-bio" style="min-height:auto;height:46px;padding:12px 14px" id="usInput" type="text" placeholder="Benutzername suchen…" autocomplete="off" oninput="usInput()"/>
      <div class="us-list" id="usList"><div class="us-empty">Tippe einen Namen, um Leute zu finden.</div></div>
    </div>
  </div>
</div>
<div class="prof-modal" id="userViewModal" style="display:none" onclick="if(event.target===this)userViewClose()">
  <div class="prof-sheet uv-sheet">
    <div class="uv-hero"><button class="uv-close" onclick="userViewClose()" aria-label="Schliessen">✕</button></div>
    <div class="prof-body uv-body">
      <div class="prof-av uv-av" id="uvAv"><span id="uvInitial"></span></div>
      <div class="uv-name" id="uvName"></div>
      <div class="uv-bio" id="uvBio"></div>
      <div class="uv-stats">
        <div class="uv-stat"><b id="uvReports">–</b><span>Reports</span></div>
        <div class="uv-stat"><b id="uvFollowers">–</b><span>Follower</span></div>
        <div class="uv-stat"><b id="uvEndoN">–</b><span>Bestätigt</span></div>
        <div class="uv-stat"><b id="uvSince">–</b><span>Dabei seit</span></div>
      </div>
      <span id="uvEndo" style="display:none"></span>
      <button class="prof-save" id="uvFollow" onclick="uvToggleFollow()">Folgen</button>
      <button class="uv-report" onclick="uvReportUser()">Nutzer melden</button>
      <div class="uv-posts" id="uvPosts"></div>
    </div>
  </div>
</div>
<div id="coach" style="display:none"><div id="coachSpot"></div><div id="coachCard"><div id="coachText"></div><div id="coachNav"><span id="coachDots"></span><button id="coachSkip">Überspringen</button><button id="coachNext">Weiter</button></div></div></div>
<!--__BOOT__-->
<script id="appjs">/*__APP_START__*/
const __D=window.__D||{};const M=__D.meta||{};function db(k){return (__D.b&&__D.b[k])||"";}
// --- Native (Capacitor) bridge — real native on iOS, harmless no-op in a browser ---
const _CAP=(typeof window!=='undefined'&&window.Capacitor)?window.Capacitor:null;
const _isNative=!!(_CAP&&_CAP.isNativePlatform&&_CAP.isNativePlatform());
function _capPlugin(n){return (_CAP&&_CAP.Plugins&&_CAP.Plugins[n])?_CAP.Plugins[n]:null;}
function nativeShare(text,url){const S=_capPlugin('Share');if(S){try{S.share({text:text||'',url:url||''});return true;}catch(e){}}return false;}
if(_isNative){try{const SB=_capPlugin('StatusBar');if(SB){SB.setStyle({style:'DARK'});SB.setOverlaysWebView({overlay:true});}}catch(e){}
  try{const SP=_capPlugin('SplashScreen');if(SP)setTimeout(()=>{try{SP.hide();}catch(e){}},450);}catch(e){}}
function dec(b){const s=atob(b),n=s.length,a=new Uint8Array(n);for(let i=0;i<n;i++)a[i]=s.charCodeAt(i);return a;}
const SNOW=dec(db('SNOW')),TEMP=dec(db('TEMP')),SUN=dec(db('SUN')),SPD=dec(db('SPD')),WDIR=dec(db('DIR'));
const WINDG=dec(db('WINDG')),RSLOPE=dec(db('RSLOPE')),RASPECT=dec(db('RASPECT')),RHOR=dec(db('RHOR'));
const PREC=dec(db('PREC')),MASPECT=dec(db('MASPECT')),MSLOPE=dec(db('MSLOPE')),MELEV=dec(db('MELEV')),ROUGHG=dec(db('ROUGHGRID'));
const ASPECT_PNG="data:image/png;base64,"+db('ASPECTPNG'),ROUGH_PNG="data:image/png;base64,"+db('ROUGHPNG');
const T=M.T,W=M.width,H=M.height,NP=W*H,P=M.wind.lat.length,NX=M.wind.nx;
const RW=M.rad.width,RH=M.rad.height,RK=M.rad.K,RNP=RW*RH;
const [RbS,RlW,RbN,RlE]=M.rad.bounds;
const wg_=(t,p)=>WINDG[t*NP+p]/M.spd_mul;
const cum=new Float32Array((T+1)*NP);
for(let t=0;t<T;t++){const o0=t*NP,o1=(t+1)*NP,s=t*NP,sc=M.snow_scale;for(let p=0;p<NP;p++)cum[o1+p]=cum[o0+p]+SNOW[s+p]*sc;}
const tv=(t,p)=>TEMP[t*NP+p]/M.temp_mul-M.temp_off, sunv=(t,p)=>SUN[t*NP+p]/M.sun_mul;
const SB=M.slf_bounds,SC=M.slf_colors,RGB=SC.map(h=>[parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)]);
function snowCol(v){if(v<SB[0])return null;for(let i=SB.length-1;i>=1;i--)if(v>=SB[i-1])return RGB[Math.min(i-1,RGB.length-1)];return RGB[0];}
// --- Powder Decision Engine: named constants ---
const PD_RAIN_LOOKBACK_H=48;
const PD_RAIN_TEMP_C=1.0;
const PD_RAIN_MIN_MM=0.1;
const PD_FT_DESTROY=4;
const PD_FT_DEGRADE=2;
const PD_SOLAR_DESTROY_WH=5000;
const PD_SOLAR_MOD_WH=2000;
const PD_DEEP_FREEZE_C=-2;
const PD_FREEZE_CLEAR_MIN_C=-2;
const PD_FREEZE_CLEAR_MAX_C=5;
const PD_GUST_CALM_KMH=20;
const PD_WIND_CALM_KMH=10;
const PD_GUST_REDIST_MIN_KMH=20;
const PD_GUST_REDIST_MAX_KMH=60;
const PD_WIND_REDIST_KMH=30;
const PD_GUST_FACTOR=1.5;
// Neu (validiert gegen Golden-Dataset, s. tools/eval_powder.py — 26/27):
const PD_MIN_NEWSNOW_CM=0.5;      // I1/D0': min. Neuschnee im Lookback
const PD_NEWSNOW_LOOKBACK_H=168;  // I1: 7 Tage
const PD_COLD_SOLAR_TMAX_C=-4;    // I2: kalte Decke vertraegt mehr Sonne
const PD_COLD_SOLAR_FACTOR=1.5;   // I2
const PD_WET_PM_TMAX_C=3;         // I3: Nachmittagsnaesse -> reduced
const PD_WET_PM_SUN_H=1;          // I3
const PD_WET_PM_DROP_S_C=6;       // I3: ab hier S raus
const PD_PERSIST_CHECK_H=72;      // I4: Kalt-Persistenz alter Powder
const PD_PERSIST_WARM_C=0.5;      // I4
const PD_PERSIST_WARM_SHARE=0.35; // I4
const precv=(t,p)=>PREC[t*NP+p]/M.prec_mul;
const maspv=p=>MASPECT[p]*M.dir_div;
const mslpv=p=>MSLOPE[p];
const melevv=p=>MELEV[p]*M.elev_scale;
const mainToWind=new Int32Array(NP);
const main2rad=new Int32Array(NP).fill(-1);
function aspectQ(deg){const a=((deg%360)+360)%360;if(a>=315||a<45)return'N';if(a<135)return'E';if(a<225)return'S';return'W';}
const ASPC={N:[0x4A,0x90,0xD9],E:[0x66,0xBB,0x6A],S:[0xEF,0x53,0x50],W:[0xFF,0xC1,0x07],F:[0x9E,0x9E,0x9E]};
function aspCol(p){const s=mslpv(p);if(s<5)return ASPC.F;const q=aspectQ(maspv(p));return ASPC[q];}
function isLee(cellAsp,windFrom){const lee=(windFrom+180)%360;let d=Math.abs(cellAsp-lee);if(d>180)d=360-d;return d<90;}
const _QCEN={N:0,E:90,S:180,W:270};
// Referenz: model/powder_engine.py — Aenderungen dort und hier synchron halten!
function computePowder(p,ta,tb){
  const asp=maspv(p),slp=mslpv(p),quad=aspectQ(asp);
  const res={powdered:false,valid_aspects:[],reason_flags:[],quality:'stable'};
  // Dominant wind + mean wind from nearest wind point
  const wk=mainToWind[p];let ss=0,sc2=0,ws=0,wc=0;
  for(let t=ta;t<tb;t++){const d=WDIR[t*P+wk]*M.dir_div*Math.PI/180;ss+=Math.sin(d);sc2+=Math.cos(d);ws+=SPD[t*P+wk]/M.spd_mul*3.6;wc++;}
  const domW=(Math.atan2(ss,sc2)*180/Math.PI+360)%360;
  const meanW=ws/Math.max(1,wc), gustW=meanW*PD_GUST_FACTOR;
  let tmax=-999;for(let t=ta;t<tb;t++){const v=tv(t,p);if(v>tmax)tmax=v;}
  // Letzter Schneefall ueber die ganze Serie (fuer D0'/D1')
  let lastSnowG=-1;for(let t=0;t<tb;t++){if(SNOW[t*NP+p]*M.snow_scale>0.1)lastSnowG=t;}
  // I1/D0': ohne Neuschnee kein Powder — ausser Decke blieb kalt (I4)
  const s0=Math.max(0,tb-PD_NEWSNOW_LOOKBACK_H);
  const newSnow=cum[tb*NP+p]-cum[s0*NP+p];
  let aged=false;
  if(newSnow<PD_MIN_NEWSNOW_CM){
    const w0=Math.max(0,tb-PD_PERSIST_CHECK_H);let warm=0;
    for(let t=w0;t<tb;t++)if(tv(t,p)>PD_PERSIST_WARM_C)warm++;
    if(warm/Math.max(1,tb-w0)>PD_PERSIST_WARM_SHARE){res.reason_flags.push('D0_no_recent_snow');return res;}
    aged=true;res.reason_flags.push('I4_old_snow_persist');
  }
  // D1': Regen in den letzten 48h — aber nur NACH dem letzten Schneefall
  let r0=Math.max(0,tb-PD_RAIN_LOOKBACK_H);if(lastSnowG>=0)r0=Math.max(r0,lastSnowG+1);
  let rain=false;
  for(let t=r0;t<tb&&!rain;t++){if(precv(t,p)>PD_RAIN_MIN_MM&&tv(t,p)>PD_RAIN_TEMP_C)rain=true;}
  if(rain){res.reason_flags.push('D1_rain');return res;}
  // D2: Freeze-thaw cycles since last snowfall
  let lastSnow=ta;
  for(let t=ta;t<tb;t++){if(SNOW[t*NP+p]>0)lastSnow=t;}
  let ftc=0,bel=tv(lastSnow,p)<0;
  for(let t=lastSnow+1;t<tb;t++){const b2=tv(t,p)<0;if(b2!==bel){ftc+=0.5;bel=b2;}}
  ftc=Math.floor(ftc);
  if(ftc>PD_FT_DESTROY){res.reason_flags.push('D2_freeze_thaw');return res;}
  // D3: Solar melt (I2: kalte Decke vertraegt mehr Sonne)
  const doy=bandDoy();if(doy!==radDoy){radCS=computeRad(doy);radDoy=doy;}
  const rp=main2rad[p];const solar=rp>=0?radCS[rp]:0;
  let destroyWh=PD_SOLAR_DESTROY_WH;
  if(tmax<PD_COLD_SOLAR_TMAX_C){destroyWh*=PD_COLD_SOLAR_FACTOR;res.reason_flags.push('I2_cold_solar_relax');}
  if(solar>=destroyWh){res.reason_flags.push('D3_solar_melt');return res;}
  // --- Preservation ---
  const ALL=['N','E','S','W'];const valid=new Set();
  // R1
  if(tmax<PD_DEEP_FREEZE_C){res.reason_flags.push('R1_deep_freeze');ALL.forEach(a=>valid.add(a));}
  // R2
  if(gustW<PD_GUST_CALM_KMH&&meanW<PD_WIND_CALM_KMH){res.reason_flags.push('R2_calm_wind');ALL.forEach(a=>valid.add(a));}
  // R3
  if(gustW>=PD_GUST_REDIST_MIN_KMH&&gustW<=PD_GUST_REDIST_MAX_KMH&&meanW<PD_WIND_REDIST_KMH){
    res.reason_flags.push('R3_wind_redistribution');
    for(const q of ALL){if(isLee(_QCEN[q],domW))valid.add(q);}
  }
  // R4
  let clearNight=false;
  for(let t=ta;t<tb&&!clearNight;t++){if(sunv(t,p)<0.01&&tv(t,p)<0)clearNight=true;}
  if(tmax>=PD_FREEZE_CLEAR_MIN_C&&tmax<PD_FREEZE_CLEAR_MAX_C&&clearNight){
    res.reason_flags.push('R4_freeze_clear_night');valid.add('N');
  }
  if(valid.size===0)return res;
  // Solar moderation — nur wenn nicht tiefgefroren (I3b)
  if(tmax>=PD_DEEP_FREEZE_C&&solar>=PD_SOLAR_MOD_WH&&solar<destroyWh){valid.delete('S');valid.delete('W');}
  // I3: Nachmittagsnaesse
  let winSun=0;for(let t=ta;t<tb;t++)winSun+=sunv(t,p);
  if(tmax>=PD_WET_PM_TMAX_C&&winSun>=PD_WET_PM_SUN_H){
    res.reason_flags.push('G2_wet_afternoon');res.quality='reduced';
    if(tmax>=PD_WET_PM_DROP_S_C)valid.delete('S');
  }
  res.valid_aspects=[...valid];
  if(!valid.has(quad))return res;
  // Degradation
  if(ftc>=PD_FT_DEGRADE||aged)res.quality='reduced';
  res.powdered=true;return res;
}
function tempCol(t){const x=Math.max(-20,Math.min(20,t))/20;let r,g,b;if(x<0){const k=x+1;r=40+k*215|0;g=80+k*175|0;b=255;}else{r=255;g=255-x*200|0;b=255-x*235|0;}return[r,g,b];}
function rampBYR(x){x=Math.max(0,Math.min(1,x));if(x<.33){const k=x/.33;return[30,120+k*120|0,255-k*120|0];}if(x<.66){const k=(x-.33)/.33;return[30+k*225|0,240,135-k*135|0];}const k=(x-.66)/.34;return[255,240-k*200|0,0];}
function sunCol(h,vmax){const x=Math.min(1,h/Math.max(1,vmax));return[255,250-x*135|0,210-x*210|0];}
const TH=[-15,-10,-5,0,5,10];
function tClass(v){let c=0;for(const t of TH)if(v>=t)c++;return c;}
function tsurfEst(t,p){
  const ta=tv(t,p),sun=sunv(t,p);
  const wk=mainToWind[p],ws=SPD[t*P+wk]/M.spd_mul*3.6;
  const radCool=(sun<0.01&&ws<10)?-3.0*(1-ws/10)*(ta<2?1:0.3):0;
  const solarWarm=sun>0.3?1.5*Math.min(1,sun):0;
  return ta+radCool+solarWarm;
}
// --- Roughness + Skiability ---
const roughv=p=>ROUGHG[p]*5.0;
function minSnowNeeded(p){const rough=roughv(p),slp=mslpv(p);if(slp>55)return 9999;const rn=Math.min(1,rough/250);const sf=1+Math.max(0,slp-10)*0.02;return(20+rn*100)*sf;}
// --- Snowfall Timeline ---
const hSnow=M.hourly_snow,nowIdx=M.now_index;
const SNOW_THRESH=0.005;
const snowEvents=[];
(function(){let s=-1;for(let t=0;t<T;t++){if(hSnow[t]>SNOW_THRESH){if(s<0)s=t;}else{if(s>=0){snowEvents.push([s,t]);s=-1;}}}if(s>=0)snowEvents.push([s,T]);})();
function sinceLastSnowfall(){let ls=nowIdx;for(const[s,e]of snowEvents){if(s<=nowIdx)ls=s;}return[ls,Math.min(T,nowIdx+1)];}
function tillTomorrow(){for(let t=nowIdx+1;t<T;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===8)return[nowIdx,t];}return[nowIdx,Math.min(T,nowIdx+24)];}
function fmtTime(i){const d=new Date(M.times[Math.max(0,Math.min(T-1,i))]+'Z');return d.getUTCHours()+':00';}
function drawTimeline(){const tc=document.getElementById('timeline');const rect=tc.getBoundingClientRect();
  if(!rect.width)return;
  const cw=rect.width,ch=rect.height||120,dpr=window.devicePixelRatio||1;
  if(tc.width!==Math.round(cw*dpr)||tc.height!==Math.round(ch*dpr)){tc.width=Math.round(cw*dpr);tc.height=Math.round(ch*dpr);}
  const ctx2=tc.getContext('2d');ctx2.setTransform(dpr,0,0,dpr,0,0);ctx2.clearRect(0,0,cw,ch);
  function rr(x,y,w,h,r){r=Math.max(0,Math.min(r,w/2,h/2));ctx2.beginPath();if(ctx2.roundRect){ctx2.roundRect(x,y,w,h,r);}else{ctx2.moveTo(x+r,y);ctx2.arcTo(x+w,y,x+w,y+h,r);ctx2.arcTo(x+w,y+h,x,y+h,r);ctx2.arcTo(x,y+h,x,y,r);ctx2.arcTo(x,y,x+w,y,r);ctx2.closePath();}}
  const nx=nowIdx/T*cw,x1=a/T*cw,x2=b/T*cw,baseY=ch-24;
  // soft selection band (rounded) — tinted with the active layer colour
  ctx2.fillStyle=tlSelTint;rr(x1,2,x2-x1,ch-4,10);ctx2.fill();
  // day gridlines + readable date labels
  ctx2.textAlign='left';let _lastLabX=-1e9;
  for(let t=0;t<T;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===0){const x=t/T*cw;
    ctx2.strokeStyle='rgba(22,21,46,.05)';ctx2.lineWidth=1;ctx2.beginPath();ctx2.moveTo(x,22);ctx2.lineTo(x,baseY);ctx2.stroke();
    // only label a day if it clears the previous label → no overlap on narrow phones
    if(x-_lastLabX>=48){ctx2.fillStyle='rgba(70,90,110,.7)';ctx2.font='700 11.5px Inter,system-ui';
      ctx2.fillText(['So','Mo','Di','Mi','Do','Fr','Sa'][d.getUTCDay()]+' '+d.getUTCDate()+'.',x+6,ch-7);_lastLabX=x;}}}
  // baseline
  ctx2.strokeStyle='rgba(22,21,46,.09)';ctx2.lineWidth=1;ctx2.beginPath();ctx2.moveTo(0,baseY+.5);ctx2.lineTo(cw,baseY+.5);ctx2.stroke();
  // snowfall bars — rounded tops, vertical gradient (snow stays blue)
  let mx=0;for(const s of hSnow)if(s>mx)mx=s;mx=Math.max(.05,mx);
  const barH=ch-48,bw=Math.max(2,cw/T);
  const gSel=ctx2.createLinearGradient(0,baseY-barH,0,baseY);gSel.addColorStop(0,'#9b8cff');gSel.addColorStop(1,'#4844c9');
  const gSelPast=ctx2.createLinearGradient(0,baseY-barH,0,baseY);gSelPast.addColorStop(0,'#a9cde8');gSelPast.addColorStop(1,'#6f9cc0');
  for(let t=0;t<T;t++){const v=hSnow[t];if(v<.002)continue;const h=Math.max(2,v/mx*barH);const x=t/T*cw;
    const inSel=(t>=a&&t<b),fut=t>=nowIdx;
    ctx2.fillStyle=inSel?(fut?gSel:gSelPast):(fut?'rgba(94,92,230,.2)':'rgba(150,165,185,.2)');
    rr(x+.5,baseY-h,Math.max(bw-1,1.4),h,Math.min(2.5,bw/2.2));ctx2.fill();}
  // selection frame + rounded grab handles (layer colour)
  ctx2.strokeStyle=tlSel;ctx2.globalAlpha=.65;ctx2.lineWidth=2;rr(x1+1,2,x2-x1-2,ch-4,10);ctx2.stroke();ctx2.globalAlpha=1;
  const hh=28,hy=(ch-hh)/2;ctx2.fillStyle=tlSel;
  rr(x1-3.5,hy,7,hh,3.5);ctx2.fill();rr(x2-3.5,hy,7,hh,3.5);ctx2.fill();
  ctx2.fillStyle='rgba(255,255,255,.92)';for(let i=-1;i<=1;i++){ctx2.fillRect(x1-1.25,hy+hh/2+i*5,2.5,2.4);ctx2.fillRect(x2-1.25,hy+hh/2+i*5,2.5,2.4);}
  // selected start / end times — pushed to the OUTER bounds when the selection is narrow
  ctx2.font='800 14px Inter,system-ui';ctx2.fillStyle=tlSel;
  const tA=fmtTime(a),tB=fmtTime(b-1),wA=ctx2.measureText(tA).width,wB=ctx2.measureText(tB).width;
  if((x2-x1)<(wA+wB+18)){
    ctx2.textAlign='right';let ax=x1-6;if(ax-wA<2)ax=wA+2;ctx2.fillText(tA,ax,19);
    ctx2.textAlign='left';let bx=x2+6;if(bx+wB>cw-2)bx=cw-2-wB;ctx2.fillText(tB,bx,19);
  }else{
    ctx2.textAlign='left';ctx2.fillText(tA,x1+9,19);
    ctx2.textAlign='right';ctx2.fillText(tB,x2-9,19);
  }
  // NOW marker: dashed line + dark pill (neutral so it never clashes with the layer colour)
  ctx2.strokeStyle='#16152e';ctx2.globalAlpha=.55;ctx2.lineWidth=1.5;ctx2.setLineDash([3,3]);ctx2.beginPath();ctx2.moveTo(nx,19);ctx2.lineTo(nx,baseY);ctx2.stroke();ctx2.setLineDash([]);ctx2.globalAlpha=1;
  const nlx=Math.max(19,Math.min(cw-19,nx));ctx2.fillStyle='#16152e';rr(nlx-17,3,34,14,7);ctx2.fill();
  ctx2.fillStyle='#fff';ctx2.font='800 9.5px Inter,system-ui';ctx2.textAlign='center';ctx2.fillText('NOW',nlx,12.5);}
// Karte + Layer
const [laMin,loMin,laMax,loMax]=M.bounds;
const _desktop=window.innerWidth>560&&!('ontouchstart'in window);
const map=L.map('map',{zoomControl:false,zoomSnap:0,zoomDelta:.5,wheelPxPerZoomLevel:_desktop?38:90,wheelDebounceTime:_desktop?12:40,maxBoundsViscosity:1.0,inertia:true}).fitBounds([[laMin,loMin],[laMax,loMax]],{padding:[6,6]});
L.control.scale({imperial:false,maxWidth:120,position:'bottomright'}).addTo(map);
if(_desktop&&map.scrollWheelZoom&&map.scrollWheelZoom.setWheelPxPerZoomLevel)map.scrollWheelZoom.setWheelPxPerZoomLevel(38);
// Constrain panning + zoom to the meteo grid, with a thin white frame around the data
const _fitZoom=map.getZoom();
map.setMinZoom(_fitZoom);map.setMaxZoom(16);  // never zoom out past the initial view
const _padLa=(laMax-laMin)*0.04,_padLo=(loMax-loMin)*0.04;
map.setMaxBounds([[laMin-_padLa,loMin-_padLo],[laMax+_padLa,loMax+_padLo]]);
const base=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe-winter/default/current/3857/{z}/{x}/{y}.jpeg",{maxZoom:17,attribution:"© swisstopo / MeteoSwiss / SLF / Copernicus"}).addTo(map);
// White mask outside the meteo grid → clean, smooth map ending
map.createPane('maskPane');map.getPane('maskPane').style.zIndex=350;map.getPane('maskPane').style.pointerEvents='none';
const _world=[[-89,-360],[-89,360],[89,360],[89,-360]];
const _hole=[[laMin,loMin],[laMin,loMax],[laMax,loMax],[laMax,loMin]];
L.polygon([_world,_hole],{pane:'maskPane',stroke:false,fillColor:'#ffffff',fillOpacity:1,interactive:false}).addTo(map);
const slopeWMTS=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.hangneigung-ueber_30/default/current/3857/{z}/{x}/{y}.png",{opacity:.7});
const reliefWMTS=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissalti3d-reliefschattierung_monodirektional/default/current/3857/{z}/{x}/{y}.png",{opacity:.85});
const roughImg=L.imageOverlay(ROUGH_PNG,[[M.png_bounds[0],M.png_bounds[1]],[M.png_bounds[2],M.png_bounds[3]]],{opacity:.78});
// Aspect: load high-res classified PNG into offscreen canvas, render via GridLayer with nearest-neighbor
const [aspS,aspWest,aspN,aspEast]=M.png_bounds;
let aspData=null,aspPW=0,aspPH=0;
const aspI=new Image();aspI.src=ASPECT_PNG;
aspI.onload=function(){aspPW=aspI.width;aspPH=aspI.height;const c=document.createElement('canvas');c.width=aspPW;c.height=aspPH;const x=c.getContext('2d');x.drawImage(aspI,0,0);aspData=x.getImageData(0,0,aspPW,aspPH).data;};
const AspectGrid=L.GridLayer.extend({createTile:function(coords){
  const tile=document.createElement('canvas'),ts=this.getTileSize();tile.width=ts.x;tile.height=ts.y;
  if(!aspData)return tile;const ctx=tile.getContext('2d'),img=ctx.createImageData(ts.x,ts.y),d=img.data;
  const nw=this._map.unproject([coords.x*ts.x,coords.y*ts.y],coords.z);
  const se=this._map.unproject([(coords.x+1)*ts.x,(coords.y+1)*ts.y],coords.z);
  for(let y=0;y<ts.y;y++){const lat=nw.lat+(se.lat-nw.lat)*y/ts.y;
    const py=Math.floor((aspN-lat)/(aspN-aspS)*aspPH);if(py<0||py>=aspPH)continue;
    for(let x=0;x<ts.x;x++){const lon=nw.lng+(se.lng-nw.lng)*x/ts.x;
      const px=Math.floor((lon-aspWest)/(aspEast-aspWest)*aspPW);if(px<0||px>=aspPW)continue;
      const si=(py*aspPW+px)*4,di=(y*ts.x+x)*4;
      d[di]=aspData[si];d[di+1]=aspData[si+1];d[di+2]=aspData[si+2];d[di+3]=aspData[si+3];}}
  ctx.putImageData(img,0,0);return tile;}});
const aspectGrid=new AspectGrid({opacity:.78,tileSize:256});
const cv=document.createElement('canvas');cv.width=W;cv.height=H;const cx=cv.getContext('2d');
let raster=L.imageOverlay(cv.toDataURL(),[[laMin,loMin],[laMax,loMax]],{opacity:.94}).addTo(map);
const rcv=document.createElement('canvas');rcv.width=RW;rcv.height=RH;const rcx=rcv.getContext('2d');
let radOverlay=L.imageOverlay(rcv.toDataURL(),[[RbS,RlW],[RbN,RlE]],{opacity:.9});
const rad2cube=new Int32Array(RNP);
(function(){for(let p=0;p<RNP;p++){const ry=(p/RW)|0,rx=p%RW;
  const lat=RbN-(RbN-RbS)*ry/(RH-1),lon=RlW+(RlE-RlW)*rx/(RW-1);
  let cx2=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy2=Math.round((laMax-lat)/(laMax-laMin)*(H-1));
  rad2cube[p]=Math.max(0,Math.min(H-1,cy2))*W+Math.max(0,Math.min(W-1,cx2));}})();
// Init powder engine mappings (need laMin/loMin/laMax/loMax + rad2cube)
(function(){for(let p=0;p<NP;p++){const y=(p/W)|0,x=p%W;const la=laMax-(laMax-laMin)*y/Math.max(1,H-1);const lo=loMin+(loMax-loMin)*x/Math.max(1,W-1);let bd=1e18,bk=0;for(let k=0;k<P;k++){const dl=la-M.wind.lat[k],dn=lo-M.wind.lon[k],d=dl*dl+dn*dn;if(d<bd){bd=d;bk=k;}}mainToWind[p]=bk;}})();
(function(){for(let rp=0;rp<RNP;rp++)main2rad[rad2cube[rp]]=rp;})();
let radCS=null,radDoy=-1;
function computeRad(doy){const decl=23.45*Math.PI/180*Math.sin(2*Math.PI*(284+doy)/365);
  const I0=1361,tau=0.72,dt=0.5,out=new Float32Array(RNP);
  for(let p=0;p<RNP;p++){const ry=(p/RW)|0;const lat=(RbN-(RbN-RbS)*ry/(RH-1))*Math.PI/180;
    const slope=RSLOPE[p]*Math.PI/180,aspect=RASPECT[p]*M.dir_div*Math.PI/180;let wh=0;
    for(let h=3.5;h<=20.5;h+=dt){const ha=(h-12)*15*Math.PI/180;
      const sinEl=Math.sin(lat)*Math.sin(decl)+Math.cos(lat)*Math.cos(decl)*Math.cos(ha);
      if(sinEl<=0.02)continue;const el=Math.asin(sinEl);
      let cosAz=Math.max(-1,Math.min(1,(Math.sin(decl)-sinEl*Math.sin(lat))/(Math.cos(el)*Math.cos(lat)+1e-9)));
      let az=Math.acos(cosAz);if(ha>0)az=2*Math.PI-az;
      const sec=((((az/(2*Math.PI)*RK)|0)%RK)+RK)%RK,hor=RHOR[sec*RNP+p]*Math.PI/180,lit=el>hor;
      const cosI=Math.cos(slope)*sinEl+Math.sin(slope)*Math.cos(el)*Math.cos(az-aspect);
      const am=1/Math.max(0.05,sinEl),Ib=I0*Math.pow(tau,am);
      const beam=(lit&&cosI>0)?Ib*cosI:0,skyview=(1+Math.cos(slope))/2,diff=0.13*I0*sinEl*skyview;
      wh+=(beam+diff)*dt;}
    out[p]=wh;}
  return out;}
function radColor(x){x=Math.max(0,Math.min(1,x));return[40+(x*215|0),20+(x*220|0),100-(x*80|0)];}
function bandDoy(){const dd=new Date(M.times[a]+"Z");const s0=new Date(Date.UTC(dd.getUTCFullYear(),0,0));return Math.floor((dd-s0)/86400000);}
function renderRadiation(){const doy=bandDoy();
  if(doy!=radDoy){radCS=computeRad(doy);radDoy=doy;}
  const win=Math.max(1,b-a);let vmax=1;for(let p=0;p<RNP;p++)if(radCS[p]>vmax)vmax=radCS[p];
  const img=rcx.createImageData(RW,RH),d=img.data;
  for(let p=0;p<RNP;p++){let val=radCS[p];
    if(layer=="radsun"){const cc=rad2cube[p];let s=0;for(let t=a;t<b;t++)s+=sunv(t,cc);const sf=Math.max(0,Math.min(1,s/(0.42*win)));val*=(0.2+0.8*sf);}
    const o=p*4;if(val<vmax*0.02){d[o+3]=0;continue;}const c=radColor(val/vmax);d[o]=c[0];d[o+1]=c[1];d[o+2]=c[2];d[o+3]=205;}
  rcx.putImageData(img,0,0);radOverlay.setUrl(rcv.toDataURL());}
// --- Quick-Powder-Reports: Demo-Punkte + Heatmap-Overlay (dunkelblau = tief & fluffy) ---
const DEMO_QPR=[
[46.797,9.827,55,95],[46.81,9.85,50,90],[46.78,9.80,45,88],[46.83,9.81,40,85],[46.77,9.87,52,92],
[46.79,9.90,38,80],[46.84,9.88,35,75],[46.75,9.83,48,90],[46.78,9.67,30,70],[46.86,9.53,28,65],
[46.73,9.60,25,60],[46.70,9.16,32,72],[46.85,9.21,20,55],[46.83,9.26,24,60],
[46.49,9.84,42,85],[46.53,9.90,36,78],[46.43,9.76,30,70],[46.60,10.03,26,62],
[46.02,7.75,45,88],[46.10,7.78,38,80],[45.98,7.71,50,90],[46.08,7.93,35,74],
[46.10,7.23,40,82],[46.06,7.30,33,72],[46.19,7.31,26,60],[46.31,7.47,30,68],
[46.62,8.03,44,86],[46.66,8.06,38,78],[46.56,7.98,32,70],[46.68,7.96,28,64],
[46.68,8.59,42,84],[46.63,8.60,36,76],[46.72,8.64,30,66],
[46.82,9.28,35,75],[46.90,9.83,30,68],[46.95,9.90,25,58],[47.05,9.44,22,55],
[46.34,8.99,28,64],[46.47,8.75,24,58],[46.86,8.63,33,72],[47.10,8.76,20,52],
[46.44,7.10,30,68],[46.35,7.99,38,80],[46.39,7.63,42,84],[46.30,7.60,35,74]];
let dbQpr=[];
const qcv=document.createElement('canvas');qcv.width=500;qcv.height=340;const qcx=qcv.getContext('2d');
let qprOverlay=L.imageOverlay(qcv.toDataURL(),[[laMin,loMin],[laMax,loMax]],{opacity:.82});
function renderQprHeat(){
  const GW=250,GH=170,f=new Float32Array(GW*GH);
  const pts=DEMO_QPR.concat(dbQpr);
  for(const[la,lo,cm,q]of pts){
    const gx=(lo-loMin)/(loMax-loMin)*(GW-1),gy=(laMax-la)/(laMax-laMin)*(GH-1);
    const inten=Math.max(0,Math.min(1,(cm/60)))*Math.max(0,Math.min(1,(q/100)));
    if(inten<=0)continue;const R=9;
    for(let dy=-R;dy<=R;dy++){const y=Math.round(gy)+dy;if(y<0||y>=GH)continue;
      for(let dx=-R;dx<=R;dx++){const x=Math.round(gx)+dx;if(x<0||x>=GW)continue;
        const d2=dx*dx+dy*dy;if(d2>R*R)continue;
        f[y*GW+x]+=inten*Math.exp(-d2/(2*3.4*3.4));}}}
  const img=qcx.createImageData(GW,GH),d=img.data;
  for(let i=0;i<GW*GH;i++){const v=Math.min(1,f[i]/1.15);const o=i*4;
    if(v<0.04){d[o+3]=0;continue;}
    const t=Math.pow(v,0.75);
    d[o]=205-190*t|0;d[o+1]=228-198*t|0;d[o+2]=255-130*t|0;
    d[o+3]=60+195*t|0;}
  const tmp=document.createElement('canvas');tmp.width=GW;tmp.height=GH;
  tmp.getContext('2d').putImageData(img,0,0);
  qcx.clearRect(0,0,500,340);qcx.imageSmoothingEnabled=true;
  qcx.drawImage(tmp,0,0,500,340);
  qprOverlay.setUrl(qcv.toDataURL());}
const windArr=L.layerGroup(); const stnGroup=L.layerGroup().addTo(map);
let layer="snow",stat="avg",windowSize=48,a=nowIdx,b=Math.min(T,nowIdx+48),showStn=true,wtimer=null;
const isMobile=window.innerWidth<=560;

function setRaster(get,border){const img=cx.createImageData(W,H),d=img.data;const cls=border?new Int16Array(NP):null;
  for(let p=0;p<NP;p++){const r=get(p);const o=p*4;if(r){d[o]=r[0];d[o+1]=r[1];d[o+2]=r[2];d[o+3]=r[3]==null?210:r[3];if(cls)cls[p]=r[4];}else{d[o+3]=0;if(cls)cls[p]=-999;}}
  if(border){for(let y=0;y<H;y++)for(let x=0;x<W;x++){const p=y*W+x;if(cls[p]==-999)continue;const rt=x<W-1?cls[p+1]:cls[p],bt=y<H-1?cls[p+W]:cls[p];if(rt!=cls[p]||bt!=cls[p]){const o=p*4;d[o]=20;d[o+1]=20;d[o+2]=30;d[o+3]=230;}}}
  cx.putImageData(img,0,0);raster.setUrl(cv.toDataURL());}
function aggT(p,m){let mn=1e9,mx=-1e9,su=0,c=0,cold=0;for(let t=a;t<b;t++){const v=tv(t,p);mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;if(v<0)cold++;}return m=="max"?mx:m=="min"?mn:m=="sub0"?cold:m=="max05"?mx:su/Math.max(1,c);}
function renderRaster(){
  if(layer=="snow"){const ca=a*NP,cb=b*NP;setRaster(p=>{const v=cum[cb+p]-cum[ca+p];const c=snowCol(v);return c?[c[0],c[1],c[2],235]:null;});}
  else if(layer=="depth"){const cb2=b*NP;setRaster(p=>{const v=cum[cb2+p];if(v<1)return null;const x=Math.min(1,v/300);let r,g,bl;if(x<.33){const k=x/.33;r=220-k*120|0;g=240-k*50|0;bl=255-k*5|0;}else if(x<.66){const k=(x-.33)/.33;r=100-k*80|0;g=190-k*90|0;bl=250-k*65|0;}else{const k=(x-.66)/.34;r=20+k*100|0;g=100-k*85|0;bl=185-k*105|0;}return[r,g,bl,215];});}
  else if(layer=="temp"){setRaster(p=>{let mn=1e9,mx=-1e9,su=0,c=0;for(let t=a;t<b;t++){const v=tv(t,p);mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;}
      if(stat=="sub0"){if(mx>=0)return null;const x=Math.min(1,-mx/20);return[40,120-(x*60|0),255,215];}
      if(stat=="max05"){if(mx<0||mx>5)return null;const x=mx/5;return[255,200-(x*110|0),60,235];}
      const v=stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c);const col=tempCol(v);return[col[0],col[1],col[2],205];});}
  else if(layer=="wind"){setRaster(p=>{let mn=1e9,mx=-1e9,su=0,c=0;for(let t=a;t<b;t++){const v=wg_(t,p)*3.6;mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;}
      if(stat=="lt10"){if(mx>=10)return null;return[40,190,90,215];}
      const val=stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c);if(val<0.5)return null;
      const c2=rampBYR(val/70);return[c2[0],c2[1],c2[2],200];});}
  else if(layer=="sun"){const vmax=48;setRaster(p=>{let s=0;for(let t=a;t<b;t++)s+=sunv(t,p);if(s<0.3)return null;const c=sunCol(s,vmax);return[c[0],c[1],c[2],205];});}
  else if(layer=="tsurf"){setRaster(p=>{let mn=1e9,mx=-1e9,su=0,c=0;for(let t=a;t<b;t++){const v=tsurfEst(t,p);mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;}
      if(stat=="sub0"){if(mx>=0)return null;const x=Math.min(1,-mx/20);return[20,80,180,215];}
      if(stat=="max05"){if(mx<0||mx>5)return null;const x=mx/5;return[200,140-(x*80|0),255-(x*200|0),235];}
      const v=stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c);const col=tempCol(v);return[col[0],col[1],col[2],205];});}
  else if(layer=="skiable"){const ca2=a*NP,cb2=b*NP;setRaster(p=>{const slp=mslpv(p);if(slp>55)return[80,0,0,150];const snow=cum[cb2+p]-cum[ca2+p];if(snow<1)return null;const need=minSnowNeeded(p);const r=snow/Math.max(1,need);if(r>=1.5)return[100,220,100,190];if(r>=1.0)return[160,220,100,180];if(r>=.7)return[255,220,50,180];if(r>=.4)return[255,140,40,170];return[255,60,40,160];});}
  else if(layer=="powder"){setRaster(p=>{const r=computePowder(p,a,b);if(!r.powdered)return null;return r.quality==='reduced'?[180,205,245,140]:[200,220,255,180];});}
  else setRaster(_=>null);
}
function windStat(k){let mn=1e9,mx=-1e9,su=0,c=0,ss=0,sc=0;
  for(let t=a;t<b;t++){const v=SPD[t*P+k]/M.spd_mul,dd=WDIR[t*P+k]*M.dir_div*Math.PI/180;
    mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;ss+=Math.sin(dd);sc+=Math.cos(dd);}
  return{v:(stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c)),dir:(Math.atan2(ss,sc)*180/Math.PI+360)%360};}
function renderWind(){windArr.clearLayers();}
function newSnowInt(s){if(!s.hs)return null;let sum=0,have=false;for(let t=a+1;t<b;t++){const h0=s.hs[t-1],h1=s.hs[t];if(h0!=null&&h1!=null){if(h1-h0>0.5)sum+=h1-h0;have=true;}}return have?sum:null;}
function stationCard(s){const ns=newSnowInt(s);
  const row=(k,v)=>v==null?"":`<span class="k">${k}</span><span>${v}</span>`;
  const dirTxt=s.dw!=null?["N","NE","E","SE","S","SW","W","NW"][Math.round(s.dw/45)%8]:null;
  const windStr=s.vw!=null?(dirTxt?dirTxt+" ":"")+(s.vw*3.6).toFixed(0)+" km/h":null;
  return `<div class="scard"><b>${s.label}</b><br><span class="sub">${s.code} · ${s.elev} m asl</span>
    <div class="g">
    ${row("Schneehöhe",s.hs_now!=null?s.hs_now.toFixed(0)+" cm":null)}
    ${row("New Snow",ns!=null?"+"+ns.toFixed(0)+" cm":null)}
    ${row("Air Temp",s.ta!=null?s.ta.toFixed(1)+" °C":null)}
    ${row("Snow Surface",s.tss!=null?s.tss.toFixed(1)+" °C":null)}
    ${row("Wind",windStr)}
    ${row("Wind Dir.",s.dw!=null?s.dw.toFixed(0)+"°":null)}
    </div></div>`;}
// Detail tiers by zoom: 0 = tiny dot, 1 = simple value pill, 2 = full multi-value card
function detailTier(){const z=map.getZoom();if(z<9.4)return 0;if(z<11.2||isMobile)return 1;return 2;}
function renderStations(){stnGroup.clearLayers();if(!showStn)return;
  const dirAb=d=>["N","NE","E","SE","S","SW","W","NW"][Math.round(d/45)%8];
  const tier=detailTier();
  // Thin out at the lowest zoom so dots don't clutter
  const stns=tier===0?M.stations.filter((_,i)=>i%2===0):M.stations;
  for(const s of stns){const ns=newSnowInt(s);
    const hs=s.hs_now!=null?s.hs_now.toFixed(0):"–";
    const nsv=ns!=null?"+"+ns.toFixed(0):"";
    const wind=s.vw!=null?(s.dw!=null?dirAb(s.dw)+" ":"")+(s.vw*3.6).toFixed(0):"";
    const tss=s.tss!=null?s.tss.toFixed(0)+"°":"";
    const ta=s.ta!=null?s.ta.toFixed(0)+"°":"";
    const gust=s.vw!=null?(s.vw*3.6*1.5).toFixed(0):"";
    let lbl=hs;
    if(layer=="wind")lbl=wind||"–";
    else if(layer=="temp"||layer=="tsurf")lbl=ta||"–";
    else if(layer=="sun"||layer=="rad"||layer=="radsun")lbl=s.elev+"m";
    let html,iSize,iAnc;
    if(tier===0){
      html='<div class="stn-dot"></div>';iSize=[12,12];iAnc=[6,6];
    }else if(tier===1){
      let unit='';if(layer=='wind')unit='';else if(layer=='temp'||layer=='tsurf')unit='';
      else if(layer=='sun'||layer=='rad'||layer=='radsun')unit='';else if(lbl!=='–')unit='';
      html='<div class="stn">'+lbl+unit+'</div>';iSize=[46,22];iAnc=[23,11];
    }else{
      let top="",bot="",ll="",lr="";
      if(layer=="wind"){top=gust?("≈"+gust+" gust"):"";bot=s.elev+"m";}
      else if(layer=="temp"||layer=="tsurf"){top=tss?("Surf "+tss):"";lr=hs!="–"?hs+"cm":"";}
      else if(layer=="sun"||layer=="rad"||layer=="radsun"){bot=ta||"";}
      else{top=tss;ll=wind;lr=nsv;bot=ta;}
      const pill=(v,c)=>v?'<span class="s-pill" style="color:'+c+'">'+v+'</span>':'';
      html='<div class="scl"><div class="s-t">'+pill(top,'#6bc5f0')+'</div>'+
        '<div class="s-row">'+pill(ll,'#b0c4de')+'<span class="s-c">'+lbl+'</span>'+pill(lr,'#70d890')+'</div>'+
        '<div class="s-b">'+pill(bot,'#f07070')+'</div></div>';
      iSize=[104,52];iAnc=[52,26];}
    const m=L.marker([s.lat,s.lon],{icon:L.divIcon({className:'',html:html,iconSize:iSize,iconAnchor:iAnc}),zIndexOffset:500});
    m.bindPopup(stationCard(s),{maxWidth:isMobile?240:260});m.addTo(stnGroup);}
}
let _lastTier=-1;
map.on('zoomend',()=>{try{const t=detailTier();if(t!==_lastTier){_lastTier=t;renderStations();loadReportMarkers();}}catch(e){}});
function fmt(i){const d=new Date(M.times[Math.max(0,Math.min(T-1,i))]+"Z");const wd=['So','Mo','Di','Mi','Do','Fr','Sa'][d.getUTCDay()];return wd+' '+d.getUTCDate()+'.'+(d.getUTCMonth()+1)+'., '+d.getUTCHours()+':00';}
function dayLabel(doy){const d=new Date(2026,0,1);d.setDate(doy);return d.toLocaleDateString('de-CH',{day:'2-digit',month:'short'});}
function legendFor(l){const sn={avg:'Mean',max:'Max',min:'Min',sub0:'always <0°C',max05:'Max 0–5°C',lt10:'max <10 km/h'}[stat];
  if(l=="snow"){let h="<b>Neuschnee [cm] (SLF-Skala)</b><br>";for(let i=0;i<SB.length-1;i++)h+=`<div><i style="background:${SC[i]}"></i>${SB[i]}–${SB[i+1]}</div>`;return h+"<div style='margin-top:5px'><span class='stn' style='padding:0 3px'>NN</span> Station (click for details)</div>";}
  if(l=="depth")return '<b>Snow Depth [cm]</b><br><div style="height:12px;border-radius:2px;background:linear-gradient(90deg,rgb(220,240,255),rgb(100,190,250),rgb(30,130,210),rgb(20,100,185),rgb(80,30,140),rgb(120,15,80));margin:4px 0"></div><div style="display:flex;justify-content:space-between;font-size:10px"><span>0</span><span>100</span><span>200</span><span>300+</span></div>';
  if(l=="temp"){let extra="blue=cold · red=warm";if(stat=="sub0")extra="only cells staying below 0°C for entire window";if(stat=="max05")extra="only cells with max 0–5°C";return `<b>Temp 2 m [°C] (${sn})</b><br>${extra}`;}
  if(l=="wind"){if(stat=="lt10")return "<b>Wind 10 m ("+sn+")</b><br>green = max wind stays below 10 km/h";return '<b>Wind 10 m (km/h, '+sn+')</b><br><div style="height:12px;border-radius:2px;background:linear-gradient(90deg,rgb(30,120,255),rgb(30,240,135),rgb(255,240,0),rgb(255,40,0));margin:4px 0"></div><div style="display:flex;justify-content:space-between;font-size:10px"><span>0</span><span>25</span><span>50</span><span>70+</span></div><div style="margin-top:4px;font-size:11px">Arrows show flow direction</div>';}
  if(l=="sun")return "<b>Σ Sunshine Hours</b><br>Scale 0–48 h+ · light→orange = more sun";
  if(l=="rad")return "<b>Clear-sky Radiation [Wh/m²/d]</b><br>Day: "+dayLabel(bandDoy())+" (= window start)<br>dark=shade/low · yellow=high<br>incl. slope, aspect & terrain shadow";
  if(l=="radsun")return "<b>Effective Radiation [Wh/m²/d]</b><br>Clear-sky × cloud attenuation (20% diffuse + 80% × sunshine)<br>Day: "+dayLabel(bandDoy());
  if(l=="slope")return "<b>Slope Classes (swisstopo)</b><br>all classes from 30° (30/35/40/45°+)";
  if(l=="aspect")return '<b>Aspect (Horn, swissALTIRegio 60 m)</b><br><div><i style="background:#4A90D9"></i>N (315°–45°)</div><div><i style="background:#66BB6A"></i>E (45°–135°)</div><div><i style="background:#EF5350"></i>S (135°–225°)</div><div><i style="background:#FFC107"></i>W (225°–315°)</div><div><i style="background:#9E9E9E"></i>Flat (&lt;5°)</div>';
  if(l=="tsurf"){let extra="estimated: air ± radiative cooling/warming";if(stat=="sub0")extra="only cells with max surface temp &lt;0°C";if(stat=="max05")extra="only cells with max 0–5°C";return `<b>T Surface [°C] (${sn})</b><br>${extra}`;}
  if(l=="rough")return "<b>Terrain Roughness</b><br>light→dark brown = rougher";
  if(l=="skiable")return '<b>Skiability Estimate</b><br>Snow depth vs. terrain roughness need<div style="margin-top:4px"><div><i style="background:#64dc64"></i>Plenty of snow</div><div><i style="background:#a0dc64"></i>Skiable</div><div><i style="background:#ffdc32"></i>Marginal</div><div><i style="background:#ff8c28"></i>Needs more snow</div><div><i style="background:#ff3c28"></i>Far from skiable</div><div><i style="background:#500000"></i>Too steep (&gt;55°)</div></div>';
  if(l=="qprheat")return '<b>Powder-Reports (Heatmap)</b><br><div><i style="background:#cde4ff"></i>vereinzelt / wenig</div><div><i style="background:#5b83d9"></i>guter Powder</div><div><i style="background:#0f2a7d"></i>tief &amp; fluffy</div><div style="margin-top:4px;font-size:11px">aus Quick-Powder-Reports · inkl. Demo-Daten</div>';
  if(l=="powder")return '<b>Powder Conditions</b><br><div><i style="background:rgba(200,220,255,.7)"></i>Powder (stable)</div><div><i style="background:rgba(180,205,245,.55)"></i>Powder (reduced)</div><div style="margin-top:4px;font-size:11px">Gust ≈ mean wind × 1.5</div>';
  return "<b>Hillshade / Relief (swisstopo)</b>";}
function legend(l){document.getElementById('legend').innerHTML=legendFor(l||layer);}
document.getElementById('legendBtn').onclick=()=>{const lg=document.getElementById('legend'),btn=document.getElementById('legendBtn');lg.classList.toggle('show');btn.classList.toggle('active');};
function showOverlay(){
  [slopeWMTS,reliefWMTS,aspectGrid,roughImg,radOverlay,qprOverlay].forEach(x=>map.removeLayer(x));
  const grid=(layer=="snow"||layer=="depth"||layer=="temp"||layer=="sun"||layer=="wind"||layer=="powder"||layer=="tsurf"||layer=="skiable");
  const radg=(layer=="rad"||layer=="radsun");
  raster.setOpacity(grid?0.94:0);
  if(radg)map.addLayer(radOverlay);
  if(layer=="slope")map.addLayer(slopeWMTS);
  else if(layer=="shade")map.addLayer(reliefWMTS);
  else if(layer=="aspect")map.addLayer(aspectGrid);
  else if(layer=="rough")map.addLayer(roughImg);
  else if(layer=="qprheat"){map.addLayer(qprOverlay);renderQprHeat();}
  if(layer=="wind"){map.addLayer(windArr);startFlow();}else{map.removeLayer(windArr);stopFlow();}
}
function renderAll(){showOverlay();renderRaster();renderStations();inspAutoRefresh();if(tlMode==='detail')drawTimeline();
  if(layer=="rad"||layer=="radsun")renderRadiation();
  if(layer=="wind"){buildFlow();if(wtimer)clearTimeout(wtimer);wtimer=setTimeout(renderWind,120);}
  document.getElementById('window').innerHTML=`${b-a}h Fenster`;syncTl();legend();}
// --- Simple / Detailed timeline mode (detailed = default) ---
let tlMode='detail',sliderActive=false;
function syncTl(){const wv=b-a;
  const lv=document.getElementById('tlLenVal');if(lv)lv.textContent=wv+'h';
  const sl=document.getElementById('tlLen');if(sl&&!sliderActive)sl.value=Math.min(parseInt(sl.max),Math.max(parseInt(sl.min),wv));
  const rg=document.getElementById('tlRange');if(rg)rg.textContent=fmt(a)+'  →  '+fmt(b-1);}
function setTlMode(m){tlMode=m;const bp=document.getElementById('bottomPanel');bp.classList.toggle('detail',m==='detail');
  document.querySelectorAll('#tlModeToggle button').forEach(x=>x.classList.toggle('active',x.dataset.m===m));
  if(m==='detail')drawTimeline();
  if(typeof panelRestore==='function')panelRestore();}
document.querySelectorAll('#tlModeToggle button').forEach(x=>x.onclick=()=>setTlMode(x.dataset.m));
(function(){const sl=document.getElementById('tlLen');if(!sl)return;sl.max=Math.min(168,T);
  sl.addEventListener('input',()=>{sliderActive=true;const ws=Math.min(T,parseInt(sl.value));windowSize=ws;
    const center=Math.round((a+b)/2);a=Math.max(0,Math.min(T-ws,center-Math.floor(ws/2)));b=Math.min(T,a+ws);
    document.getElementById('tlLenVal').textContent=(b-a)+'h';drawTimeline();document.getElementById('window').innerHTML=(b-a)+'h Fenster';document.getElementById('tlRange').textContent=fmt(a)+'  →  '+fmt(b-1);});
  sl.addEventListener('change',()=>{sliderActive=false;clearPresets();renderAll();});
  const lenBtn=document.getElementById('tlLenBtn');if(lenBtn)lenBtn.onclick=()=>{const show=sl.style.display==='none';sl.style.display=show?'block':'none';lenBtn.classList.toggle('active',show);};
  document.getElementById('tlPrev').onclick=()=>{const ws=b-a;a=Math.max(0,a-ws);b=Math.min(T,a+ws);clearPresets();renderAll();};
  document.getElementById('tlNext').onclick=()=>{const ws=b-a;b=Math.min(T,b+ws);a=Math.max(0,b-ws);clearPresets();renderAll();};
  document.getElementById('tlNow').onclick=()=>{const ws=b-a;a=Math.max(0,Math.min(T-ws,nowIdx-Math.floor(ws/2)));b=Math.min(T,a+ws);clearPresets();renderAll();};
})();
const TOPICS={
  ski:[{l:'skiable',s:'avg',label:'Skiable'},{l:'powder',s:'avg',label:'Powder'}],
  qpr:[{l:'qprheat',s:'avg',label:'Heatmap'}],
  snow:[{l:'snow',s:'avg',label:'Neuschnee'},{l:'depth',s:'avg',label:'Schneehöhe'}],
  temp:[{l:'temp',s:'avg',label:'Mean'},{l:'temp',s:'max',label:'Max'},{l:'temp',s:'min',label:'Min'},{l:'temp',s:'sub0',label:'<0°C'},{l:'temp',s:'max05',label:'0-5°C'},{l:'tsurf',s:'avg',label:'Surface'}],
  wind:[{l:'wind',s:'avg',label:'Mean'},{l:'wind',s:'max',label:'Max'},{l:'wind',s:'min',label:'Min'},{l:'wind',s:'lt10',label:'<10 km/h'}],
  rad:[{l:'rad',s:'avg',label:'Clear-sky'},{l:'radsun',s:'avg',label:'Effective'},{l:'sun',s:'avg',label:'Sunshine'}],
  terrain:[{l:'slope',s:'avg',label:'Slope'},{l:'aspect',s:'avg',label:'Aspect'},{l:'rough',s:'avg',label:'Roughness'},{l:'shade',s:'avg',label:'Hillshade'}]
};
let curTopic='snow';
// [accentHex, sublayerTint, frameBorder, vignetteGlow]
const TOPIC_COLOR={
  ski:['#0aa06e','rgba(10,160,110,.14)','rgba(10,160,110,.5)','rgba(10,160,110,.10)'],
  snow:['#5e5ce6','rgba(94,92,230,.13)','rgba(94,92,230,.5)','rgba(94,92,230,.10)'],
  temp:['#e8590c','rgba(232,89,12,.13)','rgba(232,89,12,.52)','rgba(232,89,12,.12)'],
  wind:['#0d9488','rgba(13,148,136,.14)','rgba(13,148,136,.5)','rgba(13,148,136,.10)'],
  rad:['#f59e0b','rgba(245,158,11,.16)','rgba(245,158,11,.55)','rgba(245,158,11,.13)'],
  terrain:['#64748b','rgba(100,116,139,.16)','rgba(100,116,139,.5)','rgba(100,116,139,.10)'],
  qpr:['#1e3a8a','rgba(30,58,138,.14)','rgba(30,58,138,.5)','rgba(30,58,138,.10)']};
let tlSel='#5e5ce6',tlSelTint='rgba(94,92,230,.12)';
function setTopic(t,subIdx){
  curTopic=t;
  document.querySelectorAll('#topics button[data-t]').forEach(x=>x.classList.toggle('active',x.dataset.t===t));
  const isMore=['temp','wind','rad','terrain','qpr'].includes(t);
  document.getElementById('moreTopics').classList.toggle('sel',isMore);
  document.querySelectorAll('#topicsMore button').forEach(x=>x.classList.toggle('active',x.dataset.t===t));
  const tc=TOPIC_COLOR[t]||TOPIC_COLOR.snow;const lb=document.getElementById('layerBar');
  lb.style.setProperty('--topic-accent',tc[0]);lb.style.setProperty('--topic-tint',tc[1]);
  tlSel=tc[0];tlSelTint=tc[1];
  document.documentElement.style.setProperty('--topic-accent',tc[0]);
  document.querySelectorAll('meta[name="theme-color"]').forEach(m=>m.setAttribute('content','#ffffff'));
  document.documentElement.style.setProperty('--edge-tint','rgba(255,255,255,.92)');
  document.getElementById('topics').classList.remove('expanded');document.getElementById('layerBar').classList.remove('expanded');
  if(typeof positionSearch==='function')requestAnimationFrame(positionSearch);
  const subs=document.getElementById('sublayers');
  const items=TOPICS[t];
  subs.innerHTML=items.map((s,i)=>'<button data-i="'+i+'"'+(i===(subIdx||0)?' class="active"':'')+'>'+s.label+'</button>').join('');
  subs.querySelectorAll('button').forEach(btn=>{
    btn.onclick=()=>{subs.querySelectorAll('button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');
      const sub=items[parseInt(btn.dataset.i)];layer=sub.l;stat=sub.s;renderAll();};
    btn.onmouseenter=()=>legend(items[parseInt(btn.dataset.i)].l);btn.onmouseleave=()=>legend();});
  const sel=items[subIdx||0];layer=sel.l;stat=sel.s;renderAll();
}
document.querySelectorAll('#topics button[data-t]').forEach(btn=>{
  btn.onclick=()=>setTopic(btn.dataset.t);
  btn.onmouseenter=()=>legend(TOPICS[btn.dataset.t][0].l);btn.onmouseleave=()=>legend();});
// The expanded "Mehr" menu lives OUTSIDE #topics — bind its buttons too
// (they were dead: tapping Temperatur/Wind/Powder-Reports did nothing).
document.querySelectorAll('#topicsMore button[data-t]').forEach(btn=>{
  btn.onclick=e=>{e.stopPropagation();setTopic(btn.dataset.t);};
  btn.onmouseenter=()=>legend(TOPICS[btn.dataset.t][0].l);btn.onmouseleave=()=>legend();});
function positionSearch(){try{var sw=document.getElementById('searchWrap'),lb=document.getElementById('layerBar');if(!sw||!lb)return;var r=lb.getBoundingClientRect();sw.style.top=(r.bottom+8)+'px';}catch(e){}}
addEventListener('resize',positionSearch);requestAnimationFrame(positionSearch);
function presetsFade(){const p=document.getElementById('presets');if(!p)return;p.classList.toggle('can-scroll',p.scrollWidth-p.clientWidth-p.scrollLeft>4);}
(function(){const p=document.getElementById('presets');if(p)p.addEventListener('scroll',presetsFade,{passive:true});addEventListener('resize',presetsFade);requestAnimationFrame(presetsFade);})();
(function(){const mt=document.getElementById('moreTopics'),topics=document.getElementById('topics');
  mt.onclick=e=>{e.stopPropagation();const open=topics.classList.toggle('expanded');document.getElementById('layerBar').classList.toggle('expanded',open);requestAnimationFrame(positionSearch);};
  document.addEventListener('click',e=>{const menu=document.getElementById('topicsMore');
    if(topics.classList.contains('expanded')&&!menu.contains(e.target)&&!mt.contains(e.target)){topics.classList.remove('expanded');document.getElementById('layerBar').classList.remove('expanded');requestAnimationFrame(positionSearch);}});
})();
function clearPresets(){document.querySelectorAll('#presets button').forEach(x=>x.classList.remove('active'));}
document.querySelectorAll('#presets button[data-d]').forEach(btn=>{btn.onclick=()=>{
  clearPresets();btn.classList.add('active');
  windowSize=parseInt(btn.dataset.d);const center=Math.round((a+b)/2);
  a=Math.max(0,Math.min(T-windowSize,center-Math.floor(windowSize/2)));b=Math.min(T,a+windowSize);renderAll();};});
document.querySelectorAll('#presets button[data-r]').forEach(btn=>{btn.onclick=()=>{
  const p=tillTomorrow();a=p[0];b=p[1];windowSize=b-a;
  clearPresets();btn.classList.add('active');renderAll();};});
document.getElementById('btnSinceSnow').onclick=()=>{const p=sinceLastSnowfall();a=p[0];b=p[1];windowSize=b-a;
  clearPresets();document.getElementById('btnSinceSnow').classList.add('active');renderAll();};
// --- Timeline Drag (desktop + mobile, edge resize + click-to-jump) ---
(function(){const tc=document.getElementById('timeline');let mode=null,dragStartX=0,dragStartA=0,dragStartB=0,ws=0;
  const EDGE=12;
  function getZone(cx){const rect=tc.getBoundingClientRect();
    const x1=a/T*rect.width+rect.left,x2=b/T*rect.width+rect.left;
    if(Math.abs(cx-x1)<EDGE)return'left';
    if(Math.abs(cx-x2)<EDGE)return'right';
    if(cx>x1&&cx<x2)return'center';
    return'outside';}
  function startDrag(e){const cx=e.touches?e.touches[0].clientX:e.clientX;
    const zone=getZone(cx);
    if(zone==='outside'){
      const rect=tc.getBoundingClientRect();const clickT=Math.round((cx-rect.left)/rect.width*T);
      const hw=Math.floor(windowSize/2);a=Math.max(0,Math.min(T-windowSize,clickT-hw));b=a+windowSize;
      renderAll();return;}
    mode=zone;dragStartX=cx;dragStartA=a;dragStartB=b;ws=b-a;tc.style.cursor=zone==='center'?'grabbing':'col-resize';e.preventDefault();}
  tc.addEventListener('mousedown',startDrag);tc.addEventListener('touchstart',startDrag,{passive:false});
  function onDrag(e){if(!mode)return;e.preventDefault();const cx=e.touches?e.touches[0].clientX:e.clientX;
    const rect=tc.getBoundingClientRect();const delta=Math.round((cx-dragStartX)/rect.width*T);
    if(mode==='center'){let na=Math.max(0,Math.min(T-ws,dragStartA+delta));a=na;b=na+ws;}
    else if(mode==='left'){a=Math.max(0,Math.min(dragStartB-4,dragStartA+delta));windowSize=b-a;}
    else if(mode==='right'){b=Math.min(T,Math.max(dragStartA+4,dragStartB+delta));windowSize=b-a;}
    drawTimeline();document.getElementById('window').innerHTML=(b-a)+'h Fenster';}
  document.addEventListener('mousemove',onDrag);document.addEventListener('touchmove',onDrag,{passive:false});
  function endDrag(){if(mode){mode=null;tc.style.cursor='default';renderAll();}}
  document.addEventListener('mouseup',endDrag);document.addEventListener('touchend',endDrag);
  tc.addEventListener('mousemove',function(e){if(mode)return;const cx=e.clientX;const zone=getZone(cx);
    tc.style.cursor=zone==='center'?'grab':zone==='left'||zone==='right'?'col-resize':'crosshair';});
})();

function toggleReportLayer(on){if(on)map.addLayer(reportMarkers);else map.removeLayer(reportMarkers);}
// Marker-visibility popover on the control rail
function toggleStations(){showStn=!showStn;renderStations();
  const btn=document.getElementById('railToggles');btn.classList.toggle('active',showStn);
  const rail=document.getElementById('ctrlRail');
  let note=document.getElementById('railNote');
  if(!note){note=document.createElement('div');note.id='railNote';note.className='rail-note';rail.appendChild(note);}
  note.textContent=showStn?'Stationen an':'Stationen aus';
  note.style.top=(btn.offsetTop+10)+'px';note.classList.add('show');
  clearTimeout(note._t);note._t=setTimeout(()=>note.classList.remove('show'),1500);haptic(6);}
// --- Rail: account button + locate-me ---
function accountTap(){if(sbUser)openProfile();else authShow();}
function updateAccountBtn(avatarUrl){const btn=document.getElementById('accountBtn');if(!btn)return;
  if(!sbUser){btn.classList.remove('signed');btn.title='Anmelden';
    btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>';return;}
  btn.classList.add('signed');btn.title='Mein Profil';
  if(avatarUrl){btn.innerHTML='<span class="acct-img" style="background-image:url('+avatarUrl+')"></span>';}
  else{btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';}}
let meMarker=null;
function flyToMe(){haptic(8);
  if(!navigator.geolocation){toast('Standort nicht verfügbar','err');return;}
  navigator.geolocation.getCurrentPosition(p=>{const ll=[p.coords.latitude,p.coords.longitude];
    try{
      if(meMarker)map.removeLayer(meMarker);
      meMarker=L.marker(ll,{icon:L.divIcon({className:'',html:'<div class="me-dot"></div>',iconSize:[18,18],iconAnchor:[9,9]}),interactive:false,zIndexOffset:1900}).addTo(map);
      map.flyTo(ll,13,{duration:1.1});
    }catch(e){}},
    ()=>{toast('Standort konnte nicht ermittelt werden','err');},{enableHighAccuracy:true,timeout:9000});}
// --- Bottom panel: expand (content height) / collapse (tap or swipe the handle) ---
let panelRestore=null,panelCollapsed=false;
(function(){const bp=document.getElementById('bottomPanel'),tl=document.getElementById('tlToggle'),btm=document.getElementById('btmMain');
  const minH=16;
  function invalidate(){try{map.invalidateSize({animate:false,pan:false});}catch(e){}}
  function updH(){document.documentElement.style.setProperty('--btm-h',bp.offsetHeight+'px');}
  // Collapse only hides the detailed chart — the mode toggle + presets stay
  // visible, so the time controls are never fully hidden.
  function apply(){bp.classList.toggle('collapsed',panelCollapsed);bp.style.height='';btm.style.display='';
    updH();requestAnimationFrame(()=>{updH();invalidate();});}
  requestAnimationFrame(apply);
  window.addEventListener('resize',()=>{if(!panelCollapsed){updH();invalidate();}});
  let sy=0,drag=false,moved=0;
  function down(e){drag=true;sy=e.touches?e.touches[0].clientY:e.clientY;moved=0;}
  function move(e){if(!drag)return;const y=e.touches?e.touches[0].clientY:e.clientY;moved=y-sy;}
  function up(){if(!drag)return;drag=false;
    if(moved>24)panelCollapsed=true;else if(moved<-24)panelCollapsed=false;else panelCollapsed=!panelCollapsed;
    apply();}
  tl.addEventListener('mousedown',down);tl.addEventListener('touchstart',down,{passive:true});
  document.addEventListener('mousemove',move);document.addEventListener('touchmove',move,{passive:true});
  document.addEventListener('mouseup',up);document.addEventListener('touchend',up);
  // Called after content changes (e.g. Simple↔Detailed) to recompute height
  panelRestore=function(){if(panelCollapsed){panelCollapsed=false;}apply();};
})();
// --- Windy.com-style Wind Animation ---
const flow=document.getElementById('flow'),fx=flow.getContext('2d');
const loMinW=Math.min(...M.wind.lon),loMaxW=Math.max(...M.wind.lon),laMinW=Math.min(...M.wind.lat),laMaxW=Math.max(...M.wind.lat);
let flowVel=null,parts=[],flowReq=null;
const FLOW_N=800,FLOW_MAX_AGE=90,FLOW_FADE=0.08;
function buildFlow(){flowVel=new Array(P);for(let k=0;k<P;k++){const w=windStat(k);const A=(w.dir+180)*Math.PI/180;flowVel[k]={ux:Math.sin(A),uy:-Math.cos(A),sp:w.v};}}
function wIdx(lat,lon){let ix=Math.round((lon-loMinW)/(loMaxW-loMinW)*(M.wind.nx-1)),iy=Math.round((lat-laMinW)/(laMaxW-laMinW)*(M.wind.ny-1));ix=Math.max(0,Math.min(M.wind.nx-1,ix));iy=Math.max(0,Math.min(M.wind.ny-1,iy));return iy*M.wind.nx+ix;}
function flowResize(){const s=map.getSize();flow.width=s.x;flow.height=s.y;}
function fspawn(){return{x:Math.random()*flow.width,y:Math.random()*flow.height,age:Math.random()*FLOW_MAX_AGE|0,maxAge:FLOW_MAX_AGE*(0.6+Math.random()*0.4)|0};}
function startFlow(){if(flowReq)return;flowResize();parts=[];for(let i=0;i<FLOW_N;i++)parts.push(fspawn());fx.clearRect(0,0,flow.width,flow.height);animFlow();}
function stopFlow(){if(flowReq)cancelAnimationFrame(flowReq);flowReq=null;fx.clearRect(0,0,flow.width,flow.height);}
function animFlow(){if(layer!="wind"||!flowVel){stopFlow();return;}
  fx.globalCompositeOperation='destination-out';fx.fillStyle=`rgba(255,255,255,${FLOW_FADE+0.02})`;fx.fillRect(0,0,flow.width,flow.height);
  fx.globalCompositeOperation='source-over';
  for(const p of parts){const ll=map.containerPointToLatLng([p.x,p.y]);const v=flowVel[wIdx(ll.lat,ll.lng)];
    const kmh=v.sp*3.6,sc=0.5+kmh*0.12;
    const nx=p.x+v.ux*sc,ny=p.y+v.uy*sc;
    const life=1-p.age/p.maxAge,alpha=Math.min(0.85,life*0.9+0.15);
    const c=rampBYR(kmh/70);const lw=1.0+Math.min(2.5,kmh*0.05);
    fx.strokeStyle=`rgba(${Math.max(0,c[0]-40)},${Math.max(0,c[1]-40)},${Math.max(0,c[2]-40)},${alpha.toFixed(2)})`;fx.lineWidth=lw;
    fx.beginPath();fx.moveTo(p.x,p.y);fx.lineTo(nx,ny);fx.stroke();
    if(p.age>3&&p.age%8===0){const asz=3+kmh*0.06;const ang=Math.atan2(v.uy,v.ux);
      fx.fillStyle=fx.strokeStyle;fx.beginPath();fx.moveTo(nx+Math.cos(ang)*asz,ny+Math.sin(ang)*asz);
      fx.lineTo(nx+Math.cos(ang+2.5)*asz*.7,ny+Math.sin(ang+2.5)*asz*.7);
      fx.lineTo(nx+Math.cos(ang-2.5)*asz*.7,ny+Math.sin(ang-2.5)*asz*.7);fx.fill();}
    p.x=nx;p.y=ny;p.age++;if(p.age>p.maxAge||nx<-10||ny<-10||nx>flow.width+10||ny>flow.height+10)Object.assign(p,fspawn());}
  flowReq=requestAnimationFrame(animFlow);}
map.on('move',()=>{if(layer=="wind"){flowResize();fx.clearRect(0,0,flow.width,flow.height);}});
map.on('resize',()=>{if(layer=="wind")flowResize();});
drawTimeline();
// --- Point Inspector (universal click popup) ---
map.setMaxBounds([[laMinW-0.05,loMinW-0.05],[laMaxW+0.05,loMaxW+0.05]]);
map.setMinZoom(map.getBoundsZoom([[laMinW,loMinW],[laMaxW,loMaxW]])+0.3);
function iTabSw(el,tab){const c=el.closest('.icard');c.querySelectorAll('.itab').forEach(x=>x.classList.remove('active'));el.classList.add('active');c.querySelectorAll('.ipane').forEach(x=>x.classList.toggle('active',x.dataset.p===tab));}
// --- Map-click inspect panel with charts + red X marker ---
let inspMarker=null;
function inspClose(){document.getElementById('inspPanel').classList.remove('open');document.body.classList.remove('insp-open');if(inspMarker){map.removeLayer(inspMarker);inspMarker=null;}}
map.on('click',function(e){inspOpen(e.latlng.lat,e.latlng.lng);});
let inspLast=null;
function inspAutoRefresh(){try{if(inspLast&&document.getElementById('inspPanel').classList.contains('open'))inspOpen(inspLast.lat,inspLast.lon);}catch(e){}}
function inspOpen(lat,lon){inspLast={lat,lon};document.body.classList.add('insp-open');
  const cx2=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy2=Math.round((laMax-lat)/(laMax-laMin)*(H-1));
  if(cx2<0||cx2>=W||cy2<0||cy2>=H)return;
  const p=cy2*W+cx2,elev=melevv(p),wk=mainToWind[p];
  if(inspMarker)map.removeLayer(inspMarker);
  inspMarker=L.marker([lat,lon],{icon:L.divIcon({className:'',html:'<div class="insp-xmark"><svg viewBox="0 0 24 24" fill="none" stroke="#e0245e" stroke-width="3.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></div>',iconSize:[30,30],iconAnchor:[15,15]}),interactive:false,zIndexOffset:2000}).addTo(map);
  const ca=a*NP,cb=b*NP;const newSnow=cum[cb+p]-cum[ca+p],depthNow=cum[cb+p];
  let wsum=0;for(let t=a;t<b;t++)wsum+=SPD[t*P+wk]/M.spd_mul*3.6;const wmean=wsum/Math.max(1,b-a);
  const asp=maspv(p),slp=mslpv(p),quad=aspectQ(asp),QD={N:'Nord',E:'Ost',S:'Süd',W:'West'};
  const pan=document.getElementById('inspPanel');
  pan.innerHTML=
   '<div class="insp-head"><div class="insp-t"><b>'+lat.toFixed(4)+'° N, '+lon.toFixed(4)+'° E</b>'+
     '<div class="insp-chips"><span class="insp-chip">'+ic('peak')+' '+elev.toFixed(0)+' m</span><span class="insp-chip accent">'+(QD[quad]||quad)+' · '+asp.toFixed(0)+'°</span><span class="insp-chip">'+slp.toFixed(0)+'°</span></div>'+
   '</div><button aria-label="Schliessen" onclick="inspClose()">✕</button></div>'+
   '<div class="insp-body">'+
     '<div class="insp-sec"><h4>Neuschnee <em>+'+newSnow.toFixed(1)+' cm</em></h4><canvas id="icNew"></canvas></div>'+
     '<div class="insp-sec"><h4>Schneehöhe <em>'+depthNow.toFixed(0)+' cm</em></h4><canvas id="icDepth"></canvas></div>'+
     '<div class="insp-sec"><h4>Temperatur <em>Luft · Oberfläche</em></h4><canvas id="icTemp"></canvas></div>'+
     '<div class="insp-sec"><h4>Windrose <em>Ø '+wmean.toFixed(0)+' km/h</em></h4><canvas id="icWind"></canvas></div>'+
     '<div class="insp-sec"><h4>Strahlung <em>% vom Tagesmaximum</em></h4><canvas id="icRad"></canvas></div>'+
   '</div>';
  pan.classList.add('open');
  inspApplyDrag(pan,false);inspAttachDrag(pan);
  requestAnimationFrame(()=>{
    icNewSnow(document.getElementById('icNew'),p);
    icDepth(document.getElementById('icDepth'),p);
    icTemp(document.getElementById('icTemp'),p);
    icWind(document.getElementById('icWind'),p,wk);
    icRad(document.getElementById('icRad'),p);
  });
}
// --- Inspect panel: drag anywhere, snap to the nearest side edge on release.
// Offsets persist for the session so the panel reopens where the user left it.
let inspDX=0,inspDY=0;
function inspApplyDrag(pan,animate){
  pan.style.transition=animate?'transform .32s cubic-bezier(.34,1.4,.64,1)':'none';
  pan.style.transform=(inspDX||inspDY)?('translate('+inspDX+'px,'+inspDY+'px)'):'';}
function inspSnap(pan){
  const r=pan.getBoundingClientRect(),vw=window.innerWidth,vh=window.innerHeight,m=12;
  if(r.width<vw-2*m-20){ // free horizontal space -> snap to nearest side edge
    const target=(r.left+r.width/2<vw/2)?m:(vw-m-r.width);
    inspDX+=target-r.left;}
  const top=Math.max(m,Math.min(r.top,vh-r.height-m)); // keep fully on screen
  inspDY+=top-r.top;
  inspApplyDrag(pan,true);}
function inspAttachDrag(pan){
  if(pan._drag)return;pan._drag=true;
  pan.style.touchAction='pan-y';
  let sx=0,sy=0,bx=0,by=0,on=false,decided=false,doDrag=false;
  function begin(e){pan.classList.add('dragging');pan.style.transition='none';try{pan.setPointerCapture(e.pointerId);}catch(_){}}
  pan.addEventListener('pointerdown',e=>{if(e.target.closest('button'))return;
    on=true;sx=e.clientX;sy=e.clientY;bx=inspDX;by=inspDY;
    doDrag=!!e.target.closest('.insp-head');decided=doDrag;if(doDrag)begin(e);});
  pan.addEventListener('pointermove',e=>{if(!on)return;
    const dx=e.clientX-sx,dy=e.clientY-sy;
    if(!decided){if(Math.abs(dx)<7&&Math.abs(dy)<7)return;
      decided=true;doDrag=Math.abs(dx)>=Math.abs(dy);
      if(doDrag)begin(e);else{on=false;return;}}
    if(!doDrag)return;inspDX=bx+dx;inspDY=by+dy;inspApplyDrag(pan,false);});
  const fin=()=>{if(!on)return;on=false;if(doDrag){pan.classList.remove('dragging');inspSnap(pan);}};
  pan.addEventListener('pointerup',fin);pan.addEventListener('pointercancel',fin);}
function icSetup(cv,hh,wOverride){const dpr=window.devicePixelRatio||1;const ww=wOverride||cv.getBoundingClientRect().width||320;
  cv.width=Math.round(ww*dpr);cv.height=Math.round(hh*dpr);cv.style.height=hh+'px';if(wOverride)cv.style.width=ww+'px';
  const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,ww,hh);return{ctx,w:ww,h:hh};}
function icRR(ctx,x,y,w,h,r){r=Math.max(0,Math.min(r,w/2,h/2));ctx.beginPath();if(ctx.roundRect)ctx.roundRect(x,y,w,h,r);else{ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}}
function icDayGrid(ctx,x0,plotW,h,t0,t1,baseY){ctx.textAlign='center';ctx.font='600 11px Inter,system-ui';let _lx=-1e9;
  for(let t=t0;t<t1;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===0){const x=x0+(t-t0)/(t1-t0)*plotW;
    ctx.strokeStyle='rgba(22,21,46,.05)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,6);ctx.lineTo(x,baseY);ctx.stroke();
    if(x-_lx>=52){ctx.fillStyle='rgba(90,110,130,.75)';ctx.fillText(d.toLocaleDateString('de-CH',{day:'2-digit',month:'short'}),x+22,h-3);_lx=x;}}}}
function icPill(ctx,x,y,txt,col,w){ctx.font='800 11px Inter';const tw=ctx.measureText(txt).width+12,hh=17;
  let px=Math.max(2,Math.min(x-tw/2,w-tw-2)),py=Math.max(2,y);
  ctx.fillStyle='rgba(255,255,255,.94)';icRR(ctx,px,py,tw,hh,8.5);ctx.fill();
  ctx.strokeStyle='rgba(22,21,46,.1)';ctx.lineWidth=1;icRR(ctx,px,py,tw,hh,8.5);ctx.stroke();
  ctx.fillStyle=col;ctx.textAlign='center';ctx.fillText(txt,px+tw/2,py+12.5);}
function icNewSnow(cv,p){const{ctx,w,h}=icSetup(cv,86);const t0=a,t1=b,n=Math.max(1,t1-t0);const LG=4,baseY=h-16,plotW=w-LG-4;
  let mx=0,mi=0;const vals=[];for(let t=t0;t<t1;t++){const v=SNOW[t*NP+p]*M.snow_scale;vals.push(v);if(v>mx){mx=v;mi=t-t0;}}
  const mxs=Math.max(0.05,mx);
  icDayGrid(ctx,LG,plotW,h,t0,t1,baseY);
  ctx.strokeStyle='rgba(22,21,46,.12)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(LG,baseY+.5);ctx.lineTo(w,baseY+.5);ctx.stroke();
  const bw=plotW/n,g=ctx.createLinearGradient(0,8,0,baseY);g.addColorStop(0,'#9b8cff');g.addColorStop(1,'#5e5ce6');ctx.fillStyle=g;
  for(let i=0;i<n;i++){const v=vals[i];if(v<0.002)continue;const bh=Math.max(1.5,v/mxs*(baseY-26));icRR(ctx,LG+i*bw+.4,baseY-bh,Math.max(bw-1,1.2),bh,Math.min(2,bw/2.2));ctx.fill();}
  if(mx>0.002){const px=LG+mi*bw+bw/2,bh=Math.max(1.5,mx/mxs*(baseY-26));
    icPill(ctx,px,baseY-bh-21,mx.toFixed(mx>=1?1:2)+' cm/h','#4844c9',w);}}
function icDepth(cv,p){const{ctx,w,h}=icSetup(cv,80);const t0=a,t1=b;const LG=4,baseY=h-16,plotW=w-LG-4;
  const vals=[];let mx=1,mi=0;for(let t=t0;t<=t1;t++){const v=cum[t*NP+p];vals.push(v);if(v>mx){mx=v;mi=t-t0;}}
  icDayGrid(ctx,LG,plotW,h,t0,t1,baseY);
  ctx.strokeStyle='rgba(22,21,46,.12)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(LG,baseY+.5);ctx.lineTo(w,baseY+.5);ctx.stroke();
  const Ln=vals.length,px=i=>LG+i/(Ln-1)*plotW,py=v=>baseY-v/mx*(baseY-26);
  ctx.beginPath();for(let i=0;i<Ln;i++){const x=px(i),y=py(vals[i]);i?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.lineTo(px(Ln-1),baseY);ctx.lineTo(LG,baseY);ctx.closePath();ctx.fillStyle='rgba(94,92,230,.13)';ctx.fill();
  ctx.beginPath();for(let i=0;i<Ln;i++){const x=px(i),y=py(vals[i]);i?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.strokeStyle='#4844c9';ctx.lineWidth=2.2;ctx.stroke();
  icPill(ctx,px(mi),py(vals[mi])-21,vals[mi].toFixed(0)+' cm','#4844c9',w);}
function icTemp(cv,p){const{ctx,w,h}=icSetup(cv,110);const t0=a,t1=Math.max(a+1,b),n=t1-t0;
  const baseY=h-16,topY=18,plotW=w-8,xOf=i2=>4+i2/Math.max(1,n-1)*plotW;
  let mn=1e9,mx=-1e9;const air=[],srf=[];
  for(let t=t0;t<t1;t++){const va=tv(t,p),vs=tsurfEst(t,p);air.push(va);srf.push(vs);mn=Math.min(mn,va,vs);mx=Math.max(mx,va,vs);}
  mn=Math.floor(mn-1);mx=Math.ceil(mx+1);const rng=Math.max(1,mx-mn),yOf=v=>baseY-(v-mn)/rng*(baseY-topY);
  icDayGrid(ctx,4,plotW,h,t0,t1,baseY);
  if(mx>0){const y5=yOf(Math.min(5,mx)),y0=yOf(Math.max(0,mn));ctx.fillStyle='rgba(245,166,35,.09)';ctx.fillRect(4,y5,plotW,Math.max(0,y0-y5));}
  function refLine(v,col,lbl){if(v<mn||v>mx)return;const y=yOf(v);
    ctx.strokeStyle=col;ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(4,y);ctx.lineTo(4+plotW,y);ctx.stroke();ctx.setLineDash([]);
    ctx.font='800 10px Inter';ctx.textAlign='left';ctx.fillStyle='rgba(255,255,255,.85)';ctx.fillRect(6,y-11,20,12);ctx.fillStyle=col;ctx.fillText(lbl,8,y-2);}
  refLine(0,'rgba(224,36,94,.55)','0°');refLine(5,'rgba(217,138,31,.6)','5°');
  if(nowIdx>=t0&&nowIdx<t1){const x=xOf(nowIdx-t0);ctx.strokeStyle='rgba(22,21,46,.4)';ctx.setLineDash([2,2]);ctx.beginPath();ctx.moveTo(x,topY);ctx.lineTo(x,baseY);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#e0245e';ctx.font='700 9.5px Inter';ctx.textAlign='center';ctx.fillText('JETZT',x,topY-5);}
  function line(arr,col,wd){ctx.beginPath();for(let i2=0;i2<n;i2++){const x=xOf(i2),y=yOf(arr[i2]);i2?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.strokeStyle=col;ctx.lineWidth=wd;ctx.lineJoin='round';ctx.stroke();}
  line(srf,'#f5a623',1.6);line(air,'#5e5ce6',2.2);
  let aMx=-1e9,aMxI=0,aMn=1e9,aMnI=0;
  for(let i2=0;i2<n;i2++){if(air[i2]>aMx){aMx=air[i2];aMxI=i2;}if(air[i2]<aMn){aMn=air[i2];aMnI=i2;}}
  icPill(ctx,xOf(aMxI),yOf(aMx)-22,'Hoch '+aMx.toFixed(0)+'°','#c2410c',w);
  icPill(ctx,xOf(aMnI),Math.min(baseY-18,yOf(aMn)+6),'Tief '+aMn.toFixed(0)+'°','#4844c9',w);
  ctx.textAlign='left';ctx.font='700 10.5px Inter';ctx.fillStyle='#5e5ce6';ctx.fillText('● Luft',6,12);ctx.fillStyle='#f5a623';ctx.fillText('● Oberfläche',46,12);}
function icWind(cv,p,wk){const{ctx,w,h}=icSetup(cv,152);const cx=w/2,cy=h/2+2,R=Math.min(cx,cy)-18;
  const NS=8,cnt=new Array(NS).fill(0),spd=new Array(NS).fill(0);let n=0,tot=0;
  for(let t=a;t<b;t++){let dir=(WDIR[t*P+wk]*M.dir_div)%360;if(dir<0)dir+=360;const s=SPD[t*P+wk]/M.spd_mul*3.6;const si=Math.round(dir/(360/NS))%NS;cnt[si]++;spd[si]+=s;n++;tot+=s;}
  for(let i=0;i<NS;i++)if(cnt[i])spd[i]/=cnt[i];
  const mxC=Math.max(1,...cnt);
  ctx.strokeStyle='rgba(22,21,46,.09)';ctx.lineWidth=1;for(let r=1;r<=3;r++){ctx.beginPath();ctx.arc(cx,cy,R*r/3,0,2*Math.PI);ctx.stroke();}
  const labs=['N','NE','E','SE','S','SW','W','NW'];ctx.fillStyle='rgba(90,110,130,.8)';ctx.font='700 10.5px Inter';ctx.textAlign='center';ctx.textBaseline='middle';
  for(let i=0;i<NS;i++){const ang=i*(2*Math.PI/NS)-Math.PI/2;ctx.fillText(labs[i],cx+Math.cos(ang)*(R+10),cy+Math.sin(ang)*(R+10));}
  for(let i=0;i<NS;i++){if(!cnt[i])continue;const ang=i*(2*Math.PI/NS)-Math.PI/2,len=cnt[i]/mxC*R,half=(Math.PI/NS)*0.72,c=rampBYR(Math.min(1,spd[i]/50));
    ctx.fillStyle='rgba('+c[0]+','+c[1]+','+c[2]+',.85)';ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,len,ang-half,ang+half);ctx.closePath();ctx.fill();}
  ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(cx,cy,16,0,2*Math.PI);ctx.fill();
  ctx.fillStyle='#16152e';ctx.font='800 13px Inter';ctx.textBaseline='middle';ctx.fillText((tot/Math.max(1,n)).toFixed(0),cx,cy-2);
  ctx.font='600 7.5px Inter';ctx.fillStyle='rgba(90,110,130,.85)';ctx.fillText('km/h Ø',cx,cy+9);ctx.textBaseline='alphabetic';}
function icRad(cv,p){const{ctx,w,h}=icSetup(cv,92);
  // One bar per day: percentage of that day's theoretical max radiation
  // actually reached at this location (sunshine fraction vs. clear sky).
  const days=[];let cur=null;
  for(let t=0;t<T;t++){const d=new Date(M.times[t]+'Z'),key=d.toISOString().slice(0,10);
    if(!cur||cur.key!==key){cur={key,sum:0,n:0,inWin:false,lbl:d.toLocaleDateString('de-CH',{weekday:'short'})};days.push(cur);}
    cur.sum+=sunv(t,p);cur.n++;if(t>=a&&t<b)cur.inWin=true;}
  const shown=days.filter(d2=>d2.inWin);
  const baseY=h-16,topY=20,n=shown.length,bw=(w-8)/Math.max(1,n);
  ctx.strokeStyle='rgba(22,21,46,.12)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(4,baseY+.5);ctx.lineTo(w-4,baseY+.5);ctx.stroke();
  shown.forEach((d2,i)=>{
    const pct=Math.round(100*Math.max(0,Math.min(1,d2.sum/(0.42*24))));
    const bh=Math.max(2,pct/100*(baseY-topY)),x=4+i*bw;
    ctx.fillStyle=d2.inWin?'#f5a623':'rgba(245,166,35,.32)';
    icRR(ctx,x+1.5,baseY-bh,Math.max(2,bw-3),bh,3);ctx.fill();
    ctx.font='700 9.5px Inter';ctx.textAlign='center';
    ctx.fillStyle=d2.inWin?'#b06a00':'rgba(90,110,130,.75)';
    ctx.fillText(pct+'%',x+bw/2,baseY-bh-4);
    ctx.fillStyle='rgba(90,110,130,.75)';ctx.font='600 9px Inter';
    ctx.fillText(d2.lbl,x+bw/2,h-4);});}
// --- Mini-map tools (post-location step): search a place + recenter on GPS ---
function miniMapTools(mapObj,wrapEl){if(!wrapEl||wrapEl.querySelector('.mm-tools'))return;
  const d=document.createElement('div');d.className='mm-tools';
  d.innerHTML='<button type="button" title="Ort suchen" aria-label="Ort suchen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.2" y2="16.2"/></svg></button>'
    +'<button type="button" title="Mein Standort" aria-label="Mein Standort"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 2v3.5M12 18.5V22M2 12h3.5M18.5 12H22"/></svg></button>';
  const[bs,bl]=d.querySelectorAll('button');
  bs.onclick=e=>{e.stopPropagation();const q=prompt('Ort suchen…');if(!q)return;
    fetch('https://api3.geo.admin.ch/rest/services/api/SearchServer?type=locations&searchText='+encodeURIComponent(q)+'&limit=1&sr=4326')
      .then(r=>r.json()).then(j=>{const a2=j.results&&j.results[0]&&j.results[0].attrs;
        if(a2){mapObj.setView([a2.lat||a2.y,a2.lon||a2.x],13.5);}else toast('Kein Ort gefunden.','err');})
      .catch(()=>toast('Suche fehlgeschlagen.','err'));};
  bl.onclick=e=>{e.stopPropagation();if(!navigator.geolocation){toast('Kein GPS verfügbar.','err');return;}
    navigator.geolocation.getCurrentPosition(pos=>{mapObj.setView([pos.coords.latitude,pos.coords.longitude],13.5);try{haptic(6);}catch(_){}},
      ()=>toast('Standort nicht verfügbar.','err'),{enableHighAccuracy:true,timeout:8000});};
  wrapEl.appendChild(d);}
// --- 3D Terrain Viewer (MapLibre GL) ---
let map3d=null,is3d=false,exag3d=1.5;
function make3dOverlay(mode){
  const oc=document.createElement('canvas');oc.width=W;oc.height=H;
  const ox=oc.getContext('2d'),oi=ox.createImageData(W,H),od=oi.data;
  for(let p=0;p<NP;p++){const o=p*4;let r=0,g=0,bl=0,al=0;
    if(mode==='snow'){const ca2=a*NP,cb2=b*NP;const v=cum[cb2+p]-cum[ca2+p];const c=snowCol(v);if(c){r=c[0];g=c[1];bl=c[2];al=200;}}
    else if(mode==='depth'){const cb2=b*NP;const v=cum[cb2+p];if(v>=1){const x=Math.min(1,v/300);r=220-x*180|0;g=235-x*95|0;bl=255-x*30|0;al=180;}}
    else if(mode==='temp'){let su=0,c=0;for(let t=a;t<b;t++){su+=tv(t,p);c++;}const v=su/Math.max(1,c);const tc2=tempCol(v);r=tc2[0];g=tc2[1];bl=tc2[2];al=170;}
    else if(mode==='wind'){let su=0,c=0;for(let t=a;t<b;t++){su+=wg_(t,p)*3.6;c++;}const v=su/Math.max(1,c);if(v>=1){const c2=rampBYR(v/70);r=c2[0];g=c2[1];bl=c2[2];al=160;}}
    else if(mode==='powder'){const pw=computePowder(p,a,b);if(pw.powdered){r=200;g=220;bl=255;al=pw.quality==='reduced'?140:180;}}
    od[o]=r;od[o+1]=g;od[o+2]=bl;od[o+3]=al;}
  ox.putImageData(oi,0,0);return oc.toDataURL();}
function update3dOverlay(){if(!map3d||!is3d)return;const sel=document.getElementById('overlay3d').value;
  if(sel==='none'){if(map3d.getLayer('data-overlay'))map3d.setLayoutProperty('data-overlay','visibility','none');return;}
  const url=make3dOverlay(sel);
  if(map3d.getSource('data-src')){map3d.getSource('data-src').updateImage({url:url,coordinates:[[loMin,laMax],[loMax,laMax],[loMax,laMin],[loMin,laMin]]});}
  else{map3d.addSource('data-src',{type:'image',url:url,coordinates:[[loMin,laMax],[loMax,laMax],[loMax,laMin],[loMin,laMin]]});
    map3d.addLayer({id:'data-overlay',type:'raster',source:'data-src',paint:{'raster-opacity':0.75,'raster-fade-duration':0}});}
  if(map3d.getLayer('data-overlay'))map3d.setLayoutProperty('data-overlay','visibility','visible');}
let _ml=null;
function ensureMaplibre(cb){
  if(window.maplibregl){cb();return;}
  if(_ml){_ml.push(cb);return;}
  _ml=[cb];
  const css=document.createElement('link');css.rel='stylesheet';css.href='https://unpkg.com/maplibre-gl@4.1.2/dist/maplibre-gl.css';document.head.appendChild(css);
  const sc=document.createElement('script');sc.src='https://unpkg.com/maplibre-gl@4.1.2/dist/maplibre-gl.js';
  sc.onload=()=>{const q=_ml;_ml=null;q.forEach(f=>{try{f();}catch(e){}});};
  sc.onerror=()=>{_ml=null;toast('3D-Karte konnte nicht geladen werden — Verbindung prüfen.','err');};
  document.head.appendChild(sc);}
function init3D(){
  if(!window.maplibregl){toast('3D wird geladen …','ok');ensureMaplibre(()=>init3D());return;}
  const wrap=document.getElementById('three-wrap');wrap.style.display='block';is3d=true;
  const lc=map.getCenter(),lz=map.getZoom();
  map3d=new maplibregl.Map({container:'map3d',
    style:{version:8,
      sources:{
        'swisstopo':{type:'raster',tiles:['https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe-winter/default/current/3857/{z}/{x}/{y}.jpeg'],tileSize:256,attribution:'© swisstopo'},
        'hillshade-tiles':{type:'raster',tiles:['https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissalti3d-reliefschattierung_monodirektional/default/current/3857/{z}/{x}/{y}.png'],tileSize:256},
        'terrain-dem':{type:'raster-dem',tiles:['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],tileSize:256,encoding:'terrarium',maxzoom:15}},
      layers:[
        {id:'swisstopo-base',type:'raster',source:'swisstopo',paint:{'raster-opacity':0.3}},
        {id:'hillshade',type:'raster',source:'hillshade-tiles',paint:{'raster-opacity':0.7}}],
      terrain:{source:'terrain-dem',exaggeration:exag3d}},
    center:[lc.lng,lc.lat],zoom:lz,pitch:60,bearing:-20,maxPitch:85,
    maxBounds:[[loMinW-0.1,laMinW-0.05],[loMaxW+0.1,laMaxW+0.05]],
    touchZoomRotate:true,touchPitch:true,dragRotate:true});
  map3d.addControl(new maplibregl.NavigationControl({visualizePitch:true,showCompass:true}),'top-left');
  map3d.addControl(new maplibregl.GeolocateControl({positionOptions:{enableHighAccuracy:true},trackUserLocation:false}),'top-left');
  map3d.scrollZoom.setWheelZoomRate(1/100);
  map3d.keyboard.enable();
  document.getElementById('btn3dFloat').classList.add('active');
  if(!isMobile)document.getElementById('keys3d').style.display='inline';
  map3d.on('load',()=>update3dOverlay());
  map3d.on('moveend',sync3dTo2d);
  // Desktop keyboard controls for 3D
  document.addEventListener('keydown',function(e){
    if(!is3d||!map3d)return;const s=0.002;
    if(e.key==='ArrowUp'||e.key==='w'){map3d.setPitch(Math.min(85,map3d.getPitch()+5));e.preventDefault();}
    else if(e.key==='ArrowDown'||e.key==='s'){map3d.setPitch(Math.max(0,map3d.getPitch()-5));e.preventDefault();}
    else if(e.key==='ArrowLeft'||e.key==='a'){map3d.setBearing(map3d.getBearing()-5);e.preventDefault();}
    else if(e.key==='ArrowRight'||e.key==='d'){map3d.setBearing(map3d.getBearing()+5);e.preventDefault();}
    else if(e.key==='+'||e.key==='='){map3d.zoomIn();e.preventDefault();}
    else if(e.key==='-'){map3d.zoomOut();e.preventDefault();}
    else if(e.key==='r'){map3d.easeTo({pitch:60,bearing:-20,duration:500});e.preventDefault();}
  });}
function close3D(){
  if(map3d){const c=map3d.getCenter(),z=map3d.getZoom();map.setView([c.lat,c.lng],z,{animate:false});}
  is3d=false;if(map3d){map3d.remove();map3d=null;}
  document.getElementById('three-wrap').style.display='none';
  document.getElementById('btn3dFloat').classList.remove('active');}
document.getElementById('btn3dFloat').onclick=e=>{e.stopPropagation();if(is3d)close3D();else init3D();};
document.getElementById('btn3dClose').onclick=()=>close3D();
document.getElementById('mapOpac3d').oninput=function(){if(!map3d)return;const v=this.value/100;
  map3d.setPaintProperty('swisstopo-base','raster-opacity',v);
  map3d.setPaintProperty('hillshade','raster-opacity',1-v);
  document.getElementById('mapOpacLbl').textContent=this.value+'%';};
document.getElementById('overlay3d').onchange=()=>update3dOverlay();
// --- 3D Exaggeration ---
const EXAG_VALS=[1.0,1.5,2.0,3.0];let exagIdx=1;
document.getElementById('btn3dExag').onclick=()=>{
  exagIdx=(exagIdx+1)%EXAG_VALS.length;exag3d=EXAG_VALS[exagIdx];
  document.getElementById('btn3dExag').textContent='×'+exag3d;
  if(map3d&&map3d.style)map3d.setTerrain({source:'terrain-dem',exaggeration:exag3d});};
// --- Location Search (GeoAdmin API) ---
(function(){const inp=document.getElementById('searchIn'),res=document.getElementById('searchRes');
  let debT=null,selIdx=-1,items=[];
  inp.addEventListener('input',()=>{clearTimeout(debT);const q=inp.value.trim();if(q.length<2){res.style.display='none';items=[];return;}
    debT=setTimeout(()=>{fetch('https://api3.geo.admin.ch/rest/services/api/SearchServer?type=locations&searchText='+encodeURIComponent(q)+'&limit=6&sr=4326')
      .then(r=>r.json()).then(d=>{items=d.results||[];selIdx=-1;if(!items.length){res.style.display='none';return;}
        res.innerHTML=items.map((it,i)=>{const a=it.attrs;return`<div class="sr" data-i="${i}"><div>${a.label.replace(/<[^>]+>/g,'')}</div><div class="sub">${a.detail||''}</div></div>`;}).join('');
        res.style.display='block';
        res.querySelectorAll('.sr').forEach(el=>el.onclick=()=>pickResult(parseInt(el.dataset.i)));
      }).catch(()=>{});},250);});
  inp.addEventListener('keydown',e=>{if(!items.length)return;
    if(e.key==='ArrowDown'){e.preventDefault();selIdx=Math.min(items.length-1,selIdx+1);hlSel();}
    else if(e.key==='ArrowUp'){e.preventDefault();selIdx=Math.max(0,selIdx-1);hlSel();}
    else if(e.key==='Enter'&&selIdx>=0){e.preventDefault();pickResult(selIdx);}
    else if(e.key==='Escape'){res.style.display='none';}});
  function hlSel(){res.querySelectorAll('.sr').forEach((el,i)=>el.classList.toggle('sel',i===selIdx));}
  function pickResult(i){const a=items[i].attrs;res.style.display='none';
    const lon=a.lon||a.x,lat=a.lat||a.y;
    inp.value=a.label.replace(/<[^>]+>/g,'');
    map.flyTo([lat,lon],14,{duration:1.8});
    if(map3d&&is3d)map3d.flyTo({center:[lon,lat],zoom:14,duration:2000});
    L.circle([lat,lon],{radius:80,color:'#00c8d6',weight:2,fillColor:'#00c8d6',fillOpacity:.25}).addTo(map).on('add',function(){const c=this;setTimeout(()=>map.removeLayer(c),4000);});}
  document.addEventListener('click',e=>{if(!document.getElementById('searchWrap').contains(e.target))res.style.display='none';});
})();
// --- 2D ↔ 3D Coordinate Sync ---
let syncLock=false;
function sync2dTo3d(){if(!map3d||!is3d||syncLock)return;syncLock=true;
  const c=map.getCenter(),z=map.getZoom();
  map3d.jumpTo({center:[c.lng,c.lat],zoom:z});syncLock=false;}
function sync3dTo2d(){if(!map3d||syncLock)return;syncLock=true;
  const c=map3d.getCenter(),z=map3d.getZoom();
  map.setView([c.lat,c.lng],z,{animate:false});syncLock=false;}
map.on('moveend',sync2dTo3d);
// --- Intro Animation ---
const _introStart=Date.now();
function dismissIntro(){const el=document.getElementById('intro');if(!el){maybeDisclaimer();return;}
  el.classList.add('hide');setTimeout(()=>{el.remove();maybeDisclaimer();},240);}
// --- Snow animation for intro ---
(function(){const w=document.getElementById('snowWrap');if(!w)return;
  for(let i=0;i<30;i++){const s=document.createElement('div');s.className='sf';
    s.style.left=Math.random()*100+'%';s.style.animationDuration=(2+Math.random()*4)+'s';
    s.style.animationDelay=Math.random()*3+'s';s.style.opacity=0.2+Math.random()*0.5;
    s.style.width=s.style.height=(2+Math.random()*4)+'px';w.appendChild(s);}})();
// --- First-run onboarding coach marks ---
const COACH_STEPS=[
  {sel:'#topics button[data-t="ski"]',html:'<b>Finde den besten Schnee.</b><br>Tippe auf <b>Ski</b> für Skitauglichkeit & Pulverqualität – deine wichtigsten Highlights auf einen Blick.'},
  {sel:'#topics button[data-t="snow"]',html:'<b>Schnee-Bedingungen.</b><br>Unter <b>Snow</b> siehst du Neuschnee & Schneehöhe. Über <b>More</b> kommen Temperatur, Wind & mehr dazu.'},
  {sel:'#searchWrap',html:'<b>Spring zu einem Ort.</b><br>Suche einen Berg oder Ort und zoome direkt dorthin.'},
  {sel:'#feedBtn',html:'<b>Community-Feed.</b><br>Hier siehst du aktuelle Meldungen von Tourengängern in der ganzen Schweiz.'}
];
let coachIdx=0;
function maybeOnboard(){try{if(localStorage.getItem('ssm_onboarded')){maybeA2HS();return;}}catch(e){}coachIdx=0;startCoach();}
// --- Add-to-Home-Screen sheet ---
function maybeA2HS(){
  if(isStandalone())return;
  if(!('ontouchstart'in window)&&window.innerWidth>900)return; // desktop
  try{const v=localStorage.getItem('ssm_a2hs');
    if(v==='done')return;
    if(v){const o=JSON.parse(v);if(o&&o.t&&(Date.now()-o.t)<7*864e5)return;}}catch(e){}
  setTimeout(a2hsShow,700);}
function a2hsShow(){
  const el=document.getElementById('a2hs');if(!el)return;
  const ios=isIOS();
  document.getElementById('a2hsIos').style.display=ios?'':'none';
  document.getElementById('a2hsAndroid').style.display=ios?'none':'';
  if(!ios){const has=!!bipEvt;
    document.getElementById('a2hsInstallBtn').style.display=has?'':'none';
    document.getElementById('a2hsFallback').style.display=has?'none':'';}
  el.classList.add('show');haptic(8);}
function a2hsLater(){const el=document.getElementById('a2hs');if(el)el.classList.remove('show');
  try{localStorage.setItem('ssm_a2hs',JSON.stringify({s:'later',t:Date.now()}));}catch(e){}}
async function a2hsInstall(){if(!bipEvt){a2hsLater();return;}
  try{bipEvt.prompt();const r=await bipEvt.userChoice;
    if(r&&r.outcome==='accepted'){try{localStorage.setItem('ssm_a2hs','done');}catch(e){}}
    document.getElementById('a2hs').classList.remove('show');bipEvt=null;
  }catch(e){a2hsLater();}}
function endCoach(){try{localStorage.setItem('ssm_onboarded','1');}catch(e){}const c=document.getElementById('coach');if(c)c.style.display='none';maybeA2HS();}
function showCoachStep(){
  const c=document.getElementById('coach');
  // Skip steps whose target is missing or hidden (e.g. non-Ski topics in Simple mode)
  const vis=s=>{const e=document.querySelector(s);return e&&e.getBoundingClientRect().width>0;};
  while(coachIdx<COACH_STEPS.length&&!vis(COACH_STEPS[coachIdx].sel))coachIdx++;
  if(coachIdx>=COACH_STEPS.length){endCoach();return;}
  const step=COACH_STEPS[coachIdx],el=document.querySelector(step.sel),r=el.getBoundingClientRect();
  const pad=8,spot=document.getElementById('coachSpot'),card=document.getElementById('coachCard');
  spot.style.left=(r.left-pad)+'px';spot.style.top=(r.top-pad)+'px';spot.style.width=(r.width+pad*2)+'px';spot.style.height=(r.height+pad*2)+'px';
  document.getElementById('coachText').innerHTML=step.html;
  document.getElementById('coachDots').innerHTML=COACH_STEPS.map((_,i)=>'<i class="'+(i===coachIdx?'on':'')+'"></i>').join('');
  document.getElementById('coachNext').textContent=coachIdx===COACH_STEPS.length-1?'Los geht’s':'Weiter';
  // Position card: below the target if room, else above
  const cw=Math.min(300,window.innerWidth-32),vh=window.innerHeight;
  let top=r.bottom+14,below=true;if(top+150>vh){top=r.top-14-160;below=false;}
  top=Math.max(12,Math.min(top,vh-180));
  let left=r.left+r.width/2-cw/2;left=Math.max(12,Math.min(left,window.innerWidth-cw-12));
  card.style.left=left+'px';card.style.top=top+'px';
  c.style.display='block';
}
function coachNext(){coachIdx++;if(coachIdx>=COACH_STEPS.length)endCoach();else showCoachStep();}
function startCoach(){const nx=document.getElementById('coachNext'),sk=document.getElementById('coachSkip');
  nx.onclick=coachNext;sk.onclick=endCoach;
  window.addEventListener('resize',()=>{if(document.getElementById('coach').style.display==='block')showCoachStep();});
  showCoachStep();}
// ESC closes the topmost overlay (desktop convenience)
document.addEventListener('keydown',function(e){
  if(e.key!=='Escape')return;
  const vis=id=>{const el=document.getElementById(id);return el&&el.style.display!=='none'&&el.style.display!=='';};
  if(vis('locPicker')){locPickerClose();return;}
  if(vis('cmtModal')){commentsClose();return;}
  var a2=document.getElementById('a2hs');if(a2&&a2.classList.contains('show')){a2hsLater();return;}
  if(vis('qrModal')){qrClose();return;}
  if(vis('userViewModal')){userViewClose();return;}
  if(vis('profModal')){profClose();return;}
  if(vis('reportOverlay')){obsClose();return;}
  if(document.getElementById('feedPage').classList.contains('open')){feedClose();return;}
  if(document.getElementById('inspPanel').classList.contains('open')){inspClose();return;}
  if(vis('authOverlay')&&authMode!=='biometric'){authHide();return;}
});
// Prevent the whole page (UI) from zooming — the map keeps its own zoom
document.addEventListener('gesturestart',e=>e.preventDefault());
document.addEventListener('gesturechange',e=>e.preventDefault());
(function(){let lt=0;document.addEventListener('touchend',e=>{const n=Date.now();if(n-lt<=300&&!e.target.closest('#map,#map3d')){e.preventDefault();}lt=n;},{passive:false});})();
try{map.setView([46.7970,9.8270],11.8,{animate:false});}catch(e){} // Davos (Platz/Dorf), Karte fuellt den Screen
window.__APP_OK=true;
setTopic('snow',0);dismissIntro();
try{const _pid=new URLSearchParams(location.search).get('post');
  if(_pid)setTimeout(()=>{try{feedOpenAt(_pid);}catch(e){}},1400);}catch(e){}
if('serviceWorker'in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('sw.js').then(reg=>{try{reg.update();}catch(e){}}).catch(()=>{});});}
requestAnimationFrame(()=>{document.documentElement.style.setProperty('--btm-h',document.getElementById('bottomPanel').offsetHeight+'px');});
// --- Supabase Auth & Reports ---
const SB_URL='https://gdtxwowcqtbdkcoksivb.supabase.co';
const SB_KEY='eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdkdHh3b3djcXRiZGtjb2tzaXZiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODE3NzA1ODUsImV4cCI6MjA5NzM0NjU4NX0.5t4UHGLnnWbDoPVZE0LnmN1bS_jvEU3mmk4-1JpvXmM';
let sb=null,sbUser=null,authMode='login';
try{sb=window.supabase.createClient(SB_URL,SB_KEY);}catch(e){console.warn('Supabase SDK not loaded',e);}
function haptic(ms){try{const H=_capPlugin('Haptics');if(H){H.impact({style:(ms||8)>=15?'MEDIUM':'LIGHT'});return;}navigator.vibrate(ms||8);}catch(e){}}
// --- Custom inline icon set (replaces emoji everywhere in the UI chrome) ---
const IC={
 pin:'<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
 peak:'<path d="M3 20L9 8l3.5 6L15 10l6 10z"/><path d="M9 8l1.5-3 2 3.5"/>',
 ski:'<circle cx="15.5" cy="4.5" r="1.6"/><path d="M5 20l5-11 4 6 2.5-4L21 17"/><path d="M3 21h18"/>',
 bolt:'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
 flake:'<line x1="12" y1="3" x2="12" y2="21"/><line x1="4.2" y1="7.5" x2="19.8" y2="16.5"/><line x1="4.2" y1="16.5" x2="19.8" y2="7.5"/><path d="M12 3l-2 2M12 3l2 2M12 21l-2-2M12 21l2-2"/>',
 star:'<path d="M12 2l3 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.9 21l1.2-6.8-5-4.9 6.9-1z" fill="currentColor" stroke="none"/>',
 cam:'<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>'
};
function ic(n){return '<svg class="ic-i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(IC[n]||'')+'</svg>';}
// --- Add-to-Home-Screen: detection + native-install capture ---
function isStandalone(){try{return window.matchMedia('(display-mode: standalone)').matches||window.navigator.standalone===true;}catch(e){return false;}}
function isIOS(){const ua=navigator.userAgent;return /iPhone|iPad|iPod/.test(ua)||(/Macintosh/.test(ua)&&navigator.maxTouchPoints>1);}
let bipEvt=null;
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();bipEvt=e;});
window.addEventListener('appinstalled',()=>{try{localStorage.setItem('ssm_a2hs','done');}catch(e){}
  const el=document.getElementById('a2hs');if(el)el.classList.remove('show');toast('App installiert!','ok');});
window.addEventListener('DOMContentLoaded',()=>{if(isStandalone())document.body.classList.add('standalone');});
// --- Toast notifications (surface errors + confirmations; no longer silent) ---
function toast(msg,kind){const w=document.getElementById('toastWrap');
  if(!w){try{console.log('toast:',kind||'',msg);}catch(e){}return;}
  const t=document.createElement('div');t.className='toast'+(kind?(' '+kind):'');
  const ic=kind==='err'?'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12.5"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>':kind==='ok'?'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>':'';
  t.innerHTML=ic+'<span></span>';t.querySelector('span').textContent=msg;w.appendChild(t);
  if(kind==='err'){try{haptic(18);}catch(e){}}
  setTimeout(()=>{t.classList.add('out');setTimeout(()=>{t.remove();},260);},kind==='err'?4200:2600);}
// --- First-run legal / safety disclaimer gate (must accept before onboarding) ---
const DISC_VER='1';
function maybeDisclaimer(){
  try{if(localStorage.getItem('ssm_disclaimer_v'+DISC_VER)){maybeOnboard();return;}}catch(e){}
  const el=document.getElementById('disc');if(!el){maybeOnboard();return;}el.classList.add('show');}
function acceptDisc(){
  try{localStorage.setItem('ssm_disclaimer_v'+DISC_VER,'1');}catch(e){}
  const el=document.getElementById('disc');if(el)el.classList.remove('show');
  try{haptic(12);}catch(e){}maybeOnboard();}
// --- Client-side image downscale before upload (protect the 1GB free tier) ---
async function downscaleImage(file,maxPx,q){
  maxPx=maxPx||1600;q=q||0.85;
  if(!file||!/^image\//.test(file.type||''))return file;
  try{
    const bmp=await createImageBitmap(file,{imageOrientation:'from-image'});
    const w=bmp.width,h=bmp.height,long=Math.max(w,h);
    if(long<=maxPx&&(file.size||0)<600*1024){if(bmp.close)bmp.close();return file;}
    const scale=Math.min(1,maxPx/long),cw=Math.round(w*scale),ch=Math.round(h*scale);
    const cv=document.createElement('canvas');cv.width=cw;cv.height=ch;
    cv.getContext('2d').drawImage(bmp,0,0,cw,ch);if(bmp.close)bmp.close();
    const blob=await new Promise(res=>cv.toBlob(res,'image/jpeg',q));
    return(blob&&blob.size<(file.size||Infinity))?blob:file;
  }catch(e){console.warn('downscale',e);return file;}
}
// --- Category icons / colors (defined early: used by markers on first paint) ---
const CAT_SVG={
  snow:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="12" y1="2" x2="12" y2="22"/><line x1="4.9" y1="7" x2="19.1" y2="17"/><line x1="4.9" y1="17" x2="19.1" y2="7"/><line x1="12" y1="2" x2="14" y2="4.5"/><line x1="12" y1="2" x2="10" y2="4.5"/><line x1="12" y1="22" x2="14" y2="19.5"/><line x1="12" y1="22" x2="10" y2="19.5"/><line x1="4.9" y1="7" x2="5.7" y2="9.8"/><line x1="4.9" y1="7" x2="7.5" y2="6"/><line x1="19.1" y1="17" x2="18.3" y2="14.2"/><line x1="19.1" y1="17" x2="16.5" y2="18"/><line x1="19.1" y1="7" x2="16.5" y2="6"/><line x1="19.1" y1="7" x2="18.3" y2="9.8"/><line x1="4.9" y1="17" x2="7.5" y2="18"/><line x1="4.9" y1="17" x2="5.7" y2="14.2"/></svg>',
  route:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19l4-14 4 10 4-6 4 10"/><circle cx="6" cy="5" r="1.5" fill="currentColor" stroke="none"/><circle cx="18" cy="9" r="1.5" fill="currentColor" stroke="none"/></svg>',
  danger:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.2L1.8 18.5c-.8 1.4.2 3 1.7 3h17c1.5 0 2.5-1.6 1.7-3L13.7 3.2c-.8-1.4-2.6-1.4-3.4 0z"/><line x1="12" y1="9" x2="12" y2="14"/><circle cx="12" cy="17" r="1" fill="currentColor" stroke="none"/></svg>',
  tour:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20l7-10 4 5 7-11"/><circle cx="17" cy="4" r="2"/><path d="M14 20l3-4 4 4"/></svg>',
  info:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><circle cx="12" cy="8" r=".5" fill="currentColor" stroke="none"/></svg>'
};
const CAT_COLORS={snow:'#5e5ce6',route:'#2e7d32',danger:'#d03050',tour:'#7b1fa2',info:'#e65100',avalanche:'#d03050',whumpf:'#e8590c',wind_slab:'#0d9488',other:'#5e5ce6'};
const CAT_BG={snow:'linear-gradient(135deg,#e3f2fd,#bbdefb)',route:'linear-gradient(135deg,#e8f5e9,#c8e6c9)',danger:'linear-gradient(135deg,#fce4ec,#f8bbd0)',tour:'linear-gradient(135deg,#f3e5f5,#e1bee7)',info:'linear-gradient(135deg,#fff3e0,#ffe0b2)',avalanche:'linear-gradient(135deg,#fce4ec,#f8bbd0)',whumpf:'linear-gradient(135deg,#fff3e0,#ffe0b2)',wind_slab:'linear-gradient(135deg,#e0f2f1,#b2dfdb)',other:'linear-gradient(135deg,#e3f2fd,#bbdefb)'};
// new SLF report-type icons reuse existing glyphs
CAT_SVG.avalanche=CAT_SVG.danger;CAT_SVG.whumpf=CAT_SVG.danger;CAT_SVG.wind_slab=CAT_SVG.snow;CAT_SVG.other=CAT_SVG.info;
const CAT_LABEL={snow:'Schnee',route:'Menschen',danger:'Gefahr',info:'Info',tour:'Tour',avalanche:'Lawine',whumpf:'Wumm',wind_slab:'Triebschnee',other:'Beobachtung'};
function catLabel(id){return CAT_LABEL[id]||id;}
function catSvg(id,size){return `<span class="cat-ico" style="width:${size||16}px;height:${size||16}px">${CAT_SVG[id]||''}</span>`;}
// --- Swiss peaks & famous ski destinations (for peak detection + feed filters) ---
const PEAKS=[
  {n:'Matterhorn',lat:45.9763,lng:7.6586,e:4478},{n:'Dufourspitze',lat:45.9369,lng:7.8669,e:4634},
  {n:'Dom',lat:46.0940,lng:7.8580,e:4545},{n:'Weisshorn',lat:46.1017,lng:7.7167,e:4506},
  {n:'Jungfrau',lat:46.5367,lng:7.9625,e:4158},{n:'Mönch',lat:46.5583,lng:7.9975,e:4107},
  {n:'Eiger',lat:46.5775,lng:8.0053,e:3967},{n:'Piz Bernina',lat:46.3828,lng:9.9083,e:4049},
  {n:'Finsteraarhorn',lat:46.5372,lng:8.1263,e:4274},{n:'Aletschhorn',lat:46.4650,lng:8.0000,e:4194},
  {n:'Titlis',lat:46.7722,lng:8.4364,e:3238},{n:'Säntis',lat:47.2494,lng:9.3431,e:2502},
  {n:'Piz Palü',lat:46.3800,lng:9.9670,e:3900},{n:'Tödi',lat:46.8110,lng:8.9170,e:3614},
  {n:'Piz Buin',lat:46.8419,lng:10.1197,e:3312},{n:'Piz Kesch',lat:46.6180,lng:9.8720,e:3418},
  {n:'Wildhorn',lat:46.3560,lng:7.3720,e:3247},{n:'Wildstrubel',lat:46.3910,lng:7.5250,e:3243},
  {n:'Bishorn',lat:46.1290,lng:7.7000,e:4153},{n:'Grand Combin',lat:45.9370,lng:7.2990,e:4314},
  {n:"Pigne d'Arolla",lat:46.0100,lng:7.4400,e:3790},{n:'Piz Corvatsch',lat:46.4110,lng:9.8200,e:3451},
  {n:'Pizol',lat:46.9600,lng:9.4000,e:2844},{n:'Grosser Mythen',lat:47.0350,lng:8.6900,e:1898},
  {n:'Rigi',lat:47.0570,lng:8.4850,e:1798},{n:'Piz Nair',lat:46.4890,lng:9.8100,e:3057}
];
const DESTS=[
  {n:'Davos',lat:46.7998,lng:9.8340},{n:'Lenzerheide',lat:46.7290,lng:9.5580},{n:'Arosa',lat:46.7830,lng:9.6790},
  {n:'St. Moritz',lat:46.4980,lng:9.8380},{n:'Zermatt',lat:46.0207,lng:7.7491},{n:'Verbier',lat:46.0960,lng:7.2280},
  {n:'Laax / Flims',lat:46.8030,lng:9.2580},{n:'Engelberg',lat:46.8210,lng:8.4010},{n:'Grindelwald',lat:46.6240,lng:8.0340},
  {n:'Saas-Fee',lat:46.1090,lng:7.9290},{n:'Andermatt',lat:46.6350,lng:8.5940},{n:'Crans-Montana',lat:46.3080,lng:7.4780},
  {n:'Adelboden',lat:46.4920,lng:7.5610},{n:'Gstaad',lat:46.4720,lng:7.2860},{n:'Wengen',lat:46.6050,lng:7.9220},
  {n:'Villars',lat:46.2980,lng:7.0560},{n:'Nendaz',lat:46.1830,lng:7.3060},{n:'Scuol',lat:46.7970,lng:10.2990},
  {n:'Grimentz / Zinal',lat:46.1350,lng:7.6220},{n:'Leukerbad',lat:46.3810,lng:7.6270},{n:'Champéry',lat:46.1770,lng:6.8690},
  {n:'Klosters',lat:46.8690,lng:9.8790},{n:'Meiringen',lat:46.7290,lng:8.2050},{n:'Flumserberg',lat:47.0900,lng:9.2830},
  {n:'Sörenberg',lat:46.8210,lng:8.0360},{n:'Braunwald',lat:46.9410,lng:8.9970}
];
function haversineKm(la1,lo1,la2,lo2){const R=6371,dLa=(la2-la1)*Math.PI/180,dLo=(lo2-lo1)*Math.PI/180;
  const s=Math.sin(dLa/2)**2+Math.cos(la1*Math.PI/180)*Math.cos(la2*Math.PI/180)*Math.sin(dLo/2)**2;
  return 2*R*Math.asin(Math.min(1,Math.sqrt(s)));}
function nearestOf(list,lat,lng){let best=null,bd=1e9;for(const p of list){const d=haversineKm(lat,lng,p.lat,p.lng);if(d<bd){bd=d;best=p;}}return best?{item:best,km:bd}:null;}
// Parse a Postgres/PostGIS location into [lat,lng]. Handles WKT text, GeoJSON,
// and (importantly) the EWKB hex that geography columns return by default —
// without this reports were read as (0,0), far from Switzerland.
function parseGeo(g){
  if(g==null)return null;
  if(typeof g==='object'){if(g.coordinates&&g.coordinates.length>=2)return[+g.coordinates[1],+g.coordinates[0]];return null;}
  const s=String(g);
  const m=s.match(/POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)/i);
  if(m)return[parseFloat(m[2]),parseFloat(m[1])];
  if(/^[0-9A-Fa-f]+$/.test(s)&&s.length>=42){try{
    const buf=new Uint8Array(s.length/2);for(let i=0;i<buf.length;i++)buf[i]=parseInt(s.substr(i*2,2),16);
    const dv=new DataView(buf.buffer),le=buf[0]===1,type=dv.getUint32(1,le);let off=5;
    if(type&0x20000000)off+=4;   // SRID present
    if(type&0x40000000)off+=4;   // measured
    const x=dv.getFloat64(off,le),y=dv.getFloat64(off+8,le);
    if(isFinite(x)&&isFinite(y)&&Math.abs(y)<=90&&Math.abs(x)<=180)return[y,x];
  }catch(e){}}
  return null;
}
// --- Demo data ---
function demoImg(h1,h2,sky){const svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500">'+
 '<defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="hsl('+sky+',70%,72%)"/><stop offset="1" stop-color="hsl('+sky+',45%,90%)"/></linearGradient></defs>'+
 '<rect width="800" height="500" fill="url(#s)"/>'+
 '<circle cx="660" cy="90" r="46" fill="rgba(255,250,235,.9)"/>'+
 '<path d="M0 500 L140 220 L260 360 L400 150 L540 340 L660 230 L800 420 L800 500 Z" fill="hsl('+h1+',22%,72%)"/>'+
 '<path d="M0 500 L120 330 L300 430 L470 280 L640 430 L800 350 L800 500 Z" fill="hsl('+h2+',18%,88%)"/>'+
 '<path d="M370 190 L400 150 L430 195 Z" fill="#fff" opacity=".9"/></svg>';
 return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);}
const DEMO_USERS=['AlpinMax','BerginaZH','TourenfanBE','SchneeLeo','LawinenPro','GipfelStuermer','PowderPia','FreerideFabio','SkitourSina','HochtourHans'];
const DEMO_DEFS=[
 ['snow','Neuschnee','35 cm','Frischer Powder am Titlis! Traumbedingungen seit heute Morgen.',46.771,8.427],
 ['tour','Powder','Tiefer, guter Powder · 40 cm','Mega Abfahrt vom Wildstrubel, unverspurt bis ins Tal!',46.420,7.505],
 ['snow','Firn','Firn ab 10:30','Perfekter Frühlingsfirn am Südhang, Abfahrt wie auf Samt.',46.559,7.964],
 ['danger','Wumm-Geräusche','3 erheblich','Deutliche Setzungsgeräusche oberhalb 2400 m, Triebschnee in Mulden.',46.834,8.389],
 ['snow','Triebschnee','20–40 cm','Frische Triebschneepakete hinter dem Grat, Vorsicht in Rinnen.',46.525,8.490],
 ['tour','Powder','Wenig, aber pulvrig · 15 cm','Oberhalb 2600 m noch kalter Pulver auf Nordseiten.',46.976,8.661],
 ['snow','Nassschnee','Ski sinkt ein','Ab Mittag tiefer Sulz unterhalb 2000 m, früh starten!',46.690,9.820],
 ['danger','Lawinenabgang','Gr. 3 spontan','Schneebrett am Nordhang Piz Terri, spontan um 11 Uhr.',46.612,8.999],
 ['snow','Neuschnee','22 cm','Überraschend viel Neuschnee im Goms, leicht & trocken.',46.475,8.278],
 ['tour','Bruchharsch','Viel, aber kompakt','Deckel trägt nicht überall — anstrengende Abfahrt.',46.365,7.850],
 ['snow','Windgepresst','Deckel trägt','Windharsch auf allen Westhängen oberhalb 2500 m.',46.500,9.430],
 ['tour','Powder','Tiefer, guter Powder · 50 cm','Waldabfahrten in Andermatt sind ein Traum heute!',46.635,8.594],
 ['snow','Firn','komplett Firn','Klassischer Aprilfirn an der Wildspitz-Südflanke.',47.085,8.555],
 ['danger','Warnzeichen','Risse sichtbar','Frische Risse beim Betreten der Wechte am Gemsstock.',46.602,8.612],
 ['snow','Neuschnee','18 cm','Feiner Pulver im Diemtigtal, Aufstieg gespurt.',46.552,7.564],
 ['tour','Powder','Wenig, aber pulvrig','Nordrinnen am Piz Beverin halten noch kalten Schnee.',46.663,9.362],
 ['snow','Nassschnee','nur Oberfläche','Oberflächlich feucht ab 11 Uhr, darunter kompakt.',46.020,7.749],
 ['tour','Firn','halb aufgefirnt','Sonnenaufgangstour aufs Sustenhorn — Firn ab 9 Uhr perfekt.',46.735,8.445],
 ['snow','Triebschnee','30 cm Auflage','Bise hat ordentlich verfrachtet, Leeseiten meiden.',47.020,9.280],
 ['danger','Blankeis','ab 2800 m','Kammlagen abgeblasen und eisig, Harscheisen Pflicht.',46.083,7.230]];
const DEMO_REPORTS=DEMO_DEFS.map((d,i2)=>({id:'d'+(i2+1),user:DEMO_USERS[i2%DEMO_USERS.length],cat:d[0],icon:'',
  sub:d[1],measurement:d[2],caption:d[3],lat:d[4],lng:d[5],
  time:i2<3?('vor '+(2+i2)+'h'):(i2<12?('vor '+(i2-1)+'h'):('vor '+(i2-10)+'d')),
  img:(i2%4===3)?null:demoImg(200+i2*9,210+i2*7,205+(i2%5)*6),stars:(i2%5)+1,likes:(i2*3)%17,comments:i2%4}));

let reportMarkers=L.layerGroup().addTo(map);
function demoActive(){try{if(location.search.indexOf('demo')>=0)return true;}catch(e){}return !sb;}
let allReports=demoActive()?[...DEMO_REPORTS]:[];
function loadReportMarkers(){
  reportMarkers.clearLayers();
  const tier=(typeof detailTier==='function')?detailTier():2;
  allReports.forEach(r=>{
    const mColor='#16152e'; // Post-Marker monochrom (B/W-Design)
    let icon;
    if(tier===0){
      icon=L.divIcon({className:'',html:`<div class="rpt-dot" style="background:${mColor}"></div>`,iconSize:[14,14],iconAnchor:[7,7]});
    }else{
      icon=L.divIcon({className:'',html:`<div class="rpt-marker" style="color:${mColor}">${CAT_SVG[r.cat]||r.icon||ic('pin')}</div>`,iconSize:[36,36],iconAnchor:[18,18]});
    }
    const m=L.marker([r.lat,r.lng],{icon,zIndexOffset:700}).addTo(reportMarkers);
    const rid=r.id;
    m.bindPopup(`<div style="min-width:180px">${r.img?`<img src="${r.img}" loading="lazy" style="width:100%;height:96px;object-fit:cover;border-radius:10px;margin-bottom:7px" onclick="feedOpenAt('${rid}')"/>`:''}<b>${escapeHtml(r.user)}</b> <span style="color:#7a8a9a;font-size:12px">${escapeHtml(r.time)}</span><br><span style="font-size:13px;display:inline-flex;align-items:center;gap:5px;color:${mColor}">${catSvg?catSvg(r.cat,14):''} ${escapeHtml(r.sub||r.cat)}${r.measurement?' · '+escapeHtml(r.measurement):''}</span>${r.caption?'<br><span style="font-size:13px;color:#3a4a5a">'+escapeHtml(r.caption)+'</span>':''}<br><a href="javascript:void(0)" onclick="feedOpenAt('${rid}')" style="font-size:12px;font-weight:700;color:#5e5ce6">Im Feed öffnen →</a></div>`,{maxWidth:260});
    m.on('click',function(){if(rptLastOpen===rid){feedOpenAt(rid);}else{rptLastOpen=rid;}});
    m.on('popupclose',function(){if(rptLastOpen===rid)rptLastOpen=null;});
  });
}
let rptLastOpen=null;
function feedOpenAt(id){feedScope='all';feedFilter='all';feedAnchor=null;
  if(document.getElementById('inspPanel'))document.getElementById('inspPanel').classList.remove('open');
  feedOpen();
  setTimeout(()=>{const el=document.getElementById('feedcard-'+id);if(el){el.scrollIntoView({behavior:'smooth',block:'center'});el.classList.add('flash');setTimeout(()=>el.classList.remove('flash'),1800);}},160);}
// --- Auth UI ---
function authUpdateUI(user){
  sbUser=user;
  const btn=document.getElementById('btnLogin');
  const feedB=document.getElementById('feedBtn'),banner=document.getElementById('emailBanner');
  if(user){
    const name=user.user_metadata?.username||user.email?.split('@')[0]||'User';
    btn.outerHTML=`<div class="user-pill" id="userPill" onclick="openProfile()">
      <div class="user-avatar" id="userPillAv"><span>${name[0].toUpperCase()}</span></div>
      <span class="user-name">${name}</span></div>`;
    loadMyProfileAvatar();
    updateAccountBtn(null);
    banner.style.display=user.email_confirmed_at?'none':'flex';
    // biometric lock on a restored session (not right after an explicit login)
    if(!justLoggedIn&&!bioUnlocked&&bioEnabledFor(user.id))showBioLock();
    justLoggedIn=false;
  } else {
    const pill=document.getElementById('userPill');
    if(pill)pill.outerHTML='<button class="login-btn" id="btnLogin" onclick="authShow()">Anmelden</button>';
    updateAccountBtn(null);
    banner.style.display='none';
  }
  feedB.style.display='flex';
  loadSocial().then(()=>{loadReportMarkers();loadDbReports();});
}
// --- Social state ---
let myFollowing=new Set();
async function loadSocial(){
  if(!sb)return;
  try{
    if(sbUser){
      const{data:f}=await sb.from('follows').select('following_id').eq('follower_id',sbUser.id);
      myFollowing=new Set((f||[]).map(x=>x.following_id));
    }else{myFollowing=new Set();}
  }catch(e){console.warn('loadSocial',e);}
}
async function loadDbReports(){
  if(!sb)return;
  try{
    const{data}=await sb.from('reports').select('*').order('created_at',{ascending:false}).limit(60);
    if(!data||!data.length){return;}
    try{const nw=new Date(data[0].created_at).getTime();const seen=+(localStorage.getItem('ssm_feed_seen')||0);
      const fd=document.querySelector('#feedBtn .feed-dot');if(fd)fd.classList.toggle('on',nw>seen);}catch(e){}
    try{dbQpr=data.filter(r=>r.condition_data&&r.condition_data.quick).map(r=>{const ll=parseGeo(r.location);const cd=r.condition_data;
        return ll?[ll[0],ll[1],+cd.powderAmountCm||0,+cd.powderQuality||0]:null;}).filter(Boolean);
      if(layer==='qprheat')renderQprHeat();}catch(e){}
    const ids=data.map(r=>r.id),uids=[...new Set(data.map(r=>r.user_id).filter(Boolean))];
    // usernames
    let nameMap={},avatarMap={};try{const{data:pr}=await sb.from('profiles').select('id,username,avatar_url').in('id',uids);(pr||[]).forEach(p=>{nameMap[p.id]=p.username;if(p.avatar_url)avatarMap[p.id]=p.avatar_url;});}catch(e){}
    // likes (reuse report_reactions type=like)
    let likeCount={},likedByMe={};try{const{data:rx}=await sb.from('report_reactions').select('report_id,user_id').eq('type','like').in('report_id',ids);
      (rx||[]).forEach(x=>{likeCount[x.report_id]=(likeCount[x.report_id]||0)+1;if(sbUser&&x.user_id===sbUser.id)likedByMe[x.report_id]=true;});}catch(e){}
    // comment counts
    let cmtCount={};try{const{data:cc}=await sb.from('report_comments').select('report_id').in('report_id',ids);
      (cc||[]).forEach(x=>{cmtCount[x.report_id]=(cmtCount[x.report_id]||0)+1;});}catch(e){}
    // condition ratings (crowdsourced snow quality: stars 1–5 + powder)
    let condAgg={},myCond={};try{const{data:cnd}=await sb.from('report_conditions').select('report_id,user_id,stars,powder').in('report_id',ids);
      (cnd||[]).forEach(x=>{const g=condAgg[x.report_id]||(condAgg[x.report_id]={n:0,sum:0,pow:0});g.n++;g.sum+=x.stars||0;if(x.powder)g.pow++;if(sbUser&&x.user_id===sbUser.id)myCond[x.report_id]={stars:x.stars,powder:!!x.powder};});}catch(e){}
    const dbR=data.map(r=>{
      if(r.flagged)return null; // hide community-flagged reports
      const ll=parseGeo(r.location);if(!ll||(ll[0]===0&&ll[1]===0))return null;const lat=ll[0],lng=ll[1];
      const catId=r.primary_categories?.[0]||'info';const catObj=RP_CATS.find(c=>c.id===catId);
      return{id:r.id,user:nameMap[r.user_id]||(r.user_id?.substring(0,8))||'User',userId:r.user_id,avatar:avatarMap[r.user_id]||null,cat:catId,icon:catObj?.icon||ic('pin'),sub:r.subtype,measurement:r.condition_data?.measurement||null,stars:r.condition_data?.stars||0,peak:r.condition_data?.peak||null,dest:r.condition_data?.dest||null,caption:r.caption,lat,lng,time:timeAgo(r.created_at),img:r.image_url,likes:likeCount[r.id]||0,liked:!!likedByMe[r.id],comments:cmtCount[r.id]||0,
        condN:condAgg[r.id]?condAgg[r.id].n:0,condAvg:condAgg[r.id]?condAgg[r.id].sum/condAgg[r.id].n:0,condPow:condAgg[r.id]?condAgg[r.id].pow:0,myCond:myCond[r.id]||null,dbRow:true};
    }).filter(Boolean);
    allReports=demoActive()?[...dbR,...DEMO_REPORTS]:dbR;loadReportMarkers();
    if(document.getElementById('feedPage').classList.contains('open'))feedRender();
  }catch(e){console.warn('loadDbReports',e);}
}
// --- Likes ---
async function toggleEndorse(id,ev){if(ev){ev.stopPropagation();}
  if(!sb||!sbUser){authShow();return;}
  const r=allReports.find(x=>x.id===id);if(!r||!r.dbRow)return;
  if(r.userId&&r.userId===sbUser.id){return;} // can't endorse your own
  const willEndorse=!r.liked;r.liked=willEndorse;r.likes=(r.likes||0)+(willEndorse?1:-1);feedRender();
  try{
    if(willEndorse)await sb.from('report_reactions').insert({report_id:id,user_id:sbUser.id,type:'like'});
    else await sb.from('report_reactions').delete().match({report_id:id,user_id:sbUser.id,type:'like'});
  }catch(e){r.liked=!willEndorse;r.likes+=(willEndorse?-1:1);feedRender();}
}
// --- Report moderation: flag/melden ---
async function reportFlag(id,ev){if(ev){ev.stopPropagation();}
  if(!sb||!sbUser){authShow();return;}
  const r=allReports.find(x=>x.id===id);if(!r||!r.dbRow)return;
  if(r.userId&&r.userId===sbUser.id){toast('Du kannst deinen eigenen Report nicht melden.');return;}
  if(r.flaggedByMe){toast('Bereits gemeldet.');return;}
  if(!confirm('Diesen Report als unangemessen melden?'))return;
  r.flaggedByMe=true;feedRender();
  try{
    const{error}=await sb.from('report_flags').insert({report_id:id,user_id:sbUser.id,reason:null});
    if(error)throw error;
    toast('Danke, der Report wurde gemeldet.','ok');haptic(10);
  }catch(e){r.flaggedByMe=false;feedRender();toast('Melden fehlgeschlagen: '+(e.message||e),'err');}
}
// --- Follow ---
async function toggleFollow(uid,ev){if(ev){ev.stopPropagation();}
  if(!sb||!sbUser){authShow();return;}
  if(!uid||uid===sbUser.id)return;
  const following=myFollowing.has(uid);
  if(following)myFollowing.delete(uid);else myFollowing.add(uid);feedRender();
  try{
    if(!following)await sb.from('follows').insert({follower_id:sbUser.id,following_id:uid});
    else await sb.from('follows').delete().match({follower_id:sbUser.id,following_id:uid});
  }catch(e){if(!following)myFollowing.delete(uid);else myFollowing.add(uid);feedRender();}
}
function timeAgo(ts){const d=Date.now()-new Date(ts).getTime(),h=d/36e5;if(h<1)return'gerade eben';if(h<24)return'vor '+Math.floor(h)+'h';return'vor '+Math.floor(h/24)+'d';}
if(sb){sb.auth.onAuthStateChange((ev,session)=>{authUpdateUI(session?.user||null);});
  sb.auth.getSession().then(({data})=>{authUpdateUI(data.session?.user||null);});}
else{document.getElementById('feedBtn').style.display='flex';loadReportMarkers();}
let authPendingEmail=null,justLoggedIn=false;
function authShow(){authMode='login';authRender();document.getElementById('authOverlay').style.display='flex';}
function authHide(){document.getElementById('authOverlay').style.display='none';document.getElementById('authErr').textContent='';document.getElementById('authCodeErr').textContent='';}
function authToggle(){authMode=authMode==='login'?'register':'login';authRender();}
function authBackToForm(){authMode='login';authRender();}
function authRender(){
  const isReg=authMode==='register',isCode=authMode==='code',isBio=authMode==='biometric';
  document.getElementById('authForm').style.display=(isCode||isBio)?'none':'flex';
  document.getElementById('authCodeBox').style.display=isCode?'block':'none';
  document.getElementById('authBioBox').style.display=isBio?'block':'none';
  document.getElementById('authSwitch').style.display=(isCode||isBio)?'none':'block';
  document.getElementById('authClose').style.display=isBio?'none':'flex';
  if(isCode){document.getElementById('authTitle').textContent='E-Mail bestätigen';document.getElementById('authSub').style.display='none';
    document.getElementById('authCodeSub').textContent='Wir haben einen 6-stelligen Code an '+(authPendingEmail||'deine E-Mail')+' geschickt.';return;}
  if(isBio){document.getElementById('authTitle').textContent='Biometrischer Login';document.getElementById('authSub').style.display='none';
    document.getElementById('authBioName').textContent=bioName();return;}
  document.getElementById('authSub').style.display='';
  document.getElementById('authTitle').textContent=isReg?'Account erstellen':'Anmelden';
  document.getElementById('authSub').textContent=isReg?'Erstelle ein Konto für die Community':'Anmelden für Community & Meldungen';
  document.getElementById('authUser').style.display=isReg?'':'none';
  document.getElementById('authSubmitBtn').textContent=isReg?'Registrieren':'Anmelden';
  document.getElementById('authSwitch').innerHTML=isReg?'Schon registriert? <button onclick="authToggle()">Anmelden</button>':'Kein Account? <button onclick="authToggle()">Registrieren</button>';
}
async function authSubmit(e){
  e.preventDefault();if(!sb)return;
  const email=document.getElementById('authEmail').value.trim();
  const pass=document.getElementById('authPass').value;
  const errEl=document.getElementById('authErr');errEl.textContent='';errEl.style.color='#FF5470';
  try{
    if(authMode==='register'){
      const username=document.getElementById('authUser').value.trim();
      if(!username){errEl.textContent='Benutzername erforderlich';return;}
      const{error}=await sb.auth.signUp({email,password:pass,options:{data:{username}}});
      if(error)throw error;
      authPendingEmail=email;authMode='code';authRender();setTimeout(()=>document.getElementById('authCode').focus(),100);return;
    }
    const{error}=await sb.auth.signInWithPassword({email,password:pass});
    if(error){
      if(/confirm|verif/i.test(error.message||'')){authPendingEmail=email;try{await sb.auth.resend({type:'signup',email});}catch(x){}authMode='code';authRender();return;}
      throw error;
    }
    authAfterLogin();
  }catch(err){errEl.textContent=err.message||'Fehler';}
}
function authCodeInput(){const v=document.getElementById('authCode').value.replace(/\D/g,'').slice(0,6);document.getElementById('authCode').value=v;if(v.length===6)authVerifyCode();}
async function authPasteCode(){
  try{const t=await navigator.clipboard.readText();const m=(t||'').match(/\d{6}/);
    if(m){document.getElementById('authCode').value=m[0];authVerifyCode();}
    else{document.getElementById('authCodeErr').style.color='#FF5470';document.getElementById('authCodeErr').textContent='Kein 6-stelliger Code in der Zwischenablage.';}
  }catch(e){document.getElementById('authCodeErr').style.color='#FF5470';document.getElementById('authCodeErr').textContent='Zwischenablage nicht verfügbar – Code manuell eingeben.';}
}
async function authVerifyCode(){
  if(!sb||!authPendingEmail)return;
  const token=document.getElementById('authCode').value.trim();const err=document.getElementById('authCodeErr');err.textContent='';
  if(token.length<6){err.style.color='#FF5470';err.textContent='Bitte 6 Ziffern eingeben.';return;}
  const btn=document.getElementById('authCodeBtn');btn.disabled=true;btn.textContent='Prüfe…';
  try{
    let r=await sb.auth.verifyOtp({email:authPendingEmail,token,type:'signup'});
    if(r.error){const r2=await sb.auth.verifyOtp({email:authPendingEmail,token,type:'email'});if(r2.error)throw r2.error;}
    authAfterLogin();
  }catch(e){err.style.color='#FF5470';err.textContent=(e.message||'Code ungültig oder abgelaufen.');}
  btn.disabled=false;btn.textContent='Bestätigen';
}
async function authResendCode(){if(!sb||!authPendingEmail)return;try{await sb.auth.resend({type:'signup',email:authPendingEmail});const err=document.getElementById('authCodeErr');err.style.color='#0a8f4f';err.textContent='Neuer Code gesendet.';}catch(e){}}
async function authAfterLogin(){
  justLoggedIn=true;
  const uid=sbUser?.id;
  const avail=await bioAvailable();
  let skip=false;try{skip=!!localStorage.getItem('ssm_bio_skip_'+uid);}catch(e){}
  if(avail&&uid&&!bioEnabledFor(uid)&&!skip){authMode='biometric';authRender();return;}
  authHide();
}
function authEnableBio(){
  bioEnroll(sbUser.id,sbUser.email).then(()=>{authFinishBio();}).catch(e=>{alert('Biometrie konnte nicht aktiviert werden: '+(e.message||e));authFinishBio();});
}
function authFinishBio(){authHide();authMode='login';}
function authSkipBio(){try{if(sbUser)localStorage.setItem('ssm_bio_skip_'+sbUser.id,'1');}catch(e){}authFinishBio();}
function profEnableBio(){if(!sbUser)return;
  bioEnroll(sbUser.id,sbUser.email).then(()=>{toast('Biometrisches Login aktiviert','ok');
    try{localStorage.removeItem('ssm_bio_skip_'+sbUser.id);}catch(e){}
    const b2=document.getElementById('profBioBtn');if(b2)b2.style.display='none';})
  .catch(e=>toast('Aktivierung fehlgeschlagen: '+(e.message||e),'err'));}
async function authResend(){if(!sb||!sbUser)return;await sb.auth.resend({type:'signup',email:sbUser.email});const b=document.getElementById('emailBanner').querySelector('button');if(b)b.textContent='Gesendet!';}
// --- Biometric (WebAuthn platform authenticator) ---
function _b64u(buf){let s=btoa(String.fromCharCode.apply(null,new Uint8Array(buf)));return s.replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
function _b64uDec(s){s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';const bin=atob(s),b=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)b[i]=bin.charCodeAt(i);return b.buffer;}
async function bioAvailable(){try{return !!(window.PublicKeyCredential&&await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable());}catch(e){return false;}}
function bioName(){const ua=navigator.userAgent;if(/iPhone|iPad|Macintosh/.test(ua))return 'Face ID / Touch ID';if(/Android/.test(ua))return 'Fingerabdruck';return 'Biometrie';}
function bioEnabledFor(uid){try{return !!localStorage.getItem('ssm_bio_'+uid);}catch(e){return false;}}
async function bioEnroll(uid,email){
  const challenge=crypto.getRandomValues(new Uint8Array(32));
  const cred=await navigator.credentials.create({publicKey:{
    challenge,rp:{name:'Swiss Snow Model',id:location.hostname},
    user:{id:new TextEncoder().encode(uid),name:email||uid,displayName:email||'User'},
    pubKeyCredParams:[{type:'public-key',alg:-7},{type:'public-key',alg:-257}],
    authenticatorSelection:{authenticatorAttachment:'platform',userVerification:'required',residentKey:'preferred'},
    timeout:60000,attestation:'none'}});
  localStorage.setItem('ssm_bio_'+uid,_b64u(cred.rawId));
}
async function bioVerify(uid){
  const id=localStorage.getItem('ssm_bio_'+uid);if(!id)throw new Error('no credential');
  const challenge=crypto.getRandomValues(new Uint8Array(32));
  await navigator.credentials.get({publicKey:{challenge,timeout:60000,userVerification:'required',allowCredentials:[{type:'public-key',id:_b64uDec(id)}]}});
}
let bioUnlocked=false;
let bioAutoTried=false;
function showBioLock(){const el=document.getElementById('bioLock');
  if(el.style.display==='flex')return; // already showing — no second prompt
  document.getElementById('bioLockName').textContent=bioName();
  const bn2=document.getElementById('bioLockName2');if(bn2)bn2.textContent=bioName();
  el.style.display='flex';}
  // NOTE: iOS Safari requires a user gesture for navigator.credentials.get(), so we
  // do NOT auto-trigger Face ID (it was silently rejected → "doesn't always work" +
  // an extra failed prompt). The user taps "Entsperren", which is a valid gesture.
async function bioUnlock(auto){try{await bioVerify(sbUser.id);bioUnlocked=true;document.getElementById('bioLock').style.display='none';}catch(e){if(!auto)alert('Entsperren fehlgeschlagen. Bitte erneut versuchen.');}}
function bioSignOut(){document.getElementById('bioLock').style.display='none';bioUnlocked=true;if(sb)sb.auth.signOut();}
function authMenu(){openProfile();}
// --- Profile & Settings ---
let myProfile=null,profAvatarFile=null,profAvatarUrl=null;
function profNav(v){['Main','Pers','Priv','Notif','App'].forEach(k=>{const el=document.getElementById('profView'+k);if(el)el.style.display=(v===k.toLowerCase())?'':'none';});try{haptic(4);}catch(e){}}
function profSetVis(v){try{localStorage.setItem('ssm_visibility',v);}catch(e){}
  document.querySelectorAll('#profVis button').forEach(b=>b.classList.toggle('active',b.dataset.v===v));
  (async()=>{try{if(sb&&sbUser)await sb.from('profiles').update({visibility:v}).eq('id',sbUser.id);}catch(e){}})();
  toast('Sichtbarkeit: '+(v==='me'?'Nur ich':v==='friends'?'Freunde':'Alle'),'ok');}
async function profDeleteContent(btn){if(!sb||!sbUser)return;
  if(!confirm('Wirklich ALLE deine Beiträge (inkl. Kommentare und Bewertungen dazu) löschen? Dein Konto bleibt bestehen.'))return;
  btn.disabled=true;
  try{await sb.from('reports').delete().eq('user_id',sbUser.id);toast('Alle Beiträge gelöscht','ok');loadReportMarkers();loadDbReports();}
  catch(e){toast('Löschen fehlgeschlagen: '+(e.message||e),'err');}
  btn.disabled=false;}
function sanitizeBio(t){
  t=(t||'').replace(/https?:\/\/\S+/gi,'').replace(/www\.\S+/gi,'').replace(/\S+\.(com|net|org|ch|io|de|ly|co)\S*/gi,'');
  const words=t.trim().split(/\s+/).filter(Boolean);
  return words.slice(0,200).join(' ');
}
function profBioInput(){const ta=document.getElementById('profBio');const words=ta.value.trim().split(/\s+/).filter(Boolean).length;
  const cnt=document.getElementById('profBioCnt');cnt.textContent=words+'/200';cnt.classList.toggle('over',words>200);}
async function loadMyProfileAvatar(){if(!sb||!sbUser)return;
  try{const{data}=await sb.from('profiles').select('avatar_url').eq('id',sbUser.id).single();
    if(data&&data.avatar_url){const av=document.getElementById('userPillAv');if(av){av.style.backgroundImage='url('+data.avatar_url+')';av.style.backgroundSize='cover';av.querySelector('span').style.display='none';}
      updateAccountBtn(data.avatar_url);}}catch(e){}}
async function openProfile(){
  if(!sb||!sbUser){authShow();return;}
  document.getElementById('profModal').style.display='flex';
  profNav('main');
  try{const vv=localStorage.getItem('ssm_visibility')||'all';document.querySelectorAll('#profVis button').forEach(b2=>b2.classList.toggle('active',b2.dataset.v===vv));}catch(e){}
  try{const bb=document.getElementById('profBioBtn');
    if(bb)bioAvailable().then(av=>{bb.style.display=(av&&!bioEnabledFor(sbUser.id))?'':'none';});}catch(e){}
  const name=sbUser.user_metadata?.username||sbUser.email?.split('@')[0]||'User';
  document.getElementById('profName').textContent=name;
  document.getElementById('profAvInitial').textContent=name[0].toUpperCase();
  document.getElementById('profEndo').textContent='… Trust Score';
  profAvatarFile=null;
  try{
    const{data}=await sb.from('profiles').select('bio,avatar_url,push_enabled').eq('id',sbUser.id).single();
    myProfile=data||{};
    document.getElementById('profBio').value=data?.bio||'';profBioInput();
    const av=document.getElementById('profAv');
    if(data?.avatar_url){av.classList.add('has-img');av.style.backgroundImage='url('+data.avatar_url+')';profAvatarUrl=data.avatar_url;}
    else{av.classList.remove('has-img');av.style.backgroundImage='';}
    document.getElementById('profPush').classList.toggle('on',!!data?.push_enabled);
  }catch(e){myProfile={};}
  // endorsement total = sum of endorsements across my reports
  try{
    const{data:rs}=await sb.from('reports').select('id').eq('user_id',sbUser.id);
    const ids=(rs||[]).map(r=>r.id);let total=0;
    if(ids.length){const{count}=await sb.from('report_reactions').select('*',{count:'exact',head:true}).eq('type','like').in('report_id',ids);total=count||0;}
    document.getElementById('profEndo').innerHTML='<b>'+total+'</b> Trust Score';
  }catch(e){document.getElementById('profEndo').textContent='';}
}
function profClose(){document.getElementById('profModal').style.display='none';}
function profSetAvatar(inp){if(!inp.files||!inp.files[0])return;profAvatarFile=inp.files[0];
  const url=URL.createObjectURL(profAvatarFile);const av=document.getElementById('profAv');av.classList.add('has-img');av.style.backgroundImage='url('+url+')';}
async function profTogglePush(){
  const t=document.getElementById('profPush');const willOn=!t.classList.contains('on');
  if(willOn&&'Notification'in window){try{const perm=await Notification.requestPermission();if(perm!=='granted'){alert('Benachrichtigungen wurden nicht erlaubt. Bitte im Browser aktivieren.');return;}}catch(e){}}
  t.classList.toggle('on',willOn);
}
async function profSave(){
  if(!sb||!sbUser)return;
  const btn=document.getElementById('profSave');btn.disabled=true;btn.textContent='Speichert…';
  try{
    let avatarUrl=profAvatarUrl;
    if(profAvatarFile){
      const up=await downscaleImage(profAvatarFile,512,0.85);
      const ext=(up.type==='image/jpeg')?'jpg':(profAvatarFile.name.split('.').pop()||'jpg').toLowerCase();
      const path='avatars/'+sbUser.id+'_'+Date.now()+'.'+ext;
      const{error:upErr}=await sb.storage.from('report-images').upload(path,up,{contentType:up.type||'image/jpeg',upsert:true});
      if(!upErr){const{data}=sb.storage.from('report-images').getPublicUrl(path);avatarUrl=data?.publicUrl||avatarUrl;}
    }
    const bio=sanitizeBio(document.getElementById('profBio').value);
    const push=document.getElementById('profPush').classList.contains('on');
    const{error}=await sb.from('profiles').update({bio:bio||null,avatar_url:avatarUrl||null,push_enabled:push}).eq('id',sbUser.id);
    if(error)throw error;
    profAvatarUrl=avatarUrl;loadMyProfileAvatar();profClose();
  }catch(e){alert('Fehler: '+(e.message||e));}
  btn.disabled=false;btn.textContent='Speichern';
}
function profSignOut(){if(sb)sb.auth.signOut();profClose();authUpdateUI(null);}
function sendFeedback(){try{haptic(8);}catch(e){}
  const body=encodeURIComponent('\n\n---\nSwiss Snow Model\n'+(navigator.userAgent||''));
  window.location.href='mailto:giani.morf@bluewin.ch?subject='+encodeURIComponent('Snow-Mapper Feedback')+'&body='+body;}
// --- View another user's profile ---
let uvUid=null;
async function viewUser(uid,username){
  if(!uid){return;} uvUid=uid;
  if(sbUser&&uid===sbUser.id){openProfile();return;}
  document.getElementById('userViewModal').style.display='flex';
  document.getElementById('uvName').textContent=username||'User';
  document.getElementById('uvInitial').textContent=(username||'U')[0].toUpperCase();
  const av=document.getElementById('uvAv');av.classList.remove('has-img');av.style.backgroundImage='';
  document.getElementById('uvBio').textContent='';
  ['uvReports','uvEndoN','uvSince','uvFollowers'].forEach(id=>{const el=document.getElementById(id);if(el)el.textContent='–';});
  const fb=document.getElementById('uvFollow');fb.textContent=myFollowing.has(uid)?'Folge ich':'Folgen';fb.classList.toggle('following',myFollowing.has(uid));
  if(!sb)return;
  try{const{data}=await sb.from('profiles').select('username,bio,avatar_url,created_at').eq('id',uid).single();
    if(data){document.getElementById('uvName').textContent=data.username||username||'User';document.getElementById('uvInitial').textContent=(data.username||'U')[0].toUpperCase();
      document.getElementById('uvBio').textContent=data.bio||'';
      if(data.created_at){const d=new Date(data.created_at);document.getElementById('uvSince').textContent=d.toLocaleDateString('de-CH',{month:'short',year:'2-digit'});}
      if(data.avatar_url){av.classList.add('has-img');av.style.backgroundImage='url('+data.avatar_url+')';}}
  }catch(e){}
  try{const{count:fc}=await sb.from('follows').select('*',{count:'exact',head:true}).eq('following_id',uid);
    const el=document.getElementById('uvFollowers');if(el)el.textContent=fc||0;}catch(e){}
  try{const{data:rs}=await sb.from('reports').select('*').eq('user_id',uid).order('created_at',{ascending:false}).limit(30);
    const ids=(rs||[]).map(r=>r.id);let total=0;
    if(ids.length){const{count}=await sb.from('report_reactions').select('*',{count:'exact',head:true}).eq('type','like').in('report_id',ids);total=count||0;}
    document.getElementById('uvReports').textContent=ids.length;
    document.getElementById('uvEndoN').textContent=total;
    const posts=document.getElementById('uvPosts');
    const rows=(rs||[]).map(r=>{const ll=parseGeo(r.location);const cap=escapeHtml(r.caption||r.subtype||'');
      const meta=escapeHtml((r.condition_data&&r.condition_data.measurement)||r.subtype||'');
      return '<div class="uv-post" onclick="userViewClose();'+(ll?('feedFlyTo('+ll[0]+','+ll[1]+')'):'')+'">'+
        (r.image_url?('<img src="'+r.image_url+'" loading="lazy"/>'):'')+
        '<div class="b"><b>'+meta+'</b>'+(cap?(' — '+cap):'')+'<div class="t">'+timeAgo(r.created_at)+'</div></div></div>';}).join('');
    posts.innerHTML='<h3>Beiträge</h3>'+(rows||'<div style="color:var(--mut);font-size:13px">Noch keine Beiträge.</div>');
  }catch(e){}
}
// --- Swiss/DSG data rights: export + delete + legal re-show ---
async function profExportData(btn){if(!sb||!sbUser)return;
  const orig=btn?btn.textContent:'';if(btn){btn.disabled=true;}
  try{
    const uid=sbUser.id,out={exported_at:new Date().toISOString(),user_id:uid,email:sbUser.email};
    const grab=async(tbl,col)=>{try{const{data}=await sb.from(tbl).select('*').eq(col||'user_id',uid);return data||[];}catch(e){return [];}};
    out.profile=(await grab('profiles','id'))[0]||null;
    out.reports=await grab('reports');
    out.comments=await grab('report_comments');
    out.reactions=await grab('report_reactions');
    out.condition_ratings=await grab('report_conditions');
    out.flags=await grab('report_flags');
    try{const{data:f}=await sb.from('follows').select('*').eq('follower_id',uid);out.follows=f||[];}catch(e){out.follows=[];}
    const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'});
    const a2=document.createElement('a');a2.href=URL.createObjectURL(blob);
    a2.download='snowmapper-daten-'+new Date().toISOString().slice(0,10)+'.json';
    document.body.appendChild(a2);a2.click();a2.remove();
    toast('Datenexport erstellt.','ok');
  }catch(e){toast('Export fehlgeschlagen: '+(e.message||e),'err');}
  if(btn){btn.disabled=false;btn.textContent=orig;}}
function profShowLegal(){profClose();const el=document.getElementById('disc');if(el)el.classList.add('show');
  const chk=document.getElementById('discChk'),bt=document.getElementById('discBtn');
  if(chk){chk.checked=true;}if(bt){bt.disabled=false;bt.textContent='Schliessen';}}
async function profDeleteAccount(btn){if(!sb||!sbUser)return;
  if(!confirm('Konto und ALLE Inhalte (Reports, Fotos-Verweise, Kommentare, Bewertungen) unwiderruflich löschen?'))return;
  if(!confirm('Wirklich sicher? Dieser Schritt kann nicht rückgängig gemacht werden.'))return;
  if(btn)btn.disabled=true;
  try{
    const{error}=await sb.from('profiles').delete().eq('id',sbUser.id);
    if(error)throw error;
    toast('Dein Konto und deine Inhalte wurden gelöscht.','ok');
    try{await sb.auth.signOut();}catch(e){}
    setTimeout(()=>{try{localStorage.clear();}catch(e){}location.reload();},1400);
  }catch(e){toast('Löschen fehlgeschlagen: '+(e.message||e),'err');if(btn)btn.disabled=false;}}
function userViewClose(){document.getElementById('userViewModal').style.display='none';}
let _usT=null;
function usOpen(){document.getElementById('usModal').style.display='flex';setTimeout(()=>{try{document.getElementById('usInput').focus();}catch(e){}},80);}
function usClose(){document.getElementById('usModal').style.display='none';}
function usInput(){clearTimeout(_usT);_usT=setTimeout(usSearch,280);}
async function usSearch(){
  const q=(document.getElementById('usInput').value||'').trim();
  const box=document.getElementById('usList');
  if(!q){box.innerHTML='<div class="us-empty">Tippe einen Namen, um Leute zu finden.</div>';return;}
  if(!sb){box.innerHTML='<div class="us-empty">Offline-Demo — Suche nicht verfügbar.</div>';return;}
  try{
    const{data}=await sb.from('profiles').select('id,username,avatar_url').ilike('username','%'+q.replace(/[%_]/g,'')+'%').limit(15);
    if(!data||!data.length){box.innerHTML='<div class="us-empty">Niemanden gefunden für «'+escapeHtml(q)+'».</div>';return;}
    box.innerHTML=data.map(u=>{
      const me=sbUser&&u.id===sbUser.id;
      const fol=myFollowing.has(u.id);
      return '<div class="us-row" onclick="usClose();viewUser(\''+u.id+'\',\''+escapeHtml(u.username||'User').replace(/'/g,'')+'\')">'
        +'<div class="av"'+(u.avatar_url?' style="background-image:url('+u.avatar_url+')"':'')+'>'+(u.avatar_url?'':escapeHtml((u.username||'U')[0].toUpperCase()))+'</div>'
        +'<b>'+escapeHtml(u.username||'User')+'</b>'
        +(me?'':'<button class="feed-follow '+(fol?'following':'')+'" onclick="toggleFollow(\''+u.id+'\',event);this.classList.toggle(\'following\');this.textContent=this.classList.contains(\'following\')?\'Folge ich\':\'Folgen\';">'+(fol?'Folge ich':'Folgen')+'</button>')
        +'</div>';}).join('');
  }catch(e){box.innerHTML='<div class="us-empty">Suche fehlgeschlagen.</div>';}}
async function uvReportUser(){if(!sb||!sbUser){authShow();return;}if(!uvUid)return;
  const reason=prompt('Warum möchtest du diesen Nutzer melden?');if(reason===null)return;
  try{await sb.from('user_flags').insert({user_id:uvUid,reporter_id:sbUser.id,reason:(reason||'').slice(0,300)});toast('Danke — wir prüfen das.','ok');}
  catch(e){toast('Meldung fehlgeschlagen'+(e.message?': '+e.message:''),'err');}}
async function uvToggleFollow(){if(!sb||!sbUser){authShow();return;}await toggleFollow(uvUid);
  const fb=document.getElementById('uvFollow');fb.textContent=myFollowing.has(uvUid)?'Folge ich':'Folgen';fb.classList.toggle('following',myFollowing.has(uvUid));}
// --- Report categories ---
const RP_CATS=[
  {id:'snow',label:'Schnee',icon:'',subs:['Neuschnee','Nassschnee','Triebschnee','Firn','Bruchharsch','Windgepresst']},
  {id:'route',label:'Menschen',icon:'',subs:['Personen','Verspurtheitsgrad']},
  {id:'danger',label:'Gefahr',icon:'',subs:['Lawine','Warnzeichen','Steinschlag','Blankeis']},
  {id:'info',label:'Info',icon:'',subs:[]}
];
// Follow-up questions per "cat|subtype". Each: {id,label,opts,unit?,multi?}
const RP_FOLLOWUPS={
  'snow|Neuschnee':[{id:'height',label:'Schneehöhe',opts:['0','5','10','20','30','50','100+'],unit:'cm'}],
  'snow|Triebschnee':[{id:'height',label:'Triebschnee-Höhe',opts:['0','5','10','20','30','50','100+'],unit:'cm'}],
  'snow|Nassschnee':[{id:'wet',label:'Wie feucht?',opts:['nur oberflächlich','Stock drückt durch','Ski sinkt ein']}],
  'snow|Bruchharsch':[{id:'crust',label:'Deckel',opts:['feiner Deckel','harter, nicht tragend','tragender Deckel']}],
  'snow|Firn':[{id:'firn',label:'Wann aufgesulzt?',opts:['früh morgens','vormittags','mittags','nachmittags']}],
  'snow|Windgepresst':[{id:'crust',label:'Deckel',opts:['feiner Deckel','harter, nicht tragend','tragender Deckel','eisig']}],
  'route|Personen':[{id:'people',label:'Personen heute',opts:['0','1-2','3-5','6-10','10+']}],
  'route|Verspurtheitsgrad':[{id:'tracked',label:'Verspurtheitsgrad',opts:['0%','25%','50%','75%','100%']}],
  'danger|Lawine':[{id:'trigger',label:'Auslösung',multi:true,opts:['Person','spontan','Fernauslösung']},{id:'size',label:'Grösse',opts:['klein','mittel','gross']}],
  'danger|Warnzeichen':[{id:'sign',label:'Zeichen',multi:true,opts:['Wumm','Risse','Kleiner Rutsch ausgelöst']}],
  'danger|Steinschlag':[],
  'danger|Blankeis':[]
};
function rpFollowups(){return RP_FOLLOWUPS[rpState.cat+'|'+rpState.sub]||[];}
function rpFuAnswered(q){const v=rpState.details[q.id];return q.multi?(Array.isArray(v)&&v.length>0):!!v;}
// FAB opens the SLF-style observation wizard
// --- Single-sheet report flow (legacy, retained for helpers) ---
let rpState={cat:null,sub:null,details:{},infotext:'',stars:0,photo:null,photoFile:null,loc:null,caption:'',peak:null,dest:null,peakCand:null,photoLoc:null,deviceLoc:null,locSource:null};
function rpReset(){rpState={cat:null,sub:null,details:{},infotext:'',stars:0,photo:null,photoFile:null,loc:null,caption:'',peak:null,dest:null,peakCand:null,photoLoc:null,deviceLoc:null,locSource:null};}
// --- EXIF GPS extractor (no external lib) ---
function readExifGps(file){return new Promise(res=>{
  const fr=new FileReader();
  fr.onload=function(e){try{
    const view=new DataView(e.target.result);
    if(view.getUint16(0)!==0xFFD8){res(null);return;}
    const len=view.byteLength;let off=2;
    while(off<len-4){
      const marker=view.getUint16(off);off+=2;
      if(marker===0xFFE1){
        if(view.getUint32(off+2)===0x45786966){res(parseExif(view,off+8));return;}
        off+=view.getUint16(off);
      }else if((marker&0xFF00)!==0xFF00){break;}
      else{off+=view.getUint16(off);}
    }
    res(null);
  }catch(err){res(null);}};
  fr.onerror=()=>res(null);
  fr.readAsArrayBuffer(file.slice(0,262144));
});}
function parseExif(view,tiff){try{
  const little=view.getUint16(tiff)===0x4949;
  const u16=o=>view.getUint16(tiff+o,little),u32=o=>view.getUint32(tiff+o,little),u8=o=>view.getUint8(tiff+o);
  const ifd0=u32(4);let gpsPtr=0;const n0=u16(ifd0);
  for(let i=0;i<n0;i++){const e=ifd0+2+i*12;if(u16(e)===0x8825){gpsPtr=u32(e+8);break;}}
  if(!gpsPtr)return null;
  const rat=o=>{const num=u32(o),den=u32(o+4);return den?num/den:0;};
  const dms=o=>rat(o)+rat(o+8)/60+rat(o+16)/3600;
  let latRef='N',lngRef='E',lat=null,lng=null;const gn=u16(gpsPtr);
  for(let i=0;i<gn;i++){const e=gpsPtr+2+i*12,tag=u16(e),vo=u32(e+8);
    if(tag===1)latRef=String.fromCharCode(u8(e+8));
    else if(tag===2)lat=dms(vo);
    else if(tag===3)lngRef=String.fromCharCode(u8(e+8));
    else if(tag===4)lng=dms(vo);}
  if(lat==null||lng==null)return null;
  if(latRef==='S')lat=-lat;if(lngRef==='W')lng=-lng;
  if(Math.abs(lat)>90||Math.abs(lng)>180)return null;
  return[lat,lng];
}catch(e){return null;}}
function rpScore(){let s=0;if(rpState.cat)s+=20;if(rpState.sub||rpState.infotext)s+=20;if(Object.keys(rpState.details||{}).length)s+=15;if(rpState.photo)s+=25;if(rpState.loc)s+=10;if(rpState.stars)s+=10;return Math.min(100,s);}
// --- Wizard step engine ---
const RP_PHOTO_PLACEHOLDER='<div class="rp-photo-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg><span>Foto aufnehmen</span></div>';
const RP_STEP_META={photo:{t:'Foto hinzufügen',s:'Zeig, was du siehst (optional)'},cat:{t:'Was siehst du?',s:'Wähle eine Kategorie'},sub:{t:'Details',s:'Wähle die Art'},detail:{t:'Präzisieren',s:'Wähle die passende Angabe'},info:{t:'Deine Info',s:'Kurz beschreiben – keine Links'},stars:{t:'Weiterempfehlung',s:'Wie sehr lohnt es sich gerade?'},final:{t:'Fast fertig',s:'Standort & Notiz, dann posten'}};
let rpStepList=[],rpCurStep='photo';
function rpBuildSteps(){const s=['photo','cat'];
  if(rpState.cat==='info'){s.push('info');}
  else if(rpState.cat){s.push('sub');if(rpState.sub&&rpFollowups().length)s.push('detail');}
  // recommendation stars only make sense for snow & route (not danger/info)
  if(rpState.cat==='snow'||rpState.cat==='route')s.push('stars');
  s.push('final');return s;}
function rpShow(stepId){
  rpStepList=rpBuildSteps();if(!rpStepList.includes(stepId))stepId=rpStepList[0];rpCurStep=stepId;
  document.querySelectorAll('#reportOverlay .rp-pane').forEach(p=>p.style.display=p.dataset.step===stepId?'':'none');
  const meta=RP_STEP_META[stepId];
  document.getElementById('rpStepTitle').textContent=meta.t;
  document.getElementById('rpStepSub').textContent=meta.s;
  if(stepId==='sub')rpRenderSubs();
  if(stepId==='detail')rpRenderDetail();
  if(stepId==='info')document.getElementById('rpInfoText').value=rpState.infotext||'';
  if(stepId==='stars')rpRenderStars();
  if(stepId==='final'){updateLocCard();rpRenderSummary();rpDetectPeak();}
  rpRenderProgress();rpRenderNav();
}
function rpRenderProgress(){const idx=rpStepList.indexOf(rpCurStep);
  document.getElementById('rpProgress').innerHTML=rpStepList.map((s,i)=>'<i class="'+(i<idx?'done':i===idx?'cur':'')+'"></i>').join('');}
function rpRenderNav(){const idx=rpStepList.indexOf(rpCurStep);
  document.getElementById('rpBack').disabled=idx<=0;
  const back=document.getElementById('rpBackBtn');if(back)back.style.display=idx>0?'':'none';
  const skip=document.getElementById('rpSkip'),next=document.getElementById('rpNext');
  skip.style.display=(['photo','detail','stars','info'].includes(rpCurStep))?'':'none';
  if(rpCurStep==='final'){next.textContent='Report posten';next.classList.add('post');next.disabled=!rpState.cat;}
  else{next.textContent='Weiter';next.classList.remove('post');next.disabled=(rpCurStep==='cat'&&!rpState.cat)||(rpCurStep==='sub'&&!rpState.sub);}}
function rpStepNext(){const idx=rpStepList.indexOf(rpCurStep);
  if(rpCurStep==='final'){reportSubmit();return;}
  if(rpCurStep==='cat'&&!rpState.cat)return;
  if(rpCurStep==='sub'&&!rpState.sub)return;
  rpShow(rpStepList[Math.min(rpStepList.length-1,idx+1)]);haptic(6);}
function rpStepPrev(){const idx=rpStepList.indexOf(rpCurStep);if(idx>0)rpShow(rpStepList[idx-1]);}
function reportOpenSheet(){
  document.getElementById('reportOverlay').style.display='flex';
  rpState.sub=null;rpState.details={};rpState.infotext='';rpState.stars=0;rpState.caption='';rpState.peak=null;rpState.dest=null;rpState.peakCand=null;
  if(!rpState.cat){rpState.photo=null;rpState.photoFile=null;}
  rpState.photoLoc=null;rpState.deviceLoc=null;rpState.locSource=null;rpState.loc=null;
  document.getElementById('rpCaption').value='';
  rpRenderCats();rpResetPhoto();
  rpShow(rpState.cat?'sub':'photo');
  updateLocCard();rpGeolocate();
}
// Warm, live-updating geolocation (reduces the lag / inaccuracy)
let rpGeoWatch=null;
function rpGeolocate(){
  if(!navigator.geolocation)return;
  navigator.geolocation.getCurrentPosition(rpGeoOk,()=>{updateLocCard();},{enableHighAccuracy:true,timeout:8000,maximumAge:0});
  try{if(rpGeoWatch!=null)navigator.geolocation.clearWatch(rpGeoWatch);
    rpGeoWatch=navigator.geolocation.watchPosition(rpGeoOk,()=>{},{enableHighAccuracy:true,timeout:20000,maximumAge:0});}catch(e){}
}
function rpGeoOk(p){
  const prev=rpState.deviceLoc;
  // keep the most accurate fix
  if(prev&&prev[2]&&p.coords.accuracy>prev[2]*1.4)return;
  rpState.deviceLoc=[p.coords.latitude,p.coords.longitude,p.coords.accuracy];
  if(rpState.locSource!=='photo'){rpState.locSource='device';rpState.loc=[p.coords.latitude,p.coords.longitude];}
  updateLocCard();if(rpCurStep==='final')rpDetectPeak();
}
function updateLocCard(){
  const card=document.getElementById('rpLocCard');if(!card)return;
  const src=rpState.locSource;let html='';
  if(src==='photo'){const l=rpState.photoLoc;
    html='<span>'+ic('cam')+' Foto-Standort · '+l[0].toFixed(4)+', '+l[1].toFixed(4)+'</span>';
    if(rpState.deviceLoc)html+='<button class="rp-loc-switch" onclick="rpSwitchLoc()">Gerät nutzen</button>';
  }else if(src==='device'){const l=rpState.deviceLoc;const acc=l[2]?' ±'+Math.round(l[2])+' m':'';
    html='<span>'+ic('pin')+' Geräte-Standort'+acc+' · '+l[0].toFixed(4)+', '+l[1].toFixed(4)+'</span>';
    if(rpState.photoLoc)html+='<button class="rp-loc-switch" onclick="rpSwitchLoc()">Foto nutzen</button>';
  }else{html='<span id="rpCtxLoc">'+ic('pin')+' Standort wird ermittelt…</span>';}
  card.innerHTML=html;
}
function rpSwitchLoc(){
  if(rpState.locSource==='photo'&&rpState.deviceLoc){rpState.locSource='device';rpState.loc=[rpState.deviceLoc[0],rpState.deviceLoc[1]];}
  else if(rpState.locSource==='device'&&rpState.photoLoc){rpState.locSource='photo';rpState.loc=[rpState.photoLoc[0],rpState.photoLoc[1]];}
  rpState.peak=null;rpState.peakCand=null;haptic(8);updateLocCard();rpDetectPeak();
}
function reportClose(){document.getElementById('reportOverlay').style.display='none';
  try{if(rpGeoWatch!=null){navigator.geolocation.clearWatch(rpGeoWatch);rpGeoWatch=null;}}catch(e){}
  rpReset();}
function rpRenderCats(){
  document.getElementById('rpCats').innerHTML=RP_CATS.map(c=>
    `<button class="cat-chip${rpState.cat===c.id?' active':''}" data-id="${c.id}" style="${rpState.cat===c.id?'color:'+CAT_COLORS[c.id]:''}" onclick="rpPickCat('${c.id}')"><span class="cat-ico-w" style="color:${CAT_COLORS[c.id]}">${CAT_SVG[c.id]}</span>${c.label}</button>`
  ).join('');
}
function rpPickCat(id){rpState.cat=id;rpState.sub=null;rpState.details={};haptic(10);
  document.querySelectorAll('#rpCats .cat-chip').forEach(el=>{const on=el.dataset.id===id;el.classList.toggle('active',on);el.style.color=on?CAT_COLORS[id]:'';});
  rpStepList=rpBuildSteps();setTimeout(()=>rpShow(id==='info'?'info':'sub'),170);}
function rpRenderSubs(){const cat=RP_CATS.find(c=>c.id===rpState.cat);if(!cat)return;
  document.getElementById('rpSubs').innerHTML=cat.subs.map(s=>
    `<button class="sub-chip${rpState.sub===s?' active':''}" onclick="rpPickSub(this,'${s.replace(/'/g,"\\\\'")}')">${s}</button>`).join('');}
function rpPickSub(el,val){rpState.sub=val;rpState.details={};haptic(8);
  document.querySelectorAll('#rpSubs .sub-chip').forEach(e=>e.classList.remove('active'));el.classList.add('active');
  rpStepList=rpBuildSteps();setTimeout(()=>rpStepNext(),170);}
function rpRenderDetail(){const fus=rpFollowups();const el=document.getElementById('rpDetail');
  el.innerHTML=fus.map(q=>{const sel=rpState.details[q.id];
    const hint=q.multi?' <span class="rp-fu-multi">· mehrere möglich</span>':'';
    return `<div class="rp-fu"><div class="rp-fu-lbl">${q.label}${q.unit?' ('+q.unit+')':''}${hint}</div><div class="rp-fu-opts">${q.opts.map(o=>{const on=q.multi?(Array.isArray(sel)&&sel.includes(o)):(sel===o);return `<button class="${on?'active':''}" onclick="rpPickDetail('${q.id}','${o.replace(/'/g,"\\\\'")}',${q.multi?'true':'false'},this)">${o}</button>`;}).join('')}</div></div>`;}).join('');
  rpRenderNav();}
function rpPickDetail(qid,val,multi,el){haptic(6);
  if(multi){let arr=rpState.details[qid];if(!Array.isArray(arr))arr=[];const i=arr.indexOf(val);if(i>=0)arr.splice(i,1);else arr.push(val);rpState.details[qid]=arr;el.classList.toggle('active',arr.indexOf(val)>=0);}
  else{rpState.details[qid]=val;el.parentElement.querySelectorAll('button').forEach(b=>b.classList.remove('active'));el.classList.add('active');}
  const fus=rpFollowups();
  if(fus.every(rpFuAnswered)&&!fus.some(q=>q.multi))setTimeout(()=>rpStepNext(),220);
  rpRenderNav();}
function rpRenderStars(){const el=document.getElementById('rpStars');const cur=rpState.stars||0;
  el.innerHTML=[1,2,3,4,5].map(n=>`<button class="${n<=cur?'on':''}" onclick="rpPickStar(${n})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M12 2l3 6.9 7.5.6-5.7 4.9 1.8 7.3L12 17.8 5.1 21.7l1.8-7.3L1.2 9.5l7.5-.6z"/></svg></button>`).join('');
  const lbls=['','Nicht empfohlen','Mässig','Ordentlich','Sehr gut','Top – unbedingt!'];
  document.getElementById('rpStarsLbl').textContent=cur?lbls[cur]:'Tippe auf die Sterne';}
function rpPickStar(n){rpState.stars=n;haptic(8);rpRenderStars();}
function rpSetPhoto(inp){
  if(!inp.files||!inp.files[0])return;
  const file=inp.files[0];
  rpState.photoFile=file;rpState.photo=URL.createObjectURL(file);
  rpResetPhoto();haptic(12);
  // Try to read GPS from the photo's EXIF (uploaded photos often carry it)
  readExifGps(file).then(gps=>{
    if(gps){rpState.photoLoc=gps;rpState.locSource='photo';rpState.loc=[gps[0],gps[1]];rpState.peak=null;rpState.peakCand=null;}
    else if(!rpState.locSource&&rpState.deviceLoc){rpState.locSource='device';rpState.loc=[rpState.deviceLoc[0],rpState.deviceLoc[1]];}
    updateLocCard();if(rpCurStep==='final')rpDetectPeak();
  });
  setTimeout(()=>rpShow('cat'),220);
}
function rpResetPhoto(){const big=document.getElementById('rpPhotoBig');if(!big)return;
  if(rpState.photo){big.classList.add('has-img');big.innerHTML=`<img src="${rpState.photo}" alt=""/>`;}
  else{big.classList.remove('has-img');big.innerHTML=RP_PHOTO_PLACEHOLDER;}}
// --- Peak detection ---
function rpDetectPeak(){
  const box=document.getElementById('rpPeakConfirm');if(!box)return;
  const loc=rpState.loc;if(!loc){box.style.display='none';rpRenderSummary();return;}
  const nd=nearestOf(DESTS,loc[0],loc[1]);rpState.dest=(nd&&nd.km<=12)?nd.item.n:null;
  if(rpState.peak){box.className='rp-peak confirmed';box.style.display='';box.innerHTML='<div class="rp-peak-q">✓ Gipfel: '+rpState.peak+'</div>';rpRenderSummary();return;}
  const np=nearestOf(PEAKS,loc[0],loc[1]);
  if(np&&np.km<=2.0){rpState.peakCand=np.item.n;box.className='rp-peak';box.style.display='';
    box.innerHTML='<div class="rp-peak-q">Bist du beim '+np.item.n+' ('+np.item.e+' m)?</div><div class="rp-peak-btns"><button class="yes" onclick="rpConfirmPeak(true)">Ja</button><button onclick="rpConfirmPeak(false)">Nein</button></div>';}
  else{rpState.peakCand=null;box.style.display='none';}
  rpRenderSummary();}
function rpConfirmPeak(yes){rpState.peak=yes?rpState.peakCand:null;haptic(12);rpDetectPeak();}
function rpRenderSummary(){const el=document.getElementById('rpSummary');if(!el)return;
  const cat=RP_CATS.find(c=>c.id===rpState.cat);const t=[];
  if(cat)t.push('<span class="rp-tag">'+catSvg(cat.id,13)+cat.label+'</span>');
  if(rpState.sub)t.push('<span class="rp-tag">'+rpState.sub+'</span>');
  const fus=rpFollowups();fus.forEach(q=>{const dv=rpState.details[q.id];if(dv&&(!Array.isArray(dv)||dv.length)){const txt=Array.isArray(dv)?dv.join(' + '):dv;t.push('<span class="rp-tag">'+txt+(q.unit&&q.id==='height'?' '+q.unit:'')+'</span>');}});
  if(rpState.stars)t.push('<span class="rp-tag">'+ic('star')+' '+rpState.stars+'/5</span>');
  if(rpState.photo)t.push('<span class="rp-tag">'+ic('cam')+' Foto</span>');
  if(rpState.peak)t.push('<span class="rp-tag">'+ic('peak')+' '+rpState.peak+'</span>');
  else if(rpState.dest)t.push('<span class="rp-tag">'+ic('pin')+' '+rpState.dest+'</span>');
  el.innerHTML=t.join('');}
// --- Voice drop ---
let rpRecognition=null,rpRecording=false;
function rpVoiceToggle(){
  const btn=document.getElementById('rpVoiceBtn');
  if(rpRecording){if(rpRecognition)rpRecognition.stop();rpRecording=false;btn.classList.remove('recording');btn.textContent='🎤 Halten und sprechen';return;}
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){document.getElementById('rpCaption').focus();return;}
  rpRecognition=new SR();rpRecognition.lang='de-CH';rpRecognition.continuous=false;rpRecognition.interimResults=true;
  rpRecognition.onresult=e=>{
    let t='';for(let i=0;i<e.results.length;i++)t+=e.results[i][0].transcript;
    document.getElementById('rpCaption').value=t;rpState.caption=t;
  };
  rpRecognition.onend=()=>{rpRecording=false;btn.classList.remove('recording');btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;vertical-align:-3px"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><line x1="12" y1="19" x2="12" y2="22"/></svg> Halten und sprechen';};
  rpRecognition.start();rpRecording=true;btn.classList.add('recording');btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;vertical-align:-3px"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><line x1="12" y1="19" x2="12" y2="22"/></svg> Aufnahme…';haptic(12);
}
async function reportSubmit(){
  if(!sb||!sbUser||!rpState.cat)return;
  const next=document.getElementById('rpNext');next.disabled=true;next.textContent='Poste…';
  try{
    let imageUrl=null;
    if(rpState.photoFile){
      const up=await downscaleImage(rpState.photoFile);
      const ext=(up.type==='image/jpeg')?'jpg':(rpState.photoFile.name.split('.').pop()||'jpg').toLowerCase();
      const path=`${sbUser.id}/${Date.now()}.${ext}`;
      const{error:upErr}=await sb.storage.from('report-images').upload(path,up,{contentType:up.type||'image/jpeg',upsert:false});
      if(upErr){console.error('Foto-Upload fehlgeschlagen',upErr);
        if(!confirm('Foto konnte nicht hochgeladen werden: '+(upErr.message||upErr)+' — Report trotzdem ohne Foto posten?')){next.disabled=false;next.textContent='Report posten';return;}}
      else{const{data:urlData}=sb.storage.from('report-images').getPublicUrl(path);imageUrl=urlData?.publicUrl||null;}
    }
    const loc=rpState.loc||[map.getCenter().lat,map.getCenter().lng];
    const cd={};
    const fus=rpFollowups(),dvals={};fus.forEach(q=>{if(rpState.details[q.id])dvals[q.id]=rpState.details[q.id];});
    if(Object.keys(dvals).length)cd.details=dvals;
    if(rpState.stars)cd.stars=rpState.stars;
    if(rpState.peak)cd.peak=rpState.peak;if(rpState.dest)cd.dest=rpState.dest;
    // human-readable measurement for the card
    let meas=null;if(dvals.height)meas=dvals.height+' cm';else if(fus[0]&&dvals[fus[0].id]){const dv=dvals[fus[0].id];meas=Array.isArray(dv)?dv.join(', '):dv;}
    if(meas)cd.measurement=meas;
    let caption=rpState.caption.trim();let subtype=rpState.sub;
    if(rpState.cat==='info'){const info=(rpState.infotext||'').trim();if(info){cd.info=info;if(!caption)caption=info;}subtype=subtype||'Info';}
    const row={
      user_id:sbUser.id,location:`POINT(${loc[1]} ${loc[0]})`,
      primary_categories:[rpState.cat],subtype:subtype,
      condition_data:cd,
      image_url:imageUrl,caption:caption||null,
      completion_score:rpScore()
    };
    const{error}=await sb.from('reports').insert(row);
    if(error)throw error;
    reportClose();loadDbReports();
    showUndo();
  }catch(err){alert('Error: '+(err.message||err));next.disabled=false;next.textContent='Report posten';}
}
function showUndo(){
  const bar=document.createElement('div');bar.className='undo-bar';
  bar.innerHTML='Gemeldet. Danke! Andere sehen\'s jetzt auf der Karte. <button onclick="this.parentElement.remove()">OK</button>';
  document.body.appendChild(bar);setTimeout(()=>bar.remove(),5000);
}
// ============================================================
// SLF-style observation reporting wizard
// ============================================================
const OBS_TYPE_LIST=[
 {id:'avalanche',label:'Lawine',sub:'Spontan oder ausgelöst',color:'#d03050',tint:'rgba(208,48,80,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M4 20l6-13 4 7"/><path d="M14 20c1-3 3-5 6-6"/></svg>'},
 {id:'whumpf',label:'Wumm-Geräusch',sub:'Setzungsgeräusche im Schnee',color:'#e8590c',tint:'rgba(232,89,12,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18"/><circle cx="9" cy="15" r="2"/><path d="M14 9c2 1 3 3 3 5M17 5c3 2 4 6 4 10"/></svg>'},
 {id:'wind_slab',label:'Triebschnee',sub:'Windverfrachteter Schnee',color:'#0d9488',tint:'rgba(13,148,136,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18h18M4 18l7-9 5 6"/><path d="M9.6 4.6A2 2 0 1 1 11 8H2"/></svg>'},
 {id:'snow',label:'Schneequalität',sub:'Pulver · Harsch · Firn · Nassschnee',color:'#7d6bd6',tint:'rgba(42,138,176,.14)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M4 6l16 12M20 6L4 18"/><path d="M12 2l-2.5 2.5M12 2l2.5 2.5M12 22l-2.5-2.5M12 22l2.5-2.5M4 6l.2 3.4M4 6l3.4-.2M20 18l-.2-3.4M20 18l-3.4.2M20 6l-3.4-.2M20 6l-.2 3.4M4 18l3.4.2M4 18l.2-3.4"/></svg>'},
 {id:'other',label:'Andere Beobachtung',sub:'Freie Geländemeldung',color:'#5e5ce6',tint:'rgba(94,92,230,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M5 20l5-9 4 6 5-9"/><path d="M15 5h6v5"/></svg>'}
];
const SNOW_KINDS=[
 {k:'powder',l:'Pulver',c:'#5e5ce6'},
 {k:'wind_powder',l:'Triebschnee-Pulver',c:'#7d6bd6'},
 {k:'wind_pressed',l:'Windharsch',c:'#0d9488'},
 {k:'melt_crust',l:'Schmelzharsch',c:'#e8590c'},
 {k:'wet',l:'Nassschnee',c:'#7b5cff'},
 {k:'firn',l:'Firn',c:'#f5a623'}
];
const SNOW_LIGHT=[['very_light','sehr leicht'],['light','leicht'],['medium','mittel'],['dense','schwer / feucht']];
const SNOW_THICK=[['carries','trägt'],['breaks','bricht durch'],['thin','dünn / leicht']];
const SNOW_WET=[['surface','nur Oberfläche nass'],['sticky','Ski bleibt leicht stecken'],['sink','Ski sinkt ganz ein']];
const SNOW_FIRN=[['hard','noch hart'],['semi','halb aufgefirnt'],['full','komplett Firn']];
const ASPECT8=['N','NE','E','SE','S','SW','W','NW'];
function snowKindLabel(k){const f=SNOW_KINDS.find(x=>x.k===k);return f?f.l:null;}
const OBS_SIZE=[
 {k:'small',r:1,l:'Klein',obj:'⛷️',c:'Verschüttung von Personen unwahrscheinlich'},
 {k:'medium',r:2,l:'Mittel',obj:'🧍',c:'Kann Personen verschütten, verletzen oder töten'},
 {k:'large',r:3,l:'Gross',obj:'🚗',c:'Kann Autos verschütten und zerstören'},
 {k:'very_large',r:4,l:'Sehr gross',obj:'🚆',c:'Kann Lastwagen und Züge verschütten und zerstören'},
 {k:'extreme',r:5,l:'Extrem gross',obj:'🏘️',c:'Kann die Landschaft katastrophal verwüsten'},
 {k:'unknown',r:0,l:'Unbekannt',obj:'❓',c:''}
];
function obsSizeMeta(k){return OBS_SIZE.find(s=>s.k===k)||OBS_SIZE[5];}
function compareSize(a,b){const ra=obsSizeMeta(a).r,rb=obsSizeMeta(b).r;if(!ra||!rb)return null;return ra===rb?0:(ra>rb?1:-1);}
const OBS_ENUM={
 whumpfFrequency:{title:'Wumm-Geräusche',sub:'Setzungsgeräusche deuten auf Schwachschichten hin.',required:true,opts:[{k:'none',l:'Keine'},{k:'rare',l:'Selten',ct:'1–3'},{k:'frequent',l:'Häufig',ct:'>3'}]},
 windSlab24h:{title:'Triebschnee (letzte 24 h)',sub:'Wie viel frischer Triebschnee?',required:true,opts:[{k:'none',l:'Kein'},{k:'small',l:'Klein',ct:'5–20 cm'},{k:'medium',l:'Mittel',ct:'20–50 cm'},{k:'large',l:'Gross',ct:'>50 cm'}]}
};
const OBS_TRIGGER=[['spontaneous','Spontan'],['person','Person'],['explosive','Sprengung'],['snow_groomer','Pistenfahrzeug'],['other','Andere'],['unknown','Unbekannt']];
const OBS_BURIAL=[['not_buried','Nicht verschüttet'],['partially_buried','Teilweise verschüttet'],['fully_buried','Vollständig verschüttet']];
const OBS_CONSEQ=[['uninjured','Unverletzt'],['injured','Verletzt'],['deceased','Verstorben']];
const OBS_AVTYPE=[['glide_snow','Gleitschnee'],['loose_snow','Lockerschnee'],['slab','Schneebrett'],['unknown','Unbekannt']];
const OBS_WET=[['dry','Trocken'],['wet','Nass'],['unknown','Unbekannt']];
const OBS_STEPS={
 avalanche:[{k:'media'},{k:'avdetails'},{k:'comment'},{k:'submit'}],
 whumpf:[{k:'enum',f:'whumpfFrequency'},{k:'enum',f:'windSlab24h'},{k:'media_comment',final:true}],
 wind_slab:[{k:'enum',f:'windSlab24h'},{k:'media_comment',final:true}],
 snow:[{k:'snowcond'},{k:'media_comment',final:true}],
 other:[{k:'media'},{k:'comment',final:true}]
};
function obsLbl(arr,k){const f=arr.find(x=>x[0]===k);return f?f[1]:k;}
function obsEnumLabel(f,k){const o=(OBS_ENUM[f].opts).find(x=>x.k===k);return o?o.l:k;}
let obsState=null,obsDeviceFix=null,obsMap=null,obsMarker=null,obsOpenCards=new Set();
function obsNewState(type){return{type,step:0,steps:OBS_STEPS[type]||[],media:[],comment:'',
  location:{lat:null,lon:null,elevation:null,aspect:null,source:null},observedAt:null,
  avalanche:{triggerType:'unknown',remoteTrigger:false,caughtPersons:[],characteristics:{size:'unknown',sizeRank:0,avalancheType:'unknown',wetness:'unknown'}},
  whumpfFrequency:null,windSlab24h:null,
  snow:{kind:null,depth:30,lightness:null,powderline:2200,thickness:null,alt:2200,altLow:1800,altHigh:2600,aspects:[],wetness:null,firnState:null,firnTime:''}};}
function obsDEM(lat,lon){const cx2=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy2=Math.round((laMax-lat)/(laMax-laMin)*(H-1));if(cx2<0||cx2>=W||cy2<0||cy2>=H)return{};const p=cy2*W+cx2;return{elev:Math.round(melevv(p)),aspect:aspectQ(maspv(p))};}
function obsApplyLoc(lat,lon,source){const d=obsDEM(lat,lon);obsState.location={lat,lon,elevation:(d.elev!=null?d.elev:null),aspect:(d.aspect||null),source};}
function obsWarmLocation(){if(!navigator.geolocation)return;navigator.geolocation.getCurrentPosition(p=>{obsDeviceFix={lat:p.coords.latitude,lon:p.coords.longitude};
  if(obsState&&obsState.location.source!=='exif'&&obsState.location.source!=='manual'){obsApplyLoc(p.coords.latitude,p.coords.longitude,'device');if(!obsState.observedAt)obsState.observedAt=new Date();const st=obsState.steps[obsState.step];if(st&&(st.k==='submit'||st.final))obsRender();}
},()=>{},{enableHighAccuracy:true,timeout:12000,maximumAge:0});}
function obsOpen(){if(!sb||!sbUser){authShow();return;}
  obsState=null;obsOpenCards=new Set();document.getElementById('reportOverlay').style.display='flex';
  document.getElementById('obsBack').style.visibility='hidden';
  document.getElementById('obsProgress').innerHTML='';
  document.getElementById('obsTitle').textContent='Beobachtung melden';
  document.getElementById('obsSub').textContent='Was hast du gesehen?';
  document.getElementById('obsNav').style.display='none';
  const SCATTER=[{dx:-4,dy:0,r:-2},{dx:5,dy:9,r:1.6},{dx:-6,dy:-5,r:1.2},{dx:4,dy:-8,r:-1.4},{dx:0,dy:2,r:.8}];
  document.getElementById('obsBody').innerHTML=
    '<div class="obs-types cluster">'+OBS_TYPE_LIST.map((t,i)=>{const s=SCATTER[i%SCATTER.length];
    return `<button class="obs-type" style="--i:${i};--dx:${s.dx}px;--dy:${s.dy}px;--rot:${s.r}deg;--tc:${t.color};--tt:${t.tint}" onclick="obsStart('${t.id}')"><span class="obs-type-ic">${t.icon}</span><span class="obs-type-tx"><b>${t.label}</b><span>${t.sub}</span></span></button>`;}).join('')+'</div>'+
    '<button class="obs-quick" style="--i:5" onclick="obsClose();qrOpen()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg><span><b>Quick Powder Report</b><em>Ein Fingertipp — Menge &amp; Qualität</em></span></button>';
  obsWarmLocation();
}
function obsStart(type){obsState=obsNewState(type);obsOpenCards=new Set();
  if(obsDeviceFix){obsApplyLoc(obsDeviceFix.lat,obsDeviceFix.lon,'device');}
  obsState.observedAt=new Date();
  document.getElementById('obsNav').style.display='';obsRender();haptic(8);}
function obsClose(){if(obsState&&(obsState.media.length||obsState.comment)){if(!confirm('Meldung verwerfen?'))return;}
  document.getElementById('reportOverlay').style.display='none';if(obsMap){try{obsMap.remove();}catch(e){}obsMap=null;}obsState=null;}
function obsBack(){if(!obsState){obsClose();return;}if(obsState.step>0){obsState.step--;obsRender();}else{obsOpen();}}
function obsStepDisabled(st){if(st.k==='enum'){const e=OBS_ENUM[st.f];if(e.required&&!obsState[st.f])return true;}
  if(st.k==='snowcond'){if(!obsState.snow.kind)return true;}
  if(st.k==='submit'||st.final){if(obsState.location.lat==null)return true;if(obsState.observedAt&&obsState.observedAt.getTime()>Date.now())return true;}
  return false;}
function obsNext(){const steps=obsState.steps,st=steps[obsState.step];
  if(obsStepDisabled(st))return;
  if(st.k==='submit'||st.final){obsSubmit();return;}
  if(obsState.step<steps.length-1){obsState.step++;obsRender();haptic(6);}}
const OBS_TITLES={media:['Fotos & Videos','Übersicht + Detail helfen am meisten'],avdetails:['Lawinendetails','Optional – tippe zum Ausklappen'],comment:['Kommentar','Optional, max. 500 Zeichen'],media_comment:['Beobachtung erfassen','Fotos & Kommentar'],submit:['Standort & Absenden','Prüfe Ort und Zeit'],snowcond:['Schneequalität','Was liegt & wie fährt es sich?']};
function obsRender(){const steps=obsState.steps,i=obsState.step,st=steps[i];
  document.getElementById('obsBack').style.visibility='visible';
  document.getElementById('obsProgress').innerHTML=steps.map((s,ix)=>`<i class="${ix<i?'done':ix===i?'cur':''}"></i>`).join('');
  const isFinal=st.k==='submit'||st.final;
  let title='',sub='';
  if(st.k==='enum'){title=OBS_ENUM[st.f].title;sub=OBS_ENUM[st.f].sub;}
  else{const tt=OBS_TITLES[st.k]||['',''];title=tt[0];sub=tt[1];}
  document.getElementById('obsTitle').textContent=title;document.getElementById('obsSub').textContent=sub;
  let body='';
  if(st.k==='media')body=obsMediaBlock(obsState.type==='avalanche');
  else if(st.k==='comment')body=obsCommentHTML();
  else if(st.k==='media_comment')body=obsMediaBlock(false)+obsCommentHTML();
  else if(st.k==='enum')body=obsEnumHTML(st.f);
  else if(st.k==='snowcond')body=obsSnowHTML();
  else if(st.k==='avdetails')body=obsAvDetailsHTML();
  if(isFinal)body+=obsLocationHTML()+obsSummaryHTML();
  document.getElementById('obsBody').innerHTML=body;
  if(st.k==='snowcond')setTimeout(obsAltBandAttach,0);
  if(isFinal)setTimeout(obsInitMap,30);
  const next=document.getElementById('obsNext');next.classList.toggle('post',isFinal);next.textContent=isFinal?'Melden':'Weiter';next.disabled=obsStepDisabled(st);}
function obsMediaBlock(av){const hint=av?'<div class="obs-hint">Am hilfreichsten: Übersichtsfotos der ganzen Lawine + Detailaufnahmen der Anrisskante / des Anrissgebiets. Fotos liefern automatisch Standort & Zeit.</div>':'<div class="obs-hint">Relevante Beobachtungen aus dem Gelände. Fotos liefern automatisch Standort & Zeit.</div>';
  return hint+'<div id="obsMediaWrap">'+obsMediaGrid()+'</div>';}
function obsMediaGrid(){const tiles=obsState.media.map((m,ix)=>`<div class="obs-media-tile"><img src="${m.url}"/><button class="rm" onclick="obsRemoveMedia(${ix})">✕</button></div>`).join('');
  const add='<div class="obs-media-add" onclick="document.getElementById(\'obsFile\').click()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg><span>Hinzufügen</span></div>';
  return '<div class="obs-media-grid">'+tiles+add+'</div>';}
function obsRerenderMedia(){const w=document.getElementById('obsMediaWrap');if(w)w.innerHTML=obsMediaGrid();}
function obsCommentHTML(){const v=obsState.comment||'';return '<textarea class="rp-caption" id="obsComment" maxlength="500" placeholder="Kommentar (optional)…" oninput="obsState.comment=this.value;var c=document.getElementById(\'obsCC\');if(c)c.textContent=this.value.length+\'/500\'" style="margin-top:0">'+v+'</textarea><div class="obs-cc" id="obsCC">'+v.length+'/500</div>';}
function obsEnumHTML(f){const e=OBS_ENUM[f];return '<div class="obs-enum">'+e.opts.map(o=>`<button class="${obsState[f]===o.k?'active':''}" onclick="obsPickEnum('${f}','${o.k}')"><span>${o.l}${o.ct?'<span class="ct">'+o.ct+'</span>':''}</span><span class="rd"></span></button>`).join('')+'</div>';}
function obsPickEnum(f,k){obsState[f]=k;haptic(6);obsRender();}
// --- Snow-condition step (kind selector + per-kind fields) ---
function obsSnowHTML(){const s=obsState.snow;
  let h='<div class="obs-fld-l">Schneeart</div><div class="snow-kinds">'+SNOW_KINDS.map(k=>`<button class="snow-kind${s.kind===k.k?' active':''}" style="--k:${k.c}" onclick="obsSnowKind('${k.k}')">${k.l}</button>`).join('')+'</div>';
  if(s.kind)h+='<div class="snow-fields">'+obsSnowFields(s.kind)+'</div>';
  return h;}
function obsSnowKind(k){obsState.snow.kind=(obsState.snow.kind===k?null:k);haptic(6);obsRender();}
function obsSnowSet(f,v){obsState.snow[f]=(obsState.snow[f]===v?null:v);haptic(5);obsRender();}
function obsSnowFields(kind){
  let f='';
  if(kind==='powder')f=obsDepthSlider('depth','Pulvertiefe')+obsSeg('lightness','Konsistenz',SNOW_LIGHT);
  if(kind==='wind_powder')f=obsDepthSlider('depth','Tiefe der Auflage');
  if(kind==='wind_pressed')f=obsSeg('thickness','Winddeckel',SNOW_THICK);
  if(kind==='melt_crust')f=obsSeg('thickness','Bruchharsch-Deckel',SNOW_THICK);
  if(kind==='wet')f=obsSeg('wetness','Nässegrad',SNOW_WET);
  if(kind==='firn')f=obsSeg('firnState','Firn-Reife',SNOW_FIRN)+obsFirnTime();
  // for every snow kind: altitude band (dual handles) + exposure rose
  return f+obsAltBand2('Höhenband')+obsRose('Betroffene Expositionen');
  return '';}
function obsSeg(field,label,opts){const v=obsState.snow[field];
  return '<div class="obs-fld"><div class="obs-fld-l">'+label+'</div><div class="obs-chips">'+opts.map(o=>`<button class="${v===o[0]?'active':''}" onclick="obsSnowSet('${field}','${o[0]}')">${o[1]}</button>`).join('')+'</div></div>';}
function obsDepthSlider(field,label){const v=obsState.snow[field]||0;
  return '<div class="obs-fld"><div class="obs-fld-l">'+label+' <b class="snow-val" id="sv_'+field+'">'+v+' cm</b></div><input type="range" class="obs-range" min="0" max="120" step="5" value="'+v+'" oninput="obsState.snow.'+field+'=+this.value;var l=document.getElementById(\'sv_'+field+'\');if(l)l.textContent=this.value+\' cm\'"></div>';}
function obsAltSlider(field,label){const v=obsState.snow[field]||2000;
  return '<div class="obs-fld"><div class="obs-fld-l">'+label+' <b class="snow-val" id="sv_'+field+'">'+v+' m</b></div><input type="range" class="obs-range" min="500" max="4000" step="50" value="'+v+'" oninput="obsState.snow.'+field+'=+this.value;var l=document.getElementById(\'sv_'+field+'\');if(l)l.textContent=this.value+\' m\'"></div>';}
function obsAltRange(label){const lo=obsState.snow.altLow,hi=obsState.snow.altHigh;
  return '<div class="obs-fld"><div class="obs-fld-l">'+label+' <b class="snow-val" id="sv_altband">'+lo+'–'+hi+' m</b></div>'+
   '<div class="obs-range-row"><span>ab</span><input type="range" class="obs-range" min="500" max="4000" step="50" value="'+lo+'" oninput="obsAltBand(\'lo\',+this.value)"></div>'+
   '<div class="obs-range-row"><span>bis</span><input type="range" class="obs-range" min="500" max="4000" step="50" value="'+hi+'" oninput="obsAltBand(\'hi\',+this.value)"></div></div>';}
function obsAltBand2(label){const lo=obsState.snow.altLow,hi=obsState.snow.altHigh,mn=500,mx=4000;
  const p=v=>((v-mn)/(mx-mn)*100).toFixed(2);
  return '<div class="obs-fld"><div class="obs-fld-l">'+label+'</div>'+
   '<div class="alt-band" id="altBand">'+
    '<div class="ab-fill" style="left:'+p(lo)+'%;right:'+(100-parseFloat(p(hi)))+'%"></div>'+
    '<div class="ab-h" data-h="lo" style="left:'+p(lo)+'%"><span>'+lo+'</span></div>'+
    '<div class="ab-h" data-h="hi" style="left:'+p(hi)+'%"><span>'+hi+'</span></div>'+
   '</div><div class="ab-scale"><i>500</i><i>4000 m</i></div></div>';}
function obsAltBandAttach(){const band=document.getElementById('altBand');if(!band||band._wired)return;band._wired=true;
  band.style.touchAction='none';const mn=500,mx=4000;
  let cur=null;
  function setV(which,clientX){const r=band.getBoundingClientRect();
    let v=mn+(mx-mn)*Math.max(0,Math.min(1,(clientX-r.left)/r.width));v=Math.round(v/50)*50;
    const sn=obsState.snow;
    if(which==='lo')sn.altLow=Math.min(v,sn.altHigh);else sn.altHigh=Math.max(v,sn.altLow);
    const p=x=>((x-mn)/(mx-mn)*100)+'%';
    const lo=band.querySelector('[data-h="lo"]'),hi=band.querySelector('[data-h="hi"]'),fill=band.querySelector('.ab-fill');
    lo.style.left=p(sn.altLow);hi.style.left=p(sn.altHigh);
    lo.querySelector('span').textContent=sn.altLow;hi.querySelector('span').textContent=sn.altHigh;
    fill.style.left=p(sn.altLow);fill.style.right=(100-((sn.altHigh-mn)/(mx-mn)*100))+'%';}
  band.addEventListener('pointerdown',e=>{const h=e.target.closest('.ab-h');
    if(h)cur=h.dataset.h;else{const r=band.getBoundingClientRect();const fx=(e.clientX-r.left)/r.width;
      const sn=obsState.snow;const dl=Math.abs(fx*(mx-mn)+mn-sn.altLow),dh=Math.abs(fx*(mx-mn)+mn-sn.altHigh);
      cur=dl<=dh?'lo':'hi';}
    try{band.setPointerCapture(e.pointerId);}catch(_){}setV(cur,e.clientX);});
  band.addEventListener('pointermove',e=>{if(cur)setV(cur,e.clientX);});
  const fin=()=>{cur=null;};
  band.addEventListener('pointerup',fin);band.addEventListener('pointercancel',fin);}
function obsAltBand(which,val){const s=obsState.snow;if(which==='lo')s.altLow=Math.min(val,s.altHigh);else s.altHigh=Math.max(val,s.altLow);
  var l=document.getElementById('sv_altband');if(l)l.textContent=s.altLow+'–'+s.altHigh+' m';}
function obsRose(label){const sel=obsState.snow.aspects||[];const cx=92,cy=92,rO=80,rI=34;let paths='';
  for(let i=0;i<8;i++){const mid=i*45;const a0=(mid-22.5-90)*Math.PI/180,a1=(mid+22.5-90)*Math.PI/180;
    const P=(r,a)=>[(cx+r*Math.cos(a)).toFixed(1),(cy+r*Math.sin(a)).toFixed(1)];
    const o0=P(rO,a0),o1=P(rO,a1),i0=P(rI,a0),i1=P(rI,a1);
    const d='M'+i0[0]+' '+i0[1]+' L'+o0[0]+' '+o0[1]+' A'+rO+' '+rO+' 0 0 1 '+o1[0]+' '+o1[1]+' L'+i1[0]+' '+i1[1]+' A'+rI+' '+rI+' 0 0 0 '+i0[0]+' '+i0[1]+' Z';
    const on=sel.indexOf(ASPECT8[i])>=0;const lm=(mid-90)*Math.PI/180,lr=(rO+rI)/2;
    const lx=(cx+lr*Math.cos(lm)).toFixed(1),ly=(cy+lr*Math.sin(lm)+4).toFixed(1);
    paths+='<path d="'+d+'" class="rose-w'+(on?' on':'')+'" onclick="obsRoseTog(\''+ASPECT8[i]+'\')"/><text x="'+lx+'" y="'+ly+'" class="rose-t'+(on?' on':'')+'">'+ASPECT8[i]+'</text>';}
  const presets='<div class="obs-chips rose-presets"><button onclick="obsRosePreset([\'N\',\'NE\',\'E\',\'SE\',\'S\',\'SW\',\'W\',\'NW\'])">Alle</button><button onclick="obsRosePreset([\'N\',\'NE\',\'NW\'])">Nord</button><button onclick="obsRosePreset([\'S\',\'SE\',\'SW\'])">Süd</button><button onclick="obsRosePreset([])">Keine</button></div>';
  return '<div class="obs-fld"><div class="obs-fld-l">'+label+'</div><div class="rose-wrap"><svg viewBox="0 0 184 184" class="rose-svg" aria-label="Expositionsrose">'+paths+'</svg></div>'+presets+'</div>';}
function obsRoseTog(a){const s=obsState.snow,i=s.aspects.indexOf(a);if(i>=0)s.aspects.splice(i,1);else s.aspects.push(a);haptic(5);obsRender();}
function obsRosePreset(set){obsState.snow.aspects=set.slice();haptic(6);obsRender();}
function obsFirnTime(){const v=obsState.snow.firnTime||'';return '<div class="obs-fld"><div class="obs-fld-l">Fahrbereit ab (Uhrzeit)</div><input type="time" class="obs-dt" value="'+v+'" oninput="obsState.snow.firnTime=this.value"></div>';}
function snowMeasure(sn){const l=snowKindLabel(sn.kind)||'Schnee';
  if(sn.kind==='powder'&&sn.depth)return sn.depth+' cm Pulver';
  if(sn.kind==='wind_powder'&&sn.depth)return 'Triebschnee '+sn.depth+' cm';
  return l;}
function obsCard(id,title,sum,inner){const open=obsOpenCards.has(id)?' open':'';return `<div class="obs-card${open}" id="obscard-${id}"><div class="obs-card-h" onclick="obsToggleCard('${id}')">${title}${sum?'<span class="obs-card-sum">'+sum+'</span>':''}<span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span></div><div class="obs-card-b">${inner}</div></div>`;}
function obsToggleCard(id){if(obsOpenCards.has(id))obsOpenCards.delete(id);else obsOpenCards.add(id);const el=document.getElementById('obscard-'+id);if(el)el.classList.toggle('open');}
function obsAvDetailsHTML(){const a=obsState.avalanche;
  const trigSum=a.triggerType!=='unknown'?(obsLbl(OBS_TRIGGER,a.triggerType)+(a.remoteTrigger?' · Fern':'')):'';
  const c1=obsCard('trigger','Auslösung',trigSum,
    '<div class="obs-fld"><div class="obs-chips">'+OBS_TRIGGER.map(x=>`<button class="${a.triggerType===x[0]?'active':''}" onclick="obsSetTrigger('${x[0]}')">${x[1]}</button>`).join('')+'</div></div>'+
    '<div class="obs-toggle">Fernauslösung<button class="obs-sw'+(a.remoteTrigger?' on':'')+'" onclick="obsToggleRemote(this)"><span></span></button></div>');
  const pers=a.caughtPersons.map((pp,ix)=>obsPersonHTML(pp,ix)).join('');
  const c2=obsCard('persons','Erfasste Personen',a.caughtPersons.length?(''+a.caughtPersons.length):'',
    pers+'<button class="obs-add-btn" onclick="obsAddPerson()">+ Person hinzufügen</button>');
  const ch=a.characteristics,sm=obsSizeMeta(ch.size);
  const sizeVis=ch.size!=='unknown'?`<div class="obs-size-vis"><div class="obs-size-obj">${sm.obj}</div><div class="obs-size-cons">${sm.c}</div></div>`:'';
  const c3=obsCard('chars','Lawineneigenschaften',ch.size!=='unknown'?sm.l:'',
    '<div class="obs-fld"><div class="obs-fld-l">Grösse</div>'+sizeVis+'<div class="obs-chips">'+OBS_SIZE.map(s=>`<button class="${ch.size===s.k?'active':''}" onclick="obsSetSize('${s.k}')">${s.l}</button>`).join('')+'</div></div>'+
    '<div class="obs-fld"><div class="obs-fld-l">Lawinentyp</div><div class="obs-chips">'+OBS_AVTYPE.map(x=>`<button class="${ch.avalancheType===x[0]?'active':''}" onclick="obsSetAv('avalancheType','${x[0]}')">${x[1]}</button>`).join('')+'</div></div>'+
    '<div class="obs-fld"><div class="obs-fld-l">Feuchtigkeit</div><div class="obs-chips">'+OBS_WET.map(x=>`<button class="${ch.wetness===x[0]?'active':''}" onclick="obsSetAv('wetness','${x[0]}')">${x[1]}</button>`).join('')+'</div></div>');
  return c1+c2+c3;}
function obsPersonHTML(pp,ix){return '<div class="obs-person"><button class="rm" onclick="obsRemovePerson('+ix+')">Entfernen</button><div class="obs-fld" style="margin-top:2px"><div class="obs-fld-l">Verschüttung</div><div class="obs-chips">'+OBS_BURIAL.map(x=>`<button class="${pp.burial===x[0]?'active':''}" onclick="obsSetPerson(${ix},'burial','${x[0]}')">${x[1]}</button>`).join('')+'</div></div><div class="obs-fld"><div class="obs-fld-l">Folge</div><div class="obs-chips">'+OBS_CONSEQ.map(x=>`<button class="${pp.consequence===x[0]?'active':''}" onclick="obsSetPerson(${ix},'consequence','${x[0]}')">${x[1]}</button>`).join('')+'</div></div></div>';}
function obsRerenderAv(){document.getElementById('obsBody').innerHTML=obsAvDetailsHTML();}
function obsSetTrigger(k){obsState.avalanche.triggerType=k;haptic(6);obsRerenderAv();}
function obsToggleRemote(el){obsState.avalanche.remoteTrigger=!obsState.avalanche.remoteTrigger;el.classList.toggle('on',obsState.avalanche.remoteTrigger);haptic(6);}
function obsAddPerson(){obsState.avalanche.caughtPersons.push({burial:'not_buried',consequence:'uninjured'});obsOpenCards.add('persons');obsRerenderAv();}
function obsRemovePerson(ix){obsState.avalanche.caughtPersons.splice(ix,1);obsRerenderAv();}
function obsSetPerson(ix,f,k){obsState.avalanche.caughtPersons[ix][f]=k;haptic(6);obsRerenderAv();}
function obsSetSize(k){obsState.avalanche.characteristics.size=k;obsState.avalanche.characteristics.sizeRank=obsSizeMeta(k).r;haptic(6);obsRerenderAv();}
function obsSetAv(f,k){obsState.avalanche.characteristics[f]=k;haptic(6);obsRerenderAv();}
function obsPad(n){return String(n).padStart(2,'0');}
function obsToLocalInput(d){return d.getFullYear()+'-'+obsPad(d.getMonth()+1)+'-'+obsPad(d.getDate())+'T'+obsPad(d.getHours())+':'+obsPad(d.getMinutes());}
function obsLocRowHTML(){const L=obsState.location;const srcTxt={exif:'Foto',device:'Gerät',manual:'Manuell'}[L.source]||'—';
  return '<span class="obs-loc-src">'+srcTxt+'</span><span>'+(L.lat!=null?(L.lat.toFixed(4)+', '+L.lon.toFixed(4)):'Kein Standort')+'</span>'+(L.elevation!=null?'<span>· '+L.elevation+' m</span>':'')+(L.aspect?'<span>· '+L.aspect+'</span>':'');}
function obsLocationHTML(){const dt=obsState.observedAt?obsToLocalInput(obsState.observedAt):'';
  const future=obsState.observedAt&&obsState.observedAt.getTime()>Date.now();
  const old=obsState.observedAt&&(Date.now()-obsState.observedAt.getTime())>7*864e5;
  let warn='';if(future)warn='<div class="obs-warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px;vertical-align:-2px"><path d="M10.3 3.2L1.8 18.5c-.8 1.4.2 3 1.7 3h17c1.5 0 2.5-1.6 1.7-3L13.7 3.2c-.8-1.4-2.6-1.4-3.4 0z"/><line x1="12" y1="9" x2="12" y2="14"/></svg> Zeitpunkt liegt in der Zukunft – bitte korrigieren.</div>';else if(old)warn='<div class="obs-warn">Beobachtung ist älter als 7 Tage.</div>';
  return '<div class="obs-loc-wrap"><div class="obs-loc-map" id="obsMap"></div></div><div class="obs-loc-row" id="obsLocRow">'+obsLocRowHTML()+'</div><input type="datetime-local" class="obs-dt" id="obsDt" value="'+dt+'" onchange="obsSetDateTime(this.value)"/>'+warn;}
function obsSetDateTime(v){if(v){obsState.observedAt=new Date(v);obsRender();}}
function obsInitMap(){const el=document.getElementById('obsMap');if(!el)return;if(obsMap){try{obsMap.remove();}catch(e){}obsMap=null;}
  const L0=obsState.location;const lat=L0.lat!=null?L0.lat:map.getCenter().lat,lon=L0.lon!=null?L0.lon:map.getCenter().lng;
  obsMap=L.map(el,{zoomControl:false,attributionControl:false}).setView([lat,lon],14);
  L.tileLayer('https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg').addTo(obsMap);
  // Fixed pin in the middle — the user pans the MAP underneath it until the
  // pin sits on the right spot (no marker dragging).
  if(!el.parentElement.querySelector('.obs-pin')){
    el.parentElement.insertAdjacentHTML('beforeend',
      '<div class="obs-pin"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="rgba(224,36,94,.14)"/><circle cx="12" cy="10" r="3"/></svg></div><div class="obs-pin-hint">Karte unter dem Pin verschieben</div>');}
  miniMapTools(obsMap,el.parentElement);
  function moved(){const c=obsMap.getCenter();obsApplyLoc(c.lat,c.lng,'manual');
    const r=document.getElementById('obsLocRow');if(r)r.innerHTML=obsLocRowHTML();
    const n=document.getElementById('obsNext');if(n)n.disabled=false;}
  obsMap.on('moveend',moved);
  setTimeout(()=>{try{obsMap.invalidateSize();}catch(e){}},120);}
function obsSummaryHTML(){const t=[];const ty=OBS_TYPE_LIST.find(x=>x.id===obsState.type);t.push('<span class="rp-tag">'+ty.label+'</span>');
  if(obsState.type==='avalanche'){const a=obsState.avalanche;if(a.characteristics.size!=='unknown')t.push('<span class="rp-tag">Grösse: '+obsSizeMeta(a.characteristics.size).l+'</span>');if(a.triggerType!=='unknown')t.push('<span class="rp-tag">'+obsLbl(OBS_TRIGGER,a.triggerType)+'</span>');if(a.caughtPersons.length)t.push('<span class="rp-tag">'+a.caughtPersons.length+' Pers.</span>');}
  if(obsState.whumpfFrequency)t.push('<span class="rp-tag">Wumm: '+obsEnumLabel('whumpfFrequency',obsState.whumpfFrequency)+'</span>');
  if(obsState.windSlab24h)t.push('<span class="rp-tag">Triebschnee: '+obsEnumLabel('windSlab24h',obsState.windSlab24h)+'</span>');
  if(obsState.type==='snow'){const sn=obsState.snow;if(sn.kind)t.push('<span class="rp-tag">'+snowKindLabel(sn.kind)+'</span>');
    if((sn.kind==='powder'||sn.kind==='wind_powder')&&sn.depth)t.push('<span class="rp-tag">'+sn.depth+' cm</span>');
    if(sn.kind==='powder'&&sn.powderline)t.push('<span class="rp-tag">ab '+sn.powderline+' m</span>');
    if(sn.kind==='melt_crust')t.push('<span class="rp-tag">'+sn.altLow+'–'+sn.altHigh+' m</span>');
    if(sn.kind==='wind_pressed'&&sn.alt)t.push('<span class="rp-tag">ab '+sn.alt+' m</span>');
    if(sn.aspects&&sn.aspects.length)t.push('<span class="rp-tag">'+sn.aspects.join(' ')+'</span>');
    if(sn.firnTime)t.push('<span class="rp-tag">ab '+sn.firnTime+'</span>');}
  if(obsState.media.length)t.push('<span class="rp-tag">'+ic('cam')+' '+obsState.media.length+'</span>');
  return '<div class="obs-summary">'+t.join('')+'</div>';}
function obsRemoveMedia(ix){obsState.media.splice(ix,1);obsRerenderMedia();}
function obsAddMedia(input){if(!input.files)return;const files=Array.prototype.slice.call(input.files);input.value='';
  files.forEach(f=>{const url=URL.createObjectURL(f);const m={file:f,url,type:f.type&&f.type.indexOf('video')===0?'video':'image',exif:null};obsState.media.push(m);
    obsReadExif(f).then(ex=>{m.exif=ex;let changed=false;
      if(ex&&ex.gps&&obsState.location.source!=='manual'){obsApplyLoc(ex.gps[0],ex.gps[1],'exif');changed=true;}
      if(ex&&ex.dt&&obsState.location.source!=='manual'){obsState.observedAt=ex.dt;changed=true;}
      const st=obsState.steps[obsState.step];
      if(changed&&(st.k==='submit'||st.final))obsRender();else obsRerenderMedia();
    });});
  obsRerenderMedia();}
// EXIF: GPS + DateTimeOriginal
function obsReadExif(file){return new Promise(res=>{const fr=new FileReader();
  fr.onload=function(e){try{const view=new DataView(e.target.result);if(view.getUint16(0)!==0xFFD8){res(null);return;}
    const len=view.byteLength;let off=2;
    while(off<len-4){const marker=view.getUint16(off);off+=2;
      if(marker===0xFFE1){if(view.getUint32(off+2)===0x45786966){res(obsParseExif(view,off+8));return;}off+=view.getUint16(off);}
      else if((marker&0xFF00)!==0xFF00){break;}else{off+=view.getUint16(off);}}
    res(null);}catch(err){res(null);}};
  fr.onerror=()=>res(null);fr.readAsArrayBuffer(file.slice(0,262144));});}
function obsParseExif(view,tiff){try{const little=view.getUint16(tiff)===0x4949;
  const u16=o=>view.getUint16(tiff+o,little),u32=o=>view.getUint32(tiff+o,little),u8=o=>view.getUint8(tiff+o);
  const ifd0=u32(4);let gpsPtr=0,exifPtr=0;const n0=u16(ifd0);
  for(let i=0;i<n0;i++){const en=ifd0+2+i*12,tag=u16(en);if(tag===0x8825)gpsPtr=u32(en+8);else if(tag===0x8769)exifPtr=u32(en+8);}
  let gps=null,dt=null;
  if(gpsPtr){const rat=o=>{const nu=u32(o),de=u32(o+4);return de?nu/de:0;};const dms=o=>rat(o)+rat(o+8)/60+rat(o+16)/3600;
    let latRef='N',lngRef='E',la=null,lo=null;const gn=u16(gpsPtr);
    for(let i=0;i<gn;i++){const en=gpsPtr+2+i*12,tag=u16(en),vo=u32(en+8);
      if(tag===1)latRef=String.fromCharCode(u8(en+8));else if(tag===2)la=dms(vo);else if(tag===3)lngRef=String.fromCharCode(u8(en+8));else if(tag===4)lo=dms(vo);}
    if(la!=null&&lo!=null){if(latRef==='S')la=-la;if(lngRef==='W')lo=-lo;if(Math.abs(la)<=90&&Math.abs(lo)<=180)gps=[la,lo];}}
  if(exifPtr){const en0=u16(exifPtr);for(let i=0;i<en0;i++){const en=exifPtr+2+i*12,tag=u16(en);if(tag===0x9003||tag===0x0132){const vo=u32(en+8);let s='';for(let j=0;j<19;j++){const ch=u8(vo+j);if(ch===0)break;s+=String.fromCharCode(ch);}const mt=s.match(/(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/);if(mt){dt=new Date(+mt[1],+mt[2]-1,+mt[3],+mt[4],+mt[5],+mt[6]);break;}}}}
  return(gps||dt)?{gps,dt}:null;
}catch(e){return null;}}
async function obsSubmit(){if(!sb||!sbUser||!obsState)return;const L=obsState.location;
  if(L.lat==null){alert('Bitte Standort auf der Karte setzen.');return;}
  if(obsState.observedAt&&obsState.observedAt.getTime()>Date.now()){alert('Zeitpunkt liegt in der Zukunft.');return;}
  const next=document.getElementById('obsNext');next.disabled=true;next.textContent='Melden…';
  try{const urls=[];
    for(const m of obsState.media){try{const up=await downscaleImage(m.file);const ext=(up.type==='image/jpeg')?'jpg':(m.file.name.split('.').pop()||'jpg').toLowerCase();const path=sbUser.id+'/'+Date.now()+'_'+Math.random().toString(36).slice(2,7)+'.'+ext;
      const{error}=await sb.storage.from('report-images').upload(path,up,{contentType:up.type||'image/jpeg'});
      if(!error){const{data}=sb.storage.from('report-images').getPublicUrl(path);if(data&&data.publicUrl)urls.push(data.publicUrl);}
      else if(urls.length===0&&obsState.media.length){/* keep going */}}catch(e){}}
    const cd=obsBuildCD();
    const row={user_id:sbUser.id,location:`POINT(${L.lon} ${L.lat})`,elevation_m:L.elevation||null,
      primary_categories:[obsState.type],subtype:obsSubLabel(),condition_data:cd,
      image_url:urls[0]||null,caption:(obsState.comment||'').trim()||null,
      captured_at:obsState.observedAt?obsState.observedAt.toISOString():null,completion_score:80};
    const{error}=await sb.from('reports').insert(row);if(error)throw error;
    if(obsMap){try{obsMap.remove();}catch(e){}obsMap=null;}obsState=null;
    document.getElementById('reportOverlay').style.display='none';loadDbReports();showUndo();
  }catch(e){alert('Fehler: '+(e.message||e));next.disabled=false;next.textContent='Melden';}}
function obsBuildCD(){const s=obsState;const cd={obsType:s.type,source:s.location.source,aspect:s.location.aspect||null,
  media:s.media.map(m=>({type:m.type,gps:(m.exif&&m.exif.gps)||null,takenAt:(m.exif&&m.exif.dt)?m.exif.dt.toISOString():null}))};
  if(s.type==='avalanche'){const a=s.avalanche;cd.avalanche={triggerType:a.triggerType,remoteTrigger:a.remoteTrigger,caughtPersons:a.caughtPersons,characteristics:{size:a.characteristics.size,sizeRank:a.characteristics.sizeRank,avalancheType:a.characteristics.avalancheType,wetness:a.characteristics.wetness}};if(a.characteristics.size!=='unknown')cd.measurement=obsSizeMeta(a.characteristics.size).l;}
  if(s.whumpfFrequency){cd.whumpfFrequency=s.whumpfFrequency;cd.measurement=obsEnumLabel('whumpfFrequency',s.whumpfFrequency);}
  if(s.windSlab24h){cd.windSlab24h=s.windSlab24h;if(!cd.measurement)cd.measurement=obsEnumLabel('windSlab24h',s.windSlab24h);}
  if(s.type==='snow'){cd.snow=s.snow;cd.measurement=snowMeasure(s.snow);}
  return cd;}
function obsSubLabel(){const s=obsState;if(s.type==='avalanche')return s.avalanche.characteristics.size!=='unknown'?('Lawine '+obsSizeMeta(s.avalanche.characteristics.size).l):'Lawine';if(s.type==='whumpf')return 'Wumm';if(s.type==='wind_slab')return 'Triebschnee';if(s.type==='snow')return snowKindLabel(s.snow.kind)||'Schnee';return 'Beobachtung';}
// --- Feed (Instagram-style full page) ---
let feedFilter='all',feedAnchor=null,feedScope='all',feedGroup=null;
const FEED_SCOPES=[
  {id:'all',label:'Entdecken',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="16.2 7.8 14 14 7.8 16.2 10 10"/></svg>'},
  {id:'following',label:'Folge ich',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>'},
  {id:'near',label:'Nähe',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg>'},
  {id:'map',label:'Kartenausschnitt',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="1 6 8 3 16 6 23 3 23 18 16 21 8 18 1 21 1 6"/><line x1="8" y1="3" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="21"/></svg>'},
  {id:'saved',label:'Gespeichert',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>'}
];
let savedPosts=new Set();try{savedPosts=new Set(JSON.parse(localStorage.getItem('ssm_saved')||'[]'));}catch(e){}
function toggleSave(id,ev){if(ev&&ev.stopPropagation)ev.stopPropagation();
  id=String(id);
  if(savedPosts.has(id)){savedPosts.delete(id);}else{savedPosts.add(id);try{haptic(6);}catch(e){}toast('Gespeichert — findest du unter „Gespeichert" im Feed.','ok');}
  try{localStorage.setItem('ssm_saved',JSON.stringify([...savedPosts]));}catch(e){}
  feedRender();}
function sharePost(id,ev){if(ev&&ev.stopPropagation)ev.stopPropagation();
  const r=allReports.find(x=>String(x.id)===String(id));
  const url=location.origin+location.pathname+'?post='+encodeURIComponent(id);
  const text=r?((r.user||'')+': '+(r.caption||r.sub||'Schnee-Report')):'Schnee-Report';
  try{haptic(6);}catch(e){}
  if(typeof nativeShare==='function'&&nativeShare(text,url))return;
  if(navigator.share){navigator.share({title:'Swiss Snow Model',text,url}).catch(()=>{});return;}
  try{navigator.clipboard.writeText(url).then(()=>toast('Link kopiert!','ok'));}catch(e){toast(url,'ok');}}
function feedImgTap(id,ev){ev.stopPropagation();
  const now=Date.now(),el=ev.currentTarget;
  if(el._t&&now-el._t<350){el._t=0;
    const h=document.createElement('div');h.className='dbl-heart';
    h.innerHTML='<svg viewBox="0 0 24 24" fill="#fff"><path d="M12 21s-7.5-4.9-10-9.2C.3 8.4 2 4.5 5.6 4.1 7.7 3.9 9.6 5 12 7.4 14.4 5 16.3 3.9 18.4 4.1 22 4.5 23.7 8.4 22 11.8 19.5 16.1 12 21 12 21z"/></svg>';
    el.appendChild(h);setTimeout(()=>h.remove(),750);
    const r=allReports.find(x=>String(x.id)===String(id));
    if(r&&r.dbRow&&!r.liked)toggleEndorse(id,ev);
    try{haptic(10);}catch(e){}
  }else{el._t=now;}}
function feedOpen(){
  try{localStorage.setItem('ssm_feed_seen',String(Date.now()));const fd=document.querySelector('#feedBtn .feed-dot');if(fd)fd.classList.remove('on');}catch(e){}
  const fp=document.getElementById('feedPage');fp.classList.add('open');
  document.getElementById('feedScope').innerHTML=FEED_SCOPES.map(s=>
    `<button data-s="${s.id}" class="${feedScope===s.id?'active':''}" onclick="feedSetScope('${s.id}')">${s.icon}${s.label}</button>`).join('');
  document.getElementById('feedFilter').innerHTML=['all','avalanche','whumpf','wind_slab','other'].map(f=>{
    const lbl=f==='all'?'Alle':(catSvg(f,14)+' '+catLabel(f));
    return`<button class="${feedFilter===f?'active':''}" onclick="feedSetFilter('${f}')">${lbl}</button>`;}).join('');
  document.getElementById('feedLoc').style.display=feedScope==='near'?'flex':'none';
  feedRender();
}
function feedClose(){document.getElementById('feedPage').classList.remove('open');}
function feedCreatePost(){if(!sb||!sbUser){authShow();return;}feedClose();setTimeout(()=>{obsOpen();},370);}
// --- Comments ---
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
let cmtReportId=null;
// --- Per-post condition check (crowdsourced snow quality) ---
let condReportId=null,condStars=0,condPowder=null;
function openCondition(id,ev){if(ev)ev.stopPropagation();
  if(!sb||!sbUser){authShow();return;}
  condReportId=id;const r=allReports.find(x=>x.id===id);const mine=r&&r.myCond;
  condStars=mine?mine.stars:0;condPowder=mine?mine.powder:null;
  condRenderStars();condRenderPowder();var sv=document.getElementById('condSaveBtn');if(sv)sv.disabled=!condStars;
  document.getElementById('condModal').style.display='flex';
  try{localStorage.setItem('ssm_cond_hint','1');}catch(e){}var h=document.getElementById('condHint');if(h)h.remove();}
function condClose(){document.getElementById('condModal').style.display='none';condReportId=null;}
function condSetStars(n){condStars=n;condRenderStars();var sv=document.getElementById('condSaveBtn');if(sv)sv.disabled=!condStars;haptic(5);}
function condSetPowder(v){condPowder=v;condRenderPowder();haptic(5);}
function condRenderStars(){const el=document.getElementById('condStars');if(!el)return;let h='';
  for(let i=1;i<=5;i++)h+='<button class="'+(i<=condStars?'on':'')+'" onclick="condSetStars('+i+')" aria-label="'+i+' Sterne"><svg viewBox="0 0 24 24"><path d="M12 2l3 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.9 21l1.2-6.8-5-4.9 6.9-1z"/></svg></button>';
  el.innerHTML=h;}
function condRenderPowder(){var y=document.getElementById('condPowY'),n=document.getElementById('condPowN');if(y)y.classList.toggle('on',condPowder===true);if(n)n.classList.toggle('on',condPowder===false);}
async function condSave(){if(!sb||!sbUser||!condReportId||!condStars)return;
  const id=condReportId,pw=condPowder===true;
  try{const{error}=await sb.from('report_conditions').upsert({report_id:id,user_id:sbUser.id,stars:condStars,powder:pw},{onConflict:'report_id,user_id'});
    if(error)throw error;
    const r=allReports.find(x=>x.id===id);
    if(r){const had=r.myCond,sum=(r.condAvg||0)*(r.condN||0);let n=r.condN||0,ns=sum,np=r.condPow||0;
      if(had){ns+=condStars-(had.stars||0);if(had.powder&&!pw)np--;if(!had.powder&&pw)np++;}
      else{n++;ns+=condStars;if(pw)np++;}
      r.condN=n;r.condAvg=n?ns/n:0;r.condPow=np;r.myCond={stars:condStars,powder:pw};}
    feedRender();toast('Danke fürs Bewerten!','ok');haptic(10);condClose();
  }catch(e){toast('Speichern fehlgeschlagen: '+(e.message||e),'err');}}
function condMaybeHint(){try{if(localStorage.getItem('ssm_cond_hint'))return;}catch(e){}
  if(document.getElementById('condHint'))return;
  const btn=document.querySelector('#feedList .cond-btn');if(!btn)return;
  const rect=btn.getBoundingClientRect();if(!rect.width)return;
  const tip=document.createElement('div');tip.id='condHint';tip.className='cond-hint';
  tip.innerHTML='<b>Neu:</b> Tippe hier, um kurz die Schnee-Bedingungen zu bewerten (★ &amp; Powder) — so finden alle die besten Hänge.';
  document.body.appendChild(tip);
  const below=rect.top<150;tip.classList.toggle('below',below);
  tip.style.left=Math.max(10,Math.min(rect.left+rect.width/2-tip.offsetWidth/2,window.innerWidth-tip.offsetWidth-10))+'px';
  tip.style.top=(below?rect.bottom+11:rect.top-tip.offsetHeight-11)+'px';
  try{localStorage.setItem('ssm_cond_hint','1');}catch(e){}
  const dismiss=()=>{if(tip.parentNode)tip.remove();};tip.onclick=dismiss;setTimeout(dismiss,8000);}
// --- Quick-Check: a 5-second standalone report (stars + powder + location) ---
let qrStars=0,qrPowder=null,qrLL=null,qrAmount=null,qrQuality=null,qrPhoto=null,qrMapObj=null;
function qrOpen(ev){if(ev&&ev.stopPropagation)ev.stopPropagation();
  if(!sb||!sbUser){authShow();return;}
  qrStars=0;qrPowder=null;qrLL=null;qrAmount=null;qrQuality=null;
  qrPhoto=null;
  document.getElementById('qrModal').style.display='flex';
  document.getElementById('qrStep0').style.display='';
  document.getElementById('qrStep1').style.display='none';
  document.getElementById('qrStep2').style.display='none';
  const pv=document.getElementById('qrPrev');if(pv){pv.style.display='none';pv.src='';}
  if(qrMapObj){try{qrMapObj.remove();}catch(e){}qrMapObj=null;}
  setTimeout(qrInitMap,60);
  const dot=document.getElementById('qrPadDot');if(dot)dot.style.display='none';
  const rd=document.getElementById('qrRead');if(rd)rd.textContent='Tippe oder ziehe auf dem Feld';
  qrPadAttach();qrSync();
  const lc=document.getElementById('qrLocTxt');lc.textContent='Standort wird ermittelt…';
  const fallback=()=>{try{const c=map.getCenter();qrLL=[c.lat,c.lng];lc.textContent='Kartenmitte · '+c.lat.toFixed(3)+'°N '+c.lng.toFixed(3)+'°E';}catch(e){lc.textContent='Standort unbekannt';}};
  if(navigator.geolocation){navigator.geolocation.getCurrentPosition(p=>{
    qrLL=[p.coords.latitude,p.coords.longitude];const d=obsDEM(qrLL[0],qrLL[1]);
    lc.textContent='Dein Standort'+(d.elev?(' · '+d.elev+' m'+(d.aspect?(' · '+d.aspect):'')):'');
  },fallback,{enableHighAccuracy:true,timeout:8000,maximumAge:60000});}else fallback();
  haptic(8);}
function qrClose(){document.getElementById('qrModal').style.display='none';
  if(qrMapObj){try{qrMapObj.remove();}catch(e){}qrMapObj=null;}}
function qrToStep(n){
  if(n===2&&(qrAmount===null||qrQuality===null))return;
  ['qrStep0','qrStep1','qrStep2'].forEach((id,ix)=>{const el=document.getElementById(id);
    el.style.display=ix===n?'':'none';if(ix===n){el.style.animation='none';void el.offsetWidth;el.style.animation='';}});
  haptic(6);}
function qrPhotoPick(inp){if(!inp.files||!inp.files[0])return;qrPhoto=inp.files[0];inp.value='';
  const pv=document.getElementById('qrPrev');pv.src=URL.createObjectURL(qrPhoto);pv.style.display='';haptic(6);}
function qrSkip(){qrPhoto=null;qrSubmit();}
function qrInitMap(){
  if(!qrMapObj){
    const c=qrLL||[map.getCenter().lat,map.getCenter().lng];
    qrMapObj=L.map('qrMap',{zoomControl:false,attributionControl:false}).setView(c,13);
    L.tileLayer('https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg').addTo(qrMapObj);
    const wrap=document.getElementById('qrMap').parentElement;
    if(!wrap.querySelector('.obs-pin'))wrap.insertAdjacentHTML('beforeend','<div class="obs-pin"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="rgba(224,36,94,.14)"/><circle cx="12" cy="10" r="3"/></svg></div>');
    miniMapTools(qrMapObj,wrap);
    qrMapObj.on('moveend',()=>{const cc=qrMapObj.getCenter();qrLL=[cc.lat,cc.lng];
      const d=obsDEM(cc.lat,cc.lng);
      document.getElementById('qrLocTxt').textContent='Manuell gesetzt'+(d.elev?(' · '+d.elev+' m'):'');});
    setTimeout(()=>{try{qrMapObj.invalidateSize();}catch(e){}},120);}}
function qrSync(){const b2=document.getElementById('qrNextBtn');if(b2)b2.disabled=!(qrAmount!==null&&qrQuality!==null);}
function qrQuadLabel(){const q=qrQuality==null?0:qrQuality;
  if(q>=75)return 'Pulver fluffy';
  if(q>=50)return 'Pulver';
  if(q>=25)return 'Gealterter Pulver';
  return 'Kompakt / gedeckelt';}
function qrPadAttach(){const pad=document.getElementById('qrPad');if(!pad||pad._wired)return;pad._wired=true;
  pad.style.touchAction='none';
  function setFrom(e){const r=pad.getBoundingClientRect();
    const fx=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
    const fy=Math.max(0,Math.min(1,(e.clientY-r.top)/r.height));
    qrQuality=Math.round(fx*100);              // X: Qualität (links kompakt → rechts fluffy)
    qrAmount=Math.round((1-fy)*60/5)*5;        // Y: Menge in 5-cm-Schritten (0–60)
    const dot=document.getElementById('qrPadDot');
    dot.style.display='block';dot.style.left=(fx*100)+'%';dot.style.top=(100-qrAmount/60*100)+'%';
    dot.classList.toggle('below',qrAmount>50);
    const cmEl=document.getElementById('qrDotCm');if(cmEl)cmEl.textContent=qrAmount+' cm';
    document.getElementById('qrRead').textContent=qrQuadLabel()+' · '+qrAmount+' cm';
    qrSync();}
  let on=false;
  pad.addEventListener('pointerdown',e=>{on=true;try{pad.setPointerCapture(e.pointerId);}catch(_){}setFrom(e);haptic(5);});
  pad.addEventListener('pointermove',e=>{if(on)setFrom(e);});
  const fin=()=>{on=false;};
  pad.addEventListener('pointerup',fin);pad.addEventListener('pointercancel',fin);}
async function qrSubmit(){if(!sb||!sbUser||qrAmount===null||qrQuality===null)return;
  const btn=document.getElementById('qrSaveBtn');btn.disabled=true;btn.textContent='Postet…';
  try{
    let ll=qrLL;if(!ll){try{const c=map.getCenter();ll=[c.lat,c.lng];}catch(e){ll=[46.8,8.2];}}
    const d=obsDEM(ll[0],ll[1]);
    let imageUrl=null;
    if(qrPhoto){const up=await downscaleImage(qrPhoto);
      const path=sbUser.id+'/'+Date.now()+'.jpg';
      const{error:upErr}=await sb.storage.from('report-images').upload(path,up,{contentType:up.type||'image/jpeg'});
      if(!upErr){const{data:ud}=sb.storage.from('report-images').getPublicUrl(path);imageUrl=ud?.publicUrl||null;}}
    const row={user_id:sbUser.id,location:'POINT('+ll[1]+' '+ll[0]+')',elevation_m:d.elev||null,image_url:imageUrl,
      primary_categories:['snow'],subtype:'Quick Powder Report',
      condition_data:{quick:true,powderAmountCm:qrAmount,powderQuality:qrQuality,
        stars:Math.max(1,Math.round(qrQuality/20)),powder:(qrAmount>=30&&qrQuality>=50),
        measurement:qrQuadLabel()+' · '+qrAmount+' cm'},
      caption:null,completion_score:40,captured_at:new Date().toISOString()};
    const{error}=await sb.from('reports').insert(row);if(error)throw error;
    toast('Powder-Report gepostet — danke!','ok');haptic(12);qrClose();loadDbReports();
  }catch(e){toast('Posten fehlgeschlagen: '+(e.message||e),'err');}
  btn.disabled=false;btn.textContent='Posten';}
const CMT_SKELETON='<div class="cmt-row"><div class="cmt-av skel"></div><div class="cmt-b" style="flex:1"><div class="skel" style="height:12px;width:38%;margin-bottom:7px"></div><div class="skel" style="height:11px;width:85%;margin-bottom:5px"></div><div class="skel" style="height:11px;width:55%"></div></div></div>'.repeat(3);
function openComments(id,ev){if(ev)ev.stopPropagation();cmtReportId=id;
  document.getElementById('cmtModal').style.display='flex';
  document.getElementById('cmtList').innerHTML=CMT_SKELETON;
  document.getElementById('cmtInput').value='';loadComments(id);}
function commentsClose(){document.getElementById('cmtModal').style.display='none';cmtReportId=null;}
async function loadComments(id){
  if(!sb){document.getElementById('cmtList').innerHTML='<div class="cmt-empty">Nicht verfügbar.</div>';return;}
  try{
    const{data}=await sb.from('report_comments').select('*').eq('report_id',id).order('created_at',{ascending:true});
    let names={};const uids=[...new Set((data||[]).map(c=>c.user_id).filter(Boolean))];
    if(uids.length){try{const{data:pr}=await sb.from('profiles').select('id,username').in('id',uids);(pr||[]).forEach(p=>names[p.id]=p.username);}catch(e){}}
    const list=document.getElementById('cmtList');
    if(!data||!data.length){list.innerHTML='<div class="cmt-empty">Noch keine Kommentare. Sei der Erste!</div>';return;}
    list.innerHTML=data.map(c=>{const nm=names[c.user_id]||(c.user_id?c.user_id.substring(0,8):'User');
      return`<div class="cmt-row"><div class="cmt-av">${nm[0].toUpperCase()}</div><div class="cmt-b"><span class="cmt-u">${escapeHtml(nm)}</span> <span class="cmt-t">${escapeHtml(c.body)}</span><div class="cmt-time">${timeAgo(c.created_at)}</div></div></div>`;}).join('');
    list.scrollTop=list.scrollHeight;
  }catch(e){document.getElementById('cmtList').innerHTML='<div class="cmt-empty">Fehler beim Laden.</div>';}
}
async function addComment(){
  if(!sb||!sbUser){authShow();return;}
  const inp=document.getElementById('cmtInput');const body=(inp.value||'').trim();if(!body||!cmtReportId)return;
  inp.value='';
  try{
    const{error}=await sb.from('report_comments').insert({report_id:cmtReportId,user_id:sbUser.id,body});
    if(error)throw error;
    const r=allReports.find(x=>x.id===cmtReportId);if(r)r.comments=(r.comments||0)+1;
    loadComments(cmtReportId);feedRender();
  }catch(e){alert('Fehler: '+(e.message||e));inp.value=body;}
}
function feedSetScope(s){feedScope=s;
  document.querySelectorAll('#feedScope button').forEach(b=>b.classList.toggle('active',b.dataset.s===s));
  document.getElementById('feedLoc').style.display=s==='near'?'flex':'none';
  // Nähe: immediately use the current device location
  if(s==='near'&&(!feedAnchor||feedAnchor.src!=='me')){const nb=document.getElementById('feedNear');if(nb)nb.click();}
  feedRender();haptic(5);}
function feedSetFilter(f){feedFilter=f;document.querySelectorAll('.feed-filter button').forEach((b,i)=>{b.classList.toggle('active',['all','avalanche','whumpf','wind_slab','other'][i]===f);});feedRender();}
function feedSetAnchor(a){feedAnchor=a;
  document.getElementById('feedNear').classList.toggle('active',!!a&&a.src==='me');
  document.getElementById('feedPeakBtn').classList.toggle('active',!!a&&a.src==='peak');
  document.getElementById('feedDestBtn').classList.toggle('active',!!a&&a.src==='dest');
  document.getElementById('feedPeakBtn').innerHTML=ic('peak')+' '+escapeHtml((a&&a.src==='peak')?a.name:'Gipfel');
  document.getElementById('feedDestBtn').innerHTML=ic('ski')+' '+escapeHtml((a&&a.src==='dest')?a.name:'Gebiet');
  const bar=document.getElementById('feedAnchorBar'),clr=document.getElementById('feedAnchorClear');
  if(a){bar.style.display='';bar.innerHTML='Sortiert nach Nähe zu <b>'+a.name+'</b>';clr.style.display='';}
  else{bar.style.display='none';clr.style.display='none';}
  feedRender();}
function feedClearAnchor(){feedSetAnchor(null);}
(function(){
  const near=document.getElementById('feedNear');if(!near)return;
  const nearHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg> In der Nähe';
  near.onclick=()=>{if(!navigator.geolocation){alert('Kein GPS verfügbar');return;}
    near.innerHTML='… GPS';near.disabled=true;
    navigator.geolocation.getCurrentPosition(p=>{near.disabled=false;near.innerHTML=nearHTML;
      const acc=p.coords.accuracy;
      feedSetAnchor({name:acc&&acc>3000?'meiner (ungenauen) Position':'meiner Position',lat:p.coords.latitude,lng:p.coords.longitude,src:'me'});},
    err=>{near.disabled=false;near.innerHTML=nearHTML;alert(err.code===1?'Standortzugriff wurde blockiert. Bitte in den Browser-Einstellungen erlauben.':'Standort nicht verfügbar.');},
    {enableHighAccuracy:true,timeout:15000,maximumAge:0});};
  document.getElementById('feedAnchorClear').onclick=feedClearAnchor;
})();
// --- Location search picker (Gipfel / Gebiet) ---
let locPickerMode='peak';
function openLocPicker(mode){locPickerMode=mode;
  const inp=document.getElementById('locPickerInput');inp.value='';
  inp.placeholder=mode==='peak'?'Gipfel suchen…':'Gebiet suchen…';
  document.getElementById('locPicker').style.display='flex';locPickerFilter('');
  setTimeout(()=>inp.focus(),60);}
function locPickerClose(){document.getElementById('locPicker').style.display='none';}
function locPickerFilter(q){const list=(locPickerMode==='peak'?PEAKS:DESTS);q=(q||'').toLowerCase().trim();
  const items=list.map((p,i)=>({p,i})).filter(o=>!q||o.p.n.toLowerCase().includes(q)).sort((x,y)=>x.p.n.localeCompare(y.p.n));
  const el=document.getElementById('locPickerList');
  el.innerHTML=items.length?items.map(o=>`<button onclick="locPickerPick(${o.i})"><span>${o.p.n}</span>${o.p.e?'<span class="lp-e">'+o.p.e+' m</span>':''}</button>`).join(''):'<div class="lp-empty">Nichts gefunden</div>';}
function locPickerPick(i){const p=(locPickerMode==='peak'?PEAKS:DESTS)[i];
  feedSetAnchor({name:p.n,lat:p.lat,lng:p.lng,src:locPickerMode});locPickerClose();haptic(6);}
function feedRender(){
  const list=document.getElementById('feedList');
  let base=allReports.slice();
  if(feedScope==='following'){
    if(!sbUser){list.innerHTML='<div class="feed-empty">Melde dich an, um Leuten zu folgen und ihre Reports hier zu sehen.</div>';return;}
    base=base.filter(r=>r.dbRow&&r.userId&&myFollowing.has(r.userId));
    if(!base.length){list.innerHTML='<div class="feed-empty">Du folgst noch niemandem. Tippe bei einem Report auf „Folgen".</div>';return;}
  }else if(feedScope==='map'){
    const bnds=map.getBounds();base=base.filter(r=>bnds.contains([r.lat,r.lng]));
    if(!base.length){list.innerHTML='<div class="feed-empty">Keine Reports im aktuellen Kartenausschnitt. Zoome heraus oder verschiebe die Karte.</div>';return;}
  }else if(feedScope==='saved'){
    base=base.filter(r=>savedPosts.has(String(r.id)));
    if(!base.length){list.innerHTML='<div class="feed-empty">Noch nichts gespeichert. Tippe bei einem Report auf das Lesezeichen.</div>';return;}
  }
  let filtered=feedFilter==='all'?base:base.filter(r=>r.cat===feedFilter);
  if(feedAnchor){filtered=filtered.map(r=>({r,km:haversineKm(feedAnchor.lat,feedAnchor.lng,r.lat,r.lng)})).sort((a,b)=>a.km-b.km).map(o=>{o.r._km=o.km;return o.r;});}
  if(!filtered.length){list.innerHTML='<div class="feed-empty">Noch keine Reports in dieser Kategorie.</div>';return;}
  list.innerHTML=filtered.map(r=>{
    const col=CAT_COLORS[r.cat]||'#666';
    const bg=CAT_BG[r.cat]||'linear-gradient(135deg,#f0f0f0,#e0e0e0)';
    const avatarBg=r.cat==='danger'?'linear-gradient(135deg,#d03050,#ff5470)':r.cat==='snow'?'linear-gradient(135deg,#5e5ce6,#42a5f5)':r.cat==='route'?'linear-gradient(135deg,#2e7d32,#66bb6a)':r.cat==='tour'?'linear-gradient(135deg,#7b1fa2,#ab47bc)':'linear-gradient(135deg,#e65100,#ff9800)';
    const distTag=(feedAnchor&&r._km!=null)?`<span class="feed-card-dist">${r._km<1?Math.round(r._km*1000)+' m':r._km.toFixed(r._km<10?1:0)+' km'}</span>`:'';
    const canFollow=r.dbRow&&r.userId&&(!sbUser||r.userId!==sbUser.id);
    const followBtn=canFollow?`<button class="feed-follow ${myFollowing.has(r.userId)?'following':''}" onclick="toggleFollow('${r.userId}',event)">${myFollowing.has(r.userId)?'Folge ich':'Folgen'}</button>`:'';
    const endBtn=r.dbRow?`<button class="endorse-btn ${r.liked?'endorsed':''}" onclick="toggleEndorse('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg> ${r.likes||0}<span class="endorse-lbl">${r.liked?'Bestätigt':'Bestätigen'}</span></button>`:'';
    return`<div class="feed-card${r.img?' photo':' text'}" id="feedcard-${r.id}" onclick="feedFlyTo(${r.lat},${r.lng})">
      <div class="feed-card-head">
        <div class="feed-card-avatar" style="${r.avatar?`background-image:url(${r.avatar});background-size:cover;background-position:center`:`background:${avatarBg}`}"${r.userId?` onclick="event.stopPropagation();viewUser('${r.userId}','${(r.user||'').replace(/['\"<>]/g,'')}')"`:''}>${r.avatar?'':escapeHtml((r.user||'U')[0].toUpperCase())}</div>
        <div class="feed-card-info">
          <span class="feed-card-user"${r.userId?` onclick="event.stopPropagation();viewUser('${r.userId}','${(r.user||'').replace(/['"<>]/g,'')}')"`:''}>${escapeHtml(r.user)}</span>
          <span class="feed-card-loc"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg> ${r.peak?escapeHtml(r.peak):(r.lat.toFixed(2)+'°N, '+r.lng.toFixed(2)+'°E')}</span>
        </div>
        ${followBtn||distTag||`<span class="feed-card-time">${r.time}</span>`}
      </div>
      ${r.img?`<div class="feed-card-visual" onclick="feedImgTap('${r.id}',event)"><img src="${r.img}" alt="" loading="lazy" decoding="async"/></div>`:
        (r.caption?`<div class="feed-tx" style="background:linear-gradient(150deg,${col}14,rgba(255,255,255,0) 75%)"><span class="feed-tx-bar" style="background:${col}"></span>${escapeHtml(r.caption)}</div>`:'')}
      <div class="feed-card-body">
        <div class="feed-card-badges">
          <span class="feed-badge cat-${r.cat}">${catSvg(r.cat,14)} ${escapeHtml(r.sub||r.cat)}</span>
          ${r.measurement?`<span class="feed-badge cat-${r.cat}">${escapeHtml(r.measurement)}</span>`:''}
          ${r.stars?`<span class="feed-badge cat-${r.cat}"><svg class="ic-i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2l3 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.9 21l1.2-6.8-5-4.9 6.9-1z" fill="#f5a623" stroke="none"/></svg> ${r.stars}/5</span>`:''}
          ${r.condN?`<span class="cond-chip" title="${r.condN} Bewertung(en)"><svg viewBox="0 0 24 24"><path d="M12 2l3 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.9 21l1.2-6.8-5-4.9 6.9-1z"/></svg>${r.condAvg.toFixed(1)}${r.condPow?` · ❄${r.condPow}`:''}</span>`:''}
        </div>
        ${r.img&&r.caption?`<div class="feed-card-caption"><b>${escapeHtml(r.user)}</b> ${escapeHtml(r.caption)}</div>`:''}
      </div>
      <div class="feed-card-actions">
        ${endBtn}
        ${r.dbRow?`<button onclick="openComments('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.8-.8L3 21l1.9-5.2A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/></svg> ${r.comments||0}</button>`:''}
        ${r.dbRow?`<button class="cond-btn${r.myCond?' rated':''}" title="Bedingungen bewerten" aria-label="Bedingungen bewerten" onclick="openCondition('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/><circle cx="12" cy="12" r="3"/></svg></button>`:''}
        <button onclick="event.stopPropagation();feedFlyTo(${r.lat},${r.lng})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg> Karte</button>
        <button title="Teilen" aria-label="Teilen" onclick="sharePost('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></button>
        <button class="save-btn${savedPosts.has(String(r.id))?' saved':''}" title="Merken" aria-label="Merken" onclick="toggleSave('${r.id}',event)"><svg viewBox="0 0 24 24" fill="${savedPosts.has(String(r.id))?'currentColor':'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg></button>
        ${r.dbRow&&(!sbUser||r.userId!==sbUser.id)?`<button class="flag-btn ${r.flaggedByMe?'flagged':''}" title="Melden" aria-label="Report melden" onclick="reportFlag('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg></button>`:''}
      </div>
    </div>`;
  }).join('');
  feedAnimateCards();
  condMaybeHint();
}
// First-appearance card entrance (skips re-animating on like/flag re-renders).
const feedSeen=new Set();
let _feedIO=null;
function feedAnimateCards(){
  if(window.matchMedia&&window.matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  if(!('IntersectionObserver'in window))return;
  if(!_feedIO){_feedIO=new IntersectionObserver((ents,obs)=>{
    ents.forEach(e=>{if(e.isIntersecting){e.target.classList.add('enter');obs.unobserve(e.target);}});
  },{root:document.querySelector('.feed-scroll'),threshold:.05});}
  document.querySelectorAll('#feedList .feed-card').forEach(c=>{
    const id=c.id.replace('feedcard-','');
    if(feedSeen.has(id))return;feedSeen.add(id);_feedIO.observe(c);
  });
}
function feedFlyTo(lat,lng){feedClose();setTimeout(()=>map.flyTo([lat,lng],14,{duration:1.2}),350);}
/*__APP_END__*/</script></body></html>
"""
