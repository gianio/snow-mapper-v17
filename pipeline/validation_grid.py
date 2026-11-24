"""The model's daily new snow per cell, for the validation against stations.

The SLF and MeteoSwiss observers read their new-snow boards once a day at
06 UTC (the SLF study plots: «HN_1D» at 06 UTC = the 24 h before; MeteoSwiss:
«hns000d0» of day D = 06 UTC D to 06 UTC D+1). The model's comparable value is
the sum of its hourly new snow over the same 24 h, times the 24 h settling
factor the app shows (_ns_settle_24h) -- what a board would show after a day.

Written as .npz: days (end date of each window, ISO), sums (days x H x W,
float32, cm), elev (H x W, m), bounds [laMin, loMin, laMax, loMax], times of
the build, the build's UTC time and the index of "now" (windows ending
after it are forecasts).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np


def daily_windows(times):
    """(end_date, i0, i1) for every complete 06-06 UTC window in `times`
    (hour strings 'YYYY-MM-DDTHH...'): hours i0..i1-1 lie in (end-24h, end]."""
    ts = [datetime.strptime(t[:13], "%Y-%m-%dT%H") for t in times]
    out = []
    for i, t in enumerate(ts):
        if t.hour != 6:
            continue
        # the hourly value at index i covers the hour ending at ts[i] (sum of
        # the hour before): the window (t-24h, t] is indices i-23 .. i
        i0 = i - 23
        if i0 < 0 or ts[i0] != t - timedelta(hours=23):
            continue
        out.append((t.date().isoformat(), i0, i + 1))
    return out


def write_model_daily(data, path: Path) -> str:
    from pipeline.interactive_export import _SNOW_SCALE, _ns_settle_24h, _now_idx
    snow = np.asarray(data["snow"])
    scale = float(_SNOW_SCALE)
    if snow.dtype != np.uint8:          # float cubes are cm already
        scale = 1.0
    settle = float(_ns_settle_24h() or 1.0)
    wins = daily_windows(data["times"])
    sums = np.stack([snow[i0:i1].astype(np.float32).sum(axis=0) * scale * settle for _, i0, i1 in wins]) \
        if wins else np.zeros((0,) + snow.shape[1:], np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, days=np.array([d for d, _, _ in wins]), sums=sums.astype(np.float32),
                        elev=np.asarray(data["main_elev"], np.float32), bounds=np.array(data["bounds"], np.float64),
                        built=np.array(datetime.utcnow().strftime("%Y-%m-%dT%H:%M")),
                        now_time=np.array(data["times"][_now_idx(data["times"])][:13]),
                        settle=np.array(settle))
    return f"{path}: {len(wins)} days {wins[0][0] if wins else '-'} .. {wins[-1][0] if wins else '-'} (settle {settle})"
