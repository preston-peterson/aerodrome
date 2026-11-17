"""Airports for the maps' airport layer (Live radar and display board).

static/airports.json is a trimmed copy of OurAirports' data (public domain),
rebuilt by scripts/build_airports.py: major (large), regional (medium) and
small fields with a paved runway of 3,000 ft or more, each with its code,
position, runways (end points and names), field elevation, name, IATA code and
city. The
takeoff and landing detector (airport_events.py) reads the same list.

The maps never load the whole list. near() reads it once per receiver
position, keeps the airports within RADIUS_MI (wider than any receiver's
reach) and holds only those, so a Pi keeps a few hundred entries in memory,
not eleven thousand. details() does the same with static/airport-info.json,
the runways, radio and links only the airport page shows. Standard library +
distance.py only.
"""
import json
import threading
from pathlib import Path

from distance import haversine

DATA = Path(__file__).resolve().parent / "static" / "airports.json"
INFO = DATA.with_name("airport-info.json")      # runways, radio, links: the airport page only
RADIUS_MI = 400
CATEGORIES = {"l": "major", "m": "regional", "s": "small"}

_lock = threading.Lock()
_cache = {"key": None, "airports": None}
_info_cache = {"key": None, "rows": None}


def _within(rows, lat, lon, radius_mi):
    dlat = radius_mi / 69.0
    out = []
    for row in rows:
        code, cat, alat, alon, runways = row[:5]
        if cat not in CATEGORIES or abs(alat - lat) > dlat:
            continue
        d = haversine(lat, lon, alat, alon, unit="mi")
        if d <= radius_mi:
            # Since 3.4.184 a row also carries the field elevation (ft) and the
            # airport's name, and each runway its two ends' names (13L, 31R);
            # since 3.4.186 its IATA code (DFW, or "") and city.
            out.append((d, {"code": code, "cat": CATEGORIES[cat], "lat": alat, "lon": alon,
                            "runways": runways,
                            "elev": row[5] if len(row) > 5 else None,
                            "name": row[6] if len(row) > 6 else "",
                            "iata": row[7] if len(row) > 7 else "",
                            "city": row[8] if len(row) > 8 else ""}))
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


def details(code, nearby, path=None):
    """What the airport page shows about `code` beyond the map, from
    static/airport-info.json (built with airports.json): its runways
    ([name, length_ft, width_ft, surface, lit]), radio ([label, "118.300, 120.750"]),
    website and Wikipedia links, whether the FAA publishes plates for it, and the
    code the FAA and SkyVector use for it (DFW for KDFW). None when the file has
    nothing for it or can't be read: the page then leaves those parts out.

    Like near(), it keeps only the airports in `nearby` (the codes near the
    receiver), reading the file again only when that set changes."""
    path = path or INFO
    key = (frozenset(nearby), str(path))
    with _lock:
        if _info_cache["key"] != key:
            try:
                rows = json.loads(Path(path).read_text(encoding="utf-8"))["airports"]
                kept = {r[0]: r for r in rows if r[0] in key[0]}
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                kept = {}
            _info_cache["key"], _info_cache["rows"] = key, kept
        row = _info_cache["rows"].get(code)
    if not row:
        return None
    lid, plates, website, wikipedia, runways, radio = row[1:7]
    return {"ident": lid or code, "faa_plates": bool(plates), "website": website,
            "wikipedia": wikipedia, "runways": runways, "radio": radio}
