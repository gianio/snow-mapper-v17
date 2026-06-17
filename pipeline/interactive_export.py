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
<meta name="theme-color" content="#f0f4f8"/>
<meta name="mobile-web-app-capable" content="yes"/>
<title>Swiss Snow Model</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,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"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/maplibre-gl@4.1.2/dist/maplibre-gl.js"></script>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.1.2/dist/maplibre-gl.css"/>
<style>
 :root{--fg:#1a2a3a;--fg2:#3a4a5a;--mut:#7a8a9a;--acc:#0070b8;--acc2:#005a9f;--bd:rgba(0,90,160,.12);--glass:rgba(255,255,255,.88);--glass2:rgba(245,248,252,.94);--glow:rgba(0,112,184,.15);--panel-h:52px}
 *{box-sizing:border-box}
 html,body{margin:0;padding:0;height:100%;width:100%;overflow:hidden;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:var(--fg);-webkit-tap-highlight-color:transparent;overscroll-behavior:none;background:#f0f4f8;position:fixed;top:0;left:0;right:0;bottom:0}
 #map{position:fixed;top:0;left:0;right:0;bottom:0;background:#f0f4f8}
 #flow{position:fixed;top:0;left:0;right:0;bottom:0;z-index:450;pointer-events:none}
 #layerBar{position:absolute;z-index:1000;top:0;left:0;right:0;display:flex;flex-direction:column;gap:6px;padding:calc(env(safe-area-inset-top,0px) + 10px) 12px 10px;scrollbar-width:none;background:linear-gradient(180deg,rgba(240,244,248,.95) 0%,rgba(240,244,248,.7) 80%,transparent 100%)}
 #topics{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
 #topics::-webkit-scrollbar{display:none}
 #topics button{border:1px solid var(--bd);background:rgba(255,255,255,.7);border-radius:14px;padding:10px 18px;cursor:pointer;font-size:16px;font-weight:600;min-height:46px;color:var(--fg2);transition:.15s;backdrop-filter:blur(6px);flex-shrink:0;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 #topics button:hover{border-color:var(--acc);background:rgba(255,255,255,.9)}
 #topics button.active{background:var(--acc2);color:#fff;border-color:var(--acc);box-shadow:0 2px 8px var(--glow)}
 #sublayers{display:flex;gap:5px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
 #sublayers::-webkit-scrollbar{display:none}
 #sublayers button{border:1px solid var(--bd);background:rgba(255,255,255,.6);border-radius:12px;padding:8px 14px;cursor:pointer;font-size:15px;min-height:42px;color:var(--fg2);transition:.15s;backdrop-filter:blur(4px);flex-shrink:0;white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,.04)}
 #sublayers button:hover{border-color:var(--acc);background:rgba(255,255,255,.85)}
 #sublayers button.active{background:rgba(0,112,184,.12);color:var(--acc);border-color:var(--acc);font-weight:600}
 #bottomPanel{position:absolute;z-index:1000;bottom:0;left:0;right:0;
   background:var(--glass);backdrop-filter:blur(18px) saturate(1.4);-webkit-backdrop-filter:blur(18px) saturate(1.4);border-top:1px solid var(--bd);box-shadow:0 -2px 16px rgba(0,0,0,.08);transition:none;padding-bottom:env(safe-area-inset-bottom,0px);overflow:hidden}
 #btmMain{padding:8px 12px 10px}
 #timeline{display:block;border:1px solid var(--bd);background:rgba(230,238,248,.6);border-radius:10px}
 #presets::-webkit-scrollbar{display:none}
 .winlbl{font-size:15px;margin-top:6px;font-weight:600;color:var(--fg2)}
 .seg{display:flex;flex-wrap:wrap;gap:6px}
 .seg button{border:1px solid var(--bd);background:rgba(255,255,255,.7);border-radius:14px;padding:10px 16px;cursor:pointer;font-size:16px;min-height:46px;color:var(--fg2);transition:.15s;backdrop-filter:blur(4px);flex-shrink:0;box-shadow:0 1px 4px rgba(0,0,0,.05)}
 .seg button:hover{border-color:var(--acc);background:rgba(255,255,255,.9)}
 .seg button.active{background:var(--acc2);color:#fff;border-color:var(--acc);font-weight:600;box-shadow:0 2px 8px var(--glow)}
 #tlToggle{position:absolute;top:4px;left:50%;transform:translateX(-50%);width:36px;height:5px;border-radius:3px;background:rgba(0,0,0,.15);cursor:ns-resize;z-index:1;touch-action:none}
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
 .legend{position:absolute;z-index:950;bottom:calc(var(--btm-h,80px) + 40px);left:12px;background:var(--glass);backdrop-filter:blur(16px) saturate(1.3);-webkit-backdrop-filter:blur(16px) saturate(1.3);border:1px solid var(--bd);padding:10px 12px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);font-size:13px;max-width:220px;line-height:1.5;color:var(--fg2);display:none}
 .legend.show{display:block}
 #legendBtn{position:absolute;z-index:960;bottom:var(--btm-h,80px);left:12px;width:40px;height:40px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--fg2);cursor:pointer;font-size:18px;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 6px rgba(0,0,0,.08)}
 #legendBtn:hover,#legendBtn.active{border-color:var(--acc);color:var(--acc)}
 .legend i{display:inline-block;width:12px;height:12px;margin-right:5px;vertical-align:-2px;border-radius:2px}
 .stn{background:rgba(255,255,255,.85);border:2px solid var(--acc);border-radius:11px;padding:2px 7px;font-size:13px;font-weight:700;color:var(--acc2);text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.12);white-space:nowrap}
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
 .leaflet-popup-content-wrapper{background:var(--glass2)!important;backdrop-filter:blur(14px)!important;-webkit-backdrop-filter:blur(14px)!important;border:1px solid var(--bd)!important;color:var(--fg)!important;box-shadow:0 4px 16px rgba(0,0,0,.1)!important;border-radius:14px!important}
 .leaflet-popup-content{margin:12px 14px!important}
 .leaflet-popup-tip{background:var(--glass2)!important}
 .leaflet-popup-close-button{color:var(--mut)!important;font-size:20px!important;width:28px!important;height:28px!important;line-height:28px!important}
 .leaflet-popup-close-button:hover{color:var(--fg)!important}
 #searchWrap{position:absolute;z-index:1100;top:calc(env(safe-area-inset-top,0px) + 130px);right:12px;width:220px;max-width:calc(100vw - 24px)}
 #searchWrap input{width:100%;padding:10px 14px 10px 34px;border-radius:12px;border:1px solid var(--bd);background:rgba(255,255,255,.85);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--fg);font-size:15px;outline:none;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 #searchWrap input::placeholder{color:var(--mut)}
 #searchWrap input:focus{border-color:var(--acc);background:rgba(255,255,255,.95);box-shadow:0 0 0 3px var(--glow)}
 #btn3dFloat{position:absolute;z-index:1000;bottom:var(--btm-h,80px);right:12px;padding:10px 20px;border-radius:14px;border:1px solid var(--bd);background:var(--glass);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--fg2);cursor:pointer;font-size:16px;font-weight:700;min-height:46px;box-shadow:0 1px 6px rgba(0,0,0,.08)}
 #btn3dFloat:hover,#btn3dFloat.active{border-color:var(--acc);color:var(--acc)}
 #stnToggleWrap{position:absolute;z-index:1050;top:calc(env(safe-area-inset-top,0px) + 178px);right:12px}
 #stnToggleWrap label{display:flex;align-items:center;gap:8px;font-size:15px;color:var(--fg2);cursor:pointer;padding:8px 12px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 1px 4px rgba(0,0,0,.06)}
 #stnToggleWrap input{width:18px;height:18px;accent-color:var(--acc)}
 #searchWrap .icn{position:absolute;left:12px;top:50%;transform:translateY(-50%);pointer-events:none;color:var(--mut);font-size:15px}
 #searchRes{position:absolute;top:100%;left:0;right:0;margin-top:4px;background:var(--glass2);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--bd);border-radius:12px;overflow:hidden;display:none;max-height:260px;overflow-y:auto;box-shadow:0 4px 16px rgba(0,0,0,.1)}
 #searchRes .sr{padding:10px 14px;cursor:pointer;font-size:15px;color:var(--fg2);border-bottom:1px solid rgba(0,0,0,.05);transition:.12s}
 #searchRes .sr:last-child{border-bottom:none}
 #searchRes .sr:hover,#searchRes .sr.sel{background:rgba(0,112,184,.08);color:var(--fg)}
 #searchRes .sr .sub{font-size:11px;color:var(--mut);margin-top:2px}
 #intro{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;flex-direction:column;background:linear-gradient(135deg,#00294a 0%,#00429a 40%,#0089b2 80%,#00c0d0 100%);transition:opacity .8s ease}
 #intro.hide{opacity:0;pointer-events:none}
 #intro h1{font-size:clamp(28px,5vw,48px);font-weight:800;letter-spacing:-.02em;color:#fff;margin:0;opacity:0;transform:translateY(20px);animation:introUp .7s .3s ease forwards}
 #intro .sub{font-size:clamp(13px,2vw,17px);color:rgba(255,255,255,.7);margin-top:8px;opacity:0;animation:introUp .6s .7s ease forwards}
 #intro .bar{width:140px;height:3px;border-radius:2px;background:linear-gradient(90deg,#fff,#80e0f0);margin-top:20px;opacity:0;transform:scaleX(0);animation:introBar .8s 1s ease forwards}
 @keyframes introUp{to{opacity:1;transform:translateY(0)}}
 @keyframes introBar{to{opacity:1;transform:scaleX(1)}}
 #intro .mtn{position:absolute;bottom:0;left:0;width:100%;height:40%;opacity:0;animation:introUp 1s .2s ease forwards}
 #intro .sources{font-size:clamp(10px,1.5vw,13px);color:rgba(255,255,255,.5);margin-top:16px;letter-spacing:.08em;opacity:0;animation:introUp .6s .9s ease forwards}
 #intro .snow-wrap{position:absolute;inset:0;overflow:hidden;pointer-events:none}
 .sf{position:absolute;top:-10px;width:6px;height:6px;background:white;border-radius:50%;opacity:.6;animation:sfDrop linear infinite}
 @keyframes sfDrop{0%{transform:translateY(0) translateX(0)}25%{transform:translateY(25vh) translateX(15px)}50%{transform:translateY(50vh) translateX(-10px)}75%{transform:translateY(75vh) translateX(20px)}100%{transform:translateY(110vh) translateX(5px)}}
 @media (max-width:560px){
   #layerBar{padding:calc(env(safe-area-inset-top,0px) + 8px) 10px 8px;gap:5px}
   #topics button{padding:8px 14px;font-size:15px;min-height:42px;border-radius:12px}
   #sublayers button{padding:7px 12px;font-size:14px;min-height:38px;border-radius:10px}
   #searchWrap{top:calc(env(safe-area-inset-top,0px) + 120px);right:8px;width:calc(100vw - 16px);max-width:200px}
   .icard{font-size:14px;max-width:calc(100vw - 50px);min-width:200px}
   .scard{font-size:14px;min-width:160px}
   .leaflet-popup-content-wrapper{max-width:calc(100vw - 32px)!important}
   .legend{max-width:180px;font-size:12px}
   #btn3dFloat{padding:10px 18px;font-size:16px;min-height:46px;border-radius:14px}
   #stnToggleWrap{top:calc(env(safe-area-inset-top,0px) + 168px);right:8px}
   #stnToggleWrap label{font-size:14px;padding:8px 10px}
   #legendBtn{width:40px;height:40px;font-size:18px}
   .seg button{padding:9px 14px;font-size:15px;min-height:44px}
   .itab{padding:10px 6px;font-size:14px}
 }
 @media (max-width:380px){
   #searchWrap{max-width:160px}
   .legend{max-width:140px;font-size:11px}
 }
