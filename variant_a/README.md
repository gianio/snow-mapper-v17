# Variant A — representative-point ski-quality pipeline

Offline pipeline that produces a **ski-quality layer** (18-category + simplified) and
**snow-profile data** for Switzerland, organised by the national subregions, and exports
them for the app to display. It fills a gap the main app model does not cover: it runs the
**SNOWPACK** seasonal snowpack model at representative terrain points, so it knows density,
crust, powder depth and layer stratigraphy.

The compute runs **offline** (needs the external SNOWPACK binary); only the exported
layer + profile artifacts are consumed by the app (loose coupling via `manifest.json`).

## Pipeline
```
subregions   national subregion segmentation (tile_labels + DEM terrain)
select_points real DEM cell per elevation×aspect×slope, per subregion  (representative pts)
forcing      per-point Open-Meteo -> SNOWPACK .smet  (spin-up window -> target date)
snowpack_runner  write .sno/.ini, run SNOWPACK in parallel  -> per-point .pro
classify     18-cat (+ over_weak crust refinement) + simplified skier layer + metrics
gridding     subregion-local KNN (x,y,elev,aspect) -> national ski18 / simple / density
profiles     per-point density+grain profiles for the clickable viewer
export       PNG overlays + profiles.json + manifest.json  -> outputs/variant_a/
```

## Run
```bash
export SNOWPACK_BIN=/path/to/snowpack           # external binary
python run_variant_a.py --date 2026-04-01                 # whole Switzerland
python run_variant_a.py --date 2026-04-01 --only-tile 2   # one subregion

# Verify the classify->grid->profile->export chain on EXISTING .pro runs
# (no network / no SNOWPACK rerun):
python run_variant_a.py --date 2026-04-01 \
    --points-csv <points.csv> --runs-dir <dir_of_<id>/*.pro> --step-h 12
```

## Output (`outputs/variant_a/`, git-ignored)
- `layers/{ski18,simple,density}_<ts>.png` — georeferenced RGBA overlays (WGS84 bounds)
- `profiles/profiles.json` — per-point profiles (`points`, `labels`, `profiles`, `grain`)
- `manifest.json` — bounds, timestamps, layer files + legends, subregions
- `preview.html` — standalone Leaflet check of the exported layers

## App integration
The frontend reads `manifest.json`, overlays the PNGs on its Leaflet basemap using
`bounds`, and feeds `profiles.json` to a clickable snow-profile viewer (KNN over the
point profiles in x/y/elev/aspect, client-side). No app model code is modified.

## Data / dependencies
- `data/subregions/` — committed subregion segmentation (tile_labels, tile_meta, model_points)
- `data/dem/ch_lv03_250m.dem` — national DEM (git-ignored; provide locally or via `data_connectors/dem_loader`)
- External: **SNOWPACK/MeteoIO** binaries (offline). Python: numpy, scipy, pyproj, pillow.
- Reuses app connectors (`data_connectors/open_meteo_client`, `slf_stations`, `dem_loader`).

## Production setup (matrix mode, live)

```
weather points (15 km)  x  virtual slopes (300 m bands x 8 aspects x 20/38 deg + flat)
        |                              ~135 points, ~14 000 SNOWPACK runs
forcing.py   Open-Meteo, spliced hour by hour: ICON-CH1 -> CH2 -> D2 -> icon_seamless;
             MeteoSwiss OGD GRIB on top of the forecast hours when enabled
             (tools/ogd_extract.py, own venv); IMIS precipitation factor per point
state.py     live: the snowpack of every run carried between cycles (GitHub artifact
             va-state), advanced only with past weather, lagging "now" by 5 days
matrix.py    advance the state to the window start, run the window, digest each .pro
wind.py      drift (lee) / scour (windward) index per run from the point's wind
terrain.py   horizon shade per cell for the window's dates; forest fraction (WorldCover)
imis.py      measured snow height: validation + slow precipitation correction
gates.py     publish gates -- a failing cycle is not published, the last good one stays
export.py    250 m PNG frames (zoomed out) + per-frame metric packs the app renders at
             terrain resolution (Terrarium tiles, ~30 m), shade/mask + forest rasters
```

```bash
python run_variant_a.py --date 2026-04-01 --step-h 3            # fixed date (demo)
python run_variant_a.py --live --state-dir state --state-out state_new --step-h 3
```

