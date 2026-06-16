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
from PIL import Image
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


def export_interactive_html(data, out_html: Path) -> Path:
    def u8(a, fn):
        return base64.b64encode(np.clip(fn(a), 0, 255).astype("uint8").tobytes()).decode()
    snow = u8(data["snow"], lambda a: np.round(a / _SNOW_SCALE))
    temp = u8(data["temp"], lambda a: np.round((a + _TEMP_OFF) * _TEMP_MUL))
    sun = u8(data["sun"], lambda a: np.round(a * _SUN_MUL))
    spd = u8(data["wind"]["spd"], lambda a: np.round(a * _SPD_MUL))
    wdir = u8(data["wind"]["dir"], lambda a: np.round(a / _DIR_DIV))
    windg = u8(data["wind_grid"], lambda a: np.round(np.clip(a, 0, 51) * _SPD_MUL))
    prec = u8(data["prec"], lambda a: np.round(a * _PREC_MUL))
    maspect = u8(data["main_aspect"], lambda a: np.round(a / _DIR_DIV))
    mslope = u8(data["main_slope"], lambda a: np.round(np.clip(a, 0, 90)))
    melev = u8(data["main_elev"], lambda a: np.round(np.clip(a / _ELEV_SCALE, 0, 255)))
    roughg = u8(data["rough_grid"], lambda a: np.round(np.clip(a / _ROUGH_GRID_SCALE, 0, 255)))
    rad = data["rad"]
    rslope = u8(rad["slope"], lambda a: np.round(np.clip(a, 0, 90)))
    raspect = u8(rad["aspect"], lambda a: np.round(a / _DIR_DIV))
    rhor = u8(rad["horizon"], lambda a: np.round(np.clip(a, 0, 90)))

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
    html = _HTML.replace("/*META*/", json.dumps(meta))
    for tok, blob in [("__SNOW__", snow), ("__TEMP__", temp), ("__SUN__", sun),
                      ("__SPD__", spd), ("__DIR__", wdir), ("__WINDG__", windg),
                      ("__RSLOPE__", rslope), ("__RASPECT__", raspect), ("__RHOR__", rhor),
                      ("__PREC__", prec), ("__MASPECT__", maspect), ("__MSLOPE__", mslope),
                      ("__MELEV__", melev),
                      ("__ROUGHGRID__", roughg),
                      ("__ASPECTPNG__", data["aspect_png"]), ("__ROUGHPNG__", data["rough_png"])]:
        html = html.replace(f'"{tok}"', json.dumps(blob))
    out_html.write_text(html, encoding="utf-8")
    return out_html


