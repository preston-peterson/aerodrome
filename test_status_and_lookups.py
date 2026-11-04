"""The health summary every page's dot reads, the lookups that shouldn't repeat
themselves (an aircraft hexdb has no owner for, ntfy's latest version with no
internet), and holding collection while a just-booted clock is unset. hexdb, GitHub
and the clock are mocked; hexes and data are invented.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import requests

import collector
import ntfy_installer
import server
from schema_migrations import apply_schema_migrations

ALL_UP = {"receiver": True, "database": True, "collector": True, "webserver": True}
CALM = {"load_average": {"15m": 0.5}, "cpu_cores": 4}


def summary(core=None, hexdb_ok=True, capacity_ok=True, sudoers=None, system=CALM):
    return server._health_summary(core or ALL_UP, hexdb_ok, capacity_ok, sudoers, system)


class TestHealthSummary(unittest.TestCase):

    def test_all_well(self):
        self.assertEqual(summary(), ("ok", [], None))

    def test_a_core_component_down_is_an_error_and_says_which(self):
        severity, failing, reason = summary(core=dict(ALL_UP, receiver=False, database=False))
        self.assertEqual(severity, "error")
        self.assertEqual(failing, ["receiver", "database"])
        self.assertEqual(reason, "receiver, database offline")

    def test_an_error_still_lists_the_advisories(self):
        _, failing, _ = summary(core=dict(ALL_UP, receiver=False), hexdb_ok=False, capacity_ok=False)
        self.assertEqual(failing, ["receiver", "hexdb_resolver", "capacity"])

    def test_each_thing_that_needs_attention_is_a_warning(self):
        drift = {"can_verify": True, "needs_refresh": True, "expected_version": 6, "live_version": 5}
        cases = {
            "sudoers": dict(sudoers=drift),
            "hexdb_resolver": dict(hexdb_ok=False),
            "capacity": dict(capacity_ok=False),
            "system_load": dict(system={"load_average": {"15m": 6.0}, "cpu_cores": 4}),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                severity, failing, reason = summary(**kwargs)
                self.assertEqual((severity, failing), ("warn", [name]))
                self.assertTrue(reason)

    def test_the_reason_names_the_first_of_several(self):
        drift = {"can_verify": True, "needs_refresh": True, "expected_version": 6, "live_version": 5}
        severity, failing, reason = summary(sudoers=drift, capacity_ok=False)
        self.assertEqual(failing, ["sudoers", "capacity"])
        self.assertIn("expects v6, live v5", reason)

    def test_drift_that_cant_be_verified_is_not_a_warning(self):
        drift = {"can_verify": False, "needs_refresh": True}
        self.assertEqual(summary(sudoers=drift)[0], "ok")

    def test_load_under_one_and_a_half_times_the_cores_is_normal(self):
        system = {"load_average": {"15m": 5.9}, "cpu_cores": 4}
        self.assertEqual(summary(system=system)[0], "ok")

    def test_missing_load_figures_are_not_a_warning(self):
        for system in (None, {}, {"load_average": None, "cpu_cores": 4}, {"load_average": {}, "cpu_cores": 0}):
            with self.subTest(system):
                self.assertEqual(summary(system=system)[0], "ok")

    def test_one_core_reads_right(self):
        _, _, reason = summary(system={"load_average": {"15m": 2.0}, "cpu_cores": 1})
        self.assertIn("on 1 core ", reason)


class _Db(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def sql(self, statement, args=()):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(statement, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()


class TestAircraftHexdbHasNoOwnerFor(_Db):
    ICAO = "A0B1C2"

    def setUp(self):
        super().setUp()
        for cache in (collector._ICAO_CACHE, collector._TYPE_CODE_CACHE):
            cache.clear()
        for p in (mock.patch.object(collector, "_db_path", self.db),
                  mock.patch.object(collector.requests, "get", side_effect=self.hexdb)):
            p.start()
            self.addCleanup(p.stop)
        self.answer = {"Registration": "N123EX", "ICAOTypeCode": "C172",
                       "RegisteredOwners": "", "Manufacturer": ""}
        self.asked = 0

    def hexdb(self, *args, **kwargs):
        self.asked += 1
        resp = mock.Mock(status_code=200)
        resp.json.return_value = self.answer
        return resp

    def restart(self):
        collector._ICAO_CACHE.clear()
        collector._TYPE_CODE_CACHE.clear()

    def test_a_restart_doesnt_ask_again(self):
        self.assertEqual(collector.resolve_icao_to_tail(self.ICAO), "N123EX")
        self.restart()
        self.assertEqual(collector.resolve_icao_to_tail(self.ICAO), "N123EX")
        self.assertEqual(self.asked, 1)

    def test_a_row_from_before_owners_were_stored_asks_once_more(self):
        collector.resolve_icao_to_tail(self.ICAO)
        self.sql("UPDATE hexdb_cache SET registered_owner = NULL WHERE icao = ?", (self.ICAO,))
        self.restart()
        collector.resolve_icao_to_tail(self.ICAO)
        self.assertEqual(self.asked, 2)
        self.restart()
        collector.resolve_icao_to_tail(self.ICAO)
        self.assertEqual(self.asked, 2)

    def test_an_owner_is_still_stored_and_mirrored(self):
        self.sql("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at) VALUES (?, 1, 1)", (self.ICAO,))
        self.answer["RegisteredOwners"] = "Example Aviation"
        collector.resolve_icao_to_tail(self.ICAO)
        self.assertEqual(self.sql("SELECT registered_owner FROM hexdb_cache"), [("Example Aviation",)])
        self.assertEqual(self.sql("SELECT registered_owner FROM seen_aircraft"), [("Example Aviation",)])

    def test_no_owner_never_blanks_one_already_known(self):
        self.sql("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at, registered_owner) "
                 "VALUES (?, 1, 1, 'Example Aviation')", (self.ICAO,))
        collector.resolve_icao_to_tail(self.ICAO)
        self.assertEqual(self.sql("SELECT registered_owner FROM seen_aircraft"), [("Example Aviation",)])

    def test_an_unknown_aircraft_stays_null(self):
        conn = sqlite3.connect(self.db)
        collector._hexdb_cache_write(conn, self.ICAO, None, "negative", 1000)
        conn.commit()
        conn.close()
        self.assertEqual(self.sql("SELECT registered_owner FROM hexdb_cache"), [(None,)])


class TestNtfyLatestVersion(unittest.TestCase):

    def setUp(self):
        self.now = 1000.0
        for p in (mock.patch.object(ntfy_installer, "_version_cache", None),
                  mock.patch.object(ntfy_installer, "_version_failed_at", None),
                  mock.patch.object(ntfy_installer.time, "monotonic", side_effect=lambda: self.now)):
            p.start()
            self.addCleanup(p.stop)

    def test_no_internet_is_only_waited_on_once_in_a_while(self):
        with mock.patch.object(ntfy_installer.requests, "get",
                               side_effect=requests.ConnectionError("no route")) as get:
            for _ in range(5):
                self.assertIsNone(ntfy_installer.latest_version())
            self.assertEqual(get.call_count, 1)
            self.now += ntfy_installer.VERSION_RETRY_SEC + 1
            self.assertIsNone(ntfy_installer.latest_version())
            self.assertEqual(get.call_count, 2)

    def test_it_recovers_and_keeps_the_answer(self):
        with mock.patch.object(ntfy_installer.requests, "get",
                               side_effect=requests.ConnectionError("no route")):
            ntfy_installer.latest_version()
        self.now += ntfy_installer.VERSION_RETRY_SEC + 1
        good = mock.Mock()
        good.json.return_value = {"tag_name": "v2.11.0"}
        with mock.patch.object(ntfy_installer.requests, "get", return_value=good) as get:
            self.assertEqual(ntfy_installer.latest_version(), "2.11.0")
            self.assertEqual(ntfy_installer.latest_version(), "2.11.0")
            self.assertEqual(get.call_count, 1)


class TestClockAfterBoot(_Db):
    NEWEST = 2_000_000

    def setUp(self):
        super().setUp()
        self.sql("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)", (self.NEWEST,))
        self.clock = float(self.NEWEST - 86400)     # a day behind
        self.running = 0.0
        self.uptime = 30.0
        self.slept = []
        for p in (mock.patch.object(collector.time, "time", side_effect=lambda: self.clock),
                  mock.patch.object(collector.time, "monotonic", side_effect=lambda: self.running),
                  mock.patch.object(collector.time, "sleep", side_effect=self.sleep),
                  mock.patch.object(collector, "_seconds_since_boot", side_effect=lambda: self.uptime)):
            p.start()
            self.addCleanup(p.stop)
        self.set_clock_after = None

    def sleep(self, sec):
        self.slept.append(sec)
        self.running += sec
        self.clock += sec
        if self.set_clock_after is not None and self.running >= self.set_clock_after:
            self.clock = float(self.NEWEST + 100)
            self.set_clock_after = None

    def test_a_clock_behind_the_newest_row_waits_to_be_set(self):
        self.set_clock_after = 40
        with self.assertLogs("adsb.collector", "WARNING") as logs:
            collector.wait_for_plausible_clock(self.db)
        self.assertEqual(sum(self.slept), 40)
        self.assertIn("earlier than the newest sighting", logs.output[0])

    def test_a_clock_that_never_gets_set_stops_waiting(self):
        with self.assertLogs("adsb.collector", "WARNING") as logs:
            collector.wait_for_plausible_clock(self.db)
        self.assertEqual(sum(self.slept), collector.CLOCK_WAIT_MAX_SEC)
        self.assertIn("Collecting anyway", logs.output[-1])

    def test_a_restart_long_after_boot_doesnt_wait(self):
        self.uptime = 86400.0
        collector.wait_for_plausible_clock(self.db)
        self.assertEqual(self.slept, [])

    def test_a_small_step_back_doesnt_wait(self):
        self.clock = float(self.NEWEST - 30)
        collector.wait_for_plausible_clock(self.db)
        self.assertEqual(self.slept, [])

    def test_a_clock_thats_right_doesnt_wait(self):
        self.clock = float(self.NEWEST + 5)
        collector.wait_for_plausible_clock(self.db)
        self.assertEqual(self.slept, [])

    def test_an_empty_database_doesnt_wait(self):
        self.sql("DELETE FROM all_sightings")
        collector.wait_for_plausible_clock(self.db)
        self.assertEqual(self.slept, [])

    def test_no_uptime_doesnt_wait(self):
        self.uptime = None
        collector.wait_for_plausible_clock(self.db)
        self.assertEqual(self.slept, [])

    def test_a_database_problem_doesnt_stop_collecting(self):
        with mock.patch.object(collector, "_open_db_conn", side_effect=sqlite3.OperationalError("locked")):
            collector.wait_for_plausible_clock(self.db)
        self.assertEqual(self.slept, [])


if __name__ == "__main__":
    unittest.main()
