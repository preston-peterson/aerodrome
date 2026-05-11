# Version: 2.79.0
"""distance.py — great-circle distance and bearing helpers.

Extracted in v2.79.0 from inline definitions that lived in server.py
and collector.py. Pre-v2.79.0 the same haversine math was duplicated
across four sites with two slightly different shapes:

  - server.py:_haversine   — km-only, null-safe (`if any(v is None: return None`)
  - server.py local x2     — multi-unit, no null-safety (callers gated)
  - collector.py:_haversine — multi-unit, no null-safety

Centralizing here:
  - One canonical multi-unit haversine that defaults to km (the canonical
    storage unit per the v2.60.1 design — `seen_aircraft.last_distance`
    column stores km, frontend converts to user-unit at render time).
  - Null-safe by default (returns None if any coordinate is None).
  - Multi-unit conversion via `to_user_unit(km, unit)` for the common
    "we have km, render in user-unit" path.
  - `compass_bearing` for the few sites that need it (drill panel rose
    enrichment).

Math constants:
  - Earth radius assumed spherical at 6371 km. Sufficient for receiver-
    range distances (~200 mi). Not WGS84-ellipsoid; we don't need that
    precision and the simpler model lets us skip pyproj.
  - Unit conversions:
      1 km = 0.621371 mi
      1 km = 0.539957 nmi

These are the values the pre-v2.79.0 inline definitions used; centralizing
preserves the exact same numerical output to avoid surprising any
existing card or sort path.
"""

import math
from typing import Optional


# Earth radius in km. Used by haversine() when unit is "km" (default).
# Other units derive via the conversion factors below.
EARTH_RADIUS_KM = 6371.0

# Conversion factors from km to other units. Numerical values match
# what the pre-v2.79.0 inline definitions used:
#   - 0.621371 mi/km  (so 1 mi ≈ 1.609344 km)
#   - 0.539957 nmi/km (so 1 nmi ≈ 1.852 km)
_KM_TO_MI = 0.621371
_KM_TO_NMI = 0.539957

# The legacy multi-unit haversine in collector.py and server.py used
# precomputed unit-radius constants instead of conversion factors:
#   {"mi": 3958.8, "nmi": 3440.065, "km": 6371.0}
# That's mathematically equivalent to (EARTH_RADIUS_KM * conversion factor)
# for the formula's final multiplication step, but keeping the constants
# here would let unit-radius drift from km-conversion-factor and silently
# produce slightly different numbers. distance.py uses km internally and
# converts at the end, so there's exactly one source of truth for each
# unit's relationship to km.


def haversine(lat1: Optional[float], lon1: Optional[float],
              lat2: Optional[float], lon2: Optional[float],
              unit: str = "km") -> Optional[float]:
    """Great-circle distance between two points.

    Args:
      lat1, lon1, lat2, lon2: coordinates in degrees. Any None → returns None.
      unit: output unit. "km" (default), "mi", or "nmi". Unknown values
            fall back to "km" rather than raising — the caller's intent
            is always "give me a distance"; an unknown unit is a config
            error that shouldn't crash a poll loop.

    Returns:
      Distance as a float, or None if any coordinate is None.

    The math is the standard spherical haversine — sufficient for receiver-
    range distances (~200 mi). Not WGS84-ellipsoid precision. The result
    is NOT rounded; callers that want display rounding should round
    themselves (typically `round(d, 1)` for one decimal place to match
    the v2.60.x display convention).
    """
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    km = 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
    return _km_to_unit(km, unit)


def to_user_unit(km: Optional[float], unit: str) -> Optional[float]:
    """Convert a stored km value to the user's display unit.

    Args:
      km: the stored canonical distance in km, or None.
      unit: target unit ("km", "mi", or "nmi"). Unknown → falls back
            to "mi" to match the legacy v2.60.1 default behavior.

    Returns:
      The converted distance rounded to one decimal, or None if km is None.

    This is the function the v2.60.1 server.py:_distance_km_to_user_unit
    was named to emphasize: the DB stores km canonically, response
    annotation converts to user-unit at the boundary. Mirrors the
    legacy behavior (one decimal place, mi default) exactly.
    """
    if km is None:
        return None
    u = (unit or "mi").lower()
    if u == "km":
        return round(km, 1)
    if u == "nmi":
        return round(km * _KM_TO_NMI, 1)
    # Default to miles. Includes "mi" and any unrecognized value —
    # matches the legacy inline definitions' fallback semantics.
    return round(km * _KM_TO_MI, 1)


def compass_bearing(lat1: float, lon1: float,
                    lat2: float, lon2: float) -> float:
    """Initial compass bearing from (lat1, lon1) toward (lat2, lon2).

    Returns degrees in [0, 360) where 0=N, 90=E, 180=S, 270=W. Standard
    forward-azimuth great-circle formula.

    Used by the v2.41.17 drill-panel enrichment — the Option C panel
    shows the compass direction each aircraft was in at its record-
    setting sighting. The result is NOT rounded; callers that want
    integer degrees should round themselves.

    Coordinates are required (not Optional) — bearing only makes sense
    when both points exist. Callers gate before calling, mirroring the
    pre-v2.79.0 inline definition.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    brg = math.degrees(math.atan2(y, x))
    return (brg + 360) % 360  # normalise to [0, 360)


def _km_to_unit(km: float, unit: str) -> float:
    """Internal helper — convert a km value to the target unit, no rounding.

    Used by haversine() to apply the unit conversion at the end of the
    distance computation. Separate from to_user_unit() because that
    function rounds (display-oriented) while this one preserves the
    raw float value (computation-oriented; some callers feed the
    result into further math like distance buckets).
    """
    u = (unit or "km").lower()
    if u == "mi":
        return km * _KM_TO_MI
    if u == "nmi":
        return km * _KM_TO_NMI
    # Default to km. Includes "km" and any unrecognized value.
    return km
