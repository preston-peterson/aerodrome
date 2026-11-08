"""
Unit tests for flightaware.py — the paid, budget-guarded AeroAPI client.

No network, no real clock, no real spending: `http_get` is a recording fake
(queue of canned responses; an Exception in the queue is raised) and `clock`
is a stepper, so a month boundary is one `advance()` away. The tests are
organised around the money rules, because those are the point of the module:
what is billed, what is cached instead of re-billed, what stops the spending,
and the fact that the key itself never leaves the process.

Callsigns, airports and cities are invented (EXA412 / KEXA "Exampleville").
"""
import json
import tempfile
import unittest

import flightaware
from datetime import datetime, timezone
from pathlib import Path

from flightaware import (API, DEFAULT_MONTHLY_LIMIT, ERROR_TTL_S, LOOKUP_COST_USD,
                        NONE_TTL_S, RATE_MAX, TIMEOUT_S, USAGE_TTL_S, FlightAware)

API_KEY = "example-key-a1b2c3d4e5f6"
CS = "EXA412"


def epoch(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


NOW = epoch(2026, 11, 6, 12, 0, 0)     # 2026-11-06T12:00:00Z
DEPARTED = epoch(2026, 11, 6, 11, 30, 0)
ARRIVES = epoch(2026, 11, 6, 13, 45, 0)


class FakeClock:
    def __init__(self, ts):
        self.ts = float(ts)

    def __call__(self) -> float:
        return self.ts

    def advance(self, seconds):
        self.ts += seconds


class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeHTTP:
    """Stands in for requests.get and records how it was called."""

    def __init__(self):
        self.calls = []
        self._queue = []
        self.default = FakeResponse(404, {"flights": []})

    def queue(self, *responses):
        self._queue.extend(responses)

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers,
                           "timeout": timeout})
        item = self._queue.pop(0) if self._queue else self.default
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def calls_made(self) -> int:
        return len(self.calls)

    def call(self, i=0) -> dict:
        return self.calls[i]


def airport(code="KEXA", name="Example Field", city="Exampleville", **extra):
    a = {"name": name, "city": city}
    if code is not None:
        a["code"] = code
    a.update(extra)
    return a


POSITION_ONLY = "L 33.60006 -111.87098"     # AeroAPI's position-only "code"


def flight(off="2026-11-06T11:30:00Z", on=None, est="2026-11-06T13:45:00Z",
           sched="2026-11-06T13:30:00Z", cancelled=False,
           fa_flight_id="EXA412-1762400000", registration="N412EX",
           origin=None, dest=None):
    return {
        "fa_flight_id": fa_flight_id,
        "registration": registration,
        "origin": origin if origin is not None else airport(
            "KEXA", code_icao="KEXA"),
        "destination": dest if dest is not None else airport(
            "KEXB", name="Test Valley Municipal", city="Testbrook", code_icao="KEXB"),
        "actual_off": off,
        "actual_on": on,
        "estimated_on": est,
        "scheduled_on": sched,
        "cancelled": cancelled,
    }


def payload(*flights):
    return {"flights": list(flights)}


ORIGIN = {"icao": "KEXA", "name": "Example Field", "city": "Exampleville"}
DEST = {"icao": "KEXB", "name": "Test Valley Municipal", "city": "Testbrook"}


class FlightAwareTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "flightaware_state.json"
        self.clock = FakeClock(NOW)
        self.http = FakeHTTP()

    def client(self, *responses) -> FlightAware:
        self.http.queue(*responses)
        return FlightAware(self.state, http_get=self.http, clock=self.clock)

    def cfg(self, limit=900, key=API_KEY) -> dict:
        cfg = {"watchlist_lookups": True}
        if limit is not None:
            cfg["monthly_limit"] = limit
        if key is not None:
            cfg["api_key"] = key
        return cfg

    def seeded(self, month="2026-11", count=0, key_state="untested",
               tested_at=None, key=API_KEY):
        import hashlib
        self.state.write_text(json.dumps({
            "month": month, "count": count, "key_state": key_state,
            "tested_at": tested_at,
            "key_fp": hashlib.sha256(key.encode()).hexdigest()[:12]}),
            encoding="utf-8")


# --- request shape, picking, and parsing ------------------------------------

