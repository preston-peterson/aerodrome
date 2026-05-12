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
- [Demo mode (v3.1.0)](#demo-mode-v310)
- [Multi-distro support (v3.2.0)](#multi-distro-support-v320)
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
- A Linux server with systemd and a supported package manager. As of v3.2.0,
  Aerodrome supports four distro families as tier-1 (tested before each
  release):
    - **Debian-family** — Ubuntu 22.04+, Debian 12+, Raspberry Pi OS, Linux Mint, Pop!_OS
    - **Fedora-family** — Fedora 40+, RHEL 9+, Rocky 9+, AlmaLinux 9+
    - **Arch-family** — Arch Linux (rolling), Manjaro, EndeavourOS
    - **openSUSE** — Tumbleweed, Leap 15.5+
  Any other distro with systemd + apt-get/dnf/pacman/zypper will likely
  install (the bootstrap will warn and prompt before proceeding); see
  *Multi-distro support* below for details.
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

Aerodrome has two install paths. The curl one-liner is the canonical
modern path — it auto-detects your platform, installs prereqs, downloads
the latest release with checksum verification, prompts for the bare
minimum config, and starts the service. The manual path stays
fully-supported for users who want to inspect the bootstrap before
running it, install offline, pin to a specific release, or work from a
git checkout.

### Option 1 — Curl install (recommended)

On a fresh Ubuntu 22.04+ or Debian 12+ host with no Aerodrome installed:

```bash
bash <(curl -fsSL https://install.aerodromeadsb.com)
```

That's it. The bootstrap walks through eight steps:

1. **Platform check** — confirms a recognized Debian-family system. On
   unrecognized distros, prints a warning and requires `--force` to
   proceed.
2. **Existing-install check** — refuses to overwrite an existing install
   and points you at the in-app updater instead.
3. **Prerequisites** — `apt`-installs `unzip` and `python3-venv` if
   they're missing.
4. **Release resolution** — queries the GitHub Releases API for the
   latest release tag (or accepts `--version vX.Y.Z` to pin).
5. **Download + verify** — pulls `aerodrome-vX.Y.Z.zip` and the matching
   `.sha256` file from the GitHub Releases page, verifies the checksum,
   and refuses to proceed if it doesn't match.
6. **Initial configuration** — interactive prompts for the bare minimum:
   receiver IP/port, optional receiver latitude/longitude (enables the
   Distance column), and distance unit. Time zone is auto-detected from
   `/etc/timezone` or `timedatectl` and used silently — change it later
   in the web UI's Configuration page.
7. **Extract + handoff** — extracts to `/opt/aerodrome` (configurable with
   `--prefix`), patches `config.yaml` with the prompted values, and
   hands off to the bundled `install.sh`.
8. **install.sh runs** — creates a Python venv, installs dependencies,
   writes the systemd unit, installs a scoped `sudoers.d` rule for the
   in-UI restart button, and starts the service.

When complete, open `http://your-host:8000/` and visit the gear menu →
Configuration to adjust the auto-detected timezone, set up a watchlist,
enable push notifications, and configure the rest.

**Useful bootstrap flags:**

```
--prefix <path>        Install directory (default: /opt/aerodrome)
--version <vX.Y.Z>     Pin to a specific release (default: latest)
--from-zip <path>      Skip the GitHub fetch and install from a local zip
--receiver-ip <ip>     ADS-B receiver IP (skips prompt)
--receiver-port <n>    Receiver port (skips prompt; default 8080)
--lat <n>              Receiver latitude (skips prompt)
--lon <n>              Receiver longitude (skips prompt)
--distance-unit <u>    mi / nmi / km (skips prompt; default mi)
--timezone <tz>        IANA tz name (skips prompt; default: system)
--force                Bypass OS-compat warning on unrecognized distros
-y, --yes              Accept all defaults non-interactively
-h, --help             Show full help and exit
```

Run `bash <(curl -fsSL https://install.aerodromeadsb.com) --help` to see
the same list at any time.

**What the bootstrap does NOT do**: configure remote access (Tailscale,
Cloudflare Tunnel, reverse proxy), set up backups beyond what Aerodrome
manages internally, or expose the service to the internet. See the
[Remote access](#remote-access-optional) section for those.

### Option 2 — Manual install (offline, git, version-pinned)

Use this path if you want to inspect the bootstrap before running it,
install offline from a release zip you've already downloaded, work from
a git checkout for development, or otherwise step outside the curl flow.

#### 1. Download

```bash
# From the GitHub Releases page:
#   https://github.com/preston-peterson/aerodrome/releases
# Or clone the repository:
git clone https://github.com/preston-peterson/aerodrome.git
cd aerodrome
```

#### 2. Configure

Edit `config.yaml` to set your receiver's address:

```yaml
receiver:
  ip: "192.0.2.10"            # Replace with your ADS-B receiver IP
  port: 8080                  # Your receiver port
  path: "/data/aircraft.json" # Path to aircraft JSON
```

#### 3. Deploy to your server

If running on the same machine, skip to step 4. Otherwise, copy to your
server:

```bash
rsync -av aerodrome/ user@your-server:/opt/aerodrome/
ssh user@your-server
cd /opt/aerodrome
```

#### 4. Install

> **Note:** Both `install.sh` and `uninstall.sh` need to be made
> executable before running. File-transfer tools like rsync, scp, zip
> extraction, and git checkouts on Windows often strip the executable
> bit, so this step is almost always needed.
>
> Alternatively, you can skip the `chmod` and run the scripts via
> `sudo bash install.sh` — `bash` doesn't care about the execute bit.
> Both forms work identically; pick whichever you prefer.

On the server, in your `/opt/aerodrome` directory:

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

**Bootstrap-from-local-zip variant**: if you'd prefer the bootstrap's
prompts and prereq detection without the GitHub fetch, the bootstrap
itself ships in the zip at `scripts/bootstrap.sh` and accepts a
`--from-zip` flag:

```bash
bash scripts/bootstrap.sh --from-zip ~/Downloads/aerodrome-vX.Y.Z.zip
```

This is the same flow as the curl one-liner but installs from your local
zip instead of downloading from GitHub. Useful for air-gapped installs.

## Demo mode (v3.1.0)

Demo mode lets you install and explore Aerodrome with simulated aircraft
data when you don't have a real ADS-B receiver to connect to yet. The
dashboard comes alive with 50 simulated aircraft, watchlist hits,
occasional military traffic (the simulated fleet keeps ~5% in the US
military hex range), and occasional emergency squawks — so you can see
every part of the UI exercised before committing real hardware.

### Installing in demo mode

Three paths, depending on how you're installing:

**Via the curl one-liner — interactive:**

```bash
bash <(curl -fsSL https://install.aerodromeadsb.com)
```

The bootstrap will ask whether you have a real receiver or want demo
mode. Pick option 2 — it skips the receiver IP/port prompts but still
asks for latitude/longitude (those become the simulated receiver's home
position; aircraft cluster around them).

**Via the curl one-liner — non-interactive:**

```bash
bash <(curl -fsSL https://install.aerodromeadsb.com) \
    --demo --lat 44.84 --lon -88.23
```

**Via a manually-downloaded release zip:**

```bash
unzip aerodrome-vX.Y.Z.zip
cd aerodrome-vX.Y.Z/
chmod +x install.sh
./install.sh --demo --home-lat 44.84 --home-lon -88.23
```

This third path is independent of the bootstrap — useful for air-gapped
installs or when you want to inspect the install script before running
it. `install.sh --demo` patches `config.yaml` automatically, runs the
demo-watchlist seeder, and installs the synthetic-feeder systemd unit.

### What's running on a demo install

Two systemd services side by side:

- **`aerodrome.service`** — the main tracker. Reads `config.yaml` (which
  has `receiver.ip: 127.0.0.1` and `receiver.port: 8080` in demo mode)
  and polls the synthetic feeder at the same URL it would poll a real
  receiver.
- **`aerodrome-synthetic-feeder.service`** — the simulator. Serves
  `/data/aircraft.json` on `127.0.0.1:8080` with a deterministic fleet
  of 50 aircraft (seed locked to `1903`, so the same simulated aircraft
  appear on every demo install everywhere and persist across restarts).

The feeder is bundled as `tools/synthetic_feeder/` in the release zip,
so demo installs don't need additional downloads.

### What you see in demo mode

- **A yellow banner** across every page reading "Demo mode — showing
  simulated data. Configure real receiver" with a link to the switch-to-real
  wizard.
- **A `[DEMO]` prefix on every push notification** — so if you've
  configured ntfy, messages arrive as "[DEMO] Military aircraft spotted"
  rather than alarming you with what looks like a real alert.
- **A confirmation dialog when you click "Track ↗"** on any aircraft,
  explaining that the ICAO is simulated and external trackers won't
  find it. "Continue anyway" still opens the link if you want to see
  what FlightAware/airplanes.live returns; "Cancel" closes the dialog.
- **An 8-aircraft starter watchlist** seeded at install time. The
  entries are labelled "Demo: regular #1" through "#8" so they're easy
  to identify (and easy to clear when you switch to real mode).

### Switching to a real receiver

When you have an ADS-B receiver ready, the in-app switch-to-real wizard
handles the transition cleanly. Visit **gear menu → Configuration →
Demo tab → Switch to real receiver →**, or go directly to
`/setup/switch-to-real`.

The wizard is a 4-step flow:

1. **Confirm intent.** With an explicit warning that the demo database,
   demo watchlist, and any accumulated stats will be permanently
   deleted — switching to real mode is destructive of demo state.
2. **Enter real receiver details.** IP, port, optional latitude and
   longitude, optional path override.
3. **Execute.** The server tests reachability of the new receiver first
   (catches typo'd IPs before destroying anything), then stops and
   removes the synthetic-feeder service, deletes the demo database,
   clears the watchlist, updates `config.yaml`, and restarts the
   aerodrome service. Live progress shows each step.
4. **Confirmation.** Your real receiver should start populating the
   dashboard within ~60 seconds. The yellow demo banner disappears on
   the next status poll; the `[DEMO]` prefix drops off notifications
   automatically.

### Uninstalling a demo install

`./uninstall.sh` removes both services (`aerodrome.service` and
`aerodrome-synthetic-feeder.service`) the same way it removes a real
install. The synthetic feeder is detected and cleaned up automatically;
no special demo-mode uninstall flag is needed.

## Multi-distro support (v3.2.0)

Aerodrome supports four Linux distro families as tier-1 (tested by the
maintainer before each release), and best-effort support for any other
systemd-based distro with one of the recognized package managers.

### Tier-1 families

The bootstrap and `install.sh` automatically detect the distro family
from `/etc/os-release` and use the appropriate package manager:

| Family       | Detection IDs                                                    | Refresh                 | Install                              |
|--------------|------------------------------------------------------------------|-------------------------|--------------------------------------|
| **Debian**   | `debian`, `ubuntu`, `raspbian`, `linuxmint`, `pop`, `elementary`, `neon`, `kali`, `parrot` | `apt-get update`         | `apt-get install -y`                 |
| **Fedora**   | `fedora`, `rhel`, `centos`, `rocky`, `almalinux`, `amzn`, `ol`   | *(auto-refresh on install)* | `dnf install -y`                    |
| **Arch**     | `arch`, `manjaro`, `endeavouros`, `garuda`, `artix`, `cachyos`   | `pacman -Sy`            | `pacman -S --needed`                 |
| **openSUSE** | `opensuse-*`, `sles`, `sled`                                     | `zypper refresh`        | `zypper install`                     |

For derivatives whose ID isn't listed, the family is inferred from
`ID_LIKE` in `/etc/os-release` — so most downstream distros that set
that field correctly (Pop!_OS reports `ID_LIKE=ubuntu debian`, etc.)
fall back to their parent family automatically.

### Package name differences

Python's venv module is a separate package on Debian-family
(`python3-venv`) but bundled into the main `python3`/`python` package on
Fedora, Arch, and openSUSE. The bootstrap handles this difference
transparently — it tests whether `python3 -c 'import ensurepip'` works
and only requests `python3-venv` if needed. You shouldn't have to think
about this.

On Arch and Arch derivatives the Python package is `python` (not
`python3`) and pip is `python-pip`. Everything else is identical across
families.

### Install location (v3.3.0)

Starting with v3.3.0, fresh installs default to `/opt/aerodrome` on
every distro. This is the FHS-blessed location for "add-on application
software packages" and matches the convention every other major
third-party Linux service uses. Three reasons it's the default:

1. **Works on every distro out of the box.** SELinux's targeted policy
   (Fedora, RHEL, openSUSE Tumbleweed) and AppArmor are both permissive
   in `/opt/`. Installing under `/home/` on SELinux-enforcing systems
   doesn't work — systemd can't `ExecStart` a binary in a user's home
   because the targeted policy denies it. `/opt/` avoids that entirely.
2. **One mental model.** All docs, scripts, examples, and support
   troubleshooting say `/opt/aerodrome`. No "your install location
   depends on your distro" footnotes.
3. **Cleaner backups.** Sysadmin convention: back up `/opt` and `/etc`,
   leave `/home` to user-managed sync. Aerodrome data lands in the
   right pile by default.

The bootstrap creates `/opt/aerodrome` via `sudo mkdir` and then chowns
it to the install user, so the rest of the install runs unprivileged
just like before. The systemd unit still runs as your regular user (not
root), the config file is still user-owned and editable, and the in-app
updater still works without needing sudo for file operations.

**Override:** Pass `--prefix <path>` to the bootstrap to install
anywhere else:

```bash
bash <(curl -fsSL https://install.aerodromeadsb.com) --prefix ~/aerodrome
```

This restores the pre-v3.3.0 layout if you have a strong preference.
It'll only work cleanly on distros without SELinux enforcement
(Debian/Ubuntu/Arch/openSUSE-Leap).

**Existing installs at `~/aerodrome`:** keep working untouched. The
bootstrap detects an existing install at any `--prefix` location
(VERSION + main.py present) and upgrades it in place rather than
treating it as a fresh install. No forced migration. If you ever want
to move an existing install to `/opt/aerodrome`, the procedure is:
uninstall (keeping data), reinstall to `/opt/aerodrome`, copy your old
`config.yaml` + `aircraft_history.db` over. The web UI's
*Configuration → Backup & Restore* page does that copy step for you
via a full backup/restore.

### Firewall handling

On Fedora, RHEL family, and openSUSE Tumbleweed, `firewalld` is active
by default with a restrictive `public` zone that blocks port 8000.
Aerodrome would be reachable from the install host (`localhost:8000`)
but not from other devices on the network. The bootstrap detects this
state and prompts to open port 8000 persistently on the public zone:

```
firewalld is active. Port 8000 (the web UI) is currently closed.
  Open port 8000 in firewalld so you can reach the dashboard? [Y/n]
```

Default answer is yes — almost every Aerodrome install wants the
dashboard reachable from a laptop on the same network. Say no if you
have a more restrictive zone setup or want to handle firewall rules
yourself. The exact commands the bootstrap runs are:

```bash
sudo firewall-cmd --add-port=8000/tcp --permanent
sudo firewall-cmd --reload
```

These are also printed if you decline the prompt, so you can run them
later. Debian/Ubuntu and Arch don't enable firewalld by default and
will skip this step silently.

`uninstall.sh` mirrors the install side: on firewalld-active hosts
where port 8000 is currently open, it prompts to close it during
uninstall. The state stays symmetric — what the install opens, the
uninstall closes.

### Best-effort tier (tier-2)

If your distro isn't in the tier-1 list but has both `systemctl` AND one
of `apt-get`/`dnf`/`pacman`/`zypper`, the bootstrap will warn and prompt
to continue. It'll then proceed with whichever package manager it finds.
This covers obscure derivatives the explicit ID list doesn't enumerate.

Override the interactive prompt with `--yes` or `--force`:

```bash
bash <(curl -fsSL https://install.aerodromeadsb.com) --yes
```

### What's not supported (tier-3 hard refuse)

The bootstrap refuses to run if either of these is true:

- **No systemd**: Aerodrome needs systemctl to manage its service. Init
  systems like OpenRC, runit, or s6 (Alpine, Void, Gentoo without
  systemd, etc.) aren't supported. You'd need to write your own service
  scripts; see *Manual install on unsupported distros* below.
- **No recognized package manager**: if none of apt-get/dnf/pacman/zypper
  is on PATH, the bootstrap can't install prerequisites. The same
  manual-install path applies.

### Manual install on unsupported distros

If you're on a distro the bootstrap doesn't support but you still want
to run Aerodrome:

1. **Install prerequisites manually**: Python 3.10+, pip, the venv
   module, curl, unzip. The exact package names depend on your distro.
2. **Download the release zip**: from
   `https://github.com/preston-peterson/aerodrome/releases/latest`.
3. **Extract it** to a directory like `/opt/aerodrome`.
4. **Set up the virtualenv manually**:
   ```bash
   cd /opt/aerodrome
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
5. **Write your own service script** for your init system (OpenRC,
   runit, etc.). The command Aerodrome's service needs to run is:
   `/opt/aerodrome/venv/bin/python3 /opt/aerodrome/main.py start`. The
   service should run as a non-root user, restart on failure, and have
   network access.
6. **Edit `config.yaml`** to point at your receiver, and start your
   service.

Some Aerodrome features (the in-UI restart button, the sudoers-managed
update channel, the ntfy installer) assume systemd and will be inert on
non-systemd installs. The core dashboard, watchlist, stats, and
notifications all work fine without them.

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

Aerodrome has three update paths, listed by ease of use. The GitHub
channel is the canonical path for most users — discovery and apply are
both one-click from the dashboard. The local-zip upload is for users
who want to apply a specific release zip (e.g. a pre-release, a
locally-modified build, or a release zip pulled out-of-band). The
direct-rsync path is the scripted-deployment fallback.

### Option 1 — In-app GitHub channel (recommended)

No SSH, no downloads, no terminal.

1. **Open the Updates page** via the gear menu → "Check for updates" or
   directly at `/updates`.

2. **The GitHub card** at the top of the page shows the latest release
   on GitHub. The background scheduler checks this on a configurable
   cadence (Configuration → Updates tab — daily, weekly, monthly, or
   never). The Updates page reads cached state from the database and
   doesn't hit GitHub on every page load.

3. **When a new release is available**, the GitHub card shows a banner
   with the version number and an "Apply update" button. Click it and
   the server:
   - Fetches `aerodrome-vX.Y.Z.zip` and `.sha256` from the GitHub
     Releases page for that tag.
   - Verifies the SHA256 checksum (refuses to proceed if it doesn't
     match).
   - Stages the zip into the update directory and runs the same apply
     flow as a local-zip upload: backs up the current install,
     overwrites files (preserving config and data), reinstalls
     dependencies, and restarts the service.
   - Your browser reconnects automatically after ~6 seconds.

4. **You can also configure notification surfaces** so you don't have
   to visit the Updates page to know there's something to apply.
   Configuration → Updates tab has three independent toggles:
   - **Show banner on /updates** — in-card banner with the Apply button
     (default on).
   - **Light the gear-menu badge** — amber dot on the gear menu across
     every admin page when an update is available (default on).
   - **Send ntfy push notification** — push to your phone when a new
     release is discovered. Requires this flag AND
     `notifications.events.update_available` (on the Notifications tab)
     AND `notifications.enabled`. Fires once per transition (not every
     poll). Default cooldown is 24 hours.

5. **"Check now"** — the button on the GitHub card forces an immediate
   GitHub check regardless of the configured cadence. Useful if you've
   just seen a release announcement and don't want to wait for the next
   scheduled poll.

The whole channel can be disabled at Configuration → Updates → "Update
channel enabled" if you'd rather manage updates entirely manually.

### Option 2 — Upload via the web UI (local zip)

For applying a specific release zip rather than the latest GitHub
release. No SSH required.

1. **Download the release zip** from the GitHub Releases page (or
   wherever you got the zip).

2. **Open the Updates page** in your browser.

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
unpacked release into `/opt/aerodrome/update/` via rsync or scp:

```bash
rsync -av aerodrome/ user@your-server:/opt/aerodrome/update/
```

Either staging path (UI upload or rsync) ends up in the same place;
click **Apply** in the UI to finish.

### Option 3 — Direct rsync + restart (scripted / fallback)

If the web UI isn't available (service not running, permission issue
with the sudoers rule, etc.), or if you're scripting deployments, you
can update directly:

```bash
rsync -av \
  --exclude='aircraft_history.db' \
  --exclude='logs' \
  --exclude='venv' \
  --exclude='.tracker.pid' \
  --exclude='config.yaml' \
  --exclude='.backups' \
  --exclude='update' \
  aerodrome/ user@your-server:/opt/aerodrome/

ssh user@your-server "sudo systemctl restart aerodrome"
```

Single-line version for terminals that mangle line continuations:

```bash
rsync -av --exclude='aircraft_history.db' --exclude='logs' --exclude='venv' --exclude='.tracker.pid' --exclude='config.yaml' --exclude='.backups' --exclude='update' aerodrome/ user@your-server:/opt/aerodrome/
```

### Config auto-migration (all paths)

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
cd /opt/aerodrome
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
cd /opt/aerodrome
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
cd /opt/aerodrome
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

### Server-side backup (v3.4.0)

The browser-mediated Full backup flow works well for small databases
but becomes impractical above a few GB — the browser becomes the
bottleneck. v3.4.0 introduces a server-side flow that writes the
backup directly to `<install_dir>/.backups/` on the host, then leaves
the off-host transfer to `scp` or `rsync`. This is the right model
for production-scale databases where the history is hundreds of MB
per day and retention is months or years.

**Creating a server-side backup:** open Configuration → Backup &
Restore → Server-side backup → "Create server-side backup". The
backup writes to `<install_dir>/.backups/aerodrome-backup-<timestamp>.zip`
with a sidecar `.sha256` file. Uses the same SQLite online-backup API
as the browser flow, so the service keeps running. The UI shows live
progress; on a busy host with 50 GB of history, expect 10–20 minutes.

**Moving the backup off-host:** click the "Copy scp command" button
next to any backup in the list — it copies a templated `scp` command
to your clipboard:

```bash
scp user@aerodrome-host:/opt/aerodrome/.backups/aerodrome-backup-20260512-150658.zip ./
```

Edit the username if needed, then paste-and-run from your laptop or
backup destination. For routine off-host backup, set up rsync from
cron on a separate host:

```bash
# On the backup destination, daily:
rsync -avP user@aerodrome-host:/opt/aerodrome/.backups/ ./aerodrome-backups/
```

**Restoring from a server-side backup:** copy the zip back onto the
host's `.backups/` directory (via scp or `cp` if it's already on the
machine), then in the UI use Configuration → Backup & Restore →
Server-side backup → "Restore from a path on disk" with the full path.
The destructive-confirm modal shows what will be replaced and a
`.pre-restore` safety snapshot is created first, same as any other
restore.

**Which flow to use:**

| Database size | Recommended flow |
|---|---|
| Under ~500 MB | Browser-mediated Full backup is fastest |
| ~500 MB – 2 GB | Either works; browser upload may need patience |
| Over 2 GB | Server-side backup — browser uploads stop working reliably above this |
| Over 50 GB | Server-side backup + filesystem snapshot considerations |

At very large scale (hundreds of GB), the right pattern shifts again
to filesystem-level snapshots (ZFS, btrfs, LVM) or WAL shipping;
those are out of scope for Aerodrome itself but the server-side
flow gives you the building block.

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
