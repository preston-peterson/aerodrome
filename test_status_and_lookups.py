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


def summary(core=None, hexdb_ok=True, capacity_ok=True, sudoers=None, system=CALM, notifications=None):
    return server._health_summary(core or ALL_UP, hexdb_ok, capacity_ok, sudoers, system,
                                  notifications=notifications)


NOTIFY_DOWN = {"enabled": True, "ok": False, "reachable": False}
NOTIFY_LAST_FAILED = {"enabled": True, "ok": False, "reachable": True}


class TestHealthSummary(unittest.TestCase):

    def test_all_well(self):
        self.assertEqual(summary(), ("ok", [], None))

    def test_a_core_component_down_is_an_error_and_says_which(self):
        severity, failing, reason = summary(core=dict(ALL_UP, receiver=False, database=False))
        self.assertEqual(severity, "error")
        self.assertEqual(failing, ["receiver", "database"])
        self.assertEqual(reason, "receiver, database offline")

    def test_an_error_still_lists_the_advisories(self):
        _, failing, _ = summary(core=dict(ALL_UP, receiver=False), hexdb_ok=False, capacity_ok=False,
                                notifications=NOTIFY_DOWN)
        self.assertEqual(failing, ["receiver", "hexdb_resolver", "capacity", "notifications"])

    def test_each_thing_that_needs_attention_is_a_warning(self):
        drift = {"can_verify": True, "needs_refresh": True, "expected_version": 6, "live_version": 5}
        cases = {
            "sudoers": dict(sudoers=drift),
            "hexdb_resolver": dict(hexdb_ok=False),
            "capacity": dict(capacity_ok=False),
            "system_load": dict(system={"load_average": {"15m": 6.0}, "cpu_cores": 4}),
            "notifications": dict(notifications=NOTIFY_DOWN),
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

    def test_notifications_say_which_problem(self):
        self.assertIn("unreachable", summary(notifications=NOTIFY_DOWN)[2])
        self.assertIn("last alert failed", summary(notifications=NOTIFY_LAST_FAILED)[2])

    def test_notifications_off_or_working_is_not_a_warning(self):
        for n in (None, {"enabled": False, "ok": True}, {"enabled": True, "ok": True, "reachable": True}):
            with self.subTest(n):
                self.assertEqual(summary(notifications=n), ("ok", [], None))


class _Answer:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


class TestNotifyServerProbe(unittest.TestCase):
    """The ntfy health probe behind the Status page's Notifications card.
    requests is mocked; addresses are documentation/loopback examples."""

    URL = "http://127.0.0.1:2586/example-topic"

    def probe(self, url=URL, **get):
        with mock.patch("requests.get", **get) as g:
            return server._notify_server_probe(url), g

    def test_any_answer_below_500_is_reachable(self):
        for code in (200, 404):
            with self.subTest(code):
                out, g = self.probe(return_value=_Answer(code))
                self.assertTrue(out["reachable"])
                self.assertIsNone(out["error"])
        self.assertEqual(g.call_args.args[0], "http://127.0.0.1:2586/v1/health")
        self.assertEqual(g.call_args.kwargs["allow_redirects"], False)
        self.assertTrue(g.call_args.kwargs["stream"])
        self.assertTrue(g.return_value.closed, "the body is never downloaded")

    def test_a_server_error_is_unreachable(self):
        out, _ = self.probe(return_value=_Answer(502))
        self.assertFalse(out["reachable"])
        self.assertIn("502", out["error"])

    def test_refused_and_silent(self):
        for exc, words in ((requests.ConnectionError("refused"), "couldn't connect"),
                           (requests.Timeout("slow"), "no answer within")):
            with self.subTest(words):
                out, _ = self.probe(side_effect=exc)
                self.assertFalse(out["reachable"])
                self.assertIn(words, out["error"])

    def test_server_is_host_and_port_only(self):
        out, g = self.probe("https://user:secret@ntfy.example.org:8443/private-topic?x=1",
                            return_value=_Answer(200))
        self.assertEqual(out["server"], "ntfy.example.org:8443")
        self.assertEqual(g.call_args.args[0], "https://ntfy.example.org:8443/v1/health")

    def test_a_blocked_address_is_never_asked(self):
        out, g = self.probe("http://169.254.169.254/example-topic", return_value=_Answer(200))
        g.assert_not_called()
        self.assertFalse(out["reachable"])
        self.assertIn("won't send to", out["error"])

    def test_not_a_web_address(self):
        out, g = self.probe("ftp://192.0.2.10/example-topic", return_value=_Answer(200))
        g.assert_not_called()
        self.assertFalse(out["reachable"])


class TestNotificationsCheck(unittest.TestCase):
    ON = {"enabled": True, "url": "http://127.0.0.1:2586/example-topic"}
    UP = {"reachable": True, "response_ms": 3, "error": None, "server": "127.0.0.1:2586"}
    DOWN = {"reachable": False, "response_ms": None, "error": "couldn't connect", "server": "127.0.0.1:2586"}

    def check(self, cfg=ON, stats=None, cache=None, now=1000.0, found=UP):
        calls = []
        def probe(url):
            calls.append(url)
            return dict(found)
        comp, cache = server._notifications_check(cfg, stats, cache, now, probe=probe)
        return comp, cache, calls

    def test_off_is_ok_and_never_probes(self):
        for cfg in (None, {}, {"enabled": False, "url": "http://127.0.0.1:2586/t"}, {"enabled": True, "url": ""}):
            with self.subTest(cfg):
                comp, _, calls = self.check(cfg=cfg)
                self.assertEqual(comp, {"enabled": False, "ok": True})
                self.assertEqual(calls, [])

    def test_working(self):
        comp, _, _ = self.check(stats={"last_sent": {"ts": 900}, "last_error": None})
        self.assertTrue(comp["ok"])
        self.assertEqual(comp["last_sent_ts"], 900)
        self.assertIsNone(comp["last_failed"])

    def test_unreachable(self):
        comp, _, _ = self.check(found=self.DOWN)
        self.assertFalse(comp["ok"])
        self.assertFalse(comp["reachable"])
        self.assertEqual(comp["error"], "couldn't connect")

    def test_the_last_alert_failing_counts_only_if_nothing_went_through_since(self):
        failed = {"ts": 950, "event": "watchlist", "error": "HTTP 403"}
        comp, _, _ = self.check(stats={"last_sent": {"ts": 900}, "last_error": failed})
        self.assertFalse(comp["ok"])
        self.assertEqual(comp["last_failed"], {"ts": 950, "event": "watchlist", "error": "HTTP 403"})
        comp, _, _ = self.check(stats={"last_sent": {"ts": 990}, "last_error": failed})
        self.assertTrue(comp["ok"])

    def test_a_failure_text_never_carries_the_topic(self):
        failed = {"ts": 950, "event": "watchlist",
                  "error": "Connection error: HTTPConnectionPool(host='127.0.0.1', port=2586): "
                           "Max retries exceeded with url: /example-topic"}
        comp, _, _ = self.check(stats={"last_error": failed})
        self.assertEqual(comp["last_failed"]["error"], "couldn't connect")
        self.assertNotIn("example-topic", str(comp))

    def test_probed_at_most_every_five_minutes_and_again_when_the_url_changes(self):
        _, cache, calls = self.check(now=1000.0)
        _, cache, more = self.check(cache=cache, now=1000.0 + server.NOTIFY_PROBE_CACHE_TTL_SEC - 1)
        self.assertEqual((len(calls), len(more)), (1, 0))
        _, cache, more = self.check(cache=cache, now=1000.0 + server.NOTIFY_PROBE_CACHE_TTL_SEC)
        self.assertEqual(len(more), 1)
        moved = dict(self.ON, url="http://127.0.0.1:2587/example-topic")
        _, _, more = self.check(cfg=moved, cache=cache, now=1001.0 + server.NOTIFY_PROBE_CACHE_TTL_SEC)
        self.assertEqual(more, ["http://127.0.0.1:2587/example-topic"])


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
