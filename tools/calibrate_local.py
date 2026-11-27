#!/usr/bin/env python3
"""Snowmapper: the new-snow model against past winters, on your own computer.

What it does
------------
1. Measurements of the past (both CC BY 4.0, name the source):
     * MeteoSwiss manual network (~270 boards): new snow hns000d0 and the
       gauge's precipitation rre150d0, 06 -> 06 UTC (data.geo.admin.ch);
     * SLF study plots (~100): HN_1D and HNW_1D -- the new snow AND its water
       (measurement-data.slf.ch). HNW gives the board density directly,
       independent of any error in the weather model's precipitation.
2. The weather model's own hours at every station (Open-Meteo historical
   forecast API, MeteoSwiss ICON-CH1 by default): precipitation,
   temperature, humidity, wind. Cached on disk -- a second run is offline.
3. The app's new-snow model, with the repository's own code
   (model/factors.py): rain/snow on the wet bulb, density
   rho = a + b*T + c*sqrt(U), 24 h settling of the board.
4. Calibration of those physical parameters, leave-one-month-out (a month is
   never judged with parameters fitted on it).
5. Optionally (scikit-learn) a machine-learning model on the same features,
   cross-validated the same way -- to see how much a physics-based model
   leaves on the table, and which inputs carry the rest.
6. A report (report.md), the station-days (rows.csv) and the suggested
   parameters (params_suggestion.yaml) in --out.

Install and run (Python 3.10+):

    pip install numpy requests pyyaml scipy scikit-learn matplotlib
    python tools/calibrate_local.py --start 2025-12-01 --end 2026-03-31 --out calib
    python tools/calibrate_local.py --selftest          # no network, synthetic data

Several winters: --start 2023-12-01 --end 2024-03-31 and so on (ICON-CH1
hours exist from early 2024 on; --model best_match or icon_d2 for older).
Nothing is uploaded anywhere; the only requests are the public data above.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = {"User-Agent": "snowmapper-calibration/1.0"}
MSW = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-nime"
SLF_API = "https://measurement-api.slf.ch/public/api/study-plot"
SLF_ARCH = "https://measurement-data.slf.ch/study-plot"
OM_HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OM_VARS = ["temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m"]
BANDS = [(0, 800), (800, 1200), (1200, 1600), (1600, 2000), (2000, 4500)]
# the app's current parameters (config/model_params.yaml); read from there when
# the repository is at hand
DEFAULT = {"rho_a": 80.0, "rho_b": 8.0, "rho_c": 15.0, "rho_min": 50.0, "rho_max": 220.0,
           "tw_c": 1.0, "tw_k": 2.0, "settle": 0.884}


# ── http with a disk cache ───────────────────────────────────────────────────
class Cache:
    def __init__(self, d: Path, offline=False):
        self.d = Path(d); self.d.mkdir(parents=True, exist_ok=True); self.offline = offline

    def get(self, url, params=None, tries=6, binary=False):
        key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
        f = self.d / f"{key}.gz"
        if f.exists():
            b = gzip.decompress(f.read_bytes())
            return b if binary else b.decode("utf-8", "replace")
        if self.offline:
            return None
        import requests
        wait = 5.0
        for k in range(tries):
            try:
                r = requests.get(url, params=params, headers=UA, timeout=90)
            except requests.RequestException as e:
                print(f"    network: {type(e).__name__}, again in {wait:.0f} s"); time.sleep(wait); wait *= 2; continue
            if r.status_code == 200:
                f.write_bytes(gzip.compress(r.content))
                return r.content if binary else r.content.decode("utf-8", "replace")
            if r.status_code in (404, 400, 422):
                return None
            ra = r.headers.get("Retry-After")
            w = float(ra) if ra and ra.isdigit() else wait
            print(f"    HTTP {r.status_code} from {url.split('/')[2]}, again in {w:.0f} s")
            time.sleep(min(w, 600)); wait = min(wait * 2, 600)
        return None


# ── measurements ────────────────────────────────────────────────────────────
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def msw_stations(c: Cache):
    t = c.get(MSW + "/ogd-nime_meta_stations.csv", binary=True)
    out = {}
    if not t:
        return out
    for x in csv.DictReader(io.StringIO(t.decode("latin-1")), delimiter=";"):
        la, lo, z = (_num(x.get(k)) for k in ("station_coordinates_wgs84_lat",
                                               "station_coordinates_wgs84_lon", "station_height_masl"))
        if None not in (la, lo, z):
            out[x["station_abbr"]] = {"lat": la, "lon": lo, "z": z, "name": x.get("station_name", "")}
    return out


def msw_obs(c: Cache, st, d0: date, d1: date):
    """[{src, code, day, hn, rr}] -- day = end of the 06-06 UTC window."""
    rows = []
    # MeteoSwiss: "recent" = 1 January of this year to yesterday, "historical" = before
    y0 = date(date.today().year, 1, 1)
    for i, code in enumerate(sorted(st)):
        kinds = (["historical"] if d0 < y0 + timedelta(days=1) else []) + (["recent"] if d1 >= y0 else [])
        seen = set()
        for kind in kinds:
            t = c.get(f"{MSW}/{code.lower()}/ogd-nime_{code.lower()}_d_{kind}.csv", binary=True)
            if not t:
                continue
            for x in csv.DictReader(io.StringIO(t.decode("latin-1")), delimiter=";"):
                try:
                    dd = datetime.strptime(x["reference_timestamp"][:10], "%d.%m.%Y").date() + timedelta(days=1)
                except (KeyError, ValueError):
                    continue
                if not (d0 <= dd <= d1) or dd in seen:
                    continue
                hn, rr = _num(x.get("hns000d0")), _num(x.get("rre150d0"))
                if hn is None:
                    continue
                seen.add(dd)
                rows.append({"src": "msw", "code": code, "day": dd.isoformat(), "hn": hn, "rr": rr, "hnw": None})
        if (i + 1) % 50 == 0:
            print(f"    MeteoSwiss {i + 1}/{len(st)}")
    return rows


def slf_stations(c: Cache):
    t = c.get(SLF_API + "/stations")
    out = {}
    try:
        for s in json.loads(t or "[]"):
            out[s["code"]] = {"lat": float(s["lat"]), "lon": float(s["lon"]), "z": float(s["elevation"]),
                              "name": s.get("label", "")}
    except (ValueError, KeyError, TypeError):
        pass
    return out


def slf_obs(c: Cache, st, d0: date, d1: date):
    rows = []
    for i, code in enumerate(sorted(st)):
        t = c.get(f"{SLF_ARCH}/data/by_station/{code}.csv")
        if not t:
            continue
        for x in csv.DictReader(io.StringIO(t)):
            md = x.get("measure_date", "")
            if md[11:13] != "06" or not (d0.isoformat() <= md[:10] <= d1.isoformat()):
                continue
            hn = _num(x.get("HN_1D"))
            if hn is None:
                continue
            rows.append({"src": "slf", "code": code, "day": md[:10], "hn": hn, "rr": None,
                         "hnw": _num(x.get("HNW_1D"))})
        if (i + 1) % 25 == 0:
            print(f"    SLF {i + 1}/{len(st)}")
    return rows


# ── the weather model's hours at the stations ────────────────────────────────
def om_hours(c: Cache, pts, d0: date, d1: date, model: str, chunk=20, pause=1.5):
    """{station: {time:[...], T, RH, P, U}} from the historical forecast API,
    at the station's own height (Open-Meteo moves the temperature there)."""
    out = {}
    keys = sorted(pts)
    for i in range(0, len(keys), chunk):
        ks = keys[i:i + chunk]
        params = {"latitude": ",".join(f"{pts[k]['lat']:.4f}" for k in ks),
                  "longitude": ",".join(f"{pts[k]['lon']:.4f}" for k in ks),
                  "elevation": ",".join(f"{pts[k]['z']:.0f}" for k in ks),
                  "hourly": ",".join(OM_VARS), "models": model, "timezone": "UTC",
                  "wind_speed_unit": "ms", "start_date": (d0 - timedelta(days=1)).isoformat(),
                  "end_date": d1.isoformat()}
        t = c.get(OM_HIST, params)
        if not t:
            print(f"    no weather for {len(ks)} stations ({ks[0]}..)")
            continue
        js = json.loads(t)
        js = js if isinstance(js, list) else [js]
        for k, j in zip(ks, js):
            h = j.get("hourly") or {}
            if not h.get("time"):
                continue
            arr = lambda v: np.array([np.nan if x is None else x for x in h.get(v, [])], float)
            out[k] = {"time": h["time"], "T": arr("temperature_2m"), "RH": arr("relative_humidity_2m"),
                      "P": arr("precipitation"), "U": arr("wind_speed_10m")}
        print(f"    weather {min(i + chunk, len(keys))}/{len(keys)}")
        time.sleep(pause)
    return out


