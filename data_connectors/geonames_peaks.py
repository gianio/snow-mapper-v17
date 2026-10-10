"""Gipfelnamen aus GeoNames (CC BY 4.0) fuer die Benennung der Skitouren.

Eine Datei fuer die ganze Schweiz (download.geonames.org/export/dump/CH.zip,
~3 MB), daraus nur die Gipfel und Berge (Feature-Klasse T, Codes PK, PKS,
MT, MTS, HLL). Lizenz: CC BY 4.0, Quellenangabe "GeoNames". Faellt bei
jedem Fehler auf [] zurueck -- die Touren behalten dann ihren swisstopo-Namen.
"""
from __future__ import annotations

import io
import re
import json
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

URL = "https://download.geonames.org/export/dump/CH.zip"
CODES = {"PK", "PKS", "MT", "MTS", "HLL", "SPUR"}
ATTRIBUTION = "Gipfel © GeoNames (CC BY 4.0)"

Peak = Tuple[str, float, float, Optional[float]]   # name, lat, lon, elevation
# Where a tour starts: villages and hamlets, passes, hospices/hotels, huts,
# stations and named localities (alps, valley floors).
PLACE_CODES = {"T": {"PASS"}, "S": {"HTL", "HUT", "HUTS", "RSTN", "RSTP", "BUSTN"}, "L": {"LCTY"}}
Place = Tuple[str, float, float, str]                # name, lat, lon, kind


def parse(text: str) -> List[Peak]:
    """GeoNames dump lines -> peaks. Columns: 1 name, 4 lat, 5 lon,
    6 class, 7 code, 15 elevation, 16 dem."""
    out: List[Peak] = []
    for line in text.splitlines():
        c = line.split("\t")
        if len(c) < 17 or c[6] != "T" or c[7] not in CODES:
            continue
        try:
            lat, lon = float(c[4]), float(c[5])
        except ValueError:
            continue
        el = None
        for v in (c[15], c[16]):
            try:
                el = float(v)
                if el > -1000:
                    break
            except ValueError:
                pass
        out.append((c[1], lat, lon, el))
    return out


def parse_places(text: str) -> List[Place]:
    """GeoNames dump lines -> start places (see PLACE_CODES; class P = any)."""
    out: List[Place] = []
    for line in text.splitlines():
        c = line.split("\t")
        if len(c) < 8:
            continue
        cls, code = c[6], c[7]
        if not (cls == "P" or code in PLACE_CODES.get(cls, ())):
            continue
        try:
            out.append((c[1], float(c[4]), float(c[5]), code))
        except ValueError:
            continue
    return out


_TEXT: Optional[str] = None


def _dump(timeout: float) -> str:
    """CH.txt from GeoNames, downloaded once per run for peaks and places."""
    global _TEXT
    if _TEXT is None:
        import requests
        r = requests.get(URL, timeout=timeout)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            _TEXT = z.read("CH.txt").decode("utf-8")
    return _TEXT


def fetch_places(cache_path: Optional[Path] = None, timeout: float = 60.0) -> List[Place]:
    if cache_path and cache_path.exists():
        try:
            return [tuple(p) for p in json.loads(cache_path.read_text(encoding="utf-8"))]
        except Exception:                          # noqa: BLE001
            pass
    try:
        places = parse_places(_dump(timeout))
    except Exception as e:                         # noqa: BLE001
        print(f"[PEAKS] GeoNames-Orte nicht verfuegbar: {e!r}")
        return []
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(places), encoding="utf-8")
        except Exception:                          # noqa: BLE001
            pass
    print(f"[PEAKS] {len(places)} Orte aus GeoNames.")
    return places


def fetch_peaks(cache_path: Optional[Path] = None, timeout: float = 60.0) -> List[Peak]:
    if cache_path and cache_path.exists():
        try:
            return [tuple(p) for p in json.loads(cache_path.read_text(encoding="utf-8"))]
        except Exception:                          # noqa: BLE001
            pass
    try:
        peaks = parse(_dump(timeout))
    except Exception as e:                         # noqa: BLE001
        print(f"[PEAKS] GeoNames nicht verfuegbar: {e!r}")
        return []
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(peaks), encoding="utf-8")
        except Exception:                          # noqa: BLE001
            pass
    print(f"[PEAKS] {len(peaks)} Gipfel aus GeoNames.")
    return peaks


