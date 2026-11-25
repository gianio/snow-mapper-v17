#!/usr/bin/env python3
"""Avalanche warning regions of the neighbouring countries (EAWS micro-regions,
CC0, regions.avalanches.org) -> a small file the app embeds: id, name and a
simplified outline per region, for the Alps around Switzerland.

The app draws them coloured by the day's danger level from the EAWS ratings
(static.avalanche.report/eaws_bulletins/<date>/<date>.ratings.json), which
each warning service publishes: Météo-France (FR), AINEVA and the regional
services (IT), Euregio / the Austrian services (AT), LWD Bayern (DE-BY),
ARSO (SI).

    python tools/make_eaws_regions.py data_connectors/eaws_regions.json
"""
from __future__ import annotations
import json, sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_slf_regions import _dp_ring, _rings, _enc  # noqa: E402

BASE = "https://regions.avalanches.org"
FILES = ["FR", "IT-21", "IT-23", "IT-25", "IT-32-BZ", "IT-32-TN", "IT-34", "IT-36", "IT-57",
         "AT-02", "AT-03", "AT-04", "AT-05", "AT-06", "AT-07", "AT-08", "DE-BY", "SI"]
# the Alps around Switzerland (lat, lon): what the app's map can reach
BOX = (43.6, 5.0, 48.2, 16.9)
UA = {"User-Agent": "snowmapper-regions/1.0"}


def main(dst, eps=0.007):
    names = requests.get(f"{BASE}/micro-regions_names/de.json", headers=UA, timeout=60).json()
    out = []
    for f in FILES:
        r = requests.get(f"{BASE}/micro-regions/{f}_micro-regions.geojson.json", headers=UA, timeout=120)
        if r.status_code != 200:
            print(f"  {f}: HTTP {r.status_code}")
            continue
        n0 = len(out)
        for feat in r.json().get("features", []):
            rid = (feat.get("properties") or {}).get("id")
            rings = [_dp_ring([(round(x, 5), round(y, 5)) for x, y, *_ in ring], eps)
                     for ring in _rings(feat.get("geometry") or {})]
            rings = [g for g in rings if len(g) >= 4]
            if not rid or not rings:
                continue
            lats = [p[1] for g in rings for p in g]
            lons = [p[0] for g in rings for p in g]
            if max(lats) < BOX[0] or min(lats) > BOX[2] or max(lons) < BOX[1] or min(lons) > BOX[3]:
                continue
            out.append({"id": rid, "name": names.get(rid) or rid, "c": [_enc(g) for g in rings]})
        print(f"  {f}: {len(out) - n0} regions")
    Path(dst).write_text(json.dumps({"source": "EAWS regions, CC0 (regions.avalanches.org)",
                                     "regions": out}, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(out)} regions -> {dst} ({Path(dst).stat().st_size // 1024} kB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data_connectors/eaws_regions.json")
