"""Drift of the season-long snowpack against the IMIS stations.

What "drift" means here
-----------------------
The live snowpack is integrated through the whole season and never reset
to measurements (state.py). Any systematic error in the forcing or the
model -- too much precipitation, settling too slowly, melting too early --
therefore accumulates instead of washing out. That accumulation is the
drift. Day-to-day scatter at single stations is not drift; drift is an
error that

  1. most stations share (same sign),
  2. is large compared with the snow that is there, and/or
  3. keeps growing.

Measured: snow height HS at the SLF IMIS stations, daily means, against the
model's HS at the station (flat run of the station's weather point,
interpolated to the station's elevation -- imis.model_hs_at). Only station-
days with snow on at least one side (>= SNOW_MIN_CM) count; agreeing on
bare ground says nothing.

Per evaluation, over the last WINDOW_DAYS days with data:

  B   bias     median over stations of each station's mean error
               (model - measured) [cm]
  rB  relative B / max(REL_FLOOR_CM, median measured HS)
  A   agreement share of stations whose mean error has the sign of B and
               is at least AGREE_MIN_CM
  T   trend    least-squares slope of the network's daily median error
               over the last TREND_DAYS days [cm/day]

Status (the worse of the held-out stations and all stations):

  DRIFT  |B| >= 20 cm and |rB| >= 25 % and A >= 60 %,
         or |T| >= 1.5 cm/day with |B| >= 10 cm and T, B the same sign
  WATCH  |B| >= 10 cm and |rB| >= 10 % and A >= 55 %,
         or |T| >= 0.75 cm/day with |B| >= 5 cm and T, B the same sign
  OK     otherwise
  NODATA fewer than MIN_STATIONS stations with snow (early season)

Held-out stations: every 4th station (imis.is_holdout) never feeds the
precipitation correction. The others are what the model is nudged towards,
so their error is pulled to zero by construction and would hide drift; the
held-out ones are the independent check.

The IMIS API only serves the last 7 days, so the season's history is built
up cycle by cycle in HIST_FILE, which travels with the carried state.
"""
from __future__ import annotations
import html
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

HIST_FILE = "drift_hist.json"
VERSION = 1

SNOW_MIN_CM = 5.0
MIN_PAIRS_PER_DAY = 4          # of 12 two-hourly frames
WINDOW_DAYS = 7
MIN_DAYS_PER_STATION = 3
TREND_DAYS = 21
MIN_TREND_DAYS = 10
MIN_STATIONS = {"independent": 6, "all": 8}
REL_FLOOR_CM = 20.0
AGREE_MIN_CM = 5.0

DRIFT = {"bias": 20.0, "rel": 0.25, "agree": 0.60, "trend": 1.5, "trend_bias": 10.0}
WATCH = {"bias": 10.0, "rel": 0.10, "agree": 0.55, "trend": 0.75, "trend_bias": 5.0}
ORDER = {"NODATA": 0, "OK": 1, "WATCH": 2, "DRIFT": 3}


# ── history ─────────────────────────────────────────────────────────────────

def load(state_dir, season):
    f = Path(state_dir) / HIST_FILE if state_dir else None
    if f is not None and f.exists():
        try:
            h = json.loads(f.read_text())
            if h.get("version") == VERSION and h.get("season") == season:
                return h
        except Exception:
            pass
    return {"version": VERSION, "season": season, "stations": {}}


def save(path, hist):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hist, separators=(",", ":")))
    return path