class TestRequestAndParsing(FlightAwareTestCase):
    def test_found_parses_route_and_times(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        r = fa.lookup(self.cfg(), CS)
        self.assertEqual(r["status"], "found")
        self.assertEqual(r["route"], {
            "origin": ORIGIN, "destination": DEST,
            "departed_at": DEPARTED, "arrives_at": ARRIVES,
            "fa_flight_id": "EXA412-1762400000", "registration": "N412EX"})

    def test_request_shape_and_key_header(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        fa.lookup(self.cfg(), "  exa412  ")          # normalised to EXA412
        call = self.http.call()
        self.assertEqual(call["url"], f"{API}/flights/EXA412")
        self.assertEqual(call["params"], {
            "ident_type": "designator",
            "start": "2026-11-05T10:00:00Z",           # now − 26 h
            "end": "2026-11-06T13:00:00Z",             # now + 1 h
            "max_pages": 1})
        self.assertEqual(call["headers"], {"x-apikey": API_KEY,
                                           "Accept": "application/json"})
        self.assertEqual(call["timeout"], TIMEOUT_S)

    def test_arrives_at_falls_back_to_scheduled_on(self):
        fa = self.client(FakeResponse(200, payload(flight(est=None))))
        r = fa.lookup(self.cfg(), CS)
        self.assertEqual(r["route"]["arrives_at"], epoch(2026, 11, 6, 13, 30))

    def test_latest_airborne_flight_wins(self):
        older = flight(fa_flight_id="EXA412-old", off="2026-11-05T14:00:00Z")
        newer = flight(fa_flight_id="EXA412-new", off="2026-11-06T09:00:00Z")
        landed = flight(fa_flight_id="EXA412-landed", off="2026-11-06T11:00:00Z",
                        on="2026-11-06T11:55:00Z")
        cancelled = flight(fa_flight_id="EXA412-cancelled", off="2026-11-06T12:30:00Z",
                           cancelled=True)
        fa = self.client(FakeResponse(200, payload(newer, landed, older, cancelled)))
        r = fa.lookup(self.cfg(), CS)
        self.assertEqual(r["route"]["fa_flight_id"], "EXA412-new")

    def test_position_only_airport_code_is_not_an_airport(self):
        pos = airport(code=POSITION_ONLY, name=None, city=None)
        fa = self.client(FakeResponse(200, payload(flight(dest=pos))))
        r = fa.lookup(self.cfg(), CS)
        self.assertEqual(r["status"], "found")
        self.assertEqual(r["route"]["destination"],
                         {"icao": None, "name": None, "city": None})
        self.assertEqual(r["route"]["origin"], ORIGIN)

    def test_icao_code_preferred_then_plain_code(self):
        dep = airport("EXA", code_icao="KEXA")        # IATA-looking code + ICAO
        arr = airport("KEXB")                          # code only, no code_icao
        fa = self.client(FakeResponse(200, payload(flight(origin=dep, dest=arr))))
        r = fa.lookup(self.cfg(), CS)
        self.assertEqual(r["route"]["origin"]["icao"], "KEXA")
        self.assertEqual(r["route"]["destination"]["icao"], "KEXB")

    def test_both_ends_position_only_is_none(self):
        pos = airport(code=POSITION_ONLY, name=None, city=None)
        fa = self.client(FakeResponse(200, payload(flight(origin=pos, dest=pos))))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "none")

    def test_no_airborne_flight_is_none(self):
        landed = flight(on="2026-11-06T11:55:00Z")
        planned = flight(off=None)
        fa = self.client(FakeResponse(200, payload(landed, planned)))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "none")
        self.assertEqual(self.http.calls_made, 1)

    def test_empty_flight_list_is_none(self):
        fa = self.client(FakeResponse(200, payload()))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "none")

    def test_404_is_none_and_billed(self):
        fa = self.client(FakeResponse(404, {"status": 404, "reason": "NotFound"}))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "none")
        self.assertEqual(fa.status(self.cfg())["count"], 1)

    def test_unparseable_json_is_error(self):
        fa = self.client(FakeResponse(200, bad_json=True))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")

    def test_5xx_is_error(self):
        fa = self.client(FakeResponse(503, {"reason": "Trouble"}))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")

    def test_timeout_is_error_and_not_billed(self):
        import requests
        fa = self.client(requests.exceptions.Timeout("timed out"))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")
        self.assertEqual(fa.status(self.cfg())["count"], 0)

    def test_connect_timeout_is_not_billed(self):
        import requests
        fa = self.client(requests.exceptions.ConnectTimeout("connect timed out"))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")
        self.assertEqual(fa.status(self.cfg())["count"], 0)

    def test_read_timeout_is_billed(self):
        # v3.4.175: FlightAware got the request and bills it; no answer came back in time.
        import requests
        fa = self.client(requests.exceptions.ReadTimeout("read timed out"))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")
        self.assertEqual(fa.status(self.cfg())["count"], 1)


# --- caching (the only genuinely free answer) ------------------------------

