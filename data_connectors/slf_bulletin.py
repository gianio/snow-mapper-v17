"""SLF-Lawinenbulletin-Connector.

Quelle: https://aws.slf.ch/api/bulletin -- oeffentlich, ohne Authentifizierung,
**CC BY 4.0** (dieselben Bedingungen wie die IMIS-Messdaten in
``slf_stations.py``). Namensnennung: "Quelle: WSL-Institut fuer Schnee- und
Lawinenforschung SLF". Wer die Daten produktiv nutzt, sollte sich unter
data@slf.ch auf die Liste "Lawinenbulletin" setzen lassen, um ueber
Aenderungen und Ausfaelle informiert zu werden.

Datenmodell
-----------
Das Bulletin ist nach EAWS/CAAML aufgebaut: die Schweiz ist in ~150
Warnregionen geteilt, jede mit

  * Gefahrenstufe 1-5 (ggf. getrennt ober-/unterhalb einer Hoehengrenze),
  * "Kernzone": Hoehenband + Expositionen, in denen die Gefahr konzentriert
    ist -- das ist das Feld, das fuer eine Tourenbewertung zaehlt,
  * Lawinenprobleme (Triebschnee, Neuschnee, Nassschnee, Altschnee,
    Gleitschnee).

WICHTIG -- Abgrenzung
---------------------
Dieses Modul *zeigt* das Bulletin, es *rechnet* keine eigene Lawinengefahr.
Das ist bewusst: eine eigene Gefahreneinschaetzung zu veroeffentlichen ist
Prognose mit der entsprechenden Haftung. Ein attribuierter Viewer mit Link
auf das Original ist es nicht.

Robustheit
----------
Der Aufruf ist so gebaut, dass ein Ausfall die Pipeline nie stoppt: bei
Fehlern gibt ``fetch_bulletin`` ``None`` zurueck und der Layer bleibt
ausgeblendet (wie ``dmAvailable()`` beim Messaging). Die Feldnamen sind
gegen mehrere Schreibweisen tolerant, weil die Antwortform hier nicht
gegen die echte API verifiziert werden konnte -- siehe
``tools/test_slf_bulletin.py`` und ``docs/slf-parity-and-tiering.md``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_BASE = "https://aws.slf.ch/api/bulletin"
ATTRIBUTION = "Lawinenbulletin © SLF (CC BY 4.0)"
BULLETIN_URL = "https://www.slf.ch/de/lawinenbulletin-und-schneesituation/"

# EAWS-Standardfarben der Gefahrenstufen. Nicht erfinden -- das sind die
# Farben, die Tourengeher aus dem Bulletin kennen.
DANGER_COLORS = {
    1: "#CCFF66",   # gering
    2: "#FFFF00",   # maessig
    3: "#FF9900",   # erheblich
    4: "#FF0000",   # gross
    5: "#800000",   # sehr gross
}
DANGER_LABELS = {
    1: "gering", 2: "mässig", 3: "erheblich", 4: "gross", 5: "sehr gross",
}

_ASPECT_ORDER = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _first(d: dict, *names, default=None):
    """Erster vorhandener Key -- die CAAML/JSON-Varianten benennen
    dasselbe Feld unterschiedlich (camelCase vs snake_case)."""
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return default


def _as_int(v) -> Optional[int]:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if 1 <= i <= 5 else None


def _norm_aspects(raw) -> List[str]:
    """Expositionen auf N/NE/.../NW normalisieren."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for a in raw:
        if not isinstance(a, str):
            continue
        k = a.strip().upper().replace("-", "").replace("_", "")
        # CAAML schreibt teils "NORTH", "NORTH_EAST"
        k = (k.replace("NORTHEAST", "NE").replace("NORTHWEST", "NW")
              .replace("SOUTHEAST", "SE").replace("SOUTHWEST", "SW")
              .replace("NORTH", "N").replace("SOUTH", "S")
              .replace("EAST", "E").replace("WEST", "W"))
        if k in _ASPECT_ORDER and k not in out:
            out.append(k)
    return [a for a in _ASPECT_ORDER if a in out]


