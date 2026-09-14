"""Ablation: Schmelze und Setzung der Schneedecke.

Warum dieses Modul existiert
----------------------------
Die Schneehoehe wurde bisher als reine Kumulativsumme des Neuschnees
gerechnet -- ueber 264 Stunden schmilzt und setzt sich nichts. Eine
Suedflanke bei 1800 m im April behielt damit jeden Zentimeter, genau wie
eine Nordflanke. Das ist der auffaelligste Unterschied zur SLF-Karte, und
er laesst sich nicht mit mehr Auflösung beheben.

Ansatz
------
Erweiterter Temperatur-Strahlungs-Index (Hock 1999; Pellicciotti et al.
2005):

    melt [mm w.e./h] = TF * max(0, T - T_melt) + SRF * (1 - alpha) * I

``I`` ist die kurzwellige Einstrahlung auf die *geneigte* Flaeche, also
inklusive Hangneigung und Exposition -- genau der Term, der Nord- und
Suedhang trennt. Das ist deutlich besser als ein reiner Gradtagfaktor und
ungleich billiger als eine Energiebilanz (SNOWPACK). Es ersetzt SNOWPACK
nicht und soll das auch nicht.

Bewusste Vereinfachungen
------------------------
* **Keine Horizontabschattung.** Der Selbstschattenterm (cos i <= 0) ist
  enthalten, die Abschattung durch *ferne* Berge nicht. Auf einem 3-km-
  Modellgitter ist ein Horizont ohnehin kaum aufgeloest; relevant wird er
  ab ~250 m (siehe docs/slf-parity-and-tiering.md, Phase 2). Die Anzeige
  nutzt dafuer bereits das 12-Azimut-Feld ``RHOR``.
* **Klarhimmel x Sonnenanteil** statt echter Wolkenstrahlung: die
  stuendliche Sonnenscheindauer moduliert die Klarhimmeleinstrahlung.
* **Kein Schmelzwasserrueckhalt, keine Wiedergefrierung.**
"""

from __future__ import annotations

import numpy as np

# Solarkonstante [W/m^2].
_I0 = 1361.0


def solar_geometry(day_of_year: int, hour_utc: float, lat_deg):
    """Sonnenstand fuer einen Zeitpunkt.

    Returns
    -------
    (sin_elev, azimuth_rad)
        ``sin_elev`` ist auf >= 0 beschnitten (Nacht -> 0). ``azimuth_rad``
        zaehlt von Nord im Uhrzeigersinn, passend zu ``aspect_deg``.
    """
    decl = np.radians(23.45) * np.sin(2 * np.pi * (284 + day_of_year) / 365.0)
    lat = np.radians(np.asarray(lat_deg, dtype="float64"))
    # Stundenwinkel: 0 im Sonnenhoechststand, 15 deg pro Stunde.
    ha = np.radians((hour_utc - 12.0) * 15.0)

    sin_el = np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(ha)
    sin_el = np.clip(sin_el, 0.0, 1.0)
    el = np.arcsin(sin_el)

    cos_az = np.clip(
        (np.sin(decl) - sin_el * np.sin(lat)) / (np.cos(el) * np.cos(lat) + 1e-9),
        -1.0, 1.0)
    az = np.arccos(cos_az)
    if ha > 0:                      # Nachmittag -> westlich
        az = 2 * np.pi - az
    return sin_el, np.broadcast_to(np.asarray(az), np.shape(sin_el)).copy()


def slope_irradiance(sin_el, az_rad, slope_rad, aspect_rad, params):
    """Kurzwellige Klarhimmeleinstrahlung auf die geneigte Flaeche [W/m^2].

    Direkt + diffus. Der Direktanteil verschwindet, wenn die Sonne hinter
    dem eigenen Hang steht (cos i <= 0) -- das ist der Selbstschatten.
    """
    p = params["ablation"]
    tau = float(p["transmissivity"])
    dfrac = float(p["diffuse_frac"])

    lit = sin_el > 0.02
    # Luftmasse und atmosphaerische Schwaechung.
    am = 1.0 / np.maximum(0.05, sin_el)
    beam_normal = _I0 * np.power(tau, am)

    cos_i = (np.cos(slope_rad) * sin_el
             + np.sin(slope_rad) * np.cos(np.arcsin(sin_el)) * np.cos(az_rad - aspect_rad))
    beam = np.where(lit & (cos_i > 0), beam_normal * cos_i, 0.0)

    # Himmelssichtfaktor: eine flache Flaeche sieht den ganzen Himmel.
    skyview = (1.0 + np.cos(slope_rad)) / 2.0
    diff = np.where(lit, dfrac * _I0 * sin_el * skyview, 0.0)
    return beam + diff


