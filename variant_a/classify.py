"""
Script 7: Ski quality classification from 12 representative SNOWPACK .pro files.

Reads all codes from each .pro file (heights, density, LWC, DD, SP, grain type,
ice fraction, hand hardness), applies an 18-label ski quality algorithm surface→down,
interpolates the continuous metrics over the 5 km × 5 km Alpine3D grid using
elevation + aspect IDW weights, and saves per-timestep grid arrays.

18 labels:
  no_snow, thin_cover, ice, carrying_crust, breaking_crust, fine_crust,
  fine_crust_over_powder, breaking_crust_over_powder, hard_pack, settled,
  spring_corn, depth_hoar, surface_hoar, thin_powder, powder,
  good_powder, deep_powder, wet_powder

Output:
  output/ski_quality/ski_metrics.json  — per-point ski quality + metrics time series
  output/ski_quality/ski_grid.npy      — dict: timestamps (list) + grid (ntime×nrows×ncols uint8)
"""

import os, json
import numpy as np
from datetime import datetime

# ── Ported into snow-mapper-v17/variant_a from the SNOWPACK sandbox script
#    07_ski_quality.py. Pure classification logic only; paths / IDW gridding /
#    point loading live in variant_a.gridding / variant_a.snowpack_runner. ──────

# ── 18(+3) ski quality labels ───────────────────────────────────────────────────
SKI_LABELS = [
    "no_snow",                    # 0
    "thin_cover",                 # 1
    "ice",                        # 2
    "carrying_crust",             # 3
    "breaking_crust",             # 4
    "fine_crust",                 # 5
    "fine_crust_over_powder",     # 6
    "breaking_crust_over_powder", # 7
    "hard_pack",                  # 8
    "settled",                    # 9
    "spring_corn",                # 10
    "depth_hoar",                 # 11
    "surface_hoar",               # 12
    "thin_powder",                # 13
    "powder",                     # 14
    "good_powder",                # 15
    "deep_powder",                # 16
    "wet_powder",                 # 17
    "fine_crust_over_weak",       # 18  crust on facets / depth hoar (collapsy)
    "breaking_crust_over_weak",   # 19
    "carrying_crust_over_weak",   # 20  slab on a weak layer (avalanche-relevant)
]
LABEL_INDEX = {l: i for i, l in enumerate(SKI_LABELS)}

# ── Representative points (must match 06_run_point_snowpacks.py) ─────────────
# Defaults are target values; load_real_point_params() updates them with actual
# DEM-derived slope / aspect / elevation read from the .sno files.
# POINTS is set by the caller (gridding) when the legacy build_weight_matrix is used.
POINTS = []


def load_real_point_params():
    """Update POINTS with real slope/aspect/elevation from .sno files if present."""
    updated = 0
    for pt in POINTS:
        sno_path = os.path.join(SNO_PT, f"{pt['label']}.sno")
        if not os.path.exists(sno_path):
            continue
        with open(sno_path) as f:
            for line in f:
                s = line.strip()
                if s.startswith("SlopeAngle"):
                    pt["slope"]  = float(s.split("=")[1].strip())
                elif s.startswith("SlopeAzi"):
                    pt["aspect"] = float(s.split("=")[1].strip())
                elif s.startswith("altitude"):
                    pt["elev"]   = float(s.split("=")[1].strip())
                elif s == "[DATA]":
                    break
        updated += 1
    return updated

# ── Classification thresholds ─────────────────────────────────────────────────
CRUST_DENSITY_MIN  = 380.0   # kg/m³ — wind crust / high-density MF crust
MF_CRUST_DENSITY_MIN = 130.0 # kg/m³ — minimum density for MF-grain layer to count as crust
ICE_DENSITY_MIN    = 700.0   # kg/m³ — ice layer
CRUST_HARD_MIN     = 3.5     # hand hardness index (Rammsonde)
POWDER_DENSITY_MAX = 200.0   # kg/m³ — true low-density snow; 200–280 is settled, not powder
POWDER_DD_MIN      = 0.01    # DD threshold for "still dendritic"
WET_LWC_MIN        = 1.0     # % LWC → wet snow
THIN_COVER_HS      = 20.0    # cm — HS below this → thin_cover label
CRUST_TRACE        = 0.2     # cm — minimum interpolated crust thickness to register
                             # as a crust (was 0.02; raised so trace IDW-smearing of a
                             # single crusted point does not count as a crust everywhere)
