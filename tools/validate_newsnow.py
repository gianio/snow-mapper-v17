#!/usr/bin/env python3
"""Model new snow against the official station measurements, every day.

Measured (both CC BY, the source must be named -- see SOURCES):
  slf   SLF manual study plots (Beobachterstationen, ~100): HN_1D at 06 UTC =
        new snow of the 24 h before. Live: measurement-api.slf.ch (7 days);
        older: measurement-data.slf.ch (by_station CSV).
  msw   MeteoSwiss manual precipitation network (~270): hns000d0 of day D =
        new snow 06 UTC D .. 06 UTC D+1, rre150d0 the precipitation (mm) of
        the same 24 h -- for the snow/rain question. data.geo.admin.ch (OGD).
Both are put on the same key: the END date of the 24 h window (06 UTC).

Model: tools' input .npz from run_interactive.py --model-daily
(pipeline/validation_grid.py): the daily new snow per cell, settled 24 h.
A station counts when its height is within --max-dz of its model cell.

Per day and source: n, bias, MAE, detection of >= 1 cm (POD, FAR, CSI), by
height band; for MeteoSwiss also snow/rain (days with >= 2 mm: did the model
put snow where the board had snow?). Two kinds of rows:
  an  the model's value for a day that has passed (analysis / hindcast)
  fc  what the model said for that day the day before (kept from the build
      in which the window was still ahead) -- the forecast skill
  rb  backfill: past days re-run with the model code of the backfill

History (rows + the forecasts waiting for their day + the model versions) in
a JSON file, kept in R2 between runs (validation/newsnow_hist.json) when R2 is
set up; the app reads the summary written to --out (data/validation.json).

    python tools/validate_newsnow.py --model model_daily.npz --hist hist.json \
        --out dist/data/validation.json [--kind an|rb] [--r2]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
SLF_API = "https://measurement-api.slf.ch/public/api/study-plot"
SLF_ARCH = "https://measurement-data.slf.ch/study-plot"
MSW = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-nime"
UA = {"User-Agent": "snowmapper-validation/1.0"}
BANDS = [(0, 1000), (1000, 1500), (1500, 2000), (2000, 5000)]
SOURCES = [
    {"id": "slf", "name": "WSL-Institut für Schnee- und Lawinenforschung SLF",
     "what": "Manuelle Messfelder (Beobachterstationen), Neuschnee 24 h",
     "licence": "CC BY 4.0", "url": "https://www.slf.ch",
     "terms": "https://www.slf.ch/en/services-and-products/slf-data-service/"},
    {"id": "msw", "name": "Bundesamt für Meteorologie und Klimatologie MeteoSchweiz",
     "what": "Manuelles Niederschlagsmessnetz, Neuschnee und Niederschlag 24 h",
     "licence": "CC BY 4.0", "url": "https://www.meteoschweiz.admin.ch",
     "terms": "https://opendatadocs.meteoswiss.ch/general/terms-of-use"},
]
SOURCES.append(
    {"id": "smn", "name": "Bundesamt für Meteorologie und Klimatologie MeteoSchweiz",
     "what": "SwissMetNet (automatische Stationen), Temperatur Tagesminimum und -maximum",
     "licence": "CC BY 4.0", "url": "https://www.meteoschweiz.admin.ch",
     "terms": "https://opendatadocs.meteoswiss.ch/general/terms-of-use"})
SMN = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn"
LAPSE = -0.0065          # K/m, the app's TEMP_LAPSE: model cell height -> station height
T_MAX_DZ = 800.0         # beyond that a 1 km cell says little about the station
R2_KEY = "validation/newsnow_hist.json"


def _get(url, **kw):
    for k in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 422):
                return None
        except requests.RequestException:
            pass
        time.sleep(2 + 3 * k)
    return None


# ── observations ─────────────────────────────────────────────────────────────
def slf_stations():
    r = _get(SLF_API + "/stations")
    return {s["code"]: (s["lat"], s["lon"], s["elevation"], s.get("label")) for s in (r.json() if r else [])}


def slf_obs(d0: date, d1: date):
    """{end_date: {code: HN_1D}} for d0..d1 (end dates)."""
    st = slf_stations()
    out = {}
    live = (date.today() - d0).days <= 6
    for code in st:
        rows = []
        if live:
            r = _get(f"{SLF_API}/station/{code}/measurements", params={"period_in_days": 7})
            rows = r.json() if r else []
            rows = [(x["measure_date"][:10], x.get("HN_1D")) for x in rows if x["measure_date"][11:13] == "06"]
        else:
            r = _get(f"{SLF_ARCH}/data/by_station/{code}.csv")
            if r:
                for x in csv.DictReader(io.StringIO(r.text)):
                    md = x["measure_date"]
                    if md[11:13] == "06" and d0.isoformat() <= md[:10] <= d1.isoformat():
                        rows.append((md[:10], float(x["HN_1D"]) if x["HN_1D"] not in ("", None) else None))
        for day, hn in rows:
            if hn is None or not (d0.isoformat() <= day <= d1.isoformat()):
                continue
            out.setdefault(day, {})[code] = float(hn)
    return out, st


def msw_stations():
    r = _get(MSW + "/ogd-nime_meta_stations.csv")
    if not r:
        return {}
    txt = r.content.decode("latin-1")
    st = {}
    for x in csv.DictReader(io.StringIO(txt), delimiter=";"):
        try:
            st[x["station_abbr"]] = (float(x["station_coordinates_wgs84_lat"]), float(x["station_coordinates_wgs84_lon"]),
                                     float(x["station_height_masl"]), x["station_name"])
        except (KeyError, ValueError):
            pass
    return st


def msw_obs(d0: date, d1: date):
    """({end_date: {code: hns}}, {end_date: {code: rre}}) -- hns000d0 of day D
    is the window ending D+1 06 UTC."""
    st = msw_stations()
    hn, rr = {}, {}
    hist = (date.today() - d0).days > 300
    for code in st:
        r = _get(f"{MSW}/{code.lower()}/ogd-nime_{code.lower()}_d_{'historical' if hist else 'recent'}.csv")
        if not r:
            continue
        for x in csv.DictReader(io.StringIO(r.content.decode("latin-1")), delimiter=";"):
            try:
                dd = datetime.strptime(x["reference_timestamp"][:10], "%d.%m.%Y").date() + timedelta(days=1)
            except (KeyError, ValueError):
                continue
            if not (d0 <= dd <= d1):
                continue
            def num(k):
                v = (x.get(k) or "").strip()
                try:
                    return float(v)
                except ValueError:
                    return None
            h, p = num("hns000d0"), num("rre150d0")
            if h is not None:
                hn.setdefault(dd.isoformat(), {})[code] = h
            if p is not None:
                rr.setdefault(dd.isoformat(), {})[code] = p
    return hn, rr, st


def smn_stations():
    r = _get(SMN + "/ogd-smn_meta_stations.csv")
    if not r:
        return {}
    st = {}
    for x in csv.DictReader(io.StringIO(r.content.decode("latin-1")), delimiter=";"):
        try:
            st[x["station_abbr"]] = (float(x["station_coordinates_wgs84_lat"]), float(x["station_coordinates_wgs84_lon"]),
                                     float(x["station_height_masl"]), x["station_name"])
        except (KeyError, ValueError):
            pass
    return st


def smn_temp(d0: date, d1: date):
    """({day: {code: (tmin, tmax)}}, stations) -- SwissMetNet daily 2 m
    minimum (tre200dn) and maximum (tre200dx) of the UTC day."""
    st = smn_stations()
    out = {}
    hist = (date.today() - d0).days > 300
    for code in st:
        r = _get(f"{SMN}/{code.lower()}/ogd-smn_{code.lower()}_d_{'historical' if hist else 'recent'}.csv")
        if not r:
            continue
        for x in csv.DictReader(io.StringIO(r.content.decode("latin-1")), delimiter=";"):
            try:
                dd = datetime.strptime(x["reference_timestamp"][:10], "%d.%m.%Y").date()
                lo, hi = float(x["tre200dn"]), float(x["tre200dx"])
            except (KeyError, ValueError):
                continue
            if d0 <= dd <= d1:
                out.setdefault(dd.isoformat(), {})[code] = (lo, hi)
    return out, st


# ── model ────────────────────────────────────────────────────────────────────
class Model:
    def __init__(self, path):
        z = np.load(path)
        self.days = [str(d) for d in z["days"]]
        self.sums, self.elev = z["sums"], z["elev"]
        self.laMin, self.loMin, self.laMax, self.loMax = (float(v) for v in z["bounds"])
        self.H, self.W = self.elev.shape
        self.built = str(z["built"])
        self.tdays = [str(d) for d in z["tdays"]] if "tdays" in z.files else []
        self.tmin = z["tmin"].astype(np.float32) if "tmin" in z.files else None
        self.tmax = z["tmax"].astype(np.float32) if "tmax" in z.files else None
        self.now = datetime.strptime(str(z["now_time"])[:13], "%Y-%m-%dT%H")

    def cell(self, lat, lon):
        if not (self.laMin < lat < self.laMax and self.loMin < lon < self.loMax):
            return None
        x = int((lon - self.loMin) / (self.loMax - self.loMin) * self.W)
        y = int((self.laMax - lat) / (self.laMax - self.laMin) * self.H)
        return (y, x) if 0 <= x < self.W and 0 <= y < self.H else None

    def is_forecast(self, day):
        """the window (day-1 06, day 06] lies wholly after the build's now"""
        return datetime.fromisoformat(day) + timedelta(hours=6) - timedelta(hours=24) >= self.now


