"""Variant-A configuration: paths, external SNOWPACK binary, targets, thresholds.

Variant A = representative-point SNOWPACK ski-quality pipeline. It runs OFFLINE
inside this repo and exports a ski-quality layer + snow-profile data for the app.
SNOWPACK/MeteoIO are external C++ binaries (path via env), not part of the
deployed app runtime.
"""
from __future__ import annotations
import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR / "data"
SUBREGION_DIR = DATA_DIR / "subregions"
DEM_DIR = DATA_DIR / "dem"
PROJECT_ROOT = PKG_DIR.parent
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "variant_a"
CACHE_DIR = PROJECT_ROOT / "data" / "variant_a_cache"
WORK_DIR = CACHE_DIR / "work"           # per-point .sno/.ini/.pro live here

# National reference DEM (LV03 / EPSG:21781, 250 m) shipped with the module.
# The app's own LV95 Copernicus DEM can be passed in instead via the runner.
NATIONAL_DEM = DEM_DIR / "ch_lv03_250m.dem"
DEM_EPSG = 21781                         # LV03; app uses LV95 (2056) — see subregions.py

# External SNOWPACK binary (offline compute). Override with SNOWPACK_BIN env.
SNOWPACK_BIN = os.environ.get(
    "SNOWPACK_BIN",
    "/Users/gianimorf/slf-zivi-work copy/snowpack/bin/snowpack",
)
SNOWPACK_LIBS = os.environ.get(
    "SNOWPACK_LIBS",
    "/Users/gianimorf/slf-zivi-work copy/snowpack/lib:"
    "/Users/gianimorf/slf-zivi-work copy/meteoio/lib",
)

# ── Representative-point target grid (per subregion) ─────────────────────────
ELEV_BANDS = [1600, 2000, 2400, 2800]           # m
N_ASPECTS = 12                                    # every 30°
SLOPE_CLASSES = [("20-30", 20.0, 30.0), ("30-42", 30.0, 42.0), ("42-55", 42.0, 55.0)]

# ── KNN gridding (subregion-local interpolation) ─────────────────────────────
KNN_K = 8
KNN_DX = 25000.0        # horizontal scale [m]  (locality)
KNN_DE = 350.0          # elevation scale [m]
KNN_WASP = 0.9          # aspect weight

# ── Initial snowpack vs elevation (linear interp, for .sno spin-up) ──────────
INIT_ELEV = [1000, 1400, 1800, 2200, 2600, 3000, 3400]
INIT_HS = [0.10, 0.40, 0.80, 1.20, 1.70, 2.20, 2.60]     # m
INIT_RHO = [300, 300, 310, 320, 330, 340, 350]           # kg/m3

# Full-season spin-up: SNOWPACK needs months of forcing before the target window.
SPINUP_DAYS = 120

# The forcing has to START EARLIER than the .sno ProfileDate. SNOWPACK begins
# one CALCULATION_STEP_LENGTH *before* the profile date, and MeteoIO resamples
# PSUM by accumulation over that step -- which needs a sample at or before the
# start of the accumulation window. With the .smet beginning exactly on the
# profile date there is nothing before it, and the very first timestep dies
# with "missing { precipitation }". A couple of lead-in days costs nothing
# (same single Open-Meteo request) and removes the edge case entirely.
FORCING_LEAD_DAYS = 2
