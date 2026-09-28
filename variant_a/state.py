"""Live mode: the snowpack carried from one run to the next.

A fixed-date run (the demo) spins every virtual slope up from a synthetic
base. A live service must not: the crust from Tuesday is still in the
snowpack under Friday's snowfall, and only a column that has been integrated
through all of it knows that. So each live cycle

  1. restores the state -- one SNOWPACK .sno per virtual slope, valid at the
     START of the previous cycle's output window;
  2. advances it through the (by now observed-ish) weather to the start of
     THIS cycle's window and saves that as the next state;
  3. runs the window itself (past days + forecast) from the new state, which
     is what gets exported.

The state therefore always lags "now" by the window's past half (5 days). It
is only ever advanced with weather from the past, never with a forecast.

Stored as a directory -- meta.json + sno/<run_id>.sno -- which the workflow
tars and keeps as a GitHub Actions artifact between runs.
"""
from __future__ import annotations
import json, shutil, tarfile
from datetime import datetime
from pathlib import Path

VERSION = 1
MAX_GAP_DAYS = 20          # older than this and a run is cold-started instead
ISO = "%Y-%m-%dT%H:%M"


def load(state_dir: Path):
    """{'time': datetime, 'meta': dict, 'sno': Path} or None."""
    state_dir = Path(state_dir)
    tgz = state_dir / "state.tar.gz"
    if tgz.exists() and not (state_dir / "meta.json").exists():
        with tarfile.open(tgz) as tf:
            tf.extractall(state_dir, filter="data")
    mf = state_dir / "meta.json"
    if not mf.exists():
        return None
    try:
        meta = json.loads(mf.read_text())
    except Exception:
        return None
    if meta.get("version") != VERSION:
        print(f"  [state] version {meta.get('version')} != {VERSION}: ignoring")
        return None
    sno = state_dir / "sno"
    if not sno.is_dir():
        return None
    return {"time": datetime.strptime(meta["time"], ISO), "meta": meta, "sno": sno}


def available(st, run_ids, window_start):
    """Run ids that can continue from the state rather than cold-start."""
    if st is None:
        return set()
    if st["time"] > window_start:
        print(f"  [state] valid at {st['time']:%Y-%m-%d %H:%M}, after the window "
              f"start {window_start:%Y-%m-%d %H:%M}: not usable")
        return set()
    if (window_start - st["time"]).days > MAX_GAP_DAYS:
        print(f"  [state] {(window_start - st['time']).days} days old: cold start")
        return set()
    have = {p.stem for p in st["sno"].glob("*.sno")}
    return {r for r in run_ids if r in have}


def save(state_dir: Path, time: datetime, sno_dir: Path, meta_extra=None, pack=True):
    """Write the new state. `sno_dir` holds <run_id>.sno valid at `time`."""
    state_dir = Path(state_dir)
    if state_dir.exists():
        shutil.rmtree(state_dir)
    (state_dir / "sno").mkdir(parents=True)
    n = 0
    for f in Path(sno_dir).glob("*.sno"):
        shutil.copy2(f, state_dir / "sno" / f.name)
        n += 1
    meta = {"version": VERSION, "time": time.strftime(ISO), "runs": n,
            "saved": datetime.utcnow().strftime(ISO)}
    meta.update(meta_extra or {})
    (state_dir / "meta.json").write_text(json.dumps(meta, indent=1))
    if pack:
        with tarfile.open(state_dir / "state.tar.gz", "w:gz") as tf:
            tf.add(state_dir / "meta.json", arcname="meta.json")
            tf.add(state_dir / "sno", arcname="sno")
    return meta
