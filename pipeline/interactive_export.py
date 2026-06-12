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
    """Exposition [Grad, im Uhrzeigersinn von Nord] + Hangneigung [Grad].

    Verifizierte Konvention: N=0, O=90, S=180, W=270 (Richtung, in die der
    Hang abfaellt).
    """
    zf = np.where(np.isnan(z), np.nanmean(z), z).astype("float32")
    gy, gx = np.gradient(zf, res)        # gy=d/Zeile (Nord-Sued), gx=d/Spalte (Ost-West)
    aspect = (np.degrees(np.arctan2(-gx, gy)) % 360.0).astype("float32")
    slope = np.degrees(np.arctan(np.hypot(gx, gy))).astype("float32")
    aspect = np.where(slope < 1.5, np.nan, aspect)   # flach -> keine Exposition
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
    aspect_png, png_b = _class_to_png_b64(aspect_cls, transform, aoi.crs, bounds, png_w=3000)
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
    left, bottom, right, top = array_bounds(dh, dw, dst_t)

    rad = _radiation_inputs(bounds, aoi, use_synthetic)

    stations = []
    if not use_synthetic and center_date is None:
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
    }
    html = _HTML.replace("/*META*/", json.dumps(meta))
    for tok, blob in [("__SNOW__", snow), ("__TEMP__", temp), ("__SUN__", sun),
                      ("__SPD__", spd), ("__DIR__", wdir), ("__WINDG__", windg),
                      ("__RSLOPE__", rslope), ("__RASPECT__", raspect), ("__RHOR__", rhor),
                      ("__PREC__", prec), ("__MASPECT__", maspect), ("__MSLOPE__", mslope),
                      ("__MELEV__", melev),
                      ("__ASPECTPNG__", data["aspect_png"]), ("__ROUGHPNG__", data["rough_png"])]:
        html = html.replace(f'"{tok}"', json.dumps(blob))
    out_html.write_text(html, encoding="utf-8")
    return out_html


