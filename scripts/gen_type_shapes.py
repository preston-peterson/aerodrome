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
# Outline simplification tolerance, in view units: a tenth of a unit is about
# an eighth of a pixel at the largest marker size.
RDP_EPS = 0.10


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


def pusher(prop, at, d=0.6, ln=1.2):
    """One engine on the centreline driving a propeller that faces aft: a small
    pod ending at fraction `at` of the length, with the prop bar across its end.
    Canard pushers have it at the tail; amphibians carry it above the wing."""
    return [dict(kind="pusher", prop=prop, at=at, d=d, len=ln)]


def internal(n):
    """Engines buried in the fuselage (fighters, some trainers): nothing is drawn."""
    return [dict(kind="internal", count=n)]


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
#   elliptic         an elliptical wing (cr is its root chord; sweep, ct unused)
#   bulge            dict(at, to, w): a hull w metres wide between two fractions
#                    of the length, on top of a narrower fuselage
PRESETS = {}


def preset(name, parent=None, **over):
    base = dict(PRESETS[parent]) if parent else {}
    base["stab"] = dict(base["stab"]) if isinstance(base.get("stab"), dict) else base.get("stab", {})
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
preset("b707", "airliner", fus_w=3.76, sweep=35.0, cr=0.190, ct=0.050, wing_le=0.290,
       engines=jets(2.1, 4.2, 0.36, 0.63, ahead=0.72))
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


# -- military jets: pointed nose, short swept wing set aft, big stabilizers,
#    engines inside the fuselage
preset("fighter", fus_w=1.9, nose_len=4.2, nose_pow=0.85, tail_len=1.0, tail_w=0.55,
       wing_le=0.400, sweep=40.0, cr=0.420, ct=0.110, engines=internal(1),
       stab=dict(semi=0.290, sweep=40.0, cr=0.230, ct=0.080, pos="conv", back=0.0))
preset("fighter2", "fighter", fus_w=2.9, nose_len=2.9, engines=internal(2))
# -- tailless delta (Mirage, Concorde): no stabilizer
preset("delta", "fighter", wing_le=0.330, sweep=58.0, cr=0.640, ct=0.050,
       stab=dict(semi=0.0))
# -- straight-wing jet trainers and attack aircraft
preset("jet_trainer", "fighter", nose_len=3.2, wing_le=0.380, sweep=12.0, cr=0.220, ct=0.100,
       stab=dict(semi=0.220, sweep=14.0, cr=0.130, ct=0.070))
# -- high-wing jet transports with a T-tail (C-17, C-5, Il-76)
preset("jet_transport", "airliner", fus_w=6.9, nose_len=1.3, nose_pow=0.55, sweep=25.0,
       cr=0.170, ct=0.060, kink=None, wing_le=0.300,
       engines=jets(2.6, 5.2, 0.33, 0.60, ahead=0.75),
       stab=dict(semi=0.200, sweep=27.0, cr=0.130, ct=0.060, pos="T"))
# -- sailplanes and motor gliders: long thin wing, no visible engine
preset("glider", "single", fus_w=0.65, nose_len=2.2, nose_pow=0.60, wing_le=0.250, cr=0.060,
       ct=0.028, inner=None, sweep=0.0, engines=[],
       stab=dict(semi=0.085, sweep=4.0, cr=0.040, ct=0.028, pos="T"))
# -- biplanes: from above, one broad rectangular wing well forward
preset("biplane", "cub", fus_w=0.95, wing_le=0.180, cr=0.185, ct=0.185,
       stab=dict(semi=0.165, sweep=12.0, cr=0.120, ct=0.070))
# -- twin booms (Cessna 337, P-38, OV-10): a short pod, two booms from the
#    wing to a tailplane that spans between them
preset("booms", "twin", fus_w=1.25, nose_len=1.3, nose_pow=0.50, tail_len=0.30, tail_w=0.55,
       wing_le=0.300, sweep=0.0, cr=0.150, ct=0.100, inner=0.40,
       booms=dict(eta=0.28, w=0.40, pod=0.66),
       engines=nose_prop(1.95) + [dict(kind="internal", count=1)])
