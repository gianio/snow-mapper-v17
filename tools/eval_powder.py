"""Accuracy-Harness fuer die Powder-Engine (nur Backend, keine UI).

Liest das Golden-Dataset (Punkt-Beobachtungen mit Ground Truth), holt fuer
jeden Punkt stuendliche Archivdaten von Open-Meteo, laesst die Python-
Referenz-Engine (``model/powder_engine.py``) laufen und vergleicht.

Aufruf:
    python tools/eval_powder.py                       # Standard-CSV, 24h-Fenster
    python tools/eval_powder.py --window 48           # 48h-Fenster
    python tools/eval_powder.py --csv pfad.csv
    python tools/eval_powder.py --offline             # nur Cache, kein Netz

Hinweise / bewusste Abweichungen zur JS-Engine:
- Solar: die App nutzt ein Clear-Sky-Terrainmodell (Wh/m^2/Tag). Der Harness
  summiert Open-Meteo ``shortwave_radiation`` ueber das Fenster (ebenfalls
  Wh/m^2) — gleiche Groessenordnung, aber gemessene statt theoretische Werte.
- Exposition: aus der CSV-Spalte ``aspect`` (N/E/S/W). ``leeward``/``shaded``/
  leer => Bewertung "haelt irgendeine Exposition Powder?".
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model.powder_engine import compute_powder, _QCEN  # noqa: E402

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = ("temperature_2m,precipitation,snowfall,wind_speed_10m,"
          "wind_direction_10m,sunshine_duration,shortwave_radiation")
LOOKBACK_DAYS = 10
CACHE_DIR = Path(__file__).resolve().parent / "cache"


def fetch_archive(lat: float, lon: float, start: str, end: str,
                  elevation: float | None = None, offline: bool = False) -> dict:
    key = hashlib.md5(
        f"{lat:.4f},{lon:.4f},{start},{end},{elevation}".encode()).hexdigest()
    cpath = CACHE_DIR / f"om_{key}.json"
    if cpath.exists():
        return json.loads(cpath.read_text())
    if offline:
        raise RuntimeError(f"Offline und kein Cache fuer {lat},{lon} {start}..{end}")
    import requests
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "hourly": HOURLY, "timezone": "Europe/Zurich",
    }
    # WICHTIG: Beobachtungshoehe mitgeben — sonst liefert Open-Meteo die
    # (oft viel tiefere) Rasterzellen-Hoehe -> zu warm, Regen statt Schnee.
    if elevation:
        params["elevation"] = elevation
    r = requests.get(ARCHIVE_URL, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath.write_text(json.dumps(data))
    return data


# Flachfeld-Strahlung -> grobe Expositions-Skalierung (Winter/Fruehjahr, Alpen).
# Die App macht das exakt per Terrainmodell; hier die Punkt-Naeherung.
ASPECT_SOLAR = {"N": 0.55, "E": 0.85, "S": 1.15, "W": 0.85}


def evaluate_row(row: dict, window_h: int, offline: bool) -> dict:
    lat, lon = float(row["lat"]), float(row["lon"])
    alt = float(row["alt_m"]) if (row.get("alt_m") or "").strip() else None
    obs_date = row["date"].strip()
    obs_time = (row.get("time_local") or "12:00").strip()
    obs_dt = datetime.strptime(f"{obs_date} {obs_time}", "%Y-%m-%d %H:%M")
    start = (obs_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    data = fetch_archive(lat, lon, start, obs_date, elevation=alt, offline=offline)
    h = data["hourly"]
    times = h["time"]

    def series(name, scale=1.0, default=0.0):
        vals = h.get(name) or []
        return [(v * scale if v is not None else default) for v in vals]

    temp = series("temperature_2m")
    prec = series("precipitation")
    snow = series("snowfall")                    # cm
    wind = series("wind_speed_10m")              # km/h
    wdir = series("wind_direction_10m")
    sunh = series("sunshine_duration", 1 / 3600) # s -> h
    swr = series("shortwave_radiation")          # W/m^2 je h

    # Beobachtungsstunde im Zeitraster finden
    key = obs_dt.strftime("%Y-%m-%dT%H:00")
    try:
        tb = times.index(key) + 1
    except ValueError:
        tb = len(times)
    ta = max(0, tb - window_h)
    solar_flat = sum(swr[ta:tb])

    # Exposition: nur exakte Quadranten-Tokens akzeptieren
    # ("leeward", "shaded/top" etc. -> unbekannt)
    aspect_raw = (row.get("aspect") or "").strip().upper()
    known = aspect_raw in _QCEN

    def run(quad: str | None):
        f = ASPECT_SOLAR.get(quad, 1.0)
        return compute_powder(
            temp_c=temp, precip_mm=prec, snowfall_cm=snow, wind_kmh=wind,
            wind_dir_deg=wdir, sun_h=sunh, solar_wh=solar_flat * f,
            ta=ta, tb=tb,
            aspect_deg=_QCEN[quad] if quad else None,
        )

    if known:
        res = run(aspect_raw)
    else:
        # unbekannte Exposition: haelt IRGENDEINE Exposition Powder?
        per = {q: run(q) for q in ("N", "E", "S", "W")}
        holding = [q for q, r in per.items() if r.powdered]
        best = per[holding[0]] if holding else per["N"]
        res = best
        res.valid_aspects = sorted(holding) if holding else best.valid_aspects
        res.powdered = bool(holding)

    truth = (row.get("powder") or "").strip().lower() == "yes"
    return {
        "id": row["id"], "name": (row.get("image_id") or "")[:38],
        "truth": truth, "pred": res.powdered, "quality": res.quality,
        "aspects": ",".join(res.valid_aspects) or "-",
        "flags": ",".join(res.reason_flags) or "-",
        "aspect_in": aspect_raw or "-",
        "hit": res.powdered == truth,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Powder-Engine Accuracy-Harness")
    ap.add_argument("--csv", default=str(Path(__file__).parent / "golden" /
                                         "snow_conditions_golden_dataset.csv"))
    ap.add_argument("--window", type=int, default=24, help="Fenster in h (Default 24)")
    ap.add_argument("--offline", action="store_true", help="nur Cache verwenden")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    results, errors = [], []
    for row in rows:
        try:
            results.append(evaluate_row(row, args.window, args.offline))
        except Exception as e:  # Zeile mit Fehler nicht das Ganze kippen lassen
            errors.append((row.get("id"), str(e)))

    wid = max((len(r["name"]) for r in results), default=10)
    print(f"{'id':>3} {'beobachtung':<{wid}} {'truth':<5} {'pred':<5} "
          f"{'ok':<3} {'qual':<8} {'aspects':<9} flags")
    print("-" * (wid + 70))
    for r in results:
        print(f"{r['id']:>3} {r['name']:<{wid}} "
              f"{'yes' if r['truth'] else 'no':<5} {'yes' if r['pred'] else 'no':<5} "
              f"{'✓' if r['hit'] else '✗':<3} {r['quality']:<8} {r['aspects']:<9} {r['flags']}")

    n = len(results)
    hits = sum(1 for r in results if r["hit"])
    print("-" * (wid + 70))
    print(f"Accuracy: {hits}/{n} = {100.0 * hits / max(1, n):.0f}%  "
          f"(Fenster {args.window}h, Lookback {LOOKBACK_DAYS}d)")
    misses = [r for r in results if not r["hit"]]
    if misses:
        print("Misses:", ", ".join(f"#{r['id']}({r['flags']})" for r in misses))
    for eid, msg in errors:
        print(f"FEHLER Zeile {eid}: {msg}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
