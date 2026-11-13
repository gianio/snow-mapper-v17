"""Fewer, clearer tour variants per summit.

The route network gives every combination of lower and upper branches as a
tour of its own, so one summit could have a dozen "variants" that differ only
in which fork was taken halfway. What a ski tourer chooses between is much
simpler:

  * valley -> summit   from clearly different starting points,
  * hut -> summit      when the climb passes a mountain hut,
  * the approach to a hut (valley -> hut) as a tour of its own.

``prune_variants`` keeps, per summit, the climbs from the lowest start first
and drops every further one that mostly runs on an already kept climb
(``overlap``) or starts from the same place; then it adds the hut variants.
Pure Python on [lon, lat] lists; ``elev(lon, lat)`` gives a height or None.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

KX, KY = 78000.0, 111000.0
HUT_KINDS = {"HUT", "HUTS"}
HUT_NAME = re.compile(r"h(ü|ue)tte|cabane|capanna|chamanna|camona|refuge|rifugio|biwak|bivacco", re.I)


def _d(a, b):
    return math.hypot((a[0] - b[0]) * KX, (a[1] - b[1]) * KY)


def _length(cs):
    return sum(_d(a, b) for a, b in zip(cs, cs[1:]))


def _sample(cs, step=100.0):
    out, carry = [list(cs[0])], 0.0
    for a, b in zip(cs, cs[1:]):
        d = _d(a, b)
        t = step - carry
        while d > 0 and t <= d:
            f = t / d
            out.append([a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f])
            t += step
        carry = (carry + d) % step if d > 0 else carry
    out.append(list(cs[-1]))
    return out


class _Cells:
    def __init__(self, cell):
        self.cell, self.d = cell, {}

    def _k(self, p):
        return int(p[0] * KX // self.cell), int(p[1] * KY // self.cell)

    def add(self, p):
        self.d.setdefault(self._k(p), []).append(p)

    def near(self, p, r):
        kx, ky = self._k(p)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in self.d.get((kx + dx, ky + dy), ()):
                    if _d(p, q) <= r:
                        return True
        return False


def _overlap(pts, cells, tol):
    return sum(1 for p in pts if cells.near(p, tol)) / max(1, len(pts))


def _ends(cs, elev):
    zs = [(elev(c[0], c[1]), c) for c in cs]
    zs = [z for z in zs if z[0] is not None]
    if len(zs) < 2:
        return None
    lo = min(zs, key=lambda z: z[0])
    hi = max(zs, key=lambda z: z[0])
    return ([lo[1][0], lo[1][1], round(lo[0])], [hi[1][0], hi[1][1], round(hi[0])], round(hi[0] - lo[0]))


def huts_from_places(places: Sequence[Tuple[str, float, float, str]]):
    """GeoNames places -> [(name, lat, lon)] of mountain huts."""
    return [(p[0], p[1], p[2]) for p in places
            if p[3] in HUT_KINDS or HUT_NAME.search(p[0] or "")]


def _name_group(rs: List[dict]):
    seen: Dict[str, int] = {}
    for r in rs:
        v = (f"von {r['from']}" if r.get("from")
             else (f"ab {round(r['lo'][2])} m" if r.get("lo") and len(r["lo"]) > 2 else "Variante"))
        seen[v] = seen.get(v, 0) + 1
        if seen[v] > 1:
            v = f"{v} ({seen[v]})"
        r["variant"] = v
        r["name"] = f"{r['pname']} {v}" if len(rs) > 1 else r["pname"]
        r["nvar"] = len(rs)


def prune_variants(routes: List[dict], elev: Callable[[float, float], Optional[float]],
                   huts: Sequence[Tuple[str, float, float]] = (),
                   max_per_peak: int = 3, overlap: float = 0.55, tol_m: float = 80.0,
                   hut_m: float = 150.0, min_hut_gain: float = 250.0) -> List[dict]:
    groups: Dict[str, List[dict]] = {}
    order: List[str] = []
    for r in routes:
        g = r.get("group") or f"id{r.get('id')}"
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(r)

    hut_cells = _Cells(hut_m)
    hut_at: Dict[Tuple[int, int], List[Tuple[str, float, float]]] = {}
    for h in huts:
        p = [h[2], h[1]]
        hut_cells.add(p)
        hut_at.setdefault(hut_cells._k(p), []).append(h)

    def hut_near(p):
        kx, ky = hut_cells._k(p)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for h in hut_at.get((kx + dx, ky + dy), ()):
                    d = _d(p, [h[2], h[1]])
                    if d <= hut_m and (best is None or d < best[0]):
                        best = (d, h)
        return best[1] if best else None

    out: List[dict] = []
    approaches: Dict[str, List[dict]] = {}
    for g in order:
        rs = sorted(groups[g], key=lambda r: ((r.get("lo") or [0, 0, 1e9])[2], r.get("len") or 0))
        kept, cells, froms = [], _Cells(tol_m), set()
        for r in rs:
            pts = _sample(r["coords"])
            if r.get("from") and r["from"] in froms:
                continue
            if kept and _overlap(pts, cells, tol_m) >= overlap:
                continue
            kept.append(r)
            if r.get("from"):
                froms.add(r["from"])
            for p in pts:
                cells.add(p)
            if len(kept) >= max_per_peak:
                break
        # hut -> summit, and the approach to the hut
        extra = []
        for r in list(kept):
            cs = r["coords"]
            n = len(cs)
            for i in range(1, n - 1):
                h = hut_near(cs[i])
                if not h:
                    continue
                if _d(cs[i], cs[0]) < 400 or _d(cs[i], cs[-1]) < 400:
                    break
                if any(x.get("from") == h[0] for x in kept + extra):
                    break
                up, down = cs[i:], cs[:i + 1]
                e_up, e_dn = _ends(up, elev), _ends(down, elev)
                if e_up and e_up[2] >= min_hut_gain:
                    v = dict(r)
                    v.update({"id": f"{r['id']}h", "coords": up, "from": h[0], "lo": e_up[0], "hi": e_up[1],
                              "gain": e_up[2], "len": round(_length(up))})
                    v.pop("route", None)
                    extra.append(v)
                if e_dn and e_dn[2] >= min_hut_gain:
                    a = {"id": f"{r['id']}z", "coords": down, "lo": e_dn[0], "hi": e_dn[1],
                         "gain": e_dn[2], "len": round(_length(down)),
                         "peak": [h[2], h[1], e_dn[1][2]], "group": f"{h[0]}@{h[1]:.4f},{h[2]:.4f}",
                         "pname": f"Zustieg {h[0]}", "hut": True}
                    if r.get("from"):
                        a["from"] = r["from"]
                    approaches.setdefault(a["group"], []).append(a)
                break
        grp = kept + extra
        _name_group(grp)
        out.extend(grp)

    for g, rs in approaches.items():
        rs.sort(key=lambda r: (r["lo"][2], r["len"]))
        kept, cells, froms = [], _Cells(tol_m), set()
        for r in rs:
            pts = _sample(r["coords"])
            if (r.get("from") and r["from"] in froms) or (kept and _overlap(pts, cells, tol_m) >= overlap):
                continue
            kept.append(r)
            if r.get("from"):
                froms.add(r["from"])
            for p in pts:
                cells.add(p)
            if len(kept) >= 2:
                break
        _name_group(kept)
        out.extend(kept)
    return out
