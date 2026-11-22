"""Flights for the aircraft page's list and replay (3.4.198): the rule that splits
an aircraft's positions into flights, the takeoffs and landings that label them,
and the two API answers the page reads."""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import yaml
from fastapi import HTTPException

import collector
import flights
import server
from schema_migrations import apply_schema_migrations

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")

REPO = Path(__file__).resolve().parent
MIN = 60


def run(start, n, alt, step=30):
    """n fixes a poll apart at one altitude: [(seen_at, altitude)]."""
    return [(start + i * step, alt) for i in range(n)]


class TestSplit(unittest.TestCase):
    """The cases measured on a real receiver before the rule was chosen."""

    def count(self, fixes):
        return len(flights.split(fixes))

    def test_one_poll_after_another_is_one_flight(self):
        self.assertEqual(flights.split(run(0, 40, 9000)), [(0, 39)])

    def test_an_airliner_out_of_range_for_24_min_at_cruise_is_still_one_flight(self):
        first = run(0, 10, 36000)
        self.assertEqual(self.count(first + run(first[-1][0] + 24 * MIN, 10, 36000)), 1)

    def test_out_of_range_for_over_30_min_ends_a_flight_even_at_cruise(self):
        first = run(0, 10, 36000)
        self.assertEqual(self.count(first + run(first[-1][0] + 31 * MIN, 10, 36000)), 2)

    def test_back_15_min_after_it_was_last_seen_low_is_a_new_flight(self):
        landed = run(0, 10, 3000) + [(300, 1500)]
        self.assertEqual(self.count(landed + run(300 + 15 * MIN, 10, 1200)), 2)

    def test_low_on_either_side_of_the_gap_counts(self):
        high = run(0, 10, 8000)
        self.assertEqual(self.count(high + run(high[-1][0] + 12 * MIN, 10, 1800)), 2)

    def test_pattern_work_with_short_dropouts_stays_one_flight(self):
        circuits = run(0, 8, 1200) + run(4 * MIN + 210, 8, 1300) + run(9 * MIN + 420, 8, 1100)
        self.assertEqual(self.count(circuits), 1)

    def test_no_altitude_is_on_the_ground(self):
        ground = [(0, None), (30, None)]
        self.assertEqual(self.count(ground + run(30 + 11 * MIN, 5, 2600)), 2)

    def test_exactly_at_the_limits_does_not_split(self):
        self.assertEqual(self.count([(0, 1000), (10 * MIN, 1000)]), 1)
        self.assertEqual(self.count([(0, 30000), (30 * MIN, 30000)]), 1)
        self.assertEqual(self.count([(0, 1000), (10 * MIN + 1, 1000)]), 2)
        self.assertEqual(self.count([(0, 30000), (30 * MIN + 1, 30000)]), 2)

    def test_no_positions_is_no_flights(self):
        self.assertEqual(flights.split([]), [])


