#!/usr/bin/env python3
"""SLF avalanche warning regions (EAWS micro-regions CH-xxxx) -> a small file
the app embeds: id, name and a simplified outline per region.

The SLF bulletin (aws.slf.ch, CC BY 4.0) comes as one GeoJSON feature per
bulletin. On a day when every region has its own bulletin each feature is one
region with its own outline; on other days several regions share a merged
outline. This keeps the outlines of single-region features and fills the rest
from the previous file, so running it on any day only ever adds/refreshes.

    python tools/make_slf_regions.py bulletin.geojson data_connectors/slf_regions.json
"""
from __future__ import annotations
import json, sys
from pathlib import Path


def _dp(pts, eps):
    """Douglas-Peucker on [lon, lat] points (degrees)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        n = (dx * dx + dy * dy) ** 0.5 or 1e-12
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / n
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def _dp_ring(ring, eps):
    """A closed ring starts and ends on the same point, which leaves plain DP
    nothing to measure against: split it at the point farthest from the start."""
    if len(ring) < 4:
        return ring
    x0, y0 = ring[0]
    k = max(range(len(ring)), key=lambda i: (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2)
    return _dp(ring[:k + 1], eps)[:-1] + _dp(ring[k:], eps)


def _rings(geom):
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon":
        return [c[0]]
    if t == "MultiPolygon":
        return [p[0] for p in c]
    return []


def _enc(ring):
    """delta-encoded 1e-4 degree steps, lat,lon pairs (as CH_BORDER_ENC)."""
    out, la0, lo0 = [], 0, 0
    for lon, lat in ring:
        la, lo = round(lat * 1e4), round(lon * 1e4)
        out += [la - la0, lo - lo0]
        la0, lo0 = la, lo
    return ",".join(map(str, out))


def main(src, dst, eps=0.0015):
    fc = json.loads(Path(src).read_text())
    old = {}
    if Path(dst).exists():
        old = {r["id"]: r for r in json.loads(Path(dst).read_text())["regions"]}
    names = {}
    for f in fc.get("features", []):
        regs = (f.get("properties") or {}).get("regions") or []
        for r in regs:
            names[r.get("regionID")] = r.get("name")
        if len(regs) != 1:
            continue
        rid = regs[0].get("regionID")
        rings = [_dp_ring(r, eps) for r in _rings(f.get("geometry") or {})]
        rings = [r for r in rings if len(r) >= 4]
        if rid and rings:
            old[rid] = {"id": rid, "name": regs[0].get("name"), "rings": [_enc(r) for r in rings]}
    for rid, nm in names.items():
        if rid in old and nm:
            old[rid]["name"] = nm
    regs = [old[k] for k in sorted(old)]
    Path(dst).write_text(json.dumps({"source": "SLF Lawinenbulletin (CC BY 4.0), EAWS micro-regions",
                                     "regions": regs}, ensure_ascii=False, separators=(",", ":")))
    print(f"{dst}: {len(regs)} regions, {Path(dst).stat().st_size // 1024} kB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
