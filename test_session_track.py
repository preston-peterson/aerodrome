"""
Tests for the v2.88.0 aircraft_track_daily session-tracking rollup.

Two layers:
  1. _local_day_bucket — verifies tz-aware day-bucket computation matches
     server._day_bounds_ts() semantics (local midnight, system fallback,
     handles DST transitions sanely).
  2. End-to-end against a real SQLite DB — drives the migration v6 backfill
     against a small synthetic dataset, then drives the per-poll write path
     by simulating the collector loop's UPSERT logic. Verifies the
     longest_track read query returns the expected (icao, duration) for
     each scenario.

The end-to-end path is what catches the bug class AST + pure-unit tests
can't see — column-name typos, schema drift, semantic mismatches between
the migration backfill walk and the live UPSERT logic. Same lesson as
v2.86.4 → v2.87.3: only runtime-against-real-schema catches those.

Run:
    python3 test_session_track.py
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# -----------------------------------------------------------------------------
# Layer 1: _local_day_bucket helper
# -----------------------------------------------------------------------------

class TestLocalDayBucket(unittest.TestCase):
    """Verify the collector's tz-aware day-bucket computation. Imports from
    the collector module after monkey-patching its tz state."""

    def setUp(self):
        # Reset the module-level tz cache between tests. The collector
        # caches the parsed ZoneInfo so per-poll calls don't re-parse.
        import collector as _coll
        self._coll = _coll
        _coll.set_session_track_config("", 5)  # reset to system tz

    def test_utc_tz(self):
        """UTC tz: day_bucket should align to UTC midnight."""
        self._coll.set_session_track_config("UTC", 5)
        # 2026-05-07 18:30:00 UTC
        ts = int(datetime(2026, 5, 7, 18, 30, 0).timestamp())
        # Adjust to actually be UTC: build the timestamp from a UTC datetime
        from datetime import timezone
        ts = int(datetime(2026, 5, 7, 18, 30, 0, tzinfo=timezone.utc).timestamp())
        bucket = self._coll._local_day_bucket(ts)
        expected = int(datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(bucket, expected)

    def test_west_tz_evening_doesnt_advance_to_tomorrow(self):
        """For US Central (UTC-6/UTC-5), 23:00 local on day D should
        bucket to D, not D+1, even though UTC is the next day."""
        self._coll.set_session_track_config("America/Chicago", 5)
        # 2026-05-07 23:30 in Chicago (CDT, UTC-5).
        # Build an aware datetime at that wall-clock time.
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            self.skipTest("zoneinfo not available")
        chi = ZoneInfo("America/Chicago")
        wall = datetime(2026, 5, 7, 23, 30, 0, tzinfo=chi)
        ts = int(wall.timestamp())
        bucket = self._coll._local_day_bucket(ts)
        expected = int(datetime(2026, 5, 7, 0, 0, 0, tzinfo=chi).timestamp())
        self.assertEqual(bucket, expected,
                         "23:30 Chicago should bucket to today-Chicago, not tomorrow")

    def test_east_tz_early_morning_doesnt_regress_to_yesterday(self):
        """For Asia/Tokyo (UTC+9), 02:00 local on day D should bucket
        to D, not D-1, even though UTC is the previous day."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            self.skipTest("zoneinfo not available")
        self._coll.set_session_track_config("Asia/Tokyo", 5)
        tok = ZoneInfo("Asia/Tokyo")
        wall = datetime(2026, 5, 7, 2, 0, 0, tzinfo=tok)
        ts = int(wall.timestamp())
        bucket = self._coll._local_day_bucket(ts)
        expected = int(datetime(2026, 5, 7, 0, 0, 0, tzinfo=tok).timestamp())
        self.assertEqual(bucket, expected,
                         "02:00 Tokyo should bucket to today-Tokyo, not yesterday")

    def test_invalid_tz_falls_back_to_system(self):
        """Empty/garbage tz string should not crash — falls back to system local."""
        self._coll.set_session_track_config("Not/A/Real/Tz", 5)
        ts = int(time.time())
        # Should not raise. Just verify the bucket is sane (today's
        # local-midnight, give or take 24h slack for any tz).
        bucket = self._coll._local_day_bucket(ts)
        self.assertGreater(bucket, ts - 86400 - 3600)
        self.assertLessEqual(bucket, ts)

    def test_gap_min_clamping(self):
        """gap_minutes < 1 clamps to 1 (we never want a zero or negative gap)."""
        self._coll.set_session_track_config("UTC", 0)
        self.assertEqual(self._coll._session_gap_min, 1)
        self._coll.set_session_track_config("UTC", -10)
        self.assertEqual(self._coll._session_gap_min, 1)

    def test_gap_min_default(self):
        """None gap_minutes → default 5."""
        self._coll.set_session_track_config("UTC", None)
        self.assertEqual(self._coll._session_gap_min, 5)


