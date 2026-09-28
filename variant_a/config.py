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

# ── Old-snow base at the start of the spin-up (linear interp by elevation) ───
# An ASSUMED settled base, not a measurement. With the short spin-up below it
# only has to be plausible: what decides skiing is the top ~60 cm, and that is
# built by the recent snowfalls on top of it. HS shown in the popup still
# includes it, so it is a guess there too.
#
# Until now this table did nothing at all: the .sno layers were written with
# ne=0 finite elements, so SNOWPACK built zero nodes and every run -- national
# ones included -- started from bare ground. See snowpack_runner._BASE_ELEM_M.
INIT_ELEV = [1000, 1400, 1800, 2200, 2600, 3000, 3400]
INIT_HS = [0.10, 0.40, 0.80, 1.20, 1.70, 2.20, 2.60]     # m
INIT_RHO = [300, 300, 310, 320, 330, 340, 350]           # kg/m3

# Spin-up before the output window opens. SHORT on purpose: for ski quality
# the last few snowfalls and what the weather did to them since are what
# matter -- roughly the top 60 cm -- so three weeks of real forcing on an
# assumed base covers the layers the classifier reads (it works surface-down,
# with powder tiers at 15/30/50 cm). A full season was 4x more expensive per
# run (2.7 s vs 0.7 s) and mostly bought a deep pack the ski classes ignore.
# The price: deep weak layers from early winter are not modelled.
SPINUP_DAYS = 21

# ── Weather-point matrix (the Disentis design, nationally) ──────────────────
# Weather is sampled at a FEW points; around each one SNOWPACK runs a full
# matrix of virtual slopes that SHARE that weather, so the only differences
# between them are height, aspect and slope angle. The grid then takes each
# cell's values from the runs of similar height/aspect/slope at the nearest
# weather points.
#
# The previous design did the opposite -- one best-matching DEM cell per
# height x aspect x slope anywhere in a ~50 km subregion, each with its own
# weather -- so "north vs south at 2400 m" was also "grid cell A vs grid cell
# B". On a 10 km grid those points left a median of 3 per weather cell and
# only 40% of cells with three aspect sectors.
WEATHER_SPACING_KM = 15.0          # 135 weather points over the Swiss Alps
WEATHER_MIN_TOP_M = 1800.0         # skip cells whose terrain never reaches this
MATRIX_ELEV_MIN = 1200.0
MATRIX_ELEV_MAX = 3300.0
MATRIX_ELEV_STEP = 300.0           # up to 8 bands ...
MATRIX_ASPECTS = 8                 # ... x 8 aspects: the 8x8
MATRIX_SLOPES = (20.0, 38.0)       # moderate and steep; plus one flat run per band
MATRIX_NEIGHBOURS = 3              # weather points blended per grid cell (IDW)

# Temperature lapse from a weather point's reference elevation to each run.
TA_LAPSE_K_PER_M = -0.0065

# Forcing: 1-2 km models first, tried in order, via Open-Meteo's historical-
# forecast API; the ~9-25 km archive is the last resort. The coarse archive
# is what produced near-constant drizzle and near-calm wind in the national
# runs, which kept resetting every surface to new snow.
FORCING_MODELS = ("meteoswiss_icon_ch1", "meteoswiss_icon_ch2", "icon_d2")
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# Fills the far end of a live forecast once the MeteoSwiss models run out
# (ICON-CH2 reaches 5 days; the app's window ends ~6 days ahead).
FORCING_FAR_MODEL = "icon_seamless"
FORCING_MIN_COVERAGE = 0.95        # share of hours a model must actually fill

# The popup profile shows the top of the pack at fine resolution rather
# than the whole column coarsely; see profiles.resample.
PROFILE_TOP_CM = 60

# The forcing has to START EARLIER than the .sno ProfileDate. SNOWPACK begins
# one CALCULATION_STEP_LENGTH *before* the profile date, and MeteoIO resamples
# PSUM by accumulation over that step -- which needs a sample at or before the
# start of the accumulation window. With the .smet beginning exactly on the
# profile date there is nothing before it, and the very first timestep dies
# with "missing { precipitation }". A couple of lead-in days costs nothing
# (same single Open-Meteo request) and removes the edge case entirely.
FORCING_LEAD_DAYS = 2
