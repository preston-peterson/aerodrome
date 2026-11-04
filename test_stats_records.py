"""The fastest and highest all-time records and Today's cards refuse readings a
type can't produce, keep the aircraft that really do fly that fast or high, and
correct a record stored under looser rules from the never-pruned hourly history.
All aircraft and readings are invented.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collector
import server
import type_classes
from categorize import HELICOPTER_TYPES
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
HOUR = 3600
DAY = 86400
OLD = (int(time.time()) - 400 * DAY) // HOUR * HOUR  # well past any retention


class TestCeilings(unittest.TestCase):

    def test_altitudes_a_type_cannot_reach_are_refused(self):
        for type_code, feet in (("C130", 120000), ("B738", 100000), ("A388", 61000),
                                ("C172", 60000), ("T18", 51000), ("R44", 46000),
                                ("V22", 61000)):
            with self.subTest(type_code):
                self.assertFalse(collector.altitude_is_plausible(type_code, feet))

    def test_real_altitudes_pass(self):
        for type_code, feet in (("C130", 33000), ("B738", 41000), ("GLF6", 51000),
                                ("C172", 14000), ("R44", 14000), ("EGRT", 53000)):
            with self.subTest(type_code):
                self.assertTrue(collector.altitude_is_plausible(type_code, feet))

    def test_high_flyers_and_unknown_types_have_no_altitude_ceiling(self):
        for type_code in ("U2", "ER2", "B57", "Q4", "F15", "BALL", "GLID", "", None, "  ", "ZZZZ"):
            with self.subTest(type_code):
                self.assertTrue(collector.altitude_is_plausible(type_code, 130000))
                self.assertIsNone(collector.altitude_ceiling_for_type(type_code))

    def test_a_homebuilt_gets_a_class_speed_ceiling(self):
        self.assertEqual(collector.speed_ceiling_for_type("T18"), collector.CLASS_MAX_KT["piston"])
        self.assertLess(collector.speed_ceiling_for_type("T18"), 1000)

    def test_real_ground_speeds_pass_with_a_tailwind(self):
        for type_code, knots in (("B738", 690), ("A388", 690), ("A400", 600), ("P51", 450),
                                 ("X3", 260), ("SB1", 260), ("V22", 300), ("F15", 1400),
                                 ("", 1400), (None, 1400), ("ZZZZ", 1400)):
            with self.subTest(type_code):
                self.assertLessEqual(knots, collector.speed_ceiling_for_type(type_code))

    def test_the_tighter_per_type_ceilings_still_win(self):
        self.assertEqual(collector.speed_ceiling_for_type("C172"), 250)
        self.assertEqual(collector.speed_ceiling_for_type("R44"), 180)

    def test_a_tilt_rotor_is_not_a_helicopter(self):
        self.assertNotIn("V22", HELICOPTER_TYPES)
        self.assertEqual(collector.speed_ceiling_for_type("V22"), collector.CLASS_MAX_KT["turboprop"])

    def test_type_codes_are_matched_however_they_are_written(self):
        self.assertEqual(collector.speed_ceiling_for_type(" t18 "), collector.CLASS_MAX_KT["piston"])
        self.assertFalse(collector.altitude_is_plausible(" c130 ", 120000))

    def test_the_classes_do_not_overlap(self):
        self.assertFalse(type_classes.PISTON_TYPES & type_classes.TURBOPROP_TYPES)
        self.assertFalse(HELICOPTER_TYPES & type_classes.PISTON_TYPES)
        self.assertFalse(HELICOPTER_TYPES & type_classes.TURBOPROP_TYPES)
        self.assertGreater(len(type_classes.PISTON_TYPES), 1000)
        self.assertGreater(len(type_classes.TURBOPROP_TYPES), 200)

    @unittest.skipUnless((REPO / "scripts" / "doc8643.csv").exists(), "local DOC 8643 cache not here")
    def test_the_committed_table_is_what_the_generator_makes(self):
        done = subprocess.run([sys.executable, str(REPO / "scripts" / "gen_type_classes.py"), "--check"],
                              capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class _DbCase(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()

    def execute(self, sql, *rows):
        conn = sqlite3.connect(self.db)
        try:
            for row in rows or ((),):
                conn.execute(sql, row)
            conn.commit()
        finally:
            conn.close()

    def record(self, *rows):
        self.execute("INSERT OR REPLACE INTO stats_records (record_type, value, icao, callsign, "
                     "aircraft_type, set_at, extra) VALUES (?, ?, ?, ?, ?, ?, ?)", *rows)

    def hourly(self, *rows):
        """(icao, callsign, type, speed, altitude, hour_bucket)"""
        self.execute("INSERT INTO sightings_hourly (icao, hour_bucket, callsign, aircraft_type, "
                     "sighting_count, first_seen_at, last_seen_at, max_speed, max_altitude) "
                     "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                     *[(i, h, c, t, h, h, s, a) for i, c, t, s, a, h in rows])

    def records(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return {r["record_type"]: dict(r) for r in conn.execute("SELECT * FROM stats_records")}
        finally:
            conn.close()

    def restart(self):
        collector.init_db(self.db)


class TestHeal(_DbCase):

    BAD_SPEED = ("fastest_ever", 1200.0, "A0B001", "N1BAD", "T18", OLD + 7200, "")
    BAD_ALT = ("highest_altitude_ever", 120000.0, "A0B002", "BAD2", "C130", OLD + 7200, "")

    def test_bad_records_are_replaced_from_hourly_history_older_than_retention(self):
        self.record(self.BAD_SPEED, self.BAD_ALT)
        self.hourly(("A0B001", "N1BAD", "T18", 1200, 3000, OLD + 7200),
                    ("A0B002", "BAD2", "C130", 300, 120000, OLD + 7200),
                    ("A0B003", "DAL1", "B738", 512.5, 37000, OLD),
                    ("A0B004", "DAL2", "B752", 490, 41000, OLD + HOUR))
        self.restart()
        rec = self.records()
        self.assertEqual((rec["fastest_ever"]["value"], rec["fastest_ever"]["icao"],
                          rec["fastest_ever"]["callsign"], rec["fastest_ever"]["aircraft_type"]),
                         (512.5, "A0B003", "DAL1", "B738"))
        self.assertEqual((rec["highest_altitude_ever"]["value"], rec["highest_altitude_ever"]["icao"]),
                         (41000, "A0B004"))

    def test_set_at_is_the_hour_it_happened_not_now(self):
        self.record(self.BAD_SPEED)
        self.hourly(("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        self.assertEqual(self.records()["fastest_ever"]["set_at"], OLD)

    def test_healing_fires_no_notification(self):
        self.record(self.BAD_SPEED, self.BAD_ALT)
        self.hourly(("A0B003", "DAL1", "B738", 512, 37000, OLD))
        with mock.patch.object(collector, "_fire_new_record_notification") as fire:
            self.restart()
        fire.assert_not_called()

    def test_a_healthy_record_is_left_alone_and_a_rerun_changes_nothing(self):
        healthy = [("fastest_ever", 500.0, "A0B005", "OK1", "B738", OLD, ""),
                   ("highest_altitude_ever", 41000.0, "A0B005", "OK1", "B738", OLD, "")]
        self.record(*healthy)
        # A better reading in the history isn't a reason to touch a healthy record.
        self.hourly(("A0B006", "OK2", "B772", 600, 43000, OLD + HOUR))
        before = self.records()
        self.restart()
        self.restart()
        self.assertEqual(self.records(), before)

    def test_healing_twice_is_the_same_as_once(self):
        self.record(self.BAD_SPEED, self.BAD_ALT)
        self.hourly(("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        once = self.records()
        self.restart()
        self.assertEqual(self.records(), once)

    def test_the_other_records_are_not_touched(self):
        others = [("furthest_ever", 9000.0, "A0B007", "F1", "B738", OLD, "mi"),
                  ("lowest_altitude_ever", 25.0, "A0B007", "F1", "C172", OLD, ""),
                  ("peak_simultaneous_ever", 99999.0, "", "99999 aircraft", "", OLD, "")]
        self.record(*others, self.BAD_SPEED)
        self.hourly(("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        rec = self.records()
        for row in others:
            self.assertEqual(rec[row[0]]["value"], row[1], row[0])

    def test_aircraft_that_really_fly_that_high_keep_their_record(self):
        for type_code in ("U2", "Q4", "B57", "BALL", ""):
            with self.subTest(type_code):
                self.record(("highest_altitude_ever", 90000.0, "A0B008", "HIGH1", type_code, OLD, ""))
                self.restart()
                self.assertEqual(self.records()["highest_altitude_ever"]["value"], 90000.0)

    def test_high_flyers_can_take_over_a_bad_record(self):
        self.record(self.BAD_ALT)
        self.hourly(("A0B009", "DRAGON1", "U2", 400, 72000, OLD),
                    ("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        self.assertEqual(self.records()["highest_altitude_ever"]["icao"], "A0B009")

    def test_a_long_run_of_glitches_cannot_hide_the_real_answer(self):
        self.record(self.BAD_SPEED)
        self.hourly(*[("A0B010", "N2BAD", "T18", 1000 + i, 3000, OLD + i * HOUR) for i in range(120)],
                    ("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        self.assertEqual(self.records()["fastest_ever"]["icao"], "A0B003")

    def test_a_blank_type_is_judged_by_the_type_learned_later(self):
        self.record(("fastest_ever", 1200.0, "A0B011", "N3BAD", "", OLD, ""))
        self.hourly(("A0B011", "N3BAD", "", 1200, 3000, OLD),
                    ("A0B011", "N3BAD", "T18", 120, 3000, OLD + HOUR),
                    ("A0B012", "N4BAD", "", 1100, 3000, OLD),
                    ("A0B012", "N4BAD", "C172", 100, 3000, OLD + HOUR),
                    ("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        self.assertEqual(self.records()["fastest_ever"]["icao"], "A0B003")

    def test_a_blank_type_never_learned_stays_permissive(self):
        self.record(self.BAD_SPEED)
        self.hourly(("A0B013", "MIL1", "", 1300, 30000, OLD),
                    ("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        self.assertEqual(self.records()["fastest_ever"]["icao"], "A0B013")

    def test_a_type_found_in_history_is_shown_on_the_record(self):
        self.record(self.BAD_ALT)
        self.hourly(("A0B014", "DAL3", "", 450, 39000, OLD),
                    ("A0B014", "DAL3", "B738", 300, 20000, OLD + HOUR))
        self.restart()
        self.assertEqual(self.records()["highest_altitude_ever"]["aircraft_type"], "B738")

    def test_relayed_targets_and_stringy_values_are_skipped(self):
        self.record(self.BAD_SPEED)
        self.hourly(("~B015A", "TISB", "B738", 650, 37000, OLD),
                    ("A0B016", "ODD1", "B738", "ground", 37000, OLD),
                    ("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        self.assertEqual(self.records()["fastest_ever"]["icao"], "A0B003")

    def test_with_nothing_to_replace_it_the_record_is_cleared_and_the_next_one_sets_it(self):
        self.record(self.BAD_SPEED)
        self.restart()
        self.assertNotIn("fastest_ever", self.records())
        conn = sqlite3.connect(self.db)
        ac = {"hex": "A0B017", "callsign": "NEW1", "aircraft_type": "B738"}
        collector._update_record(conn, "fastest_ever", 400, "gt", ac, 1000)
        conn.commit()
        conn.close()
        self.assertEqual(self.records()["fastest_ever"]["value"], 400)

    def test_a_missing_record_is_filled_from_history(self):
        self.record(("furthest_ever", 100.0, "A0B007", "F1", "B738", OLD, "mi"))
        self.hourly(("A0B003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        rec = self.records()
        self.assertEqual(rec["fastest_ever"]["value"], 512)
        self.assertEqual(rec["highest_altitude_ever"]["value"], 37000)

    def test_an_empty_database_stays_empty(self):
        self.restart()
        self.assertEqual(self.records(), {})


class TestSeed(_DbCase):
    """An empty records table beside existing sightings, as on a first upgrade."""

    def sightings(self, *rows):
        self.execute("INSERT INTO all_sightings (icao, callsign, aircraft_type, speed, altitude, "
                     "seen_at) VALUES (?, ?, ?, ?, ?, ?)", *rows)

    def test_the_seed_refuses_impossible_readings(self):
        now = int(time.time())
        self.sightings(("A0C001", "N1BAD", "T18", 1200, 3000, now),
                       ("A0C002", "BAD2", "C130", 300, 120000, now),
                       ("A0C003", "DAL1", "B738", 480, 37000, now))
        self.restart()
        rec = self.records()
        self.assertEqual(rec["fastest_ever"]["icao"], "A0C003")
        self.assertEqual(rec["highest_altitude_ever"]["icao"], "A0C003")

    def test_the_hourly_history_answers_when_glitches_fill_the_seeds_top_rows(self):
        now = int(time.time())
        self.sightings(*[("A0C002", "BAD2", "C130", 300, 120000 + i, now + i) for i in range(30)])
        self.hourly(("A0C003", "DAL1", "B738", 480, 37000, OLD))
        self.restart()
        self.assertEqual(self.records()["highest_altitude_ever"]["icao"], "A0C003")


class _Feed:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, aircraft):
        self.payload = {"now": time.time(), "messages": 1, "aircraft": aircraft}
        self.text = json.dumps(self.payload)
        self.content = self.text.encode()

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def _plane(hex_code, type_code, speed, altitude):
    return {"hex": hex_code, "flight": "TEST1", "t": type_code, "gs": speed, "alt_baro": altitude,
            "lat": 40.1, "lon": -90.1, "seen": 1}


class TestLivePoll(_DbCase):

    def setUp(self):
        super().setUp()
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        fire = mock.patch.object(collector, "_fire_new_record_notification")
        self.fire = fire.start()
        self.addCleanup(fire.stop)

    def poll(self, *planes):
        with mock.patch.object(collector.requests, "get", return_value=_Feed(list(planes))), \
                mock.patch.object(collector.hexdb_resolver, "enqueue", return_value=False):
            collector._poll_once(self.cfg, collector.build_watchlist_lookup(self.cfg))

    def test_a_poll_refuses_impossible_readings_and_keeps_real_ones(self):
        self.poll(_plane("A0D001", "T18", 1200, 3000), _plane("A0D002", "C130", 300, 120000),
                  _plane("A0D003", "B738", 450, 36000))
        rec = self.records()
        self.assertEqual((rec["fastest_ever"]["value"], rec["fastest_ever"]["icao"]), (450, "A0D003"))
        self.assertEqual((rec["highest_altitude_ever"]["value"], rec["highest_altitude_ever"]["icao"]),
                         (36000, "A0D003"))

    def test_a_glitch_cannot_beat_a_record_or_notify(self):
        self.poll(_plane("A0D003", "B738", 450, 36000))
        self.fire.reset_mock()
        self.poll(_plane("A0D001", "T18", 1200, 3000), _plane("A0D002", "C130", 300, 120000))
        rec = self.records()
        self.assertEqual(rec["fastest_ever"]["value"], 450)
        self.assertEqual(rec["highest_altitude_ever"]["value"], 36000)
        fired = [call.args[0] for call in self.fire.call_args_list]
        self.assertNotIn("fastest_ever", fired)
        self.assertNotIn("highest_altitude_ever", fired)

    def test_real_new_records_still_land_and_notify(self):
        self.poll(_plane("A0D003", "B738", 450, 36000))
        self.fire.reset_mock()
        self.poll(_plane("A0D004", "U2", 410, 72000), _plane("A0D005", "F15", 1400, 40000))
        rec = self.records()
        self.assertEqual(rec["highest_altitude_ever"]["icao"], "A0D004")
        self.assertEqual(rec["fastest_ever"]["icao"], "A0D005")
        fired = [call.args[0] for call in self.fire.call_args_list]
        self.assertIn("fastest_ever", fired)
        self.assertIn("highest_altitude_ever", fired)

    def test_a_glitch_altitude_does_not_stop_the_low_record(self):
        self.poll(_plane("A0D003", "B738", 450, 3000))
        self.poll(_plane("A0D002", "C130", 300, 120000))
        self.assertEqual(self.records()["lowest_altitude_ever"]["value"], 3000)


class TestReadPath(_DbCase):

    def setUp(self):
        super().setUp()
        left = DAY - time.time() % DAY
        if left < 5:
            time.sleep(left + 0.05)
        self.today = int(time.time()) // DAY * DAY
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        cfg.setdefault("stats", {})["timezone"] = "UTC"
        eps = {r.path: r.endpoint for r in server.get_app(cfg, os.devnull).routes if hasattr(r, "endpoint")}
        self.stats_ep, self.drill_ep = eps["/api/stats"], eps["/api/stats/drill"]

    def today_rows(self, *planes):
        """(icao, callsign, type, speed, altitude)"""
        d = self.today
        self.hourly(*[(i, c, t, s, a, d) for i, c, t, s, a in planes])
        self.execute("INSERT INTO all_sightings (icao, callsign, aircraft_type, speed, altitude, "
                     "seen_at) VALUES (?, ?, ?, ?, ?, ?)", *[(i, c, t, s, a, d + 60) for i, c, t, s, a in planes])

    def test_the_stats_page_shows_the_corrected_records(self):
        self.record(("fastest_ever", 1200.0, "A0E001", "N1BAD", "T18", OLD, ""),
                    ("highest_altitude_ever", 120000.0, "A0E002", "BAD2", "C130", OLD, ""))
        self.hourly(("A0E003", "DAL1", "B738", 512, 37000, OLD))
        self.restart()
        rows = {r["record_type"]: r for r in self.stats_ep()["cards"]["all_time_records"]}
        self.assertEqual((rows["fastest_ever"]["value"], rows["highest_altitude_ever"]["value"]), (512, 37000))
        self.assertEqual(rows["fastest_ever"]["set_at"], OLD)

    def test_todays_cards_skip_impossible_readings(self):
        self.today_rows(("A0E001", "N1BAD", "T18", 1200, 3000), ("A0E002", "BAD2", "C130", 300, 120000),
                        ("A0E003", "DAL1", "B738", 450, 36000))
        cards = self.stats_ep()["cards"]
        self.assertEqual(cards["fastest"]["icao"], "A0E003")
        self.assertEqual(cards["highest_altitude"]["icao"], "A0E003")

    def test_todays_cards_keep_high_flyers_and_unknown_types(self):
        self.today_rows(("A0E004", "HIGH1", "U2", 400, 72000), ("A0E005", "", "", 400, 90000),
                        ("A0E003", "DAL1", "B738", 450, 36000))
        self.assertEqual(self.stats_ep()["cards"]["highest_altitude"]["icao"], "A0E005")

    def test_the_drills_agree_with_the_cards(self):
        self.today_rows(("A0E001", "N1BAD", "T18", 1200, 3000), ("A0E002", "BAD2", "C130", 300, 120000),
                        ("A0E004", "HIGH1", "U2", 400, 72000), ("A0E003", "DAL1", "B738", 450, 36000))
        high = [r["icao"] for r in self.drill_ep(card="highest_altitude", value=None, all=False)["rows"]]
        self.assertEqual(high[0], "A0E004")
        self.assertNotIn("A0E002", high)
        fast = [r["icao"] for r in self.drill_ep(card="fastest", value=None, all=False)["rows"]]
        self.assertEqual(fast[0], "A0E003")
        self.assertNotIn("A0E001", fast)


if __name__ == "__main__":
    unittest.main()
