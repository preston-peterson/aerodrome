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
| `main.py` | CLI entrypoint, process lifecycle, collector thread driver | ~630 lines |
| `collector.py` | Poll/normalize/store/notify pipeline, SQLite schema | ~2,750 lines |
| `server.py` | FastAPI routes, API handlers, template rendering | ~9,500 lines |
| `notifier.py` | Notification formatting, rate limiting, ntfy delivery | ~840 lines |
| `config_validator.py` | Runtime config schema validation | ~900 lines |
| `schema_migrations.py` | Versioned schema migrations (rollup, concurrent-minute, daily-track, update-state) | ~1,380 lines |
| `designators.py` | ICAO aircraft type + airline lookup tables | ~400 lines |
| `ntfy_installer.py` | Self-hosted ntfy server install/upgrade helper; validates and renders its `server.yml` | ~1,300 lines |
| `updater.py` | In-app update: staged apply, test-load, switch-in, and the boot-time guard that rolls a release back (stdlib only; imports no app module) | ~1,100 lines |
| `atomic_io.py` | Crash-safe small-file writes: temp file + fsync + rename + directory fsync, disk-full detection (stdlib only) | ~100 lines |
| `templates/*.html` | 11 admin pages — vanilla HTML/CSS/JS, no framework | ~13,500 lines |
| `static/theme.css`, `static/theme.js` | Shared theme system across all templates | ~660 lines |

The split is deliberate:

- **collector.py** handles everything data-plane: talking to the receiver, normalizing the response, detecting military aircraft, matching watchlist entries, writing rows, pruning old rows, enqueueing unknown ICAOs for tail resolution, and firing notification events. It has no HTTP handlers and no knowledge of the web UI.
- **server.py** handles everything control-plane: read-only API calls that turn SQLite rows into JSON, plus write API calls for config edits and updates. It also owns three small background schedulers — the tail-resolve worker, the daily-summary scheduler, and the GitHub update-check scheduler — all of which are server-side concerns (HTTP egress to external APIs, scheduled notification dispatch) rather than collector concerns. It reaches into `collector` for shared utility functions (haversine math, military detection logic).
- **main.py** is the only place where both sides are wired together. It spins up the collector thread in `run_collector()`, lets `server.py` start its own scheduler threads at import time, and hands the FastAPI app to uvicorn in `start()`.

The consequence is that each module can be read in isolation. You don't need to understand the web layer to understand how data gets collected, and vice versa.

## Startup sequence

What `python3 main.py start` does, in order (v3.4.147):

```
main.py import   update guard (before any third-party import): updater.on_boot()
                   - only for `start`, and only when no other Aerodrome owns the install
                   - an interrupted update (or rollback) → finish it or put the
                     previous version back
                   - a journal for a version that isn't installed (an older release
                     without the guard was applied over it) → set aside, nothing undone
                   - the first starts of a new release are counted; the third start
                     after two that never confirmed puts the previous version back
                     (unless its copy in .update-old-<ts>/ is gone: nothing to put back)
                 import yaml, uvicorn, collector, server
main()           restart = stop, then exec a fresh `main.py start`
start()          another live Aerodrome on this install (identity-checked) → exit 1
                 init_db(): creates the schema, then the one-time rollup backfills,
                   synchronously (migrations v1, v5 and v16 read those rollups)
                 gate: a backfill didn't finish AND migrations are pending → exit 1,
                   nothing changed (the next start retries)
                 apply_schema_migrations()  (BEGIN IMMEDIATE, 60 s busy timeout)
                 collector thread, get_app(), bind pre-flight
                 update-confirm thread, only while an update is pending: waits for
                   GET /api/version to answer with the new version, then commits
                   the update 30 s later (a restart the new version's UI asks for
                   after that first answer commits it first)
                 uvicorn.run
```

`updater.py` is boot-critical: `main.py` imports it before anything else, inside a block that can never stop startup. It is stdlib-only and imports no other Aerodrome module, so it loads from any half-switched tree.

## The threading model

Four long-running background threads, each with a single responsibility:

**1. The collector** (started by `main.py`, lives in `collector.py`):

1. Wakes up every `poll_interval` seconds (default 60)
2. Fetches JSON from the receiver URL
3. Normalizes the aircraft list
4. Detects military + watchlist matches
5. Writes rows to the three sightings tables
6. Updates "first seen today" counters
7. Fires notification events where applicable
8. Sleeps for the remaining interval

