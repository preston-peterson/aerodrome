#!/usr/bin/env python3
# Version: 3.4.11
"""
main.py — Aerodrome ADS-B Tracker

Usage:
    python3 main.py start     Start the tracker
    python3 main.py stop      Stop the tracker
    python3 main.py status    Check status
    python3 main.py restart   Stop then start
"""

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Tuple

import yaml
import uvicorn

from collector import (init_db, build_watchlist_lookup, fetch_and_store,
                       set_db_path, _open_db_conn, set_db_tuning_profile,
                       set_receiver_location, check_capacity_alerts)
from server import get_app

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.yaml.example"
PID_FILE = BASE_DIR / ".tracker.pid"


def _deep_merge_missing(target: dict, defaults: dict, path: str = "") -> list:
    """
    Walk `defaults`, inserting any missing keys into `target` in place.
    Does NOT overwrite existing values — only fills gaps.
    Returns a flat list of dotted key paths that were added.
    """
    added = []
    for key, default_val in defaults.items():
        full_path = f"{path}.{key}" if path else key
        if key not in target:
            target[key] = default_val
            added.append(full_path)
        elif isinstance(default_val, dict) and isinstance(target.get(key), dict):
            # Recurse into nested dicts
            added.extend(_deep_merge_missing(target[key], default_val, full_path))
    return added


# How many config.yaml.bak.* files to retain; older ones are deleted
# when a new backup is created.
CONFIG_BACKUP_KEEP = 5


def _prune_config_backups():
    """Keep only the CONFIG_BACKUP_KEEP most-recent config.yaml.bak.* files
    in the config's directory. Silently ignores filesystem errors."""
    try:
        parent = CONFIG_PATH.parent
        backups = sorted(
            parent.glob("config.yaml.bak.*"),
            # Sort by the timestamp suffix when possible; fall back to mtime
            key=lambda p: p.name,
            reverse=True,
        )
        for old in backups[CONFIG_BACKUP_KEEP:]:
            try:
                old.unlink()
                print(f"Pruned old config backup: {old.name}")
            except OSError as e:
                print(f"WARNING: Could not delete old backup {old.name}: {e}")
    except Exception as e:
        print(f"WARNING: Could not prune config backups: {e}")


def migrate_config(config: dict) -> dict:
    """
    Check the user's config against config.yaml.example for missing keys.
    If any are missing, back up the current config and merge in defaults.
    Returns the possibly-updated config dict.

    Uses ruamel.yaml internally to preserve the user's existing comments,
    indentation, and key order when writing the merged config back.
    """
    if not CONFIG_EXAMPLE_PATH.exists():
        # No example shipped — nothing to compare against. Likely a dev checkout.
        return config

    try:
        from ruamel.yaml import YAML
    except ImportError:
        # ruamel.yaml not installed — fall back to safe merge without comment preservation
        return _migrate_config_plain(config)

    ryaml = YAML()
    ryaml.preserve_quotes = True
    ryaml.indent(mapping=2, sequence=4, offset=2)

    try:
        with open(CONFIG_EXAMPLE_PATH) as f:
            example = ryaml.load(f) or {}
    except Exception as e:
        print(f"WARNING: Could not parse {CONFIG_EXAMPLE_PATH.name}: {e}")
        return config

    try:
        with open(CONFIG_PATH) as f:
            live = ryaml.load(f) or {}
    except Exception as e:
        print(f"WARNING: Could not re-parse {CONFIG_PATH.name} for migration: {e}")
        return config

    added = _deep_merge_missing(live, example)
    if not added:
        return config

    # Back up first
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = CONFIG_PATH.with_name(f"config.yaml.bak.{ts}")
    try:
        shutil.copy2(CONFIG_PATH, backup_path)
    except Exception as e:
        print(f"WARNING: Could not back up config before migration: {e}")
        return config
    _prune_config_backups()

    # Write merged config back (preserves comments from the user's original)
    try:
        with open(CONFIG_PATH, "w") as f:
            ryaml.dump(live, f)
    except Exception as e:
        print(f"ERROR: Could not write merged config: {e}")
        return config

    print("=" * 60)
    print(f"Config migrated — added {len(added)} new key(s) from this release:")
    for key in added:
        print(f"  + {key}")
    print(f"Previous config backed up to: {backup_path.name}")
    print(f"Your existing settings and comments were preserved.")
    print("=" * 60)

    # Reload via plain PyYAML so the rest of the app gets a vanilla dict
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _migrate_config_plain(config: dict) -> dict:
    """Fallback migration using PyYAML only (loses comments but stays safe)."""
    try:
        with open(CONFIG_EXAMPLE_PATH) as f:
            example = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"WARNING: Could not parse {CONFIG_EXAMPLE_PATH.name}: {e}")
        return config

    added = _deep_merge_missing(config, example)
    if not added:
        return config

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = CONFIG_PATH.with_name(f"config.yaml.bak.{ts}")
    try:
        shutil.copy2(CONFIG_PATH, backup_path)
    except Exception as e:
        print(f"WARNING: Could not back up config before migration: {e}")
        return config
    _prune_config_backups()

    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        print(f"ERROR: Could not write merged config: {e}")
        return config

    print("=" * 60)
    print(f"Config migrated (plain mode) — added {len(added)} new key(s):")
    for key in added:
        print(f"  + {key}")
    print(f"Previous config backed up to: {backup_path.name}")
    print("Note: install 'ruamel.yaml' to preserve comments during future migrations.")
    print("=" * 60)

    return config


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        # First-run convenience: copy the example if present
        if CONFIG_EXAMPLE_PATH.exists():
            shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
            print(f"Created config.yaml from config.yaml.example — edit it before starting.")
        else:
            print(f"ERROR: {CONFIG_PATH} not found.")
            sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    for section in ["receiver", "web", "data", "logging", "retention"]:
        if section not in config:
            print(f"ERROR: Missing '{section}' in config.yaml")
            sys.exit(1)

    # Auto-merge any new keys from the shipped example (handles upgrades)
    config = migrate_config(config)

    return config