# ── the model ───────────────────────────────────────────────────────────────
def wet_bulb(t, rh, z):
    try:
        from model.factors import wet_bulb_c
        return np.asarray(wet_bulb_c(t, rh, z), float)
    except Exception:                                   # noqa: BLE001 -- the same psychrometry, inline
        t = np.asarray(t, float); rh = np.clip(np.asarray(rh, float), 1, 100)
        p = 1013.25 * (1 - 2.25577e-5 * float(z)) ** 5.25588
        es = lambda x: 6.112 * np.exp(17.62 * x / (243.12 + x))
        e = rh / 100 * es(t); tw = t.copy()
        for _ in range(6):
            f = es(tw) - 6.53e-4 * p * (t - tw) - e
            tw = tw - f / (es(tw) * 17.62 * 243.12 / (243.12 + tw) ** 2 + 6.53e-4 * p)
        return np.minimum(tw, t)


def windows(times):
    """{end day: slice} for every complete 06 -> 06 UTC window."""
    ts = [datetime.strptime(s[:13], "%Y-%m-%dT%H") for s in times]
    out = {}
    for i, t in enumerate(ts):
        if t.hour == 6 and i >= 23 and ts[i - 23] == t - timedelta(hours=23):
            out[t.date().isoformat()] = slice(i - 23, i + 1)
    return out


