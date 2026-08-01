"""
Lazy callsign → flight-route resolver (route enrichment, v3.4.99;
re-sourced to adsb.lol in v3.4.109).

ADS-B does not broadcast a route — origin/destination come from a
callsign→route database. This resolver looks a callsign up against
adsb.lol's VRS standing-data mirror (free, no API key) and caches the
result in the `route_cache` table (callsign-keyed; schema migrations
v12 + v14), with a NEGATIVE-cache marker for callsigns with no scheduled
route (GA/private, 404s) so the same dead callsign isn't re-queried on
every view.

SOURCE HISTORY: v3.4.99 used adsbdb.com, whose data proved ~68% unreliable
against an independent cross-check (display pulled in v3.4.105). adsb.lol's
data matched aircraft's actual positions ~4× better in the head-to-head
validation (2026-06-17), and — unlike adsbdb — it stores MULTI-LEG
itineraries ("KMSP-KPHL-KMSP": one flight number flying an out-and-back),
which is precisely the case single-pair databases get wrong. adsb.lol's
`/api/0/route/{callsign}` endpoint is deprecated — it 302-redirects to the
static per-callsign JSON at vrs-standing-data.adsb.lol, so we fetch that
directly (no redirect following; 404 = clean miss).

A multi-leg chain means "this callsign is known to fly these airports in
this order" — it can't say which leg the plane overhead is on. That's what
pick_current_leg() answers: the plane's live position against each leg's
great-circle path (the same cross-track math the June validation used).

Mirrors the hexdb owner resolver's caching discipline (collector.py): a
TTL'd positive entry + a shorter-lived negative entry, and transient network
errors are NOT cached so they retry. DIFFERENCE: this is FETCH-ON-DEMAND
(lazy v1) — the /api/callsign/{cs}/route endpoint calls resolve_route() when
a user opens an aircraft. There is no background worker yet; a later phase
could add one (à la hexdb_resolver) to pre-resolve every live callsign.
"""
import json
import logging
import math
import re
import time
from typing import List, Optional

import requests

from designators import airline_name
from distance import haversine

logger = logging.getLogger(__name__)

# The VRS standing-data mirror shards routes by the callsign's first two
# characters: /routes/DA/DAL2688.json. _CALLSIGN_RE guarantees ≥2 chars, so
# the prefix always exists.
_ROUTE_URL = "https://vrs-standing-data.adsb.lol/routes/{prefix}/{cs}.json"
_TIMEOUT_SEC = 6

# Routes are near-static per callsign but shift with seasonal schedules, so a
# known route is re-resolved at most monthly. Misses expire sooner because
# the dataset grows (a callsign unknown today may be known next week) —
# the same positive/negative split the hexdb owner resolver uses.
ROUTE_POSITIVE_TTL_DAYS = 30
ROUTE_NEGATIVE_TTL_DAYS = 7

# Callsigns are the ICAO form: 2-8 uppercase alphanumerics. Validating here
# keeps a hostile/garbage callsign out of the URL — the host is hardcoded,
# so this charset cap closes the only caller-influenced part of the request.
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")

# Current-leg inference: legs whose path-distance is within this of the best
# are all plausible from position alone — an out-and-back's two legs are the
# SAME path in opposite directions (identical distances), and a plane on the
# ground sits equally near every leg touching that airport. Those ties fall
# to the heading tie-break; with no heading (or no clear winner) the UI shows
# the full chain rather than guess.
LEG_AMBIGUITY_KM = 25.0

# If the plane isn't within this of ANY leg's path, the route data doesn't
# describe where it actually is — don't pick a "current leg" from it.
LEG_MAX_XTRACK_KM = 150.0

# Heading tie-break: alignment is cos(track − leg bearing), 1 = flying along
# the leg, −1 = flying it backwards. The winner must be meaningfully aligned
# AND meaningfully better than the runner-up (opposite legs score ±x, so an
# out-and-back separates cleanly; two near-parallel legs won't, and stay
# ambiguous).
LEG_ALIGN_MIN = 0.2
LEG_ALIGN_MARGIN = 0.5

# Observability — same spirit as the hexdb resolver's stats counters.
_stats = {"attempts": 0, "hits": 0, "misses": 0, "errors": 0, "last_error": None}


def resolver_stats() -> dict:
    """Snapshot for diagnostics."""
    return dict(_stats)


def _norm(callsign: Optional[str]) -> str:
    return (callsign or "").strip().upper()


def _miss(callsign: str, cached: bool) -> dict:
    return {"ok": True, "found": False, "cached": cached, "callsign": callsign}


