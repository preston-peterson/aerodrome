# Search architecture (v2.51.0 design)

This document is the planning artifact for v2.51.0's headline feature:
search across the deep archive. It is NOT a shipped feature yet — it is
the design we agreed on before any code was written. If implementation
diverges from this document, the document is wrong; update it.

## Context

Today's Stats and (in v2.51.0's era, before v2.67.0) the All tab were
bounded by `retention.all_days` (default 30). Once data falls out of
`all_sightings`, it's pruned. Users with a 6-month-old install can't
ask "have I ever seen aircraft N12345?" or "what military aircraft
flew through last summer?" because the raw data is gone.

`sightings_hourly` is already retained forever (no time-based DELETE
anywhere in the codebase) — so the deep archive functionally exists as
of v2.50.0. What's missing is a UI that surfaces it. The original
"deep archive" feature filing was about that gap.

In conversation we agreed the centerpiece feature should be search:
a Spotlight-style single-box query interface that hits the entire
history of the install. Other features (lifetime composition cards,
trend charts, time-machine views) are filed as natural follow-ups in
v2.51.x patches but are not v2.51.0 scope.

## Goals

- Single search input that classifies tokens and routes to the right
  index. User types what they remember, the system figures out what
  they mean.
- Sub-50ms search latency on installs with 5+ years of accumulated
  unique-aircraft data (target: ~200k rows in `seen_aircraft`).
- No new processes. No new network dependencies. No new operational
  surface area beyond what SQLite already provides.
- Schema migrations are idempotent, atomic, and reversible.
- No regression in collector write throughput beyond ~10%, measured.

## Non-goals (v2.51.0)

- Lifetime composition cards (filed for v2.51.x).
- Trend charts / monthly views (filed for v2.51.x).
- Time-machine / arbitrary-past-day drill (filed for v2.51.x).
- Cmd+K keyboard shortcut (filed as later UX polish).
- Search across position data with sub-hour granularity (intentionally
  out of scope — hourly granularity is sufficient for old data, per
  user agreement).
- Multi-receiver federated search (no current users with this need).

## Architecture

### Storage tool: SQLite + FTS5 + denormalization on `seen_aircraft`

