"""
End-to-end tests for migration v7 and the sticky-military semantic.

Three layers:
  1. Migration v7 backfill against synthetic seen_aircraft + military_sightings.
     Verifies the column gets populated with the right category for each
     row, including transitive military membership via military_sightings.
  2. Sticky-military rule in the collector UPSERT. Simulates two polls —
     first with is_military=True, second with is_military=False — and
     verifies category stays 'military'. This is the bug class where a
     feeder flicker (dbFlags missing on one poll) shouldn't downgrade a
     known military aircraft.
  3. Stats category_mix card and drill panel reading from the column.
     Verifies the read-side queries return the same shape as before.

Run:
    python3 test_migration_v7.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


class TestMigrationV7Backfill(unittest.TestCase):
    """Layer 1: migration v7 backfills category correctly for every row.

    Setup applies all migrations (including v7, which creates the column),
    then NULLs out category and inserts test rows. The v7 backfill is
    re-invoked directly to test the backfill logic against known inputs."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        from collector import init_db
        init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Apply all migrations so the seen_aircraft schema is current
        # (includes the v1 denormalization columns and the v7 category
        # column). v7 will run its own backfill on an empty table — no-op.
        from schema_migrations import (
            apply_schema_migrations, set_v6_backfill_config
        )
        set_v6_backfill_config("UTC", 5)
        result = apply_schema_migrations(self.conn, "test-2.89.0")
        self.assertTrue(result["ok"],
                        f"Migration setup failed: {result.get('error')}")

    def tearDown(self):
        self.conn.close()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def _seen(self, icao, atype, adesc="", last_seen_at=1000):
        """Insert a seen_aircraft row with category=NULL, simulating the
        pre-backfill state. The v7 backfill targets exactly these rows."""
        self.conn.execute("""
            INSERT INTO seen_aircraft
            (icao, first_seen_at, first_callsign, first_aircraft_type,
             last_callsign, aircraft_type, aircraft_type_desc, operator, country,
             last_lat, last_lon, last_distance, last_seen_at, sighting_count,
             fts_dirty, category)
            VALUES (?, ?, '', ?, '', ?, ?, '', '', NULL, NULL, NULL, ?, 1, 0, NULL)
        """, (icao, last_seen_at, atype, atype, adesc, last_seen_at))

    def _mil_sighting(self, icao):
        """Insert a military_sightings row. Establishes transitive
        military membership for migration v7's backfill."""
        self.conn.execute("""
            INSERT INTO military_sightings
            (icao, callsign, speed, lat, lon, altitude, aircraft_type, type_desc,
             seen_at, special_label, squawk)
            VALUES (?, '', NULL, NULL, NULL, NULL, '', '', 1000, '', '')
        """, (icao,))

    def _run_backfill(self):
        """Re-invoke v7's backfill directly (bypasses the schema_version
        gate so we can test it on rows we just inserted)."""
        from schema_migrations import _migration_v7_category_column
        _migration_v7_category_column(self.conn)

    def _category_of(self, icao):
        row = self.conn.execute(
            "SELECT category FROM seen_aircraft WHERE icao = ?", (icao,)
        ).fetchone()
        return row["category"] if row else None

    def test_commercial_aircraft(self):
        self._seen("aaa001", "B738", "Boeing 737-800")
        self.conn.commit()
        self._run_backfill()
        self.assertEqual(self._category_of("aaa001"), "commercial")

    def test_general_aviation_aircraft(self):
        self._seen("aaa002", "C172", "Cessna 172")
        self.conn.commit()
        self._run_backfill()
        self.assertEqual(self._category_of("aaa002"), "general_aviation")

    def test_helicopter_aircraft_by_type(self):
        self._seen("aaa003", "H60", "Black Hawk")
        self.conn.commit()
        self._run_backfill()
        # No military_sightings row → not military → falls through to helicopter.
        self.assertEqual(self._category_of("aaa003"), "helicopter")

    def test_helicopter_aircraft_by_description(self):
        """Type code unrecognized but description mentions helicopter."""
        self._seen("aaa004", "XYZ", "Custom rotorcraft helicopter prototype")
        self.conn.commit()
        self._run_backfill()
        self.assertEqual(self._category_of("aaa004"), "helicopter")

    def test_unknown_aircraft(self):
        self._seen("aaa005", "", "")
        self.conn.commit()
        self._run_backfill()
        self.assertEqual(self._category_of("aaa005"), "unknown")

    def test_military_via_transitive_membership(self):
        """An aircraft with a military_sightings row classifies as military
        EVEN IF its type code would otherwise be commercial. Sticky-military
        applied transitively at backfill time."""
        self._seen("aaa006", "B738", "Boeing 737-800")  # commercial type
        self._mil_sighting("aaa006")                     # but flagged military
        self.conn.commit()
        self._run_backfill()
        self.assertEqual(
            self._category_of("aaa006"),
            "military",
            "Transitive military membership should win over type-based classification"
        )

    def test_military_helicopter_via_transitive_membership(self):
        """A Black Hawk with military_sightings should be 'military', not
        'helicopter' — military precedence is highest in classify()."""
        self._seen("aaa007", "H60", "Black Hawk")
        self._mil_sighting("aaa007")
        self.conn.commit()
        self._run_backfill()
        self.assertEqual(self._category_of("aaa007"), "military")

    def test_idempotent_rerun(self):
        """Running migrations again is a no-op for v7 — the NULL-count
        gate should skip the backfill on re-run."""
        self._seen("aaa008", "C172", "Cessna 172")
        self.conn.commit()
        self._run_backfill()
        cat_first = self._category_of("aaa008")
        # Manually re-run v7 directly (bypasses schema_version check)
        from schema_migrations import _migration_v7_category_column
        _migration_v7_category_column(self.conn)
        cat_second = self._category_of("aaa008")
        self.assertEqual(cat_first, cat_second)