# -----------------------------------------------------------------------------
# Layer 2: End-to-end against a real SQLite DB
# -----------------------------------------------------------------------------

class TestSessionTrackEndToEnd(unittest.TestCase):
    """Drive the migration v6 backfill against a synthetic all_sightings
    dataset, then verify the longest_track read query returns the right
    answer for each scenario.

    Day-bucket alignment is UTC (via `set_v6_backfill_config(stats_tz='UTC')`)
    so test assertions are reproducible regardless of the host's timezone."""

    def setUp(self):
        # Fresh DB per test.
        self.db_path = tempfile.mktemp(suffix=".db")
        self.conn = sqlite3.connect(self.db_path)
        # Run init_db to create the base schema (all_sightings etc.).
        from collector import init_db
        self.conn.close()
        init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

        # Pin "today" to 2026-05-07 UTC for reproducibility. The
        # migration computes "today" as datetime.now(tz)'s local
        # midnight — which depends on wall-clock time. We work around
        # this by inserting all_sightings rows whose seen_at falls
        # within today UTC (using actual wall-clock time + offsets),
        # so the migration's "today" computation captures them.
        from datetime import timezone
        now_dt = datetime.now(tz=timezone.utc)
        self.today_midnight = int(
            now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

    def tearDown(self):
        self.conn.close()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def _insert_sightings(self, sightings):
        """sightings: list of (icao, seen_at_offset_seconds, callsign, type)."""
        for icao, off, cs, ty in sightings:
            seen_at = self.today_midnight + off
            self.conn.execute(
                "INSERT INTO all_sightings "
                "(icao, callsign, speed, lat, lon, altitude, "
                " aircraft_type, type_desc, seen_at, squawk) "
                "VALUES (?, ?, NULL, NULL, NULL, NULL, ?, '', ?, '')",
                (icao, cs, ty, seen_at)
            )
        self.conn.commit()

    def _run_v6_migration(self, gap_min=5):
        from schema_migrations import apply_schema_migrations, set_v6_backfill_config
        set_v6_backfill_config("UTC", gap_min)
        result = apply_schema_migrations(self.conn, "test-2.88.0")
        self.assertTrue(result["ok"],
                        f"Migration failed: {result.get('error')}")

    def _read_longest(self):
        """Run the same query the Stats card uses."""
        rows = self.conn.execute("""
            SELECT icao, callsign, aircraft_type,
                   best_session_duration AS dur,
                   best_session_start    AS first_seen,
                   best_session_end      AS last_seen
            FROM aircraft_track_daily
            WHERE day_bucket >= ? AND best_session_duration > 0
              AND icao NOT LIKE '~%'
            ORDER BY best_session_duration DESC LIMIT 1
        """, (self.today_midnight,)).fetchall()
        return dict(rows[0]) if rows else None

    def test_empty_db_returns_none(self):
        """No sightings → no rollup rows → longest_track returns None."""
        self._run_v6_migration()
        self.assertIsNone(self._read_longest())

    def test_single_sighting_returns_none(self):
        """One sighting (duration 0) is filtered by best_session_duration > 0."""
        self._insert_sightings([("a00001", 100, "TEST1", "B738")])
        self._run_v6_migration()
        self.assertIsNone(self._read_longest())

    def test_continuous_session(self):
        """Aircraft sighted 60 times across 20 minutes (poll cadence 20s,
        no gap) → one continuous session of 1200 seconds."""
        sightings = [("a00001", i * 20, "FLT100", "B738") for i in range(61)]
        self._insert_sightings(sightings)
        self._run_v6_migration()
        result = self._read_longest()
        self.assertIsNotNone(result)
        self.assertEqual(result["icao"], "a00001")
        self.assertEqual(result["dur"], 1200)
        self.assertEqual(result["callsign"], "FLT100")

    def test_gap_splits_session(self):
        """Aircraft sighted 0-300s, then nothing, then 1000-1500s.
        Two sessions: 300s and 500s. Best = 500s."""
        first = [("a00001", t, "FLT200", "A320") for t in range(0, 301, 20)]
        second = [("a00001", t, "FLT200", "A320") for t in range(1000, 1501, 20)]
        self._insert_sightings(first + second)
        self._run_v6_migration(gap_min=5)  # 300s gap threshold
        result = self._read_longest()
        self.assertIsNotNone(result)
        self.assertEqual(result["icao"], "a00001")
        self.assertEqual(result["dur"], 500,
                         "Should pick the longer session, not the first")

    def test_gap_at_exactly_threshold_does_not_split(self):
        """Gap = exactly track_gap_minutes (300s) does NOT split — only
        gap > threshold splits. Verifies the `>` not `>=` semantic."""
        first = [("a00001", t, "FLT300", "A320") for t in range(0, 301, 20)]
        # Resume exactly 300s after the last sighting.
        last_first = first[-1][1]
        second = [("a00001", last_first + 300 + i * 20, "FLT300", "A320")
                  for i in range(15)]
        self._insert_sightings(first + second)
        self._run_v6_migration(gap_min=5)
        result = self._read_longest()
        self.assertIsNotNone(result)
        # All sightings collapse into one session.
        self.assertEqual(result["icao"], "a00001")
        self.assertEqual(
            result["dur"],
            second[-1][1] - first[0][1],
            "Gap of exactly 300s should not split the session"
        )

    def test_global_winner_across_aircraft(self):
        """Two aircraft, one with a longer session. Read returns that one."""
        a = [("a00001", t, "SHORT", "C172") for t in range(0, 201, 20)]
        b = [("b00002", t, "LONG", "B777") for t in range(0, 1001, 20)]
        self._insert_sightings(a + b)
        self._run_v6_migration()
        result = self._read_longest()
        self.assertIsNotNone(result)
        self.assertEqual(result["icao"], "b00002")
        self.assertEqual(result["dur"], 1000)

    def test_tilde_hex_filtered_on_read(self):
        """~hex aircraft are written to the rollup but filtered on read."""
        normal = [("a00001", t, "REAL", "A320") for t in range(0, 401, 20)]
        ghost = [("~a00001", t, "GHOST", "A320") for t in range(0, 1001, 20)]
        self._insert_sightings(normal + ghost)
        self._run_v6_migration()
        # Despite ~a00001 having a longer session in the rollup, the
        # read filters it out.
        result = self._read_longest()
        self.assertIsNotNone(result)
        self.assertEqual(result["icao"], "a00001")
        # And verify ~a00001 IS in the rollup (just filtered on read).
        ghost_row = self.conn.execute(
            "SELECT best_session_duration FROM aircraft_track_daily "
            "WHERE icao = ?", ("~a00001",)
        ).fetchone()
        self.assertIsNotNone(ghost_row, "~hex should be in the rollup")
        self.assertEqual(ghost_row[0], 1000)

    def test_callsign_and_type_denormalized(self):
        """Callsign and aircraft_type are stored on the rollup row, so
        the drill-panel doesn't need a follow-up all_sightings lookup."""
        sightings = [("a00001", t, "UAL123", "B738") for t in range(0, 401, 20)]
        self._insert_sightings(sightings)
        self._run_v6_migration()
        result = self._read_longest()
        self.assertIsNotNone(result)
        self.assertEqual(result["callsign"], "UAL123")
        self.assertEqual(result["aircraft_type"], "B738")

    def test_idempotent_rerun(self):
        """Running the migration a second time is a no-op (skip backfill
        when the table is already populated). Verifies the row-count gate."""
        sightings = [("a00001", t, "FLT", "A320") for t in range(0, 401, 20)]
        self._insert_sightings(sightings)
        self._run_v6_migration()
        rows_first = self.conn.execute(
            "SELECT COUNT(*) FROM aircraft_track_daily"
        ).fetchone()[0]
        # Re-run by manually resetting schema_version, since
        # apply_schema_migrations naturally skips already-applied ones.
        # We test the inner gate instead — call _migration_v6 directly.
        from schema_migrations import _migration_v6_aircraft_track_daily
        _migration_v6_aircraft_track_daily(self.conn)  # should skip
        rows_second = self.conn.execute(
            "SELECT COUNT(*) FROM aircraft_track_daily"
        ).fetchone()[0]
        self.assertEqual(rows_first, rows_second,
                         "Re-running migration v6 should not duplicate rows")

    def test_simulated_collector_writes(self):
        """End-to-end: simulate the collector loop's per-poll UPSERT logic
        and verify aircraft_track_daily ends up with the right state.

        This is the "does write-side match read-side" test — if the live
        collector path and the read query disagree on session semantics,
        this test catches it. Mirrors the production code path in
        collector.py at the per-aircraft block."""
        from collector import _local_day_bucket, set_session_track_config
        set_session_track_config("UTC", 5)
        gap_sec = 300

        def upsert(icao, seen_at, callsign="FLT", actype="A320"):
            day = _local_day_bucket(seen_at)
            existing = self.conn.execute(
                "SELECT current_session_start, current_session_last, "
                "       best_session_start, best_session_end, best_session_duration, "
                "       callsign, aircraft_type "
                "FROM aircraft_track_daily WHERE icao=? AND day_bucket=?",
                (icao, day)
            ).fetchone()
            if existing is None or seen_at - existing[1] > gap_sec:
                cur_start, cur_last = seen_at, seen_at
            else:
                cur_start, cur_last = existing[0], seen_at
            cur_dur = cur_last - cur_start
            if existing is None or cur_dur > existing[4]:
                best_s, best_e, best_d = cur_start, cur_last, cur_dur
            else:
                best_s, best_e, best_d = existing[2], existing[3], existing[4]
            self.conn.execute("""
                INSERT OR REPLACE INTO aircraft_track_daily
                (icao, day_bucket, callsign, aircraft_type,
                 current_session_start, current_session_last,
                 best_session_start, best_session_end, best_session_duration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (icao, day, callsign, actype,
                  cur_start, cur_last, best_s, best_e, best_d))

        # Need the table to exist first — run migration.
        self._run_v6_migration()

        # Poll cadence 20s. Aircraft visible from t=0 to t=600,
        # disappears, reappears t=2000 to t=3500. Gap > 300s splits.
        # Expected: best is the second session (1500s).
        for t in range(self.today_midnight, self.today_midnight + 601, 20):
            upsert("c00001", t, "TEST", "B738")
        for t in range(self.today_midnight + 2000,
                       self.today_midnight + 3501, 20):
            upsert("c00001", t, "TEST", "B738")
        self.conn.commit()

        result = self._read_longest()
        self.assertIsNotNone(result)
        self.assertEqual(result["icao"], "c00001")
        self.assertEqual(result["dur"], 1500,
                         "Live UPSERT path should produce same answer "
                         "as the migration backfill walk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
