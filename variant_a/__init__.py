"""Variant A — representative-point SNOWPACK ski-quality pipeline for Switzerland.

Runs offline inside snow-mapper-v17 and exports a ski-quality layer (18-category +
simplified) plus interpolated snow-profile data for the app to display. Organised by
the earlier-defined national subregions.

Modules:
    config            paths, SNOWPACK binary, targets, thresholds
    subregions        national subregion segmentation (per-cell id + polygons)
    select_points     generic terrain-point selector (elev x aspect x slope)
    forcing           per-point Open-Meteo -> SNOWPACK .smet
    snowpack_runner   write .sno/.ini, run the SNOWPACK binary in parallel
    classify          18-cat + weak/crust refinement + simplified skier layer
    gridding          subregion-local KNN interpolation to the national raster
    profiles          density-top30 + per-point/per-cell snow-profile data
    export            app-facing layer overlays + profile JSON + manifest
"""
__all__ = ["config", "subregions", "select_points", "forcing",
           "snowpack_runner", "classify", "gridding", "profiles", "export"]
