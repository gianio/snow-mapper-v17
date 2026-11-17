#!/usr/bin/env python3
"""Compare Snowmapper's new snow with two outside sources, cell by cell.

  1. PowderGuide (map.powderguide.com, ICON-EU, variable snow_gsp__lsfwe):
     the FORECAST new snow of the next day (24 h sum, 10:1 -- 1 mm = 1 cm),
     read from its colour tiles and turned back into cm with its colour bar.
  2. WhiteRisk / SLF (whiterisk.ch snow maps, HN1D): the MEASURED new snow
     of the last 24 h (interpolated station network), read from the
     value-encoded image the site uses itself.

Snowmapper is read from the live site's data blob (data/latest.json): the
hourly new snow per 1 km cell, summed over the same 24 h as the source.

Every source is sampled at the centre of each Snowmapper cell; only cells the
SLF map covers (its data mask = Switzerland) count, so both comparisons use
one area. Statistics, on the cells and once more on 10 km blocks (a fairer
scale for a ~7 km forecast model):
  bias, MAE, RMSE, Pearson r, Spearman rho, share within ±2 cm / ±50 %,
  detection scores for >=1 / 5 / 10 / 20 cm (POD, FAR, CSI, frequency bias),
  agreement of the snow-scale classes, and everything again per elevation
  band (cell height) and per region of Switzerland.

    python tools/compare_newsnow.py --out report_dir [--pg-day DAY048]
        [--pg-run 20261117] [--hn-date 2026-11-17] [--site URL]

Writes report.md, report.json and maps.png into the output folder.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import io
import json
import math
from pathlib import Path

import numpy as np
import requests
from PIL import Image

SITE = "https://gianio.github.io/snow-mapper-v17/"
PG_TILES = "https://v2.tiles.powderguide.com/?p="
SLF_HN = "https://snow-maps-hs.slf.ch/public/hn/map/{period}/{date}/encoded"
# a plain client name: nothing in the requests says who runs the comparison
UA = {"User-Agent": "Mozilla/5.0 (compatible; map-compare/1.0)"}
R_EARTH = 6378137.0
CLASSES = [0, 1, 5, 10, 20, 30, 50, 1e9]          # snow-scale bands for the class agreement
THRESH = [1, 5, 10, 20]


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=60, **kw)
    r.raise_for_status()
    return r


# ── Snowmapper ──────────────────────────────────────────────────────────────
def load_snowmapper(site=SITE, path=None):
    if path:  # a downloaded snowdata-<version>.json.gz
        lj = {"version": Path(path).stem.split("-")[-1].split(".")[0], "generated": None}
        raw = gzip.decompress(Path(path).read_bytes())
    else:
        lj = get(site + "data/latest.json", params={"t": int(dt.datetime.now().timestamp())}).json()
        raw = gzip.decompress(get(site + "data/" + lj["gz"]).content)
    j = json.loads(raw)
    m, b = j["meta"], j["b"]
    W, H, T = m["width"], m["height"], m["T"]
    # what the app shows: the snowfall after a day of settling (meta.ns_settle)
    settle = float(m.get("ns_settle") or 1.0)
    snow = np.frombuffer(base64.b64decode(b["SNOW"]), np.uint8)[:T * W * H].reshape(T, H, W).astype(np.float32) * m["snow_scale"] * settle
    elev = np.frombuffer(base64.b64decode(b["MELEV"]), np.uint8)[:W * H].reshape(H, W).astype(np.float32) * m.get("elev_scale", 20)
    times = [dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc) for t in m["times"]]
    la0, lo0, la1, lo1 = m["bounds"]
    lat = la1 - (np.arange(H) + .5) * (la1 - la0) / H
    lon = lo0 + (np.arange(W) + .5) * (lo1 - lo0) / W
    return {"snow": snow, "elev": elev, "times": times, "lat": lat, "lon": lon, "version": lj.get("version"),
            "generated": lj.get("generated"), "settle": settle}


def sm_window(sm, t0, t1):
    """New snow per cell from t0 (incl.) to t1 (excl.), cm."""
    idx = [i for i, t in enumerate(sm["times"]) if t0 <= t < t1]
    if len(idx) < (t1 - t0).total_seconds() / 3600 * .9:
        raise SystemExit(f"Snowmapper data does not cover {t0:%d.%m. %H:%M} – {t1:%d.%m. %H:%M} UTC")
    return sm["snow"][idx].sum(0)


# ── web mercator helpers ────────────────────────────────────────────────────
def merc(lon, lat):
    x = np.radians(lon) * R_EARTH
    y = np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * R_EARTH
    return x, y


# ── WhiteRisk / SLF measured new snow ───────────────────────────────────────
def load_slf(date, period="HN1D"):
    r = get(SLF_HN.format(period=period, date=date))
    ext = json.loads(r.headers["ch.slf.snow-maps.extent"])
    valid = dt.datetime.fromisoformat(r.headers["ch.slf.snow-maps.valid-at"].replace("Z", "+00:00"))
    img = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB")).astype(np.float32)
    # as the site's shader: B = data present (>= 0.5), R = value on the
    # colour scale's range (first step 1 cm .. last step 100 cm)
    cs = get("https://snow-maps-hs.slf.ch/public/hn/map/colorscale").json()
    lo, hi = cs["steps"][0]["start"], cs["steps"][-1]["start"]
    present = img[..., 2] >= 128
    val = np.where(img[..., 0] > 0, lo + img[..., 0] / 255 * (hi - lo), 0.0)
    return {"val": np.where(present, val, np.nan), "extent": ext, "valid": valid,
            "hours": {"HN1D": 24, "HN2D": 48, "HN3D": 72}[period]}


def sample_slf(slf, lat, lon):
    x0, y0, x1, y1 = slf["extent"]
    h, w = slf["val"].shape
    X, Y = merc(*np.meshgrid(lon, lat))
    c = np.floor((X - x0) / (x1 - x0) * w).astype(int)
    r = np.floor((y1 - Y) / (y1 - y0) * h).astype(int)
    ok = (c >= 0) & (c < w) & (r >= 0) & (r < h)
    out = np.full(X.shape, np.nan, np.float32)
    out[ok] = slf["val"][r[ok], c[ok]]
    return out


# ── PowderGuide forecast (colour tiles) ─────────────────────────────────────
def load_pg_cbar():
    cb = get(PG_TILES + "cbars_en.json").json()["snow_gsp__lsfwe"]
    pts = [(c["r"], c["g"], c["b"], float(c["v"])) for c in cb if isinstance(c.get("v"), (int, float))]
    pts = [p for p in pts if p[:3] != (0, 0, 0)]
    return np.array([p[:3] for p in pts], np.float32), np.array([p[3] for p in pts], np.float32)


def pg_plot(run, day, region="eur"):
    plots = get(PG_TILES + "plots_snow_gsp__lsfwe.json").json()["plots"]
    for p in plots:
        if p["date"] == run and p["day"] == day and p["region"] == region:
            return p
    have = sorted({(p["date"], p["day"]) for p in plots if p["region"] == region})
    raise SystemExit(f"PowderGuide has no {region} plot for run {run} / {day}; available e.g. {have[-6:]}")


def load_pg(plot, lat, lon, z=6):
    cols, vals = load_pg_cbar()
    X, Y = merc(*np.meshgrid(lon, lat))
    n = 2 ** z
    size = 2 * math.pi * R_EARTH
    fx = (X + size / 2) / size * n
    fy = (size / 2 - Y) / size * n
    tiles = {}
    out = np.full(X.shape, np.nan, np.float32)
    for tx, ty in {(int(a), int(b)) for a, b in zip(fx.ravel(), fy.ravel())}:
        url = PG_TILES + plot["tileUrlPath"].format(z=z, x=tx, y=ty) + ".png"
        im = np.asarray(Image.open(io.BytesIO(get(url).content)).convert("RGBA")).astype(np.float32)
        tiles[(tx, ty)] = im
    for (tx, ty), im in tiles.items():
        sel = (fx.astype(int) == tx) & (fy.astype(int) == ty)
        px = np.clip(((fx[sel] - tx) * 256).astype(int), 0, 255)
        py = np.clip(((fy[sel] - ty) * 256).astype(int), 0, 255)
        rgba = im[py, px]
        v = np.zeros(len(px), np.float32)                      # transparent = below the scale (< 1 cm)
        a = rgba[:, 3] > 127
        if a.any():
            d = ((rgba[a, None, :3] - cols[None]) ** 2).sum(-1)
            v[a] = vals[d.argmin(1)]
        out[sel] = v
    return out


# ── statistics ──────────────────────────────────────────────────────────────
def rank(a):
    o = np.argsort(a, kind="stable")
    r = np.empty(len(a))
    r[o] = np.arange(len(a))
    return r


def stats(ref, sm):
    """ref = the outside source, sm = Snowmapper; 1-D arrays without NaN."""
    n = len(ref)
    if n < 10:
        return {"n": n}
    d = sm - ref
    s = {"n": int(n), "mean_ref": float(ref.mean()), "mean_sm": float(sm.mean()),
         "bias": float(d.mean()), "mae": float(np.abs(d).mean()), "rmse": float(np.sqrt((d ** 2).mean())),
         "within_2cm": float((np.abs(d) <= 2).mean()),
         "within_50pct": float((np.abs(d) <= np.maximum(1, .5 * ref)).mean())}
    s["pearson"] = float(np.corrcoef(ref, sm)[0, 1]) if ref.std() > 0 and sm.std() > 0 else None
    rr, rs = rank(ref), rank(sm)
    s["spearman"] = float(np.corrcoef(rr, rs)[0, 1]) if rr.std() > 0 and rs.std() > 0 else None
    det = {}
    for th in THRESH:
        o, f = ref >= th, sm >= th
        hit, miss, fa = int((o & f).sum()), int((o & ~f).sum()), int((~o & f).sum())
        det[th] = {"obs": int(o.sum()), "fc": int(f.sum()), "hits": hit, "misses": miss, "false": fa,
                   "pod": hit / (hit + miss) if hit + miss else None,
                   "far": fa / (hit + fa) if hit + fa else None,
                   "csi": hit / (hit + miss + fa) if hit + miss + fa else None,
                   "fbias": (hit + fa) / (hit + miss) if hit + miss else None}
    s["detect"] = det
    cr, cs = np.digitize(ref, CLASSES) - 1, np.digitize(sm, CLASSES) - 1
    s["class_exact"] = float((cr == cs).mean())
    s["class_pm1"] = float((np.abs(cr - cs) <= 1).mean())
    return s


def blocks(a, k):
    """Mean over k x k blocks (NaN-aware)."""
    H, W = a.shape
    h, w = H // k, W // k
    b = a[:h * k, :w * k].reshape(h, k, w, k)
    with np.errstate(all="ignore"):
        return np.nanmean(b, axis=(1, 3))


REGIONS = {  # rough parts of Switzerland by longitude / latitude of the cell
    "West (VD/FR/Unterwallis)": lambda la, lo: lo < 7.4,
    "Wallis": lambda la, lo: (lo >= 7.0) & (lo < 8.4) & (la < 46.45),
    "Nordalpen": lambda la, lo: (lo >= 7.4) & (lo < 9.5) & (la >= 46.6),
    "Zentral / Tessin": lambda la, lo: (lo >= 8.4) & (lo < 9.4) & (la < 46.6),
    "Graubünden": lambda la, lo: lo >= 9.4,
}
ELEV_BANDS = [(0, 1000), (1000, 1500), (1500, 2000), (2000, 2500), (2500, 5000)]


def compare(ref, sm, elev, lat, lon, km_cell=1.0):
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    ok = np.isfinite(ref) & np.isfinite(sm)
    res = {"cells": stats(ref[ok], sm[ok])}
    k = max(1, round(10 / km_cell))
    rb, sb = blocks(np.where(ok, ref, np.nan), k), blocks(np.where(ok, sm, np.nan), k)
    okb = np.isfinite(rb) & np.isfinite(sb)
    res["blocks_10km"] = stats(rb[okb], sb[okb])
    res["elev"] = {f"{a}–{b} m": stats(ref[ok & (elev >= a) & (elev < b)], sm[ok & (elev >= a) & (elev < b)]) for a, b in ELEV_BANDS}
    res["region"] = {name: stats(ref[ok & f(LAT, LON)], sm[ok & f(LAT, LON)]) for name, f in REGIONS.items()}
    return res


# ── report ─────────────────────────────────────────────────────────────────
def fmt(v, p=1, pct=False):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    return f"{v * 100:.0f} %" if pct else f"{v:.{p}f}"


def section(title, what, res, ref_name):
    c, b = res["cells"], res["blocks_10km"]
    L = [f"## {title}", "", what, ""]
    if c.get("n", 0) < 10:
        return L + ["Zu wenige gemeinsame Zellen für eine Auswertung.", ""]
    L += ["| Kennzahl | Zellen (1 km) | Blöcke (10 km) |", "|---|---|---|",
          f"| Anzahl | {c['n']} | {b.get('n', '–')} |",
          f"| Mittel {ref_name} [cm] | {fmt(c['mean_ref'])} | {fmt(b.get('mean_ref'))} |",
          f"| Mittel Snowmapper [cm] | {fmt(c['mean_sm'])} | {fmt(b.get('mean_sm'))} |",
          f"| Bias (Snowmapper − {ref_name}) [cm] | {fmt(c['bias'])} | {fmt(b.get('bias'))} |",
          f"| MAE [cm] | {fmt(c['mae'])} | {fmt(b.get('mae'))} |",
          f"| RMSE [cm] | {fmt(c['rmse'])} | {fmt(b.get('rmse'))} |",
          f"| Pearson r | {fmt(c['pearson'], 2)} | {fmt(b.get('pearson'), 2)} |",
          f"| Spearman ρ | {fmt(c['spearman'], 2)} | {fmt(b.get('spearman'), 2)} |",
          f"| innerhalb ±2 cm | {fmt(c['within_2cm'], pct=True)} | {fmt(b.get('within_2cm'), pct=True)} |",
          f"| innerhalb ±50 % (min. 1 cm) | {fmt(c['within_50pct'], pct=True)} | {fmt(b.get('within_50pct'), pct=True)} |",
          f"| gleiche Klasse (0/1/5/10/20/30/50 cm) | {fmt(c['class_exact'], pct=True)} | {fmt(b.get('class_exact'), pct=True)} |",
          f"| Klasse ±1 | {fmt(c['class_pm1'], pct=True)} | {fmt(b.get('class_pm1'), pct=True)} |", "",
          "**Erkennung (Zellen):** Trefferquote POD, Fehlalarme FAR, CSI, Frequenz-Bias (>1 = Snowmapper zeigt die Schwelle öfter)", "",
          "| Schwelle | beob./Quelle | Snowmapper | POD | FAR | CSI | Freq.-Bias |", "|---|---|---|---|---|---|---|"]
    for th, d in c["detect"].items():
        L.append(f"| ≥ {th} cm | {d['obs']} | {d['fc']} | {fmt(d['pod'], pct=True)} | {fmt(d['far'], pct=True)} | {fmt(d['csi'], 2)} | {fmt(d['fbias'], 2)} |")
    L += ["", "**Nach Höhe der Zelle**", "", f"| Höhe | n | Mittel {ref_name} | Mittel Snowmapper | Bias | MAE | r |", "|---|---|---|---|---|---|---|"]
    for k, s in res["elev"].items():
        if s.get("n", 0) >= 10:
            L.append(f"| {k} | {s['n']} | {fmt(s['mean_ref'])} | {fmt(s['mean_sm'])} | {fmt(s['bias'])} | {fmt(s['mae'])} | {fmt(s['pearson'], 2)} |")
    L += ["", "**Nach Region**", "", f"| Region | n | Mittel {ref_name} | Mittel Snowmapper | Bias | MAE | r |", "|---|---|---|---|---|---|---|"]
    for k, s in res["region"].items():
        if s.get("n", 0) >= 10:
            L.append(f"| {k} | {s['n']} | {fmt(s['mean_ref'])} | {fmt(s['mean_sm'])} | {fmt(s['bias'])} | {fmt(s['mae'])} | {fmt(s['pearson'], 2)} |")
    return L + [""]


def maps_png(path, panels, lat, lon):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    cols = ['#93DFF0', '#62C8EE', '#3BA8E8', '#2A86DE', '#3563CF', '#5446BC', '#6631A3', '#741F86']
    bounds = [1, 5, 10, 15, 20, 30, 40, 50, 75]
    cm, nm = ListedColormap(cols), BoundaryNorm(bounds, len(cols))
    cm.set_under("#F4F6F8")
    cm.set_over('#640C44')
    fig, axs = plt.subplots(len(panels), 3, figsize=(15, 3.6 * len(panels)), squeeze=False)
    ext = [lon[0], lon[-1], lat[-1], lat[0]]
    for i, (title, ref, sm) in enumerate(panels):
        for j, (t, a) in enumerate([(title, ref), ("Snowmapper", sm)]):
            ax = axs[i][j]
            im = ax.imshow(np.ma.masked_invalid(a), extent=ext, cmap=cm, norm=nm, aspect=1.45)
            ax.set_title(t, fontsize=10)
            fig.colorbar(im, ax=ax, shrink=.75, extend="both", label="cm")
        ax = axs[i][2]
        d = sm - ref
        im = ax.imshow(np.ma.masked_invalid(d), extent=ext, cmap="RdBu", vmin=-15, vmax=15, aspect=1.45)
        ax.set_title("Differenz Snowmapper − " + title.split(" ")[0], fontsize=10)
        fig.colorbar(im, ax=ax, shrink=.75, label="cm")
    fig.tight_layout()
    fig.savefig(path, dpi=110)


PG_MAP = ("https://map.powderguide.com/en/?b=&c%5Blng%5D=8.2&c%5Blat%5D=46.7&date={run}&day={day}"
          "&maskStr=nomask&model=&p=0&poi=cReports&poiTime=30days&t=&varName=snow_gsp__lsfwe&z=7")
WR_MAP = "https://whiterisk.ch/de/conditions/snow-maps/new_snow"
HIST_COLS = ["date", "source", "window_start", "window_end", "n", "mean_ref", "mean_sm", "bias", "mae", "rmse",
             "pearson_10km", "csi1", "csi5", "csi10", "fbias5", "sm_version", "ns_settle"]


def hist_rows(date, name, res, window, sm):
    c, b = res["cells"], res["blocks_10km"]
    if c.get("n", 0) < 10:
        return []
    d = c["detect"]
    return [[date, name, window[0], window[1], c["n"], *(round(c[k], 2) for k in ("mean_ref", "mean_sm", "bias", "mae", "rmse")),
             round(b["pearson"], 3) if b.get("pearson") is not None else "",
             *(round(d[t]["csi"], 3) if d[t]["csi"] is not None else "" for t in (1, 5, 10)),
             round(d[5]["fbias"], 2) if d[5]["fbias"] is not None else "", sm["version"], sm["settle"]]]


def hist_update(path, rows):
    """Append today's rows (replacing any earlier rows of the same date and
    source), return all rows for the running summary."""
    import csv
    p = Path(path)
    old = []
    if p.exists():
        with p.open(newline="") as f:
            old = [r for r in csv.reader(f)][1:]
    keys = {(r[0], r[1]) for r in rows}
    allr = [r for r in old if (r[0], r[1]) not in keys] + [[str(x) for x in r] for r in rows]
    allr.sort(key=lambda r: (r[0], r[1]))
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HIST_COLS)
        w.writerows(allr)
    return allr


def hist_section(allr):
    L = ["## Verlauf (alle bisherigen Tage)", "",
         "| Quelle | Tage | Ø Quelle [cm] | Ø Snowmapper [cm] | Ø Bias | Ø MAE | Ø r (10 km) | Ø CSI ≥1 | Ø CSI ≥5 | Ø Freq.-Bias ≥5 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    i = {c: k for k, c in enumerate(HIST_COLS)}
    for src in sorted({r[1] for r in allr}):
        R = [r for r in allr if r[1] == src]
        def avg(col):
            v = [float(r[i[col]]) for r in R if r[i[col]] not in ("", "None")]
            return sum(v) / len(v) if v else None
        L.append(f"| {src} | {len(R)} | {fmt(avg('mean_ref'))} | {fmt(avg('mean_sm'))} | {fmt(avg('bias'))} | {fmt(avg('mae'))} | "
                 f"{fmt(avg('pearson_10km'), 2)} | {fmt(avg('csi1'), 2)} | {fmt(avg('csi5'), 2)} | {fmt(avg('fbias5'), 2)} |")
    L += ["", "Ein Tag ist Zufall; erst über viele Tage (verschiedene Wetterlagen) wird der Bias belastbar genug, "
          "um das Modell zu kalibrieren (SLR, Höhenzuschlag, Faktor auf den Niederschlag).", ""]
    return L


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="compare_out")
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--sm-file", help="read Snowmapper from a local snowdata-*.json.gz instead of the site")
    ap.add_argument("--pg-run", help="PowderGuide run date YYYYMMDD (default: the newest)")
    ap.add_argument("--pg-day", default="auto",
                    help="PowderGuide 24 h field, e.g. DAY048 (= day after the run day); auto = tomorrow (UTC) from the chosen run")
    ap.add_argument("--hn-date", help="SLF HN1D map date YYYY-MM-DD (default: today)")
    ap.add_argument("--history", help="CSV to append the day's key numbers to (and summarise in the report)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    sm = load_snowmapper(a.site, a.sm_file)
    lat, lon = sm["lat"], sm["lon"]
    report = {"snowmapper": {"version": sm["version"], "generated": sm["generated"]}}

    # SLF measured new snow (also gives the Swiss mask for both comparisons)
    hn_date = a.hn_date or dt.date.today().isoformat()
    slf = load_slf(hn_date)
    t1 = slf["valid"]
    t0 = t1 - dt.timedelta(hours=slf["hours"])
    slf_g = sample_slf(slf, lat, lon)
    mask = np.isfinite(slf_g)
    sm_slf = sm_window(sm, t0, t1)
    res_slf = compare(slf_g, np.where(mask, sm_slf, np.nan), sm["elev"], lat, lon)
    report["whiterisk"] = {"window": [t0.isoformat(), t1.isoformat()], **res_slf}

    # PowderGuide forecast for the next day
    plots = get(PG_TILES + "plots_snow_gsp__lsfwe.json").json()["plots"]
    run = a.pg_run or max(p["date"] for p in plots if p["region"] == "eur")
    if a.pg_day == "auto":
        r_d = dt.datetime.strptime(run, "%Y%m%d").date()
        lead = ((dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=2)) - r_d).days * 24
        a.pg_day = f"DAY{min(120, max(48, lead)):03d}"
    plot = pg_plot(run, a.pg_day)
    lead = int("".join(ch for ch in a.pg_day if ch.isdigit()))
    r0 = dt.datetime.strptime(run, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    p1 = r0 + dt.timedelta(hours=lead)
    p0 = p1 - dt.timedelta(hours=24)
    pg = load_pg(plot, lat, lon)
    sm_pg = sm_window(sm, p0, p1)
    res_pg = compare(np.where(mask, pg, np.nan), np.where(mask, sm_pg, np.nan), sm["elev"], lat, lon)
    report["powderguide"] = {"run": run, "field": a.pg_day, "model": plot["model"], "window": [p0.isoformat(), p1.isoformat()], **res_pg}

    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    maps_png(out / "maps.png", [(f"PowderGuide {plot['model']} {p0:%d.%m.} (Prognose)", np.where(mask, pg, np.nan), np.where(mask, sm_pg, np.nan)),
                                (f"SLF/WhiteRisk HN1D bis {t1:%d.%m. %H} UTC (gemessen)", slf_g, np.where(mask, sm_slf, np.nan))], lat, lon)
    L = ["# Neuschnee-Vergleich Snowmapper", "",
         f"Erstellt {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · Snowmapper-Lauf {sm['version']} (generiert {sm['generated']})", "",
         "Gebiet: alle 1-km-Zellen von Snowmapper, die die SLF-Neuschneekarte abdeckt (= Schweiz). Jede Quelle wird in der Mitte "
         "jeder Zelle abgelesen. Zusätzlich auf 10-km-Blöcken gemittelt, weil ein Modell mit ~7 km Maschenweite einzelne 1-km-Zellen nicht auflösen kann.", ""]
    L += section(f"1 · PowderGuide – Prognose für den {p0:%d.%m.%Y}",
                 f"PowderGuide {plot['model'].upper()}, Lauf {run} 00 UTC, Feld `{a.pg_day}` = Neuschnee-Summe {p0:%d.%m. %H}–{p1:%d.%m. %H} UTC, "
                 "Annahme 1 mm Niederschlag als Schnee = 1 cm (Dichte 100 kg/m³). Aus den Farbkacheln (Zoom 6, ~1.7 km/Pixel) über die Farbskala "
                 "zurückgerechnet; < 1 cm ist dort transparent und zählt als 0. Snowmapper: Summe der Stundenwerte im gleichen Zeitraum.",
                 res_pg, "PowderGuide")
    L += section(f"2 · WhiteRisk / SLF – gemessener Neuschnee bis {t1:%d.%m.%Y %H:%M} UTC",
                 f"SLF-Neuschneekarte HN1D (gültig {t1:%d.%m. %H:%M} UTC, 24 h aus dem Messnetz interpoliert), Werte aus dem codierten Bild der Seite "
                 f"(Rot = Wert auf der Skala 1–100 cm, Blau = Daten vorhanden). Snowmapper: Summe {t0:%d.%m. %H}–{t1:%d.%m. %H} UTC "
                 "(Rückblick = MeteoSchweiz-Archiv, kein Vorhersagetest).",
                 res_slf, "SLF")
    L += ["## So siehst du dasselbe bei den Quellen", "",
          f"- **PowderGuide:** [Karte öffnen]({PG_MAP.format(run=run, day=a.pg_day)}) – Variable «Neuschnee» (`snow_gsp__lsfwe`), "
          f"Modell ICON-EU, Lauf {r0:%d.%m.%Y}, Zeitraum `{a.pg_day}` = 24 h bis {p1:%d.%m. %H} UTC, keine Maske. "
          f"Falls die Seite den Zeitraum nicht übernimmt: im Zeitregler den {p0:%d.%m.} mit «24 h» wählen.",
          f"- **WhiteRisk:** [{WR_MAP}]({WR_MAP}) – Neuschnee, Zeitraum «1 Tag», Datum {t1:%d.%m.%Y} "
          f"(gemessen bis {t1:%H:%M} UTC). Das ausgewertete Bild: {SLF_HN.format(period='HN1D', date=hn_date)}",
          f"- **Snowmapper:** Ebene Neuschnee, Zeitraum wie oben; Werte nach 24 h Setzung (Faktor {sm['settle']:.3f}).",
          ""]
    if a.history:
        rows = hist_rows(hn_date, "PowderGuide", res_pg, (p0.isoformat(), p1.isoformat()), sm) \
            + hist_rows(hn_date, "SLF", res_slf, (t0.isoformat(), t1.isoformat()), sm)
        L += hist_section(hist_update(a.history, rows))
    L += ["## Hinweise zur Deutung", "",
          "- Bias > 0: Snowmapper zeigt mehr Neuschnee als die Quelle.",
          "- PowderGuide rechnet 1 mm = 1 cm (10:1); Snowmapper rechnet mit einer temperaturabhängigen Neuschneedichte "
          "(bei Kälte mehr, bei Nässe weniger cm pro mm). Ein Teil der Abweichung ist also Definitionssache, nicht Modellfehler.",
          "- Die SLF-Karte ist gemessen und flächig interpoliert; über 2500 m und abseits der Stationen ist sie selbst unsicher.",
          "- Bei wenig Schnee im ganzen Gebiet sind r und ρ kaum aussagekräftig – dann zählen MAE und die Erkennung der Schwellen.",
          "", "Karten: `maps.png` · Rohzahlen: `report.json`", ""]
    (out / "report.md").write_text("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    main()
