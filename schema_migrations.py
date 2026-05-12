"""
Schema migration framework for Aerodrome.

Up to v2.50.x, schema changes were handled inline via CREATE TABLE IF NOT
EXISTS / ALTER TABLE statements scattered through init_db(). That worked
when the schema was small and changes were additive. As of v2.51.0 — the
search-feature release — we need a more disciplined approach: idempotent
migrations, atomic application, version-stamped state, and a rollback
guarantee on partial failure.

This module owns that discipline. It exposes one public function:

    apply_schema_migrations(conn, current_app_version) -> MigrationResult

The function:
  1. Reads the schema_version stamp (creates the table if absent).
  2. Determines which migrations need to run (by version comparison).
  3. Runs each in a single transaction. If any step fails, the transaction
     rolls back, the schema_version stamp does NOT advance, and the DB is
     left in the same state it was when the function was called.
  4. Returns a MigrationResult dict with details for logging / display.

Migrations are append-only — once a migration is shipped, it never gets
edited. A bug in a previous migration is fixed by adding a NEW migration
that corrects it. This is the same discipline as Alembic, Rails
ActiveRecord, etc., and it's what lets a migration on a fresh DB produce
the same result as a migration on a long-running DB.

Why this matters: the project's "single SQLite file is the entire
backup" property is preserved only if migrations don't break older DBs.
A failed migration that leaves a DB half-migrated is much worse than a
clean DB at the old version. The transaction wrapper is non-negotiable.
"""
import logging
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Current schema version. Bump whenever a new migration is added.
# v2.51.0 introduces schema version 1 (the search-feature schema).
# Any DB without a schema_version table is implicitly at version 0.
CURRENT_SCHEMA_VERSION = 9


# A migration is a (target_version, description, callable) tuple.
# The callable takes a sqlite3 connection (already inside a transaction)
# and runs all the DDL/DML for that version. Must be idempotent at the
# DDL level (CREATE TABLE IF NOT EXISTS / IF NOT EXISTS on indexes) so
# that a partial application followed by a retry succeeds.
Migration = Tuple[int, str, Callable[[sqlite3.Connection], None]]


def _ensure_schema_version_table(conn: sqlite3.Connection) -> int:
    """Create the schema_version table if it doesn't exist, and return
    the current version. A DB that's never been through this function
    is at version 0."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL,
            description TEXT NOT NULL,
            app_version TEXT NOT NULL
        )
    """)
    row = conn.execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()
    return int(row[0] or 0)


def _apply_migration(conn: sqlite3.Connection, mig: Migration,
                     app_version: str) -> None:
    """Apply a single migration inside a transaction. The caller has
    already opened the transaction; this function only adds the
    migration's DDL/DML and the version-stamp INSERT."""
    target_version, description, fn = mig
    logger.info(f"Applying schema migration v{target_version}: {description}")
    fn(conn)
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description, app_version) "
        "VALUES (?, ?, ?, ?)",
        (target_version, int(time.time()), description, app_version),
    )


