"""Write SNOWPACK inputs (.sno init + .ini) per representative point and run the
external SNOWPACK binary in parallel. Forcing = the per-point .smet from forcing.py
(single-station INCOMING radiation). Ported/streamlined from sandbox 71/06.
"""
from __future__ import annotations
import os, glob, time, subprocess
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np

from . import config

_END = None  # set per run

# SNOWPACK integration step [min]. Also drives the PSUM accumulation
# period in the .ini -- see write_ini.
CALC_STEP_MIN = 30
# Fallback profile write cadence [days] when no export step is given.
PROF_DAYS_BETWEEN = 0.25


def _init_snow(elev):
    hs = max(float(np.interp(elev, config.INIT_ELEV, config.INIT_HS)), 0.05)
    rho = float(np.interp(elev, config.INIT_ELEV, config.INIT_RHO))
    return hs, rho


def _profile_start_iso(target_date, spinup_days):
    from datetime import datetime, timedelta
    d = datetime.strptime(target_date, "%Y-%m-%d").date() - timedelta(days=spinup_days)
    return d.isoformat()


# The starting base: settled, rounded, dry old snow. Only a base -- with a
# short spin-up the top ~60 cm that decides skiing is built by the recent
# snowfalls on top of it, not by this.
#
# It used to be written with marker mk=7, dendricity 0.5 and sphericity 0.5.
# mk % 10 == 7 is SNOWPACK's ICE marker (DataClasses.cc counts it as ice, and
# more than 2 m of it flags the column as a glacier -- the old 3000 m start was
# 2.2 m). Dendricity 0.5 is half-fresh snow texture. So the "old snow" base
# was glacier-marked fresh-looking snow, and came out of the model as melt
# forms (grain code 770). The shipped SNOWPACK examples describe metamorphosed
# snow as dd=0, sp=1 with a non-ice marker, which is what this is now.
_BASE_RG, _BASE_RB = 0.35, 0.15      # grain / bond radius [mm]: rounded, sintered
_BASE_DD, _BASE_SP = 0.0, 1.0        # no dendricity left, fully rounded
_BASE_MK = 0                         # dry old snow; NOT 7 (ice) or 8 (ice layer)
# bottom -> top: near 0 degC at the ground, colder towards the surface, and
# denser at the bottom where it has been loaded longest.
_BASE_LAYERS = ((271.65, +30.0), (269.65, 0.0), (267.65, -30.0))
# Finite elements per metre of base. THIS was the reason no run ever had a
# starting snowpack: `ne` was written as 0, and SnowStation::initialize builds
# `ne` nodes per layer -- zero elements, zero snow. Every run, including the
# national ones, started from bare ground and the INIT_HS table did nothing.
# ~2 cm elements, like new-snow elements and the sea-ice example's 1.5 cm.
_BASE_ELEM_M = 0.02


def write_sno(p, sno_dir: Path, start_date):
    hs, rho = _init_snow(p["elev"]); nl = len(_BASE_LAYERS); th = hs / nl
    ne = max(1, int(round(th / _BASE_ELEM_M)))
    e95, n95 = p.get("e_lv95", 0.0), p.get("n_lv95", 0.0)

    def layer(T, drho):
        ti = min(0.9, max(0.1, (rho + drho) / 917.0)); tv = 1.0 - ti
        return (f"1900-01-01T00:00 {th:.4f} {T:.2f} {ti:.4f} 0.0000 {tv:.4f} 0.0000 "
                f"0.0000 0.0000 0.0000 {_BASE_RG:.4f} {_BASE_RB:.4f} {_BASE_DD:.4f} "
                f"{_BASE_SP:.4f} {_BASE_MK} 0.000000 {ne} 0.000000 0.000000")
    c = (f"SMET 1.1 ASCII\n[HEADER]\nstation_id   = {p['id']}\nstation_name = va_{p['id']}\n"
         f"latitude     = {p['lat']:.6f}\nlongitude    = {p['lon']:.6f}\naltitude     = {p['elev']:.1f}\n"
         f"easting      = {e95:.0f}\nnorthing     = {n95:.0f}\nnodata       = -999\n"
         f"ProfileDate  = {start_date}T00:00:00\nHS_Last      = {hs:.4f}\n"
         f"SlopeAngle   = {p['slope']:.2f}\nSlopeAzi     = {p['aspect']:.2f}\n"
         f"nSoilLayerData   = 0\nnSnowLayerData   = {nl}\nSoilAlbedo       = 0.20\n"
         f"BareSoil_z0      = 0.020\nCanopyHeight     = 0.00\nCanopyLeafAreaIndex = 0.00\n"
         f"CanopyDirectThroughfall = 1.00\nWindScalingFactor = 1.00\nErosionLevel     = 0\n"
         f"TimeCountDeltaHS = 0.000000\n"
         f"fields = timestamp Layer_Thick T Vol_Frac_I Vol_Frac_W Vol_Frac_V Vol_Frac_S "
         f"Rho_S Conduc_S HeatCapac_S rg rb dd sp mk mass_hoar ne CDot metamo\n[DATA]\n"
         + "".join(layer(T, d) + "\n" for T, d in _BASE_LAYERS))
    (sno_dir / f"{p['id']}.sno").write_text(c)


