"""Community reports vs SNOWPACK (variant_a/community.py), offline.

Synthetic weather point, runs and model output; reports shaped like the
app writes them (drawn zones, quick reports, hex EWKB locations)."""
import os
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from variant_a import community as C  # noqa: E402
from variant_a.gridding import METS  # noqa: E402

NOW = datetime(2026, 12, 10, 12)
TS = [NOW - timedelta(hours=h) for h in range(48, -1, -2)]


def ewkb(lat, lon):
    return (struct.pack("<BI", 1, 0x20000001) + struct.pack("<I", 4326) + struct.pack("<dd", lon, lat)).hex()


def setup(model_powder=10.0):
    wp = {"id": "w1", "lat": 46.80, "lon": 9.83, "bands": [1600, 2000, 2400, 2800]}
    runs = []
    for b in wp["bands"]:
        runs.append({"id": f"w1_{b}_F", "wp": "w1", "elev": b, "slope": 0.0, "aspect": 0.0})
        for k, a in enumerate(range(0, 360, 45)):
            runs.append({"id": f"w1_{b}_{k}_38", "wp": "w1", "elev": b, "slope": 38.0, "aspect": float(a)})
    results = {}
    for r in runs:
        m = np.zeros((len(TS), len(METS)))
        m[:, METS.index("total_hs_cm")] = 80
        m[:, METS.index("powder_depth_cm")] = model_powder
        results[r["id"]] = (m,)
    return [wp], runs, results


def rep(i, uid, cm, aspect=45.0, elev=2400, hours_ago=6):
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "user_id": uid, "location": ewkb(46.80, 9.83),
            "condition_data": {"draw": True, "zones": [{"type": "powder", "cm": cm, "slope": 35,
                                                        "elevMin": elev - 100, "elevMax": elev + 100,
                                                        "centroid": [46.801, 9.831], "aspectDeg": aspect,
                                                        "aspectConc": 0.9}]},
            "created_at": (NOW - timedelta(hours=hours_ago)).isoformat() + "Z"}


