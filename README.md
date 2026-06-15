# Swiss Snow Model

Physically-inspired new-snow raster forecast for Switzerland, output as a single
interactive HTML dashboard. Combines Open-Meteo weather data, swisstopo terrain
models, and SLF/IMIS station observations into a multi-layer map with a draggable
time window (today +/-5 days).

```bash
python run_interactive.py                      # today +/-5 days, live data + SLF stations
python run_interactive.py --date 2026-02-19    # centred on a specific date
python run_interactive.py --offline --res 3000 # synthetic data, no network
```

Output: `outputs/interactive_<date>.html` — open in any browser.

---

## 1. Data Sources

### 1.1 Open-Meteo Forecast API

| Item | Detail |
|---|---|
| **Provider** | [Open-Meteo](https://open-meteo.com/) (free, non-commercial) |
| **Endpoint (forecast)** | `https://api.open-meteo.com/v1/forecast` |
| **Endpoint (archive)** | `https://archive-api.open-meteo.com/v1/archive` |
| **Variables** | `temperature_2m` [degC], `precipitation` [mm/h], `snowfall` [cm/h], `wind_speed_10m` [m/s], `wind_direction_10m` [deg, meteorological from-direction], `sunshine_duration` [s/h] |
| **Temporal res.** | Hourly |
| **Spatial res.** | Model-native (~2-11 km depending on model chosen) |
| **Fetch method** | HTTP GET with comma-separated lat/lon (chunked, 15 points/request) |
| **Model** | `best_match` by default; overridable via `OPEN_METEO_MODEL` env var (e.g. `meteoswiss_icon_ch1`) |
| **Archive switch** | Dates >80 days in the past automatically use the ERA5 archive endpoint |
| **Limitations** | Free tier is rate-limited (HTTP 429); client retries with exponential backoff. Archive has no `sunshine_duration`. |

### 1.2 swissALTIRegio DEM (Exposition & Terrain)

| Item | Detail |
|---|---|
| **Provider** | [swisstopo](https://www.swisstopo.admin.ch/) |
| **Product** | swissALTIRegio — national DEM for Switzerland |
| **Native res.** | 10 m |
| **CRS** | EPSG:2056 (LV95) |
| **Fetch method** | Cloud-Optimized GeoTIFF (COG) via `/vsicurl/`, read with windowed + decimated access |
| **URL** | `https://data.geo.admin.ch/ch.swisstopo.swissaltiregio/swissaltiregio/swissaltiregio_2056_5728.tif` |
| **Used for** | Aspect (exposition), slope, roughness, TPI wind exposure index |
| **Read resolution** | Decimated to ~60 m for aspect/slope, ~240 m for roughness/TPI |
| **Limitations** | Full 10 m resolution for all of Switzerland exceeds memory for a single PNG overlay (~115 m/px at 3000 px width). Regional tiling or a tile service would be needed for finer detail. |

### 1.3 Copernicus DEM (Main Raster Elevation)

| Item | Detail |
|---|---|
| **Provider** | Copernicus / ESA, hosted on AWS Open Data |
| **Product** | Copernicus DEM GLO-30 |
| **Native res.** | ~30 m |
| **Fetch method** | COG via HTTP (rasterio `/vsicurl/`), windowed read with overview decimation |
| **Used for** | Main grid elevation for lapse-rate correction, slope/aspect for snow model, horizon angles for solar model |
| **Cache** | Downloaded tiles cached under `data/dem_cache/` |
| **Limitations** | Behind corporate TLS inspection proxies, set `GDAL_HTTP_UNSAFESSL=YES`. |

### 1.4 swisstopo WMTS Layers

| Item | Detail |
|---|---|
| **Provider** | swisstopo WMTS |
| **Basemap** | `ch.swisstopo.pixelkarte-farbe` (Swiss national map, JPEG tiles) |
| **Slope classes** | `ch.swisstopo.hangneigung-ueber_30` (slope >= 30 deg classes, PNG tiles) |
| **Hillshade** | `ch.swisstopo.swissalti3d-reliefschattierung_monodirektional` (monodirectional relief shading, PNG tiles) |
| **Fetch method** | Standard WMTS tile URLs loaded by Leaflet in the browser |
| **Used for** | Hangneigung (slope) and Schummerung (hillshade) overlay layers |

### 1.5 SLF/IMIS Station API

| Item | Detail |
|---|---|
| **Provider** | SLF (WSL), CC BY 4.0 |
| **Data** | Snow height (HS), air temperature (TA), snow surface temperature (TSS), wind speed (VW), wind direction (DW) |
| **Fetch method** | SLF JSON API (`slf_stations.py`) — station list + latest values + HS time series |
| **Station count** | ~60 stations selected (60% snow-capable, 40% wind-only, sorted by elevation) |
| **Temporal res.** | Half-hourly measurements, aggregated to hourly keys |
| **Limitations** | Only available for live/recent data (today +/-N days). Historical dates or offline mode have no station overlay. |

---

## 2. Calculations & Physics

### 2.1 New Snow Accumulation

**Mode "precip" (default):**

```
T_cell     = T_ref + lapse_rate * (z_cell - z_ref)
P_cell     = P_ref * altitude_factor * orographic_factor
snow_frac  = 1 / (1 + exp(k * (T_cell - T_center)))
SLR        = clip(slr_intercept + slr_slope * T_cell, slr_min, slr_max)
snow_pre   = P_cell * snow_frac * SLR / 10     [cm]
Snow_new   = snow_pre * wind_factor             [cm]
```

| Step | Formula | Units | Source |
|---|---|---|---|
| Lapse-rate correction | `T_cell = T_ref - 6.5 K/km * dz` | degC | Standard atmosphere |
| Altitude precip factor | `1 + 0.0004 * dz`, clipped [0.6, 1.8] | dimensionless | Orographic enhancement ~+4%/100 m |
| Rain/snow phase | Logistic sigmoid, centre 1.0 degC, steepness k=1.4 | fraction [0,1] | Dai (2008) |
| Snow-to-liquid ratio | `clip(10 - 0.8 * T, 6, 25)` | cm snow / cm SWE | Roebber et al. (2003) |
| Orographic factor | `1 + 0.6 * sin(slope) * wind_norm * cos(aspect - wind_dir)`, clipped [0.4, 1.6] | dimensionless | Luv/lee enhancement |
| Wind redistribution | Erosion on convex ridges, deposition in concave/lee, clipped [0.2, 2.5] | dimensionless | Simplified saltation heuristic |

**Mode "snowfall":** Uses Open-Meteo `snowfall` [cm] directly (phase already resolved),
applies only altitude, orographic, and wind factors.

### 2.2 Freeze-Thaw Cycle Counting

Counted from hourly temperature at each cell over the selected time window.

```
A cycle = one transition from below 0 degC to above 0 degC followed by a
          return below 0 degC.
Implementation: count each 0 degC crossing as +0.5, then floor().
```

- Input: hourly temperature [degC] per cell
- Output: integer cycle count

### 2.3 Solar Radiation Model (Clear-Sky)

Computed entirely in the browser from DEM-derived terrain inputs.

**Inputs (from Python pipeline, reprojected to WGS84):**
- Slope [deg] per radiation grid cell
- Aspect [deg] per radiation grid cell
- Horizon elevation angle [deg] for K=12 azimuth sectors (terrain shadow)

**Algorithm (per cell, per day):**

```
For hour h = 3.5 to 20.5 UTC, step 0.5h:
  1. Solar declination:  decl = 23.45 deg * sin(2*pi*(284+doy)/365)
  2. Hour angle:         ha = (h - 12) * 15 deg
  3. Solar elevation:    sin(el) = sin(lat)*sin(decl) + cos(lat)*cos(decl)*cos(ha)
     Skip if el <= ~1 deg
  4. Solar azimuth:      from spherical geometry
  5. Horizon check:      el > horizon_angle[azimuth_sector] => lit
  6. Incidence angle:    cos(I) = cos(slope)*sin(el) + sin(slope)*cos(el)*cos(az - aspect)
  7. Air mass:           AM = 1 / sin(el)
  8. Direct beam:        Ib = 1361 * 0.72^AM * cos(I)   (if lit and cos(I)>0)
  9. Diffuse:            Id = 0.13 * 1361 * sin(el) * sky_view
     where sky_view = (1 + cos(slope)) / 2
  10. Accumulate:        Wh += (Ib + Id) * dt
```

- Output: Wh/m2/day (clear-sky, single day determined by time window start)
- Assumptions: clear atmosphere (tau=0.72), no clouds, no snow albedo feedback

### 2.4 Effective Radiation (x Sunshine)

```
effective_rad = clear_sky_rad * clamp(sunshine_hours / (0.42 * window_hours), 0, 1)
```

Combines the clear-sky model with actual Open-Meteo sunshine duration data.

### 2.5 Wind Animation

Canvas-based particle flow animation in the browser:
- ~420 semi-transparent particles
- Position updated per frame using interpolated wind speed/direction from the sparse wind grid
- Fade trails via `globalCompositeOperation = 'destination-out'` (transparent fade, no white wash)
- Particle speed proportional to wind: `pixel_speed = 0.6 + kmh * 0.10`

### 2.6 Aspect Classification

Discrete 4-quadrant classification from continuous aspect angle:

| Range | Direction | Color |
|---|---|---|
| 315 deg - 45 deg | North | Blue `#4A90D9` |
| 45 deg - 135 deg | East | Green `#66BB6A` |
| 135 deg - 225 deg | South | Red/Orange `#EF5350` |
| 225 deg - 315 deg | West | Yellow `#FFC107` |

Flat cells (slope < 5 deg) are grey `#9E9E9E`. Hard boundaries, no interpolation.

Aspect convention: angle of the downslope direction, clockwise from North.
`aspect = atan2(-dz/dx, dz/dy) mod 360`, where dx=East, dy=North.

### 2.7 Powder Decision Engine

Per-cell binary evaluation (POWDERED = TRUE / FALSE) over the user-selected time window.

**Execution order:**

1. **Destruction rules** (checked first; any one triggers FALSE):

| ID | Rule | Condition | Result |
|---|---|---|---|
| D1 | Heavy Rain | Any rain (precip > 0.1 mm AND temp > 1 degC) in last 48 h | FALSE |
| D2 | Freeze-Thaw Collapse | Freeze-thaw cycles > 4 in window | FALSE |
| D3 | Strong Solar Melt | Clear-sky radiation >= 5000 Wh/m2/day | FALSE |

2. **Preservation rules** (checked second; any one triggers TRUE with aspect constraints):

| ID | Rule | Condition | Valid Aspects |
|---|---|---|---|
| R1 | Deep Freeze | Tmax < -2 degC | ALL |
| R2 | Calm Wind | Approx. gust < 20 km/h AND mean wind < 10 km/h | ALL |
| R3 | Wind Redistribution | Approx. gust 20-60 km/h AND mean wind < 30 km/h | LEESIDE only |
| R4 | Freeze + Clear Night | -2 degC <= Tmax < 5 degC AND clear night detected | NORTH only |

3. **Solar moderation** (post-filter on TRUE cells):
   - 2000-5000 Wh/m2/day: remove SOUTH and WEST from valid aspect set
   - < 2000 Wh/m2/day: no effect

4. **Aspect check**: if the cell's own aspect quadrant is NOT in the valid set after moderation, the cell becomes FALSE.

5. **Intermediate degradation**: 2-4 freeze-thaw cycles mark the cell as "reduced quality" (still TRUE but visually distinct). 0-1 cycles = "stable".

**Aspect logic:**
- LEESIDE = the quadrant(s) whose centre is within 90 deg of the direction 180 deg opposite the dominant wind-from direction
- NORTH = 315 deg - 45 deg
- Dominant wind direction: vector-averaged from the nearest sparse wind grid point over the time window

**Gust approximation:** `gust ~ mean_wind * 1.5` (no dedicated gust variable fetched).

**Clear night proxy:** Any hour with sunshine_duration ~ 0 AND temperature < 0 degC.

---

## 3. Thresholds & Rules

Every threshold used in the model, in one table. Values from `config/model_params.yaml`
unless noted as browser-side powder engine constants.

### 3.1 Snow Model Thresholds (model_params.yaml)

| Parameter | Value | Unit | Controls | Tunable | Source/Reasoning |
|---|---|---|---|---|---|
| `t_center_c` | 1.0 | degC | Rain/snow phase transition centre | Yes | Wet snow occurs above 0 degC; Dai (2008) |
| `k` | 1.4 | 1/degC | Steepness of rain/snow sigmoid | Yes | Moderate transition width ~2 degC |
| `temp_lapse_k_per_m` | -0.0065 | K/m | Temperature altitude correction | Yes | Standard atmosphere -6.5 K/km |
| `precip_gamma_per_m` | 0.0004 | 1/m | Precip increase per metre elevation | Yes | ~+4%/100 m orographic gradient |
| `precip_factor_min` | 0.6 | - | Min altitude precip factor | Yes | Prevent unrealistic suppression |
| `precip_factor_max` | 1.8 | - | Max altitude precip factor | Yes | Saturation cap |
| `slr_intercept` | 10.0 | - | SLR at 0 degC | Yes | Roebber et al. (2003) |
| `slr_slope` | -0.8 | 1/degC | SLR change per degC | Yes | Colder = fluffier |
| `slr_min` | 6.0 | - | Minimum SLR | Yes | Very wet snow |
| `slr_max` | 25.0 | - | Maximum SLR | Yes | Very cold, dry snow |
| `k_oro` | 0.6 | - | Orographic factor strength | Yes | Luv/lee enhancement magnitude |
| `wind_ref_ms` (oro) | 8.0 | m/s | Wind normalisation for orographic | Yes | Moderate reference wind |
| `factor_min` (oro) | 0.4 | - | Min orographic factor | Yes | |
| `factor_max` (oro) | 1.6 | - | Max orographic factor | Yes | |
| `k_erode` | 0.35 | - | Wind erosion strength on ridges | Yes | Convex terrain exposure |
| `k_deposit` | 0.45 | - | Wind deposition in lee/concave | Yes | |
| `wind_ref_ms` (redist) | 8.0 | m/s | Wind normalisation for redistribution | Yes | |
| `factor_min` (redist) | 0.2 | - | Min wind redistribution factor | Yes | |
| `factor_max` (redist) | 2.5 | - | Max wind redistribution factor | Yes | |
| `max_new_snow_cm` | 300.0 | cm | Plausibility cap per time window | Yes | Physical upper bound |

### 3.2 Powder Engine Thresholds (browser-side constants)

| Constant | Value | Unit | Controls | Tunable | Reasoning |
|---|---|---|---|---|---|
| `PD_RAIN_LOOKBACK_H` | 48 | hours | D1: lookback window for rain detection | Yes | Rain destroys powder within ~2 days |
| `PD_RAIN_TEMP_C` | 1.0 | degC | D1: temperature above which precip counts as rain | Yes | Matches model's rain/snow transition |
| `PD_RAIN_MIN_MM` | 0.1 | mm/h | D1: minimum precip to count as rain event | Yes | Filter trace amounts |
| `PD_FT_DESTROY` | 4 | cycles | D2: freeze-thaw cycles for destruction | Yes | >4 cycles typically forms melt crust |
| `PD_FT_DEGRADE` | 2 | cycles | Degradation: cycles for "reduced" quality | Yes | 2-4 cycles = partial metamorphism |
| `PD_SOLAR_DESTROY_WH` | 5000 | Wh/m2/day | D3: solar radiation destruction threshold | Yes | Approx. 500 W/m2 peak * 10h effective |
| `PD_SOLAR_MOD_WH` | 2000 | Wh/m2/day | Solar moderation threshold (remove S/W) | Yes | Approx. 200 W/m2 peak * 10h effective |
| `PD_DEEP_FREEZE_C` | -2 | degC | R1: Tmax threshold for deep freeze | Yes | Persistent cold preserves crystal structure |
| `PD_FREEZE_CLEAR_MIN_C` | -2 | degC | R4: Tmax lower bound | Yes | |
| `PD_FREEZE_CLEAR_MAX_C` | 5 | degC | R4: Tmax upper bound (marginal conditions) | Yes | Up to 5 degC with radiative cooling at night |
| `PD_GUST_CALM_KMH` | 20 | km/h | R2: gust threshold for calm wind | Yes | Below 20 km/h, minimal wind transport |
| `PD_WIND_CALM_KMH` | 10 | km/h | R2: mean wind threshold for calm | Yes | |
| `PD_GUST_REDIST_MIN_KMH` | 20 | km/h | R3: gust lower bound for redistribution | Yes | Onset of significant snow transport |
| `PD_GUST_REDIST_MAX_KMH` | 60 | km/h | R3: gust upper bound for redistribution | Yes | Above 60 km/h, destruction dominates |
| `PD_WIND_REDIST_KMH` | 30 | km/h | R3: mean wind threshold | Yes | |
| `PD_GUST_FACTOR` | 1.5 | - | Gust approximation multiplier | Yes | Standard gust factor for moderate terrain |

### 3.3 Other Thresholds

| Parameter | Value | Unit | Location | Controls |
|---|---|---|---|---|
| Aspect flat cutoff | 5.0 | deg | `_aspect_rgba` | Slope below which cells are coloured grey |
| Aspect flat cutoff (terrain) | 1.5 | deg | `_corrected_aspect` | Slope below which aspect is set to NaN |
| Solar model tau | 0.72 | - | Browser JS `computeRad` | Atmospheric transmittance (clear sky) |
| Solar model I0 | 1361 | W/m2 | Browser JS `computeRad` | Solar constant |
| Solar model dt | 0.5 | h | Browser JS `computeRad` | Time step for daily integration |
| Horizon sectors K | 12 | - | Pipeline + browser | Azimuth resolution for terrain shadow |
| Wind grid step | 9000 | m | `_WIND_STEP` | Spacing of sparse wind point grid |
| Radiation grid res | 1000 | m | `_RAD_RES` | Resolution for solar model terrain inputs |
| Fine terrain res | 60 | m | `_FINE_RES` | swissALTIRegio read resolution for aspect |

---

## 4. Architecture

### 4.1 Pipeline Overview

```
Python (run_interactive.py)                    Browser (HTML/JS)
========================                       ==================
1. Load config & params                        1. Decode base64 raster cubes
2. Fetch DEM (Copernicus or synthetic)         2. Build cumulative snow array
3. Compute terrain features (slope/aspect)     3. Build powder engine mappings
4. Fetch weather (Open-Meteo, chunked)         4. On time window change:
5. IDW-interpolate weather to DEM grid            - Aggregate raster stats
6. Run snow model per hour per cell               - Run powder engine per cell
7. Compute fine terrain (swissALTIRegio)          - Render to canvas → image overlay
8. Compute radiation inputs (horizon angles)      - Compute solar radiation (clear-sky)
9. Fetch SLF stations (if live mode)              - Update wind arrows + flow animation
10. Reproject all cubes LV95 → WGS84             - Update station markers
11. Encode as uint8 + base64                   5. Click handler for powder popup
12. Inject into HTML template                  6. Leaflet map with swisstopo basemap
13. Write single HTML file
```

**Key design decision:** Everything is output as a single self-contained HTML file.
All raster data is base64-encoded inline. No server needed — open the file in any browser.

### 4.2 File / Module Structure

```
config/
  settings.py          Paths, bounding boxes, API URLs, AOI dataclass, RunConfig
  model_params.yaml    All snow model coefficients (one file, no magic numbers)

data_connectors/
  open_meteo_client.py HTTP wrapper for Open-Meteo (forecast + archive)
  copernicus_dem_loader.py  Copernicus DEM COG reader with caching
  dem_loader.py        Synthetic DEM generator (for offline mode)
  slf_stations.py      SLF/IMIS station API (list, latest values, HS time series)
  synthetic_weather.py Synthetic weather generator (for offline mode)

model/
  terrain_features.py  Slope, aspect, curvature from DEM (pure numpy)
  factors.py           Factor functions: temperature, altitude, orographic, wind (pure numpy)
  snow_model.py        Core snow accumulation formula (combines all factors)
  raster_engine.py     Grid coordinate generation
  interpolation.py     IDW interpolation utilities
  aggregation.py       Temporal aggregation

pipeline/
  interactive_export.py  Main pipeline: data assembly, reprojection, HTML generation
                         Contains the full HTML/JS template as _HTML string
  overlay_export.py      Static overlay export (GeoTIFF, PNG, folium map)
  geo_utils.py           Weather sample grid generation
  run_pipeline.py        Batch pipeline orchestration

viz/
  visualize.py         matplotlib diagnostic plots

validation/
  validate_slf.py      SLF comparison and bias correction
```

### 4.3 Data Encoding

Raster cubes are encoded as uint8 arrays (base64) with linear scaling:

| Array | Python encode | JS decode | Meaning |
|---|---|---|---|
| SNOW | `round(snow_cm / 0.2)` | `val * 0.2` | New snow [cm] per hour per cell |
| TEMP | `round((temp_c + 60) * 2)` | `val / 2 - 60` | Temperature [degC] |
| SUN | `round(sunshine_frac * 100)` | `val / 100` | Sunshine fraction [0-1] |
| WINDG | `round(wind_ms * 5)` | `val / 5` | Wind speed [m/s] on main grid |
| SPD | `round(wind_ms * 5)` | `val / 5` | Wind speed [m/s] on sparse grid |
| WDIR | `round(dir_deg / 2)` | `val * 2` | Wind direction [deg] |
| PREC | `round(precip_mm * 5)` | `val / 5` | Precipitation [mm/h] |
| MASPECT | `round(aspect_deg / 2)` | `val * 2` | Main grid aspect [deg] |
| MSLOPE | `round(slope_deg)` | `val` | Main grid slope [deg] |
| RSLOPE | `round(slope_deg)` | `val` | Radiation grid slope [deg] |
| RASPECT | `round(aspect_deg / 2)` | `val * 2` | Radiation grid aspect [deg] |
| RHOR | `round(horizon_deg)` | `val` | Horizon elevation angle [deg] |

### 4.4 How to Run

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Interactive dashboard (main use case)
python run_interactive.py                          # today, live data
python run_interactive.py --date 2026-02-19        # specific date
python run_interactive.py --offline --res 3000     # synthetic, fast test

# Switzerland-wide static overlay
python run_switzerland.py --date 2026-02-19 --window 72

# Single AOI with diagnostics
python main.py --dem /path/to/dem.tif --hours 24 --plot --overlay
```

| Flag | Meaning | Default |
|---|---|---|
| `--date YYYY-MM-DD` | Centre date for time window | today |
| `--days N` | Days each side of centre (max 5) | 5 |
| `--res M` | Grid resolution in metres | 2000 |
| `--offline` | Synthetic DEM + weather (no API calls) | off |
| `--weather-step X` | Weather sample grid spacing [deg] | 0.2 |
| `--stations N` | Number of SLF stations to fetch | 60 |

**Environment variables:**

| Variable | Effect |
|---|---|
| `OPEN_METEO_URL` | Override forecast endpoint |
| `OPEN_METEO_ARCHIVE_URL` | Override archive endpoint |
| `OPEN_METEO_MODEL` | Weather model selection (e.g. `meteoswiss_icon_ch1`) |
| `GDAL_HTTP_UNSAFESSL=YES` | Skip TLS verification for DEM downloads (corporate proxies) |

---

## 5. Known Limitations

### From the Snow Model

- **Exposition at zoom:** True 20 m ridge resolution is not achievable with a single national PNG (~115 m/px at 3000 px across Switzerland). A tiled service or regional swissALTI3D (2 m) would be needed.
- **Solar radiation** is a clear-sky approximation. Pattern is correct, absolute values are uncalibrated (no measured product).
- **Sunshine / Radiation x Sunshine / live station data** require the Open-Meteo forecast or SLF live API. The archive endpoint does not provide sunshine duration, so historical dates lack sunshine-based layers.
- **Wind redistribution** uses a simplified curvature-based heuristic, not a full saltation/suspension transport model (e.g. SnowTran-3D).
- **No snow-on-ground persistence:** The model computes new snowfall per time step. It does not track existing snowpack, settling, or melt over days.

### From the Powder Engine

- **Gust approximation:** Gusts are estimated as `mean_wind * 1.5`. Real gust data (`wind_gusts_10m`) is available from Open-Meteo but not currently fetched to keep data volume manageable. Thresholds are calibrated to the approximation.
- **Clear night proxy:** Uses `sunshine_duration == 0 AND temperature < 0 degC`. This does not distinguish between cloudy nights (warmer, less radiation loss) and clear nights (strong radiative cooling). A cloud cover variable would improve this.
- **Leeside simplification:** Lee is determined from the dominant wind direction at the nearest sparse wind grid point (~9 km resolution), not from a fine-scale topographic flow model. In complex terrain with valley winds, the actual lee can differ significantly.
- **Solar thresholds in Wh/m2/day:** The spec defines thresholds in W/m2 (instantaneous). These are converted to daily energy (Wh/m2/day) using an assumed ~10 effective sun hours. The correspondence is approximate and varies by season and latitude.
- **No snow amount check:** The powder engine evaluates weather conditions only. It does not verify that snow actually exists at a given cell. Combine with the Neuschnee layer for meaningful interpretation.
- **Precipitation = rain proxy:** Rain is identified as `precip > 0.1 mm/h AND temp > 1 degC`. This is a simplification; mixed precipitation near 0-2 degC may be misclassified.

### General

- This is an estimation and visualisation tool, **not** a measurement or avalanche warning system.
- Absolute snow amounts are heuristic and not event-calibrated. Use SLF validation (`validation/`) for bias assessment.
- The free Open-Meteo tier has rate limits. For many grid points, use a coarser `--weather-step`.

---

## 6. Licensing & Legal Requirements

All data sources used by this project are listed below with their respective licences and usage conditions. Users of this software must comply with all applicable terms.

### 6.1 Open-Meteo

| Item | Detail |
|---|---|
| **Provider** | [Open-Meteo GmbH](https://open-meteo.com/) |
| **Licence** | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) for non-commercial use. Commercial use requires a paid subscription. |
| **Attribution** | "Weather data by [Open-Meteo.com](https://open-meteo.com/)" must be displayed when data is shown publicly. |
| **Terms** | [https://open-meteo.com/en/terms](https://open-meteo.com/en/terms) |
| **Rate limits** | Free tier: rate-limited (HTTP 429). Do not abuse the API; keep request frequency reasonable. |
| **Underlying models** | Open-Meteo aggregates data from national weather services (DWD ICON, MeteoSwiss, ECMWF, NOAA GFS, etc.), each with their own open data policies. The Open-Meteo CC BY 4.0 licence applies to the API output. |

### 6.2 swisstopo

| Item | Detail |
|---|---|
| **Provider** | [Swiss Federal Office of Topography (swisstopo)](https://www.swisstopo.admin.ch/) |
| **Products used** | swissALTIRegio (DEM), pixelkarte-farbe (basemap tiles), hangneigung-ueber_30 (slope classes), swissalti3d-reliefschattierung_monodirektional (hillshade) |
| **Licence** | **Open Government Data (OGD)** since 1 March 2021 — free for commercial and non-commercial use. See [Swiss Ordinance on Geoinformation (GeoIV), Art. 20a](https://www.admin.ch/opc/en/classified-compilation/20071088/index.html). |
| **Attribution** | "© swisstopo" must appear on all maps and derived products. |
| **Terms of use** | [https://www.swisstopo.admin.ch/en/terms-of-use](https://www.swisstopo.admin.ch/en/terms-of-use) |
| **WMTS terms** | Tile services (WMTS) may be used freely, but swisstopo requests reasonable use and a User-Agent header. High-volume automated scraping is discouraged. See [geo.admin.ch Terms of Use](https://www.geo.admin.ch/en/terms-of-use). |

### 6.3 Copernicus DEM (GLO-30)

| Item | Detail |
|---|---|
| **Provider** | [European Space Agency (ESA) / Copernicus](https://spacedata.copernicus.eu/) |
| **Product** | Copernicus DEM GLO-30 (30 m global DEM) |
| **Licence** | Free and open access under [Copernicus Data Space Ecosystem Terms](https://dataspace.copernicus.eu/terms-and-conditions). Redistribution allowed with attribution. |
| **Attribution** | "Contains modified Copernicus Sentinel data [year]" or "Copernicus DEM — GLO-30, provided under the Copernicus programme." |
| **Hosting** | AWS Open Data Registry (free download, no API key). Also available via Copernicus Data Space Ecosystem. |
| **Restrictions** | No restrictions on commercial or non-commercial use. Must not imply ESA endorsement. |

### 6.4 SLF / IMIS Station Data

| Item | Detail |
|---|---|
| **Provider** | [WSL Institute for Snow and Avalanche Research SLF](https://www.slf.ch/) (part of WSL / ETH domain) |
| **Data** | IMIS automatic measurement stations: snow height, air temperature, snow surface temperature, wind speed/direction |
| **Licence** | **CC BY 4.0** — [https://creativecommons.org/licenses/by/4.0/](https://creativecommons.org/licenses/by/4.0/) |
| **Attribution** | "Data: SLF/WSL, CC BY 4.0" |
| **Terms** | [https://www.slf.ch/en/about-the-slf/legal-information.html](https://www.slf.ch/en/about-the-slf/legal-information.html) |
| **Restrictions** | Data is provided for informational purposes. It is explicitly **not** an avalanche warning and must not be presented as such. The official Swiss avalanche bulletin is published at [whiterisk.ch](https://whiterisk.ch/). |

### 6.5 Leaflet

| Item | Detail |
|---|---|
| **Library** | [Leaflet](https://leafletjs.com/) (JS mapping library) |
| **Licence** | [BSD 2-Clause](https://github.com/Leaflet/Leaflet/blob/main/LICENSE) |
| **Attribution** | "Leaflet" link in map attribution (automatically included). |

### 6.6 Three.js

| Item | Detail |
|---|---|
| **Library** | [Three.js](https://threejs.org/) (3D rendering library) |
| **Licence** | [MIT](https://github.com/mrdoob/three.js/blob/dev/LICENSE) |
| **Restrictions** | None beyond MIT licence terms (include copyright notice in distributions). |

### 6.7 Summary of Attribution Requirements

When displaying output from this tool publicly (e.g., screenshots, embedded maps, reports), the following attribution line covers all sources:

```
© swisstopo | Weather data by Open-Meteo.com | Copernicus DEM GLO-30 | Data: SLF/WSL (CC BY 4.0) | Map: Leaflet
```

This attribution is already included in the HTML output's map attribution string.

### 6.8 Disclaimer

This software is provided for research and personal use. It is **not** an official weather, snow, or avalanche forecast. Users must not rely on it for safety-critical decisions. Always consult official sources:

- **Avalanche bulletin (Switzerland):** [whiterisk.ch](https://whiterisk.ch/) / [slf.ch](https://www.slf.ch/)
- **Weather forecast:** [meteoswiss.ch](https://www.meteoswiss.ch/)
- **Snow reports:** Local ski resort or tourist office
