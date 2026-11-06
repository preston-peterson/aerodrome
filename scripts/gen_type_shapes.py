#!/usr/bin/env python3
"""Generate Aerodrome's own aircraft type silhouettes (parametric SVG).

These are original, parametrically drawn silhouettes — NOT derivatives of any
third-party shape set. Geometry is computed from a per-type row (a family
preset plus overrides) and the type's real wingspan and length; nothing is
traced from another icon set.

Output contract (what the marker code expects):
  - one file per ICAO typecode:  static/shapes/<CODE>.svg
  - viewBox "0 0 24 24", nose up, centered on x=12
  - ONE closed <path> of M/L commands, no fill or stroke — the frontend
    injects fill/stroke; rotation is applied by the marker code
  - <!-- aerodrome-scale: N --> comment in each file: the physical span
    (metres) the silhouette represents
  - every shape is fit to the same 22-unit box by its own longest side: the
    shape is a legibility-scaled TYPE SIGN, not a scale drawing

How a shape is built: the parts (fuselage, wing, engines, stabilizer) are
laid out in METRES from the nose, scaled into the box, thickened by BOLD so
thin parts survive at map size, then unioned: even-odd scanline rasterization
(CELL=0.03) -> 8-connected blob filter -> Moore-neighbour boundary trace ->
RDP simplify.

Dimensions: wingspan, length and engine count come from scripts/type_dims.csv,
derived from the FAA Aircraft Characteristics Database (a US-government work,
public domain; feet converted to metres, the with-winglets span where the FAA
lists one). Types the FAA table lacks, or gets wrong, are in EXTRA_DIMS with
their source. Everything else (sweep, wing position, engine and tail layout)
is a per-family preset tuned by eye — no freely licensed table carries those.

To add a type: add a SOURCES row naming the closest family, re-run, and look
at the result. Re-running regenerates every file, so tune shapes by adjusting
rows and presets, never the output files.

Usage: python3 scripts/gen_type_shapes.py [--out static/shapes] [--report] [CODE ...]
"""

import argparse
import csv
import math
import os
import re

BOX = 22.0    # every shape's longest side, in view units
BOLD = 0.25   # outline thickening, in view units (about a quarter pixel at 26 px)
# Legibility floors, in view units. A real 0.8 m nacelle on a 30 m airliner
# would be a fifth of a pixel wide on the map; these keep such parts visible.
MIN_FUS = 1.55
MIN_TIP = 0.70
MIN_POD = 0.95
MIN_PROP = 0.42


# ----------------------------------------------------------------- engines
def jets(d, ln, *etas, ahead=0.62):
    """Pods under the wing at the given span fractions."""
    return [dict(kind="wing", eta=e, d=d, len=ln, ahead=ahead) for e in etas]


def aft(d, ln, at):
    """A pod each side of the rear fuselage, starting at fraction `at` of the length."""
    return [dict(kind="aft", d=d, len=ln, at=at)]


def centre(d, ln, at):
    """One engine on the centreline at the tail (trijets, single-engine jets)."""
    return [dict(kind="centre", d=d, len=ln, at=at)]


def props(d, ahead, behind, prop, *etas):
    """Prop nacelles on the wing: `ahead` of the leading edge, `behind` the trailing edge."""
    return [dict(kind="prop", eta=e, d=d, ahead=ahead, behind=behind, prop=prop) for e in etas]


def nose_prop(prop):
    return [dict(kind="nose", prop=prop)]


def over_wing(d, ln, eta):
    """Pods on pylons above the wing's trailing edge (HondaJet)."""
    return [dict(kind="over", eta=eta, d=d, len=ln)]


# ----------------------------------------------------------------- presets
# Lengths are metres unless noted. Fractions:
#   wing_le          leading edge on the centreline, as a fraction of length
#   cr, ct           wing root and tip chord, as a fraction of span
#   kink             span fraction inboard of which the trailing edge is square
#   inner            span fraction inboard of which the chord is constant
#   nose_len         nose taper length, in fuselage widths
#   tail_len         tail taper length, in fuselage widths (fraction of length
#                    when tail_frac is set: light aircraft taper from the cabin)
#   tail_w           width at the tail end, as a fraction of fuselage width
#   stab             semi (fraction of span), sweep (deg), cr/ct (fraction of
#                    span), pos: conv | cruci | T
PRESETS = {}


def preset(name, parent=None, **over):
    base = dict(PRESETS[parent]) if parent else {}
    base["stab"] = dict(base.get("stab", {}))
    for k, v in over.items():
        if k == "stab":
            base["stab"].update(v)
        else:
            base[k] = v
    PRESETS[name] = base


# -- swept low wing, pods under the wing, conventional tail
preset("airliner", fus_w=3.95, nose_len=1.7, nose_pow=0.62, tail_len=3.3, tail_w=0.14,
       wing_le=0.300, sweep=27.0, cr=0.150, ct=0.042, kink=0.35,
       engines=jets(2.1, 4.4, 0.32),
       stab=dict(semi=0.175, sweep=30.0, cr=0.115, ct=0.040, pos="conv"))
