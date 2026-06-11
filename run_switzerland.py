"""Interaktiver Einstieg fuer den landesweiten Schweiz-Lauf.

Der Nutzer waehlt ZUERST das Datum und DANN das Akkumulationsfenster (24h/72h).
Beides kann auch per CLI uebergeben werden (dann keine Rueckfrage).

Beispiele
---------
    # Interaktiv (fragt Datum + Fenster ab):
    python run_switzerland.py --offline

    # Nicht-interaktiv:
    python run_switzerland.py --date 2026-01-15 --window 72 --offline
    python run_switzerland.py --date 2026-01-15 --window 24 --res 100 \
        --dem /pfad/swissalti3d_ch.tif
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

from config.settings import NATIONAL_PREVIEW_RES_M
from pipeline.switzerland import run_switzerland


def _valid_date(text: str) -> str:
    """Validiert ein YYYY-MM-DD Datum, wirft ValueError sonst."""
    datetime.strptime(text.strip(), "%Y-%m-%d")
    return text.strip()


def prompt_date() -> str:
    today = date_cls.today().isoformat()
    while True:
        raw = input(f"Datum waehlen (YYYY-MM-DD) [Default {today}]: ").strip()
        if not raw:
            return today
        try:
            return _valid_date(raw)
        except ValueError:
            print("  Ungueltiges Datum. Format: YYYY-MM-DD (z.B. 2026-01-15).")


def prompt_window() -> int:
    while True:
        raw = input("Akkumulationsfenster waehlen - [1] 24h  [2] 72h: ").strip()
        if raw in ("1", "24"):
            return 24
        if raw in ("2", "72"):
            return 72
        print("  Bitte 1 (24h) oder 2 (72h) waehlen.")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Schweiz-weiter Neuschnee-Lauf")
    ap.add_argument("--date", type=str, default=None, help="Zieldatum YYYY-MM-DD")
    ap.add_argument("--window", type=int, choices=[24, 72], default=None,
                    help="Akkumulationsfenster in Stunden (24 oder 72)")
    ap.add_argument("--res", type=float, default=NATIONAL_PREVIEW_RES_M,
                    help="Aufloesung [m] (Default 200; 10 erfordert echtes DEM + Tiling)")
    ap.add_argument("--dem", type=str, default=None, help="Landesweites DEM-GeoTIFF")
    ap.add_argument("--real-dem", action="store_true",
                    help="Echtes Copernicus-DEM erzwingen (online ist es ohnehin Default)")
    ap.add_argument("--offline", action="store_true", help="Synthetisches Wetter")
    ap.add_argument("--weather-step", type=float, default=0.2,
                    help="Wetter-Gitterweite [Grad] (groesser = weniger API-Last)")
    ap.add_argument("--tile-km", type=float, default=20.0, help="Kachelgroesse [km]")
    ap.add_argument("--no-html", action="store_true", help="Overlay ohne Leaflet-HTML")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    # 1. Datum -> 2. Fenster (interaktiv, falls nicht uebergeben).
    date = args.date
    window = args.window
    if date is None or window is None:
        if not sys.stdin.isatty():
            print("Fehler: --date und --window noetig (keine interaktive Konsole).")
            sys.exit(1)
        print("== Schweiz-weiter Neuschnee ==")
        if date is None:
            date = prompt_date()
        if window is None:
            window = prompt_window()
    else:
        date = _valid_date(date)

    # Online standardmaessig ECHTES Terrain (Copernicus). Synthetisches DEM nur
    # im Offline-Modus oder wenn kein eigenes GeoTIFF angegeben ist.
    if args.dem:
        dem_source = "synthetic"  # ignoriert, da dem_path gesetzt
    elif args.offline and not args.real_dem:
        dem_source = "synthetic"
    else:
        dem_source = "copernicus"

    run_switzerland(
        date=date,
        window_hours=window,
        resolution_m=args.res,
        use_synthetic_weather=args.offline,
        dem_path=Path(args.dem) if args.dem else None,
        dem_source=dem_source,
        tile_size_m=args.tile_km * 1000.0,
        weather_step_deg=args.weather_step,
        make_html=not args.no_html,
    )


if __name__ == "__main__":
    main()