class TestCache(FlightAwareTestCase):
    def test_found_served_from_cache_without_network(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        first = fa.lookup(self.cfg(), CS)
        second = fa.lookup(self.cfg(), CS)
        self.assertEqual(self.http.calls_made, 1)
        self.assertEqual(second["route"], first["route"])
        self.assertEqual(second["status"], "found")

    def test_found_cache_lives_until_arrival_plus_30_min(self):
        fa = self.client(FakeResponse(200, payload(flight())),
                        FakeResponse(200, payload(flight())))
        fa.lookup(self.cfg(), CS)
        self.clock.advance(ARRIVES + 1800 - NOW - 1)
        fa.lookup(self.cfg(), CS)
        self.assertEqual(self.http.calls_made, 1)      # still fresh
        self.clock.advance(1)
        fa.lookup(self.cfg(), CS)
        self.assertEqual(self.http.calls_made, 2)      # now stale → re-asked

    def test_found_cache_never_exceeds_18_hours(self):
        long_flight = flight(est="2026-11-09T12:00:00Z")
        fa = self.client(FakeResponse(200, payload(long_flight)),
                        FakeResponse(200, payload(long_flight)))
        fa.lookup(self.cfg(), CS)
        self.clock.advance(18 * 3600 - 1)
        fa.lookup(self.cfg(), CS)
        self.assertEqual(self.http.calls_made, 1)
        self.clock.advance(1)
        fa.lookup(self.cfg(), CS)
        self.assertEqual(self.http.calls_made, 2)

    def test_found_without_arrival_time_caches_18_hours(self):
        fa = self.client(FakeResponse(200, payload(flight(est=None, sched=None))),
                        FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(self.cfg(), CS)["route"]["arrives_at"], None)
        self.clock.advance(18 * 3600 - 1)
        fa.lookup(self.cfg(), CS)
        self.assertEqual(self.http.calls_made, 1)

    def test_none_cached_for_none_ttl(self):
        fa = self.client(FakeResponse(200, payload()),
                        FakeResponse(200, payload()))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "none")
        self.clock.advance(NONE_TTL_S - 1)
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "none")
        self.assertEqual(self.http.calls_made, 1)
        self.clock.advance(1)
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "none")
        self.assertEqual(self.http.calls_made, 2)

    def test_error_cached_only_for_error_ttl(self):
        fa = self.client(FakeResponse(500), FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")
        self.clock.advance(ERROR_TTL_S - 1)
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")
        self.assertEqual(self.http.calls_made, 1)
        self.clock.advance(1)
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "found")
        self.assertEqual(self.http.calls_made, 2)

    def test_cached_accessor_serves_route_without_network(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        fa.lookup(self.cfg(), CS)
        self.clock.advance(60)
        self.assertEqual(self.http.calls_made, 1)
        self.assertEqual(fa.cached(" exa412 "), dict(
            origin=ORIGIN, destination=DEST, departed_at=DEPARTED,
            arrives_at=ARRIVES, fa_flight_id="EXA412-1762400000",
            registration="N412EX"))
        self.assertEqual(fa.cached("EXA999"), None)
        self.assertEqual(self.http.calls_made, 1)

    def test_cached_is_none_for_a_negative_entry(self):
        fa = self.client(FakeResponse(200, payload()))
        fa.lookup(self.cfg(), CS)
        self.assertEqual(fa.cached(CS), None)


# --- gating: no key, bad callsign, budget -----------------------------------

class TestGating(FlightAwareTestCase):
    def test_no_key_never_touches_network(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        for cfg in (None, {}, {"api_key": ""}, {"api_key": "   "}):
            self.assertEqual(fa.lookup(cfg, CS)["status"], "no_key")
        self.assertEqual(self.http.calls_made, 0)

    def test_bad_callsign_is_skipped_without_network(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        for bad in ("EXA", "EX12", "ABCD123", "EXA41234", "EXA41ABX", "EXA 412",
                    "EXA412!", "", None, "  "):
            self.assertEqual(fa.lookup(self.cfg(), bad)["status"], "skipped", bad)
        self.assertEqual(self.http.calls_made, 0)

    def test_callsign_shapes_that_are_valid(self):
        fa = self.client(*[FakeResponse(200, payload()) for _ in range(5)])
        for good in ("EXA412", "exa412", " EXA1 ", "EXA4123", "EXA1234AB"):
            self.assertEqual(fa.lookup(self.cfg(), good)["status"], "none", good)

    def test_paused_once_the_month_hits_the_limit(self):
        cfg = self.cfg(limit=3)
        fa = self.client(*[FakeResponse(200, payload()) for _ in range(3)])
        for i in range(3):
            self.assertEqual(fa.lookup(cfg, f"EXA{i + 1}")["status"], "none")
        self.assertEqual(fa.status(cfg)["paused"], True)
        self.assertEqual(fa.lookup(cfg, "EXA9")["status"], "paused")
        self.assertEqual(self.http.calls_made, 3)

    def test_limit_zero_switches_lookups_off(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(self.cfg(limit=0), CS)["status"], "paused")
        self.assertEqual(self.http.calls_made, 0)
        self.assertEqual(fa.status(self.cfg(limit=0))["paused"], True)

    def test_missing_or_junk_limit_falls_back_to_default(self):
        fa = self.client()
        for cfg in ({"api_key": API_KEY}, {"api_key": API_KEY, "monthly_limit": None},
                    {"api_key": API_KEY, "monthly_limit": "many"},
                    {"api_key": API_KEY, "monthly_limit": -5}):
            self.assertEqual(fa.status(cfg)["limit"], DEFAULT_MONTHLY_LIMIT)

    def test_pause_persists_and_clears_next_month(self):
        cfg = self.cfg(limit=1)
        self.seeded(count=1)
        fa = self.client(FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(cfg, CS)["status"], "paused")
        self.clock.advance(31 * 86400)                  # past the UTC month flip
        self.assertEqual(fa.lookup(cfg, CS)["status"], "found")
        self.assertEqual(fa.status(cfg)["count"], 1)


# --- counting, state file, month rollover -----------------------------------

class TestCountingAndState(FlightAwareTestCase):
    def test_every_non_429_response_is_counted(self):
        fa = self.client(FakeResponse(200, payload(flight())),
                        FakeResponse(404),
                        FakeResponse(500),
                        FakeResponse(429))
        cfg = self.cfg()
        self.assertEqual(fa.lookup(cfg, "EXA1")["status"], "found")
        self.assertEqual(fa.lookup(cfg, "EXA2")["status"], "none")
        self.assertEqual(fa.lookup(cfg, "EXA3")["status"], "error")
        self.assertEqual(fa.lookup(cfg, "EXA4")["status"], "busy")
        self.assertEqual(fa.status(cfg)["count"], 3)

    def test_network_errors_are_not_counted(self):
        import requests
        fa = self.client(requests.exceptions.ConnectionError("no route to host"))
        self.assertEqual(fa.lookup(self.cfg(), CS)["status"], "error")
        self.assertEqual(fa.status(self.cfg())["count"], 0)

    def test_state_file_holds_only_the_agreed_shape(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        fa.lookup(self.cfg(), CS)
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(set(st), {"month", "count", "key_count", "key_fp", "key_state",
                                   "tested_at"})
        self.assertEqual(st["month"], "2026-11")
        self.assertEqual(st["count"], 1)
        self.assertNotIn(API_KEY, self.state.read_text(encoding="utf-8"))

    def test_count_survives_a_restart(self):
        first = self.client(FakeResponse(200, payload()))
        first.lookup(self.cfg(), "EXA1")
        second = self.client(FakeResponse(200, payload()))
        self.assertEqual(second.status(self.cfg())["count"], 1)
        second.lookup(self.cfg(), "EXA2")
        self.assertEqual(self.http.calls_made, 2)
        self.assertEqual(self.client().status(self.cfg())["count"], 2)

    def test_missing_or_corrupt_state_file_is_a_fresh_start(self):
        self.assertEqual(FlightAware(self.state, http_get=self.http,
                                     clock=self.clock).status(self.cfg())["count"], 0)
        self.state.write_text("{not json at all", encoding="utf-8")
        st = FlightAware(self.state, http_get=self.http, clock=self.clock).status(self.cfg())
        self.assertEqual(st["count"], 0)
        self.assertEqual(st["month"], "2026-11")

    def test_new_month_resets_the_count(self):
        self.seeded(month="2026-10", count=899)
        fa = self.client(FakeResponse(200, payload(flight())))
        st = fa.status(self.cfg())
        self.assertEqual((st["month"], st["count"], st["paused"]),
                         ("2026-11", 0, False))
        fa.lookup(self.cfg(), CS)
        self.assertEqual(json.loads(self.state.read_text())["month"], "2026-11")

    def test_rolling_state_keeps_key_identity(self):
        self.seeded(month="2026-10", count=5, key_state="invalid", tested_at=NOW)
        fa = self.client()
        st = fa.status(self.cfg())
        self.assertEqual(st["key_state"], "invalid")   # key unchanged → still distrusted
        self.assertEqual(st["tested_at"], NOW)


# --- key health -------------------------------------------------------------

class TestKeyState(FlightAwareTestCase):
    def test_rejected_key_stops_all_further_network_traffic(self):
        cfg = self.cfg()
        fa = self.client(FakeResponse(401, {"status": 401, "reason": "NotAuthorized"}),
                         FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(cfg, CS)["status"], "invalid_key")
        self.assertEqual(fa.status(cfg)["count"], 1)      # the 401 was billed
        self.assertEqual(fa.status(cfg)["key_state"], "invalid")
        self.assertEqual(fa.lookup(cfg, "EXA413")["status"], "invalid_key")
        self.assertEqual(self.http.calls_made, 1)         # no second request

    def test_403_invalidates_the_same_way(self):
        cfg = self.cfg()
        fa = self.client(FakeResponse(403, {"status": 403, "reason": "NotAllowed"}))
        self.assertEqual(fa.lookup(cfg, CS)["status"], "invalid_key")
        self.assertEqual(fa.status(cfg)["key_state"], "invalid")

    def test_invalid_key_is_persisted_and_read_on_construction(self):
        cfg = self.cfg()
        self.seeded(key_state="invalid")
        fa = self.client(FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(cfg, CS)["status"], "invalid_key")
        self.assertEqual(self.http.calls_made, 0)

    def test_a_replaced_key_resumes_lookups(self):
        dead = self.cfg(key="example-dead-key-00000")
        fa = self.client(FakeResponse(401, {}), FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(dead, CS)["status"], "invalid_key")
        fresh = self.cfg(key="example-live-key-99999")
        self.assertEqual(fa.status(fresh)["key_state"], "untested")
        self.assertEqual(fa.lookup(fresh, CS)["status"], "found")
        self.assertEqual(self.http.calls_made, 2)

    def test_state_file_stores_only_a_key_fingerprint(self):
        import hashlib
        fa = self.client(FakeResponse(401, {}))
        fa.lookup(self.cfg(), CS)
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(st["key_fp"],
                         hashlib.sha256(API_KEY.encode()).hexdigest()[:12])
        self.assertEqual(len(st["key_fp"]), 12)


# --- rate limiting ----------------------------------------------------------

class TestRateLimit(FlightAwareTestCase):
    def _ten_lookups(self, fa, cfg):
        for i in range(RATE_MAX):
            fa.lookup(cfg, f"EXA{i + 1}")
        self.assertEqual(self.http.calls_made, RATE_MAX)

    def test_busy_after_ten_network_lookups_in_60s(self):
        cfg = self.cfg()
        fa = self.client()
        self._ten_lookups(fa, cfg)
        self.assertEqual(fa.lookup(cfg, "EXA99")["status"], "busy")
        self.assertEqual(self.http.calls_made, RATE_MAX)

    def test_rate_window_slides(self):
        cfg = self.cfg()
        fa = self.client()
        self._ten_lookups(fa, cfg)
        self.clock.advance(59)
        self.assertEqual(fa.lookup(cfg, "EXA99")["status"], "busy")
        self.clock.advance(2)                      # the first lookups are now >60s old
        self.assertEqual(fa.lookup(cfg, "EXA99")["status"], "none")
        self.assertEqual(self.http.calls_made, RATE_MAX + 1)

    def test_429_cools_off_for_a_full_minute_and_is_not_billed(self):
        cfg = self.cfg()
        fa = self.client(FakeResponse(429, {"status": 429, "reason": "TooManyRequests"}),
                        FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(cfg, CS)["status"], "busy")
        self.assertEqual(fa.status(cfg)["count"], 0)
        self.assertEqual(fa.lookup(cfg, CS)["status"], "busy")
        self.clock.advance(60)
        self.assertEqual(fa.lookup(cfg, CS)["status"], "found")
        self.assertEqual(self.http.calls_made, 2)


# --- usage (free) and test_key ---------------------------------------------

USAGE_OK = {"usage": {"total_cost": 1.235, "total_discount_cost": 1.230,
                      "total_calls": 247,
                      "resource_details": [
                          {"operation": "GET /account/usage",
                           "total_resource_calls": 13},
                          {"operation": "GET /flights/{ident}",
                           "total_resource_calls": 101}]}}


# v3.4.175: the same figure for every key on the account (all_keys=true)
USAGE_ACCT = {"usage": {"total_cost": 1.300, "total_discount_cost": 1.295,
                        "total_calls": 260,
                        "resource_details": [
                            {"operation": "GET /account/usage",
                             "total_resource_calls": 15},
                            {"operation": "GET /flights/{ident}",
                             "total_resource_calls": 105}]}}


class TestUsage(FlightAwareTestCase):
    def test_usage_parses_the_free_endpoint(self):
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT))
        u = fa.usage(self.cfg())
        self.assertEqual(u, {"total_cost": 1.235, "total_discount_cost": 1.230,
                             "total_calls": 247, "lookups": 101, "as_of": NOW,
                             "account": {"total_cost": 1.300, "total_discount_cost": 1.295,
                                         "total_calls": 260, "lookups": 105}})
        mine, every = self.http.call(0), self.http.call(1)
        self.assertEqual(mine["url"], f"{API}/account/usage")
        self.assertEqual(mine["params"], {"start": "2026-11-01T00:00:00Z"})     # this key
        self.assertEqual(every["url"], f"{API}/account/usage")
        self.assertEqual(every["params"], {"start": "2026-11-01T00:00:00Z", "all_keys": "true"})

    def test_a_failed_account_call_still_returns_this_keys_figure(self):
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(500))
        u = fa.usage(self.cfg())
        self.assertEqual(u["lookups"], 101)
        self.assertIsNone(u["account"])

    def test_usage_is_not_counted_against_the_limit(self):
        cfg = self.cfg(limit=2)
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT),
                        FakeResponse(200, payload()),
                        FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT),
                        FakeResponse(200, payload()))
        fa.usage(cfg)
        fa.lookup(cfg, "EXA1")
        fa.usage(cfg, force=True)
        fa.lookup(cfg, "EXA2")
        self.assertEqual(fa.status(cfg)["count"], 2)
        self.assertEqual(fa.usage(cfg)["total_calls"], 247)

    def test_usage_is_cached(self):
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT),
                        FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT))
        fa.usage(self.cfg())
        fa.usage(self.cfg())
        self.assertEqual(self.http.calls_made, 2)          # this key + every key
        self.clock.advance(USAGE_TTL_S)
        fa.usage(self.cfg())
        self.assertEqual(self.http.calls_made, 4)

    def test_force_refetches(self):
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT),
                        FakeResponse(200, {"usage": {"total_calls": 3}}), FakeResponse(200, USAGE_ACCT))
        fa.usage(self.cfg())
        self.assertEqual(fa.usage(self.cfg(), force=True)["total_calls"], 3)
        self.assertEqual(self.http.calls_made, 4)

    def test_usage_errors_return_none(self):
        import requests
        for bad in (FakeResponse(500), FakeResponse(401, {}),
                    requests.exceptions.ConnectionError("dns")):
            http = FakeHTTP()
            http.queue(bad)
            fa = FlightAware(self.state, http_get=http, clock=self.clock)
            self.assertIsNone(fa.usage(self.cfg()))

    def test_usage_without_a_key_is_none_and_silent(self):
        fa = self.client(FakeResponse(200, USAGE_OK))
        self.assertIsNone(fa.usage(None))
        self.assertIsNone(fa.usage({"api_key": ""}))
        self.assertEqual(self.http.calls_made, 0)


