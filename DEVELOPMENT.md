# Development setup

How to actually run Aerodrome locally for development work — without disrupting a production install if you're already running one.

## The short version

```bash
git clone <repo-url> aerodrome-dev
cd aerodrome-dev
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Edit config.yaml — at minimum, set receiver.ip to a real readsb/dump1090 host
python3 main.py start
```

Visit `http://localhost:8000`. That's it — no systemd, no sudo, no install scripts needed for dev work.

## Prerequisites

- **Python 3.10 or newer.** The code uses `str | None` union syntax and `match`/`case`, both 3.10+.
- **An ADS-B receiver reachable on your network.** If you don't have one, see "Running without a real receiver" below.
- **A text editor.** This project has no IDE lock-in — vim, VS Code, whatever works.

## Dependencies

Two requirements files:

- `requirements.txt` — runtime. FastAPI, uvicorn, requests, pyyaml, psutil, ruamel.yaml, python-multipart. This is what `install.sh` uses for production installs.
- `requirements-dev.txt` — documentation tooling. Only needed if you'll rebuild screenshots (`scripts/screenshots.py`) or the Overview PDF (`scripts/build_overview_pdf.py`). Reportlab + Pillow + Playwright.

A venv isn't strictly required on most systems, but Ubuntu/Debian default to a protected system Python these days and `pip install` will refuse without `--break-system-packages`. Use a venv.

## Configuring for dev

The shipped `config.yaml.example` is aimed at a real production install and has some defaults that are annoying during development. For dev, start with the example and then override these:

```yaml
server:
  host: "127.0.0.1"       # don't bind to 0.0.0.0 on a dev machine
  port: 8000
  log_level: "DEBUG"      # INFO is the prod default; DEBUG while developing

data:
  db_file: "./dev.db"     # local path so you don't touch a prod db

receiver:
  ip: "<your receiver>"   # required
  poll_interval: 30       # faster feedback when testing collector changes

retention:
  all_days: 2             # smaller db while iterating
  military_days: 7
  watchlist_days: 30

notifications:
  enabled: false          # don't spam your phone while developing
```

Everything else can stay at the example defaults.

## Running

```bash
source .venv/bin/activate
python3 main.py start
```

`main.py` is the entrypoint for both dev and production. It:

1. Loads `config.yaml` from the current directory
2. Spawns a collector thread (polling your receiver)
3. Starts uvicorn serving FastAPI

**Stopping:** `Ctrl+C` in the terminal, OR `python3 main.py stop` from another terminal (it uses a PID file under the data dir).

**Status:** `python3 main.py status` shows whether the process is running and what config file it loaded.

**Restart:** `python3 main.py restart` — useful when you've changed template code but not Python, since template content is read fresh on every request but Python modules are only loaded at startup.

### When to restart

- **Changed a `.py` file?** Restart required. No auto-reload is wired up. (You can run under `uvicorn --reload` manually if you want it — see below.)
- **Changed a template or `static/*` file?** No restart needed. Reload the browser page.
- **Changed `config.yaml`?** Most config keys are re-read per-poll or per-request. A handful (server host/port, log level) are read once at startup and need a restart. Log output will say `Config reloaded` when a hot-reload path fires successfully.

### uvicorn auto-reload

For rapid iteration on server-side code, skip `main.py` and go straight to uvicorn:

```bash
uvicorn "main:_app_for_reload" --reload --host 127.0.0.1 --port 8000
```

Note this won't run the collector thread, so `/api/live` etc will return empty. Fine for working on endpoint logic in isolation; not fine for testing the polling loop. `_app_for_reload` isn't currently exposed — if you want this workflow, you'll need to add a shim that calls `get_app(load_config(), "config.yaml")` at module top-level. It's on the list.

## Running without a real receiver

If you don't have an ADS-B receiver on hand, the quickest option is a static JSON file served by Python:

```bash
# in a separate terminal
mkdir fake-receiver && cd fake-receiver
cp ../docs/sample-aircraft.json data/aircraft.json  # if one exists, else hand-craft
python3 -m http.server 8081
```

Then in `config.yaml`:

```yaml
receiver:
  ip: "127.0.0.1"
  port: 8081
  path: "/data/aircraft.json"
```

The sample JSON needs to match the structure readsb/dump1090 serves. The schema:

```json
{
  "aircraft": [
    {
      "hex": "a12345",
      "flight": "DAL312  ",
      "alt_baro": 32000,
      "gs": 438,
      "lat": 40.7,
      "lon": -74.0,
      "squawk": "1200",
      "t": "A321",
      "r": "N123DL"
    }
  ],
  "now": 1735862400
}
```

