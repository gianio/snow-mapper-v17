"""Output-Export: GeoTIFF (Hauptoutput) und CSV-Rasterzusammenfassung."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

try:
    import rasterio
    _HAS_RASTERIO = True
except Exception:  # pragma: no cover
    _HAS_RASTERIO = False


def write_geotiff(
    array: np.ndarray,
    transform,
    crs: str,
    path: str | Path,
    nodata: float = -9999.0,
) -> Path:
    """Schreibt ein 2D-Array als einbandiges Float32-GeoTIFF."""
    if not _HAS_RASTERIO:
        raise RuntimeError("rasterio nicht installiert - GeoTIFF-Export nicht moeglich.")
    path = Path(path)
    data = np.where(np.isfinite(array), array, nodata).astype("float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
        compress="deflate",
    ) as dst:
        dst.write(data, 1)
    return path


def write_csv_summary(
    layers: dict[str, np.ndarray],
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    path: str | Path,
    sample_step: int = 10,
) -> Path:
    """Schreibt eine CSV mit Koordinaten + Modellschichten (ausgeduennt)."""
    path = Path(path)
    sl = (slice(None, None, sample_step), slice(None, None, sample_step))
    data = {"east": grid_x[sl].ravel(), "north": grid_y[sl].ravel()}
    for name, arr in layers.items():
        data[name] = arr[sl].ravel()
    pd.DataFrame(data).to_csv(path, index=False)
    return path
