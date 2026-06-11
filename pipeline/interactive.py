"""Interaktive Neuschnee-Akkumulationskarte (wePowder-/SLF-artig).

Idee: Statt EIN fixes 24/72h-Fenster zu rechnen, wird das Neuschnee-INKREMENT
je Zeitschritt (Default 3 h) ueber einen Datumsbereich vorberechnet. Alle Frames
werden kompakt (uint8) in eine self-contained HTML eingebettet. Im Browser waehlt
der Nutzer Startzeit + Fensterlaenge (24/48/72 h); JavaScript SUMMIERT die
betroffenen Frames fliessend auf und faerbt das Ergebnis in der festen
SLF-Skala ein - alles client-seitig, ohne Server.

Das Anzeige-Raster ist bewusst grob (Default ~0.025 deg ~ 2 km), damit die
Datenmenge browsertauglich bleibt (wePowder/SLF rendern interaktiv ebenfalls
nicht in 10 m). Der hochaufgeloeste 10-m-Export bleibt der statischen Pipeline
vorbehalten.
"""
from __future__ import annotations

import base64
import json
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple

import numpy as np

from config.settings import (
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_MODEL,
    OPEN_METEO_URL,
    ARCHIVE_THRESHOLD_DAYS,
    OUTPUT_DIR,
    SWITZERLAND_BBOX_WGS84,
    load_model_params,
)
from data_connectors.open_meteo_client import OpenMeteoClient
from model import factors
from model.interpolation import idw_grid, idw_direction_grid
from model.snow_model import WeatherGrid, compute_new_snow
from model.terrain_features import compute_terrain_features
from pipeline.overlay_export import SLF_BOUNDS, SLF_COLORS


def build_interactive_map(
    date: str | None,
    days: int = 5,
    step_hours: int = 3,
    grid_step_deg: float = 0.025,
    weather_step_deg: float = 0.2,
    use_synthetic_weather: bool = False,
    out_html: Path | None = None,
) -> Path:
    """Erzeugt die interaktive HTML-Akkumulationskarte fuer die ganze Schweiz."""
    params = load_model_params()
    start = (datetime.strptime(date, "%Y-%m-%d").date() if date else date_cls.today())

    # --- Anzeige-Raster (regelmaessiges lat/lon-Gitter) ------------------- #
    b = SWITZERLAND_BBOX_WGS84
    lons = np.arange(b["lon_min"], b["lon_max"], grid_step_deg)
    lats = np.arange(b["lat_min"], b["lat_max"], grid_step_deg)
    grid_lon, grid_lat = np.meshgrid(lons, lats)
    # Norden oben: Zeilen absteigend nach lat sortieren (row 0 = lat_max).
    grid_lat = grid_lat[::-1]
    ny, nx = grid_lon.shape
    lat_max, lat_min = float(grid_lat[0, 0]), float(grid_lat[-1, 0])
    lon_min, lon_max = float(grid_lon[0, 0]), float(grid_lon[0, -1])

    # --- Hoehe + Terrain auf dem Anzeige-Raster --------------------------- #
    elevation = _elevation_on_grid(grid_lon, grid_lat, use_synthetic_weather)
    res_m = grid_step_deg * 111320.0  # ~ Meter pro Gradschritt (Naeherung)
    terrain = compute_terrain_features(elevation, res_m)

    # --- Stuendliches Wetter ueber den Bereich holen ---------------------- #
    wx_lats, wx_lons = _weather_points(b, weather_step_deg)
    points_lonlat = np.column_stack([wx_lons, wx_lats])
    end = start + timedelta(days=days - 1)

    if use_synthetic_weather:
        from data_connectors.synthetic_weather import synthetic_forecast
        wx_elev = _elevation_at_points(wx_lons, wx_lats, use_synthetic_weather=True)
        forecasts = synthetic_forecast(wx_lats, wx_lons, wx_elev, hours=days * 24)
    else:
        base_url, _ = _endpoint(start.isoformat())
        client = OpenMeteoClient(base_url, model=OPEN_METEO_MODEL)
        forecasts = client.fetch(
            wx_lats, wx_lons, start_date=start.isoformat(), end_date=end.isoformat()
        )

    # --- Pro Zeitschritt das Neuschnee-Inkrement rechnen ------------------ #
    P = len(forecasts)
    H = min(len(f.time) for f in forecasts)
    precip = np.array([f.precipitation[:H] for f in forecasts])
    snow = np.array([f.snowfall[:H] for f in forecasts])
    temp = np.array([f.temperature_2m[:H] for f in forecasts])
    wspd = np.array([f.wind_speed_10m[:H] for f in forecasts])
    wdir = np.array([f.wind_direction_10m[:H] for f in forecasts])
    pelev = np.array([f.elevation for f in forecasts])
    times = forecasts[0].time[:H]

    n_steps = H // step_hours
    ref_elev_grid = idw_grid(points_lonlat, pelev, grid_lon, grid_lat, power=2.0)

    frames: List[np.ndarray] = []
    step_times: List[str] = []
    for k in range(n_steps):
        sl = slice(k * step_hours, (k + 1) * step_hours)
        p_sum = precip[:, sl].sum(axis=1)
        s_sum = snow[:, sl].sum(axis=1)
        t_mean = temp[:, sl].mean(axis=1)
        w_mean = wspd[:, sl].mean(axis=1)
        wd = _vector_dir(wdir[:, sl], wspd[:, sl])

        wg = WeatherGrid(
            precipitation_mm=idw_grid(points_lonlat, p_sum, grid_lon, grid_lat),
            snowfall_cm=idw_grid(points_lonlat, s_sum, grid_lon, grid_lat),
            temperature_c_ref=idw_grid(points_lonlat, t_mean, grid_lon, grid_lat),
            ref_elevation=ref_elev_grid,
            wind_speed_ms=idw_grid(points_lonlat, w_mean, grid_lon, grid_lat),
            wind_direction_deg=idw_direction_grid(points_lonlat, wd, grid_lon, grid_lat),
        )
        inc = compute_new_snow(terrain, wg, params)["new_snow_cm"]
        frames.append(np.clip(inc, 0, None).astype("float32"))
        step_times.append(times[k * step_hours])

    # --- Quantisieren + HTML schreiben ------------------------------------ #
    out_html = out_html or (OUTPUT_DIR / f"interactive_snow_{start.isoformat()}.html")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    _write_html(
        out_html, frames, step_times, step_hours,
        (lat_min, lon_min, lat_max, lon_max), nx, ny, start.isoformat(), days,
    )
    print(f"[OUT] Interaktive Karte: {out_html}  ({n_steps} Schritte x {step_hours}h)")
    return out_html


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def _endpoint(date: str) -> Tuple[str, bool]:
    target = datetime.strptime(date, "%Y-%m-%d").date()
    if (date_cls.today() - target).days > ARCHIVE_THRESHOLD_DAYS:
        return OPEN_METEO_ARCHIVE_URL, True
    return OPEN_METEO_URL, False