def build_dataset(obs, stations, hours):
    """Station-days with the 24 hourly inputs: (meta list, P, T, TW, U arrays)."""
    meta, P, T, TW, U = [], [], [], [], []
    win = {k: windows(h["time"]) for k, h in hours.items()}
    tw_cache = {}
    for r in obs:
        k = (r["src"], r["code"]); h = hours.get(k)
        if h is None or r["day"] not in win[k]:
            continue
        sl = win[k][r["day"]]
        p, t, rh, u = h["P"][sl], h["T"][sl], h["RH"][sl], h["U"][sl]
        if np.isnan(p).any() or np.isnan(t).any():
            continue
        if k not in tw_cache:
            rr = np.where(np.isnan(h["RH"]), 100.0, h["RH"])
            tw_cache[k] = wet_bulb(h["T"], rr, stations[k]["z"])
        s = stations[k]
        meta.append(dict(r, z=s["z"], lat=s["lat"], lon=s["lon"], month=r["day"][:7]))
        P.append(p); T.append(t); TW.append(tw_cache[k][sl]); U.append(np.nan_to_num(u, nan=2.0))
    return meta, np.array(P), np.array(T), np.array(TW), np.array(U)


def predict(th, P, T, TW, U):
    frac = 1.0 / (1.0 + np.exp(np.clip(th["tw_k"] * (TW - th["tw_c"]), -50, 50)))
    rho = np.clip(th["rho_a"] + th["rho_b"] * T + th["rho_c"] * np.sqrt(np.maximum(U, 0)), th["rho_min"], th["rho_max"])
    return th["settle"] * (P * frac * 100.0 / rho).sum(axis=1)


def metrics(m, o, z):
    out = {"n": int(len(o)), "ratio": float(m.sum() / max(o.sum(), 1e-9)), "mae": float(np.abs(m - o).mean()),
           "bias": float((m - o).mean())}
    ho, hm = o >= 10, m >= 10
    out["ratio_obs10"] = float(m[ho].sum() / max(o[ho].sum(), 1e-9)) if ho.any() else None
    out["ratio_mod10"] = float(m[hm].sum() / max(o[hm].sum(), 1e-9)) if hm.any() else None
    o5, m5 = o >= 5, m >= 5
    out["pod5"] = float((m5 & o5).sum() / max(o5.sum(), 1))
    out["far5"] = float((m5 & ~o5).sum() / max(m5.sum(), 1))
    out["bands"] = {f"{lo}-{hi}": round(float((m - o)[(z >= lo) & (z < hi)].mean()), 2)
                    for lo, hi in BANDS if ((z >= lo) & (z < hi)).any()}
    return out