</style></head><body>
<div id="intro"><div class="snow-wrap" id="snowWrap"></div><svg class="mtn" viewBox="0 0 800 200" preserveAspectRatio="none"><path d="M0,200 L80,110 L140,155 L240,55 L310,125 L390,35 L460,105 L540,55 L620,115 L700,65 L800,140 L800,200Z" fill="rgba(255,255,255,.04)"/><path d="M0,200 L100,130 L180,165 L300,80 L380,150 L470,85 L550,145 L660,95 L750,145 L800,165 L800,200Z" fill="rgba(255,255,255,.07)"/><path d="M0,200 L60,170 L160,145 L260,175 L360,130 L440,170 L520,150 L620,175 L720,155 L800,180 L800,200Z" fill="rgba(255,255,255,.03)"/></svg><h1>Swiss Snow Model</h1><div class="sub">Interactive Snow Forecast Map</div><div class="sources">swisstopo &middot; MeteoSwiss &middot; SLF &middot; Open-Meteo &middot; Copernicus</div><div class="bar"></div></div>
<div id="map"></div>
<canvas id="flow"></canvas>
<div id="layerBar">
  <div id="topics">
    <button data-t="ski">Ski</button>
    <button data-t="snow" class="active">Snow</button>
    <button data-t="temp">Temp</button>
    <button data-t="wind">Wind</button>
    <button data-t="rad">Radiation</button>
    <button data-t="terrain">Terrain</button>
  </div>
  <div id="sublayers"></div>
