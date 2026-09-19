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


def _init_snow(elev):
    hs = max(float(np.interp(elev, config.INIT_ELEV, config.INIT_HS)), 0.05)
    rho = float(np.interp(elev, config.INIT_ELEV, config.INIT_RHO))
    return hs, rho


def _profile_start_iso(target_date, spinup_days):
    from datetime import datetime, timedelta
    d = datetime.strptime(target_date, "%Y-%m-%d").date() - timedelta(days=spinup_days)
    return d.isoformat()


def write_sno(p, sno_dir: Path, start_date):
    hs, rho = _init_snow(p["elev"]); nl = 3; th = hs / nl; ti = rho / 917.0; tv = 1.0 - ti
    e95, n95 = p.get("e_lv95", 0.0), p.get("n_lv95", 0.0)
    layer = (lambda T: f"1900-01-01T00:00 {th:.4f} {T:.2f} {ti:.4f} 0.0000 {tv:.4f} 0.0000 "
             f"0.0000 0.0000 0.0000 0.1500 0.1000 0.5000 0.5000 7 0.000000 0 0.000000 0.000000")
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
         f"{layer(266.15)}\n{layer(267.15)}\n{layer(268.15)}\n")
    (sno_dir / f"{p['id']}.sno").write_text(c)


def write_ini(p, ini_dir: Path, sno_dir: Path, meteo_dir: Path, runs_dir: Path):
    run_out = runs_dir / p["id"]; run_out.mkdir(parents=True, exist_ok=True)
    c = f"""[GENERAL]
BUFFER_SIZE = 370
BUFF_BEFORE = 1.5
[INPUT]
COORDSYS = CH1903
TIME_ZONE = 0
METEO = SMET
METEOPATH = {meteo_dir}
STATION1 = {p['id']}
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
PROF_START = 0.0
PROF_DAYS_BETWEEN = 0.25
PROF_AGE_OR_DATE = AGE
PROF_ID_OR_MK = ID
TS_WRITE = FALSE
SNOW_WRITE = FALSE
[SNOWPACK]
CALCULATION_STEP_LENGTH = 30
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
PSUM::ARG1::period = 3600
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
    return os.path.basename(ini), p.returncode, (p.stderr[-200:] if p.returncode else "")


def run_points(points, target_date, spinup_days=None, workers=None):
    """Prepare + run SNOWPACK for all points. Returns runs_dir with <id>/*.pro."""
    spinup_days = spinup_days or config.SPINUP_DAYS
    start = _profile_start_iso(target_date, spinup_days)
    base = config.WORK_DIR
    sno_dir = base / "sno"; ini_dir = base / "ini"; runs_dir = base / "runs"
    meteo_dir = base / "meteo"
    for d in (sno_dir, ini_dir, runs_dir):
        d.mkdir(parents=True, exist_ok=True)
    for p in points:
        write_sno(p, sno_dir, start); write_ini(p, ini_dir, sno_dir, meteo_dir, runs_dir)
    inis = [str(ini_dir / f"{p['id']}.ini") for p in points]
    end = f"{target_date}T00:00"
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
