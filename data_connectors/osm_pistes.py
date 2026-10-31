"""Ski pistes and lifts in Switzerland from OpenStreetMap, for the app's own
piste layer (instead of a third-party tile server).

One Overpass query, simplified to ~8 m and packed small: the app draws the
lines itself -- downhill pistes by difficulty, nordic trails, lifts by type.
Data (c) OpenStreetMap contributors, ODbL: the app credits it on the layer
and in «Datenquellen & Lizenzen».

    data = fetch(cache_path=Path("cache/pistes.json"))
    -> {"v": 1, "attr": ..., "p": [[kind, diff, coords], ...], "l": [[kind, name, coords], ...]}

coords: flat integer list [lon0, lat0, dlon1, dlat1, ...] in 1e-5 degrees,
delta encoded after the first point (small numbers compress well).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

OVERPASS = ("https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
ATTRIBUTION = "© OpenStreetMap-Mitwirkende (ODbL)"

QUERY = """[out:json][timeout:280];
area["ISO3166-1"="CH"][admin_level=2]->.ch;
(
  way["piste:type"~"^(downhill|nordic)$"](area.ch);
  way["aerialway"~"^(cable_car|gondola|mixed_lift|chair_lift|drag_lift|t-bar|j-bar|platter|rope_tow|magic_carpet|zip_line)$"](area.ch);
);
out tags geom;"""

# piste:difficulty -> 0 novice (green), 1 easy (blue), 2 intermediate (red),
# 3 advanced/expert (black), 4 freeride (yellow skiroute); None unknown
DIFF = {"novice": 0, "easy": 1, "intermediate": 2, "advanced": 3, "expert": 3,
        "extreme": 3, "freeride": 4}
PISTE_KIND = {"downhill": 0, "nordic": 1}
# lifts: 0 cable car / gondola, 1 chair lift, 2 drag lift / rope tow, 3 other
LIFT_KIND = {"cable_car": 0, "gondola": 0, "mixed_lift": 0, "chair_lift": 1,
             "drag_lift": 2, "t-bar": 2, "j-bar": 2, "platter": 2, "rope_tow": 2,
             "magic_carpet": 2, "zip_line": 3}


def _simplify(pts: List[tuple], tol_m: float) -> List[tuple]:
    """Douglas-Peucker on (lon, lat) in local metres."""
    if len(pts) < 3:
        return pts
    lat0 = math.radians(sum(p[1] for p in pts) / len(pts))
    kx, ky = 111320.0 * math.cos(lat0), 110540.0
    xy = [(p[0] * kx, p[1] * ky) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]; bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if L2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, bi = d, i
        if best > tol_m and bi > 0:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def _encode(pts: List[tuple]) -> List[int]:
    out, px, py = [], 0, 0
    for i, (lon, lat) in enumerate(pts):
        x, y = int(round(lon * 1e5)), int(round(lat * 1e5))
        if i == 0:
            out += [x, y]
        else:
            out += [x - px, y - py]
        px, py = x, y
    return out


def pack(payload: Dict[str, Any], tol_m: float = 8.0) -> Dict[str, Any]:
    """Overpass JSON -> the compact form the app reads."""
    p, l = [], []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        pts = [(g["lon"], g["lat"]) for g in geom if "lon" in g and "lat" in g]
        if len(pts) < 2:
            continue
        tags = el.get("tags") or {}
        pts = _simplify(pts, tol_m)
        if "aerialway" in tags:
            k = LIFT_KIND.get(tags["aerialway"])
            if k is not None:
                l.append([k, (tags.get("name") or "")[:60], _encode(pts)])
        else:
            k = PISTE_KIND.get(tags.get("piste:type", ""))
            if k is not None:
                d = DIFF.get(tags.get("piste:difficulty", ""))
                p.append([k, d, _encode(pts)])
    return {"v": 1, "attr": ATTRIBUTION, "p": p, "l": l}


def fetch(cache_path: Optional[Path] = None, timeout: float = 300.0) -> Optional[Dict[str, Any]]:
    """Fetch and pack; the cache when Overpass is not reachable. Never raises."""
    import requests
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": QUERY}, timeout=timeout,
                              headers={"User-Agent": "Snowmapper piste layer (weekly)"})
            r.raise_for_status()
            data = pack(r.json())
            if not data["p"] and not data["l"]:
                print(f"[OSM] {url}: no pistes in the answer")
                continue
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            print(f"[OSM] {len(data['p'])} pistes, {len(data['l'])} lifts from {url}")
            return data
        except Exception as e:                      # noqa: BLE001
            print(f"[OSM] {url} failed: {e!r}")
    if cache_path and cache_path.exists():
        print("[OSM] pistes: cached copy")
        return json.loads(cache_path.read_text(encoding="utf-8"))
    return None
