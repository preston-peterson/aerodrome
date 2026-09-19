#!/usr/bin/env python3
"""Generate Aerodrome's own aircraft type silhouettes (parametric SVG).

These are original, parametrically drawn silhouettes — NOT derivatives of any
third-party shape set. Geometry is computed from a per-type parameter table
(wingspan, length, sweep, tail, engine pods); nothing is traced from another
icon set.

Output contract (what the marker-wiring change expects):
  - one file per ICAO typecode:  static/shapes/<CODE>.svg
  - viewBox "0 0 24 24", nose up at y=0, centered on x=12
  - ONE <path>, single fill, no stroke — the frontend injects
    fill/opacity/stroke; rotation is applied by the marker code
  - <!-- aerodrome-scale: N --> comment in each file: the physical span
    (metres) the silhouette represents, so the frontend can size markers
    proportionally (real-scaled or bucketed)
  - uniform 24px draw is the safe default; small types are drawn small
    ON-CANVAS (a C172 is a ~5-unit speck by design — that IS the data)

Pipeline: per-type part polygons (fuselage / wing / engines / stabilizer /
fin) -> even-odd scanline rasterization (CELL=0.03) -> 8-connected blob
filter (specks <9px dropped) -> Moore-neighbour boundary trace
(search-clockwise-from-entry; exit only when start regains a background
neighbour, so concave re-touches don't truncate the ring) -> RDP simplify
(ring split at the farthest-from-start point first, so the closed loop
can't collapse to a segment).

Edit SOURCES below and re-run; the script regenerates every file, so any
manual tweak to an output file WILL be overwritten. Tune shapes by
adjusting parameters, not paths.

Usage: python3 scripts/gen_type_shapes.py [--out static/shapes] [--report]
"""

import argparse
import math
import os
import re
from xml.dom import minidom

