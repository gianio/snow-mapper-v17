"""Per-point snow-profile data for the app's clickable profile viewer.

Resamples each representative point's SNOWPACK .pro to fixed depth bins (density +
grain type) per timestamp. For a NATIONAL product we export per-point profiles + point
coordinates (elevation/aspect) — small and portable — and the app interpolates a
profile at a clicked location client-side (KNN in x/y/elev/aspect). Ported from
sandbox 60/62 (which precomputed per-cell weights; impractical to ship nationally).
"""
from __future__ import annotations
import glob, os
import numpy as np
from . import classify

NB = 28  # depth bins (0 = surface, 1 = ground)

# grain F1 -> (label, rgb) for the app legend
GRAIN = {1: ("PP", (168, 216, 240)), 2: ("DF", (150, 220, 150)), 3: ("RG", (116, 196, 118)),
         4: ("FC", (246, 215, 75)), 5: ("DH", (253, 141, 60)), 6: ("SH", (227, 119, 194)),
         7: ("MF", (148, 103, 189)), 8: ("IF", (99, 99, 99)), 9: ("FCxr", (200, 150, 60)),
         0: ("-", (220, 220, 220))}


def resample(ts):
    n = int(ts.get("n", 0))
    if n == 0:
        return None
    hh = np.asarray(ts["heights"][:n], float); HS = float(hh[-1])
    if HS < 1:
        return None
    dens = np.asarray(ts.get("density", np.zeros(n))[:n], float)
    grain = (np.asarray(ts.get("grain", np.zeros(n))[:n], float) // 100).astype(int)
    ncm = max(2, int(round(HS))); dcm = np.zeros(ncm); gcm = np.zeros(ncm, int)
    for i in range(n):
        bot = HS - (hh[i - 1] if i > 0 else 0.0); top = HS - hh[i]
        a = int(max(0, round(top))); b = int(min(ncm, round(bot)))
        if b <= a:
            b = min(ncm, a + 1)
        dcm[a:b] = dens[i]; gcm[a:b] = grain[i]
    edges = np.linspace(0, ncm, NB + 1).astype(int); db = []; gb = []
    for k in range(NB):
        lo, hi = edges[k], max(edges[k] + 1, edges[k + 1])
        db.append(round(float(dcm[lo:hi].mean()), 0))
        v = gcm[lo:hi]; gb.append(int(np.bincount(v).argmax()) if len(v) else 0)
    return db, gb, round(HS, 0)


def build_payload(points, runs_dir, timestamps):
    """Return an app-portable profile payload (per-point profiles per timestamp)."""
    def find_pro(pid):
        g = glob.glob(os.path.join(runs_dir, pid, "*.pro"))
        return g[0] if g else None
    kept = []
    raw = {}
    for p in points:
        pro = p.get("pro") or find_pro(p["id"])
        if not pro or not os.path.exists(pro):
            continue
        ts = classify.parse_pro(pro)
        if ts:
            raw[p["id"]] = {t["dt"]: t for t in ts}; kept.append(p)
    prof = []
    for dt in timestamps:
        step = []
        for p in kept:
            av = raw[p["id"]]
            t = av.get(dt) or av[min(av, key=lambda x: abs((x - dt).total_seconds()))]
            r = resample(t)
            step.append({"hs": 0, "db": [0] * NB, "gb": [0] * NB} if r is None
                        else {"hs": r[2], "db": r[0], "gb": r[1]})
        prof.append(step)
    return {
        "nb": NB,
        "grain": {k: [v[0], list(v[1])] for k, v in GRAIN.items()},
        "labels": [dt.strftime("%Y-%m-%dT%H:%M") for dt in timestamps],
        "points": [{"id": p["id"], "lat": p["lat"], "lon": p["lon"], "elev": p["elev"],
                    "aspect": p["aspect"], "slope": p["slope"], "tile": p["tile"]} for p in kept],
        "profiles": prof,
    }
