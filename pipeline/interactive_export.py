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
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
 :root{--fg:#0f1d2f;--fg2:#2c3e54;--mut:#6b7f96;--acc:#1a7fd4;--acc2:#0e5fa3;--bd:rgba(14,95,163,.1);--glass:rgba(255,255,255,.82);--glass2:rgba(248,251,255,.92);--glow:rgba(26,127,212,.12);--panel-h:52px;--r:14px;--r-lg:18px}
 *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
 html,body{margin:0;padding:0;height:100%;width:100%;overflow:hidden;font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--fg);overscroll-behavior:none;background:#edf2f8;position:fixed;inset:0;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 #map{position:fixed;top:0;left:0;right:0;bottom:var(--btm-h,0px);background:#fff;transition:bottom .28s cubic-bezier(.4,0,.2,1)}
 body.panel-dragging #map,body.panel-dragging #flow{transition:none}
 #flow{position:fixed;top:0;left:0;right:0;bottom:var(--btm-h,0px);z-index:450;pointer-events:none}
 /* --- Layer selector: one elegant floating glass card --- */
 #layerBar{position:absolute;z-index:1000;top:calc(env(safe-area-inset-top,0px) + 12px);left:12px;max-width:calc(100vw - 112px);display:inline-flex;flex-direction:column;padding:6px;background:rgba(255,255,255,.7);backdrop-filter:blur(24px) saturate(1.8);-webkit-backdrop-filter:blur(24px) saturate(1.8);border:1px solid rgba(255,255,255,.7);border-radius:20px;box-shadow:0 10px 34px rgba(15,29,47,.13),0 1px 0 rgba(255,255,255,.7) inset;--topic-accent:#1a7fd4;--topic-tint:rgba(26,127,212,.13)}
 #topics{display:flex;gap:2px;align-items:center;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
 #topics::-webkit-scrollbar{display:none}
 #topics button{display:inline-flex;align-items:center;gap:7px;border:none;background:transparent;border-radius:13px;padding:9px 14px;cursor:pointer;font-size:14px;font-weight:650;min-height:40px;color:var(--mut);transition:all .22s cubic-bezier(.4,0,.2,1);flex-shrink:0;white-space:nowrap;letter-spacing:-.01em;font-family:inherit}
 #topics button svg{width:18px;height:18px;flex-shrink:0;transition:color .2s}
 #topics button:hover{color:var(--fg)}
 #topics button.active{background:#fff;color:var(--fg);box-shadow:0 2px 9px rgba(15,29,47,.13),0 0 0 1px rgba(15,29,47,.04)}
 #topics button.active svg{color:var(--topic-accent)}
 #moreTopics{color:var(--mut);padding:9px 12px;gap:5px}
 #moreTopics .chev{width:13px!important;height:13px!important;transition:transform .25s;opacity:.65}
 #moreTopics.sel{background:#fff;color:var(--fg);box-shadow:0 2px 9px rgba(15,29,47,.13)}
 #moreTopics.sel>svg:first-child{color:var(--topic-accent)}
 #topics.expanded #moreTopics{background:rgba(15,29,47,.06);color:var(--fg)}
 #topics.expanded #moreTopics .chev{transform:rotate(180deg)}
 /* More dropdown: iconed, described (position set by JS) */
 #topicsMore{position:fixed;display:none;flex-direction:column;gap:1px;background:rgba(255,255,255,.98);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid rgba(15,29,47,.08);border-radius:16px;padding:6px;box-shadow:0 18px 46px rgba(15,29,47,.2);z-index:1200;min-width:214px;overflow-y:auto;scrollbar-width:none;animation:moreIn .18s cubic-bezier(.34,1.4,.64,1)}
 #topicsMore::-webkit-scrollbar{display:none}
 @keyframes moreIn{from{opacity:0;transform:translateY(-8px) scale(.97)}to{opacity:1;transform:translateY(0) scale(1)}}
 #topics.expanded #topicsMore{display:flex}
 #topicsMore button{display:flex;align-items:center;gap:12px;width:100%;justify-content:flex-start;background:transparent;border:none;border-radius:12px;padding:9px 10px;min-height:auto;cursor:pointer;font-family:inherit;text-align:left;transition:background .15s;color:var(--fg)}
 #topicsMore .mt-ic{width:36px;height:36px;border-radius:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
 #topicsMore .mt-ic svg{width:20px;height:20px}
 #topicsMore .mt-tx{display:flex;flex-direction:column;gap:1px;min-width:0}
 #topicsMore .mt-tx b{font-size:14px;font-weight:700;color:var(--fg);letter-spacing:-.01em}
 #topicsMore .mt-tx span{font-size:11.5px;color:var(--mut);font-weight:500;white-space:nowrap}
 #topicsMore button:hover{background:rgba(15,29,47,.045)}
 #topicsMore button.active{background:rgba(15,29,47,.06)}
 /* Sublayers: connected secondary segment row */
 #sublayers{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;margin-top:6px;padding-top:6px;border-top:1px solid rgba(15,29,47,.07)}
 #sublayers:empty{display:none}
 #sublayers::-webkit-scrollbar{display:none}
 #sublayers button{border:none;background:rgba(15,29,47,.05);border-radius:9px;padding:6px 12px;cursor:pointer;font-size:12.5px;font-weight:650;min-height:32px;color:var(--fg2);transition:all .18s;flex-shrink:0;white-space:nowrap;font-family:inherit;letter-spacing:-.01em}
 #sublayers button:hover{background:rgba(15,29,47,.09);color:var(--fg)}
 #sublayers button.active{background:var(--topic-tint);color:var(--topic-accent)}
 #bottomPanel{position:absolute;z-index:1000;bottom:0;left:0;right:0;
   background:rgba(255,255,255,.78);backdrop-filter:blur(20px) saturate(1.5);-webkit-backdrop-filter:blur(20px) saturate(1.5);border-top:1px solid rgba(255,255,255,.5);box-shadow:0 -1px 0 var(--bd),0 -4px 20px rgba(0,0,0,.06);transition:none;padding-bottom:env(safe-area-inset-bottom,0px);overflow:hidden}
 #btmMain{padding:10px 14px 12px}
 #timeline{display:block;border:1px solid var(--bd);background:rgba(237,242,248,.5);border-radius:var(--r)}
 #presets::-webkit-scrollbar{display:none}
 .winlbl{font-size:14px;font-weight:700;color:var(--fg);letter-spacing:-.01em}
 #tlHead{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:10px}
 #tlModeToggle{display:inline-flex;background:rgba(0,0,0,.05);border-radius:999px;padding:3px;flex-shrink:0}
 #tlModeToggle button{border:none;background:none;padding:5px 13px;border-radius:999px;font-size:12px;font-weight:600;color:var(--mut);cursor:pointer;font-family:inherit;transition:.15s}
 #tlModeToggle button.active{background:#fff;color:var(--fg);box-shadow:0 1px 3px rgba(0,0,0,.12)}
 #tlDetail{display:none}
 #bottomPanel.detail #tlDetail{display:block}
 #tlExtended{margin-top:12px}
 .tl-row{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--fg2)}
 .tl-lbl{font-weight:600}.tl-val{font-weight:700;color:var(--acc2)}
 #tlLen{width:100%;accent-color:var(--acc);margin:8px 0 12px;cursor:pointer}
 .tl-steprow{display:flex;gap:6px}
 .tl-step{flex:1;border:1px solid var(--bd);background:rgba(255,255,255,.7);border-radius:10px;padding:9px 6px;font-size:12px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;transition:.15s}
 .tl-step:hover{background:#fff;color:var(--fg)}
 .tl-range{margin-top:10px;font-size:11px;color:var(--mut);text-align:center;font-weight:500}
 .seg{display:flex;flex-wrap:wrap;gap:5px}
 .seg button{border:1.5px solid transparent;background:rgba(255,255,255,.6);border-radius:var(--r);padding:8px 14px;cursor:pointer;font-size:14px;font-weight:500;min-height:40px;color:var(--fg2);transition:all .2s cubic-bezier(.4,0,.2,1);flex-shrink:0}
 .seg button:hover{background:rgba(255,255,255,.95)}
 .seg button.active{background:var(--fg);color:#fff;border-color:var(--fg);font-weight:600;box-shadow:0 2px 8px rgba(15,29,47,.15)}
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
 #legendBtn{position:absolute;z-index:960;bottom:var(--btm-h,80px);left:12px;width:36px;height:36px;border-radius:10px;border:1px solid rgba(255,255,255,.5);background:rgba(255,255,255,.7);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--mut);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 #legendBtn:hover,#legendBtn.active{color:var(--acc);background:rgba(255,255,255,.9)}
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
 .leaflet-popup-content-wrapper{background:rgba(255,255,255,.9)!important;backdrop-filter:blur(16px) saturate(1.4)!important;-webkit-backdrop-filter:blur(16px) saturate(1.4)!important;border:1px solid rgba(255,255,255,.6)!important;color:var(--fg)!important;box-shadow:0 4px 24px rgba(0,0,0,.1),0 1px 0 rgba(255,255,255,.5) inset!important;border-radius:var(--r-lg)!important}
 .leaflet-popup-content{margin:12px 14px!important;font-size:14px!important}
 .leaflet-popup-tip{background:rgba(255,255,255,.9)!important}
 .leaflet-popup-close-button{color:var(--mut)!important;font-size:18px!important;width:28px!important;height:28px!important;line-height:28px!important}
 .leaflet-popup-close-button:hover{color:var(--fg)!important}
 /* Attribution: tiny, faint, unobtrusive (expand on hover) */
 .leaflet-control-attribution{background:rgba(255,255,255,.4)!important;color:rgba(80,100,120,.55)!important;font-size:9px!important;padding:1px 6px!important;border-radius:6px 0 0 0!important;box-shadow:none!important;backdrop-filter:blur(4px);max-width:22px;overflow:hidden;white-space:nowrap;transition:max-width .3s,background .3s}
 .leaflet-control-attribution:hover{max-width:80vw;background:rgba(255,255,255,.85)!important;color:var(--mut)!important}
 .leaflet-control-attribution a{color:inherit!important}
 #searchWrap{position:absolute;z-index:1100;top:calc(env(safe-area-inset-top,0px) + 58px);right:12px;width:230px;max-width:calc(100vw - 24px)}
 #searchWrap input{width:100%;padding:10px 12px 10px 34px;border-radius:12px;border:1px solid rgba(255,255,255,.6);background:rgba(255,255,255,.85);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);color:var(--fg);font-size:14px;font-weight:500;outline:none;box-shadow:0 2px 8px rgba(0,0,0,.07);font-family:inherit}
 #searchWrap input::placeholder{color:var(--mut);font-weight:400}
 #searchWrap input:focus{border-color:var(--acc);background:#fff;box-shadow:0 0 0 3px var(--glow)}
 #searchWrap .icn{position:absolute;left:12px;top:50%;transform:translateY(-50%);pointer-events:none;color:var(--mut);font-size:15px}
 /* Right-side control rail */
 #ctrlRail{position:absolute;z-index:1050;right:12px;top:calc(env(safe-area-inset-top,0px) + 110px);display:flex;flex-direction:column;gap:10px}
 .rail-btn{position:relative;width:46px;height:46px;border-radius:14px;border:1px solid rgba(255,255,255,.6);background:rgba(255,255,255,.85);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);color:var(--fg2);display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.09);transition:all .18s cubic-bezier(.34,1.56,.64,1)}
 .rail-btn:hover{background:#fff;color:var(--fg);transform:translateY(-1px)}
 .rail-btn:active{transform:scale(.92)}
 .rail-btn.active{background:var(--fg);color:#fff;border-color:var(--fg)}
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
 #intro{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;flex-direction:column;background:linear-gradient(160deg,#0a1628 0%,#0e3460 35%,#1a6090 65%,#2a8ab0 100%);transition:opacity .5s ease}
 #intro.hide{opacity:0;pointer-events:none}
 #intro h1{font-family:'Inter',sans-serif;font-size:clamp(26px,5vw,44px);font-weight:800;letter-spacing:-.03em;color:#fff;margin:0;opacity:0;transform:translateY(20px);animation:introUp .45s .1s ease forwards}
 #intro .sub{font-family:'Inter',sans-serif;font-size:clamp(12px,2vw,15px);color:rgba(255,255,255,.55);margin-top:8px;letter-spacing:.08em;text-transform:uppercase;opacity:0;animation:introUp .4s .3s ease forwards}
 #intro .bar{width:100px;height:2px;border-radius:1px;background:linear-gradient(90deg,rgba(255,255,255,.6),rgba(94,200,255,.8));margin-top:20px;opacity:0;transform:scaleX(0);animation:introBar .5s .45s ease forwards}
 @keyframes introUp{to{opacity:1;transform:translateY(0)}}
 @keyframes introBar{to{opacity:1;transform:scaleX(1)}}
 #intro .mtn{position:absolute;bottom:0;left:0;width:100%;height:40%;opacity:0;animation:introUp .6s .05s ease forwards}
 #intro .sources{font-size:clamp(10px,1.5vw,13px);color:rgba(255,255,255,.5);margin-top:16px;letter-spacing:.08em;opacity:0;animation:introUp .4s .5s ease forwards}
 #intro .snow-wrap{position:absolute;inset:0;overflow:hidden;pointer-events:none}
 .sf{position:absolute;top:-10px;width:6px;height:6px;background:white;border-radius:50%;opacity:.6;animation:sfDrop linear infinite}
 @keyframes sfDrop{0%{transform:translateY(0) translateX(0)}25%{transform:translateY(25vh) translateX(15px)}50%{transform:translateY(50vh) translateX(-10px)}75%{transform:translateY(75vh) translateX(20px)}100%{transform:translateY(110vh) translateX(5px)}}
 /* --- Onboarding coach marks --- */
 #coach{position:fixed;inset:0;z-index:8000}
 #coachSpot{position:absolute;border-radius:14px;box-shadow:0 0 0 9999px rgba(11,17,32,.6);transition:all .35s cubic-bezier(.4,0,.2,1);pointer-events:none}
 #coachCard{position:absolute;width:min(300px,calc(100vw - 32px));background:#fff;border-radius:18px;padding:18px 20px;box-shadow:0 16px 50px rgba(0,0,0,.3);transition:top .35s cubic-bezier(.4,0,.2,1),left .35s cubic-bezier(.4,0,.2,1);opacity:0;animation:coachIn .3s .1s ease forwards}
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
   #searchWrap{top:calc(env(safe-area-inset-top,0px) + 112px);right:8px;width:calc(100vw - 16px);max-width:210px}
   .icard{font-size:14px;max-width:calc(100vw - 50px);min-width:200px}
   .scard{font-size:14px;min-width:160px}
   .leaflet-popup-content-wrapper{max-width:calc(100vw - 32px)!important}
   .legend{max-width:180px;font-size:12px}
   #ctrlRail{top:calc(env(safe-area-inset-top,0px) + 162px);right:8px;gap:11px}
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
 #userBar{position:fixed;top:calc(env(safe-area-inset-top,0px)+12px);right:12px;z-index:1100;display:flex;gap:8px;align-items:center}
 .login-btn{padding:8px 16px;border-radius:999px;border:1.5px solid rgba(15,29,47,.12);background:rgba(255,255,255,.85);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:var(--fg);font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.06);letter-spacing:-.01em}
 .login-btn:hover{background:#fff}
 .user-pill{display:flex;align-items:center;gap:8px;padding:5px 14px 5px 5px;border-radius:999px;border:1.5px solid rgba(15,29,47,.08);background:rgba(255,255,255,.85);backdrop-filter:blur(12px);cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 .user-avatar{width:26px;height:26px;border-radius:50%;background:var(--fg);color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;letter-spacing:.02em}
 .user-name{font-size:13px;font-weight:600;color:var(--fg2);max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .auth-overlay{position:fixed;inset:0;z-index:5000;background:rgba(11,17,32,.5);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:16px}
 .auth-modal{position:relative;background:#fff;border-radius:24px;padding:36px 28px 28px;width:100%;max-width:360px;box-shadow:0 24px 80px rgba(0,0,0,.2),0 1px 0 rgba(255,255,255,.5) inset}
 .auth-modal h2{margin:0 0 4px;font-size:24px;font-weight:800;color:var(--fg);letter-spacing:-.03em}
 .auth-sub{font-size:13px;color:var(--mut);margin:0 0 24px}
 .auth-modal form{display:flex;flex-direction:column;gap:10px}
 .auth-modal input[type="text"],.auth-modal input[type="email"],.auth-modal input[type="password"]{width:100%;padding:13px 16px;border:1.5px solid rgba(0,0,0,.08);border-radius:var(--r);font-size:15px;font-family:inherit;color:var(--fg);background:#f5f7fa;outline:none;box-sizing:border-box;transition:border-color .15s,box-shadow .15s}
 .auth-modal input:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--glow);background:#fff}
 .auth-err{color:#d03030;font-size:13px;padding:8px 12px;background:rgba(220,60,60,.06);border-radius:10px;min-height:0}
 .auth-btn{padding:13px;border-radius:var(--r);border:none;font-size:15px;font-weight:700;cursor:pointer;width:100%;font-family:inherit;letter-spacing:-.01em;transition:all .15s}
 .auth-btn.primary{background:var(--fg);color:#fff}.auth-btn.primary:hover{background:#000}
 .auth-close{position:absolute;top:16px;right:16px;background:none;border:none;font-size:20px;color:var(--mut);cursor:pointer;width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center}
 .auth-close:hover{background:rgba(0,0,0,.05)}
 .auth-switch{text-align:center;margin-top:20px;font-size:13px;color:var(--mut)}
 .auth-switch button{background:none;border:none;color:var(--acc);font-weight:600;cursor:pointer;font-size:13px}
 .email-banner{position:fixed;top:calc(env(safe-area-inset-top,0px)+56px);left:12px;right:12px;z-index:1200;display:none;align-items:center;justify-content:space-between;gap:12px;padding:10px 16px;background:rgba(255,240,220,.95);border:1px solid rgba(200,150,50,.2);border-radius:12px;font-size:13px;color:#6a4a10;box-shadow:0 2px 8px rgba(0,0,0,.06)}
 .email-banner button{background:none;border:none;color:#0070b8;font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}
 /* --- Report FAB (primary action) --- */
 #reportFab{position:fixed;bottom:calc(var(--btm-h,80px) + 16px);right:16px;z-index:1000;width:58px;height:58px;border-radius:50%;border:none;background:var(--fg);color:#fff;cursor:pointer;display:none;align-items:center;justify-content:center;box-shadow:0 6px 22px rgba(15,29,47,.3);touch-action:none;transition:transform .2s cubic-bezier(.34,1.56,.64,1),box-shadow .2s;-webkit-tap-highlight-color:transparent}
 #reportFab:active{transform:scale(.9)}
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
 .report-sheet{position:relative;width:100%;max-width:440px;max-height:92vh;overflow-y:auto;background:#fff;border-radius:26px;box-shadow:0 30px 90px rgba(11,17,32,.35);-webkit-overflow-scrolling:touch;animation:rpSheetIn .28s cubic-bezier(.34,1.4,.64,1)}
 @keyframes rpSheetIn{from{opacity:0;transform:translateY(16px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
 .report-sheet .sh{display:none}
 .cat-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
 .cat-chip{display:flex;flex-direction:column;align-items:center;gap:7px;padding:14px 4px;border-radius:16px;border:1.5px solid rgba(15,29,47,.08);background:#f5f8fb;cursor:pointer;font-size:12px;font-weight:650;color:var(--mut);transition:all .2s cubic-bezier(.34,1.56,.64,1);-webkit-tap-highlight-color:transparent}
 .cat-chip:active{transform:scale(.94)}
 .cat-chip.active{border-color:currentColor;background:#fff;box-shadow:0 4px 16px rgba(15,29,47,.1)}
 .cat-chip .cat-ico-w{width:30px;height:30px;display:flex;align-items:center;justify-content:center}
 .cat-chip .cat-ico-w svg{width:26px;height:26px;stroke:currentColor}
 .sub-chips{display:flex;flex-wrap:wrap;gap:8px}
 .sub-chip{padding:11px 16px;border-radius:999px;border:1.5px solid rgba(15,29,47,.1);background:#f5f8fb;cursor:pointer;font-size:14px;font-weight:600;color:var(--fg2);transition:all .15s;-webkit-tap-highlight-color:transparent}
 .sub-chip:active{transform:scale(.95)}
 .sub-chip.active{background:var(--acc);border-color:var(--acc);color:#fff}
 .bucket-track{display:flex;gap:8px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;padding:4px 0}
 .bucket-track::-webkit-scrollbar{display:none}
 .bucket{min-width:64px;padding:14px 8px;border-radius:15px;border:1.5px solid rgba(15,29,47,.08);background:#f5f8fb;cursor:pointer;font-size:16px;font-weight:700;color:var(--fg2);text-align:center;flex-shrink:0;transition:all .2s cubic-bezier(.34,1.56,.64,1);-webkit-tap-highlight-color:transparent}
 .bucket:active{transform:scale(.95)}
 .bucket.active{background:var(--acc);border-color:var(--acc);color:#fff;box-shadow:0 4px 14px rgba(26,127,212,.3)}
 .bucket-val{font-size:12px;color:var(--mut);text-align:center;margin-top:8px;min-height:16px;font-weight:600}
 .rp-voice-btn{width:100%;padding:14px;border-radius:14px;border:1.5px solid rgba(15,29,47,.1);background:#f5f8fb;cursor:pointer;font-size:14px;font-weight:600;color:var(--fg2);display:flex;align-items:center;justify-content:center;gap:8px;transition:all .15s;-webkit-tap-highlight-color:transparent}
 .rp-voice-btn:active,.rp-voice-btn.recording{background:rgba(26,127,212,.1);border-color:var(--acc);color:var(--acc)}
 .rp-caption{width:100%;padding:14px;border:1.5px solid rgba(15,29,47,.1);border-radius:14px;font-size:15px;font-family:inherit;resize:none;outline:none;background:#f5f8fb;color:var(--fg);box-sizing:border-box;min-height:64px;margin-top:10px}
 .rp-caption::placeholder{color:var(--mut)}
 .rp-caption:focus{border-color:var(--acc);background:#fff;box-shadow:0 0 0 3px var(--glow)}
 .rp-wiz-head{display:flex;align-items:center;gap:12px;padding:16px 18px 0}
 .rp-nav-btn{width:36px;height:36px;border-radius:11px;border:none;background:rgba(15,29,47,.05);color:var(--fg2);display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:.15s}
 .rp-nav-btn svg{width:20px;height:20px}
 .rp-nav-btn:hover{background:rgba(15,29,47,.1);color:var(--fg)}
 .rp-nav-btn:disabled{opacity:.3;cursor:default}
 .rp-progress{flex:1;display:flex;gap:6px}
 .rp-progress i{flex:1;height:4px;border-radius:2px;background:rgba(15,29,47,.1);transition:background .25s}
 .rp-progress i.done{background:var(--acc)}
 .rp-progress i.cur{background:var(--acc);box-shadow:0 0 0 3px var(--glow)}
 .rp-step-title{font-size:21px;font-weight:800;color:var(--fg);letter-spacing:-.02em;padding:14px 20px 2px}
 .rp-step-sub{font-size:13px;color:var(--mut);padding:0 20px 4px}
 .rp-pane{padding:14px 20px 0;animation:rpPaneIn .25s ease}
 @keyframes rpPaneIn{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:translateX(0)}}
 .rp-photo-big{width:100%;aspect-ratio:4/3;max-height:40vh;border-radius:18px;background:#f5f8fb;border:2px dashed rgba(15,29,47,.14);cursor:pointer;overflow:hidden;display:flex;align-items:center;justify-content:center;transition:border-color .2s}
 .rp-photo-big:active{border-color:var(--acc)}
 .rp-photo-big.has-img{border:none;background:#eef2f6}
 .rp-photo-big img{width:100%;height:100%;object-fit:cover}
 .rp-photo-placeholder{display:flex;flex-direction:column;align-items:center;gap:12px;color:var(--acc)}
 .rp-photo-placeholder svg{width:52px;height:52px;opacity:.9}
 .rp-photo-placeholder span{font-size:15px;font-weight:700}
 .rp-loc-card{display:flex;align-items:center;gap:8px;padding:12px 14px;border-radius:14px;background:#f5f8fb;color:var(--fg2);font-size:13.5px;font-weight:600;margin-bottom:10px}
 .rp-loc-switch{margin-left:auto;background:none;border:none;color:var(--acc);font-weight:700;font-size:12.5px;cursor:pointer;font-family:inherit;flex-shrink:0;white-space:nowrap}
 .rp-peak{margin-bottom:12px;padding:14px 16px;border-radius:16px;background:rgba(26,127,212,.06);border:1.5px solid rgba(26,127,212,.18)}
 .rp-peak-q{font-size:15px;color:var(--fg);font-weight:700;margin-bottom:10px}
 .rp-peak-btns{display:flex;gap:8px}
 .rp-peak-btns button{flex:1;padding:11px;border-radius:11px;border:1.5px solid rgba(15,29,47,.12);background:#fff;color:var(--fg2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}
 .rp-peak-btns button.yes{background:var(--acc);color:#fff;border-color:var(--acc)}
 .rp-peak.confirmed{border-color:#25a35a;background:rgba(37,163,90,.08)}
 .rp-peak.confirmed .rp-peak-q{color:#1c7a44}
 .rp-group{display:flex;align-items:center;gap:10px;margin-top:12px}
 .rp-group-lbl{font-size:14px;color:var(--fg2);font-weight:650;flex-shrink:0}
 .rp-group-sel{flex:1;padding:11px 14px;border-radius:12px;border:1.5px solid rgba(15,29,47,.1);background:#f5f8fb;color:var(--fg);font-size:14px;font-family:inherit;outline:none;-webkit-appearance:none;appearance:none}
 .rp-summary{margin-top:14px;display:flex;flex-wrap:wrap;gap:6px}
 .rp-summary .rp-tag{padding:5px 11px;border-radius:999px;background:rgba(15,29,47,.05);color:var(--fg2);font-size:12.5px;font-weight:650;display:inline-flex;align-items:center;gap:5px}
 .rp-summary .rp-tag svg{width:13px;height:13px;stroke:var(--acc)}
 .rp-wiz-nav{display:flex;align-items:center;gap:10px;padding:16px 20px calc(env(safe-area-inset-bottom,0px)+16px);position:sticky;bottom:0;background:linear-gradient(180deg,rgba(255,255,255,0),#fff 32%)}
 .rp-skip{background:none;border:none;color:var(--mut);font-size:15px;font-weight:650;cursor:pointer;font-family:inherit;padding:12px}
 .rp-next{flex:1;padding:15px;border-radius:15px;border:none;background:var(--fg);color:#fff;font-size:16px;font-weight:800;cursor:pointer;font-family:inherit;letter-spacing:-.01em;transition:.15s}
 .rp-next:hover{background:#000}
 .rp-next:disabled{opacity:.35;cursor:default}
 .rp-next.post{background:var(--acc)}
 .rp-next.post:hover{background:var(--acc2)}
 .cat-chip .cat-ico-w{width:30px;height:30px;display:flex;align-items:center;justify-content:center}
 .cat-chip .cat-ico-w svg{width:26px;height:26px;stroke:currentColor}
 /* --- Undo snackbar --- */
 .undo-bar{position:fixed;bottom:calc(env(safe-area-inset-bottom,0px)+20px);left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:999px;background:rgba(20,30,51,.95);backdrop-filter:blur(12px);color:#E8EEF7;font-size:14px;font-weight:600;display:flex;align-items:center;gap:12px;box-shadow:0 4px 20px rgba(0,0,0,.3);z-index:5000;animation:undoIn .3s cubic-bezier(.34,1.56,.64,1)}
 .undo-bar button{background:none;border:none;color:#FF8A5B;font-weight:700;font-size:14px;cursor:pointer}
 @keyframes undoIn{from{opacity:0;transform:translateX(-50%) translateY(20px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
 /* --- Feed (full-page, Instagram-style) --- */
 .feed-page{position:fixed;inset:0;z-index:3000;background:#fafafa;transform:translateX(100%);transition:transform .35s cubic-bezier(.32,.72,.42,1);display:flex;flex-direction:column;will-change:transform}
 .feed-page.open{transform:translateX(0)}
 .feed-nav{display:flex;align-items:center;gap:12px;padding:12px 16px;background:#fff;border-bottom:1px solid rgba(0,0,0,.06);position:sticky;top:0;z-index:1;padding-top:calc(12px + env(safe-area-inset-top,0px))}
 .feed-back{background:none;border:none;cursor:pointer;padding:6px;display:flex;align-items:center;justify-content:center;color:var(--fg);border-radius:8px}
 .feed-back:hover{background:rgba(0,0,0,.04)}
 .feed-title{font-size:22px;font-weight:800;color:var(--fg);letter-spacing:-.04em;flex:1}
 .feed-filter{display:flex;gap:6px;padding:10px 16px;overflow-x:auto;scrollbar-width:none;background:#fff;border-bottom:1px solid rgba(0,0,0,.04)}
 .feed-filter::-webkit-scrollbar{display:none}
 .feed-filter button{padding:7px 16px;border-radius:999px;border:1.5px solid rgba(0,0,0,.08);background:#fff;font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;white-space:nowrap;flex-shrink:0;transition:all .15s;font-family:inherit;display:flex;align-items:center;gap:6px}
 .feed-filter button .cat-ico{width:16px;height:16px;display:flex;align-items:center;justify-content:center}
 .feed-filter button .cat-ico svg{width:14px;height:14px}
 .feed-filter button.active{background:var(--fg);color:#fff;border-color:var(--fg)}
 .feed-filter button.active .cat-ico svg{stroke:#fff}
 .feed-loc{display:flex;gap:8px;padding:0 16px 12px;overflow-x:auto;scrollbar-width:none;background:#fff}
 .feed-loc::-webkit-scrollbar{display:none}
 .feed-loc-btn,.feed-loc-sel,.feed-loc-clear{flex-shrink:0;padding:8px 14px;border-radius:999px;border:1.5px solid rgba(0,0,0,.08);background:#fff;font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px}
 .feed-loc-btn svg{width:15px;height:15px}
 .feed-loc-btn.active{background:var(--fg);color:#fff;border-color:var(--fg)}
 .feed-loc-sel{-webkit-appearance:none;appearance:none;padding-right:16px}
 .feed-loc-sel.active{background:var(--acc);color:#fff;border-color:var(--acc)}
 .feed-loc-clear{background:rgba(255,84,112,.1);color:#d03050;border-color:transparent}
 .feed-anchor-bar{padding:0 16px 12px;background:#fff;font-size:13px;color:var(--fg2);font-weight:600;display:flex;align-items:center;gap:6px}
 .feed-anchor-bar b{color:var(--acc2)}
 .feed-card-dist{font-size:11px;font-weight:700;color:var(--acc2);background:rgba(26,127,212,.1);padding:2px 8px;border-radius:999px;margin-left:auto;flex-shrink:0}
 /* Feed scope segmented control */
 .feed-scope{display:flex;gap:6px;padding:10px 16px 10px;overflow-x:auto;scrollbar-width:none;background:#fff}
 .feed-scope::-webkit-scrollbar{display:none}
 .feed-scope button{flex:1;min-width:max-content;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:9px 12px;border-radius:12px;border:none;background:rgba(0,0,0,.04);font-size:13px;font-weight:700;color:var(--mut);cursor:pointer;font-family:inherit;white-space:nowrap;transition:.15s}
 .feed-scope button svg{width:16px;height:16px}
 .feed-scope button.active{background:var(--fg);color:#fff}
 /* Feed group chips row */
 .feed-groups{display:flex;gap:8px;padding:0 16px 12px;overflow-x:auto;scrollbar-width:none;background:#fff}
 .feed-groups::-webkit-scrollbar{display:none}
 .feed-groups button{flex-shrink:0;padding:8px 14px;border-radius:999px;border:1.5px solid rgba(0,0,0,.08);background:#fff;font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px}
 .feed-groups button.active{background:var(--acc);color:#fff;border-color:var(--acc)}
 .feed-groups button.manage{background:rgba(0,0,0,.04);border-color:transparent;color:var(--fg2)}
 /* Like + follow on cards */
 .feed-card-actions button.liked{color:#e0245e}
 .feed-card-actions button.liked svg{fill:#e0245e;stroke:#e0245e}
 .feed-follow{margin-left:auto;flex-shrink:0;padding:5px 12px;border-radius:999px;border:1.5px solid var(--acc);background:none;color:var(--acc);font-size:12px;font-weight:700;cursor:pointer;font-family:inherit}
 .feed-follow.following{background:var(--acc);color:#fff}
 .feed-card-group{font-size:11px;font-weight:700;color:#7b1fa2;background:rgba(156,39,176,.1);padding:2px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:4px}
 /* Groups modal */
 .groups-modal{position:fixed;inset:0;z-index:3600;background:rgba(11,17,32,.5);backdrop-filter:blur(4px);display:flex;flex-direction:column;justify-content:flex-end}
 .groups-sheet{background:#fff;border-radius:22px 22px 0 0;max-height:80vh;display:flex;flex-direction:column;padding-bottom:calc(env(safe-area-inset-bottom,0px)+12px);box-shadow:0 -8px 40px rgba(0,0,0,.2)}
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
 .feed-scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding-bottom:env(safe-area-inset-bottom,0px)}
 .feed-grid{max-width:600px;margin:0 auto;padding:0}
 .feed-card{background:#fff;margin-bottom:8px;cursor:pointer}
 .feed-card-head{display:flex;align-items:center;gap:10px;padding:12px 16px}
 .feed-card-avatar{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;letter-spacing:.02em;color:#fff}
 .feed-card-info{flex:1;min-width:0}
 .feed-card-user{font-size:14px;font-weight:700;color:var(--fg);letter-spacing:-.01em;display:block}
 .feed-card-loc{font-size:12px;color:var(--mut);font-weight:500;display:flex;align-items:center;gap:3px}
 .feed-card-time{font-size:12px;color:var(--mut);font-weight:500;flex-shrink:0}
 .feed-card-visual{width:100%;aspect-ratio:4/3;position:relative;overflow:hidden;background:#edf2f8}
 .feed-card-visual img{width:100%;height:100%;object-fit:cover}
 .feed-card-visual .card-placeholder{width:100%;height:100%;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px}
 .feed-card-visual .card-placeholder>span:first-child svg{width:56px;height:56px;opacity:.5}
 .feed-card-visual .card-placeholder>span:last-child{font-size:15px;font-weight:700;opacity:.45;letter-spacing:.02em}
 .feed-card-body{padding:12px 16px 16px}
 .feed-card-badges{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
 .feed-badge{padding:4px 12px;border-radius:999px;font-size:13px;font-weight:600;display:flex;align-items:center;gap:5px}
 .feed-badge .cat-ico{width:14px;height:14px;display:flex;align-items:center;justify-content:center}
 .feed-badge .cat-ico svg{width:13px;height:13px}
 .feed-badge.cat-snow{background:rgba(56,152,236,.1);color:#1a7fd4}
 .feed-badge.cat-snow svg{stroke:#1a7fd4}
 .feed-badge.cat-route{background:rgba(76,175,80,.1);color:#2e7d32}
 .feed-badge.cat-route svg{stroke:#2e7d32}
 .feed-badge.cat-danger{background:rgba(255,84,112,.1);color:#d03050}
 .feed-badge.cat-danger svg{stroke:#d03050}
 .feed-badge.cat-tour{background:rgba(156,39,176,.1);color:#7b1fa2}
 .feed-badge.cat-tour svg{stroke:#7b1fa2}
 .feed-badge.cat-info{background:rgba(255,152,0,.1);color:#e65100}
 .feed-badge.cat-info svg{stroke:#e65100}
 .feed-card-caption{font-size:14px;color:var(--fg2);line-height:1.5}
 .feed-card-caption b{color:var(--fg);font-weight:700}
 .feed-card-actions{display:flex;align-items:center;gap:16px;padding:10px 16px 4px;border-top:none}
 .feed-card-actions button{background:none;border:none;cursor:pointer;padding:4px;color:var(--fg2);display:flex;align-items:center;gap:5px;font-size:13px;font-weight:600;font-family:inherit}
 .feed-card-actions button svg{width:20px;height:20px}
 .feed-card-actions button:hover{color:var(--fg)}
 .feed-divider{height:1px;background:rgba(0,0,0,.06)}
 .feed-empty{text-align:center;padding:80px 20px;color:var(--mut);font-size:15px}
 @media(min-width:561px){.feed-grid{padding:12px}.feed-card{border-radius:var(--r-lg);margin-bottom:12px;border:1px solid rgba(0,0,0,.06);overflow:hidden}}
 /* --- Report markers --- */
 .rpt-marker{width:36px;height:36px;border-radius:50%;background:#fff;border:2.5px solid currentColor;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.15);cursor:pointer;transition:transform .15s}
 .rpt-marker svg{width:18px;height:18px}
 .rpt-marker:hover{transform:scale(1.15)}
 @media(prefers-reduced-motion:reduce){.cat-chip,.sub-chip,.bucket,.slide-knob,.radial-seg{transition:none!important}}
</style></head><body>
<div id="intro"><div class="snow-wrap" id="snowWrap"></div><svg class="mtn" viewBox="0 0 800 200" preserveAspectRatio="none"><path d="M0,200 L80,110 L140,155 L240,55 L310,125 L390,35 L460,105 L540,55 L620,115 L700,65 L800,140 L800,200Z" fill="rgba(255,255,255,.04)"/><path d="M0,200 L100,130 L180,165 L300,80 L380,150 L470,85 L550,145 L660,95 L750,145 L800,165 L800,200Z" fill="rgba(255,255,255,.07)"/><path d="M0,200 L60,170 L160,145 L260,175 L360,130 L440,170 L520,150 L620,175 L720,155 L800,180 L800,200Z" fill="rgba(255,255,255,.03)"/></svg><h1>Swiss Snow Model</h1><div class="sub">Interactive Snow Forecast Map</div><div class="sources">swisstopo &middot; MeteoSwiss &middot; SLF &middot; Open-Meteo &middot; Copernicus</div><div class="bar"></div></div>
<div id="map"></div>
<canvas id="flow"></canvas>
<div id="layerBar">
  <div id="topics">
    <button data-t="ski"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M5 20l5-11 4 6 2.5-4L21 20"/><circle cx="15.5" cy="4.5" r="1.5"/></svg><span>Ski</span></button>
    <button data-t="snow" class="active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><line x1="12" y1="3" x2="12" y2="21"/><line x1="5" y1="7.5" x2="19" y2="16.5"/><line x1="5" y1="16.5" x2="19" y2="7.5"/></svg><span>Snow</span></button>
    <button id="moreTopics" title="More conditions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h16M4 6h16M4 18h16"/></svg><span>More</span><svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button>
    <span id="topicsMore">
      <button data-t="temp"><span class="mt-ic" style="background:rgba(232,89,12,.12);color:#e8590c"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 14.76V4a2 2 0 1 0-4 0v10.76a4 4 0 1 0 4 0z"/></svg></span><span class="mt-tx"><b>Temperatur</b><span>Luft &amp; Oberfläche</span></span></button>
      <button data-t="wind"><span class="mt-ic" style="background:rgba(13,148,136,.12);color:#0d9488"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.6 4.6A2 2 0 1 1 11 8H2M12.6 19.4A2 2 0 1 0 14 16H2M17.6 7.6A2.5 2.5 0 1 1 19 12H2"/></svg></span><span class="mt-tx"><b>Wind</b><span>Geschwindigkeit &amp; Böen</span></span></button>
      <button data-t="rad"><span class="mt-ic" style="background:rgba(245,158,11,.14);color:#f59e0b"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></span><span class="mt-tx"><b>Strahlung</b><span>Einstrahlung &amp; Sonne</span></span></button>
      <button data-t="terrain"><span class="mt-ic" style="background:rgba(100,116,139,.14);color:#64748b"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20l6-11 3.5 6M11 20l4-8 6 8z"/></svg></span><span class="mt-tx"><b>Gelände</b><span>Hang, Exposition, Relief</span></span></button>
    </span>
  </div>
  <div id="sublayers"></div>
</div>
<div id="searchWrap"><span class="icn">&#x1F50D;</span><input id="searchIn" type="text" placeholder="Search location..." autocomplete="off"/><div id="searchRes"></div></div>
<div id="ctrlRail">
  <button class="rail-btn" id="feedBtn" onclick="feedOpen()" title="Community feed"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v10a2 2 0 0 1-2 2H8l-4 3.5V5z"/><line x1="8" y1="9.5" x2="16" y2="9.5"/><line x1="8" y1="13" x2="13" y2="13"/></svg></button>
  <button class="rail-btn" id="railToggles" title="Show / hide markers"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="2.5"/></svg></button>
  <button class="rail-btn" id="btn3dFloat" title="3D terrain"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.5l8.5 4.75v9.5L12 21.5l-8.5-4.75v-9.5L12 2.5z"/><path d="M12 12l8.5-4.75M12 12v9.5M12 12L3.5 7.25"/></svg></button>
  <div id="togglePop">
    <label><input type="checkbox" id="stnToggle" checked/> SLF Stations</label>
    <label><input type="checkbox" id="rptToggle" checked onchange="toggleReportLayer(this.checked)"/> Reports</label>
  </div>
</div>
<div id="bottomPanel">
  <div id="tlToggle"></div>
  <div id="btmMain">
    <div id="tlHead">
      <span class="winlbl" id="window"></span>
      <div id="tlModeToggle">
        <button data-m="simple" class="active">Simple</button>
        <button data-m="detail">Detailed</button>
      </div>
    </div>
    <div class="seg" id="presets" style="gap:5px;margin-top:2px;overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;-webkit-overflow-scrolling:touch">
      <button data-d="24">24h</button>
      <button data-d="48" class="active">48h</button>
      <button data-d="72">72h</button>
      <button data-d="120">120h</button>
      <button id="btnSinceSnow">Last Snow</button>
      <button data-r="tomorrow">Till tomorrow</button>
    </div>
    <div id="tlDetail">
      <canvas id="timeline" width="900" height="120" style="width:100%;height:120px;border-radius:10px;cursor:default;margin-top:10px"></canvas>
      <div id="tlExtended">
        <div class="tl-row"><span class="tl-lbl">Window length</span><span id="tlLenVal" class="tl-val">48h</span></div>
        <input type="range" id="tlLen" min="6" max="168" step="6" value="48"/>
        <div class="tl-steprow">
          <button id="tlPrev" class="tl-step">◀ Earlier</button>
          <button id="tlNow" class="tl-step">Now</button>
          <button id="tlNext" class="tl-step">Later ▶</button>
        </div>
        <div id="tlRange" class="tl-range"></div>
      </div>
    </div>
  </div>
</div>
<button id="legendBtn" title="Toggle legend">&#x2139;</button><div class="legend" id="legend"></div>
<div id="three-wrap"><div id="map3d" style="width:100%;height:100%"></div><button id="btn3dClose">✕ 2D</button>
<div class="ctrl3d"><label style="display:flex;align-items:center;gap:8px;color:var(--fg2);font-size:16px">Relief <input id="mapOpac3d" type="range" min="0" max="100" value="30" style="width:100px;accent-color:var(--acc)"> Map <span id="mapOpacLbl">30%</span></label>
<button id="btn3dExag">×1.5</button>
<select id="overlay3d" style="padding:10px 14px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);font-size:16px;backdrop-filter:blur(10px);min-height:46px"><option value="none">No overlay</option><option value="snow">Snow</option><option value="temp">Temperature</option><option value="wind">Wind</option><option value="depth">Snow Depth</option><option value="powder">Powder</option></select>
<span id="keys3d" style="font-size:12px;color:var(--mut);opacity:.7;padding:6px 10px;display:none">WASD/Arrows: rotate · +/-: zoom · R: reset</span></div>
</div>
<!-- Auth & Reports UI -->
<div id="userBar"><button class="login-btn" id="btnLogin" onclick="authShow()">Sign in</button></div>
<div class="email-banner" id="emailBanner"><span>Please confirm your email to post reports.</span><button onclick="authResend()">Resend</button></div>
<button id="reportFab" title="Add a report"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
<div class="radial-wrap" id="radialWrap"><div class="radial-bg"></div><div class="radial-ring" id="radialRing"></div><div class="radial-center" id="radialCenter">✕</div></div>
<div class="auth-overlay" id="authOverlay" style="display:none" onclick="authHide()">
<div class="auth-modal" onclick="event.stopPropagation()">
<button class="auth-close" onclick="authHide()">&times;</button>
<h2 id="authTitle">Sign in</h2>
<p class="auth-sub" id="authSub">Sign in to post field reports</p>
<form id="authForm" onsubmit="authSubmit(event)">
<input type="text" id="authUser" placeholder="Username" autocomplete="username" style="display:none"/>
<input type="email" id="authEmail" placeholder="Email" autocomplete="email" required/>
<input type="password" id="authPass" placeholder="Password" autocomplete="current-password" required minlength="8"/>
<div class="auth-err" id="authErr"></div>
<button class="auth-btn primary" type="submit" id="authSubmitBtn">Sign in</button>
</form>
<div class="auth-switch" id="authSwitch">No account? <button onclick="authToggle()">Register</button></div>
</div>
</div>
<div class="report-overlay" id="reportOverlay" style="display:none">
<div class="ro-bg" onclick="reportClose()"></div>
<div class="report-sheet" onclick="event.stopPropagation()">
<div class="sh"></div>
<div class="rp-wiz-head">
  <button class="rp-nav-btn" id="rpBack" onclick="rpStepPrev()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg></button>
  <div class="rp-progress" id="rpProgress"></div>
  <button class="rp-nav-btn" onclick="reportClose()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
</div>
<div class="rp-step-title" id="rpStepTitle">Foto hinzufügen</div>
<div class="rp-step-sub" id="rpStepSub">Zeig, was du siehst (optional)</div>
<input type="file" id="rpFile" accept="image/*" onchange="rpSetPhoto(this)" hidden/>

<!-- STEP: photo -->
<div class="rp-pane" data-step="photo">
  <div class="rp-photo-big" id="rpPhotoBig" onclick="document.getElementById('rpFile').click()">
    <div class="rp-photo-placeholder" id="rpPhotoPlaceholder">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
      <span>Foto aufnehmen</span>
    </div>
  </div>
</div>

<!-- STEP: cat -->
<div class="rp-pane" data-step="cat" style="display:none">
  <div class="cat-grid" id="rpCats"></div>
</div>

<!-- STEP: sub -->
<div class="rp-pane" data-step="sub" style="display:none">
  <div class="sub-chips" id="rpSubs"></div>
</div>

<!-- STEP: bucket -->
<div class="rp-pane" data-step="bucket" style="display:none">
  <div class="bucket-track" id="rpBuckets"></div>
  <div class="bucket-val" id="rpBucketVal"></div>
</div>

<!-- STEP: final -->
<div class="rp-pane" data-step="final" style="display:none">
  <div class="rp-loc-card" id="rpLocCard"><span id="rpCtxLoc">📍 Standort wird ermittelt…</span></div>
  <div class="rp-peak" id="rpPeakConfirm" style="display:none"></div>
  <button class="rp-voice-btn" id="rpVoiceBtn" onclick="rpVoiceToggle()">🎤 Halten und sprechen</button>
  <textarea class="rp-caption" id="rpCaption" placeholder="Kurz beschreiben (optional)…" oninput="rpState.caption=this.value"></textarea>
  <div class="rp-group" id="rpGroupWrap" style="display:none"><span class="rp-group-lbl">Teilen in</span><select class="rp-group-sel" id="rpGroupSel" onchange="rpState.group=this.value||null"><option value="">🌍 Öffentlich</option></select></div>
  <div class="rp-summary" id="rpSummary"></div>
</div>

<div class="rp-wiz-nav">
  <button class="rp-skip" id="rpSkip" onclick="rpStepNext()">Überspringen</button>
  <button class="rp-next" id="rpNext" onclick="rpStepNext()">Weiter</button>
</div>
</div></div>
</div>
<div class="feed-page" id="feedPage">
<div class="feed-nav">
<button class="feed-back" onclick="feedClose()"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>
<span class="feed-title">Field Reports</span>
</div>
<div class="feed-scope" id="feedScope"></div>
<div class="feed-filter" id="feedFilter"></div>
<div class="feed-loc" id="feedLoc" style="display:none">
  <button class="feed-loc-btn" id="feedNear"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg> In der Nähe</button>
  <select class="feed-loc-sel" id="feedPeak"><option value="">⛰ Gipfel wählen…</option></select>
  <select class="feed-loc-sel" id="feedDest"><option value="">🎿 Skigebiet wählen…</option></select>
  <button class="feed-loc-clear" id="feedAnchorClear" style="display:none">✕ Filter</button>
</div>
<div class="feed-groups" id="feedGroups" style="display:none"></div>
<div class="feed-anchor-bar" id="feedAnchorBar" style="display:none"></div>
<div class="feed-scroll"><div class="feed-grid" id="feedList"><div class="feed-empty">Loading reports...</div></div></div>
</div>
<div class="groups-modal" id="groupsModal" style="display:none" onclick="if(event.target===this)groupsClose()">
  <div class="groups-sheet">
    <div class="groups-head"><span>Gruppen</span><button onclick="groupsClose()">✕</button></div>
    <div class="groups-new"><input id="groupNewName" type="text" placeholder="Neue Gruppe erstellen…" maxlength="40"/><button onclick="createGroup()">Erstellen</button></div>
    <div class="groups-list" id="groupsList"></div>
  </div>
</div>
<div id="coach" style="display:none"><div id="coachSpot"></div><div id="coachCard"><div id="coachText"></div><div id="coachNav"><span id="coachDots"></span><button id="coachSkip">Überspringen</button><button id="coachNext">Weiter</button></div></div></div>
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
function drawTimeline(){const tc=document.getElementById('timeline');const rect=tc.getBoundingClientRect();
  if(!rect.width)return;
  const cw=rect.width,ch=rect.height||120,dpr=window.devicePixelRatio||1;
  if(tc.width!==Math.round(cw*dpr)||tc.height!==Math.round(ch*dpr)){tc.width=Math.round(cw*dpr);tc.height=Math.round(ch*dpr);}
  const ctx2=tc.getContext('2d');ctx2.setTransform(dpr,0,0,dpr,0,0);ctx2.clearRect(0,0,cw,ch);
  function rr(x,y,w,h,r){r=Math.max(0,Math.min(r,w/2,h/2));ctx2.beginPath();if(ctx2.roundRect){ctx2.roundRect(x,y,w,h,r);}else{ctx2.moveTo(x+r,y);ctx2.arcTo(x+w,y,x+w,y+h,r);ctx2.arcTo(x+w,y+h,x,y+h,r);ctx2.arcTo(x,y+h,x,y,r);ctx2.arcTo(x,y,x+w,y,r);ctx2.closePath();}}
  const nx=nowIdx/T*cw,x1=a/T*cw,x2=b/T*cw,baseY=ch-26;
  // soft selection band (rounded)
  ctx2.fillStyle='rgba(26,127,212,.08)';rr(x1,2,x2-x1,ch-4,11);ctx2.fill();
  // day gridlines + readable date labels
  ctx2.textAlign='left';
  for(let t=0;t<T;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===0){const x=t/T*cw;
    ctx2.strokeStyle='rgba(15,29,47,.05)';ctx2.lineWidth=1;ctx2.beginPath();ctx2.moveTo(x,24);ctx2.lineTo(x,baseY);ctx2.stroke();
    ctx2.fillStyle='rgba(70,90,110,.7)';ctx2.font='700 12.5px Inter,system-ui';
    ctx2.fillText(d.toLocaleDateString('en-GB',{weekday:'short',day:'2-digit'}),x+7,ch-8);}}
  // baseline
  ctx2.strokeStyle='rgba(15,29,47,.09)';ctx2.lineWidth=1;ctx2.beginPath();ctx2.moveTo(0,baseY+.5);ctx2.lineTo(cw,baseY+.5);ctx2.stroke();
  // snowfall bars — rounded tops, vertical gradient
  let mx=0;for(const s of hSnow)if(s>mx)mx=s;mx=Math.max(.05,mx);
  const barH=ch-52,bw=Math.max(2,cw/T);
  const gSel=ctx2.createLinearGradient(0,baseY-barH,0,baseY);gSel.addColorStop(0,'#54aef0');gSel.addColorStop(1,'#0e5fa3');
  const gSelPast=ctx2.createLinearGradient(0,baseY-barH,0,baseY);gSelPast.addColorStop(0,'#a9cde8');gSelPast.addColorStop(1,'#6f9cc0');
  for(let t=0;t<T;t++){const v=hSnow[t];if(v<.002)continue;const h=Math.max(2,v/mx*barH);const x=t/T*cw;
    const inSel=(t>=a&&t<b),fut=t>=nowIdx;
    ctx2.fillStyle=inSel?(fut?gSel:gSelPast):(fut?'rgba(26,127,212,.2)':'rgba(150,165,185,.2)');
    rr(x+.5,baseY-h,Math.max(bw-1,1.4),h,Math.min(2.5,bw/2.2));ctx2.fill();}
  // max label
  ctx2.fillStyle='rgba(90,110,130,.6)';ctx2.font='600 12px Inter,system-ui';ctx2.textAlign='right';
  ctx2.fillText('max '+mx.toFixed(mx>=1?0:1)+' cm/h',cw-8,17);
  // selection frame + rounded grab handles
  ctx2.strokeStyle='rgba(26,127,212,.5)';ctx2.lineWidth=2;rr(x1+1,2,x2-x1-2,ch-4,11);ctx2.stroke();
  const hh=30,hy=(ch-hh)/2;ctx2.fillStyle='#1a7fd4';
  rr(x1-3.5,hy,7,hh,3.5);ctx2.fill();rr(x2-3.5,hy,7,hh,3.5);ctx2.fill();
  ctx2.fillStyle='rgba(255,255,255,.92)';for(let i=-1;i<=1;i++){ctx2.fillRect(x1-1.25,hy+hh/2+i*5,2.5,2.4);ctx2.fillRect(x2-1.25,hy+hh/2+i*5,2.5,2.4);}
  // selected start / end times
  ctx2.font='800 15px Inter,system-ui';ctx2.fillStyle='#0e5fa3';
  ctx2.textAlign='left';ctx2.fillText(fmtTime(a),x1+9,20);
  ctx2.textAlign='right';ctx2.fillText(fmtTime(b-1),x2-9,20);
  // NOW marker: dashed line + pill at top
  ctx2.strokeStyle='#e0245e';ctx2.lineWidth=2;ctx2.setLineDash([3,3]);ctx2.beginPath();ctx2.moveTo(nx,20);ctx2.lineTo(nx,baseY);ctx2.stroke();ctx2.setLineDash([]);
  const nlx=Math.max(20,Math.min(cw-20,nx));ctx2.fillStyle='#e0245e';rr(nlx-18,3,36,15,7.5);ctx2.fill();
  ctx2.fillStyle='#fff';ctx2.font='800 10px Inter,system-ui';ctx2.textAlign='center';ctx2.fillText('NOW',nlx,13.5);}
// Karte + Layer
const [laMin,loMin,laMax,loMax]=M.bounds;
const map=L.map('map',{zoomControl:false,zoomSnap:0,zoomDelta:.5,wheelPxPerZoomLevel:90,maxBoundsViscosity:1.0,inertia:true}).fitBounds([[laMin,loMin],[laMax,loMax]],{padding:[6,6]});
// Constrain panning + zoom to the meteo grid, with a thin white frame around the data
const _fitZoom=map.getZoom();
map.setMinZoom(_fitZoom);map.setMaxZoom(16);  // never zoom out past the initial view
const _padLa=(laMax-laMin)*0.04,_padLo=(loMax-loMin)*0.04;
map.setMaxBounds([[laMin-_padLa,loMin-_padLo],[laMax+_padLa,loMax+_padLo]]);
const base=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg",{attribution:"© swisstopo / MeteoSwiss / SLF / Copernicus"}).addTo(map);
// White mask outside the meteo grid → clean, smooth map ending
map.createPane('maskPane');map.getPane('maskPane').style.zIndex=350;map.getPane('maskPane').style.pointerEvents='none';
const _world=[[-89,-360],[-89,360],[89,360],[89,-360]];
const _hole=[[laMin,loMin],[laMin,loMax],[laMax,loMax],[laMax,loMin]];
L.polygon([_world,_hole],{pane:'maskPane',stroke:false,fillColor:'#ffffff',fillOpacity:1,interactive:false}).addTo(map);
L.rectangle([[laMin,loMin],[laMax,loMax]],{pane:'maskPane',fill:false,color:'rgba(15,29,47,.12)',weight:1,interactive:false}).addTo(map);
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
map.on('zoomend',()=>{const t=detailTier();if(t!==_lastTier){_lastTier=t;renderStations();loadReportMarkers();}});
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
function renderAll(){showOverlay();renderRaster();renderStations();if(tlMode==='detail')drawTimeline();
  if(layer=="rad"||layer=="radsun")renderRadiation();
  if(layer=="wind"){buildFlow();if(wtimer)clearTimeout(wtimer);wtimer=setTimeout(renderWind,120);}
  document.getElementById('window').innerHTML=`${b-a}h window`;syncTl();legend();}
// --- Simple / Detailed timeline mode ---
let tlMode='simple',sliderActive=false;
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
    document.getElementById('tlLenVal').textContent=(b-a)+'h';drawTimeline();document.getElementById('window').innerHTML=(b-a)+'h window';document.getElementById('tlRange').textContent=fmt(a)+'  →  '+fmt(b-1);});
  sl.addEventListener('change',()=>{sliderActive=false;clearPresets();renderAll();});
  document.getElementById('tlPrev').onclick=()=>{const ws=b-a;a=Math.max(0,a-ws);b=Math.min(T,a+ws);clearPresets();renderAll();};
  document.getElementById('tlNext').onclick=()=>{const ws=b-a;b=Math.min(T,b+ws);a=Math.max(0,b-ws);clearPresets();renderAll();};
  document.getElementById('tlNow').onclick=()=>{const ws=b-a;a=Math.max(0,Math.min(T-ws,nowIdx-Math.floor(ws/2)));b=Math.min(T,a+ws);clearPresets();renderAll();};
})();
const TOPICS={
  ski:[{l:'skiable',s:'avg',label:'Skiable'},{l:'powder',s:'avg',label:'Powder'}],
  snow:[{l:'snow',s:'avg',label:'New Snow'},{l:'depth',s:'avg',label:'Snow Depth'}],
  temp:[{l:'temp',s:'avg',label:'Mean'},{l:'temp',s:'max',label:'Max'},{l:'temp',s:'min',label:'Min'},{l:'temp',s:'sub0',label:'<0°C'},{l:'temp',s:'max05',label:'0-5°C'},{l:'tsurf',s:'avg',label:'Surface'}],
  wind:[{l:'wind',s:'avg',label:'Mean'},{l:'wind',s:'max',label:'Max'},{l:'wind',s:'min',label:'Min'},{l:'wind',s:'lt10',label:'<10 km/h'}],
  rad:[{l:'rad',s:'avg',label:'Clear-sky'},{l:'radsun',s:'avg',label:'Effective'},{l:'sun',s:'avg',label:'Sunshine'}],
  terrain:[{l:'slope',s:'avg',label:'Slope'},{l:'aspect',s:'avg',label:'Aspect'},{l:'rough',s:'avg',label:'Roughness'},{l:'shade',s:'avg',label:'Hillshade'}]
};
let curTopic='snow';
const TOPIC_COLOR={ski:['#0aa06e','rgba(10,160,110,.14)'],snow:['#1a7fd4','rgba(26,127,212,.13)'],temp:['#e8590c','rgba(232,89,12,.13)'],wind:['#0d9488','rgba(13,148,136,.14)'],rad:['#f59e0b','rgba(245,158,11,.16)'],terrain:['#64748b','rgba(100,116,139,.16)']};
function setTopic(t,subIdx){
  curTopic=t;
  document.querySelectorAll('#topics button[data-t]').forEach(x=>x.classList.toggle('active',x.dataset.t===t));
  const isMore=['temp','wind','rad','terrain'].includes(t);
  document.getElementById('moreTopics').classList.toggle('sel',isMore);
  document.querySelectorAll('#topicsMore button').forEach(x=>x.classList.toggle('active',x.dataset.t===t));
  const tc=TOPIC_COLOR[t]||TOPIC_COLOR.snow;const lb=document.getElementById('layerBar');
  lb.style.setProperty('--topic-accent',tc[0]);lb.style.setProperty('--topic-tint',tc[1]);
  document.getElementById('topics').classList.remove('expanded');
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
(function(){const mt=document.getElementById('moreTopics'),menu=document.getElementById('topicsMore'),topics=document.getElementById('topics');
  function place(){const lb=document.getElementById('layerBar').getBoundingClientRect();const r=mt.getBoundingClientRect();
    menu.style.top=(lb.bottom+8)+'px';menu.style.maxHeight=(window.innerHeight-lb.bottom-24)+'px';
    let left=Math.min(r.left,window.innerWidth-menu.offsetWidth-10);menu.style.left=Math.max(8,left)+'px';}
  mt.onclick=e=>{e.stopPropagation();const open=topics.classList.toggle('expanded');if(open)place();};
  document.addEventListener('click',e=>{if(topics.classList.contains('expanded')&&!menu.contains(e.target)&&!mt.contains(e.target))topics.classList.remove('expanded');});
  window.addEventListener('resize',()=>{if(topics.classList.contains('expanded'))place();});
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
    drawTimeline();document.getElementById('window').innerHTML=(b-a)+'h window';}
  document.addEventListener('mousemove',onDrag);document.addEventListener('touchmove',onDrag,{passive:false});
  function endDrag(){if(mode){mode=null;tc.style.cursor='default';renderAll();}}
  document.addEventListener('mouseup',endDrag);document.addEventListener('touchend',endDrag);
  tc.addEventListener('mousemove',function(e){if(mode)return;const cx=e.clientX;const zone=getZone(cx);
    tc.style.cursor=zone==='center'?'grab':zone==='left'||zone==='right'?'col-resize':'crosshair';});
})();
document.getElementById('stnToggle').onchange=e=>{showStn=e.target.checked;renderStations();};
function toggleReportLayer(on){if(on)map.addLayer(reportMarkers);else map.removeLayer(reportMarkers);}
// Marker-visibility popover on the control rail
(function(){const btn=document.getElementById('railToggles'),pop=document.getElementById('togglePop');
  btn.onclick=e=>{e.stopPropagation();const open=pop.classList.toggle('show');btn.classList.toggle('active',open);if(open)pop.style.top=btn.offsetTop+'px';};
  document.addEventListener('click',e=>{if(!pop.contains(e.target)&&e.target!==btn&&!btn.contains(e.target)){pop.classList.remove('show');btn.classList.remove('active');}});
})();
// --- Bottom panel: expand (content height) / collapse (tap or swipe the handle) ---
let panelRestore=null,panelCollapsed=false;
(function(){const bp=document.getElementById('bottomPanel'),tl=document.getElementById('tlToggle'),btm=document.getElementById('btmMain');
  const minH=16;
  function invalidate(){try{map.invalidateSize({animate:false,pan:false});}catch(e){}}
  function updH(){document.documentElement.style.setProperty('--btm-h',bp.offsetHeight+'px');}
  function apply(){if(panelCollapsed){bp.style.height=minH+'px';btm.style.display='none';}else{bp.style.height='';btm.style.display='';}
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
function dismissIntro(){const el=document.getElementById('intro');if(!el)return;
  const wait=Math.max(0,1200-(Date.now()-_introStart));
  setTimeout(()=>{el.classList.add('hide');setTimeout(()=>{el.remove();maybeOnboard();},500);},wait);}
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
function maybeOnboard(){try{if(localStorage.getItem('ssm_onboarded'))return;}catch(e){}coachIdx=0;startCoach();}
function endCoach(){try{localStorage.setItem('ssm_onboarded','1');}catch(e){}const c=document.getElementById('coach');if(c)c.style.display='none';}
function showCoachStep(){
  const c=document.getElementById('coach');
  // Skip steps whose target is missing
  while(coachIdx<COACH_STEPS.length&&!document.querySelector(COACH_STEPS[coachIdx].sel))coachIdx++;
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
setTopic('snow',0);dismissIntro();
requestAnimationFrame(()=>{document.documentElement.style.setProperty('--btm-h',document.getElementById('bottomPanel').offsetHeight+'px');});
// --- Supabase Auth & Reports ---
const SB_URL='https://gdtxwowcqtbdkcoksivb.supabase.co';
const SB_KEY='eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdkdHh3b3djcXRiZGtjb2tzaXZiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODE3NzA1ODUsImV4cCI6MjA5NzM0NjU4NX0.5t4UHGLnnWbDoPVZE0LnmN1bS_jvEU3mmk4-1JpvXmM';
let sb=null,sbUser=null,authMode='login';
try{sb=window.supabase.createClient(SB_URL,SB_KEY);}catch(e){console.warn('Supabase SDK not loaded',e);}
function haptic(ms){try{navigator.vibrate(ms||8);}catch(e){}}
// --- Category icons / colors (defined early: used by markers on first paint) ---
const CAT_SVG={
  snow:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="12" y1="2" x2="12" y2="22"/><line x1="4.9" y1="7" x2="19.1" y2="17"/><line x1="4.9" y1="17" x2="19.1" y2="7"/><line x1="12" y1="2" x2="14" y2="4.5"/><line x1="12" y1="2" x2="10" y2="4.5"/><line x1="12" y1="22" x2="14" y2="19.5"/><line x1="12" y1="22" x2="10" y2="19.5"/><line x1="4.9" y1="7" x2="5.7" y2="9.8"/><line x1="4.9" y1="7" x2="7.5" y2="6"/><line x1="19.1" y1="17" x2="18.3" y2="14.2"/><line x1="19.1" y1="17" x2="16.5" y2="18"/><line x1="19.1" y1="7" x2="16.5" y2="6"/><line x1="19.1" y1="7" x2="18.3" y2="9.8"/><line x1="4.9" y1="17" x2="7.5" y2="18"/><line x1="4.9" y1="17" x2="5.7" y2="14.2"/></svg>',
  route:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19l4-14 4 10 4-6 4 10"/><circle cx="6" cy="5" r="1.5" fill="currentColor" stroke="none"/><circle cx="18" cy="9" r="1.5" fill="currentColor" stroke="none"/></svg>',
  danger:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.2L1.8 18.5c-.8 1.4.2 3 1.7 3h17c1.5 0 2.5-1.6 1.7-3L13.7 3.2c-.8-1.4-2.6-1.4-3.4 0z"/><line x1="12" y1="9" x2="12" y2="14"/><circle cx="12" cy="17" r="1" fill="currentColor" stroke="none"/></svg>',
  tour:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20l7-10 4 5 7-11"/><circle cx="17" cy="4" r="2"/><path d="M14 20l3-4 4 4"/></svg>',
  info:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><circle cx="12" cy="8" r=".5" fill="currentColor" stroke="none"/></svg>'
};
const CAT_COLORS={snow:'#1a7fd4',route:'#2e7d32',danger:'#d03050',tour:'#7b1fa2',info:'#e65100'};
const CAT_BG={snow:'linear-gradient(135deg,#e3f2fd,#bbdefb)',route:'linear-gradient(135deg,#e8f5e9,#c8e6c9)',danger:'linear-gradient(135deg,#fce4ec,#f8bbd0)',tour:'linear-gradient(135deg,#f3e5f5,#e1bee7)',info:'linear-gradient(135deg,#fff3e0,#ffe0b2)'};
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
// --- Demo data ---
const DEMO_REPORTS=[
  {id:'d1',user:'AlpinMax',cat:'snow',icon:'❄️',sub:'Neuschnee',measurement:'30 cm',caption:'Frischer Powder am Titlis Nordwand! Traumhafte Bedingungen seit heute Morgen.',lat:46.7712,lng:8.4267,time:'vor 2h',img:null},
  {id:'d2',user:'BerginaZH',cat:'danger',icon:'⚠️',sub:'Wumm-Geräusche',measurement:'3 erheblich',caption:'Deutliche Setzungsgeräusche oberhalb 2400m. Triebschnee in Mulden.',lat:46.8342,lng:8.3891,time:'vor 4h',img:null},
  {id:'d3',user:'TourenfanBE',cat:'tour',icon:'⛷️',sub:'Powder',measurement:'⭐⭐⭐⭐⭐',caption:'Mega Abfahrt vom Wildstrubel! Unverspurter Pulver bis ins Tal.',lat:46.4200,lng:7.5050,time:'vor 5h',img:null},
  {id:'d4',user:'SchneeLeo',cat:'snow',icon:'❄️',sub:'Firn',measurement:'10 cm',caption:'Firn ab 10 Uhr, darunter tragfähige Altschneedecke.',lat:46.5586,lng:7.9641,time:'vor 8h',img:null},
  {id:'d5',user:'HüttenWart',cat:'info',icon:'ℹ️',sub:'Hütte offen',measurement:null,caption:'Tierberglihütte ist offen! Abendessen ab 18:00, Duschen verfügbar.',lat:46.7650,lng:8.4050,time:'vor 12h',img:null},
  {id:'d6',user:'LawinenPro',cat:'danger',icon:'⚠️',sub:'Lawinenabgang',measurement:'4 gross',caption:'Spontane Schneebrettlawine Grösse 3 am Nordhang Pizzo Rotondo.',lat:46.5250,lng:8.4900,time:'vor 1d',img:null},
  {id:'d7',user:'AlpinMax',cat:'route',icon:'🥾',sub:'Gespurt',measurement:null,caption:'Route auf den Stoos frisch gespurt. Super Aufstiegsspur.',lat:46.9765,lng:8.6612,time:'vor 1d',img:null},
  {id:'d8',user:'GipfelStürmer',cat:'tour',icon:'⛷️',sub:'Bruchharsch',measurement:'⭐⭐',caption:'Bruchharsch ab 2000m, Abfahrt nur bedingt empfehlenswert.',lat:46.6900,lng:9.8200,time:'vor 2d',img:null}
];
let reportMarkers=L.layerGroup().addTo(map);
let allReports=[...DEMO_REPORTS];
function loadReportMarkers(){
  reportMarkers.clearLayers();
  const tier=(typeof detailTier==='function')?detailTier():2;
  allReports.forEach(r=>{
    const mColor=CAT_COLORS[r.cat]||'#1a7fd4';
    let icon;
    if(tier===0){
      icon=L.divIcon({className:'',html:`<div class="rpt-dot" style="background:${mColor}"></div>`,iconSize:[14,14],iconAnchor:[7,7]});
    }else{
      icon=L.divIcon({className:'',html:`<div class="rpt-marker" style="color:${mColor}">${CAT_SVG[r.cat]||r.icon}</div>`,iconSize:[36,36],iconAnchor:[18,18]});
    }
    const m=L.marker([r.lat,r.lng],{icon,zIndexOffset:700}).addTo(reportMarkers);
    m.bindPopup(`<div style="min-width:180px"><b>${r.user}</b> <span style="color:#7a8a9a;font-size:12px">${r.time}</span><br><span style="font-size:13px;display:inline-flex;align-items:center;gap:5px;color:${mColor}">${catSvg?catSvg(r.cat,14):''} ${r.sub||r.cat}${r.measurement?' · '+r.measurement:''}</span>${r.caption?'<br><span style="font-size:13px;color:#3a4a5a">'+r.caption+'</span>':''}</div>`,{maxWidth:260});
  });
}
// --- Auth UI ---
function authUpdateUI(user){
  sbUser=user;
  const btn=document.getElementById('btnLogin');
  const fab=document.getElementById('reportFab'),feedB=document.getElementById('feedBtn'),banner=document.getElementById('emailBanner');
  if(user){
    const name=user.user_metadata?.username||user.email?.split('@')[0]||'User';
    btn.outerHTML=`<div class="user-pill" id="userPill" onclick="authMenu()">
      <div class="user-avatar"><span>${name[0].toUpperCase()}</span></div>
      <span class="user-name">${name}</span></div>`;
    fab.style.display='flex';
    banner.style.display=user.email_confirmed_at?'none':'flex';
  } else {
    const pill=document.getElementById('userPill');
    if(pill)pill.outerHTML='<button class="login-btn" id="btnLogin" onclick="authShow()">Sign in</button>';
    fab.style.display='none';banner.style.display='none';
  }
  feedB.style.display='flex';
  loadSocial().then(()=>{loadReportMarkers();loadDbReports();});
}
// --- Social state ---
let myFollowing=new Set(),myGroups=[],allGroups=[];
async function loadSocial(){
  if(!sb)return;
  try{
    if(sbUser){
      const{data:f}=await sb.from('follows').select('following_id').eq('follower_id',sbUser.id);
      myFollowing=new Set((f||[]).map(x=>x.following_id));
      const{data:gm}=await sb.from('group_members').select('group_id,groups(name)').eq('user_id',sbUser.id);
      myGroups=(gm||[]).map(x=>({id:x.group_id,name:x.groups?.name||'Gruppe'}));
    }else{myFollowing=new Set();myGroups=[];}
    const{data:g}=await sb.from('groups').select('*').order('created_at',{ascending:false}).limit(200);
    allGroups=g||[];
  }catch(e){console.warn('loadSocial',e);}
}
async function loadDbReports(){
  if(!sb)return;
  try{
    const{data}=await sb.from('reports').select('*').order('created_at',{ascending:false}).limit(60);
    if(!data||!data.length){return;}
    const ids=data.map(r=>r.id),uids=[...new Set(data.map(r=>r.user_id).filter(Boolean))];
    // usernames
    let nameMap={};try{const{data:pr}=await sb.from('profiles').select('id,username').in('id',uids);(pr||[]).forEach(p=>nameMap[p.id]=p.username);}catch(e){}
    // likes (reuse report_reactions type=like)
    let likeCount={},likedByMe={};try{const{data:rx}=await sb.from('report_reactions').select('report_id,user_id').eq('type','like').in('report_id',ids);
      (rx||[]).forEach(x=>{likeCount[x.report_id]=(likeCount[x.report_id]||0)+1;if(sbUser&&x.user_id===sbUser.id)likedByMe[x.report_id]=true;});}catch(e){}
    const dbR=data.map(r=>{
      let lat=0,lng=0;const m=r.location?.match?.(/POINT\(([-\d.]+)\s+([-\d.]+)\)/);
      if(m){lng=parseFloat(m[1]);lat=parseFloat(m[2]);}
      const catId=r.primary_categories?.[0]||'info';const catObj=RP_CATS.find(c=>c.id===catId);
      return{id:r.id,user:nameMap[r.user_id]||(r.user_id?.substring(0,8))||'User',userId:r.user_id,cat:catId,icon:catObj?.icon||'📍',sub:r.subtype,measurement:r.condition_data?.measurement||null,peak:r.condition_data?.peak||null,dest:r.condition_data?.dest||null,caption:r.caption,lat,lng,time:timeAgo(r.created_at),img:r.image_url,groupId:r.group_id||null,likes:likeCount[r.id]||0,liked:!!likedByMe[r.id],dbRow:true};
    });
    allReports=[...dbR,...DEMO_REPORTS];loadReportMarkers();
    if(document.getElementById('feedPage').classList.contains('open'))feedRender();
  }catch(e){console.warn('loadDbReports',e);}
}
// --- Likes ---
async function toggleLike(id,ev){if(ev){ev.stopPropagation();}
  if(!sb||!sbUser){authShow();return;}
  const r=allReports.find(x=>x.id===id);if(!r||!r.dbRow)return;
  const willLike=!r.liked;r.liked=willLike;r.likes=(r.likes||0)+(willLike?1:-1);feedRender();
  try{
    if(willLike)await sb.from('report_reactions').insert({report_id:id,user_id:sbUser.id,type:'like'});
    else await sb.from('report_reactions').delete().match({report_id:id,user_id:sbUser.id,type:'like'});
  }catch(e){r.liked=!willLike;r.likes+=(willLike?-1:1);feedRender();}
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
// --- Groups ---
async function joinGroup(id){if(!sb||!sbUser){authShow();return;}
  try{await sb.from('group_members').insert({group_id:id,user_id:sbUser.id});await loadSocial();groupsRender();feedRender();}catch(e){alert('Fehler: '+(e.message||e));}}
async function leaveGroup(id){if(!sb||!sbUser)return;
  try{await sb.from('group_members').delete().match({group_id:id,user_id:sbUser.id});await loadSocial();groupsRender();feedRender();}catch(e){}}
async function createGroup(){if(!sb||!sbUser){authShow();return;}
  const name=(document.getElementById('groupNewName').value||'').trim();if(!name)return;
  try{const{data,error}=await sb.from('groups').insert({name,created_by:sbUser.id}).select().single();if(error)throw error;
    await sb.from('group_members').insert({group_id:data.id,user_id:sbUser.id});
    document.getElementById('groupNewName').value='';await loadSocial();groupsRender();}catch(e){alert('Fehler: '+(e.message||e));}}
function timeAgo(ts){const d=Date.now()-new Date(ts).getTime(),h=d/36e5;if(h<1)return'gerade eben';if(h<24)return'vor '+Math.floor(h)+'h';return'vor '+Math.floor(h/24)+'d';}
if(sb){sb.auth.onAuthStateChange((ev,session)=>{authUpdateUI(session?.user||null);});
  sb.auth.getSession().then(({data})=>{authUpdateUI(data.session?.user||null);});}
else{document.getElementById('feedBtn').style.display='flex';loadReportMarkers();}
function authShow(){authMode='login';authRender();document.getElementById('authOverlay').style.display='flex';}
function authHide(){document.getElementById('authOverlay').style.display='none';document.getElementById('authErr').textContent='';}
function authToggle(){authMode=authMode==='login'?'register':'login';authRender();}
function authRender(){
  const isReg=authMode==='register';
  document.getElementById('authTitle').textContent=isReg?'Account erstellen':'Anmelden';
  document.getElementById('authSub').textContent=isReg?'Erstelle ein Konto für Field Reports':'Anmelden für Field Reports';
  document.getElementById('authUser').style.display=isReg?'':'none';
  document.getElementById('authSubmitBtn').textContent=isReg?'Registrieren':'Anmelden';
  document.getElementById('authSwitch').innerHTML=isReg?'Schon registriert? <button onclick="authToggle()">Anmelden</button>':'Kein Account? <button onclick="authToggle()">Registrieren</button>';
}
async function authSubmit(e){
  e.preventDefault();if(!sb)return;
  const email=document.getElementById('authEmail').value.trim();
  const pass=document.getElementById('authPass').value;
  const errEl=document.getElementById('authErr');errEl.textContent='';
  try{
    if(authMode==='register'){
      const username=document.getElementById('authUser').value.trim();
      if(!username){errEl.textContent='Username required';return;}
      const{error}=await sb.auth.signUp({email,password:pass,options:{data:{username}}});
      if(error)throw error;
      errEl.style.color='#5EC8FF';errEl.textContent='Check your email to confirm!';return;
    }
    const{error}=await sb.auth.signInWithPassword({email,password:pass});
    if(error)throw error;authHide();
  }catch(err){errEl.style.color='#FF5470';errEl.textContent=err.message||'Error';}
}
async function authResend(){if(!sb||!sbUser)return;await sb.auth.resend({type:'signup',email:sbUser.email});document.getElementById('emailBanner').querySelector('button').textContent='Sent!';}
function authMenu(){if(confirm('Sign out?')){if(sb)sb.auth.signOut();authUpdateUI(null);}}
// --- Report categories ---
const RP_CATS=[
  {id:'snow',label:'Schnee',icon:'❄️',subs:['Neuschnee','Nassschnee','Triebschnee','Sulz','Firn','Bruchharsch','Windgepresst']},
  {id:'route',label:'Route',icon:'🥾',subs:['Gespurt','Verspurt','Keine Spur','Lawinenzug','Wechte','Gletscherspalte']},
  {id:'danger',label:'Gefahr',icon:'⚠️',subs:['Lawinenabgang','Risse/Setzungen','Wumm-Geräusche','Steinschlag','Blankeis','Triebschnee']},
  {id:'tour',label:'Tour',icon:'⛷️',subs:['Powder','Sulz-Genuss','Abgeblasen','Bruchharsch','Nicht empfohlen']},
  {id:'info',label:'Info',icon:'ℹ️',subs:['Hütte offen','Hütte geschlossen','Weg gesperrt','Brücke fehlt','Markierung fehlt']}
];
const RP_BUCKETS={snow:['0','5','10','20','30','50','100+'],danger:['1','2','3','4','5'],tour:['⭐','⭐⭐','⭐⭐⭐','⭐⭐⭐⭐','⭐⭐⭐⭐⭐']};
const RP_BUCKET_UNITS={snow:'cm',danger:'Stufe',tour:''};
const RP_BUCKET_LABELS={snow:'Schneehöhe',danger:'Gefahrenstufe',tour:'Bewertung'};
// --- Radial quick-pick (long-press FAB) ---
let radialActive=false,radialCat=null;
const RAD_POS=[{a:-90},{a:-162},{a:-18},{a:162},{a:18}];
(function(){
  const fab=document.getElementById('reportFab'),wrap=document.getElementById('radialWrap'),ring=document.getElementById('radialRing'),ctr=document.getElementById('radialCenter');
  let timer=null,ox=0,oy=0;
  function showRadial(cx,cy){
    if(!sbUser){authShow();return;}
    radialActive=true;radialCat=null;wrap.style.display='block';
    const sz=280,hsz=sz/2;
    const rx=Math.min(window.innerWidth-hsz-10,Math.max(hsz+10,cx))-hsz;
    const ry=Math.min(window.innerHeight-hsz-10,Math.max(hsz+10,cy))-hsz;
    ring.style.left=rx+'px';ring.style.top=ry+'px';
    ctr.style.left=(rx+hsz-24)+'px';ctr.style.top=(ry+hsz-24)+'px';
    ox=rx+hsz;oy=ry+hsz;
    ring.innerHTML=RP_CATS.map((c,i)=>{
      const ang=RAD_POS[i].a*Math.PI/180;const r=100;
      const x=hsz+Math.cos(ang)*r-36;const y=hsz+Math.sin(ang)*r-36;
      return`<div class="radial-seg" data-i="${i}" style="left:${x}px;top:${y}px"><span class="re">${c.icon}</span>${c.label}</div>`;
    }).join('');
    haptic(12);
  }
  function moveRadial(cx,cy){
    if(!radialActive)return;
    const dx=cx-ox,dy=cy-oy,dist=Math.sqrt(dx*dx+dy*dy);
    let closest=-1;
    if(dist>40){
      const ang=Math.atan2(dy,dx)*180/Math.PI;
      let minD=999;
      RAD_POS.forEach((p,i)=>{let d=Math.abs(ang-p.a);if(d>180)d=360-d;if(d<minD){minD=d;closest=i;}});
    }
    ring.querySelectorAll('.radial-seg').forEach((s,i)=>{
      const isH=i===closest;
      s.classList.toggle('hover',isH);
      if(isH&&radialCat!==i){radialCat=i;haptic(6);}
    });
    if(closest===-1)radialCat=null;
  }
  function endRadial(){
    if(!radialActive)return;radialActive=false;wrap.style.display='none';
    if(radialCat!==null){
      haptic(20);
      rpState.cat=RP_CATS[radialCat].id;
      reportOpenSheet();
    }
  }
  fab.addEventListener('pointerdown',e=>{
    e.preventDefault();
    timer=setTimeout(()=>{timer=null;showRadial(e.clientX,e.clientY);},300);
  });
  fab.addEventListener('pointermove',e=>{if(radialActive)moveRadial(e.clientX,e.clientY);});
  fab.addEventListener('pointerup',e=>{
    if(timer){clearTimeout(timer);timer=null;if(!sbUser){authShow();}else{rpState.cat=null;reportOpenSheet();}}
    else endRadial();
  });
  fab.addEventListener('pointercancel',()=>{if(timer)clearTimeout(timer);endRadial();});
  wrap.addEventListener('pointermove',e=>moveRadial(e.clientX,e.clientY));
  wrap.addEventListener('pointerup',endRadial);
})();
// --- Single-sheet report flow ---
let rpState={cat:null,sub:null,bucket:null,photo:null,photoFile:null,loc:null,caption:'',peak:null,dest:null,peakCand:null,group:null,photoLoc:null,deviceLoc:null,locSource:null};
function rpReset(){rpState={cat:null,sub:null,bucket:null,photo:null,photoFile:null,loc:null,caption:'',peak:null,dest:null,peakCand:null,group:null,photoLoc:null,deviceLoc:null,locSource:null};}
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
function rpScore(){let s=0;if(rpState.cat)s+=25;if(rpState.sub)s+=25;if(rpState.bucket)s+=15;if(rpState.photo)s+=25;if(rpState.loc)s+=10;return Math.min(100,s);}
// --- Wizard step engine ---
const RP_PHOTO_PLACEHOLDER='<div class="rp-photo-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg><span>Foto aufnehmen</span></div>';
const RP_STEP_META={photo:{t:'Foto hinzufügen',s:'Zeig, was du siehst (optional)'},cat:{t:'Was siehst du?',s:'Wähle eine Kategorie'},sub:{t:'Details',s:'Genauer beschreiben'},bucket:{t:'Messung',s:'Wie viel / wie stark?'},final:{t:'Fast fertig',s:'Standort & Notiz, dann posten'}};
let rpStepList=[],rpCurStep='photo';
function rpBuildSteps(){const s=['photo','cat','sub'];if(RP_BUCKETS[rpState.cat])s.push('bucket');s.push('final');return s;}
function rpShow(stepId){
  rpStepList=rpBuildSteps();if(!rpStepList.includes(stepId))stepId=rpStepList[0];rpCurStep=stepId;
  document.querySelectorAll('#reportOverlay .rp-pane').forEach(p=>p.style.display=p.dataset.step===stepId?'':'none');
  const meta=RP_STEP_META[stepId];
  document.getElementById('rpStepTitle').textContent=meta.t;
  let sub=meta.s;if(stepId==='bucket')sub=RP_BUCKET_LABELS[rpState.cat]||meta.s;
  document.getElementById('rpStepSub').textContent=sub;
  if(stepId==='sub')rpRenderSubs();
  if(stepId==='bucket')rpRenderBuckets();
  if(stepId==='final'){updateLocCard();rpRenderSummary();rpDetectPeak();rpRenderGroupSel();}
  rpRenderProgress();rpRenderNav();
}
function rpRenderProgress(){const idx=rpStepList.indexOf(rpCurStep);
  document.getElementById('rpProgress').innerHTML=rpStepList.map((s,i)=>'<i class="'+(i<idx?'done':i===idx?'cur':'')+'"></i>').join('');}
function rpRenderNav(){const idx=rpStepList.indexOf(rpCurStep);
  document.getElementById('rpBack').disabled=idx<=0;
  const skip=document.getElementById('rpSkip'),next=document.getElementById('rpNext');
  skip.style.display=(['photo','sub','bucket'].includes(rpCurStep))?'':'none';
  if(rpCurStep==='final'){next.textContent='Report posten';next.classList.add('post');next.disabled=!rpState.cat;}
  else{next.textContent='Weiter';next.classList.remove('post');next.disabled=(rpCurStep==='cat'&&!rpState.cat);}}
function rpStepNext(){const idx=rpStepList.indexOf(rpCurStep);
  if(rpCurStep==='final'){reportSubmit();return;}
  if(rpCurStep==='cat'&&!rpState.cat)return;
  rpShow(rpStepList[Math.min(rpStepList.length-1,idx+1)]);haptic(6);}
function rpStepPrev(){const idx=rpStepList.indexOf(rpCurStep);if(idx>0)rpShow(rpStepList[idx-1]);}
function reportOpenSheet(){
  document.getElementById('reportOverlay').style.display='flex';
  rpState.sub=null;rpState.bucket=null;rpState.caption='';rpState.peak=null;rpState.dest=null;rpState.peakCand=null;
  if(!rpState.cat){rpState.photo=null;rpState.photoFile=null;}
  rpState.photoLoc=null;rpState.deviceLoc=null;rpState.locSource=null;rpState.loc=null;
  document.getElementById('rpCaption').value='';
  rpRenderCats();rpResetPhoto();
  rpShow(rpState.cat?'sub':'photo');
  updateLocCard();
  if(navigator.geolocation)navigator.geolocation.getCurrentPosition(p=>{
    rpState.deviceLoc=[p.coords.latitude,p.coords.longitude,p.coords.accuracy];
    if(rpState.locSource!=='photo'){rpState.locSource='device';rpState.loc=[p.coords.latitude,p.coords.longitude];}
    updateLocCard();if(rpCurStep==='final')rpDetectPeak();
  },()=>{updateLocCard();},{enableHighAccuracy:true,timeout:12000});
}
function updateLocCard(){
  const card=document.getElementById('rpLocCard');if(!card)return;
  const src=rpState.locSource;let html='';
  if(src==='photo'){const l=rpState.photoLoc;
    html='<span>📷 Foto-Standort · '+l[0].toFixed(4)+', '+l[1].toFixed(4)+'</span>';
    if(rpState.deviceLoc)html+='<button class="rp-loc-switch" onclick="rpSwitchLoc()">Gerät nutzen</button>';
  }else if(src==='device'){const l=rpState.deviceLoc;const acc=l[2]?' ±'+Math.round(l[2])+' m':'';
    html='<span>📍 Geräte-Standort'+acc+' · '+l[0].toFixed(4)+', '+l[1].toFixed(4)+'</span>';
    if(rpState.photoLoc)html+='<button class="rp-loc-switch" onclick="rpSwitchLoc()">Foto nutzen</button>';
  }else{html='<span id="rpCtxLoc">📍 Standort wird ermittelt…</span>';}
  card.innerHTML=html;
}
function rpSwitchLoc(){
  if(rpState.locSource==='photo'&&rpState.deviceLoc){rpState.locSource='device';rpState.loc=[rpState.deviceLoc[0],rpState.deviceLoc[1]];}
  else if(rpState.locSource==='device'&&rpState.photoLoc){rpState.locSource='photo';rpState.loc=[rpState.photoLoc[0],rpState.photoLoc[1]];}
  rpState.peak=null;rpState.peakCand=null;haptic(8);updateLocCard();rpDetectPeak();
}
function reportClose(){document.getElementById('reportOverlay').style.display='none';rpReset();}
function rpRenderCats(){
  document.getElementById('rpCats').innerHTML=RP_CATS.map(c=>
    `<button class="cat-chip${rpState.cat===c.id?' active':''}" data-id="${c.id}" style="${rpState.cat===c.id?'color:'+CAT_COLORS[c.id]:''}" onclick="rpPickCat('${c.id}')"><span class="cat-ico-w">${CAT_SVG[c.id]}</span>${c.label}</button>`
  ).join('');
}
function rpPickCat(id){rpState.cat=id;rpState.sub=null;rpState.bucket=null;haptic(10);
  document.querySelectorAll('#rpCats .cat-chip').forEach(el=>{const on=el.dataset.id===id;el.classList.toggle('active',on);el.style.color=on?CAT_COLORS[id]:'';});
  rpStepList=rpBuildSteps();setTimeout(()=>rpShow('sub'),170);}
function rpRenderSubs(){const cat=RP_CATS.find(c=>c.id===rpState.cat);if(!cat)return;
  document.getElementById('rpSubs').innerHTML=cat.subs.map(s=>
    `<button class="sub-chip${rpState.sub===s?' active':''}" onclick="rpPickSub(this,'${s.replace(/'/g,"\\\\'")}')">${s}</button>`).join('');}
function rpPickSub(el,val){rpState.sub=val;haptic(8);
  document.querySelectorAll('#rpSubs .sub-chip').forEach(e=>e.classList.remove('active'));el.classList.add('active');
  setTimeout(()=>rpStepNext(),170);}
function rpRenderBuckets(){const bk=RP_BUCKETS[rpState.cat];if(!bk)return;
  document.getElementById('rpBuckets').innerHTML=bk.map(b=>
    `<button class="bucket${rpState.bucket===b?' active':''}" onclick="rpPickBucket(this,'${b}')">${b}</button>`).join('');
  document.getElementById('rpBucketVal').textContent=rpState.bucket?(rpState.bucket+' '+(RP_BUCKET_UNITS[rpState.cat]||'')):'';}
function rpPickBucket(el,val){rpState.bucket=val;haptic(8);
  document.querySelectorAll('#rpBuckets .bucket').forEach(e=>e.classList.remove('active'));el.classList.add('active');
  document.getElementById('rpBucketVal').textContent=val+' '+(RP_BUCKET_UNITS[rpState.cat]||'');
  setTimeout(()=>rpStepNext(),180);}
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
function rpRenderGroupSel(){const wrap=document.getElementById('rpGroupWrap'),sel=document.getElementById('rpGroupSel');if(!wrap)return;
  if(sbUser&&myGroups.length){wrap.style.display='';sel.innerHTML='<option value="">🌍 Öffentlich</option>'+myGroups.map(g=>`<option value="${g.id}"${rpState.group===g.id?' selected':''}>👥 ${g.name}</option>`).join('');}
  else{wrap.style.display='none';}}
function rpRenderSummary(){const el=document.getElementById('rpSummary');if(!el)return;
  const cat=RP_CATS.find(c=>c.id===rpState.cat);const t=[];
  if(cat)t.push('<span class="rp-tag">'+catSvg(cat.id,13)+cat.label+'</span>');
  if(rpState.sub)t.push('<span class="rp-tag">'+rpState.sub+'</span>');
  if(rpState.bucket)t.push('<span class="rp-tag">'+rpState.bucket+' '+(RP_BUCKET_UNITS[rpState.cat]||'')+'</span>');
  if(rpState.photo)t.push('<span class="rp-tag">📷 Foto</span>');
  if(rpState.peak)t.push('<span class="rp-tag">⛰ '+rpState.peak+'</span>');
  else if(rpState.dest)t.push('<span class="rp-tag">📍 '+rpState.dest+'</span>');
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
  rpRecognition.onend=()=>{rpRecording=false;btn.classList.remove('recording');btn.textContent='🎤 Halten und sprechen';};
  rpRecognition.start();rpRecording=true;btn.classList.add('recording');btn.textContent='🎤 Recording...';haptic(12);
}
async function reportSubmit(){
  if(!sb||!sbUser||!rpState.cat)return;
  const next=document.getElementById('rpNext');next.disabled=true;next.textContent='Poste…';
  try{
    let imageUrl=null;
    if(rpState.photoFile){
      const ext=(rpState.photoFile.name.split('.').pop()||'jpg').toLowerCase();
      const path=`${sbUser.id}/${Date.now()}.${ext}`;
      const{error:upErr}=await sb.storage.from('report-images').upload(path,rpState.photoFile,{contentType:rpState.photoFile.type||'image/jpeg',upsert:false});
      if(upErr){console.error('Foto-Upload fehlgeschlagen',upErr);
        if(!confirm('Foto konnte nicht hochgeladen werden ('+(upErr.message||upErr)+').\\n\\nMöglich: der Storage-Bucket „report-images" fehlt oder ist nicht öffentlich.\\n\\nTrotzdem ohne Foto posten?')){next.disabled=false;next.textContent='Report posten';return;}}
      else{const{data:urlData}=sb.storage.from('report-images').getPublicUrl(path);imageUrl=urlData?.publicUrl||null;}
    }
    const loc=rpState.loc||[map.getCenter().lat,map.getCenter().lng];
    const cd=rpState.bucket?{measurement:rpState.bucket}:{};
    if(rpState.peak)cd.peak=rpState.peak;if(rpState.dest)cd.dest=rpState.dest;
    const row={
      user_id:sbUser.id,location:`POINT(${loc[1]} ${loc[0]})`,
      primary_categories:[rpState.cat],subtype:rpState.sub,
      condition_data:cd,
      image_url:imageUrl,caption:rpState.caption.trim()||null,
      completion_score:rpScore()
    };
    if(rpState.group)row.group_id=rpState.group;
    const{error}=await sb.from('reports').insert(row);
    if(error)throw error;
    reportClose();loadDbReports();
    showUndo();
  }catch(err){alert('Error: '+(err.message||err));next.disabled=false;next.textContent='Report posten';}
}
function showUndo(){
  const bar=document.createElement('div');bar.className='undo-bar';
  bar.innerHTML='Gepostet. Andere sehen\'s jetzt auf der Karte. <button onclick="this.parentElement.remove()">OK</button>';
  document.body.appendChild(bar);setTimeout(()=>bar.remove(),5000);
}
// --- Feed (Instagram-style full page) ---
let feedFilter='all',feedAnchor=null,feedScope='all',feedGroup=null;
const FEED_SCOPES=[
  {id:'all',label:'Entdecken',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="16.2 7.8 14 14 7.8 16.2 10 10"/></svg>'},
  {id:'following',label:'Folge ich',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>'},
  {id:'groups',label:'Gruppen',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.2"/><circle cx="17" cy="9" r="2.4"/><path d="M2.5 19a6.5 6.5 0 0 1 13 0M15.5 19a5 5 0 0 1 6 0"/></svg>'},
  {id:'near',label:'Nähe',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg>'}
];
function groupName(id){const g=allGroups.find(x=>x.id===id)||myGroups.find(x=>x.id===id);return g?g.name:'Gruppe';}
function feedOpen(){
  const fp=document.getElementById('feedPage');fp.classList.add('open');
  document.getElementById('feedScope').innerHTML=FEED_SCOPES.map(s=>
    `<button data-s="${s.id}" class="${feedScope===s.id?'active':''}" onclick="feedSetScope('${s.id}')">${s.icon}${s.label}</button>`).join('');
  document.getElementById('feedFilter').innerHTML=['all','snow','route','danger','tour','info'].map(f=>{
    const cat=RP_CATS.find(c=>c.id===f);const lbl=f==='all'?'Alle':(catSvg(f,14)+' '+cat.label);
    return`<button class="${feedFilter===f?'active':''}" onclick="feedSetFilter('${f}')">${lbl}</button>`;}).join('');
  const pk=document.getElementById('feedPeak');
  if(pk.options.length<=1)pk.innerHTML='<option value="">⛰ Gipfel wählen…</option>'+PEAKS.slice().sort((a,b)=>a.n.localeCompare(b.n)).map(p=>`<option value="${PEAKS.indexOf(p)}">${p.n} (${p.e} m)</option>`).join('');
  const ds=document.getElementById('feedDest');
  if(ds.options.length<=1)ds.innerHTML='<option value="">🎿 Skigebiet wählen…</option>'+DESTS.slice().sort((a,b)=>a.n.localeCompare(b.n)).map(p=>`<option value="${DESTS.indexOf(p)}">${p.n}</option>`).join('');
  document.getElementById('feedLoc').style.display=feedScope==='near'?'flex':'none';
  document.getElementById('feedGroups').style.display=feedScope==='groups'?'flex':'none';
  groupsRender();feedRender();
}
function feedClose(){document.getElementById('feedPage').classList.remove('open');}
function feedSetScope(s){feedScope=s;
  document.querySelectorAll('#feedScope button').forEach(b=>b.classList.toggle('active',b.dataset.s===s));
  document.getElementById('feedLoc').style.display=s==='near'?'flex':'none';
  document.getElementById('feedGroups').style.display=s==='groups'?'flex':'none';
  if(s==='groups')groupsRender();
  feedRender();haptic(5);}
function feedSetFilter(f){feedFilter=f;document.querySelectorAll('.feed-filter button').forEach((b,i)=>{b.classList.toggle('active',['all','snow','route','danger','tour','info'][i]===f);});feedRender();}
function feedSetGroup(id){feedGroup=id;groupsRender();feedRender();}
function feedSetAnchor(a){feedAnchor=a;
  document.getElementById('feedNear').classList.toggle('active',!!a&&a.src==='me');
  document.getElementById('feedPeak').classList.toggle('active',!!a&&a.src==='peak');
  document.getElementById('feedDest').classList.toggle('active',!!a&&a.src==='dest');
  const bar=document.getElementById('feedAnchorBar'),clr=document.getElementById('feedAnchorClear');
  if(a){bar.style.display='';bar.innerHTML='Sortiert nach Nähe zu <b>'+a.name+'</b>';clr.style.display='';}
  else{bar.style.display='none';clr.style.display='none';document.getElementById('feedPeak').value='';document.getElementById('feedDest').value='';}
  feedRender();}
function feedClearAnchor(){feedSetAnchor(null);}
(function(){
  const near=document.getElementById('feedNear');if(!near)return;
  const nearHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg> In der Nähe';
  near.onclick=()=>{if(!navigator.geolocation){alert('Kein GPS verfügbar');return;}
    near.textContent='… GPS';near.disabled=true;
    navigator.geolocation.getCurrentPosition(p=>{near.disabled=false;near.innerHTML=nearHTML;
      feedSetAnchor({name:'meiner Position',lat:p.coords.latitude,lng:p.coords.longitude,src:'me'});},
    ()=>{near.disabled=false;near.innerHTML=nearHTML;alert('Standort nicht verfügbar');},{enableHighAccuracy:true,timeout:10000});};
  document.getElementById('feedPeak').onchange=function(){if(this.value===''){feedClearAnchor();return;}const p=PEAKS[+this.value];document.getElementById('feedDest').value='';feedSetAnchor({name:p.n,lat:p.lat,lng:p.lng,src:'peak'});};
  document.getElementById('feedDest').onchange=function(){if(this.value===''){feedClearAnchor();return;}const p=DESTS[+this.value];document.getElementById('feedPeak').value='';feedSetAnchor({name:p.n,lat:p.lat,lng:p.lng,src:'dest'});};
  document.getElementById('feedAnchorClear').onclick=feedClearAnchor;
})();
// --- Groups UI ---
function groupsRender(){
  const row=document.getElementById('feedGroups');
  if(row){let h='';
    if(myGroups.length){h+=`<button class="${feedGroup===null?'active':''}" onclick="feedSetGroup(null)">Alle meine</button>`;
      h+=myGroups.map(g=>`<button class="${feedGroup===g.id?'active':''}" onclick="feedSetGroup('${g.id}')">👥 ${g.name}</button>`).join('');}
    h+=`<button class="manage" onclick="groupsOpen()">⚙ Verwalten</button>`;row.innerHTML=h;}
  const list=document.getElementById('groupsList');
  if(list){
    if(!sb||!sbUser){list.innerHTML='<div class="groups-empty">Melde dich an, um Gruppen beizutreten oder zu erstellen.</div>';return;}
    if(!allGroups.length){list.innerHTML='<div class="groups-empty">Noch keine Gruppen. Erstelle oben die erste!</div>';return;}
    const mine=new Set(myGroups.map(g=>g.id));
    list.innerHTML=allGroups.map(g=>{const joined=mine.has(g.id);
      return`<div class="groups-row"><div style="flex:1"><div class="gr-name">${g.name}</div>${g.description?'<div class="gr-meta">'+g.description+'</div>':''}</div><button class="${joined?'joined':''}" onclick="${joined?'leaveGroup':'joinGroup'}('${g.id}')">${joined?'Mitglied':'Beitreten'}</button></div>`;}).join('');}
}
function groupsOpen(){document.getElementById('groupsModal').style.display='flex';groupsRender();}
function groupsClose(){document.getElementById('groupsModal').style.display='none';}
function feedRender(){
  const list=document.getElementById('feedList');
  let base=allReports.slice();
  if(feedScope==='following'){
    if(!sbUser){list.innerHTML='<div class="feed-empty">Melde dich an, um Leuten zu folgen und ihre Reports hier zu sehen.</div>';return;}
    base=base.filter(r=>r.dbRow&&r.userId&&myFollowing.has(r.userId));
    if(!base.length){list.innerHTML='<div class="feed-empty">Du folgst noch niemandem. Tippe bei einem Report auf „Folgen".</div>';return;}
  }else if(feedScope==='groups'){
    const gm=new Set(myGroups.map(g=>g.id));
    base=base.filter(r=>r.dbRow&&r.groupId&&(feedGroup?r.groupId===feedGroup:gm.has(r.groupId)));
    if(!base.length){list.innerHTML='<div class="feed-empty">Keine Gruppen-Reports. Tritt einer Gruppe bei (⚙ Verwalten) oder poste in einer Gruppe.</div>';return;}
  }
  let filtered=feedFilter==='all'?base:base.filter(r=>r.cat===feedFilter);
  if(feedAnchor){filtered=filtered.map(r=>({r,km:haversineKm(feedAnchor.lat,feedAnchor.lng,r.lat,r.lng)})).sort((a,b)=>a.km-b.km).map(o=>{o.r._km=o.km;return o.r;});}
  if(!filtered.length){list.innerHTML='<div class="feed-empty">Noch keine Reports in dieser Kategorie.</div>';return;}
  list.innerHTML=filtered.map(r=>{
    const col=CAT_COLORS[r.cat]||'#666';
    const bg=CAT_BG[r.cat]||'linear-gradient(135deg,#f0f0f0,#e0e0e0)';
    const avatarBg=r.cat==='danger'?'linear-gradient(135deg,#d03050,#ff5470)':r.cat==='snow'?'linear-gradient(135deg,#1a7fd4,#42a5f5)':r.cat==='route'?'linear-gradient(135deg,#2e7d32,#66bb6a)':r.cat==='tour'?'linear-gradient(135deg,#7b1fa2,#ab47bc)':'linear-gradient(135deg,#e65100,#ff9800)';
    const distTag=(feedAnchor&&r._km!=null)?`<span class="feed-card-dist">${r._km<1?Math.round(r._km*1000)+' m':r._km.toFixed(r._km<10?1:0)+' km'}</span>`:'';
    const canFollow=r.dbRow&&r.userId&&(!sbUser||r.userId!==sbUser.id);
    const followBtn=canFollow?`<button class="feed-follow ${myFollowing.has(r.userId)?'following':''}" onclick="toggleFollow('${r.userId}',event)">${myFollowing.has(r.userId)?'Folge ich':'Folgen'}</button>`:'';
    const likeBtn=r.dbRow?`<button class="${r.liked?'liked':''}" onclick="toggleLike('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z"/></svg> ${r.likes||0}</button>`:'';
    const grpBadge=r.groupId?`<span class="feed-card-group">👥 ${groupName(r.groupId)}</span>`:'';
    return`<div class="feed-card" onclick="feedFlyTo(${r.lat},${r.lng})">
      <div class="feed-card-head">
        <div class="feed-card-avatar" style="background:${avatarBg}">${r.user[0].toUpperCase()}</div>
        <div class="feed-card-info">
          <span class="feed-card-user">${r.user}</span>
          <span class="feed-card-loc"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg> ${r.peak?r.peak:(r.lat.toFixed(2)+'°N, '+r.lng.toFixed(2)+'°E')}</span>
        </div>
        ${followBtn||distTag||`<span class="feed-card-time">${r.time}</span>`}
      </div>
      <div class="feed-card-visual">
        ${r.img?`<img src="${r.img}" alt=""/>`:
        `<div class="card-placeholder" style="background:${bg}"><span style="color:${col}">${CAT_SVG[r.cat]||''}</span><span style="color:${col}">${r.sub||r.cat}</span></div>`}
      </div>
      <div class="feed-card-body">
        <div class="feed-card-badges">
          <span class="feed-badge cat-${r.cat}">${catSvg(r.cat,14)} ${r.sub||r.cat}</span>
          ${r.measurement?`<span class="feed-badge cat-${r.cat}">${r.measurement}</span>`:''}
          ${grpBadge}
        </div>
        ${r.caption?`<div class="feed-card-caption"><b>${r.user}</b> ${r.caption}</div>`:''}
      </div>
      <div class="feed-card-actions">
        ${likeBtn}
        <button onclick="event.stopPropagation();feedFlyTo(${r.lat},${r.lng})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg> Karte</button>
        ${distTag&&!feedAnchor?'':''}
      </div>
    </div>`;
  }).join('');
}
function feedFlyTo(lat,lng){feedClose();setTimeout(()=>map.flyTo([lat,lng],14,{duration:1.2}),350);}
</script></body></html>
"""