_HTML = r"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Swiss Snow Model</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/nouislider@15.7.1/dist/nouislider.min.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/nouislider@15.7.1/dist/nouislider.min.js"></script>
<style>
 :root{--fg:#e8ecf1;--fg2:#c0c8d4;--mut:#8694a6;--acc:#5b9cf5;--acc2:#3d7de0;--bd:rgba(255,255,255,.12);--glass:rgba(15,20,35,.72);--glass2:rgba(15,20,35,.85);--glow:rgba(91,156,245,.15)}
 *{box-sizing:border-box}
 html,body{margin:0;height:100%;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:var(--fg)}
 #map{position:absolute;inset:0}
 #flow{position:absolute;inset:0;z-index:450;pointer-events:none}
 .panel{position:absolute;z-index:1000;top:12px;left:12px;width:392px;max-width:calc(100vw - 24px);
   background:var(--glass);backdrop-filter:blur(18px) saturate(1.4);-webkit-backdrop-filter:blur(18px) saturate(1.4);border:1px solid var(--bd);border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.06);overflow:hidden}
 .phead{display:flex;align-items:center;justify-content:space-between;padding:13px 16px;cursor:pointer}
 .phead h3{margin:0;font-size:16px;font-weight:650}
 .tog{font-size:18px;color:var(--mut);border:none;background:none;cursor:pointer}
 .pbody{padding:0 16px 16px;max-height:74vh;overflow:auto;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.15) transparent}
 .sec{margin-top:15px}
 .cap{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);margin-bottom:8px}
 .seg{display:flex;flex-wrap:wrap;gap:6px}
 .seg button{border:1px solid var(--bd);background:rgba(255,255,255,.07);border-radius:10px;padding:8px 12px;cursor:pointer;font-size:13px;min-height:38px;color:var(--fg2);transition:.15s;backdrop-filter:blur(4px)}
 .seg button:hover{border-color:var(--acc);background:rgba(255,255,255,.12)}
 .seg button.active{background:var(--acc2);color:#fff;border-color:var(--acc);font-weight:600;box-shadow:0 0 12px var(--glow)}
 #band{margin:20px 8px 6px}
 .noUi-connect{background:var(--acc2)!important}
 .noUi-target{background:rgba(255,255,255,.1)!important;border-color:var(--bd)!important;box-shadow:none!important}
 .noUi-handle{background:var(--acc)!important;border:none!important;box-shadow:0 0 6px rgba(91,156,245,.4)!important}
 .noUi-tooltip{background:var(--glass2)!important;color:var(--fg)!important;border:1px solid var(--bd)!important;font-size:11px!important}
 .winlbl{font-size:13px;margin-top:13px;font-weight:600}
 .sub{font-size:12px;color:var(--mut)}
 .ck{display:flex;align-items:center;gap:9px;margin-top:15px;font-size:13px;cursor:pointer;color:var(--fg2)}
 .ck input{width:18px;height:18px;accent-color:var(--acc)}
 .asp-crisp img{image-rendering:pixelated;image-rendering:crisp-edges}
 .legend{position:absolute;z-index:1000;bottom:16px;left:12px;background:var(--glass);backdrop-filter:blur(16px) saturate(1.3);-webkit-backdrop-filter:blur(16px) saturate(1.3);border:1px solid var(--bd);padding:10px 12px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.35);font-size:12px;max-width:244px;line-height:1.5;color:var(--fg2)}
 .legend i{display:inline-block;width:13px;height:13px;margin-right:6px;vertical-align:-2px;border-radius:2px}
 .collapsed .pbody{display:none}
 .stn{background:rgba(15,20,35,.7);border:2px solid var(--acc);border-radius:11px;padding:1px 6px;font-size:11px;font-weight:700;color:var(--acc);text-align:center;box-shadow:0 1px 6px rgba(0,0,0,.5);white-space:nowrap}
 .scl{position:relative;width:104px;height:52px;font-size:10px;font-weight:700;text-align:center;pointer-events:none}
 .scl>div{position:absolute;left:0;right:0;text-shadow:0 0 3px rgba(0,0,0,.8),0 0 6px rgba(0,0,0,.5);white-space:nowrap}
 .scl .s-t{top:-1px;color:#6bc5f0}.scl .s-b{bottom:-1px;color:#f07070}
 .scl .s-row{top:17px;display:flex;align-items:center;justify-content:center;gap:5px}
 .scl .s-l{color:#b0c4de}.scl .s-r{color:#70d890}
 .scl .s-c{pointer-events:auto;background:rgba(15,20,35,.75);border:2px solid var(--acc);border-radius:9px;color:var(--acc);padding:0 4px;font-size:11px;box-shadow:0 1px 5px rgba(0,0,0,.45);cursor:pointer}
 .scard{font:12.5px system-ui;line-height:1.5;min-width:150px;color:var(--fg)}
 .scard b{font-size:13px}
 .scard .g{display:grid;grid-template-columns:auto auto;gap:2px 14px;margin-top:6px}
 .scard .k{color:var(--mut)}
 .wn{font-size:9.5px;color:#c0d0e0;text-shadow:0 0 3px rgba(0,0,0,.8);font-weight:600}
 .icard{font:12.5px system-ui;line-height:1.5;min-width:200px;max-width:280px;color:var(--fg)}
 .icard b{font-size:13px}
 .icard .ig{display:grid;grid-template-columns:auto auto;gap:2px 14px;margin-top:6px}
 .icard .ik{color:var(--mut);white-space:nowrap}
 .icard .isep{grid-column:1/-1;border-top:1px solid var(--bd);margin:4px 0}
 .icard .ipow{margin-top:6px;padding:4px 8px;border-radius:6px;font-weight:600;font-size:12px;text-align:center}
 .icard .ipow.yes{background:rgba(91,156,245,.2);color:#8ec4ff}
 .icard .ipow.no{background:rgba(255,100,100,.15);color:#ff9090}
 .leaflet-popup-content-wrapper{background:var(--glass2)!important;backdrop-filter:blur(14px)!important;-webkit-backdrop-filter:blur(14px)!important;border:1px solid var(--bd)!important;color:var(--fg)!important;box-shadow:0 6px 24px rgba(0,0,0,.5)!important}
 .leaflet-popup-tip{background:var(--glass2)!important}
 .leaflet-popup-close-button{color:var(--mut)!important}
 .leaflet-popup-close-button:hover{color:var(--fg)!important}
 @media (max-width:560px){
   .panel{top:auto;bottom:0;left:0;right:0;width:100%;max-width:100%;border-radius:20px 20px 0 0;max-height:55vh}
   .phead{padding:10px 16px}.phead::before{content:'';display:block;width:36px;height:4px;border-radius:2px;background:rgba(255,255,255,.25);margin:0 auto 8px}
   .pbody{max-height:42vh;padding-bottom:env(safe-area-inset-bottom,12px)}
   .legend{font-size:11px;max-width:170px;bottom:auto;top:10px;left:8px}
 }
</style></head><body>
<div id="map"></div>
<canvas id="flow"></canvas>
<div class="panel" id="panel">
 <div class="phead" id="phead"><h3>Swiss Snow Model</h3><button class="tog" id="tog">▾</button></div>
 <div class="pbody">
   <div class="sec"><div class="cap">Layer</div>
     <div class="seg" id="layer">
       <button data-l="snow" class="active">New Snow</button>
       <button data-l="temp">Temperature</button>
       <button data-l="wind">Wind</button>
       <button data-l="sun">Sunshine</button>
       <button data-l="rad">Radiation</button>
       <button data-l="radsun">Eff. Radiation</button>
       <button data-l="slope">Slope</button>
       <button data-l="aspect">Aspect</button>
       <button data-l="rough">Roughness</button>
       <button data-l="tsurf">T Surface</button>
       <button data-l="shade">Hillshade</button>
       <button data-l="powder">Powder</button>
     </div></div>
   <div class="sec" id="statRow" style="display:none"><div class="cap">Statistic</div>
     <div class="seg" id="stat">
       <button data-s="avg" class="active">Ø Mean</button><button data-s="max">Max</button>
       <button data-s="min">Min</button>
       <button data-s="sub0">always &lt;0°C</button><button data-s="max05">Max 0–5°C</button>
       <button data-s="lt10">max &lt;10 km/h</button></div></div>
   <div class="sec"><div class="cap">Time Window (±5 days)</div>
     <div id="band"></div>
     <div class="seg" id="presets" style="margin-top:16px">
       <button data-h="24">24h</button><button data-h="48">48h</button>
       <button data-h="72">72h</button><button data-h="120">120h</button></div>
     <div class="winlbl" id="window"></div></div>
   <label class="ck"><input type="checkbox" id="stnToggle" checked/> Show SLF stations</label>
   <div class="sub" id="hint" style="margin-top:10px">Click map = inspect cell. Hover layer button = legend. Click station = measurements.</div>
 </div>
</div>
<div class="legend" id="legend"></div>
<script>
const M=/*META*/;
function dec(b){const s=atob(b),n=s.length,a=new Uint8Array(n);for(let i=0;i<n;i++)a[i]=s.charCodeAt(i);return a;}
const SNOW=dec("__SNOW__"),TEMP=dec("__TEMP__"),SUN=dec("__SUN__"),SPD=dec("__SPD__"),WDIR=dec("__DIR__");
const WINDG=dec("__WINDG__"),RSLOPE=dec("__RSLOPE__"),RASPECT=dec("__RASPECT__"),RHOR=dec("__RHOR__");
const PREC=dec("__PREC__"),MASPECT=dec("__MASPECT__"),MSLOPE=dec("__MSLOPE__"),MELEV=dec("__MELEV__");
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
// Karte + Layer
const [laMin,loMin,laMax,loMax]=M.bounds;
const map=L.map('map').fitBounds([[laMin,loMin],[laMax,loMax]]);
const base=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg",{attribution:"© swisstopo / MeteoSwiss / SLF / Copernicus"}).addTo(map);
const slopeWMTS=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.hangneigung-ueber_30/default/current/3857/{z}/{x}/{y}.png",{opacity:.7});
const reliefWMTS=L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissalti3d-reliefschattierung_monodirektional/default/current/3857/{z}/{x}/{y}.png",{opacity:.85});
const aspectImg=L.imageOverlay(ASPECT_PNG,[[M.png_bounds[0],M.png_bounds[1]],[M.png_bounds[2],M.png_bounds[3]]],{opacity:.72,className:'asp-crisp'});
const roughImg=L.imageOverlay(ROUGH_PNG,[[M.png_bounds[0],M.png_bounds[1]],[M.png_bounds[2],M.png_bounds[3]]],{opacity:.78});
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
let layer="snow",stat="avg",a=M.today_index,b=Math.min(T,M.today_index+72),showStn=true,wtimer=null;

function setRaster(get,border){const img=cx.createImageData(W,H),d=img.data;const cls=border?new Int16Array(NP):null;
  for(let p=0;p<NP;p++){const r=get(p);const o=p*4;if(r){d[o]=r[0];d[o+1]=r[1];d[o+2]=r[2];d[o+3]=r[3]==null?210:r[3];if(cls)cls[p]=r[4];}else{d[o+3]=0;if(cls)cls[p]=-999;}}
  if(border){for(let y=0;y<H;y++)for(let x=0;x<W;x++){const p=y*W+x;if(cls[p]==-999)continue;const rt=x<W-1?cls[p+1]:cls[p],bt=y<H-1?cls[p+W]:cls[p];if(rt!=cls[p]||bt!=cls[p]){const o=p*4;d[o]=20;d[o+1]=20;d[o+2]=30;d[o+3]=230;}}}
  cx.putImageData(img,0,0);raster.setUrl(cv.toDataURL());}
function aggT(p,m){let mn=1e9,mx=-1e9,su=0,c=0,cold=0;for(let t=a;t<b;t++){const v=tv(t,p);mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;if(v<0)cold++;}return m=="max"?mx:m=="min"?mn:m=="sub0"?cold:m=="max05"?mx:su/Math.max(1,c);}
function renderRaster(){
  if(layer=="snow"){const ca=a*NP,cb=b*NP;setRaster(p=>{const v=cum[cb+p]-cum[ca+p];const c=snowCol(v);return c?[c[0],c[1],c[2],235]:null;});}
  else if(layer=="temp"){setRaster(p=>{let mn=1e9,mx=-1e9,su=0,c=0;for(let t=a;t<b;t++){const v=tv(t,p);mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;}
      if(stat=="sub0"){if(mx>=0)return null;const x=Math.min(1,-mx/20);return[40,120-(x*60|0),255,215];}
      if(stat=="max05"){if(mx<0||mx>5)return null;const x=mx/5;return[255,200-(x*110|0),60,235];}
      const v=stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c);const col=tempCol(v);return[col[0],col[1],col[2],205];});}
  else if(layer=="wind"){setRaster(p=>{let mn=1e9,mx=-1e9,su=0,c=0;for(let t=a;t<b;t++){const v=wg_(t,p)*3.6;mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;}
      if(stat=="lt10"){if(mx>=10)return null;const x=mx/10;return[40,150+(x*40|0),90-(x*40|0),215];}
      const val=stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c);if(val<1)return null;const col=rampBYR(val/70);return[col[0],col[1],col[2],195];});}
  else if(layer=="sun"){const vmax=48;setRaster(p=>{let s=0;for(let t=a;t<b;t++)s+=sunv(t,p);if(s<0.3)return null;const c=sunCol(s,vmax);return[c[0],c[1],c[2],205];});}
  else if(layer=="tsurf"){setRaster(p=>{let mn=1e9,mx=-1e9,su=0,c=0;for(let t=a;t<b;t++){const v=tsurfEst(t,p);mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;}
      if(stat=="sub0"){if(mx>=0)return null;const x=Math.min(1,-mx/20);return[20,80,180,215];}
      if(stat=="max05"){if(mx<0||mx>5)return null;const x=mx/5;return[200,140-(x*80|0),255-(x*200|0),235];}
      const v=stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c);const col=tempCol(v);return[col[0],col[1],col[2],205];});}
  else if(layer=="powder"){setRaster(p=>{const r=computePowder(p,a,b);if(!r.powdered)return null;return r.quality==='reduced'?[180,205,245,140]:[200,220,255,180];});}
  else setRaster(_=>null);
}
function windStat(k){let mn=1e9,mx=-1e9,su=0,c=0,ss=0,sc=0;
  for(let t=a;t<b;t++){const v=SPD[t*P+k]/M.spd_mul,dd=WDIR[t*P+k]*M.dir_div*Math.PI/180;
    mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;ss+=Math.sin(dd);sc+=Math.cos(dd);}
  return{v:(stat=="max"?mx:stat=="min"?mn:su/Math.max(1,c)),dir:(Math.atan2(ss,sc)*180/Math.PI+360)%360};}
function renderWind(){windArr.clearLayers();if(layer!="wind")return;
  for(let k=0;k<P;k++){const w=windStat(k),kmh=w.v*3.6;
    const col=rampBYR(kmh/70),ang=(w.dir+180)%360,len=10+Math.min(22,kmh*0.45);
    const html=`<div style="opacity:.9;transform:rotate(${ang}deg);transform-origin:center"><svg width="${len}" height="10" style="overflow:visible"><line x1="0" y1="5" x2="${len-5}" y2="5" stroke="rgb(${col.join(",")})" stroke-width="2"/><polygon points="${len-5},1.5 ${len},5 ${len-5},8.5" fill="rgb(${col.join(",")})"/></svg></div><div class="wn" style="text-align:center">${kmh.toFixed(0)}</div>`;
    L.marker([M.wind.lat[k],M.wind.lon[k]],{icon:L.divIcon({className:'',html:html,iconSize:[42,22],iconAnchor:[21,11]})}).addTo(windArr);}
}
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
  for(const s of M.stations){const ns=newSnowInt(s);
    const hs=s.hs_now!=null?s.hs_now.toFixed(0):"–";
    const nsv=ns!=null?"+"+ns.toFixed(0):"";
    const wind=s.vw!=null?(s.dw!=null?dirAb(s.dw)+" ":"")+(s.vw*3.6).toFixed(0):"";
    const tss=s.tss!=null?s.tss.toFixed(0)+"°":"";
    const ta=s.ta!=null?s.ta.toFixed(0)+"°":"";
    const html=`<div class="scl"><div class="s-t">${tss}</div>`+
      `<div class="s-row"><span class="s-l">${wind}</span><span class="s-c">${hs}</span><span class="s-r">${nsv}</span></div>`+
      `<div class="s-b">${ta}</div></div>`;
    const m=L.marker([s.lat,s.lon],{icon:L.divIcon({className:'',html:html,iconSize:[104,52],iconAnchor:[52,26]}),zIndexOffset:500});
    m.bindPopup(stationCard(s),{maxWidth:260});m.addTo(stnGroup);}
}
function fmt(i){const d=new Date(M.times[Math.max(0,Math.min(T-1,i))]+"Z");return d.toLocaleString('en-GB',{weekday:'short',day:'2-digit',month:'2-digit',hour:'2-digit'});}
function dayLabel(doy){const d=new Date(2026,0,1);d.setDate(doy);return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'});}
function legendFor(l){const sn={avg:'Mean',max:'Max',min:'Min',sub0:'always <0°C',max05:'Max 0–5°C',lt10:'max <10 km/h'}[stat];
  if(l=="snow"){let h="<b>New Snow [cm] (SLF scale)</b><br>";for(let i=0;i<SB.length-1;i++)h+=`<div><i style="background:${SC[i]}"></i>${SB[i]}–${SB[i+1]}</div>`;return h+"<div style='margin-top:5px'><span class='stn' style='padding:0 3px'>NN</span> Station (click for details)</div>";}
  if(l=="temp"){let extra="blue=cold · red=warm";if(stat=="sub0")extra="only cells staying below 0°C for entire window";if(stat=="max05")extra="only cells with max 0–5°C";return `<b>Temp 2 m [°C] (${sn})</b><br>${extra}`;}
  if(l=="wind"){let extra="color=speed, arrows=direction, flow animation";if(stat=="lt10")extra="green = max wind stays below 10 km/h";return "<b>Wind 10 m (km/h, "+sn+")</b><br>"+extra;}
  if(l=="sun")return "<b>Σ Sunshine Hours</b><br>Scale 0–48 h+ · light→orange = more sun";
  if(l=="rad")return "<b>Clear-sky Radiation [Wh/m²/d]</b><br>Day: "+dayLabel(bandDoy())+" (= window start)<br>dark=shade/low · yellow=high<br>incl. slope, aspect & terrain shadow";
  if(l=="radsun")return "<b>Effective Radiation [Wh/m²/d]</b><br>Clear-sky × cloud attenuation (20% diffuse + 80% × sunshine)<br>Day: "+dayLabel(bandDoy());
  if(l=="slope")return "<b>Slope Classes (swisstopo)</b><br>all classes from 30° (30/35/40/45°+)";
  if(l=="aspect")return '<b>Aspect (swissALTIRegio)</b><br><div><i style="background:#4A90D9"></i>N (315°–45°)</div><div><i style="background:#66BB6A"></i>E (45°–135°)</div><div><i style="background:#EF5350"></i>S (135°–225°)</div><div><i style="background:#FFC107"></i>W (225°–315°)</div><div><i style="background:#9E9E9E"></i>Flat (&lt;5°)</div>';
  if(l=="tsurf"){let extra="estimated: air ± radiative cooling/warming";if(stat=="sub0")extra="only cells with max surface temp &lt;0°C";if(stat=="max05")extra="only cells with max 0–5°C";return `<b>T Surface [°C] (${sn})</b><br>${extra}`;}
  if(l=="rough")return "<b>Terrain Roughness</b><br>light→dark brown = rougher";
  if(l=="powder")return '<b>Powder Conditions</b><br><div><i style="background:rgba(200,220,255,.7)"></i>Powder (stable)</div><div><i style="background:rgba(180,205,245,.55)"></i>Powder (reduced)</div><div style="margin-top:4px;font-size:11px">Gust ≈ mean wind × 1.5</div>';
  return "<b>Hillshade / Relief (swisstopo)</b>";}
function legend(l){document.getElementById('legend').innerHTML=legendFor(l||layer);}
function showOverlay(){
  [slopeWMTS,reliefWMTS].forEach(x=>map.removeLayer(x));[aspectImg,roughImg,radOverlay].forEach(x=>map.removeLayer(x));
  const grid=(layer=="snow"||layer=="temp"||layer=="sun"||layer=="wind"||layer=="powder"||layer=="tsurf");
  const radg=(layer=="rad"||layer=="radsun");
  raster.setOpacity(grid?0.82:0);
  if(radg)map.addLayer(radOverlay);
  if(layer=="slope")map.addLayer(slopeWMTS);
  else if(layer=="shade")map.addLayer(reliefWMTS);
  else if(layer=="aspect")map.addLayer(aspectImg);
  else if(layer=="rough")map.addLayer(roughImg);
  if(layer=="wind"){map.addLayer(windArr);startFlow();}else{map.removeLayer(windArr);stopFlow();}
}
function renderAll(){showOverlay();renderRaster();renderStations();
  if(layer=="rad"||layer=="radsun")renderRadiation();
  if(layer=="wind"){buildFlow();if(wtimer)clearTimeout(wtimer);wtimer=setTimeout(renderWind,120);}
  document.getElementById('window').innerHTML=`<b>${fmt(a)}</b> → <b>${fmt(b)}</b> (${b-a} h)`;legend();}
document.querySelectorAll('#layer button').forEach(btn=>{
  btn.onclick=()=>{document.querySelectorAll('#layer button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');layer=btn.dataset.l;
    document.getElementById('statRow').style.display=(layer=="temp"||layer=="wind"||layer=="tsurf")?"block":"none";
    document.querySelectorAll('#stat [data-s=sub0],#stat [data-s=max05]').forEach(x=>x.style.display=(layer=="temp"||layer=="tsurf")?"":"none");
    document.querySelectorAll('#stat [data-s=lt10]').forEach(x=>x.style.display=(layer=="wind")?"":"none");
    if((layer=="wind"&&(stat=="sub0"||stat=="max05"))||((layer=="temp"||layer=="tsurf")&&stat=="lt10")){stat="avg";document.querySelectorAll('#stat button').forEach(x=>x.classList.toggle('active',x.dataset.s=="avg"));}
    renderAll();};
  btn.onmouseenter=()=>legend(btn.dataset.l);btn.onmouseleave=()=>legend();});
document.querySelectorAll('#stat button').forEach(btn=>btn.onclick=()=>{document.querySelectorAll('#stat button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');stat=btn.dataset.s;renderAll();});
document.querySelectorAll('#presets button').forEach(btn=>btn.onclick=()=>{const h=+btn.dataset.h;let na=M.today_index,nb=Math.min(T,na+h);if(nb>=T){nb=T;na=Math.max(0,T-h);}band.noUiSlider.set([na,nb]);});
document.getElementById('stnToggle').onchange=e=>{showStn=e.target.checked;renderStations();};
document.getElementById('phead').onclick=()=>{const p=document.getElementById('panel');p.classList.toggle('collapsed');document.getElementById('tog').textContent=p.classList.contains('collapsed')?'▸':'▾';};
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
const band=document.getElementById('band');
noUiSlider.create(band,{start:[a,b],connect:true,step:1,range:{min:0,max:T},tooltips:[{to:fmt,from:Number},{to:fmt,from:Number}]});
let raf=null;band.noUiSlider.on('update',v=>{a=Math.round(+v[0]);b=Math.max(a+1,Math.round(+v[1]));if(raf)cancelAnimationFrame(raf);raf=requestAnimationFrame(renderAll);});
// --- Point Inspector (universal click popup) ---
map.setMaxBounds([[laMin-0.15,loMin-0.3],[laMax+0.15,loMax+0.3]]);
map.setMinZoom(map.getBoundsZoom([[laMin,loMin],[laMax,loMax]]));
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
  let h='<div class="icard"><b>'+lat.toFixed(4)+'° N, '+lon.toFixed(4)+'° E</b>';
  h+='<div class="ig">';
  h+=R('Elevation',elev.toFixed(0)+' m');
  h+=R('Slope',slp.toFixed(0)+'°');
  h+=R('Aspect',quad+' ('+asp.toFixed(0)+'°)');
  h+='<div class="isep"></div>';
  h+=R('New Snow',newSnow.toFixed(1)+' cm');
  h+=R('T Air Ø',tmean.toFixed(1)+' °C');
  h+=R('T Air Min',tmin.toFixed(1)+' °C');
  h+=R('T Air Max',tmax.toFixed(1)+' °C');
  h+=R('T Surf Ø',tsMean.toFixed(1)+' °C');
  h+=R('T Surf Min',tsMin.toFixed(1)+' °C');
  h+=R('Freeze-Thaw',ftc+' cycles');
  h+='<div class="isep"></div>';
  h+=R('Wind Ø',wmean.toFixed(0)+' km/h');
  h+=R('Wind Max',wmax.toFixed(0)+' km/h');
  h+=R('≈ Gust Max',(wmax*PD_GUST_FACTOR).toFixed(0)+' km/h');
  h+='<div class="isep"></div>';
  h+=R('Rad. Clear',solar.toFixed(0)+' Wh/m²/d');
  let sunSum=0;for(let t=a;t<b;t++)sunSum+=sunv(t,p);
  const sunFrac=Math.max(0,Math.min(1,sunSum/(0.42*Math.max(1,b-a))));
  const effRad=solar*(0.2+0.8*sunFrac);
  h+=R('Rad. Eff.',effRad.toFixed(0)+' Wh/m²/d');
  h+='</div>';
  h+='<div class="ipow '+(pw.powdered?'yes':'no')+'">';
  h+='Powder: '+(pw.powdered?'YES':'NO');
  if(pw.quality==='reduced')h+=' (reduced)';
  h+='</div>';
  if(pw.reason_flags.length)h+='<div style="font-size:11px;color:var(--mut);margin-top:4px">'+pw.reason_flags.join(', ')+'</div>';
  if(pw.valid_aspects.length)h+='<div style="font-size:11px;margin-top:2px">Valid: '+pw.valid_aspects.join(', ')+'</div>';
  h+='</div>';
  inspPopup=L.popup({maxWidth:300}).setLatLng(e.latlng).setContent(h).openOn(map);
});
renderAll();
</script></body></html>
"""