class TestBilledLookups(unittest.TestCase):
    def test_no_resource_details_is_none(self):
        self.assertIsNone(flightaware.billed_lookups({"total_calls": 247}))
        self.assertIsNone(flightaware.billed_lookups({"resource_details": "nope"}))

    def test_skips_the_free_usage_operation(self):
        self.assertEqual(flightaware.billed_lookups(USAGE_OK["usage"]), 101)

    def test_sums_several_billed_operations(self):
        usage = {"resource_details": [
            {"operation": "GET /account/usage", "total_resource_calls": 13},
            {"operation": "GET /flights/{ident}", "total_resource_calls": 101},
            {"operation": "GET /flights/{ident}/history", "total_resource_calls": 20},
            {"operation": "GET /aircraft/{ident}/position", "total_resource_calls": "nope"}]}
        self.assertEqual(flightaware.billed_lookups(usage), 121)


class TestKeyCount(FlightAwareTestCase):
    """v3.4.175: this month's lookups with the CURRENT key, so the card can put
    FlightAware's per-key figure beside the matching count after a key change."""

    def test_counts_with_the_month_on_a_fresh_install(self):
        fa = self.client(FakeResponse(200, payload()), FakeResponse(200, payload()))
        fa.lookup(self.cfg(), "EXA1")
        fa.lookup(self.cfg(), "EXA2")
        st = fa.status(self.cfg())
        self.assertEqual((st["count"], st["key_count"]), (2, 2))

    def test_a_new_key_starts_from_zero_but_the_month_carries_on(self):
        fa = self.client(FakeResponse(200, payload()), FakeResponse(200, payload()))
        fa.lookup(self.cfg(), "EXA1")
        other = self.cfg(key="exampleKEY-0000-other")
        fa.lookup(other, "EXA2")
        st = fa.status(other)
        self.assertEqual((st["count"], st["key_count"]), (2, 1))

    def test_an_unbilled_failure_gives_both_back(self):
        import requests
        fa = self.client(FakeResponse(200, payload()), requests.exceptions.ConnectionError("dns"))
        fa.lookup(self.cfg(), "EXA1")
        fa.lookup(self.cfg(), "EXA2")
        st = fa.status(self.cfg())
        self.assertEqual((st["count"], st["key_count"]), (1, 1))

    def test_a_new_month_resets_both(self):
        fa = self.client(FakeResponse(200, payload()))
        fa.lookup(self.cfg(), "EXA1")
        self.clock.advance(31 * 86400)
        st = fa.status(self.cfg())
        self.assertEqual((st["count"], st["key_count"]), (0, 0))

    def test_an_older_state_file_is_unknown_until_flightaware_answers(self):
        self.seeded(count=28)
        fa = self.client()
        self.assertIsNone(fa.status(self.cfg())["key_count"])

    def test_seed_after_a_key_change_uses_this_keys_figure(self):
        # Other keys were used this month, so FlightAware's figure for this key is the better guess.
        self.seeded(count=28)
        mine = {"usage": {"resource_details": [{"operation": "GET /flights/{ident}", "total_resource_calls": 3}]}}
        fa = self.client(FakeResponse(200, mine), FakeResponse(200, USAGE_ACCT))
        fa.usage(self.cfg())
        self.assertEqual(fa.status(self.cfg())["key_count"], 3)

    def test_seed_with_one_key_uses_the_months_count(self):
        self.seeded(count=28)
        same = {"usage": {"resource_details": [{"operation": "GET /flights/{ident}", "total_resource_calls": 27}]}}
        fa = self.client(FakeResponse(200, same), FakeResponse(200, same))
        fa.usage(self.cfg())
        self.assertEqual(fa.status(self.cfg())["key_count"], 28)

    def test_seed_waits_for_the_account_figure(self):
        self.seeded(count=28)
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(500))
        fa.usage(self.cfg())
        self.assertIsNone(fa.status(self.cfg())["key_count"])

    def test_a_known_count_is_never_reseeded(self):
        fa = self.client(FakeResponse(200, payload()), FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT))
        fa.lookup(self.cfg(), "EXA1")
        fa.usage(self.cfg())
        self.assertEqual(fa.status(self.cfg())["key_count"], 1)


