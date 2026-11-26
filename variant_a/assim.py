"""Light assimilation of IMIS snow heights into what the app shows.

The carried SNOWPACK state is left alone (the drift check keeps judging the
raw model). What is *exported* is corrected: where nearby IMIS stations
(not the held-out ones) measure less snow than the model has at their
height, that excess is taken off every frame of the runs of that weather
point, faded out with the height difference. New snow in the forecast
still lands on top -- only the snow the stations do not see is removed.

Why: in autumn the model kept 10-30 cm where the stations had ~1 cm
(Nov 2026: bias +10..13 cm with 1 cm measured). That phantom base is what
made the map show "Pulver" on bare slopes.

Also `mask_band_outliers`: a single slope class at one height band with far
more snow than the bands right above and below (e.g. 104 cm between 6 and
26 cm) is a broken run, not weather. Its ok flag is dropped so the app
interpolates from the neighbours instead of drawing the spike.
"""
from __future__ import annotations

import numpy as np

MIN_EXCESS_CM = 3.0     # below this the station and the model agree
ORPHAN_KM = 25.0        # weather points without a station of their own: stations this close
ORPHAN_DZ = 300.0       # ... whose height lies in (or this close to) the point's bands
ORPHAN_MIN_F = 0.5      # never take off more than half the snow
FADE_M = 600.0          # station influence fades to zero over this height distance
DEPTH_METS = ("total_hs_cm", "powder_depth_cm", "soft_top_cm", "weak_layer_depth_cm", "mf_base_cm")


def offsets(rows):
    """{wp: [(station elevation, excess cm)]} from imis.compare rows."""
    out = {}
    for r in rows or []:
        if r.get("holdout"):
            continue
        ex = float(r.get("last_model", 0)) - float(r.get("last_meas", 0))
        if ex >= MIN_EXCESS_CM:
            out.setdefault(r["wp"], []).append((float(r["elev"]), ex))
    return out


def excess_at(stations, elev):
    """Weighted excess [cm] for a run at `elev` from its weather point's stations."""
    num = den = 0.0
    for ze, ex in stations:
        w = max(0.0, 1.0 - abs(elev - ze) / FADE_M)
        num += w * ex
        den += w
    if den <= 0:
        return 0.0
    # a single far station counts only partly; full weight from w = 1
    return num / den * min(1.0, den)


def apply(rows, runs, results, mets):
    """Remove the station-confirmed excess in place. Returns a summary."""
    offs = offsets(rows)
    if not offs:
        return {"used": False, "runs": 0}
    idx = {m: mets.index(m) for m in DEPTH_METS if m in mets}
    ih = idx.get("total_hs_cm")
    if ih is None:
        return {"used": False, "runs": 0}
    n, tot = 0, 0.0
    for r in runs:
        st = offs.get(r["wp"])
        if not st or r["id"] not in results:
            continue
        d = excess_at(st, float(r["elev"]))
        if d < 0.5:
            continue
        arr = results[r["id"]][0]
        hs_new = np.maximum(0.0, arr[:, ih] - d)
        for m, i in idx.items():
            if m == "total_hs_cm":
                continue
            arr[:, i] = np.minimum(arr[:, i], hs_new)
        arr[:, ih] = hs_new
        n += 1
        tot += d
    return {"used": True, "runs": n, "weather_points": len(offs),
            "mean_cm": round(tot / max(1, n), 1)}


def mask_band_outliers(runs, results, mets, frame=-1, min_excess=15.0, ratio=1.6):
    """Ids of runs whose HS sticks far out above both neighbouring bands."""
    ih = mets.index("total_hs_cm")
    col = {}
    for r in runs:
        if r["id"] in results:
            key = (r["wp"], r.get("slope_c", r.get("slope")), r.get("aspect_k", r.get("aspect")))
            col.setdefault(key, []).append((float(r["elev"]), r["id"]))
    bad = set()
    for key, lst in col.items():
        lst.sort()
        hs = [float(results[i][0][frame, ih]) for _, i in lst]
        for k in range(1, len(lst) - 1):
            nb = max(hs[k - 1], hs[k + 1])
            if hs[k] > nb + min_excess and hs[k] > ratio * nb:
                bad.add(lst[k][1])
    return bad


def _km(la1, lo1, la2, lo2):
    return float(np.hypot((la1 - la2) * 111.2, (lo1 - lo2) * 111.2 * np.cos(np.radians((la1 + la2) / 2))))


def orphans(stations, wps, rows, runs, results, mets, layer_ts, now=None):
    """Weather points with no IMIS station of their own, checked against the
    stations around them.

    Nov 2026: the Säntis point (w000) carried 100-150 cm at 1800-2100 m, twice
    its neighbours, while every station within 30 km measured 36-67 cm -- it
    has no station of its own, so neither the excess removal above nor the
    precipitation factor ever touched it. Here such a point is read at the
    heights of the stations within ORPHAN_KM; where it has clearly more snow
    than they measure, all its runs are scaled by measured / model (distance
    weighted, never below ORPHAN_MIN_F). Only downward, like apply()."""
    from datetime import datetime
    from variant_a.imis import model_hs_at, is_holdout
    if not stations:
        return {"used": False, "weather_points": 0}
    now = now or datetime.utcnow()
    past = [i for i, t in enumerate(layer_ts) if t <= now]
    if not past:
        return {"used": False, "weather_points": 0}
    fi = past[-1]
    tkey = layer_ts[fi].strftime("%Y-%m-%dT%H")
    ih = mets.index("total_hs_cm")
    idx = [mets.index(m) for m in DEPTH_METS if m in mets]
    own = {r["wp"] for r in rows or []}
    by_wp = {}
    for r in runs:
        by_wp.setdefault(r["wp"], []).append(r)
    done = {}
    for w in wps:
        if w["id"] in own:
            continue
        num = den = wmod = wmeas = 0.0
        used = 0
        for s in stations:
            # held-out stations stay independent: they judge the model, never correct it
            if is_holdout(getattr(s, "code", "")):
                continue
            meas = s.hs.get(tkey) if getattr(s, "hs", None) else None
            if meas is None:
                continue
            d = _km(s.lat, s.lon, w["lat"], w["lon"])
            if d > ORPHAN_KM or not (w["bands"][0] - ORPHAN_DZ <= s.elevation <= w["bands"][-1] + ORPHAN_DZ):
                continue
            mod = model_hs_at(runs, results, w["id"], s.elevation, fi, ih)
            if mod is None:
                continue
            k = 1.0 / max(d, 3.0)
            wmod += k * mod
            wmeas += k * float(meas)
            den += k
            used += 1
        if not used or den <= 0:
            continue
        excess = (wmod - wmeas) / den
        if excess < 2 * MIN_EXCESS_CM or wmod <= 0:
            continue
        f = max(ORPHAN_MIN_F, min(1.0, wmeas / wmod))
        if f > 0.95:
            continue
        for r in by_wp.get(w["id"], []):
            if r["id"] not in results:
                continue
            arr = results[r["id"]][0]
            for i in idx:
                arr[:, i] = arr[:, i] * f
        done[w["id"]] = {"factor": round(f, 2), "stations": used, "excess_cm": round(excess, 1)}
    return {"used": bool(done), "weather_points": len(done), "points": done}

