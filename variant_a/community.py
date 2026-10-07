"""Community reports as a check on, and a gentle correction of, SNOWPACK.

A report is a measurement of unknown quality: someone stood on a slope and
said "15 cm of powder here, north-east, around 2400 m", or drew that onto the
map. IMIS stations are better instruments but stand on flat, sheltered
ground; reports come from steep slopes of every aspect, exactly where the
model has no other check. So reports are used the way the stations are used
(imis.py), only more carefully:

1. Export. The public reports of the last HOURS_BACK hours, read with the
   app's public (anon) key, so exactly what any visitor can see -- friends-only
   posts never enter the model.

2. Weight and filter. Each report weighs as in the app's "Gemeldetes Powder"
   layer: confirmations ("like") and the author's trust (likes received over
   all their reports) raise it, "stale" votes can drop it. A single author
   may not carry more than USER_SHARE_MAX of a weather point. Reports far off
   both the model and the other reports nearby (MAD test) are dropped.

3. Match. Every observation goes to the virtual slope of the matrix that
   fits it: nearest weather point within MATCH_KM, nearest height band,
   nearest slope class and aspect (the flat run if the report says nothing
   about either), at the last frame before the report.

4. Compare.
   * Powder depth [cm] vs the model's powder_depth_cm -> bias / MAE.
   * Categories (powder yes/no, no snow, wet/corn, crust, drift, scoured) vs
     the model's own classes -> hit rates per category. These calibrate the
     CLASSIFICATION thresholds, not the physics; a suggested powder
     threshold is reported, never applied automatically.

5. Correct. As for IMIS, a slow multiplicative step on the weather point's
   precipitation factor: geometric mean of (report + damp) / (model + damp),
   clipped per cycle to R_MIN..R_MAX and moved only by its STEP_EXP power
   (half the IMIS step: reports count for less than stations). It needs at
   least MIN_REPORTS reports from MIN_USERS different people.

6. Hold out. A fixed HOLDOUT share of reports (by report id) never feeds the
   correction. They estimate whether the step would have helped: the MAE of
   the model as it is vs the model scaled by the step. The step is only
   applied when COMMUNITY_APPLY=1 is set AND the holdout says it helps --
   otherwise it is computed and logged (shadow mode), so its effect can be
   watched over a few weeks before it touches the forecast.

The state is never overwritten with a report; only precipitation is nudged.
"""
from __future__ import annotations
import hashlib
import math
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HOURS_BACK = 72
MATCH_KM = 12.0
MATCH_DZ = 300.0            # report height may sit this far outside the bands
DAMP_CM = 10.0
STEP_EXP = 0.25             # IMIS uses 0.5
R_MIN, R_MAX = 0.8, 1.25
F_MIN, F_MAX = 0.5, 2.0
MIN_REPORTS = 3
MIN_USERS = 2
USER_SHARE_MAX = 0.5
HOLDOUT = 0.2
MIN_HOLDOUT = 5
OUTLIER_MAD = 3.0
END_MAX, END_HALF = 0.60, 3.0      # as the app's progReportWeight
TRUST_MAX, TRUST_HALF = 0.50, 8.0
POWDER_MIN_CM = 5.0
NOSNOW_MAX_CM = 5.0

# drawn zone type -> observation kind
ZONE_KIND = {"powder": "powder", "drift": "drift", "wet": "wet", "firn": "wet",
             "suncrust": "crust", "windpressed": "crust", "icy": "crust",
             "scoured": "scoured", "nosnow": "nosnow"}
ASPECT_DEG = {"N": 0, "NO": 45, "NE": 45, "O": 90, "E": 90, "SO": 135, "SE": 135, "S": 180,
              "SW": 225, "W": 270, "NW": 315}


@dataclass
class Obs:
    rid: str
    uid: str
    kind: str
    lat: float
    lon: float
    t: datetime
    elev: float | None = None
    aspect: float | None = None
    slope: float | None = None
    cm: float | None = None
    w: float = 1.0
    holdout: bool = False
    extra: dict = field(default_factory=dict)