preset("a320", "airliner")
preset("a320neo", "a320", engines=jets(2.4, 4.6, 0.32))
preset("b737", "airliner", fus_w=3.76, sweep=25.0, kink=0.30, cr=0.160, wing_le=0.325)
preset("b737max", "b737", engines=jets(2.35, 4.6, 0.32, ahead=0.68))
preset("b757", "airliner", fus_w=3.76, sweep=25.0, wing_le=0.335, engines=jets(2.6, 5.2, 0.30))
preset("b767", "airliner", fus_w=5.03, sweep=31.5, cr=0.165, engines=jets(2.7, 5.6, 0.33))
preset("b777", "airliner", fus_w=6.20, sweep=31.6, cr=0.172, ct=0.036, kink=0.30,
       engines=jets(3.6, 7.3, 0.31, ahead=0.66), stab=dict(semi=0.166, sweep=35.0))
preset("b787", "airliner", fus_w=5.77, sweep=32.2, cr=0.160, ct=0.030, kink=0.32,
       engines=jets(3.3, 6.0, 0.33, ahead=0.66), stab=dict(sweep=34.0))
preset("a330", "airliner", fus_w=5.64, sweep=30.0, cr=0.165, ct=0.035, kink=0.31,
       engines=jets(3.0, 6.0, 0.31, ahead=0.66))
preset("a340", "a330", wing_le=0.310, engines=jets(2.2, 4.4, 0.31, 0.61, ahead=0.70))
preset("a350", "airliner", fus_w=5.96, sweep=32.0, cr=0.165, ct=0.030, kink=0.31,
       engines=jets(3.4, 6.5, 0.32, ahead=0.66), stab=dict(sweep=34.0))
preset("a220", "airliner", fus_w=3.70, sweep=25.0, wing_le=0.310, engines=jets(2.3, 4.2, 0.31))
preset("ejet", "airliner", fus_w=3.01, sweep=25.0, wing_le=0.330, engines=jets(1.75, 3.6, 0.30),
       stab=dict(cr=0.125))
preset("b747", "airliner", fus_w=6.50, sweep=37.5, cr=0.200, ct=0.062, kink=0.40,
       wing_le=0.265, nose_len=1.5, engines=jets(2.7, 5.2, 0.39, 0.68, ahead=0.72),
       stab=dict(semi=0.172, sweep=37.0, cr=0.150))
preset("a380", "airliner", fus_w=7.14, sweep=33.5, cr=0.220, ct=0.050, kink=0.38,
       wing_le=0.270, nose_len=1.45, engines=jets(3.2, 5.6, 0.37, 0.645, ahead=0.70),
       stab=dict(semi=0.190, sweep=35.0, cr=0.150))
preset("trijet", "airliner", fus_w=6.02, sweep=35.0, cr=0.185, wing_le=0.330,
       engines=jets(2.9, 5.6, 0.33, ahead=0.66) + centre(3.0, 9.0, 0.80),
       stab=dict(semi=0.172, sweep=35.0))

# -- swept wing set aft, pods on the rear fuselage, T-tail
preset("rearjet", fus_w=2.69, nose_len=2.0, nose_pow=0.70, tail_len=3.0, tail_w=0.16,
       wing_le=0.400, sweep=26.0, cr=0.185, ct=0.050, kink=0.30,
       engines=aft(1.5, 3.6, 0.685),
       stab=dict(semi=0.175, sweep=30.0, cr=0.105, ct=0.045, pos="T"))
preset("crj", "rearjet")
preset("erj", "rearjet", fus_w=2.28, sweep=22.7, wing_le=0.430, engines=aft(1.35, 3.3, 0.70))
preset("dc9", "rearjet", fus_w=3.34, sweep=24.5, wing_le=0.430, cr=0.165, nose_len=1.8,
       engines=aft(1.9, 5.0, 0.70), stab=dict(semi=0.186))

# -- business jets: rear pods, no trailing-edge kink
preset("bizjet", "rearjet", kink=None, nose_len=2.4)
preset("citation", "bizjet", fus_w=1.70, sweep=4.0, cr=0.150, ct=0.060, wing_le=0.395,
       nose_len=2.3, engines=aft(1.05, 2.7, 0.60),
       stab=dict(semi=0.185, sweep=12.0, cr=0.100, pos="cruci"))
preset("cj", "citation", fus_w=1.60, engines=aft(1.0, 2.5, 0.61),
       stab=dict(sweep=20.0, pos="T"))
preset("citation_swept", "bizjet", fus_w=1.90, sweep=25.0, cr=0.190, ct=0.050, wing_le=0.410,
       engines=aft(1.2, 3.2, 0.64), stab=dict(semi=0.190, sweep=28.0))
preset("learjet", "bizjet", fus_w=1.75, sweep=14.0, cr=0.200, ct=0.085, wing_le=0.430,
       nose_len=2.8, engines=aft(1.05, 2.8, 0.63), stab=dict(semi=0.205, sweep=26.0))
preset("gulfstream", "bizjet", fus_w=2.39, sweep=30.0, cr=0.200, ct=0.050, wing_le=0.385,
       nose_len=2.6, engines=aft(1.55, 4.6, 0.655), stab=dict(semi=0.190, sweep=32.0, cr=0.115))
preset("global", "bizjet", fus_w=2.70, sweep=34.0, cr=0.200, ct=0.045, wing_le=0.390,
       engines=aft(1.6, 4.6, 0.665), stab=dict(semi=0.190, sweep=33.0, cr=0.115))
preset("challenger", "bizjet", fus_w=2.69, sweep=26.0, cr=0.190, ct=0.050, wing_le=0.400,
       nose_len=2.0, engines=aft(1.4, 3.4, 0.645))
