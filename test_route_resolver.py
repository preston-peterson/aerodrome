"""
Unit tests for route_resolver.py (callsign→route enrichment).

All adsbdb HTTP is mocked — no network. Exercises the cache-first logic:
parse+store a hit, serve a fresh hit/miss from cache without re-fetching,
negative-cache a 404, NOT cache a transient error (so it retries), reject an
invalid callsign without fetching, and re-fetch a stale entry.
"""
import sqlite3
import time
import unittest
from unittest import mock

from schema_migrations import _migration_v12_route_cache
import route_resolver

HIT_PAYLOAD = {"response": {"flightroute": {
    "callsign": "SWA2178",
    "airline": {"name": "Southwest Airlines", "icao": "SWA"},
    "origin": {"icao_code": "KSJC", "municipality": "San Jose",
               "name": "Norman Y. Mineta San Jose International Airport"},
    "destination": {"icao_code": "KLAX", "municipality": "Los Angeles",
                    "name": "Los Angeles International Airport"},
}}}


def _conn():
    c = sqlite3.connect(":memory:")
    _migration_v12_route_cache(c)
    return c


def _resp(status, payload=None):
    m = mock.Mock()
    m.status_code = status
    m.json.return_value = payload or {}
    if status >= 400 and status != 404:
        m.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    else:
        m.raise_for_status.return_value = None
    return m


class TestRouteResolver(unittest.TestCase):
    def setUp(self):
        route_resolver._stats.update(
            attempts=0, hits=0, misses=0, errors=0, last_error=None)

    def test_hit_parses_and_caches(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, HIT_PAYLOAD)):
            r = route_resolver.resolve_route(c, "swa2178")  # lowercase input
        self.assertTrue(r["found"])
        self.assertFalse(r["cached"])
        self.assertEqual(r["callsign"], "SWA2178")
        self.assertEqual(r["origin_icao"], "KSJC")
        self.assertEqual(r["origin_name"], "San Jose")
        self.assertEqual(r["dest_icao"], "KLAX")
        self.assertEqual(r["airline"], "Southwest Airlines")
        self.assertEqual(
            c.execute("SELECT last_outcome, origin_icao FROM route_cache "
                      "WHERE callsign='SWA2178'").fetchone(),
            ("hit", "KSJC"))

    def test_fresh_cache_hit_skips_fetch(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, HIT_PAYLOAD)) as g:
            route_resolver.resolve_route(c, "SWA2178")
            r2 = route_resolver.resolve_route(c, "SWA2178")
            self.assertEqual(g.call_count, 1)  # 2nd call served from cache
        self.assertTrue(r2["found"])
        self.assertTrue(r2["cached"])

    def test_404_negative_caches(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(404)):
            r = route_resolver.resolve_route(c, "N900RH")
        self.assertFalse(r["found"])
        self.assertEqual(
            c.execute("SELECT last_outcome, origin_icao FROM route_cache "
                      "WHERE callsign='N900RH'").fetchone(),
            ("miss", None))

    def test_negative_cache_hit_skips_fetch(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(404)) as g:
            route_resolver.resolve_route(c, "N900RH")
            r2 = route_resolver.resolve_route(c, "N900RH")
            self.assertEqual(g.call_count, 1)
        self.assertFalse(r2["found"])
        self.assertTrue(r2["cached"])

    def test_transient_5xx_not_cached(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(503)):
            r = route_resolver.resolve_route(c, "SWA9999")
        self.assertFalse(r["found"])
        self.assertFalse(r["cached"])
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM route_cache").fetchone()[0], 0)

    def test_network_exception_not_cached(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               side_effect=RuntimeError("timeout")):
            r = route_resolver.resolve_route(c, "SWA9999")
        self.assertFalse(r["found"])
        self.assertFalse(r["cached"])
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM route_cache").fetchone()[0], 0)

    def test_invalid_callsign_no_fetch(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get") as g:
            r = route_resolver.resolve_route(c, "bad/callsign!")
            self.assertFalse(g.called)
        self.assertFalse(r["found"])

    def test_stale_entry_refetches(self):
        c = _conn()
        old = int(time.time()) - 40 * 86400   # older than the 30-day positive TTL
        c.execute("INSERT INTO route_cache (callsign, origin_icao, dest_icao, "
                  "resolved_at, last_outcome, hit_count) "
                  "VALUES ('SWA2178','KOLD','KOLD',?,'hit',1)", (old,))
        c.commit()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, HIT_PAYLOAD)) as g:
            r = route_resolver.resolve_route(c, "SWA2178")
            self.assertTrue(g.called)            # stale → refetched
        self.assertEqual(r["origin_icao"], "KSJC")  # fresh value, not KOLD


if __name__ == "__main__":
    unittest.main(verbosity=2)