**2. Tail-resolve worker** (started in `server.py`): pulls ICAOs off an in-process queue and resolves them against the hexdb.io API at ~2 req/sec, populating the `hexdb_cache` table. Rate-limited deliberately — the API is free and has no published limit, but respect is cheaper than apology.

**3. Daily summary scheduler** (started in `server.py`): wakes once per day at the configured local-time hour, builds the daily summary, and fires the `daily_summary` ntfy event. Configurable via `notifications.events.daily_summary` and `notifications.daily_summary_hour`.

**4. Update check scheduler** (v3.0.0; started in `server.py`): wakes on the configured cadence (daily/weekly/monthly/never per `updates.github.poll_interval`), queries the GitHub Releases API for the latest tag, writes the discovery result to the `update_state` table, and fires the `update_available` ntfy event on transitions. Page loads of `/updates` read cached state from `update_state` and never hit GitHub directly.

All four threads are daemons (process exit doesn't wait for them). The FastAPI server runs in the foreground on uvicorn. Its endpoints are mostly `async def` but do blocking SQLite reads inside — FastAPI's thread pool handles this. The trade-off is fine for a personal-scale project: SQLite read latency on a warm page cache is sub-millisecond, and the WAL mode means reads never block writes.

On demo-mode installs (v3.1.0), a *separate process* — `aerodrome-synthetic-feeder.service` — runs alongside `aerodrome.service`. It binds to `127.0.0.1:8080` and serves a synthetic `/data/aircraft.json`. From the collector's perspective there's no difference between this and a real ADS-B receiver; the data-plane stays oblivious to demo mode. See *Demo mode* below.

Shared state between threads is SQLite and **nothing else** (with one small exception: the tail-resolve worker has an in-memory queue of pending ICAOs that the collector enqueues into). There is no in-process queue for the dashboard, no global dict of "current aircraft," no shared memory. Every API call hits the database.

## The data model

Thirteen SQLite tables, defined across `collector.py::init_db()` and the versioned migrations in `schema_migrations.py`:

**Core sightings tables:**
- **`all_sightings`** — every aircraft ever seen, one row per (ICAO, minute) tuple. The primary time-series table.
- **`military_sightings`** — subset of `all_sightings` where `is_military()` returned true. Columns mirror the base table plus a `special_label` for the detected category (transport, fighter, etc).
- **`watchlist_sightings`** — subset where the aircraft matched a user-configured watchlist entry. Adds a `watchlist_label` column.

**Per-aircraft summary:**
- **`seen_aircraft`** — per-ICAO summary row: first-seen timestamp, last-seen timestamp, callsign, type, registration, country, etc. Used for "today's new aircraft" counts and the aircraft detail page. Has a paired `seen_aircraft_fts` FTS5 virtual table for full-text search.

**Aggregations and analytics:**
- **`sightings_hourly`** — pre-computed hourly rollup of `all_sightings`, populated incrementally by the collector. Most Stats-tab queries hit this instead of the raw sightings table.
- **`aircraft_track_daily`** — per-(ICAO, day) track summaries: durations, sighting counts, best-record markers. Powers the aircraft detail page's daily sighting cards.
- **`concurrent_minute`** — per-minute aircraft counts for concurrency analysis (busiest minute / hour stats).

**Notifications and external data:**
- **`stats_records`** — all-time records (farthest sighting, fastest, highest, etc). One row per record type.
- **`hexdb_cache`** — cached aircraft metadata from hexdb.io lookups (tail, registration, type details).
- **`hexdb_events`** — log of hexdb resolver events (resolved, negative-cached, errored) used for health monitoring.
- **`update_state`** — single-row table tracking the GitHub-based update channel's discovery state (latest tag seen, last poll, last apply result). Added v3.0.0.

**Framework / migration:**
- **`_aerodrome_meta`** — pre-v2.50.x migration markers. Legacy; kept for backward compatibility.
- **`schema_version`** — v2.50.x+ migration framework state. Every migration step bumps the version and records its name and timestamp.

Indexes are built in `init_db()` (which runs `CREATE INDEX IF NOT EXISTS` on every start) and the relevant migration files. The workhorse is `idx_all_seen_icao` on `all_sightings(seen_at, icao)`: Stats card and drill reads of a time window — usually today's slice — seek it and can serve `COUNT(DISTINCT icao)` / `GROUP BY icao` from the index alone. Without `sqlite_stat4`, SQLite guesses that a one-sided `seen_at >= ?` keeps about a quarter of the table, and for a `GROUP BY icao` it may walk all of `idx_all_icao` instead — seconds to minutes on a busy install. Reads of that shape therefore pin the index with `INDEXED BY idx_all_seen_icao` (v2.42.7 for the card faces, v3.4.143 for the drill panels); `test_stats_drills.py` checks the drills' plans under several planner-statistics profiles.

**Retention** is configurable per table (`retention.all_days`, `.military_days`, `.watchlist_days` in config.yaml) and is enforced by `cleanup_old_data()` which the collector calls on each poll. There is no background garbage collector; old rows are deleted inline with the next write.

**WAL mode** is enabled at startup. This matters because otherwise reads and writes would serialize and the dashboard would stutter during high-traffic polls.

## Adding a feature

Because the layers are cleanly split, most features follow one of four shapes:

1. **New data collected** — change `normalize()` in collector.py, add the column to the relevant CREATE TABLE in `collector.py::init_db()` for fresh installs, add a versioned migration in `schema_migrations.py` for existing installs, update the stats queries that should surface it.
2. **New tab / dashboard view** — add a FastAPI endpoint in server.py that returns the data, add the tab markup and fetch logic to `templates/index.html`.
3. **New admin page** — add a route in server.py, add a new template file in `templates/`, copy the gear-menu HTML block from any existing admin template (e.g. `templates/status.html` is a good reference). The gear-menu HTML is duplicated across admin pages rather than templated; keeping the markup verbatim across pages is the convention so admin templates each stand on their own.
4. **New notification type** — add an event definition in notifier.py's `Notifier` class, call `_safe_notify()` from wherever the event is detected in collector.py.

Features that don't fit one of these shapes usually indicate a missing abstraction. Before inventing a new module, look at whether the existing split wants to be extended.

## What isn't here

Listing these is as useful as listing what is:

- **No database ORM.** All queries are raw SQL with `sqlite3.Row` for cursor access. The schema is small enough that an ORM adds more syntax than it saves.
- **No frontend framework.** Vanilla JS, no React/Vue/Svelte/etc. Each admin template is ~4,000 lines of hand-written HTML + CSS + JS that stands on its own.
- **No build step for frontend.** Templates are served as-is. No bundler, no transpiler, no minifier. The cost is that you'll see some repetition between templates; the savings is that you can edit a page and reload the browser.
- **No comprehensive integration test suite.** Targeted stdlib-`unittest` files at the repo root (`test_*.py`, run with `python3 -m unittest` from the repo root) cover high-leverage logic — aircraft categorization, type/operator decoding, the schema migration framework and individual migrations, startup config preflight, the search query grammar, session-aware track stitching, collector poll robustness, Stats queries, web hardening and so on — plus subprocess suites that run the release scripts (`bump-version.sh`, `package-release.sh`, `publish-release.sh`), the installer-consistency gate, and the installers themselves (`install.sh`, `scripts/bootstrap.sh`, `uninstall.sh`, `scripts/recover-sudoers.sh` — under stub `sudo`/`systemctl`/package managers and a fake root) against throwaway copies of the working tree (`AERODROME_SKIP_SLOW_TESTS=1` skips those for fast iteration). End-to-end coverage of the web UI is still manual + the `bump-version.sh` release gates + `scripts/check_docs.py` drift detection. Full integration coverage is on the roadmap.
- **No authentication.** Aerodrome is designed to run on your home LAN. If you expose it to the internet, put it behind Tailscale or Cloudflare Tunnel — don't add a login page to a personal tracker.
- **No message queue.** The notification pipeline is synchronous with the collector poll. If ntfy is down, the notification attempts fail and are logged; there's no retry queue. This is an intentional simplification.

## What the codebase is careful about

A few invariants worth knowing because violating them breaks things in non-obvious ways:

- **Nothing in the collector thread makes long blocking calls.** The poll loop has a budget — it must finish before the next interval tick. If you add a network call, it gets a timeout.
- **No new config keys without updating `config_validator.py` and `config.yaml.example`.** The validator runs at startup and refuses to boot with an unknown or malformed config key. It is strict on purpose. See `CONTRIBUTING.md` for the doc-update rules.
- **Template changes need matching screenshot updates under some rules.** See CONTRIBUTING.md.
- **The schema migration path is versioned.** New schema changes for existing installs go through `schema_migrations.py`, which runs versioned `_migration_NN_*` functions in order against the `schema_version` table. Each migration step must be idempotent (safe to re-run) and forward-only (never drop a column; add a new one). `init_db()` in `collector.py` handles fresh installs by creating the current schema directly. Together they must be safe to run against any prior database version and a fresh empty file. Pre-v2.50.x changes were handled inline in `init_db()` via `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN` inside try/except — that path is preserved for the migrations already there, but new schema changes go through the versioned framework.
- **Rollup backfills run before migrations, synchronously, and a failed one blocks them.** `init_db()` finishes the one-time rollup backfills before it returns; `main.start()` refuses to migrate (exit 1, nothing changed) while a backfill flag is unset and migrations are pending, because migrations v1, v5 and v16 compute from those rollups and nothing recomputes them afterwards.
- **One maintenance operation at a time.** Every endpoint that rewrites the install or the database (update upload/apply/download, restores, server-side backup create/delete, the pre-restore purge, config import/restore, switch-to-real) holds `server._MAINT`. It is try-only (refuses with 409, never waits), kept for a couple of minutes after a restart is requested, and freed at once if the restart call fails.
- **Maintenance handlers are plain `def`, and nothing awaits a plain function.** A blocking `async def` handler freezes every other request. The few `async def` route handlers are an explicit allowlist in `test_request_limits_offload.py`, which also fails on `await <plain function>(…)` (v3.4.51 shipped one and broke every GitHub apply).
- **`updater.py` imports no Aerodrome module, and the update guard stays above every third-party import in `main.py`.** Static tests enforce both.
- **N-1 compatibility: whatever a release changes must still let the release before it run.** Automatic rollback puts the previous release back on the same database and install, so: every schema migration leaves the database readable and writable by the previous release; an install-level change (systemd unit, sudoers rule, a config key rename, an EnvironmentFile) keeps the old one working for one release; Python requirements only rise to versions the previous release still imports and runs with (the venv is shared: a rollback, and a refused update, both leave the new packages installed — the apply reports `previous_loads: false` when the running version no longer loads with them); and the update journal (`.update-pending.json`) and `.update-last-result.json` only ever gain fields — their state set {swapping, pending_first_boot, rolling_back, committing} is frozen, and a newer updater reads every older journal (`test_update_apply.py` keeps frozen copies of them; add one, never edit one). Note that the rollback of a release is done by that release's OWN `updater.py` (it is swapped in with the release).

## Demo mode (v3.1.0)

A small but architecturally distinct mode for users who don't have a real ADS-B receiver yet. Two design choices make it worth understanding:

**The synthetic feeder is a separate process, not in-process.** `tools/synthetic_feeder/` bundles a tiny zero-dependency HTTP server (`serve.py`) that holds a deterministic fleet (`generator.py`) and serves `/data/aircraft.json` on a local port. On demo installs, `install.sh --demo` writes a second systemd unit (`aerodrome-synthetic-feeder.service`) that runs alongside `aerodrome.service`. The main collector polls `127.0.0.1:8080` exactly as it would poll a real receiver — it has no idea the data is synthetic. This was deliberate: keeping the collector ignorant of demo mode means no demo-specific branches in the data-plane code, no dual code paths to maintain, and the entire receiver-to-dashboard pipeline gets exercised end-to-end on demo installs the same way it does on real ones.

**Demo state is gated by a single config flag, surfaced in three places.** `demo.enabled` (top-level, default false) drives: (1) the persistent yellow banner on every page, injected centrally by `static/theme.js`, (2) the `[DEMO]` prefix on outgoing notifications, applied at the top of `Notifier.notify()`, and (3) the external-link guard that intercepts "Track ↗" clicks. All three read the flag via `/api/status.demo_enabled` (or directly from CONFIG on the server side), so flipping the flag in the switch-to-real wizard takes effect within one status-poll cycle (~30s) without a service restart. The fleet seed is locked to `1903` so every demo install everywhere sees the same simulated aircraft, and a small starter watchlist is seeded at install time from the same generator to ensure watchlist hits actually trigger during a demo session.

The switch-to-real wizard at `/setup/switch-to-real` handles the destructive transition: tests reachability of the user's real receiver first (refuses to proceed on typo'd IPs), then stops + disables + removes the feeder service, nukes the demo database (synthetic sightings polluting all-time stats forever is the failure mode this avoids), clears the demo watchlist, updates `config.yaml`, and restarts aerodrome. The route lives at its own URL rather than inside `/config` so the browser back button does the right thing during a multi-step destructive flow.

