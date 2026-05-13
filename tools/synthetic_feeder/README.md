# Synthetic feeder

Maintainer tool for testing Aerodrome at scale without waiting weeks of
real-time accumulation, and without needing a real ADS-B receiver
attached to the test bench.

Two modes, sharing one generator:

- **Mode A** — HTTP server mimicking dump1090/tar1090's
  `/data/aircraft.json` endpoint. Point Aerodrome at this and the Live
  tab populates from synthetic data. For testing collector behaviour,
  watchlist alert paths, the Live tab, and any other code that depends
  on observing live aircraft.
- **Mode B** — bulk-inserts synthetic historical sightings directly
  into a fresh `aircraft_history.db`. For testing query-side surfaces
  (detail page, drill, Stats) at production scale. Reaches loaded-install
  scale (12.6M rows, 18 days, 26k aircraft) in roughly 10–15 minutes
  on commodity hardware.

The generator is hermetic: random valid hex ICAOs, no real
registrations, no outbound network calls during testing. ~5% of
generated aircraft fall in the US military hex range
(`AE0000`–`AFFFFF`) so the watchlist/military classifier paths get
exercised at a realistic fraction.

## Quickstart — interactive menu

If you don't want to remember flags, run the menu:

```bash
python3 -m tools.synthetic_feeder.menu
```

It prompts for the choices that matter and runs Mode A or Mode B for
you. Useful defaults at every prompt — press Enter to accept.
Backfill presets cover the common cases (tiny smoke test, quick
test, match loaded install, custom). The menu launches each mode as a
subprocess so Ctrl-C in a running server returns you to the menu
instead of killing everything.

For automation or when you know exactly what you want, invoke
`serve.py` / `backfill.py` directly with their own flags as below.

## Mode A — synthetic feeder server

Start the server (defaults to port 8080, 100 visible aircraft, receiver
at 40N 75W with 250 km coverage):

```bash
python3 -m tools.synthetic_feeder.serve
```

Then update the test system's `config.yaml`:

```yaml
receiver:
  ip: 127.0.0.1
  port: 8080
  path: /data/aircraft.json
```

Restart Aerodrome. The Live tab will populate from the synthetic feed
within one poll cycle.

### Useful flags

```
--port N                Bind port (default 8080)
--visible N             Aircraft simultaneously visible (default 100)
--home-lat F            Receiver latitude (default 40.0)
--home-lon F            Receiver longitude (default -75.0)
--range KM              Receiver coverage radius in km (default 250)
--military-fraction F   Fraction in military hex range (default 0.05)
--tick-interval S       Fleet position update period (default 1.0)
--seed N                Random seed for reproducible runs
```

### What ramping up looks like

A real busy receiver shows 30–50 aircraft visible. To stress-test
beyond what a real install would see:

```bash
# Heavy load — 500 visible aircraft, faster motion
python3 -m tools.synthetic_feeder.serve --visible 500 --tick-interval 0.5
```

Aerodrome's collector polls on its own schedule (configured in
`config.yaml`). The feeder's tick interval is independent — fleet
state advances at the tick rate regardless of poll rate. Set tick
interval shorter than the poll interval if you want each poll to see
substantial motion; equal or longer is fine if you just want the feed
to look stable.

## Mode B — bulk historical backfill

Generate a synthetic database at a realistically-loaded install's production scale:

```bash
python3 -m tools.synthetic_feeder.backfill --match-loaded
```

This produces `./aircraft_history_synthetic.db` with 12.6M sightings
over 18 days across 26,000 unique aircraft. Then point a test instance
of Aerodrome at it via `data.db_file` in `config.yaml`, restart, and
the detail page / drill / Stats endpoints will run against the
synthetic data at full loaded-install scale.

### Custom shapes

```bash
# Smaller test: 1M rows over 7 days, 10k aircraft
python3 -m tools.synthetic_feeder.backfill --rows 1000000 --days 7 --aircraft 10000

# Different output path
python3 -m tools.synthetic_feeder.backfill --db /tmp/scenario_a.db
```

### Useful flags

