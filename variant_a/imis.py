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

* Correction ("nudging by precipitation"). Snow height is, to first order,
  precipitation. If a weather point's flat run keeps ending up with less
  snow than its stations measure, its precipitation is scaled up for the
  next cycle, and vice versa. The factor moves slowly (square root of the
  ratio per cycle, clipped), so one odd station or one bad day does not
  swing it. It feeds the next state advance and forecast; the state itself
  is never overwritten with a measurement.
"""
from __future__ import annotations
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

MATCH_KM = 12.0            # station must be this close to its weather point
MATCH_DZ = 150.0           # ... and inside the bands, give or take this
DAMP_CM = 20.0             # added to both sides of the ratio: small HS is noisy
STEP_EXP = 0.5             # per-cycle move: factor *= ratio ** STEP_EXP
F_MIN, F_MAX = 0.5, 2.0    # hard bounds on the precipitation factor
R_MIN, R_MAX = 0.7, 1.4    # bounds on one cycle's ratio


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


def compare(stations, wps, runs, results, layer_ts, mets, now=None):
    """Per matched station: model vs measured HS over the past frames.

    Returns (rows, summary). Each row: station, wp, elev, n, bias, mae,
    last_meas, last_model (cm)."""
    i_hs = mets.index("total_hs_cm")
    now = now or datetime.utcnow()
    past = [(i, t) for i, t in enumerate(layer_ts) if t <= now]
    rows = []
    for s, w in match_stations(stations, wps):
        pairs = []
        for i, t in past:
            meas = s.hs.get(t.strftime("%Y-%m-%dT%H"))
            if meas is None:
                continue
            mod = model_hs_at(runs, results, w["id"], s.elevation, i, i_hs)
            if mod is None:
                continue
            pairs.append((meas, mod))
        if len(pairs) < 3:
            continue
        m = np.array(pairs)
        err = m[:, 1] - m[:, 0]
        rows.append({"station": s.code, "label": s.label, "wp": w["id"],
                     "elev": round(s.elevation), "n": len(pairs),
                     "bias": round(float(err.mean()), 1),
                     "mae": round(float(np.abs(err).mean()), 1),
                     "last_meas": round(float(m[-1, 0]), 1),
                     "last_model": round(float(m[-1, 1]), 1)})
    if not rows:
        return rows, {"stations": 0}
    b = np.array([r["bias"] for r in rows]); a = np.array([r["mae"] for r in rows])
    lm = np.array([r["last_meas"] for r in rows])
    summary = {"stations": len(rows), "bias_cm": round(float(b.mean()), 1),
               "mae_cm": round(float(a.mean()), 1),
               "rmse_cm": round(float(np.sqrt((b ** 2).mean())), 1),
               "median_measured_cm": round(float(np.median(lm)), 1)}
    return rows, summary


def update_factors(rows, old=None):
    """New {wp: precipitation factor} from the comparison rows."""
    old = dict(old or {})
    by = {}
    for r in rows:
        ratio = (r["last_meas"] + DAMP_CM) / (r["last_model"] + DAMP_CM)
        by.setdefault(r["wp"], []).append(min(R_MAX, max(R_MIN, ratio)))
    new = dict(old)
    for wp, rs in by.items():
        g = math.exp(sum(math.log(x) for x in rs) / len(rs))     # geometric mean
        f = old.get(wp, 1.0) * g ** STEP_EXP
        new[wp] = round(min(F_MAX, max(F_MIN, f)), 3)
    return new
