"""DEM-Loader.

REINER Datenzugriff auf Hoehenmodelle. Zwei Quellen:

1. ``load_dem_geotiff`` - liest ein lokales GeoTIFF (z.B. swissALTI3D 10 m,
   LV95/EPSG:2056, oder Copernicus DEM 30 m, EPSG:4326) und schneidet es auf
   die AOI zu.
2. ``synthetic_dem`` - erzeugt ein plausibles alpines Hoehenmodell rein
   prozedural, damit die Pipeline ohne Gigabyte-Downloads end-to-end laeuft.

Bezugsquellen der echten Daten siehe docs/README.md.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:  # rasterio ist nur fuer echte GeoTIFFs noetig, nicht fuer die Demo.
    import rasterio
    from rasterio.windows import from_bounds
    _HAS_RASTERIO = True
except Exception:  # pragma: no cover
    _HAS_RASTERIO = False


@dataclass
class DEM:
    """Hoehenraster mit Georeferenz.

    elevation : 2D-Array [m], Zeile 0 = Norden (north-up).
    transform : affine Transformation (rasterio.Affine) oder None (synthetisch).
    crs       : EPSG-Code als String.
    res       : Rasterweite in Metern.
    bounds    : (east_min, north_min, east_max, north_max).
    """

    elevation: np.ndarray
    transform: object
    crs: str
    res: float
    bounds: tuple[float, float, float, float]


def load_dem_geotiff(
    path: str,
    bounds: tuple[float, float, float, float],
    crs: str,
    res: float,
) -> DEM:
    """Liest ein GeoTIFF und schneidet es auf ``bounds`` (im CRS der Datei) zu.

    Annahme: Datei-CRS == AOI-CRS. Reprojektion bewusst ausgelagert, um den
    Connector frei von Verarbeitungslogik zu halten.
    """
    if not _HAS_RASTERIO:
        raise RuntimeError("rasterio nicht installiert - GeoTIFF nicht lesbar.")

    east_min, north_min, east_max, north_max = bounds
    with rasterio.open(path) as src:
        window = from_bounds(east_min, north_min, east_max, north_max, src.transform)
        elevation = src.read(1, window=window).astype("float64")
        win_transform = src.window_transform(window)
        file_crs = str(src.crs)

    elevation = np.where(np.isfinite(elevation), elevation, np.nan)
    return DEM(
        elevation=elevation,
        transform=win_transform,
        crs=file_crs or crs,
        res=res,
        bounds=bounds,
    )


def synthetic_dem(
    bounds: tuple[float, float, float, float],
    res: float,
    crs: str = "EPSG:2056",
    seed: int = 42,
) -> DEM:
    """Erzeugt ein prozedurales alpines DEM (Demo / Tests, ohne Download).

    WICHTIG: Das Hoehenfeld ist eine DETERMINISTISCHE Funktion der ABSOLUTEN
    LV95-Koordinaten (kein zufaelliges Mikrorelief). Dadurch passen benachbarte
    Kacheln nahtlos zusammen und ein gekachelter Schweiz-Lauf ergibt ein
    konsistentes Mosaik. Die Hoehenverteilung (~500-3500 m) imitiert grob den
    Alpenbogen (Mittelland tiefer im Norden, Alpenkamm hoeher im Sueden).
    """
    east_min, north_min, east_max, north_max = bounds
    width = int(round((east_max - east_min) / res))
    height = int(round((north_max - north_min) / res))

    # Absolute Koordinaten der Zellmittelpunkte (north-up).
    xs = east_min + (np.arange(width) + 0.5) * res
    ys = north_max - (np.arange(height) + 0.5) * res
    X, Y = np.meshgrid(xs, ys)

    # Multi-Frequenz-Relief mit Wellenlaengen im km-Bereich (in Metern).
    def wave(coord, wavelength, phase=0.0):
        return np.sin(2 * np.pi * coord / wavelength + phase)

    relief = (
        520 * wave(X, 61700) * wave(Y, 50300)
        + 300 * wave(X, 23300, 0.6)
        + 240 * wave(Y, 18700, 1.1)
        + 180 * wave(X, 41300, 2.0) * wave(Y, 33100, 0.4)
        + 90 * wave(X + 0.4 * Y, 9700, 0.9)
        + 55 * wave(0.6 * X - Y, 6100, 0.3)
    )
    # Alpenbogen-Trend: hoeher Richtung Sueden (kleineres N), zentriert.
    n_ref, n_span = 1296000.0, 221000.0
    arc = 700 * np.cos(np.clip((Y - (n_ref - n_span * 0.55)) / (n_span * 0.5), -1, 1) * (np.pi / 2))
    base = 900.0

    elevation = base + arc + relief
    elevation = np.clip(elevation, 350.0, 3600.0)

    transform = _affine(east_min, north_max, res)
    return DEM(
        elevation=elevation,
        transform=transform,
        crs=crs,
        res=res,
        bounds=bounds,
    )


def _affine(east_origin: float, north_origin: float, res: float):
    """Baut eine affine Transformation; nutzt rasterio.Affine falls vorhanden."""
    if _HAS_RASTERIO:
        from rasterio.transform import from_origin

        return from_origin(east_origin, north_origin, res, res)
    # Fallback ohne rasterio: 6-Tupel (a, b, c, d, e, f) im Affine-Stil.
    return (res, 0.0, east_origin, 0.0, -res, north_origin)
