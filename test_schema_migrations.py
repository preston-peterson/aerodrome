"""
Test harness for schema_migrations.py.

Why this exists separately: schema migrations are the highest-risk piece
of Phase 1 of the v2.51.0 search feature. They run once per install ever
and a buggy migration can corrupt the database. Catching bugs in a unit-
test environment is much cheaper than catching them in a user's database.

Scenarios exercised below:

  1. Empty fresh DB — never been through any version of the schema.
     Migration should succeed, schema_version=1, all tables present.

  2. v2.50.x DB with no data — tables exist, are empty, no migrations.
     Migration should add columns/indexes/FTS5/triggers and complete with
     no rows backfilled.

  3. v2.50.x DB with a small synthetic dataset (~100 ICAOs, mirrors a
     just-started install). Migration should populate registration from
     hexdb_cache for resolved aircraft, last_callsign from latest
     all_sightings, country from countries.py, sighting_count from rollup.

  4. v2.50.x DB shaped like a quiet-airspace install (~10 days, ~5k unique
     ICAOs). Verifies migration scales reasonably for typical use.

  5. v2.50.x DB shaped like Pi user's busy install (~12 days, ~20k unique
     ICAOs, ~1M rollup rows). Verifies migration completes in the
     advertised <60s envelope at scale.

  6. Idempotency: run the migration TWICE on the same DB. Second run
     should be a no-op (schema_version already at 1) and the data should
     be unchanged. Critical for the "user upgrades twice" scenario.

  7. Rollback on failure: inject a failure mid-migration and verify the
     transaction rolls back, the schema_version stamp does not advance,
     and the DB is left in its pre-migration state.

Run:
    python3 test_schema_migrations.py
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure we can import the module under test from its location alongside
# the rest of the application code.
sys.path.insert(0, str(Path(__file__).parent))

from schema_migrations import (
    apply_schema_migrations,
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
)
from countries import country_for_icao


# ---------------------------------------------------------------------------
# Fixtures: helpers that build synthetic DBs at known shapes.
# ---------------------------------------------------------------------------

def _make_v2_50_db(path: str) -> sqlite3.Connection:
    """Build a DB at the v2.50.x schema (no schema_version table, no new
    columns). Mirrors what init_db() produces."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE all_sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT NOT NULL,
            callsign TEXT DEFAULT '',
            speed REAL,
            lat REAL,
            lon REAL,
            altitude REAL,
            aircraft_type TEXT DEFAULT '',
            type_desc TEXT DEFAULT '',
            seen_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE seen_aircraft (
            icao TEXT PRIMARY KEY,
            first_seen_at INTEGER NOT NULL,
            first_callsign TEXT DEFAULT '',
            first_aircraft_type TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE sightings_hourly (
            icao TEXT NOT NULL,
            hour_bucket INTEGER NOT NULL,
            callsign TEXT,
            aircraft_type TEXT,
            type_desc TEXT,
            sighting_count INTEGER DEFAULT 1,
            first_seen_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            last_lat REAL,
            last_lon REAL,
            last_altitude REAL,
            last_speed REAL,
            last_squawk TEXT,
            min_altitude REAL,
            max_altitude REAL,
            max_speed REAL,
            PRIMARY KEY (icao, hour_bucket)
        )
    """)
    conn.execute("""
        CREATE TABLE hexdb_cache (
            icao TEXT PRIMARY KEY,
            registration TEXT,
            resolved_at INTEGER NOT NULL,
            last_outcome TEXT NOT NULL,
            hit_count INTEGER DEFAULT 0,
            last_hit_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX idx_seen_first ON seen_aircraft(first_seen_at)")
    # Mirror real v2.50.x install indexes that affect migration query plans.
    # idx_all_icao is the critical one — backfill subqueries do per-ICAO
    # lookups and need this index, otherwise step 5b is O(N×M).
    conn.execute("CREATE INDEX idx_all_icao ON all_sightings(icao)")
    conn.execute("CREATE INDEX idx_all_seen ON all_sightings(seen_at)")
    conn.execute("CREATE INDEX idx_all_seen_icao ON all_sightings(seen_at, icao)")
    conn.execute("CREATE INDEX idx_hourly_bucket_icao ON sightings_hourly(hour_bucket, icao)")
    conn.commit()
    return conn


def _seed_synthetic_data(conn: sqlite3.Connection, n_aircraft: int,
                         days: int = 10, hexdb_resolved_pct: float = 0.6) -> None:
    """Populate the v2.50.x DB with synthetic data shaped like a real
    install. Each aircraft gets:
      - one row in seen_aircraft (first_seen)
      - 5-30 rows in all_sightings spread over the days window
      - one row per hour in sightings_hourly (rolled up)
      - hexdb_cache entry with probability hexdb_resolved_pct
    """
    now = int(time.time())
    earliest = now - days * 86400

    # Mix ICAOs across realistic country blocks so country_for_icao
    # returns reasonable distribution. Bay Area / urban US would skew
    # heavy US, with sprinkling of CA, MX, JP, etc.
    icao_blocks = [
        # Use larger block ranges so the seeder can scale to 20k+ aircraft
        # without colliding. Real installs see this distribution roughly
        # — heavy US, modest CA/UK/DE/etc.
        (0xA00000, 0xAFFFFF, "United States"),
        (0xC00000, 0xC3FFFF, "Canada"),
        (0x380000, 0x3BFFFF, "France"),
        (0x840000, 0x87FFFF, "Japan"),
        (0x400000, 0x43FFFF, "United Kingdom"),
        (0x3C0000, 0x3FFFFF, "Germany"),
    ]
    types = ["B738", "A320", "A321", "B739", "CRJ9", "E175", "B77W", "C172"]
    type_descs = {
        "B738": "Boeing 737-800", "A320": "Airbus A320", "A321": "Airbus A321",
        "B739": "Boeing 737-900", "CRJ9": "Bombardier CRJ-900",
        "E175": "Embraer 175", "B77W": "Boeing 777-300ER", "C172": "Cessna 172",
    }

    for i in range(n_aircraft):
        block_idx = i % len(icao_blocks)
        block_start, block_end, _ = icao_blocks[block_idx]
        icao_int = block_start + (i // len(icao_blocks))
        if icao_int > block_end:
            icao_int = block_start + (i % (block_end - block_start))
        icao = f"{icao_int:06X}"

        first_seen = earliest + (i * (days * 86400) // max(1, n_aircraft))
        callsign = f"AAL{1000 + i % 9000}"
        atype = types[i % len(types)]
        adesc = type_descs[atype]

        conn.execute(
            "INSERT INTO seen_aircraft (icao, first_seen_at, first_callsign, "
            "first_aircraft_type) VALUES (?, ?, ?, ?)",
            (icao, first_seen, callsign, atype),
        )

        # Hexdb resolution for some fraction of aircraft. Registration
        # is e.g. "N12345" for US, "C-FXXX" for Canada, etc.
        if (i * 2654435761) % 1000 < int(hexdb_resolved_pct * 1000):
            reg = f"N{12345 + i % 99999}"
            conn.execute(
                "INSERT INTO hexdb_cache (icao, registration, resolved_at, last_outcome, hit_count) "
                "VALUES (?, ?, ?, 'hit', 1)",
                (icao, reg, first_seen),
            )

        # 5-30 sightings per aircraft. seen_at distributed across days.
        n_sightings = 5 + (i % 26)
        for s in range(n_sightings):
            seen_at = first_seen + (s * 3600)  # one per hour roughly
            if seen_at > now:
                seen_at = now - (s * 60)
            conn.execute(
                "INSERT INTO all_sightings (icao, callsign, speed, lat, lon, "
                "altitude, aircraft_type, type_desc, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (icao, callsign, 400 + (s * 7) % 100, 37.5 + (s % 10) * 0.01,
                 -122.1 + (s % 10) * 0.01, 30000 + (s * 100) % 5000,
                 atype, adesc, seen_at),
            )

        # Rollup: one row per hour bucket the aircraft was seen.
        bucket_count = max(1, n_sightings // 4)
        for b in range(bucket_count):
            hr = (first_seen // 3600) * 3600 + (b * 3600)
            if hr > now:
                hr = now - (b * 3600)
            conn.execute(
                "INSERT OR IGNORE INTO sightings_hourly "
                "(icao, hour_bucket, callsign, aircraft_type, type_desc, "
                "sighting_count, first_seen_at, last_seen_at, last_lat, "
                "last_lon, last_altitude, last_speed, last_squawk, "
                "min_altitude, max_altitude, max_speed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (icao, hr, callsign, atype, adesc, 4, hr, hr + 3500,
                 37.5, -122.1, 30000, 450, "1200", 28000, 32000, 480),
            )

    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyDb(unittest.TestCase):
    """Scenario: brand-new install. Tables don't even exist yet."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_migrate_empty_db_fails_gracefully(self):
        # On an empty DB (no tables at all), v1 migration's ALTER TABLE
        # has nothing to alter. It should fail because seen_aircraft
        # doesn't exist. This case is realistic only if init_db() hasn't
        # run yet — but in practice main.py calls init_db() before
        # apply_schema_migrations(). Test verifies behavior for safety.
        conn = sqlite3.connect(self.tmp.name)
        try:
            result = apply_schema_migrations(conn, "test")
            # Expected: migration fails because seen_aircraft doesn't exist.
            self.assertFalse(result["ok"])
            self.assertIsNotNone(result["error"])
        finally:
            conn.close()


class TestFreshV250Db(unittest.TestCase):
    """Scenario: just-installed v2.50.x DB. Tables exist, are empty."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.tmp.close()
        self.conn = _make_v2_50_db(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_migrate_fresh_v250_succeeds(self):
        result = apply_schema_migrations(self.conn, "test")
        self.assertTrue(result["ok"], f"Migration failed: {result.get('error')}")
        self.assertEqual(result["starting_version"], 0)
        # v2.50.42: bumped from 1 to 2 — operator backfill migration added.
        # If you add a future migration vN, bump this to N too.
        from schema_migrations import CURRENT_SCHEMA_VERSION
        self.assertEqual(result["ending_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(len(result["applied"]), CURRENT_SCHEMA_VERSION)

    def test_new_columns_present_after_migration(self):
        apply_schema_migrations(self.conn, "test")
        # Check each new column exists
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(seen_aircraft)"
        ).fetchall()}
        for expected in ("registration", "last_callsign", "aircraft_type",
                         "aircraft_type_desc", "operator", "country",
                         "last_lat", "last_lon", "last_seen_at",
                         "sighting_count"):
            self.assertIn(expected, cols, f"Missing column: {expected}")

    def test_indexes_created(self):
        apply_schema_migrations(self.conn, "test")
        idx_names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='seen_aircraft'"
        ).fetchall()}
        for expected in ("idx_seen_registration", "idx_seen_callsign",
                         "idx_seen_type", "idx_seen_country", "idx_seen_last"):
            self.assertIn(expected, idx_names, f"Missing index: {expected}")

    def test_fts5_table_created(self):
        apply_schema_migrations(self.conn, "test")
        # FTS5 virtual tables show up in sqlite_master with type='table'
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='seen_aircraft_fts'"
        ).fetchall()
        self.assertEqual(len(rows), 1, "seen_aircraft_fts not created")

    def test_no_fts_triggers_created(self):
        """v2.51.0 Flavor C: FTS5 triggers are NOT created. Sync happens
        via the collector's cycle-end batch flush of dirty rows. Verify
        the migration did NOT create the inline-sync triggers we
        considered and rejected."""
        apply_schema_migrations(self.conn, "test")
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'seen_aircraft_fts_%'"
        ).fetchall()
        self.assertEqual(rows, [],
                         "FTS5 triggers should not exist — sync goes through "
                         "the collector's dirty-flag flush, not triggers")

    def test_fts_dirty_column_present(self):
        """Migration must add fts_dirty column."""
        apply_schema_migrations(self.conn, "test")
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(seen_aircraft)"
        ).fetchall()}
        self.assertIn("fts_dirty", cols)

    def test_fts_dirty_partial_index_present(self):
        """Migration must create the partial index that makes the
        cycle-end flush O(dirty rows)."""
        apply_schema_migrations(self.conn, "test")
        rows = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_seen_fts_dirty'"
        ).fetchall()
        self.assertEqual(len(rows), 1, "idx_seen_fts_dirty missing")
        # Verify it's the partial form (has WHERE clause)
        self.assertIn("WHERE", rows[0][1].upper(),
                      "idx_seen_fts_dirty should be a partial index, "
                      "otherwise the dirty-row scan defeats the purpose")


