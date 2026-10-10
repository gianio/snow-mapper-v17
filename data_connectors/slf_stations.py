"""SLF/IMIS Stations-Connector (Hochgebirge, ueber der Baumgrenze).

Offene SLF-API (measurement-api.slf.ch, Lizenz CC BY 4.0 - Quelle: WSL/SLF).
Die ~200 IMIS-Stationen liegen meist 2000-3000 m und messen u.a.:
  - HS               : Schneehoehe [cm]
  - TA_30MIN_MEAN    : Lufttemperatur [degC]
  - TSS_30MIN_MEAN   : Schneeoberflaechentemperatur [degC]
  - VW_/DW_          : Wind

Genutzt fuer:
  - mehr BERG-Stationen im Crosscheck (SwissMetNet liegt eher im Tal),
  - Icons mit gemessener Luft- und Schneeoberflaechentemperatur,
  - gemessener Neuschnee = Sigma positiver HS-Zuwaechse ueber das Fenster.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests

from config.settings import DATA_DIR

_BASE = "https://measurement-api.slf.ch"


@dataclass
class ImisStation:
    code: str
    label: str
    lon: float
    lat: float
    elevation: float
    type: str = ""
    ta: float | None = None     # aktuelle Lufttemperatur [degC]
    tss: float | None = None    # aktuelle Schneeoberflaechentemp [degC]
    hs_now: float | None = None  # aktuelle Schneehoehe [cm]
    vw: float | None = None      # aktuelle Windgeschw. [m/s]
    dw: float | None = None      # aktuelle Windrichtung [Grad]
    hs: Dict[str, float] = field(default_factory=dict)  # hourkey -> HS [cm]


def get_stations(timeout: float = 30.0) -> List[ImisStation]:
    """Liste aller IMIS-Stationen mit Koordinaten/Hoehe."""
    j = requests.get(_BASE + "/public/api/imis/stations", timeout=timeout).json()
    out = []
    for s in j:
        try:
            out.append(ImisStation(code=s["code"], label=s.get("label", ""),
                                   lon=float(s["lon"]), lat=float(s["lat"]),
                                   elevation=float(s.get("elevation", 0.0)),
                                   type=s.get("type", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def attach_latest(stations: List[ImisStation], timeout: float = 40.0) -> None:
    """Setzt ta/tss/hs_now je Station aus den aktuellsten Bulk-Messwerten."""
    rows = requests.get(_BASE + "/public/api/imis/measurements", timeout=timeout).json()
    latest: Dict[str, dict] = {}
    for r in rows:
        c = r.get("station_code")
        d = r.get("measure_date", "")
        if not c:
            continue
        if c not in latest or d > latest[c].get("measure_date", ""):
            latest[c] = r
    by_code = {s.code: s for s in stations}
    for c, r in latest.items():
        s = by_code.get(c)
        if not s:
            continue
        s.ta = _num(r.get("TA_30MIN_MEAN"))
        s.tss = _num(r.get("TSS_30MIN_MEAN"))
        s.hs_now = _num(r.get("HS"))
        s.vw = _num(r.get("VW_30MIN_MEAN"))
        s.dw = _num(r.get("DW_30MIN_MEAN"))


# Hourly series exported per station: our key -> SLF measurement field.
SERIES_FIELDS = {
    "hs": "HS",                 # Schneehoehe [cm]
    "ta": "TA_30MIN_MEAN",      # Lufttemperatur [degC]
    "tss": "TSS_30MIN_MEAN",    # Schneeoberflaechentemperatur [degC]
    "vw": "VW_30MIN_MEAN",      # Wind Mittel [m/s]
    "vwmax": "VW_30MIN_MAX",    # Boe [m/s] (nicht jede Station)
    "dw": "DW_30MIN_MEAN",      # Windrichtung [Grad]
}


def fetch_series(code: str, period_days: int, timeout: float = 40.0,
                 cache_dir: Path | None = None) -> Dict[str, Dict[str, float]]:
    """Stuendliche Messreihen einer Station: {feld: {'YYYY-MM-DDTHH': wert}} (UTC).

    Felder siehe SERIES_FIELDS. Aggregiert die 30-Minuten-Werte auf Stunden
    (letzter Wert je Stunde). Die API liefert hoechstens 7 Tage zurueck.
    """
    cache_dir = cache_dir or (DATA_DIR / "slf_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # API erlaubt nur period_in_days in {1,3,7}; auf naechstgroesseren Wert klemmen.
    period_days = 1 if period_days <= 1 else 3 if period_days <= 3 else 7
    # Stunde im Dateinamen: ein Cache von gestern wuerde sonst ewig die alte
    # Reihe liefern (lokal; in CI ist das Verzeichnis ohnehin frisch).
    stamp = datetime.utcnow().strftime("%Y%m%d%H")
    cache = cache_dir / f"{code}_{period_days}d_{stamp}.json"
    import json
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    url = f"{_BASE}/public/api/imis/station/{code}/measurements?period_in_days={period_days}"
    try:
        rows = requests.get(url, timeout=timeout).json()
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    out: Dict[str, Dict[str, float]] = {k: {} for k in SERIES_FIELDS}
    for r in sorted(rows, key=lambda r: r.get("measure_date", "")):
        d = r.get("measure_date", "")
        if len(d) < 13:
            continue
        for key, field_name in SERIES_FIELDS.items():
            v = _num(r.get(field_name))
            if v is not None:
                out[key][d[:13]] = v  # letzter 30-min-Wert der Stunde gewinnt
    out = {k: v for k, v in out.items() if v}
    try:
        cache.write_text(json.dumps(out))
    except Exception:
        pass
    return out


def fetch_hs_series(code: str, period_days: int, timeout: float = 40.0,
                    cache_dir: Path | None = None) -> Dict[str, float]:
    """Stuendliche Schneehoehe (cm) einer Station, key 'YYYY-MM-DDTHH' (UTC)."""
    return fetch_series(code, period_days, timeout, cache_dir).get("hs", {})


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