Workflows: `variant-a-live.yml` (4x daily, carries the state, uploads `va-live` only when
the gates pass), `deploy.yml` (publishes the newest `va-live` to `data/variant_a_live`),
`probe-ogd.yml` (checks the MeteoSwiss OGD extractor against Open-Meteo).

### Resolution: what is sharp and what is not

The map follows the terrain at ~30 m (Terrarium tiles, device-rendered from
zoom 10), but the snow information has coarser sources:

| input | resolution |
|---|---|
| weather (SNOWPACK forcing) | 135 points, ~15 km, 3 nearest blended (IDW) |
| per point | 300 m bands x 8 aspects x slopes 0/20/38/45 deg, all interpolated |
| precipitation pattern | ICON-CH1 1 km field (`precip.py`), live runs only |
| horizon shade / forest | 250 m / 100 m, sampled bilinearly in the app |

The 45 deg node (`MATRIX_SLOPES`) keeps couloirs and steep faces from being
read as 38 deg (+~47% runs). `precip.py` scales each cell's new snow by
`P(cell) / P(blend of its weather points)` from ICON-CH1's TOT_PREC field,
which `tools/ogd_extract.py` saves as `precip_ch1.npz`; past days come from
the first 6 h of earlier cycles, kept in the live state as
`precip_hist.npz`. Without a field (demo, archive dates) nothing is scaled.

### Temperature at the height of each band

Each virtual slope runs at its band's height, not at the weather point's.
How its temperature gets there, best first:

1. **ICON-CH1 cells at that height** (live, forecast hours; `elevtemp.py`):
   within 10 km of the weather point, the up-to-3 cells whose model terrain
   (HSURF, or our DEM smoothed to ~1 km) lies within 150 m of the band; their
   2 m temperature, with the few metres left bridged by the profile below.
   `tools/ogd_extract.py` saves the field (`t2m_ch1.npz`).
2. **The model's vertical profile, hour by hour** (all runs; `forcing._profile_dT`):
   850/700/500 hPa temperature and height from Open-Meteo (from ICON seamless
   when the surface model has no pressure levels), piecewise linear in height,
   lapse clipped to -9.8 .. +15 K/km. Catches inversions and warm air aloft.
3. **Fixed -6.5 K/km** for hours with neither.

The run log prints the share of run-hours from each source.

### Checking the model against reality

* **IMIS stations (used):** the SLF measurement API gives the station snow heights behind
  the SLF snow-height maps -- the same measurements, machine-readable, CC BY 4.0. Each
  cycle compares the flat runs (interpolated to the station height) with them, writes
  bias/MAE into the manifest and the Actions summary, blocks publishing when the error is
  gross, and nudges the precipitation factor.
* **The SLF snow-height map itself** is an interpolation of those stations (and
  observers), published as an image for people; there is no documented data interface,
  so it is better used as a visual cross-check than as a reference in code.
* Further independent references, not yet wired in: SLF/OSHD gridded snow water
  equivalent and snow height (1 km, daily; historic data on EnviDat, operational on
  request), satellite snow cover (Sentinel-2 / MODIS) for where there is snow at all,
  SwissMetNet snow depth at the stations that measure it, SLF observer profiles, and
  the app's own user reports ("Pulver? Harsch? Sulz?") as ground truth on slopes.

## Community reports (`community.py`)

Every live cycle compares the public community reports of the last 72 h with
the matching virtual slope (nearest weather point ≤ 12 km, height band, slope
class, aspect) at the report's time:

* **Powder depth** (drawn powder zones, quick reports) vs `powder_depth_cm`
  → bias / MAE, plus a suggested model powder threshold for the classes.
* **Categories** (powder, no snow, wet/corn, crust, drift, scoured) vs the
  model's own classes → hit rate per category.
* **Correction**: like IMIS, a slow step on the weather point's precipitation
  factor (half the IMIS step, ≥ 3 reports from ≥ 2 people, one author ≤ 50 %
  of the weight, MAD outlier test, confirmations and author trust as weight).
* **Holdout**: 20 % of reports (fixed by id) never feed the correction and
  check whether it would have helped.

The step is **only applied** when the repository variable `COMMUNITY_APPLY`
is `1` *and* the holdout confirms it; otherwise it runs in shadow mode and is
only reported (manifest `validation.community`, run summary). Only reports
visible to everyone are read (anon key), never friends-only posts.