CRUST_FINE         = 0.25    # cm — crust thickness classes
CRUST_BREAKING     = 2.0     # cm
CRUST_GATE         = 0.5     # min fraction of a grid cell's IDW weight that must come
                             # from actually-crusted points before ANY crust is assigned;
                             # prevents one crusted slope from smearing crust across the
                             # whole domain (a crust exists on a slope or it does not)
POWDER_THIN        = 5.0     # cm
POWDER_GOOD        = 15.0    # cm
POWDER_DEEP        = 30.0    # cm
WEAK_BELOW_MIN     = 3.0     # cm — min faceted/depth-hoar layer under a crust to flag "_over_weak"


# ─────────────────────────────────────────────────────────────────────────────
#  .pro parser
# ─────────────────────────────────────────────────────────────────────────────

CODES_NEEDED = {"0501", "0502", "0506", "0508", "0509", "0512", "0513", "0515"}


def _parse_floats(parts, n):
    """Parse n float values from parts list; fill missing with np.nan."""
    vals = []
    for p in parts[2 : 2 + n]:
        try:
            vals.append(float(p))
        except ValueError:
            vals.append(np.nan)
    while len(vals) < n:
        vals.append(np.nan)
    return np.array(vals, dtype=np.float32)


def parse_pro(pro_path):
    """
    Parse a SNOWPACK .pro file.

    Returns list of timestep dicts, each with:
      'dt'      : datetime
      'heights' : np.array cm, cumulative from bottom (top of each element)
      'density' : np.array kg/m³
      'lw'      : np.array % LWC
      'dd'      : np.array dendricity 0-1
      'sp'      : np.array sphericity 0-1
      'rg'      : np.array grain size mm
      'grain'   : np.array Swiss grain code F1F2F3 (int)
      'ice_frac': np.array ice volume %
      'hardness': np.array hand hardness index
    """
    timesteps = []
    current = {}

    with open(pro_path) as f:
        in_data = False
        for raw_line in f:
            line = raw_line.strip()

            if line == "[DATA]":
                in_data = True
                continue
            if not in_data:
                continue

            parts = line.split(",")
            if len(parts) < 2:
                continue
            code = parts[0].strip()

            if code == "0500":
                # Save previous timestep if it has layer heights
                if current.get("dt") and "heights" in current:
                    timesteps.append(current)
                current = {}
                try:
                    current["dt"] = datetime.strptime(parts[1].strip(), "%d.%m.%Y %H:%M:%S")
                except ValueError:
                    pass

            elif code in CODES_NEEDED and "dt" in current:
                try:
                    n = int(parts[1])
                except (ValueError, IndexError):
                    continue

                arr = _parse_floats(parts, n)

                if code == "0501":
                    current["heights"] = arr
                    current["n"] = n
                elif code == "0502":
                    current["density"] = arr[:current.get("n", n)]
                elif code == "0506":
                    current["lw"] = arr[:current.get("n", n)]
                elif code == "0508":
                    current["dd"] = arr[:current.get("n", n)]
                elif code == "0509":
                    current["sp"] = arr[:current.get("n", n)]
                elif code == "0512":
                    current["rg"] = arr[:current.get("n", n)]
                elif code == "0513":
                    n_snow = current.get("n", n)
                    # 0513 sometimes has n_snow+1 entries (extra soil marker)
                    current["grain"] = arr[:n_snow].astype(np.int32)
                elif code == "0515":
                    current["ice_frac"] = arr[:current.get("n", n)]
                elif code == "0534":
                    current["hardness"] = arr[:current.get("n", n)]

    if current.get("dt") and "heights" in current:
        timesteps.append(current)

    return timesteps


