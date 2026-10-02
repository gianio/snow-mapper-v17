"""Per-point meteorological forcing for SNOWPACK, from Open-Meteo.

SNOWPACK needs TA, RH, VW, DW, PSUM and ISWR (ILWR is generated internally). The
shared app client (data_connectors/open_meteo_client) only requests 6 variables and
omits RH + shortwave, so this module does its own self-contained Open-Meteo request
with the full SNOWPACK variable set, reusing the same endpoint/units/backoff idea.
Writes one .smet per point (spin-up window -> target date), cached under data/.
"""
from __future__ import annotations
import json, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import OPEN_METEO_URL, OPEN_METEO_ARCHIVE_URL, ARCHIVE_THRESHOLD_DAYS
from . import config

_VARS = ["temperature_2m", "relative_humidity_2m", "precipitation",
         "wind_speed_10m", "wind_direction_10m", "shortwave_radiation"]
# The model's own vertical temperature profile (free atmosphere), hour by hour.
# A fixed -6.5 K/km misses exactly the situations that matter for snow:
# inversions (cold valleys, mild slopes), warm air aloft, a cold-air pool.
_PL_LEVELS = (850, 700, 500)
_PL = [f"temperature_{p}hPa" for p in _PL_LEVELS] + [f"geopotential_height_{p}hPa" for p in _PL_LEVELS]
LAPSE_MIN, LAPSE_MAX = -0.0098, 0.015   # K/m: dry adiabatic .. strong inversion


