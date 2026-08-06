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
_ELEV_Q = 4.0            # metres per step in the fine elevation raster
_ELEV_SCALE = 20.0
_WIND_STEP = 9000.0
_FINE_RES = 30.0        # swissALTIRegio (10 m Quelle) — feiner gelesen, damit
                        # Felswaende nicht zu Rampen verschliffen werden
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
    0: (0x9E, 0x9E, 0x9E, 178),  # flat grey
    1: (0x4A, 0x90, 0xD9, 255),  # N  blue
    2: (0x45, 0xC0, 0xC0, 255),  # NE teal
    3: (0x66, 0xBB, 0x6A, 255),  # E  green
    4: (0xBE, 0xDB, 0x39, 255),  # SE lime
    5: (0xEF, 0x53, 0x50, 255),  # S  red
    6: (0xFF, 0x9E, 0x42, 255),  # SW orange
    7: (0xFF, 0xC1, 0x07, 255),  # W  yellow
    8: (0xA9, 0x87, 0xE0, 255),  # NW violet
}


def _aspect_classify(aspect_deg, slope_deg):
    """Classify aspect into 8-way index (0=flat,1=N,2=NE,3=E,4=SE,5=S,6=SW,7=W,8=NW).

    45 deg sectors centred on each direction (N=[337.5,22.5)). Done BEFORE
    reprojection so nearest-neighbour on the integer index never blends.
    """
    cls = np.zeros(aspect_deg.shape, dtype="uint8")
    a = aspect_deg % 360
    steep = slope_deg >= 5.0
    for lo, hi, idx in [(337.5, 360.0, 1), (0.0, 22.5, 1), (22.5, 67.5, 2),
                        (67.5, 112.5, 3), (112.5, 157.5, 4), (157.5, 202.5, 5),
                        (202.5, 247.5, 6), (247.5, 292.5, 7), (292.5, 337.5, 8)]:
        cls[steep & (a >= lo) & (a < hi)] = idx
    return cls


def _class_to_png_b64(cls_lv95, src_t, src_crs, bounds, png_w=3000):
    """Reproject integer class raster -> WGS84, then map to RGBA PNG."""
    h, w = cls_lv95.shape
    dst_t, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h, *bounds)
    scale = max(1.0, dw / png_w)
    dw2, dh2 = int(dw / scale), int(dh / scale)
    dst_t2 = from_origin(dst_t.c, dst_t.f, (dst_t.a * dw) / dw2, (-dst_t.e * dh) / dh2)
    cls_wgs = np.zeros((dh2, dw2), dtype="uint8")
    # mode() = majority of the underlying fine cells. nearest() picks one sample
    # per output cell, which aliases into stripes across steep, fast-changing
    # terrain; the majority is both stabler and truer to the ground.
    reproject(source=cls_lv95, destination=cls_wgs, src_transform=src_t,
              src_crs=src_crs, dst_transform=dst_t2, dst_crs="EPSG:4326",
              resampling=Resampling.mode)
    rgba = np.zeros((dh2, dw2, 4), dtype="uint8")
    for idx, (r, g, b, a) in _ASPECT_COLORS.items():
        mask = cls_wgs == idx
        rgba[mask] = [r, g, b, a]
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    left, bottom, right, top = array_bounds(dh2, dw2, dst_t2)
    return base64.b64encode(buf.getvalue()).decode(), (bottom, left, top, right)


def _elev_to_png_b64(elev, slope, src_t, src_crs, bounds, png_w=2200):
    """Reproject elevation + slope -> WGS84 and encode as RGBA:
    (R*256+G)*_ELEV_Q = elevation in metres, B = slope in degrees.
    Both are reprojected as floats BEFORE byte-splitting, and the client
    samples them bilinearly, so point values stay accurate between pixels.
    """
    h, w = elev.shape
    dst_t, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h, *bounds)
    scale = max(1.0, dw / png_w)
    dw2, dh2 = int(dw / scale), int(dh / scale)
    dst_t2 = from_origin(dst_t.c, dst_t.f, (dst_t.a * dw) / dw2, (-dst_t.e * dh) / dh2)

    def _rp(src):
        out = np.zeros((dh2, dw2), "float32")
        reproject(source=src.astype("float32"), destination=out, src_transform=src_t,
                  src_crs=src_crs, dst_transform=dst_t2, dst_crs="EPSG:4326",
                  resampling=Resampling.bilinear)
        return out

    e = np.clip(np.round(_rp(elev) / _ELEV_Q), 0, 65535).astype("uint32")
    sl = np.clip(np.round(_rp(slope)), 0, 90).astype("uint8")
    rgba = np.zeros((dh2, dw2, 4), dtype="uint8")
    rgba[:, :, 0] = ((e >> 8) & 0xFF).astype("uint8")
    rgba[:, :, 1] = (e & 0xFF).astype("uint8")
    rgba[:, :, 2] = sl
    rgba[:, :, 3] = 255
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
        # average() smears a cliff into a smooth ramp, which is what makes the
        # 8-way aspect band up like contour lines in steep rock; bilinear keeps
        # the real gradient direction.
        arr = ds.read(1, window=win, out_shape=(oh, ow),
                      resampling=Resampling.bilinear, boundless=True, fill_value=np.nan)
    arr = np.where(arr < -100, np.nan, arr).astype("float32")
    return _DEM(arr, from_origin(e0, n1, res, res), res, bounds, "EPSG:2056")


def _corrected_aspect(z, res):
    """Aspect [deg, clockwise from N] + slope [deg] using Horn's method.

    Horn (1981) 3x3 weighted gradient — same algorithm as GDAL gdaldem.
    Convention: N=0, E=90, S=180, W=270 (downslope direction).
    """
    zf = np.where(np.isnan(z), np.nanmean(z), z).astype("float32")
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
    # keep a decimated copy of the TRUE 60 m slope for the fine terrain raster
    slope_dec = np.ascontiguousarray(slope_deg[::4, ::4]).astype("float32")
    del aspect_deg, slope_deg
    aspect_png, png_b = _class_to_png_b64(aspect_cls, transform, aoi.crs, bounds, png_w=6000)
    del aspect_cls

    # --- Rauigkeit & TPI aus dezimiertem DEM (4x groeber) ---
    zc = np.nan_to_num(dem.elevation, nan=float(np.nanmean(dem.elevation)))[::4, ::4]
    del dem
    rough = roughness(zc)
    vmax = float(np.nanpercentile(rough, 98))
    ct = from_origin(bounds[0], bounds[3], res * 4, res * 4)
    sd = slope_dec[:zc.shape[0], :zc.shape[1]]
    if sd.shape != zc.shape:  # guard against odd rounding of the decimation
        sd = np.zeros_like(zc, dtype="float32")
    elev_png, elev_b = _elev_to_png_b64(zc, sd, ct, aoi.crs, bounds, png_w=2200)
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
            "elev_png": elev_png, "elev_bounds": elev_b, "expo_at": expo_at}


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
        "png_bounds": fine["png_bounds"], "elev_png": fine["elev_png"],
        "elev_bounds": fine["elev_bounds"], "rad": rad, "stations": stations,
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
        "ELEVPNG": data["elev_png"],
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
        "elev_bounds": data["elev_bounds"], "elev_q": _ELEV_Q,
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
    payload = json.dumps({"meta": meta, "b": blobs})
    (ddir / dataname).write_text(payload, encoding="utf-8")
    # real gzip alongside (GitHub Pages does not reliably compress huge JSON);
    # the boot loader picks .gz when DecompressionStream is available.
    import gzip as _gzip
    with _gzip.GzipFile(str(ddir / (dataname + ".gz")), "wb", compresslevel=6, mtime=0) as gf:
        gf.write(payload.encode("utf-8"))
    (ddir / "latest.json").write_text(json.dumps(
        {"version": stamp, "data": dataname, "gz": dataname + ".gz",
         "generated": datetime.now().replace(microsecond=0).isoformat() + "Z"}),
        encoding="utf-8")
    # keep the last few snapshots for rollback, prune older ones
    for old in sorted(ddir.glob("snowdata-*.json"))[:-4]:
        try:
            old.unlink()
            (old.parent / (old.name + ".gz")).unlink(missing_ok=True)
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
  function prog(f){var b=document.querySelector('#boot i');
    if(b)b.style.width=(Math.max(2,Math.min(1,f)*100))+'%';}
  function bootDone(){var e=document.getElementById('boot');if(!e)return;
    prog(1);e.classList.add('done');setTimeout(function(){if(e.parentNode)e.parentNode.removeChild(e);},300);}
  window.__bootDone=bootDone;
  function fail(m){
    var e=document.getElementById('boot');if(e&&e.parentNode)e.parentNode.removeChild(e);
    var d=document.getElementById('bootErr');
    if(!d){d=document.createElement('div');d.id='bootErr';document.body.appendChild(d);}
    d.textContent=m;
    var b=document.createElement('button');b.textContent='Erneut versuchen';
    b.onclick=function(){location.reload();};d.appendChild(b);
    d.classList.add('show');
    try{if(window.Sentry)window.Sentry.captureMessage(m);}catch(e2){}}
  function fetchT(url,opts,ms){var c=('AbortController'in window)?new AbortController():null;
    if(c){opts=opts||{};opts.signal=c.signal;setTimeout(function(){c.abort();},ms||30000);}
    return fetch(url,opts);}
  // app.js parallel zum Datenblob laden (vorher: sequenziell danach)
  var appJsP=fetchT('app.js',{cache:'no-cache'},30000).then(function(r){if(!r.ok)throw new Error('app.js HTTP '+r.status);return r.text();});
  function waitLeaflet(){return new Promise(function(res,rej){var t0=Date.now();
    (function poll(){if(window.L)return res();if(Date.now()-t0>20000)return rej(new Error('Leaflet timeout'));setTimeout(poll,150);})();});}
  (async function(){
    try{
      var isDemo=location.search.indexOf('demo')>=0;
      var ljr=await fetchT(isDemo?'data/demo-latest.json':'data/latest.json',{cache:'no-cache'},15000);
      if(!ljr.ok&&isDemo)ljr=await fetchT('data/latest.json',{cache:'no-cache'},15000);
      var lj=await ljr.json();
      var useGz=!!(lj.gz&&window.DecompressionStream);
      var res=await fetchT('data/'+(useGz?lj.gz:lj.data),{cache:'force-cache'},90000);
      if(!res.ok)throw new Error('HTTP '+res.status);
      var total=+(res.headers.get('Content-Length')||0),text;
      if(res.body&&total&&window.ReadableStream){
        var reader=res.body.getReader(),chunks=[],got=0,r;
        while(!(r=await reader.read()).done){chunks.push(r.value);got+=r.value.length;prog(got/total);}
        if(useGz){
          var ds=new Blob(chunks).stream().pipeThrough(new DecompressionStream('gzip'));
          text=await new Response(ds).text();
        }else{
          var buf=new Uint8Array(got),off=0;chunks.forEach(function(c){buf.set(c,off);off+=c.length;});
          text=new TextDecoder().decode(buf);
        }
      }else if(useGz){
        var ds2=(await res.blob()).stream().pipeThrough(new DecompressionStream('gzip'));
        text=await new Response(ds2).text();prog(1);
      }else{text=await res.text();prog(1);}
      window.__D=JSON.parse(text);text=null;
      var code=await appJsP;
      try{await waitLeaflet();}catch(e){fail('Karten-Bibliothek nicht erreichbar — Verbindung prüfen.');return;}
      await new Promise(function(res){var t0=Date.now();(function poll(){if(window.supabase||Date.now()-t0>2500)return res();setTimeout(poll,120);})();});
      var sc=document.createElement('script');sc.textContent=code;document.body.appendChild(sc);
      setTimeout(function(){if(!window.__APP_OK&&window.__introStuck)window.__introStuck('App-Start fehlgeschlagen — bitte erneut versuchen.');},6000);
    }catch(e){fail('Daten konnten nicht geladen werden — Verbindung prüfen.');try{if(window.Sentry)window.Sentry.captureException(e);}catch(_){}}
  })();
})();
</script>"""


def _firn_icon(size: int) -> Image.Image:
    """The Firn mark: a twin-peak silhouette (the firn line runs along a
    ridge), white on ink-900, matching the in-app brand glyph exactly."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=rad, fill=(11, 21, 32, 255))
    u = size / 24.0  # same coordinate space as the inline SVG mark
    pts = [(3 * u, 19 * u), (10 * u, 6 * u), (13.5 * u, 12 * u), (15 * u, 9 * u), (21 * u, 19 * u)]
    d.polygon(pts, fill=(255, 255, 255, 235))
    return img


def _write_pwa_assets(out_dir: Path) -> None:
    """Emit manifest, service worker and icons next to index.html so the app is
    installable (Add to Home Screen) and loads offline with the last data."""
    for px, name in [(180, "icon-180.png"), (192, "icon-192.png"), (512, "icon-512.png")]:
        try:
            _firn_icon(px).save(out_dir / name)
        except Exception:  # icon is cosmetic; never fail the build over it
            pass
    manifest = {
        "name": "Firn",
        "short_name": "Firn",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "description": "Firn — interaktive Neuschnee-, Pulver- und Skitauglichkeits-Karte für die Schweiz.",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (out_dir / "manifest.webmanifest").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "sw.js").write_text(_SERVICE_WORKER, encoding="utf-8")


_SERVICE_WORKER = r"""// Firn — service worker (installable + offline last-data).
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
<title>Firn</title>
<link rel="icon" type="image/png" href="icon-192.png"/>
<link rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,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"/>
<link rel="preconnect" href="https://unpkg.com" crossorigin>
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="preconnect" href="https://wmts.geo.admin.ch" crossorigin>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script defer src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
<!-- maplibre-gl (3D, ~800 KB) wird erst beim ersten 3D-Klick geladen -->
<link rel="manifest" href="manifest.webmanifest"/>
<link rel="apple-touch-icon" href="icon-180.png"/>
<!-- Sentry error tracking: paste your browser DSN into SENTRY_DSN to enable. -->
<script>
(function(){
  function stuck(msg){if(window.__APP_OK)return;
    var d=document.getElementById('bootErr');
    if(!d){d=document.createElement('div');d.id='bootErr';document.body.appendChild(d);}
    if(d.classList.contains('show'))return;
    d.textContent=msg;
    var b=document.createElement('button');b.textContent='Erneut versuchen';
    b.onclick=function(){location.reload();};d.appendChild(b);
    d.classList.add('show');
    var e=document.getElementById('boot');if(e&&e.parentNode)e.parentNode.removeChild(e);}
  window.__introStuck=stuck;
  window.addEventListener('error',function(e){
    if(!window.__APP_OK)stuck('Fehler beim Start: '+String((e&&e.message)||'unbekannt').slice(0,120));});
  setTimeout(function(){if(!window.__APP_OK)stuck('Der Start dauert ungewöhnlich lange — Verbindung prüfen.');},45000);
})();
</script>
<script>window.SENTRY_DSN='';(function(){if(!window.SENTRY_DSN)return;var s=document.createElement('script');s.src='https://browser.sentry-cdn.com/7.120.0/bundle.tracing.min.js';s.crossOrigin='anonymous';s.onload=function(){try{window.Sentry.init({dsn:window.SENTRY_DSN,tracesSampleRate:0,release:'firn'});}catch(e){}};document.head.appendChild(s);window.addEventListener('error',function(e){try{if(window.Sentry&&e.error)window.Sentry.captureException(e.error);}catch(_){}});window.addEventListener('unhandledrejection',function(e){try{if(window.Sentry)window.Sentry.captureException(e.reason);}catch(_){}});})();</script>
<!-- The map tiles are the first thing a user waits for: do the DNS + TLS
     handshake with the tile host while the scripts are still parsing. -->
<link rel="preconnect" href="https://wmts.geo.admin.ch" crossorigin>
<link rel="dns-prefetch" href="https://wmts.geo.admin.ch">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
 /* ===================================================================
    ALPIN GRID design tokens  —  see docs/design-system.md
    Colour law: colour is data, chrome is neutral, one accent at a time.
    Nothing outside this block may declare a raw colour for chrome.
    =================================================================== */
 :root{
   /* ===== Firn: data in snow, data in stone ============================
      Warm bone paper -- old snow in shade, and the stock a topographic map
      is printed on -- with warm charcoal ink instead of a cold navy. The two
      model accents are taken off the mountain: glacier for the forecast
      model, larch bark for the community one. Nothing here is white and
      nothing is sky blue; the only blues left in the product are the SLF
      snow-depth scale and the drawing pens, where blue means a number.
      ==================================================================== */
   --fg:#211E19;--fg2:#403B33;--mut:#736C61;
   --acc:#1D6A6A;--acc2:#8C4F27;
   --bd:rgba(33,30,25,.13);
   --glass:rgba(246,243,237,.84);--glass2:rgba(250,248,244,.93);
   --glow:rgba(29,106,106,.13);
   --panel-h:52px;--r:14px;--r-lg:18px;
   --blur:blur(22px) saturate(1.25);
   /* shadows carry the paper's warmth, so nothing greys out on the bone */
   --elev1:0 1px 3px rgba(33,30,25,.07);
   --elev2:0 6px 20px rgba(33,30,25,.10);
   --elev3:0 10px 34px rgba(33,30,25,.13);

   /* --- the newer component names, derived so nothing drifts apart --- */
   --ink-900:var(--fg);--ink-700:var(--fg2);--ink-500:var(--mut);
   --ink-300:#A79E90;--ink-150:#CFC7B9;--ink-100:rgba(33,30,25,.11);--ink-050:#F0ECE3;
   --paper:#F7F4EE;                    /* bone, not white */
   --accent:var(--acc);--accent-soft:var(--glow);
   --accent-meteo:#1D6A6A;--accent-meteo-soft:rgba(29,106,106,.13);
   --accent-report:#8C4F27;--accent-report-soft:rgba(140,79,39,.14);
   --ok:#4A7A3F;--warn:#C08A2E;--danger:#A83A2E;--danger-tint:rgba(168,58,46,.11);
   --sp1:4px;--sp2:8px;--sp3:12px;--sp4:16px;--sp5:24px;--sp6:32px;
   --r-1:var(--r);--r-2:var(--r-lg);--r-full:999px;
   --r-sm:10px;--r-xl:var(--r-lg);
   --lift:var(--elev3);
   --dur-1:.12s;--dur-2:.22s;--ease:cubic-bezier(.4,0,.2,1);
   --dur:.22s;--ease-spring:cubic-bezier(.34,1.4,.64,1);
   --tap:44px;--tap-sm:36px;
   --hair:var(--bd);--ring:var(--acc);
   /* cards sit a shade lighter than the page, and inputs a shade darker --
      all three on the bone, so nothing punches a white hole in the paper */
   --card:#FCFAF6;--fill:#EFEBE2;--fill2:#E3DDD1;--page:#F2EEE6;
   --topic-accent:var(--accent);--topic-tint:var(--accent-soft)}
 /* The frosted surfaces the original build floated over the map. Only the
    things that actually float get this; docked panels stay opaque so text on
    them never has to fight the terrain underneath. */
 #layerBar,#searchWrap input,.rail-btn,#demoPill,#legendBtn,.legend,
 .insp-panel,.toast,#coachCard,.feed-nav,.auth-modal,#disc .sheet{
   backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}
 @media (prefers-reduced-transparency:reduce){
   :root{--glass:var(--paper);--glass2:#FCFAF6}
   #layerBar,#searchWrap input,.rail-btn,#demoPill,#legendBtn,.legend,
   .insp-panel,.toast,#coachCard,.feed-nav,.auth-modal,#disc .sheet{
     backdrop-filter:none;-webkit-backdrop-filter:none;background:var(--paper)}}
 /* One palette. The app is read outdoors in snow glare against a light map,
    so the chrome is light too — there is no dark variant to drift out of sync. */
 /* P5: numbers a user compares are tabular so columns line up */
 .num,.snow-val,#tlLenVal,#progConfVal,#drawDepthVVal,#drawBrushVVal,.insp-chip,.tl-range{font-variant-numeric:tabular-nums}
 /* Micro-label (section labels, field labels) */
 .lbl-micro{font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-500)}
 /* Respect reduced-motion: kill non-essential animation/transition globally */
 @media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important;scroll-behavior:auto!important}}
 /* Higher-contrast borders/text for snow-glare / accessibility */
 /* Snow glare / high-contrast: darken the muted end of the ramp and the rules */
 @media (prefers-contrast:more){:root{--ink-500:#4A5563;--ink-150:#98A2AE;--ink-100:#C3CAD3}}
 /* Solid surfaces for users who reduce transparency (a11y) */
 /* --- Toast notifications (errors are no longer silent) --- */
 #toastWrap{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(env(safe-area-inset-bottom,0px) + 92px);z-index:9500;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;width:max-content;max-width:calc(100vw - 32px)}
 .toast{border:1px solid var(--ink-100);pointer-events:auto;display:flex;align-items:center;gap:9px;padding:11px 16px;border-radius:var(--r);font-size:13.5px;font-weight:600;color:var(--fg);background:var(--glass2);border:1px solid var(--ink-100);box-shadow:var(--elev2);max-width:100%;animation:toastIn .3s var(--ease-spring)}
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
 #statusScrimB{position:fixed;bottom:0;left:0;right:0;height:env(safe-area-inset-bottom, 0px);z-index:2900;pointer-events:none;background:linear-gradient(0deg,var(--edge-tint,var(--paper)),rgba(248,251,255,.4));}
 #statusScrim{position:fixed;top:0;left:0;right:0;height:env(safe-area-inset-top,0px);z-index:2900;pointer-events:none;background:linear-gradient(180deg,var(--edge-tint,var(--paper)),rgba(248,251,255,.55));}
 /* --- Skeleton shimmer for loading feed/cards --- */
 .skel{position:relative;overflow:hidden;background:var(--ink-050);border-radius:var(--r-sm)}
 .skel::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(252,250,246,.55),transparent);transform:translateX(-100%);animation:shimmer 1.3s infinite}
 @keyframes shimmer{100%{transform:translateX(100%)}}
 *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
 html{background:#EDE8DE}
 html,body{margin:0;padding:0;height:100%;height:100dvh;width:100%;overflow:hidden;font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--fg);overscroll-behavior:none;background:#EDE8DE;position:fixed;inset:0;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 #map{position:absolute;top:0;left:0;right:0;bottom:var(--btm-h,0px);background:#F7F4EE;--resort-op:0;--resort-lbl:0}
 /* Resort markers: a dot that fades in with zoom, and a label that follows a
    little later so the country view stays uncluttered. */
 .resort-pin{position:absolute;transform:translate(-50%,-50%);display:flex;align-items:center;gap:4px;white-space:nowrap;opacity:var(--resort-op);transition:opacity .25s linear}
 .resort-pin i{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 2px var(--paper);flex-shrink:0}
 #map.no-resort-labels .resort-pin span{display:none}
 .resort-pin span{font-size:10.5px;font-weight:800;color:var(--ink-700);letter-spacing:-.01em;opacity:var(--resort-lbl);text-shadow:0 0 3px #fff,0 0 6px #fff,0 1px 0 #fff}
 .leaflet-control-scale{margin-right:92px!important;margin-bottom:10px!important}
 #flow{position:absolute;top:0;left:0;right:0;bottom:var(--btm-h,0px);z-index:450;pointer-events:none}
 #modeGlow{display:none}
 .rail-btn svg,#legendBtn svg{color:currentColor}
 /* ============ Alpin Grid component recipes (docs/design-system.md §6) ====
    Shared classes so every screen is built from the same parts. */
 /* --- swisstopo-style sheet parts (see the reference screenshots) ---------
    An uppercase accent section label, a scrolling row of outlined pill tabs,
    and picture cards for choices. */
 .as-seclabel{font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;
   color:var(--accent);text-align:center;margin:var(--sp5) 0 var(--sp3)}
 .as-tabs{display:flex;gap:var(--sp2);overflow-x:auto;scrollbar-width:none;padding:2px var(--sp4);
   -webkit-overflow-scrolling:touch}
 .as-tabs::-webkit-scrollbar{display:none}
 .as-tab{flex:0 0 auto;min-height:var(--tap);padding:0 20px;border-radius:var(--r-full);
   border:2px solid transparent;background:transparent;color:var(--ink-500);
   font-family:inherit;font-size:16px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center}
 .as-tab.on{border-color:var(--accent);color:var(--accent)}
 .as-tab:active{transform:scale(.97)}
 .as-cards{display:flex;gap:var(--sp3);overflow-x:auto;scrollbar-width:none;padding:2px var(--sp4) var(--sp2)}
 .as-cards::-webkit-scrollbar{display:none}
 .as-card{flex:0 0 auto;width:112px;background:none;border:none;padding:0;cursor:pointer;font-family:inherit}
 .as-card .as-card-img{width:112px;height:74px;border-radius:var(--r-1);overflow:hidden;
   border:2px solid var(--ink-100);background:var(--ink-050);display:block}
 .as-card.on .as-card-img{border-color:var(--accent)}
 .as-card .as-card-lbl{display:block;margin-top:6px;font-size:13px;font-weight:700;
   color:var(--ink-500);line-height:1.25;text-align:center}
 .as-card.on .as-card-lbl{color:var(--accent)}
 /* list row with a round check and an info affordance */
 .as-row{display:flex;align-items:center;gap:var(--sp3);min-height:56px;padding:0 var(--sp4);
   border-bottom:1px solid var(--ink-100);background:none;width:100%;font-family:inherit;
   font-size:16px;font-weight:600;color:var(--ink-900);cursor:pointer;text-align:left}
 .as-row .as-check{width:26px;height:26px;border-radius:50%;flex-shrink:0;border:2px solid var(--ink-150);
   display:flex;align-items:center;justify-content:center;color:transparent}
 .as-row.on .as-check{background:var(--accent);border-color:var(--accent);color:var(--paper)}
 .as-row .as-check svg{width:15px;height:15px}
 .as-row .as-info{margin-left:auto;width:26px;height:26px;border-radius:50%;border:1.5px solid var(--accent);
   color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;flex-shrink:0}
 .as-surface{background:var(--paper);border:1px solid var(--ink-100);border-radius:var(--r-2)}
 .as-sheet{background:var(--paper);border-radius:var(--r-2) var(--r-2) 0 0;box-shadow:var(--lift)}
 .as-grab{width:36px;height:4px;border-radius:2px;background:var(--ink-150);margin:10px auto}
 .as-btn{min-height:var(--tap);border-radius:var(--r-1);font-family:inherit;font-size:15px;font-weight:700;
   cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:var(--sp2);
   transition:background var(--dur-1) var(--ease),color var(--dur-1) var(--ease),border-color var(--dur-1) var(--ease)}
 .as-btn:active{transform:scale(.97)}
 .as-btn--primary{background:var(--ink-900);color:var(--paper);border:1px solid var(--ink-900)}
 .as-btn--secondary{background:var(--paper);color:var(--ink-900);border:1px solid var(--ink-100)}
 .as-btn--quiet{background:transparent;color:var(--ink-700);border:none}
 .as-btn--danger{background:transparent;color:var(--danger);border:1px solid var(--ink-100)}
 .as-input{background:var(--ink-050);border:1px solid var(--ink-100);border-radius:var(--r-1);
   min-height:var(--tap);padding:0 var(--sp3);font-family:inherit;font-size:15px;color:var(--ink-900)}
 .as-input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
 /* every focusable control gets the same visible focus ring (§9) */
 button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible,
 [tabindex]:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
 /* ===================== The screen shell ==============================
    Three peer screens on a wrap-around carousel: Search -> Report -> Feed ->
    Search. Each pane is its own fixed, viewport-sized layer with its own
    transform; there is no shared wrapper, which is what lets the feed page
    act as a pane where it already stands in the markup.

    A transformed ancestor is a containing block for fixed descendants, so
    everything inside a pane keeps resolving against the viewport. The rule
    that follows from that: modals live OUTSIDE every pane, or they would be
    trapped in one and travel when you swipe.
    --------------------------------------------------------------------- */
 .pane{position:fixed;inset:0;z-index:1;overflow:hidden;
   transition:transform .34s cubic-bezier(.32,.72,.42,1);will-change:transform;
   backface-visibility:hidden}
 body.pane-dragging .pane{transition:none}
 .pane[data-at="-1"],.pane[data-at="1"]{pointer-events:none}
 .pane[data-at="0"]{pointer-events:auto}
 #paneReport{background:var(--ink-050);display:flex;flex-direction:column}
 #feedPage.pane{z-index:1;transform:none}
 .pane-top{display:flex;align-items:center;gap:var(--sp3);flex-shrink:0;
   padding:calc(env(safe-area-inset-top,0px) + 12px) var(--sp4) 12px}
 .pt-home{display:inline-flex;align-items:center;gap:7px;min-height:var(--tap-sm);
   padding:0 10px 0 6px;border:none;background:none;border-radius:var(--r-1);
   font-family:inherit;font-size:14.5px;font-weight:800;color:var(--ink-900);cursor:pointer}
 .pt-home svg{width:19px;height:19px;flex-shrink:0}
 .pt-home:active{transform:scale(.96)}
 .pt-acc{margin-left:auto;width:var(--tap);height:var(--tap);border-radius:50%;
   border:1px solid var(--bd);background:var(--glass2);
   backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
   color:var(--ink-700);display:flex;align-items:center;justify-content:center;cursor:pointer}
 .pt-acc svg{width:21px;height:21px}
 /* Edge tabs: slim slivers naming the screens either side. */
 .edge-tab{position:fixed;z-index:40;top:50%;transform:translateY(-50%);
   display:flex;align-items:center;justify-content:center;min-height:74px;width:22px;padding:0;
   border:1px solid var(--bd);background:var(--glass);
   backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
   color:var(--ink-500);font-family:inherit;font-size:10px;font-weight:800;
   letter-spacing:.08em;text-transform:uppercase;cursor:pointer;box-shadow:var(--elev1)}
 .edge-tab span{writing-mode:vertical-rl}
 .edge-tab.left{left:0;border-left:none;border-radius:0 var(--r) var(--r) 0}
 .edge-tab.right{right:0;border-right:none;border-radius:var(--r) 0 0 var(--r)}
 .edge-tab.left span{transform:rotate(180deg)}
 .edge-tab:active{background:var(--glass2)}
 body.draw-on .edge-tab,body.scr-home .edge-tab,
 body.lb-open .edge-tab,body.insp-open .edge-tab{display:none}
 /* ===================== Screen 1: the launcher ======================== */
 #scrInitial{position:fixed;inset:0;z-index:200;display:flex;flex-direction:column;
   justify-content:center;gap:var(--sp6);padding:var(--sp6) 26px;
   background:var(--paper);
   transition:opacity .3s var(--ease),transform .3s var(--ease)}
 body:not(.scr-home) #scrInitial{opacity:0;transform:scale(1.03);pointer-events:none}
 /* Contour lines, the way a map draws a mountain. Two stacked repeating
    gradients read as a ridge without shipping an image. */
 .si-ridge{position:absolute;inset:0;z-index:-1;opacity:.5;
   background:
     radial-gradient(120% 80% at 50% 118%,rgba(29,106,106,.16),transparent 62%),
     repeating-linear-gradient(172deg,transparent 0 26px,rgba(33,30,25,.05) 26px 27px),
     repeating-linear-gradient(8deg,transparent 0 34px,rgba(140,79,39,.045) 34px 35px)}
 .si-mark{width:38px;height:38px;color:var(--accent-meteo);display:block}
 .si-head h1{margin:var(--sp3) 0 0;font-size:38px;font-weight:800;letter-spacing:-.035em;color:var(--ink-900)}
 .si-head p{margin:6px 0 0;font-size:15px;color:var(--ink-500);max-width:22ch;line-height:1.45}
 .si-grid{display:flex;flex-direction:column;gap:var(--sp3)}
 .si-card{display:grid;grid-template-columns:auto 1fr;grid-template-rows:auto auto;
   gap:2px var(--sp3);align-items:center;text-align:left;
   min-height:82px;padding:var(--sp4);border-radius:var(--r-lg);cursor:pointer;
   background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
   border:1px solid var(--bd);box-shadow:var(--elev2);font-family:inherit;
   transition:transform var(--dur-1) var(--ease)}
 .si-card:active{transform:scale(.985)}
 .si-ic{grid-row:1/3;width:46px;height:46px;border-radius:var(--r-1);
   display:flex;align-items:center;justify-content:center;
   background:var(--accent-soft);color:var(--accent-meteo)}
 .si-ic svg{width:23px;height:23px}
 .si-card b{font-size:19px;font-weight:800;color:var(--ink-900);letter-spacing:-.02em}
 .si-card>span:last-child{font-size:13.5px;color:var(--ink-500)}
 /* ===================== Pane: Report ================================== */
 .rp-body{flex:1;display:flex;flex-direction:column;justify-content:center;
   gap:var(--sp5);padding:var(--sp4) 26px calc(env(safe-area-inset-bottom,0px) + var(--sp6))}
 .rp-h{margin:0;font-size:24px;font-weight:800;letter-spacing:-.025em;color:var(--ink-900)}
 .rp-choices{display:flex;flex-direction:column;gap:var(--sp3)}
 .rp-choice{display:grid;grid-template-columns:auto 1fr;grid-template-rows:auto auto;
   gap:4px var(--sp3);align-items:start;text-align:left;padding:var(--sp4);
   border-radius:var(--r-lg);cursor:pointer;font-family:inherit;
   background:var(--paper);border:1px solid var(--bd);box-shadow:var(--elev2);
   transition:transform var(--dur-1) var(--ease)}
 .rp-choice:active{transform:scale(.985)}
 .rp-ic{grid-row:1/3;width:52px;height:52px;border-radius:var(--r-1);
   display:flex;align-items:center;justify-content:center;
   background:var(--accent-soft);color:var(--accent)}
 .rp-ic svg{width:25px;height:25px}
 .rp-choice b{font-size:17.5px;font-weight:800;color:var(--ink-900);letter-spacing:-.015em}
 .rp-choice>span:last-child{font-size:13.5px;color:var(--ink-500);line-height:1.45}
 /* ===================== Layers: the bar on the map ====================
    A short scrollable row of the layers people actually switch between, and
    one button for the rest. The model a layer belongs to (A/B) is no longer
    the first thing you read -- it is a small mark inside the full list.
    --------------------------------------------------------------------- */
 #lbBar{position:absolute;z-index:1100;top:calc(env(safe-area-inset-top,0px) + 12px);
   left:10px;right:10px;display:flex;align-items:center;gap:7px}
 body.draw-on #lbBar{display:none}
 #lbQuick{flex:1;min-width:0;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;
   -webkit-overflow-scrolling:touch;padding:1px;
   -webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 22px),transparent);
   mask-image:linear-gradient(90deg,#000 calc(100% - 22px),transparent)}
 #lbQuick::-webkit-scrollbar{display:none}
 .lb-chip{flex:0 0 auto;min-height:38px;padding:0 13px;border-radius:var(--r);
   border:1px solid var(--bd);background:var(--glass);
   backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
   color:var(--ink-700);font-family:inherit;font-size:13.5px;font-weight:700;
   white-space:nowrap;cursor:pointer;box-shadow:var(--elev1);
   transition:background var(--dur-1) var(--ease),color var(--dur-1) var(--ease)}
 .lb-chip.on{background:var(--accent);border-color:var(--accent);color:var(--paper)}
 .lb-chip:active{transform:scale(.97)}
 #lbMore{flex:0 0 auto;min-height:38px;padding:0 10px 0 13px;border-radius:var(--r);
   border:1px solid var(--bd);background:var(--glass2);
   backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
   color:var(--ink-900);font-family:inherit;font-size:13.5px;font-weight:800;
   display:inline-flex;align-items:center;gap:2px;cursor:pointer;box-shadow:var(--elev1)}
 #lbMore svg{width:16px;height:16px}
 #lbMore:active{transform:scale(.97)}
 /* the full list */
 #lbScrim{position:fixed;inset:0;z-index:3300;background:rgba(33,30,25,.32);
   opacity:0;pointer-events:none;transition:opacity var(--dur-2) var(--ease)}
 body.lb-open #lbScrim{opacity:1;pointer-events:auto}
 #lbSheet{position:fixed;z-index:3400;left:0;right:0;bottom:0;max-height:78vh;
   display:flex;flex-direction:column;background:var(--paper);
   border-radius:var(--r-lg) var(--r-lg) 0 0;box-shadow:var(--elev3);
   transform:translateY(101%);transition:transform var(--dur-2) var(--ease);
   padding-bottom:calc(env(safe-area-inset-bottom,0px) + 8px);will-change:transform}
 body.lb-open #lbSheet{transform:translateY(0)}
 #lbGrab{width:36px;height:4px;border-radius:2px;background:var(--ink-150);
   margin:10px auto 2px;flex-shrink:0}
 #lbSheet header{display:flex;align-items:center;gap:var(--sp2);
   padding:var(--sp2) var(--sp4);flex-shrink:0}
 #lbSheet header h2{flex:1;margin:0;font-size:17px;font-weight:800;color:var(--ink-900);letter-spacing:-.01em}
 #lbSheet header button{width:var(--tap);height:var(--tap);border:none;background:none;
   border-radius:var(--r-1);color:var(--ink-700);display:flex;align-items:center;justify-content:center;cursor:pointer}
 #lbSheet header button svg{width:21px;height:21px}
 #lbList{overflow-y:auto;-webkit-overflow-scrolling:touch;padding:0 var(--sp4) var(--sp4)}
 .lb-row{display:flex;align-items:center;gap:var(--sp3);width:100%;min-height:58px;
   padding:0 4px;border:none;border-bottom:1px solid var(--bd);background:none;
   font-family:inherit;text-align:left;cursor:pointer;color:var(--ink-900)}
 .lb-row:last-child{border-bottom:none}
 .lb-row:active{background:var(--ink-050)}
 /* the model mark: small, quiet, and the only place A/B still appears */
 .lb-mark{width:22px;height:22px;flex-shrink:0;border-radius:6px;
   display:flex;align-items:center;justify-content:center;
   font-size:10.5px;font-weight:800;letter-spacing:0}
 .lb-meteo{background:var(--accent-meteo-soft);color:var(--accent-meteo)}
 .lb-report{background:var(--accent-report-soft);color:var(--accent-report)}
 .lb-t{flex:1;min-width:0}
 .lb-t b{display:block;font-size:15.5px;font-weight:700;letter-spacing:-.01em}
 .lb-t span{display:block;font-size:12.5px;color:var(--ink-500);margin-top:1px}
 .lb-tick{color:var(--accent);flex-shrink:0}
 .lb-tick svg{width:18px;height:18px}
 .lb-row.on .lb-t b{color:var(--accent)}
 /* the search field drops below the layer bar */
 /* --- The console: time controls; the layers moved to the map --- */
 #layerBar{--topic-accent:var(--accent);--topic-tint:var(--accent-soft)}
 #layerBar:empty,#variants:empty{display:none}
 /* keep a hairline only when the variant/prognosis rows actually show */
 #layerBar:has(#variants:not(:empty)),#layerBar:has(#progBar.on){
   border-bottom:1px solid var(--bd);padding:var(--sp3) var(--sp4)}
 body.draw-on #layerBar{display:none}
 /* Variants of the active layer: a quieter second rank (P5, size + weight) */
 #variants{display:flex;flex-wrap:wrap;gap:4px}
 #variants:empty{display:none}
 #variants button{min-height:var(--tap-sm);padding:0 12px;border-radius:var(--r-1);border:none;
   background:transparent;color:var(--ink-500);font-family:inherit;font-size:12.5px;font-weight:700;
   cursor:pointer;display:inline-flex;align-items:center;white-space:nowrap}
 #variants button.on{background:var(--accent-soft);color:var(--accent)}
 #variants button:active{transform:scale(.97)}
 #progBar{display:none;flex-direction:column;gap:var(--sp2);padding-top:var(--sp2);border-top:1px solid var(--ink-100)}
 #progBar.on{display:flex}
 .pb-row{display:flex;align-items:center;gap:var(--sp2)}
 .pb-lbl{font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-500);flex-shrink:0;min-width:56px}
 .pb-seg{display:flex;gap:4px;flex-wrap:wrap}
 .pb-seg button{border:1px solid var(--ink-100);background:var(--paper);border-radius:var(--r-1);padding:0 11px;
   min-height:var(--tap-sm);cursor:pointer;font-size:12px;font-weight:700;color:var(--ink-700);font-family:inherit;
   white-space:nowrap;display:inline-flex;align-items:center}
 .pb-seg button.active{background:var(--accent);border-color:var(--accent);color:var(--paper)}
 #progBar input[type=range]{flex:1;accent-color:var(--accent);min-width:0;height:30px}
 #progConfVal{font-size:12.5px;font-weight:800;color:var(--ink-900);min-width:38px;text-align:right}
 #bottomPanel{position:absolute;z-index:1000;bottom:0;left:0;right:0;
   background:var(--paper);border-top:1px solid var(--ink-100);box-shadow:none;transition:none;padding-bottom:max(env(safe-area-inset-bottom, 0px) - 18px, 4px);overflow:hidden}
 #btmMain{padding:6px 14px 2px}
 #timeline{display:block;border:1px solid var(--ink-100);background:var(--ink-050);border-radius:var(--r-1)}
 #presets::-webkit-scrollbar{display:none}
 #presets.can-scroll{-webkit-mask-image:linear-gradient(90deg,#000 86%,transparent);mask-image:linear-gradient(90deg,#000 86%,transparent)}
 .winlbl{font-size:13px;font-weight:700;color:var(--fg);letter-spacing:-.01em}
 #tlHead{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:10px}
 #tlModeToggle{display:none!important}
 #tlModeToggleOFF{display:inline-flex;background:rgba(0,0,0,.05);border-radius:999px;padding:3px;flex-shrink:0}
 #tlModeToggle button{border:none;background:none;padding:5px 13px;border-radius:999px;font-size:12px;font-weight:600;color:var(--mut);cursor:pointer;font-family:inherit;transition:.15s}
 #tlModeToggle button.active{background:var(--card);color:var(--fg);box-shadow:0 1px 3px rgba(0,0,0,.12)}
 #tlDetail{display:none}
 #bottomPanel.detail #tlDetail{display:block}
 #bottomPanel.collapsed #tlDetail{display:none}
 #bottomPanel.collapsed #tlModeToggle{opacity:.5;pointer-events:none}
 #tlExtended{margin-top:8px}
 #tlLen{width:100%;accent-color:var(--acc);margin:8px 0 2px;cursor:pointer}
 .tl-steprow{display:none!important}
 .tl-steprowOFF{display:flex;gap:6px}
 .tl-step{min-height:var(--tap-sm);border:1px solid var(--bd);background:rgba(252,250,246,.7);border-radius:10px;padding:8px 10px;font-size:12px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;transition:.15s;white-space:nowrap}
 .tl-step:hover{background:var(--card);color:var(--fg)}
 #tlPrev,#tlNext,#tlNow{flex:1}
 .tl-len{flex-shrink:0}
 .tl-len b{color:var(--acc2)}
 .tl-range{margin-top:8px;font-size:11px;color:var(--mut);text-align:center;font-weight:500}
 .seg{display:flex;flex-wrap:wrap;gap:5px}
 .seg button{border:1px solid var(--ink-100);background:var(--paper);border-radius:var(--r-1);padding:0 14px;cursor:pointer;font-size:14px;font-weight:600;min-height:var(--tap);color:var(--fg2);transition:all .2s cubic-bezier(.4,0,.2,1);flex-shrink:0}
 .seg button:hover{background:rgba(252,250,246,.95)}
 .seg button.active{background:var(--ink-900);color:var(--paper);border-color:var(--ink-900);font-weight:700;box-shadow:none}
 #tlToggle{position:absolute;top:5px;left:50%;transform:translateX(-50%);width:32px;height:4px;border-radius:2px;background:rgba(0,0,0,.12);cursor:ns-resize;z-index:1;touch-action:none}
 .sec{margin-top:12px}
 .cap{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);margin-bottom:6px}
 .ck{display:flex;align-items:center;gap:9px;margin-top:12px;font-size:13px;cursor:pointer;color:var(--fg2)}
 .ck input{width:18px;height:18px;accent-color:var(--acc)}
 #three-wrap{position:absolute;inset:0;z-index:2000;display:none;background:#e8eef4}
 #three-wrap .maplibregl-canvas{outline:none}
 #three-wrap .maplibregl-map{width:100%;height:100%}
 #btn3dClose{position:absolute;top:calc(14px + env(safe-area-inset-top,0px));right:14px;z-index:2001;padding:10px 18px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);color:var(--fg);cursor:pointer;font-size:16px;font-weight:600;}
 #three-wrap .ctrl3d{position:absolute;bottom:calc(30px + env(safe-area-inset-bottom,0px));left:50%;transform:translateX(-50%);z-index:2001;display:flex;gap:10px;flex-wrap:wrap;justify-content:center;max-width:calc(100vw - 24px)}
 #three-wrap .ctrl3d button,#three-wrap .ctrl3d label,#three-wrap .ctrl3d select{padding:10px 16px;border-radius:14px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);cursor:pointer;font-size:16px;min-height:46px;touch-action:manipulation}
 #three-wrap .ctrl3d button:hover{border-color:var(--acc);color:var(--fg)}
 @media (max-width:560px){#three-wrap .ctrl3d{bottom:calc(16px + env(safe-area-inset-bottom,0px));gap:5px}#three-wrap .ctrl3d button,#three-wrap .ctrl3d label,#three-wrap .ctrl3d select{padding:10px 14px;font-size:15px;min-height:46px;border-radius:12px}#btn3dClose{top:calc(8px + env(safe-area-inset-top,0px));right:8px;padding:10px 18px;font-size:16px;border-radius:14px}}
 .sub{font-size:12px;color:var(--mut)}
 .asp-crisp img{image-rendering:pixelated;image-rendering:crisp-edges}
 .legend{position:absolute;z-index:950;bottom:calc(var(--btm-h,80px) + 40px);left:12px;background:var(--paper);border:1px solid rgba(252,250,246,.5);padding:10px 12px;border-radius:var(--r);box-shadow:0 2px 12px var(--ink-100);font-size:12px;max-width:220px;line-height:1.5;color:var(--fg2);display:none}
 .legend.show{display:block}
 #legendBtn{position:absolute;z-index:960;bottom:var(--btm-h,80px);left:12px;width:40px;height:40px;border-radius:var(--r-1);border:1px solid var(--ink-100);background:var(--paper);color:var(--ink-700);cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:var(--lift);transition:background var(--dur-1) var(--ease)}
 #legendBtn svg{width:20px;height:20px}
 #legendBtn:active{transform:scale(.92)}
 #legendBtn:hover,#legendBtn.active{color:var(--acc);background:var(--paper)}
 .legend i{display:inline-block;width:12px;height:12px;margin-right:5px;vertical-align:-2px;border-radius:2px}
 .stn{background:var(--paper);border:1.5px solid var(--acc);border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700;color:var(--acc2);text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.12);white-space:nowrap}
 .stn-dot{width:12px;height:12px;border-radius:50%;background:var(--acc);border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.25);cursor:pointer;transition:transform .12s}
 .stn-dot:hover{transform:scale(1.35)}
 .rpt-dot{width:14px;height:14px;border-radius:50%;border:2.5px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.3);cursor:pointer;transition:transform .12s}
 .rpt-dot:hover{transform:scale(1.35)}
 .scl{position:relative;width:110px;height:56px;font-size:10px;font-weight:700;text-align:center;pointer-events:none}
 .scl>div{position:absolute;left:0;right:0;white-space:nowrap}
 .scl .s-pill{display:inline-block;background:rgba(252,250,246,.8);border-radius:6px;padding:1px 5px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
 .scl .s-t{top:-2px;color:var(--ink-700)}.scl .s-b{bottom:-2px;color:#d04040}
 .scl .s-row{top:17px;display:flex;align-items:center;justify-content:center;gap:4px}
 .scl .s-l{color:#4a6a8a}.scl .s-r{color:#2a8a4a}
 .scl .s-c{pointer-events:auto;background:var(--paper);border:2px solid var(--acc);border-radius:10px;color:var(--acc2);padding:1px 7px;font-size:14px;font-weight:800;box-shadow:0 1px 6px rgba(0,0,0,.12),0 0 0 3px var(--glow);cursor:pointer;letter-spacing:.02em}
 .scard{font:15px system-ui;line-height:1.6;min-width:180px;color:var(--fg)}
 .scard b{font-size:16px}
 .scard .g{display:grid;grid-template-columns:auto auto;gap:3px 16px;margin-top:6px}
 .scard .k{color:var(--mut)}
 .wn{font-size:10px;color:#2a4a6a;font-weight:700;display:inline-block;background:rgba(0,90,160,.08);border-radius:5px;padding:0 4px;box-shadow:0 1px 2px var(--ink-100)}
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
 body.insp-open #mapQr,body.insp-open #mapFab,body.insp-open #mapDraw{opacity:0;pointer-events:none;transition:opacity .2s}
 /* Docked, not floating: the panel sits flush against an edge and against the
    console, so it reads as part of the screen instead of a card on top of it.
    It does not move, and there is nothing to drag. */
 .insp-panel{position:absolute;z-index:2600;background:var(--paper);display:none;flex-direction:column;overflow:hidden;border:0 solid var(--ink-100)}
 .insp-chip{background:var(--ink-050)}
 .insp-tile{background:var(--ink-050);border-color:var(--ink-100)}
 .insp-head button{background:var(--ink-050)}
 .insp-panel.open{display:flex;animation:inspIn .3s cubic-bezier(.32,.72,.42,1)}
 /* Desktop: a column against the right edge, from the top down to the console. */
 @media(min-width:561px){.insp-panel{top:0;right:0;left:auto;bottom:var(--btm-h,0px);width:340px;
   border-left-width:1px;border-radius:0}
   @keyframes inspIn{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:none}}}
 /* Phone: a band across the bottom edge, resting directly on the console. */
 @media(max-width:560px){.insp-panel{left:0;right:0;bottom:var(--btm-h,0px);width:auto;
   max-height:min(52vh,calc(100vh - var(--btm-h,0px) - 190px));
   border-top-width:1px;border-radius:var(--r-2) var(--r-2) 0 0}
   @keyframes inspIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}}
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
 .insp-xmark::before{content:'';position:absolute;inset:0;border-radius:50%;border:2.5px solid #A83A2E;box-shadow:0 0 0 3px rgba(168,58,46,.18);animation:xpulse 1.8s ease-out infinite}
 @keyframes xpulse{0%{transform:scale(.7);opacity:1}70%{transform:scale(1.35);opacity:0}100%{transform:scale(1.35);opacity:0}}
 .leaflet-popup-content-wrapper{background:var(--paper)!important;border:1px solid var(--ink-100)!important;color:var(--fg)!important;box-shadow:0 4px 24px rgba(0,0,0,.1),0 1px 0 rgba(252,250,246,.5) inset!important;border-radius:var(--r-lg)!important}
 .leaflet-popup-content{margin:12px 14px!important;font-size:14px!important}
 .leaflet-popup-tip{background:var(--paper)!important}
 .leaflet-popup-close-button{color:var(--mut)!important;font-size:18px!important;width:28px!important;height:28px!important;line-height:28px!important}
 .leaflet-popup-close-button:hover{color:var(--fg)!important}
 /* Attribution: tiny, faint, unobtrusive (expand on hover) */
 .leaflet-control-attribution{background:rgba(247,244,238,.45)!important;color:rgba(115,108,97,.6)!important;font-size:9px!important;padding:1px 6px!important;border-radius:6px 0 0 0!important;box-shadow:none!important;max-width:22px;overflow:hidden;white-space:nowrap;transition:max-width .3s,background .3s}
 .leaflet-control-attribution:hover{max-width:80vw;background:var(--paper)!important;color:var(--mut)!important}
 .leaflet-control-attribution a{color:inherit!important}
 .leaflet-control-scale-line{background:rgba(247,244,238,.72)!important;border-color:rgba(33,30,25,.32)!important;color:var(--fg2)!important;font-family:'Inter',system-ui!important;font-size:10px!important;font-weight:600!important;}
 /* the right-hand corner belongs to the FABs, so the scale bar is pinned to
    the opposite one instead of stacking under them */
 .leaflet-control-scale{position:fixed!important;left:12px!important;right:auto!important;
   bottom:calc(var(--btm-h,0px) + 62px)!important;margin:0!important;z-index:800}
 /* the attribution shares the quiet left edge instead of the FAB corner */
 .leaflet-control-attribution{position:fixed!important;left:12px!important;right:auto!important;
   bottom:calc(var(--btm-h,0px) + 44px)!important;margin:0!important;z-index:800;
   border-radius:5px!important}
 body.draw-on .leaflet-control-attribution{display:none!important}
 body.draw-on .leaflet-control-scale{display:none!important}
 #demoPill{min-height:var(--tap-sm);position:absolute;z-index:1050;top:calc(env(safe-area-inset-top,0px) + 172px);left:12px;display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:999px;border:1px solid var(--hair);background:var(--paper);color:var(--fg2);font-size:12px;font-weight:800;font-family:inherit;cursor:pointer;box-shadow:var(--elev1)}
 #demoPill .dp-dot{width:7px;height:7px;border-radius:50%;background:rgba(20,20,25,.35)}
 #demoPill.on{background:var(--ink-900);color:#fff;border-color:var(--ink-900)}
 #demoPill.on .dp-dot{background:#5ee68a}
 /* The empty strip above the search bar is where the brand mark lives. */
 #brandMark{position:absolute;z-index:1100;top:calc(env(safe-area-inset-top,0px) + 76px);left:10px;
   background:var(--glass2);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
   border:1px solid var(--bd);box-shadow:var(--elev1)}
 #searchWrap{position:absolute;z-index:1100;top:calc(env(safe-area-inset-top,0px) + 62px);left:12px;width:230px;max-width:calc(100vw - 24px)}
 #searchWrap input{width:100%;min-height:var(--tap);padding:0 12px 0 34px;border-radius:var(--r-1);border:1px solid var(--bd);background:var(--glass);color:var(--ink-900);font-size:15px;font-weight:500;outline:none;box-shadow:var(--lift);font-family:inherit}
 #searchWrap input::placeholder{color:var(--mut);font-weight:400}
 #searchWrap input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
 #searchWrap .icn{position:absolute;left:12px;top:50%;transform:translateY(-50%);pointer-events:none;color:var(--mut);font-size:15px}
 /* Right-side control rail */
 #ctrlRail{position:absolute;z-index:1050;right:12px;top:calc(env(safe-area-inset-top,0px) + 12px);display:flex;flex-direction:column;gap:10px}
 .rail-btn{position:relative;width:46px;height:46px;border-radius:var(--r-1);border:1px solid var(--bd);background:var(--glass);color:var(--ink-700);display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:none;transition:background var(--dur-1) var(--ease),color var(--dur-1) var(--ease),border-color var(--dur-1) var(--ease)}
 .rail-btn:hover{background:var(--glass2);color:var(--fg);transform:translateY(-1px)}
 .rail-btn:active{transform:scale(.95)}
 .rail-btn.active{background:var(--ink-900);color:var(--paper);border-color:var(--ink-900)}
 #btn3dFloat{background:var(--glass);color:var(--ink-700);border-color:var(--bd)}
 .rail-btn.feed-accent{background:var(--accent-report-soft);color:var(--accent-report);border-color:rgba(140,79,39,.26)}
 #accountBtn.signed{color:var(--accent-report)}
 #railToggles.active{color:var(--accent-meteo);background:var(--accent-meteo-soft);border-color:rgba(29,106,106,.26)}
 .rail-note{position:absolute;right:56px;padding:6px 11px;border-radius:10px;background:var(--fg);color:#fff;font-size:12px;font-weight:700;white-space:nowrap;opacity:0;transform:translateX(6px);transition:.25s var(--ease);pointer-events:none}
 .rail-note.show{opacity:1;transform:translateX(0)}
 .me-dot{position:relative;width:18px;height:18px;border-radius:50%;background:var(--fg);border:3px solid var(--paper);box-shadow:0 1px 6px rgba(0,0,0,.35)}
 .me-dot::after{content:'';position:absolute;inset:-7px;border-radius:50%;border:2px solid rgba(33,30,25,.45);animation:xpulse 2s ease-out infinite}
 .acct-img{display:block;width:34px;height:34px;border-radius:28%;background-size:cover;background-position:center;box-shadow:0 0 0 2px var(--paper)}
 #userBar{display:none!important} /* replaced by the rail account button */
 .rail-btn.feed-accent:hover{color:var(--acc)}
 .feed-dot{position:absolute;top:8px;right:8px;width:8px;height:8px;border-radius:50%;background:var(--danger);border:1.5px solid var(--paper);box-shadow:0 1px 3px rgba(0,0,0,.2);display:none}
 .feed-dot.on{display:block}
 #btn3dFloat:hover{background:var(--glass2);color:var(--fg)}
 #btn3dFloat.active{background:var(--acc);color:var(--paper);border-color:var(--acc)}
 .rail-btn svg{width:21px;height:21px}
 #togglePop{position:absolute;right:56px;top:0;background:var(--paper);border:1px solid var(--bd);border-radius:14px;padding:10px 12px;box-shadow:0 6px 26px rgba(0,0,0,.14);display:none;flex-direction:column;gap:10px;white-space:nowrap}
 #togglePop.show{display:flex}
 #togglePop label{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer}
 #togglePop input{width:16px;height:16px;accent-color:var(--acc)}
 #searchRes{position:absolute;top:100%;left:0;right:0;margin-top:4px;background:var(--glass2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;display:none;max-height:260px;overflow-y:auto;box-shadow:0 4px 16px rgba(0,0,0,.1)}
 #searchRes .sr{padding:10px 14px;cursor:pointer;font-size:15px;color:var(--fg2);border-bottom:1px solid rgba(0,0,0,.05);transition:.12s}
 #searchRes .sr:last-child{border-bottom:none}
 #searchRes .sr:hover,#searchRes .sr.sel{background:rgba(0,112,184,.08);color:var(--fg)}
 #searchRes .sr .sub{font-size:11px;color:var(--mut);margin-top:2px}
 /* The only thing the load shows: a 2 px rule along the top edge, filled by
    the actual number of bytes received. It removes itself when done. */
 #boot{position:fixed;top:0;left:0;right:0;height:2px;z-index:9999;background:transparent;pointer-events:none}
 #boot i{display:block;height:100%;width:0;background:var(--ink-900);transition:width .18s linear}
 #boot.done{opacity:0;transition:opacity .25s ease}
 /* A load that failed needs words, not a stalled bar. */
 #bootErr{position:fixed;left:12px;right:12px;top:calc(env(safe-area-inset-top,0px) + 12px);z-index:9999;
   display:none;background:var(--paper);border:1px solid var(--ink-100);border-radius:var(--r-2);
   box-shadow:var(--lift);padding:14px 16px;font-size:14px;color:var(--ink-900);line-height:1.45}
 #bootErr.show{display:block}
 #bootErr button{margin-top:12px;min-height:44px;padding:0 18px;border:none;border-radius:var(--r-1);
   background:var(--ink-900);color:var(--paper);font:800 14px Inter,system-ui;cursor:pointer}
 /* --- First-run legal / safety disclaimer gate --- */
 #disc{position:fixed;inset:0;z-index:9800;display:none;align-items:flex-end;justify-content:center;background:rgba(6,14,26,.55);}
 #disc.show{display:flex;animation:discBg .3s ease}
 @keyframes discBg{from{opacity:0}to{opacity:1}}
 #disc .sheet{width:100%;max-width:520px;background:var(--glass2);border:1px solid var(--ink-100);border-radius:var(--r-xl) var(--r-xl) 0 0;box-shadow:var(--elev3);padding:24px 22px calc(env(safe-area-inset-bottom,0px) + 22px);animation:discUp .4s var(--ease-spring)}
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
 #a2hs{position:fixed;inset:0;z-index:9700;display:none;align-items:flex-end;justify-content:center;background:rgba(10,9,26,.5);}
 #a2hs.show{display:flex;animation:discBg .3s ease}
 #a2hs .sheet{width:100%;max-width:480px;background:var(--glass2);border:1px solid var(--ink-100);border-radius:var(--r-xl) var(--r-xl) 0 0;box-shadow:var(--elev3);padding:22px 22px calc(env(safe-area-inset-bottom, 0px) + 20px)}
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
 /* --- Onboarding coach marks --- */
 #coach{position:fixed;inset:0;z-index:8000}
 #coachSpot{position:absolute;border-radius:14px;box-shadow:0 0 0 9999px rgba(14,17,22,.32);transition:all .35s cubic-bezier(.4,0,.2,1);pointer-events:none}
 #coachCard{position:absolute;width:min(300px,calc(100vw - 32px));background:var(--glass2);border:1px solid var(--ink-100);border-radius:var(--r-lg);padding:18px 20px;box-shadow:var(--elev3);transition:top .35s var(--ease),left .35s var(--ease);opacity:0;animation:coachIn .3s .1s ease forwards}
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
 .login-btn{min-height:40px;padding:9px 18px;border-radius:999px;border:1.5px solid rgba(22,21,46,.12);background:var(--paper);color:var(--fg);font-size:14px;font-weight:600;cursor:pointer;box-shadow:0 1px 4px var(--ink-100);letter-spacing:-.01em}
 .login-btn:hover{background:var(--card)}
 .user-pill{display:flex;align-items:center;gap:8px;min-height:40px;padding:5px 14px 5px 5px;border-radius:999px;border:1.5px solid var(--ink-100);background:var(--paper);cursor:pointer;box-shadow:0 1px 4px var(--ink-100)}
 .user-avatar{border-radius:28%!important;width:26px;height:26px;border-radius:50%;background:var(--fg);color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;letter-spacing:.02em}
 .user-name{font-size:13px;font-weight:600;color:var(--fg2);max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .auth-overlay{position:fixed;inset:0;z-index:5000;background:rgba(14,17,22,.32);display:flex;align-items:center;justify-content:center;padding:16px}
 .auth-modal{position:relative;background:var(--glass2);border-radius:var(--r-xl);padding:36px 28px 28px;width:100%;max-width:360px;box-shadow:var(--elev3),0 1px 0 rgba(252,250,246,.5) inset}
 .auth-modal h2{margin:0 0 4px;font-size:24px;font-weight:800;color:var(--fg);letter-spacing:-.03em}
 .auth-note{font-size:12px;color:var(--mut);margin:4px 0 10px;line-height:1.5}
 .auth-sub{font-size:13px;color:var(--mut);margin:0 0 24px}
 .auth-modal form{display:flex;flex-direction:column;gap:10px}
 .auth-sent-ic{width:52px;height:52px;margin:2px auto 12px;border-radius:16px;background:var(--fill);display:flex;align-items:center;justify-content:center;color:var(--fg2)}
 .auth-sent-ic svg{width:26px;height:26px}
 .auth-forgot{display:block;width:100%;margin-top:4px;border:none;background:none;color:var(--mut);font-size:13px;font-weight:700;font-family:inherit;cursor:pointer;padding:6px}
 .auth-forgot:hover{color:var(--fg2);text-decoration:underline}
 .auth-modal input[type="text"],.auth-modal input[type="email"],.auth-modal input[type="password"]{width:100%;padding:13px 16px;border:1.5px solid var(--ink-100);border-radius:var(--r);font-size:15px;font-family:inherit;color:var(--fg);background:var(--fill);outline:none;box-sizing:border-box;transition:border-color .15s,box-shadow .15s}
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
 .bio-lock-card .auth-bio-ic{background:rgba(252,250,246,.12);color:#fff;width:76px;height:76px}
 .bio-lock-t{font-size:22px;font-weight:800;letter-spacing:-.02em}
 .bio-lock-s{font-size:14px;color:rgba(252,250,246,.7);margin:4px 0 22px}
 .bio-lock-card .auth-btn.primary{background:var(--card);color:var(--fg)}
 .bio-lock-out{background:none;border:none;color:rgba(252,250,246,.6);font-size:14px;font-weight:600;cursor:pointer;margin-top:14px;font-family:inherit}
 .auth-close{position:absolute;top:16px;right:16px;background:none;border:none;font-size:20px;color:var(--mut);cursor:pointer;width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center}
 .auth-close:hover{background:rgba(0,0,0,.05)}
 .auth-switch{text-align:center;margin-top:20px;font-size:13px;color:var(--mut)}
 .auth-switch button{background:none;border:none;color:var(--acc);font-weight:600;cursor:pointer;font-size:13px}
 .email-banner{position:fixed;top:calc(env(safe-area-inset-top,0px) + 56px);left:12px;right:12px;z-index:1200;display:none;align-items:center;justify-content:space-between;gap:12px;padding:10px 16px;background:rgba(255,240,220,.95);border:1px solid rgba(200,150,50,.2);border-radius:12px;font-size:13px;color:#6a4a10;box-shadow:0 2px 8px var(--ink-100)}
 .email-banner button{background:none;border:none;color:var(--ink-700);font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}
 /* --- Report FAB (primary action) --- */
 /* reportFab entfernt — der + FAB (mapFab) ist der einzige Melde-Einstieg auf der Karte */
 /* --- Radial quick-pick --- */
 .radial-wrap{position:fixed;inset:0;z-index:4000;touch-action:none;display:none}
 .radial-bg{position:absolute;inset:0;background:rgba(14,17,22,.32);}
 .radial-ring{position:absolute;width:280px;height:280px;border-radius:50%}
 .radial-seg{position:absolute;width:72px;height:72px;border-radius:50%;background:rgba(20,30,51,.85);border:2px solid rgba(94,200,255,.15);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:13px;font-weight:600;color:#E8EEF7;transition:transform .15s cubic-bezier(.34,1.56,.64,1),border-color .15s,background .15s;box-shadow:inset 0 1px 0 rgba(252,250,246,.08)}
 .radial-seg .re{font-size:28px}
 .radial-seg.hover{transform:scale(1.18);border-color:#5EC8FF;background:rgba(94,200,255,.15);box-shadow:0 0 24px rgba(94,200,255,.3),inset 0 1px 0 rgba(252,250,246,.1)}
 .radial-center{position:absolute;width:48px;height:48px;border-radius:50%;background:rgba(94,200,255,.2);border:2px solid rgba(94,200,255,.3);display:flex;align-items:center;justify-content:center;color:#5EC8FF;font-size:20px}
 /* --- Report sheet (Alpenglühen dark) --- */
 /* --- Report wizard: centered light modal --- */
 /* --- Draw-based snow report (v3) --- */
 body.draw-on #ctrlRail,body.draw-on #layerBar,body.draw-on #searchWrap,body.draw-on #brandMark,body.draw-on #demoPill,body.draw-on #mapQr,body.draw-on #mapFab,body.draw-on #mapDraw,body.draw-on #bottomPanel,body.draw-on #legendBtn,body.draw-on .legend{display:none!important}
 .fab-v{position:absolute;top:-3px;left:-3px;min-width:16px;height:15px;padding:0 3px;border-radius:7px;background:var(--ink-900);color:#fff;font-size:8.5px;font-weight:900;line-height:15px;text-align:center;border:1.5px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.3)}
 .feed-fab .fab-v{background:var(--card);color:var(--ink-900);border-color:var(--ink-900)}
 /* v1 on top, then v2, then v3 -- the stack closes up when a FAB is hidden
    (Quick Powder is off in this version), so no empty slot is left behind. */
 [hidden]{display:none!important}
 #mapFab{bottom:calc(var(--btm-h,90px) + 178px)!important}
 #mapQr{bottom:calc(var(--btm-h,90px) + 96px)!important}
 #mapQr[hidden]~#mapFab{bottom:calc(var(--btm-h,90px) + 96px)!important}
 #mapDraw{position:fixed;left:auto;right:14px;transform:none;bottom:calc(var(--btm-h,90px) + 14px)!important;z-index:900}
 #mapDraw:active{transform:scale(.9)}
 .feed-draw{background:var(--paper)!important;border-color:var(--accent)!important;color:var(--accent)!important}
 .feed-draw svg{color:var(--accent)}
 #drawWrap{position:fixed;inset:0;z-index:4000;display:none}
 #drawCanvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none;cursor:crosshair}
 body.draw-pan #drawCanvas{pointer-events:none;cursor:grab}
 body.draw-pan #drawWrap{pointer-events:none}
 body.draw-pan #drawClose,body.draw-pan #drawPan{pointer-events:auto}
 #drawClose{position:fixed;top:calc(env(safe-area-inset-top,0px) + 12px);left:12px;z-index:4002;width:40px;height:40px;border-radius:50%;border:none;background:var(--paper);box-shadow:var(--elev1);font-size:17px;color:var(--fg);cursor:pointer}
 #drawPan{position:fixed;top:calc(env(safe-area-inset-top,0px) + 12px);right:12px;z-index:4002;display:inline-flex;align-items:center;gap:7px;height:40px;padding:0 14px;border-radius:999px;border:1px solid var(--hair);background:var(--paper);box-shadow:var(--elev1);font-size:12px;font-weight:800;font-family:inherit;color:var(--fg2);cursor:pointer;white-space:nowrap}
 #drawPan svg{width:16px;height:16px;flex-shrink:0}
 #drawPan.on{background:var(--accent);color:#fff;border-color:var(--accent)}
 #drawHint{position:fixed;top:calc(env(safe-area-inset-top,0px) + 60px);left:50%;transform:translateX(-50%) translateY(-8px);z-index:4002;background:var(--ink-900);color:#fff;font-size:12.5px;font-weight:700;padding:9px 15px;border-radius:999px;opacity:0;transition:.3s;pointer-events:none;box-shadow:var(--elev2);max-width:82vw;text-align:center}
 #drawHint.show{opacity:1;transform:translateX(-50%) translateY(0)}
 #drawBar{position:fixed;left:0;right:0;bottom:0;z-index:4002;padding:14px 12px calc(env(safe-area-inset-bottom,0px) + 12px);background:linear-gradient(180deg,rgba(252,250,246,0),var(--paper) 24%)}
 body.draw-pan #drawBar{opacity:.4;pointer-events:none;filter:saturate(.4)}
 #drawSlider{display:none;align-items:center;gap:11px;background:var(--card);border:1px solid var(--hair);border-radius:14px;padding:10px 15px;margin-bottom:10px;box-shadow:var(--elev1);animation:qrFade .22s var(--ease) both}
 #drawSlider label{font-size:13px;font-weight:800;color:var(--fg2);white-space:nowrap}
 #drawSlider input{flex:1;accent-color:var(--fg);min-width:0}
 #drawSlider b{font-size:13px;font-weight:800;color:var(--fg);min-width:92px;text-align:right}
 /* Vertical brush size on the left, vertical snow depth on the right, both
    reachable with a thumb while the other hand holds the phone. */
 #drawBrushV,#drawDepthV{position:fixed;z-index:4003;display:none;flex-direction:column;align-items:center;gap:7px;
   padding:10px 7px;border-radius:16px;background:var(--paper);
   border:1px solid var(--hair);box-shadow:var(--elev2)}
 body.draw-on #drawBrushV,body.draw-on #drawDepthV{display:flex}
 body.draw-pan #drawBrushV,body.draw-pan #drawDepthV{opacity:.35;pointer-events:none}
 #drawBrushV{left:10px;top:50%;transform:translateY(-50%)}
 #drawDepthV{right:10px;top:50%;transform:translateY(-50%)}
 #drawBrushV.off,#drawDepthV.off{display:none!important}
 #drawBrushV input[type=range],#drawDepthV input[type=range]{-webkit-appearance:slider-vertical;appearance:slider-vertical;
   writing-mode:vertical-lr;direction:rtl;width:26px;height:150px;accent-color:var(--fg);margin:0}
 #drawBrushV .dbv-cap,#drawDepthV .ddv-cap{font-size:9.5px;font-weight:900;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 #drawDepthV .ddv-cap{color:var(--fg2)}
 #drawDepthV .ddv-cap svg{width:15px;height:15px;display:block}
 #drawBrushV b,#drawDepthV b{font-size:13px;font-weight:900;color:var(--fg);line-height:1}
 #drawDepthV .ddv-unit{font-size:9.5px;font-weight:800;color:var(--mut);margin-top:-4px}
 #drawBrushV .dbv-dot{width:34px;height:34px;display:flex;align-items:center;justify-content:center}
 #drawBrushV .dbv-dot i{display:block;border-radius:50%;background:var(--fg);opacity:.22;transition:width .1s,height .1s}
 /* the depth box is tinted with the SLF colour for the chosen depth */
 #drawDepthV{border-width:2px}
#drawBrush{display:none;align-items:center;gap:11px;background:var(--card);border:1px solid var(--hair);border-radius:14px;padding:8px 15px;margin-bottom:10px;box-shadow:var(--elev1)}
 #drawBrush label{font-size:13px;font-weight:800;color:var(--fg2);white-space:nowrap}
 #drawBrush input{flex:1;accent-color:var(--fg);min-width:0}
 #drawBrush b{font-size:13px;font-weight:800;color:var(--fg);min-width:52px;text-align:right}
 #drawCats{display:flex;gap:4px;margin-bottom:10px;background:var(--fill);border:1px solid var(--hair);border-radius:13px;padding:4px}
 .dc-tab{min-height:var(--tap-sm);flex:1;display:flex;align-items:center;justify-content:center;gap:5px;border:none;background:none;border-radius:9px;font-family:inherit;font-size:12px;font-weight:800;color:var(--fg2);cursor:pointer;white-space:nowrap}
 .dc-tab svg{width:15px;height:15px}
 .dc-tab.on{background:var(--card);color:var(--fg);box-shadow:var(--elev1)}
 #drawPens{display:flex;gap:9px;overflow-x:auto;scrollbar-width:none;padding:2px 2px 4px;margin-bottom:11px;min-height:56px}
 #drawPens::-webkit-scrollbar{display:none}
 .dt-sw{flex:0 0 auto;display:flex;flex-direction:column;align-items:center;gap:5px;border:none;background:none;font-family:inherit;cursor:pointer;padding:2px;opacity:.55;transition:opacity .15s,transform .15s}
 .dt-sw.on{opacity:1;transform:translateY(-1px)}
 .dt-sw i{width:34px;height:34px;border-radius:50%;border:2.5px solid #fff;box-shadow:0 2px 7px rgba(0,0,0,.22);display:block;background-size:6px 6px}
 .dt-sw.on i{box-shadow:0 0 0 2.5px var(--fg),0 2px 7px rgba(0,0,0,.22)}
 .dt-sw span{font-size:10px;font-weight:800;color:var(--fg2);white-space:nowrap;letter-spacing:-.01em}
 #drawActions{display:flex;align-items:center;gap:9px}
 #drawActions .db{width:46px;height:46px;flex-shrink:0;border-radius:14px;border:1px solid var(--hair);background:var(--card);box-shadow:var(--elev1);display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--fg2)}
 #drawActions .db:active{transform:scale(.94)}
 #drawActions .db svg{width:20px;height:20px}
 #drawPostBtn{flex:1;height:46px;border-radius:14px;border:none;background:var(--fg);color:#fff;font-size:15px;font-weight:800;font-family:inherit;cursor:pointer}
 #drawPostBtn:active{transform:scale(.98)}
 /* draw finalize sheet + feed snapshot image */
 #drawFinish{position:fixed;inset:0;z-index:4100;display:none;align-items:flex-end;justify-content:center}
 #drawFinish::before{content:"";position:absolute;inset:0;background:rgba(14,17,22,.32);}
 .dfin-sheet{border:none;position:relative;width:100%;max-width:440px;max-height:88vh;overflow-y:auto;background:var(--glass2);border-radius:24px 24px 0 0;padding:8px 16px calc(env(safe-area-inset-bottom,0px) + 16px);box-shadow:var(--elev3);animation:rpSheetIn .28s cubic-bezier(.34,1.4,.64,1);-webkit-overflow-scrolling:touch}
 .dfin-grab{width:38px;height:4px;border-radius:2px;background:var(--hair);margin:6px auto 10px}
 .dfin-sheet h3{margin:2px 0 12px;font-size:17px;font-weight:800;text-align:center;color:var(--fg)}
 .dfin-snap{position:relative;border-radius:16px;overflow:hidden;border:1px solid var(--hair);margin-bottom:12px;box-shadow:var(--elev1)}
 .dfin-snap img{width:100%;display:block;max-height:260px;object-fit:cover}
 .dfin-tag{position:absolute;left:10px;bottom:10px;background:rgba(10,10,12,.72);color:#fff;font-size:11px;font-weight:800;padding:4px 9px;border-radius:9px}
 .dfin-photo{width:100%;display:flex;align-items:center;justify-content:center;gap:8px;height:46px;border-radius:13px;border:1.5px dashed var(--hair);background:var(--fill);color:var(--fg2);font-size:14px;font-weight:800;font-family:inherit;cursor:pointer;margin-bottom:12px}
 .dfin-photo svg{width:19px;height:19px}
 #drawPhotoPrev{width:100%;max-height:220px;object-fit:cover;border-radius:13px;margin-bottom:12px;display:none}
 #drawCaption{width:100%;box-sizing:border-box;min-height:74px;resize:vertical;border-radius:13px;border:1px solid var(--hair);background:var(--fill);padding:11px 13px;font-family:inherit;font-size:14px;color:var(--fg);margin-bottom:14px}
 .insp-prog h4 em{color:#7c3aed}
 .prog-conf{display:flex;align-items:center;gap:9px;margin-top:3px}
 .prog-bar{flex:1;height:8px;border-radius:5px;background:var(--fill2);overflow:hidden}
 .prog-bar i{display:block;height:100%;border-radius:5px;background:linear-gradient(90deg,#ef4444,#f59e0b 45%,#22c55e)}
 .prog-conf span{font-size:12px;font-weight:800;color:var(--fg2);white-space:nowrap}
 .prog-note{font-size:11.5px;color:var(--mut);margin-top:6px;line-height:1.4}
 .dfin-actions{display:flex;gap:10px}
 .dfin-back{flex:0 0 auto;padding:0 18px;height:48px;border-radius:14px;border:1px solid var(--hair);background:var(--card);color:var(--fg2);font-size:14px;font-weight:800;font-family:inherit;cursor:pointer}
 .dfin-post{flex:1;height:48px;border-radius:14px;border:none;background:var(--fg);color:#fff;font-size:15px;font-weight:800;font-family:inherit;cursor:pointer}
 .dfin-post:active,.dfin-back:active{transform:scale(.98)}
 .feed-card-visual.second{margin-top:6px;position:relative}
 .fc-wrap{position:relative;margin:2px 14px 0;aspect-ratio:4/3;border-radius:16px;overflow:hidden;background:var(--fill2)}
 .fc-carousel{display:flex;height:100%;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none;-webkit-overflow-scrolling:touch}
 .fc-carousel::-webkit-scrollbar{display:none}
 .fc-slide{flex:0 0 100%;height:100%;scroll-snap-align:start;position:relative}
 .fc-slide img{width:100%;height:100%;object-fit:cover;display:block}
 .fc-dots{position:absolute;left:0;right:0;bottom:8px;display:flex;gap:6px;justify-content:center;pointer-events:none}
 .fc-dots i{width:6px;height:6px;border-radius:50%;background:rgba(252,250,246,.6);box-shadow:0 1px 3px rgba(0,0,0,.5);transition:width .2s,background .2s}
 .fc-dots i.on{background:var(--card);width:16px;border-radius:3px}
 .fc-snap-tag{position:absolute;left:8px;bottom:8px;background:rgba(10,10,12,.72);color:#fff;font-size:10.5px;font-weight:800;padding:4px 8px;border-radius:8px}
 .report-overlay{position:fixed;inset:0;z-index:3000;display:flex;align-items:center;justify-content:center;padding:16px}
 .report-overlay .ro-bg{position:absolute;inset:0;background:rgba(14,17,22,.32);}
 .report-sheet{position:relative;width:100%;max-width:440px;max-height:92vh;overflow-y:auto;background:var(--glass2);border-radius:26px;box-shadow:var(--elev3);-webkit-overflow-scrolling:touch;animation:rpSheetIn .28s cubic-bezier(.34,1.4,.64,1)}
 @keyframes rpSheetIn{from{opacity:0;transform:translateY(16px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
 .report-sheet .sh{display:none}
 .cat-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
 .cat-chip{display:flex;flex-direction:column;align-items:center;gap:7px;padding:14px 4px;border-radius:16px;border:1.5px solid var(--ink-100);background:var(--fill);cursor:pointer;font-size:12px;font-weight:650;color:var(--mut);transition:all .2s cubic-bezier(.34,1.56,.64,1);-webkit-tap-highlight-color:transparent}
 .cat-chip:active{transform:scale(.94)}
 .cat-chip.active{border-color:currentColor;background:var(--card);box-shadow:0 4px 16px rgba(22,21,46,.1)}
 .cat-chip .cat-ico-w{width:30px;height:30px;display:flex;align-items:center;justify-content:center}
 .cat-chip .cat-ico-w svg{width:26px;height:26px;stroke:currentColor}
 .sub-chips{display:flex;flex-wrap:wrap;gap:8px}
 .sub-chip{padding:11px 16px;border-radius:999px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);cursor:pointer;font-size:14px;font-weight:600;color:var(--fg2);transition:all .15s;-webkit-tap-highlight-color:transparent}
 .sub-chip:active{transform:scale(.95)}
 .sub-chip.active{background:var(--acc);border-color:var(--acc);color:#fff}
 .bucket-track{display:flex;gap:8px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;padding:4px 0}
 .bucket-track::-webkit-scrollbar{display:none}
 .bucket{min-width:64px;padding:14px 8px;border-radius:15px;border:1.5px solid var(--ink-100);background:var(--fill);cursor:pointer;font-size:16px;font-weight:700;color:var(--fg2);text-align:center;flex-shrink:0;transition:all .2s cubic-bezier(.34,1.56,.64,1);-webkit-tap-highlight-color:transparent}
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
 .rp-stars button.on{color:var(--warn)}
 .rp-stars button.on svg{fill:#C08A2E}
 .rp-stars button:active{transform:scale(1.2)}
 .rp-stars-lbl{text-align:center;font-size:14px;font-weight:700;color:var(--fg2);margin-top:8px;min-height:20px}
 .rp-voice-btn{width:100%;padding:14px;border-radius:14px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);cursor:pointer;font-size:14px;font-weight:600;color:var(--fg2);display:flex;align-items:center;justify-content:center;gap:8px;transition:all .15s;-webkit-tap-highlight-color:transparent}
 .rp-voice-btn:active,.rp-voice-btn.recording{background:rgba(20,20,25,.1);border-color:var(--acc);color:var(--acc)}
 .rp-caption{width:100%;padding:14px;border:1.5px solid rgba(22,21,46,.1);border-radius:14px;font-size:15px;font-family:inherit;resize:none;outline:none;background:var(--fill);color:var(--fg);box-sizing:border-box;min-height:64px;margin-top:10px}
 .rp-caption::placeholder{color:var(--mut)}
 .rp-caption:focus{border-color:var(--acc);background:var(--card);box-shadow:0 0 0 3px var(--glow)}
 .rp-wiz-head{display:flex;align-items:center;gap:12px;padding:16px 18px 0}
 .rp-nav-btn{width:36px;height:36px;border-radius:11px;border:none;background:var(--ink-050);color:var(--fg2);display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:.15s}
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
 .rp-photo-big.has-img{border:none;background:var(--ink-050)}
 .rp-photo-big img{width:100%;height:100%;object-fit:cover}
 .rp-photo-placeholder{display:flex;flex-direction:column;align-items:center;gap:12px;color:var(--acc)}
 .rp-photo-placeholder svg{width:52px;height:52px;opacity:.9}
 .rp-photo-placeholder span{font-size:15px;font-weight:700}
 .rp-loc-card{display:flex;align-items:center;gap:8px;padding:12px 14px;border-radius:14px;background:var(--fill);color:var(--fg2);font-size:13.5px;font-weight:600;margin-bottom:10px}
 .rp-loc-switch{margin-left:auto;background:none;border:none;color:var(--acc);font-weight:700;font-size:12.5px;cursor:pointer;font-family:inherit;flex-shrink:0;white-space:nowrap}
 .rp-peak{margin-bottom:12px;padding:14px 16px;border-radius:16px;background:rgba(20,20,25,.06);border:1.5px solid rgba(20,20,25,.18)}
 .rp-peak-q{font-size:15px;color:var(--fg);font-weight:700;margin-bottom:10px}
 .rp-peak-btns{display:flex;gap:8px}
 .rp-peak-btns button{flex:1;padding:11px;border-radius:11px;border:1.5px solid rgba(22,21,46,.12);background:var(--card);color:var(--fg2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}
 .rp-peak-btns button.yes{background:var(--acc);color:#fff;border-color:var(--acc)}
 .rp-peak.confirmed{border-color:#25a35a;background:rgba(37,163,90,.08)}
 .rp-peak.confirmed .rp-peak-q{color:#1c7a44}
 .rp-group{display:flex;align-items:center;gap:10px;margin-top:12px}
 .rp-group-lbl{font-size:14px;color:var(--fg2);font-weight:650;flex-shrink:0}
 .rp-group-sel{flex:1;padding:11px 14px;border-radius:12px;border:1.5px solid rgba(22,21,46,.1);background:var(--fill);color:var(--fg);font-size:14px;font-family:inherit;outline:none;-webkit-appearance:none;appearance:none}
 .rp-summary{margin-top:14px;display:flex;flex-wrap:wrap;gap:6px}
 .rp-summary .rp-tag{padding:5px 11px;border-radius:999px;background:var(--ink-050);color:var(--fg2);font-size:12.5px;font-weight:650;display:inline-flex;align-items:center;gap:5px}
 .rp-summary .rp-tag svg{width:13px;height:13px;stroke:var(--acc)}
 .rp-wiz-nav{display:flex;align-items:center;gap:12px;margin-top:14px;padding:16px 20px calc(env(safe-area-inset-bottom,0px) + 26px);position:sticky;bottom:0;background:linear-gradient(180deg,rgba(252,250,246,0),#fff 34%);border-top:1px solid var(--ink-050)}
 .rp-back-btn{display:inline-flex;align-items:center;gap:4px;background:var(--ink-050);border:none;color:var(--fg2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;padding:12px 15px;border-radius:14px;flex-shrink:0}
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
 .obs-types.cluster .obs-type{flex-direction:column;align-items:flex-start;gap:10px;padding:15px 14px 13px;border-radius:20px;border:1px solid var(--hair);background:linear-gradient(155deg,var(--tt),var(--paper) 72%);transform:translate(var(--dx),var(--dy)) rotate(var(--rot));animation:obsPop .55s var(--ease-spring) both;animation-delay:calc(var(--i)*70ms);box-shadow:var(--elev1)}
 .obs-types.cluster .obs-type:hover{box-shadow:var(--elev2);background:linear-gradient(155deg,var(--tt),#fff 72%)}
 .obs-types.cluster .obs-type:active{transform:translate(var(--dx),var(--dy)) rotate(var(--rot)) scale(.95)}
 .obs-types.cluster .obs-type:last-child{grid-column:1/-1;flex-direction:row;align-items:center}
 .obs-types.cluster .obs-type-ic{width:52px;height:52px;border-radius:16px;background:var(--tc);color:#fff;box-shadow:0 6px 14px var(--tt)}
 .obs-types.cluster .obs-type-ic svg{width:28px;height:28px}
 /* Landing der normalen Meldung: monochrom (B/W) */
 .obs-types.cluster .obs-type{background:var(--card)}
 .obs-types.cluster .obs-type:hover{background:var(--card)}
 .obs-types.cluster .obs-type-ic{background:#111116;box-shadow:0 6px 14px rgba(0,0,0,.20)}
 .obs-quick{border-color:rgba(10,10,12,.4);background:var(--card)}
 .obs-quick svg{color:var(--ink-900)}
 .obs-types.cluster .obs-type-tx b{font-size:15.5px}
 @keyframes obsPop{from{opacity:0;transform:translate(0,30px) scale(.35) rotate(0deg)}60%{opacity:1}to{opacity:1;transform:translate(var(--dx),var(--dy)) rotate(var(--rot)) scale(1)}}
 .obs-type{display:flex;align-items:center;gap:14px;padding:16px;border-radius:18px;border:1.5px solid var(--ink-100);background:var(--fill);cursor:pointer;text-align:left;font-family:inherit;transition:all .16s cubic-bezier(.34,1.56,.64,1)}
 .obs-type:active{transform:scale(.98)}
 .obs-type:hover{background:var(--card);box-shadow:0 4px 16px var(--ink-100)}
 .obs-type-ic{width:46px;height:46px;border-radius:14px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
 .obs-type-ic svg{width:26px;height:26px}
 .obs-type-tx b{display:block;font-size:16px;font-weight:800;color:var(--fg);letter-spacing:-.01em}
 .obs-type-tx span{font-size:12.5px;color:var(--mut);font-weight:500}
 .obs-media-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
 .obs-media-tile{aspect-ratio:1;border-radius:14px;overflow:hidden;position:relative;background:var(--ink-050)}
 .obs-media-tile img{width:100%;height:100%;object-fit:cover}
 .obs-media-tile .rm{position:absolute;top:4px;right:4px;width:22px;height:22px;border-radius:50%;background:rgba(14,17,22,.32);color:#fff;border:none;font-size:13px;cursor:pointer;display:flex;align-items:center;justify-content:center}
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
 .obs-card{border:1.5px solid var(--ink-100);border-radius:16px;margin-bottom:10px;overflow:hidden;background:var(--card)}
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
 .obs-sw span{position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:var(--card);transition:left .18s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
 .obs-sw.on{background:var(--acc)}.obs-sw.on span{left:23px}
 .obs-size-vis{display:flex;align-items:center;gap:14px;margin:6px 0 12px;padding:14px;border-radius:16px;background:var(--fill)}
 .obs-size-obj{font-size:44px;line-height:1;flex-shrink:0}
 .obs-size-cons{font-size:13.5px;color:var(--fg2);font-weight:600;line-height:1.4}
 .obs-person{border:1.5px solid rgba(22,21,46,.1);border-radius:14px;padding:12px 14px;margin-bottom:10px;position:relative}
 .obs-person .rm{position:absolute;top:8px;right:8px;background:none;border:none;color:var(--danger);font-size:13px;font-weight:700;cursor:pointer}
 .obs-add-btn{width:100%;padding:12px;border-radius:12px;border:1.5px dashed rgba(20,20,25,.3);background:rgba(20,20,25,.05);color:var(--acc2);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}
 .obs-loc-map{width:100%;height:170px;border-radius:16px;overflow:hidden;margin-bottom:10px;border:1px solid rgba(22,21,46,.1)}
 .obs-loc-wrap{position:relative}
 .obs-pin{position:absolute;left:50%;top:50%;transform:translate(-50%,-100%);z-index:600;color:#A83A2E;pointer-events:none;filter:drop-shadow(0 3px 5px rgba(0,0,0,.3))}
 .obs-pin svg{width:34px;height:34px;display:block}
 .mm-tools{position:absolute;right:8px;top:8px;display:flex;flex-direction:column;gap:6px;z-index:600}
 .mm-tools button{width:36px;height:36px;border-radius:11px;border:1px solid var(--hair);background:var(--paper);color:var(--fg);cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:var(--elev1)}
 .mm-tools button svg{width:17px;height:17px}
 .obs-pin-hint{position:absolute;left:50%;bottom:8px;transform:translateX(-50%);z-index:600;pointer-events:none;font-size:10.5px;font-weight:700;color:#fff;background:rgba(11,17,32,.62);padding:4px 10px;border-radius:999px;white-space:nowrap}
 .obs-loc-row{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--fg2);font-weight:600;margin-bottom:10px;flex-wrap:wrap}
 .obs-loc-src{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;background:rgba(20,20,25,.12);color:var(--acc2)}
 .obs-dt{width:100%;box-sizing:border-box;padding:12px 14px;border:1.5px solid rgba(22,21,46,.1);border-radius:12px;font-size:14px;font-family:inherit;color:var(--fg);background:var(--fill);margin-bottom:10px}
 .obs-warn{font-size:12.5px;color:var(--warn);background:rgba(245,158,11,.12);border-radius:10px;padding:8px 11px;margin-bottom:10px;font-weight:600}
 .obs-cc{font-size:11px;color:var(--mut);text-align:right;margin-top:4px}
 .obs-summary{margin-top:14px;display:flex;flex-wrap:wrap;gap:6px}
 .obs-summary .rp-tag{padding:5px 11px;border-radius:999px;background:var(--ink-050);color:var(--fg2);font-size:12.5px;font-weight:650}
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
 .ab-h span{position:absolute;top:-24px;left:50%;transform:translateX(-50%);font-size:11px;font-weight:800;color:var(--acc2);background:var(--paper);padding:1px 6px;border-radius:7px;white-space:nowrap}
 .ab-scale{display:flex;justify-content:space-between;font-size:10px;font-weight:700;color:var(--mut);margin-top:4px;padding:0 4px}
 .cat-chip .cat-ico-w{width:30px;height:30px;display:flex;align-items:center;justify-content:center}
 .cat-chip .cat-ico-w svg{width:26px;height:26px;stroke:currentColor}
 /* --- Undo snackbar --- */
 .undo-bar{position:fixed;bottom:calc(env(safe-area-inset-bottom,0px) + 20px);left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:999px;background:rgba(20,30,51,.95);color:#E8EEF7;font-size:14px;font-weight:600;display:flex;align-items:center;gap:12px;box-shadow:0 4px 20px rgba(0,0,0,.3);z-index:5000;animation:undoIn .3s cubic-bezier(.34,1.56,.64,1)}
 .undo-bar button{background:none;border:none;color:#FF8A5B;font-weight:700;font-size:14px;cursor:pointer}
 @keyframes undoIn{from{opacity:0;transform:translateX(-50%) translateY(20px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
 /* --- Feed (full-page, Instagram-style) --- */
 .feed-page{position:fixed;inset:0;z-index:3000;background:var(--page);transform:translateX(100%);transition:transform .35s cubic-bezier(.32,.72,.42,1);display:flex;flex-direction:column;will-change:transform}
 .feed-page.open{transform:translateX(0)}
 .feed-nav{display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--glass2);border-bottom:1px solid var(--hair);position:sticky;top:0;z-index:2;padding-top:calc(12px + env(safe-area-inset-top,0px))}
 .feed-back{background:none;border:none;cursor:pointer;padding:6px;display:flex;align-items:center;justify-content:center;color:var(--fg);border-radius:8px}
 .feed-back:hover{background:rgba(0,0,0,.04)}
 .feed-title{font-size:22px;font-weight:800;color:var(--fg);letter-spacing:-.04em;flex:1}
 .feed-filter{display:flex;gap:6px;padding:10px 16px;overflow-x:auto;scrollbar-width:none;background:transparent;border-bottom:none}
 .feed-filter::-webkit-scrollbar{display:none}
 .feed-filter button{min-height:var(--tap-sm);padding:0 16px;border-radius:var(--r-full);border:1px solid var(--ink-100);background:var(--paper);font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;white-space:nowrap;flex-shrink:0;transition:all .15s var(--ease);font-family:inherit;display:flex;align-items:center;gap:6px;box-shadow:var(--elev1)}
 .feed-filter button .cat-ico{width:16px;height:16px;display:flex;align-items:center;justify-content:center}
 .feed-filter button .cat-ico svg{width:14px;height:14px}
 .feed-filter button.active{background:var(--fg);color:#fff;border-color:var(--fg)}
 .feed-filter button.active .cat-ico svg{stroke:#fff}
 .feed-loc{display:flex;gap:8px;padding:0 16px 12px;overflow-x:auto;scrollbar-width:none;background:transparent}
 .feed-loc::-webkit-scrollbar{display:none}
 .feed-loc-btn,.feed-loc-sel,.feed-loc-clear{flex-shrink:0;padding:8px 14px;border-radius:999px;border:1.5px solid var(--ink-100);background:var(--card);font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px}
 .feed-loc-btn svg{width:15px;height:15px}
 .feed-loc-btn.active{background:var(--fg);color:#fff;border-color:var(--fg)}
 .feed-loc-sel{-webkit-appearance:none;appearance:none;padding-right:16px}
 .feed-loc-sel.active{background:var(--acc);color:#fff;border-color:var(--acc)}
 .feed-loc-clear{background:rgba(255,84,112,.1);color:var(--danger);border-color:transparent}
 .feed-anchor-bar{padding:0 16px 12px;background:var(--card);font-size:13px;color:var(--fg2);font-weight:600;display:flex;align-items:center;gap:6px}
 .feed-anchor-bar b{color:var(--acc2)}
 .feed-card-dist{font-size:11px;font-weight:700;color:var(--acc2);background:rgba(20,20,25,.1);padding:2px 8px;border-radius:999px;margin-left:auto;flex-shrink:0}
 /* Feed scope segmented control */
 .feed-scope{display:flex;gap:6px;padding:10px 16px 10px;overflow-x:auto;scrollbar-width:none;background:transparent}
 .feed-scope::-webkit-scrollbar{display:none}
 .feed-scope button{min-height:var(--tap-sm);flex:1;min-width:max-content;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:0 12px;border-radius:12px;border:none;background:var(--fill);font-size:13px;font-weight:700;color:var(--mut);cursor:pointer;font-family:inherit;white-space:nowrap;transition:.18s var(--ease)}
 .feed-scope button svg{width:16px;height:16px}
 .feed-scope button.active{background:var(--ink-900);color:var(--paper)}
 /* Feed group chips row */
 .feed-groups{display:flex;gap:8px;padding:0 16px 12px;overflow-x:auto;scrollbar-width:none;background:transparent}
 .feed-groups::-webkit-scrollbar{display:none}
 .feed-groups button{flex-shrink:0;padding:8px 14px;border-radius:999px;border:1.5px solid var(--ink-100);background:var(--card);font-size:13px;font-weight:600;color:var(--fg2);cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px}
 .feed-groups button.active{background:var(--acc);color:#fff;border-color:var(--acc)}
 .feed-groups button.manage{background:rgba(0,0,0,.04);border-color:transparent;color:var(--fg2)}
 /* Like + follow on cards */
 .endorse-btn .endorse-lbl{font-weight:700}
 .feed-card-actions button.endorsed{color:var(--ok)}
 .feed-card-actions button.endorsed svg{stroke:var(--ok)}
 .feed-card-actions .del-btn{color:var(--danger)}
 .feed-card-actions .del-btn svg{stroke:var(--danger)}
 .uv-del{position:absolute;top:6px;right:6px;width:28px;height:28px;border:none;border-radius:9px;background:rgba(14,17,22,.32);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;}
 .uv-del svg{width:15px;height:15px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
 .uv-cell{position:relative}
 .uv-post{position:relative}
 .uv-post .uv-del{background:rgba(192,48,74,.1);color:var(--danger);top:50%;transform:translateY(-50%)}
 .uv-post .uv-del svg{stroke:var(--danger)}
 .uv-post.own .b{padding-right:44px}
 .feed-follow{margin-left:auto;flex-shrink:0;padding:5px 12px;border-radius:999px;border:1.5px solid var(--acc);background:none;color:var(--acc);font-size:12px;font-weight:700;cursor:pointer;font-family:inherit}
 .feed-follow.following{background:var(--acc);color:#fff}
 .feed-card-group{font-size:11px;font-weight:700;color:var(--ink-700);background:rgba(156,39,176,.1);padding:2px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:4px}
 /* Groups modal */
 .groups-modal{position:fixed;inset:0;z-index:3600;background:rgba(14,17,22,.32);display:flex;flex-direction:column;justify-content:flex-end}
 .groups-sheet{border:none;background:var(--glass2);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:80vh;display:flex;flex-direction:column;padding-bottom:calc(env(safe-area-inset-bottom,0px) + 12px);box-shadow:var(--elev3)}
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
 .cmt-modal{position:fixed;inset:0;z-index:3900;background:rgba(14,17,22,.32);display:flex;flex-direction:column;justify-content:flex-end}
 .cmt-sheet{border:none;background:var(--glass2);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:82vh;display:flex;flex-direction:column;box-shadow:var(--elev3)}
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
 .prof-modal{position:fixed;inset:0;z-index:5200;background:rgba(14,17,22,.32);display:flex;flex-direction:column;justify-content:flex-end}
 @media(min-width:561px){.prof-modal{justify-content:center;align-items:center;padding:16px}}
 .prof-sheet{background:var(--glass2);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:88vh;overflow-y:auto;box-shadow:var(--elev3)}
 @media(min-width:561px){.prof-sheet{border-radius:var(--r-xl);width:100%;max-width:420px}}
 .prof-head{display:flex;align-items:center;justify-content:space-between;padding:16px 20px 8px;font-size:19px;font-weight:800;color:var(--fg)}
 .prof-head button{background:none;border:none;font-size:19px;color:var(--mut);cursor:pointer}
 .prof-body{padding:14px 24px calc(env(safe-area-inset-bottom,0px) + 30px)}
 .prof-top{display:flex;align-items:center;gap:16px;margin-bottom:18px}
 .prof-av{position:relative;width:76px;height:76px;border-radius:26%;background:var(--fg);color:#fff;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:800;cursor:pointer;flex-shrink:0;overflow:hidden;background-size:cover;background-position:center}
 .prof-av .prof-av-edit{position:absolute;right:-2px;bottom:-2px;width:28px;height:28px;border-radius:50%;background:var(--acc);color:#fff;display:flex;align-items:center;justify-content:center;border:2.5px solid #fff}
 .prof-av .prof-av-edit svg{width:14px;height:14px}
 .prof-av.has-img span{display:none}
 .prof-av.has-img .prof-av-edit{display:none}
 .prof-meta{min-width:0}
 .prof-name{font-size:20px;font-weight:800;color:var(--fg);letter-spacing:-.02em}
 .prof-endo{font-size:14px;color:var(--acc2);font-weight:700;margin-top:2px}
 .prof-lbl{display:flex;justify-content:space-between;font-size:13px;font-weight:700;color:var(--fg2);margin-bottom:6px}
 .prof-cnt{color:var(--mut);font-weight:600}
 .prof-cnt.over{color:var(--danger)}
 .prof-bio{width:100%;box-sizing:border-box;padding:12px 14px;border:1.5px solid var(--hair);border-radius:14px;font-size:15px;font-family:inherit;resize:none;outline:none;background:var(--fill);color:var(--fg);min-height:96px}
 .prof-bio:focus{border-color:var(--acc);background:var(--card);box-shadow:0 0 0 3px var(--glow)}
 .prof-row{display:flex;align-items:center;justify-content:space-between;margin-top:16px;font-size:15px;font-weight:600;color:var(--fg)}
 .prof-toggle{width:50px;height:30px;border-radius:999px;border:none;background:rgba(22,21,46,.15);cursor:pointer;position:relative;transition:background .2s;flex-shrink:0}
 .prof-toggle span{position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:var(--card);transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
 .prof-toggle.on{background:var(--acc)}
 .prof-toggle.on span{left:23px}
 .prof-save{width:100%;margin-top:20px;padding:15px;border-radius:15px;border:none;background:var(--acc);color:#fff;font-size:16px;font-weight:800;cursor:pointer;font-family:inherit}
 .prof-save:hover{background:var(--acc2)}
 .prof-feedback{width:100%;margin-top:10px;padding:13px;border-radius:14px;border:1px solid var(--bd);background:none;color:var(--acc2);font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}
 .prof-signout{width:100%;margin-top:10px;padding:13px;border-radius:14px;border:none;background:none;color:var(--danger);font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}
 .prof-bioview{font-size:14px;color:var(--fg2);line-height:1.55;margin-top:4px;white-space:pre-wrap;min-height:12px}
 /* --- Public user profile (hero layout) --- */
 .uv-sheet{overflow:hidden}
 #userViewModal{justify-content:stretch;align-items:stretch;padding:0}
 #userViewModal .prof-sheet{width:100%;max-width:none;height:100%;max-height:none;border-radius:0;display:flex;flex-direction:column}
 #userViewModal .uv-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch}
 .uv-posts{margin-top:22px;text-align:left}
 .uv-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;border-radius:14px;overflow:hidden;margin-bottom:12px}
 .uv-cell{aspect-ratio:1;cursor:pointer;background:var(--fill)}
 .uv-cell img{width:100%;height:100%;object-fit:cover;display:block}
 .uv-posts h3{font-size:13px;font-weight:800;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px}
 .uv-post{background:var(--card);border:1px solid var(--hair);border-radius:14px;margin-bottom:10px;overflow:hidden;cursor:pointer;box-shadow:var(--elev1)}
 .uv-post img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block}
 .uv-post .b{padding:10px 12px;font-size:13px;color:var(--fg2)}
 .uv-post .b b{color:var(--fg)}
 .uv-post .t{font-size:11px;color:var(--mut);font-weight:600;margin-top:3px}
 .uv-hero{position:relative;height:108px;padding-top:env(safe-area-inset-top,0px);background:linear-gradient(150deg,#0a0a0c 0%,#26262c 60%,#55555e 100%)}
 .uv-hero::after{content:'';position:absolute;inset:0;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 90'%3E%3Cpath d='M0 90 60 42 110 68 180 22 240 58 300 30 400 74 400 90Z' fill='rgba(252,250,246,.10)'/%3E%3C/svg%3E") bottom/cover no-repeat}
 .uv-close{position:absolute;top:calc(env(safe-area-inset-top,0px) + 10px);right:10px;z-index:1;width:30px;height:30px;border-radius:10px;border:none;background:rgba(252,250,246,.22);color:#fff;font-size:15px;cursor:pointer;}
 .uv-body{text-align:center;padding-top:60px!important}
 .uv-av{position:absolute;left:50%;bottom:-46px;transform:translateX(-50%);margin:0;width:92px;height:92px;font-size:32px;box-shadow:0 0 0 5px #fff,var(--elev2);z-index:2}
 .uv-name{font-size:21px;font-weight:800;color:var(--fg);letter-spacing:-.02em}
 .uv-bio{font-size:14px;color:var(--fg2);line-height:1.5;margin:6px auto 0;max-width:290px;white-space:pre-wrap}
 .uv-bio:empty{display:none}
 .uv-stats{display:flex;justify-content:center;gap:8px;margin:16px 0 4px}
 .uv-stat{flex:1;max-width:110px;background:var(--card);border:1px solid var(--hair);border-radius:16px;padding:12px 6px;box-shadow:var(--elev1)}
 .uv-stat b{display:block;font-size:19px;font-weight:800;color:var(--fg);letter-spacing:-.02em}
 .uv-stat span{font-size:10.5px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 .uv-report{width:100%;margin-top:10px;border:none;background:none;color:var(--mut);font-size:13px;font-weight:700;font-family:inherit;cursor:pointer;padding:9px;text-decoration:underline;text-underline-offset:3px}
 /* --- Settings items (own profile) --- */
 .prof-view{animation:qrFade .22s var(--ease) both}
 .prof-item .chev{margin-left:auto;color:var(--mut);font-size:17px;font-weight:600}
 .prof-back{border:none;background:none;color:var(--fg2);font-size:14px;font-weight:750;font-family:inherit;cursor:pointer;padding:8px 0 2px;margin-left:-2px;display:block}
 .prof-hint{font-size:12px;color:var(--mut);margin-top:12px;line-height:1.55}
 .prof-input{width:100%;box-sizing:border-box;background:var(--ink-050);border:1px solid var(--ink-100);
   border-radius:var(--r-1);min-height:var(--tap);padding:0 var(--sp3);font-family:inherit;font-size:15px;
   color:var(--ink-900);margin-bottom:var(--sp2)}
 .prof-input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
 select.prof-input{appearance:none;-webkit-appearance:none;padding-right:34px;
   background-image:linear-gradient(45deg,transparent 50%,currentColor 50%),linear-gradient(135deg,currentColor 50%,transparent 50%);
   background-position:calc(100% - 18px) 50%,calc(100% - 13px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
 .prof-seg{display:flex;gap:4px;background:var(--ink-050);border-radius:var(--r-1);padding:3px;margin-bottom:var(--sp3)}
 .prof-seg button{flex:1;min-height:var(--tap-sm);border:none;border-radius:6px;background:transparent;
   font-family:inherit;font-size:13px;font-weight:700;color:var(--ink-500);cursor:pointer;white-space:nowrap}
 .prof-seg button.on{background:var(--accent);color:var(--paper)}
 .prof-seg button:active{transform:scale(.97)}
 .prof-seg{display:flex;background:var(--fill);border:1px solid var(--hair);border-radius:13px;padding:3px;gap:3px;margin-bottom:4px}
 .prof-seg button{flex:1;border:none;background:none;padding:10px 6px;border-radius:10px;font-size:13px;font-weight:700;color:var(--mut);font-family:inherit;cursor:pointer;transition:.15s}
 .prof-seg button.active{background:var(--card);color:var(--fg);box-shadow:var(--elev1)}
 .prof-sec-title{margin:24px 2px 10px;font-size:12px;font-weight:800;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
 .prof-item{min-height:var(--tap);width:100%;display:flex;align-items:center;gap:11px;padding:13px 14px;margin-bottom:8px;border-radius:14px;border:1px solid var(--hair);background:var(--fill);color:var(--fg);font-size:14.5px;font-weight:650;font-family:inherit;cursor:pointer;text-align:left;transition:.15s}
 .prof-item svg{width:19px;height:19px;flex-shrink:0;color:var(--acc2)}
 .prof-item:hover{background:var(--card)}
 .prof-item.danger{color:var(--danger)}
 .prof-item.danger svg{color:var(--danger)}
 .prof-save.following{background:var(--fg)}
 .feed-card-avatar,.feed-card-user{cursor:pointer}
 /* Location search picker (Gipfel / Gebiet) */
 .loc-picker{position:fixed;inset:0;z-index:3800;background:rgba(14,17,22,.32);display:flex;flex-direction:column;justify-content:flex-end}
 .loc-picker-sheet{border:none;background:var(--glass2);border-radius:var(--r-xl) var(--r-xl) 0 0;max-height:80vh;display:flex;flex-direction:column;padding-bottom:calc(env(safe-area-inset-bottom,0px) + 8px);box-shadow:var(--elev3)}
 .loc-picker-head{display:flex;align-items:center;gap:10px;padding:12px 18px 12px;border-bottom:1px solid var(--hair)}
 .loc-picker-head svg{width:20px;height:20px;color:var(--mut);flex-shrink:0}
 .loc-picker-head input{flex:1;border:none;outline:none;font-size:17px;font-family:inherit;color:var(--fg);background:none}
 .loc-picker-head input::placeholder{color:var(--mut)}
 .loc-picker-head button{background:none;border:none;font-size:19px;color:var(--mut);cursor:pointer;width:30px;flex-shrink:0}
 .loc-picker-list{overflow-y:auto;-webkit-overflow-scrolling:touch;padding:6px}
 .loc-picker-list button{display:flex;align-items:center;width:100%;gap:10px;padding:13px 14px;border:none;background:none;border-radius:12px;cursor:pointer;font-family:inherit;font-size:15px;font-weight:600;color:var(--fg);text-align:left}
 .loc-picker-list button:hover{background:var(--ink-050)}
 .loc-picker-list button .lp-e{margin-left:auto;font-size:12.5px;font-weight:600;color:var(--mut)}
 .loc-picker-list .lp-empty{text-align:center;color:var(--mut);padding:30px;font-size:14px}
 .feed-scroll{background:var(--ink-050);flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding-bottom:env(safe-area-inset-bottom,0px)}
 .feed-grid{max-width:600px;margin:0 auto;padding:10px 0 120px}
 .feed-card{background:var(--card);margin:0 14px 20px;border-radius:22px;border:1px solid var(--hair);box-shadow:var(--elev1);overflow:hidden;cursor:pointer;transition:box-shadow .25s var(--ease),transform .25s var(--ease)}
 .feed-card:hover{box-shadow:var(--elev2)}
 @media(hover:none){.feed-card:active{transform:scale(.992)}}
 .feed-card.enter{animation:cardIn .5s var(--ease) both}
 @keyframes cardIn{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
 .feed-card.flash{box-shadow:0 0 0 3px var(--acc) inset;animation:cardFlash 1.8s ease}
 @keyframes cardFlash{0%,100%{background:var(--card)}30%{background:rgba(20,20,25,.08)}}
 .feed-card-head{display:flex;align-items:center;gap:11px;padding:14px 16px 10px}
  .feed-card-avatar{width:42px;height:42px;border-radius:var(--r-1);display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:800;flex-shrink:0;letter-spacing:.02em;color:var(--ink-700);background:var(--ink-100)!important;background-image:none;border:1px solid var(--ink-100);box-shadow:none;overflow:hidden}
  .feed-card-avatar[style*="url("]{background-image:inherit}
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
 /* Badges are chrome, so they are ink — except the danger categories, where
    the colour carries the meaning (colour law §4.3). */
 .feed-badge.cat-snow,.feed-badge.cat-route,.feed-badge.cat-tour,
 .feed-badge.cat-info,.feed-badge.cat-wind_slab,.feed-badge.cat-whumpf{background:var(--ink-050);color:var(--ink-700)}
 .feed-badge.cat-snow svg,.feed-badge.cat-route svg,.feed-badge.cat-tour svg,
 .feed-badge.cat-info svg,.feed-badge.cat-wind_slab svg,.feed-badge.cat-whumpf svg{stroke:var(--ink-700)}
 .feed-badge.cat-danger,.feed-badge.cat-avalanche{background:var(--danger-tint);color:var(--danger)}
 .feed-badge.cat-danger svg,.feed-badge.cat-avalanche svg{stroke:var(--danger)}
 .feed-badge.cat-other{background:rgba(20,20,25,.1);color:var(--ink-900)}.feed-badge.cat-other svg{stroke:#141419}
 .feed-card-caption{font-size:14.5px;color:var(--fg2);line-height:1.55;margin-top:2px}
 .feed-card-caption b{color:var(--fg);font-weight:700}
 .feed-card-actions{display:flex;align-items:center;gap:20px;padding:12px 18px 13px;border-top:1px solid var(--hair);margin-top:10px}
 .feed-card-actions button{min-height:var(--tap);background:none;border:none;cursor:pointer;padding:4px;color:var(--fg2);display:flex;align-items:center;gap:5px;font-size:13px;font-weight:600;font-family:inherit}
 .feed-card-actions button svg{width:20px;height:20px}
 .feed-card-actions button:hover{color:var(--fg)}
 .feed-card-actions .flag-btn{margin-left:auto;color:var(--mut)}
 .feed-card-actions .flag-btn:hover{color:var(--danger)}
 .feed-card-actions .flag-btn.flagged{color:var(--danger)}
 .feed-card-actions .cond-btn.rated{color:var(--acc2)}
 .feed-card-actions .cond-btn.rated svg{color:var(--acc)}
 .cond-chip{font-size:12px;font-weight:800;color:var(--acc2);background:rgba(20,20,25,.1);padding:3px 10px;border-radius:999px;display:inline-flex;align-items:center;gap:4px}
 .cond-chip svg{width:13px;height:13px;fill:#C08A2E}
 /* condition rating modal */
 .cond-modal{position:fixed;inset:0;z-index:3950;background:rgba(14,17,22,.32);display:flex;flex-direction:column;justify-content:flex-end}
 @media(min-width:561px){.cond-modal{justify-content:center;align-items:center;padding:16px}}
 .cond-sheet{background:var(--glass2);border-radius:var(--r-xl) var(--r-xl) 0 0;box-shadow:var(--elev3);padding-bottom:calc(env(safe-area-inset-bottom,0px) + 18px)}
 @media(min-width:561px){.cond-sheet{border-radius:var(--r-xl);width:100%;max-width:380px}}
 .cond-sheet::before{content:'';display:block;width:38px;height:4px;border-radius:2px;background:rgba(22,21,46,.16);margin:8px auto 0}
 .cond-head{display:flex;align-items:center;justify-content:space-between;padding:8px 20px 0}
 .cond-head b{font-size:18px;font-weight:800;color:var(--fg);letter-spacing:-.01em}
 .cond-head button{background:none;border:none;font-size:19px;color:var(--mut);cursor:pointer}
 .cond-body{padding:2px 20px 4px}
 .cond-lbl{display:flex;align-items:center;gap:7px}
 .cond-lbl svg{width:16px;height:16px;flex-shrink:0;color:var(--fg2)}
 .cond-lblOFF{font-size:13px;font-weight:700;color:var(--fg2);margin:16px 0 8px}
 .cond-stars{display:flex;gap:8px;justify-content:center}
 .cond-stars button{background:none;border:none;cursor:pointer;padding:2px}
 .cond-stars button svg{width:40px;height:40px;fill:var(--fill2);stroke:none;transition:transform .12s var(--ease-spring),fill .12s}
 .cond-stars button.on svg{fill:#C08A2E}
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
 .qr-pad{position:relative;flex:1;aspect-ratio:1.18;border-radius:16px;border:1px solid var(--hair);background:linear-gradient(to right,rgba(13,148,136,.24),rgba(13,148,136,.06) 19%,rgba(217,119,6,.24) 21%,rgba(217,119,6,.06) 39%,rgba(2,132,199,.24) 41%,rgba(2,132,199,.06) 59%,rgba(124,111,208,.24) 61%,rgba(124,111,208,.06) 79%,rgba(30,58,138,.26) 81%,rgba(30,58,138,.08) 100%);cursor:crosshair;overflow:hidden}
 .qr-qline{position:absolute;top:0;bottom:0;width:0;border-left:2px dashed rgba(20,20,25,.30);pointer-events:none}
 .qr-xbands{display:flex;margin-top:8px;padding-left:24px}
 .qr-xband{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;text-align:center;font-size:9.8px;font-weight:800;line-height:1.16;letter-spacing:.01em}
 .qr-xband svg{width:22px;height:22px}
 .qr-xband:nth-child(1){color:var(--ink-700)}
 .qr-xband:nth-child(2){color:#d97706}
 .qr-xband:nth-child(3){color:#0284c7}
 .qr-xband:nth-child(4){color:#7c6fd0}
 .qr-xband:nth-child(5){color:#1e3a8a}
 .qr-grad{display:flex;align-items:center;justify-content:space-between;margin-top:7px;padding:0 2px 0 26px;font-size:9.5px;color:var(--mut);font-weight:600}
 .qr-grad b{font-weight:800;color:var(--fg2)}
 .qr-corner{position:absolute;font-size:10.5px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;color:var(--mut);pointer-events:none}
 .qr-corner.tr{top:8px;right:10px;color:var(--acc2)}
 .qr-corner.bl{bottom:8px;left:10px}
 .qr-pad-dot{position:absolute;width:24px;height:24px;margin:-12px 0 0 -12px;border-radius:50%;background:var(--acc);border:3.5px solid #fff;box-shadow:var(--elev2);pointer-events:none;transition:left .05s linear,top .05s linear;z-index:2}
 .qr-dot-cm{position:absolute;left:50%;bottom:calc(100% + 7px);transform:translateX(-50%);background:var(--ink-900);color:#fff;font-size:10.5px;font-weight:800;padding:2px 7px;border-radius:8px;white-space:nowrap;box-shadow:var(--elev1)}
 .qr-dot-cm::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:4px solid transparent;border-top-color:var(--ink-900)}
 .qr-pad-dot.below .qr-dot-cm{bottom:auto;top:calc(100% + 7px)}
 .qr-pad-dot.below .qr-dot-cm::after{top:auto;bottom:100%;border-top-color:transparent;border-bottom-color:var(--ink-900)}
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
 #mapFab[hidden],#mapQr[hidden],#mapDraw[hidden]{display:none!important}
 #mapFab{position:fixed;left:auto;right:14px;transform:none;bottom:calc(var(--btm-h,90px) + 14px);width:56px;height:56px;z-index:900;background:var(--ink-900)!important;color:var(--paper)!important;border-color:var(--ink-900)!important}
 #mapFab:active{transform:scale(.9)}
 #mapFab span{display:none}
 #mapQr{position:fixed;left:auto;right:14px;transform:none;bottom:calc(var(--btm-h,90px) + 94px);z-index:900}
 #mapQr:active{transform:scale(.9)}
 .feed-qr{position:absolute;bottom:calc(env(safe-area-inset-bottom, 0px) + 108px);left:auto;right:16px;transform:none;width:56px;height:56px;padding:0;border-radius:50%;border:1.5px solid rgba(10,10,12,.9);background:var(--card);color:var(--ink-900);display:flex;align-items:center;justify-content:center;cursor:pointer;font-family:inherit;box-shadow:0 10px 26px rgba(0,0,0,.22);z-index:5;transition:transform .18s cubic-bezier(.34,1.56,.64,1)}
 .feed-qr svg{width:24px;height:24px}
 .feed-qr span{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);font-size:10px;font-weight:800;letter-spacing:.03em;color:var(--fg2);text-shadow:0 1px 2px var(--paper);white-space:nowrap}
 .feed-qr:active{transform:scale(.92)}
 .qr-loc{font-size:12.5px;font-weight:650;color:var(--fg2);background:var(--fill);border:1px solid var(--hair);border-radius:10px;padding:8px 11px;margin-top:12px}
 .rail-btn.qr-accent{background:linear-gradient(150deg,rgba(245,166,35,.24),rgba(245,166,35,.1));color:var(--warn);border-color:rgba(245,166,35,.4)}
 .feed-quick{margin-left:auto;display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:999px;border:1px solid rgba(245,166,35,.45);background:linear-gradient(150deg,rgba(245,166,35,.2),rgba(245,166,35,.08));color:#8a5200;font-size:13px;font-weight:800;font-family:inherit;cursor:pointer;transition:.15s}
 .feed-quick svg{width:15px;height:15px}
 .feed-quick:active{transform:scale(.95)}
 .obs-quick{width:100%;display:flex;align-items:center;gap:12px;padding:13px 15px;margin:2px 0 4px;border-radius:16px;border:1px solid rgba(245,166,35,.4);background:linear-gradient(140deg,rgba(245,166,35,.18),var(--paper) 70%);font-family:inherit;cursor:pointer;text-align:left;box-shadow:var(--elev1);transition:.15s;animation:obsPop .5s var(--ease-spring) both}
 .obs-quick svg{width:24px;height:24px;color:var(--warn);flex-shrink:0}
 .obs-quick b{display:block;font-size:15px;font-weight:800;color:var(--fg)}
 .obs-quick em{font-style:normal;font-size:12px;color:var(--mut);font-weight:600}
 .obs-quick:hover{box-shadow:var(--elev2)}
 /* first-time explainer tooltip */
 .cond-hint{position:fixed;z-index:4200;max-width:236px;background:var(--fg);color:#fff;font-size:12.5px;font-weight:600;line-height:1.45;padding:11px 13px;border-radius:14px;box-shadow:var(--elev3);animation:toastIn .3s var(--ease-spring)}
 .cond-hint b{color:#7cc4ff}
 .cond-hint::after{content:'';position:absolute;bottom:-6px;left:50%;margin-left:-7px;width:14px;height:14px;background:var(--fg);transform:rotate(45deg);border-radius:2px}
 .cond-hint.below::after{bottom:auto;top:-6px}
 .feed-divider{height:1px;background:var(--ink-100)}
 .feed-card-visual{position:relative}
 .dbl-heart{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;animation:dblHeart .72s var(--ease) both}
 .dbl-heart svg{width:84px;height:84px;filter:drop-shadow(0 4px 16px rgba(0,0,0,.35))}
 @keyframes dblHeart{0%{opacity:0;transform:scale(.4)}25%{opacity:1;transform:scale(1.12)}55%{opacity:1;transform:scale(1)}100%{opacity:0;transform:scale(1.15)}}
 .feed-card-actions .save-btn.saved{color:var(--fg)}
 .us-list{max-height:52vh;overflow-y:auto;-webkit-overflow-scrolling:touch;margin-top:8px}
 .us-row{display:flex;align-items:center;gap:12px;padding:10px 4px;border-bottom:1px solid var(--hair);cursor:pointer}
 .us-row .av{width:42px;height:42px;border-radius:50%;background:var(--ink-900) center/cover;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:16px;flex-shrink:0}
 .us-row b{flex:1;font-size:15px;color:var(--fg)}
 .us-row .feed-follow{flex-shrink:0}
 .us-empty{color:var(--mut);font-size:13px;text-align:center;padding:18px 0}
 .feed-empty{text-align:center;padding:80px 20px;color:var(--mut);font-size:15px}
 /* Instagram-style centered create button at the very bottom of the feed */
 .feed-fab{position:absolute;bottom:calc(env(safe-area-inset-bottom, 0px) + 18px);left:auto;right:16px;transform:none;width:56px;height:56px;padding:0;border-radius:50%;border:none;background:linear-gradient(140deg,#3c3c42,#141419 55%,#000000);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;font-family:inherit;box-shadow:0 10px 28px rgba(20,20,25,.45),0 0 0 5px rgba(252,250,246,.75);z-index:5;transition:transform .18s cubic-bezier(.34,1.56,.64,1),box-shadow .18s}
 .feed-fab svg{width:24px;height:24px}
 .feed-fab span{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);font-size:10px;font-weight:800;letter-spacing:.03em;color:var(--fg2);text-shadow:0 1px 2px rgba(252,250,246,.8)}
 .feed-fab:active{transform:scale(.9)}
 .feed-fab:hover{box-shadow:0 12px 32px rgba(20,20,25,.55),0 0 0 5px var(--paper)}
 /* Text-only posts: first-class "quote" cards with a category tint wash */
 .feed-tx{position:relative;margin:2px 16px 0;padding:16px 18px 15px 22px;border-radius:16px;font-size:17px;line-height:1.5;font-weight:550;color:var(--fg);letter-spacing:-.012em;display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}
 .feed-tx-bar{position:absolute;left:8px;top:14px;bottom:14px;width:4px;border-radius:2px;opacity:.75}
 .feed-card.text .feed-card-actions{margin-top:8px}
 @media(min-width:561px){.feed-grid{padding:18px 0 120px}.feed-card{margin:0 0 20px}}
 /* --- Report markers --- */
 /* --- Map report markers: progressive disclosure by zoom --- */
 .rpt-ic{background:none!important;border:none!important}
 .rpt-cluster{position:relative;border-radius:50%;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 50% 34%,color-mix(in srgb,var(--cc) 58%,#fff),var(--cc));border:2.5px solid #fff;box-shadow:0 5px 16px rgba(0,0,0,.30);cursor:pointer;transition:transform .16s cubic-bezier(.34,1.56,.64,1)}
 .rpt-cluster:hover{transform:scale(1.09)}
 .rpt-cluster span{color:#fff;font-weight:800;font-size:14px;letter-spacing:-.02em;text-shadow:0 1px 2px rgba(0,0,0,.35)}
 .rpt-cluster .rc-ph{position:absolute;top:-4px;right:-4px;background:var(--card);color:var(--ink-900);font-size:9px;font-weight:800;min-width:16px;height:16px;border-radius:8px;display:flex;align-items:center;justify-content:center;gap:2px;padding:0 3px;box-shadow:0 1px 3px rgba(0,0,0,.3)}
 .rpt-cluster .rc-ph svg{width:8px;height:8px}
 /* Drawn-report marker: a real circle, crisp at any DPR, showing the snow
    type that dominates the drawing. */
 .rpt-snowmark{width:46px;height:46px;border-radius:50%;background:var(--paper);border:2.5px solid var(--paper);
   box-shadow:0 3px 12px rgba(14,17,22,.28);cursor:pointer;overflow:hidden;display:flex;
   transition:transform .16s var(--ease)}
 .rpt-snowmark:hover{transform:scale(1.1)}
 .rpt-snow{flex:1;border-radius:50%;display:block}
 .rpt-photo{position:relative;width:52px;height:52px;border-radius:50%;padding:3px;background:var(--cc);box-shadow:0 5px 16px rgba(0,0,0,.32);cursor:pointer;transition:transform .16s cubic-bezier(.34,1.56,.64,1)}
 .rpt-photo:hover{transform:scale(1.12)}
 .rpt-photo>img{width:100%;height:100%;object-fit:cover;border-radius:50%;display:block;border:2px solid #fff}
 .rpt-photo .rp-cat{position:absolute;bottom:-1px;right:-1px;width:21px;height:21px;border-radius:50%;border:2.5px solid #fff;display:flex;align-items:center;justify-content:center;color:#fff}
 .rpt-photo .rp-cat svg{width:11px;height:11px}
 .rpt-chip{display:inline-flex;align-items:center;gap:5px;padding:5px 11px 5px 8px;border-radius:999px;background:var(--card);border:1.5px solid var(--cc);color:var(--cc);font-weight:800;font-size:12.5px;white-space:nowrap;box-shadow:0 3px 11px rgba(0,0,0,.20);cursor:pointer;transition:transform .15s}
 .rpt-chip:hover{transform:scale(1.06)}
 .rpt-chip svg{width:14px;height:14px}
 .rpt-tiny{width:9px;height:9px;border-radius:50%;background:var(--cc);box-shadow:0 0 0 1.6px var(--paper),0 1px 3px rgba(0,0,0,.35);cursor:pointer}
 .rpt-pin{position:relative;width:34px;height:34px;border-radius:50%;background:var(--card);border:2.5px solid var(--cc);color:var(--cc);display:flex;align-items:center;justify-content:center;box-shadow:0 2px 9px rgba(0,0,0,.22);cursor:pointer;transition:transform .15s}
 .rpt-pin:hover{transform:scale(1.15)}
 .rpt-pin svg{width:17px;height:17px}
 .rpt-pin .rp-dot{position:absolute;top:-2px;right:-2px;width:11px;height:11px;border-radius:50%;background:var(--cc);border:2px solid #fff}
 /* clean report preview popup */
 .leaflet-popup.rpt-pop .leaflet-popup-content-wrapper{border-radius:18px;padding:0;overflow:hidden;box-shadow:0 18px 46px rgba(22,21,46,.30)}
 .leaflet-popup.rpt-pop .leaflet-popup-content{margin:0!important;width:234px!important}
 .rpop-img{position:relative;height:134px;cursor:pointer;background:var(--fill)}
 .rpop-img>img{width:100%;height:100%;object-fit:cover;display:block}
 .rpop-cat{display:inline-flex;align-items:center;gap:5px;padding:4px 9px;border-radius:999px;color:#fff;font-size:11.5px;font-weight:800}
 .rpop-img .rpop-cat{position:absolute;left:10px;bottom:10px;box-shadow:0 2px 8px rgba(0,0,0,.35)}
 .rpop-cat svg{width:12px;height:12px}
 .rpop-cat.sm{margin-left:auto;flex-shrink:0}
 .rpop-body{padding:12px 13px 13px}
 .rpop-head{display:flex;align-items:center;gap:9px;margin-bottom:9px}
 .rpop-av{width:34px;height:34px;border-radius:50%;background:var(--ink-900) center/cover;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;flex-shrink:0}
 .rpop-meta{display:flex;flex-direction:column;min-width:0}
 .rpop-meta b{font-size:14px;color:var(--fg);letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .rpop-meta span{font-size:11.5px;color:var(--mut)}
 .rpop-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:9px}
 .rpop-chip{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:999px;background:var(--fill);border:1px solid var(--hair);font-size:12px;font-weight:800;color:var(--fg2)}
 .rpop-chip.dep{background:color-mix(in srgb,var(--cc) 15%,#fff);border-color:color-mix(in srgb,var(--cc) 32%,#fff);color:var(--cc)}
 .rpop-chip svg{width:12px;height:12px}
 .rpop-cap{font-size:13px;line-height:1.45;color:var(--fg2);margin:0 0 11px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
 .rpop-cta{width:100%;padding:10px;border-radius:12px;border:none;background:var(--fg);color:#fff;font-size:13.5px;font-weight:800;font-family:inherit;cursor:pointer}
 .rpop-cta:active{transform:scale(.97)}
 @media(prefers-reduced-motion:reduce){.cat-chip,.sub-chip,.bucket,.slide-knob,.radial-seg{transition:none!important}}
</style></head><body>
<!-- No splash. The app is the first thing on screen; while the forecast data
     streams in, a hairline at the top edge carries the real progress. -->
<div id="boot"><i></i></div>
<div id="toastWrap" aria-live="polite"></div>
<div id="disc"><div class="sheet" role="dialog" aria-modal="true" aria-labelledby="discH"><div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></div><h2 id="discH">Bevor du startest</h2><p><b>Experimentelle Modelldaten.</b> Diese App zeigt modellierte Schnee-, Pulver- und Skitauglichkeits-Sch&auml;tzungen. Sie ist <b>kein Lawinenbulletin</b> und ersetzt nicht die offizielle Beurteilung des <a href="https://www.slf.ch/de/lawinenbulletin-und-schneesituation.html" target="_blank" rel="noopener">SLF</a> bzw. <a href="https://whiterisk.ch" target="_blank" rel="noopener">White Risk</a>. Entscheidungen im Gel&auml;nde triffst du auf <b>eigenes Risiko</b>.</p><p class="fine">Datenschutz: F&uuml;r Konto, Meldungen und Fotos werden E-Mail, Standort und Bilddaten bei Supabase (EU) gespeichert. Du kannst Konto und Beitr&auml;ge jederzeit l&ouml;schen.</p><label class="chk"><input type="checkbox" id="discChk" onchange="var b=document.getElementById('discBtn');if(b)b.disabled=!this.checked"><span>Ich habe verstanden, dass dies experimentelle Daten sind und kein Lawinenbulletin ersetzt.</span></label><button class="accept" id="discBtn" disabled onclick="acceptDisc()">Verstanden &ndash; loslegen</button></div></div>
<div id="a2hs" onclick="if(event.target===this)a2hsLater()"><div class="sheet" role="dialog" aria-modal="true">
  <div class="a2hs-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="3" x2="12" y2="21"/><line x1="4.2" y1="7.5" x2="19.8" y2="16.5"/><line x1="4.2" y1="16.5" x2="19.8" y2="7.5"/></svg></div>
  <h2>Firn aufs Home-Screen</h2>
  <p>Als App installiert läuft Firn im Vollbild (randlos), startet offline mit den letzten Daten und ist einen Fingertipp entfernt.</p>
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
<!-- ===================== Screen 1: the launcher ======================= -->
<div id="scrInitial">
  <div class="si-ridge" aria-hidden="true"></div>
  <div class="si-head">
    <svg class="si-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 19L10 6L13.5 12L15 9L21 19Z"/><path d="M8.6 11.4h2.8"/></svg>
    <h1>Firn</h1>
    <p>Schnee, Pulver und Verhältnisse in der Schweiz.</p>
  </div>
  <nav class="si-grid">
    <button class="si-card" onclick="scrGo('report')">
      <span class="si-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19l7-7 2.5 2.5-7 7L12 22l-2.5-.5z"/><path d="M15.5 6.5l2 2"/><circle cx="6" cy="7" r="3"/><path d="M6 10v7"/></svg></span>
      <b>Report</b><span>Verhältnisse melden</span>
    </button>
    <button class="si-card" onclick="scrGo('search')">
      <span class="si-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.2" y2="16.2"/></svg></span>
      <b>Search</b><span>Karte &amp; Ebenen</span>
    </button>
    <button class="si-card" onclick="scrGo('feed')">
      <span class="si-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/></svg></span>
      <b>Feed</b><span>Meldungen der Community</span>
    </button>
  </nav>
</div>
<!-- ===================== Pane: Search ================================== -->
<section class="pane" id="paneSearch" aria-label="Karte">
<div id="map"></div>
<canvas id="flow"></canvas>
<div id="modeGlow"></div>
<div id="statusScrim"></div>
<div id="statusScrimB"></div>
<div id="inspPanel" class="insp-panel"></div>
<button id="demoPill" onclick="demoToggle()" title="Demo-Modus umschalten"><span class="dp-dot"></span><span id="demoPillTxt">Demo</span></button>
<!-- The one brand mark on the map screen: quiet, non-interactive, the same
     twin-peak glyph as the app icon. Rebranding lives here and in the names
     the app uses when it talks about itself (title, share sheet, PWA
     install) -- not in new chrome competing with the map. -->
<button id="brandMark" class="pt-home" onclick="scrHome()" aria-label="Startseite">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 19L10 6L13.5 12L15 9L21 19Z"/><path d="M8.6 11.4h2.8"/></svg>
  <span>Firn</span>
</button>
<!-- The layers people actually reach for, at the top of the map. Everything
     else -- and which model a layer belongs to -- lives behind "Alle". -->
<div id="lbBar">
  <div id="lbQuick"></div>
  <button id="lbMore" onclick="lbSheetOpen()">Alle<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button>
</div>
<div id="lbScrim" onclick="lbSheetClose()"></div>
<section id="lbSheet" role="dialog" aria-modal="true" aria-label="Alle Ebenen">
  <div id="lbGrab"></div>
  <header><h2>Ebenen</h2><button onclick="lbSheetClose()" aria-label="Schliessen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button></header>
  <div id="lbList"></div>
</section>
<div id="searchWrap"><span class="icn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" style="width:15px;height:15px;display:block"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.2" y2="16.2"/></svg></span><input id="searchIn" type="text" placeholder="Ort suchen…" autocomplete="off"/><div id="searchRes"></div></div>
<div id="ctrlRail">
  <button class="rail-btn" id="accountBtn" onclick="accountTap()" title="Anmelden" aria-label="Konto"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="8" r="3.6"/><path d="M3.5 20a6.5 6.5 0 0 1 13 0"/><line x1="19" y1="6" x2="19" y2="12"/><line x1="16" y1="9" x2="22" y2="9"/></svg></button>
  <button class="rail-btn feed-accent" id="feedBtn" onclick="feedOpen()" title="Community-Feed" aria-label="Community-Feed"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg><span class="feed-dot"></span></button>
  <button class="rail-btn active" id="railToggles" onclick="toggleStations()" title="Messstationen ein/aus" aria-label="Messstationen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="21" x2="12" y2="10"/><path d="M8 21h8"/><circle cx="12" cy="7.5" r="2.5"/><path d="M7 4.5a7 7 0 0 1 10 0M9 7a4 4 0 0 1 6 0" stroke-dasharray="0"/></svg></button>
  <button class="rail-btn" id="btn3dFloat" title="3D-Ansicht" aria-label="3D-Ansicht"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2.5" y="5" width="19" height="14" rx="3.5"/><text x="12" y="15.6" text-anchor="middle" font-family="Inter,system-ui,sans-serif" font-size="8.6" font-weight="800" fill="currentColor" stroke="none">3D</text></svg></button>
  <button class="rail-btn" id="locBtn" onclick="flyToMe()" title="Zu meinem Standort" aria-label="Zu meinem Standort"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 2v3.5M12 18.5V22M2 12h3.5M18.5 12H22"/></svg></button>
</div>
<button class="feed-qr" id="mapQr" onclick="qrOpen(event)" title="Quick Powder Report" hidden><i class="fab-v">v2</i><svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 2L4.5 13.2c-.4.5 0 1.3.6 1.3H11l-1.4 7.2c-.1.7.8 1.1 1.2.5L20 11.5c.4-.5 0-1.3-.6-1.3H13l1.3-7.7c.1-.7-.8-1.1-1.3-.5z"/></svg><span>Powder</span></button>
<button class="feed-qr feed-draw" id="mapDraw" onclick="drawOpen()" title="Schnee-Karte zeichnen" hidden><i class="fab-v">v3</i><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19l7-7 2.5 2.5-7 7L12 22l-2.5-.5z"/><path d="M15.5 6.5l2 2"/><circle cx="6" cy="7" r="3"/><path d="M6 10v7"/></svg><span>Zeichnen</span></button>
<button class="feed-fab" id="mapFab" onclick="obsOpen()" title="Beobachtung melden" hidden><i class="fab-v">v1</i><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg><span>Melden</span></button>
<div id="bottomPanel" class="detail">
  <div id="tlToggle"></div>
  <!-- The console. One surface, sectioned by hairlines: every map layer of both
       models is visible and one tap away, the time controls sit underneath. -->
  <div id="layerBar">
    <div id="variants"></div>
    <div id="progBar"></div>
  </div>
  <div id="btmMain">
    <div id="tlHead">
      <span class="winlbl" id="window"></span>
      <div id="tlModeToggle">
        <button data-m="simple">Einfach</button>
        <button data-m="detail" class="active">Detail</button>
      </div>
    </div>
    <div class="seg" id="presets" style="gap:5px;margin-top:2px;overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;-webkit-overflow-scrolling:touch">
      <button id="btnSinceSnow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:14px;height:14px;vertical-align:-2px;margin-right:4px"><path d="M12 2v20M4.2 6.5l15.6 11M4.2 17.5l15.6-11"/></svg>Letzter Schnee</button>
      <button data-r="tomorrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:14px;height:14px;vertical-align:-2px;margin-right:4px"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg>Bis morgen</button>
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
</section>
<!-- ===================== Pane: Report ================================== -->
<section class="pane" id="paneReport" aria-label="Melden">
  <div class="pane-top">
    <button class="pt-home" onclick="scrHome()" aria-label="Startseite"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 19L10 6L13.5 12L15 9L21 19Z"/><path d="M8.6 11.4h2.8"/></svg><span>Firn</span></button>
    <button class="pt-acc" onclick="accountTap()" aria-label="Konto"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3.6"/><path d="M5.5 20a6.5 6.5 0 0 1 13 0"/></svg></button>
  </div>
  <div class="rp-body">
    <h2 class="rp-h">Was möchtest du melden?</h2>
    <div class="rp-choices">
      <button class="rp-choice" onclick="drawOpen()">
        <span class="rp-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19l7-7 2.5 2.5-7 7L12 22l-2.5-.5z"/><path d="M15.5 6.5l2 2"/><circle cx="6" cy="7" r="3"/><path d="M6 10v7"/></svg></span>
        <b>Schnee-Karte zeichnen</b>
        <span>Male die Verhältnisse direkt auf die Karte — daraus entsteht das Report-Modell.</span>
      </button>
      <button class="rp-choice" onclick="obsOpen()">
        <span class="rp-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>
        <b>Beobachtung melden</b>
        <span>Lawine, Wumm-Geräusch, Triebschnee oder Schneequalität.</span>
      </button>
    </div>
  </div>
</section>
<div id="three-wrap"><div id="map3d" style="width:100%;height:100%"></div><button id="btn3dClose">✕ 2D</button>
<div class="ctrl3d"><label style="display:flex;align-items:center;gap:8px;color:var(--fg2);font-size:16px">Relief <input id="mapOpac3d" type="range" min="0" max="100" value="30" style="width:100px;accent-color:var(--acc)"> Map <span id="mapOpacLbl">30%</span></label>
<button id="btn3dExag">×1.5</button>
<select id="overlay3d" style="padding:10px 14px;border-radius:12px;border:1px solid var(--bd);background:var(--glass);color:var(--fg2);font-size:16px;min-height:46px"><option value="none">No overlay</option><option value="snow">Snow</option><option value="temp">Temperature</option><option value="wind">Wind</option><option value="depth">Schneehöhe</option><option value="powder">Powder</option></select>
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
<p class="auth-note" id="authNote">Karte &amp; Prognosen funktionieren ohne Konto — du brauchst es nur zum Melden, für den Feed und dein Profil.</p>
<form id="authForm" onsubmit="authSubmit(event)">
<input type="text" id="authUser" placeholder="Username" autocomplete="username" style="display:none"/>
<input type="email" id="authEmail" placeholder="E-Mail" autocomplete="email" required/>
<input type="password" id="authPass" placeholder="Passwort" autocomplete="current-password" required minlength="8"/>
<div class="auth-err" id="authErr"></div>
<button class="auth-btn primary" type="submit" id="authSubmitBtn">Anmelden</button>
<button class="auth-forgot" type="button" id="authForgotBtn" onclick="authForgot()">Passwort vergessen?</button>
</form>
<div id="authCodeBox" style="display:none">
  <p class="auth-sub" id="authCodeSub">Wir haben dir einen 6-stelligen Code geschickt.</p>
  <input type="text" id="authCode" class="auth-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="––––––" oninput="authCodeInput()"/>
  <button class="auth-paste" type="button" onclick="authPasteCode()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Code aus E-Mail einfügen</button>
  <div class="auth-err" id="authCodeErr"></div>
  <button class="auth-btn primary" id="authCodeBtn" onclick="authVerifyCode()">Bestätigen</button>
  <div class="auth-switch">Kein Code erhalten? <button onclick="authResendCode()">Erneut senden</button> · <button onclick="authBackToForm()">Zurück</button></div>
</div>
<div id="authSentBox" style="display:none">
  <div class="auth-sent-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="4.5" width="19" height="15" rx="2.5"/><path d="M3 7l9 6 9-6"/></svg></div>
  <p class="auth-sub" style="text-align:center">Wir haben dir einen Link an <b id="authSentMail"></b> geschickt. Öffne die E-Mail und tippe auf <b>„Passwort zurücksetzen“</b> — danach kannst du hier direkt ein neues Passwort setzen.</p>
  <p class="auth-note" style="text-align:center">Der Link öffnet die App wieder. Nichts erhalten? Schau im Spam-Ordner nach.</p>
  <div class="auth-switch"><button onclick="authBackToForm()">Zurück zur Anmeldung</button></div>
</div>
<div id="authNewPassBox" style="display:none">
  <p class="auth-sub">Wähle ein neues Passwort (mindestens 8 Zeichen).</p>
  <input type="password" id="authNewPass" placeholder="Neues Passwort" autocomplete="new-password" minlength="8"/>
  <input type="password" id="authNewPass2" placeholder="Neues Passwort wiederholen" autocomplete="new-password" minlength="8"/>
  <div class="auth-err" id="authNewPassErr"></div>
  <button class="auth-btn primary" id="authNewPassBtn" onclick="authSetNewPassword()">Passwort speichern</button>
  <div class="auth-switch"><button onclick="authBackToForm()">Abbrechen</button></div>
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
    <div class="bio-lock-t">Firn</div>
    <div class="bio-lock-s">Entsperre mit <b id="bioLockName">Biometrie</b></div>
    <button class="auth-btn primary" onclick="haptic(6);bioUnlock()">Mit <span id="bioLockName2">Face ID</span> entsperren</button>
    <button class="bio-lock-out" onclick="bioSignOut()">Abmelden</button>
  </div>
</div>
<div id="drawWrap">
  <canvas id="drawCanvas"></canvas>
  <button id="drawClose" onclick="drawClose()" aria-label="Schliessen">✕</button>
  <button id="drawPan" onclick="drawTogglePan()" title="Karte bewegen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 9l-3 3 3 3M9 5l3-3 3 3M15 19l-3 3-3-3M19 9l3 3-3 3M2 12h20M12 2v20"/></svg><span>Karte bewegen</span></button>
  <div id="drawHint">Male die Zonen — oben rechts kannst du die Karte bewegen</div>
  <div id="drawBrushV" aria-label="Pinselgrösse">
    <span class="dbv-cap">Pinsel</span>
    <input type="range" min="12" max="80" step="2" value="34" orient="vertical" aria-label="Pinselgrösse"/>
    <b id="drawBrushVVal">34</b>
    <span class="dbv-dot"><i></i></span>
  </div>
  <div id="drawDepthV" aria-label="Schneehöhe">
    <span class="ddv-cap"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v20M4.2 6.5l15.6 11M4.2 17.5l15.6-11"/></svg></span>
    <input type="range" min="5" max="150" step="5" value="30" orient="vertical" aria-label="Schneehöhe in cm"/>
    <b id="drawDepthVVal">30</b><span class="ddv-unit">cm</span>
  </div>
  <div id="drawBar">
    <div id="drawBrush"></div>
    <div id="drawSlider"></div>
    <div id="drawCats"></div>
    <div id="drawPens"></div>
    <div id="drawActions">
      <button class="db" onclick="drawUndo()" title="Rückgängig" aria-label="Rückgängig"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/></svg></button>
      <button class="db" onclick="drawClear()" title="Löschen" aria-label="Löschen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg></button>
      <button id="drawPostBtn" onclick="drawOpenFinish()">Posten</button>
    </div>
  </div>
  <div id="drawFinish">
    <div class="dfin-sheet">
      <div class="dfin-grab"></div>
      <h3>Schnee-Karte melden</h3>
      <div class="dfin-snap"><img id="drawSnapImg" alt=""/><span class="dfin-tag">Karte + Zeichnung</span></div>
      <input type="file" id="drawFile" accept="image/*" hidden onchange="drawPhotoPick(this)"/>
      <button class="dfin-photo" id="drawPhotoBtn" onclick="document.getElementById('drawFile').click()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg> Foto hinzufügen (optional)</button>
      <img id="drawPhotoPrev" alt=""/>
      <textarea id="drawCaption" maxlength="500" placeholder="Beschreibung (optional) — Verhältnisse, Ort, Hinweise…"></textarea>
      <div class="dfin-actions">
        <button class="dfin-back" onclick="drawFinishClose()">Zurück</button>
        <button class="dfin-post" id="drawPublishBtn" onclick="drawPublish()">Veröffentlichen</button>
      </div>
    </div>
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
<div class="feed-page pane" id="feedPage">
<div class="feed-nav">
<button class="feed-back pt-home" onclick="scrHome()" aria-label="Startseite"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>
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
<button class="feed-qr" id="feedQr" onclick="qrOpen(event)" title="Quick Powder Report" hidden><svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 2L4.5 13.2c-.4.5 0 1.3.6 1.3H11l-1.4 7.2c-.1.7.8 1.1 1.2.5L20 11.5c.4-.5 0-1.3-.6-1.3H13l1.3-7.7c.1-.7-.8-1.1-1.3-.5z"/></svg><span>Powder</span></button>
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
    <div class="cond-head"><b><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:19px;height:19px;vertical-align:-3px;color:var(--warn)"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Quick Powder Report</b><button onclick="qrClose()">✕</button></div>
    <div class="cond-body">
      <div class="qr-loc" id="qrLoc"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;vertical-align:-2px;color:var(--acc2)"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg> <span id="qrLocTxt">Standort wird ermittelt…</span></div>
      <div class="qr-step" id="qrStep0">
        <div class="cond-lbl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>1 · Standort — Karte unter dem Pin verschieben</div>
        <div class="obs-loc-wrap"><div class="obs-loc-map" id="qrMap" style="height:180px"></div></div>
        <button class="cond-save" onclick="qrToStep(1)">Weiter <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;vertical-align:-3px"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
      </div>
      <div class="qr-step" id="qrStep1" style="display:none">
        <div class="cond-lbl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v20M4.2 6.5l15.6 11M4.2 17.5l15.6-11"/></svg>2 · Wie viel &amp; wie gut?</div>
        <div class="qr-padwrap">
          <div class="qr-ylab">wenig ← Menge (cm) → viel</div>
          <div class="qr-pad" id="qrPad">
            <div class="qr-qline" style="left:20%"></div>
            <div class="qr-qline" style="left:40%"></div>
            <div class="qr-qline" style="left:60%"></div>
            <div class="qr-qline" style="left:80%"></div>
            <div class="qr-cmline" style="top:16.7%"><i>50</i></div>
            <div class="qr-cmline" style="top:33.3%"><i>40</i></div>
            <div class="qr-cmline" style="top:50%"><i>30</i></div>
            <div class="qr-cmline" style="top:66.7%"><i>20</i></div>
            <div class="qr-cmline" style="top:83.3%"><i>10</i></div>
            <div class="qr-pad-dot" id="qrPadDot" style="display:none"><span class="qr-dot-cm" id="qrDotCm">0 cm</span></div>
          </div>
        </div>
        <div class="qr-xbands">
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9.6 4.6A2 2 0 1 1 11 8H2M12.6 19.4A2 2 0 1 0 14 16H2M17.6 7.6A2.5 2.5 0 1 1 19 12H2"/></svg><span>Wind-<br>deckel</span></div>
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8"/></svg><span>Sonnen-<br>deckel</span></div>
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.7s6.5 7.5 6.5 12a6.5 6.5 0 0 1-13 0c0-4.5 6.5-12 6.5-12z"/></svg><span>Durch-<br>nässt</span></div>
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 3v11M7 9.5l5 4 5-4"/><circle cx="12" cy="19" r="1.5" fill="currentColor" stroke="none"/></svg><span>Gealterter<br>Pulver</span></div>
          <div class="qr-xband"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 2v20M3.5 7l17 10M20.5 7l-17 10"/></svg><span>Pulver</span></div>
        </div>
        <div class="qr-grad"><b>◀ stark</b><span>Abstufung innerhalb jeder Kategorie</span><b>leicht ▶</b></div>
        <div class="qr-read" id="qrRead">Tippe oder ziehe auf dem Feld</div>
        <button class="cond-save" id="qrNextBtn" onclick="qrToStep(2)" disabled>Weiter <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;vertical-align:-3px"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
      </div>
      <div class="qr-step" id="qrStep2" style="display:none">
        <div class="cond-lbl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>3 · Foto (optional)</div>
        <label class="qr-photo-one" for="qrLib"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg><span>Foto aufnehmen oder wählen</span></label>
        <input id="qrLib" type="file" accept="image/*" hidden onchange="qrPhotoPick(this)">
        <img id="qrPrev" class="qr-prev" style="display:none" alt=""/>
        <button class="cond-save" id="qrSaveBtn" onclick="qrSubmit()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;vertical-align:-3px;margin-right:6px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>Posten</button>
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
        <div class="prof-sec-title">Meine Beiträge</div>
        <div class="uv-posts" id="profPosts"><div class="prof-hint">Lade …</div></div>
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
        <label class="prof-lbl">Anzeigename</label>
        <input class="prof-input" id="profDisplay" maxlength="32" placeholder="Wie du im Feed erscheinst" oninput="profDirty()"/>
        <label class="prof-lbl">Über dich <span class="prof-cnt" id="profBioCnt">0/200</span></label>
        <textarea class="prof-bio" id="profBio" maxlength="1400" placeholder="Kurze Beschreibung (max. 200 Wörter, keine Links)…" oninput="profBioInput()"></textarea>

        <div class="prof-sec-title">Darstellung</div>
        <label class="prof-lbl">Kartenbeschriftung</label>
        <div class="prof-seg" id="profLabels">
          <button data-v="on">Skigebiete an</button><button data-v="off">Aus</button>
        </div>

        <div class="prof-sec-title">Start &amp; Karte</div>
        <label class="prof-lbl">Startansicht</label>
        <div class="prof-seg" id="profStart">
          <button data-v="country">Schweiz</button><button data-v="last">Letzte Position</button><button data-v="home">Heimatgebiet</button>
        </div>
        <label class="prof-lbl">Heimatgebiet</label>
        <select class="prof-input" id="profHome" onchange="profDirty()"></select>
        <label class="prof-lbl">Ebene beim Öffnen</label>
        <select class="prof-input" id="profLayer" onchange="profDirty()"></select>
        <label class="prof-lbl">Standard-Zeitfenster</label>
        <div class="prof-seg" id="profWindow">
          <button data-v="24">24 h</button><button data-v="48">48 h</button><button data-v="72">72 h</button>
        </div>

        <button class="prof-save" id="profSave" onclick="profSave()">Speichern</button>
        <div class="prof-hint">Tippe oben auf dein Profilbild, um es zu ändern. Anzeigename und Beschreibung erscheinen auf deinem öffentlichen Profil; alles unter „Darstellung“ und „Start &amp; Karte“ bleibt auf diesem Gerät.</div>
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
    <div class="uv-hero"><button class="uv-close" onclick="userViewClose()" aria-label="Schliessen">✕</button><div class="prof-av uv-av" id="uvAv"><span id="uvInitial"></span></div></div>
    <div class="prof-body uv-body">
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
<button class="edge-tab left" id="edgeL" onclick="scrPrev()" aria-label="Vorheriger Screen"><span></span></button>
<button class="edge-tab right" id="edgeR" onclick="scrNext()" aria-label="Nächster Screen"><span></span></button>
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
const ASPECT_PNG="data:image/png;base64,"+db('ASPECTPNG'),ROUGH_PNG="data:image/png;base64,"+db('ROUGHPNG'),ELEV_PNG="data:image/png;base64,"+db('ELEVPNG');
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
function asp8(deg){if(deg==null||isNaN(deg))return null;return ['N','NO','O','SO','S','SW','W','NW'][Math.round(((deg%360)+360)%360/45)%8];}
const SWISS_PEAKS=[[45.976,7.658,'Matterhorn'],[45.937,7.867,'Dufourspitze'],[46.094,7.858,'Dom'],[46.101,7.717,'Weisshorn'],[46.033,7.612,'Dent Blanche'],[45.937,7.299,'Grand Combin'],[46.049,7.899,'Allalinhorn'],[46.057,7.861,'Alphubel'],[46.018,7.833,'Rimpfischhorn'],[45.941,7.719,'Breithorn'],[46.393,7.862,'Bietschhorn'],[46.161,8.028,'Fletschhorn'],[46.156,8.001,'Lagginhorn'],[46.128,8.011,'Weissmies'],[46.577,8.005,'Eiger'],[46.558,7.999,'Mönch'],[46.536,7.962,'Jungfrau'],[46.537,8.116,'Finsteraarhorn'],[46.463,8.021,'Aletschhorn'],[46.588,8.117,'Schreckhorn'],[46.651,8.107,'Wetterhorn'],[46.492,7.752,'Blüemlisalp'],[46.391,7.531,'Wildstrubel'],[46.358,7.371,'Wildhorn'],[46.474,7.720,'Doldenhorn'],[46.646,7.652,'Niesen'],[46.772,8.437,'Titlis'],[46.808,8.918,'Tödi'],[46.826,8.859,'Clariden'],[46.702,8.454,'Sustenhorn'],[46.642,8.421,'Dammastock'],[46.979,8.252,'Pilatus'],[47.048,8.485,'Rigi'],[47.249,9.343,'Säntis'],[47.158,9.311,'Churfirsten'],[46.995,9.000,'Glärnisch'],[46.900,9.267,'Ringelspitz'],[46.383,9.908,'Piz Bernina'],[46.377,9.965,'Piz Palü'],[46.616,9.874,'Piz Kesch'],[46.833,9.807,'Weissfluhgipfel'],[46.773,9.855,'Jakobshorn'],[46.842,10.119,'Piz Buin'],[46.845,10.062,'Piz Linard'],[46.472,9.720,'Piz Julier'],[46.408,9.820,'Piz Corvatsch'],[46.512,9.828,'Piz Nair'],[46.550,9.660,'Piz d\'Err'],[46.550,9.700,'Piz Ela'],[46.622,9.031,'Rheinwaldhorn'],[46.417,8.478,'Basòdino'],[46.421,8.720,'Campo Tencia'],[46.331,7.208,'Les Diablerets'],[46.161,6.920,'Dents du Midi'],[46.243,7.148,'Grand Muveran'],[46.548,7.020,'Moléson'],[46.512,7.150,'Vanil Noir'],[46.925,8.548,'Uri Rotstock'],[46.780,8.680,'Bristen'],[46.930,9.180,'Piz Sardona'],[46.749,9.949,'Flüela Wisshorn'],[46.575,9.671,'Tinzenhorn'],[47.192,9.271,'Alvier'],[46.803,9.930,'Piz Vadret'],[46.360,8.000,'Nufenen'],[46.557,8.561,'Titlis-Region']];
function nearestPeak(lat,lng){let best=null,bd=1e9;for(const pk of SWISS_PEAKS){const dLat=(lat-pk[0])*111,dLo=(lng-pk[1])*111*Math.cos(lat*Math.PI/180),d=Math.hypot(dLat,dLo);if(d<bd){bd=d;best=pk;}}return(best&&bd<7)?best[2]:null;}
// --- OCR-basierte Gipfelerkennung: liest den Bergnamen direkt von der Karte ---
let _ocrLoad=null,_ocrWorker=null,_ocrWorkerP=null;
function ensureTesseract(){
  if(window.Tesseract)return Promise.resolve(true);
  if(_ocrLoad)return _ocrLoad;
  _ocrLoad=new Promise(res=>{try{const sc=document.createElement('script');
    sc.src='https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js';sc.async=true;
    sc.onload=()=>res(!!window.Tesseract);sc.onerror=()=>res(false);
    setTimeout(()=>res(!!window.Tesseract),12000);document.head.appendChild(sc);}catch(e){res(false);}});
  return _ocrLoad;
}
async function ocrGetWorker(){
  if(_ocrWorker)return _ocrWorker;
  if(_ocrWorkerP)return _ocrWorkerP;
  _ocrWorkerP=(async()=>{const ok=await ensureTesseract();if(!ok||!window.Tesseract)return null;
    try{_ocrWorker=await Tesseract.createWorker('deu');}catch(e){try{_ocrWorker=await Tesseract.createWorker('eng');}catch(_){_ocrWorker=null;}}
    return _ocrWorker;})();
  return _ocrWorkerP;
}
async function ocrMapCrop(){
  let c;try{c=map.latLngToContainerPoint(map.getCenter());}catch(e){return null;}
  const CW=340,CH=250,SC=2.4,x0=c.x-CW/2,y0=c.y-CH/2;
  const cv=document.createElement('canvas');cv.width=Math.round(CW*SC);cv.height=Math.round(CH*SC);
  const g=cv.getContext('2d');g.fillStyle='#fff';g.fillRect(0,0,cv.width,cv.height);
  const mc=map.getContainer(),mr=mc.getBoundingClientRect();
  const tiles=[...mc.querySelectorAll('.leaflet-tile-pane img')].filter(im=>im.complete&&im.naturalWidth&&im.src&&im.src.indexOf('data:')!==0)
    .map(im=>{const r=im.getBoundingClientRect();return {src:im.src,x:r.left-mr.left,y:r.top-mr.top,w:r.width,h:r.height};});
  const loaded=await Promise.all(tiles.map(t=>new Promise(res=>{const ni=new Image();let to;const done=v=>{clearTimeout(to);res(v);};
    ni.crossOrigin='anonymous';ni.onload=()=>done(Object.assign(t,{img:ni}));ni.onerror=()=>done(null);to=setTimeout(()=>done(null),3000);ni.src=t.src;})));
  let drew=0;loaded.forEach(t=>{if(t&&t.img){try{g.drawImage(t.img,(t.x-x0)*SC,(t.y-y0)*SC,t.w*SC,t.h*SC);drew++;}catch(e){}}});
  if(!drew)return null;
  try{const id=g.getImageData(0,0,cv.width,cv.height),d=id.data;
    for(let i=0;i<d.length;i+=4){let v=0.3*d[i]+0.59*d[i+1]+0.11*d[i+2];v=(v-128)*1.55+128;v=v<0?0:v>255?255:v;d[i]=d[i+1]=d[i+2]=v;}
    g.putImageData(id,0,0);}catch(e){return null;}
  return cv;
}
async function ocrPeakFromMap(){
  try{const cv=await ocrMapCrop();if(!cv)return null;
    const w=await ocrGetWorker();if(!w)return null;
    const{data}=await w.recognize(cv);
    const cxc=cv.width/2,cyc=cv.height/2;let best=null,bs=-1e9;
    const bad=/^(Bergstation|Talstation|Hütte|Huette|Restaurant|Parkplatz|See|Stausee|Gletscher|Pass|Alp|Camping|Bahnhof)$/i;
    (data.lines||[]).forEach(ln=>{const txt=(ln.text||'').replace(/\s+/g,' ').trim();
      const m=txt.match(/([A-Za-zÀ-ſ][A-Za-zÀ-ſ'’.\- ]{2,}?)\s+(\d{3,4})(?!\d)/);
      if(!m)return;const nm=m[1].trim(),el=+m[2];
      if(el<700||el>4700||nm.length<3||bad.test(nm))return;
      const bb=ln.bbox||{x0:0,y0:0,x1:cv.width,y1:cv.height},bx=(bb.x0+bb.x1)/2,by=(bb.y0+bb.y1)/2;
      const dist=Math.hypot(bx-cxc,by-cyc)/cv.width;
      const score=((ln.confidence||50)/100)-dist*1.15;
      if(score>bs){bs=score;best=nm;}});
    return best;
  }catch(e){return null;}
}
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
// A canvas 2D context cannot resolve CSS custom properties: assigning
// `ctx.fillStyle=cvTok('--paper','#F7F4EE')` is silently ignored and the *previous* fill is
// reused, which shows up as a shape wearing some unrelated chart's colour.
// Anything drawn on a canvas resolves its tokens through here instead.
var _cvTok={};
function cvTok(n,fb){if(_cvTok[n])return _cvTok[n];
  var v='';try{v=getComputedStyle(document.documentElement).getPropertyValue(n).trim();}catch(e){}
  return (_cvTok[n]=v||fb);}
// The timeline is a canvas, so it cannot inherit CSS variables -- it reads them
// once per paint instead, which is what makes it follow the theme.
function tlPalette(){
  const cs=getComputedStyle(document.documentElement);
  const g=(v,f)=>{const q=cs.getPropertyValue(v).trim();return q||f;};
  return {ink:g('--ink-900','#0B1520'),mut:g('--ink-500','#5C728A'),
          hair:g('--ink-100','#DCE7F1'),fill:g('--ink-050','#EFF5FA'),
          paper:g('--paper','#fff'),accent:g('--accent','#0B6BCB')};
}
function drawTimeline(){const tc=document.getElementById('timeline');const rect=tc.getBoundingClientRect();
  if(!rect.width)return;
  const cw=rect.width,ch=rect.height||120,dpr=window.devicePixelRatio||1;
  if(tc.width!==Math.round(cw*dpr)||tc.height!==Math.round(ch*dpr)){tc.width=Math.round(cw*dpr);tc.height=Math.round(ch*dpr);}
  const ctx2=tc.getContext('2d');ctx2.setTransform(dpr,0,0,dpr,0,0);ctx2.clearRect(0,0,cw,ch);
  function rr(x,y,w,h,r){r=Math.max(0,Math.min(r,w/2,h/2));ctx2.beginPath();if(ctx2.roundRect){ctx2.roundRect(x,y,w,h,r);}else{ctx2.moveTo(x+r,y);ctx2.arcTo(x+w,y,x+w,y+h,r);ctx2.arcTo(x+w,y+h,x,y+h,r);ctx2.arcTo(x,y+h,x,y,r);ctx2.arcTo(x,y,x+w,y,r);ctx2.closePath();}}
  const _P=tlPalette();const nx=nowIdx/T*cw,x1=a/T*cw,x2=b/T*cw,baseY=ch-24;
  // soft selection band (rounded) — tinted with the active layer colour
  ctx2.fillStyle=tlSelTint;rr(x1,2,x2-x1,ch-4,10);ctx2.fill();
  // day gridlines + readable date labels
  ctx2.textAlign='left';let _lastLabX=-1e9;
  for(let t=0;t<T;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===0){const x=t/T*cw;
    ctx2.strokeStyle=_P.fill;ctx2.lineWidth=1;ctx2.beginPath();ctx2.moveTo(x,22);ctx2.lineTo(x,baseY);ctx2.stroke();
    // only label a day if it clears the previous label → no overlap on narrow phones
    if(x-_lastLabX>=48){ctx2.fillStyle=_P.mut;ctx2.font='700 11.5px Inter,system-ui';
      ctx2.fillText(['So','Mo','Di','Mi','Do','Fr','Sa'][d.getUTCDay()]+' '+d.getUTCDate()+'.',x+6,ch-7);_lastLabX=x;}}}
  // baseline
  ctx2.strokeStyle=_P.hair;ctx2.lineWidth=1;ctx2.beginPath();ctx2.moveTo(0,baseY+.5);ctx2.lineTo(cw,baseY+.5);ctx2.stroke();
  // snowfall bars — rounded tops, vertical gradient (snow stays blue)
  let mx=0;for(const s of hSnow)if(s>mx)mx=s;mx=Math.max(.05,mx);
  const barH=ch-48,bw=Math.max(2,cw/T);
  const gSel=ctx2.createLinearGradient(0,baseY-barH,0,baseY);gSel.addColorStop(0,_P.accent);gSel.addColorStop(1,_P.accent);
  const gSelPast=ctx2.createLinearGradient(0,baseY-barH,0,baseY);gSelPast.addColorStop(0,_P.hair);gSelPast.addColorStop(1,_P.mut);
  for(let t=0;t<T;t++){const v=hSnow[t];if(v<.002)continue;const h=Math.max(2,v/mx*barH);const x=t/T*cw;
    const inSel=(t>=a&&t<b),fut=t>=nowIdx;
    ctx2.fillStyle=inSel?(fut?gSel:gSelPast):(fut?'rgba(29,106,106,.18)':'rgba(122,114,102,.20)');
    rr(x+.5,baseY-h,Math.max(bw-1,1.4),h,Math.min(2.5,bw/2.2));ctx2.fill();}
  // selection frame + rounded grab handles (layer colour)
  ctx2.strokeStyle=tlSel;ctx2.globalAlpha=.65;ctx2.lineWidth=2;rr(x1+1,2,x2-x1-2,ch-4,10);ctx2.stroke();ctx2.globalAlpha=1;
  const hh=28,hy=(ch-hh)/2;ctx2.fillStyle=tlSel;
  rr(x1-3.5,hy,7,hh,3.5);ctx2.fill();rr(x2-3.5,hy,7,hh,3.5);ctx2.fill();
  ctx2.fillStyle=_P.paper;for(let i=-1;i<=1;i++){ctx2.fillRect(x1-1.25,hy+hh/2+i*5,2.5,2.4);ctx2.fillRect(x2-1.25,hy+hh/2+i*5,2.5,2.4);}
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
  ctx2.strokeStyle=_P.ink;ctx2.globalAlpha=.55;ctx2.lineWidth=1.5;ctx2.setLineDash([3,3]);ctx2.beginPath();ctx2.moveTo(nx,19);ctx2.lineTo(nx,baseY);ctx2.stroke();ctx2.setLineDash([]);ctx2.globalAlpha=1;
  const nlx=Math.max(19,Math.min(cw-19,nx));ctx2.fillStyle=_P.ink;rr(nlx-17,3,34,14,7);ctx2.fill();
  ctx2.fillStyle=_P.paper;ctx2.font='800 9.5px Inter,system-ui';ctx2.textAlign='center';ctx2.fillText('NOW',nlx,12.5);}
// Karte + Layer
const [laMin,loMin,laMax,loMax]=M.bounds;
const _desktop=window.innerWidth>560&&!('ontouchstart'in window);
const map=L.map('map',{zoomControl:false,zoomSnap:0,zoomDelta:.5,wheelPxPerZoomLevel:_desktop?38:90,wheelDebounceTime:_desktop?12:40,maxBoundsViscosity:1.0,inertia:true}).fitBounds([[laMin,loMin],[laMax,loMax]],{padding:[6,6]});
L.control.scale({imperial:false,maxWidth:120,position:'bottomright'}).addTo(map);
if(_desktop&&map.scrollWheelZoom&&map.scrollWheelZoom.setWheelPxPerZoomLevel)map.scrollWheelZoom.setWheelPxPerZoomLevel(38);
// Constrain panning + zoom to the meteo grid, with a thin white frame around the data
const _fitZoom=map.getZoom();
map.setZoom(_fitZoom,{animate:false});map.setMinZoom(_fitZoom);map.setMaxZoom(16);  // never zoom out past the initial view (sync — kein animierter Auto-Zoom)
const _padLa=(laMax-laMin)*0.04,_padLo=(loMax-loMin)*0.04;
map.setMaxBounds([[laMin-_padLa,loMin-_padLo],[laMax+_padLa,loMax+_padLo]]);
// ===== Base map: ONE canonical tile source ==================================
// Every raster basemap in the app (main map, 3D, the mini-maps in the report
// and pin flows) comes from this one swisstopo product through this one URL
// builder. The OpenStreetMap under-layer that used to sit beneath it is gone:
// it drew the same ground a second time, and the abstract border layer below
// now supplies the "outside Switzerland" context on its own.
function swissTile(id,ext){return "https://wmts.geo.admin.ch/1.0.0/"+id+"/default/current/3857/{z}/{x}/{y}."+(ext||"jpeg");}
const BASE_TILE='ch.swisstopo.pixelkarte-farbe-winter';
const BASE_ATTR="© swisstopo / MeteoSwiss / SLF / Copernicus";
function swissBaseLayer(opts){return L.tileLayer(swissTile(BASE_TILE),
  Object.assign({maxZoom:17,crossOrigin:true,attribution:BASE_ATTR},opts||{}));}
const base=swissBaseLayer().addTo(map);
// --- Abstract far-zoom base: white page + the Swiss border, nothing else -----
// Simplified national outline (Natural Earth 10 m, Douglas-Peucker 0.006 deg),
// delta-encoded in 1e-4 degree steps as lat,lon pairs. Baked in so the abstract
// view needs no network at all -- the swisstopo tiles fade in over it.
const CH_BORDER_ENC="468644,104538,-412,-89,-243,-277,-433,115,-201,-290,-347,-53,-284,-252,-336,264,-31,426,-121,209,-451,68,-408,-219,134,-1486,144,-195,98,-412,427,-9,88,-417,-224,-1043,-400,-166,-314,-384,-491,-47,-169,159,-207,-177,-136,158,-187,913,-111,74,-218,-76,-197,-282,-233,-126,-287,125,-470,540,-191,-131,-122,-279,-106,-752,396,-110,380,-541,580,-136,151,-456,-42,-630,-284,-873,123,-480,-85,-108,-307,-9,-199,-341,68,-1378,221,-339,361,-200,185,-382,1230,-98,-319,-500,191,-330,124,-8,36,-198,-41,-487,-123,-191,-486,-252,-198,229,-370,-5,-483,148,-1002,-504,-271,-435,-318,-181,-341,-726,-193,-185,-355,-18,-216,-111,-225,-571,-114,-43,-348,178,-287,-351,-377,303,-112,409,-165,114,-509,-287,-274,-320,141,-628,-84,-396,570,121,637,-411,314,-706,46,-325,833,665,233,-255,47,-618,151,-232,-137,-464,270,-757,851,-915,275,-722,404,-149,776,197,590,29,225,-42,138,-137,35,-287,-185,-829,-283,-297,-179,109,-174,-160,-160,-398,-449,-491,-237,-857,-137,-196,-182,-142,-180,268,-396,296,-366,28,-324,-217,-264,-437,-95,-416,-614,-146,-304,-249,-174,-876,-225,-253,-190,-27,-7,-206,-205,-59,-47,-125,142,-1372,376,-510,178,-1019,-274,-374,-108,-507,-381,-912,56,-751,-369,-1332,137,-866,431,-517,598,-275,555,-726,70,-227,-115,-232,55,-183,354,-23,273,206,103,-153,122,-796,168,-86,343,92,836,528,760,-773,330,317,357,50,151,-244,266,-1490,-73,-1308,-404,-852,-137,-961,-195,-325,-595,-550,-476,235,-80,147,32,237,-230,52,-484,-898,-415,-511,-116,-324,106,-340,-13,-456,-174,-695,403,241,291,-281,121,37,311,844,33,468,550,110,579,357,372,-282,229,-540,142,134,375,-34,805,815,187,-242,132,-31,969,1479,270,716,534,913,252,34,162,-155,555,297,249,-16,265,-174,351,148,423,1561,348,667,225,229,186,-120,159,134,256,563,172,-12,71,300,831,1135,339,679,454,24,389,780,110,77,276,-403,-139,-1374,284,174,234,405,189,16,103,424,170,225,369,-177,101,365,-88,441,59,498,-80,771,-284,-182,-163,57,-268,698,159,710,55,972,269,230,251,-150,-83,531,110,171,220,-76,24,242,157,41,93,-225,332,676,195,867,16,227,-251,-132,-68,-368,-204,737,117,833,393,530,-75,785,-272,140,0,1301,68,449,484,918,62,540,-62,556,-236,177,-112,478,33,948,218,129,158,606,-325,383,108,199,388,-3,36,133,-100,80,237,57,66,-391,-230,-1099,256,-670,108,64,232,-56,237,458,407,257,154,880,157,-93,62,159,-66,434,-323,60,-50,98,55,124,282,143,-29,129,-294,249,-13,312,-101,67,-238,-194,-289,167,5,528,62,-82,196,90,-9,269,-293,585,-29,-184,-70,1,-247,439,-18,637,246,712,-85,1668,-143,135,-60,763,-1156,2743,-236,75,-302,295,-286,658,-424,-8,-152,-97,-332,-388,-335,-136,-841,-828,-337,-172,-337,-24,-469,269,-214,5,-348,-365,-93,11,-115,836,38,1085,-407,1889,-114,-17,-53,143,-590,-82,-124,127,-366,1318,-252,387,-185,657,197,901,565,337,-6,606,187,5,229,175,198,252,114,401,-589,795,-169,53,-339,-124,-214,24";
const CH_BORDER=(function(){const v=CH_BORDER_ENC.split(",");const out=[];let la=0,lo=0;
  for(let i=0;i<v.length;i+=2){la+=+v[i];lo+=+v[i+1];out.push([la/1e4,lo/1e4]);}return out;})();
// Canton boundaries (Natural Earth admin-1, same simplification) and the major
// alpine resorts, as context UNDER the national border. Both are dimmed at the
// country view and sharpen as you zoom, mirroring how the terrain tiles fade.
const CH_CANTONS_ENC="459397,78496,-205,-59,-47,-125,159,-992,-47,-257,406,-633,178,-1019,-274,-374,-108,-507,-381,-912,56,-751,-329,-1030,0,-935,448,-681,678,-344,555,-726,70,-227,-115,-232,55,-183,354,-23,273,206,103,-153,122,-796,168,-86,343,92,836,528,760,-773,330,317,265,60,-23,785,-279,279,-267,0,-138,365,-267,91,-1177,1048,299,712,372,541,279,258,240,25,368,641,183,133,76,384,-197,34,-34,119,87,377,214,369,-29,1054,195,503,104,-189,170,568,208,266,-232,987,611,1218,43,570,301,642,79,68,164,-77,174,379,-102,1111,-144,592,-110,110,134,820,383,827,646,304,164,191,-16,191,-717,-61,-346,258,-148,395,-272,-293,-137,-516,-407,-101,-63,-425,-259,-484,-126,-84,-78,104,-192,-55,-243,-497,-449,-492,-237,-857,-137,-196,-182,-142,-180,269,-396,295,-366,28,-324,-217,-264,-437,-95,-416,-614,-146,-304,-249,-174,-876,-225,-253,-190,-27,-7,-206;461082,87290,-124,-515,270,-757,851,-915,275,-722,404,-149,776,197,590,29,225,-42,138,-137,15,-420,407,101,137,516,463,622,276,140,60,133,-133,992,124,416,-155,1105,211,1097,83,118,108,-39,121,88,119,382,-175,118,-99,573,-142,164,-572,-233,-193,62,-333,272,14,287,-149,115,-250,-22,-263,112,-222,-98,-302,11,-269,-189,-468,-121,-694,294,-476,763,-348,-732,-193,-185,-355,-18,-216,-111,-225,-571,-114,-43,-348,178,-287,-351,-377,303,-112,409,-165,114,-509,-287,-274,-320,141,-628,-84,-396,570,121,637,-411,314,-706,46,-325,833,665,233,-254,47,-619,138,-181;462312,92248,-271,-435,-311,-175,618,-874,268,1,374,-195,647,321,302,-11,222,98,263,-112,250,22,149,-115,-14,-287,333,-272,193,-62,627,200,163,-364,23,-340,173,-174,-117,-326,-262,-71,-190,-604,-71,-589,49,-772,106,-333,339,-45,156,-194,533,151,186,465,280,332,42,549,334,211,202,8,246,461,-148,535,190,780,158,215,386,243,-100,660,160,341,71,469,332,449,-134,193,-117,1436,-125,419,270,287,435,184,301,392,806,-550,-115,836,38,1085,-407,1889,-114,-17,-53,143,-590,-82,-124,127,-366,1318,-252,387,-185,657,197,901,565,337,-6,606,187,5,229,175,312,542,-23,165,-566,741,-169,53,-339,-124,-536,-29,-334,-313,-432,115,-201,-290,-347,-53,-284,-252,-336,264,-31,426,-121,209,-451,68,-408,-219,134,-1486,144,-195,98,-412,427,-9,88,-417,-224,-1043,-400,-166,-314,-384,-491,-47,-169,159,-207,-177,-136,158,-187,913,-111,74,-218,-76,-295,-358,-226,-56,-666,671,-191,-131,-233,-701,5,-330,396,-110,380,-541,580,-136,151,-456,-42,-630,-284,-873,123,-480,-85,-108,-307,-9,-199,-341,68,-1378,221,-339,361,-200,185,-382,1230,-98,-319,-500,191,-330,124,-8,36,-198,-41,-487,-364,-365,-245,-78,-198,229,-370,-5,-483,148,-1002,-504;478012,85582,-66,434,-323,60,-50,98,55,124,282,143,-29,129,-294,249,-13,312,-101,67,-238,-194,-289,167,5,528,61,-82,197,90,-9,269,-293,585,-29,-184,-165,142,105,-516,-263,-226,-83,-699,171,-262,312,16,8,-432,-381,-464,-8,-739,-170,-436,251,-848,108,64,232,-56,237,458,407,257,154,880,157,-93,62,159;475894,85607,56,156,-343,-204,147,-265,215,223,-75,90;476713,88519,-195,543,104,756,167,348,-85,1668,-143,135,-60,763,-1156,2743,-176,56,-205,-934,38,-137,120,25,45,-101,-353,-579,261,-375,179,14,100,-93,-147,-671,-49,318,-150,51,-174,-152,-60,-340,76,-566,226,-476,-149,-137,-48,-245,78,-857,-138,-272,-218,308,-196,-9,-70,-412,-456,-384,-79,-183,286,-399,278,189,97,-180,334,-191,126,112,361,30,159,-467,137,-98,84,65,94,-110,350,-882,94,151,-125,389,144,149,192,135,263,-167,-48,-165,192,118,-105,516;476326,86016,237,57,19,-137,381,464,-8,432,-312,-16,-171,262,-11,408,213,564,-263,167,-336,-284,125,-389,-94,-151,-350,882,-94,110,-174,-26,-239,552,-328,-56,-126,-112,-334,191,-97,180,-278,-189,-330,442,-291,238,-292,86,-233,-296,-192,-95,-243,-682,31,-542,-99,-184,-231,-138,-60,-433,-231,-489,-106,-82,-155,85,68,-519,451,-608,45,-392,-84,-575,132,-581,595,-335,98,324,369,307,-64,-296,125,-140,236,15,158,-327,321,327,634,-456,430,2,643,630,32,278,218,129,137,306,21,300,-98,154,-152,139,-215,-223,-147,265,395,247,388,-3,36,133,-100,80;476220,82511,-62,375,-280,276,-67,1049,-643,-630,-292,-55,-772,509,-321,-327,-158,327,-236,-15,-125,140,64,296,-369,-307,-98,-324,-406,249,-260,-144,-772,126,8,-291,137,-251,1113,-628,142,-280,-12,-169,-572,-602,60,-242,130,11,77,-562,106,-149,-182,-689,300,-223,44,-154,-20,-271,-288,-69,-148,-1094,372,-164,417,346,276,444,-100,797,62,153,447,322,189,-11,118,-310,388,-179,147,-367,202,-22,68,-397,549,-511,22,-203,-178,-77,-169,-387,198,-50,178,-504,128,583,321,341,-75,785,-272,140,0,1301,68,449,484,918,62,721;475966,76597,-320,-238,1,-262,-133,372,-217,-65,143,-287,-91,-228,285,-680,332,1388;475443,76834,61,439,-178,504,-198,50,169,387,178,77,-22,203,-549,511,-68,397,-202,22,-57,125,-171,-100,-183,88,-145,-248,-54,-469,-177,-115,-211,-652,-251,-192,277,-723,60,-686,343,-73,80,376,312,128,216,251,243,-491,-75,-498,-113,-64,-110,185,-221,-115,-114,-403,-149,12,-37,-377,-194,-220,75,-500,-185,-400,338,-421,83,709,175,-93,120,-252,142,87,251,-150,-33,317,-349,82,88,797,331,-49,49,-432,129,-102,56,255,157,41,93,-225,211,382,-285,680,91,228,-143,287,217,65,-71,365;474306,73785,27,97,-202,-40,188,-473,-13,416;475169,95531,-362,314,-286,658,-424,-8,-484,-485,-335,-136,-841,-828,-337,-172,-337,-24,-469,269,-214,5,-348,-365,-132,13,-767,548,-301,-392,-435,-184,-270,-287,125,-419,117,-1436,134,-193,376,-107,344,76,344,-191,118,-163,-73,-446,105,-256,189,371,616,106,141,-1052,384,-677,-58,-219,268,-332,182,34,67,-1799,231,138,99,184,-31,542,186,589,443,453,331,-55,335,-281,79,183,456,384,70,412,196,9,218,-308,138,272,-78,857,48,245,149,137,-226,476,-76,566,60,340,174,152,150,-51,49,-318,147,671,-100,93,-179,-14,-261,375,353,579,-45,101,-120,-25,-38,137,205,934;474819,74674,169,184,-49,432,-331,49,-88,-797,349,-82,-50,214;474333,73882,176,324,-258,352,-105,-154,-15,-562,202,40;473207,75590,4,85,167,0,301,-469,114,-835,165,156,-72,541,211,315,37,377,149,-12,114,403,221,115,110,-185,113,64,-7,352,86,66,-247,571,-216,-251,-312,-128,-80,-376,-216,17,-168,142,-19,600,-277,723,251,192,211,652,177,115,147,658,52,59,183,-88,171,100,-90,242,-388,179,-118,310,-189,11,-447,-322,-62,-153,100,-797,-276,-444,-417,-346,-75,-911,327,-455,-166,-1029,-304,195,-370,529,-257,194,-121,-25,-199,-353,125,-840,-354,-402,-202,-446,-179,68,-118,-107,-43,-184,226,-95,-18,-485,237,-118,13,535,286,144,148,466,263,-151,-296,-833,231,-112,98,-237,224,-89,229,750,70,-27,123,212,392,1005,227,162;474992,70098,-88,441,59,498,-80,771,-284,-182,-163,57,-268,698,151,988,-336,743,-190,151,-155,1027,-198,255,-201,-179,-32,224,-141,-624,60,-1094,121,-182,-300,-498,-84,-410,108,-1033,-183,-26,-111,-160,-202,-16,-52,-869,-329,-276,-250,-467,-9,-331,-292,-664,138,-559,432,502,339,679,454,24,499,857,276,-403,-139,-1374,284,174,234,405,189,16,189,573,84,76,369,-177,101,365;462412,60619,118,321,484,67,304,179,-318,556,95,424,-416,210,-80,147,32,237,-230,52,-484,-898,-415,-511,-116,-324,106,-340,-13,-456,-174,-695,403,241,291,-281,121,37,311,844,-19,190;463318,61186,386,165,261,-269,229,-540,142,134,375,-34,805,815,187,-242,132,-31,969,1479,707,1516,180,151,169,-4,162,-155,493,260,50,787,825,2037,-220,9,-208,193,-771,253,-188,270,-99,-45,-102,420,60,830,187,115,35,-188,470,187,115,-289,49,230,354,-489,183,323,-528,591,87,180,-318,33,-52,136,-252,-281,-241,-40,-298,-309,-47,-170,-113,223,-83,-37,-624,-638,-143,-40,-129,-630,-707,-22,24,529,-138,448,-322,-381,58,-385,-119,-85,-246,183,-13,265,177,304,-39,244,-285,555,-459,256,78,233,326,362,110,518,317,436,120,574,165,299,-175,114,-259,-13,-267,-186,-234,11,-254,-319,-280,85,-245,-113,-206,328,-327,-54,-225,-293,-240,-25,-279,-258,-372,-541,-299,-712,1177,-1048,267,-91,138,-365,267,0,279,-279,23,-785,191,-104,52,-150,266,-1490,-73,-1308,-404,-852,-137,-961,-654,-821,-196,-29,-95,-424,318,-556;469897,70211,-115,413,-409,108,-364,337,-59,-225,-292,-169,-134,-260,948,-956,425,752;468515,64431,201,53,375,-206,351,148,423,1561,348,667,225,229,186,-120,159,134,256,563,172,-12,71,300,399,633,-107,324,-191,173,-486,-105,337,1618,-138,7,-226,544,-58,-66,-246,35,-207,-473,-462,-227,-1444,-2551,293,-183,216,-20,208,-193,220,-9,-825,-2037,-50,-787;473440,75545,-62,130,-167,0,28,-309,201,179;467692,84462,-186,-96,-558,115,-61,-464,-330,77,-176,-299,-596,-253,-433,-878,-134,-820,110,-110,144,-592,102,-1111,-174,-379,-164,77,-79,-68,-301,-642,-43,-570,-611,-1218,232,-987,-208,-266,-170,-568,-104,189,-195,-503,29,-1054,-214,-369,-87,-377,34,-119,175,-6,22,-88,-90,-358,-169,-99,-143,-348,327,54,206,-328,245,113,280,-85,186,283,302,25,267,186,259,13,175,-114,247,405,68,361,520,6,87,110,-26,349,99,124,352,47,319,-700,255,-83,751,147,326,-42,47,408,306,0,150,-1498,234,101,345,-124,215,329,169,-113,-188,-825,-55,-803,115,-413,462,227,207,473,348,-8,182,-505,138,-7,-337,-1618,433,129,244,-197,77,-207,-94,520,278,586,9,331,177,361,402,382,52,869,202,16,111,160,183,26,-108,1033,84,410,300,498,-121,182,-60,1094,116,597,-202,-135,-145,-490,-370,-727,-70,27,-277,-776,-516,526,307,771,-316,117,-95,-432,-286,-144,-56,-551,-238,227,62,392,-226,95,104,284,265,-27,173,412,354,402,-125,840,232,379,345,-195,370,-529,304,-195,166,1029,-327,455,75,911,-220,34,-782,520,-405,88,-269,-130,-533,-8,-366,242,-88,550,-565,-244,-146,-169,-72,-355,-296,-5,-169,-143,-276,96,-726,1128,116,401,-21,520,-282,827,86,406,-88,813,307,882,-198,329,-5,451;471423,84056,-290,368,-163,-362,-348,-183,-91,164,-23,554,-278,421,-177,-36,-120,-331,180,-332,-127,-197,91,-462,-104,-369,6,-610,-96,-419,-167,9,-112,-723,-214,11,-336,-441,25,-236,-145,-147,-541,-270,-332,135,-178,-163,-95,-666,715,-1066,276,-96,169,143,296,5,72,355,146,169,565,244,88,-550,366,-242,533,8,269,130,484,-120,551,-358,148,1094,288,69,20,271,-44,154,-300,223,182,689,-106,149,-77,562,-130,-11,-60,242,572,602,12,169,-142,280,-1113,628,-137,251,-8,291;471133,84424,290,-368,772,-126,260,144,-189,86,-118,464,25,1084,-451,608,-155,703,-508,-534,-169,-608,36,-899,269,-175,-62,-379;469892,85910,-296,182,-140,782,-212,150,168,588,-80,563,-430,274,320,854,-195,153,-222,26,-346,-764,-305,-1,-246,-461,-202,-8,-334,-211,-42,-549,-280,-332,-145,-430,-517,-199,-213,207,-339,45,-124,-416,119,-1066,-322,-199,-191,-329,148,-395,346,-258,647,101,98,-123,330,-77,61,464,606,-104,138,85,68,324,276,-80,440,295,81,-275,490,-132,385,856,449,167,11,293;469461,86596,135,-504,296,-182,-76,-1015,117,-244,120,331,128,68,85,-69,242,-384,64,-664,137,-56,261,185,250,566,-25,175,-269,175,31,515,-85,265,130,617,565,644,87,-184,213,-62,279,548,59,1920,-66,312,-182,-34,-268,332,-684,-291,-202,14,-222,-179,-205,-448,-111,-48,-652,687,-317,-70,-394,-1067,430,-274,80,-563,-182,-529,226,-209,5,-278;469222,89303,74,213,317,70,652,-687,111,48,205,448,222,179,202,-14,684,291,58,219,-384,677,-141,1052,-616,-106,-189,-371,-105,256,73,446,-118,163,-344,191,-344,-76,-376,107,-332,-449,-71,-469,-160,-341,104,-396,-26,-330,-364,-177,-158,-215,-192,-874,112,-81,38,-360,241,-16,410,781,222,-26,195,-153;469933,84651,-117,244,65,722,-61,15,-421,-223,-352,-815,-490,132,-12,-486,130,-187,-93,-292,100,9,40,-130,-516,88,-374,497,-81,-309,424,-648,960,66,232,-417,306,159,121,-440,-78,-365,77,-48,162,287,18,781,104,369,-91,462,127,197,-20,142,-160,190;470042,72159,-186,206,-215,-329,-345,124,-234,-101,-150,1498,-306,0,-47,-408,-326,42,-751,-147,-255,83,-319,700,-352,-47,-99,-124,49,-306,-110,-153,-293,37,-269,-88,-26,-316,-412,-704,-120,-574,-317,-436,-110,-518,-326,-362,-78,-233,459,-256,285,-555,39,-244,-177,-304,13,-265,246,-183,119,85,-58,385,322,381,138,-448,-24,-529,707,22,129,630,143,40,624,638,83,37,113,-223,47,170,298,309,241,40,252,281,52,-136,318,-33,-87,-180,528,-591,217,385,-948,956,134,260,292,169,59,225,364,-337,409,-108,55,803,205,732;469412,72374,-77,-63,48,-86,29,149;469072,68751,-354,489,-49,-230,-115,289,-470,-187,-35,188,-187,-115,-60,-830,102,-420,99,45,116,-234,334,-86,619,1091;467519,67868,-190,50,-124,-240,199,-91,115,281;467734,68596,-215,289,-173,-107,-75,-149,91,-290,-99,-339,471,596;467895,83682,-307,-882,88,-813,-86,-406,253,-635,40,-509,178,163,332,-135,589,302,110,163,-38,188,336,441,214,-11,190,1088,-121,440,-306,-159,-232,417,-960,-66,-280,414;467697,84011,54,-95,81,309,374,-497,516,-88,-40,130,-100,-9,93,292,-130,187,-30,738,-479,-272,-276,80,-63,-775;473936,94866,-251,-52,-151,68,304,-1448,-198,-7,-311,-337,-294,-62,-504,287,-30,-231,310,-1118,94,117,288,-64,176,161,33,-189,175,-130,426,399,115,1834,222,163,341,966,-183,861,-86,-600,-168,331,-92,17,-180,-427,258,23,101,-189,-395,-373;474498,96084,-254,-269,128,-331,126,600;473534,94882,-570,-171,-249,-195,-245,-478,-73,-283,69,-308,483,-401,321,10,275,318,335,124,-339,1063,-7,321;473972,95405,-36,-539,113,30,98,244,113,-28,77,166,-107,150,-258,-23";
const CH_CANTONS=CH_CANTONS_ENC.split(";").map(function(r){
  const v=r.split(",");const out=[];let la=0,lo=0;
  for(let i=0;i<v.length;i+=2){la+=+v[i];lo+=+v[i+1];out.push([la/1e4,lo/1e4]);}return out;});
const CH_RESORTS=("Zermatt~460200~77486|Davos~468043~98372|St. Moritz~464994~98433|Verbier~461002~72265|Arosa~467779~96762|Grindelwald~466240~80360|Saas-Fee~461080~79274|Crans-Montana~463132~74791|Engelberg~468211~84013|Andermatt~466356~85939|Laax~468045~92579|Flims~468370~92846|Adelboden~464914~75603|Lenzerheide~467221~95590|Scuol~467967~102980|Gstaad~464721~72869|Champéry~461754~68690|Grächen~461953~78374|Pontresina~464955~99013|Disentis~467034~88509|Meiringen~467271~81872|Kandersteg~464947~76733|Leukerbad~463794~76269|Villars-sur-Ollon~462983~70563|Leysin~463418~70115|Airolo~465286~86119|Zweisimmen~465554~73730|Lauterbrunnen~465931~79094|Zuoz~466021~99596|Celerina~465122~98579|Amden~471489~91423|Saanen~464894~72600|Fiesch~463998~81353|Evolène~461142~74941|Beckenried~469665~84757|Wildhaus~472058~93540|Klosters Serneus~468892~98383|Charmey~466196~71649").split("|").map(function(e){
  const q=e.split("~");return {name:q[0],lat:+q[1]/1e4,lng:+q[2]/1e4};});
map.createPane('abstractPane');
map.getPane('abstractPane').style.zIndex=150;          // below tilePane (200)
map.getPane('abstractPane').style.pointerEvents='none';
map.createPane('resortPane');
map.getPane('resortPane').style.zIndex=460;            // above overlays, below markers
map.getPane('resortPane').style.pointerEvents='none';
const chCantons=L.polyline(CH_CANTONS,{pane:'abstractPane',interactive:false,smoothFactor:0.5,
  color:'#9fb0c4',weight:1,opacity:.18,fill:false}).addTo(map);
const chOutline=L.polygon(CH_BORDER,{pane:'abstractPane',interactive:false,smoothFactor:0.35,
  color:'#7c8ca3',weight:2.2,opacity:.95,fillColor:'#eef3f8',fillOpacity:1,lineJoin:'round'}).addTo(map);
const resortGroup=L.layerGroup([],{pane:'resortPane'}).addTo(map);
CH_RESORTS.forEach(function(r){
  L.marker([r.lat,r.lng],{pane:'resortPane',interactive:false,keyboard:false,
    icon:L.divIcon({className:'',html:'<div class="resort-pin"><i></i><span>'+r.name+'</span></div>',
      iconSize:[0,0],iconAnchor:[0,0]})}).addTo(resortGroup);});
// Tiles are transparent at the initial (fully zoomed-out) view and only reach
// full strength at the deepest zoom, so the map starts abstract and gains
// detail as you come closer.
const BASE_FLOOR=0.22;   // terrain never fully disappears, even zoomed right out
function baseFadeT(){
  const z0=map.getMinZoom(),z1=map.getMaxZoom();
  const t=(map.getZoom()-z0)/Math.max(.001,z1-z0);
  return Math.max(0,Math.min(1,t));
}
function updateBaseFade(){
  const t=baseFadeT();
  // The country view is abstract but no longer blank: the terrain starts at
  // BASE_FLOOR rather than 0, so you can already read where the mountains are.
  const op=BASE_FLOOR+(1-BASE_FLOOR)*Math.pow(t,1.45);
  base.setOpacity(op);
  chOutline.setStyle({weight:1.1+1.5*(1-op),opacity:.30+.65*(1-op),fillOpacity:Math.max(0,.72-op*.72)});
  // Canton borders are legible from the very first view and only sharpen.
  chCantons.setStyle({opacity:.42+.34*t,weight:.9+.9*t});
  // Ski resorts pop in as soon as you leave the country view.
  const rt=Math.max(0,Math.min(1,(t-0.06)/0.28));
  const el=document.getElementById('map');
  if(el){el.style.setProperty('--resort-op',rt.toFixed(3));
    el.style.setProperty('--resort-lbl',Math.max(0,Math.min(1,(t-0.14)/0.20)).toFixed(3));}
}
map.on('zoom zoomend',updateBaseFade);updateBaseFade();
// Keine weisse Maske mehr: die gedimmte OSM-Unterlage zeigt die Nachbarlaender,
// die Winter-Pixelkarte liegt fuer die Schweiz darueber.
const slopeWMTS=L.tileLayer(swissTile('ch.swisstopo.hangneigung-ueber_30','png'),{opacity:.7});
const reliefWMTS=L.tileLayer(swissTile('ch.swisstopo.swissalti3d-reliefschattierung_monodirektional','png'),{opacity:.85});
const roughImg=L.imageOverlay(ROUGH_PNG,[[M.png_bounds[0],M.png_bounds[1]],[M.png_bounds[2],M.png_bounds[3]]],{opacity:.78});
// Defer work until the browser has actually painted once. Two frames, because
// the first only commits the layout; the second is when pixels are on screen.
function _afterFirstPaint(fn){
  requestAnimationFrame(function(){requestAnimationFrame(function(){
    if(window.requestIdleCallback)requestIdleCallback(fn,{timeout:600});else setTimeout(fn,0);});});}
// Aspect: load high-res classified PNG into offscreen canvas, render via GridLayer with nearest-neighbor
const [aspS,aspWest,aspN,aspEast]=M.png_bounds;
let aspData=null,aspPW=0,aspPH=0;
const aspI=new Image();
// Decoding starts after the first frame: nothing on screen depends on it.
_afterFirstPaint(function(){aspI.src=ASPECT_PNG;});
aspI.onload=function(){aspPW=aspI.width;aspPH=aspI.height;const c=document.createElement('canvas');c.width=aspPW;c.height=aspPH;const x=c.getContext('2d',{willReadFrequently:true});x.drawImage(aspI,0,0);try{aspData=x.getImageData(0,0,aspPW,aspPH).data;}catch(e){}};
// --- Fine terrain sampling: 60 m aspect (8-way) + fine elevation raster ---
let elevData=null,elevPW=0,elevPH=0;
const elevI=new Image();
_afterFirstPaint(function(){elevI.src=ELEV_PNG;});
elevI.onload=function(){elevPW=elevI.width;elevPH=elevI.height;const c=document.createElement('canvas');c.width=elevPW;c.height=elevPH;const x=c.getContext('2d',{willReadFrequently:true});x.drawImage(elevI,0,0);try{elevData=x.getImageData(0,0,elevPW,elevPH).data;}catch(e){}
  try{if(demoFitZones()){loadReportMarkers();if(layer==='prog')renderPrognosis(stat);}}catch(e){}};
const ASP8DEG=[[0x4A,0x90,0xD9,0],[0x45,0xC0,0xC0,45],[0x66,0xBB,0x6A,90],[0xBE,0xDB,0x39,135],[0xEF,0x53,0x50,180],[0xFF,0x9E,0x42,225],[0xFF,0xC1,0x07,270],[0xA9,0x87,0xE0,315]];
function _pngSample(data,pw,ph,bnds,lat,lng){if(!data||!bnds)return null;const S=bnds[0],Wst=bnds[1],N=bnds[2],E=bnds[3];
  if(lat>N||lat<S||lng<Wst||lng>E)return null;
  const px=Math.floor((lng-Wst)/(E-Wst)*(pw-1)),py=Math.floor((N-lat)/(N-S)*(ph-1));
  if(px<0||px>=pw||py<0||py>=ph)return null;const i=(py*pw+px)*4;return [data[i],data[i+1],data[i+2],data[i+3]];}
// Bilinear sample of the fine terrain raster: f(px) -> value. Interpolating
// between pixel centres keeps point elevation/slope accurate at ~150 m spacing.
function _pngBilinear(data,pw,ph,bnds,lat,lng,f){
  if(!data||!bnds)return null;const S=bnds[0],Wst=bnds[1],N=bnds[2],E=bnds[3];
  if(lat>N||lat<S||lng<Wst||lng>E)return null;
  const fx=(lng-Wst)/(E-Wst)*(pw-1),fy=(N-lat)/(N-S)*(ph-1);
  const x0=Math.floor(fx),y0=Math.floor(fy),tx=fx-x0,ty=fy-y0;
  const x1=Math.min(pw-1,x0+1),y1=Math.min(ph-1,y0+1);
  if(x0<0||y0<0||x0>=pw||y0>=ph)return null;
  const at=(x,y)=>{const i=(y*pw+x)*4;if(data[i+3]<128)return null;return f(data[i],data[i+1],data[i+2]);};
  const v00=at(x0,y0),v10=at(x1,y0),v01=at(x0,y1),v11=at(x1,y1);
  if(v00==null||v10==null||v01==null||v11==null)return v00!=null?v00:(v10!=null?v10:(v01!=null?v01:v11));
  return (v00*(1-tx)+v10*tx)*(1-ty)+(v01*(1-tx)+v11*tx)*ty;
}
const ELEV_Q=M.elev_q||1;
function fineElev(lat,lng){const v=_pngBilinear(elevData,elevPW,elevPH,M.elev_bounds,lat,lng,(r,g,b)=>(r*256+g)*ELEV_Q);return(v!=null&&v>0)?v:null;}
function fineSlope(lat,lng){const v=_pngBilinear(elevData,elevPW,elevPH,M.elev_bounds,lat,lng,(r,g,b)=>b);return v!=null?v:null;}
function fineAspectDeg(lat,lng){const c=_pngSample(aspData,aspPW,aspPH,M.png_bounds,lat,lng);if(!c||c[3]<128)return null;
  const gd=Math.abs(c[0]-0x9E)+Math.abs(c[1]-0x9E)+Math.abs(c[2]-0x9E);let best=null,bd=1e9;
  for(const a of ASP8DEG){const dd=Math.abs(c[0]-a[0])+Math.abs(c[1]-a[1])+Math.abs(c[2]-a[2]);if(dd<bd){bd=dd;best=a;}}
  if(gd<=bd)return null;return best?best[3]:null;}
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
// ===== Schnee-Prognose: Aspekt + Höhen-Heuristik aus Zeichnen-Reports =====
const PROG_GW=340,PROG_GH=240;
const PROG_TYPE_ZOOM=11.5;   // below this only Reported Powder is offered
const PROG_MIN_CONF=75;      // Reported Powder is defined as "confidence > 75%"
const pcv=document.createElement('canvas');pcv.width=PROG_GW*2;pcv.height=PROG_GH*2;const pcx=pcv.getContext('2d');
let prognosisOverlay=L.imageOverlay(pcv.toDataURL(),[[laMin,loMin],[laMax,loMax]],{opacity:1});
let PROG_ASP=null,PROG_ELEV=null,PROG_SLP=null,PROG_LA=null,PROG_LO=null,PROG_Q=null;
// The grid used to be baked once over the WHOLE COUNTRY: 340x240 cells across
// 355 km is ~1 km per cell, so at the zoom people actually use (one slope on
// screen) the entire viewport was 1.2 cells wide -- a single cell either
// covered the screen or missed it, which is why the layer looked broken.
// It is now rebuilt for the visible area, so a cell is metres, not kilometres.
let PROG_BOUNDS=null;
let _progField=null,_progConf=null,_progType=null,_progModel=null,_progDiff=null;
const PROG_COL={powder:[74,163,255],drift:[232,89,12],wet:[245,158,11],suncrust:[244,208,63],windpressed:[150,168,196],firn:[63,178,127],scoured:[154,135,120]};
// Colourful, perceptually ordered ramp for the 0-100% confidence map.
const PROG_CONF_STOPS=[[0.00,48,18,59],[0.15,62,73,204],[0.30,33,144,231],[0.45,26,196,171],
                       [0.60,96,214,79],[0.72,212,225,53],[0.85,249,152,42],[1.00,231,49,38]];
function progConfColor(t){
  t=Math.max(0,Math.min(1,t));
  for(let i=1;i<PROG_CONF_STOPS.length;i++){
    const a=PROG_CONF_STOPS[i-1],b=PROG_CONF_STOPS[i];
    if(t<=b[0]){const f=(t-a[0])/Math.max(1e-6,b[0]-a[0]);
      return [Math.round(a[1]+(b[1]-a[1])*f),Math.round(a[2]+(b[2]-a[2])*f),Math.round(a[3]+(b[3]-a[3])*f)];}}
  const l=PROG_CONF_STOPS[PROG_CONF_STOPS.length-1];return [l[1],l[2],l[3]];
}
const PROG_LABEL={powder:'Powder',drift:'Triebschnee',wet:'Nassschnee',suncrust:'Sonnendeckel',windpressed:'Windgepresst',firn:'Firn',scoured:'Abgeweht'};
// The area the grid is built over: the current view, clamped to the data, with
// a little margin so a small pan does not immediately show an empty edge.
function progViewBounds(){
  let s0=laMin,n0=laMax,w0=loMin,e0=loMax;
  try{const b=map.getBounds(),dLa=(b.getNorth()-b.getSouth())*0.15,dLo=(b.getEast()-b.getWest())*0.15;
    s0=Math.max(laMin,b.getSouth()-dLa);n0=Math.min(laMax,b.getNorth()+dLa);
    w0=Math.max(loMin,b.getWest()-dLo);e0=Math.min(loMax,b.getEast()+dLo);
    if(!(n0>s0&&e0>w0)){s0=laMin;n0=laMax;w0=loMin;e0=loMax;}}catch(e){}
  return {s:s0,n:n0,w:w0,e:e0};
}
function progTerrain(force){
  const bb=progViewBounds();
  if(PROG_ASP&&PROG_BOUNDS&&!force){
    // rebuild only when the view really moved (a few % of the current span)
    const tl=(PROG_BOUNDS.n-PROG_BOUNDS.s)*0.04,to=(PROG_BOUNDS.e-PROG_BOUNDS.w)*0.04;
    if(Math.abs(bb.s-PROG_BOUNDS.s)<tl&&Math.abs(bb.n-PROG_BOUNDS.n)<tl&&
       Math.abs(bb.w-PROG_BOUNDS.w)<to&&Math.abs(bb.e-PROG_BOUNDS.e)<to)return;
  }
  PROG_BOUNDS=bb;
  const N=PROG_GW*PROG_GH;
  PROG_ASP=new Float32Array(N);PROG_ELEV=new Float32Array(N);PROG_SLP=new Float32Array(N);PROG_LA=new Float32Array(N);PROG_LO=new Float32Array(N);
  PROG_Q=new Int32Array(N);
  for(let gy=0;gy<PROG_GH;gy++)for(let gx=0;gx<PROG_GW;gx++){const idx=gy*PROG_GW+gx;
    const lat=bb.n-gy/(PROG_GH-1)*(bb.n-bb.s),lon=bb.w+gx/(PROG_GW-1)*(bb.e-bb.w);
    PROG_LA[idx]=lat;PROG_LO[idx]=lon;PROG_Q[idx]=-1;
    const cx=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy=Math.round((laMax-lat)/(laMax-laMin)*(H-1));
    if(cx<0||cx>=W||cy<0||cy>=H){PROG_ELEV[idx]=-1;continue;}
    const q=cy*W+cx;PROG_Q[idx]=q;const fa=fineAspectDeg(lat,lon),fe=fineElev(lat,lon),fs=fineSlope(lat,lon);
    PROG_ASP[idx]=(fa!=null?fa:maspv(q));PROG_ELEV[idx]=(fe!=null?fe:melevv(q));
    PROG_SLP[idx]=(fs!=null?fs:mslpv(q));}
}
// Every report layer paints into the same canvas and must anchor it to the area
// the grid was actually built for.
function progCommit(){
  try{prognosisOverlay.setBounds([[PROG_BOUNDS.s,PROG_BOUNDS.w],[PROG_BOUNDS.n,PROG_BOUNDS.e]]);}catch(e){}
  prognosisOverlay.setUrl(pcv.toDataURL());
}
// Only zones that can actually reach the visible area are worth scoring: the
// distance term is exp(-(d/11km)^2), so beyond ~3 scale lengths a zone
// contributes ~1e-4. Culling keeps a pan O(visible reports) instead of
// O(every report in Switzerland).
function progNearZones(zones){
  if(!PROG_BOUNDS)return zones;
  const pad=3*PROG_SC_KM;
  const dLa=pad/111,mid=(PROG_BOUNDS.s+PROG_BOUNDS.n)/2;
  const dLo=pad/(111*Math.max(0.2,Math.cos(mid*Math.PI/180)));
  return zones.filter(z=>z.lat>=PROG_BOUNDS.s-dLa&&z.lat<=PROG_BOUNDS.n+dLa&&
                         z.lng>=PROG_BOUNDS.w-dLo&&z.lng<=PROG_BOUNDS.e+dLo);
}
// Grid index for a lat/lon inside the current grid (or -1).
function progIdxAt(lat,lon){
  if(!PROG_BOUNDS)return -1;
  const gx=Math.round((lon-PROG_BOUNDS.w)/(PROG_BOUNDS.e-PROG_BOUNDS.w)*(PROG_GW-1));
  const gy=Math.round((PROG_BOUNDS.n-lat)/(PROG_BOUNDS.n-PROG_BOUNDS.s)*(PROG_GH-1));
  if(gx<0||gx>=PROG_GW||gy<0||gy>=PROG_GH)return -1;
  return gy*PROG_GW+gx;
}
// Report age in hours: DB rows carry created_at, demo rows a "vor 5h/3d" label.
function progAgeH(r){
  try{if(r.createdAt){const d=(Date.now()-new Date(r.createdAt).getTime())/3.6e6;if(isFinite(d)&&d>=0)return d;}}catch(e){}
  const t=String(r.time||'');if(/gerade/i.test(t))return 0;
  let m=t.match(/(\d+)\s*h/);if(m)return +m[1];
  m=t.match(/(\d+)\s*d/);if(m)return +m[1]*24;
  return 24;
}
let _demoFitted=false;
function demoFitZones(){
  if(_demoFitted||!elevData||!aspData||typeof DEMO_REPORTS==='undefined')return false;
  _demoFitted=true;let changed=false;
  DEMO_REPORTS.forEach(r=>{const cd=r.condition_data;
    if(!cd||!cd.demoFit||!cd.zones||!cd.zones.length)return;
    const e=fineElev(r.lat,r.lng),a=fineAspectDeg(r.lat,r.lng);
    cd.zones.forEach(z=>{
      if(e!=null){z.elevMin=Math.round((e-260)/50)*50;z.elevMax=Math.round((e+260)/50)*50;}
      if(a!=null)z.aspectDeg=a;});
    const z0=cd.zones[0];
    const parts=[cd.label0||'Schnee'];
    if(z0.elevMin!=null&&z0.elevMax!=null)parts.push(z0.elevMin+'\u2013'+z0.elevMax+' m');
    const a8=asp8(z0.aspectDeg);if(a8)parts.push(a8);
    cd.measurement=parts.join(' \u00b7 ');r.measurement=cd.measurement;changed=true;});
  return changed;
}
// Which report kinds feed the prognosis: drawn snow maps, quick powder
// reports, and the longer field/observation reports.
let progSrc='both';
const PROG_SRC_LABEL={draw:'Zeichnen',quick:'Quick',both:'Zeichnen + Quick',all:'Alle Reports'};
function progSetSrc(v){progSrc=v;progRenderBar();
  try{if(layer==='prog'||layer==='powfind'||layer==='progdiff'){showOverlay();legend();}}catch(e){}}
// map free text from a written report onto a snow type
function progTypeFromText(t){t=(t||'').toLowerCase();
  if(/trieb|windzeichen|schneebrett/.test(t))return 'drift';
  if(/nass|durchn|sulz/.test(t))return 'wet';
  if(/sonnendeckel|harsch.*sonn|bruchharsch/.test(t))return 'suncrust';
  if(/windgepresst|windharsch|winddeckel|gepresst/.test(t))return 'windpressed';
  if(/firn/.test(t))return 'firn';
  if(/pulver|powder|neuschnee/.test(t))return 'powder';
  return null;}
function progZoneFromPoint(r,type,cm,conc){
  const d=drawDemFull(r.lat,r.lng);
  const e=(d&&d.elev!=null)?Math.round(d.elev):null;
  return {type:type,lat:r.lat,lng:r.lng,
    e0:e!=null?e-250:null,e1:e!=null?e+250:null,
    asp:(d&&d.aspectDeg!=null)?d.aspectDeg:null,conc:conc,
    slp:(d&&d.slope!=null)?d.slope:null,slpSd:14,
    cm:cm,ageH:progAgeH(r)};
}
// --- Report credibility: confirmations on the post + author trust score -----
// "Bestätigen" writes a report_reactions row, so r.likes IS the number of
// confirmations. A user's trust score is the sum of confirmations across all
// of their reports — the same number the profile shows as "Trust Score".
// Both saturate, so no single popular post can dominate the map.
const PROG_END_HALF=3;    // confirmations for half of the endorsement bonus
const PROG_TRUST_HALF=8;  // trust points for half of the trust bonus
const PROG_END_MAX=0.60;  // max +60% from confirmations
const PROG_TRUST_MAX=0.50;// max +50% from a very trusted author
let _progTrust=null;
function progTrustMap(){
  if(_progTrust)return _progTrust;
  const m=new Map();
  (allReports||[]).forEach(r=>{if(!r.userId)return;
    m.set(r.userId,(m.get(r.userId)||0)+(r.likes||0));});
  _progTrust=m;return m;
}
function progTrustOf(uid){if(!uid)return 0;return progTrustMap().get(uid)||0;}
function progInvalidateTrust(){_progTrust=null;}
// Weight of one report, 1.0 = unconfirmed post by an unknown author.
function progReportWeight(r){
  const n=Math.max(0,r.likes||0),t=Math.max(0,progTrustOf(r.userId));
  const endW=1+PROG_END_MAX*(n/(n+PROG_END_HALF));
  const trW=1+PROG_TRUST_MAX*(t/(t+PROG_TRUST_HALF));
  return endW*trW;
}
function progZones(){
  demoFitZones();progInvalidateTrust();
  const useDraw=progSrc!=='quick',useQuick=progSrc!=='draw',useRep=(progSrc==='all');
  const out=[];(allReports||[]).forEach(r=>{const cd=r.condition_data;if(!cd)return;
    const w=progReportWeight(r);
    if(cd.zones&&cd.zones.length){
      if(!useDraw)return;
      cd.zones.forEach(z=>{if(!z||!z.type)return;const c=z.centroid||[r.lat,r.lng];if(c[0]==null)return;
        out.push({type:z.type,lat:c[0],lng:c[1],e0:z.elevMin,e1:z.elevMax,asp:z.aspectDeg,
          conc:z.aspectConc==null?0.6:z.aspectConc,slp:z.slope==null?null:z.slope,
          slpSd:z.slopeSd==null?null:z.slopeSd,cm:z.cm==null?null:z.cm,ageH:progAgeH(r),w:w});});
      return;}
    if(cd.quick){
      if(!useQuick)return;
      const q=cd.powderQuality,cm=cd.powderAmountCm==null?null:cd.powderAmountCm;
      let ty='powder';
      if(q!=null){ty=q<20?'windpressed':(q<40?'suncrust':(q<60?'wet':'powder'));}
      const z=progZoneFromPoint(r,ty,cm,0.4);z.w=w;out.push(z);return;}
    if(useRep){
      const ty=progTypeFromText((r.sub||'')+' '+(r.measurement||'')+' '+(r.caption||''));
      if(!ty)return;
      let cm=null;const m=/(\d{1,3})\s*cm/.exec(r.measurement||'');if(m)cm=+m[1];
      const z=progZoneFromPoint(r,ty,cm,0.35);z.w=w;out.push(z);}
  });
  return out;
}
// --- Terrain-similarity model (pure functions, unit-tested) ---------------
// Aspect: 8-sector logic — same sector matches fully, one sector over is a
// partial match, two sectors is weak, opposite slopes do not match at all.
const PROG_SC_KM=11;      // spatial decay scale
const PROG_ELEV_SD=180;   // elevation roll-off outside the reported band [m]
const PROG_AGE_H=72;      // report half-life-ish for recency weighting
const PROG_ASP_SD_MIN=30; // aspect tolerance of a tightly drawn report [deg]
const PROG_ASP_SD_MAX=85; // aspect tolerance of a diffuse one [deg]
const PROG_SLP_SD_MIN=9;  // slope tolerance [deg] -- steepness changes the snow
const PROG_SLP_FLOOR=0.30;// a very different steepness still is not zero evidence
function progAspectMatch(asp,zAsp,conc){
  if(zAsp==null||asp==null)return 0.6;
  let d=Math.abs(asp-zAsp)%360;if(d>180)d=360-d;
  const c=Math.max(0,Math.min(1,conc==null?0.7:conc));
  // Concentration controls HOW FAST the match falls off with angle -- it must
  // never discount a perfect match. (It used to: m*c + 0.45(1-c)^2 capped an
  // exactly-matching slope at 0.84 for a typical conc of 0.82, so a north slope
  // right next to a north-facing report could never read as near-certain.)
  // A tightly drawn report tolerates ~30 deg, a diffuse one ~85 deg.
  const sigma=PROG_ASP_SD_MIN+(PROG_ASP_SD_MAX-PROG_ASP_SD_MIN)*(1-c);
  let m=Math.exp(-(d*d)/(2*sigma*sigma));
  // Beyond two sectors only a genuinely diffuse report carries any signal.
  if(d>112.5)m*=(1-c);
  return m;
}
// Steepness matters as much as aspect: powder on a 35 deg north face says
// little about a 5 deg shoulder at the same aspect and height. The report's own
// slope spread widens the tolerance, and the floor keeps a mismatch from wiping
// the evidence out entirely.
function progSlopeMatch(slp,zSlp,zSd){
  if(zSlp==null||slp==null)return 1;
  const sd=Math.max(PROG_SLP_SD_MIN,zSd==null?PROG_SLP_SD_MIN:zSd);
  const d=slp-zSlp;
  const m=Math.exp(-(d*d)/(2*sd*sd));
  return PROG_SLP_FLOOR+(1-PROG_SLP_FLOOR)*m;
}
function progElevMatch(elev,e0,e1){
  if(e0==null||e1==null)return 0.7;
  if(elev>=e0&&elev<=e1)return 1;                       // inside the reported band
  const d=Math.max(e0-elev,elev-e1);
  return Math.exp(-(d*d)/(PROG_ELEV_SD*PROG_ELEV_SD));  // fades outside it
}
function progEnvelope(asp,elev,slp,z){
  if(elev<0)return 0;
  const em=progElevMatch(elev,z.e0,z.e1);
  // On genuinely flat ground aspect carries no information, so it is not held
  // against the cell; the slope term below is what separates flats from faces.
  const am=(slp!=null&&slp<12)?0.75:progAspectMatch(asp,z.asp,z.conc);
  const sm=progSlopeMatch(slp,z.slp,z.slpSd);
  return em*am*sm;
}
function progDistKm(lat,lon,z){const dLat=(lat-z.lat)*111,dLo=(lon-z.lng)*111*Math.cos(lat*Math.PI/180);return Math.hypot(dLat,dLo);}
function progRecency(z){const a=z.ageH==null?12:z.ageH;return 0.35+0.65*Math.exp(-a/PROG_AGE_H);}
// Combine supporting (sel) and conflicting (oth) reports for one location.
// like = how likely this snow type is here; conf = how much we trust it.
// Credibility (confirmations x author trust) deliberately does NOT scale the
// raw terrain match: how similar this slope is to the reported one is a
// property of the terrain, not of the reporter. It scales the *evidence*, so a
// confirmed report by a trusted user carries more weight when several reports
// overlap — it wins the agree/conflict tug-of-war and lifts confidence faster.
function progCell(asp,elev,slp,lat,lon,sel,oth){
  let sS=0,sO=0,best=0,sw=0,sw2=0,cmS=0,cmW=0;
  for(const z of sel){const e=progEnvelope(asp,elev,slp,z);if(e<=0.04)continue;
    const dd=progDistKm(lat,lon,z),m0=e*Math.exp(-(dd*dd)/(PROG_SC_KM*PROG_SC_KM))*progRecency(z);
    const m=m0*(z.w==null?1:z.w);
    sS+=m;sw+=m;sw2+=m*m;if(m0>best)best=m0;
    if(z.cm!=null){cmS+=z.cm*m;cmW+=m;}}
  for(const z of oth){const e=progEnvelope(asp,elev,slp,z);if(e<=0.04)continue;
    const dd=progDistKm(lat,lon,z);sO+=e*Math.exp(-(dd*dd)/(PROG_SC_KM*PROG_SC_KM))*progRecency(z)*(z.w==null?1:z.w);}
  const tot=sS+sO,agree=tot>0?sS/tot:0;
  // Standing on terrain that matches a nearby report IS close to an
  // observation, so evidence saturates quickly and a single good report can
  // reach ~95%; extra corroborating reports take it the rest of the way.
  const evid=1-Math.exp(-tot/0.22);
  const nEff=sw2>0?(sw*sw)/sw2:0;                        // effective independent reports
  const multi=0.97+0.03*Math.min(1,Math.max(0,nEff-1));
  const like=Math.min(1,best*(0.45+0.55*agree));
  return {like:like,conf:Math.round(100*agree*evid*multi),cm:cmW>0?(cmS/cmW):null};
}
function renderPrognosis(type){
  progTerrain();_progType=type;
  const zones=progNearZones(progZones()),sel=zones.filter(z=>z.type===type),oth=zones.filter(z=>z.type!==type);
  const N=PROG_GW*PROG_GH;
  _progField=new Float32Array(N);_progConf=new Float32Array(N);
  const col=PROG_COL[type]||[74,163,255];
  const img=new ImageData(PROG_GW,PROG_GH),d=img.data;
  for(let idx=0;idx<N;idx++){
    const asp=PROG_ASP[idx],elev=PROG_ELEV[idx],slp=PROG_SLP[idx],lat=PROG_LA[idx],lon=PROG_LO[idx];
    if(elev<0){d[idx*4+3]=0;continue;}
    const r=progCell(asp,elev,slp,lat,lon,sel,oth);const like=r.like;
    _progField[idx]=like;_progConf[idx]=r.conf;
    const o=idx*4;if(like<0.04){d[o+3]=0;continue;}
    // hue = confidence (0-100%), opacity = how likely this snow type is here
    const cc=progConfColor(r.conf/100),t=Math.pow(like,0.75);
    d[o]=cc[0];d[o+1]=cc[1];d[o+2]=cc[2];d[o+3]=Math.min(200,38+172*t)|0;
  }
  const tmp=document.createElement('canvas');tmp.width=PROG_GW;tmp.height=PROG_GH;tmp.getContext('2d').putImageData(img,0,0);
  pcx.clearRect(0,0,pcv.width,pcv.height);pcx.imageSmoothingEnabled=true;pcx.drawImage(tmp,0,0,pcv.width,pcv.height);
  progCommit();
}
// Powder-Finder: where powder is expected, painted in the SLF new-snow depth
// colours, filtered by a minimum confidence the user picks.
let progConfMin=PROG_MIN_CONF;
function progSetConfMin(v){progConfMin=+v;
  const o=document.getElementById('progConfVal');if(o)o.textContent=progConfMin+'%';
  try{if(layer==='powfind'){renderPowderFind();legend();}
     else if(layer==='progdiff'){renderProgDiff();legend();}
     else if(layer==='progpat'){renderProgPattern(stat);legend();}}catch(e){}}
// "Reported Powder": where drawn reports say powder, painted in the SLF snow
// depth colours. The depth shown is progCell's report-weighted mean, i.e. the
// nearby drawn reports averaged per aspect sector and elevation band.
function renderPowderFind(){
  progTerrain();_progType='powder';
  const _keep=progSrc;progSrc='draw';                 // drawn reports only, by definition
  const zones=progZones();progSrc=_keep;
  const near=progNearZones(zones);
  const sel=near.filter(z=>z.type==='powder'),oth=near.filter(z=>z.type!=='powder');
  const N=PROG_GW*PROG_GH;
  _progField=new Float32Array(N);_progConf=new Float32Array(N);
  const img=new ImageData(PROG_GW,PROG_GH),d=img.data;
  for(let idx=0;idx<N;idx++){
    const elev=PROG_ELEV[idx];const o=idx*4;
    if(elev<0){d[o+3]=0;continue;}
    const r=progCell(PROG_ASP[idx],elev,PROG_SLP[idx],PROG_LA[idx],PROG_LO[idx],sel,oth);
    _progField[idx]=r.like;_progConf[idx]=r.conf;
    if(r.conf<progConfMin||r.like<0.05){d[o+3]=0;continue;}
    const cm=r.cm!=null?r.cm:25;                       // depth drives the colour
    const c=snowCol(cm)||[120,170,255];
    d[o]=c[0];d[o+1]=c[1];d[o+2]=c[2];
    d[o+3]=Math.min(255,200+55*Math.pow(r.like,0.7))|0; // ~80% coverage and up, clearly readable
  }
  const tmp=document.createElement('canvas');tmp.width=PROG_GW;tmp.height=PROG_GH;tmp.getContext('2d').putImageData(img,0,0);
  pcx.clearRect(0,0,pcv.width,pcv.height);pcx.imageSmoothingEnabled=true;pcx.drawImage(tmp,0,0,pcv.width,pcv.height);
  progCommit();
}
// --- Abweichung: report-based powder prognosis vs. the model "Ski > Powder" --
// Both layers answer the same question from different evidence, so the
// interesting places are where they disagree. Model powder is a yes/no verdict
// per forecast cell, so it maps onto 0 / 0.55 (reduced) / 1 (stable).
const PROGDIFF_TOL=0.18;   // |rep-model| below this counts as agreement
function progModelPowder(q,cache){
  if(q<0)return 0;
  if(cache.has(q))return cache.get(q);
  let v=0;
  try{const r=computePowder(q,a,b);if(r&&r.powdered)v=(r.quality==='reduced')?0.55:1;}catch(e){}
  cache.set(q,v);return v;
}
function renderProgDiff(){
  progTerrain();_progType='powder';
  const zones=progNearZones(progZones()),sel=zones.filter(z=>z.type==='powder'),oth=zones.filter(z=>z.type!=='powder');
  const N=PROG_GW*PROG_GH;
  _progField=new Float32Array(N);_progConf=new Float32Array(N);_progModel=new Float32Array(N);_progDiff=new Float32Array(N);
  const cache=new Map();
  const img=new ImageData(PROG_GW,PROG_GH),d=img.data;
  for(let idx=0;idx<N;idx++){
    const elev=PROG_ELEV[idx];const o=idx*4;
    if(elev<0){d[o+3]=0;continue;}
    const mod=progModelPowder(PROG_Q[idx],cache);
    const r=progCell(PROG_ASP[idx],elev,PROG_SLP[idx],PROG_LA[idx],PROG_LO[idx],sel,oth);
    _progField[idx]=r.like;_progConf[idx]=r.conf;_progModel[idx]=mod;
    if(r.conf<progConfMin){                       // no report we trust enough here
      _progDiff[idx]=NaN;
      if(mod<=0){d[o+3]=0;continue;}
      d[o]=132;d[o+1]=146;d[o+2]=168;d[o+3]=(32+34*mod)|0;   // model alone, muted
      continue;}
    const diff=r.like-mod;_progDiff[idx]=diff;
    if(Math.abs(diff)<=PROGDIFF_TOL){
      if(Math.max(r.like,mod)<0.15){d[o+3]=0;continue;}       // both say "no powder"
      d[o]=42;d[o+1]=190;d[o+2]=112;d[o+3]=(78+95*Math.max(r.like,mod))|0; // agreement
      continue;}
    const t=Math.min(1,(Math.abs(diff)-PROGDIFF_TOL)/(1-PROGDIFF_TOL));
    if(diff<0){ // model claims powder, the reports do not -> orange to red
      d[o]=Math.round(250-19*t);d[o+1]=Math.round(168-119*t);d[o+2]=Math.round(60-16*t);}
    else{       // reports say powder, the model misses it -> cyan to violet
      d[o]=Math.round(34+92*t);d[o+1]=Math.round(206-130*t);d[o+2]=Math.round(214+33*t);}
    d[o+3]=(92+112*t)|0;
  }
  const tmp=document.createElement('canvas');tmp.width=PROG_GW;tmp.height=PROG_GH;tmp.getContext('2d').putImageData(img,0,0);
  pcx.clearRect(0,0,pcv.width,pcv.height);pcx.imageSmoothingEnabled=true;pcx.drawImage(tmp,0,0,pcv.width,pcv.height);
  progCommit();
}
// --- One pattern language for every snow type except Powder -----------------
// Powder is the only type that keeps a solid colour (blue, and on the map the
// SLF depth scale). Everything else is told apart by TEXTURE, so the types stay
// distinguishable without relying on colour at all — in the drawing tool, in
// the palette and in the map layers, all from this one table.
const SNOW_PATTERNS={
  drift:{label:'Triebschnee',hint:'Stipplung — windverfrachtet'},
  windpressed:{label:'Windgepresst',hint:'==== tragfähiger Deckel'},
  suncrust:{label:'Sonnendeckel',hint:'raue Schraffur — Bruchharsch'},
  wet:{label:'Nassschnee',hint:'Wellenlinien'},
  firn:{label:'Firn',hint:'Kreuzraster'},
  scoured:{label:'Abgeweht',hint:'lockere Strichlinien'},
  compact:{label:'Kompakt',hint:'feine Linien'},
  icy:{label:'Eisig',hint:'harte Doppeldiagonalen'},
  nosnow:{label:'Kein Schnee',hint:'offene Kreuze'}
};
const PROG_PATTERNS=SNOW_PATTERNS;   // the map layers use the same textures
// ink = the texture colour, bg = optional backing (the drawing pens want one so
// the painted area reads as a filled zone; the map layers do not).
function snowPatternTile(type,ink,bg){
  const S=14,c=document.createElement('canvas');c.width=S*2;c.height=S*2;
  const x=c.getContext('2d');x.scale(2,2);
  if(bg){x.fillStyle=bg;x.fillRect(0,0,S,S);}
  x.strokeStyle=ink||'#14161a';x.fillStyle=ink||'#14161a';x.lineWidth=1.4;x.lineCap='butt';
  if(type==='windpressed'){                                  // ==== double rules
    [3.5,6.5,10.5,13.5].forEach(y=>{x.beginPath();x.moveTo(0,y);x.lineTo(S,y);x.stroke();});
  }else if(type==='drift'){                                  // stipple: wind-blown grains
    [[3,3],[10,4],[6,7.5],[12.5,9.5],[2.5,11],[8.5,12.5]].forEach(q=>{
      x.beginPath();x.arc(q[0],q[1],1.35,0,6.2832);x.fill();});
  }else if(type==='suncrust'){                               // rough, broken hatching
    x.lineWidth=1.6;
    [[0,4,6,1],[8,2,14,6],[1,9,7,12],[9,11,14,8],[4,13,10,14.5]].forEach(q=>{
      x.beginPath();x.moveTo(q[0],q[1]);x.lineTo(q[2],q[3]);x.stroke();});
  }else if(type==='wet'){                                    // wave lines
    [4,11].forEach(y=>{x.beginPath();x.moveTo(0,y);
      for(let q=0;q<=S;q+=2)x.lineTo(q,y+((q/2)%2?2:-2));x.stroke();});
  }else if(type==='firn'){                                   // cross hatch
    for(let k=-S;k<S*2;k+=6){x.beginPath();x.moveTo(k,0);x.lineTo(k+S,S);x.stroke();
      x.beginPath();x.moveTo(k,S);x.lineTo(k+S,0);x.stroke();}
  }else if(type==='compact'){                                // fine dense rules
    x.lineWidth=1;[2.5,6,9.5,13].forEach(y=>{x.beginPath();x.moveTo(0,y);x.lineTo(S,y);x.stroke();});
  }else if(type==='icy'){                                    // hard double diagonals
    x.lineWidth=1.7;
    for(let k=-S;k<S*2;k+=7){[0,2.4].forEach(o=>{x.beginPath();x.moveTo(k+o,0);x.lineTo(k+o+S,S);x.stroke();});}
  }else if(type==='nosnow'){                                 // open crosses
    x.lineWidth=1.3;[[3.5,3.5],[10.5,10.5]].forEach(q=>{
      x.beginPath();x.moveTo(q[0]-2.5,q[1]-2.5);x.lineTo(q[0]+2.5,q[1]+2.5);
      x.moveTo(q[0]+2.5,q[1]-2.5);x.lineTo(q[0]-2.5,q[1]+2.5);x.stroke();});
  }else{                                                     // scoured: loose dashes
    x.setLineDash([5,5]);[3.5,7,10.5,14].forEach(y=>{x.beginPath();x.moveTo(0,y);x.lineTo(S,y);x.stroke();});
  }
  return c;
}
function progPatternTile(type){return snowPatternTile(type,'#14161a',null);}
// CSS background-image for a palette swatch — same geometry, drawn small.
function snowPatternURL(type){
  const c=snowPatternTile(type,'#20242b','#f2f5f9');
  try{return c.toDataURL();}catch(e){return '';}
}
const _progPatCache={};
function renderProgPattern(type){
  progTerrain();_progType=type;
  const _keep=progSrc;progSrc='draw';                 // drawn reports only
  const zones=progNearZones(progZones());progSrc=_keep;
  const sel=zones.filter(z=>z.type===type),oth=zones.filter(z=>z.type!==type);
  const N=PROG_GW*PROG_GH;
  _progField=new Float32Array(N);_progConf=new Float32Array(N);
  const img=new ImageData(PROG_GW,PROG_GH),d=img.data;
  for(let idx=0;idx<N;idx++){
    const elev=PROG_ELEV[idx];const o=idx*4;
    if(elev<0){d[o+3]=0;continue;}
    const r=progCell(PROG_ASP[idx],elev,PROG_SLP[idx],PROG_LA[idx],PROG_LO[idx],sel,oth);
    _progField[idx]=r.like;_progConf[idx]=r.conf;
    if(r.conf<progConfMin||r.like<0.05){d[o+3]=0;continue;}
    d[o]=20;d[o+1]=22;d[o+2]=26;                       // the mask is black
    d[o+3]=Math.min(255,150+105*Math.pow(r.like,0.7))|0;
  }
  const tmp=document.createElement('canvas');tmp.width=PROG_GW;tmp.height=PROG_GH;
  tmp.getContext('2d').putImageData(img,0,0);
  pcx.clearRect(0,0,pcv.width,pcv.height);
  pcx.globalCompositeOperation='source-over';pcx.imageSmoothingEnabled=true;
  pcx.drawImage(tmp,0,0,pcv.width,pcv.height);
  // Clip the texture to the mask, so the pattern only shows inside the zone.
  const tile=_progPatCache[type]||(_progPatCache[type]=progPatternTile(type));
  pcx.globalCompositeOperation='source-in';
  pcx.fillStyle=pcx.createPattern(tile,'repeat');
  pcx.fillRect(0,0,pcv.width,pcv.height);
  pcx.globalCompositeOperation='source-over';
  progCommit();
}
function progDiffAt(lat,lon){
  if(!_progDiff)return null;
  const idx=progIdxAt(lat,lon);if(idx<0)return null;
  return {rep:Math.round(_progField[idx]*100),model:Math.round(_progModel[idx]*100),
          conf:_progConf[idx],diff:isNaN(_progDiff[idx])?null:Math.round(_progDiff[idx]*100)};
}
function prognosisAt(lat,lon){
  if(!_progField)return null;
  const idx=progIdxAt(lat,lon);if(idx<0)return null;
  return {type:_progType,like:Math.round(_progField[idx]*100),conf:_progConf[idx]};
}
const windArr=L.layerGroup(); const stnGroup=L.layerGroup().addTo(map);
let layer="snow",stat="avg",windowSize=48,a=nowIdx,b=Math.min(T,nowIdx+48),showStn=true,wtimer=null;
const isMobile=window.innerWidth<=560;

function depthCol(v){const x=Math.min(1,v/300);let r,g,bl;if(x<.33){const k=x/.33;r=220-k*120|0;g=240-k*50|0;bl=255-k*5|0;}else if(x<.66){const k=(x-.33)/.33;r=100-k*80|0;g=190-k*90|0;bl=250-k*65|0;}else{const k=(x-.66)/.34;r=20+k*100|0;g=100-k*85|0;bl=185-k*105|0;}return[r,g,bl];}
// --- Domain edge mask -----------------------------------------------------
// A gridded forecast is unreliable in the cells at its own boundary: they have
// no upwind neighbours, so advection and the wind field are one-sided there.
// The outermost EDGE_KM are therefore left blank rather than drawn as if they
// were data. Computed once from the grid geometry, in cells, per axis.
const EDGE_KM=3;
const _kmPerRow=((laMax-laMin)*110.574)/Math.max(1,H-1);
const _kmPerCol=((loMax-loMin)*111.320*Math.cos((laMin+laMax)/2*Math.PI/180))/Math.max(1,W-1);
const EDGE_ROWS=Math.min(Math.floor((H-1)/2),Math.ceil(EDGE_KM/Math.max(1e-6,_kmPerRow)));
const EDGE_COLS=Math.min(Math.floor((W-1)/2),Math.ceil(EDGE_KM/Math.max(1e-6,_kmPerCol)));
function inDomainEdge(p){const y=(p/W)|0,x=p-y*W;
  return x<EDGE_COLS||x>=W-EDGE_COLS||y<EDGE_ROWS||y>=H-EDGE_ROWS;}
function setRaster(get,border){const img=cx.createImageData(W,H),d=img.data;const cls=border?new Int16Array(NP):null;
  for(let p=0;p<NP;p++){const r=inDomainEdge(p)?null:get(p);const o=p*4;if(r){d[o]=r[0];d[o+1]=r[1];d[o+2]=r[2];d[o+3]=r[3]==null?210:r[3];if(cls)cls[p]=r[4];}else{d[o+3]=0;if(cls)cls[p]=-999;}}
  if(border){for(let y=0;y<H;y++)for(let x=0;x<W;x++){const p=y*W+x;if(cls[p]==-999)continue;const rt=x<W-1?cls[p+1]:cls[p],bt=y<H-1?cls[p+W]:cls[p];if(rt!=cls[p]||bt!=cls[p]){const o=p*4;d[o]=20;d[o+1]=20;d[o+2]=30;d[o+3]=230;}}}
  cx.putImageData(img,0,0);raster.setUrl(cv.toDataURL());}
function aggT(p,m){let mn=1e9,mx=-1e9,su=0,c=0,cold=0;for(let t=a;t<b;t++){const v=tv(t,p);mn=Math.min(mn,v);mx=Math.max(mx,v);su+=v;c++;if(v<0)cold++;}return m=="max"?mx:m=="min"?mn:m=="sub0"?cold:m=="max05"?mx:su/Math.max(1,c);}
function renderRaster(){
  if(layer=="snow"){const ca=a*NP,cb=b*NP;setRaster(p=>{const v=cum[cb+p]-cum[ca+p];const c=snowCol(v);return c?[c[0],c[1],c[2],235]:null;});}
  else if(layer=="depth"){const cb2=b*NP;setRaster(p=>{const v=cum[cb2+p];if(v<1)return null;const c=depthCol(v);return[c[0],c[1],c[2],215];});}
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
// A viewport-scoped raster has to follow the viewport.
let _progMoveT=null;
function progRefresh(){
  if(!(layer==='prog'||layer==='powfind'||layer==='progdiff'||layer==='progpat'))return;
  if(_progMoveT)clearTimeout(_progMoveT);
  _progMoveT=setTimeout(()=>{try{
    if(layer==='powfind')renderPowderFind();
    else if(layer==='progdiff')renderProgDiff();
    else if(layer==='progpat')renderProgPattern(stat);
    else renderPrognosis(stat);
  }catch(e){}},140);
}
map.on('moveend zoomend',progRefresh);
let _lastTier=-1;
map.on('zoomend',()=>{try{const t=detailTier();if(t!==_lastTier){_lastTier=t;renderStations();}loadReportMarkers();}catch(e){}});
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
  if(l=="aspect")return '<b>Aspekt (Horn · swissALTIRegio 60 m · 8 Sektoren)</b><br><div><i style="background:#4A90D9"></i>N</div><div><i style="background:#45C0C0"></i>NO</div><div><i style="background:#66BB6A"></i>O</div><div><i style="background:#BEDB39"></i>SO</div><div><i style="background:#EF5350"></i>S</div><div><i style="background:#FF9E42"></i>SW</div><div><i style="background:#FFC107"></i>W</div><div><i style="background:#A987E0"></i>NW</div><div><i style="background:#9E9E9E"></i>flach (&lt;5°)</div>';
  if(l=="tsurf"){let extra="estimated: air ± radiative cooling/warming";if(stat=="sub0")extra="only cells with max surface temp &lt;0°C";if(stat=="max05")extra="only cells with max 0–5°C";return `<b>T Surface [°C] (${sn})</b><br>${extra}`;}
  if(l=="rough")return "<b>Terrain Roughness</b><br>light→dark brown = rougher";
  if(l=="skiable")return '<b>Skiability Estimate</b><br>Snow depth vs. terrain roughness need<div style="margin-top:4px"><div><i style="background:#64dc64"></i>Plenty of snow</div><div><i style="background:#a0dc64"></i>Skiable</div><div><i style="background:#ffdc32"></i>Marginal</div><div><i style="background:#ff8c28"></i>Needs more snow</div><div><i style="background:#ff3c28"></i>Far from skiable</div><div><i style="background:#500000"></i>Too steep (&gt;55°)</div></div>';
  if(l=="powfind"){
    const sw=(cm)=>{const c=snowCol(cm)||[150,150,150];return '<div><i style="background:rgb('+c[0]+','+c[1]+','+c[2]+')"></i>'+cm+' cm</div>';};
    return '<b>Reported Powder</b><br>'+sw(10)+sw(30)+sw(60)+sw(100)+
      '<div style="margin-top:4px;font-size:11px">Farbe = Schneehöhe (SLF-Skala), gemittelt über die nahen Zeichnen-Reports pro Exposition und Höhenband.</div>'+
      '<div style="font-size:11px">Nur Zeichnen-Reports · Vertrauen ≥ '+progConfMin+'%</div>';}
  if(l=="progpat"){const ty=_progType||stat,pt=PROG_PATTERNS[ty]||{label:ty,hint:''};
    return '<b>'+pt.label+'</b><br><div style="font-size:11px">Muster statt Farbe — Farbe bleibt der Schneehöhe vorbehalten ('+pt.hint+').</div>'+
      '<div style="margin-top:3px;font-size:11px">Nur Zeichnen-Reports · Vertrauen ≥ '+progConfMin+'% · ab Zoom '+PROG_TYPE_ZOOM+'</div>';}
  if(l=="progdiff")
    return '<b>Abweichung · Meldungen vs. Modell</b>'+
      '<div style="margin:3px 0 2px"><span style="display:block;height:11px;border-radius:6px;background:linear-gradient(90deg,rgb(231,49,44),rgb(250,168,60),rgb(42,190,112),rgb(34,206,214),rgb(126,76,247))"></span>'+
      '<span style="display:flex;justify-content:space-between;font-size:10px;margin-top:2px"><span>Modell +</span><span>einig</span><span>Meldungen +</span></span></div>'+
      '<div><i style="background:rgb(250,168,60)"></i>Modell sagt Powder, Meldungen nicht</div>'+
      '<div><i style="background:rgb(42,190,112)"></i>beide einig (±'+Math.round(PROGDIFF_TOL*100)+'%)</div>'+
      '<div><i style="background:rgb(34,206,214)"></i>Meldungen sagen Powder, Modell nicht</div>'+
      '<div><i style="background:rgb(132,146,168)"></i>nur Modell (keine Meldung ≥ '+progConfMin+'%)</div>'+
      '<div style="margin-top:4px;font-size:11px">Vergleicht den Powder-Layer aus <b>Ski &rsaquo; Powder</b> mit der Prognose aus Meldungen. Quelle: '+(PROG_SRC_LABEL[progSrc]||progSrc)+'</div>';
  if(l=="prog"){const ty=_progType||stat,cl=(PROG_LABEL[ty]||ty);
    const ramp=PROG_CONF_STOPS.map(st=>'rgb('+st[1]+','+st[2]+','+st[3]+') '+Math.round(st[0]*100)+'%').join(',');
    return '<b>Prognose · '+cl+'</b><br><div style="margin:3px 0 2px"><span style="display:block;height:11px;border-radius:6px;background:linear-gradient(90deg,'+ramp+')"></span>'+
      '<span style="display:flex;justify-content:space-between;font-size:10px;margin-top:2px"><span>0%</span><span>50%</span><span>100%</span></span></div>'+
      '<div style="font-size:11px">Farbe = <b>Vertrauen</b>, Deckkraft = Wahrscheinlichkeit für '+cl+'.</div>'+
      '<div style="margin-top:3px;font-size:11px">Hänge mit gleichem Aspekt &amp; gleicher Höhe nahe einer Meldung · Quelle: '+(PROG_SRC_LABEL[progSrc]||progSrc)+'</div>';}
  if(l=="qprheat")return '<b>Powder-Reports (Heatmap)</b><br><div><i style="background:#cde4ff"></i>vereinzelt / wenig</div><div><i style="background:#5b83d9"></i>guter Powder</div><div><i style="background:#0f2a7d"></i>tief &amp; fluffy</div><div style="margin-top:4px;font-size:11px">aus Quick-Powder-Reports · inkl. Demo-Daten</div>';
  if(l=="powder")return '<b>Powder Conditions</b><br><div><i style="background:rgba(200,220,255,.7)"></i>Powder (stable)</div><div><i style="background:rgba(180,205,245,.55)"></i>Powder (reduced)</div><div style="margin-top:4px;font-size:11px">Gust ≈ mean wind × 1.5</div>';
  return "<b>Hillshade / Relief (swisstopo)</b>";}
function legend(l){document.getElementById('legend').innerHTML=legendFor(l||layer);}
document.getElementById('legendBtn').onclick=()=>{const lg=document.getElementById('legend'),btn=document.getElementById('legendBtn');lg.classList.toggle('show');btn.classList.toggle('active');};
function showOverlay(){
  [slopeWMTS,reliefWMTS,aspectGrid,roughImg,radOverlay,qprOverlay,prognosisOverlay].forEach(x=>map.removeLayer(x));
  const grid=(layer=="snow"||layer=="depth"||layer=="temp"||layer=="sun"||layer=="wind"||layer=="powder"||layer=="tsurf"||layer=="skiable");
  const radg=(layer=="rad"||layer=="radsun");
  raster.setOpacity(grid?0.94:0);
  if(radg)map.addLayer(radOverlay);
  if(layer=="slope")map.addLayer(slopeWMTS);
  else if(layer=="shade")map.addLayer(reliefWMTS);
  else if(layer=="aspect")map.addLayer(aspectGrid);
  else if(layer=="rough")map.addLayer(roughImg);
  else if(layer=="qprheat"){map.addLayer(qprOverlay);renderQprHeat();}
  else if(layer=="prog"){map.addLayer(prognosisOverlay);renderPrognosis(stat);}
  else if(layer=="powfind"){map.addLayer(prognosisOverlay);renderPowderFind();}
  else if(layer=="progdiff"){map.addLayer(prognosisOverlay);renderProgDiff();}
  else if(layer=="progpat"){map.addLayer(prognosisOverlay);renderProgPattern(stat);}
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
// ===== Layer menu: A Meteo-Modell (forecast model) / B Report-Modell ========
// Two groups, both visible at the top. A group holds items; an item with more
// than one variant gets a third row. The order inside "vars" is the order the
// buttons appear in, and vars[0] is what a fresh selection shows.
const GROUPS={
  meteo:{tag:'A',label:'Meteo-Modell',items:[
    {id:'powder',label:'Powder',vars:[{l:'powder',s:'avg',label:'Powder'}]},
    {id:'newsnow',label:'Neuschnee',vars:[{l:'snow',s:'avg',label:'Neuschnee'}]},
    {id:'depth',label:'Schneehöhe',vars:[{l:'depth',s:'avg',label:'Schneehöhe'}]},
    {id:'wind',label:'Wind',vars:[{l:'wind',s:'lt10',label:'<10 km/h'},{l:'wind',s:'avg',label:'Mittel'},{l:'wind',s:'max',label:'Max'},{l:'wind',s:'min',label:'Min'}]},
    {id:'temp',label:'Temperatur',vars:[{l:'temp',s:'sub0',label:'<0 °C'},{l:'tsurf',s:'avg',label:'Oberfläche'},{l:'temp',s:'avg',label:'Mittel'},{l:'temp',s:'max',label:'Max'},{l:'temp',s:'min',label:'Min'},{l:'temp',s:'max05',label:'0–5 °C'}]}
  ]},
  report:{tag:'B',label:'Report-Modell',items:[
    {id:'reppow',label:'Reported Powder',vars:[{l:'powfind',s:'powder',label:'Reported Powder'}]},
    {id:'diff',label:'Abweichung',vars:[{l:'progdiff',s:'powder',label:'Abweichung'}]},
    // The remaining snow types only appear once you are close enough for them
    // to mean anything, and are drawn as black/white patterns, not colours.
    {id:'drift',label:'Triebschnee',minZoom:PROG_TYPE_ZOOM,vars:[{l:'progpat',s:'drift',label:'Triebschnee'}]},
    {id:'windpressed',label:'Windgepresst',minZoom:PROG_TYPE_ZOOM,vars:[{l:'progpat',s:'windpressed',label:'Windgepresst'}]},
    {id:'suncrust',label:'Sonnendeckel',minZoom:PROG_TYPE_ZOOM,vars:[{l:'progpat',s:'suncrust',label:'Sonnendeckel'}]},
    {id:'wet',label:'Nassschnee',minZoom:PROG_TYPE_ZOOM,vars:[{l:'progpat',s:'wet',label:'Nassschnee'}]},
    {id:'firn',label:'Firn',minZoom:PROG_TYPE_ZOOM,vars:[{l:'progpat',s:'firn',label:'Firn'}]},
    {id:'scoured',label:'Abgeweht',minZoom:PROG_TYPE_ZOOM,vars:[{l:'progpat',s:'scoured',label:'Abgeweht'}]}
  ]}
};
// Layers kept in the code but no longer surfaced in the menu (skiable, aspect,
// slope, shade, rough, rad/radsun/sun, qprheat, prog). Re-add an entry above to
// bring one back.
let curTopic='meteo',curItem=0,curVar=0;

// [accentHex, sublayerTint, frameBorder, vignetteGlow]
const TOPIC_COLOR={
  meteo:['#1D6A6A','rgba(29,106,106,.13)','rgba(29,106,106,.5)','rgba(29,106,106,.10)'],
  report:['#8C4F27','rgba(140,79,39,.14)','rgba(140,79,39,.5)','rgba(140,79,39,.11)'],
  ski:['#4A7A3F','rgba(74,122,63,.14)','rgba(74,122,63,.5)','rgba(74,122,63,.10)'],
  snow:['#3E7C8C','rgba(62,124,140,.13)','rgba(62,124,140,.5)','rgba(62,124,140,.10)'],
  temp:['#B4552A','rgba(180,85,42,.13)','rgba(180,85,42,.52)','rgba(180,85,42,.12)'],
  wind:['#4C7A78','rgba(76,122,120,.14)','rgba(76,122,120,.5)','rgba(76,122,120,.10)'],
  rad:['#C08A2E','rgba(192,138,46,.16)','rgba(192,138,46,.55)','rgba(192,138,46,.13)'],
  terrain:['#7A7266','rgba(122,114,102,.16)','rgba(122,114,102,.5)','rgba(122,114,102,.10)'],
  qpr:['#2E5A4A','rgba(46,90,74,.14)','rgba(46,90,74,.5)','rgba(46,90,74,.10)'],
  prog:['#8C4F27','rgba(140,79,39,.14)','rgba(140,79,39,.5)','rgba(140,79,39,.11)'],
  aspect:['#4C7A78','rgba(76,122,120,.14)','rgba(76,122,120,.5)','rgba(76,122,120,.10)']};
let tlSel='#1D6A6A',tlSelTint='rgba(29,106,106,.12)';
function groupItems(g){const z=(function(){try{return map.getZoom();}catch(e){return 99;}})();
  return (GROUPS[g]||GROUPS.meteo).items.filter(it=>it.minZoom==null||z>=it.minZoom);}
function setTopic(t,itemIdx,varIdx){
  if(!GROUPS[t])t='meteo';
  curTopic=t;
  // Colour law: exactly one accent is live, and it is the one belonging to the
  // model group the selected layer came from.
  const rep=(t==='report');
  document.documentElement.style.setProperty('--accent',rep?'var(--accent-report)':'var(--accent-meteo)');
  document.documentElement.style.setProperty('--accent-soft',rep?'var(--accent-report-soft)':'var(--accent-meteo-soft)');
  const tc=TOPIC_COLOR[t]||TOPIC_COLOR.meteo;
  tlSel=tc[0];tlSelTint=tc[1];
  document.querySelectorAll('meta[name="theme-color"]').forEach(m=>m.setAttribute('content','#F7F4EE'));
  const items=groupItems(t);
  curItem=Math.max(0,Math.min(items.length-1,itemIdx||0));
  renderLayerStrip();
  const it=items[curItem],vars=it.vars;
  curVar=Math.max(0,Math.min(vars.length-1,varIdx||0));
  const vb=document.getElementById('variants');
  vb.innerHTML=vars.length>1?vars.map((v,i)=>'<button data-i="'+i+'"'+(i===curVar?' class="on"':'')+'>'+v.label+'</button>').join(''):'';
  vb.querySelectorAll('button').forEach(btn=>{btn.onclick=()=>setTopic(t,curItem,parseInt(btn.dataset.i));});
  const sel=vars[curVar];layer=sel.l;stat=sel.s;renderAll();progRenderBar();
  try{lbRender();}catch(e){}
  if(typeof panelRestore==='function')requestAnimationFrame(()=>{try{panelRestore();}catch(e){}});
}
// ===================== Layers: a few at the top, the rest behind "Alle" ===
// The model a layer belongs to used to be the first thing you had to read --
// two big "A · Meteo-Modell" / "B · Report-Modell" headings before any layer
// name. It is demoted here to a small mark on the row inside the full list.
// What sits on the map is the handful people actually switch between.
const LB_QUICK=[['meteo',0],['meteo',1],['meteo',2],['report',0]];
function lbItem(g,i){const items=groupItems(g);return items&&items[i]?items[i]:null;}
function lbRender(){
  const q=document.getElementById('lbQuick');if(!q)return;
  const seen=[];
  LB_QUICK.forEach(([g,i])=>{const it=lbItem(g,i);if(it)seen.push([g,i,it]);});
  // whatever is live always has a chip, even when it is not one of the few
  if(!seen.some(([g,i])=>g===curTopic&&i===curItem)){
    const it=lbItem(curTopic,curItem);if(it)seen.unshift([curTopic,curItem,it]);}
  q.innerHTML=seen.map(([g,i,it])=>'<button class="lb-chip'+
    (g===curTopic&&i===curItem?' on':'')+'" data-g="'+g+'" data-i="'+i+'">'+it.label+'</button>').join('');
  q.querySelectorAll('.lb-chip').forEach(b=>b.onclick=()=>{
    setTopic(b.dataset.g,parseInt(b.dataset.i),0);try{haptic(4);}catch(e){}});
  const on=q.querySelector('.lb-chip.on');
  if(on)requestAnimationFrame(()=>{try{on.scrollIntoView({block:'nearest',inline:'nearest'});}catch(e){}});
  // the bar's height sets where search and the pills start
  requestAnimationFrame(()=>{try{positionSearch();}catch(e){}});
}
function lbSheetRender(){
  const host=document.getElementById('lbList');if(!host)return;
  const rows=[];
  Object.keys(GROUPS).forEach(g=>groupItems(g).forEach((it,i)=>rows.push([g,i,it])));
  host.innerHTML=rows.map(([g,i,it])=>{
    const on=(g===curTopic&&i===curItem);
    const many=it.vars.length>1;
    return '<button class="lb-row'+(on?' on':'')+'" data-g="'+g+'" data-i="'+i+'">'+
      '<span class="lb-mark lb-'+g+'">'+GROUPS[g].tag+'</span>'+
      '<span class="lb-t"><b>'+it.label+'</b>'+
        (many?'<span>'+(on?it.vars[curVar].label:it.vars.length+' Varianten')+'</span>':'')+'</span>'+
      (on?'<span class="lb-tick"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 6"/></svg></span>':'')+
      '</button>';}).join('');
  host.querySelectorAll('.lb-row').forEach(b=>b.onclick=()=>{
    const g=b.dataset.g,i=parseInt(b.dataset.i);
    setTopic(g,i,0);lbSheetRender();
    const it=lbItem(g,i);
    // nothing more to choose -> get out of the way; variants stay for tap two
    if(!it||it.vars.length<=1)lbSheetClose();});
}
function lbSheetOpen(){document.body.classList.add('lb-open');lbSheetRender();try{haptic(5);}catch(e){}}
function lbSheetClose(){document.body.classList.remove('lb-open');}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&document.body.classList.contains('lb-open'))lbSheetClose();});
// Deep-zoom layers appear and disappear with the zoom, so keep both in sync.
function renderLayerStrip(){try{lbRender();
  if(document.body.classList.contains('lb-open'))lbSheetRender();}catch(e){}}
// Deep-zoom items appear/disappear as the user zooms, so keep the row in sync.
map.on('zoomend',function(){try{
  const shown=document.querySelectorAll('#lbList .lb-row').length||document.querySelectorAll('#lbQuick .lb-chip').length;
  const want=Object.keys(GROUPS).reduce((n,g)=>n+groupItems(g).length,0);
  if(shown!==want)setTopic(curTopic,Math.min(curItem,groupItems(curTopic).length-1),curVar);}catch(e){}});

function progRenderBar(){
  const bar=document.getElementById('progBar');if(!bar)return;
  const on=(layer==='prog'||layer==='powfind'||layer==='progdiff'||layer==='progpat');
  bar.classList.toggle('on',on);
  if(!on){bar.innerHTML='';return;}
  const srcs=['draw','quick','both','all'];
  // Reported Powder and the pattern layers are defined as drawn-reports-only,
  // so there is no source to choose there.
  let html=(layer==='powfind'||layer==='progpat')
    ?'<div class="pb-row"><span class="pb-lbl">Quelle</span><div class="pb-seg"><button class="active" disabled>Zeichnen-Reports</button></div></div>'
    :'<div class="pb-row"><span class="pb-lbl">Quelle</span><div class="pb-seg">'+
      srcs.map(k=>'<button data-src="'+k+'"'+(progSrc===k?' class="active"':'')+'>'+PROG_SRC_LABEL[k]+'</button>').join('')+
      '</div></div>';
  if(layer==='powfind'||layer==='progdiff'||layer==='progpat')
    html+='<div class="pb-row"><span class="pb-lbl">Vertrauen</span>'+
      '<input type="range" min="0" max="95" step="5" value="'+progConfMin+'" oninput="progSetConfMin(this.value)"/>'+
      '<b id="progConfVal">'+progConfMin+'%</b></div>';
  bar.innerHTML=html;
  bar.querySelectorAll('.pb-seg button[data-src]').forEach(b=>b.onclick=()=>progSetSrc(b.dataset.src));
}
// The layer bar is the first thing at the top of the map, and its height is
// not fixed (chips wrap on a narrow phone), so everything below it -- search,
// the demo pill, the brand mark -- is stacked off its measured bottom edge
// rather than off a hardcoded offset.
function positionSearch(){try{
  var sw=document.getElementById('searchWrap');if(!sw)return;
  var lb=document.getElementById('lbBar');
  var top=lb?Math.round(lb.getBoundingClientRect().bottom)+10:12;
  sw.style.top=top+'px';
  var dp=document.getElementById('demoPill');
  var bottom=sw.getBoundingClientRect().bottom;
  if(dp){dp.style.top=(bottom+8)+'px';bottom=dp.getBoundingClientRect().bottom;}
  var bm=document.getElementById('brandMark');
  if(bm)bm.style.top=(bottom+14)+'px';
}catch(e){}}
addEventListener('resize',positionSearch);requestAnimationFrame(positionSearch);
function presetsFade(){const p=document.getElementById('presets');if(!p)return;p.classList.toggle('can-scroll',p.scrollWidth-p.clientWidth-p.scrollLeft>4);}
(function(){const p=document.getElementById('presets');if(p)p.addEventListener('scroll',presetsFade,{passive:true});addEventListener('resize',presetsFade);requestAnimationFrame(presetsFade);})();
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
    // person + plus reads unambiguously as "sign in"; the old door-and-arrow
    // glyph is the same shape the convention uses for "log out".
    btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="8" r="3.6"/><path d="M3.5 20a6.5 6.5 0 0 1 13 0"/><line x1="19" y1="6" x2="19" y2="12"/><line x1="16" y1="9" x2="22" y2="9"/></svg>';return;}
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
// ===================== Screen router =====================================
// Three screens on a wrap-around carousel: Search -> Report -> Feed -> Search.
const SCREENS=['search','report','feed'];
const SCREEN_EL={search:'paneSearch',report:'paneReport',feed:'feedPage'};
const SCREEN_LABEL={search:'Search',report:'Report',feed:'Feed'};
let scrIdx=0;
function scrPaneOf(n){return document.getElementById(SCREEN_EL[n]);}
function scrCurrent(){return SCREENS[scrIdx];}
// -1 left, 0 centre, +1 right. With three panes every pane always holds one
// of those slots, which is what makes the wrap seamless.
function scrSlot(i){return ((i-scrIdx+1)%3+3)%3-1;}
function scrLayout(dxPx){
  const dx=dxPx||0;
  SCREENS.forEach((name,i)=>{
    const el=scrPaneOf(name);if(!el)return;
    const slot=scrSlot(i);
    el.style.transform='translate3d(calc('+(slot*100)+'% + '+dx+'px),0,0)';
    el.setAttribute('data-at',String(slot));
    el.setAttribute('aria-hidden',slot===0?'false':'true');
  });
}
function scrRenderTabs(){
  const L=document.getElementById('edgeL'),R=document.getElementById('edgeR');
  if(!L||!R)return;
  L.querySelector('span').textContent=SCREEN_LABEL[SCREENS[((scrIdx-1)%3+3)%3]];
  R.querySelector('span').textContent=SCREEN_LABEL[SCREENS[(scrIdx+1)%3]];
}
function scrGoTo(i,silent){
  // the layer sheet lives inside the map pane, so it leaves with the map
  try{lbSheetClose();}catch(e){}
  scrIdx=((i%3)+3)%3;
  document.body.classList.remove('scr-home');
  document.body.setAttribute('data-screen',scrCurrent());
  scrLayout(0);scrRenderTabs();
  if(!silent){try{haptic(5);}catch(e){}}
  setTimeout(()=>{try{map.invalidateSize({animate:false});}catch(e){}
    if(scrCurrent()==='feed'){try{feedRefresh();}catch(e){}}},360);
}
function scrGo(name){const i=SCREENS.indexOf(name);if(i>=0)scrGoTo(i);}
function scrNext(){scrGoTo(scrIdx+1);}
function scrPrev(){scrGoTo(scrIdx-1);}
function scrHome(){document.body.classList.add('scr-home');
  document.body.removeAttribute('data-screen');try{haptic(5);}catch(e){}}
(function(){
  // On the map the swipe has to start at a screen edge, because anywhere else
  // the gesture is a map pan. On the other panes the whole surface is fair
  // game once the movement is clearly horizontal.
  const EDGE=30,TAKEOVER=10;
  let x0=0,y0=0,dx=0,on=false,decided=false;
  function start(e){
    if(document.body.classList.contains('scr-home'))return;
    if(document.body.classList.contains('draw-on'))return;
    const t=e.touches?e.touches[0]:e;
    x0=t.clientX;y0=t.clientY;dx=0;decided=false;
    on=(scrCurrent()==='search')?(x0<=EDGE||x0>=innerWidth-EDGE):true;
  }
  function move(e){
    if(!on)return;
    const t=e.touches?e.touches[0]:e,ddx=t.clientX-x0,ddy=t.clientY-y0;
    if(!decided){
      if(Math.abs(ddx)<TAKEOVER&&Math.abs(ddy)<TAKEOVER)return;
      if(Math.abs(ddy)>Math.abs(ddx)){on=false;return;}  // vertical belongs to the scroller
      decided=true;document.body.classList.add('pane-dragging');
    }
    dx=ddx;scrLayout(dx);
    if(e.cancelable)e.preventDefault();
  }
  function end(){
    if(!on)return;on=false;
    if(!decided)return;
    decided=false;document.body.classList.remove('pane-dragging');
    const need=Math.min(96,innerWidth*0.22);
    if(dx<=-need)scrGoTo(scrIdx+1);else if(dx>=need)scrGoTo(scrIdx-1);else scrLayout(0);
    dx=0;
  }
  addEventListener('touchstart',start,{passive:true});
  addEventListener('touchmove',move,{passive:false});
  addEventListener('touchend',end);addEventListener('touchcancel',end);
  addEventListener('keydown',e=>{
    if(document.body.classList.contains('scr-home'))return;
    if(/^(INPUT|TEXTAREA|SELECT)$/.test((e.target&&e.target.tagName)||''))return;
    if(e.key==='ArrowRight')scrNext();else if(e.key==='ArrowLeft')scrPrev();});
})();
// --- Bottom panel: expand / collapse by tapping the handle (no swipe) ---
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
  // One tap toggles the detail chart. A gesture that can be started by accident
  // while panning the map has no business moving a docked surface.
  tl.setAttribute('role','button');tl.setAttribute('tabindex','0');
  tl.setAttribute('aria-label','Zeitdetails ein- oder ausblenden');
  function toggle(){panelCollapsed=!panelCollapsed;apply();try{haptic(4);}catch(e){}}
  tl.addEventListener('click',toggle);
  tl.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}});
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
map.setMaxBounds([[laMinW-1.0,loMinW-1.5],[laMaxW+1.0,loMaxW+1.5]]);
const _mz2=map.getBoundsZoom([[laMinW,loMinW],[laMaxW,loMaxW]])+0.3;if(map.getZoom()<_mz2)map.setZoom(_mz2,{animate:false});map.setMinZoom(_mz2);
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
  const _fe=fineElev(lat,lon),_fa=fineAspectDeg(lat,lon),_fs=fineSlope(lat,lon);
  const elevD=(_fe!=null?_fe:elev),slp=(_fs!=null?_fs:mslpv(p));
  const QD8={N:'Nord',NO:'Nordost',O:'Ost',SO:'Südost',S:'Süd',SW:'Südwest',W:'West',NW:'Nordwest'},QD4={N:'Nord',E:'Ost',S:'Süd',W:'West'};
  const aspDeg=(_fa!=null?_fa:maspv(p)),aspLbl=(_fa!=null?QD8[asp8(_fa)]:(QD4[aspectQ(maspv(p))]||''));
  const pan=document.getElementById('inspPanel');
  let progSec='';
  if(layer==='prog'){try{const pr=prognosisAt(lat,lon);if(pr){const cl=(PROG_LABEL[pr.type]||pr.type);const zc=progZones().filter(z=>z.type===pr.type).length;
    progSec='<div class="insp-sec insp-prog"><h4>Prognose '+cl+' <em>'+pr.like+'% wahrsch.</em></h4>'+
      '<div class="prog-conf"><div class="prog-bar"><i style="width:'+pr.conf+'%"></i></div><span>'+pr.conf+'% Vertrauen</span></div>'+
      '<div class="prog-note">Aus '+zc+' Meldung(en), gewichtet nach Aspekt, Höhe, Distanz sowie Bestätigungen und Trust Score der Melder.</div></div>';}}catch(e){}}
  if(layer==='progdiff'){try{const pd=progDiffAt(lat,lon);if(pd){
    const verdict=pd.diff==null?'Keine belastbare Meldung hier – nur Modell.'
      :(Math.abs(pd.diff)<=PROGDIFF_TOL*100?'Meldungen und Modell sind sich einig.'
        :(pd.diff<0?'Modell erwartet Powder, die Meldungen nicht.':'Meldungen zeigen Powder, das Modell nicht.'));
    progSec='<div class="insp-sec insp-prog"><h4>Abweichung <em>'+(pd.diff==null?'–':(pd.diff>0?'+':'')+pd.diff+'%')+'</em></h4>'+
      '<div class="prog-conf"><div class="prog-bar"><i style="width:'+pd.rep+'%"></i></div><span>Meldungen '+pd.rep+'%</span></div>'+
      '<div class="prog-conf"><div class="prog-bar"><i style="width:'+pd.model+'%"></i></div><span>Modell '+pd.model+'%</span></div>'+
      '<div class="prog-note">'+verdict+' Vertrauen der Meldungen: '+pd.conf+'%.</div></div>';}}catch(e){}}
  pan.innerHTML=
   '<div class="insp-head"><div class="insp-t"><b>'+lat.toFixed(4)+'° N, '+lon.toFixed(4)+'° E</b>'+
     '<div class="insp-chips"><span class="insp-chip">'+ic('peak')+' '+elevD.toFixed(0)+' m</span><span class="insp-chip accent">'+aspLbl+' · '+aspDeg.toFixed(0)+'°</span><span class="insp-chip">'+slp.toFixed(0)+'°</span></div>'+
   '</div><button aria-label="Schliessen" onclick="inspClose()">✕</button></div>'+
   '<div class="insp-body">'+progSec+
     '<div class="insp-sec"><h4>Neuschnee <em>+'+newSnow.toFixed(1)+' cm</em></h4><canvas id="icNew"></canvas></div>'+
     '<div class="insp-sec"><h4>Schneehöhe <em>'+depthNow.toFixed(0)+' cm</em></h4><canvas id="icDepth"></canvas></div>'+
     '<div class="insp-sec"><h4>Temperatur <em>Luft · Oberfläche</em></h4><canvas id="icTemp"></canvas></div>'+
     '<div class="insp-sec"><h4>Windrose <em>Ø '+wmean.toFixed(0)+' km/h</em></h4><canvas id="icWind"></canvas></div>'+
     '<div class="insp-sec"><h4>Strahlung <em>% vom Tagesmaximum</em></h4><canvas id="icRad"></canvas></div>'+
   '</div>';
  pan.classList.add('open');
  requestAnimationFrame(()=>{
    icNewSnow(document.getElementById('icNew'),p);
    icDepth(document.getElementById('icDepth'),p);
    icTemp(document.getElementById('icTemp'),p);
    icWind(document.getElementById('icWind'),p,wk);
    icRad(document.getElementById('icRad'),p);
  });
}
// The inspect panel is docked by CSS: no drag, no snap, no stored offset. It
// always appears in the same place, which is what makes it feel built in.
function icSetup(cv,hh,wOverride){const dpr=window.devicePixelRatio||1;const ww=wOverride||cv.getBoundingClientRect().width||320;
  cv.width=Math.round(ww*dpr);cv.height=Math.round(hh*dpr);cv.style.height=hh+'px';if(wOverride)cv.style.width=ww+'px';
  const ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,ww,hh);return{ctx,w:ww,h:hh};}
function icRR(ctx,x,y,w,h,r){r=Math.max(0,Math.min(r,w/2,h/2));ctx.beginPath();if(ctx.roundRect)ctx.roundRect(x,y,w,h,r);else{ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}}
function icDayGrid(ctx,x0,plotW,h,t0,t1,baseY){ctx.textAlign='center';ctx.font='600 11px Inter,system-ui';let _lx=-1e9;
  for(let t=t0;t<t1;t++){const d=new Date(M.times[t]+'Z');if(d.getUTCHours()===0){const x=x0+(t-t0)/(t1-t0)*plotW;
    ctx.strokeStyle=cvTok('--ink-050','#F0ECE3');ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,6);ctx.lineTo(x,baseY);ctx.stroke();
    if(x-_lx>=52){ctx.fillStyle='rgba(115,108,97,.8)';ctx.fillText(d.toLocaleDateString('de-CH',{day:'2-digit',month:'short'}),x+22,h-3);_lx=x;}}}}
function icPill(ctx,x,y,txt,col,w){ctx.font='800 11px Inter';const tw=ctx.measureText(txt).width+12,hh=17;
  let px=Math.max(2,Math.min(x-tw/2,w-tw-2)),py=Math.max(2,y);
  ctx.fillStyle=cvTok('--paper','#F7F4EE');icRR(ctx,px,py,tw,hh,8.5);ctx.fill();
  ctx.strokeStyle='rgba(33,30,25,.12)';ctx.lineWidth=1;icRR(ctx,px,py,tw,hh,8.5);ctx.stroke();
  ctx.fillStyle=col;ctx.textAlign='center';ctx.fillText(txt,px+tw/2,py+12.5);}
function icNewSnow(cv,p){const{ctx,w,h}=icSetup(cv,86);const t0=a,t1=b,n=Math.max(1,t1-t0);const LG=4,baseY=h-16,plotW=w-LG-4;
  let mx=0,mi=0;const vals=[];for(let t=t0;t<t1;t++){const v=SNOW[t*NP+p]*M.snow_scale;vals.push(v);if(v>mx){mx=v;mi=t-t0;}}
  const mxs=Math.max(0.05,mx);
  icDayGrid(ctx,LG,plotW,h,t0,t1,baseY);
  ctx.strokeStyle='rgba(33,30,25,.12)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(LG,baseY+.5);ctx.lineTo(w,baseY+.5);ctx.stroke();
  const bw=plotW/n,g=ctx.createLinearGradient(0,8,0,baseY);g.addColorStop(0,'#4E9A9A');g.addColorStop(1,'#1D6A6A');ctx.fillStyle=g;
  for(let i=0;i<n;i++){const v=vals[i];if(v<0.002)continue;const bh=Math.max(1.5,v/mxs*(baseY-26));icRR(ctx,LG+i*bw+.4,baseY-bh,Math.max(bw-1,1.2),bh,Math.min(2,bw/2.2));ctx.fill();}
  if(mx>0.002){const px=LG+mi*bw+bw/2,bh=Math.max(1.5,mx/mxs*(baseY-26));
    icPill(ctx,px,baseY-bh-21,mx.toFixed(mx>=1?1:2)+' cm/h',cvTok('--accent-meteo','#1D6A6A'),w);}}
function icDepth(cv,p){const{ctx,w,h}=icSetup(cv,80);const t0=a,t1=b;const LG=4,baseY=h-16,plotW=w-LG-4;
  const vals=[];let mx=1,mi=0;for(let t=t0;t<=t1;t++){const v=cum[t*NP+p];vals.push(v);if(v>mx){mx=v;mi=t-t0;}}
  icDayGrid(ctx,LG,plotW,h,t0,t1,baseY);
  ctx.strokeStyle='rgba(33,30,25,.12)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(LG,baseY+.5);ctx.lineTo(w,baseY+.5);ctx.stroke();
  const Ln=vals.length,px=i=>LG+i/(Ln-1)*plotW,py=v=>baseY-v/mx*(baseY-26);
  ctx.beginPath();for(let i=0;i<Ln;i++){const x=px(i),y=py(vals[i]);i?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.lineTo(px(Ln-1),baseY);ctx.lineTo(LG,baseY);ctx.closePath();ctx.fillStyle='rgba(29,106,106,.13)';ctx.fill();
  ctx.beginPath();for(let i=0;i<Ln;i++){const x=px(i),y=py(vals[i]);i?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.strokeStyle=cvTok('--accent-meteo','#1D6A6A');ctx.lineWidth=2.2;ctx.stroke();
  icPill(ctx,px(mi),py(vals[mi])-21,vals[mi].toFixed(0)+' cm',cvTok('--accent-meteo','#1D6A6A'),w);}
function icTemp(cv,p){const{ctx,w,h}=icSetup(cv,110);const t0=a,t1=Math.max(a+1,b),n=t1-t0;
  const baseY=h-16,topY=18,plotW=w-8,xOf=i2=>4+i2/Math.max(1,n-1)*plotW;
  let mn=1e9,mx=-1e9;const air=[],srf=[];
  for(let t=t0;t<t1;t++){const va=tv(t,p),vs=tsurfEst(t,p);air.push(va);srf.push(vs);mn=Math.min(mn,va,vs);mx=Math.max(mx,va,vs);}
  mn=Math.floor(mn-1);mx=Math.ceil(mx+1);const rng=Math.max(1,mx-mn),yOf=v=>baseY-(v-mn)/rng*(baseY-topY);
  icDayGrid(ctx,4,plotW,h,t0,t1,baseY);
  if(mx>0){const y5=yOf(Math.min(5,mx)),y0=yOf(Math.max(0,mn));ctx.fillStyle='rgba(192,138,46,.10)';ctx.fillRect(4,y5,plotW,Math.max(0,y0-y5));}
  function refLine(v,col,lbl){if(v<mn||v>mx)return;const y=yOf(v);
    ctx.strokeStyle=col;ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(4,y);ctx.lineTo(4+plotW,y);ctx.stroke();ctx.setLineDash([]);
    ctx.font='800 10px Inter';ctx.textAlign='left';ctx.fillStyle=cvTok('--paper','#F7F4EE');ctx.fillRect(6,y-11,20,12);ctx.fillStyle=col;ctx.fillText(lbl,8,y-2);}
  refLine(0,'rgba(168,58,46,.55)','0°');refLine(5,'rgba(192,138,46,.65)','5°');
  if(nowIdx>=t0&&nowIdx<t1){const x=xOf(nowIdx-t0);ctx.strokeStyle='rgba(33,30,25,.4)';ctx.setLineDash([2,2]);ctx.beginPath();ctx.moveTo(x,topY);ctx.lineTo(x,baseY);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#e0245e';ctx.font='700 9.5px Inter';ctx.textAlign='center';ctx.fillText('JETZT',x,topY-5);}
  function line(arr,col,wd){ctx.beginPath();for(let i2=0;i2<n;i2++){const x=xOf(i2),y=yOf(arr[i2]);i2?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.strokeStyle=col;ctx.lineWidth=wd;ctx.lineJoin='round';ctx.stroke();}
  line(srf,'#C08A2E',1.6);line(air,'#3E7C8C',2.2);
  let aMx=-1e9,aMxI=0,aMn=1e9,aMnI=0;
  for(let i2=0;i2<n;i2++){if(air[i2]>aMx){aMx=air[i2];aMxI=i2;}if(air[i2]<aMn){aMn=air[i2];aMnI=i2;}}
  icPill(ctx,xOf(aMxI),yOf(aMx)-22,'Hoch '+aMx.toFixed(0)+'°','#B4552A',w);
  icPill(ctx,xOf(aMnI),Math.min(baseY-18,yOf(aMn)+6),'Tief '+aMn.toFixed(0)+'°',cvTok('--accent-meteo','#1D6A6A'),w);
  // the legend hugs the right edge: the "Tief" pill is clamped to the left one
  ctx.textAlign='right';ctx.font='700 10.5px Inter';
  ctx.fillStyle='#C08A2E';ctx.fillText('● Oberfläche',w-6,12);
  ctx.fillStyle='#3E7C8C';ctx.fillText('● Luft',w-78,12);ctx.textAlign='left';}
function icWind(cv,p,wk){const{ctx,w,h}=icSetup(cv,152);const cx=w/2,cy=h/2+2,R=Math.min(cx,cy)-18;
  const NS=8,cnt=new Array(NS).fill(0),spd=new Array(NS).fill(0);let n=0,tot=0;
  for(let t=a;t<b;t++){let dir=(WDIR[t*P+wk]*M.dir_div)%360;if(dir<0)dir+=360;const s=SPD[t*P+wk]/M.spd_mul*3.6;const si=Math.round(dir/(360/NS))%NS;cnt[si]++;spd[si]+=s;n++;tot+=s;}
  for(let i=0;i<NS;i++)if(cnt[i])spd[i]/=cnt[i];
  const mxC=Math.max(1,...cnt);
  ctx.strokeStyle=cvTok('--ink-100','rgba(33,30,25,.11)');ctx.lineWidth=1;for(let r=1;r<=3;r++){ctx.beginPath();ctx.arc(cx,cy,R*r/3,0,2*Math.PI);ctx.stroke();}
  const labs=['N','NE','E','SE','S','SW','W','NW'];ctx.fillStyle='rgba(115,108,97,.85)';ctx.font='700 10.5px Inter';ctx.textAlign='center';ctx.textBaseline='middle';
  for(let i=0;i<NS;i++){const ang=i*(2*Math.PI/NS)-Math.PI/2;ctx.fillText(labs[i],cx+Math.cos(ang)*(R+10),cy+Math.sin(ang)*(R+10));}
  for(let i=0;i<NS;i++){if(!cnt[i])continue;const ang=i*(2*Math.PI/NS)-Math.PI/2,len=cnt[i]/mxC*R,half=(Math.PI/NS)*0.72,c=rampBYR(Math.min(1,spd[i]/50));
    ctx.fillStyle='rgba('+c[0]+','+c[1]+','+c[2]+',.85)';ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,len,ang-half,ang+half);ctx.closePath();ctx.fill();}
  ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(cx,cy,16,0,2*Math.PI);ctx.fill();
  ctx.fillStyle='#16152e';ctx.font='800 13px Inter';ctx.textBaseline='middle';ctx.fillText((tot/Math.max(1,n)).toFixed(0),cx,cy-2);
  ctx.font='600 7.5px Inter';ctx.fillStyle='rgba(115,108,97,.85)';ctx.fillText('km/h Ø',cx,cy+9);ctx.textBaseline='alphabetic';}
function icRad(cv,p){const{ctx,w,h}=icSetup(cv,92);
  // One bar per day: percentage of that day's theoretical max radiation
  // actually reached at this location (sunshine fraction vs. clear sky).
  const days=[];let cur=null;
  for(let t=0;t<T;t++){const d=new Date(M.times[t]+'Z'),key=d.toISOString().slice(0,10);
    if(!cur||cur.key!==key){cur={key,sum:0,n:0,inWin:false,lbl:d.toLocaleDateString('de-CH',{weekday:'short'})};days.push(cur);}
    cur.sum+=sunv(t,p);cur.n++;if(t>=a&&t<b)cur.inWin=true;}
  const shown=days.filter(d2=>d2.inWin);
  const baseY=h-16,topY=20,n=shown.length,bw=(w-8)/Math.max(1,n);
  ctx.strokeStyle='rgba(33,30,25,.12)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(4,baseY+.5);ctx.lineTo(w-4,baseY+.5);ctx.stroke();
  shown.forEach((d2,i)=>{
    const pct=Math.round(100*Math.max(0,Math.min(1,d2.sum/(0.42*24))));
    const bh=Math.max(2,pct/100*(baseY-topY)),x=4+i*bw;
    ctx.fillStyle=d2.inWin?'#C08A2E':'rgba(192,138,46,.32)';
    icRR(ctx,x+1.5,baseY-bh,Math.max(2,bw-3),bh,3);ctx.fill();
    ctx.font='700 9.5px Inter';ctx.textAlign='center';
    ctx.fillStyle=d2.inWin?'#8A5F14':'rgba(115,108,97,.8)';
    ctx.fillText(pct+'%',x+bw/2,baseY-bh-4);
    ctx.fillStyle='rgba(115,108,97,.8)';ctx.font='600 9px Inter';
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
        'swisstopo':{type:'raster',tiles:[swissTile(BASE_TILE)],tileSize:256,attribution:BASE_ATTR},
        'hillshade-tiles':{type:'raster',tiles:[swissTile('ch.swisstopo.swissalti3d-reliefschattierung_monodirektional','png')],tileSize:256},
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
// Declared here, above the boot call below, on purpose: maybeDisclaimer() runs
// during module evaluation, so a const declared further down would still be in
// its temporal dead zone and the gate would re-prompt everyone on every start.
const DISC_VER='1';
// There is no splash to dismiss: the app is already on screen. All that is
// left is to retire the loading hairline and put the disclaimer up.
function dismissIntro(){try{if(window.__bootDone)window.__bootDone();}catch(e){}
  maybeDisclaimer();}
// --- First-run onboarding coach marks ---
// These pointed at the old floating layer card, which no longer exists — they
// now walk the one-screen console.
const COACH_STEPS=[
  {sel:'#lbBar',html:'<b>Ebenen.</b><br>Die wichtigsten liegen oben auf der Karte — <b>Alle</b> öffnet die ganze Liste.'},
  {sel:'#btmMain',html:'<b>Zeitfenster.</b><br>Wähle den Zeitraum – die Karte rechnet sofort neu.'},
  {sel:'#searchWrap',html:'<b>Spring zu einem Ort.</b><br>Suche einen Berg oder Ort und zoome direkt dorthin.'},
  {sel:'#edgeR',html:'<b>Wisch dich durch.</b><br>Nach rechts geht es zu <b>Report</b>, nach links zum <b>Feed</b> – oder tippe auf die Laschen am Rand.'},
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
// Start on the abstract country view (fitBounds above already set it) and
// re-assert it in case a boot animation moves the map. ?start=davos still
// jumps straight to Davos for demos.
function _gotoDavos(){try{map.stop();map.setView([46.7970,9.8270],11.8,{animate:false});}catch(e){}}
// Fit the whole country (data grid UNION national border) once the layout has
// settled -- the very first fitBounds runs before --btm-h and the safe-area
// insets are applied, so its zoom is slightly too close and Switzerland was
// clipped left and right. Re-fitting here also re-derives minZoom.
function _gotoCountry(){try{map.stop();map.invalidateSize({animate:false});
  const bb=L.latLngBounds([laMin,loMin],[laMax,loMax]).extend(chOutline.getBounds());
  map.setMinZoom(2);map.fitBounds(bb,{padding:[10,10],animate:false});
  map.setMinZoom(map.getZoom());updateBaseFade();}catch(e){}}
(function(){const davos=(function(){try{return location.search.indexOf('start=davos')>=0;}catch(e){return false;}})();
  const go=davos?_gotoDavos:_gotoCountry;go();setTimeout(go,400);})();
window.__APP_OK=true;
setTopic('meteo',0,0);dismissIntro();
// Personal preferences (opening layer, time window, where the map lands) are
// applied once the map and the layer strip exist.
requestAnimationFrame(()=>{try{prefsApplyStartup();}catch(e){}
  try{document.body.classList.add('scr-home');scrLayout(0);scrRenderTabs();}catch(e){}
  try{lbRender();}catch(e){}});
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
const CAT_COLORS={snow:'#3E7C8C',route:'#4A7A3F',danger:'#A83A2E',tour:'#7A4E86',info:'#C08A2E',avalanche:'#A83A2E',whumpf:'#B4552A',wind_slab:'#1D6A6A',other:'#8C4F27'};
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
// --- Which reporting flows are surfaced -------------------------------------
// Everything is still implemented; these switches only decide what the UI
// offers. Flip one back to true to bring that flow back in a later version.
const FEATURES={
  draw:true,          // v3 paint a snow map  -- the flow this version is about
  observation:true,   // v1 "+" -- avalanche, warning signs, snow depth, ...
  quickPowder:false,  // v2 one-tap powder report
  demoOther:false     // seed the demo feed with non-drawn reports too
};
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
const DEMO_REPORTS=(FEATURES.demoOther?DEMO_DEFS:[]).map((d,i2)=>({id:'d'+(i2+1),user:DEMO_USERS[i2%DEMO_USERS.length],cat:d[0],icon:'',
  sub:d[1],measurement:d[2],caption:d[3],lat:d[4],lng:d[5],
  time:i2<3?('vor '+(2+i2)+'h'):(i2<12?('vor '+(i2-1)+'h'):('vor '+(i2-10)+'d')),
  img:(i2%4===3)?null:demoImg(200+i2*9,210+i2*7,205+(i2%5)*6),stars:(i2%5)+1,likes:(i2*3)%17,comments:i2%4}));

// Grosse Demo-Flotte: ~150 zusätzliche Meldungen von vielen Nutzern
(function(){
  const U2=[...DEMO_USERS,'NiveNina','CouloirChris','FirnFelix','TiefschneeTina','RidgeRuedi','SplitSelina','EngadinElia','WallisWendy','GlarnerGian','UrnerUrs','BuendnerBea','JuraJon','TessinTom','SaasSera','ArosaAnja','ElmEnzo','DavosDario','ZermattZoe','GrindelGwen','AndermattAri'];
  const spots=[[46.797,9.827],[46.78,9.67],[46.49,9.84],[46.02,7.75],[46.10,7.23],[46.62,8.03],[46.68,8.59],[46.34,8.99],[46.86,8.63],[47.05,9.44],[46.44,7.10],[46.35,7.99],[46.39,7.63],[46.31,7.47],[46.56,7.98],[46.72,8.64],[46.85,9.21],[46.60,10.03],[46.53,9.90],[47.10,8.76],[46.19,7.31],[46.90,9.83],[46.43,9.76],[46.66,8.06],[46.08,7.93],[46.73,9.60],[46.70,9.16],[46.47,8.75],[46.63,8.60],[46.30,7.60]];
  let seed=42;const rnd=()=> (seed=(seed*1103515245+12345)&0x7fffffff)/0x7fffffff;
  const jit=()=> (rnd()-0.5)*0.08;
  const qualN=['Winddeckel','Sonnendeckel','Durchnässter Pulver','Gealterter Pulver','Pulver'];
  let id=DEMO_REPORTS.length;
  function add(o){DEMO_REPORTS.push(Object.assign({id:'g'+(++id),icon:'',likes:Math.floor(rnd()*25),comments:Math.floor(rnd()*5),stars:1+Math.floor(rnd()*5)},o));}
  // Quick-Powder-Reports: regional differenziert — jede Region hat ein eigenes
  // Schnee-Profil (Engadin fluffy & tief, Wallis-Sued Sonnendeckel, Ostschweiz
  // Winddeckel, Tessin durchnaesst ...), inkl. Abstufung stark/leicht, Expo+Hoehe,
  // teils Foto und Caption.
  const qLbl=q=>{const bi=Math.min(4,Math.floor(q/20)),f=(q-bi*20)/20;
    if(bi<=1)return (f<0.33?'Starker ':f>0.67?'Leichter ':'')+qualN[bi];
    if(bi===2)return f<0.33?'Stark durchnässt':f>0.67?'Leicht durchnässt':qualN[bi];
    return qualN[bi]+(f>0.67?' · top':'');};
  const regions=[
    {idx:[0,17,18,21],q:82,qs:16,cm:42,cs:14},  // Davos/Engadin: fluffy & tief
    {idx:[3,4,24,20],q:32,qs:18,cm:30,cs:12},   // Wallis Sued: Sonnendeckel
    {idx:[10,14,11,25],q:65,qs:20,cm:34,cs:12}, // Berner Oberland: gut
    {idx:[5,6,8,27],q:55,qs:25,cm:26,cs:10},    // Zentralschweiz: gemischt
    {idx:[7,12,22],q:48,qs:12,cm:22,cs:10},     // Tessin: durchnaesst
    {idx:[9,19,15,26],q:14,qs:12,cm:16,cs:8}];  // Ostschweiz/Jura: Winddeckel
  const qCaps=['Bester Run der Saison!','Oben top, unten Deckel.','Nordseitig noch kalt & trocken.','Ab Mittag feucht.','Windgepresst am Grat, im Schutz gut.','Knietief!','Tragfähiger Deckel, drunter weich.','Erste 200 Hm traumhaft.'];
  const expos=['N','NE','E','SE','S','SW','W','NW'];
  for(let i=0;i<(FEATURES.demoOther?50:0);i++){
    const rg=regions[i%regions.length];
    const sp=spots[rg.idx[Math.floor(rnd()*rg.idx.length)]%spots.length];
    let q=Math.round(rg.q+(rnd()*2-1)*rg.qs);q=Math.max(0,Math.min(99,q));
    let cm=5*Math.round((rg.cm+(rnd()*2-1)*rg.cs)/5);cm=Math.max(5,Math.min(60,cm));
    add({user:U2[(i*11+3)%U2.length],cat:'snow',sub:'Quick Powder',
      measurement:qLbl(q)+' · '+cm+' cm',
      caption:(i%3===0)?qCaps[i%qCaps.length]:null,
      lat:sp[0]+jit(),lng:sp[1]+jit(),time:'vor '+(1+Math.floor(rnd()*96))+'h',
      img:(i%5===4)?demoImg(210+i*13,190+i*17,205+i*7):null,
      condition_data:{quick:true,powderAmountCm:cm,powderQuality:q,exposition:expos[Math.floor(rnd()*8)],altitudeM:1800+Math.round(rnd()*14)*100}});}
  const caps=['Traumtag!','Unverspurt bis Mittag.','Harter Deckel oberhalb 2600 m.','Beste Verhältnisse im Nordhang.','Sicht mässig, Schnee top.','Viel Wind am Grat.','Sulz ab Mittag.','Perfekter Firn.','Grosse Einsinktiefe, Vorsicht.','Lohnt sich!'];
  for(let i=0;i<(FEATURES.demoOther?50:0);i++){ // Feld-Reports mit Foto
    const sp=spots[(i*3)%spots.length];
    add({user:U2[(i*7)%U2.length],cat:i%3?'snow':'tour',sub:i%3?'Neuschnee':'Skitour',
      measurement:(10+5*Math.floor(rnd()*9))+' cm',caption:caps[i%caps.length],
      lat:sp[0]+jit(),lng:sp[1]+jit(),time:'vor '+(1+Math.floor(rnd()*96))+'h',
      img:demoImg(180+i*11,200+i*13,190+i*7)});}
  const obsT=[['danger','Wumm-Geräusche','Setzungsgeräusche beim Aufstieg.'],['danger','Lawinenabgang','Frisches Schneebrett im NE-Hang.'],['danger','Triebschnee','Deutliche Triebschnee-Ansammlungen am Grat.'],['snow','Schneehöhe','Messung am Hangfuss.'],['tour','Aufstiegsspur','Spur neu angelegt, gut zu gehen.']];
  for(let i=0;i<(FEATURES.demoOther?50:0);i++){ // Beobachtungen
    const sp=spots[(i*5+2)%spots.length],t=obsT[i%obsT.length];
    add({user:U2[(i*3+1)%U2.length],cat:t[0],sub:t[1],
      measurement:t[1]==='Schneehöhe'?(40+5*Math.floor(rnd()*20))+' cm':null,
      caption:t[2],lat:sp[0]+jit(),lng:sp[1]+jit(),time:'vor '+(1+Math.floor(rnd()*5))+'d',
      img:(i%5===0)?demoImg(150+i*9,170+i*11,160+i*5):null});}
  // ---- Demo Schnee-Karten (Zeichnen v3) mit Aspekt/Höhen-Zonen fürs Prognose-Modell ----
  const DC={powder:'#4aa3ff',drift:'#e8590c',wet:'#f59e0b',suncrust:'#f4d03f',windpressed:'#b9c4d4',firn:'#3fb27f',scoured:'#9a8778'};
  const DCL={powder:'Powder',drift:'Triebschnee',wet:'Nassschnee',suncrust:'Sonnendeckel',windpressed:'Windgepresst',firn:'Firn',scoured:'Abgeweht'};
  function drawDemoSnap(col){return 'data:image/svg+xml;utf8,'+encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><rect width="400" height="300" fill="#e9eef3"/>'+
    '<path d="M0 215 L95 150 L175 195 L265 120 L345 170 L400 135 L400 300 L0 300Z" fill="#dee6ec"/>'+
    '<path d="M55 130 L150 82 L215 122 L305 74" stroke="#cdd7e0" stroke-width="6" fill="none"/>'+
    '<ellipse cx="210" cy="152" rx="122" ry="66" fill="'+col+'" opacity="0.5"/>'+
    '<ellipse cx="150" cy="120" rx="70" ry="40" fill="'+col+'" opacity="0.44"/></svg>');}
  // Drawn snow maps are the only demo content now, so there are more of them
  // and the powder ones carry a depth so Reported Powder has a scale to show.
  const drawDemos=[
    [46.80,9.83,'powder',0,1700,2300,'Top Pulver an den Nordhängen bei Davos',45],
    [46.86,9.88,'powder',315,1900,2500,'Davos NW — noch komplett unverspurt',55],
    [46.49,9.90,'powder',315,2000,2600,'Engadin NW — unverspurt & fluffy',40],
    [46.56,9.96,'powder',0,2100,2700,'Oberengadin Nordhänge, kalt & trocken',35],
    [46.10,7.60,'suncrust',180,2200,2900,'Wallis Südhänge — harter Sonnendeckel oben',null],
    [46.62,8.03,'windpressed',45,2100,2700,'Berner Oberland — windgepresst am Grat NE',null],
    [46.94,8.63,'wet',90,1200,1800,'Zentralschweiz Osthänge — nass ab Mittag',null],
    [46.30,8.80,'wet',180,1000,1700,'Tessin — durchnässt, Südhänge',null],
    [46.73,9.60,'powder',0,1800,2400,'Nordbünden — kalter Pulver an N-Hängen',30],
    [47.05,9.10,'windpressed',315,1900,2500,'Glarner Alpen — Triebschnee-Deckel NW',null],
    [46.02,7.75,'drift',270,2300,2900,'Wallis Westgrate — frischer Triebschnee',25],
    [46.44,7.10,'firn',180,1000,1500,'Firn an tiefen Südhängen (Jura-Voralpen)',null],
    [46.68,8.59,'powder',0,1600,2200,'Andermatt — Waldabfahrten traumhaft',60],
    [46.63,8.66,'powder',45,1800,2400,'Gemsstock NE — knietief und leicht',50],
    [46.02,7.23,'powder',315,2400,3000,'Zermatt NW hoch oben noch top',35],
    [46.09,7.94,'suncrust',225,2000,2600,'Saastal SW — Deckel trägt am Morgen',null],
    [47.02,9.28,'scoured',270,2000,2600,'Ostschweiz — Kammlagen abgeblasen',null],
    [46.35,7.99,'drift',90,2200,2800,'Lötschental Osthänge — Triebschnee in Mulden',20],
    [46.90,8.40,'powder',0,1500,2100,'Engelberg Nordseiten, viel Neuschnee',65],
    [46.55,7.56,'powder',315,1400,2000,'Diemtigtal NW — leichter Pulver tief unten',30],
    [46.20,7.31,'firn',180,1600,2200,'Val d\'Anniviers — Firn ab 9:30',null],
    [46.66,9.36,'powder',0,1900,2500,'Piz Beverin Nordrinnen halten kalt',40],
    [46.83,8.39,'windpressed',45,2200,2800,'Sustengebiet NE — harter Winddeckel',null],
    [47.10,8.76,'wet',180,900,1500,'Voralpen Süd — ab Mittag Sulz',null]
  ];
  drawDemos.forEach((dd,k)=>{const la=dd[0],lo=dd[1],ty=dd[2],asp=dd[3],e0=dd[4],e1=dd[5],cap=dd[6],cm=dd[7];
    const snap=drawDemoSnap(DC[ty]||'#4aa3ff'),photo=(k%2===0)?demoImg(160+k*12,185+k*10,175+k*6):null;
    const meas=[DCL[ty],e0+'\u2013'+e1+' m',asp8(asp)].concat(cm?[cm+' cm']:[]).join(' · ');
    add({user:U2[(k*5+2)%U2.length],cat:'snow',sub:'Schnee-Karte',
      measurement:meas,caption:cap,lat:la,lng:lo,
      time:'vor '+(2+Math.floor(rnd()*40))+'h',img:photo||snap,
      condition_data:{draw:true,snapshot:snap,measurement:meas,demoFit:true,label0:DCL[ty],
        zones:[{type:ty,centroid:[la,lo],elevMin:e0,elevMax:e1,aspectDeg:asp,aspectConc:0.82,n:120,cm:cm}]}});});
})();
// Apply the feature switches to the entry points.
(function(){try{
  [['mapQr',FEATURES.quickPowder],['feedQr',FEATURES.quickPowder],
   ['mapDraw',FEATURES.draw],['mapFab',FEATURES.observation]].forEach(([id,on])=>{
    const el=document.getElementById(id);if(el)el.hidden=!on;});
}catch(e){}})();
let reportMarkers=L.layerGroup().addTo(map);
function demoActive(){try{if(location.search.indexOf('demo')>=0)return true;}catch(e){}return !sb;}
function demoToggle(){try{const u=new URL(location.href);
  if(u.searchParams.has('demo'))u.searchParams.delete('demo');else u.searchParams.set('demo','1');
  location.href=u.toString();}catch(e){}}
(function(){try{const b=document.getElementById('demoPill');if(!b)return;
  if(location.search.indexOf('demo')>=0){b.classList.add('on');document.getElementById('demoPillTxt').textContent='DEMO · 1.4.2026';}}catch(e){}})();
let allReports=demoActive()?[...DEMO_REPORTS]:[];
function _rptDepth(r){const cd=r.condition_data||{};let cm=(cd.quick&&cd.powderAmountCm!=null)?+cd.powderAmountCm:null;if(cm==null&&r.measurement){const mm=/(\d+)\s*cm/.exec(r.measurement);if(mm)cm=+mm[1];}return cm;}
function _rptColor(r){const cm=_rptDepth(r);if(cm!=null&&cm>=5){const sc=snowCol(cm);if(sc)return 'rgb('+sc.join(',')+')';}return CAT_COLORS[r.cat]||'#16152e';}
function _rptShort(r){const t=(r.sub||r.cat||'').toString();return t.length>11?t.slice(0,10)+'\u2026':t;}
function _rptAdd(lat,lng,html,w,h,onclick,zoff){
  const icon=L.divIcon({className:'rpt-ic',html:html,iconSize:[w,h],iconAnchor:[w/2,h/2]});
  const m=L.marker([lat,lng],{icon:icon,zIndexOffset:zoff||700}).addTo(reportMarkers);
  if(onclick)m.on('click',onclick);
  return m;}
function reportPopupHTML(r){
  const col=_rptColor(r),cm=_rptDepth(r);
  const catBadge='<span class="rpop-cat'+(r.img?'':' sm')+'" style="background:'+(CAT_COLORS[r.cat]||'#333')+'">'+(CAT_SVG[r.cat]||'')+(r.img?(' '+escapeHtml(r.sub||r.cat)):'')+'</span>';
  const stars=r.stars?'<span class="rpop-chip"><svg viewBox="0 0 24 24"><path d="M12 2l3 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.9 21l1.2-6.8-5-4.9 6.9-1z" fill="var(--warn)" stroke="none"/></svg>'+r.stars+'/5</span>':'';
  const other=(r.measurement&&!/\d+\s*cm/.test(r.measurement))?'<span class="rpop-chip">'+escapeHtml(r.measurement)+'</span>':'';
  return '<div class="rpop">'
    +(r.img?('<div class="rpop-img" onclick="feedOpenAt(\''+r.id+'\')"><img src="'+r.img+'" loading="lazy" alt=""/>'+catBadge+'</div>'):'')
    +'<div class="rpop-body"><div class="rpop-head">'
      +'<div class="rpop-av"'+(r.avatar?(' style="background-image:url('+r.avatar+')"'):'')+'>'+(r.avatar?'':escapeHtml((r.user||'U')[0].toUpperCase()))+'</div>'
      +'<div class="rpop-meta"><b>'+escapeHtml(r.user||'User')+'</b><span>'+escapeHtml(r.time||'')+'</span></div>'
      +(r.img?'':catBadge)
    +'</div>'
    +'<div class="rpop-chips">'
      +(cm!=null?('<span class="rpop-chip dep" style="--cc:'+col+'">'+cm+' cm</span>'):'')
      +other+stars
    +'</div>'
    +(r.caption?('<p class="rpop-cap">'+escapeHtml(r.caption)+'</p>'):'')
    +'<button class="rpop-cta" onclick="feedOpenAt(\''+r.id+'\')">Details ansehen \u2192</button>'
    +'</div></div>';}
// A drawn snow map is about the SNOW, so its marker shows the dominant snow
// type as an icon rather than a photo thumbnail -- and as a true circle at
// device resolution instead of a stretched, blurry photo.
function _rptDominantType(r){
  const z=(r.condition_data&&r.condition_data.zones)||[];
  if(!z.length)return null;
  const area={};z.forEach(q=>{if(q&&q.type)area[q.type]=(area[q.type]||0)+(q.n||1);});
  let best=null,bn=-1;for(const k in area)if(area[k]>bn){bn=area[k];best=k;}
  return best;
}
function _rptSnowIcon(type,size){
  const p=DRAW_PENS[type];
  const solid=(type==='powder');
  const bg=solid?('rgb('+((snowCol(30)||[74,163,255]).join(','))+')')
                :'url('+snowPatternURL(type)+') repeat';
  return '<span class="rpt-snow" style="background:'+bg+';background-size:'+(solid?'auto':'12px 12px')+'"></span>';
}
function _rptMarker(r,mode){
  const col=_rptColor(r);let html,w,h;
  const dom=_rptDominantType(r);
  if(mode==='photo'&&dom){
    html='<div class="rpt-snowmark" style="--cc:'+col+'" title="'+escapeHtml((DRAW_PENS[dom]&&DRAW_PENS[dom].label)||dom)+'">'+
      _rptSnowIcon(dom)+'</div>';w=h=52;}
  else if(mode==='photo'&&r.img){html='<div class="rpt-photo" style="--cc:'+col+'"><img src="'+r.img+'" loading="lazy" alt=""/><i class="rp-cat" style="background:'+(CAT_COLORS[r.cat]||'#333')+'">'+(CAT_SVG[r.cat]||'')+'</i></div>';w=h=58;}
  else if(mode==='chip'){const cm=_rptDepth(r);const lab=(cm!=null)?(cm+' cm'):_rptShort(r);html='<div class="rpt-chip" style="--cc:'+col+'">'+(CAT_SVG[r.cat]||'')+'<b>'+escapeHtml(lab)+'</b></div>';w=Math.round(30+lab.length*8.6);h=30;}
  else if(mode==='dot'){html='<div class="rpt-tiny" style="--cc:'+col+'"></div>';w=h=12;}
  else{html='<div class="rpt-pin" style="--cc:'+col+'">'+(CAT_SVG[r.cat]||'')+(r.img?'<i class="rp-dot"></i>':'')+'</div>';w=h=38;}
  const m=_rptAdd(r.lat,r.lng,html,w,h,null,700);
  m.bindPopup(reportPopupHTML(r),{maxWidth:236,className:'rpt-pop',closeButton:true});}
function _rptCluster(g,zoom){
  let lat=0,lng=0,dsum=0,dn=0,ph=0;
  g.forEach(r=>{lat+=r.lat;lng+=r.lng;const cm=_rptDepth(r);if(cm!=null){dsum+=cm;dn++;}if(r.img)ph++;});
  lat/=g.length;lng/=g.length;
  const sc=dn?snowCol(dsum/dn):null;const col=sc?('rgb('+sc.join(',')+')'):'#8C4F27';
  const n=g.length,sz=Math.min(60,32+Math.round(Math.sqrt(n)*7));
  const ph2=ph?('<i class="rc-ph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="12" cy="12" r="3"/></svg>'+ph+'</i>'):'';
  const html='<div class="rpt-cluster" style="width:'+sz+'px;height:'+sz+'px;--cc:'+col+'"><span>'+n+'</span>'+ph2+'</div>';
  _rptAdd(lat,lng,html,sz,sz,()=>map.flyTo([lat,lng],Math.min(15,zoom+2.3),{duration:.7}),500);}
function loadReportMarkers(){
  reportMarkers.clearLayers();
  if(!window._drawOv)window._drawOv=L.layerGroup().addTo(map);
  _drawOv.clearLayers();
  (allReports||[]).forEach(r=>{const cd=r.condition_data||{};const _im=cd.drawImage||r.img;if(cd.draw&&cd.bounds&&_im){try{L.imageOverlay(_im,cd.bounds,{opacity:DRAW_DIM,interactive:false}).addTo(_drawOv);}catch(e){}}});
  if(!allReports||!allReports.length)return;
  const z=map.getZoom(),cellPx=66,tiny=z<9.5,clusterOn=z<12.5,rich=z>=12.5;
  if(tiny){allReports.forEach(r=>_rptMarker(r,'dot'));return;}
  const cells={};
  allReports.forEach(r=>{let key;
    if(clusterOn){try{const pt=map.latLngToLayerPoint([r.lat,r.lng]);key=Math.round(pt.x/cellPx)+'|'+Math.round(pt.y/cellPx);}catch(e){key='s'+r.id;}}
    else key='s'+r.id;
    (cells[key]||(cells[key]=[])).push(r);});
  Object.keys(cells).forEach(k=>{const g=cells[k];
    if(clusterOn&&g.length>=2){_rptCluster(g,z);return;}
    g.forEach(r=>{ if(rich)_rptMarker(r,r.img?'photo':'chip'); else _rptMarker(r,'pin'); });});
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
      if(layer==='qprheat')renderQprHeat();if(layer==='prog')renderPrognosis(stat);if(layer==='powfind')renderPowderFind();if(layer==='progdiff')renderProgDiff();if(layer==='progpat')renderProgPattern(stat);}catch(e){}
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
      return{id:r.id,user:nameMap[r.user_id]||(r.user_id?.substring(0,8))||'User',userId:r.user_id,avatar:avatarMap[r.user_id]||null,cat:catId,icon:catObj?.icon||ic('pin'),sub:r.subtype,measurement:r.condition_data?.measurement||null,stars:r.condition_data?.stars||0,peak:r.condition_data?.peak||null,dest:r.condition_data?.dest||null,caption:r.caption,lat,lng,time:timeAgo(r.created_at),img:r.image_url,condition_data:r.condition_data||null,createdAt:r.created_at,likes:likeCount[r.id]||0,liked:!!likedByMe[r.id],comments:cmtCount[r.id]||0,
        condN:condAgg[r.id]?condAgg[r.id].n:0,condAvg:condAgg[r.id]?condAgg[r.id].sum/condAgg[r.id].n:0,condPow:condAgg[r.id]?condAgg[r.id].pow:0,myCond:myCond[r.id]||null,dbRow:true};
    }).filter(Boolean);
    allReports=demoActive()?[...dbR,...DEMO_REPORTS]:dbR;loadReportMarkers();
    if(document.getElementById('feedPage').classList.contains('open'))feedRender();
  }catch(e){console.warn('loadDbReports',e);}
}
// --- Delete one of my own reports --------------------------------------
// The report row is the anchor: comments, reactions, flags and condition
// ratings all reference it with ON DELETE CASCADE, so removing it takes the
// whole thread with it. RLS ("reports_delete_own") is what actually enforces
// that only the author can do this — the UI check is only for the button.
function storagePathFromUrl(u){
  try{const m=String(u||'').match(/\/report-images\/(.+)$/);
    return m?decodeURIComponent(m[1].split('?')[0]):null;}catch(e){return null;}
}
async function deleteReport(id,ev){
  if(ev){ev.stopPropagation();ev.preventDefault();}
  if(!sb||!sbUser){authShow();return;}
  const r=(allReports||[]).find(x=>String(x.id)===String(id));
  if(!r||!r.dbRow){toast('Dieser Beitrag kann nicht gelöscht werden','err');return;}
  if(r.userId&&r.userId!==sbUser.id){toast('Nur eigene Beiträge löschbar','err');return;}
  if(!confirm('Diesen Beitrag endgültig löschen? Kommentare, Bestätigungen und Bewertungen dazu verschwinden mit.'))return;
  try{
    const{error}=await sb.from('reports').delete().eq('id',id).eq('user_id',sbUser.id);
    if(error)throw error;
    // photos are best-effort: a failure here must not leave the row behind
    const paths=[];const cd=r.condition_data||{};
    [r.img,cd.drawImage].concat(cd.images||[]).forEach(u=>{const p=storagePathFromUrl(u);if(p)paths.push(p);});
    if(paths.length){try{await sb.storage.from('report-images').remove(paths);}catch(e){}}
    allReports=(allReports||[]).filter(x=>String(x.id)!==String(id));
    try{savedPosts.delete(String(id));}catch(e){}
    loadReportMarkers();feedRender();
    try{if(layer==='prog')renderPrognosis(stat);else if(layer==='powfind')renderPowderFind();
        else if(layer==='progdiff')renderProgDiff();else if(layer==='progpat')renderProgPattern(stat);}catch(e){}
    toast('Beitrag gelöscht','ok');haptic(12);
    loadDbReports();
  }catch(e){toast('Löschen fehlgeschlagen: '+(e.message||e),'err');}
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
if(sb){sb.auth.onAuthStateChange((ev,session)=>{
    if(ev==='PASSWORD_RECOVERY'){authUpdateUI(session?.user||null);authOnRecovery();return;}
    authUpdateUI(session?.user||null);});
  sb.auth.getSession().then(({data})=>{authUpdateUI(data.session?.user||null);});}
else{document.getElementById('feedBtn').style.display='flex';loadReportMarkers();}
let authPendingEmail=null,justLoggedIn=false;
function authShow(){authMode='login';authRender();document.getElementById('authOverlay').style.display='flex';}
function authHide(){document.getElementById('authOverlay').style.display='none';document.getElementById('authErr').textContent='';document.getElementById('authCodeErr').textContent='';}
function authToggle(){authMode=authMode==='login'?'register':'login';authRender();}
function authBackToForm(){authMode='login';authRender();}
function authForgot(){authMode='forgot';authRender();}
async function authSendRecovery(email){
  const errEl=document.getElementById('authErr');
  if(!email){errEl.textContent='Bitte E-Mail eingeben.';return;}
  const btn=document.getElementById('authSubmitBtn');btn.disabled=true;btn.textContent='Sende…';
  try{
    // Link-based reset: Supabase mails a recovery link that reopens the app;
    // detectSessionInUrl then fires PASSWORD_RECOVERY (see authOnRecovery).
    const{error}=await sb.auth.resetPasswordForEmail(email,{redirectTo:location.origin+location.pathname});
    if(error)throw error;
    authPendingEmail=email;authMode='sent';authRender();
  }catch(e){errEl.textContent=e.message||'Konnte E-Mail nicht senden.';}
  btn.disabled=false;authRender();
}
// The recovery link puts us back here with a temporary session -> let the user
// pick a new password straight away.
let authRecoveryPending=false;
function authOnRecovery(){
  authRecoveryPending=true;
  try{document.getElementById('authOverlay').style.display='flex';}catch(e){}
  authMode='newpass';authRender();
  try{history.replaceState(null,'',location.origin+location.pathname+location.search);}catch(e){}
  setTimeout(()=>{const np=document.getElementById('authNewPass');if(np)np.focus();},80);
}
(function(){try{const h=location.hash||'';
  if(/type=recovery/.test(h)||/[?&]type=recovery/.test(location.search))setTimeout(authOnRecovery,900);}catch(e){}})();
async function authSetNewPassword(){
  const err=document.getElementById('authNewPassErr');err.style.color='#A83A2E';err.textContent='';
  const a=document.getElementById('authNewPass').value,b=document.getElementById('authNewPass2').value;
  if(!a||a.length<8){err.textContent='Mindestens 8 Zeichen.';return;}
  if(a!==b){err.textContent='Die Passwörter stimmen nicht überein.';return;}
  const btn=document.getElementById('authNewPassBtn');btn.disabled=true;btn.textContent='Speichere…';
  try{
    const{error}=await sb.auth.updateUser({password:a});
    if(error)throw error;
    authRecoveryPending=false;toast('Passwort geändert — du bist angemeldet.','ok');authAfterLogin();
  }catch(e){err.textContent=e.message||'Konnte Passwort nicht ändern.';}
  btn.disabled=false;btn.textContent='Passwort speichern';
}
function authRender(){
  const isReg=authMode==='register',isCode=authMode==='code',isBio=authMode==='biometric';
  const isForgot=authMode==='forgot',isNew=authMode==='newpass',isSent=authMode==='sent';
  document.getElementById('authForm').style.display=(isCode||isBio||isNew||isSent)?'none':'flex';
  document.getElementById('authCodeBox').style.display=isCode?'block':'none';
  document.getElementById('authBioBox').style.display=isBio?'block':'none';
  document.getElementById('authNewPassBox').style.display=isNew?'block':'none';
  document.getElementById('authSentBox').style.display=isSent?'block':'none';
  document.getElementById('authSwitch').style.display=(isCode||isBio||isNew||isSent)?'none':'block';
  document.getElementById('authNote').style.display=(isCode||isBio||isNew||isSent||isForgot)?'none':'';
  document.getElementById('authClose').style.display=isBio?'none':'flex';
  document.getElementById('authPass').style.display=isForgot?'none':'';
  document.getElementById('authPass').required=!isForgot;
  document.getElementById('authForgotBtn').style.display=(isReg||isForgot||isSent)?'none':'block';
  if(isSent){document.getElementById('authTitle').textContent='E-Mail unterwegs';document.getElementById('authSub').style.display='none';
    document.getElementById('authSentMail').textContent=authPendingEmail||'deine E-Mail';return;}
  if(isNew){document.getElementById('authTitle').textContent='Neues Passwort';document.getElementById('authSub').style.display='none';return;}
  if(isForgot){document.getElementById('authTitle').textContent='Passwort zurücksetzen';
    document.getElementById('authSub').style.display='';
    document.getElementById('authSub').textContent='Wir senden dir einen Link zum Zurücksetzen an deine E-Mail.';
    document.getElementById('authUser').style.display='none';
    document.getElementById('authSubmitBtn').textContent='Link senden';
    document.getElementById('authSwitch').innerHTML='<button onclick="authBackToForm()">Zurück zur Anmeldung</button>';return;}
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
  const errEl=document.getElementById('authErr');errEl.textContent='';errEl.style.color='#A83A2E';
  if(authMode==='forgot'){await authSendRecovery(email);return;}
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
    else{document.getElementById('authCodeErr').style.color='#A83A2E';document.getElementById('authCodeErr').textContent='Kein 6-stelliger Code in der Zwischenablage.';}
  }catch(e){document.getElementById('authCodeErr').style.color='#A83A2E';document.getElementById('authCodeErr').textContent='Zwischenablage nicht verfügbar – Code manuell eingeben.';}
}
async function authVerifyCode(){
  if(!sb||!authPendingEmail)return;
  const token=document.getElementById('authCode').value.trim();const err=document.getElementById('authCodeErr');err.textContent='';
  if(token.length<6){err.style.color='#A83A2E';err.textContent='Bitte 6 Ziffern eingeben.';return;}
  const btn=document.getElementById('authCodeBtn');btn.disabled=true;btn.textContent='Prüfe…';
  try{
    let r=await sb.auth.verifyOtp({email:authPendingEmail,token,type:'signup'});
    if(r.error){const r2=await sb.auth.verifyOtp({email:authPendingEmail,token,type:'email'});if(r2.error)throw r2.error;}
    authAfterLogin();
  }catch(e){err.style.color='#A83A2E';err.textContent=(e.message||'Code ungültig oder abgelaufen.');}
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
    challenge,rp:{name:'Firn',id:location.hostname},
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
// --- Personal preferences ---------------------------------------------------
// Device-local, so they work before sign-in and never need a round trip.
const PREF_KEY='ssm_prefs_v1';
const PREF_DEFAULTS={labels:'on',start:'country',home:'',layer:'meteo:0',window:'48'};
let prefs=Object.assign({},PREF_DEFAULTS);
function prefsLoad(){try{const v=JSON.parse(localStorage.getItem(PREF_KEY)||'{}');
  prefs=Object.assign({},PREF_DEFAULTS,v||{});}catch(e){prefs=Object.assign({},PREF_DEFAULTS);}
  return prefs;}
function prefsSave(){try{localStorage.setItem(PREF_KEY,JSON.stringify(prefs));}catch(e){}}
function applyLabels(){try{const el=document.getElementById('map');
  if(el)el.classList.toggle('no-resort-labels',prefs.labels==='off');}catch(e){}}
function prefsApplyAll(){applyLabels();}
prefsLoad();
// --- Profile & Settings ---
let myProfile=null,profAvatarFile=null,profAvatarUrl=null;
function profNav(v){['Main','Pers','Priv','Notif','App'].forEach(k=>{const el=document.getElementById('profView'+k);if(el)el.style.display=(v===k.toLowerCase())?'':'none';});try{haptic(4);}catch(e){}}
function profSetVis(v){try{localStorage.setItem('ssm_visibility',v);}catch(e){}
  document.querySelectorAll('#profVis button').forEach(b=>b.classList.toggle('active',b.dataset.v===v));
  (async()=>{try{if(sb&&sbUser)await sb.from('profiles').update({visibility:v}).eq('id',sbUser.id);}catch(e){}})();
  toast('Sichtbarkeit: '+(v==='me'?'Nur ich':v==='friends'?'Freunde':'Alle'),'ok');}
// "Meine Beiträge" in the profile: every post with its own delete button, so a
// user can remove a single report instead of only the all-or-nothing wipe.
async function profLoadPosts(){
  const box=document.getElementById('profPosts');if(!box)return;
  if(!sb||!sbUser){box.innerHTML='';return;}
  const del=id=>'<button class="uv-del" title="Löschen" aria-label="Beitrag löschen" onclick="uvDeletePost(\''+id+'\',event)">'+
    '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg></button>';
  try{
    const{data:rs}=await sb.from('reports').select('id,caption,subtype,image_url,condition_data,location,created_at')
      .eq('user_id',sbUser.id).order('created_at',{ascending:false}).limit(40);
    if(!rs||!rs.length){box.innerHTML='<div class="prof-hint">Noch keine Beiträge.</div>';return;}
    const withImg=rs.filter(r=>r.image_url),noImg=rs.filter(r=>!r.image_url);
    const grid=withImg.length?('<div class="uv-grid">'+withImg.map(r=>{const ll=parseGeo(r.location);
      return '<div class="uv-cell" onclick="profClose();'+(ll?('feedFlyTo('+ll[0]+','+ll[1]+')'):'')+'"><img src="'+r.image_url+'" loading="lazy" alt=""/>'+del(r.id)+'</div>';}).join('')+'</div>'):'';
    const rows=noImg.map(r=>{const ll=parseGeo(r.location);const cap=escapeHtml(r.caption||r.subtype||'');
      const meta=escapeHtml((r.condition_data&&r.condition_data.measurement)||r.subtype||'Beitrag');
      return '<div class="uv-post own" onclick="profClose();'+(ll?('feedFlyTo('+ll[0]+','+ll[1]+')'):'')+'">'+
        '<div class="b"><b>'+meta+'</b>'+(cap?(' — '+cap):'')+'<div class="t">'+timeAgo(r.created_at)+'</div></div>'+del(r.id)+'</div>';}).join('');
    box.innerHTML=grid+rows;
  }catch(e){box.innerHTML='<div class="prof-hint">Beiträge konnten nicht geladen werden.</div>';}
}
// Delete straight from a profile list — the post may be older than the 60 rows
// the feed keeps in memory, so this deletes by id instead of via allReports.
async function uvDeletePost(id,ev){
  if(ev){ev.stopPropagation();ev.preventDefault();}
  if(!sb||!sbUser)return;
  if(!confirm('Diesen Beitrag endgültig löschen? Kommentare, Bestätigungen und Bewertungen dazu verschwinden mit.'))return;
  try{
    let row=null;try{const{data}=await sb.from('reports').select('image_url,condition_data').eq('id',id).single();row=data;}catch(e){}
    const{error}=await sb.from('reports').delete().eq('id',id).eq('user_id',sbUser.id);
    if(error)throw error;
    const paths=[];const cd=(row&&row.condition_data)||{};
    [row&&row.image_url,cd.drawImage].concat(cd.images||[]).forEach(u=>{const p=storagePathFromUrl(u);if(p)paths.push(p);});
    if(paths.length){try{await sb.storage.from('report-images').remove(paths);}catch(e){}}
    allReports=(allReports||[]).filter(x=>String(x.id)!==String(id));
    loadReportMarkers();try{feedRender();}catch(e){}
    toast('Beitrag gelöscht','ok');haptic(12);
    profLoadPosts();loadDbReports();
  }catch(e){toast('Löschen fehlgeschlagen: '+(e.message||e),'err');}
}
async function profDeleteContent(btn){if(!sb||!sbUser)return;
  if(!confirm('Wirklich ALLE deine Beiträge (inkl. Kommentare und Bewertungen dazu) löschen? Dein Konto bleibt bestehen.'))return;
  btn.disabled=true;
  try{await sb.from('reports').delete().eq('user_id',sbUser.id);toast('Alle Beiträge gelöscht','ok');loadReportMarkers();loadDbReports();profLoadPosts();}
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
  profAvatarFile=null;profLoadPosts();profRenderPrefs();
  try{const d=document.getElementById('profDisplay');if(d)d.value=name;}catch(e){}
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
let _profDirty=false;
function profDirty(){_profDirty=true;const b=document.getElementById('profSave');if(b)b.classList.add('on');}
// A segmented control that writes straight into prefs and applies immediately,
// so the user sees the effect (especially the theme) while the sheet is open.
function profSeg(id,key,after){
  const box=document.getElementById(id);if(!box)return;
  box.querySelectorAll('button').forEach(b=>{
    b.classList.toggle('on',b.dataset.v===String(prefs[key]));
    b.onclick=()=>{prefs[key]=b.dataset.v;prefsSave();
      box.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
      if(after)after();try{haptic(4);}catch(e){}};});
}
function profRenderPrefs(){
  prefsLoad();
  profSeg('profLabels','labels',applyLabels);
  profSeg('profStart','start');
  profSeg('profWindow','window');
  const home=document.getElementById('profHome');
  if(home&&!home._filled){home._filled=true;
    home.innerHTML='<option value="">— kein Heimatgebiet —</option>'+
      CH_RESORTS.slice().sort((a,b)=>a.name.localeCompare(b.name))
        .map(r=>'<option value="'+r.name+'">'+r.name+'</option>').join('');}
  if(home)home.value=prefs.home||'';
  const lay=document.getElementById('profLayer');
  if(lay&&!lay._filled){lay._filled=true;
    lay.innerHTML=Object.keys(GROUPS).map(g=>GROUPS[g].items.map((it,i)=>
      '<option value="'+g+':'+i+'">'+GROUPS[g].tag+' · '+it.label+'</option>').join('')).join('');}
  if(lay)lay.value=prefs.layer||'meteo:0';
}
// Applied once at startup: window length, opening layer and where the map lands.
function prefsApplyStartup(){
  try{
    const w=parseInt(prefs.window||'48');
    if(w&&w!==(b-a)){windowSize=w;a=Math.max(0,Math.min(T-w,nowIdx-Math.floor(w/2)));b=Math.min(T,a+w);}
    const lv=(prefs.layer||'meteo:0').split(':');
    if(GROUPS[lv[0]])setTopic(lv[0],parseInt(lv[1])||0,0);
    if(prefs.start==='home'&&prefs.home){
      const r=CH_RESORTS.find(x=>x.name===prefs.home);
      if(r){map.setView([r.lat,r.lng],12.5,{animate:false});updateBaseFade();}
    }else if(prefs.start==='last'){
      const v=JSON.parse(localStorage.getItem('ssm_lastview')||'null');
      if(v&&v.lat!=null){map.setView([v.lat,v.lng],v.z,{animate:false});updateBaseFade();}
    }
    applyLabels();
  }catch(e){}
}
try{map.on('moveend',()=>{try{const c=map.getCenter();
  localStorage.setItem('ssm_lastview',JSON.stringify({lat:c.lat,lng:c.lng,z:map.getZoom()}));}catch(e){}});}catch(e){}
function profSetAvatar(inp){if(!inp.files||!inp.files[0])return;profAvatarFile=inp.files[0];
  const url=URL.createObjectURL(profAvatarFile);const av=document.getElementById('profAv');av.classList.add('has-img');av.style.backgroundImage='url('+url+')';}
async function profTogglePush(){
  const t=document.getElementById('profPush');const willOn=!t.classList.contains('on');
  if(willOn&&'Notification'in window){try{const perm=await Notification.requestPermission();if(perm!=='granted'){alert('Benachrichtigungen wurden nicht erlaubt. Bitte im Browser aktivieren.');return;}}catch(e){}}
  t.classList.toggle('on',willOn);
}
async function profSave(){
  try{const d=document.getElementById('profDisplay');
    const home=document.getElementById('profHome'),lay=document.getElementById('profLayer');
    if(home)prefs.home=home.value;if(lay)prefs.layer=lay.value;prefsSave();
    if(d&&d.value.trim()&&sb&&sbUser){await sb.auth.updateUser({data:{username:d.value.trim()}});
      await sb.from('profiles').update({username:d.value.trim()}).eq('id',sbUser.id);
      document.getElementById('profName').textContent=d.value.trim();}
  }catch(e){}
  _profDirty=false;
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
  const body=encodeURIComponent('\n\n---\nFirn\n'+(navigator.userAgent||''));
  window.location.href='mailto:giani.morf@bluewin.ch?subject='+encodeURIComponent('Firn Feedback')+'&body='+body;}
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
    // Own profile: every post gets a delete button (RLS still enforces ownership).
    const mine=!!(sbUser&&uid===sbUser.id);
    const delBtn=id=>mine?('<button class="uv-del" title="Löschen" aria-label="Beitrag löschen" onclick="uvDeletePost(\''+id+'\',event)">'+
      '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg></button>'):'';
    const withImg=(rs||[]).filter(r=>r.image_url),noImg=(rs||[]).filter(r=>!r.image_url);
    const grid=withImg.length?('<div class="uv-grid">'+withImg.map(r=>{const ll=parseGeo(r.location);
      return '<div class="uv-cell" onclick="userViewClose();'+(ll?('feedFlyTo('+ll[0]+','+ll[1]+')'):'')+'"><img src="'+r.image_url+'" loading="lazy" alt=""/>'+delBtn(r.id)+'</div>';}).join('')+'</div>'):'';
    const rows=noImg.map(r=>{const ll=parseGeo(r.location);const cap=escapeHtml(r.caption||r.subtype||'');
      const meta=escapeHtml((r.condition_data&&r.condition_data.measurement)||r.subtype||'');
      return '<div class="uv-post'+(mine?' own':'')+'" onclick="userViewClose();'+(ll?('feedFlyTo('+ll[0]+','+ll[1]+')'):'')+'">'+
        '<div class="b"><b>'+meta+'</b>'+(cap?(' — '+cap):'')+'<div class="t">'+timeAgo(r.created_at)+'</div></div>'+delBtn(r.id)+'</div>';}).join('');
    posts.innerHTML='<h3>Beiträge · '+ids.length+'</h3>'+((grid+rows)||'<div style="color:var(--mut);font-size:13px;text-align:center;padding:14px 0">Noch keine Beiträge.</div>');
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
    a2.download='firn-daten-'+new Date().toISOString().slice(0,10)+'.json';
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
 {id:'avalanche',label:'Lawine',sub:'Spontan oder ausgelöst',color:'#A83A2E',tint:'rgba(168,58,46,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M4 20l6-13 4 7"/><path d="M14 20c1-3 3-5 6-6"/></svg>'},
 {id:'whumpf',label:'Wumm-Geräusch',sub:'Setzungsgeräusche im Schnee',color:'#e8590c',tint:'rgba(232,89,12,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18"/><circle cx="9" cy="15" r="2"/><path d="M14 9c2 1 3 3 3 5M17 5c3 2 4 6 4 10"/></svg>'},
 {id:'wind_slab',label:'Triebschnee',sub:'Windverfrachteter Schnee',color:'#0d9488',tint:'rgba(13,148,136,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18h18M4 18l7-9 5 6"/><path d="M9.6 4.6A2 2 0 1 1 11 8H2"/></svg>'},
 {id:'snow',label:'Schneequalität',sub:'Pulver · Harsch · Firn · Nassschnee',color:'#7d6bd6',tint:'rgba(42,138,176,.14)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M4 6l16 12M20 6L4 18"/><path d="M12 2l-2.5 2.5M12 2l2.5 2.5M12 22l-2.5-2.5M12 22l2.5-2.5M4 6l.2 3.4M4 6l3.4-.2M20 18l-.2-3.4M20 18l-3.4.2M20 6l-3.4-.2M20 6l-.2 3.4M4 18l3.4.2M4 18l.2-3.4"/></svg>'},
 {id:'other',label:'Andere Beobachtung',sub:'Freie Geländemeldung',color:'#8C4F27',tint:'rgba(140,79,39,.12)',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M5 20l5-9 4 6 5-9"/><path d="M15 5h6v5"/></svg>'}
];
const SNOW_KINDS=[
 {k:'powder',l:'Pulver',c:'#3E7C8C'},
 {k:'wind_powder',l:'Triebschnee-Pulver',c:'#7d6bd6'},
 {k:'wind_pressed',l:'Windharsch',c:'#0d9488'},
 {k:'melt_crust',l:'Schmelzharsch',c:'#e8590c'},
 {k:'wet',l:'Nassschnee',c:'#7b5cff'},
 {k:'firn',l:'Firn',c:'#C08A2E'}
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
function obsDEM(lat,lon){const fe=fineElev(lat,lon),fa=fineAspectDeg(lat,lon);
  const cx2=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy2=Math.round((laMax-lat)/(laMax-laMin)*(H-1));
  let ce=null,ca=null;if(cx2>=0&&cx2<W&&cy2>=0&&cy2<H){const p=cy2*W+cx2;ce=Math.round(melevv(p));ca=aspectQ(maspv(p));}
  const el=(fe!=null?Math.round(fe):ce),as=(fa!=null?asp8(fa):ca);
  if(el==null&&as==null)return{};return{elev:el,aspect:as};}
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
    (FEATURES.quickPowder?'<button class="obs-quick" style="--i:5" onclick="obsClose();qrOpen()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg><span><b>Quick Powder Report</b><em>Ein Fingertipp — Menge &amp; Qualität</em></span></button>':'');
  obsWarmLocation();
}
// ===== Draw-based snow report v3 (paint condition zones on the map) =====
const _SVG_SNOW='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M4.2 6.5l15.6 11M4.2 17.5l15.6-11"/></svg>';
const _SVG_ROUTE='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20c4-1 5-5 8-5s5 4 8 3"/><circle cx="4" cy="20" r="1.6"/><circle cx="20" cy="18" r="1.6"/></svg>';
const _SVG_TRACKS='<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="7" cy="6" r="1.4"/><circle cx="12" cy="6" r="1.4"/><circle cx="17" cy="6" r="1.4"/><circle cx="7" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="17" cy="12" r="1.4"/><circle cx="7" cy="18" r="1.4"/><circle cx="12" cy="18" r="1.4"/><circle cx="17" cy="18" r="1.4"/></svg>';
const _SVG_ERASER='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 20H21M15.5 5.5l3 3L9 18H6l-2.5-2.5a1.5 1.5 0 0 1 0-2.1L13 3.9a1.5 1.5 0 0 1 2.1 0z"/></svg>';
const _CRUST_LABELS=['super leicht','mühsam','tragend'];
// Deep days happen: every snow-depth input runs to 150 cm, and the top step is
// an open-ended "150+" bucket rather than a hard ceiling.
const SNOW_CM_MAX=150;
function snowCmLabel(v){return (+v>=SNOW_CM_MAX?SNOW_CM_MAX+'+':(''+v));}
// Route colours: uphill is the darker orange, downhill the bright one.
const ROUTE_UP_COL='#b45309',ROUTE_DOWN_COL='#fb923c';
const TRACK_LABELS=['< 3 Personen','< 10 Personen','> 20 Personen'];
const DRAW_PENS={
 powder:{id:'powder',label:'Powder',color:'#4aa3ff',kind:'zone',slfDepth:true,slider:{label:'Tiefe',min:5,max:SNOW_CM_MAX,step:5,unit:' cm',val:30}},
 drift:{id:'drift',label:'Triebschnee',color:'#e8590c',kind:'zone',slider:{label:'Mächtigkeit',min:5,max:SNOW_CM_MAX,step:5,unit:' cm',val:20}},
 compact:{id:'compact',label:'Kompakt',color:'#8aa0c8',kind:'zone'},
 wet:{id:'wet',label:'Nassschnee',color:'#f59e0b',kind:'zone',slider:{label:'Nässe',min:1,max:5,step:1,unit:'/5',val:3}},
 suncrust:{id:'suncrust',label:'Sonnendeckel',color:'#f4d03f',kind:'zone',slider:{label:'Deckel',min:1,max:3,step:1,val:2,labels:_CRUST_LABELS}},
 windpressed:{id:'windpressed',label:'Windgepresst',color:'#b9c4d4',kind:'zone',slider:{label:'Deckel',min:1,max:3,step:1,val:2,labels:_CRUST_LABELS}},
 firn:{id:'firn',label:'Firn',color:'#3fb27f',kind:'zone'},
 scoured:{id:'scoured',label:'Abgeweht',color:'#9a8778',kind:'zone',dash:[24,20]},
 icy:{id:'icy',label:'Eisig',color:'#38bdf8',kind:'zone',dash:[24,20]},
 nosnow:{id:'nosnow',label:'Kein Schnee',color:'#e0483c',kind:'zone',dash:[9,16]},
 // One route pen: each segment is coloured from the DEM, so the app decides
 // uphill (dark orange) vs downhill (bright orange) instead of the user.
 route:{id:'route',label:'Route',color:ROUTE_DOWN_COL,kind:'route'},
 tracks:{id:'tracks',label:'Verspurung',color:'#1f2937',kind:'tracks',slider:{label:'Verspurt',min:1,max:3,step:1,val:2,labels:TRACK_LABELS}},
 eraser:{id:'eraser',label:'Radierer',color:'#eef2f7',kind:'eraser'}
};
const DRAW_CATS=[
 {id:'snow',label:'Schnee',icon:_SVG_SNOW,pens:['powder','drift','compact','wet','suncrust','windpressed','firn','scoured','icy','nosnow']},
 {id:'route',label:'Route',icon:_SVG_ROUTE,pens:['route']},
 {id:'tracks',label:'Spuren',icon:_SVG_TRACKS,pens:['tracks']},
 {id:'eraser',label:'Radierer',icon:_SVG_ERASER,pens:['eraser']}
];
let drawCtx=null,drawPen=DRAW_PENS.powder,drawCat='snow',drawPanOn=false;
let drawZoneCanvas=null,drawZoneW=0,drawZoneH=0,drawDpr=1,drawRefBounds=null;
let drawRoutes=[],drawTracks=[],drawZoneUsed=new Set(),drawHistory=[];
let _drawPainting=false,_drawLastPt=null,_drawCurRoute=null,_drawCurTrack=null,_drawDashAcc=0;
let _drawTrackGrid=new Set(),drawTrackZoom=null;
let drawBrushSize=34,drawZoneSamples={},_drawLastTrackC=null,_drawBtmH='';
const DRAW_ZONE_W=34; // base zone brush width (CSS px)

function drawHasContent(){return drawZoneUsed.size||drawRoutes.length||drawTracks.length;}
function drawFitCanvas(){
  // Pin the canvas exactly onto the map container so that canvas pixel (x,y)
  // IS map container point (x,y). Without this the canvas covers the whole
  // viewport while #map stops at --btm-h, and the drawing lands offset.
  const cv=document.getElementById('drawCanvas');
  try{const mr=map.getContainer().getBoundingClientRect();
    cv.style.left=mr.left+'px';cv.style.top=mr.top+'px';
    cv.style.width=mr.width+'px';cv.style.height=mr.height+'px';
    cv.style.right='auto';cv.style.bottom='auto';}catch(e){}
  return cv;
}
function drawViewBounds(){return map.getBounds();}
function drawRefRect(){
  if(!drawRefBounds)return {x:0,y:0,w:drawZoneW/drawDpr||1,h:drawZoneH/drawDpr||1};
  try{const nw=map.latLngToContainerPoint(drawRefBounds.getNorthWest());
    const se=map.latLngToContainerPoint(drawRefBounds.getSouthEast());
    return {x:nw.x,y:nw.y,w:(se.x-nw.x)||1,h:(se.y-nw.y)||1};}catch(e){return {x:0,y:0,w:1,h:1};}
}
function drawC2O(cx,cy){const r=drawRefRect();return [(cx-r.x)/r.w*drawZoneW,(cy-r.y)/r.h*drawZoneH];}
function drawOScale(){const r=drawRefRect();return drawZoneW/r.w;}

function drawOpen(){
  drawRoutes=[];drawTracks=[];drawZoneUsed=new Set();drawHistory=[];drawPanOn=false;_drawPainting=false;drawZoneCanvas=null;_drawTrackGrid=new Set();drawTrackZoom=null;drawZoneSamples={};
  document.body.classList.remove('draw-pan');document.body.classList.add('draw-on');
  document.getElementById('drawWrap').style.display='block';
  _drawBtmH=document.documentElement.style.getPropertyValue('--btm-h');
  document.documentElement.style.setProperty('--btm-h','0px');
  try{map.invalidateSize({animate:false,pan:false});}catch(e){}
  try{map.dragging.disable();map.touchZoom.disable();map.doubleClickZoom.disable();map.scrollWheelZoom.disable();}catch(e){}
  const pb=document.getElementById('drawPan');if(pb){pb.classList.remove('on');pb.querySelector('span').textContent='Karte bewegen';}
  drawSetupCanvas();drawRenderCats();drawSelectCat('snow');drawShowBrush();
  try{haptic(8);}catch(e){}
  const h=document.getElementById('drawHint');h.classList.add('show');setTimeout(()=>h.classList.remove('show'),3200);
}
function drawClose(){
  if(drawHasContent()&&!confirm('Zeichnung verwerfen?'))return;
  document.getElementById('drawWrap').style.display='none';
  document.body.classList.remove('draw-on','draw-pan');drawPanOn=false;
  if(_drawBtmH)document.documentElement.style.setProperty('--btm-h',_drawBtmH);
  try{map.invalidateSize({animate:false,pan:false});}catch(e){}
  try{map.dragging.enable();map.touchZoom.enable();map.doubleClickZoom.enable();if(_desktop)map.scrollWheelZoom.enable();}catch(e){}
}
function drawTogglePan(){
  drawPanOn=!drawPanOn;document.body.classList.toggle('draw-pan',drawPanOn);
  const btn=document.getElementById('drawPan'),lbl=btn?btn.querySelector('span'):null;
  if(drawPanOn){if(btn)btn.classList.add('on');if(lbl)lbl.textContent='Fertig — zeichnen';
    try{map.dragging.enable();map.touchZoom.enable();map.doubleClickZoom.enable();if(_desktop)map.scrollWheelZoom.enable();}catch(e){}
  }else{if(btn)btn.classList.remove('on');if(lbl)lbl.textContent='Karte bewegen';
    try{map.dragging.disable();map.touchZoom.disable();map.doubleClickZoom.disable();map.scrollWheelZoom.disable();}catch(e){}
    drawReanchor();
  }
  try{haptic(6);}catch(e){}
}
function drawSetupCanvas(){
  const cv=drawFitCanvas();
  drawDpr=Math.min(window.devicePixelRatio||1,2);
  const w=cv.clientWidth,h=cv.clientHeight;
  cv.width=Math.round(w*drawDpr);cv.height=Math.round(h*drawDpr);
  drawCtx=cv.getContext('2d');drawCtx.setTransform(drawDpr,0,0,drawDpr,0,0);
  drawZoneW=Math.round(w*drawDpr);drawZoneH=Math.round(h*drawDpr);
  drawZoneCanvas=document.createElement('canvas');drawZoneCanvas.width=drawZoneW;drawZoneCanvas.height=drawZoneH;
  try{drawRefBounds=drawViewBounds();}catch(e){drawRefBounds=null;}
  try{if(drawTrackZoom==null)drawTrackZoom=map.getZoom();}catch(e){drawTrackZoom=13;}
  if(!cv._wired){cv._wired=true;
    const pt=e=>{const r=cv.getBoundingClientRect();return [e.clientX-r.left,e.clientY-r.top];};
    cv.addEventListener('pointerdown',e=>{if(drawPanOn)return;try{cv.setPointerCapture(e.pointerId);}catch(_){}
      _drawPainting=true;drawPushHistory();drawStrokeBegin(pt(e));drawRepaint();try{haptic(4);}catch(_){}});
    cv.addEventListener('pointermove',e=>{if(drawPanOn||!_drawPainting)return;drawStrokeExtend(pt(e));drawRepaint();});
    const end=()=>{if(!_drawPainting)return;_drawPainting=false;_drawCurRoute=null;_drawCurTrack=null;_drawLastPt=null;};
    cv.addEventListener('pointerup',end);cv.addEventListener('pointercancel',end);cv.addEventListener('pointerleave',end);
  }
  if(!map._drawMoveBound){map._drawMoveBound=true;
    map.on('move zoom moveend zoomend',()=>{if(document.body.classList.contains('draw-on'))drawRepaint();});}
  drawRepaint();
}
function drawReanchor(){
  if(!drawZoneCanvas)return;const r=drawRefRect();
  const nc=document.createElement('canvas');nc.width=drawZoneW;nc.height=drawZoneH;
  const nctx=nc.getContext('2d');
  nctx.drawImage(drawZoneCanvas,0,0,drawZoneW,drawZoneH,r.x*drawDpr,r.y*drawDpr,r.w*drawDpr,r.h*drawDpr);
  drawZoneCanvas=nc;try{drawRefBounds=drawViewBounds();}catch(e){}
  drawRepaint();
}
function drawStrokeBegin(p){
  const k=drawPen.kind;
  if(k==='zone'||k==='eraser'){_drawDashAcc=0;_drawLastPt=drawC2O(p[0],p[1]);drawCommitSeg(_drawLastPt,_drawLastPt);if(k==='zone')drawSampleAt(p);}
  else if(k==='route'){const ll=map.containerPointToLatLng(p);
    _drawCurRoute={pen:drawPen.id,pts:[ll],elev:[routeElevAt(ll)],dir:[0]};drawRoutes.push(_drawCurRoute);}
  else if(k==='tracks'){_drawLastTrackC=p;drawTrackAt(p[0],p[1]);}
}
function drawStrokeExtend(p){
  const k=drawPen.kind;
  if(k==='zone'||k==='eraser'){const o=drawC2O(p[0],p[1]);drawCommitSeg(_drawLastPt,o);_drawLastPt=o;if(k==='zone')drawSampleAt(p);}
  else if(k==='route'&&_drawCurRoute)routeAddPoint(_drawCurRoute,map.containerPointToLatLng(p));
  else if(k==='tracks'){drawTrackStep(p);}
}
// Verspurung: place a stipple dot on a fixed geo grid (brush keeps constant
// spacing; passing over the same spot twice never adds a second dot).
function drawTrackGap(level){return level===1?30:(level===2?18:11);}
// A brush that stamps a fixed geo-grid mesh of dots within the brush radius —
// keeps constant spacing and never double-stamps the same cell.
function drawTrackAt(cx,cy){
  const level=drawPen.slider.val,gap=drawTrackGap(level);
  let ll;try{ll=map.containerPointToLatLng([cx,cy]);}catch(e){return;}
  const z=drawTrackZoom!=null?drawTrackZoom:map.getZoom();
  let pz;try{pz=map.project(ll,z);}catch(e){return;}
  const rad=Math.max(gap*0.6,(drawBrushSize/2)*Math.pow(2,z-map.getZoom()));
  const cgx=Math.round(pz.x/gap),cgy=Math.round(pz.y/gap),span=Math.ceil(rad/gap);
  for(let gy=cgy-span;gy<=cgy+span;gy++)for(let gx=cgx-span;gx<=cgx+span;gx++){
    const ppx=gx*gap,ppy=gy*gap;if(Math.hypot(ppx-pz.x,ppy-pz.y)>rad)continue;
    const key=gap+':'+gx+'_'+gy;if(_drawTrackGrid.has(key))continue;_drawTrackGrid.add(key);
    let snapped;try{snapped=map.unproject(L.point(ppx,ppy),z);}catch(e){continue;}
    drawTracks.push({ll:snapped,level:level});drawRecordSample('tracks',snapped.lat,snapped.lng);
  }
}
function drawTrackStep(p){
  const last=_drawLastTrackC||p,dx=p[0]-last[0],dy=p[1]-last[1],dist=Math.hypot(dx,dy);
  const step=Math.max(3,drawBrushSize*0.35),n=Math.max(1,Math.ceil(dist/step));
  for(let i=1;i<=n;i++)drawTrackAt(last[0]+dx*i/n,last[1]+dy*i/n);
  _drawLastTrackC=p;
}
// --- Route direction from the terrain ---------------------------------------
// Which way the line crosses the contours decides the colour, so a single pen
// draws an ascent and a descent without the user switching anything. The
// comparison is against a point ROUTE_LOOKBACK samples back, because
// neighbouring samples are only metres apart and their elevation difference is
// pure DEM noise.
const ROUTE_LOOKBACK=6,ROUTE_DEADBAND=4;   // samples, metres
function routeElevAt(ll){try{const e=fineElev(ll.lat,ll.lng);if(e!=null)return e;
  const d=drawDemFull(ll.lat,ll.lng);return d&&d.elev!=null?d.elev:null;}catch(e){return null;}}
function routeAddPoint(rt,ll){
  rt.pts.push(ll);rt.elev.push(routeElevAt(ll));
  const i=rt.pts.length-1,j=Math.max(0,i-ROUTE_LOOKBACK);
  const e1=rt.elev[i],e0=rt.elev[j];
  let dir=0;
  if(e1!=null&&e0!=null){const dz=e1-e0;
    if(dz>ROUTE_DEADBAND)dir=1;else if(dz<-ROUTE_DEADBAND)dir=-1;
    else dir=rt.dir[i-1]||0;}                    // inside the deadband: hold
  else dir=rt.dir[i-1]||0;
  rt.dir.push(dir);
}
function routeSegColor(dir){return dir>0?ROUTE_UP_COL:ROUTE_DOWN_COL;}
function routeSummary(rt){
  let up=0,down=0;for(let i=1;i<rt.dir.length;i++){if(rt.dir[i]>0)up++;else down++;}
  return up>down*1.15?'Aufstieg':(down>up*1.15?'Abfahrt':'Route (auf + ab)');
}
function drawSampleAt(p){let ll;try{ll=map.containerPointToLatLng(p);}catch(e){return;}drawRecordSample(drawPen.id,ll.lat,ll.lng);}
function drawRecordSample(type,lat,lng){if(!type)return;const a=(drawZoneSamples[type]=drawZoneSamples[type]||[]);if(a.length<600)a.push([lat,lng]);}
function drawDemFull(lat,lon){const cx=Math.round((lon-loMin)/(loMax-loMin)*(W-1)),cy=Math.round((laMax-lat)/(laMax-laMin)*(H-1));
  const fe=fineElev(lat,lon),fa=fineAspectDeg(lat,lon);
  let ce=null,ca=null,cs=null;if(cx>=0&&cx<W&&cy>=0&&cy<H){const q=cy*W+cx;ce=melevv(q);ca=maspv(q);cs=mslpv(q);}
  const el=(fe!=null?fe:ce),ad=(fa!=null?fa:ca),fs=fineSlope(lat,lon);if(el==null&&ad==null)return null;
  return{elev:el,aspectDeg:ad,slope:(fs!=null?fs:(cs!=null?cs:0))};}
function drawZoneLabel(zones){if(!zones||!zones.length)return '';
  return zones.map(z=>{const t=(typeof DRAW_PENS!=='undefined'&&DRAW_PENS[z.type]&&DRAW_PENS[z.type].label)||z.type;const parts=[t];
    if(z.elevMin!=null&&z.elevMax!=null)parts.push(z.elevMin+'\u2013'+z.elevMax+' m');
    const a=asp8(z.aspectDeg);if(a)parts.push(a);return parts.join(' \u00b7 ');}).join(' / ');}
function drawComputeZones(){
  const zones=[];
  for(const type in drawZoneSamples){const pts=drawZoneSamples[type];if(!pts||pts.length<2)continue;
    // Only snow-type pens describe conditions. Verspurung is a usage marker, and
    // if it leaked in here the prognosis would read it as a snow type that
    // CONFLICTS with the powder painted on the same slope.
    if(!DRAW_PENS[type]||DRAW_PENS[type].kind!=='zone')continue;
    let slat=0,slng=0,emin=1e9,emax=-1e9,sx=0,sy=0,na=0,ssl=0,ssl2=0,ns=0;
    for(const pr of pts){const la=pr[0],lo=pr[1];slat+=la;slng+=lo;const d=drawDemFull(la,lo);if(!d)continue;
      if(d.elev!=null){if(d.elev<emin)emin=d.elev;if(d.elev>emax)emax=d.elev;}
      if(d.slope!=null){ssl+=d.slope;ssl2+=d.slope*d.slope;ns++;}
      if(d.slope>=6&&d.aspectDeg!=null){const r=d.aspectDeg*Math.PI/180;sx+=Math.cos(r);sy+=Math.sin(r);na++;}}
    const _pen=DRAW_PENS[type];
    const _cm=(_pen&&_pen.slider&&(type==='powder'||type==='drift'))?_pen.slider.val:null;
    const _sMean=ns?ssl/ns:null;
    const _sSd=(ns>1)?Math.sqrt(Math.max(0,ssl2/ns-_sMean*_sMean)):null;
    zones.push({type:type,centroid:[slat/pts.length,slng/pts.length],
      elevMin:emin<1e9?Math.round(emin):null,elevMax:emax>-1e9?Math.round(emax):null,
      aspectDeg:na?((Math.atan2(sy,sx)*180/Math.PI)+360)%360:null,aspectConc:na?Math.hypot(sx,sy)/na:0,
      slope:_sMean!=null?Math.round(_sMean*10)/10:null,
      slopeSd:_sSd!=null?Math.round(_sSd*10)/10:null,
      cm:_cm,n:pts.length});
  }
  return zones;
}
function drawRebuildTrackGrid(){
  _drawTrackGrid=new Set();const z=drawTrackZoom!=null?drawTrackZoom:map.getZoom();
  drawTracks.forEach(d=>{const gap=drawTrackGap(d.level);let pz;try{pz=map.project(d.ll,z);}catch(e){return;}
    _drawTrackGrid.add(gap+':'+Math.round(pz.x/gap)+'_'+Math.round(pz.y/gap));});
}
function drawCommitSeg(a,b){
  const octx=drawZoneCanvas.getContext('2d');octx.lineJoin='round';
  const scale=drawOScale(),isEr=drawPen.kind==='eraser';
  if(isEr){octx.globalCompositeOperation='destination-out';octx.lineCap='round';octx.setLineDash([]);
    octx.strokeStyle='rgba(0,0,0,1)';octx.lineWidth=drawBrushSize*scale;
  }else{octx.globalCompositeOperation='destination-over'; // no overpainting: new paint stays under existing
    // Powder is the one type that paints as a solid colour (the SLF depth
    // scale); every other snow type paints as its texture, so the drawing is
    // readable without colour.
    let col;
    if(drawPen.slfDepth)col='rgb('+((snowCol(drawPen.slider.val)||[74,163,255]).join(','))+')';
    else if(SNOW_PATTERNS[drawPen.id])col=drawPenPattern(octx,drawPen.id);
    else col=drawPen.color;
    octx.strokeStyle=col;octx.lineWidth=drawBrushSize*scale;
    if(drawPen.dash){octx.lineCap='butt';octx.setLineDash(drawPen.dash.map(d=>d*scale));octx.lineDashOffset=-_drawDashAcc;}
    else{octx.lineCap='round';octx.setLineDash([]);octx.lineDashOffset=0;}
  }
  octx.beginPath();octx.moveTo(a[0],a[1]);octx.lineTo(b[0]+.01,b[1]);octx.stroke();
  if(!isEr&&drawPen.dash)_drawDashAcc+=Math.hypot(b[0]-a[0],b[1]-a[1]);
  octx.globalCompositeOperation='source-over';octx.setLineDash([]);octx.lineDashOffset=0;
  if(!isEr)drawZoneUsed.add(drawPen.id);
}
// Canvas patterns are per-context, so they are cached per context+type.
const _drawPatCache=new WeakMap();
function drawPenPattern(ctx,type){
  let m=_drawPatCache.get(ctx);if(!m){m={};_drawPatCache.set(ctx,m);}
  if(!m[type]){const tile=snowPatternTile(type,'#1d2430','rgba(246,248,251,.88)');
    m[type]=ctx.createPattern(tile,'repeat');}
  return m[type];
}
// The drawing is painted at DRAW_DIM while you work, and the version that gets
// posted must look the same -- it used to be exported at .85, so the posted
// snapshot was noticeably heavier than what the user actually drew.
const DRAW_DIM=0.5;
function drawRepaint(expAlpha){
  const ctx=drawCtx;if(!ctx)return;const cv=document.getElementById('drawCanvas');
  const W=cv.clientWidth,H=cv.clientHeight;ctx.clearRect(0,0,W,H);
  if(drawZoneCanvas){const r=drawRefRect();ctx.globalAlpha=expAlpha||DRAW_DIM;
    ctx.drawImage(drawZoneCanvas,0,0,drawZoneW,drawZoneH,r.x,r.y,r.w,r.h);ctx.globalAlpha=1;}
  drawPaintTrack(ctx);
  drawRoutes.forEach(rt=>drawPaintRoute(ctx,rt));
}
function drawPaintRoute(ctx,rt){
  let pts;try{pts=rt.pts.map(ll=>map.latLngToContainerPoint(ll));}catch(e){return;}
  if(!pts.length)return;
  ctx.lineCap='round';ctx.lineJoin='round';ctx.globalAlpha=1;ctx.setLineDash([]);
  // white casing first so the route reads on any base map
  ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));
  if(pts.length===1)ctx.lineTo(pts[0].x+.01,pts[0].y);
  ctx.strokeStyle=cvTok('--paper','#F7F4EE');ctx.lineWidth=9;ctx.stroke();
  if(pts.length===1){ctx.strokeStyle=routeSegColor(rt.dir?rt.dir[0]:0);ctx.lineWidth=5;ctx.stroke();return;}
  // then one stroke per run of equal direction (uphill / downhill)
  const dirs=rt.dir||[];ctx.lineWidth=5;
  let i=1;
  while(i<pts.length){
    const d=dirs[i]||0;let j=i;
    while(j+1<pts.length&&(dirs[j+1]||0)===d)j++;
    ctx.beginPath();ctx.moveTo(pts[i-1].x,pts[i-1].y);
    for(let k=i;k<=j;k++)ctx.lineTo(pts[k].x,pts[k].y);
    ctx.strokeStyle=routeSegColor(d);ctx.stroke();
    i=j+1;
  }
}
// Convex hull (monotone chain) — the painted mesh points define an area, and
// the hull is what gets outlined.
function drawHull(pts){
  if(pts.length<3)return pts.slice();
  const p=pts.slice().sort((a,b)=>a.x-b.x||a.y-b.y);
  const cross=(o,a,b)=>(a.x-o.x)*(b.y-o.y)-(a.y-o.y)*(b.x-o.x);
  const lo=[],up=[];
  for(const q of p){while(lo.length>=2&&cross(lo[lo.length-2],lo[lo.length-1],q)<=0)lo.pop();lo.push(q);}
  for(let i=p.length-1;i>=0;i--){const q=p[i];
    while(up.length>=2&&cross(up[up.length-2],up[up.length-1],q)<=0)up.pop();up.push(q);}
  lo.pop();up.pop();return lo.concat(up);
}
// Verspurung is shown as a bounded, dashed area with how many people have been
// through it written in the middle — not as a stipple of dots.
function drawPaintTrack(ctx){
  if(!drawTracks.length)return;
  const byLevel={};
  drawTracks.forEach(d=>{let pt;try{pt=map.latLngToContainerPoint(d.ll);}catch(e){return;}
    (byLevel[d.level]||(byLevel[d.level]=[])).push(pt);});
  ctx.globalAlpha=1;
  Object.keys(byLevel).forEach(lv=>{
    const pts=byLevel[lv],hull=drawHull(pts);
    if(hull.length<3){ctx.fillStyle='rgba(31,41,55,.8)';
      pts.forEach(q=>{ctx.beginPath();ctx.arc(q.x,q.y,2.2,0,6.28318);ctx.fill();});return;}
    ctx.beginPath();hull.forEach((q,i)=>i?ctx.lineTo(q.x,q.y):ctx.moveTo(q.x,q.y));ctx.closePath();
    ctx.fillStyle='rgba(31,41,55,.10)';ctx.fill();
    ctx.setLineDash([9,7]);ctx.lineJoin='round';
    ctx.strokeStyle=cvTok('--paper','#F7F4EE');ctx.lineWidth=5;ctx.stroke();
    ctx.strokeStyle='#1f2937';ctx.lineWidth=2.4;ctx.stroke();
    ctx.setLineDash([]);
    let cx=0,cy=0;hull.forEach(q=>{cx+=q.x;cy+=q.y;});cx/=hull.length;cy/=hull.length;
    const txt=TRACK_LABELS[(+lv)-1]||('Level '+lv);
    ctx.font='800 12px Inter,system-ui,sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
    const w=ctx.measureText(txt).width+16;
    ctx.fillStyle=cvTok('--paper','#F7F4EE');
    if(ctx.roundRect){ctx.beginPath();ctx.roundRect(cx-w/2,cy-11,w,22,8);ctx.fill();
      ctx.strokeStyle='#1f2937';ctx.lineWidth=1.2;ctx.stroke();}
    else ctx.fillRect(cx-w/2,cy-11,w,22);
    ctx.fillStyle='#1f2937';ctx.fillText(txt,cx,cy);
  });
}
function drawPushHistory(){
  if(!drawZoneCanvas)return;const c=document.createElement('canvas');c.width=drawZoneW;c.height=drawZoneH;
  c.getContext('2d').drawImage(drawZoneCanvas,0,0);
  drawHistory.push({zone:c,nR:drawRoutes.length,nT:drawTracks.length,used:[...drawZoneUsed]});
  if(drawHistory.length>18)drawHistory.shift();
}
function drawUndo(){
  if(!drawHistory.length)return;const h=drawHistory.pop();
  const octx=drawZoneCanvas.getContext('2d');octx.setTransform(1,0,0,1,0,0);
  octx.clearRect(0,0,drawZoneW,drawZoneH);octx.drawImage(h.zone,0,0);
  drawRoutes.length=h.nR;drawTracks.length=h.nT;drawZoneUsed=new Set(h.used);drawRebuildTrackGrid();
  drawRepaint();try{haptic(5);}catch(e){}
}
function drawClearSilent(){
  if(drawZoneCanvas)drawZoneCanvas.getContext('2d').clearRect(0,0,drawZoneW,drawZoneH);
  drawRoutes=[];drawTracks=[];drawZoneUsed.clear();drawHistory=[];_drawTrackGrid=new Set();drawZoneSamples={};
}
function drawClear(){if(!drawHasContent())return;if(!confirm('Ganze Zeichnung löschen?'))return;drawClearSilent();drawRepaint();}
function drawRenderCats(){
  const el=document.getElementById('drawCats');
  el.innerHTML=DRAW_CATS.map(c=>'<button class="dc-tab" data-cat="'+c.id+'">'+c.icon+'<span>'+c.label+'</span></button>').join('');
  el.querySelectorAll('.dc-tab').forEach(b=>b.addEventListener('click',()=>drawSelectCat(b.dataset.cat)));
}
function drawSelectCat(id){
  drawCat=id;const cat=DRAW_CATS.find(c=>c.id===id)||DRAW_CATS[0];
  document.querySelectorAll('#drawCats .dc-tab').forEach(b=>b.classList.toggle('on',b.dataset.cat===id));
  drawRenderPens(cat);drawSelectPen(cat.pens[0]);try{haptic(4);}catch(e){}
}
function drawRenderPens(cat){
  const el=document.getElementById('drawPens');
  el.innerHTML=cat.pens.map(pid=>{const p=DRAW_PENS[pid];
    let style='',extra='';
    if(SNOW_PATTERNS[p.id]){                       // pattern types: show the texture
      style='background-color:#f2f5f9;background-image:url('+snowPatternURL(p.id)+');background-size:14px 14px';
    }else{style='background:'+p.color;}            // powder (and non-snow pens)
    if(p.kind==='eraser')extra=';border-style:dashed;border-color:#9aa4b2';
    if(p.kind==='route')extra=';border-color:'+p.color;
    return '<button class="dt-sw" data-pen="'+p.id+'"><i style="'+style+extra+'"></i><span>'+p.label+'</span></button>';
  }).join('');
  el.querySelectorAll('.dt-sw').forEach(b=>b.addEventListener('click',()=>drawSelectPen(b.dataset.pen)));
}
function drawSelectPen(id){
  drawPen=DRAW_PENS[id]||DRAW_PENS.powder;
  document.querySelectorAll('#drawPens .dt-sw').forEach(b=>b.classList.toggle('on',b.dataset.pen===id));
  drawShowSlider();drawWireVertical();try{haptic(4);}catch(e){}
}
// The horizontal in-bar brush row is replaced by the vertical slider; the old
// element stays in the DOM (hidden) so nothing that references it breaks.
function drawShowBrush(){const box=document.getElementById('drawBrush');if(box)box.style.display='none';drawWireVertical();}
let _drawVWired=false;
function drawWireVertical(){
  const bv=document.getElementById('drawBrushV'),dv=document.getElementById('drawDepthV');
  if(!bv||!dv)return;
  if(!_drawVWired){_drawVWired=true;
    const bi=bv.querySelector('input');
    bi.oninput=function(){drawBrushSize=+this.value;drawSyncBrushV();};
    const di=dv.querySelector('input');
    di.oninput=function(){const sl=drawDepthSlider();if(sl){sl.val=+this.value;
      if(drawPen.slfDepth){const c=snowCol(sl.val);if(c){const sw=document.querySelector('#drawPens .dt-sw.on i');if(sw)sw.style.background='rgb('+c.join(',')+')';}}}
      drawSyncDepthV();};
  }
  drawSyncBrushV();drawSyncDepthV();
}
// Which pen slider the right-hand snow box drives (a depth in cm).
function drawDepthSlider(){const sl=drawPen.slider;
  return (sl&&(sl.unit===' cm'))?sl:null;}
function drawSyncBrushV(){
  const bv=document.getElementById('drawBrushV');if(!bv)return;
  bv.classList.toggle('off',drawPen.kind==='route');       // a route has a fixed width
  bv.querySelector('input').value=drawBrushSize;
  bv.querySelector('#drawBrushVVal').textContent=drawBrushSize;
  const dot=bv.querySelector('.dbv-dot i');
  if(dot){const d=Math.max(4,Math.min(30,drawBrushSize*0.38));dot.style.width=d+'px';dot.style.height=d+'px';}
}
function drawSyncDepthV(){
  const dv=document.getElementById('drawDepthV');if(!dv)return;
  const sl=drawDepthSlider();
  dv.classList.toggle('off',!sl);
  if(!sl)return;
  const inp=dv.querySelector('input');
  inp.min=sl.min;inp.max=sl.max;inp.step=sl.step;inp.value=sl.val;
  dv.querySelector('#drawDepthVVal').textContent=snowCmLabel(sl.val);
  const c=snowCol(sl.val);
  dv.style.borderColor=c?('rgb('+c.join(',')+')'):'';
  dv.style.background=c?('rgba('+c.join(',')+',.20)'):'';
}
function drawSliderTxt(sl){if(sl.labels)return sl.labels[sl.val-1];
  return (sl.unit===' cm'?snowCmLabel(sl.val):sl.val)+(sl.unit||'');}
function drawShowSlider(){
  const box=document.getElementById('drawSlider'),sl=drawPen.slider;
  // cm depths live in the vertical snow box on the right; only the other
  // sliders (crust strength, track density, wetness) need this row.
  if(!sl||drawDepthSlider()){box.style.display='none';return;}
  box.style.display='flex';
  box.innerHTML='<label>'+sl.label+'</label><input type="range" min="'+sl.min+'" max="'+sl.max+'" step="'+sl.step+'" value="'+sl.val+'"><b id="drawSliderVal">'+drawSliderTxt(sl)+'</b>';
  const inp=box.querySelector('input');
  inp.oninput=function(){sl.val=+this.value;document.getElementById('drawSliderVal').textContent=drawSliderTxt(sl);
    if(drawPen.slfDepth){const c=snowCol(sl.val);if(c){const sw=document.querySelector('#drawPens .dt-sw.on i');if(sw)sw.style.background='rgb('+c.join(',')+')';}}};
}
function drawSummary(){
  const out=[];
  drawZoneUsed.forEach(id=>{const p=DRAW_PENS[id];if(!p)return;let n=p.label;
    if(p.slider)n+=' ('+drawSliderTxt(p.slider)+')';out.push(n);});
  const rk=new Set(drawRoutes.map(routeSummary));rk.forEach(k=>out.push(k));
  if(drawTracks.length){const lv=drawTracks[drawTracks.length-1].level;out.push('Verspurt: '+TRACK_LABELS[lv-1]);}
  return out;
}
let drawPhotoFile=null,drawSnapData=null,drawPaintData=null,drawFinCen=null,drawFinBnds=null,drawFinNames=null,drawFinPeak=null;
function drawPaintingPNG(){try{drawRepaint(DRAW_DIM);const png=document.getElementById('drawCanvas').toDataURL('image/png');drawRepaint();return png;}catch(e){return null;}}
// Composite the map behind the drawing. Base tiles are re-loaded through fresh
// crossOrigin images so the canvas is never tainted (a non-CORS tile simply
// fails to load and is skipped rather than blanking the whole snapshot).
async function drawMapSnapshot(){
  const cv=document.getElementById('drawCanvas'),W=cv.clientWidth,H=cv.clientHeight,dpr=drawDpr;
  const mk=()=>{const s=document.createElement('canvas');s.width=Math.round(W*dpr);s.height=Math.round(H*dpr);
    const x=s.getContext('2d');x.scale(dpr,dpr);x.fillStyle='#e8eef4';x.fillRect(0,0,W,H);return {s:s,x:x};};
  try{
    const cr=cv.getBoundingClientRect();
    const tiles=[...map.getContainer().querySelectorAll('.leaflet-tile-pane img')]
      .filter(im=>im.complete&&im.naturalWidth&&im.src&&im.src.indexOf('data:')!==0)
      .map(im=>{const r=im.getBoundingClientRect();let op=1;try{op=parseFloat(getComputedStyle(im.parentNode).opacity);if(isNaN(op))op=1;}catch(e){}
        return {src:im.src,x:r.left-cr.left,y:r.top-cr.top,w:r.width,h:r.height,op:op};});
    const loaded=await Promise.all(tiles.map(t=>new Promise(res=>{
      const ni=new Image();let to;const done=v=>{clearTimeout(to);res(v);};
      ni.crossOrigin='anonymous';ni.onload=()=>done(Object.assign(t,{img:ni}));ni.onerror=()=>done(null);
      to=setTimeout(()=>done(null),3000);ni.src=t.src;})));
    const g=mk();
    loaded.forEach(t=>{if(t&&t.img){try{g.x.globalAlpha=t.op;g.x.drawImage(t.img,t.x,t.y,t.w,t.h);}catch(e){}}});
    g.x.globalAlpha=1;
    drawRepaint(.92);g.x.drawImage(cv,0,0,W,H);drawRepaint();
    return g.s.toDataURL('image/png');
  }catch(e){
    try{const g=mk();drawRepaint(.92);g.x.drawImage(cv,0,0,W,H);drawRepaint();return g.s.toDataURL('image/png');}catch(_){return drawPaintData||null;}
  }
}
async function drawOpenFinish(){
  if(!drawHasContent()){toast('Zeichne zuerst mindestens eine Zone ein.','err');return;}
  drawFinNames=drawSummary();
  try{const vb=drawViewBounds();drawFinBnds=[[vb.getSouth(),vb.getWest()],[vb.getNorth(),vb.getEast()]];const cc=vb.getCenter();drawFinCen={lat:cc.lat,lng:cc.lng};}catch(e){drawFinCen={lat:46.8,lng:8.2};drawFinBnds=null;}
  drawPhotoFile=null;
  drawPaintData=drawPaintingPNG();drawSnapData=null;
  const img=document.getElementById('drawSnapImg');if(img)img.src=drawPaintData||'';
  const pv=document.getElementById('drawPhotoPrev');if(pv){pv.style.display='none';pv.src='';}
  drawFinPeak=null;const ta=document.getElementById('drawCaption');
  if(ta){ta.value='';const op=ta.getAttribute('placeholder');
    try{const pk=nearestPeak(drawFinCen.lat,drawFinCen.lng);if(pk){drawFinPeak=pk;ta.value=pk+' — ';}}catch(e){}
    const auto=ta.value;ta.setAttribute('placeholder','Berg wird von der Karte erkannt …');
    ocrPeakFromMap().then(nm=>{try{ta.setAttribute('placeholder',op||'');if(nm&&(ta.value===auto||ta.value==='')){ta.value=nm+' — ';drawFinPeak=nm;}}catch(e){}}).catch(()=>{try{ta.setAttribute('placeholder',op||'');}catch(e){}});}
  const pb=document.getElementById('drawPhotoBtn');if(pb)pb.style.display='';
  document.getElementById('drawFinish').style.display='flex';
  try{haptic(6);}catch(e){}
  try{drawSnapData=await drawMapSnapshot();if(img&&drawSnapData)img.src=drawSnapData;}catch(e){}
}
function drawFinishClose(){document.getElementById('drawFinish').style.display='none';}
function drawPhotoPick(inp){if(!inp.files||!inp.files[0])return;drawPhotoFile=inp.files[0];inp.value='';
  const pv=document.getElementById('drawPhotoPrev');if(pv){try{pv.src=URL.createObjectURL(drawPhotoFile);}catch(e){}pv.style.display='block';}
  const pb=document.getElementById('drawPhotoBtn');if(pb)pb.style.display='none';try{haptic(6);}catch(e){}}
async function drawPublish(){
  const cap=(document.getElementById('drawCaption').value||'').trim()||null;
  const names=drawFinNames||drawSummary(),cen=drawFinCen||{lat:46.8,lng:8.2};
  const zns=drawComputeZones();const zlabel=drawZoneLabel(zns)||names.join(', ');
  const cd={draw:true,bounds:drawFinBnds,types:names,measurement:zlabel,snapshot:drawSnapData||null,drawImage:drawPaintData||null,zones:zns,peak:drawFinPeak||null};
  if(demoActive()||!sb){
    let photoUrl=null;if(drawPhotoFile){try{photoUrl=URL.createObjectURL(drawPhotoFile);}catch(e){}}
    allReports.unshift({id:'draw'+Date.now(),user:'Du',cat:'snow',sub:'Schnee-Karte',measurement:cd.measurement,caption:cap,lat:cen.lat,lng:cen.lng,time:'gerade eben',img:photoUrl||drawSnapData||drawPaintData,likes:0,comments:0,stars:0,condition_data:cd});
    loadReportMarkers();drawFinishClose();drawClearSilent();drawClose();toast('Schnee-Karte gespeichert (Demo).','ok');return;
  }
  if(!sbUser){authShow();return;}
  const btn=document.getElementById('drawPublishBtn');btn.disabled=true;btn.textContent='Postet…';
  try{
    async function up(src,ext,ct){try{const blob=typeof src==='string'?await (await fetch(src)).blob():src;
      const path=sbUser.id+'/draw'+Date.now()+'_'+Math.random().toString(36).slice(2,7)+'.'+ext;
      const{error}=await sb.storage.from('report-images').upload(path,blob,{contentType:ct});
      if(error)return null;const{data:ud}=sb.storage.from('report-images').getPublicUrl(path);return ud?.publicUrl||null;}catch(e){return null;}}
    let photoUrl=null;if(drawPhotoFile){const dw=await downscaleImage(drawPhotoFile);photoUrl=await up(dw,'jpg',(dw&&dw.type)||'image/jpeg');}
    const paintUrl=drawPaintData?await up(drawPaintData,'png','image/png'):null;
    const snapUrl=drawSnapData?await up(drawSnapData,'png','image/png'):null;
    cd.snapshot=snapUrl;cd.drawImage=paintUrl;
    const row={user_id:sbUser.id,location:'POINT('+cen.lng+' '+cen.lat+')',image_url:photoUrl||snapUrl||paintUrl,
      primary_categories:['snow'],subtype:'Schnee-Karte',condition_data:cd,
      caption:cap,completion_score:(cap||photoUrl)?70:50,captured_at:new Date().toISOString()};
    const{error}=await sb.from('reports').insert(row);if(error)throw error;
    toast('Schnee-Karte gepostet — danke!','ok');try{haptic(12);}catch(e){}drawFinishClose();drawClearSilent();drawClose();loadDbReports();
  }catch(e){toast('Posten fehlgeschlagen: '+(e.message||e),'err');}
  btn.disabled=false;btn.textContent='Veröffentlichen';
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
  return '<div class="obs-fld"><div class="obs-fld-l">'+label+' <b class="snow-val" id="sv_'+field+'">'+snowCmLabel(v)+' cm</b></div><input type="range" class="obs-range" min="0" max="'+SNOW_CM_MAX+'" step="5" value="'+v+'" oninput="obsState.snow.'+field+'=+this.value;var l=document.getElementById(\'sv_'+field+'\');if(l)l.textContent=snowCmLabel(this.value)+\' cm\'"></div>';}
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
  swissBaseLayer().addTo(obsMap);
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
  if(navigator.share){navigator.share({title:'Firn',text,url}).catch(()=>{});return;}
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
// The feed is a screen now: "open the feed" means "go to the feed screen".
function feedRefresh(){
  try{localStorage.setItem('ssm_feed_seen',String(Date.now()));const fd=document.querySelector('#feedBtn .feed-dot');if(fd)fd.classList.remove('on');}catch(e){}
  document.getElementById('feedScope').innerHTML=FEED_SCOPES.map(s=>
    `<button data-s="${s.id}" class="${feedScope===s.id?'active':''}" onclick="feedSetScope('${s.id}')">${s.icon}${s.label}</button>`).join('');
  document.getElementById('feedFilter').innerHTML=['all','avalanche','whumpf','wind_slab','other'].map(f=>{
    const lbl=f==='all'?'Alle':(catSvg(f,14)+' '+catLabel(f));
    return`<button class="${feedFilter===f?'active':''}" onclick="feedSetFilter('${f}')">${lbl}</button>`;}).join('');
  document.getElementById('feedLoc').style.display=feedScope==='near'?'flex':'none';
  feedRender();
}
function feedOpen(){feedRefresh();scrGo('feed');}
function feedClose(){scrGo('search');}
function feedCreatePost(){if(!sb||!sbUser){authShow();return;}scrGo('report');}
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
    swissBaseLayer().addTo(qrMapObj);
    const wrap=document.getElementById('qrMap').parentElement;
    if(!wrap.querySelector('.obs-pin'))wrap.insertAdjacentHTML('beforeend','<div class="obs-pin"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="rgba(224,36,94,.14)"/><circle cx="12" cy="10" r="3"/></svg></div>');
    miniMapTools(qrMapObj,wrap);
    qrMapObj.on('moveend',()=>{const cc=qrMapObj.getCenter();qrLL=[cc.lat,cc.lng];
      const d=obsDEM(cc.lat,cc.lng);
      document.getElementById('qrLocTxt').textContent='Manuell gesetzt'+(d.elev?(' · '+d.elev+' m'):'');});
    setTimeout(()=>{try{qrMapObj.invalidateSize();}catch(e){}},120);}}
function qrSync(){const b2=document.getElementById('qrNextBtn');if(b2)b2.disabled=!(qrAmount!==null&&qrQuality!==null);}
function qrQuadLabel(){const q=qrQuality==null?0:qrQuality;
  const i=Math.min(4,Math.floor(q/20)),f=(q-i*20)/20;   // Band + Position im Band
  const names=['Winddeckel','Sonnendeckel','Durchnässter Pulver','Gealterter Pulver','Pulver'];
  if(i<=1)return (f<0.33?'Starker ':f>0.67?'Leichter ':'')+names[i];
  if(i===2)return f<0.33?'Stark durchnässt':f>0.67?'Leicht durchnässt':names[i];
  return names[i]+(f>0.67?' · top':'');}
function qrPadAttach(){const pad=document.getElementById('qrPad');if(!pad||pad._wired)return;pad._wired=true;
  pad.style.touchAction='none';
  function setFrom(e){const r=pad.getBoundingClientRect();
    const fx=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
    const fy=Math.max(0,Math.min(1,(e.clientY-r.top)/r.height));
    qrQuality=Math.round(fx*100);              // X: Qualität (links kompakt → rechts fluffy)
    qrAmount=Math.round((1-fy)*SNOW_CM_MAX/5)*5;   // Y: Menge in 5-cm-Schritten (0–150)
    const dot=document.getElementById('qrPadDot');
    dot.style.display='block';dot.style.left=(fx*100)+'%';dot.style.top=(100-qrAmount/SNOW_CM_MAX*100)+'%';
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
const _SNAP_ICON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:11px;height:11px;vertical-align:-1px"><path d="M12 19l7-7 2.5 2.5-7 7L12 22l-2.5-.5z"/><path d="M15.5 6.5l2 2"/></svg>';
function feedVisual(r,col){
  const snap=r.condition_data&&r.condition_data.snapshot;
  if(r.img&&snap&&snap!==r.img){
    return '<div class="fc-wrap"><div class="fc-carousel" id="fcar-'+r.id+'">'+
      '<div class="fc-slide"><img src="'+r.img+'" alt="" loading="lazy" decoding="async"/></div>'+
      '<div class="fc-slide"><img src="'+snap+'" alt="" loading="lazy" decoding="async"/><span class="fc-snap-tag">'+_SNAP_ICON+' Zeichnung auf Karte</span></div>'+
      '</div><div class="fc-dots" id="fcd-'+r.id+'"><i class="on"></i><i></i></div></div>';
  }
  if(r.img)return '<div class="feed-card-visual" onclick="feedImgTap(\''+r.id+'\',event)"><img src="'+r.img+'" alt="" loading="lazy" decoding="async"/></div>';
  if(r.caption)return '<div class="feed-tx" style="background:linear-gradient(150deg,'+col+'14,rgba(255,255,255,0) 75%)"><span class="feed-tx-bar" style="background:'+col+'"></span>'+escapeHtml(r.caption)+'</div>';
  return '';
}
function feedWireCarousels(){document.querySelectorAll('.fc-carousel').forEach(c=>{if(c._w)return;c._w=1;
  const dots=document.getElementById('fcd-'+c.id.slice(5));
  c.addEventListener('scroll',()=>{const i=Math.round(c.scrollLeft/Math.max(1,c.clientWidth));
    if(dots)dots.querySelectorAll('i').forEach((d,k)=>d.classList.toggle('on',k===i));},{passive:true});});}
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
    // Avatars are chrome: a neutral tile with the initial, never a category hue.
    const avatarBg='var(--ink-100)';
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
      ${feedVisual(r,col)}
      <div class="feed-card-body">
        <div class="feed-card-badges">
          <span class="feed-badge cat-${r.cat}">${catSvg(r.cat,14)} ${escapeHtml(r.sub||r.cat)}</span>
          ${r.measurement?`<span class="feed-badge cat-${r.cat}">${escapeHtml(r.measurement)}</span>`:''}
          ${r.stars?`<span class="feed-badge cat-${r.cat}"><svg class="ic-i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2l3 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.9 21l1.2-6.8-5-4.9 6.9-1z" fill="var(--warn)" stroke="none"/></svg> ${r.stars}/5</span>`:''}
        </div>
        ${r.img&&r.caption?`<div class="feed-card-caption"><b>${escapeHtml(r.user)}</b> ${escapeHtml(r.caption)}</div>`:''}
      </div>
      <div class="feed-card-actions">
        ${endBtn}
        ${r.dbRow?`<button onclick="openComments('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.8-.8L3 21l1.9-5.2A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/></svg> ${r.comments||0}</button>`:''}
        <button onclick="event.stopPropagation();feedFlyTo(${r.lat},${r.lng})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg> Karte</button>
        <button title="Teilen" aria-label="Teilen" onclick="sharePost('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></button>
        <button class="save-btn${savedPosts.has(String(r.id))?' saved':''}" title="Merken" aria-label="Merken" onclick="toggleSave('${r.id}',event)"><svg viewBox="0 0 24 24" fill="${savedPosts.has(String(r.id))?'currentColor':'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg></button>
        ${r.dbRow&&(!sbUser||r.userId!==sbUser.id)?`<button class="flag-btn ${r.flaggedByMe?'flagged':''}" title="Melden" aria-label="Report melden" onclick="reportFlag('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg></button>`:''}
        ${r.dbRow&&sbUser&&r.userId===sbUser.id?`<button class="del-btn" title="Löschen" aria-label="Beitrag löschen" onclick="deleteReport('${r.id}',event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg></button>`:''}
      </div>
    </div>`;
  }).join('');
  feedAnimateCards();feedWireCarousels();
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