class PeakIndex:
    """Nearest named peak to a point, in metres (planar, fine at CH scale)."""

    def __init__(self, peaks: List[Peak]):
        self.peaks = peaks
        self.tree = None
        if peaks:
            from scipy.spatial import cKDTree
            self.tree = cKDTree([(p[2] * 76000.0, p[1] * 111000.0) for p in peaks])

    def nearest(self, lon: float, lat: float, max_m: float = 900.0) -> Optional[Peak]:
        if self.tree is None:
            return None
        d, i = self.tree.query((lon * 76000.0, lat * 111000.0))
        return self.peaks[int(i)] if d <= max_m else None


_REGION_SUFFIX = re.compile(r"(?i)[\s-]*(pass|hospiz|ospizio|hospice|dorf|platz)$")


def _region_of(name: str) -> str:
    """'Flüelapass' -> 'Flüela', 'Flüela Hospiz' -> 'Flüela'."""
    core = _REGION_SUFFIX.sub("", name).strip()
    return core or name


def name_tours(routes: List[dict], peaks: List[Peak], places: List[Place],
               peak_m: float = 900.0, start_m: float = 4000.0,
               region_m: float = 12000.0) -> List[dict]:
    """Keep only tours that lead to a named summit, and name them so that
    several tours to one summit read as its variants.

    Each kept route gets
      peak   [lon, lat, elev] of the summit,
      group  one key per summit (its variants share it),
      pname  the summit's display name -- with its region in front when two
             summits share a name ("Flüela Schwarzhorn"),
      from   the nearest start place to the valley end ("Flüela Hospiz"),
      variant "von Flüela Hospiz" (or "ab 1850 m" without a place),
      name   pname, plus " von ..." when the summit has more than one tour.
    Without any peak data nothing is dropped (the routes keep their names).
    """
    pidx, sidx = PeakIndex(peaks), PeakIndex(places)
    if pidx.tree is None:
        return routes
    kept = []
    for r in routes:
        hi, lo = r.get("hi"), r.get("lo")
        pk = pidx.nearest(hi[0], hi[1], peak_m) if hi else None
        if not pk:
            continue
        if r.get("name") and r["name"] != pk[0] and not r.get("route"):
            r["route"] = r["name"]
        r["peak"] = [pk[2], pk[1], pk[3]]
        r["group"] = f"{pk[0]}@{pk[1]:.4f},{pk[2]:.4f}"
        st = sidx.nearest(lo[0], lo[1], start_m) if lo else None
        if st:
            r["from"] = st[0]
        kept.append((r, pk))
    # a summit name used by several different summits needs its region
    by_name: dict = {}
    for r, pk in kept:
        by_name.setdefault(pk[0], set()).add(r["group"])
    region_cache: dict = {}
    for r, pk in kept:
        nm = pk[0]
        if len(by_name[nm]) > 1:
            g = r["group"]
            if g not in region_cache:
                # nearest pass or locality, else village, around the summit
                near = None
                if sidx.tree is not None:
                    ds, ii = sidx.tree.query((pk[2] * 76000.0, pk[1] * 111000.0), k=min(25, len(places)))
                    cands = [(d, places[int(i)]) for d, i in zip(ds if hasattr(ds, "__len__") else [ds],
                                                               ii if hasattr(ii, "__len__") else [ii])
                             if d <= region_m]
                    pref = [c for c in cands if c[1][3] in ("PASS", "LCTY")] or cands
                    near = pref[0][1][0] if pref else None
                reg = _region_of(near) if near else None
                region_cache[g] = (f"{reg} {nm}" if reg and reg.lower() not in nm.lower()
                                   else (f"{nm} ({round(pk[3])} m)" if pk[3] else nm))
            nm = region_cache[g]
        r["pname"] = nm
    groups: dict = {}
    for r, _ in kept:
        groups.setdefault(r["group"], []).append(r)
    for g, rs in groups.items():
        seen: dict = {}
        for r in rs:
            v = (f"von {r['from']}" if r.get("from")
                 else (f"ab {round(r['lo'][2])} m" if r.get("lo") and len(r["lo"]) > 2 else "Variante"))
            seen[v] = seen.get(v, 0) + 1
            if seen[v] > 1:
                v = f"{v} ({seen[v]})"
            r["variant"] = v
            r["name"] = f"{r['pname']} {v}" if len(rs) > 1 else r["pname"]
            r["nvar"] = len(rs)
    return [r for r, _ in kept]