# -- helicopters: drawn by build_heli. Dimensions are (rotor diameter, length).
#    blades = main rotor blades; bw = body width, nose = nose ahead of the mast,
#    rear = cabin behind it, tail = tail end behind it, tailplane = its span
#    (all fractions of the rotor diameter).
preset("heli", builder="heli", blades=4, bw=0.19, nose=0.32, rear=0.20, tail=0.60, tailplane=0.20)
preset("heli_light", "heli", blades=2, bw=0.17, nose=0.26, rear=0.16, tail=0.62, tailplane=0.0)
preset("heli_heavy", "heli", blades=4, bw=0.17, nose=0.40, rear=0.24, tail=0.62, tailplane=0.24)
preset("heli_tandem", "heli", builder="heli", blades=3, bw=0.21, tandem=0.65)
# -- tiltrotor (V-22): drawn by build_tiltrotor with its rotors up, as it
#    looks from above in helicopter mode. Dimensions are (width across the
#    turning rotors, fuselage length); rotor = rotor diameter in metres.
preset("tiltrotor", builder="tilt", blades=3, rotor=11.6, fus_w=2.3, wing_le=0.330,
       chord=2.5, stab=dict(semi=2.8, cr=1.7))


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
    # --- military jets
    "F16": row("fighter"),
    "F18H": row("fighter2", sweep=28.0, cr=0.360, wing_le=0.430),
    "F15": row("fighter2", fus_w=3.3, sweep=45.0),
    "F22": row("fighter2", fus_w=3.4, sweep=42.0, cr=0.520, wing_le=0.330),
    "T38": row("fighter", engines=internal(2), fus_w=1.5, sweep=32.0, cr=0.360, wing_le=0.480),
    "HAWK": row("jet_trainer", sweep=26.0),
    "A10": row("jet_trainer", fus_w=1.8, nose_len=2.4, wing_le=0.330, sweep=2.0, cr=0.170, ct=0.100,
               engines=aft(1.5, 3.4, 0.56), stab=dict(semi=0.160, sweep=0.0, cr=0.110, ct=0.110)),
    "C17": row("jet_transport"),
    "IL76": row("jet_transport", fus_w=4.8, nose_len=1.5),
    "A124": row("jet_transport", fus_w=7.3, sweep=30.0, stab=dict(pos="conv")),
    "K35R": row("airliner", fus_w=3.66, sweep=35.0, cr=0.190, ct=0.050, wing_le=0.290,
                engines=jets(2.1, 4.2, 0.36, 0.63, ahead=0.72)),
    "B52": row("airliner", fus_w=3.0, nose_len=1.6, sweep=35.0, cr=0.150, ct=0.050, kink=None,
               wing_le=0.300,
               engines=[dict(kind="wing", eta=0.33, d=2.6, len=4.4, ahead=0.85, count=4),
                        dict(kind="wing", eta=0.62, d=2.6, len=4.4, ahead=0.85, count=4)],
               stab=dict(semi=0.140, sweep=32.0)),
    # --- canard, tip tanks, twin boom
    "P180": row("kingair", fus_w=1.95, nose_len=2.2, wing_le=0.560, cr=0.105, ct=0.045, inner=None,
                engines=props(0.85, 0.3, 1.9, 2.2, 0.29),
                canard=dict(at=0.090, semi=0.120, cr=0.060, ct=0.040)),
    "C310": row("twin", tip_tanks=dict(d=0.55, len=3.0)),
    "C337": row("booms"),
    # --- helicopters
    "R44": row("heli_light"),
    "B06": row("heli_light", nose=0.30),
    "AS50": row("heli", blades=3, tailplane=0.18),
    "EC35": row("heli", blades=4),
    "H60": row("heli_heavy"),
    "H47": row("heli_tandem"),
    # --- airliners and jets, A to E
    "A306": row("airliner", fus_w=5.64, sweep=28.0, cr=0.170, ct=0.040, kink=0.33, wing_le=0.335,
                engines=jets(2.7, 5.2, 0.34)),                       # A300-600
    "A30B": row("airliner", fus_w=5.64, sweep=28.0, cr=0.170, ct=0.040, kink=0.33, wing_le=0.340,
                engines=jets(2.5, 4.8, 0.34)),                       # A300 B2/B4
    "A310": row("airliner", fus_w=5.64, sweep=28.0, cr=0.170, ct=0.040, kink=0.33, wing_le=0.335,
                engines=jets(2.6, 5.0, 0.34)),                       # A310
    "A318": row("a320", wing_le=0.295, engines=jets(2.0, 4.2, 0.32)),  # A318
    "A337": row("a330", fus_w=5.64, bulge=dict(at=0.12, to=0.88, w=8.8), wing_le=0.305),  # Beluga XL
    "A338": row("a330", wing_le=0.300, engines=jets(3.3, 6.4, 0.31, ahead=0.66)),  # A330-800
    "A339": row("a330", wing_le=0.315, engines=jets(3.3, 6.4, 0.31, ahead=0.66)),  # A330-900
    "A342": row("a340", wing_le=0.300),                              # A340-200
    "A345": row("a340", wing_le=0.340, engines=jets(2.7, 5.4, 0.31, 0.61, ahead=0.70)),  # A340-500
    "A346": row("a340", wing_le=0.355, engines=jets(2.7, 5.4, 0.31, 0.61, ahead=0.70)),  # A340-600
    "AN72": row("jet_transport", fus_w=2.9, nose_len=1.5, sweep=12.0, cr=0.150, ct=0.070,
                wing_le=0.340, engines=jets(1.5, 3.8, 0.28, ahead=1.1),
                stab=dict(semi=0.200, sweep=25.0, cr=0.120, ct=0.060, pos="T")),  # An-72/74
    "B461": row("jet_transport", fus_w=3.56, nose_len=1.6, nose_pow=0.6, sweep=15.0, cr=0.150, ct=0.075,
                wing_le=0.300, engines=jets(1.15, 2.6, 0.30, 0.55, ahead=0.5),
                stab=dict(semi=0.200, sweep=25.0, cr=0.120, ct=0.065, pos="T")),  # BAe 146-100
    "B462": row("jet_transport", fus_w=3.56, nose_len=1.6, nose_pow=0.6, sweep=15.0, cr=0.150, ct=0.075,
                wing_le=0.310, engines=jets(1.15, 2.6, 0.30, 0.55, ahead=0.5),
                stab=dict(semi=0.200, sweep=25.0, cr=0.120, ct=0.065, pos="T")),  # BAe 146-200
    "B703": row("airliner", fus_w=3.76, sweep=35.0, cr=0.175, ct=0.045, kink=0.38, wing_le=0.380,
                nose_len=1.9, engines=jets(1.7, 4.2, 0.31, 0.57, ahead=0.70)),  # 707-320
    "B721": row("airliner", fus_w=3.76, sweep=32.0, cr=0.150, ct=0.045, kink=0.32, wing_le=0.360,
                tail_len=3.8, engines=aft(1.9, 5.0, 0.70) + centre(2.0, 4.5, 0.80),
                stab=dict(semi=0.185, sweep=35.0, cr=0.110, ct=0.045, pos="T")),  # 727-100
    "B722": row("airliner", fus_w=3.76, sweep=32.0, cr=0.150, ct=0.045, kink=0.32, wing_le=0.350,
                tail_len=3.8, engines=aft(1.9, 5.0, 0.73) + centre(2.0, 4.5, 0.82),
                stab=dict(semi=0.185, sweep=35.0, cr=0.110, ct=0.045, pos="T")),  # 727-200
    "B732": row("b737", wing_le=0.350, engines=jets(1.8, 5.0, 0.28, ahead=0.75)),  # 737-200
    "B733": row("b737", wing_le=0.340, engines=jets(1.9, 4.0, 0.29)),  # 737-300
    "B734": row("b737", wing_le=0.335, engines=jets(1.9, 4.0, 0.29)),  # 737-400
    "B735": row("b737", wing_le=0.350, engines=jets(1.9, 4.0, 0.29)),  # 737-500
    "B736": row("b737", wing_le=0.295),                             # 737-600
    "B741": row("b747"),                                            # 747-100
    "B742": row("b747"),                                            # 747-200
    "B743": row("b747"),                                            # 747-300
    "B762": row("b767", wing_le=0.330),                             # 767-200
    "B773": row("b777", wing_le=0.325, engines=jets(3.6, 7.3, 0.31, ahead=0.66)),  # 777-300
    "B778": row("b777", wing_le=0.335, sweep=34.0, engines=jets(4.1, 7.8, 0.29, ahead=0.66)),  # 777-8
    "B779": row("b777", wing_le=0.330, sweep=34.0, engines=jets(4.1, 7.8, 0.29, ahead=0.66)),  # 777-9
    "BA11": row("dc9", fus_w=3.2, sweep=20.0, wing_le=0.440, nose_len=1.8,
                engines=aft(1.6, 4.2, 0.70)),                       # BAC One-Eleven
    "BE40": row("phenom", fus_w=1.75, sweep=13.0, cr=0.170, ct=0.080, wing_le=0.420,
                engines=aft(1.1, 2.8, 0.62)),                       # Beechjet 400
    "BLCF": row("b747", fus_w=6.5, bulge=dict(at=0.17, to=0.80, w=8.4)),    # 747 Dreamlifter
    "C500": row("citation", fus_w=1.55, wing_le=0.400),             # Citation I
    "C501": row("citation", fus_w=1.55, wing_le=0.400),             # Citation I/SP
    "C510": row("cj", fus_w=1.40, sweep=2.0, wing_le=0.420,
                engines=aft(0.85, 2.2, 0.60)),                      # Citation Mustang
    "C551": row("citation", wing_le=0.400),                         # Citation II
    "C55B": row("citation", wing_le=0.400),                         # Citation Bravo
    "CL41": row("jet_trainer", fus_w=1.6, nose_len=2.2, nose_pow=0.55, wing_le=0.380, sweep=3.0,
                cr=0.190, ct=0.110, tail_len=2.5, tail_w=0.30,
                stab=dict(semi=0.210, sweep=8.0, cr=0.130, ct=0.080, pos="T")),  # CT-114 Tutor
    "CRJ1": row("crj", wing_le=0.420),                              # CRJ100
    "DC10": row("trijet", wing_le=0.370),                           # DC-10
    "DC87": row("airliner", fus_w=3.76, sweep=30.0, cr=0.150, ct=0.045, kink=0.35, wing_le=0.370,
                engines=jets(1.7, 4.2, 0.33, 0.61, ahead=0.72)),    # DC-8-70
    "DC91": row("dc9", wing_le=0.400, engines=aft(1.9, 5.0, 0.69)),  # DC-9-10
    "DC95": row("dc9", wing_le=0.395),                              # DC-9-50
    "E195": row("ejet", wing_le=0.345, engines=jets(1.9, 3.9, 0.30)),  # E195
    # --- airliners and jets, E to W
    "E290": row("ejet", wing_le=0.340, engines=jets(2.3, 4.2, 0.30)),   # Embraer E190-E2
    "E35L": row("erj", fus_w=2.28, wing_le=0.420, engines=aft(1.35, 3.3, 0.68)),   # Embraer Legacy 600
    "E45X": row("erj"),   # ERJ-145XR
    "E50P": row("phenom", fus_w=1.65, wing_le=0.410),   # Phenom 100
    "E75S": row("ejet", wing_le=0.320),   # E175
    "F18S": row("fighter2", fus_w=2.6, sweep=22.0, cr=0.340, wing_le=0.420),   # Super Hornet
    "FA20": row("falcon", fus_w=1.95, sweep=30.0, wing_le=0.410, engines=aft(1.1, 2.9, 0.63)),   # Falcon 20
    "FA50": row("falcon3", fus_w=2.30),   # Falcon 50 trijet
    "GALX": row("legacy", fus_w=2.2, sweep=28.0, engines=aft(1.2, 3.1, 0.64)),   # Gulfstream G200
    "GLF2": row("gulfstream", sweep=27.0, wing_le=0.400),   # Gulfstream II
    "GLF3": row("gulfstream", sweep=27.0, wing_le=0.400),   # Gulfstream III
    "H25A": row("bizjet", fus_w=1.85, sweep=20.0, cr=0.190, ct=0.080, wing_le=0.410,
                engines=aft(1.0, 2.9, 0.63)),   # HS 125-400/600
    "H25B": row("bizjet", fus_w=1.85, sweep=20.0, cr=0.190, ct=0.070, wing_le=0.410,
                engines=aft(1.1, 3.0, 0.63)),   # Hawker 800
    "H25C": row("bizjet", fus_w=1.85, sweep=20.0, cr=0.190, ct=0.070, wing_le=0.405,
                engines=aft(1.2, 3.2, 0.64)),   # Hawker 1000
    "HA4T": row("challenger", fus_w=2.45, sweep=28.0, wing_le=0.400, engines=aft(1.35, 3.4, 0.65)),   # Hawker 4000
    "J328": row("turboprop", fus_w=2.2, wing_le=0.370, sweep=3.0, cr=0.110, ct=0.060,
                engines=jets(1.3, 3.2, 0.34, ahead=0.9)),   # Dornier 328JET, high wing T-tail
    "L29B": row("bizjet", fus_w=2.2, sweep=30.0, cr=0.200, ct=0.060, wing_le=0.370,
                engines=aft(1.1, 2.6, 0.60) + aft(1.1, 2.6, 0.66)),   # JetStar, two pods per side
    "LJ23": row("learjet", fus_w=1.55, sweep=13.0, tip_tanks=dict(d=0.55, len=2.9)),   # Learjet 23
    "LJ24": row("learjet", fus_w=1.55, sweep=13.0, tip_tanks=dict(d=0.55, len=2.9)),   # Learjet 24
    "LJ25": row("learjet", fus_w=1.55, sweep=13.0, tip_tanks=dict(d=0.55, len=2.9)),   # Learjet 25
    "LJ31": row("learjet", sweep=15.0),   # Learjet 31
    "LJ35": row("learjet", fus_w=1.65, tip_tanks=dict(d=0.55, len=2.9)),   # Learjet 35/36
    "LJ40": row("learjet"),   # Learjet 40
    "LJ55": row("learjet", sweep=15.0),   # Learjet 55
    "LJ60": row("learjet", sweep=15.0),   # Learjet 60
    "LJ70": row("learjet", sweep=15.0),   # Learjet 70
    "LJ75": row("learjet", sweep=15.0),   # Learjet 75
    "MD81": row("dc9", engines=aft(1.9, 6.0, 0.715)),   # MD-81
    "MD82": row("dc9", engines=aft(1.9, 6.0, 0.715)),   # MD-82
    "MD87": row("dc9", engines=aft(1.9, 6.0, 0.715)),   # MD-87
    "MD88": row("dc9", engines=aft(1.9, 6.0, 0.715)),   # MD-88
    "MD90": row("dc9", engines=aft(2.0, 6.0, 0.715)),   # MD-90
    "MU30": row("phenom", fus_w=1.6, sweep=18.0, wing_le=0.400, engines=aft(1.0, 2.6, 0.62)),   # Diamond / MU-300
    "P8": row("b737", sweep=28.0, wing_le=0.330, engines=jets(2.35, 4.6, 0.32, ahead=0.68)),   # P-8 Poseidon
    "PRM1": row("citation_swept", fus_w=1.65, sweep=15.0, cr=0.170, wing_le=0.400,
                engines=aft(1.0, 2.6, 0.61)),   # Premier 1
    "R721": row("dc9", fus_w=3.76, nose_len=1.7, sweep=32.0, cr=0.150, ct=0.045, wing_le=0.385,
                engines=aft(2.1, 4.5, 0.70) + centre(2.2, 5.0, 0.78)),   # 727-100 re-engined
    "R722": row("dc9", fus_w=3.76, nose_len=1.7, sweep=32.0, cr=0.150, ct=0.045, wing_le=0.395,
                engines=aft(2.1, 4.5, 0.72) + centre(2.2, 5.0, 0.80)),   # 727-200 re-engined
    "RJ1H": row("jet_transport", fus_w=3.56, nose_len=1.6, sweep=15.0, cr=0.120, ct=0.065,
                wing_le=0.300, engines=jets(1.3, 2.8, 0.26, 0.52, ahead=0.6),
                stab=dict(semi=0.20, sweep=25.0, cr=0.110, ct=0.060)),   # Avro RJ100
    "RJ85": row("jet_transport", fus_w=3.56, nose_len=1.6, sweep=15.0, cr=0.120, ct=0.065,
                wing_le=0.310, engines=jets(1.3, 2.8, 0.26, 0.52, ahead=0.6),
                stab=dict(semi=0.20, sweep=25.0, cr=0.110, ct=0.060)),   # Avro RJ85
    "SBR1": row("bizjet", fus_w=1.9, sweep=28.0, cr=0.170, ct=0.100, wing_le=0.390,
                engines=aft(1.0, 2.8, 0.60), stab=dict(sweep=15.0, pos="cruci")),   # Sabreliner 40/60
    "SBR2": row("bizjet", fus_w=1.9, sweep=28.0, cr=0.170, ct=0.100, wing_le=0.390,
                engines=aft(1.0, 2.8, 0.60), stab=dict(sweep=15.0, pos="cruci")),   # Sabreliner 75
    "SU95": row("ejet", fus_w=3.46, wing_le=0.340, sweep=25.0, engines=jets(2.0, 3.8, 0.30)),   # Superjet 100
    "WW24": row("bizjet", fus_w=1.95, sweep=14.0, cr=0.190, ct=0.080, wing_le=0.405,
                engines=aft(1.0, 2.9, 0.62)),   # Westwind 1124
    # --- turboprops
    "AC80": row("kingair", fus_w=1.45, wing_le=0.320, engines=props(0.95, 2.0, 0.0, 2.5, 0.31), stab=dict(pos="conv", back=0.03, sweep=8.0)),  # Turbo Commander 680
    "AC90": row("kingair", fus_w=1.45, wing_le=0.320, engines=props(0.95, 2.0, 0.0, 2.5, 0.31), stab=dict(pos="conv", back=0.03, sweep=8.0)),  # Commander 690
    "AC95": row("kingair", fus_w=1.45, wing_le=0.320, engines=props(0.95, 2.0, 0.0, 2.5, 0.31), stab=dict(pos="conv", back=0.03, sweep=8.0)),  # Commander 1000
    "AT3T": row("turbine_single", fus_w=1.35, nose_len=1.6, nose_pow=0.55, wing_le=0.330, cr=0.135, ct=0.115, stab=dict(semi=0.20, sweep=4.0, cr=0.11, ct=0.07)),  # Air Tractor AT-402
    "AT43": row("atr", fus_w=2.57, wing_le=0.380),  # ATR 42-300
    "AT44": row("atr", fus_w=2.57, wing_le=0.380),  # ATR 42-400
    "AT46": row("atr", fus_w=2.57, wing_le=0.380),  # ATR 42-600
    "AT5T": row("turbine_single", fus_w=1.35, nose_len=1.6, nose_pow=0.55, wing_le=0.330, cr=0.135, ct=0.115, stab=dict(semi=0.20, sweep=4.0, cr=0.11, ct=0.07)),  # Air Tractor AT-503
    "AT6T": row("turbine_single", fus_w=1.40, nose_len=1.6, nose_pow=0.55, wing_le=0.330, cr=0.135, ct=0.115, stab=dict(semi=0.20, sweep=4.0, cr=0.11, ct=0.07)),  # Air Tractor AT-602
    "AT72": row("atr"),  # ATR 72-200
    "AT73": row("atr"),  # ATR 72-210
    "AT76": row("atr"),  # ATR 72-600
    "AT8T": row("turbine_single", fus_w=1.50, nose_len=1.6, nose_pow=0.55, wing_le=0.330, cr=0.135, ct=0.115, stab=dict(semi=0.20, sweep=4.0, cr=0.11, ct=0.07)),  # Air Tractor AT-802
    "B18T": row("twin", fus_w=1.30, nose_len=1.6, nose_pow=0.55, wing_le=0.300, cr=0.140, ct=0.075, engines=props(0.85, 1.6, 0.2, 2.1, 0.28), stab=dict(semi=0.20, sweep=0.0, cr=0.10, ct=0.08)),  # Beech 18 turbine
    "B36T": row("bonanza", fus_w=1.25, nose_len=1.9, nose_pow=0.65, engines=nose_prop(2.1)),  # Turbine Bonanza
    "BE10": row("kingair", fus_w=1.40, wing_le=0.330, stab=dict(pos="conv", back=0.03)),  # King Air 100
    "BE30": row("kingair", fus_w=1.65, wing_le=0.320),  # Super King Air 300
    "BE99": row("kingair", fus_w=1.45, wing_le=0.340, cr=0.115, ct=0.065, engines=props(0.90, 2.0, 0.0, 2.3, 0.31), stab=dict(pos="conv", back=0.03, sweep=12.0)),  # Beech 99
    "BE9L": row("kingair", fus_w=1.40, wing_le=0.340, stab=dict(pos="conv", back=0.03)),  # King Air 90
    "BE9T": row("kingair", fus_w=1.40, wing_le=0.340),  # King Air F90
    "C160": row("turboprop", fus_w=3.4, nose_len=1.4, nose_pow=0.55, wing_le=0.340, cr=0.115, ct=0.060, inner=0.30, engines=props(1.4, 3.0, 1.4, 5.0, 0.30), stab=dict(semi=0.19, pos="conv", back=0.02, sweep=10.0, cr=0.11)),  # Transall C-160
    "C212": row("turboprop", fus_w=2.0, nose_len=1.6, wing_le=0.300, cr=0.110, ct=0.080, inner=0.31, sweep=0.0, engines=props(0.95, 1.8, 0.3, 2.7, 0.30), stab=dict(semi=0.17, pos="conv", back=0.02, sweep=6.0)),  # CASA C-212 Aviocar
    "C425": row("kingair", fus_w=1.35, wing_le=0.300, cr=0.150, ct=0.075, engines=props(0.85, 2.0, 0.0, 2.2, 0.30), stab=dict(pos="conv", back=0.03, sweep=10.0)),  # Cessna 425 Corsair
    "CN35": row("turboprop", fus_w=2.6, nose_len=1.5, wing_le=0.340, cr=0.110, ct=0.070, inner=0.31, sweep=0.0, engines=props(1.1, 2.4, 0.5, 3.2, 0.29), stab=dict(semi=0.17, pos="conv", back=0.02, sweep=8.0)),  # CASA CN-235
    "CVLT": row("turboprop", fus_w=3.0, nose_len=1.7, wing_le=0.330, cr=0.115, ct=0.060, engines=props(1.2, 2.8, 0.8, 4.1, 0.29), stab=dict(semi=0.19, pos="conv", back=0.02, sweep=10.0)),  # Convair 580
    "D328": row("turboprop", fus_w=2.4, nose_len=2.0, wing_le=0.360, cr=0.090, ct=0.055, engines=props(1.0, 2.2, 0.8, 3.2, 0.31)),  # Dornier 328 (T-tail)
    "DC3T": row("turboprop", fus_w=2.6, nose_len=1.5, nose_pow=0.55, wing_le=0.290, cr=0.125, ct=0.060, inner=0.28, engines=props(1.2, 2.8, 0.8, 3.7, 0.29), stab=dict(semi=0.20, pos="conv", back=0.02, sweep=8.0, cr=0.10)),  # Basler BT-67 / DC-3
    "DH8A": row("turboprop", fus_w=2.5, wing_le=0.360, cr=0.100, engines=props(1.1, 2.4, 1.0, 3.8, 0.31)),  # Dash 8-100
    "DH8B": row("turboprop", fus_w=2.5, wing_le=0.360, cr=0.100, engines=props(1.1, 2.4, 1.0, 3.8, 0.31)),  # Dash 8-200
    "DH8C": row("turboprop", fus_w=2.6, wing_le=0.370),  # Dash 8-300
    "DHC6": row("twin", fus_w=1.65, nose_len=1.0, nose_pow=0.50, tail_len=0.35, wing_le=0.230, sweep=0.0, cr=0.115, ct=0.115, engines=props(0.9, 1.6, 0.3, 2.7, 0.30), stab=dict(semi=0.20, pos="cruci", sweep=0.0, cr=0.095, ct=0.095)),  # Twin Otter
    "DHC7": row("turboprop", fus_w=2.6, wing_le=0.350, cr=0.095, ct=0.060, engines=props(1.0, 2.2, 1.0, 3.4, 0.20, 0.40), stab=dict(semi=0.19)),  # Dash 7 (4 engines)
    "E110": row("kingair", fus_w=1.75, wing_le=0.320, cr=0.130, ct=0.065, nose_len=2.0, engines=props(0.9, 2.0, 0.0, 2.5, 0.31), stab=dict(pos="conv", back=0.03, sweep=10.0)),  # EMB-110 Bandeirante
    "E120": row("kingair", fus_w=2.28, wing_le=0.340, nose_len=2.2, engines=props(1.0, 2.3, 0.5, 2.9, 0.31), stab=dict(pos="T", sweep=14.0, semi=0.17)),  # EMB-120 Brasilia
    "EVOT": row("turbine_single", fus_w=1.10, wing_le=0.350, cr=0.115, ct=0.060, nose_len=2.0, engines=nose_prop(1.9)),  # Lancair Evolution
    "F406": row("twin", fus_w=1.55, wing_le=0.300, nose_len=1.8, cr=0.140, ct=0.090, engines=props(0.85, 1.6, 0.3, 2.2, 0.30), stab=dict(semi=0.20, sweep=6.0, cr=0.100, ct=0.070)),  # Cessna F406 Caravan II
    "JS31": row("kingair", fus_w=1.85, wing_le=0.360, engines=props(0.95, 2.0, 0.2, 2.7, 0.34), stab=dict(pos="cruci", sweep=18.0)),  # Jetstream 31
    "JS32": row("kingair", fus_w=1.85, wing_le=0.360, engines=props(0.95, 2.0, 0.2, 2.7, 0.34), stab=dict(pos="cruci", sweep=18.0)),  # Jetstream 32
    "LNP4": row("turbine_single", fus_w=1.05, wing_le=0.350, cr=0.115, ct=0.060, nose_len=2.0, engines=nose_prop(1.9)),  # Lancair Propjet
    "MU2": row("kingair", fus_w=1.45, wing_le=0.280, cr=0.130, ct=0.100, inner=None, sweep=0.0, nose_len=2.8, engines=props(0.85, 1.8, 0.0, 2.3, 0.28), tip_tanks=dict(d=0.55, len=3.0), stab=dict(pos="conv", back=0.03, sweep=8.0)),  # Mitsubishi MU-2 (tip tanks)
    "P3": row("turboprop", fus_w=3.4, nose_len=1.6, nose_pow=0.55, wing_le=0.350, cr=0.115, ct=0.060, engines=props(1.3, 3.0, 1.0, 4.1, 0.21, 0.42), stab=dict(semi=0.19, pos="conv", back=0.02, sweep=10.0)),  # P-3 Orion
    "P46T": row("turbine_single", fus_w=1.30, nose_len=2.0, wing_le=0.300, cr=0.115, ct=0.060, engines=nose_prop(2.0)),  # Meridian
    "P750": row("turbine_single", fus_w=1.45, nose_len=1.2, nose_pow=0.50, wing_le=0.260, cr=0.130, ct=0.100, inner=None, sweep=0.0, engines=nose_prop(2.4)),  # P-750 XSTOL
    "PAT4": row("kingair", fus_w=1.35, wing_le=0.310, cr=0.150, ct=0.075, engines=props(0.85, 2.0, 0.0, 2.2, 0.30), stab=dict(pos="conv", back=0.03, sweep=14.0)),  # PA-31T3 T-1040
    "PAY1": row("kingair", fus_w=1.35, wing_le=0.310, cr=0.150, ct=0.075, engines=props(0.85, 2.0, 0.0, 2.2, 0.30), stab=dict(pos="conv", back=0.03, sweep=14.0)),  # Cheyenne I
    "PAY2": row("kingair", fus_w=1.35, wing_le=0.310, cr=0.150, ct=0.075, engines=props(0.85, 2.0, 0.0, 2.2, 0.30), stab=dict(pos="conv", back=0.03, sweep=14.0)),  # Cheyenne II
    "PAY3": row("kingair", fus_w=1.40, wing_le=0.320, cr=0.140, ct=0.065, engines=props(0.85, 2.1, 0.0, 2.3, 0.30), stab=dict(pos="T", sweep=14.0)),  # Cheyenne III
    "PAY4": row("kingair", fus_w=1.40, wing_le=0.320, cr=0.140, ct=0.065, engines=props(0.85, 2.1, 0.0, 2.3, 0.30), stab=dict(pos="T", sweep=14.0)),  # Cheyenne 400
    "SB20": row("turboprop", fus_w=2.3, nose_len=2.0, wing_le=0.370, cr=0.095, ct=0.050, inner=0.31, engines=props(1.0, 2.4, 1.0, 3.5, 0.30), stab=dict(pos="conv", back=0.02, sweep=10.0, semi=0.17)),  # Saab 2000 (FAA row lists 1 engine)
    "SC7": row("turboprop", fus_w=2.2, nose_len=0.9, nose_pow=0.50, wing_le=0.250, cr=0.100, ct=0.100, inner=None, sweep=0.0, engines=props(1.0, 1.4, 0.4, 2.7, 0.30), stab=dict(semi=0.16, pos="conv", sweep=0.0, cr=0.10, ct=0.10)),  # Short Skyvan
    "SF34": row("turboprop", fus_w=2.31, nose_len=2.0, wing_le=0.370, cr=0.095, ct=0.050, engines=props(1.0, 2.2, 0.8, 3.3, 0.31), stab=dict(pos="conv", back=0.02, sweep=10.0, semi=0.18)),  # Saab 340
    "SH33": row("turboprop", fus_w=2.0, nose_len=1.6, wing_le=0.330, cr=0.100, ct=0.070, inner=None, sweep=0.0, engines=props(0.95, 2.0, 0.3, 2.8, 0.29), stab=dict(semi=0.17, pos="conv", back=0.02, sweep=0.0, cr=0.10, ct=0.08)),  # Shorts 330
    "SW3": row("kingair", fus_w=1.55, wing_le=0.340, nose_len=2.8, cr=0.120, ct=0.060, engines=props(0.9, 2.0, 0.3, 2.4, 0.31), stab=dict(pos="cruci", sweep=14.0)),  # Merlin III (tip tanks)
    "SW4": row("kingair", fus_w=1.70, wing_le=0.350, nose_len=2.8, cr=0.115, ct=0.060, engines=props(0.9, 2.0, 0.3, 2.4, 0.31), stab=dict(pos="cruci", sweep=14.0)),  # Metro / Merlin IV (tip tanks)
    "TBM7": row("turbine_single", fus_w=1.30, wing_le=0.380),  # TBM 700
    "TBM8": row("turbine_single", fus_w=1.30, wing_le=0.380),  # TBM 850
    "TEX2": row("turbine_single", fus_w=1.25, nose_len=2.0, wing_le=0.330, cr=0.150, ct=0.075, engines=nose_prop(2.3)),  # T-6 Texan II
    "A5": row("cherokee", fus_w=1.15, wing_le=0.300, cr=0.140, ct=0.100, engines=pusher(1.8, 0.56, d=0.7, ln=1.5), stab=dict(semi=0.17, pos="conv", back=0.04)),  # ICON A5 (pusher engine above the wing)
    "LA4": row("single", fus_w=1.30, wing_le=0.260, cr=0.150, ct=0.105, engines=pusher(1.8, 0.52, d=0.7, ln=1.5), stab=dict(semi=0.17, pos="conv", back=0.05)),  # Lake LA-4 (pusher engine above the wing)
    "C526": row("cj", fus_w=1.50, sweep=2.0, cr=0.135, ct=0.070, wing_le=0.400),  # CitationJet 526 trainer
    "KODI": row("turbine_single", fus_w=1.55, nose_len=1.8, wing_le=0.300, cr=0.125, ct=0.095, inner=0.40),  # Kodiak
    # --- piston aircraft, A to C
    "AA1": row("cherokee", fus_w=1.0, cr=0.150, ct=0.130, inner=None),   # Grumman American AA-1
    "AA5": row("cherokee", fus_w=1.1),   # AA-5 Traveler/Cheetah
    "AC11": row("cherokee", fus_w=1.2, wing_le=0.270, nose_len=1.4, cr=0.160, ct=0.110, inner=None),   # Commander 112
    "AC56": row("twin", fus_w=1.45, wing_le=0.300, cr=0.150, ct=0.065,
                engines=props(0.90, 1.5, 0.3, 2.1, 0.27)),   # Aero Commander 560
    "AC68": row("twin", fus_w=1.45, wing_le=0.300, cr=0.150, ct=0.065,
                engines=props(0.90, 1.5, 0.3, 2.1, 0.27)),   # Aero Commander 680
    "AC6L": row("twin", fus_w=1.50, wing_le=0.280, cr=0.130, ct=0.060,
                engines=props(0.90, 1.5, 0.3, 2.1, 0.27)),   # Aero Commander 680FL
    "AR11": row("cub", fus_w=1.0),   # Aeronca 11 Chief
    "BE18": row("twin", fus_w=1.45, wing_le=0.280, cr=0.150, ct=0.075, nose_len=1.6, nose_pow=0.55,
                engines=props(0.90, 1.9, 0.2, 2.6, 0.28),
                stab=dict(semi=0.200, cr=0.115, ct=0.070)),   # Beech 18 (twin fins, plain tailplane)
    "BE19": row("cherokee", fus_w=1.2, cr=0.155, ct=0.120),   # Musketeer
    "BE23": row("cherokee", fus_w=1.2, cr=0.155, ct=0.120),   # Sundowner
    "BE24": row("cherokee", fus_w=1.2, cr=0.155, ct=0.120),   # Sierra
    "BE33": row("bonanza"),   # Debonair/Bonanza 33
    "BE50": row("twin", fus_w=1.40, wing_le=0.300, cr=0.150, ct=0.070,
                engines=props(0.90, 1.4, 0.2, 2.3, 0.28)),   # Twin Bonanza
    "BE55": row("twin"),   # Baron 55
    "BE60": row("twin", fus_w=1.35, wing_le=0.300, cr=0.160, ct=0.070,
                engines=props(0.85, 1.5, 0.0, 2.1, 0.30)),   # Duke
    "BE65": row("twin", fus_w=1.50, wing_le=0.290, cr=0.150, ct=0.065,
                engines=props(0.90, 1.5, 0.2, 2.2, 0.28)),   # Queen Air 65
    "BE70": row("twin", fus_w=1.50, wing_le=0.290, cr=0.150, ct=0.065,
                engines=props(0.90, 1.5, 0.2, 2.2, 0.28)),   # Queen Air 70
    "BE76": row("twin", fus_w=1.20, wing_le=0.300, cr=0.150, ct=0.110, inner=0.40,
                stab=dict(sweep=0.0, cr=0.095, ct=0.095, pos="T")),   # Duchess (T-tail)
    "BE77": row("cherokee", fus_w=1.1, cr=0.150, ct=0.150, stab=dict(pos="T")),   # Skipper (T-tail)
    "BE80": row("twin", fus_w=1.50, wing_le=0.290, cr=0.150, ct=0.065,
                engines=props(0.90, 1.5, 0.2, 2.2, 0.28)),   # Queen Air 80
    "BE95": row("twin", fus_w=1.25, wing_le=0.300),   # Travel Air
    "BL17": row("cherokee", fus_w=1.25, cr=0.170, ct=0.090, inner=None, nose_len=1.5, nose_pow=0.55),   # Viking
    "C120": row("single", fus_w=0.95, inner=None, cr=0.140, ct=0.120, wing_le=0.215),   # Cessna 120
    "C140": row("single", fus_w=0.95, inner=None, cr=0.140, ct=0.120, wing_le=0.215),   # Cessna 140
    "C150": row("single"),   # Cessna 150
    "C162": row("single", fus_w=1.05),   # Skycatcher
    "C170": row("single"),   # Cessna 170
    "C177": row("single", inner=None, cr=0.150, ct=0.110),   # Cardinal
    "C180": row("single"),   # Skywagon 180
    "C185": row("single"),   # Skywagon 185
    "C188": row("single", cr=0.150, ct=0.125, inner=None),   # AgWagon
    "C195": row("single", fus_w=1.30, nose_len=1.0, nose_pow=0.5),   # Cessna 195
    "C206": row("single", inner=0.30, ct=0.090),   # Stationair
    "C207": row("single", inner=0.30, ct=0.090, fus_w=1.2),   # Stationair 7
    "C240": row("cirrus", fus_w=1.15),   # TTx
    "C303": row("twin", fus_w=1.30, wing_le=0.290, cr=0.150, ct=0.100, inner=0.40,
                engines=props(0.80, 1.4, 0.0, 2.0, 0.29),
                stab=dict(sweep=8.0, cr=0.095, ct=0.060, pos="T")),   # Crusader (T-tail)
    "C320": row("twin", fus_w=1.30, wing_le=0.300, tip_tanks=dict(d=0.55, len=3.0)),   # Skyknight
    "C335": row("twin", fus_w=1.35, wing_le=0.300, tip_tanks=dict(d=0.55, len=3.0)),   # Cessna 335
    "C402": row("twin", fus_w=1.40, wing_le=0.300, cr=0.140, ct=0.090,
                engines=props(0.85, 1.4, 0.1, 2.0, 0.29), tip_tanks=dict(d=0.55, len=3.0)),   # 401/402
    "C404": row("twin", fus_w=1.45, wing_le=0.300, cr=0.140, ct=0.090,
                engines=props(0.85, 1.4, 0.1, 2.1, 0.29)),   # Titan
    "C421": row("twin", fus_w=1.45, wing_le=0.300, cr=0.140, ct=0.090, tip_tanks=dict(d=0.55, len=3.0),
                engines=props(0.85, 1.4, 0.1, 2.0, 0.29)),   # Golden Eagle
    "C77R": row("single", inner=None, cr=0.150, ct=0.110),   # Cardinal RG
    "C82R": row("single"),   # Skylane RG
    "CH7A": row("cub", fus_w=0.90),   # Aeronca 7AC Champ
    "CH7B": row("cub", fus_w=0.90),   # Citabria
    "COL3": row("cirrus", fus_w=1.15),   # Columbia 300
    "COL4": row("cirrus", fus_w=1.15),   # Columbia 400
    "COUR": row("single", fus_w=1.20, inner=None, cr=0.150, ct=0.120, wing_le=0.215),   # Helio Courier
    # --- piston aircraft, C to T
    "CRUZ": row("cherokee", fus_w=1.05, cr=0.150, ct=0.130),  # CZAW SportCruiser
    "CVLP": row("twin", fus_w=2.9, nose_len=1.6, nose_pow=0.6, wing_le=0.340, cr=0.150, ct=0.065, inner=0.35,
                engines=props(1.4, 3.0, 1.5, 4.0, 0.28), tail_frac=False, tail_len=3.2, tail_w=0.14,
                stab=dict(semi=0.185, sweep=8.0, cr=0.100, ct=0.050)),  # Convair 440
    "DA40": row("cirrus", fus_w=1.15, cr=0.095, ct=0.060, wing_le=0.280, sweep=0.0, nose_len=1.4,
                stab=dict(semi=0.150, sweep=5.0, cr=0.075, ct=0.050, pos="T")),  # Diamond DA40
    "DA42": row("twin", fus_w=1.15, nose_len=1.6, nose_pow=0.6, wing_le=0.300, cr=0.100, ct=0.060, sweep=0.0,
                engines=props(0.65, 1.3, -0.1, 1.9, 0.26),
                stab=dict(semi=0.150, sweep=5.0, cr=0.075, ct=0.050, pos="T")),  # Diamond DA42
    "DC3": row("twin", fus_w=2.4, nose_len=1.4, nose_pow=0.55, wing_le=0.330, cr=0.190, ct=0.075, inner=0.40,
               engines=props(1.4, 3.0, 1.4, 3.7, 0.21), tail_frac=False, tail_len=5.0, tail_w=0.12,
               stab=dict(semi=0.190, sweep=8.0, cr=0.110, ct=0.060)),  # DC-3 / C-47
    "DC3S": row("twin", fus_w=2.4, nose_len=1.4, nose_pow=0.55, wing_le=0.330, cr=0.190, ct=0.075, inner=0.40,
                engines=props(1.4, 3.0, 1.4, 3.7, 0.21), tail_frac=False, tail_len=5.0, tail_w=0.12,
                stab=dict(semi=0.190, sweep=8.0, cr=0.110, ct=0.060)),  # Super DC-3
    "DC6": row("twin", fus_w=3.4, nose_len=1.4, nose_pow=0.55, wing_le=0.340, cr=0.150, ct=0.055, inner=0.40,
               engines=props(1.5, 3.0, 1.8, 4.1, 0.19, 0.38), tail_frac=False, tail_len=6.0, tail_w=0.12,
               stab=dict(semi=0.180, sweep=8.0, cr=0.100, ct=0.050)),  # DC-6
    "DHC2": row("cub", fus_w=1.25, wing_le=0.200, cr=0.170, ct=0.170, engines=nose_prop(2.6)),  # Beaver
    "DV20": row("cirrus", fus_w=0.95, cr=0.090, ct=0.060, wing_le=0.300, sweep=0.0, nose_len=1.5,
                engines=nose_prop(1.7),
                stab=dict(semi=0.150, sweep=5.0, cr=0.075, ct=0.050, pos="T")),  # Diamond Katana
    "ERCO": row("cherokee", fus_w=1.05, cr=0.150, ct=0.115, wing_le=0.270,
                stab=dict(semi=0.140, sweep=0.0, cr=0.080, ct=0.070)),  # Ercoupe
    "FDCT": row("cherokee", fus_w=1.05, cr=0.140, ct=0.120),  # Flight Design CT
    "G164": row("biplane", fus_w=1.0, engines=nose_prop(2.5)),  # Ag-Cat
    "GA7": row("twin", fus_w=1.2, nose_len=1.8, wing_le=0.300, cr=0.160, ct=0.100, inner=0.40,
               engines=props(0.75, 1.2, -0.1, 1.8, 0.30),
               stab=dict(semi=0.180, sweep=0.0, cr=0.100, ct=0.060, pos="T")),  # Cougar
    "GC1": row("cherokee", fus_w=1.0, cr=0.140, ct=0.100, wing_le=0.290, nose_len=1.6),  # Globe Swift
    "HUSK": row("cub", fus_w=1.0, engines=nose_prop(2.0)),  # Aviat Husky
    "L5": row("cub", fus_w=1.0, cr=0.160, ct=0.125),  # Stinson L-5
    "L8": row("cherokee", fus_w=1.0, cr=0.160, ct=0.130, wing_le=0.250),  # Luscombe 8
    "LNC4": row("bonanza", fus_w=1.1, cr=0.100, ct=0.065, wing_le=0.340, sweep=2.0, engines=nose_prop(2.0)),  # Lancair IV
    "M20T": row("mooney"),  # Mooney M20 turbo
    "NAVI": row("bonanza", fus_w=1.2, cr=0.170, ct=0.100, wing_le=0.280, nose_len=1.5),  # Navion
    "P210": row("single", fus_w=1.2, wing_le=0.235),  # Centurion
    "P28B": row("cherokee"),  # Turbo Dakota
    "P28R": row("cherokee"),  # Arrow
    "P28T": row("cherokee", stab=dict(semi=0.160, sweep=0.0, cr=0.090, ct=0.090, pos="T")),  # Arrow IV T-tail
    "P32T": row("cherokee", fus_w=1.25, stab=dict(semi=0.160, sweep=0.0, cr=0.090, ct=0.090, pos="T")),  # Lance
    "P51": row("cherokee", fus_w=1.0, nose_len=2.8, nose_pow=0.7, wing_le=0.340, cr=0.190, ct=0.075, inner=None,
               engines=nose_prop(3.4), tail_len=1.6, tail_frac=False,
               stab=dict(semi=0.160, sweep=10.0, cr=0.110, ct=0.060)),  # P-51 Mustang
    "PA11": row("cub", wing_le=0.225),  # Cub Special
    "PA12": row("cub", wing_le=0.225),  # Super Cruiser
    "PA16": row("cub", cr=0.165, ct=0.165, wing_le=0.250),  # Clipper
    "PA20": row("cub", cr=0.165, ct=0.165, wing_le=0.250),  # Pacer
    "PA22": row("cub", cr=0.165, ct=0.165, wing_le=0.250),  # Tri-Pacer
    "PA23": row("twin", fus_w=1.2, nose_len=1.6, cr=0.170, ct=0.110, inner=0.40,
                stab=dict(semi=0.180, sweep=0.0, cr=0.100, ct=0.065)),  # Apache
    "PA24": row("cherokee", fus_w=1.15, cr=0.160, ct=0.120, nose_len=1.5),  # Comanche
    "PA25": row("cub", fus_w=0.95, wing_le=0.240, cr=0.150, ct=0.140),  # Pawnee
    "PA27": row("twin", fus_w=1.3, cr=0.165, ct=0.110, inner=0.40,
                stab=dict(semi=0.180, sweep=0.0, cr=0.100, ct=0.065)),  # Aztec
    "PA30": row("twin", fus_w=1.15, cr=0.150, ct=0.105, inner=0.40,
                stab=dict(semi=0.180, sweep=0.0, cr=0.100, ct=0.065)),  # Twin Comanche
    "PA31": row("twin", fus_w=1.4, cr=0.150, ct=0.100, inner=0.40,
                stab=dict(semi=0.180, sweep=0.0, cr=0.100, ct=0.065)),  # Navajo
    "PA32": row("cherokee", fus_w=1.25, nose_len=1.4),  # Cherokee Six
    "PA36": row("cherokee", fus_w=1.0, wing_le=0.280, cr=0.160, ct=0.140, nose_len=1.6),  # Pawnee Brave
    "PA38": row("cherokee", fus_w=1.0, cr=0.140, ct=0.130, wing_le=0.270,
                stab=dict(semi=0.160, sweep=0.0, cr=0.090, ct=0.090, pos="T")),  # Tomahawk
    "PA44": row("twin", fus_w=1.2, nose_len=1.6, cr=0.140, ct=0.100, inner=0.40, sweep=0.0,
                stab=dict(semi=0.170, sweep=0.0, cr=0.090, ct=0.080, pos="T")),  # Seminole
    "S108": row("single", fus_w=1.0, wing_le=0.250, cr=0.160, ct=0.125, inner=None),  # Stinson 108
    "T28": row("bonanza", fus_w=1.2, nose_len=2.2, nose_pow=0.65, wing_le=0.300, cr=0.190, ct=0.075,
               engines=nose_prop(3.2)),  # T-28 Trojan
    "T34P": row("bonanza", fus_w=1.1, cr=0.150, ct=0.070, wing_le=0.300),  # T-34C Mentor
    "T6": row("bonanza", fus_w=1.1, nose_len=2.0, nose_pow=0.6, wing_le=0.280, cr=0.175, ct=0.075,
              engines=nose_prop(2.9)),  # T-6 Texan
    "TAYB": row("cub", fus_w=0.95, wing_le=0.240, cr=0.150, ct=0.130),  # Taylorcraft BC
    "TB20": row("cirrus", fus_w=1.1, cr=0.110, ct=0.080, wing_le=0.290),  # Trinidad
    "TOBA": row("cirrus", fus_w=1.1, cr=0.110, ct=0.080, wing_le=0.290),  # Tobago
    # --- military and government
    "F35": row("fighter", fus_w=2.2, sweep=34.0, cr=0.380, wing_le=0.420),            # F-35A
    "F4": row("fighter2", fus_w=2.5, sweep=45.0, cr=0.380, wing_le=0.400),            # F-4 Phantom II
    "F5": row("fighter2", fus_w=1.6, nose_len=3.6, sweep=32.0, cr=0.330, ct=0.100, wing_le=0.420),   # F-5E
    "F14": row("fighter2", fus_w=3.2, sweep=60.0, cr=0.480, ct=0.120, wing_le=0.330),  # F-14, wings swept
    "F104": row("fighter", fus_w=1.6, sweep=10.0, cr=0.280, ct=0.140, wing_le=0.480,
                stab=dict(semi=0.220, sweep=20.0, cr=0.180, ct=0.080, pos="T")),      # F-104 Starfighter
    "F117": row("delta", fus_w=2.2, nose_len=3.0, nose_pow=0.55, sweep=67.0, cr=0.760, ct=0.060,
                wing_le=0.250, engines=internal(2)),                                  # F-117 Nighthawk
    "A4": row("jet_trainer", fus_w=1.9, sweep=33.0, cr=0.300, ct=0.090, wing_le=0.380,
              stab=dict(semi=0.230, sweep=30.0, cr=0.150, ct=0.070)),                 # A-4 Skyhawk
    "HAR": row("fighter", fus_w=2.2, sweep=30.0, cr=0.320, ct=0.080, wing_le=0.380,
               stab=dict(semi=0.240, sweep=30.0, cr=0.150, ct=0.070)),                # AV-8B Harrier II
    "B1": row("fighter", fus_w=3.0, nose_len=4.0, sweep=60.0, cr=0.240, ct=0.100, wing_le=0.330,
              engines=internal(4), stab=dict(semi=0.380, sweep=50.0, cr=0.280, ct=0.100)),   # B-1B swept
    "U2": row("glider", fus_w=1.5, nose_len=3.0, wing_le=0.350, cr=0.070, ct=0.045,
              engines=internal(1), stab=dict(semi=0.140, sweep=8.0, cr=0.060, ct=0.040, pos="conv")),   # U-2S
    "E3TF": row("b707"),                                                  # E-3A/B/C Sentry
    "E3CF": row("b707"),                                                  # E-3D/F Sentry
    "E6": row("b707"),                                                    # E-6B Mercury
    "C5M": row("jet_transport", fus_w=7.0, sweep=25.0, cr=0.150, ct=0.055, wing_le=0.300,
               engines=jets(2.6, 5.8, 0.30, 0.55, ahead=0.75)),                       # C-5M Super Galaxy
    "E2": row("turboprop", fus_w=2.4, nose_len=2.2, wing_le=0.300, cr=0.110, ct=0.070,
              sweep=0.0, inner=0.22, engines=props(1.2, 2.6, 0.8, 4.1, 0.26),
              stab=dict(semi=0.150, sweep=0.0, cr=0.100, ct=0.080, pos="conv")),      # E-2C/D Hawkeye
    "T33": row("jet_trainer", fus_w=1.5, nose_len=3.4, sweep=8.0, cr=0.200, ct=0.090, wing_le=0.400,
               stab=dict(semi=0.220, sweep=10.0, cr=0.120, ct=0.070, pos="conv")),       # T-33
    "L39": row("jet_trainer", fus_w=1.4, nose_len=3.2, sweep=5.0, cr=0.185, ct=0.100, wing_le=0.400,
               stab=dict(semi=0.200, sweep=10.0, cr=0.110, ct=0.060)),                # L-39 Albatros
    "PC7": row("turbine_single", fus_w=1.15, nose_len=2.4, wing_le=0.340, cr=0.150, ct=0.080,
               inner=0.30, engines=nose_prop(2.6), stab=dict(semi=0.180, sweep=6.0)), # PC-7
    "PC9": row("turbine_single", fus_w=1.15, nose_len=2.4, wing_le=0.340, cr=0.150, ct=0.080,
               inner=0.30, engines=nose_prop(2.4), stab=dict(semi=0.180, sweep=6.0)), # PC-9
    "PC21": row("turbine_single", fus_w=1.15, nose_len=2.6, wing_le=0.350, cr=0.155, ct=0.085,
                inner=0.30, engines=nose_prop(2.3), stab=dict(semi=0.180, sweep=6.0)),  # PC-21
    "T37": row("jet_trainer", fus_w=1.5, nose_len=2.0, nose_pow=0.55, sweep=1.0, cr=0.190, ct=0.110,
               wing_le=0.340, engines=jets(0.8, 2.0, 0.07, ahead=0.35),
               stab=dict(semi=0.200, sweep=3.0, cr=0.120, ct=0.080)),                 # T-37 Tweet
    "F86": row("fighter", fus_w=1.6, nose_len=3.6, sweep=35.0, cr=0.260, ct=0.110, wing_le=0.420,
               stab=dict(semi=0.220, sweep=35.0, cr=0.140, ct=0.070)),                # F-86 Sabre
    "MG29": row("fighter2", fus_w=3.0, nose_len=2.6, sweep=42.0, cr=0.360, wing_le=0.400),   # MiG-29
    "SU27": row("fighter2", fus_w=3.4, nose_len=2.6, sweep=42.0, cr=0.380, wing_le=0.380),   # Su-27 family
    "EUFI": row("delta", fus_w=2.4, sweep=53.0, cr=0.520, ct=0.060, wing_le=0.350,
                engines=internal(2),
                stab=dict(semi=0.0),
                canard=dict(at=0.200, semi=0.190, sweep=45.0, cr=0.150, ct=0.050)),   # Typhoon: canards, no tailplane
    "RFAL": row("delta", fus_w=2.4, sweep=52.0, cr=0.520, ct=0.060, wing_le=0.340,
                engines=internal(2)),                                                 # Rafale
    "TOR": row("fighter2", fus_w=2.4, nose_len=3.4, sweep=65.0, cr=0.520, ct=0.100, wing_le=0.380,
               stab=dict(semi=0.300, sweep=50.0, cr=0.240, ct=0.080)),                # Tornado, wings swept
    "A6": row("jet_trainer", fus_w=2.8, nose_len=2.4, nose_pow=0.6, sweep=25.0, cr=0.240, ct=0.100,
              wing_le=0.380, engines=internal(2),
              stab=dict(semi=0.200, sweep=28.0, cr=0.150, ct=0.070)),                 # A-6 Intruder
    "S3": row("airliner", fus_w=2.6, nose_len=2.2, nose_pow=0.6, sweep=15.0, cr=0.150, ct=0.080,
              wing_le=0.340, kink=None, engines=jets(1.5, 3.9, 0.22, ahead=0.60),
              stab=dict(semi=0.200, sweep=20.0, cr=0.110, ct=0.060)),                 # S-3 Viking
    "C27J": row("turboprop", fus_w=3.0, nose_len=1.2, nose_pow=0.55, wing_le=0.340, cr=0.130,
                ct=0.080, inner=0.31, engines=props(1.3, 3.1, 0.4, 4.0, 0.31),
                stab=dict(semi=0.190, sweep=8.0, cr=0.110, ct=0.060, pos="conv")),       # C-27J
    "C295": row("turboprop", fus_w=2.9, nose_len=1.4, nose_pow=0.55, wing_le=0.350, cr=0.125,
                ct=0.075, inner=0.34, engines=props(1.2, 2.8, 0.6, 3.7, 0.34),
                stab=dict(semi=0.190, sweep=8.0, cr=0.110, ct=0.060, pos="conv")),       # C-295
    "AN22": row("hercules", fus_w=4.4, nose_len=1.3, wing_le=0.300, cr=0.110, ct=0.060,
                engines=props(1.4, 3.6, 0.0, 5.6, 0.22, 0.46),
                stab=dict(semi=0.165, sweep=8.0, cr=0.085, ct=0.045, pos="T")),       # An-22 (twin fins ignored)
    "V10": row("booms", fus_w=1.3, nose_len=1.5, wing_le=0.280, cr=0.140, ct=0.100,
               engines=props(0.9, 1.4, 0.4, 2.7, 0.28)),                              # OV-10 Bronco
    "C123": row("hercules", fus_w=3.2, nose_len=1.3, wing_le=0.330, cr=0.130, ct=0.080, sweep=0.0,
                engines=props(1.2, 2.6, 0.3, 3.9, 0.30),
                stab=dict(semi=0.200, sweep=6.0, cr=0.110, ct=0.060, pos="conv")),       # C-123 Provider
    "C119": row("booms", fus_w=3.2, nose_len=1.2, wing_le=0.300, cr=0.130, ct=0.090,
                booms=dict(eta=0.30, w=0.90, pod=0.58),
                engines=props(1.2, 2.2, 0.3, 3.9, 0.30)),                             # C-119 Flying Boxcar
    # --- more airliners, utility and sport aircraft
    "B3XM": row("b737max", wing_le=0.360),  # 737 MAX 10
    "B74S": row("b747", wing_le=0.300),  # 747SP
    "B74R": row("b747"),  # 747SR
    "A3ST": row("a330", fus_w=5.64, bulge=dict(at=0.10, to=0.86, w=7.3), wing_le=0.300, engines=jets(2.7, 5.3, 0.33, ahead=0.66)),  # Beluga
    "CRJX": row("crj", wing_le=0.450),  # CRJ-1000
    "A148": row("jet_transport", fus_w=3.4, nose_len=1.9, nose_pow=0.62, sweep=25.0, cr=0.15, ct=0.05,
                wing_le=0.36, engines=jets(1.7, 3.5, 0.29, ahead=0.7), stab=dict(semi=0.19)),  # An-148
    "A158": row("jet_transport", fus_w=3.4, nose_len=1.9, nose_pow=0.62, sweep=25.0, cr=0.15, ct=0.05,
                wing_le=0.37, engines=jets(1.7, 3.5, 0.29, ahead=0.7), stab=dict(semi=0.19)),  # An-158
    "AN24": row("turboprop", fus_w=2.6, wing_le=0.37, sweep=1.0, cr=0.115, ct=0.06,
                engines=props(1.4, 3.0, 3.2, 3.9, 0.22), stab=dict(pos="conv", semi=0.17)),  # An-24 / Y-7
    "AN30": row("turboprop", fus_w=2.6, wing_le=0.40, sweep=1.0, cr=0.115, ct=0.06, nose_len=1.4,
                engines=props(1.4, 3.0, 3.2, 3.9, 0.22), stab=dict(pos="conv", semi=0.17)),  # An-30 glazed nose
    "AN32": row("turboprop", fus_w=2.6, wing_le=0.37, sweep=1.0, cr=0.115, ct=0.06,
                engines=props(1.8, 3.0, 3.2, 4.7, 0.22), stab=dict(pos="conv", semi=0.17)),  # An-32
    "AN28": row("turboprop", fus_w=1.9, wing_le=0.32, sweep=0.0, cr=0.115, ct=0.09, nose_len=1.3,
                engines=props(0.9, 1.8, 1.0, 2.9, 0.30), stab=dict(pos="conv", semi=0.19)),  # M-28 Bryza
    "IL62": row("rearjet", fus_w=4.1, sweep=35.0, wing_le=0.38, cr=0.14, ct=0.04,
                engines=[dict(kind="aft", d=2.6, len=6.0, at=0.62, count=4)], stab=dict(sweep=38.0)),  # Il-62, paired nacelles drawn as one wide pod
    "IL96": row("a340", fus_w=6.0, wing_le=0.33, engines=jets(2.8, 5.6, 0.31, 0.59, ahead=0.70)),  # Il-96
    "T154": row("rearjet", fus_w=3.8, sweep=35.0, wing_le=0.37, cr=0.16,
                engines=aft(1.5, 4.2, 0.67) + centre(1.6, 5.0, 0.76)),  # Tu-154
    "T204": row("a320", fus_w=3.8, wing_le=0.35, sweep=28.0, engines=jets(2.4, 5.0, 0.30)),  # Tu-204
    "YK42": row("rearjet", fus_w=3.8, sweep=24.0, wing_le=0.40,
                engines=aft(1.4, 3.6, 0.68) + centre(1.5, 4.5, 0.76)),  # Yak-42
    "F27": row("turboprop", fus_w=2.7, wing_le=0.38, sweep=0.0, cr=0.11, ct=0.06,
               engines=props(1.3, 2.4, 2.0, 3.5, 0.28), stab=dict(pos="conv", semi=0.17)),  # Fokker F27
    "F50": row("turboprop", fus_w=2.7, wing_le=0.39, sweep=0.0, cr=0.11, ct=0.06,
               engines=props(1.3, 2.4, 2.0, 3.66, 0.28), stab=dict(pos="conv", semi=0.17)),  # Fokker 50
    "F70": row("crj", fus_w=3.3, sweep=17.5, wing_le=0.40, engines=aft(1.6, 4.0, 0.70)),  # Fokker 70
    "F100": row("crj", fus_w=3.3, sweep=17.5, wing_le=0.42, engines=aft(1.6, 4.0, 0.72)),  # Fokker 100
    "F28": row("crj", fus_w=3.3, sweep=17.5, wing_le=0.40, engines=aft(1.6, 4.0, 0.70)),  # Fokker F28
    "DC86": row("airliner", fus_w=3.75, sweep=30.0, wing_le=0.37, kink=0.33,
                engines=jets(2.0, 5.0, 0.34, 0.60, ahead=0.72)),  # DC-8-60
    "DC94": row("dc9", wing_le=0.430),  # DC-9-40
    "B701": row("airliner", fus_w=3.76, sweep=35.0, wing_le=0.37, cr=0.15,
                engines=jets(1.5, 4.8, 0.34, 0.60, ahead=0.72)),  # 707-100
    "B720": row("airliner", fus_w=3.76, sweep=35.0, wing_le=0.38, cr=0.15,
                engines=jets(1.5, 4.8, 0.34, 0.60, ahead=0.72)),  # 720
    "L188": row("turboprop", fus_w=3.3, nose_len=1.6, wing_le=0.37, sweep=0.0, cr=0.12, ct=0.06,
                engines=props(1.2, 3.0, 0.5, 4.1, 0.22, 0.43), stab=dict(pos="conv", semi=0.19)),  # Electra
    "D228": row("turboprop", fus_w=1.7, wing_le=0.35, sweep=0.0, cr=0.11, ct=0.09, inner=None,
                engines=props(0.9, 1.5, 0.8, 2.7, 0.33), stab=dict(pos="conv", semi=0.19)),  # Dornier 228
    "L410": row("turboprop", fus_w=1.85, wing_le=0.30, sweep=0.0, cr=0.12, ct=0.09, inner=None,
                engines=props(0.9, 1.8, 0.8, 2.6, 0.30), stab=dict(pos="conv", semi=0.19)),  # L-410
    "E121": row("kingair", fus_w=1.6, wing_le=0.34, nose_len=2.8),  # Xingu
    "BN2P": row("twin", fus_w=1.3, nose_len=1.4, nose_pow=0.5, wing_le=0.32, sweep=0.0, cr=0.13, ct=0.13,
                inner=None, engines=props(0.7, 1.3, 0.5, 1.9, 0.28)),  # Islander
    "TRIS": row("twin", fus_w=1.3, nose_len=1.4, nose_pow=0.5, wing_le=0.32, sweep=0.0, cr=0.13, ct=0.13,
                inner=None, engines=props(0.7, 1.3, 0.5, 1.9, 0.28) + centre(0.8, 1.8, 0.80)),  # Trislander
    "DHC3": row("cub", fus_w=1.6, wing_le=0.27, cr=0.14, ct=0.14, engines=nose_prop(2.7)),  # Otter
    "DH2T": row("cub", fus_w=1.3, wing_le=0.24, cr=0.14, ct=0.14, engines=nose_prop(2.7)),  # Turbo Beaver
    "DHC4": row("turboprop", fus_w=3.0, wing_le=0.33, sweep=0.0, cr=0.115, ct=0.06,
                engines=props(1.2, 2.4, 1.0, 4.1, 0.27), stab=dict(pos="cruci")),  # Caribou
    "DHC5": row("turboprop", fus_w=3.0, wing_le=0.33, sweep=0.0, cr=0.115, ct=0.06,
                engines=props(1.3, 2.4, 2.0, 4.1, 0.27)),  # Buffalo
    "AN2": row("biplane", fus_w=1.6, wing_le=0.28, cr=0.14, ct=0.14, engines=nose_prop(3.3)),  # An-2
    "PC6T": row("cub", fus_w=1.3, wing_le=0.28, cr=0.13, ct=0.13, engines=nose_prop(2.4)),  # PC-6
    "C46": row("twin", fus_w=2.5, nose_len=1.2, nose_pow=0.5, wing_le=0.30,
               engines=props(1.3, 2.8, 1.0, 3.9, 0.30)),  # Commando
    "RV4": row("cirrus", fus_w=0.95, nose_len=1.3, wing_le=0.30, cr=0.165, ct=0.145, engines=nose_prop(1.8)),  # RV-4
    "RV6": row("cirrus", fus_w=1.0, nose_len=1.3, wing_le=0.30, cr=0.165, ct=0.145, engines=nose_prop(1.8)),  # RV-6
    "RV7": row("cirrus", fus_w=1.0, nose_len=1.3, wing_le=0.30, cr=0.160, ct=0.140, engines=nose_prop(1.8)),  # RV-7
    "RV8": row("cirrus", fus_w=0.95, nose_len=1.3, wing_le=0.30, cr=0.165, ct=0.145, engines=nose_prop(1.8)),  # RV-8
    "RV9": row("cirrus", fus_w=1.0, nose_len=1.3, wing_le=0.30, cr=0.150, ct=0.130, engines=nose_prop(1.8)),  # RV-9
    "RV10": row("cirrus", fus_w=1.2, nose_len=1.3, wing_le=0.30, cr=0.145, ct=0.125, engines=nose_prop(1.9)),  # RV-10
    "RV14": row("cirrus", fus_w=1.0, nose_len=1.3, wing_le=0.30, cr=0.150, ct=0.130, engines=nose_prop(1.8)),  # RV-14
    "M7": row("cub", fus_w=1.1, wing_le=0.22, engines=nose_prop(2.0)),  # Maule MX-7
    "C82S": row("single"),  # T182
    "DA62": row("twin", fus_w=1.3, wing_le=0.33, sweep=1.0, cr=0.095, ct=0.06,
                engines=props(0.7, 1.4, 0.5, 1.9, 0.27), stab=dict(semi=0.17, pos="T")),  # DA62
    "DA50": row("cirrus", stab=dict(pos="T", semi=0.17), engines=nose_prop(2.0)),  # DA50
    "P28U": row("cherokee", stab=dict(pos="T", semi=0.17)),  # Arrow IV
    "M600": row("turbine_single", fus_w=1.3, wing_le=0.34, engines=nose_prop(2.2)),  # M600
    "P208": row("single", fus_w=1.1, wing_le=0.24, engines=nose_prop(1.7)),  # P2008
    "P06T": row("twin", fus_w=1.1, wing_le=0.27, sweep=0.0, cr=0.12, ct=0.09,
                engines=props(0.65, 1.2, 0.4, 1.8, 0.28)),  # P2006T
    "ECHO": row("single", fus_w=1.05, wing_le=0.24, engines=nose_prop(1.7)),  # P92
    "PTS1": row("biplane", fus_w=0.8, wing_le=0.22, cr=0.19, ct=0.19, engines=nose_prop(1.9)),  # Pitts S-1
    "PTS2": row("biplane", fus_w=0.85, wing_le=0.22, cr=0.19, ct=0.19, engines=nose_prop(2.0)),  # Pitts S-2
    "ST75": row("biplane", fus_w=0.95, wing_le=0.20, cr=0.17, ct=0.17, engines=nose_prop(2.5)),  # Stearman
    "YK52": row("cherokee", fus_w=1.1, cr=0.15, ct=0.12, wing_le=0.28, engines=nose_prop(2.3)),  # Yak-52
    "Z42": row("cirrus", fus_w=1.1, cr=0.15, ct=0.12, wing_le=0.28, engines=nose_prop(2.0)),  # Zlin 242
    "SPIT": row("cherokee", fus_w=0.9, nose_len=3.2, nose_pow=0.7, wing_le=0.30, cr=0.225, ct=0.06, elliptic=True,
                engines=nose_prop(3.0)),  # Spitfire
    "B17": row("hercules", fus_w=2.5, nose_len=1.6, wing_le=0.36, cr=0.12, ct=0.07,
               engines=props(1.3, 3.5, 0.5, 3.5, 0.23, 0.43)),  # B-17
    "B25": row("twin", fus_w=1.9, nose_len=1.6, wing_le=0.34, cr=0.16, ct=0.10,
               engines=props(1.3, 3.0, 1.0, 3.7, 0.23)),  # B-25
    "B29": row("hercules", fus_w=2.9, nose_len=1.6, wing_le=0.36, cr=0.12, ct=0.06,
               engines=props(1.5, 3.5, 0.5, 5.0, 0.20, 0.40)),  # B-29
    "LGEZ": row("single", fus_w=0.7, nose_len=1.6, wing_le=0.55, sweep=25.0, cr=0.12, ct=0.05, inner=None,
                engines=pusher(1.6, 0.97), canard=dict(at=0.12, semi=0.30, cr=0.07, ct=0.05),
                stab=dict(semi=0.0)),  # Long-EZ
    # --- helicopters
    "R22":  row("heli_light"),                                  # Robinson R22
    "R66":  row("heli_light", nose=0.28),                       # Robinson R66
    "B06T": row("heli_light", nose=0.30),                       # Bell 206LT TwinRanger
    "B407": row("heli", blades=4),                              # Bell 407
    "B412": row("heli", blades=4),                              # Bell 412
    "B429": row("heli", blades=4),                              # Bell 429
    "B430": row("heli", blades=4),                              # Bell 430
    "B212": row("heli", blades=2),                              # Bell 212
    "UH1":  row("heli", blades=2),                              # Bell 205 / UH-1H
    "B47G": row("heli_light", nose=0.24, bw=0.14),              # Bell 47G
    "B47J": row("heli_light", nose=0.24, bw=0.15),              # Bell 47J Ranger
    "B505": row("heli_light", nose=0.29),                       # Bell 505
    "B222": row("heli", blades=2),                              # Bell 222
    "B230": row("heli", blades=2),                              # Bell 230
    "AS65": row("heli", blades=4),                              # Dauphin / HH-65
    "AS32": row("heli_heavy", blades=4),                        # Super Puma
    "EC20": row("heli", blades=3, tailplane=0.16),              # EC120
    "EC45": row("heli", blades=4),                              # EC145 / UH-72
    "EC55": row("heli", blades=5),                              # EC155
    "EC25": row("heli_heavy", blades=5),                        # H225
    "EC75": row("heli_heavy", blades=5),                        # H175
    "A109": row("heli", blades=4),                              # AW109
    "A119": row("heli", blades=4),                              # AW119 Koala
    "A139": row("heli_heavy", blades=5),                        # AW139
    "A169": row("heli", blades=5),                              # AW169
    "A189": row("heli_heavy", blades=5),                        # AW189
    "S76":  row("heli", blades=4),                              # Sikorsky S-76
    "S92":  row("heli_heavy", blades=4),                        # Sikorsky S-92
    "S61":  row("heli_heavy", blades=5),                        # Sikorsky S-61
    "H64":  row("heli", blades=4, bw=0.13, nose=0.30),          # AH-64 Apache
    "H53":  row("heli_heavy", blades=6),                        # CH-53A/D
    "H500": row("heli_light", blades=5, bw=0.18, tailplane=0.16),  # MD 500
    "MD52": row("heli_light", blades=5, bw=0.18),               # MD 520N
    "MD60": row("heli_light", blades=6, bw=0.18),               # MD 600N
    "EXPL": row("heli", blades=5),                              # MD Explorer
    "EN28": row("heli_light", blades=3),                        # Enstrom 280
    "EN48": row("heli_light", blades=3),                        # Enstrom 480
    "H269": row("heli_light", blades=3, bw=0.15, nose=0.22),    # Hughes 269 / Schweizer 300
    "G2CA": row("heli_light", blades=3, bw=0.16, nose=0.22),    # Guimbal Cabri G2
    "BK17": row("heli", blades=4),                              # BK117
    "B105": row("heli", blades=4, bw=0.18),                     # BO-105
    "HUCO": row("heli_light", bw=0.12, tailplane=0.16),         # AH-1 HueyCobra
    "MI8":  row("heli_heavy", blades=5),                        # Mi-8
    "H46":  row("heli_tandem", blades=3),                       # CH-46
    "EH10": row("heli_heavy", blades=5),                        # AW101
    "NH90": row("heli_heavy", blades=4),                        # NH90
    "LYNX": row("heli", blades=4),                              # Lynx / Wildcat
    "H160": row("heli", blades=5),                              # H160
    "GLID": row("glider"),   # ICAO's generic designator for a sailplane
    # --- seen on a real receiver, added after the first coverage pass
    "GA8C": row("gulfstream", fus_w=2.59, sweep=33.0),          # Gulfstream G800
    "FA6X": row("falcon", fus_w=2.7, sweep=32.0, engines=aft(1.4, 3.6, 0.665)),   # Falcon 6X (a twin)
    "V22": row("tiltrotor"),                                    # V-22 Osprey
    "ULAC": row("cub", fus_w=0.8),      # ICAO's generic designator for an ultralight
    "GYRO": row("heli_light"),          # ICAO's generic designator for a gyrocopter
    "ACRO": row("biplane", fus_w=0.75, wing_le=0.22, cr=0.19, ct=0.19, engines=nose_prop(1.7)),   # Acro Sport I
    "AG10": row("cirrus", fus_w=1.1, wing_le=0.28, nose_len=1.4),   # AVIC/CAIGA AG100 (Cirrus SR10 derived trainer)
    "AJET": row("jet_trainer", fus_w=1.5, nose_len=3.2, wing_le=0.380, sweep=28.0, cr=0.200, ct=0.080,
                engines=internal(2)),   # Alpha Jet
    "ALIG": row("cirrus", fus_w=1.0, wing_le=0.30, cr=0.130, ct=0.080, nose_len=1.5, engines=nose_prop(1.7)),   # Arion Lightning
    "ANGL": row("twin", fus_w=1.4, nose_len=1.6, wing_le=0.30, cr=0.13, ct=0.10, inner=0.40,
                engines=props(0.80, 0.0, 1.1, 1.9, 0.30)),   # Angel 44: nacelles on the wing, pusher props behind it
    "AP22": row("cub", fus_w=1.0, cr=0.135, ct=0.135, wing_le=0.25, engines=nose_prop(1.7)),   # Aeroprakt A-22 Foxbat
    "AP32": row("cub", fus_w=1.0, cr=0.135, ct=0.135, wing_le=0.25, engines=nose_prop(1.7)),   # Aeroprakt A-32 Vixxen
    "AS31": row("glider", fus_w=0.70, nose_len=2.0, wing_le=0.28, cr=0.046, ct=0.018, engines=internal(1),
                stab=dict(semi=0.075, cr=0.032, ct=0.022)),   # ASH 31 Mi, 21 m span
    "ASTO": row("cherokee", fus_w=1.1, wing_le=0.28, cr=0.150, ct=0.115, engines=nose_prop(1.8)),   # Tecnam Astore
    "AT3P": row("cherokee", fus_w=1.1, nose_len=1.8, wing_le=0.30, cr=0.135, ct=0.120, inner=None,
                engines=nose_prop(2.5),
                stab=dict(semi=0.150, sweep=0.0, cr=0.120, ct=0.090)),   # Air Tractor AT-300
    "AVID": row("cub", fus_w=0.9, cr=0.135, ct=0.135, wing_le=0.26, engines=nose_prop(1.8)),   # Avid Flyer / Airdale
    "B14A": row("cherokee", fus_w=1.1, wing_le=0.27, cr=0.150, ct=0.100, inner=None),   # Bellanca 14-13 Cruisair
    "B26": row("twin", fus_w=1.7, nose_len=2.0, wing_le=0.360, sweep=2.0, cr=0.125, ct=0.060,
               engines=props(1.2, 3.0, 1.0, 3.7, 0.25),
               stab=dict(semi=0.170, cr=0.100, ct=0.055)),   # A-26 Invader
    "BDOG": row("cherokee", fus_w=1.15, wing_le=0.28, cr=0.150, ct=0.110, inner=None,
                engines=nose_prop(1.9)),   # Bulldog
    "BE4W": row("citation_swept", fus_w=1.75, sweep=20.0, cr=0.170, ct=0.060, wing_le=0.410,
                engines=aft(1.05, 3.0, 0.64)),   # Beechjet 400XT
    "BIPL": row("biplane", fus_w=0.65, wing_le=0.22, cr=0.20, ct=0.20, engines=nose_prop(1.6)),   # EAA Biplane
    "BOLT": row("biplane", fus_w=0.80, wing_le=0.20, cr=0.19, ct=0.19, engines=nose_prop(1.9)),   # Steen Skybolt
    "BPAT": row("cub", fus_w=1.0, cr=0.135, ct=0.135, wing_le=0.24, engines=nose_prop(2.0)),   # Bearhawk Patrol
    "BR23": row("cherokee", fus_w=1.15, wing_le=0.27, cr=0.140, ct=0.115, inner=None,
                engines=nose_prop(1.9)),   # Bristell B23
    "BUSH": row("cub", fus_w=0.9, cr=0.135, ct=0.135, wing_le=0.25, engines=nose_prop(1.8)),   # SkyReach BushCat
    "C190": row("single", fus_w=1.25, wing_le=0.24, cr=0.150, ct=0.100, engines=nose_prop(2.1)),   # Cessna 190
    "CB1": row("biplane", fus_w=0.80, wing_le=0.22, cr=0.19, ct=0.19, engines=nose_prop(1.7)),   # Hatz CB-1
    "CC11": row("cub", fus_w=0.90, cr=0.135, ct=0.135, wing_le=0.25, engines=nose_prop(2.0)),   # Carbon Cub
    "CC19": row("cub", fus_w=0.95, cr=0.130, ct=0.130, wing_le=0.25, engines=nose_prop(2.0)),   # XCub / NXCub
    "CH60": row("cherokee", fus_w=1.1, wing_le=0.30, cr=0.167, ct=0.167, inner=None,
                engines=nose_prop(1.7)),   # Zodiac CH 601
    "CH75": row("cub", fus_w=1.0, cr=0.150, ct=0.150, wing_le=0.25, engines=nose_prop(1.8)),   # CH 750 STOL
    "CH80": row("cub", fus_w=1.1, cr=0.150, ct=0.150, wing_le=0.25, engines=nose_prop(2.0)),   # CH 801 STOL
    "CJ6": row("bonanza", fus_w=1.0, nose_len=2.0, nose_pow=0.6, wing_le=0.300, cr=0.170, ct=0.075,
               engines=nose_prop(2.7)),   # CJ-6 / BT-6
    "CL2T": row("turboprop", fus_w=2.3, nose_len=1.4, nose_pow=0.55, wing_le=0.37, sweep=0.0,
                cr=0.130, ct=0.085, inner=0.5, engines=props(1.0, 2.2, 1.0, 3.8, 0.28),
                stab=dict(semi=0.17, cr=0.11, ct=0.07, pos="T")),   # CL-415 amphibian, straight high wing
    "CMAS": row("single", fus_w=1.0, wing_le=0.25, cr=0.150, ct=0.110, engines=nose_prop(1.9)),   # Cessna C-34 Airmaster
    "COY2": row("cub", fus_w=0.85, cr=0.130, ct=0.130, wing_le=0.25, engines=nose_prop(1.8)),   # Rans S-6 Coyote II
    "COZY": row("single", fus_w=0.85, nose_len=1.6, wing_le=0.52, sweep=22.0, cr=0.13, ct=0.06, inner=None,
                engines=pusher(1.6, 0.97), canard=dict(at=0.10, semi=0.24, cr=0.07, ct=0.05),
                stab=dict(semi=0.0)),   # Cozy / AeroCanard
    "CRER": row("cub", fus_w=1.0, cr=0.150, ct=0.150, wing_le=0.25, engines=nose_prop(1.9)),   # Rans S-7 Courier
    "D11": row("cherokee", fus_w=0.95, wing_le=0.30, cr=0.165, ct=0.120, inner=0.40, engines=nose_prop(1.7)),   # Jodel D.112
    "DA2": row("cub", fus_w=0.80, cr=0.220, ct=0.220, wing_le=0.25, engines=nose_prop(1.6)),   # Davis DA-2
    "DEFI": row("single", fus_w=0.95, nose_len=1.6, wing_le=0.50, sweep=0.0, cr=0.13, ct=0.07, inner=None,
                engines=nose_prop(1.7) + pusher(1.7, 0.97),
                canard=dict(at=0.10, semi=0.24, cr=0.07, ct=0.05),
                stab=dict(semi=0.0)),   # Rutan Defiant push-pull
    "DH82": row("biplane", fus_w=0.85, wing_le=0.19, cr=0.185, ct=0.185, engines=nose_prop(2.0)),   # Tiger Moth
    "DO28": row("twin", fus_w=1.3, nose_len=1.5, wing_le=0.36, sweep=0.0, cr=0.135, ct=0.100,
                engines=props(0.85, 1.1, 0.2, 2.0, 0.28)),   # Do 28A
    "E300": row("cherokee", fus_w=0.85, wing_le=0.27, cr=0.19, ct=0.13, inner=None,
                nose_len=1.6, nose_pow=0.55, engines=nose_prop(1.9),
                stab=dict(semi=0.17, cr=0.11, ct=0.08)),   # Extra EA-300
    "E390": row("jet_transport", fus_w=3.6, nose_len=1.5, wing_le=0.38, sweep=24.0, cr=0.15, ct=0.05,
                tail_len=2.6, engines=jets(1.9, 4.0, 0.30, ahead=0.7),
                stab=dict(semi=0.17, sweep=25.0, cr=0.12)),   # KC-390
    "EAGL": row("biplane", fus_w=0.75, cr=0.21, ct=0.21, wing_le=0.20,
                engines=nose_prop(1.9)),   # Aviat Christen Eagle
    "EDGE": row("cherokee", fus_w=0.85, wing_le=0.27, cr=0.20, ct=0.13, inner=None,
                nose_len=1.6, nose_pow=0.55, engines=nose_prop(1.9),
                stab=dict(semi=0.17, cr=0.11, ct=0.08)),   # Edge 540
    "EFOX": row("cub", fus_w=1.0, wing_le=0.22, engines=nose_prop(1.7)),   # Eurofox
    "EV97": row("cherokee", fus_w=1.05, cr=0.17, ct=0.14, inner=None, engines=nose_prop(1.7)),   # EV-97 EuroStar
    "EVOP": row("cirrus", fus_w=1.2, nose_len=1.8, cr=0.14, ct=0.075, engines=nose_prop(2.0)),   # Lancair Evolution
    "EVSS": row("cherokee", fus_w=1.05, cr=0.17, ct=0.14, inner=None, engines=nose_prop(1.7)),   # SportStar
    "EXNG": row("cherokee", fus_w=0.85, wing_le=0.27, cr=0.19, ct=0.13, inner=None,
                nose_len=1.6, nose_pow=0.55, engines=nose_prop(1.9),
                stab=dict(semi=0.17, cr=0.11, ct=0.08)),   # Extra NG
    "F260": row("cherokee", fus_w=1.0, cr=0.17, ct=0.11, inner=None, nose_len=1.6, nose_pow=0.55,
                tip_tanks=dict(d=0.45, len=2.0), engines=nose_prop(2.0)),   # SF.260
    "F8L": row("cherokee", fus_w=1.0, cr=0.16, ct=0.10, inner=None, nose_len=1.6, nose_pow=0.55,
               engines=nose_prop(1.9)),   # Falco
    "FDF2": row("single", fus_w=1.0, cr=0.14, ct=0.115, inner=None, nose_len=1.4, engines=nose_prop(1.7)),   # Flight Design F2
    "FOX": row("cub", fus_w=0.95, wing_le=0.22, engines=nose_prop(1.8)),   # Kitfox
    "G21": row("twin", fus_w=1.5, nose_len=1.3, nose_pow=0.5, wing_le=0.30, cr=0.150, ct=0.100, inner=0.40,
               engines=props(0.95, 1.5, 0.3, 2.6, 0.40),
               stab=dict(semi=0.18, cr=0.11, ct=0.07)),   # Grumman Goose (high wing, hull)
    "GA4C": row("gulfstream", sweep=33.0, engines=aft(1.5, 3.9, 0.665)),   # Gulfstream G400
    "GB1": row("cherokee", fus_w=0.8, cr=0.20, ct=0.14, inner=None, wing_le=0.28,
               nose_len=1.6, nose_pow=0.55, engines=nose_prop(1.9)),   # GameBird
    "GLAS": row("cirrus", fus_w=0.95, cr=0.19, ct=0.12, wing_le=0.28, nose_len=1.7, engines=nose_prop(1.8)),   # Glasair
    "GLST": row("cub", fus_w=1.15, wing_le=0.23, cr=0.14, ct=0.12, engines=nose_prop(1.9)),   # GlaStar
    "HROC": row("cherokee", fus_w=0.8, cr=0.21, ct=0.15, inner=None, wing_le=0.28,
                nose_len=1.6, nose_pow=0.55, engines=nose_prop(1.8)),   # Harmon Rocket
    "IR23": row("cherokee", fus_w=1.2, cr=0.15, ct=0.11, inner=None, engines=nose_prop(1.9)),   # IAR-823
    "J3": row("cub", fus_w=0.9, engines=nose_prop(1.8)),   # Legend Cub
    "JAB4": row("single", fus_w=1.0, cr=0.14, ct=0.115, inner=None, engines=nose_prop(1.6)),   # Jabiru J200
    "JABI": row("single", fus_w=0.95, cr=0.14, ct=0.115, inner=None, engines=nose_prop(1.6)),   # Jabiru LSA
    "JSX": row("cherokee", fus_w=0.7, cr=0.20, ct=0.14, inner=None, nose_len=1.5,
               engines=centre(0.5, 1.3, 0.60)),   # SubSonex
    "LA25": row("single", fus_w=1.3, nose_len=1.0, nose_pow=0.5, wing_le=0.30, cr=0.14, ct=0.115, inner=None,
                engines=pusher(1.8, 0.6, d=0.7, ln=1.5), stab=dict(pos="T")),   # Lake Renegade (hull, pusher above wing)
    "LEG2": row("cirrus", fus_w=0.95, cr=0.17, ct=0.10, wing_le=0.28, nose_len=1.6, engines=nose_prop(1.8)),   # Lancair Legacy
    "LNC2": row("cirrus", fus_w=0.90, cr=0.17, ct=0.10, wing_le=0.28, nose_len=1.6, engines=nose_prop(1.7)),   # Lancair 200
    "LNCE": row("cirrus", fus_w=1.15, nose_len=1.6, engines=nose_prop(2.0)),   # Lancair ES
    "M200": row("cherokee", fus_w=1.2, cr=0.16, ct=0.10, inner=None, nose_len=1.6, nose_pow=0.55,
                engines=nose_prop(1.9)),   # Aero Commander 200
    "MG17": row("fighter", fus_w=1.4, nose_len=3.0, wing_le=0.38, sweep=43.0, cr=0.31, ct=0.12,
                stab=dict(semi=0.19, sweep=45.0, cr=0.14, ct=0.07)),   # FT-5 (two-seat MiG-17)
    "MOR2": row("cherokee", fus_w=1.0, cr=0.16, ct=0.12, inner=None, engines=nose_prop(1.8)),   # Morrisey 2150
    "MRF1": row("fighter", fus_w=1.5, nose_len=3.4, wing_le=0.42, sweep=47.0, cr=0.50, ct=0.15,
                stab=dict(semi=0.27, sweep=50.0, cr=0.28, ct=0.10)),   # Mirage F1
    "NG5": row("cherokee", fus_w=1.1, cr=0.14, ct=0.12, inner=None, engines=nose_prop(1.8)),   # Bristell NG-5
    "P136": row("twin", fus_w=1.4, nose_len=1.4, nose_pow=0.5, wing_le=0.28, cr=0.13, ct=0.10, inner=0.40,
                engines=props(0.9, 0.2, 1.2, 2.0, 0.17),
                stab=dict(semi=0.17, cr=0.10, ct=0.07)),   # Piaggio P.136 (gull wing, pushers)
    "P38": row("booms", fus_w=1.3, nose_len=1.2, wing_le=0.30, cr=0.21, ct=0.09, inner=None,
               booms=dict(eta=0.36, w=0.75, pod=0.62),
               engines=props(1.0, 2.5, 1.0, 3.3, 0.36),
               stab=dict(semi=0.20, sweep=0.0, cr=0.11, ct=0.09)),   # F-5 Lightning (P-38 photo)
    "PIAT": row("cherokee", fus_w=1.0, cr=0.12, ct=0.095, inner=None, engines=nose_prop(1.7)),   # Alpha Trainer
    "PISI": row("glider", engines=nose_prop(1.75), fus_w=0.85, nose_len=1.6,
                stab=dict(pos="T", semi=0.095)),   # Pipistrel Sinus
    "PIVI": row("glider", fus_w=0.85, nose_len=1.8, engines=nose_prop(1.7), wing_le=0.30, cr=0.085, ct=0.050, stab=dict(semi=0.12, pos="T")),  # Pipistrel Surveyor
    "PP3":  row("bonanza", fus_w=1.0, nose_len=1.8, wing_le=0.30, cr=0.150, ct=0.085, engines=nose_prop(2.0)),  # Pilatus P-3
    "RBEL": row("cub", fus_w=0.95, wing_le=0.24),  # Murphy Rebel
    "RC3":  row("single", fus_w=1.35, nose_len=1.2, wing_le=0.30, cr=0.130, ct=0.110, inner=0.5, engines=pusher(1.8, 0.56, d=0.7, ln=1.5), stab=dict(semi=0.16, pos="conv", back=0.05)),  # Republic RC-3 Seabee (pusher engine above the wing; amphibian, high wing)
    "RELI": row("single", fus_w=1.25, nose_len=1.3, wing_le=0.24, cr=0.135, ct=0.105, inner=0.40, engines=nose_prop(2.4)),  # Stinson AT-19 Reliant
    "RISN": row("cirrus", fus_w=0.95, nose_len=1.7, wing_le=0.30, cr=0.115, ct=0.075, engines=nose_prop(1.8)),  # Porto Risen
    "RS20": row("cub", fus_w=0.95, wing_le=0.23),  # Rans S-20 Raven
    "RS21": row("cub", fus_w=1.05, wing_le=0.22, cr=0.150, ct=0.125),  # Rans S-21 Outbound
    "S211": row("jet_trainer", fus_w=1.35, nose_len=2.3, nose_pow=0.7, wing_le=0.40, sweep=10.0, cr=0.200, ct=0.100, stab=dict(semi=0.200, sweep=18.0, cr=0.140, ct=0.070, pos="T")),  # PADC / Aermacchi S-211
    "S58T": row("heli_heavy", blades=4, bw=0.16, nose=0.30, rear=0.22, tail=0.58, tailplane=0.12),  # Sikorsky S-58T (4 blades)
    "S64":  row("heli_heavy", blades=6, bw=0.11, nose=0.22, rear=0.16, tail=0.50, tailplane=0.14),  # Sikorsky S-64 / CH-54 Skycrane (6 blades)
    "SA30": row("biplane", fus_w=0.85, wing_le=0.20, cr=0.175, ct=0.175),  # Stolp Starduster Too
    "SGRA": row("cub", fus_w=1.05, wing_le=0.23, cr=0.150, ct=0.130),  # Prestige Storm Rally
    "SKAR": row("single", fus_w=0.95, nose_len=1.4, wing_le=0.27, cr=0.130, ct=0.100, inner=0.40, stab=dict(semi=0.15, pos="conv")),  # Sky Arrow 650
    "SLG2": row("cherokee", fus_w=1.05, nose_len=1.5, wing_le=0.28, cr=0.135, ct=0.105, engines=nose_prop(1.7)),  # Sling 2 LSA
    "SLG4": row("cherokee", fus_w=1.15, nose_len=1.5, wing_le=0.28, cr=0.135, ct=0.100, engines=nose_prop(1.9)),  # Sling 4 TSi
    "SONX": row("cherokee", fus_w=0.85, nose_len=1.5, wing_le=0.30, cr=0.150, ct=0.130, engines=nose_prop(1.5)),  # Sonex
    "SPUP": row("biplane", fus_w=0.85, wing_le=0.20, cr=0.165, ct=0.165, engines=nose_prop(2.3)),  # Sopwith Pup replica
    "SREY": row("single", fus_w=1.25, nose_len=1.2, wing_le=0.28, cr=0.135, ct=0.120, inner=0.5, engines=pusher(1.8, 0.55, d=0.7, ln=1.5), stab=dict(semi=0.15, pos="conv", back=0.05)),  # Progressive Aerodyne SeaRey (pusher on pylon, amphibian, high wing)
    "SS2T": row("turbine_single", fus_w=1.30, nose_len=1.5, nose_pow=0.5, wing_le=0.30, cr=0.095, ct=0.085, stab=dict(semi=0.20, sweep=4.0, cr=0.11, ct=0.07), engines=nose_prop(2.6)),  # Ayres S-2R Turbo Thrush
    "SVNH": row("cub", fus_w=1.05, wing_le=0.22, cr=0.145, ct=0.130),  # Savannah MXP-740
    "SX30": row("cirrus", fus_w=0.90, nose_len=1.6, wing_le=0.30, cr=0.130, ct=0.100, engines=nose_prop(1.8)),  # Swearingen SX-300
    "SYMP": row("cherokee", fus_w=1.10, nose_len=1.5, wing_le=0.28, cr=0.140, ct=0.100, engines=nose_prop(1.9)),  # OMF Symphony
    "T18":  row("cirrus", fus_w=0.80, nose_len=1.7, wing_le=0.31, cr=0.170, ct=0.130, engines=nose_prop(1.7)),  # Classic Sport S-18 (Thorp T-18)
    "TAIL": row("single", fus_w=0.80, nose_len=1.6, wing_le=0.25, cr=0.180, ct=0.130, inner=0.4, engines=nose_prop(1.8)),  # Wittman W-10 Tailwind
    "TARR": row("cherokee", fus_w=1.05, nose_len=1.5, wing_le=0.28, cr=0.130, ct=0.100, engines=nose_prop(1.8)),  # Pelegrin Tarragon
    "TF21": row("single", fus_w=0.95, nose_len=1.3, wing_le=0.24, cr=0.140, ct=0.125, inner=0.5, engines=nose_prop(1.9)),  # Taylor Kits T-Kraft (Taylorcraft-style high wing)
    "TNAV": row("twin", fus_w=1.15, nose_len=1.8, wing_le=0.33, cr=0.150, ct=0.095, engines=props(0.70, 1.2, -0.1, 1.9, 0.30)),  # Camair Twin Navion
    "TRF1": row("cirrus", fus_w=0.85, nose_len=1.4, wing_le=0.32, cr=0.150, ct=0.130, engines=nose_prop(1.6)),  # Team Rocket F1
    "TRIM": row("twin", fus_w=1.9, nose_len=1.2, nose_pow=0.5, wing_le=0.27, sweep=0.0, cr=0.190, ct=0.170, inner=0.9,
                engines=nose_prop(2.4) + props(0.85, 1.8, 0.0, 2.3, 0.28),
                stab=dict(semi=0.20, sweep=0.0, cr=0.100, ct=0.085, pos="conv", back=0.03)),  # Ford 4-AT Trimotor (nose + 2 wing engines)
    "TWEN": row("single", fus_w=1.25, nose_len=1.4, wing_le=0.25, cr=0.130, ct=0.100, inner=0.45, engines=nose_prop(1.9)),  # Tecnam P2010
    "VELO": row("single", fus_w=0.85, nose_len=1.6, wing_le=0.50, sweep=16.0, cr=0.13, ct=0.06, inner=None,
                engines=pusher(1.6, 0.97), canard=dict(at=0.12, semi=0.28, cr=0.08, ct=0.06),
                stab=dict(semi=0.0)),  # Velocity 173 (canard pusher, like Long-EZ row)
    "VJ22": row("single", fus_w=1.30, nose_len=1.2, wing_le=0.30, cr=0.130, ct=0.100, inner=0.5, engines=pusher(1.8, 0.57, d=0.7, ln=1.5), stab=dict(semi=0.15, pos="T", back=0.04)),  # Volmer VJ-22 Sportsman (pusher on pylon, amphibian, high wing)
    "VTRA": row("cub", fus_w=1.00, wing_le=0.22),  # Rans S-19 Venterra
    "WACF": row("biplane", fus_w=1.0, nose_len=1.4, wing_le=0.20, cr=0.150, ct=0.150, engines=nose_prop(2.0)),  # Waco Classic YMF-5
    "WAIX": row("cherokee", fus_w=0.85, nose_len=1.5, wing_le=0.30, cr=0.150, ct=0.130, engines=nose_prop(1.5), stab=dict(semi=0.12, sweep=20.0, cr=0.11, ct=0.06)),  # Sonex Waiex (V-tail drawn as a small stab)
    "XL2":  row("cherokee", fus_w=1.05, nose_len=1.5, wing_le=0.28, cr=0.150, ct=0.115, engines=nose_prop(1.8)),  # Liberty / Discovery XL-2
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
    "C337": (11.63, 9.07, "SKYBRARY Cessna 337 (11.60 m, 9.10 m), 38 ft 2 in x 29 ft 9 in"),
    # --- in the FAA table, but its row describes another variant
    "AC6L": (14.95, 10.70, "Twin Commander 680FL data, 588.56 in x 421.25 in; the FAA row's 13.44 x 13.11 m is wrong in both span and length"),
    "B744": (64.44, 70.66, "747-400 with winglets; the FAA row has the 747-100/-200 span"),
    "AT75": (27.05, 27.17, "ATR 72-500 fact sheet; the FAA row has the ATR 42 span"),
    "E75L": (28.65, 31.68, "E175 with the extended wingtips; the FAA row has E175-E2 figures"),
    "E135": (20.04, 26.33, "ERJ-135; the FAA row has the longer ERJ-140"),
    "C340": (11.62, 10.46, "Cessna 340A; the FAA row lists 13.20 m, longer than the larger Cessna 414"),
    "GLID": (15.00, 6.80, "typical 15 m class sailplane; GLID is a generic designator"),
    # --- airliners and jets, A to E
    "B733": (28.88, 33.40, "Boeing 737 airport planning document, 94 ft 9 in; the FAA row carries a 31.21 m span with winglets that 737-300s lack"),
    "B773": (60.93, 73.86, "Boeing 777 airport planning data, 242 ft 4 in; the FAA row carries a 63.73 m length, the 777-200's"),
    # --- piston aircraft, C to T
    "CVLP": (32.11, 24.14, "Convair 340/440 data, 105 ft 4 in x 79 ft 2 in; the FAA row carries a 27.98 m span, the CV-240's"),
    # --- military and government
    "F35": (10.67, 15.67, "USAF Air Combat Command fact sheet, F-35A (35 ft, 51.4 ft)"),
    "F4": (11.71, 19.20, "Air National Guard fact sheet, F-4E (38 ft 5 in, 63 ft)"),
    "F5": (8.13, 14.45, "USAF Hill AFB museum fact sheet span, F-5E (26 ft 8 in); length from reference data"),
    "F14": (11.65, 19.10, "F-14D wings fully swept 38 ft 2.5 in, length 62 ft 8 in, Navy-derived specs"),
    "F104": (6.68, 16.71, "National Museum of the USAF fact sheet, F-104C (21 ft 11 in, 54 ft 10 in)"),
    "F117": (13.20, 20.09, "USAF and Lockheed Martin fact sheets, F-117A (43 ft 4 in, 65 ft 11 in)"),
    "A4": (8.38, 12.22, "A-4F Skyhawk, National Museum of Naval Aviation airframe data (27 ft 6 in, 40 ft 1 in)"),
    "HAR": (9.25, 14.12, "AV-8B Harrier II, Pima museum data (30 ft 4 in, 46 ft 4 in)"),
    "B1": (24.08, 44.50, "Boeing / USAF museum B-1B data, swept 79 ft, length 146 ft"),
    "U2": (32.00, 19.20, "USAF fact sheet, U-2S (105 ft, 63 ft)"),
    "E3TF": (44.42, 46.61, "USAF E-3 Sentry fact sheet (145 ft 9 in, 152 ft 11 in)"),
    "E3CF": (44.42, 46.61, "USAF E-3 Sentry fact sheet (145 ft 9 in, 152 ft 11 in)"),
    "E6": (45.21, 45.82, "E-6B Mercury, Tinker AFB / Navy fact file (148 ft 4 in, 150 ft 4 in)"),
    "C5M": (67.89, 75.31, "USAF Air Mobility Command fact sheet, C-5M (222 ft 9 in, 75.3 m)"),
    "E2": (24.56, 17.60, "US Navy fact file, E-2D (80 ft 7 in, 57 ft 8.75 in)"),
    "T33": (11.43, 11.48, "National Museum of the USAF fact sheet, T-33A (37 ft 6 in, 37 ft 8 in)"),
    "L39": (9.46, 12.13, "L-39 Albatros, museum data sheet, span with tip tanks"),
    "PC7": (10.40, 9.78, "Pilatus PC-7, reference data sheet"),
    "PC9": (10.12, 10.14, "Pilatus PC-9M, reference data (33 ft 3 in, 33 ft 3 in)"),
    "PC21": (9.11, 11.23, "Pilatus PC-21 reference data (9.108 m, 11.233 m)"),
    "T37": (10.31, 8.92, "T-37B, Pima museum / reference data (33 ft 10 in, 29 ft 3 in)"),
    "F86": (11.30, 11.43, "Air National Guard fact sheet, F-86 (37 ft 1 in, 37 ft 6 in)"),
    "MG29": (11.36, 17.32, "MiG-29 9-12 reference data (11.36 m, 17.32 m)"),
    "SU27": (14.70, 21.94, "Su-27S reference data (14.70 m, 21.94 m)"),
    "EUFI": (10.95, 15.96, "Leonardo Eurofighter Typhoon page (10.95 m, 15.96 m)"),
    "RFAL": (10.90, 15.30, "Dassault Aviation Rafale specifications page"),
    "TOR": (8.60, 16.72, "Tornado IDS, MILAVIA data, wings fully swept 67 degrees"),
    "A6": (16.15, 16.69, "A-6E Intruder, Patriots Point / aero-web data (53 ft, 54 ft 9 in)"),
    "S3": (20.93, 16.26, "S-3B Viking, Navy SAC-derived data (68 ft 8 in, 53 ft 4 in)"),
    "C27J": (28.70, 22.70, "Bundeswehr C-27J page"),
    "C295": (25.81, 24.45, "Airbus C295M data, no winglets (C295W with winglets is 27.59 m)"),
    "AN22": (64.40, 57.92, "An-22: flugzeuginfo.net 64.40 m span; length 57.8-57.9 m by source, 57.92 m nose-config dependent (aerospaceweb/aerocorner)"),
    "V10": (12.19, 12.67, "National Museum of the USAF fact sheet, OV-10A (40 ft, 41 ft 7 in)"),
    "C123": (33.53, 23.09, "Travis AFB Aviation Museum, C-123K (110 ft, 75 ft 9 in)"),
    "C119": (33.32, 26.37, "Travis AFB / Hill Aerospace Museum, C-119G (109 ft 4 in, 86 ft 6 in)"),
    # --- more airliners, utility and sport aircraft
    "B3XM": (35.92, 43.80, "Boeing 737-10 specifications page"),
    "B74S": (59.64, 56.31, "Boeing 747SP, Wikipedia/flugzeuginfo agree (59.64 m, 56.3 m)"),
    "B74R": (59.64, 70.66, "ICAO Doc 8643 listing for B74R (59.6 m, 70.7 m)"),
    "A3ST": (44.84, 56.16, "Airbus A300-600ST data (SKYbrary 56.2 m, aircraft directory 56.16 m)"),
    "CRJX": (26.18, 39.13, "MHI CRJ page / Bombardier fact sheet (26.2 m, 39.1 m)"),
    "A148": (28.91, 29.13, "Antonov An-148 (all-aero, Wikipedia-derived)"),
    "A158": (28.91, 34.37, "Antonov An-158 (zap16, armyrecognition; some sources say 31.6 m)"),
    "AN24": (29.20, 23.53, "airvectors An-24V (29.20 m, 23.53 m)"),
    "AN30": (29.20, 24.26, "Wikipedia An-30 infobox"),
    "AN32": (29.20, 23.78, "airvectors An-32 (23.78 m)"),
    "AN28": (22.06, 13.10, "Wikipedia PZL M28 (SKYbrary 22.10 m, 13.00 m)"),
    "IL62": (43.20, 53.12, "Il-62M: GlobalSecurity Il-62 specs, 43.20 m span, 53.12 m length"),
    "IL96": (60.11, 55.35, "Il-96-300 technical data (60.11 m, 55.35 m)"),
    "T154": (37.55, 47.90, "Tu-154M spec table (SKYbrary 37.5 m, 48.0 m)"),
    "T204": (41.80, 46.14, "Tu-204-100 Wikipedia; GlobalSecurity gives 42.0 m, 46.0 m"),
    "YK42": (34.88, 36.38, "Yak-42 (Wikipedia-derived; SKYbrary 34.9 m, 36.4 m)"),
    "F27": (29.00, 23.56, "Fokker F27 Mk 200 (globalmilitary 29 m, 23.6 m)"),
    "F50": (29.00, 25.25, "Fokker 50 (29.00 m, 25.25 m, two sources)"),
    "F70": (28.08, 30.91, "Fokker 70 (Fokker 100 wing, 30.91 m)"),
    "F100": (28.08, 35.53, "Fokker 100 span 28.08 m, length 35.53 m (Fokker data); SKYbrary rounds to 28.10 / 35.50 m"),
    "F28": (25.07, 29.61, "Fokker F28-4000 (25.07 m, 29.61 m)"),
    "DC86": (45.23, 57.12, "DC-8-63 airframe reference (45.24 m, 57.12 m)"),
    "DC94": (28.47, 38.28, "McDonnell Douglas DC-9-41 listing (28.47 m, 38.28 m)"),
    "B701": (39.88, 44.07, "Boeing 707-120 (130 ft 10 in, 144 ft 6 in)"),
    "B720": (39.88, 41.68, "Boeing 720 span same as 707-120; length 41.68 m (unverified, sources vary 41.25-41.68)"),
    "L188": (30.18, 31.85, "Lockheed L-188 (flugzeuginfo, all-aero: 99 ft, 104 ft 6 in)"),
    "D228": (16.97, 16.56, "Dornier 228-200 (aeropedia 16.97 m, 16.56 m)"),
    "L410": (19.98, 14.42, "Let L-410 UVP-E (Vanair 19.980 m, 14.424 m)"),
    "E121": (14.45, 12.25, "Embraer EMB-121 Xingu (Embraer / technical data, Xingu I)"),
    "BN2P": (14.94, 10.86, "Britten-Norman BN-2 Islander (49 ft span; length ~35 ft 7.75 in)"),
    "TRIS": (16.15, 15.01, "Britten-Norman Trislander (Wikipedia: 53 ft 0 in, 49 ft 3 in)"),
    "DHC3": (17.69, 12.75, "DHC-3 Otter (58 ft, 41 ft 10 in)"),
    "DH2T": (14.63, 10.74, "SKYbrary DH2T Turbo Beaver (14.63 m, 10.74 m)"),
    "DHC4": (29.15, 22.13, "DHC-4 Caribou (95 ft 7.5 in, 72 ft 7 in)"),
    "DHC5": (29.26, 24.08, "DHC-5D Buffalo (96 ft 0 in, 79 ft 0 in)"),
    "AN2": (18.18, 12.74, "Antonov An-2 upper wing 18.18 m, length 12.74 m (flugzeuginfo.net, all-aero); some sources say 12.4 m"),
    "PC6T": (15.87, 10.90, "Pilatus PC-6/B2-H4 Turbo Porter (15.87 m; SKYbrary length 10.90 m)"),
    "C46": (32.92, 23.27, "Curtiss C-46 (108 ft, 76 ft 4 in)"),
    "RV4": (7.01, 6.20, "Van's Aircraft RV-4 specifications (23 ft, 20 ft 4 in)"),
    "RV6": (7.01, 6.15, "Van's Aircraft RV-6 specifications (23 ft, 20 ft 2 in)"),
    "RV7": (7.62, 6.20, "Van's Aircraft RV-7 specifications (25 ft, 20 ft 4 in)"),
    "RV8": (7.32, 6.40, "Van's Aircraft RV-8 specifications (24 ft, 21 ft)"),
    "RV9": (8.53, 6.22, "Van's Aircraft RV-9 specifications (28 ft, 20 ft 5 in)"),
    "RV10": (9.68, 7.44, "Van's Aircraft RV-10 specifications (31 ft 9 in, 24 ft 5 in)"),
    "RV14": (8.23, 6.43, "Van's Aircraft RV-14 specifications (27 ft, 21 ft 1 in)"),
    "M7": (10.03, 7.21, "Wikipedia Maule M-7-235B (32.92 ft, 23.67 ft); other M-7 figures vary"),
    "C82S": (10.97, 8.84, "Cessna T182T specifications (36 ft, 29 ft)"),
    "DA62": (14.55, 9.19, "Diamond DA62 (Wikipedia/GlobalAir 14.55 m, 9.19 m)"),
    "DA50": (13.41, 9.23, "Diamond DA50 RG (Wikipedia: 13.41 m, 9.23 m)"),
    "P28U": (10.80, 8.30, "PA-28RT-201T Turbo Arrow IV (35 ft 5 in; SKYbrary length 8.30 m)"),
    "M600": (13.16, 9.05, "Piper M600 spec sheet (43.16 ft, 29.7 ft)"),
    "P208": (8.80, 6.93, "Tecnam P2008 (Wikipedia: 8.80 m, 6.93 m)"),
    "P06T": (11.40, 8.69, "Tecnam P2006T (Wikipedia/AOPA: 37 ft 5 in, 28 ft 6 in)"),
    "ECHO": (8.70, 6.40, "Tecnam P92 Echo MkII (Wikipedia: 8.7 m, 6.4 m; early Echo 9.6 m)"),
    "PTS1": (5.28, 4.72, "Pitts S-1S (Smithsonian: 17 ft 4 in, 15 ft 6 in)"),
    "PTS2": (6.10, 5.72, "Pitts S-2B Jane's (20 ft 0 in, 18 ft 9 in)"),
    "ST75": (9.80, 7.54, "Boeing-Stearman PT-17 (32 ft 2 in, 24 ft 9 in)"),
    "YK52": (9.30, 7.75, "Yak-52 spec listing (30 ft 6 in, 25 ft 5 in)"),
    "Z42": (9.34, 6.94, "Zlin Z-242L (aeropedia 9.34 m, 6.94 m)"),
    "SPIT": (11.23, 9.12, "Spitfire Mk I (36 ft 10 in, 29 ft 11 in)"),
    "B17": (31.62, 22.66, "Boeing B-17G (103 ft 9 in, 74 ft 4 in) per flugzeuginfo.net"),
    "B25": (20.60, 16.13, "North American B-25 per flugzeuginfo.net (67 ft 7 in span); some museum airframes list longer"),
    "B29": (43.05, 30.18, "Boeing B-29 per flugzeuginfo.net, 43.05 m span, 30.18 m length"),
    "LGEZ": (7.96, 5.12, "Rutan Long-EZ (Jane's/AOPA: 26 ft 1 in, 16 ft 10 in)"),
    # --- helicopters: (main rotor diameter, fuselage length). Only the rotor diameter is
    #     used, and only for the scale comment: a helicopter's drawing does not depend on
    #     either figure. These were not individually re-checked against the sources named.
    "R44":  (10.06, 9.07, "Robinson R44 data sheet: rotor 33 ft, fuselage ~9.07 m"),
    "B06":  (10.16, 9.50, "Bell 206B-3 spec: rotor 33 ft 4 in, fuselage 9.50 m"),
    "AS50": (10.69, 10.93, "Airbus AS350 B3/H125: rotor 10.69 m, fuselage 10.93 m"),
    "EC35": (10.20, 10.20, "Airbus EC135/H135: rotor 10.20 m, fuselage 10.2 m"),
    "H60":  (16.36, 15.26, "UH-60A: rotor 53 ft 8 in, fuselage 15.26 m"),
    "H47":  (18.29, 15.87, "CH-47D: rotors 60 ft each, fuselage 15.87 m"),
    "R22":  (7.67, 6.30, "Robinson R22 data sheet: rotor 25 ft 2 in"),
    "R66":  (10.06, 9.00, "Robinson R66 data sheet: rotor 33 ft"),
    "B06T": (11.28, 10.67, "Bell 206L series: rotor 37 ft, fuselage ~10.7 m"),
    "B407": (10.66, 9.80, "Bell 407 spec: rotor 35 ft, fuselage ~9.8 m"),
    "B412": (14.02, 12.92, "Bell 412 spec: rotor 46 ft, fuselage ~12.9 m"),
    "B429": (10.97, 11.00, "Bell 429 spec: rotor 36 ft, fuselage ~11 m"),
    "B430": (12.80, 12.70, "Bell 430 spec: rotor 42 ft, fuselage ~12.7 m"),
    "B212": (14.69, 12.92, "Bell 212 spec: rotor 48 ft 2 in, fuselage ~12.9 m"),
    "UH1":  (14.63, 12.77, "Bell 205/UH-1H: rotor 48 ft, fuselage 12.77 m"),
    "B47G": (11.32, 9.63, "Bell 47G: rotor 37 ft 2 in, fuselage ~9.6 m"),
    "B47J": (11.32, 9.00, "Bell 47J Ranger: rotor 37 ft 2 in, fuselage ~9 m"),
    "B505": (10.98, 11.20, "Bell 505 Jet Ranger X spec: rotor 36 ft"),
    "B222": (12.80, 12.40, "Bell 222: rotor 42 ft, fuselage ~12.4 m"),
    "B230": (12.80, 12.40, "Bell 230: rotor 42 ft, fuselage ~12.4 m"),
    "AS65": (11.94, 11.63, "Airbus AS365 Dauphin: rotor 11.94 m, fuselage 11.63 m"),
    "AS32": (15.60, 16.29, "Airbus AS332L Super Puma: rotor 15.6 m, fuselage ~16.3 m"),
    "EC20": (10.00, 9.60, "Airbus EC120: rotor 10.0 m, fuselage ~9.6 m"),
    "EC45": (11.00, 10.20, "Airbus EC145: rotor 11.0 m, fuselage ~10.2 m"),
    "EC55": (12.60, 12.70, "Airbus EC155: rotor 12.6 m, fuselage ~12.7 m"),
    "EC25": (16.20, 16.79, "Airbus H225: rotor 16.2 m, fuselage ~16.8 m"),
    "EC75": (14.80, 15.70, "Airbus H175: rotor 14.8 m, fuselage ~15.7 m"),
    "A109": (11.00, 11.45, "Leonardo AW109: rotor 11.0 m, fuselage ~11.5 m"),
    "A119": (10.83, 10.10, "Leonardo AW119: rotor 10.83 m, fuselage ~10.1 m"),
    "A139": (13.80, 13.77, "Leonardo AW139: rotor 13.8 m, fuselage 13.77 m"),
    "A169": (12.12, 12.19, "Leonardo AW169: rotor 12.12 m, fuselage ~12.2 m"),
    "A189": (14.60, 14.90, "Leonardo AW189: rotor 14.6 m, fuselage ~14.9 m"),
    "S76":  (13.41, 13.22, "Sikorsky S-76C: rotor 44 ft, fuselage 13.22 m"),
    "S92":  (17.17, 17.10, "Sikorsky S-92: rotor 56 ft 4 in, fuselage ~17.1 m"),
    "S61":  (18.90, 17.00, "Sikorsky S-61N: rotor 62 ft, fuselage ~17 m"),
    "H64":  (14.63, 15.06, "AH-64D: rotor 48 ft, fuselage 15.06 m"),
    "H53":  (22.02, 20.47, "Sikorsky CH-53D: six-blade rotor 72 ft 3 in (the seven-blade CH-53E has its own designator)"),
    "H500": (8.05, 7.30, "MD 500E: rotor 26 ft 5 in, fuselage ~7.3 m"),
    "MD52": (8.33, 9.80, "MD 520N: rotor 27 ft 4 in"),
    "MD60": (8.38, 9.40, "MD 600N: rotor 27 ft 6 in"),
    "EXPL": (10.31, 9.80, "MD 900/902 Explorer: rotor 33 ft 10 in"),
    "EN28": (9.75, 8.92, "Enstrom 280/F28: rotor 32 ft, length ~8.9 m"),
    "EN48": (9.75, 9.30, "Enstrom 480: rotor 32 ft"),
    "H269": (8.18, 6.90, "Hughes 269/300C: rotor 26 ft 10 in"),
    "G2CA": (7.20, 6.80, "Guimbal Cabri G2: rotor 7.2 m"),
    "BK17": (11.00, 9.91, "BK117 C-1: rotor 11.0 m, fuselage 9.91 m"),
    "B105": (9.84, 8.56, "MBB BO-105: rotor 9.84 m, fuselage 8.56 m"),
    "HUCO": (14.63, 13.60, "AH-1F: rotor 48 ft, fuselage ~13.6 m"),
    "MI8":  (21.29, 18.17, "Mil Mi-8T: rotor 21.29 m, fuselage 18.17 m"),
    "H46":  (15.24, 13.66, "CH-46E: rotors 50 ft each"),
    "EH10": (18.59, 19.53, "AW101: rotor 18.59 m, fuselage ~19.5 m"),
    "NH90": (16.30, 16.13, "NH90: rotor 16.3 m, fuselage ~16.1 m"),
    "LYNX": (12.80, 12.06, "Super Lynx: rotor 12.8 m, fuselage ~12.1 m"),
    "H160": (13.40, 13.96, "Airbus H160: rotor 13.4 m"),
    # --- seen on a real receiver, added after the first coverage pass
    "GA8C": (31.39, 30.40, "Gulfstream G800 103 ft 0 in span, 99 ft 9 in length (aerocorner spec listing)"),
    "FA6X": (25.94, 25.68, "airport-technology.com and aviation24.be spec tables"),
    "V22": (25.78, 17.48, "Boeing / USAF fact sheet: width across the turning rotors (rotor diameter 11.6 m) and fuselage length; the FAA row gives the wing's span, which leaves the rotors out"),
    "ULAC": (9.00, 6.00, "typical ultralight; ULAC is a generic designator"),
    "GYRO": (8.40, 5.00, "typical gyrocopter rotor; GYRO is a generic designator"),
    "ACRO": (5.97, 5.33, "Acro Sport I: Wikipedia/aircraft directory, upper wing 19 ft 7 in, 17 ft 6 in"),
    "AG10": (10.42, 7.26, "AVIC AG100: Chinese state media quoting AVIC, 10.42 m span, 7.26 m long"),
    "AJET": (9.11, 11.75, "Alpha Jet E: Milavia/SKYbrary 9.11 m span, 11.75 m (Milavia; others 12.3-13.2 m with probe)"),
    "ALIG": (9.30, 6.30, "Arion Lightning LS-1: manufacturer figures via Wikipedia, 30 ft 6 in, 20 ft 8 in"),
    "ANGL": (12.18, 10.21, "Angel 44: Jane's via Wikipedia, 39 ft 11.5 in, 33 ft 6 in"),
    "AP22": (10.10, 6.30, "Aeroprakt A-22: Wikipedia-derived spec, 10.1 m, 6.3 m (A22LS is 9.55 m)"),
    "AP32": (9.45, 6.27, "Aeroprakt A-32 Vixxen: Foxbat Australia spec table and POH, 9.45 m, 6.27 m"),
    "AS31": (21.00, 7.07, "ASH 31 Mi: Wikipedia spec, 21 m with winglets, 7.07 m (EASA TCDS 7.10 m)"),
    "ASTO": (8.65, 7.00, "Tecnam Astore: AOPA/Tecnam data via Wikipedia, 8.65 m, 7.0 m"),
    "AT3P": (13.72, 8.23, "Air Tractor AT-300 series: aeropedia/Jane's, 45 ft, 27 ft"),
    "AVID": (9.14, 5.74, "Airdale Flyer/Backcountry: 30 ft 0 in span, 18 ft 10 in long (Wikipedia/aircraft directory)"),
    "B14A": (10.41, 6.50, "Bellanca 14-13 Cruisair Senior: Wikipedia/GlobalAir, 34 ft 2 in, 21 ft 4 in"),
    "B26": (21.34, 15.42, "A-26 Invader: sources agree 70 ft span; length 50 ft 7 in (15.42 m)"),
    "BDOG": (10.06, 7.09, "Beagle/Scottish Aviation Bulldog: aeropedia, 33 ft 0 in, 23 ft 3 in"),
    "BE4W": (13.25, 14.75, "Beechjet 400XT: Aerocorner/AvPay, 43 ft 6 in, 48 ft 5 in"),
    "BIPL": (6.10, 5.18, "EAA Biplane: Wikipedia/aircraft directory, 20 ft 0 in, 17 ft 0 in"),
    "BOLT": (7.32, 5.79, "Steen Skybolt: Wikipedia/Pima museum, 24 ft, 19 ft"),
    "BPAT": (10.36, 6.91, "Bearhawk Patrol: manufacturer page 34 ft span, 22 ft 8 in long"),
    "BR23": (9.27, 6.58, "Bristell B23: EASA TCDS, 9.27 m span, 6.58 m long"),
    "BUSH": (9.60, 5.70, "SkyReach BushCat: Wikipedia/aeroexpo, 9.6 m, 5.7 m"),
    "C190": (11.02, 8.33, "Cessna 190/195: Wikipedia/aeropedia, 36 ft 2 in, 27 ft 4 in"),
    "CB1": (7.72, 5.79, "Hatz CB-1: Jane's via Wikipedia, 25 ft 4 in, 19 ft 0 in"),
    "CC11": (10.44, 6.93, "Carbon Cub SS: Wikipedia (Flying magazine data), 34.25 ft, 22.75 ft"),
    "CC19": (10.46, 7.26, "CubCrafters XCub: Wikipedia/EASA TCDS, 10.46 m, 7.26 m (NXCub is the same airframe with nosewheel)"),
    "CH60": (8.23, 5.80, "Zenith CH 601 Zodiac: aeropedia/GlobalAir, 27 ft, 19 ft"),
    "CH75": (9.09, 6.65, "Zenith STOL CH 750: Zenith spec page, 29 ft 10 in, 21 ft 10 in"),
    "CH80": (9.55, 7.50, "Zenith STOL CH 801: aeropedia, 9.55 m, ~7.5 m"),
    "CJ6": (10.22, 8.46, "Nanchang CJ-6A: Jane's via Wikipedia, 10.22 m, 8.46 m"),
    "CL2T": (28.60, 19.82, "CL-415: SKYbrary/Army Recognition, 28.6 m span, 19.82 m long"),
    "CMAS": (10.31, 7.57, "Cessna C-34 Airmaster: Wikipedia, 33 ft 10 in, 24 ft 10 in"),
    "COY2": (10.52, 6.10, "Rans S-6 Coyote II: Wikipedia, 34 ft 6 in, 20 ft 0 in"),
    "COZY": (8.57, 5.15, "Cozy Mk IV: Wikipedia, 28 ft 1.2 in, 16 ft 10.8 in (AeroCanard is the same size)"),
    "CRER": (8.92, 7.09, "Rans S-7 Courier: Rans spec page, 29 ft 3 in, 23 ft 3 in"),
    "D11": (8.20, 6.37, "Jodel D.112: aeropedia, 8.2 m, 6.37 m"),
    "DA2": (5.86, 5.44, "Davis DA-2: Wikipedia, 19 ft 3 in, 17 ft 10 in"),
    "DEFI": (9.40, 6.95, "Rutan Defiant: Wikipedia, 30 ft 9 in, 22 ft 10 in"),
    "DH82": (8.94, 7.29, "DH.82 Tiger Moth: de Havilland Museum/GlobalAir, 29 ft 4 in, 23 ft 11 in"),
    "DO28": (13.80, 9.00, "Do 28 A-1/B-1: Jane's via Wikipedia, 13.80 m, 9.00 m"),
    "E300": (8.0, 6.9, "aviationdimension.com/web search, EA-300LP 26 ft 3 in, 22 ft 8 in"),
    "E390": (35.05, 35.2, "Embraer KC-390 brochure"),
    "EAGL": (6.07, 5.46, "Aviat Christen Eagle II kit table, 19 ft 11 in, 17 ft 11 in"),
    "EDGE": (7.42, 6.27, "Zivko Edge 540 data, 24 ft 4 in, 20 ft 7 in"),
    "EFOX": (9.125, 5.605, "Aeropro Eurofox manufacturer brochure"),
    "EV97": (8.1, 5.98, "pilotfriend.com EV-97 Eurostar data"),
    "EVOP": (11.0, 9.1, "Lancair Evolution manufacturer data, 37 ft, 30 ft"),
    "EVSS": (8.65, 5.98, "Evektor SportStar Max, AvSport/Jane's"),
    "EXNG": (8.3, 7.11, "Extra Aircraft Extra NG specification page"),
    "F260": (8.35, 7.10, "SF.260C data, 27 ft 5 in, 23 ft 4 in"),
    "F8L": (8.0, 6.5, "Falco F8L, Aeropedia/aircraftworlddirectory"),
    "FDF2": (9.87, 6.86, "EASA type data sheet, Flight Design F2"),
    "FOX": (9.75, 5.6, "GlobalAir Kitfox Classic IV, 32 ft, 18 ft 5 in"),
    "G21": (14.94, 11.7, "Grumman G-21A Goose, 49 ft 0 in, 38 ft"),
    "GA4C": (26.31, 26.29, "Gulfstream G400 manufacturer page"),
    "GB1": (7.7, 6.9, "Game Composites GB1, AOPA/Top Gun Voltige sheet"),
    "GLAS": (7.09, 6.5, "Glasair III, 23 ft 3 in, 21 ft 4 in"),
    "GLST": (10.67, 6.71, "GlaStar, Kitplanes data via Wikipedia, 35 ft, 22 ft"),
    "HROC": (6.67, 6.55, "Harmon Rocket II, Aeropedia"),
    "IR23": (10.0, 8.24, "IAR-823, Jane's 1980-81 via Wikipedia"),
    "J3": (10.82, 6.83, "American Legend AL3C-100, 35 ft 6 in, 22 ft 5 in"),
    "JAB4": (8.1, 6.55, "Jabiru J200, all-aero.com"),
    "JABI": (8.03, 5.0, "Jabiru LSA with 1600 engine, Wikipedia"),
    "JSX": (5.48, 5.05, "Sonex SubSonex JSX-2, Aeropedia"),
    "LA25": (11.58, 8.64, "Lake LA-250 Renegade, Jane's 1984-85, 38 ft, 28 ft 4 in"),
    "LEG2": (7.77, 6.71, "Lancair Legacy spec sheet, 25 ft 6 in, 22 ft"),
    "LNC2": (7.16, 5.99, "Lancair 200, Aeropedia, 23 ft 6 in, 19 ft 8 in"),
    "LNCE": (10.82, 7.62, "Lancair ES, SKYbrary"),
    "M200": (9.30, 7.41, "Aero Commander 200D, 30 ft 6 in, 24 ft 4 in"),
    "MG17": (9.63, 11.5, "JJ-5/FT-5 two-seat trainer data"),
    "MOR2": (9.14, 6.45, "Morrisey 2150A = Shinn/Varga 2150 Kachina: 30 ft 0 in, 21 ft 2 in (GlobalAir, Wikipedia)"),
    "MRF1": (8.40, 15.30, "Mirage F1C, aerospaceweb/migflug"),
    "NG5": (9.13, 6.45, "Bristell NG5 LSA 9.13 m span, 6.45 m length (GlobalAir 29 ft 11 in; Wikipedia)"),
    "P136": (13.53, 10.8, "Piaggio P.136-L1, Vintage Flying Museum data"),
    "P38": (15.84, 11.53, "P-38L data (F-5 is the same airframe), 52 ft, 37 ft 10 in"),
    "PIAT": (10.5, 6.5, "Pipistrel Alpha Trainer manufacturer sheet"),
    "PISI": (14.97, 6.5, "Pipistrel Sinus manufacturer datasheet"),
    "PIVI": (10.70, 6.45, "Pipistrel Surveyor sheet 10.7 m wing option; length from Virus SW listing 6.45 m (base airframe)"),
    "PP3":  (10.40, 8.75, "Pilatus P-3 specifications (10.40 m, 8.75 m)"),
    "RBEL": (9.14, 6.55, "Murphy Rebel (Aeropedia 9.14 m, 6.55 m; 30 ft, 21 ft 4 in on maker data)"),
    "RC3":  (11.47, 8.50, "Republic RC-3 Seabee (Smithsonian 37 ft 8 in; 27 ft 11 in)"),
    "RELI": (12.77, 8.60, "Stinson AT-19/V-77 (41 ft 11 in, ~28 ft 3 in, restored-AT-19 article)"),
    "RISN": (9.00, 6.80, "Risen maker comparison table (9.00 m, 6.80 m)"),
    "RS20": (9.22, 6.40, "Rans S-20 Raven spec page (30 ft 3 in, 21 ft)"),
    "RS21": (8.53, 6.83, "Rans S-21 Outbound (28 ft, 22 ft 4.9 in)"),
    "S211": (8.43, 9.30, "S-211 (Volandia/Aeropedia 8.43 m; Global Military/migflug 9.3 m)"),
    "S58T": (17.07, 14.25, "Sikorsky archives S-58 (56 ft rotor, 46 ft 9 in fuselage)"),
    "S64":  (21.95, 21.41, "S-64 Skycrane (72 ft rotor, 70 ft 3 in fuselage)"),
    "SA30": (7.30, 6.27, "Stolp Starduster Too (24 ft, 20 ft 7 in; Aircraft Spruce data)"),
    "SGRA": (8.60, 7.08, "Storm Rally (Aeropedia 8.6 m, 7.08 m)"),
    "SKAR": (9.68, 7.60, "Sky Arrow 650 (Giotto Air datasheet via Wikipedia)"),
    "SLG2": (9.16, 6.68, "Sling LSA (AeroExpo/Aeropedia 9.16 m, 6.68 m)"),
    "SLG4": (9.54, 7.175, "Sling TSi maker brochure (9.54 m, 7.175 m)"),
    "SONX": (6.71, 5.51, "Sonex (22 ft, 18 ft 1 in; maker data via Wikipedia)"),
    "SPUP": (8.20, 5.80, "Airdrome Sopwith Pup (27 ft, 19 ft)"),
    "SREY": (9.39, 6.84, "SeaRey (AOPA guide 30 ft 10 in, 22 ft 5 in)"),
    "SS2T": (14.47, 10.35, "Ayres S-2R-H80 Turbo Thrush (Aircraft World Directory 47 ft 6 in, 33 ft 11 in); S-2R-G close"),
    "SVNH": (9.00, 6.60, "MXP-740 Savannah (Polish ULC TCDS 9.0 m, 6.6 m)"),
    "SX30": (7.43, 6.44, "Swearingen SX-300 (AOPA Pilot 24 ft 4.5 in, 21 ft 1.5 in)"),
    "SYMP": (10.67, 6.96, "Symphony 160 (EASA TCDS 6.96 m; 35 ft span)"),
    "T18":  (6.35, 5.77, "Classic Sport S-18 (20 ft 10 in; 18 ft 10 in)"),
    "TAIL": (7.31, 5.94, "Wittman W-10 Tailwind (24 ft, 19 ft 6 in)"),
    "TARR": (7.94, 7.53, "Pelegrin Tarragon (7.94 m, 7.53 m, Wikipedia citing maker)"),
    "TF21": (10.97, 6.78, "Taylorcraft F-21 (Taylor Kits F-21B lineage): 36 ft span, 22 ft 3 in length (aircraftworlddirectory, aeropedia)"),
    "TNAV": (10.57, 8.53, "Camair Twin Navion (34 ft 8 in, 28 ft; twinnavion.com)"),
    "TRF1": (6.56, 6.40, "Team Rocket F1 (Jane's 2003-04: 21 ft 6 in, 21 ft)"),
    "TRIM": (22.56, 15.19, "Ford 4-AT (74 ft, 49 ft 10 in)"),
    "TWEN": (10.30, 7.97, "Tecnam P2010 (10.30 m, 7.97 m)"),
    "VELO": (8.94, 5.80, "Velocity SE maker page (29 ft 4 in, 19 ft)"),
    "VJ22": (11.13, 7.32, "Volmer VJ-22 (Jane's 1993-94: 36 ft 6 in, 24 ft)"),
    "VTRA": (8.53, 6.06, "Rans S-19 Venterra (28 ft, 19 ft 10.75 in)"),
    "WACF": (9.14, 7.04, "Waco YMF-5 (30 ft, 23 ft 1 in)"),
    "WAIX": (6.71, 5.51, "Sonex Waiex (22 ft, 18 ft 1 in)"),
    "XL2":  (8.72, 6.19, "Liberty XL2 (Wikipedia 8.72 m, 6.193 m)"),
}

