# Installation & Operations Manual

This is the long-form documentation for installing, configuring, updating,
and operating Aerodrome. The README has the ~3-minute version; this file
has everything else.

If you just want to get the service running, jump to
[Installation](#installation). If you've already installed and need
something specific, the table of contents below has direct links.

## Table of contents

- [Requirements](#requirements)
- [Hardware sizing](#hardware-sizing)
- [Capacity planning](#capacity-planning)
- [Installation](#installation)
- [Remote access (optional)](#remote-access-optional)
- [Managing the service](#managing-the-service)
- [Updating](#updating)
- [Rolling back](#rolling-back)
- [Applying sudoers updates](#applying-sudoers-updates)
- [Uninstalling](#uninstalling)
- [Configuration](#configuration)
  - [Receiver location](#receiver-location-for-the-distance-column)
  - [Retention](#retention-per-tab)
  - [Logging](#logging)
  - [Stats](#stats)
  - [Data quality filters](#data-quality-filters)
  - [Military detection](#military-detection)
  - [Watchlist](#watchlist)
  - [Notifications](#notifications)
- [Performance diagnostic](#performance-diagnostic)
- [Backup and restore](#backup-and-restore)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)

## Requirements

- An ADS-B receiver on your network serving an `aircraft.json` endpoint
  (readsb, dump1090-fa, tar1090, PiAware, or any compatible).
- A Linux server (Ubuntu 22.04+ or Debian 12+ recommended).
- Python 3.10+ (installed automatically by the install script if missing).

## Hardware sizing

Pick based on expected traffic volume. "Sightings" are individual poll
records — a single aircraft at altitude typically produces dozens of
sightings before it flies out of range.

- **Rural / quiet airspace** — under ~500 aircraft/day, under ~300k
  sightings at 30-day retention. Raspberry Pi 4 with any reasonable SD
  card is fine. Everything is fast.

- **Suburban / moderate** — ~500-2,000 aircraft/day, ~300k-1.5M sightings
  at 30-day retention. Raspberry Pi 4 still works, but use an A2-rated SD
  card for random-I/O performance (examples as of 2026: SanDisk Extreme
  Pro, Samsung Pro Plus). Stats tab loads in the 3-8 second range.

- **Major-airport / heavy** — 2,000+ aircraft/day, 1.5M+ sightings at
  30-day retention. Raspberry Pi 5 strongly recommended, and an NVMe HAT
  is worth the money — the Stats tab's worst-case queries are bound by
  storage I/O, and NVMe is roughly 10-20× the random-read throughput of
  an SD card. Example HATs (as of 2026): Pimoroni NVMe Base, Pineberry Pi
  HatDrive. Any small x86 box with an SSD is an equivalent alternative.

If you don't want to upgrade hardware, reducing retention is the easiest
win. A 3M-row install at 30-day retention becomes a 1M-row install at
10-day retention — and often runs comfortably on the original hardware
tier. Adjust it in the Configuration → Retention tab in the web UI
(recommended), or edit `retention.all_days` in `config.yaml` directly.

## Capacity planning

How much disk you need depends on receiver traffic, which varies a lot
with location and antenna setup. The two reference installs the project
has data for bracket the typical range:

| Traffic    | Rows/day | Daily growth | 7d retention | 30d retention | 60d retention | 90d retention |
|------------|----------|--------------|--------------|---------------|---------------|---------------|
| Quiet      | ~50k     | ~9 MB/day    | ~60 MB       | ~270 MB       | ~540 MB       | ~810 MB       |
| Busy       | ~715k    | ~118 MB/day  | ~830 MB      | ~3.5 GB       | ~7.0 GB       | ~10.6 GB      |

"Quiet" approximates suburban airspace with a modest antenna; "busy"
approximates urban airspace with a high-gain setup. Most installs land
somewhere between these two — pick the band closer to your receiver's
location.

The numbers above are estimates. Once your install has run for ~3 days,
Aerodrome measures the *actual* daily growth rate from your
`all_sightings` table (a rolling 7-day average) and shows projected
steady-state size for your real install in two places:

- **Status page → Capacity card** — current size, daily growth, projected
  settled size at current retention, free disk space, and a what-if
  expansion showing projections at 7/14/30/60/90/180 day retention values
  with headroom warnings.
- **Configuration → Retention tab** — a live preview line under the
  retention fields that updates as you change the All-history value, so
  you can see the projected impact before saving.

If your Capacity card shows headroom under 1.2× at your current
retention, the projected DB will come close to filling your disk — drop
retention or add storage. Aerodrome itself doesn't enforce any
disk-space safety margin, so this is a planning concern, not a runtime
one.

## Installation

### 1. Download

```bash
# From the GitHub Releases page:
#   https://github.com/preston-peterson/aerodrome/releases
# Or clone the repository:
git clone https://github.com/preston-peterson/aerodrome.git
cd aerodrome
```

### 2. Configure

Edit `config.yaml` to set your receiver's address:

```yaml
receiver:
  ip: "192.0.2.10"            # Replace with your ADS-B receiver IP
  port: 8080                  # Your receiver port
  path: "/data/aircraft.json" # Path to aircraft JSON
```

### 3. Deploy to your server

If running on the same machine, skip to step 4. Otherwise, copy to your
server:

```bash
rsync -av aerodrome/ user@your-server:~/aerodrome/
ssh user@your-server
cd ~/aerodrome
```

### 4. Install

> **Note:** Both `install.sh` and `uninstall.sh` need to be made
> executable before running. File-transfer tools like rsync, scp, zip
> extraction, and git checkouts on Windows often strip the executable
> bit, so this step is almost always needed.
>
> Alternatively, you can skip the `chmod` and run the scripts via
> `sudo bash install.sh` — `bash` doesn't care about the execute bit.
> Both forms work identically; pick whichever you prefer.

On the server, in your `~/aerodrome` directory:

```bash
chmod +x install.sh uninstall.sh
./install.sh
```

The install script:
- Installs Python 3 and required system packages
- Creates a Python virtual environment
- Installs Python dependencies
- Installs and starts a systemd service called `aerodrome`

When complete, open `http://your-server-ip:8000` in your browser.

> **Note:** The install script auto-detects your current username and
> configures the systemd service to run as that user. Just don't run it
> as root.

## Remote access (optional)

Aerodrome listens on port 8000 by default. On your home LAN, that's all
you need. For access from outside your home network — and for push
notifications via a locally-hosted ntfy server to reach your phone while
travelling — use a WireGuard-style overlay network rather than
port-forwarding.

Recommended options:

- **[Tailscale](https://tailscale.com)** — the simplest path. Free for
  personal use, no configuration. Install the Tailscale daemon on your
  Aerodrome server and the Tailscale app on your phone; both devices
  get stable private addresses on the `100.64.x.x` range, reachable
  from anywhere. Aerodrome's web UI, the local ntfy server, and SSH
  all become accessible at the server's overlay address.
- **[Headscale](https://github.com/juanfont/headscale)** — self-hosted
  Tailscale-compatible coordination server if you prefer not to rely
  on Tailscale's hosted coordination.
- **[Netbird](https://netbird.io)** /
  **[ZeroTier](https://www.zerotier.com)** — similar WireGuard-based
  overlays with different tradeoffs.
- **Your own WireGuard** — Aerodrome works fine over any existing VPN.

**Why not port-forwarding?** Aerodrome has no authentication; it trusts
the network layer for access control. Exposing it directly to the public
internet would mean anyone who finds the port can read your data, edit
your config, and trigger restarts. Overlay networks close that attack
surface entirely — only devices you've joined to your overlay can reach
the service.

## Managing the service

```bash
sudo systemctl status aerodrome      # Check status
sudo systemctl restart aerodrome     # Restart (after config edits)
sudo systemctl stop aerodrome        # Stop
sudo journalctl -u aerodrome -f      # Follow live logs
```

## Updating

Aerodrome has two update paths. The web UI is the simpler one for most
upgrades; the rsync command is still there as a fallback.

### Option 1 — Upload via the web UI (recommended)

No SSH required.

1. **Download the release zip** from the GitHub Releases page.

2. **Open the Updates page** in your browser: click the gear icon in
   the header → **Check for updates** (or go directly to `/updates`).

3. **Drag the zip onto the "Upload a release zip" drop zone** (or
   click it to open a file picker). The server extracts the zip,
   validates that it contains a `VERSION` file and `main.py`, and
   stages it. You'll see the staged version appear in the Local Update
   card.

4. If the staged version is newer than what's running, click **Apply
   X.Y.Z & restart**. The server will:

   - Back up the current install to `.backups/<timestamp>/`
   - Copy new files over the install, preserving `config.yaml`,
     `aircraft_history.db*`, `logs/`, `venv/`, `.tracker.pid`,
     `.backups/`, and `update/`
   - Run `venv/bin/pip install -r requirements.txt` to pick up any
     new dependencies
   - Clear the `update/` folder (except its README) so it's ready for
     next time
   - Restart the service via `systemctl`
   - Your browser will reconnect automatically after ~6 seconds

That's it. If you'd rather stage updates from the command line (handy
for scripted workflows or quick dev iteration), you can drop the
unpacked release into `~/aerodrome/update/` via rsync or scp:

```bash
rsync -av aerodrome/ user@your-server:~/aerodrome/update/
```

Either staging path (UI upload or rsync) ends up in the same place;
click **Apply** in the UI to finish.

### Option 2 — Direct rsync + restart (fallback)

If the web UI isn't available (service not running, permission issue
with the sudoers rule, etc.), you can still update the old-fashioned
way:

```bash
rsync -av \
  --exclude='aircraft_history.db' \
  --exclude='logs' \
  --exclude='venv' \
  --exclude='.tracker.pid' \
  --exclude='config.yaml' \
  --exclude='.backups' \
  --exclude='update' \
  aerodrome/ user@your-server:~/aerodrome/

ssh user@your-server "sudo systemctl restart aerodrome"
```

Single-line version for terminals that mangle line continuations:

```bash
rsync -av --exclude='aircraft_history.db' --exclude='logs' --exclude='venv' --exclude='.tracker.pid' --exclude='config.yaml' --exclude='.backups' --exclude='update' aerodrome/ user@your-server:~/aerodrome/
```

### Config auto-migration (both paths)

Regardless of which update path you use, Aerodrome handles config
schema changes automatically. **If the new release adds config keys
your `config.yaml` doesn't have yet, they're auto-merged in with their
defaults and your previous config is backed up as
`config.yaml.bak.<timestamp>`.** Your existing values are never
overwritten. The 5 most recent backups are kept; older ones are pruned
automatically.

The service logs what was added, so you can see new settings at:

```bash
sudo journalctl -u aerodrome -n 50
```

If you'd rather overwrite your config entirely (e.g. to get fresh
comments), drop the `--exclude='config.yaml'` from the rsync command.

## Rolling back

If an update breaks something, each update leaves a snapshot of the
previous source files in `.backups/<timestamp>/`. To roll back:

```bash
ssh user@your-server
cd ~/aerodrome
# List available snapshots
ls -1 .backups/
# Restore (replace TIMESTAMP with the folder name)
cp -r .backups/TIMESTAMP/* ./
sudo systemctl restart aerodrome
```

Note that `.backups/` holds source files only — your `config.yaml` and
database were never touched, so they don't need restoring. Aerodrome
keeps the 5 most recent snapshots here; older ones are pruned
automatically when a new one is created.

## Applying sudoers updates

Some releases add new system-level operations — things like "install
the local ntfy server" or "write a systemd unit" — that require new
entries in `/etc/sudoers.d/aerodrome`. Aerodrome detects when a staged
release needs a sudoers refresh and shows you an amber "Sudoers update
required" banner on the Updates page *before* letting you apply. The
Apply button is disabled until the sudoers file is refreshed.

**Why doesn't Aerodrome just update the sudoers file automatically?**
Because that would be a permission-escalation risk. If the Aerodrome
service (running as an unprivileged user) could write new sudo rules
for itself, any compromise of the service would let an attacker grant
themselves arbitrary root privileges. Keeping the refresh a manual
step means you explicitly consent to each permission change.

**What do I do when I see the banner?** SSH to your server and re-run
the install script:

```bash
ssh user@your-server
cd ~/aerodrome
sudo bash install.sh
```

Re-running `install.sh` is idempotent and safe — it updates the
sudoers file to match the new version and leaves everything else
(your config, database, logs, venv) untouched. You don't need to stop
the service first. After running, click **Re-check** in the modal;
once the version matches, the Apply button enables and you can
proceed with the update normally.

**Which releases need this?** Most don't. The banner only appears
when a release genuinely changed the sudoers file — you'll see it in
the changelog for those releases too. After the refresh, every
subsequent release that keeps the same sudoers version applies with
no extra step.

## Uninstalling

> **Note:** If you didn't run `chmod +x uninstall.sh` during install,
> or you pulled a fresh copy since then, you'll need to run it before
> the script can execute (file transfers strip the executable bit).

To remove Aerodrome completely:

```bash
ssh user@your-server
cd ~/aerodrome
chmod +x uninstall.sh
./uninstall.sh
```

The uninstall script is interactive by default — it will prompt before
deleting your database, logs, or config. If you want to skip the
prompts:

```bash
./uninstall.sh --purge   # Remove everything (data included)
./uninstall.sh --keep    # Remove only the service and venv, keep all data
```

The uninstaller removes the systemd service, the Python virtual
environment, runtime files (`.tracker.pid`, `__pycache__`), and
(optionally) your data. It does **not** remove system packages like
`python3` or `pip`, since those may be used by other applications.

## Configuration

All settings live in `config.yaml`. You can edit it two ways:

**Option 1 — Web UI (recommended)**

Open `http://your-server:8000/config` (or click the ⚙ icon →
Configuration in the header). Every setting from `config.yaml` is
editable with inline validation. Changes to live settings (retention,
distance unit, special aircraft) apply immediately. Changes to
restart-required settings (receiver IP, web port, etc.) show an amber
banner with a "Restart now" button.

The install script sets up a targeted `sudoers.d` rule so the
"Restart now" button works without a password. This rule only permits
restarting the `aerodrome` service — nothing else. It's removed
automatically on uninstall.

**Option 2 — Edit the YAML directly**

```bash
nano config.yaml
sudo systemctl restart aerodrome
```

Either way, your edits are preserved on upgrade via the auto-merge
described in the [Updating](#updating) section.

### Receiver location (for the Distance column)

To show distance from your receiver to each aircraft, set your
receiver's coordinates:

```yaml
receiver:
  latitude: 37.7749       # Your receiver's latitude
  longitude: -122.4194    # Your receiver's longitude
  distance_unit: "mi"     # "mi", "nmi", or "km"
```

Find your coords at [latlong.net](https://www.latlong.net/). If you
leave `latitude` or `longitude` as `null`, the Distance column is
hidden.

### Retention (per tab)

```yaml
retention:
  military_days: 30
  watchlist_days: 30
  all_days: 30
```

### Logging

```yaml
logging:
  level: "INFO"   # DEBUG, INFO, WARNING, ERROR
  dir: "logs"     # directory where tracker.log is written
```

`DEBUG` is useful when troubleshooting receiver connectivity or
hexdb.io resolution issues; `INFO` is the sensible default for normal
operation.

### Stats

```yaml
stats:
  enabled: true                # Set false to hide the Stats tab entirely
  refresh_interval: 300        # Auto-refresh every N seconds (0 to disable)
  track_gap_minutes: 5         # Gap threshold for "longest continuous track"
```

`track_gap_minutes` defines when a sighting series becomes a new
"track". If the receiver doesn't hear from an aircraft for more than
this many minutes, the current track ends. Prevents a brief dawn
sighting plus a reappearance at dusk from being reported as one
22-hour continuous track. Valid range 1–60; typical values 2, 5, 10,
15, or 30 minutes. Additional settings (timezone, card visibility,
new-record alert color, range rose configuration) are easiest to
manage through the web UI's Configuration page.

### Data quality filters

ADS-B data is noisy. Transponders misreport, receivers relay indirect
data, and the occasional garbage value sneaks through. Aerodrome
applies three filters to keep bad data from polluting your records
and stats:

- **Type-aware speed ceilings.** A 696-kt reading on a Cessna 172 is
  garbage — a 172's max cruise is ~120 kt. Aerodrome caps each
  aircraft type at a physically plausible ground speed (e.g. 250 kt
  for light GA singles, 400 kt for large turboprops, 700 kt for
  airliners, 1500 kt for fighters and unknown types). Readings above
  the cap are excluded from the "fastest ever" record. The cap
  applies only to records; the raw value still appears on the Live
  tab if you want to see it.

- **TIS-B / MLAT pseudo-target exclusion.** Some receivers (via
  dump1090 or readsb) relay aircraft position data they didn't hear
  directly — it came from ATC or from triangulation across a network.
  These show up with ICAO hex codes prefixed `~` and often have
  noisy altitude or speed values. Aerodrome excludes them from
  all-time records (but still stores them for visibility on the Live
  tab).

- **Retroactive self-heal.** When Aerodrome tightens a filter in a
  future release, previously-stored records that violate the new
  rule get recomputed from historical data on next startup. A bogus
  record stored under looser rules will quietly fix itself.

These filters apply to the Stats tab's records and the "Today's
extremes" cards. They do not affect the Live, Watchlist, or Military
tabs — those show what the receiver saw, unfiltered, so you can spot
anomalies yourself.

### Military detection

Customize what counts as "military":

```yaml
military:
  callsign_prefixes:
    - "RCH"       # Air Mobility Command
    - "VV"        # Special Air Mission
    - "NATO"
    - "ARMY"
    - "NAVY"
    - "PAT"
  icao_prefixes:
    - "AE"        # US military block
  special_aircraft:
    "ADFDF9":
      label: "Air Force 1"
      color: "#3b82f6"
    "AE4AE6":
      label: "Air Force 2"
      color: "#22c55e"
```

### Watchlist

Track specific aircraft or entire aircraft types. Add via `config.yaml`
or the web UI:

```yaml
watchlist:
  - tail: "N12345"
    label: "Friend's Cessna"
  - icao: "A1B2C3"
    label: "Cool plane"
  - callsign: "UAL"
    label: "United flights"         # callsigns match by prefix
  - model: "Cirrus"
    label: "Any Cirrus aircraft"    # substring match against type + description
  - model: "G650"
    label: "Any Gulfstream G650"
```

Entry types:

- **`icao`** — exact 6-char hex code (`A1B2C3`). Most reliable since
  ICAO is always broadcast.
- **`tail`** — registration (`N12345`). Resolved to ICAO hex via
  hexdb.io when added.
- **`callsign`** — prefix match (`UAL` matches `UAL123`, `UAL9876`,
  etc.).
- **`model`** — case-insensitive substring match against the
  aircraft's type code (e.g., `S22T`) and description (e.g.,
  `CIRRUS SR-22 Turbo`). Useful for watching every aircraft of a
  particular make or model. Changes take effect live on the next
  poll cycle.

Clicking the `+` button on any row in Live, Military, or All lets
you pick which identifier to use, including model when the aircraft
broadcasts a type/description.

#### Alerts

Aerodrome can flash the Watchlist tab when a matching aircraft is
seen, so you don't need to keep the tab open to catch activity.
Configure in the web UI (gear → Configuration → Watchlist alerts) or
directly in `config.yaml`:

```yaml
watchlist_alerts:
  enabled: true
  trigger: "new"          # "new" | "continuous" | "continuous_dismissable"
  effect: "pulse_dot"     # "pulse_dot" | "pulse" | "dot" | "flash"
  color: "#f59e0b"
```

- `trigger: "new"` flashes when an aircraft first appears (or
  reappears after going out of range) and stops when you click the
  Watchlist tab.
- `trigger: "continuous"` flashes any time a watchlist aircraft is
  visible.
- `trigger: "continuous_dismissable"` flashes continuously but
  dismisses on tab click; resumes on the next new sighting.
- `effect: "pulse_dot"` combines a soft pulse with a notification
  dot (recommended).
- `effect: "flash"` does a 3-second rapid attention-grabbing flash.
- Changes apply live without a service restart.

### Notifications

Push notifications to your phone via [ntfy](https://ntfy.sh) when
notable events happen. Configure from the web UI (gear →
Configuration → Notifications) or directly in `config.yaml`:

```yaml
notifications:
  enabled: true
  url: "https://ntfy.sh/your-unique-topic-here"
  priority: "default"        # min, low, default, high, max
  events:
    receiver_offline: true
    receiver_recovered: true
    watchlist_hit: false     # opt in for noisier events
    new_record: false
    special_aircraft: false
    daily_summary: false     # coming in a later release
  cooldown_minutes:
    watchlist_hit: 10        # suppress same-aircraft repeats within 10 min
    special_aircraft: 30
  rate_limit_per_hour: 20    # overall ceiling across all events
  quiet_hours:
    enabled: false
    start: "22:00"
    end: "07:00"
  receiver_offline:
    consecutive_failed_polls: 5   # ~5 min at default 60s poll interval
```

**Two setup paths:**

1. **Public [ntfy.sh](https://ntfy.sh)** — 30-second setup. Pick a
   hard-to-guess topic name (e.g., `aerodrome-<random>`), set the URL
   to `https://ntfy.sh/<your-topic>`, install the ntfy mobile app, and
   subscribe to the same URL. Topic name is your only auth, so keep
   it private. Public ntfy.sh has generous free-tier limits — more
   than enough for personal use.

2. **Self-hosted** — runs on the same Ubuntu server as Aerodrome. The
   Notifications tab has a one-click installer that downloads the
   ntfy binary from GitHub (verified by SHA256), installs it as a
   systemd service, and auto-fills your subscription URL. Default
   port 2586, bind 0.0.0.0 so your phone on the same LAN can
   subscribe directly. For remote access, use
   [an overlay network](#remote-access-optional) rather than
   port-forwarding.

**How it works: Android vs iOS**

Real-time push from a self-hosted ntfy server works differently on
the two mobile platforms, and it's worth understanding which path
applies to you.

On **Android**, the ntfy app keeps a persistent HTTP streaming
connection open to your self-hosted server. When Aerodrome posts a
message, it travels directly: Aerodrome → your ntfy server → your
phone. No third party involved. This works out of the box as long as
your phone can reach the server (same LAN, Tailscale, or a reverse
proxy). If Android battery optimization is aggressive, you may need
to whitelist the ntfy app so the OS doesn't suspend its background
connection.

On **iOS**, persistent background connections are forbidden by Apple
— apps can only be woken up through APNs (Apple Push Notification
service), and APNs credentials aren't something you can set up
yourself. So the ntfy iOS app relies on a wake-up relay: when a
message arrives at your server, your server pings ntfy.sh with just
the message ID, ntfy.sh dispatches an APNs push to wake up your
phone, and then your phone fetches the full message body directly
from your server. The upstream relay is enabled by default in
Aerodrome's ntfy installer via:

```yaml
upstream-base-url: "https://ntfy.sh"
```

**Privacy note for iOS users:** enabling `upstream-base-url` means
ntfy.sh sees your topic names and message IDs (but NOT message
content — only the server can return the body). For most people this
is an acceptable trade-off for real-time push. If you prefer to
disable it, uncheck "iOS instant-push via ntfy.sh relay" in the
Notifications tab. Your iPhone will still see messages, but only on
manual pull-to-refresh.

**Base URL.** The installer auto-detects your LAN IP and sets it as
the `base-url` in the generated `server.yml`. If detection picks the
wrong interface (common on multi-homed hosts with Docker bridges,
VPNs, or Tailscale), you can edit the Base URL in the Notifications
tab — or use a Tailscale/reverse-proxy URL for remote access.
`base-url` must be reachable from your phone; `http://localhost`
won't work (that would be localhost on the phone, not on the server).

**What each event fires on:**

- `receiver_offline` — collector fails N consecutive polls (default
  5 ≈ 5 min)
- `receiver_recovered` — receiver comes back after an outage
- `watchlist_hit` — any watchlist aircraft is sighted (per-ICAO
  cooldown applies)
- `new_record` — a new all-time record is set (fastest, highest,
  furthest, etc.)
- `special_aircraft` — an aircraft listed in `military.special_aircraft`
  appears
- `daily_summary` — end-of-day digest (planned for a later release)

**Test your setup** with the "Send test" button on the Notifications
tab. Works even when notifications are disabled, so you can verify
the URL is reachable before committing. The tab also shows a sliding
log of the last 20 notification attempts — sent or suppressed — with
the reason for each suppression (cooldown, rate limit, quiet hours,
etc.), which is the fastest way to debug why a notification didn't
arrive.

## Performance diagnostic

A moderate urban or suburban receiver sees a few thousand unique
aircraft per day; a busy one near a major airport can see 5,000+ with
over a million individual sightings (one plane at altitude might
appear in dozens of polls before it flies out of range). At the
default 30-day retention, that's up to ~30 million rows to keep
indexed — the kind of workload where query shape and index coverage
start to matter, especially on a Raspberry Pi with an SD card.

The `/performance` page measures your actual system rather than
letting you guess what's wrong. Accessible from the gear menu →
Performance, it produces a read-only snapshot covering:

- **Storage** — database file path, size including WAL/SHM.
- **Tables** — row counts per table with count-query timing,
  oldest/newest timestamps, and retention span.
- **Indexes** — which indexes exist on which tables.
- **Query timings** — six representative queries the UI actually runs
  (live aircraft, all-tab count, military count, watchlist count, the
  big all-tab paginated group-by, seen_aircraft total). Each row is
  color-coded (green < 100 ms, amber 100–1000 ms, red > 1000 ms) and
  click to expand its `EXPLAIN QUERY PLAN`.
- **Disk I/O baseline** — 1 MB sequential read from the database file
  as a sanity check against slow storage.
- **SQLite + system** — version, platform, pragmas (journal_mode,
  page_size, cache_size, etc.).
- **Auto-generated hints** — specific warnings surface when the
  database is over 5 GB, any table is over 10 million rows, a query
  takes longer than 2 seconds, or disk throughput is under 15 MB/s.
  No hints means a healthy system.

A "Copy diagnostic report" button produces a plaintext dump suitable
for pasting into a GitHub issue. On plain-HTTP deployments (where the
browser blocks the modern clipboard API) a fallback modal shows the
report in a selectable textarea so you can copy it manually.

See `docs/PERFORMANCE.md` for tuning guidance — hardware
recommendations by activity level, retention-tuning options, and a
reference table for expected query timings at common row counts.

## Backup and restore

The Configuration page has a **Full backup** section that produces a
complete disaster-recovery zip in one click. It contains:

- `config.yaml` — all settings.
- `aircraft_history.db` — the full sighting database, snapshotted
  safely via SQLite's online backup API so the copy is consistent
  even while the service is actively writing new sightings.
- ntfy server config (if you're running a local ntfy server).
- A manifest with version, timestamp, and file sizes.

To restore to a fresh Aerodrome install: install the target version
on the new host, open the Configuration page, scroll to Full backup
→ **Restore from backup**, and upload the zip. The server stops the
collector, replaces the relevant files, and restarts. Your history,
config, and notification setup come back exactly as they were.

The zip is portable across hosts — you can back up on a Raspberry Pi
and restore onto an x86 box (or vice versa), as long as the target
is running the same Aerodrome version or a newer one. Different
Aerodrome versions handle config auto-migration the same way they
do on an in-place upgrade.

## Project structure

```
aerodrome/
├── install.sh              # One-time server setup
├── uninstall.sh            # Clean uninstall
├── bump-version.sh         # Version management + doc rebuild pipeline
├── main.py                 # Entry point (start/stop/status/restart)
├── collector.py            # ADS-B fetching and classification
├── server.py               # FastAPI web server and API
├── config_validator.py     # Strict field validation for config updates
├── notifier.py             # Push notification dispatch (ntfy)
├── ntfy_installer.py       # One-click local ntfy install/upgrade/uninstall
├── designators.py          # ICAO airline + aircraft-type code → friendly name
├── config.yaml             # All settings
├── config.yaml.example     # Reference defaults (used for auto-merging new keys on upgrade)
├── requirements.txt        # Runtime Python dependencies (installed by install.sh)
├── requirements-dev.txt    # Dev-only deps (reportlab, playwright) for docs tooling
├── VERSION                 # Single source of truth for version
├── README.md               # Project front door
├── CHANGELOG.md            # Version history
├── CONTRIBUTING.md         # How to interact with the project
├── LICENSE                 # MIT license
├── templates/
│   ├── index.html          # Main dashboard (Live/Watchlist/Military/Stats/All)
│   ├── status.html         # Status page
│   ├── config.html         # Configuration editor
│   ├── updates.html        # Updates page (drop-zone upload + local + GitHub)
│   ├── performance.html    # Performance diagnostic page
│   ├── docs.html           # In-app documentation viewer
│   └── logs.html           # Log viewer with tail/search/download
├── docs/                   # Documentation assets
│   ├── INSTALL.md          # This file
│   ├── PERFORMANCE.md      # Performance guidance for busy receivers
│   ├── SEARCH_SYNTAX.md    # Search parser tokens reference
│   ├── Aerodrome_Overview.pdf  # Shareable project overview
│   └── screenshot-*.png    # UI screenshots
├── scripts/                # Developer tooling (not run at service runtime)
│   ├── README.md           # What's in here and how to run it
│   ├── screenshots.py      # Regenerates docs/screenshot-*.png from the templates
│   ├── build_overview_pdf.py  # Regenerates docs/Aerodrome_Overview.pdf
│   └── check_docs.py       # Documentation drift detector (run by bump-version.sh)
├── update/                 # Drop a new release here to stage it for the Updates page
├── .backups/               # Auto-created — snapshots of previous installs after each update
├── logs/                   # Auto-created
└── aircraft_history.db     # Auto-created SQLite database
```

## Troubleshooting

**No aircraft showing up**

- Check the service is running: `sudo systemctl status aerodrome`
- Verify the receiver is reachable from the server:
  `curl http://YOUR_RECEIVER_IP:8080/data/aircraft.json`
- Check logs: `sudo journalctl -u aerodrome -n 50`

**Can't access the web UI**

- Open the firewall port: `sudo ufw allow 8000/tcp`
- Confirm the service is listening: `ss -tln | grep 8000`

**Watchlist tail number not resolving**

- Server needs internet for hexdb.io lookups.
- Use ICAO hex directly as a fallback if a tail can't be resolved.

**Database getting too large**

- Reduce `all_days` in `config.yaml`.
- Cleanup runs automatically on every poll cycle.

**Performance feels slow**

- Open `/performance` and run the diagnostic. The auto-generated
  hints at the bottom usually point at the culprit (large database,
  slow disk, single hot table). For more detail, see
  `docs/PERFORMANCE.md` or the in-app Performance documentation tab.