def apply_schema_migrations(conn: sqlite3.Connection,
                            app_version: str) -> Dict[str, Any]:
    """Apply all pending schema migrations.

    Returns a dict with:
        ok:                 True if all migrations succeeded (or none needed)
        starting_version:   schema version before this call
        ending_version:     schema version after this call
        applied:            list of dicts {version, description, duration_sec}
        error:              str if ok=False, else None

    On failure, the transaction is rolled back — the DB is left at
    starting_version. Callers should treat ok=False as a fatal startup
    error: continuing to run against a partially-migrated DB risks
    data corruption.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "starting_version": None,
        "ending_version": None,
        "applied": [],
        "error": None,
    }

    try:
        starting = _ensure_schema_version_table(conn)
        result["starting_version"] = starting

        pending = [m for m in MIGRATIONS if m[0] > starting]
        pending.sort(key=lambda m: m[0])

        if not pending:
            result["ok"] = True
            result["ending_version"] = starting
            return result

        logger.info(
            f"Schema at v{starting}, target v{CURRENT_SCHEMA_VERSION}; "
            f"{len(pending)} migration(s) pending"
        )

        # Single outer transaction wrapping all pending migrations. If
        # ANY migration fails, ALL pending migrations roll back as a
        # group. This is intentional: we either ship the whole upgrade
        # or none of it, never a partial state where some tables exist
        # at v2 schema and others don't.
        conn.execute("BEGIN")
        try:
            for mig in pending:
                t0 = time.time()
                _apply_migration(conn, mig, app_version)
                duration = time.time() - t0
                result["applied"].append({
                    "version": mig[0],
                    "description": mig[1],
                    "duration_sec": round(duration, 2),
                })
                logger.info(
                    f"  v{mig[0]} applied in {duration:.2f}s — {mig[1]}"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        result["ok"] = True
        result["ending_version"] = CURRENT_SCHEMA_VERSION
        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Schema migration FAILED: {e}", exc_info=True)
        return result


# =============================================================================
# Migration v1: search-feature schema (v2.51.0)
# =============================================================================
# Adds the columns, indexes, and FTS5 virtual table required for the
# search feature. Backfills derivable data from existing all_sightings
# and hexdb_cache rows.
#
# v2.51.0-Phase1 design note: we use the *dirty-flag* pattern (Flavor C)
# rather than inline FTS5 triggers. Each row in seen_aircraft has an
# fts_dirty flag; the collector sets it to 1 in the UPSERT only when
# an FTS-indexed field actually changes; the collector's end-of-cycle
# handler flushes dirty rows to FTS5 in a single batch. This is
# meaningfully faster than per-write triggers (~13× over baseline vs
# ~24-32× for inline triggers) because in steady state most sightings
# don't change FTS-indexed fields — they only bump sighting_count and
# last_seen_at.
#
# Tradeoff: search index lags by one collector cycle (60s default).
# Acceptable for an archive search feature where data is by definition
# historical. The 60s lag means an aircraft's callsign change wouldn't
# be searchable for up to a minute — fine.
#
# This is the first formal migration. The pattern established here is what
# every subsequent migration should follow:
#   1. ALTER TABLE adds at the top (idempotent via try-around-each — sqlite
#      doesn't support ADD COLUMN IF NOT EXISTS in older versions we still
#      target, so we catch the OperationalError that means "already exists").
#   2. CREATE INDEX IF NOT EXISTS for new indexes.
#   3. CREATE VIRTUAL TABLE for FTS5 (also idempotent with IF NOT EXISTS).
#   4. Backfill: derive new column values from existing data. Idempotent —
#      uses UPDATE WHERE column IS NULL so re-running won't clobber data
#      the collector has already populated.
#   5. Mark all rows as fts_dirty so the collector's first cycle after
#      migration flushes them all to FTS5. This avoids a multi-second
#      blocking FTS rebuild during migration on installs with many rows.

def _migration_v1_search_schema(conn: sqlite3.Connection) -> None:
    # --- Step 1: ALTER TABLE adds for new seen_aircraft columns ---
    # SQLite's ALTER TABLE ADD COLUMN doesn't have an IF NOT EXISTS
    # syntax in versions we still target (some Pi OS releases ship
    # SQLite 3.34 which lacks it). We wrap each in a try/except that
    # tolerates the "duplicate column name" error, making the
    # operation effectively idempotent.
    columns_to_add = [
        ("registration", "TEXT"),
        ("last_callsign", "TEXT"),
        ("aircraft_type", "TEXT"),  # current 'first_aircraft_type' is preserved as-is
        ("aircraft_type_desc", "TEXT"),
        ("operator", "TEXT"),
        ("country", "TEXT"),
        ("last_lat", "REAL"),
        ("last_lon", "REAL"),
        ("last_seen_at", "INTEGER"),
        ("sighting_count", "INTEGER NOT NULL DEFAULT 0"),
        # v2.51.0 Flavor C: dirty flag set by the collector's UPSERT
        # whenever an FTS-indexed field changes. Cycle-end batch handler
        # flushes dirty rows to FTS5 then clears the flag. Default 0 on
        # existing rows; the migration sets it to 1 for all rows so the
        # first post-migration cycle does the initial FTS population.
        ("fts_dirty", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, typ in columns_to_add:
        try:
            conn.execute(f"ALTER TABLE seen_aircraft ADD COLUMN {col} {typ}")
            logger.debug(f"  added column seen_aircraft.{col}")
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column name" in msg:
                # Column already exists from a previous (possibly partial)
                # migration attempt. Safe to skip.
                logger.debug(f"  column seen_aircraft.{col} already exists, skipping")
                continue
            raise

    # --- Step 2: indexes for the search query patterns ---
    indexes = [
        ("idx_seen_registration", "registration"),
        ("idx_seen_callsign",     "last_callsign"),
        ("idx_seen_type",         "aircraft_type"),
        ("idx_seen_country",      "country"),
        ("idx_seen_last",         "last_seen_at"),
    ]
    for idx_name, col in indexes:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON seen_aircraft({col})"
        )

    # v2.51.0 Flavor C: partial index on fts_dirty makes the cycle-end
    # batch flush O(dirty rows) rather than O(table). Without this,
    # `WHERE fts_dirty = 1` would scan the whole table. The partial
    # condition (`WHERE fts_dirty = 1`) means the index only contains
    # entries for rows that need flushing — typically a tiny fraction
    # of total rows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seen_fts_dirty "
        "ON seen_aircraft(fts_dirty) WHERE fts_dirty = 1"
    )

    # --- Step 3: FTS5 virtual table for free-text fields ---
    # Use the contentless-table form (no `content=` clause) so we manage
    # the FTS rows explicitly via triggers + the rebuild step below.
    # The contentful form (`content='seen_aircraft'`) saves storage but
    # complicates the trigger pattern; we prefer the simpler explicit
    # version for v1, can optimize later if storage becomes a concern.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS seen_aircraft_fts USING fts5(
            icao,
            registration,
            last_callsign,
            aircraft_type,
            aircraft_type_desc,
            operator,
            country,
            tokenize='unicode61 remove_diacritics 1'
        )
    """)

    # --- Step 4: NO FTS5 triggers (Flavor C) ---
    # Inline triggers on seen_aircraft would fire on every collector
    # write and re-tokenize FTS5 entries; benchmarks showed this caused
    # a ~24-32× regression in collector cycle latency. We instead use
    # the dirty-flag pattern: the collector's UPSERT sets fts_dirty=1
    # only when an FTS-indexed field changes, and a cycle-end batch
    # handler flushes dirty rows to FTS5 in one pass.
    #
    # If you're considering re-introducing triggers here, run the
    # benchmark in test_schema_migrations.py first — the win from
    # batching is real and was measured against three competing designs.

    # --- Step 5: backfill new columns from existing data ---
    # Idempotency: every UPDATE has `WHERE col IS NULL` so the migration
    # can be re-run safely (or partial-run + retried) without clobbering
    # data the collector has already written.
    #
    # 5a. registration from hexdb_cache (LEFT JOIN — many rows won't
    # have a hexdb resolution).

    # v2.51.1: progress logging. On Pi-class hardware with ~21k aircraft,
    # the backfill below can take 30-60 seconds. Without these logs the
    # journal goes silent during the heavy stretch and a user reasonably
    # wonders if the service hung. Logging the row count up-front gives
    # them a sense of scale; each step's "done" log marks progress.
    n_seen = conn.execute("SELECT COUNT(*) FROM seen_aircraft").fetchone()[0]
    logger.info(f"  Migration v1: backfilling {n_seen} aircraft rows "
                f"(may take 30-60s on slower hardware)…")

    conn.execute("""
        UPDATE seen_aircraft
        SET registration = (
            SELECT h.registration FROM hexdb_cache h
            WHERE h.icao = seen_aircraft.icao
              AND h.registration IS NOT NULL AND h.registration != ''
        )
        WHERE registration IS NULL
    """)
    logger.info("  Migration v1: registration backfill done")

    # 5b. last_callsign, aircraft_type, aircraft_type_desc, last_lat,
    # last_lon, last_seen_at from the most recent all_sightings row per ICAO.
    # We use a correlated subquery to find the latest row per ICAO and
    # pull all the relevant columns in one pass. On large installs this
    # is the heaviest step of the migration — see test harness for
    # measurement.
    conn.execute("""
        UPDATE seen_aircraft
        SET last_callsign = (
                SELECT a.callsign FROM all_sightings a
                WHERE a.icao = seen_aircraft.icao
                ORDER BY a.seen_at DESC LIMIT 1
            ),
            aircraft_type = COALESCE(
                aircraft_type,
                (SELECT a.aircraft_type FROM all_sightings a
                 WHERE a.icao = seen_aircraft.icao
                   AND a.aircraft_type IS NOT NULL AND a.aircraft_type != ''
                 ORDER BY a.seen_at DESC LIMIT 1),
                first_aircraft_type
            ),
            aircraft_type_desc = (
                SELECT a.type_desc FROM all_sightings a
                WHERE a.icao = seen_aircraft.icao
                  AND a.type_desc IS NOT NULL AND a.type_desc != ''
                ORDER BY a.seen_at DESC LIMIT 1
            ),
            last_lat = (
                SELECT a.lat FROM all_sightings a
                WHERE a.icao = seen_aircraft.icao
                  AND a.lat IS NOT NULL
                ORDER BY a.seen_at DESC LIMIT 1
            ),
            last_lon = (
                SELECT a.lon FROM all_sightings a
                WHERE a.icao = seen_aircraft.icao
                  AND a.lon IS NOT NULL
                ORDER BY a.seen_at DESC LIMIT 1
            ),
            last_seen_at = (
                SELECT MAX(a.seen_at) FROM all_sightings a
                WHERE a.icao = seen_aircraft.icao
            )
        WHERE last_seen_at IS NULL
    """)
    logger.info("  Migration v1: sightings backfill done (heaviest step)")

    # 5c. country derived via the static range table in countries.py.
    # We do this in Python because the lookup is a binary search over a
    # sorted Python list — not expressible as a single SQL UPDATE with
    # acceptable performance. Cheap: ~250 ranges, ~21k aircraft on
    # reference Pi install = a few thousand binary searches, sub-second.
    try:
        from countries import country_for_icao
    except ImportError:
        country_for_icao = None
    if country_for_icao is not None:
        rows = conn.execute(
            "SELECT icao FROM seen_aircraft WHERE country IS NULL"
        ).fetchall()
        n_country_resolved = 0
        for (icao,) in rows:
            cname = country_for_icao(icao)
            if cname is not None:
                conn.execute(
                    "UPDATE seen_aircraft SET country = ? WHERE icao = ? AND country IS NULL",
                    (cname, icao),
                )
                n_country_resolved += 1
        logger.info(f"  Migration v1: country backfill done "
                    f"({n_country_resolved} of {len(rows)} resolved)")

    # 5d. sighting_count from the rollup. The rollup sums per-hour-bucket
    # sighting counts; summing them gives total sightings per aircraft
    # over the whole archive. This is the right number — represents the
    # full retention-independent history because sightings_hourly is
    # never pruned.
    conn.execute("""
        UPDATE seen_aircraft
        SET sighting_count = COALESCE((
            SELECT SUM(sh.sighting_count) FROM sightings_hourly sh
            WHERE sh.icao = seen_aircraft.icao
        ), 0)
        WHERE sighting_count = 0
    """)

    # 5e. operator: derived from callsign prefix. Keep this as a NULL
    # initially in this first migration — operator inference logic
    # lives in server.py at present and would require lifting it into
    # a shared module to call from here. The collector will populate
    # operator on the next sighting per aircraft. Acceptable: the
    # column is queryable as soon as it has data, and search results
    # without operator just don't show that field for that aircraft.
    # Filed as a v2.51.0 follow-up to extract operator-derivation into
    # a shared helper so backfill can use it.

    # --- Step 6: mark all rows as fts_dirty for the collector's first cycle ---
    # The collector's cycle-end handler will then flush these to FTS5
    # in a single batch. This avoids running an FTS5 rebuild during
    # migration itself (which is a startup-blocking operation; on a
    # 200k-row install that's ~5 seconds we'd rather not pay at every
    # restart even though migration runs once).
    #
    # The cost: the *first* post-migration collector cycle will be
    # slower than usual because it has every row to flush. At Pi-user
    # scale (~21k rows) this is roughly 150-300ms one-time. After that
    # cycle, all rows are clean and steady-state behavior kicks in.
    logger.info(f"  Migration v1: marking {n_seen} rows dirty for first FTS5 flush")
    conn.execute("UPDATE seen_aircraft SET fts_dirty = 1")


