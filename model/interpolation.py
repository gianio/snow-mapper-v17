"""Raeumliche Interpolation duenner Wetterpunkte auf das DEM-Raster.

Inverse-Distance-Weighting (IDW) in projizierten Metern. Skalare Felder
(Niederschlag, Temperatur, Windgeschwindigkeit, Punkt-Hoehe) werden direkt
interpoliert; die Windrichtung ueber ihre Sinus-/Kosinus-Komponenten, um den
360->0-Grad-Sprung korrekt zu behandeln. Reine Mathematik, kein Datenzugriff.
"""
from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree


def idw_grid(
    points_xy: np.ndarray,
    values: np.ndarray,
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    power: float = 2.0,
    k: int = 12,
    eps: float = 1e-6,
) -> np.ndarray:
    """Interpoliert ``values`` an den Punkten ``points_xy`` auf ein Gitter.

    Nutzt einen KD-Baum und die ``k`` naechsten Stuetzpunkte je Zielzelle -
    skaliert damit auf Millionen Zellen (O(M log N) statt O(M*N)).

    Parameters
    ----------
    points_xy : (N, 2)  Stuetzpunkte in Metern (Ost, Nord).
    values    : (N,)    Werte an den Stuetzpunkten.
    grid_x, grid_y : (H, W)  Zielkoordinaten (meshgrid, Meter).
    power     : IDW-Exponent (2 = klassisch).
    k         : Anzahl naechster Nachbarn.

    Returns
    -------
    (H, W) interpoliertes Feld.
    """
    points_xy = np.asarray(points_xy, dtype="float64")
    values = np.asarray(values, dtype="float64")
    k = int(min(k, len(points_xy)))

    tree = cKDTree(points_xy)
    targets = np.column_stack([grid_x.ravel(), grid_y.ravel()])
    dist, idx = tree.query(targets, k=k)
    if k == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    weights = 1.0 / np.power(dist + eps, power)
    interp = np.sum(weights * values[idx], axis=1) / np.sum(weights, axis=1)
    return interp.reshape(grid_x.shape)


def idw_direction_grid(
    points_xy: np.ndarray,
    direction_deg: np.ndarray,
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    power: float = 2.0,
) -> np.ndarray:
    """IDW fuer Windrichtung ueber u/v-Komponenten (Grad, [0, 360))."""
    rad = np.radians(direction_deg)
    u = idw_grid(points_xy, np.sin(rad), grid_x, grid_y, power)
    v = idw_grid(points_xy, np.cos(rad), grid_x, grid_y, power)
    return np.degrees(np.arctan2(u, v)) % 360.0
