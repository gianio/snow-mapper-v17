"""Powder-Bewertung entlang einer Skitour.

Idee
----
Skitourenguru bewertet das *Risiko* einer Route mit SLABS (Schmudlach &
Koehler): nichtlinear in der Hangneigung, linear in Gefahrenstufe, Hoehe und
Exposition, kalibriert an 57'800 km GPS-Tracks und 1'250 Unfaellen. Die
Geometrie-Maschinerie ist dieselbe, die Zielgroesse nicht: wir bewerten
*Schneequalitaet*, nicht Gefahr.

Warum eine Verteilung und kein Mittelwert
-----------------------------------------
Ein Mittelwert ueber die Route ist wertlos. "40 % kalter Powder, 35 %
windgepresst, 25 % Sonnendeckel" sagt genau das, was jemand wissen will --
naemlich ob es *irgendwo* gut ist. Ein Mittelwert verwischt exakt die eine
gute Rinne.

Die wichtigste Design-Entscheidung
----------------------------------
**Ein Powder-Score darf keine Attraktivitaetsbewertung fuer Lawinengelaende
werden.** Leuchtet der Score an einer 38-Grad-Nordflanke bei Stufe 3 gruen
auf, lenkt er Leute genau in die Kernzone des Bulletins. Deshalb:

* jedes Segment in der Kernzone wird markiert (``core_zone``),
* ``verdict`` wird geklemmt, sobald ein relevanter Anteil der Route in der
  Kernzone liegt -- unabhaengig davon, wie gut der Schnee waere,
* der Score ist explizit KEINE Gefahreneinschaetzung und ersetzt das
  Bulletin nicht. Wir rechnen keine eigene Gefahrenstufe.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Ab diesem Anteil in der Kernzone wird das Urteil geklemmt. 15 % einer
# Route sind schon ein ganzer Hang.
CORE_ZONE_CLAMP_SHARE = 0.15
# Erst ab dieser Neigung traegt ein Segment zum Abfahrtsgenuss bei -- flache
# Zustiegsspuren sollen die Verteilung nicht dominieren.
DESCENT_MIN_SLOPE_DEG = 22.0
# Ueber dieser Neigung ist es kein Powderhang mehr, sondern Steilgelaende.
DESCENT_MAX_SLOPE_DEG = 50.0
_EARTH_R = 6371008.8


@dataclass
class Segment:
    lon: float
    lat: float
    dist_m: float                 # kumulierte Distanz vom Start
    elev_m: Optional[float] = None
    slope_deg: Optional[float] = None
    aspect_deg: Optional[float] = None
    quality: str = "unknown"
    powdered: bool = False
    core_zone: bool = False
    flags: List[str] = field(default_factory=list)


@dataclass
class TourScore:
    segments: List[Segment]
    length_m: float
    distribution: Dict[str, float]      # Qualitaet -> Anteil der Abfahrtslaenge
    descent_m: float                    # Laenge der abfahrtsrelevanten Segmente
    core_zone_share: float
    powder_share: float
    verdict: str
    clamped: bool
    caveats: List[str]


def haversine_m(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def resample_route(coords, step_m: float = 75.0) -> List[Segment]:
    """Route auf gleichmaessige Abstaende bringen.

    swisstopo-Routen sind kartografische Linien mit sehr ungleichen
    Stuetzpunktabstaenden -- ohne Resampling wuerde ein dicht digitalisierter
    Abschnitt die Statistik dominieren, nur weil er mehr Punkte hat.
    """
    pts = [(float(c[0]), float(c[1])) for c in coords if c is not None and len(c) >= 2]
    if len(pts) < 2:
        return [Segment(lon=p[0], lat=p[1], dist_m=0.0) for p in pts[:1]]

    out = [Segment(lon=pts[0][0], lat=pts[0][1], dist_m=0.0)]
    carry = 0.0
    total = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = haversine_m(x0, y0, x1, y1)
        if seg <= 0:
            continue
        walked = step_m - carry
        while walked <= seg:
            f = walked / seg
            out.append(Segment(lon=x0 + (x1 - x0) * f, lat=y0 + (y1 - y0) * f,
                               dist_m=total + walked))
            walked += step_m
        carry = (carry + seg) % step_m
        total += seg
    if out[-1].dist_m < total - 1e-6:
        out.append(Segment(lon=pts[-1][0], lat=pts[-1][1], dist_m=total))
    return out


def _is_descent(seg: Segment) -> bool:
    s = seg.slope_deg
    return s is not None and DESCENT_MIN_SLOPE_DEG <= s <= DESCENT_MAX_SLOPE_DEG


def score_tour(coords, sampler: Callable[[Segment], None],
               step_m: float = 75.0) -> TourScore:
    """Route bewerten.

    ``sampler`` fuellt je Segment ``elev_m``, ``slope_deg``, ``aspect_deg``,
    ``quality``, ``powdered``, ``core_zone`` und ``flags``. Er wird als
    Callback uebergeben, damit dieses Modul nichts ueber das Datenlayout der
    Pipeline wissen muss -- und damit es ohne Pipeline testbar bleibt.
    """
    segs = resample_route(coords, step_m)
    for s in segs:
        sampler(s)

    length_m = segs[-1].dist_m if segs else 0.0

    # Gewichtung nach Segmentlaenge, nicht nach Segmentzahl.
    def weight(i: int) -> float:
        if len(segs) < 2:
            return 1.0
        if i == 0:
            return segs[1].dist_m - segs[0].dist_m
        return segs[i].dist_m - segs[i - 1].dist_m

    desc_w = 0.0
    dist: Dict[str, float] = {}
    core_w = 0.0
    all_w = 0.0
    for i, s in enumerate(segs):
        w = weight(i)
        all_w += w
        if s.core_zone:
            core_w += w
        if _is_descent(s):
            desc_w += w
            dist[s.quality] = dist.get(s.quality, 0.0) + w

    distribution = ({k: v / desc_w for k, v in sorted(dist.items(), key=lambda kv: -kv[1])}
                    if desc_w > 0 else {})
    core_share = (core_w / all_w) if all_w > 0 else 0.0
    powder_w = sum(weight(i) for i, s in enumerate(segs)
                   if _is_descent(s) and s.powdered)
    powder_share = (powder_w / desc_w) if desc_w > 0 else 0.0

    caveats: List[str] = []
    clamped = core_share >= CORE_ZONE_CLAMP_SHARE
    if clamped:
        caveats.append(
            f"{core_share*100:.0f} % der Route liegt in der Kernzone des "
            "Lawinenbulletins.")
    if desc_w == 0:
        caveats.append("Keine abfahrtsrelevanten Segmente "
                       f"({DESCENT_MIN_SLOPE_DEG:.0f}–{DESCENT_MAX_SLOPE_DEG:.0f}°).")
    caveats.append("Schneequalitaet, keine Lawinenbeurteilung. "
                   "Das Bulletin des SLF bleibt maßgeblich.")

    verdict = _verdict(powder_share, desc_w, clamped)
    return TourScore(segments=segs, length_m=length_m, distribution=distribution,
                     descent_m=desc_w, core_zone_share=core_share,
                     powder_share=powder_share, verdict=verdict,
                     clamped=clamped, caveats=caveats)


def _verdict(powder_share: float, descent_m: float, clamped: bool) -> str:
    """Kurzurteil -- bewusst nie werbend, wenn die Kernzone betroffen ist.

    Der geklemmte Fall gibt absichtlich KEIN Qualitaetslob zurueck, auch
    nicht bei perfektem Schnee: sonst wird aus einer Qualitaetsaussage eine
    Empfehlung, in die Kernzone zu fahren.
    """
    if descent_m <= 0:
        return "keine Abfahrtsbewertung"
    if clamped:
        return "Kernzone betroffen – Bulletin zuerst"
    if powder_share >= 0.6:
        return "überwiegend Powder"
    if powder_share >= 0.3:
        return "teilweise Powder"
    if powder_share > 0.0:
        return "vereinzelt Powder"
    return "kein Powder erwartet"