def objective(m, o, z):
    e = m - o
    bb = [abs(e[(z >= lo) & (z < hi)].mean()) for lo, hi in BANDS if ((z >= lo) & (z < hi)).sum() >= 20]
    return float(np.abs(e).mean() + 0.5 * (np.mean(bb) if bb else 0.0))


FREE = ("rho_a", "rho_b", "rho_c", "rho_min", "tw_c")
BOUNDS = {"rho_a": (40, 160), "rho_b": (0, 12), "rho_c": (0, 45), "rho_min": (30, 90), "tw_c": (-0.5, 2.5)}


def fit(th0, P, T, TW, U, o, z):
    """Coarse grid, then Nelder-Mead (scipy) inside the bounds."""
    best, bv = dict(th0), objective(predict(th0, P, T, TW, U), o, z)
    for a in range(50, 141, 10):
        for b in (4, 6, 8, 10):
            for cc in (0, 10, 20, 30):
                for tc in (0.5, 1.0, 1.5):
                    th = dict(th0, rho_a=a, rho_b=b, rho_c=cc, tw_c=tc)
                    v = objective(predict(th, P, T, TW, U), o, z)
                    if v < bv:
                        best, bv = th, v
    try:
        from scipy.optimize import minimize
        x0 = np.array([best[k] for k in FREE])

        def f(x):
            th = dict(best, **{k: float(np.clip(v, *BOUNDS[k])) for k, v in zip(FREE, x)})
            return objective(predict(th, P, T, TW, U), o, z)
        r = minimize(f, x0, method="Nelder-Mead", options={"maxiter": 400, "xatol": 0.5, "fatol": 1e-3})
        th = dict(best, **{k: float(np.clip(v, *BOUNDS[k])) for k, v in zip(FREE, r.x)})
        if f(r.x) < bv:
            best = th
    except ImportError:
        pass
    return best


def cv_by_month(th0, meta, P, T, TW, U, o, z):
    """Leave-one-month-out: each month predicted with parameters fitted on the others."""
    months = np.array([r["month"] for r in meta])
    pred = np.zeros_like(o)
    fits = {}
    for mo in sorted(set(months)):
        te = months == mo
        tr = ~te
        if tr.sum() < 50:
            pred[te] = predict(th0, P[te], T[te], TW[te], U[te]); continue
        th = fit(th0, P[tr], T[tr], TW[tr], U[tr], o[tr], z[tr])
        fits[mo] = {k: round(th[k], 2) for k in FREE}
        pred[te] = predict(th, P[te], T[te], TW[te], U[te])
    return pred, fits


# ── the board density from SLF's own water measurement (HNW) ────────────────
def density_check(meta, T, U, th):
    rows = []
    for i, r in enumerate(meta):
        if r["src"] != "slf" or not r.get("hnw") or r["hn"] < 5:
            continue
        rho_obs = r["hnw"] / (r["hn"] / 100.0)          # mm water / m snow = kg/m3
        tm, um = float(np.mean(T[i])), float(np.mean(U[i]))
        rho_mod = float(np.clip(th["rho_a"] + th["rho_b"] * tm + th["rho_c"] * math.sqrt(max(um, 0)), th["rho_min"], th["rho_max"]))
        rows.append((tm, um, rho_obs, rho_mod / th["settle"]))
    if len(rows) < 10:
        return None
    A = np.array(rows)
    out = {"n": len(A), "rho_board_median": float(np.median(A[:, 2])),
           "rho_model_settled_median": float(np.median(A[:, 3]))}
    bins = [(-30, -8), (-8, -4), (-4, -1), (-1, 3)]
    out["by_T"] = {f"{lo}..{hi} C": (int(((A[:, 0] >= lo) & (A[:, 0] < hi)).sum()),
                                     round(float(np.median(A[(A[:, 0] >= lo) & (A[:, 0] < hi), 2])), 0) if ((A[:, 0] >= lo) & (A[:, 0] < hi)).any() else None)
                   for lo, hi in bins}
    # rho = a + b T + c sqrt(U), least squares on the boards themselves
    X = np.column_stack([np.ones(len(A)), A[:, 0], np.sqrt(np.maximum(A[:, 1], 0))])
    coef, *_ = np.linalg.lstsq(X, A[:, 2], rcond=None)
    out["fit_rho_board"] = {"a": round(float(coef[0]), 1), "b": round(float(coef[1]), 2), "c": round(float(coef[2]), 1)}
    return out


