# Architecture

A short tour of how Aerodrome is put together, written for someone who's landed on the repo and wants to decide whether it's worth their time to dig in.

Aerodrome is a **single-process Python application** with a background polling thread, a FastAPI web server, and a SQLite database. There is no message queue, no separate worker process, no container orchestration, no cloud dependency. If you can run a Python script on a Raspberry Pi, you can run Aerodrome.

## The big picture

```
┌────────────────────┐       ┌──────────────────┐       ┌─────────────┐
│ ADS-B receiver     │       │   Aerodrome      │       │  Your phone │
│ (readsb/dump1090/  │◀──────│                  │──────▶│  (ntfy app) │
│  tar1090)          │  HTTP │ ┌──────────────┐ │ HTTPS │             │
│                    │       │ │ collector    │ │       └─────────────┘
│ Serves aircraft    │       │ │ thread       │ │
│ JSON at :8080/data │       │ │ (main.py)    │ │       ┌─────────────┐
└────────────────────┘       │ └──────┬───────┘ │       │ Web browser │
                             │        │         │──────▶│ (dashboard) │
                             │        ▼         │ HTTP  │             │
                             │ ┌──────────────┐ │       └─────────────┘
                             │ │  SQLite      │ │
                             │ │  aircraft-   │ │
                             │ │  history.db  │ │
                             │ └──────┬───────┘ │
                             │        │         │
                             │ ┌──────▼───────┐ │
                             │ │  FastAPI     │ │
                             │ │  (server.py) │ │
                             │ └──────────────┘ │
                             └──────────────────┘
```

The collector thread polls your local ADS-B receiver every N seconds (default 60), writes what it sees to SQLite, and fires notifications when interesting aircraft appear. The web server reads that same SQLite database to answer API calls from the dashboard. They share the database and nothing else — no in-memory coordination, no locks beyond what SQLite provides.

## The modules

| File | Role | Size |
|---|---|---|
| `main.py` | CLI entrypoint, process lifecycle, collector thread driver | ~430 lines |
| `collector.py` | Poll/normalize/store/notify pipeline, SQLite schema | ~1,280 lines |
| `server.py` | FastAPI routes, API handlers, template rendering | ~6,300 lines |
| `notifier.py` | Notification formatting, rate limiting, ntfy delivery | ~780 lines |
| `config_validator.py` | Runtime config schema validation | ~700 lines |
| `designators.py` | ICAO aircraft type + airline lookup tables | ~350 lines |
| `ntfy_installer.py` | Self-hosted ntfy server install/upgrade helper | ~1,020 lines |
| `templates/*.html` | 9 admin pages — vanilla HTML/CSS/JS, no framework | ~13,500 lines |
| `static/theme.css`, `static/theme.js` | Shared theme system across all templates | ~150 lines |

The split is deliberate:

- **collector.py** handles everything data-plane: talking to the receiver, normalizing the response, detecting military aircraft, matching watchlist entries, writing rows, pruning old rows, resolving tails via hexdb.io, and firing notification events. It has no HTTP handlers and no knowledge of the web UI.
- **server.py** handles everything control-plane: read-only API calls that turn SQLite rows into JSON, plus write API calls for config edits and updates. It has no polling loop and only reaches into `collector` for shared utility functions (haversine math, military detection logic).
- **main.py** is the only place where both sides are wired together. It spins up the collector thread in `run_collector()` and hands the FastAPI app to uvicorn in `start()`.

The consequence is that each module can be read in isolation. You don't need to understand the web layer to understand how data gets collected, and vice versa.

## The threading model

There is exactly one long-running background thread — the collector. It:

1. Wakes up every `poll_interval` seconds (default 60)
2. Fetches JSON from the receiver URL
3. Normalizes the aircraft list
4. Detects military + watchlist matches
5. Writes rows to the three sightings tables
6. Updates "first seen today" counters
7. Fires notification events where applicable
8. Sleeps for the remaining interval

The FastAPI server runs in the foreground on uvicorn. Its endpoints are mostly `async def` but do blocking SQLite reads inside — FastAPI's thread pool handles this. The trade-off is fine for a personal-scale project: SQLite read latency on a warm page cache is sub-millisecond, and the WAL mode means reads never block writes.

Shared state between the collector and the web server is SQLite and **nothing else**. There is no in-process queue, no global dict of "current aircraft," no shared memory. Every API call hits the database.

## The data model

Seven SQLite tables, all defined in `collector.py::init_db()`:

- **`all_sightings`** — every aircraft ever seen, one row per (ICAO, minute) tuple. The primary time-series table.
- **`military_sightings`** — subset of `all_sightings` where `is_military()` returned true. Columns mirror the base table plus a `special_label` for the detected category (transport, fighter, etc).
- **`watchlist_sightings`** — subset where the aircraft matched a user-configured watchlist entry. Adds a `watchlist_label` column.
- **`seen_aircraft`** — per-ICAO first-seen timestamp, used for "today's new aircraft" counts.
- **`stats_records`** — all-time records (farthest sighting, fastest, highest, etc). One row per record type.
- **`_aerodrome_meta`** — internal migration state, schema version markers.