class TestTestKey(FlightAwareTestCase):
    def test_ok_clears_invalid_and_stamps_tested_at(self):
        cfg = self.cfg()
        self.seeded(key_state="invalid", tested_at=NOW - 999)
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT))
        r = fa.test_key(cfg)
        self.assertEqual(r["state"], "ok")
        self.assertEqual(r["message"], "Key accepted. This key: 101 lookups billed this month. Whole account: 105.")
        self.assertNotIn("all_keys", self.http.call(0)["params"])          # the key itself decides
        self.assertEqual(self.http.call(1)["params"]["all_keys"], "true")
        st = fa.status(cfg)
        self.assertEqual((st["key_state"], st["tested_at"]), ("ok", NOW))
        self.assertEqual(fa.lookup(cfg, CS)["status"], "none")   # lookups work again

    def test_ok_with_one_key_names_only_this_key(self):
        for account in (FakeResponse(200, USAGE_OK), FakeResponse(500)):
            http = FakeHTTP()
            http.queue(FakeResponse(200, USAGE_OK), account)
            r = FlightAware(self.state, http_get=http, clock=self.clock).test_key(self.cfg())
            self.assertEqual(r["message"], "Key accepted. 101 lookups billed to this key this month.")

    def test_ok_without_resource_details_is_just_key_accepted(self):
        fa = self.client(FakeResponse(200, {"usage": {"total_calls": 5}}))
        r = fa.test_key(self.cfg())
        self.assertEqual(r["state"], "ok")
        self.assertEqual(r["message"], "Key accepted.")

    def test_invalid_is_persisted_and_stops_lookups(self):
        cfg = self.cfg()
        fa = self.client(FakeResponse(401, {"status": 401}),
                        FakeResponse(200, payload(flight())))
        r = fa.test_key(cfg)
        self.assertEqual(r["state"], "invalid")
        self.assertEqual(self.http.calls_made, 1)
        self.assertEqual(fa.lookup(cfg, CS)["status"], "invalid_key")
        self.assertEqual(json.loads(self.state.read_text())["key_state"], "invalid")

    def test_unreachable_is_error_state(self):
        import requests
        cfg = self.cfg()
        fa = self.client(requests.exceptions.ConnectionError("dns"))
        r = fa.test_key(cfg)
        self.assertEqual(r["state"], "error")
        self.assertEqual(fa.status(cfg)["key_state"], "untested")

    def test_no_key_configured_is_error_state(self):
        fa = self.client()
        r = fa.test_key({})
        self.assertEqual(r["state"], "error")
        self.assertEqual(self.http.calls_made, 0)