# ── machine learning on the same station-days ───────────────────────────────
def ml_compare(meta, P, T, TW, U, o, pred_phys):
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.inspection import permutation_importance
    except ImportError:
        return None
    feats = {
        "precip_mm": P.sum(1), "precip_wet_h": (P > 0.1).sum(1), "T_mean": T.mean(1), "T_min": T.min(1),
        "T_max": T.max(1), "Tw_mean_wet": np.array([tw[p > 0.1].mean() if (p > 0.1).any() else tw.mean() for tw, p in zip(TW, P)]),
        "wind_mean": U.mean(1), "elev": np.array([r["z"] for r in meta]), "lat": np.array([r["lat"] for r in meta]),
        "lon": np.array([r["lon"] for r in meta]), "month": np.array([int(r["day"][5:7]) for r in meta]),
        "physics_cm": pred_phys}
    names = list(feats)
    X = np.column_stack([feats[k] for k in names])
    months = np.array([r["month"] for r in meta])
    pred = np.zeros_like(o)
    for mo in sorted(set(months)):
        te = months == mo
        if (~te).sum() < 100:
            pred[te] = pred_phys[te]; continue
        m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, l2_regularization=1.0)
        m.fit(X[~te], o[~te]); pred[te] = np.maximum(0, m.predict(X[te]))
    m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, l2_regularization=1.0).fit(X, o)
    imp = permutation_importance(m, X, o, n_repeats=5, random_state=0, scoring="neg_mean_absolute_error")
    order = np.argsort(-imp.importances_mean)
    return pred, [(names[i], round(float(imp.importances_mean[i]), 3)) for i in order]


# ── report ──────────────────────────────────────────────────────────────────
def fmt(mt):
    return (f"n {mt['n']}, Verhältnis {mt['ratio']:.2f}, MAE {mt['mae']:.2f} cm, Bias {mt['bias']:+.2f} cm, "
            f"Treffer ≥5 cm {mt['pod5']:.0%}, Fehlalarm ≥5 cm {mt['far5']:.0%}, "
            f"Tage ≥10 cm gemessen: {mt['ratio_obs10'] if mt['ratio_obs10'] is None else round(mt['ratio_obs10'], 2)}, "
            f"Tage ≥10 cm modelliert: {mt['ratio_mod10'] if mt['ratio_mod10'] is None else round(mt['ratio_mod10'], 2)}; "
            f"Höhenstufen (Modell − Messung): " + ", ".join(f"{k} m {v:+.2f}" for k, v in mt["bands"].items()))


def current_params():
    th = dict(DEFAULT)
    try:
        from config.settings import load_model_params
        p = load_model_params()
        s, t = p["slr"], p["temperature"]
        th.update(rho_a=float(s["rho_a"]), rho_b=float(s["rho_b"]), rho_c=float(s["rho_c"]),
                  rho_min=float(s["rho_min"]), rho_max=float(s["rho_max"]),
                  tw_c=float(t["wetbulb_center_c"]), tw_k=float(t["wetbulb_k"]))
        from pipeline.interactive_export import _ns_settle_24h
        th["settle"] = float(_ns_settle_24h() or th["settle"])
    except Exception as e:                              # noqa: BLE001
        print(f"  (repository parameters not readable, defaults used: {type(e).__name__})")
    return th