Decision rationale (recorded here so it doesn't get re-debated later):

**Why not Postgres?** Single-writer system. Adding a separate database
process inflates operational complexity (separate backup story,
permission model, upgrade path) for marginal benefit at our scale.
The single-file SQLite model is one of the project's defining
strengths — `cp aircraft_history.db backup.db` is the entire backup
story.

**Why not specialized search engines (Elasticsearch, Meilisearch,
Typesense)?** Wildly over-engineered for ~21k-1M rows. Specialized
engines justify themselves at 10M+ documents with QPS we won't see.
They require an additional process the user has to install, configure,
and keep alive.

**Why not DuckDB?** Tempting for analytical queries (trend charts in
v2.51.x), but adopting it now means either running two databases
side-by-side or rewriting every existing query. Benefit doesn't
justify cost at v2.51.0. If trend-chart performance becomes a
constraint later, DuckDB-as-secondary remains an option.

**Why SQLite + FTS5?** FTS5 is bundled with default SQLite builds on
every platform we care about (Debian/Ubuntu/Pi OS/macOS/Windows). It's
been in the standard distribution since SQLite 3.20 (2017). It's a
proper inverted-index full-text search with BM25 ranking, maintained
incrementally via triggers. Same database, same backup story, same
single-file model.

### The critical design insight

**Search filters hit `seen_aircraft`, not `sightings_hourly`.**

`seen_aircraft` has one row per unique ICAO ever observed. Pi user's
install has ~21k rows after 12 days. After 5 years on a busy install,
estimate ~200k rows. The unique-aircraft count grows asymptotically —
most aircraft a receiver will ever see, it's already seen — so
`seen_aircraft` plateaus naturally.

Search performance is therefore bounded by `seen_aircraft` size, which
has a clear scaling envelope:

| `seen_aircraft` size | Query cost  | UX |
|---------------------|-------------|--------|
| 21k (today)         | <5ms        | Instant |
| 200k (5+ years)     | <20ms       | Instant |
| 1M (10+ years extreme) | 50-100ms | Good |
| 10M+                | TBD — re-architect at that point | — |

By contrast, naive search against `sightings_hourly` (~1M rows after
a year on a busy install) would scan tens of millions of rows over
time. The hourly rollup is the wrong table to filter on.

**Result rows are self-contained — no joins.**

The collector denormalizes "everything a result row needs to display"
onto `seen_aircraft` as it observes new data. This means a search
query is a single-table SELECT with indexes, no JOINs, no application-
side merging. Result row carries: ICAO, registration, last callsign,
aircraft type, type description, operator, country, last position,
last-seen timestamp, sighting count.

Cost: small write-time tax (UPSERT on every insert). Benefit:
read-time queries are flat.

## Schema changes

New columns on `seen_aircraft`:

```sql
ALTER TABLE seen_aircraft ADD COLUMN registration TEXT;
ALTER TABLE seen_aircraft ADD COLUMN last_callsign TEXT;
ALTER TABLE seen_aircraft ADD COLUMN aircraft_type_desc TEXT;
ALTER TABLE seen_aircraft ADD COLUMN operator TEXT;
ALTER TABLE seen_aircraft ADD COLUMN country TEXT;
ALTER TABLE seen_aircraft ADD COLUMN last_lat REAL;
ALTER TABLE seen_aircraft ADD COLUMN last_lon REAL;
ALTER TABLE seen_aircraft ADD COLUMN last_seen_at INTEGER;
ALTER TABLE seen_aircraft ADD COLUMN sighting_count INTEGER NOT NULL DEFAULT 0;
```

New indexes:

```sql
CREATE INDEX idx_seen_registration ON seen_aircraft(registration);
CREATE INDEX idx_seen_callsign ON seen_aircraft(last_callsign);
CREATE INDEX idx_seen_type ON seen_aircraft(aircraft_type);
CREATE INDEX idx_seen_country ON seen_aircraft(country);
-- existing idx_seen_first preserved as is
```

New FTS5 virtual table for free-text fields:

```sql
CREATE VIRTUAL TABLE seen_aircraft_fts USING fts5(
    icao UNINDEXED,
    registration,
    last_callsign,
    aircraft_type,
    aircraft_type_desc,
    operator,
    country,
    content='seen_aircraft',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 1'
);
```

Triggers to keep FTS5 in sync (standard pattern):

```sql
CREATE TRIGGER seen_aircraft_ai AFTER INSERT ON seen_aircraft BEGIN
    INSERT INTO seen_aircraft_fts(rowid, icao, registration, last_callsign,
                                   aircraft_type, aircraft_type_desc, operator, country)
    VALUES (new.rowid, new.icao, new.registration, new.last_callsign,
            new.aircraft_type, new.aircraft_type_desc, new.operator, new.country);
END;

CREATE TRIGGER seen_aircraft_ad AFTER DELETE ON seen_aircraft BEGIN
    INSERT INTO seen_aircraft_fts(seen_aircraft_fts, rowid, icao, ...)
    VALUES ('delete', old.rowid, old.icao, ...);
END;

CREATE TRIGGER seen_aircraft_au AFTER UPDATE ON seen_aircraft BEGIN
    INSERT INTO seen_aircraft_fts(seen_aircraft_fts, rowid, icao, ...)
    VALUES ('delete', old.rowid, old.icao, ...);
    INSERT INTO seen_aircraft_fts(rowid, icao, ...)
    VALUES (new.rowid, new.icao, ...);
END;
```

Estimated additional storage: ~15-25 MB on a Pi-user-scale install
(21k rows × ~500 bytes/row indexed × 2 for B-tree overhead + small
FTS5 vocabulary). At 1M rows, ~700 MB-1 GB. Reasonable.

## Migration strategy

**Idempotent, atomic, on-startup:**

1. Detect schema version. New `schema_version` table tracks the v2.51.0
   schema-version stamp. Absence implies v2.50.x or earlier.
2. If schema is already v2.51.0+, skip migration entirely.
3. Begin transaction.
4. ALTER TABLE adds for new columns (NULL-defaulted so existing rows
   are valid).
5. CREATE INDEX statements (CREATE INDEX IF NOT EXISTS for idempotency).
6. CREATE VIRTUAL TABLE for FTS5.
7. CREATE TRIGGER statements.
8. Backfill: walk existing `seen_aircraft` rows, populate new columns
   from data we can derive (`country` via `countries.country_for_icao()`,
   `last_callsign` via `MAX(callsign) FROM all_sightings WHERE icao=?`,
   etc.). For rows where data isn't derivable, leave NULL — collector
   will populate as the aircraft is next seen.
9. Populate FTS5 from the now-backfilled rows: `INSERT INTO
   seen_aircraft_fts(seen_aircraft_fts) VALUES('rebuild');`
10. Update `schema_version` table.
11. Commit transaction. If anything fails, rollback leaves DB in v2.50.x
    state, no partial migration.

**Backfill scaling:** at 21k rows, full backfill completes in seconds.
At 200k rows, maybe 30-60 seconds. The backfill blocks startup, which
is acceptable because (a) it runs once per install ever, and (b)
showing a "migrating database, please wait" log line is honest.

If backfill needs to be made non-blocking later (e.g., user complains
that a 1M-row install takes 5 minutes to start), we can move it to a
background thread that the rest of the app waits on for any FTS5
queries until done. But not v2.51.0 scope.

## Collector changes

On every `INSERT INTO all_sightings`, the collector also UPSERTs the
matching `seen_aircraft` row:

```sql
INSERT INTO seen_aircraft (icao, first_seen_at, first_callsign, first_aircraft_type,
                           last_callsign, aircraft_type, aircraft_type_desc, operator,
                           country, last_lat, last_lon, last_seen_at, registration,
                           sighting_count)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(icao) DO UPDATE SET
    last_callsign = excluded.last_callsign,
    last_lat = excluded.last_lat,
    last_lon = excluded.last_lon,
    last_seen_at = excluded.last_seen_at,
    aircraft_type_desc = COALESCE(excluded.aircraft_type_desc, aircraft_type_desc),
    operator = COALESCE(excluded.operator, operator),
    sighting_count = sighting_count + 1;
```

This single statement does both insert-if-new and update-if-existing.
Cost: one extra row write per collector cycle.

**Measurement plan after Phase 1:** run a before/after benchmark on
both reference installs. Compare collector cycle latency at p50 and
p95. If regression exceeds 10% on either install, redesign before
Phase 2 (likely options: batch the UPSERTs, or use a smaller derived
table).

## Search query parser

User input is tokenized on whitespace. Each token classifies into
one of these patterns, in priority order:

| Token pattern              | Field         | Match type |
|---------------------------|---------------|------------|
| `[0-9A-F]{6}` (case-ins.) | ICAO hex      | Exact      |
| `7500` / `7600` / `7700`  | Squawk        | Exact      |
| `2025-MM-DD`, `2025-MM`, `YYYY` | Date         | Range      |
| Aircraft type code (matches `designators.py`) | Aircraft type | Exact      |
| Country name (matches `countries.py`) | Country | Exact      |
| Tail registration (e.g., `N12345`, `G-XYZA`) | Registration | Exact      |
| 3-letter prefix + digits  | Callsign      | Exact      |
| 3-letter prefix only      | Callsign      | Prefix     |
| Anything else             | Free text     | FTS5 MATCH |

A query string `B738 Canada last week` produces three filters:
`aircraft_type = 'B738'`, `country = 'Canada'`, `last_seen_at >=
(now - 7 days)`. AND-ed together.

A query string with ambiguous tokens uses OR within that token's
candidates. E.g., `A320` matches both "type A320" and "starts with
'A320' as registration prefix"; the parser emits both candidates as
OR conditions, ranking handles which is more relevant.

**Free-text search uses FTS5 directly:**
```sql
WHERE rowid IN (SELECT rowid FROM seen_aircraft_fts WHERE seen_aircraft_fts MATCH ?)
```
FTS5 handles tokenization and ranking via BM25.

## Ranking

Search results are scored:

```
score = 0
score += 1000 if exact_field_match else 0
score += min(sighting_count, 100)
score += 50 if last_seen_within(30 days) else 0
score += bm25_score (FTS5 only, scaled to fit ~100-200 range)
```

Tunable. Tested empirically against both reference installs once
search is wired up. If results "feel wrong," scoring constants are
the first thing to tune.

ORDER BY score DESC, then last_seen_at DESC as tiebreaker.

## API shape

```
GET /api/search?q=<query>&limit=50&offset=0&sort=relevance
```

Returns:
```json
{
    "ok": true,
    "query": "B738 Canada",
    "parsed_filters": [
        {"field": "aircraft_type", "match": "exact", "value": "B738"},
        {"field": "country", "match": "exact", "value": "Canada"}
    ],
    "total_count": 47,
    "rows": [
        {
            "icao": "C01FBA",
            "registration": "C-FCRA",
            "last_callsign": "ACA847",
            "aircraft_type": "B738",
            "aircraft_type_desc": "Boeing 737-800",
            "operator": "Air Canada",
            "country": "Canada",
            "last_lat": 49.2,
            "last_lon": -123.1,
            "last_seen_at": 1745945823,
            "sighting_count": 142,
            "score": 1187
        }
    ]
}
```

```
GET /api/search/aircraft/{icao}
```

Per-aircraft detail: full sighting history from `sightings_hourly`,
peak altitude/speed observed, callsigns used, country, link templates
to external track services.

## UI

New top-level nav: `Live | Watchlist | Military | Stats | All | Search | …`

Search page shape:
- Search input, prominent, focused on page load.
- Below input: parsed-filter chips appear as you type (Phase 4).
- Results list — cards with operator, tail, ICAO, type, last position
  (lat/lon as small monospace strip), sighting count, last-seen time.
- Click a result → existing aircraft-detail flow (`/aircraft/{icao}`),
  enhanced with lifetime view in Phase 3.
- URL state — `/search?q=...` so links are shareable, browser back/forward
  works, deep-linking from external sites works.

Empty state: brief explanation + 3-4 example queries.

## Phase breakdown

Each phase ships as a single patch. Phases 1-4 collectively constitute
v2.51.0 once Phase 4 is in.

### Phase 1 (v2.51.0-alpha-equivalent — schema migration)
- New columns, indexes, FTS5 table, triggers.
- Migration script with rollback.
- Collector UPSERT pattern.
- Backfill from existing data.
- **Measurement pass:** collector cycle latency before/after on both
  reference installs. Decision gate: proceed only if regression < 10%.
- No user-visible change. The infrastructure is invisible until
  Phase 2.

### Phase 2 (search backend)
- `/api/search` endpoint.
- Token classifier and query construction.
- Ranking.
- No UI yet — search is exercisable via curl or browser URL.

### Phase 3 (search UI + nav entry)
- New top-level Search tab.
- Search input, results list with chosen result-row info (operator +
  tail + last lat/lon, per agreed scope).
- Click-through to existing aircraft detail.
- URL state.

### Phase 4 (filter chips)
- Parsed-filter chips appear inline as user types.
- Explicit "Add filter" UI for non-typed filter construction.

## Risks & mitigations

**Risk: collector write tax exceeds 10%.**
Mitigation: measurement gate after Phase 1. If exceeded, batch the
UPSERTs (one per N rows instead of per-row), or use a smaller derived
table that we update less frequently.

**Risk: schema migration fails on a real install.**
Mitigation: test migration against both reference installs before
shipping. Idempotent + transactional design means a failed migration
leaves DB at v2.50.x.

**Risk: FTS5 not available on user's SQLite build.**
Mitigation: detect at startup. If unavailable, log a clear error and
fall back to regular LIKE search (degraded but functional). Document
as a known limitation.

**Risk: search ranking "feels wrong" to users.**
Mitigation: scoring constants are configurable. We expect to tune
once both reference installs report on real-world feel. If genuinely
intractable, fall back to user-pickable sort orders.

**Risk: schema migration takes too long on big installs.**
Mitigation: known scaling envelope. At 21k rows, < 5 sec. At 200k,
< 60 sec. Beyond that, move backfill to background thread —
post-v2.51.0 if we hit it.

**Risk: feature is built but neither user has enough data to test
it meaningfully.**
Mitigation: this is a real concern, named explicitly. Both reference
installs have under a month of data. Search "feels good" testing will
be limited until installs age. Acceptable because: (a) the
architecture's correctness can be validated synthetically, (b) the
performance bounds are computable, (c) value compounds with time.

## Open questions

- **Operator-name source of truth.** Operator is currently derived from
  callsign prefix in scattered places. Search wants a canonical
  per-aircraft operator field. We should consolidate the derivation
  logic into a single function (`operator_for_callsign()` or similar)
  before Phase 1 schema work.
- **What about aircraft we resolved via hexdb vs. inferred via
  callsign?** Need a deterministic precedence — hexdb results win
  over callsign-derived guesses. Filed for Phase 1 implementation.
- **FTS5 trigger overhead — have we measured?** Standard FTS5
  triggers add a small per-write cost. Bundled into the Phase 1
  measurement gate.

## Cross-references

- v2.50.27: country lookup table (`countries.py`) — already in place,
  search will reuse for token classification.
- v2.50.30: capacity card; the new schema doesn't change retention
  semantics, but adds ~15-25 MB to typical install size which the
  capacity projection should pick up automatically (via measured
  bytes/row).
- v2.50.13: SQLite tuning profiles. FTS5 + new indexes increase
  benefit from larger cache_size; tuning profiles should remain
  appropriate but worth validating after Phase 1.

## Status

**Phase 1 — schema infrastructure — COMPLETE in v2.50.33.**

Outcome notes (not in original plan):

- **Reframed performance gate.** Original gate said "regression < 10%" against
  collector cycle latency. Real-install measurement showed +38ms / +214%
  on this maintainer's 7,748-aircraft install (17.8ms → 55.9ms). The
  10% framing didn't survive — but at the absolute scale (~38ms per
  60s poll cycle, ~0.06% wall-clock load) the impact is acceptable.
  Reframed as "absolute latency budget" with explicit decision to
  accept the regression, documented in CHANGELOG of v2.50.33.
