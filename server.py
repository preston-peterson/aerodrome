# Version: 3.0.9
"""
server.py — Web server and API for the ADS-B tracker.

Endpoints:
  GET  /                        Web UI
  GET  /api/live                Live aircraft from receiver (no DB)
  GET  /api/military            Military sightings (retention-limited)
  GET  /api/watchlist           Watchlist sightings (retention-limited)
  GET  /api/all/drill           Per-aircraft sighting history (used by detail page)
  GET  /api/search              Full-text search across every aircraft
  GET  /api/watchlist/entries   Current watchlist config
  POST /api/watchlist/add       Add to watchlist
  POST /api/watchlist/remove    Remove from watchlist
  GET  /api/status              System status
"""

import logging
import math
import re
import shutil
import sqlite3
import time
import os
from pathlib import Path
from typing import Optional, Dict, List, Any

import requests as req
import yaml
from fastapi import FastAPI, Query, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

# Local — ICAO code → friendly display name lookups for the Stats cards.
from designators import airline_name, aircraft_type_name
# v2.50.13: central DB-connection helper that applies tuning pragmas
from collector import _open_db_conn
# v2.79.0 (Phase 3 polish): centralized distance + bearing helpers.
# Pre-v2.79.0 the haversine math was duplicated across four sites
# (this file's _haversine, the stats endpoint local def, the records
# endpoint local def, and collector.py). distance.py is the single
# source of truth now. Numerical output matches the legacy inline
# definitions to display precision (1 decimal); see distance.py's
# module docstring for the conversion-factor note.
from distance import haversine as _dist_haversine, to_user_unit as _dist_to_user_unit, compass_bearing as _dist_compass_bearing

logger = logging.getLogger("adsb.server")

CONFIG = {}
CONFIG_PATH = ""

# Notifier singleton. Constructed in get_app() from the initial config and
# handed to the collector via collector.set_notifier(). Also kept at module
# level so API endpoints (test, recent, etc.) can reach it.
_NOTIFIER = None

# v2.57.1: tail → ICAO resolution cache for watchlist entries that
# specify only `tail:` (not `icao:`). Resolved at server startup and on
# config reload by reverse-querying the hexdb_cache table — that table
# is maintained by the collector as it observes aircraft, so tail
# resolution here is a local SQL lookup rather than a network call.
#
# Map shape: { "N12345": "A12345", ... }  (uppercase tail → uppercase ICAO)
#
# Tails that hexdb_cache doesn't contain (aircraft never seen by this
# install) are logged as warnings and omitted from the map. The
# watchlist filter / pill annotation paths read this map alongside the
# raw config, so resolved tails behave identically to icao-direct
# entries from the user's perspective.
_RESOLVED_WATCHLIST_TAILS: Dict[str, str] = {}


def _resolve_watchlist_tails(config: dict, db_path: str) -> Dict[str, str]:
    """v2.57.1: resolve any tail-only watchlist entries to ICAOs by
    querying hexdb_cache (the collector's local registration cache).

    Returns a mapping {tail_upper: icao_upper}. Entries whose tail
    isn't in hexdb_cache are logged as warnings and omitted —
    typically that means the aircraft has never been seen on this
    install, so it wouldn't appear in search results either way.

    Called at server startup and on every config reload. Cheap: one
    SELECT per tail-only entry, indexed lookup against
    hexdb_cache.registration. With 10s of watchlist entries (typical),
    total cost is <1ms. Network-free."""
    resolved: Dict[str, str] = {}
    watchlist = config.get("watchlist") or []
    if not watchlist:
        return resolved

    # Collect the tails we need to resolve — entries with `tail:` set
    # but no `icao:` (entries with both already work without
    # resolution).
    tails_to_resolve: List[str] = []
    for entry in watchlist:
        if not isinstance(entry, dict):
            continue
        if entry.get("icao"):
            continue  # already has an ICAO, no resolution needed
        tail = entry.get("tail")
        if tail:
            tails_to_resolve.append(str(tail).strip().upper())

    if not tails_to_resolve:
        return resolved

    try:
        conn = sqlite3.connect(db_path)
        try:
            for tail in tails_to_resolve:
                # Reverse lookup: registration → icao. UPPER on both
                # sides because hexdb_cache stores registrations in the
                # form aircraft broadcast them (often uppercase but not
                # guaranteed), and the user's config tail might be
                # mixed case.
                row = conn.execute(
                    "SELECT icao FROM hexdb_cache "
                    "WHERE UPPER(registration) = UPPER(?) "
                    "  AND last_outcome = 'positive' "
                    "LIMIT 1",
                    (tail,),
                ).fetchone()
                if row and row[0]:
                    resolved[tail] = row[0].upper()
                    logger.info(
                        f"Watchlist tail-resolution: {tail} → {row[0].upper()} "
                        f"(via hexdb_cache)"
                    )
                else:
                    # Same warning shape the collector logs at startup
                    # when resolve_tail_to_icao misses. Honest: we
                    # can't search-filter or pill-annotate this entry.
                    logger.warning(
                        f"Watchlist tail-resolution: could not resolve '{tail}' "
                        f"via hexdb_cache — aircraft may not have been seen "
                        f"yet on this install. Add the ICAO directly to the "
                        f"watchlist entry to enable search filtering."
                    )
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        # hexdb_cache table doesn't exist (very fresh install before
        # collector has run). Same outcome as no resolutions — log
        # once at info level rather than warning per tail.
        logger.info(
            f"Watchlist tail-resolution: hexdb_cache not available ({e}); "
            f"tail-only entries will not be search-filterable until the "
            f"collector populates the cache."
        )

    return resolved



def _build_notifier():
    """Construct a Notifier from the current CONFIG. Returns the notifier
    instance. Idempotent wrt state (reinvoking just rebuilds config)."""
    from notifier import Notifier
    nt_cfg = CONFIG.get("notifications") or {}
    stats_tz = (CONFIG.get("stats") or {}).get("timezone") or None
    return Notifier(nt_cfg, stats_timezone=stats_tz)


def _refresh_notifier_config():
    """Apply the current CONFIG to the existing notifier. Called after
    a PUT /api/config that touched the notifications block."""
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = _build_notifier()
    else:
        nt_cfg = CONFIG.get("notifications") or {}
        stats_tz = (CONFIG.get("stats") or {}).get("timezone") or None
        _NOTIFIER.update_config(nt_cfg, stats_timezone=stats_tz)
    # Always keep collector pointed at the current notifier. Cheap, idempotent.
    try:
        import collector as _collector_mod
        _collector_mod.set_notifier(_NOTIFIER)
        # v2.49.0: make the DB path available for the resolver's persistent
        # cache. Same rationale as set_notifier — idempotent, cheap, safe to
        # call on every config reload. Only needed once at startup in
        # practice but calling on reload keeps the path in sync if data.db_file
        # ever changes.
        _collector_mod.set_db_path(CONFIG["data"]["db_file"])
        # v2.50.13: propagate the SQLite tuning profile so subsequent
        # _open_db_conn() calls apply the right pragmas. Same idempotent-
        # cheap-call-on-every-reload pattern; takes effect for new
        # connections only, so live reloads don't retune existing ones.
        tuning_cfg = (CONFIG.get("data") or {}).get("tuning") or {}
        _collector_mod.set_db_tuning_profile(tuning_cfg.get("profile") or "auto")
        # v2.88.0: push the timezone + track_gap_minutes values the
        # collector needs to maintain aircraft_track_daily on every
        # poll. Same idempotent-cheap-call-on-every-reload pattern as
        # the tuning profile above. The setter caches the parsed
        # ZoneInfo object, so per-poll calls don't re-parse.
        _stats_cfg = CONFIG.get("stats") or {}
        _collector_mod.set_session_track_config(
            _stats_cfg.get("timezone"),
            _stats_cfg.get("track_gap_minutes"),
        )
    except Exception as e:
        logger.warning(f"Failed to wire notifier into collector: {e}")


# --- Pre-restore safety-snapshot retention (v2.50.6) ---
# The in-app Restore flow at POST /api/backup/import copies the live
# config.yaml and aircraft_history.db aside with a `.pre-restore` suffix
# before overwriting them — a one-click undo for "I just clobbered my data".
# Without a retention policy these accumulated forever; on installs that
# restore frequently (and especially on installs whose live DB is large),
# the snapshots quietly consumed gigabytes. Cap at the most recent N pairs;
# prune at the end of every successful restore AND at service startup so
# accumulated cruft from earlier versions self-heals on the next boot.
_PRE_RESTORE_KEEP = 3
_PRE_RESTORE_TS_RE = re.compile(r"\.(\d{8}-\d{6})\.pre-restore$")


def _pre_restore_install_dir() -> Path:
    return Path(__file__).parent


def _pre_restore_db_name() -> str:
    """Resolve the live DB file's basename for glob matching. Falls back
    to the default if CONFIG isn't loaded yet (e.g. early in startup)."""
    db_file = (CONFIG.get("data") or {}).get("db_file", "aircraft_history.db")
    return Path(db_file).name


def _pre_restore_globs() -> List[str]:
    """Glob patterns covering both flavors of pre-restore snapshot."""
    return [
        f"{_pre_restore_db_name()}.bak.*.pre-restore",
        "config.yaml.bak.*.pre-restore",
    ]


def _list_pre_restore_snapshots() -> List[Dict[str, Any]]:
    """Return pre-restore snapshots paired by timestamp, newest first.
    Each entry: {timestamp, files: [{name, kind, size_bytes, mtime}, ...],
    total_bytes, mtime}."""
    install_dir = _pre_restore_install_dir()
    db_name = _pre_restore_db_name()

    by_ts: Dict[str, Dict[str, Any]] = {}
    for pat in _pre_restore_globs():
        for p in install_dir.glob(pat):
            if not p.is_file():
                continue
            m = _PRE_RESTORE_TS_RE.search(p.name)
            if not m:
                continue
            ts = m.group(1)
            try:
                st = p.stat()
            except OSError:
                continue
            kind = "database" if p.name.startswith(db_name + ".bak.") else "config"
            entry = by_ts.setdefault(ts, {
                "timestamp": ts,
                "files": [],
                "total_bytes": 0,
                "mtime": 0,
            })
            entry["files"].append({
                "name": p.name,
                "kind": kind,
                "size_bytes": st.st_size,
                "mtime": int(st.st_mtime),
            })
            entry["total_bytes"] += st.st_size
            entry["mtime"] = max(entry["mtime"], int(st.st_mtime))

    snapshots = list(by_ts.values())
    # Newest first. Timestamp strings are YYYYMMDD-HHMMSS so lexical sort
    # is chronological.
    snapshots.sort(key=lambda s: s["timestamp"], reverse=True)
    return snapshots


def _prune_pre_restore_snapshots(keep: int = _PRE_RESTORE_KEEP):
    """Delete pre-restore files beyond the most-recent `keep` per glob.
    The two patterns (DB and config) are pruned independently, so a
    missing or mismatched pair (e.g. a config that was written when the
    DB write failed, or vice versa) doesn't block trimming the other.

    Returns (deleted_count, freed_bytes). Errors on individual files are
    logged but never raised — pruning is best-effort housekeeping."""
    install_dir = _pre_restore_install_dir()
    deleted_count = 0
    freed_bytes = 0
    for pat in _pre_restore_globs():
        files = []
        for p in install_dir.glob(pat):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((p, st.st_size))
        # Newest first by name (timestamps sort chronologically)
        files.sort(key=lambda t: t[0].name, reverse=True)
        for p, size in files[keep:]:
            try:
                p.unlink()
                deleted_count += 1
                freed_bytes += size
                logger.info(f"Pruned old pre-restore snapshot: {p.name}")
            except OSError as e:
                logger.warning(f"Could not delete pre-restore {p.name}: {e}")
    return deleted_count, freed_bytes


def _purge_all_pre_restore_snapshots():
    """Delete every pre-restore snapshot. Used by the manual purge endpoint
    when the keep-N retention is more conservative than the user wants.

    Returns (deleted_count, freed_bytes)."""
    return _prune_pre_restore_snapshots(keep=0)


# --- Install-snapshot bloat policy (v2.50.8) ---
# `apply_local_update` makes a code-rollback snapshot of the install dir at
# .backups/<timestamp>/ before each in-app update. Pre-v2.50.8 the loop only
# filtered by exact-name `PRESERVE_PATHS`, so anything with a *suffix* —
# .pre-restore safety snapshots, .bak.* config/db backups, ntfy
# .from-backup.* stash files — fell through and got copied verbatim. On
# installs that had .pre-restore files in the root at update time this
# meant multi-GB embedded user-data copies inside every code snapshot.
# This helper centralises the suffix patterns we don't want in snapshots,
# used both by the create-snapshot loop AND by the startup heal that
# strips the same patterns from historical snapshots.
def _should_skip_in_install_snapshot(name: str) -> bool:
    """True if a path NAME (basename, not full path) is user-data or
    transient cruft that should never be inside .backups/<timestamp>/."""
    if name == "__pycache__":
        return True
    if name.endswith(".pyc"):
        return True
    if name.endswith(".pre-restore"):
        return True
    if ".bak." in name:
        return True
    if ".from-backup." in name:
        return True
    return False


def _heal_install_snapshots():
    """One-shot cleanup of historical .backups/<timestamp>/ directories.
    Strips files and __pycache__ subdirectories matching the bloat patterns
    in `_should_skip_in_install_snapshot`. The snapshot folders themselves
    stay in place — `_prune_install_backups` continues to manage their
    keep-N retention, and code rollback to that snapshot's source state
    still works for the actual code files.

    Best-effort: per-path errors are logged but never raised.

    Returns (deleted_count, freed_bytes)."""
    install_dir = _pre_restore_install_dir()
    backups_root = install_dir / ".backups"
    if not backups_root.is_dir():
        return 0, 0

    snapshot_re = re.compile(r"^\d{8}-\d{6}$")
    deleted_count = 0
    freed_bytes = 0

    for snap in backups_root.iterdir():
        if not snap.is_dir() or not snapshot_re.match(snap.name):
            continue
        # rglob enumerates descendants only — the snap dir itself is never
        # a candidate for deletion. We materialize the list because we'll
        # be removing entries during iteration (a __pycache__ rmtree may
        # invalidate paths the iterator already produced from inside it).
        for path in list(snap.rglob("*")):
            if not path.exists():
                # Already cleaned up as part of a parent rmtree above.
                continue
            if not _should_skip_in_install_snapshot(path.name):
                continue
            try:
                if path.is_dir():
                    sz = sum(p.stat().st_size for p in path.rglob("*")
                             if p.is_file())
                    shutil.rmtree(path)
                    deleted_count += 1
                    freed_bytes += sz
                else:
                    sz = path.stat().st_size
                    path.unlink()
                    deleted_count += 1
                    freed_bytes += sz
            except OSError as e:
                logger.warning(f"Could not strip {path} from snapshot: {e}")

    return deleted_count, freed_bytes


# --- Config-only auto-backup retention (v2.50.9) ---
# `_apply_config_from_text` writes a snapshot of the previous config to
# `config.yaml.bak.YYYYMMDD-HHMMSS` every time the config is saved (whether
# from the UI's editor, an upload, or a schema migration). The Backup &
# Restore UI has always claimed "Aerodrome keeps the 5 most recent
# auto-backups" — but no code enforced it, so on installs that edited the
# config often the file count grew without bound. This is the same pattern
# v2.50.6 added for `.pre-restore` snapshots, applied to the plain
# auto-backup series (which is filtered out of the pre-restore section
# now, see _list_config_backups).
_CONFIG_AUTO_BACKUP_KEEP = 5
_CONFIG_AUTO_BACKUP_RE = re.compile(
    r"^config\.yaml\.bak\.\d{8}-\d{6}$"
)


def _list_config_auto_backups():
    """Return plain config.yaml.bak.YYYYMMDD-HHMMSS files (no .pre-restore
    suffix and no other trailing junk), newest first. Each entry is
    (path, size_bytes, mtime). The strict regex is what keeps this
    helper from accidentally pruning files that share a similar prefix
    but were written by some other tool."""
    install_dir = _pre_restore_install_dir()
    files = []
    for p in install_dir.glob("config.yaml.bak.*"):
        if not p.is_file():
            continue
        if not _CONFIG_AUTO_BACKUP_RE.match(p.name):
            continue  # skip .pre-restore and any future variants
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((p, st.st_size, st.st_mtime))
    # Newest first by name (timestamp string sorts chronologically)
    files.sort(key=lambda t: t[0].name, reverse=True)
    return files


def _prune_config_auto_backups(keep: int = _CONFIG_AUTO_BACKUP_KEEP):
    """Delete plain config auto-backups beyond the most-recent `keep`.
    Returns (deleted_count, freed_bytes). Best-effort: per-file errors
    are logged but never raised."""
    files = _list_config_auto_backups()
    deleted = 0
    freed = 0
    for p, size, _mtime in files[keep:]:
        try:
            p.unlink()
            deleted += 1
            freed += size
            logger.info(f"Pruned old config auto-backup: {p.name}")
        except OSError as e:
            logger.warning(f"Could not delete config auto-backup {p.name}: {e}")
    return deleted, freed


def _normalize_squawk_str(raw) -> str:
    """Normalize a raw squawk value from the receiver into a 4-digit
    string (or empty string if missing). Matches the logic in
    collector._normalize_squawk so stored and live squawks have the
    same shape on the frontend.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    try:
        return s.zfill(4)[-4:] if s.isdigit() else s[:4]
    except Exception:
        return ""


# Emergency squawk codes per ICAO convention. Used by the frontend chip
# renderer. Single source of truth for code → label.
EMERGENCY_SQUAWKS = {
    "7500": "HIJACK",
    "7600": "RADIO FAIL",
    "7700": "GENERAL",
}


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in kilometers.

    v2.79.0: thin wrapper around distance.haversine() — preserved as a
    private name in this module because two existing call sites (line 484,
    568) reference it directly. distance.haversine() is the canonical
    home; this delegate keeps the call-site signature stable.
    """
    return _dist_haversine(lat1, lon1, lat2, lon2, unit="km")


def _distance_from_receiver(ac_lat, ac_lon):
    """Compute distance from configured receiver location to aircraft position.
    Returns distance in the configured unit, or None if unavailable."""
    r = CONFIG.get("receiver", {})
    rlat = r.get("latitude")
    rlon = r.get("longitude")
    if rlat is None or rlon is None or ac_lat is None or ac_lon is None:
        return None
    km = _haversine(rlat, rlon, ac_lat, ac_lon)
    if km is None:
        return None
    unit = (r.get("distance_unit") or "mi").lower()
    return _dist_to_user_unit(km, unit)


# v2.60.1 (Phase 1A.5 perf): convert a stored last_distance value
# (canonical km) to the user's configured display unit. Mirrors the
# unit-conversion tail of _distance_from_receiver but takes the km
# value directly rather than recomputing from coords. Used where the
# DB returns the stored column value and the API response wants
# user-unit.
# v2.79.0: thin wrapper around distance.to_user_unit() — kept as a
# named symbol in this module for the existing call sites that import
# it.
def _distance_km_to_user_unit(km):
    if km is None:
        return None
    r = CONFIG.get("receiver", {})
    unit = (r.get("distance_unit") or "mi").lower()
    return _dist_to_user_unit(km, unit)


# v2.60.1: recompute seen_aircraft.last_distance for all rows using
# the supplied receiver location. Called from main.py startup (after
# migrations + CONFIG load) and from the receiver-location-change
# config save handler. Stores km always (canonical unit); display
# conversion happens at response-annotation time.
#
# Receiver lat/lon are PASSED IN rather than read from CONFIG so this
# function works regardless of whether the server's CONFIG global is
# populated. Callers pass None/None when the receiver isn't
# configured — function clears all last_distance to NULL in that case.
#
# Side-effects: writes to seen_aircraft.last_distance for every row
# where last_lat / last_lon are non-NULL. Rows without coords keep
# last_distance = NULL (ORDER BY puts those last regardless of dir).
#
# Cost: ~6,800 haversines + 6,800 UPDATEs on a typical install. On a
# Pi this completes in single-digit seconds; not parallelized
# because SQLite serializes writes anyway. The function is
# intentionally synchronous so callers know the column is consistent
# when it returns.
#
# Returns the count of rows updated for logging.
def _recompute_all_last_distance(db_path: str,
                                  rlat: Optional[float] = None,
                                  rlon: Optional[float] = None) -> int:
    # Backwards-compat: when called without explicit coords, fall back
    # to CONFIG (caller is somewhere in the request-handling path
    # where CONFIG is established). Explicit args take precedence.
    if rlat is None and rlon is None:
        r = CONFIG.get("receiver", {})
        rlat = r.get("latitude")
        rlon = r.get("longitude")

    conn = sqlite3.connect(db_path)
    try:
        # No receiver location configured → set every last_distance to
        # NULL. This is the right behavior on a fresh install before
        # the user has filled in coordinates: the column is honest
        # ("we don't know") rather than misleading (0 km would imply
        # "everything is here").
        if rlat is None or rlon is None:
            conn.execute("UPDATE seen_aircraft SET last_distance = NULL")
            conn.commit()
            logger.info("Distance recompute: receiver location not set; "
                        "cleared all last_distance values")
            return 0

        # Pull every row with coordinates and recompute.
        rows = conn.execute("""
            SELECT icao, last_lat, last_lon FROM seen_aircraft
            WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
        """).fetchall()

        updates = []
        for icao, lat, lon in rows:
            km = _haversine(rlat, rlon, lat, lon)
            updates.append((km, icao))

        if updates:
            conn.executemany(
                "UPDATE seen_aircraft SET last_distance = ? WHERE icao = ?",
                updates
            )
        # Also clear any rows without coords (idempotent — they were
        # likely NULL already, but explicit is safer than implicit if
        # a row was ever populated by an older code path).
        conn.execute("""
            UPDATE seen_aircraft SET last_distance = NULL
            WHERE last_lat IS NULL OR last_lon IS NULL
        """)
        conn.commit()
        logger.info(f"Distance recompute: updated {len(updates)} rows "
                    f"with distance from receiver ({rlat}, {rlon})")
        return len(updates)
    finally:
        conn.close()


def _annotate_military(entry: dict):
    """Add is_military / mil_label / mil_color fields to a DB-sourced row dict.

    Unlike the live check, this can't see the original dbFlags (it's not stored),
    so it falls back to: special_aircraft explicit list, callsign prefix match,
    ICAO prefix match, or — if the row was written to military_sightings in the
    past — the special_label field (present only on Military tab rows)."""
    mil_cfg = CONFIG.get("military", {})
    specials = {k.upper(): v for k, v in (mil_cfg.get("special_aircraft") or {}).items()}
    default_color = mil_cfg.get("default_color", "#ef4444")

    icao = (entry.get("icao") or "").upper()
    callsign = (entry.get("callsign") or "").upper()

    is_mil = False
    mil_label = None
    mil_color = None

    # 1) Explicit special_aircraft match — highest priority
    if icao and icao in specials:
        spec = specials[icao]
        is_mil = True
        mil_label = spec.get("label") or "MIL"
        mil_color = spec.get("color") or default_color
    else:
        # 2) ICAO prefix match
        for prefix in (mil_cfg.get("icao_prefixes") or []):
            if icao and icao.startswith(prefix.upper()):
                is_mil = True
                break
        # 3) Callsign prefix match
        if not is_mil:
            for prefix in (mil_cfg.get("callsign_prefixes") or []):
                if callsign and callsign.startswith(prefix.upper()):
                    is_mil = True
                    break
        # 4) If this row came from military_sightings, the special_label column
        #    tells us it was flagged as military when captured, even if current
        #    rules wouldn't catch it (e.g., dbFlags bit)
        if not is_mil and entry.get("special_label") is not None:
            is_mil = True
            sl = entry.get("special_label")
            if sl:
                mil_label = sl

        if is_mil and mil_color is None:
            mil_color = default_color
            mil_label = mil_label or "MIL"

    entry["is_military"] = is_mil
    entry["mil_label"] = mil_label
    entry["mil_color"] = mil_color
    return entry


def _annotate_watchlist(entry: dict):
    """v2.57.0: add is_watchlist / watchlist_label fields to a DB-sourced
    row dict.

    Mirrors the matching logic in collector.match_watchlist across all
    four entry kinds: icao (exact), tail (resolved via the
    _RESOLVED_WATCHLIST_TAILS map), callsign prefix, and model
    substring.

    v2.57.1: tail-only entries now match via the resolved-tails map
    populated at server startup from hexdb_cache. An entry like
    `{tail: "N12345", label: "My plane"}` now annotates is_watchlist
    on the corresponding ICAO row, exactly the same as if the user
    had specified `{icao: "A12345"}` directly. If hexdb_cache didn't
    contain the tail at startup (logged as a warning), tail entries
    won't match — same outcome as v2.57.0 for that edge case.

    Reads CONFIG['watchlist'] (the list of entry dicts as the user
    configured them). Stops at the first match and returns the
    entry's label, matching the collector's first-match-wins
    behavior."""
    icao = (entry.get("icao") or "").upper()
    callsign = (entry.get("callsign") or "").upper()
    ac_type = (entry.get("aircraft_type") or "").lower()
    type_desc = (entry.get("aircraft_type_desc") or entry.get("type_desc") or "").lower()

    is_wl = False
    label = None

    for wl_entry in (CONFIG.get("watchlist") or []):
        if not isinstance(wl_entry, dict):
            continue
        wl_label = wl_entry.get("label", "Watched")

        if wl_entry.get("icao"):
            wl_icao = str(wl_entry["icao"]).strip().upper()
            if icao == wl_icao:
                is_wl = True; label = wl_label; break
        elif wl_entry.get("tail"):
            # v2.57.1: tail entries match via the resolved-tails map.
            # _RESOLVED_WATCHLIST_TAILS maps uppercase tail → ICAO.
            tail = str(wl_entry["tail"]).strip().upper()
            resolved_icao = _RESOLVED_WATCHLIST_TAILS.get(tail)
            if resolved_icao and icao == resolved_icao:
                is_wl = True; label = wl_label; break
        elif wl_entry.get("callsign"):
            prefix = str(wl_entry["callsign"]).strip().upper()
            if callsign and callsign.startswith(prefix):
                is_wl = True; label = wl_label; break
        elif wl_entry.get("model"):
            sub = str(wl_entry["model"]).strip().lower()
            if sub and (sub in ac_type or sub in type_desc):
                is_wl = True; label = wl_label; break

    entry["is_watchlist"] = is_wl
    entry["watchlist_label"] = label
    return entry


# =============================================================================
# Capacity metrics (v2.50.30, refactored to capacity.py in v2.50.31)
# =============================================================================
# The real implementation lives in capacity.py — both server.py and
# collector.py need it (server for /api/status + /api/capacity, collector
# for the alert evaluation that runs in the poll loop). Importing here
# keeps the public API at module level for backward compat with any
# existing references; capacity.py is the single source of truth.

from capacity import _compute_capacity_metrics, CAPACITY_DEFAULT_BYTES_PER_ROW


# Stats filtering constants are defined in collector.py so both the
# collector's real-time record updates and the server's today-extremes
# queries share one source of truth. Imported lazily inside handlers
# rather than at module top to avoid tightening our coupling to the
# collector module's import order.


# =============================================================================
# Daily summary (v2.41.35)
# =============================================================================
# A rolling-24h digest sent once per day at a user-configured time. The
# summary covers the 24h window ending at the send moment — NOT a calendar
# day. This is intentional: a user setting "send me the digest at 08:00"
# most naturally wants "what happened in the last day," not "what happened
# from 00:00 yesterday to 00:00 today."
#
# Content is "rich auto-trimming" per user choice — top-line totals
# always present, peak-moment if there were aircraft, military breakdown
# if any military were seen, new records if any were set during the
# window, named specials if any were spotted. Sections empty of content
# are omitted from the output rather than showing "0" or "none".

def compose_daily_summary_data(db_path: str, config: dict,
                                window_hours: int = 24,
                                now_ts: Optional[float] = None) -> dict:
    """Gather the raw data for a daily summary. Does NOT format — that's
    notifier.py's job. Returns a dict with every section pre-computed so
    the composer can cheaply decide what to include.

    db_path: path to the SQLite database
    config: the full CONFIG dict (for receiver location, military labels)
    window_hours: size of the window ending at now_ts (default 24)
    now_ts: timestamp to use as the window's end (default: time.time())
            — accepting an override makes unit testing possible
    """
    import time as _t
    if now_ts is None:
        now_ts = _t.time()
    end_ts = int(now_ts)
    start_ts = end_ts - (window_hours * 3600)

    result = {
        "window_start_ts": start_ts,
        "window_end_ts":   end_ts,
        "window_hours":    window_hours,
        "unique_aircraft": 0,
        "total_sightings": 0,
        "military_count":  0,
        "military_breakdown": [],   # [(label_or_type, count), ...] sorted desc
        "watchlist_count": 0,
        "watchlist_rules_hit": 0,
        "peak": None,               # {"count": int, "at_ts": int}
        "new_records":     [],      # records with set_at in the window
        "specials":        [],      # distinct specials seen in the window
    }

    # Single connection for the whole gather. read_uncommitted because
    # we're fine reading an in-progress poll — the summary will be
    # approximate by a few seconds and that's acceptable.
    try:
        conn = _open_db_conn(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as e:
        logger.warning(f"daily summary: could not open db: {e}")
        return result

    try:
        c = conn.cursor()

        # Volume
        r = c.execute(
            "SELECT COUNT(DISTINCT icao) AS n FROM all_sightings "
            "WHERE seen_at >= ? AND seen_at <= ?",
            (start_ts, end_ts)
        ).fetchone()
        result["unique_aircraft"] = r["n"] if r else 0

        r = c.execute(
            "SELECT COUNT(*) AS n FROM all_sightings "
            "WHERE seen_at >= ? AND seen_at <= ?",
            (start_ts, end_ts)
        ).fetchone()
        result["total_sightings"] = r["n"] if r else 0

        # Peak simultaneous — 60s buckets, same as the Stats card
        r = c.execute("""
            SELECT cnt, bucket FROM (
                SELECT COUNT(DISTINCT icao) AS cnt, (seen_at / 60) AS bucket
                FROM all_sightings
                WHERE seen_at >= ? AND seen_at <= ?
                GROUP BY bucket
                ORDER BY cnt DESC
                LIMIT 1
            )
        """, (start_ts, end_ts)).fetchone()
        if r and (r["cnt"] or 0) > 0:
            result["peak"] = {
                "count":  r["cnt"],
                "at_ts":  int(r["bucket"]) * 60,  # back to unix seconds
            }

        # Military totals
        try:
            r = c.execute(
                "SELECT COUNT(DISTINCT icao) AS n FROM military_sightings "
                "WHERE seen_at >= ? AND seen_at <= ?",
                (start_ts, end_ts)
            ).fetchone()
            result["military_count"] = r["n"] if r else 0

            # Breakdown — group by aircraft_type (fall back to special_label
            # if type is blank, since specials often have sparse type info)
            rows = c.execute("""
                SELECT
                    CASE
                        WHEN special_label IS NOT NULL AND special_label != ''
                            THEN special_label
                        WHEN aircraft_type IS NOT NULL AND aircraft_type != ''
                            THEN aircraft_type
                        ELSE '(unknown)'
                    END AS label,
                    COUNT(DISTINCT icao) AS n
                FROM military_sightings
                WHERE seen_at >= ? AND seen_at <= ?
                GROUP BY label
                ORDER BY n DESC
                LIMIT 6
            """, (start_ts, end_ts)).fetchall()
            result["military_breakdown"] = [(r["label"], r["n"]) for r in rows]
        except sqlite3.OperationalError:
            # military_sightings table missing on very old DBs — not fatal
            pass

        # Watchlist
        try:
            r = c.execute(
                "SELECT COUNT(DISTINCT icao) AS n, "
                "       COUNT(DISTINCT watchlist_label) AS rules "
                "FROM watchlist_sightings "
                "WHERE seen_at >= ? AND seen_at <= ?",
                (start_ts, end_ts)
            ).fetchone()
            if r:
                result["watchlist_count"]     = r["n"] or 0
                result["watchlist_rules_hit"] = r["rules"] or 0
        except sqlite3.OperationalError:
            pass

        # New records set within the window
        try:
            rows = c.execute("""
                SELECT record_type, value, icao, callsign, aircraft_type, set_at
                FROM stats_records
                WHERE set_at >= ? AND set_at <= ?
                ORDER BY set_at DESC
            """, (start_ts, end_ts)).fetchall()
            result["new_records"] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            # stats_records didn't exist pre-Wave-3
            pass

        # Specials — distinct named special aircraft seen in the window
        try:
            rows = c.execute("""
                SELECT icao, special_label,
                       MAX(callsign)  AS callsign,
                       MAX(seen_at)   AS last_seen_at
                FROM military_sightings
                WHERE seen_at >= ? AND seen_at <= ?
                  AND special_label IS NOT NULL AND special_label != ''
                GROUP BY icao, special_label
                ORDER BY last_seen_at DESC
                LIMIT 5
            """, (start_ts, end_ts)).fetchall()
            result["specials"] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

    finally:
        conn.close()

    return result



def get_app(config: dict, config_path: str) -> FastAPI:
    global CONFIG, CONFIG_PATH, _RESOLVED_WATCHLIST_TAILS
    CONFIG = config
    CONFIG_PATH = config_path

    # v2.57.1: resolve any tail-only watchlist entries to ICAOs once
    # at startup so subsequent search-filter and pill-annotation paths
    # see them as if the user had specified ICAO directly. See
    # _resolve_watchlist_tails docstring for the resolution mechanism.
    db_path = (config.get("data") or {}).get("db_file", "aerodrome.db")
    _RESOLVED_WATCHLIST_TAILS = _resolve_watchlist_tails(config, db_path)

    app = FastAPI(title="Aerodrome")

    # v2.47.0: mount /static for shared assets (theme.css, theme.js). The
    # theme system lived duplicated across nine templates through v2.46.1;
    # this refactor extracted it into static/theme.css and static/theme.js,
    # referenced by every admin template via <link> and <script src>. The
    # FOUC-prevention script stays inline in each template because it must
    # run synchronously before CSS applies — see the "theme:inline-fouc"
    # block in each <head>.
    from fastapi.staticfiles import StaticFiles
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
    else:
        logger.warning(f"Static dir not found at {_static_dir}; theme CSS/JS will 404")

    # v2.50.2: serve favicon at the root path browsers request by default.
    # Without this, every page load logged a 404 for /favicon.ico — low-grade
    # noise but visible in the Logs viewer. The actual file lives at
    # static/favicon.ico (multi-resolution: 16/32/48); the route alias
    # below lets browsers find it without explicit <link> tags. Templates
    # also include explicit <link rel="icon"> tags pointing at the SVG
    # version for browsers that prefer it (most modern ones do — SVG
    # scales perfectly to any DPI).
    from fastapi.responses import FileResponse
    _favicon_ico = _static_dir / "favicon.ico"
    _favicon_svg = _static_dir / "favicon.svg"
    _apple_touch = _static_dir / "apple-touch-icon.png"
    if _favicon_ico.exists():
        @app.get("/favicon.ico", include_in_schema=False)
        async def _favicon_ico_route():
            return FileResponse(str(_favicon_ico), media_type="image/x-icon")
    if _favicon_svg.exists():
        @app.get("/favicon.svg", include_in_schema=False)
        async def _favicon_svg_route():
            return FileResponse(str(_favicon_svg), media_type="image/svg+xml")
    if _apple_touch.exists():
        @app.get("/apple-touch-icon.png", include_in_schema=False)
        async def _apple_touch_route():
            return FileResponse(str(_apple_touch), media_type="image/png")

    # --- Notifier initialization ---
    # Construct the notifier from the loaded config and hand it to the
    # collector. Both the notifier and the collector-side hook are
    # best-effort — if either step fails, the app still runs, just without
    # notifications. Logged at WARNING so it's visible without being fatal.
    try:
        _refresh_notifier_config()
        logger.info("Notifier initialized from config")
    except Exception as e:
        logger.warning(f"Notifier init failed, notifications disabled: {e}")

    # --- Startup self-heal ---
    # Fix up the update/ folder docs on servers crossing the pre-2.40.1 →
    # 2.40.1+ boundary. The file was renamed from README.md to
    # UPDATE_README.md so the name no longer collides with the root README.md
    # when a release is staged in update/, which was the root cause of the
    # "Updates tab shows root README" bug.
    #
    # Two transition cases to handle:
    #
    #   Case 1: legacy README.md AND new UPDATE_README.md both on disk.
    #     Happens when the new applier ran at least once and refreshed
    #     UPDATE_README.md, but didn't know about the legacy file to clean up.
    #     → Delete the legacy.
    #
    #   Case 2: legacy README.md on disk, UPDATE_README.md missing.
    #     Happens when the apply that brought in 2.40.1 was run by the OLD
    #     (pre-2.40.1) server code, which had no _refresh_update_folder_docs
    #     helper and skipped the whole update/ dir during copy. The legacy
    #     file was never replaced and the new file was never copied in.
    #     → If the legacy file's content looks correct (starts with the
    #       expected staging-folder header), rename it to UPDATE_README.md.
    #       If the content is wrong (e.g. a copy of the root README left
    #       behind by the original aliasing bug), delete the legacy so the
    #       Updates tab returns a clean 404 instead of showing misleading
    #       root-README content. In the delete-only case, the next update
    #       apply will repopulate UPDATE_README.md automatically via
    #       _refresh_update_folder_docs.
    try:
        update_dir = Path(__file__).parent / "update"
        legacy_readme = update_dir / "README.md"
        new_readme = update_dir / "UPDATE_README.md"
        if legacy_readme.is_file():
            log = logging.getLogger("adsb.server")
            if new_readme.is_file():
                # Case 1: both present — drop the legacy.
                legacy_readme.unlink()
                log.info(
                    f"Removed legacy {legacy_readme} "
                    f"(replaced by UPDATE_README.md)"
                )
            else:
                # Case 2: only the legacy is present. Sniff first line to
                # decide between rename (correct content) and delete (wrong
                # content from the original aliasing bug).
                try:
                    with legacy_readme.open("r", encoding="utf-8", errors="replace") as f:
                        first_line = f.readline().strip()
                except Exception:
                    first_line = ""
                if first_line.startswith("# Update staging folder"):
                    legacy_readme.rename(new_readme)
                    log.info(
                        f"Promoted legacy {legacy_readme.name} → "
                        f"UPDATE_README.md (content was correct)"
                    )
                else:
                    legacy_readme.unlink()
                    log.warning(
                        f"Removed legacy {legacy_readme} (had wrong content: "
                        f"first line {first_line!r}). Updates tab will 404 "
                        f"until the next update apply refreshes the file."
                    )
    except Exception as e:
        logging.getLogger("adsb.server").warning(
            f"Could not self-heal update/ docs: {e}"
        )

    # v2.50.6: prune accumulated .pre-restore safety snapshots from earlier
    # restores. Until v2.50.6 these were never cleaned up, so installs that
    # restored frequently (or restored a large DB) accumulated multiple
    # gigabytes of stale snapshots in the install dir. Run the trim at
    # startup so existing installs self-heal on their next boot — we don't
    # need to wait for the user to do another restore to free the space.
    try:
        deleted, freed = _prune_pre_restore_snapshots()
        if deleted:
            logging.getLogger("adsb.server").info(
                f"Startup self-heal: pruned {deleted} old pre-restore "
                f"snapshot(s), freed {freed} bytes"
            )
    except Exception as e:
        logging.getLogger("adsb.server").warning(
            f"Could not prune pre-restore snapshots at startup: {e}"
        )

    # v2.50.8: heal historical .backups/<timestamp>/ directories. Pre-v2.50.8
    # the snapshot loop in apply_local_update only filtered by exact-name
    # PRESERVE_PATHS, so anything with a suffix (.pre-restore, .bak.*,
    # .from-backup.*) fell through and got copied verbatim into each code
    # snapshot. Strip those patterns plus __pycache__/*.pyc from existing
    # snapshots so installs that accumulated multi-GB of embedded user
    # data reclaim it on the next service restart. The snapshot folders
    # themselves are preserved — keep-N retention stays in
    # _prune_install_backups, this heal only touches their contents.
    try:
        snap_deleted, snap_freed = _heal_install_snapshots()
        if snap_deleted:
            logging.getLogger("adsb.server").info(
                f"Startup self-heal: stripped {snap_deleted} bloat path(s) "
                f"from .backups/<ts>/ snapshots, freed {snap_freed} bytes"
            )
    except Exception as e:
        logging.getLogger("adsb.server").warning(
            f"Could not heal install snapshots at startup: {e}"
        )

    # v2.50.9: enforce the keep-5 retention promise on `config.yaml.bak.*`
    # auto-backups that's been advertised in the UI since the feature
    # shipped but never actually implemented. On installs that have
    # edited the config many times this trims the historical accumulation
    # on next boot.
    try:
        cfg_deleted, cfg_freed = _prune_config_auto_backups()
        if cfg_deleted:
            logging.getLogger("adsb.server").info(
                f"Startup self-heal: pruned {cfg_deleted} old config "
                f"auto-backup(s), freed {cfg_freed} bytes"
            )
    except Exception as e:
        logging.getLogger("adsb.server").warning(
            f"Could not prune config auto-backups at startup: {e}"
        )

    # --- Frontend ---
    # v2.50.5: cache-bust theme.css and theme.js by appending the current
    # Aerodrome version as a query string. Without this, browsers aggressively
    # cache /static/theme.css across releases — so when v2.50.4 added new
    # CSS rules to theme.css (the .home-link flex layout for the brand mark),
    # users with cached theme.css from v2.50.3 saw a broken header until they
    # hard-refreshed. Appending ?v=<version> changes the URL on every release,
    # which forces browsers to re-fetch the stylesheet. The actual file path
    # is unchanged — query strings are ignored by the static-file handler.
    try:
        _aerodrome_version = (Path(__file__).parent / "VERSION").read_text().strip()
    except Exception:
        _aerodrome_version = "dev"

    def _serve_template(filename: str) -> HTMLResponse:
        template = Path(__file__).parent / "templates" / filename
        html = template.read_text()
        # Append ?v=<version> to theme.css/theme.js URLs. Idempotent — if
        # the template already has a query string we leave it alone.
        html = html.replace(
            'href="/static/theme.css"',
            f'href="/static/theme.css?v={_aerodrome_version}"'
        )
        html = html.replace(
            'src="/static/theme.js"',
            f'src="/static/theme.js?v={_aerodrome_version}"'
        )
        html = html.replace(
            'src="/static/health-indicator.js"',
            f'src="/static/health-indicator.js?v={_aerodrome_version}"'
        )
        # v2.85.12: inject the configured time_format and load the shared
        # timefmt.js helper. The injection sets window._aerodromeTimeFormat
        # synchronously BEFORE timefmt.js loads, so the formatters are
        # initialized with the right mode by the time any inline page JS
        # calls them. Cheaper than per-page /api/ui-config fetches and
        # eliminates the "first render in wrong format" flash. The
        # version querystring busts cache when timefmt.js itself updates.
        time_format = (CONFIG.get("display") or {}).get("time_format", "auto")
        if time_format not in ("auto", "12h", "24h"):
            time_format = "auto"
        timefmt_block = (
            f'<script>window._aerodromeTimeFormat={time_format!r};</script>'
            f'<script src="/static/timefmt.js?v={_aerodrome_version}"></script>'
        )
        html = html.replace('</head>', timefmt_block + '</head>', 1)
        return HTMLResponse(content=html)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _serve_template("index.html")

    # --- Live (direct from receiver, no DB) ---
    @app.get("/api/live")
    async def get_live():
        from collector import is_military
        receiver = CONFIG["receiver"]
        mil_cfg = CONFIG.get("military", {})
        specials = mil_cfg.get("special_aircraft", {})
        default_mil_color = mil_cfg.get("default_color", "#ef4444")
        # Build upper-case lookup for specials
        specials_upper = {k.upper(): v for k, v in specials.items()}

        url = f"http://{receiver['ip']}:{receiver['port']}{receiver['path']}"
        try:
            r = req.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            aircraft = data.get("aircraft", []) if isinstance(data, dict) else data
            # Normalize for frontend
            result = []
            for ac in aircraft:
                if not isinstance(ac, dict):
                    continue
                lat = ac.get("lat")
                lon = ac.get("lon")
                icao_up = ac.get("hex", "").strip().upper()

                # Detect military status using current config rules
                is_mil, special_label = is_military(ac, CONFIG)
                mil_color = None
                mil_label = None
                if is_mil:
                    if icao_up in specials_upper:
                        spec = specials_upper[icao_up]
                        mil_color = spec.get("color")
                        mil_label = spec.get("label") or special_label or "MIL"
                    else:
                        mil_color = default_mil_color
                        mil_label = "MIL"

                result.append({
                    "icao": icao_up,
                    "callsign": (ac.get("flight") or "").strip(),
                    "speed": ac.get("gs"),
                    "lat": lat,
                    "lon": lon,
                    "altitude": ac.get("alt_baro") or ac.get("altitude") or ac.get("alt"),
                    "aircraft_type": ac.get("t") or ac.get("type") or "",
                    "type_desc": ac.get("desc") or ac.get("description") or "",
                    "seen_at": int(time.time()),
                    "distance": _distance_from_receiver(lat, lon),
                    "is_military": is_mil,
                    "mil_label": mil_label,
                    "mil_color": mil_color,
                    "squawk": _normalize_squawk_str(ac.get("squawk")),
                })
            return {"aircraft": result, "count": len(result), "last_updated": int(time.time())}
        except Exception as e:
            logger.error(f"Live fetch failed: {e}")
            return {"aircraft": [], "count": 0, "last_updated": int(time.time()), "error": str(e)}

    # --- Military ---
    @app.get("/api/military")
    async def get_military():
        db_path = CONFIG["data"]["db_file"]
        days = CONFIG["retention"]["military_days"]
        cutoff = int(time.time()) - (days * 86400)

        conn = _open_db_conn(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT icao, callsign, speed, lat, lon, altitude,
                   aircraft_type, type_desc, seen_at, special_label, squawk
            FROM military_sightings WHERE seen_at >= ?
            ORDER BY seen_at DESC
        """, (cutoff,)).fetchall()
        conn.close()

        grouped = {}
        for row in rows:
            entry = dict(row)
            entry["distance"] = _distance_from_receiver(entry.get("lat"), entry.get("lon"))
            _annotate_military(entry)
            # v2.83.3: derive country from the ICAO 24-bit address using
            # the same country_for_icao helper that backs the Stats
            # "Top 5 countries" card. military_sightings doesn't store
            # country, so we resolve at API time; the lookup is a pure
            # function of the ICAO hex (no DB or network), so the per-row
            # cost is trivial.
            from countries import country_for_icao
            entry["country"] = country_for_icao(entry["icao"]) or ""
            icao = entry["icao"]
            if icao not in grouped:
                grouped[icao] = {"latest": entry, "sightings": []}
            grouped[icao]["sightings"].append(entry)

        specials = CONFIG.get("military", {}).get("special_aircraft", {})
        return {
            "aircraft": list(grouped.values()),
            "special_aircraft": {k.upper(): v for k, v in specials.items()},
            "default_military_color": CONFIG.get("military", {}).get("default_color", "#ef4444"),
            "last_updated": int(time.time()),
            "retention_days": days,
        }

    # --- Watchlist sightings ---
    @app.get("/api/watchlist")
    async def get_watchlist_sightings():
        db_path = CONFIG["data"]["db_file"]
        days = CONFIG["retention"]["watchlist_days"]
        cutoff = int(time.time()) - (days * 86400)

        conn = _open_db_conn(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT icao, callsign, speed, lat, lon, altitude,
                   aircraft_type, type_desc, seen_at, watchlist_label, squawk
            FROM watchlist_sightings WHERE seen_at >= ?
            ORDER BY seen_at DESC
        """, (cutoff,)).fetchall()
        conn.close()

        grouped = {}
        for row in rows:
            entry = dict(row)
            entry["distance"] = _distance_from_receiver(entry.get("lat"), entry.get("lon"))
            _annotate_military(entry)
            icao = entry["icao"]
            if icao not in grouped:
                grouped[icao] = {"latest": entry, "sightings": []}
            grouped[icao]["sightings"].append(entry)

        return {
            "aircraft": list(grouped.values()),
            "last_updated": int(time.time()),
            "retention_days": days,
        }

    # --- /api/all/drill ---
    # Fetch individual sightings for a specific ICAO within a window.
    # Originally called by the All-tab frontend when the user expanded a
    # row. v2.67.0 removed the All tab; this endpoint stays because the
    # aircraft detail page (/aircraft/{ICAO}) uses it for the sightings
    # history table — same query, same shape, just a different caller.
    @app.get("/api/all/drill")
    async def get_all_drill(
        icao: str = Query(..., description="ICAO hex of the aircraft to drill into"),
        from_ts: Optional[int] = Query(None, description="Start timestamp (unix)"),
        to_ts: Optional[int] = Query(None, description="End timestamp (unix)"),
        limit: int = Query(500, description="Max sightings to return"),
        offset: int = Query(0, description="Skip this many rows for pagination"),
        order: str = Query("seen_at", description="Sort column"),
        dir: str = Query("desc", description="Sort direction: asc or desc"),
    ):
        icao = icao.strip().upper()
        if not icao:
            return JSONResponse(status_code=400,
                content={"ok": False, "error": "icao required"})
        # Safety cap — 500 sightings is generous. If a user somehow has an
        # aircraft with >500 sightings in the window they're drilling into,
        # they probably want aggregate stats rather than row-by-row.
        limit = max(1, min(limit, 2000))
        # v2.53.3: offset for paginated Load-more on the aircraft detail
        # page. The Search drill doesn't use offset (it loads up to 2000
        # in one shot via "Expand to lifetime"); the detail page uses
        # offset + limit for true pagination through full sighting
        # history. No upper cap — bounded indirectly by total row count.
        offset = max(0, offset)

        # v2.92.0: parameterized ORDER BY for the aircraft detail page's
        # sortable Sightings table. Whitelist of legal sort columns ->
        # SQL expression. Position is intentionally absent — sorting by
        # lat or lon alone is rarely what users want (geographic order
        # is two-dimensional), and a synthetic "distance from receiver"
        # sort would need the receiver coords plumbed in. Squawk is
        # absent for the same reason callsign sorts naturally enough on
        # the existing column — there's a row but it's not a primary
        # sort target. Default falls through to seen_at desc to preserve
        # the legacy behavior for pre-v2.92.0 callers that don't pass
        # the new params.
        _SORT_COLUMN_MAP = {
            "seen_at":  "seen_at",
            "callsign": "callsign",
            "altitude": "altitude",
            "speed":    "speed",
        }
        order_col = _SORT_COLUMN_MAP.get(order, "seen_at")
        dir_norm = (dir or "").lower()
        order_dir = "ASC" if dir_norm == "asc" else "DESC"
        # NULL handling: numeric columns (altitude, speed) can be NULL when
        # the position-only or speed-less sighting hit the table. Push
        # NULLs to the end regardless of direction — matches the convention
        # in _build_order_by_clauses for the search page. seen_at and
        # callsign are NOT NULL in practice (callsign defaults to '').
        if order_col in ("altitude", "speed"):
            order_clause = f"{order_col} IS NULL, {order_col} {order_dir}"
        else:
            order_clause = f"{order_col} {order_dir}"
        # Always tie-break on seen_at DESC so the "most recent" intuition
        # holds for ties — important when sorting by a low-cardinality
        # column like callsign where many rows share the same value.
        if order_col != "seen_at":
            order_clause += ", seen_at DESC"

        db_path = CONFIG["data"]["db_file"]
        days = CONFIG["retention"]["all_days"]
        now = int(time.time())
        if from_ts is None:
            from_ts = now - (days * 86400)
        if to_ts is None:
            to_ts = now

        conn = _open_db_conn(db_path)
        conn.row_factory = sqlite3.Row
        # v2.84.0: instrumented via slow_query_log so the diagnostics UI
        # captures timing and plan when these queries cross the slow
        # threshold. Both queries are suspects for the from_ts=0 query
        # planner pathology — the SELECT may pick idx_all_seen_icao
        # over idx_all_icao, scanning the full table; the COUNT has the
        # same risk and isn't probed by the perf-diag at all.
        from slow_query_log import time_query as _slow_q
        rows = _slow_q(conn, f"""
            SELECT icao, callsign, speed, lat, lon, altitude,
                   aircraft_type, type_desc, seen_at, squawk
            FROM all_sightings
            WHERE icao = ? AND seen_at >= ? AND seen_at <= ?
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """, (icao, from_ts, to_ts, limit, offset),
        endpoint="/api/all/drill", label="drill_select")
        # Count total regardless of limit for the "showing N of M" banner
        total_row = _slow_q(conn, """
            SELECT COUNT(*) AS n FROM all_sightings
            WHERE icao = ? AND seen_at >= ? AND seen_at <= ?
        """, (icao, from_ts, to_ts),
        endpoint="/api/all/drill", label="drill_count", fetch="one")
        total = total_row["n"]
        conn.close()

        sightings = []
        for row in rows:
            entry = dict(row)
            entry["distance"] = _distance_from_receiver(entry.get("lat"), entry.get("lon"))
            sightings.append(entry)

        return {
            "ok": True,
            "icao": icao,
            "sightings": sightings,
            "total_count": total,
            "returned_count": len(sightings),
            "truncated": total > len(sightings),
            "from_ts": from_ts,
            "to_ts": to_ts,
        }

    # --- Watchlist management ---
    class WatchlistEntry(BaseModel):
        identifier: str
        id_type: str              # "icao", "tail", "callsign", or "model"
        label: str = ""
        # When true, also delete rows from watchlist_sightings whose
        # watchlist_label matches the removed entry's label. Only meaningful
        # on /api/watchlist/remove; ignored elsewhere. Defaults to False to
        # preserve existing behavior for any caller that doesn't set it.
        delete_history: bool = False

    def _db_conn():
        """Connect to the sightings DB using the configured path. Mirrors the
        resolution logic in the drill endpoint (absolute path wins, otherwise
        treat as relative to the install dir)."""
        db_path = CONFIG.get("data", {}).get("db_file", "aircraft_history.db")
        if not Path(db_path).is_absolute():
            db_path = str(Path(__file__).parent / db_path)
        return _open_db_conn(db_path)

    def _count_watchlist_history(label: Optional[str]) -> int:
        """Count rows in watchlist_sightings. When label is None/empty, counts
        all rows; otherwise counts only rows with that exact watchlist_label.
        Used by both the per-entry confirm dialog and the clear-all button.
        Returns 0 on any DB error (rather than raising) so the UI can fall
        back to a generic confirm."""
        try:
            conn = _db_conn()
            cur = conn.cursor()
            if label:
                cur.execute(
                    "SELECT COUNT(*) FROM watchlist_sightings WHERE watchlist_label = ?",
                    (label,),
                )
            else:
                cur.execute("SELECT COUNT(*) FROM watchlist_sightings")
            n = cur.fetchone()[0]
            conn.close()
            return int(n)
        except Exception as e:
            logger.warning(f"Count watchlist history failed (label={label!r}): {e}")
            return 0

    # =========================================================================
    # v2.51.0 Phase 2: search endpoints
    # =========================================================================
    # Backend search infrastructure built in Phase 1 (denormalized columns,
    # FTS5, dirty-flag write path) gets its first consumer here. The
    # endpoints are intentionally minimal — token classification and SQL
    # construction live in search.py; server.py is just glue.
    #
    # No UI shipping in this release (Phase 3). Users exercise these
    # endpoints via curl until the Search tab arrives.

    @app.get("/api/search")
    async def get_search(q: str = "", limit: int = 50, offset: int = 0,
                          order: str = "", dir: str = "",
                          from_ts: int = 0, to_ts: int = 0):
        """Free-form search across aircraft. Returns ranked results.

        Query syntax (informal — see docs/SEARCH_DESIGN.md for full grammar):
          - Bare word     → free-text search across all FTS-indexed fields
          - 6 hex chars   → ICAO match (e.g. A12345)
          - Type code     → aircraft type filter (e.g. B738, A320)
          - Country name  → country filter (e.g. Canada, "United States")
          - Tail number   → registration filter (e.g. N12345, G-XYZA)
          - Callsign      → callsign filter (e.g. UAL2024 exact, UAL prefix)
          - Date          → time-range filter (e.g. 2026-04-29, 2026-04, 2026)

        Multiple tokens AND together. Limit is capped to defend against
        memory-bombing requests; no auth here so we play defense at the
        protocol layer.

        v2.60.0: optional sort parameters.
          - order: column to sort by (icao, callsign, aircraft_type,
                   type_desc, operator, country, speed, altitude, squawk,
                   distance, seen_at, first_seen_at, sightings). Empty
                   or unrecognized → relevance order.
          - dir:   asc|desc. Empty or unrecognized → per-column default
                   (descending for numeric/time, ascending for text).

        v2.62.0 (Phase 1E): optional date-range parameters from the
        Search tab's preset row.
          - from_ts: unix seconds, lower bound (inclusive). 0 means
                     "no lower bound" (treated as None).
          - to_ts:   unix seconds, upper bound (exclusive). 0 means
                     "no upper bound" (treated as None).
          When either is non-zero, the parsed query's time_range
          (extracted from typed date tokens like "2025") is overridden
          — preset wins, typed date is silently ignored. This is the
          locked design decision from the v2.62.0 mockup review.
        """
        from search import parse_query, execute_search
        # Defensive caps. limit=50 default, hard ceiling of 500. offset
        # has no upper bound but is bounded indirectly by total_count.
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        # v2.52.0: read date format preference from config so the parser
        # accepts the locale's slash-date convention. Defaults to MDY
        # if the section/key is missing (older configs that haven't
        # been merged with the example config yet).
        date_fmt = (CONFIG.get("display") or {}).get("date_format", "MDY")
        # v2.66.2: compute the user's tz offset from CONFIG['stats']['timezone']
        # so the parser's `today` and `hour:N` tokens build windows in
        # the same timezone as the Patterns hourly_histogram (which is
        # the source of truth for "hour 12" semantics in this app).
        # Same logic as the histogram's stats endpoint — sample the
        # offset at "now" so DST is handled correctly. Falls back to
        # 0 (server-local) if zoneinfo is missing or the config is unset.
        import datetime as _dt
        tz_offset_sec = 0
        st_tz_name = ((CONFIG.get("stats") or {}).get("timezone") or "").strip()
        if st_tz_name:
            try:
                from zoneinfo import ZoneInfo
                _now_dt = _dt.datetime.now(ZoneInfo(st_tz_name))
                tz_offset_sec = int(_now_dt.utcoffset().total_seconds())
            except Exception:
                pass  # bad zone name or zoneinfo missing — leave at 0
        parsed = parse_query(q, date_format=date_fmt,
                              tz_offset_sec=tz_offset_sec)
        # v2.57.0: pass military + watchlist configuration into the
        # executor so boolean-filter tokens "mil"/"military"/"watchlist"
        # can build proper WHERE clauses from the configured prefixes
        # / lists.
        mil_config = CONFIG.get("military") or {}
        watchlist_config = CONFIG.get("watchlist") or []
        # v2.65.0: pass receiver's configured distance unit through so
        # the parser's `distance:LO-HI` filter can convert user-input
        # bounds (in display unit) to canonical km for the WHERE clause.
        rx_unit = (CONFIG.get("receiver", {}) or {}).get("distance_unit", "mi")
        # v2.85.0: switched from sqlite3.connect() to _open_db_conn so
        # this endpoint respects the user's Configuration → Database
        # profile (cache_size, mmap_size, temp_store) like the rest of
        # the hot-path endpoints. The default sqlite3.connect() ignored
        # the profile entirely and used SQLite's 2 MB default cache,
        # which on memory-constrained installs (e.g. Pi 4B 4GB with a
        # 2 GB DB) made every search query effectively cold-cache —
        # the per-query working set didn't fit in the connection cache,
        # so each search re-fetched index pages from disk. Pre-fix:
        # ~100-200 ms per query on a Pi-class install. Post-fix: query
        # cache survives across the search's sub-queries and across
        # successive user searches via WAL-shared mmap.
        conn = _open_db_conn(CONFIG["data"]["db_file"])
        try:
            result = execute_search(
                conn, parsed, limit=limit, offset=offset,
                mil_config=mil_config,
                watchlist_config=watchlist_config,
                # v2.57.1: pass the resolved-tails map so tail-only
                # watchlist entries participate in `wl` / `watchlist`
                # filtering. Map is populated at server startup and
                # refreshed on watchlist add/remove.
                resolved_tails=_RESOLVED_WATCHLIST_TAILS,
                # v2.60.0: user-specified sort. execute_search validates
                # both against allowlists and falls back to relevance on
                # invalid input.
                order=(order or None),
                direction=(dir or None),
                # v2.62.0 (Phase 1E): preset-supplied date range. 0 →
                # treat as None (unbounded on that side). When either
                # bound is set, execute_search overrides any parser-
                # extracted time_range from the query string.
                from_ts=(from_ts if from_ts > 0 else None),
                to_ts=(to_ts if to_ts > 0 else None),
                distance_unit=rx_unit,
            )
        finally:
            conn.close()

        # v2.60.1: last_distance is now a stored column (canonical km)
        # populated by the collector at write time. Convert to the
        # user's configured unit at response time. This replaces the
        # per-row haversine that v2.56.0 did inline. Cost: ~one
        # multiply per row instead of ~6 trig ops.
        for row in result.get("rows", []):
            row["distance"] = _distance_km_to_user_unit(
                row.get("last_distance_km")
            )
            # v2.56.0: attach military annotation (is_military / mil_label /
            # mil_color) so the Search card can render the same MIL pill
            # styling as Live/Watchlist/Military/All-tab rows. _annotate_military
            # mutates the dict in place.
            _annotate_military(row)
            # v2.57.0: attach watchlist annotation (is_watchlist /
            # watchlist_label) so the Search card can render the
            # orange WATCHLIST chip with the user's custom label.
            _annotate_watchlist(row)

        # v2.60.1: distance-sort no longer needs a post-fetch Python
        # reorder — the SQL ORDER BY now sorts the full result set
        # directly via seen_aircraft.last_distance. The pre-v2.60.1
        # post-sort that operated on only the visible page is gone.

        return {
            "ok": "error" not in result,
            "query": q,
            # v2.51.0 Phase 4: include ambiguous_group so the chip UI can
            # render "X or Y: AAL" as a single visual chip rather than two
            # confusing same-value chips. Filters without ambiguity have
            # ambiguous_group missing or null.
            "parsed_filters": parsed["filters"],
            "free_text": parsed["free_text"],
            "time_range": parsed["time_range"],
            "total_count": result["total_count"],
            "rows": result["rows"],
            "execution_ms": result["execution_ms"],
            "error": result.get("error"),
        }

    @app.get("/api/search/aircraft/{icao}")
    async def get_search_aircraft(icao: str):
        """Per-aircraft detail page data. ICAO must be 6 hex chars."""
        from search import detail_for_aircraft
        if len(icao) != 6 or not all(c in "0123456789ABCDEFabcdef" for c in icao):
            raise HTTPException(status_code=400, detail="invalid ICAO hex")
        # v2.85.0: tuned connection — see /api/search above for rationale.
        conn = _open_db_conn(CONFIG["data"]["db_file"])
        try:
            d = detail_for_aircraft(conn, icao)
        finally:
            conn.close()
        if d is None:
            raise HTTPException(status_code=404, detail="aircraft not found")
        return d

    @app.get("/api/search/suggestions")
    async def get_search_suggestions():
        """v2.51.0 Phase 3: derive example queries from THIS install's data
        so the Search tab's empty state shows clickable examples that are
        guaranteed to return at least one result.

        Static example queries (e.g. "B738 Canada") are dangerous as
        empty-state suggestions: a brand-new install or one in different
        airspace could click an example and get zero results, which makes
        the feature look broken. Real-data suggestions sidestep that —
        every example is something the user actually has data for.

        Returns up to 4 suggestions: top type, top country, recent
        callsign, recent ICAO. All queries are bounded and indexed —
        sub-millisecond cost on any plausible install size. The UI
        slots these into the empty-state placeholders as they arrive
        so the page renders the static skeleton synchronously and
        fills in dynamic data when ready.
        """
        # v2.85.0: tuned connection — see /api/search for rationale.
        # Note: this HTML route only runs the search-suggestions query
        # for the typeahead. The detail page's data comes from a separate
        # /api/aircraft/{icao} fetch which has its own tuned connection.
        conn = _open_db_conn(CONFIG["data"]["db_file"])
        try:
            suggestions = []

            # Top aircraft type by sighting count.
            row = conn.execute("""
                SELECT aircraft_type, COUNT(*) AS n
                FROM seen_aircraft
                WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
                GROUP BY aircraft_type
                ORDER BY n DESC LIMIT 1
            """).fetchone()
            if row and row[0]:
                suggestions.append({
                    "query": row[0],
                    "label": f"{row[0]} ({row[1]} aircraft)",
                    "kind": "type",
                })

            # Top country by aircraft count. Wrap multi-word countries
            # in quotes so the query parser handles them as a single
            # token (matches the parser's multi-token country logic).
            row = conn.execute("""
                SELECT country, COUNT(*) AS n
                FROM seen_aircraft
                WHERE country IS NOT NULL AND country != ''
                GROUP BY country
                ORDER BY n DESC LIMIT 1
            """).fetchone()
            if row and row[0]:
                suggestions.append({
                    "query": row[0],  # multi-word countries handled by parser
                    "label": f"{row[0]} ({row[1]} aircraft)",
                    "kind": "country",
                })

            # Most recent callsign (last_callsign != '' AND not just digits).
            # We avoid pure-numeric callsigns since those parse as years/dates.
            row = conn.execute("""
                SELECT last_callsign, last_seen_at
                FROM seen_aircraft
                WHERE last_callsign IS NOT NULL
                  AND last_callsign != ''
                  AND last_callsign GLOB '*[A-Z]*'
                ORDER BY last_seen_at DESC LIMIT 1
            """).fetchone()
            if row and row[0]:
                suggestions.append({
                    "query": row[0],
                    "label": f"callsign {row[0]}",
                    "kind": "callsign",
                })

            # Most recently seen aircraft (ICAO lookup demo).
            row = conn.execute("""
                SELECT icao, aircraft_type, country
                FROM seen_aircraft
                WHERE last_seen_at IS NOT NULL
                ORDER BY last_seen_at DESC LIMIT 1
            """).fetchone()
            if row and row[0]:
                hint = " ".join(filter(None, [row[1], row[2]]))
                suggestions.append({
                    "query": row[0],
                    "label": f"ICAO {row[0]}" + (f" — {hint}" if hint else ""),
                    "kind": "icao",
                })

            # v2.52.0: include the install's configured date format so the
            # frontend's help panel can show the matching slash-date example.
            # Cheap to thread through here rather than adding a separate
            # config-fetch endpoint just for this.
            date_fmt = (CONFIG.get("display") or {}).get("date_format", "MDY")
            return {"ok": True, "suggestions": suggestions, "date_format": date_fmt}
        except Exception as e:
            logger.warning(f"/api/search/suggestions failed: {e}")
            return {"ok": False, "suggestions": [], "error": str(e)}
        finally:
            conn.close()

    @app.get("/api/watchlist/entries")
    async def get_watchlist_entries():
        return {"entries": CONFIG.get("watchlist", []) or []}

    @app.post("/api/watchlist/add")
    async def add_watchlist_entry(entry: WatchlistEntry):
        watchlist = CONFIG.get("watchlist") or []

        new_entry = {"label": entry.label or entry.identifier}
        if entry.id_type == "icao":
            new_entry["icao"] = entry.identifier.upper()
        elif entry.id_type == "tail":
            new_entry["tail"] = entry.identifier.upper()
        elif entry.id_type == "callsign":
            new_entry["callsign"] = entry.identifier.upper()
        elif entry.id_type == "model":
            # Preserve original case for the stored value; matching is case-insensitive
            new_entry["model"] = entry.identifier.strip()
        else:
            return JSONResponse(status_code=400, content={"error": f"Unknown id_type: {entry.id_type}"})

        # Duplicate check — compare case-insensitively across all identifier types
        for existing in watchlist:
            if (existing.get("icao", "").upper() == new_entry.get("icao", "").upper() and
                existing.get("tail", "").upper() == new_entry.get("tail", "").upper() and
                existing.get("callsign", "").upper() == new_entry.get("callsign", "").upper() and
                existing.get("model", "").lower() == new_entry.get("model", "").lower()):
                return JSONResponse(status_code=409, content={"error": "Already in watchlist"})

        watchlist.append(new_entry)
        CONFIG["watchlist"] = watchlist
        _save_config()
        # v2.57.1: re-resolve tail-only entries so a new {tail: "..."}
        # entry surfaces in search filtering as soon as it's added,
        # provided hexdb_cache has the tail. Cheap (one SELECT per
        # tail entry).
        global _RESOLVED_WATCHLIST_TAILS
        db_path = (CONFIG.get("data") or {}).get("db_file", "aerodrome.db")
        _RESOLVED_WATCHLIST_TAILS = _resolve_watchlist_tails(CONFIG, db_path)
        return {"status": "added", "entry": new_entry, "message": "Live within one poll cycle"}

    @app.post("/api/watchlist/remove")
    async def remove_watchlist_entry(entry: WatchlistEntry):
        watchlist = CONFIG.get("watchlist") or []
        identifier = entry.identifier
        ident_upper = identifier.upper()
        ident_lower = identifier.lower()

        # Find the matching entry first so we can grab its label for the
        # optional history delete. If delete_history is false we'll just
        # discard the label. Two entries could match (e.g. someone typed
        # the same ICAO twice in different cases) — unlikely, but we take
        # the first hit since the list comprehension below will drop all
        # matches anyway.
        matched = None
        for e in watchlist:
            if (e.get("icao", "").upper() == ident_upper or
                    e.get("tail", "").upper() == ident_upper or
                    e.get("callsign", "").upper() == ident_upper or
                    e.get("model", "").lower() == ident_lower):
                matched = e
                break

        if matched is None:
            return JSONResponse(status_code=404, content={"error": f"'{identifier}' not found"})

        matched_label = matched.get("label", "")

        # Filter out all matching entries from the config. Using the same
        # predicate as the original loop so behavior is unchanged for the
        # config side.
        watchlist = [e for e in watchlist if not (
            e.get("icao", "").upper() == ident_upper or
            e.get("tail", "").upper() == ident_upper or
            e.get("callsign", "").upper() == ident_upper or
            e.get("model", "").lower() == ident_lower
        )]
        CONFIG["watchlist"] = watchlist
        _save_config()
        # v2.57.1: re-resolve tail-only entries — same rationale as in
        # the add path. After a remove, the resolved-tails map should
        # no longer carry the removed tail.
        global _RESOLVED_WATCHLIST_TAILS
        db_path = (CONFIG.get("data") or {}).get("db_file", "aerodrome.db")
        _RESOLVED_WATCHLIST_TAILS = _resolve_watchlist_tails(CONFIG, db_path)

        # Optionally delete history rows matching the removed entry's label.
        # Label-match semantics: rows written by the collector carry the
        # label active at match time. If two watchlist entries share a
        # label, removing one deletes the other's history too — a known
        # trade-off (see CHANGELOG). The caller is expected to show the
        # affected count before confirming so this isn't surprising.
        deleted = 0
        history_error = None
        if entry.delete_history and matched_label:
            try:
                conn = _db_conn()
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM watchlist_sightings WHERE watchlist_label = ?",
                    (matched_label,),
                )
                deleted = cur.rowcount or 0
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Failed to delete history for label {matched_label!r}: {e}")
                history_error = str(e)

        return {
            "status": "removed",
            "identifier": identifier,
            "label": matched_label,
            "history_deleted": deleted,
            "history_error": history_error,
        }

    @app.get("/api/watchlist/history/count")
    async def watchlist_history_count(label: Optional[str] = Query(None)):
        """Return how many watchlist_sightings rows match a given label, or
        the total row count if no label is given. Used by the UI to populate
        the remove-with-history confirm dialog and the clear-all-history
        button. Never errors — returns 0 on DB failure."""
        return {"label": label, "count": _count_watchlist_history(label)}

    @app.post("/api/watchlist/history/clear")
    async def watchlist_history_clear():
        """Delete ALL rows from watchlist_sightings. Irreversible. The UI
        gates this behind an explicit confirm showing the row count."""
        try:
            conn = _db_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM watchlist_sightings")
            deleted = cur.rowcount or 0
            conn.commit()
            conn.close()
            logger.info(f"Cleared all watchlist history ({deleted} rows)")
            return {"status": "cleared", "deleted": deleted}
        except Exception as e:
            logger.error(f"Clear watchlist history failed: {e}")
            return JSONResponse(
                status_code=500,
                content={"status": "error", "error": str(e)},
            )

    # --- First-seen lookups ---
    # Surfaces the seen_aircraft.first_seen_at column (maintained by the
    # collector for the "first time seen today" stats card) to the frontend,
    # where a small chip next to each aircraft's label/callsign shows how
    # long we've been tracking them. Batches multiple ICAOs into a single
    # query so per-tab renders only cost one round-trip regardless of how
    # many rows are visible.
    @app.get("/api/first-seen")
    async def get_first_seen(icaos: str = Query("", description="Comma-separated ICAO hex list")):
        """Return first_seen_at timestamps for the given ICAOs. Empty input
        returns an empty map. Unknown ICAOs are simply omitted from the
        response (no error — the frontend treats absence as 'no data')."""
        # Parse + normalize: split on commas, strip whitespace, uppercase,
        # drop empties, de-dupe. Cap at 5000 to keep the SQL placeholder
        # count bounded — matches the /api/all limit.
        raw = [s.strip().upper() for s in (icaos or "").split(",")]
        wanted = list({s for s in raw if s})[:5000]
        if not wanted:
            return {"first_seen": {}}
        try:
            conn = _db_conn()
            cur = conn.cursor()
            placeholders = ",".join("?" * len(wanted))
            cur.execute(
                f"SELECT icao, first_seen_at FROM seen_aircraft WHERE icao IN ({placeholders})",
                wanted,
            )
            result = {row[0]: int(row[1]) for row in cur.fetchall()}
            conn.close()
            return {"first_seen": result}
        except Exception as e:
            logger.warning(f"first-seen lookup failed: {e}")
            # Return an empty map rather than 500 — the frontend's chip is
            # optional decoration; a transient DB error shouldn't turn into
            # a visible failure on the aircraft tabs.
            return {"first_seen": {}}

    # --- Tail resolution for Track-link URL building ---
    # FR24, AirNav Radar, and PlaneFinder URL patterns want the aircraft's
    # registration (tail number) — e.g. /data/aircraft/N487UA. The ADS-B
    # feed gives us only the ICAO hex, so we need to resolve hex→tail via
    # hexdb.io.
    #
    # Design: never block the HTTP request on external calls. The endpoint
    # reads from a cache maintained by collector._ICAO_CACHE, and pushes
    # any unresolved ICAOs into _tail_resolve_queue. A background thread
    # drains the queue at ~2 req/sec (respectful to hexdb's free API) and
    # populates the cache. Consequence: the first time a new ICAO appears,
    # its Track link gracefully falls back to airplanes.live (whatever the
    # frontend configured as fallback); within a few seconds the cache
    # fills and subsequent renders use the provider-specific reg URL.
    import collector as _collector_mod
    import queue as _queue
    import threading as _threading

    _tail_resolve_queue: _queue.Queue = _queue.Queue(maxsize=5000)
    _tail_resolve_seen: set = set()          # what we've queued this process life
    _tail_resolve_seen_lock = _threading.Lock()

    def _tail_resolve_worker():
        """Background thread that drains the queue and resolves ICAOs via
        hexdb.io at a ~2 req/sec pace. Populates collector._ICAO_CACHE.
        Runs for the lifetime of the process; deliberately non-daemon so
        systemctl's graceful shutdown can still flush in-flight work."""
        import time as _t
        while True:
            try:
                icao = _tail_resolve_queue.get(timeout=1.0)
            except _queue.Empty:
                continue
            if not icao:
                continue
            # resolve_icao_to_tail handles its own caching + errors. We just
            # call it — side effect is that _ICAO_CACHE[icao] now has an
            # answer (the tail or None sentinel).
            try:
                _collector_mod.resolve_icao_to_tail(icao)
            except Exception as e:
                logger.warning(f"tail resolve worker: unexpected error for {icao}: {e}")
            # Rate limit: ~2 req/sec to be polite to hexdb.io. Free API,
            # no published rate limit, but respect is cheaper than apology.
            _t.sleep(0.5)

    _worker_thread = _threading.Thread(
        target=_tail_resolve_worker,
        name="tail-resolve-worker",
        daemon=True,  # daemon=True → process exit doesn't hang waiting for it
    )
    _worker_thread.start()
    logger.info(
        f"Tail-resolver background worker started "
        f"(rate ~2 req/sec, queue cap 5000; "
        f"curl /api/resolve-tail/debug for status)"
    )

    # =========================================================================
    # Daily summary scheduler (v2.41.35)
    # =========================================================================
    # Background thread that ticks every 60s and evaluates whether to fire
    # a daily summary notification. Fire condition:
    #   1. notifications.enabled = true
    #   2. notifications.events.daily_summary = true
    #   3. current HH:MM (in stats_timezone) == configured time
    #   4. haven't already fired on this local-day
    #
    # "Haven't fired today" uses a persisted last_sent_date (YYYY-MM-DD in
    # the stats timezone) stored in a JSON file next to the DB. On restart
    # after the scheduled time has passed, the date check prevents firing.
    # If the service was down when the scheduled time passed, we skip —
    # this is the "no backfill" policy the user chose.

    def _daily_summary_state_path() -> Path:
        """JSON file holding the daily-summary scheduler's state. Sits
        next to the DB so it follows the deployment's data directory."""
        db_file = CONFIG.get("data", {}).get("db_file") or "aerodrome.db"
        return Path(db_file).resolve().parent / ".daily-summary-state.json"

    def _daily_summary_load_state() -> dict:
        p = _daily_summary_state_path()
        if not p.exists():
            return {}
        try:
            import json as _json
            return _json.loads(p.read_text()) or {}
        except Exception as e:
            logger.warning(f"daily summary: could not read state file: {e}")
            return {}

    def _daily_summary_save_state(state: dict) -> None:
        p = _daily_summary_state_path()
        try:
            import json as _json
            p.write_text(_json.dumps(state, indent=2))
        except Exception as e:
            logger.warning(f"daily summary: could not write state file: {e}")

    def _daily_summary_local_date_str(stats_tz_name: Optional[str]) -> str:
        """Today's YYYY-MM-DD in the stats timezone. Falls back to local
        system time if the stats tz can't be resolved."""
        import datetime as _dt
        tz = None
        if stats_tz_name:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(stats_tz_name)
            except Exception:
                tz = None
        return _dt.datetime.now(tz=tz).strftime("%Y-%m-%d")

    def _daily_summary_current_hhmm(stats_tz_name: Optional[str]) -> str:
        """HH:MM in the stats timezone. Used for the match against the
        configured fire time."""
        import datetime as _dt
        tz = None
        if stats_tz_name:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(stats_tz_name)
            except Exception:
                tz = None
        return _dt.datetime.now(tz=tz).strftime("%H:%M")

    def _daily_summary_fire_now():
        """Gather data and fire the summary. Called from the scheduler
        when all the fire conditions are met, and from the test endpoint
        when the user clicks 'Send test summary'."""
        global _NOTIFIER
        if _NOTIFIER is None:
            return False, "notifier not initialized"
        db_path = CONFIG["data"]["db_file"]
        data = compose_daily_summary_data(db_path, CONFIG)
        # Pull version for the footer line
        version = None
        try:
            vf = Path(__file__).parent / "VERSION"
            if vf.exists():
                version = vf.read_text().strip()
        except Exception:
            pass
        ok = _NOTIFIER.send_daily_summary(data, version=version)
        return ok, "sent" if ok else "suppressed (check notifications log)"

    def _daily_summary_scheduler():
        """Background thread body — ticks every 60s."""
        import time as _t
        # First tick: sleep a moment so we don't fire in the middle of
        # a config reload / service restart.
        _t.sleep(10.0)
        logger.info("Daily-summary scheduler started")

        while True:
            try:
                nt = CONFIG.get("notifications") or {}
                ds = (nt.get("daily_summary") or {})
                events = (nt.get("events") or {})
                enabled = bool(nt.get("enabled")) and bool(events.get("daily_summary"))
                fire_time = (ds.get("time") or "").strip()
                stats_tz = (CONFIG.get("stats") or {}).get("timezone") or None

                # Is there a configured fire time? Empty string means user
                # enabled the event but never picked a time — don't fire.
                if enabled and fire_time and len(fire_time) == 5 and fire_time[2] == ":":
                    current_hhmm = _daily_summary_current_hhmm(stats_tz)
                    today = _daily_summary_local_date_str(stats_tz)
                    state = _daily_summary_load_state()
                    last_sent = state.get("last_sent_date")

                    if current_hhmm == fire_time and last_sent != today:
                        ok, msg = _daily_summary_fire_now()
                        # Mark the day sent regardless of whether the
                        # notification actually went out. If it was
                        # suppressed (quiet hours, rate limit, etc.) the
                        # user still doesn't want a retry at 08:01. The
                        # whole point of "daily" is one attempt per day.
                        state["last_sent_date"] = today
                        state["last_sent_at_unix"] = int(_t.time())
                        state["last_sent_result"] = msg
                        _daily_summary_save_state(state)
                        logger.info(f"Daily summary fired: {msg}")
            except Exception as e:
                logger.warning(f"Daily-summary scheduler tick failed: {e}")

            # Tick once a minute. HH:MM has minute resolution; no point
            # checking more often.
            _t.sleep(60.0)

    _daily_summary_thread = _threading.Thread(
        target=_daily_summary_scheduler,
        name="daily-summary-scheduler",
        daemon=True,
    )
    _daily_summary_thread.start()

    # ──────────────────────────────────────────────────────────────────
    # v3.0.0: GitHub-Releases-based update channel
    # ──────────────────────────────────────────────────────────────────
    # Periodically checks the GitHub Releases API for newer versions and
    # surfaces results via /api/updates/github/check, the /updates page
    # banner, and (when warned) the gear-menu badge. Apply path lands in
    # v3.0.1; ntfy push lands in v3.0.2. For now this milestone delivers
    # the "Aerodrome knows when it's outdated" surface — the apply step
    # still goes through the existing local-update flow.

    import urllib.request as _urllib_request
    import urllib.error as _urllib_error
    import json as _json_module

    _update_check_lock = _threading.Lock()
    GITHUB_RELEASES_LATEST_URL = (
        "https://api.github.com/repos/preston-peterson/aerodrome/releases/latest"
    )
    _POLL_INTERVAL_SECONDS = {
        "daily": 86400,
        "weekly": 604800,
        "monthly": 2592000,  # 30 days
        "never": None,
    }

    def _updates_config() -> dict:
        """Read updates.github config with defaults applied. Defaults are
        the v3.0.0 launch defaults: enabled, monthly polling, banner on,
        gear badge on. Re-reads CONFIG every call so the scheduler picks
        up live config edits within its sleep granularity (1 hour cap)."""
        upd = (CONFIG.get("updates") or {}).get("github") or {}
        notify = upd.get("notify") or {}
        return {
            "enabled": bool(upd.get("enabled", True)),
            "poll_interval": upd.get("poll_interval", "monthly"),
            "notify_banner": bool(notify.get("banner", True)),
            "notify_gear_badge": bool(notify.get("gear_badge", True)),
            "notify_ntfy": bool(notify.get("ntfy", False)),
        }

    def _get_running_version() -> str:
        """Read running version from the VERSION file alongside server.py.
        Returns the raw string (e.g. '2.98.3') or '' if unreadable."""
        try:
            version_path = Path(__file__).parent / "VERSION"
            if version_path.exists():
                return version_path.read_text().strip()
        except Exception:
            pass
        return ""

    def _parse_semver(tag: str) -> Optional[tuple]:
        """Parse 'v2.98.3' or '2.98.3' into (2, 98, 3). Returns None for
        anything that doesn't parse as exactly three integer components —
        which means non-semver tags (e.g. date-based, '-rc1' suffixes)
        will not contribute to version comparison and will be treated
        as 'no actionable update' rather than crashing the check."""
        if not tag:
            return None
        s = tag.lstrip("v").strip()
        # Reject anything with a pre-release/build suffix; releases/latest
        # excludes pre-releases by definition, but defensive belt-and-braces.
        if "-" in s or "+" in s:
            return None
        parts = s.split(".")
        if len(parts) != 3:
            return None
        try:
            return tuple(int(p) for p in parts)
        except ValueError:
            return None

    def _is_newer_version(latest_tag: str, running: str) -> bool:
        """True if latest_tag is strictly newer than running. False for
        equal, older, or unparseable inputs — Aerodrome never suggests
        downgrades, and never crashes on non-semver tag formats."""
        l = _parse_semver(latest_tag)
        r = _parse_semver(running)
        if l is None or r is None:
            return False
        return l > r

    def _get_update_state() -> dict:
        """Read the single-row update_state from SQLite. Returns dict with
        all five state fields; all may be None when the table is empty
        (first run, never checked)."""
        empty = {
            "last_check_ts": None,
            "last_check_result": None,
            "last_check_error": None,
            "last_known_latest": None,
            "last_known_latest_ts": None,
        }
        try:
            db_path = CONFIG["data"]["db_file"]
            conn = _open_db_conn(db_path)
            row = conn.execute(
                "SELECT last_check_ts, last_check_result, last_check_error, "
                "last_known_latest, last_known_latest_ts "
                "FROM update_state WHERE id = 1"
            ).fetchone()
            conn.close()
            if row is None:
                return empty
            return {
                "last_check_ts": row[0],
                "last_check_result": row[1],
                "last_check_error": row[2],
                "last_known_latest": row[3],
                "last_known_latest_ts": row[4],
            }
        except Exception as e:
            logger.warning(f"_get_update_state: read failed: {e}")
            return {**empty, "last_check_error": str(e)}

    def _save_update_state(result: str, error: Optional[str],
                           latest_tag: Optional[str]) -> None:
        """Persist a check result. result is 'success' or 'error'.

        On success: latest_tag becomes the new last_known_latest and we
        update last_known_latest_ts to now. last_check_error is cleared.

        On error: last_known_latest and last_known_latest_ts are preserved
        from the previous row (if any) — a transient network glitch
        shouldn't wipe out our knowledge of the most recent successful
        check, and the UI uses last_known_latest_ts to render 'last
        successful check: N hours ago' alongside the error message."""
        now = int(time.time())
        try:
            db_path = CONFIG["data"]["db_file"]
            conn = _open_db_conn(db_path)
            if result == "success":
                conn.execute(
                    "INSERT OR REPLACE INTO update_state "
                    "(id, last_check_ts, last_check_result, last_check_error, "
                    "last_known_latest, last_known_latest_ts, updated_at) "
                    "VALUES (1, ?, 'success', NULL, ?, ?, ?)",
                    (now, latest_tag, now, now),
                )
            else:
                existing = conn.execute(
                    "SELECT last_known_latest, last_known_latest_ts "
                    "FROM update_state WHERE id = 1"
                ).fetchone()
                keep_latest = existing[0] if existing else None
                keep_latest_ts = existing[1] if existing else None
                conn.execute(
                    "INSERT OR REPLACE INTO update_state "
                    "(id, last_check_ts, last_check_result, last_check_error, "
                    "last_known_latest, last_known_latest_ts, updated_at) "
                    "VALUES (1, ?, 'error', ?, ?, ?, ?)",
                    (now, error, keep_latest, keep_latest_ts, now),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"_save_update_state: write failed: {e}")

    def _perform_github_check() -> dict:
        """Hit the GitHub Releases API, parse, persist, return new state.
        Acquires _update_check_lock so a manual 'Check now' button click
        racing with a scheduled tick produces exactly one HTTP call and
        one state update, not two interleaved ones.

        v3.0.2: on a successful check that discovers a strictly-newer tag
        than the previous last_known_latest (transition event, not every
        tick), fires the update_available ntfy notification. Read old
        state BEFORE save, fire AFTER save — order matters so the cached
        state the notification's tap-to-open will display is already the
        new state by the time the user opens it."""
        with _update_check_lock:
            # v3.0.2: snapshot the previous last_known_latest so we can
            # detect the transition after the save. Snapshot happens
            # inside the lock so a concurrent manual check can't race.
            prior_state = _get_update_state()
            prior_latest = prior_state["last_known_latest"]

            new_tag = None
            try:
                req_obj = _urllib_request.Request(
                    GITHUB_RELEASES_LATEST_URL,
                    headers={
                        "User-Agent": "Aerodrome/update-check",
                        "Accept": "application/vnd.github+json",
                    },
                )
                with _urllib_request.urlopen(req_obj, timeout=30) as response:
                    data = _json_module.loads(response.read().decode("utf-8"))
                    tag = (data.get("tag_name") or "").strip()
                    if not tag:
                        _save_update_state(
                            "error",
                            "GitHub response had no tag_name",
                            None,
                        )
                    else:
                        _save_update_state("success", None, tag)
                        new_tag = tag
                        logger.info(
                            f"Update check: latest GitHub release is {tag}"
                        )
            except _urllib_error.HTTPError as e:
                if e.code == 403:
                    msg = ("GitHub API rate limit exceeded "
                           "(60 req/hour for anonymous requests).")
                elif e.code == 404:
                    msg = "No published Releases found on GitHub yet."
                else:
                    msg = f"GitHub returned HTTP {e.code}."
                logger.warning(f"Update check failed: {msg}")
                _save_update_state("error", msg, None)
            except _urllib_error.URLError as e:
                msg = f"Couldn't reach GitHub: {e.reason}"
                logger.warning(f"Update check failed: {msg}")
                _save_update_state("error", msg, None)
            except Exception as e:
                msg = f"Unexpected error: {type(e).__name__}: {e}"
                logger.warning(f"Update check failed: {msg}")
                _save_update_state("error", msg, None)

            # v3.0.2: notification fires AFTER state save, on transition only.
            # Transition is defined as: a successful check AND new_tag is
            # strictly newer than both the prior last_known_latest AND the
            # running version. The running-version comparison is what
            # prevents notifying for "you discovered v3.1.0 but you're
            # already on v3.1.0" scenarios (e.g., manual install ahead of
            # scheduler's discovery). The prior_latest comparison is what
            # prevents re-firing every poll tick until the user applies —
            # treat NULL prior as "first discovery is a transition too" so
            # the notification fires on the first successful check after
            # install (when there was no prior knowledge).
            if new_tag:
                try:
                    cfg = _updates_config()
                    running = _get_running_version()
                    is_newer_than_prior = (
                        prior_latest is None
                        or _is_newer_version(new_tag, prior_latest)
                    )
                    is_newer_than_running = _is_newer_version(new_tag, running)
                    if (cfg["notify_ntfy"]
                            and is_newer_than_prior
                            and is_newer_than_running):
                        _notify_update_available(new_tag, running)
                except Exception as e:
                    # Notifier failure must never crash the scheduler. The
                    # state has already been saved successfully; the user
                    # will see the update on the /updates page regardless,
                    # they just won't get the push.
                    logger.warning(
                        f"update_available notification failed (state already saved): {e}"
                    )

            return _get_update_state()

    def _notify_update_available(new_tag: str, running: str) -> None:
        """Compose and fire the update_available ntfy notification. The
        notifier handles all gating internally (master switch, per-event
        opt-in, quiet hours, cooldown, rate limit) — we just call it and
        let it decide whether to actually send."""
        global _NOTIFIER
        if _NOTIFIER is None:
            # Notifier not initialized yet — early startup race. Skip
            # silently; the next discovery (if there is one) will retry.
            return
        title = f"Aerodrome update available: {new_tag}"
        body = (
            f"{new_tag} is available. You're on v{running}. "
            f"Open the Updates page to apply."
        )
        # click_route='updates' lands the tap-to-open on /updates so the
        # user can click Apply directly. Requires notifications.public_url
        # to be configured; without it, the tap defaults to the ntfy app.
        _NOTIFIER.notify(
            event="update_available",
            title=title,
            body=body,
            tags=["arrow_up"],
            click_route="updates",
        )

    def _interval_elapsed() -> bool:
        """True if the configured poll interval has elapsed since the
        last successful check, or if no successful check has happened
        yet. Returns False when poll_interval is 'never' — manual
        'Check now' clicks still work in that mode."""
        cfg = _updates_config()
        pi = cfg["poll_interval"]
        if pi == "never":
            return False
        interval_s = _POLL_INTERVAL_SECONDS.get(pi)
        if interval_s is None:
            return False
        state = _get_update_state()
        last = state["last_known_latest_ts"]
        if last is None:
            return True  # Never successfully checked — first check on startup
        return (int(time.time()) - last) >= interval_s

    def _seconds_until_next_check() -> int:
        """How long the scheduler should sleep before next deciding whether
        to check. Bounded to [60s, 3600s]: the 1-hour cap means changing
        poll_interval (or toggling enabled) takes effect within an hour
        with no service restart needed — the LIVE_KEYS contract."""
        cfg = _updates_config()
        pi = cfg["poll_interval"]
        if pi == "never" or pi not in _POLL_INTERVAL_SECONDS:
            return 3600
        interval_s = _POLL_INTERVAL_SECONDS[pi]
        state = _get_update_state()
        last = state["last_known_latest_ts"]
        if last is None:
            return 60  # Check soon — startup case with no prior success
        elapsed = int(time.time()) - last
        remaining = max(60, interval_s - elapsed)
        return min(remaining, 3600)

    def _update_check_scheduler():
        """Background thread body. Wakes on schedule; calls
        _perform_github_check() when the interval has elapsed and updates
        are enabled. Modeled on _daily_summary_scheduler. Always starts
        regardless of enabled flag so enabling at runtime via config edit
        works without service restart."""
        import time as _t
        # Settle delay: avoid firing in the middle of a service restart
        _t.sleep(15.0)
        logger.info("Update-check scheduler started")

        while True:
            try:
                cfg = _updates_config()
                if cfg["enabled"] and _interval_elapsed():
                    _perform_github_check()
            except Exception as e:
                logger.warning(f"Update-check scheduler tick failed: {e}")
            _t.sleep(_seconds_until_next_check())

    _update_check_thread = _threading.Thread(
        target=_update_check_scheduler,
        name="update-check-scheduler",
        daemon=True,
    )
    _update_check_thread.start()

    @app.post("/api/notifications/daily-summary/test")
    async def post_daily_summary_test():
        """Compose + send a daily summary immediately, bypassing the
        scheduled-time check but NOT the usual notify() gates (disabled,
        quiet hours, rate limit). For the UI's 'Send test summary'
        button. Returns {ok, message, body_preview} so the client can
        show the composed text even when the notification is suppressed.

        v2.42.4: wrapped the whole body in try/except. Earlier version
        let exceptions escape FastAPI's default handler, which returns a
        500 HTML page — the UI's r.json() then threw 'JSON.parse:
        unexpected character at line 1 column 0' with no useful context
        for the user. The traceback IS logged server-side. Now we catch
        everything, log the traceback, and return a structured JSON
        error the client can display.
        """
        import traceback as _tb
        global _NOTIFIER
        try:
            if _NOTIFIER is None:
                return JSONResponse({"ok": False,
                                     "message": "notifier not initialized"})

            db_path = CONFIG["data"]["db_file"]
            data = compose_daily_summary_data(db_path, CONFIG)
            version = None
            try:
                vf = Path(__file__).parent / "VERSION"
                if vf.exists():
                    version = vf.read_text().strip()
            except Exception:
                pass

            # Compose for the preview BEFORE sending — so the client sees
            # what the message would look like even if it was suppressed.
            title, body = _NOTIFIER.compose_daily_summary_body(data, version=version)
            ok = _NOTIFIER.send_daily_summary(data, version=version)
            return JSONResponse({
                "ok": ok,
                "message": "sent" if ok else "suppressed (disabled, quiet hours, or rate limited)",
                "title": title,
                "body_preview": body,
                "window": {"start": data["window_start_ts"], "end": data["window_end_ts"]},
            })
        except Exception as e:
            # Log the full traceback so operators have something to look at
            # in journalctl. Return a compact error to the client so the UI
            # can show what went wrong without surfacing stack traces.
            logger.error("Daily summary test failed: %s\n%s",
                         e, _tb.format_exc())
            return JSONResponse(status_code=500, content={
                "ok": False,
                "message": f"Server error while composing summary: "
                           f"{type(e).__name__}: {e}",
            })

    @app.get("/api/resolve-tail")
    async def get_resolve_tail(icaos: str = Query("", description="Comma-separated ICAO hex list")):
        """Return hex → registration mappings for the requested ICAOs.

        Only returns what's currently cached (including negative-cached
        entries, which are returned as empty string so the frontend can
        distinguish 'looked up, none found' from 'not yet looked up').
        Any ICAOs missing from the response should be assumed unresolved;
        the endpoint queues them for background resolution, so a
        subsequent call a few seconds later will usually have them.

        Response: {"tails": {"A835D2": "N487UA", "ABCDEF": "", ...}}
          - present with non-empty value → use the registration in URLs
          - present with empty value → aircraft has no known registration
            (e.g. anonymized privacy ICAO, military with no civilian reg)
          - absent → not cached yet; try again after a refresh cycle
        """
        raw = [s.strip().upper() for s in (icaos or "").split(",")]
        wanted = list({s for s in raw if s})[:500]  # bound per-request
        if not wanted:
            return {"tails": {}}

        result = {}
        to_resolve = []
        for icao in wanted:
            if icao in _collector_mod._ICAO_CACHE:
                cached = _collector_mod._ICAO_CACHE[icao]
                # Empty string = negative-cached sentinel in the response.
                # None = same thing but distinguishing keeps the client's
                # logic simple (just check for truthy).
                result[icao] = cached or ""
            else:
                to_resolve.append(icao)

        # Inline resolution with a time budget. Without this, the first time
        # an aircraft appears the frontend gets an empty response and falls
        # back to airplanes.live until the background worker catches up —
        # 10+ seconds later. On a high-churn Live tab, aircraft often
        # disappear before the worker gets to them. Resolving inline means
        # the first fetch response includes tail numbers for as many
        # ICAOs as we can fit in ~3 seconds (at hexdb's typical response
        # time of ~200ms, that's roughly 15 lookups synchronously).
        # Anything beyond the budget goes to the background worker.
        #
        # resolve_icao_to_tail is blocking (uses requests.get); we dispatch
        # through asyncio.to_thread so we don't block FastAPI's event loop
        # for the duration of the budget.
        import asyncio as _asyncio
        import time as _t
        INLINE_BUDGET_SEC = 3.0
        deadline = _t.monotonic() + INLINE_BUDGET_SEC
        remaining = []
        for icao in to_resolve:
            if _t.monotonic() >= deadline:
                remaining.append(icao)
                continue
            try:
                # asyncio.to_thread runs the blocking call in a worker
                # thread, keeping the event loop free for other requests.
                await _asyncio.wait_for(
                    _asyncio.to_thread(_collector_mod.resolve_icao_to_tail, icao),
                    timeout=max(0.1, deadline - _t.monotonic()),
                )
            except _asyncio.TimeoutError:
                # Ran past our deadline — leave to background worker
                remaining.append(icao)
                continue
            # After the call, icao is now in _ICAO_CACHE (positive, negative,
            # or absent if it was a transient error). Read back from cache
            # to get a consistent view.
            if icao in _collector_mod._ICAO_CACHE:
                cached = _collector_mod._ICAO_CACHE[icao]
                result[icao] = cached or ""
            else:
                # Transient error — leave to background retry
                remaining.append(icao)

        # Anything we couldn't resolve inline goes to the background worker.
        # Dedupe against what we've already queued this process lifetime so
        # we don't pile the same ICAO up repeatedly when the frontend retries.
        if remaining:
            with _tail_resolve_seen_lock:
                for icao in remaining:
                    if icao in _tail_resolve_seen:
                        continue
                    try:
                        _tail_resolve_queue.put_nowait(icao)
                        _tail_resolve_seen.add(icao)
                    except _queue.Full:
                        # Very unusual — 5000 queued is ~40 minutes of work
                        # at 2/sec. If we get here, just drop. Next cycle
                        # will try again.
                        logger.warning(f"tail resolve queue full; dropping {icao}")
                        break

        return {"tails": result}

    # --- Tail resolver diagnostics ---
    # Introspection endpoint for debugging why Track links aren't using the
    # configured provider. Returns cache stats, queue state, and worker
    # health so users/operators can see at a glance whether the resolver
    # is working. Intended to be curl-friendly:
    #   curl -s http://HOST:PORT/api/resolve-tail/debug | python3 -m json.tool
    @app.get("/api/resolve-tail/debug")
    async def get_resolve_tail_debug():
        cache = _collector_mod._ICAO_CACHE
        positive = sum(1 for v in cache.values() if v)
        negative = sum(1 for v in cache.values() if not v)
        # Sample entries so we can see what's actually in the cache —
        # helpful for distinguishing 'all negative' from 'all positive'.
        sample_positive = [
            {"icao": k, "reg": v}
            for k, v in list(cache.items())[:5]
            if v
        ]
        sample_negative = [
            k for k, v in list(cache.items())[:100] if not v
        ][:5]
        return {
            "cache_size": len(cache),
            "cache_positive": positive,
            "cache_negative": negative,
            "queue_depth": _tail_resolve_queue.qsize(),
            "queue_seen_lifetime": len(_tail_resolve_seen),
            "worker_alive": _worker_thread.is_alive(),
            "worker_name": _worker_thread.name,
            # Resolver-side stats — exposes whether hexdb.io is actually
            # reachable. If 'attempts' > 0 but 'successes_*' == 0 and
            # 'network_errors' == attempts, hexdb is unreachable from this
            # host (check DNS, firewall, proxy).
            "resolver_stats": dict(_collector_mod._icao_resolver_stats),
            "sample_positive": sample_positive,
            "sample_negative": sample_negative,
        }

    # --- Status (comprehensive component health check) ---
    @app.get("/api/capacity")
    async def get_capacity():
        """v2.50.30: lightweight capacity metrics endpoint. Same data
        the Capacity card on the Status page renders, but without the
        receiver/hexdb probes that /api/status does. Used by the
        Configuration → Retention live-preview line so dragging the
        retention slider doesn't trigger a full system check on every
        change."""
        db_path = CONFIG["data"]["db_file"]
        retention_days = CONFIG.get("retention", {}).get("all_days", 30)
        return {"ok": True, "capacity": _compute_capacity_metrics(db_path, retention_days)}

    @app.get("/api/version")
    async def get_version():
        """v2.87.2: lightweight version endpoint. The Updates page
        polls this during apply-and-restart so the browser can show
        live status (shutting down, restarting, back online) instead
        of the dead "this site can't be reached" page that browsers
        show by default when the service goes down mid-request.

        Tiny payload by design — needs to be fast even when a Pi 4B
        is mid-migration with the service just barely back up. No
        DB access, no config-dependent computation; just the version
        string the server has cached in memory.
        """
        return {"version": _aerodrome_version}

    @app.get("/api/status")
    async def get_status():
        """Comprehensive health check of all Aerodrome components."""
        now = time.time()

        # --- Receiver check ---
        receiver = CONFIG["receiver"]
        receiver_url = f"http://{receiver['ip']}:{receiver['port']}{receiver['path']}"
        receiver_check = {"ok": False, "url": receiver_url, "response_ms": None, "error": None}
        try:
            t0 = time.time()
            r = req.get(receiver_url, timeout=5)
            receiver_check["response_ms"] = int((time.time() - t0) * 1000)
            receiver_check["ok"] = r.status_code == 200
            if not receiver_check["ok"]:
                receiver_check["error"] = f"HTTP {r.status_code}"
        except req.ConnectionError:
            receiver_check["error"] = "Connection refused"
        except req.Timeout:
            receiver_check["error"] = "Timeout after 5s"
        except Exception as e:
            receiver_check["error"] = str(e)

        # --- Hexdb.io resolver check ---
        # Probe hexdb with a known-good hex (a well-known easyJet A319 used
        # as the example in hexdb's own docs — should always return a
        # populated Registration). Accept only 200 as healthy; in v2.40.1
        # and earlier the URL had an extraneous /icao/ segment that caused
        # every request to 404, but the check treated 404 as "service is
        # up" which masked the bug across four releases. Now 404 (or
        # anything non-200) counts as a failure, and we also sanity-check
        # that the response body actually contains a Registration field
        # so a future URL-path change from hexdb would be caught.
        #
        # v2.50.1: cache the probe result with a 30s TTL. When hexdb is
        # unreachable the previous 5s timeout was paid on every status
        # poll — and v2.49.3 made every admin page poll /api/status —
        # so a flaky hexdb made the whole UI feel slow. Caching the
        # "unreachable" verdict for 30s means at most one slow probe
        # per 30s window instead of one per page nav. Pairs with the
        # same-shaped db_stats_cache from v2.49.7. Timeout is also
        # tightened to 2s as defense-in-depth: if the cache somehow
        # fails to take effect (logic bug, exception path), worst-case
        # response time is bounded at 2s instead of 5s.
        # v2.71.0: compute provider_requires_tail FIRST so we can skip
        # the network probe entirely when the resolver isn't in use.
        # Mirrors the gate refreshTails() applies on the frontend — the
        # resolver matters only when the chosen track-link provider
        # needs a registration (FR24 / AirNavRadar / PlaneFinder). For
        # airplanes_live / FlightAware, hexdb is never called at runtime
        # and probing it every 30s wastes a 2s timeout when hexdb is
        # slow AND surfaces a scary error string on the Status card
        # that's misleading (looks like something is broken when actually
        # nothing in this install needs the resolver).
        #
        # Edge case: a user with tail-based watchlist entries on an
        # airplanes_live install does need hexdb for resolve_tail_to_icao,
        # but only at startup/config-reload (not at runtime). Watchlist-
        # tail resolution failures show up in the startup log; they
        # don't need a continuous reachability probe. The probe gate
        # follows the runtime usage signal (track_link provider) rather
        # than the start-time usage signal (watchlist tails).
        try:
            import collector as _collector_mod
            chosen_provider = (CONFIG.get("receiver", {}).get("track_link_provider")
                               or _collector_mod.TRACK_LINK_FALLBACK)
            provider_cfg = _collector_mod.TRACK_LINK_PROVIDERS.get(chosen_provider) or {}
            provider_requires_tail = bool(provider_cfg.get("reg_required"))
            provider_in_use = chosen_provider
            provider_label = provider_cfg.get("label") or chosen_provider
        except Exception as e:
            logger.debug(f"track provider lookup failed in /api/status: {e}")
            # Defensive default: if we can't read the provider config,
            # treat as "needs probe" so we don't accidentally hide a
            # real hexdb outage from a user whose config is malformed.
            provider_requires_tail = True
            provider_in_use = None
            provider_label = None

        hexdb_cache = nonlocal_state.get("hexdb_probe_cache")
        if not provider_requires_tail:
            # v2.71.0: skip the network probe. Synthesize an "ok + note"
            # response that the frontend's existing "Not in use" branch
            # already handles correctly via provider_requires_tail=False.
            # Setting ok=True (rather than passing through a stale cached
            # error) means the gear-icon overall-status banner won't
            # flag this as an advisory issue when nothing is actually
            # wrong. response_ms=None and error=None keep the card
            # visually clean.
            hexdb_check = {
                "ok": True, "response_ms": None, "error": None,
                "cache_stats": None,
                "probe_cached_age_sec": 0,
                "probe_skipped": True,
            }
        elif hexdb_cache and (now - hexdb_cache.get("timestamp", 0)) < HEXDB_PROBE_CACHE_TTL_SEC:
            hexdb_check = dict(hexdb_cache["data"])
            hexdb_check["probe_cached_age_sec"] = int(now) - hexdb_cache["timestamp"]
        else:
            hexdb_check = {"ok": False, "response_ms": None, "error": None,
                           "cache_stats": None}
            try:
                t0 = time.time()
                r = req.get("https://hexdb.io/api/v1/aircraft/4010EE", timeout=2)
                hexdb_check["response_ms"] = int((time.time() - t0) * 1000)
                if r.status_code == 200:
                    try:
                        data = r.json()
                        if isinstance(data, dict) and data.get("Registration"):
                            hexdb_check["ok"] = True
                        else:
                            hexdb_check["error"] = "200 OK but no Registration field in response"
                    except Exception as e:
                        hexdb_check["error"] = f"200 OK but response wasn't JSON: {e}"
                else:
                    hexdb_check["error"] = f"HTTP {r.status_code}"
            except Exception as e:
                hexdb_check["error"] = str(e)
            # Cache the probe result. Note: we cache the dict before
            # attaching cache_stats / provider_in_use below — those are
            # cheap local lookups that should always be fresh, not cached.
            nonlocal_state["hexdb_probe_cache"] = {
                "data": dict(hexdb_check), "timestamp": int(now)
            }
            hexdb_check["probe_cached_age_sec"] = 0

        # v2.49.0: attach persistent-cache stats. Uses the module-level db_path
        # set at startup, not this function's CONFIG lookup — they're the same
        # in practice but collector owns its own reference. Safe to call even
        # if set_db_path hasn't run (returns {db_available: false}).
        try:
            import collector as _collector_mod
            hexdb_check["cache_stats"] = _collector_mod.hexdb_cache_stats()
        except Exception as e:
            logger.debug(f"hexdb_cache_stats failed in /api/status: {e}")
            hexdb_check["cache_stats"] = None

        # v2.49.1: provider context (label / requires_tail) so the
        # frontend can show "Not in use — <provider> doesn't need
        # tail-number lookup" instead of leaving the user wondering
        # about an empty cache.
        # v2.71.0: provider lookup moved to the top of this block so
        # the same value can gate the network probe above. Just attach
        # the precomputed values here.
        hexdb_check["provider_in_use"] = provider_in_use
        hexdb_check["provider_label"] = provider_label
        hexdb_check["provider_requires_tail"] = (
            provider_requires_tail if provider_in_use is not None else None
        )

        # --- Database check ---
        db_path = CONFIG["data"]["db_file"]
        db_check = {"ok": False, "path": db_path, "size_mb": None, "error": None, "stats": {}}
        if os.path.exists(db_path):
            try:
                db_check["size_mb"] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
                conn = _open_db_conn(db_path)
                now_ts = int(time.time())

                # v2.49.7: cache the per-table count queries. At scale
                # (millions of rows in all_sightings) these COUNT(DISTINCT
                # icao) and COUNT(*) queries cost several seconds each —
                # multiply by three tables and /api/status takes 8-15s
                # to return, which the browser shows as "Load Failed"
                # before eventually rendering. Cache TTL of 30s matches
                # the health-indicator polling cadence so post-warmup
                # the polling never hits the slow path.
                cache = nonlocal_state.get("db_stats_cache")
                if cache and (now_ts - cache.get("timestamp", 0)) < DB_STATS_CACHE_TTL_SEC:
                    db_check["stats"] = cache["data"]
                    db_check["stats_cached_age_sec"] = now_ts - cache["timestamp"]
                else:
                    fresh_stats = {}
                    # Military and watchlist stay on raw — small tables and
                    # the v2.50.17 covering indexes (idx_mil_seen_icao,
                    # idx_watch_seen_icao) made these queries fast (sub-60ms
                    # on 8M-row install).
                    for table, key, days_key in [
                        ("military_sightings", "military", "military_days"),
                        ("watchlist_sightings", "watchlist", "watchlist_days"),
                    ]:
                        cutoff = now_ts - (CONFIG["retention"][days_key] * 86400)
                        unique = conn.execute(
                            f"SELECT COUNT(DISTINCT icao) FROM {table} WHERE seen_at >= ?", (cutoff,)
                        ).fetchone()[0]
                        total = conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE seen_at >= ?", (cutoff,)
                        ).fetchone()[0]
                        fresh_stats[key] = {"unique": unique, "total": total}

                    # v2.50.19: all_sightings stats migrate to the hourly
                    # rollup. Why this caller, why now: every admin page
                    # (gear menu items — Status, Logs, Documentation,
                    # Configuration, Updates) polls /api/status on load
                    # for the header health indicator. With raw queries
                    # over 8M+ rows the COUNT(DISTINCT icao) ran 9.5 sec
                    # (per the v2.50.17 perf-diag from the reference Pi
                    # install). Combined with the 30s cache TTL, that
                    # meant every gear-menu navigation outside the cache
                    # window paid a multi-second cold-path penalty,
                    # making admin pages feel sluggish even though the
                    # Live/Watchlist/Military tabs (which switch
                    # client-side without HTTP) felt snappy. Symptom
                    # report from Pi user: "gear navigation is slow,
                    # rest of Live tabs etc are faster."
                    #
                    # Same migration pattern as v2.50.0 for /api/all:
                    # COUNT(DISTINCT icao) → over hour_bucket range
                    # served by idx_hourly_bucket_icao covering index
                    # (the all_tab_count_rollup probe shows 162ms vs
                    # 9.5sec raw on the same hardware — ~57x). The
                    # COUNT(*) becomes SUM(sighting_count): the rollup's
                    # sighting_count is incremented per raw insert
                    # (collector.py _upsert_hourly_rollup line 1999),
                    # so summing across the bucket window is exactly
                    # the raw row count.
                    #
                    # Boundary semantics shift slightly: hour_bucket is
                    # cutoff_ts // 3600, so we may include up to 59 min
                    # of pre-cutoff data inside the cutoff hour. For a
                    # 30-day retention window that's 0.14% — invisible
                    # in a status-card display. The military/watchlist
                    # paths above are unchanged because retention
                    # boundaries on those small tables matter more
                    # (shorter retention windows in practice) and they
                    # weren't slow.
                    all_cutoff_ts = now_ts - (CONFIG["retention"]["all_days"] * 86400)
                    all_cutoff_bucket = all_cutoff_ts // 3600
                    all_unique = conn.execute(
                        "SELECT COUNT(DISTINCT icao) FROM sightings_hourly WHERE hour_bucket >= ?",
                        (all_cutoff_bucket,)
                    ).fetchone()[0]
                    all_total_row = conn.execute(
                        "SELECT COALESCE(SUM(sighting_count), 0) FROM sightings_hourly WHERE hour_bucket >= ?",
                        (all_cutoff_bucket,)
                    ).fetchone()
                    all_total = int(all_total_row[0]) if all_total_row else 0
                    fresh_stats["all"] = {"unique": all_unique, "total": all_total}

                    nonlocal_state["db_stats_cache"] = {"data": fresh_stats, "timestamp": now_ts}
                    db_check["stats"] = fresh_stats
                    db_check["stats_cached_age_sec"] = 0
                conn.close()
                db_check["ok"] = True
                # v2.50.30: capacity metrics for the new Status page card
                # and the live Configuration → Retention preview line.
                # Same caching shape as db_stats_cache — capacity values
                # don't shift second-to-second, recomputing every poll
                # would be wasteful. TTL of 30s matches db_stats_cache.
                cap_cache = nonlocal_state.get("capacity_cache")
                if cap_cache and (now_ts - cap_cache.get("timestamp", 0)) < DB_STATS_CACHE_TTL_SEC:
                    db_check["capacity"] = cap_cache["data"]
                else:
                    retention_days = CONFIG.get("retention", {}).get("all_days", 30)
                    cap = _compute_capacity_metrics(db_path, retention_days)
                    nonlocal_state["capacity_cache"] = {"data": cap, "timestamp": now_ts}
                    db_check["capacity"] = cap
            except Exception as e:
                db_check["error"] = str(e)
        else:
            db_check["error"] = "Database file does not exist (will be created on first poll)"

        # --- Collector check ---
        # Collector health is inferred from recent writes to all_sightings.
        # v2.42.14: also computes live records/sec over the last 5 minutes.
        # This metric used to live on the Stats tab as the 'messages_rate'
        # card. It's a health signal (is the receiver still producing data
        # at the expected rate?) so it belongs alongside the other health
        # signals, not scattered across the Stats view.
        collector_check = {
            "ok": False,
            "last_write_seconds_ago": None,
            "records_per_sec_5m": None,
            "records_sample_count_5m": None,
            "error": None,
        }
        if db_check["ok"]:
            try:
                conn = _open_db_conn(db_path)
                row = conn.execute("SELECT MAX(seen_at) FROM all_sightings").fetchone()
                if row and row[0]:
                    last_write = int(row[0])
                    age = int(time.time()) - last_write
                    collector_check["last_write_seconds_ago"] = age
                    # Healthy if we've written within 3x the poll interval
                    threshold = CONFIG["receiver"]["poll_interval"] * 3
                    collector_check["ok"] = age <= threshold
                    if not collector_check["ok"]:
                        collector_check["error"] = f"No writes in {age}s (threshold {threshold}s)"
                else:
                    collector_check["error"] = "No data written yet"
                # 5-minute throughput. Cheap query (uses idx_all_seen,
                # scans only the last 5 min of rows).
                try:
                    window_sec = 300
                    window_start = int(time.time()) - window_sec
                    n_row = conn.execute(
                        "SELECT COUNT(*) FROM all_sightings WHERE seen_at >= ?",
                        (window_start,)
                    ).fetchone()
                    n = int(n_row[0]) if n_row and n_row[0] is not None else 0
                    collector_check["records_sample_count_5m"] = n
                    collector_check["records_per_sec_5m"] = round(n / window_sec, 2)
                except Exception as e:
                    # Non-fatal — main collector check can still pass
                    logger.warning(f"Could not compute records/sec: {e}")
                conn.close()
            except Exception as e:
                collector_check["error"] = str(e)

        # --- Web server check ---
        # If this endpoint responded, the server is up
        webserver_check = {"ok": True, "host": CONFIG["web"]["host"], "port": CONFIG["web"]["port"]}

        # --- System info ---
        system_info = _get_system_info()

        # --- Version info ---
        version_file = Path(__file__).parent / "VERSION"
        version = version_file.read_text().strip() if version_file.exists() else "unknown"

        # --- Severity ---
        # 'ok'    — everything healthy
        # 'warn'  — only advisory components (hexdb_resolver, capacity) have issues
        # 'error' — at least one core component is failing
        core_ok = all([
            receiver_check["ok"],
            db_check["ok"],
            collector_check["ok"],
            webserver_check["ok"],
        ])
        advisory_ok = hexdb_check["ok"]

        # v2.50.31: capacity is a warn-level contributor. We read the
        # alert state directly from the collector's module-level state
        # rather than re-evaluating here, because the collector's poll
        # loop is the one source of truth for the state machine — re-
        # computing in the API path could disagree with what the
        # notifier saw on its last evaluation, leading to a moment
        # where the gear icon says "ok" while a notification has
        # already fired. Importing collector here is OK because main.py
        # has already imported it by the time the app boots.
        capacity_ok = True
        try:
            import collector
            if collector._capacity_state.get("alert_active"):
                capacity_ok = False
        except Exception:
            # If we can't read the state, default to OK — the alert
            # itself, if it ever fired, will have notified separately.
            pass
        if not core_ok:
            severity = "error"
        elif not advisory_ok or not capacity_ok:
            severity = "warn"
        else:
            severity = "ok"

        # Collect short names of failing components for display
        failing = []
        for name, chk in [
            ("receiver", receiver_check), ("database", db_check),
            ("collector", collector_check), ("webserver", webserver_check),
            ("hexdb_resolver", hexdb_check),
        ]:
            if not chk["ok"]:
                failing.append(name)
        if not capacity_ok:
            failing.append("capacity")

        # v3.0.0: GitHub update availability. The gear-menu badge driver
        # (static/health-indicator.js) reads this and adds 'warn' to the
        # gear button when an update is available — same amber dot used
        # for sudoers drift and capacity issues. update_available is
        # purely informational here; the /updates page renders the full
        # state (5 distinct UI states) via /api/updates/github/check.
        try:
            _upd_cfg = _updates_config()
            _upd_state = _get_update_state()
            _upd_latest = _upd_state["last_known_latest"]
            _upd_available = bool(
                _upd_cfg["enabled"]
                and _upd_cfg["notify_gear_badge"]
                and _upd_latest
                and _is_newer_version(_upd_latest, _get_running_version())
            )
        except Exception:
            _upd_available = False

        return {
            "overall_ok": core_ok,
            "severity": severity,
            "failing": failing,
            "timestamp": int(now),
            "version": version,
            "update_available": _upd_available,
            "components": {
                "collector": collector_check,
                "webserver": webserver_check,
                "database": db_check,
                "receiver": receiver_check,
                "hexdb_resolver": hexdb_check,
            },
            "system": system_info,
            "retention": CONFIG["retention"],
            # v2.41.2: include cached sudoers drift state so the header
            # badge can reflect it without a separate API call. May be
            # None if the startup check hasn't populated yet.
            "sudoers": nonlocal_state.get("last_sudoers_check"),
            # v2.50.0: hourly-rollup backfill state. The All tab UI
            # uses this to show a "migration in progress" banner during
            # first-boot of v2.50.0 on installs with existing all_sightings
            # data. Phase: "complete" | "running" | "error" | "unknown".
            "hourly_rollup": (lambda: (
                __import__("collector").get_hourly_backfill_status()
            ))(),
        }

    # --- CSV Export (all sightings for a given tab) ---
    # v2.67.0 (Phase 1D): the 'all' tab branch was removed alongside the
    # All tab. Watchlist and Military are the remaining tabs that route
    # through this endpoint. Search results have their own export path
    # (/api/search?... + frontend CSV stitching) which is independent.
    @app.get("/api/export")
    async def export_csv(
        tab: str = Query(..., description="'watchlist' or 'military'"),
        from_ts: Optional[int] = Query(None),
        to_ts: Optional[int] = Query(None),
        search: Optional[str] = Query(None),
    ):
        from fastapi.responses import StreamingResponse
        import csv, io

        db_path = CONFIG["data"]["db_file"]
        now = int(time.time())

        if tab == "watchlist":
            table = "watchlist_sightings"
            columns = ["icao", "callsign", "watchlist_label", "aircraft_type", "type_desc",
                       "speed", "altitude", "lat", "lon", "seen_at"]
            days = CONFIG["retention"]["watchlist_days"]
            default_cutoff = now - (days * 86400)
        elif tab == "military":
            table = "military_sightings"
            columns = ["icao", "callsign", "special_label", "aircraft_type", "type_desc",
                       "speed", "altitude", "lat", "lon", "seen_at"]
            days = CONFIG["retention"]["military_days"]
            default_cutoff = now - (days * 86400)
        else:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid tab: {tab}. Must be watchlist or military."}
            )

        # Apply filters (custom range + optional search box)
        cutoff_from = from_ts if from_ts is not None else default_cutoff
        cutoff_to = to_ts if to_ts is not None else now

        where = "seen_at >= ? AND seen_at <= ?"
        params = [cutoff_from, cutoff_to]

        if search:
            pattern = f"%{search.upper()}%"
            where += (" AND (UPPER(icao) LIKE ? OR UPPER(callsign) LIKE ?"
                      " OR UPPER(aircraft_type) LIKE ? OR UPPER(type_desc) LIKE ?)")
            params.extend([pattern, pattern, pattern, pattern])

        query = f"SELECT {', '.join(columns)} FROM {table} WHERE {where} ORDER BY seen_at DESC"

        def generate():
            # CSV header — map internal column names to friendlier labels
            header_map = {
                "icao": "icao",
                "callsign": "callsign",
                "watchlist_label": "watchlist_label",
                "special_label": "special_label",
                "aircraft_type": "aircraft_type",
                "type_desc": "type_desc",
                "speed": "speed_kt",
                "altitude": "altitude_ft",
                "lat": "lat",
                "lon": "lon",
                "seen_at": "seen_at_utc",
            }
            unit = (CONFIG.get("receiver", {}).get("distance_unit") or "mi").lower()
            distance_header = f"distance_{unit}"
            headers = [header_map.get(c, c) for c in columns] + [distance_header]
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(headers)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

            conn = _open_db_conn(db_path)
            try:
                lat_idx = columns.index("lat")
                lon_idx = columns.index("lon")
                cursor = conn.execute(query, params)
                for row in cursor:
                    row_list = list(row)
                    # Convert seen_at (unix) to ISO 8601 string
                    seen_at_idx = columns.index("seen_at")
                    if row_list[seen_at_idx]:
                        t = time.gmtime(int(row_list[seen_at_idx]))
                        row_list[seen_at_idx] = time.strftime("%Y-%m-%d %H:%M:%S", t)
                    # Append computed distance (may be None)
                    dist = _distance_from_receiver(row_list[lat_idx], row_list[lon_idx])
                    row_list.append("" if dist is None else dist)
                    writer.writerow(row_list)
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
            finally:
                conn.close()

        filename = f"aerodrome-{tab}-all-{time.strftime('%Y%m%d-%H%M')}.csv"
        return StreamingResponse(
            generate(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    # --- UI config (what the frontend needs to know about settings) ---
    @app.get("/api/ui-config")
    async def get_ui_config():
        r = CONFIG.get("receiver", {})
        has_location = r.get("latitude") is not None and r.get("longitude") is not None
        mil = CONFIG.get("military", {})
        # Watchlist alerts — pass through with sensible defaults
        wa = CONFIG.get("watchlist_alerts") or {}
        # Stats — pass the enabled flag, refresh interval, and which cards to show
        st = CONFIG.get("stats") or {}
        st_cards = st.get("cards") or {}
        # v2.44.1: expose the track-link provider registry so the frontend
        # renders Track URLs from the same source of truth the notifier
        # uses. Before this release, templates/index.html maintained its
        # own TRACK_LINK_URLS dict that had to be kept in sync manually
        # with collector.py's _build_track_url. Now collector.py's
        # TRACK_LINK_PROVIDERS is the sole canonical registry.
        from collector import TRACK_LINK_PROVIDERS, TRACK_LINK_FALLBACK
        # v2.85.11: surface display.time_format so the frontend's shared
        # time formatter (static/timefmt.js) knows whether to render
        # times in 12h, 24h, or browser-locale-determined ("auto") form.
        # date_format is also available here, though most code paths
        # currently read it from the search-suggest endpoint instead.
        display_cfg = CONFIG.get("display") or {}
        return {
            "distance_enabled": has_location,
            "distance_unit": (r.get("distance_unit") or "mi").lower(),
            # Which external tracker Track ↗ links point to. The frontend's
            # trackLink() helper renders track_link_providers[chosen].url
            # with placeholder substitution. Unknown or missing chosen →
            # the frontend falls back to track_link_fallback.
            "track_link_provider": (r.get("track_link_provider") or TRACK_LINK_FALLBACK),
            "track_link_providers": TRACK_LINK_PROVIDERS,
            "track_link_fallback": TRACK_LINK_FALLBACK,
            "default_military_color": mil.get("default_color", "#ef4444"),
            "watchlist_alerts": {
                "enabled": bool(wa.get("enabled", True)),
                # v2.50.23: 'new' was historically a separate trigger option but
                # its implementation in the frontend collapsed to be identical
                # to 'continuous_dismissable' (both used !isDismissed). To
                # eliminate user-facing duplication without breaking existing
                # configs, the API translates 'new' to 'continuous_dismissable'
                # here so the frontend only ever sees canonical values.
                # Existing config.yaml files keep working unchanged; if the
                # user re-saves config from the UI, the canonical form is
                # written back automatically.
                "trigger": (lambda t: "continuous_dismissable" if t == "new" else t)(
                    wa.get("trigger", "continuous_dismissable")
                ),
                "effect": wa.get("effect", "pulse_dot"),
                "color": wa.get("color", "#f59e0b"),
            },
            "stats": {
                "enabled": bool(st.get("enabled", True)),
                "refresh_interval": int(st.get("refresh_interval", 300) or 0),
                "timezone": st.get("timezone") or "",
                "cards": {k: bool(v) for k, v in st_cards.items()},
                "groups": {k: bool(v) for k, v in (st.get("groups") or {}).items()},
                "new_record_alerts": {
                    "enabled": bool((st.get("new_record_alerts") or {}).get("enabled", True)),
                    "color": (st.get("new_record_alerts") or {}).get("color", "#22c55e"),
                    "dismiss_after_seconds": int((st.get("new_record_alerts") or {}).get("dismiss_after_seconds", 30)),
                },
            },
            "display": {
                "date_format": display_cfg.get("date_format", "MDY"),
                "time_format": display_cfg.get("time_format", "auto"),
            },
        }

    # =========================================================================
    # /api/stats — today-stats for the Stats tab
    # =========================================================================
    def _day_bounds_ts():
        """Return (start_ts, end_ts) for 'today' in the configured timezone as
        Unix timestamps. start_ts = local midnight. end_ts = now."""
        import time as _time
        from datetime import datetime, timedelta
        st = CONFIG.get("stats") or {}
        tz_name = (st.get("timezone") or "").strip()
        tz = None
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = None  # fall back to system tz
        now = datetime.now(tz) if tz else datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(now.timestamp())

    # Canonical mapping of stat cards to their category/group. Used to:
    # (1) suppress cards whose parent group is disabled (backend)
    # (2) render collapsible sections with the right ordering (frontend)
    # Order matters — sections appear in this order on the Stats page.
    CARD_GROUPS = [
        ("today",       "Today", [
            "unique_today", "peak_simultaneous", "average_concurrent",
            "military_today", "watchlist_hits", "first_last_contact"
        ]),
        ("extremes",    "Today's extremes", [
            "furthest", "highest_altitude", "lowest_altitude",
            "fastest", "slowest", "longest_track"
        ]),
        ("composition", "Composition", [
            "top_types", "top_operators", "military_branches", "category_mix",
            "top_countries"
        ]),
        ("patterns",    "Patterns", [
            "hourly_histogram"
        ]),
        ("history",     "History", [
            "first_time_seen", "daily_counts_7d", "watchlist_frequency"
        ]),
        ("records",     "All-time records", [
            "all_time_records"
        ]),
        ("coverage",    "Coverage", [
            "range_rose", "distance_histogram"
        ]),
    ]
    # Flat lookup: card_name → group_name
    _CARD_TO_GROUP = {c: g for g, _, cs in CARD_GROUPS for c in cs}

    def _group_enabled(groups, group_name):
        """A group is enabled if config says so or if unspecified (default on)."""
        return bool(groups.get(group_name, True))

    def _card_enabled(cards, name, groups=None):
        """A card is shown if:
          - its individual card toggle is on (default True), AND
          - its parent group is enabled (default True)
        Groups act as a master switch: disabling a group hides all its cards
        regardless of individual card toggles."""
        if not bool(cards.get(name, True)):
            return False
        if groups is not None:
            group = _CARD_TO_GROUP.get(name)
            if group and not _group_enabled(groups, group):
                return False
        return True

    @app.get("/api/stats")
    async def get_stats():
        import sqlite3
        st = CONFIG.get("stats") or {}
        if not st.get("enabled", True):
            return {"enabled": False, "cards": {}}

        cards = st.get("cards") or {}
        groups = st.get("groups") or {}
        start_ts, end_ts = _day_bounds_ts()

        db_path = CONFIG.get("data", {}).get("db_file", "aircraft_history.db")
        if not Path(db_path).is_absolute():
            db_path = str(Path(__file__).parent / db_path)

        # Build a list of enabled groups (in canonical order) so the frontend
        # knows which sections to render and in what order. Cards within a
        # group are filtered down to only those that actually have data
        # (populated in result["cards"] below).
        # Build the enabled groups list, then reorder per user's saved
        # groups_order if set. Unknown ids in groups_order are ignored;
        # any enabled groups not in groups_order get appended in canonical order.
        enabled_groups = [
            {"id": gid, "label": glabel, "cards": cs}
            for gid, glabel, cs in CARD_GROUPS
            if _group_enabled(groups, gid)
        ]
        user_order = st.get("groups_order") or []
        if isinstance(user_order, list) and user_order:
            by_id = {g["id"]: g for g in enabled_groups}
            ordered = []
            seen = set()
            for gid in user_order:
                if isinstance(gid, str) and gid in by_id and gid not in seen:
                    ordered.append(by_id[gid])
                    seen.add(gid)
            # Append any enabled groups not covered by user_order, in canonical order
            for g in enabled_groups:
                if g["id"] not in seen:
                    ordered.append(g)
            enabled_groups = ordered

        result = {
            "enabled": True,
            "day_start_ts": start_ts,
            "now_ts": end_ts,
            "timezone": (st.get("timezone") or "").strip() or "(system)",
            "groups": enabled_groups,
            "cards": {},
        }

        try:
            conn = _open_db_conn(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # v2.42.8: per-query timing instrumentation for the Stats endpoint.
            # A user reported Stats tab loading times in the MINUTES range on
            # a Pi with 3M rows. The endpoint runs ~30+ queries across enabled
            # cards, and without timing data we can't tell which one is the
            # bottleneck. Each q() call below records (label, ms). The full
            # list is returned in result["_query_timings"] for clients, and
            # any query exceeding SLOW_QUERY_MS gets a WARNING log line so
            # operators can grep journalctl.
            #
            # Implementation: _current_card is set by the card-block code via
            # a simple context variable. q() reads it on each call so every
            # query inside an `if card_check("X"):` block
            # auto-attributes to card "X" without needing explicit labels on
            # every call. Falls back to a SQL-prefix label if no current
            # card is set.
            #
            # Note: timings include both SQL execution AND row fetch. That
            # matches what the user actually waits for. We don't separate
            # the two \u2014 if a query returns 100k rows, the fetch time IS
            # part of the problem.
            import time as _stats_t
            _query_timings = []
            _current_card = [None]  # list-wrapped for closure mutation
            SLOW_QUERY_MS = 500

            def q(sql, params=(), label=None):
                t0 = _stats_t.time()
                cur.execute(sql, params)
                rows = cur.fetchall()
                ms = (_stats_t.time() - t0) * 1000
                if label is None:
                    if _current_card[0]:
                        label = _current_card[0]
                    else:
                        label = "(pre-cards) " + " ".join(sql.split())[:60]
                _query_timings.append({"label": label, "ms": round(ms, 1)})
                if ms > SLOW_QUERY_MS:
                    logger.warning(
                        f"Stats slow query: {label} took {ms:.0f}ms "
                        f"(rows returned: {len(rows)})"
                    )
                    # v2.84.0: also feed the in-memory ring buffer so the
                    # diagnostics UI surfaces this without requiring file
                    # log access. Plan capture: best-effort, same
                    # connection — silent on error so a diagnostic-side
                    # failure can never break a Stats response.
                    try:
                        from slow_query_log import record_slow_query
                        plan = None
                        try:
                            plan_rows = cur.execute(
                                "EXPLAIN QUERY PLAN " + sql, params
                            ).fetchall()
                            plan = [
                                row[3] if len(row) >= 4 else str(row)
                                for row in plan_rows
                            ]
                        except Exception:
                            pass
                        record_slow_query(
                            endpoint="/api/stats",
                            label=label,
                            duration_ms=ms,
                            sql=sql,
                            params=params,
                            rows_returned=len(rows),
                            plan=plan,
                        )
                    except Exception:
                        pass
                return rows

            # Wraps _card_enabled and also sets _current_card as a side effect.
            # Intentional: every `if card_check("X"):` block attributes its
            # q() calls to card "X" for timing purposes, without having to
            # edit 28 card blocks to pass an explicit label.
            def card_check(card_name):
                enabled = _card_enabled(cards, card_name, groups)
                if enabled:
                    _current_card[0] = card_name
                return enabled

            # --- Volume ---
            if card_check("unique_today"):
                # v2.86.2: rewritten to query sightings_hourly rollup
                # (one row per icao+hour) instead of all_sightings (one
                # row per poll-tick observation). Same answer — both
                # tables have a row for every (icao, hour) the aircraft
                # was seen — but sightings_hourly is ~70× smaller, so
                # the COUNT(DISTINCT icao) scan finishes in single-digit
                # ms instead of single-digit seconds. Confirmed via the
                # `unique_aircraft_count_rollup` performance probe
                # (47.8ms over 365 days; today is one day, so much
                # faster).
                #
                # v2.42.7 history: the previous all_sightings query used
                # an INDEXED BY hint to defend against planner mis-choice
                # (idx_all_seen_icao vs idx_all_icao made a 2000× speed
                # difference on busy installs). The rollup-table version
                # is small enough that planner choice doesn't matter
                # — sightings_hourly fits in cache trivially.
                row = q("SELECT COUNT(DISTINCT icao) AS n FROM sightings_hourly "
                        "WHERE hour_bucket >= ?", (start_ts,))
                result["cards"]["unique_today"] = row[0]["n"] if row else 0

            if card_check("peak_simultaneous"):
                # v2.87.0: rewritten to read from concurrent_minute rollup
                # (one row per 60-second bucket, populated by the
                # collector on every poll). Drops from ~1.5s to
                # sub-millisecond on busy installs because we're MAX-ing
                # over ~1440 rows/day instead of GROUP BY-ing several
                # hundred thousand all_sightings rows.
                #
                # Semantic note: the old query computed COUNT(DISTINCT
                # icao) per 60s bucket — the *union* of aircraft seen
                # at any sub-poll within the bucket. The new rollup
                # stores the *maximum* count seen at any single
                # sub-poll. For default 60s poll cadence these are
                # identical; for sub-60s cadences the new metric is
                # arguably more meaningful ("largest number of
                # aircraft visible at the same instant"). See the v4
                # migration notes for the full discussion.
                #
                # v2.42.7 history: previous query used INDEXED BY
                # idx_all_seen_icao to defend against planner mis-
                # choice (saved a 20-second regression). The new
                # rollup table is small enough that planner choice
                # doesn't matter.
                rows = q("""
                    SELECT MAX(count) AS peak FROM concurrent_minute
                    WHERE minute_bucket >= ?
                """, (start_ts,))
                result["cards"]["peak_simultaneous"] = (rows[0]["peak"] or 0) if rows else 0

            if card_check("average_concurrent"):
                # v2.87.0: rewritten to use concurrent_minute. Same
                # rationale as peak_simultaneous above. AVG over the
                # rollup is the average per-minute concurrent count
                # across today's buckets.
                rows = q("""
                    SELECT AVG(count) AS avg_cnt FROM concurrent_minute
                    WHERE minute_bucket >= ?
                """, (start_ts,))
                val = rows[0]["avg_cnt"] if rows and rows[0]["avg_cnt"] is not None else 0
                result["cards"]["average_concurrent"] = round(val, 1)

            if card_check("military_today"):
                row = q("SELECT COUNT(DISTINCT icao) AS n FROM military_sightings WHERE seen_at >= ?", (start_ts,))
                result["cards"]["military_today"] = row[0]["n"] if row else 0

            if card_check("watchlist_hits"):
                row = q("SELECT COUNT(DISTINCT icao) AS n FROM watchlist_sightings WHERE seen_at >= ?", (start_ts,))
                result["cards"]["watchlist_hits"] = row[0]["n"] if row else 0

            if card_check("first_last_contact"):
                first = q("SELECT icao, callsign, seen_at FROM all_sightings WHERE seen_at >= ? ORDER BY seen_at ASC LIMIT 1", (start_ts,))
                last = q("SELECT icao, callsign, seen_at FROM all_sightings WHERE seen_at >= ? ORDER BY seen_at DESC LIMIT 1", (start_ts,))
                result["cards"]["first_last_contact"] = {
                    "first": dict(first[0]) if first else None,
                    "last": dict(last[0]) if last else None,
                }

            # --- Extremes ---
            # For distance, we need lat/lon and receiver location
            r = CONFIG.get("receiver", {})
            rx_lat, rx_lon = r.get("latitude"), r.get("longitude")
            have_location = rx_lat is not None and rx_lon is not None
            distance_unit = (r.get("distance_unit") or "mi").lower()

            # v2.79.0: local haversine alias preserved so the existing
            # call sites in this scope (lines below) keep their original
            # call shape. distance.haversine is the single math home.
            haversine = _dist_haversine

            if card_check("furthest"):
                if have_location:
                    # v2.86.4: rewritten to read from seen_aircraft
                    # instead of scanning all_sightings. seen_aircraft
                    # has a `last_distance` column (km, populated by
                    # the collector on every poll) that holds the
                    # distance from receiver to each aircraft's most
                    # recent observed position. ORDER BY last_distance
                    # DESC LIMIT 1 finds today's furthest aircraft in
                    # ~13ms on busy installs vs the ~3100ms the
                    # all_sightings scan was taking on a 1-CPU VM.
                    #
                    # Semantic note: this changes "furthest today" from
                    # "the aircraft that flew furthest from the receiver
                    # at any point during today" (true historical max
                    # across every observation) to "the aircraft whose
                    # most-recent observation today was furthest" (last-
                    # position only). For most users these produce the
                    # same answer; they diverge only when an aircraft
                    # both went far and then came close before being
                    # lost. The casual "Furthest aircraft today" framing
                    # on the Stats card naturally reads as "where it
                    # was last seen" so the new semantic matches user
                    # intuition. Power users wanting true-max can wait
                    # for a future schema change tracking per-aircraft
                    # daily max distance.
                    #
                    # v2.42.9 history (preserved for context): the
                    # previous all_sightings approach used a SQL pre-
                    # rank by squared-coord-proxy + Python haversine on
                    # the top 50 to avoid pulling 500K+ rows to Python
                    # on a busy-airspace Pi. The new approach skips the
                    # pre-rank entirely because last_distance is already
                    # computed and indexed.
                    rows = q("""
                        SELECT icao, last_callsign AS callsign,
                               last_lat AS lat, last_lon AS lon,
                               aircraft_type, last_distance
                        FROM seen_aircraft
                        WHERE last_seen_at >= ?
                          AND last_distance IS NOT NULL
                        ORDER BY last_distance DESC
                        LIMIT 1
                    """, (start_ts,))
                    if rows:
                        row = rows[0]
                        # last_distance is stored in km; convert to the
                        # user's preferred unit. _dist_to_user_unit is
                        # the canonical converter shared with Search and
                        # Live tab so all distances display consistently.
                        d = _dist_to_user_unit(row["last_distance"], distance_unit)
                        bearing = _dist_compass_bearing(
                            rx_lat, rx_lon, row["lat"], row["lon"]
                        )
                        # Note: altitude was previously included in this
                        # result dict (the v2.42.9 all_sightings query
                        # selected the per-sighting altitude), but the
                        # frontend's furthestCard renderer doesn't use
                        # it — only icao, callsign, aircraft_type,
                        # distance, bearing are displayed. seen_aircraft
                        # doesn't store last_altitude, so the v2.86.4
                        # rewrite drops it from the response. If a
                        # future card change wants altitude back, the
                        # source would be sightings_hourly.last_altitude
                        # for the matching (icao, current hour) row.
                        result["cards"]["furthest"] = {
                            "icao": row["icao"],
                            "callsign": row["callsign"],
                            "distance": round(d, 1) if d is not None else None,
                            "unit": distance_unit,
                            "bearing": round(bearing, 0),
                            "aircraft_type": row["aircraft_type"],
                        }
                    else:
                        result["cards"]["furthest"] = None
                else:
                    result["cards"]["furthest"] = None  # no receiver location configured

            if card_check("highest_altitude"):
                # v2.86.6: rewritten to use sightings_hourly.max_altitude
                # (the per-aircraft max altitude observed in each hour
                # bucket, populated by the collector). Taking MAX across
                # all today's hours = global max altitude any aircraft
                # reached today — same answer as the all_sightings scan
                # but on a ~70× smaller table.
                #
                # The typeof() guard is preserved even though sightings_
                # hourly.max_altitude is REAL — defensive, costs nothing,
                # and matches the v2.42.x lesson that SQLite happily
                # stores stringy values like "ground" in REAL columns
                # if the upstream feeder gets weird. Same for the ~hex
                # icao exclusion (TIS-B/MLAT pseudo-targets often have
                # ATC-relayed altitude readings that are less reliable).
                rows = q("""
                    SELECT icao, callsign, max_altitude AS altitude, aircraft_type
                    FROM sightings_hourly
                    WHERE hour_bucket >= ? AND max_altitude IS NOT NULL
                      AND typeof(max_altitude) IN ('integer', 'real')
                      AND icao NOT LIKE '~%'
                    ORDER BY max_altitude DESC LIMIT 1
                """, (start_ts,))
                result["cards"]["highest_altitude"] = dict(rows[0]) if rows else None

            if card_check("lowest_altitude"):
                # v2.87.1: rewritten to read from sightings_hourly's
                # new min_nonzero_altitude column (added by migration
                # v5, populated by the collector on every poll). Drops
                # from ~1.9s to single-digit ms — same fix shape as
                # highest_altitude but using a new column rather than
                # an existing one because of the v2.86.6 correctness
                # gotcha. See migration v5 docstring for the full
                # discussion of why min_altitude isn't a drop-in
                # substitute (taxi-then-airborne aircraft would have
                # bucket min_altitude=0, excluded by the > 0 filter).
                #
                # Original v2.42.7 typeof guard preserved against
                # stringy values, ~hex exclusion preserved for TIS-B/
                # MLAT pseudo-targets.
                rows = q("""
                    SELECT icao, callsign, min_nonzero_altitude AS altitude, aircraft_type
                    FROM sightings_hourly
                    WHERE hour_bucket >= ? AND min_nonzero_altitude IS NOT NULL
                      AND typeof(min_nonzero_altitude) IN ('integer', 'real')
                      AND icao NOT LIKE '~%'
                    ORDER BY min_nonzero_altitude ASC LIMIT 1
                """, (start_ts,))
                result["cards"]["lowest_altitude"] = dict(rows[0]) if rows else None

            if card_check("fastest"):
                # v2.87.5: rewritten to use sightings_hourly.max_speed
                # rollup instead of scanning all_sightings. Same shape
                # as the v2.86.6 highest_altitude rewrite — pull top 20
                # candidates from the rollup, then filter in Python via
                # the type-aware speed ceiling. Drops from ~1.7s to
                # single-digit ms on busy installs (sightings_hourly
                # is ~70× smaller than today's all_sightings slice).
                #
                # Subtle improvement on glitchy data: the original
                # top-20-from-all_sightings pattern can fill all 20
                # slots with duplicate sightings of one glitchy ICAO
                # (e.g. 200 sightings of a B763 reporting 1010 kt
                # before its real reading shows up as the 21st row).
                # Python filter rejects them all, then has no
                # fallback. The rollup version is naturally diverse
                # (one row per icao per hour), so the top-20 set
                # spans more aircraft and the filter finds a
                # plausible answer cleanly. Same answer in the
                # non-glitchy case, better in the glitchy case.
                #
                # The original v2.42.x typeof guard against stringy
                # speed values is preserved and the ~hex exclusion
                # for TIS-B/MLAT pseudo-targets is preserved.
                # LIMIT 50 (vs 20 in the v2.42.x all_sightings version)
                # because the rollup row shape is different. In the
                # original query, 20 sighting-rows could represent only
                # 1-2 different aircraft (many sightings each), so 20
                # was plenty of headroom. In the rollup, 20 rows means
                # 20 different (icao, hour) pairs — and a single very
                # glitchy aircraft active for several hours could
                # contribute multiple of those rows, eating into the
                # legit-aircraft headroom. 50 is still tiny on the
                # rollup table and gives comfortable margin against
                # realistic glitch counts (most installs see 1-3
                # glitchy aircraft / day, each contributing 1-5 hour
                # buckets, so 30+ slots remain for legit answers).
                rows = q("""
                    SELECT icao, callsign, max_speed AS speed, aircraft_type
                    FROM sightings_hourly
                    WHERE hour_bucket >= ? AND max_speed IS NOT NULL
                      AND typeof(max_speed) IN ('integer', 'real')
                      AND icao NOT LIKE '~%'
                    ORDER BY max_speed DESC LIMIT 50
                """, (start_ts,))
                fastest = None
                for r in rows:
                    cap = _collector_mod.speed_ceiling_for_type(r["aircraft_type"])
                    if r["speed"] <= cap:
                        fastest = dict(r)
                        break
                result["cards"]["fastest"] = fastest

            if card_check("slowest"):
                # Floor of 40 kt excludes airport service vehicles and
                # stopped/landing aircraft. Also excludes TIS-B pseudos.
                rows = q("""
                    SELECT icao, callsign, speed, aircraft_type
                    FROM all_sightings
                    WHERE seen_at >= ? AND speed IS NOT NULL
                      AND typeof(speed) IN ('integer', 'real')
                      AND speed >= 40
                      AND icao NOT LIKE '~%'
                    ORDER BY speed ASC LIMIT 1
                """, (start_ts,))
                result["cards"]["slowest"] = dict(rows[0]) if rows else None

            if card_check("longest_track"):
                # v2.88.0: rewritten to read from aircraft_track_daily
                # rollup (one row per (icao, day_bucket), populated by
                # the collector on every poll, backfilled today-only by
                # migration v6). Drops from ~1.4-1.7s to single-digit
                # ms — the previous implementation pulled ~970K
                # (icao, seen_at) rows from all_sightings and walked
                # them in Python, which was already the *optimized*
                # version (rewritten from a 30+s window-function
                # pattern in v2.42.9). The session-tracking rollup
                # finally moves the work from query time to write time.
                #
                # The rollup's `best_session_*` columns track the
                # longest session today including the in-flight one —
                # the collector promotes-to-best on every poll, so
                # `best_session_duration` is always current. No need
                # to compose open + closed at read time.
                #
                # `best_session_duration > 0` filters single-sighting
                # aircraft (one observation, no session length) — same
                # `if best_dur > 0` guard the previous implementation
                # used. ~hex exclusion stays on the read path,
                # matching how every other rollup-backed Stats query
                # filters TIS-B/MLAT pseudo-targets.
                rows = q("""
                    SELECT icao, callsign,
                           best_session_duration AS duration_seconds,
                           best_session_start    AS first_seen,
                           best_session_end      AS last_seen
                    FROM aircraft_track_daily
                    WHERE day_bucket >= ? AND best_session_duration > 0
                      AND icao NOT LIKE '~%'
                    ORDER BY best_session_duration DESC LIMIT 1
                """, (start_ts,))
                if rows:
                    r = rows[0]
                    result["cards"]["longest_track"] = {
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "duration_seconds": r["duration_seconds"],
                        "first_seen": r["first_seen"],
                        "last_seen": r["last_seen"],
                    }
                else:
                    result["cards"]["longest_track"] = None

            # --- Composition ---
            if card_check("top_types"):
                # v2.85.9: switched from all_sightings COUNT(DISTINCT icao)
                # GROUP BY aircraft_type to seen_aircraft GROUP BY
                # aircraft_type. The denormalized aircraft_type column on
                # seen_aircraft is populated by the collector's UPSERT
                # (and by migration v1's backfill on existing installs)
                # using the same value all_sightings.aircraft_type would
                # carry — so this returns identical results with ~1000×
                # fewer rows scanned. Index used: idx_seen_type. Measured
                # 6.3s → 14ms on a 5M-sighting / 26k-aircraft synthetic
                # install (450× speedup, identical top-5).
                rows = q("""
                    SELECT aircraft_type, COUNT(*) AS n FROM seen_aircraft
                    WHERE last_seen_at >= ?
                      AND aircraft_type IS NOT NULL AND aircraft_type != ''
                    GROUP BY aircraft_type
                    ORDER BY n DESC LIMIT 5
                """, (start_ts,))
                # v2.41.15: attach a friendly name when we recognize the ICAO
                # type designator ("A321" -> "Airbus A321"). Frontend renders
                # "CODE — Name" when `name` is present, falls back to just the
                # code otherwise. Unknown codes stay as-is — the base behavior
                # before this release was code-only.
                out = []
                for r in rows:
                    d = dict(r)
                    nm = aircraft_type_name(d.get("aircraft_type", ""))
                    if nm:
                        d["name"] = nm
                    out.append(d)
                result["cards"]["top_types"] = out

            if card_check("top_operators"):
                # v2.85.9: switched from all_sightings GROUP BY callsign
                # plus Python regex prefix-extraction to a direct query
                # against seen_aircraft.operator. The operator column is
                # populated by migration v2 (which derives operator from
                # last_callsign with the same ^[A-Z]{2,4} regex used here
                # before) and refreshed on every collector UPSERT. The
                # query goes from ~5.7s on a 5M-sighting install to ~24ms
                # (240× speedup), and as a bonus it eliminates the Python
                # regex pass entirely.
                #
                # Subtle semantic difference worth knowing about: the old
                # code aggregated COUNT(DISTINCT icao) per callsign, then
                # rolled callsigns up to operators in Python — meaning an
                # aircraft that flew under two different operator codes
                # within the window would be counted under both. The new
                # code uses each aircraft's *current* operator (last
                # callsign's prefix) and counts each aircraft once. For
                # aircraft that don't switch operators (the vast majority),
                # results are identical. For ferry flights or recently-
                # repainted airframes that flew under a previous operator
                # code earlier in the window, the new query credits only
                # the current operator — which is arguably more accurate
                # for "who's flying these aircraft right now".
                rows = q("""
                    SELECT operator, COUNT(*) AS n FROM seen_aircraft
                    WHERE last_seen_at >= ?
                      AND operator IS NOT NULL AND operator != ''
                    GROUP BY operator
                    ORDER BY n DESC LIMIT 5
                """, (start_ts,))
                # v2.41.15: attach airline name when we recognize the ICAO
                # 3-letter designator ("DAL" -> "Delta Air Lines").
                out = []
                for r in rows:
                    d = dict(r)
                    nm = airline_name(d["operator"])
                    if nm:
                        d["name"] = nm
                    out.append(d)
                result["cards"]["top_operators"] = out

            if card_check("military_branches"):
                # Classify military ICAOs by prefix ranges (US military block is AE)
                # AE0000–AE1FFF = Air Force, AE2000–AE3FFF = Navy, etc.
                # This is a heuristic — accurate for US mil, best-effort for others.
                rows = q("""
                    SELECT DISTINCT icao FROM military_sightings WHERE seen_at >= ?
                """, (start_ts,))
                branches = {"Air Force": 0, "Navy": 0, "Army": 0, "Marines": 0, "Coast Guard": 0, "Other": 0}
                for row in rows:
                    icao = (row["icao"] or "").upper()
                    # Very rough US mil classification by ICAO hex
                    if icao.startswith("AE"):
                        try:
                            n = int(icao[2:], 16)
                            if n <= 0x1FFF:       branches["Air Force"] += 1
                            elif n <= 0x3FFF:    branches["Navy"] += 1
                            elif n <= 0x5FFF:    branches["Army"] += 1
                            elif n <= 0x6FFF:    branches["Marines"] += 1
                            elif n <= 0x7FFF:    branches["Coast Guard"] += 1
                            else:                branches["Other"] += 1
                        except ValueError:
                            branches["Other"] += 1
                    else:
                        branches["Other"] += 1
                # Filter zeros and sort by count
                bl = [{"branch": b, "n": n} for b, n in branches.items() if n > 0]
                bl.sort(key=lambda x: x["n"], reverse=True)
                result["cards"]["military_branches"] = bl

            if card_check("category_mix"):
                # v2.89.0: rewrote from a 30-line Python heuristic loop
                # over seen_aircraft to a single SQL GROUP BY against
                # the new seen_aircraft.category column. The heuristics
                # (helicopter type codes, commercial type-code prefixes,
                # military membership) now live in categorize.py — the
                # collector applies them at write time, migration v7
                # backfilled them for existing rows.
                #
                # Display labels match what the previous version
                # produced exactly: "Commercial", "General Aviation",
                # "Military", "Helicopter", "Unknown" (the column
                # stores lowercase tokens). Order: descending by count,
                # same as before. Empty buckets are dropped.
                _CAT_LABELS = {
                    "commercial":       "Commercial",
                    "general_aviation": "General Aviation",
                    "military":         "Military",
                    "helicopter":       "Helicopter",
                    "unknown":          "Unknown",
                }
                rows = q("""
                    SELECT category, COUNT(*) AS n
                    FROM seen_aircraft
                    WHERE last_seen_at >= ? AND category IS NOT NULL
                    GROUP BY category
                """, (start_ts,))
                cl = [
                    {"category": _CAT_LABELS.get(r["category"], r["category"]),
                     "n": r["n"]}
                    for r in rows if r["n"] > 0
                ]
                cl.sort(key=lambda x: x["n"], reverse=True)
                result["cards"]["category_mix"] = cl

            # v2.50.27: Top countries by registration. The country a
            # given aircraft is registered in is determined by which
            # ICAO-allocated 24-bit address block its hex falls into —
            # see countries.country_for_icao for the lookup. This is
            # a static, source-of-truth mapping with no external data
            # dependency, so the only DB work is fetching today's
            # unique ICAOs; the country grouping happens in Python.
            #
            # Counted by unique aircraft (one per ICAO), today only —
            # mirrors top_types and top_operators. Top 5 shown on the
            # card with a drill-in for the full list (see
            # all_countries handling further down in /api/stats/drill).
            if card_check("top_countries"):
                # v2.85.2: switched from DISTINCT icao on all_sightings +
                # Python country_for_icao() loop to a SQL GROUP BY against
                # seen_aircraft.country (denormalized at insert time, indexed
                # via idx_seen_country). The previous shape pulled every
                # distinct ICAO seen in the window from a 25M-row table,
                # then bucketed in Python — that scan dominated the query
                # cost (~1.2 seconds at 25M rows on the test bench, ~100
                # seconds on the Pi user's hardware per his earlier diag
                # capture, where it was the slowest single Stats query).
                #
                # The new shape scans seen_aircraft (~26k rows for a typical
                # install), groups by the already-resolved country, applies
                # LIMIT 5 server-side. ~1000× row reduction, expected sub-
                # millisecond on most installs. Visually identical output —
                # seen_aircraft.country is populated by the same
                # country_for_icao() function the old code called per-ICAO,
                # just resolved once at insert time rather than once per
                # query.
                #
                # Window filter: last_seen_at >= start_ts. Same window as
                # the old query's seen_at >= start_ts. The seen_aircraft
                # row is "current" relative to its last sighting, so an
                # aircraft last seen inside the window counts; one last
                # seen outside doesn't. Matches old semantics.
                rows = q("""
                    SELECT country, COUNT(*) AS n
                    FROM seen_aircraft
                    WHERE last_seen_at >= ?
                      AND country IS NOT NULL AND country != ''
                    GROUP BY country
                    ORDER BY n DESC
                    LIMIT 5
                """, (start_ts,))
                result["cards"]["top_countries"] = [
                    {"country": r["country"], "n": r["n"]} for r in rows
                ]

            # --- Patterns ---
            if card_check("hourly_histogram"):
                # Bucket by hour-of-day in configured timezone
                # v2.42.9: moved the DISTINCT-per-hour aggregation from
                # Python to SQL. The old code pulled 566K rows to Python
                # on a busy-airspace Pi and bucketed in a dict-of-sets.
                # Observed 1.8 seconds.
                #
                # Timezone handling: the stats.timezone config can shift
                # the "day" so hour-0 aligns with local midnight. Rather
                # than compute strftime-with-tz inside SQL (inconsistent
                # across SQLite builds), we precompute the tz offset in
                # seconds and add it to seen_at inside the GROUP BY.
                # The bucket is then ((seen_at + offset) / 3600) % 24.
                import datetime as _dt
                st_tz_name = (st.get("timezone") or "").strip()
                tz_offset_sec = 0
                try:
                    from zoneinfo import ZoneInfo
                    if st_tz_name:
                        tz = ZoneInfo(st_tz_name)
                        # Use the offset at start_ts \u2014 handles DST edge
                        # cases by sampling the offset at the window start.
                        tz_offset_sec = int(
                            tz.utcoffset(_dt.datetime.fromtimestamp(start_ts)).total_seconds()
                        )
                except Exception:
                    pass
                rows = q("""
                    SELECT ((seen_at + :off) / 3600) % 24 AS hour,
                           COUNT(DISTINCT icao) AS n
                    FROM all_sightings INDEXED BY idx_all_seen_icao
                    WHERE seen_at >= :start_ts
                    GROUP BY hour
                """, {"off": tz_offset_sec, "start_ts": start_ts})
                # Normalize \u2014 SQL returns only hours that have data; fill
                # the rest with zero so the frontend gets a 24-element array.
                by_hour = {int(r["hour"]): int(r["n"]) for r in rows}
                result["cards"]["hourly_histogram"] = [
                    {"hour": h, "n": by_hour.get(h, 0)} for h in range(24)
                ]

            # --- History (Wave 2) ---
            if card_check("first_time_seen"):
                # Aircraft whose first-ever sighting was today.
                # JOIN seen_aircraft on its first_seen_at field.
                try:
                    rows = q("""
                        SELECT icao, first_seen_at, first_callsign, first_aircraft_type
                        FROM seen_aircraft
                        WHERE first_seen_at >= ?
                        ORDER BY first_seen_at DESC
                        LIMIT 20
                    """, (start_ts,))
                    total_row = q("SELECT COUNT(*) AS n FROM seen_aircraft WHERE first_seen_at >= ?", (start_ts,))
                    total = total_row[0]["n"] if total_row else 0
                    result["cards"]["first_time_seen"] = {
                        "total": total,
                        "list": [dict(r) for r in rows],
                    }
                except sqlite3.OperationalError:
                    # Table doesn't exist (pre-Wave-2 DB, not yet migrated)
                    result["cards"]["first_time_seen"] = {"total": 0, "list": []}

            if card_check("daily_counts_7d"):
                # Unique aircraft per day for the last 7 days (in configured tz).
                # v2.42.9 tried to collapse the 7 queries into one GROUP BY,
                # but that regressed from ~2.6s to ~6s on a Pi with 3M rows.
                # Reason: the GROUP BY version does COUNT(DISTINCT icao)
                # across the whole 7-day window with bucket-aware
                # deduplication, which requires SQLite to materialize a
                # (bucket, icao) temp B-tree over millions of rows. The
                # original 7-query loop does 7 simple COUNT(DISTINCT icao)
                # each scoped to one day's worth of data \u2014 each uses
                # idx_all_seen_icao as a pure covering scan with no temp
                # structures. Seven small queries beat one big one.
                # Lesson filed: "1 query good, N queries bad" is context-
                # dependent; the number of queries matters less than the
                # per-query plan.
                import datetime as _dt
                st_tz_name = (st.get("timezone") or "").strip()
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(st_tz_name) if st_tz_name else None
                except Exception:
                    tz = None
                now_dt = _dt.datetime.fromtimestamp(end_ts, tz=tz)
                midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                # Pull last 7 day boundaries
                buckets = []
                for i in range(6, -1, -1):  # 6 days ago through today
                    day_start = midnight - _dt.timedelta(days=i)
                    day_end = day_start + _dt.timedelta(days=1)
                    buckets.append({
                        "date": day_start.strftime("%Y-%m-%d"),
                        "label": day_start.strftime("%a"),
                        "start_ts": int(day_start.timestamp()),
                        "end_ts": int(day_end.timestamp()),
                    })
                # v2.85.9: each query switched from all_sightings to
                # sightings_hourly. Same shape as before (7 small queries,
                # one per day, each a COUNT(DISTINCT icao) over a one-day
                # window) — but each scans the rollup table (~9k rows per
                # day on the test bench) instead of all_sightings (~700k
                # rows per day). The ICAO column is faithful in the
                # rollup because sightings_hourly has one row per (icao,
                # hour-bucket): if an aircraft was seen in any hour of a
                # given day, exactly one rollup row contributes to that
                # day's count. Measured 642ms → 15ms on the synthetic
                # test bench (43× speedup, identical or near-identical
                # day counts within rounding from how partial-day
                # sightings get bucketed).
                #
                # Why we still keep 7 separate queries instead of one
                # GROUP BY: same reason v2.42.9's GROUP BY rewrite was
                # rolled back. The 7-query approach lets each query be
                # a simple range-seek on a single index; one GROUP BY
                # would force SQLite to materialize a temp B-tree across
                # 7 days of (icao, day) pairs. The rollup table makes
                # both shapes faster, but the relative ordering of "7
                # small > 1 big" is unchanged, so we keep the proven
                # shape.
                for b in buckets:
                    row = q("""
                        SELECT COUNT(DISTINCT icao) AS n
                        FROM sightings_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?
                    """, (b["start_ts"], b["end_ts"]))
                    b["n"] = row[0]["n"] if row else 0
                    # Strip internal fields the frontend doesn't need
                    del b["start_ts"], b["end_ts"]
                result["cards"]["daily_counts_7d"] = buckets

            if card_check("watchlist_frequency"):
                # Top watchlist entries by number of distinct sightings
                # in the last 30 days (rolling window).
                thirty_days_ago = end_ts - (30 * 86400)
                rows = q("""
                    SELECT watchlist_label,
                           COUNT(DISTINCT icao) AS unique_aircraft,
                           COUNT(*) AS total_hits
                    FROM watchlist_sightings
                    WHERE seen_at >= ? AND watchlist_label IS NOT NULL AND watchlist_label != ''
                    GROUP BY watchlist_label
                    ORDER BY total_hits DESC
                    LIMIT 10
                """, (thirty_days_ago,))
                result["cards"]["watchlist_frequency"] = [dict(r) for r in rows]

            # --- All-time records (Wave 3) ---
            if card_check("all_time_records"):
                try:
                    rows = q("""
                        SELECT record_type, value, icao, callsign, aircraft_type, set_at, extra
                        FROM stats_records
                        ORDER BY record_type ASC
                    """)
                    result["cards"]["all_time_records"] = [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    # Table doesn't exist (pre-Wave-3 DB, not yet migrated)
                    result["cards"]["all_time_records"] = []

            # --- Coverage (Wave 5) ---
            # Both the range rose and the distance histogram operate on the
            # same underlying query: count positions by (direction_bin, distance_bucket)
            # over the configured time window. We compute it once and share.
            #
            # v2.42.12: this block uses _card_enabled directly because it
            # services TWO cards off a single query. card_check's per-card
            # attribution doesn't fit that shape, so we set _current_card
            # manually for the shared query. Without this, slow queries
            # here inherited whatever the previous card set _current_card
            # to (typically "db_size"), causing persistent mis-attribution
            # in the v2.42.8 timing logs.
            want_rose = _card_enabled(cards, "range_rose", groups)
            want_histo = _card_enabled(cards, "distance_histogram", groups)
            if want_rose or want_histo:
                _current_card[0] = "range_rose_histogram"
            if (want_rose or want_histo) and have_location:
                # Resolve the time window
                rr = st.get("range_rose") or {}
                window = (rr.get("window") or "30d").strip()
                if window == "today":
                    window_start = start_ts
                elif window == "7d":
                    window_start = end_ts - 7 * 86400
                elif window == "30d":
                    window_start = end_ts - 30 * 86400
                elif window == "all_time":
                    window_start = 0
                elif window == "custom":
                    days = int(rr.get("window_custom_days") or 14)
                    window_start = end_ts - days * 86400
                else:
                    window_start = end_ts - 30 * 86400

                # Resolve the distance buckets
                buckets = rr.get("distance_buckets") or [50, 100, 150, 200, 250]
                try:
                    buckets = [float(b) for b in buckets if b is not None]
                except (TypeError, ValueError):
                    buckets = [50, 100, 150, 200, 250]

                # Build bucket label strings for the frontend. Example:
                # buckets=[50,100,150,200,250] → labels=[<50, 50-100, 100-150, 150-200, 200-250, 250+]
                bucket_labels = []
                for i, upper in enumerate(buckets):
                    lower = 0 if i == 0 else buckets[i-1]
                    if i == 0:
                        bucket_labels.append(f"<{int(upper)}")
                    else:
                        bucket_labels.append(f"{int(lower)}-{int(upper)}")
                bucket_labels.append(f"{int(buckets[-1])}+")

                # v2.85.2: switched from all_sightings to sightings_hourly.
                # The earlier shape (v2.42.12-v2.85.1) used the covering
                # index idx_all_seen_lat_lon to scan all_sightings in
                # index-only fashion, which works but still scales with
                # sighting count — at 25M rows the index walk and hash-
                # aggregation took 12+ seconds even on fast disk. The
                # query is fundamentally "show me the position
                # distribution of aircraft over the time window," and
                # that question can be answered just as well from the
                # sightings_hourly rollup at ~150× fewer rows scanned.
                #
                # Each sightings_hourly row contributes its (last_lat,
                # last_lon) position weighted by sighting_count — so an
                # aircraft seen 60 times in one hour contributes 60 to
                # whichever grid cell contains its last-known position.
                # Total weighted count equals the total sighting count
                # exactly (verified against the all_sightings COUNT(*)
                # on representative datasets).
                #
                # Tradeoff: aircraft that fly across multiple grid cells
                # within a single hour collapse to the cell containing
                # their last-observed position, slightly under-counting
                # direction diversity for fast overflights. For the
                # range-rose chart's purpose (statistical distribution
                # of where aircraft tend to be relative to the receiver)
                # this approximation is fine — measured at ~12% fewer
                # distinct grid cells but the same total weight
                # distributed across them, producing a visually-similar
                # rose. If precise per-position fidelity becomes
                # important later, the right fix is a dedicated
                # range_rose_grid rollup table maintained on insert
                # rather than going back to scanning all_sightings.
                #
                # Window filter is on hour_bucket (the rollup's
                # primary timestamp column) which has its own index;
                # plan is "SEARCH sightings_hourly USING INDEX
                # idx_hourly_bucket".
                import math
                GRID_RES = 0.05
                rows = q("""
                    SELECT
                        ROUND(last_lat / :res) * :res AS lat_bucket,
                        ROUND(last_lon / :res) * :res AS lon_bucket,
                        SUM(sighting_count) AS cnt
                    FROM sightings_hourly
                    WHERE hour_bucket >= :start_ts
                      AND last_lat IS NOT NULL AND last_lon IS NOT NULL
                    GROUP BY lat_bucket, lon_bucket
                """, {"res": GRID_RES, "start_ts": window_start})

                # 16-direction grid + n-bucket distance grid
                num_dirs = 16
                num_buckets = len(buckets) + 1  # rings include the "above last" bucket
                grid = [[0] * num_buckets for _ in range(num_dirs)]
                bucket_totals = [0] * num_buckets
                total_positions = 0

                for row in rows:
                    lat2 = row["lat_bucket"]
                    lon2 = row["lon_bucket"]
                    cnt = row["cnt"]
                    # Haversine distance (computed once per cell, not per row)
                    d = haversine(rx_lat, rx_lon, lat2, lon2, distance_unit)
                    # v2.79.1: bearing now via distance.compass_bearing
                    # (extracted in v2.79.0). Pre-v2.79.1 this was 7 lines
                    # of inline trig duplicated from the furthest-card scope.
                    bearing = _dist_compass_bearing(rx_lat, rx_lon, lat2, lon2)
                    # Snap to 16-direction bin (bins are centered on cardinals)
                    dir_idx = int(round(bearing / 22.5)) % num_dirs
                    # Snap to distance bucket
                    bi = num_buckets - 1  # default to last (overflow) bucket
                    for i, upper in enumerate(buckets):
                        if d < upper:
                            bi = i
                            break
                    # Weight by cell count \u2014 a cell with 50 sightings
                    # contributes 50 to the bucket, not 1.
                    grid[dir_idx][bi] += cnt
                    bucket_totals[bi] += cnt
                    total_positions += cnt

                if want_rose:
                    result["cards"]["range_rose"] = {
                        "window": window,
                        "unit": distance_unit,
                        "directions": ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                                       "S","SSW","SW","WSW","W","WNW","NW","NNW"],
                        "bucket_labels": bucket_labels,
                        "grid": grid,
                        "total_positions": total_positions,
                    }
                if want_histo:
                    result["cards"]["distance_histogram"] = {
                        "window": window,
                        "unit": distance_unit,
                        "buckets": bucket_labels,
                        "counts": bucket_totals,
                        "total_positions": total_positions,
                    }
            elif want_rose or want_histo:
                # No receiver location set — emit null so frontend can show hint
                if want_rose:
                    result["cards"]["range_rose"] = None
                if want_histo:
                    result["cards"]["distance_histogram"] = None

            conn.close()

            # v2.42.8: attach the collected timings to the response. Sorted
            # slowest-first so the problem queries are immediately obvious
            # in the JSON. Also emits a single summary log line \u2014 the
            # per-query WARN lines are already logged inside q().
            _query_timings.sort(key=lambda x: -x["ms"])
            result["_query_timings"] = _query_timings
            total_ms = sum(t["ms"] for t in _query_timings)
            slow_count = sum(1 for t in _query_timings if t["ms"] > SLOW_QUERY_MS)
            if slow_count:
                logger.warning(
                    f"Stats endpoint: {len(_query_timings)} queries, "
                    f"{total_ms:.0f}ms total, {slow_count} exceeded "
                    f"{SLOW_QUERY_MS}ms threshold"
                )
            else:
                logger.info(
                    f"Stats endpoint: {len(_query_timings)} queries, "
                    f"{total_ms:.0f}ms total"
                )
        except Exception as e:
            logger.error(f"Stats query failed: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})

        return result


    # =========================================================================
    # Shared classifier helpers used by both /api/stats and /api/stats/drill
    # =========================================================================
    # These are pure functions kept next to the drill endpoint so the row-click
    # drill-downs can reuse the same categorization logic that produced the
    # counts on the list cards. Keeping these as module-level functions (not
    # nested inside one handler) means updates stay in one place.

    # v2.89.0: _HELI_TYPES and _classify_category moved to categorize.py
    # so the heuristics live in one place. The category_mix card and its
    # drill panel now read seen_aircraft.category directly. Migration v7
    # backfilled existing rows; the collector maintains the column on
    # every poll with a SQL-side sticky-military rule.

    def _operator_prefix(callsign):
        """Extract the 2-4 letter operator prefix from a callsign.
        'UAL123' → 'UAL', 'N54321' → None (no leading letters to extract).
        Returns None if no match."""
        import re
        if not callsign:
            return None
        m = re.match(r"^([A-Z]{2,4})", callsign.strip().upper())
        return m.group(1) if m else None

    def _classify_branch(icao):
        """Classify a US military ICAO into a service branch.
        Rough heuristic based on the AE0000–AE7FFF hex range allocation."""
        if not icao:
            return "Other"
        icao = icao.upper()
        if not icao.startswith("AE"):
            return "Other"
        try:
            n = int(icao[2:], 16)
        except ValueError:
            return "Other"
        if n <= 0x1FFF:  return "Air Force"
        if n <= 0x3FFF:  return "Navy"
        if n <= 0x5FFF:  return "Army"
        if n <= 0x6FFF:  return "Marines"
        if n <= 0x7FFF:  return "Coast Guard"
        return "Other"

    # =========================================================================
    # /api/stats/drill — drill-down into the aircraft behind a stat card
    # =========================================================================
    # Shared endpoint for all card drill-downs. Dispatches by ?card=... to a
    # SQL pattern. All drills return the same row shape:
    #   {rows: [{icao, callsign, aircraft_type, metric, metric_label, seen_at}, ...]}
    # so the frontend has one table renderer. metric is the value to show in
    # the "value" column (distance, altitude, speed, duration, etc); metric_label
    # formats it with units for display.
    @app.get("/api/stats/drill")
    async def drill_stats(card: str, value: Optional[str] = Query(None),
                           all: bool = Query(False, alias="all")):
        """Drill into the aircraft behind a stat card.
        For simple cards (Waves 1 & 2): card=<id> is all that's needed.
        For list-card row clicks (Wave 3): card=<id>&value=<specific value>
        (e.g. card=top_types&value=C172).

        v2.66.0: when all=true, bypass the 25-row cap. Used by the
        Composition cards' "View all N {types|operators|etc}" expand
        button. Server still computes the full result either way; the
        cap was just a render budget for the default Option C panel.
        """
        # v2.66.0: rebind to local name to avoid shadowing the Python
        # builtin all() inside this scope.
        bypass_cap = bool(all)
        import sqlite3, math
        st = CONFIG.get("stats") or {}
        if not st.get("enabled", True):
            return {"rows": []}

        start_ts, end_ts = _day_bounds_ts()

        db_path = CONFIG.get("data", {}).get("db_file", "aircraft_history.db")
        if not Path(db_path).is_absolute():
            db_path = str(Path(__file__).parent / db_path)

        rx = CONFIG.get("receiver", {}) or {}
        rx_lat = rx.get("latitude")
        rx_lon = rx.get("longitude")
        have_location = isinstance(rx_lat, (int, float)) and isinstance(rx_lon, (int, float))
        distance_unit = rx.get("distance_unit", "mi")

        # v2.79.0: distance + bearing helpers come from distance.py.
        # Local aliases preserve the existing call shapes in this scope.
        haversine = _dist_haversine
        compass_bearing = _dist_compass_bearing

        # Human-readable compass label: "N", "NE", "E", etc. from a bearing.
        # 16-point rose would be nicer but 8 points is plenty for the drill
        # panel — the numeric degrees are also shown next to the label.
        def compass_label(deg):
            points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
            idx = int((deg + 22.5) // 45) % 8
            return points[idx]

        # Defensive numeric coercion for SQLite values. SQLite columns are
        # dynamically typed and MAX()/MIN() on speed/altitude columns can
        # occasionally come back as strings (e.g. "ground") or unexpected
        # types. The speed/altitude drill branches have typeof() filters in
        # their SQL, but this helper is kept as a belt-and-suspenders fallback
        # so a single weird row never takes down the whole drill panel with
        # a NameError / ValueError.
        def _to_number(v):
            if v is None:
                return None
            # isinstance(True, int) is True in Python, so filter bools out
            # explicitly to avoid silently formatting them as "1 ft". Note we
            # also have to guard the fallback below because float(True) == 1.0.
            if isinstance(v, bool):
                return None
            if isinstance(v, (int, float)):
                return v
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        try:
            conn = _open_db_conn(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            def q(sql, params=()):
                cur.execute(sql, params)
                return cur.fetchall()

            rows = []

            if card == "furthest":
                # Top-N by distance today. Need lat/lon per aircraft.
                if have_location:
                    raw = q("""
                        SELECT icao,
                               MAX(callsign) AS callsign,
                               MAX(aircraft_type) AS aircraft_type,
                               lat, lon, seen_at
                        FROM all_sightings
                        WHERE seen_at >= ? AND lat IS NOT NULL AND lon IS NOT NULL
                        GROUP BY icao, lat, lon, seen_at
                    """, (start_ts,))
                    best_per_icao = {}
                    for r in raw:
                        d = haversine(rx_lat, rx_lon, r["lat"], r["lon"], distance_unit)
                        if r["icao"] not in best_per_icao or d > best_per_icao[r["icao"]]["metric"]:
                            best_per_icao[r["icao"]] = {
                                "icao": r["icao"],
                                "callsign": (r["callsign"] or "").strip(),
                                "aircraft_type": r["aircraft_type"] or "",
                                "metric": round(d, 1),
                                "metric_label": f"{round(d, 1)} {distance_unit}",
                                "seen_at": r["seen_at"],
                            }
                    rows = sorted(best_per_icao.values(), key=lambda x: -x["metric"])

            elif card == "fastest":
                # Apply the same type-aware speed ceiling and TIS-B filter
                # as the summary card query (see stats_today). Pull a
                # generous top-K then filter Python-side by the cap.
                #
                # For seen_at we want the timestamp of the row that set
                # the per-ICAO maximum — NOT just MAX(seen_at). The
                # frontend uses this to highlight the exact sighting on
                # the aircraft detail page. MIN(seen_at) as a tiebreaker
                # for the rare case where an aircraft hit the same peak
                # speed twice — preferring the earlier occurrence feels
                # more honest (this is when it first got there).
                raw = q("""
                    WITH peaks AS (
                        SELECT icao,
                               MAX(speed) AS peak_speed
                        FROM all_sightings
                        WHERE seen_at >= ? AND speed IS NOT NULL
                          AND typeof(speed) IN ('integer', 'real')
                          AND icao NOT LIKE '~%'
                        GROUP BY icao
                    )
                    SELECT s.icao,
                           MAX(s.callsign) AS callsign,
                           MAX(s.aircraft_type) AS aircraft_type,
                           p.peak_speed AS metric,
                           MIN(s.seen_at) AS seen_at
                    FROM all_sightings s
                    JOIN peaks p ON p.icao = s.icao
                    WHERE s.seen_at >= ?
                      AND s.speed = p.peak_speed
                    GROUP BY s.icao, p.peak_speed
                    ORDER BY metric DESC
                """, (start_ts, start_ts))
                rows = []
                for r in raw:
                    m = _to_number(r["metric"]) or 0
                    cap = _collector_mod.speed_ceiling_for_type(r["aircraft_type"])
                    if m > cap:
                        continue
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": m,
                        "metric_label": f"{int(m)} kt",
                        "seen_at": r["seen_at"],
                    })

            elif card == "slowest":
                # Floor of 40 kt to exclude airport service vehicles and
                # stopped/parking aircraft. TIS-B pseudos excluded.
                # seen_at is the timestamp of the slowest-speed sighting
                # for each ICAO (not MAX(seen_at)), so the frontend can
                # highlight the exact moment on the aircraft detail page.
                raw = q("""
                    WITH lows AS (
                        SELECT icao,
                               MIN(speed) AS low_speed
                        FROM all_sightings
                        WHERE seen_at >= ? AND speed IS NOT NULL
                          AND typeof(speed) IN ('integer', 'real')
                          AND speed >= 40
                          AND icao NOT LIKE '~%'
                        GROUP BY icao
                    )
                    SELECT s.icao,
                           MAX(s.callsign) AS callsign,
                           MAX(s.aircraft_type) AS aircraft_type,
                           l.low_speed AS metric,
                           MIN(s.seen_at) AS seen_at
                    FROM all_sightings s
                    JOIN lows l ON l.icao = s.icao
                    WHERE s.seen_at >= ?
                      AND s.speed = l.low_speed
                    GROUP BY s.icao, l.low_speed
                    ORDER BY metric ASC
                """, (start_ts, start_ts))
                rows = [{
                    "icao": r["icao"],
                    "callsign": (r["callsign"] or "").strip(),
                    "aircraft_type": r["aircraft_type"] or "",
                    "metric": _to_number(r["metric"]) or 0,
                    "metric_label": f"{int(_to_number(r['metric']) or 0)} kt",
                    "seen_at": r["seen_at"],
                } for r in raw]

            elif card == "highest_altitude":
                # typeof filter excludes stringy values like "ground" that would
                # otherwise sort higher than any number in DESC order. TIS-B
                # pseudo-targets excluded — their altitude is ATC-relayed.
                # seen_at is the timestamp of the peak-altitude sighting
                # so the frontend can highlight the exact moment on the
                # aircraft detail page.
                raw = q("""
                    WITH peaks AS (
                        SELECT icao, MAX(altitude) AS peak_alt
                        FROM all_sightings
                        WHERE seen_at >= ? AND altitude IS NOT NULL
                          AND typeof(altitude) IN ('integer', 'real')
                          AND icao NOT LIKE '~%'
                        GROUP BY icao
                    )
                    SELECT s.icao,
                           MAX(s.callsign) AS callsign,
                           MAX(s.aircraft_type) AS aircraft_type,
                           p.peak_alt AS metric,
                           MIN(s.seen_at) AS seen_at
                    FROM all_sightings s
                    JOIN peaks p ON p.icao = s.icao
                    WHERE s.seen_at >= ?
                      AND s.altitude = p.peak_alt
                    GROUP BY s.icao, p.peak_alt
                    ORDER BY metric DESC
                """, (start_ts, start_ts))
                rows = [{
                    "icao": r["icao"],
                    "callsign": (r["callsign"] or "").strip(),
                    "aircraft_type": r["aircraft_type"] or "",
                    "metric": _to_number(r["metric"]) or 0,
                    "metric_label": f"{int(_to_number(r['metric']) or 0):,} ft",
                    "seen_at": r["seen_at"],
                } for r in raw]

            elif card == "lowest_altitude":
                # Exclude ground (altitude 0 / null / "ground"). typeof filter
                # guards against stringy values in the REAL column. seen_at
                # is the timestamp of the lowest-altitude sighting.
                raw = q("""
                    WITH lows AS (
                        SELECT icao, MIN(altitude) AS low_alt
                        FROM all_sightings
                        WHERE seen_at >= ? AND altitude IS NOT NULL
                          AND typeof(altitude) IN ('integer', 'real')
                          AND altitude > 0
                          AND icao NOT LIKE '~%'
                        GROUP BY icao
                    )
                    SELECT s.icao,
                           MAX(s.callsign) AS callsign,
                           MAX(s.aircraft_type) AS aircraft_type,
                           l.low_alt AS metric,
                           MIN(s.seen_at) AS seen_at
                    FROM all_sightings s
                    JOIN lows l ON l.icao = s.icao
                    WHERE s.seen_at >= ?
                      AND s.altitude = l.low_alt
                    GROUP BY s.icao, l.low_alt
                    ORDER BY metric ASC
                """, (start_ts, start_ts))
                rows = [{
                    "icao": r["icao"],
                    "callsign": (r["callsign"] or "").strip(),
                    "aircraft_type": r["aircraft_type"] or "",
                    "metric": _to_number(r["metric"]) or 0,
                    "metric_label": f"{int(_to_number(r['metric']) or 0):,} ft",
                    "seen_at": r["seen_at"],
                } for r in raw]

            elif card == "longest_track":
                # v2.88.0: rewritten to read from aircraft_track_daily,
                # the same per-aircraft per-day session-tracking rollup
                # that powers the summary card. The previous (v2.68.0)
                # implementation was already the optimized version of
                # the drill — a Python single-pass walk over today's
                # all_sightings slice — but it still pulled 950K+ rows
                # per render and ran a follow-up IN-clause query
                # against idx_all_icao for callsign+type metadata. The
                # rollup version reads ~50-200 pre-computed rows
                # already keyed by (icao, day_bucket), and the
                # callsign + aircraft_type fields are denormalized
                # into the rollup so no follow-up metadata query is
                # needed. Drops the drill from ~700-1700ms to single-
                # digit ms.
                #
                # No LIMIT in the SQL — the drill panel renders all
                # ranked aircraft with sessions today (the card
                # variant returns just LIMIT 1). The rollup is
                # already small (~50-200 rows/day), so an unbounded
                # ORDER BY is fine.
                rollup_rows = q("""
                    SELECT icao, callsign, aircraft_type,
                           best_session_duration AS dur,
                           best_session_start    AS first_seen,
                           best_session_end      AS last_seen
                    FROM aircraft_track_daily
                    WHERE day_bucket >= ?
                      AND best_session_duration > 0
                      AND icao NOT LIKE '~%'
                    ORDER BY best_session_duration DESC
                """, (start_ts,))
                rows = []
                for r in rollup_rows:
                    dur_sec = r["dur"]
                    h = dur_sec // 3600
                    mi = (dur_sec % 3600) // 60
                    label = f"{h}h {mi}m" if h else f"{mi}m"
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": dur_sec,
                        "metric_label": label,
                        "seen_at": r["last_seen"],
                    })

            elif card == "unique_today":
                # All unique aircraft seen today, sorted by first contact.
                # The metric column shows duration (first..last), matching the
                # overall stat's notion of "today's traffic".
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           MAX(seen_at) AS last_seen,
                           COUNT(*) AS hits
                    FROM all_sightings
                    WHERE seen_at >= ?
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (start_ts,))
                for r in raw:
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                    })

            elif card == "peak_simultaneous":
                # Find the moment(s) today with the most distinct aircraft
                # airborne, then list the aircraft seen in a small window
                # around that peak. Strategy: bin seen_at into 60-second
                # buckets, count distinct ICAOs per bucket, find the peak
                # bucket, then return all aircraft whose sightings fall in
                # that bucket (and a little padding on each side).
                #
                # v2.82.0: bucket_min ASC tiebreaker added so that on tied
                # days (multiple buckets share the max count), the drill
                # panel and the new peak_today search filter both pick
                # the earliest peak bucket — without this, SQLite's
                # implementation-defined LIMIT 1 ordering could pick
                # different buckets in the two surfaces and a user
                # navigating between them would see different aircraft.
                bucket_rows = q("""
                    SELECT (seen_at / 60) AS bucket_min, COUNT(DISTINCT icao) AS n
                    FROM all_sightings
                    WHERE seen_at >= ?
                    GROUP BY bucket_min
                    ORDER BY n DESC, bucket_min ASC
                    LIMIT 1
                """, (start_ts,))
                if bucket_rows:
                    peak_bucket = int(bucket_rows[0]["bucket_min"])
                    peak_start = peak_bucket * 60
                    peak_end = peak_start + 60
                    raw = q("""
                        SELECT icao,
                               MAX(callsign) AS callsign,
                               MAX(aircraft_type) AS aircraft_type,
                               MIN(seen_at) AS first_seen,
                               MAX(seen_at) AS last_seen
                        FROM all_sightings
                        WHERE seen_at >= ? AND seen_at < ?
                        GROUP BY icao
                        ORDER BY first_seen ASC
                    """, (peak_start, peak_end))
                    for r in raw:
                        rows.append({
                            "icao": r["icao"],
                            "callsign": (r["callsign"] or "").strip(),
                            "aircraft_type": r["aircraft_type"] or "",
                            "metric": 0,  # no per-aircraft value here
                            "metric_label": "",  # col hidden for this card
                            "seen_at": r["first_seen"],
                            "extra": f"peak at {time.strftime('%H:%M', time.localtime(peak_start))}",
                        })

            elif card == "military_today":
                # All aircraft flagged as military today. Same shape as
                # unique_today but filtered to is_military=1.
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           MAX(seen_at) AS last_seen,
                           COUNT(*) AS hits
                    FROM military_sightings
                    WHERE seen_at >= ?
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (start_ts,))
                for r in raw:
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                    })

            elif card == "watchlist_hits":
                # Watchlist aircraft seen today, with which watchlist entry
                # triggered the match. watchlist_sightings has watchlist_label
                # (when present) identifying the match source.
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MAX(watchlist_label) AS watchlist_label,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM watchlist_sightings
                    WHERE seen_at >= ?
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (start_ts,))
                for r in raw:
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                        "extra": r["watchlist_label"] or "",
                    })

            elif card == "top_types":
                # Drill into a specific aircraft type. ?value=C172 → list all
                # aircraft with that type today. value is matched case-insensitive.
                if not value:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required for top_types drill"})
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM all_sightings
                    WHERE seen_at >= ? AND UPPER(aircraft_type) = ?
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (start_ts, value.upper()))
                for r in raw:
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                    })

            elif card == "top_operators":
                # Drill into a specific callsign prefix. ?value=UAL → all UAL*
                # aircraft today. Need to re-parse each callsign since SQLite
                # doesn't have regex; do it in Python.
                if not value:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required for top_operators drill"})
                target_prefix = value.strip().upper()
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM all_sightings
                    WHERE seen_at >= ? AND callsign IS NOT NULL AND callsign != ''
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (start_ts,))
                for r in raw:
                    prefix = _operator_prefix(r["callsign"] or "")
                    if prefix != target_prefix:
                        continue
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                    })

            elif card == "military_branches":
                # Drill into a specific military branch. ?value=Air Force etc.
                #
                # v2.52.1: kept this on military_sightings rather than
                # converting to seen_aircraft like the top_countries and
                # category_mix drills below. Reasoning: military_sightings
                # is small (~76k rows on the busiest reference install,
                # ~370 on quiet installs), and seen_aircraft has no
                # "is_military" column we could filter on without
                # cross-referencing military_sightings anyway. The current
                # query is already fast on the perf-diag (45ms range);
                # touching it would add complexity without measurable
                # benefit.
                if not value:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required for military_branches drill"})
                target_branch = value.strip()
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM military_sightings
                    WHERE seen_at >= ?
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (start_ts,))
                for r in raw:
                    if _classify_branch(r["icao"]) != target_branch:
                        continue
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                    })

            elif card == "category_mix":
                # Drill into a specific category (Commercial/GA/Military/Helicopter/Unknown).
                #
                # v2.89.0: rewritten to read from seen_aircraft.category
                # directly. Previous version (v2.85.9) ran the
                # categorization heuristics in Python per row and
                # cross-referenced military_sightings; with the column
                # populated by the collector and backfilled by migration
                # v7, the drill becomes a simple WHERE filter on an
                # indexed column. Drops Python categorization work
                # entirely — the heuristics now live only in
                # categorize.py.
                if not value:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required for category_mix drill"})
                # Drill receives the display label ("Commercial",
                # "General Aviation", etc.); the column stores the
                # lowercase token. Map back to the token before the
                # WHERE filter.
                _CAT_LABEL_TO_TOKEN = {
                    "Commercial":       "commercial",
                    "General Aviation": "general_aviation",
                    "Military":         "military",
                    "Helicopter":       "helicopter",
                    "Unknown":          "unknown",
                }
                target_token = _CAT_LABEL_TO_TOKEN.get(value.strip())
                if target_token is None:
                    # Unknown label — return empty result rather than
                    # 400, since the frontend may have a stale label
                    # cached after a categorize.py update that adds
                    # categories.
                    rows = []
                else:
                    rows_raw = q("""
                        SELECT icao, last_callsign, aircraft_type,
                               first_seen_at, sighting_count
                        FROM seen_aircraft
                        WHERE category = ?
                          AND last_seen_at >= ?
                        ORDER BY first_seen_at ASC
                    """, (target_token, start_ts))
                    for r in rows_raw:
                        hits = int(r["sighting_count"] or 0)
                        rows.append({
                            "icao": r["icao"],
                            "callsign": (r["last_callsign"] or "").strip(),
                            "aircraft_type": r["aircraft_type"] or "",
                            "metric": hits,
                            "metric_label": f"{hits:,} hits",
                            "seen_at": r["first_seen_at"],
                        })

            elif card == "top_countries":
                # v2.50.27: drill into a specific country. ?value=Germany →
                # all aircraft registered in that country seen today.
                #
                # v2.52.1 rewrite: switched from GROUP BY all_sightings to
                # SELECT FROM seen_aircraft. Pi user's perf-diag showed
                # the old query took 30-60s on installs with 9.5M+ all_sightings
                # rows, exceeding the frontend timeout and hanging the drill.
                # The denormalized seen_aircraft.country column (built in
                # migration v1, indexed by idx_seen_country) makes this a
                # 23k-row indexed lookup instead of a 9.5M-row GROUP BY —
                # roughly 400× less work, sub-millisecond on any plausible
                # install.
                #
                # Semantic note: the old query returned `MAX(callsign)` and
                # `MIN(seen_at)` (lexically-largest callsign + first sighting
                # in window). The new query returns `last_callsign`
                # (most recent) and `first_seen_at` (first ever sighting,
                # not first in window). For the drill UI those are the
                # right semantics anyway — "last known callsign" is what
                # users want to see, and "first ever seen" is more durable
                # than "first seen in this 30-day window" which would
                # change as the window slides.
                if not value:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required for top_countries drill"})
                rows_raw = q("""
                    SELECT icao, last_callsign, aircraft_type,
                           first_seen_at, sighting_count
                    FROM seen_aircraft
                    WHERE country = ?
                      AND last_seen_at >= ?
                    ORDER BY first_seen_at ASC
                """, (value, start_ts))
                for r in rows_raw:
                    hits = int(r["sighting_count"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["last_callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen_at"],
                    })

            elif card == "watchlist_frequency":
                # Drill into a specific watchlist entry (identified by its label).
                # Window is last 30 days (matches the card's own window).
                if not value:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required for watchlist_frequency drill"})
                window_start = end_ts - 30 * 86400
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           MAX(seen_at) AS last_seen,
                           COUNT(*) AS hits
                    FROM watchlist_sightings
                    WHERE seen_at >= ? AND watchlist_label = ?
                    GROUP BY icao
                    ORDER BY last_seen DESC
                """, (window_start, value))
                for r in raw:
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["last_seen"],
                    })

            elif card == "hourly_histogram":
                # Drill into a specific hour of today. ?value=<hour 0-23>
                # Returns the aircraft first seen during that hour, plus their
                # hit count in that hour window.
                if value is None:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required (hour 0-23)"})
                try:
                    hour = int(value)
                    if not (0 <= hour <= 23):
                        raise ValueError("out of range")
                except (ValueError, TypeError):
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value must be an integer 0-23"})
                # Compute the hour window in the configured timezone, consistent
                # with how the hourly_histogram card bins its data.
                hour_start = start_ts + hour * 3600
                hour_end = hour_start + 3600
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM all_sightings
                    WHERE seen_at >= ? AND seen_at < ?
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (hour_start, hour_end))
                for r in raw:
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                    })

            elif card == "daily_counts_7d":
                # Drill into a specific day. ?value=YYYY-MM-DD in the configured
                # timezone. Uses the same TZ calc as _day_bounds_ts to determine
                # the day boundaries.
                if not value:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required (YYYY-MM-DD)"})
                try:
                    from datetime import datetime, timezone
                    try:
                        from zoneinfo import ZoneInfo
                    except ImportError:
                        ZoneInfo = None
                    # Parse the date in the configured timezone so boundaries align
                    tz_name = (st.get("timezone") or "").strip()
                    tzinfo = ZoneInfo(tz_name) if (ZoneInfo and tz_name) else None
                    y, m, d = [int(x) for x in value.split("-")]
                    if tzinfo:
                        day_start_dt = datetime(y, m, d, 0, 0, 0, tzinfo=tzinfo)
                    else:
                        day_start_dt = datetime(y, m, d, 0, 0, 0)
                        day_start_dt = day_start_dt.replace(tzinfo=timezone.utc)
                    day_start_ts = int(day_start_dt.timestamp())
                    day_end_ts = day_start_ts + 86400
                except Exception as e:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": f"Invalid date '{value}': {e}"})
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM all_sightings
                    WHERE seen_at >= ? AND seen_at < ?
                    GROUP BY icao
                    ORDER BY first_seen ASC
                """, (day_start_ts, day_end_ts))
                for r in raw:
                    hits = int(r["hits"] or 0)
                    rows.append({
                        "icao": r["icao"],
                        "callsign": (r["callsign"] or "").strip(),
                        "aircraft_type": r["aircraft_type"] or "",
                        "metric": hits,
                        "metric_label": f"{hits:,} hits",
                        "seen_at": r["first_seen"],
                    })

            elif card == "distance_histogram":
                # Drill into a specific distance bucket. ?value=<bucket index>
                # where 0 = first bucket (below the first threshold), N = last
                # (above the last threshold). Reuses the configured
                # distance_buckets + time window from stats.range_rose.
                if value is None:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value parameter required (bucket index)"})
                try:
                    bucket_idx = int(value)
                except (ValueError, TypeError):
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "value must be an integer (bucket index)"})
                if not have_location:
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": "receiver location not configured"})

                rr = st.get("range_rose") or {}
                window = (rr.get("window") or "30d").strip()
                if window == "today":
                    window_start = start_ts
                elif window == "7d":
                    window_start = end_ts - 7 * 86400
                elif window == "30d":
                    window_start = end_ts - 30 * 86400
                elif window == "all_time":
                    window_start = 0
                elif window == "custom":
                    days = int(rr.get("window_custom_days") or 14)
                    window_start = end_ts - days * 86400
                else:
                    window_start = end_ts - 30 * 86400

                buckets = rr.get("distance_buckets") or [50, 100, 150, 200, 250]
                try:
                    buckets = [float(b) for b in buckets if b is not None]
                except (TypeError, ValueError):
                    buckets = [50, 100, 150, 200, 250]
                if bucket_idx < 0 or bucket_idx > len(buckets):
                    conn.close()
                    return JSONResponse(status_code=400,
                                        content={"error": f"bucket index out of range (0-{len(buckets)})"})

                # Compute bucket boundaries for this index
                if bucket_idx == 0:
                    d_low, d_high = 0.0, buckets[0]
                    bucket_label = f"<{int(buckets[0])} {distance_unit}"
                elif bucket_idx == len(buckets):
                    d_low, d_high = buckets[-1], float("inf")
                    bucket_label = f"{int(buckets[-1])}+ {distance_unit}"
                else:
                    d_low, d_high = buckets[bucket_idx - 1], buckets[bucket_idx]
                    bucket_label = f"{int(d_low)}-{int(d_high)} {distance_unit}"

                # Pull positions in the window, bin each, keep those matching
                # the target bucket. Aggregate per-icao to count hits.
                raw = q("""
                    SELECT icao, callsign, lat, lon, seen_at, aircraft_type
                    FROM all_sightings
                    WHERE seen_at >= ? AND lat IS NOT NULL AND lon IS NOT NULL
                """, (window_start,))
                per_icao = {}
                for r in raw:
                    d = haversine(rx_lat, rx_lon, r["lat"], r["lon"], distance_unit)
                    if d < d_low or d >= d_high:
                        continue
                    icao = r["icao"]
                    if icao not in per_icao:
                        per_icao[icao] = {
                            "icao": icao,
                            "callsign": (r["callsign"] or "").strip(),
                            "aircraft_type": r["aircraft_type"] or "",
                            "metric": 0,
                            "metric_label": "",
                            "seen_at": r["seen_at"],
                            "extra": bucket_label,
                        }
                    per_icao[icao]["metric"] += 1
                    # Keep earliest seen_at
                    if r["seen_at"] < per_icao[icao]["seen_at"]:
                        per_icao[icao]["seen_at"] = r["seen_at"]
                    # Prefer non-empty callsign/type
                    if (r["callsign"] or "").strip():
                        per_icao[icao]["callsign"] = r["callsign"].strip()
                    if r["aircraft_type"]:
                        per_icao[icao]["aircraft_type"] = r["aircraft_type"]
                for v in per_icao.values():
                    v["metric_label"] = f"{v['metric']:,} hits"
                    rows.append(v)
                rows.sort(key=lambda x: -x["metric"])

            elif card == "all_operators":
                # v2.41.22: aggregate drill — one row per operator code, showing
                # the full distribution (not capped at top 5 like the card face).
                # Each row is a summary, not a specific aircraft, so icao/seen_at
                # are omitted from the row and the v2.41.17 enrichment block
                # silently skips it. Row clicks route back through the existing
                # top_operators drill (filter by prefix) to get the per-aircraft
                # view for a specific operator.
                op_raw = q("""
                    SELECT icao, callsign, aircraft_type, seen_at
                    FROM all_sightings
                    WHERE seen_at >= ? AND callsign IS NOT NULL AND callsign != ''
                """, (start_ts,))
                # Bucket sightings by operator prefix; track distinct ICAOs,
                # total sightings, first/last seen, and the most common
                # aircraft type per operator.
                by_op = {}  # prefix -> {icaos, sightings, first, last, types}
                for r in op_raw:
                    prefix = _operator_prefix(r["callsign"] or "")
                    if not prefix:
                        continue
                    slot = by_op.setdefault(prefix, {
                        "icaos": set(), "sightings": 0,
                        "first": r["seen_at"], "last": r["seen_at"],
                        "types": {},
                    })
                    slot["icaos"].add(r["icao"])
                    slot["sightings"] += 1
                    if r["seen_at"] < slot["first"]:
                        slot["first"] = r["seen_at"]
                    if r["seen_at"] > slot["last"]:
                        slot["last"] = r["seen_at"]
                    at = (r["aircraft_type"] or "").strip()
                    if at:
                        slot["types"][at] = slot["types"].get(at, 0) + 1
                # Materialize rows sorted by aircraft count desc
                for prefix, slot in by_op.items():
                    top_type = max(slot["types"].items(), key=lambda x: x[1])[0] \
                        if slot["types"] else ""
                    row = {
                        "operator": prefix,
                        "aircraft_count": len(slot["icaos"]),
                        "sightings": slot["sightings"],
                        "top_type": top_type,
                        "first_seen": slot["first"],
                        "last_seen": slot["last"],
                        # For the Option C renderer's primary-metric column:
                        "metric": len(slot["icaos"]),
                        "metric_label": f"{len(slot['icaos']):,}",
                        # seen_at deliberately set to first_seen so the frontend's
                        # jumpToAllTabForAircraft-style time context works; but
                        # without icao, the row won't be treated as clickable
                        # for jumping into a specific aircraft.
                        "seen_at": slot["first"],
                    }
                    # Attach friendly airline name when recognized
                    nm = airline_name(prefix)
                    if nm:
                        row["name"] = nm
                    rows.append(row)
                rows.sort(key=lambda x: -x["aircraft_count"])

            elif card == "all_types":
                # v2.41.22: aggregate drill for aircraft types. Same shape as
                # all_operators but keyed by aircraft_type. Returns the full
                # distribution not capped at top 5. Row clicks route back
                # through the top_types drill.
                type_raw = q("""
                    SELECT icao, callsign, aircraft_type, seen_at
                    FROM all_sightings
                    WHERE seen_at >= ? AND aircraft_type IS NOT NULL
                      AND aircraft_type != ''
                """, (start_ts,))
                by_type = {}  # type -> {icaos, sightings, first, last, operators}
                for r in type_raw:
                    at = (r["aircraft_type"] or "").strip().upper()
                    if not at:
                        continue
                    slot = by_type.setdefault(at, {
                        "icaos": set(), "sightings": 0,
                        "first": r["seen_at"], "last": r["seen_at"],
                        "operators": {},
                    })
                    slot["icaos"].add(r["icao"])
                    slot["sightings"] += 1
                    if r["seen_at"] < slot["first"]:
                        slot["first"] = r["seen_at"]
                    if r["seen_at"] > slot["last"]:
                        slot["last"] = r["seen_at"]
                    prefix = _operator_prefix(r["callsign"] or "")
                    if prefix:
                        slot["operators"][prefix] = \
                            slot["operators"].get(prefix, 0) + 1
                for at, slot in by_type.items():
                    top_op = max(slot["operators"].items(),
                                 key=lambda x: x[1])[0] \
                        if slot["operators"] else ""
                    row = {
                        "aircraft_type": at,
                        "aircraft_count": len(slot["icaos"]),
                        "sightings": slot["sightings"],
                        "top_operator": top_op,
                        "first_seen": slot["first"],
                        "last_seen": slot["last"],
                        "metric": len(slot["icaos"]),
                        "metric_label": f"{len(slot['icaos']):,}",
                        "seen_at": slot["first"],
                    }
                    nm = aircraft_type_name(at)
                    if nm:
                        row["name"] = nm
                    rows.append(row)
                rows.sort(key=lambda x: -x["aircraft_count"])

            elif card == "all_military_branches":
                # v2.41.22: full list of military branches, matches the shape of
                # all_operators/all_types so the Option C renderer handles all
                # three uniformly. Branch is computed at query time from ICAO
                # hex via the shared _classify_branch helper, matching the exact
                # classification in the top-5 card face.
                mil_raw = q("""
                    SELECT icao,
                           MIN(seen_at) AS first_seen, MAX(seen_at) AS last_seen,
                           COUNT(*) AS sightings
                    FROM military_sightings
                    WHERE seen_at >= ?
                    GROUP BY icao
                """, (start_ts,))
                by_branch = {}
                for r in mil_raw:
                    branch = _classify_branch(r["icao"])
                    slot = by_branch.setdefault(branch, {
                        "icaos": set(), "sightings": 0,
                        "first": r["first_seen"], "last": r["last_seen"],
                    })
                    slot["icaos"].add(r["icao"])
                    slot["sightings"] += int(r["sightings"] or 0)
                    if r["first_seen"] < slot["first"]:
                        slot["first"] = r["first_seen"]
                    if r["last_seen"] > slot["last"]:
                        slot["last"] = r["last_seen"]
                for branch, slot in by_branch.items():
                    rows.append({
                        "branch": branch,
                        "aircraft_count": len(slot["icaos"]),
                        "sightings": slot["sightings"],
                        "first_seen": slot["first"],
                        "last_seen": slot["last"],
                        "metric": len(slot["icaos"]),
                        "metric_label": f"{len(slot['icaos']):,}",
                        "seen_at": slot["first"],
                    })
                rows.sort(key=lambda x: -x["aircraft_count"])

            elif card == "all_category_mix":
                # v2.41.22: full category distribution. Categories are computed
                # at query time from aircraft_type / type_desc / military lookup,
                # NOT stored in a column. This reuses the exact classification
                # logic from the top-5 card face (see _card_enabled("category_mix")
                # in get_stats_today) so the numbers agree.
                cat_raw = q("""
                    SELECT icao, aircraft_type, type_desc,
                           MIN(seen_at) AS first_seen, MAX(seen_at) AS last_seen,
                           COUNT(*) AS sightings
                    FROM all_sightings
                    WHERE seen_at >= ?
                    GROUP BY icao
                """, (start_ts,))
                mil_icaos = set(r["icao"] for r in q(
                    "SELECT DISTINCT icao FROM military_sightings WHERE seen_at >= ?",
                    (start_ts,)
                ))
                heli_types = {"H60", "H47", "EC35", "EC45", "EC55", "AS50", "AS55",
                              "R22", "R44", "B06", "B206", "B407", "B412", "B429",
                              "B430", "A109", "A119", "A139"}
                by_cat = {}
                for r in cat_raw:
                    icao = (r["icao"] or "").upper()
                    t = (r["aircraft_type"] or "").upper()
                    desc = (r["type_desc"] or "").lower()
                    if icao in mil_icaos:
                        cat = "Military"
                    elif t in heli_types or "helicopter" in desc:
                        cat = "Helicopter"
                    elif t and (t.startswith("A3") or t.startswith("A2") or
                                t.startswith("B7") or t.startswith("B3") or
                                t.startswith("CRJ") or t.startswith("E1") or
                                t.startswith("E7") or t in {
                                    "MD80","MD82","MD83","MD88","MD90",
                                    "A220","A319","A320","A321","A330",
                                    "A340","A350","A380",
                                }):
                        cat = "Commercial"
                    elif t:
                        cat = "General Aviation"
                    else:
                        cat = "Unknown"
                    slot = by_cat.setdefault(cat, {
                        "icaos": set(), "sightings": 0,
                        "first": r["first_seen"], "last": r["last_seen"],
                    })
                    slot["icaos"].add(r["icao"])
                    slot["sightings"] += int(r["sightings"] or 0)
                    if r["first_seen"] < slot["first"]:
                        slot["first"] = r["first_seen"]
                    if r["last_seen"] > slot["last"]:
                        slot["last"] = r["last_seen"]
                for cat, slot in by_cat.items():
                    rows.append({
                        "category": cat,
                        "aircraft_count": len(slot["icaos"]),
                        "sightings": slot["sightings"],
                        "first_seen": slot["first"],
                        "last_seen": slot["last"],
                        "metric": len(slot["icaos"]),
                        "metric_label": f"{len(slot['icaos']):,}",
                        "seen_at": slot["first"],
                    })
                rows.sort(key=lambda x: -x["aircraft_count"])

            elif card == "all_countries":
                # v2.50.27: full country distribution. Country is computed
                # at query time from the ICAO 24-bit address via
                # countries.country_for_icao — same logic the top_countries
                # card face uses, so the numbers agree. Returns the full
                # distribution not capped at top 5. Row clicks route back
                # through the top_countries drill (handled above).
                from countries import country_for_icao
                country_raw = q("""
                    SELECT icao, callsign, aircraft_type,
                           MIN(seen_at) AS first_seen, MAX(seen_at) AS last_seen,
                           COUNT(*) AS sightings
                    FROM all_sightings
                    WHERE seen_at >= ?
                    GROUP BY icao
                """, (start_ts,))
                by_country = {}
                for r in country_raw:
                    name = country_for_icao(r["icao"])
                    if not name:
                        continue
                    slot = by_country.setdefault(name, {
                        "icaos": set(), "sightings": 0,
                        "first": r["first_seen"], "last": r["last_seen"],
                    })
                    slot["icaos"].add(r["icao"])
                    slot["sightings"] += int(r["sightings"] or 0)
                    if r["first_seen"] < slot["first"]:
                        slot["first"] = r["first_seen"]
                    if r["last_seen"] > slot["last"]:
                        slot["last"] = r["last_seen"]
                for name, slot in by_country.items():
                    rows.append({
                        "country": name,
                        "aircraft_count": len(slot["icaos"]),
                        "sightings": slot["sightings"],
                        "first_seen": slot["first"],
                        "last_seen": slot["last"],
                        "metric": len(slot["icaos"]),
                        "metric_label": f"{len(slot['icaos']):,}",
                        "seen_at": slot["first"],
                    })
                rows.sort(key=lambda x: -x["aircraft_count"])

            else:
                # Unknown card — not drillable in this release
                conn.close()
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Card '{card}' does not support drill-down"},
                )

            # --- v2.41.17: row cap + field enrichment for Option C drill panel ---
            #
            # Two goals:
            #
            #   1. Cap the response size. The Option C panel shows at most 25
            #      rows; sending 1000+ over the wire for every drill click is
            #      wasteful when the frontend will only render 25. Keep the full
            #      count as `total_count` so the frontend can render the
            #      "Showing 25 of 1019" strip.
            #
            #   2. Enrich each of the top-25 rows with altitude, speed, lat, lon,
            #      distance, and compass bearing from the record-setting
            #      sighting — the exact row that made this aircraft the furthest
            #      / fastest / highest / etc. This lets the drill panel show a
            #      rich 9-column table without the frontend having to issue
            #      per-row lookups.
            #
            # Why post-hoc enrichment rather than modifying each of the six
            # drill branches' SQL: keeps the change surgical (no SQL touches)
            # and isolates the extra work to just the rows the client will
            # actually render. Per-row lookups are fast because
            # idx_all_seen_icao covers (icao, seen_at) exactly.
            total_count = len(rows)
            # v2.66.0: cap at 25 unless caller asks for all. The "View
            # all N {types|etc}" button on Composition cards passes
            # all=true; the default panel render uses the cap for budget.
            if not bypass_cap:
                rows = rows[:25]

            for r in rows:
                seen_at = r.get("seen_at")
                icao = r.get("icao")
                if seen_at is None or not icao:
                    continue
                # The sighting that set this aircraft's record. Pick the first
                # row at that exact timestamp (most aircraft only have one).
                enrich_row = q(
                    """
                    SELECT altitude, speed, lat, lon
                    FROM all_sightings
                    WHERE icao = ? AND seen_at = ?
                    LIMIT 1
                    """,
                    (icao, seen_at),
                )
                if not enrich_row:
                    continue
                er = enrich_row[0]
                alt = _to_number(er["altitude"])
                spd = _to_number(er["speed"])
                lat = _to_number(er["lat"])
                lon = _to_number(er["lon"])
                if alt is not None:
                    r["altitude"] = int(alt)
                if spd is not None:
                    r["speed"] = int(spd)
                if lat is not None:
                    r["lat"] = lat
                if lon is not None:
                    r["lon"] = lon
                # Distance + bearing only make sense when we know where the
                # receiver is. Emit the numeric value and a pre-formatted label
                # (the frontend will use either, depending on context).
                if have_location and lat is not None and lon is not None:
                    try:
                        d = haversine(rx_lat, rx_lon, lat, lon, distance_unit)
                        r["distance"] = round(d, 1)
                        r["distance_label"] = f"{round(d, 1)} {distance_unit}"
                        brg = compass_bearing(rx_lat, rx_lon, lat, lon)
                        r["bearing_deg"] = int(round(brg))
                        r["bearing_label"] = f"{compass_label(brg)} ({int(round(brg))}°)"
                    except Exception:
                        # Don't let a single bad coord pair break the whole drill
                        pass

            conn.close()
            return {
                "card": card,
                "rows": rows,
                "count": len(rows),
                "total_count": total_count,
            }

        except Exception as e:
            logger.error(f"Drill query failed for card={card}: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})


    @app.get("/api/config")
    async def get_full_config():
        """Return the full current config as JSON."""
        return CONFIG

    class ConfigPayload(BaseModel):
        config: dict

    @app.put("/api/config")
    async def update_full_config(payload: ConfigPayload):
        """Validate and save a new config. Returns which keys changed and whether
        a service restart is required for the changes to fully take effect."""
        from config_validator import validate_config, diff_keys, requires_restart

        new_cfg = payload.config
        errors = validate_config(new_cfg)
        if errors:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "errors": [{"path": p, "message": m} for p, m in errors],
                },
            )

        # Diff against current config to determine what changed
        changed_paths = diff_keys(CONFIG, new_cfg)
        needs_restart = requires_restart(changed_paths)

        # v2.60.1 (Phase 1A.5 perf): detect receiver-location changes
        # before the in-memory swap so we can compare old vs new. If
        # either coordinate moved, trigger a full distance recompute
        # AFTER the swap (so the recompute reads the new location).
        # Distance unit changes don't need a recompute — the stored
        # km value is canonical and unit conversion happens at
        # display time.
        _old_rcv = (CONFIG.get("receiver") or {})
        _new_rcv = (new_cfg.get("receiver") or {})
        _receiver_moved = (
            _old_rcv.get("latitude") != _new_rcv.get("latitude") or
            _old_rcv.get("longitude") != _new_rcv.get("longitude")
        )

        # Apply in-memory for live keys
        for key in list(CONFIG.keys()):
            if key not in new_cfg:
                CONFIG.pop(key)
        CONFIG.update(new_cfg)

        # Persist to disk (preserving comments via ruamel.yaml if available)
        try:
            _save_config_preserving_comments()
        except Exception as e:
            logger.error(f"Failed to persist config: {e}")
            return JSONResponse(
                status_code=500,
                content={"ok": False, "errors": [{"path": "", "message": str(e)}]},
            )

        # Refresh the notifier with the new config. Cheap and idempotent;
        # always safe to call even if notifications.* didn't actually change.
        try:
            _refresh_notifier_config()
        except Exception as e:
            logger.warning(f"Notifier refresh failed (continuing): {e}")

        # v2.60.1: receiver-location-change → push the new coordinates
        # into the collector AND recompute every seen_aircraft.last_distance
        # so subsequent Search distance-sort queries return correct
        # results immediately. The recompute runs synchronously here
        # because the user is already waiting on a config save and the
        # cost is bounded (~7K UPDATEs, single-digit seconds on a Pi).
        # Wrapped in try/except so a recompute failure doesn't fail
        # the config save itself — the column would just be stale
        # until the next service restart triggers main.py's startup
        # recompute path.
        if _receiver_moved:
            try:
                from collector import set_receiver_location
                set_receiver_location(_new_rcv.get("latitude"),
                                       _new_rcv.get("longitude"))
                _recompute_all_last_distance(
                    CONFIG["data"]["db_file"],
                    rlat=_new_rcv.get("latitude"),
                    rlon=_new_rcv.get("longitude"),
                )
            except Exception as e:
                logger.warning(f"Receiver-location distance recompute failed "
                               f"(distance sort may be stale until restart): {e}")

        return {
            "ok": True,
            "changed": changed_paths,
            "needs_restart": needs_restart,
        }

    # --- Config backup / restore / import / export ---
    # Safe, robust filename pattern for auto-backups: "config.yaml.bak.YYYYMMDD-HHMMSS"
    _BACKUP_NAME_RE = re.compile(r"^config\.yaml\.bak\.[A-Za-z0-9_\-]{1,64}$")

    # How many .backups/<timestamp>/ install snapshots to retain after an update.
    INSTALL_BACKUP_KEEP = 5

    def _prune_install_backups():
        """Keep only the INSTALL_BACKUP_KEEP most-recent install snapshots in
        .backups/. Each snapshot is a folder containing the pre-update source
        files (copied by apply_local_update)."""
        try:
            install_dir = Path(__file__).parent
            backups_root = install_dir / ".backups"
            if not backups_root.is_dir():
                return
            # Only consider top-level directories with timestamp-like names
            snapshots = [
                p for p in backups_root.iterdir()
                if p.is_dir() and re.match(r"^\d{8}-\d{6}$", p.name)
            ]
            # Sort newest first (lexical works because timestamps are YYYYMMDD-HHMMSS)
            snapshots.sort(key=lambda p: p.name, reverse=True)
            for old in snapshots[INSTALL_BACKUP_KEEP:]:
                try:
                    shutil.rmtree(old)
                    logger.info(f"Pruned old install backup: {old.name}")
                except OSError as e:
                    logger.warning(f"Could not delete old install backup {old.name}: {e}")
        except Exception as e:
            logger.warning(f"Could not prune install backups: {e}")

    def _do_restart():
        """Trigger a systemd restart. Returns (ok, note).

        Tries `systemctl restart --no-block aerodrome` first (preferred — returns
        immediately, doesn't race with our own shutdown). Falls back to the plain
        `systemctl restart aerodrome` if sudo rejects the --no-block variant,
        which happens on installs whose sudoers rule predates this feature.

        A non-zero return with empty stderr is treated as success — that's the
        expected case when systemd kills us before the subprocess finishes."""
        import subprocess
        cmds = [
            ["sudo", "-n", "systemctl", "restart", "--no-block", "aerodrome"],
            ["sudo", "-n", "systemctl", "restart", "aerodrome"],
        ]
        last_note = ""
        for cmd in cmds:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    return True, ""
                err = (result.stderr or "").strip()
                # If sudo blocked us because of the rule not matching, try the next variant
                low = err.lower()
                if err and ("not allowed" in low or "password is required" in low
                            or "sudo:" in low and "command" in low):
                    last_note = err
                    continue
                # Non-zero but no stderr — mid-request kill, treat as success
                if not err:
                    return True, ""
                # Some other kind of failure — real note to surface
                return False, err
            except subprocess.TimeoutExpired:
                # Expected when the service kills us mid-call
                return True, ""
            except FileNotFoundError:
                return False, "systemctl not found"
            except Exception as e:
                last_note = str(e)
                continue
        return False, last_note

    def _list_config_backups():
        """Return sorted list of backup metadata (newest first)."""
        install_dir = Path(__file__).parent
        items = []
        for p in install_dir.glob("config.yaml.bak.*"):
            if not p.is_file():
                continue
            # v2.50.6: pre-restore safety snapshots are managed separately
            # (see the "Restore safety snapshots" section in the Backup &
            # Restore UI and the /api/backup/pre-restore endpoints). Filter
            # them out of the regular config-only auto-backup list to avoid
            # showing the same snapshot in two places.
            if p.name.endswith(".pre-restore"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            # Try to pull version from the file content (lightweight — just look at first ~300 bytes)
            version = None
            try:
                head = p.read_text(errors="replace")[:400]
                m = re.search(r"^\s*#\s*Version:\s*([0-9][0-9.a-zA-Z\-]*)\s*$", head, re.MULTILINE)
                if m:
                    version = m.group(1)
            except Exception:
                pass
            items.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "mtime": int(st.st_mtime),
                "version": version,
            })
        items.sort(key=lambda e: e["name"], reverse=True)
        return items

    @app.get("/api/config/backups")
    async def list_config_backups():
        """Return all config.yaml.bak.* files with metadata."""
        return {"backups": _list_config_backups()}

    @app.get("/api/config/db-tuning")
    async def get_db_tuning_status():
        """v2.50.14: surface what the SQLite tuning auto-detect resolves
        to on this hardware, so the Configuration UI can render a status
        line under the dropdown like 'Auto resolves to Balanced (3.8 GB
        RAM detected)'. Without this, 'Auto (recommended)' is opaque —
        users can't tell whether to step up to Aggressive or down to
        Conservative because they don't know where they're starting from.

        Returns the resolved auto-pick along with system memory and the
        pragma values bundled in each profile, so the frontend can
        render exactly what's being applied without hardcoding the
        profile table on its end."""
        try:
            import collector as _collector_mod
            auto_pick = _collector_mod._detect_auto_db_profile()
            profiles = _collector_mod.TUNING_PROFILES
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"Could not introspect tuning: {e}"
            })

        # Read /proc/meminfo for the GB number — same source of truth
        # the auto-detect uses, so what we display matches what we'd pick.
        mem_gb = None
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        mem_gb = round(kb / (1024 * 1024), 1)
                        break
        except Exception:
            pass

        return {
            "ok": True,
            "auto_resolves_to": auto_pick,
            "system_memory_gb": mem_gb,
            "profiles": profiles,
        }

    @app.get("/api/config/backup/{name}", response_class=PlainTextResponse)
    async def read_config_backup(name: str):
        """Return the raw text of a specific backup so the frontend can preview it."""
        if not _BACKUP_NAME_RE.match(name):
            return PlainTextResponse("Invalid backup name", status_code=400)
        path = Path(__file__).parent / name
        if not path.is_file():
            return PlainTextResponse("Backup not found", status_code=404)
        try:
            return PlainTextResponse(path.read_text(errors="replace"))
        except Exception as e:
            return PlainTextResponse(f"Error reading backup: {e}", status_code=500)

    @app.get("/api/config/export", response_class=Response)
    async def export_config():
        """Download the current config.yaml as an attachment."""
        path = Path(CONFIG_PATH)
        if not path.is_file():
            return JSONResponse(status_code=404, content={"ok": False, "error": "config.yaml not found"})
        try:
            content = path.read_text()
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
        ts = time.strftime("%Y%m%d-%H%M%S")
        filename = f"config.yaml.{ts}"
        return Response(
            content=content,
            media_type="application/x-yaml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _apply_config_from_text(new_text: str, source_label: str):
        """Validate the supplied YAML text, back up the current config, then
        replace it. Reloads CONFIG in memory. Returns (ok, payload)."""
        from config_validator import validate_config

        # Parse YAML — reject on any parse error
        try:
            parsed = yaml.safe_load(new_text)
        except yaml.YAMLError as e:
            return False, {"ok": False, "error": f"YAML parse error: {e}"}

        if not isinstance(parsed, dict):
            return False, {"ok": False, "error": "Config must be a YAML mapping"}

        # Validate — reject if the new config has any errors
        errors = validate_config(parsed)
        if errors:
            return False, {
                "ok": False,
                "error": "Validation failed",
                "errors": [{"path": p, "message": m} for p, m in errors],
            }

        # Back up current config
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_name = f"config.yaml.bak.{ts}"
        backup_path = Path(CONFIG_PATH).with_name(backup_name)
        try:
            shutil.copy2(CONFIG_PATH, backup_path)
        except Exception as e:
            return False, {"ok": False, "error": f"Could not back up current config: {e}"}

        # Write the new config
        try:
            Path(CONFIG_PATH).write_text(new_text)
        except Exception as e:
            return False, {"ok": False, "error": f"Could not write new config: {e}"}

        # Reload into in-memory CONFIG so subsequent API calls see the new values
        for k in list(CONFIG.keys()):
            CONFIG.pop(k, None)
        CONFIG.update(parsed)

        logger.info(f"Config replaced from {source_label}; prior config saved as {backup_name}")

        # v2.50.9: this save just created a fresh auto-backup. Trim older
        # ones to honor the keep-5 retention the UI advertises. Best-effort.
        try:
            pruned, _freed = _prune_config_auto_backups()
            if pruned:
                logger.info(f"Pruned {pruned} older config auto-backup(s)")
        except Exception as e:
            logger.warning(f"Could not prune config auto-backups: {e}")

        return True, {
            "ok": True,
            "backup_name": backup_name,
            "message": f"Config replaced from {source_label}. Previous config saved as {backup_name}.",
        }

    @app.post("/api/config/restore/{name}")
    async def restore_config_backup(name: str):
        """Replace config.yaml with the contents of a specific backup.
        Backs up the current config first."""
        if not _BACKUP_NAME_RE.match(name):
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid backup name"})
        src = Path(__file__).parent / name
        if not src.is_file():
            return JSONResponse(status_code=404, content={"ok": False, "error": "Backup not found"})
        try:
            text = src.read_text()
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

        ok, payload = _apply_config_from_text(text, f"backup {name}")
        if not ok:
            return JSONResponse(status_code=400, content=payload)

        # Trigger restart via helper (handles --no-block + fallback)
        restart_ok, note = _do_restart()
        if not restart_ok and note:
            payload["restart_note"] = note

        return payload

    @app.post("/api/config/import")
    async def import_config(file: UploadFile = File(...)):
        """Upload a user-supplied config.yaml, validate, back up current, replace, restart."""
        try:
            raw = await file.read()
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": f"Upload read failed: {e}"})

        # Sanity: reject very large files (>256 KB is plenty for a config)
        if len(raw) > 256 * 1024:
            return JSONResponse(status_code=413, content={
                "ok": False,
                "error": "Uploaded file is too large (limit: 256 KB)"
            })

        ok, payload = _apply_config_from_text(text, f"upload {file.filename!r}")
        if not ok:
            return JSONResponse(status_code=400, content=payload)

        # Trigger restart via helper (handles --no-block + fallback)
        restart_ok, note = _do_restart()
        if not restart_ok and note:
            payload["restart_note"] = note

        return payload

    # --- Full backup / restore (v2.41.4) ---
    # Bundles config.yaml + aerodrome.db + ntfy server.yml into a single zip
    # for disaster recovery and migration. Distinct from /api/config/export
    # which is narrow (just config.yaml, simple download). Restore is
    # destructive — overwrites config, DB, and ntfy config, then restarts.
    #
    # Manifest format (v1):
    #   manifest.json  - version info, timestamps, which files are included
    #   config.yaml    - always included
    #   aerodrome.db   - included if DB file exists and is readable
    #   ntfy/server.yml - included if ntfy is aerodrome-managed
    #   VERSION        - Aerodrome version that produced the backup
    #
    # Known limitations (documented inline):
    #   - cache.db from ntfy is not backed up (large, private, rarely useful)
    #   - base_url in the restored ntfy server.yml may reference the OLD
    #     server's LAN IP; user needs to update it via the Notifications tab
    #   - restoring ntfy's server.yml requires ntfy to be installed on the
    #     target machine; we skip it with a warning if not

    @app.get("/api/backup/export")
    async def backup_export():
        """Build + stream a zip containing config + DB + ntfy config.
        Size depends on the database; can be large (100MB+ for a year of
        history). Streamed rather than built-in-memory to avoid OOM."""
        import io, json, zipfile
        from fastapi.responses import StreamingResponse

        install_dir = Path(__file__).parent
        db_path = Path(CONFIG.get("data", {}).get("db_file", "aircraft_history.db"))
        if not db_path.is_absolute():
            db_path = install_dir / db_path

        # Decide what goes in the bundle upfront so the manifest is accurate
        manifest = {
            "manifest_version": 1,
            "aerodrome_version": (install_dir / "VERSION").read_text().strip()
                if (install_dir / "VERSION").exists() else "unknown",
            "created_at": int(time.time()),
            "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "includes": {
                "config": Path(CONFIG_PATH).is_file(),
                "database": db_path.is_file(),
                "ntfy_config": False,  # populated below
            },
            "notes": [
                "Restore with POST /api/backup/import (multipart file upload).",
                "The service will restart after restore.",
                "ntfy server.yml is included only when ntfy is aerodrome-managed.",
                "ntfy cache.db is NOT backed up — reinstall ntfy to get a fresh cache.",
            ],
        }

        # Check ntfy state
        ntfy_config_text: Optional[str] = None
        try:
            from ntfy_installer import install_status, CONFIG_FILE as NTFY_CONFIG_FILE
            ns = install_status()
            if ns.get("state") == "aerodrome_managed" and NTFY_CONFIG_FILE.is_file():
                ntfy_config_text = NTFY_CONFIG_FILE.read_text()
                manifest["includes"]["ntfy_config"] = True
                manifest["ntfy_version"] = ns.get("version")
        except Exception as e:
            logger.warning(f"Could not include ntfy config in backup: {e}")

        # Build the zip in memory — database could be large but streaming
        # SQLite snapshots is nontrivial; for now, hold in memory and let
        # the user see the Content-Length up front. If this becomes a
        # problem we revisit with SQLite's backup API + streaming.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            if manifest["includes"]["config"]:
                zf.write(CONFIG_PATH, arcname="config.yaml")
            if manifest["includes"]["database"]:
                # Use SQLite's online backup API to snapshot a live WAL
                # database safely. A naive file copy could race with
                # writes and produce a corrupt backup.
                try:
                    import sqlite3, tempfile
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                        tmp_path = tmp.name
                    try:
                        src = _open_db_conn(str(db_path))
                        dst = _open_db_conn(tmp_path)
                        with dst:
                            src.backup(dst)
                        src.close()
                        dst.close()
                        zf.write(tmp_path, arcname="aerodrome.db")
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                except Exception as e:
                    logger.error(f"Database snapshot failed: {e}")
                    # Don't abort — user still gets config at least
                    manifest["includes"]["database"] = False
                    # Rewrite manifest to reflect reality
                    zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            if ntfy_config_text is not None:
                zf.writestr("ntfy/server.yml", ntfy_config_text)
            if (install_dir / "VERSION").exists():
                zf.write(install_dir / "VERSION", arcname="VERSION")

        buf.seek(0)
        ts = time.strftime("%Y%m%d-%H%M%S")
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="aerodrome-backup-{ts}.zip"',
                "Content-Length": str(buf.getbuffer().nbytes),
            },
        )

    @app.get("/api/backup/preview")
    async def backup_preview():
        """Return what the backup WOULD contain, without actually building
        the zip. Used to show file sizes + warnings before the user clicks
        Download — databases can be large and we want to set expectations."""
        install_dir = Path(__file__).parent
        db_path = Path(CONFIG.get("data", {}).get("db_file", "aircraft_history.db"))
        if not db_path.is_absolute():
            db_path = install_dir / db_path

        def _size_of(p: Path) -> Optional[int]:
            try:
                return p.stat().st_size if p.is_file() else None
            except OSError:
                return None

        items = []
        total_bytes = 0
        cfg_size = _size_of(Path(CONFIG_PATH))
        if cfg_size is not None:
            items.append({"name": "config.yaml", "bytes": cfg_size, "required": True})
            total_bytes += cfg_size
        db_size = _size_of(db_path)
        if db_size is not None:
            items.append({"name": "aerodrome.db", "bytes": db_size, "required": False,
                          "note": "Snapshotted via SQLite backup API (safe with live service)"})
            total_bytes += db_size

        ntfy_available = False
        ntfy_size = None
        try:
            from ntfy_installer import install_status, CONFIG_FILE as NTFY_CONFIG_FILE
            ns = install_status()
            if ns.get("state") == "aerodrome_managed" and NTFY_CONFIG_FILE.is_file():
                ntfy_available = True
                ntfy_size = _size_of(NTFY_CONFIG_FILE)
                if ntfy_size:
                    items.append({"name": "ntfy/server.yml", "bytes": ntfy_size,
                                  "required": False})
                    total_bytes += ntfy_size
        except Exception:
            pass

        # Warnings to surface in the UI
        warnings = []
        if db_size and db_size > 50 * 1024 * 1024:  # 50MB
            warnings.append(f"Database is {_fmt_bytes(db_size)} — the download "
                            "may take a while and use non-trivial memory on the server.")
        if not ntfy_available:
            warnings.append("ntfy is not Aerodrome-managed, so its config won't be "
                            "included. Not a problem unless you use a local ntfy.")

        return {
            "items": items,
            "approximate_total_bytes": total_bytes,
            "warnings": warnings,
            "manifest_version": 1,
        }

    class BackupRestoreResult(BaseModel):
        ok: bool
        message: str
        restored: Dict[str, Any]
        skipped: List[Dict[str, Any]]
        warnings: List[str]
        restart_note: Optional[str] = None

    @app.post("/api/backup/import")
    async def backup_import(file: UploadFile = File(...)):
        """Restore from a zip produced by /api/backup/export. Destructive:
        replaces config.yaml, aerodrome.db, and ntfy server.yml (when present).
        Backs up the existing versions of each file first, then triggers a
        service restart so the new DB/config take effect."""
        import io, json, zipfile, tempfile

        raw = await file.read()
        if not raw:
            return JSONResponse(status_code=400, content={
                "ok": False, "message": "Uploaded file is empty"
            })
        # Hard cap at 2 GB — anything bigger is almost certainly wrong
        if len(raw) > 2 * 1024 * 1024 * 1024:
            return JSONResponse(status_code=413, content={
                "ok": False, "message": "Backup too large (>2 GB)"
            })

        try:
            zf = zipfile.ZipFile(io.BytesIO(raw), "r")
        except zipfile.BadZipFile:
            return JSONResponse(status_code=400, content={
                "ok": False, "message": "Not a valid zip file"
            })

        # v2.49.8: detect a single wrapper folder around the backup contents.
        # Aerodrome's own backup writer puts files at the zip root, but cloud
        # sync tools (Synology Drive, Google Drive, OneDrive) sometimes
        # re-package downloaded zip contents under a folder named after the
        # backup. The result is entries like "aerodrome-backup-20260427-191125/manifest.json"
        # instead of just "manifest.json". When that happens we want to
        # accept the backup rather than fail with a confusing "missing
        # manifest" error — the file IS in the archive, just one level
        # deeper than expected.
        #
        # Strategy: find manifest.json wherever it is in the archive. If
        # it has a directory prefix, use that prefix for all subsequent
        # reads. If we find more than one manifest.json, that's a malformed
        # archive and we refuse it. If we find none at any depth, that's
        # a real "missing manifest" condition.
        manifest_candidates = [
            n for n in zf.namelist()
            if n == "manifest.json" or n.endswith("/manifest.json")
        ]
        if len(manifest_candidates) == 0:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "message": "Missing manifest.json: archive does not contain a manifest at any depth.",
            })
        if len(manifest_candidates) > 1:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "message": f"Ambiguous backup: multiple manifest.json files found at "
                           f"{manifest_candidates}. Aerodrome can only restore from a "
                           f"single-backup archive.",
            })
        manifest_path = manifest_candidates[0]
        # Derive the prefix (everything before "manifest.json") so we can
        # apply it to every other read. Empty string when manifest is at root.
        prefix = manifest_path[: -len("manifest.json")]
        if prefix:
            logger.info(f"Backup archive has wrapper folder prefix: {prefix!r}")

        # Read + validate manifest first — we refuse to apply a backup
        # from a wildly different manifest version we don't understand.
        try:
            manifest_bytes = zf.read(manifest_path)
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as e:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "message": f"Missing or invalid manifest.json: {e}",
            })

        if manifest.get("manifest_version") != 1:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "message": f"Unsupported manifest version "
                           f"{manifest.get('manifest_version')}. "
                           f"This Aerodrome only handles version 1 backups.",
            })

        install_dir = Path(__file__).parent
        db_path = Path(CONFIG.get("data", {}).get("db_file", "aircraft_history.db"))
        if not db_path.is_absolute():
            db_path = install_dir / db_path

        restored: Dict[str, Any] = {}
        skipped: List[Dict[str, Any]] = []
        warnings: List[str] = []
        ts = time.strftime("%Y%m%d-%H%M%S")

        # --- 1. config.yaml ---
        try:
            cfg_bytes = zf.read(prefix + "config.yaml")
            cfg_text = cfg_bytes.decode("utf-8")
            # Back up the current config before overwriting
            cfg_backup_name = f"config.yaml.bak.{ts}.pre-restore"
            cfg_backup_path = Path(CONFIG_PATH).with_name(cfg_backup_name)
            if Path(CONFIG_PATH).is_file():
                shutil.copy2(CONFIG_PATH, cfg_backup_path)
            Path(CONFIG_PATH).write_text(cfg_text)
            # Reload in-memory CONFIG so subsequent calls see new values
            # (restart picks it up properly, but this keeps the next few
            # API calls coherent while restart is queued)
            try:
                parsed = yaml.safe_load(cfg_text) or {}
                if isinstance(parsed, dict):
                    for k in list(CONFIG.keys()):
                        CONFIG.pop(k, None)
                    CONFIG.update(parsed)
            except Exception:
                pass
            restored["config"] = {"ok": True, "bytes": len(cfg_bytes),
                                  "previous_backed_up_as": cfg_backup_name}
        except KeyError:
            skipped.append({"file": "config.yaml", "reason": "not in backup"})
        except Exception as e:
            skipped.append({"file": "config.yaml", "reason": f"error: {e}"})

        # --- 2. aerodrome.db ---
        try:
            db_bytes = zf.read(prefix + "aerodrome.db")
            # Back up current DB
            db_backup_path = None
            if db_path.is_file():
                db_backup_path = db_path.with_name(
                    f"{db_path.name}.bak.{ts}.pre-restore")
                shutil.copy2(db_path, db_backup_path)
            # Also move any stale WAL/SHM aside so SQLite doesn't mix old
            # WAL entries into the restored DB.
            for suffix in ("-wal", "-shm"):
                sidecar = db_path.with_name(db_path.name + suffix)
                if sidecar.is_file():
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
            db_path.write_bytes(db_bytes)
            restored["database"] = {
                "ok": True, "bytes": len(db_bytes),
                "previous_backed_up_as": (db_backup_path.name if db_backup_path else None),
            }
        except KeyError:
            skipped.append({"file": "aerodrome.db", "reason": "not in backup"})
        except Exception as e:
            skipped.append({"file": "aerodrome.db", "reason": f"error: {e}"})

        # --- 3. ntfy/server.yml ---
        try:
            ntfy_bytes = zf.read(prefix + "ntfy/server.yml")
            # Only restore if ntfy is actually installed here AND managed
            # by Aerodrome (same gate as export). Otherwise save the bytes
            # to the install dir so the user can manually move them later.
            try:
                from ntfy_installer import install_status, CONFIG_FILE as NTFY_CONFIG_FILE, _sudo_run
                ns = install_status()
                if ns.get("state") == "aerodrome_managed":
                    # Write through sudo — same path the installer uses
                    _sudo_run(["tee", str(NTFY_CONFIG_FILE)],
                              input_text=ntfy_bytes.decode("utf-8"))
                    # Restart ntfy to pick up the new config
                    try:
                        _sudo_run(["systemctl", "restart", "ntfy"])
                    except RuntimeError as e:
                        warnings.append(f"ntfy config restored but restart failed: {e}")
                    restored["ntfy_config"] = {"ok": True, "bytes": len(ntfy_bytes)}
                    warnings.append(
                        "ntfy base_url in the restored config may reference the OLD "
                        "server's LAN IP. Check it in the Notifications tab if your "
                        "phones can't reach ntfy after restore."
                    )
                else:
                    # ntfy not aerodrome-managed — save bytes to install dir
                    # for the user to apply manually later
                    stash_path = install_dir / f"ntfy-server.yml.from-backup.{ts}"
                    stash_path.write_bytes(ntfy_bytes)
                    skipped.append({
                        "file": "ntfy/server.yml",
                        "reason": f"ntfy is not Aerodrome-managed on this system "
                                  f"(state={ns.get('state')!r}). Saved to "
                                  f"{stash_path.name} for manual review.",
                    })
            except ImportError:
                skipped.append({"file": "ntfy/server.yml",
                                "reason": "ntfy_installer module unavailable"})
            except Exception as e:
                skipped.append({"file": "ntfy/server.yml", "reason": f"error: {e}"})
        except KeyError:
            pass  # not in backup, not a problem

        # v2.50.6: this restore just created a fresh pre-restore pair. Trim
        # older snapshots so they don't accumulate forever — at scale the DB
        # snapshot alone can be hundreds of MB to GB per restore. Best-effort:
        # any errors are logged but never block the success response.
        try:
            pruned_count, pruned_bytes = _prune_pre_restore_snapshots()
            if pruned_count:
                logger.info(
                    f"Pruned {pruned_count} older pre-restore snapshot(s), "
                    f"freed {pruned_bytes} bytes"
                )
        except Exception as e:
            logger.warning(
                f"Could not prune older pre-restore snapshots: {e}"
            )

        # --- 4. Trigger restart ---
        restart_ok, restart_note = _do_restart()
        if not restart_ok and restart_note:
            warnings.append(f"Restore applied but restart failed: {restart_note}. "
                            "Restart manually with `sudo systemctl restart aerodrome`.")

        return {
            "ok": True,
            "message": "Restore completed. Service is restarting.",
            "manifest": manifest,
            "restored": restored,
            "skipped": skipped,
            "warnings": warnings,
            "restart_note": restart_note if not restart_ok else None,
        }

    # --- Pre-restore safety snapshots (v2.50.6) ---
    # Each Restore through /api/backup/import drops a copy of the previous
    # config.yaml and aircraft_history.db into the install dir with a
    # ".pre-restore" suffix as a one-click undo target. Auto-pruning keeps
    # the most recent _PRE_RESTORE_KEEP per kind; these endpoints back the
    # UI that surfaces the snapshots and lets the user purge them all.

    @app.get("/api/backup/pre-restore")
    async def list_pre_restore():
        """List pre-restore safety snapshots paired by timestamp. Newest
        first. Returns total bytes used and the active keep-N policy so
        the UI can show context."""
        snapshots = _list_pre_restore_snapshots()
        total_bytes = sum(s["total_bytes"] for s in snapshots)
        return {
            "ok": True,
            "snapshots": snapshots,
            "count": len(snapshots),
            "total_bytes": total_bytes,
            "keep": _PRE_RESTORE_KEEP,
        }

    @app.post("/api/backup/pre-restore/purge")
    async def purge_pre_restore():
        """Delete every pre-restore snapshot file. Useful when the keep-N
        retention (default 3) is more conservative than the user wants and
        they'd rather reclaim every byte. Future restores will recreate
        the most recent snapshot anyway."""
        try:
            deleted, freed = _purge_all_pre_restore_snapshots()
            return {
                "ok": True,
                "deleted_count": deleted,
                "freed_bytes": freed,
                "message": f"Purged {deleted} pre-restore file(s), "
                           f"freed {freed} bytes.",
            }
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": f"Purge failed: {e}",
            })

    # --- Performance diagnostics (v2.41.8) ---
    # Single endpoint that returns a complete snapshot of what matters for
    # performance on constrained hardware: DB size + row counts + index
    # coverage + timing on a handful of representative queries + SQLite
    # pragmas + system info. Intended to be pastable into a GitHub issue
    # so we don't have to play 20 questions when someone says "it's slow."
    #
    # Running this is safe (read-only queries, no schema changes) and cheap
    # (all queries are bounded and most already run in the normal API path).
    # The big-window count query can take seconds on a huge DB, which is
    # itself a useful data point — we time it and report it rather than
    # try to avoid it.

    @app.get("/api/perf/diagnostics")
    async def perf_diagnostics(include_legacy: bool = False):
        """Performance diagnostic snapshot. Returns storage footprint,
        query timings, index coverage, and system context.

        v2.50.26: include_legacy query parameter (default False) controls
        whether the legacy raw-fallback and window-functions reference
        probes are timed. On large installs those three probes account
        for ~97% of total runtime (the Pi user reported ~150 sec total,
        of which ~143 sec was the two raw probes alone). Skipping them
        by default brings the routine perf-diag from minutes down to a
        few seconds while preserving the option to compare against the
        legacy paths when intentionally requested. The frontend exposes
        this as a checkbox; power users can also hit
        /api/perf/diagnostics?include_legacy=true directly."""
        import platform, sqlite3 as sq
        db_path_str = CONFIG["data"]["db_file"]
        db_path = Path(db_path_str)
        if not db_path.is_absolute():
            db_path = Path(__file__).parent / db_path
        install_dir = Path(__file__).parent

        report: Dict[str, Any] = {
            "ok": True,
            "generated_at": int(time.time()),
            "aerodrome_version": (install_dir / "VERSION").read_text().strip()
                if (install_dir / "VERSION").exists() else "unknown",
            "sqlite_version": sq.sqlite_version,
            "python_version": platform.python_version(),
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),  # armv7l / aarch64 / x86_64
                "processor": platform.processor() or "unknown",
                "release": platform.release(),
            },
        }

        # --- Storage footprint ---
        storage = {"db_file": str(db_path)}
        try:
            st = db_path.stat()
            storage["size_bytes"] = st.st_size
            storage["size_human"] = _fmt_bytes(st.st_size)
            # WAL + SHM sidecars
            for suffix in ("-wal", "-shm"):
                side = db_path.with_name(db_path.name + suffix)
                if side.is_file():
                    ss = side.stat()
                    storage["size_bytes"] += ss.st_size
                    storage[f"{suffix[1:]}_bytes"] = ss.st_size
        except OSError as e:
            storage["error"] = str(e)
        report["storage"] = storage

        # --- Per-table row counts + timestamp range ---
        # Uses COUNT(*) which on SQLite scans the full table — that cost is
        # itself informative. We time each so the user sees how long
        # "count all rows" takes on their hardware.
        tables_info = []
        try:
            conn = _open_db_conn(str(db_path))
            conn.row_factory = sq.Row

            # Pragmas that matter for performance
            pragmas = {}
            for p in ("journal_mode", "page_size", "page_count", "cache_size",
                      "wal_autocheckpoint", "synchronous", "temp_store",
                      "auto_vacuum", "mmap_size"):
                try:
                    row = conn.execute(f"PRAGMA {p}").fetchone()
                    if row is not None:
                        pragmas[p] = row[0]
                except sq.DatabaseError:
                    pass
            # Derived: logical DB size reported by SQLite (page_size * page_count)
            if "page_size" in pragmas and "page_count" in pragmas:
                try:
                    pragmas["logical_size_bytes"] = int(pragmas["page_size"]) * int(pragmas["page_count"])
                except (TypeError, ValueError):
                    pass
            report["pragmas"] = pragmas

            # v2.52.1: surface the configured tuning profile alongside the
            # raw pragmas. The pragmas show what's *currently in effect*;
            # the profile shows what the user *requested*. They diverge in
            # informative ways: e.g. profile=auto with cache_size=-32768
            # tells us auto-detection chose the conservative 32MB cache,
            # which on Pi-class installs may be too small for big rollup
            # workloads. The Pi user requested this addition after seeing
            # the v2.51.1 perf-diag.
            tuning_cfg = (CONFIG.get("data") or {}).get("tuning") or {}
            report["config"] = {
                "tuning_profile": tuning_cfg.get("profile") or "auto",
                "tuning_overrides": {
                    k: v for k, v in tuning_cfg.items() if k != "profile"
                } if tuning_cfg else {},
            }

            # Table inventory.
            # v2.80.0 (Phase 4 perf-diag revisit): expanded from the
            # original 5 sighting tables to cover the full set of user-
            # facing tables. The pre-v2.80.0 inventory missed
            # sightings_hourly (v2.50.0 rollup, the production hot
            # table for /api/all and Search counts), hexdb_cache and
            # hexdb_events (v2.49.0 caching infrastructure), and the
            # FTS5 shadow tables that back Search. On a typical install
            # those 4 omitted tables can account for 15-30% of the DB
            # file size — the perf-diag's "where did my disk go?"
            # answer was incomplete.
            #
            # Each table reports rows, count_ms, and size_bytes (when
            # available via dbstat virtual table). Sighting tables also
            # report timestamp range; hexdb_events uses `ts` as its
            # range column, others use seen_at / first_seen_at /
            # hour_bucket as appropriate.
            TABLE_SPECS = [
                # (name, ts_column_or_None, ts_is_bucket)
                ("all_sightings",       "seen_at",       False),
                ("military_sightings",  "seen_at",       False),
                ("watchlist_sightings", "seen_at",       False),
                ("seen_aircraft",       "first_seen_at", False),
                ("sightings_hourly",    "hour_bucket",   True),
                ("hexdb_cache",         "resolved_at",   False),
                ("hexdb_events",        "ts",            False),
                ("stats_records",       None,            False),
                # FTS5 virtual table — has its own table, plus shadow
                # tables (data/idx/content/docsize/config) that SQLite
                # auto-creates. The COUNT(*) on the virtual table
                # reflects rows; the size aggregation below covers
                # the shadows.
                ("seen_aircraft_fts",   None,            False),
            ]
            for table, ts_col, ts_is_bucket in TABLE_SPECS:
                info: Dict[str, Any] = {"name": table}
                t0 = time.time()
                try:
                    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                    info["rows"] = row["n"]
                    info["count_ms"] = round((time.time() - t0) * 1000, 1)
                except sq.DatabaseError as e:
                    # Table might not exist on very old installs (pre-
                    # v2.49.0 hexdb tables, pre-v2.50.0 sightings_hourly,
                    # pre-v2.51.0 FTS5). Surface as an error rather than
                    # crashing the whole diag — installs in different
                    # migration states should still get a useful report.
                    info["error"] = str(e)
                # Timestamp range. ts_is_bucket=True means the column
                # is a precomputed hour-aligned timestamp (sightings_hourly
                # uses hour_bucket = unix_ts // 3600 * 3600); the
                # display logic is the same.
                if ts_col:
                    try:
                        r = conn.execute(
                            f"SELECT MIN({ts_col}) AS mn, MAX({ts_col}) AS mx FROM {table}"
                        ).fetchone()
                        info["oldest_ts"] = r["mn"]
                        info["newest_ts"] = r["mx"]
                        if r["mn"] and r["mx"]:
                            info["span_days"] = round((r["mx"] - r["mn"]) / 86400, 1)
                    except sq.DatabaseError:
                        pass
                tables_info.append(info)

            # FTS5 shadow tables — SQLite creates these automatically
            # for every FTS5 virtual table. They store the inverted
            # index, content cache, and config. On busy installs the
            # _data shadow can be substantial. We aggregate them as
            # one synthetic row in the inventory rather than listing
            # each shadow separately, because users care about "how
            # much disk is FTS5 costing me" more than the per-shadow
            # breakdown.
            try:
                fts_shadows = ("seen_aircraft_fts_data",
                               "seen_aircraft_fts_idx",
                               "seen_aircraft_fts_content",
                               "seen_aircraft_fts_docsize",
                               "seen_aircraft_fts_config")
                shadow_total_bytes = 0
                shadow_present = []
                for sh in fts_shadows:
                    try:
                        # dbstat is a virtual table that exposes per-
                        # btree page counts. Available on SQLite >=
                        # 3.7.9 (compiled with SQLITE_ENABLE_DBSTAT_VTAB,
                        # which the manylinux wheels include). If
                        # missing, the except branch falls through.
                        r = conn.execute(
                            "SELECT SUM(pgsize) AS b FROM dbstat WHERE name = ?",
                            (sh,)
                        ).fetchone()
                        if r and r["b"]:
                            shadow_total_bytes += int(r["b"])
                            shadow_present.append(sh)
                    except sq.DatabaseError:
                        # Shadow doesn't exist or dbstat unavailable;
                        # skip silently.
                        pass
                if shadow_present:
                    tables_info.append({
                        "name": "seen_aircraft_fts (shadows)",
                        "rows": None,
                        "size_bytes": shadow_total_bytes,
                        "shadow_tables": shadow_present,
                        "note": (f"FTS5 backing storage across "
                                 f"{len(shadow_present)} shadow tables"),
                    })
            except Exception as e:
                logger.debug(f"FTS5 shadow size lookup failed: {e}")

            # Per-table size_bytes via dbstat. Done as a second pass so
            # the COUNT(*) timing above isn't padded by dbstat overhead.
            # dbstat aggregates btree pages including indexes; we want
            # just the table's primary btree, which is identified by
            # name == table_name.
            try:
                for info in tables_info:
                    if "shadow_tables" in info:
                        continue  # already populated above
                    name = info["name"]
                    try:
                        r = conn.execute(
                            "SELECT SUM(pgsize) AS b FROM dbstat WHERE name = ?",
                            (name,)
                        ).fetchone()
                        if r and r["b"]:
                            info["size_bytes"] = int(r["b"])
                    except sq.DatabaseError:
                        pass
            except Exception as e:
                logger.debug(f"per-table size lookup failed: {e}")

            # Indexes present, for coverage verification
            index_rows = conn.execute(
                "SELECT name, tbl_name FROM sqlite_master "
                "WHERE type='index' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY tbl_name, name"
            ).fetchall()
            report["indexes"] = [
                {"name": r["name"], "table": r["tbl_name"]} for r in index_rows
            ]

            # --- Representative query timings ---
            # We run the same queries the UI fires, at the scale the user's
            # config asks for, so the numbers match what they'd actually see.
            now = int(time.time())
            retention = CONFIG.get("retention", {})
            all_days = int(retention.get("all_days", 30))
            mil_days = int(retention.get("military_days", 180))
            watch_days = int(retention.get("watchlist_days", 365))

            def _time_query(label: str, sql: str, params: tuple,
                            fetch_all: bool = False) -> Dict[str, Any]:
                """Run a query, return timing + plan + row count."""
                t0 = time.time()
                try:
                    cur = conn.execute(sql, params)
                    if fetch_all:
                        rows = cur.fetchall()
                        n = len(rows)
                    else:
                        row = cur.fetchone()
                        n = 1 if row else 0
                    ms = round((time.time() - t0) * 1000, 1)
                    # Query plan
                    plan_rows = conn.execute(
                        f"EXPLAIN QUERY PLAN {sql}", params
                    ).fetchall()
                    plan = [dict(r) for r in plan_rows]
                    return {"label": label, "ms": ms, "rows_returned": n,
                            "plan": plan, "ok": True}
                except Exception as e:
                    return {"label": label, "error": str(e),
                            "ms": round((time.time() - t0) * 1000, 1), "ok": False}

            queries: List[Dict[str, Any]] = []

            # v2.50.10: surface the hourly-rollup phase alongside the probe
            # results so the reader can tell which of the paired probes
            # below corresponds to their production /api/all path. The
            # rollup probes match production when phase == "complete";
            # the raw probes match production during backfill or after a
            # backfill error.
            try:
                import collector as _collector_mod
                rollup_status = _collector_mod.get_hourly_backfill_status()
                report["hourly_rollup"] = {
                    "phase": rollup_status.get("phase"),
                    "rows_processed": rollup_status.get("rows_processed"),
                    "rows_total": rollup_status.get("rows_total"),
                    "error": rollup_status.get("error"),
                }
            except Exception as e:
                report["hourly_rollup"] = {"phase": "unknown",
                                           "error": f"could not query: {e}"}

            # Q1: Synthetic "recent-range DISTINCT" probe. This query is NOT
            # run by the Live tab in normal operation — /api/live fetches
            # directly from the ADS-B receiver over HTTP, no DB access. The
            # probe exists to measure the range-filter + DISTINCT index path
            # on all_sightings, which is used by several Stats-tab cards
            # (unique_today, peak_simultaneous, average_concurrent). If this
            # probe is slow, those cards are likely slow too. If it's fast,
            # they're likely fine. Note: pre-v2.50.0 this path also served
            # the /api/all count, but that's now on the rollup; see the
            # rollup-path probes below.
            #
            # v2.42.7: INDEXED BY to pin the covering index. Some SQLite
            # versions' planners picked idx_all_icao (icao-first) here
            # instead of idx_all_seen_icao (seen_at, icao), producing a
            # 20-second full-index scan on 3M-row tables. Hinting removes
            # that variance so the probe measures the fast path consistently.
            queries.append(_time_query(
                "recent_range_distinct (synthetic probe, not a hot path)",
                "SELECT COUNT(DISTINCT icao) FROM all_sightings "
                "INDEXED BY idx_all_seen_icao WHERE seen_at >= ?",
                (now - 300,),
            ))

            # Q2/Q2a: distinct-aircraft count over the full retention
            # window — the number that backed the (since-removed) All
            # tab's "Showing N aircraft" header pre-Phase 1D, and now
            # backs Search result counts and the rollup-health probe.
            # Two probes paired so the reader can see both:
            #   - rollup path: what /api/all (and Search) runs post-
            #     v2.50.0 when backfill is complete
            #   - raw fallback: what /api/all runs during backfill or
            #     on a backfill-errored install
            # Compare against report["hourly_rollup"]["phase"] above to
            # know which one matches your production right now.
            #
            # v2.80.0: renamed from `all_tab_count_rollup` →
            # `unique_aircraft_count_rollup` and `all_tab_count_raw` →
            # `unique_aircraft_count_raw_fallback`. The All tab is gone
            # post-Phase 1D; the labels were misleading. Frontend keys
            # off the JSON payload's label string for display only,
            # not for matching, so the rename is safe.
            from_ts = now - all_days * 86400
            from_bucket = (from_ts // 3600) * 3600
            queries.append(_time_query(
                f"unique_aircraft_count_rollup (sightings_hourly, last {all_days}d)",
                "SELECT COUNT(DISTINCT icao) FROM sightings_hourly "
                "WHERE hour_bucket >= ? AND hour_bucket <= ?",
                (from_bucket, now),
            ))
            if include_legacy:
                # v2.50.26: legacy raw-fallback probe. Made obsolete for
                # the routine path by v2.50.19's /api/status migration to
                # the rollup. Kept available behind the flag for occasional
                # comparison — e.g. confirming the rollup is meaningfully
                # faster on a given install, or sanity-checking after a
                # SQLite version upgrade.
                queries.append(_time_query(
                    f"unique_aircraft_count_raw_fallback (all_sightings, last {all_days}d)",
                    "SELECT COUNT(DISTINCT icao) FROM all_sightings "
                    "WHERE seen_at >= ? AND seen_at <= ?",
                    (from_ts, now),
                ))

            # Q3: Military tab count
            queries.append(_time_query(
                f"military_count (over last {mil_days}d)",
                "SELECT COUNT(DISTINCT icao) FROM military_sightings WHERE seen_at >= ? AND seen_at <= ?",
                (now - mil_days * 86400, now),
            ))

            # Q4: Watchlist tab count
            queries.append(_time_query(
                f"watchlist_count (over last {watch_days}d)",
                "SELECT COUNT(DISTINCT icao) FROM watchlist_sightings WHERE seen_at >= ? AND seen_at <= ?",
                (now - watch_days * 86400, now),
            ))

            # Q5: removed in v2.83.5. Three probes (rollup, window-legacy,
            # raw-fallback) timed the All-tab page query, but the All tab
            # was removed in v2.67.0 (Phase 1D); Search inherited the
            # "browse all aircraft" intent but uses a different query
            # shape (FTS5 + seen_aircraft, not GROUP BY over
            # sightings_hourly). The probes had no production consumer
            # since v2.67.0 — the v2.80.0 rename to recent_browse_page_*
            # was cosmetic and didn't change what was being measured.
            # Reading the diag at face value (especially "(production
            # query)" in the label) implied a hot path that wasn't hot.
            # Same shape of mistake the v2.50.10 fix caught in reverse:
            # probe labels need to track current consumers, not
            # historical ones. When the consumer is gone, the probe goes.
            # If a future surface needs the rollup-grouped query shape
            # again, the v2.50.20 / v2.50.25 history in the changelog
            # has the rationale and reference numbers for re-introducing
            # an appropriately-named probe.

            # Q6: seen_aircraft count for "total unique aircraft ever seen" stats
            queries.append(_time_query(
                "seen_aircraft_total (all-time unique ICAOs)",
                "SELECT COUNT(*) FROM seen_aircraft", (),
            ))

            # ===========================================================
            # v2.80.0: new probes covering surfaces the pre-v2.80.0 diag
            # didn't measure. Each closes a real visibility gap:
            #   - Search probe: flagship browse surface post-Phase 1D
            #   - fts_dirty lag: catches FTS5 batch-flush regressions
            #   - Stats drill: catches v2.68.0 SQL pre-rank index drift
            #   - Aircraft detail page: PK lookup + paginated sightings
            #   - hexdb_events retention: catches retention-sweep regressions
            # ===========================================================

            # Q7: Search production query shape. Production /api/search
            # at search.py:execute_search joins seen_aircraft to
            # seen_aircraft_fts via FTS5 MATCH and orders by bm25.
            # The probe uses a hardcoded MATCH string covering common
            # type codes + a callsign prefix so the result set isn't
            # empty on most installs while still exercising the
            # multi-term OR path that bm25 ranks.
            #
            # Probe is skipped (with a synthesized "ok=False" entry) if
            # seen_aircraft_fts is empty — first-boot before FTS5
            # backfill completes. Without that gate, the probe would
            # report 0 ms which would be misleading (production search
            # would also return nothing, but for a different reason).
            try:
                fts_count_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM seen_aircraft_fts"
                ).fetchone()
                fts_populated = bool(fts_count_row and fts_count_row["n"] > 0)
            except sq.DatabaseError:
                fts_populated = False
            if fts_populated:
                queries.append(_time_query(
                    "search_match_bm25 (seen_aircraft_fts MATCH + bm25 + JOIN, top 50)",
                    "SELECT s.icao, s.last_callsign, s.aircraft_type, "
                    "       bm25(seen_aircraft_fts) AS score "
                    "FROM seen_aircraft_fts "
                    "JOIN seen_aircraft s ON s.rowid = seen_aircraft_fts.rowid "
                    "WHERE seen_aircraft_fts MATCH ? "
                    "ORDER BY score "
                    "LIMIT 50",
                    ("B738 OR A320 OR DAL",),
                    fetch_all=True,
                ))
            else:
                queries.append({
                    "label": "search_match_bm25 (seen_aircraft_fts MATCH + bm25 + JOIN, top 50)",
                    "ms": 0.0, "rows_returned": 0, "ok": True,
                    "skipped": True,
                    "skip_reason": "FTS5 not yet populated (first-boot or migration in progress)",
                })

            # Q8: FTS5 dirty-flag lag. The v2.51.0 design uses a
            # fts_dirty=1 flag on seen_aircraft rows that have FTS-
            # indexed fields (callsign / type / operator / country)
            # changed but not yet flushed to the FTS5 virtual table.
            # The collector flushes these in a cycle-end batch every
            # poll. If this count grows beyond a typical poll's worth
            # of changes (~50-200 rows on busy installs), the batch
            # flush is broken or stalled and Search results are
            # showing stale data.
            #
            # Healthy installs report 0-50 here. Anything over a few
            # hundred warrants investigation — see collector.py's
            # _flush_fts_dirty_batch for the relevant code path.
            queries.append(_time_query(
                "fts5_dirty_count (rows pending FTS5 flush)",
                "SELECT COUNT(*) FROM seen_aircraft WHERE fts_dirty = 1",
                (),
            ))

            # Q9: Stats furthest-card SQL pre-rank. The v2.68.0 rewrite
            # of the longest-track and furthest drill uses a SQL pre-
            # rank step (cos²(lat)-scaled coordinate distance proxy)
            # to narrow candidates before the Python haversine
            # refinement loop. If this query slows down, the furthest
            # card and its drill panel feel slow even though the
            # post-narrowing math is fast. The probe measures the
            # pre-rank's index coverage at production scale.
            #
            # Uses a synthetic receiver location near the bbox
            # midpoint — the actual rx_lat/rx_lon from CONFIG would
            # work too but would introduce per-install variance in
            # the probe's measurement. Fixed coords keep the probe
            # numerically comparable across installs.
            queries.append(_time_query(
                f"stats_furthest_prerank (cos\u00b2-scaled coord-dist proxy, last {all_days}d)",
                "SELECT icao, "
                "       (last_lat - 37.5) * (last_lat - 37.5) "
                "       + (last_lon - (-122.0)) * (last_lon - (-122.0)) * 0.628 AS dist_proxy "
                "FROM seen_aircraft "
                "WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL "
                "  AND last_seen_at >= ? "
                "ORDER BY dist_proxy DESC LIMIT 100",
                (now - all_days * 86400,),
                fetch_all=True,
            ))

            # Q10: Aircraft detail page — PK lookup. /aircraft/{icao}
            # fetches one seen_aircraft row by primary key. Should be
            # sub-millisecond on any install; if it isn't, the PK
            # index is broken or the table has degraded into a heap.
            # The probe uses a known-present ICAO from the table to
            # avoid measuring a "row doesn't exist" path.
            try:
                sample_row = conn.execute(
                    "SELECT icao FROM seen_aircraft LIMIT 1"
                ).fetchone()
                sample_icao = sample_row["icao"] if sample_row else None
            except sq.DatabaseError:
                sample_icao = None
            if sample_icao:
                queries.append(_time_query(
                    "aircraft_detail_pk_lookup (seen_aircraft by ICAO PK)",
                    "SELECT * FROM seen_aircraft WHERE icao = ?",
                    (sample_icao,),
                ))

                # Q11: Aircraft detail page — paginated sightings.
                # /aircraft/{icao} also fetches the per-ICAO sightings
                # list, paginated. Production query at the detail
                # page's _fetchSightingsPage uses the (icao, seen_at
                # DESC) index pattern. The probe measures one page's
                # worth (default page size 100) for the same sample
                # ICAO from Q10.
                queries.append(_time_query(
                    "aircraft_detail_sightings_page (all_sightings by ICAO, page 1)",
                    "SELECT seen_at, lat, lon, altitude, speed, squawk, callsign "
                    "FROM all_sightings "
                    "WHERE icao = ? "
                    "ORDER BY seen_at DESC LIMIT 100",
                    (sample_icao,),
                    fetch_all=True,
                ))

            # Q12: hexdb_events retention. The v2.49.0 hexdb cache
            # records each hit/miss in hexdb_events; cleanup_old_data
            # prunes events older than HEXDB_EVENTS_RETENTION_DAYS.
            # Probe surfaces oldest-row age + total count so retention
            # regressions become visible. A healthy install reports
            # oldest age ≤ retention_days; a regression shows older
            # rows piling up.
            try:
                oldest_row = conn.execute(
                    "SELECT MIN(ts) AS mn, COUNT(*) AS n FROM hexdb_events"
                ).fetchone()
                if oldest_row and oldest_row["n"] is not None:
                    oldest_age_days = None
                    if oldest_row["mn"]:
                        oldest_age_days = round((now - oldest_row["mn"]) / 86400, 1)
                    report["hexdb_events_retention"] = {
                        "rows": oldest_row["n"],
                        "oldest_age_days": oldest_age_days,
                        "ok": True,
                    }
                else:
                    report["hexdb_events_retention"] = {
                        "rows": 0, "oldest_age_days": None, "ok": True,
                    }
            except sq.DatabaseError as e:
                # Table missing on pre-v2.49.0 installs.
                report["hexdb_events_retention"] = {"ok": False, "error": str(e)}

            report["queries"] = queries
            # v2.50.26: surface the flag and the count of skipped legacy
            # probes so the frontend can render an explicit "skipped"
            # note rather than letting the absence of those rows look
            # like an unexplained omission.
            report["include_legacy"] = include_legacy
            # v2.83.5: count dropped from 3 to 1 when the two All-tab
            # Q5 legacy probes were removed alongside the production-named
            # Q5 probe (no current consumer since v2.67.0). Only
            # unique_aircraft_count_raw_fallback remains gated.
            report["legacy_probes_skipped"] = 0 if include_legacy else 1

            # --- Disk I/O baseline ---
            # Measure a simple sequential read from the DB file so the user
            # gets a sense of SD-card throughput. Reads the first 1 MB.
            io_baseline: Dict[str, Any] = {}
            try:
                sample_bytes = 1024 * 1024
                if db_path.is_file() and db_path.stat().st_size >= sample_bytes:
                    t0 = time.time()
                    with open(db_path, "rb") as fh:
                        _ = fh.read(sample_bytes)
                    elapsed = time.time() - t0
                    if elapsed > 0:
                        io_baseline["sample_bytes"] = sample_bytes
                        io_baseline["elapsed_ms"] = round(elapsed * 1000, 1)
                        io_baseline["throughput_mb_s"] = round(
                            (sample_bytes / 1024 / 1024) / elapsed, 1)
            except Exception as e:
                io_baseline["error"] = str(e)
            report["io_baseline"] = io_baseline

            conn.close()
        except Exception as e:
            report["ok"] = False
            report["error"] = f"Diagnostic failed: {e}"

        report["tables"] = tables_info

        # --- Hardware hints ---
        # Not authoritative, but enough to match reports to known hardware
        hints: List[str] = []
        size_bytes = report.get("storage", {}).get("size_bytes", 0)
        if size_bytes > 5 * 1024 ** 3:  # 5 GB
            hints.append(
                f"Database is {_fmt_bytes(size_bytes)} — at this size, consider "
                f"shorter retention (`retention.all_days` in config) or moving "
                f"to a USB SSD if on an SD card. See docs/PERFORMANCE.md."
            )
        for t in tables_info:
            # v2.80.1: `or 0` coerces both missing-key and None-value to 0.
            # The v2.80.0 FTS5 shadows aggregated row is appended with
            # `"rows": None` (count is meaningless for an aggregated shadow);
            # `t.get("rows", 0)` returned None there because the key exists,
            # and `None > 10_000_000` raised TypeError, crashing the whole
            # endpoint and surfacing client-side as the cryptic
            # "JSON.parse: unexpected character at line 1 column 1" (the
            # FastAPI 500 HTML body that fetch().json() couldn't parse).
            if (t.get("rows") or 0) > 10_000_000:
                hints.append(
                    f"{t['name']} has {t['rows']:,} rows. Count-level queries "
                    f"will be slow on an SD card; consider reducing retention."
                )
        # Slow-query hints
        for q in report.get("queries", []):
            if q.get("ms", 0) > 2000:
                hints.append(
                    f"Query '{q['label']}' took {q['ms']:.0f} ms — anything over "
                    f"~500 ms shows as UI lag."
                )
        io = report.get("io_baseline", {})
        if io.get("throughput_mb_s", 999) < 15:
            hints.append(
                f"Disk read throughput {io.get('throughput_mb_s', '?')} MB/s is "
                f"slower than a typical SD card (20-80 MB/s). Is the card old or the "
                f"filesystem heavily fragmented?"
            )
        report["hints"] = hints

        return report

    def _fmt_bytes(n: int) -> str:
        """Format byte count as human-friendly string."""
        for unit in ["B", "KB", "MB", "GB"]:
            if abs(n) < 1024.0:
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
            n /= 1024.0
        return f"{n:.1f} TB"

    @app.post("/api/perf/analyze")
    async def post_perf_analyze():
        """v2.42.6: manually re-run ANALYZE to refresh SQLite query-planner
        statistics. Useful after heavy data churn or if a user notices
        queries got slow and wants to try refreshing stats before
        reporting a bug.

        ANALYZE itself is read-only wrt data; it just updates internal
        sqlite_stat1 tables. Safe to run any time, including while the
        collector is actively inserting.

        On a 3M-row DB this takes ~30-60s. The endpoint blocks until
        complete and returns timing. Clients should show a loading
        indicator while waiting.
        """
        import time as _t
        db_path = CONFIG["data"]["db_file"]
        t0 = _t.time()
        try:
            conn = _open_db_conn(db_path, timeout=60.0)
            conn.execute("ANALYZE")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Manual ANALYZE failed: {e}")
            return JSONResponse(status_code=500, content={
                "ok": False,
                "message": f"ANALYZE failed: {type(e).__name__}: {e}",
            })
        elapsed = _t.time() - t0
        logger.info(f"Manual ANALYZE complete in {elapsed:.1f}s")
        return {
            "ok": True,
            "message": f"ANALYZE complete in {elapsed:.1f}s. Query planner stats refreshed.",
            "elapsed_seconds": round(elapsed, 2),
        }

    # --- Notifications ---
    # Endpoints for managing notification behavior at runtime:
    #   GET  /api/notifications/recent     — last ~20 notification attempts (sent + suppressed)
    #   POST /api/notifications/test       — send a one-off test to the configured URL
    #                                        (or a URL passed in the body, for verifying
    #                                        a URL before saving it to config)

    @app.get("/api/notifications/recent")
    async def notifications_recent(limit: int = 20):
        """Recent notification attempts, newest first. Used by the UI to show
        a sliding log of what Aerodrome has tried to send — helpful when
        debugging why a notification didn't arrive."""
        if _NOTIFIER is None:
            return {"ok": True, "items": []}
        limit = max(1, min(100, int(limit)))
        return {"ok": True, "items": _NOTIFIER.recent(limit)}

    @app.get("/api/notifications/stats")
    async def notifications_stats():
        """(v2.41.3) Summary of notification activity since service start.
        Includes counts + breakdowns over last 24h, last 7d, and since startup,
        plus last-sent / last-error records. Used by the Stats tab and any
        dashboard tile that wants a notifications overview.

        Note: stats reset when the service restarts — they live in-memory
        in the notifier, not the DB. A future release may persist them."""
        if _NOTIFIER is None:
            return {"ok": True, "stats": None, "reason": "Notifier not initialized"}
        return {"ok": True, "stats": _NOTIFIER.stats()}

    @app.get("/api/ntfy/logs")
    async def ntfy_logs(lines: int = 100):
        """(v2.41.3) Return recent ntfy systemd journal output. Tries
        `journalctl -u ntfy` first; on many Ubuntu systems the aerodrome
        user's adm/systemd-journal group membership grants read access
        without sudo. If that fails (permission), returns a helpful
        message explaining the limitation — we deliberately do NOT add
        a sudoers rule for journalctl because it would trigger the
        drift flow for every user just to read logs."""
        import subprocess
        lines = max(10, min(1000, int(lines)))
        try:
            result = subprocess.run(
                ["journalctl", "-u", "ntfy", "-n", str(lines),
                 "--no-pager", "-o", "short-iso"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {
                    "ok": True,
                    "lines": result.stdout.splitlines(),
                    "can_read": True,
                }
            # journalctl is present but refused — most commonly a
            # permission issue on stricter Linux distributions.
            stderr = (result.stderr or "").strip()
            return {
                "ok": True,
                "lines": [],
                "can_read": False,
                "reason": (
                    "Cannot read ntfy's journal without elevated permissions. "
                    "Try running this on the server: "
                    "sudo journalctl -u ntfy -n 100. "
                    f"(journalctl stderr: {stderr[:200]})"
                ),
            }
        except FileNotFoundError:
            return {
                "ok": True, "lines": [], "can_read": False,
                "reason": "journalctl not found on this system.",
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": True, "lines": [], "can_read": False,
                "reason": "Timed out reading ntfy's journal.",
            }
        except Exception as e:
            return {
                "ok": True, "lines": [], "can_read": False,
                "reason": f"Error reading ntfy's journal: {e}",
            }

    class TestNotificationBody(BaseModel):
        url: Optional[str] = None  # if set, test this URL instead of configured one

    @app.post("/api/notifications/test")
    async def notifications_test(body: TestNotificationBody):
        """Send a test notification. Useful for confirming the URL is
        reachable and the phone is subscribed. Bypasses the enable + event
        gates (but still honors rate limit), so works even when the feature
        is toggled off during initial setup.

        v2.47.2: the response includes a `tap_to_open` block describing
        whether the notification carried a Click URL (only happens when
        notifications.public_url is configured). The UI surfaces a warning
        when tap_to_open.configured is false so the user knows that half
        of the feature still needs setup.
        """
        if _NOTIFIER is None:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": "Notifier not initialized"
            })
        ok, message, info = _NOTIFIER.send_test(url=body.url)
        return {
            "ok": ok,
            "message": message,
            "tap_to_open": {
                "configured": info["tap_to_open_configured"],
                "url": info["tap_to_open_url"],
            },
        }

    # --- ntfy installer ---
    # Endpoints for the "Install local ntfy" affordance in the Notifications
    # tab. The server calls into ntfy_installer.py which shells out to sudo
    # for privileged operations (install.sh sets up the sudoers rule).

    @app.get("/api/ntfy/status")
    async def ntfy_status():
        """Describe the local ntfy install: present/missing/external/partial,
        version, service active, installable flag, etc. The UI uses the
        'state' field to decide which affordances to show."""
        try:
            from ntfy_installer import install_status, latest_version
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        status = install_status()
        # Add the latest-available version as a hint for the 'Upgrade' button
        latest = None
        try:
            latest = latest_version()
        except Exception:
            pass
        # v2.40.5: include current server.yml values so the UI can populate
        # the Base URL field and upstream-relay checkbox without a separate call.
        current_config = {}
        stale_data = False
        if status.get("state") == "aerodrome_managed":
            try:
                from ntfy_installer import _read_base_url, _read_upstream_relay, _detect_lan_ip
                current_config = {
                    "base_url": _read_base_url(),
                    "upstream_relay": _read_upstream_relay(),
                    "detected_lan_ip": _detect_lan_ip(),
                }
            except Exception:
                pass
        elif status.get("state") == "not_installed":
            # v2.41.0: detect leftover cache.db from a prior install so the
            # install UI can note "reinstalling will inherit old messages."
            try:
                from ntfy_installer import stale_data_present
                stale_data = stale_data_present()
            except Exception:
                pass
        return {
            "ok": True,
            "status": status,
            "latest_available": latest,
            "config": current_config,
            "stale_data": stale_data,
        }

    class NtfyInstallBody(BaseModel):
        port: int = 2586
        bind: str = "0.0.0.0"
        topic: Optional[str] = None
        # v2.40.5: external URL phones use to reach the server. None = auto-detect LAN IP.
        base_url: Optional[str] = None
        # v2.40.5: whether to set upstream-base-url: https://ntfy.sh for iOS push support.
        upstream_relay: bool = True

    @app.post("/api/ntfy/install")
    async def ntfy_install(body: NtfyInstallBody):
        """Download, verify, install, configure, and start ntfy as a systemd
        service alongside Aerodrome. Idempotent — re-running when already
        installed is a no-op that returns current state."""
        try:
            from ntfy_installer import install
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        # Basic sanity on the inputs we pass to a privileged operation
        if not (1 <= body.port <= 65535):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "Port must be between 1 and 65535"
            })
        if body.bind not in ("0.0.0.0", "127.0.0.1"):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "bind must be '0.0.0.0' or '127.0.0.1'"
            })
        # Reject obviously-garbage base_url inputs, but allow users to pass
        # any http(s) URL (they might use a Tailscale hostname, reverse proxy, etc.)
        if body.base_url is not None:
            if not (body.base_url.startswith("http://") or body.base_url.startswith("https://")):
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": "base_url must start with http:// or https://"
                })
        result = install(port=body.port, bind=body.bind, topic=body.topic,
                         base_url=body.base_url, upstream_relay=body.upstream_relay)
        if not result.get("ok"):
            return JSONResponse(status_code=400, content=result)
        return result

    class NtfyConfigBody(BaseModel):
        # v2.40.5: partial-update semantics — None means "keep current value."
        base_url: Optional[str] = None
        upstream_relay: Optional[bool] = None

    @app.post("/api/ntfy/config")
    async def ntfy_config_update(body: NtfyConfigBody):
        """(v2.40.5) Update /etc/ntfy/server.yml's base-url and/or upstream
        relay toggle. Restarts the service to apply. Only works on
        aerodrome-managed installs. Used by the Base URL field and the
        upstream-relay checkbox in the Notifications tab."""
        try:
            from ntfy_installer import update_config
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        if body.base_url is not None:
            if not (body.base_url.startswith("http://") or body.base_url.startswith("https://")):
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": "base_url must start with http:// or https://"
                })
        result = update_config(base_url=body.base_url,
                               upstream_relay=body.upstream_relay)
        if not result.get("ok"):
            return JSONResponse(status_code=400, content=result)
        return result

    @app.post("/api/ntfy/upgrade")
    async def ntfy_upgrade():
        """Upgrade the local ntfy install to the latest release. Only works
        on aerodrome_managed installs."""
        try:
            from ntfy_installer import upgrade
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        result = upgrade()
        if not result.get("ok"):
            return JSONResponse(status_code=400, content=result)
        return result

    class NtfyUninstallBody(BaseModel):
        purge_data: bool = False

    @app.post("/api/ntfy/uninstall")
    async def ntfy_uninstall(body: NtfyUninstallBody = NtfyUninstallBody()):
        """Remove a local aerodrome-managed ntfy install. Refuses to touch
        external (user-managed) installs.

        v2.41.0: optional purge_data flag wipes /var/lib/ntfy/cache.db
        and related files. Default False so reinstalls preserve history."""
        try:
            from ntfy_installer import uninstall
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        result = uninstall(purge_data=body.purge_data)
        if not result.get("ok"):
            return JSONResponse(status_code=400, content=result)
        return result

    @app.post("/api/restart")
    async def restart_service():
        """Restart the Aerodrome systemd service to pick up restart-only config changes."""
        ok, note = _do_restart()
        if ok:
            return {"ok": True, "message": "Service restart initiated"}
        return JSONResponse(status_code=500, content={"ok": False, "error": note or "Restart failed"})

    # --- Updates ---
    # Local update flow:
    #   1. User drops a new aerodrome release into <install_dir>/update/
    #   2. GET /api/updates/local/check — compare update/VERSION to current
    #   3. POST /api/updates/local/apply — back up, copy files, reinstall deps, restart

    # Paths that are preserved across an update (relative to install dir)
    PRESERVE_PATHS = {
        "config.yaml",
        "aircraft_history.db",
        "aircraft_history.db-shm",
        "aircraft_history.db-wal",
        "logs",
        "venv",
        ".tracker.pid",
        ".backups",
        "update",
    }

    def _parse_version(v: str):
        """Return a tuple of ints from 'X.Y.Z' for comparison. None if unparseable."""
        try:
            parts = v.strip().split(".")
            if len(parts) != 3:
                return None
            return tuple(int(p) for p in parts)
        except (ValueError, AttributeError):
            return None

    # Files inside update/ that should be refreshed from every new release.
    # These are the static docs of the staging folder itself — they describe
    # how the update flow works and have no user-specific content.
    # The main doc is named UPDATE_README.md (not README.md) to avoid a
    # filename collision with the release's root README.md that caused an
    # iter-order bug in earlier versions.
    UPDATE_FOLDER_REFRESH = {"UPDATE_README.md", ".gitkeep"}

    def _refresh_update_folder_docs(src_update: Path, dst_update: Path):
        """Copy the staging-folder docs (UPDATE_README.md and .gitkeep) from
        a staged release onto the live install's update/ folder, without
        touching anything else the user might have in there. Called during
        the update flow, which otherwise preserves the whole update/ tree.

        Intentionally does NOT touch update/README.md here — during apply,
        dst_update is the same path as update_root, so deleting
        dst_update/README.md would delete the staged release's root README
        before the main copy loop gets to it. The legacy README.md file is
        cleaned up at service startup instead (see the startup self-heal
        near the top of get_app)."""
        if not dst_update.exists():
            try:
                dst_update.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"Could not create {dst_update}: {e}")
                return
        for name in UPDATE_FOLDER_REFRESH:
            src_file = src_update / name
            if src_file.is_file():
                try:
                    shutil.copy2(src_file, dst_update / name)
                except Exception as e:
                    logger.warning(f"Could not refresh update/{name}: {e}")

    @app.get("/api/updates/local/check")
    async def check_local_update():
        """Scan the ./update folder and report whether a newer version is staged."""
        install_dir = Path(__file__).parent
        update_dir = install_dir / "update"
        current_ver = (install_dir / "VERSION").read_text().strip() if (install_dir / "VERSION").exists() else "unknown"

        if not update_dir.exists():
            return {
                "available": False,
                "current_version": current_ver,
                "update_version": None,
                "reason": "No update/ directory found. Copy a new release there to stage an update.",
                "update_dir": str(update_dir),
            }

        # Look for VERSION file inside update/ — accept either update/VERSION
        # or update/aerodrome/VERSION (user may have copied the whole zip contents)
        candidates = [
            update_dir / "VERSION",
            update_dir / "aerodrome" / "VERSION",
        ]
        version_file = next((c for c in candidates if c.exists()), None)
        if not version_file:
            return {
                "available": False,
                "current_version": current_ver,
                "update_version": None,
                "reason": "No VERSION file in update/. Make sure you copied the full release contents.",
                "update_dir": str(update_dir),
            }

        update_ver = version_file.read_text().strip()
        # Root of the staged release (the dir that contains VERSION)
        update_root = version_file.parent

        cur_tuple = _parse_version(current_ver)
        new_tuple = _parse_version(update_ver)

        status = "unknown"
        if cur_tuple and new_tuple:
            if new_tuple > cur_tuple:
                status = "newer"
            elif new_tuple == cur_tuple:
                status = "same"
            else:
                status = "older"

        # Quick sanity check: require main.py to be present in the staged release
        has_main = (update_root / "main.py").exists()

        # v2.40.1: sudoers-drift detection. The staged release's install.sh
        # declares a SUDOERS_VERSION; the live /etc/sudoers.d/aerodrome also
        # carries one (since v2.40.1). If they differ, the sudoers file is
        # stale and needs a refresh before apply — otherwise features that
        # rely on new sudoers lines will silently fail. Report the drift so
        # the frontend can block apply and show the "Sudoers update required"
        # modal with the exact command to run on the server.
        sudoers_check = _check_sudoers_drift(update_root)

        return {
            "available": status == "newer" and has_main,
            "current_version": current_ver,
            "update_version": update_ver,
            "status": status,
            "has_main_py": has_main,
            "update_root": str(update_root),
            "install_dir": str(install_dir),
            "sudoers": sudoers_check,
            "reason": (
                "Update ready to apply." if status == "newer" and has_main
                else "Staged version matches current." if status == "same"
                else "Staged version is older than current." if status == "older"
                else "Staged release is missing main.py." if not has_main
                else "Could not parse versions."
            ),
        }

    # v2.41.7: upload-and-stage endpoint. Before this, the only way to
    # stage a release was to rsync/scp it to ~/aerodrome/update/ from a
    # terminal \u2014 which assumed the user had SSH access and remembered the
    # exact path. This endpoint lets the web UI receive a release zip and
    # extract it into update/ directly, removing the SSH dependency from
    # the update flow for users who have their release file on a device
    # where they're already browsing Aerodrome. The rsync path is kept as
    # an alternative for power users and fallback.
    #
    # Security considerations:
    #   - Zip files only. Content-type isn't trusted; we try to parse as
    #     a zip and reject if that fails.
    #   - 50 MB hard cap. Our own release zips are ~3 MB. Anything larger
    #     is almost certainly the wrong file (e.g. a full-backup zip).
    #   - Path-traversal rejection. Any zip entry whose name resolves
    #     outside update/ after extraction is rejected, and the whole
    #     operation is aborted before any files are written.
    #   - update/ is wiped first so stale files from a prior staging
    #     can't mix with the new release.
    #   - Single-top-level-directory layout (our release convention) is
    #     automatically flattened so VERSION lands at update/VERSION.

    @app.post("/api/updates/local/upload")
    async def upload_local_update(file: UploadFile = File(...)):
        """Receive a release zip and extract it into update/. Runs the
        same check logic as /api/updates/local/check after extraction so
        the frontend gets a ready-to-apply response."""
        import io, zipfile

        install_dir = Path(__file__).parent
        update_dir = install_dir / "update"
        MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

        raw = await file.read()
        if not raw:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "Uploaded file is empty"
            })
        if len(raw) > MAX_UPLOAD_BYTES:
            return JSONResponse(status_code=413, content={
                "ok": False,
                "error": f"Upload too large ({len(raw)} bytes). "
                         f"Aerodrome release zips are a few MB; did you upload a full backup?"
            })

        # Validate zip format
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw), "r")
            bad_file = zf.testzip()
            if bad_file is not None:
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": f"Corrupted zip entry: {bad_file}"
                })
        except zipfile.BadZipFile:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "Uploaded file is not a valid zip archive."
            })

        # Path-traversal check. ZipFile doesn't reject '..' entries by
        # default. We resolve each member to an absolute path under the
        # intended extract root and refuse anything that escapes.
        names = zf.namelist()
        if not names:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "Zip archive contains no files."
            })

        target_root = update_dir.resolve()
        for name in names:
            # Normalize: reject any entry containing a null byte, absolute
            # path, or that resolves outside the extract root.
            if "\x00" in name:
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": f"Zip entry has null byte: {name!r}"
                })
            if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": f"Zip entry has absolute path: {name!r}"
                })
            # Resolve each entry relative to the target root and check
            # containment. Note: we use target_root even though it
            # doesn't exist yet \u2014 we compare path strings below.
            candidate = (target_root / name).resolve()
            try:
                candidate.relative_to(target_root)
            except ValueError:
                return JSONResponse(status_code=400, content={
                    "ok": False,
                    "error": f"Zip entry escapes extraction root: {name!r}"
                })

        # Wipe update/ (after all validation, so we don't destroy the
        # prior state if the uploaded zip turns out to be malformed).
        if update_dir.exists():
            try:
                shutil.rmtree(update_dir)
            except OSError as e:
                return JSONResponse(status_code=500, content={
                    "ok": False, "error": f"Could not clear update/: {e}"
                })
        update_dir.mkdir(parents=True, exist_ok=True)

        # Extract. ZipFile.extractall is safe after the path-traversal
        # checks above \u2014 but to be defensive we extract one entry at a
        # time so we can recheck each resolved path right before writing.
        try:
            for name in names:
                resolved = (target_root / name).resolve()
                try:
                    resolved.relative_to(target_root)
                except ValueError:
                    # Should have been caught above; belt + suspenders.
                    raise RuntimeError(f"Refusing to write outside update/: {name!r}")
                if name.endswith("/"):
                    resolved.mkdir(parents=True, exist_ok=True)
                else:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(resolved, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except Exception as e:
            # Clean up partial extract \u2014 user gets a clean retry
            try:
                shutil.rmtree(update_dir)
                update_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"Extraction failed: {e}"
            })

        # Flatten single-top-level-directory layout. Our release zips are
        # shaped as "aerodrome-vX.Y.Z/<files>", which after extraction
        # puts VERSION at update/aerodrome-vX.Y.Z/VERSION. The existing
        # check logic already handles that via its two-candidate lookup,
        # but the apply logic is cleaner when everything is at update/
        # top level. Detect that shape and flatten.
        children = [p for p in update_dir.iterdir() if not p.name.startswith(".")]
        if len(children) == 1 and children[0].is_dir():
            inner = children[0]
            # Only flatten if the inner dir actually contains VERSION \u2014
            # otherwise we might unwrap a legitimate nested structure.
            if (inner / "VERSION").is_file():
                # Move each child of inner/ up into update/
                for item in list(inner.iterdir()):
                    shutil.move(str(item), str(update_dir / item.name))
                try:
                    inner.rmdir()
                except OSError:
                    pass  # best effort

        # Verify the expected files are present
        version_file = update_dir / "VERSION"
        main_py = update_dir / "main.py"
        if not version_file.is_file():
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "Uploaded zip did not produce a VERSION file at the "
                         "expected location. Make sure this is an Aerodrome release zip."
            })
        if not main_py.is_file():
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "Uploaded zip did not produce main.py. This zip doesn't "
                         "look like a complete Aerodrome release."
            })

        # Success \u2014 return the same shape as /api/updates/local/check
        # so the frontend can pass it directly to its render function.
        current_ver = (install_dir / "VERSION").read_text().strip() if (install_dir / "VERSION").exists() else "unknown"
        update_ver = version_file.read_text().strip()
        cur_tuple = _parse_version(current_ver)
        new_tuple = _parse_version(update_ver)
        update_status = "unknown"
        if cur_tuple and new_tuple:
            if new_tuple > cur_tuple: update_status = "newer"
            elif new_tuple == cur_tuple: update_status = "same"
            else: update_status = "older"
        sudoers_check = _check_sudoers_drift(update_dir)

        return {
            "ok": True,
            "available": update_status == "newer",
            "current_version": current_ver,
            "update_version": update_ver,
            "status": update_status,
            "has_main_py": True,
            "update_root": str(update_dir),
            "install_dir": str(install_dir),
            "sudoers": sudoers_check,
            "uploaded_bytes": len(raw),
            "reason": (
                "Upload complete. Update ready to apply." if update_status == "newer"
                else "Upload complete. Staged version matches current."
                if update_status == "same"
                else "Upload complete. Staged version is older than current \u2014 this would be a downgrade."
                if update_status == "older"
                else "Upload complete but version comparison failed."
            ),
        }


    def _read_sudoers_version(path: Path) -> Optional[int]:
        """Extract the SUDOERS_VERSION from a sudoers-style file. Returns the
        integer version, 0 if no marker is present (pre-v2.40.1 format), or
        None if the file can't be read at all.

        Format: a comment line like '# SUDOERS_VERSION: 2' anywhere in the
        file. Whitespace-tolerant on both sides of the colon.

        v2.41.1 fix: we used to cap the scan at 20 lines as a
        micro-optimization. That silently broke the drift check for
        EVERY release since v2.40.1, because install.sh's SUDOERS_VERSION
        marker actually lives at line ~152 (deep inside the sudoers
        heredoc). Scanner returned 0 ("predates versioning") which the
        caller treats as "nothing to check, allow apply." No banner ever
        fired. Scanning the whole file costs nothing (install.sh is
        ~230 lines, /etc/sudoers.d/aerodrome is ~40 lines) and eliminates
        the trap.

        REGRESSION GUARD: if you ever reintroduce a line-count cap, make
        sure it's larger than the line number where install.sh's marker
        actually lives. Better: don't cap, and do what we do here.
        Sanity-check below verifies the function finds install.sh's
        marker at runtime.
        """
        try:
            with open(path, "r") as f:
                for line in f:
                    m = re.match(r"\s*#\s*SUDOERS_VERSION\s*:\s*(\d+)", line)
                    if m:
                        return int(m.group(1))
            # File readable but no marker found — pre-versioning release.
            return 0
        except FileNotFoundError:
            return None
        except PermissionError:
            # /etc/sudoers.d/aerodrome is 0440 root — normally unreadable by
            # the aerodrome user. Use sudo + cat. If sudo won't let us read
            # it without a password (pre-v2.40.1 sudoers), return None and
            # the caller will treat this as "can't verify, allow apply" so
            # we don't lock users out forever.
            try:
                import subprocess
                result = subprocess.run(
                    ["sudo", "-n", "cat", str(path)],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    return None
                for line in result.stdout.splitlines():
                    m = re.match(r"\s*#\s*SUDOERS_VERSION\s*:\s*(\d+)", line)
                    if m:
                        return int(m.group(1))
                return 0
            except Exception:
                return None
        except Exception:
            return None


    def _check_sudoers_drift(update_root: Path) -> dict:
        """Compare the SUDOERS_VERSION in the staged release's install.sh
        against /etc/sudoers.d/aerodrome. Returns a dict suitable for
        direct inclusion in the update-check response.

        Outcomes:
          needs_refresh=False, can_verify=True  — versions match, safe to apply
          needs_refresh=True,  can_verify=True  — versions differ, block apply
          needs_refresh=False, can_verify=False — can't read live file,
              assume fine (pre-v2.40.1 deployments where the aerodrome user
              has no sudoers grant to read it; we don't want to lock users
              out forever on first upgrade)
        """
        staged_installer = update_root / "install.sh"
        live_sudoers = Path("/etc/sudoers.d/aerodrome")

        staged_ver = _read_sudoers_version(staged_installer)
        if staged_ver is None:
            # Staged release has no install.sh or it isn't readable — odd,
            # but not reason to block.
            return {
                "can_verify": False,
                "needs_refresh": False,
                "reason": "Staged release has no install.sh to check.",
                "staged_version": None,
                "live_version": None,
            }
        if staged_ver == 0:
            # Staged release predates the versioning convention — nothing
            # to check. This is the "old release staged against new live"
            # direction and we shouldn't block it.
            return {
                "can_verify": False,
                "needs_refresh": False,
                "reason": "Staged release predates sudoers versioning.",
                "staged_version": 0,
                "live_version": None,
            }

        live_ver = _read_sudoers_version(live_sudoers)
        if live_ver is None:
            # Couldn't read the live file at all. Most common reason:
            # pre-v2.40.1 sudoers grant doesn't include a rule letting us
            # sudo-cat this file. We don't want to hard-block the upgrade
            # that WOULD fix that situation, so we let it through. Users
            # who see post-upgrade feature failures can re-run install.sh.
            return {
                "can_verify": False,
                "needs_refresh": False,
                "reason": "Cannot read /etc/sudoers.d/aerodrome to verify version. "
                          "After this upgrade the version can be verified automatically.",
                "staged_version": staged_ver,
                "live_version": None,
            }

        if live_ver < staged_ver:
            return {
                "can_verify": True,
                "needs_refresh": True,
                "reason": (
                    f"Staged release requires sudoers version {staged_ver}, "
                    f"but /etc/sudoers.d/aerodrome is at version {live_ver}. "
                    f"Re-run install.sh on the server before applying this update."
                ),
                "staged_version": staged_ver,
                "live_version": live_ver,
            }

        # live_ver >= staged_ver — we're fine. The >= case handles downgrade,
        # which doesn't need a sudoers refresh.
        return {
            "can_verify": True,
            "needs_refresh": False,
            "reason": "Sudoers version matches.",
            "staged_version": staged_ver,
            "live_version": live_ver,
        }

    # v2.41.1 regression guard: at server startup, verify _read_sudoers_version
    # can actually find this release's own install.sh marker. If it can't, the
    # drift check will silently soft-allow every future apply — exactly the
    # bug that escaped detection between v2.40.1 and v2.41.0. Log a noisy
    # warning that'll show up in `journalctl -u aerodrome` so future bugs of
    # this shape fail loudly instead of silently.
    try:
        _self_installer = Path(__file__).parent / "install.sh"
        if _self_installer.exists():
            _self_version = _read_sudoers_version(_self_installer)
            if _self_version is None:
                logger.warning("sudoers-drift self-check: could not read own install.sh")
            elif _self_version == 0:
                logger.warning(
                    "sudoers-drift self-check: own install.sh exists but returned "
                    "version 0 ('pre-versioning'). _read_sudoers_version is "
                    "broken — every drift check will soft-allow."
                )
            else:
                logger.info("sudoers-drift self-check: OK (own install.sh reports version %d)",
                            _self_version)
    except Exception as e:
        logger.warning("sudoers-drift self-check failed: %s", e)

    # v2.41.2: standalone drift check against the INSTALLED install.sh
    # (not a staged one). Used by the header badge + the Updates page
    # to surface stale sudoers even when there's no update to apply.
    # Before v2.41.2, drift was only ever checked as part of the update
    # flow, which meant users could sit on a stale sudoers indefinitely
    # if no new release was staged.
    def _check_live_sudoers_drift() -> dict:
        """Compare /etc/sudoers.d/aerodrome against the currently-installed
        install.sh. Same return shape as _check_sudoers_drift for consistency
        with the existing applyLocal frontend code."""
        installed = Path(__file__).parent / "install.sh"
        live_sudoers = Path("/etc/sudoers.d/aerodrome")
        expected_ver = _read_sudoers_version(installed)
        if expected_ver is None or expected_ver == 0:
            return {
                "can_verify": False,
                "needs_refresh": False,
                "reason": "Cannot read installed install.sh or it has no SUDOERS_VERSION marker.",
                "expected_version": expected_ver,
                "live_version": None,
                "install_dir": str(installed.parent),
            }
        live_ver = _read_sudoers_version(live_sudoers)
        if live_ver is None:
            return {
                "can_verify": False,
                "needs_refresh": False,
                "reason": "Cannot read /etc/sudoers.d/aerodrome. "
                          "Re-run install.sh to set up the read grant.",
                "expected_version": expected_ver,
                "live_version": None,
                "install_dir": str(installed.parent),
            }
        if live_ver < expected_ver:
            return {
                "can_verify": True,
                "needs_refresh": True,
                "reason": (
                    f"Installed Aerodrome expects sudoers version {expected_ver}, "
                    f"but /etc/sudoers.d/aerodrome is at version {live_ver}. "
                    f"Re-run install.sh on the server to refresh permissions."
                ),
                "expected_version": expected_ver,
                "live_version": live_ver,
                "install_dir": str(installed.parent),
            }
        return {
            "can_verify": True,
            "needs_refresh": False,
            "reason": "Sudoers version matches installed Aerodrome.",
            "expected_version": expected_ver,
            "live_version": live_ver,
            "install_dir": str(installed.parent),
        }

    @app.get("/api/sudoers/status")
    async def sudoers_status():
        """(v2.41.2) Return whether /etc/sudoers.d/aerodrome matches what
        the currently-installed Aerodrome expects. Independent of the
        update flow — surfaced via a header badge and the Updates page
        so users aren't stranded when an earlier release silently
        soft-allowed past a needed sudoers refresh.

        Response matches _check_sudoers_drift's shape for frontend reuse,
        except 'staged_version' is named 'expected_version' here (there
        is no staged release — we're comparing against the installed code).
        """
        result = _check_live_sudoers_drift()
        # Cache the result so the /api/status endpoint can reuse it for
        # the header badge without paying the file-read cost on every
        # health poll (every few seconds).
        nonlocal_state["last_sudoers_check"] = result
        return result

    # Poll-free cache for /api/status to pull from. The actual refresh
    # happens on startup below and every time /api/sudoers/status is
    # called. Drift state only changes when install.sh or the sudoers
    # file is rewritten, both of which are explicit events.
    nonlocal_state = {
        "last_sudoers_check": None,
        # v2.49.7: cache for the expensive db_check["stats"] block in
        # /api/status. The COUNT(DISTINCT icao) and COUNT(*) queries over
        # the 30-day retention window cost ~8 seconds at scale (Pi user,
        # 7.4M rows in all_sightings — see perf diag in handoff). Caching
        # for 30 seconds means the first request after a stale cache is
        # slow, but every subsequent request within the TTL is instant.
        # Health-indicator polling on every admin page (v2.49.3) hits
        # /api/status every 30 seconds, which is exactly the cache TTL —
        # so post-warmup the polling never sees a slow response.
        #
        # Shape: {"data": <stats dict>, "timestamp": <unix int>}
        # Invalidated by TTL only, not by events. The numbers are
        # advisory — they don't drive severity decisions, just card
        # display — so 30s of staleness is acceptable.
        "db_stats_cache": None,
        # v2.50.1: cache for the hexdb.io reachability probe in /api/status.
        # Same shape and reasoning as db_stats_cache. The probe makes an
        # outbound HTTPS request with a network timeout — when hexdb is
        # unreachable, every status poll paid the full timeout cost, and
        # since v2.49.3 every admin page polls /api/status, the whole UI
        # got slow whenever hexdb was flaky. Caching the probe result
        # (whether ok or unreachable) for 30s means at most one slow probe
        # per 30s window.
        #
        # Shape: {"data": <hexdb_check dict>, "timestamp": <unix int>}
        # Note: only the probe result itself is cached. cache_stats and
        # provider_in_use are local lookups attached fresh on every call.
        "hexdb_probe_cache": None,
        # v2.50.30: cache for capacity metrics (current size, growth rate,
        # projection at retention). Same TTL shape as db_stats_cache —
        # the underlying numbers don't shift second-to-second and
        # recomputing on every status poll would do extra DB work for
        # no display benefit.
        "capacity_cache": None,
    }
    DB_STATS_CACHE_TTL_SEC = 30
    HEXDB_PROBE_CACHE_TTL_SEC = 30

    # Initial populate at startup so the first /api/status call has a
    # meaningful value for the header badge.
    try:
        nonlocal_state["last_sudoers_check"] = _check_live_sudoers_drift()
    except Exception as e:
        logger.warning("Initial sudoers drift check failed: %s", e)

    @app.post("/api/updates/local/apply")
    async def apply_local_update():
        """Back up current install, copy files from update/ over it, reinstall deps, restart."""
        import subprocess
        install_dir = Path(__file__).parent
        update_dir = install_dir / "update"

        # Re-run the check to find the staged release root
        candidates = [update_dir / "VERSION", update_dir / "aerodrome" / "VERSION"]
        version_file = next((c for c in candidates if c.exists()), None)
        if not version_file:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "No VERSION file in update/. Nothing to apply."
            })
        update_root = version_file.parent
        new_version = version_file.read_text().strip()
        cur_version = (install_dir / "VERSION").read_text().strip() if (install_dir / "VERSION").exists() else "unknown"

        # v2.40.1: sudoers-drift pre-flight guard. Even if the frontend is
        # bypassed (direct POST, scripting, old cached tab), we refuse to
        # apply an update whose sudoers version is ahead of the live file.
        # Applying anyway would leave the user's notifications/ntfy features
        # silently broken until they SSH'd to run install.sh — exactly the
        # experience v2.40.1 was built to prevent.
        sudoers_check = _check_sudoers_drift(update_root)
        if sudoers_check.get("can_verify") and sudoers_check.get("needs_refresh"):
            return JSONResponse(status_code=409, content={
                "ok": False,
                "error": "sudoers_refresh_required",
                "message": sudoers_check.get("reason",
                    "Sudoers version is stale. Re-run install.sh on the server."),
                "sudoers": sudoers_check,
            })

        # --- Back up everything that will be overwritten ---
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = install_dir / ".backups" / ts
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for item in install_dir.iterdir():
                if item.name in PRESERVE_PATHS:
                    continue
                # v2.50.8: skip user-data backup artifacts (.pre-restore,
                # .bak.*, .from-backup.*) and Python bytecode caches at
                # the top level. PRESERVE_PATHS is exact-name only, so
                # without this check anything with a suffix falls through
                # and bloats .backups/<ts>/ — this is what made historical
                # snapshots multi-GB on installs that had pre-restore
                # files in the install root at update time.
                if _should_skip_in_install_snapshot(item.name):
                    continue
                dest = backup_dir / item.name
                if item.is_dir():
                    # v2.50.8: also exclude __pycache__/*.pyc from any
                    # subdirectory we DO snapshot — they show up in
                    # scripts/ and elsewhere and have no rollback value.
                    shutil.copytree(
                        item, dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
                else:
                    shutil.copy2(item, dest)
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": f"Backup failed: {e}. Update not applied."
            })

        # Prune old install snapshots so .backups/ doesn't grow unbounded
        _prune_install_backups()

        # --- Copy new files from update_root over install_dir ---
        copied = []
        try:
            for item in update_root.iterdir():
                if item.name in PRESERVE_PATHS:
                    # User-data paths (config.yaml, db, logs, etc.) are never
                    # overwritten. The `update/` folder is also preserved so
                    # that anything the user staged mid-update survives — BUT
                    # we still want to refresh the documentation inside it
                    # (README.md, .gitkeep) so the in-app docs viewer doesn't
                    # show a stale copy forever. Handle that special case here.
                    if item.name == "update" and item.is_dir():
                        _refresh_update_folder_docs(item, install_dir / "update")
                        copied.append("update/UPDATE_README.md (docs only)")
                    continue
                dest = install_dir / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
                copied.append(item.name)
        except Exception as e:
            logger.error(f"Copy failed: {e}")
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": f"Copy failed: {e}. Partial update — check {backup_dir} to restore."
            })

        # v2.41.20: ensure all shell scripts in the install dir are executable.
        # Background: zip archives don't always preserve the POSIX execute bit
        # (depends on how the zip was built and extracted). Even when they do,
        # the Python release-packaging pipeline occasionally builds zips where
        # install.sh / uninstall.sh come in as 0644 instead of 0755. Combined
        # with the fact that git-on-Windows and some file-transfer tools strip
        # the exec bit entirely, users who try `./install.sh` after unzipping
        # a release hit "permission denied" even though `sudo bash install.sh`
        # works. Fix here: after every apply, walk the install root and flip
        # +x on every *.sh file. Idempotent, safe, and covers any future
        # scripts we add without needing to maintain a hard-coded list.
        try:
            for sh in install_dir.glob("*.sh"):
                if sh.is_file():
                    mode = sh.stat().st_mode
                    # Add u+x, g+x, o+x (0o111) while preserving everything
                    # else. Equivalent to `chmod +x <file>`.
                    sh.chmod(mode | 0o111)
            logger.info("Restored executable bit on shell scripts in install dir")
        except Exception as e:
            # Not fatal — the sudoers-update instruction in the UI uses
            # `sudo bash install.sh` which doesn't need +x, so users can
            # still recover. Log it loudly so we see it in journalctl.
            logger.warning(f"Could not restore +x on shell scripts: {e}")

        # --- Reinstall requirements (best effort; service restart will catch import errors) ---
        venv_pip = install_dir / "venv" / "bin" / "pip"
        pip_ok = True
        pip_msg = ""
        if venv_pip.exists() and (install_dir / "requirements.txt").exists():
            try:
                result = subprocess.run(
                    [str(venv_pip), "install", "-r", "requirements.txt", "-q"],
                    cwd=str(install_dir),
                    capture_output=True, text=True, timeout=120,
                )
                pip_ok = (result.returncode == 0)
                pip_msg = result.stderr.strip() if not pip_ok else ""
            except Exception as e:
                pip_ok = False
                pip_msg = str(e)

        # --- Clean the update/ directory (success path) ---
        # Preserve the folder's own docs so the user sees instructions next time.
        # UPDATE_README.md is the new-style name; README.md is deleted if
        # present (legacy artifact from pre-2.40.1 installs).
        update_dir_keep = {"UPDATE_README.md", ".gitkeep"}
        try:
            for item in update_dir.iterdir():
                if item.name in update_dir_keep:
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        except Exception as e:
            logger.warning(f"Could not fully clear update/: {e}")

        # --- Trigger restart via helper (handles --no-block + fallback) ---
        restart_ok, restart_note = _do_restart()

        return {
            "ok": True,
            "from_version": cur_version,
            "to_version": new_version,
            "backup_dir": str(backup_dir.relative_to(install_dir)),
            "files_updated": len(copied),
            "pip_ok": pip_ok,
            "pip_msg": pip_msg,
            "restart_ok": restart_ok,
            "restart_note": restart_note,
            "message": "Update applied. Service is restarting.",
        }

    # ──────────────────────────────────────────────────────────────────
    # v3.0.1: GitHub-Releases-based apply path
    # ──────────────────────────────────────────────────────────────────
    # Closes the v3.0.x arc's second half. v3.0.0 added the discover
    # surface (poll, cache, render 5 states). v3.0.1 adds the act
    # surface: fetch the release zip + sha256 from the public asset
    # URLs, verify the checksum, stage into update/, then hand off to
    # the existing apply_local_update() flow which already does
    # backup + copy + deps + restart correctly. The apply step itself
    # doesn't change — v3.0.1 is purely a fetch+verify+stage layer in
    # front of existing infrastructure.

    import hashlib as _hashlib
    import zipfile as _zipfile
    import io as _io

    def _fetch_github_release_assets(tag: str) -> tuple:
        """Download zip + sha256 for a GitHub release tag, both into memory.

        Returns (zip_bytes, sha256_text) on success. Raises a ValueError
        with a user-presentable message on any failure. 60-second timeout
        per asset — long enough for a slow connection on a 5MB zip, short
        enough that a failed CDN doesn't hang the UI indefinitely.

        We use urllib (stdlib) rather than requests so this path has no
        runtime dependency the rest of server.py doesn't already have."""
        base = f"https://github.com/preston-peterson/aerodrome/releases/download/{tag}"
        zip_url = f"{base}/aerodrome-{tag}.zip"
        sha_url = f"{base}/aerodrome-{tag}.zip.sha256"

        def _get(url: str, what: str) -> bytes:
            try:
                req_obj = _urllib_request.Request(
                    url,
                    headers={"User-Agent": "Aerodrome/github-apply"},
                )
                with _urllib_request.urlopen(req_obj, timeout=60) as response:
                    return response.read()
            except _urllib_error.HTTPError as e:
                if e.code == 404:
                    # v3.0.6: distinguish transient CDN cache from real
                    # missing-asset state. Dogfooding v3.0.5 across two
                    # machines: one succeeded immediately, the other
                    # 404'd for ~10 minutes before clearing on its own.
                    # Root cause: GitHub Releases assets serve via an
                    # edge CDN that caches 404 responses per-edge.
                    # When you publish a Release without assets and
                    # attach them after, the edge serving Apply requests
                    # holds the cached 404 until it expires (usually
                    # single-digit minutes). The original copy said
                    # "this is a packaging bug — report it" which is
                    # the rare case; the common case is "wait a few
                    # minutes." Lead with the common case.
                    raise ValueError(
                        f"Couldn't download {what} for {tag} (HTTP 404). "
                        f"This is usually a transient CDN cache from a "
                        f"just-published release — try again in a few "
                        f"minutes. If it persists past ~10 minutes, the "
                        f"asset may genuinely not be attached to the "
                        f"Release on GitHub."
                    )
                if e.code == 403:
                    raise ValueError(
                        "GitHub rate-limited the download (60 req/hour anonymous). "
                        "Try again in an hour."
                    )
                raise ValueError(f"GitHub returned HTTP {e.code} for {what}.")
            except _urllib_error.URLError as e:
                raise ValueError(f"Couldn't reach GitHub for {what}: {e.reason}")
            except Exception as e:
                raise ValueError(
                    f"Unexpected error downloading {what}: {type(e).__name__}: {e}"
                )

        zip_bytes = _get(zip_url, "the release zip")
        sha_bytes = _get(sha_url, "the SHA256 checksum")
        # The .sha256 file format produced by scripts/package-release.sh is the
        # `sha256sum -c` standard: "<64-hex-chars>  <filename>\n". Parse the
        # hash field; everything else is checksum-file noise we ignore.
        try:
            sha_text = sha_bytes.decode("utf-8").strip()
            expected_hash = sha_text.split()[0].lower()
            if len(expected_hash) != 64 or not all(
                c in "0123456789abcdef" for c in expected_hash
            ):
                raise ValueError(
                    "Checksum file format unexpected — not a valid SHA256 hex digest."
                )
            return zip_bytes, expected_hash
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Couldn't parse the checksum file: {e}")

    def _verify_and_stage_github_zip(zip_bytes: bytes,
                                     expected_hash: str,
                                     install_dir: Path) -> None:
        """Verify SHA256 matches, then unzip into <install_dir>/update/.

        Always wipes update/ first (except the docs files the local-apply
        flow expects to find there) — applying from GitHub is a clean-slate
        operation. Anything previously staged from a local upload is
        replaced. Raises ValueError on checksum mismatch or unzip failure.

        The release zip has a top-level `aerodrome-vX.Y.Z/` wrapper folder
        per the v2.98.2 packaging convention. We unzip as-is; the
        apply_local_update() flow already handles both flat and wrapped
        layouts (it looks for VERSION at update/VERSION OR
        update/aerodrome/VERSION OR update/aerodrome-vX.Y.Z/VERSION...
        actually let me re-check that. The candidates list at apply
        time is [update/VERSION, update/aerodrome/VERSION]. So we need
        to either unwrap the zip's top-level folder, or rely on the
        unzip producing exactly one of those two layouts. The v2.98.2+
        wrapper is `aerodrome-vX.Y.Z/`, not `aerodrome/`, so we have
        to unwrap. Done below: extract to a temp area, find the
        single top-level dir, then promote its contents to update/.)"""
        # Checksum verification — the trust anchor for the whole channel
        actual_hash = _hashlib.sha256(zip_bytes).hexdigest().lower()
        if actual_hash != expected_hash:
            raise ValueError(
                f"Downloaded zip didn't match expected checksum. "
                f"Expected {expected_hash[:16]}…, got {actual_hash[:16]}…. "
                f"Something is wrong with the Release packaging — "
                f"please report it at the issue tracker."
            )

        # Clean-slate the update/ directory, preserving only the docs that
        # apply_local_update()'s cleanup step also preserves on success.
        update_dir = install_dir / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        preserve_in_update = {"UPDATE_README.md", ".gitkeep"}
        try:
            for item in update_dir.iterdir():
                if item.name in preserve_in_update:
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        except Exception as e:
            raise ValueError(f"Couldn't clear staged update/: {e}")

        # Unzip — handle both wrapped (aerodrome-vX.Y.Z/…) and flat layouts.
        # Detect by inspecting the first member's path: if every member
        # shares a common top-level directory, strip it. Otherwise extract
        # as-is and let apply_local_update() find VERSION wherever it ended up.
        try:
            with _zipfile.ZipFile(_io.BytesIO(zip_bytes)) as zf:
                names = [n for n in zf.namelist() if n and not n.endswith("/")]
                if not names:
                    raise ValueError("Release zip appears to be empty.")
                # Find common top-level prefix (e.g. "aerodrome-v3.0.1/")
                first_parts = names[0].split("/", 1)
                if len(first_parts) > 1:
                    candidate_prefix = first_parts[0] + "/"
                    has_common_prefix = all(
                        n.startswith(candidate_prefix) for n in names
                    )
                else:
                    has_common_prefix = False

                for member in zf.namelist():
                    if has_common_prefix:
                        # Strip the wrapper folder
                        stripped = member[len(candidate_prefix):]
                        if not stripped:  # the prefix entry itself
                            continue
                        dest_path = update_dir / stripped
                    else:
                        dest_path = update_dir / member

                    if member.endswith("/"):
                        dest_path.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src:
                            with open(dest_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)
        except _zipfile.BadZipFile:
            raise ValueError(
                "Downloaded file isn't a valid zip archive. "
                "The download was corrupted — try again."
            )
        except OSError as e:
            # Most likely disk full or permissions; surface raw error
            if "No space left" in str(e):
                raise ValueError(
                    "Not enough disk space to stage the update. "
                    "Free up some space and try again."
                )
            raise ValueError(f"Couldn't write staged files: {e}")
        except Exception as e:
            raise ValueError(f"Unexpected error staging zip: {e}")

    @app.post("/api/updates/github/apply")
    async def apply_github_update():
        """v3.0.1: download + verify + stage from GitHub, then apply.

        No request body — the tag to apply is read from update_state's
        last_known_latest (the same tag the user just saw on the card).
        Refuses if no update is currently available (running >= latest,
        or last_known_latest is null), so a stale UI cache can't trigger
        an unwanted apply.

        Returns the same response shape as POST /api/updates/local/apply
        — the UI can reuse the local-apply result-rendering code without
        special-casing the GitHub path."""
        cfg = _updates_config()
        if not cfg["enabled"]:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "GitHub updates are disabled in config.yaml.",
            })

        state = _get_update_state()
        latest = state["last_known_latest"]
        running = _get_running_version()
        if not latest or not _is_newer_version(latest, running):
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": (
                    "No update is currently available. "
                    "Click 'Check now' on the card to refresh, "
                    "or apply from a local zip if you have one staged."
                ),
            })

        install_dir = Path(__file__).parent

        # Fetch + verify + stage. Each step has its own clear error message.
        try:
            zip_bytes, expected_hash = _fetch_github_release_assets(latest)
            logger.info(
                f"GitHub apply: downloaded {len(zip_bytes)} bytes for {latest} "
                f"(expected sha256 {expected_hash[:16]}…)"
            )
            _verify_and_stage_github_zip(zip_bytes, expected_hash, install_dir)
            logger.info(f"GitHub apply: staged {latest} into update/, handing off to apply_local_update()")
        except ValueError as e:
            logger.warning(f"GitHub apply failed during fetch/stage: {e}")
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": str(e),
            })

        # Hand off to the existing apply flow. It already handles sudoers
        # drift pre-flight, backup, copy, deps, restart, and the cleanup
        # of update/ on success. By reusing it we get all of that for free
        # — including the same response shape, so the UI doesn't need
        # special handling for the GitHub-apply path.
        return await apply_local_update()

    @app.get("/api/updates/github/check")
    async def check_github_update(force: bool = False):
        """v3.0.0: real implementation of the GitHub update check.

        Page-load reads the cached state from SQLite — no HTTP call to
        GitHub on page load, so the 60 req/hour anonymous rate limit
        scales with polling cadence (Monthly default → ~1 call/month),
        not with traffic.

        The 'Check now' button on /updates passes force=true, which
        triggers _perform_github_check() synchronously before returning
        state. force is silently ignored when the master toggle is off
        (no point hitting GitHub when the feature is disabled).

        Response shape:
          enabled, poll_interval — current config
          running_version       — VERSION file contents (e.g. '2.98.3')
          latest_known          — last-fetched tag, or null
          available             — true iff latest_known > running (semver)
          last_check_ts         — unix ts of last attempt (success or fail)
          last_check_result     — 'success' / 'error' / null
          last_check_error      — error message if last result was error
          last_known_latest_ts  — unix ts of last successful check
          release_url           — GitHub release page URL when available
        """
        cfg = _updates_config()

        if force and cfg["enabled"]:
            _perform_github_check()

        state = _get_update_state()
        running = _get_running_version()
        latest = state["last_known_latest"]
        available = bool(latest and _is_newer_version(latest, running))
        release_url = (
            f"https://github.com/preston-peterson/aerodrome/releases/tag/{latest}"
            if (available and latest) else None
        )

        return {
            "enabled": cfg["enabled"],
            "poll_interval": cfg["poll_interval"],
            "running_version": running,
            "latest_known": latest,
            "available": available,
            "last_check_ts": state["last_check_ts"],
            "last_check_result": state["last_check_result"],
            "last_check_error": state["last_check_error"],
            "last_known_latest_ts": state["last_known_latest_ts"],
            "release_url": release_url,
        }

    def _parse_changelog(content: str):
        """Parse a Keep-a-Changelog formatted text and return a list of entries."""
        entries = []
        current = None
        current_type = None

        for line in content.splitlines():
            # New version heading: "## [2.40.1] — 2026-04-17"
            m = re.match(r"^##\s+\[([^\]]+)\]\s*[—-]\s*(.+)$", line)
            if m:
                if current is not None:
                    entries.append(current)
                current = {
                    "version": m.group(1).strip(),
                    "date": m.group(2).strip(),
                    "sections": {},
                }
                current_type = None
                continue

            # Type heading: "### Added" etc.
            m = re.match(r"^###\s+(.+)$", line)
            if m and current is not None:
                current_type = m.group(1).strip()
                current["sections"].setdefault(current_type, [])
                continue

            # Bullet: "- text..."
            m = re.match(r"^-\s+(.+)$", line)
            if m and current is not None and current_type is not None:
                current["sections"][current_type].append(m.group(1).strip())
                continue

            # Continuation line (indented text within a bullet)
            if (current is not None and current_type is not None
                    and current["sections"].get(current_type)
                    and (line.startswith("  ") or line.startswith("\t"))):
                last = current["sections"][current_type][-1]
                current["sections"][current_type][-1] = last + " " + line.strip()

        if current is not None:
            entries.append(current)
        return entries

    @app.get("/api/changelog")
    async def get_changelog(source: str = "install"):
        """Parse CHANGELOG.md and return entries as structured JSON.

        source="install" (default) reads CHANGELOG.md from the running install.
        source="update" reads CHANGELOG.md from the staged update/ folder (used
        by the Updates page to show the new version's notes before applying)."""
        install_dir = Path(__file__).parent

        if source == "update":
            # Try both possible update layouts
            candidates = [
                install_dir / "update" / "CHANGELOG.md",
                install_dir / "update" / "aerodrome" / "CHANGELOG.md",
            ]
            changelog_path = next((c for c in candidates if c.is_file()), None)
            if not changelog_path:
                return {"entries": [], "error": "No CHANGELOG.md found in update/"}
        else:
            changelog_path = install_dir / "CHANGELOG.md"
            if not changelog_path.is_file():
                return {"entries": [], "error": "CHANGELOG.md not found"}

        try:
            content = changelog_path.read_text()
        except Exception as e:
            return {"entries": [], "error": f"Could not read changelog: {e}"}

        return {"entries": _parse_changelog(content), "source": source}


    @app.get("/notification-test-ok", response_class=HTMLResponse)
    async def notification_test_ok():
        """Confirmation page reached via tap on a test notification's
        Click action. v2.47.2: exists so users can verify the full
        tap-to-open path end-to-end (ntfy delivery + Click header +
        public_url routing + phone's browser can reach the server).

        Deliberately standalone — no fetches to /api/*, no template
        dependencies, no JS beyond a tiny timestamp helper. Must render
        cleanly even if the rest of the service is degraded, because a
        user tapping this page is already in the middle of debugging
        notifications and doesn't need a cascading failure.

        Uses the shared theme palette via a <link> to /static/theme.css
        so it matches Aerodrome's look, but doesn't pull in the widget
        JS — the page has no toggle and doesn't need one.
        """
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <script>
    // Inline FOUC prevention — match the rest of the site. See the
    // <!-- theme:inline-fouc-start --> blocks in the admin templates.
    (function(){
        try {
            var saved = localStorage.getItem('aerodrome-theme') || 'auto';
            var applied = saved;
            if (saved === 'auto') {
                applied = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
            }
            document.documentElement.setAttribute('data-theme', applied);
        } catch (e) { /* ok */ }
    })();
    </script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tap-to-open working — Aerodrome</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/static/theme.css">
    <style>
    body {
        font-family: var(--font);
        background: var(--bg0); color: var(--t1);
        margin: 0; min-height: 100vh;
        display: flex; align-items: center; justify-content: center;
        padding: 24px;
    }
    .card {
        background: var(--bg1); border: 1px solid var(--bdr);
        border-radius: 12px; padding: 32px 28px; max-width: 440px;
        width: 100%; box-shadow: var(--shadow);
        text-align: center;
    }
    .checkmark {
        width: 64px; height: 64px; margin: 0 auto 16px;
        border-radius: 50%;
        background: rgba(34,197,94,0.15); color: var(--green);
        display: flex; align-items: center; justify-content: center;
        font-size: 36px; font-weight: 600;
    }
    h1 {
        font-size: 20px; margin: 0 0 6px; color: var(--t1);
        font-weight: 600;
    }
    .sub {
        color: var(--t2); font-size: 14px; margin-bottom: 22px;
        line-height: 1.5;
    }
    .rows {
        text-align: left;
        border-top: 1px solid var(--bdr);
        padding-top: 16px;
    }
    .row {
        display: flex; justify-content: space-between; gap: 12px;
        margin-bottom: 8px; font-size: 12px;
    }
    .row:last-child { margin-bottom: 0; }
    .lbl { color: var(--t3); }
    .val { color: var(--t1); font-family: var(--mono); font-size: 11px;
           word-break: break-all; text-align: right; }
    .back {
        display: inline-block; margin-top: 20px;
        color: var(--link); text-decoration: none;
        font-size: 13px;
    }
    .back:hover { color: var(--link-hover); text-decoration: underline; }
    </style>
</head>
<body>
    <div class="card">
        <div class="checkmark">&check;</div>
        <h1>Tap-to-open is working</h1>
        <div class="sub">
            You tapped an Aerodrome test notification and landed here.
            Real alerts will take you to the specific aircraft or page.
        </div>
        <div class="rows">
            <div class="row">
                <span class="lbl">Reached at</span>
                <span class="val" id="url">—</span>
            </div>
            <div class="row">
                <span class="lbl">Tapped</span>
                <span class="val" id="ts">—</span>
            </div>
        </div>
        <a href="/" class="back">← Back to dashboard</a>
    </div>
    <script>
    // Fill in the reached-at URL and tap timestamp. Done client-side so
    // the server doesn't have to know — this page is static HTML.
    document.getElementById('url').textContent = window.location.origin;
    var now = new Date();
    var pad = function(n){ return n < 10 ? '0'+n : ''+n; };
    document.getElementById('ts').textContent =
        now.getFullYear() + '-' + pad(now.getMonth()+1) + '-' + pad(now.getDate())
        + ' ' + pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());
    </script>
</body>
</html>"""
        return HTMLResponse(content=html)


    @app.get("/status", response_class=HTMLResponse)
    async def status_page():
        return _serve_template("status.html")

    @app.get("/config", response_class=HTMLResponse)
    async def config_page():
        return _serve_template("config.html")

    @app.get("/updates", response_class=HTMLResponse)
    async def updates_page():
        return _serve_template("updates.html")

    @app.get("/documentation", response_class=HTMLResponse)
    async def docs_page():
        return _serve_template("docs.html")

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page():
        return _serve_template("logs.html")

    @app.get("/performance", response_class=HTMLResponse)
    async def performance_page():
        """v2.41.8: diagnostic page that renders /api/perf/diagnostics.
        Accessed from the Updates page or gear menu; intended for debugging
        slow-hardware scenarios (Pi with large databases, etc.).

        v2.41.23: Performance is now one of several diagnostics reachable
        from the /diagnostics hub. The page itself is unchanged; only the
        navigation path to it changed."""
        return _serve_template("performance.html")

    # v2.53.0: dedicated aircraft detail page. URL pattern is path-based
    # (`/aircraft/{ICAO}`) so it's bookmarkable and shareable. Inline
    # drill on Search/All stays as the in-flow lookup; this page is the
    # deep-dive linked from the drill via "View full detail ↗".
    #
    # ICAO is forced to uppercase canonical form. Lowercase URLs
    # 301-redirect to the uppercase variant — the DB stores them
    # uppercase and we want a single canonical URL per aircraft to
    # avoid bookmark fragmentation and weird cache behavior.
    @app.get("/aircraft/{icao}", response_class=HTMLResponse)
    async def aircraft_detail_page(icao: str):
        # Validate format first — 6 hex chars, no exceptions
        if len(icao) != 6 or not all(c in "0123456789ABCDEFabcdef" for c in icao):
            raise HTTPException(status_code=400, detail="invalid ICAO hex")
        # Canonicalize case via redirect
        if icao != icao.upper():
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/aircraft/{icao.upper()}", status_code=301)
        return _serve_template("aircraft.html")

    @app.get("/api/aircraft/{icao}")
    async def get_aircraft_detail(icao: str):
        """v2.53.0: rich detail dataset for the /aircraft/{ICAO} page.
        Superset of /api/search/aircraft/{icao} — adds hour-of-day and
        day-of-week distributions, altitude/speed ranges, derived
        pattern chips, and the 20 most recent sightings with positions.

        v2.57.0: annotates the response with is_military/mil_label/
        mil_color and is_watchlist/watchlist_label so the detail-page
        template can render the same MIL pill and orange WATCHLIST
        chip that appear elsewhere in the app.

        The 'mode' field in the response indicates whether the install's
        data is rich enough for analytical sections — full mode renders
        chips and distributions, sparse mode (sighting_count <
        LOW_SIGHTING_THRESHOLD, default 10) renders facts + sightings
        table only. The frontend respects this and hides the meaningless
        sections rather than showing them with degenerate data."""
        from search import detail_page_data_for_aircraft
        if len(icao) != 6 or not all(c in "0123456789ABCDEFabcdef" for c in icao):
            raise HTTPException(status_code=400, detail="invalid ICAO hex")
        # v2.85.0: tuned connection. This endpoint runs the heaviest
        # work of any hot path — detail_page_data_for_aircraft fires
        # ~10 sub-queries (recent_sightings, sightings_hourly hour-of-day
        # GROUP BY, day-of-week GROUP BY, altitude bands, daily totals,
        # cruise altitude, primary callsign, etc). On the previous
        # default-cache (2 MB) connection, the per-aircraft working set
        # didn't survive across sub-queries, forcing repeated disk
        # reads of the same pages. On a Pi 4B 4 GB install with a
        # 2 GB database and other services competing for OS page cache,
        # this manifested as 3-4 minute detail-page load times. The
        # tuned connection (32 MB cache + 128 MB mmap on default
        # profile) lets the working set survive — most index pages and
        # the relevant table pages stay resident across the whole
        # detail-data fetch. See diagnostics-slow-queries page for
        # a connection-tuning audit.
        conn = _open_db_conn(CONFIG["data"]["db_file"])
        try:
            d = detail_page_data_for_aircraft(conn, icao.upper())
        finally:
            conn.close()
        if d is None:
            raise HTTPException(status_code=404, detail="aircraft not found")

        # v2.57.0: annotate military / watchlist status. detail_page_data_for_aircraft
        # returns the aircraft's denormalized fields (icao, callsign, type,
        # type_desc) which is exactly what the annotation helpers expect.
        # Also expose 'callsign' as an alias of 'last_callsign' since
        # _annotate_military / _annotate_watchlist read the generic key.
        if d.get("last_callsign") and not d.get("callsign"):
            d["callsign"] = d["last_callsign"]
        _annotate_military(d)
        _annotate_watchlist(d)

        return d

    @app.get("/api/aircraft/{icao}/positions")
    async def get_aircraft_positions(icao: str, window: str = "24h"):
        """v2.86.0: position stream for the aircraft-detail-page map.
        Returns every position fix recorded for one ICAO over the
        selected time window, plus the receiver's own coordinates so
        the client knows where to anchor the map.

        The map renders dots — one per sighting, color-coded by
        altitude — without any track-line connection between them.
        That design (dots-only, no session-grouping heuristics) was
        chosen so the dot density itself communicates flight pattern:
        heavy fliers cluster, transients show single dots, repeat
        visitors stand out. Users wanting actual flight paths use the
        external Track ↗ link to airplanes.live or similar.

        Query parameters:
          window — one of "24h", "7d", "30d", "all". Default "24h".

        Response shape:
          {
            "ok": true,
            "icao": "ACF27F",
            "window": "24h",
            "count": 4193,
            "positions": [[seen_at, lat, lon, altitude_or_null], ...],
            "receiver": {"lat": 37.7, "lon": -122.4} | null,
            "truncated": false  (true if cap was hit; see below)
          }

        Capping: a runaway 30-day window for a heavy local flier could
        return 50k+ points, which is more than Leaflet's canvas
        renderer wants to handle comfortably on a Pi. If the result
        set exceeds POSITION_LIMIT we evenly sample down to that limit
        and set truncated=true so the frontend can surface a small
        notice. Recent positions are kept verbatim — the sampling is
        applied after sorting by seen_at DESC, then re-reversed for
        output, so the most recent N are always present even when the
        window is wide enough to need truncation. Limit is generous
        (10k points) to avoid kicking in at typical scales — most
        aircraft × most windows produce well under that count.
        """
        POSITION_LIMIT = 10_000
        if len(icao) != 6 or not all(c in "0123456789ABCDEFabcdef" for c in icao):
            raise HTTPException(status_code=400, detail="invalid ICAO hex")
        valid_windows = {"24h": 86400, "7d": 7 * 86400,
                         "30d": 30 * 86400, "all": None}
        if window not in valid_windows:
            raise HTTPException(
                status_code=400,
                detail=f"window must be one of {sorted(valid_windows)}"
            )
        seconds = valid_windows[window]
        now = int(time.time())
        # Receiver coords for client-side map anchoring. If unconfigured
        # the frontend renders a placeholder with a config nudge instead
        # of trying to render the map without anchor — we still return
        # null here so the client can detect the case explicitly.
        rx_cfg = CONFIG.get("receiver") or {}
        receiver = None
        if rx_cfg.get("latitude") is not None and rx_cfg.get("longitude") is not None:
            receiver = {
                "lat": float(rx_cfg["latitude"]),
                "lon": float(rx_cfg["longitude"]),
            }

        conn = _open_db_conn(CONFIG["data"]["db_file"])
        try:
            if seconds is None:
                rows = conn.execute(
                    "SELECT seen_at, lat, lon, altitude FROM all_sightings "
                    "WHERE icao = ? AND lat IS NOT NULL AND lon IS NOT NULL "
                    "ORDER BY seen_at DESC",
                    (icao.upper(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT seen_at, lat, lon, altitude FROM all_sightings "
                    "WHERE icao = ? AND seen_at >= ? "
                    "  AND lat IS NOT NULL AND lon IS NOT NULL "
                    "ORDER BY seen_at DESC",
                    (icao.upper(), now - seconds),
                ).fetchall()
        finally:
            conn.close()

        truncated = False
        if len(rows) > POSITION_LIMIT:
            # Even-sample down to the limit. Stride = ceil(N / limit).
            # Recent positions land at indexes 0, stride, 2*stride, ...
            # — so the first ~POSITION_LIMIT samples taken include the
            # most recent observation regardless of window size.
            stride = (len(rows) + POSITION_LIMIT - 1) // POSITION_LIMIT
            rows = rows[::stride]
            truncated = True

        # Reverse back to chronological order for client convenience —
        # frontends typically render in the order they receive points,
        # and chronological-ascending makes "first observation" /
        # "most recent" reasoning straightforward.
        positions = [
            [row[0], row[1], row[2], row[3]]
            for row in reversed(rows)
        ]

        return {
            "ok": True,
            "icao": icao.upper(),
            "window": window,
            "count": len(positions),
            "positions": positions,
            "receiver": receiver,
            "truncated": truncated,
        }

    @app.get("/diagnostics", response_class=HTMLResponse)
    async def diagnostics_hub_page():
        """v2.41.23: hub page listing all available troubleshooting diagnostics.
        Each registered diagnostic appears as a card linking to its dedicated
        page. The hub itself is pure static content; the registry lives in the
        template's JavaScript so adding a new diagnostic doesn't touch server
        code except to register a route for the new page."""
        return _serve_template("diagnostics.html")

    @app.get("/diagnostics/watchlist-alerts", response_class=HTMLResponse)
    async def diagnostics_watchlist_page():
        """v2.41.23: client-side diagnostic that captures the state driving
        the Watchlist tab pulse. Snapshots localStorage, the /api/watchlist
        response, and the watchlist_alerts configuration, then evaluates
        what the alert logic would decide given that state. Produces a
        pasteable text report for sharing in support threads."""
        return _serve_template("diagnostics-watchlist.html")

    # =========================================================================
    # v2.84.0: Slow-query diagnostic — page + API endpoints
    # =========================================================================
    # Captures slow queries from instrumented endpoints (/api/aircraft/{ICAO},
    # /api/all/drill, /api/stats) into a process-local ring buffer that the
    # diagnostic page renders without requiring SSH or file-log access. Built
    # because the previous workflow for a slow page report involved asking
    # the user to grep journalctl, capture access logs, and run sqlite3
    # EXPLAIN QUERY PLAN by hand — a friction wall that meant the data we
    # actually needed for triage was never obtained. The page surfaces the
    # last N slow events with their EXPLAIN output, a button to run EXPLAIN
    # against the user's actual data on demand, and a static audit of
    # endpoints that aren't using the tuned connection helper.

    @app.get("/diagnostics/slow-queries", response_class=HTMLResponse)
    async def diagnostics_slow_queries_page():
        """Slow-query diagnostic page (v2.84.0)."""
        return _serve_template("diagnostics-slow-queries.html")

    @app.get("/api/diagnostics/slow-queries/recent")
    async def get_recent_slow_queries(limit: int = Query(50, ge=1, le=200)):
        """Return the most recent slow queries from the in-memory ring.
        Page reloads call this on a button click; no automatic poll
        (the ring is bounded and meant for triage-during-incident, not
        ambient monitoring)."""
        from slow_query_log import recent_slow_queries, capacity
        return {
            "ok": True,
            "entries": recent_slow_queries(limit=limit),
            "capacity": capacity(),
        }

    @app.post("/api/diagnostics/slow-queries/clear")
    async def clear_slow_queries():
        """Empty the ring. Backs the diagnostic page's "Clear" button."""
        from slow_query_log import clear
        clear()
        return {"ok": True}

    @app.get("/api/diagnostics/slow-queries/explain")
    async def explain_query_plan(query: str = Query(..., regex=r"^[a-z_]+$")):
        """Run EXPLAIN QUERY PLAN against a known query shape with a
        representative parameter value drawn from the user's actual data.
        `query` is a label naming one of the canonical shapes, NOT raw
        SQL — letting users post arbitrary SQL would be an injection
        vector. Whitelist of supported shapes is right below."""
        # Pick a representative ICAO: the seen_aircraft row with the most
        # sightings. This gives a worst-case plan because heavy flyers
        # are exactly when planner mis-picks hurt the most. If the table
        # is empty, return a graceful empty response.
        db_path = CONFIG["data"]["db_file"]
        conn = _open_db_conn(db_path)
        try:
            sample = conn.execute(
                "SELECT icao, sighting_count FROM seen_aircraft "
                "ORDER BY sighting_count DESC LIMIT 1"
            ).fetchone()
            if sample is None:
                return {
                    "ok": False,
                    "error": "no aircraft in seen_aircraft to use as a sample",
                }
            sample_icao = sample[0]
            sample_count = sample[1] or 0
            now = int(time.time())

            # Whitelisted query shapes. Each entry pairs a label with the
            # query the production endpoint actually runs. When new
            # endpoints get instrumented, their query shapes should be
            # added here so the diagnostic UI can EXPLAIN them too.
            shapes = {
                "drill_select": (
                    "SELECT icao, callsign, speed, lat, lon, altitude, "
                    "aircraft_type, type_desc, seen_at, squawk "
                    "FROM all_sightings "
                    "WHERE icao = ? AND seen_at >= ? AND seen_at <= ? "
                    "ORDER BY seen_at DESC LIMIT ? OFFSET ?",
                    (sample_icao, 0, now, 100, 0),
                ),
                "drill_count": (
                    "SELECT COUNT(*) AS n FROM all_sightings "
                    "WHERE icao = ? AND seen_at >= ? AND seen_at <= ?",
                    (sample_icao, 0, now),
                ),
                "detail_recent_sightings": (
                    "SELECT seen_at, callsign, altitude, speed, lat, lon "
                    "FROM all_sightings WHERE icao = ? "
                    "ORDER BY seen_at DESC LIMIT 20",
                    (sample_icao,),
                ),
            }
            if query not in shapes:
                return {
                    "ok": False,
                    "error": f"unknown query shape: {query}. "
                             f"Known: {list(shapes.keys())}",
                }
            sql, params = shapes[query]

            # Capture plan + actual execution time. Both useful: plan
            # tells us which index, time tells us how bad it is on
            # this user's data right now.
            plan_rows = conn.execute(
                "EXPLAIN QUERY PLAN " + sql, params
            ).fetchall()
            plan = [
                row[3] if len(row) >= 4 else str(row)
                for row in plan_rows
            ]
            t0 = time.time()
            conn.execute(sql, params).fetchall()
            duration_ms = (time.time() - t0) * 1000

            return {
                "ok": True,
                "query_label": query,
                "sql": sql,
                "params": list(params),
                "sample_icao": sample_icao,
                "sample_sighting_count": sample_count,
                "plan": plan,
                "duration_ms": round(duration_ms, 1),
            }
        finally:
            conn.close()

    @app.get("/api/diagnostics/connection-tuning-audit")
    async def connection_tuning_audit():
        """Static report of which endpoints use the tuned connection
        helper (`_open_db_conn`) vs. raw `sqlite3.connect()`. The list
        is hand-maintained — kept short and deliberate rather than
        auto-detected because auto-detection would lag refactors and
        false-positive on test code. A "Last verified" version stamp
        keeps the maintenance honest."""
        # Format: list of {endpoint, tuned, notes}. "tuned" means the
        # endpoint goes through _open_db_conn (cache_size, mmap_size,
        # temp_store applied per the user's profile). "notes" surfaces
        # the impact when an endpoint is untuned.
        return {
            "ok": True,
            "verified_in_version": "2.85.0",
            "tuned_helper": "_open_db_conn (collector.py)",
            "endpoints": [
                {
                    "endpoint": "/api/all/drill",
                    "tuned": True,
                    "notes": "Sightings table on aircraft detail page.",
                },
                {
                    "endpoint": "/api/stats",
                    "tuned": True,
                    "notes": "Stats page; uses tuned connection.",
                },
                {
                    "endpoint": "/api/search",
                    "tuned": True,
                    "notes": "Search hot path. Switched to tuned in "
                             "v2.85.0 — was opening with default 2MB "
                             "cache regardless of profile setting.",
                },
                {
                    "endpoint": "/api/search/aircraft/{icao}",
                    "tuned": True,
                    "notes": "Inline drill from search results. "
                             "Switched to tuned in v2.85.0.",
                },
                {
                    "endpoint": "/api/aircraft/{icao}",
                    "tuned": True,
                    "notes": "Aircraft detail page data — fires ~10 "
                             "sub-queries per page load. Switched to "
                             "tuned in v2.85.0; was almost certainly "
                             "the dominant cause of multi-minute "
                             "detail-page loads on memory-constrained "
                             "installs (Pi-class hardware with large "
                             "DBs).",
                },
                {
                    "endpoint": "/aircraft/{icao} (HTML)",
                    "tuned": True,
                    "notes": "Static template route; runs only the "
                             "typeahead suggestions query. Switched to "
                             "tuned in v2.85.0 for consistency.",
                },
            ],
        }


    # =========================================================================
    # /api/docs — serve raw markdown docs for the in-app viewer
    # =========================================================================
    # Whitelisted so we only serve project documentation — NOT an arbitrary
    # file read. Values are paths relative to the install directory.
    #
    # Note on update_readme: the staging-folder docs file is named
    # UPDATE_README.md (not README.md) deliberately. Having two files named
    # README.md in a release zip — one at the root and one in update/ — led
    # to an iter-order bug in the apply flow where the staging README could
    # clobber the root README (or vice versa) during copy. Giving the staging
    # README a distinct name makes the collision impossible and lets the
    # apply flow be dead simple.
    DOC_FILES = {
        "readme":         "README.md",
        "install":        "docs/INSTALL.md",
        "changelog":      "CHANGELOG.md",
        "contributing":   "CONTRIBUTING.md",
        "scripts_readme": "scripts/README.md",
        "update_readme":  "update/UPDATE_README.md",
        "performance":    "docs/PERFORMANCE.md",
        "search_syntax":  "docs/SEARCH_SYNTAX.md",
        "license":        "LICENSE",
    }

    @app.get("/api/docs/{slug}", response_class=PlainTextResponse)
    async def get_doc(slug: str):
        """Return the raw markdown for the named doc."""
        rel = DOC_FILES.get(slug)
        if not rel:
            return PlainTextResponse("Unknown doc slug", status_code=404)
        path = Path(__file__).parent / rel
        if not path.is_file():
            return PlainTextResponse(f"# {slug}\n\nDoc file not found.",
                                     status_code=404)
        return PlainTextResponse(path.read_text(encoding="utf-8"))

        # Serve screenshots that README references as docs/screenshot-*.png so
    # images render inside the in-app doc viewer. Restricted to filenames
    # matching the pattern — not an arbitrary static-file server.
    @app.get("/docs/{filename}")
    async def get_doc_asset(filename: str):
        from fastapi.responses import FileResponse
        # Reject anything that isn't a bare file name inside docs/ — no
        # traversal allowed.
        if "/" in filename or ".." in filename or filename.startswith("."):
            return PlainTextResponse("Not found", status_code=404)
        if not re.match(r"^[A-Za-z0-9_\-]+\.(png|jpg|jpeg|gif|webp|svg)$",
                        filename):
            return PlainTextResponse("Not found", status_code=404)
        path = Path(__file__).parent / "docs" / filename
        if not path.is_file():
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(path)

    # =========================================================================
    # /api/logs — read-only access to the service log file
    # =========================================================================
    # Intentionally read-only: view + download only. No clear/delete endpoints.
    # Aerodrome's log file lives at logs/tracker.log (set up in main.py's
    # setup_logging). We hard-code that path here rather than reading config,
    # because the log is opened by that path before this module runs.
    def _log_file_path() -> Path:
        log_dir = CONFIG.get("logging", {}).get("dir", "logs")
        return Path(__file__).parent / log_dir / "tracker.log"

    @app.get("/api/logs/info")
    async def get_logs_info():
        """Return file size, line count (estimated from size for large files,
        exact for small ones), and last-modified timestamp."""
        p = _log_file_path()
        if not p.is_file():
            return {"path": str(p), "size_bytes": 0, "line_count": 0,
                    "mtime": None, "error": "Log file not found"}
        try:
            size = p.stat().st_size
            mtime = int(p.stat().st_mtime)
            # For files under ~20MB, count lines exactly. For larger files,
            # skip counting — the browser never needs to know exactly, and
            # counting 100MB line-by-line is wasteful on every refresh.
            if size < 20 * 1024 * 1024:
                with open(p, "rb") as f:
                    line_count = sum(1 for _ in f)
            else:
                line_count = None  # front-end will show "—"
            return {"path": str(p), "size_bytes": size,
                    "line_count": line_count, "mtime": mtime, "error": None}
        except Exception as e:
            return {"path": str(p), "size_bytes": 0, "line_count": 0,
                    "mtime": None, "error": str(e)}

    # Cap on how much we'll ever return in a single /tail response, to keep
    # the browser from having to render multi-hundred-MB strings even if the
    # user clicks "Full" on a huge file.
    LOG_TAIL_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

    @app.get("/api/logs/tail", response_class=PlainTextResponse)
    async def get_logs_tail(n: int = Query(500, ge=0, le=200_000)):
        """Return the last N lines of the log, or the full file if n == 0.
        Text is returned as plain text for the browser to split and render."""
        p = _log_file_path()
        if not p.is_file():
            return PlainTextResponse("", status_code=200)
        try:
            size = p.stat().st_size
            if n == 0:
                # Full file — but clamp to LOG_TAIL_MAX_BYTES from the end so
                # we never OOM. If the file is bigger than the cap, return the
                # tail-cap worth and a header comment explaining the trim.
                if size <= LOG_TAIL_MAX_BYTES:
                    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))
                with open(p, "rb") as f:
                    f.seek(-LOG_TAIL_MAX_BYTES, 2)
                    # Skip the (likely partial) first line
                    f.readline()
                    data = f.read()
                trimmed = data.decode("utf-8", errors="replace")
                banner = (f"# --- Log file is {size:,} bytes. Showing the last "
                          f"{LOG_TAIL_MAX_BYTES // (1024*1024)} MB. "
                          f"Use Download for the full file. ---\n")
                return PlainTextResponse(banner + trimmed)
            # Last N lines via a tail-from-end read. For modest N, reading
            # the whole file and splitting is simpler than a reverse scan
            # and still fast.
            if size < 10 * 1024 * 1024:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                return PlainTextResponse("\n".join(lines[-n:]))
            # Large file: read a chunk from the end large enough to contain
            # at least n lines (assume ~200 bytes/line avg, round up generously).
            chunk = min(size, max(n * 400, 64 * 1024))
            with open(p, "rb") as f:
                f.seek(-chunk, 2)
                data = f.read()
            text = data.decode("utf-8", errors="replace")
            # Drop partial first line since we may have started mid-line
            if "\n" in text:
                text = text.split("\n", 1)[1]
            lines = text.splitlines()
            return PlainTextResponse("\n".join(lines[-n:]))
        except Exception as e:
            return PlainTextResponse(f"# error reading log: {e}", status_code=500)

    @app.get("/api/logs/download")
    async def download_log():
        """Serve the log file as an attachment download."""
        from fastapi.responses import FileResponse
        p = _log_file_path()
        if not p.is_file():
            return PlainTextResponse("Log file not found", status_code=404)
        return FileResponse(
            path=p,
            media_type="text/plain",
            filename=f"tracker-{int(time.time())}.log",
        )

    return app


def _get_system_info():
    """Collect system stats using psutil.

    CPU measurement uses the non-blocking delta pattern — psutil.cpu_percent()
    called with interval=None returns the percentage over the interval SINCE
    THE LAST CALL. For the Status page's 10-second auto-refresh, that gives a
    stable 10-second rolling average, which is what tools like top/htop show.

    The previous implementation used interval=0.1 (a 100ms blocking sample),
    which consistently under-reported CPU usage: Aerodrome polls the receiver
    every 60 seconds, so a 100ms sample almost always landed in an idle
    window and read near-zero even when the host was at 25% average.

    cpu_percent() also needs to be "primed" — per psutil docs, the very first
    call with interval=None returns a meaningless 0.0 because there's no
    prior call to measure against. We prime it at module import below so
    the first /api/status request returns real numbers rather than zeros.
    """
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        started_at = int(proc.create_time())
        uptime_seconds = int(time.time() - started_at)

        # Host-wide CPU since last call (non-blocking).
        host_cpu = psutil.cpu_percent(interval=None)

        # Process CPU since last call (non-blocking). Divide by cpu_count to
        # normalize: proc.cpu_percent() returns up to N*100 on N cores (e.g.
        # a 4-core box running one busy thread at 100% of one core reports
        # 100; a four-thread busy loop reports 400). Normalizing to 0..100
        # makes the meter bar behave naturally and lets the user compare
        # Aerodrome's slice against the host total at a glance.
        raw_proc_cpu = proc.cpu_percent(interval=None)
        n_cores = psutil.cpu_count() or 1
        proc_cpu = raw_proc_cpu / n_cores

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        # v2.95.0: swap memory. Collected the same way as virtual memory.
        # On systems without swap configured (or all-zero swap), psutil
        # still returns a swap object — total_mb of 0 in that case is the
        # signal the UI uses to skip rendering the row entirely. swap_health
        # gives the UI a colour-tier hint matched to the load_health pattern:
        #   used_mb == 0          → ok       (swap exists but unused — the common case)
        #   percent <  10         → light    (a little spillover, usually fine)
        #   percent <  50         → busy     (real pressure, worth investigating)
        #   percent >= 50         → warn     (sustained swap = paging, perf will suffer)
        # Threshold tiers chosen to match the rest of the diagnostics card
        # palette (ok / busy / warn / overload). "light" is added because
        # zero-vs-non-zero is the most important distinction users care about
        # — any non-zero swap on a healthy box deserves a visual cue.
        swap = psutil.swap_memory()
        swap_health = None
        if swap.total > 0:
            if swap.used == 0:
                swap_health = "ok"
            elif swap.percent < 10:
                swap_health = "light"
            elif swap.percent < 50:
                swap_health = "busy"
            else:
                swap_health = "warn"

        # v2.41.6: 1/5/15-minute load averages. These are the canonical Unix
        # triple from /proc/loadavg. Load is defined as "average number of
        # processes running or waiting for I/O", so it needs CPU-count
        # context to interpret: on a 4-core box, load of 4.0 is 100% busy,
        # not overloaded. We include load_health as a hint so the UI
        # doesn't have to re-derive the threshold.
        #
        # Health tiers (standard sysadmin heuristic):
        #   load_1m / cores < 0.7  → ok      (plenty of headroom)
        #   load_1m / cores < 1.0  → busy    (at capacity, no headroom)
        #   load_1m / cores < 2.0  → warn    (backlog forming)
        #   load_1m / cores >= 2.0 → overload
        load_avg = None
        load_health = None
        try:
            la1, la5, la15 = os.getloadavg()
            load_avg = {
                "1m": round(la1, 2),
                "5m": round(la5, 2),
                "15m": round(la15, 2),
            }
            ratio = la1 / max(n_cores, 1)
            if ratio < 0.7:
                load_health = "ok"
            elif ratio < 1.0:
                load_health = "busy"
            elif ratio < 2.0:
                load_health = "warn"
            else:
                load_health = "overload"
        except (OSError, AttributeError):
            # os.getloadavg raises on Windows; AttributeError on obscure
            # platforms without that module function. Silently omit.
            pass

        return {
            "ok": True,
            "uptime_seconds": uptime_seconds,
            "started_at": started_at,
            "cpu_percent": round(host_cpu, 1),
            "process_cpu_percent": round(proc_cpu, 1),
            "cpu_cores": n_cores,
            "load_average": load_avg,
            "load_health": load_health,
            "memory": {
                "used_mb": round(mem.used / (1024 * 1024)),
                "total_mb": round(mem.total / (1024 * 1024)),
                "percent": mem.percent,
            },
            "swap": {
                "used_mb": round(swap.used / (1024 * 1024)),
                "total_mb": round(swap.total / (1024 * 1024)),
                "percent": swap.percent,
                "health": swap_health,
            },
            "disk": {
                "used_gb": round(disk.used / (1024 ** 3), 1),
                "total_gb": round(disk.total / (1024 ** 3), 1),
                "percent": disk.percent,
            },
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Prime psutil's cpu_percent counters at import so the first /api/status
# call returns real numbers. Both the host-wide counter and our process
# counter need priming because the first interval=None call on each
# returns 0.0 by design. Silent no-op if psutil isn't available — the
# status endpoint will fall back to the {"ok": False, ...} error path.
try:
    import psutil as _psutil_prime
    _psutil_prime.cpu_percent(interval=None)
    _psutil_prime.Process(os.getpid()).cpu_percent(interval=None)
except Exception:
    pass


def _save_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(CONFIG, f, default_flow_style=False, sort_keys=False)
        logger.info("Config saved")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")


def _save_config_preserving_comments():
    """Save CONFIG to disk, preserving comments and formatting from the live file
    when possible (via ruamel.yaml). Falls back to plain PyYAML if unavailable."""
    try:
        from ruamel.yaml import YAML
        ry = YAML()
        ry.preserve_quotes = True
        ry.indent(mapping=2, sequence=4, offset=2)

        # Load existing file with ruamel so we keep its comments, then overwrite
        # values with our in-memory config recursively.
        with open(CONFIG_PATH) as f:
            doc = ry.load(f)

        def _overlay(target, source):
            """Apply source values onto target, preserving comments in target.
            Removes keys from target that don't exist in source."""
            # Remove keys no longer present
            for k in list(target.keys()):
                if k not in source:
                    del target[k]
            # Apply values from source
            for k, v in source.items():
                if k in target and isinstance(target[k], dict) and isinstance(v, dict):
                    _overlay(target[k], v)
                else:
                    target[k] = v

        if doc is None:
            doc = {}
        _overlay(doc, CONFIG)

        with open(CONFIG_PATH, "w") as f:
            ry.dump(doc, f)
        logger.info("Config saved (comments preserved)")
    except ImportError:
        # ruamel.yaml not installed — fall back to plain save
        _save_config()
    except Exception as e:
        logger.error(f"Failed to persist config via ruamel: {e}; falling back")
        _save_config()