</div>
<div id="searchWrap"><span class="icn">&#x1F50D;</span><input id="searchIn" type="text" placeholder="Search location..." autocomplete="off"/><div id="searchRes"></div></div>
<div id="stnToggleWrap"><label><input type="checkbox" id="stnToggle" checked/> SLF Stations</label></div>
<button id="btn3dFloat">3D</button>
<div id="bottomPanel">
  <div id="tlToggle"></div>
  <div id="btmMain">
    <canvas id="timeline" width="900" height="200" style="width:100%;border-radius:10px;cursor:default"></canvas>
    <div class="winlbl" id="window" style="margin-top:6px"></div>
    <div class="seg" id="presets" style="gap:5px;margin-top:6px;overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;-webkit-overflow-scrolling:touch">
      <button data-d="24">24h</button>
      <button data-d="48" class="active">48h</button>
      <button data-d="72">72h</button>
      <button data-d="120">120h</button>
      <button id="btnSinceSnow">Last Snow</button>
      <button data-r="tomorrow">Tomorrow</button>
    </div>
  </div>
</div>
<button id="legendBtn" title="Toggle legend">&#x2139;</button><div class="legend" id="legend"></div>
<div id="three-wrap"><div id="map3d" style="width:100%;height:100%"></div><button id="btn3dClose">✕ 2D</button>
<div class="ctrl3d"><label style="display:flex;align-items:center;gap:8px;color:var(--fg2);font-size:16px">Relief <input id="mapOpac3d" type="range" min="0" max="100" value="30" style="width:100px;accent-color:var(--acc)"> Map <span id="mapOpacLbl">30%</span></label>
<button id="btn3dExag">×1.5</button>
<select id="overlay3d" style="padding:10px 14px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);font-size:16px;backdrop-filter:blur(10px);min-height:46px"><option value="none">No overlay</option><option value="snow">Snow</option><option value="temp">Temperature</option><option value="wind">Wind</option><option value="depth">Snow Depth</option><option value="powder">Powder</option></select></div>
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
function fmtTime(i){const d=new Date(M.times[Math.max(0,Math.min(T-1,i))]+'Z');const h=d.getUTCHours();return h===0?'12AM':h===12?'12PM':h<12?h+'AM':(h-12)+'PM';}
function drawTimeline(){const tc=document.getElementById('timeline'),ctx2=tc.getContext('2d'),cw=tc.width,ch=tc.height;
  ctx2.clearRect(0,0,cw,ch);
  const nx=nowIdx/T*cw;
  ctx2.fillStyle='rgba(200,210,225,.35)';ctx2.fillRect(0,0,nx,ch);
  ctx2.fillStyle='rgba(220,232,248,.25)';ctx2.fillRect(nx,0,cw-nx,ch);
  const x1=a/T*cw,x2=b/T*cw;ctx2.fillStyle='rgba(0,112,184,.1)';ctx2.fillRect(x1,0,x2-x1,ch);
  ctx2.font='13px system-ui';ctx2.textAlign='center';
  for(let t=0;t<T;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===0){const x=t/T*cw;
    ctx2.fillStyle=t>=nowIdx?'rgba(0,112,184,.12)':'rgba(0,0,0,.06)';ctx2.fillRect(x,0,1,ch);
    ctx2.fillStyle=t>=nowIdx?'rgba(0,90,160,.7)':'rgba(80,100,120,.5)';
    ctx2.fillText(d.toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'short'}),x+30,ch-4);}}
  let mx=0;for(const s of hSnow)if(s>mx)mx=s;mx=Math.max(.05,mx);
  const bw=Math.max(1.5,cw/T),barH=ch-30;
  for(let t=0;t<T;t++){const v=hSnow[t];if(v<.002)continue;const h=Math.max(1,v/mx*barH);const x=t/T*cw;
    const inSel=(t>=a&&t<b);const fut=t>=nowIdx;
    ctx2.fillStyle=inSel?(fut?'rgba(0,112,184,.85)':'rgba(80,150,200,.65)'):(fut?'rgba(0,112,184,.25)':'rgba(140,160,180,.2)');
    ctx2.fillRect(x,ch-22-h,Math.max(bw-.3,1),h);}
  for(const[s,e]of snowEvents){const xs=s/T*cw,xe=e/T*cw;ctx2.fillStyle='rgba(0,112,184,.25)';ctx2.fillRect(xs,ch-22,xe-xs,4);}
  const scW=40;ctx2.fillStyle='rgba(240,244,248,.7)';ctx2.fillRect(cw-scW,0,scW,ch-18);
  ctx2.font='11px system-ui';ctx2.textAlign='right';ctx2.fillStyle='rgba(60,80,100,.5)';
  for(let i=0;i<=4;i++){const frac=i/4;const cm=(mx*frac).toFixed(mx>=1?0:1);const y=ch-22-frac*barH;
    ctx2.fillText(cm,cw-4,y+4);if(i>0){ctx2.strokeStyle='rgba(0,0,0,.04)';ctx2.lineWidth=.5;ctx2.beginPath();ctx2.moveTo(0,y);ctx2.lineTo(cw-scW,y);ctx2.stroke();}}
  ctx2.fillStyle='rgba(60,80,100,.35)';ctx2.font='10px system-ui';ctx2.fillText('cm/h',cw-4,16);
  ctx2.strokeStyle='rgba(0,112,184,.5)';ctx2.lineWidth=2;ctx2.strokeRect(x1+.5,0,x2-x1-1,ch);
  ctx2.font='bold 14px system-ui';ctx2.fillStyle='rgba(0,90,160,.85)';
  const tA=fmtTime(a),tB=fmtTime(b-1);
  ctx2.textAlign='left';ctx2.fillText(tA,x1+4,20);
  ctx2.textAlign='right';ctx2.fillText(tB,x2-4,20);
  ctx2.strokeStyle='#e03030';ctx2.lineWidth=2.5;ctx2.beginPath();ctx2.moveTo(nx,0);ctx2.lineTo(nx,ch);ctx2.stroke();
  ctx2.fillStyle='#e03030';ctx2.font='bold 13px system-ui';ctx2.textAlign='center';ctx2.fillText('NOW',nx,38);
  ctx2.font='bold 11px system-ui';ctx2.globalAlpha=.3;
  if(nx>35){ctx2.textAlign='right';ctx2.fillStyle='#5a6a7a';ctx2.fillText('PAST',nx-8,54);}
  if(cw-nx>60){ctx2.textAlign='left';ctx2.fillStyle='#0070b8';ctx2.fillText('FORECAST',nx+8,54);}
  ctx2.globalAlpha=1;}
