"""Airports for the maps' airport layer (Live radar and display board).

static/airports.json is a trimmed copy of OurAirports' data (public domain),
rebuilt by scripts/build_airports.py: major (large), regional (medium) and
small fields with a paved runway of 3,000 ft or more, each with its code,
position and runway end points.

The maps never load the whole list. near() reads it once per receiver
position, keeps the airports within RADIUS_MI (wider than any receiver's
reach) and holds only those, so a Pi keeps a few hundred entries in memory,
not eleven thousand. Standard library + distance.py only.
"""
import json
import threading
from pathlib import Path

from distance import haversine

DATA = Path(__file__).resolve().parent / "static" / "airports.json"
RADIUS_MI = 400
CATEGORIES = {"l": "major", "m": "regional", "s": "small"}

_lock = threading.Lock()
_cache = {"key": None, "airports": None}


def _within(rows, lat, lon, radius_mi):
    dlat = radius_mi / 69.0
    out = []
    for code, cat, alat, alon, runways in rows:
        if cat not in CATEGORIES or abs(alat - lat) > dlat:
            continue
        d = haversine(lat, lon, alat, alon, unit="mi")
        if d <= radius_mi:
            out.append((d, {"code": code, "cat": CATEGORIES[cat], "lat": alat, "lon": alon,
                            "runways": runways}))
    out.sort(key=lambda t: t[0])
    return [a for _, a in out]


def near(lat, lon, radius_mi=RADIUS_MI, path=None):
    """The airports within radius_mi of (lat, lon), nearest first.

    Raises OSError, ValueError, KeyError or TypeError when the data file is
    missing or malformed; the caller reports that instead of drawing nothing
    silently."""
    path = path or DATA
    key = (round(float(lat), 4), round(float(lon), 4), radius_mi, str(path))
    with _lock:
        if _cache["key"] == key:
            return _cache["airports"]
        rows = json.loads(Path(path).read_text(encoding="utf-8"))["airports"]
        found = _within(rows, float(lat), float(lon), radius_mi)
        _cache["key"], _cache["airports"] = key, found
        return found
