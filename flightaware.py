"""
FlightAware AeroAPI client — paid, budget-guarded callsign→route lookups.

AeroAPI is the only route source here that reads a flight's ACTUAL itinerary
(adsb.lol's standing data is a schedule guess). It is also the only source we
pay for: $0.005 per response, and FlightAware offers **no spending cap** —
every call that isn't an HTTP 429 is billed, including the 404 for a callsign
it has never seen and the 401 from a mistyped key. So the money rules are this
module's whole reason to exist, and they live here rather than in the caller:

  * a monthly call limit (DEFAULT_MONTHLY_LIMIT) counted from the persisted
    state file and enforced BEFORE a request goes out, not after;
  * our own 10-per-60s ceiling on outbound lookups — this is about the meter,
    not about their API's rate limit — plus a full 60-second cooldown after
    any 429;
  * TTL caches for found / not-found / error, because answering twice from
    memory is the only genuinely free answer we get;
  * a permanently "invalid" key that stops all lookups until the key changes
    — a dead key retried on every page view is the classic way a $0 account
    turns into a bill.

The ONE deliberate exception to "everything we do is counted": the free
`/account/usage` call (FlightAware does not bill it), which is what `usage()`
and `test_key()` both ride — reporting on spend must never itself cost.

State (month count + key health) is JSON at `state_path`, written temp+
os.replace through atomic_io so a power cut can't zero the count mid-month.
Caches are in-memory only — they die with the process, which is fine; they
cost money to rebuild, not truth.

The API key never appears in status(), in an exception message, or in a log
line — only its sha256 fingerprint (12 hex, to notice a key change) and its
last 4 characters (so an owner can tell two keys apart).
"""
import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

import atomic_io

logger = logging.getLogger(__name__)

API = "https://aeroapi.flightaware.com/aeroapi"
LOOKUP_COST_USD = 0.005        # every non-429 response is billed at this rate
DEFAULT_MONTHLY_LIMIT = 900    # 900 × $0.005 = the $4.50/month default ceiling

# AeroAPI's flight-ident designator form, e.g. EXA412, SKW1234, AAL42AB.
# Validating keeps a hostile/garbage callsign out of the URL path — the host
# is hardcoded, so this charset cap closes the only caller-influenced part.
CALLSIGN_RE = re.compile(r"^[A-Z]{3}[0-9]{1,4}[A-Z]{0,2}$")

TIMEOUT_S = 6
NONE_TTL_S = 1200              # a "no airborne flight" answer goes stale in 20 min
ERROR_TTL_S = 120              # transient failures retry after 2 min (they cost, so not sooner)
USAGE_TTL_S = 600              # /account/usage is free; 10 min is just politeness
RATE_MAX = 10                  # network lookups allowed per RATE_WINDOW_S
RATE_WINDOW_S = 60
LOOKBACK_H = 26                # flight-search window: now − 26 h … now + 1 h
FOUND_MIN_TTL_S = 1800         # a known flight is re-asked at most every 30 min
FOUND_MAX_TTL_S = 18 * 3600    # … and never held longer than 18 h

KEY_UNTESTED = "untested"
KEY_VALID = "ok"
KEY_INVALID = "invalid"

_POS_ONLY = "L "               # position-only ident: "L 33.60006 -111.87098"

# Owner-facing links (Configuration → Display → FlightAware routes).
SIGNUP_URL = "https://www.flightaware.com/commercial/aeroapi/"
USAGE_URL = "https://www.flightaware.com/aeroapi/portal/usage"
STATE_FILE = ".flightaware-state.json"   # beside the database, like .daily-summary-state.json; uninstall.sh removes it


def _norm(callsign) -> str:
    return (callsign or "").strip().upper()


def _key(cfg) -> str:
    return str(((cfg or {}).get("api_key") or "")).strip()


