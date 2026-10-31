#!/usr/bin/env python3
"""Write the app's piste layer (OpenStreetMap, ODbL) to <out>/data/pistes.json.

    python tools/build_pistes.py dist --cache cache/pistes.json --max-age-days 7

A cache younger than --max-age-days is used as is, so Overpass is asked about
once a week, not on every build. An older cache is refreshed, and stays in use
when Overpass is not reachable. Without network and without a cache it writes
nothing and the layer shows only the resort names.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_connectors.osm_pistes import fetch  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--cache", default="cache/pistes.json")
    ap.add_argument("--max-age-days", type=float, default=7.0)
    a = ap.parse_args()
    cache = Path(a.cache)
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < a.max_age_days * 86400
    data = json.loads(cache.read_text(encoding="utf-8")) if fresh else fetch(cache_path=cache)
    if not data:
        print("pistes: none (no network, no cache)")
        return 0
    out = Path(a.out) / "data" / "pistes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"pistes: {len(data['p'])} pistes, {len(data['l'])} lifts -> {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
