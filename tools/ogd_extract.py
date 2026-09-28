#!/usr/bin/env python3
"""MeteoSwiss open data (OGD): ICON-CH1/CH2 point series at the weather points.

Runs in its OWN virtualenv (meteodata-lab pins numpy < 2.4, the pipeline does
not), and writes plain JSON the pipeline splices into its forcing ahead of
Open-Meteo -- see variant_a/forcing.py (ogd_dir). Only the forecast part
comes from here: OGD keeps the latest runs, not an archive, so the past days
of a window stay with Open-Meteo.

    python tools/ogd_extract.py points.json out_dir [--models ch1,ch2] [--hours 33,120]

points.json: [{"id": "w000", "lat": 46.5, "lon": 9.8}, ...]
out_dir/<id>.json: {"model": ..., "ref_time": ..., "cell_elev": m|null,
                    "hourly": {time, temperature_2m, relative_humidity_2m,
                               precipitation, wind_speed_10m,
                               wind_direction_10m, shortwave_radiation}}
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, sys, time
from pathlib import Path

import numpy as np

COLL = {"ch1": "ogd-forecasting-icon-ch1", "ch2": "ogd-forecasting-icon-ch2"}
# DWD/ICON short names. TOT_PREC is accumulated since the run start, the
# radiation terms are averaged since the run start.
VARS = ["T_2M", "RELHUM_2M", "U_10M", "V_10M", "TOT_PREC", "ASWDIR_S", "ASWDIFD_S"]


def _get(coll, var, hours):
    from meteodatalab import ogd_api
    req = ogd_api.Request(collection=coll, variable=var, reference_datetime="latest",
                          perturbed=False, horizon=[dt.timedelta(hours=h) for h in hours])
    return ogd_api.get_from_ogd(req)


def available(coll, pages=6):
    """{variable: set(lead hours)} of the newest reference time, from STAC."""
    import requests
    url = "https://data.geo.admin.ch/api/stac/v1/search"
    body = {"collections": [f"ch.meteoschweiz.{coll}"], "limit": 500,
            "forecast:perturbed": False}
    seen, refs = {}, set()
    for _ in range(pages):
        r = requests.post(url, json=body, timeout=60)
        r.raise_for_status()
        j = r.json()
        for it in j.get("features", []):
            pr = it.get("properties", {})
            v, ref, hz = pr.get("forecast:variable"), pr.get("forecast:reference_datetime"), \
                pr.get("forecast:horizon")
            if v and ref:
                seen.setdefault(ref, {}).setdefault(v, set()).add(hz)
                refs.add(ref)
        nxt = [l for l in j.get("links", []) if l.get("rel") == "next"]
        if not nxt:
            break
        body = {**body, **(nxt[0].get("body") or {})}
    if not refs:
        return {}, None
    newest = max(refs)
    return seen[newest], newest


# first name that exists wins; RH can be derived from the dew point
ALTS = {"RELHUM_2M": ["RELHUM_2M", "TD_2M"], "ASWDIR_S": ["ASWDIR_S", "ASOB_S", "GLOB"],
        "ASWDIFD_S": ["ASWDIFD_S", None]}


def _fetch_var(coll, var, hours):
    t0 = time.time()
    da = _get(coll, var, hours)
    return var, da, time.time() - t0


def extract(points, model, hours):
    from concurrent.futures import ThreadPoolExecutor
    coll = COLL[model]
    try:
        have, ref = available(coll)
        print(f"  {model}: STAC newest run {ref}: " + ", ".join(sorted(have)), flush=True)
    except Exception as e:
        have, ref = {}, None
        print(f"  {model}: STAC listing failed ({e}); trying the default names", flush=True)
    want = []
    for v in VARS:
        for alt in ALTS.get(v, [v]):
            if alt is None or not have or alt in have:
                if alt is not None:
                    want.append(alt)
                break
    print(f"  {model}: fetching {want}", flush=True)
    out = {p["id"]: {} for p in points}
    geo = None
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_fetch_var, coll, v, list(range(0, hours + 1))) for v in want]
        for fu in futs:
            try:
                var, da, sec = fu.result()
            except Exception as e:
                print(f"  {model}: a variable failed: {type(e).__name__}: {e}", flush=True)
                continue
            vals, lead, ref_dt = _series(da)
            if geo is None:
                lat, lon = _latlon(da)
                idx = {}
                for p in points:
                    d = (lat - p["lat"]) ** 2 + ((lon - p["lon"]) * math.cos(math.radians(p["lat"]))) ** 2
                    idx[p["id"]] = int(np.nanargmin(d))
                geo = (lead, ref_dt, idx)
            for p in points:
                out[p["id"]][var] = vals[:, geo[2][p["id"]]]
            print(f"  {model} {var}: {vals.shape[0]} lead times in {sec:.0f}s", flush=True)
    if geo is None:
        raise RuntimeError("no variable could be fetched")
    return out, geo[0], geo[1], geo[2]


def to_hourly(v, lead, ref):
    """ICON conventions -> Open-Meteo style hourly block."""
    n = len(lead)
    times = [(ref + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in lead]
    nanv = np.full(n, np.nan)
    tp = np.asarray(v.get("TOT_PREC", nanv), float)
    prec = np.concatenate([[np.nan], np.maximum(np.diff(tp), 0.0)])
    def deavg(a):
        a = np.asarray(a, float); o = np.full(n, np.nan)
        for i in range(1, n):
            h0, h1 = lead[i - 1], lead[i]
            o[i] = (a[i] * h1 - a[i - 1] * h0) / max(1, h1 - h0)
        return np.maximum(o, 0.0)
    if "ASWDIR_S" in v:
        sw = deavg(v["ASWDIR_S"]) + (deavg(v["ASWDIFD_S"]) if "ASWDIFD_S" in v else 0.0)
    elif "GLOB" in v:
        sw = deavg(v["GLOB"])
    elif "ASOB_S" in v:                       # net shortwave: back to incoming, albedo ~0.8 on snow
        sw = deavg(v["ASOB_S"]) / 0.2
    else:
        sw = nanv
    u, w = np.asarray(v.get("U_10M", nanv), float), np.asarray(v.get("V_10M", nanv), float)
    spd = np.hypot(u, w)
    dirn = (np.degrees(np.arctan2(-u, -w)) + 360.0) % 360.0      # FROM direction
    t = np.asarray(v.get("T_2M", nanv), float)
    if np.nanmean(t) > 150:
        t = t - 273.15
    if "RELHUM_2M" in v:
        rh = np.clip(np.asarray(v["RELHUM_2M"], float), 0, 100)
    elif "TD_2M" in v:
        td = np.asarray(v["TD_2M"], float)
        td = td - 273.15 if np.nanmean(td) > 150 else td
        es = lambda x: np.exp(17.625 * x / (x + 243.04))       # Magnus
        rh = np.clip(100 * es(td) / es(t), 0, 100)
    else:
        rh = nanv
    def L(a):
        return [None if not np.isfinite(x) else round(float(x), 3) for x in a]
    # hour 0 has no accumulation / average interval: drop it
    return {"time": times[1:], "temperature_2m": L(t[1:]), "relative_humidity_2m": L(rh[1:]),
            "precipitation": L(prec[1:]), "wind_speed_10m": L(spd[1:]),
            "wind_direction_10m": L(dirn[1:]), "shortwave_radiation": L(sw[1:])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("points"); ap.add_argument("out")
    ap.add_argument("--models", default="ch1,ch2")
    ap.add_argument("--hours", default="33,120")
    a = ap.parse_args()
    pts = json.loads(Path(a.points).read_text())
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    merged = {p["id"]: None for p in pts}
    for model, hrs in zip(a.models.split(","), [int(x) for x in a.hours.split(",")]):
        try:
            vals, lead, ref, idx = extract(pts, model, hrs)
        except Exception as e:
            print(f"  {model}: FAILED {type(e).__name__}: {e}")
            continue
        print(f"  {model}: run {ref:%Y-%m-%d %H:%M} UTC, {len(lead)} lead times")
        for p in pts:
            h = to_hourly(vals[p["id"]], lead, ref)
            prev = merged[p["id"]]
            if prev is None:
                merged[p["id"]] = {"model": f"ogd-icon-{model}", "ref_time": f"{ref:%Y-%m-%dT%H:%M}",
                                   "hourly": h}
            else:                                    # finer model first, coarser fills later hours
                have = set(prev["hourly"]["time"])
                for i, t in enumerate(h["time"]):
                    if t not in have:
                        for k in h:
                            prev["hourly"][k].append(h[k][i])
                prev["model"] += f"+{model}"
    n = 0
    for pid, d in merged.items():
        if d:
            (out / f"{pid}.json").write_text(json.dumps(d))
            n += 1
    print(f"OGD: {n}/{len(pts)} points written to {out}")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
