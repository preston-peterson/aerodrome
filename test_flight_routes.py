"""FlightAware route history (v3.4.171): schema v19's flight_routes table on a
fresh install and on an upgrade, the write path (a lookup that finds a flight
records it; a cache hit doesn't), and the read path (the aircraft page's
Sightings rows carry the route of the flight they belong to). Callsigns,
airports and hexes are invented.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collector
import flightaware
import server
from schema_migrations import CURRENT_SCHEMA_VERSION, _migration_v19_flight_routes, apply_schema_migrations

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub

REPO = Path(__file__).resolve().parent
COLS = {"fa_flight_id", "callsign", "registration", "origin_icao", "origin_name", "dest_icao",
        "dest_name", "departed_at", "arrives_at", "looked_up_at"}
ROUTE = {"origin": {"icao": "KBOS", "name": "Boston Logan Intl", "city": "Boston"},
         "destination": {"icao": "KDEN", "name": "Denver Intl", "city": "Denver"},
         "departed_at": 1794000000, "arrives_at": 1794015000,
         "fa_flight_id": "EXA412-1794000000-airline-0001", "registration": "N412EX"}


class _Db(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class TestMigration(_Db):

    def test_a_fresh_install_has_the_table_and_its_index(self):
        self.assertEqual({r[1] for r in self.q("PRAGMA table_info(flight_routes)")}, COLS)
        self.assertIn(("idx_flight_routes_callsign",),
                      self.q("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='flight_routes'"))
        self.assertEqual(self.q("SELECT MAX(version) FROM schema_version")[0][0], CURRENT_SCHEMA_VERSION)

    def test_a_rerun_keeps_its_rows(self):
        flightaware.record_route(self.db, "EXA412", ROUTE)
        conn = sqlite3.connect(self.db)
        _migration_v19_flight_routes(conn)
        conn.commit()
        conn.close()
        self.assertEqual(self.q("SELECT COUNT(*) FROM flight_routes")[0][0], 1)

    def test_an_upgrade_from_v18_runs_v19_onward(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE flight_routes")
        conn.execute("DELETE FROM schema_version WHERE version >= 19")
        conn.commit()
        result = apply_schema_migrations(conn, "test")
        conn.close()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual((result["starting_version"], result["ending_version"]), (18, CURRENT_SCHEMA_VERSION))
        self.assertEqual(len(result["applied"]), CURRENT_SCHEMA_VERSION - 18)
        self.assertEqual({r[1] for r in self.q("PRAGMA table_info(flight_routes)")}, COLS)


class TestWritePath(_Db):

    def test_a_found_route_is_recorded_with_derived_fields_only(self):
        flightaware.record_route(self.db, "exa412", ROUTE)
        self.assertEqual(self.q("SELECT callsign, registration, origin_icao, origin_name, dest_icao, dest_name, "
                                "departed_at, arrives_at FROM flight_routes"),
                         [("EXA412", "N412EX", "KBOS", "Boston", "KDEN", "Denver", 1794000000, 1794015000)])

    def test_the_same_flight_again_updates_its_row(self):
        flightaware.record_route(self.db, "EXA412", ROUTE)
        flightaware.record_route(self.db, "EXA412", dict(ROUTE, arrives_at=1794016000))
        self.assertEqual(self.q("SELECT COUNT(*), MAX(arrives_at) FROM flight_routes"), [(1, 1794016000)])

    def test_a_route_with_an_unknown_end_isnt_recorded(self):
        flightaware.record_route(self.db, "EXA412", dict(ROUTE, destination={"icao": None}))
        flightaware.record_route(self.db, "EXA412", dict(ROUTE, fa_flight_id=""))
        self.assertEqual(self.q("SELECT COUNT(*) FROM flight_routes")[0][0], 0)

    def test_the_client_records_a_lookup_but_not_a_cache_hit(self):
        aero = {"flights": [{"fa_flight_id": ROUTE["fa_flight_id"], "registration": "N412EX",
                             "actual_off": "2026-11-06T11:30:00Z", "actual_on": None,
                             "estimated_on": "2026-11-06T15:05:00Z", "cancelled": False,
                             "origin": {"code_icao": "KBOS", "city": "Boston"},
                             "destination": {"code_icao": "KDEN", "city": "Denver"}}]}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = aero
        http = mock.Mock(return_value=resp)
        flightaware._shared = None
        self.addCleanup(setattr, flightaware, "_shared", None)
        client = flightaware.shared(str(self.dir / ".flightaware-state.json"), db_path=self.db)
        client._http_get = http
        cfg = {"api_key": "EXAMPLEkey", "monthly_limit": 50}
        with mock.patch.object(flightaware, "record_route", wraps=flightaware.record_route) as rec:
            self.assertEqual(client.lookup(cfg, "EXA412")["status"], "found")
            self.assertEqual(client.lookup(cfg, "EXA412")["status"], "found")     # cached
        self.assertEqual(http.call_count, 1)
        self.assertEqual(rec.call_count, 1)
        self.assertEqual(self.q("SELECT origin_icao, dest_icao FROM flight_routes"), [("KBOS", "KDEN")])


class TestReadPath(_Db):
    """The aircraft page's Sightings rows (GET /api/all/drill)."""

    def setUp(self):
        super().setUp()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        cfg_path = self.dir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        app = server.get_app(cfg, str(cfg_path))
        self.drill = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/all/drill")
        dep = ROUTE["departed_at"]
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO all_sightings (icao, callsign, seen_at) VALUES (?, ?, ?)", [
            ("A00010", "EXA412", dep + 1800),        # on the looked-up flight
            ("A00010", "EXA412", dep - 86400),       # the same flight number a day earlier
            ("A00010", "EXA977", dep + 1900),        # another flight, never looked up
        ])
        conn.commit()
        conn.close()

    def rows(self):
        out = self.drill(icao="A00010", from_ts=0, to_ts=ROUTE["arrives_at"] + 86400, limit=50, offset=0,
                         order="seen_at", dir="desc")
        return {(r["callsign"], r["seen_at"]): r.get("route") for r in out["sightings"]}

    def test_rows_carry_their_flights_route(self):
        flightaware.record_route(self.db, "EXA412", ROUTE)
        dep = ROUTE["departed_at"]
        got = self.rows()
        self.assertEqual(got[("EXA412", dep + 1800)], {"origin": "KBOS", "dest": "KDEN", "source": "flightaware"})
        self.assertIsNone(got[("EXA412", dep - 86400)], "a day earlier was a different flight")
        self.assertIsNone(got[("EXA977", dep + 1900)])

    def test_no_routes_no_change(self):
        self.assertTrue(all(v is None for v in self.rows().values()))

    def test_a_database_without_the_table_still_answers(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE flight_routes")
        conn.commit()
        conn.close()
        self.assertTrue(all(v is None for v in self.rows().values()))


if __name__ == "__main__":
    unittest.main()
