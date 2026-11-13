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