def _migration_v2_operator_backfill(conn: sqlite3.Connection) -> None:
    """v2.50.42: backfill the seen_aircraft.operator column from each
    aircraft's last_callsign, and mark all affected rows as fts_dirty
    so the collector's next cycle re-enriches their FTS rows with the
    full airline name (e.g. 'UAL United Airlines' instead of just 'UAL').

    Migration v1 added the operator COLUMN but didn't populate it —
    that was filed as a follow-up because v1's backfill code didn't
    have access to a Python-resident airline lookup. This migration
    closes that gap.

    Idempotent: re-running this migration is a no-op for rows already
    correctly populated. Safe across edge cases:
      - rows with NULL last_callsign or empty string → no operator set
      - rows with last_callsign that doesn't start with 3 letters →
        no operator set (matches the runtime collector behavior)
      - rows with a 3-letter prefix that isn't in AIRLINES → no
        operator set (consistent: we don't fabricate operators)
    """
    from designators import operator_from_callsign

    rows = conn.execute("""
        SELECT icao, last_callsign FROM seen_aircraft
        WHERE last_callsign IS NOT NULL AND last_callsign != ''
          AND (operator IS NULL OR operator = '')
    """).fetchall()

    updates = []
    for icao, callsign in rows:
        op = operator_from_callsign(callsign)
        if op:
            updates.append((op, icao))

    if updates:
        conn.executemany(
            "UPDATE seen_aircraft SET operator = ?, fts_dirty = 1 WHERE icao = ?",
            updates
        )

    # Log a one-line summary so install operators can verify the
    # backfill did something. No-op installs (no aircraft with airline
    # callsigns) get a "0 rows" line — also useful signal.
    logger.info(
        f"Migration v2: operator backfill — derived for {len(updates)} of "
        f"{len(rows)} candidate aircraft"
    )