preset("phenom", "bizjet", fus_w=1.75, sweep=24.0, cr=0.170, ct=0.055, wing_le=0.400,
       engines=aft(1.1, 2.7, 0.62))
preset("legacy", "bizjet", fus_w=2.30, sweep=27.0, cr=0.190, ct=0.050, wing_le=0.400,
       engines=aft(1.3, 3.3, 0.645))
preset("falcon", "bizjet", fus_w=2.50, sweep=29.0, cr=0.190, ct=0.050, wing_le=0.400,
       nose_len=2.2, engines=aft(1.25, 3.3, 0.63),
       stab=dict(semi=0.200, sweep=30.0, pos="cruci"))
preset("falcon3", "falcon", engines=aft(1.25, 3.3, 0.63) + centre(1.0, 3.6, 0.70))

# -- straight tapered wing, two prop nacelles, T-tail (Dash 8, ATR)
preset("turboprop", fus_w=2.69, nose_len=1.8, nose_pow=0.62, tail_len=3.4, tail_w=0.14,
       wing_le=0.395, sweep=3.0, cr=0.105, ct=0.050, inner=0.31,
       engines=props(1.15, 2.6, 1.2, 4.0, 0.31),
       stab=dict(semi=0.160, sweep=8.0, cr=0.085, ct=0.050, pos="T"))
preset("atr", "turboprop", fus_w=2.87, wing_le=0.385, engines=props(1.1, 2.3, 0.3, 3.93, 0.30))
# -- four-engine military transports: blunt nose, big square tail
preset("hercules", "turboprop", fus_w=3.9, nose_len=1.0, nose_pow=0.5, wing_le=0.300,
       cr=0.122, ct=0.062, inner=0.26, tail_len=3.2, tail_w=0.2,
       engines=props(1.3, 3.3, 0.4, 4.1, 0.245, 0.500),
       stab=dict(semi=0.199, sweep=10.0, cr=0.115, ct=0.050, pos="conv", back=0.02))

# -- light piston single, high wing (Cessna)
preset("single", fus_w=1.15, nose_len=1.15, nose_pow=0.42, tail_len=0.58, tail_frac=True,
       tail_w=0.20, wing_le=0.225, sweep=0.0, cr=0.148, ct=0.103, inner=0.45,
       engines=nose_prop(1.9),
       stab=dict(semi=0.155, sweep=6.0, cr=0.095, ct=0.065, pos="conv", back=0.035))
# -- rectangular fabric wing (Cub, Scout, Maule)
preset("cub", "single", fus_w=0.95, cr=0.150, ct=0.150, inner=None, wing_le=0.215,
       stab=dict(semi=0.150, sweep=10.0, cr=0.105, ct=0.060))
# -- low wing singles
preset("cherokee", "single", wing_le=0.255, cr=0.165, ct=0.125, inner=0.42,
       stab=dict(semi=0.160, sweep=0.0, cr=0.085, ct=0.085))
preset("cirrus", "single", wing_le=0.270, cr=0.128, ct=0.070, inner=None, sweep=1.5,
       nose_len=1.5, nose_pow=0.55, stab=dict(semi=0.165, sweep=8.0, cr=0.085, ct=0.050))
preset("bonanza", "single", wing_le=0.265, cr=0.205, ct=0.090, inner=None, sweep=5.0,
       nose_len=1.5, nose_pow=0.55, stab=dict(semi=0.180, sweep=8.0, cr=0.105, ct=0.060))
preset("mooney", "single", wing_le=0.300, cr=0.170, ct=0.075, inner=None, sweep=0.0,
       nose_len=1.6, nose_pow=0.55, stab=dict(semi=0.165, sweep=0.0, cr=0.105, ct=0.060))
# -- single turboprops: long pointed nose
preset("turbine_single", "single", fus_w=1.45, nose_len=2.4, nose_pow=0.75, wing_le=0.360,
       cr=0.130, ct=0.068, inner=None, sweep=2.0, tail_len=0.42, engines=nose_prop(2.4),
       stab=dict(semi=0.175, sweep=10.0, cr=0.095, ct=0.055))

# -- light twin, nacelles on a straight tapered wing
preset("twin", fus_w=1.25, nose_len=2.0, nose_pow=0.70, tail_len=0.50, tail_frac=True,
       tail_w=0.18, wing_le=0.285, sweep=4.0, cr=0.180, ct=0.080,
       engines=props(0.80, 1.25, -0.2, 1.95, 0.30),
       stab=dict(semi=0.185, sweep=8.0, cr=0.105, ct=0.060, pos="conv", back=0.03))
preset("kingair", "twin", fus_w=1.50, wing_le=0.300, cr=0.135, ct=0.055, inner=0.31,
       nose_len=2.4, engines=props(0.95, 2.3, 0.0, 2.5, 0.31),
       stab=dict(semi=0.168, sweep=14.0, pos="T"))


def row(family, **over):
    return (family, over)