_HTML = r"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1, user-scalable=no"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="theme-color" content="#0a0e1a"/>
<meta name="mobile-web-app-capable" content="yes"/>
<title>Swiss Snow Model</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/maplibre-gl@4.1.2/dist/maplibre-gl.js"></script>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.1.2/dist/maplibre-gl.css"/>
<style>
 :root{--fg:#e8ecf1;--fg2:#c0c8d4;--mut:#8694a6;--acc:#5b9cf5;--acc2:#3d7de0;--bd:rgba(255,255,255,.12);--glass:rgba(15,20,35,.72);--glass2:rgba(15,20,35,.85);--glow:rgba(91,156,245,.15);--panel-h:52px}
 *{box-sizing:border-box}
 html,body{margin:0;height:100%;overflow:hidden;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:var(--fg);-webkit-tap-highlight-color:transparent;overscroll-behavior:none}
 #map{position:absolute;inset:0}
 #flow{position:absolute;inset:0;z-index:450;pointer-events:none}
 #layerBar{position:absolute;z-index:1000;top:0;left:0;right:0;display:flex;gap:5px;padding:8px 12px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;background:linear-gradient(180deg,rgba(10,14,26,.7) 0%,transparent 100%)}
 #layerBar::-webkit-scrollbar{display:none}
 #layerBar button{border:1px solid var(--bd);background:rgba(255,255,255,.08);border-radius:10px;padding:6px 12px;cursor:pointer;font-size:12px;min-height:34px;color:var(--fg2);transition:.15s;backdrop-filter:blur(6px);flex-shrink:0;white-space:nowrap}
 #layerBar button:hover{border-color:var(--acc);background:rgba(255,255,255,.14)}
 #layerBar button.active{background:var(--acc2);color:#fff;border-color:var(--acc);font-weight:600;box-shadow:0 0 10px var(--glow)}
 #statBar{position:absolute;z-index:999;top:42px;left:0;right:0;display:none;gap:5px;padding:4px 12px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;background:linear-gradient(180deg,rgba(10,14,26,.55) 0%,transparent 100%);align-items:center}
 #statBar::-webkit-scrollbar{display:none}
 #statBar .cap{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);flex-shrink:0}
 #statBar button{border:1px solid var(--bd);background:rgba(255,255,255,.08);border-radius:8px;padding:4px 10px;cursor:pointer;font-size:11px;min-height:28px;color:var(--fg2);transition:.15s;backdrop-filter:blur(6px);flex-shrink:0;white-space:nowrap}
 #statBar button:hover{border-color:var(--acc);background:rgba(255,255,255,.14)}
 #statBar button.active{background:var(--acc2);color:#fff;border-color:var(--acc);font-weight:600}
 #bottomPanel{position:absolute;z-index:1000;bottom:0;left:0;right:0;
   background:var(--glass);backdrop-filter:blur(18px) saturate(1.4);-webkit-backdrop-filter:blur(18px) saturate(1.4);border-top:1px solid var(--bd);box-shadow:0 -4px 24px rgba(0,0,0,.4);transition:max-height .3s ease;padding-bottom:env(safe-area-inset-bottom,0px)}
 #bottomPanel .drag{width:36px;height:4px;border-radius:2px;background:rgba(255,255,255,.25);margin:6px auto 4px}
 #btmMain{padding:4px 12px 8px}
 #btmExtra{padding:0 12px 12px;display:none;max-height:40vh;overflow-y:auto;-webkit-overflow-scrolling:touch}
 #bottomPanel.expanded #btmExtra{display:block}
 #timeline{display:block;border:1px solid var(--bd);background:rgba(15,20,35,.5);border-radius:6px}
 .winlbl{font-size:12px;margin-top:4px;font-weight:600;color:var(--fg2)}
 .seg{display:flex;flex-wrap:wrap;gap:6px}
 .seg button{border:1px solid var(--bd);background:rgba(255,255,255,.07);border-radius:10px;padding:8px 12px;cursor:pointer;font-size:13px;min-height:38px;color:var(--fg2);transition:.15s;backdrop-filter:blur(4px)}
 .seg button:hover{border-color:var(--acc);background:rgba(255,255,255,.12)}
 .seg button.active{background:var(--acc2);color:#fff;border-color:var(--acc);font-weight:600;box-shadow:0 0 12px var(--glow)}
 .sec{margin-top:12px}
 .cap{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);margin-bottom:6px}
 .ck{display:flex;align-items:center;gap:9px;margin-top:12px;font-size:13px;cursor:pointer;color:var(--fg2)}
 .ck input{width:18px;height:18px;accent-color:var(--acc)}
 #three-wrap{position:absolute;inset:0;z-index:2000;display:none;background:#0a0a1a}
 #three-wrap .maplibregl-canvas{outline:none}
 #three-wrap .maplibregl-map{width:100%;height:100%}
 #btn3dClose{position:absolute;top:14px;right:14px;z-index:2001;padding:7px 16px;border-radius:10px;border:1px solid var(--bd);background:var(--glass);color:var(--fg);cursor:pointer;font-size:13px;font-weight:600;backdrop-filter:blur(10px)}
 #three-wrap .ctrl3d{position:absolute;bottom:30px;left:50%;transform:translateX(-50%);z-index:2001;display:flex;gap:10px;flex-wrap:wrap;justify-content:center;max-width:calc(100vw - 24px)}
 #three-wrap .ctrl3d button,#three-wrap .ctrl3d label,#three-wrap .ctrl3d select{padding:6px 14px;border-radius:8px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);cursor:pointer;font-size:12px;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);touch-action:manipulation}
 #three-wrap .ctrl3d button:hover{border-color:var(--acc);color:var(--fg)}
 @media (max-width:560px){#three-wrap .ctrl3d{bottom:calc(16px + env(safe-area-inset-bottom,0px));gap:5px}#three-wrap .ctrl3d button,#three-wrap .ctrl3d label,#three-wrap .ctrl3d select{padding:10px 12px;font-size:12px;min-height:44px;border-radius:10px}#btn3dClose{top:calc(8px + env(safe-area-inset-top,0px));right:8px;padding:10px 16px;font-size:14px;border-radius:12px}}
 .sub{font-size:12px;color:var(--mut)}
 .asp-crisp img{image-rendering:pixelated;image-rendering:crisp-edges}
 .legend{position:absolute;z-index:950;bottom:calc(var(--btm-h,80px) + 40px);left:12px;background:var(--glass);backdrop-filter:blur(16px) saturate(1.3);-webkit-backdrop-filter:blur(16px) saturate(1.3);border:1px solid var(--bd);padding:8px 10px;border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,.35);font-size:11px;max-width:200px;line-height:1.5;color:var(--fg2);display:none}
 .legend.show{display:block}
 #legendBtn{position:absolute;z-index:960;bottom:var(--btm-h,80px);left:12px;width:34px;height:34px;border-radius:10px;border:1px solid var(--bd);background:var(--glass);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--fg2);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.3)}
 #legendBtn:hover,#legendBtn.active{border-color:var(--acc);color:var(--acc)}
 .legend i{display:inline-block;width:12px;height:12px;margin-right:5px;vertical-align:-2px;border-radius:2px}
 .stn{background:rgba(15,20,35,.7);border:2px solid var(--acc);border-radius:11px;padding:1px 6px;font-size:11px;font-weight:700;color:var(--acc);text-align:center;box-shadow:0 1px 6px rgba(0,0,0,.5);white-space:nowrap}
 .scl{position:relative;width:110px;height:56px;font-size:10px;font-weight:700;text-align:center;pointer-events:none}
 .scl>div{position:absolute;left:0;right:0;white-space:nowrap}
 .scl .s-pill{display:inline-block;background:rgba(8,12,28,.7);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);border-radius:6px;padding:1px 5px;box-shadow:0 1px 4px rgba(0,0,0,.4),inset 0 0 0 1px rgba(255,255,255,.08)}
 .scl .s-t{top:-2px;color:#6bc5f0}.scl .s-b{bottom:-2px;color:#f07070}
 .scl .s-row{top:17px;display:flex;align-items:center;justify-content:center;gap:4px}
 .scl .s-l{color:#b0c4de}.scl .s-r{color:#70d890}
 .scl .s-c{pointer-events:auto;background:rgba(8,12,28,.82);border:2px solid var(--acc);border-radius:10px;color:var(--acc);padding:1px 7px;font-size:12px;font-weight:800;box-shadow:0 2px 8px rgba(0,0,0,.5),0 0 0 3px rgba(91,156,245,.12);cursor:pointer;letter-spacing:.02em}
 .scard{font:13px system-ui;line-height:1.6;min-width:160px;color:var(--fg)}
 .scard b{font-size:14px}
 .scard .g{display:grid;grid-template-columns:auto auto;gap:3px 16px;margin-top:6px}
 .scard .k{color:var(--mut)}
 .wn{font-size:10px;color:#d0ddf0;font-weight:700;display:inline-block;background:rgba(8,12,28,.65);border-radius:5px;padding:0 4px;box-shadow:0 1px 3px rgba(0,0,0,.35)}
 .icard{font:12.5px system-ui;line-height:1.5;min-width:200px;max-width:280px;color:var(--fg)}
 .icard b{font-size:13px}
 .icard .ig{display:grid;grid-template-columns:auto auto;gap:2px 14px;margin-top:6px}
 .icard .ik{color:var(--mut);white-space:nowrap}
 .icard .isep{grid-column:1/-1;border-top:1px solid var(--bd);margin:4px 0}
 .icard .ipow{margin-top:6px;padding:4px 8px;border-radius:6px;font-weight:600;font-size:12px;text-align:center}
 .icard .ipow.yes{background:rgba(91,156,245,.2);color:#8ec4ff}
 .icard .ipow.no{background:rgba(255,100,100,.15);color:#ff9090}
 .icard .imore{color:var(--acc);font-size:12px;margin-top:6px;cursor:pointer;text-align:center;padding:4px;border-radius:6px;background:rgba(255,255,255,.06)}
 .leaflet-popup-content-wrapper{background:var(--glass2)!important;backdrop-filter:blur(14px)!important;-webkit-backdrop-filter:blur(14px)!important;border:1px solid var(--bd)!important;color:var(--fg)!important;box-shadow:0 6px 24px rgba(0,0,0,.5)!important;border-radius:14px!important}
 .leaflet-popup-content{margin:12px 14px!important}
 .leaflet-popup-tip{background:var(--glass2)!important}
 .leaflet-popup-close-button{color:var(--mut)!important;font-size:20px!important;width:28px!important;height:28px!important;line-height:28px!important}
 .leaflet-popup-close-button:hover{color:var(--fg)!important}
 #searchWrap{position:absolute;z-index:1100;top:46px;right:12px;width:220px;max-width:calc(100vw - 24px)}
 #searchWrap input{width:100%;padding:9px 12px 9px 32px;border-radius:10px;border:1px solid rgba(255,255,255,.08);background:rgba(15,20,35,.35);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--fg);font-size:13px;outline:none}
 #searchWrap input::placeholder{color:var(--mut)}
 #searchWrap input:focus{border-color:var(--acc);background:var(--glass);box-shadow:0 0 0 3px var(--glow)}
 #btn3dFloat{position:absolute;z-index:1000;bottom:var(--btm-h,80px);right:12px;padding:8px 14px;border-radius:10px;border:1px solid var(--bd);background:var(--glass);backdrop-filter:blur(12px);color:var(--fg2);cursor:pointer;font-size:13px;font-weight:600}
 #btn3dFloat:hover,#btn3dFloat.active{border-color:var(--acc);color:var(--acc)}
 #stnToggleWrap{position:absolute;z-index:1050;top:82px;right:12px}
 #stnToggleWrap label{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--fg2);cursor:pointer;padding:6px 10px;border-radius:10px;border:1px solid var(--bd);background:var(--glass);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
 #stnToggleWrap input{width:16px;height:16px;accent-color:var(--acc)}
 #searchWrap .icn{position:absolute;left:12px;top:50%;transform:translateY(-50%);pointer-events:none;color:var(--mut);font-size:15px}
 #searchRes{position:absolute;top:100%;left:0;right:0;margin-top:4px;background:var(--glass2);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--bd);border-radius:12px;overflow:hidden;display:none;max-height:260px;overflow-y:auto;box-shadow:0 8px 28px rgba(0,0,0,.5)}
 #searchRes .sr{padding:10px 14px;cursor:pointer;font-size:13px;color:var(--fg2);border-bottom:1px solid rgba(255,255,255,.06);transition:.12s}
 #searchRes .sr:last-child{border-bottom:none}
 #searchRes .sr:hover,#searchRes .sr.sel{background:rgba(91,156,245,.15);color:var(--fg)}
 #searchRes .sr .sub{font-size:11px;color:var(--mut);margin-top:2px}
 #intro{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;flex-direction:column;background:linear-gradient(135deg,#0a0e1a 0%,#111b33 40%,#0d1525 100%);transition:opacity .8s ease}
 #intro.hide{opacity:0;pointer-events:none}
 #intro h1{font-size:clamp(28px,5vw,48px);font-weight:800;letter-spacing:-.02em;color:#e8ecf1;margin:0;opacity:0;transform:translateY(20px);animation:introUp .7s .3s ease forwards}
 #intro .sub{font-size:clamp(13px,2vw,17px);color:var(--mut);margin-top:8px;opacity:0;animation:introUp .6s .7s ease forwards}
 #intro .bar{width:120px;height:3px;border-radius:2px;background:var(--acc);margin-top:20px;opacity:0;transform:scaleX(0);animation:introBar .8s 1s ease forwards}
 @keyframes introUp{to{opacity:1;transform:translateY(0)}}
 @keyframes introBar{to{opacity:1;transform:scaleX(1)}}
 #intro .mtn{position:absolute;bottom:0;left:0;width:100%;height:40%;opacity:0;animation:introUp 1s .2s ease forwards}
 #intro .sources{font-size:clamp(10px,1.5vw,13px);color:var(--mut);margin-top:16px;letter-spacing:.08em;opacity:0;animation:introUp .6s .9s ease forwards}
 #intro .snow-wrap{position:absolute;inset:0;overflow:hidden;pointer-events:none}
 .sf{position:absolute;top:-10px;width:6px;height:6px;background:white;border-radius:50%;opacity:.6;animation:sfDrop linear infinite}
 @keyframes sfDrop{0%{transform:translateY(0) translateX(0)}25%{transform:translateY(25vh) translateX(15px)}50%{transform:translateY(50vh) translateX(-10px)}75%{transform:translateY(75vh) translateX(20px)}100%{transform:translateY(110vh) translateX(5px)}}
 @media (max-width:560px){
   #layerBar{padding:6px 8px;gap:4px}
   #layerBar button{padding:5px 10px;font-size:11px;min-height:30px}
   #searchWrap{top:42px;right:8px;width:calc(100vw - 16px);max-width:200px}
   .icard{font-size:11.5px;max-width:220px;min-width:160px}
   .scard{font-size:12px;min-width:140px}
   .leaflet-popup-content-wrapper{max-width:calc(100vw - 40px)!important}
   .legend{max-width:150px;font-size:10px}
   #btn3dFloat{padding:6px 12px;font-size:12px}
   #statBar{top:38px;padding:3px 8px;gap:4px}
   #statBar button{padding:3px 8px;font-size:10px;min-height:24px}
   #stnToggleWrap{top:74px;right:8px}
   #stnToggleWrap label{font-size:11px;padding:5px 8px}
   #legendBtn{width:30px;height:30px;font-size:14px}
 }
 @media (max-width:380px){
   #searchWrap{max-width:160px}
   .legend{max-width:120px;font-size:9px}
 }
</style></head><body>
<div id="intro"><div class="snow-wrap" id="snowWrap"></div><svg class="mtn" viewBox="0 0 800 200" preserveAspectRatio="none"><path d="M0,200 L80,110 L140,155 L240,55 L310,125 L390,35 L460,105 L540,55 L620,115 L700,65 L800,140 L800,200Z" fill="rgba(255,255,255,.04)"/><path d="M0,200 L100,130 L180,165 L300,80 L380,150 L470,85 L550,145 L660,95 L750,145 L800,165 L800,200Z" fill="rgba(255,255,255,.07)"/><path d="M0,200 L60,170 L160,145 L260,175 L360,130 L440,170 L520,150 L620,175 L720,155 L800,180 L800,200Z" fill="rgba(255,255,255,.03)"/></svg><h1>Swiss Snow Model</h1><div class="sub">Interactive Snow Forecast Map</div><div class="sources">swisstopo &middot; MeteoSwiss &middot; SLF &middot; Open-Meteo &middot; Copernicus</div><div class="bar"></div></div>
<div id="map"></div>
<canvas id="flow"></canvas>
<div id="layerBar">
  <button data-l="snow" class="active">New Snow</button>
  <button data-l="depth">Snow Depth</button>
  <button data-l="temp">Temperature</button>
  <button data-l="wind">Wind</button>
  <button data-l="sun">Sunshine</button>
  <button data-l="rad">Radiation</button>
  <button data-l="radsun">Eff. Rad.</button>
  <button data-l="slope">Slope</button>
  <button data-l="aspect">Aspect</button>
  <button data-l="rough">Roughness</button>
  <button data-l="tsurf">T Surface</button>
  <button data-l="shade">Hillshade</button>
  <button data-l="skiable">Skiable</button>
  <button data-l="powder">Powder</button>
</div>
<div id="statBar"><span class="cap">Stat</span><div class="seg" id="stat">
<button data-s="avg" class="active">Ø Mean</button><button data-s="max">Max</button>
<button data-s="min">Min</button>
<button data-s="sub0">always &lt;0°C</button><button data-s="max05">Max 0–5°C</button>
<button data-s="lt10">max &lt;10 km/h</button></div></div>
<div id="searchWrap"><span class="icn">&#x1F50D;</span><input id="searchIn" type="text" placeholder="Search location..." autocomplete="off"/><div id="searchRes"></div></div>
<div id="stnToggleWrap"><label><input type="checkbox" id="stnToggle" checked/> SLF Stations</label></div>
<button id="btn3dFloat">3D</button>
<div id="bottomPanel">
  <div class="drag" id="btmDrag"></div>
  <div id="btmMain">
    <canvas id="timeline" width="720" height="66" style="width:100%;border-radius:6px;cursor:default"></canvas>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
      <div class="winlbl" id="window"></div>
      <div class="seg" id="presets" style="gap:4px">
        <button data-d="24" style="padding:4px 8px;font-size:11px;min-height:28px">24h</button>
        <button data-d="48" class="active" style="padding:4px 8px;font-size:11px;min-height:28px">48h</button>
        <button data-d="72" style="padding:4px 8px;font-size:11px;min-height:28px">72h</button>
        <button data-d="120" style="padding:4px 8px;font-size:11px;min-height:28px">120h</button>
      </div>
    </div>
  </div>
  <div id="btmExtra">
    <div class="seg" style="margin-top:8px">
      <button id="btnSinceSnow" style="font-size:12px">Since Last Snowfall</button>
      <button data-r="tomorrow" style="font-size:12px">Till Tomorrow</button>
    </div>
  </div>
</div>
<button id="legendBtn" title="Toggle legend">&#x2139;</button><div class="legend" id="legend"></div>
<div id="three-wrap"><div id="map3d" style="width:100%;height:100%"></div><button id="btn3dClose">✕ 2D</button>
<div class="ctrl3d">
<button onclick="map3d.easeTo({pitch:0,bearing:0,duration:600})">Top</button><button onclick="map3d.easeTo({pitch:60,duration:600})">Tilt</button><button onclick="map3d.easeTo({pitch:75,duration:600})">FatMap</button>
</div>
<div class="ctrl3d" style="bottom:70px"><label style="display:flex;align-items:center;gap:6px;color:var(--fg2);font-size:11px">Map <input id="mapOpac3d" type="range" min="0" max="100" value="15" style="width:80px;accent-color:var(--acc)"> <span id="mapOpacLbl">15%</span></label>
<select id="overlay3d" style="padding:4px 8px;border-radius:8px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);font-size:12px;backdrop-filter:blur(10px)"><option value="none">No overlay</option><option value="snow">Snow</option><option value="temp">Temperature</option><option value="wind">Wind</option><option value="depth">Snow Depth</option></select></div>
</div>
<script>
const M=/*META*/;
function dec(b){const s=atob(b),n=s.length,a=new Uint8Array(n);for(let i=0;i<n;i++)a[i]=s.charCodeAt(i);return a;}
const SNOW=dec("__SNOW__"),TEMP=dec("__TEMP__"),SUN=dec("__SUN__"),SPD=dec("__SPD__"),WDIR=dec("__DIR__");
const WINDG=dec("__WINDG__"),RSLOPE=dec("__RSLOPE__"),RASPECT=dec("__RASPECT__"),RHOR=dec("__RHOR__");
const PREC=dec("__PREC__"),MASPECT=dec("__MASPECT__"),MSLOPE=dec("__MSLOPE__"),MELEV=dec("__MELEV__"),ROUGHG=dec("__ROUGHGRID__");
const ASPECT_PNG="data:image/png;base64,"+"__ASPECTPNG__",ROUGH_PNG="data:image/png;base64,"+"__ROUGHPNG__";
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
function computePowder(p,ta,tb){
  const asp=maspv(p),slp=mslpv(p),quad=aspectQ(asp);
  const res={powdered:false,valid_aspects:[],reason_flags:[],quality:'stable'};
  // Dominant wind + mean wind from nearest wind point
  const wk=mainToWind[p];let ss=0,sc2=0,ws=0,wc=0;
  for(let t=ta;t<tb;t++){const d=WDIR[t*P+wk]*M.dir_div*Math.PI/180;ss+=Math.sin(d);sc2+=Math.cos(d);ws+=SPD[t*P+wk]/M.spd_mul*3.6;wc++;}
  const domW=(Math.atan2(ss,sc2)*180/Math.PI+360)%360;
  const meanW=ws/Math.max(1,wc), gustW=meanW*PD_GUST_FACTOR;
  // D1: Rain in last 48h
  const r0=Math.max(0,tb-PD_RAIN_LOOKBACK_H);let rain=false;
  for(let t=r0;t<tb&&!rain;t++){if(precv(t,p)>PD_RAIN_MIN_MM&&tv(t,p)>PD_RAIN_TEMP_C)rain=true;}
  if(rain){res.reason_flags.push('D1_rain');return res;}
  // D2: Freeze-thaw cycles since last snowfall
  let lastSnow=ta;
  for(let t=ta;t<tb;t++){if(SNOW[t*NP+p]>0)lastSnow=t;}
  let ftc=0,bel=tv(lastSnow,p)<0;
  for(let t=lastSnow+1;t<tb;t++){const b2=tv(t,p)<0;if(b2!==bel){ftc+=0.5;bel=b2;}}
  ftc=Math.floor(ftc);
  if(ftc>PD_FT_DESTROY){res.reason_flags.push('D2_freeze_thaw');return res;}
  // D3: Solar melt
  const doy=bandDoy();if(doy!==radDoy){radCS=computeRad(doy);radDoy=doy;}
  const rp=main2rad[p];const solar=rp>=0?radCS[rp]:0;
  if(solar>=PD_SOLAR_DESTROY_WH){res.reason_flags.push('D3_solar_melt');return res;}
  // --- Preservation ---
  let tmax=-999;for(let t=ta;t<tb;t++){const v=tv(t,p);if(v>tmax)tmax=v;}
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
  // Solar moderation
  if(solar>=PD_SOLAR_MOD_WH&&solar<PD_SOLAR_DESTROY_WH){valid.delete('S');valid.delete('W');}
  res.valid_aspects=[...valid];
  if(!valid.has(quad))return res;
  // Degradation
  if(ftc>=PD_FT_DEGRADE)res.quality='reduced';
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
function drawTimeline(){const tc=document.getElementById('timeline'),ctx2=tc.getContext('2d'),cw=tc.width,ch=tc.height;
  ctx2.clearRect(0,0,cw,ch);
  const nx=nowIdx/T*cw;
  ctx2.fillStyle='rgba(15,20,35,.6)';ctx2.fillRect(0,0,nx,ch);
  ctx2.fillStyle='rgba(25,35,65,.45)';ctx2.fillRect(nx,0,cw-nx,ch);
  const x1=a/T*cw,x2=b/T*cw;ctx2.fillStyle='rgba(91,156,245,.2)';ctx2.fillRect(x1,0,x2-x1,ch);
  ctx2.font='10px system-ui';ctx2.textAlign='center';
  for(let t=0;t<T;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===0){const x=t/T*cw;
    ctx2.fillStyle=t>=nowIdx?'rgba(100,160,255,.18)':'rgba(255,255,255,.1)';ctx2.fillRect(x,0,1,ch);
    ctx2.fillStyle=t>=nowIdx?'rgba(140,180,240,.7)':'rgba(200,210,225,.5)';
    ctx2.fillText(d.toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'short'}),x+22,ch-3);}}
  let mx=0;for(const s of hSnow)if(s>mx)mx=s;mx=Math.max(.05,mx);
  const bw=Math.max(1.2,cw/T),barH=ch-22;
  for(let t=0;t<T;t++){const v=hSnow[t];if(v<.002)continue;const h=Math.max(1,v/mx*barH);const x=t/T*cw;
    const inSel=(t>=a&&t<b);const fut=t>=nowIdx;
    ctx2.fillStyle=inSel?(fut?'rgba(130,200,255,.9)':'rgba(200,225,255,.8)'):(fut?'rgba(80,130,200,.3)':'rgba(140,160,180,.3)');
    ctx2.fillRect(x,ch-16-h,Math.max(bw-.3,1),h);}
  for(const[s,e]of snowEvents){const xs=s/T*cw,xe=e/T*cw;ctx2.fillStyle='rgba(91,156,245,.3)';ctx2.fillRect(xs,ch-16,xe-xs,3);}
  const scW=30;ctx2.fillStyle='rgba(10,15,30,.6)';ctx2.fillRect(cw-scW,0,scW,ch-14);
  ctx2.font='8px system-ui';ctx2.textAlign='right';ctx2.fillStyle='rgba(200,215,235,.6)';
  for(let i=0;i<=3;i++){const frac=i/3;const cm=(mx*frac).toFixed(mx>=1?0:1);const y=ch-16-frac*barH;
    ctx2.fillText(cm,cw-3,y+3);if(i>0){ctx2.strokeStyle='rgba(255,255,255,.06)';ctx2.lineWidth=.5;ctx2.beginPath();ctx2.moveTo(0,y);ctx2.lineTo(cw-scW,y);ctx2.stroke();}}
  ctx2.fillStyle='rgba(200,215,235,.4)';ctx2.font='7px system-ui';ctx2.fillText('cm/h',cw-3,12);
  ctx2.strokeStyle='rgba(91,156,245,.5)';ctx2.lineWidth=1.5;ctx2.strokeRect(x1+.5,0,x2-x1-1,ch);
  ctx2.strokeStyle='#ff3040';ctx2.lineWidth=2.5;ctx2.beginPath();ctx2.moveTo(nx,0);ctx2.lineTo(nx,ch);ctx2.stroke();
  ctx2.fillStyle='#ff3040';ctx2.font='bold 10px system-ui';ctx2.textAlign='center';ctx2.fillText('NOW',nx,12);
  ctx2.font='bold 8px system-ui';ctx2.globalAlpha=.35;
  if(nx>28){ctx2.textAlign='right';ctx2.fillStyle='#aab8cc';ctx2.fillText('PAST',nx-5,24);}
  if(cw-nx>50){ctx2.textAlign='left';ctx2.fillStyle='#8cb8f0';ctx2.fillText('FORECAST',nx+5,24);}
  ctx2.globalAlpha=1;}
// Karte + Layer
const [laMin,loMin,laMax,loMax]=M.bounds;
const map=L.map('map').fitBounds([[laMin,loMin],[laMax,loMax]],{padding:[10,10]});
const base=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg",{attribution:"© swisstopo / MeteoSwiss / SLF / Copernicus"}).addTo(map);
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
let raster=L.imageOverlay(cv.toDataURL(),[[laMin,loMin],[laMax,loMax]],{opacity:.82}).addTo(map);
const rcv=document.createElement('canvas');rcv.width=RW;rcv.height=RH;const rcx=rcv.getContext('2d');
let radOverlay=L.imageOverlay(rcv.toDataURL(),[[RbS,RlW],[RbN,RlE]],{opacity:.8});
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
    ${row("Snow Depth",s.hs_now!=null?s.hs_now.toFixed(0)+" cm":null)}
    ${row("New Snow",ns!=null?"+"+ns.toFixed(0)+" cm":null)}
    ${row("Air Temp",s.ta!=null?s.ta.toFixed(1)+" °C":null)}
    ${row("Snow Surface",s.tss!=null?s.tss.toFixed(1)+" °C":null)}
    ${row("Wind",windStr)}
    ${row("Wind Dir.",s.dw!=null?s.dw.toFixed(0)+"°":null)}
    </div></div>`;}
function renderStations(){stnGroup.clearLayers();if(!showStn)return;
  const dirAb=d=>["N","NE","E","SE","S","SW","W","NW"][Math.round(d/45)%8];
  const zoom=map.getZoom();
  const stns=isMobile?M.stations.filter((_,i)=>zoom>=10||i%3===0):M.stations;
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
    if(isMobile){
      let unit='';if(layer=='wind')unit=' km/h';else if(layer=='temp'||layer=='tsurf')unit='C';
      else if(layer=='sun'||layer=='rad'||layer=='radsun')unit='';else if(lbl!=='–')unit=' cm';
      html='<div class="stn">'+lbl+unit+'</div>';iSize=[56,22];iAnc=[28,11];
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
if(isMobile)map.on('zoomend',renderStations);
function fmt(i){const d=new Date(M.times[Math.max(0,Math.min(T-1,i))]+"Z");return d.toLocaleString('en-GB',{weekday:'short',day:'2-digit',month:'2-digit',hour:'2-digit'});}
function dayLabel(doy){const d=new Date(2026,0,1);d.setDate(doy);return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'});}
function legendFor(l){const sn={avg:'Mean',max:'Max',min:'Min',sub0:'always <0°C',max05:'Max 0–5°C',lt10:'max <10 km/h'}[stat];
  if(l=="snow"){let h="<b>New Snow [cm] (SLF scale)</b><br>";for(let i=0;i<SB.length-1;i++)h+=`<div><i style="background:${SC[i]}"></i>${SB[i]}–${SB[i+1]}</div>`;return h+"<div style='margin-top:5px'><span class='stn' style='padding:0 3px'>NN</span> Station (click for details)</div>";}
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
  if(l=="powder")return '<b>Powder Conditions</b><br><div><i style="background:rgba(200,220,255,.7)"></i>Powder (stable)</div><div><i style="background:rgba(180,205,245,.55)"></i>Powder (reduced)</div><div style="margin-top:4px;font-size:11px">Gust ≈ mean wind × 1.5</div>';
  return "<b>Hillshade / Relief (swisstopo)</b>";}
function legend(l){document.getElementById('legend').innerHTML=legendFor(l||layer);}
document.getElementById('legendBtn').onclick=()=>{const lg=document.getElementById('legend'),btn=document.getElementById('legendBtn');lg.classList.toggle('show');btn.classList.toggle('active');};
function showOverlay(){
  [slopeWMTS,reliefWMTS,aspectGrid,roughImg,radOverlay].forEach(x=>map.removeLayer(x));
  const grid=(layer=="snow"||layer=="depth"||layer=="temp"||layer=="sun"||layer=="wind"||layer=="powder"||layer=="tsurf"||layer=="skiable");
  const radg=(layer=="rad"||layer=="radsun");
  raster.setOpacity(grid?0.82:0);
  if(radg)map.addLayer(radOverlay);
  if(layer=="slope")map.addLayer(slopeWMTS);
  else if(layer=="shade")map.addLayer(reliefWMTS);
  else if(layer=="aspect")map.addLayer(aspectGrid);
  else if(layer=="rough")map.addLayer(roughImg);
  if(layer=="wind"){map.addLayer(windArr);startFlow();}else{map.removeLayer(windArr);stopFlow();}
}
function renderAll(){showOverlay();renderRaster();renderStations();drawTimeline();
  if(layer=="rad"||layer=="radsun")renderRadiation();
  if(layer=="wind"){buildFlow();if(wtimer)clearTimeout(wtimer);wtimer=setTimeout(renderWind,120);}
  document.getElementById('window').innerHTML=`<b>${fmt(a)}</b> → <b>${fmt(b)}</b> (${b-a} h)`;legend();}
document.querySelectorAll('#layerBar button').forEach(btn=>{
  btn.onclick=()=>{document.querySelectorAll('#layerBar button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');layer=btn.dataset.l;
    document.getElementById('statBar').style.display=(layer=="temp"||layer=="wind"||layer=="tsurf")?"flex":"none";
    document.querySelectorAll('#stat [data-s=sub0],#stat [data-s=max05]').forEach(x=>x.style.display=(layer=="temp"||layer=="tsurf")?"":"none");
    document.querySelectorAll('#stat [data-s=lt10]').forEach(x=>x.style.display=(layer=="wind")?"":"none");
    if((layer=="wind"&&(stat=="sub0"||stat=="max05"))||((layer=="temp"||layer=="tsurf")&&stat=="lt10")){stat="avg";document.querySelectorAll('#stat button').forEach(x=>x.classList.toggle('active',x.dataset.s=="avg"));}
    renderAll();};
  btn.onmouseenter=()=>legend(btn.dataset.l);btn.onmouseleave=()=>legend();});
document.querySelectorAll('#stat button').forEach(btn=>btn.onclick=()=>{document.querySelectorAll('#stat button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');stat=btn.dataset.s;renderAll();});
document.querySelectorAll('#presets button[data-d]').forEach(btn=>{btn.onclick=()=>{
  document.querySelectorAll('#presets button[data-d]').forEach(x=>x.classList.remove('active'));btn.classList.add('active');
  windowSize=parseInt(btn.dataset.d);const center=Math.round((a+b)/2);
  a=Math.max(0,Math.min(T-windowSize,center-Math.floor(windowSize/2)));b=Math.min(T,a+windowSize);renderAll();};});
document.querySelectorAll('#presets button[data-r]').forEach(btn=>{btn.onclick=()=>{
  const p=tillTomorrow();a=p[0];b=p[1];windowSize=b-a;
  document.querySelectorAll('#presets button[data-d]').forEach(x=>x.classList.remove('active'));renderAll();};});
document.getElementById('btnSinceSnow').onclick=()=>{const p=sinceLastSnowfall();a=p[0];b=p[1];windowSize=b-a;
  document.querySelectorAll('#presets button[data-d]').forEach(x=>x.classList.remove('active'));renderAll();};
// --- Timeline Drag ---
(function(){const tc=document.getElementById('timeline');let dragging=false,dragStartX=0,dragStartA=0,ws=0;
  function startDrag(e){const cx=e.touches?e.touches[0].clientX:e.clientX;const rect=tc.getBoundingClientRect();
    const x1=a/T*rect.width+rect.left,x2=b/T*rect.width+rect.left;
    if(cx>=x1-15&&cx<=x2+15){dragging=true;dragStartX=cx;dragStartA=a;ws=b-a;tc.style.cursor='grabbing';e.preventDefault();}}
  tc.addEventListener('mousedown',startDrag);tc.addEventListener('touchstart',startDrag,{passive:false});
  function onDrag(e){if(!dragging)return;e.preventDefault();const cx=e.touches?e.touches[0].clientX:e.clientX;
    const rect=tc.getBoundingClientRect();const delta=Math.round((cx-dragStartX)/rect.width*T);
    let na=Math.max(0,Math.min(T-ws,dragStartA+delta));a=na;b=na+ws;
    drawTimeline();document.getElementById('window').innerHTML='<b>'+fmt(a)+'</b> → <b>'+fmt(b)+'</b> ('+ws+' h)';}
  document.addEventListener('mousemove',onDrag);document.addEventListener('touchmove',onDrag,{passive:false});
  function endDrag(){if(dragging){dragging=false;tc.style.cursor='grab';renderAll();}}
  document.addEventListener('mouseup',endDrag);document.addEventListener('touchend',endDrag);
  tc.addEventListener('mousemove',function(e){if(dragging)return;const rect=tc.getBoundingClientRect();const cx=e.clientX;
    const x1=a/T*rect.width+rect.left,x2=b/T*rect.width+rect.left;
    tc.style.cursor=(cx>=x1-5&&cx<=x2+5)?'grab':'default';});
})();
document.getElementById('stnToggle').onchange=e=>{showStn=e.target.checked;renderStations();};
// --- Bottom Panel slide ---
(function(){const bp=document.getElementById('bottomPanel'),drag=document.getElementById('btmDrag');let ty=0;
  function updateBtmH(){requestAnimationFrame(()=>{document.documentElement.style.setProperty('--btm-h',bp.offsetHeight+'px');});}
  function toggleExtra(){bp.classList.toggle('expanded');updateBtmH();}
  drag.addEventListener('click',toggleExtra);
  drag.addEventListener('touchstart',e=>{ty=e.touches[0].clientY;},{passive:true});
  drag.addEventListener('touchend',e=>{const dy=ty-e.changedTouches[0].clientY;
    if(dy>30&&!bp.classList.contains('expanded')){bp.classList.add('expanded');updateBtmH();}
    else if(dy<-30&&bp.classList.contains('expanded')){bp.classList.remove('expanded');updateBtmH();}
  },{passive:true});
  updateBtmH();
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
  fx.globalCompositeOperation='destination-out';fx.fillStyle=`rgba(0,0,0,${FLOW_FADE})`;fx.fillRect(0,0,flow.width,flow.height);
  fx.globalCompositeOperation='source-over';
  for(const p of parts){const ll=map.containerPointToLatLng([p.x,p.y]);const v=flowVel[wIdx(ll.lat,ll.lng)];
    const kmh=v.sp*3.6,sc=0.5+kmh*0.12;
    const nx=p.x+v.ux*sc,ny=p.y+v.uy*sc;
    const life=1-p.age/p.maxAge,alpha=Math.min(0.7,life*0.8+0.1);
    const c=rampBYR(kmh/70);const lw=0.8+Math.min(2.2,kmh*0.04);
    fx.strokeStyle=`rgba(${c[0]},${c[1]},${c[2]},${alpha.toFixed(2)})`;fx.lineWidth=lw;
    fx.beginPath();fx.moveTo(p.x,p.y);fx.lineTo(nx,ny);fx.stroke();
    p.x=nx;p.y=ny;p.age++;if(p.age>p.maxAge||nx<-10||ny<-10||nx>flow.width+10||ny>flow.height+10)Object.assign(p,fspawn());}
  flowReq=requestAnimationFrame(animFlow);}
map.on('move',()=>{if(layer=="wind"){flowResize();fx.clearRect(0,0,flow.width,flow.height);}});
map.on('resize',()=>{if(layer=="wind")flowResize();});
drawTimeline();
// --- Point Inspector (universal click popup) ---
map.setMaxBounds([[laMinW-0.05,loMinW-0.05],[laMaxW+0.05,loMaxW+0.05]]);
map.setMinZoom(map.getBoundsZoom([[laMinW,loMinW],[laMaxW,loMaxW]])+0.3);
let inspPopup=null;
map.on('click',function(e){
  if(inspPopup){map.closePopup(inspPopup);inspPopup=null;}
  const lat=e.latlng.lat,lon=e.latlng.lng;
  const cx2=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy2=Math.round((laMax-lat)/(laMax-laMin)*(H-1));
  if(cx2<0||cx2>=W||cy2<0||cy2>=H)return;
  const p=cy2*W+cx2;
  const elev=melevv(p),asp=maspv(p),slp=mslpv(p),quad=aspectQ(asp);
  const ca=a*NP,cb=b*NP;const newSnow=cum[cb+p]-cum[ca+p];
  let tmin=1e9,tmax=-1e9,tsum=0,tc=0;
  for(let t=a;t<b;t++){const v=tv(t,p);if(v<tmin)tmin=v;if(v>tmax)tmax=v;tsum+=v;tc++;}
  const tmean=tsum/Math.max(1,tc);
  let lastSnowT=a;
  for(let t=a;t<b;t++){if(SNOW[t*NP+p]>0)lastSnowT=t;}
  let ftc=0,bel=tv(lastSnowT,p)<0;
  for(let t=lastSnowT+1;t<b;t++){const b2=tv(t,p)<0;if(b2!==bel){ftc+=0.5;bel=b2;}}
  ftc=Math.floor(ftc);
  const wk=mainToWind[p];let wsum=0,wmax=-1;
  for(let t=a;t<b;t++){const v=SPD[t*P+wk]/M.spd_mul*3.6;wsum+=v;if(v>wmax)wmax=v;}
  const wmean=wsum/Math.max(1,b-a);
  const doy=bandDoy();if(doy!==radDoy){radCS=computeRad(doy);radDoy=doy;}
  const rp=main2rad[p];const solar=rp>=0?radCS[rp]:0;
  let tsMin=1e9,tsMax=-1e9,tsSum=0;
  for(let t=a;t<b;t++){const v=tsurfEst(t,p);if(v<tsMin)tsMin=v;if(v>tsMax)tsMax=v;tsSum+=v;}
  const tsMean=tsSum/Math.max(1,b-a);
  const pw=computePowder(p,a,b);
  const R=(k,v)=>'<span class="ik">'+k+'</span><span>'+v+'</span>';
  let sunSum=0;for(let t=a;t<b;t++)sunSum+=sunv(t,p);
  const sunFrac=Math.max(0,Math.min(1,sunSum/(0.42*Math.max(1,b-a))));
  const effRad=solar*(0.2+0.8*sunFrac);
  const needS=minSnowNeeded(p),ratS=newSnow/Math.max(1,needS);
  let h='<div class="icard"><b>'+lat.toFixed(4)+'° N, '+lon.toFixed(4)+'° E</b>';
  h+='<div class="ig">';
  h+=R('Elevation',elev.toFixed(0)+' m');
  h+=R('New Snow',newSnow.toFixed(1)+' cm');
  h+=R('T Air Ø',tmean.toFixed(1)+' °C');
  h+=R('Wind Ø',wmean.toFixed(0)+' km/h');
  h+='</div>';
  h+='<div class="ipow '+(pw.powdered?'yes':'no')+'">';
  h+='Powder: '+(pw.powdered?'YES':'NO');
  if(pw.quality==='reduced')h+=' (reduced)';
  h+='</div>';
  if(isMobile){
    h+='<div class="imore" onclick="const d=this.nextElementSibling;d.style.display=d.style.display===\'none\'?\'grid\':\'none\';this.textContent=this.textContent===\'More...\'?\'Less\':\'More...\'">More...</div>';
    h+='<div class="ig" style="display:none">';}
  else h+='<div class="ig">';
  h+=R('Slope',slp.toFixed(0)+'°');
  h+=R('Aspect',quad+' ('+asp.toFixed(0)+'°)');
  h+=R('T Air Min',tmin.toFixed(1)+' °C');
  h+=R('T Air Max',tmax.toFixed(1)+' °C');
  h+=R('T Surf Ø',tsMean.toFixed(1)+' °C');
  h+=R('T Surf Min',tsMin.toFixed(1)+' °C');
  h+=R('Freeze-Thaw',ftc+' cycles');
  h+='<div class="isep"></div>';
  h+=R('Wind Max',wmax.toFixed(0)+' km/h');
  h+=R('≈ Gust Max',(wmax*PD_GUST_FACTOR).toFixed(0)+' km/h');
  h+='<div class="isep"></div>';
  h+=R('Rad. Clear',solar.toFixed(0)+' Wh/m²/d');
  h+=R('Rad. Eff.',effRad.toFixed(0)+' Wh/m²/d');
  h+='<div class="isep"></div>';
  h+=R('Roughness',roughv(p).toFixed(0)+' m');
  h+=R('Min Snow',needS.toFixed(0)+' cm');
  h+=R('Skiable',ratS>=1.0?'YES ('+ratS.toFixed(1)+'×)':'NO ('+ratS.toFixed(1)+'×)');
  h+='</div>';
  if(pw.reason_flags.length)h+='<div style="font-size:11px;color:var(--mut);margin-top:4px">'+pw.reason_flags.join(', ')+'</div>';
  if(pw.valid_aspects.length)h+='<div style="font-size:11px;margin-top:2px">Valid: '+pw.valid_aspects.join(', ')+'</div>';
  h+='</div>';
  inspPopup=L.popup({maxWidth:isMobile?240:300,autoPanPaddingTopLeft:[10,50],autoPanPaddingBottomRight:[10,isMobile?120:20]}).setLatLng(e.latlng).setContent(h).openOn(map);
});
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
    od[o]=r;od[o+1]=g;od[o+2]=bl;od[o+3]=al;}
  ox.putImageData(oi,0,0);return oc.toDataURL();}
function update3dOverlay(){if(!map3d||!is3d)return;const sel=document.getElementById('overlay3d').value;
  if(sel==='none'){if(map3d.getLayer('data-overlay'))map3d.setLayoutProperty('data-overlay','visibility','none');return;}
  const url=make3dOverlay(sel);
  if(map3d.getSource('data-src')){map3d.getSource('data-src').updateImage({url:url,coordinates:[[loMin,laMax],[loMax,laMax],[loMax,laMin],[loMin,laMin]]});}
  else{map3d.addSource('data-src',{type:'image',url:url,coordinates:[[loMin,laMax],[loMax,laMax],[loMax,laMin],[loMin,laMin]]});
    map3d.addLayer({id:'data-overlay',type:'raster',source:'data-src',paint:{'raster-opacity':0.75,'raster-fade-duration':0}});}
  if(map3d.getLayer('data-overlay'))map3d.setLayoutProperty('data-overlay','visibility','visible');}
function init3D(){
  const wrap=document.getElementById('three-wrap');wrap.style.display='block';is3d=true;
  const lc=map.getCenter(),lz=map.getZoom();
  map3d=new maplibregl.Map({container:'map3d',
    style:{version:8,
      sources:{
        'swisstopo':{type:'raster',tiles:['https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe-winter/default/current/3857/{z}/{x}/{y}.jpeg'],tileSize:256,attribution:'© swisstopo'},
        'hillshade-tiles':{type:'raster',tiles:['https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissalti3d-reliefschattierung_monodirektional/default/current/3857/{z}/{x}/{y}.png'],tileSize:256},
        'terrain-dem':{type:'raster-dem',tiles:['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],tileSize:256,encoding:'terrarium',maxzoom:15}},
      layers:[
        {id:'swisstopo-base',type:'raster',source:'swisstopo',paint:{'raster-opacity':0.15}},
        {id:'hillshade',type:'raster',source:'hillshade-tiles',paint:{'raster-opacity':1.0}}],
      terrain:{source:'terrain-dem',exaggeration:exag3d}},
    center:[lc.lng,lc.lat],zoom:lz,pitch:60,bearing:-20,maxPitch:85,
    maxBounds:[[loMinW-0.1,laMinW-0.05],[loMaxW+0.1,laMaxW+0.05]],
    touchZoomRotate:true,touchPitch:true,dragRotate:true});
  map3d.addControl(new maplibregl.NavigationControl({visualizePitch:true,showCompass:true}),'top-left');
  map3d.addControl(new maplibregl.GeolocateControl({positionOptions:{enableHighAccuracy:true},trackUserLocation:false}),'top-left');
  map3d.scrollZoom.setWheelZoomRate(1/200);
  document.getElementById('btn3dFloat').classList.add('active');
  map3d.on('load',()=>update3dOverlay());
  map3d.on('moveend',sync3dTo2d);}
function close3D(){
  if(map3d){const c=map3d.getCenter(),z=map3d.getZoom();map.setView([c.lat,c.lng],z,{animate:false});}
  is3d=false;if(map3d){map3d.remove();map3d=null;}
  document.getElementById('three-wrap').style.display='none';
  document.getElementById('btn3dFloat').classList.remove('active');}
document.getElementById('btn3dFloat').onclick=e=>{e.stopPropagation();if(is3d)close3D();else init3D();};
document.getElementById('btn3dClose').onclick=()=>close3D();
document.getElementById('mapOpac3d').oninput=function(){if(!map3d)return;const v=this.value/100;
  map3d.setPaintProperty('swisstopo-base','raster-opacity',v);
  document.getElementById('mapOpacLbl').textContent=this.value+'%';};
document.getElementById('overlay3d').onchange=()=>update3dOverlay();
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
    L.circle([lat,lon],{radius:80,color:'#5b9cf5',weight:2,fillColor:'#5b9cf5',fillOpacity:.25}).addTo(map).on('add',function(){const c=this;setTimeout(()=>map.removeLayer(c),4000);});}
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
function dismissIntro(){const el=document.getElementById('intro');if(!el)return;
  const wait=Math.max(0,3000-(Date.now()-_introStart));
  setTimeout(()=>{el.classList.add('hide');setTimeout(()=>el.remove(),900);},wait);}
// --- Snow animation for intro ---
(function(){const w=document.getElementById('snowWrap');if(!w)return;
  for(let i=0;i<30;i++){const s=document.createElement('div');s.className='sf';
    s.style.left=Math.random()*100+'%';s.style.animationDuration=(2+Math.random()*4)+'s';
    s.style.animationDelay=Math.random()*3+'s';s.style.opacity=0.2+Math.random()*0.5;
    s.style.width=s.style.height=(2+Math.random()*4)+'px';w.appendChild(s);}})();
renderAll();dismissIntro();
// Update bottom panel height for legend positioning
requestAnimationFrame(()=>{document.documentElement.style.setProperty('--btm-h',document.getElementById('bottomPanel').offsetHeight+'px');});
</script></body></html>
"""