// Karte + Layer
const [laMin,loMin,laMax,loMax]=M.bounds;
const map=L.map('map',{zoomControl:false}).fitBounds([[laMin,loMin],[laMax,loMax]],{padding:[10,10]});
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
  document.getElementById('window').innerHTML=`${b-a}h window`;legend();}
const TOPICS={
  ski:[{l:'skiable',s:'avg',label:'Skiable'},{l:'powder',s:'avg',label:'Powder'}],
  snow:[{l:'snow',s:'avg',label:'New Snow'},{l:'depth',s:'avg',label:'Snow Depth'}],
  temp:[{l:'temp',s:'avg',label:'Mean'},{l:'temp',s:'max',label:'Max'},{l:'temp',s:'min',label:'Min'},{l:'temp',s:'sub0',label:'<0°C'},{l:'temp',s:'max05',label:'0-5°C'},{l:'tsurf',s:'avg',label:'Surface'}],
  wind:[{l:'wind',s:'avg',label:'Mean'},{l:'wind',s:'max',label:'Max'},{l:'wind',s:'min',label:'Min'},{l:'wind',s:'lt10',label:'<10 km/h'}],
  rad:[{l:'rad',s:'avg',label:'Clear-sky'},{l:'radsun',s:'avg',label:'Effective'},{l:'sun',s:'avg',label:'Sunshine'}],
  terrain:[{l:'slope',s:'avg',label:'Slope'},{l:'aspect',s:'avg',label:'Aspect'},{l:'rough',s:'avg',label:'Roughness'},{l:'shade',s:'avg',label:'Hillshade'}]
};
let curTopic='snow';
function setTopic(t,subIdx){
  curTopic=t;
  document.querySelectorAll('#topics button').forEach(x=>x.classList.toggle('active',x.dataset.t===t));
  const subs=document.getElementById('sublayers');
  const items=TOPICS[t];
  subs.innerHTML=items.map((s,i)=>'<button data-i="'+i+'"'+(i===(subIdx||0)?' class="active"':'')+'>'+s.label+'</button>').join('');
  subs.querySelectorAll('button').forEach(btn=>{
    btn.onclick=()=>{subs.querySelectorAll('button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');
      const sub=items[parseInt(btn.dataset.i)];layer=sub.l;stat=sub.s;renderAll();};
    btn.onmouseenter=()=>legend(items[parseInt(btn.dataset.i)].l);btn.onmouseleave=()=>legend();});
  const sel=items[subIdx||0];layer=sel.l;stat=sel.s;renderAll();
}
document.querySelectorAll('#topics button').forEach(btn=>{
  btn.onclick=()=>setTopic(btn.dataset.t);
  btn.onmouseenter=()=>legend(TOPICS[btn.dataset.t][0].l);btn.onmouseleave=()=>legend();});
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
// --- Timeline Drag ---
(function(){const tc=document.getElementById('timeline');let dragging=false,dragStartX=0,dragStartA=0,ws=0;
  function startDrag(e){const cx=e.touches?e.touches[0].clientX:e.clientX;const rect=tc.getBoundingClientRect();
    const x1=a/T*rect.width+rect.left,x2=b/T*rect.width+rect.left;
    if(cx>=x1-15&&cx<=x2+15){dragging=true;dragStartX=cx;dragStartA=a;ws=b-a;tc.style.cursor='grabbing';e.preventDefault();}}
  tc.addEventListener('mousedown',startDrag);tc.addEventListener('touchstart',startDrag,{passive:false});
  function onDrag(e){if(!dragging)return;e.preventDefault();const cx=e.touches?e.touches[0].clientX:e.clientX;
    const rect=tc.getBoundingClientRect();const delta=Math.round((cx-dragStartX)/rect.width*T);
    let na=Math.max(0,Math.min(T-ws,dragStartA+delta));a=na;b=na+ws;
    drawTimeline();document.getElementById('window').innerHTML=ws+'h window';}
  document.addEventListener('mousemove',onDrag);document.addEventListener('touchmove',onDrag,{passive:false});
  function endDrag(){if(dragging){dragging=false;tc.style.cursor='grab';renderAll();}}
  document.addEventListener('mouseup',endDrag);document.addEventListener('touchend',endDrag);
  tc.addEventListener('mousemove',function(e){if(dragging)return;const rect=tc.getBoundingClientRect();const cx=e.clientX;
    const x1=a/T*rect.width+rect.left,x2=b/T*rect.width+rect.left;
    tc.style.cursor=(cx>=x1-5&&cx<=x2+5)?'grab':'default';});
})();
document.getElementById('stnToggle').onchange=e=>{showStn=e.target.checked;renderStations();};
// --- Bottom Panel drag-to-resize ---
(function(){const bp=document.getElementById('bottomPanel'),tl=document.getElementById('tlToggle'),btm=document.getElementById('btmMain');
  let maxH=0,minH=14,curH=0,dragging=false,startY=0,startH=0;
  function updH(){document.documentElement.style.setProperty('--btm-h',bp.offsetHeight+'px');}
  function setH(h){h=Math.max(minH,Math.min(maxH,h));bp.style.height=h+'px';
    const show=h>minH+20;btm.style.display=show?'':'none';updH();}
  function initMax(){bp.style.height='';btm.style.display='';maxH=bp.offsetHeight;curH=maxH;updH();}
  requestAnimationFrame(initMax);
  window.addEventListener('resize',initMax);
  function onDown(e){dragging=true;startY=e.touches?e.touches[0].clientY:e.clientY;startH=bp.offsetHeight;e.preventDefault();}
  function onMove(e){if(!dragging)return;const cy=e.touches?e.touches[0].clientY:e.clientY;const dy=cy-startY;setH(startH-dy);e.preventDefault();}
  function onUp(){if(!dragging)return;dragging=false;curH=bp.offsetHeight;if(curH<minH+40)setH(minH);
    requestAnimationFrame(updH);}
  tl.addEventListener('mousedown',onDown);tl.addEventListener('touchstart',onDown,{passive:false});
  document.addEventListener('mousemove',onMove);document.addEventListener('touchmove',onMove,{passive:false});
  document.addEventListener('mouseup',onUp);document.addEventListener('touchend',onUp);
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
let inspPopup=null;
map.on('click',function(e){
  if(inspPopup){map.closePopup(inspPopup);inspPopup=null;}
  const lat=e.latlng.lat,lon=e.latlng.lng;
  const cx2=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy2=Math.round((laMax-lat)/(laMax-laMin)*(H-1));
  if(cx2<0||cx2>=W||cy2<0||cy2>=H)return;
  const p=cy2*W+cx2;
  const elev=melevv(p),asp=maspv(p),slp=mslpv(p),quad=aspectQ(asp);
  const ca=a*NP,cb=b*NP;const newSnow=cum[cb+p]-cum[ca+p];const cumDepth=cum[cb+p];
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
  let h='<div class="icard"><b>'+lat.toFixed(4)+'° N, '+lon.toFixed(4)+'° E</b> · <span style="color:var(--mut)">'+elev.toFixed(0)+' m</span>';
  h+='<div class="itabs">';
  h+='<div class="itab active" onclick="iTabSw(this,\'snow\')">Snow</div>';
  h+='<div class="itab" onclick="iTabSw(this,\'temp\')">Temp</div>';
  h+='<div class="itab" onclick="iTabSw(this,\'wind\')">Wind</div>';
  h+='<div class="itab" onclick="iTabSw(this,\'terrain\')">Terrain</div>';
  h+='</div>';
  h+='<div class="ipane active" data-p="snow"><div class="ig">';
  h+=R('New Snow',newSnow.toFixed(1)+' cm');
  h+=R('Cum. Depth',cumDepth.toFixed(0)+' cm');
  h+=R('Sunshine',sunSum.toFixed(1)+' h');
  h+='</div>';
  h+='<div class="ipow '+(pw.powdered?'yes':'no')+'">Powder: '+(pw.powdered?'YES':'NO')+(pw.quality==='reduced'?' (reduced)':'')+'</div>';
  if(pw.valid_aspects.length)h+='<div style="font-size:11px;margin-top:2px">Valid: '+pw.valid_aspects.join(', ')+'</div>';
  if(pw.reason_flags.length)h+='<div style="font-size:10px;color:var(--mut);margin-top:2px">'+pw.reason_flags.join(', ')+'</div>';
  h+='</div>';
  h+='<div class="ipane" data-p="temp"><div class="ig">';
  h+=R('T Air Ø',tmean.toFixed(1)+' °C');
  h+=R('T Air Min',tmin.toFixed(1)+' °C');
  h+=R('T Air Max',tmax.toFixed(1)+' °C');
  h+='<div class="isep"></div>';
  h+=R('T Surf Ø',tsMean.toFixed(1)+' °C');
  h+=R('T Surf Min',tsMin.toFixed(1)+' °C');
  h+=R('Freeze-Thaw',ftc+' cycles');
  h+='</div></div>';
  h+='<div class="ipane" data-p="wind"><div class="ig">';
  h+=R('Wind Ø',wmean.toFixed(0)+' km/h');
  h+=R('Wind Max',wmax.toFixed(0)+' km/h');
  h+=R('≈ Gust Max',(wmax*PD_GUST_FACTOR).toFixed(0)+' km/h');
  h+='</div></div>';
  h+='<div class="ipane" data-p="terrain"><div class="ig">';
  h+=R('Slope',slp.toFixed(0)+'°');
  h+=R('Aspect',quad+' ('+asp.toFixed(0)+'°)');
  h+=R('Roughness',roughv(p).toFixed(0)+' m');
  h+='<div class="isep"></div>';
  h+=R('Rad. Clear',solar.toFixed(0)+' Wh/m²/d');
  h+=R('Rad. Eff.',effRad.toFixed(0)+' Wh/m²/d');
  h+='<div class="isep"></div>';
  h+=R('Min Snow',needS.toFixed(0)+' cm');
  h+=R('Skiable',ratS>=1.0?'YES ('+ratS.toFixed(1)+'×)':'NO ('+ratS.toFixed(1)+'×)');
  h+='</div></div>';
  h+='</div>';
  inspPopup=L.popup({maxWidth:isMobile?280:320,autoPanPaddingTopLeft:[10,50],autoPanPaddingBottomRight:[10,isMobile?120:20]}).setLatLng(e.latlng).setContent(h).openOn(map);
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
        {id:'swisstopo-base',type:'raster',source:'swisstopo',paint:{'raster-opacity':0.3}},
        {id:'hillshade',type:'raster',source:'hillshade-tiles',paint:{'raster-opacity':0.7}}],
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
function dismissIntro(){const el=document.getElementById('intro');if(!el)return;
  const wait=Math.max(0,3000-(Date.now()-_introStart));
  setTimeout(()=>{el.classList.add('hide');setTimeout(()=>el.remove(),900);},wait);}
// --- Snow animation for intro ---
(function(){const w=document.getElementById('snowWrap');if(!w)return;
  for(let i=0;i<30;i++){const s=document.createElement('div');s.className='sf';
    s.style.left=Math.random()*100+'%';s.style.animationDuration=(2+Math.random()*4)+'s';
    s.style.animationDelay=Math.random()*3+'s';s.style.opacity=0.2+Math.random()*0.5;
    s.style.width=s.style.height=(2+Math.random()*4)+'px';w.appendChild(s);}})();
setTopic('snow',0);dismissIntro();
requestAnimationFrame(()=>{document.documentElement.style.setProperty('--btm-h',document.getElementById('bottomPanel').offsetHeight+'px');});
</script></body></html>
"""