class TestStickyMilitary(unittest.TestCase):
    """Layer 2: the collector UPSERT's sticky-military CASE preserves
    'military' across polls where is_military() returns False later."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        from collector import init_db
        init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Run all migrations so the category column exists.
        from schema_migrations import (
            apply_schema_migrations, set_v6_backfill_config
        )
        set_v6_backfill_config("UTC", 5)
        apply_schema_migrations(self.conn, "test-2.89.0")

    def tearDown(self):
        self.conn.close()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def _upsert(self, icao, atype, adesc, category):
        """Mimic the seen_aircraft UPSERT in collector.py — the CASE
        expression for sticky-military lives in the SQL, so we need to
        run the actual UPSERT statement to test it. Streamlined-down
        version of the production INSERT for test brevity."""
        self.conn.execute("""
            INSERT INTO seen_aircraft (
                icao, first_seen_at, first_callsign, first_aircraft_type,
                last_callsign, aircraft_type, aircraft_type_desc, operator, country,
                last_lat, last_lon, last_distance, last_seen_at, sighting_count,
                fts_dirty, category
            ) VALUES (?, 1000, '', ?, '', ?, ?, '', '', NULL, NULL, NULL, 1000, 1, 1, ?)
            ON CONFLICT(icao) DO UPDATE SET
                aircraft_type = COALESCE(NULLIF(excluded.aircraft_type, ''), aircraft_type),
                aircraft_type_desc = COALESCE(NULLIF(excluded.aircraft_type_desc, ''), aircraft_type_desc),
                last_seen_at = excluded.last_seen_at,
                sighting_count = sighting_count + 1,
                category = CASE
                    WHEN excluded.category = 'military' THEN 'military'
                    WHEN seen_aircraft.category = 'military' THEN 'military'
                    ELSE excluded.category
                END
        """, (icao, atype, atype, adesc, category))

    def _cat(self, icao):
        row = self.conn.execute(
            "SELECT category FROM seen_aircraft WHERE icao = ?", (icao,)
        ).fetchone()
        return row["category"] if row else None

    def test_first_poll_military_sticks_when_second_says_commercial(self):
        """The classic feeder flicker: poll 1 has dbFlags set (military),
        poll 2 doesn't (commercial classification). Sticky should keep
        the row at 'military'."""
        self._upsert("bbb001", "B738", "Boeing 737-800", "military")
        self.assertEqual(self._cat("bbb001"), "military")
        # Second poll — feeder flicker, is_military returned False this time.
        self._upsert("bbb001", "B738", "Boeing 737-800", "commercial")
        self.assertEqual(self._cat("bbb001"), "military",
                         "Sticky rule: military classification must persist")

    def test_first_poll_commercial_can_upgrade_to_military(self):
        """Reverse direction: a commercial aircraft later identified as
        military upgrades to military (the new poll's military
        classification wins)."""
        self._upsert("bbb002", "B738", "Boeing 737-800", "commercial")
        self.assertEqual(self._cat("bbb002"), "commercial")
        self._upsert("bbb002", "B738", "Boeing 737-800", "military")
        self.assertEqual(self._cat("bbb002"), "military")

    def test_non_military_classifications_can_change(self):
        """Sticky rule only applies to military. A commercial → GA flip
        (which would happen if aircraft_type changed) should be allowed."""
        self._upsert("bbb003", "B738", "", "commercial")
        self.assertEqual(self._cat("bbb003"), "commercial")
        self._upsert("bbb003", "C172", "", "general_aviation")
        self.assertEqual(self._cat("bbb003"), "general_aviation")

    def test_unknown_then_known_classification(self):
        """A first poll with no type info (unknown) followed by a poll
        with type info should adopt the new classification — there's no
        sticky rule for unknown."""
        self._upsert("bbb004", "", "", "unknown")
        self.assertEqual(self._cat("bbb004"), "unknown")
        self._upsert("bbb004", "B738", "Boeing 737-800", "commercial")
        self.assertEqual(self._cat("bbb004"), "commercial")


class TestCategoryMixRead(unittest.TestCase):
    """Layer 3: the rewritten category_mix card and drill panel queries
    return the right shape from the new column."""

    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        from collector import init_db
        init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        from schema_migrations import (
            apply_schema_migrations, set_v6_backfill_config
        )
        set_v6_backfill_config("UTC", 5)
        apply_schema_migrations(self.conn, "test-2.89.0")

    def tearDown(self):
        self.conn.close()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def _seed(self, icao, atype, category, last_seen_at=1000):
        self.conn.execute("""
            INSERT INTO seen_aircraft (
                icao, first_seen_at, first_callsign, first_aircraft_type,
                last_callsign, aircraft_type, aircraft_type_desc, operator, country,
                last_lat, last_lon, last_distance, last_seen_at, sighting_count,
                fts_dirty, category
            ) VALUES (?, ?, '', ?, '', ?, '', '', '', NULL, NULL, NULL, ?, 1, 0, ?)
        """, (icao, last_seen_at, atype, atype, last_seen_at, category))

    def test_card_query_groups_correctly(self):
        """The new GROUP BY query should return one row per category
        with correct counts."""
        self._seed("c0001", "B738", "commercial")
        self._seed("c0002", "B738", "commercial")
        self._seed("c0003", "C172", "general_aviation")
        self._seed("c0004", "H60", "military")
        self.conn.commit()
        rows = self.conn.execute("""
            SELECT category, COUNT(*) AS n
            FROM seen_aircraft
            WHERE last_seen_at >= 0 AND category IS NOT NULL
            GROUP BY category
        """).fetchall()
        counts = {r["category"]: r["n"] for r in rows}
        self.assertEqual(counts.get("commercial"), 2)
        self.assertEqual(counts.get("general_aviation"), 1)
        self.assertEqual(counts.get("military"), 1)

    def test_drill_query_filters_by_token(self):
        """The drill query (WHERE category = ?) should return all rows
        with that category in the time window."""
        self._seed("d0001", "B738", "commercial")
        self._seed("d0002", "A320", "commercial")
        self._seed("d0003", "C172", "general_aviation")
        self.conn.commit()
        rows = self.conn.execute("""
            SELECT icao FROM seen_aircraft
            WHERE category = ? AND last_seen_at >= 0
            ORDER BY first_seen_at ASC
        """, ("commercial",)).fetchall()
        icaos = [r["icao"] for r in rows]
        self.assertEqual(icaos, ["d0001", "d0002"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
