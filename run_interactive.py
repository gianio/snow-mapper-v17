"""Interaktive Schneekarte (wepowder-Stil) mit Intervall-Band, Mess-Crosscheck
und Temperatur-/Wind-Layern.

Der Nutzer waehlt optional ein Zentrumsdatum; das Zeitfenster (Start/Ende, max
+/-5 Tage) sowie Layer (Schnee/Temp/Wind) und Statistik (Mittel/Max/Min) werden
DIREKT in der HTML eingestellt. Standard ohne --date: zentriert auf heute.

Beispiele
---------
    python run_interactive.py                    # heute +/-5 Tage, echte Daten
    python run_interactive.py --date 2026-02-19  # zentriert auf dieses Datum
    python run_interactive.py --offline          # synthetisch (Test, ohne Stationen)
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from config.settings import OUTPUT_DIR
from pipeline.interactive_export import build_interactive_data, export_interactive_html


def _valid_date(text: str) -> str:
    datetime.strptime(text.strip(), "%Y-%m-%d")
    return text.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Interaktive Neuschneekarte (Band-Slider)")
    ap.add_argument("--date", type=str, default=None,
                    help="Zentrumsdatum YYYY-MM-DD (Default: heute)")
    ap.add_argument("--days", type=int, default=5,
                    help="Tage je Seite (max. Bereich des Bands), Default 5")
    ap.add_argument("--res", type=float, default=2000.0, help="Aufloesung [m]")
    ap.add_argument("--offline", action="store_true", help="Synthetisch (ohne Stationen)")
    ap.add_argument("--weather-step", type=float, default=0.2, help="Wetter-Gitter [Grad]")
    ap.add_argument("--stations", type=int, default=60, help="Anzahl SLF-Stationen")
    args = ap.parse_args()

    date = _valid_date(args.date) if args.date else None
    data = build_interactive_data(
        center_date=date,
        days_each_side=max(1, min(5, args.days)),
        resolution_m=args.res,
        use_synthetic=args.offline,
        weather_step_deg=args.weather_step,
        n_stations=args.stations,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = date or "today"
    out = export_interactive_html(data, OUTPUT_DIR / f"interactive_{tag}.html")
    print(f"[OUT] Interaktive Karte: {out}")
    print("      Band ziehen (max +/-5 Tage), Layer Schnee/Temp/Wind, Stat Mittel/Max/Min.")


if __name__ == "__main__":
    main()