def run(meta, P, T, TW, U, out: Path, note=""):
    o = np.array([r["hn"] for r in meta], float); z = np.array([r["z"] for r in meta], float)
    th0 = current_params()
    p0 = predict(th0, P, T, TW, U)
    print("  fitting, leave-one-month-out ...")
    pcv, fits = cv_by_month(th0, meta, P, T, TW, U, o, z)
    th_all = fit(th0, P, T, TW, U, o, z)
    p_all = predict(th_all, P, T, TW, U)
    dens = density_check(meta, T, U, th_all)
    ml = ml_compare(meta, P, T, TW, U, o, p0)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "rows.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["src", "code", "day", "elev", "obs_cm", "gauge_mm", "model_mm", "model_now_cm", "model_cv_cm", "model_fit_cm"]
                   + (["ml_cv_cm"] if ml else []))
        for i, r in enumerate(meta):
            w.writerow([r["src"], r["code"], r["day"], int(r["z"]), r["hn"], r.get("rr"), round(float(P[i].sum()), 2),
                        round(float(p0[i]), 2), round(float(pcv[i]), 2), round(float(p_all[i]), 2)]
                       + ([round(float(ml[0][i]), 2)] if ml else []))
    sug = {"slr": {"method": "crocus", "rho_a": round(th_all["rho_a"], 1), "rho_b": round(th_all["rho_b"], 2),
                   "rho_c": round(th_all["rho_c"], 1), "rho_min": round(th_all["rho_min"], 1)},
           "temperature": {"wetbulb_center_c": round(th_all["tw_c"], 2)}}
    try:
        import yaml
        (out / "params_suggestion.yaml").write_text(yaml.safe_dump(sug, sort_keys=False, allow_unicode=True))
    except ImportError:
        (out / "params_suggestion.yaml").write_text(json.dumps(sug, indent=2))
    L = [f"# Neuschnee-Modell gegen Messungen ({note})", "",
         f"{len(meta)} Stationstage, {len(set((r['src'], r['code']) for r in meta))} Stationen, "
         f"{min(r['day'] for r in meta)} bis {max(r['day'] for r in meta)}.", "",
         "## Heutige Parameter der App", "", fmt(metrics(p0, o, z)), "",
         "## Kalibriert, Leave-one-month-out (ehrliche Prüfung)", "", fmt(metrics(pcv, o, z)), "",
         "Parameter je ausgelassenem Monat: " + json.dumps(fits), "",
         "## Kalibriert auf allem (Vorschlag)", "", fmt(metrics(p_all, o, z)), "",
         "```yaml", json.dumps(sug, indent=2), "```", ""]
    if dens:
        L += ["## Neuschneedichte direkt gemessen (SLF HNW)", "",
              f"{dens['n']} Tage mit ≥5 cm und gemessenem Wasser. Brett-Dichte Median {dens['rho_board_median']:.0f} kg/m³, "
              f"Modell auf dem Brett (nach 24 h Setzung) Median {dens['rho_model_settled_median']:.0f} kg/m³.", "",
              "Nach Temperatur (Tage, Median kg/m³): " + json.dumps(dens["by_T"], ensure_ascii=False), "",
              "Direkte Anpassung rho = a + b·T + c·√U an die Bretter (Brett-Dichte; mal Setzfaktor = frische Dichte "
              "wie in model_params.yaml): " + json.dumps(dens["fit_rho_board"]), "",
              "Liegt die Brett-Dichte deutlich über der kalibrierten, fehlt dem Wettermodell Niederschlag "
              "(oder der Messer fängt zu wenig); liegt sie darunter, ist es umgekehrt.", ""]
    if ml:
        L += ["## Machine Learning zum Vergleich (Gradient Boosting, Leave-one-month-out)", "",
              fmt(metrics(ml[0], o, z)), "",
              "Wichtigste Eingänge (Permutation, MAE-Zuwachs in cm): " + ", ".join(f"{k} {v}" for k, v in ml[1][:8]), "",
              "Ist das ML-Modell klar besser als die kalibrierte Physik, steckt in den wichtigsten Eingängen "
              "noch ein Effekt, den die Formel nicht hat -- ein Hinweis, welche Physik als Nächstes dran ist. "
              "Ins Modell gehört dann die Physik, nicht das ML.", ""]
    (out / "report.md").write_text("\n".join(L))
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 2, figsize=(11, 4.5))
        for a, (nm, p) in zip(ax, (("heute", p0), ("kalibriert (CV)", pcv))):
            a.scatter(o, p, s=6, alpha=.35); mx = max(5, float(np.percentile(np.r_[o, p], 99.5)))
            a.plot([0, mx], [0, mx], "k--", lw=.8); a.set_xlim(0, mx); a.set_ylim(0, mx)
            a.set_xlabel("gemessen (cm)"); a.set_ylabel("Modell (cm)"); a.set_title(f"Neuschnee 24 h, {nm}")
        fig.tight_layout(); fig.savefig(out / "scatter.png", dpi=130)
    except ImportError:
        pass
    print("\n".join(L[:14]))
    print(f"\n  -> {out / 'report.md'}, {out / 'rows.csv'}, {out / 'params_suggestion.yaml'}")