# ICAO type designator -> (family preset, overrides). Span, length and engine
# count are NOT here: they come from type_dims.csv / EXTRA_DIMS.
SOURCES = {
    # --- Airbus narrowbody
    "A319": row("a320", wing_le=0.285),
    "A320": row("a320"),
    "A321": row("a320", wing_le=0.335),
    "A19N": row("a320neo", wing_le=0.285),
    "A20N": row("a320neo"),
    "A21N": row("a320neo", wing_le=0.335),
    "BCS1": row("a220"),
    "BCS3": row("a220", wing_le=0.325),
    # --- Airbus widebody
    "A332": row("a330"),
    "A333": row("a330", wing_le=0.320),
    "A343": row("a340"),
    "A359": row("a350"),
    "A35K": row("a350", wing_le=0.315),
    "A388": row("a380"),
    # --- Boeing narrowbody
    "B737": row("b737", wing_le=0.300),
    "B738": row("b737"),
    "B739": row("b737", wing_le=0.340),
    "B37M": row("b737max", wing_le=0.305),
    "B38M": row("b737max"),
    "B39M": row("b737max", wing_le=0.340),
    "B752": row("b757"),
    "B753": row("b757", wing_le=0.360),
    # --- Boeing widebody
    "B763": row("b767"),
    "B764": row("b767", wing_le=0.320),
    "B772": row("b777"),
    "B77L": row("b777"),
    "B77W": row("b777", wing_le=0.320, engines=jets(3.8, 7.6, 0.30, ahead=0.66)),
    "B788": row("b787"),
    "B789": row("b787", wing_le=0.320),
    "B78X": row("b787", wing_le=0.335),
    "B744": row("b747"),
    "B748": row("b747", wing_le=0.285, engines=jets(3.0, 5.6, 0.39, 0.68, ahead=0.72)),
    "MD11": row("trijet"),
    "L101": row("trijet", fus_w=5.97, engines=jets(2.8, 5.6, 0.33, ahead=0.66) + centre(2.8, 8.0, 0.80)),
    # --- regional jets
    "E170": row("ejet", wing_le=0.320),
    "E75L": row("ejet"),
    "E190": row("ejet", wing_le=0.340, engines=jets(1.9, 3.9, 0.30)),
    "E275": row("ejet", engines=jets(2.2, 4.0, 0.30, ahead=0.68)),
    "E295": row("ejet", wing_le=0.355, engines=jets(2.3, 4.2, 0.28, ahead=0.68)),
    "CRJ2": row("crj", sweep=24.5, engines=aft(1.4, 3.2, 0.70)),
    "CRJ7": row("crj", engines=aft(1.5, 3.6, 0.70)),
    "CRJ9": row("crj"),
    "E135": row("erj", wing_le=0.420, engines=aft(1.35, 3.3, 0.68)),
    "E145": row("erj"),
    "B712": row("dc9"),
    "DC93": row("dc9"),
    "MD83": row("dc9", engines=aft(1.9, 6.0, 0.715)),
    # --- business jets: Cessna
    "C525": row("cj"),
    "C25A": row("cj"),
    "C25B": row("cj"),
    "C25C": row("cj", sweep=10.0),
    "C25M": row("cj"),
    "C550": row("citation"),
    "C560": row("citation"),
    "C56X": row("citation"),
    "C650": row("citation_swept", fus_w=1.75),
    "C680": row("citation_swept", sweep=14.0, stab=dict(pos="cruci")),
    "C68A": row("citation_swept", sweep=14.0, fus_w=2.10, stab=dict(pos="cruci")),
    "C700": row("citation_swept", sweep=28.0, fus_w=2.10),
    "C750": row("citation_swept", sweep=37.0),
    # --- business jets: others
    "LJ45": row("learjet"),
    "GLF4": row("gulfstream", sweep=28.0),
    "GLF5": row("gulfstream"),
    "GLF6": row("gulfstream", fus_w=2.59, sweep=33.0),
    "GA5C": row("gulfstream", sweep=33.0),
    "GA6C": row("gulfstream", sweep=33.0),
    "GA7C": row("gulfstream", fus_w=2.59, sweep=33.0),
    "G280": row("gulfstream", fus_w=2.10, sweep=31.0, engines=aft(1.25, 3.4, 0.64)),
    "ASTR": row("gulfstream", fus_w=1.60, sweep=34.0, engines=aft(1.05, 2.9, 0.64)),
    "GLEX": row("global"),
    "GL5T": row("global"),
    "GL7T": row("global"),
    "CL60": row("challenger"),
    "CL30": row("challenger", fus_w=2.20, sweep=27.0),
    "CL35": row("challenger", fus_w=2.20, sweep=27.0),
    "E55P": row("phenom"),
    "E545": row("legacy"),
    "E550": row("legacy"),
    "PC24": row("phenom", fus_w=1.90, sweep=10.0, cr=0.160),
    "EA50": row("cj", fus_w=1.50, engines=aft(0.85, 2.0, 0.62)),
    "SJ30": row("phenom", fus_w=1.50, sweep=32.0, engines=aft(0.95, 2.6, 0.64)),
    "HDJT": row("cj", fus_w=1.55, sweep=6.0, wing_le=0.430, engines=over_wing(0.95, 2.6, 0.36)),
    "SF50": row("cj", fus_w=1.55, sweep=4.0, wing_le=0.380, engines=centre(0.85, 2.2, 0.55),
                stab=dict(semi=0.140, sweep=32.0, pos="conv", back=0.0)),   # V-tail
    "F2TH": row("falcon"),
    "FA10": row("falcon", fus_w=1.65, engines=aft(1.0, 2.6, 0.64)),
    "F900": row("falcon3"),
    "FA7X": row("falcon3", sweep=34.0),
    "FA8X": row("falcon3", sweep=34.0),
    # --- turboprop airliners and transports
    "DH8D": row("turboprop"),
    "AT45": row("atr"),
    "AT75": row("atr"),
    "SH36": row("turboprop", fus_w=2.30, nose_len=2.2, wing_le=0.380, cr=0.080, ct=0.062,
                inner=None, sweep=0.0, engines=props(1.0, 2.0, 0.2, 2.8, 0.27),
                stab=dict(pos="conv", back=0.02, sweep=4.0)),
    "JS41": row("kingair", fus_w=1.98, wing_le=0.360, engines=props(0.95, 2.1, 0.2, 2.9, 0.34),
                stab=dict(pos="cruci", sweep=20.0)),
    "B190": row("kingair", fus_w=1.40, wing_le=0.360, nose_len=2.8),
    "BE20": row("kingair"),
    "B350": row("kingair", wing_le=0.310),
    "C441": row("kingair", engines=props(0.85, 2.0, 0.0, 2.3, 0.30),
                stab=dict(pos="conv", back=0.03, sweep=10.0)),
    "C130": row("hercules"),
    "C30J": row("hercules", wing_le=0.330),
    "AN12": row("hercules", fus_w=4.1, wing_le=0.330, nose_len=1.2),
    "AN26": row("turboprop", fus_w=2.90, wing_le=0.340, cr=0.115, engines=props(1.2, 2.4, 1.0, 3.9, 0.29),
                stab=dict(semi=0.170, pos="conv", back=0.02, sweep=14.0)),
    "A400": row("hercules", fus_w=5.6, nose_len=1.3, nose_pow=0.6, wing_le=0.330, sweep=15.0,
                cr=0.140, ct=0.060, inner=None, engines=props(1.4, 3.6, 0.0, 5.3, 0.27, 0.52),
                stab=dict(semi=0.220, sweep=28.0, pos="T")),
    # --- piston singles, high wing
    "C152": row("single"),
    "C172": row("single"),
    "C72R": row("single"),
    "C175": row("single"),
    "C182": row("single"),
    "C210": row("single", inner=0.30, ct=0.090),
    "T210": row("single", inner=0.30, ct=0.090),
    "PA18": row("cub"),
    "BL8": row("cub"),
    "M5": row("cub", fus_w=1.05),
    "C208": row("turbine_single", fus_w=1.60, nose_len=1.9, wing_le=0.300, cr=0.125, ct=0.078,
                inner=0.40, tail_len=0.50, stab=dict(semi=0.195, sweep=6.0)),
    # --- piston singles, low wing
    "P28A": row("cherokee"),
    "P32R": row("cherokee", nose_len=1.6, nose_pow=0.55),
    "BE35": row("bonanza", stab=dict(semi=0.130, sweep=26.0, cr=0.110, ct=0.050)),   # V-tail
    "BE36": row("bonanza"),
    "BT36": row("bonanza"),
    "SR20": row("cirrus"),
    "SR22": row("cirrus"),
    "S22T": row("cirrus"),
    "M20P": row("mooney"),
    "RV12": row("cherokee", cr=0.155, ct=0.155, inner=None, wing_le=0.270),
    "PA46": row("turbine_single", fus_w=1.30, nose_len=2.0, wing_le=0.300, cr=0.115, ct=0.060,
                engines=nose_prop(2.0)),
    "TBM9": row("turbine_single", fus_w=1.30, wing_le=0.380),
    "EPIC": row("turbine_single", fus_w=1.40, wing_le=0.360),
    "PC12": row("turbine_single", fus_w=1.60, nose_len=2.6, engines=nose_prop(2.67),
                stab=dict(semi=0.160, pos="T")),
    # --- piston twins
    "BE58": row("twin"),
    "PA34": row("twin", cr=0.150, ct=0.110, inner=0.40, stab=dict(sweep=0.0, cr=0.085, ct=0.085)),
    "C340": row("twin", fus_w=1.35, wing_le=0.300),
    "C414": row("twin", fus_w=1.40, wing_le=0.300),
    "AEST": row("twin", fus_w=1.20, wing_le=0.360, nose_len=2.6, cr=0.170, ct=0.065,
                stab=dict(sweep=24.0)),
    "AC50": row("twin", fus_w=1.35, wing_le=0.300, cr=0.150, ct=0.065,
                engines=props(0.85, 1.4, 0.3, 2.0, 0.26)),
    "P68": row("twin", fus_w=1.20, wing_le=0.300, cr=0.130, ct=0.130, sweep=0.0,
               engines=props(0.65, 1.0, -0.4, 1.8, 0.28),
               stab=dict(sweep=0.0, cr=0.090, ct=0.090)),
    # --- flying wing (own builder, see BUILDERS)
    "B2": row("airliner"),
}

