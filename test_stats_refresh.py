"""Query-planner statistics (v3.4.190): a table is analyzed again only when its size has
doubled or halved since its last ANALYZE, so a restart analyzes nothing; sizes come from
rowids, never a scan. Also the log line before init_db drops a big index. Real schema
(init_db + every migration); invented data.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import collector
from schema_migrations import apply_schema_migrations

START = 1_700_000_000


class _StatsCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.cfg = {"data": {"db_file": self.db}}
        self.next_at = START

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def sql(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(sql, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def add(self, n):
        """n sightings, in time order, as the collector writes them."""
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
                         [(f"A{i % 50:05X}", self.next_at + i) for i in range(n)])
        conn.commit()
        conn.close()
        self.next_at += n

    def drop_oldest(self, n):
        """As retention does: the oldest rows go first."""
        self.sql("DELETE FROM all_sightings WHERE rowid IN "
                 "(SELECT rowid FROM all_sightings ORDER BY seen_at LIMIT ?)", (n,))

    def analyze_all(self):
        conn = sqlite3.connect(self.db)
        try:
            collector.analyze_db(conn)
        finally:
            conn.close()

    def refresh(self):
        """One refresh as the first poll after a start makes it; returns what it analyzed."""
        with mock.patch.object(collector, "_last_stats_optimize_ts", 0.0), \
                mock.patch.object(collector, "analyze_db", wraps=collector.analyze_db) as analyze:
            collector.refresh_query_planner_stats(self.cfg)
        return [c.args[1] for c in analyze.call_args_list]

    def stat_rows(self, table="all_sightings"):
        return max(int(s.split()[0]) for (s,) in self.sql("SELECT stat FROM sqlite_stat1 WHERE tbl = ?", (table,)))

    def noted(self):
        row = self.sql("SELECT value FROM _aerodrome_meta WHERE key = ?", (collector.STATS_ROWS_KEY,))
        return json.loads(row[0][0]) if row else None


class TestStatsRefresh(_StatsCase):

    def test_a_restart_analyzes_nothing(self):
        self.add(5000)
        self.analyze_all()
        self.assertEqual(self.refresh(), [])
        self.assertEqual(self.refresh(), [])

    def test_a_table_that_doubled_is_analyzed_alone(self):
        self.add(5000)
        self.analyze_all()
        self.add(6000)
        self.assertEqual(self.refresh(), ["all_sightings"])
        self.assertEqual(self.stat_rows(), 11000)
        self.assertEqual(self.noted()["all_sightings"], 11000)
        self.assertEqual(self.refresh(), [], "noted, so not again")

    def test_a_table_that_halved_is_analyzed(self):
        self.add(10000)
        self.analyze_all()
        self.drop_oldest(6000)
        self.assertEqual(self.refresh(), ["all_sightings"])
        self.assertEqual(self.stat_rows(), 4000)

    def test_small_changes_and_small_tables_are_left_alone(self):
        self.add(5000)
        self.analyze_all()
        self.add(4000)                                   # 1.8x
        self.assertEqual(self.refresh(), [])
        self.sql("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at) VALUES ('B00001', 1, 1)")
        for i in range(2, 900):                          # 1 -> 899 rows, under STATS_MIN_ROWS
            self.sql("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at) VALUES (?, 1, 1)",
                     (f"B{i:05X}",))
        self.assertEqual(self.refresh(), [])

    def test_an_install_analyzed_by_an_older_release_uses_its_sqlite_stat1_count(self):
        self.add(200)
        self.analyze_all()
        self.sql("DELETE FROM _aerodrome_meta WHERE key = ?", (collector.STATS_ROWS_KEY,))
        self.assertEqual(self.refresh(), [], "same size as its numbers say")
        self.add(5000)
        self.assertEqual(self.refresh(), ["all_sightings"])
        self.assertGreater(self.stat_rows(), 1000)

    def test_a_table_never_analyzed_is_analyzed_once_it_matters(self):
        self.sql("DELETE FROM sqlite_stat1")
        self.sql("DELETE FROM _aerodrome_meta WHERE key = ?", (collector.STATS_ROWS_KEY,))
        self.add(500)
        self.assertEqual(self.refresh(), [])
        self.add(1500)
        self.assertEqual(self.refresh(), ["all_sightings"])

    def test_a_full_analyze_notes_every_table_and_forgets_dropped_ones(self):
        self.sql("CREATE TABLE scratch_t (a)")
        self.analyze_all()
        noted = self.noted()
        self.assertLessEqual({"all_sightings", "sightings_hourly", "seen_aircraft", "scratch_t"}, set(noted))
        self.assertNotIn("seen_aircraft_fts", noted, "virtual table")
        self.assertNotIn("_aerodrome_meta", noted, "a few keys rewritten in place")
        for shadow in ("_data", "_idx", "_content", "_docsize", "_config"):
            self.assertNotIn("seen_aircraft_fts" + shadow, noted, "FTS5 keeps its own rowids")
        self.sql("DROP TABLE scratch_t")
        self.analyze_all()
        self.assertNotIn("scratch_t", self.noted())

    def test_the_noskipscan_mark_survives_a_refresh_of_one_table(self):
        collector.ensure_lookup_indexes(self.db)
        self.add(3000)
        self.assertEqual(self.refresh(), ["all_sightings"])
        stat = dict(self.sql("SELECT idx, stat FROM sqlite_stat1"))["idx_all_icao_seen"]
        self.assertEqual(stat.count("noskipscan"), 1)

    def test_a_big_table_is_announced(self):
        self.add(3000)
        self.analyze_all()
        self.add(3000)
        with mock.patch.object(collector, "STATS_BIG_TABLE_ROWS", 2000), \
                self.assertLogs("adsb.collector", level="INFO") as logs:
            self.refresh()
        text = "\n".join(logs.output)
        self.assertIn("Updating query-planner statistics for all_sightings, which had 3,000 rows when they "
                      "were last taken. Collection pauses until it's done.", text)
        self.assertIn("Query-planner stats refreshed for all_sightings: 6,000 rows (sampled ANALYZE", text)

    def test_a_small_one_is_not(self):
        self.add(3000)
        self.analyze_all()
        self.add(3000)
        with self.assertLogs("adsb.collector", level="INFO") as logs:
            self.refresh()
        self.assertNotIn("Updating query-planner statistics", "\n".join(logs.output))

    def test_a_table_rewritten_in_place_settles_after_one_analyze(self):
        """aircraft_track_daily: today's row for each aircraft is rewritten every poll, earlier
        days' rows stay, so its rowids run far ahead of its rows."""
        conn = sqlite3.connect(self.db)
        for day in range(3):
            for poll in range(20):
                conn.executemany("INSERT OR REPLACE INTO aircraft_track_daily (icao, day_bucket, "
                                 "current_session_start, current_session_last, best_session_start, "
                                 "best_session_end) VALUES (?, ?, 1, ?, 1, ?)",
                                 [(f"C{i:05X}", 1_700_006_400 + day * 86400, poll, poll) for i in range(100)])
        conn.commit()
        conn.close()
        self.analyze_all()
        self.sql("DELETE FROM _aerodrome_meta WHERE key = ?", (collector.STATS_ROWS_KEY,))   # as after an upgrade
        self.assertEqual(self.refresh(), ["aircraft_track_daily"], "300 rows, rowid span 4,100: once")
        self.assertEqual(self.stat_rows("aircraft_track_daily"), 300)
        self.assertEqual(self.refresh(), [])

    def test_sizes_come_from_seeks_not_scans(self):
        self.add(100)
        traced = []
        conn = sqlite3.connect(self.db)
        try:
            conn.set_trace_callback(traced.append)
            sizes = collector._table_rows(conn)
            conn.set_trace_callback(None)
            self.assertEqual(sizes["all_sightings"], 100)
            for stmt in traced:
                if stmt.startswith("SELECT M"):
                    plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + stmt))
                    self.assertNotIn("SCAN", plan, stmt)
        finally:
            conn.close()

    def test_rows_after_the_oldest_left_are_counted_exactly(self):
        self.add(3000)
        self.drop_oldest(1200)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(collector._table_rows(conn, ["all_sightings"]), {"all_sightings": 1800})
            self.assertEqual(collector._table_rows(conn, ["no_such_table"]), {})
        finally:
            conn.close()

    def test_a_refresh_that_fails_is_logged_and_never_raises(self):
        with mock.patch.object(collector, "_tables_due_for_stats", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("adsb.collector", level="WARNING") as logs:
            self.refresh()
        self.assertIn("Query-planner stats refresh failed", "\n".join(logs.output))


class TestOldIndexDrop(_StatsCase):

    def test_a_big_index_is_announced_before_it_goes(self):
        self.add(100_001)
        self.sql("CREATE INDEX idx_all_seen ON all_sightings(seen_at)")
        with self.assertLogs("adsb.collector", level="WARNING") as logs:
            collector.init_db(self.db)
        self.assertIn("Removing the unused index idx_all_seen on 100,001 rows, once. "
                      "The web UI starts when it's done.", "\n".join(logs.output))
        self.assertFalse(self.sql("SELECT 1 FROM sqlite_master WHERE name = 'idx_all_seen'"))

    def test_a_small_one_goes_quietly(self):
        self.add(10)
        self.sql("CREATE INDEX idx_all_seen ON all_sightings(seen_at)")
        with self.assertNoLogs("adsb.collector", level="WARNING"):
            collector.init_db(self.db)
        self.assertFalse(self.sql("SELECT 1 FROM sqlite_master WHERE name = 'idx_all_seen'"))


if __name__ == "__main__":
    unittest.main()