# --- status -----------------------------------------------------------------

class TestStatus(FlightAwareTestCase):
    def test_status_fields(self):
        cfg = self.cfg(limit=400)
        fa = self.client(FakeResponse(200, payload(flight())),
                        FakeResponse(200, payload()))
        fa.lookup(cfg, CS)
        fa.lookup(cfg, "EXA413")
        st = fa.status(cfg)
        self.assertEqual(st.pop("last_lookup"), {"ts": NOW, "callsign": "EXA413", "status": "none",
                                                 "origin": None, "destination": None})
        self.assertEqual(st, {
            "configured": True, "key_last4": "e5f6", "key_state": "untested",
            "tested_at": None, "month": "2026-11", "count": 2, "key_count": 2, "limit": 400,
            "paused": False, "resets_on": "2026-12-01",
            "cost_estimate": round(2 * LOOKUP_COST_USD, 4),
            "limit_cost": round(400 * LOOKUP_COST_USD, 4)})

    def test_status_without_a_key(self):
        st = self.client().status({})
        self.assertEqual(st["configured"], False)
        self.assertIsNone(st["key_last4"])
        self.assertEqual(st["key_state"], "untested")
        self.assertEqual(st["count"], 0)

    def test_status_resets_on_rolls_the_year(self):
        self.clock.ts = float(epoch(2026, 12, 31, 23, 0))
        st = self.client().status(self.cfg())
        self.assertEqual((st["month"], st["resets_on"]),
                         ("2026-12", "2027-01-01"))

    def test_cost_estimate_tracks_billing(self):
        cfg = self.cfg(limit=2)
        fa = self.client(FakeResponse(200, payload()), FakeResponse(200, payload()))
        fa.lookup(cfg, "EXA1")
        self.assertAlmostEqual(fa.status(cfg)["cost_estimate"], LOOKUP_COST_USD, places=6)
        self.assertEqual(fa.status(cfg)["limit_cost"],
                         round(2 * LOOKUP_COST_USD, 4))