def update(hist, collected):
    """Fold one cycle's hourly pairs ({code: {..., pairs: [(t, meas, mod)]}})
    into daily means. A day already in the history is replaced when this
    cycle saw at least as many hours of it (the newer run had the better
    state)."""
    st = hist.setdefault("stations", {})
    n_days = 0
    for code, c in collected.items():
        days = {}
        for t, meas, mod in c.get("pairs") or ():
            d = t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t)[:10]
            days.setdefault(d, []).append((float(meas), float(mod)))
        rec = st.setdefault(code, {"days": {}})
        for k in ("label", "elev", "wp", "tile", "holdout"):
            if c.get(k) is not None:
                rec[k] = c[k]
        for d, ps in days.items():
            if len(ps) < MIN_PAIRS_PER_DAY:
                continue
            old = rec["days"].get(d)
            if old is None or len(ps) >= old[2]:
                rec["days"][d] = [round(sum(p[0] for p in ps) / len(ps), 1),
                                  round(sum(p[1] for p in ps) / len(ps), 1), len(ps)]
                n_days += 1
    hist["updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
    return n_days


# ── evaluation ──────────────────────────────────────────────────────────────

def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def _quant(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _slope(ys):
    """Least-squares slope per index step of [(x, y)]."""
    n = len(ys)
    if n < 2:
        return None
    mx = sum(x for x, _ in ys) / n
    my = sum(y for _, y in ys) / n
    sxx = sum((x - mx) ** 2 for x, _ in ys)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in ys) / sxx


def _sign(x):
    return (x > 0) - (x < 0)


def daily_series(hist, subset="all"):
    """[(date, {median, p25, p75, n, meas, model})] over the season."""
    by_day = {}
    for code, rec in (hist.get("stations") or {}).items():
        if subset == "independent" and not rec.get("holdout"):
            continue
        for d, (meas, mod, _n) in (rec.get("days") or {}).items():
            if max(meas, mod) < SNOW_MIN_CM:
                continue
            by_day.setdefault(d, []).append((mod - meas, meas, mod))
    out = []
    for d in sorted(by_day):
        v = by_day[d]
        errs = [e for e, _, _ in v]
        out.append((d, {"median": round(_median(errs), 1), "p25": round(_quant(errs, 0.25), 1),
                        "p75": round(_quant(errs, 0.75), 1), "n": len(v),
                        "meas": round(_median([m for _, m, _ in v]), 1),
                        "model": round(_median([m for _, _, m in v]), 1)}))
    return out


def _classify(B, rB, A, T):
    reasons = []
    for name, th in (("DRIFT", DRIFT), ("WATCH", WATCH)):
        level = abs(B) >= th["bias"] and abs(rB) >= th["rel"] and A >= th["agree"]
        trend = (T is not None and abs(T) >= th["trend"] and abs(B) >= th["trend_bias"]
                 and _sign(T) == _sign(B))
        if level:
            reasons.append(f"bias {B:+.0f} cm ({rB:+.0%} of the snow), "
                           f"{A:.0%} of stations agree")
        if trend:
            reasons.append(f"error growing {T:+.2f} cm/day over {TREND_DAYS} days")
        if level or trend:
            return name, reasons
    return "OK", reasons


def evaluate_subset(hist, subset, today=None):
    stations = hist.get("stations") or {}
    days_all = sorted({d for r in stations.values() for d in (r.get("days") or {})})
    if not days_all:
        return {"subset": subset, "status": "NODATA", "reasons": ["no station history yet"],
                "stations": 0}
    end = today or days_all[-1]
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    per = []
    for code, rec in stations.items():
        if subset == "independent" and not rec.get("holdout"):
            continue
        es, ms = [], []
        for d, (meas, mod, _n) in (rec.get("days") or {}).items():
            if start <= d <= end and max(meas, mod) >= SNOW_MIN_CM:
                es.append(mod - meas)
                ms.append(meas)
        if len(es) >= MIN_DAYS_PER_STATION:
            per.append({"code": code, "label": rec.get("label", ""), "elev": rec.get("elev"),
                        "tile": rec.get("tile"), "holdout": bool(rec.get("holdout")),
                        "err": round(sum(es) / len(es), 1), "meas": round(sum(ms) / len(ms), 1),
                        "days": len(es)})
    out = {"subset": subset, "window": [start, end], "stations": len(per)}
    if len(per) < MIN_STATIONS[subset]:
        out.update(status="NODATA",
                   reasons=[f"{len(per)} stations with snow in the last {WINDOW_DAYS} days "
                            f"(need {MIN_STATIONS[subset]})"])
        return out
    B = _median([p["err"] for p in per])
    med_meas = _median([p["meas"] for p in per])
    rB = B / max(REL_FLOOR_CM, med_meas)
    A = (sum(1 for p in per if _sign(p["err"]) == _sign(B) and abs(p["err"]) >= AGREE_MIN_CM)
         / len(per)) if B else 0.0
    series = daily_series(hist, subset)
    tail = [(datetime.strptime(d, "%Y-%m-%d"), v["median"]) for d, v in series
            if v["n"] >= max(3, MIN_STATIONS[subset] // 2)]
    t_end = datetime.strptime(end, "%Y-%m-%d")
    tail = [((d - t_end).days, y) for d, y in tail if 0 <= (t_end - d).days < TREND_DAYS]
    T = _slope(tail) if len(tail) >= MIN_TREND_DAYS else None
    status, reasons = _classify(B, rB, A, T)
    out.update(status=status, reasons=reasons, bias_cm=round(B, 1), rel_bias=round(rB, 3),
               agreement=round(A, 3), trend_cm_per_day=None if T is None else round(T, 2),
               trend_days=len(tail), median_measured_cm=round(med_meas, 1),
               mae_cm=round(sum(abs(p["err"]) for p in per) / len(per), 1),
               worst=sorted(per, key=lambda p: -abs(p["err"]))[:12])
    # where: the same rules per subregion (fewer stations needed)
    tiles = {}
    for p in per:
        tiles.setdefault(p["tile"], []).append(p)
    by_tile = {}
    for tile, ps in sorted(tiles.items(), key=lambda kv: str(kv[0])):
        if len(ps) < 3:
            continue
        b = _median([p["err"] for p in ps])
        r = b / max(REL_FLOOR_CM, _median([p["meas"] for p in ps]))
        a = (sum(1 for p in ps if _sign(p["err"]) == _sign(b) and abs(p["err"]) >= AGREE_MIN_CM)
             / len(ps)) if b else 0.0
        s, _ = _classify(b, r, a, None)
        by_tile[str(tile)] = {"status": s, "bias_cm": round(b, 1), "stations": len(ps)}
    out["by_subregion"] = by_tile
    return out


def evaluate(hist, today=None, full_season=True, origin=None):
    ind = evaluate_subset(hist, "independent", today)
    alls = evaluate_subset(hist, "all", today)
    worst = max((ind, alls), key=lambda r: ORDER[r["status"]])
    res = {"status": worst["status"], "season": hist.get("season"),
           "updated": hist.get("updated"), "independent": ind, "all": alls,
           "full_season": bool(full_season), "origin": origin,
           "definition": {"snow_min_cm": SNOW_MIN_CM, "window_days": WINDOW_DAYS,
                          "trend_days": TREND_DAYS, "drift": DRIFT, "watch": WATCH}}
    return res


# ── reporting ───────────────────────────────────────────────────────────────

LABEL = {"OK": "OK", "WATCH": "Beobachten", "DRIFT": "Drift", "NODATA": "Keine Aussage"}


def markdown(res):
    """For the GitHub job summary."""
    icon = {"OK": "🟢", "WATCH": "🟡", "DRIFT": "🔴", "NODATA": "⚪"}
    lines = [f"### Snowpack drift check: {icon[res['status']]} {res['status']}",
             f"Season {res.get('season')}, history updated {res.get('updated')}"
             + ("" if res.get("full_season") else
                " -- **not a full season** (some slopes started on a short spin-up; "
                "run the *Variant A season replay* workflow)"), "",
             "| stations | status | n | bias | rel. | agree | trend | MAE | median HS |",
             "|---|---|---|---|---|---|---|---|---|"]
    for key, name in (("independent", "held out (independent)"), ("all", "all")):
        r = res[key]
        if r["status"] == "NODATA":
            lines.append(f"| {name} | {r['status']} | {r.get('stations', 0)} | | | | | | |")
            continue
        tr = r.get("trend_cm_per_day")
        lines.append(f"| {name} | {r['status']} | {r['stations']} | {r['bias_cm']:+.1f} cm | "
                     f"{r['rel_bias']:+.0%} | {r['agreement']:.0%} | "
                     f"{'-' if tr is None else f'{tr:+.2f} cm/d'} | {r['mae_cm']:.1f} cm | "
                     f"{r['median_measured_cm']:.0f} cm |")
    reasons = res["independent"].get("reasons", []) + res["all"].get("reasons", [])
    if reasons:
        lines += [""] + [f"- {x}" for x in dict.fromkeys(reasons)]
    return "\n".join(lines) + "\n"


def _svg_chart(series_by, w=480, h=210, key="median", band=True, title=""):
    """Small dependency-free line chart. series_by: [(name, cls, [(date, v)])]."""
    pts = [p for _, _, s in series_by for p in s]
    if not pts:
        return '<p class="muted">Noch keine Daten.</p>'
    dates = sorted({d for d, _ in pts})
    d0 = datetime.strptime(dates[0], "%Y-%m-%d")
    d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
    span = max(1, (d1 - d0).days)
    vals = []
    for _, _, s in series_by:
        for _, v in s:
            vals.append(v[key])
            if band and "p25" in v:
                vals += [v["p25"], v["p75"]]
    if key == "median":
        vals += [-DRIFT["bias"], DRIFT["bias"]]
    lo, hi = min(vals + [0]), max(vals + [0])
    pad = (hi - lo) * 0.08 or 5
    lo, hi = lo - pad, hi + pad
    L, R, Tp, Bt = 44, 10, 10, 26

    def X(d):
        return L + (w - L - R) * (datetime.strptime(d, "%Y-%m-%d") - d0).days / span

    def Y(v):
        return Tp + (h - Tp - Bt) * (hi - v) / (hi - lo)

    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)}">']
    # gridlines + y labels
    step = 10 if hi - lo <= 120 else 25 if hi - lo <= 300 else 50
    v = math.ceil(lo / step) * step
    while v <= hi:
        g.append(f'<line class="grid" x1="{L}" x2="{w-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                 f'<text class="ax" x="{L-6}" y="{Y(v)+4:.1f}" text-anchor="end">{v:g}</text>')
        v += step
    if key == "median":
        for th, cls in ((WATCH["bias"], "th-w"), (DRIFT["bias"], "th-d")):
            for sgn in (1, -1):
                g.append(f'<line class="{cls}" x1="{L}" x2="{w-R}" y1="{Y(sgn*th):.1f}" '
                         f'y2="{Y(sgn*th):.1f}"/>')
        g.append(f'<line class="zero" x1="{L}" x2="{w-R}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    # x labels: ~6 dates
    for i in range(0, len(dates), max(1, len(dates) // 6)):
        d = dates[i]
        g.append(f'<text class="ax" x="{X(d):.1f}" y="{h-8}" text-anchor="middle">'
                 f'{d[8:10]}.{d[5:7]}.</text>')
    for name, cls, s in series_by:
        if band and s and "p25" in s[0][1]:
            up = " ".join(f"{X(d):.1f},{Y(v['p75']):.1f}" for d, v in s)
            dn = " ".join(f"{X(d):.1f},{Y(v['p25']):.1f}" for d, v in reversed(s))
            g.append(f'<polygon class="band {cls}" points="{up} {dn}"/>')
        path = " ".join(f"{X(d):.1f},{Y(v[key]):.1f}" for d, v in s)
        g.append(f'<polyline class="ln {cls}" points="{path}"><title>{html.escape(name)}</title></polyline>')
        if len(s) == 1:
            d, v = s[0]
            g.append(f'<circle class="pt {cls}" cx="{X(d):.1f}" cy="{Y(v[key]):.1f}" r="3"/>')
    g.append("</svg>")
    return "".join(g)


def render_html(res, hist):
    """drift.html: one self-contained page (published with the live export)."""
    ind = daily_series(hist, "independent")
    alls = daily_series(hist, "all")
    err_chart = _svg_chart([("Gehaltene Stationen (unabhängig)", "s-ind", ind),
                            ("Alle Stationen", "s-all", alls)],
                           title="Fehler Modell minus Messung")
    hs_chart = _svg_chart([("Gemessen (Median)", "s-meas", [(d, {"v": v["meas"]}) for d, v in alls]),
                           ("Modell (Median)", "s-mod", [(d, {"v": v["model"]}) for d, v in alls])],
                          key="v", band=False, title="Schneehöhe")
    st = res["status"]
    rows = []
    for key, name in (("independent", "Gehalten (unabhängig)"), ("all", "Alle")):
        r = res[key]
        if r["status"] == "NODATA":
            rows.append(f"<tr><td>{name}</td><td><span class='b {r['status']}'>{LABEL[r['status']]}</span></td>"
                        f"<td>{r.get('stations', 0)}</td><td colspan=5 class=muted>"
                        f"{html.escape('; '.join(r.get('reasons', [])))}</td></tr>")
            continue
        tr = r.get("trend_cm_per_day")
        rows.append(f"<tr><td>{name}</td><td><span class='b {r['status']}'>{LABEL[r['status']]}</span></td>"
                    f"<td>{r['stations']}</td><td>{r['bias_cm']:+.1f} cm</td><td>{r['rel_bias']:+.0%}</td>"
                    f"<td>{r['agreement']:.0%}</td><td>{'–' if tr is None else f'{tr:+.2f} cm/Tag'}</td>"
                    f"<td>{r['mae_cm']:.1f} cm</td></tr>")
    worst = (res["all"].get("worst") or [])
    wrows = "".join(
        f"<tr><td class=nm>{html.escape(p.get('label') or p['code'])}</td>"
        f"<td>{p.get('elev') or ''} m</td><td>{p['meas']:.0f} cm</td><td>{p['err']:+.0f} cm</td>"
        f"<td>{'ja' if p['holdout'] else ''}</td></tr>" for p in worst)
    tiles = res["all"].get("by_subregion") or {}
    trows = "".join(f"<tr><td>{html.escape(k)}</td><td><span class='b {v['status']}'>{LABEL[v['status']]}"
                    f"</span></td><td>{v['stations']}</td><td>{v['bias_cm']:+.1f} cm</td></tr>"
                    for k, v in tiles.items())
    reasons = list(dict.fromkeys(res["independent"].get("reasons", []) + res["all"].get("reasons", [])))
    origin = res.get("origin") or {}
    season_note = ("Durchgehende Saison: jeder virtuelle Hang ist seit dem Saisonstart "
                   "(oder länger) ohne Neustart gerechnet."
                   if res.get("full_season") else
                   "Achtung: Ein Teil der Hänge wurde mitten in der Saison mit kurzer "
                   "Einschwingzeit neu gestartet. Der Workflow „Variant A season replay“ "
                   "rechnet die ganze Saison nach.")
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Snowpack Drift</title>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--fg:#1d2330;--muted:#677084;--grid:#e3e6ec;
--ind:#2563eb;--all:#8a94a8;--meas:#0f766e;--mod:#b45309;--ok:#15803d;--watch:#b45309;--drift:#b91c1c;--nod:#677084}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#11141a;--card:#1a1f28;--fg:#e8ebf1;
--muted:#9aa3b5;--grid:#2a303c;--ind:#6ea0ff;--all:#7d879a;--meas:#3fbfb0;--mod:#f0a54a;--ok:#4ade80;--watch:#fbbf24;--drift:#f87171;--nod:#9aa3b5}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:820px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 10px}}
.card{{background:var(--card);border-radius:12px;padding:16px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.06)}}
.muted{{color:var(--muted)}} .big{{font-size:28px;font-weight:700}}
.b{{display:inline-block;padding:2px 9px;border-radius:99px;font-size:13px;font-weight:600;color:#fff}}
.b.OK{{background:var(--ok)}} .b.WATCH{{background:var(--watch)}} .b.DRIFT{{background:var(--drift)}} .b.NODATA{{background:var(--nod)}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{text-align:left;padding:6px 6px;border-bottom:1px solid var(--grid);white-space:nowrap}}
td.nm{{white-space:normal}}
th{{color:var(--muted);font-weight:600}} .tw{{overflow-x:auto}}
svg{{width:100%;height:auto;display:block}} .grid{{stroke:var(--grid)}} .zero{{stroke:var(--muted);stroke-width:1.2}}
.th-w{{stroke:var(--watch);stroke-dasharray:4 4;opacity:.6}} .th-d{{stroke:var(--drift);stroke-dasharray:4 4;opacity:.6}}
.ax{{fill:var(--muted);font-size:11px}} .ln{{fill:none;stroke-width:2.2}} .band{{opacity:.14;stroke:none}}
.s-ind{{stroke:var(--ind);fill:var(--ind)}} .s-all{{stroke:var(--all);fill:var(--all)}}
.s-meas{{stroke:var(--meas);fill:var(--meas)}} .s-mod{{stroke:var(--mod);fill:var(--mod)}}
.ln.s-ind,.ln.s-all,.ln.s-meas,.ln.s-mod{{fill:none}}
.key{{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-top:6px}}
.key i{{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}}
ul{{margin:6px 0 0 18px;padding:0}}
</style></head><body><main>
<h1>Snowpack-Drift</h1>
<div class="muted">Saison {html.escape(str(res.get('season')))} · aktualisiert {html.escape(str(res.get('updated')))} UTC</div>
<div class="card"><div class="big"><span class="b {st}" style="font-size:18px">{LABEL[st]}</span></div>
<p>{html.escape(season_note)}</p>
{('<ul>' + ''.join(f'<li>{html.escape(x)}</li>' for x in reasons) + '</ul>') if reasons else ''}
<div class="tw"><table><tr><th>Stationen</th><th>Status</th><th>n</th><th>Bias</th><th>relativ</th><th>einig</th><th>Trend</th><th>MAE</th></tr>
{''.join(rows)}</table></div></div>
<div class="card"><h2>Fehler über die Saison (Modell − Messung, Median, cm)</h2>
{err_chart}
<div class="key"><span><i style="background:var(--ind)"></i>gehaltene Stationen (unabhängig)</span>
<span><i style="background:var(--all)"></i>alle Stationen</span><span><i style="background:var(--watch)"></i>±{WATCH['bias']:g} cm beobachten</span>
<span><i style="background:var(--drift)"></i>±{DRIFT['bias']:g} cm Drift</span></div>
<p class="muted">Fläche: mittlere Hälfte der Stationen (25.–75. Perzentil). Nur Tage mit Schnee (≥ {SNOW_MIN_CM:g} cm gemessen oder modelliert).</p></div>
<div class="card"><h2>Schneehöhe an den Stationen (Median, cm)</h2>{hs_chart}
<div class="key"><span><i style="background:var(--meas)"></i>gemessen (IMIS)</span><span><i style="background:var(--mod)"></i>Modell</span></div></div>
<div class="card"><h2>Grösste Abweichungen, letzte {WINDOW_DAYS} Tage</h2><div class="tw"><table>
<tr><th>Station</th><th>Höhe</th><th>gemessen</th><th>Fehler</th><th>gehalten</th></tr>{wrows or '<tr><td colspan=5 class=muted>Keine Stationen mit Schnee.</td></tr>'}</table></div></div>
{f'<div class="card"><h2>Nach Teilregion</h2><div class="tw"><table><tr><th>Region</th><th>Status</th><th>n</th><th>Bias</th></tr>{trows}</table></div></div>' if trows else ''}
<div class="card"><h2>Was heisst Drift?</h2>
<p>Der Schneedeckenzustand wird über die ganze Saison weitergerechnet und nie auf Messungen zurückgesetzt. Ein systematischer Fehler (zu viel Niederschlag, zu langsames Setzen, zu frühes Schmelzen) sammelt sich deshalb an. Das ist Drift — nicht die tägliche Streuung einzelner Stationen.</p>
<p>Verglichen wird die gemessene Schneehöhe der SLF-IMIS-Stationen mit der modellierten auf gleicher Höhe, als Tagesmittel. Bewertet werden die letzten {WINDOW_DAYS} Tage:</p>
<ul><li><b>Drift</b>: Bias ≥ {DRIFT['bias']:g} cm und ≥ {DRIFT['rel']:.0%} der Schneehöhe, bei ≥ {DRIFT['agree']:.0%} der Stationen in die gleiche Richtung — oder der Fehler wächst ≥ {DRIFT['trend']:g} cm/Tag über {TREND_DAYS} Tage.</li>
<li><b>Beobachten</b>: Bias ≥ {WATCH['bias']:g} cm und ≥ {WATCH['rel']:.0%} bei ≥ {WATCH['agree']:.0%} — oder Wachstum ≥ {WATCH['trend']:g} cm/Tag.</li>
<li><b>Keine Aussage</b>: zu wenige Stationen mit Schnee (Saisonbeginn).</li></ul>
<p>Jede vierte Station wird nie zur Niederschlagskorrektur verwendet. Nur diese „gehaltenen“ Stationen sind ein unabhängiger Test; die übrigen zieht die Korrektur absichtlich gegen null.</p>
<p class="muted">Quelle Messungen: WSL-Institut für Schnee- und Lawinenforschung SLF, IMIS (CC BY 4.0).</p></div>
</main></body></html>"""
