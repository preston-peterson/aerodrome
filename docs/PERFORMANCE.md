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

Go to **⚙ gear menu → Performance**. The page runs a read-only
diagnostic against your actual database and reports:

- Database size and per-table row counts
- Oldest and newest sightings in each table (retention span)
- Timings for the queries the UI runs
- `EXPLAIN QUERY PLAN` for each, so index usage is visible
- Sequential disk read throughput (how fast your storage is)
- SQLite pragmas and system context

If something is wrong, hints appear at the top. There's a "Copy
diagnostic report" button that produces a plaintext block suitable for
pasting into a GitHub issue or an email — we ask for that rather than
guessing when someone reports slowness.

## Expected row counts by activity

Row volume is driven by two things: how many aircraft your receiver
sees per day, and how often they show up in your poll results (the
default collector polls every 60 seconds, so a plane in range for 40
minutes produces ~40 rows).

| Daily aircraft  | Sightings/day   | `all_sightings` after 30d |
| --------------- | --------------- | ------------------------- |
| 100  (rural)    | ~5,000          | ~150,000                  |
| 500  (suburban) | ~30,000         | ~900,000                  |
| 2,000 (busy)    | ~200,000        | ~6,000,000                |
| 5,000+ (major)  | ~1,000,000+     | ~30,000,000+              |

The last row is "near a major airport with a well-placed antenna." This
is where SQLite starts to feel the pressure on SD-card storage.

## Hardware classes and recommendations

Understanding where your hardware sits helps calibrate expectations.

**Pi 3 / Pi Zero 2 / old hardware with SD card.** ARMv7, 1 GB RAM, SD
card writes in the 5–20 MB/s range. Totally fine for watching occasional
traffic; can struggle at > 500 unique aircraft/day with the default 30-day
retention. Recommend shorter `retention.all_days` (7–14 days) or a USB SSD.

**Pi 4 / Pi 5 with SD card.** ARMv8 aarch64, 2–8 GB RAM, SD card writes
maybe 20–40 MB/s. Comfortable up to a few thousand aircraft/day. Start
to feel it past ~5M rows in `all_sightings`. A Class A2 SD card helps
(better random I/O than A1).

**Pi 4 / Pi 5 with USB SSD or eMMC.** This is the sweet spot for the
constrained form factor. Write throughput 10–20× SD, and SQLite is much
happier. Handles even major-airport traffic without needing aggressive
retention tuning.

**x86 mini PC / NUC / laptop.** NVMe or SATA SSD. Not constrained in
any meaningful way for Aerodrome's workload.

## Retention settings (the biggest lever)

`config.yaml` has three retention keys, one per data table:

```yaml
retention:
  watchlist_days: 365   # Watchlist tab — usually sparse
  military_days: 180    # Military tab — moderate volume
  all_days: 30          # All-sightings table — everything, highest volume
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

## Storage recommendations

**If you're on an SD card and seeing slowness, a USB SSD is the single
most effective upgrade.** A $25 USB 3 SSD boot drive for a Pi 4 outperforms
any SD card you can buy. The Pi itself doesn't need to be replaced.

SD card endurance is also a long-term concern for a 24/7 writer like
this. After a year or two of running, consumer SD cards can degrade to
the point where write latency becomes erratic (a symptom that looks
exactly like "it randomly gets slow"). The diagnostic's I/O baseline
measurement can spot this — expect 20+ MB/s from a healthy card; if you
see 5 MB/s, the card is probably wearing out.

## Query performance reference

The Performance page reports timings on representative queries. For
context, here's what to expect:

| Query                    | Good      | Tolerable  | Too slow   |
| ------------------------ | --------- | ---------- | ---------- |
| Live aircraft (last 5m)  | < 10 ms   | < 100 ms   | > 500 ms   |
| Search count             | < 100 ms  | < 500 ms   | > 2,000 ms |
| Search first page        | < 200 ms  | < 1,000 ms | > 5,000 ms |
| Military count           | < 50 ms   | < 500 ms   | > 2,000 ms |

If the first-page query is in the "too slow" bucket on your hardware,
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
