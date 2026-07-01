"""
Lazy ICAO-hex → aircraft-photo resolver (photo enrichment, v3.4.107).

The aircraft detail page shows a thumbnail photo of the airframe. Photos come
from planespotters.net's free public photo API, keyed by ICAO hex (the 24-bit
Mode S address we always have) and cached in the `photo_cache` table (schema
migration v13), with a NEGATIVE-cache marker for airframes planespotters has
no photo of, so the same unphotographed hex isn't re-queried on every view.

Mirrors route_resolver.py's caching discipline: a TTL'd positive entry + a
shorter-lived negative entry, and transient network errors are NOT cached so
they retry. FETCH-ON-DEMAND (lazy): the /api/aircraft/{icao}/photo endpoint
calls resolve_photo() when a user opens an aircraft.

planespotters REQUIRES a descriptive User-Agent that names the app and gives a
contact URL — a generic library UA is rejected with a 400. We send one below.
Attribution (photographer + a link back to planespotters) is REQUIRED by their
terms and is carried through to the UI.
"""
import logging
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_PLANESPOTTERS_URL = "https://api.planespotters.net/pub/photos/hex/{hex}"
_TIMEOUT_SEC = 6

# planespotters rejects generic User-Agents (400 "Generic library User-Agent
# strings are not accepted"). Must identify the app + a contact URL.
_USER_AGENT = "Aerodrome-ADSB (+https://github.com/preston-peterson/aerodrome)"

# A photo set for an airframe is near-static but new photos get added, so a
# known photo is re-resolved at most monthly. Misses expire sooner because a
# currently-unphotographed airframe may get its first photo any week — the
# same positive/negative split route_resolver / the hexdb owner resolver use.
PHOTO_POSITIVE_TTL_DAYS = 30
PHOTO_NEGATIVE_TTL_DAYS = 7

# 24-bit Mode S address = exactly 6 hex digits. Pseudo-ICAO (dump1090's '~'
# TIS-B/MLAT prefix) is never a real airframe → won't have photos → rejected
# here. Validating keeps a hostile/garbage hex out of the URL (host is
# hardcoded, so this charset cap closes the only caller-influenced part).
_HEX_RE = re.compile(r"^[0-9A-F]{6}$")

# Observability — same spirit as route_resolver's stats counters.
_stats = {"attempts": 0, "hits": 0, "misses": 0, "errors": 0, "last_error": None}


def resolver_stats() -> dict:
    """Snapshot for diagnostics."""
    return dict(_stats)


def _norm(icao: Optional[str]) -> str:
    return (icao or "").strip().upper()


def _miss(icao: str, cached: bool) -> dict:
    return {"ok": True, "found": False, "cached": cached, "icao": icao}


def _fetch(hexid: str) -> Optional[dict]:
    """Fetch photos for one ICAO hex from planespotters. Returns a photo dict
    on a hit, None on a clean miss (HTTP 200 with an empty photo list). RAISES
    on a transient error (timeout, non-200, network) so the caller does NOT
    negative-cache a blip."""
    _stats["attempts"] += 1
    url = _PLANESPOTTERS_URL.format(hex=hexid)   # hexid is _HEX_RE-validated → URL-safe
    resp = requests.get(
        url, timeout=_TIMEOUT_SEC,
        headers={"User-Agent": _USER_AGENT},     # required — see module docstring
        allow_redirects=False,                   # hardcoded host; don't follow a redirect
    )
    resp.raise_for_status()                       # any non-200 → transient, raise
    photos = resp.json().get("photos") or []
    if not photos:
        return None                               # 200 + no photos = a real miss
    p = photos[0]
    thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
    src = (thumb.get("src") or "").strip()
    if not src:
        return None
    return {
        "thumbnail_url": src,
        "photo_link": (p.get("link") or "").strip(),
        "photographer": (p.get("photographer") or "").strip(),
    }


def resolve_photo(conn, icao: str) -> dict:
    """Cache-first ICAO-hex→photo resolve. Returns:
        {ok, found, cached, icao[, thumbnail_url, photo_link, photographer]}
    Never raises: a transient fetch error returns found=False, cached=False and
    writes NOTHING (so it retries next view). A clean 'no photo' is negative-
    cached. Caller provides an open, writable sqlite3 connection."""
    hexid = _norm(icao)
    if not _HEX_RE.match(hexid):
        return _miss(hexid, cached=False)
    now = int(time.time())

    row = conn.execute(
        "SELECT thumbnail_url, photo_link, photographer, resolved_at, last_outcome "
        "FROM photo_cache WHERE icao = ?",
        (hexid,)).fetchone()
    if row is not None:
        thumb, link, photog, resolved_at, outcome = row
        ttl = (PHOTO_POSITIVE_TTL_DAYS if outcome == "hit"
               else PHOTO_NEGATIVE_TTL_DAYS) * 86400
        if (now - (resolved_at or 0)) < ttl:
            conn.execute(
                "UPDATE photo_cache SET hit_count = hit_count + 1 WHERE icao = ?",
                (hexid,))
            conn.commit()
            if outcome == "hit":
                return {"ok": True, "found": True, "cached": True, "icao": hexid,
                        "thumbnail_url": thumb, "photo_link": link,
                        "photographer": photog}
            return _miss(hexid, cached=True)
        # stale → fall through and re-fetch

    try:
        photo = _fetch(hexid)
    except Exception as e:
        _stats["errors"] += 1
        _stats["last_error"] = repr(e)
        logger.info(f"photo resolve {hexid}: transient error, not cached: {e}")
        return _miss(hexid, cached=False)

    if photo is None:
        _stats["misses"] += 1
        conn.execute(
            "INSERT INTO photo_cache "
            "(icao, thumbnail_url, photo_link, photographer, resolved_at, "
            " last_outcome, hit_count) "
            "VALUES (?, NULL, NULL, NULL, ?, 'miss', 0) "
            "ON CONFLICT(icao) DO UPDATE SET "
            "  thumbnail_url=NULL, photo_link=NULL, photographer=NULL, "
            "  resolved_at=excluded.resolved_at, last_outcome='miss'",
            (hexid, now))
        conn.commit()
        return _miss(hexid, cached=False)

    _stats["hits"] += 1
    conn.execute(
        "INSERT INTO photo_cache "
        "(icao, thumbnail_url, photo_link, photographer, resolved_at, "
        " last_outcome, hit_count) "
        "VALUES (?, ?, ?, ?, ?, 'hit', 0) "
        "ON CONFLICT(icao) DO UPDATE SET "
        "  thumbnail_url=excluded.thumbnail_url, photo_link=excluded.photo_link, "
        "  photographer=excluded.photographer, resolved_at=excluded.resolved_at, "
        "  last_outcome='hit'",
        (hexid, photo["thumbnail_url"], photo["photo_link"], photo["photographer"], now))
    conn.commit()
    return {"ok": True, "found": True, "cached": False, "icao": hexid, **photo}
