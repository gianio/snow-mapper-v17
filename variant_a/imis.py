"""Measured snow height (SLF IMIS stations) as correction and as a check.

Why the stations and not the SLF snow-height MAP: the map on slf.ch is an
interpolation OF these stations (plus observers), published as images for
people. The station series are the same measurements, machine-readable,
CC BY 4.0, via measurement-api.slf.ch -- and they can be compared with the
model at the station's own height and on the same hours.

Two uses, both per weather point:

* Validation. The flat run of the weather point, interpolated to the
  station's elevation, against the measured HS over the past half of the
  window. Summarised as bias / MAE / RMSE and written into the manifest.

* Correction ("nudging by precipitation"). What precipitation decides is
  how much the snow GROWS when it snows. So the factor learns from the
  growth: the sum of the daily HS gains over the past days, measured vs
  model at the station's height, whenever one of them gained at least
  GAIN_MIN_CM. Without a snowfall there is nothing to learn and the factor
  relaxes towards 1 (LEAK per cycle). It feeds the next state advance and
  forecast; the state itself is never overwritten with a measurement.

  Until 27.11. it learnt from the absolute HS instead. That error sits in
  the carried snowpack and does not go away when the precipitation is
  right, so the factor kept moving every cycle until it hit its bound:
  25 weather points ended at x0.5 and halved the snowfall of 23.-27.11.,
  which ICON had about right (0.95x the gauges).
"""
from __future__ import annotations
import math
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

MATCH_KM = 12.0            # station must be this close to its weather point
MATCH_DZ = 150.0           # ... and inside the bands, give or take this
DAMP_CM = 5.0              # added to both gains: a few cm of growth are noisy
GAIN_MIN_CM = 8.0          # a snowfall worth learning from (measured or model)
GAIN_STEP_H = 24           # HS read once a day: the hourly sensor jitters
STEP_EXP = 0.5             # per-cycle move: factor *= ratio ** STEP_EXP
LEAK = 0.9                 # per cycle without a snowfall: factor **= LEAK (back towards 1)
F_MIN, F_MAX = 0.67, 1.5   # hard bounds on the precipitation factor
R_MIN, R_MAX = 0.7, 1.4    # bounds on one cycle's ratio
FACTOR_KIND = "gain"       # what the stored factors were learnt from
HOLDOUT_MOD = 4            # every 4th station (by a hash of its code) is held
                           # out of the correction, so the drift check
                           # (drift.py) has stations the model was never
                           # nudged towards


def is_holdout(code):
    """Stable across runs and machines (crc32, not Python's salted hash)."""
    return zlib.crc32(str(code).encode()) % HOLDOUT_MOD == 0


def _km(lat1, lon1, lat2, lon2):
    return math.hypot((lon2 - lon1) * 78.0, (lat2 - lat1) * 111.0)


def match_stations(stations, wps):
    """[(station, wp)] for stations close to a weather point, within its bands."""
    out = []
    for s in stations:
        best, bd = None, MATCH_KM
        for w in wps:
            d = _km(s.lat, s.lon, w["lat"], w["lon"])
            if d < bd and w["bands"][0] - MATCH_DZ <= s.elevation <= w["bands"][-1] + MATCH_DZ:
                best, bd = w, d
        if best is not None:
            out.append((s, best))
    return out


def model_hs_at(runs, results, wp_id, elev, frame_idx, i_hs):
    """HS [cm] of the wp's flat runs, linearly interpolated to `elev`."""
    flats = sorted((r["elev"], results[r["id"]][0][frame_idx, i_hs])
                   for r in runs if r["wp"] == wp_id and r["slope"] == 0 and r["id"] in results)
    if not flats:
        return None
    z = np.array([f[0] for f in flats]); h = np.array([f[1] for f in flats])
    return float(np.interp(elev, z, h))


def fetch(period_days=7, workers=8):
    """IMIS stations with their hourly HS series (UTC 'YYYY-MM-DDTHH' -> cm)."""
    from data_connectors import slf_stations as S
    st = S.get_stations()

    def one(s):
        try:
            s.hs = S.fetch_hs_series(s.code, period_days)
        except Exception:
            s.hs = {}
        return s
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [s for s in ex.map(one, st) if s.hs]