def _migration_v3_distance_column(conn: sqlite3.Connection) -> None:
    """v2.60.1 (Phase 1A.5 perf): add `last_distance` column to
    seen_aircraft so Search can ORDER BY distance across the full
    result set, not just the current page.

    The column stores distance in **kilometers** (always — the
    canonical-unit choice keeps the math simple). Display-time unit
    conversion to miles / nmi happens in server.py response
    annotation, where it always has.

    NULL means "we don't know the distance for this aircraft" — either
    the receiver location isn't configured yet, or the aircraft was
    seen without coordinates. SQL ORDER BY on this column uses
    `last_distance IS NULL` as a pre-sort key so NULL rows always
    appear last regardless of asc/desc.

    The migration itself only creates the column and index. Backfill
    happens in main.py at startup, after CONFIG is loaded — same code
    path that runs on receiver-location-change. That keeps the
    backfill logic in one place rather than duplicating it here.

    Idempotent: re-running this migration is a no-op (ALTER TABLE
    will fail with "duplicate column name" — caught and swallowed).
    """
    try:
        conn.execute("ALTER TABLE seen_aircraft ADD COLUMN last_distance REAL")
        logger.info("Migration v3: added seen_aircraft.last_distance column")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            logger.info("Migration v3: last_distance column already exists "
                        "(re-run no-op)")
        else:
            raise

    # Index on last_distance to make ORDER BY fast even for queries
    # that don't filter heavily before sort (e.g. broad searches like
    # "United States" returning thousands of rows). The index is small
    # — REAL values, ~7K rows on a typical install. CREATE INDEX IF
    # NOT EXISTS handles the re-run case cleanly.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_seen_distance
        ON seen_aircraft(last_distance)
    """)
    logger.info("Migration v3: created idx_seen_distance index")


def _migration_v4_concurrent_minute(conn: sqlite3.Connection) -> None:
    """Migration v4 (v2.87.0): create concurrent_minute table + backfill.

    Adds a per-minute rollup of concurrent aircraft counts that the
    Stats endpoint's peak_simultaneous and average_concurrent cards
    can query directly instead of GROUP BY-ing all_sightings every
    request. Each row is one minute bucket and the maximum number of
    distinct aircraft visible during any sub-poll within that minute.

    Why this exists: peak_simultaneous and average_concurrent were the
    last two slow Stats queries after the v2.85.x → v2.86.x rollup
    work. Both group all_sightings by 60-second bucket and count
    distinct ICAOs per bucket, which on busy installs touches
    hundreds of thousands of rows per Stats page render. With this
    rollup the Stats queries become a single index seek + scan over
    ~1440 rows/day. Live updates happen in the collector poll path
    (one extra UPSERT per poll, negligible cost).

    Schema:
        minute_bucket  INTEGER PRIMARY KEY    (epoch seconds, top of minute)
        count          INTEGER NOT NULL       (peak distinct aircraft this minute)

    Semantic note: the original peak_simultaneous query computed
    COUNT(DISTINCT icao) per 60-second bucket — i.e., the *union* of
    aircraft seen across all sub-polls within that bucket. The new
    table stores the *maximum* count seen at any single sub-poll
    within the bucket. For default 60s poll cadence these are
    identical (one sub-poll per bucket). For sub-60s cadences the
    new metric is arguably more meaningful: "what's the largest
    number of aircraft visible at the same instant" rather than
    "what's the union of aircraft across all instants in this
    minute." The MAX-style aggregation also avoids the union's
    counterintuitive case where aircraft A and B never coexisted
    but both appeared within the same 60-second window.

    Backfill: scans all_sightings, computes COUNT(DISTINCT icao) per
    60-second bucket, populates concurrent_minute. On a busy install
    (15M rows, 1-CPU VM) this takes ~30 seconds — paid once at
    upgrade time. Without backfill, peak_simultaneous would show 0
    until midnight rolls over to a fresh day. Skipped if the table
    is already populated, so re-running the migration is a no-op.

    Idempotent: safe to re-run. CREATE TABLE IF NOT EXISTS handles
    the table; the backfill is gated on a row-count check.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concurrent_minute (
            minute_bucket INTEGER PRIMARY KEY,
            count INTEGER NOT NULL
        )
    """)
    logger.info("Migration v4: created concurrent_minute table")

    # Backfill only when the table is empty. If a previous attempt
    # populated some buckets and crashed, we'd rather complete the
    # backfill than leave a partial state — but in practice "empty
    # vs populated" is a clean enough signal because the backfill is
    # one transaction.
    existing = conn.execute(
        "SELECT COUNT(*) FROM concurrent_minute"
    ).fetchone()[0]
    if existing == 0:
        # Source query: GROUP BY 60-second bucket, COUNT(DISTINCT icao).
        # This recovers the "union" semantic for historical data, which
        # is fine — the collector forward-going writes will use the
        # tighter "max per sub-poll" semantic. Mixing the two is
        # acceptable because the all_sightings retention window means
        # backfilled data ages out within all_days days anyway.
        logger.info(
            "Migration v4: backfilling concurrent_minute from "
            "all_sightings (this can take ~30s on busy installs)…"
        )
        conn.execute("""
            INSERT INTO concurrent_minute(minute_bucket, count)
            SELECT (seen_at / 60) * 60 AS mb,
                   COUNT(DISTINCT icao) AS cnt
            FROM all_sightings
            GROUP BY mb
        """)
        backfilled = conn.execute(
            "SELECT COUNT(*) FROM concurrent_minute"
        ).fetchone()[0]
        logger.info(
            f"Migration v4: backfilled {backfilled} minute buckets "
            f"from all_sightings"
        )
    else:
        logger.info(
            f"Migration v4: concurrent_minute already populated "
            f"({existing} rows); skipping backfill"
        )


def _migration_v5_min_nonzero_altitude(conn: sqlite3.Connection) -> None:
    """Migration v5 (v2.87.1): add sightings_hourly.min_nonzero_altitude.

    Adds a per-bucket "minimum nonzero altitude" column so the Stats
    `lowest_altitude` card can read from sightings_hourly instead of
    scanning all_sightings on every render.

    Why a new column instead of just using min_altitude: the existing
    min_altitude column captures the bucket's overall minimum, which
    can be 0 for an aircraft that taxied or was on the ground during
    the same hour it was airborne. Filtering `min_altitude > 0`
    against that column would exclude the entire bucket, never
    seeing the airborne low-altitude readings (taxi → takeoff →
    climb to 200ft within one hour: bucket min_altitude=0, but the
    "lowest non-zero altitude" of 200ft is what we want for the
    Stats card). The new column tracks the minimum of just the
    non-zero observations, preserving correctness for the rewrite.

    Wave by wave:
      - Wave A: ALTER TABLE adds the column (NULL for existing rows)
      - Wave B: backfill from all_sightings via an UPDATE FROM that
        aggregates per (icao, hour_bucket) the MIN of non-zero,
        non-stringy altitude readings.
      - Forward-going writes are handled in collector.py's per-poll
        sightings_hourly UPSERT — same MIN-with-null-handling pattern
        as the existing min_altitude column.

    Idempotent: re-running this migration is a no-op. ALTER TABLE
    raises "duplicate column" if the column exists, which we catch.
    Backfill is gated on a NULL-count check so re-running doesn't
    redo the work or clobber forward-going collector writes.

    Backfill cost: ~10-30s on a busy install (15M all_sightings rows,
    1-CPU VM). Aggregation runs once over all_sightings with a
    GROUP BY producing ~200K rows; UPDATE then seeks each row in
    sightings_hourly via the (icao, hour_bucket) primary key.
    """
    try:
        conn.execute(
            "ALTER TABLE sightings_hourly ADD COLUMN min_nonzero_altitude REAL"
        )
        logger.info(
            "Migration v5: added sightings_hourly.min_nonzero_altitude column"
        )
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            logger.info(
                "Migration v5: min_nonzero_altitude column already exists "
                "(re-run no-op)"
            )
        else:
            raise

    # Backfill: only if there are NULL values to fill in. Re-running
    # after a successful backfill is a no-op. Re-running after a
    # crashed partial backfill resumes from where it left off (the
    # already-backfilled rows have non-NULL values and will be
    # selected back into the same value, which is fine).
    null_count = conn.execute(
        "SELECT COUNT(*) FROM sightings_hourly "
        "WHERE min_nonzero_altitude IS NULL"
    ).fetchone()[0]
    total_count = conn.execute(
        "SELECT COUNT(*) FROM sightings_hourly"
    ).fetchone()[0]

    if null_count == 0 or total_count == 0:
        logger.info(
            f"Migration v5: backfill not needed "
            f"(null_count={null_count}, total_count={total_count})"
        )
        return

    logger.info(
        f"Migration v5: backfilling min_nonzero_altitude for "
        f"{null_count} sightings_hourly rows from all_sightings "
        f"(this can take 10-30s on busy installs)…"
    )
    # SQLite 3.33+ UPDATE FROM. Aggregation runs once over
    # all_sightings; UPDATE seeks each sightings_hourly row via
    # the (icao, hour_bucket) primary key. Typeof guard mirrors
    # the original lowest_altitude query — defends against stringy
    # values like "ground" that SQLite happily stores in REAL columns.
    conn.execute("""
        UPDATE sightings_hourly
        SET min_nonzero_altitude = bucket_mins.min_alt
        FROM (
            SELECT icao,
                   (seen_at / 3600) * 3600 AS hour_bucket,
                   MIN(altitude) AS min_alt
            FROM all_sightings
            WHERE altitude IS NOT NULL
              AND typeof(altitude) IN ('integer', 'real')
              AND altitude > 0
            GROUP BY icao, hour_bucket
        ) AS bucket_mins
        WHERE sightings_hourly.icao = bucket_mins.icao
          AND sightings_hourly.hour_bucket = bucket_mins.hour_bucket
    """)
    backfilled = conn.execute(
        "SELECT COUNT(*) FROM sightings_hourly "
        "WHERE min_nonzero_altitude IS NOT NULL"
    ).fetchone()[0]
    logger.info(
        f"Migration v5: backfilled min_nonzero_altitude on {backfilled} "
        f"of {total_count} sightings_hourly rows. The remaining "
        f"{total_count - backfilled} rows had no non-zero altitude "
        f"observations in their hour bucket (legitimate NULL — aircraft "
        f"may have only been seen on the ground or without altitude data)."
    )


# v2.88.0: backfill context for migration v6 (aircraft_track_daily).
# Populated by main.py from the parsed YAML config just before calling
# apply_schema_migrations(). Migrations don't otherwise have access to
# server-side CONFIG state because they run before the FastAPI app
# starts up. Same shape as `set_db_tuning_profile` in collector.py,
# just adapted to the migration entry path.
#
# Keys read by _migration_v6_aircraft_track_daily:
#   stats_tz       — IANA timezone name (str), empty → system local
#   track_gap_min  — minutes (int), default 5
#
# Setting this dict has no effect on already-applied migrations
# (idempotent backfill skips work when rows already exist).
_v6_backfill_config: Dict[str, Any] = {}


def set_v6_backfill_config(stats_tz: Optional[str],
                            track_gap_min: Optional[int]) -> None:
    """Provide migration v6 (aircraft_track_daily) with the timezone
    and gap-minutes values it needs to compute today's local-midnight
    bucket and detect session boundaries during backfill. Call this
    from main.py with values from the parsed config, immediately
    before calling apply_schema_migrations(). No-op for already-
    applied migrations (the backfill is idempotent on row count)."""
    _v6_backfill_config["stats_tz"] = stats_tz or ""
    _v6_backfill_config["track_gap_min"] = (
        int(track_gap_min) if track_gap_min is not None else 5
    )


def _migration_v6_aircraft_track_daily(conn: sqlite3.Connection) -> None:
    """Migration v6 (v2.88.0): create aircraft_track_daily table + today-only backfill.

    Adds a per-aircraft per-day session-tracking rollup that the Stats
    `longest_track` card (and its drill panel) can read via a trivial
    ORDER BY DESC LIMIT 1, instead of pulling 950K+ (icao, seen_at)
    rows from all_sightings and walking them in Python on every
    render. Drops longest_track from ~1.4-1.7s to single-digit ms on
    busy installs — the last big slow Stats query.

    Schema:
        icao                  TEXT NOT NULL
        day_bucket            INTEGER NOT NULL  (local-midnight epoch seconds)
        callsign              TEXT             (most-recent non-empty callsign)
        aircraft_type         TEXT             (most-recent non-empty type code)
        current_session_start INTEGER NOT NULL (epoch seconds)
        current_session_last  INTEGER NOT NULL (epoch seconds)
        best_session_start    INTEGER NOT NULL (epoch seconds)
        best_session_end      INTEGER NOT NULL (epoch seconds)
        best_session_duration INTEGER NOT NULL DEFAULT 0
        PRIMARY KEY (icao, day_bucket)

    Why per-(icao, day) rather than per-session: the longest_track
    card only ever asks "what's the longest session today, period",
    so summarizing the whole day in one row per aircraft is the
    smallest schema that solves the problem. A per-session table
    would be more flexible for hypothetical future cards but every
    query becomes a composition step (MAX over per-aircraft sessions),
    and the simpler design matches the pattern of sightings_hourly /
    concurrent_minute (one row per bucket).

    Why local-tz day buckets rather than UTC: sightings_hourly and
    concurrent_minute use UTC buckets and read with `bucket >=
    local_midnight`, which works for SUM/COUNT/MAX aggregations
    because off-by-one-bucket at the boundary is harmless. For
    session tracking the bucket boundary matters more — a long
    flight crossing UTC midnight (which is mid-evening for non-UTC
    users) would split mid-flight in the rollup. Local-tz alignment
    splits sessions only at local midnight, when traffic is
    minimal, matching the user's mental model of "today".

    Why bake gap_min into the rollup at ingest rather than recompute
    on config change: matches the existing precedent that CONFIG
    reloads update the global but don't reprocess existing data
    (retention changes, tz changes, tuning changes all behave the
    same way). Changing track_gap_minutes propagates forward; full
    effect after rollup rows age out (~24h for today's bucket).
    Documented in the config tooltip.

    Backfill scope: today only. The longest_track card only ever
    reads today's bucket, and the migration's job is to make sure
    today's bucket is correct as of upgrade time — yesterday and
    older buckets don't power any current card and would just
    inflate the migration cost. Backfill walks today's slice of
    all_sightings (~50K rows on a 1-CPU test VM, ~50-200K on busy
    installs), sorts and groups by icao in Python, applies the same
    session-walk logic as the live collector, and INSERTs one row
    per aircraft active today. Estimated 5-10s on the 1-CPU VM,
    proportionally faster on a Pi 4B. Well within the v2.87.2
    polling state machine's comfortable window.

    Idempotent: CREATE TABLE IF NOT EXISTS handles the table; the
    backfill is gated on a row-count check, so re-running the
    migration is a no-op. Re-running after a crashed partial backfill
    isn't ideal (the partial state remains), but the worst-case
    impact is "today's longest_track underreports until the next
    midnight" — recoverable by deleting the table and re-running.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aircraft_track_daily (
            icao TEXT NOT NULL,
            day_bucket INTEGER NOT NULL,
            callsign TEXT,
            aircraft_type TEXT,
            current_session_start INTEGER NOT NULL,
            current_session_last  INTEGER NOT NULL,
            best_session_start    INTEGER NOT NULL,
            best_session_end      INTEGER NOT NULL,
            best_session_duration INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (icao, day_bucket)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_atd_day_best_dur
        ON aircraft_track_daily(day_bucket, best_session_duration DESC)
    """)
    logger.info(
        "Migration v6: created aircraft_track_daily table + idx_atd_day_best_dur"
    )

    # Backfill only when the table is empty — same gating pattern as
    # migration v4. If a previous attempt populated some rows and
    # crashed, we leave them; the worst-case impact is undercount on
    # today's longest_track until midnight rolls over.
    existing = conn.execute(
        "SELECT COUNT(*) FROM aircraft_track_daily"
    ).fetchone()[0]
    if existing > 0:
        logger.info(
            f"Migration v6: aircraft_track_daily already populated "
            f"({existing} rows); skipping backfill"
        )
        return

    # Compute today's local-midnight epoch using whatever timezone
    # the server is configured to use. The migration runs before
    # CONFIG is loaded into server.py's globals, so we read from
    # the module-level _v6_backfill_config dict that main.py
    # populates from the parsed YAML config just before calling
    # apply_schema_migrations(). Empty/invalid tz string → system
    # local time, mirroring server._day_bounds_ts()'s fallback.
    from datetime import datetime
    tz = None
    tz_name = (_v6_backfill_config.get("stats_tz") or "").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    now_dt = datetime.now(tz) if tz else datetime.now()
    today_midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ts = int(today_midnight.timestamp())

    # Same source for gap_minutes. Default 5 matches the CONFIG
    # default declared in config.yaml.example.
    try:
        gap_min = int(_v6_backfill_config.get("track_gap_min") or 5)
    except (ValueError, TypeError):
        gap_min = 5
    if gap_min < 1:
        gap_min = 1
    gap_sec = gap_min * 60

    logger.info(
        f"Migration v6: backfilling aircraft_track_daily for today "
        f"(local midnight={today_start_ts}, gap_min={gap_min})…"
    )

    # Pull today's slice ordered by (icao, seen_at) so we can walk
    # each icao's sightings sequentially. Same predicate as the
    # original longest_track query (>= local midnight). On a busy
    # install this is 50-200K rows; on a 1-CPU reference VM, ~50K.
    # ~hex pseudo-targets are NOT filtered here — matching the live
    # collector path's behavior, which writes all aircraft to the
    # rollup. Read-side queries filter ~hex via WHERE icao NOT LIKE
    # '~%' the same way they do for sightings_hourly.
    cursor = conn.execute("""
        SELECT icao, seen_at, callsign, aircraft_type
        FROM all_sightings
        WHERE seen_at >= ?
        ORDER BY icao, seen_at
    """, (today_start_ts,))

    # Walk row-by-row, emitting a finalized row per icao when the
    # icao changes. Streaming keeps peak memory bounded — no
    # materializing of the full result set in Python.
    inserts: List[tuple] = []
    cur_icao = None
    cur_callsign = ""
    cur_type = ""
    sess_start = 0
    sess_last = 0
    best_start = 0
    best_end = 0
    best_dur = 0

    def _finalize(icao_, callsign_, type_, sess_start_, sess_last_,
                   best_start_, best_end_, best_dur_):
        # Promote the in-flight session to best if it's longer
        # (handles the icao-with-only-one-session case).
        cur_dur = sess_last_ - sess_start_
        if cur_dur > best_dur_:
            best_start_ = sess_start_
            best_end_ = sess_last_
            best_dur_ = cur_dur
        inserts.append((
            icao_, today_start_ts, callsign_, type_,
            sess_start_, sess_last_,
            best_start_, best_end_, best_dur_,
        ))

    for row in cursor:
        icao = row[0]
        seen_at = row[1]
        callsign_raw = (row[2] or "").strip()
        type_raw = (row[3] or "").strip()
        if icao != cur_icao:
            # Emit the previous icao's row (if any), reset state.
            if cur_icao is not None:
                _finalize(cur_icao, cur_callsign, cur_type,
                          sess_start, sess_last,
                          best_start, best_end, best_dur)
            cur_icao = icao
            cur_callsign = callsign_raw
            cur_type = type_raw
            sess_start = seen_at
            sess_last = seen_at
            best_start = seen_at
            best_end = seen_at
            best_dur = 0
            continue
        # Same icao: update callsign/type if non-empty (latest wins,
        # matching the COALESCE-NULLIF pattern in the live collector).
        if callsign_raw:
            cur_callsign = callsign_raw
        if type_raw:
            cur_type = type_raw
        # Session continuation vs gap detection.
        if seen_at - sess_last > gap_sec:
            # Close current, check if it's the new best, start new.
            cur_dur = sess_last - sess_start
            if cur_dur > best_dur:
                best_start = sess_start
                best_end = sess_last
                best_dur = cur_dur
            sess_start = seen_at
        sess_last = seen_at
    # Tail icao (loop ends without re-entering the icao-change branch).
    if cur_icao is not None:
        _finalize(cur_icao, cur_callsign, cur_type,
                  sess_start, sess_last,
                  best_start, best_end, best_dur)

    if inserts:
        conn.executemany("""
            INSERT INTO aircraft_track_daily
                (icao, day_bucket, callsign, aircraft_type,
                 current_session_start, current_session_last,
                 best_session_start, best_session_end, best_session_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, inserts)
    logger.info(
        f"Migration v6: backfilled {len(inserts)} aircraft_track_daily rows "
        f"for today (icaos active in today's all_sightings slice)"
    )


def _migration_v7_category_column(conn: sqlite3.Connection) -> None:
    """Migration v7 (v2.89.0): add seen_aircraft.category column + backfill.

    Adds a per-aircraft category column (commercial / general_aviation /
    military / helicopter / unknown) populated by the collector at
    write time. Two motivating uses:

      1. Stats `category_mix` card replaces a 30-line Python heuristic
         loop with a single SQL GROUP BY against a stored column.
      2. Search system gains category tokens (commercial, general_aviation,
         helicopter — military already exists) in v2.90.0.

    Schema:
        seen_aircraft.category TEXT
        idx_seen_category ON seen_aircraft(category)

    Backfill: every existing seen_aircraft row gets categorized using
    the same heuristics the collector applies forward. Military
    membership is determined transitively — if any military_sightings
    row exists for an icao, that icao was classified military by some
    prior poll's is_military() call, so it stays military. (The
    "sticky military" semantic the collector enforces forward is
    naturally inherited backward by checking historical
    military_sightings membership.)

    Heuristics live in categorize.classify() — single source of truth.
    Importing it here is fine; categorize.py is a leaf module with
    no aerodrome-internal dependencies.

    Idempotent: ALTER TABLE catches "duplicate column" on re-run; the
    backfill is gated on a NULL-count check so it only runs when
    there's work to do.

    Backfill cost: O(seen_aircraft rows). On the test VM with ~26K
    rows, single-digit seconds. The military-membership EXISTS check
    uses idx_mil_seen_icao for fast lookup per icao.
    """
    try:
        conn.execute(
            "ALTER TABLE seen_aircraft ADD COLUMN category TEXT"
        )
        logger.info("Migration v7: added seen_aircraft.category column")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            logger.info(
                "Migration v7: category column already exists (re-run no-op)"
            )
        else:
            raise

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seen_category "
        "ON seen_aircraft(category)"
    )
    logger.info("Migration v7: ensured idx_seen_category index")

    # Backfill only when there are NULLs to fill. Re-running after a
    # successful backfill is a no-op; re-running after a partial
    # backfill resumes from where it left off (already-backfilled
    # rows have non-NULL values and are skipped by the WHERE clause).
    null_count = conn.execute(
        "SELECT COUNT(*) FROM seen_aircraft WHERE category IS NULL"
    ).fetchone()[0]
    if null_count == 0:
        logger.info(
            "Migration v7: backfill not needed (no NULL category rows)"
        )
        return

    logger.info(
        f"Migration v7: backfilling category for {null_count} "
        f"seen_aircraft rows…"
    )

    # Detect whether military_sightings exists before referencing it.
    # The table is created by init_db() in modern installs, but very
    # old test fixtures (and conceivably pre-v2.51 installs that
    # never created the table) may be missing it. When absent, every
    # aircraft falls through to type-based classification — no
    # historical military determinations to inherit, which is correct
    # for that pre-existing-table state. Forward-going polls populate
    # military_sightings via the collector's existing path; the
    # category column then upgrades on the sticky-military rule.
    _has_mil_table = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'military_sightings'"
    ).fetchone() is not None

    if _has_mil_table:
        rows = conn.execute("""
            SELECT
                sa.icao,
                sa.aircraft_type,
                sa.aircraft_type_desc,
                EXISTS(
                    SELECT 1 FROM military_sightings ms
                    WHERE ms.icao = sa.icao
                ) AS is_mil
            FROM seen_aircraft sa
            WHERE sa.category IS NULL
        """).fetchall()
    else:
        logger.info(
            "Migration v7: military_sightings table absent (pre-v2.51 "
            "schema?); falling back to type-only classification for backfill"
        )
        rows = conn.execute("""
            SELECT
                sa.icao,
                sa.aircraft_type,
                sa.aircraft_type_desc,
                0 AS is_mil
            FROM seen_aircraft sa
            WHERE sa.category IS NULL
        """).fetchall()

    # Local import — categorize.py is a sibling module. Importing
    # inside the migration function keeps the module-import side
    # effects bounded; if categorize.py ever fails to import, only
    # this migration breaks rather than the whole schema_migrations
    # module.
    from categorize import classify

    updates = []
    for icao, aircraft_type, type_desc, is_mil in rows:
        cat = classify(aircraft_type, type_desc, bool(is_mil))
        updates.append((cat, icao))

    if updates:
        conn.executemany(
            "UPDATE seen_aircraft SET category = ? WHERE icao = ?",
            updates,
        )
    logger.info(
        f"Migration v7: backfilled category on {len(updates)} "
        f"seen_aircraft rows"
    )