def _endpoint(start: str):
    """Archive endpoint for old dates, forecast otherwise."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    if (datetime.utcnow().date() - d).days > ARCHIVE_THRESHOLD_DAYS:
        return OPEN_METEO_ARCHIVE_URL, False   # archive doesn't accept 'models'
    return OPEN_METEO_URL, True


# Worst case per point used to be ~8 min: five 90 s timeouts plus 2+4+6+8 s of
# backoff, all of it silent. Across the national 978 that is longer than the
# GitHub job limit, so the budget is now explicit and bounded.
_TIMEOUT_S = 40.0
_RETRY_BUDGET_S = 75.0


def _get(url, params, retries=4, backoff=2.0, budget=_RETRY_BUDGET_S):
    q = urllib.parse.urlencode(params, safe=",")
    t0 = time.time()
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=_TIMEOUT_S) as r:
                return json.load(r)
        except Exception as e:
            last = e
            wait = backoff * (i + 1)
            if i == retries - 1 or time.time() - t0 + wait > budget:
                raise
            time.sleep(wait)
    raise last  # unreachable; kept so the contract is obvious


def fetch_point(lat, lon, start_date, end_date, model="best_match"):
    url, send_model = _endpoint(start_date)
    params = {"latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
              "hourly": ",".join(_VARS), "wind_speed_unit": "ms",
              "timezone": "UTC", "start_date": start_date, "end_date": end_date}
    if send_model:
        params["models"] = model
    d = _get(url, params)
    return d.get("hourly", {})


def hourly_to_smet(pid, lat, lon, elev, hourly, dst: Path):
    """Write a SNOWPACK SMET from an Open-Meteo hourly block."""
    t = hourly.get("time", [])
    TA = hourly.get("temperature_2m", [])
    RH = hourly.get("relative_humidity_2m", [])
    P = hourly.get("precipitation", [])
    VW = hourly.get("wind_speed_10m", [])
    DW = hourly.get("wind_direction_10m", [])
    IS = hourly.get("shortwave_radiation", [])
    lines = ["SMET 1.1 ASCII", "[HEADER]", f"station_id   = {pid}",
             f"station_name = va_{pid}", f"latitude     = {lat:.6f}",
             f"longitude    = {lon:.6f}", f"altitude     = {elev:.1f}",
             "nodata       = -999", "tz           = 0",
             "fields       = timestamp TA RH VW DW PSUM ISWR", "[DATA]"]
    def g(a, i, d=None):
        v = a[i] if i < len(a) else None
        return v if v is not None else d
    for i, ts in enumerate(t):
        ta = g(TA, i); rh = g(RH, i); p = g(P, i); vw = g(VW, i); dw = g(DW, i); isw = g(IS, i)
        if ta is None:
            continue
        lines.append(f"{ts}:00 {ta+273.15:.2f} {max(0.01,min(1.0,(rh or 50)/100)):.3f} "
                     f"{max(0.2,vw or 0.2):.2f} {dw or 0:.0f} {max(0.0,p or 0.0):.3f} {max(0.0,isw or 0.0):.1f}")
    dst.write_text("\n".join(lines) + "\n")
    return dst


def _covers(path: Path, start: str) -> bool:
    """True if an existing .smet already begins at or before `start`.

    A plain `dst.exists()` cache check is not enough once the required window
    moves: a file fetched under the old (lead-in-free) window starts a day too
    late, gets reused, and SNOWPACK dies on its first timestep exactly as it
    did before. So look at the first data row instead of the filename.
    """
    try:
        txt = path.read_text()
    except OSError:
        return False
    body = txt.split("[DATA]", 1)
    if len(body) != 2:
        return False
    for line in body[1].splitlines():
        line = line.strip()
        if line:
            return line.split()[0][:10] <= start
    return False


def build_forcing(points, target_date, spinup_days=None, model="best_match",
                  meteo_dir: Path | None = None, lead_days=None, workers=6,
                  since=None, until=None):
    """Fetch + write .smet for every point. Returns dir with <id>.smet files.

    `since`/`until` (YYYY-MM-DD) are the OUTPUT WINDOW, and both ends matter.

    until: the app's timeline runs days past the target date, and SNOWPACK
    cannot be integrated into hours it has no meteo for.

    since: the spin-up is measured back from the START of that window, which
    is days BEFORE the target date. Anchoring it on the target date instead
    left the .smet starting three days after the model's own ProfileDate, and
    SNOWPACK died on its first timestep with `missing { TA sw_radiation
    precipitation precip_splitting VW }` -- the same class of failure as the
    original lead-in bug, one layer up.
    """
    spinup_days = spinup_days or config.SPINUP_DAYS
    lead_days = config.FORCING_LEAD_DAYS if lead_days is None else lead_days
    meteo_dir = meteo_dir or (config.WORK_DIR / "meteo")
    meteo_dir.mkdir(parents=True, exist_ok=True)
    end = datetime.strptime(until or target_date, "%Y-%m-%d").date()
    spin_from = datetime.strptime(since or target_date, "%Y-%m-%d").date()
    # spin-up window + lead-in, so the .smet starts strictly before the
    # .sno ProfileDate (= end - spinup_days). See config.FORCING_LEAD_DAYS.
    start = (spin_from - timedelta(days=spinup_days + lead_days)).isoformat()
    todo = [p for p in points if not _covers(meteo_dir / f"{p['id']}.smet", start)]
    print(f"  forcing: {len(points) - len(todo)}/{len(points)} already cached, "
          f"fetching {len(todo)} ({start} .. {end})")

    def one(p):
        dst = meteo_dir / f"{p['id']}.smet"
        try:
            h = fetch_point(p["lat"], p["lon"], start, end.isoformat(), model)
            hourly_to_smet(p["id"], p["lat"], p["lon"], p["elev"], h, dst)
            return p["id"], None
        except Exception as e:
            return p["id"], f"{type(e).__name__}: {e}"

    # Modest concurrency. Open-Meteo allows far more than this per minute, and
    # the archive endpoint is slow rather than rate-limiting, so a handful of
    # workers turns a serial stall into a bounded one. Writes go to distinct
    # paths, so no locking is needed.
    t0 = time.time()
    failed = []
    done = 0
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for pid, err in ex.map(one, todo):
                done += 1
                if err:
                    failed.append((pid, err))
                # Frequent enough that a stall is visible in the CI log rather
                # than looking like a hang.
                if done % 25 == 0 or done == len(todo):
                    rate = done / max(1e-6, time.time() - t0)
                    print(f"  forcing {done}/{len(todo)}  {rate:.1f}/s  "
                          f"{len(failed)} failed", flush=True)
    if failed:
        print(f"  [forcing] {len(failed)} failed; first few: {failed[:3]}")
    # Summarise unconditionally. The progress line above only fires every 20
    # points, so a short run (a CI smoke test with --limit 8) printed NOTHING
    # -- including when every fetch failed. An empty .smet is also the likeliest
    # reason SNOWPACK exits immediately having written no .pro, so the sizes
    # matter as much as the count.
    smets = sorted(meteo_dir.glob("*.smet"))
    sizes = [f.stat().st_size for f in smets]
    tiny = [f.name for f, z in zip(smets, sizes) if z < 2000]
    print(f"  forcing: {len(smets)} .smet in {meteo_dir}, "
          f"median {int(sorted(sizes)[len(sizes)//2]) if sizes else 0} B")
    if tiny:
        print(f"  [forcing] WARNING {len(tiny)} suspiciously small: {tiny[:5]}")
    return meteo_dir


# ── Weather-point forcing (matrix mode) ─────────────────────────────────────
# One request per weather point, not per SNOWPACK run: every virtual slope of
# a weather point shares its weather, which is the whole point of the design.
# 135 requests instead of 978, and each can afford a better model.

_FETCH_TRIES = 4
_RETRY_PAUSE_S = 5.0
_BACKOFF_429_S = 15.0
FORCING_WORKERS = 3          # parallel weather points; 6 ran into Open-Meteo's rate limit


def _fetch_json(url, params, timeout=_TIMEOUT_S):
    """(json, None) or (None, reason). A 400 is a verdict, not a hiccup --
    unknown model, date out of range -- so it is returned immediately rather
    than retried."""
    import urllib.error
    q = urllib.parse.urlencode(params, safe=",")
    last = None
    for i in range(_FETCH_TRIES):
        wait = 2.0 * (i + 1)
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=timeout) as r:
                return json.load(r), None
        except urllib.error.HTTPError as e:
            if e.code == 400:
                try:
                    return None, json.load(e).get("reason", "HTTP 400")
                except Exception:
                    return None, "HTTP 400"
            last = f"HTTP {e.code}"
            if e.code == 429:
                # Rate limited: the first national live cycle lost 9 weather
                # points to 429s retried after 2-6 s. Back off for real, and
                # take Retry-After when the server names it.
                try:
                    wait = float(e.headers.get("Retry-After") or 0) or 0
                except Exception:
                    wait = 0
                wait = max(wait, _BACKOFF_429_S * (2 ** i))
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if i < _FETCH_TRIES - 1:
            time.sleep(min(wait, 120.0))
    return None, last


def _coverage(hourly):
    """Share of hours where every variable SNOWPACK needs is actually filled."""
    t = hourly.get("time") or []
    if not t:
        return 0.0
    need = ("temperature_2m", "precipitation", "shortwave_radiation", "wind_speed_10m")
    ok = sum(all((hourly.get(v) or [None] * len(t))[i] is not None for v in need)
             for i in range(len(t)))
    return ok / len(t)


def _recent(end_iso):
    from datetime import date
    return (date.today() - datetime.strptime(end_iso, "%Y-%m-%d").date()).days < 5


_NEED = ("temperature_2m", "precipitation", "shortwave_radiation", "wind_speed_10m")


def _splice(base, extra):
    """Fill the hours `base` lacks from `extra`, keyed by timestamp.

    ICON-CH1 only reaches ~33 h ahead and ICON-CH2 5 days, so a live window
    that ends 6 days out needs more than one model. Splicing hour by hour
    keeps the finest model wherever it exists instead of throwing it away
    for the one model that happens to cover everything.
    Returns (spliced, hours_taken_from_extra).
    """
    if not base or not base.get("time"):
        ta = extra.get("temperature_2m") or []
        return ({k: list(v) for k, v in extra.items()},
                sum(1 for x in ta if x is not None))
    pos = {t: i for i, t in enumerate(base["time"])}
    out = {k: list(v) for k, v in base.items()}
    taken = 0
    et = extra.get("time") or []
    for j, t in enumerate(et):
        i = pos.get(t)
        if i is None:
            continue
        if all(out.get(v) is not None and i < len(out[v]) and out[v][i] is not None
               for v in _NEED):
            continue
        filled = False
        for v in _VARS:
            ev = (extra.get(v) or [None] * len(et))
            if j < len(ev) and ev[j] is not None:
                if out.get(v) is None:
                    out[v] = [None] * len(out["time"])
                if out[v][i] is None:
                    out[v][i] = ev[j]; filled = True
        taken += filled
    # The vertical profile is filled wherever a model has it, independent of
    # which model supplied the surface values for that hour.
    for v in _PL:
        ev = extra.get(v)
        if not ev:
            continue
        if out.get(v) is None:
            out[v] = [None] * len(out["time"])
        for j, t in enumerate(et):
            i = pos.get(t)
            if i is not None and j < len(ev) and ev[j] is not None and out[v][i] is None:
                out[v][i] = ev[j]
    return out, taken


def fetch_weather(lat, lon, elev, start, end, models=None):
    """Hourly forcing for one weather point, finest model first, spliced.

    Each high-resolution model in turn -- through the forecast API when the
    window reaches the last few days (live), else the historical-forecast
    API -- fills the hours the finer ones left empty. `icon_seamless` then
    covers the far end of a live forecast, and the ~9-25 km archive is the
    last resort for old dates. `elevation` makes the API downscale
    temperature to the weather point's reference height; the per-run lapse is
    applied on top of that.

    Returns (hourly, model_label, notes). The label names the model that
    supplied most hours, with a "+" when others filled gaps.
    """
    models = config.FORCING_MODELS if models is None else models
    base = {"latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
            "elevation": f"{elev:.0f}", "hourly": ",".join(_VARS + _PL),
            "wind_speed_unit": "ms", "timezone": "UTC",
            "start_date": start, "end_date": end}
    live = _recent(end)
    url = OPEN_METEO_URL if live else config.HISTORICAL_FORECAST_URL
    chain = list(models) + ([config.FORCING_FAR_MODEL] if live else [])
    notes, used = [], {}
    h = {}
    for m in chain:
        if h and _coverage(h) >= 0.999:
            break
        d, why = _fetch_json(url, {**base, "models": m})
        if d is None and why and "hPa" in why:
            # a model without pressure levels: the surface values still count
            d, why = _fetch_json(url, {**base, "models": m, "hourly": ",".join(_VARS)})
        hm = (d or {}).get("hourly") or {}
        if not hm.get("time"):
            notes.append(f"{m}: {why or 'no data'}")
            continue
        h, n = _splice(h, hm)
        if n:
            used[m] = n
        else:
            notes.append(f"{m}: nothing new")
    if _coverage(h) < config.FORCING_MIN_COVERAGE:
        d, why = _fetch_json(OPEN_METEO_ARCHIVE_URL, base)
        ha = (d or {}).get("hourly") or {}
        if ha.get("time"):
            h, n = _splice(h, ha)
            if n:
                used["archive"] = n
        else:
            notes.append(f"archive: {why or 'no data'}")
    if _coverage(h) < config.FORCING_MIN_COVERAGE:
        notes.append(f"coverage {_coverage(h)*100:.0f}% after all sources")
        return None, None, notes
    # The vertical profile: if the surface models did not bring it (not every
    # model publishes pressure levels), one extra request for the profile
    # alone from ICON seamless (ICON-D2/EU/global), which always has it.
    n = len(h.get("time") or [])
    have = sum(1 for v in (h.get("temperature_850hPa") or []) if v is not None)
    if n and have < 0.9 * n:
        d, why = _fetch_json(url, {**base, "models": config.FORCING_FAR_MODEL, "hourly": ",".join(_PL)})
        hp = (d or {}).get("hourly") or {}
        if hp.get("time"):
            h, _ = _splice(h, hp)
            notes.append(f"profile from {config.FORCING_FAR_MODEL}")
        else:
            notes.append(f"no vertical profile ({why or 'no data'}): fixed lapse rate")
    main = max(used, key=used.get)
    label = main + ("+" if len(used) > 1 else "")
    if len(used) > 1:
        notes.append("hours: " + ", ".join(f"{k} {v}" for k, v in used.items()))
    return h, label, notes


def _overlay_ogd(base, ogd):
    """Put MeteoSwiss OGD forecast hours over the Open-Meteo series.

    OGD values are at the ICON cell's own height, not at the weather point's
    reference height that Open-Meteo downscales to. So OGD temperature is
    shifted by the mean difference over the hours both have -- the height
    correction, measured instead of assumed. Returns (merged, hours, dT).
    """
    if not ogd or not ogd.get("time"):
        return base, 0, 0.0
    if not base or not base.get("time"):
        return {k: list(v) for k, v in ogd.items()}, len(ogd["time"]), 0.0
    pos = {t: i for i, t in enumerate(base["time"])}
    diffs = []
    for j, t in enumerate(ogd["time"]):
        i = pos.get(t)
        if i is None:
            continue
        a = (base.get("temperature_2m") or [None] * len(base["time"]))[i]
        b = ogd["temperature_2m"][j]
        if a is not None and b is not None:
            diffs.append(a - b)
    dT = sum(diffs) / len(diffs) if len(diffs) >= 6 else 0.0
    out = {k: list(v) for k, v in base.items()}
    n = 0
    for j, t in enumerate(ogd["time"]):
        i = pos.get(t)
        col = lambda v: (ogd.get(v) or [None] * len(ogd["time"]))[j]
        if i is None or any(col(v) is None for v in _NEED):
            continue
        for v in _VARS:
            x = col(v)
            if x is None:
                continue
            if v == "temperature_2m":
                x = x + dT
            out.setdefault(v, [None] * len(out["time"]))[i] = x
        n += 1
    return out, n, dT


def _profile_dT(hourly, i, z_ref, z):
    """Temperature change from z_ref to z (m) in hour i, from the model's
    free-atmosphere profile (850/700/500 hPa, piecewise linear in height,
    extrapolated with the nearest segment), clipped to a physical lapse
    range. None when the hour has fewer than two levels."""
    nodes = []
    for p in _PL_LEVELS:
        t = (hourly.get(f"temperature_{p}hPa") or [None] * (i + 1))
        g = (hourly.get(f"geopotential_height_{p}hPa") or [None] * (i + 1))
        if i < len(t) and i < len(g) and t[i] is not None and g[i] is not None:
            nodes.append((float(g[i]), float(t[i])))
    if len(nodes) < 2 or z == z_ref:
        return None if len(nodes) < 2 else 0.0
    nodes.sort()
    def at(zz):
        k = 0
        while k < len(nodes) - 2 and zz > nodes[k + 1][0]:
            k += 1
        (z0, t0), (z1, t1) = nodes[k], nodes[k + 1]
        return t0 + (t1 - t0) * (zz - z0) / max(1.0, z1 - z0)
    g = (at(z) - at(z_ref)) / (z - z_ref)
    return min(LAPSE_MAX, max(LAPSE_MIN, g)) * (z - z_ref)


def _lapsed(hourly, dz, precip_factor=1.0, z_ref=None, band_t=None):
    """Same weather, temperature moved `dz` metres up or down, precipitation
    scaled by `precip_factor` (the IMIS snow-height correction, imis.py).

    With `z_ref` and a vertical profile in `hourly`, each hour uses the
    model's own lapse rate; hours without one fall back to the fixed rate.
    `band_t` (live, elevtemp.py): {time: (temperature, dz_cell)} measured in an
    ICON-CH1 cell at about this height -- used as is for those hours, with the
    profile only bridging the few metres between cell and band."""
    t_in = hourly.get("temperature_2m", [])
    times = hourly.get("time", [])
    out = dict(hourly)
    ta, n_prof = [], 0
    for i, v in enumerate(t_in):
        if v is None:
            ta.append(None); continue
        bt = band_t.get(times[i]) if band_t and i < len(times) else None
        if bt is not None:
            tc, dzc = bt
            d = _profile_dT(hourly, i, 0.0, dzc) if z_ref is not None else None
            ta.append(tc + (d if d is not None else config.TA_LAPSE_K_PER_M * dzc)); n_prof += 1
            continue
        d = _profile_dT(hourly, i, z_ref, z_ref + dz) if z_ref is not None else None
        if d is None:
            d = config.TA_LAPSE_K_PER_M * dz
        else:
            n_prof += 1
        ta.append(v + d)
    out["temperature_2m"] = ta
    out["_profile_hours"] = n_prof
    if precip_factor != 1.0:
        out["precipitation"] = [None if v is None else v * precip_factor
                                for v in hourly.get("precipitation", [])]
    return out


def build_forcing_matrix(wps, runs, target_date, win, meteo_dir: Path | None = None,
                         spinup_days=None, lead_days=None, workers=None, since=None,
                         precip_factor=None, ogd_dir=None, band_temps=None):
    """One fetch per weather point, one lapse-rated .smet per run.

    `since`: earliest instant any run starts integrating (live mode: the
    carried state's time). Defaults to the spin-up start before the window.
    `precip_factor`: {weather_point_id: factor} from the IMIS correction.
    `band_temps`: {weather_point_id: {band_elev: {time: (T, dz)}}} -- ICON-CH1
    2 m temperature from cells at the band's own height (elevtemp.py, live).

    Returns {weather_point_id: model_used} so the export can say what drove it.
    """
    spinup_days = spinup_days or config.SPINUP_DAYS
    lead_days = config.FORCING_LEAD_DAYS if lead_days is None else lead_days
    meteo_dir = meteo_dir or (config.WORK_DIR / "meteo")
    meteo_dir.mkdir(parents=True, exist_ok=True)
    precip_factor = precip_factor or {}
    first = since if since is not None else win[0] - timedelta(days=spinup_days)
    start = (first - timedelta(days=lead_days)).date().isoformat()
    # One day of margin past the window, so the final integration step never
    # asks for an hour the forcing does not have.
    end = (win[1] + timedelta(days=1)).date().isoformat()
    print(f"  forcing: {len(wps)} weather points, {start} .. {end}, "
          f"models {', '.join(config.FORCING_MODELS)} then archive")

    def one(w):
        cache = meteo_dir / f"{w['id']}.json"
        if cache.exists():
            try:
                c = json.loads(cache.read_text())
                if c.get("start") <= start and c.get("end") >= end and c.get("hourly"):
                    return w["id"], c["hourly"], c["model"], []
            except Exception:
                pass
        h, m, notes = fetch_weather(w["lat"], w["lon"], w["ref_elev"], start, end)
        # MeteoSwiss OGD forecast hours on top (tools/ogd_extract.py), when
        # the workflow produced them.
        of = Path(ogd_dir) / f"{w['id']}.json" if ogd_dir else None
        if h and of is not None and of.exists():
            try:
                od = json.loads(of.read_text())
                h, n_ogd, dT = _overlay_ogd(h, od.get("hourly"))
                if n_ogd:
                    notes.append(f"OGD {od.get('model')} {n_ogd} h, dT {dT:+.1f} K")
                    m = f"{od.get('model')}+{m}"
            except Exception as e:
                notes.append(f"OGD unreadable: {e}")
        if h:
            cache.write_text(json.dumps({"start": start, "end": end, "model": m,
                                         "hourly": h}))
        return w["id"], h, m, notes

    used, failed = {}, []
    t0 = time.time()
    by_wp = {}
    for r in runs:
        by_wp.setdefault(r["wp"], []).append(r)
    ref = {w["id"]: w["ref_elev"] for w in wps}
    workers = workers or FORCING_WORKERS

    band_temps = band_temps or {}
    stats = {"hours": 0, "profile": 0, "cell": 0}

    def write(wid, h, m):
        used[wid] = m
        bt_wp = band_temps.get(wid) or {}
        for r in by_wp.get(wid, []):
            bt = bt_wp.get(int(r["elev"]))
            hh = _lapsed(h, r["elev"] - ref[wid], precip_factor.get(wid, 1.0),
                         z_ref=ref[wid], band_t=bt)
            stats["hours"] += sum(1 for v in hh["temperature_2m"] if v is not None)
            stats["profile"] += hh.get("_profile_hours", 0)
            stats["cell"] += len(bt or {})
            hourly_to_smet(r["id"], r["lat"], r["lon"], r["elev"], hh,
                           meteo_dir / f"{r['id']}.smet")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for k, (wid, h, m, notes) in enumerate(ex.map(one, wps), 1):
            if not h:
                failed.append((wid, notes))
            else:
                write(wid, h, m)
            if k % 25 == 0 or k == len(wps):
                print(f"  forcing {k}/{len(wps)}  {time.time()-t0:.0f}s  "
                      f"{len(failed)} failed", flush=True)
    # A second, sequential pass for what failed -- rate limits and handshake
    # timeouts are about the burst, not about the point.
    if failed:
        byid = {w["id"]: w for w in wps}
        print(f"  forcing: retrying {len(failed)} weather points one at a time", flush=True)
        again = []
        for wid, _ in failed:
            time.sleep(_RETRY_PAUSE_S)
            _, h, m, notes = one(byid[wid])
            if h:
                write(wid, h, m)
            else:
                again.append((wid, notes))
        failed = again
    from collections import Counter
    print(f"  forcing models used: {dict(Counter(used.values()))}")
    if stats["hours"]:
        print(f"  temperature by height: {stats['profile'] / stats['hours']:.0%} of run-hours from the "
              f"model's own profile ({stats['cell'] / stats['hours']:.0%} from ICON-CH1 cells at the "
              f"band's height), the rest at the fixed {config.TA_LAPSE_K_PER_M * 1000:+.1f} K/km")
    if failed:
        print(f"  [forcing] {len(failed)} weather points without forcing; first: {failed[:2]}")
    return used