# ── 1. export ────────────────────────────────────────────────────────────

def sb_config():
    """(url, anon key): environment first, else the app's own public constants."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY")
    if url and key:
        return url.rstrip("/"), key
    try:
        src = (Path(__file__).resolve().parent.parent / "pipeline" / "interactive_export.py").read_text()
        u = re.search(r"const SB_URL='([^']+)'", src).group(1)
        k = re.search(r"const SB_KEY='([^']+)'", src).group(1)
        return u.rstrip("/"), k
    except Exception:
        return None, None


def _get(sess, url, key, path, params):
    r = sess.get(f"{url}/rest/v1/{path}", params=params, timeout=30,
                 headers={"apikey": key, "Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.json()


def _chunks(xs, n=120):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def fetch(hours=HOURS_BACK, now=None):
    """(rows, likes, stale, trust) for the public reports of the last `hours`."""
    import requests
    url, key = sb_config()
    if not url or not key:
        raise RuntimeError("no Supabase URL / anon key")
    now = now or datetime.utcnow()
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = requests.Session()
    rows = _get(s, url, key, "reports", {
        "select": "id,user_id,location,elevation_m,subtype,primary_categories,condition_data,captured_at,created_at",
        "created_at": f"gte.{since}", "order": "created_at.desc", "limit": "2000"})
    ids = [r["id"] for r in rows]
    likes, stale = {}, {}
    for ch in _chunks(ids):
        for x in _get(s, url, key, "report_reactions", {
                "select": "report_id,type", "type": "in.(like,stale)",
                "report_id": f"in.({','.join(ch)})"}):
            d = likes if x["type"] == "like" else stale
            d[x["report_id"]] = d.get(x["report_id"], 0) + 1
    # trust: likes received over all of an author's reports
    uids = sorted({r["user_id"] for r in rows if r.get("user_id")})
    trust = {}
    if uids:
        own = []
        for ch in _chunks(uids):
            own += _get(s, url, key, "reports", {"select": "id,user_id",
                                                 "user_id": f"in.({','.join(ch)})", "limit": "5000"})
        by_id = {o["id"]: o["user_id"] for o in own}
        for ch in _chunks(list(by_id)):
            for x in _get(s, url, key, "report_reactions", {
                    "select": "report_id", "type": "eq.like", "report_id": f"in.({','.join(ch)})"}):
                u = by_id.get(x["report_id"])
                if u:
                    trust[u] = trust.get(u, 0) + 1
    return rows, likes, stale, trust


# ── 2. observations, weights ─────────────────────────────────────────────

def parse_geo(g):
    """[lat, lon] from GeoJSON, WKT or hex (E)WKB, else None."""
    if g is None:
        return None
    if isinstance(g, dict):
        c = g.get("coordinates")
        return [float(c[1]), float(c[0])] if c and len(c) >= 2 else None
    s = str(g)
    m = re.search(r"POINT\s*\(\s*([-\d.eE]+)\s+([-\d.eE]+)", s, re.I)
    if m:
        return [float(m.group(2)), float(m.group(1))]
    if re.fullmatch(r"[0-9A-Fa-f]+", s) and len(s) >= 42:
        b = bytes.fromhex(s)
        e = "<" if b[0] == 1 else ">"
        typ = struct.unpack(e + "I", b[1:5])[0]
        off = 5 + (4 if typ & 0x20000000 else 0)
        x, y = struct.unpack(e + "dd", b[off:off + 16])
        if math.isfinite(x) and math.isfinite(y) and abs(y) <= 90 and abs(x) <= 180:
            return [y, x]
    return None


def _ts(s):
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    s = re.sub(r"(\.\d{1,6})\d*", r"\1", s)
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def is_holdout(rid):
    return int(hashlib.sha1(str(rid).encode()).hexdigest()[:8], 16) % 1000 < HOLDOUT * 1000


def report_weight(n_likes, trust):
    n, t = max(0, n_likes or 0), max(0, trust or 0)
    return (1 + END_MAX * n / (n + END_HALF)) * (1 + TRUST_MAX * t / (t + TRUST_HALF))


def observations(rows, likes=None, stale=None, trust=None):
    """Reports -> [Obs]. Drawn maps give one observation per zone."""
    likes, stale, trust = likes or {}, stale or {}, trust or {}
    out = []
    for r in rows:
        cd = r.get("condition_data") or {}
        if cd.get("demoFit") or cd.get("activity") or cd.get("snowp"):
            continue
        rid, uid = str(r.get("id")), str(r.get("user_id") or "")
        nl, ns = likes.get(r.get("id"), 0), stale.get(r.get("id"), 0)
        if ns >= 2 and ns > nl:                       # the community says: outdated
            continue
        t = _ts(r.get("captured_at")) or _ts(r.get("created_at"))
        if t is None:
            continue
        w = report_weight(nl, trust.get(r.get("user_id"), 0))
        ho = is_holdout(rid)
        ll = parse_geo(r.get("location"))
        zones = cd.get("zones") or []
        if zones:
            for i, z in enumerate(zones):
                kind = ZONE_KIND.get((z or {}).get("type"))
                c = (z or {}).get("centroid") or ll
                if not kind or not c or c[0] is None:
                    continue
                e0, e1 = _num(z.get("elevMin")), _num(z.get("elevMax"))
                elev = (e0 + e1) / 2 if e0 is not None and e1 is not None else (e0 or e1)
                conc = _num(z.get("aspectConc"))
                out.append(Obs(rid=f"{rid}#{i}", uid=uid, kind=kind, lat=float(c[0]), lon=float(c[1]), t=t,
                               elev=elev,
                               aspect=_num(z.get("aspectDeg")) if conc is None or conc >= 0.5 else None,
                               slope=_num(z.get("slope")),
                               cm=_num(z.get("cm")) if kind in ("powder", "drift") else None,
                               w=w, holdout=ho))
            continue
        if cd.get("obsType") == "wind_slab" and ll:
            amt = cd.get("windSlab24h")
            if amt and amt != "none":
                out.append(Obs(rid=rid, uid=uid, kind="drift", lat=ll[0], lon=ll[1], t=t,
                               elev=_num(r.get("elevation_m")),
                               aspect=ASPECT_DEG.get(str(cd.get("aspect") or "").upper()),
                               slope=35.0, w=w, holdout=ho))
            continue
        if cd.get("quick") and ll:
            cm = _num(cd.get("powderAmountCm"))
            asp = ASPECT_DEG.get(str(cd.get("exposition") or "").upper())
            out.append(Obs(rid=rid, uid=uid, kind="powder", lat=ll[0], lon=ll[1], t=t,
                           elev=_num(r.get("elevation_m")) or _num(cd.get("altitudeM")),
                           aspect=asp, slope=30.0 if asp is not None else None,
                           cm=cm, w=w, holdout=ho))
    return out


# ── 3. match ─────────────────────────────────────────────────────────────

def _km(lat1, lon1, lat2, lon2):
    return math.hypot((lon2 - lon1) * 78.0, (lat2 - lat1) * 111.0)


def _adiff(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def match(o, wps, runs_by_wp):
    """(wp, run) for one observation, or None."""
    best, bd = None, MATCH_KM
    for w in wps:
        d = _km(o.lat, o.lon, w["lat"], w["lon"])
        if d < bd and (o.elev is None or w["bands"][0] - MATCH_DZ <= o.elev <= w["bands"][-1] + MATCH_DZ):
            best, bd = w, d
    if best is None:
        return None
    rs = runs_by_wp.get(best["id"]) or []
    if not rs:
        return None
    band = min(best["bands"], key=lambda b: abs(b - (o.elev if o.elev is not None else b)))
    rs = [r for r in rs if r["elev"] == band]
    flat = o.aspect is None or o.slope is None or o.slope < 10
    if flat:
        cand = [r for r in rs if r["slope"] == 0]
    else:
        sl = [r for r in rs if r["slope"] > 0]
        if not sl:
            return None
        s0 = min({r["slope"] for r in sl}, key=lambda s: abs(s - o.slope))
        cand = sorted((r for r in sl if r["slope"] == s0), key=lambda r: _adiff(r["aspect"], o.aspect))[:1]
    return (best, cand[0]) if cand else None


# ── 4. compare ───────────────────────────────────────────────────────────

def _model_hit(kind, cm, m):
    """Does the model agree with this observation's category? None = no opinion."""
    from . import classify as C
    hs, pw = m["total_hs_cm"], m["powder_depth_cm"]
    if kind == "powder":
        if cm is None:
            return pw >= POWDER_MIN_CM
        return (pw >= POWDER_MIN_CM) == (cm >= POWDER_MIN_CM)
    if kind == "nosnow":
        return hs < NOSNOW_MAX_CM
    if kind == "wet":
        return m["surface_lw"] >= C.WET_LWC_MIN or m.get("soft_top_cm", 0) >= 2
    if kind == "crust":
        return m["crust_thick_cm"] >= C.CRUST_TRACE or m.get("refrozen", 0) >= 0.5
    if kind in ("drift", "scoured"):
        wc = int(C.classify_wind(np.array([hs]), np.array([m["drift_load"]]), np.array([m["wind_scour"]]))[0])
        return wc >= 2 if kind == "drift" else wc == 1
    return None


