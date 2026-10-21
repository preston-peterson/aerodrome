"""v3.4.141 collector/live-feed robustness: malformed feed records, the
per-aircraft savepoint, the hourly rollup's last known position, the
query-planner stats refresh, and /api/live's number handling. Runs the real
fetch_and_store against a migrated scratch DB with the receiver mocked.
Hex values and callsigns are invented.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collector
import hexdb_resolver
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
GOOD = {"hex": "a00001", "flight": "DEMO1 ", "lat": 40.0, "lon": -90.0,
        "alt_baro": 12000, "gs": 300, "t": "B738", "squawk": "1200"}


def _feed(aircraft):
    payload = {"now": 0, "aircraft": aircraft}
    resp = mock.Mock(status_code=200, headers={"Content-Type": "application/json"})
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    resp.raise_for_status = lambda: None
    return resp


class _PollCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        patches = [mock.patch.object(hexdb_resolver, "enqueue", return_value=False),
                   mock.patch.object(hexdb_resolver, "start_worker")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def poll(self, aircraft):
        with mock.patch.object(collector.requests, "get", return_value=_feed(aircraft)):
            collector.fetch_and_store(self.cfg, collector.build_watchlist_lookup(self.cfg))

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class TestMalformedRecords(_PollCase):

    CASES = {
        "flight_null": {"flight": None},
        "flight_numeric": {"flight": 1234},
        "hex_numeric": {"hex": 123456},
        "desc_numeric": {"desc": 5},
        "type_numeric": {"t": 737},
        "lat_inf_string": {"lat": "1e999"},
        "alt_inf": {"alt_baro": float("inf")},
        "alt_nan": {"alt_baro": float("nan")},
        "alt_ground": {"alt_baro": "ground"},
    }

    def test_one_bad_record_never_aborts_the_poll(self):
        for name, patch in self.CASES.items():
            with self.subTest(case=name):
                self.setUp()
                bad = dict(GOOD, hex="a00002", flight="DEMO2")
                bad.update(patch)
                self.poll([bad, GOOD])
                icaos = {r[0] for r in self.q("SELECT icao FROM all_sightings")}
                self.assertIn("A00001", icaos)
                self.assertEqual(self.q(
                    "SELECT COUNT(*) FROM all_sightings WHERE altitude > 1e300 "
                    "OR lat > 1e300 OR altitude != altitude"), [(0,)])
                self.tearDown()

    def test_non_finite_numbers_are_none(self):
        for v in (float("inf"), float("-inf"), float("nan"), "1e999", "ground", None, [1]):
            with self.subTest(v=v):
                self.assertIsNone(collector._to_number(v))
        self.assertEqual(collector._to_number("12000"), 12000.0)
        self.assertEqual(collector._to_number(0), 0.0)


class TestPerAircraftSavepoint(_PollCase):

    def test_a_record_that_raises_is_rolled_back_alone(self):
        real = collector._compute_distance_km

        def boom(lat, lon):
            if lat == 41.0:
                raise RuntimeError("synthetic failure")
            return real(lat, lon)

        bad = dict(GOOD, hex="a00002", flight="DEMO2", lat=41.0)
        with mock.patch.object(collector, "_compute_distance_km", side_effect=boom):
            self.poll([bad, GOOD])
        # The failing aircraft's all_sightings + rollup rows (written before
        # the failure point) are rolled back; the good one is stored.
        self.assertEqual(self.q("SELECT icao FROM all_sightings"), [("A00001",)])
        self.assertEqual(self.q("SELECT icao FROM sightings_hourly"), [("A00001",)])
        self.assertEqual(self.q("SELECT icao FROM seen_aircraft"), [("A00001",)])


class TestHourlyLastPosition(_PollCase):

    def test_position_less_poll_keeps_last_known_values(self):
        self.poll([GOOD])
        self.poll([{"hex": "a00001", "flight": "DEMO1", "t": "B738"}])
        self.assertEqual(
            self.q("SELECT sighting_count, last_lat, last_lon, last_altitude, "
                   "last_speed FROM sightings_hourly WHERE icao='A00001'"),
            [(2, 40.0, -90.0, 12000.0, 300.0)])
        moved = dict(GOOD, lat=40.5, lon=-90.5, alt_baro=9000, gs=250)
        self.poll([moved])
        self.assertEqual(
            self.q("SELECT last_lat, last_lon, last_altitude, last_speed "
                   "FROM sightings_hourly WHERE icao='A00001'"),
            [(40.5, -90.5, 9000.0, 250.0)])


class TestQueryPlannerStatsRefresh(_PollCase):

    def test_refresh_updates_stale_stats(self):
        conn = sqlite3.connect(self.db)
        conn.executemany(
            "INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
            [(f"A{i:05X}", 1700000000 + i) for i in range(200)])
        conn.execute("ANALYZE")
        conn.executemany(
            "INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
            [(f"B{i:05X}", 1700001000 + i) for i in range(5000)])
        conn.commit()
        conn.close()

        def stat_rows():
            return int(self.q("SELECT stat FROM sqlite_stat1 "
                              "WHERE tbl='all_sightings' LIMIT 1")[0][0].split()[0])

        self.assertEqual(stat_rows(), 200)
        with mock.patch.object(collector, "_last_stats_optimize_ts", 0.0):
            collector.refresh_query_planner_stats(self.cfg)
        self.assertGreater(stat_rows(), 1000)


class TestLiveEndpoint(unittest.TestCase):
    """/api/live parses the feed itself (it never touches the DB)."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = ":memory:"
        cfg["watchlist"] = []
        app = server.get_app(cfg, os.devnull)
        cls.get_live = staticmethod(next(r.endpoint for r in app.routes
                                         if getattr(r, "path", "") == "/api/live"))

    def test_ground_and_malformed_records(self):
        feed = [dict(GOOD, alt_baro="ground", gs=12),
                dict(GOOD, hex="a00002", flight=None),
                dict(GOOD, hex="a00003", desc=5, alt_baro=float("inf"))]
        with mock.patch.object(self.server.req, "get", return_value=_feed(feed)):
            out = self.get_live()
        by_icao = {a["icao"]: a for a in out["aircraft"]}
        self.assertEqual(set(by_icao), {"A00001", "A00002", "A00003"})
        self.assertIsNone(by_icao["A00001"]["altitude"])      # was "ground" → "NaN ft"
        self.assertEqual(by_icao["A00001"]["speed"], 12.0)
        self.assertEqual(by_icao["A00002"]["callsign"], "")
        self.assertIsNone(by_icao["A00003"]["altitude"])
        json.dumps(out, allow_nan=False)                       # strict-JSON safe


if __name__ == "__main__":
    unittest.main(verbosity=2)