# Span and length (metres) for types the FAA table lacks or gets wrong.
EXTRA_DIMS = {
    # --- not in the FAA table
    "GA7C": (31.39, 33.48, "Gulfstream G700 specification sheet"),
    "EPIC": (13.11, 10.92, "Epic Aircraft E1000 technical specifications"),
    "E275": (31.00, 32.40, "SKYbrary, Embraer E175-E2"),
    "SJ30": (12.90, 14.30, "AOPA aircraft guide, SJ30-2"),
    "L101": (47.34, 54.17, "L-1011-1/-100/-200; the shorter -500 shares the designator"),
    "AN26": (29.20, 23.80, "Czech military aircraft register"),
    # --- in the FAA table, but its row describes another variant
    "B744": (64.44, 70.66, "747-400 with winglets; the FAA row has the 747-100/-200 span"),
    "AT75": (27.05, 27.17, "ATR 72-500 fact sheet; the FAA row has the ATR 42 span"),
    "E75L": (28.65, 31.68, "E175 with the extended wingtips; the FAA row has E175-E2 figures"),
    "E135": (20.04, 26.33, "ERJ-135; the FAA row has the longer ERJ-140"),
    "C340": (11.62, 10.46, "Cessna 340A; the FAA row lists 13.20 m, longer than the larger Cessna 414"),
}