```
--db PATH               Target DB path (default ./aircraft_history_synthetic.db)
--force                 Overwrite existing DB (default: refuse)
--match-loaded         Use the loaded-install preset (12.6M / 18d / 26k)
--rows N                Total sighting rows (default 1,000,000)
--days N                Time window length in days, ending now (default 7)
--aircraft N            Unique aircraft to populate (default 2,000)
--home-lat F            Receiver latitude for spawned aircraft positions
--home-lon F            Receiver longitude for spawned aircraft positions
--military-fraction F   Fraction in military hex range (default 0.05)
--seed N                Random seed (default 42, reproducible)
```

### How it works

1. Calls `collector.init_db()` to create the schema. Same schema
   Aerodrome would create itself — including all migrations through
   the current release. No schema duplication; future schema changes
   automatically apply.
2. Drops user indexes on `all_sightings` for fast bulk insert.
3. Generates an aircraft pool with stable identity per aircraft.
4. Streams sightings in 50,000-row batches with Zipf-weighted
   selection (heavy fliers dominate, matching real receiver data).
5. Recreates indexes after the bulk load.
6. Populates `sightings_hourly` from `all_sightings` via SQL
   aggregation (single GROUP BY — much faster than per-row UPSERT).
7. Runs `ANALYZE` so the query planner has accurate stats.

### Sighting distribution

Aircraft are weighted with a Zipf-ish distribution (`weight ∝ 1/(i+1)^0.7`).
Result: the heaviest aircraft accumulates roughly 50× the sightings of
median, ~500× the sightings of lightest. This matches the
heavy-tail pattern visible in real installs — local-area heavy fliers
dominate the table, occasional transients appear once or twice.

### Performance characteristics

On commodity hardware (Apple M1, NVMe SSD):

| Scale | all_sightings load | sightings_hourly | ANALYZE | Total |
|---|---|---|---|---|
| 50k rows | <1s | 7s | <1s | ~8s |
| 1M rows | 15s | 2-3 min | 5s | ~3 min |
| 12.6M rows (--match-loaded) | 3 min | 8-12 min | 30s | ~12 min |

Most of the time at large scale is the sightings_hourly aggregation —
it runs correlated subqueries to extract the last-non-empty values per
hour bucket. Acceptable at the tool's intended scale; if it becomes a
bottleneck the SQL can be rewritten as a window-function or
GROUP-BY-then-JOIN-to-MAX shape for a substantial speedup.

## Configuration tips

### Avoiding the negative-cache spam

The hexdb resolution path will log a warning every time it can't
resolve a synthetic aircraft. To avoid log noise during synthetic
testing, either:

1. Set `hexdb.enabled: false` in the test instance's `config.yaml`,
   or
2. Tolerate the noise — synthetic ICAOs all hit the negative cache
   after the first lookup, so the warning is one-shot per aircraft.

### Never run against the production DB

Both modes are designed to make it impossible to accidentally pollute
your real test install's database:

- **Mode B** writes to `./aircraft_history_synthetic.db` by default
  and refuses to overwrite an existing file without `--force`.
- **Mode A** doesn't touch any database at all — it only serves HTTP
  and the test Aerodrome instance writes to whatever DB its config
  points at.

If you want both running simultaneously (Mode A serving live, Mode B
having pre-loaded historical data into the test instance's DB), point
the test Aerodrome at the synthetic DB via `data.db_file` and at the
synthetic feeder via `receiver.{ip,port,path}`. Both work
independently.

## Files

- `generator.py` — Aircraft state machine + Fleet manager. Shared by
  both modes.
- `serve.py` — Mode A HTTP server. Stdlib `http.server`, no
  third-party deps.
- `backfill.py` — Mode B bulk loader. Imports `collector.init_db` for
  schema; everything else is local.
- `menu.py` — Interactive wrapper around both modes. Convenience for
  ad-hoc test sessions; not used for automation.
- `__init__.py` — package marker.

## Limitations

- Generated ICAOs don't resolve via hexdb (they're random). Tail
  resolution and registration enrichment paths exercise their
  negative-cache code paths but never their positive-resolution code
  paths. If you need to test positive resolution, bring your own real
  ICAOs.
- Data shape is "indistinguishable from real" only at the JSON level.
  The motion model is a random walk with reasonable speed/altitude
  bounds — it's not actually doing flight planning. Aircraft don't
  follow great-circle routes between airports because there are no
  airports.
- Mode B assumes the Aerodrome version installed is the one whose
  schema you want. If you upgrade Aerodrome and want the synthetic
  DB to match, regenerate it.
