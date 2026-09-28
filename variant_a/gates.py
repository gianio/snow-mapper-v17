"""Publish gates: a live cycle that fails any HARD check is not published,
and the app keeps showing the last cycle that passed.

Hard (block publishing):
  * enough SNOWPACK runs succeeded, and every weather point kept some;
  * the export has every frame of the window;
  * the gridded values are finite and in physical ranges;
  * where IMIS stations report real snow, the model is not wildly off.
Soft (reported, never blocking): the IMIS bias/MAE numbers themselves, and
how many runs started cold instead of from the carried state.
"""
from __future__ import annotations

MIN_RUN_SUCCESS = 0.90
IMIS_MIN_STATIONS = 8
IMIS_MAX_MAE_CM = 80.0        # with a median measured HS above ...
IMIS_MIN_MEDIAN_CM = 30.0     # ... this, i.e. only once there is real snow


def check(n_runs, results, runs, wps, frames_expected, frames_written,
          frame_stats, imis_summary=None, n_cold=0):
    hard, soft = [], []
    ok_share = len(results) / max(1, n_runs)
    if ok_share < MIN_RUN_SUCCESS:
        hard.append(f"only {ok_share:.0%} of SNOWPACK runs succeeded (< {MIN_RUN_SUCCESS:.0%})")
    have = {r["wp"] for r in runs if r["id"] in results}
    lost = [w["id"] for w in wps if w["id"] not in have]
    if lost:
        hard.append(f"{len(lost)} weather points lost every run: {lost[:5]}")
    if frames_written != frames_expected:
        hard.append(f"{frames_written}/{frames_expected} frames written")
    for fs in frame_stats:
        if not fs.get("finite", True):
            hard.append(f"non-finite values in frame {fs.get('tag')}")
            break
        if fs.get("hs_max", 0) > 1500:
            hard.append(f"implausible snow height {fs['hs_max']:.0f} cm in frame {fs.get('tag')}")
            break
    s = imis_summary or {}
    if s.get("stations", 0) >= IMIS_MIN_STATIONS and s.get("median_measured_cm", 0) >= IMIS_MIN_MEDIAN_CM:
        if s.get("mae_cm", 0) > IMIS_MAX_MAE_CM:
            hard.append(f"IMIS snow height MAE {s['mae_cm']} cm > {IMIS_MAX_MAE_CM} cm "
                        f"over {s['stations']} stations")
    if s.get("stations"):
        soft.append(f"IMIS: {s['stations']} stations, bias {s.get('bias_cm')} cm, "
                    f"MAE {s.get('mae_cm')} cm")
    else:
        soft.append("IMIS: no station comparison")
    if n_cold:
        soft.append(f"{n_cold} runs cold-started (no carried state)")
    return {"passed": not hard, "hard": hard, "soft": soft,
            "run_success": round(ok_share, 4)}
