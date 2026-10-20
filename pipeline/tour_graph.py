"""Ski tours as whole climbs, from the swisstopo route network.

The swisstopo ski-route data is a network: lines run between junctions,
several tours share their lower part and split higher up, or start in two
valleys and meet below the summit. Taking each line as a tour gave pieces
of tours. Here the lines become a graph and every way from a low end
(nothing leads further down) to a high end (nothing leads further up) is
one tour of its own -- so where routes meet or part, each possibility is
there separately, from its lowest to its highest point.

Edges point uphill: nodes are ordered by height, so the graph has no cycles
and a tour can only climb from node to node (the line between two nodes may
dip, the junctions may not).

Pure Python on lon/lat lists; ``elev(lon, lat)`` gives a height or None.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Sequence, Tuple

KX, KY = 78000.0, 111000.0          # metres per degree near 47 N


def _xy(c):
    return c[0] * KX, c[1] * KY


def _dist(a, b):
    return math.hypot((a[0] - b[0]) * KX, (a[1] - b[1]) * KY)


def _thin(cs, tol_m):
    out = [cs[0]]
    for c in cs[1:-1]:
        if _dist(c, out[-1]) >= tol_m:
            out.append(c)
    out.append(cs[-1])
    return out


def _length(cs):
    return sum(_dist(a, b) for a, b in zip(cs, cs[1:]))


class _Grid:
    """Points hashed into cells of the snap distance, for neighbour lookups."""

    def __init__(self, cell):
        self.cell = cell
        self.d = defaultdict(list)

    def key(self, p):
        x, y = _xy(p)
        return int(x // self.cell), int(y // self.cell)

    def add(self, p, item):
        self.d[self.key(p)].append((p, item))

    def near(self, p, r):
        kx, ky = self.key(p)
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for q, item in self.d.get((kx + i, ky + j), ()):
                    if _dist(p, q) <= r:
                        yield q, item


def split_at_junctions(lines: List[List[list]], snap_m: float) -> List[List[list]]:
    """Cut lines where another line's end touches them in the middle (a T)."""
    g = _Grid(snap_m)
    for li, cs in enumerate(lines):
        for vi in range(1, len(cs) - 1):
            g.add(cs[vi], (li, vi))
    cuts = defaultdict(set)
    for li, cs in enumerate(lines):
        for end in (cs[0], cs[-1]):
            best = None
            for q, (lj, vj) in g.near(end, snap_m):
                if lj == li:
                    continue
                d = _dist(end, q)
                if best is None or d < best[0]:
                    best = (d, lj, vj)
            if best:
                cuts[best[1]].add(best[2])
    out = []
    for li, cs in enumerate(lines):
        idx = sorted(cuts.get(li, ()))
        if not idx:
            out.append(cs)
            continue
        s = 0
        for vi in idx:
            if vi - s >= 1:
                out.append(cs[s:vi + 1])
            s = vi
        if len(cs) - 1 - s >= 1:
            out.append(cs[s:])
    return [cs for cs in out if len(cs) >= 2]