## Multi-distro support (v3.2.0)

Aerodrome's install scripts (`bootstrap.sh`, `install.sh`) recognize four package-manager families and translate between them: apt-get for Debian-family, dnf for Fedora/RHEL, pacman for Arch, zypper for openSUSE. Detection runs against `/etc/os-release` with `ID` first (specific) then `ID_LIKE` (derivative fallback), and a translation table handles per-family package-name differences — most notably that `python3-venv` is a separate package only on Debian-family, since Fedora/Arch/openSUSE bundle the venv module into their main `python3`/`python` package. When neither `ID` nor `ID_LIKE` is recognised, the family is inferred from whichever of `apt-get`/`dnf`/`pacman`/`zypper` is installed and flagged `PKG_FAMILY_GUESSED`, so the bootstrap still warns (or needs `--force`) but prerequisite installs work. `install.sh` skips the package manager entirely when Python (with `venv`/`ensurepip`) and `curl` are already present.

The detection + install functions (`pkg_detect`, `pkg_install`, `pkg_refresh`) are *duplicated* in two scripts — `scripts/bootstrap.sh` and `install.sh` — rather than sourced from a shared library. This is deliberate: the curl-installed `bootstrap.sh` runs standalone from `/tmp` before any release zip exists, so it has no way to source from a shared file. When updating the detection logic, update both copies: `pkg_detect` must stay byte-identical, and `scripts/check_installers.py` — a required gate in every `bump-version.sh` run — refuses a release whose two copies differ by a single byte. (The two `pkg_install` copies differ only in their unknown-family error message.) The duplication is small (~70 lines) and stable — it's not the kind of code that needs frequent changes.

