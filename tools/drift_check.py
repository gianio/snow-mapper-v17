#!/usr/bin/env python3
"""Drift check for the GitHub job: verdict into the job summary + annotation.

    python tools/drift_check.py outputs/variant_a/drift.json [--strict]

The verdict itself is made in the live cycle (variant_a/drift.py); this
only reports it. WATCH becomes a warning annotation, DRIFT an error
annotation. The step still passes -- a drifting snowpack is still the best
one there is, and a failed workflow would also stop the deploy -- unless
--strict is given.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from variant_a import drift  # noqa: E402

PAGE = "https://gianio.github.io/snow-mapper-v17/data/variant_a_live/drift.html"


def main(argv):
    if not argv or not Path(argv[0]).exists():
        print("no drift.json -- the cycle did not get as far as the station comparison")
        return 0
    res = json.loads(Path(argv[0]).read_text())
    md = drift.markdown(res) + f"\nPage: {PAGE}\n"
    print(md)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write(md)
    reasons = "; ".join(dict.fromkeys(res["independent"].get("reasons", [])
                                      + res["all"].get("reasons", []))) or res["status"]
    if res["status"] == "DRIFT":
        print(f"::error title=Snowpack drift::{reasons} -- {PAGE}")
    elif res["status"] == "WATCH":
        print(f"::warning title=Snowpack drift (watch)::{reasons} -- {PAGE}")
    if not res.get("full_season"):
        print("::warning title=Snowpack season::not every slope runs from the season start "
              "-- run the 'Variant A season replay' workflow")
    return 1 if ("--strict" in argv and res["status"] == "DRIFT") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