def _fetch(cs: str) -> Optional[dict]:
    """Fetch one callsign's route JSON. Returns a route dict on a hit, None
    on a clean miss (404 / unusable payload). RAISES on a transient error
    (timeout, 5xx, 429, network) so the caller does NOT negative-cache a
    blip."""
    _stats["attempts"] += 1
    url = _ROUTE_URL.format(prefix=cs[:2], cs=cs)  # cs is _CALLSIGN_RE-validated → URL-safe
    resp = requests.get(
        url, timeout=_TIMEOUT_SEC,
        headers={"User-Agent": "aerodrome-route/2.0"},
        allow_redirects=False,        # hardcoded host; don't follow a redirect
    )
    if resp.status_code == 404:
        return None                   # unknown callsign — a real miss
    resp.raise_for_status()           # any other non-200 → transient, raise
    data = resp.json()
    if not isinstance(data, dict):
        return None
    codes = [c.strip().upper() for c in (data.get("airport_codes") or "").split("-")
             if c.strip()]
    if len(codes) < 2:
        return None                   # no usable itinerary → a real miss
    # _airports is ordered to match airport_codes (a repeated airport appears
    # once per position). If the payload disagrees on length, fall back to
    # codes-only entries rather than mis-aligning names to the wrong stops.
    raw_airports = data.get("_airports") or []
    aligned = (len(raw_airports) == len(codes))
    airports = []
    for i, code in enumerate(codes):
        ap = raw_airports[i] if aligned and isinstance(raw_airports[i], dict) else {}
        airports.append({
            "icao": code,
            "name": (ap.get("location") or ap.get("name") or "").strip() or None,
            "lat":  ap.get("lat"),
            "lon":  ap.get("lon"),
        })
    return {
        "airports": airports,
        # Stored as the display NAME (via the local designator table), not the
        # raw code — the UI shows names, and old cache rows already hold names.
        # An unrecognized code stores None: better silent than a cryptic "DAL".
        "airline": airline_name((data.get("airline_code") or "").strip()),
    }


def pick_current_leg(airports: List[dict], lat: Optional[float],
                     lon: Optional[float],
                     track: Optional[float] = None) -> Optional[int]:
    """Which leg of a multi-airport chain is a plane at (lat, lon), heading
    `track` degrees, actually flying? Returns the leg index i (the leg
    airports[i] → airports[i+1]), or None when it can't be answered
    confidently — the caller shows the full chain instead.

    Method: great-circle cross-track distance from the plane to each leg's
    path (clamped to the segment — a plane beyond an endpoint scores its
    distance to that endpoint), the same math the 2026-06-17 source
    validation used to prove adsb.lol's accuracy. Position alone decides
    when one leg is clearly closest. When several legs are equally close —
    an out-and-back's two legs are the SAME path flown in opposite
    directions, so this is the NORMAL multi-leg case, not a corner — the
    plane's heading breaks the tie: the leg whose bearing it's actually
    flying along wins (outbound vs return differ by ~180°).

    Returns None when: no position, any airport lacks coordinates, the
    plane isn't near any leg's path (LEG_MAX_XTRACK_KM — bad route data),
    or the positional tie can't be broken by heading (no track given, plane
    on the ground at a shared airport pointing nowhere useful, near-parallel
    legs). A single-leg chain trivially returns 0.
    """
    if lat is None or lon is None or len(airports) < 2:
        return None
    if len(airports) == 2:
        return 0
    if any(a.get("lat") is None or a.get("lon") is None for a in airports):
        return None
    dists = []
    for i in range(len(airports) - 1):
        a, b = airports[i], airports[i + 1]
        dists.append(_xtrack_km(lat, lon, a["lat"], a["lon"], b["lat"], b["lon"]))
    best = min(dists)
    if best > LEG_MAX_XTRACK_KM:
        return None
    candidates = [i for i, d in enumerate(dists) if d - best < LEG_AMBIGUITY_KM]
    if len(candidates) == 1:
        return candidates[0]
    if track is None:
        return None
    # Heading tie-break among the positionally-plausible legs.
    tr = math.radians(track)
    aligns = []
    for i in candidates:
        a, b = airports[i], airports[i + 1]
        brg = _bearing_rad(a["lat"], a["lon"], b["lat"], b["lon"])
        aligns.append((math.cos(tr - brg), i))
    aligns.sort(reverse=True)
    if aligns[0][0] < LEG_ALIGN_MIN:
        return None
    if len(aligns) > 1 and (aligns[0][0] - aligns[1][0]) < LEG_ALIGN_MARGIN:
        return None
    return aligns[0][1]


def _xtrack_km(plat: float, plon: float, alat: float, alon: float,
               blat: float, blon: float) -> float:
    """Distance (km) from point P to the great-circle SEGMENT A→B: the
    cross-track distance when P projects onto the segment, else the distance
    to the nearer endpoint. Standard spherical formulas on the same
    EARTH_RADIUS the shared haversine uses (via the d13/haversine terms)."""
    d_pa = haversine(plat, plon, alat, alon) or 0.0
    d_pb = haversine(plat, plon, blat, blon) or 0.0
    d_ab = haversine(alat, alon, blat, blon) or 0.0
    if d_ab == 0.0:
        return d_pa
    # Angular distances/bearings for the cross-track term. R cancels out of
    # the bearing math; reuse haversine's km outputs with a nominal R.
    R = 6371.0
    delta13 = d_pa / R
    theta13 = _bearing_rad(alat, alon, plat, plon)
    theta12 = _bearing_rad(alat, alon, blat, blon)
    dxt = math.asin(max(-1.0, min(1.0, math.sin(delta13) * math.sin(theta13 - theta12)))) * R
    # Along-track: how far along A→B the projection of P falls.
    cos_dxt = math.cos(abs(dxt) / R)
    if cos_dxt == 0.0:
        return min(d_pa, d_pb)
    ratio = max(-1.0, min(1.0, math.cos(delta13) / cos_dxt))
    dat = math.acos(ratio) * R
    behind_a = math.cos(theta13 - theta12) < 0   # projection falls before A
    if behind_a:
        return d_pa
    if dat > d_ab:                               # projection falls past B
        return d_pb
    return abs(dxt)