def write_ini(p, ini_dir: Path, sno_dir: Path, meteo_dir: Path, runs_dir: Path,
              prof_start_days=0.0, step_h=None):
    """prof_start_days = how long into the run to START writing profiles.

    The spin-up is the point of the long run, but none of it needs to be
    *written*: assess_ski_quality() reads one timestamp at a time and never
    looks back, so the early season is dead weight. At PROF_START=0 a
    120-day point costs an 11 MB .pro -- 10.7 GB for the national 978, more
    than a CI runner has. Writing only the target window drops that to a
    few hundred kB per point without changing a single simulated value.
    """
    run_out = runs_dir / p["id"]; run_out.mkdir(parents=True, exist_ok=True)
    # PSUM is re-accumulated over exactly one calculation step -- SNOWPACK
    # warns when the two disagree ("should be re-accumulated over
    # CALCULATION_STEP_LENGTH"), so derive one from the other.
    psum_period = int(CALC_STEP_MIN * 60)
    # How often SNOWPACK WRITES a profile, derived from the export step rather
    # than fixed. It was pinned at 6 h, so asking the exporter for a 3 h step
    # would have filtered 3-hourly over profiles that only existed 6-hourly
    # and quietly produced the same frame count -- the export would have
    # looked finer without being finer.
    prof_between = (step_h / 24.0) if step_h else PROF_DAYS_BETWEEN
    c = f"""[GENERAL]
BUFFER_SIZE = 370
BUFF_BEFORE = 1.5
[INPUT]
COORDSYS = CH1903
TIME_ZONE = 0
METEO = SMET
METEOPATH = {meteo_dir}
METEOFILE1 = {p['id']}.smet
SNOW = SMET
SNOWPATH = {sno_dir}
SNOWFILE1 = {p['id']}
[OUTPUT]
COORDSYS = CH1903
TIME_ZONE = 0
METEOPATH = {run_out}
EXPERIMENT = va
PROF_WRITE = TRUE
PROF_FORMAT = PRO
PROF_START = {prof_start_days:.4f}
PROF_DAYS_BETWEEN = {prof_between:.6f}
PROF_AGE_OR_DATE = AGE
PROF_ID_OR_MK = ID
TS_WRITE = FALSE
SNOW_WRITE = FALSE
[SNOWPACK]
CALCULATION_STEP_LENGTH = {CALC_STEP_MIN}
ATMOSPHERIC_STABILITY = MO_MICHLMAYR
SW_MODE = INCOMING
HEIGHT_OF_WIND_VALUE = 10.0
HEIGHT_OF_METEO_VALUES = 2.0
ROUGHNESS_LENGTH = 0.003
MEAS_TSS = FALSE
ENFORCE_MEASURED_SNOW_HEIGHTS = FALSE
SNP_SOIL = FALSE
SOIL_FLUX = FALSE
GEO_HEAT = 0.06
CANOPY = FALSE
CHANGE_BC = FALSE
[FILTERS]
TA::filter1 = min_max
TA::arg1::min = 233
TA::arg1::max = 320
RH::filter1 = min_max
RH::arg1::min = 0.01
RH::arg1::max = 1.2
PSUM::filter1 = min_max
PSUM::arg1::min = -0.1
PSUM::arg1::max = 100.0
VW::filter1 = min_max
VW::arg1::min = 0.2
VW::arg1::max = 70
ISWR::filter1 = min_max
ISWR::arg1::min = 0
ISWR::arg1::max = 1500
[INTERPOLATIONS1D]
MAX_GAP_SIZE = 86400
PSUM::resample1 = accumulate
PSUM::ARG1::period = {psum_period}
[GENERATORS]
TSG::generator1 = CST
TSG::arg1::value = 273.15
RH::generator1 = CST
RH::arg1::value = 0.7
ILWR::generator1 = ALLSKY_LW
ILWR::arg1::type = Unsworth
ILWR::generator2 = CLEARSKY_LW
ILWR::arg2::type = Dilley
"""
    (ini_dir / f"{p['id']}.ini").write_text(c)


