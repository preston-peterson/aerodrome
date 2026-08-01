"""
Unit tests for route_resolver.py (callsign→route enrichment, adsb.lol
VRS-standing-data source as of v3.4.109).

All HTTP is mocked — no network. Exercises the cache-first logic (parse+store
a hit, serve a fresh hit/miss from cache without re-fetching, negative-cache
a 404, NOT cache a transient error so it retries, reject an invalid callsign
without fetching, re-fetch a stale entry), the multi-leg airport-chain
parsing, and the current-leg inference (cross-track distance + the heading
tie-break that splits an out-and-back's two opposite legs).
"""
import json
import math
import sqlite3
import time
import unittest
from unittest import mock

from schema_migrations import (_migration_v12_route_cache,
                               _migration_v14_route_airports)
import route_resolver

# adsb.lol VRS standing-data payload shapes (vrs-standing-data.adsb.lol).
# _airports is ordered to match airport_codes; a repeated airport appears
# once per position (DAL2688 is a real out-and-back).
KMSP = {"name": "Minneapolis-St Paul International/Wold-Chamberlain Airport",
        "icao": "KMSP", "iata": "MSP", "location": "Minneapolis",
        "countryiso2": "US", "lat": 44.882, "lon": -93.221802}
KPHL = {"name": "Philadelphia International Airport",
        "icao": "KPHL", "iata": "PHL", "location": "Philadelphia",
        "countryiso2": "US", "lat": 39.871899, "lon": -75.241096}
KSJC = {"name": "Norman Y. Mineta San Jose International Airport",
        "icao": "KSJC", "iata": "SJC", "location": "San Jose",
        "countryiso2": "US", "lat": 37.3626, "lon": -121.929001}
KLAX = {"name": "Los Angeles International Airport",
        "icao": "KLAX", "iata": "LAX", "location": "Los Angeles",
        "countryiso2": "US", "lat": 33.942501, "lon": -118.407997}

SINGLE_LEG_PAYLOAD = {
    "callsign": "SWA2178", "number": "2178", "airline_code": "SWA",
    "airport_codes": "KSJC-KLAX", "_airport_codes_iata": "SJC-LAX",
    "_airports": [KSJC, KLAX],
}

MULTI_LEG_PAYLOAD = {
    "callsign": "DAL2688", "number": "2688", "airline_code": "DAL",
    "airport_codes": "KMSP-KPHL-KMSP", "_airport_codes_iata": "MSP-PHL-MSP",
    "_airports": [KMSP, KPHL, KMSP],
}