Indexes are built in the same `init_db()` function. The most-used is `idx_all_seen` on `all_sightings(seen_at)` — almost every Stats-tab query filters by a time window.

**Retention** is configurable per table (`retention.all_days`, `.military_days`, `.watchlist_days` in config.yaml) and is enforced by `cleanup_old_data()` which the collector calls on each poll. There is no background garbage collector; old rows are deleted inline with the next write.

**WAL mode** is enabled at startup. This matters because otherwise reads and writes would serialize and the dashboard would stutter during high-traffic polls.

## Adding a feature

Because the layers are cleanly split, most features follow one of four shapes:

1. **New data collected** — change `normalize()` in collector.py, add the column to the relevant CREATE TABLE, add a migration in `init_db()` to alter existing databases, update the stats queries that should surface it.
2. **New tab / dashboard view** — add a FastAPI endpoint in server.py that returns the data, add the tab markup and fetch logic to `templates/index.html`.
3. **New admin page** — add a route in server.py, add a new template file in `templates/`, register the gear-menu link in every existing admin template (inject_theme_submenu.py is a reference for how).
4. **New notification type** — add an event definition in notifier.py's `Notifier` class, call `_safe_notify()` from wherever the event is detected in collector.py.

Features that don't fit one of these shapes usually indicate a missing abstraction. Before inventing a new module, look at whether the existing split wants to be extended.

## What isn't here

Listing these is as useful as listing what is:

- **No database ORM.** All queries are raw SQL with `sqlite3.Row` for cursor access. The schema is small enough that an ORM adds more syntax than it saves.
- **No frontend framework.** Vanilla JS, no React/Vue/Svelte/etc. Each admin template is ~4,000 lines of hand-written HTML + CSS + JS that stands on its own.
- **No build step for frontend.** Templates are served as-is. No bundler, no transpiler, no minifier. The cost is that you'll see some repetition between templates; the savings is that you can edit a page and reload the browser.
- **No test suite.** Noted in CONTRIBUTING.md. Manual testing + the `bump-version.sh` import-check + the `scripts/check_docs.py` linter cover most regressions. A real test suite is on the road map for a public release.
- **No authentication.** Aerodrome is designed to run on your home LAN. If you expose it to the internet, put it behind Tailscale or Cloudflare Tunnel — don't add a login page to a personal tracker.
- **No message queue.** The notification pipeline is synchronous with the collector poll. If ntfy is down, the notification attempts fail and are logged; there's no retry queue. This is an intentional simplification.

## What the codebase is careful about

A few invariants worth knowing because violating them breaks things in non-obvious ways:

- **Nothing in the collector thread makes long blocking calls.** The poll loop has a budget — it must finish before the next interval tick. If you add a network call, it gets a timeout.
- **No new config keys without updating `config_validator.py` and `config.yaml.example`.** The validator runs at startup and refuses to boot with an unknown or malformed config key. It is strict on purpose. See `CONTRIBUTING.md` for the doc-update rules.
- **Template changes need matching screenshot updates under some rules.** See CONTRIBUTING.md.
- **Tests the schema migration path.** `init_db()` must be safe to call against a v1 database, a v2 database, and a fresh empty file, in that order. Migration steps are `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ADD COLUMN` inside try/except. Never drop a column; add a new one.

## Where the surprises hide

Three places in the code that have caught even the author off-guard and will catch you too:

1. **hexdb.io URL path.** The correct path is `/api/v1/aircraft/{hex}`. An earlier version used `/api/v1/aircraft/icao/{hex}` (mirroring the tail→ICAO resolver), which 404'd every request for four releases because the health-check treated 404 as "service up." Don't change this URL without reading the comment on `resolve_icao_to_tail()`.
2. **Null distances in sort.** The Live tab defaults to distance-ascending when a receiver location is configured. `cmp()` in index.html puts null distances at the *bottom* specifically for the distance column and at the *top* for every other numeric column. This is an intentional asymmetry.
3. **Update flow filename collision.** Historical: `update/README.md` used to collide with the release root `README.md` during staging, causing the Updates tab to render the wrong content. Fixed by renaming the staged file to `update/UPDATE_README.md`. There's a startup self-heal step in `server.py` that removes legacy `update/README.md` files from older installs. If you see that file mentioned in code, it's the self-heal, not a mistake.

## Further reading

- `DEVELOPMENT.md` — how to run this locally for dev work.
- `API.md` — reference for the ~60 HTTP endpoints.
- `CONTRIBUTING.md` — documentation rules, PR workflow, versioning conventions.
- `docs/PERFORMANCE.md` — what the Performance diagnostic page measures and how to act on its output.
