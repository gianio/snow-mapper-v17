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


LOAD_TAU_H = 36.0        # drift deposits fade with this e-folding time (half-life ~25 h)
LOAD_WINDOW_H = 96       # hours of wind that can still have loaded a lee slope
CREST = 0.6              # crest wind as a share of the free-air wind at that height
PL_LEVELS = (850, 700, 500)


def free_air(hourly, i, z):
    """(speed m/s, FROM-direction deg) of the free-air wind at height z (m)
    in hour i, interpolated between the pressure levels by geopotential
    height (u/v components, so directions average properly). None when the
    hour has fewer than two levels."""
    nodes = []
    for p in PL_LEVELS:
        g = hourly.get(f"geopotential_height_{p}hPa") or []
        u = hourly.get(f"wind_speed_{p}hPa") or []
        d = hourly.get(f"wind_direction_{p}hPa") or []
        if i < len(g) and i < len(u) and i < len(d) and None not in (g[i], u[i], d[i]):
            r = math.radians(float(d[i]))
            nodes.append((float(g[i]), float(u[i]) * math.sin(r), float(u[i]) * math.cos(r)))
    if len(nodes) < 2:
        return None
    nodes.sort()
    z = min(max(z, nodes[0][0]), nodes[-1][0])           # no extrapolation
    k = 0
    while k < len(nodes) - 2 and z > nodes[k + 1][0]:
        k += 1
    (z0, a0, b0), (z1, a1, b1) = nodes[k], nodes[k + 1]
    w = (z - z0) / max(1.0, z1 - z0)
    a, b = a0 + (a1 - a0) * w, b0 + (b1 - b0) * w
    return math.hypot(a, b), (math.degrees(math.atan2(a, b)) + 360.0) % 360.0


def wind_at(hourly, z):
    """Hourly (speed, FROM-direction) a slope at height z feels: the weather
    point's 10 m wind (often a valley or a smoothed model cell), or -- when
    stronger -- the crest share of the free-air wind at the slope's height,
    with that direction. Without a profile it is the 10 m wind."""
    u = [v if v is not None else 0.0 for v in hourly.get("wind_speed_10m", [])]
    d = [v if v is not None else 0.0 for v in hourly.get("wind_direction_10m", [])]
    n = min(len(u), len(d))
    us, ds = np.array(u[:n], float), np.array(d[:n], float)
    if z is None:
        return us, ds
    for i in range(n):
        fa = free_air(hourly, i, z)
        if fa and CREST * fa[0] > us[i]:
            us[i], ds[i] = CREST * fa[0], fa[1]
    return us, ds


def load_series(hourly, frames, z=None, ut=U_T):
    """Per frame: (transport now 0..1, drift load 0..1, load FROM-direction).

    Transport is the last WINDOW_H hours, as transport_series. The load adds
    up the transport of the last LOAD_WINDOW_H hours, each hour weighted by
    exp(-age / LOAD_TAU_H): a lee slope loaded by yesterday's storm is still
    loaded today, a little less, and calm air does not reset it.
    """
    t = [datetime.strptime(s[:16], "%Y-%m-%dT%H:%M") for s in hourly.get("time", [])]
    u, d = wind_at(hourly, z)
    n = min(len(t), len(u))
    t, u, d = t[:n], u[:n], d[:n]
    q = np.maximum(u - ut, 0.0) ** 3
    qs, qc = q * np.sin(np.radians(d)), q * np.cos(np.radians(d))
    now = np.zeros(len(frames)); load = np.zeros(len(frames)); dirn = np.zeros(len(frames))
    if not n:
        return now, load, dirn
    tt = np.array([x.timestamp() for x in t])
    for i, f in enumerate(frames):
        f_ts = f.timestamp()
        m = (tt > f_ts - WINDOW_H * 3600) & (tt <= f_ts)
        T = float(q[m].sum())
        now[i] = T / (T + T_HALF)
        ml = (tt > f_ts - LOAD_WINDOW_H * 3600) & (tt <= f_ts)
        w = np.exp(-(f_ts - tt[ml]) / 3600.0 / LOAD_TAU_H)
        L = float((q[ml] * w).sum())
        load[i] = L / (L + T_HALF)
        sa, ca = float((qs[ml] * w).sum()), float((qc[ml] * w).sum())
        dirn[i] = (math.degrees(math.atan2(sa, ca)) + 360.0) % 360.0 if L > 0 else 0.0
    return now, load, dirn


def apply(runs, results, frames, wp_hourly, mets):
    """Fill the drift_load / wind_scour columns of every run's metric block.

    `results[run_id][0]` is the (frames, METS) float32 block from _digest.
    The wind is the one at the run's own height (wind_at). Loose snow to move
    counts when it lay at the frame or at any frame of the day before it (the
    snow that fell during the storm is what it moved), so an old, settled
    surface is not drifted. Drift deposits build over days (load_series);
    scour is the wind blowing now.
    """
    i_pow, i_rho = mets.index("powder_depth_cm"), mets.index("surface_density")
    i_hs = mets.index("total_hs_cm")
    i_d, i_s = mets.index("drift_load"), mets.index("wind_scour")
    fts = [f.timestamp() for f in frames]
    cache = {}
    for r in runs:
        res = results.get(r["id"])
        if res is None:
            continue
        h = wp_hourly.get(r["wp"])
        if not h:
            continue
        z = r.get("elev")
        key = (r["wp"], None if z is None else round(float(z)))
        if key not in cache:
            cache[key] = load_series(h, frames, z)
        now, load, dirn = cache[key]
        met = res[0]
        for f in range(len(frames)):
            if met[f, i_hs] < 1.0:
                continue
            loose = False
            for g in range(f, -1, -1):
                if fts[f] - fts[g] > WINDOW_H * 3600:
                    break
                if met[g, i_pow] >= LOOSE_POWDER_CM or 0 < met[g, i_rho] < LOOSE_DENSITY:
                    loose = True
                    break
            avail = 1.0 if loose else 0.3
            lee, wwd = slope_factors(r["slope"], r["aspect"], dirn[f])
            met[f, i_d] = load[f] * lee * avail
            met[f, i_s] = now[f] * wwd * avail