def _conn():
    c = sqlite3.connect(":memory:")
    _migration_v12_route_cache(c)
    _migration_v14_route_airports(c)
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
                               return_value=_resp(200, SINGLE_LEG_PAYLOAD)):
            r = route_resolver.resolve_route(c, "swa2178")  # lowercase input
        self.assertTrue(r["found"])
        self.assertFalse(r["cached"])
        self.assertEqual(r["callsign"], "SWA2178")
        self.assertEqual(r["origin_icao"], "KSJC")
        self.assertEqual(r["origin_name"], "San Jose")
        self.assertEqual(r["dest_icao"], "KLAX")
        # airline_code mapped to a display name via the local designator table
        self.assertEqual(r["airline"], "Southwest Airlines")
        self.assertEqual(len(r["airports"]), 2)
        self.assertEqual(
            c.execute("SELECT last_outcome, origin_icao FROM route_cache "
                      "WHERE callsign='SWA2178'").fetchone(),
            ("hit", "KSJC"))

    def test_fetch_url_uses_two_char_prefix_shard(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, SINGLE_LEG_PAYLOAD)) as g:
            route_resolver.resolve_route(c, "SWA2178")
        self.assertEqual(
            g.call_args[0][0],
            "https://vrs-standing-data.adsb.lol/routes/SW/SWA2178.json")

    def test_multi_leg_chain_parsed_and_stored(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, MULTI_LEG_PAYLOAD)):
            r = route_resolver.resolve_route(c, "DAL2688")
        self.assertTrue(r["found"])
        self.assertEqual([a["icao"] for a in r["airports"]],
                         ["KMSP", "KPHL", "KMSP"])
        self.assertEqual(r["airports"][1]["name"], "Philadelphia")
        self.assertAlmostEqual(r["airports"][1]["lat"], 39.871899)
        # origin/dest = the chain's first/last stops
        self.assertEqual((r["origin_icao"], r["dest_icao"]), ("KMSP", "KMSP"))
        self.assertEqual(r["airline"], "Delta Air Lines")
        stored = json.loads(c.execute(
            "SELECT airports_json FROM route_cache WHERE callsign='DAL2688'"
        ).fetchone()[0])
        self.assertEqual([a["icao"] for a in stored], ["KMSP", "KPHL", "KMSP"])

    def test_cached_hit_round_trips_airports(self):
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, MULTI_LEG_PAYLOAD)) as g:
            route_resolver.resolve_route(c, "DAL2688")
            r2 = route_resolver.resolve_route(c, "DAL2688")
            self.assertEqual(g.call_count, 1)  # 2nd call served from cache
        self.assertTrue(r2["cached"])
        self.assertEqual([a["icao"] for a in r2["airports"]],
                         ["KMSP", "KPHL", "KMSP"])
        self.assertAlmostEqual(r2["airports"][0]["lat"], 44.882)

    def test_unknown_airline_code_stays_silent(self):
        c = _conn()
        payload = dict(SINGLE_LEG_PAYLOAD, airline_code="ZZQ")
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, payload)):
            r = route_resolver.resolve_route(c, "ZZQ123")
        self.assertTrue(r["found"])
        self.assertEqual(r["airline"], "")   # unrecognized code → no name shown

    def test_payload_without_itinerary_is_a_miss(self):
        # A parseable 200 with no usable airport_codes negative-caches.
        c = _conn()
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, {"callsign": "SWA1"})):
            r = route_resolver.resolve_route(c, "SWA1")
        self.assertFalse(r["found"])
        self.assertEqual(
            c.execute("SELECT last_outcome FROM route_cache "
                      "WHERE callsign='SWA1'").fetchone(), ("miss",))

    def test_misaligned_airports_list_falls_back_to_codes(self):
        # _airports shorter than airport_codes → codes-only entries rather
        # than names mis-aligned to the wrong stops.
        c = _conn()
        payload = dict(MULTI_LEG_PAYLOAD, _airports=[KMSP])
        with mock.patch.object(route_resolver.requests, "get",
                               return_value=_resp(200, payload)):
            r = route_resolver.resolve_route(c, "DAL2688")
        self.assertTrue(r["found"])
        self.assertEqual([a["icao"] for a in r["airports"]],
                         ["KMSP", "KPHL", "KMSP"])
        self.assertTrue(all(a["name"] is None and a["lat"] is None
                            for a in r["airports"]))

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
                               return_value=_resp(200, SINGLE_LEG_PAYLOAD)) as g:
            r = route_resolver.resolve_route(c, "SWA2178")
            self.assertTrue(g.called)            # stale → refetched
        self.assertEqual(r["origin_icao"], "KSJC")  # fresh value, not KOLD

    def test_pre_v14_row_shape_still_serves(self):
        # A fresh 'hit' row with NULL airports_json (pre-v14 shape) rebuilds
        # a 2-stop chain from the origin/dest columns instead of crashing.
        c = _conn()
        c.execute("INSERT INTO route_cache (callsign, origin_icao, origin_name, "
                  "dest_icao, dest_name, airline, resolved_at, last_outcome, "
                  "hit_count) VALUES ('SWA2178','KSJC','San Jose','KLAX',"
                  "'Los Angeles','Southwest Airlines',?,'hit',0)",
                  (int(time.time()),))
        c.commit()
        with mock.patch.object(route_resolver.requests, "get") as g:
            r = route_resolver.resolve_route(c, "SWA2178")
            self.assertFalse(g.called)
        self.assertTrue(r["found"])
        self.assertEqual([a["icao"] for a in r["airports"]], ["KSJC", "KLAX"])