def compare(stations, wps, runs, results, layer_ts, mets, now=None, collect=None):
    """Per matched station: model vs measured HS over the past frames.

    Returns (rows, summary). Each row: station, wp, elev, n, bias, mae,
    last_meas, last_model (cm), holdout. `collect` (a dict), when given,
    receives every station's hourly (time, measured, model) pairs for the
    drift history."""
    i_hs = mets.index("total_hs_cm")
    now = now or datetime.utcnow()
    past = [(i, t) for i, t in enumerate(layer_ts) if t <= now]
    rows = []
    for s, w in match_stations(stations, wps):
        pairs, pairs_t = [], []
        for i, t in past:
            meas = s.hs.get(t.strftime("%Y-%m-%dT%H"))
            if meas is None:
                continue
            mod = model_hs_at(runs, results, w["id"], s.elevation, i, i_hs)
            if mod is None:
                continue
            pairs.append((meas, mod))
            pairs_t.append((t, meas, mod))
        if collect is not None and pairs:
            collect[s.code] = {"label": s.label, "elev": round(s.elevation), "wp": w["id"],
                               "tile": w.get("tile"), "holdout": is_holdout(s.code),
                               "pairs": pairs_t}
        if len(pairs) < 3:
            continue
        m = np.array(pairs)
        err = m[:, 1] - m[:, 0]
        g_meas, g_mod = gains(pairs_t)
        rows.append({"station": s.code, "label": s.label, "wp": w["id"],
                     "elev": round(s.elevation), "n": len(pairs),
                     "bias": round(float(err.mean()), 1),
                     "mae": round(float(np.abs(err).mean()), 1),
                     "last_meas": round(float(m[-1, 0]), 1),
                     "last_model": round(float(m[-1, 1]), 1),
                     "gain_meas": round(g_meas, 1), "gain_model": round(g_mod, 1),
                     "holdout": is_holdout(s.code)})
    if not rows:
        return rows, {"stations": 0}
    b = np.array([r["bias"] for r in rows]); a = np.array([r["mae"] for r in rows])
    lm = np.array([r["last_meas"] for r in rows])
    summary = {"stations": len(rows), "bias_cm": round(float(b.mean()), 1),
               "mae_cm": round(float(a.mean()), 1),
               "rmse_cm": round(float(np.sqrt((b ** 2).mean())), 1),
               "median_measured_cm": round(float(np.median(lm)), 1)}
    return rows, summary


def gains(pairs_t, step_h=GAIN_STEP_H):
    """(measured, model) HS growth [cm]: the positive day-to-day changes of
    the series read every `step_h` hours back from its last hour, summed.
    Settling and melt between snowfalls do not count against it."""
    if len(pairs_t) < 2:
        return 0.0, 0.0
    pts = sorted(pairs_t, key=lambda p: p[0])
    by_t = {p[0]: p for p in pts}
    t_last = pts[-1][0]
    from datetime import timedelta
    seq, t = [], t_last
    while t >= pts[0][0]:
        p = by_t.get(t)
        if p is None:                    # the nearest hour within 3 h
            near = min(pts, key=lambda q: abs((q[0] - t).total_seconds()))
            p = near if abs((near[0] - t).total_seconds()) <= 3 * 3600 else None
        if p is not None:
            seq.append(p)
        t -= timedelta(hours=step_h)
    seq.reverse()
    gm = sum(max(0.0, b[1] - a[1]) for a, b in zip(seq, seq[1:]))
    gd = sum(max(0.0, b[2] - a[2]) for a, b in zip(seq, seq[1:]))
    return float(gm), float(gd)


def update_factors(rows, old=None):
    """New {wp: precipitation factor} from the comparison rows: learnt from
    the snowfall growth (gains), relaxed towards 1 without a snowfall."""
    old = dict(old or {})
    by = {}
    for r in rows:
        if r.get("holdout"):
            continue
        gm, gd = r.get("gain_meas"), r.get("gain_model")
        if gm is None or gd is None or max(gm, gd) < GAIN_MIN_CM:
            continue
        ratio = (gm + DAMP_CM) / (gd + DAMP_CM)
        by.setdefault(r["wp"], []).append(min(R_MAX, max(R_MIN, ratio)))
    new = {}
    for wp in set(old) | set(by):
        f = old.get(wp, 1.0) ** LEAK
        rs = by.get(wp)
        if rs:
            g = math.exp(sum(math.log(x) for x in rs) / len(rs))     # geometric mean
            f *= g ** STEP_EXP
        f = round(min(F_MAX, max(F_MIN, f)), 3)
        if abs(f - 1.0) >= 0.005:
            new[wp] = f
    return new
