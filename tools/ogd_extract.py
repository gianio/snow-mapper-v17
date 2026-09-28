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


def _latlon(da):
    lat = np.asarray(da["lat"]).ravel(); lon = np.asarray(da["lon"]).ravel()
    if np.nanmax(np.abs(lat)) < 3.2:                 # radians
        lat, lon = np.degrees(lat), np.degrees(lon)
    return lat, lon


def _series(da):
    """(lead_hours, cells) float array and the reference time."""
    a = da.squeeze()
    dims = list(a.dims)
    lt = [d for d in dims if d in ("lead_time", "step")][0]
    cell = [d for d in dims if d not in (lt, "ref_time", "eps", "z")][0]
    a = a.transpose(lt, cell)
    lead = [int(round(np.timedelta64(v, "s").astype(float) / 3600))
            if not isinstance(v, (int, float)) else int(v) for v in a[lt].values]
    ref = da["ref_time"].values
    ref = np.atleast_1d(ref)[0]
    ref_dt = dt.datetime.utcfromtimestamp(ref.astype("datetime64[s]").astype(int))
    return np.asarray(a.values, float), lead, ref_dt


def extract(points, model, hours):
    coll = COLL[model]
    first = None
    out = {p["id"]: {} for p in points}
    for var in VARS:
        t0 = time.time()
        da = _get(coll, var, list(range(0, hours + 1)))
        vals, lead, ref = _series(da)
        if first is None:
            lat, lon = _latlon(da)
            idx = {}
            for p in points:
                d = (lat - p["lat"]) ** 2 + ((lon - p["lon"]) * math.cos(math.radians(p["lat"]))) ** 2
                idx[p["id"]] = int(np.nanargmin(d))
            first = (lead, ref, idx)
        lead, ref, idx = first[0], first[1], first[2]
        for p in points:
            out[p["id"]][var] = vals[:, idx[p["id"]]]
        print(f"  {model} {var}: {vals.shape[0]} lead times in {time.time()-t0:.0f}s", flush=True)
    return out, lead, ref, first[2]


def to_hourly(v, lead, ref):
    """ICON conventions -> Open-Meteo style hourly block."""
    n = len(lead)
    times = [(ref + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in lead]
    tp = np.asarray(v["TOT_PREC"], float)
    prec = np.concatenate([[np.nan], np.maximum(np.diff(tp), 0.0)])
    def deavg(a):
        a = np.asarray(a, float); o = np.full(n, np.nan)
        for i in range(1, n):
            h0, h1 = lead[i - 1], lead[i]
            o[i] = (a[i] * h1 - a[i - 1] * h0) / max(1, h1 - h0)
        return np.maximum(o, 0.0)
    sw = deavg(v["ASWDIR_S"]) + deavg(v["ASWDIFD_S"])
    u, w = np.asarray(v["U_10M"], float), np.asarray(v["V_10M"], float)
    spd = np.hypot(u, w)
    dirn = (np.degrees(np.arctan2(-u, -w)) + 360.0) % 360.0      # FROM direction
    t = np.asarray(v["T_2M"], float)
    if np.nanmean(t) > 150:
        t = t - 273.15
    rh = np.clip(np.asarray(v["RELHUM_2M"], float), 0, 100)
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