def _bearing_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in radians."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return math.atan2(y, x)


def _hit_dict(cs: str, cached: bool, airports: List[dict],
              airline: Optional[str]) -> dict:
    """Assemble the resolve_route() hit shape. origin/dest are the chain's
    first/last so single-leg consumers read exactly what they always did."""
    first, last = airports[0], airports[-1]
    return {"ok": True, "found": True, "cached": cached, "callsign": cs,
            "origin_icao": first["icao"], "origin_name": first["name"] or "",
            "dest_icao": last["icao"], "dest_name": last["name"] or "",
            "airline": airline or "", "airports": airports}


def resolve_route(conn, callsign: str) -> dict:
    """Cache-first callsign→route resolve. Returns:
        {ok, found, cached, callsign[, origin_icao, origin_name,
         dest_icao, dest_name, airline, airports]}
    `airports` is the FULL ordered chain [{icao, name, lat, lon}, ...] —
    2 entries for a plain A→B route, more for a multi-leg flight number.
    Never raises: a transient fetch error returns found=False, cached=False
    and writes NOTHING (so it retries next view). A clean 'no route' is
    negative-cached. Caller provides an open, writable sqlite3 connection."""
    cs = _norm(callsign)
    if not _CALLSIGN_RE.match(cs):
        return _miss(cs, cached=False)
    now = int(time.time())

    row = conn.execute(
        "SELECT origin_icao, origin_name, dest_icao, dest_name, airline, "
        "airports_json, resolved_at, last_outcome FROM route_cache "
        "WHERE callsign = ?",
        (cs,)).fetchone()
    if row is not None:
        oi, on_, di, dn, al, apj, resolved_at, outcome = row
        ttl = (ROUTE_POSITIVE_TTL_DAYS if outcome == "hit"
               else ROUTE_NEGATIVE_TTL_DAYS) * 86400
        if (now - (resolved_at or 0)) < ttl:
            conn.execute(
                "UPDATE route_cache SET hit_count = hit_count + 1 WHERE callsign = ?",
                (cs,))
            conn.commit()
            if outcome == "hit":
                try:
                    airports = json.loads(apj) if apj else None
                except (TypeError, ValueError):
                    airports = None
                if not airports:
                    # Pre-v14 row shape (shouldn't survive the v14 cache
                    # clear, but stay defensive): rebuild a 2-stop chain
                    # from the origin/dest columns, no coordinates.
                    airports = [{"icao": oi, "name": on_ or None, "lat": None, "lon": None},
                                {"icao": di, "name": dn or None, "lat": None, "lon": None}]
                return _hit_dict(cs, True, airports, al)
            return _miss(cs, cached=True)
        # stale → fall through and re-fetch

    try:
        route = _fetch(cs)
    except Exception as e:
        _stats["errors"] += 1
        _stats["last_error"] = repr(e)
        logger.info(f"route resolve {cs}: transient error, not cached: {e}")
        return _miss(cs, cached=False)

    if route is None:
        _stats["misses"] += 1
        conn.execute(
            "INSERT INTO route_cache "
            "(callsign, origin_icao, origin_name, dest_icao, dest_name, airline, "
            " airports_json, resolved_at, last_outcome, hit_count) "
            "VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL, ?, 'miss', 0) "
            "ON CONFLICT(callsign) DO UPDATE SET "
            "  origin_icao=NULL, origin_name=NULL, dest_icao=NULL, dest_name=NULL, "
            "  airline=NULL, airports_json=NULL, "
            "  resolved_at=excluded.resolved_at, last_outcome='miss'",
            (cs, now))
        conn.commit()
        return _miss(cs, cached=False)

    _stats["hits"] += 1
    airports = route["airports"]
    first, last = airports[0], airports[-1]
    conn.execute(
        "INSERT INTO route_cache "
        "(callsign, origin_icao, origin_name, dest_icao, dest_name, airline, "
        " airports_json, resolved_at, last_outcome, hit_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'hit', 0) "
        "ON CONFLICT(callsign) DO UPDATE SET "
        "  origin_icao=excluded.origin_icao, origin_name=excluded.origin_name, "
        "  dest_icao=excluded.dest_icao, dest_name=excluded.dest_name, "
        "  airline=excluded.airline, airports_json=excluded.airports_json, "
        "  resolved_at=excluded.resolved_at, last_outcome='hit'",
        (cs, first["icao"], first["name"] or "", last["icao"], last["name"] or "",
         route["airline"], json.dumps(airports), now))
    conn.commit()
    return _hit_dict(cs, False, airports, route["airline"])