def main():
    # geometry parsing
    assert C.parse_geo(ewkb(46.5, 9.1)) == [46.5, 9.1]
    assert C.parse_geo("POINT(9.1 46.5)") == [46.5, 9.1]
    assert C.parse_geo({"type": "Point", "coordinates": [9.1, 46.5]}) == [46.5, 9.1]

    # weights grow with confirmations and trust, but stay bounded
    assert C.report_weight(0, 0) == 1.0
    assert 1.0 < C.report_weight(3, 0) < C.report_weight(30, 0) < 1.6 + 1e-9
    assert C.report_weight(100, 1000) < 1.6 * 1.5 + 1e-9

    wps, runs, results = setup(model_powder=10.0)
    # many reports say 30 cm where the model has 10 cm, from several people
    rows_db = [rep(i, f"u{i % 4}", 30) for i in range(40)]
    rows_db.append(rep(99, "u9", 400))                     # an outlier
    rows_db.append({**rep(98, "u8", 30), "condition_data": {"demoFit": True}})   # never used
    stale = {rows_db[0]["id"]: 3}                         # voted outdated
    obs = C.observations(rows_db, {}, stale, {})
    assert all(o.kind == "powder" for o in obs)
    assert len(obs) == 40                                  # demo dropped, stale dropped, outlier kept here
    rows, summ = C.compare(obs, wps, runs, results, TS, METS, now=NOW)
    assert summ["observations"] == 40, summ
    # matched to the NE 38 deg run of the 2400 m band
    assert all(r["run"] == "w1_2400_1_38" for r in rows), {r["run"] for r in rows}
    assert summ["powder_depth"]["bias_cm"] < 0                    # model too low
    assert summ["hit_rate"]["powder"]["hit"] == 1.0               # both say "powder"

    steps = C.suggest_steps(rows)
    assert "w1" in steps and 1.0 < steps["w1"] <= C.R_MAX ** C.STEP_EXP + 1e-3, steps
    hold = C.holdout_check(rows, steps)
    assert hold["n"] > 0 and hold["helps"], hold

    # shadow mode by default: factors untouched
    os.environ.pop("COMMUNITY_APPLY", None)
    f, _, s = C.cycle(wps, runs, results, TS, METS, {"w1": 1.1}, now=NOW,
                      fetched=(rows_db, {}, stale, {}))
    assert f == {"w1": 1.1} and s["mode"] == "shadow" and not s["applied"], s
    # applied only when asked for and confirmed by the holdout
    os.environ["COMMUNITY_APPLY"] = "1"
    f, _, s = C.cycle(wps, runs, results, TS, METS, {"w1": 1.1}, now=NOW,
                      fetched=(rows_db, {}, stale, {}))
    if s["holdout_check"]["n"] >= C.MIN_HOLDOUT:
        assert s["applied"] and f["w1"] > 1.1, s
    os.environ.pop("COMMUNITY_APPLY")

    # one person alone cannot move a weather point
    solo = [rep(i, "u1", 50) for i in range(20)]
    r1, _ = C.compare(C.observations(solo), wps, runs, results, TS, METS, now=NOW)
    assert C.suggest_steps(r1) == {}, "single author must not correct"

    # a dominant author is capped: 30 reports of 60 cm by u1, 4 of 10 cm by others
    mix = [rep(i, "u1", 60) for i in range(30)] + [rep(100 + i, f"v{i}", 10) for i in range(6)]
    r2, _ = C.compare(C.observations(mix), wps, runs, results, TS, METS, now=NOW)
    st = C.suggest_steps(r2)
    full = (C.R_MAX) ** C.STEP_EXP
    assert not st or st["w1"] < full - 1e-3, st

    # out of range: too far away, too old, in the future
    far = [{**rep(200, "u1", 30), "condition_data": {"zones": [{"type": "powder", "cm": 30,
                                                                "centroid": [47.5, 7.5], "elevMin": 2300,
                                                                "elevMax": 2500}]}}]
    old = [rep(201, "u1", 30, hours_ago=200)]
    r3, s3 = C.compare(C.observations(far + old), wps, runs, results, TS, METS, now=NOW)
    assert not r3 and s3["observations"] == 0

    # categories: "no snow" against a model with 80 cm is a miss
    ns = [{**rep(300, "u1", None), "condition_data": {"zones": [{"type": "nosnow", "centroid": [46.8, 9.83],
                                                                "elevMin": 1500, "elevMax": 1700}]}}]
    r4, s4 = C.compare(C.observations(ns), wps, runs, results, TS, METS, now=NOW)
    assert s4["hit_rate"]["nosnow"]["hit"] == 0.0, s4

    # quick report with exposition and altitude
    q = [{"id": "q1", "user_id": "u1", "location": "POINT(9.83 46.80)", "elevation_m": 2050,
          "condition_data": {"quick": True, "powderAmountCm": 0, "exposition": "S"},
          "created_at": (NOW - timedelta(hours=3)).isoformat()}]
    r5, _ = C.compare(C.observations(q), wps, runs, results, TS, METS, now=NOW)
    assert r5 and r5[0]["run"] == "w1_2000_4_38" and r5[0]["hit"] is False, r5

    # rows exactly as PostgREST returns them (taken from the live database)
    real = [{"id": "7471b596-8edf-4bc6-8652-f1ff042ff610", "user_id": "730fb192",
             "location": "0101000020E6100000E686C29CB1CF2140BC2A784FFD554740", "elevation_m": None,
             "condition_data": {"draw": True, "zones": [
                 {"n": 587, "cm": 5, "type": "powder", "elevMax": 2736, "elevMin": 2338,
                  "centroid": [46.67475785958557, 8.90375864008499], "aspectDeg": 315.03, "aspectConc": 0.90},
                 {"n": 294, "cm": None, "type": "suncrust", "elevMax": 2821, "elevMin": 2410,
                  "centroid": [46.66942832139791, 8.904232615488553], "aspectDeg": 218.6, "aspectConc": 0.76}]},
             "captured_at": "2026-08-02T07:38:07.986+00:00", "created_at": "2026-08-02T07:38:08.391324+00:00"},
            {"id": "c993fd87", "user_id": "730fb192", "location": "0101000020E610000001000080CDEB2140167368F3E0954740",
             "elevation_m": 446, "condition_data": {"aspect": "NO", "obsType": "wind_slab", "windSlab24h": "medium"},
             "captured_at": "2026-10-05T19:53:15.857+00:00"},
            {"id": "17ff4914", "user_id": "730fb192", "location": "0101000020E6100000C02154A9D9EB2140C59107228B964740",
             "condition_data": {"stars": 5, "activity": {"track": []}}, "created_at": "2026-10-06T13:51:07.158384+00:00"}]
    ro = C.observations(real)
    assert [o.kind for o in ro] == ["powder", "crust", "drift"], [o.kind for o in ro]
    assert ro[0].t == datetime(2026, 8, 2, 7, 38, 7, 986000) and ro[0].cm == 5 and ro[1].cm is None
    assert abs(ro[0].lat - 46.6748) < 1e-3 and ro[2].aspect == 45 and ro[2].elev == 446

    # bounded factors
    assert C.apply_steps({"w1": 1.99}, {"w1": 1.2})["w1"] == C.F_MAX
    print("COMMUNITY OK")


if __name__ == "__main__":
    main()