def compare(obs, wps, runs, results, layer_ts, mets, now=None):
    """Per observation: model vs report. Returns (rows, summary)."""
    now = now or datetime.utcnow()
    by_wp = {}
    for r in runs:
        if r["id"] in results:
            by_wp.setdefault(r["wp"], []).append(r)
    ts = list(layer_ts)
    rows = []
    for o in obs:
        if not ts or o.t < ts[0] or o.t > now:
            continue
        fi = max(i for i, t in enumerate(ts) if t <= o.t)
        mt = match(o, wps, by_wp)
        if mt is None:
            continue
        w, run = mt
        vec = results[run["id"]][0][fi]
        m = {k: float(vec[i]) for i, k in enumerate(mets)}
        rows.append({"rid": o.rid, "uid": o.uid, "kind": o.kind, "wp": w["id"], "run": run["id"],
                     "t": o.t.strftime("%Y-%m-%dT%H:%M"), "w": round(o.w, 3), "holdout": o.holdout,
                     "rep_cm": o.cm, "mod_cm": round(m["powder_depth_cm"], 1),
                     "hit": _model_hit(o.kind, o.cm, m)})
    return rows, summarize(rows)


def _depth_stats(rs):
    rs = [r for r in rs if r["kind"] == "powder" and r["rep_cm"] is not None]
    if not rs:
        return {"n": 0}
    e = np.array([r["mod_cm"] - r["rep_cm"] for r in rs])
    return {"n": len(rs), "bias_cm": round(float(e.mean()), 1), "mae_cm": round(float(np.abs(e).mean()), 1)}