def _run_one(args):
    ini, end = args
    env = dict(os.environ)
    env["DYLD_FALLBACK_LIBRARY_PATH"] = config.SNOWPACK_LIBS + ":" + env.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = config.SNOWPACK_LIBS + ":" + env.get("LD_LIBRARY_PATH", "")
    p = subprocess.run([config.SNOWPACK_BIN, "-c", ini, "-e", end],
                       capture_output=True, text=True, env=env, cwd=os.path.dirname(ini))
    # "Exited 0 having done nothing" is a real failure mode and it used to be
    # invisible: stderr was discarded whenever the return code was 0. In CI
    # that showed up as "SNOWPACK 8/8 (8 ok) in 0.1s" followed by "0 points
    # classified from .pro" -- success reported, no output produced. So treat
    # a missing .pro as a failure in its own right and keep the diagnostics.
    pid = os.path.splitext(os.path.basename(ini))[0]
    run_out = os.path.join(os.path.dirname(os.path.dirname(ini)), "runs", pid)
    pro = glob.glob(os.path.join(run_out, "*.pro"))
    if p.returncode == 0 and not pro:
        tail = ((p.stderr or "").strip() or (p.stdout or "").strip())[-400:]
        return os.path.basename(ini), -1, f"exited 0 but wrote no .pro — {tail}"
    return os.path.basename(ini), p.returncode, (p.stderr[-400:] if p.returncode else "")


def run_points(points, target_date, spinup_days=None, workers=None,
               out_start=None, out_end=None, step_h=None):
    """Prepare + run SNOWPACK for all points. Returns runs_dir with <id>/*.pro.

    out_start/out_end bound the window that gets WRITTEN, and out_end is also
    how far the model is integrated. Both come from the app's timeline (see
    run_variant_a.app_window) so the exported frames span the same hours the
    slider offers. Passing neither keeps the whole season, which is only sane
    for a handful of points.
    """
    from datetime import datetime as _dtm, timedelta as _td
    spinup_days = spinup_days or config.SPINUP_DAYS
    # The spin-up runs up to the START of the output window, not to the target
    # date -- the window now opens days before that date.
    if out_start is not None:
        sim_start = (out_start - _td(days=spinup_days)).date().isoformat()
        prof_start = float(spinup_days)
    else:
        sim_start = _profile_start_iso(target_date, spinup_days)
        prof_start = 0.0
    start = sim_start
    base = config.WORK_DIR
    sno_dir = base / "sno"; ini_dir = base / "ini"; runs_dir = base / "runs"
    meteo_dir = base / "meteo"
    for d in (sno_dir, ini_dir, runs_dir):
        d.mkdir(parents=True, exist_ok=True)
    for p in points:
        write_sno(p, sno_dir, start)
        write_ini(p, ini_dir, sno_dir, meteo_dir, runs_dir, prof_start, step_h)
    inis = [str(ini_dir / f"{p['id']}.ini") for p in points]
    # Integrate to the end of the OUTPUT window. Stopping at the target date
    # is what left the second half of the app's slider with no frames at all.
    end = (out_end.strftime("%Y-%m-%dT%H:%M") if out_end is not None
           else f"{target_date}T00:00")
    workers = workers or (os.cpu_count() or 6)
    ok = 0; fail = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, (i, end)): i for i in inis}
        for k, fu in enumerate(as_completed(futs), 1):
            n, rc, err = fu.result()
            if rc == 0: ok += 1
            else: fail.append((n, err))
            if k % 40 == 0 or k == len(inis):
                print(f"  snowpack {k}/{len(inis)} ({ok} ok)")
    print(f"SNOWPACK {ok}/{len(inis)} in {time.time()-t0:.1f}s")
    for n, e in fail[:5]:
        print("  FAIL", n, e.strip()[:150])
    return runs_dir
