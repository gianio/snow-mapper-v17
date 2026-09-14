"""swisstopo Ski- und Schneeschuhrouten (Vektorgeometrie).

Lizenz
------
swisstopo-Basisgeodaten sind seit 1. Maerz 2021 **OGD**: kostenlos fuer
jeden Zweck, auch kommerziell, veraenderbar und weiterverteilbar. Einzige
Pflicht ist die Quellenangabe -- "Quelle: Bundesamt fuer Landestopografie
swisstopo" oder "© swisstopo", entweder neben den Daten oder zentral auf
einer Quellenseite mit Link. Siehe ``ATTRIBUTION``.

Zwei Zugriffsebenen
-------------------
* **WMTS** (``ch.swisstopo-karto.skitouren``) -- fertig gerenderte Tiles,
  reicht fuer einen Anzeige-Layer und ist im Client schon eingebaut.
* **Vektor** (dieses Modul) -- die Liniengeometrie, die man braucht, um
  entlang einer Route zu rechnen (siehe ``model/tour_score.py``).

Wichtig zur Datenqualitaet
--------------------------
Das sind *kartografische* Routen aus den Schneesportkarten 1:50'000, keine
GPS-Tracks. Sie sind Richtungsangaben, nicht metergenaue Spuren. Fuer eine
Schneequalitaets-Statistik entlang einer Route ist das ausreichend; als
Navigationsgrundlage waere es falsch, und die App soll das auch nicht
suggerieren.

Offline-Betrieb
---------------
Der Download braucht Netz und GDAL/fiona. Beides ist in der Build-Umgebung
nicht garantiert, deshalb ist der Pfad so gebaut, dass ein Fehlschlag eine
leere Liste ergibt und der Layer ausgeblendet bleibt -- nie ein Abbruch.
Eine heruntergeladene Fassung wird als GeoJSON gecacht.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

ATTRIBUTION = "Skitouren © swisstopo"
WMTS_SKITOUR = "ch.swisstopo-karto.skitouren"
WMTS_SNOWSHOE = "ch.swisstopo-karto.schneeschuhrouten"
# STAC-Sammlung, ueber die swisstopo die Vektorfassung ausliefert.
STAC_COLLECTION = ("https://data.geo.admin.ch/api/stac/v0.9/collections/"
                   "ch.swisstopo-karto.skitouren/items")


def _linestrings(geom: Any) -> List[List[list]]:
    """LineString / MultiLineString -> Liste von Koordinatenlisten."""
    if not isinstance(geom, dict):
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "LineString" and isinstance(c, list):
        return [c]
    if t == "MultiLineString" and isinstance(c, list):
        return [ls for ls in c if isinstance(ls, list)]
    return []


def parse_routes(fc: Any, min_points: int = 4) -> List[Dict[str, Any]]:
    """GeoJSON-FeatureCollection -> Routenliste.

    Defensiv: was keine brauchbare Linie ist, fliegt raus statt geraten zu
    werden. Sehr kurze Fragmente (Kartenschnipsel an Blattraendern) werden
    verworfen -- sie ergeben keine sinnvolle Streckenstatistik.
    """
    if not isinstance(fc, dict):
        return []
    feats = fc.get("features")
    if not isinstance(feats, list):
        return []
    out: List[Dict[str, Any]] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        name = None
        for k in ("name", "NAME", "bezeichnung", "route_name", "label"):
            v = props.get(k)
            if isinstance(v, str) and v.strip():
                name = v.strip()
                break
        for i, coords in enumerate(_linestrings(f.get("geometry"))):
            pts = [c for c in coords
                   if isinstance(c, (list, tuple)) and len(c) >= 2
                   and all(isinstance(x, (int, float)) for x in c[:2])]
            if len(pts) < min_points:
                continue
            rid = f.get("id") or props.get("id") or f"route-{len(out)}"
            out.append({
                "id": f"{rid}-{i}" if i else str(rid),
                "name": name,
                "coords": [[float(c[0]), float(c[1])] for c in pts],
            })
    return out


def load_routes(path: Path) -> List[Dict[str, Any]]:
    """Routen aus einer lokalen GeoJSON- oder GeoPackage-Datei lesen."""
    if not path.exists():
        return []
    if path.suffix.lower() in (".json", ".geojson"):
        try:
            return parse_routes(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"[TOPO] Routen {path.name} nicht lesbar: {e!r}")
            return []
    # GeoPackage -> ueber fiona, wenn vorhanden.
    try:
        import fiona
    except ImportError:
        print("[TOPO] fiona fehlt -- GeoPackage kann nicht gelesen werden.")
        return []
    try:
        feats = []
        with fiona.open(str(path)) as src:
            for rec in src:
                feats.append({"id": rec.get("id"),
                              "properties": dict(rec.get("properties") or {}),
                              "geometry": dict(rec["geometry"])})
        return parse_routes({"features": feats})
    except Exception as e:                        # noqa: BLE001
        print(f"[TOPO] GeoPackage {path.name} nicht lesbar: {e!r}")
        return []


def fetch_routes(cache_path: Optional[Path] = None,
                 timeout: float = 60.0) -> List[Dict[str, Any]]:
    """Routen beschaffen: Cache, sonst Download ueber die STAC-Sammlung.

    Gibt bei jedem Fehler ``[]`` zurueck -- ein swisstopo-Ausfall darf den
    Modelllauf nicht abbrechen.
    """
    if cache_path and cache_path.exists():
        routes = load_routes(cache_path)
        if routes:
            print(f"[TOPO] {len(routes)} Routen aus dem Cache.")
            return routes
    try:
        import requests
        r = requests.get(STAC_COLLECTION, timeout=timeout)
        r.raise_for_status()
        items = (r.json() or {}).get("features") or []
        # Die STAC-Items verlinken die eigentlichen Dateien in ihren Assets.
        hrefs = [a.get("href") for it in items
                 for a in (it.get("assets") or {}).values()
                 if isinstance(a, dict) and isinstance(a.get("href"), str)
                 and a["href"].lower().endswith((".geojson", ".json"))]
        for href in hrefs:
            rr = requests.get(href, timeout=timeout)
            rr.raise_for_status()
            routes = parse_routes(rr.json())
            if routes:
                if cache_path:
                    try:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_text(json.dumps(
                            {"type": "FeatureCollection", "features": [
                                {"type": "Feature", "id": x["id"],
                                 "properties": {"name": x["name"]},
                                 "geometry": {"type": "LineString",
                                              "coordinates": x["coords"]}}
                                for x in routes]}), encoding="utf-8")
                    except OSError:
                        pass
                print(f"[TOPO] {len(routes)} Skitouren-Routen geladen.")
                return routes
        print("[TOPO] Keine Vektorfassung in der STAC-Sammlung gefunden.")
    except Exception as e:                        # noqa: BLE001
        print(f"[TOPO] Routen-Download fehlgeschlagen: {e!r}")
    return []