# --- the key never leaves the process ----------------------------------------

class TestKeyPrivacy(FlightAwareTestCase):
    def test_key_is_absent_from_every_log_line(self):
        import requests
        cfg = self.cfg()
        with self.assertLogs("flightaware", level="INFO") as captured:
            for bad in (FakeResponse(500, {"status": 500}),
                        requests.exceptions.ConnectionError("boom"),
                        FakeResponse(429, {"status": 429}),
                        FakeResponse(401, {"status": 401}),
                        FakeResponse(403, {"status": 403})):
                self.client(bad).lookup(cfg, CS)      # fresh client per scenario
            fa = self.client(FakeResponse(500, {"status": 500}))
            self.assertIsNone(fa.usage(cfg))
            self.assertEqual(fa.test_key(cfg)["state"], "error")
        output = "\n".join(captured.output)
        self.assertTrue(output)
        self.assertIn(API_KEY, json.dumps(self.http.calls))   # it really was sent
        self.assertNotIn(API_KEY, output)
        self.assertNotIn(API_KEY, json.dumps(fa.status(cfg)))
        self.assertNotIn(API_KEY, repr(fa.status(cfg)))

    def test_key_is_absent_from_every_message_the_module_returns(self):
        import requests
        leaks = f"connection reset while sending {API_KEY}"
        outcomes = [self.client(requests.exceptions.ConnectionError(leaks)).lookup(self.cfg(), CS),
                    self.client(FakeResponse(403, {"status": 403})).lookup(self.cfg(), CS),
                    self.client(FakeResponse(500, {"status": 500})).lookup(self.cfg(), CS),
                    self.client(FakeResponse(403, {"status": 403})).test_key(self.cfg()),
                    self.client(requests.exceptions.ConnectionError(leaks)).test_key(self.cfg())]
        text = json.dumps(outcomes, default=str)
        self.assertIn("error", text)                  # they really did fail
        self.assertNotIn(API_KEY, text)

    def test_status_never_carries_the_key(self):
        fa = self.client()
        st = fa.status(self.cfg())
        self.assertEqual(st["key_last4"], API_KEY[-4:])
        self.assertNotIn(API_KEY, json.dumps(st))


