"""
Lazy callsign → flight-route resolver (route enrichment, v3.4.99).

ADS-B does not broadcast a route — origin/destination come from a
callsign→route database. This resolver looks a callsign up against
adsbdb.com (free, no API key) and caches the result in the `route_cache`
table (callsign-keyed; schema migration v12), with a NEGATIVE-cache marker
for callsigns with no scheduled route (GA/private, adsbdb 404s) so the same
dead callsign isn't re-queried on every view.

Mirrors the hexdb owner resolver's caching discipline (collector.py): a
TTL'd positive entry + a shorter-lived negative entry, and transient network
errors are NOT cached so they retry. DIFFERENCE: this is FETCH-ON-DEMAND
(lazy v1) — the /api/callsign/{cs}/route endpoint calls resolve_route() when
a user opens an aircraft. There is no background worker yet; a later phase
could add one (à la hexdb_resolver) to pre-resolve every live callsign.
"""
import logging
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{cs}"
_TIMEOUT_SEC = 6

# Routes are near-static per callsign but shift with seasonal schedules, so a
# known route is re-resolved at most monthly. Misses expire sooner because
# adsbdb's dataset grows (a callsign unknown today may be known next week) —
# the same positive/negative split the hexdb owner resolver uses.
ROUTE_POSITIVE_TTL_DAYS = 30
ROUTE_NEGATIVE_TTL_DAYS = 7

# adsbdb callsigns are the ICAO form: 2-8 uppercase alphanumerics. Validating
# here keeps a hostile/garbage callsign out of the URL — the host is hardcoded,
# so this charset cap closes the only caller-influenced part of the request.
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")

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
    """Fetch one callsign from adsbdb. Returns a route dict on a hit, None on a
    clean miss (404 / no flightroute). RAISES on a transient error (timeout,
    5xx, 429, network) so the caller does NOT negative-cache a blip."""
    _stats["attempts"] += 1
    url = _ADSBDB_URL.format(cs=cs)   # cs is _CALLSIGN_RE-validated → URL-safe
    resp = requests.get(
        url, timeout=_TIMEOUT_SEC,
        headers={"User-Agent": "aerodrome-route/1.0"},
        allow_redirects=False,        # hardcoded host; don't follow a redirect
    )
    if resp.status_code == 404:
        return None                   # adsbdb's "unknown callsign" — a real miss
    resp.raise_for_status()           # any other non-200 → transient, raise
    fr = (resp.json().get("response") or {})
    fr = fr.get("flightroute") if isinstance(fr, dict) else None
    if not fr:
        return None
    o = fr.get("origin") or {}
    d = fr.get("destination") or {}
    return {
        "origin_icao": (o.get("icao_code") or "").strip(),
        "origin_name": (o.get("municipality") or o.get("name") or "").strip(),
        "dest_icao":   (d.get("icao_code") or "").strip(),
        "dest_name":   (d.get("municipality") or d.get("name") or "").strip(),
        "airline":     ((fr.get("airline") or {}).get("name") or "").strip(),
    }


def resolve_route(conn, callsign: str) -> dict:
    """Cache-first callsign→route resolve. Returns:
        {ok, found, cached, callsign[, origin_icao, origin_name,
         dest_icao, dest_name, airline]}
    Never raises: a transient fetch error returns found=False, cached=False and
    writes NOTHING (so it retries next view). A clean 'no route' is negative-
    cached. Caller provides an open, writable sqlite3 connection."""
    cs = _norm(callsign)
    if not _CALLSIGN_RE.match(cs):
        return _miss(cs, cached=False)
    now = int(time.time())

    row = conn.execute(
        "SELECT origin_icao, origin_name, dest_icao, dest_name, airline, "
        "resolved_at, last_outcome FROM route_cache WHERE callsign = ?",
        (cs,)).fetchone()
    if row is not None:
        oi, on_, di, dn, al, resolved_at, outcome = row
        ttl = (ROUTE_POSITIVE_TTL_DAYS if outcome == "hit"
               else ROUTE_NEGATIVE_TTL_DAYS) * 86400
        if (now - (resolved_at or 0)) < ttl:
            conn.execute(
                "UPDATE route_cache SET hit_count = hit_count + 1 WHERE callsign = ?",
                (cs,))
            conn.commit()
            if outcome == "hit":
                return {"ok": True, "found": True, "cached": True, "callsign": cs,
                        "origin_icao": oi, "origin_name": on_,
                        "dest_icao": di, "dest_name": dn, "airline": al}
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
            " resolved_at, last_outcome, hit_count) "
            "VALUES (?, NULL, NULL, NULL, NULL, NULL, ?, 'miss', 0) "
            "ON CONFLICT(callsign) DO UPDATE SET "
            "  origin_icao=NULL, origin_name=NULL, dest_icao=NULL, dest_name=NULL, "
            "  airline=NULL, resolved_at=excluded.resolved_at, last_outcome='miss'",
            (cs, now))
        conn.commit()
        return _miss(cs, cached=False)

    _stats["hits"] += 1
    conn.execute(
        "INSERT INTO route_cache "
        "(callsign, origin_icao, origin_name, dest_icao, dest_name, airline, "
        " resolved_at, last_outcome, hit_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'hit', 0) "
        "ON CONFLICT(callsign) DO UPDATE SET "
        "  origin_icao=excluded.origin_icao, origin_name=excluded.origin_name, "
        "  dest_icao=excluded.dest_icao, dest_name=excluded.dest_name, "
        "  airline=excluded.airline, resolved_at=excluded.resolved_at, "
        "  last_outcome='hit'",
        (cs, route["origin_icao"], route["origin_name"], route["dest_icao"],
         route["dest_name"], route["airline"], now))
    conn.commit()
    return {"ok": True, "found": True, "cached": False, "callsign": cs, **route}
