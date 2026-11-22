"""One aircraft's stored positions, split into flights (3.4.198).

The aircraft page lists these so one flight can be drawn on its own and played
back. A flight is the positions between two gaps that end one:

  - out of range for more than OUT_OF_RANGE seconds, wherever it was; or
  - out of range for more than LOW_GAP seconds when it was below LOW_FT (or
    reporting itself on the ground) just before or just after: it landed.

Measured on a real receiver's 30 days (67 aircraft, 61,200 positions): an
airliner at the edge of coverage can vanish for 14-24 minutes at cruise and
come back 100-170 nm further on, still the same flight; a light aircraft last
seen low and back 15-30 minutes later has landed and left again. A flat
10-minute rule cut 19 flights in two; a flat 30-minute rule merged 26 pairs;
this rule cut 1 and merged none. Splitting at detected landings instead merged
23 pairs, because many stops are at fields the takeoff/landing detection
doesn't cover, so those events label a flight rather than split it.

Standard library + airport_events only.
"""
import sqlite3
from collections import Counter

import airport_events

OUT_OF_RANGE = 1800   # s out of range always ends a flight
LOW_GAP = 600         # s out of range ends one when it was low on either side
LOW_FT = 2500         # below this (or no altitude: on the ground) is low
LOOKBACK = 6 * 3600   # s read before a window, so its first flight starts where it began
MAX_FLIGHTS = 500     # newest first; older ones are dropped and `truncated` says so
EVENT_SLACK = 120     # s either side of a flight that its takeoff/landing may fall in


def _low(altitude):
    return altitude is None or altitude < LOW_FT


def ends_between(prev, nxt):
    """Whether a flight ends between two consecutive (seen_at, altitude) fixes."""
    gap = nxt[0] - prev[0]
    return gap > OUT_OF_RANGE or (gap > LOW_GAP and (_low(prev[1]) or _low(nxt[1])))


def split(fixes):
    """Time-ordered (seen_at, altitude[, callsign]) fixes -> [(first, last)] index
    ranges, one per flight, oldest first."""
    out = []
    first = 0
    for i in range(1, len(fixes)):
        if ends_between(fixes[i - 1], fixes[i]):
            out.append((first, i - 1))
            first = i
    if fixes:
        out.append((first, len(fixes) - 1))
    return out


def aircraft_flights(conn, config, icao, since=None):
    """The aircraft's flights that end at or after `since` (None: all kept),
    newest first: {"flights": [...], "truncated": bool}. Each flight:
    start, end, fixes, callsign (its most used), max_alt, longest_gap (s),
    and its takeoffs/landings (touch-and-goes folded as on the aircraft page)."""
    lo = 0 if since is None else max(0, int(since) - LOOKBACK)
    rows = conn.execute(
        "SELECT seen_at, altitude, callsign FROM all_sightings "
        "WHERE icao = ? AND seen_at >= ? AND lat IS NOT NULL AND lon IS NOT NULL "
        "ORDER BY seen_at", (icao, lo)).fetchall()
    rows = [(t, a if isinstance(a, (int, float)) else None, c) for t, a, c in rows]
    flights = []
    for first, last in split(rows):
        part = rows[first:last + 1]
        if since is not None and part[-1][0] < since:
            continue
        signs = Counter((c or "").strip() for _, _, c in part if (c or "").strip())
        alts = [a for _, a, _ in part if a is not None]
        flights.append({
            "start": part[0][0], "end": part[-1][0], "fixes": len(part),
            "callsign": signs.most_common(1)[0][0] if signs else "",
            "max_alt": max(alts) if alts else None,
            "longest_gap": max((b[0] - a[0] for a, b in zip(part, part[1:])), default=0),
            "events": [],
        })
    flights.reverse()
    truncated = len(flights) > MAX_FLIGHTS
    flights = flights[:MAX_FLIGHTS]
    if flights:
        _attach_events(conn, config, icao, flights)
    return {"flights": flights, "truncated": truncated}


def _attach_events(conn, config, icao, flights):
    """Give each flight (newest first) the takeoffs and landings inside it."""
    oldest = flights[-1]["start"] - EVENT_SLACK
    try:
        rows = conn.execute("SELECT kind, airport, runway, at, callsign FROM airport_events "
                            "WHERE icao = ? AND at >= ? ORDER BY at DESC", (icao, oldest)).fetchall()
    except sqlite3.OperationalError as e:  # no airport_events table yet
        if "no such table" in str(e):
            return
        raise
    names = airport_events._names(config) if rows else {}
    for item in airport_events._group(rows):
        for f in flights:
            if f["start"] - EVENT_SLACK <= item["at"] <= f["end"] + EVENT_SLACK:
                item.pop("callsign", None)
                item["name"] = names.get(item["airport"], "")
                f["events"].insert(0, item)    # oldest first within a flight
                break
