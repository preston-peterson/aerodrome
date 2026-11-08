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
| GET | `/board` | Kiosk display board for wall TVs — layout per screen via `?mode=radar\|board\|hybrid` (no mode: one-time picker; `?mode=pick` re-picks) |
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
| GET | `/api/search` | Full-text search across every aircraft (`?q=...&from_ts=...&to_ts=...&preset=today&limit=...&offset=...`; `preset=today` starts from midnight in Aerodrome's timezone). The response adds `time_label` (the date window in words) and `time_tokens` (the date tokens as typed); with a window active, each row adds `window_sightings` |
| GET | `/api/all/drill` | Per-aircraft sighting history (ICAO + window). Used by the aircraft detail page. A row of a flight FlightAware confirmed (it was looked up) carries `route: {origin, dest, source: "flightaware"}`. |
| GET | `/api/first-seen` | Per-ICAO first-seen timestamps (for "new today" highlighting) |
| GET | `/api/callsign/{callsign}/route` | A flight's route, looked up by callsign. With `lat`/`lon` (and `track`), a route from the free list that doesn't fit where the plane is comes back `found: false, mismatch: true`. With `fa=1` (sent when you open an aircraft: the Live map's panel, the aircraft page, the display board's details) and a FlightAware key set, FlightAware is asked first, spending a lookup unless the route is cached; its answer carries `source: "flightaware"`, `departed_at` and `arrives_at` (Unix timestamps). With `fa=cached` (the display board's rows and cards) a FlightAware route is used only if one was already looked up; it never spends a lookup. |

`/api/search` is the canonical "browse every aircraft" endpoint — replaces the old `/api/all` (which was removed in v2.67.0 alongside the All tab). It accepts a parsed query string (`q=`) plus optional filters and pagination. See `search.py` for the parser grammar (callsigns, ICAO, types, countries, operators, dates, `today`, `hour:N`, `distance:LO-HI`, `military`, `watchlist`).

## Watchlist management

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/watchlist/entries` | Current watchlist config (ICAO/tail/callsign/model patterns + labels). Tail-only entries also carry `tail_status` (`found`, `pending`, `unknown` = hexdb.io has no record, `error` = no usable answer, retried soon) and `tail_icaos` |
| POST | `/api/watchlist/add` | Add an entry. Body: `{identifier, label}` |
| POST | `/api/watchlist/remove` | Remove an entry. Body: `{identifier}` |
| GET | `/api/watchlist/history/count` | Row count in `watchlist_sightings` — used to confirm the bulk-delete dialog |
| POST | `/api/watchlist/history/clear` | Delete all rows from `watchlist_sightings` |

The watchlist is stored in `config.yaml` under the `watchlist:` key. Changes hot-reload — the collector rebuilds its matcher on the next poll. Adding or removing an entry saves `config.yaml` all or nothing, keeping its comments; when the save fails nothing changes (the running watchlist included) and the response is `{ok: false, error, message}` — `507` when the disk is full, `500` otherwise.

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
| GET | `/api/status` | Comprehensive health check — collector, web server, database, receiver, hexdb resolver, notifications, FlightAware (`components.flightaware`, only when a key is set: this month's lookups and budget, the last lookup, FlightAware's usage figure as last fetched; built without calling FlightAware), system info. A FlightAware key FlightAware refused is a warning (`failing` includes `flightaware`) |
| GET | `/api/sudoers/status` | Check if /etc/sudoers.d/aerodrome matches the expected version; every reply carries `recovery_command`, the permissions-refresh command to run on the server. From rules version 6, `live_version` counts only as far as the system helper the rules name (`rules_version`, `helper_version`); with no rules file at all, `can_verify` and `managed` are false and nothing needs doing (also where Aerodrome's account can't look inside `/etc/sudoers.d`, as on Fedora/RHEL, sudo won't show it a rules file and there is no system helper); with one Aerodrome can't read, it asks the helper itself |

`/api/status` is used both by the Status page and by external health-check tooling. The response shape is documented in the comment block above the handler. It's the most stable endpoint in the API — deliberately kept backward-compatible.

`components.notifications` is `{enabled: false, ok: true}` while notifications are off. When they're on it adds `server` (host:port only, never the topic), `reachable`, `response_ms` and `error` from a check of the ntfy server's `/v1/health` (at most every 5 minutes, and at once after the address changes; any answer below 500 counts as reachable), `last_sent_ts`, and `last_failed` (`{ts, event, error}` when the most recent alert that tried to send failed and nothing has gone through since). `ok` is false when the server isn't reachable or the last alert failed; that's an advisory, so `severity` becomes `warn` (never `error`) with `notifications` in `failing`.

## Configuration

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/config` | Current config as JSON. A saved FlightAware key comes back as `__kept__`, never in full; a `PUT` carrying `__kept__` keeps the stored key |
| PUT | `/api/config` | Replace config (full document) |
| GET | `/api/ui-config` | UI-only subset of config (track link provider, distance unit, map prefs including `map.carto_api_key`, etc.) |
| GET | `/api/flightaware/status` | The FlightAware key's state (`key_last4` only, never the key), this month's lookups against `monthly_limit` (`count`; `key_count` is those made with the current key, `null` until known; `earlier_keys` lists this month's earlier keys as `{last4, count}`), whether lookups are paused, and FlightAware's own usage figure for this key (`usage`: `lookups` billed, without the free usage checks, and `total_cost`), with the same for every key on the account in `usage.account`. Free calls, cached 10 minutes, can lag an hour |
| POST | `/api/flightaware/test` | Check the saved FlightAware key with FlightAware's free usage call: `{ok, state: ok\|invalid\|error\|no_key, message}` |
| GET | `/api/config/backups` | List automatic config backups (made on every save) |
| GET | `/api/config/backup/{name}` | Fetch a specific backup as YAML text |
| GET | `/api/config/export` | Current config as a downloadable YAML file |
| POST | `/api/config/restore/{name}` | Restore from a backup |
| POST | `/api/config/import` | Import an uploaded YAML config |

The config validator (`config_validator.py`) runs on every `PUT /api/config`, `POST /api/config/import` and `POST /api/config/restore/{name}`. A malformed payload returns `400` with a list of validation errors (`errors: [{path, message}]`); the existing config isn't touched. Three settings are also checked against **this machine**, only when they change: `web.host` must be one of its addresses (`0.0.0.0` and `::` always are), `web.port` must be free to listen on (not used by another program, not a port the service account may not use; the port the running server already listens on is not re-checked), and the folder of `data.db_file` must exist and be writable — otherwise `400 {ok: false, error, message, errors}` with an `errors` entry for `web.host` / `web.port` / `data.db_file`.

Every write of `config.yaml` is all or nothing: the new text is written to a temporary file beside it, synced, then renamed over it, so a full disk or a crash can't leave it empty or cut short. A failed save changes nothing — neither the file nor the running config — and says why: `PUT /api/config` returns `{ok: false, error, message, errors: [{path: "", message}]}`, import and restore `{ok: false, error, message}`, with `507` when the disk is full and `500` for other write errors. An imported config is written exactly as uploaded (its comments kept); a Settings save keeps the file's comments where it can. `config.yaml` and its `config.yaml.bak.<date>` copies are written readable by the account Aerodrome runs as only. Two layouts are written in place instead of renamed over (free space checked first, the old text put back if the write fails): a `config.yaml` symlinked into a folder the service account can't write, and one owned by another account that the service account may write (owner and mode kept). A `config.yaml` the service account can't write is refused (`500`, nothing changed). None of these saves refreshes `.backups/config.yaml.last-good`: it is the last config that *started*, written by a start that could listen on its address and, with the config that start used, by the step that confirms an update.

## Backup / restore

Full disaster-recovery bundles — config + database + notification history. Used by the Backup section of the Config page.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/backup/export` | Download a zip containing config.yaml + aircraft-history.db + notification data |
| GET | `/api/backup/preview` | Inspect an uploaded backup without applying it |
| POST | `/api/backup/import` | Restore an uploaded backup (checked and staged; switched in at the restart) |
| POST | `/api/backup/restore-from-path` | Restore a backup already on the server, in `.backups/` (`{"path": ..., "keep_current_config": false}`) |
| POST | `/api/backup/staged-restore/cancel` | Cancel a restore that waits for its restart |
| GET | `/api/backup/pre-restore` | The copies kept before each restore, paired by date |
| POST | `/api/backup/pre-restore/{ts}/restore` | Put a pair of pre-restore copies back (a restore of them) |
| POST | `/api/backup/pre-restore/purge` | Delete every pre-restore copy (except the pair a waiting restore needs) |

Backups are taken live — the SQLite database is copied via SQLite's online backup API so the service doesn't have to pause.

**Restoring a backup** (`POST /api/backup/import`, `POST /api/backup/restore-from-path`; v3.4.149) checks the whole backup first and changes nothing live: the backup's `config.yaml` must load the way startup reads it and pass Settings' validation, its database is unpacked beside the live one and checked (`quick_check`, the tables Aerodrome needs, a schema no newer than this release), and copies of the current database (SQLite backup API) and `config.yaml` are kept as `.pre-restore` copies — the database's beside the database, `config.yaml`'s beside `config.yaml`. This machine's `web.host`, `web.port`, `data.db_file` and `logging.dir` are kept, whatever the backup says (each one that differed is listed in `notes`; `notes` also names hostnames the backup's `web.allowed_hosts` or `notifications.public_url` would let the web UI answer to that this machine's settings didn't). A `data.db_file` that is a symbolic link keeps its link: the staged file, the copy and the switch are on the file it points to (database copies a release before 3.4.149 made beside the link are still listed, pruned, purged and put back from there). Then Aerodrome restarts and switches to the restored files **at startup**, before it opens the database. If the restored data doesn't load (the database won't open, it can't be upgraded, or Aerodrome doesn't finish starting twice), the previous data is put back on its own, by moving the pre-restore copies back — so a rolled-back restore's copies are no longer listed. A web address that can't be listened on isn't the restore's doing (its address and port are this machine's): that start exits for systemd to retry, as any start would, and doesn't count against the restore. The outcome is in `GET /api/maintenance/status` → `restore.result`.

- Success: `200 {ok: true, staged: true, changed: false, message, manifest, restored, skipped, warnings, notes, restart_note}`. `restored` says what will be switched in and which copies were kept (`previous_backed_up_as`); `restart_note` is set only when the restart call failed (the restore then waits, below).
- `keep_current_config=true` restores the database only and keeps this machine's settings — and its notification server (the backup's `ntfy/server.yml` is listed in `skipped`, never applied): a query parameter on the upload (`POST /api/backup/import?keep_current_config=true`; the form holds exactly one file part), a body field on `restore-from-path`. A backup with no database then has `nothing_to_restore` (`400`).
- Refusals change nothing and don't restart. `400`: not a zip, no or several `manifest.json`, an unsupported backup format, an entry stored with a compression method other than stored/deflate or encrypted (refused before anything is decompressed), a damaged or truncated entry, not an Aerodrome database (including a `schema_version` that isn't a table of integers), nothing to restore. `403`: a path outside `.backups/` (whether or not it exists) or one of Aerodrome's own temporary files; `404` only for a missing file inside `.backups/`. `409`: `newer_schema` ("Update Aerodrome first"); another maintenance operation is running; an update is being confirmed (`update_pending`); a restore already waits (`restore_pending`). `422 invalid_settings`: the backup's `config.yaml` wouldn't work here — it doesn't load, isn't UTF-8, is over 256 KB, uses YAML anchors/aliases, or fails Settings' validation (then with `errors: [{path, message}]`); when the backup has a database, also `database_only: true` and the message says the database can be restored alone (retry with `keep_current_config`). `507 no_space`: the restored database, a copy of the current one and 256 MB wouldn't fit on the database's disk, or the disk filled while unpacking. Every refusal carries `error` and `message` (the same text), `code` and `changed: false`.
- A backup holding only the notification server's settings (`ntfy/server.yml`) is applied at once, with no restart: `200 {ok: true, staged: false, ...}`. In every restore those settings are applied right after the checks pass, not at the restart (ntfy is its own service), and a rollback doesn't undo them.
- `restore-from-path` takes a file inside `.backups/` only, and never one of Aerodrome's own temporary files (`.backups/.tmp`, `.backups/.uploads`): `403`.
- An upload is copied to `.backups/.tmp` (readable by the service account only), and that copy is removed when the request ends, whatever the outcome.

**A restore that waits.** When the restart call fails (e.g. no sudo rule), the restore stays staged and applies at the next start **within an hour**; after that it is dropped with nothing changed (`restore.result.expired`). While one waits, `PUT /api/config`, the watchlist add/remove, config import and config restore, and every maintenance operation return `409 {code: "restore_pending", message: "A restore is waiting for the restart — restart Aerodrome to finish it, or cancel it in Settings."}`: the restart would otherwise put the backup's settings over a change made meanwhile. `POST /api/restart` and `POST /api/backup/staged-restore/cancel` stay allowed. The cancel removes the staged files and returns `{ok: true, cancelled: true, ntfy_applied, message}` (the pre-restore copies it took stay listed; `ntfy_applied: true` when the backup's notification-server settings were already applied at staging — they stay, and the message says so); `{ok: true, cancelled: false}` when nothing waits; `409 restore_switching` for a restore being switched in (also one whose switch a start began and was cut short: the next start finishes it). An expired restore is dropped by the next maintenance operation, the status poll or the next start.

**Put this back** (`POST /api/backup/pre-restore/{ts}/restore?keep_current_config=`) is a restore of a pair of pre-restore copies (`ts`: the `timestamp` from `GET /api/backup/pre-restore`): the same checks, the same switch at the restart and the same rollback. A copy of the current data is kept first, so it can be undone the same way; putting a pair back doesn't use it up (it is pruned like any pre-restore copy, after three newer ones). A pair whose settings copy is gone puts back the database only and says so in `skipped`; a settings copy that can't be used is `422` with `database_only` when the pair has a database. `404` when no copy from `ts` exists. `GET /api/backup/pre-restore` marks the pair a waiting restore needs with `in_use: true`; it is never pruned or purged, and the purge and the server-side backup delete are refused (`409`) while a restore waits.

**The backup download** (`GET /api/backup/export`) is one maintenance operation like the others below (`409` while another runs). It is built in `.backups/.tmp` and its file is removed before the download starts, so an abandoned or dropped download never leaves a database-sized file behind. Requests that come from other websites are refused with `403` before anything is built: the browser's `Sec-Fetch-Site` header must be absent, `same-origin` or `none`, and an `Origin` or `Referer` header must name this server. The dashboard, a typed URL and `curl` are unaffected. About twice the database's size plus 256 MB must be free (`507` otherwise).

**Server-side backups** (`POST /api/backup/create-server-side`) are built as `.backups/.tmp/aerodrome-backup-<date>.zip.partial` and only moved into `.backups/` (with their `.sha256`) once complete, so an interrupted backup never shows up in the list. They need about twice the database's size plus 256 MB free (`507` otherwise, or when the disk fills during the build). A second backup in the same second gets a `-2` suffix. Backup zips are created readable by the account Aerodrome runs as only, in a `.backups/` folder only that account can open.

**Upload sizes.** Every request body is capped at 2 MB, whatever its content type, except three multipart uploads: `POST /api/config/import` (256 KB), `POST /api/updates/local/upload` (50 MB) and `POST /api/backup/import` (2 GB), each plus 64 KB of form framing. A body over its cap gets `413 {ok: false, code: "too_large", error, message}` — before it is read when its size is declared, otherwise as soon as it passes the cap. The two large uploads are also refused **before their first byte is read**: `409` with `busy` while a maintenance operation runs (an update upload also gets `409 update_pending` while the last update is being confirmed); `409 upload_in_progress` while another large upload is still being received; `507 no_space` when the declared size wouldn't fit with 256 MB to spare — for a restore upload, three times its size plus the database (the spool, its staged copy and the restore's own check; a body without a declared size is re-checked every 64 MB). A request whose `Host` or `Origin` the same-origin guard refuses gets that `403` instead, before any of these checks and with its body unread. A large upload whose body stops arriving for 2 minutes is ended with `408 {ok: false, code: "upload_stalled", error, message}`. `POST /api/backup/import` takes exactly one file part, named `file`, and no other fields (`400 {code: "bad_upload"}` otherwise); if the disk fills up while it is received or stored, it answers `507 {code: "no_space"}`. The two large uploads are received into `.backups/.tmp` on the install's own disk instead of the system temp folder; an update zip (≤ 50 MB) is then read into memory, and a config import (≤ 256 KB) stays in memory. Leftovers in `.backups/.tmp` are removed when Aerodrome starts.

**One maintenance operation at a time.** Restores (`POST /api/backup/import`, `/api/backup/restore-from-path`, `/api/backup/pre-restore/{ts}/restore`, `/api/backup/staged-restore/cancel`), `GET /api/backup/export`, `POST /api/backup/create-server-side`, `DELETE /api/backup/server-side/{filename}`, `POST /api/backup/pre-restore/purge`, `POST /api/config/import`, `POST /api/config/restore/{name}`, `POST /api/setup/switch-to-real` and the update endpoints share one lock. While another one runs — or for up to 2 minutes after one that restarts the service — they return `409` with the same text in `error` and `message`, a `code` (`busy`; `update_in_progress` / `update_pending` on the update endpoints) and `busy: {op, since, age_sec, parked}` (what's running, its start as a Unix time, and whether it's waiting for a restart). A server-side backup refused because another server-side backup is running keeps its old body (`message: "A server-side backup is already running."`, `progress`) plus `busy`.

## Performance diagnostic

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/perf/diagnostics` | Run a live benchmark: db size, row counts, pragmas, index coverage, query timings, disk-read throughput, auto-generated hints. Per-table sizes only with `?sizes=true` (measuring reads every page of the database); `table_sizes` says when sizes were last measured (`measured_at`, `took_ms`, `fresh`), and a normal run reuses them |
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
| POST | `/api/ntfy/install` | Install ntfy (`port`, `bind`, optional `topic`, `base_url`, `upstream_relay`). On a partly installed ntfy (state `partial`) it repairs instead: it puts back what's missing and keeps the port, bind, base URL and relay setting the existing server.yml still holds (the body's `port`/`bind` fill in only when that file is the missing piece); the reply then has `repaired: true` |
| POST | `/api/ntfy/config` | Change the local ntfy server's address (`base_url`) or iPhone relay (`upstream_relay`) |
| POST | `/api/ntfy/upgrade` | Upgrade ntfy to a newer version |
| POST | `/api/ntfy/uninstall` | Remove the self-hosted ntfy install; `{"purge_data": true}` also deletes its cached messages (the local server's cache and SQLite's `-wal`/`-shm`, in `/var/lib/aerodrome-ntfy` and, from before v3.4.150, `/var/lib/ntfy`; a `cache-file` set by hand in server.yml is left in place, and `message` says so). With nothing installed it deletes a cache an earlier install left |

These endpoints delegate to `ntfy_installer.py`, which uses Aerodrome's system helper (see INSTALL.md → *System permissions*). When the helper can't do something, the reply has `ok: false`, the reason in `error` and `message`, and a `code`:

| Status | `code` | Meaning |
|---|---|---|
| 500 | `helper_unavailable` | No helper answered: this server's system permissions need a refresh first. `command` is the command to run on the server; `message` includes it. Nothing was changed. |
| 202 | `still_running` | The call took too long (a download can take minutes); the helper may still be at it. Check `GET /api/ntfy/status` again in a minute, and try again if nothing has changed (a helper stopped part-way — Aerodrome restarting stops one it started — has its change settled by the next ntfy action: kept when ntfy answers on it, put back when it doesn't). |
| 409 | `busy` | Another Aerodrome system task is still running. Try again in a minute. |
| 409 | `custom_keys` | As for `/api/ntfy/config` below — also from `/api/ntfy/install` when a server.yml with settings Aerodrome doesn't manage is in the way. |

Anything else the helper refused or that failed is a `400` with its reason in `message` — for example, after a settings change ntfy didn't answer again, so its previous settings were put back.

**Server address.** `/api/ntfy/install` and `/api/ntfy/config` accept only a plain web address in `base_url`: `http://` or `https://`, a host name, IPv4 address or `[IPv6]` address, an optional port (1–65535) and an optional path — no spaces, quotes, user name, query or fragment, at most 255 characters (for example `http://192.0.2.10:2586`). Anything else is a `400` with the same text in `error` and `message`, and nothing is changed.

**`POST /api/ntfy/config`** takes `{"base_url"?, "upstream_relay"?, "drop_custom_keys"?}`. A field you leave out keeps its current value. Aerodrome rewrites the notification server's config file from the settings it manages, so it can refuse instead of saving. Every refusal writes nothing, names settings only (never their values) and explains itself in `message`; the two you can act on are:

| Status | `code` | Meaning |
|---|---|---|
| 409 | `custom_keys` | The config file has settings Aerodrome doesn't manage, listed in `custom_keys` (for example `["auth-file"]`). Saving would remove them. To save anyway, resend with `drop_custom_keys` set to that list (the Notifications tab names them and asks before it does). If the file has a setting your list doesn't name (one added since), you get a fresh 409 with the current names instead of a save. |
| 400 | `stored_value_invalid` | A setting can't be carried over from the file — usually a saved address that's no longer accepted. The names are in `keys`. For `base-url`, send a new `base_url`; for `upstream-base-url`, send `upstream_relay`. For `listen-http`, this endpoint can't take a new value: Aerodrome keeps only `0.0.0.0:PORT`, `127.0.0.1:PORT` or `:PORT` there, so fix it in `/etc/ntfy/server.yml` by hand or uninstall and reinstall the notification server. |

Other 400s (`invalid`, `unreadable`, or a notification server Aerodrome doesn't manage) carry their reason in `message`.

`GET /api/ntfy/status` reports the same picture in its `config` block: `base_url` (null when the saved address is no longer accepted), `upstream_relay`, `custom_keys` and `problems` (setting names only). `status.legacy_unit` is `true` while an install made before v3.4.150 still runs on its older, root-run setup (saving its settings moves it); `config.migrate_command` is then the command that moves it from the server (with `--drop-custom-keys` and the names, when there are settings it would remove). `status.state` counts an official ntfy package (`/usr/bin/ntfy` or its unit) as `external`.

## Updates

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/updates/local/check` | Check for a staged release in `update/`; also `preflight` (below). With nothing to apply, `staged` is `"none"` (`update/` is missing or holds only its own `UPDATE_README.md`/`.gitkeep`) or `"partial"` (something is there, but no `VERSION`) |
| POST | `/api/updates/local/upload` | Upload a release zip (staged into `update/`) |
| POST | `/api/updates/local/apply` | Apply the staged release: stage to the side, test-load, switch in, restart; rolled back automatically if it doesn't start |
| GET | `/api/updates/github/check` | The cached GitHub release check (`?force=true` checks now) |
| POST | `/api/updates/github/apply` | Download the latest GitHub release, verify its SHA256, stage it, then apply it exactly like a local apply |
| GET | `/api/maintenance/status` | What's running and how the last update went (below) |
| POST | `/api/maintenance/ack` | `{"kind": "update"}` — mark the last update result as seen; `{"kind": "config"}` — mark the last config recovery as seen; `{"kind": "restore"}` — mark the last restore result as seen |
| POST | `/api/restart` | Restart the Aerodrome service (409 only while a maintenance operation holds the lock). Asked for by a just-updated version after it has answered, it confirms that update first, so the restart is never counted as a failed start |

**Apply** (`POST /api/updates/local/apply?snapshot_db=&allow_downgrade=&allow_reinstall=`, and the GitHub apply). A staged release that is older than, or the same as, the running version is refused unless `allow_downgrade=true` / `allow_reinstall=true`. Success: `{ok: true, from_version, to_version, restart_ok: true, backup_dir, pip_warning, previous_loads, final, message}`:

- `pip_warning`: the package installer's message when it failed but the new version still loaded (otherwise null).
- `previous_loads`: when the release's Python requirements changed, whether the running version still loads with the new packages (`false` means a rollback to it, or its next restart, would fail — the venv is shared and is never rolled back); null when no packages were installed.
- `final`: `true` when the target is a release from before 3.4.147 (going back with `allow_downgrade`). Such a release can't confirm or roll back an update, so the switch is made permanent at once; the previous version is kept in `.backups/<ts>/`.

Every refusal leaves Aerodrome's own files as they were and keeps the release staged (once the release's packages were installed, the venv may already have changed: the message then says so). The refusals with a `code` below return `{ok: false, code, error, message, detail, from_version, to_version}` (`detail`: the installer's and test-load's output, or the restart command's message) and are recorded for `/api/maintenance/status`; the two busy 409s return `busy` instead of `detail` and aren't recorded:

| Status | `code` | Meaning |
|---|---|---|
| 409 | `update_in_progress` | Another maintenance operation is running (`busy` says which) |
| 409 | `update_pending` | The previous update is still being confirmed — try again in a minute (or, when it stopped part-way through switching files, it is put back at the next start) |
| 409 | `downgrade_refused`, `same_version` | See above |
| 409 | `not_writable` | Folders the update must replace aren't writable by the account Aerodrome runs as; `message` has the command that fixes it |
| 400 | `bad_version` | The staged `VERSION` isn't a release number (X.Y.Z) |
| 500 | `stage_failed` | Copying the release to the side failed (for a reason other than disk space), or its staged copy changed while it was being checked |
| 500 | `apply_failed` | Another file error before the switch; nothing was switched |
| 500 | `verify_failed` | The new version doesn't load (syntax error, missing package) |
| 500 | `deps_failed` | Its packages failed to install and it doesn't load without them |
| 500 | `swap_failed` | Switching the files in failed; the previous version was put back |
| 500 | `restart_failed` | Aerodrome can't restart itself on this install; the switch was undone |
| 507 | `no_space` | Not enough free disk space to stage or switch in the release |

Two refusals keep their older bodies, with no `code` and nothing recorded: `409 {ok: false, error: "sudoers_refresh_required", message, sudoers, install_dir}` when the release needs newer system permissions first (the command to run is `sudoers.recovery_command`; `GET /api/updates/local/check` reports the same `sudoers` block), and `400 {ok: false, error: "No VERSION file in update/. Nothing to apply."}` when nothing is staged. A server with no `/etc/sudoers.d/aerodrome` at all (`sudoers.managed: false`), or one Aerodrome can't read, is never refused by this permissions check (an apply still needs the restart rule: `restart_failed` above).

**Upload** (`POST /api/updates/local/upload`, and the GitHub download) checks the zip before anything is unpacked or the staged release is replaced: `400` (`not_a_release`) for an entry compressed with anything but deflate (or stored), or encrypted — Aerodrome releases never are, and other methods could expand without limit; `413` (`too_large`) above 200 MB unpacked; `507` (`no_space`) when it wouldn't fit with 256 MB to spare. The release staged before is kept either way.

**`preflight`** on `/api/updates/local/check` is a read-only check that an apply could write everything it has to — the install folder and the folders the staged release would replace (with nothing staged, the folders a release ships): `{ok, problems, message, fix_command}` (`problems`: paths that aren't writable; `fix_command`: a `chown` + `chmod u+w` command that makes them writable).

**`GET /api/maintenance/status`** returns `{busy, update, update_pending, config, restore}`: `busy` is the operation holding the maintenance lock (or null); `update` is the last update's result — `{result: "applied" | "rolled_back" | "refused", from_version, to_version, at, acknowledged, …}` with `reason`, `failed_dir`, `schema_before`/`schema_after` for a rollback, `code`/`message`/`detail` for a refusal, `pip_warning` and `backup_dir` for an applied update, `previous_loads` for either — or null; `update_pending` is the update being confirmed right now (`{from_version, to_version, failed_starts, served_at, applier_pid, state, waiting_for_restart}`) or null; `config` is what the last start did when `config.yaml` couldn't be used — `{kind: "unreadable" | "bind", reason, source, source_date, web_host, web_port, broken_copy, rewritten, at, acknowledged}` (`source`: the copy it runs on, `.backups/config.yaml.last-good` or a `config.yaml.bak.<date>`; `broken_copy`: where the unusable file was kept; `tried`: the address that couldn't be listened on, for `bind`) — or null; `restore` is `{pending, result}`. `pending`: a restore staged and waiting for its restart — `{phase: "staged", kind: "restore" | "put_back", staged_at, expires_at, restart_failed, waiting_for_restart, source, backup_created_at_iso, keep_current_config, notes, ntfy_applied}` (`restart_failed`: why the restart call failed; `waiting_for_restart`: the process that staged it is still the one answering; `backup_created_at_iso`: the backup manifest's date when it is one, else null; `ntfy_applied`: the backup's notification-server settings were already applied) — or null. `result`: what the last restore did at its start — `{ok: true, notes}`; `{ok: false, rolled_back: true, error}` (with `kept_current: [<database name>]` when the switch failed before the restored database was moved in, so the current one was never replaced and is kept); `{ok: false, rolled_back: false, error, missing, manual, set_aside}` when a pre-restore copy it needed was gone (`set_aside`: the name the restored database was moved to, so that Aerodrome starts on a fresh one); `{ok: false, expired: true}`; `{ok: false, cancelled: true}` (both with `ntfy_applied: true` when the backup's notification-server settings had been applied) — each with `source`, `kind`, `backup_created_at_iso`, `at`, `acknowledged` — or null.

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
