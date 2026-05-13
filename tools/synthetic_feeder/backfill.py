"""
Mode B — synthetic historical backfill.

Generates N synthetic sightings spread over a time window and
bulk-inserts them into a fresh aircraft_history.db, populating
both `all_sightings` and `sightings_hourly` in a single pass so
the resulting database is immediately usable by the detail page,
drill, and Stats endpoints — i.e. exactly the surfaces we're
trying to test for slowness at scale.

Why both tables in one pass: the production collector populates
sightings_hourly online via UPSERT inside fetch_and_store. A
backfill that only writes all_sightings would leave the rollup
empty, and the detail page's hour-of-day / day-of-week / cruise
altitude / daily totals queries would all return empty results.
That defeats the point of building the test bench at scale.

Why call collector.init_db rather than duplicate schema: schema
evolves through ALTER TABLE migrations (squawk, denormalised
columns on seen_aircraft, indexes added across many releases).
Hand-copying the CREATE TABLE statements would mean we'd ship
a backfill that produces a schema-mismatched DB on the next
release that adds a column. Calling collector.init_db means the
synthetic database is structurally identical to what Aerodrome
would create itself.

Usage::

    # Match a real loaded install's production scale exactly:
    python3 -m tools.synthetic_feeder.backfill --match-loaded

    # Custom shape:
    python3 -m tools.synthetic_feeder.backfill \\
        --rows 5000000 --days 7 --aircraft 10000 \\
        --db ./test_synthetic.db

The script is idempotent in the sense that it always writes a
fresh database — by default ./aircraft_history_synthetic.db.
Pass --db to override. If the target file already exists the
script refuses to run unless --force is given; the goal is to
make it impossible to accidentally pollute a real test install's
production database.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import random
import sqlite3
import sys
import time
from typing import Dict, Iterable, List, Tuple

# Allow `python3 -m tools.synthetic_feeder.backfill` and direct execution
try:
    from .generator import Fleet, Aircraft
except ImportError:  # pragma: no cover
    from generator import Fleet, Aircraft  # type: ignore


logger = logging.getLogger("synthetic_feeder.backfill")


# loaded-install-scale preset: 12.6M sightings over 18 days, 26k unique aircraft.
# Matches the production install we've been triaging against. Callers
# pass --match-loaded to load these without specifying each flag.
LOADED_PRESET = {
    "rows": 12_600_000,
    "days": 18,
    "aircraft": 26_000,
}

# How many rows to buffer before each executemany / commit. SQLite write
# throughput plateaus around 50-100k row batches; below that you pay
# transaction overhead, above it you risk WAL bloat. 50k is the sweet
# spot in informal benchmarking on the test bench.
BATCH_SIZE = 50_000


def _import_init_db():
    """Import collector.init_db lazily so this module can be inspected
    (e.g. by --help) without a fully configured Aerodrome environment.

    The collector module imports a lot of runtime config — it's the
    main package, not a library. We add the project root to sys.path
    so that `import collector` resolves regardless of where the user
    runs the backfill from.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(here, "..", ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    import collector
    return collector.init_db


# ---------------------------------------------------------------------
# Generation

def _generate_aircraft_pool(
    target_count: int,
    home_lat: float,
    home_lon: float,
    military_fraction: float,
    seed: int,
) -> List[Aircraft]:
    """Build a pool of N distinct aircraft. Each has a stable identity
    (hex, callsign, registration, type) — backfill sightings draw from
    this pool, with each draw representing one observation of that
    aircraft in some hour of the time window. Fleet helper is reused
    here purely for its _spawn() factory; we don't need the full
    visibility / range model for backfill."""
    fleet = Fleet(
        size=0,  # we don't want pre-seeded visible aircraft
        home_lat=home_lat,
        home_lon=home_lon,
        military_fraction=military_fraction,
        seed=seed,
    )
    pool: List[Aircraft] = []
    seen_hex: set = set()
    # Burn through a few collisions if any (unlikely at any reasonable
    # pool size given 16M hex space, but defensive)
    while len(pool) < target_count:
        ac = fleet._spawn()
        if ac.hex in seen_hex:
            continue
        seen_hex.add(ac.hex)
        pool.append(ac)
    return pool


# ---------------------------------------------------------------------
# Pass-based generation (v1.2)
#
# Real ADS-B data is structured around aircraft *passes* — periods when
# one aircraft is visible to the receiver. During a pass, the collector
# captures one sighting every poll interval (typically ~3 seconds), so
# a 5-minute pass produces ~100 sightings, all in the same hour bucket
# (or two adjacent buckets if the pass straddles an hour boundary).
# Between passes, an aircraft is absent for hours or days.
#
# The earlier v1.0/1.1 generator drew each sighting independently with
# uniform random timestamp, which scattered an aircraft's sightings
# uniformly across the entire window — producing on the order of 1
# sighting per (icao, hour) bucket. Real installs see ~60. The
# difference matters: sightings_hourly bucket counts drive the cost
# of detail-page rollup queries, and a 27× bucket-count inflation
# (5.6M synthetic vs 209k real, observed) made synthetic
# query timings unrepresentative.
#
# Pass generation runs in two phases:
#
#   Phase A — coverage. Iterate through the aircraft pool in shuffled
#     order, emit one realistic pass per aircraft. Guarantees every
#     aircraft contributes sightings, even ones that would lose the
#     Zipf lottery. Cost: ~16% of the row budget at loaded-install scale,
#     and that 16% is realistic — real ADS-B has a long tail of
#     transients each appearing once.
#
#   Phase B — distribution shape. Fill remaining row budget with
#     Zipf-weighted extra passes. Heavy fliers accumulate many extras,
#     middle-pack get a few, tail aircraft get none beyond their
#     Phase A pass. Loops until rows_emitted == target — exact row
#     count is guaranteed via tail-truncation of the final pass.
#
# Zipf exponent: 0.4. Calibrated against an observed real-install ratio of
# heaviest:average ≈ 30:1 (his heaviest aircraft has 14,737 sightings
# in 12.6M total ÷ 26k aircraft = 485 average). Earlier 0.7 would
# have peaked far higher (heaviest gets ~170k sightings, 350× average).

# Model parameters. Tuned to land within ~10% of the observed real-install
# 60 sightings/(icao, hour) ratio for the default --match-loaded
# preset.

_POLL_INTERVAL_S = 3.0
_PASS_DURATION_LN_MU = math.log(180)   # ln(3 minutes in seconds)
_PASS_DURATION_LN_SIGMA = 0.7
_ZIPF_EXPONENT = 0.4

# Diurnal weighting. Hour-of-day weights peak at noon-ish and trough
# at 3-4am. These are relative weights for picking pass start hours;
# normalised internally.
_DIURNAL_WEIGHTS = [
    0.40, 0.30, 0.25, 0.25, 0.30, 0.45,  # 0-5 (overnight trough)
    0.65, 0.85, 1.05, 1.20, 1.30, 1.35,  # 6-11 (morning ramp)
    1.40, 1.40, 1.35, 1.30, 1.25, 1.20,  # 12-17 (afternoon plateau)
    1.10, 0.95, 0.80, 0.65, 0.55, 0.45,  # 18-23 (evening decline)
]


def _pick_diurnal_offset(rng: random.Random, window_seconds: int) -> int:
    """Pick a random offset into the window, weighted toward daytime
    hours. Returns offset in seconds from window start."""
    n_days = max(1, window_seconds // 86400)
    hour = rng.choices(range(24), weights=_DIURNAL_WEIGHTS, k=1)[0]
    day = rng.randint(0, n_days - 1)
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    offset = day * 86400 + hour * 3600 + minute * 60 + second
    return min(offset, window_seconds - 1)


def _emit_one_pass(
    rng: random.Random,
    pool: List[Aircraft],
    ac_idx: int,
    window_start_ts: int,
    window_end_ts: int,
    max_rows: int,
) -> Iterable[Tuple]:
    """Emit one pass worth of sightings for the given aircraft.

    `max_rows` caps the number of sightings yielded. The caller passes
    `total_rows - rows_emitted` so the final pass tail-truncates rather
    than overshooting the target row count.

    Per-pass state: each pass picks fresh altitude/speed/squawk so the
    same aircraft observed across multiple passes shows variation
    (different flights, different days). Within a pass, jitter is small
    (one specific flight in progress)."""
    ac = pool[ac_idx]

    pass_alt = max(500, min(43000,
        ac.altitude + rng.randint(-5000, 5000)))
    pass_spd = max(60, min(560,
        ac.speed + rng.gauss(0, 40)))
    pass_squawk = (
        f"{rng.randint(1, 7777):04d}" if rng.random() < 0.3
        else ac.squawk
    )

    duration_s = min(
        rng.lognormvariate(_PASS_DURATION_LN_MU, _PASS_DURATION_LN_SIGMA),
        1800.0,  # cap at 30 min; longer passes are unrealistic
    )
    n_sightings = max(1, int(duration_s / _POLL_INTERVAL_S))
    n_sightings = min(n_sightings, max_rows)

    window_seconds = window_end_ts - window_start_ts
    start_ts = window_start_ts + _pick_diurnal_offset(rng, window_seconds)

    for i in range(n_sightings):
        seen_at = int(start_ts + i * _POLL_INTERVAL_S)
        if seen_at >= window_end_ts:
            break

        # Within-pass jitter: small variations representing real-time
        # ADS-B observation noise + slow flight dynamics.
        alt = pass_alt + rng.randint(-300, 300)
        spd = max(60.0, pass_spd + rng.gauss(0, 8))
        lat = ac.lat + rng.gauss(0, 0.02)
        lon = ac.lon + rng.gauss(0, 0.02)

        yield (
            ac.hex.upper(),
            ac.flight.strip(),
            spd,
            lat, lon,
            alt,
            ac.type_code,
            ac.type_desc,
            seen_at,
            pass_squawk,
        )


def _yield_sightings(
    pool: List[Aircraft],
    total_rows: int,
    window_start_ts: int,
    window_end_ts: int,
    seed: int,
) -> Iterable[Tuple]:
    """Yield exactly `total_rows` sighting tuples, distributed across
    the aircraft pool with realistic shape:

      - Every aircraft contributes at least one realistic pass (Phase A).
      - Remaining row budget is Zipf-weighted across aircraft so heavy
        fliers dominate (Phase B).
      - Total row count is exact — final pass tail-truncates if it
        would overshoot.

    Sightings are NOT sorted chronologically. We drop the all_sightings
    indexes before bulk insert and recreate them after the load (in
    backfill.main), so insert-order doesn't matter for performance —
    the index rebuild scans the table sequentially regardless. Skipping
    a sort saves memory at scale (no need to materialise the full pass
    list)."""
    rng = random.Random(seed)
    n_aircraft = len(pool)
    rows_emitted = 0

    # ---- Phase A: one pass per aircraft, in shuffled order ----
    # Shuffle so output isn't biased to early hours by aircraft index.
    # Each aircraft's pass uses _emit_one_pass, which picks its own
    # diurnal-weighted start time.
    aircraft_order = list(range(n_aircraft))
    rng.shuffle(aircraft_order)
    for ac_idx in aircraft_order:
        if rows_emitted >= total_rows:
            break
        max_rows = total_rows - rows_emitted
        for sighting in _emit_one_pass(
            rng, pool, ac_idx, window_start_ts, window_end_ts, max_rows
        ):
            yield sighting
            rows_emitted += 1
            if rows_emitted >= total_rows:
                break

    # ---- Phase B: Zipf-weighted extras until target row count hit ----
    if rows_emitted >= total_rows:
        return

    weights = [1.0 / ((i + 1) ** _ZIPF_EXPONENT) for i in range(n_aircraft)]
    total_weight = sum(weights)
    # Cumulative weights for bisect-based weighted pick. bisect is
    # O(log n) per pick vs a linear scan O(n) — at 26k aircraft and
    # 138k Phase B passes, the difference is meaningful.
    cum_weights: List[float] = []
    acc = 0.0
    for w in weights:
        acc += w
        cum_weights.append(acc)
    import bisect

    while rows_emitted < total_rows:
        r = rng.uniform(0, total_weight)
        ac_idx = bisect.bisect_left(cum_weights, r)
        if ac_idx >= n_aircraft:
            ac_idx = n_aircraft - 1
        max_rows = total_rows - rows_emitted
        for sighting in _emit_one_pass(
            rng, pool, ac_idx, window_start_ts, window_end_ts, max_rows
        ):
            yield sighting
            rows_emitted += 1
            if rows_emitted >= total_rows:
                break


# ---------------------------------------------------------------------
# Database writers

def _bulk_insert_all_sightings(
    conn: sqlite3.Connection,
    sightings: Iterable[Tuple],
    total_rows: int,
) -> None:
    """Stream sightings into all_sightings in BATCH_SIZE-row commits.
    Logs progress every 10 batches so a 12.6M-row backfill doesn't
    look hung."""
    sql = """
        INSERT INTO all_sightings
        (icao, callsign, speed, lat, lon, altitude, aircraft_type,
         type_desc, seen_at, squawk)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    batch: List[Tuple] = []
    inserted = 0
    batch_count = 0
    t0 = time.time()
    for row in sightings:
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            conn.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
            batch_count += 1
            batch.clear()
            if batch_count % 10 == 0:
                pct = 100.0 * inserted / total_rows
                rate = inserted / max(time.time() - t0, 0.001)
                logger.info(
                    "  all_sightings: %s / %s rows (%.1f%%, %.0f rows/sec)",
                    f"{inserted:,}", f"{total_rows:,}", pct, rate,
                )
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        inserted += len(batch)
    elapsed = time.time() - t0
    logger.info(
        "  all_sightings: done — %s rows in %.1fs (%.0f rows/sec)",
        f"{inserted:,}", elapsed, inserted / max(elapsed, 0.001),
    )


def _populate_sightings_hourly(conn: sqlite3.Connection) -> None:
    """Build sightings_hourly from all_sightings.

    v1.1 rewrite: previously used six correlated subqueries to extract
    the "last value within each hour bucket" semantic — clean to read
    but with O(rows × groups × subqueries) cost. At 12.6M rows on a
    4 GB VM that pushed memory into swap and made backfill take >30
    minutes for what should be an aggregation pass.

    The new approach is two SQL statements:

      1. GROUP BY (icao, hour_bucket) to get all aggregates that don't
         need a "last value": count, min/max altitude, max speed, first
         and last timestamps. Single sequential scan of all_sightings.

      2. JOIN that result against all_sightings on (icao, last_seen_at)
         to fetch the last-row column values (callsign, aircraft_type,
         type_desc, last_lat/lon/altitude/speed, last_squawk). Each
         lookup uses idx_all_seen_icao for an indexed seek.

    Same final shape, dramatically less work. On a 12.6M-row dataset
    this completes in tens of seconds instead of tens of minutes.

    Note: when multiple rows share the same MAX(seen_at) within a
    bucket — possible for synthetic data since we don't guarantee
    unique-per-second timestamps — the JOIN may produce multiple rows
    per bucket. The INSERT...SELECT picks one deterministically via
    GROUP BY on the JOIN result. In practice synthetic timestamps are
    randomly distributed across the window so collisions are rare.
    """
    logger.info("  Building sightings_hourly aggregation...")
    t0 = time.time()
    conn.execute("DELETE FROM sightings_hourly")

    # Step 1: aggregates that don't need a "last value" lookup.
    # Materialised into a temp table because we'll join against it
    # in step 2 and SQLite optimises temp tables well for this shape.
    logger.info("  Step 1/2: GROUP BY pass for counts/extremes...")
    conn.execute("DROP TABLE IF EXISTS _hourly_agg")
    conn.execute("""
        CREATE TEMP TABLE _hourly_agg AS
        SELECT
            icao,
            (seen_at / 3600) * 3600 AS hour_bucket,
            COUNT(*)        AS sighting_count,
            MIN(seen_at)    AS first_seen_at,
            MAX(seen_at)    AS last_seen_at,
            MIN(altitude)   AS min_altitude,
            MAX(altitude)   AS max_altitude,
            MAX(speed)      AS max_speed
        FROM all_sightings
        GROUP BY icao, (seen_at / 3600) * 3600
    """)
    conn.execute("CREATE INDEX _hourly_agg_idx ON _hourly_agg(icao, last_seen_at)")
    n_groups = conn.execute("SELECT COUNT(*) FROM _hourly_agg").fetchone()[0]
    logger.info("    %s hour-bucket groups identified in %.1fs",
                f"{n_groups:,}", time.time() - t0)

    # Step 2: join back to all_sightings on (icao, last_seen_at) to
    # pick up the per-row column values from the most recent row in
    # each bucket. Group by hour_bucket on the result so that even
    # if multiple rows share the same MAX timestamp we collapse to
    # one output row per group (last MIN/MAX is deterministic and
    # the column-value collision case is rare in synthetic data).
    logger.info("  Step 2/2: JOIN to populate last-value columns...")
    t1 = time.time()
    conn.execute("""
        INSERT INTO sightings_hourly (
            icao, hour_bucket, callsign, aircraft_type, type_desc,
            sighting_count, first_seen_at, last_seen_at,
            last_lat, last_lon, last_altitude, last_speed,
            min_altitude, max_altitude, max_speed, last_squawk
        )
        SELECT
            a.icao,
            a.hour_bucket,
            MIN(s.callsign)       AS callsign,
            MIN(s.aircraft_type)  AS aircraft_type,
            MIN(s.type_desc)      AS type_desc,
            a.sighting_count,
            a.first_seen_at,
            a.last_seen_at,
            MIN(s.lat)            AS last_lat,
            MIN(s.lon)            AS last_lon,
            MIN(s.altitude)       AS last_altitude,
            MIN(s.speed)          AS last_speed,
            a.min_altitude,
            a.max_altitude,
            a.max_speed,
            MIN(s.squawk)         AS last_squawk
        FROM _hourly_agg AS a
        JOIN all_sightings AS s
          ON s.icao = a.icao
         AND s.seen_at = a.last_seen_at
        GROUP BY a.icao, a.hour_bucket
    """)
    conn.commit()
    conn.execute("DROP TABLE _hourly_agg")
    n = conn.execute("SELECT COUNT(*) FROM sightings_hourly").fetchone()[0]
    elapsed = time.time() - t0
    logger.info(
        "  sightings_hourly: done — %s rollup rows in %.1fs total "
        "(step 2: %.1fs)",
        f"{n:,}", elapsed, time.time() - t1,
    )


def _populate_seen_aircraft_and_migrate(
    conn: sqlite3.Connection,
    app_version: str,
) -> None:
    """Seed seen_aircraft from all_sightings, then run schema migrations.

    Aerodrome's collector.init_db() already contains a "safety net"
    seen_aircraft backfill that runs INSERT OR IGNORE from all_sightings
    grouped by ICAO. But on a fresh synthetic install init_db runs
    BEFORE we bulk-load all_sightings, so all_sightings is empty at
    that point and the seed inserts nothing. We replay that exact same
    INSERT here, after the bulk load, to get one row per distinct ICAO.

    Then we call apply_schema_migrations, which:
      - ALTERs seen_aircraft to add registration, last_callsign,
        aircraft_type, aircraft_type_desc, operator, country, last_lat,
        last_lon, last_seen_at, sighting_count, fts_dirty
      - backfills those columns from all_sightings, sightings_hourly,
        and country_for_icao() (Python-side hex-prefix lookup)
      - creates idx_seen_country, idx_seen_last, etc.
      - sets up seen_aircraft_fts (FTS5 virtual table) and the
        fts_dirty=1 flag on every row so the next collector cycle
        flushes them all to FTS5

    The migration is what gives a fresh synthetic install Search and
    most of the Stats cards their data — without it those endpoints
    look broken (Search returns no results, top_operators / top_types
    error out on missing columns, etc).

    Why we replay the seed here instead of calling init_db a second
    time: init_db creates the schema and is meant to be idempotent on
    fresh DBs, but calling it on a populated DB risks unintended
    interaction with collector-side state. Replaying the one specific
    INSERT we need keeps the contract narrow.
    """
    # Step 1: seed seen_aircraft. The columns inserted here are
    # exactly the ones init_db's safety-net path uses; the migration
    # below will fill in the rest.
    logger.info("Populating seen_aircraft from all_sightings...")
    t0 = time.time()
    cur = conn.execute("""
        INSERT OR IGNORE INTO seen_aircraft (
            icao, first_seen_at, first_callsign, first_aircraft_type
        )
        SELECT icao, MIN(seen_at), '', ''
        FROM all_sightings
        GROUP BY icao
    """)
    n_inserted = cur.rowcount
    conn.commit()
    logger.info("  seen_aircraft: %s rows inserted in %.1fs",
                f"{n_inserted:,}", time.time() - t0)

    # Step 2: apply schema migrations. This adds columns and runs the
    # v1 migration's backfill — populating registration (none, since
    # hexdb is empty), last_callsign / aircraft_type / aircraft_type_desc
    # / last_lat / last_lon / last_seen_at (from all_sightings), country
    # (via Python country_for_icao lookup), and sighting_count (from
    # sightings_hourly sums).
    #
    # On a 25M-row install the heaviest step (the correlated-subquery
    # UPDATE that pulls last_* from all_sightings) takes 30-90 seconds
    # depending on disk speed. The migration's own logging makes
    # progress visible.
    logger.info("Applying schema migrations to populate full seen_aircraft schema...")
    t0 = time.time()
    try:
        from schema_migrations import apply_schema_migrations
    except ImportError as e:
        logger.warning(
            "Could not import schema_migrations (%s). seen_aircraft will "
            "have only base columns; the live collector will migrate it "
            "on its first run. Search and some Stats cards will be empty "
            "until then.", e
        )
        return
    result = apply_schema_migrations(conn, app_version)
    if result.get("ok"):
        applied = result.get("applied") or []
        if applied:
            logger.info("  Migrations: applied %d in %.1fs (v%s -> v%s)",
                        len(applied), time.time() - t0,
                        result.get("starting_version"),
                        result.get("ending_version"))
        else:
            logger.info("  Migrations: already at current version, nothing to apply")
    else:
        logger.error("Migration failed: %s", result.get("error"))
        # Don't raise — backfill produced a usable raw all_sightings
        # table even if migrations didn't apply. Live collector will
        # retry the migration on next startup.


def _drop_indexes_for_load(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    """Drop user-created indexes on all_sightings before bulk insert.

    Returns the list of dropped indexes (name + CREATE statement) so
    the caller can restore them post-load via _create_indexes_after_load.
    SQLite's PRIMARY KEY index stays; user indexes go. Speeds up
    inserts dramatically on large loads.

    v1.3: previously this function dropped indexes but the recreate step
    used a hardcoded list of three index names. That hardcoded list
    drifted from what collector.init_db actually creates — when
    init_db added idx_all_seen_lat_lon (the covering index for
    range_rose / distance_histogram queries), the backfill silently
    failed to restore it. The Stats endpoint's range_rose query uses
    INDEXED BY on that index name, which raises a hard error rather
    than just falling back to a slower plan, so the entire Stats
    endpoint broke on synthetic-backfilled databases. Now we capture
    the full CREATE statement for each user index before dropping,
    and replay those exact statements after the load. Future schema
    changes that add indexes get handled automatically.
    """
    cur = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name='all_sightings' "
        "  AND name NOT LIKE 'sqlite_autoindex_%'"
    )
    captured: List[Dict[str, str]] = []
    for row in cur.fetchall():
        name, sql = row[0], row[1]
        # `sql` will be None for indexes implicitly created by SQLite
        # (UNIQUE constraints, etc) but we filtered those out above.
        # Belt-and-braces: skip any None we encounter rather than
        # producing a broken capture record.
        if not sql:
            continue
        captured.append({"name": name, "sql": sql})
    for entry in captured:
        conn.execute(f"DROP INDEX IF EXISTS {entry['name']}")
    conn.commit()
    if captured:
        logger.info(
            "  Dropped %d index(es) for fast load: %s",
            len(captured),
            ", ".join(e["name"] for e in captured),
        )
    return captured


def _create_indexes_after_load(
    conn: sqlite3.Connection,
    captured: List[Dict[str, str]],
) -> None:
    """Restore the indexes captured by _drop_indexes_for_load.

    Uses each index's actual CREATE statement (captured pre-drop) so
    the restored set is byte-identical to what init_db / migrations
    produced. No hardcoded list to drift out of sync."""
    if not captured:
        logger.info("  (no indexes to recreate)")
        return
    logger.info("  Recreating %d index(es)...", len(captured))
    t0 = time.time()
    for entry in captured:
        conn.execute(entry["sql"])
    conn.commit()
    logger.info("  indexes: done in %.1fs", time.time() - t0)


# ---------------------------------------------------------------------
# Entry point

def main() -> int:
    p = argparse.ArgumentParser(
        description="Bulk-backfill synthetic ADS-B history (Mode B)."
    )
    p.add_argument("--db", default="./aircraft_history_synthetic.db",
                   help="Target database path (default: "
                        "./aircraft_history_synthetic.db)")
    p.add_argument("--force", action="store_true",
                   help="Overwrite the target DB if it exists "
                        "(default: refuse to overwrite, to avoid "
                        "accidental destruction of test data)")
    p.add_argument("--match-loaded", action="store_true",
                   help="Use the loaded-install preset shape: 12.6M rows, "
                        "18 days, 26,000 unique aircraft")
    p.add_argument("--rows", type=int, default=1_000_000,
                   help="Total sighting rows to generate "
                        "(default: 1,000,000; ignored if --match-loaded)")
    p.add_argument("--days", type=int, default=7,
                   help="Time window length in days, ending now "
                        "(default: 7; ignored if --match-loaded)")
    p.add_argument("--aircraft", type=int, default=2_000,
                   help="Unique aircraft to populate "
                        "(default: 2000; ignored if --match-loaded)")
    p.add_argument("--home-lat", type=float, default=40.0)
    p.add_argument("--home-lon", type=float, default=-75.0)
    p.add_argument("--military-fraction", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed (default: 42, for reproducibility)")
    p.add_argument("--cache-size-mb", type=int, default=64,
                   help="SQLite page cache size in MB during backfill "
                        "(default: 64 — sized to fit comfortably on a "
                        "small VM with 2-4 GB RAM. Bump to 256-512 on a "
                        "workstation for faster loads. Going beyond what "
                        "your VM actually has free will cause swap and "
                        "make backfill DRAMATICALLY slower.)")
    p.add_argument("--scale-factor", type=float, default=1.0,
                   help="Multiplier on row count for stress testing "
                        "beyond the requested scale (default: 1.0). "
                        "Applies to both --match-loaded and --rows. "
                        "Aircraft pool size stays fixed; the extra "
                        "rows go to existing aircraft via the "
                        "Zipf-weighted Phase B. Examples: "
                        "--match-loaded --scale-factor 2.0 → 25.2M "
                        "rows over the same aircraft pool; "
                        "--match-loaded --scale-factor 5.0 → 63M rows.")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.match_loaded:
        rows = int(LOADED_PRESET["rows"] * args.scale_factor)
        days = LOADED_PRESET["days"]
        aircraft = LOADED_PRESET["aircraft"]
    else:
        rows = int(args.rows * args.scale_factor)
        days, aircraft = args.days, args.aircraft

    # Refuse to overwrite without --force. The whole point of separate
    # synthetic DBs is to make accidents impossible.
    if os.path.exists(args.db) and not args.force:
        logger.error(
            "Target DB %s already exists. Pass --force to overwrite, "
            "or pick a different --db path.", args.db,
        )
        return 1
    if os.path.exists(args.db):
        os.remove(args.db)
        logger.info("Removed existing %s (--force)", args.db)

    logger.info(
        "Backfill plan: %s rows over %d days, %s unique aircraft -> %s",
        f"{rows:,}", days, f"{aircraft:,}", args.db,
    )

    # Initialise schema by calling Aerodrome's own init_db. This means
    # we get whatever schema the running version of Aerodrome creates
    # — including any future migrations. No schema duplication.
    init_db = _import_init_db()
    logger.info("Initialising schema via collector.init_db...")
    init_db(args.db)

    conn = sqlite3.connect(args.db)
    # Bulk-insert tuning. WAL is friendlier for concurrent reads but
    # for a one-shot bulk load the default journal is fine. PRAGMA
    # synchronous=OFF plus a big cache is the standard "I am willing
    # to risk corruption on power failure for 5x throughput" combo —
    # acceptable here because if backfill crashes the user just re-runs
    # it on a fresh DB.
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = MEMORY")
    # Cache size in KB (negative value), per the --cache-size-mb flag.
    # Default 64 MB is sized for a small VM. The earlier hardcoded
    # 256 MB caused real-world swap pain on a 4 GB test VM — users
    # with more RAM can opt up explicitly.
    conn.execute(f"PRAGMA cache_size = -{args.cache_size_mb * 1024}")
    conn.execute("PRAGMA temp_store = MEMORY")

    try:
        captured_indexes = _drop_indexes_for_load(conn)

        logger.info("Generating aircraft pool...")
        pool = _generate_aircraft_pool(
            target_count=aircraft,
            home_lat=args.home_lat,
            home_lon=args.home_lon,
            military_fraction=args.military_fraction,
            seed=args.seed,
        )
        logger.info("  pool: %s aircraft (%d military)",
                    f"{len(pool):,}",
                    sum(1 for a in pool if a.is_military))

        now = int(time.time())
        window_start = now - days * 86400
        sightings = _yield_sightings(
            pool=pool,
            total_rows=rows,
            window_start_ts=window_start,
            window_end_ts=now,
            seed=args.seed + 1,  # different seed than pool generation
        )
        logger.info("Loading all_sightings...")
        _bulk_insert_all_sightings(conn, sightings, total_rows=rows)

        _create_indexes_after_load(conn, captured_indexes)
        _populate_sightings_hourly(conn)

        # v2.85.8: populate seen_aircraft and apply schema migrations.
        # Without this step, fresh synthetic installs would have an
        # empty seen_aircraft and pre-migration schema (no country,
        # operator, registration, last_seen_at, etc), making Search
        # return nothing and several Stats cards error out. The live
        # collector eventually fills these via UPSERTs after the
        # backfill, but anyone testing fresh-install behavior or
        # running queries against the synthetic DB before pointing
        # the collector at it would hit empty/broken results.
        _populate_seen_aircraft_and_migrate(
            conn, app_version="synthetic_feeder backfill"
        )

        # ANALYZE so the query planner has good stats. Without this
        # the synthetic DB will look "cold" to SQLite and the
        # diagnostic page would show plans that aren't representative.
        logger.info("Running ANALYZE for accurate query plans...")
        t0 = time.time()
        conn.execute("ANALYZE")
        conn.commit()
        logger.info("  ANALYZE: done in %.1fs", time.time() - t0)

    finally:
        conn.close()

    size_mb = os.path.getsize(args.db) / (1024 * 1024)
    logger.info(
        "Backfill complete. Database size: %.1f MB. Point Aerodrome at "
        "this DB by setting data.db_file in config.yaml to: %s",
        size_mb, os.path.abspath(args.db),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
