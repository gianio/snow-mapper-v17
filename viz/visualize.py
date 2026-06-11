"""Visualisierung der Modellresultate (matplotlib; optional folium)."""
from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402


def plot_layers(
    layers: Dict[str, np.ndarray],
    bounds: tuple[float, float, float, float],
    out_path: str | Path,
) -> Path:
    """Erzeugt eine Mehrfach-Panel-Karte (Neuschnee + Diagnoseschichten)."""
    out_path = Path(out_path)
    east_min, north_min, east_max, north_max = bounds
    extent = (east_min, east_max, north_min, north_max)

    panels = [
        ("new_snow_cm", "Neuschnee [cm]", "Blues"),
        ("elevation", "Hoehe [m]", "terrain"),
        ("snow_fraction", "Schneeanteil [-]", "viridis"),
        ("orographic_factor", "Orograf. Faktor [-]", "RdBu_r"),
        ("wind_factor", "Wind-Faktor [-]", "RdBu_r"),
        ("slope_deg", "Hangneigung [Grad]", "magma"),
    ]
    panels = [p for p in panels if p[0] in layers]

    cols = 3
    rows = int(np.ceil(len(panels) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4.2 * rows))
    axes = np.atleast_1d(axes).ravel()

    for ax, (key, title, cmap) in zip(axes, panels):
        data = np.where(np.isfinite(layers[key]), layers[key], np.nan)
        im = ax.imshow(data, extent=extent, origin="upper", cmap=cmap)
        ax.set_title(title)
        ax.set_xlabel("Ost [m]")
        ax.set_ylabel("Nord [m]")
        fig.colorbar(im, ax=ax, shrink=0.8)

    for ax in axes[len(panels):]:
        ax.axis("off")

    fig.suptitle("Swiss Snow Model - Neuschneeverteilung", fontsize=14)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path