def _weather_points(bbox, step_deg) -> Tuple[list, list]:
    lons = np.arange(bbox["lon_min"], bbox["lon_max"] + step_deg, step_deg)
    lats = np.arange(bbox["lat_min"], bbox["lat_max"] + step_deg, step_deg)
    glon, glat = np.meshgrid(lons, lats)
    return glat.ravel().tolist(), glon.ravel().tolist()


def _elevation_on_grid(grid_lon, grid_lat, synthetic: bool) -> np.ndarray:
    if synthetic:
        return _synthetic_elev(grid_lon, grid_lat)
    # Echtes Copernicus-DEM auf das lat/lon-Anzeige-Raster reprojizieren.
    from data_connectors.copernicus_dem_loader import (
        _required_tiles, _tile_url, _NODATA,
    )
    import os
    import rasterio
    from rasterio import Affine
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    lon_min, lon_max = float(grid_lon.min()), float(grid_lon.max())
    lat_min, lat_max = float(grid_lat.min()), float(grid_lat.max())
    ny, nx = grid_lon.shape
    res_lon = (lon_max - lon_min) / nx
    res_lat = (lat_max - lat_min) / ny
    dst = np.full((ny, nx), _NODATA, dtype="float32")
    dst_transform = from_origin(lon_min, lat_max, res_lon, res_lat)
    tiles = _required_tiles(lon_min, lat_min, lon_max, lat_max)
    decim = max(1, int(res_lat / 0.000277))  # grobe COG-Stufe -> schnell

    gdal_env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_USE_HEAD": "NO",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_TIMEOUT": os.environ.get("GDAL_HTTP_TIMEOUT", "30"),
        "GDAL_HTTP_CONNECTTIMEOUT": os.environ.get("GDAL_HTTP_CONNECTTIMEOUT", "15"),
        "GDAL_HTTP_MAX_RETRY": "2", "GDAL_HTTP_RETRY_DELAY": "1", "VSI_CACHE": "TRUE",
        "GDAL_HTTP_UNSAFESSL": os.environ.get("GDAL_HTTP_UNSAFESSL", "NO"),
        "AWS_NO_SIGN_REQUEST": "YES",
    }
    with rasterio.Env(**gdal_env):
        for i, (lat, lon) in enumerate(tiles):
            print(f"[DEM] Kachel {i + 1}/{len(tiles)} N{lat}E{lon} ...")
            try:
                with rasterio.open(_tile_url(lat, lon)) as src:
                    oh, ow = max(1, src.height // decim), max(1, src.width // decim)
                    data = src.read(1, out_shape=(oh, ow), resampling=Resampling.average).astype("float32")
                    st = src.transform * Affine.scale(src.width / ow, src.height / oh)
                    reproject(data, dst, src_transform=st, src_crs=src.crs,
                              dst_transform=dst_transform, dst_crs="EPSG:4326",
                              dst_nodata=_NODATA, resampling=Resampling.bilinear,
                              init_dest_nodata=False)
            except Exception as exc:
                print(f"[DEM] Kachel N{lat}E{lon} uebersprungen: {exc!r}")
    return np.where(dst == _NODATA, np.nan, dst).astype("float64")


def _synthetic_elev(grid_lon, grid_lat) -> np.ndarray:
    """Grobes synthetisches Alpenrelief in lat/lon (nur Offline-Test)."""
    x = (grid_lon - 5.9) / (10.6 - 5.9)
    y = (grid_lat - 45.8) / (47.85 - 45.8)
    relief = (
        900 * np.cos((y - 0.35) * np.pi)  # hoeher im Sueden
        + 350 * np.sin(6 * np.pi * x) * np.cos(5 * np.pi * y)
        + 200 * np.sin(11 * np.pi * x + 0.5)
    )
    return np.clip(700 + relief, 300, 3600)


def _elevation_at_points(lons, lats, use_synthetic_weather: bool) -> list:
    gl = np.array(lons)[None, :]
    ga = np.array(lats)[None, :]
    return _synthetic_elev(gl, ga).ravel().tolist()


def _vector_dir(direction_deg: np.ndarray, speed: np.ndarray) -> np.ndarray:
    rad = np.radians(direction_deg)
    w = np.where(speed > 0, speed, 1.0)
    u = (w * np.sin(rad)).sum(axis=1)
    v = (w * np.cos(rad)).sum(axis=1)
    return (np.degrees(np.arctan2(u, v))) % 360.0


def _write_html(out_html, frames, step_times, step_hours, bounds_latlon,
                nx, ny, start_iso, days):
    lat_min, lon_min, lat_max, lon_max = bounds_latlon
    stack = np.stack(frames)  # (n_steps, ny, nx)
    vmax_step = float(max(stack.max(), 0.5))
    scale = vmax_step / 255.0
    q = np.clip(np.round(stack / scale), 0, 255).astype("uint8")
    b64 = base64.b64encode(q.tobytes()).decode("ascii")

    payload = {
        "nx": nx, "ny": ny, "n_steps": len(frames), "step_hours": step_hours,
        "scale": scale, "times": step_times,
        "bounds": [lat_min, lon_min, lat_max, lon_max],
        "slf_bounds": SLF_BOUNDS, "slf_colors": SLF_COLORS,
        "start": start_iso, "days": days,
    }
    html = _HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload)).replace("__DATA__", b64)
    out_html.write_text(html, encoding="utf-8")


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Neuschnee Schweiz - interaktiv</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,Arial,sans-serif}
  #map{position:absolute;top:0;bottom:0;left:0;right:0}
  .panel{position:absolute;z-index:1000;top:12px;left:12px;background:#fff;
    padding:12px 14px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.2);width:320px}
  .panel h3{margin:0 0 6px;font-size:15px}
  .win button{border:1px solid #bbb;background:#f4f4f4;border-radius:6px;padding:5px 10px;
    margin-right:6px;cursor:pointer;font-size:13px}
  .win button.active{background:#2a62b5;color:#fff;border-color:#2a62b5}
  #slider{width:100%;margin-top:10px}
  .rng{font-size:13px;color:#333;margin-top:6px}
  .legend{position:absolute;z-index:1000;bottom:18px;left:12px;background:#fff;
    padding:8px 10px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.2);font-size:12px}
  .legend i{display:inline-block;width:16px;height:12px;margin-right:5px;vertical-align:middle}
  .legend div{margin:1px 0}
</style></head>
<body>
<div id="map"></div>
<div class="panel">
  <h3>❄️ Neuschnee Schweiz</h3>
  <div class="win" id="win">
    <button data-h="24">24h</button>
    <button data-h="48">48h</button>
    <button data-h="72" class="active">72h</button>
  </div>
  <input type="range" id="slider" min="0" max="0" value="0" step="1">
  <div class="rng" id="rng"></div>
</div>
<div class="legend" id="legend"></div>
<script>
const P = __PAYLOAD__;
const RAW = atob("__DATA__");
const N = P.nx*P.ny;
const data = new Uint8Array(RAW.length);
for (let i=0;i<RAW.length;i++) data[i]=RAW.charCodeAt(i);

// SLF-Farbskala (feste Klassen) -> Funktion cm -> [r,g,b,a]
function hex(h){return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}
const SLF_RGB = P.slf_colors.map(hex);
function colorFor(cm){
  if (cm < P.slf_bounds[0]) return [0,0,0,0];          // < 1 cm transparent
  for (let i=0;i<P.slf_bounds.length-1;i++){
    if (cm < P.slf_bounds[i+1]) return [...SLF_RGB[i],205];
  }
  return [...SLF_RGB[SLF_RGB.length-1],205];
}

// Karte + Schweizer Basemap
const map = L.map('map').setView([46.8,8.23], 8);
L.tileLayer('https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg',
  {attribution:'© swisstopo'}).addTo(map);

const canvas = document.createElement('canvas');
canvas.width=P.nx; canvas.height=P.ny;
const ctx = canvas.getContext('2d');
const img = ctx.createImageData(P.nx,P.ny);
const bnds = [[P.bounds[0],P.bounds[1]],[P.bounds[2],P.bounds[3]]];
let overlay = L.imageOverlay(canvas.toDataURL(), bnds, {opacity:0.8}).addTo(map);

let windowH = 72;
let startStep = 0;
const stepH = P.step_hours;
const winSteps = ()=> Math.round(windowH/stepH);
const maxStart = ()=> Math.max(0, P.n_steps - winSteps());

function fmt(t){ // "2026-05-27T08:00" -> "27.05. 08:00"
  const d=new Date(t+"Z");
  const p=n=>String(n).padStart(2,'0');
  return p(d.getUTCDate())+"."+p(d.getUTCMonth()+1)+". "+p(d.getUTCHours())+":00 UTC";
}

function render(){
  const ws = winSteps();
  const s0 = Math.min(startStep, maxStart());
  // Frames im Fenster fliessend aufsummieren
  const sum = new Float32Array(N);
  for (let k=s0;k<s0+ws && k<P.n_steps;k++){
    const off=k*N;
    for (let i=0;i<N;i++) sum[i]+=data[off+i]*P.scale;
  }
  for (let i=0;i<N;i++){
    const c=colorFor(sum[i]); const j=i*4;
    img.data[j]=c[0];img.data[j+1]=c[1];img.data[j+2]=c[2];img.data[j+3]=c[3];
  }
  ctx.putImageData(img,0,0);
  overlay.setUrl(canvas.toDataURL());
  const t0=P.times[s0], t1=P.times[Math.min(s0+ws,P.n_steps-1)];
  document.getElementById('rng').textContent =
    windowH+"h-Summe:  "+fmt(t0)+"  →  "+fmt(t1);
}

// UI
const slider=document.getElementById('slider');
function syncSlider(){ slider.max=maxStart(); if(startStep>maxStart())startStep=maxStart(); slider.value=startStep; }
slider.addEventListener('input',e=>{startStep=+e.target.value; render();});
document.querySelectorAll('#win button').forEach(b=>{
  b.addEventListener('click',()=>{
    document.querySelectorAll('#win button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); windowH=+b.dataset.h; syncSlider(); render();
  });
});

// Legende
const lg=document.getElementById('legend');
let html="<b>Neuschnee [cm]</b><br>";
for(let i=0;i<P.slf_bounds.length-1;i++){
  html+="<div><i style='background:"+P.slf_colors[i]+"'></i>"+P.slf_bounds[i]+"–"+P.slf_bounds[i+1]+"</div>";
}
lg.innerHTML=html;

syncSlider(); render();
</script></body></html>
"""
