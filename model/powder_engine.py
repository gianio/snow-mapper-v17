"""Powder Decision Engine — Python-Referenzimplementierung.

Dies ist die 1:1-Portierung der JS-Engine ``computePowder`` aus
``pipeline/interactive_export.py`` plus drei dokumentierte Verbesserungen
(I1–I3). Die JS-Engine wird synchron gehalten; diese Datei ist die
Referenz fuer den Accuracy-Harness (``tools/eval_powder.py``).

Regeln (Destroy / Preserve / Degrade):
  D0  kein Neuschnee in den letzten N Tagen            -> kein Powder   [NEU I1]
  D1  Regen in den letzten 48 h (Niederschlag & T>1°C) -> kein Powder
  D2  > 4 Frost-Tau-Wechsel seit letztem Schneefall    -> kein Powder
  D3  Solareintrag >= Schwelle                         -> kein Powder
      (Schwelle skaliert bei Kaelte hoch: tmax < -4°C  -> x1.5)         [NEU I2]
  R1  tmax < -2°C                                      -> alle Expositionen
  R2  windstill (Gust<20, Mittel<10 km/h)              -> alle Expositionen
  R3  Verfrachtung (Gust 20–60, Mittel<30)             -> Lee-Expositionen
  R4  Frost + klare Nacht                              -> N
  Mod Solar 2000–5000 Wh                               -> S, W raus
  G1  >= 2 Frost-Tau-Wechsel                           -> quality='reduced'
  G2  Nachmittags warm (tmax >= 3°C) + Sonne           -> quality='reduced';
      ab tmax >= 6°C zusaetzlich S raus                                 [NEU I3]
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# --- Konstanten (identisch zur JS-Engine halten!) ---
PD_RAIN_LOOKBACK_H = 48
PD_RAIN_TEMP_C = 1.0
PD_RAIN_MIN_MM = 0.1
PD_FT_DESTROY = 4
PD_FT_DEGRADE = 2
PD_SOLAR_DESTROY_WH = 5000.0
PD_SOLAR_MOD_WH = 2000.0
PD_DEEP_FREEZE_C = -2.0
PD_FREEZE_CLEAR_MIN_C = -2.0
PD_FREEZE_CLEAR_MAX_C = 5.0
PD_GUST_CALM_KMH = 20.0
PD_WIND_CALM_KMH = 10.0
PD_GUST_REDIST_MIN_KMH = 20.0
PD_GUST_REDIST_MAX_KMH = 60.0
PD_WIND_REDIST_KMH = 30.0
PD_GUST_FACTOR = 1.5
# Neu (I1–I3):
PD_MIN_NEWSNOW_CM = 0.5        # I1: min. Neuschnee im Lookback (trace zaehlt)
PD_NEWSNOW_LOOKBACK_H = 168    # I1: 7 Tage
PD_COLD_SOLAR_TMAX_C = -4.0    # I2: unter diesem tmax Solar-Schwelle anheben
PD_COLD_SOLAR_FACTOR = 1.5     # I2
PD_WET_PM_TMAX_C = 3.0         # I3: Nachmittagsnaesse -> reduced
PD_WET_PM_SUN_H = 1.0          # I3: min. Sonnenstunden im Fenster
PD_WET_PM_DROP_S_C = 6.0       # I3: ab hier S-Expositionen raus
PD_PERSIST_CHECK_H = 72        # I4: Kalt-Persistenz-Pruefung (h)
PD_PERSIST_WARM_C = 0.5        # I4: "warm" ab dieser Temperatur
PD_PERSIST_WARM_SHARE = 0.35   # I4: >35% warme Stunden -> kein alter Powder

_QCEN = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}
_ALL = ["N", "E", "S", "W"]


def aspect_quadrant(deg: float) -> str:
    a = ((deg % 360.0) + 360.0) % 360.0
    if a >= 315 or a < 45:
        return "N"
    if a < 135:
        return "E"
    if a < 225:
        return "S"
    return "W"


def _is_lee(cell_asp_deg: float, wind_from_deg: float) -> bool:
    lee = (wind_from_deg + 180.0) % 360.0
    d = abs(cell_asp_deg - lee)
    if d > 180:
        d = 360 - d
    return d < 90


@dataclass
class PowderResult:
    powdered: bool = False
    valid_aspects: list = field(default_factory=list)
    reason_flags: list = field(default_factory=list)
    quality: str = "stable"


def compute_powder(
    *,
    temp_c: list,          # stuendlich, Index 0 = Serienstart
    precip_mm: list,       # stuendlicher Regen/Niederschlag (mm)
    snowfall_cm: list,     # stuendlicher Neuschnee (cm)
    wind_kmh: list,        # stuendliches Windmittel (km/h)
    wind_dir_deg: list,    # stuendliche Windrichtung (Herkunft, Grad)
    sun_h: list,           # stuendliche Sonnenscheindauer (0..1 h)
    solar_wh: float,       # Solareintrag im Fenster (Wh/m^2, Tagesgroessenordnung)
    ta: int,               # Fensterstart (Index)
    tb: int,               # Fensterende (Index, exklusiv)
    aspect_deg: float | None = None,  # Zellen-Exposition; None = nur Aspekt-Liste
) -> PowderResult:
    res = PowderResult()
    n = len(temp_c)
    ta = max(0, ta)
    tb = min(n, tb)
    if tb <= ta:
        return res

    quad = aspect_quadrant(aspect_deg) if aspect_deg is not None else None

    # Dominanter Wind + Mittelwind im Fenster
    ss = sc = ws = 0.0
    wc = 0
    for t in range(ta, tb):
        d = math.radians(wind_dir_deg[t])
        ss += math.sin(d)
        sc += math.cos(d)
        ws += wind_kmh[t]
        wc += 1
    dom_w = (math.degrees(math.atan2(ss, sc)) + 360.0) % 360.0
    mean_w = ws / max(1, wc)
    gust_w = mean_w * PD_GUST_FACTOR

    # Letzter Schneefall ueber die GANZE verfuegbare Serie (fuer D0/D1)
    last_snow_glob = -1
    for t in range(0, tb):
        if snowfall_cm[t] > 0.1:
            last_snow_glob = t

    # I1 / D0': ohne Neuschnee kein Powder — AUSSER die Decke blieb kalt
    # (alter Pulver haelt sich in hoher, schattiger Lage wochenlang).
    s0 = max(0, tb - PD_NEWSNOW_LOOKBACK_H)
    new_snow = sum(snowfall_cm[s0:tb])
    aged = False
    if new_snow < PD_MIN_NEWSNOW_CM:
        w0 = max(0, tb - PD_PERSIST_CHECK_H)
        warm = sum(1 for t in range(w0, tb) if temp_c[t] > PD_PERSIST_WARM_C)
        if warm / max(1, tb - w0) > PD_PERSIST_WARM_SHARE:
            res.reason_flags.append("D0_no_recent_snow")
            return res
        aged = True
        res.reason_flags.append("I4_old_snow_persist")

    # D1': Regen in den letzten 48 h — aber nur NACH dem letzten Schneefall
    # (Neuschnee auf einer Regenkruste ist trotzdem Pulver).
    r0 = max(0, tb - PD_RAIN_LOOKBACK_H)
    if last_snow_glob >= 0:
        r0 = max(r0, last_snow_glob + 1)
    for t in range(r0, tb):
        if precip_mm[t] > PD_RAIN_MIN_MM and temp_c[t] > PD_RAIN_TEMP_C:
            res.reason_flags.append("D1_rain")
            return res

    # D2: Frost-Tau-Wechsel seit letztem Schneefall
    last_snow = ta
    for t in range(ta, tb):
        if snowfall_cm[t] > 0:
            last_snow = t
    ftc = 0.0
    below = temp_c[last_snow] < 0
    for t in range(last_snow + 1, tb):
        b2 = temp_c[t] < 0
        if b2 != below:
            ftc += 0.5
            below = b2
    ftc = int(ftc)
    if ftc > PD_FT_DESTROY:
        res.reason_flags.append("D2_freeze_thaw")
        return res

    tmax = max(temp_c[ta:tb])

    # D3: Solareintrag (I2: kalte Schneedecke vertraegt mehr)
    destroy_wh = PD_SOLAR_DESTROY_WH
    if tmax < PD_COLD_SOLAR_TMAX_C:
        destroy_wh *= PD_COLD_SOLAR_FACTOR
        res.reason_flags.append("I2_cold_solar_relax")
    if solar_wh >= destroy_wh:
        res.reason_flags.append("D3_solar_melt")
        return res

    valid: set = set()
    # R1
    if tmax < PD_DEEP_FREEZE_C:
        res.reason_flags.append("R1_deep_freeze")
        valid.update(_ALL)
    # R2
    if gust_w < PD_GUST_CALM_KMH and mean_w < PD_WIND_CALM_KMH:
        res.reason_flags.append("R2_calm_wind")
        valid.update(_ALL)
    # R3
    if PD_GUST_REDIST_MIN_KMH <= gust_w <= PD_GUST_REDIST_MAX_KMH and mean_w < PD_WIND_REDIST_KMH:
        res.reason_flags.append("R3_wind_redistribution")
        for q in _ALL:
            if _is_lee(_QCEN[q], dom_w):
                valid.add(q)
    # R4
    clear_night = any(sun_h[t] < 0.01 and temp_c[t] < 0 for t in range(ta, tb))
    if PD_FREEZE_CLEAR_MIN_C <= tmax < PD_FREEZE_CLEAR_MAX_C and clear_night:
        res.reason_flags.append("R4_freeze_clear_night")
        valid.add("N")

    if not valid:
        return res

    # Solar-Moderation — nur wenn die Decke nicht tiefgefroren ist:
    # unter -2°C bleibt Pulver auch auf Sonnenhaengen trocken. [I3b]
    if tmax >= PD_DEEP_FREEZE_C and PD_SOLAR_MOD_WH <= solar_wh < destroy_wh:
        valid.discard("S")
        valid.discard("W")

    # I3: Nachmittagsnaesse
    win_sun = sum(sun_h[ta:tb])
    if tmax >= PD_WET_PM_TMAX_C and win_sun >= PD_WET_PM_SUN_H:
        res.reason_flags.append("G2_wet_afternoon")
        res.quality = "reduced"
        if tmax >= PD_WET_PM_DROP_S_C:
            valid.discard("S")

    res.valid_aspects = sorted(valid)
    if quad is not None and quad not in valid:
        return res
    if not valid:
        return res

    if ftc >= PD_FT_DEGRADE or aged:
        res.quality = "reduced"
    res.powdered = True
    return res
