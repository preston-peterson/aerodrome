# Performance on constrained hardware

Aerodrome keeps every aircraft sighting in a SQLite database. On typical
setups (a spare laptop, a cloud VM, a Pi 5 with an SSD), this scales
comfortably to years of data. On a Pi 3 or 4 with an SD card in busy
airspace, the numbers get big enough to matter.

This doc exists because performance problems on constrained hardware
aren't actually mysterious — they follow predictable patterns from the
combination of **row volume**, **random-write speed**, and **query
shape**. Knowing where you sit on each axis tells you whether you need
to change anything and, if so, what.

## Quickstart: run the diagnostic

Go to **⚙ gear menu → Diagnostics → Performance**, then press **Run
diagnostic**. It runs a read-only diagnostic against your actual database
and reports:

- Database size and per-table row counts
- Oldest and newest sightings in each table (retention span)
- Timings for the queries the UI runs
- `EXPLAIN QUERY PLAN` for each, so index usage is visible
- Sequential disk read throughput (how fast your storage is)
- SQLite pragmas and system context

On a busy Raspberry Pi it can take a couple of minutes.

Table sizes are not measured in a normal run: measuring them reads the whole
database and can slow other pages for a few minutes afterwards. Press
**Measure table sizes** when you want them. The **Re-analyze DB** button
runs SQLite's `ANALYZE` to refresh the query planner's statistics; it is the
one thing on this page that writes to the database. You rarely need it:
Aerodrome does this by itself when the database grows or shrinks a lot. On a
big database it takes minutes, and Aerodrome stops recording until it's done.

If something is wrong, hints appear at the top. There's a "Copy
diagnostic report" button that produces a plaintext block suitable for
pasting into a GitHub issue or an email — we ask for that rather than
guessing when someone reports slowness.

## Expected row counts by activity

Row volume is driven by two things: how many aircraft your receiver
hears at once, and how often it polls (every 60 seconds by default, so a
plane in range for 40 minutes produces ~40 rows; at a 15-second poll
interval, ~160). How many different aircraft pass in a day matters much
less. Two real installs that each see about 4,000 aircraft a day:

| Install                                       | Rows a day | `all_sightings` after 30d |
| --------------------------------------------- | ---------- | ------------------------- |
| Home receiver, default poll interval          | ~66,000    | ~2,000,000                |
| Very busy airspace, polling every 15 s        | ~900,000   | ~27,000,000               |

The Status page's **Daily growth** shows your own rows a day.

The second is where SQLite starts to feel the pressure on SD-card
storage.

## Hardware recommendations by activity

- **Quiet** (under 100k rows a day): Raspberry Pi 4 with any SD card.
  Everything is fast.
- **Moderate** (100k-300k rows a day): Raspberry Pi 4 with an SD card
  still works well. The Stats page takes a few seconds to open; a
  USB SSD in place of the SD card makes it quicker.
- **Busy** (over 300k rows a day): Raspberry Pi 4 with 4 GB of memory
  and a USB SSD in place of the SD card, or a faster computer such as a
  Raspberry Pi 5 or a small PC. On an SD card Aerodrome still records
  every aircraft, but the Stats page can take 20 seconds to open, and
  Run diagnostic a couple of minutes.

A Pi 3 or Pi Zero 2 W is slower than a Pi 4: keep less history on one
(see below). On any hardware, a poll interval of 60 seconds stores a
quarter of what 15 seconds does.

## Retention settings (the biggest lever)

`config.yaml` has four retention keys, one per data table. The values below
are examples, not defaults; the shipped defaults are in `config.yaml.example`
(30 days for `all_days`, `military_days` and `watchlist_days`, 365 for
`airport_days`):

```yaml
retention:
  watchlist_days: 365   # Watchlist tab — usually sparse
  military_days: 180    # Military tab — moderate volume
  all_days: 30          # All-sightings table — everything, highest volume
  airport_days: 365     # Takeoff and landing history (airport_events), small
```

