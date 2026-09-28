"""Wind drift and scour on the virtual slopes -- without a snow-transport model.

SNOWPACK runs each virtual slope as a 1-D column and does not move snow
sideways, and Alpine3D-style redistribution is out of scope on purpose. But
the two wind effects a skier actually meets are mostly geometric:

* lee slopes collect wind-deposited snow ("Triebschnee"): soft to firm,
  pressed, often deeper than the new snow alone would give;
* windward slopes and ridges are scoured or wind-pressed ("verblasen").

Both need the same three things, which the forcing already has: how much
snow the wind could move in the last day (transport ~ (U - U_t)^3 above a
threshold), which way it blew, and whether loose snow was there to move. The
slope direction then decides which of the two a virtual slope gets. The
result is two 0..1 indices per run and frame, gridded like every other metric.
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta

import numpy as np

U_T = 5.0            # m/s  -- transport threshold for fresh, loose snow (10 m wind)
T_HALF = 24 * 3.0 ** 3   # transport that maps to index 0.5: a day at U_t + 3 m/s
WINDOW_H = 24        # hours of wind that count for the current surface
LOOSE_POWDER_CM = 2.0    # loose snow available when at least this much powder
LOOSE_DENSITY = 150.0    # ... or the surface is still this light
FLAT_EXPOSURE = 0.6      # flat runs: no lee, some scour (open terrain)


def transport_series(hourly, frames, window_h=WINDOW_H, ut=U_T):
    """Per frame: (transport index 0..1, transport-weighted FROM-direction deg).

    `hourly` is an Open-Meteo style block (time, wind_speed_10m,
    wind_direction_10m); `frames` are naive UTC datetimes.
    """
    t = [datetime.strptime(s[:16], "%Y-%m-%dT%H:%M") for s in hourly.get("time", [])]
    u = np.array([v if v is not None else 0.0 for v in hourly.get("wind_speed_10m", [])], float)
    d = np.array([v if v is not None else 0.0 for v in hourly.get("wind_direction_10m", [])], float)
    n = min(len(t), len(u), len(d))
    t, u, d = t[:n], u[:n], d[:n]
    q = np.maximum(u - ut, 0.0) ** 3                 # transport rate proxy
    qs, qc = q * np.sin(np.radians(d)), q * np.cos(np.radians(d))
    idx = np.zeros(len(frames)); dirn = np.zeros(len(frames))
    if not n:
        return idx, dirn
    tt = np.array([x.timestamp() for x in t])
    for i, f in enumerate(frames):
        f_ts = f.timestamp()
        m = (tt > f_ts - window_h * 3600) & (tt <= f_ts)
        T = float(q[m].sum())
        idx[i] = T / (T + T_HALF)
        dirn[i] = (math.degrees(math.atan2(qs[m].sum(), qc[m].sum())) + 360.0) % 360.0 if T > 0 else 0.0
    return idx, dirn


def slope_factors(slope, aspect, from_dir):
    """(lee, windward) exposure 0..1 of a slope to wind FROM `from_dir`.

    A slope facing the direction the wind blows TO (from_dir + 180) is lee.
    Steeper slopes carry more of it, flat runs get no lee and moderate scour.
    """
    if slope <= 0:
        return 0.0, FLAT_EXPOSURE
    s = min(1.0, slope / 30.0)
    lee = max(0.0, math.cos(math.radians(aspect - (from_dir + 180.0))))
    wwd = max(0.0, math.cos(math.radians(aspect - from_dir)))
    return lee * s, wwd * s


def apply(runs, results, frames, wp_hourly, mets):
    """Fill the drift_load / wind_scour columns of every run's metric block.

    `results[run_id][0]` is the (frames, METS) float32 block from _digest.
    Availability of loose snow is read from that same block (powder depth and
    surface density at the frame), so an old, settled surface is not drifted.
    """
    i_pow, i_rho = mets.index("powder_depth_cm"), mets.index("surface_density")
    i_hs = mets.index("total_hs_cm")
    i_d, i_s = mets.index("drift_load"), mets.index("wind_scour")
    cache = {}
    for r in runs:
        res = results.get(r["id"])
        if res is None:
            continue
        h = wp_hourly.get(r["wp"])
        if not h:
            continue
        if r["wp"] not in cache:
            cache[r["wp"]] = transport_series(h, frames)
        idx, dirn = cache[r["wp"]]
        met = res[0]
        for f in range(len(frames)):
            if met[f, i_hs] < 1.0:
                continue
            loose = met[f, i_pow] >= LOOSE_POWDER_CM or 0 < met[f, i_rho] < LOOSE_DENSITY
            avail = 1.0 if loose else 0.3
            lee, wwd = slope_factors(r["slope"], r["aspect"], dirn[f])
            met[f, i_d] = idx[f] * lee * avail
            met[f, i_s] = idx[f] * wwd * avail