def setup_logging(config: dict):
    log_dir = BASE_DIR / config["logging"]["dir"]
    log_dir.mkdir(exist_ok=True)
    level = getattr(logging, config["logging"]["level"].upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "tracker.log", mode="a"),
            logging.StreamHandler(),
        ],
    )


def run_collector(config: dict, stop_event: threading.Event):
    logger = logging.getLogger("adsb.main")
    interval = config["receiver"]["poll_interval"]
    logger.info(f"Collector started — polling every {interval}s")

    # Track last-seen watchlist to detect changes and log them
    last_watchlist_sig = None

    while not stop_event.is_set():
        try:
            # Rebuild watchlist on every poll so UI/config changes take effect live.
            # Tail-number resolution is cached, so this is cheap after the first run.
            current_watchlist = config.get("watchlist") or []
            sig = tuple(sorted(
                (e.get("icao") or "", e.get("tail") or "", e.get("callsign") or "",
                 e.get("model") or "", e.get("label") or "")
                for e in current_watchlist
            ))
            if sig != last_watchlist_sig:
                logger.info(f"Watchlist changed ({len(current_watchlist)} entries) — rebuilding lookup")
                watchlist_lookup = build_watchlist_lookup(config)
                last_watchlist_sig = sig

            fetch_and_store(config, watchlist_lookup)
            # v2.50.31: capacity-alert evaluation. Internally rate-limited
            # to once per CAPACITY_CHECK_INTERVAL_SEC (60s default), so
            # calling it every poll iteration is fine even at sub-60s
            # poll cadences. Never raises (best-effort try/except inside).
            check_capacity_alerts(config)
        except Exception as e:
            logger.error(f"Collector error: {e}", exc_info=True)
        stop_event.wait(interval)
    logger.info("Collector stopped")


