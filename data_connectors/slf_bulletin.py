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


# The live API (checked 13 Nov 2026) writes the level as an EAWS word.
_LEVEL_WORDS = {"low": 1, "moderate": 2, "considerable": 3, "high": 4, "very_high": 5,
                "veryhigh": 5}
PROBLEM_DE = {"new_snow": "Neuschnee", "wind_slab": "Triebschnee",
              "persistent_weak_layers": "Altschnee", "wet_snow": "Nassschnee",
              "gliding_snow": "Gleitschnee", "favourable_situation": "günstige Situation",
              "no_distinct_avalanche_problem": "kein ausgeprägtes Problem"}


def _as_int(v) -> Optional[int]:
    if isinstance(v, str) and v.strip().lower().replace(" ", "_") in _LEVEL_WORDS:
        return _LEVEL_WORDS[v.strip().lower().replace(" ", "_")]
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if 1 <= i <= 5 else None


def _round_geom(g, nd=4):
    """Coordinates to ~10 m: the region outlines are drawn, not measured."""
    if not isinstance(g, dict):
        return g
    def r(c):
        if isinstance(c, (int, float)):
            return round(c, nd)
        return [r(x) for x in c]
    out = dict(g)
    if "coordinates" in g:
        out["coordinates"] = r(g["coordinates"])
    return out


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


def _bulletin_entry(b: dict) -> Optional[Dict[str, Any]]:
    """One CAAML bulletin -> one entry covering all its regions."""
    ratings = _first(b, "dangerRatings", "danger_ratings", default=[]) or []
    levels, sub = [], ""
    for r in ratings:
        if not isinstance(r, dict):
            continue
        lv = _as_int(_first(r, "mainValue", "main_value", "value"))
        if lv is None:
            continue
        levels.append(lv)
        sd = (((r.get("customData") or {}).get("CH") or {}).get("subdivision") or "")
        if lv == max(levels) and sd:
            sub = {"plus": "+", "minus": "-", "neutral": "="}.get(sd, "")
    if not levels:
        return None
    problems = _first(b, "avalancheProblems", "avalanche_problems", default=[]) or []
    aspects, elev_lo, elev_hi, kinds, core = [], None, None, [], ""
    for pr in problems:
        if not isinstance(pr, dict):
            continue
        kind = _first(pr, "problemType", "problem_type", "type")
        if isinstance(kind, str):
            kinds.append(PROBLEM_DE.get(kind, kind))
        aspects += _norm_aspects(_first(pr, "aspects", "aspect"))
        el = _first(pr, "elevation", default={}) or {}
        lo = _first(el, "lowerBound", "lower_bound")
        hi = _first(el, "upperBound", "upper_bound")
        lo = int(lo) if isinstance(lo, (int, float)) or (isinstance(lo, str) and lo.isdigit()) else None
        hi = int(hi) if isinstance(hi, (int, float)) or (isinstance(hi, str) and hi.isdigit()) else None
        if lo is not None:
            elev_lo = lo if elev_lo is None else min(elev_lo, lo)
        if hi is not None:
            elev_hi = hi if elev_hi is None else max(elev_hi, hi)
        ct = (((pr.get("customData") or {}).get("CH") or {}).get("coreZoneText") or "")
        if ct and not core:
            core = ct
    regs = [r for r in (_first(b, "regions", "region", default=[]) or []) if isinstance(r, dict)]
    return {
        "id": _first(b, "bulletinID", "bulletinId", default=None)
              or ",".join(str(r.get("regionID")) for r in regs),
        "danger": max(levels), "danger_lo": min(levels), "sub": sub,
        "aspects": [a for a in _ASPECT_ORDER if a in aspects],
        "elev_lo": elev_lo, "elev_hi": elev_hi,
        "problems": list(dict.fromkeys(kinds)), "core": core,
        "regions": [r.get("name") for r in regs if r.get("name")],
        "valid": _first(_first(b, "validTime", "validity_time", default={}) or {},
                        "startTime", "start_time"),
        "until": _first(_first(b, "validTime", "validity_time", default={}) or {},
                        "endTime", "end_time"),
    }


def _parse_geojson(fc: dict) -> List[Dict[str, Any]]:
    """GeoJSON: one feature per bulletin -- CAAML properties (dangerRatings,
    avalancheProblems, regions) and the merged outline of its regions."""
    feats = fc.get("features")
    if not isinstance(feats, list):
        return []
    out = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        p = f.get("properties") or {}
        if p.get("dangerRatings") is not None:
            ent = _bulletin_entry(p)
            if ent is not None:
                ent["geometry"] = _round_geom(f.get("geometry"))
                out.append(ent)
            continue
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


def _looks_empty(payload) -> bool:
    """A well-formed response that simply carries no bulletin.

    Distinguishes "no danger ratings published today" (summer) from "the
    response shape changed and the parser is broken" -- which otherwise look
    identical from the outside: zero regions either way.
    """
    if isinstance(payload, dict):
        if payload.get("type") == "FeatureCollection":
            return payload.get("features") == []
        for key in ("bulletins", "Bulletin", "bulletin"):
            if key in payload:
                return payload[key] in ([], None)
    return payload == []


def fetch_bulletin(lang: str = "de", timeout: float = 20.0,
                   cache_path: Path | None = None) -> Optional[Dict[str, Any]]:
    """Bulletin holen. Gibt ``None`` zurueck, wenn es nicht erreichbar ist.

    Niemals eine Exception nach aussen: ein SLF-Ausfall darf den
    Modelllauf nicht abbrechen, genauso wie bei den IMIS-Stationen.
    """
    import requests

    urls = [f"{_BASE}/caaml/{lang}/geojson", f"{_BASE}/caaml/{lang}/json"]
    empty_ok = False
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            payload = r.json()
            regions = parse_bulletin(payload)
            if not regions:
                # A 200 with an empty collection is the NORMAL summer answer:
                # Switzerland has no avalanche bulletin outside roughly
                # November-May. Verified against the live API on 15 Sep 2026,
                # which returned {"type":"FeatureCollection","features":[]}
                # and {"bulletins":[]}. That is not a parse failure, and
                # reporting it as one sent me looking for a bug that was not
                # there -- so distinguish the two.
                if _looks_empty(payload):
                    empty_ok = True
                    print(f"[SLF] Bulletin {url}: keine aktive Gefahrenstufe "
                          f"(ausserhalb der Lawinensaison).")
                else:
                    print(f"[SLF] Bulletin {url}: Antwort nicht interpretierbar "
                          f"-- Struktur weicht ab.")
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

    if empty_ok:
        # Out of season: the layer stays hidden, and a stale winter bulletin
        # would be worse than nothing.
        return None

    if cache_path and cache_path.exists():
        try:
            doc = json.loads(cache_path.read_text(encoding="utf-8"))
            doc["stale"] = True
            print("[SLF] Bulletin: letzte gecachte Fassung (stale).")
            return doc
        except (OSError, ValueError):
            pass
    return None
