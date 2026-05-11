# HTTP API reference

A reference for the ~60 HTTP endpoints Aerodrome exposes. All paths are relative to the server URL (default `http://localhost:8000`).

**This document is a map, not a spec.** It describes what the endpoints do, what they return, and when you'd use them. For exact request/response shapes, `server.py` is the source of truth — each handler is short enough to read. Line numbers in tables are approximate; use your editor's symbol search for the current location.

## Conventions

- All responses are JSON unless marked otherwise. HTML page routes (`/`, `/status`, etc.) return `text/html`.
- Error responses generally follow `{"error": "description"}` with a 4xx/5xx status. Some endpoints return `{"ok": false, "error": "..."}` with a 200 status when the error is operational rather than protocol-level — this is legacy and inconsistent.
- Endpoints under `/api/` are intended for programmatic access. Endpoints without the `/api/` prefix are either HTML pages or backwards-compat URLs.
- There is **no authentication**. Aerodrome is designed for trusted LAN deployment. Do not expose these endpoints to the public internet without putting them behind something like Tailscale or Cloudflare Tunnel.

## Page routes

Plain HTML responses served by Jinja templates. The frontend-heavy logic lives in these templates; the Python handlers just render them with whatever context the template needs.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Main dashboard — Live, Watchlist, Military, Stats, Search tabs |
| GET | `/status` | System health check — subsystem cards, resource usage |
| GET | `/config` | Configuration editor |
| GET | `/updates` | Release upload + apply + rollback |
| GET | `/documentation` | In-app documentation viewer |
| GET | `/logs` | Service log tail with search + severity filter |
| GET | `/performance` | Performance diagnostic page |
| GET | `/diagnostics` | Subsystem diagnostics index |
| GET | `/diagnostics/watchlist-alerts` | Watchlist alert behavior diagnostic |

## Aircraft data

Core endpoints the dashboard's tabs consume. All return a list of aircraft objects with fields like `icao`, `callsign`, `altitude`, `speed`, `distance`, `seen_at` (Unix timestamp), etc. Exact fields vary by endpoint — see the handlers for the specifics.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/live` | Aircraft currently transmitting (seen within the last minute) |
| GET | `/api/watchlist` | Historical + current watchlist hits |
| GET | `/api/military` | Historical + current military hits |
| GET | `/api/search` | Full-text search across every aircraft (`?q=...&from_ts=...&to_ts=...&limit=...&offset=...`) |
| GET | `/api/all/drill` | Per-aircraft sighting history (ICAO + window). Used by the aircraft detail page. |
| GET | `/api/first-seen` | Per-ICAO first-seen timestamps (for "new today" highlighting) |

`/api/search` is the canonical "browse every aircraft" endpoint — replaces the old `/api/all` (which was removed in v2.67.0 alongside the All tab). It accepts a parsed query string (`q=`) plus optional filters and pagination. See `search.py` for the parser grammar (callsigns, ICAO, types, countries, operators, dates, `today`, `hour:N`, `distance:LO-HI`, `military`, `watchlist`).

## Watchlist management

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/watchlist/entries` | Current watchlist config (ICAO/tail/callsign/model patterns + labels) |
| POST | `/api/watchlist/add` | Add an entry. Body: `{identifier, label}` |
| POST | `/api/watchlist/remove` | Remove an entry. Body: `{identifier}` |
| GET | `/api/watchlist/history/count` | Row count in `watchlist_sightings` — used to confirm the bulk-delete dialog |
| POST | `/api/watchlist/history/clear` | Delete all rows from `watchlist_sightings` |

The watchlist is stored in `config.yaml` under the `watchlist:` key. Changes hot-reload — the collector rebuilds its matcher on the next poll.

## Stats and drill-downs

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/stats` | Today-stats + all-time records for the Stats tab |
| GET | `/api/stats/drill` | Drill into a specific stat card to see the underlying rows |

`/api/stats` is the biggest single endpoint handler in the codebase (several hundred lines). It returns a structured object with groups (`today`, `all_time`, `records`, etc), each containing card definitions. The frontend renders each card based on its type. See `stats.cards.*` config keys to control which cards are shown.

## Tail resolution

Aerodrome uses hexdb.io to map ICAO hex codes to tail numbers (registrations). Results are cached in-process.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/resolve-tail?icaos=A,B,C` | Resolve one or more hex codes to registrations |
| GET | `/api/resolve-tail/debug` | Diagnostic view of the resolver cache (populated + negative entries + TTL) |

The `?icaos=` query string is comma-separated. The response is `{hex: registration, ...}`. Unresolved hexes map to empty string (`""`), which means hexdb returned no entry — not "we haven't looked yet."

## System status

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Comprehensive health check — collector, web server, database, receiver, hexdb resolver, system info |
| GET | `/api/sudoers/status` | Check if /etc/sudoers.d/aerodrome matches the expected version |

`/api/status` is used both by the Status page and by external health-check tooling. The response shape is documented in the comment block above the handler. It's the most stable endpoint in the API — deliberately kept backward-compatible.

## Configuration

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/config` | Current config as JSON |
| PUT | `/api/config` | Replace config (full document) |
| GET | `/api/ui-config` | UI-only subset of config (track link provider, distance unit, etc.) |
| GET | `/api/config/backups` | List automatic config backups (made on every save) |
| GET | `/api/config/backup/{name}` | Fetch a specific backup as YAML text |
| GET | `/api/config/export` | Current config as a downloadable YAML file |
| POST | `/api/config/restore/{name}` | Restore from a backup |
| POST | `/api/config/import` | Import an uploaded YAML config |

