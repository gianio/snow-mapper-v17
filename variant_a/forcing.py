"""Per-point meteorological forcing for SNOWPACK, from Open-Meteo.

SNOWPACK needs TA, RH, VW, DW, PSUM and ISWR (ILWR is generated internally). The
shared app client (data_connectors/open_meteo_client) only requests 6 variables and
omits RH + shortwave, so this module does its own self-contained Open-Meteo request
with the full SNOWPACK variable set, reusing the same endpoint/units/backoff idea.
Writes one .smet per point (spin-up window -> target date), cached under data/.
"""
from __future__ import annotations
import json, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import OPEN_METEO_URL, OPEN_METEO_ARCHIVE_URL, ARCHIVE_THRESHOLD_DAYS
from . import config

_VARS = ["temperature_2m", "relative_humidity_2m", "precipitation",
         "wind_speed_10m", "wind_direction_10m", "shortwave_radiation"]


def _endpoint(start: str):
    """Archive endpoint for old dates, forecast otherwise."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    if (datetime.utcnow().date() - d).days > ARCHIVE_THRESHOLD_DAYS:
        return OPEN_METEO_ARCHIVE_URL, False   # archive doesn't accept 'models'
    return OPEN_METEO_URL, True


def _get(url, params, retries=5, backoff=2.0):
    q = urllib.parse.urlencode(params, safe=",")
    for i in range(retries):
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=90) as r:
                return json.load(r)
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(backoff * (i + 1))


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
                  meteo_dir: Path | None = None, lead_days=None):
    """Fetch + write .smet for every point. Returns dir with <id>.smet files."""
    spinup_days = spinup_days or config.SPINUP_DAYS
    lead_days = config.FORCING_LEAD_DAYS if lead_days is None else lead_days
    meteo_dir = meteo_dir or (config.WORK_DIR / "meteo")
    meteo_dir.mkdir(parents=True, exist_ok=True)
    end = datetime.strptime(target_date, "%Y-%m-%d").date()
    # spin-up window + lead-in, so the .smet starts strictly before the
    # .sno ProfileDate (= end - spinup_days). See config.FORCING_LEAD_DAYS.
    start = (end - timedelta(days=spinup_days + lead_days)).isoformat()
    for k, p in enumerate(points, 1):
        dst = meteo_dir / f"{p['id']}.smet"
        if _covers(dst, start):
            continue
        try:
            h = fetch_point(p["lat"], p["lon"], start, end.isoformat(), model)
            hourly_to_smet(p["id"], p["lat"], p["lon"], p["elev"], h, dst)
        except Exception as e:
            print(f"  [forcing] {p['id']} failed: {e}")
        if k % 20 == 0:
            print(f"  forcing {k}/{len(points)}")
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