def snow_albedo(hours_since_snow, params):
    """Albedo, die mit dem Alter der Oberflaeche exponentiell abfaellt."""
    p = params["ablation"]
    a_new, a_old = float(p["albedo_fresh"]), float(p["albedo_old"])
    tau = max(1.0, float(p["albedo_tau_h"]))
    return a_old + (a_new - a_old) * np.exp(-np.asarray(hours_since_snow) / tau)


def melt_depth_cm(temp_c, irradiance, sun_frac, albedo, params):
    """Schmelze einer Stunde als *Hoehenverlust* [cm].

    Der Strahlungsterm wird mit dem stuendlichen Sonnenanteil skaliert
    (Klarhimmel x Bewoelkungsmodulation), der Temperaturterm nicht -- der
    wirkt auch unter Wolken.
    """
    p = params["ablation"]
    tf = float(p["tf_mm_per_h_per_k"])
    srf = float(p["srf_mm_per_h_per_wm2"])
    t_melt = float(p["t_melt_c"])
    rho = float(p["melt_density_kg_m3"])

    # Bewoelkung: 0.2 Grundstrahlung auch bei voller Bedeckung (diffus).
    cloud = 0.2 + 0.8 * np.clip(sun_frac, 0.0, 1.0)

    mm_we = (tf * np.maximum(0.0, temp_c - t_melt)
             + srf * (1.0 - albedo) * irradiance * cloud)
    mm_we = np.maximum(0.0, mm_we)
    # 1 mm w.e. = 1 kg/m^2 -> Hoehe = 100/rho cm.
    return mm_we * (100.0 / rho)


def settling_rate(hours_since_snow, params):
    """Setzungsrate [1/h], die mit dem Alter der Oberflaeche abfaellt.

    Frischer Schnee verdichtet sich schnell, eine durchgesetzte Decke kaum
    noch. Eine konstante Rate auf die Gesamthoehe angewandt haette eine
    200-cm-Decke dauerhaft 0.7 cm/h verlieren lassen -- und das
    aspektabhaengige Strahlungssignal vollstaendig uebertoent.
    """
    p = params["ablation"]
    s_new = float(p["settling_frac_per_h"])
    s_old = float(p["settling_frac_min_per_h"])
    tau = max(1.0, float(p["settling_tau_h"]))
    return s_old + (s_new - s_old) * np.exp(-np.asarray(hours_since_snow) / tau)


def step_snowpack(depth_cm, new_snow_cm, hours_since_snow, temp_c, irradiance,
                  sun_frac, params):
    """Eine Stunde Schneedecke: Zuwachs, Schmelze, Setzung.

    Gibt ``(depth_neu, hours_since_snow_neu, ablation_cm)`` zurueck.
    ``ablation_cm`` ist der *tatsaechlich angewandte* Verlust, also nie
    mehr als vorhanden war -- damit der Client mit
    ``max(0, cum + SNOW - ABL)`` exakt dasselbe reproduziert.
    """
    p = params["ablation"]
    fresh_thr = float(p["fresh_snow_threshold_cm"])

    hours_since_snow = np.where(new_snow_cm >= fresh_thr, 0.0, hours_since_snow + 1.0)
    albedo = snow_albedo(hours_since_snow, params)

    grown = depth_cm + new_snow_cm
    melt = melt_depth_cm(temp_c, irradiance, sun_frac, albedo, params)
    # Setzung greift an der vorhandenen Decke an, nicht am Neuschnee dieser
    # Stunde -- sonst wuerde frischer Schnee doppelt bestraft.
    compaction = settling_rate(hours_since_snow, params) * depth_cm

    loss = np.minimum(grown, melt + compaction)
    return grown - loss, hours_since_snow, loss