def selftest(out: Path):
    """Synthetic winter: a known density law, the fit has to find it."""
    rng = np.random.default_rng(1)
    n = 1500
    meta = []
    T = rng.normal(-3, 4, (n, 24)); U = np.abs(rng.normal(3, 2, (n, 24)))
    TW = T - np.abs(rng.normal(0.6, 0.3, (n, 24)))
    P = np.where(rng.random((n, 24)) < 0.25, rng.gamma(1.2, 1.0, (n, 24)), 0.0)
    true = dict(DEFAULT, rho_a=95.0, rho_b=7.0, rho_c=20.0)
    o = np.maximum(0, predict(true, P, T, TW, U) * rng.lognormal(0, 0.3, n))
    for i in range(n):
        meta.append({"src": "msw", "code": f"S{i % 60}", "day": f"2026-{1 + i % 3:02d}-{1 + i % 28:02d}",
                     "hn": float(o[i]), "rr": None, "hnw": None, "z": float(600 + (i % 30) * 80),
                     "lat": 46.5, "lon": 8.5, "month": f"2026-{1 + i % 3:02d}"})
    run(meta, P, T, TW, U, out, "Selbsttest, synthetisch: wahre Dichte 95 + 7T + 20√U")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2025-12-01")
    ap.add_argument("--end", default="2026-03-31")
    ap.add_argument("--out", default="calib")
    ap.add_argument("--cache", default=".calib_cache")
    ap.add_argument("--model", default="meteoswiss_icon_ch1",
                    help="Open-Meteo model: meteoswiss_icon_ch1 (from 2024), icon_d2, best_match ...")
    ap.add_argument("--sources", default="msw,slf")
    ap.add_argument("--max-stations", type=int, default=0, help="for a quick first look")
    ap.add_argument("--offline", action="store_true", help="only what is in the cache")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    if a.selftest:
        selftest(out); return
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    c = Cache(Path(a.cache), offline=a.offline)
    stations, obs = {}, []
    if "msw" in a.sources:
        print("MeteoSwiss manual network ...")
        st = msw_stations(c)
        if a.max_stations:
            st = dict(list(st.items())[:a.max_stations])
        stations.update({("msw", k): v for k, v in st.items()})
        obs += msw_obs(c, st, d0, d1)
    if "slf" in a.sources:
        print("SLF study plots ...")
        st = slf_stations(c)
        if a.max_stations:
            st = dict(list(st.items())[:a.max_stations])
        stations.update({("slf", k): v for k, v in st.items()})
        obs += slf_obs(c, st, d0, d1)
    print(f"  {len(obs)} station-days measured")
    need = {k: v for k, v in stations.items() if any((r["src"], r["code"]) == k for r in obs)}
    print(f"Weather model ({a.model}) at {len(need)} stations ...")
    hrs = om_hours(c, {f"{k[0]}:{k[1]}": v for k, v in need.items()}, d0, d1, a.model)
    hours = {tuple(k.split(":", 1)): v for k, v in hrs.items()}
    meta, P, T, TW, U = build_dataset(obs, stations, hours)
    print(f"  {len(meta)} station-days with weather")
    if len(meta) < 100:
        print("Too few station-days -- a longer period, or check the network/cache."); return
    run(meta, P, T, TW, U, out, f"{a.start} bis {a.end}, Wetter {a.model}")


if __name__ == "__main__":
    main()
