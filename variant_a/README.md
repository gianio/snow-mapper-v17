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