def suggest_powder_threshold(rows):
    """Model powder depth [cm] that best separates "powder" from "no powder"
    reports -- the calibration hint for classify.POWDER_THIN. None if the
    reports do not cover both sides."""
    rs = [r for r in rows if r["kind"] == "powder" and r["rep_cm"] is not None and not r["holdout"]]
    yes = [r for r in rs if r["rep_cm"] >= POWDER_MIN_CM]
    if len(yes) < 3 or len(rs) - len(yes) < 3:
        return None
    best, bs = None, -1.0
    for th in (1, 2, 3, 5, 8, 10, 15):
        ok = sum(r["w"] for r in rs if (r["mod_cm"] >= th) == (r["rep_cm"] >= POWDER_MIN_CM))
        sc = ok / sum(r["w"] for r in rs)
        if sc > bs:
            best, bs = th, sc
    return {"cm": best, "agreement": round(bs, 3), "n": len(rs)}


def summarize(rows):
    if not rows:
        return {"observations": 0}
    kinds = {}
    for r in rows:
        if r["hit"] is None:
            continue
        k = kinds.setdefault(r["kind"], [0, 0])
        k[0] += 1
        k[1] += int(bool(r["hit"]))
    return {"observations": len(rows), "users": len({r["uid"] for r in rows}),
            "holdout": sum(1 for r in rows if r["holdout"]),
            "powder_depth": _depth_stats([r for r in rows if not r["holdout"]]),
            "hit_rate": {k: {"n": n, "hit": round(h / n, 3)} for k, (n, h) in kinds.items()},
            "powder_threshold": suggest_powder_threshold(rows)}