- **Inline FTS5 triggers were rejected after benchmarking.** Initial
  design called for AFTER INSERT/UPDATE/DELETE triggers on
  seen_aircraft to maintain seen_aircraft_fts. Bench showed this
  caused a ~32× regression (vs the ~3× of the dirty-flag approach we
  ended up with). Filed in code comments — if anyone considers
  re-introducing inline triggers, run the test_schema_migrations
  benchmark first.
- **Dirty-flag (Flavor C) is what we shipped.** Hot path sets
  `fts_dirty=1` only when FTS-indexed fields change. End-of-cycle
  batch flush propagates to FTS5. Steady-state real-data dirty rate
  is 3% — exactly the rate of new ICAOs introduced per cycle.
- **Operator column populated by collector deferred to Phase 1.5.**
  Operator derivation from callsign prefix lives in scattered places
  in server.py today; lifting into a shared module is its own small
  refactor. Search results show no operator until that ships.

**Phase 2 — search backend `/api/search` endpoint — COMPLETE in v2.50.34.**

- `search.py` extracted as a standalone module; `server.py` is glue.
- 35 tests in `test_search.py` cover parser branches, executor SQL,
  ranking, hostile input, pagination, detail endpoint.
- Real-DB validation: 0.4-3.3ms typical query latency on 7,748-aircraft
  install. 44 hits for "B738 Canada", 6380 hits for "United States".
- One real bug found and fixed during integration: the SQL builder
  needed `seen_aircraft.` qualifiers on column references because
  the FTS5 join introduces ambiguity on dual-resident column names
  (registration, last_callsign, aircraft_type, country). Tests added
  to cover the integration shape, not just isolated parser/executor.
- Operator and squawk filters unwired — both require denormalization
  the schema doesn't have yet. Filed.

**Phase 3 — search UI tab — next concrete work item.**

Phase 3 (UI) and Phase 4 (filter chips) follow. v2.51.0 is minted only
when the user-visible feature ships in Phase 3.