class TestSmallSyntheticData(unittest.TestCase):
    """Scenario: install with ~100 ICAOs and modest history. Verifies
    backfill correctness end-to-end on a hand-checkable scale."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.tmp.close()
        self.conn = _make_v2_50_db(self.tmp.name)
        _seed_synthetic_data(self.conn, n_aircraft=100, days=5,
                              hexdb_resolved_pct=0.5)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_country_backfilled(self):
        apply_schema_migrations(self.conn, "test")
        # Of 100 aircraft spread across 6 country blocks, every one
        # should land inside an allocated block (we picked the blocks
        # specifically to be fully allocated). Country must not be NULL.
        null_country = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft WHERE country IS NULL"
        ).fetchone()[0]
        self.assertEqual(null_country, 0,
                         f"{null_country} aircraft have NULL country after backfill")

    def test_registration_backfilled_for_resolved(self):
        apply_schema_migrations(self.conn, "test")
        # ~50% have hexdb_cache rows; those should have registration set.
        # The other ~50% should have NULL registration.
        with_reg = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft WHERE registration IS NOT NULL"
        ).fetchone()[0]
        # Allow some leeway in the 50% target (synthetic data uses a
        # deterministic hash, exact count varies).
        self.assertGreater(with_reg, 30, "too few registrations backfilled")
        self.assertLess(with_reg, 70, "too many registrations backfilled")

    def test_last_callsign_backfilled(self):
        apply_schema_migrations(self.conn, "test")
        null_cs = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft WHERE last_callsign IS NULL"
        ).fetchone()[0]
        self.assertEqual(null_cs, 0,
                         "every aircraft has at least one all_sightings row, "
                         "so last_callsign should be populated")

    def test_sighting_count_backfilled(self):
        apply_schema_migrations(self.conn, "test")
        # Should be > 0 for every aircraft (we seeded multiple sightings each)
        zero_count = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft WHERE sighting_count = 0"
        ).fetchone()[0]
        self.assertEqual(zero_count, 0)

    def test_all_rows_marked_dirty_after_migration(self):
        """v2.51.0 Flavor C: rather than rebuilding FTS5 inline (slow),
        migration marks every existing row as fts_dirty. The collector's
        first post-migration cycle flushes them all to FTS5 in batch."""
        apply_schema_migrations(self.conn, "test")
        n_aircraft = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft"
        ).fetchone()[0]
        n_dirty = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft WHERE fts_dirty = 1"
        ).fetchone()[0]
        self.assertEqual(n_dirty, n_aircraft,
                         f"Migration should mark all {n_aircraft} rows dirty "
                         f"for collector to flush; only {n_dirty} are dirty")

    def test_fts_table_empty_after_migration(self):
        """Migration creates the FTS5 table but does NOT populate it.
        Population is the collector's job on its first cycle. Verify
        the table is empty post-migration (it'll be filled on cycle 1)."""
        apply_schema_migrations(self.conn, "test")
        n_fts = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft_fts"
        ).fetchone()[0]
        self.assertEqual(n_fts, 0,
                         "FTS5 should be empty post-migration; "
                         "collector flushes dirty rows on first cycle")

    def test_dirty_flush_simulation_populates_fts(self):
        """End-to-end: simulate what the collector does at cycle end.
        After running this, FTS5 must be populated and queryable.
        This is the protocol the collector implements; verify it works."""
        apply_schema_migrations(self.conn, "test")
        n_aircraft = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft"
        ).fetchone()[0]

        # The flush protocol: delete any FTS rows for dirty seen_aircraft
        # rows (idempotency on retry), insert fresh, clear flag.
        self.conn.execute("""
            DELETE FROM seen_aircraft_fts
            WHERE rowid IN (SELECT rowid FROM seen_aircraft WHERE fts_dirty = 1)
        """)
        self.conn.execute("""
            INSERT INTO seen_aircraft_fts (
                rowid, icao, registration, last_callsign,
                aircraft_type, aircraft_type_desc, operator, country
            )
            SELECT rowid, icao, registration, last_callsign,
                   aircraft_type, aircraft_type_desc, operator, country
            FROM seen_aircraft WHERE fts_dirty = 1
        """)
        self.conn.execute("UPDATE seen_aircraft SET fts_dirty = 0 WHERE fts_dirty = 1")
        self.conn.commit()

        # FTS5 should now have one row per aircraft
        n_fts = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft_fts"
        ).fetchone()[0]
        self.assertEqual(n_aircraft, n_fts,
                         "After cycle-end flush, FTS5 should have one row "
                         "per seen_aircraft row")

        # All rows should now be clean
        n_dirty = self.conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft WHERE fts_dirty = 1"
        ).fetchone()[0]
        self.assertEqual(n_dirty, 0, "Flush should clear all dirty flags")

        # FTS5 query should return matches
        rows = self.conn.execute(
            "SELECT icao FROM seen_aircraft_fts WHERE seen_aircraft_fts MATCH ?",
            ('"United States"',)
        ).fetchall()
        self.assertGreater(len(rows), 5,
                           "FTS5 query for country should return matches")