class TestThreading(FlightAwareTestCase):
    def test_concurrent_lookups_never_overshoot_the_limit(self):
        import threading
        cfg = self.cfg(limit=2)
        fa = self.client()                     # every call answers 404 (billed)
        results = []
        started = threading.Barrier(4)

        def worker(n):
            started.wait()
            results.append(fa.lookup(cfg, f"EXA{n}"))

        threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2, 3, 4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([r["status"] for r in results].count("none"), 2)
        self.assertEqual([r["status"] for r in results].count("paused"), 2)
        self.assertEqual(fa.status(cfg)["count"], 2)
        self.assertEqual(self.http.calls_made, 2)


class TestStatusPageNumbers(FlightAwareTestCase):
    """The Status page's card (v3.4.169): the last lookup sent to FlightAware,
    and FlightAware's usage figure from the cache only, never a call."""

    def test_the_last_lookup_names_the_flight_and_its_route(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        self.assertIsNone(fa.status(self.cfg())["last_lookup"])
        fa.lookup(self.cfg(), CS)
        self.assertEqual(fa.status(self.cfg())["last_lookup"],
                         {"ts": NOW, "callsign": CS, "status": "found", "origin": "KEXA", "destination": "KEXB"})

    def test_a_cache_hit_isnt_a_lookup(self):
        fa = self.client(FakeResponse(200, payload(flight())))
        fa.lookup(self.cfg(), CS)
        self.clock.advance(60)
        fa.lookup(self.cfg(), CS)
        self.assertEqual(fa.status(self.cfg())["last_lookup"]["ts"], NOW)

    def test_a_refused_key_is_the_last_lookup(self):
        fa = self.client(FakeResponse(401, {"status": 401}))
        fa.lookup(self.cfg(), CS)
        self.assertEqual(fa.status(self.cfg())["last_lookup"]["status"], "invalid_key")

    def test_cached_usage_never_calls(self):
        fa = self.client(FakeResponse(200, USAGE_OK), FakeResponse(200, USAGE_ACCT))
        self.assertIsNone(fa.cached_usage())
        fa.usage(self.cfg())
        self.clock.advance(3600)                      # long past USAGE_TTL_S
        self.assertEqual(fa.cached_usage()["total_cost"], 1.235)
        self.assertEqual(fa.cached_usage()["account"]["lookups"], 105)
        self.assertEqual(self.http.calls_made, 2)


class TestReviewFixes(FlightAwareTestCase):
    """Fixed in review: AeroAPI's airport fields are origin/destination; a
    cached answer is free, so it's served even while paused; checking status
    with no key doesn't forget a tested key; one shared client per install."""

    def test_an_aeroapi_shaped_flight_parses(self):
        # Field names as in AeroAPI 4.30's FlightsResponse (invented values).
        aero = {"ident": "EXA412", "ident_icao": "EXA412", "fa_flight_id": "EXA412-1794000000-airline-0001",
                "registration": "N412EX", "cancelled": False, "diverted": False, "position_only": False,
                "actual_off": "2026-11-06T11:30:00Z", "actual_on": None,
                "estimated_on": "2026-11-06T15:05:00Z", "scheduled_on": "2026-11-06T15:00:00Z",
                "origin": {"code": "KBOS", "code_icao": "KBOS", "code_iata": "BOS", "code_lid": "BOS",
                           "timezone": "America/New_York", "name": "Boston Logan Intl", "city": "Boston",
                           "airport_info_url": "/airports/KBOS"},
                "destination": {"code": "KDEN", "code_icao": "KDEN", "code_iata": "DEN", "code_lid": "DEN",
                                "timezone": "America/Denver", "name": "Denver Intl", "city": "Denver",
                                "airport_info_url": "/airports/KDEN"}}
        fa = self.client(FakeResponse(200, payload(aero)))
        r = fa.lookup(self.cfg(), CS)
        self.assertEqual(r["status"], "found")
        self.assertEqual((r["route"]["origin"]["icao"], r["route"]["origin"]["city"]), ("KBOS", "Boston"))
        self.assertEqual((r["route"]["destination"]["icao"], r["route"]["destination"]["city"]), ("KDEN", "Denver"))

    def test_a_cached_route_is_served_while_paused(self):
        cfg = self.cfg(limit=1)
        fa = self.client(FakeResponse(200, payload(flight())))
        self.assertEqual(fa.lookup(cfg, CS)["status"], "found")
        self.assertTrue(fa.status(cfg)["paused"])
        self.assertEqual(fa.lookup(cfg, CS)["status"], "found")
        self.assertEqual(fa.lookup(cfg, "EXA999")["status"], "paused")
        self.assertEqual(self.http.calls_made, 1)

    def test_status_without_a_key_keeps_the_tested_state(self):
        cfg = self.cfg()
        fa = self.client(FakeResponse(200, USAGE_OK))
        self.assertEqual(fa.test_key(cfg)["state"], "ok")
        self.assertFalse(fa.status(self.cfg(key=None))["configured"])
        self.assertEqual(fa.status(cfg)["key_state"], "ok")

    def test_one_shared_client_per_state_file(self):
        path = flightaware.state_path_for({"data": {"db_file": str(self.state.parent / "history.db")}})
        self.assertEqual(path, str(self.state.parent.resolve() / ".flightaware-state.json"))
        self.assertIs(flightaware.shared(path), flightaware.shared(path))
        other = flightaware.state_path_for({"data": {"db_file": str(self.state.parent / "b" / "history.db")}})
        self.assertIsNot(flightaware.shared(path), flightaware.shared(other))


if __name__ == "__main__":
    unittest.main(verbosity=2)