The config validator (`config_validator.py`) runs on every `PUT /api/config` and `POST /api/config/import`. A malformed payload returns 4xx with a list of validation errors; the existing config isn't touched.

## Backup / restore

Full disaster-recovery bundles — config + database + notification history. Used by the Backup section of the Config page.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/backup/export` | Download a zip containing config.yaml + aircraft-history.db + notification data |
| GET | `/api/backup/preview` | Inspect an uploaded backup without applying it |
| POST | `/api/backup/import` | Apply an uploaded backup to replace the current install |

Backups are taken live — the SQLite database is copied via SQLite's online backup API so the service doesn't have to pause.

## Performance diagnostic

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/perf/diagnostics` | Run a live benchmark: db size, row counts, pragmas, index coverage, query timings, disk-read throughput, auto-generated hints |
| POST | `/api/perf/analyze` | Run `ANALYZE` on the SQLite database to refresh query planner stats |

`/api/perf/diagnostics` is the most expensive endpoint in the API — it runs six representative queries in sequence and can take several seconds on a large database. Intended for manual invocation from the Performance page, not for polling.

## Notifications

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/notifications/recent?limit=N` | Last N notification attempts (sent + suppressed) |
| GET | `/api/notifications/stats` | Summary counts over 24h / 7d / since-startup |
| GET | `/api/ntfy/logs` | Tail of the ntfy delivery log |
| POST | `/api/notifications/test` | Send a test notification (to configured URL or body-provided URL) |
| POST | `/api/notifications/daily-summary/test` | Generate + send today's daily summary |

## ntfy server management

For users running a self-hosted ntfy server via Aerodrome's installer helper.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ntfy/status` | Is a self-hosted ntfy running? What version? Reachable? |
| POST | `/api/ntfy/install` | Install ntfy to a user-specified directory |
| POST | `/api/ntfy/config` | Update the running ntfy's config.yml |
| POST | `/api/ntfy/upgrade` | Upgrade ntfy to a newer version |
| POST | `/api/ntfy/uninstall` | Remove the self-hosted ntfy install |

These endpoints delegate to `ntfy_installer.py`. Requires sudoers entries — see install.sh.

## Updates

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/updates/local/check` | Check for a staged release in `update/` |
| POST | `/api/updates/local/upload` | Upload a release zip |
| POST | `/api/updates/local/apply` | Apply a staged release (backs up current install, swaps files, restarts service) |
| GET | `/api/updates/github/check` | Placeholder for future GitHub-release checking. Returns `{"available": false, "enabled": false}` — not yet implemented. |
| POST | `/api/restart` | Restart the Aerodrome service |

## Exports

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/export?tab=watchlist` | Download a CSV of sightings. `?tab=` is `military` or `watchlist`. Supports `?from_ts=...&to_ts=...&search=...`. (The `all` tab option was removed in v2.67.0 alongside the All tab; for full sighting history, use `/api/search` and the Search-tab Export ▾ button instead.) |

## Documentation viewer

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/docs/{slug}` | Fetch a markdown document by slug (README, CHANGELOG, CONTRIBUTING, etc.) |
| GET | `/api/changelog` | Parsed CHANGELOG.md as structured JSON |
| GET | `/docs/{filename}` | Serve image assets referenced by in-app markdown docs (screenshots) |

`{filename}` is restricted to names matching `[A-Za-z0-9_-]+\.(png|jpg|jpeg|gif|webp|svg)` — no path traversal, no arbitrary file serving.

## Logs

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/logs/info` | Log file path, size, line count |
| GET | `/api/logs/tail?n=500` | Last N lines (plain text) |
| GET | `/api/logs/download` | Full log as a file download |

## Static assets

| Method | Path | Purpose |
|---|---|---|
| GET | `/static/*` | Shared assets — currently `theme.css` and `theme.js`. Mounted via FastAPI StaticFiles. |

---

## Calling these from outside the dashboard

Most endpoints return JSON and are CORS-unrestricted by default (no `Access-Control-Allow-Origin` header is set). From a script on the same machine:

```bash
curl http://localhost:8000/api/status | jq .
curl http://localhost:8000/api/live | jq '.aircraft | length'
curl "http://localhost:8000/api/export?tab=military&from_ts=$(date -d '1 hour ago' +%s)" > last_hour.csv
```

From a different host on the same LAN, replace `localhost` with the IP of the machine running Aerodrome and ensure `server.host: "0.0.0.0"` in config (default).

## What's not here

- **GraphQL** — no. JSON over HTTP is enough for this project's scale.
- **Webhooks for events** — not implemented. Notifications push to ntfy; if you want to hook other systems, poll `/api/notifications/recent` or subscribe to the same ntfy topic.
- **Write endpoints for sightings data** — no. Aerodrome is a one-way collector; you can't inject fake aircraft through the API. (That would be a separate dev-fixture tool, which isn't built.)
- **Versioning on the API** — the API is unversioned. Changes have been backward-compatible in practice; once Aerodrome is public this may need to become more formal.