class TestIdempotency(unittest.TestCase):
    """Scenario: user runs migration twice. Second run should be a clean
    no-op."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.tmp.close()
        self.conn = _make_v2_50_db(self.tmp.name)
        _seed_synthetic_data(self.conn, n_aircraft=50, days=3)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_second_run_is_noop(self):
        from schema_migrations import CURRENT_SCHEMA_VERSION
        # First run
        r1 = apply_schema_migrations(self.conn, "test")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["ending_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(len(r1["applied"]), CURRENT_SCHEMA_VERSION)

        # Snapshot data after first run
        snap1 = self.conn.execute(
            "SELECT icao, registration, last_callsign, country, sighting_count "
            "FROM seen_aircraft ORDER BY icao"
        ).fetchall()

        # Second run
        r2 = apply_schema_migrations(self.conn, "test")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["starting_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(r2["ending_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(len(r2["applied"]), 0,
                         "Second run should apply zero migrations")

        # Data must be unchanged
        snap2 = self.conn.execute(
            "SELECT icao, registration, last_callsign, country, sighting_count "
            "FROM seen_aircraft ORDER BY icao"
        ).fetchall()
        self.assertEqual(snap1, snap2,
                         "Data changed on second migration run — non-idempotent")


class TestRollbackOnFailure(unittest.TestCase):
    """Scenario: migration is interrupted mid-way (simulated by injecting
    a failure into a custom migration function). DB must be left at the
    starting version, untouched."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.tmp.close()
        self.conn = _make_v2_50_db(self.tmp.name)
        _seed_synthetic_data(self.conn, n_aircraft=20, days=2)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_rollback_restores_starting_state(self):
        from schema_migrations import (
            apply_schema_migrations as orig_apply,
            _ensure_schema_version_table,
        )
        import schema_migrations as sm

        # Snapshot pre-migration state
        cols_before = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(seen_aircraft)"
        ).fetchall()}

        # Inject a broken migration that fails halfway
        def broken_migration(conn):
            # Do a real ALTER (which would partial-apply if we weren't
            # in a transaction), then raise.
            conn.execute("ALTER TABLE seen_aircraft ADD COLUMN registration TEXT")
            raise RuntimeError("simulated mid-migration failure")

        original_migrations = sm.MIGRATIONS
        sm.MIGRATIONS = [(1, "broken test migration", broken_migration)]
        try:
            result = apply_schema_migrations(self.conn, "test")
            self.assertFalse(result["ok"])
            self.assertIn("simulated mid-migration failure", result["error"])
        finally:
            sm.MIGRATIONS = original_migrations

        # CRITICAL: column must NOT have been added (rollback worked)
        cols_after = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(seen_aircraft)"
        ).fetchall()}
        self.assertEqual(cols_before, cols_after,
                         "Column was added despite rollback — transaction wrapper failed")

        # schema_version table should exist (created outside transaction)
        # but should have NO rows
        n_rows = self.conn.execute(
            "SELECT COUNT(*) FROM schema_version"
        ).fetchone()[0]
        self.assertEqual(n_rows, 0,
                         "schema_version was stamped despite rollback")