# Designators that draw another row's shape. Either a code the feed sends that
# is not an ICAO designator, or one whose plan-form matches a drawn type. The
# frontend lookup is one flat table, so aliases are indistinguishable from
# real codes downstream.
ALIASES = {
    "G150": "ASTR",   # Gulfstream G150: the Astra airframe
    "GA11": "ASTR",   # not an ICAO designator; seen in feeds for the same airframe
    "SJ45": "E545",   # not an ICAO designator
    "PA28": "P28A",   # not an ICAO designator; Cherokees are P28A/P28B/P28R
    "F150": "C152",   # not an ICAO designator; Cessna 150s are C150
}


def load_dims(path=None):
    """ICAO code -> (span_m, length_m) from type_dims.csv, then EXTRA_DIMS on top."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "type_dims.csv")
    dims = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            dims[r["code"]] = (float(r["span_m"]), float(r["length_m"]))
    for code, (span, length, _source) in EXTRA_DIMS.items():
        dims[code] = (span, length)
    return dims


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
    # Only ONE outline is traced below. A second real blob means a part (an
    # engine, a stabilizer) is not touching the rest and would silently
    # vanish from the shape, so refuse instead.
    if sum(1 for blob in blobs if len(blob) >= floor_px) > 1:
        raise RuntimeError("detached part: the shape is not one connected piece")
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


# ---------------------------------------------------------------- geometry
def _sym(right):
    """Mirror a right-hand outline (running nose->tail) about the centreline."""
    return right + [(-x, y) for x, y in reversed(right)]


def _fuselage(L, w, nose_len, nose_pow, tail_len, tail_w, n=9):
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append(((w / 2) * (1 - (1 - t) ** 2) ** nose_pow, nose_len * t))
    for i in range(1, n + 1):
        t = i / n
        pts.append((tail_w / 2 + (w / 2 - tail_w / 2) * (1 - t ** 1.5), L - tail_len + tail_len * t))
    return _sym(pts)


def _surface(stations):
    """Wing or stabilizer from (x, y_leading_edge, chord) stations, root->tip."""
    le = [(x, y) for x, y, c in stations]
    te = [(x, y + c) for x, y, c in reversed(stations)]
    return _sym(le + te)


def _trapezoid(y0, semi, sweep_deg, c_root, c_tip, kink=None, inner=None):
    """Stations for a tapered surface with a clipped tip. y0 is the leading
    edge on the centreline. Returns (stations, le(x), chord(x))."""
    t = math.tan(math.radians(sweep_deg))

    def le(x):
        return y0 + t * x

    def ch(x):
        if inner:
            xi = inner * semi
            if x <= xi:
                return c_root
            return c_root + (c_tip - c_root) * (x - xi) / (semi - xi)
        return c_root + (c_tip - c_root) * x / semi

    st = []
    if kink:
        xk = kink * semi
        st.append((0.0, y0, le(xk) + ch(xk) - y0))
        st.append((xk, le(xk), ch(xk)))
    else:
        st.append((0.0, y0, c_root))
        if inner:
            st.append((inner * semi, le(inner * semi), c_root))
    xi = semi * 0.95
    st.append((xi, le(xi), ch(xi)))
    st.append((semi, le(semi) + 0.28 * c_tip, 0.62 * c_tip))
    return st, le, ch


def _pod(x, y_front, length, width, front=0.7, back=0.6, shoulder=0.35):
    h = width / 2
    sh = min(shoulder * width, length * 0.4)
    return [
        (x - h * front, y_front), (x + h * front, y_front),
        (x + h, y_front + sh), (x + h, y_front + length - sh),
        (x + h * back, y_front + length), (x - h * back, y_front + length),
        (x - h, y_front + length - sh), (x - h, y_front + sh),
    ]


def _bar(x, y, width, thick):
    return [(x - width / 2, y), (x + width / 2, y),
            (x + width / 2, y + thick), (x - width / 2, y + thick)]


def _inflate(pts, d):
    """Offset a simple polygon outward by d (mitred, limit 2.5)."""
    ded = []
    for p in pts:  # _sym() repeats the centreline points; drop the repeats
        if not ded or math.hypot(p[0] - ded[-1][0], p[1] - ded[-1][1]) > 1e-6:
            ded.append(p)
    if len(ded) > 1 and math.hypot(ded[0][0] - ded[-1][0], ded[0][1] - ded[-1][1]) < 1e-6:
        ded.pop()
    pts = ded
    if d <= 0:
        return pts
    n = len(pts)
    area = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
               for i in range(n))
    sign = 1.0 if area > 0 else -1.0
    out = []
    for i in range(n):
        (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[(i + 1) % n]
        ax, ay, bx, by = x1 - x0, y1 - y0, x2 - x1, y2 - y1
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        n1 = (ay / la * sign, -ax / la * sign)
        n2 = (by / lb * sign, -bx / lb * sign)
        mx, my = n1[0] + n2[0], n1[1] + n2[1]
        ml = math.hypot(mx, my)
        if ml < 1e-6:
            out.append((x1 + n1[0] * d, y1 + n1[1] * d))
            continue
        mx, my = mx / ml, my / ml
        k = min(1.0 / max(mx * n1[0] + my * n1[1], 0.4), 2.5)
        out.append((x1 + mx * d * k, y1 + my * d * k))
    return out


def spec_for(code):
    """The full parameter set for a code: its family preset plus the row's overrides."""
    family, over = SOURCES[code]
    sp = dict(PRESETS[family])
    sp["stab"] = dict(sp["stab"])
    for k, v in over.items():
        if k == "stab":
            sp["stab"].update(v)
        else:
            sp[k] = v
    return sp