def _limit(cfg) -> int:
    """Monthly call limit. Anything unusable (missing, junk, negative) is the
    default; an explicit 0 stays 0, because 0 means "paid lookups off"."""
    try:
        val = int(((cfg or {}).get("monthly_limit")))
    except (TypeError, ValueError):
        return DEFAULT_MONTHLY_LIMIT
    return DEFAULT_MONTHLY_LIMIT if val < 0 else val


def _iso_z(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value):
    """AeroAPI ISO-8601 (…Z or an offset) → epoch seconds int, or None."""
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _month(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def _next_month_reset(month: str) -> str:
    try:
        year, mon = int(month[:4]), int(month[5:7])
    except (TypeError, ValueError, IndexError):
        return month
    return f"{year + 1}-01-01" if mon == 12 else f"{year}-{mon + 1:02d}-01"


def _airport(raw) -> Optional[dict]:
    """AeroAPI airport object → {icao, name, city}, icao None when the flight
    only has a position for that end (code_icao absent, code is "L <lat>
    <lon>") — a position is not an airport and must not be rendered as one."""
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("code_icao") or raw.get("code") or "").strip().upper()
    if not code or code.startswith(_POS_ONLY):
        code = None
    return {"icao": code, "name": raw.get("name"), "city": raw.get("city")}


def _has_airport(end) -> bool:
    """A route end only counts if it resolved to an ICAO code — an airport
    object that is nothing but a position (or a bare name) is not a place."""
    return bool(end and end.get("icao"))


def _pick_flight(flights) -> Optional[dict]:
    """The airborne flight for this callsign: actual_off set, actual_on still
    null, not cancelled. Latest actual_off wins (a callsign that flew twice
    today and is airborne again is on its newest flight)."""
    best = None
    for fl in flights if isinstance(flights, list) else []:
        if not isinstance(fl, dict):
            continue
        off = _epoch(fl.get("actual_off"))
        if off is None or _epoch(fl.get("actual_on")) is not None:
            continue
        if fl.get("cancelled"):
            continue
        if best is None or off > _epoch(best.get("actual_off")):
            best = fl
    return best


def _route(flight) -> dict:
    dep = flight.get("origin")
    arr = flight.get("destination")
    arrives = _epoch(flight.get("estimated_on"))
    if arrives is None:
        arrives = _epoch(flight.get("scheduled_on"))
    return {"origin": _airport(dep), "destination": _airport(arr),
            "departed_at": _epoch(flight.get("actual_off")),
            "arrives_at": arrives,
            "fa_flight_id": flight.get("fa_flight_id") or "",
            "registration": flight.get("registration") or None}