def parse_bulletin(payload: Any) -> List[Dict[str, Any]]:
    """CAAML-JSON/GeoJSON -> flache Regionenliste.

    Bewusst defensiv: unbekannte Formen werden uebersprungen, nicht geraten.
    Eine Region ohne Gefahrenstufe ist wertlos und fliegt raus.
    """
    bulletins = None
    if isinstance(payload, dict):
        bulletins = _first(payload, "bulletins", "Bulletin", "bulletin")
        if bulletins is None and payload.get("type") == "FeatureCollection":
            return _parse_geojson(payload)
    elif isinstance(payload, list):
        bulletins = payload
    if not isinstance(bulletins, list):
        return []

    out: List[Dict[str, Any]] = []
    for b in bulletins:
        if not isinstance(b, dict):
            continue
        ratings = _first(b, "dangerRatings", "danger_ratings", default=[]) or []
        levels = [_as_int(_first(r, "mainValue", "main_value", "value"))
                  for r in ratings if isinstance(r, dict)]
        levels = [x for x in levels if x is not None]
        if not levels:
            continue

        # Kernzone: Hoehenband + Expositionen des staerksten Problems.
        problems = _first(b, "avalancheProblems", "avalanche_problems", default=[]) or []
        aspects, elev_lo, elev_hi, kinds = [], None, None, []
        for pr in problems:
            if not isinstance(pr, dict):
                continue
            kind = _first(pr, "problemType", "problem_type", "type")
            if isinstance(kind, str):
                kinds.append(kind)
            aspects += _norm_aspects(_first(pr, "aspects", "aspect"))
            el = _first(pr, "elevation", default={}) or {}
            lo = _first(el, "lowerBound", "lower_bound")
            hi = _first(el, "upperBound", "upper_bound")
            if isinstance(lo, (int, float)):
                elev_lo = lo if elev_lo is None else min(elev_lo, lo)
            if isinstance(hi, (int, float)):
                elev_hi = hi if elev_hi is None else max(elev_hi, hi)

        for reg in _first(b, "regions", "region", default=[]) or []:
            rid = _first(reg, "regionID", "regionId", "region_id", "id") if isinstance(reg, dict) else reg
            if not isinstance(rid, str):
                continue
            out.append({
                "id": rid,
                "danger": max(levels),
                "danger_lo": min(levels),
                "aspects": [a for a in _ASPECT_ORDER if a in aspects],
                "elev_lo": elev_lo,
                "elev_hi": elev_hi,
                "problems": sorted(set(kinds)),
                "valid": _first(_first(b, "validTime", "validity_time", default={}) or {},
                                "startTime", "start_time"),
            })
    return out


def _parse_geojson(fc: dict) -> List[Dict[str, Any]]:
    """GeoJSON-Variante: Gefahrenstufe steckt in den Feature-Properties."""
    feats = fc.get("features")
    if not isinstance(feats, list):
        return []
    out = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        p = f.get("properties") or {}
        lvl = _as_int(_first(p, "danger_level", "dangerLevel", "danger", "max_danger"))
        rid = _first(p, "region_id", "regionID", "id")
        if lvl is None or not isinstance(rid, str):
            continue
        out.append({
            "id": rid, "danger": lvl, "danger_lo": lvl,
            "aspects": _norm_aspects(_first(p, "aspects", "aspect")),
            "elev_lo": _first(p, "elevation_lower", "elev_lo"),
            "elev_hi": _first(p, "elevation_upper", "elev_hi"),
            "problems": [], "valid": _first(p, "valid_from", "start_time"),
            "geometry": f.get("geometry"),
        })
    return out


def fetch_bulletin(lang: str = "de", timeout: float = 20.0,
                   cache_path: Path | None = None) -> Optional[Dict[str, Any]]:
    """Bulletin holen. Gibt ``None`` zurueck, wenn es nicht erreichbar ist.

    Niemals eine Exception nach aussen: ein SLF-Ausfall darf den
    Modelllauf nicht abbrechen, genauso wie bei den IMIS-Stationen.
    """
    import requests

    urls = [f"{_BASE}/caaml/{lang}/geojson", f"{_BASE}/caaml/{lang}/json"]
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            regions = parse_bulletin(r.json())
            if not regions:
                continue
            doc = {"regions": regions, "source": url,
                   "attribution": ATTRIBUTION, "url": BULLETIN_URL}
            if cache_path:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(doc), encoding="utf-8")
                except OSError:
                    pass
            return doc
        except Exception as e:                      # noqa: BLE001
            print(f"[SLF] Bulletin {url} nicht verfuegbar: {e!r}")

    if cache_path and cache_path.exists():
        try:
            doc = json.loads(cache_path.read_text(encoding="utf-8"))
            doc["stale"] = True
            print("[SLF] Bulletin: letzte gecachte Fassung (stale).")
            return doc
        except (OSError, ValueError):
            pass
    return None