def start(config: dict):
    logger = logging.getLogger("adsb.main")

    if PID_FILE.exists():
        # v2.41.9: defensive parse. If the file is empty or garbage
        # (e.g. a crash during write, or a full-disk that truncated it),
        # treat it as stale and continue rather than crashing out.
        try:
            pid_text = PID_FILE.read_text().strip()
            pid = int(pid_text) if pid_text else 0
        except (OSError, ValueError):
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
                print(f"Already running (PID {pid}). Use 'restart' or 'stop' first.")
                sys.exit(1)
            except ProcessLookupError:
                PID_FILE.unlink(missing_ok=True)
        else:
            PID_FILE.unlink(missing_ok=True)

    PID_FILE.write_text(str(os.getpid()))
    # v2.50.13: set the SQLite tuning profile BEFORE init_db so the
    # init connection itself uses the right cache_size/mmap_size/temp_store.
    # init_db's connection mostly does schema setup so cache benefit is
    # small, but doing it here means there's no point in startup where a
    # connection runs with stale/default tuning.
    tuning_cfg = (config.get("data") or {}).get("tuning") or {}
    set_db_tuning_profile(tuning_cfg.get("profile") or "auto")
    init_db(config["data"]["db_file"])

    # v2.51.0: apply any pending schema migrations. init_db is idempotent
    # for existing schema (CREATE TABLE IF NOT EXISTS) but doesn't handle
    # the search-feature schema introduced in v2.51.0 — that needs ALTER
    # TABLE adds, FTS5 setup, and backfill. The migration framework owns
    # that work, runs once per install ever, and is a no-op on subsequent
    # startups (version-stamped). On failure, the transaction rolls back
    # and we abort startup — running against a partially-migrated DB
    # would corrupt data.
    import sqlite3 as _sq
    from schema_migrations import apply_schema_migrations as _apply_migrations
    from schema_migrations import set_v6_backfill_config as _set_v6_cfg
    # Read VERSION for the migration record (so the schema_version table
    # captures which app version applied each migration — useful for
    # forensic debugging if a future migration bug needs tracing back to
    # a specific release).
    try:
        _app_version = (BASE_DIR / "VERSION").read_text().strip()
    except Exception:
        _app_version = "unknown"
    # v2.88.0: hand migration v6 the timezone + gap_min values it needs
    # to compute today's local-midnight bucket and detect session
    # boundaries during backfill. CONFIG isn't pushed into server.py's
    # globals yet at this point in startup, so we read directly from the
    # parsed YAML. No-op for already-applied migrations.
    _stats_cfg = (config.get("stats") or {})
    _set_v6_cfg(
        (_stats_cfg.get("timezone") or "").strip(),
        _stats_cfg.get("track_gap_minutes"),
    )
    _mconn = _sq.connect(config["data"]["db_file"])
    try:
        _mres = _apply_migrations(_mconn, _app_version)
        if not _mres["ok"]:
            logger.error(
                f"Schema migration failed at version "
                f"{_mres['starting_version']}: {_mres.get('error')}"
            )
            logger.error(
                "Refusing to start with a partially-migrated database. "
                "DB is unchanged (transaction rolled back). Restore from "
                "backup if needed, or report this issue with the error above."
            )
            sys.exit(1)
        if _mres["applied"]:
            for _entry in _mres["applied"]:
                logger.info(
                    f"Schema migrated to v{_entry['version']} "
                    f"in {_entry['duration_sec']}s — {_entry['description']}"
                )

        # v2.87.3: schema pre-flight check. Verifies the columns the
        # Stats endpoint's queries expect actually exist in the live
        # schema. Logs warnings for any drift but doesn't block
        # startup — the goal is to surface a v2.86.4-class bug
        # (column-name typo crashes the entire Stats tab) at startup
        # rather than at next user click. See the schema_migrations
        # module-level docstring for the full rationale and
        # discussion of what this catches vs misses.
        try:
            from schema_migrations import verify_stats_schema as _verify_schema
            _verify_schema(_mconn)
        except Exception as _e:
            # Pre-flight is a hedge — its own failure should never
            # block server startup. Log and move on.
            logger.warning(
                f"Schema preflight check itself failed (non-fatal): {_e}"
            )
    finally:
        _mconn.close()

    # v2.49.0: make the db path available to collector's hexdb resolver so
    # its cache persists across restarts.
    set_db_path(config["data"]["db_file"])

    # v2.60.1 (Phase 1A.5 perf): push receiver location into the
    # collector so subsequent position writes populate
    # seen_aircraft.last_distance. Pulled from CONFIG; None when the
    # receiver isn't configured (collector stores NULL in that case).
    _rcv = (config.get("receiver") or {})
    set_receiver_location(_rcv.get("latitude"), _rcv.get("longitude"))

    # v2.60.1: backfill seen_aircraft.last_distance for ALL existing
    # rows using the current receiver location. Runs every startup
    # because it's cheap (~7K row UPDATE on a typical install, single-
    # digit seconds even on a Pi) and keeps the column consistent
    # across edge cases — first install after the v3 migration, user
    # changed receiver location while the service was stopped, or
    # any race where the collector wrote a row with stale receiver
    # config.
    from server import _recompute_all_last_distance
    try:
        _recompute_all_last_distance(
            config["data"]["db_file"],
            rlat=_rcv.get("latitude"),
            rlon=_rcv.get("longitude"),
        )
    except Exception as e:
        # Non-fatal — distance sort will degrade to "NULLs sort last"
        # for rows that didn't get computed. Log but proceed.
        logger.warning(f"Initial last_distance recompute failed: {e}")

    stop_event = threading.Event()

    def shutdown(signum, frame):
        logger.info("Shutdown signal received...")
        stop_event.set()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    collector_thread = threading.Thread(
        target=run_collector, args=(config, stop_event), daemon=True
    )
    collector_thread.start()

    host = config["web"]["host"]
    port = config["web"]["port"]
    app = get_app(config, str(CONFIG_PATH))

    # v2.50.35: pre-flight bind validation. Catches the install footgun
    # where web.host is set to a specific LAN IP (e.g. "192.168.1.50")
    # that isn't actually bindable on this machine — the IP changed,
    # the interface is down, or the config was copied from a different
    # machine. Without this check, uvicorn.run() would attempt the bind
    # and fail in a way that's hard to see (output buffering swallows
    # the error under systemd; the process can stay alive in some code
    # paths). With this check we exit early with a clear, actionable
    # error message and a non-zero status code.
    #
    # We test by trying to actually bind a socket to (host, port), then
    # closing it. There's a tiny race between our bind+close and
    # uvicorn's bind, but anything that happens in that window would
    # also have been caught by uvicorn's own bind — we're checking for
    # "address not assigned to this machine", not "port already in use",
    # and the former is the common silent-failure case.
    _bind_ok, _bind_err = _preflight_bind_check(host, port)
    if not _bind_ok:
        logger.error(f"Pre-flight web bind check FAILED: {_bind_err}")
        logger.error(
            f"Cannot bind to {host}:{port} on this machine. "
            f"Most common cause: 'web.host' in config.yaml is set to a "
            f"specific IP that isn't currently assigned to a local "
            f"network interface."
        )
        logger.error(
            f"Fix: edit {CONFIG_PATH} and set 'web.host' to '0.0.0.0' "
            f"to listen on all interfaces (recommended), or to a value "
            f"that matches an actual IP on this machine. Then restart."
        )
        sys.exit(2)

    r = config["retention"]
    # v2.50.2: read version dynamically rather than hardcoding. The banner
    # used to say "v2.40.1" through ~10 releases because the string here
    # was never updated alongside the VERSION file. Read it the same way
    # server.py and other places do.
    try:
        _vfile = Path(__file__).parent / "VERSION"
        _vstr = _vfile.read_text().strip() if _vfile.exists() else "unknown"
    except Exception:
        _vstr = "unknown"
    # Banner is printed AFTER pre-flight passes, so we never advertise
    # a URL the user can't actually reach.
    print(f"""
╔══════════════════════════════════════════════════╗
║          Aerodrome  v{_vstr:<28}║
╠══════════════════════════════════════════════════╣
║  Web UI:    http://{host}:{port:<5}                  ║
║  Receiver:  {config['receiver']['ip']}:{config['receiver']['port']:<5}               ║
║  Polling:   every {config['receiver']['poll_interval']}s                          ║
║  Retention: mil {r['military_days']}d / watch {r['watchlist_days']}d / all {r['all_days']}d     ║
╚══════════════════════════════════════════════════╝
    """, flush=True)
    # flush=True so the banner reaches the journal (or terminal) BEFORE
    # uvicorn.run() takes over and the buffer flush behavior changes.
    # Under systemd the stdout pipe is block-buffered — without flush=True
    # the banner would only flush on shutdown (real bug seen in v2.50.34).

    uvicorn.run(app, host=host, port=port, log_level="warning")