def _migration_v8_update_state(conn: sqlite3.Connection) -> None:
    """v8: add update_state table for GitHub-update-channel state.

    Single-row table (enforced via CHECK (id = 1)) that tracks the most
    recent GitHub Releases API check. Two distinct timestamps:

    - last_check_ts: when the most recent check attempt happened, regardless
      of success or failure. Drives the "last checked: X ago" display and
      the interval-elapsed decision in the scheduler.
    - last_known_latest_ts: when the most recent SUCCESSFUL check happened.
      Drives the "last successful check: X ago" display when the latest
      attempt errored, so the user can tell stale-data from fresh-data
      regardless of recent network issues.

    last_check_result is 'success' or 'error' (or NULL if never checked).
    last_check_error carries the error message when result is 'error'.
    last_known_latest carries the latest version tag from GitHub (e.g.
    'v2.99.0') after a successful check, never overwritten on errors.

    The table is intentionally NOT pre-populated with a row — the
    scheduler's first check (on startup if interval elapsed) does the
    initial INSERT OR REPLACE. Empty table means "never checked," which
    is the correct initial state.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS update_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_check_ts INTEGER,
            last_check_result TEXT,
            last_check_error TEXT,
            last_known_latest TEXT,
            last_known_latest_ts INTEGER,
            updated_at INTEGER NOT NULL
        )
        """
    )
    logger.info("Migration v8: created update_state table (single-row, empty)")


