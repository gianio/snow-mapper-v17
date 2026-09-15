#!/usr/bin/env python3
"""Check the live upstream payloads against the parsers written for them.

Why this exists: the agent build environment's egress policy blocks
``aws.slf.ch`` and ``data.geo.admin.ch`` (403 at the proxy, and the proxy
README is explicit that a policy denial must not be routed around). So
``slf_bulletin.py`` and ``swisstopo_routes.py`` were written against
fixtures and have never seen a real response. A CI runner can reach both.

This reports; it never fails the build. The point is to surface a shape
mismatch as a readable diff of expectations rather than as "0 regions" in
production three months later.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _short(obj, n=900):
    try:
        return json.dumps(obj, ensure_ascii=False)[:n]
    except (TypeError, ValueError):
        return str(obj)[:n]


def probe_bulletin(lang="de") -> bool:
    import requests
    from data_connectors import slf_bulletin as B

    print("=" * 72)
    print("SLF avalanche bulletin —", B._BASE, "(CC BY 4.0)")
    ok = False
    for suffix in ("geojson", "json", ""):
        url = f"{B._BASE}/caaml/{lang}/{suffix}".rstrip("/")
        try:
            r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
        except Exception as e:                       # noqa: BLE001
            print(f"  {url}\n    ERROR {e!r}")
            continue
        print(f"  {url}\n    HTTP {r.status_code} "
              f"{r.headers.get('content-type','?')} {len(r.content)} bytes")
        if r.status_code != 200:
            continue
        try:
            payload = r.json()
        except ValueError as e:
            print(f"    not JSON ({e}); starts: {r.text[:200]!r}")
            continue
        if isinstance(payload, dict):
            print("    top-level keys:", sorted(payload)[:20])
        elif isinstance(payload, list):
            print(f"    top-level list of {len(payload)}")
            if payload and isinstance(payload[0], dict):
                print("    item[0] keys:", sorted(payload[0])[:20])
        regions = B.parse_bulletin(payload)
        print(f"    parse_bulletin -> {len(regions)} regions")
        if regions:
            ok = True
            geo = sum(1 for x in regions if x.get("geometry"))
            print(f"    with geometry: {geo}/{len(regions)}"
                  + ("  (map layer works)" if geo else "  (NO geometry -> layer stays hidden)"))
            print("    danger levels:", sorted({x["danger"] for x in regions}))
            print("    aspects seen:", sorted({a for x in regions for a in x["aspects"]}))
            print("    sample:", _short({k: v for k, v in regions[0].items()
                                         if k != "geometry"}))
        else:
            print("    !! parser returned nothing — live shape differs from the fixtures")
            print("    raw:", _short(payload, 1200))
    return ok


def probe_routes() -> bool:
    import requests
    from data_connectors import swisstopo_routes as R

    print("=" * 72)
    print("swisstopo ski routes — OGD,", R.ATTRIBUTION)
    try:
        r = requests.get(R.STAC_COLLECTION, timeout=60)
        print(f"  STAC {R.STAC_COLLECTION}\n    HTTP {r.status_code} "
              f"{r.headers.get('content-type','?')}")
        if r.status_code == 200:
            items = (r.json() or {}).get("features") or []
            print(f"    {len(items)} items")
            for it in items[:3]:
                assets = it.get("assets") or {}
                fmts = sorted({(v or {}).get("href", "").rsplit(".", 1)[-1].lower()
                               for v in assets.values()})
                print(f"    item {it.get('id')}: {len(assets)} assets, formats {fmts}")
    except Exception as e:                           # noqa: BLE001
        print(f"    ERROR {e!r}")

    routes = R.fetch_routes()
    print(f"  fetch_routes -> {len(routes)} routes")
    if routes:
        pts = sum(len(x["coords"]) for x in routes)
        named = sum(1 for x in routes if x.get("name"))
        print(f"    {pts} vertices, {named} named")
        s = routes[0]
        print(f"    sample: {s['id']} | {s.get('name')} | "
              f"{len(s['coords'])} pts | first {s['coords'][0]}")
        return True
    print("    !! nothing parsed — the STAC assets are probably not GeoJSON.")
    print("    The tour layer stays hidden until this resolves; see the")
    print("    asset formats listed above for what the reader needs to handle.")
    return False


if __name__ == "__main__":
    b = r = False
    try:
        b = probe_bulletin()
    except Exception as e:                           # noqa: BLE001
        print(f"bulletin probe crashed: {e!r}")
    try:
        r = probe_routes()
    except Exception as e:                           # noqa: BLE001
        print(f"routes probe crashed: {e!r}")
    print("=" * 72)
    print(f"bulletin parsed: {'YES' if b else 'NO'} | routes parsed: {'YES' if r else 'NO'}")
    # Exit 0 regardless: this reports, it does not gate.
    sys.exit(0)