# (wingspan_m, length_m, n_engines, engine_span_m, engine_len_m,
#  tail_wing_m, tail_sweep_u, cargo)
# tail_sweep_u: forward sweep of the horizontal stabilizer tip (u at tip).
SOURCES = {
    # narrowbody twins
    "A19N": (35.8, 46.0, 2, 9.5, 6.2, 4.5, 1.6, False),
    "A20N": (35.8, 37.6, 2, 9.4, 5.9, 4.4, 1.6, False),
    "A21N": (44.3, 44.3, 2, 10.9, 7.2, 5.3, 1.9, False),
    "A319": (35.8, 33.8, 2, 9.3, 5.6, 4.3, 1.6, False),
    "A320": (35.8, 37.6, 2, 9.4, 5.9, 4.4, 1.6, False),
    "A321": (35.8, 44.5, 2, 9.6, 6.8, 4.9, 1.8, False),
    "A332": (60.3, 58.8, 2, 15.6, 9.2, 7.4, 2.6, False),
    "A333": (60.3, 63.7, 2, 15.7, 9.6, 7.6, 2.7, False),
    "A343": (63.5, 63.7, 4, 12.2, 8.2, 7.6, 2.7, False),
    "A359": (64.8, 66.8, 2, 16.8, 10.4, 7.9, 2.8, False),
    "A388": (79.8, 72.7, 4, 17.2, 11.2, 9.2, 3.2, True),   # double-deck fuselage
    "B737": (35.8, 39.5, 2, 9.4, 6.0, 4.5, 1.7, False),
    "B38M": (35.9, 39.5, 2, 9.4, 6.2, 4.5, 1.7, False),   # 737 MAX 8
    "C56X": (17.4, 16.9, 2, 5.8, 3.8, 2.8, 1.2, False),   # Citation Mustang
    "SR22": (11.6, 8.8, 1, 0.0, 0.0, 2.4, 1.0, "taper"),    # Cirrus (tapered wing, near-straight LE)
    "E55P": (19.6, 16.9, 2, 6.2, 4.2, 3.0, 1.3, False),   # Phenom 100
    # --- v3.4.130b: live-traffic coverage gap (Wisconsin scope, 2026-09-19) ---
    "A35K": (64.8, 68.8, 2, 16.8, 10.6, 7.9, 2.8, False),   # A350-1000
    "B39M": (37.3, 43.8, 2, 9.6, 6.4, 4.7, 1.7, False),     # 737 MAX 9
    "B748": (68.4, 76.3, 4, 15.0, 9.6, 8.2, 2.8, True),     # 747-8 (freighter box)
    "B77L": (64.8, 73.9, 2, 17.2, 10.8, 8.2, 2.9, False),   # 777-9 (same 777X plan)
    "B78X": (63.7, 63.7, 2, 16.4, 9.9, 7.6, 2.7, False),    # 787-10
    "BCS1": (35.1, 38.7, 2, 9.6, 6.2, 4.6, 1.8, False),     # A220-100
    "BCS3": (35.1, 38.7, 2, 9.6, 6.2, 4.6, 1.8, False),     # A220-300 (maps to A220 plan)
    "BE58": (13.8, 11.5, 2, 4.6, 3.2, 2.6, 1.0, False),     # Baron
    "C25B": (18.3, 15.0, 2, 6.0, 4.0, 3.0, 1.3, False),     # Citation Citation X
    "C25M": (14.3, 12.5, 2, 4.8, 3.4, 2.5, 1.1, False),     # M2/CJ1+2
    "C340": (16.6, 14.3, 2, 5.4, 3.6, 2.6, 1.1, False),     # 208 Caravan sibling (single turboprop)
    "C68A": (19.2, 15.5, 2, 6.2, 4.0, 3.0, 1.3, False),     # Citation Latitude
    "C700": (22.1, 16.9, 2, 6.8, 4.4, 3.3, 1.4, False),     # Citation Longitude
    "C750": (17.4, 16.9, 2, 5.8, 3.8, 2.8, 1.2, False),     # Citation XLS
    "CL30": (21.2, 20.9, 2, 6.8, 4.4, 3.4, 1.5, False),     # CL-600 (Challenger 601)
    "CL35": (25.8, 24.7, 2, 7.6, 4.8, 3.7, 1.6, False),     # Challenger 300/350
    "CRJ2": (21.2, 26.3, 2, 6.4, 4.8, 3.5, 1.7, False),     # CRJ100/200
    "E170": (28.7, 29.9, 2, 8.0, 5.4, 4.2, 1.7, False),     # ERJ-170
    "E295": (28.6, 26.5, 2, 8.2, 5.4, 4.2, 1.6, False),     # E190-E2 (same-family plan)
    "E550": (19.6, 15.6, 2, 6.2, 4.2, 3.0, 1.3, False),     # Phenom 300
    "F2TH": (19.3, 20.1, 2, 6.4, 4.4, 3.2, 1.5, False),     # Falcon 2000
    "GA5C": (18.0, 16.9, 2, 5.8, 4.0, 2.9, 1.3, False),     # G150
    "GLF4": (28.5, 28.2, 2, 8.2, 5.0, 4.0, 1.7, False),     # G-V / Gulfstream IV
    "GLF5": (28.5, 29.4, 2, 8.4, 5.2, 4.1, 1.7, False),     # G550
    "M20P": (12.5, 9.1, 1, 0.0, 0.0, 2.6, 1.1, "taper"),    # M20 Moose (braced tapered wing)
    "PC24": (17.4, 15.5, 2, 5.8, 3.8, 2.8, 1.2, False),     # PC-24
    "SF50": (17.1, 15.6, 2, 5.8, 4.0, 2.9, 1.4, False),     # Vision SF50
    "B738": (35.8, 39.5, 2, 9.4, 6.0, 4.5, 1.7, False),
    "B739": (35.8, 39.5, 2, 9.4, 6.0, 4.5, 1.7, False),
    "B763": (47.6, 54.9, 2, 12.4, 8.0, 5.9, 2.1, False),
    "B772": (60.9, 63.7, 2, 16.2, 10.0, 7.6, 2.7, False),
    "B77W": (64.8, 73.9, 2, 17.2, 10.8, 8.2, 2.9, False),
    "B788": (57.5, 56.7, 2, 14.8, 9.0, 7.0, 2.5, False),
    "B789": (63.7, 62.8, 2, 16.2, 9.8, 7.5, 2.7, False),
    "E135": (20.0, 31.7, 2, 6.2, 5.0, 3.4, 1.4, False),
    "E145": (20.0, 26.3, 2, 6.2, 5.0, 3.4, 1.4, False),
    "E190": (28.7, 38.5, 2, 8.2, 5.6, 4.3, 1.6, False),
    "E275": (35.1, 40.1, 2, 9.6, 6.0, 4.5, 1.7, False),   # E175/E2
    "CRJ7": (24.9, 32.5, 2, 7.2, 5.2, 3.8, 1.8, False),
    "CRJ9": (24.9, 36.4, 2, 7.3, 5.4, 3.9, 1.8, False),
    "E75L": (28.7, 36.8, 2, 8.2, 5.6, 4.3, 1.7, False),   # E175
    "DH8D": (27.4, 32.8, 2, 8.6, 5.6, 4.0, 1.5, False),
    "AT75": (27.2, 30.8, 2, 8.4, 5.4, 3.9, 1.5, False),
    "AT45": (27.2, 25.5, 2, 8.2, 5.0, 3.7, 1.5, False),
    "SH36": (22.8, 22.8, 2, 7.0, 4.6, 3.4, 1.4, False),
    "JS41": (23.1, 26.2, 2, 7.4, 4.8, 3.5, 1.5, False),
    "BE20": (16.6, 14.3, 2, 5.4, 3.6, 2.6, 1.1, False),
    "C208": (15.9, 12.2, 1, 0.0, 0.0, 2.6, 1.0, False),   # single turboprop
    "PC12": (16.3, 14.4, 1, 0.0, 0.0, 2.8, 1.2, False),
    "C560": (17.4, 16.9, 2, 5.8, 3.8, 2.8, 1.2, False),   # Citation
    "C680": (19.2, 15.5, 2, 6.2, 4.0, 3.0, 1.3, False),   # Citation Sovereign
    "GLF6": (30.4, 30.4, 2, 8.6, 5.2, 4.2, 1.8, False),
    "GA11": (17.2, 18.2, 2, 5.8, 4.0, 2.9, 1.3, False),   # Astra/G150 family
    "SJ30": (16.7, 18.4, 2, 5.8, 4.0, 3.0, 1.5, False),   # Phenom 300
    "SJ45": (19.6, 19.6, 2, 6.6, 4.4, 3.2, 1.5, False),   # Praetor 500
    "B350": (19.0, 15.3, 3, 5.2, 3.4, 2.4, 0.9, False),   # King Air 350
    "B190": (19.3, 16.8, 2, 6.0, 4.0, 3.0, 1.3, False),   # 1900D
    "B712": (28.4, 34.3, 3, 7.6, 5.2, 4.0, 1.6, False),   # 717 / MD-80 family
    "DC93": (27.0, 32.6, 2, 7.6, 5.2, 4.0, 1.7, False),
    "MD83": (32.8, 45.1, 2, 8.6, 6.0, 4.6, 2.0, False),
    "MD11": (51.7, 61.7, 3, 13.4, 8.6, 6.6, 2.4, False),  # + tail engine
    "L101": (44.0, 50.6, 3, 11.2, 7.6, 5.8, 1.6, False),  # S-duct tail engine
    # widebody/heavy props & freighters
    "AN12": (50.5, 33.1, 4, 13.0, 6.4, 6.4, 2.0, True),
    "AN26": (45.5, 31.4, 2, 12.0, 6.2, 6.0, 1.9, True),
    "A400": (45.1, 45.1, 4, 11.6, 6.0, 6.2, 2.4, True),
    "C130": (40.4, 29.8, 4, 10.8, 5.8, 5.6, 1.8, True),
    "C172": (11.0, 8.3, 1, 0.0, 0.0, 2.2, 0.8, False),
    "PA28": (9.1, 7.4, 1, 0.0, 0.0, 2.0, 0.7, False),
    "PA34": (11.6, 8.5, 2, 4.2, 2.8, 2.2, 0.8, False),
    "F150": (11.0, 7.3, 1, 0.0, 0.0, 2.0, 0.7, False),    # Cessna 150
    "B2": (53.6, 21.0, 2, 0.0, 0.0, 0.0, 0.0, False),     # flying wing, below
    "CL60": (21.2, 20.9, 2, 6.8, 4.4, 3.4, 1.5, False),   # Challenger 600
    "FA7X": (26.3, 27.0, 2, 7.8, 5.0, 3.9, 1.7, False),   # Falcon 7X
    "FA8X": (26.3, 27.3, 2, 7.8, 5.0, 3.9, 1.7, False),   # Falcon 8X
}