# Engine counts the FAA table gets wrong (checked by test_type_shapes.py).
ENGINE_COUNT_FIXES = {
    "B721": 3, "B722": 3,   # the 727 is a trijet; the FAA rows say 2
    "SB20": 2,              # the Saab 2000 is a twin; the FAA row says 1
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
    "C135": "K35R", "R135": "K35R", "K35E": "K35R", "C2": "E2",
    "BN2T": "BN2P",
    "DH3T": "DHC3",
    "AS55": "AS50",
    "EC30": "AS50",
    "C82T": "C82R",   # Turbo Skylane RG: the Skylane RG airframe
    "T206": "C206",   # Turbo Stationair: the Stationair airframe
    "M700": "M600",   # Piper M700 Fury: the M600 airframe
    "K100": "KODI",   # Kodiak 100: the designator that replaced KODI
    "W135": "K35R",   # WC-135R: a KC-135R conversion
    "B58T": "BE58",   # Pressurized Baron: the Baron 58 airframe
    "P28S": "P28R",   # Turbo Arrow III: the Arrow airframe
    "P337": "C337",   # pressurized Skymaster: the Skymaster airframe
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
    a = _rdp(pts[: kmax + 1], RDP_EPS)
    b = _rdp(pts[kmax:], RDP_EPS)
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


def _elliptic(y0, semi, c_root, c_min):
    """Stations for an elliptical wing (Spitfire): chord follows an ellipse
    and the quarter-chord line is straight, so the leading edge curves back a
    little and the trailing edge curves forward a lot."""
    def ch(x):
        return max(c_root * math.sqrt(max(0.0, 1 - (x / semi) ** 2)), c_min)

    def le(x):
        return y0 + 0.25 * (c_root - ch(x))

    fr = (0.0, 0.25, 0.45, 0.62, 0.76, 0.87, 0.94, 0.98, 1.0)   # ends AT the tip: the span stays true
    return [(f * semi, le(f * semi), ch(f * semi)) for f in fr], le, ch


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
    if isinstance(sp.get("stab"), dict):
        sp["stab"] = dict(sp["stab"])
    for k, v in over.items():
        if k == "stab" and isinstance(v, dict):
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
    booms = sp.get("booms")
    if booms:
        fus_end = L * booms["pod"]             # a pod, not a full-length fuselage
        tail_len = sp["tail_len"] * fus_end

    parts = [_fuselage(fus_end, w, sp["nose_len"] * sp["fus_w"], sp["nose_pow"],
                       tail_len, max(sp["tail_w"] * w, 0.45 / s))]

    c_tip = max(sp["ct"] * span, MIN_TIP / s)
    if sp.get("elliptic"):
        # The tip only keeps a legibility floor; ct plays no part.
        stations, le, ch = _elliptic(sp["wing_le"] * L, semi, sp["cr"] * span, MIN_TIP * 0.7 / s)
    else:
        stations, le, ch = _trapezoid(sp["wing_le"] * L, semi, sp["sweep"], sp["cr"] * span,
                                      c_tip, kink=sp.get("kink"), inner=sp.get("inner"))
    parts.append(_surface(stations))
    bulge = sp.get("bulge")
    if bulge:   # a hull wider than the fuselage below it (Beluga, Dreamlifter)
        y_a, y_b = bulge["at"] * L, bulge["to"] * L
        parts.append([(x, y + y_a) for x, y in
                      _fuselage(y_b - y_a, bulge["w"] * 1.08, bulge["w"] * 0.7, 0.55,
                                bulge["w"] * 1.1, w * 0.9)])   # x1.08 like the fuselage

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
        elif kind == "pusher":
            d = max(e["d"], MIN_POD * 0.8 / s)
            y_prop = min(e["at"] * L, fus_end)
            parts.append(_pod(0.0, y_prop - e["len"], e["len"], d, front=0.6, back=0.6))
            parts.append(_bar(0.0, y_prop - MIN_PROP / s, e["prop"], MIN_PROP / s))
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
        # kind "internal": nothing to draw

    tanks = sp.get("tip_tanks")
    if tanks:   # pods on the wingtips (Cessna 310, early Learjets)
        d = max(tanks["d"], MIN_POD * 0.8 / s)
        for sx in (semi - d * 0.3, -(semi - d * 0.3)):
            parts.append(_pod(sx, le(semi) + ch(semi) / 2 - tanks["len"] * 0.45, tanks["len"], d,
                              front=0.3, back=0.3, shoulder=0.9))
    canard = sp.get("canard")
    if canard:  # a small foreplane near the nose
        c_semi = canard["semi"] * span
        c_ct = max(canard["ct"] * span, MIN_TIP * 0.9 / s)
        st_c, _, _ = _trapezoid(canard["at"] * L, c_semi, canard.get("sweep", 0.0),
                                max(canard["cr"] * span, c_ct), c_ct)
        parts.append(_surface(st_c))
    if booms:
        bx = booms["eta"] * semi
        bw = max(booms["w"], MIN_POD * 0.8 / s)
        chord = max(st["cr"] * span, MIN_TIP / s)
        for sx in (bx, -bx):
            parts.append([(sx - bw / 2, le(bx)), (sx + bw / 2, le(bx)),
                          (sx + bw / 2, L), (sx - bw / 2, L)])
        # The tailplane is drawn as two halves with a gap at the centre: one
        # outline is traced, so a closed ring (wing, booms, tailplane) would
        # come out filled in.
        reach = bx * 0.80
        for sx in (1, -1):
            parts.append([(sx * bx, L - chord), (sx * (bx - reach), L - chord),
                          (sx * (bx - reach), L), (sx * bx, L)])
            parts.append(_bar(sx * (bx + bw * 0.9), L - chord, bw * 1.2, chord))

    if not booms and st["semi"]:
        parts += _tail(sp, span, L, s, w, fus_end, tail_len)
    return _finish(parts), span


def _tail(sp, span, L, s, w, fus_end, tail_len):
    """The horizontal stabilizer (and, for a T-tail, the fin top that carries it)."""
    st = sp["stab"]
    t_tail = st["pos"] == "T"
    parts = []
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
    return parts


def _finish(parts):
    """Into the 24-box: fit the parts' own bounding box to the 22-unit box
    (less the bolding), centred; then thicken and union. Normally that box is
    span by length, but a sharply swept wing can reach behind the tail."""
    xs = [x for p in parts for x, _ in p]
    ys = [y for p in parts for _, y in p]
    k = (BOX - 2 * BOLD) / max(max(xs) - min(xs), max(ys) - min(ys))
    cy = (max(ys) + min(ys)) / 2
    placed = [("p", _inflate([(12.0 + x * k, 12.0 + (y - cy) * k) for x, y in p], BOLD))
              for p in parts]
    return _merge(placed)


def _blade(cx, cy, radius, chord, deg):
    a = math.radians(deg)
    ux, uy = math.cos(a), math.sin(a)
    px, py = -uy * chord / 2, ux * chord / 2
    return [(cx + px, cy + py), (cx + ux * radius + px, cy + uy * radius + py),
            (cx + ux * radius - px, cy + uy * radius - py), (cx - px, cy - py)]


def build_heli(code, dims):
    """Helicopter: body, tail boom and the main rotor's blades, in rotor
    diameters about the mast. The blades are what make it read as a
    helicopter; the boom gives it a heading."""
    sp = spec_for(code)
    diameter, _length = dims[code]
    n = sp["blades"]
    chord = 0.052
    parts = []

    def rotor(cy, phase):
        for k in range(n):
            parts.append(_blade(0.0, cy, 0.5, chord, phase + k * 360.0 / n))
        parts.append([(0.07 * math.cos(math.radians(a)), cy + 0.07 * math.sin(math.radians(a)))
                      for a in range(0, 360, 45)])

    if sp.get("tandem"):
        gap = sp["tandem"]                      # distance between the two masts
        body = gap + 0.34
        parts.append([(x, y - gap / 2 - 0.20) for x, y in
                      _fuselage(body, sp["bw"], 0.30, 0.75, 0.05, sp["bw"] * 0.92)])
        # One blade of each rotor lies along the body; the others splay out
        # ahead of the front mast and behind the rear one, so the two rotors
        # read as two and the shape stays compact. (These angles suit three
        # blades, which every tandem type in the table has.)
        rotor(-gap / 2, 90)
        rotor(gap / 2, 270)
    else:
        cabin = sp["nose"] + sp["rear"]
        parts.append([(x, y - sp["nose"]) for x, y in
                      _fuselage(cabin + 0.16, sp["bw"], cabin * 0.50, 0.55, 0.26, 0.05)])
        parts.append(_bar(0.0, sp["rear"] * 0.5, 0.050, sp["tail"] - sp["rear"] * 0.5))
        if sp["tailplane"]:
            parts.append(_bar(0.0, sp["tail"] - 0.13, sp["tailplane"], 0.050))
        parts.append(_bar(0.0, sp["tail"] - 0.05, 0.085, 0.09))     # fin and tail rotor
        # Two blades straight across would read as a wing; set them diagonal.
        rotor(0.0, 35.0 if n == 2 else 90 + 180.0 / n)

    return _finish(parts), diameter


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


def build_tiltrotor(code, dims):
    """Tiltrotor: a fuselage, a short straight wing with a nacelle and a
    rotor at each tip, and a tailplane, drawn with the rotors up. In metres
    from the nose. The first dimension is the width ACROSS THE TURNING
    ROTORS, not the wing's span."""
    sp = spec_for(code)
    width, L = dims[code]
    r = sp["rotor"] / 2
    x_mast = width / 2 - r                 # rotor centres sit at the wingtips
    w = sp["fus_w"]
    if x_mast - r < w / 2:
        raise RuntimeError(f"{code}: rotors would overlap the fuselage: its first dimension"
                           " must be the width across the turning rotors")
    y_wing = sp["wing_le"] * L
    parts = [_fuselage(L, w, 1.6 * w, 0.6, 3.0 * w, 0.3 * w),
             _bar(0.0, y_wing, 2 * x_mast, sp["chord"]),
             _bar(0.0, L - sp["stab"]["cr"], 2 * sp["stab"]["semi"], sp["stab"]["cr"])]
    y_mast = y_wing + sp["chord"] * 0.45
    n = sp["blades"]
    for side in (1, -1):
        cx = side * x_mast
        parts.append(_pod(cx, y_mast - 1.3, 2.6, 1.3, front=0.5, back=0.5))
        for k in range(n):
            deg = 90.0 / n + k * 360.0 / n          # the left rotor mirrors the right
            parts.append(_blade(cx, y_mast, r, 0.62, deg if side > 0 else 180.0 - deg))
    return _finish(parts), width


BUILDERS = {"B2": build_flying_wing}


def is_rotorcraft(code):
    """True for a row drawn with rotors (helicopters, the tiltrotor): the
    marker code needs to know, since these report the rotorcraft category."""
    return PRESETS[SOURCES[code][0]].get("builder") in ("heli", "tilt")


def build(code, dims):
    if code in BUILDERS:
        return BUILDERS[code](code, dims)
    builder = PRESETS[SOURCES[code][0]].get("builder")
    if builder == "heli":
        return build_heli(code, dims)
    if builder == "tilt":
        return build_tiltrotor(code, dims)
    return build_source(code, dims)


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
    display board via one JS constant (a few hundred KB) — one request per page, no per-
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
        # The rotorcraft among them, so the marker code can tell a helicopter
        # silhouette from a fixed-wing one (see _radarDrawsShape in index.html).
        + "const SHAPES_ROTORCRAFT = new Set("
        + json.dumps(sorted(k for k in list(paths) + [a for a, t in ALIASES.items() if t in paths]
                            if is_rotorcraft(ALIASES.get(k, k))))
        + ");\n"
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