# ── metrics ──────────────────────────────────────────────────────────────────
def metrics(pairs):
    """pairs: [(model, obs, elev)] -> dict"""
    if not pairs:
        return {"n": 0}
    m = np.array([p[0] for p in pairs]); o = np.array([p[1] for p in pairs])
    e = m - o
    hit = int(((m >= 1) & (o >= 1)).sum()); miss = int(((m < 1) & (o >= 1)).sum()); fa = int(((m >= 1) & (o < 1)).sum())
    res = {"n": len(pairs), "bias": round(float(e.mean()), 2), "mae": round(float(np.abs(e).mean()), 2),
           "obs_mean": round(float(o.mean()), 2), "mod_mean": round(float(m.mean()), 2),
           "hit": hit, "miss": miss, "fa": fa,
           "pod": round(hit / (hit + miss), 3) if hit + miss else None,
           "far": round(fa / (hit + fa), 3) if hit + fa else None,
           "csi": round(hit / (hit + miss + fa), 3) if hit + miss + fa else None}
    bands = {}
    for lo, hi in BANDS:
        sel = [p for p in pairs if lo <= p[2] < hi]
        if sel:
            ee = np.array([p[0] - p[1] for p in sel])
            bands[f"{lo}-{hi}"] = {"n": len(sel), "bias": round(float(ee.mean()), 2), "mae": round(float(np.abs(ee).mean()), 2)}
    res["bands"] = bands
    return res


