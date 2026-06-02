# Version: 3.4.55
"""
collector.py — ADS-B data fetcher and classifier.

Polls your receiver, stores EVERY aircraft in the 'all_sightings' table,
and additionally tags military + watchlist matches into their own tables.
"""

import logging
import math
import pathlib
import re
import requests
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# v2.79.0 (Phase 3 polish): centralized haversine. Pre-v2.79.0 the
# math lived inline at line ~2012; now it's in distance.py with the
# server-side haversine sites it shared semantics with.
from distance import haversine as _dist_haversine

logger = logging.getLogger("adsb.collector")


# v3.4.40: distinct exception type for the "URL is reachable but content
# isn't ADS-B" failure mode. Treated separately from ConnectionError
# (network down) and generic Exception (true unexpected error) so the
# log message and the user-facing notification can be specific about
# what's wrong — distinguishing "your receiver is unreachable" from
# "your receiver URL points at the wrong service" from "an unexpected
# error happened." All three flow through the same offline-threshold
# counter so a misconfiguration eventually triggers the same notify.
class _NonAdsbResponse(Exception):
    """Receiver URL returned 200 but the body isn't a recognizable
    ADS-B feed shape (HTML, non-JSON, or JSON without 'aircraft' key)."""
    pass


# =============================================================================
# Notification hooks
# =============================================================================
# The collector fires notifications for events like receiver offline, watchlist
# hits, new records, and so on. The notifier itself is managed by server.py
# (which owns the lifecycle — config reloads, install state, etc.). We keep a
# module-level reference that server.py can set after construction.
#
# All notifier interactions MUST be best-effort. If the notifier raises, we
# log and continue. Notifications must never break the collector.
_notifier = None  # type: Optional[Any]

# Track consecutive failed polls for receiver_offline / receiver_recovered
# detection. Resets to 0 on the first successful poll after an outage.
# We also remember whether we've already fired receiver_offline so we don't
# spam it every poll while the receiver stays down.
_consecutive_failed_polls = 0
_offline_notified = False
_last_offline_reason = ""  # The error message from the most recent failure


def get_collector_health():
    """v3.4.41: read-only snapshot of the collector's poll-failure state
    for the /api/status endpoint to surface on the Collector card.

    The Status page used to show only the SYMPTOM ("No data written
    yet" / "No writes in Xs") when the collector was wedged, which made
    diagnostic effort necessary to find the CAUSE (the user had to know
    to grep the journal log for the actual error). The collector
    already knows the cause — `_last_offline_reason` carries the most
    recent fetch-failure message, including the specific v3.4.40
    non-ADS-B detection text. This accessor exposes it cleanly so the
    Status card can render it inline.

    Returns a dict with three fields:
      - last_poll_error: str | None — the most recent fetch-failure
        message, or None if no failure has been seen since startup.
      - consecutive_failed_polls: int — failure-streak counter,
        gated against the offline-notification threshold internally.
      - offline_notified: bool — whether a receiver_offline notification
        has already fired for the current streak (resets when a
        successful poll lands).

    All three reflect the current in-memory state; they are not
    persisted across restarts. After a service restart the streak
    counter is 0 and last_poll_error is empty regardless of the
    pre-restart state — the next poll re-evaluates from scratch.
    """
    return {
        "last_poll_error": _last_offline_reason or None,
        "consecutive_failed_polls": int(_consecutive_failed_polls),
        "offline_notified": bool(_offline_notified),
    }


# v3.4.42: demo-mode auto-recovery state. The recovery walks the same
# port-fallback chain `install.sh --demo` uses (8080 → 8088 → 28080)
# and fires once per process lifetime. The once-per-process guard
# prevents loops: if recovery fails (sudoers misconfigured, all ports
# taken, write failure), the collector falls back to its normal
# non-ADS-B error path and the user can intervene manually. A service
# restart re-arms the guard, so a transient sudo/permission issue
# resolved out-of-band gets retried on the next boot.
_demo_recovery_attempted = False
_NON_ADSB_RECOVERY_THRESHOLD = 1   # consecutive failed polls before firing
_DEMO_FEEDER_PORT_CANDIDATES = (8080, 8088, 28080)
_FEEDER_UNIT_PATH = "/etc/systemd/system/aerodrome-synthetic-feeder.service"


def _set_receiver_port_in_yaml_text(text: str, new_port: int) -> Optional[str]:
    """v3.4.43: targeted line-edit of receiver.port in a YAML config file,
    preserving every other byte of the file (comments, ordering, blank
    lines, indent style, trailing comments on the changed line).

    Algorithm:
      1. Find the first top-level (indent=0) line whose key is 'receiver'.
      2. Within that section (lines whose indent is greater than the
         receiver: line's indent), find the first line whose key is 'port'.
      3. Replace that line's VALUE, preserving the 'port:' prefix, any
         whitespace after the colon, and any trailing inline comment.
      4. Return the full file text with the one line modified.

    Returns None if the structure didn't match (no top-level receiver
    block, no port key under it) so the caller can log a clear miss
    rather than silently writing a wrong file.

    Replaces the v3.4.42 yaml.safe_load + yaml.safe_dump round-trip,
    which worked for value-correctness but DESTROYED the file's
    comments, ordering, and human-authored structure. The example
    config has hundreds of lines of section headers and explanatory
    comments; round-tripping through PyYAML threw all of that away.
    This targeted-edit approach treats the file as text and only
    mutates the one byte range that actually needs changing.

    Limitations (acceptable for Aerodrome's schema):
      - Assumes 'port:' under 'receiver:' is unambiguous (Aerodrome's
        config has exactly one match, no nested sub-blocks under
        receiver containing their own port field).
      - Doesn't handle YAML flow-style ({receiver: {port: 8080}}).
        Aerodrome's example uses block style; flow style isn't
        emitted by anything we ship.
    """
    lines = text.splitlines(keepends=True)
    in_receiver = False
    receiver_indent = -1
    for i, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        # Skip blank + comment-only lines for the structural walk.
        body = stripped.strip()
        if not body or body.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if not in_receiver:
            # Looking for the receiver: section header at top level.
            if indent == 0 and body.split(":", 1)[0].strip() == "receiver":
                in_receiver = True
                receiver_indent = indent
            continue
        # We're inside receiver. If indent dropped back to receiver's
        # level (or lower), we've exited the section without finding port.
        if indent <= receiver_indent:
            break
        # Inside receiver. Look for the port: line.
        key = body.split(":", 1)[0].strip()
        if key == "port":
            # Preserve prefix (indent + 'port:' + spaces) and any trailing
            # inline comment; only the numeric value is replaced.
            m = re.match(
                r"^(?P<prefix>\s*port\s*:\s*)(?P<val>\S+)(?P<tail>.*?)$",
                stripped,
            )
            if not m:
                return None
            # Reattach the original line ending.
            line_ending = line[len(stripped):] or "\n"
            lines[i] = f"{m.group('prefix')}{new_port}{m.group('tail')}{line_ending}"
            return "".join(lines)
    return None


def _port_in_use(port: int) -> bool:
    """Return True if `port` has a TCP listener anywhere on this host.

    Uses `ss -tln` (iproute2; present on every tier-1 distro). Falls
    back to True on error so a failed probe doesn't cause us to pick
    a port that's actually taken — better to skip a candidate than to
    collide on it.
    """
    try:
        out = subprocess.run(
            ["ss", "-tln"], capture_output=True, text=True, timeout=2,
        ).stdout
        # Each listening line includes ":PORT" in the Local Address column.
        # Match with regex so :8080 doesn't match :18080 etc.
        return bool(re.search(rf":{port}\s", out))
    except Exception:
        return True