def build_source(code, dims):
    sp = spec_for(code)
    span, L = dims[code]
    s = BOX / max(span, L)          # view units per metre
    semi = span / 2
    w = max(sp["fus_w"] * 1.08, MIN_FUS / s)
    tail_len = sp["tail_len"] * (L if sp.get("tail_frac") else sp["fus_w"])
    st = sp["stab"]
    t_tail = st["pos"] == "T"
    fus_end = L * (0.975 if t_tail else 1.0)   # a T-tail overhangs the tail cone

    parts = [_fuselage(fus_end, w, sp["nose_len"] * sp["fus_w"], sp["nose_pow"],
                       tail_len, max(sp["tail_w"] * w, 0.45 / s))]

    c_tip = max(sp["ct"] * span, MIN_TIP / s)
    stations, le, ch = _trapezoid(sp["wing_le"] * L, semi, sp["sweep"], sp["cr"] * span,
                                  c_tip, kink=sp.get("kink"), inner=sp.get("inner"))
    parts.append(_surface(stations))

    for e in sp["engines"]:
        kind = e["kind"]
        if kind == "wing":
            d = max(e["d"] * 1.15, MIN_POD / s)
            x = e["eta"] * semi
            for sx in (x, -x):
                parts.append(_pod(sx, le(x) - e["ahead"] * e["len"], e["len"], d,
                                  front=0.8, back=0.55))
        elif kind == "aft":
            d = max(e["d"] * 1.15, MIN_POD / s)
            x = w / 2 + d * 0.6
            for sx in (x, -x):
                parts.append(_pod(sx, e["at"] * L, e["len"], d, front=0.45, back=0.4,
                                  shoulder=0.55))
            parts.append(_bar(0.0, e["at"] * L + e["len"] * 0.35, 2 * x, e["len"] * 0.3))
        elif kind == "centre":
            d = max(e["d"], MIN_POD / s) * 1.25
            ln = min(e["len"], fus_end * 0.99 - e["at"] * L)
            parts.append(_pod(0.0, e["at"] * L, ln, d, front=0.6, back=0.5))
        elif kind == "over":
            d = max(e["d"] * 1.15, MIN_POD / s)
            x = e["eta"] * semi
            for sx in (x, -x):
                parts.append(_pod(sx, le(x) + 0.45 * ch(x), e["len"], d, front=0.6, back=0.5))
        elif kind == "prop":
            d = max(e["d"] * 1.2, MIN_POD / s)
            x = e["eta"] * semi
            ln = e["ahead"] + ch(x) + e["behind"]
            for sx in (x, -x):
                parts.append(_pod(sx, le(x) - e["ahead"], ln, d, front=0.35, back=0.45,
                                  shoulder=0.7))
        elif kind == "nose":
            # A prop bar on the nose marks a single; on wing nacelles it only
            # adds clutter at map size, so multi-engine types get none.
            parts.append(_bar(0.0, 0.18 * w, e["prop"], MIN_PROP / s))

    s_semi = st["semi"] * span
    s_ct = max(st["ct"] * span, MIN_TIP * 0.9 / s)
    s_cr = max(st["cr"] * span, s_ct)
    reach = math.tan(math.radians(st["sweep"])) * s_semi + s_ct
    back = st.get("back", {"T": 0.0, "cruci": 0.05}.get(st["pos"], 0.012))
    y0 = L - reach - back * L
    stations, _, _ = _trapezoid(y0, s_semi, st["sweep"], s_cr, s_ct)
    parts.append(_surface(stations))
    if t_tail:  # the fin top, joining the tail cone to the stabilizer
        parts.append(_sym([(0.22 * w, fus_end - tail_len * 0.6), (0.16 * w, y0 + s_cr * 0.9)]))

    # Into the 24-box: centred, shrunk by the bolding so the result still
    # fits the 22-unit box.
    k = (BOX - 2 * BOLD) / BOX
    y_off = 12.0 - L * s / 2
    placed = [("p", _inflate([(12.0 + x * s * k, 12.0 + (y_off + y * s - 12.0) * k)
                              for x, y in p], BOLD)) for p in parts]
    return _merge(placed), span


def build_flying_wing(code, dims):
    # B-2: cranked-arrow planform, sawtooth trailing edge, no tails.
    span, length = dims[code]
    s = BOX / span
    cx = 12.0
    half = span * s / 2
    nose_y, tail_y = 12.0 - length * s / 2, 12.0 + length * s / 2
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
    return pts + mirror, span


