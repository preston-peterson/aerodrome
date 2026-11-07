"""The collector's upkeep work on the real schema (init_db + every migration):
retention in short batches, the read-only lookup indexes, notifications sent after a
poll's transaction, the send hold after a failure, last_distance written only when it
changes, and the capacity row counts' cache. Hexes and data are invented.
"""
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

import yaml

import capacity
import collector
import hexdb_resolver
import main
import notifier
import server
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
DAY = 86400
RETENTION = {"military_days": 30, "watchlist_days": 30, "all_days": 30}


class _DbCase(unittest.TestCase):

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

    def run_sql(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(sql, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def count(self, table):
        return self.run_sql(f"SELECT COUNT(*) FROM {table}")[0][0]

    def indexes(self):
        return {r[0] for r in self.run_sql("SELECT name FROM sqlite_master WHERE type = 'index'")}

    def plan(self, sql, args=()):
        return " | ".join(r[3] for r in self.run_sql("EXPLAIN QUERY PLAN " + sql, args))


class TestCleanup(_DbCase):

    def seed(self, ts):
        """One row at ts in every table retention looks after."""
        conn = sqlite3.connect(self.db)
        for table in ("all_sightings", "military_sightings", "watchlist_sightings"):
            conn.execute(f"INSERT INTO {table} (icao, seen_at) VALUES ('A00001', ?)", (ts,))
        bucket = ts - ts % 3600
        for table in ("military_hourly", "watchlist_hourly"):
            label = "watchlist_label" if table == "watchlist_hourly" else "special_label"
            conn.execute(f"INSERT INTO {table} (icao, hour_bucket, {label}, sighting_count, first_seen_at, "
                         f"last_seen_at) VALUES ('A00001', ?, 'x', 1, ?, ?)", (bucket, ts, ts))
        conn.execute("INSERT INTO hexdb_events (ts, kind, icao) VALUES (?, 'hit', 'A00001')", (ts,))
        conn.execute("INSERT INTO concurrent_minute (minute_bucket, count) VALUES (?, 1)", (ts,))
        conn.execute("INSERT INTO aircraft_track_daily (icao, day_bucket, current_session_start, "
                     "current_session_last, best_session_start, best_session_end) "
                     "VALUES ('A00001', ?, ?, ?, ?, ?)", (ts - ts % DAY, ts, ts, ts, ts))
        conn.commit()
        conn.close()

    TABLES = ("all_sightings", "military_sightings", "watchlist_sightings", "military_hourly",
              "watchlist_hourly", "concurrent_minute", "aircraft_track_daily")

    def test_old_rows_go_and_recent_rows_stay_in_every_table(self):
        now = int(time.time())
        self.seed(now - 40 * DAY)
        self.seed(now - 5 * DAY)
        self.assertTrue(collector.cleanup_old_data(self.db, RETENTION))
        for table in self.TABLES:
            self.assertEqual(self.count(table), 1, table)
        # hexdb_events has its own, shorter retention
        self.assertEqual(self.run_sql("SELECT COUNT(*) FROM hexdb_events WHERE ts < ?",
                                      (now - collector.HEXDB_EVENTS_RETENTION_DAYS * DAY,))[0][0], 0)

    def test_a_rollup_bucket_goes_only_when_the_whole_hour_is_past(self):
        now = int(time.time())
        cutoff = now - 30 * DAY
        straddling = cutoff - cutoff % 3600
        self.run_sql("INSERT INTO military_hourly (icao, hour_bucket, special_label, sighting_count, first_seen_at, "
                     "last_seen_at) VALUES ('A00001', ?, 'x', 1, ?, ?), ('A00002', ?, 'x', 1, ?, ?)",
                     (straddling, straddling, straddling,
                      straddling - 3600, straddling - 3600, straddling - 1))
        collector.cleanup_old_data(self.db, RETENTION)
        self.assertEqual(self.run_sql("SELECT icao FROM military_hourly"), [("A00001",)])

    def test_todays_track_row_survives_the_shortest_retention(self):
        now = int(time.time())
        self.seed(now)
        collector.cleanup_old_data(self.db, {"military_days": 1, "watchlist_days": 1, "all_days": 1})
        self.assertEqual(self.count("aircraft_track_daily"), 1)

    def test_out_of_budget_it_stops_and_the_next_round_finishes(self):
        old = int(time.time()) - 40 * DAY
        recent = int(time.time()) - DAY
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)",
                         [(old + i,) for i in range(25)] + [(recent,)])
        conn.commit()
        conn.close()
        with mock.patch.object(collector, "CLEANUP_BATCH_ROWS", 10), \
                mock.patch.object(collector, "CLEANUP_BUDGET_SEC", 0):
            self.assertFalse(collector.cleanup_old_data(self.db, RETENTION))
            # Committed: another connection sees it, and can write right now.
            self.assertEqual(self.count("all_sightings"), 16)
            self.run_sql("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00002', ?)", (recent,))
            self.assertFalse(collector.cleanup_old_data(self.db, RETENTION))
            self.assertEqual(self.count("all_sightings"), 7)
            self.assertTrue(collector.cleanup_old_data(self.db, RETENTION))
        self.assertEqual(self.count("all_sightings"), 2)

    def test_a_batch_exactly_the_size_of_what_is_left_finishes_on_the_next_pass(self):
        old = int(time.time()) - 40 * DAY
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)",
                         [(old + i,) for i in range(20)])
        conn.commit()
        conn.close()
        with mock.patch.object(collector, "CLEANUP_BATCH_ROWS", 10):
            self.assertTrue(collector.cleanup_old_data(self.db, RETENTION))
        self.assertEqual(self.count("all_sightings"), 0)

    def test_a_table_an_old_database_lacks_is_skipped(self):
        self.run_sql("DROP TABLE aircraft_track_daily")
        self.run_sql("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)", (int(time.time()) - 40 * DAY,))
        self.assertTrue(collector.cleanup_old_data(self.db, RETENTION))
        self.assertEqual(self.count("all_sightings"), 0)

    def test_every_prune_seeks_an_index(self):
        for table, column in (("hexdb_events", "ts"), ("concurrent_minute", "minute_bucket"),
                              ("military_hourly", "hour_bucket"), ("watchlist_hourly", "hour_bucket"),
                              ("aircraft_track_daily", "day_bucket"), ("military_sightings", "seen_at"),
                              ("watchlist_sightings", "seen_at"), ("all_sightings", "seen_at")):
            plan = self.plan(f"SELECT rowid FROM {table} WHERE {column} < ? LIMIT 5", (1,))
            self.assertTrue(plan.startswith("SEARCH"), f"{table}: {plan}")