def _preflight_bind_check(host: str, port: int) -> Tuple[bool, str]:
    """Verify that (host, port) is bindable on this machine.

    Returns (ok, error_message). ok=True means the bind succeeded;
    we close the test socket immediately so uvicorn can claim the
    same address microseconds later.

    Special-cases: '0.0.0.0', '::', 'localhost', '127.0.0.1' are
    always bindable (they don't require a specific interface to
    be up). For these we just verify the port isn't in use by
    something else, which is what uvicorn would also catch with a
    clear "address already in use" error.
    """
    import socket
    try:
        # Use AF_INET6 if host looks like IPv6, else AF_INET.
        # For 'localhost' AF_INET is fine (loopback works on both).
        family = socket.AF_INET6 if ":" in host and host != "0.0.0.0" else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        # SO_REUSEADDR mirrors uvicorn's own behavior — without it, a
        # recently-killed instance might leave TIME_WAIT sockets that
        # would make our test fail when uvicorn would succeed.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, int(port)))
            return (True, "")
        finally:
            s.close()
    except OSError as e:
        # errno 99 (Cannot assign requested address) = the host isn't
        # a local IP. errno 98 (Address already in use) = port collision.
        # Both deserve clear messages but the first is the install
        # footgun we're really targeting.
        return (False, f"socket.bind({host!r}, {port}) → {type(e).__name__}: {e}")
    except Exception as e:
        return (False, f"unexpected error during bind check: {e}")