def snow_rain(pairs):
    """[(model_hn, obs_hn, precip_mm)] on days with >= 2 mm: snow (>= 1 cm on
    the board) or rain, and did the model have snow there?"""
    sel = [p for p in pairs if p[2] is not None and p[2] >= 2.0]
    if not sel:
        return {"n": 0}
    agree = sum(1 for m, o, _ in sel if (m >= 1) == (o >= 1))
    return {"n": len(sel), "agree": round(agree / len(sel), 3),
            "obs_snow": sum(1 for _, o, _ in sel if o >= 1), "mod_snow": sum(1 for m, _, _ in sel if m >= 1)}


# ── history ──────────────────────────────────────────────────────────────────
def r2():
    acc, key, sec = (os.environ.get(k, "") for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"))
    ep = os.environ.get("S3_ENDPOINT") or (f"https://{acc}.r2.cloudflarestorage.com" if acc else "")
    if not (ep and key and sec):
        return None
    bucket = os.environ.get("R2_ARCHIVE_BUCKET") or os.environ.get("R2_BUCKET") or "snowmapper-tiles"
    env = dict(os.environ, AWS_ACCESS_KEY_ID=key, AWS_SECRET_ACCESS_KEY=sec, AWS_DEFAULT_REGION="auto")
    return ep, bucket, env


def r2_cp(src, dst):
    s = r2()
    if not s:
        return False
    ep, b, env = s
    src = src.replace("r2://", f"s3://{b}/"); dst = dst.replace("r2://", f"s3://{b}/")
    r = subprocess.run(["aws", "s3", "cp", src, dst, "--endpoint-url", ep, "--only-show-errors"], env=env,
                       capture_output=True, text=True)
    if r.returncode:
        print("r2:", r.stderr.strip()[-300:])
    return r.returncode == 0


def load_hist(path: Path, use_r2: bool):
    if use_r2 and r2():
        if not r2_cp("r2://" + R2_KEY, str(path)):
            print("history: none in R2 yet (or not reachable) -- starting a new one")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"rows": {}, "pred": {}, "versions": []}