def _migration_v9_seen_furthest_covering_index(conn: sqlite3.Connection) -> None:
    """v9 (v3.4.8): covering index for stats_furthest_prerank.

    Context: v3.4.7 originally added this index inside
    _migration_v1_search_schema. That was wrong — modifying an
    already-run migration is a silent no-op on existing installs
    (the registry sees "v1 already applied" and skips it). Fresh
    installs got the index; everyone upgrading didn't. v3.4.8
    moves the index to a proper new migration so existing installs
    actually receive it.

    The Stats card "furthest aircraft" pre-rank query is:
      SELECT icao, (computed_distance) AS dist_proxy
      FROM seen_aircraft
      WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
        AND last_seen_at >= ?
      ORDER BY dist_proxy DESC LIMIT 100

    With idx_seen_last alone (single column on last_seen_at), the
    planner does a range scan but then table-fetches each matching
    row to read last_lat / last_lon / icao. At 170K seen_aircraft
    rows on cold cache, that's ~170K disk reads. The v3.4.6
    cross-RAM benchmark showed this query at 226 ms (4 GB), 130 ms
    (6 GB), 49 ms (8 GB) — clearly disk-I/O bound on those table
    fetches.

    With (last_seen_at, last_lat, last_lon, icao) as a covering
    index, the planner satisfies both the range filter AND reads
    all selected columns directly from index leaves — no table
    fetch. icao is included because seen_aircraft uses TEXT
    PRIMARY KEY (not INTEGER PRIMARY KEY), so icao is stored in
    the table, not as the rowid; without including it the index
    isn't fully covering and the planner correctly declines to
    pick it.

    The ORDER BY temp B-tree on the computed dist_proxy still has
    to happen (computed expressions can't be indexed) but that's
    CPU on the narrowed result, not disk I/O.

    Index size: ~38 bytes/row × ~170K rows ≈ 6.5 MB on a typical
    heavy-tier install. One-time build cost during this migration
    proportional to seen_aircraft row count — well under a second
    for typical sizes.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seen_last_latlon "
        "ON seen_aircraft(last_seen_at, last_lat, last_lon, icao)"
    )
    logger.info(
        "Migration v9: created idx_seen_last_latlon covering index "
        "for stats_furthest_prerank"
    )


# Ordered list of all migrations. NEVER edit a shipped migration —
# always add a new one. The list is the source of truth for what
# CURRENT_SCHEMA_VERSION should be.
MIGRATIONS: List[Migration] = [
    (1, "search schema: denormalized columns, indexes, FTS5 + triggers, backfill",
     _migration_v1_search_schema),
    (2, "operator backfill: derive operator code from last_callsign for existing rows",
     _migration_v2_operator_backfill),
    (3, "distance column: add seen_aircraft.last_distance for full-result-set distance sort",
     _migration_v3_distance_column),
    (4, "concurrent_minute rollup: per-minute aircraft count for peak/average concurrency cards",
     _migration_v4_concurrent_minute),
    (5, "min_nonzero_altitude column: per-bucket minimum nonzero altitude for lowest_altitude card",
     _migration_v5_min_nonzero_altitude),
    (6, "aircraft_track_daily rollup: per-aircraft per-day session tracking for longest_track card",
     _migration_v6_aircraft_track_daily),
    (7, "category column: per-aircraft category for category_mix card and search filter",
     _migration_v7_category_column),
    (8, "update_state table: single-row state for GitHub-update-channel cache (v3.0.0)",
     _migration_v8_update_state),
    (9, "seen_aircraft covering index for stats_furthest_prerank (v3.4.8 — corrects v3.4.7's mis-placed index)",
     _migration_v9_seen_furthest_covering_index),
]


# Sanity check at import: CURRENT_SCHEMA_VERSION should match the
# highest migration number declared. Catches the easy "added a migration,
# forgot to bump the constant" bug at module load time.
assert CURRENT_SCHEMA_VERSION == max(m[0] for m in MIGRATIONS), (
    f"CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION} doesn't match "
    f"max migration version={max(m[0] for m in MIGRATIONS)}"
)


# =============================================================================
# v2.87.3: schema pre-flight
# =============================================================================
# This is a hedge, not a guarantee. The intent is to catch a specific class
# of bug — column-name typos in queries that reference nonexistent columns —
# at startup time, instead of at the next time a user clicks the affected
# page. The motivating case was v2.86.4: a Stats query selected
# `last_altitude` from `seen_aircraft`, but `last_altitude` is a column on
# `sightings_hourly`, not `seen_aircraft`. The query crashed at runtime,
# the outer try/except caught it, and the entire Stats endpoint returned
# 500 with empty cards — visible to the user as "No stat cards to show".
# Python AST validation and the test suite couldn't catch the bug because
# the SQL was syntactically valid Python; it only fails when SQLite actually
# tries to bind the column name against the live schema.
#
# Honest scope: this function checks that the columns the Stats endpoint's
# queries reference actually exist in the live schema. It does NOT actually
# run the queries (that would require parameter values, side effects on the
# slow-query log, etc.), and it does NOT detect every possible column-name
# typo automatically — the EXPECTED_TABLES list is hand-maintained and
# subject to drift. When you add a new column reference to a Stats query,
# you must also add it here. The discipline is "update both lists when
# changing one" — exactly the discipline whose absence caused v2.86.4 in
# the first place. So why bother? Because the cost of maintaining this
# list is small (one line per column), and a hedge that catches half of
# future v2.86.4-class bugs is better than no hedge at all.
#
# What this catches well:
#   - Column names referenced by Stats queries that don't exist on the
#     stated table (the v2.86.4 case)
#   - Tables that are referenced but missing entirely (e.g. forgot a
#     migration)
#   - Future renames that update the schema but miss the query, IF the
#     reviewer remembers to update this list when changing schema
#
# What this does NOT catch:
#   - Column references that AREN'T listed here (drift between this list
#     and the actual queries — the failure mode that brought us here)
#   - JOIN typos, subquery issues, syntactic SQL errors
#   - Logic bugs (wrong column queried but the column does exist)
#
# Future hardening, if we keep finding this pattern useful, would extract
# the Stats queries into a single dict at module scope and use that as
# both the source of truth and the input to a real EXPLAIN-based pre-flight.
# That's a meaningful refactor (40+ inline queries) and not worth the
# churn until/unless this lighter approach proves itself in practice.

# Tables and columns the Stats endpoint queries reference. KEEP IN SYNC
# with the queries in server.py's get_stats(). Order doesn't matter; the
# pre-flight checks each column independently.
STATS_EXPECTED_SCHEMA = {
    "all_sightings": [
        # all_sightings is queried by lowest_altitude (after v2.87.1's
        # rewrite this falls back to all_sightings only if the rollup
        # column happens to be NULL — but the column refs here are the
        # ones the existing query uses), aircraft_detail_sightings_page,
        # several drill-down queries.
        "icao", "callsign", "speed", "lat", "lon", "altitude",
        "aircraft_type", "type_desc", "seen_at", "squawk",
    ],
    "seen_aircraft": [
        # furthest (v2.86.5+) and the search/detail endpoints. The
        # last_distance column is the one v2.86.4 thought was
        # last_altitude — keeping both names visible here so the
        # historical confusion doesn't repeat.
        "icao", "first_seen_at", "first_callsign", "first_aircraft_type",
        "last_callsign", "aircraft_type", "aircraft_type_desc", "operator",
        "country", "last_lat", "last_lon", "last_distance", "last_seen_at",
        "sighting_count", "fts_dirty",
        # v2.89.0: per-aircraft category for category_mix card and
        # (planned v2.90.0) search filter tokens. Backfilled by
        # migration v7; sticky-military maintained by the collector
        # UPSERT's CASE expression.
        "category",
    ],
    "sightings_hourly": [
        # The big rollup table — drives most of the Stats endpoint after
        # the v2.85 → v2.87 rewrites. Note min_nonzero_altitude (added in
        # migration v5) is here — the new column whose absence v2.86.6
        # worried about and v2.87.1 finally addressed.
        "icao", "hour_bucket", "callsign", "aircraft_type", "type_desc",
        "sighting_count", "first_seen_at", "last_seen_at",
        "last_lat", "last_lon", "last_altitude", "last_speed",
        "min_altitude", "max_altitude", "max_speed", "last_squawk",
        "min_nonzero_altitude",
    ],
    "concurrent_minute": [
        # peak_simultaneous and average_concurrent (v2.87.0)
        "minute_bucket", "count",
    ],
    "aircraft_track_daily": [
        # longest_track card + drill panel (v2.88.0). One row per
        # (icao, day_bucket); current_session_* tracks the in-flight
        # session, best_session_* tracks the longest closed-or-open
        # session today. callsign + aircraft_type are denormalized
        # so the drill-panel rendering doesn't need a follow-up
        # all_sightings lookup the way the v2.68 version did.
        "icao", "day_bucket", "callsign", "aircraft_type",
        "current_session_start", "current_session_last",
        "best_session_start", "best_session_end", "best_session_duration",
    ],
    "military_sightings": [
        "icao", "seen_at", "callsign", "speed", "lat", "lon",
        "altitude", "aircraft_type", "type_desc", "squawk",
    ],
    "watchlist_sightings": [
        "icao", "seen_at", "callsign", "speed", "lat", "lon",
        "altitude", "aircraft_type", "type_desc", "squawk",
        "watchlist_label",
    ],
    "stats_records": [
        # Wave 3 all-time records
        "record_type", "value", "icao", "callsign", "aircraft_type",
        "set_at", "extra",
    ],
}


def verify_stats_schema(conn: sqlite3.Connection) -> int:
    """Verify the Stats endpoint's expected columns exist in the live
    schema. Returns the number of issues found (0 on success). Logs
    a WARNING per missing column or table. Does NOT raise — the
    intent is to surface drift loudly without blocking startup, so
    a partially-broken Stats endpoint is still better than no server.

    Called from main.py at startup, after apply_schema_migrations.
    See the module-level docstring above for the full scope discussion.
    """
    problems = 0
    for table, expected_cols in STATS_EXPECTED_SCHEMA.items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(
                f"Schema preflight: PRAGMA table_info({table}) failed: {e}. "
                f"Stats queries against {table} will fail at runtime."
            )
            problems += 1
            continue
        if not rows:
            logger.warning(
                f"Schema preflight: table '{table}' is missing entirely. "
                f"Schema migrations may not have completed; check logs above."
            )
            problems += 1
            continue
        actual_cols = {row[1] for row in rows}
        for col in expected_cols:
            if col not in actual_cols:
                logger.warning(
                    f"Schema preflight: expected column "
                    f"'{table}.{col}' is missing from the live schema. "
                    f"This usually means STATS_EXPECTED_SCHEMA in "
                    f"schema_migrations.py drifted from the actual "
                    f"schema, OR a migration that should have added "
                    f"this column didn't run."
                )
                problems += 1

    if problems == 0:
        logger.info(
            f"Schema preflight: all {sum(len(v) for v in STATS_EXPECTED_SCHEMA.values())} "
            f"expected columns across {len(STATS_EXPECTED_SCHEMA)} tables present"
        )
    else:
        logger.warning(
            f"Schema preflight: found {problems} issue(s). "
            f"The Stats endpoint may 500 at runtime. "
            f"Verify schema migrations completed successfully."
        )
    return problems
