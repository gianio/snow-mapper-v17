"""Gipfelnamen aus GeoNames (CC BY 4.0) fuer die Benennung der Skitouren.

Eine Datei fuer die ganze Schweiz (download.geonames.org/export/dump/CH.zip,
~3 MB), daraus nur die Gipfel und Berge (Feature-Klasse T, Codes PK, PKS,
MT, MTS, HLL). Lizenz: CC BY 4.0, Quellenangabe "GeoNames". Faellt bei
jedem Fehler auf [] zurueck -- die Touren behalten dann ihren swisstopo-Namen.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

URL = "https://download.geonames.org/export/dump/CH.zip"
CODES = {"PK", "PKS", "MT", "MTS", "HLL", "SPUR"}
ATTRIBUTION = "Gipfel © GeoNames (CC BY 4.0)"

Peak = Tuple[str, float, float, Optional[float]]   # name, lat, lon, elevation


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


def fetch_peaks(cache_path: Optional[Path] = None, timeout: float = 60.0) -> List[Peak]:
    if cache_path and cache_path.exists():
        try:
            return [tuple(p) for p in json.loads(cache_path.read_text(encoding="utf-8"))]
        except Exception:                          # noqa: BLE001
            pass
    try:
        import requests
        r = requests.get(URL, timeout=timeout)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            text = z.read("CH.txt").decode("utf-8")
        peaks = parse(text)
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