class TestCleanupSchedule(_DbCase):

    def setUp(self):
        super().setUp()
        collector._next_cleanup_at = 0.0
        collector._last_cleanup_retention = None
        self.addCleanup(setattr, collector, "_next_cleanup_at", 0.0)
        self.addCleanup(setattr, collector, "_last_cleanup_retention", None)

    def test_runs_once_then_waits(self):
        with mock.patch.object(collector, "cleanup_old_data", return_value=True) as clean:
            collector.run_due_cleanup(self.db, RETENTION)
            collector.run_due_cleanup(self.db, RETENTION)
        self.assertEqual(clean.call_count, 1)

    def test_a_changed_retention_setting_runs_at_once(self):
        with mock.patch.object(collector, "cleanup_old_data", return_value=True) as clean:
            collector.run_due_cleanup(self.db, RETENTION)
            collector.run_due_cleanup(self.db, dict(RETENTION, all_days=7))
        self.assertEqual(clean.call_count, 2)

    def test_while_catching_up_the_wait_is_short(self):
        with mock.patch.object(collector, "cleanup_old_data", return_value=False):
            collector.run_due_cleanup(self.db, RETENTION)
        self.assertLessEqual(collector._next_cleanup_at - time.monotonic(), collector.CLEANUP_CATCH_UP_SEC)

    def test_a_failed_round_stays_due(self):
        with mock.patch.object(collector, "cleanup_old_data",
                               side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                collector.run_due_cleanup(self.db, RETENTION)
        with mock.patch.object(collector, "cleanup_old_data", return_value=True) as clean:
            collector.run_due_cleanup(self.db, RETENTION)
        self.assertEqual(clean.call_count, 1)


class TestLookupIndexes(_DbCase):

    def old_install(self):
        """As every install before these indexes: the old per-aircraft index, rows in the table."""
        self.run_sql("CREATE INDEX idx_all_icao ON all_sightings(icao)")
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)",
                         [(1000 + i,) for i in range(50)])
        conn.commit()
        conn.close()

    def test_a_new_install_has_no_old_index_to_replace(self):
        self.assertNotIn("idx_all_icao", self.indexes())
        collector.ensure_lookup_indexes(self.db)
        self.assertLessEqual({"idx_all_icao_seen", "idx_hourly_icao_sum"}, self.indexes())
        self.assertNotIn("idx_all_icao", self.indexes())

    def test_an_old_install_swaps_the_index_and_keeps_its_rows(self):
        self.old_install()
        collector.ensure_lookup_indexes(self.db)
        names = self.indexes()
        self.assertLessEqual({"idx_all_icao_seen", "idx_hourly_icao_sum"}, names)
        self.assertNotIn("idx_all_icao", names)
        self.assertEqual(self.count("all_sightings"), 50)
        # The planner has numbers for the new index at once.
        self.assertTrue(self.run_sql("SELECT 1 FROM sqlite_stat1 WHERE idx = 'idx_all_icao_seen'"))

    def test_init_db_on_an_upgraded_install_does_not_bring_the_old_index_back(self):
        self.old_install()
        collector.ensure_lookup_indexes(self.db)
        collector.init_db(self.db)
        self.assertNotIn("idx_all_icao", self.indexes())

    def test_after_a_downgrade_and_back_the_old_index_goes_again(self):
        self.old_install()
        collector.ensure_lookup_indexes(self.db)
        # 3.4.152's init_db puts its index back.
        self.run_sql("CREATE INDEX IF NOT EXISTS idx_all_icao ON all_sightings(icao)")
        with mock.patch.object(collector.shutil, "disk_usage") as usage:
            collector.ensure_lookup_indexes(self.db)
        usage.assert_not_called()
        self.assertNotIn("idx_all_icao", self.indexes())
        self.assertIn("idx_all_icao_seen", self.indexes())

    def test_a_second_run_does_nothing(self):
        collector.ensure_lookup_indexes(self.db)
        with mock.patch.object(collector.shutil, "disk_usage") as usage:
            collector.ensure_lookup_indexes(self.db)
        usage.assert_not_called()

    def test_per_aircraft_queries_read_in_order_with_no_sort(self):
        collector.ensure_lookup_indexes(self.db)
        for sql, args in (
                ("SELECT seen_at, callsign FROM all_sightings WHERE icao = ? ORDER BY seen_at DESC LIMIT 20", ("A",)),
                ("SELECT seen_at, lat FROM all_sightings WHERE icao = ? AND seen_at >= ? ORDER BY seen_at DESC", ("A", 1)),
                ("SELECT seen_at FROM all_sightings WHERE icao = ? AND seen_at >= ? AND seen_at <= ? "
                 "ORDER BY seen_at DESC LIMIT 100", ("A", 1, 2))):
            plan = self.plan(sql, args)
            self.assertIn("idx_all_icao_seen", plan)
            self.assertNotIn("TEMP B-TREE", plan)

    def test_the_window_sum_reads_only_the_index(self):
        collector.ensure_lookup_indexes(self.db)
        plan = self.plan("SELECT SUM(sighting_count) FROM sightings_hourly "
                         "WHERE icao = ? AND hour_bucket >= ? AND hour_bucket < ?", ("A", 1, 2))
        self.assertIn("COVERING INDEX idx_hourly_icao_sum", plan)

    def busy_install(self):
        """Enough rows per aircraft for ANALYZE's numbers to invite a skip-scan."""
        now = int(time.time())
        conn = sqlite3.connect(self.db)
        for i in range(8):
            icao = f"A0000{i}"
            conn.executemany("INSERT INTO all_sightings (icao, callsign, seen_at) VALUES (?, 'X', ?)",
                             [(icao, now - j * 60) for j in range(120)])
            conn.executemany("INSERT INTO sightings_hourly (icao, hour_bucket, sighting_count, first_seen_at, "
                             "last_seen_at) VALUES (?, ?, 1, 1, 1)",
                             [(icao, (now // 3600 - j) * 3600) for j in range(120)])
        conn.commit()
        conn.close()
        return now

    def assert_no_skip_scan(self, now):
        for sql, args in (
                ("SELECT icao, MAX(callsign) FROM all_sightings WHERE seen_at >= ? AND seen_at < ? GROUP BY icao",
                 (now - 60, now)),
                ("SELECT COUNT(*) FROM all_sightings WHERE seen_at >= ?", (now - 60,)),
                ("SELECT COALESCE(SUM(sighting_count), 0) FROM sightings_hourly WHERE hour_bucket >= ?", (now - 3600,)),
                ("SELECT COUNT(DISTINCT icao) FROM sightings_hourly WHERE hour_bucket >= ?", (now - 3600,))):
            # (sightings_hourly's own primary key can still be skip-scanned, as it always could.)
            plan = self.plan(sql, args)
            self.assertNotIn("idx_all_icao_seen", plan, sql)
            self.assertNotIn("idx_hourly_icao_sum", plan, sql)

    def test_a_build_leaves_numbers_that_rule_out_a_skip_scan(self):
        now = self.busy_install()
        collector.ensure_lookup_indexes(self.db)
        stats = dict(self.run_sql("SELECT idx, stat FROM sqlite_stat1"))
        self.assertIn("noskipscan", stats["idx_all_icao_seen"])
        self.assertIn("noskipscan", stats["idx_hourly_icao_sum"])
        self.assert_no_skip_scan(now)

    def test_the_regular_refresh_keeps_the_mark(self):
        now = self.busy_install()
        collector.ensure_lookup_indexes(self.db)
        with mock.patch.object(collector, "_last_stats_optimize_ts", 0):
            collector.refresh_query_planner_stats({"data": {"db_file": self.db}})
        stat = dict(self.run_sql("SELECT idx, stat FROM sqlite_stat1"))["idx_all_icao_seen"]
        self.assertEqual(stat.count("noskipscan"), 1)
        self.assert_no_skip_scan(now)

    def test_a_second_analyze_does_not_repeat_the_mark(self):
        self.busy_install()
        collector.ensure_lookup_indexes(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE sqlite_stat1 SET stat = stat WHERE 1")   # leaves the mark in place
        conn.commit()
        collector.analyze_db(conn)
        collector.analyze_db(conn)
        conn.close()
        stat = dict(self.run_sql("SELECT idx, stat FROM sqlite_stat1"))["idx_hourly_icao_sum"]
        self.assertEqual(stat.count("noskipscan"), 1)

    def test_every_analyze_in_the_app_goes_through_the_helper(self):
        for name in ("collector.py", "server.py", "main.py", "schema_migrations.py", "search.py"):
            calls = re.findall(r"""execute\(f?["']ANALYZE""", (REPO / name).read_text(encoding="utf-8"))
            allowed = 1 if name == "collector.py" else 0     # analyze_db itself
            self.assertEqual(len(calls), allowed, name)

    def test_too_little_disk_builds_nothing_and_the_next_start_tries_again(self):
        self.old_install()
        low = namedtuple("usage", "total used free")(100, 100, 1024)
        with mock.patch.object(collector.shutil, "disk_usage", return_value=low), \
                self.assertLogs("adsb.collector", "WARNING") as logs:
            collector.ensure_lookup_indexes(self.db)
        self.assertIn("only 0 MB free", "\n".join(logs.output))
        self.assertIn("idx_all_icao", self.indexes())
        self.assertNotIn("idx_all_icao_seen", self.indexes())
        collector.ensure_lookup_indexes(self.db)
        self.assertIn("idx_all_icao_seen", self.indexes())

    def test_a_locked_database_changes_nothing_and_never_raises(self):
        self.old_install()
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(collector, "_STARTUP_DB_TIMEOUT", 0.05), \
                    self.assertLogs("adsb.collector", "WARNING"):
                collector.ensure_lookup_indexes(self.db)
        finally:
            holder.rollback()
            holder.close()
        self.assertIn("idx_all_icao", self.indexes())
        self.assertNotIn("idx_all_icao_seen", self.indexes())

    def test_a_failure_part_way_keeps_the_old_index(self):
        self.old_install()
        real = sqlite3.connect
        def fails_on_drop(*a, **kw):
            conn = real(*a, **kw)
            conn.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY
                                if action == sqlite3.SQLITE_DROP_INDEX else sqlite3.SQLITE_OK)
            return conn
        with mock.patch.object(collector.sqlite3, "connect", side_effect=fails_on_drop), \
                self.assertLogs("adsb.collector", "WARNING"):
            collector.ensure_lookup_indexes(self.db)
        names = self.indexes()
        self.assertIn("idx_all_icao", names)
        self.assertNotIn("idx_all_icao_seen", names)

    def test_a_missing_database_folder_never_raises(self):
        with self.assertLogs("adsb.collector", "WARNING"):
            collector.ensure_lookup_indexes(os.path.join(tempfile.gettempdir(), "no-such-dir-b7", "x.db"))

    def test_the_collector_builds_them_before_its_first_poll(self):
        stop = threading.Event()
        order = []
        def poll(*_a):
            order.append("poll")
            stop.set()
        cfg = {"receiver": {"poll_interval": 0}, "data": {"db_file": self.db}, "watchlist": []}
        with mock.patch.object(main, "ensure_lookup_indexes", side_effect=lambda _d: order.append("index")), \
                mock.patch.object(main, "fetch_and_store", side_effect=poll), \
                mock.patch.object(main, "sync_watchlist_tails"), \
                mock.patch.object(main, "check_capacity_alerts"), \
                mock.patch.object(main, "refresh_query_planner_stats"):
            main.run_collector(cfg, stop)
        self.assertEqual(order, ["index", "poll"])


class _FakeNotifier:
    def __init__(self, on_notify=None):
        self.sent = []
        self.on_notify = on_notify

    def notify(self, event, title, body, **_kw):
        if self.on_notify:
            self.on_notify(event)
        self.sent.append(event)


def _feed(aircraft):
    payload = {"now": 0, "aircraft": aircraft}
    resp = mock.Mock(status_code=200, headers={"Content-Type": "application/json"})
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    resp.raise_for_status = lambda: None
    return resp


EMERGENCY = {"hex": "a00077", "flight": "DEMO77", "lat": 40.0, "lon": -90.0, "alt_baro": 9000,
             "gs": 200, "t": "B738", "squawk": "7700"}


class TestReleaseDUpkeep(_DbCase):
    """v3.4.171: a poll with no aircraft still runs cleanup; idx_all_seen(seen_at)
    is gone (the seen_at-leading composites cover it) and range reads still search
    an index."""

    def plan(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        finally:
            conn.close()

    def indexes(self):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='all_sightings'")}
        finally:
            conn.close()

    def test_a_poll_with_no_aircraft_still_cleans_up(self):
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        with mock.patch.object(collector.requests, "get", return_value=_feed([])), \
                mock.patch.object(collector, "run_due_cleanup") as clean:
            collector.fetch_and_store(cfg, collector.build_watchlist_lookup(cfg))
        clean.assert_called_once_with(self.db, cfg["retention"])

    def test_a_fresh_install_has_no_idx_all_seen(self):
        self.assertNotIn("idx_all_seen", self.indexes())
        self.assertIn("idx_all_seen_icao", self.indexes())

    def test_an_upgrade_drops_it(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE INDEX idx_all_seen ON all_sightings(seen_at)")
        conn.commit()
        conn.close()
        self.assertIn("idx_all_seen", self.indexes())
        collector.init_db(self.db)
        self.assertNotIn("idx_all_seen", self.indexes())

    def test_seen_at_reads_still_search_an_index(self):
        for sql in ("SELECT icao FROM all_sightings WHERE seen_at >= ? ORDER BY seen_at LIMIT 1",
                    "SELECT COUNT(*) FROM all_sightings WHERE seen_at >= ?",
                    "SELECT rowid FROM all_sightings WHERE seen_at < ? LIMIT 5000"):
            plan = self.plan(sql, (1,))
            self.assertIn("USING", plan, sql)
            self.assertNotIn("idx_all_seen ", plan + " ", sql)
            self.assertFalse(plan.startswith("SCAN all_sightings") and "INDEX" not in plan, plan)


class TestPollNotifications(_DbCase):

    def setUp(self):
        super().setUp()
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        for p in (mock.patch.object(hexdb_resolver, "enqueue", return_value=False),
                  mock.patch.object(hexdb_resolver, "start_worker")):
            p.start()
            self.addCleanup(p.stop)
        collector._last_squawk_by_icao.clear()
        self.addCleanup(collector._last_squawk_by_icao.clear)

    def poll(self):
        with mock.patch.object(collector.requests, "get", return_value=_feed([EMERGENCY])):
            collector.fetch_and_store(self.cfg, collector.build_watchlist_lookup(self.cfg))

    def test_a_send_happens_with_the_poll_committed_and_the_write_lock_free(self):
        seen = {}
        def check(_event):
            other = sqlite3.connect(self.db, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")     # raises if the poll still holds the lock
                other.rollback()
                seen["rows"] = other.execute("SELECT COUNT(*) FROM all_sightings").fetchone()[0]
            finally:
                other.close()
        fake = _FakeNotifier(check)
        with mock.patch.object(collector, "_notifier", fake):
            self.poll()
        self.assertEqual(fake.sent, ["emergency_squawk"])
        self.assertEqual(seen["rows"], 1)

    def test_a_poll_that_fails_still_sends_and_does_not_leave_sends_waiting(self):
        fake = _FakeNotifier()
        with mock.patch.object(collector, "_notifier", fake), \
                mock.patch.object(collector, "_flush_dirty_to_fts", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.poll()
        self.assertEqual(fake.sent, ["emergency_squawk"])
        self.assertIsNone(collector._poll_notifications.pending)
        with mock.patch.object(collector, "_notifier", fake):
            collector._safe_notify("test", "t", "b")
        self.assertEqual(fake.sent, ["emergency_squawk", "test"])

    def test_a_notifier_that_raises_does_not_break_the_poll(self):
        def boom(_event):
            raise RuntimeError("boom")
        with mock.patch.object(collector, "_notifier", _FakeNotifier(boom)):
            self.poll()
        self.assertEqual(self.count("all_sightings"), 1)

    def test_only_the_collectors_own_notifications_wait(self):
        fake = _FakeNotifier()
        collector._poll_notifications.pending = []
        self.addCleanup(setattr, collector._poll_notifications, "pending", None)
        with mock.patch.object(collector, "_notifier", fake):
            t = threading.Thread(target=collector._safe_notify, args=("test", "t", "b"))
            t.start()
            t.join()
            self.assertEqual(fake.sent, ["test"])
            collector._safe_notify("test", "t", "b")
            self.assertEqual(fake.sent, ["test"])
            self.assertEqual(len(collector._poll_notifications.pending), 1)


class TestSendHold(unittest.TestCase):

    def setUp(self):
        self.n = notifier.Notifier({"enabled": True, "url": "http://ntfy.example/topic",
                                    "rate_limit_per_hour": 0,
                                    "events": {"watchlist_hit": True, "special_aircraft": True,
                                               "emergency_squawk": True, "new_record": True}})
        self.clock = 1_000_000.0
        for p in (mock.patch.object(notifier.time, "time", side_effect=lambda: self.clock),):
            p.start()
            self.addCleanup(p.stop)
        self.post = mock.patch.object(self.n, "_post_ntfy", return_value=(False, 0, "Timeout (5s) reaching ntfy"))
        self.posted = self.post.start()
        self.addCleanup(self.post.stop)

    def hit(self, event="watchlist_hit", icao="A00001"):
        return self.n.notify(event, "t", "b", aircraft_icao=icao)

    def test_after_a_failure_repeating_events_wait(self):
        self.hit()
        self.hit()
        self.hit(icao="A00002")
        self.hit("special_aircraft")
        self.assertEqual(self.posted.call_count, 1)
        self.assertEqual(self.n._drops["send_hold"], 3)

    def test_events_that_fire_once_still_get_their_attempt(self):
        self.hit()
        self.hit("emergency_squawk")
        self.hit("new_record")
        self.assertEqual(self.posted.call_count, 3)

    def test_the_wait_doubles_to_a_limit(self):
        waits = []
        for _ in range(6):
            self.hit()
            waits.append(self.n._send_hold_until - self.clock)
            self.clock = self.n._send_hold_until
        self.assertEqual(waits, [60, 120, 240, 480, 600, 600])

    def test_the_server_coming_back_is_noticed_and_clears_the_hold(self):
        self.hit()
        self.posted.return_value = (True, 200, "")
        self.clock += notifier.SEND_HOLD_FIRST_SEC
        self.assertTrue(self.hit())
        self.assertEqual(self.n._send_hold_until, 0.0)
        self.assertEqual(self.n._send_hold_sec, notifier.SEND_HOLD_FIRST_SEC)
        self.assertTrue(self.hit(icao="A00002"))

    def test_saving_settings_clears_the_hold(self):
        self.hit()
        self.n.update_config({"enabled": True, "url": "http://ntfy.example/other",
                              "rate_limit_per_hour": 0, "events": {"watchlist_hit": True}})
        self.hit(icao="A00002")
        self.assertEqual(self.posted.call_count, 2)


class TestLastDistance(_DbCase):

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.executemany(
            "INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at, last_lat, last_lon) VALUES (?, 1, 2, ?, ?)",
            [("A00001", 41.0, -88.0), ("A00002", 42.0, -89.0), ("A00003", None, None)])
        # Count real writes, whatever SQLite's change counters say.
        conn.execute("CREATE TABLE writes (icao TEXT)")
        conn.execute("CREATE TRIGGER counted AFTER UPDATE OF last_distance ON seen_aircraft "
                     "BEGIN INSERT INTO writes VALUES (new.icao); END")
        conn.commit()
        conn.close()

    def writes(self):
        n = self.count("writes")
        self.run_sql("DELETE FROM writes")
        return n

    def test_a_start_with_nothing_changed_writes_nothing(self):
        self.assertEqual(server._recompute_all_last_distance(self.db, 40.0, -88.0), 2)
        self.assertEqual(self.writes(), 2)
        self.assertEqual(server._recompute_all_last_distance(self.db, 40.0, -88.0), 0)
        self.assertEqual(self.writes(), 0)

    def test_a_moved_receiver_writes_every_row_with_coordinates(self):
        server._recompute_all_last_distance(self.db, 40.0, -88.0)
        self.writes()
        self.assertEqual(server._recompute_all_last_distance(self.db, 45.0, -80.0), 2)
        self.assertEqual(self.writes(), 2)
        d = dict(self.run_sql("SELECT icao, last_distance FROM seen_aircraft"))
        self.assertAlmostEqual(d["A00001"], server._haversine(45.0, -80.0, 41.0, -88.0))
        self.assertIsNone(d["A00003"])

    def test_a_stored_distance_with_no_coordinates_is_cleared_once(self):
        server._recompute_all_last_distance(self.db, 40.0, -88.0)
        self.run_sql("UPDATE seen_aircraft SET last_distance = 5 WHERE icao = 'A00003'")
        self.writes()
        server._recompute_all_last_distance(self.db, 40.0, -88.0)
        self.assertEqual(self.writes(), 1)
        self.assertIsNone(self.run_sql("SELECT last_distance FROM seen_aircraft WHERE icao = 'A00003'")[0][0])

    def test_no_receiver_location_clears_and_then_writes_nothing(self):
        server._recompute_all_last_distance(self.db, 40.0, -88.0)
        self.writes()
        with mock.patch.dict(server.CONFIG, {"receiver": {}}):
            server._recompute_all_last_distance(self.db, None, None)
            self.assertEqual(self.writes(), 2)
            server._recompute_all_last_distance(self.db, None, None)
            self.assertEqual(self.writes(), 0)

    def test_the_collectors_own_value_counts_as_unchanged(self):
        collector.set_receiver_location(40.0, -88.0)
        self.addCleanup(collector.set_receiver_location, None, None)
        for icao, lat, lon in (("A00001", 41.0, -88.0), ("A00002", 42.0, -89.0)):
            self.run_sql("UPDATE seen_aircraft SET last_distance = ? WHERE icao = ?",
                         (collector._compute_distance_km(lat, lon), icao))
        self.writes()
        self.assertEqual(server._recompute_all_last_distance(self.db, 40.0, -88.0), 0)


class TestCapacityCounts(_DbCase):

    def setUp(self):
        super().setUp()
        capacity._rowcount_cache.clear()
        self.addCleanup(capacity._rowcount_cache.clear)
        now = int(time.time())
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)",
                         [(now - 20 * DAY,)] + [(now - DAY,)] * 7)
        conn.commit()
        conn.close()

    def metric(self):
        return capacity._compute_capacity_metrics(self.db, 30)["rows_per_day"]

    def test_the_weekly_count_is_served_from_the_cache_until_it_expires(self):
        self.assertEqual(self.metric(), 1.0)
        self.run_sql("INSERT INTO all_sightings (icao, seen_at) VALUES ('A00001', ?)", (int(time.time()),))
        self.assertEqual(self.metric(), 1.0)
        self.assertEqual(capacity._rowcount_cache[(self.db, "last_7_days")][0], 7)
        later = time.time() + capacity._ROWCOUNT_CACHE_TTL_SEC + 1
        with mock.patch.object(capacity.time, "time", return_value=later):
            self.metric()
        self.assertEqual(capacity._rowcount_cache[(self.db, "last_7_days")][0], 8)

    def test_two_databases_do_not_share_a_count(self):
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(capacity._cached_count(conn, "one.db", "n", "SELECT 1"), 1)
            self.assertEqual(capacity._cached_count(conn, "one.db", "n", "SELECT 2"), 1)
            self.assertEqual(capacity._cached_count(conn, "two.db", "n", "SELECT 2"), 2)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