def model_version():
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        except OSError:
            sha = ""
    return sha[:10]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--hist", default="validation_hist.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--kind", default="an", choices=["an", "rb"], help="an = daily run, rb = backfill run")
    ap.add_argument("--max-dz", type=float, default=300.0)
    ap.add_argument("--r2", action="store_true", help="keep the history in R2 (validation/newsnow_hist.json)")
    ap.add_argument("--no-write-r2", action="store_true")
    a = ap.parse_args()

    M = Model(a.model)
    hpath = Path(a.hist)
    H = load_hist(hpath, a.r2)
    ver = model_version()
    if not H["versions"] or H["versions"][-1]["sha"] != ver:
        H["versions"].append({"since": M.built[:10], "sha": ver, "kind": a.kind})

    if a.kind == "rb":      # a re-run of past days: all of them are past, none a forecast
        past, fut = [d for d in M.days if d <= date.today().isoformat()], []
    else:
        past = [d for d in M.days if not M.is_forecast(d) and d <= date.today().isoformat()]
        fut = [d for d in M.days if M.is_forecast(d)]
    if past:
        d0, d1 = date.fromisoformat(min(past)), date.fromisoformat(max(past))
        print(f"model {M.built}: past days {d0} .. {d1}, forecast days {fut}")
        slf, slf_st = slf_obs(d0, d1)
        mhn, mrr, msw_st = msw_obs(d0, d1)
    else:
        slf, slf_st, mhn, mrr, msw_st = {}, slf_stations(), {}, {}, msw_stations()

    def pairs_for(day, obs, st):
        k = M.days.index(day)
        out = []
        for code, ov in (obs.get(day) or {}).items():
            s = st.get(code)
            c = s and M.cell(s[0], s[1])
            if not c:
                continue
            ez = float(M.elev[c])
            if not math.isfinite(ez) or abs(ez - s[2]) > a.max_dz:
                continue
            out.append((float(M.sums[k][c]), ov, s[2], code))
        return out

    # analysis / backfill rows for the past days
    for day in past:
        for sid, obs, st in (("slf", slf, slf_st), ("msw", mhn, msw_st)):
            pr = pairs_for(day, obs, st)
            if not pr:
                continue
            row = metrics([(p[0], p[1], p[2]) for p in pr])
            row.update({"sha": ver, "built": M.built})
            if sid == "msw":
                row["snow_rain"] = snow_rain([(p[0], p[1], (mrr.get(day) or {}).get(p[3])) for p in pr])
            # an backfill row never replaces an operational one
            key = f"{day}|{sid}|{a.kind}"
            H["rows"][key] = row
            if a.kind == "an":       # per station, for the stations layer of the app
                H.setdefault("nst", {}).setdefault(day, {})[sid] = {p[3]: [round(p[0], 1), p[1]] for p in pr}
            # forecasts waiting for this day: scored now
            fc = (H["pred"].get(day) or {}).get(sid)
            if fc:
                fpairs = [(fc["v"][p[3]], p[1], p[2]) for p in pr if p[3] in fc["v"]]
                if fpairs:
                    fr = metrics(fpairs)
                    fr.update({"sha": fc["sha"], "built": fc["built"]})
                    H["rows"][f"{day}|{sid}|fc"] = fr
    # remember the forecast for the next day (the first complete window ahead)
    if a.kind == "an" and fut:
        day = fut[0]
        k = M.days.index(day)
        for sid, st in (("slf", slf_st), ("msw", msw_st)):
            v = {}
            for code, s in st.items():
                c = M.cell(s[0], s[1])
                if c and math.isfinite(float(M.elev[c])) and abs(float(M.elev[c]) - s[2]) <= a.max_dz:
                    v[code] = round(float(M.sums[k][c]), 1)
            H["pred"].setdefault(day, {})[sid] = {"v": v, "sha": ver, "built": M.built}
    # 2 m temperature: daily min / max against SwissMetNet, the model moved
    # from its cell's mean height to the station's
    tpast = [d for d in M.tdays if datetime.fromisoformat(d) + timedelta(days=1) <= M.now
             and d < date.today().isoformat()] if a.kind == "an" else [d for d in M.tdays if d < date.today().isoformat()]
    if tpast:
        tobs, tst = smn_temp(date.fromisoformat(min(tpast)), date.fromisoformat(max(tpast)))
        H.setdefault("tst", {})
        for day in tpast:
            k = M.tdays.index(day)
            per = {}
            for code, (olo, ohi) in (tobs.get(day) or {}).items():
                s_ = tst.get(code)
                c = s_ and M.cell(s_[0], s_[1])
                if not c:
                    continue
                ez = float(M.elev[c])
                if not math.isfinite(ez) or abs(ez - s_[2]) > T_MAX_DZ:
                    continue
                dz = LAPSE * (s_[2] - ez)
                per[code] = (round(float(M.tmin[k][c]) + dz, 1), round(float(M.tmax[k][c]) + dz, 1), olo, ohi)
            if not per:
                continue
            e_lo = [v[0] - v[2] for v in per.values()]
            e_hi = [v[1] - v[3] for v in per.values()]
            H["rows"][f"{day}|tmp|{a.kind}"] = {
                "n": len(per), "sha": ver, "built": M.built,
                "bias_min": round(sum(e_lo) / len(e_lo), 2), "mae_min": round(sum(abs(x) for x in e_lo) / len(e_lo), 2),
                "bias_max": round(sum(e_hi) / len(e_hi), 2), "mae_max": round(sum(abs(x) for x in e_hi) / len(e_hi), 2)}
            if a.kind == "an":
                H["tst"][day] = per
        H["tmeta"] = {c: [round(v[0], 4), round(v[1], 4), round(v[2]), v[3]] for c, v in tst.items()}
        cut14 = (date.today() - timedelta(days=14)).isoformat()
        H["tst"] = {d: v for d, v in H["tst"].items() if d >= cut14}
    if past:
        H["nmeta"] = {"slf": {c: [round(v[0], 4), round(v[1], 4), round(v[2]), v[3]] for c, v in slf_st.items()},
                      "msw": {c: [round(v[0], 4), round(v[1], 4), round(v[2]), v[3]] for c, v in msw_st.items()}}
    cut14 = (date.today() - timedelta(days=14)).isoformat()
    H["nst"] = {d: v for d, v in (H.get("nst") or {}).items() if d >= cut14}
    # forget forecasts older than 10 days
    cut = (date.today() - timedelta(days=10)).isoformat()
    H["pred"] = {d: v for d, v in H["pred"].items() if d >= cut}
    H["updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
    hpath.write_text(json.dumps(H, separators=(",", ":")))
    if a.r2 and not a.no_write_r2:
        print("history -> R2:", r2_cp(str(hpath), "r2://" + R2_KEY))
    write_summary(H, Path(a.out))


def _agg(rows):
    """pooled metrics over several day rows (weighted by n)"""
    rows = [r for r in rows if r.get("n")]
    if not rows:
        return None
    n = sum(r["n"] for r in rows)
    w = lambda k: round(sum(r[k] * r["n"] for r in rows) / n, 2)
    hit, miss, fa = (sum(r.get(k, 0) for r in rows) for k in ("hit", "miss", "fa"))
    return {"n": n, "days": len(rows), "bias": w("bias"), "mae": w("mae"),
            "pod": round(hit / (hit + miss), 3) if hit + miss else None,
            "far": round(fa / (hit + fa), 3) if hit + fa else None}


def write_summary(H, out: Path):
    rows = H["rows"]
    by_day = {}
    tby = {}
    for key, r in rows.items():
        day, sid, kind = key.split("|")
        if sid == "tmp":
            tby.setdefault(day, {})[kind] = r
            continue
        by_day.setdefault(day, {}).setdefault(kind, []).append(r)
    days = sorted(by_day)
    today = date.today()
    def window(n, kind):
        lo = (today - timedelta(days=n)).isoformat()
        return _agg([r for d in days if d >= lo for r in by_day[d].get(kind, [])])
    series = []
    for d in days[-120:]:
        e = {"d": d}
        for kind in ("an", "fc", "rb"):
            ag = _agg(by_day[d].get(kind, []))
            if ag:
                e[kind] = {"mae": ag["mae"], "bias": ag["bias"], "n": ag["n"], "pod": ag["pod"]}
        series.append(e)
    sr = [r["snow_rain"] for k, r in rows.items() if k.endswith("|msw|an") and r.get("snow_rain", {}).get("n")]
    sr_n = sum(x["n"] for x in sr)
    bands = {}
    for k, r in rows.items():
        if not k.endswith("|an") or k[:10] < (today - timedelta(days=30)).isoformat():
            continue
        for b, v in (r.get("bands") or {}).items():
            t = bands.setdefault(b, [0, 0.0, 0.0])
            t[0] += v["n"]; t[1] += v["bias"] * v["n"]; t[2] += v["mae"] * v["n"]
    changes = []
    try:
        changes = json.loads((ROOT / "docs" / "model_changes.json").read_text())
    except (OSError, ValueError):
        pass
    def tagg(n, kind="an"):
        lo = (today - timedelta(days=n)).isoformat()
        rs = [v[kind] for d, v in tby.items() if d >= lo and kind in v]
        if not rs:
            return None
        nn = sum(r["n"] for r in rs)
        w = lambda k: round(sum(r[k] * r["n"] for r in rs) / nn, 2)
        return {"n": nn, "days": len(rs), "bias_min": w("bias_min"), "mae_min": w("mae_min"),
                "bias_max": w("bias_max"), "mae_max": w("mae_max")}
    tser = [{"d": d, **{k: tby[d][kd][k] for kd in ("an", "rb") if kd in tby[d] for k in ("mae_min", "mae_max", "bias_min", "bias_max")},
             "kind": "an" if "an" in tby[d] else "rb"} for d in sorted(tby)[-120:]]
    # the stations, with the model beside the measurement for the last days
    tst, meta = H.get("tst") or {}, H.get("tmeta") or {}
    tdays = sorted(tst)[-14:]
    stations = []
    for code, m in meta.items():
        v = [(d, tst[d][code]) for d in tdays if code in tst[d]]
        if not v:
            continue
        stations.append({"code": code, "lat": m[0], "lon": m[1], "z": m[2], "name": m[3],
                         "days": [{"d": d, "mn": x[0], "mx": x[1], "on": x[2], "ox": x[3]} for d, x in v]})
    # new snow per station: the last 14 measured days, model beside it
    nst, nmeta = H.get("nst") or {}, H.get("nmeta") or {}
    ndays = sorted(nst)[-14:]
    nstations = []
    for sid in ("slf", "msw"):
        for code, m in (nmeta.get(sid) or {}).items():
            v = [{"d": d, "m": nst[d][sid][code][0], "o": nst[d][sid][code][1]} for d in ndays if code in (nst[d].get(sid) or {})]
            if v:
                nstations.append({"src": sid, "code": code, "lat": m[0], "lon": m[1], "z": m[2], "name": m[3], "days": v})
    summary = {
        "updated": H.get("updated"),
        "stations_newsnow": nstations,
        "temp": {"last7": tagg(7), "last30": tagg(30), "season": tagg(400) or tagg(400, "rb"), "series": tser,
                 "stations": stations, "lapse": LAPSE} if tby else None,
        "newsnow": {
            "last7": {"an": window(7, "an"), "fc": window(7, "fc")},
            "last30": {"an": window(30, "an"), "fc": window(30, "fc")},
            "season": {"an": window(400, "an"), "fc": window(400, "fc"), "rb": window(400, "rb")},
            "series": series,
            "bands30": {b: {"n": t[0], "bias": round(t[1] / t[0], 2), "mae": round(t[2] / t[0], 2)} for b, t in bands.items() if t[0]},
            "snow_rain": {"n": sr_n, "agree": round(sum(x["agree"] * x["n"] for x in sr) / sr_n, 3)} if sr_n else None,
        },
        "versions": H.get("versions", [])[-40:],
        "changes": changes,
        "sources": SOURCES,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    s7 = summary["newsnow"]["last7"]["an"]
    print(f"summary -> {out}: last 7 days {s7}")


if __name__ == "__main__":
    sys.exit(main())