def _attempt_demo_port_recovery(config: dict):
    """Once-per-process attempt to recover from the v3.4.40 non-ADS-B
    wedge state by walking the feeder-port fallback chain, rewriting
    the feeder unit file + config.yaml, mutating in-memory config, and
    restarting the feeder service.

    Preconditions checked here (so callers can fire the trigger
    indiscriminately):
      1. We're in demo mode (config["demo"]["enabled"] is true).
      2. The feeder unit file exists at the expected path.
      3. We haven't already attempted recovery this process lifetime.

    On success: in-memory config is mutated so the NEXT poll uses the
    new URL without an aerodrome restart. The disk config.yaml is also
    updated so a future restart picks up the same port.

    Returns the new port on success, or None if recovery failed for
    any reason (preconditions, all ports taken, sudo failure, write
    failure, etc). Logging is verbose so journal log shows what was
    attempted and why if anything didn't work.

    The once-per-process guard is set regardless of outcome, so we
    don't keep retrying a failing recovery in tight loop. A service
    restart re-arms the guard.
    """
    global _demo_recovery_attempted
    if _demo_recovery_attempted:
        return None
    # Set the guard NOW (not at end) so any early-return branches still
    # leave us in the don't-retry state.
    _demo_recovery_attempted = True

    # Precondition 1: demo mode
    if not bool((config.get("demo") or {}).get("enabled", False)):
        logger.debug("Auto-recovery skipped: not in demo mode")
        return None

    # Precondition 2: feeder unit file exists
    unit_path = pathlib.Path(_FEEDER_UNIT_PATH)
    if not unit_path.exists():
        logger.debug(
            "Auto-recovery skipped: feeder unit %s does not exist", unit_path
        )
        return None

    logger.info(
        "Auto-recovery: detected demo-mode non-ADS-B wedge; walking "
        "port candidates %s", list(_DEMO_FEEDER_PORT_CANDIDATES),
    )

    # Walk the port chain
    new_port = None
    for candidate in _DEMO_FEEDER_PORT_CANDIDATES:
        if not _port_in_use(candidate):
            new_port = candidate
            break

    if new_port is None:
        logger.error(
            "Auto-recovery failed: every candidate port in %s is in use. "
            "Manual intervention needed — choose a free port and edit "
            "/etc/systemd/system/aerodrome-synthetic-feeder.service "
            "(--port flag) plus /opt/aerodrome/config.yaml (receiver.port).",
            list(_DEMO_FEEDER_PORT_CANDIDATES),
        )
        return None

    logger.info("Auto-recovery: selected port %d", new_port)

    # v3.4.43: validate-all-then-write ordering. Compute both new file
    # contents up front; if EITHER computation fails, abort BEFORE any
    # disk writes happen. The old v3.4.42 ordering wrote the unit file
    # first, then validated the yaml edit — if the yaml edit failed, we
    # left the install with a unit-file-and-config disagreement that
    # was worse than the pre-recovery state.

    # Read the existing feeder unit content (world-readable; no sudo
    # needed for read) and substitute the --port flag value. Bounded
    # regex — only changes existing `--port N` patterns.
    try:
        current_unit = unit_path.read_text()
    except Exception as e:
        logger.error("Auto-recovery: could not read feeder unit: %s", e)
        return None
    new_unit = re.sub(r"--port\s+\d+", f"--port {new_port}", current_unit)
    if new_unit == current_unit:
        logger.warning(
            "Auto-recovery: feeder unit had no --port flag to replace; "
            "skipping unit-file edit. The feeder will continue using "
            "whatever port its current ExecStart specifies."
        )

    # Read + compute the config.yaml edit. If the structure can't be
    # found (malformed YAML, flow style, etc), abort — the unit file
    # hasn't been touched yet, so the install is still in the
    # pre-recovery state (consistent with itself, just wedged).
    cfg_path = pathlib.Path(config["data"].get("config_path") or "config.yaml")
    if not cfg_path.is_absolute():
        cfg_path = pathlib.Path(__file__).parent / cfg_path
    try:
        cfg_text = cfg_path.read_text()
    except Exception as e:
        logger.error("Auto-recovery: could not read config.yaml: %s", e)
        return None
    new_cfg_text = _set_receiver_port_in_yaml_text(cfg_text, new_port)
    if new_cfg_text is None:
        logger.error(
            "Auto-recovery: couldn't find receiver.port in config.yaml "
            "to edit (file may be malformed, in flow style, or missing "
            "the receiver section). Aborting BEFORE any writes so the "
            "install stays in its pre-recovery (consistent) state."
        )
        return None

    # Both contents validated. Now do the writes.
    # Write the unit file via `sudo tee` (the v3.4.42 sudoers rule
    # grants exactly this one path). tee reads from stdin, writes to
    # argv[1]; stdout is captured-and-discarded so we don't echo the
    # unit file to the log.
    try:
        result = subprocess.run(
            ["sudo", "tee", str(unit_path)],
            input=new_unit, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.error(
                "Auto-recovery: sudo tee failed (rc=%d). Stderr: %s. If "
                "you see 'sudo: a password is required', re-run "
                "./install.sh on this server to refresh the sudoers rule "
                "to SUDOERS_VERSION 5+.",
                result.returncode, (result.stderr or "").strip(),
            )
            return None
    except Exception as e:
        logger.error("Auto-recovery: unit-file write raised: %s", e)
        return None

    # Write config.yaml. Pre-validated above, so this should succeed
    # unless something raced us to the file.
    try:
        cfg_path.write_text(new_cfg_text)
    except Exception as e:
        logger.error(
            "Auto-recovery: config.yaml write failed AFTER unit-file "
            "write succeeded. Install is now in a half-edited state. "
            "Manual recovery: re-run the auto-recovery's intended edit "
            "by setting receiver.port to %d in /opt/aerodrome/config.yaml "
            "and running 'sudo systemctl restart aerodrome'. Underlying "
            "error: %s",
            new_port, e,
        )
        return None

    # Mutate in-memory config so the NEXT poll uses the new URL without
    # waiting for an aerodrome restart. `config` is the same dict that
    # fetch_and_store reads `receiver` from on every poll (the run_collector
    # loop in main.py passes the same reference each tick), so this
    # mutation propagates.
    config["receiver"]["port"] = new_port

    # daemon-reload to pick up the new unit file, then restart the feeder.
    # NOT restarting aerodrome itself — the in-memory config mutation
    # above means the next poll uses the new URL, no process restart
    # needed. (Restarting aerodrome from inside aerodrome's own process
    # would kill the current poll mid-execution; in-memory mutation
    # avoids that entire class of problem.)
    try:
        subprocess.run(["sudo", "systemctl", "daemon-reload"],
                       check=True, timeout=10)
        subprocess.run(["sudo", "systemctl", "restart",
                        "aerodrome-synthetic-feeder"],
                       check=True, timeout=15)
    except Exception as e:
        logger.error("Auto-recovery: systemctl restart failed: %s", e)
        return None

    logger.info(
        "Auto-recovery: feeder moved to port %d. Next poll will use "
        "http://%s:%d%s — Status card should clear within ~%ds.",
        new_port, config["receiver"]["ip"], new_port,
        config["receiver"]["path"], config["receiver"]["poll_interval"],
    )
    return new_port

# v2.50.31: capacity-alert state machine. Updated by check_capacity_alerts()
# at most once every CAPACITY_CHECK_INTERVAL_SEC seconds (60s by default).
# Persisted only in memory — on restart, the next check re-evaluates and
# will fire a fresh alert if conditions are still tripped. That's the
# right behavior: we'd rather over-notify after a restart than silently
# fail to notify if the disk is genuinely full.
_capacity_state = {
    "alert_active": False,
    "reason": None,
    "fired_at": None,
}
_last_capacity_check_ts = 0
CAPACITY_CHECK_INTERVAL_SEC = 60

# v3.4.53: periodic query-planner stats refresh. The marker-gated ANALYZE
# in init_db runs once (fresh install / schema bump) and never again, so
# SQLite's planner statistics go stale as tables grow. Observed in
# practice on a 45-day-old install: plans built from first-week row counts
# mis-ordered a join in the Stats "top_aircraft" query, making a one-day
# windowed lookup run ~5s (slower than a 365-day count over the same
# table — the signature of a wrong plan). PRAGMA optimize re-analyzes only
# the tables whose size has drifted enough to matter, and analysis_limit
# bounds it to a sample rather than a full index scan, so the per-run cost
# stays small (sub-second) even on a multi-gigabyte database. Run once per
# poll cycle, rate-limited like the capacity check.
_last_stats_optimize_ts = 0.0
STATS_OPTIMIZE_INTERVAL_SEC = 6 * 3600   # every 6 hours
STATS_OPTIMIZE_ANALYSIS_LIMIT = 1000

# v2.48.0: per-ICAO "last seen squawk" map used to detect EDGES into an
# emergency code (7500/7600/7700) rather than re-firing every poll while
# the aircraft stays in that state. Keyed by ICAO hex; value is the most
# recently observed squawk string (or "" if no squawk on last sighting).
# Not persisted across restarts — on restart every ongoing emergency
# squawker will fire once, which is the right default behavior.
_last_squawk_by_icao: Dict[str, str] = {}
# Canonical emergency-squawk set (mirrors server.py::EMERGENCY_SQUAWKS and
# templates/index.html::EMERGENCY_SQUAWKS). Kept in sync by value, not by
# code sharing — see CONTRIBUTING notes on squawk constants.
_EMERGENCY_SQUAWK_LABELS = {
    "7500": "HIJACK",
    "7600": "RADIO FAIL",
    "7700": "GENERAL EMERGENCY",
}


def set_notifier(notifier) -> None:
    """Called from server.py at startup and on config reload."""
    global _notifier
    _notifier = notifier


# v2.49.0: module-level DB path so resolve_icao_to_tail can read/write the
# persistent hexdb cache without every caller plumbing the path through.
# Set at startup via set_db_path() — same pattern as set_notifier().
_db_path: Optional[str] = None


def set_db_path(db_path: str) -> None:
    """Called from main.py at startup so resolve_icao_to_tail can persist
    its cache entries. Before this is called, the resolver falls back to
    in-memory-only _ICAO_CACHE behavior — useful during tests or any code
    path that imports collector without running init_db."""
    global _db_path
    _db_path = db_path


# --- SQLite tuning (v2.50.13) ---
# Pragmas like cache_size, mmap_size, and temp_store are PER-CONNECTION,
# so applying them at init_db time only would be useless — every read
# request opens its own connection. Every sqlite3.connect call in the
# codebase therefore goes through _open_db_conn() below, which applies
# the active tuning profile's pragmas before returning the connection.
#
# Profile is set from CONFIG['data']['tuning']['profile'] at startup
# via set_db_tuning_profile(). 'auto' resolves at connection time by
# reading /proc/meminfo and picking a profile sized to total system
# memory. The lookup is cheap (one read of a small kernel file) and
# bounded — fall through to 'balanced' on any error.

# Each profile bundles (cache_size_mib, mmap_size_mib, temp_store).
# temp_store: 0 = SQLite default (disk), 2 = memory.
TUNING_PROFILES: Dict[str, Dict[str, int]] = {
    "default":      {"cache_mib": 2,   "mmap_mib": 0,   "temp_store": 0},
    "conservative": {"cache_mib": 8,   "mmap_mib": 32,  "temp_store": 2},
    "balanced":     {"cache_mib": 32,  "mmap_mib": 128, "temp_store": 2},
    "aggressive":   {"cache_mib": 64,  "mmap_mib": 256, "temp_store": 2},
    "high_memory":  {"cache_mib": 128, "mmap_mib": 512, "temp_store": 2},
}

_db_tuning_profile: str = "auto"


def set_db_tuning_profile(profile: Optional[str]) -> None:
    """Called from server.py when CONFIG is loaded so subsequent
    _open_db_conn() calls apply the user's chosen profile. Falls back
    to 'auto' when called with None or an unknown name."""
    global _db_tuning_profile
    if not profile or (profile != "auto" and profile not in TUNING_PROFILES):
        profile = "auto"
    _db_tuning_profile = profile


# v2.60.1: receiver location used by the seen_aircraft.last_distance
# write path. Pushed in from server.py when CONFIG loads (and on
# config save when the user changes the receiver location). Both
# values None → distance is stored as NULL on each write (consistent
# with the receiver-not-configured semantics elsewhere in the app).
_receiver_lat: Optional[float] = None
_receiver_lon: Optional[float] = None


def set_receiver_location(lat: Optional[float], lon: Optional[float]) -> None:
    """Update the in-memory receiver location used by collector writes.
    Pass (None, None) when the receiver isn't configured. This is
    also a hot-update — calling it during runtime takes effect on
    the next position update."""
    global _receiver_lat, _receiver_lon
    _receiver_lat = lat
    _receiver_lon = lon


# v2.88.0: per-aircraft per-day session-tracking rollup config. Pushed
# in from server.py when CONFIG loads (and on config save when the user
# changes stats.timezone or stats.track_gap_minutes). Same shape as
# `set_receiver_location` above — push values from server, cache them
# here, use them on the per-poll write path that maintains
# aircraft_track_daily.
#
# `_session_tz_obj` caches the parsed ZoneInfo so we don't re-parse on
# every poll. None falls back to system local time (matching
# server._day_bounds_ts()'s behavior on empty/invalid tz config).
_session_tz_name: str = ""
_session_tz_obj: Optional[Any] = None  # zoneinfo.ZoneInfo or None
_session_gap_min: int = 5


def set_session_track_config(tz_name: Optional[str],
                              gap_minutes: Optional[int]) -> None:
    """Update the in-memory tz + gap_min used by the per-poll
    aircraft_track_daily write path. Called from server.py whenever
    CONFIG is loaded or reloaded. Re-parses the ZoneInfo object once
    so the per-poll path stays cheap. Empty/invalid tz_name falls
    back to system local time (matching server._day_bounds_ts()'s
    behavior). gap_minutes < 1 is clamped to 1."""
    global _session_tz_name, _session_tz_obj, _session_gap_min
    new_name = (tz_name or "").strip()
    if new_name != _session_tz_name or _session_tz_obj is None:
        _session_tz_name = new_name
        if new_name:
            try:
                from zoneinfo import ZoneInfo
                _session_tz_obj = ZoneInfo(new_name)
            except Exception:
                _session_tz_obj = None
        else:
            _session_tz_obj = None
    try:
        gm = int(gap_minutes) if gap_minutes is not None else 5
    except (ValueError, TypeError):
        gm = 5
    _session_gap_min = max(1, gm)


def _local_day_bucket(now_epoch: int) -> int:
    """Return the local-midnight epoch (seconds) for the day containing
    now_epoch. Uses the cached `_session_tz_obj` if set, else system
    local time. Matches the semantic of server._day_bounds_ts()[0] when
    called with now_epoch == time.time()."""
    if _session_tz_obj is not None:
        dt = datetime.fromtimestamp(now_epoch, tz=_session_tz_obj)
    else:
        # Naive datetime in system local time. .timestamp() on a naive
        # datetime treats it as local, which is what we want for the
        # system-tz fallback path.
        dt = datetime.fromtimestamp(now_epoch)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _compute_distance_km(ac_lat: Optional[float],
                          ac_lon: Optional[float]) -> Optional[float]:
    """Pure haversine, km. Returns None when any input is missing or
    the receiver location isn't configured. Used by the collector
    write path to populate seen_aircraft.last_distance.

    This duplicates server.py's _haversine intentionally — collector
    runs in its own thread and the server module isn't always loaded
    when the collector is the only active component (e.g. the
    test path that imports collector standalone). The function is
    a few lines; duplication is cheaper than the import dependency."""
    if (_receiver_lat is None or _receiver_lon is None or
            ac_lat is None or ac_lon is None):
        return None
    R = 6371.0
    phi1, phi2 = math.radians(_receiver_lat), math.radians(ac_lat)
    dphi = math.radians(ac_lat - _receiver_lat)
    dlam = math.radians(ac_lon - _receiver_lon)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _detect_auto_db_profile() -> str:
    """Pick a tuning profile based on total system memory. Linux-only
    (Aerodrome is Linux-only), reads /proc/meminfo. Falls through to
    'balanced' on any read failure or weirdness."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    gb = kb / (1024 * 1024)
                    if gb < 1.5:
                        return "conservative"   # Pi class
                    if gb < 6:
                        return "balanced"        # 2-4 GB SBC / VPS
                    if gb < 24:
                        return "aggressive"      # 8-16 GB workstation
                    return "high_memory"         # 24+ GB
                    break
    except (OSError, ValueError, IndexError):
        pass
    return "balanced"


def _resolve_db_tuning_profile() -> str:
    """Returns the concrete profile name to apply. Resolves 'auto' to
    a memory-sized profile; passes through any explicit profile name."""
    p = _db_tuning_profile
    if p == "auto":
        return _detect_auto_db_profile()
    if p in TUNING_PROFILES:
        return p
    return "balanced"


def _open_db_conn(db_path, **kwargs) -> sqlite3.Connection:
    """Open a SQLite connection with the active tuning profile's pragmas
    applied. All call sites in the codebase that previously called
    _open_db_conn() directly should go through this helper so cache
    sizing, memory-mapping, and temp-store policy are uniform.

    Pragmas are silently best-effort — on a corrupt or read-only DB they
    might fail, but that's the connection's problem, not ours; the
    failure surfaces on the next real query. We log the warning and
    return the connection regardless so the caller's error handling
    runs as designed."""
    conn = sqlite3.connect(db_path, **kwargs)
    profile_name = _resolve_db_tuning_profile()
    p = TUNING_PROFILES[profile_name]
    cache_kib = p["cache_mib"] * 1024
    mmap_bytes = p["mmap_mib"] * 1024 * 1024
    try:
        # Negative cache_size = "kibibytes of memory cap" (positive would
        # mean pages, which depends on page_size and is fiddlier).
        conn.execute(f"PRAGMA cache_size = -{cache_kib}")
        conn.execute(f"PRAGMA mmap_size = {mmap_bytes}")
        conn.execute(f"PRAGMA temp_store = {p['temp_store']}")
    except sqlite3.DatabaseError as e:
        # Don't log here — would spam at every connection open. The next
        # real query will surface any underlying issue.
        pass
    return conn


def _safe_notify(event: str, title: str, body: str, **kwargs) -> None:
    """Best-effort call to the notifier. Never raises."""
    if _notifier is None:
        return
    try:
        _notifier.notify(event, title, body, **kwargs)
    except Exception as e:
        logger.warning("Notifier threw (ignoring): %s", e)


def _flush_dirty_to_fts(conn: sqlite3.Connection) -> int:
    """v2.51.0 Flavor C: flush rows marked fts_dirty=1 into seen_aircraft_fts.

    Returns the number of rows flushed (mostly for debug/logging). Safe
    to call on a DB without the FTS5 table or fts_dirty column — does
    nothing in that case (so pre-migration databases or test fixtures
    that don't set up search infrastructure don't break).

    Called once per fetch_and_store cycle, inside the cycle's
    transaction. Bounded by the number of changed rows, not total
    seen_aircraft size, thanks to the partial index on fts_dirty.
    """
    try:
        # Confirm the v2.51.0 schema exists. Both the FTS5 table and the
        # fts_dirty column have to be present for this to work.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='seen_aircraft_fts'"
        )
        if cur.fetchone() is None:
            return 0
        # PRAGMA returns no row if column doesn't exist
        cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_aircraft)")}
        if "fts_dirty" not in cols:
            return 0

        n = conn.execute(
            "SELECT COUNT(*) FROM seen_aircraft WHERE fts_dirty = 1"
        ).fetchone()[0]
        if n == 0:
            return 0

        # Delete + re-insert pattern. FTS5's contentless table form
        # would be more efficient (uses UPDATE), but we picked the
        # simpler explicit form in the migration; sticking with
        # delete-then-insert for now and revisiting if it becomes
        # a bottleneck.
        conn.execute("""
            DELETE FROM seen_aircraft_fts
            WHERE rowid IN (SELECT rowid FROM seen_aircraft WHERE fts_dirty = 1)
        """)
        # v2.50.42: enrich the operator FTS column at flush time.
        # seen_aircraft.operator stores the 3-letter ICAO code ("UAL")
        # for a clean API/CSV shape, but for free-text search to match
        # "United Airlines" we need the full name tokenized into FTS.
        # Pull dirty rows, build the enriched string per row, and
        # bulk-insert. Couldn't do this in pure SQL because the
        # AIRLINES dictionary lives in Python.
        from designators import fts_operator_string
        dirty_rows = conn.execute("""
            SELECT rowid, icao, registration, last_callsign,
                   aircraft_type, aircraft_type_desc, operator, country
            FROM seen_aircraft WHERE fts_dirty = 1
        """).fetchall()
        if dirty_rows:
            enriched = [
                (r[0], r[1], r[2], r[3], r[4], r[5],
                 fts_operator_string(r[6]), r[7])
                for r in dirty_rows
            ]
            conn.executemany("""
                INSERT INTO seen_aircraft_fts (
                    rowid, icao, registration, last_callsign,
                    aircraft_type, aircraft_type_desc, operator, country
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, enriched)
        conn.execute("UPDATE seen_aircraft SET fts_dirty = 0 WHERE fts_dirty = 1")
        return n
    except Exception as e:
        # Don't kill the collector cycle for an FTS sync failure. Log
        # and proceed — the dirty rows stay flagged and will be picked
        # up on the next cycle.
        logger.warning(f"FTS5 flush failed (will retry next cycle): {e}")
        return 0


def check_capacity_alerts(config: Dict[str, Any]) -> None:
    """v2.50.31: evaluate capacity thresholds and fire/recover alerts.

    Called once per poll cycle (rate-limited to CAPACITY_CHECK_INTERVAL_SEC
    so a sub-60s poll cadence doesn't multiply the work). Pulls fresh
    metrics from capacity._compute_capacity_metrics(), runs them through
    the state machine in capacity.evaluate_capacity_alerts(), and
    dispatches notifications via _safe_notify() if the state machine
    returns an action.

    Never raises — capacity probing failures must not break the
    collector. A best-effort try/except wraps the body, errors logged
    at warning level."""
    global _capacity_state, _last_capacity_check_ts
    try:
        now = time.time()
        if now - _last_capacity_check_ts < CAPACITY_CHECK_INTERVAL_SEC:
            return
        _last_capacity_check_ts = now

        notif_cfg = (config.get("notifications") or {})
        capacity_cfg = (notif_cfg.get("capacity") or {})

        # Master event-enabled gate. Even if capacity alerts are
        # configured, the user can disable the whole event in
        # notifications.events. We honor that here so we don't
        # uselessly probe disk on a system where alerts are off.
        events_cfg = (notif_cfg.get("events") or {})
        if events_cfg.get("capacity_low") is False:
            return

        from capacity import _compute_capacity_metrics, evaluate_capacity_alerts

        db_path = config["data"]["db_file"]
        retention_days = config.get("retention", {}).get("all_days", 30)
        metrics = _compute_capacity_metrics(db_path, retention_days)

        new_state, action = evaluate_capacity_alerts(metrics, capacity_cfg, _capacity_state)
        _capacity_state = new_state

        if action is None:
            return

        # Compose the notification message. Same shape for fire and
        # recovered — current numbers, why we're alerting (or that we
        # cleared), and a pointer at where to fix it.
        kind = action["kind"]
        size = metrics.get("db_size_mb")
        growth = metrics.get("mb_per_day")
        retention = metrics.get("retention_days")
        projected = metrics.get("projected_settled_mb")
        free = action.get("disk_free_mb")
        total = action.get("disk_total_mb")
        floor = action.get("free_floor_mb")
        headroom = action.get("headroom")

        def fmt_mb(mb):
            if mb is None: return "—"
            if mb >= 1024: return f"{mb/1024:.1f} GB"
            if mb >= 100:  return f"{round(mb)} MB"
            return f"{mb:.1f} MB"

        body_lines = [
            f"Current size:    {fmt_mb(size)}",
            f"Daily growth:    {fmt_mb(growth)}/day",
            f"Retention:       {retention} days",
            f"Projected:       {fmt_mb(projected)}",
            f"Free disk:       {fmt_mb(free)}" + (f" of {fmt_mb(total)}" if total else ""),
            f"Headroom:        {headroom:.2f}×" if headroom is not None else "Headroom:        —",
        ]

        if kind == "fire":
            title = "Aerodrome capacity warning"
            body_lines.append("")
            body_lines.append(f"Reason: {action['reason']}")
            body_lines.append("")
            body_lines.append("Adjust retention in Configuration → Retention,")
            body_lines.append("or free disk space outside Aerodrome.")
            _safe_notify(
                "capacity_low",
                title,
                "\n".join(body_lines),
                priority="high",
                tags=["warning"],
                click_route="status",
            )
        else:  # recovered
            title = "Aerodrome capacity recovered"
            body_lines.append("")
            body_lines.append("Capacity is back within configured thresholds.")
            _safe_notify(
                "capacity_recovered",
                title,
                "\n".join(body_lines),
                priority="default",
                tags=["white_check_mark"],
                click_route="status",
            )
    except Exception as e:
        logger.warning("Capacity alert check failed (ignoring): %s", e)


def refresh_query_planner_stats(config: Dict[str, Any]) -> None:
    """v3.4.53: periodically refresh SQLite's query-planner statistics.

    Called once per poll cycle, rate-limited to STATS_OPTIMIZE_INTERVAL_SEC
    (6 hours) so a sub-60s poll cadence doesn't multiply the work. Runs
    PRAGMA optimize with a bounded analysis_limit: optimize re-analyzes
    only the tables whose size has drifted enough since the last ANALYZE
    to risk a bad plan, and analysis_limit caps each ANALYZE to a sample
    rather than a full index scan — so the cost is small even on a
    multi-gigabyte database. This is the durable fix for stats going stale
    on long-running installs (the marker-gated ANALYZE in init_db only
    runs once). Never raises — a stats-refresh failure must not break the
    collector."""
    global _last_stats_optimize_ts
    try:
        now = time.time()
        if now - _last_stats_optimize_ts < STATS_OPTIMIZE_INTERVAL_SEC:
            return
        _last_stats_optimize_ts = now

        db_path = config["data"]["db_file"]
        conn = _open_db_conn(db_path)
        try:
            conn.execute(f"PRAGMA analysis_limit={STATS_OPTIMIZE_ANALYSIS_LIMIT}")
            t0 = time.time()
            conn.execute("PRAGMA optimize")
            conn.commit()
            logger.info(
                "Query-planner stats refreshed (PRAGMA optimize, %.2fs)",
                time.time() - t0,
            )
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Query-planner stats refresh failed (ignoring): %s", e)


# =============================================================================
# Stats quality filters
# =============================================================================
# Housed here in collector.py so both the collector (for real-time
# all-time-record updates) and the server (for today's-extremes cards
# and drill queries) can apply consistent filtering. Defined once.
#
# The problem these filters solve:
#   - Fastest: transponder glitches occasionally report 1000+ kt for
#     commercial airframes that physically max out around 500 kt.
#   - Slowest: airport service vehicles and stopped aircraft broadcast
#     ADS-B too, producing "slowest: 1 kt" readings.
#   - Longest track: MIN→MAX per icao doesn't detect gaps, so an ICAO
#     that appeared at 01:00 and reappeared at 23:00 would show as
#     "22h continuous track" when in reality nothing of the sort.
#   - All extremes: TIS-B / MLAT pseudo-targets (ICAOs starting with '~'
#     in dump1090's convention) often have incomplete or inaccurate data
#     relayed from ATC rather than broadcast directly.

# Curated set of ICAO type codes for aircraft that physically cannot exceed
# ~650 kt ground speed in normal operation. When a speed > 700 kt is
# reported for one of these types, it's almost certainly bad data and
# gets filtered out. Aircraft types NOT in this set (unknown types,
# fighters, test aircraft, anything military-adjacent) get a permissive
# 1500 kt ceiling so we don't clamp a legitimately-fast F-22.
SUBSONIC_AIRFRAMES = frozenset({
    # Airbus commercial
    "A19N", "A20N", "A21N", "A318", "A319", "A320", "A321",
    "A306", "A30B", "A310", "A332", "A333", "A338", "A339",
    "A342", "A343", "A345", "A346", "A359", "A35K", "A388",
    # Boeing commercial (excluding supersonic B-1 bomber which shares prefix)
    "B712", "B721", "B722",
    "B731", "B732", "B733", "B734", "B735", "B736", "B737", "B738", "B739",
    "B73J", "B73M", "B38M", "B39M", "B3XM",
    "B741", "B742", "B743", "B744", "B748",
    "B752", "B753",
    "B762", "B763", "B764",
    "B772", "B773", "B77L", "B77W", "B778", "B779",
    "B788", "B789", "B78X",
    # Embraer
    "E135", "E145", "E170", "E175", "E190", "E195", "E290", "E295",
    "E50P", "E545", "E55P", "E75L", "E75S",
    # Bombardier / Canadair
    "CRJ1", "CRJ2", "CRJ7", "CRJ9", "CRJX",
    "BCS1", "BCS3",
    "CL30", "CL35", "CL60", "CL64", "CL65",
    "GLF4", "GLF5", "GLF6", "G150", "G280", "GALX",
    "GL5T", "GL7T",
    # De Havilland / Dash
    "DH8A", "DH8B", "DH8C", "DH8D",
    # ATR
    "AT43", "AT45", "AT46", "AT72", "AT75", "AT76",
    # McDonnell Douglas
    "MD11", "MD81", "MD82", "MD83", "MD87", "MD88", "MD90",
    "DC9", "DC91", "DC93", "DC94", "DC95",
    # Cessna GA / bizjets (all subsonic)
    "C172", "C177", "C182", "C206", "C208", "C210",
    "C25A", "C25B", "C25C", "C25M", "C500", "C510", "C525", "C550",
    "C551", "C560", "C56X", "C650", "C680", "C68A", "C700", "C750",
    # Beechcraft GA
    "BE20", "BE30", "BE33", "BE35", "BE36", "BE40", "BE55", "BE58",
    "BE60", "BE76", "BE99", "BE9L", "BE9T",
    # Piper GA
    "P28A", "P28B", "P28R", "P28T", "P32R", "P32T", "PA31", "PA46",
    # Other common GA / light twins
    "SR20", "SR22", "M20P", "M20T", "DA40", "DA42", "DA62",
    # Dassault bizjets (subsonic)
    "F2TH", "F7X", "F8X", "F900", "FA7X", "FA10", "FA20", "FA50", "FA90",
    "FA8X", "F50", "F100", "F70", "F27", "F28",
    # Hawker / BAe
    "H25A", "H25B", "H25C", "HA4T",
    # Learjet (subsonic)
    "LJ24", "LJ25", "LJ31", "LJ35", "LJ40", "LJ45", "LJ55", "LJ60", "LJ75",
    # Pilatus
    "PC12", "PC24", "PC6T", "PC7", "PC9",
    # Common military transports (all subsonic)
    "C5", "C5M", "C17", "C130", "C30J", "C40",
    "KC10", "KC30", "KC35", "KC46", "KC7", "KC97", "KDC1",
    "E3", "E3CF", "E3TF", "E4", "E6", "E8",
    "P3", "P8", "P3C", "U2",
    # Tu / Il / An (subsonic)
    "A124", "A148", "A158", "A225", "IL76", "IL86", "IL96",
    "T154", "T204", "T214",
    # Helicopters (definitively cannot exceed 200 kt)
    "A109", "A119", "A139", "A169", "A189",
    "B06", "B212", "B214", "B222", "B230", "B407", "B412", "B429",
    "EC20", "EC25", "EC30", "EC35", "EC55", "EC20T",
    "R22", "R44", "R66",
    "S61", "S76", "S92", "S97",
    "UH1", "UH60", "UH72", "CH47",
    "H47", "H53", "H60", "H64", "H65", "H6", "EC45", "EC75",
})
SUBSONIC_MAX_KT = 700
PERMISSIVE_MAX_KT = 1500  # for unknown types, fighters, etc.
MIN_PLAUSIBLE_SPEED_KT = 40  # below this, it's a ground vehicle or parked

# v2.49.2: fine-grained per-type speed ceilings. The 700 kt subsonic cap
# catches airliner-scale glitches (a B763 reporting 1010 kt) but is WAY
# too loose for light GA — a Cessna 172 reporting 696 kt still passes
# through at 700 and wins "fastest ever". These caps are generous
# (roughly 1.5-2x typical cruise to allow tailwind descents) but tight
# enough to reject obvious transponder glitches on small aircraft.
# Types not listed here fall back to SUBSONIC_MAX_KT (if in
# SUBSONIC_AIRFRAMES) or PERMISSIVE_MAX_KT (unknown/military).
TYPE_CEILINGS = {
    # GA piston singles (typical cruise 100-180 kt)
    "C172": 250, "C177": 250, "C182": 260, "C206": 260, "C210": 260,
    "P28A": 250, "P28B": 250, "P28R": 260, "P28T": 270,
    "DA40": 260, "SR20": 260, "SR22": 280,
    "M20P": 280, "M20T": 280,
    "BE33": 260, "BE35": 260, "BE36": 260,
    # GA piston twins (typical cruise ~200 kt)
    "BE55": 300, "BE58": 300, "BE60": 300, "BE76": 260,
    "DA42": 280, "DA62": 300,
    "PA31": 300, "PA46": 320,
    # Turboprops (typical cruise 280-320 kt)
    "C208": 320,
    "PC12": 340, "PC24": 460,  # PC24 is a jet, higher cap
    "BE20": 340, "BE30": 340, "BE99": 340, "BE9L": 340, "BE9T": 340,
    "AT43": 350, "AT45": 350, "AT46": 350, "AT72": 380, "AT75": 380, "AT76": 380,
    "DH8A": 380, "DH8B": 380, "DH8C": 380, "DH8D": 400,
    # Helicopters — actual max ~150 kt; cap generously for outliers/tailwind
    "R22": 180, "R44": 180, "R66": 200,
    "B06": 200, "B212": 200, "B214": 220, "B222": 220, "B230": 220,
    "B407": 220, "B412": 220, "B429": 220,
    "A109": 220, "A119": 220, "A139": 220, "A169": 220, "A189": 220,
    "EC20": 220, "EC25": 220, "EC30": 220, "EC35": 220, "EC55": 220, "EC20T": 220,
    "S61": 220, "S76": 220, "S92": 220, "S97": 240,
    "UH1": 200, "UH60": 220, "UH72": 220, "CH47": 220,
    "H47": 220, "H53": 240, "H60": 220, "H64": 220, "H65": 220, "H6": 220,
    "EC45": 220, "EC75": 220,
    # Note: airliners and bizjets stay at SUBSONIC_MAX_KT (700). They can
    # legitimately hit 600+ kt ground speed with a strong tailwind, so
    # tightening here would reject real records.
}


def speed_ceiling_for_type(aircraft_type: Optional[str]) -> int:
    """Return the maximum plausible ground speed (kt) for an aircraft of
    the given ICAO type.

    Lookup order:
      1. TYPE_CEILINGS — per-type class-appropriate cap (GA singles at
         250 kt, helicopters at 220 kt, etc.)
      2. SUBSONIC_MAX_KT (700) — for anything else in SUBSONIC_AIRFRAMES
         (airliners, bizjets)
      3. PERMISSIVE_MAX_KT (1500) — unknown types, fighters, test aircraft

    Used by both the collector's record-update logic and the server's
    stats queries to reject nonsense speed readings like transponder
    glitches. The 250-kt cap on a Cessna 172 means a transponder reading
    696 kt (seen in the wild, Apr 2026) won't set the all-time fastest
    record."""
    t = (aircraft_type or "").strip().upper()
    if t in TYPE_CEILINGS:
        return TYPE_CEILINGS[t]
    if t in SUBSONIC_AIRFRAMES:
        return SUBSONIC_MAX_KT
    return PERMISSIVE_MAX_KT


def is_pseudo_icao(icao: Optional[str]) -> bool:
    """True for TIS-B/MLAT pseudo-targets. dump1090 prefixes these with
    '~' to indicate the ICAO is synthetic (not broadcast directly by the
    aircraft). Their data is ATC-relayed and often inaccurate or
    incomplete — excluded from all stats extremes."""
    return bool(icao) and str(icao).startswith("~")


# Cache of tail → ICAO lookups so we don't hammer hexdb.io when the collector
# rebuilds the watchlist every poll. None value means "looked up, not found".
_TAIL_CACHE: Dict[str, Optional[str]] = {}

# v2.49.0: TTLs for the persistent ICAO→registration cache (hexdb_cache table).
# Positive entries (we got a registration back) are considered fresh for 30
# days — aircraft registrations rarely change, so re-asking hexdb every month
# is a reasonable middle ground between "respect the free API" and "catch
# re-registrations eventually." Negative entries (hexdb 404 or empty response)
# expire sooner because hexdb's dataset grows over time — an aircraft unknown
# today may be known next week.
HEXDB_POSITIVE_TTL_DAYS = 30
HEXDB_NEGATIVE_TTL_DAYS = 7
# Error entries are treated as short-lived misses — retry on the next lookup.
# No TTL because they never enter the cache (see resolve_icao_to_tail — only
# positive and negative outcomes are persisted).
#
# Rolling events log retention. Cleanup runs alongside the other retention
# sweeps in cleanup_old_data.
HEXDB_EVENTS_RETENTION_DAYS = 7

# Cache of ICAO → registration (tail) lookups. Populated on-demand by the
# server's /api/resolve-tail endpoint — not by the collector itself (the
# collector has no reason to reverse-resolve). In-memory only; cache is
# rebuilt on service restart. Most of the cost is amortised because the same
# aircraft appear on the Watchlist/Military tabs across refresh cycles.
# None value means "looked up, none found" so we don't retry dead ICAOs.
_ICAO_CACHE: Dict[str, Optional[str]] = {}

# Counters for ICAO→tail resolver observability. Reset on process start.
# Exposed via /api/resolve-tail/debug so operators can see at a glance
# whether the resolver is actually working, or whether hexdb.io is
# unreachable from this host. Without these, a silent-fail would look
# exactly like "cache is cold, be patient" — indistinguishable for days.
_icao_resolver_stats = {
    "attempts": 0,         # total hexdb GET attempts
    "successes_positive": 0,  # returned a Registration
    "successes_negative": 0,  # hexdb 404 or empty Registration
    "network_errors": 0,   # exceptions (DNS, timeout, TLS, etc.)
    "last_error": None,    # repr of the most recent exception
    "last_success_icao": None,  # last ICAO that produced a positive result
}


# =============================================================================
# Tail Number → ICAO Hex Lookup
# =============================================================================

def resolve_tail_to_icao(tail: str) -> Optional[str]:
    """Look up ICAO hex from tail/registration via hexdb.io."""
    key = tail.upper().strip()
    if key in _TAIL_CACHE:
        return _TAIL_CACHE[key]
    try:
        url = f"https://hexdb.io/api/v1/aircraft/registration/{key}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            icao = data.get("ModeS", "").strip().upper()
            if icao:
                _TAIL_CACHE[key] = icao
                logger.info(f"Resolved tail {tail} → ICAO {icao}")
                return icao
        logger.warning(f"Could not resolve tail number: {tail}")
        _TAIL_CACHE[key] = None  # cache negative lookups too
        return None
    except Exception as e:
        logger.warning(f"Tail lookup failed for {tail}: {e}")
        # Don't cache transient network errors — let next rebuild retry
        return None


# =============================================================================
# ICAO Hex → Tail Number Lookup (reverse direction)
# =============================================================================

def _hexdb_cache_check(conn, key: str, now_ts: int) -> Tuple[str, Optional[str]]:
    """Look up `key` in hexdb_cache. Returns ("fresh_positive", reg) or
    ("fresh_negative", None) if the cached entry is still within TTL,
    otherwise ("stale", None) meaning the caller should refresh from hexdb.

    Updates hit_count / last_hit_at on fresh hits so we can see per-entry
    reuse activity in diagnostics."""
    row = conn.execute(
        "SELECT registration, resolved_at, last_outcome FROM hexdb_cache WHERE icao = ?",
        (key,),
    ).fetchone()
    if not row:
        return ("miss_never_cached", None)
    reg, resolved_at, last_outcome = row
    # Pick the TTL based on what the cache recorded
    ttl_days = (HEXDB_POSITIVE_TTL_DAYS if last_outcome == "positive"
                else HEXDB_NEGATIVE_TTL_DAYS)
    age_days = (now_ts - resolved_at) / 86400.0
    if age_days > ttl_days:
        return ("stale", None)
    # Fresh hit — bump counters
    conn.execute(
        "UPDATE hexdb_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE icao = ?",
        (now_ts, key),
    )
    if last_outcome == "positive":
        return ("fresh_positive", reg)
    return ("fresh_negative", None)


def _hexdb_cache_write(conn, key: str, reg: Optional[str],
                       outcome: str, now_ts: int) -> None:
    """Upsert into hexdb_cache. Resets hit_count to 0 on refresh because the
    entry is effectively new."""
    conn.execute("""
        INSERT INTO hexdb_cache (icao, registration, resolved_at, last_outcome, hit_count, last_hit_at)
        VALUES (?, ?, ?, ?, 0, NULL)
        ON CONFLICT(icao) DO UPDATE SET
            registration = excluded.registration,
            resolved_at = excluded.resolved_at,
            last_outcome = excluded.last_outcome,
            hit_count = 0,
            last_hit_at = NULL
    """, (key, reg, now_ts, outcome))
    # v2.51.0: when hexdb resolves a registration, mirror it into
    # seen_aircraft so search results carry tail registration without
    # needing a JOIN at query time. Only write if reg is non-empty —
    # outcome="miss" calls pass reg=None / "" and we don't want to
    # overwrite a previously-resolved registration with a miss.
    # Flavor C: registration is an FTS-indexed field, so mark dirty
    # so the cycle-end flush picks it up.
    if reg:
        conn.execute(
            "UPDATE seen_aircraft SET registration = ?, fts_dirty = 1 "
            "WHERE icao = ? AND (registration IS NULL OR registration != ?)",
            (reg, key, reg),
        )


def _hexdb_event_log(conn, kind: str, icao: Optional[str], now_ts: int) -> None:
    """Append one row to hexdb_events. Fire-and-forget — wrapped in try/except
    at the call site because an events-log failure should never break a lookup."""
    conn.execute(
        "INSERT INTO hexdb_events (ts, kind, icao) VALUES (?, ?, ?)",
        (now_ts, kind, icao),
    )


def hexdb_cache_stats() -> Dict[str, Any]:
    """Return stats about the persistent hexdb cache, for the Status page.

    v2.49.0: computed fresh on each call from hexdb_cache + hexdb_events.
    Cheap enough to run inside /api/status (both tables are small —
    cache is bounded by the size of your local sky traffic over 30 days,
    events log is bounded by HEXDB_EVENTS_RETENTION_DAYS). If these
    queries ever show up in /api/perf/diagnostics as slow, consider
    caching the result with a short TTL.

    Shape:
      {
        "cache_size": int,           # total entries currently stored
        "refreshed_24h": int,        # entries written/refreshed in last 24h
        "events_24h": {
            "hit": int,              # served from cache
            "miss_positive": int,    # cache miss, hexdb returned a reg
            "miss_negative": int,    # cache miss, hexdb said no
            "miss_error": int,       # cache miss, network/hexdb failed
            "total": int,            # sum of the above
        },
        "hit_rate_pct": int | None,  # hits / total * 100, or None if no events
        "db_available": bool,        # False if set_db_path hasn't been called
      }
    """
    if not _db_path:
        return {
            "cache_size": 0, "refreshed_24h": 0,
            "events_24h": {"hit": 0, "miss_positive": 0, "miss_negative": 0,
                           "miss_error": 0, "total": 0},
            "hit_rate_pct": None, "db_available": False,
        }
    try:
        now_ts = int(time.time())
        day_ago = now_ts - 86400
        conn = _open_db_conn(_db_path)
        # Cache size
        size = conn.execute("SELECT COUNT(*) FROM hexdb_cache").fetchone()[0]
        refreshed = conn.execute(
            "SELECT COUNT(*) FROM hexdb_cache WHERE resolved_at >= ?",
            (day_ago,),
        ).fetchone()[0]
        # Events breakdown
        rows = conn.execute("""
            SELECT kind, COUNT(*) FROM hexdb_events
            WHERE ts >= ?
            GROUP BY kind
        """, (day_ago,)).fetchall()
        conn.close()
        events = {"hit": 0, "miss_positive": 0, "miss_negative": 0, "miss_error": 0}
        for kind, count in rows:
            if kind in events:
                events[kind] = count
        events["total"] = sum(events[k] for k in
                              ("hit", "miss_positive", "miss_negative", "miss_error"))
        hit_rate = (int(events["hit"] * 100 / events["total"])
                    if events["total"] > 0 else None)
        return {
            "cache_size": size,
            "refreshed_24h": refreshed,
            "events_24h": events,
            "hit_rate_pct": hit_rate,
            "db_available": True,
        }
    except Exception as e:
        logger.debug(f"hexdb_cache_stats failed: {e}")
        return {
            "cache_size": 0, "refreshed_24h": 0,
            "events_24h": {"hit": 0, "miss_positive": 0, "miss_negative": 0,
                           "miss_error": 0, "total": 0},
            "hit_rate_pct": None, "db_available": False,
        }


def resolve_icao_to_tail(icao: str) -> Optional[str]:
    """Look up aircraft registration (tail number) from an ICAO hex code via
    hexdb.io. Used by the server's /api/resolve-tail endpoint to populate
    Track-link URLs for providers (FR24, AirNav, PlaneFinder) that want a
    registration rather than a hex.

    v2.49.0: now backed by a persistent cache in the hexdb_cache SQLite
    table (survives restarts). Per-entry TTLs — positive entries are
    considered fresh for HEXDB_POSITIVE_TTL_DAYS, negative for
    HEXDB_NEGATIVE_TTL_DAYS. Every lookup logs one row to hexdb_events
    (kind = hit / miss_positive / miss_negative / miss_error) so the
    Status page can show accurate 24h counts. In-memory _ICAO_CACHE still
    exists as a warm front cache — hit first, then DB, then hexdb.

    Returns None on: unknown-to-hexdb ICAOs, hexdb returning no Registration
    field, network errors. Callers should treat None as 'no reg; fall back
    to hex-based URL'.

    Observability: maintains _icao_resolver_stats for operator inspection
    via /api/resolve-tail/debug. First success and first network error are
    logged at INFO/ERROR level respectively so operators can spot a broken
    resolver without having to dig through log levels."""
    key = (icao or "").upper().strip()
    if not key:
        return None

    # In-memory front cache — same-process calls avoid even the SQLite hit.
    # Populated from DB on first lookup this process-life.
    if key in _ICAO_CACHE:
        # Log as a hit in the events table too, if we have a db_path,
        # so the 24h stats reflect real usage (not just cold misses).
        if _db_path:
            try:
                conn = _open_db_conn(_db_path)
                _hexdb_event_log(conn, "hit", key, int(time.time()))
                # Bump hit_count on the persistent entry too
                conn.execute(
                    "UPDATE hexdb_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE icao = ?",
                    (int(time.time()), key),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.debug(f"hexdb event log failed (memory hit): {e}")
        return _ICAO_CACHE[key]

    now_ts = int(time.time())

    # DB cache check (second layer). Only attempted when set_db_path has been
    # called — if not, fall through to live hexdb fetch like pre-v2.49.0.
    if _db_path:
        try:
            conn = _open_db_conn(_db_path)
            outcome, cached_reg = _hexdb_cache_check(conn, key, now_ts)
            if outcome in ("fresh_positive", "fresh_negative"):
                _ICAO_CACHE[key] = cached_reg  # warm the in-memory layer
                _hexdb_event_log(conn, "hit", key, now_ts)
                conn.commit()
                conn.close()
                return cached_reg
            conn.close()
            # outcome is "stale" or "miss_never_cached" — fall through to hexdb
        except Exception as e:
            logger.debug(f"hexdb cache read failed for {key}: {e}")
            # Fall through and try live

    # Cache miss (or no DB wired up yet) — go to hexdb
    _icao_resolver_stats["attempts"] += 1
    try:
        # hexdb.io's aircraft endpoint is /api/v1/aircraft/{hex} — note the
        # hex goes directly after /aircraft/. Earlier versions used an
        # extraneous /icao/ segment (mirroring the tail→ICAO resolver's
        # /aircraft/registration/{reg} path), which 404'd for every lookup
        # and caused every Track ↗ link to fall back. Fixed in v2.36.5.
        url = f"https://hexdb.io/api/v1/aircraft/{key}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            reg = (data.get("Registration") or "").strip().upper()
            if reg:
                _ICAO_CACHE[key] = reg
                if _db_path:
                    try:
                        conn = _open_db_conn(_db_path)
                        _hexdb_cache_write(conn, key, reg, "positive", now_ts)
                        _hexdb_event_log(conn, "miss_positive", key, now_ts)
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.debug(f"hexdb cache write failed: {e}")
                # First-of-process-life success: log at INFO so operators
                # can confirm the resolver is actually working.
                if _icao_resolver_stats["successes_positive"] == 0:
                    logger.info(
                        f"ICAO→tail resolver working: {key} → {reg} "
                        f"(first successful lookup this process life)"
                    )
                else:
                    logger.debug(f"Resolved ICAO {key} → tail {reg}")
                _icao_resolver_stats["successes_positive"] += 1
                _icao_resolver_stats["last_success_icao"] = key
                return reg
        # 404 or empty Registration field → negative-cache so we don't retry
        _ICAO_CACHE[key] = None
        if _db_path:
            try:
                conn = _open_db_conn(_db_path)
                _hexdb_cache_write(conn, key, None, "negative", now_ts)
                _hexdb_event_log(conn, "miss_negative", key, now_ts)
                conn.commit()
                conn.close()
            except Exception as e:
                logger.debug(f"hexdb cache write failed: {e}")
        _icao_resolver_stats["successes_negative"] += 1
        return None
    except Exception as e:
        _icao_resolver_stats["network_errors"] += 1
        _icao_resolver_stats["last_error"] = repr(e)
        if _db_path:
            try:
                conn = _open_db_conn(_db_path)
                _hexdb_event_log(conn, "miss_error", key, now_ts)
                conn.commit()
                conn.close()
            except Exception:
                pass
        # First network error: log at ERROR so a fundamentally-broken
        # resolver shouts loud enough to notice. Subsequent errors drop to
        # WARNING, then DEBUG, to avoid spamming logs when hexdb is down.
        if _icao_resolver_stats["network_errors"] == 1:
            logger.error(
                f"ICAO→tail resolver: FIRST network error for {key}: {e}. "
                f"If this persists, check that hexdb.io is reachable from "
                f"this host (curl https://hexdb.io/api/v1/aircraft/{key})."
            )
        elif _icao_resolver_stats["network_errors"] < 10:
            logger.warning(f"ICAO→tail lookup failed for {key}: {e}")
        else:
            logger.debug(f"ICAO→tail lookup failed for {key}: {e}")
        # Transient error — don't cache the outcome (might succeed next time).
        return None


# ---------------------------------------------------------------------------
# Track-link providers — single source of truth for external tracker URLs.
# ---------------------------------------------------------------------------
# Each provider entry has two URL templates with placeholder tokens:
#   {HEX_UPPER} / {HEX_LOWER}  — the aircraft's ICAO hex
#   {REG_UPPER} / {REG_LOWER}  — the aircraft's registration ('tail number')
#
# Each provider declares whether it *needs* the registration (reg_required).
# Registration-needing providers fall back to the fallback provider when the
# reg isn't resolvable.
#
# v2.44.1: this dict is exposed via /api/ui-config as the canonical
# definition; the frontend TRACK_LINK_URLS helper uses these templates
# instead of redeclaring the URL shapes. Before this release we maintained
# the mapping twice (once here in collector.py, once in templates/index.html),
# which was a guaranteed-to-bitrot source of tech debt.
TRACK_LINK_PROVIDERS = {
    "airplanes_live": {
        "label": "airplanes.live",
        "url": "https://globe.airplanes.live/?icao={HEX_LOWER}",
        "reg_required": False,
    },
    "flightaware": {
        "label": "FlightAware",
        "url": "https://flightaware.com/live/modes/{HEX_UPPER}/redirect",
        "reg_required": False,
    },
    "flightradar24": {
        "label": "Flightradar24",
        "url": "https://www.flightradar24.com/data/aircraft/{REG_LOWER}",
        "reg_required": True,
    },
    "airnavradar": {
        "label": "AirNavRadar",
        "url": "https://www.airnavradar.com/data/registration/{REG_UPPER}",
        "reg_required": True,
    },
    "planefinder": {
        "label": "PlaneFinder",
        "url": "https://planefinder.net/data/aircraft/{REG_UPPER}",
        "reg_required": True,
    },
}
# Fallback when the chosen provider needs a registration that's not in
# cache, or when an unknown provider is passed in.
TRACK_LINK_FALLBACK = "airplanes_live"


def _render_track_url_template(template: str, hex_code: str, reg: Optional[str]) -> str:
    """Substitute {HEX_UPPER}/{HEX_LOWER}/{REG_UPPER}/{REG_LOWER} placeholders
    into a provider URL template."""
    hex_up = hex_code.upper()
    hex_lo = hex_code.lower()
    reg_up = (reg or "").upper()
    reg_lo = (reg or "").lower()
    return (template
            .replace("{HEX_UPPER}", hex_up)
            .replace("{HEX_LOWER}", hex_lo)
            .replace("{REG_UPPER}", reg_up)
            .replace("{REG_LOWER}", reg_lo))


def _build_track_url(icao: str, provider: str) -> str:
    """Build an external track-link URL for the configured provider.

    Used to build the 'Track' button URL on watchlist and special-aircraft
    notifications. Uses the TRACK_LINK_PROVIDERS registry as the source of
    truth — the frontend reads the same registry via /api/ui-config, so a
    new provider added here automatically shows up in the UI with no code
    changes needed on the frontend side.

    Reads the registration from _ICAO_CACHE but never triggers a fresh
    hexdb lookup — hexdb has a 10-second timeout and this runs on the
    collector thread inside the poll loop. A cache miss simply falls
    back to airplanes.live for that one notification; the cache fills
    naturally as the UI resolves tails over time, so repeat sightings
    of the same aircraft will use the configured provider.
    """
    hex_up = (icao or "").upper().strip()
    if not hex_up:
        return ""

    # Resolve provider; unknown provider → fallback
    cfg = TRACK_LINK_PROVIDERS.get(provider) if provider else None
    if cfg is None:
        cfg = TRACK_LINK_PROVIDERS[TRACK_LINK_FALLBACK]

    # If the provider needs a registration, pull it from cache. A cache
    # miss (not yet resolved, or negative-cached) → fall back.
    reg = None
    if cfg["reg_required"]:
        reg = _ICAO_CACHE.get(hex_up)
        if not reg:
            cfg = TRACK_LINK_PROVIDERS[TRACK_LINK_FALLBACK]

    return _render_track_url_template(cfg["url"], hex_up, reg)


# =============================================================================
# Database
# =============================================================================

def init_db(db_path: str):
    """Create tables. all_sightings stores everything; military/watchlist
    store tagged subsets with their extra label columns."""
    conn = _open_db_conn(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS all_sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT NOT NULL,
            callsign TEXT DEFAULT '',
            speed REAL,
            lat REAL,
            lon REAL,
            altitude REAL,
            aircraft_type TEXT DEFAULT '',
            type_desc TEXT DEFAULT '',
            seen_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS military_sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT NOT NULL,
            callsign TEXT DEFAULT '',
            speed REAL,
            lat REAL,
            lon REAL,
            altitude REAL,
            aircraft_type TEXT DEFAULT '',
            type_desc TEXT DEFAULT '',
            seen_at INTEGER NOT NULL,
            special_label TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT NOT NULL,
            callsign TEXT DEFAULT '',
            speed REAL,
            lat REAL,
            lon REAL,
            altitude REAL,
            aircraft_type TEXT DEFAULT '',
            type_desc TEXT DEFAULT '',
            seen_at INTEGER NOT NULL,
            watchlist_label TEXT DEFAULT ''
        )
    """)
    # Wave 2 — permanent record of the first time each ICAO has ever been seen.
    # Unlike all_sightings (which follows retention policy and gets pruned),
    # seen_aircraft is append-only and never pruned. Used by the Stats tab's
    # "First time seen today" card.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_aircraft (
            icao TEXT PRIMARY KEY,
            first_seen_at INTEGER NOT NULL,
            first_callsign TEXT DEFAULT '',
            first_aircraft_type TEXT DEFAULT ''
        )
    """)
    # Wave 3 — all-time records. Stores one row per record type (furthest_ever,
    # fastest_ever, etc.) with the winning aircraft's details and when the
    # record was set. Incrementally updated by the collector on every poll if
    # the record is beaten. Never pruned.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats_records (
            record_type TEXT PRIMARY KEY,
            value REAL NOT NULL,
            icao TEXT DEFAULT '',
            callsign TEXT DEFAULT '',
            aircraft_type TEXT DEFAULT '',
            set_at INTEGER NOT NULL,
            extra TEXT DEFAULT ''
        )
    """)

    # --- v2.50.0: hourly rollups for stats summaries and Search ---
    # Each row is one (icao, hour_bucket) where hour_bucket is the unix
    # timestamp at the top of the hour (epoch / 3600 * 3600). Captures
    # the aggregates needed by the count and group-by queries that the
    # All tab originally drove (Search inherited those query shapes when
    # the All tab was removed in Phase 1D):
    #   - per-aircraft counts and first/last timing (for "since X" display)
    #   - last-known position/altitude/speed/squawk (for current-state row)
    #   - intra-hour extremes (min/max altitude, max speed)
    #
    # Does NOT replace all_sightings — raw sightings stay for queries
    # that need fine-grained position data (range rose, distance
    # histogram, today's extremes that need the exact row that set a
    # peak). Rollup serves the queries where one-row-per-aircraft-per-hour
    # is enough.
    #
    # Populated two ways:
    #   1. Online by the collector on every poll — see record_aircraft_batch
    #   2. Backfilled at startup from existing all_sightings rows, once
    #      per install (idempotent — uses _aerodrome_meta to mark done)
    #
    # See: design doc v2.50.0-design.md, section 4 (schema rationale)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sightings_hourly (
            icao            TEXT    NOT NULL,
            hour_bucket     INTEGER NOT NULL,
            callsign        TEXT    DEFAULT '',
            aircraft_type   TEXT    DEFAULT '',
            type_desc       TEXT    DEFAULT '',
            sighting_count  INTEGER NOT NULL,
            first_seen_at   INTEGER NOT NULL,
            last_seen_at    INTEGER NOT NULL,
            last_lat        REAL,
            last_lon        REAL,
            last_altitude   REAL,
            last_speed      REAL,
            min_altitude    REAL,
            max_altitude    REAL,
            max_speed       REAL,
            last_squawk     TEXT    DEFAULT '',
            min_nonzero_altitude REAL,  -- v2.87.1: lowest_altitude card source
            PRIMARY KEY (icao, hour_bucket)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_bucket ON sightings_hourly(hour_bucket)")
    # v2.50.12: drop idx_hourly_icao_bucket on (icao, hour_bucket DESC).
    # That index was redundant with the table's PRIMARY KEY (icao,
    # hour_bucket), and the DESC direction was never exercised — every
    # production query against sightings_hourly filters on hour_bucket
    # range, none filter or sort on icao alone. SQLite can scan the PK
    # index in either direction efficiently, so the duplicate added
    # write overhead on every collector poll for zero read benefit.
    # DROP IF EXISTS is idempotent — re-attempts on subsequent startups
    # are silent no-ops.
    conn.execute("DROP INDEX IF EXISTS idx_hourly_icao_bucket")
    # v2.50.11: covering index for the All-tab COUNT(DISTINCT icao) path.
    # The single-column idx_hourly_bucket above is non-covering — SQLite
    # has to do a table fetch for every matching row to read the icao
    # value. With a (hour_bucket, icao) composite the icao is in the
    # index leaf itself, so the count is satisfied entirely by an index
    # scan with no table touches. Symptom that flagged this: on a 465k-
    # raw / 21k-rollup install, all_tab_count_rollup ran 94 ms while
    # all_tab_count_raw ran 66 ms (raw uses the covering idx_all_seen_icao
    # = (seen_at, icao)). Rollup should always win on row count alone;
    # the wrong-shape penalty was the cause.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_bucket_icao ON sightings_hourly(hour_bucket, icao)")

    # --- v3.4.6: military_hourly + watchlist_hourly rollup tables ---
    # Same shape and purpose as sightings_hourly but tracking the
    # military and watchlist Stats card paths. Reason: the v3.4.3
    # diagnostic on a 5.1 GB / 29M-row install showed military_count
    # 365d running at 483 ms (slowest hot-path query in the set) and
    # watchlist_count 365d at 223 ms. Both are COUNT(DISTINCT icao)
    # over a date range on the raw per-poll tables; both benefit from
    # the same rollup pattern that v2.50.0 applied to all_sightings.
    #
    # Populated two ways (same as sightings_hourly):
    #   1. Online by the collector on every poll that's military/watchlist
    #   2. Backfilled at startup from existing rows, once per install
    #      (idempotent — uses _aerodrome_meta to mark done)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS military_hourly (
            icao            TEXT    NOT NULL,
            hour_bucket     INTEGER NOT NULL,
            callsign        TEXT    DEFAULT '',
            aircraft_type   TEXT    DEFAULT '',
            type_desc       TEXT    DEFAULT '',
            special_label   TEXT    DEFAULT '',
            sighting_count  INTEGER NOT NULL,
            first_seen_at   INTEGER NOT NULL,
            last_seen_at    INTEGER NOT NULL,
            PRIMARY KEY (icao, hour_bucket)
        )
    """)
    # Covering index for the military_count Stats card path: same shape
    # as idx_hourly_bucket_icao on sightings_hourly. With (hour_bucket,
    # icao) the COUNT(DISTINCT icao) WHERE hour_bucket BETWEEN ? AND ?
    # query plan becomes "SEARCH military_hourly USING COVERING INDEX",
    # zero table fetches.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mil_hourly_bucket_icao ON military_hourly(hour_bucket, icao)")
    # Specials breakdown query (Stats card: top 5 named specials seen
    # in the window) does GROUP BY (icao, special_label) ORDER BY
    # last_seen_at DESC LIMIT 5. A separate index on
    # (hour_bucket, special_label) helps the GROUP BY when
    # special_label is non-empty (the common filter case for the
    # specials card).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mil_hourly_bucket_special ON military_hourly(hour_bucket, special_label)")

    # watchlist_hourly. PK is THREE-way (icao, hour_bucket, watchlist_label)
    # rather than two-way like military_hourly. Reason: the
    # watchlist_count Stats card query returns BOTH COUNT(DISTINCT icao)
    # AND COUNT(DISTINCT watchlist_label). With a two-way PK we'd lose
    # which labels each ICAO hit across the window. Three-way captures
    # the full cardinality. Row count is bounded by (ICAOs × hours ×
    # labels-per-ICAO) — still ~70× smaller than per-poll raw rows on
    # typical traffic patterns where most aircraft hit one rule.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_hourly (
            icao            TEXT    NOT NULL,
            hour_bucket     INTEGER NOT NULL,
            watchlist_label TEXT    NOT NULL,
            callsign        TEXT    DEFAULT '',
            aircraft_type   TEXT    DEFAULT '',
            type_desc       TEXT    DEFAULT '',
            sighting_count  INTEGER NOT NULL,
            first_seen_at   INTEGER NOT NULL,
            last_seen_at    INTEGER NOT NULL,
            PRIMARY KEY (icao, hour_bucket, watchlist_label)
        )
    """)
    # Covering index for the watchlist_count Stats card path.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_hourly_bucket_icao ON watchlist_hourly(hour_bucket, icao)")
    # Covering index for the COUNT(DISTINCT watchlist_label) variant.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_hourly_bucket_label ON watchlist_hourly(hour_bucket, watchlist_label)")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_all_seen ON all_sightings(seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_all_icao ON all_sightings(icao)")
    # v2.50.17: covering indexes for the military_count and watchlist_count
    # Stats card paths. Same shape as the v2.50.11 idx_hourly_bucket_icao
    # fix on sightings_hourly: COUNT(DISTINCT icao) WHERE seen_at BETWEEN ?
    # AND ? on a single-column-on-seen_at index has to fetch each matching
    # row from the table to read the icao value. The (seen_at, icao)
    # composite makes the count an index-only scan, dropping the count
    # query from "USING INDEX" to "USING COVERING INDEX" in the plan.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mil_seen_icao ON military_sightings(seen_at, icao)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_seen_icao ON watchlist_sightings(seen_at, icao)")
    # v2.50.22: drop the now-redundant single-column indexes on
    # military_sightings(seen_at) and watchlist_sightings(seen_at). They
    # were originally created to serve range queries on seen_at, but
    # the (seen_at, icao) composites added in v2.50.17 cover those
    # queries identically — seen_at is the leading column, so any
    # WHERE seen_at = ? or WHERE seen_at BETWEEN ? AND ? clause can use
    # the composite. Verified no INDEXED BY hint anywhere in the codebase
    # references these by name. Removing the redundant indexes saves a
    # small amount of disk plus write amplification on inserts. Same
    # cleanup pattern as v2.50.12, which dropped idx_hourly_icao_bucket
    # after idx_hourly_bucket_icao made it redundant. DROP INDEX IF
    # EXISTS is idempotent on fresh installs (where they were never
    # created) and on existing installs (where init_db re-runs at every
    # startup).
    conn.execute("DROP INDEX IF EXISTS idx_mil_seen")
    conn.execute("DROP INDEX IF EXISTS idx_watch_seen")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_first ON seen_aircraft(first_seen_at)")

    # --- v2.40.1 migration: squawk column on sightings tables ---
    # Adds the `squawk` column if missing. SQLite doesn't have
    # "ADD COLUMN IF NOT EXISTS", so we catch the "duplicate column"
    # error pattern and continue. Existing rows get NULL. New rows
    # are populated by the collector on each poll.
    for table in ("all_sightings", "military_sightings", "watchlist_sightings"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN squawk TEXT DEFAULT ''")
            logger.info(f"Migrated: added squawk column to {table}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise  # Unexpected error — re-raise
            # Column already exists; idempotent no-op.

    # --- v2.40.1 composite index for the All tab grouping query ---
    # The rewritten /api/all does:
    #   SELECT icao, COUNT(*), MIN(seen_at), MAX(seen_at), ...
    #   FROM all_sightings WHERE seen_at BETWEEN ? AND ? GROUP BY icao
    # A composite (seen_at, icao) index lets SQLite use an index-only scan
    # for the WHERE+GROUP BY, which matters on 7-day windows with heavy
    # traffic (users with 10k+ aircraft/day). Without this index, the
    # planner falls back to a full table scan + sort.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_all_seen_icao ON all_sightings(seen_at, icao)")

    # --- v2.42.13 covering index for range_rose / distance_histogram ---
    # The range_rose and distance_histogram cards on the Stats tab scan
    # every (lat, lon) position in the selected time window (default:
    # 30 days). Without a covering index on (seen_at, lat, lon), SQLite
    # has to read the table rows after the seen_at range scan — on a
    # 3M-row install that's ~10s, the single largest remaining cost on
    # the Stats endpoint after the v2.42.9–v2.42.12 rewrites.
    #
    # With this index, the query becomes an index-only scan: seen_at
    # provides the range filter, lat/lon are covered directly by the
    # index entries, no table read required. The range_rose block in
    # server.py pins the plan via INDEXED BY.
    #
    # Disk cost: roughly 25-30 bytes per row (seen_at INTEGER + two
    # REALs + rowid) plus B-tree overhead. On a 3M-row install that's
    # ~70-90MB of extra index. On a fresh install: zero overhead until
    # rows accumulate.
    #
    # Build cost: ~30-90s on a Pi 4 with a 3M-row table. On a fresh
    # install the CREATE INDEX is effectively instant. We log at WARN
    # when the table is large enough for the build to take noticeable
    # time, so operators watching journalctl see what's happening
    # during the pause. A separate marker (range_index_version) rather
    # than bumping analyze_version so the two concerns stay independent.
    _RANGE_INDEX_MARKER_VERSION = 1
    try:
        row = conn.execute(
            "SELECT value FROM _aerodrome_meta WHERE key = 'range_index_version'"
        ).fetchone()
        current_range_idx_marker = int(row[0]) if row and row[0] else 0
    except sqlite3.OperationalError:
        # Table doesn't exist yet — create it (shared with analyze_version).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _aerodrome_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        current_range_idx_marker = 0

    if current_range_idx_marker < _RANGE_INDEX_MARKER_VERSION:
        # Estimate migration time from current row count so the log message
        # can be specific ("on 2,945,816 rows" is more reassuring than a
        # generic "this may take a while" when a user is staring at a
        # service that's stopped responding).
        try:
            row_count = conn.execute(
                "SELECT COUNT(*) FROM all_sightings"
            ).fetchone()[0] or 0
        except sqlite3.OperationalError:
            row_count = 0
        if row_count > 100_000:
            logger.warning(
                f"Building covering index idx_all_seen_lat_lon on "
                f"{row_count:,} rows — first-time migration after upgrade "
                f"to v2.42.13, may take 30-90s on a Pi-class host. "
                f"Service will resume once the build finishes."
            )
        else:
            logger.info(
                f"Creating covering index idx_all_seen_lat_lon "
                f"({row_count:,} rows)…"
            )
        _t_start = time.time()
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_all_seen_lat_lon "
            "ON all_sightings(seen_at, lat, lon)"
        )
        _t_elapsed = time.time() - _t_start
        logger.info(
            f"Covering index idx_all_seen_lat_lon built in {_t_elapsed:.1f}s "
            f"— range_rose / distance_histogram queries will now use "
            f"index-only scans"
        )
        conn.execute(
            "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
            ("range_index_version", str(_RANGE_INDEX_MARKER_VERSION))
        )

    # --- v2.42.6: run ANALYZE on fresh installs and on schema migrations ---
    # SQLite's query planner uses stats from ANALYZE to estimate index
    # selectivity. Without stats, it falls back to heuristics that work
    # fine on small tables but can misjudge badly once tables grow into
    # the millions of rows. Observed in practice: a Pi user with 3M rows
    # in all_sightings had the planner choose idx_all_icao (ICAO-first)
    # for a seen_at-range query, producing a 20-second table scan where
    # idx_all_seen would have run in milliseconds.
    #
    # Policy: run ANALYZE once when we see a sentinel marker missing, and
    # again whenever new indexes are added (bumping the marker). This
    # avoids penalizing fast restarts with a 30-60s ANALYZE on large DBs.
    # The Performance diagnostics page exposes a manual 'Re-analyze' button
    # for users who want to refresh stats after heavy data churn.
    _ANALYZE_MARKER_VERSION = 1
    try:
        row = conn.execute(
            "SELECT value FROM _aerodrome_meta WHERE key = 'analyze_version'"
        ).fetchone()
        current_marker = int(row[0]) if row and row[0] else 0
    except sqlite3.OperationalError:
        # Table doesn't exist yet — create it
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _aerodrome_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        current_marker = 0

    if current_marker < _ANALYZE_MARKER_VERSION:
        # On large DBs this may take 30-60s; log so operators can see the
        # service is doing something during that window.
        logger.info(f"Running ANALYZE (marker {current_marker} → {_ANALYZE_MARKER_VERSION})…")
        _t_start = time.time()
        conn.execute("ANALYZE")
        _t_elapsed = time.time() - _t_start
        logger.info(f"ANALYZE complete in {_t_elapsed:.1f}s — query planner stats refreshed")
        conn.execute(
            "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
            ("analyze_version", str(_ANALYZE_MARKER_VERSION))
        )


    # Backfill seen_aircraft from any existing all_sightings rows. On a brand
    # new install this is a no-op (both tables empty). On upgrades from pre-
    # Wave-2 versions, this ensures aircraft first seen before the upgrade
    # aren't falsely flagged as "first time seen today" by the Stats tab.
    # We only do this if seen_aircraft is empty (first time the table exists),
    # so it's effectively a one-time migration.
    seen_count_row = conn.execute("SELECT COUNT(*) AS n FROM seen_aircraft").fetchone()
    if seen_count_row and seen_count_row[0] == 0:
        backfilled = conn.execute("""
            INSERT OR IGNORE INTO seen_aircraft (icao, first_seen_at, first_callsign, first_aircraft_type)
            SELECT icao, MIN(seen_at) AS first_seen_at,
                   COALESCE((SELECT callsign FROM all_sightings a2
                             WHERE a2.icao = a1.icao ORDER BY seen_at ASC LIMIT 1), '') AS first_callsign,
                   COALESCE((SELECT aircraft_type FROM all_sightings a3
                             WHERE a3.icao = a1.icao ORDER BY seen_at ASC LIMIT 1), '') AS first_type
            FROM all_sightings a1
            GROUP BY icao
        """).rowcount
        if backfilled > 0:
            logger.info(f"Backfilled seen_aircraft with {backfilled} existing ICAOs from all_sightings")

    # Wave 3 — backfill stats_records from existing all_sightings on first
    # upgrade. Only runs if the records table is empty (one-time migration).
    # Also: if any existing stats_records rows have non-numeric 'value' fields
    # (from a prior buggy version), purge them so the next poll can re-seed
    # them cleanly.
    def _is_numeric(v):
        try:
            float(v)
            return True
        except (TypeError, ValueError):
            return False

    bad_rows = conn.execute("SELECT record_type, value FROM stats_records").fetchall()
    for rt, val in bad_rows:
        if not _is_numeric(val):
            conn.execute("DELETE FROM stats_records WHERE record_type = ?", (rt,))
            logger.warning(f"Removed corrupt stats_records row '{rt}' (value was non-numeric: {val!r})")

    rec_count_row = conn.execute("SELECT COUNT(*) AS n FROM stats_records").fetchone()
    if rec_count_row and rec_count_row[0] == 0:
        backfilled_recs = 0

        def _num_or_none(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        # Fastest ever (speed > 0). Pull top candidates and pick the first
        # whose speed coerces to a valid number AND passes the quality
        # filters (TIS-B exclusion + type-aware speed ceiling). Without
        # the filters, a transponder-glitched 1010-kt B763 would become
        # the all-time fastest the moment this migration ran on any
        # existing database with that history.
        rows = conn.execute("""
            SELECT icao, callsign, aircraft_type, speed, seen_at
            FROM all_sightings WHERE speed IS NOT NULL
              AND icao NOT LIKE '~%'
            ORDER BY CAST(speed AS REAL) DESC LIMIT 40
        """).fetchall()
        for row in rows:
            v = _num_or_none(row[3])
            if v is None or v <= 0:
                continue
            cap = speed_ceiling_for_type(row[2])
            if v > cap:
                continue
            conn.execute("""INSERT INTO stats_records(record_type,value,icao,callsign,aircraft_type,set_at,extra)
                            VALUES('fastest_ever',?,?,?,?,?,'')""",
                         (v, row[0], row[1], row[2], row[4]))
            backfilled_recs += 1
            break

        # Highest altitude ever — exclude TIS-B pseudo-targets (their
        # altitude is ATC-relayed and often imprecise).
        rows = conn.execute("""
            SELECT icao, callsign, aircraft_type, altitude, seen_at
            FROM all_sightings WHERE altitude IS NOT NULL
              AND icao NOT LIKE '~%'
            ORDER BY CAST(altitude AS REAL) DESC LIMIT 20
        """).fetchall()
        for row in rows:
            v = _num_or_none(row[3])
            if v is not None:
                conn.execute("""INSERT INTO stats_records(record_type,value,icao,callsign,aircraft_type,set_at,extra)
                                VALUES('highest_altitude_ever',?,?,?,?,?,'')""",
                             (v, row[0], row[1], row[2], row[4]))
                backfilled_recs += 1
                break

        # Lowest altitude ever (excluding ground — altitude > 0). Also
        # exclude TIS-B pseudo-targets — many of them are ground vehicles
        # at airports that would otherwise dominate this record.
        rows = conn.execute("""
            SELECT icao, callsign, aircraft_type, altitude, seen_at
            FROM all_sightings WHERE altitude IS NOT NULL
              AND icao NOT LIKE '~%'
            ORDER BY CAST(altitude AS REAL) ASC LIMIT 20
        """).fetchall()
        for row in rows:
            v = _num_or_none(row[3])
            if v is not None and v > 0:
                conn.execute("""INSERT INTO stats_records(record_type,value,icao,callsign,aircraft_type,set_at,extra)
                                VALUES('lowest_altitude_ever',?,?,?,?,?,'')""",
                             (v, row[0], row[1], row[2], row[4]))
                backfilled_recs += 1
                break

        # Peak simultaneous ever — reconstruct by counting distinct ICAOs
        # per 60-second bucket and taking the max
        row = conn.execute("""
            SELECT MAX(cnt) AS peak, MIN(ts) AS first_ts
            FROM (
                SELECT COUNT(DISTINCT icao) AS cnt, seen_at/60 AS bucket, MIN(seen_at) AS ts
                FROM all_sightings GROUP BY bucket
            )
        """).fetchone()
        if row and row[0]:
            peak = _num_or_none(row[0])
            if peak is not None:
                conn.execute("""INSERT INTO stats_records(record_type,value,icao,callsign,aircraft_type,set_at,extra)
                                VALUES('peak_simultaneous_ever',?,'',?,'',?,'')""",
                             (peak, f"{int(peak)} aircraft", row[1] or int(time.time())))
                backfilled_recs += 1
        # Note: furthest_ever cannot be backfilled because we don't know the
        # historical receiver location. It starts fresh from the next poll.

        if backfilled_recs > 0:
            logger.info(f"Backfilled {backfilled_recs} all-time records from all_sightings history")

    # v2.49.2: self-heal for fastest_ever when the stored record violates
    # the current type-specific speed ceiling. Runs on EVERY init_db call
    # (not gated on the "fresh table" flag above) so tightening the cap
    # in a future release retroactively corrects bad records.
    #
    # Scenario: before v2.49.2 the subsonic cap was 700kt and anything
    # below it (696kt on a Cessna 172, say) was accepted as a valid
    # "fastest ever". v2.49.2 introduces per-type caps (250kt for a C172);
    # this block detects records that were valid under the old cap but
    # aren't under the new one, deletes them, and recomputes from
    # all_sightings using the new logic.
    try:
        row = conn.execute(
            "SELECT value, aircraft_type FROM stats_records WHERE record_type='fastest_ever'"
        ).fetchone()
        if row:
            cur_val, cur_type = row
            cap = speed_ceiling_for_type(cur_type)
            if cur_val and cur_val > cap:
                logger.info(
                    f"Self-healing fastest_ever: stored {cur_val} kt for type "
                    f"{cur_type or '(unknown)'} exceeds current ceiling of {cap} kt. "
                    f"Recomputing from all_sightings with new per-type caps."
                )
                conn.execute("DELETE FROM stats_records WHERE record_type='fastest_ever'")
                # Reuse the same top-40 + first-valid pattern as the pre-Wave-3
                # migration. The LIMIT 40 is generous enough to skip past a
                # cluster of glitches at the top and still find real data.
                rows = conn.execute("""
                    SELECT icao, callsign, aircraft_type, speed, seen_at
                    FROM all_sightings WHERE speed IS NOT NULL
                      AND icao NOT LIKE '~%'
                    ORDER BY CAST(speed AS REAL) DESC LIMIT 40
                """).fetchall()
                for r in rows:
                    try:
                        v = float(r[3]) if r[3] is not None else None
                    except (TypeError, ValueError):
                        v = None
                    if v is None or v <= 0:
                        continue
                    if v > speed_ceiling_for_type(r[2]):
                        continue
                    conn.execute(
                        """INSERT INTO stats_records
                           (record_type, value, icao, callsign, aircraft_type, set_at, extra)
                           VALUES ('fastest_ever', ?, ?, ?, ?, ?, '')""",
                        (v, r[0], r[1], r[2], r[4]),
                    )
                    logger.info(
                        f"New fastest_ever after self-heal: {v} kt "
                        f"({r[1] or '(no callsign)'} · {r[2] or '(unknown type)'})"
                    )
                    break
                else:
                    logger.info(
                        "Self-heal found no valid candidates for fastest_ever "
                        "— record cleared; new one will set on next qualifying sighting."
                    )
    except sqlite3.OperationalError:
        # Tables don't exist yet (truly fresh install that hasn't even
        # run the record-backfill block above). Nothing to heal.
        pass

    # v2.49.0: persistent cache for ICAO→registration lookups resolved via
    # hexdb.io. Survives restarts so we don't re-request every registration
    # on every service bounce. Negative entries (hexdb said no) are stored
    # too, to prevent re-asking about ICAOs hexdb doesn't know.
    #
    # last_outcome distinguishes positive vs negative at a glance without
    # parsing registration (NULL means 'we looked up and hexdb said no').
    # Network errors are NOT persisted here — they're logged in hexdb_events
    # but the cache only records settled outcomes so the next lookup retries.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hexdb_cache (
            icao TEXT PRIMARY KEY,
            registration TEXT,
            resolved_at INTEGER NOT NULL,
            last_outcome TEXT NOT NULL,
            hit_count INTEGER DEFAULT 0,
            last_hit_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hexdb_resolved_at ON hexdb_cache(resolved_at)")

    # Rolling events log: one row per cache hit or miss so the Status card
    # can show exact 24-hour counts. Pruned to HEXDB_EVENTS_RETENTION_DAYS
    # by cleanup_old_data so the table stays bounded even in long-running
    # installs. Indexed on ts for the WHERE ts >= now-86400 queries.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hexdb_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            kind TEXT NOT NULL,
            icao TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hexdb_events_ts ON hexdb_events(ts)")

    conn.commit()
    conn.close()

    # v2.50.0: kick off the hourly-rollup backfill in a background thread.
    # init_db itself returns quickly; the thread does the heavy lifting.
    # Queries that depend on the rollup (Search count, Stats summaries,
    # the aircraft detail page's sightings count) fall back to raw
    # `all_sightings` while the migration is in progress, so users see
    # a working (slow) dashboard rather than a stalled startup. When the
    # migration completes, those queries switch to the fast rollup.
    _start_hourly_backfill_if_needed(db_path)

    # v3.4.6: start the parallel military_hourly + watchlist_hourly
    # backfills. Same pattern, same fallback behavior: while a backfill
    # is running, the corresponding Stats card queries fall back to
    # the raw per-poll tables (slower but correct). When the backfill
    # completes, queries automatically switch to the fast rollup via
    # the get_*_backfill_status() check in server.py.
    _start_military_backfill_if_needed(db_path)
    _start_watchlist_backfill_if_needed(db_path)

    logger.info(f"Database initialized at {db_path}")


# v2.50.0: module-level state for the rollup backfill. Tracks whether the
# migration is in progress, complete, or hasn't started; exposed via
# get_hourly_backfill_status() for the Status page and for the server's
# All-tab query path to decide rollup-vs-raw.
_hourly_backfill_state = {
    "phase": "unknown",       # "unknown" | "running" | "complete" | "skipped" | "error"
    "started_at": None,
    "finished_at": None,
    "rows_processed": 0,
    "rows_total": None,
    "error": None,
}
_hourly_backfill_lock = threading.Lock()


def get_hourly_backfill_status() -> Dict[str, Any]:
    """Return a snapshot of the rollup backfill state. Safe to call
    from any thread."""
    with _hourly_backfill_lock:
        return dict(_hourly_backfill_state)


def _start_hourly_backfill_if_needed(db_path: str) -> None:
    """Decide whether to run the rollup backfill, and if so kick off
    a background thread. Idempotent — checks _aerodrome_meta to see if
    we've already run it on this install."""
    global _hourly_backfill_state

    # Quick synchronous check: has this migration already run?
    try:
        conn = _open_db_conn(db_path)
        # Ensure the meta table exists (other migrations also use it,
        # but we shouldn't depend on order).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _aerodrome_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT value FROM _aerodrome_meta WHERE key = 'hourly_rollup_backfilled'"
        ).fetchone()
        if row and row[0] == "1":
            with _hourly_backfill_lock:
                _hourly_backfill_state.update({"phase": "complete"})
            conn.close()
            return

        # Estimate total work — gives the UI something to show progress
        # against. COUNT(*) on all_sightings is cheap if the index is
        # there but slow on a freshly-restored Pi-scale DB; cap the
        # query time with a reasonable timeout via the SQL itself.
        try:
            n_rows = conn.execute("SELECT COUNT(*) FROM all_sightings").fetchone()[0]
        except sqlite3.OperationalError:
            n_rows = None
        conn.close()
    except Exception as e:
        logger.warning(f"Could not check hourly_rollup_backfilled state: {e}")
        with _hourly_backfill_lock:
            _hourly_backfill_state.update({"phase": "error", "error": str(e)})
        return

    # If there's no data yet (fresh install), no backfill is needed —
    # the online write path in fetch_and_store will populate the rollup
    # going forward. Mark as complete immediately.
    if not n_rows:
        try:
            conn = _open_db_conn(db_path)
            conn.execute(
                "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
                ("hourly_rollup_backfilled", "1"),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Could not mark fresh-install backfill complete: {e}")
        with _hourly_backfill_lock:
            _hourly_backfill_state.update({"phase": "complete", "rows_total": 0})
        return

    # Real backfill needed. Kick off a daemon thread.
    with _hourly_backfill_lock:
        _hourly_backfill_state.update({
            "phase": "running",
            "started_at": int(time.time()),
            "rows_total": n_rows,
        })

    t = threading.Thread(
        target=_run_hourly_backfill,
        args=(db_path, n_rows),
        name="aerodrome-hourly-backfill",
        daemon=True,
    )
    t.start()
    logger.info(
        f"Started hourly-rollup backfill in background thread "
        f"(estimated {n_rows:,} source rows to process)"
    )


def _run_hourly_backfill(db_path: str, n_rows: int) -> None:
    """Backfill sightings_hourly from existing all_sightings rows.
    Runs in a daemon thread so service startup isn't blocked.

    Uses a window-function approach: ROW_NUMBER over (icao, hour_bucket)
    ordered by seen_at DESC tags the "latest sighting in this hour" row,
    then we filter to rn=1 to extract the last_* values, joining back
    against an aggregate query for the count/min/max/first/last_seen_at.

    Single pass over all_sightings — much faster than correlated
    subqueries (which scale O(n²) per bucket). Tested: 20k rows in
    ~0.3s, scaling roughly linearly to ~2 minutes for 7.4M rows."""
    global _hourly_backfill_state
    try:
        conn = _open_db_conn(db_path)
        t0 = time.time()

        # The aggregate part: counts, extremes, and timing per (icao, bucket).
        # The "latest row" part: pick the row with MAX(seen_at) per bucket
        # to provide the last_* snapshot fields. Joined inline.
        #
        # SQLite handles a CTE-with-window-function efficiently here because
        # all_sightings has idx_all_seen / idx_all_icao that the planner
        # can use for the partition. The output is pre-grouped — no
        # post-INSERT dedup needed.
        conn.execute("BEGIN")
        conn.execute("""
            INSERT OR REPLACE INTO sightings_hourly (
                icao, hour_bucket, callsign, aircraft_type, type_desc,
                sighting_count, first_seen_at, last_seen_at,
                last_lat, last_lon, last_altitude, last_speed,
                min_altitude, max_altitude, max_speed, last_squawk
            )
            WITH ranked AS (
                SELECT
                    icao,
                    (seen_at / 3600) * 3600 AS hour_bucket,
                    callsign, aircraft_type, type_desc,
                    seen_at, lat, lon, altitude, speed, squawk,
                    ROW_NUMBER() OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                        ORDER BY seen_at DESC
                    ) AS rn,
                    COUNT(*) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_count,
                    MIN(seen_at) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_first,
                    MAX(seen_at) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_last,
                    MIN(altitude) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_min_alt,
                    MAX(altitude) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_max_alt,
                    MAX(speed) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_max_speed
                FROM all_sightings
            )
            SELECT
                icao, hour_bucket,
                COALESCE(callsign, '')      AS callsign,
                COALESCE(aircraft_type, '') AS aircraft_type,
                COALESCE(type_desc, '')     AS type_desc,
                bucket_count, bucket_first, bucket_last,
                lat, lon, altitude, speed,
                bucket_min_alt, bucket_max_alt, bucket_max_speed,
                COALESCE(squawk, '')        AS last_squawk
            FROM ranked
            WHERE rn = 1
        """)
        rows_inserted = conn.execute(
            "SELECT COUNT(*) FROM sightings_hourly"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
            ("hourly_rollup_backfilled", "1"),
        )
        conn.commit()
        conn.close()
        elapsed = time.time() - t0

        with _hourly_backfill_lock:
            _hourly_backfill_state.update({
                "phase": "complete",
                "finished_at": int(time.time()),
                "rows_processed": rows_inserted,
            })
        logger.info(
            f"Hourly-rollup backfill complete: {rows_inserted:,} rollup rows "
            f"from {n_rows:,} source sightings in {elapsed:.1f}s"
        )
    except Exception as e:
        logger.error(f"Hourly-rollup backfill failed: {e}")
        with _hourly_backfill_lock:
            _hourly_backfill_state.update({
                "phase": "error",
                "finished_at": int(time.time()),
                "error": str(e),
            })


# --- v3.4.6: military_hourly + watchlist_hourly backfill machinery ---
# Same pattern as the v2.50.0 hourly_backfill above, applied to the
# two new rollup tables. Each backfill runs in its own daemon thread,
# tracks its own state, marks completion in _aerodrome_meta with its
# own key. All three can run concurrently (touching different source
# and target tables).

_military_backfill_state = {
    "phase": "unknown",
    "started_at": None,
    "finished_at": None,
    "rows_processed": 0,
    "rows_total": None,
    "error": None,
}
_military_backfill_lock = threading.Lock()

_watchlist_backfill_state = {
    "phase": "unknown",
    "started_at": None,
    "finished_at": None,
    "rows_processed": 0,
    "rows_total": None,
    "error": None,
}
_watchlist_backfill_lock = threading.Lock()


def get_military_backfill_status() -> Dict[str, Any]:
    """Snapshot of the military_hourly backfill state."""
    with _military_backfill_lock:
        return dict(_military_backfill_state)


def get_watchlist_backfill_status() -> Dict[str, Any]:
    """Snapshot of the watchlist_hourly backfill state."""
    with _watchlist_backfill_lock:
        return dict(_watchlist_backfill_state)


def _start_military_backfill_if_needed(db_path: str) -> None:
    """Decide whether to run the military_hourly backfill, and if so
    kick off a background thread. Idempotent — checks _aerodrome_meta."""
    global _military_backfill_state

    try:
        conn = _open_db_conn(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _aerodrome_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT value FROM _aerodrome_meta WHERE key = 'military_hourly_backfilled'"
        ).fetchone()
        if row and row[0] == "1":
            with _military_backfill_lock:
                _military_backfill_state.update({"phase": "complete"})
            conn.close()
            return

        try:
            n_rows = conn.execute("SELECT COUNT(*) FROM military_sightings").fetchone()[0]
        except sqlite3.OperationalError:
            n_rows = None
        conn.close()
    except Exception as e:
        logger.warning(f"Could not check military_hourly_backfilled state: {e}")
        with _military_backfill_lock:
            _military_backfill_state.update({"phase": "error", "error": str(e)})
        return

    # Fresh install — no rows to backfill, just mark done so the
    # online write path is the sole writer going forward.
    if not n_rows:
        try:
            conn = _open_db_conn(db_path)
            conn.execute(
                "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
                ("military_hourly_backfilled", "1"),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Could not mark fresh-install military backfill complete: {e}")
        with _military_backfill_lock:
            _military_backfill_state.update({"phase": "complete", "rows_total": 0})
        return

    with _military_backfill_lock:
        _military_backfill_state.update({
            "phase": "running",
            "started_at": int(time.time()),
            "rows_total": n_rows,
        })
    t = threading.Thread(
        target=_run_military_backfill,
        args=(db_path, n_rows),
        name="aerodrome-military-backfill",
        daemon=True,
    )
    t.start()
    logger.info(
        f"Started military_hourly backfill in background thread "
        f"(estimated {n_rows:,} source rows to process)"
    )


def _start_watchlist_backfill_if_needed(db_path: str) -> None:
    """Decide whether to run the watchlist_hourly backfill. Same shape
    as the military version above."""
    global _watchlist_backfill_state

    try:
        conn = _open_db_conn(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _aerodrome_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT value FROM _aerodrome_meta WHERE key = 'watchlist_hourly_backfilled'"
        ).fetchone()
        if row and row[0] == "1":
            with _watchlist_backfill_lock:
                _watchlist_backfill_state.update({"phase": "complete"})
            conn.close()
            return

        try:
            n_rows = conn.execute("SELECT COUNT(*) FROM watchlist_sightings").fetchone()[0]
        except sqlite3.OperationalError:
            n_rows = None
        conn.close()
    except Exception as e:
        logger.warning(f"Could not check watchlist_hourly_backfilled state: {e}")
        with _watchlist_backfill_lock:
            _watchlist_backfill_state.update({"phase": "error", "error": str(e)})
        return

    if not n_rows:
        try:
            conn = _open_db_conn(db_path)
            conn.execute(
                "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
                ("watchlist_hourly_backfilled", "1"),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Could not mark fresh-install watchlist backfill complete: {e}")
        with _watchlist_backfill_lock:
            _watchlist_backfill_state.update({"phase": "complete", "rows_total": 0})
        return

    with _watchlist_backfill_lock:
        _watchlist_backfill_state.update({
            "phase": "running",
            "started_at": int(time.time()),
            "rows_total": n_rows,
        })
    t = threading.Thread(
        target=_run_watchlist_backfill,
        args=(db_path, n_rows),
        name="aerodrome-watchlist-backfill",
        daemon=True,
    )
    t.start()
    logger.info(
        f"Started watchlist_hourly backfill in background thread "
        f"(estimated {n_rows:,} source rows to process)"
    )


def _run_military_backfill(db_path: str, n_rows: int) -> None:
    """Backfill military_hourly from existing military_sightings rows.
    Runs in a daemon thread. Single-pass aggregate query, same
    window-function shape as _run_hourly_backfill but with the
    military-specific columns (no last_lat/lon/altitude/speed/min/max,
    PLUS special_label preserved from the most-recent row in each bucket
    via the rn=1 window). Idempotent via _aerodrome_meta marker."""
    global _military_backfill_state
    try:
        conn = _open_db_conn(db_path)
        t0 = time.time()
        conn.execute("BEGIN")
        conn.execute("""
            INSERT OR REPLACE INTO military_hourly (
                icao, hour_bucket, callsign, aircraft_type, type_desc,
                special_label, sighting_count, first_seen_at, last_seen_at
            )
            WITH ranked AS (
                SELECT
                    icao,
                    (seen_at / 3600) * 3600 AS hour_bucket,
                    callsign, aircraft_type, type_desc, special_label,
                    seen_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                        ORDER BY seen_at DESC
                    ) AS rn,
                    COUNT(*) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_count,
                    MIN(seen_at) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_first,
                    MAX(seen_at) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600
                    ) AS bucket_last
                FROM military_sightings
            )
            SELECT
                icao, hour_bucket,
                COALESCE(callsign, '')      AS callsign,
                COALESCE(aircraft_type, '') AS aircraft_type,
                COALESCE(type_desc, '')     AS type_desc,
                COALESCE(special_label, '') AS special_label,
                bucket_count, bucket_first, bucket_last
            FROM ranked
            WHERE rn = 1
        """)
        rows_inserted = conn.execute(
            "SELECT COUNT(*) FROM military_hourly"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
            ("military_hourly_backfilled", "1"),
        )
        conn.commit()
        conn.close()
        elapsed = time.time() - t0
        with _military_backfill_lock:
            _military_backfill_state.update({
                "phase": "complete",
                "finished_at": int(time.time()),
                "rows_processed": rows_inserted,
            })
        logger.info(
            f"military_hourly backfill complete: {rows_inserted:,} rollup rows "
            f"from {n_rows:,} source sightings in {elapsed:.1f}s"
        )
    except Exception as e:
        logger.error(f"military_hourly backfill failed: {e}")
        with _military_backfill_lock:
            _military_backfill_state.update({
                "phase": "error",
                "finished_at": int(time.time()),
                "error": str(e),
            })


def _run_watchlist_backfill(db_path: str, n_rows: int) -> None:
    """Backfill watchlist_hourly from existing watchlist_sightings rows.
    PK is THREE-way (icao, hour_bucket, watchlist_label), so the
    PARTITION BY in the window function is three columns, not two.
    Otherwise same shape as the military backfill. Idempotent."""
    global _watchlist_backfill_state
    try:
        conn = _open_db_conn(db_path)
        t0 = time.time()
        conn.execute("BEGIN")
        conn.execute("""
            INSERT OR REPLACE INTO watchlist_hourly (
                icao, hour_bucket, watchlist_label, callsign,
                aircraft_type, type_desc, sighting_count,
                first_seen_at, last_seen_at
            )
            WITH ranked AS (
                SELECT
                    icao,
                    (seen_at / 3600) * 3600 AS hour_bucket,
                    watchlist_label,
                    callsign, aircraft_type, type_desc,
                    seen_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600, watchlist_label
                        ORDER BY seen_at DESC
                    ) AS rn,
                    COUNT(*) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600, watchlist_label
                    ) AS bucket_count,
                    MIN(seen_at) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600, watchlist_label
                    ) AS bucket_first,
                    MAX(seen_at) OVER (
                        PARTITION BY icao, (seen_at / 3600) * 3600, watchlist_label
                    ) AS bucket_last
                FROM watchlist_sightings
                WHERE watchlist_label IS NOT NULL AND watchlist_label != ''
            )
            SELECT
                icao, hour_bucket, watchlist_label,
                COALESCE(callsign, '')      AS callsign,
                COALESCE(aircraft_type, '') AS aircraft_type,
                COALESCE(type_desc, '')     AS type_desc,
                bucket_count, bucket_first, bucket_last
            FROM ranked
            WHERE rn = 1
        """)
        rows_inserted = conn.execute(
            "SELECT COUNT(*) FROM watchlist_hourly"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
            ("watchlist_hourly_backfilled", "1"),
        )
        conn.commit()
        conn.close()
        elapsed = time.time() - t0
        with _watchlist_backfill_lock:
            _watchlist_backfill_state.update({
                "phase": "complete",
                "finished_at": int(time.time()),
                "rows_processed": rows_inserted,
            })
        logger.info(
            f"watchlist_hourly backfill complete: {rows_inserted:,} rollup rows "
            f"from {n_rows:,} source sightings in {elapsed:.1f}s"
        )
    except Exception as e:
        logger.error(f"watchlist_hourly backfill failed: {e}")
        with _watchlist_backfill_lock:
            _watchlist_backfill_state.update({
                "phase": "error",
                "finished_at": int(time.time()),
                "error": str(e),
            })


def cleanup_old_data(db_path: str, retention: dict):
    """Remove data older than each tab's retention window."""
    now = int(time.time())
    conn = _open_db_conn(db_path)

    mil_cutoff = now - (retention["military_days"] * 86400)
    watch_cutoff = now - (retention["watchlist_days"] * 86400)
    all_cutoff = now - (retention["all_days"] * 86400)

    d1 = conn.execute("DELETE FROM military_sightings WHERE seen_at < ?", (mil_cutoff,)).rowcount
    d2 = conn.execute("DELETE FROM watchlist_sightings WHERE seen_at < ?", (watch_cutoff,)).rowcount
    d3 = conn.execute("DELETE FROM all_sightings WHERE seen_at < ?", (all_cutoff,)).rowcount

    # v3.4.6: prune the military_hourly and watchlist_hourly rollups
    # in parallel with their raw parent tables. Same cutoff each so
    # the rollup retention always matches what its count queries see
    # — no orphaned rollup rows past the raw retention boundary.
    # last_seen_at is used as the cutoff key (mirrors how the raw
    # tables use seen_at) so a bucket's most-recent observation
    # determines whether the bucket survives.
    try:
        d1b = conn.execute("DELETE FROM military_hourly WHERE last_seen_at < ?", (mil_cutoff,)).rowcount
    except sqlite3.OperationalError:
        d1b = 0  # table doesn't exist on truly pre-v3.4.6 installs
    try:
        d2b = conn.execute("DELETE FROM watchlist_hourly WHERE last_seen_at < ?", (watch_cutoff,)).rowcount
    except sqlite3.OperationalError:
        d2b = 0

    # v2.49.0: prune hexdb_events to keep the rolling log bounded. Cache
    # entries in hexdb_cache are NOT pruned here — they expire by TTL via
    # the freshness check in _hexdb_cache_check, and dropping stale entries
    # from the cache would just cause unnecessary hexdb re-fetches. We
    # tolerate "stale rows I haven't refreshed yet" as a cheap idle state.
    events_cutoff = now - (HEXDB_EVENTS_RETENTION_DAYS * 86400)
    d4 = conn.execute("DELETE FROM hexdb_events WHERE ts < ?", (events_cutoff,)).rowcount

    # v2.87.0: prune concurrent_minute rollup rows. Same retention as
    # all_sightings since the rollup mirrors that table's coverage —
    # a Stats card querying historical days won't see anything in
    # concurrent_minute for days that have aged out of all_sightings
    # anyway, so keeping the rollup beyond all_days would just be
    # storage waste. Tiny table (~1440 rows/day) so the impact is
    # small either way; matching all_days is the cleaner invariant.
    d5 = conn.execute(
        "DELETE FROM concurrent_minute WHERE minute_bucket < ?",
        (all_cutoff,)
    ).rowcount

    conn.commit()
    conn.close()

    if d1 or d2 or d3 or d4 or d5 or d1b or d2b:
        logger.info(
            f"Cleanup: removed {d1} military ({d1b} rollup), "
            f"{d2} watchlist ({d2b} rollup), {d3} all, "
            f"{d4} hexdb-events, {d5} concurrent-minute old entries"
        )


# =============================================================================
# Military Detection
# =============================================================================

def is_military(aircraft: Dict, config: Dict) -> Tuple[bool, str]:
    """Returns (is_military, special_label)."""
    mil_config = config.get("military", {})
    icao = aircraft.get("hex", "").strip().upper()
    callsign = aircraft.get("flight", "").strip().upper()
    db_flags = aircraft.get("dbFlags", 0)

    # Special aircraft (AF1, AF2, etc.)
    specials = mil_config.get("special_aircraft", {})
    for special_icao, info in specials.items():
        if icao == special_icao.upper():
            return True, info.get("label", "")

    # dbFlags bit 0
    if mil_config.get("use_db_flags", True) and isinstance(db_flags, int) and (db_flags & 1):
        return True, ""

    # Explicit military flag
    if aircraft.get("military", False):
        return True, ""

    # Callsign prefixes
    for prefix in mil_config.get("callsign_prefixes", []):
        if callsign and callsign.startswith(prefix.upper()):
            return True, ""

    # ICAO hex prefixes
    for prefix in mil_config.get("icao_prefixes", []):
        if icao and icao.startswith(prefix.upper()):
            return True, ""

    return False, ""


# =============================================================================
# Watchlist
# =============================================================================

def build_watchlist_lookup(config: Dict) -> Dict:
    """Build fast lookup from config. Resolves tail numbers at startup."""
    lookup = {
        "icao_map": {},             # exact ICAO hex → label
        "callsign_prefixes": [],    # list of (prefix, label) tuples
        "model_substrings": [],     # list of (substring_lowercase, label) tuples
                                    # — matches against aircraft_type (e.g. S22T)
                                    # and type_desc (e.g. "CIRRUS SR-22 Turbo")
    }
    watchlist = config.get("watchlist", []) or []

    for entry in watchlist:
        label = entry.get("label", "Watched")

        if entry.get("icao"):
            icao = entry["icao"].strip().upper()
            lookup["icao_map"][icao] = label
            logger.info(f"Watchlist: ICAO {icao} → '{label}'")
        elif entry.get("tail"):
            icao = resolve_tail_to_icao(entry["tail"])
            if icao:
                lookup["icao_map"][icao] = label
            else:
                logger.warning(f"Watchlist: could not resolve tail '{entry['tail']}'")
        elif entry.get("callsign"):
            prefix = entry["callsign"].strip().upper()
            lookup["callsign_prefixes"].append((prefix, label))
            logger.info(f"Watchlist: callsign prefix '{prefix}' → '{label}'")
        elif entry.get("model"):
            needle = entry["model"].strip().lower()
            if needle:
                lookup["model_substrings"].append((needle, label))
                logger.info(f"Watchlist: model substring '{needle}' → '{label}'")

    return lookup


def match_watchlist(aircraft: Dict, lookup: Dict) -> Optional[str]:
    """Returns watchlist label if matched, None otherwise."""
    icao = aircraft.get("hex", "").strip().upper()
    callsign = aircraft.get("flight", "").strip().upper()

    if icao in lookup["icao_map"]:
        return lookup["icao_map"][icao]

    for prefix, label in lookup["callsign_prefixes"]:
        if callsign and callsign.startswith(prefix):
            return label

    # Model match — case-insensitive substring against type code and description
    if lookup.get("model_substrings"):
        ac_type = (aircraft.get("t") or aircraft.get("type") or "").lower()
        ac_desc = (aircraft.get("desc") or aircraft.get("description") or "").lower()
        for needle, label in lookup["model_substrings"]:
            if needle in ac_type or needle in ac_desc:
                return label

    return None


# =============================================================================
# Normalize
# =============================================================================

def _to_number(v):
    """Coerce v to a float if possible, else None. Handles strings like
    'ground' (which some ADS-B feeds send for altitude) by returning None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize(raw: Dict) -> Dict:
    """Convert raw receiver data into clean format."""
    return {
        "hex": raw.get("hex", "").strip().upper(),
        "callsign": (raw.get("flight") or raw.get("callsign") or "").strip(),
        "speed": _to_number(raw.get("gs") or raw.get("speed")),
        "lat": _to_number(raw.get("lat")),
        "lon": _to_number(raw.get("lon")),
        "altitude": _to_number(raw.get("alt_baro") or raw.get("altitude") or raw.get("alt")),
        "aircraft_type": raw.get("t") or raw.get("type") or "",
        "type_desc": raw.get("desc") or raw.get("description") or "",
        # Squawk is a 4-digit octal transponder code as a string, e.g. "1200".
        # Normalized to uppercase zero-padded 4 chars. Invalid/missing → "".
        "squawk": _normalize_squawk(raw.get("squawk")),
    }


def _normalize_squawk(raw) -> str:
    """ADS-B feeds expose squawk as a 4-digit string or number. We want a
    clean 4-char string or empty string if the value is missing/malformed.
    Valid octal squawks only use digits 0-7, but we don't enforce that here
    — we accept anything 4 digits since some receivers occasionally send
    slightly off values and we'd rather preserve than drop."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # Strip leading zeros that some formatters add beyond 4 chars, then
    # zero-pad back up to 4. e.g. "7700" stays "7700", "200" → "0200".
    try:
        return s.zfill(4)[-4:] if s.isdigit() else s[:4]
    except Exception:
        return ""


# =============================================================================
# All-Time Records (Wave 3)
# =============================================================================


def _haversine(lat1, lon1, lat2, lon2, unit="mi"):
    """Great-circle distance between two points. Returns unit as specified.

    v2.79.0: thin wrapper around distance.haversine() — preserved as a
    private name in this module because line 2416 calls it directly.
    distance.haversine() is the canonical home; this delegate keeps the
    call site stable.
    """
    return _dist_haversine(lat1, lon1, lat2, lon2, unit=unit)


def _update_record(conn, record_type: str, new_value: float, beats: str,
                   ac: Dict, now: int, extra: str = ""):
    """Update an all-time record if the new value beats the stored one.

    beats='gt' → update if new_value > stored value (records like fastest, furthest, highest)
    beats='lt' → update if new_value < stored value (records like lowest altitude)
    """
    # Coerce new_value to float. If it's not numeric (e.g. string "ground"
    # from some ADS-B feeds), skip this update rather than crashing.
    try:
        new_value = float(new_value)
    except (TypeError, ValueError):
        return False

    row = conn.execute(
        "SELECT value FROM stats_records WHERE record_type = ?",
        (record_type,)
    ).fetchone()
    if row is None:
        # First record — insert
        conn.execute("""
            INSERT INTO stats_records (record_type, value, icao, callsign,
                                        aircraft_type, set_at, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (record_type, new_value, ac.get("hex", ""), ac.get("callsign", ""),
              ac.get("aircraft_type", ""), now, extra))
        return True
    # Coerce stored value too — it came from SQLite as whatever type was
    # originally inserted, which might predate this fix.
    try:
        stored = float(row[0])
    except (TypeError, ValueError):
        # Corrupt stored value — overwrite it with our new good value
        conn.execute("""
            UPDATE stats_records
            SET value = ?, icao = ?, callsign = ?, aircraft_type = ?, set_at = ?, extra = ?
            WHERE record_type = ?
        """, (new_value, ac.get("hex", ""), ac.get("callsign", ""),
              ac.get("aircraft_type", ""), now, extra, record_type))
        return True
    if (beats == "gt" and new_value > stored) or (beats == "lt" and new_value < stored):
        conn.execute("""
            UPDATE stats_records
            SET value = ?, icao = ?, callsign = ?, aircraft_type = ?, set_at = ?, extra = ?
            WHERE record_type = ?
        """, (new_value, ac.get("hex", ""), ac.get("callsign", ""),
              ac.get("aircraft_type", ""), now, extra, record_type))
        # New record! Fire notification. Formatted label like
        # "Fastest ever: 612 kt by UAL1234 (B738)"
        _fire_new_record_notification(record_type, new_value, stored, ac, extra)
        return True
    return False


# Mapping of record_type to a human label and unit suffix, for notification
# body formatting. The record_type values come from _update_record callers.
_RECORD_LABELS = {
    "fastest_ever":             ("Fastest aircraft ever",     "kt"),
    "highest_altitude_ever":    ("Highest altitude ever",     "ft"),
    "lowest_altitude_ever":     ("Lowest altitude ever",      "ft"),
    "furthest_ever":            ("Furthest aircraft ever",    ""),   # unit in extra
    "peak_simultaneous_ever":   ("Peak simultaneous aircraft", ""),
    "longest_track_ever":       ("Longest continuous track",  ""),
}


def _fire_new_record_notification(record_type: str, new_value: float,
                                   old_value: float, ac: Dict, extra: str) -> None:
    """Format and fire a new_record notification. Best-effort."""
    label, unit = _RECORD_LABELS.get(record_type, (record_type, ""))
    if record_type == "furthest_ever":
        unit = extra or "mi"
    if record_type == "peak_simultaneous_ever":
        # extra is blank, value is a count; callsign carries the "N aircraft" label
        value_str = f"{int(new_value)} aircraft"
    elif unit in ("kt", "ft"):
        value_str = f"{int(round(new_value))} {unit}"
    else:
        value_str = f"{new_value:.1f} {unit}".strip()

    # "by UAL1234 (B738)" tag when we have aircraft context
    who = ""
    callsign = (ac.get("callsign") or "").strip()
    hex_code = (ac.get("hex") or "").upper()
    a_type = (ac.get("aircraft_type") or "").strip()
    if record_type != "peak_simultaneous_ever":
        if callsign or hex_code:
            who = f" by {callsign or hex_code}"
            if a_type:
                who += f" ({a_type})"

    title = f"New record: {label}"
    body = (f"{label}: {value_str}{who}. "
            f"Previous record: {int(round(old_value))}.")
    _safe_notify("new_record", title, body,
                 priority="high", tags=["trophy"],
                 aircraft_icao=hex_code or None,
                 click_route="stats")


# =============================================================================
# Main Collection Cycle
# =============================================================================

def fetch_and_store(config: Dict, watchlist_lookup: Dict):
    """
    One poll cycle:
      1. Fetch all aircraft from receiver
      2. Store every aircraft in all_sightings
      3. Tag military matches → military_sightings
      4. Tag watchlist matches → watchlist_sightings
      5. Clean up old data per retention settings
    """
    receiver = config["receiver"]
    url = f"http://{receiver['ip']}:{receiver['port']}{receiver['path']}"
    db_path = config["data"]["db_file"]
    now = int(time.time())

    # Fetch
    global _consecutive_failed_polls, _offline_notified, _last_offline_reason
    notif_cfg = (config.get("notifications") or {})
    offline_threshold = int(
        (notif_cfg.get("receiver_offline") or {}).get("consecutive_failed_polls", 5)
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        # v3.4.40: validate the response actually looks like ADS-B data
        # before falling through to a generic JSON parse error. The
        # symptom this catches: the configured receiver URL is being
        # served by something OTHER than the expected ADS-B feed
        # (Aerodrome's synthetic feeder failed to start and a different
        # app on the same port is responding, or the user pointed the
        # config at the wrong service). The other service returns
        # HTTP 200 with HTML or non-ADS-B JSON, the collector's old
        # path tried to parse it and surfaced "Expecting value: line 1
        # column 1 (char 0)" — accurate but unhelpful.
        content_type = resp.headers.get("Content-Type", "").lower()
        body_prefix = resp.text[:1].strip() if resp.text else ""
        if "html" in content_type or body_prefix in ("<",):
            raise _NonAdsbResponse(
                f"Receiver URL returned {content_type or 'HTML'} content — "
                f"this URL is reachable but isn't an ADS-B feed. "
                f"Check that the configured port matches the feed source "
                f"(on demo installs, see 'systemctl status aerodrome-synthetic-feeder')."
            )
        try:
            data = resp.json()
        except ValueError:
            raise _NonAdsbResponse(
                f"Receiver URL returned non-JSON content "
                f"({len(resp.text)} bytes, first 60 chars: {resp.text[:60]!r}). "
                f"Check that the configured port matches the feed source."
            )
        # Shape check: dump1090-fa returns {now, aircraft: [...], messages}.
        # Other JSON endpoints might return 200 with {error: ...} or similar.
        if isinstance(data, dict) and "aircraft" not in data:
            raise _NonAdsbResponse(
                f"Receiver URL returned JSON without an 'aircraft' key "
                f"(got keys: {sorted(data.keys())[:6]}). "
                f"Is the configured URL pointing at an ADS-B feed?"
            )
        raw_list = data.get("aircraft", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except _NonAdsbResponse as e:
        logger.error(str(e))
        _consecutive_failed_polls += 1
        _last_offline_reason = str(e)
        # v3.4.42: after 3 consecutive non-ADS-B errors, attempt auto-recovery
        # via the demo-mode port-fallback chain. Once per process lifetime
        # (the function's internal guard), gated on demo mode + feeder unit
        # existing. If recovery succeeds, mutate-in-memory means the next
        # poll uses the new URL — we reset the streak counter so the status
        # card clears cleanly. If recovery fails (all ports taken, sudoers
        # not refreshed, etc), fall through to the existing notification
        # path and let the user intervene manually.
        if _consecutive_failed_polls >= _NON_ADSB_RECOVERY_THRESHOLD:
            recovered_port = _attempt_demo_port_recovery(config)
            if recovered_port is not None:
                _consecutive_failed_polls = 0
                _last_offline_reason = ""
                # Skip the rest of this poll cycle's offline-notification
                # check below — we just took recovery action; let the
                # next poll evaluate fresh state.
                return
        if _consecutive_failed_polls >= offline_threshold and not _offline_notified:
            _safe_notify(
                "receiver_offline",
                "Receiver returning non-ADS-B data",
                f"Aerodrome is reaching the receiver URL at "
                f"{receiver['ip']}:{receiver['port']} but the content isn't an "
                f"ADS-B feed. {e}",
                priority="high",
                tags=["warning"],
                click_route="status",
            )
            _offline_notified = True
        return
    except requests.ConnectionError as e:
        logger.error(f"Cannot connect to receiver at {url}")
        _consecutive_failed_polls += 1
        _last_offline_reason = f"ConnectionError: {e}"
        if _consecutive_failed_polls >= offline_threshold and not _offline_notified:
            _safe_notify(
                "receiver_offline",
                "Receiver offline",
                f"Aerodrome hasn't reached the ADS-B receiver "
                f"at {receiver['ip']}:{receiver['port']} for "
                f"{_consecutive_failed_polls} consecutive polls. Last error: {_last_offline_reason}",
                priority="high",
                tags=["warning"],
                click_route="status",
            )
            _offline_notified = True
        return
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        _consecutive_failed_polls += 1
        _last_offline_reason = str(e)
        if _consecutive_failed_polls >= offline_threshold and not _offline_notified:
            _safe_notify(
                "receiver_offline",
                "Receiver offline",
                f"Aerodrome is getting errors from the receiver at "
                f"{receiver['ip']}:{receiver['port']}. Last error: {e}",
                priority="high",
                tags=["warning"],
                click_route="status",
            )
            _offline_notified = True
        return

    # Poll succeeded. If we'd previously declared it offline, fire recovered.
    if _offline_notified:
        _safe_notify(
            "receiver_recovered",
            "Receiver recovered",
            f"The ADS-B receiver at {receiver['ip']}:{receiver['port']} "
            f"is reachable again after {_consecutive_failed_polls} failed polls.",
            priority="default",
            tags=["white_check_mark"],
            click_route="status",
        )
        _offline_notified = False
    _consecutive_failed_polls = 0
    _last_offline_reason = ""

    if not raw_list:
        logger.debug("No aircraft from receiver")
        return

    conn = _open_db_conn(db_path)
    all_count = 0
    mil_count = 0
    watch_count = 0

    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        ac = normalize(raw)
        if not ac["hex"]:
            continue

        # Store in all_sightings
        conn.execute("""
            INSERT INTO all_sightings
            (icao, callsign, speed, lat, lon, altitude, aircraft_type, type_desc, seen_at, squawk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ac["hex"], ac["callsign"], ac["speed"], ac["lat"], ac["lon"],
              ac["altitude"], ac["aircraft_type"], ac["type_desc"], now, ac["squawk"]))
        all_count += 1

        # v2.50.0: also upsert into the hourly rollup. Per-aircraft-per-hour
        # row that aggregates count + first/last + extremes + last-state
        # for the queries that originally backed the All tab page render
        # (since Phase 1D, the same aggregates back Search and Stats).
        # See design doc section 5 (Option A — online populate) for the
        # rationale; tl;dr is that the write amplification is small (one
        # extra UPSERT per poll per aircraft) and avoids the UNION-with-
        # current-hour complexity a batch-rollup approach would force on
        # read paths.
        #
        # Conflict resolution per column:
        #   callsign / aircraft_type / type_desc / last_squawk:
        #       COALESCE(NULLIF(excluded.x, ''), table.x)
        #       — keep the new value if non-empty, else preserve existing
        #   sighting_count: incremented
        #   first_seen_at: MIN — first sighting in hour wins
        #   last_seen_at, last_lat/lon/altitude/speed:
        #       always overwritten — most recent observation
        #   min_altitude, max_altitude, max_speed:
        #       MIN/MAX — extremes preserved across the hour
        hour_bucket = (now // 3600) * 3600
        # v2.87.1: also compute min_nonzero_altitude — the per-bucket
        # minimum altitude across non-zero observations only. None when
        # this poll's altitude is null or zero (taxi/ground/no data);
        # the conflict resolution below skips nulls so existing
        # bucket values survive. Source for the Stats lowest_altitude
        # card; see migration v5 docstring for the why-not-min_altitude
        # rationale.
        _alt = ac["altitude"]
        _alt_nonzero = _alt if (_alt is not None and _alt > 0) else None
        conn.execute("""
            INSERT INTO sightings_hourly (
                icao, hour_bucket, callsign, aircraft_type, type_desc,
                sighting_count, first_seen_at, last_seen_at,
                last_lat, last_lon, last_altitude, last_speed,
                min_altitude, max_altitude, max_speed, last_squawk,
                min_nonzero_altitude
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (icao, hour_bucket) DO UPDATE SET
                callsign       = COALESCE(NULLIF(excluded.callsign, ''),       sightings_hourly.callsign),
                aircraft_type  = COALESCE(NULLIF(excluded.aircraft_type, ''),  sightings_hourly.aircraft_type),
                type_desc      = COALESCE(NULLIF(excluded.type_desc, ''),      sightings_hourly.type_desc),
                sighting_count = sightings_hourly.sighting_count + 1,
                last_seen_at   = excluded.last_seen_at,
                last_lat       = excluded.last_lat,
                last_lon       = excluded.last_lon,
                last_altitude  = excluded.last_altitude,
                last_speed     = excluded.last_speed,
                last_squawk    = COALESCE(NULLIF(excluded.last_squawk, ''),    sightings_hourly.last_squawk),
                min_altitude   = CASE
                    WHEN excluded.min_altitude IS NULL THEN sightings_hourly.min_altitude
                    WHEN sightings_hourly.min_altitude IS NULL THEN excluded.min_altitude
                    ELSE MIN(sightings_hourly.min_altitude, excluded.min_altitude)
                END,
                max_altitude   = CASE
                    WHEN excluded.max_altitude IS NULL THEN sightings_hourly.max_altitude
                    WHEN sightings_hourly.max_altitude IS NULL THEN excluded.max_altitude
                    ELSE MAX(sightings_hourly.max_altitude, excluded.max_altitude)
                END,
                max_speed      = CASE
                    WHEN excluded.max_speed IS NULL THEN sightings_hourly.max_speed
                    WHEN sightings_hourly.max_speed IS NULL THEN excluded.max_speed
                    ELSE MAX(sightings_hourly.max_speed, excluded.max_speed)
                END,
                min_nonzero_altitude = CASE
                    WHEN excluded.min_nonzero_altitude IS NULL THEN sightings_hourly.min_nonzero_altitude
                    WHEN sightings_hourly.min_nonzero_altitude IS NULL THEN excluded.min_nonzero_altitude
                    ELSE MIN(sightings_hourly.min_nonzero_altitude, excluded.min_nonzero_altitude)
                END
        """, (
            ac["hex"], hour_bucket, ac["callsign"] or "",
            ac["aircraft_type"] or "", ac["type_desc"] or "",
            now, now,                                    # first_seen_at, last_seen_at
            ac["lat"], ac["lon"], ac["altitude"], ac["speed"],  # last_*
            ac["altitude"], ac["altitude"], ac["speed"],  # min_alt, max_alt, max_speed
            ac["squawk"] or "",
            _alt_nonzero,                                # v2.87.1: min_nonzero_altitude
        ))

        # v2.88.0: per-aircraft per-day session tracking. Reads the
        # existing row, decides whether this poll continues the
        # current session or starts a new one (gap >
        # `_session_gap_min` minutes ends a session), promotes to
        # best-of-day if the in-flight session is now the longest,
        # writes back via INSERT OR REPLACE. This is the read path
        # for the Stats `longest_track` card and its drill panel —
        # the previous all_sightings Python walk read 950K+ rows
        # per Stats render; this rollup turns it into ORDER BY DESC
        # LIMIT 1 over ~50-200 rows/day.
        #
        # Two queries per aircraft per poll (SELECT + INSERT OR
        # REPLACE). Considered a single ON CONFLICT DO UPDATE
        # like sightings_hourly does, but the gap-detection logic
        # would require repeating the same CASE expression four
        # times in the SET clause (current_start, best_start,
        # best_end, best_duration all need the "did we just gap?"
        # decision). Two queries with Python branching is roughly
        # half the line count and substantially more readable —
        # and at 50 aircraft × 20s polls × 2 queries it's ~5
        # queries/sec/process, well below SQLite's noise floor.
        #
        # Day-bucket alignment is local-tz (not UTC like the other
        # rollups) — see migration v6 docstring for the rationale.
        # Sessions split only at local midnight, when traffic is
        # minimal, matching the user's mental model of "today".
        day_bucket = _local_day_bucket(now)
        gap_sec = _session_gap_min * 60
        existing = conn.execute(
            "SELECT current_session_start, current_session_last, "
            "       best_session_start, best_session_end, best_session_duration, "
            "       callsign, aircraft_type "
            "FROM aircraft_track_daily WHERE icao=? AND day_bucket=?",
            (ac["hex"], day_bucket)
        ).fetchone()
        if existing is None or now - existing[1] > gap_sec:
            # New row OR gap detected: start a fresh in-flight session.
            atd_cur_start = now
            atd_cur_last = now
        else:
            # Continue the existing session.
            atd_cur_start = existing[0]
            atd_cur_last = now
        atd_cur_dur = atd_cur_last - atd_cur_start
        if existing is None or atd_cur_dur > existing[4]:
            # First sighting today, or in-flight session has now
            # exceeded the previous best.
            atd_best_start = atd_cur_start
            atd_best_end = atd_cur_last
            atd_best_dur = atd_cur_dur
        else:
            # Earlier session (closed or in-flight) is still longest.
            atd_best_start = existing[2]
            atd_best_end = existing[3]
            atd_best_dur = existing[4]
        # Callsign and aircraft_type: latest non-empty wins, matching
        # the COALESCE-NULLIF pattern in the sightings_hourly UPSERT
        # above. Empty strings (no callsign reported on this poll)
        # preserve whatever was previously stored.
        atd_new_callsign = (ac["callsign"] or "").strip()
        atd_new_type = (ac["aircraft_type"] or "").strip()
        if existing is None:
            atd_callsign_out = atd_new_callsign
            atd_type_out = atd_new_type
        else:
            atd_callsign_out = atd_new_callsign or (existing[5] or "")
            atd_type_out = atd_new_type or (existing[6] or "")
        conn.execute("""
            INSERT OR REPLACE INTO aircraft_track_daily (
                icao, day_bucket, callsign, aircraft_type,
                current_session_start, current_session_last,
                best_session_start, best_session_end, best_session_duration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ac["hex"], day_bucket, atd_callsign_out, atd_type_out,
            atd_cur_start, atd_cur_last,
            atd_best_start, atd_best_end, atd_best_dur,
        ))

        # v2.48.0: emergency-squawk edge detection. We fire an
        # emergency_squawk event when an aircraft's current squawk is
        # 7500/7600/7700 AND the previous observation we have of this
        # ICAO was either absent (fresh appearance) or a DIFFERENT
        # squawk. That way a plane circling in range with a stuck 7700
        # fires once, not every minute. Per-ICAO cooldown in the
        # notifier (default 60min) adds belt-and-suspenders protection.
        #
        # _last_squawk_by_icao is process-local; on restart, ongoing
        # emergency squawkers fire once — which is the correct default
        # (the operator probably wants to know there's an active
        # emergency after a service restart).
        cur_sq = ac["squawk"] or ""
        prev_sq = _last_squawk_by_icao.get(ac["hex"], "")
        if cur_sq in _EMERGENCY_SQUAWK_LABELS and cur_sq != prev_sq:
            label = _EMERGENCY_SQUAWK_LABELS[cur_sq]
            callsign = (ac["callsign"] or "").strip() or ac["hex"]
            type_desc = ac.get("aircraft_type") or "unknown type"
            alt_str = f"{ac['altitude']:,} ft" if ac.get("altitude") else "altitude unknown"
            _safe_notify(
                "emergency_squawk",
                f"Emergency squawk {cur_sq} - {callsign}",
                f"{callsign} ({ac['hex']}) is squawking {cur_sq} ({label}). "
                f"Type: {type_desc}. Altitude: {alt_str}.",
                priority="high",
                tags=["rotating_light"],
                aircraft_icao=ac["hex"],
                click_route="live",
                track_url=_build_track_url(
                    ac["hex"],
                    receiver.get("track_link_provider") or "airplanes_live",
                ),
            )
        # Update the tracker regardless — we want to know what the
        # aircraft was squawking last poll no matter whether it was
        # emergency or normal.
        _last_squawk_by_icao[ac["hex"]] = cur_sq

        # v2.51.0: full UPSERT to maintain denormalized search-feature columns.
        # Replaces the old INSERT OR IGNORE pattern. On insert, populate
        # everything we know about this first sighting. On conflict (row
        # already exists), update last_callsign / aircraft_type / last_lat /
        # last_lon / last_seen_at / sighting_count to reflect this newer
        # sighting. first_seen_at, first_callsign, first_aircraft_type are
        # NEVER touched on conflict — those capture the first-ever sighting
        # by definition and remain immutable.
        #
        # Flavor C: fts_dirty is set to 1 on INSERT (new row needs FTS
        # entry) or whenever an FTS-indexed field changes on UPDATE.
        # Routine sighting_count bumps don't touch fts_dirty. The
        # cycle-end flush below clears these by syncing FTS5.
        from countries import country_for_icao
        from designators import operator_from_callsign
        from categorize import classify as _categorize
        _cs = (ac["callsign"] or "").strip()
        _atype = ac["aircraft_type"] or ""
        _adesc = ac.get("type_desc") or ""
        _country = country_for_icao(ac["hex"])
        # v2.50.42: derive operator from callsign. Returns the 3-letter
        # ICAO airline designator (e.g. "UAL") when the callsign starts
        # with a recognized airline code, else None. Empty string in the
        # column would tokenize into FTS5 as nothing — but None becomes
        # NULL which the FTS5 join handles cleanly.
        _operator = operator_from_callsign(_cs) or ""
        # v2.60.1: compute distance in canonical km from the aircraft's
        # current position to the configured receiver location. None
        # when receiver isn't configured or aircraft has no position.
        # ORDER BY seen_aircraft.last_distance now sorts the full
        # result set on Search.
        _distance_km = _compute_distance_km(ac.get("lat"), ac.get("lon"))
        # v2.89.0: compute is_military and category BEFORE the
        # seen_aircraft UPSERT so the row's category column reflects
        # the same classification the rest of this poll uses (military
        # status drives military_sightings inserts later in the loop;
        # we cache the result here and reuse it). The category
        # heuristics live in categorize.py — single source of truth
        # shared with migration v7's backfill and the Stats
        # category_mix card's read query.
        is_mil, special_label = is_military(raw, config)
        _category = _categorize(_atype, _adesc, is_mil)
        # Sticky-military rule lives in the SQL ON CONFLICT below: if
        # either the new poll's classification OR the existing row's
        # classification is 'military', the result stays 'military'.
        # That means a feeder flicker (dbFlags missing on one poll)
        # can't downgrade a known military aircraft to commercial.
        conn.execute("""
            INSERT INTO seen_aircraft (
                icao, first_seen_at, first_callsign, first_aircraft_type,
                last_callsign, aircraft_type, aircraft_type_desc, operator, country,
                last_lat, last_lon, last_distance, last_seen_at, sighting_count,
                fts_dirty, category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
            ON CONFLICT(icao) DO UPDATE SET
                last_callsign = COALESCE(NULLIF(excluded.last_callsign, ''), last_callsign),
                aircraft_type = COALESCE(NULLIF(excluded.aircraft_type, ''), aircraft_type),
                aircraft_type_desc = COALESCE(NULLIF(excluded.aircraft_type_desc, ''), aircraft_type_desc),
                operator = COALESCE(NULLIF(excluded.operator, ''), operator),
                country = COALESCE(excluded.country, country),
                last_lat = COALESCE(excluded.last_lat, last_lat),
                last_lon = COALESCE(excluded.last_lon, last_lon),
                last_distance = COALESCE(excluded.last_distance, last_distance),
                last_seen_at = excluded.last_seen_at,
                sighting_count = sighting_count + 1,
                category = CASE
                    WHEN excluded.category = 'military' THEN 'military'
                    WHEN seen_aircraft.category = 'military' THEN 'military'
                    ELSE excluded.category
                END,
                fts_dirty = CASE
                    WHEN last_callsign IS NOT COALESCE(NULLIF(excluded.last_callsign, ''), last_callsign) THEN 1
                    WHEN aircraft_type IS NOT COALESCE(NULLIF(excluded.aircraft_type, ''), aircraft_type) THEN 1
                    WHEN aircraft_type_desc IS NOT COALESCE(NULLIF(excluded.aircraft_type_desc, ''), aircraft_type_desc) THEN 1
                    WHEN operator IS NOT COALESCE(NULLIF(excluded.operator, ''), operator) THEN 1
                    WHEN country IS NOT COALESCE(excluded.country, country) THEN 1
                    ELSE fts_dirty
                END
        """, (ac["hex"], now, _cs, _atype,
              _cs, _atype, _adesc, _operator, _country,
              ac.get("lat"), ac.get("lon"), _distance_km, now, _category))

        # Update all-time records (Wave 3). Each check is a single SELECT and
        # an UPDATE only on the rare case of a new record. No noticeable cost.
        # Wrapped in try/except so one weird aircraft can't kill the whole poll
        # — the rest of the data still gets saved.
        #
        # Quality filters (see stats quality filters section at top of file):
        # - Skip TIS-B/MLAT pseudo-targets entirely. Their data is relayed
        #   and often inaccurate — they shouldn't set permanent records.
        # - For fastest_ever, enforce the type-aware speed ceiling to
        #   reject transponder glitches (e.g. a B763 reporting 1010 kt).
        # - Slowest/lowest-altitude intentionally are NOT all-time records
        #   (that direction would just trend toward 1 kt / 1 ft over time
        #   as more ground noise gets caught), so no filter needed there.
        try:
            if is_pseudo_icao(ac.get("hex")):
                pass  # skip all record updates for pseudo-targets
            else:
                if ac["speed"] is not None and ac["speed"] > 0:
                    cap = speed_ceiling_for_type(ac.get("aircraft_type"))
                    if ac["speed"] <= cap:
                        _update_record(conn, "fastest_ever", ac["speed"], "gt", ac, now)
                if ac["altitude"] is not None:
                    _update_record(conn, "highest_altitude_ever", ac["altitude"], "gt", ac, now)
                    if ac["altitude"] > 0:
                        _update_record(conn, "lowest_altitude_ever", ac["altitude"], "lt", ac, now)
                # Furthest — only if we know the receiver's location
                rx = config.get("receiver", {})
                if (ac["lat"] is not None and ac["lon"] is not None
                        and rx.get("latitude") is not None and rx.get("longitude") is not None):
                    unit = (rx.get("distance_unit") or "mi").lower()
                    dist = _haversine(rx["latitude"], rx["longitude"],
                                      ac["lat"], ac["lon"], unit)
                    _update_record(conn, "furthest_ever", dist, "gt", ac, now, extra=unit)
        except Exception as e:
            logger.warning(f"Record update failed for {ac.get('hex', '?')}: {e}")

        # Military check — is_mil and special_label were already
        # computed above for the seen_aircraft category column.
        # Reusing them here avoids a second is_military(raw, config)
        # call per aircraft per poll.
        if is_mil:
            conn.execute("""
                INSERT INTO military_sightings
                (icao, callsign, speed, lat, lon, altitude, aircraft_type, type_desc, seen_at, special_label, squawk)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ac["hex"], ac["callsign"], ac["speed"], ac["lat"], ac["lon"],
                  ac["altitude"], ac["aircraft_type"], ac["type_desc"], now, special_label, ac["squawk"]))
            mil_count += 1
            # v3.4.6: keep military_hourly rollup current. Same shape as
            # the sightings_hourly upsert above. PK (icao, hour_bucket)
            # collapses repeat polls within an hour into one row;
            # special_label / callsign / aircraft_type are preserved
            # via COALESCE so a later non-empty value updates the row
            # but a NULL/empty value doesn't wipe out an earlier
            # populated one.
            conn.execute("""
                INSERT INTO military_hourly (
                    icao, hour_bucket, callsign, aircraft_type, type_desc,
                    special_label, sighting_count, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (icao, hour_bucket) DO UPDATE SET
                    callsign       = COALESCE(NULLIF(excluded.callsign, ''),       military_hourly.callsign),
                    aircraft_type  = COALESCE(NULLIF(excluded.aircraft_type, ''),  military_hourly.aircraft_type),
                    type_desc      = COALESCE(NULLIF(excluded.type_desc, ''),      military_hourly.type_desc),
                    special_label  = COALESCE(NULLIF(excluded.special_label, ''),  military_hourly.special_label),
                    sighting_count = military_hourly.sighting_count + 1,
                    last_seen_at   = excluded.last_seen_at
            """, (
                ac["hex"], hour_bucket, ac["callsign"] or "",
                ac["aircraft_type"] or "", ac["type_desc"] or "",
                special_label or "",
                now, now,
            ))
            # Special aircraft notification. Only fires for aircraft listed in
            # military.special_aircraft (the ones with custom labels like
            # "Air Force 1"), not every generic military contact. Cooldown per
            # ICAO suppresses repeat alerts while the aircraft stays in range.
            if special_label:
                _safe_notify(
                    "special_aircraft",
                    f"Special aircraft: {special_label}",
                    f"{special_label} ({ac['callsign'] or ac['hex']}) "
                    f"is visible to your receiver. "
                    f"Type: {ac.get('aircraft_type') or 'unknown'}. "
                    f"Altitude: {ac.get('altitude') or '?'} ft.",
                    priority="high",
                    tags=["airplane"],
                    aircraft_icao=ac["hex"],
                    click_route="military",
                    track_url=_build_track_url(
                        ac["hex"],
                        receiver.get("track_link_provider") or "airplanes_live",
                    ),
                )

        # Watchlist check
        watch_label = match_watchlist(raw, watchlist_lookup)
        if watch_label:
            conn.execute("""
                INSERT INTO watchlist_sightings
                (icao, callsign, speed, lat, lon, altitude, aircraft_type, type_desc, seen_at, watchlist_label, squawk)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ac["hex"], ac["callsign"], ac["speed"], ac["lat"], ac["lon"],
                  ac["altitude"], ac["aircraft_type"], ac["type_desc"], now, watch_label, ac["squawk"]))
            watch_count += 1
            # v3.4.6: keep watchlist_hourly rollup current. PK is THREE-way
            # (icao, hour_bucket, watchlist_label) because we need to
            # COUNT(DISTINCT watchlist_label) from the rollup for the
            # watchlist_rules_hit Stats card. Same callsign/type
            # COALESCE preservation as the military path.
            conn.execute("""
                INSERT INTO watchlist_hourly (
                    icao, hour_bucket, watchlist_label, callsign,
                    aircraft_type, type_desc, sighting_count,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (icao, hour_bucket, watchlist_label) DO UPDATE SET
                    callsign       = COALESCE(NULLIF(excluded.callsign, ''),       watchlist_hourly.callsign),
                    aircraft_type  = COALESCE(NULLIF(excluded.aircraft_type, ''),  watchlist_hourly.aircraft_type),
                    type_desc      = COALESCE(NULLIF(excluded.type_desc, ''),      watchlist_hourly.type_desc),
                    sighting_count = watchlist_hourly.sighting_count + 1,
                    last_seen_at   = excluded.last_seen_at
            """, (
                ac["hex"], hour_bucket, watch_label,
                ac["callsign"] or "",
                ac["aircraft_type"] or "", ac["type_desc"] or "",
                now, now,
            ))
            # Watchlist hit notification. Cooldown per ICAO (default 10 min)
            # prevents a plane circling in range from firing every poll.
            _safe_notify(
                "watchlist_hit",
                f"Watchlist: {watch_label}",
                f"{watch_label} ({ac['callsign'] or ac['hex']}) "
                f"is visible. Type: {ac.get('aircraft_type') or 'unknown'}. "
                f"Altitude: {ac.get('altitude') or '?'} ft.",
                priority="default",
                tags=["eye"],
                aircraft_icao=ac["hex"],
                click_route="watchlist",
                track_url=_build_track_url(
                    ac["hex"],
                    receiver.get("track_link_provider") or "airplanes_live",
                ),
            )

    # Peak simultaneous record (Wave 3) — check once per poll
    if all_count > 0:
        # No specific aircraft "wins" this; we use the count as the value and
        # leave icao/callsign blank. The callsign field gets a descriptive label.
        sentinel_ac = {"hex": "", "callsign": f"{all_count} aircraft", "aircraft_type": ""}
        _update_record(conn, "peak_simultaneous_ever", all_count, "gt",
                       sentinel_ac, now)

    # v2.87.0: per-minute concurrent rollup. One row per 60-second
    # bucket; on conflict we keep the larger count via CASE WHEN, so
    # if poll cadence is sub-60s we record the peak instant within
    # the minute rather than just the last sub-poll's count. This is
    # the same data the Stats peak_simultaneous and average_concurrent
    # cards used to derive by GROUP BY-ing all_sightings every render
    # — moving it to a precomputed rollup makes those queries
    # near-instant. all_count is the number of distinct aircraft we
    # processed in this poll (one row per ICAO inserted into
    # all_sightings above).
    if all_count > 0:
        minute_bucket = (now // 60) * 60
        conn.execute("""
            INSERT INTO concurrent_minute(minute_bucket, count)
            VALUES (?, ?)
            ON CONFLICT(minute_bucket) DO UPDATE SET
                count = CASE
                    WHEN excluded.count > count THEN excluded.count
                    ELSE count
                END
        """, (minute_bucket, all_count))

    # v2.48.0: prune the per-ICAO squawk tracker to what's currently
    # visible. Stops it growing without bound across long-running
    # installs. An aircraft that drops off then reappears with an
    # emergency code will fire a fresh notification — which is the
    # right behavior (we treat "gone for a poll" the same as "new").
    visible_hexes = {
        (raw.get("hex") or "").lower()
        for raw in raw_list
        if isinstance(raw, dict) and raw.get("hex")
    }
    for icao in list(_last_squawk_by_icao.keys()):
        if icao.lower() not in visible_hexes:
            del _last_squawk_by_icao[icao]

    # v2.51.0 Flavor C: cycle-end FTS5 flush. The UPSERT loop above
    # marked rows fts_dirty=1 whenever an FTS-indexed field changed
    # (callsign, type, country, etc.). Steady-state sightings — same
    # aircraft seen again with same callsign — leave fts_dirty unchanged
    # so this flush is bounded to genuinely-changed rows.
    #
    # The protocol:
    #   1. Delete any FTS5 rows for currently-dirty seen_aircraft rows.
    #      Idempotent — harmless if FTS row didn't exist (e.g. brand-new
    #      ICAO not yet in FTS).
    #   2. Insert fresh FTS5 rows from the dirty seen_aircraft rows.
    #   3. Clear the fts_dirty flag.
    # All inside the surrounding transaction so a crash leaves dirty
    # rows still flagged for retry on the next cycle.
    _flush_dirty_to_fts(conn)

    conn.commit()
    conn.close()

    logger.info(f"Stored {all_count} total, {mil_count} military, {watch_count} watchlist")

    # Cleanup
    cleanup_old_data(db_path, config["retention"])