def build_graph(lines, names, elev, snap_m=40.0):
    """Nodes = line ends within ``snap_m`` of each other; edges = lines."""
    ends = []                                   # (point, line index, which end)
    for li, cs in enumerate(lines):
        ends.append((cs[0], li, 0))
        ends.append((cs[-1], li, 1))
    parent = list(range(len(ends)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    g = _Grid(snap_m)
    for i, (p, _, _) in enumerate(ends):
        for _, j in g.near(p, snap_m):
            parent[find(i)] = find(j)
        g.add(p, i)
    groups = defaultdict(list)
    for i in range(len(ends)):
        groups[find(i)].append(i)
    node_of = {}
    nodes = []                                  # [lon, lat, height]
    for members in groups.values():
        lon = sum(ends[i][0][0] for i in members) / len(members)
        lat = sum(ends[i][0][1] for i in members) / len(members)
        z = elev(lon, lat)
        if z is None:
            zs = [elev(ends[i][0][0], ends[i][0][1]) for i in members]
            zs = [v for v in zs if v is not None]
            z = sum(zs) / len(zs) if zs else None
        nid = len(nodes)
        nodes.append([lon, lat, z])
        for i in members:
            node_of[(ends[i][1], ends[i][2])] = nid
    edges = []                                  # (from, to, coords, length, name)
    for li, cs in enumerate(lines):
        a, b = node_of[(li, 0)], node_of[(li, 1)]
        if a == b or nodes[a][2] is None or nodes[b][2] is None:
            continue
        # uphill by node height; equal heights by node number, so the order
        # is total and the graph acyclic
        if (nodes[a][2], a) > (nodes[b][2], b):
            a, b, cs = b, a, cs[::-1]
        cs = list(cs)
        cs[0] = nodes[a][:2]
        cs[-1] = nodes[b][:2]
        edges.append((a, b, cs, _length(cs), names[li]))
    return nodes, edges


def enumerate_climbs(nodes, edges, per_top=16, max_total=20000):
    """Every way from a low end to a high end, at most ``per_top`` per top.

    Walked backwards from each top, lower predecessors first: when a top has
    more ways up than the cap, the long climbs from the deep valleys are the
    ones kept.
    """
    inc = defaultdict(list)
    out_deg = defaultdict(int)
    for k, (a, b, _, _, _) in enumerate(edges):
        inc[b].append(k)
        out_deg[a] += 1
    for b in inc:
        inc[b].sort(key=lambda k: nodes[edges[k][0]][2])
    tops = [n for n in range(len(nodes)) if inc.get(n) and not out_deg.get(n)]
    paths = []
    for top in tops:
        found = []
        stack = [(top, [])]
        while stack and len(found) < per_top:
            n, path = stack.pop()
            preds = inc.get(n)
            if not preds:
                if path:
                    found.append(path[::-1])
                continue
            # pushed in reverse so the lowest predecessor is explored first
            for k in reversed(preds):
                stack.append((edges[k][0], path + [k]))
        paths.extend(found)
        if len(paths) >= max_total:
            break
    return paths


def _coords(p, edges):
    cs = []
    for k in p:
        seg = edges[k][2]
        cs.extend(seg if not cs else seg[1:])
    return cs


def _sample(cs, step=50.0):
    out, carry = [cs[0]], 0.0
    for a, b in zip(cs, cs[1:]):
        d = _dist(a, b)
        t = step - carry
        while t <= d:
            f = t / d
            out.append([a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f])
            t += step
        carry = (carry + d) % step
    out.append(cs[-1])
    return out


def _same_climb(ca, cb, tol_m):
    """Two ways are one tour when each runs within ``tol_m`` of the other
    nearly all the way (a road drawn twice, a short parallel bit)."""
    g = _Grid(tol_m)
    for q in cb:
        g.add(q, 0)
    off = sum(1 for p in ca if next(g.near(p, tol_m), None) is None)
    return off <= max(1, 0.05 * len(ca))


def climbs_from_lines(raw: Sequence[dict], elev: Callable[[float, float], Optional[float]],
                      snap_m: float = 40.0, thin_m: float = 15.0,
                      per_top: int = 16, dedupe_m: float = 60.0) -> List[dict]:
    """swisstopo route lines -> one entry per climb (coords low -> high)."""
    lines, names = [], []
    for r in raw:
        cs = r.get("coords") or []
        if len(cs) < 2:
            continue
        lines.append(_thin([list(c[:2]) for c in cs], thin_m))
        names.append(r.get("name"))
    lines = split_at_junctions(lines, snap_m)
    # the split pieces keep the name of the line they came from: match by
    # first point (cheap, and names are only a fallback for the summit name)
    first = {}
    for r in raw:
        cs = r.get("coords") or []
        for c in cs:
            first.setdefault((round(c[0], 4), round(c[1], 4)), r.get("name"))
    piece_names = [first.get((round(cs[0][0], 4), round(cs[0][1], 4))) for cs in lines]
    nodes, edges = build_graph(lines, piece_names, elev, snap_m)
    paths = enumerate_climbs(nodes, edges, per_top=per_top)
    # ways that differ only by a short parallel bit are the same tour
    by_ends = defaultdict(list)
    for p in paths:
        by_ends[(edges[p[0]][0], edges[p[-1]][1])].append(p)
    kept = []
    for group in by_ends.values():
        uniq = []                                # (path, sampled points)
        for p in group:
            sp = _sample(_coords(p, edges))
            if not any(_same_climb(sp, q, dedupe_m) and _same_climb(q, sp, dedupe_m) for _, q in uniq):
                uniq.append((p, sp))
        kept.extend(p for p, _ in uniq)
    out = []
    for p in kept:
        cs = _coords(p, edges)
        nm = [edges[k][4] for k in p if edges[k][4]]
        name = max(set(nm), key=nm.count) if nm else None
        key = ";".join(f"{c[0]:.4f},{c[1]:.4f}" for c in cs[::max(1, len(cs) // 40)])
        tid = "c" + hashlib.md5(key.encode()).hexdigest()[:10]
        out.append({"id": tid, "name": name, "coords": cs})
    return out