class _Db(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        self.now = int(time.time()) // MIN * MIN
        eps = {r.path: r.endpoint for r in server.get_app(self.cfg, os.devnull).routes if hasattr(r, "endpoint")}
        self.flights_ep = eps["/api/aircraft/{icao}/flights"]
        self.positions_ep = eps["/api/aircraft/{icao}/positions"]

    def execute(self, sql, rows):
        conn = sqlite3.connect(self.db)
        try:
            conn.executemany(sql, rows)
            conn.commit()
        finally:
            conn.close()

    def fly(self, icao, fixes, callsign="EXA1", speed=300.0):
        self.execute("INSERT INTO all_sightings (icao, callsign, speed, lat, lon, altitude, seen_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [(icao, callsign, speed, 40 + i / 1000, -90, alt, t) for i, (t, alt) in enumerate(fixes)])

    def event(self, icao, kind, airport, at, runway="18"):
        self.execute("INSERT INTO airport_events (icao, callsign, kind, airport, runway, at) VALUES (?, ?, ?, ?, ?, ?)",
                     [(icao, "EXA1", kind, airport, runway, at)])


class TestAircraftFlights(_Db):

    def test_flights_come_newest_first_with_what_the_list_shows(self):
        t = self.now - 5 * 3600
        self.fly("A0F101", run(t, 20, 3000) + [(t + 600, 1200)], callsign="EXA1")
        self.fly("A0F101", run(t + 2 * 3600, 30, 34000), callsign="EXA2")
        got = self.flights_ep("a0f101", window="24h")
        self.assertEqual((got["icao"], got["window"], got["truncated"]), ("A0F101", "24h", False))
        newest, oldest = got["flights"]
        self.assertEqual((newest["start"], newest["end"], newest["fixes"], newest["callsign"], newest["max_alt"]),
                         (t + 2 * 3600, t + 2 * 3600 + 29 * 30, 30, "EXA2", 34000))
        self.assertEqual((oldest["fixes"], oldest["callsign"], oldest["max_alt"]), (21, "EXA1", 3000))

    def test_the_callsign_is_the_one_used_most_and_a_long_gap_is_reported(self):
        t = self.now - 3600
        self.fly("A0F102", run(t, 3, 30000), callsign="")
        self.fly("A0F102", run(t + 90, 10, 30000), callsign="EXA7 ")
        self.fly("A0F102", run(t + 90 + 9 * 30 + 20 * MIN, 5, 31000), callsign="EXA8")
        (f,) = self.flights_ep("A0F102", window="24h")["flights"]
        self.assertEqual((f["callsign"], f["fixes"], f["longest_gap"]), ("EXA7", 18, 20 * MIN))

    def test_takeoffs_and_landings_label_the_flight_they_are_in(self):
        t = self.now - 6 * 3600
        self.fly("A0F103", run(t, 20, 4000))
        self.fly("A0F103", run(t + 3 * 3600, 20, 4000))
        self.event("A0F103", "takeoff", "KXMP", t + 30)
        self.event("A0F103", "landing", "KXAB", t + 19 * 30 + 60, runway="27")
        for k in range(3):
            self.event("A0F103", "touch_and_go", "KXAB", t + 3 * 3600 + 120 + k * 180)
        newest, oldest = self.flights_ep("A0F103", window="24h")["flights"]
        self.assertEqual([(e["kind"], e["airport"], e["runway"]) for e in oldest["events"]],
                         [("takeoff", "KXMP", "18"), ("landing", "KXAB", "27")])
        self.assertEqual([(e["kind"], e["count"]) for e in newest["events"]], [("touch_and_go", 3)])
        self.assertNotIn("callsign", newest["events"][0])

    def test_the_window_keeps_a_flight_that_began_before_it_whole(self):
        start = self.now - 86400 - 20 * MIN
        self.fly("A0F104", run(start, 80, 30000))
        (f,) = self.flights_ep("A0F104", window="24h")["flights"]
        self.assertEqual((f["start"], f["fixes"]), (start, 80))

    def test_a_flight_that_ended_before_the_window_is_left_out(self):
        self.fly("A0F105", run(self.now - 86400 - 3 * 3600, 10, 30000))
        self.assertEqual(self.flights_ep("A0F105", window="24h")["flights"], [])
        self.assertEqual(len(self.flights_ep("A0F105", window="7d")["flights"]), 1)

    def test_all_reaches_back_past_30_days(self):
        self.fly("A0F106", run(self.now - 90 * 86400, 10, 30000))
        self.assertEqual(self.flights_ep("A0F106", window="30d")["flights"], [])
        self.assertEqual(len(self.flights_ep("A0F106", window="all")["flights"]), 1)

    def test_a_long_history_keeps_the_newest_flights(self):
        t = self.now - 20 * 86400
        fixes = [(t + k * 3600 + i * 30, 30000) for k in range(flights.MAX_FLIGHTS + 3) for i in range(2)]
        self.fly("A0F107", fixes)
        got = self.flights_ep("A0F107", window="all")
        self.assertTrue(got["truncated"])
        self.assertEqual(len(got["flights"]), flights.MAX_FLIGHTS)
        self.assertEqual(got["flights"][0]["start"], fixes[-2][0])

    def test_positions_without_a_place_and_other_aircraft_are_not_counted(self):
        t = self.now - 3600
        self.fly("A0F108", run(t, 5, 30000))
        self.execute("INSERT INTO all_sightings (icao, altitude, seen_at) VALUES (?, ?, ?)",
                     [("A0F108", 30000, t + 40 * MIN)])
        self.fly("A0F109", run(t, 5, 30000))
        (f,) = self.flights_ep("A0F108", window="24h")["flights"]
        self.assertEqual(f["fixes"], 5)

    def test_bad_hex_or_window_is_refused(self):
        for icao, window in (("XYZ", "24h"), ("A0F10Z", "24h"), ("A0F101", "1y")):
            with self.assertRaises(HTTPException) as cm:
                self.flights_ep(icao, window=window)
            self.assertEqual(cm.exception.status_code, 400)

    def test_an_aircraft_never_seen_has_no_flights(self):
        self.assertEqual(self.flights_ep("A0F1FF", window="all")["flights"], [])


class TestFlightPositions(_Db):

    def test_since_and_until_fetch_one_flight_with_its_speeds(self):
        t = self.now - 3600
        self.fly("A0F110", run(t, 10, 30000), speed=451.5)
        self.fly("A0F110", run(t + 40 * MIN, 10, 30000), speed=300.0)
        got = self.positions_ep("A0F110", since=t, until=t + 9 * 30)
        self.assertEqual(got["count"], 10)
        self.assertEqual(got["positions"][0][0], t)
        self.assertEqual({p[4] for p in got["positions"]}, {451.5})

    def test_the_window_answer_carries_speed_too(self):
        t = self.now - 3600
        self.fly("A0F111", run(t, 3, 30000), speed=250.0)
        got = self.positions_ep("A0F111", window="24h")
        self.assertEqual([len(p) for p in got["positions"]], [5, 5, 5])
        self.assertEqual(got["positions"][-1][4], 250.0)

    def test_since_without_until_or_backwards_is_refused(self):
        for since, until in ((self.now, None), (None, self.now), (self.now, self.now - 1)):
            with self.assertRaises(HTTPException) as cm:
                self.positions_ep("A0F110", since=since, until=until)
            self.assertEqual(cm.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