def _merge(polygon):
    """Union a list of (name, [(x,y)...]) shapes into a single outline.

    Grid rasterization + Moore-neighbour boundary tracing. Output carries no
    grid artifact: shapes are smooth enough at 24px that a 12mm cell reads
    exact, and we round to 0.1 view units.
    """
    W, H, CELL = 24.0, 24.0, 0.03
    nx, ny = int(W / CELL), int(H / CELL)
    grid = bytearray(nx * ny)

    def inside(shape, x, y):
        n = len(shape)
        j = n - 1
        for i in range(n):
            if (shape[i][1] > y) != (shape[j][1] > y):
                xc = shape[j][0] + (y - shape[j][1]) / (shape[i][1] - shape[j][1]) * (
                    shape[i][0] - shape[j][0]
                )
                if x < xc:
                    return True
            j = i
        return False

    for _, shape in polygon:
        ys = [p[1] for p in shape]
        y0, y1 = max(0, int(min(ys) / CELL)), min(ny - 1, int(max(ys) / CELL) + 1)
        for gy in range(y0, y1 + 1):
            cy = (gy + 0.5) * CELL
            # scanline span fill (even-odd) instead of per-pixel point-in-poly
            xints = []
            n = len(shape)
            j = n - 1
            for i in range(n):
                if (shape[i][1] > cy) != (shape[j][1] > cy):
                    xints.append(
                        shape[j][0]
                        + (cy - shape[j][1]) / (shape[i][1] - shape[j][1])
                        * (shape[i][0] - shape[j][0])
                    )
                j = i
            xints.sort()
            row = gy * nx
            for k in range(0, len(xints) - 1, 2):
                xa, xb = xints[k], xints[k + 1]
                gx0 = max(0, int(xa / CELL))
                gx1 = min(nx - 1, int(math.ceil(xb / CELL)) - 1)
                for gx in range(gx0, gx1 + 1):
                    if (gx + 0.5) * CELL >= xa:
                        grid[row + gx] = 1

    def solid(gx, gy):
        if gx < 0 or gy < 0 or gx >= nx or gy >= ny:
            return 0
        return grid[gy * nx + gx]

    # keep every blob >= 9px (drops stray raster specks only; thin parts
    # survive regardless of size)
    seen = bytearray(nx * ny)
    blobs = []
    for sy in range(ny):
        for sx in range(nx):
            if grid[sy * nx + sx] and not seen[sy * nx + sx]:
                stack = [(sx, sy)]
                blob = []
                seen[sy * nx + sx] = 1
                while stack:
                    x, y = stack.pop()
                    blob.append((x, y))
                    for dx, dy in (
                        (1, 0), (-1, 0), (0, 1), (0, -1),
                        (1, 1), (1, -1), (-1, 1), (-1, -1),
                    ):
                        nx_, ny_ = x + dx, y + dy
                        if solid(nx_, ny_) and not seen[ny_ * nx + nx_]:
                            seen[ny_ * nx + nx_] = 1
                            stack.append((nx_, ny_))
                blobs.append(blob)
    if not blobs:
        raise RuntimeError("empty silhouette")
    blobs.sort(key=len, reverse=True)
    biggest = len(blobs[0])
    floor_px = 9  # specks only
    grid = bytearray(nx * ny)
    for blob in blobs:
        if len(blob) >= floor_px:
            for x, y in blob:
                grid[y * nx + x] = 1

    # Moore-neighbour trace (pixel centers), search-clockwise-from-entry:
    # NB lists neighbours clockwise from East; after stepping in neighbour
    # index i, the next search starts at (i+5)%8 (one past the backtrack,
    # clockwise) - the classic rule, which neither 3-cycles nor leaks.
    NB = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
    start = None
    for gy in range(ny):
        for gx in range(nx):
            if grid[gy * nx + gx]:
                start = (gx, gy)
                break
        if start:
            break

    came_from = None
    for i, (dx, dy) in enumerate(NB):
        if not solid(start[0] + dx, start[1] + dy):
            came_from = i
            break
    if came_from is None:
        raise RuntimeError("isolated start pixel")

    contour = [start]
    p = start
    search = came_from
    guard = 0
    left_start = False
    while True:
        found = None
        for k in range(8):
            i = (search + k) % 8
            q = (p[0] + NB[i][0], p[1] + NB[i][1])
            if solid(*q):
                found = (q, i)
                break
        if found is None:
            break  # isolated pixel
        q, i = found
        p = q
        contour.append(q)
        search = (i + 5) % 8
        if p == start:
            # Stop only if the start pixel has acquired a background pixel
            # to its left/below since we left it - otherwise we are merely
            # re-touching the boundary at a concavity (returns-to-start
            # happens mid-contour near rasters' concave corners otherwise).
            if left_start:
                break
            if not solid(start[0] - 1, start[1]) or not solid(start[0], start[1] + 1):
                break
            left_start = True
        guard += 1
        if guard > 4 * nx * ny:
            raise RuntimeError("trace runaway")

    # Closed trace: RDP would flatten it (first==last → straight line), so
    # split the ring at the point farthest from start and simplify each arc.
    pts = [(x * CELL, y * CELL) for x, y in contour]
    n = len(pts)
    dmax, kmax = -1.0, 1
    for k in range(1, n - 1):
        d = (pts[k][0] - pts[0][0]) ** 2 + (pts[k][1] - pts[0][1]) ** 2
        if d > dmax:
            dmax, kmax = d, k
    a = _rdp(pts[: kmax + 1], 0.06)
    b = _rdp(pts[kmax:], 0.06)
    pts = a[:-1] + b[:-1]  # drop duplicated seam points, close with Z
    return pts