class TestPickCurrentLeg(unittest.TestCase):
    """Current-leg inference for multi-leg chains. The out-and-back
    (KMSP-KPHL-KMSP) is the canonical hard case: both legs are the SAME
    great-circle path in opposite directions, so position can never split
    them — only the heading tie-break can."""

    CHAIN = [
        {"icao": "KMSP", "name": "Minneapolis", "lat": 44.882, "lon": -93.221802},
        {"icao": "KPHL", "name": "Philadelphia", "lat": 39.871899, "lon": -75.241096},
        {"icao": "KMSP", "name": "Minneapolis", "lat": 44.882, "lon": -93.221802},
    ]
    # A distinct 3-stop chain where the legs are different paths.
    CHAIN_LSHAPE = [
        {"icao": "KSJC", "name": "San Jose", "lat": 37.3626, "lon": -121.929001},
        {"icao": "KLAX", "name": "Los Angeles", "lat": 33.942501, "lon": -118.407997},
        {"icao": "KPHL", "name": "Philadelphia", "lat": 39.871899, "lon": -75.241096},
    ]
    # Roughly mid-path between KMSP and KPHL (over lower Michigan).
    MIDPATH = (42.6, -84.5)

    def _track_toward(self, lat, lon, ap):
        """A plane heading from (lat, lon) toward airport `ap`, in degrees."""
        brg = route_resolver._bearing_rad(lat, lon, ap["lat"], ap["lon"])
        return math.degrees(brg) % 360.0

    def test_two_stop_chain_is_trivially_leg_zero(self):
        self.assertEqual(
            route_resolver.pick_current_leg(self.CHAIN[:2], 40.0, -85.0), 0)

    def test_no_position_returns_none(self):
        self.assertIsNone(route_resolver.pick_current_leg(self.CHAIN, None, None))

    def test_missing_airport_coords_returns_none(self):
        chain = [dict(a) for a in self.CHAIN]
        chain[1]["lat"] = None
        self.assertIsNone(
            route_resolver.pick_current_leg(chain, *self.MIDPATH))

    def test_out_and_back_without_track_is_ambiguous(self):
        # Both legs are the same path — position alone can't split them.
        lat, lon = self.MIDPATH
        self.assertIsNone(route_resolver.pick_current_leg(self.CHAIN, lat, lon))

    def test_out_and_back_track_picks_outbound_leg(self):
        lat, lon = self.MIDPATH
        tr = self._track_toward(lat, lon, self.CHAIN[1])   # heading to KPHL
        self.assertEqual(
            route_resolver.pick_current_leg(self.CHAIN, lat, lon, tr), 0)

    def test_out_and_back_track_picks_return_leg(self):
        lat, lon = self.MIDPATH
        tr = self._track_toward(lat, lon, self.CHAIN[2])   # heading to KMSP
        self.assertEqual(
            route_resolver.pick_current_leg(self.CHAIN, lat, lon, tr), 1)

    def test_distinct_legs_position_alone_decides(self):
        # Over Nevada, on the KLAX→KPHL leg's path and nowhere near
        # KSJC→KLAX — no track needed.
        self.assertEqual(
            route_resolver.pick_current_leg(self.CHAIN_LSHAPE, 36.1, -112.0), 1)

    def test_far_from_every_leg_returns_none(self):
        # A plane in Florida isn't on any leg of the MSP↔PHL out-and-back —
        # bad route data for this plane; don't pick a leg from it.
        self.assertIsNone(
            route_resolver.pick_current_leg(self.CHAIN, 27.9, -82.5, 300.0))

    def test_on_ground_at_shared_airport_is_ambiguous(self):
        # At KMSP itself, heading somewhere unhelpful (perpendicular to the
        # path): departing leg 0 vs just-arrived leg 1 stays ambiguous.
        perp = (self._track_toward(44.882, -93.221802, self.CHAIN[1]) + 90) % 360
        self.assertIsNone(
            route_resolver.pick_current_leg(self.CHAIN, 44.882, -93.221802, perp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
