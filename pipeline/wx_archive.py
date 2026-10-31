"""Our own weather archive: MeteoSwiss ICON-CH hours at the weather points.

MeteoSwiss open data (OGD) only keeps the latest model runs, so the past days
of a SNOWPACK window used to come from Open-Meteo. Every live cycle now files
the hours it gets from OGD (tools/ogd_extract.py) here, per UTC day:

    <archive>/ogd/YYYY/MM/DD.json.gz
    {"v": 1, "vars": [...], "p": {point_id: {"HH:MM": [lead, model, v1, v2, ...]}}}

Of two runs for the same hour the one with the shorter lead time wins, so a
day ends up as a chain of short forecasts (lead 1-3 h at a 3-hourly cycle),
close to an analysis. Longer leads fill the gaps a missed cycle leaves.

The archive is plain files: the live workflow keeps them in Cloudflare R2
(tools/wx_archive.py pull/push; no egress fees, ~0.015 USD per GB and month)
and in the Actions cache as a fallback while R2 is not set up.

    add_run(archive, ogd_dir)                    -> hours written
    series(archive, point_id, start, end)        -> Open-Meteo style hourly or None
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

VARS = ["temperature_2m", "relative_humidity_2m", "precipitation",
        "wind_speed_10m", "wind_direction_10m", "shortwave_radiation"]
MAX_LEAD = 24          # hours of each run worth keeping (shorter leads win)
_FMT = "%Y-%m-%dT%H:%M"


def _day_path(archive: Path, day: dt.date) -> Path:
    return Path(archive) / "ogd" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}.json.gz"


def _read(path: Path) -> Dict:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") == 1 and d.get("vars") == VARS:
            return d
    except FileNotFoundError:
        pass
    except Exception as e:                          # noqa: BLE001
        print(f"[ARCHIVE] {path} unreadable ({type(e).__name__}) -- starting it again")
    return {"v": 1, "vars": VARS, "p": {}}


def _write(path: Path, d: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(d, f, separators=(",", ":"))
    tmp.replace(path)


def add_run(archive: Path, ogd_dir: Path, max_lead: int = MAX_LEAD) -> int:
    """File the hours of one OGD extract (ogd_dir/<point>.json) into the day
    files. Returns the number of point-hours written or replaced."""
    days: Dict[dt.date, Dict] = {}
    n = 0
    for f in sorted(Path(ogd_dir).glob("*.json")):
        try:
            d = json.loads(f.read_text())
            ref = dt.datetime.strptime(d["ref_time"], _FMT)
            h = d["hourly"]
        except Exception:                           # noqa: BLE001 -- not a point file
            continue
        pid, model = f.stem, str(d.get("model", "ogd"))
        for i, ts in enumerate(h.get("time") or []):
            t = dt.datetime.strptime(ts, _FMT)
            lead = int(round((t - ref).total_seconds() / 3600))
            if lead < 1 or lead > max_lead:
                continue
            vals = [(h.get(v) or [None] * (i + 1))[i] for v in VARS]
            if vals[0] is None:                     # no temperature, no hour
                continue
            day = days.get(t.date())
            if day is None:
                day = days[t.date()] = _read(_day_path(archive, t.date()))
            hours = day["p"].setdefault(pid, {})
            key = f"{t:%H:%M}"
            old = hours.get(key)
            if old is None or lead <= old[0]:
                hours[key] = [lead, model] + vals
                n += 1
    for day, d in days.items():
        _write(_day_path(archive, day), d)
    return n


def _days(start: dt.datetime, end: dt.datetime) -> Iterable[dt.date]:
    d = start.date()
    while d <= end.date():
        yield d
        d += dt.timedelta(days=1)


def series(archive: Path, pid: str, start: str, end: str) -> Optional[Dict[str, List]]:
    """The archived hours of one point between start and end (dates or
    ISO times, inclusive), as an Open-Meteo style hourly block -- the shape
    variant_a/forcing.py lays over its base series. None if there are none."""
    if not archive:
        return None
    s = dt.datetime.fromisoformat(start)
    e = dt.datetime.fromisoformat(end)
    if len(end) <= 10:                              # a date: the whole day
        e = e + dt.timedelta(hours=23)
    out: Dict[str, List] = {"time": [], **{v: [] for v in VARS}}
    for day in _days(s, e):
        p = _day_path(Path(archive), day)
        if not p.exists():
            continue
        hours = _read(p)["p"].get(pid) or {}
        for key in sorted(hours):
            t = dt.datetime.combine(day, dt.time.fromisoformat(key))
            if t < s or t > e:
                continue
            row = hours[key]
            out["time"].append(f"{t:{_FMT}}")
            for j, v in enumerate(VARS):
                out[v].append(row[2 + j])
    return out if out["time"] else None


def coverage(archive: Path, days: int = 14, today: Optional[dt.date] = None) -> List[tuple]:
    """(date, points, hours) for the last days -- for the job summary."""
    today = today or dt.datetime.utcnow().date()
    rows = []
    for k in range(days, -1, -1):
        day = today - dt.timedelta(days=k)
        p = _day_path(Path(archive), day)
        if p.exists():
            d = _read(p)["p"]
            rows.append((day, len(d), sum(len(h) for h in d.values())))
    return rows


def prune(archive: Path, keep_days: int, today: Optional[dt.date] = None) -> int:
    """Delete local day files older than keep_days (the R2 copy stays)."""
    today = today or dt.datetime.utcnow().date()
    n = 0
    for p in Path(archive).glob("ogd/*/*/*.json.gz"):
        try:
            y, m, d = p.parts[-3], p.parts[-2], p.name[:2]
            if (today - dt.date(int(y), int(m), int(d))).days > keep_days:
                p.unlink()
                n += 1
        except ValueError:
            continue
    return n