def _rdp(points, eps):
    if len(points) < 3:
        return points
    start, end = points[0], points[-1]
    dx, dy = end[0] - start[0], end[1] - start[1]
    norm = math.hypot(dx, dy) or 1.0
    dmax, idx = -1.0, 0
    for i in range(1, len(points) - 1):
        d = abs(dy * points[i][0] - dx * points[i][1] + end[0] * start[1] - end[1] * start[0]) / norm
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        left = _rdp(points[: idx + 1], eps)
        right = _rdp(points[idx:], eps)
        return left[:-1] + right
    return [start, end]


def _poly(points):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def build_source(code, p):
    (span, length, n_eng, eng_span, eng_len, tail_w, tail_sweep, cargo) = p
    # UNIFORM scale (same for x and y); the wingspan metadata drives real
    # size differences at draw time. Per-type: the whole airframe (wing
    # trailing edge + tailplane, or nose-to-tail when longer) is fit into
    # the 24-box with a 1-unit margin, so small types fill the box the way
    # large types do — a C172 reads as a C172, not a speck.
    extent = max(span, length + tail_w)
    s = min(0.55, 22.0 / extent)
    # (legacy per-type fit; small types intentionally stay small on-canvas —
    # wingspan metadata drives real sizing at draw time)
    half = span * s / 2.0
    L = length * s
    y_nose, y_tail = (24.0 - L) / 2.0, (24.0 + L) / 2.0
    cx = 12.0
    fus = max(0.6, L * 0.14)
    taper = cargo == "taper"   # straight leading edge, tapered trailing edge
    if cargo is True:
        fus *= 1.25

    parts = []
    # fuselage (rounded nose cone + taper to tail cone)
    nose_k = L * 0.18
    parts.append((
        "fuse",
        [
            (cx, y_nose),
            (cx + fus * 0.58, y_nose + nose_k * 0.55),
            (cx + fus, y_nose + nose_k),
            (cx + fus, y_tail - fus * 2.4),
            (cx + fus * 0.45, y_tail),
            (cx - fus * 0.45, y_tail),
            (cx - fus, y_tail - fus * 2.4),
            (cx - fus, y_nose + nose_k),
            (cx - fus * 0.58, y_nose + nose_k * 0.55),
        ],
    ))
    # main wing: quarter-chord sweep 0.85 (moderate) / 1.25 (Boeing family),
    # clamped so the tip can never sit behind the root chord (which would
    # invert the quad on short-fat types like the PA-28).
    y_root = y_nose + L * 0.40  # leading edge at the root, mid-fuselage
    sweep_k = 1.25 if code.startswith("B7") else 0.85
    half = span * s / 2.0
    root_chord = L * 0.30
    half_chord = half * 0.32
    max_sweep = max(0.05, root_chord * 0.85 + half_chord * 0.3)
    if taper:
        # Cessna/Cirrus planform: unswept leading edge, swept trailing edge.
        tip_u = y_root
        tip_chord = root_chord * 0.40
    else:
        sweep = min(root_chord * sweep_k + half_chord * 0.55, max_sweep)
        tip_u = y_root + sweep
        tip_chord = root_chord * 0.45
    wing_b = tip_u + tip_chord
    parts.append(("wing", [
        (cx + fus * 0.5, y_root),
        (cx + half, tip_u),
        (cx + half, tip_u + tip_chord),
        (cx - half, tip_u + tip_chord),
        (cx - half, tip_u),
        (cx - fus * 0.5, y_root),
    ]))
    # engines: on-wing (most airliners) or aft-fuselage (regional bizjet layouts)
    aft_mounted = code in ("E135", "E145", "CRJ7", "MD83", "B712", "DC93", "CL60",
                           "FA7X", "FA8X", "GLF6", "GA11", "SJ30", "SJ45", "C560", "C680")
    def engine(ex, ey):
        w = max(0.34, eng_span * s * 0.34)
        ln = eng_len * s
        return (
            "eng",
            [
                (ex, ey - ln * 0.55),
                (ex + w, ey - ln * 0.18),
                (ex + w, ey + ln * 0.45),
                (ex, ey + ln * 0.55),
                (ex - w, ey + ln * 0.45),
                (ex - w, ey - ln * 0.18),
            ],
        )
    if n_eng == 1 and eng_span == 0:  # single prop: no nacelle to draw
        pass
    elif n_eng == 1:
        parts.append(engine(cx, y_nose + nose_k))
    elif aft_mounted:
        ey = y_tail - fus * 1.2 - eng_len * s * 0.3
        ex = max(eng_span * s * 0.14, fus * 0.62)
        if n_eng >= 2:
            parts.append(engine(cx - ex, ey))
            parts.append(engine(cx + ex, ey))
    else:  # on-wing pods at ~55% span
        for i in range(n_eng):
            frac = 0.55 if n_eng == 2 else (0.34 + 0.26 * i)
            ex = cx + (half - fus) * frac * (1 if i % 2 == 0 else -1) if n_eng == 2 else \
                 (cx + (half - fus) * (0.34 + 0.26 * i) if i < 2 else cx - (half - fus) * (0.34 + 0.26 * (i - 2)))
            # pylons/wing fraction to place engine near leading edge slope
            frac_y = (abs(ex - cx) - fus * 0.5) / max(half - fus * 0.5, 0.1)
            ey = y_root + (tip_u - y_root) * min(frac_y, 1.0) + eng_len * s * 0.35
            parts.append(engine(ex, ey))
        if n_eng == 3 and code in ("MD11", "L101"):  # center tail engine
            parts.append(engine(cx, y_tail - fus * 1.0 - eng_len * s * 0.25))
    # horizontal stabilizer, swept both tips
    y_h = y_tail - fus * 0.5
    parts.append(("stab", [
        (cx + fus * 0.5, y_h - 0.2),
        (cx + tail_w, y_tail - tail_sweep),
        (cx + tail_w, y_tail),
        (cx - tail_w, y_tail),
        (cx - tail_w, y_tail - tail_sweep),
        (cx - fus * 0.5, y_h - 0.2),
    ]))
    # vertical fin, from above (visible wedge on the tail)
    parts.append(("fin", [
        (cx + fus * 0.35, y_tail - fus * 2.2),
        (cx + fus * 0.9, y_tail),
        (cx - fus * 0.9, y_tail),
        (cx - fus * 0.35, y_tail - fus * 2.2),
    ]))
    pts = _merge(parts)
    return pts, span