# ── 5. correction ────────────────────────────────────────────────────────

def _median(xs):
    return float(np.median(np.array(xs))) if xs else 0.0


def suggest_steps(rows):
    """{wp: multiplicative step on the precipitation factor} from the
    non-holdout powder-depth comparisons."""
    by = {}
    for r in rows:
        if r["kind"] == "powder" and r["rep_cm"] is not None and not r["holdout"]:
            by.setdefault(r["wp"], []).append(r)
    steps = {}
    for wp, rs in by.items():
        lr = [math.log((r["rep_cm"] + DAMP_CM) / (r["mod_cm"] + DAMP_CM)) for r in rs]
        med = _median(lr)
        mad = _median([abs(x - med) for x in lr]) or 0.05
        keep = [(r, x) for r, x in zip(rs, lr) if abs(x - med) <= OUTLIER_MAD * 1.4826 * mad]
        if len(keep) < MIN_REPORTS or len({r["uid"] for r, _ in keep}) < MIN_USERS:
            continue
        # no single author carries more than USER_SHARE_MAX of the weight
        tot = sum(r["w"] for r, _ in keep)
        per = {}
        for r, _ in keep:
            per[r["uid"]] = per.get(r["uid"], 0.0) + r["w"]
        cap = USER_SHARE_MAX * tot
        scale = {u: min(1.0, cap / v) for u, v in per.items()}
        ws = [r["w"] * scale[r["uid"]] for r, _ in keep]
        g = math.exp(sum(w * x for w, (_, x) in zip(ws, keep)) / sum(ws))
        steps[wp] = round(min(R_MAX, max(R_MIN, g)) ** STEP_EXP, 4)
    return steps


def holdout_check(rows, steps):
    """Would the steps have helped on reports they never saw? MAE of the
    model as is vs scaled by the step (snow depth ~ precipitation)."""
    rs = [r for r in rows if r["holdout"] and r["kind"] == "powder" and r["rep_cm"] is not None
          and r["wp"] in steps]
    if not rs:
        return {"n": 0}
    raw = np.mean([abs(r["mod_cm"] - r["rep_cm"]) for r in rs])
    cor = np.mean([abs(r["mod_cm"] * steps[r["wp"]] - r["rep_cm"]) for r in rs])
    return {"n": len(rs), "mae_raw_cm": round(float(raw), 2), "mae_corrected_cm": round(float(cor), 2),
            "helps": bool(cor <= raw)}


def apply_steps(factors, steps):
    out = dict(factors or {})
    for wp, s in steps.items():
        out[wp] = round(min(F_MAX, max(F_MIN, out.get(wp, 1.0) * s)), 3)
    return out


def cycle(wps, runs, results, layer_ts, mets, factors, now=None, fetched=None):
    """One live cycle: fetch, compare, suggest, check, maybe apply.

    Returns (new_factors, rows, summary)."""
    rows_db, likes, stale, trust = fetched if fetched is not None else fetch(now=now)
    obs = observations(rows_db, likes, stale, trust)
    rows, summ = compare(obs, wps, runs, results, layer_ts, mets, now=now)
    steps = suggest_steps(rows)
    hold = holdout_check(rows, steps)
    want = os.environ.get("COMMUNITY_APPLY", "0") == "1"
    ok = hold.get("n", 0) >= MIN_HOLDOUT and hold.get("helps")
    applied = bool(want and ok and steps)
    summ.update({"reports": len(rows_db), "steps": steps, "holdout_check": hold,
                 "mode": "apply" if want else "shadow", "applied": applied})
    return (apply_steps(factors, steps) if applied else dict(factors or {})), rows, summ