def _ensure(ts, key, n, default=0.0):
    """Return array from timestep or fill with default."""
    if key in ts:
        arr = ts[key]
        if len(arr) < n:
            return np.concatenate([arr, np.full(n - len(arr), default, dtype=np.float32)])
        return arr[:n]
    return np.full(n, default, dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
#  Ski quality classifier (per point, per timestep)
# ─────────────────────────────────────────────────────────────────────────────

def assess_ski_quality(ts):
    """
    Classify one timestep from a parsed .pro record.

    Returns dict with:
      label            : str (one of 18 SKI_LABELS)
      total_hs_cm      : float
      powder_depth_cm  : float (0 if no powder at/near surface)
      powder_dd        : float (mean DD of powder layers)
      powder_lw        : float (mean LWC% of powder layers)
      crust_thick_cm   : float (crust at surface; 0 if none)
      surface_density  : float
      surface_hardness : float
      surface_lw       : float
      surface_grain    : int   (primary grain type digit 1-9)
    """
    n = ts.get("n", 0)

    result = {
        "label":           "no_snow",
        "total_hs_cm":     0.0,
        "powder_depth_cm": 0.0,
        "powder_dd":       0.0,
        "powder_lw":       0.0,
        "crust_thick_cm":  0.0,
        "weak_below_cm":   0.0,
        "surface_density": 0.0,
        "surface_hardness":0.0,
        "surface_lw":      0.0,
        "surface_grain":   0,
    }

    if n == 0:
        return result

    heights  = ts["heights"]
    total_hs = float(heights[-1]) if len(heights) > 0 else 0.0
    result["total_hs_cm"] = total_hs

    if total_hs < 0.5:
        return result   # no_snow

    density  = _ensure(ts, "density",  n)
    lw       = _ensure(ts, "lw",       n)
    dd       = _ensure(ts, "dd",       n)
    ice_frac = _ensure(ts, "ice_frac", n)
    hardness = _ensure(ts, "hardness", n)
    grain    = _ensure(ts, "grain",    n).astype(np.int32)

    # Layer thicknesses (bottom→top, i=0 is deepest)
    thicks = np.empty(n, dtype=np.float32)
    thicks[0] = heights[0]
    thicks[1:] = np.diff(heights)
    thicks = np.maximum(thicks, 0.0)

    # Top layer = surface (index n-1)
    surf_density  = float(density[-1])
    surf_lw       = float(lw[-1])
    surf_dd       = float(dd[-1])
    surf_hard     = float(hardness[-1])
    surf_grain    = int(grain[-1]) // 100    # primary digit F1

    result["surface_density"]  = surf_density
    result["surface_hardness"] = surf_hard
    result["surface_lw"]       = surf_lw
    result["surface_grain"]    = surf_grain

    thin = total_hs < THIN_COVER_HS

    # ── Special surface states ───────────────────────────────────────────────

    if surf_density >= ICE_DENSITY_MIN or float(ice_frac[-1]) > 90:
        result["label"] = "thin_cover" if thin else "ice"
        return result

    if surf_grain == 6:                         # SH — surface hoar
        result["label"] = "thin_cover" if thin else "surface_hoar"
        return result

    if surf_grain == 7 and surf_lw > WET_LWC_MIN:  # MF + wet → spring corn
        result["label"] = "thin_cover" if thin else "spring_corn"
        return result

    # ── Crust at surface? ───────────────────────────────────────────────────
    # Crust = density ≥ CRUST_DENSITY_MIN, OR hardness ≥ CRUST_HARD_MIN (code 0532),
    # OR MF-type grain (F1=7) with density ≥ MF_CRUST_DENSITY_MIN
    # (excludes negative hardness which are SNOWPACK placeholders)

    def _is_crust(i):
        hard = float(hardness[i])
        g1 = int(grain[i]) // 100
        return (float(density[i]) >= CRUST_DENSITY_MIN or
                (hard > 0 and hard >= CRUST_HARD_MIN) or
                (g1 == 7 and float(density[i]) >= MF_CRUST_DENSITY_MIN))

    if _is_crust(n - 1):
        crust_cm = 0.0
        i = n - 1
        while i >= 0 and _is_crust(i):
            crust_cm += float(thicks[i])
            i -= 1

        # Powder below the crust?
        powder_below = 0.0
        while i >= 0:
            d_i = float(dd[i])
            rho_i = float(density[i])
            if d_i >= POWDER_DD_MIN or rho_i < POWDER_DENSITY_MAX:
                powder_below += float(thicks[i])
                i -= 1
            else:
                break

        # Weak layer (facets FC=4 / depth hoar DH=5) directly below the crust?
        weak_below = 0.0
        j = i
        while j >= 0:
            g1 = int(grain[j]) // 100
            if g1 in (4, 5) or (float(density[j]) < POWDER_DENSITY_MAX and float(rg[j]) >= 1.0):
                weak_below += float(thicks[j]); j -= 1
            else:
                break

        result["crust_thick_cm"]  = crust_cm
        result["powder_depth_cm"] = powder_below
        result["weak_below_cm"]   = weak_below

        if crust_cm < CRUST_FINE:
            base = "fine_crust"
        elif crust_cm < CRUST_BREAKING:
            base = "breaking_crust"
        else:
            base = "carrying_crust"

        if powder_below >= POWDER_THIN and base != "carrying_crust":
            lbl = base + "_over_powder"
        elif weak_below >= WEAK_BELOW_MIN:
            lbl = base + "_over_weak"
        else:
            lbl = base

        result["label"] = "thin_cover" if thin else lbl
        return result

    # ── Powder at surface? ──────────────────────────────────────────────────
    # MF-grain layers with sufficient density are crusts, not powder — stop there.
    def _is_powder(i):
        g1 = int(grain[i]) // 100
        if g1 == 7 and float(density[i]) >= MF_CRUST_DENSITY_MIN:
            return False
        return float(dd[i]) >= POWDER_DD_MIN or float(density[i]) < POWDER_DENSITY_MAX

    if _is_powder(n - 1):
        powder_cm = 0.0
        dd_vals   = []
        lw_vals   = []
        i = n - 1
        while i >= 0 and _is_powder(i):
            powder_cm += float(thicks[i])
            dd_vals.append(float(dd[i]))
            lw_vals.append(float(lw[i]))
            i -= 1

        mean_dd = float(np.nanmean(dd_vals)) if dd_vals else 0.0
        mean_lw = float(np.nanmean(lw_vals)) if lw_vals else 0.0

        result["powder_depth_cm"] = powder_cm
        result["powder_dd"]       = mean_dd
        result["powder_lw"]       = mean_lw

        # Buried crust directly below thin powder → dominated by crust experience
        if i >= 0 and _is_crust(i) and powder_cm < POWDER_THIN:
            crust_cm = 0.0
            j = i
            while j >= 0 and _is_crust(j):
                crust_cm += float(thicks[j])
                j -= 1
            result["crust_thick_cm"] = crust_cm
            if crust_cm >= CRUST_BREAKING:
                lbl = "carrying_crust"
            elif crust_cm >= CRUST_FINE:
                lbl = "breaking_crust_over_powder" if powder_cm >= 0.5 else "breaking_crust"
            else:
                lbl = "fine_crust_over_powder" if powder_cm >= 0.5 else "fine_crust"
            result["label"] = "thin_cover" if thin else lbl
            return result

        if mean_lw > WET_LWC_MIN:
            lbl = "wet_powder"
        elif powder_cm < POWDER_THIN:
            lbl = "thin_powder"
        elif powder_cm < POWDER_GOOD:
            lbl = "powder"
        elif powder_cm < POWDER_DEEP:
            lbl = "good_powder"
        else:
            lbl = "deep_powder"

        result["label"] = "thin_cover" if thin else lbl
        return result

    # ── Settled / faceted / depth hoar (no powder, no crust at surface) ─────
    if surf_grain == 5:                         # DH — depth hoar at surface
        result["label"] = "thin_cover" if thin else "depth_hoar"
        return result

    if surf_hard > 0 and surf_hard >= CRUST_HARD_MIN:
        lbl = "hard_pack"
    else:
        lbl = "settled"

    result["label"] = "thin_cover" if thin else lbl
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Parse all 12 .pro files
# ─────────────────────────────────────────────────────────────────────────────

def find_pro(label):
    run_dir = os.path.join(RUNS_OUT, label)
    if not os.path.isdir(run_dir):
        return None
    for fname in os.listdir(run_dir):
        if fname.endswith(".pro"):
            return os.path.join(run_dir, fname)
    return None


def parse_all_points():
    """Returns dict label → list of (datetime, ski_quality_dict)."""
    result = {}
    for pt in POINTS:
        label = pt["label"]
        pro_path = find_pro(label)
        if pro_path is None:
            print(f"  {label:12s}  MISSING .pro — skipping")
            continue

        timesteps = parse_pro(pro_path)
        series = []
        for ts in timesteps:
            q = assess_ski_quality(ts)
            series.append((ts["dt"], q))

        result[label] = series
        n   = len(series)
        if n:
            labels = [s[1]["label"] for s in series]
            from collections import Counter
            top3 = Counter(labels).most_common(3)
            top3_str = ", ".join(f"{l}:{c}" for l, c in top3)
            print(f"  {label:12s}  {n} steps  top: {top3_str}")
        else:
            print(f"  {label:12s}  0 steps (empty .pro?)")

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  DEM reader + aspect computation
# ─────────────────────────────────────────────────────────────────────────────

def read_dem(dem_path):
    """Return (elevation 2D array, header dict)."""
    header = {}
    with open(dem_path) as f:
        for _ in range(6):
            key, val = f.readline().split()
            header[key.lower()] = float(val)
    data = np.loadtxt(dem_path, skiprows=6)
    nodata = header.get("nodata_value", -999.0)
    data = np.where(data == nodata, np.nan, data)
    return data, header


def compute_aspect(dem):
    """
    Compute aspect in degrees (0=N, 90=E, 180=S, 270=W).
    Uses numpy gradient; flat pixels → 0°.
    """
    dy, dx = np.gradient(dem)
    asp = np.degrees(np.arctan2(-dx, dy)) % 360
    asp = np.where(np.isnan(dem), np.nan, asp)
    return asp


def compute_slope(dem, cellsize=50.0):
    """Return slope angle in degrees (0–90)."""
    dy, dx = np.gradient(dem, cellsize)
    sl = np.degrees(np.arctan(np.sqrt(dx**2 + dy**2)))
    return np.where(np.isnan(dem), np.nan, sl)


# ─────────────────────────────────────────────────────────────────────────────
#  IDW weight matrix  (nrows × ncols × 12)
# ─────────────────────────────────────────────────────────────────────────────

def build_weight_matrix(dem, asp, slope):
    """
    IDW weights in normalised (elevation, aspect, slope-angle) space.

      d_elev  = |z_cell − z_pt|        / 900   (elev range 1800–2700 m)
      d_asp   = circ_dist(a, a_pt)     / 180
      d_slope = |slope_cell − slope_pt| / 38   (slope range 12–50°)
      d       = sqrt(d_elev² + d_asp² + d_slope²)
      w       = 1 / (d + ε)²

    Adding slope angle ensures a steep couloir does not contaminate a flat
    plateau at the same elevation and aspect, and vice versa.
    Returns W of shape (nrows, ncols, npts), each row sums to 1.
    """
    nrows, ncols = dem.shape
    npts = len(POINTS)
    W = np.zeros((nrows, ncols, npts), dtype=np.float32)

    pt_elevs  = np.array([p["elev"]            for p in POINTS], dtype=np.float32)
    pt_aspects = np.array([p["aspect"]          for p in POINTS], dtype=np.float32)
    pt_slopes  = np.array([p.get("slope", 30.0) for p in POINTS], dtype=np.float32)

    ELEV_RANGE  = 2400.0  # 3400 − 1000 (national bands)
    SLOPE_RANGE = 38.0    # 50 − 12

    for pi in range(npts):
        d_elev = np.abs(dem - pt_elevs[pi]) / ELEV_RANGE

        raw_diff  = asp - pt_aspects[pi]
        circ_diff = np.abs(raw_diff % 360)
        circ_diff = np.where(circ_diff > 180, 360 - circ_diff, circ_diff)
        d_asp = circ_diff / 180.0

        d_slope = np.abs(slope - pt_slopes[pi]) / SLOPE_RANGE

        d = np.sqrt(d_elev**2 + d_asp**2 + d_slope**2)
        W[:, :, pi] = 1.0 / (d + 0.02) ** 2

    W_sum = W.sum(axis=2, keepdims=True)
    W_sum = np.where(W_sum == 0, 1.0, W_sum)
    W /= W_sum
    W[np.isnan(dem)] = 0.0
    return W


# ─────────────────────────────────────────────────────────────────────────────
#  Grid-level classification from interpolated continuous metrics
# ─────────────────────────────────────────────────────────────────────────────

def classify_grid_metrics(hs, powder, crust, pdd, plw, sdens, shard, slw, weak=None):
    """
    Element-wise classification from interpolated scalar fields.
    `weak` = interpolated faceted/depth-hoar thickness below a surface crust (cm);
    where present it routes crust cells to the *_over_weak labels.
    Returns uint8 array with LABEL_INDEX values.
    """
    out = np.full(hs.shape, LABEL_INDEX["no_snow"], dtype=np.uint8)
    if weak is None:
        weak = np.zeros_like(hs)
    has_weak = weak >= WEAK_BELOW_MIN
    thin = hs < THIN_COVER_HS

    # no_snow
    out[hs < 0.5] = LABEL_INDEX["no_snow"]

    # has snow
    snow = hs >= 0.5

    # ice (very high density at surface)
    mask = snow & (sdens >= ICE_DENSITY_MIN)
    out[mask] = np.where(thin[mask], LABEL_INDEX["thin_cover"], LABEL_INDEX["ice"])

    # spring corn (wet + high density, not ice)
    mask = snow & (sdens < ICE_DENSITY_MIN) & (slw > WET_LWC_MIN) & (sdens > 350)
    out[mask] = np.where(thin[mask], LABEL_INDEX["thin_cover"], LABEL_INDEX["spring_corn"])

    has_pow = powder >= POWDER_THIN

    # carrying crust — weak substrate (slab on weak) vs plain
    mask = snow & (crust >= CRUST_BREAKING) & (sdens < ICE_DENSITY_MIN)
    out[mask & has_weak]  = np.where(thin[mask & has_weak],
                                     LABEL_INDEX["thin_cover"], LABEL_INDEX["carrying_crust_over_weak"])
    out[mask & ~has_weak] = np.where(thin[mask & ~has_weak],
                                     LABEL_INDEX["thin_cover"], LABEL_INDEX["carrying_crust"])

    # breaking crust — substrate priority: powder > weak > plain
    mask = snow & (crust >= CRUST_FINE) & (crust < CRUST_BREAKING)
    m_p = mask & has_pow; m_w = mask & ~has_pow & has_weak; m_h = mask & ~has_pow & ~has_weak
    out[m_p] = np.where(thin[m_p], LABEL_INDEX["thin_cover"], LABEL_INDEX["breaking_crust_over_powder"])
    out[m_w] = np.where(thin[m_w], LABEL_INDEX["thin_cover"], LABEL_INDEX["breaking_crust_over_weak"])
    out[m_h] = np.where(thin[m_h], LABEL_INDEX["thin_cover"], LABEL_INDEX["breaking_crust"])

    # fine crust — substrate priority: powder > weak > plain
    mask = snow & (crust > CRUST_TRACE) & (crust < CRUST_FINE)
    m_p = mask & has_pow; m_w = mask & ~has_pow & has_weak; m_h = mask & ~has_pow & ~has_weak
    out[m_p] = np.where(thin[m_p], LABEL_INDEX["thin_cover"], LABEL_INDEX["fine_crust_over_powder"])
    out[m_w] = np.where(thin[m_w], LABEL_INDEX["thin_cover"], LABEL_INDEX["fine_crust_over_weak"])
    out[m_h] = np.where(thin[m_h], LABEL_INDEX["thin_cover"], LABEL_INDEX["fine_crust"])

    # powder (no significant crust)
    no_crust = snow & (crust <= CRUST_TRACE)
    wet_pow  = no_crust & (powder >= 0.5) & (plw > WET_LWC_MIN)
    out[wet_pow] = np.where(thin[wet_pow], LABEL_INDEX["thin_cover"], LABEL_INDEX["wet_powder"])

    dry_pow = no_crust & (powder >= 0.5) & (plw <= WET_LWC_MIN)
    deep_p  = dry_pow & (powder >= POWDER_DEEP)
    good_p  = dry_pow & (powder >= POWDER_GOOD) & (powder < POWDER_DEEP)
    med_p   = dry_pow & (powder >= POWDER_THIN)  & (powder < POWDER_GOOD)
    thin_p  = dry_pow & (powder <  POWDER_THIN)

    for mask2, lbl in [(deep_p, "deep_powder"), (good_p, "good_powder"),
                       (med_p, "powder"), (thin_p, "thin_powder")]:
        out[mask2] = np.where(thin[mask2], LABEL_INDEX["thin_cover"], LABEL_INDEX[lbl])

    # settled / hard pack (no crust, no powder)
    settled_zone = no_crust & (powder < 0.5)
    hard_p = settled_zone & (shard > 0) & (shard >= CRUST_HARD_MIN)
    out[hard_p] = np.where(thin[hard_p], LABEL_INDEX["thin_cover"], LABEL_INDEX["hard_pack"])
    soft_s = settled_zone & ~hard_p & (hs >= 0.5)
    out[soft_s] = np.where(thin[soft_s], LABEL_INDEX["thin_cover"], LABEL_INDEX["settled"])

    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Build common time axis from representative point data
# ─────────────────────────────────────────────────────────────────────────────

def common_timestamps(all_series):
    """Return sorted list of datetimes present in ALL point series."""
    sets = []
    for series in all_series.values():
        sets.append(set(dt for dt, _ in series))
    if not sets:
        return []
    common = sets[0].intersection(*sets[1:])
    return sorted(common)


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(SKI_OUT, exist_ok=True)

    print("Parsing .pro files ...\n")
    all_series = parse_all_points()

    if not all_series:
        print("No .pro files found. Run 06_run_point_snowpacks.py first.")
        return

    # ── Save per-point JSON ──────────────────────────────────────────────────
    json_out = {}
    for label, series in all_series.items():
        rows = []
        for dt, q in series:
            row = {"dt": dt.isoformat()}
            row.update(q)
            rows.append(row)
        json_out[label] = rows

    json_path = os.path.join(SKI_OUT, "ski_metrics.json")
    with open(json_path, "w") as f:
        json.dump(json_out, f, indent=2)
    print(f"\nSaved: {json_path}")

    # ── Load DEM and compute aspect ──────────────────────────────────────────
    print("\nLoading DEM and computing aspect ...")
    dem, hdr = read_dem(DEM_PATH)
    asp = compute_aspect(dem)
    slope = compute_slope(dem, hdr.get("cellsize", 50.0))
    nrows, ncols = dem.shape
    print(f"  DEM: {nrows}×{ncols}, elev range {np.nanmin(dem):.0f}–{np.nanmax(dem):.0f} m")

    # ── Build IDW weight matrix ─────────────────────────────────────────────
    print("Building IDW weight matrix ...")
    W = build_weight_matrix(dem, asp, slope)    # (nrows, ncols, 12)
    W_flat = W.reshape(-1, len(POINTS))  # (nrows*ncols, 12)
    print(f"  Weight matrix shape: {W.shape}")

    # ── Build common time axis ───────────────────────────────────────────────
    timestamps = common_timestamps(all_series)
    if not timestamps:
        # Use union of all timestamps if no point has complete coverage
        all_ts = set()
        for series in all_series.values():
            all_ts.update(dt for dt, _ in series)
        timestamps = sorted(all_ts)
    print(f"\nTime axis: {len(timestamps)} steps  "
          f"({timestamps[0].isoformat()} → {timestamps[-1].isoformat()})")

    # Build per-label lookup: label → dt → metrics dict
    series_idx = {}
    for label, series in all_series.items():
        series_idx[label] = {dt: q for dt, q in series}

    # Metric order: [powder_depth, crust_thick, powder_dd, powder_lw,
    #                surface_density, surface_hardness, surface_lw, total_hs]
    N_METRICS = 8
    IDX_POWDER = 0; IDX_CRUST = 1; IDX_PDD = 2; IDX_PLW = 3
    IDX_SDENS = 4;  IDX_SHARD = 5; IDX_SLW = 6; IDX_HS = 7

    # ── Grid interpolation per timestep ────────────────────────────────────
    ntime = len(timestamps)
    ski_grid = np.zeros((ntime, nrows, ncols), dtype=np.uint8)

    print("\nInterpolating to grid ...")
    for ti, dt in enumerate(timestamps):
        # Collect per-point metric vector (12 × 8)
        metrics_pt = np.zeros((len(POINTS), N_METRICS), dtype=np.float32)
        for pi, pt in enumerate(POINTS):
            label = pt["label"]
            q = series_idx.get(label, {}).get(dt)
            if q is None:
                # Find nearest timestep
                available = series_idx.get(label, {})
                if available:
                    nearest = min(available.keys(), key=lambda d: abs((d - dt).total_seconds()))
                    q = available[nearest]
                else:
                    continue
            metrics_pt[pi, IDX_POWDER] = q["powder_depth_cm"]
            metrics_pt[pi, IDX_CRUST]  = q["crust_thick_cm"]
            metrics_pt[pi, IDX_PDD]    = q["powder_dd"]
            metrics_pt[pi, IDX_PLW]    = q["powder_lw"]
            metrics_pt[pi, IDX_SDENS]  = q["surface_density"]
            metrics_pt[pi, IDX_SHARD]  = q["surface_hardness"]
            metrics_pt[pi, IDX_SLW]    = q["surface_lw"]
            metrics_pt[pi, IDX_HS]     = q["total_hs_cm"]

        # Interpolate: (nrows*ncols, 12) @ (12, 8) → (nrows*ncols, 8)
        metrics_grid = (W_flat @ metrics_pt).reshape(nrows, ncols, N_METRICS)

        hs    = metrics_grid[:, :, IDX_HS]
        powd  = metrics_grid[:, :, IDX_POWDER]
        crust = metrics_grid[:, :, IDX_CRUST]
        pdd   = metrics_grid[:, :, IDX_PDD]
        plw   = metrics_grid[:, :, IDX_PLW]
        sdens = metrics_grid[:, :, IDX_SDENS]
        shard = metrics_grid[:, :, IDX_SHARD]
        slw   = metrics_grid[:, :, IDX_SLW]

        # Gate crust: crust thickness is a spatially discontinuous, slope-specific
        # quantity, so a plain IDW blend smears one crusted point over the whole
        # domain (its weights sum to 1, giving every cell a fraction of its crust).
        # Keep crust only where enough of the cell's weight comes from points that
        # actually have a crust; elsewhere force it to zero.
        crusted_pt = (metrics_pt[:, IDX_CRUST] > 0.0).astype(np.float32)   # (12,)
        crust_wt   = (W_flat @ crusted_pt).reshape(nrows, ncols)           # fraction 0..1
        crust      = np.where(crust_wt >= CRUST_GATE, crust, 0.0)

        ski_grid[ti] = classify_grid_metrics(hs, powd, crust, pdd, plw, sdens, shard, slw)

        if (ti + 1) % 24 == 0 or ti == ntime - 1:
            print(f"  {ti+1}/{ntime}  {dt.strftime('%Y-%m-%d %H:00')}")

    # ── Save grid ───────────────────────────────────────────────────────────
    grid_path = os.path.join(SKI_OUT, "ski_grid.npy")
    np.save(grid_path, {
        "timestamps": [dt.isoformat() for dt in timestamps],
        "grid":       ski_grid,
        "labels":     SKI_LABELS,
        "dem_header": hdr,
    })
    print(f"\nSaved: {grid_path}  shape={ski_grid.shape}")
    print("Done. Run 05_make_html_map.py to add the Ski Quality layer to the map.")


# ═════════════════════════════════════════════════════════════════════════════
#  Colour tables + simplified skier layer (ported from sandbox 73_ski_simple.py)
# ═════════════════════════════════════════════════════════════════════════════

# RGBA for the 18(+3) ski labels (used when rendering the detailed layer)
SKI_RGBA = {0:(0,0,0,0),1:(200,200,200,150),2:(80,100,180,220),3:(160,20,20,220),
 4:(220,70,0,210),5:(255,150,30,190),6:(255,200,50,190),7:(255,120,0,200),
 8:(120,80,40,200),9:(160,150,130,190),10:(240,100,120,200),11:(200,170,100,200),
 12:(200,190,240,200),13:(180,220,250,200),14:(80,160,240,220),15:(20,110,220,230),
 16:(0,50,180,240),17:(80,200,220,200),18:(210,130,210,215),19:(170,50,170,220),
 20:(120,15,120,230)}

# Simplified, skier-oriented scheme (Powder depth tiers / crusts / compact / dust / wet)
SIMPLE_LABELS = ["none","thin","powder_5_15","powder_15_30","powder_30_50","powder_gt_50",
 "dust_on_crust","dust_on_compact","thin_crust","breaking_crust","carrying_crust","compact","wet"]
SIMPLE_RGBA = {0:(0,0,0,0),1:(205,205,205,140),2:(155,210,248,215),3:(90,165,235,225),
 4:(35,105,215,235),5:(10,45,150,245),6:(206,160,232,220),7:(140,205,195,215),
 8:(250,222,120,215),9:(236,130,40,220),10:(165,25,25,225),11:(150,150,120,205),
 12:(150,90,210,215)}
# simplified thresholds
S_THIN_HS=15.0; S_DUST=5.0; S_PT1=15.0; S_PT2=30.0; S_PT3=50.0
S_CRUST_TRACE=0.3; S_THIN_CRUST=1.5; S_CARRY=4.0; S_WET_LWC=1.0

def classify_simple(hs, powder, crust, sdens, slw):
    """Vectorised simplified skier classification from interpolated metric fields.
    Inputs are numpy arrays (same shape). Returns uint8 SIMPLE_LABELS indices."""
    hs=np.asarray(hs,float); powder=np.asarray(powder,float); crust=np.asarray(crust,float)
    slw=np.asarray(slw,float)
    lab=np.zeros(hs.shape,np.uint8); snow=hs>=1.0; thin=hs<S_THIN_HS
    lab[snow]=11                                             # default compact
    cs=snow&(crust>=S_CRUST_TRACE)&(powder<1.0)             # surface crust
    lab[cs&(crust<S_THIN_CRUST)]=8
    lab[cs&(crust>=S_THIN_CRUST)&(crust<S_CARRY)]=9
    lab[cs&(crust>=S_CARRY)]=10
    du=snow&(powder>=1.0)&(powder<S_DUST)                   # dust on a base
    lab[du&(crust>=S_CRUST_TRACE)]=6
    lab[du&(crust<S_CRUST_TRACE)]=7
    pw=snow&(powder>=S_DUST)                                 # powder tiers
    lab[pw&(powder<S_PT1)]=2; lab[pw&(powder>=S_PT1)&(powder<S_PT2)]=3
    lab[pw&(powder>=S_PT2)&(powder<S_PT3)]=4; lab[pw&(powder>=S_PT3)]=5
    lab[snow&(slw>S_WET_LWC)]=12                             # wet/spring overrides
    lab[snow&thin]=1; lab[~snow]=0
    return lab


if __name__ == "__main__":
    main()