class FlightAware:
    """One instance per process, owning the state file and the caches.

    `http_get` and `clock` are injectable so the money rules are testable
    without a network or a real month boundary."""

    def __init__(self, state_path, http_get=requests.get, clock=time.time):
        self.state_path = state_path
        self._http_get = http_get
        self._clock = clock
        self._lock = threading.Lock()
        self._cache = {}        # callsign → {status, route, expires}
        self._stamps = []       # lookups that hit the network, for RATE_MAX
        self._blocked_until = 0.0   # set by a 429
        self._usage = None      # {"data": dict, "expires": ts}
        self._last = None       # the last lookup sent to FlightAware (memory only), for the Status page
        self.on_found = None    # v3.4.171: called (callsign, route) after a lookup finds a flight; errors ignored
        st = atomic_io.read_json(state_path) or {}
        self._month = str(st.get("month") or "")
        try:
            self._count = int(st.get("count") or 0)
        except (TypeError, ValueError):
            self._count = 0
        self._key_fp = st.get("key_fp") or None
        self._key_state = st.get("key_state") or KEY_UNTESTED
        self._tested_at = st.get("tested_at") if isinstance(st.get("tested_at"), int) else None

    # --- state file ---------------------------------------------------------

    @staticmethod
    def _fp(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    def _roll_month_locked(self) -> None:
        """A new UTC month resets the count — the limit is monthly, and a
        stale count is how an owner gets silently throttled."""
        month = _month(self._clock())
        if self._month != month:
            self._month = month
            self._count = 0
            self._persist_locked()

    def _adopt_key_locked(self, key: str) -> None:
        """A different key is a different credential: forget what we knew about
        the old one, so a replaced key is trusted again (untested) and a
        previously-invalid one gets another chance once the owner fixes it."""
        fp = self._fp(key)
        if fp != self._key_fp:
            self._key_fp = fp
            self._key_state = KEY_UNTESTED
            self._tested_at = None
            self._persist_locked()

    def _persist_locked(self) -> None:
        payload = {"month": self._month, "count": self._count,
                   "key_fp": self._key_fp, "key_state": self._key_state,
                   "tested_at": self._tested_at}
        try:
            atomic_io.ensure_private_dir(Path(str(self.state_path)).parent)
            atomic_io.write_json_atomic(self.state_path, payload,
                                        mode=atomic_io.PRIVATE_FILE_MODE)
        except OSError as e:
            # The in-memory count still guards this process; losing the file
            # write must never take a lookup path down with it.
            logger.info(f"flightaware: could not persist state to {self.state_path}: {e}")

    def _reserve_locked(self) -> str:
        """Take a billing slot BEFORE the request goes out, and return the month
        it was taken in. Counting on the reply instead would let two page
        renders that both arrive at count == limit-1 both pass the gate — a
        limit you can overrun by racing isn't a limit. Replies that turn out
        not to be billed give the slot back (`_refund_locked`)."""
        self._roll_month_locked()
        self._count += 1
        self._persist_locked()
        return self._month

    def _refund_locked(self, month: str) -> None:
        """No reply (timeout/connection error) or a 429 — the only reply
        AeroAPI doesn't bill. Skip it if the month rolled mid-flight, else the
        refund lands on a month that never got the charge."""
        if month == self._month and self._count > 0:
            self._count -= 1
            self._persist_locked()

    # --- rate limiting ------------------------------------------------------

    def _busy_locked(self) -> bool:
        now = self._clock()
        if now < self._blocked_until:
            return True
        self._stamps = [t for t in self._stamps if now - t < RATE_WINDOW_S]
        return len(self._stamps) >= RATE_MAX

    # --- cache --------------------------------------------------------------

    def _cache_get(self, cs: str, now):
        hit = self._cache.get(cs)
        if not hit or now >= hit["expires"]:
            return None
        return hit

    def _cache_put_locked(self, cs: str, status: str, now, route=None, expires=None):
        if expires is None:
            expires = now + (NONE_TTL_S if status == "none" else ERROR_TTL_S)
        self._cache[cs] = {"status": status, "route": route, "expires": expires}

    def _found_ttl(self, route: dict, now: int) -> int:
        """Refresh 30 min after the flight is due to land, but never hold a
        live flight's stale plan for more than 18 h, and never less than 30
        min (an unknown arrival time must not turn into a per-view re-bill)."""
        hard = now + FOUND_MAX_TTL_S
        arrives = route.get("arrives_at")
        if not arrives:
            return hard
        return max(now + FOUND_MIN_TTL_S, min(arrives + FOUND_MIN_TTL_S, hard))

    def cached(self, callsign) -> Optional[dict]:
        """The cached route for a callsign, or None. Never touches the network
        or the state file — safe on every page render."""
        hit = self._cache_get(_norm(callsign), self._clock())
        return hit["route"] if hit and hit["status"] == "found" else None

    # --- HTTP ---------------------------------------------------------------

    def _get(self, path: str, key: str, params=None):
        headers = {"x-apikey": key, "Accept": "application/json"}
        return self._http_get(f"{API}{path}", params=params, headers=headers,
                              timeout=TIMEOUT_S)

    def _safe(self, key: str, message: str) -> str:
        """Belt and braces: an exception repr must never carry the key, even
        if a transport ever puts a header dump in it."""
        return message.replace(key, "[redacted]") if key else message

    # --- public API ---------------------------------------------------------

    def lookup(self, cfg, callsign) -> dict:
        """Cache-first paid lookup. Returns {"status": ...} — found also
        carries "route". A cached answer costs nothing, so it is served even
        while lookups are paused, busy or the key is rejected; the money gates
        only stand between a cache miss and the network."""
        cs = _norm(callsign)
        key = _key(cfg)
        if not key:
            return {"status": "no_key"}
        if not CALLSIGN_RE.match(cs):
            return {"status": "skipped"}

        now = int(self._clock())
        with self._lock:
            self._adopt_key_locked(key)
            self._roll_month_locked()
            hit = self._cache_get(cs, now)
            if hit:
                out = {"status": hit["status"]}
                if hit["status"] == "found":
                    out["route"] = hit["route"]
                return out
            count, limit = self._count, _limit(cfg)
            if limit <= 0 or count >= limit:
                return {"status": "paused"}
            if self._key_state == KEY_INVALID:
                return {"status": "invalid_key"}
            if self._busy_locked():
                return {"status": "busy"}
            self._stamps.append(now)
            reserved = self._reserve_locked()

        try:
            resp = self._get(f"/flights/{cs}", key,
                             params={"ident_type": "designator",
                                     "start": _iso_z(now - LOOKBACK_H * 3600),
                                     "end": _iso_z(now + 3600),
                                     "max_pages": 1})
        except Exception as e:
            message = self._safe(key, f"error connecting to FlightAware: {e}")
            logger.info(f"flightaware {cs}: not billed, cached {ERROR_TTL_S}s: {message}")
            with self._lock:
                self._refund_locked(reserved)
                self._cache_put_locked(cs, "error", now)
                self._note_locked(cs, now, "error")
            return {"status": "error", "message": message}

        status_code = getattr(resp, "status_code", 0)
        if status_code == 429:
            # Not billed (the only reply that isn't) — and back off a full
            # minute rather than re-asking on the next page view.
            logger.info(f"flightaware {cs}: 429 rate-limited, cooling off {RATE_WINDOW_S}s")
            with self._lock:
                self._refund_locked(reserved)
                self._blocked_until = now + RATE_WINDOW_S
                self._note_locked(cs, now, "busy")
            return {"status": "busy"}

        # Every remaining reply — 200, 404, 401, 500 — is billed, so the
        # reserved slot stands.

        if status_code in (401, 403):
            message = self._safe(
                key, f"FlightAware rejected the API key (HTTP {status_code})")
            logger.info(f"flightaware {cs}: key rejected (HTTP {status_code}); "
                        f"no further lookups until the key changes")
            with self._lock:
                self._key_state = KEY_INVALID
                self._tested_at = now
                self._persist_locked()
                self._note_locked(cs, now, "invalid_key")
            return {"status": "invalid_key", "message": message}

        if status_code == 200:
            payload = self._json(resp)
            if payload is None:
                # A 200 whose body we can't read is an anomaly, not an answer
                # — retrying it in ERROR_TTL_S is right; a real "no flights"
                # answer gets the longer NONE_TTL_S below.
                logger.info(f"flightaware {cs}: HTTP 200 body was not a JSON "
                            f"object, cached {ERROR_TTL_S}s")
                with self._lock:
                    self._cache_put_locked(cs, "error", now)
                    self._note_locked(cs, now, "error")
                return {"status": "error", "message": "Unreadable response body"}
            flight = _pick_flight(payload.get("flights"))
            route = None
            if flight is not None:
                candidate = _route(flight)
                if (_has_airport(candidate["origin"])
                        or _has_airport(candidate["destination"])):
                    route = candidate
            if route is not None:
                with self._lock:
                    self._cache_put_locked(cs, "found", now, route=route,
                                           expires=self._found_ttl(route, now))
                    self._note_locked(cs, now, "found", route)
                if self.on_found is not None:
                    try:
                        self.on_found(cs, route)
                    except Exception as e:
                        logger.info(f"flightaware {cs}: recording the route failed: {type(e).__name__}")
                return {"status": "found", "route": route}
            with self._lock:
                self._cache_put_locked(cs, "none", now)
                self._note_locked(cs, now, "none")
            return {"status": "none"}

        if status_code == 404:
            # A callsign AeroAPI has no flight for: a real answer, billed.
            with self._lock:
                self._cache_put_locked(cs, "none", now)
                self._note_locked(cs, now, "none")
            return {"status": "none"}

        message = self._safe(key, f"FlightAware returned HTTP {status_code}")
        logger.info(f"flightaware {cs}: HTTP {status_code}, cached "
                    f"{ERROR_TTL_S}s (billed anyway): {message}")
        with self._lock:
            self._cache_put_locked(cs, "error", now)
            self._note_locked(cs, now, "error")
        return {"status": "error", "message": message}

    def _note_locked(self, cs: str, now: int, status: str, route=None) -> None:
        """Remember the last lookup sent to FlightAware, for the Status page."""
        o = (route or {}).get("origin") or {}
        d = (route or {}).get("destination") or {}
        self._last = {"ts": now, "callsign": cs, "status": status,
                      "origin": o.get("icao"), "destination": d.get("icao")}

    @staticmethod
    def _json(resp):
        try:
            data = resp.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def usage(self, cfg, force=False) -> Optional[dict]:
        """Spending so far this calendar month, from FlightAware's FREE
        /account/usage (billed by neither party): {"total_cost",
        "total_discount_cost", "total_calls", "as_of"}, or None on any error.
        Cached USAGE_TTL_S unless `force`. Never counted against the limit —
        counting the thing that tells us what we spent would be self-defeating."""
        key = _key(cfg)
        if not key:
            return None
        now = int(self._clock())
        with self._lock:
            self._adopt_key_locked(key)
            if not force and self._usage and now < self._usage["expires"]:
                return dict(self._usage["data"])
        try:
            resp = self._get("/account/usage", key,
                             params={"start": f"{_month(now)}-01T00:00:00Z"})
        except Exception as e:
            logger.info(f"flightaware usage: {self._safe(key, str(e))}")
            return None
        status_code = getattr(resp, "status_code", 0)
        if status_code != 200:
            logger.info(f"flightaware usage: HTTP {status_code}")
            return None
        payload = self._json(resp) or {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
        data = {"total_cost": self._num(usage.get("total_cost")),
                "total_discount_cost": self._num(usage.get("total_discount_cost")),
                "total_calls": self._num(usage.get("total_calls")),
                "as_of": now}
        with self._lock:
            self._usage = {"data": dict(data), "expires": now + USAGE_TTL_S}
        return data

    def cached_usage(self) -> Optional[dict]:
        """FlightAware's usage figure from the last usage() call, however old
        (its "as_of" says when), or None. Never touches the network: for
        /api/status, which every page polls."""
        with self._lock:
            return dict(self._usage["data"]) if self._usage else None

    @staticmethod
    def _num(value):
        return value if isinstance(value, (int, float)) else None

    def test_key(self, cfg) -> dict:
        """Owner-facing key check on the config page, over the free usage
        call. Persists key_state + tested_at so `status` survives a restart."""
        key = _key(cfg)
        now = int(self._clock())
        if not key:
            return {"state": "error", "message": "No FlightAware API key is configured."}
        with self._lock:
            self._adopt_key_locked(key)
        try:
            resp = self._get("/account/usage", key,
                             params={"start": f"{_month(now)}-01T00:00:00Z"})
        except Exception as e:
            message = self._safe(key, f"Could not reach FlightAware: {e}")
            logger.info(f"flightaware test_key: {message}")
            return {"state": "error", "message": message}

        status_code = getattr(resp, "status_code", 0)
        if status_code == 200:
            payload = self._json(resp) or {}
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
            calls = self._num(usage.get("total_calls"))
            with self._lock:
                self._key_state = KEY_VALID
                self._tested_at = now
                self._persist_locked()
            message = (f"Key accepted. {calls:.0f} billed calls so far this month."
                       if calls is not None else "Key accepted.")
            return {"state": "ok", "message": message}
        if status_code in (401, 403):
            message = self._safe(
                key, f"FlightAware rejected the key (HTTP {status_code}).")
            logger.info(f"flightaware test_key: key rejected (HTTP {status_code})")
            with self._lock:
                self._key_state = KEY_INVALID
                self._tested_at = now
                self._persist_locked()
            return {"state": "invalid", "message": message}
        return {"state": "error",
                "message": self._safe(key, f"FlightAware returned HTTP {status_code}.")}

    def status(self, cfg) -> dict:
        """Owner-facing summary for the status/config pages. Carries no key —
        last 4 characters and a hash fingerprint at most."""
        key = _key(cfg)
        with self._lock:
            if key:
                self._adopt_key_locked(key)
            self._roll_month_locked()
            count, month = self._count, self._month
            limit = _limit(cfg)
            return {"configured": bool(key),
                    "key_last4": key[-4:] if key else None,
                    "key_state": self._key_state if key else KEY_UNTESTED,
                    "tested_at": self._tested_at,
                    "month": month,
                    "count": count,
                    "limit": limit,
                    "paused": limit <= 0 or count >= limit,
                    "resets_on": _next_month_reset(month),
                    "cost_estimate": round(count * LOOKUP_COST_USD, 4),
                    "limit_cost": round(limit * LOOKUP_COST_USD, 4),
                    "last_lookup": dict(self._last) if self._last else None}


def state_path_for(config) -> str:
    """The install's state file: next to its database, so a second install on
    the same machine keeps its own monthly count."""
    db = str(((config or {}).get("data") or {}).get("db_file") or "history.db")
    return str(Path(db).resolve().parent / STATE_FILE)


def record_route(db_path, callsign: str, route: dict) -> None:
    """v3.4.171: keep what a lookup found, derived fields only (never the raw
    reply), in flight_routes for the aircraft page's route history."""
    fid = (route or {}).get("fa_flight_id")
    o, d = (route or {}).get("origin") or {}, (route or {}).get("destination") or {}
    if not db_path or not fid or not (o.get("icao") and d.get("icao")):
        return
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("""
            INSERT INTO flight_routes (fa_flight_id, callsign, registration, origin_icao, origin_name,
                                       dest_icao, dest_name, departed_at, arrives_at, looked_up_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fa_flight_id) DO UPDATE SET
                origin_icao = excluded.origin_icao, origin_name = excluded.origin_name,
                dest_icao = excluded.dest_icao, dest_name = excluded.dest_name,
                departed_at = excluded.departed_at, arrives_at = excluded.arrives_at,
                registration = excluded.registration, looked_up_at = excluded.looked_up_at
        """, (fid, _norm(callsign), route.get("registration"),
              o["icao"], o.get("city") or o.get("name"), d["icao"], d.get("city") or d.get("name"),
              route.get("departed_at"), route.get("arrives_at"), int(time.time())))
        conn.commit()
    finally:
        conn.close()


_shared = None
_shared_lock = threading.Lock()


def shared(state_path, db_path=None) -> "FlightAware":
    """The process's one client. The web server and the collector run in the
    same process, so they share its caches and its monthly count. With
    `db_path`, a found route is also kept in that database's flight_routes."""
    global _shared
    with _shared_lock:
        if _shared is None or str(_shared.state_path) != str(state_path):
            _shared = FlightAware(state_path)
        if db_path:
            _shared.on_found = lambda cs, route, _db=str(db_path): record_route(_db, cs, route)
        return _shared