`collector.py::normalize()` is the source of truth for what fields Aerodrome reads; anything missing falls back to None. You can generate plausible-looking fixture data with a few lines of Python and edit the file between polls to test changes.

## Code layout for dev work

See `ARCHITECTURE.md` for the mental model. Short version:

- `collector.py` — data plane. Polling, normalization, storage, notifications.
- `server.py` — control plane. FastAPI endpoints, template rendering.
- `main.py` — wires the two together. CLI entrypoint.
- `templates/*.html` — the 9 admin pages.
- `static/theme.css`, `static/theme.js` — shared theme system.

`server.py` is a big file (~6,300 lines). Jump to a specific endpoint with your editor's symbol search; every route is declared with `@app.get("/api/...")` or `@app.post(...)` and the decorator line is usually within 30 lines of the handler body.

## Testing your changes

There's no test suite (yet). Manual testing is the workflow:

1. Make your change.
2. If you changed Python, restart the service.
3. Exercise the change in the browser at `http://localhost:8000`.
4. Check logs. If you set `log_level: DEBUG` they're noisy but useful.
5. Hit `/api/status` to confirm all subsystems are still healthy — this is a good smoke test after any change to collector or server code.
6. If you changed the UI, open the gear menu → Diagnostics and verify the page loads without JS errors (check browser devtools).

For changes that touch the database layer:

- Delete your local `dev.db` and restart — `init_db()` will create a fresh schema. This is the fastest way to confirm your CREATE TABLE changes are valid.
- For migrations against an existing database, copy an older db to `dev.db` first, then start. The `init_db()` function is designed to be idempotent and forward-migrate safely.

## The lint + doc checker

Before opening a PR, run:

```bash
python3 scripts/check_docs.py
```

This looks for documentation drift (version strings that fell out of sync, missing config keys in example files, etc). Not a substitute for code review but catches the mechanical mistakes that otherwise slip through.

## The tech-debt audit

```bash
python3 scripts/tech_debt_audit.py
```

Static scan for dead Python functions, orphan endpoints, dead JS, stale version comments. Not required to run before PRs, but good to run after a big change to confirm you didn't leave something behind. The report lands at `docs/tech-debt-audit.md` locally — it is not tracked or shipped (findings age fast; run the tool against the code you actually have).

## Screenshots

If your change requires a screenshot update (see CONTRIBUTING.md for the rules), regenerate with:

```bash
pip install -r requirements-dev.txt
python3 -m playwright install chromium
python3 scripts/screenshots.py
```

This runs headless against the templates with synthetic mock data — no live server or real aircraft needed. Output PNGs land in `docs/`.

## Debugging a production install

Sometimes you need to reproduce something that only shows up on your real install. A few paths:

**Pull the live db to your dev machine:**

```bash
scp user@server:/opt/aerodrome/aircraft_history.db ./dev.db
```

Then point your dev config at it. You now have the real data to reproduce Stats-tab issues, query timings, etc.

**Tail the live service log:**

```bash
ssh user@server 'sudo journalctl -u aerodrome -f'
```

**Download the Performance diagnostic report:**

The Status → Diagnostics → Performance page has a "Copy Diagnostic Report" button. The resulting text includes query timings, EXPLAIN QUERY PLAN output, index coverage, and disk-read throughput — the kind of thing you want in a reproduction case for a slow-query bug.

## Versioning + releases

Every change gets a version bump via `bump-version.sh`:

```bash
./bump-version.sh patch "Fixed crash on empty receiver response" --type=fixed
./bump-version.sh minor "Added CSV export for the Search tab" --type=added
./bump-version.sh major "Changed config format — breaking change" --type=changed
```

This updates `VERSION`, rewrites the `<!-- Version: ... -->` markers at the top of every Python and HTML file, and prepends a new entry to `CHANGELOG.md`. On minor/major bumps it also rebuilds `docs/Aerodrome_Overview.pdf` (if reportlab is installed) and surfaces a reminder if the tech-debt audit is stale.

Don't edit `VERSION` by hand — the version-string sync across 20+ files is tedious and easy to get wrong.

## A note on the "big template" pattern

`templates/index.html` is 5,400+ lines. This is intentional:

- It gets served as one request and parses in the browser once. No module-loader round-trips, no bundle-splitting complexity.
- The JS is plain enough that "find in file" is a viable navigation tool.
- All the state lives in a handful of top-level `let` declarations near the top of the script block.

If you're used to React/Vue, this will feel wrong. Resist the urge to split it up. The cost of adding a framework or bundler would dwarf the savings on a codebase this size, and the current structure has shipped 150+ releases without becoming unmanageable.

## Where to ask

Open an issue with the `question` label. If you're adding something substantial, it's usually worth sketching the approach in an issue before writing the code.