def build_flying_wing():
    # B-2: cranked-arrow planform, sawtooth trailing edge, no tails.
    # Same scale rule as build_source: extent 53.6m -> s = 22/53.6 = 0.410.
    s = 22.0 / 53.6
    cx = 12.0
    half = 53.6 * s / 2
    nose_y, tail_y = 12.0 - 21.0 * s / 2, 12.0 + 21.0 * s / 2
    tip_y = tail_y - 1.4
    pts = [
        (cx, nose_y),
        (cx + half, tip_y),
        (cx + 6.2 * s, tail_y),           # leading crank in
        (cx + 4.4 * s, tip_y + 1.2),      # sawtooth notch
        (cx + 2.6 * s, tail_y),
        (cx + 1.4 * s, tip_y + 1.4),
        (cx, tail_y - 0.4),
    ]
    mirror = [(2 * cx - x, y) for x, y in reversed(pts[1:-1])]
    return pts + mirror, 53.6


BUILDERS = {"B2": build_flying_wing}


def svg_for(code, pts, wingspan_m):
    d = "M " + " ".join(
        ("L " if i else "") + f"{x:.2f} {y:.2f} " for i, (x, y) in enumerate(pts)
    ) + "Z"
    # round-trip coordinates for compactness
    d = d.replace("L ", "L")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n'
        f'<!-- aerodrome-scale: {wingspan_m:.1f} -->\n'
        f'<path d="{d.strip()}"/>\n</svg>\n'
    )