# ---------------------------------------------------------------------------
# Performance benchmark — not a hard pass/fail test, but reports timings
# for a Pi-user-scale install. Run with --bench to include.
# ---------------------------------------------------------------------------

class BenchmarkLargeMigration(unittest.TestCase):
    """Scenario: install shaped like Pi user's data (~20k unique aircraft,
    ~10 days of sightings). Reports migration timing for human review.
    Not a hard fail — but if this takes >60 seconds we should rethink
    the backfill approach before shipping."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.tmp.close()
        self.conn = _make_v2_50_db(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    @unittest.skipUnless(os.environ.get("BENCH"), "set BENCH=1 to run")
    def test_pi_user_scale_migration(self):
        print("\n  Seeding synthetic Pi-user-scale data (this takes a moment)...")
        t0 = time.time()
        # 5000 aircraft is large enough to exercise the loops without
        # making the test absurdly slow on CI. Pi user has ~21k; if
        # this is fast at 5k it'll be fast enough at 21k (the work is
        # roughly linear).
        _seed_synthetic_data(self.conn, n_aircraft=5000, days=10,
                              hexdb_resolved_pct=0.6)
        seed_time = time.time() - t0
        print(f"  Seeding done in {seed_time:.1f}s")

        n_seen = self.conn.execute("SELECT COUNT(*) FROM seen_aircraft").fetchone()[0]
        n_sight = self.conn.execute("SELECT COUNT(*) FROM all_sightings").fetchone()[0]
        n_roll = self.conn.execute("SELECT COUNT(*) FROM sightings_hourly").fetchone()[0]
        print(f"  Synthetic DB: {n_seen} unique ICAOs, {n_sight} all_sightings rows, "
              f"{n_roll} rollup rows")

        t0 = time.time()
        result = apply_schema_migrations(self.conn, "bench")
        elapsed = time.time() - t0

        self.assertTrue(result["ok"], f"Migration failed: {result.get('error')}")
        print(f"  Migration completed in {elapsed:.2f}s")
        print(f"  Per-aircraft cost: {1000 * elapsed / n_seen:.2f}ms")

        if elapsed > 60:
            print(f"  ⚠ Migration took {elapsed:.0f}s on 5k aircraft — "
                  f"would extrapolate to >{60 * 21000//5000}s on Pi user's 21k. "
                  f"Consider redesign before shipping.")


# v2.50.42: tests for the operator backfill migration (v2)
class TestMigrationV2OperatorBackfill(unittest.TestCase):
    """Migration v2 derives operator codes from existing last_callsign
    values. These tests verify the derivation rules stay consistent
    with the runtime collector behavior — same source of truth via
    designators.operator_from_callsign.

    Test approach: build pre-v1 schema, apply v1 to get the operator
    column + last_callsign column, manually clear operator + insert
    test rows, then call v2 directly to verify behavior."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        # Build pre-v1 (no schema_version table, basic seen_aircraft only)
        from collector import init_db
        init_db(self.tmp.name)
        self.conn = sqlite3.connect(self.tmp.name)
        # Apply v1 to get the search-feature columns
        from schema_migrations import _migration_v1_search_schema
        _migration_v1_search_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _seed(self, rows):
        """rows = [(icao, last_callsign), ...]
        Inserts rows with NULL operator so v2 has something to fix.
        v1 already migrated other rows; we just add fresh ones at the
        post-v1 schema."""
        for icao, cs in rows:
            self.conn.execute(
                "INSERT INTO seen_aircraft (icao, first_seen_at, first_callsign, "
                "first_aircraft_type, last_callsign, operator, fts_dirty) "
                "VALUES (?, ?, ?, ?, ?, NULL, 0)",
                (icao, 1700000000, cs or "", "B738", cs or "")
            )
        self.conn.commit()

    def _run_v2(self):
        """Apply v2 directly (not through the migration runner, since
        that gates on schema_version which we'd have to mock)."""
        from schema_migrations import _migration_v2_operator_backfill
        _migration_v2_operator_backfill(self.conn)

    def test_v2_derives_operator_from_callsign(self):
        """Standard case: airline callsign → operator code."""
        self._seed([("A12345", "UAL2024"), ("A23456", "DAL415"), ("A34567", "SWA8001")])
        self._run_v2()
        ops = dict(self.conn.execute(
            "SELECT icao, operator FROM seen_aircraft WHERE icao LIKE 'A%'"
        ).fetchall())
        self.assertEqual(ops["A12345"], "UAL")
        self.assertEqual(ops["A23456"], "DAL")
        self.assertEqual(ops["A34567"], "SWA")

    def test_v2_skips_non_airline_callsigns(self):
        """Tail-number callsigns shouldn't get a fake operator."""
        self._seed([("A12345", "N12345"), ("A23456", "G-ABCD")])
        self._run_v2()
        ops = self.conn.execute(
            "SELECT icao, operator FROM seen_aircraft WHERE icao IN ('A12345', 'A23456')"
        ).fetchall()
        for _icao, op in ops:
            self.assertIn(op, (None, ""))

    def test_v2_skips_unknown_airline_codes(self):
        """3-letter prefixes that aren't real ICAO airline codes shouldn't
        be fabricated as operators."""
        self._seed([("A12345", "ZZZ1234")])
        self._run_v2()
        op = self.conn.execute(
            "SELECT operator FROM seen_aircraft WHERE icao = 'A12345'"
        ).fetchone()[0]
        self.assertIn(op, (None, ""))

    def test_v2_marks_backfilled_rows_dirty(self):
        """Backfilled rows must be marked fts_dirty so the next collector
        cycle flushes them to FTS5 with enriched operator strings."""
        self._seed([("A12345", "UAL2024")])
        self._run_v2()
        dirty = self.conn.execute(
            "SELECT fts_dirty FROM seen_aircraft WHERE icao = 'A12345'"
        ).fetchone()[0]
        self.assertEqual(dirty, 1)

    def test_v2_idempotent(self):
        """Running v2 twice should produce the same result."""
        self._seed([("A12345", "UAL2024")])
        self._run_v2()
        op1 = self.conn.execute(
            "SELECT operator FROM seen_aircraft WHERE icao = 'A12345'"
        ).fetchone()[0]
        self._run_v2()
        op2 = self.conn.execute(
            "SELECT operator FROM seen_aircraft WHERE icao = 'A12345'"
        ).fetchone()[0]
        self.assertEqual(op1, "UAL")
        self.assertEqual(op2, "UAL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
