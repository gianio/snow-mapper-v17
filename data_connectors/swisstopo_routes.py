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


# The live GeoPackage puts literal placeholder strings in the name column --
# "Keine Routeninfo verfügbar" came back as the name of the first route. That
# is a data value, not a name, and showing it as a tour title would be worse
# than showing nothing.
_PLACEHOLDERS = ("keine routeninfo", "keine angabe", "no route info",
                 "pas d'info", "nessuna info", "unbekannt", "unknown", "n/a")


def _is_placeholder(v: str) -> bool:
    low = v.strip().lower()
    return any(low.startswith(p) for p in _PLACEHOLDERS)


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
            if isinstance(v, str) and v.strip() and not _is_placeholder(v):
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
        print("[TOPO] fiona fehlt -- GeoPackage kann nicht gelesen werden "
              "(steht in requirements.txt).")
        return []
    try:
        feats = []
        with fiona.open(str(path)) as src:
            # WICHTIG: swisstopo liefert LV95 (EPSG:2056), nicht WGS84 -- der
            # Dateiname sagt es sogar (skitouren_2056.gpkg). Bestaetigt an der
            # Live-Datei: die erste Koordinate war [2573311.9, 1080362.5].
            # Ohne Umprojektion landen alle Routen irgendwo weit ausserhalb
            # der Karte, weil der Client Grad erwartet.
            src_crs = None
            try:
                src_crs = src.crs.to_string() if hasattr(src.crs, "to_string") else str(src.crs)
            except Exception:                     # noqa: BLE001
                pass
            for rec in src:
                feats.append({"id": rec.get("id"),
                              "properties": dict(rec.get("properties") or {}),
                              "geometry": dict(rec["geometry"])})
        routes = parse_routes({"features": feats})
        return _to_wgs84(routes, src_crs)
    except Exception as e:                        # noqa: BLE001
        print(f"[TOPO] GeoPackage {path.name} nicht lesbar: {e!r}")
        return []


def _to_wgs84(routes, src_crs):
    """Routen nach WGS84 umprojizieren, falls noetig."""
    if not routes:
        return routes
    x, y = routes[0]["coords"][0]
    looks_degrees = -180 <= x <= 180 and -90 <= y <= 90
    if looks_degrees and not (src_crs and "2056" in str(src_crs)):
        return routes
    crs = src_crs or "EPSG:2056"
    try:
        from pyproj import Transformer
        tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    except Exception as e:                        # noqa: BLE001
        print(f"[TOPO] Umprojektion von {crs} nicht moeglich: {e!r}")
        return []
    print(f"[TOPO] Routen von {crs} nach EPSG:4326 umprojiziert.")
    for r in routes:
        xs = [c[0] for c in r["coords"]]
        ys = [c[1] for c in r["coords"]]
        lon, lat = tf.transform(xs, ys)
        r["coords"] = [[float(a), float(b)] for a, b in zip(lon, lat)]
    return routes


def _routes_from_zip_url(href: str, timeout: float) -> List[Dict[str, Any]]:
    """Ein gezipptes Shapefile/GeoPackage von data.geo.admin.ch lesen.

    swisstopo liefert die Vektorfassung als ZIP (bestaetigt gegen die
    STAC-Sammlung). Wird in ein temporaeres Verzeichnis entpackt und ueber
    fiona gelesen; ohne fiona gibt es nichts zu tun, und das wird gesagt
    statt stillschweigend nichts zurueckzugeben.
    """
    import shutil
    import tempfile
    import zipfile

    import requests

    try:
        import fiona                                # noqa: F401
    except ImportError:
        print("[TOPO] fiona fehlt -- gezippte Vektordaten koennen nicht "
              "gelesen werden (steht in requirements.txt).")
        return []

    tmp = Path(tempfile.mkdtemp(prefix="skitouren-"))
    try:
        zpath = tmp / "routes.zip"
        with requests.get(href, timeout=timeout, stream=True) as rr:
            rr.raise_for_status()
            with open(zpath, "wb") as fh:
                for chunk in rr.iter_content(1 << 20):
                    fh.write(chunk)
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            print(f"[TOPO] ZIP {href.rsplit('/', 1)[-1]}: {len(names)} Dateien")
            # GeoPackage bevorzugen (eine Datei, ein Layer-Katalog),
            # sonst Shapefile -- das braucht seine Begleitdateien, also alles
            # entpacken statt nur die .shp.
            inner = ([n for n in names if n.lower().endswith(".gpkg")]
                     or [n for n in names if n.lower().endswith(".shp")])
            if not inner:
                print(f"[TOPO] Keine .gpkg/.shp im ZIP: {names[:12]}")
                return []
            zf.extractall(tmp)
        return load_routes(tmp / inner[0])
    except Exception as e:                           # noqa: BLE001
        print(f"[TOPO] ZIP {href} nicht lesbar: {e!r}")
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        hrefs = [a["href"] for it in items
                 for a in (it.get("assets") or {}).values()
                 if isinstance(a, dict) and isinstance(a.get("href"), str)]
        # Verified against the live STAC collection on 15 Sep 2026: the single
        # item carries TWO assets and both are .zip -- there is no GeoJSON to
        # fetch. The reader previously filtered for .geojson/.json only and so
        # found nothing while reporting "no vector version available", which
        # was misleading: the data is there, just packaged.
        hrefs.sort(key=lambda h: (not h.lower().endswith((".geojson", ".json")),
                                  not h.lower().endswith(".zip")))
        for href in hrefs:
            low = href.lower()
            if low.endswith((".geojson", ".json")):
                rr = requests.get(href, timeout=timeout)
                rr.raise_for_status()
                routes = parse_routes(rr.json())
            elif low.endswith(".zip"):
                routes = _routes_from_zip_url(href, timeout)
            else:
                continue
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
