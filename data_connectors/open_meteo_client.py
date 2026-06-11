"""Open-Meteo Wetter-Client.

REINER Datenzugriff: holt stuendliche Vorhersagen fuer eine Liste von
Koordinaten und parst die JSON-Antwort in typisierte Strukturen. Es findet
hier KEINE meteorologische oder modellseitige Verrechnung statt.

API: https://open-meteo.com/  (kein API-Key fuer nicht-kommerzielle Nutzung)
Mehrere Standorte werden ueber kommaseparierte Koordinatenlisten in EINEM
Request abgefragt. ``precipitation`` ist Stundensumme [mm], ``snowfall`` [cm],
``temperature_2m`` [degC], ``wind_speed_10m`` [m/s], ``wind_direction_10m``
ist die Richtung, AUS der der Wind weht [Grad, met. Konvention].
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import time

import requests

HOURLY_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "precipitation",
    "snowfall",
    "wind_speed_10m",
    "wind_direction_10m",
    "sunshine_duration",
)


@dataclass
class PointForecast:
    """Stuendliche Rohdaten eines einzelnen Gitterpunkts."""

    latitude: float
    longitude: float
    elevation: float            # Modellhoehe des Wetterpunkts [m]
    time: List[str]
    temperature_2m: List[float]      # [degC]
    precipitation: List[float]       # [mm] Stundensumme
    snowfall: List[float]            # [cm] Stundensumme
    wind_speed_10m: List[float]      # [m/s]
    wind_direction_10m: List[float]  # [Grad, woher]
    sunshine_duration: List[float]   # [s] Sonnenscheindauer pro Stunde


class OpenMeteoClient:
    """Duenner HTTP-Wrapper um den Open-Meteo Forecast-Endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str = "best_match",
        timeout: float = 90.0,
        wind_speed_unit: str = "ms",
        max_retries: int = 5,
        backoff_s: float = 2.0,
        pause_between_chunks_s: float = 1.0,
        send_model: bool = True,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.wind_speed_unit = wind_speed_unit
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.pause_between_chunks_s = pause_between_chunks_s
        self.send_model = send_model  # Archiv-Endpoint vertraegt 'models' nicht immer

    def fetch(
        self,
        latitudes: Sequence[float],
        longitudes: Sequence[float],
        forecast_days: int = 2,
        past_days: int = 0,
        start_date: str | None = None,
        end_date: str | None = None,
        chunk_size: int = 15,
    ) -> List[PointForecast]:
        """Holt stuendliche Vorhersagen fuer alle (lat, lon)-Paare.

        Bei gesetztem ``start_date``/``end_date`` (YYYY-MM-DD) wird genau dieser
        Zeitraum geliefert (sonst forecast_days/past_days). Viele Punkte werden
        in Bloecken von ``chunk_size`` abgefragt, da die URL-Laenge begrenzt ist.

        Returns
        -------
        list[PointForecast]
            Ein Eintrag pro abgefragtem Standort, Reihenfolge wie Input.
        """
        if len(latitudes) != len(longitudes):
            raise ValueError("latitudes und longitudes muessen gleich lang sein.")

        results: List[PointForecast] = []
        n_chunks = (len(latitudes) + chunk_size - 1) // chunk_size
        for ci, i in enumerate(range(0, len(latitudes), chunk_size)):
            lat_chunk = latitudes[i : i + chunk_size]
            lon_chunk = longitudes[i : i + chunk_size]
            results.extend(
                self._fetch_chunk(
                    lat_chunk, lon_chunk, forecast_days, past_days, start_date, end_date
                )
            )
            # Hoeflichkeitspause gegen Rate-Limits (ausser nach dem letzten Batch).
            if self.pause_between_chunks_s and ci < n_chunks - 1:
                time.sleep(self.pause_between_chunks_s)
        return results

    def _fetch_chunk(
        self,
        latitudes: Sequence[float],
        longitudes: Sequence[float],
        forecast_days: int,
        past_days: int,
        start_date: str | None,
        end_date: str | None,
    ) -> List[PointForecast]:
        params = {
            "latitude": ",".join(f"{v:.5f}" for v in latitudes),
            "longitude": ",".join(f"{v:.5f}" for v in longitudes),
            "hourly": ",".join(HOURLY_VARIABLES),
            "wind_speed_unit": self.wind_speed_unit,
            "timezone": "UTC",
        }
        if self.send_model:
            params["models"] = self.model
        if start_date and end_date:
            params["start_date"] = start_date
            params["end_date"] = end_date
        else:
            params["forecast_days"] = forecast_days
            params["past_days"] = past_days

        payload = self._request_with_retry(params)
        if isinstance(payload, dict):
            payload = [payload]
        return [self._parse_point(p) for p in payload]

    def _request_with_retry(self, params: dict):
        """GET mit exponentiellem Backoff bei 429/5xx (respektiert Retry-After)."""
        headers = {"User-Agent": "swiss-snow-model/1.0 (research)"}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            resp = requests.get(
                self.base_url, params=params, headers=headers, timeout=self.timeout
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() \
                    else self.backoff_s * (2 ** attempt)
                print(f"[WX] {resp.status_code} - warte {wait:.0f}s "
                      f"(Versuch {attempt + 1}/{self.max_retries}) ...")
                time.sleep(wait)
                last_exc = requests.HTTPError(f"{resp.status_code}", response=resp)
                continue
            resp.raise_for_status()
        # Alle Versuche erschoepft.
        raise RuntimeError(
            "Open-Meteo Rate-Limit/Fehler nach mehreren Versuchen. Tipp: groeberes "
            "--weather-step waehlen oder spaeter erneut versuchen."
        ) from last_exc

    @staticmethod
    def _parse_point(p: dict) -> PointForecast:
        hourly = p["hourly"]
        return PointForecast(
            latitude=float(p["latitude"]),
            longitude=float(p["longitude"]),
            elevation=float(p.get("elevation", 0.0)),
            time=list(hourly["time"]),
            temperature_2m=_clean(hourly["temperature_2m"]),
            precipitation=_clean(hourly["precipitation"]),
            snowfall=_clean(hourly["snowfall"]),
            wind_speed_10m=_clean(hourly["wind_speed_10m"]),
            wind_direction_10m=_clean(hourly["wind_direction_10m"]),
            sunshine_duration=_clean(hourly.get("sunshine_duration", [])),
        )


def _clean(values: Sequence[float | None]) -> List[float]:
    """Ersetzt fehlende Werte (None) durch 0.0 — defensives Parsen."""
    return [0.0 if v is None else float(v) for v in values]