BUILDERS = {"B2": build_flying_wing}


def build(code, dims):
    return BUILDERS.get(code, build_source)(code, dims)


def svg_for(code, pts, wingspan_m):
    # One decimal is a tenth of a pixel at the largest marker size.
    def num(v):
        t = f"{v:.1f}"
        return t[:-2] if t.endswith(".0") else t

    d = "M" + " L".join(f"{num(x)} {num(y)}" for x, y in pts) + " Z"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n'
        f'<!-- aerodrome-scale: {wingspan_m:.1f} -->\n'
        f'<path d="{d}"/>\n</svg>\n'
    )


def build_js(svg_dir="static/shapes", out_path="static/shapes-data.js"):
    """Collect the <path> of every generated SVG into the frontend data file
    (/static/shapes-data.js). Serves templates/index.html's radar and the
    display board via one JS constant (about 70 KB) — one request per page, no per-
    aircraft fetch. The SVG tree under --out stays the per-file source;
    this file is the deployment artifact the browser actually loads."""
    import json
    paths = {}
    # Only the table's own codes: any other .svg in the directory is not ours.
    for code in sorted(SOURCES):
        f = os.path.join(svg_dir, code + ".svg")
        if not os.path.exists(f):
            continue    # a partial run into a scratch directory
        with open(f) as fh:
            m = re.search(r"(<path[^>]*/>)", fh.read())
        if not m:
            raise RuntimeError(f"{f}: no <path> found — regenerate")
        paths[code] = m.group(1)
    body = (
        "// Generated by scripts/gen_type_shapes.py — do not edit by hand; edit SOURCES and re-run.\n"
        "// (Manual edits are lost on the next regeneration — everything below is rebuilt from\n"
        "// SOURCES + ALIASES in that script, which is the single source of truth.)\n"
        "// Original parametric silhouettes (see the script header). ICAO typecode -> <path> in a\n"
        "// 0 0 24 24 box, nose up; frontend applies fill/stroke/rotation.\n"
        "const SHAPES_DATA = {\n"
        + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in paths.items())
        # ALIASES: designators that broadcast a typecode whose plan-form is
        # a shipped shape's, under a code we'll never draw separately. Keyed
        # here (not as SOURCES rows) so the generator stays the single
        # source of truth. Sources for the map: aircraft typecode the trans-
        # ponder actually emits vs DOC 8643's canonical designation (some
        # fleets broadcast an OEM's marketing code — e.g. Gulfstream G150,
        # the Astra rebrand — while enthusiasts draw one shared airframe).
        # If a code keeps showing a chevron and matches an existing type,
        # add it here rather than duplicating geometry in SOURCES.
        # Alias targets that SOURCES didn't draw (renamed/removed upstream)
        # are skipped with a warning rather than emitting a null entry the
        # frontend would silently treat as "no shape".
        + "".join("" if v in paths else
                  f'// alias {k} -> {v} skipped (not in SOURCES)\n'
                  for k, v in ALIASES.items())
        + "".join(f",\n  {json.dumps(k)}: {json.dumps(paths[v])}"
                  for k, v in ALIASES.items() if v in paths)
        + "\n};\n"
    )
    with open(out_path, "w") as f:
        f.write(body)
    return len(paths)


def _one(code):
    pts, span = build(code, load_dims())
    return code, svg_for(code, pts, span), len(pts), span


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="static/shapes")
    ap.add_argument("--js", default=None,
                    help="data file to write (default: static/shapes-data.js, or"
                         " shapes-data.js inside --out when --out is given)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("codes", nargs="*", help="only these codes (default: every SOURCES row)")
    args = ap.parse_args()
    if args.js is None:
        # A preview run into another directory must not overwrite the shipped
        # data file with whatever that directory holds.
        args.js = ("static/shapes-data.js" if args.out == ap.get_default("out")
                   else os.path.join(args.out, "shapes-data.js"))
    os.makedirs(args.out, exist_ok=True)
    codes = sorted(args.codes or SOURCES)
    for code in codes:
        if code not in SOURCES:
            ap.error(f"{code}: no SOURCES row")
    if args.jobs > 1 and len(codes) > 1:
        import multiprocessing
        with multiprocessing.Pool(args.jobs) as pool:
            results = pool.map(_one, codes)
    else:
        results = [_one(c) for c in codes]
    for code, svg, _, _ in results:
        with open(os.path.join(args.out, f"{code}.svg"), "w") as f:
            f.write(svg)
    if not args.codes:
        # A file for a code that no longer has a row would otherwise keep
        # shipping as a shape. Only files this script wrote are removed: a
        # designator-shaped name AND our scale comment inside.
        for name in sorted(os.listdir(args.out)):
            if not re.fullmatch(r"[A-Z0-9]{2,4}\.svg", name) or name[:-4] in SOURCES:
                continue
            path = os.path.join(args.out, name)
            with open(path) as fh:
                ours = "<!-- aerodrome-scale:" in fh.read()
            if ours:
                os.remove(path)
                print(f"removed stale {name}")
    n_js = build_js(args.out, args.js)
    print(f"wrote {len(results)} shapes to {args.out} (+ {args.js}:"
          f" {n_js} shapes in that directory, plus the aliases that point at them)")
    if args.report:
        for code, _, n, span in results:
            print(f"  {code:6s} span={span:5.1f}m  vertices={n:3d}")


if __name__ == "__main__":
    main()
