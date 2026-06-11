"""MeteoSchweiz SwissMetNet Stations-Connector (fuer den Mess-Crosscheck).

REINER Datenzugriff auf die offenen MeteoSchweiz-Stationsdaten (ogd-smn):
- ``get_catalog`` : Liste aller automatischen Stationen mit Koordinaten.
- ``fetch_hourly`` : stuendliche Zeitreihe einer Station (Schneehoehe, Temp, Wind)
  aus der ``*_h_recent.csv`` (Zeitstempel in UTC).

Daraus wird spaeter der GEMESSENE Neuschnee als Summe der positiven
Schneehoehen-Zuwaechse ueber das gewaehlte Fenster gebildet (Settling/Wind
nicht korrigiert -> klar als Mess-Naeherung gekennzeichnet).

Quelle: opendata MeteoSchweiz (STAC ch.meteoschweiz.ogd-smn). "Source: MeteoSwiss".
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests

from config.settings import DATA_DIR

_STAC = ("https://data.geo.admin.ch/api/stac/v1/collections/"
         "ch.meteoschweiz.ogd-smn/items")
_CSV = ("https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/"
        "{abbr}/ogd-smn_{abbr}_h_recent.csv")

# Spalten der stuendlichen CSV
_COL_TIME = "reference_timestamp"
_COL_HS = "htoauths"     # automatische Schneehoehe [cm]
_COL_T = "tre200h0"      # Lufttemperatur 2 m [degC]


@dataclass
class Station:
    abbr: str
    lon: float
    lat: float
    elevation: float = 0.0
    hs: Dict[str, float] = field(default_factory=dict)   # "YYYY-MM-DDTHH" -> cm


def get_catalog(timeout: float = 30.0, max_pages: int = 4) -> List[Station]:
    """Listet alle automatischen Stationen mit Koordinaten (STAC, paginiert)."""
    out: List[Station] = []
    url = _STAC + "?limit=100"
    for _ in range(max_pages):
        j = requests.get(url, timeout=timeout).json()
        for f in j.get("features", []):
            c = f.get("geometry", {}).get("coordinates")
            if c and len(c) >= 2:
                out.append(Station(abbr=f["id"], lon=float(c[0]), lat=float(c[1])))
        nxt = [l["href"] for l in j.get("links", []) if l.get("rel") == "next"]
        if not nxt:
            break
        url = nxt[0]
    return out


def fetch_hourly(abbr: str, timeout: float = 30.0,
                 cache_dir: Path | None = None) -> Dict[str, float]:
    """Liest die stuendliche Schneehoehe (cm) einer Station, key = 'YYYY-MM-DDTHH' (UTC)."""
    cache_dir = cache_dir or (DATA_DIR / "station_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{abbr}_h_recent.csv"

    if cache.exists():
        txt = cache.read_text(encoding="utf-8", errors="replace")
    else:
        r = requests.get(_CSV.format(abbr=abbr), timeout=timeout)
        if r.status_code != 200:
            return {}
        txt = r.content.decode("utf-8", "replace")
        try:
            cache.write_text(txt, encoding="utf-8")
        except Exception:
            pass

    lines = txt.splitlines()
    if not lines:
        return {}
    hdr = lines[0].split(";")
    try:
        i_t = hdr.index(_COL_TIME)
        i_hs = hdr.index(_COL_HS)
    except ValueError:
        return {}

    series: Dict[str, float] = {}
    for line in lines[1:]:
        p = line.split(";")
        if len(p) <= max(i_t, i_hs):
            continue
        raw_t, raw_hs = p[i_t], p[i_hs]
        if not raw_hs:
            continue
        try:
            dt = datetime.strptime(raw_t, "%d.%m.%Y %H:%M")
            hs = float(raw_hs)
        except ValueError:
            continue
        series[dt.strftime("%Y-%m-%dT%H")] = hs
    return series


def select_alpine(stations: List[Station], elevation_at, n: int = 24,
                  min_elev: float = 1000.0) -> List[Station]:
    """Waehlt ~n schneerelevante (hoehere) Stationen, raeumlich gestreut.

    ``elevation_at(lon, lat) -> float`` liefert die Hoehe (z.B. aus dem DEM).
    """
    for s in stations:
        try:
            s.elevation = float(elevation_at(s.lon, s.lat))
        except Exception:
            s.elevation = 0.0
    high = sorted([s for s in stations if s.elevation >= min_elev],
                  key=lambda s: -s.elevation)
    return high[:n]