def build_js(svg_dir="static/shapes", out_path="static/shapes-data.js"):
    """Collect the <path> of every generated SVG into the frontend data file
    (/static/shapes-data.js). Serves templates/index.html's radar and the
    display board via one 21KB JS constant — one request per page, no per-
    aircraft fetch. The SVG tree under --out stays the per-file source;
    this file is the deployment artifact the browser actually loads."""
    import glob
    import json
    paths = {}
    for f in sorted(glob.glob(os.path.join(svg_dir, "*.svg"))):
        code = os.path.basename(f)[:-4]
        with open(f) as fh:
            m = re.search(r"(<path[^>]*/>)", fh.read())
        if not m:
            raise RuntimeError(f"{f}: no <path> found — regenerate")
        paths[code] = m.group(1)
    body = (
        "// Generated by scripts/gen_type_shapes.py — do not edit by hand; edit SOURCES and re-run.\n"
        "// Original parametric silhouettes (see the script header). ICAO typecode -> <path> in a\n"
        "// 0 0 24 24 box, nose up; frontend applies fill/stroke/rotation.\n"
        "const SHAPES_DATA = {\n"
        + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in paths.items())
        + "\n};\n"
    )
    with open(out_path, "w") as f:
        f.write(body)
    return len(paths)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="static/shapes")
    ap.add_argument("--js", default="static/shapes-data.js")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    manifest = []
    for code, p in sorted(SOURCES.items()):
        if code in BUILDERS:
            pts, span = BUILDERS[code]()
        else:
            pts, span = build_source(code, p)
        manifest.append((code, len(pts), span))
        with open(os.path.join(args.out, f"{code}.svg"), "w") as f:
            f.write(svg_for(code, pts, span))
    n_js = build_js(args.out, args.js)
    print(f"wrote {len(manifest)} shapes to {args.out} (+ {args.js}, {n_js} codes)")
    if args.report:
        for code, n, span in manifest:
            print(f"  {code:6s} span={span:5.1f}m  vertices={n:3d}")


if __name__ == "__main__":
    main()
