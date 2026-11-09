"""v3.4.178: the Collector card says why writes stopped.

The collector notes what it needs (collector.get_collector_health): maintenance
that holds collection up, when the last poll started, how long the receiver has
reported no aircraft, whether its message counter moves, whether its answer
changes at all, and the last poll that raised while storing.
server._collector_state turns that into the card's state, and the waiting
states are a warning, not a core failure.

It also stops storing a repeat: a receiver whose answer is byte for byte the
last poll's (a stopped decoder whose last file is still served) would record
the same positions as new sightings on every poll.
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")

import collector
import hexdb_resolver
import server
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
PLANE = {"hex": "a00001", "flight": "DEMO1 ", "lat": 40.0, "lon": -90.0,
         "alt_baro": 12000, "gs": 300, "t": "B738"}


def _answer(payload):
    resp = mock.Mock(status_code=200, headers={"Content-Type": "application/json"})
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    resp.raise_for_status = lambda: None
    return resp


def _reset_collector_state():
    for name, value in (("_activity", None), ("_last_poll_at", None), ("_empty_since", None),
                        ("_messages_last", None), ("_messages_moved_at", None), ("_feed_last", None),
                        ("_feed_changed_at", None), ("_save_error", None), ("_repeat_logged", False),
                        ("_consecutive_failed_polls", 0), ("_last_offline_reason", ""),
                        ("_offline_notified", False)):
        setattr(collector, name, value)


class _PollCase(unittest.TestCase):

    def setUp(self):
        _reset_collector_state()
        self.addCleanup(_reset_collector_state)
        d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.db = os.path.join(d, "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        for p in (mock.patch.object(hexdb_resolver, "enqueue", return_value=False),
                  mock.patch.object(hexdb_resolver, "start_worker")):
            p.start()
            self.addCleanup(p.stop)
        self.clock = 1794000000.0

    def poll(self, aircraft, now="tick", **extra):
        if now == "tick":
            self.clock += 15
            now = self.clock
        payload = dict({"aircraft": aircraft}, **({} if now is None else {"now": now}), **extra)
        with mock.patch.object(collector.requests, "get", return_value=_answer(payload)):
            collector.fetch_and_store(self.cfg, collector.build_watchlist_lookup(self.cfg))

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM all_sightings").fetchone()[0]
        finally:
            conn.close()

    def health(self):
        return collector.get_collector_health()


class TestRepeatsAreNotStored(_PollCase):

    def test_the_same_answer_twice_is_stored_once(self):
        self.poll([PLANE], now=1794000100.0)
        self.assertEqual(self.rows(), 1)
        with self.assertLogs("adsb.collector", "WARNING") as logs:
            self.poll([PLANE], now=1794000100.0)
        self.assertEqual(self.rows(), 1, "a stopped decoder's file isn't a new sighting")
        self.assertIn("same answer as the last poll", "\n".join(logs.output))
        self.assertIsNotNone(self.health()["feed_static_seconds"])
        with self.assertLogs("adsb.collector", "INFO") as logs:
            self.poll([PLANE])                               # the decoder is back
        self.assertEqual(self.rows(), 2)
        self.assertIn("changing again", "\n".join(logs.output))

    def test_a_run_of_repeats_logs_once(self):
        self.poll([PLANE], now=5.0)
        with self.assertLogs("adsb.collector", "WARNING") as logs:
            for _ in range(4):
                self.poll([PLANE], now=5.0)
        self.assertEqual(sum("same answer" in line for line in logs.output), 1)

    def test_a_constant_clock_with_moving_aircraft_is_stored_every_time(self):
        # Some feeds send a fixed (or no) "now": only a byte-identical answer counts.
        for lat in (40.0, 40.1, 40.2):
            self.poll([dict(PLANE, lat=lat)], now=0)
        self.assertEqual(self.rows(), 3)
        for lat in (40.3, 40.4):
            self.poll([dict(PLANE, lat=lat)], now=None)
        self.assertEqual(self.rows(), 5)

    def test_identical_answers_without_a_clock_are_still_repeats(self):
        self.poll([PLANE], now=None)
        self.poll([PLANE], now=None)
        self.assertEqual(self.rows(), 1)

    def test_a_repeat_is_per_database(self):
        self.poll([PLANE], now=7.0)
        other = self.db + "-other.db"
        collector.init_db(other)
        conn = sqlite3.connect(other)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.addCleanup(lambda: [os.path.exists(other + s) and os.unlink(other + s) for s in ("", "-wal", "-shm")])
        self.cfg["data"]["db_file"] = other
        self.poll([PLANE], now=7.0)
        conn = sqlite3.connect(other)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM all_sightings").fetchone()[0], 1)
        conn.close()

    def test_empty_answers_are_never_repeats(self):
        self.poll([], now=None)
        self.poll([], now=None)
        h = self.health()
        self.assertIsNone(h["feed_static_seconds"])
        self.assertIsNotNone(h["empty_seconds"])
        self.poll([PLANE])
        self.assertIsNone(self.health()["empty_seconds"])
        self.assertEqual(self.rows(), 1)

    def test_a_repeat_still_runs_the_due_cleanup(self):
        self.poll([PLANE], now=9.0)
        with mock.patch.object(collector, "run_due_cleanup") as cleanup:
            self.poll([PLANE], now=9.0)
        cleanup.assert_called_once()


class TestHealth(_PollCase):

    def test_message_counter(self):
        self.assertIsNone(self.health()["messages_static_seconds"])
        self.poll([], messages=10)
        first = collector._messages_moved_at
        self.poll([], messages=10)
        self.assertEqual(collector._messages_moved_at, first, "unchanged: not hearing anything")
        self.poll([], messages=12)
        self.assertGreater(collector._messages_moved_at, first)
        self.poll([])                                            # a feed without the counter
        self.assertIsNone(self.health()["messages_static_seconds"])

    def test_last_poll_and_uptime(self):
        self.assertIsNone(self.health()["last_poll_seconds_ago"])
        self.poll([PLANE])
        h = self.health()
        self.assertLess(h["last_poll_seconds_ago"], 5)
        self.assertGreaterEqual(h["uptime_seconds"], 0)

    def test_a_poll_that_raises_is_a_save_error_until_one_succeeds(self):
        full = sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(collector, "_open_db_conn", side_effect=full):
            for _ in range(2):
                with self.assertRaises(sqlite3.OperationalError):
                    self.poll([PLANE])
        e = self.health()["save_error"]
        self.assertEqual((e["kind"], e["count"]), ("disk_full", 2))
        self.assertEqual(e["message"], "sqlite3.OperationalError: database or disk is full")
        self.poll([PLANE])
        self.assertIsNone(self.health()["save_error"])
        self.assertEqual(self.rows(), 1)

    def test_error_kinds(self):
        cases = {
            "database is locked": "locked",
            "database table is locked": "locked",
            "attempt to write a readonly database": "readonly",
            "database disk image is malformed": "corrupt",
            "file is not a database": "corrupt",
            "disk I/O error": "io",
            "no such table: all_sightings": "other",
        }
        for text, kind in cases.items():
            self.assertEqual(collector._poll_error_kind(sqlite3.OperationalError(text)), kind, text)
        self.assertEqual(collector._poll_error_kind(OSError(28, "No space left on device")), "disk_full")
        self.assertEqual(collector._poll_error_kind(OSError(5, "Input/output error")), "io")
        collector._note_poll_error(KeyError("hex"))
        self.assertEqual(collector._save_error["message"], "KeyError: 'hex'")

    def test_receiver_errors_are_not_save_errors(self):
        with mock.patch.object(collector.requests, "get", side_effect=collector.requests.ConnectionError("refused")):
            collector.fetch_and_store(self.cfg, collector.build_watchlist_lookup(self.cfg))
        h = self.health()
        self.assertIsNone(h["save_error"])
        self.assertEqual(h["consecutive_failed_polls"], 1)


class TestMaintenanceIsMarked(_PollCase):

    def seen_during(self, target, call):
        seen = []
        with mock.patch.object(collector, target,
                               side_effect=lambda *a, **k: seen.append(self.health()["activity"]) or True):
            call()
        self.assertIsNone(self.health()["activity"], "cleared afterwards")
        return seen[0]["kind"] if seen and seen[0] else None

    def test_statistics_refresh(self):
        collector._last_stats_optimize_ts = 0.0
        self.addCleanup(setattr, collector, "_last_stats_optimize_ts", 0.0)
        self.assertEqual(self.seen_during("analyze_db", lambda: collector.refresh_query_planner_stats(self.cfg)),
                         "stats")

    def test_cleanup(self):
        collector._next_cleanup_at = 0.0
        self.assertEqual(self.seen_during("cleanup_old_data",
                                          lambda: collector.run_due_cleanup(self.db, self.cfg["retention"])),
                         "cleanup")

    def test_index_build(self):
        conn = sqlite3.connect(self.db)
        name = collector.LOOKUP_INDEXES[0][0]
        conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.commit()
        conn.close()
        self.assertEqual(self.seen_during("_build_indexes", lambda: collector.ensure_lookup_indexes(self.db)),
                         "index")

    def test_clock_wait(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)", (int(time.time()) + 86400,))
        conn.commit()
        conn.close()

        class Stop(Exception):
            pass

        seen = []

        def sleep(_):
            seen.append(self.health()["activity"])
            raise Stop

        with mock.patch.object(collector, "_seconds_since_boot", return_value=10), \
                mock.patch.object(collector.time, "sleep", side_effect=sleep), self.assertRaises(Stop):
            collector.wait_for_plausible_clock(self.db)
        self.assertEqual(seen[0]["kind"], "clock")
        self.assertIsNone(self.health()["activity"])

    def test_nested_marks_restore_the_outer_one(self):
        with collector._maintenance("index"):
            with collector._maintenance("stats"):
                self.assertEqual(self.health()["activity"]["kind"], "stats")
            self.assertEqual(self.health()["activity"]["kind"], "index")
        self.assertIsNone(self.health()["activity"])


def _h(**kw):
    base = {"last_poll_error": None, "consecutive_failed_polls": 0, "activity": None,
            "last_poll_seconds_ago": 3.0, "uptime_seconds": 9000.0, "empty_seconds": None,
            "messages_static_seconds": None, "feed_static_seconds": None, "save_error": None}
    base.update(kw)
    return base


class TestCollectorState(unittest.TestCase):
    T = 45

    def state(self, writes_ok=False, **kw):
        return server._collector_state(_h(**kw), writes_ok, self.T)

    def test_running(self):
        self.assertEqual(self.state(True), "running")
        # a short cleanup during fresh writes isn't worth an amber card
        self.assertEqual(self.state(True, activity={"kind": "cleanup", "seconds": 2}), "running")

    def test_maintenance_first(self):
        self.assertEqual(self.state(activity={"kind": "stats", "seconds": 247},
                                    last_poll_error="x", consecutive_failed_polls=3), "paused")
        # the clock wait: the newest row is "in the future", so writes look fresh
        self.assertEqual(self.state(True, activity={"kind": "clock", "seconds": 60}), "paused")

    def test_causes_in_order(self):
        self.assertEqual(self.state(feed_static_seconds=300, empty_seconds=10, last_poll_error="x",
                                    consecutive_failed_polls=1), "receiver_stale")
        self.assertEqual(self.state(feed_static_seconds=10, last_poll_error="x",
                                    consecutive_failed_polls=4), "receiver_error")
        self.assertEqual(self.state(last_poll_error="old", consecutive_failed_polls=0,
                                    save_error={"kind": "locked"}), "save_error")
        self.assertEqual(self.state(empty_seconds=372), "no_aircraft")
        self.assertEqual(self.state(last_poll_seconds_ago=371), "not_polling")
        self.assertEqual(self.state(last_poll_seconds_ago=None, uptime_seconds=5), "starting")
        self.assertEqual(self.state(last_poll_seconds_ago=None, uptime_seconds=600), "not_polling")
        self.assertEqual(self.state(), "stalled")

    def test_waiting_states_are_warnings(self):
        sev, failing, reason = server._health_summary(
            {"receiver": True, "database": True, "collector": True, "webserver": True},
            hexdb_ok=True, capacity_ok=True, sudoers=None, system=None,
            collector_warn=server.COLLECTOR_WARN_STATES["no_aircraft"])
        self.assertEqual((sev, failing, reason), ("warn", ["collector"], "the receiver reports no aircraft"))
        self.assertEqual(set(server.COLLECTOR_WARN_STATES), {"paused", "no_aircraft", "starting"})


class TestStatusEndpoint(_PollCase):
    """/api/status's collector component and severity, end to end."""

    def setUp(self):
        super().setUp()
        cfg_path = Path(self.db).parent / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False))
        self.app = server.get_app(self.cfg, str(cfg_path))
        ok = mock.Mock(status_code=200, json=lambda: {"Registration": "G-EZAA"})
        p = mock.patch.object(server.req, "get", return_value=ok)
        p.start()
        self.addCleanup(p.stop)

    def status(self):
        ep = next(r.endpoint for r in self.app.routes
                  if getattr(r, "path", "") == "/api/status" and "GET" in getattr(r, "methods", ()))
        return ep()

    def write(self, age):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)", (int(time.time()) - age,))
        conn.commit()
        conn.close()

    def test_running(self):
        self.write(5)
        collector._last_poll_at = time.monotonic()
        c = self.status()["components"]["collector"]
        self.assertEqual((c["state"], c["ok"], c["warn"]), ("running", True, False))
        self.assertEqual(c["threshold_seconds"], self.cfg["receiver"]["poll_interval"] * 3)

    def test_maintenance_is_amber_not_red(self):
        self.write(600)
        collector._activity = ("stats", time.monotonic() - 247)
        s = self.status()
        c = s["components"]["collector"]
        self.assertEqual((c["state"], c["ok"], c["warn"]), ("paused", False, True))
        self.assertEqual(c["activity"]["kind"], "stats")
        self.assertEqual((s["severity"], s["failing"], s["health_reason"]),
                         ("warn", ["collector"], "collection paused for database maintenance"))
        self.assertTrue(s["overall_ok"])

    def test_no_aircraft_reports_whether_the_receiver_hears_anything(self):
        self.write(600)
        t = time.monotonic()
        collector._last_poll_at = t
        collector._empty_since = t - 400
        collector._messages_last, collector._messages_moved_at = 10, t - 400
        c = self.status()["components"]["collector"]
        self.assertEqual((c["state"], c["warn"], c["receiver_hearing"]), ("no_aircraft", True, False))
        collector._messages_moved_at = t - 1
        self.assertTrue(self.status()["components"]["collector"]["receiver_hearing"])

    def test_a_save_error_is_red(self):
        self.write(600)
        collector._last_poll_at = time.monotonic()
        collector._note_poll_error(sqlite3.OperationalError("database is locked"))
        s = self.status()
        c = s["components"]["collector"]
        self.assertEqual((c["state"], c["ok"], c["warn"]), ("save_error", False, False))
        self.assertEqual(c["save_error"]["kind"], "locked")
        self.assertEqual(s["severity"], "error")
        self.assertIn("collector", s["failing"])


if __name__ == "__main__":
    unittest.main()
