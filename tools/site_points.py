#!/usr/bin/env python3
"""The web app's weather points (the grid the site build interpolates), for
tools/ogd_extract.py -- ids s0000, s0001, … in the order the site build uses.

    python tools/site_points.py site_points.json [--step 0.2]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--step", type=float, default=0.2, help="same as run_interactive.py --weather-step")
    a = ap.parse_args()
    from config.settings import SWITZERLAND_BBOX_LV95 as b
    from pipeline.geo_utils import weather_sample_grid
    lats, lons, _ = weather_sample_grid((b["east_min"], b["north_min"], b["east_max"], b["north_max"]),
                                        "EPSG:2056", a.step)
    pts = [{"id": f"s{i:04d}", "lat": round(float(la), 5), "lon": round(float(lo), 5)}
           for i, (la, lo) in enumerate(zip(lats, lons))]
    Path(a.out).write_text(json.dumps(pts))
    print(f"site points: {len(pts)} -> {a.out}")


if __name__ == "__main__":
    main()