def stop(config: dict):
    if not PID_FILE.exists():
        print("Not running (no PID file).")
        return
    # v2.41.9: defensive parse — tolerate empty/garbage PID files.
    try:
        pid_text = PID_FILE.read_text().strip()
        pid = int(pid_text) if pid_text else 0
    except (OSError, ValueError):
        pid = 0
    if pid <= 0:
        print("PID file is empty or corrupt — cleaning up.")
        PID_FILE.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent stop signal (PID {pid}).")
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                print("Stopped.")
                PID_FILE.unlink(missing_ok=True)
                return
        print(f"WARNING: May still be running. Force: kill -9 {pid}")
    except ProcessLookupError:
        print("Was not running (stale PID removed).")
        PID_FILE.unlink(missing_ok=True)


def status(config: dict):
    import requests

    running = False
    pid = None
    if PID_FILE.exists():
        # v2.41.9: defensive parse — tolerate empty/garbage PID files.
        try:
            pid_text = PID_FILE.read_text().strip()
            pid = int(pid_text) if pid_text else None
        except (OSError, ValueError):
            pid = None
        if pid:
            try:
                os.kill(pid, 0)
                running = True
            except ProcessLookupError:
                pass

    receiver = config["receiver"]
    url = f"http://{receiver['ip']}:{receiver['port']}{receiver['path']}"
    receiver_ok = False
    try:
        r = requests.get(url, timeout=5)
        receiver_ok = r.status_code == 200
    except Exception:
        pass

    db_path = config["data"]["db_file"]
    db_exists = os.path.exists(db_path)
    stats = {"military": 0, "watchlist": 0, "all": 0}
    if db_exists:
        import sqlite3
        conn = _open_db_conn(db_path)
        now = int(time.time())
        for table, key, days_key in [
            ("military_sightings", "military", "military_days"),
            ("watchlist_sightings", "watchlist", "watchlist_days"),
            ("all_sightings", "all", "all_days"),
        ]:
            cutoff = now - (config["retention"][days_key] * 86400)
            stats[key] = conn.execute(
                f"SELECT COUNT(DISTINCT icao) FROM {table} WHERE seen_at >= ?", (cutoff,)
            ).fetchone()[0]
        conn.close()

    G = '\033[32m\033[1m'
    R = '\033[31m\033[1m'
    RST = '\033[0m'
    r = config["retention"]

    print(f"\n  Tracker:    {G+'running'+RST+f' (PID {pid})' if running else R+'not running'+RST}")
    print(f"  Receiver:   {G+'reachable'+RST if receiver_ok else R+'unreachable'+RST}  ({url})")
    print(f"  Database:   {G+'exists'+RST if db_exists else R+'missing'+RST}  ({db_path})")
    print(f"  Retention:  military {r['military_days']}d / watchlist {r['watchlist_days']}d / all {r['all_days']}d")
    print(f"  Military:   {stats['military']} unique aircraft")
    print(f"  Watchlist:  {stats['watchlist']} unique aircraft")
    print(f"  All:        {stats['all']} unique aircraft")
    print()


def main():
    parser = argparse.ArgumentParser(description="Aerodrome — ADS-B Tracker")
    parser.add_argument("command", choices=["start", "stop", "status", "restart"])
    args = parser.parse_args()
    config = load_config()
    setup_logging(config)

    if args.command == "start":
        start(config)
    elif args.command == "stop":
        stop(config)
    elif args.command == "status":
        status(config)
    elif args.command == "restart":
        stop(config)
        time.sleep(1)
        start(config)


if __name__ == "__main__":
    main()