`all_days` is the one that controls the `all_sightings` table, which is
by far the largest. Halving it halves the row count. If the Performance
page shows `all_sightings` at tens of millions and queries in the
seconds, reducing `all_days` is the most effective single change.

Shorter retention isn't a downgrade — the Stats tab's "first seen"
records live in the `seen_aircraft` table (never pruned), and all-time
records live in `stats_records` (never pruned). Those survive retention
rotation. What you lose with shorter retention is the ability to
search or browse aircraft history beyond that window via the Search
tab — which queries the `all_sightings` table directly.

Lowering a setting is safe on a big database. Old rows are removed in
short steps, two seconds at a time with a pause between, so collection and
the web UI carry on. Cleanup otherwise runs every 10 minutes. On a busy
Raspberry Pi with an SD card, going from 30 days to 14 takes about 5 hours to
clear, and Aerodrome keeps recording meanwhile.

## Indexes built on the first start after an upgrade

Two indexes make per-aircraft pages and Search's sort by Sightings faster:
`idx_all_icao_seen` on `all_sightings` (it replaces `idx_all_icao`) and
`idx_hourly_icao_sum` on `sightings_hourly`. Aerodrome builds them once, on
the first start of a release that has them:

- The web UI is up during the build. Collection pauses until it's done:
  43 seconds on 12.6 million rows on a small virtual machine, seven on 11
  million on a fast SSD, and about 15 minutes on a busy Raspberry Pi with an
  SD card. The log says when it starts and finishes.
- The database file grows by about 20 bytes per row of `all_sightings`: 258 MB
  on 12.6 million rows. Most of that is the replaced index's old space, now
  free inside the file, and new rows fill it first. The data itself grows by
  about 5 bytes per row.
- They need free space while building, about 70 bytes per row. With less
  than that plus 256 MB free, the build waits for a later start and the log
  says so. Everything still works, more slowly, without them.

## Storage recommendations

**If you're on an SD card and seeing slowness, move to a USB SSD.** On a
Pi 4 the Stats page opens about twice as fast the first time. The Pi itself
doesn't need to be replaced.

SD card endurance is also a long-term concern for a 24/7 writer like
this. After a year or two of running, consumer SD cards can degrade to
the point where write latency becomes erratic (a symptom that looks
exactly like "it randomly gets slow"). The diagnostic's I/O baseline
measurement can spot this — expect 20+ MB/s from a healthy card; if you
see 5 MB/s, the card is probably wearing out.

## Query performance reference

The Performance page times a set of representative queries, each shown under
its own label (for example `unique_aircraft_count_rollup`,
`military_count_rollup`, `watchlist_count_rollup`, `seen_aircraft_total`,
`search_match_bm25`, `aircraft_detail_pk_lookup` and
`aircraft_detail_sightings_page`) with its query plan. The page colours a
timing under 100 ms as fine, 100 ms to 1 s as slow and over 1 s as bad, and
adds a hint at the top for any query over 2 seconds. Other hints appear for a
database over 5 GB, a table over 10 million rows, and disk read throughput
under 15 MB/s.

The numbers below are rough guides for the kinds of query involved, not
limits the page enforces:

| Kind of query                         | Good      | Tolerable  | Too slow   |
| ------------------------------------- | --------- | ---------- | ---------- |
| Military and watchlist counts         | < 50 ms   | < 500 ms   | > 2,000 ms |
| Search (`search_match_bm25`)          | < 200 ms  | < 1,000 ms | > 5,000 ms |

If a search query is in the "too slow" bucket on your hardware,
that's the biggest user-visible contributor to "the UI feels sluggish."
A combination of shorter retention and faster storage usually fixes it.

## If you want to help improve this

Performance feedback from actual deployments is valuable. If you're
running at scale (daily 5,000+ unique aircraft, or `all_sightings` in
the millions), the diagnostic output tells us which queries are slow
on your hardware and where our indexes aren't pulling their weight.

Paste the diagnostic's copy-report output into a GitHub issue, along
with one or two sentences about what you were doing when things felt
slow, and we can usually triage from there.