The data plane (collector, server, notifier, classifier) is entirely distro-agnostic; only the install surface knows about the package manager. Inside the systemd unit, every distro looks identical from Aerodrome's perspective: a Python process running under a non-root user with network access.

## Privileges: one owner of the sudoers rules (v3.4.145)

The service runs as a non-root account (the account that installed it); the few root operations it needs (restart, the local ntfy installer, the demo feeder's lifecycle) are granted by `/etc/sudoers.d/aerodrome`. **`install.sh` is the only owner of those rules**: they live in its `render_sudoers()` function, whose `# SUDOERS_VERSION: N` line is the first marker in the file — the one the updater reads from a *staged* `install.sh` to decide whether an update needs a permissions refresh first. The drop-in is written by `install_sudoers_dropin()`: a dot-named temp file inside `/etc/sudoers.d` (sudo and sudo-rs skip dot files), `visudo -cf`, then a `mv` (atomic rename) — sudo never sees a partial or unchecked file, and a rejected rule leaves the old file as it was.

`install.sh --privileges-only` refreshes only that drop-in for an existing install — no packages, venv, units or restart. It is automatic when `install.sh` finds itself inside an install's `update/` staging folder (the command older Updates pages show), resolves the service account from systemd's effective unit (`systemctl show`, compared on canonical paths — never `SUDO_USER`), and has a **downgrade guard**: it never replaces a live drop-in whose marker is higher than its own (exit 3) unless `--allow-privilege-downgrade` is passed; a full install in the same situation skips just the sudoers step. A fresh install runs the service as the account running `install.sh`, or — run as root — the account that typed `sudo` (`SUDO_USER`); a non-root run ignores `SUDO_USER`, which `sudo -iu <account>` leaves set to the admin. A full re-run keeps the account the existing unit runs as and refuses a non-sudo run by a different account.

`scripts/recover-sudoers.sh` carries no rules: it is a hand-off that picks, among its own tree, `<install>/update/`, `<install>/update/aerodrome/` and `<install>/`, the `install.sh` with the highest first marker that supports `--privileges-only`, and runs it with `--install-dir`. That is the command the Updates page shows (`server._sudoers_recovery_command` for a staged release, `_live_recovery_command` for the installed one), so, from v3.4.145 on, the prompt rendered by an older installed release still lifts a newer staged release's gate (the v3.4.140–144 upload modal and the GitHub card below v3.4.44 show `sudo bash <install>/install.sh`, which doesn't; `docs/INSTALL.md` gives the command to run instead). `scripts/check_installers.py`'s `recover-delegates` check keeps it that way (no rule line or marker in the shim, which hands off on a real code line; the first marker is the first line of `render_sudoers()`'s heredoc).

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
