# Version: 3.4.176
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

import asyncio
import logging
import math
import re
import shutil
import sqlite3
import time
import os
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from contextlib import contextmanager

import requests as req
import yaml
from fastapi import FastAPI, Query, UploadFile, File, HTTPException, Request, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from urllib.parse import urlsplit

# Local — ICAO code → friendly display name lookups for the Stats cards.
from designators import airline_name, aircraft_type_name
# v2.50.13: central DB-connection helper that applies tuning pragmas
import tzwindow
from search import window_sources
from collector import (_open_db_conn, aircraft_type_for, analyze_db, sync_watchlist_tails,
                       watchlist_tail_map, watchlist_tail_status)
# v2.79.0 (Phase 3 polish): centralized distance + bearing helpers.
# Pre-v2.79.0 the haversine math was duplicated across four sites
# (this file's _haversine, the stats endpoint local def, the records
# endpoint local def, and collector.py). distance.py is the single
# source of truth now. Numerical output matches the legacy inline
# definitions to display precision (1 decimal); see distance.py's
# module docstring for the conversion-factor note.
from distance import haversine as _dist_haversine, to_user_unit as _dist_to_user_unit, compass_bearing as _dist_compass_bearing
# v3.4.147: staged update apply + boot guard (stdlib only; imports no app
# module, so main.py can run it first) and crash-safe small-file writes.
import threading

import updater
import atomic_io
# v3.4.148: every config.yaml write goes through config_io (all or nothing,
# under CONFIG_LOCK).
import config_io
# v3.4.149: a full-backup restore is checked and staged here, and switched
# in at the next start (restore_io).
import restore_io
# v3.4.150: the one module that runs sudo (the system helper, the restart
# and the sudoers read).
import privhelper

# v3.4.168: GET /api/config shows a saved FlightAware AeroAPI key as this placeholder,
# never in full (lookups are billed to the key's owner); a save carrying it keeps the key.
FA_KEY_KEPT = "__kept__"

logger = logging.getLogger("adsb.server")

CONFIG = {}
CONFIG_PATH = ""

# The live sudoers drop-in install.sh writes. Read at call time by the drift
# checks, so tests (and a throwaway local instance) can point it elsewhere.
SUDOERS_DROPIN = Path("/etc/sudoers.d/aerodrome")

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


def _dp_simplify_track(pts: list, eps_deg: float) -> list:
    """Douglas-Peucker line simplification for a ground track.

    `pts` is a list of [t, lat, lon, alt] fixes in time order; returns a
    subset preserving the shape of the path: endpoints are always kept, and
    interior points are kept only where dropping them would move the line by
    more than `eps_deg` (perpendicular distance, in degrees). Turns and
    curves survive; long straight cruise legs collapse to their endpoints.

    This beats blind even-downsampling for trails — a stride sampler throws
    away the very corner points that define a track's shape and keeps
    redundant ones along the straights. DP spends its point budget where the
    path actually bends, so the same (or fewer) points read as a sharper
    trail.

    Longitude deltas are scaled by cos(lat) so a degree of lon counts for the
    real ground distance it covers at this latitude — otherwise east-west
    wander near the poles would be over-weighted and north-south under-. The
    altitude channel rides along on the kept fixes but is not part of the
    distance metric (we simplify the 2-D ground path, not the climb profile).

    Iterative (explicit stack) so a pathologically long track can't blow the
    recursion limit.
    """
    n = len(pts)
    if n < 3:
        return pts[:]
    # cos(lat) at the track's mid-latitude — one factor for the whole segment
    # is plenty at trail scales (a single pass spans a few tenths of a degree).
    mid_lat = pts[n // 2][1]
    cos_lat = math.cos(math.radians(mid_lat)) or 1e-6
    eps2 = eps_deg * eps_deg

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        ax, ay = pts[lo][2] * cos_lat, pts[lo][1]
        bx, by = pts[hi][2] * cos_lat, pts[hi][1]
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        max_d2 = -1.0
        max_i = lo
        for i in range(lo + 1, hi):
            px, py = pts[i][2] * cos_lat, pts[i][1]
            if seg_len2 <= 1e-18:
                # endpoints coincide — fall back to point distance
                ex, ey = px - ax, py - ay
                d2 = ex * ex + ey * ey
            else:
                # perpendicular distance from point to the A-B line
                t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                cx, cy = ax + t * dx, ay + t * dy
                ex, ey = px - cx, py - cy
                d2 = ex * ex + ey * ey
            if d2 > max_d2:
                max_d2 = d2
                max_i = i
        if max_d2 > eps2:
            keep[max_i] = True
            stack.append((lo, max_i))
            stack.append((max_i, hi))
    return [pts[i] for i in range(n) if keep[i]]


def _attach_airline_name(row: dict, operator_code: str | None) -> dict:
    """v3.4.35: if `operator_code` maps to a known airline, set
    `row['name']` to the friendly name. Returns the row for chaining.
    No-op if `operator_code` is None/empty or `airline_name()` doesn't
    recognize the code (we never fabricate names).

    Consolidates the 4-line "lookup-name, attach-if-truthy" pattern
    that was duplicated in two card-rendering paths (top_operators
    and the all_operators drill). The duplication was trivial but
    real — extracting prevents future drift where one site picks up
    a behavior (e.g. fallback name, normalization) and the other
    doesn't."""
    if not operator_code:
        return row
    nm = airline_name(operator_code)
    if nm:
        row["name"] = nm
    return row


# Largest offset / from_ts / to_ts /api/search passes on to SQLite.
_SEARCH_INT_MAX = 2 ** 53


def _histogram_offset_sql(segments):
    """SQL for seen_at's UTC offset across tzwindow.offset_segments. Computed
    integers, so safe to inline."""
    if len(segments) == 1:
        return str(int(segments[0][2]))
    whens = " ".join(f"WHEN seen_at < {int(end)} THEN {int(off)}"
                     for _, end, off in segments[:-1])
    return f"CASE {whens} ELSE {int(segments[-1][2])} END"


def _build_notifier():
    """Construct a Notifier from the current CONFIG. Returns the notifier
    instance. Idempotent wrt state (reinvoking just rebuilds config)."""
    from notifier import Notifier
    nt_cfg = CONFIG.get("notifications") or {}
    stats_tz = (CONFIG.get("stats") or {}).get("timezone") or None
    demo_enabled = bool((CONFIG.get("demo") or {}).get("enabled", False))
    return Notifier(nt_cfg, stats_timezone=stats_tz, demo_enabled=demo_enabled)


def _refresh_notifier_config():
    """Apply the current CONFIG to the existing notifier. Called after
    a PUT /api/config that touched the notifications block."""
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = _build_notifier()
    else:
        nt_cfg = CONFIG.get("notifications") or {}
        stats_tz = (CONFIG.get("stats") or {}).get("timezone") or None
        demo_enabled = bool((CONFIG.get("demo") or {}).get("enabled", False))
        _NOTIFIER.update_config(nt_cfg, stats_timezone=stats_tz,
                                demo_enabled=demo_enabled)
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


def _install_dir() -> Path:
    """The live install's directory. Every maintenance endpoint (updates,
    backups, restores, config import) locates the install through this one
    function, so a test can point all of them at a throwaway install."""
    return Path(__file__).parent


def _pre_restore_install_dir() -> Path:
    return _install_dir()


# The web address this process was started to serve (set by get_app):
# its own listening socket holds that port until the next restart.
_SERVING_WEB = (None, None)


def _machine_key_problems(new_cfg: dict, old_cfg: dict) -> List[tuple]:
    """v3.4.148: [(path, message)] for settings that can't work on THIS
    machine, checked only where they changed from the running config (so a
    save that doesn't touch them is never refused, and config_validator —
    which also checks other machines' backups — stays machine-independent):

      web.host      must be an address of this machine (a bind to port 0
                    gives EADDRNOTAVAIL otherwise; 0.0.0.0 and :: always pass)
      web.port      must be free for Aerodrome to listen on: not held by
                    another program (EADDRINUSE) and not a port this account
                    may not use (EACCES, below 1024). Not checked when it is
                    the port this process already listens on.
      data.db_file  its folder must exist and be writable"""
    import errno as _errno
    import socket as _socket
    problems = []
    new_web, old_web = new_cfg.get("web") or {}, old_cfg.get("web") or {}
    host = new_web.get("host")
    if isinstance(host, str) and host.strip() and host != old_web.get("host"):
        h = host.strip()
        fam = _socket.AF_INET6 if ":" in h else _socket.AF_INET
        try:
            s = _socket.socket(fam, _socket.SOCK_STREAM)
            try:
                s.bind((h.strip("[]"), 0))
            finally:
                s.close()
        except _socket.gaierror:
            problems.append(("web.host", f"{h} can't be found from this machine — use "
                                         f"0.0.0.0 to listen on all addresses"))
        except OSError as e:
            if e.errno == _errno.EADDRNOTAVAIL:
                problems.append(("web.host", f"{h} isn't an address on this machine — use "
                                             f"0.0.0.0 to listen on all"))
    port = new_web.get("port")
    if (type(port) is int and 0 < port < 65536 and not problems
            and port != old_web.get("port") and port != _SERVING_WEB[1]):
        h = (host.strip() if isinstance(host, str) and host.strip() else "0.0.0.0").strip("[]")
        try:
            s = _socket.socket(_socket.AF_INET6 if ":" in h else _socket.AF_INET,
                               _socket.SOCK_STREAM)
            try:
                # As the restart will: SO_REUSEADDR, so only a socket that is
                # really in use (not a closed one in TIME_WAIT) counts.
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                s.bind((h, port))
            finally:
                s.close()
        except OSError as e:
            if e.errno == _errno.EADDRINUSE:
                problems.append(("web.port", f"Port {port} is already used by another "
                                             f"program on this machine — pick another port"))
            elif e.errno in (_errno.EACCES, _errno.EPERM):
                problems.append(("web.port", f"Port {port} needs administrator rights on "
                                             f"this machine — use a port above 1023"))
    new_db = (new_cfg.get("data") or {}).get("db_file")
    old_db = (old_cfg.get("data") or {}).get("db_file")
    if isinstance(new_db, str) and new_db.strip() and new_db != old_db:
        p = Path(new_db.strip())
        if not p.is_absolute():
            p = _install_dir() / p
        folder = p.parent
        if not folder.is_dir():
            problems.append(("data.db_file", f"The folder {folder} doesn't exist on this machine"))
        elif not os.access(folder, os.W_OK | os.X_OK):
            problems.append(("data.db_file", f"Aerodrome can't write to the folder {folder}"))
    return problems


# --- One maintenance operation at a time (v3.4.147) ---
# Updates, restores, server-side backups, config import/restore, the
# pre-restore purge, server-side backup delete and switch-to-real all
# rewrite the install or the database. Two at once (an upload during an
# apply, two server-side backups in the same second) left half-applied trees
# and corrupt zips. One lock covers them all:
#   - try-only: it never waits, so it is safe to take on the event loop;
#   - an operation that requested a restart PARKS its hold for a while, so
#     nothing else starts in the seconds before the restart lands;
#   - expiry uses time.monotonic() (a clock change can't free or wedge it);
#     an active hold expires after 6 h as a backstop against a leaked token.
# Lock order: _MAINT is never waited on. Other locks (e.g. a config lock)
# may be taken while it is held, never the other way round.
class _SingleRun:
    BACKSTOP_SEC = 6 * 3600

    def __init__(self):
        import itertools
        import threading
        self._mu = threading.Lock()     # held for microseconds only
        self._h = None                  # {op, since_wall, until_mono, tok, parked}
        self._seq = itertools.count(1)

    def try_acquire(self, op: str):
        """A token when the lock was free (or its hold had expired), else None."""
        now = time.monotonic()
        with self._mu:
            h = self._h
            if h is not None and now < h["until_mono"]:
                return None
            if h is not None:
                logger.warning(f"Maintenance lock: taking over an expired hold "
                               f"({h['op']}, since {time.ctime(h['since_wall'])})")
            tok = next(self._seq)
            self._h = {"op": op, "since_wall": time.time(),
                       "until_mono": now + self.BACKSTOP_SEC, "tok": tok, "parked": False}
            return tok

    def release(self, tok) -> None:
        """Free the lock. A stale or parked token does nothing."""
        with self._mu:
            h = self._h
            if h is not None and h["tok"] == tok and not h["parked"]:
                self._h = None

    def park(self, tok, op: str, seconds: float) -> bool:
        """Keep the hold for `seconds` after the operation returns (a restart
        was requested). release() no longer frees it; expiry does."""
        with self._mu:
            h = self._h
            if h is None or h["tok"] != tok:
                return False
            h.update(op=op, parked=True, until_mono=time.monotonic() + seconds)
            return True

    def unpark(self, tok) -> bool:
        """Free a parked hold early: the restart it waited for was refused
        (v3.4.149: a restore's restart runs after its response is sent)."""
        with self._mu:
            h = self._h
            if h is None or h["tok"] != tok or not h["parked"]:
                return False
            self._h = None
            return True

    def holder(self):
        """{op, since, age_sec, parked} of the active hold, or None."""
        now = time.monotonic()
        with self._mu:
            h = self._h
            if h is None or now >= h["until_mono"]:
                return None
            return {"op": h["op"], "since": h["since_wall"],
                    "age_sec": max(0, int(time.time() - h["since_wall"])),
                    "parked": h["parked"]}


# Module level so other request paths can read it. get_app() starts each app
# with a fresh one (one app per process in production).
_MAINT = _SingleRun()

# What ends the newest app's background schedulers (the daily summary and
# the update check). get_app() sets the one before it, so the schedulers of
# an app a newer one replaced (a test run builds many in one process;
# production builds one) end instead of waking to act on the newer app's
# settings and database.
_SCHEDULERS_STOP: Optional[threading.Event] = None

# How long an operation that requested a restart keeps the lock.
_RESTART_HOLD_SEC = 120

# An update zip's total UNPACKED size is checked before anything is
# decompressed or update/ is wiped. A release unpacks to about 12 MB.
UPDATE_UNPACKED_MAX_BYTES = 200 * 1024 * 1024
UPDATE_UNPACK_HEADROOM_BYTES = 256 * 1024 * 1024

# How long to wait before retrying a FAILED update check. The poll interval is
# measured from the last successful check, so a check that keeps failing would
# otherwise fire on every scheduler wake (every minute). One hour because
# GitHub's anonymous rate limit resets within the hour.
UPDATE_CHECK_RETRY_SEC = 3600


def _update_check_due_in(interval_s: int, now: int, state: dict) -> int:
    """Seconds until the next scheduled update check is due; 0 means now.
    `state` is an update-state dict (see _get_update_state). The interval runs
    from the last SUCCESSFUL check, which a failing check never moves, so the
    failure back-off below is what stops a broken check from firing on every
    scheduler wake. A later success replaces the back-off (it rewrites
    last_check_result)."""
    last_success = state.get("last_known_latest_ts")
    due = 0 if last_success is None else last_success + interval_s
    last_attempt = state.get("last_check_ts")
    if state.get("last_check_result") == "error" and last_attempt:
        due = max(due, last_attempt + UPDATE_CHECK_RETRY_SEC)
    return max(0, due - now)


def _unpacked_size_refusal(zf, install_dir: Path):
    """(status, code, message) when an update zip can't be a release (400),
    is too big to unpack (413) or wouldn't fit on the disk (507); None when
    it's fine. Uses the sizes the zip declares, so nothing is decompressed
    to find out. Those sizes only bound what is decompressed for stored and
    deflated entries (zipfile limits each deflate call to the declared
    size); bzip2, LZMA and zstd entries decompress without a limit, so a
    tiny zip could expand to gigabytes. Releases are built with deflate:
    any other method, or encryption, is refused before anything is read."""
    import zipfile as _zipmod
    mb = 1024 * 1024
    for i in zf.infolist():
        if i.compress_type not in (_zipmod.ZIP_STORED, _zipmod.ZIP_DEFLATED) \
                or i.flag_bits & 0x1:
            return (400, "not_a_release",
                    "This zip uses a compression method (or encryption) that Aerodrome "
                    "releases never use, so it wasn't unpacked. Nothing was changed. "
                    "Did you pick the right file?")
    total = sum(i.file_size for i in zf.infolist())
    if total > UPDATE_UNPACKED_MAX_BYTES:
        return (413, "too_large",
                f"This zip unpacks to {total // mb} MB — far more than an Aerodrome "
                f"release (about 12 MB; the limit is {UPDATE_UNPACKED_MAX_BYTES // mb} MB). "
                f"Nothing was changed. Did you pick the right file?")
    try:
        free = shutil.disk_usage(str(install_dir)).free
    except OSError:
        return None
    if total > free - UPDATE_UNPACK_HEADROOM_BYTES:
        return (507, "no_space",
                f"Not enough free disk space to unpack this update: it needs about "
                f"{(total + UPDATE_UNPACK_HEADROOM_BYTES) // mb} MB, {free // mb} MB "
                f"available. Nothing was changed.")
    return None


class _Busy(Exception):
    """Another maintenance operation holds the lock (or an update is being
    confirmed). Carries the holder for the 409 body."""

    def __init__(self, busy: Optional[dict], code: str = "busy", message: str = ""):
        self.busy = busy or {}
        self.code = code
        super().__init__(message or _busy_message(self.busy))


def _ago(sec) -> str:
    sec = int(sec or 0)
    if sec < 60:
        return "just now"
    if sec < 3600:
        return f"{sec // 60} min ago"
    return f"{sec // 3600} h {sec % 3600 // 60} min ago"


def _busy_message(busy: dict) -> str:
    op = str(busy.get("op") or "another maintenance task")
    if busy.get("parked"):
        return f"Aerodrome is {op} — try again in a minute."
    return (f"{op[:1].upper()}{op[1:]} is running (started {_ago(busy.get('age_sec'))}). "
            f"Try again when it finishes.")


# v3.4.149: what settings writes and maintenance answer while a restore waits
# for its restart (the restart would otherwise put the backup's settings over
# a change made meanwhile, without a word).
RESTORE_WAITING_MESSAGE = ("A restore is waiting for the restart — restart Aerodrome to "
                           "finish it, or cancel it in Settings.")


def _restore_waiting():
    """The marker of a restore waiting for its restart, else None (one that
    waited too long no longer counts). Never drops anything: an expired
    restore is dropped only with the maintenance lock held (_maintenance,
    the status poll) — a caller without it could otherwise drop a NEW
    restore staged meanwhile under the same file names. Never raises."""
    try:
        return restore_io.pending(_install_dir())
    except Exception as e:
        logger.warning(f"Could not read the restore marker: {e}")
        return None


def _drop_expired_restore():
    """Drop a staged restore past its hour. Only with the maintenance lock
    held (or from startup). Never raises."""
    try:
        restore_io.discard_if_expired(_install_dir(), log=logger.warning)
    except Exception as e:
        logger.warning(f"Could not drop an expired restore: {e}")


def _restore_waiting_busy() -> "_Busy":
    return _Busy({"op": "a restore waiting for the restart", "since": None, "age_sec": 0,
                  "parked": True}, code="restore_pending", message=RESTORE_WAITING_MESSAGE)


def _restore_waiting_response(field_errors: bool = False) -> Optional[JSONResponse]:
    """409 for a settings write while a restore waits for its restart.
    field_errors: also as Settings' path-less save error
    (errors: [{path: "", message}]), the shape its save handler reads."""
    if _restore_waiting() is None:
        return None
    extra = {"errors": [{"path": "", "message": RESTORE_WAITING_MESSAGE}]} if field_errors else {}
    return _busy_response(_restore_waiting_busy(), **extra)


@contextmanager
def _maintenance(op: str, update_op: bool = False, restore_op: bool = False,
                 allow_restore_pending: bool = False):
    """Hold the maintenance lock for one operation, or raise _Busy.

    update_op: an update upload/apply/download is also refused while the
    previous update is still being confirmed (its journal exists). Other
    operations are not refused. A rollback is still possible until the new
    version commits — normally 30 s after its first good answer, at most
    10 min if the commit is stuck — and it puts config.yaml back to the
    pre-update copy, so a settings change or config import made in that
    window would be undone if the new version then crashed (database
    changes are kept). A restart the new version's own UI asks for in that
    window commits first (_restart_service), so it can't cause one.

    restore_op (v3.4.149): a restore is refused in that window too — the
    two would meet at the same start (the update goes first; a rolled-back
    update must find the data it knows).

    Every operation is refused while a restore waits for its restart
    (it would land under, or be undone by, the swap) — except the one that
    is the way out: allow_restore_pending (cancelling it)."""
    if update_op or restore_op:
        j = updater.pending(_install_dir())
        if j:
            if j.get("state") in ("swapping", "rolling_back"):
                msg = ("The last update didn't finish switching files; Aerodrome puts "
                       "the previous version back when it next starts. Restart Aerodrome, "
                       "then try again.")
            else:
                msg = "The last update is still being confirmed — try again in a minute."
            raise _Busy({"op": "confirming the last update", "since": None, "age_sec": 0,
                         "parked": True}, code="update_pending", message=msg)
    tok = _MAINT.try_acquire(op)
    if tok is None:
        raise _Busy(_MAINT.holder() or {"op": "another maintenance task"},
                    code="update_in_progress" if update_op else "busy")
    try:
        _drop_expired_restore()
        if not allow_restore_pending and _restore_waiting() is not None:
            raise _restore_waiting_busy()
        yield tok
    finally:
        _MAINT.release(tok)


def _busy_response(b: _Busy, message: Optional[str] = None, **extra) -> JSONResponse:
    """409 with both `error` (updates.html) and `message` (config.html)."""
    msg = message or str(b)
    busy = {k: b.busy.get(k) for k in ("op", "since", "age_sec", "parked")}
    return JSONResponse(status_code=409, content={
        "ok": False, "code": b.code, "error": msg, "message": msg, "busy": busy, **extra})


def _confirm_update_before_restart():
    """v3.4.147: a restart asked for by the new version's own UI (settings,
    a config import, a restore, the switch to real data) after it has
    answered is planned, not a crash: the pending update is committed
    first, so the restart is never counted as a failed start and can't
    roll the release (and config.yaml) back (updater.commit_if_served).
    Every in-app restart of Aerodrome itself goes through here. Never
    raises: a restart is never blocked on this."""
    try:
        if updater.commit_if_served(_install_dir()):
            logger.info("Update confirmed before this restart (the new version had "
                        "already answered)")
    except Exception as e:  # never block a restart on this
        logger.warning(f"Could not confirm the pending update before restarting: {e}")


def _restart_service():
    """Trigger a systemd restart. Returns (ok, note).

    Tries `systemctl restart --no-block aerodrome` first (preferred — returns
    immediately, doesn't race with our own shutdown). Falls back to the plain
    `systemctl restart aerodrome` if sudo rejects the --no-block variant,
    which happens on installs whose sudoers rule predates this feature.

    A non-zero return with empty stderr is treated as success — that's the
    expected case when systemd kills us before the subprocess finishes.

    v3.4.147: a pending update the new version has answered for is
    committed first (_confirm_update_before_restart)."""
    import subprocess
    _confirm_update_before_restart()
    cmds = [
        ["systemctl", "restart", "--no-block", "aerodrome"],
        ["systemctl", "restart", "aerodrome"],
    ]
    last_note = ""
    for cmd in cmds:
        try:
            result = privhelper.run_sudo(cmd, timeout=10)
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
            return False, "sudo or systemctl not found"
        except Exception as e:
            last_note = str(e)
            continue
    return False, last_note


def _pre_restore_db_name() -> str:
    """Resolve the live DB file's basename for glob matching. Falls back
    to the default if CONFIG isn't loaded yet (e.g. early in startup)."""
    db_file = (CONFIG.get("data") or {}).get("db_file", "aircraft_history.db")
    return Path(db_file).name


def _pre_restore_db_configured() -> Path:
    """The live database as configured (data.db_file; a relative name is in
    the install), not resolved."""
    db = Path((CONFIG.get("data") or {}).get("db_file", "aircraft_history.db"))
    return db if db.is_absolute() else _pre_restore_install_dir() / db


def _pre_restore_db_path() -> Path:
    """The live database, resolved: with data.db_file a symlink to another
    disk, its copies are made beside the real file (v3.4.149)."""
    return Path(os.path.realpath(str(_pre_restore_db_configured())))


def _pre_restore_db_copy(ts: str) -> Path:
    """The database copy from restore `ts`: beside the real database, or —
    made by a release before 3.4.149 with data.db_file a symlink — beside
    the link. (The first that exists; else the first.)"""
    real = restore_io.db_snapshot_path(_pre_restore_db_path(), ts)
    if not real.is_file():
        old = restore_io.db_snapshot_path(_pre_restore_db_configured(), ts)
        if old.is_file():
            return old
    return real


def _pre_restore_sources() -> List[tuple]:
    """(directory, glob, kind) of each kind of pre-restore copy. v3.4.149:
    each copy lives beside the file it copies, on that file's own
    filesystem — the database's beside the database (data.db_file may be
    outside the install: those copies used to pile up unlisted and
    unpruned), config.yaml's beside config.yaml. With data.db_file a
    symlink, the copies releases before 3.4.149 made beside the LINK are
    looked for there too (listed, pruned, purged and put back like any)."""
    db = _pre_restore_db_path()
    out = [(db.parent, f"{db.name}.bak.*.pre-restore", "database")]
    link = _pre_restore_db_configured()
    if (os.path.realpath(str(link.parent)), link.name) != (str(db.parent), db.name):
        out.append((link.parent, f"{link.name}.bak.*.pre-restore", "database"))
    cfg_dir = Path(CONFIG_PATH).parent if CONFIG_PATH else _pre_restore_install_dir()
    out.append((cfg_dir, "config.yaml.bak.*.pre-restore", "config"))
    return out


def _pre_restore_globs() -> List[str]:
    """Glob patterns covering both flavors of pre-restore snapshot."""
    return [pat for _d, pat, _k in _pre_restore_sources()]


def _pre_restore_files():
    """[(path, kind, ts, stat)] of every pre-restore copy, each looked for
    in its own directory."""
    out = []
    seen = set()
    for d, pat, kind in _pre_restore_sources():
        try:
            found = list(d.glob(pat))
        except OSError:
            continue
        for p in found:
            key = os.path.realpath(str(p))
            if key in seen or not p.is_file():
                continue
            seen.add(key)
            m = _PRE_RESTORE_TS_RE.search(p.name)
            if not m:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((p, kind, m.group(1), st))
    return out


def _pre_restore_protected() -> set:
    """The real paths of the files a restore marker names (never pruned,
    purged or put back while it is there)."""
    return {os.path.realpath(p)
            for p in restore_io.protected_paths(_pre_restore_install_dir())}


def _list_pre_restore_snapshots() -> List[Dict[str, Any]]:
    """Return pre-restore snapshots paired by timestamp, newest first.
    Each entry: {timestamp, files: [{name, kind, size_bytes, mtime}, ...],
    total_bytes, mtime, in_use}. v3.4.149: `in_use` — a restore waiting for
    its restart (or being switched in) puts this pair back if it fails, so
    it can't be put back, purged or pruned now."""
    protected = _pre_restore_protected()
    by_ts: Dict[str, Dict[str, Any]] = {}
    for p, kind, ts, st in _pre_restore_files():
        entry = by_ts.setdefault(ts, {
            "timestamp": ts,
            "files": [],
            "total_bytes": 0,
            "mtime": 0,
            "in_use": False,
        })
        entry["files"].append({
            "name": p.name,
            "kind": kind,
            "size_bytes": st.st_size,
            "mtime": int(st.st_mtime),
        })
        entry["total_bytes"] += st.st_size
        entry["mtime"] = max(entry["mtime"], int(st.st_mtime))
        if os.path.realpath(str(p)) in protected:
            entry["in_use"] = True

    snapshots = list(by_ts.values())
    # Newest first. Timestamp strings are YYYYMMDD-HHMMSS so lexical sort
    # is chronological.
    snapshots.sort(key=lambda s: s["timestamp"], reverse=True)
    return snapshots


def _prune_pre_restore_snapshots(keep: int = _PRE_RESTORE_KEEP):
    """Delete pre-restore files beyond the most-recent `keep` per kind.
    The two kinds (DB and config) are pruned independently, so a
    missing or mismatched pair (e.g. a config that was written when the
    DB write failed, or vice versa) doesn't block trimming the other.
    v3.4.149: each kind in its own directory, and never a file the restore
    marker names (the copy a pending restore would put back) — not even
    when keep is 0 (the purge).

    Returns (deleted_count, freed_bytes). Errors on individual files are
    logged but never raised — pruning is best-effort housekeeping."""
    protected = _pre_restore_protected()
    deleted_count = 0
    freed_bytes = 0
    by_kind: Dict[str, list] = {}
    for p, kind, ts, st in _pre_restore_files():
        if os.path.realpath(str(p)) in protected:
            continue
        by_kind.setdefault(kind, []).append((ts, p, st.st_size))
    for files in by_kind.values():
        # Newest first by timestamp (YYYYMMDD-HHMMSS sorts chronologically;
        # the names of one kind can differ: beside a symlinked database and
        # beside its target)
        files.sort(key=lambda t: (t[0], t[1].name), reverse=True)
        for _ts, p, size in files[keep:]:
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
    The copies a pending restore names are kept (see the prune).

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


# --- Temporary files in .backups/.tmp (v3.4.148) ---
# Upload spools, the browser backup download, the database snapshot inside
# a backup and a server-side backup's unfinished zip all live in
# <install>/.backups/.tmp (0700): on the install's own disk (not a RAM-backed
# /tmp), private to the account Aerodrome runs as, and emptied at startup,
# so a crash, a kill or an abandoned download never leaves a database-sized
# file behind for good.
_BACKUP_TMP_NAME = ".tmp"
# NamedTemporaryFile leftovers earlier versions could leave in .backups/
# (the export's zip and the snapshot it took of the database).
_LEGACY_TMP_RE = re.compile(r"^tmp[a-z0-9_]{8}\.(zip|db)(-journal|-wal|-shm)?$")
# atomic_io's temp files (".<name>.<8 random chars>.tmp", beside the file)
# for a write cut short by a kill: config.yaml and its automatic copies at
# the top of the install, ...
_CONFIG_STAGE_RE = re.compile(
    r"^\.config\.yaml(\.bak\.[0-9]{8}-[0-9]{6}(\.pre-restore)?)?\.[a-z0-9_]{8}\.tmp\Z")
# ... and last-good, the recovery notice, a broken copy, a backup's sha256
# sidecar, and (v3.4.149) the restore marker and result in .backups/. (The
# files a restore stages — beside the database and config.yaml — and the
# marker itself are never swept here: restore_io.sweep_orphans removes
# staged leftovers, and only when no marker names them.)
_BACKUPS_STAGE_RE = re.compile(
    r"^\.(config\.yaml\.last-good|config-recovery\.json"
    r"|config\.yaml\.broken\.[0-9]{8}-[0-9]{6}(-[0-9]+)?"
    r"|pending-restore\.json|restore-result\.json"
    r"|aerodrome-backup-[0-9]{8}-[0-9]{6}(-[0-9]+)?\.zip\.sha256)\.[a-z0-9_]{8}\.tmp\Z")


def _backup_tmp_dir(install_dir: Optional[Path] = None) -> Path:
    """<install>/.backups/.tmp, created 0700 if missing (never system temp).
    A .tmp that is a symlink is refused (OSError): spools and backups are
    never written into a folder somewhere else."""
    inst = Path(install_dir) if install_dir is not None else _install_dir()
    atomic_io.ensure_private_dir(inst / ".backups")
    d = atomic_io.ensure_private_dir(inst / ".backups" / _BACKUP_TMP_NAME)
    if os.path.islink(d):
        import errno as _errno
        raise OSError(_errno.ELOOP, f"{d} is a symbolic link")
    return d


def _sweep_backup_temp_files(install_dir) -> tuple:
    """Delete leftover temporary files (startup only — nothing else of this
    install runs then). Returns (count, bytes freed).

    Removes: everything in .backups/.tmp/; legacy .backups/.uploads/
    upload-*.zip; NamedTemporaryFile leftovers and atomic-write temp files
    (last-good, notice, broken copy, sha256 sidecar) at the top of
    .backups/; the temp file of a config.yaml or config.yaml.bak.* write
    next to config.yaml. Never touches backup zips, update snapshots
    (.backups/<date>/), last-good or broken config copies, or markers, and
    never looks outside the install (a symlinked config.yaml's folder)."""
    inst = Path(install_dir)
    bdir = inst / ".backups"
    victims = []
    tmp = bdir / _BACKUP_TMP_NAME
    if tmp.is_dir() and not tmp.is_symlink():
        victims += list(tmp.iterdir())
    up = bdir / ".uploads"
    if up.is_dir() and not up.is_symlink():
        victims += [p for p in up.glob("upload-*.zip")]
    if bdir.is_dir():
        victims += [p for p in bdir.iterdir()
                    if _LEGACY_TMP_RE.match(p.name) or _BACKUPS_STAGE_RE.match(p.name)]
    try:
        victims += [p for p in inst.iterdir() if _CONFIG_STAGE_RE.match(p.name)]
    except OSError:
        pass
    count = freed = 0
    for p in victims:
        try:
            if p.is_dir() and not p.is_symlink():
                size = sum(q.lstat().st_size for q in p.rglob("*") if q.is_file())
                shutil.rmtree(p)
            else:
                size = p.lstat().st_size
                p.unlink()
            count += 1
            freed += size
        except OSError as e:
            logger.warning(f"Startup cleanup: could not remove {p}: {e}")
    if count:
        logger.info(f"Startup cleanup: removed {count} leftover temporary file(s) "
                    f"from earlier runs ({_human_bytes(freed)} freed)")
    return count, freed


def _human_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _db_size_bytes(install_dir) -> int:
    """The live database's size on disk (with its -wal), 0 when missing."""
    db = Path((CONFIG.get("data") or {}).get("db_file", "aircraft_history.db"))
    if not db.is_absolute():
        db = Path(install_dir) / db
    total = 0
    for sfx in ("", "-wal"):
        try:
            total += os.stat(str(db) + sfx).st_size
        except OSError:
            pass
    return total


def _no_space_response(need: int, free: int, what: str) -> JSONResponse:
    mb = 1024 * 1024
    msg = (f"Not enough free disk space to {what}: it needs about {need // mb} MB, "
           f"{free // mb} MB available. Nothing was changed.")
    return JSONResponse(status_code=507, content={
        "ok": False, "code": "no_space", "error": msg, "message": msg})


def _upload_disk_filled_response() -> JSONResponse:
    """507 for a restore upload the disk filled up under (while it was
    received or staged)."""
    msg = ("Not enough free disk space for this upload: the disk filled up "
           "while it was being stored. Nothing was changed.")
    return JSONResponse(status_code=507, content={
        "ok": False, "code": "no_space", "error": msg, "message": msg})


def _cross_site_refusal(request: Request) -> Optional[JSONResponse]:
    """v3.4.148: the backup download is a GET (so a typed URL and scripts
    keep working), which the same-origin guard doesn't cover. Refuse it
    (403) when the browser says another website's page asked for it:
    Sec-Fetch-Site other than same-origin / none, or an Origin / Referer
    for a different host:port. Clients that send none of these (curl) and
    the page itself are unaffected."""
    site = (request.headers.get("sec-fetch-site") or "").lower()
    bad = bool(site) and site not in ("same-origin", "none")
    for h in ("origin", "referer"):
        v = request.headers.get(h)
        if v and v != "null" and urlsplit(v).netloc != request.url.netloc:
            bad = True
        elif v == "null":
            bad = True
    if not bad:
        return None
    msg = ("Requests that come from other websites are refused. Download the "
           "backup from Aerodrome's own Settings page.")
    return JSONResponse(status_code=403, content={
        "ok": False, "code": "cross_site", "error": msg, "message": msg})


def _open_file_response(fobj, size: int, filename: str):
    """A response that streams an already-open (and already-unlinked) file
    and ALWAYS closes it when the response ends — sent, client gone, or
    failed — so its disk space is freed deterministically, not whenever a
    generator happens to be collected. Reads run in the threadpool. (The
    class is built here, not at import: release checks import this module
    with the web framework stubbed out.)"""
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import iterate_in_threadpool

    class _OpenFileResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                fobj.close()

    def chunks():
        while True:
            c = fobj.read(1024 * 1024)
            if not c:
                return
            yield c
    return _OpenFileResponse(
        iterate_in_threadpool(chunks()), media_type="application/zip",
        headers={"Content-Length": str(size),
                 "Content-Disposition": f'attachment; filename="{filename}"'})


def prepare_backup_tmp(install_dir) -> Optional[Path]:
    """main.start: create .backups/.tmp (0700), sweep leftovers, and check a
    file can be created there. Returns the directory, or None when it can't
    be used (the caller keeps the system temp dir then)."""
    try:
        d = _backup_tmp_dir(install_dir)
        os.chmod(d, atomic_io.PRIVATE_DIR_MODE)
        _sweep_backup_temp_files(install_dir)
        import tempfile
        fd, probe = tempfile.mkstemp(dir=str(d), prefix="probe-")
        os.close(fd)
        os.unlink(probe)
        return d
    except OSError as e:
        logger.warning(f"Can't use {Path(install_dir) / '.backups' / _BACKUP_TMP_NAME} "
                       f"for temporary files: {e}")
        return None


# Written in .backups/ once the step below has run, so it runs only once.
PRIVATE_MODES_MARKER = ".private-modes-applied"


def tighten_existing_private_files(install_dir, force: bool = False) -> list:
    """DECISION (v3.4.148, the maintainer's call — main calls this at a
    start with no update or restore pending, and once an update is
    confirmed; main.TIGHTEN_EXISTING_PRIVATE_FILES = False switches it off):
    earlier versions left .backups/ 0755 and the backup zips, config.yaml
    and the copies of it kept next to it 0644, readable by every local
    account (and install.sh still creates .backups/ with `mkdir -p` and
    copies config.yaml 0644). Aerodrome now creates its own files private;
    this makes the existing ones private too — ONCE (recorded in
    .backups/.private-modes-applied), so a mode an admin opens up later on
    purpose is kept. Pass force=True to run it again.

    Changed where group- or world-readable: .backups/ → 0700;
    .backups/aerodrome-backup-*.zip → 0600; config.yaml (through a symlink:
    its target), config.yaml.bak.* (including .pre-restore copies) and
    ntfy-server.yml.from-backup.* in the install folder → 0600. Files of
    another account are skipped (they can't be changed; DEBUG line); no
    other symlink is followed — a .backups/ that links to another folder is
    left as it is, zips included. The database files are left alone (other
    local tools may read the database). One INFO line per path changed.
    Never raises. Returns the paths changed."""
    import stat as _stat
    inst = Path(install_dir)
    bdir = inst / ".backups"
    marker = bdir / PRIVATE_MODES_MARKER
    if not force and os.path.lexists(marker):
        return []
    todo = []
    try:
        if bdir.is_dir() and not bdir.is_symlink():
            todo.append((bdir, atomic_io.PRIVATE_DIR_MODE, True))
            todo += [(p, atomic_io.PRIVATE_FILE_MODE, False)
                     for p in sorted(bdir.glob("aerodrome-backup-*.zip"))]
        cfg = inst / "config.yaml"
        if os.path.lexists(cfg):
            todo.append((Path(os.path.realpath(cfg)), atomic_io.PRIVATE_FILE_MODE, False))
        todo += [(p, atomic_io.PRIVATE_FILE_MODE, False)
                 for pat in ("config.yaml.bak.*", "ntfy-server.yml.from-backup.*")
                 for p in sorted(inst.glob(pat))]
    except OSError as e:
        logger.warning(f"Could not check file permissions in {inst}: {e}")
    me = os.geteuid()
    changed = []
    for p, mode, is_dir in todo:
        try:
            st = p.lstat()
            if (_stat.S_ISDIR(st.st_mode) != is_dir
                    or not (_stat.S_ISDIR(st.st_mode) or _stat.S_ISREG(st.st_mode))):
                continue
            old = _stat.S_IMODE(st.st_mode)
            if not old & 0o077:
                continue
            if me != 0 and st.st_uid != me:
                logger.debug(f"Left {p} as it is: it belongs to another account")
                continue
            os.chmod(p, mode)
            changed.append(p)
            logger.info(f"Made {p} readable by this account only "
                        f"(was {old:04o}, now {_stat.S_IMODE(p.stat().st_mode):04o})")
        except OSError as e:
            logger.warning(f"Could not make {p} private: {e}")
    try:
        atomic_io.ensure_private_dir(bdir)
        with atomic_io.open_private(marker) as f:
            f.write(b"Existing backups and config copies were made private once "
                    b"(v3.4.148). Delete this file to do it again at the next start.\n")
    except FileExistsError:
        pass
    except OSError as e:
        logger.debug(f"Could not record {marker}: {e} (the check runs again next start)")
    return changed


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
    r"^config\.yaml\.bak\.[0-9]{8}-[0-9]{6}\Z"     # v3.4.148: ASCII digits, no trailing newline
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
        # v3.4.140: non-digits are malformed (as the docstring says) — they
        # used to pass through as s[:4] and reach the UI as raw HTML.
        return s.zfill(4)[-4:] if s.isdigit() else ""
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
# Side-effects: writes seen_aircraft.last_distance where the stored value
# differs. Rows without coords keep last_distance = NULL (ORDER BY puts
# those last regardless of dir).
#
# The function is intentionally synchronous so callers know the column
# is consistent when it returns.
#
# Returns the count of rows updated for logging.
_DISTANCE_SAME_KM = 0.001   # the collector computes the same formula independently
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
            conn.execute("UPDATE seen_aircraft SET last_distance = NULL "
                         "WHERE last_distance IS NOT NULL")
            conn.commit()
            logger.info("Distance recompute: receiver location not set; "
                        "cleared all last_distance values")
            return 0

        # Pull every row with coordinates and recompute. Only what changed is
        # written: on an ordinary restart that's nothing, so a start no longer
        # rewrites every row (and its index entry) to the same value.
        rows = conn.execute("""
            SELECT icao, last_lat, last_lon, last_distance FROM seen_aircraft
            WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
        """).fetchall()

        updates = []
        for icao, lat, lon, stored in rows:
            km = _haversine(rlat, rlon, lat, lon)
            if stored is None or abs(km - stored) > _DISTANCE_SAME_KM:
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
            WHERE (last_lat IS NULL OR last_lon IS NULL) AND last_distance IS NOT NULL
        """)
        conn.commit()
        logger.info(f"Distance recompute: updated {len(updates)} of {len(rows)} rows "
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
    four entry kinds: icao (exact), tail (via the collector's shared
    tail map), callsign prefix, and model substring.

    An entry like `{tail: "N12345", label: "My plane"}` annotates
    is_watchlist on the corresponding ICAO row, exactly the same as if
    the user had specified `{icao: "A12345"}` directly. A tail not
    resolved yet doesn't match, and its chip on the Watchlist tab says why.

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
            # Uppercase tail → its ICAO hexes.
            tail = str(wl_entry["tail"]).strip().upper()
            if icao in watchlist_tail_map().get(tail, ()):
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
        # v3.4.6: query the military_hourly rollup when backfill is complete,
        # fall back to raw military_sightings while backfill is in progress.
        # Same dual-path pattern as the v2.50.0 sightings_hourly rollout.
        # The rollup's hour_bucket is unix-seconds-at-top-of-hour, so the
        # start_ts/end_ts comparison works identically. Bucket boundary
        # semantics shift slightly: a bucket survives the WHERE filter if
        # ANY of its sightings fall within the window. For the 24h+ windows
        # the Stats cards display, the boundary effect is at most one extra
        # hour at each end — well within tolerance for these aggregates.
        try:
            _mil_backfill = __import__("collector").get_military_backfill_status()
            _mil_use_rollup = (_mil_backfill.get("phase") == "complete")
        except Exception:
            _mil_use_rollup = False
        try:
            if _mil_use_rollup:
                r = c.execute(
                    "SELECT COUNT(DISTINCT icao) AS n FROM military_hourly "
                    "WHERE hour_bucket >= ? AND hour_bucket <= ?",
                    (start_ts, end_ts)
                ).fetchone()
            else:
                r = c.execute(
                    "SELECT COUNT(DISTINCT icao) AS n FROM military_sightings "
                    "WHERE seen_at >= ? AND seen_at <= ?",
                    (start_ts, end_ts)
                ).fetchone()
            result["military_count"] = r["n"] if r else 0

            # Breakdown — group by aircraft_type (fall back to special_label
            # if type is blank, since specials often have sparse type info).
            # Same source-table decision as the count above.
            if _mil_use_rollup:
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
                    FROM military_hourly
                    WHERE hour_bucket >= ? AND hour_bucket <= ?
                    GROUP BY label
                    ORDER BY n DESC
                    LIMIT 6
                """, (start_ts, end_ts)).fetchall()
            else:
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
            # military_sightings or military_hourly missing on very old DBs — not fatal
            pass

        # Watchlist
        # v3.4.6: same dual-path pattern as the military block above.
        # The three-way PK on watchlist_hourly (icao, hour_bucket,
        # watchlist_label) is what makes COUNT(DISTINCT watchlist_label)
        # work correctly on the rollup — see watchlist_hourly schema
        # comment in collector.py for the rationale.
        try:
            _watch_backfill = __import__("collector").get_watchlist_backfill_status()
            _watch_use_rollup = (_watch_backfill.get("phase") == "complete")
        except Exception:
            _watch_use_rollup = False
        try:
            if _watch_use_rollup:
                r = c.execute(
                    "SELECT COUNT(DISTINCT icao) AS n, "
                    "       COUNT(DISTINCT watchlist_label) AS rules "
                    "FROM watchlist_hourly "
                    "WHERE hour_bucket >= ? AND hour_bucket <= ?",
                    (start_ts, end_ts)
                ).fetchone()
            else:
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

        # Specials — distinct named special aircraft seen in the window.
        # v3.4.6: dual-path same as the military_count block. The rollup
        # already stores special_label per (icao, hour_bucket), so the
        # GROUP BY (icao, special_label) maps cleanly to either table.
        # Reuses _mil_use_rollup decision from the count block above.
        try:
            if _mil_use_rollup:
                rows = c.execute("""
                    SELECT icao, special_label,
                           MAX(callsign)       AS callsign,
                           MAX(last_seen_at)   AS last_seen_at
                    FROM military_hourly
                    WHERE hour_bucket >= ? AND hour_bucket <= ?
                      AND special_label IS NOT NULL AND special_label != ''
                    GROUP BY icao, special_label
                    ORDER BY last_seen_at DESC
                    LIMIT 5
                """, (start_ts, end_ts)).fetchall()
            else:
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


# v3.4.87: cap the in-memory request body (DoS guard). JSON/form bodies are
# buffered whole and parsed (~2x peak RAM), so an unbounded POST can exhaust
# memory on a small host (e.g. a 4 GB Pi) and crash-loop the service.
#
# v3.4.148: the cap applies to EVERY content type and is chosen per path.
# Before, anything labelled multipart/form-data skipped it: a JSON route then
# buffered the whole body in RAM, and the three upload routes only checked
# their own limits after Starlette had spooled the entire upload to disk.
# Upload routes get their endpoint limit plus multipart framing slack;
# everything else (including multipart sent to a JSON route) gets 2 MB.
# Small caps are read ahead and replayed; the two large upload routes are
# counted while they stream (never buffered) and answered 413 the moment
# the count passes the cap. Before a large upload's first byte is read it
# is refused (409) while a maintenance task runs or another large upload is
# in progress, and (507) when its declared size wouldn't fit on the disk (a
# restore upload needs about three times its size plus the database: the
# spool, the staged copy, and the room the restore itself checks for). A
# Host or Origin the same-origin guard refuses is handed straight to it
# (403, body unread) — no refusal here may tell another site's page what is
# running or how much disk is free. An upload that stops arriving for
# UPLOAD_STALL_TIMEOUT_SEC is ended (408) so it can't hold the one-upload
# gate and its spool until the service restarts.
_MAX_JSON_BODY_BYTES = 2 * 1024 * 1024  # 2 MB — config.yaml is documented < 256 KB
CONFIG_IMPORT_MAX_BYTES = 256 * 1024
UPDATE_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
BACKUP_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
_MULTIPART_SLACK_BYTES = 64 * 1024          # boundary + part headers
UPLOAD_FREE_HEADROOM_BYTES = 256 * 1024 * 1024
_SPACE_RECHECK_BYTES = 64 * 1024 * 1024     # chunked bodies: re-check free space this often
UPLOAD_STALL_TIMEOUT_SEC = 120              # no body bytes for this long → 408
_UPDATE_UPLOAD_PATH = "/api/updates/local/upload"
_BACKUP_UPLOAD_PATH = "/api/backup/import"
_UPLOAD_ROUTE_CAPS = {
    "/api/config/import": (
        CONFIG_IMPORT_MAX_BYTES,
        "This file is too large to be a config.yaml (the limit is 256 KB). "
        "Did you pick the right file?"),
    _UPDATE_UPLOAD_PATH: (
        UPDATE_UPLOAD_MAX_BYTES,
        "Upload is larger than 50 MB. Aerodrome release zips are a few MB; "
        "did you upload a full backup?"),
    _BACKUP_UPLOAD_PATH: (
        BACKUP_UPLOAD_MAX_BYTES,
        "Backup is too large for browser upload (over 2 GB). Copy it into "
        "<install_dir>/.backups/ with scp or rsync, then restore it from the "
        "Existing server-side backups list (click Rescan)."),
}
# One large upload at a time: two 2 GB restore uploads at once would spool
# 4 GB before either endpoint could refuse the second.
_LARGE_UPLOAD = threading.Lock()


class _RequestBodyTooLarge(OSError):
    """Raised into the app's receive() once an upload is refused mid-body.
    An OSError on purpose: Starlette's multipart parser closes (and so
    frees) every spooled upload file on OSError."""


def _refusal_body(code: str, message: str, **extra) -> bytes:
    import json as _json
    return _json.dumps({"ok": False, "code": code, "error": message,
                        "message": message, **extra}).encode()


def _spool_free_bytes() -> Optional[int]:
    import tempfile
    try:
        return shutil.disk_usage(tempfile.gettempdir()).free
    except OSError:
        return None


def _no_space_message(need: int, free: int) -> str:
    mb = 1024 * 1024
    return (f"Not enough free disk space for this upload: it needs about "
            f"{(need + UPLOAD_FREE_HEADROOM_BYTES) // mb} MB, {max(0, free) // mb} MB "
            f"available. Nothing was changed.")


def _upload_space_need(path: str, size: int) -> int:
    """Free space (before headroom) an upload of `size` bytes to `path`
    needs. A restore upload: its spool, the staged copy in .backups/.tmp
    and the room the restore checks for (its size again plus the
    database's). An update upload: its spool."""
    if path != _BACKUP_UPLOAD_PATH:
        return size
    try:
        db = _db_size_bytes(_install_dir())
    except Exception:
        db = 0
    return 3 * size + db


# The machine's own name (always an allowed Host) — set by get_app.
_OWN_HOSTNAME: Optional[str] = None


def _remember_this_server(config: dict) -> None:
    """get_app: this machine's name, and the web address this process was
    started to serve (CONFIG can change under it until the next restart)."""
    global _OWN_HOSTNAME, _SERVING_WEB
    import socket as _socket
    _OWN_HOSTNAME = _socket.gethostname()
    web = config.get("web") or {}
    _SERVING_WEB = (web.get("host"), web.get("port"))


def _own_hostname() -> str:
    global _OWN_HOSTNAME
    if _OWN_HOSTNAME is None:
        import socket as _socket
        _OWN_HOSTNAME = _socket.gethostname()
    return _OWN_HOSTNAME


def _origin_refusal(host_header: str, source: Optional[str], netloc: str,
                    unsafe: bool) -> Optional[str]:
    """The same-origin guard's rules (v3.4.82 / v3.4.140), in one place so
    the upload gate applies exactly the same ones: "host" when the request
    is addressed to a name Aerodrome doesn't answer to, "origin" when an
    unsafe request came from another site's page (Origin / Referer), else
    None."""
    if not _host_allowed(host_header, _configured_hosts(CONFIG), _own_hostname()):
        return "host"
    if unsafe and source and urlsplit(source).netloc != netloc:
        return "origin"
    return None


class _BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = _MAX_JSON_BODY_BYTES, route_caps=None):
        self.app = app
        self.max_bytes = max_bytes
        self.route_caps = _UPLOAD_ROUTE_CAPS if route_caps is None else route_caps

    @staticmethod
    async def _answer(send, status, body: bytes):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _reject(self, send, message="Request body too large."):
        await self._answer(send, 413, _refusal_body("too_large", message))

    def _cap_for(self, path, content_type):
        route = self.route_caps.get(path)
        if route and content_type.lower().startswith("multipart/form-data"):
            return route[0] + _MULTIPART_SLACK_BYTES, route[1]
        return self.max_bytes, "Request body too large."

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS", "TRACE"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        cap, message = self._cap_for(path, headers.get("content-type", ""))
        # Fast path: reject an oversized declared length before reading a
        # byte. Browsers always declare it for FormData uploads.
        clen = headers.get("content-length")
        declared = int(clen) if clen and clen.isdigit() else None
        if declared is not None and declared > cap:
            await self._reject(send, message)
            return
        if cap <= self.max_bytes:
            await self._buffered(scope, receive, send, cap, message)
            return
        # A request the same-origin guard refuses never reaches the gate:
        # the guard answers 403 without reading the body, and nothing about
        # what is running or the free disk space leaks to another site.
        from starlette.requests import Request as _Request
        req = _Request(scope)
        if _origin_refusal(req.headers.get("host", ""),
                           req.headers.get("origin") or req.headers.get("referer"),
                           req.url.netloc, True):
            await self.app(scope, receive, send)
            return
        # A large upload: refused before its first byte is read when it
        # couldn't be used anyway.
        refusal = self._large_upload_refusal(path, declared)
        if refusal is not None:
            await self._answer(send, *refusal)
            return
        if not _LARGE_UPLOAD.acquire(blocking=False):
            msg = ("Another upload is in progress. Wait for it to finish, "
                   "then try again.")
            await self._answer(send, 409, _refusal_body("upload_in_progress", msg))
            return
        try:
            await self._counted(scope, receive, send, cap, message, declared)
        finally:
            _LARGE_UPLOAD.release()

    @staticmethod
    def _large_upload_refusal(path, declared):
        """(status, body) when a large upload must be refused before it is
        read: a maintenance task is running (or, for an update upload, the
        last update is still being confirmed), or it won't fit on the disk."""
        holder = _MAINT.holder()
        if holder:
            b = _Busy(holder)
            return 409, _refusal_body(b.code, str(b), busy={
                k: holder.get(k) for k in ("op", "since", "age_sec", "parked")})
        # v3.4.149: a restore waits for its restart — the endpoint would
        # refuse anyway, after the whole upload was spooled.
        if _restore_waiting() is not None:
            b = _restore_waiting_busy()
            return 409, _refusal_body(b.code, str(b), busy={
                k: b.busy.get(k) for k in ("op", "since", "age_sec", "parked")})
        if path == _UPDATE_UPLOAD_PATH:
            try:
                pending = updater.pending(_install_dir())
            except Exception:
                pending = None
            if pending:
                msg = "The last update is still being confirmed — try again in a minute."
                return 409, _refusal_body("update_pending", msg)
        if declared is not None:
            free = _spool_free_bytes()
            need = _upload_space_need(path, declared)
            if free is not None and need > free - UPLOAD_FREE_HEADROOM_BYTES:
                return 507, _refusal_body("no_space", _no_space_message(need, free))
        return None

    async def _buffered(self, scope, receive, send, cap, message):
        # Read-ahead: buffer up to the cap (covers chunked / mis-declared
        # length), counting bytes; reject if exceeded, else replay to the app.
        buffered = []
        total = 0
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                buffered.append(msg)
                break
            total += len(msg.get("body", b""))
            if total > cap:
                await self._reject(send, message)
                return
            buffered.append(msg)
            if not msg.get("more_body", False):
                break

        async def replay():
            if buffered:
                return buffered.pop(0)
            return await receive()
        await self.app(scope, replay, send)

    async def _counted(self, scope, receive, send, cap, message, declared):
        # Pass chunks straight through (Starlette spools them) but count
        # them. Past the cap — or, for a body without a declared length,
        # when the disk is about to run out, or when the body stops arriving
        # — answer ourselves and raise into the app so the multipart parser
        # stops writing and frees its spool.
        path = scope.get("path", "")
        state = {"total": 0, "tripped": False, "started": False, "aborted": False,
                 "body_done": False, "next_check": _SPACE_RECHECK_BYTES}

        async def trip(status, body):
            state["tripped"] = True
            if not state["started"]:
                await self._answer(send, status, body)
            raise _RequestBodyTooLarge(message)

        async def counted_receive():
            if state["tripped"]:
                raise _RequestBodyTooLarge(message)
            if state["body_done"]:
                # The body is in; the app only waits for a disconnect now
                # (no deadline: a long restore may still be running).
                return await receive()
            try:
                # The deadline restarts with every chunk: a slow but live
                # upload is fine, one whose sender vanished (lid closed, Wi-Fi
                # gone — no FIN ever arrives) is ended.
                msg = await asyncio.wait_for(receive(), UPLOAD_STALL_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                await trip(408, _refusal_body(
                    "upload_stalled", "The upload stopped arriving, so it was cancelled. "
                    "Nothing was changed. Check the connection and try again."))
            if msg["type"] == "http.disconnect":
                state["aborted"] = True        # the user cancelled mid-upload
                state["body_done"] = True
            if msg["type"] == "http.request":
                state["total"] += len(msg.get("body", b""))
                if not msg.get("more_body", False):
                    state["body_done"] = True
                if state["total"] > cap:
                    await trip(413, _refusal_body("too_large", message))
                if declared is None and state["total"] >= state["next_check"]:
                    state["next_check"] = state["total"] + _SPACE_RECHECK_BYTES
                    free = _spool_free_bytes()
                    # What is still to come: the rest of a restore's room
                    # (its staged copy, the restore check) beyond the spool.
                    still = _upload_space_need(path, state["total"]) - state["total"]
                    if free is not None and free < still + UPLOAD_FREE_HEADROOM_BYTES:
                        await trip(507, _refusal_body("no_space", _no_space_message(
                            still, free)))
            return msg

        async def guarded_send(msg):
            if state["tripped"]:
                return  # we already answered
            if msg["type"] == "http.response.start":
                state["started"] = True
            await send(msg)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:      # incl. an ExceptionGroup from a TaskGroup (3.11+)
            if not state["tripped"]:
                raise
        finally:
            if state["tripped"] or state["aborted"]:
                # Starlette's parser only closes its spooled upload files on
                # its own errors. A cancelled upload never gets there, and our
                # OSError can arrive wrapped by the same-origin middleware's
                # task group — either way a spool of up to 2 GB would be left
                # as cyclic garbage until some later GC. Collect it now.
                import gc
                gc.collect()


# v3.4.140: DNS-rebinding guard. The same-origin check below compares Origin
# with the Host the request was addressed to — but a rebinding page (an
# attacker's domain re-pointed at this box's LAN IP) controls BOTH, so it
# passes, and gets the full same-origin API (config read, update upload +
# apply). The only defence without auth is refusing Host names we weren't
# told to answer to. Rebinding always arrives with a public DNS name, so IP
# literals, localhost, this machine's own name, and names that can't exist
# in public DNS (single-label, .local/.lan/.home.arpa/.internal) are always
# allowed; anything else must be listed in web.allowed_hosts (live key).
_PRIVATE_NAME_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal",
                          ".localdomain", ".localhost")


def _host_name(host_header: str) -> str:
    """Host header → bare lower-case name or IP (port + IPv6 brackets stripped)."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):                      # [v6] or [v6]:port
        return h[1:h.index("]")] if "]" in h else h[1:]
    if h.count(":") == 1:                      # name:port / v4:port
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def _host_allowed(host_header: str, allowed_hosts: str, own_hostname: str) -> bool:
    """True if a request addressed to `host_header` may be served."""
    import ipaddress
    name = _host_name(host_header)
    if not name:
        return True       # no Host header → not a browser; rebinding needs one
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or "." not in name or name.endswith(_PRIVATE_NAME_SUFFIXES):
        return True
    if name == (own_hostname or "").strip().lower().rstrip("."):
        return True
    extra = {p.strip().lower().rstrip(".") for p in str(allowed_hosts or "").split(",")}
    return name in extra


def _configured_hosts(cfg: dict) -> str:
    """web.allowed_hosts plus the host of notifications.public_url — that URL
    is already the operator's declared address for Aerodrome (ntfy tap
    actions open it), so it shouldn't need listing twice."""
    hosts = str((cfg.get("web") or {}).get("allowed_hosts") or "")
    pub = str((cfg.get("notifications") or {}).get("public_url") or "")
    if pub:
        try:
            hosts += "," + (urlsplit(pub).hostname or "")
        except ValueError:
            pass
    return hosts


def _host_rejected_response(request: Request) -> Response:
    """403 for a Host we don't answer to — an explanatory page for a browser
    navigation (with a link via the address the request actually reached),
    JSON for API calls. The Host value is attacker-controlled: escape it."""
    import html
    name = _host_name(request.headers.get("host", ""))
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse(status_code=403, content={
            "error": "host_not_allowed", "host": name,
            "message": f"'{name}' isn't in Aerodrome's allowed hostnames "
                       "(Configuration → Web server → Allowed hostnames)."})
    server = request.scope.get("server") or ("", None)
    addr, port = server[0], server[1]
    if addr and ":" in addr:
        addr = f"[{addr}]"
    link = (f'<a href="http://{html.escape(addr)}:{port}/config#web">'
            f'http://{html.escape(addr)}:{port}/config#web</a>'
            if addr and port and addr not in ("0.0.0.0", "[::]") else
            "this machine's IP address, then Configuration → Web server")
    page = f"""<!doctype html><meta charset="utf-8"><title>Aerodrome — hostname not allowed</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#1e293b}}
code{{background:#f1f5f9;padding:.1rem .35rem;border-radius:.25rem}}</style>
<h1>This address isn't on Aerodrome's allowed list</h1>
<p>Aerodrome was opened as <code>{html.escape(name)}</code>. To protect against
malicious web pages ("DNS rebinding"), it only answers to IP addresses, local
names like <code>raspberrypi.local</code>, and hostnames you've allowed.</p>
<p>To use this name, open {link}, add <code>{html.escape(name)}</code> under
<b>Allowed hostnames</b>, and save — it takes effect immediately.</p>"""
    return HTMLResponse(status_code=403, content=page)


def _sudoers_recovery_command(update_root: Path,
                              install_dir: Optional[Path] = None) -> str:
    """The command the "Sudoers update required" prompt shows for a STAGED
    release: its scripts/recover-sudoers.sh, told where the live install is.
    That script hands off to the install.sh carrying the newest permission
    rules (staged or installed) in --privileges-only mode, which refreshes
    the sudoers rule only. `--install-dir` instead of the older
    `sudo AERODROME_INSTALL_DIR=… bash …` form: no `sudo VAR=value` (sudo
    may refuse to pass it through). Paths are shell-quoted."""
    import shlex
    inst = install_dir or Path(__file__).resolve().parent
    script = update_root / "scripts" / "recover-sudoers.sh"
    return (f"sudo bash {shlex.quote(str(script))} "
            f"--install-dir {shlex.quote(str(inst))}")


def _live_recovery_command(install_dir: Optional[Path] = None) -> str:
    """The command that refreshes the permissions of the INSTALLED release
    (no update staged): its own scripts/recover-sudoers.sh. It also picks up a
    staged update's newer rules, if one is staged. Path is shell-quoted.
    (privhelper.live_recovery_command, for this file's install.)"""
    return privhelper.live_recovery_command(install_dir or Path(__file__).resolve().parent)


def get_app(config: dict, config_path: str) -> FastAPI:
    global CONFIG, CONFIG_PATH, _MAINT, _SCHEDULERS_STOP
    if _SCHEDULERS_STOP is not None:
        _SCHEDULERS_STOP.set()    # the replaced app's schedulers end at their wait
    _SCHEDULERS_STOP = schedulers_stop = threading.Event()
    CONFIG = config
    CONFIG_PATH = config_path
    _MAINT = _SingleRun()     # v3.4.147: one maintenance lock per app

    # Tail-only watchlist entries, so search and the row pills see them as
    # if the user had specified ICAO directly from the first request on.
    sync_watchlist_tails(config)

    app = FastAPI(title="Aerodrome")

    # v3.4.82: same-origin guard + baseline security headers.
    #
    # Aerodrome has no authentication by design (it trusts the local network —
    # see the threat model in the docs). That trust model is fine for who can
    # *reach* the app, but it leaves it open to CSRF: a malicious web page the
    # operator happens to visit can make their browser fire state-changing
    # requests at the app's guessable LAN address (e.g. http://<host>:8000),
    # with no token to stop it. The reachable-from-a-page endpoints include the
    # local-update apply path (which copies staged files over the live install
    # and restarts — i.e. code execution) and config/backup overwrite, so this
    # is the highest-impact gap even within the LAN-trust model.
    #
    # We can't require auth without breaking that model, so instead we reject
    # *cross-origin* unsafe requests. Browsers always attach an Origin (or, as a
    # fallback, Referer) header to cross-origin POST/PUT/DELETE/PATCH; we require
    # that header's host:port to match the host the request was addressed to.
    # Requests carrying NO Origin/Referer at all (curl, the updater calling
    # itself, health checks — i.e. non-browser clients) are allowed, consistent
    # with LAN-trust: the CSRF threat is specifically the victim's browser, and
    # browsers do send Origin on cross-origin mutations. Same-origin UI fetches
    # send a matching Origin, so the dashboard is unaffected.
    _UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
    _remember_this_server(config)

    @app.middleware("http")
    async def _same_origin_and_headers(request: Request, call_next):
        # v3.4.140: Host allowlist first, on EVERY method — a rebinding page
        # is same-origin, so it could otherwise read as well as write.
        # (_origin_refusal: the upload gate applies the same rules.)
        why = _origin_refusal(request.headers.get("host", ""),
                              request.headers.get("origin") or request.headers.get("referer"),
                              request.url.netloc, request.method in _UNSAFE_METHODS)
        if why == "host":
            return _host_rejected_response(request)
        if why == "origin":
            return JSONResponse(
                status_code=403,
                content={"error": "Cross-origin request rejected."},
            )
        response = await call_next(request)
        # Defense-in-depth headers on every response. No Aerodrome page is ever
        # framed (the admin UI is always top-level), so DENY is safe and blocks
        # cross-origin clickjacking; nosniff is a backstop for the XSS sinks.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    # v3.4.87: cap oversized request bodies (DoS guard). Added last so it wraps
    # outermost and rejects a too-large body before anything else reads it.
    app.add_middleware(_BodySizeLimitMiddleware)

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
        def _favicon_ico_route():
            return FileResponse(str(_favicon_ico), media_type="image/x-icon")
    if _favicon_svg.exists():
        @app.get("/favicon.svg", include_in_schema=False)
        def _favicon_svg_route():
            return FileResponse(str(_favicon_svg), media_type="image/svg+xml")
    if _apple_touch.exists():
        @app.get("/apple-touch-icon.png", include_in_schema=False)
        def _apple_touch_route():
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
        # v3.4.24: add-watchlist.js is the shared "Add to Watchlist" component
        # (used by index.html and aircraft.html). Cache-bust it like the other
        # shared static JS so a stale copy isn't served across an update.
        html = html.replace(
            'src="/static/add-watchlist.js"',
            f'src="/static/add-watchlist.js?v={_aerodrome_version}"'
        )
        # v3.5.x: aircraft type silhouettes data (shared Live + Board).
        html = html.replace(
            'src="/static/shapes-data.js"',
            f'src="/static/shapes-data.js?v={_aerodrome_version}"'
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
        # v3.4.27: actually inject the timefmt block into the served HTML.
        # This .replace() line was dropped during the v3.4.25 firstrun_block
        # edit below, leaving timefmt.js never loaded on any page — every
        # call to formatTime/formatTimeFull/formatDateTime threw a
        # ReferenceError, visibly breaking /status (Failed to load status:
        # formatTimeFull is not defined) and silently breaking row-time
        # formatting in templates/index.html, /aircraft, and the test-
        # notification result + recent-notifications log in /config.
        html = html.replace('</head>', timefmt_block + '</head>', 1)
        return HTMLResponse(content=html)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _serve_template("index.html")

    # --- Live (direct from receiver, no DB) ---
    @app.get("/api/live")
    def get_live():
        from collector import (is_military, clean_icao_hex, _to_number,
                               _sanitize_feed_record)
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
                # v3.4.141: the collector's feed-record hygiene — a
                # `flight: null` used to empty the whole live list, and
                # alt_baro "ground" reached the UI as "NaN ft".
                ac = _sanitize_feed_record(ac)
                lat = _to_number(ac.get("lat"))
                lon = _to_number(ac.get("lon"))
                # v3.4.84: validate the hex at this ingest point too (get_live
                # parses the feed independently of collector.normalize and
                # reflects `icao` straight to the UI). Drop contacts whose hex
                # isn't a real address — that's the stored/reflected-XSS guard.
                icao_up = clean_icao_hex(ac.get("hex"))
                if not icao_up:
                    continue

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
                    "speed": _to_number(ac.get("gs")),
                    "lat": lat,
                    "lon": lon,
                    "altitude": _to_number(ac.get("alt_baro") or ac.get("altitude") or ac.get("alt")),
                    # v3.4.138: feed `t`, else hexdb's type code — never
                    # the feed's `type` (that's the message source).
                    "aircraft_type": aircraft_type_for(ac, icao_up),
                    "type_desc": ac.get("desc") or ac.get("description") or "",
                    "seen_at": int(time.time()),
                    "distance": _distance_from_receiver(lat, lon),
                    "is_military": is_mil,
                    "mil_label": mil_label,
                    "mil_color": mil_color,
                    "squawk": _normalize_squawk_str(ac.get("squawk")),
                    # v3.5.0 (radar): directional + classification fields the
                    # live map needs at render time. `track` is true ground
                    # track in degrees (chevron rotation; falls back to
                    # true_heading when a feeder omits track on a poll).
                    # `vertical_rate` (ft/min) is barometric-preferred,
                    # geometric fallback — for the overlay telemetry and the
                    # eventual dead-reckoning glide. `category` is the ADS-B
                    # emitter category ("A1".."C7") that drives marker size
                    # (light/jet/heavy). All may be None — the frontend treats
                    # missing track as "no rotation" and missing category as
                    # the medium default.
                    "track": ac.get("track") if ac.get("track") is not None
                             else ac.get("true_heading"),
                    "vertical_rate": (ac.get("baro_rate") if ac.get("baro_rate") is not None
                                      else ac.get("geom_rate")),
                    "category": ac.get("category") or None,
                })
            # v3.5.0 (radar C-wl): annotate watchlist membership on each live
            # aircraft so the radar can show a WL pill (parallel to the MIL
            # pill). _annotate_watchlist is a pure in-memory match against
            # CONFIG['watchlist'] (no DB), so it's cheap per poll even at 100+
            # contacts. Adds is_watchlist + watchlist_label to each entry.
            for _e in result:
                _annotate_watchlist(_e)
            # v3.5.0 (radar): expose the configured receiver position so the
            # map can drop a receiver marker + range rings. Null lat/lon when
            # the receiver location isn't configured — the frontend then skips
            # the receiver marker/rings and just centers on the aircraft.
            _rx = CONFIG.get("receiver", {})
            return {
                "aircraft": result,
                "count": len(result),
                "last_updated": int(time.time()),
                "receiver": {
                    "lat": _rx.get("latitude"),
                    "lon": _rx.get("longitude"),
                    "distance_unit": (_rx.get("distance_unit") or "mi").lower(),
                },
            }
        except Exception as e:
            logger.error(f"Live fetch failed: {e}")
            _rx = CONFIG.get("receiver", {})
            return {"aircraft": [], "count": 0, "last_updated": int(time.time()),
                    "receiver": {"lat": _rx.get("latitude"), "lon": _rx.get("longitude"),
                                 "distance_unit": (_rx.get("distance_unit") or "mi").lower()},
                    "error": str(e)}

    # --- Military ---
    # v3.4.48: NON-async (plain def), same rationale as /api/watchlist below
    # — synchronous row-fetch-and-group that must not run on the event loop.
    # military_sightings is the same scale as watchlist_sightings; this
    # endpoint only escaped the freeze because its retention window currently
    # holds fewer rows. Running it in FastAPI's threadpool keeps it from
    # becoming the next loop-blocker as data grows.
    @app.get("/api/military")
    def get_military():
        db_path = CONFIG["data"]["db_file"]
        days = CONFIG["retention"]["military_days"]
        cutoff = int(time.time()) - (days * 86400)

        conn = _open_db_conn(db_path)
        conn.row_factory = sqlite3.Row
        # v3.4.52: was "fetch every sighting in the window, group in Python".
        # On the reference install that returned a ~600 MB JSON payload
        # (every one of ~1.9M rows serialized) and never completed — the
        # query, the serialization, the network transfer, and the browser
        # parse were all O(all sightings). The tab only needs one row per
        # aircraft (the latest) plus a count, so this now does that in SQL:
        # an aggregate pass for MAX(seen_at) + COUNT(*) per icao, then a
        # join back to fetch just the latest row's fields. Returns one row
        # per aircraft instead of every sighting — ~1.8s and a few hundred
        # KB on the reference data, versus 37-50s and 600 MB before.
        # Verified equivalent to the old grouping with EXPLAIN QUERY PLAN +
        # a row-count/latest-timestamp cross-check on synthetic data.
        rows = conn.execute("""
            WITH agg AS (
                SELECT icao, MAX(seen_at) AS max_seen, COUNT(*) AS cnt
                FROM military_sightings WHERE seen_at >= ?
                GROUP BY icao
            )
            SELECT ms.icao, ms.callsign, ms.speed, ms.lat, ms.lon, ms.altitude,
                   ms.aircraft_type, ms.type_desc, ms.seen_at, ms.special_label,
                   ms.squawk, agg.cnt AS sighting_count
            FROM agg
            JOIN military_sightings ms
              ON ms.icao = agg.icao AND ms.seen_at = agg.max_seen
            GROUP BY ms.icao
        """, (cutoff,)).fetchall()
        conn.close()

        from countries import country_for_icao
        aircraft = []
        for row in rows:
            entry = dict(row)
            sighting_count = int(entry.pop("sighting_count") or 0)
            entry["distance"] = _distance_from_receiver(entry.get("lat"), entry.get("lon"))
            _annotate_military(entry)
            # v2.83.3: derive country from the ICAO 24-bit address (pure
            # function of the hex, no DB/network). Only the latest row per
            # aircraft is annotated now, not every sighting.
            entry["country"] = country_for_icao(entry["icao"]) or ""
            entry["sighting_count"] = sighting_count
            # sightings kept as an empty list for backward compatibility with
            # any frontend code that reads g.sightings; the per-aircraft count
            # is carried by sighting_count (which the list/CSV/total now read).
            aircraft.append({"latest": entry, "sightings": [], "sighting_count": sighting_count})

        specials = CONFIG.get("military", {}).get("special_aircraft", {})
        return {
            "aircraft": aircraft,
            "special_aircraft": {k.upper(): v for k, v in specials.items()},
            "default_military_color": CONFIG.get("military", {}).get("default_color", "#ef4444"),
            "last_updated": int(time.time()),
            "retention_days": days,
        }

    # --- Watchlist sightings ---
    # v3.4.48: NON-async (plain def) on purpose. This handler does
    # synchronous SQLite work that, at scale (millions of watchlist_sightings
    # rows in the retention window), can take tens of seconds — it pulls the
    # window's rows and groups them in Python. As an `async def` it ran that
    # blocking work directly on the asyncio event loop, which single-handedly
    # froze EVERY other request (admin-page loads, /api/status health polls)
    # behind it — diagnosed from a HAR where /api/status calls whose own work
    # was 9 ms still showed 30-54 s wall time because they were queued behind
    # a 58-64 s /api/watchlist call holding the loop. FastAPI runs plain `def`
    # endpoints in its threadpool instead of on the loop, so the blocking
    # query no longer starves everything else. (The query itself is still
    # heavy — a follow-up will reduce it to latest-per-aircraft + a count via
    # GROUP BY, which the frontend already supports via ac.sighting_count —
    # but getting it off the loop is the fix for the reported symptom.)
    @app.get("/api/watchlist")
    def get_watchlist_sightings():
        db_path = CONFIG["data"]["db_file"]
        days = CONFIG["retention"]["watchlist_days"]
        cutoff = int(time.time()) - (days * 86400)

        conn = _open_db_conn(db_path)
        conn.row_factory = sqlite3.Row
        # v3.4.52: was "fetch every sighting in the window, group in Python",
        # which serialized a ~600 MB payload of all ~2.7M rows and never
        # completed. The tab needs one row per aircraft (latest) plus a
        # count, so this aggregates MAX(seen_at) + COUNT(*) per icao then
        # joins back for the latest row's fields. See the matching note in
        # /api/military above; same approach, verified the same way.
        rows = conn.execute("""
            WITH agg AS (
                SELECT icao, MAX(seen_at) AS max_seen, COUNT(*) AS cnt
                FROM watchlist_sightings WHERE seen_at >= ?
                GROUP BY icao
            )
            SELECT ws.icao, ws.callsign, ws.speed, ws.lat, ws.lon, ws.altitude,
                   ws.aircraft_type, ws.type_desc, ws.seen_at, ws.watchlist_label,
                   ws.squawk, agg.cnt AS sighting_count
            FROM agg
            JOIN watchlist_sightings ws
              ON ws.icao = agg.icao AND ws.seen_at = agg.max_seen
            GROUP BY ws.icao
        """, (cutoff,)).fetchall()
        conn.close()

        aircraft = []
        for row in rows:
            entry = dict(row)
            sighting_count = int(entry.pop("sighting_count") or 0)
            entry["distance"] = _distance_from_receiver(entry.get("lat"), entry.get("lon"))
            _annotate_military(entry)
            entry["sighting_count"] = sighting_count
            aircraft.append({"latest": entry, "sightings": [], "sighting_count": sighting_count})

        return {
            "aircraft": aircraft,
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
    def get_all_drill(
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
        # over idx_all_icao_seen, scanning the full table; the COUNT has the
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
        # v3.4.171: the FlightAware-confirmed route of each looked-up flight
        # (flight_routes), matched by callsign and time: one indexed query for
        # the page's callsigns, then each row picks the flight whose departure
        # to arrival (an hour either side) contains it.
        routes_by_cs: Dict[str, list] = {}
        callsigns = sorted({(r["callsign"] or "").strip().upper() for r in rows} - {""})
        if callsigns:
            try:
                marks = ",".join("?" * len(callsigns))
                for fr in conn.execute(
                        f"SELECT callsign, origin_icao, dest_icao, departed_at, arrives_at, looked_up_at "
                        f"FROM flight_routes WHERE callsign IN ({marks})", callsigns):
                    routes_by_cs.setdefault(fr["callsign"], []).append(fr)
            except sqlite3.OperationalError:
                routes_by_cs = {}
        conn.close()

        def _route_for(cs, ts):
            for fr in routes_by_cs.get((cs or "").strip().upper(), ()):
                lo = (fr["departed_at"] or fr["looked_up_at"] - 43200) - 3600
                hi = (fr["arrives_at"] or fr["looked_up_at"] + 43200) + 3600
                if lo <= ts <= hi:
                    return {"origin": fr["origin_icao"], "dest": fr["dest_icao"], "source": "flightaware"}
            return None

        sightings = []
        for row in rows:
            entry = dict(row)
            entry["distance"] = _distance_from_receiver(entry.get("lat"), entry.get("lon"))
            route = _route_for(entry.get("callsign"), entry.get("seen_at") or 0)
            if route:
                entry["route"] = route
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
    def get_search(q: str = "", limit: int = 50, offset: int = 0,
                          order: str = "", dir: str = "",
                          from_ts: int = 0, to_ts: int = 0, preset: str = ""):
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
        # Defensive caps. limit=50 default, hard ceiling of 500. The others
        # only need to fit SQLite's integers: past that a request would be a
        # 500 rather than simply finding nothing.
        limit = max(1, min(int(limit), 500))
        offset = max(0, min(int(offset), _SEARCH_INT_MAX))
        from_ts = min(int(from_ts), _SEARCH_INT_MAX)
        to_ts = min(int(to_ts), _SEARCH_INT_MAX)
        # The Today button: the start of today in Aerodrome's timezone, so it means
        # the same as the Stats cards and the word `today`, whatever the browser's.
        if preset == "today":
            from_ts = tzwindow.today_bounds(tzwindow.resolve_tz(CONFIG))[0]
            to_ts = 0

        # v2.52.0: read date format preference from config so the parser
        # accepts the locale's slash-date convention. Defaults to MDY
        # if the section/key is missing (older configs that haven't
        # been merged with the example config yet).
        date_fmt = (CONFIG.get("display") or {}).get("date_format", "MDY")
        parsed = parse_query(q, date_format=date_fmt, tz=tzwindow.resolve_tz(CONFIG))
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
                # So tail-only watchlist entries participate in `wl` /
                # `watchlist` filtering.
                resolved_tails=watchlist_tail_map(),
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
            # The window in words for its chip, and the tokens that made it, so
            # removing another chip keeps the date as typed.
            "time_label": parsed["time_label"],
            "time_tokens": parsed["time_tokens"],
            "total_count": result["total_count"],
            "rows": result["rows"],
            "execution_ms": result["execution_ms"],
            "error": result.get("error"),
        }

    @app.get("/api/search/aircraft/{icao}")
    def get_search_aircraft(icao: str):
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
    def get_search_suggestions():
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
    def get_watchlist_entries():
        # Tail entries carry how their lookup went, for the chip and the Live
        # alert. On a copy: these fields are for display and never reach
        # config.yaml.
        entries = []
        for entry in CONFIG.get("watchlist", []) or []:
            if isinstance(entry, dict) and entry.get("tail") and not entry.get("icao"):
                key = str(entry["tail"]).strip().upper()
                entry = {**entry,
                         "tail_status": watchlist_tail_status(key),
                         "tail_icaos": watchlist_tail_map().get(key, [])}
            entries.append(entry)
        return {"entries": entries}

    @app.post("/api/watchlist/add")
    def add_watchlist_entry(entry: WatchlistEntry):
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

        # v3.4.148: the read-modify-write holds CONFIG_LOCK, and config.yaml
        # is saved (all or nothing) BEFORE the running config changes — a
        # failed save leaves both as they were and says why.
        with config_io.CONFIG_LOCK:
            # v3.4.149: not while a restore waits for its restart (it would
            # put the backup's watchlist over this one without a word).
            waiting = _restore_waiting_response()
            if waiting is not None:
                return waiting
            watchlist = list(CONFIG.get("watchlist") or [])
            # Duplicate check — compare case-insensitively across all identifier types
            for existing in watchlist:
                if (existing.get("icao", "").upper() == new_entry.get("icao", "").upper() and
                    existing.get("tail", "").upper() == new_entry.get("tail", "").upper() and
                    existing.get("callsign", "").upper() == new_entry.get("callsign", "").upper() and
                    existing.get("model", "").lower() == new_entry.get("model", "").lower()):
                    return JSONResponse(status_code=409, content={"error": "Already in watchlist"})

            watchlist.append(new_entry)
            failed = _save_config_or_response({**CONFIG, "watchlist": watchlist})
            if failed is not None:
                return failed
            CONFIG["watchlist"] = watchlist
        # So a new {tail: "..."} entry surfaces in search filtering as soon
        # as it's added, and the chip can tell a lookup is under way.
        sync_watchlist_tails(CONFIG)
        return {"status": "added", "entry": new_entry, "message": "Live within one poll cycle"}

    @app.post("/api/watchlist/remove")
    def remove_watchlist_entry(entry: WatchlistEntry):
        identifier = entry.identifier
        ident_upper = identifier.upper()
        ident_lower = identifier.lower()

        # v3.4.148: same locked save-then-swap as add (the history delete
        # below stays outside the lock).
        with config_io.CONFIG_LOCK:
            waiting = _restore_waiting_response()       # v3.4.149 (see add)
            if waiting is not None:
                return waiting
            watchlist = CONFIG.get("watchlist") or []
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
            failed = _save_config_or_response({**CONFIG, "watchlist": watchlist})
            if failed is not None:
                return failed
            CONFIG["watchlist"] = watchlist
        # After a remove, the shared map no longer carries the removed tail.
        sync_watchlist_tails(CONFIG)

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
                # v3.4.141: the hourly rollup behind the Stats watchlist
                # cards goes too, or they kept counting the deleted hits.
                cur.execute(
                    "DELETE FROM watchlist_hourly WHERE watchlist_label = ?",
                    (matched_label,),
                )
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
    def watchlist_history_count(label: Optional[str] = Query(None)):
        """Return how many watchlist_sightings rows match a given label, or
        the total row count if no label is given. Used by the UI to populate
        the remove-with-history confirm dialog and the clear-all-history
        button. Never errors — returns 0 on DB failure."""
        return {"label": label, "count": _count_watchlist_history(label)}

    @app.post("/api/watchlist/history/clear")
    def watchlist_history_clear():
        """Delete ALL rows from watchlist_sightings (and the watchlist_hourly
        rollup the Stats cards read). Irreversible. The UI gates this behind
        an explicit confirm showing the row count."""
        try:
            conn = _db_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM watchlist_sightings")
            deleted = cur.rowcount or 0
            cur.execute("DELETE FROM watchlist_hourly")   # v3.4.141: Stats cards' rollup
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
    def get_first_seen(icaos: str = Query("", description="Comma-separated ICAO hex list")):
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
    # any unresolved ICAOs into the shared hexdb_resolver queue. Its single
    # worker drains at ~2 req/sec (respectful to hexdb's free API) and
    # populates the cache. Consequence: the first time a new ICAO appears,
    # its Track link gracefully falls back to airplanes.live (whatever the
    # frontend configured as fallback); within a few seconds the cache
    # fills and subsequent renders use the provider-specific reg URL.
    # v3.4.138: this used to be a private queue + worker; the collector now
    # feeds the same queue (type codes for feeds without `t`), and one
    # worker keeps the combined hexdb rate at ~2 req/sec.
    import collector as _collector_mod
    import hexdb_resolver as _hexdb_resolver
    import threading as _threading

    _hexdb_resolver.start_worker()

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
        """Background thread body — ticks every 60s, until a newer
        get_app() replaces this app (schedulers_stop)."""
        import time as _t
        # First tick: sleep a moment so we don't fire in the middle of
        # a config reload / service restart.
        if schedulers_stop.wait(10.0):
            return
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
            if schedulers_stop.wait(60.0):
                return

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
            version_path = _install_dir() / "VERSION"
            if version_path.exists():
                return version_path.read_text().strip()
        except Exception:
            pass
        return ""

    def _is_newer_version(latest_tag: str, running: str) -> bool:
        """True if latest_tag is strictly newer than running. False for
        equal, older, or unparseable inputs — Aerodrome never suggests
        downgrades, and never crashes on non-semver tag formats
        (updater.parse_version: 'v2.98.3' / '2.98.3'; '-rc1' etc. → None)."""
        l = updater.parse_version(latest_tag)
        r = updater.parse_version(running)
        if l is None or r is None:
            return False
        return l > r

    def _update_state_db():
        """The sightings database, for the update check's state row — opened
        without creating it: a check that runs while the file is gone
        (switch-to-real deletes it before its restart) must not leave an
        empty database in its place."""
        uri = Path(CONFIG["data"]["db_file"]).resolve().as_uri() + "?mode=rw"
        return _open_db_conn(uri, uri=True)

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
            conn = _update_state_db()
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
            conn = _update_state_db()
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
        yet (subject to the failure back-off). Returns False when
        poll_interval is 'never' — manual 'Check now' clicks still work
        in that mode."""
        cfg = _updates_config()
        pi = cfg["poll_interval"]
        if pi == "never":
            return False
        interval_s = _POLL_INTERVAL_SECONDS.get(pi)
        if interval_s is None:
            return False
        return _update_check_due_in(
            interval_s, int(time.time()), _get_update_state()) == 0

    def _seconds_until_next_check() -> int:
        """How long the scheduler should sleep before next deciding whether
        to check. Bounded to [60s, 3600s]: the 1-hour cap means changing
        poll_interval (or toggling enabled) takes effect within an hour
        with no service restart needed — the LIVE_KEYS contract. A failing
        check returns the retry delay instead, so the scheduler sleeps out
        the back-off rather than waking every minute."""
        cfg = _updates_config()
        pi = cfg["poll_interval"]
        if pi == "never" or pi not in _POLL_INTERVAL_SECONDS:
            return 3600
        interval_s = _POLL_INTERVAL_SECONDS[pi]
        return min(3600, max(60, _update_check_due_in(
            interval_s, int(time.time()), _get_update_state())))

    def _update_check_scheduler():
        """Background thread body. Wakes on schedule; calls
        _perform_github_check() when the interval has elapsed and updates
        are enabled. Modeled on _daily_summary_scheduler. Always starts
        regardless of enabled flag so enabling at runtime via config edit
        works without service restart. Ends when a newer get_app()
        replaces this app (schedulers_stop)."""
        # Settle delay: avoid firing in the middle of a service restart
        if schedulers_stop.wait(15.0):
            return
        logger.info("Update-check scheduler started")

        while True:
            try:
                cfg = _updates_config()
                if cfg["enabled"] and _interval_elapsed():
                    _perform_github_check()
            except Exception as e:
                logger.warning(f"Update-check scheduler tick failed: {e}")
            if schedulers_stop.wait(_seconds_until_next_check()):
                return

    # AERODROME_NO_UPDATE_CHECK=1 is set by the test suite: tests build apps
    # and must never reach api.github.com.
    if os.environ.get("AERODROME_NO_UPDATE_CHECK") != "1":
        _update_check_thread = _threading.Thread(
            target=_update_check_scheduler,
            name="update-check-scheduler",
            daemon=True,
        )
        _update_check_thread.start()

    @app.post("/api/notifications/daily-summary/test")
    def post_daily_summary_test():
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
        # v3.4.140: hex only — anything else used to reach the caches, the DB
        # cache tables and hexdb.io (any web page can fire this GET).
        wanted = list({s for s in raw if re.fullmatch(r"[0-9A-F]{6}", s)})[:500]
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
        # enqueue() dedupes against what's already queued this process
        # lifetime (and logs + drops when the queue is full), so frontend
        # retries don't pile the same ICAO up.
        for icao in remaining:
            _hexdb_resolver.enqueue(icao)

        return {"tails": result}

    # --- Tail resolver diagnostics ---
    # Introspection endpoint for debugging why Track links aren't using the
    # configured provider. Returns cache stats, queue state, and worker
    # health so users/operators can see at a glance whether the resolver
    # is working. Intended to be curl-friendly:
    #   curl -s http://HOST:PORT/api/resolve-tail/debug | python3 -m json.tool
    @app.get("/api/resolve-tail/debug")
    def get_resolve_tail_debug():
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
        qs = _hexdb_resolver.queue_stats()
        worker = _hexdb_resolver._worker_thread
        return {
            "cache_size": len(cache),
            "cache_positive": positive,
            "cache_negative": negative,
            "queue_depth": qs["primary_depth"] + qs["backfill_depth"],
            "queue_seen_lifetime": qs["seen_lifetime"],
            "worker_alive": bool(worker and worker.is_alive()),
            "worker_name": worker.name if worker else None,
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
    def get_capacity():
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
    def get_version():
        """v2.87.2: lightweight version endpoint. The Updates page
        polls this during apply-and-restart so the browser can show
        live status (shutting down, restarting, back online) instead
        of the dead "this site can't be reached" page that browsers
        show by default when the service goes down mid-request.

        Tiny payload by design — needs to be fast even when a Pi 4B
        is mid-migration with the service just barely back up. No
        DB access, no config-dependent computation; just the version
        string the server has cached in memory.

        This is a LIVENESS probe — answers as soon as FastAPI is
        listening on the port. Not the same as readiness (rollup
        backfills, page cache warmup, etc.) — for that see
        /api/ready below.
        """
        return {"version": _aerodrome_version}

    @app.get("/api/ready")
    def get_ready():
        """v3.4.10: readiness probe, distinct from /api/version's
        liveness probe. Returns 200 only when the service is ready
        to serve heavy requests (Stats tab, dashboard pages, etc.).
        Returns 503 with a JSON body explaining what's still in
        flight when it isn't.

        Why this exists: /api/version answers as soon as FastAPI
        binds the port, which on a heavy install (5+ GB DB, 30M+
        rows, 4 GB RAM) happens seconds before the rollup-backfill
        daemon threads finish and seconds before the SQLite page
        cache is warm enough to serve Stats queries quickly. The
        Updates page used to poll /api/version, detect "service is
        alive!" the moment FastAPI bound, then reload the dashboard
        — which would fire heavier requests against a service that
        was technically up but not ready. Result: connection-refused
        or hung-request "dead page" appearance until the backend
        caught up.

        Readiness checks (all must be true for 200):
          1. Schema migrations complete (always true here — server
             only starts after migrations succeed)
          2. hourly_rollup backfill complete or skipped
          3. military_rollup backfill complete or skipped
          4. watchlist_rollup backfill complete or skipped
          5. Trivial DB read succeeds in <500ms (probes the SQLite
             page cache + planner basic responsiveness)

        Conservative on failure: any subsystem returning an
        unexpected status counts as "not ready" rather than
        "ready, ignore." Better to make the user wait an extra few
        seconds than to reload into a half-broken page.
        """
        import collector as _coll
        try:
            backfill_states = {
                "hourly_rollup":    _coll.get_hourly_backfill_status(),
                "military_rollup":  _coll.get_military_backfill_status(),
                "watchlist_rollup": _coll.get_watchlist_backfill_status(),
            }
        except Exception as e:
            return JSONResponse(status_code=503, content={
                "ready": False,
                "version": _aerodrome_version,
                "reason": f"backfill-state probe failed: {e}",
            })

        pending = {}
        for key, state in backfill_states.items():
            phase = (state or {}).get("phase", "unknown")
            if phase in ("running", "unknown"):
                pending[key] = state
        if pending:
            return JSONResponse(status_code=503, content={
                "ready": False,
                "version": _aerodrome_version,
                "reason": "rollup backfill(s) in progress",
                "pending": pending,
            })

        # DB probe — confirms the page cache + planner are
        # responsive enough to serve heavy requests. Tiny query
        # against a small table to avoid skewing the measurement.
        try:
            t0 = time.time()
            db_path = CONFIG["data"]["db_file"]
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                conn.execute("SELECT COUNT(*) FROM stats_records").fetchone()
            finally:
                conn.close()
            db_ms = (time.time() - t0) * 1000
            if db_ms > 500:
                return JSONResponse(status_code=503, content={
                    "ready": False,
                    "version": _aerodrome_version,
                    "reason": f"db probe slow ({int(db_ms)}ms); cache likely cold",
                    "db_probe_ms": int(db_ms),
                })
        except Exception as e:
            return JSONResponse(status_code=503, content={
                "ready": False,
                "version": _aerodrome_version,
                "reason": f"db probe failed: {e}",
            })

        return {
            "ready": True,
            "version": _aerodrome_version,
            "db_probe_ms": int(db_ms),
        }

    @app.get("/api/status")
    def get_status():
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
        # airplanes_live install can need hexdb for tails that aren't US
        # N-numbers. Those failures show on the entry's chip on the Watchlist
        # tab, so they don't need a continuous reachability probe. The probe gate
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
            # v3.4.50: serialize the whole db_check under the stampede lock.
            # The cache-freshness checks below happen AFTER acquiring, so a
            # thread that waited on the lock finds the cache another thread
            # just refreshed and skips the expensive recompute — the
            # double-check is inherent in the existing "if fresh: use else:
            # recompute" structure. This also serializes the connection
            # opens, which matters on a memory-constrained box where many
            # concurrent connections (each reserving cache_size + mmap_size)
            # were compounding the page-cache thrash.
            _db_check_refresh_lock.acquire()
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

                    # v2.50.19/v3.4.39: all_sightings stats come from
                    # seen_aircraft (unique) and the hourly rollup (total).
                    # seen_aircraft is never pruned by retention, so the
                    # WHERE last_seen_at >= cutoff restricts the count to the
                    # retention window — same set the old rollup-window query
                    # produced, via a single index range scan + COUNT(*) (no
                    # DISTINCT, no temp B-tree). The rollup SUM gives the
                    # cumulative raw-sighting count over the window.
                    all_cutoff_ts = now_ts - (CONFIG["retention"]["all_days"] * 86400)
                    all_unique = conn.execute(
                        "SELECT COUNT(*) FROM seen_aircraft WHERE last_seen_at >= ?",
                        (all_cutoff_ts,)
                    ).fetchone()[0]
                    # v3.4.57: hour_bucket is stored in seconds, so the window
                    # bound is all_cutoff_ts directly (not //3600). Behaviorally
                    # a no-op — the rollup is pruned to retention, so both forms
                    # select the same rows — but corrected so no stray //3600
                    # remains and this matches the other hour_bucket queries.
                    all_total_row = conn.execute(
                        "SELECT COALESCE(SUM(sighting_count), 0) FROM sightings_hourly WHERE hour_bucket >= ?",
                        (all_cutoff_ts,)
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
            finally:
                _db_check_refresh_lock.release()
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
            # v3.4.41: runtime poll-failure state from the collector module.
            # Populated below from collector.get_collector_health() so the
            # Status card can surface the SPECIFIC error (e.g. the v3.4.40
            # non-ADS-B detection message) rather than just the generic
            # "No data written yet" symptom.
            "last_poll_error": None,
            "consecutive_failed_polls": 0,
        }
        try:
            import collector as _collector_mod
            _ch = _collector_mod.get_collector_health()
            collector_check["last_poll_error"] = _ch.get("last_poll_error")
            collector_check["consecutive_failed_polls"] = int(_ch.get("consecutive_failed_polls") or 0)
        except Exception as e:
            # Non-fatal — the symptom-level error path below still works,
            # we just lose the cause-level diagnostic for this response.
            logger.debug(f"collector health lookup failed in /api/status: {e}")
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

        # --- Notifications (advisory): is the ntfy server answering, and did
        # the last alert that was sent go through? See _notifications_check.
        try:
            notify_check, nonlocal_state["notify_probe_cache"] = _notifications_check(
                CONFIG.get("notifications"),
                _NOTIFIER.stats() if _NOTIFIER is not None else None,
                nonlocal_state.get("notify_probe_cache"), now)
        except Exception as e:
            logger.debug(f"notifications check failed in /api/status: {e}")
            notify_check = {"enabled": False, "ok": True}

        # --- FlightAware (v3.4.169): only when a key is set. Built from what
        # Aerodrome already knows (its own count, the key's last test, the
        # last lookup, FlightAware's usage figure as last fetched): never a
        # call to FlightAware, because every page polls this endpoint.
        fa_check = None
        try:
            fa_cfg = CONFIG.get("flightaware") or {}
            if str(fa_cfg.get("api_key") or "").strip():
                import flightaware as _flightaware
                _fa = _fa_client()
                fa_check = dict(_fa.status(fa_cfg), usage=_fa.cached_usage(),
                                usage_url=_flightaware.USAGE_URL)
        except Exception as e:
            logger.debug(f"flightaware status failed in /api/status: {type(e).__name__}")
            fa_check = None

        # --- Severity: see _health_summary ---
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
        severity, failing, health_reason = _health_summary(
            {"receiver": receiver_check["ok"], "database": db_check["ok"],
             "collector": collector_check["ok"], "webserver": webserver_check["ok"]},
            hexdb_ok=advisory_ok, capacity_ok=capacity_ok,
            sudoers=nonlocal_state.get("last_sudoers_check"), system=system_info,
            notifications=notify_check, flightaware=fa_check,
        )

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
            "health_reason": health_reason,
            "timestamp": int(now),
            "version": version,
            "update_available": _upd_available,
            "components": {
                "collector": collector_check,
                "webserver": webserver_check,
                "database": db_check,
                "receiver": receiver_check,
                "hexdb_resolver": hexdb_check,
                "notifications": notify_check,
                "flightaware": fa_check,
            },
            "system": system_info,
            "retention": CONFIG["retention"],
            # v3.1.0: demo mode flag. Every admin page polls /api/status
            # on load (v2.49.3), so the dashboard banner JS reads demoMode
            # from here without needing a separate API call. False on
            # real installs; flips to true when --demo was passed at
            # install time. Flips back to false post-switch-to-real-wizard.
            "demo_enabled": bool((CONFIG.get("demo") or {}).get("enabled", False)),
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
            # v3.4.6: surface the new military_hourly + watchlist_hourly
            # backfill states alongside hourly_rollup. Same shape, used
            # by the same migration-in-progress banner.
            "military_rollup": (lambda: (
                __import__("collector").get_military_backfill_status()
            ))(),
            "watchlist_rollup": (lambda: (
                __import__("collector").get_watchlist_backfill_status()
            ))(),
        }

    # --- CSV Export (all sightings for a given tab) ---
    # v2.67.0 (Phase 1D): the 'all' tab branch was removed alongside the
    # All tab. Watchlist and Military are the remaining tabs that route
    # through this endpoint. Search results have their own export path
    # (/api/search?... + frontend CSV stitching) which is independent.
    @app.get("/api/export")
    def export_csv(
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
    def _resolved_ring_distances():
        """Effective range-ring distances, resolved server-side. Shared by the
        radar (/api/ui-config) AND the aircraft-detail position map
        (/api/aircraft/{icao}/positions) so the two maps draw identical rings.
        Follows the Stats range-rose bands when configured, else parses the
        comma-separated map.ring_distances string; defaults to 50/100/150/200/250.
        Defensive parse — a hand-edited config shouldn't break either map."""
        mp = CONFIG.get("map") or {}
        if mp.get("follow_range_rose"):
            return ((CONFIG.get("stats") or {}).get("range_rose") or {}).get(
                "distance_buckets") or [50, 100, 150, 200, 250]
        rings = []
        for p in str(mp.get("ring_distances") or "50,100,150,200,250").split(","):
            p = p.strip()
            if not p:
                continue
            try:
                v = float(p)
            except ValueError:
                continue
            if v > 0:
                rings.append(int(v) if v == int(v) else v)
        return rings or [50, 100, 150, 200, 250]

    @app.get("/api/ui-config")
    def get_ui_config():
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
        # v3.5.0: radar map prefs. Resolve the effective ring distances here
        # (server-side) so the frontend just gets a clean list: follow the
        # Stats range-rose bands when asked, else parse the comma-separated
        # string. Defensive parse — the validator already guarantees the
        # string is well-formed on save, but a hand-edited config shouldn't
        # break the dashboard.
        mp = CONFIG.get("map") or {}
        _rings = _resolved_ring_distances()
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
                # v3.4.110: default /board layout for screens that haven't
                # picked one ("ask" = show the picker); a per-screen choice
                # (localStorage or ?mode=) always wins over this.
                "wallboard_layout": display_cfg.get("wallboard_layout", "ask"),
                # v3.4.114: route-display toggle (radar overlay, detail page,
                # display board). Off = frontends skip the lookup entirely.
                "show_routes": display_cfg.get("show_routes", True) is not False,
            },
            "map": {
                "show_range_rings": bool(mp.get("show_range_rings", True)),
                "ring_distances": _rings,
                "starting_view": mp.get("starting_view", "fit_all"),
                "fixed_zoom": int(mp.get("fixed_zoom", 9) or 9),
                "default_theme": mp.get("default_theme", "auto"),
                # v3.5.x: aircraft type silhouettes (Live + Board markers
                # draw a per-typecode shape instead of the chevron).
                "type_shapes": mp.get("type_shapes", True) is not False,
                # CARTO dark-basemap key. Leaflet fetches tiles in the
                # browser, so the key has to travel with this payload
                # (same LAN-trust model as the rest of /api/config).
                "carto_api_key": (
                    mp.get("carto_api_key").strip()
                    if isinstance(mp.get("carto_api_key"), str)
                    else ""
                ),
            },
        }

    # =========================================================================
    # /api/coverage — max-range coverage outline (radar overlay, v3.5.0)
    # =========================================================================
    # The farthest aircraft received in each compass direction, as a polygon
    # the radar can overlay (tar1090's "range outline"). Computed from the
    # SAME sightings_hourly grid the Stats range rose uses — group positions
    # into a coarse lat/lon grid, then for each of N bearing bins keep the
    # single farthest cell. Cached (recompute at most every few minutes) since
    # it scans the rollup. Plain `def` so the scan runs off the event loop.
    _coverage_cache = {"ts": 0.0, "payload": None}
    @app.get("/api/coverage")
    def get_coverage():
        r = CONFIG.get("receiver", {})
        rx_lat, rx_lon = r.get("latitude"), r.get("longitude")
        if not isinstance(rx_lat, (int, float)) or not isinstance(rx_lon, (int, float)):
            return {"available": False, "reason": "receiver location not configured"}
        unit = (r.get("distance_unit") or "mi").lower()
        now = time.time()
        if _coverage_cache["payload"] is not None and (now - _coverage_cache["ts"]) < 300:
            return _coverage_cache["payload"]
        GRID_RES = 0.05
        conn = _open_db_conn(CONFIG["data"]["db_file"])
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT ROUND(last_lat / ?) * ? AS lat_b,
                       ROUND(last_lon / ?) * ? AS lon_b
                FROM sightings_hourly
                WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
                GROUP BY lat_b, lon_b
            """, (GRID_RES, GRID_RES, GRID_RES, GRID_RES)).fetchall()
        finally:
            conn.close()
        NB = 36  # 10-degree bearing bins
        best_d = [0.0] * NB
        best_pt = [None] * NB
        for row in rows:
            lat2, lon2 = row["lat_b"], row["lon_b"]
            d = _dist_haversine(rx_lat, rx_lon, lat2, lon2, unit)
            if d is None or d <= 0:
                continue
            bi = int(round(_dist_compass_bearing(rx_lat, rx_lon, lat2, lon2) / (360.0 / NB))) % NB
            if d > best_d[bi]:
                best_d[bi] = d
                best_pt[bi] = [round(lat2, 4), round(lon2, 4)]
        points = [p for p in best_pt if p is not None]
        payload = {
            "available": True,
            "receiver": {"lat": rx_lat, "lon": rx_lon},
            "points": points,  # ordered by bearing bin (0°..350°)
            "max_distance": round(max(best_d), 1) if any(best_d) else 0,
            "distance_unit": unit,
        }
        _coverage_cache["ts"] = now
        _coverage_cache["payload"] = payload
        return payload

    # =========================================================================
    # /api/live/trails — bulk recent tracks for the radar fleet-trails (↝)
    # =========================================================================
    _live_trails_cache = {"ts": 0.0, "window": None, "payload": None}
    @app.get("/api/live/trails")
    def get_live_trails(window: int = 3600):
        """Recent ground track for EVERY aircraft seen in the window — the data
        behind the radar's fleet-trails overlay (the ↝ toggle).

        One indexed query over all_sightings instead of N per-aircraft
        /positions calls: the client fetches this ONCE when the toggle goes on
        to seed each contact's existing track, then grows the trails forward
        from the /api/live poll it already runs — so this endpoint is hit
        rarely (a short cache covers re-toggles / multiple browsers).

        Each aircraft's track is trimmed server-side to its CURRENT PASS — the
        most-recent contiguous run of fixes, cut at the first gap larger than
        TRAIL_GAP_SEC (the same 10-minute rule the single-plane trail uses) —
        then shape-simplified with Douglas-Peucker (TRAIL_DP_EPS_DEG): corners
        and curves are kept, straight cruise legs collapse to their endpoints,
        so the trail reads sharp while staying light. A safety cap of
        TRAIL_MAX_PTS still applies to a pathologically wandering track. The
        fleet view is a touch coarser than clicking one plane (which stays
        full-resolution and un-thinned), but far closer than the old blind
        stride sampler.

        window — look-back seconds (default 3600 = 1h, clamped to [60, 6h]).
                 Bounds the scan; a current pass longer than the window is
                 clipped to it.

        Response: {"ok": True, "trails": {ICAO: [[seen_at, lat, lon, alt], ...]}}
        positions ascending by time — the same [t, lat, lon, alt] shape as the
        single-aircraft /positions endpoint.
        """
        TRAIL_GAP_SEC = 600          # a hole > 10 min = a separate pass
        TRAIL_DP_EPS_DEG = 0.0009    # Douglas-Peucker tolerance (~100 m)
        TRAIL_MAX_PTS = 150          # hard safety cap per aircraft
        window = max(60, min(int(window or 3600), 6 * 3600))
        now = int(time.time())
        nowf = time.time()
        if (_live_trails_cache["payload"] is not None
                and _live_trails_cache["window"] == window
                and (nowf - _live_trails_cache["ts"]) < 4):
            return _live_trails_cache["payload"]

        conn = _open_db_conn(CONFIG["data"]["db_file"])
        try:
            # idx_all_seen_icao(seen_at, icao) makes this a range scan that also
            # yields rows in seen_at order — so per-icao lists below come out
            # already sorted ascending, no extra sort needed.
            rows = conn.execute(
                "SELECT icao, seen_at, lat, lon, altitude FROM all_sightings "
                "WHERE seen_at >= ? AND lat IS NOT NULL AND lon IS NOT NULL "
                "ORDER BY seen_at",
                (now - window,),
            ).fetchall()
        finally:
            conn.close()

        by_icao = {}
        for icao, t, lat, lon, alt in rows:
            by_icao.setdefault(icao, []).append([t, lat, lon, alt])

        trails = {}
        total = 0
        for icao, pts in by_icao.items():
            # Current pass: walk back from the latest fix, stop at the first
            # gap > TRAIL_GAP_SEC. (pts is ascending by seen_at.)
            start = len(pts) - 1
            while start > 0 and (pts[start][0] - pts[start - 1][0]) <= TRAIL_GAP_SEC:
                start -= 1
            seg = pts[start:]
            if len(seg) < 2:
                continue             # a single fix has no path to draw
            # Shape-simplify: keep corners/curves, collapse straights. Endpoints
            # are preserved by DP, so the leading end stays on the plane's
            # current spot and the trailing end on where the pass began.
            if len(seg) > 2:
                seg = _dp_simplify_track(seg, TRAIL_DP_EPS_DEG)
            # Safety net: a track that genuinely wanders (holding pattern, survey
            # grid) can still exceed the budget after DP — even-thin to the cap,
            # always keeping the most-recent fix.
            if len(seg) > TRAIL_MAX_PTS:
                stride = (len(seg) + TRAIL_MAX_PTS - 1) // TRAIL_MAX_PTS
                sampled = seg[::stride]
                if sampled[-1] is not seg[-1]:
                    sampled.append(seg[-1])
                seg = sampled
            trails[icao] = seg
            total += len(seg)

        payload = {"ok": True, "trails": trails, "window": window, "count": total}
        _live_trails_cache["ts"] = nowf
        _live_trails_cache["window"] = window
        _live_trails_cache["payload"] = payload
        return payload

    # =========================================================================
    # /api/stats — today-stats for the Stats tab
    # =========================================================================
    def _day_bounds_ts():
        """Return (start_ts, end_ts) for 'today' in the configured timezone as
        Unix timestamps. start_ts = local midnight. end_ts = now."""
        now = time.time()
        start, _ = tzwindow.today_bounds(tzwindow.resolve_tz(CONFIG), now)
        return start, int(now)

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
            "top_aircraft", "top_types", "top_operators",
            "military_branches", "category_mix", "top_countries"
        ]),
        ("patterns",    "Patterns", [
            "hourly_histogram"
        ]),
        ("history",     "History", [
            "first_time_seen", "daily_counts_7d", "watchlist_frequency"
        ]),
        ("records",     "All-time records", [
            "all_time_records", "most_seen_alltime"
        ]),
        ("coverage",    "Coverage", [
            "range_rose", "distance_histogram"
        ]),
    ]
    # Flat lookup: card_name → group_name
    _CARD_TO_GROUP = {c: g for g, _, cs in CARD_GROUPS for c in cs}

    # Display label for each seen_aircraft.category token (the column stores
    # the lowercase token; categorize.py owns the rules). v3.4.143: one map
    # for the category_mix card face and both of its drills — the
    # all_category_mix drill had drifted onto its own stale copy of the
    # old rules, so its counts disagreed with the card.
    _CATEGORY_LABELS = {
        "commercial":       "Commercial",
        "general_aviation": "General Aviation",
        "military":         "Military",
        "helicopter":       "Helicopter",
        "unknown":          "Unknown",
    }

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
    def get_stats():
        import sqlite3
        st = CONFIG.get("stats") or {}
        if not st.get("enabled", True):
            return {"enabled": False, "cards": {}}

        cards = st.get("cards") or {}
        groups = st.get("groups") or {}
        start_ts, end_ts = _day_bounds_ts()
        # v3.4.171: a half-hour time zone's day starts mid-way through a UTC
        # rollup hour (see search.window_sources). Today's rollup cards read
        # the whole hours from the rollups and the partial edges from the raw
        # tables, as the 7-day card already did; in other zones there are no
        # edges and nothing changes.
        _today_whole, _today_edges = window_sources(start_ts, end_ts)

        def _today_parts(rollup_sql: str, raw_sql: str):
            """SELECTs (and their params) covering today: rollup_sql takes
            `hour_bucket >= ? AND hour_bucket < ?`, raw_sql `seen_at >= ? AND
            seen_at < ?`, in that order."""
            parts, params = [], []
            if _today_whole:
                parts.append(rollup_sql)
                params += list(_today_whole)
            for edge in _today_edges:
                parts.append(raw_sql)
                params += list(edge)
            return parts, tuple(params)

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
                parts, params = _today_parts(
                    "SELECT icao FROM sightings_hourly WHERE hour_bucket >= ? AND hour_bucket < ?",
                    "SELECT icao FROM all_sightings WHERE seen_at >= ? AND seen_at < ?")
                row = q(f"SELECT COUNT(*) AS n FROM ({' UNION '.join(parts)})", params)
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
                # v3.4.6: dual-path rollup-or-raw same as the Stats card
                # block above.
                try:
                    _mil_phase = __import__("collector").get_military_backfill_status().get("phase")
                except Exception:
                    _mil_phase = None
                if _mil_phase == "complete":
                    parts, params = _today_parts(
                        "SELECT icao FROM military_hourly WHERE hour_bucket >= ? AND hour_bucket < ?",
                        "SELECT icao FROM military_sightings WHERE seen_at >= ? AND seen_at < ?")
                    row = q(f"SELECT COUNT(*) AS n FROM ({' UNION '.join(parts)})", params)
                else:
                    row = q(
                        "SELECT COUNT(DISTINCT icao) AS n FROM military_sightings "
                        "WHERE seen_at >= ?",
                        (start_ts,))
                result["cards"]["military_today"] = row[0]["n"] if row else 0

            if card_check("watchlist_hits"):
                # v3.4.6: dual-path rollup-or-raw.
                try:
                    _watch_phase = __import__("collector").get_watchlist_backfill_status().get("phase")
                except Exception:
                    _watch_phase = None
                if _watch_phase == "complete":
                    parts, params = _today_parts(
                        "SELECT icao FROM watchlist_hourly WHERE hour_bucket >= ? AND hour_bucket < ?",
                        "SELECT icao FROM watchlist_sightings WHERE seen_at >= ? AND seen_at < ?")
                    row = q(f"SELECT COUNT(*) AS n FROM ({' UNION '.join(parts)})", params)
                else:
                    row = q(
                        "SELECT COUNT(DISTINCT icao) AS n FROM watchlist_sightings "
                        "WHERE seen_at >= ?",
                        (start_ts,))
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
                parts, params = _today_parts(
                    """SELECT icao, callsign, max_altitude AS altitude, aircraft_type
                       FROM sightings_hourly
                       WHERE hour_bucket >= ? AND hour_bucket < ? AND max_altitude IS NOT NULL
                         AND typeof(max_altitude) IN ('integer', 'real') AND icao NOT LIKE '~%'""",
                    # The raw edge, one row per aircraft like the rollup.
                    """SELECT icao, MAX(callsign) AS callsign, MAX(altitude) AS altitude,
                              MAX(aircraft_type) AS aircraft_type
                       FROM all_sightings
                       WHERE seen_at >= ? AND seen_at < ? AND altitude IS NOT NULL
                         AND typeof(altitude) IN ('integer', 'real') AND icao NOT LIKE '~%'
                       GROUP BY icao""")
                rows = q(f"SELECT * FROM ({' UNION ALL '.join(parts)}) "
                         "ORDER BY altitude DESC LIMIT 50", params)
                result["cards"]["highest_altitude"] = next(
                    (dict(r) for r in rows if _collector_mod.altitude_is_plausible(
                        r["aircraft_type"], r["altitude"])), None)

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
                parts, params = _today_parts(
                    """SELECT icao, callsign, min_nonzero_altitude AS altitude, aircraft_type
                       FROM sightings_hourly
                       WHERE hour_bucket >= ? AND hour_bucket < ? AND min_nonzero_altitude IS NOT NULL
                         AND typeof(min_nonzero_altitude) IN ('integer', 'real') AND icao NOT LIKE '~%'""",
                    """SELECT icao, MAX(callsign) AS callsign, MIN(altitude) AS altitude,
                              MAX(aircraft_type) AS aircraft_type
                       FROM all_sightings
                       WHERE seen_at >= ? AND seen_at < ? AND altitude > 0
                         AND typeof(altitude) IN ('integer', 'real') AND icao NOT LIKE '~%'
                       GROUP BY icao""")
                rows = q(f"SELECT * FROM ({' UNION ALL '.join(parts)}) "
                         "ORDER BY altitude ASC LIMIT 1", params)
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
                #
                # v3.4.157: the rollup says which hours to look at; the raw
                # readings of those hours say which speed a neighbouring reading
                # backs up (see the spike check in collector.py). One indexed
                # lookup per row, and the walk stops as soon as no later row can
                # beat the best so far: usually one or two.
                # v3.4.171: from the hour that holds midnight (a half-hour zone's
                # first hour straddles it); an agreed reading from before
                # midnight doesn't count for today.
                rows = q("""
                    SELECT icao, callsign, max_speed AS speed, aircraft_type, hour_bucket
                    FROM sightings_hourly
                    WHERE hour_bucket >= ? AND hour_bucket < ? AND max_speed IS NOT NULL
                      AND typeof(max_speed) IN ('integer', 'real')
                      AND icao NOT LIKE '~%'
                    ORDER BY max_speed DESC LIMIT 50
                """, (start_ts // 3600 * 3600, end_ts))
                window = _collector_mod.speed_agree_window(CONFIG["receiver"].get("poll_interval"))
                fastest = None
                for r in rows:
                    if fastest and r["speed"] <= fastest["speed"]:
                        break
                    found = _collector_mod.best_agreed_speed(
                        conn, r["icao"], r["hour_bucket"], r["aircraft_type"], window)
                    if found and found[1] < start_ts:
                        continue
                    if found and (not fastest or found[0] > fastest["speed"]):
                        fastest = {"icao": r["icao"], "callsign": r["callsign"],
                                   "speed": found[0], "aircraft_type": r["aircraft_type"]}
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
            if card_check("top_aircraft"):
                # v3.4.36: "most frequently seen aircraft" — the regulars
                # in your airspace. Sum sighting_count from sightings_hourly
                # within the window so the metric is windowed (unlike
                # seen_aircraft.sighting_count which is all-time). Join
                # with seen_aircraft for display fields (callsign, type,
                # operator, registration). The JOIN is on the rollup's
                # icao + seen_aircraft.icao PK, so it's an index-driven
                # lookup not a table scan.
                #
                # Why SUM(sighting_count) and not COUNT(DISTINCT day)?
                # SUM is much cheaper (no DISTINCT, single column read),
                # already covered by the (hour_bucket, icao) index, and
                # the semantic — "aircraft that spent the most time in
                # your sky" — matches what users mean when they ask for
                # the regulars. A slow helicopter loitering daily *is*
                # more present than a fast jet that streaks past once.
                # The bias is the feature.
                # v3.4.54: aggregate-first. The previous form joined
                # sightings_hourly to seen_aircraft and THEN grouped, which
                # let the planner choose to drive the join from the
                # 535K-row seen_aircraft table — measured at ~3s on the
                # reference install even with fresh statistics (the join
                # order, not stale stats, was the remaining cost). Doing the
                # windowed SUM/GROUP/LIMIT on sightings_hourly alone first
                # reduces it to 5 rows, then joins seen_aircraft for only
                # those 5. Same top-5 result; verified equivalent on
                # synthetic data (41ms -> 7ms, 0 mismatches).
                parts, params = _today_parts(
                    "SELECT icao, sighting_count AS n FROM sightings_hourly "
                    "WHERE hour_bucket >= ? AND hour_bucket < ?",
                    "SELECT icao, 1 AS n FROM all_sightings WHERE seen_at >= ? AND seen_at < ?")
                rows = q(f"""
                    WITH top AS (
                        SELECT icao, SUM(n) AS n
                        FROM ({' UNION ALL '.join(parts)})
                        GROUP BY icao
                        ORDER BY n DESC LIMIT 5
                    )
                    SELECT s.icao,
                           s.last_callsign,
                           s.aircraft_type,
                           s.aircraft_type_desc,
                           s.operator,
                           s.registration,
                           top.n AS n
                    FROM top
                    JOIN seen_aircraft s ON s.icao = top.icao
                    ORDER BY top.n DESC
                """, params)
                # v3.4.56: param is start_ts (seconds), NOT start_ts//3600.
                # hour_bucket is stored as (seen_at/3600)*3600 — truncated
                # to the hour but in SECONDS. Passing hours made the filter
                # (hour_bucket >= ~494000) match every row in the table, so
                # this card silently aggregated ALL-TIME data instead of the
                # intended day window — both wrong (the card is a "today"
                # card) and slow (full-table scan of the rollup). With the
                # seconds value the filter is selective and uses the
                # hour_bucket index.
                # Enrich each row with operator's friendly name (when
                # known) via the shared helper from v3.4.35. Same
                # pattern as top_operators.
                result["cards"]["top_aircraft"] = [
                    _attach_airline_name(dict(r), r["operator"]) for r in rows
                ]

            # --- All-time records ---
            if card_check("most_seen_alltime"):
                # "Most seen, all time" — companion to top_aircraft (which
                # windows to today). seen_aircraft.sighting_count is a running
                # all-time per-aircraft total, incremented on every sighting in
                # the collector upsert, and seen_aircraft is NEVER pruned by
                # retention — so this is a true all-time count, even for
                # aircraft whose rollup history has aged out. These are the
                # regulars that are *always* overhead. Read the counter
                # directly; no rollup aggregation needed.
                #
                # Deliberately NOT backed by an index on sighting_count:
                # that column is incremented on the hot collector upsert path,
                # so an index would add write amplification to EVERY sighting
                # to save ~50ms on this once-per-stats-load top-5 scan — a bad
                # trade. The 535K-row scan + top-5 sort is the cheaper side.
                # The query is auto-timed via card_check, so if it ever shows
                # up hot in /api/stats timings we can revisit.
                rows = q("""
                    SELECT icao,
                           last_callsign,
                           aircraft_type,
                           aircraft_type_desc,
                           operator,
                           registration,
                           sighting_count AS n
                    FROM seen_aircraft
                    ORDER BY sighting_count DESC
                    LIMIT 5
                """)
                # Same enrichment + row shape as top_aircraft so the frontend
                # reuses topAircraftCard unchanged.
                result["cards"]["most_seen_alltime"] = [
                    _attach_airline_name(dict(r), r["operator"]) for r in rows
                ]

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
                # v3.4.35: factored into _attach_airline_name (module-scope).
                out = [_attach_airline_name(dict(r), r["operator"]) for r in rows]
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
                rows = q("""
                    SELECT category, COUNT(*) AS n
                    FROM seen_aircraft
                    WHERE last_seen_at >= ? AND category IS NOT NULL
                    GROUP BY category
                """, (start_ts,))
                cl = [
                    {"category": _CATEGORY_LABELS.get(r["category"], r["category"]),
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
                # Timezone handling: rather than compute strftime-with-tz
                # inside SQL (inconsistent across SQLite builds), the bucket
                # is ((seen_at + offset) / 3600) % 24 with today's offset, or
                # offsets on a clock-change day, so both passes of a repeated
                # hour land in its bar, as the hour drill finds them.
                offset_sql = _histogram_offset_sql(
                    tzwindow.offset_segments(start_ts, end_ts + 1,
                                             tzwindow.resolve_tz(CONFIG)))
                rows = q(f"""
                    SELECT ((seen_at + {offset_sql}) / 3600) % 24 AS hour,
                           COUNT(DISTINCT icao) AS n
                    FROM all_sightings INDEXED BY idx_all_seen_icao
                    WHERE seen_at >= :start_ts
                    GROUP BY hour
                """, {"start_ts": start_ts})
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
                tz = tzwindow.resolve_tz(CONFIG)
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
                    # A half-hour timezone's day starts mid-bucket: its partial
                    # edge hours come from raw sightings, so this count, the
                    # day drill and Search agree. Other zones have no edges.
                    whole, edges = window_sources(b["start_ts"], b["end_ts"])
                    selects = ["SELECT icao FROM sightings_hourly "
                               "WHERE hour_bucket >= ? AND hour_bucket < ?"] if whole else []
                    selects += ["SELECT icao FROM all_sightings "
                                "WHERE seen_at >= ? AND seen_at < ?"] * len(edges)
                    row = q(f"SELECT COUNT(*) AS n FROM ({' UNION '.join(selects)})",
                            tuple(v for pair in ([whole] if whole else []) + edges for v in pair))
                    b["n"] = row[0]["n"] if row else 0
                    # Strip internal fields the frontend doesn't need
                    del b["start_ts"], b["end_ts"]
                result["cards"]["daily_counts_7d"] = buckets

            if card_check("watchlist_frequency"):
                # Top watchlist entries by number of distinct sightings
                # in the last 30 days (rolling window).
                #
                # v3.4.55: read the watchlist_hourly rollup instead of
                # scanning raw watchlist_sightings. The raw form grouped
                # ~2.7M rows by label with a COUNT(DISTINCT icao) (~1.3s on
                # the reference install); the rollup holds one row per
                # (icao, hour, label) — ~27K rows total — and carries
                # sighting_count, so SUM(sighting_count) reproduces the raw
                # COUNT(*) exactly and COUNT(DISTINCT icao) is over the same
                # set of aircraft. Same pattern top_aircraft already uses
                # against sightings_hourly. Verified identical results to
                # the raw query on synthetic data. The hour-bucket window
                # boundary is hour-aligned rather than exact-second, an
                # immaterial difference for a 30-day "top 10" card.
                thirty_days_ago = end_ts - (30 * 86400)
                rows = q("""
                    SELECT watchlist_label,
                           COUNT(DISTINCT icao) AS unique_aircraft,
                           SUM(sighting_count) AS total_hits
                    FROM watchlist_hourly
                    WHERE hour_bucket >= ? AND watchlist_label IS NOT NULL AND watchlist_label != ''
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
    def drill_stats(card: str, value: Optional[str] = Query(None),
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
            # v3.4.37: all_aircraft drill sets this to the real total-distinct-
            # aircraft count from a separate COUNT query (the SQL LIMIT in that
            # branch would otherwise make total_count just equal len(rows)).
            # All other drills leave it None and use the default len(rows).
            all_aircraft_total = None

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
                #
                # v3.4.143: one pass over today's slice, pinned to
                # idx_all_seen_icao — as is every other read here that takes
                # today's slice with no upper bound and groups by icao
                # (slowest, highest/lowest_altitude, unique_today, top_types,
                # top_operators, all_category_mix, all_countries). Only that
                # shape tempts the planner: with no stat4 it guesses that
                # `seen_at >= midnight` keeps ~25% of the table, so it walked
                # all of idx_all_icao (every row ever kept) to skip a GROUP BY
                # sort, then joined back through each aircraft's whole
                # history — 12-15 s per drill on a busy install for a slice
                # under 1% of the table. A fresh ANALYZE keeps that guess.
                # The per-aircraft peak is a window MAX over the same slice
                # rather than a CTE joined back: with a join left in, other
                # planner statistics picked other slow plans (a 52-58 s
                # skip-scan on a busy install, a scan of the peaks per row
                # after a one-aircraft first poll). init_db creates the
                # index on every start.
                #
                # v3.4.157: the peak is the fastest reading a neighbouring
                # reading backs up, the same spike check as the card (see
                # collector.py), so a glitch doesn't hide the aircraft's real
                # peak. An aircraft with no such reading isn't listed. The slice
                # starts a window early so the day's first reading can have a
                # neighbour, as it does on the card. 0.12 s on a 76,000-reading
                # day, against 0.05-0.09 s before.
                agree_sec = _collector_mod.speed_agree_window(CONFIG["receiver"].get("poll_interval"))
                agree_kt = _collector_mod.SPEED_AGREE_KT
                raw = q("""
                    WITH readings AS (
                        SELECT icao, callsign, aircraft_type, speed AS v, seen_at,
                               LAG(speed) OVER w AS prev_v, LAG(seen_at) OVER w AS prev_at,
                               LEAD(speed) OVER w AS next_v, LEAD(seen_at) OVER w AS next_at
                        FROM all_sightings INDEXED BY idx_all_seen_icao
                        WHERE seen_at >= ? AND speed IS NOT NULL
                          AND typeof(speed) IN ('integer', 'real')
                          AND icao NOT LIKE '~%'
                        WINDOW w AS (PARTITION BY icao ORDER BY seen_at)
                    ),
                    r AS (
                        SELECT icao, callsign, aircraft_type, v, seen_at,
                               MAX(v) OVER (PARTITION BY icao) AS peak
                        FROM readings
                        WHERE seen_at >= ?
                          AND ((seen_at - prev_at <= ? AND ABS(v - prev_v) <= ?)
                            OR (next_at - seen_at <= ? AND ABS(v - next_v) <= ?))
                    )
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           peak AS metric,
                           MIN(seen_at) AS seen_at
                    FROM r
                    WHERE v = peak
                    GROUP BY icao, peak
                    ORDER BY metric DESC, icao
                """, (start_ts - agree_sec, start_ts, agree_sec, agree_kt, agree_sec, agree_kt))
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
                # v3.4.143: one pinned pass with a window MIN/MAX (see fastest).
                raw = q("""
                    WITH r AS (
                        SELECT icao, callsign, aircraft_type, speed AS v, seen_at,
                               MIN(speed) OVER (PARTITION BY icao) AS low
                        FROM all_sightings INDEXED BY idx_all_seen_icao
                        WHERE seen_at >= ? AND speed IS NOT NULL
                          AND typeof(speed) IN ('integer', 'real')
                          AND speed >= 40
                          AND icao NOT LIKE '~%'
                    )
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           low AS metric,
                           MIN(seen_at) AS seen_at
                    FROM r
                    WHERE v = low
                    GROUP BY icao, low
                    ORDER BY metric ASC, icao
                """, (start_ts,))
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
                # v3.4.143: one pinned pass with a window MIN/MAX (see fastest).
                raw = q("""
                    WITH r AS (
                        SELECT icao, callsign, aircraft_type, altitude AS v, seen_at,
                               MAX(altitude) OVER (PARTITION BY icao) AS peak
                        FROM all_sightings INDEXED BY idx_all_seen_icao
                        WHERE seen_at >= ? AND altitude IS NOT NULL
                          AND typeof(altitude) IN ('integer', 'real')
                          AND icao NOT LIKE '~%'
                    )
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           peak AS metric,
                           MIN(seen_at) AS seen_at
                    FROM r
                    WHERE v = peak
                    GROUP BY icao, peak
                    ORDER BY metric DESC, icao
                """, (start_ts,))
                rows = [{
                    "icao": r["icao"],
                    "callsign": (r["callsign"] or "").strip(),
                    "aircraft_type": r["aircraft_type"] or "",
                    "metric": _to_number(r["metric"]) or 0,
                    "metric_label": f"{int(_to_number(r['metric']) or 0):,} ft",
                    "seen_at": r["seen_at"],
                } for r in raw if _collector_mod.altitude_is_plausible(
                    r["aircraft_type"], _to_number(r["metric"]) or 0)]

            elif card == "lowest_altitude":
                # Exclude ground (altitude 0 / null / "ground"). typeof filter
                # guards against stringy values in the REAL column. seen_at
                # is the timestamp of the lowest-altitude sighting.
                # v3.4.143: one pinned pass with a window MIN/MAX (see fastest).
                raw = q("""
                    WITH r AS (
                        SELECT icao, callsign, aircraft_type, altitude AS v, seen_at,
                               MIN(altitude) OVER (PARTITION BY icao) AS low
                        FROM all_sightings INDEXED BY idx_all_seen_icao
                        WHERE seen_at >= ? AND altitude IS NOT NULL
                          AND typeof(altitude) IN ('integer', 'real')
                          AND altitude > 0
                          AND icao NOT LIKE '~%'
                    )
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           low AS metric,
                           MIN(seen_at) AS seen_at
                    FROM r
                    WHERE v = low
                    GROUP BY icao, low
                    ORDER BY metric ASC, icao
                """, (start_ts,))
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
                # v3.4.143: index pinned (see fastest); the icao tiebreak
                # spells out the order aircraft sharing a first poll have
                # always come back in.
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           MAX(seen_at) AS last_seen,
                           COUNT(*) AS hits
                    FROM all_sightings INDEXED BY idx_all_seen_icao
                    WHERE seen_at >= ?
                    GROUP BY icao
                    ORDER BY first_seen ASC, icao
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
                            "extra": "peak at " + tzwindow.local_dt(
                                peak_start, tzwindow.resolve_tz(CONFIG)).strftime("%H:%M"),
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
                # v3.4.143: index pinned (see fastest), icao tiebreak as
                # in unique_today.
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM all_sightings INDEXED BY idx_all_seen_icao
                    WHERE seen_at >= ? AND UPPER(aircraft_type) = ?
                    GROUP BY icao
                    ORDER BY first_seen ASC, icao
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
                # v3.4.143: index pinned (see fastest), icao tiebreak as
                # in unique_today.
                raw = q("""
                    SELECT icao,
                           MAX(callsign) AS callsign,
                           MAX(aircraft_type) AS aircraft_type,
                           MIN(seen_at) AS first_seen,
                           COUNT(*) AS hits
                    FROM all_sightings INDEXED BY idx_all_seen_icao
                    WHERE seen_at >= ? AND callsign IS NOT NULL AND callsign != ''
                    GROUP BY icao
                    ORDER BY first_seen ASC, icao
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
                target_token = {label: token for token, label
                                in _CATEGORY_LABELS.items()}.get(value.strip())
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
                # The hour window in the configured timezone, consistent with
                # how the hourly_histogram card bins its data (and right on a
                # clock-change day, where start_ts + hour * 3600 isn't).
                drill_tz = tzwindow.resolve_tz(CONFIG)
                hour_start, hour_end = tzwindow.hour_bounds(
                    tzwindow.local_dt(start_ts, drill_tz).date(), hour, hour, drill_tz)
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
                    from datetime import date as _date
                    # In the configured timezone, like the card's day boundaries.
                    y, m, d = [int(x) for x in value.split("-")]
                    day_start_ts, day_end_ts = tzwindow.day_bounds(
                        _date(y, m, d), tzwindow.resolve_tz(CONFIG))
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
                    # Attach friendly airline name when recognized.
                    # v3.4.35: factored into _attach_airline_name.
                    _attach_airline_name(row, prefix)
                    rows.append(row)
                rows.sort(key=lambda x: -x["aircraft_count"])

            elif card == "all_aircraft":
                # v3.4.37: full top-100 list backing the top_aircraft card's
                # whole-card drill. Same metric as the card (windowed
                # SUM(sighting_count) from sightings_hourly joined with
                # seen_aircraft for display fields), capped at 100 so the
                # panel stays scannable and the query stays bounded — on a
                # busy install the full population would be many thousands
                # of aircraft, more than a drill table can usefully render.
                # Each row is a per-aircraft entry: icao + seen_at on the
                # row makes the existing _drillRowHtml path treat it as a
                # clickable jump to /aircraft/<icao>, the same UX as
                # individual-aircraft drills (furthest_aircraft etc).
                # v3.4.56: hour_bucket is stored in seconds (truncated to
                # the hour), so the window param is start_ts directly. The
                # previous start_ts//3600 passed hours, matching every row
                # and making the drill show all-time instead of the window.
                start_bucket = start_ts
                # Separate count of distinct aircraft in window — used by
                # the panel footer ("Showing top 100 of N aircraft"). The
                # main query's LIMIT 100 would make len(rows) misleading;
                # this gives the honest population number even when the
                # display is capped. One COUNT(DISTINCT icao) on the
                # rollup; same shape as the Tier-4 #6 query so equivalent
                # cost — one-time on drill open, acceptable.
                total_count_row = q(
                    "SELECT COUNT(DISTINCT icao) AS n FROM sightings_hourly "
                    "WHERE hour_bucket >= ?",
                    (start_bucket,),
                )
                _all_aircraft_total = total_count_row[0]["n"] if total_count_row else 0
                ac_rows = q("""
                    SELECT s.icao,
                           s.last_callsign,
                           s.aircraft_type,
                           s.aircraft_type_desc,
                           s.operator,
                           s.registration,
                           s.first_seen_at,
                           s.last_seen_at,
                           SUM(h.sighting_count) AS n,
                           MIN(h.first_seen_at) AS hourly_first,
                           MAX(h.last_seen_at)  AS hourly_last
                    FROM sightings_hourly h
                    JOIN seen_aircraft s ON s.icao = h.icao
                    WHERE h.hour_bucket >= ?
                    GROUP BY h.icao
                    ORDER BY n DESC
                    LIMIT 100
                """, (start_bucket,))
                for r in ac_rows:
                    d = dict(r)
                    d["callsign"] = (d.get("last_callsign") or "").strip()
                    d["sightings"] = d.get("n") or 0
                    d["sightings_fmt"] = f"{d['sightings']:,}"
                    d["first_seen"] = d.get("hourly_first") or d.get("first_seen_at")
                    d["last_seen"]  = d.get("hourly_last")  or d.get("last_seen_at")
                    d["seen_at"] = d["last_seen"]
                    _attach_airline_name(d, d.get("operator"))
                    rows.append(d)
                # Stash the real total on a sentinel field so the response
                # builder below overrides len(rows). See the override at
                # `if all_aircraft_total is not None` further down.
                all_aircraft_total = _all_aircraft_total

            elif card == "most_seen_alltime":
                # Top-100 backing the "Most seen, all time" card's whole-card
                # drill. Same metric as the card — all-time
                # seen_aircraft.sighting_count read directly (no rollup sum),
                # capped at 100. Mirrors the all_aircraft drill's row shape so
                # the existing aircraft_list drill preset + clickable
                # /aircraft/<icao> rows work unchanged. No index (see the card
                # comment in get_stats).
                ac_rows = q("""
                    SELECT icao,
                           last_callsign,
                           aircraft_type,
                           aircraft_type_desc,
                           operator,
                           registration,
                           first_seen_at,
                           last_seen_at,
                           sighting_count AS n
                    FROM seen_aircraft
                    ORDER BY sighting_count DESC
                    LIMIT 100
                """)
                for r in ac_rows:
                    d = dict(r)
                    d["callsign"] = (d.get("last_callsign") or "").strip()
                    d["sightings"] = d.get("n") or 0
                    d["sightings_fmt"] = f"{d['sightings']:,}"
                    d["first_seen"] = d.get("first_seen_at")
                    d["last_seen"]  = d.get("last_seen_at")
                    d["seen_at"] = d["last_seen"]
                    _attach_airline_name(d, d.get("operator"))
                    rows.append(d)
                # Honest population total for the panel footer ("top 100 of N").
                # seen_aircraft is the all-time population (never pruned).
                total_row = q("SELECT COUNT(*) AS n FROM seen_aircraft")
                all_aircraft_total = total_row[0]["n"] if total_row else 0

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
                # v2.41.22: full category distribution.
                #
                # v3.4.143: the category comes from seen_aircraft.category
                # with the card face's own WHERE, so each aircraft_count
                # equals the category_mix card. This drill used to classify
                # today's sightings itself with an inline copy of the pre-v2.89
                # heuristics (military from today's military_sightings, type
                # from today's rows rather than the stored sticky column), so
                # it could already differ from the card, and the gap widened
                # when categorize.py moved on (v3.4.141). sightings /
                # first_seen / last_seen are still today's per-aircraft
                # all_sightings aggregates, summed per category (index
                # pinned — see fastest). Labels come from the shared
                # _CATEGORY_LABELS.
                counts = q("""
                    SELECT category, COUNT(*) AS n
                    FROM seen_aircraft
                    WHERE last_seen_at >= ? AND category IS NOT NULL
                    GROUP BY category
                """, (start_ts,))
                # Today's slice drives a primary-key probe of seen_aircraft
                # (CROSS JOIN fixes that loop order), so no planner
                # statistics can turn it into a scan per aircraft.
                totals = {r["category"]: r for r in q("""
                    SELECT a.category,
                           SUM(t.sightings) AS sightings,
                           MIN(t.first_seen) AS first_seen,
                           MAX(t.last_seen) AS last_seen
                    FROM (
                        SELECT icao, COUNT(*) AS sightings,
                               MIN(seen_at) AS first_seen,
                               MAX(seen_at) AS last_seen
                        FROM all_sightings INDEXED BY idx_all_seen_icao
                        WHERE seen_at >= ?
                        GROUP BY icao
                    ) t
                    CROSS JOIN seen_aircraft a ON a.icao = t.icao
                    WHERE a.last_seen_at >= ? AND a.category IS NOT NULL
                    GROUP BY a.category
                """, (start_ts, start_ts))}
                for c in counts:
                    n, t = c["n"], totals.get(c["category"])
                    first = t["first_seen"] if t else None
                    rows.append({
                        "category": _CATEGORY_LABELS.get(c["category"], c["category"]),
                        "aircraft_count": n,
                        "sightings": t["sightings"] if t else 0,
                        "first_seen": first,
                        "last_seen": t["last_seen"] if t else None,
                        "metric": n,
                        "metric_label": f"{n:,}",
                        "seen_at": first,
                    })
                rows.sort(key=lambda x: -x["aircraft_count"])

            elif card == "all_countries":
                # v2.50.27: full country distribution, not capped at top 5.
                # Row clicks route back through the top_countries drill
                # (handled above). Country is recomputed here from each ICAO
                # with countries.country_for_icao; the top_countries card face
                # (v2.85.2) and its row drill read the stored
                # seen_aircraft.country instead, which the collector writes
                # from the same function on every sighting.
                from countries import country_for_icao
                # v3.4.143: index pinned (see fastest). Reading only
                # (icao, seen_at) lets idx_all_seen_icao cover the scan —
                # the bare callsign / aircraft_type columns were never
                # used. ORDER BY icao keeps countries tied on
                # aircraft_count in the order they have always come back.
                country_raw = q("""
                    SELECT icao,
                           MIN(seen_at) AS first_seen, MAX(seen_at) AS last_seen,
                           COUNT(*) AS sightings
                    FROM all_sightings INDEXED BY idx_all_seen_icao
                    WHERE seen_at >= ?
                    GROUP BY icao
                    ORDER BY icao
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
            # v3.4.37: all_aircraft branch supplies its own real total
            # (true distinct-aircraft count in window) since len(rows)
            # there reflects only the SQL LIMIT cap.
            if all_aircraft_total is not None:
                total_count = all_aircraft_total
            # v2.66.0: cap at 25 unless caller asks for all. The "View
            # all N {types|etc}" button on Composition cards passes
            # all=true; the default panel render uses the cap for budget.
            # v3.4.37: skip the 25-cap for all_aircraft — its SQL LIMIT
            # of 100 IS the intended display size (the parent card asks
            # for top 100 as its drill target), not a budget to be
            # further trimmed.
            if not bypass_cap and card != "all_aircraft":
                rows = rows[:25]

            for r in rows:
                seen_at = r.get("seen_at")
                icao = r.get("icao")
                if seen_at is None or not icao:
                    continue
                # The sighting that set this aircraft's record. Pick the first
                # row at that exact timestamp (most aircraft only have one).
                # v3.4.143: pinned — (seen_at, icao) is an exact seek, where
                # icao alone walks the aircraft's whole history, and some
                # planner statistics chose a full scan here for every row.
                enrich_row = q(
                    """
                    SELECT altitude, speed, lat, lon
                    FROM all_sightings INDEXED BY idx_all_seen_icao
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
    def get_full_config():
        """Return the full current config as JSON. v3.4.168: a saved FlightAware
        key comes back as FA_KEY_KEPT, never in full (lookups are billed to its
        owner); a save carrying FA_KEY_KEPT keeps the stored key."""
        fa = CONFIG.get("flightaware")
        if isinstance(fa, dict) and str(fa.get("api_key") or "").strip():
            out = dict(CONFIG)
            out["flightaware"] = dict(fa, api_key=FA_KEY_KEPT)
            return out
        return CONFIG

    class ConfigPayload(BaseModel):
        config: dict

    @app.put("/api/config")
    def update_full_config(payload: ConfigPayload):
        """Validate and save a new config. Returns which keys changed and whether
        a service restart is required for the changes to fully take effect."""
        from config_validator import validate_config, diff_keys, requires_restart

        new_cfg = payload.config
        fa_new = new_cfg.get("flightaware")
        if isinstance(fa_new, dict) and fa_new.get("api_key") == FA_KEY_KEPT:
            fa_new["api_key"] = str((CONFIG.get("flightaware") or {}).get("api_key") or "")
        errors = validate_config(new_cfg)
        if errors:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "errors": [{"path": p, "message": m} for p, m in errors],
                },
            )

        # v3.3.1: post-validation runtime check — reject configs that
        # would put notifications into a silent-failure state. The
        # scenario: notifications.enabled=true with notifications.url
        # pointing at localhost, but no local ntfy server is installed.
        # The client-side gate disables the checkbox in this state, but
        # a raw API caller (or a stale browser tab) could still POST it.
        # Defense in depth.
        nt = (new_cfg.get("notifications") or {})
        if nt.get("enabled") is True:
            url = (nt.get("url") or "").strip().lower()
            is_local_url = (
                url.startswith("http://localhost") or
                url.startswith("http://127.0.0.1") or
                url.startswith("https://localhost") or
                url.startswith("https://127.0.0.1")
            )
            if is_local_url:
                try:
                    from ntfy_installer import install_status
                    ns = install_status()
                    if ns.get("state") in ("not_installed", "partial"):
                        return JSONResponse(
                            status_code=400,
                            content={
                                "ok": False,
                                "errors": [{
                                    "path": "notifications.enabled",
                                    "message": (
                                        "Local ntfy server is not installed, "
                                        "but notifications.url points at "
                                        "localhost. Notifications would "
                                        "silently fail. Install ntfy via "
                                        "Configuration → Notifications → "
                                        "'Install local ntfy', or change "
                                        "the URL to an external ntfy server."
                                    ),
                                }],
                            },
                        )
                except ImportError:
                    pass  # ntfy_installer unavailable; let it through
                except Exception:
                    pass  # don't block config save on a check failure

        # v3.4.148: settings that only make sense on THIS machine — a listen
        # address that isn't one of its addresses, a database folder that
        # doesn't exist — are refused here instead of failing at the next
        # restart (with the web UI gone). Only keys that changed are checked.
        problems = _machine_key_problems(new_cfg, CONFIG)
        if problems:
            msg = "Some settings don't work on this machine."
            return JSONResponse(status_code=400, content={
                "ok": False, "error": msg, "message": msg,
                "errors": [{"path": p, "message": m} for p, m in problems],
            })

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

        # v3.4.148: persist FIRST (all or nothing; comments kept), then swap
        # the running config — a failed write leaves both the file and the
        # running config exactly as they were, and says why. update() before
        # pop() so a reader never sees a required section missing.
        with config_io.CONFIG_LOCK:
            # v3.4.149: not while a restore waits for its restart — the
            # restart would put the backup's settings over this save.
            waiting = _restore_waiting_response(field_errors=True)
            if waiting is not None:
                return waiting
            try:
                _save_config(new_cfg)
            except (config_io.ConfigWriteError, ValueError) as e:
                logger.error(f"Failed to persist config: {e}")
                msg = str(e)
                if isinstance(e, ValueError):
                    msg = f"Couldn't save config.yaml: {e}. Nothing was changed."
                return JSONResponse(
                    status_code=(507 if getattr(e, "disk_full", False)
                                 else 400 if isinstance(e, ValueError) else 500),
                    content={"ok": False, "error": msg, "message": msg,
                             "errors": [{"path": "", "message": msg}]},
                )
            CONFIG.update(new_cfg)
            for key in list(CONFIG.keys()):
                if key not in new_cfg:
                    CONFIG.pop(key)

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
        .backups/. Each snapshot is a folder holding the previous release's
        files (moved there when an update is confirmed). The folder a
        pending update's journal names is never pruned."""
        try:
            install_dir = _install_dir()
            backups_root = install_dir / ".backups"
            if not backups_root.is_dir():
                return
            _j = updater.pending(install_dir)
            protected = {str(_j.get("ts"))} if _j else set()
            # Only consider top-level directories with timestamp-like names
            snapshots = [
                p for p in backups_root.iterdir()
                if p.is_dir() and re.match(r"^\d{8}-\d{6}$", p.name)
            ]
            # Sort newest first (lexical works because timestamps are YYYYMMDD-HHMMSS)
            snapshots.sort(key=lambda p: p.name, reverse=True)
            for old in snapshots[INSTALL_BACKUP_KEEP:]:
                if old.name in protected:
                    continue
                try:
                    shutil.rmtree(old)
                    logger.info(f"Pruned old install backup: {old.name}")
                except OSError as e:
                    logger.warning(f"Could not delete old install backup {old.name}: {e}")
        except Exception as e:
            logger.warning(f"Could not prune install backups: {e}")

    def _do_restart():
        """Trigger a systemd restart. Returns (ok, note). See _restart_service."""
        return _restart_service()

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
    def list_config_backups():
        """Return all config.yaml.bak.* files with metadata."""
        return {"backups": _list_config_backups()}

    @app.get("/api/config/db-tuning")
    def get_db_tuning_status():
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
    def read_config_backup(name: str):
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
    def export_config():
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

        # v3.4.148: it must also load the way the next start reads it, and
        # its machine-specific settings (listen address, database folder)
        # must work on this machine where they differ from the running ones.
        try:
            config_io.check_loadable(new_text)
        except ValueError as e:
            msg = f"This config couldn't start Aerodrome: {e}."
            return False, {"ok": False, "error": msg, "message": msg}
        problems = _machine_key_problems(parsed, CONFIG)
        if problems:
            msg = "Some settings don't work on this machine."
            return False, {"ok": False, "error": msg, "message": msg,
                           "errors": [{"path": p, "message": m} for p, m in problems]}

        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_name = f"config.yaml.bak.{ts}"
        backup_path = Path(CONFIG_PATH).with_name(backup_name)
        with config_io.CONFIG_LOCK:
            # Back up current config — atomically, so a half-written .bak is
            # never later offered as a restore point (or used by recovery).
            try:
                config_io.write_text(backup_path, Path(CONFIG_PATH).read_text(encoding="utf-8"),
                                     check=False)
            except (OSError, ValueError, config_io.ConfigWriteError) as e:
                msg = f"Could not back up the current config: {e}"
                return False, {"ok": False, "error": msg, "message": msg,
                               "_status": 507 if getattr(e, "disk_full", False) else 500}

            # Write the new config — verbatim (the user's comments kept), all
            # or nothing.
            try:
                config_io.write_text(CONFIG_PATH, new_text, check=False)
            except config_io.ConfigWriteError as e:
                return False, {"ok": False, "error": str(e), "message": str(e),
                               "_status": 507 if e.disk_full else 500}

            # Reload into in-memory CONFIG so subsequent API calls see the
            # new values (update before pop: no reader sees a section missing)
            CONFIG.update(parsed)
            for k in list(CONFIG.keys()):
                if k not in parsed:
                    CONFIG.pop(k, None)

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
    def restore_config_backup(name: str):
        """Replace config.yaml with the contents of a specific backup.
        Backs up the current config first."""
        if not _BACKUP_NAME_RE.match(name):
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid backup name"})
        try:
            with _maintenance("a config restore") as tok:
                src = _install_dir() / name
                if not src.is_file():
                    return JSONResponse(status_code=404, content={"ok": False, "error": "Backup not found"})
                try:
                    text = src.read_text()
                except Exception as e:
                    return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

                ok, payload = _apply_config_from_text(text, f"backup {name}")
                if not ok:
                    return JSONResponse(status_code=payload.pop("_status", 400),
                                        content=payload)

                # Trigger restart via helper (handles --no-block + fallback)
                restart_ok, note = _do_restart()
                if not restart_ok and note:
                    payload["restart_note"] = note
                if restart_ok:
                    _MAINT.park(tok, "restarting after a config restore", _RESTART_HOLD_SEC)

                return payload
        except _Busy as b:
            return _busy_response(b)

    # v3.4.9: helper for size-bounded UploadFile reads. The previous
    # pattern was `raw = await file.read(); if len(raw) > MAX: reject`,
    # which loads the ENTIRE upload into memory before the size check
    # fires — a malicious 100GB upload to a 256KB-limit endpoint
    # allocates 100GB of RAM before rejection. Same class of bug as
    # the v3.4.0 OOM-kill on large restore uploads, just smaller in
    # absolute terms because the endpoints have lower limits.
    #
    # This helper streams the upload in chunks and aborts as soon as
    # cumulative size exceeds max_bytes — a too-large upload allocates
    # at most max_bytes + one chunk, not the full malicious size.
    # Returns (bytes, None) on success or (None, error_message) on
    # size-exceeded / read failure. Caller still handles the response.
    #
    # Use for small bounded uploads where we want the bytes in
    # memory (config.yaml at <256KB, update zip at <50MB). For
    # larger uploads (restore backups up to 2GB), keep using the
    # stream-to-temp-file pattern from /api/backup/import.
    #
    # v3.4.147: synchronous (reads the spooled upload's file object), so the
    # handlers that use it are plain `def` and run in the threadpool — a
    # slow disk or a big upload no longer freezes every other request.
    def _read_upload_bounded(file: UploadFile, max_bytes: int):
        CHUNK = 64 * 1024  # 64 KB — small enough to bound overshoot
        total = 0
        chunks: List[bytes] = []
        try:
            while True:
                chunk = file.file.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None, f"Upload exceeds size limit ({max_bytes} bytes)"
                chunks.append(chunk)
        except Exception as e:
            return None, f"Upload read failed: {e}"
        return b"".join(chunks), None

    @app.post("/api/config/import")
    def import_config(file: UploadFile = File(...)):
        """Upload a user-supplied config.yaml, validate, back up current, replace, restart."""
        try:
            with _maintenance("a config import") as tok:
                # v3.4.9: size-bounded stream-read. Was unbounded `await file.read()`
                # followed by post-hoc len() check, which allocated full upload
                # before rejection. Now aborts as soon as the 256 KB cap is hit.
                raw, err = _read_upload_bounded(file, CONFIG_IMPORT_MAX_BYTES)
                if err is not None:
                    return JSONResponse(status_code=413, content={
                        "ok": False, "error": err, "message": err})
                text = raw.decode("utf-8", errors="replace")
                # v3.4.149: nested YAML aliases expand to billions of nodes
                # once compared or shown — refused (Aerodrome never writes them).
                try:
                    config_io.refuse_aliases(text)
                except ValueError as e:
                    msg = f"This config can't be imported: {e}."
                    return JSONResponse(status_code=400, content={
                        "ok": False, "error": msg, "message": msg})

                ok, payload = _apply_config_from_text(text, f"upload {file.filename!r}")
                if not ok:
                    return JSONResponse(status_code=payload.pop("_status", 400),
                                        content=payload)

                # Trigger restart via helper (handles --no-block + fallback)
                restart_ok, note = _do_restart()
                if not restart_ok and note:
                    payload["restart_note"] = note
                if restart_ok:
                    _MAINT.park(tok, "restarting after a config import", _RESTART_HOLD_SEC)

                return payload
        except _Busy as b:
            return _busy_response(b)

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
    #   - restoring ntfy's server.yml carries over only the settings
    #     Aerodrome manages (address, port, listen address, iPhone relay),
    #     never the file itself (v3.4.146); a missing local ntfy is
    #     installed first (v3.3.1); an external one is never touched

    @app.get("/api/backup/export")
    def backup_export(request: Request):
        """Build a zip of config + DB + ntfy config and send it.

        v3.4.148: only for this page, one at a time, and never leaves a file
        behind. A request another website's page made (Sec-Fetch-Site /
        Origin / Referer say so) gets 403 before anything is built; the UI,
        a typed URL and curl are unaffected. The zip is built in
        .backups/.tmp (0600) under the maintenance lock, opened, and its
        name removed BEFORE the response starts: an abandoned download, a
        dropped connection or a crash can no longer leave a database-sized
        file behind (the space is freed when the response closes the file,
        whatever happens to the client)."""
        import uuid
        refusal = _cross_site_refusal(request)
        if refusal is not None:
            return refusal
        try:
            with _maintenance("a backup download"):
                install_dir = _install_dir()
                need = 2 * _db_size_bytes(install_dir) + UPLOAD_FREE_HEADROOM_BYTES
                tmp_dir = _backup_tmp_dir(install_dir)
                free = shutil.disk_usage(str(tmp_dir)).free
                if free < need:
                    return _no_space_response(need, free, "build this backup")
                tmp_path = tmp_dir / f"export-{uuid.uuid4().hex}.zip"
                try:
                    result = _build_backup_zip(install_dir, str(tmp_path))
                    if not result["ok"]:
                        return JSONResponse(status_code=507 if result.get("disk_full") else 500,
                                            content={**result, "error": result.get("message")})
                    f = open(tmp_path, "rb")
                    size = os.fstat(f.fileno()).st_size
                finally:
                    try:
                        os.unlink(tmp_path)        # the name is gone before we return
                    except OSError:
                        pass
        except _Busy as b:
            return _busy_response(b)
        ts = time.strftime("%Y%m%d-%H%M%S")
        return _open_file_response(f, size, f"aerodrome-backup-{ts}.zip")

    def _build_backup_zip(install_dir, dest_path, progress_callback=None):
        """v3.4.0 shared backup-build logic. Writes the zip incrementally to
        `dest_path` (a filesystem path string). Both /api/backup/export
        (browser download) and /api/backup/create-server-side use this.

        Returns dict: {ok, manifest, bytes, includes, errors}.

        Behavior:
        - Streams writes — the zip never exists in memory in full
        - SQLite DB is copied via online .backup API to a sibling temp
          file, then added to the zip. This temp file is the only
          DB-size buffer; it's deleted after the zip add completes.
        - ntfy/server.yml included only when ntfy is aerodrome_managed
        - manifest.json describes what's inside and when it was built
        - progress_callback (if given) is invoked as
          callback(phase: str, percent: int, bytes_written: int)
        """
        import io, json, zipfile, tempfile, hashlib, sqlite3
        db_path = Path(CONFIG.get("data", {}).get("db_file", "aircraft_history.db"))
        if not db_path.is_absolute():
            db_path = install_dir / db_path

        manifest = {
            "manifest_version": 1,
            "aerodrome_version": (install_dir / "VERSION").read_text().strip()
                if (install_dir / "VERSION").exists() else "unknown",
            "created_at": int(time.time()),
            "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "includes": {
                "config": Path(CONFIG_PATH).is_file(),
                "database": db_path.is_file(),
                "ntfy_config": False,
            },
            "notes": [
                "Restore with POST /api/backup/import (multipart) or "
                "POST /api/backup/restore-from-path (server-side).",
                "The service will restart after restore.",
                "ntfy server.yml is included only when ntfy is aerodrome-managed.",
                "ntfy cache.db is NOT backed up — reinstall ntfy to get a fresh cache.",
            ],
        }

        # ntfy state
        ntfy_config_text = None
        try:
            from ntfy_installer import install_status, CONFIG_FILE as NTFY_CONFIG_FILE
            ns = install_status()
            if ns.get("state") == "aerodrome_managed" and NTFY_CONFIG_FILE.is_file():
                ntfy_config_text = NTFY_CONFIG_FILE.read_text()
                manifest["includes"]["ntfy_config"] = True
                manifest["ntfy_version"] = ns.get("version")
        except Exception as e:
            logger.warning(f"Could not include ntfy config in backup: {e}")

        if progress_callback:
            progress_callback("manifest", 5, 0)

        # Open destination for writing. zipfile.ZipFile streams writes
        # incrementally as we add entries, so the zip never exists
        # fully in memory.
        try:
            # v3.4.148: 0600 — a backup holds the config and the database.
            with atomic_io.open_private(dest_path) as dest_f:
                with zipfile.ZipFile(dest_f, "w",
                                     compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=6) as zf:
                    zf.writestr("manifest.json", json.dumps(manifest, indent=2))
                    if manifest["includes"]["config"]:
                        zf.write(CONFIG_PATH, arcname="config.yaml")
                    if progress_callback:
                        progress_callback("config", 10, os.path.getsize(dest_path))

                    if manifest["includes"]["database"]:
                        # SQLite online backup → temp file → zip it in.
                        # The temp file is the DB-sized intermediate; it
                        # gets removed after zf.write reads it. v3.4.148:
                        # always in .backups/.tmp (0600; swept at startup),
                        # never the system temp dir.
                        _fd, tmp_db_path = tempfile.mkstemp(
                            dir=str(_backup_tmp_dir(install_dir)), prefix="snapshot-",
                            suffix=".db")
                        os.close(_fd)
                        try:
                            if progress_callback:
                                progress_callback("db_snapshot", 15,
                                                  os.path.getsize(dest_path))
                            src = _open_db_conn(str(db_path))
                            dst = _open_db_conn(tmp_db_path)
                            with dst:
                                src.backup(dst)
                            src.close()
                            dst.close()
                            if progress_callback:
                                progress_callback("db_zip", 40,
                                                  os.path.getsize(dest_path))
                            zf.write(tmp_db_path, arcname="aerodrome.db")
                            if progress_callback:
                                progress_callback("db_zipped", 90,
                                                  os.path.getsize(dest_path))
                        finally:
                            for _sfx in ("", "-journal", "-wal", "-shm"):
                                try: os.unlink(tmp_db_path + _sfx)
                                except OSError: pass

                    if ntfy_config_text is not None:
                        zf.writestr("ntfy/server.yml", ntfy_config_text)
                    if (install_dir / "VERSION").exists():
                        zf.write(install_dir / "VERSION", arcname="VERSION")

            size = os.path.getsize(dest_path)
            if progress_callback:
                progress_callback("done", 100, size)
            return {
                "ok": True,
                "manifest": manifest,
                "bytes": size,
                "path": str(dest_path),
            }
        except Exception as e:
            logger.error(f"Backup build failed: {e}")
            try: os.unlink(dest_path)
            except OSError: pass
            full = atomic_io.is_disk_full(e) or "database or disk is full" in str(e)
            msg = ("Not enough free disk space to build the backup. Nothing was kept."
                   if full else str(e))
            return {"ok": False, "message": msg, "disk_full": full}

    @app.get("/api/backup/preview")
    def backup_preview():
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

    async def _one_backup_file(request: Request):
        """v3.4.148: the restore upload's form, parsed with at most ONE file
        part and no other fields. FastAPI's File() parses with Starlette's
        defaults (1000 files and 1000 fields, each up to 1 MB held in
        memory), so one request of many small parts could hold gigabytes of
        RAM within the 2 GB cap. Yields the UploadFile, a message saying
        what was wrong with the form (400), or the 507 response when the
        disk filled while the upload was being received."""
        from starlette.datastructures import UploadFile as _UploadFile
        from starlette.exceptions import HTTPException as _FormError
        problem = None
        try:
            form = await request.form(max_files=1, max_fields=0)
        except _FormError as e:
            problem = f"This upload isn't a single backup file ({e.detail}). Choose one backup zip."
        except Exception as e:
            # As FastAPI's own form parsing did: a body that didn't arrive in
            # full (the user cancelled, or the size gate stopped it) becomes a
            # 400 nobody reads — not an exception whose traceback would keep
            # the partial spool open until some later garbage collection.
            # The disk filling up (from elsewhere) while the upload was being
            # spooled is a 507, as when it fills while the upload is staged.
            problem = (_upload_disk_filled_response() if atomic_io.is_disk_full(e)
                       else "The upload didn't arrive in full. Nothing was changed.")
        if problem is not None:            # (outside the except: nothing kept alive)
            yield problem
            return
        try:
            f = form.get("file")
            yield f if isinstance(f, _UploadFile) else "No backup file was uploaded."
        finally:
            await form.close()

    @app.post("/api/backup/import")
    def backup_import(file=Depends(_one_backup_file), keep_current_config: bool = False,
                      background_tasks: BackgroundTasks = None):
        """Restore from a zip produced by /api/backup/export (see
        _restore_from_local_zip). v3.4.147: one maintenance operation at a
        time; the lock is kept for a while once the restart is scheduled.

        v3.4.149: a plain def (the copy and the checks run in the
        threadpool, never on the event loop). keep_current_config — a query
        parameter, since the form holds exactly one file part: restore the
        database only and keep this machine's settings (the answer to a 422
        about the backup's settings)."""
        if isinstance(file, str):
            return JSONResponse(status_code=400, content={
                "ok": False, "code": "bad_upload", "error": file, "message": file})
        if isinstance(file, JSONResponse):
            return file
        try:
            with _maintenance("a restore", restore_op=True) as tok:
                return _backup_import_locked(file, background_tasks, keep_current_config, tok)
        except _Busy as b:
            return _busy_response(b)

    def _backup_import_locked(file: UploadFile, background_tasks, keep_current_config, tok):
        """Copy the upload to a private file and restore from it.

        v3.4.0: streamed to a file on disk, never buffered in RAM (a
        multi-GB backup OOM-killed Pi-class hosts). v3.4.1: browser uploads
        stop at 2 GB (past that, use the server-side flow: scp the zip into
        .backups/, then restore it from the Existing server-side backups
        list). v3.4.149: the copy is .backups/.tmp/upload-<random>.zip
        (0600, in the 0700 spool folder, swept at startup) and is removed
        when the request ends, whatever the outcome — a refused restore no
        longer leaves it behind."""
        import uuid
        install_dir = _install_dir()
        try:
            tmp_dir = _backup_tmp_dir(install_dir)
        except OSError as e:
            msg = f"Could not create the upload staging folder: {e}"
            return JSONResponse(status_code=500, content={
                "ok": False, "error": msg, "message": msg})
        upload_tmp = tmp_dir / f"upload-{uuid.uuid4().hex}.zip"
        bytes_written = 0
        try:
            try:
                with atomic_io.open_private(upload_tmp) as out:
                    while True:
                        chunk = file.file.read(1024 * 1024)  # 1 MB chunks
                        if not chunk:
                            break
                        out.write(chunk)
                        bytes_written += len(chunk)
                        if bytes_written > BACKUP_UPLOAD_MAX_BYTES:
                            break
            except OSError as e:
                if not atomic_io.is_disk_full(e):
                    raise
                # v3.4.148: the disk filled while the upload was staged.
                return _upload_disk_filled_response()

            if bytes_written > BACKUP_UPLOAD_MAX_BYTES:
                msg = ("Backup is too large for browser upload (>2 GB). "
                       "Above this size, browser uploads become unreliable "
                       "(network timeouts, memory pressure, proxy limits). "
                       "Use the server-side restore flow instead: copy this "
                       "file into <install_dir>/.backups/ via scp or rsync, "
                       "then use the Existing server-side backups list "
                       "below — your backup will appear after clicking "
                       "Rescan. No browser upload needed.")
                return JSONResponse(status_code=413, content={
                    "ok": False, "code": "too_large", "error": msg, "message": msg})

            if bytes_written == 0:
                msg = "Uploaded file is empty"
                return JSONResponse(status_code=400, content={
                    "ok": False, "code": "bad_upload", "error": msg, "message": msg})

            return _restore_from_local_zip(upload_tmp, source_label="upload",
                                           background_tasks=background_tasks,
                                           keep_current_config=keep_current_config, tok=tok)
        except Exception as e:
            logger.error(f"Restore upload failed: {e}", exc_info=True)
            msg = f"Upload failed: {e}"
            return JSONResponse(status_code=500, content={
                "ok": False, "error": msg, "message": msg})
        finally:
            try:
                upload_tmp.unlink()
            except OSError:
                pass

    def _restore_db_path() -> Path:
        """The live database as configured (data.db_file; a relative name is
        in the install). restore_io.prepare resolves it: a data.db_file
        symlinked onto another disk keeps its link — the staged file, the
        copy and the swap are all on the real file's filesystem, and the
        marker records both paths (v3.4.149)."""
        db = Path(CONFIG.get("data", {}).get("db_file", "aircraft_history.db"))
        return db if db.is_absolute() else _install_dir() / db

    def _restore_ntfy_part(zf, prefix, ts, install_dir, restored, skipped, warnings):
        """The backup's notification-server settings (ntfy/server.yml),
        moved here unchanged from the restore of v3.4.146 (B5's rework of
        it plugs in here). v3.4.149: called only once every check of the
        backup passed and the restore is recorded — and it is the one part
        of a restore that takes effect at once, not at the restart: ntfy is
        its own service, and a rollback of Aerodrome's data doesn't undo it."""
        # --- 3. ntfy/server.yml ---
        # v3.4.146: the backup's server.yml is never written as-is. Only the
        # settings Aerodrome manages (address, port, listen address, iPhone
        # relay) are read out of it and written through the same validated
        # paths the Notifications tab uses; anything else is left out and
        # named in a warning (names only, never values). A live file with
        # hand-added settings, or a hand-set listen address Aerodrome can't
        # represent, is left alone and the backup's copy is saved (0600)
        # beside the install for review.
        try:
            # v3.4.9: defense-in-depth size check on zip entry.
            # ntfy server.yml is typically a few hundred bytes to a
            # few KB; 256 KB is enormously generous.
            ntfy_info = zf.getinfo(prefix + "ntfy/server.yml")
            if ntfy_info.file_size > 256 * 1024:
                skipped.append({
                    "file": "ntfy/server.yml",
                    "reason": (f"improbably large ({ntfy_info.file_size} bytes; "
                               f"expected <256 KB); skipped to avoid OOM"),
                })
                raise KeyError("ntfy/server.yml size exceeds limit")
            ntfy_bytes = zf.read(prefix + "ntfy/server.yml")

            def _stash_ntfy_backup():
                # 0600: the backup's server.yml can hold credentials ntfy
                # allows there (tokens, passwords, keys).
                stash_path = install_dir / f"ntfy-server.yml.from-backup.{ts}"
                fd = os.open(stash_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    os.fchmod(f.fileno(), 0o600)    # also an older same-name file
                    f.write(ntfy_bytes)
                return stash_path

            # Only restore if ntfy is actually installed here AND managed
            # by Aerodrome (same gate as export). Otherwise save the bytes
            # to the install dir so the user can manually move them later.
            #
            # v3.3.1: for state == 'not_installed' the backup came from a
            # host with an Aerodrome-managed ntfy, so install it first — the
            # install renders the backup's managed values directly. External
            # or system-managed installs are never taken over.
            try:
                from ntfy_installer import (
                    install_status, install as ntfy_install,
                    update_config as ntfy_update_config, classify_server_yml,
                )
                cls = classify_server_yml(ntfy_bytes.decode("utf-8", "replace"))
                carry_upstream = (None if "upstream-base-url" in cls["problems"]
                                  else cls["upstream"])
                carried = [name for name, value in (("base-url", cls["base_url"]),
                                                    ("listen-http", cls["port"]),
                                                    ("upstream-base-url", carry_upstream))
                           if value is not None]
                ns = install_status()
                fresh = ns.get("state") == "not_installed"
                ntfy_result = None

                if fresh:
                    try:
                        ntfy_result = ntfy_install(
                            port=cls["port"] or 2586,
                            bind=cls["bind"] or "0.0.0.0",
                            base_url=cls["base_url"],          # None → LAN auto-detect
                            upstream_relay=(True if carry_upstream is None
                                            else carry_upstream),
                        )
                    except Exception as e:
                        ntfy_result = {"ok": False, "message": str(e)}
                    if ntfy_result.get("ok"):
                        warnings.append(
                            "Re-installed Aerodrome-managed ntfy server "
                            "(was not present in the new install — restored "
                            "from backup automatically)."
                        )
                    else:
                        stash_path = _stash_ntfy_backup()
                        skipped.append({
                            "file": "ntfy/server.yml",
                            "reason": f"Auto-install of ntfy failed: "
                                      f"{ntfy_result.get('message') or 'unknown error'}. "
                                      f"Saved to {stash_path.name}; install ntfy "
                                      f"manually via the Notifications tab if you "
                                      f"want notifications restored.",
                        })
                elif ns.get("state") == "aerodrome_managed":
                    # None for a value the backup couldn't carry keeps the
                    # current one; hand-added settings in the LIVE file stop
                    # the write (custom_keys) rather than being dropped, and
                    # so does a hand-set listen address Aerodrome can't
                    # represent (stored_value_invalid).
                    ntfy_result = ntfy_update_config(
                        base_url=cls["base_url"], upstream_relay=carry_upstream,
                        port=cls["port"], bind=cls["bind"], drop_custom_keys=None)
                    if not ntfy_result.get("ok"):
                        stash_path = _stash_ntfy_backup()
                        if ntfy_result.get("code") == "custom_keys":
                            reason = (
                                "Your current notification-server config has settings "
                                "Aerodrome doesn't manage "
                                f"({', '.join(ntfy_result.get('custom_keys') or [])}), "
                                "so it was left unchanged. The backup's copy is saved "
                                f"as {stash_path.name} for review.")
                        else:
                            reason = (f"{ntfy_result.get('message') or 'Update failed.'} "
                                      f"The backup's copy is saved as {stash_path.name}.")
                        skipped.append({"file": "ntfy/server.yml", "reason": reason})
                else:
                    # External or system-managed ntfy — never take over
                    stash_path = _stash_ntfy_backup()
                    skipped.append({
                        "file": "ntfy/server.yml",
                        "reason": f"ntfy is not Aerodrome-managed on this system "
                                  f"(state={ns.get('state')!r}). Saved to "
                                  f"{stash_path.name} for manual review.",
                    })

                # "written": server.yml now holds the backup's managed values
                # even though a later step (the restart) failed — skipped[]
                # says what failed; these warnings still say what was left out.
                if ntfy_result is not None and (ntfy_result.get("ok")
                                                or ntfy_result.get("written")):
                    if ntfy_result.get("ok"):
                        restored["ntfy_config"] = {"ok": True, "bytes": len(ntfy_bytes),
                                                   "carried": carried}
                    if cls["custom"]:
                        warnings.append(
                            "Left out notification-server settings Aerodrome "
                            "doesn't manage: " + ", ".join(cls["custom"]))
                    if "base-url" in cls["problems"]:
                        warnings.append(
                            "The backup had no usable notification server address, so "
                            + ("it was detected automatically." if fresh
                               else "the current one was kept."))
                    else:
                        warnings.append(
                            "ntfy base_url in the restored config may reference the OLD "
                            "server's LAN IP. Check it in the Notifications tab if your "
                            "phones can't reach ntfy after restore."
                        )
                    if "listen-http" in cls["problems"]:
                        warnings.append(
                            "The backup's notification server port wasn't usable, so "
                            + ("the default (2586) was used." if fresh
                               else "the current one was kept."))
            except ImportError:
                skipped.append({"file": "ntfy/server.yml",
                                "reason": "ntfy_installer module unavailable"})
            except Exception as e:
                skipped.append({"file": "ntfy/server.yml", "reason": f"error: {e}"})
        except KeyError:
            pass  # not in backup, not a problem

    def _prepare_restore(zip_path, source_label, keep_current_config):
        """The blocking half of a restore (threadpool, maintenance lock held):
        open the zip; check the settings (or keep this machine's); check
        the disk space on the database's filesystem; stage the database
        beside the live one and check it; stage the settings; keep
        pre-restore copies of the live database and config.yaml; record the
        restore (the marker); then the notification server's part. Raises
        restore_io.RestoreError with nothing changed and everything it made
        removed. The live database, config.yaml and the running settings are
        never touched here — the swap is the next start's
        (restore_io.apply_pending_restore)."""
        from config_validator import validate_config
        from schema_migrations import CURRENT_SCHEMA_VERSION
        install_dir = _install_dir()
        db_path = _restore_db_path()
        cfg_path = Path(CONFIG_PATH)
        ts = time.strftime("%Y%m%d-%H%M%S")
        restored: Dict[str, Any] = {}
        skipped: List[Dict[str, Any]] = []
        warnings: List[str] = []
        notes: List[str] = []

        zf, prefix, manifest = restore_io.open_backup(zip_path)
        marker = None
        try:
            if prefix:
                # v2.49.8: cloud sync tools re-package a backup under a folder
                # named after it; its entries are read through that prefix.
                logger.info(f"Backup archive has wrapper folder prefix: {prefix!r}")
            db_info = restore_io.member(zf, prefix + "aerodrome.db")
            cfg_info = restore_io.member(zf, prefix + "config.yaml")
            ntfy_info = restore_io.member(zf, prefix + "ntfy/server.yml")

            # 1. settings — checked the way startup reads them, this machine's
            #    address, port, database and log locations kept (Decision 4)
            cfg_text = None
            if keep_current_config:
                if cfg_info is not None:
                    skipped.append({"file": "config.yaml",
                                    "reason": "kept this machine's settings (database-only "
                                              "restore)"})
                # A database-only restore keeps this machine's notification
                # server as well: it never installs, widens or re-points it.
                if ntfy_info is not None:
                    skipped.append({"file": "ntfy/server.yml",
                                    "reason": "kept this machine's notification server "
                                              "(database-only restore)"})
                    ntfy_info = None
            elif cfg_info is None:
                skipped.append({"file": "config.yaml", "reason": "not in backup"})
            else:
                try:
                    raw = restore_io.read_config_member(zf, cfg_info)
                    cfg_text, cfg_notes = restore_io.prepare_config(
                        raw, CONFIG, validate_config, hosts_of=_configured_hosts)
                except restore_io.RestoreError as e:
                    e.database_only = db_info is not None
                    raise
                notes += cfg_notes
            if db_info is None:
                skipped.append({"file": "aerodrome.db", "reason": "not in backup"})

            if cfg_text is None and db_info is None:
                if keep_current_config:
                    raise restore_io.RestoreError(
                        "Nothing to restore: a database-only restore needs a database, and "
                        "this backup holds none. Nothing was changed.",
                        code="nothing_to_restore")
                if ntfy_info is None:
                    raise restore_io.RestoreError(
                        "Nothing to restore: this backup holds neither settings nor a "
                        "database. Nothing was changed.", code="nothing_to_restore")
                # Only the notification server's settings: they apply now,
                # and there is nothing for a restart to switch.
                _run_ntfy_part(zf, prefix, ts, install_dir, restored, skipped, warnings)
                return {"manifest": manifest, "restored": restored, "skipped": skipped,
                        "warnings": warnings, "notes": notes, "staged": False}

            # 2-7. space, stage + check the database, stage the settings, the
            #      pre-restore copies, the marker (restore_io.prepare)
            marker, info = restore_io.prepare(
                install_dir, db_live=db_path, config_live=cfg_path, source=source_label,
                manifest=manifest, notes=notes, cfg_text=cfg_text,
                db_writer=((lambda dest, mode: restore_io.stage_database(zf, db_info, dest, mode))
                           if db_info is not None else None),
                db_size=db_info.file_size if db_info is not None else 0,
                current_schema=CURRENT_SCHEMA_VERSION,
                keep_current_config=keep_current_config, ts=ts)
            restored.update(info)
            # 8. the notification server — after everything above passed
            #    (not for a database-only restore: skipped above)
            if ntfy_info is not None:
                n_warn = len(warnings)
                _run_ntfy_part(zf, prefix, ts, install_dir, restored, skipped, warnings)
                # (the part adds a warning whenever it wrote or installed
                # something) — a cancel or an expiry must then not say
                # "nothing was changed"
                if restored.get("ntfy_config") or len(warnings) > n_warn:
                    restore_io.note_ntfy_applied(install_dir)
        finally:
            zf.close()

        # v2.50.6: trim older pre-restore copies so they don't accumulate
        # (never the pair this restore just made, nor any the marker names).
        try:
            pruned_count, pruned_bytes = _prune_pre_restore_snapshots()
            if pruned_count:
                logger.info(f"Pruned {pruned_count} older pre-restore snapshot(s), "
                            f"freed {pruned_bytes} bytes")
        except Exception as e:
            logger.warning(f"Could not prune older pre-restore snapshots: {e}")
        return {"manifest": manifest, "restored": restored, "skipped": skipped,
                "warnings": warnings, "notes": notes, "staged": True, "marker": marker}

    def _run_ntfy_part(zf, prefix, ts, install_dir, restored, skipped, warnings):
        """_restore_ntfy_part, which must never fail a restore that is
        already recorded: anything it raises becomes a `skipped` line."""
        try:
            _restore_ntfy_part(zf, prefix, ts, install_dir, restored, skipped, warnings)
        except Exception as e:
            logger.warning(f"Restoring the notification server's settings failed: {e}")
            skipped.append({"file": "ntfy/server.yml", "reason": f"error: {e}"})

    def _restart_after_restore(tok, delay_seconds=0.75):
        """The restart that switches to a staged restore, run by
        BackgroundTasks AFTER the response is sent (v3.4.2: otherwise
        systemd's SIGTERM could land before the response left the kernel's
        send buffer, and the page reported a failed restore that had
        worked), after a short pause for slow links. v3.4.149: when the
        restart is refused (e.g. no sudo rule), the marker records why — the
        Settings page shows the restore as staged and waiting, with Cancel —
        and the maintenance lock is freed at once."""
        time.sleep(delay_seconds)
        ok, note = _do_restart()
        if not ok:
            note = note or "the restart call failed"
            logger.warning(f"Aerodrome couldn't restart itself after a restore ({note}). "
                           f"The restore is staged and applies at the next start within the "
                           f"hour, or can be cancelled in Settings.")
            restore_io.note_restart_failed(_install_dir(), note)
            _MAINT.unpark(tok)

    _DATABASE_ONLY_HINT = ("You can restore just the database and keep this machine's "
                           "settings.")

    def _restore_refused(e, source_label):
        logger.warning(f"Restore refused ({source_label}): {e.message}")
        body = e.payload()
        if e.status == 422 and getattr(e, "database_only", False):
            body["database_only"] = True
            if _DATABASE_ONLY_HINT not in body["message"]:
                body["message"] = body["error"] = f"{body['message']} {_DATABASE_ONLY_HINT}"
        return JSONResponse(status_code=e.status, content=body)

    def _restore_staged_response(outcome, background_tasks, tok, message=None):
        """The answer to a restore that passed every check: restart to switch
        to it (after the response, when BackgroundTasks is there), keep the
        maintenance lock meanwhile."""
        base = {"manifest": outcome.get("manifest"), "restored": outcome.get("restored") or {},
                "skipped": outcome.get("skipped") or [],
                "warnings": outcome.get("warnings") or [], "notes": outcome.get("notes") or []}
        if not outcome.get("staged"):
            msg = ("The backup held only the notification server's settings; they were "
                   "restored. It had no Aerodrome settings or database, so nothing else "
                   "changed and no restart is needed.")
            return {"ok": True, "staged": False, "message": msg, "restart_note": None, **base}
        restart_note = None
        if background_tasks is not None:
            background_tasks.add_task(_restart_after_restore, tok)
            restart_ok = True
        else:
            restart_ok, note = _do_restart()
            if not restart_ok:
                note = note or "the restart call failed"
                restore_io.note_restart_failed(_install_dir(), note)
                restart_note = (f"Aerodrome couldn't restart itself ({note}). The restore is "
                                f"staged and applies the next time Aerodrome starts within the "
                                f"hour — or cancel it in Settings.")
        if restart_ok and tok is not None:
            _MAINT.park(tok, "restarting after a restore", _RESTART_HOLD_SEC)
        msg = message or ("Backup checked — Aerodrome is restarting to switch to it. If the "
                          "restored data won't load, your current data is put back "
                          "automatically.")
        return {"ok": True, "staged": True, "changed": False, "message": msg,
                "restart_note": restart_note, **base}

    def _restore_from_local_zip(zip_path, source_label="local", background_tasks=None,
                                keep_current_config=False, tok=None):
        """v3.4.0 shared restore logic for /api/backup/import (the upload,
        copied to disk first) and /api/backup/restore-from-path (a backup
        already on disk). v3.4.149: the backup is checked and staged
        (_prepare_restore); nothing live changes until the restart, which
        switches to it — or puts the current data back if it won't load.
        A refusal (damaged backup, full disk, settings that wouldn't work,
        a backup from a newer Aerodrome) changes nothing and restarts
        nothing. Runs in the threadpool, maintenance lock (tok) held."""
        try:
            outcome = _prepare_restore(zip_path, source_label, keep_current_config)
        except restore_io.RestoreError as e:
            return _restore_refused(e, source_label)
        except Exception as e:
            logger.error(f"Restore failed before anything was changed: {e}", exc_info=True)
            msg = f"The restore failed before anything was changed: {e}"
            return JSONResponse(status_code=500, content={
                "ok": False, "code": "restore_failed", "error": msg, "message": msg,
                "changed": False})
        return _restore_staged_response(outcome, background_tasks, tok)

    # --- v3.4.0: server-side backup + restore ---
    # The browser-mediated /api/backup/export and /api/backup/import
    # paths work fine for small databases but stop being practical
    # above a few GB — the browser becomes the bottleneck, not the
    # disk. Server-side backup writes the zip directly to disk on
    # the host, then the admin moves it off via scp/rsync out-of-band.
    # Restore-from-path runs against a file already on disk; no upload.
    # The zip format is identical to /api/backup/export so a backup
    # made one way restores the other way (just scp it into place
    # first if it's a server-side backup).

    # Concurrency: only one server-side backup may run at a time.
    # The SQLite online backup API holds a read snapshot for the
    # duration; running two backups concurrently would double the
    # disk-space requirement and the CPU load with no upside.
    # Tracked via a module-level dict so progress queries can find it.
    _server_side_backup_state = {"running": False, "phase": None,
                                  "percent": 0, "bytes_written": 0,
                                  "path": None, "started_at": None}

    @app.post("/api/backup/create-server-side")
    def create_server_side_backup():
        """v3.4.0: write a backup zip directly to <install_dir>/.backups/
        without round-tripping through the browser. Returns when the
        backup is complete (synchronous). For long-running backups,
        poll /api/backup/create-server-side/progress to see status.

        v3.4.147: concurrency is the maintenance lock (the old running-flag
        check-then-set let two backups in the same second both start and
        write the same zip). _server_side_backup_state is progress data only.
        """
        try:
            with _maintenance("a server-side backup"):
                return _create_server_side_backup_locked()
        except _Busy as b:
            if b.busy.get("op") == "a server-side backup":
                # Same body as before the lock existed, plus `busy`.
                return _busy_response(b, message="A server-side backup is already running.",
                                      progress=dict(_server_side_backup_state))
            return _busy_response(b)

    def _create_server_side_backup_locked():
        """v3.4.148: the zip is built as .backups/.tmp/aerodrome-backup-<ts>
        .zip.partial (0600) and only renamed into .backups/ once it is
        complete and hashed — a full disk, a crash or a kill never leaves a
        cut-short zip in the backup list (the next start clears the
        partial). The space check counts the database snapshot AND the zip
        (2x the database + 256 MB)."""
        import hashlib
        install_dir = _install_dir()
        backups_dir = install_dir / ".backups"
        try:
            atomic_io.ensure_private_dir(backups_dir)
            tmp_dir = _backup_tmp_dir(install_dir)
        except OSError as e:
            msg = f"Could not create the .backups folder: {e}"
            return JSONResponse(status_code=500, content={
                "ok": False, "error": msg, "message": msg})

        need_bytes = 2 * _db_size_bytes(install_dir) + UPLOAD_FREE_HEADROOM_BYTES
        free_bytes = shutil.disk_usage(str(backups_dir)).free
        if free_bytes < need_bytes:
            return _no_space_response(need_bytes, free_bytes, "build this backup")

        ts = time.strftime("%Y%m%d-%H%M%S")
        partial = tmp_dir / f"aerodrome-backup-{ts}.zip.partial"
        dest_path = backups_dir / f"aerodrome-backup-{ts}.zip"

        _server_side_backup_state.update({
            "running": True, "phase": "starting", "percent": 0,
            "bytes_written": 0, "path": str(dest_path),
            "started_at": int(time.time()),
        })

        def _progress(phase, percent, bytes_written):
            _server_side_backup_state.update({
                "phase": phase, "percent": percent,
                "bytes_written": bytes_written,
            })

        started = time.time()
        try:
            try:
                try:
                    os.unlink(partial)     # a stale one (same second); it's created afresh
                except FileNotFoundError:
                    pass
                result = _build_backup_zip(install_dir, str(partial),
                                           progress_callback=_progress)
                if not result.get("ok"):
                    return JSONResponse(status_code=507 if result.get("disk_full") else 500,
                                        content={**result, "error": result.get("message")})
                # The sidecar hash is computed on the finished partial, so the
                # listed zip and its hash always match.
                h = hashlib.sha256()
                with open(partial, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                sha_hex = h.hexdigest()
                n = 1
                while os.path.lexists(dest_path):      # same second as another one
                    n += 1
                    dest_path = backups_dir / f"aerodrome-backup-{ts}-{n}.zip"
                os.replace(partial, dest_path)
                atomic_io.fsync_dir(backups_dir)
            finally:
                _server_side_backup_state["running"] = False
                try:
                    os.unlink(partial)
                except OSError:
                    pass
        except OSError as e:
            msg = ("Not enough free disk space to build the backup. Nothing was kept."
                   if atomic_io.is_disk_full(e) else f"Backup failed: {e}")
            return JSONResponse(status_code=507 if atomic_io.is_disk_full(e) else 500,
                                content={"ok": False, "error": msg, "message": msg})

        # Cache the sha256 as a sidecar file so list operations don't have
        # to re-hash multi-GB zips.
        sha_path = dest_path.with_name(dest_path.name + ".sha256")
        try:
            atomic_io.write_atomic(sha_path, f"{sha_hex}  {dest_path.name}\n")
        except OSError as e:
            logger.warning(f"sha256 sidecar write failed: {e}")
        _server_side_backup_state["path"] = str(dest_path)

        duration = time.time() - started
        return {
            "ok": True,
            "path": str(dest_path),
            "filename": dest_path.name,
            "bytes": result["bytes"],
            "sha256": sha_hex,
            "duration_seconds": round(duration, 2),
            "manifest": result["manifest"],
        }

    @app.get("/api/backup/create-server-side/progress")
    def server_side_backup_progress():
        """Poll endpoint for the running server-side backup. Returns
        current phase + percent + bytes_written, or running=false when
        nothing is in flight."""
        return {"ok": True, **dict(_server_side_backup_state)}

    @app.get("/api/backup/list-server-side")
    def list_server_side_backups():
        """List user-initiated server-side backups under
        <install_dir>/.backups/. Does NOT include the auto-managed
        .pre-restore.* snapshots — those have their own endpoint."""
        install_dir = Path(__file__).parent
        backups_dir = install_dir / ".backups"
        if not backups_dir.is_dir():
            return {"ok": True, "backups": []}

        items = []
        for p in sorted(backups_dir.glob("aerodrome-backup-*.zip")):
            if not p.is_file():
                continue
            sha_path = p.with_suffix(p.suffix + ".sha256")
            sha_hex = None
            if sha_path.is_file():
                try:
                    # Sidecar format is "<sha256>  <filename>\n"
                    sha_hex = sha_path.read_text().split()[0]
                except Exception:
                    pass
            try:
                stat = p.stat()
                items.append({
                    "filename": p.name,
                    "path": str(p),
                    "bytes": stat.st_size,
                    "mtime": int(stat.st_mtime),
                    "mtime_iso": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
                    "sha256": sha_hex,
                })
            except OSError:
                pass

        # Newest first
        items.sort(key=lambda x: x["mtime"], reverse=True)
        total_bytes = sum(i["bytes"] for i in items)
        return {"ok": True, "backups": items, "total_bytes": total_bytes,
                "count": len(items)}

    class RestoreFromPathPayload(BaseModel):
        path: str
        keep_current_config: bool = False

    @app.post("/api/backup/restore-from-path")
    def restore_from_path(payload: RestoreFromPathPayload,
                          background_tasks: BackgroundTasks = None):
        """Restore from a backup file already on disk (see
        _restore_from_path_locked). v3.4.147: one maintenance operation at a
        time; the lock is kept for a while once the restart is scheduled.
        v3.4.149: a plain def (threadpool); keep_current_config restores the
        database only."""
        try:
            with _maintenance("a restore", restore_op=True) as tok:
                return _restore_from_path_locked(payload, background_tasks, tok)
        except _Busy as b:
            return _busy_response(b)

    def _restore_from_path_locked(payload, background_tasks, tok):
        """v3.4.0: restore from a backup file already on disk. Useful
        when the file is too large for the browser-upload path. The
        path must be within <install_dir>/.backups/ (scp the zip there
        first) — but never one of Aerodrome's own temporary files in
        .backups/.tmp or .backups/.uploads (v3.4.149).

        Source file is NOT deleted on success — the user explicitly
        chose to keep this backup on disk by using server-side
        restore. Cleanup is via DELETE /api/backup/server-side/<name>.
        """
        install_dir = _install_dir()
        backups_dir = (install_dir / ".backups").resolve()

        try:
            src = Path(payload.path).resolve(strict=False)
        except Exception as e:
            msg = f"Invalid path: {e}"
            return JSONResponse(status_code=400, content={
                "ok": False, "error": msg, "message": msg})

        # Default scoping: must be within .backups/. This blocks the
        # naive "user pastes /etc/passwd" footgun. Admin can drop the
        # backup zip into .backups/ to restore from a remote source.
        # v3.4.149: checked BEFORE whether the file exists, so the answer
        # (403 vs 404) never says which files exist elsewhere on the box.
        try:
            src.relative_to(backups_dir)
            within_scope = True
        except ValueError:
            within_scope = False

        if not within_scope:
            msg = (f"Path is outside the allowed restore scope "
                   f"({backups_dir}). Move the backup zip into "
                   f".backups/ first (e.g. via scp), then retry.")
            return JSONResponse(status_code=403, content={
                "ok": False, "error": msg, "message": msg})

        for sub in (_BACKUP_TMP_NAME, ".uploads"):
            try:
                src.relative_to(backups_dir / sub)
            except ValueError:
                continue
            msg = ("That file is one of Aerodrome's own temporary files, not a backup. "
                   "Pick a backup from the Existing server-side backups list.")
            return JSONResponse(status_code=403, content={
                "ok": False, "code": "not_a_backup", "error": msg, "message": msg})

        if not src.is_file():
            msg = f"Backup file not found: {src}"
            return JSONResponse(status_code=404, content={
                "ok": False, "error": msg, "message": msg})

        return _restore_from_local_zip(
            src, source_label="server-side-path", background_tasks=background_tasks,
            keep_current_config=payload.keep_current_config, tok=tok)

    @app.post("/api/backup/staged-restore/cancel")
    def cancel_staged_restore():
        """v3.4.149: cancel a restore that is staged and waits for its
        restart (e.g. Aerodrome couldn't restart itself): the staged files
        and the marker go, nothing live was changed — the pre-restore copies
        it took stay listed. The way out, so it runs while a restore waits;
        409 while another maintenance operation runs (or the restart is
        under way)."""
        try:
            with _maintenance("a restore cancel", allow_restore_pending=True):
                try:
                    m = restore_io.cancel_pending_restore(_install_dir(), log=logger.info)
                except ValueError:
                    msg = ("This restore is already being switched in, so it can't be "
                           "cancelled.")
                    return JSONResponse(status_code=409, content={
                        "ok": False, "code": "restore_switching", "error": msg, "message": msg})
                except OSError as e:
                    msg = f"Could not cancel the restore: {e}"
                    return JSONResponse(status_code=507 if atomic_io.is_disk_full(e) else 500,
                                        content={"ok": False, "error": msg, "message": msg})
                if m is None:
                    return {"ok": True, "cancelled": False,
                            "message": "No restore is waiting for a restart."}
                return {"ok": True, "cancelled": True,
                        "ntfy_applied": bool(m.get("ntfy_applied")),
                        "message": "The staged restore was cancelled. "
                                   + (restore_io.NTFY_KEPT_NOTE if m.get("ntfy_applied")
                                      else "Nothing was changed.")}
        except _Busy as b:
            return _busy_response(b)

    @app.post("/api/backup/pre-restore/{ts}/restore")
    def put_back_pre_restore(ts: str, keep_current_config: bool = False,
                             background_tasks: BackgroundTasks = None):
        """v3.4.149 (Decision 5a): "Put this back" — switch back to a pair of
        pre-restore copies (the database and config.yaml as they were
        before an earlier restore). It is a restore like any other: the copy
        is checked, staged and switched in at the restart (this machine's
        address, port and database location kept), a copy of the CURRENT
        data is kept first (so this can be undone the same way), and the
        copy put back stays listed. keep_current_config: the database only."""
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", ts or ""):
            msg = "Invalid snapshot name."
            return JSONResponse(status_code=400, content={
                "ok": False, "error": msg, "message": msg})
        try:
            with _maintenance("a restore", restore_op=True) as tok:
                return _put_back_locked(ts, keep_current_config, background_tasks, tok)
        except _Busy as b:
            return _busy_response(b)

    def _put_back_locked(ts, keep_current_config, background_tasks, tok):
        from config_validator import validate_config
        from schema_migrations import CURRENT_SCHEMA_VERSION
        install_dir = _install_dir()
        db_path = _restore_db_path()
        cfg_path = Path(CONFIG_PATH)
        # the copies sit beside the REAL database (data.db_file may be a
        # link; a release before 3.4.149 made them beside the link)
        db_snap = _pre_restore_db_copy(ts)
        cfg_snap = restore_io.config_snapshot_path(cfg_path, ts)
        has_db, has_cfg = db_snap.is_file(), cfg_snap.is_file()
        if not has_db and not has_cfg:
            msg = f"There is no pre-restore copy from {ts}."
            return JSONResponse(status_code=404, content={
                "ok": False, "code": "not_found", "error": msg, "message": msg})
        try:
            when = time.mktime(time.strptime(ts, "%Y%m%d-%H%M%S"))
            when_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
        except (ValueError, OverflowError):
            when_iso = None
        skipped: List[Dict[str, Any]] = []
        notes: List[str] = []
        label = f"pre-restore {ts}"
        try:
            cfg_text = None
            if has_cfg and keep_current_config:
                skipped.append({"file": cfg_snap.name,
                                "reason": "kept this machine's settings (database-only)"})
            elif has_cfg:
                try:
                    if cfg_snap.stat().st_size > restore_io.MAX_CONFIG_BYTES:
                        raise restore_io.RestoreError(
                            f"{cfg_snap.name} is improbably large, so it wasn't read. "
                            "Nothing was changed.", 422)
                    try:
                        raw = cfg_snap.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as e:
                        raise restore_io.RestoreError(f"{cfg_snap.name} can't be read ({e}). "
                                                      "Nothing was changed.", 422)
                    cfg_text, cfg_notes = restore_io.prepare_config(
                        raw, CONFIG, validate_config, hosts_of=_configured_hosts)
                except restore_io.RestoreError as e:
                    e.database_only = has_db
                    raise
                notes += cfg_notes
            elif has_db and not keep_current_config:
                # The settings half of this pair is gone (deleted by hand, or
                # pruned): only the database is put back — say so.
                skipped.append({"file": cfg_snap.name,
                                "reason": "no settings copy from then; this machine's "
                                          "settings are kept"})
            if not has_db:
                skipped.append({"file": db_snap.name, "reason": "no database copy from then"})
            marker, info = restore_io.prepare(
                install_dir, db_live=db_path, config_live=cfg_path, source=label,
                manifest={"created_at_iso": when_iso}, notes=notes, cfg_text=cfg_text,
                db_writer=((lambda dest, mode: restore_io.copy_database(db_snap, dest, mode))
                           if has_db else None),
                db_size=db_snap.stat().st_size if has_db else 0,
                current_schema=CURRENT_SCHEMA_VERSION,
                keep_current_config=keep_current_config, kind="put_back")
        except restore_io.RestoreError as e:
            return _restore_refused(e, label)
        except Exception as e:
            logger.error(f"Put-back failed before anything was changed: {e}", exc_info=True)
            msg = f"Putting the copy back failed before anything was changed: {e}"
            return JSONResponse(status_code=500, content={
                "ok": False, "code": "restore_failed", "error": msg, "message": msg,
                "changed": False})
        return _restore_staged_response(
            {"manifest": {"created_at_iso": when_iso}, "restored": info, "skipped": skipped,
             "warnings": [], "notes": notes, "staged": True},
            background_tasks, tok,
            message=("The copy from before that restore was checked — Aerodrome is "
                     "restarting to put it back. A copy of your current data was kept "
                     "first, so this can be undone the same way."))

    @app.delete("/api/backup/server-side/{filename}")
    def delete_server_side_backup(filename: str):
        """v3.4.0: delete a named server-side backup and its sha256
        sidecar. Filename is validated to be a bare basename within
        <install_dir>/.backups/ — no path traversal. v3.4.147: never while
        another maintenance operation (e.g. a restore reading it) runs."""
        try:
            with _maintenance("a backup delete"):
                return _delete_server_side_backup_locked(filename)
        except _Busy as b:
            return _busy_response(b)

    def _delete_server_side_backup_locked(filename: str):
        install_dir = _install_dir()
        backups_dir = (install_dir / ".backups").resolve()

        # Validate: filename must be a bare basename, must match the
        # aerodrome-backup-*.zip pattern, no separators or .. tricks.
        if (("/" in filename) or ("\\" in filename) or (".." in filename)
                or not filename.startswith("aerodrome-backup-")
                or not filename.endswith(".zip")):
            return JSONResponse(status_code=400, content={
                "ok": False, "message": "Invalid filename"
            })

        target = (backups_dir / filename).resolve()
        try:
            target.relative_to(backups_dir)
        except ValueError:
            return JSONResponse(status_code=400, content={
                "ok": False, "message": "Path traversal blocked"
            })

        if not target.is_file():
            return JSONResponse(status_code=404, content={
                "ok": False, "message": f"Backup not found: {filename}"
            })

        try:
            target.unlink()
            sidecar = target.with_suffix(target.suffix + ".sha256")
            if sidecar.is_file():
                sidecar.unlink()
            return {"ok": True, "deleted": filename}
        except OSError as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "message": f"Delete failed: {e}",
            })

    # --- Pre-restore safety snapshots (v2.50.6) ---
    # Each restore keeps a copy of the previous config.yaml beside
    # config.yaml and of the database beside the database (each on its own
    # file's filesystem; v3.4.149), named <file>.bak.<ts>.pre-restore. A
    # restore whose data won't load is rolled back from them, and "Put this
    # back" stages a pair as a restore (Decision 5a). Auto-pruning keeps the
    # most recent _PRE_RESTORE_KEEP per kind — never the pair a waiting
    # restore needs; these endpoints list, put back and purge them.

    @app.get("/api/backup/pre-restore")
    def list_pre_restore():
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
    def purge_pre_restore():
        """Delete every pre-restore snapshot file. Useful when the keep-N
        retention (default 3) is more conservative than the user wants and
        they'd rather reclaim every byte. Future restores will recreate
        the most recent snapshot anyway. v3.4.147: never while another
        maintenance operation (e.g. a restore) runs."""
        try:
            with _maintenance("a pre-restore cleanup"):
                deleted, freed = _purge_all_pre_restore_snapshots()
            return {
                "ok": True,
                "deleted_count": deleted,
                "freed_bytes": freed,
                "message": f"Purged {deleted} pre-restore file(s), "
                           f"freed {freed} bytes.",
            }
        except _Busy as b:
            return _busy_response(b)
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

    # Table sizes from the last run that asked for them (?sizes=true), so a
    # normal run can show when they were last measured instead of paying for
    # them again. One per app instance, like the other per-app state.
    _perf_table_sizes: Dict[str, Any] = {}

    @app.get("/api/perf/diagnostics")
    def perf_diagnostics(include_legacy: bool = False, sizes: bool = False):
        """Performance diagnostic snapshot. Returns storage footprint,
        query timings, index coverage, and system context.

        v3.4.171: sizes query parameter (default False) controls the two
        dbstat passes that produce per-table size_bytes. dbstat reads every
        page of the database file — on a multi-GB install that is most of a
        30-second run, and in doing so it pushes the rest of the database out
        of the page cache, so other pages stay slow for a few minutes after.
        Measuring is therefore opt-in: a normal run reports no sizes (or the
        last remembered ones) and the page offers a "Measure table sizes"
        button that re-runs with ?sizes=true.

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
        # True only on the run that actually measured (see table_sizes).
        sizes_measured = False
        # Overwritten at the end from whatever this run measured or remembered.
        report["table_sizes"] = None

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
                # v3.4.6: parallel rollup tables for military and watchlist
                ("military_hourly",     "hour_bucket",   True),
                ("watchlist_hourly",    "hour_bucket",   True),
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

            # --- Table sizes (dbstat, opt-in since v3.4.171) ---
            # Both passes below read dbstat, which walks every page of the
            # file, so they only run when the caller asked for sizes. When
            # they do run they're timed together and remembered, so the next
            # normal run can still show the numbers it didn't pay for.
            sizes_t0 = time.time()
            measured_sizes: Dict[str, int] = {}
            if sizes:
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
                        measured_sizes["seen_aircraft_fts (shadows)"] = \
                            shadow_total_bytes
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
                                measured_sizes[name] = int(r["b"])
                        except sq.DatabaseError:
                            pass
                except Exception as e:
                    logger.debug(f"per-table size lookup failed: {e}")

                if measured_sizes:
                    sizes_measured = True
                    _perf_table_sizes.clear()
                    _perf_table_sizes.update({
                        "measured_at": int(time.time()),
                        "took_ms": int(round((time.time() - sizes_t0) * 1000)),
                        "sizes": measured_sizes,
                    })
            if not measured_sizes and _perf_table_sizes:
                # Nothing measured on this run (either sizes weren't asked
                # for, or dbstat isn't available), but an earlier run did:
                # fill the sizes in from what's remembered. `table_sizes`
                # says when they were taken so nobody reads stale bytes as
                # current ones.
                remembered = _perf_table_sizes.get("sizes") or {}
                for info in tables_info:
                    remembered_bytes = remembered.get(info["name"])
                    if remembered_bytes:
                        info["size_bytes"] = int(remembered_bytes)
                # The search index's storage is a synthetic row that only a
                # measuring run builds; bring it back from memory too.
                fts_row = "seen_aircraft_fts (shadows)"
                if remembered.get(fts_row) and not any(t["name"] == fts_row for t in tables_info):
                    tables_info.append({"name": fts_row, "rows": None,
                                        "size_bytes": int(remembered[fts_row]),
                                        "note": "FTS5 backing storage (search index)"})

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
            # v3.4.39: new production path. /api/status now runs this
            # query instead of the COUNT(DISTINCT) rollup above —
            # `idx_seen_last` makes it a single index range scan, no
            # temp B-tree, no DISTINCT. Probe kept paired with the
            # old rollup query so the reader can see the win directly
            # (and confirm the two return the same row count, modulo
            # any aircraft seen exactly at the cutoff boundary which
            # the hour_bucket truncation might include but
            # last_seen_at >= cutoff_ts wouldn't).
            queries.append(_time_query(
                f"unique_aircraft_count_seen (seen_aircraft.last_seen_at, last {all_days}d)",
                "SELECT COUNT(*) FROM seen_aircraft "
                "WHERE last_seen_at >= ?",
                (from_ts,),
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
            # v3.4.6: paired probes — rollup path (post-backfill) and
            # raw fallback (during backfill or on errored installs).
            # Same pattern as the unique_aircraft probes. Compare
            # against report["military_rollup"]["phase"] to know
            # which one matches your production right now.
            queries.append(_time_query(
                f"military_count_rollup (military_hourly, last {mil_days}d)",
                "SELECT COUNT(DISTINCT icao) FROM military_hourly "
                "WHERE hour_bucket >= ? AND hour_bucket <= ?",
                (now - mil_days * 86400, now),
            ))
            queries.append(_time_query(
                f"military_count_raw_fallback (military_sightings, last {mil_days}d)",
                "SELECT COUNT(DISTINCT icao) FROM military_sightings "
                "WHERE seen_at >= ? AND seen_at <= ?",
                (now - mil_days * 86400, now),
            ))

            # Q4: Watchlist tab count
            queries.append(_time_query(
                f"watchlist_count_rollup (watchlist_hourly, last {watch_days}d)",
                "SELECT COUNT(DISTINCT icao) FROM watchlist_hourly "
                "WHERE hour_bucket >= ? AND hour_bucket <= ?",
                (now - watch_days * 86400, now),
            ))
            queries.append(_time_query(
                f"watchlist_count_raw_fallback (watchlist_sightings, last {watch_days}d)",
                "SELECT COUNT(DISTINCT icao) FROM watchlist_sightings "
                "WHERE seen_at >= ? AND seen_at <= ?",
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
        if _perf_table_sizes:
            report["table_sizes"] = {
                "measured_at": _perf_table_sizes.get("measured_at"),
                "took_ms": _perf_table_sizes.get("took_ms"),
                "fresh": sizes_measured,
            }

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
    def post_perf_analyze():
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
            analyze_db(conn)
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
    def notifications_recent(limit: int = 20):
        """Recent notification attempts, newest first. Used by the UI to show
        a sliding log of what Aerodrome has tried to send — helpful when
        debugging why a notification didn't arrive."""
        if _NOTIFIER is None:
            return {"ok": True, "items": []}
        limit = max(1, min(100, int(limit)))
        return {"ok": True, "items": _NOTIFIER.recent(limit)}

    @app.get("/api/notifications/stats")
    def notifications_stats():
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
    def ntfy_logs(lines: int = 100):
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
    def notifications_test(body: TestNotificationBody):
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
    # tab. The server calls into ntfy_installer.py, whose every system change
    # is one call to Aerodrome's root-owned helper (privhelper.run_helper;
    # install.sh installs it and the sudoers rule that names it).

    def _ntfy_reply(result):
        """An ntfy_installer result as the response: as is when ok; a
        refusal or failure by its code (v3.4.150 adds the helper's): 409
        custom_keys / busy, 202 still_running (the helper may still be at
        it; check the status again), 500 helper_unavailable (with the
        refresh command), else 400. Both error and message are set."""
        if result.get("ok"):
            return result
        content = dict(result)
        content.setdefault("error", content.get("message"))
        code = {"custom_keys": 409, "busy": 409, "still_running": 202,
                "helper_unavailable": 500}.get(result.get("code"), 400)
        return JSONResponse(status_code=code, content=content)

    @app.get("/api/ntfy/status")
    def ntfy_status():
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
                from ntfy_installer import (
                    current_config as ntfy_current_config,
                    _read_upstream_relay, _detect_lan_ip,
                )
                # v3.4.27: _detect_lan_ip now returns (ip, error_reason).
                # Both are surfaced in the API response so the wizard's
                # "LAN IP could not be auto-detected" warning can show the
                # actual cause (typically ENETUNREACH from an install-time
                # DHCP race) and the wizard's self-heal button can offer
                # a one-click fix when re-detection now succeeds.
                lan_ip, lan_ip_err = _detect_lan_ip()
                # v3.4.146: base_url is None when the saved address no longer
                # validates; custom_keys / problems are key names only.
                cls = ntfy_current_config() or {}
                current_config = {
                    "base_url": cls.get("base_url"),
                    "upstream_relay": _read_upstream_relay(),
                    "custom_keys": cls.get("custom", []),
                    "problems": cls.get("problems", []),
                    "detected_lan_ip": lan_ip,
                    "detected_lan_ip_error": lan_ip_err,
                }
                # v3.4.150: still on the older root-run unit (status.legacy_unit):
                # the command that finishes the move (saving in the tab does too).
                if status.get("legacy_unit"):
                    import shlex
                    drop = cls.get("custom") or []
                    current_config["migrate_command"] = (
                        f"sudo {privhelper.HELPER} ntfy-migrate"
                        + (f" --drop-custom-keys {shlex.quote(','.join(drop))}" if drop else ""))
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
    def ntfy_install(body: NtfyInstallBody):
        """Download, verify, install, configure, and start ntfy as a systemd
        service alongside Aerodrome. Idempotent — re-running when already
        installed is a no-op that returns current state."""
        try:
            from ntfy_installer import install, validate_base_url
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })

        def refuse(msg):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": msg, "message": msg})

        # Basic sanity on the inputs we pass to a privileged operation
        if not (1 <= body.port <= 65535):
            return refuse("Port must be between 1 and 65535")
        if body.bind not in ("0.0.0.0", "127.0.0.1"):
            return refuse("bind must be '0.0.0.0' or '127.0.0.1'")
        # v3.4.146: a plain http(s)://host[:port][/path] address only — it is
        # written into server.yml, so nothing YAML could misread gets through.
        if body.base_url is not None:
            try:
                validate_base_url(body.base_url)
            except ValueError as e:
                return refuse(str(e))
        return _ntfy_reply(install(port=body.port, bind=body.bind, topic=body.topic,
                                   base_url=body.base_url, upstream_relay=body.upstream_relay))

    class NtfyConfigBody(BaseModel):
        # v2.40.5: partial-update semantics — None means "keep current value."
        base_url: Optional[str] = None
        upstream_relay: Optional[bool] = None
        # v3.4.146: the names a 409 custom_keys response listed, sent back
        # to confirm removing exactly those; a setting the caller didn't
        # name brings a fresh 409 instead of being dropped.
        drop_custom_keys: Optional[List[str]] = None

    @app.post("/api/ntfy/config")
    def ntfy_config_update(body: NtfyConfigBody):
        """(v2.40.5) Update /etc/ntfy/server.yml's base-url and/or upstream
        relay toggle. Restarts the service to apply. Only works on
        aerodrome-managed installs. Used by the Base URL field and the
        upstream-relay checkbox in the Notifications tab.

        v3.4.146: 400 for an address that isn't a plain http(s) URL; 409
        code "custom_keys" (names in custom_keys) when the file has settings
        Aerodrome doesn't manage that drop_custom_keys doesn't name; 400 code
        "stored_value_invalid" (names in keys) when a value the caller left
        out can't be carried over from the file. Nothing is written on any
        of these."""
        try:
            from ntfy_installer import update_config, validate_base_url
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        if body.base_url is not None:
            try:
                validate_base_url(body.base_url)
            except ValueError as e:
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": str(e), "message": str(e)})
        return _ntfy_reply(update_config(base_url=body.base_url,
                                         upstream_relay=body.upstream_relay,
                                         drop_custom_keys=body.drop_custom_keys))

    @app.post("/api/ntfy/upgrade")
    def ntfy_upgrade():
        """Upgrade the local ntfy install to the latest release. Only works
        on aerodrome_managed installs."""
        try:
            from ntfy_installer import upgrade
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        return _ntfy_reply(upgrade())

    class NtfyUninstallBody(BaseModel):
        purge_data: bool = False

    @app.post("/api/ntfy/uninstall")
    def ntfy_uninstall(body: NtfyUninstallBody = NtfyUninstallBody()):
        """Remove a local aerodrome-managed ntfy install. Refuses to touch
        external (user-managed) installs.

        v2.41.0: optional purge_data flag deletes ntfy's cached messages.
        Default False so reinstalls preserve history."""
        try:
            from ntfy_installer import uninstall
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"ntfy_installer module unavailable: {e}"
            })
        return _ntfy_reply(uninstall(purge_data=body.purge_data))

    @app.post("/api/restart")
    def restart_service():
        """Restart the Aerodrome systemd service to pick up restart-only config changes.

        v3.4.147: refused (409) only while a maintenance operation holds the
        lock — never just because an update is being confirmed. If an
        update's restart call reported OK but no restart came, the hold
        expires after 2 minutes and this is the way out."""
        holder = _MAINT.holder()
        if holder:
            return _busy_response(_Busy(holder))
        ok, note = _do_restart()
        if ok:
            return {"ok": True, "message": "Service restart initiated"}
        return JSONResponse(status_code=500, content={"ok": False, "error": note or "Restart failed"})

    @app.get("/api/maintenance/status")
    def maintenance_status():
        """v3.4.147: what the Updates (and later Settings) page shows about
        maintenance. `busy`: the operation holding the lock, or null.
        `update`: the last update result (.update-last-result.json: applied,
        rolled_back or refused), or null. `update_pending`: the update
        being confirmed right now, or null. v3.4.148 `config`: what the last
        startup did when config.yaml couldn't be used (config-recovery.json:
        kind "unreadable" or "bind", reason, source, source_date, web_host,
        web_port, broken_copy, acknowledged), or null. v3.4.149 `restore`:
        {pending, result} — pending: a restore staged and waiting for its
        restart (staged_at, expires_at, restart_failed, waiting_for_restart,
        source, backup_created_at_iso, keep_current_config, notes, kind), or
        null; result: what the last restore did at its start
        (restore-result.json: ok + notes, or rolled_back + error, or
        missing + manual, or expired / cancelled), or null."""
        inst = _install_dir()
        j = updater.pending(inst)
        pend = None
        if j:
            pend = {k: j.get(k) for k in ("from_version", "to_version", "failed_starts",
                                         "served_at", "applier_pid", "state")}
            # The process that applied it is still the one answering: its
            # restart call returned OK but no restart has come (yet).
            pend["waiting_for_restart"] = j.get("applier_pid") == os.getpid()
        # A staged restore past its hour is dropped here — only when the
        # maintenance lock is free (a restore being staged right now holds it).
        _tok = _MAINT.try_acquire("dropping an expired restore")
        if _tok is not None:
            try:
                _drop_expired_restore()
            finally:
                _MAINT.release(_tok)
        return {"busy": _MAINT.holder(), "update": updater.last_result(inst),
                "update_pending": pend,
                "config": config_io.read_recovery_notice(CONFIG_PATH),
                "restore": restore_io.status(inst)}

    class MaintenanceAck(BaseModel):
        kind: str

    @app.post("/api/maintenance/ack")
    def maintenance_ack(body: MaintenanceAck):
        """v3.4.147: dismiss a shown result (kind "update"; v3.4.148 also
        "config"; v3.4.149 "restore")."""
        if body.kind in ("config", "restore"):
            try:
                done = (config_io.ack_recovery_notice(CONFIG_PATH) if body.kind == "config"
                        else restore_io.ack_result(_install_dir()))
            except OSError as e:
                msg = f"Could not record that: {e}"
                return JSONResponse(status_code=507 if atomic_io.is_disk_full(e) else 500,
                                    content={"ok": False, "error": msg, "message": msg})
            return {"ok": True, "acknowledged": done}
        if body.kind != "update":
            return JSONResponse(status_code=400, content={
                "ok": False, "error": f"Unknown kind {body.kind!r}.",
                "message": f"Unknown kind {body.kind!r}."})
        path = _install_dir() / updater.LAST_RESULT
        res = atomic_io.read_json(path)
        if res is None:
            return {"ok": True, "acknowledged": False}
        res["acknowledged"] = True
        try:
            atomic_io.write_json_atomic(path, res)
        except OSError as e:
            msg = f"Could not record that: {e}"
            return JSONResponse(status_code=507 if atomic_io.is_disk_full(e) else 500,
                                content={"ok": False, "error": msg, "message": msg})
        return {"ok": True, "acknowledged": True}

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
    # v3.4.34: also preserve the *configured* db filename if it differs
    # from the historical default. install.sh --demo configures
    # aircraft_history_synthetic.db; user-customized installs can use
    # any filename via CONFIG["data"]["db_file"]. Without this dynamic
    # add, the update-apply backup loop tries to copy the data DB into
    # .backups/<ts>/ (potentially many GB), and racing against SQLite's
    # WAL checkpointing trips an ENOENT on the .db-wal file mid-copy.
    # On the test install (8.4 GB synthetic DB), this was bug-class:
    # the apply would fail entirely with "Backup failed: [Errno 2]".
    # Path.name strips any directory component since db_file is allowed
    # to be either a bare filename or an absolute path.
    try:
        _configured_db = (CONFIG.get("data") or {}).get("db_file") or "aircraft_history.db"
        _configured_db_name = Path(_configured_db).name
        PRESERVE_PATHS.add(_configured_db_name)
        PRESERVE_PATHS.add(_configured_db_name + "-shm")
        PRESERVE_PATHS.add(_configured_db_name + "-wal")
    except Exception:
        # Hard fallback — if CONFIG is somehow malformed at this point,
        # keep the static set rather than crashing route registration.
        # The default-filename entries above still cover stock installs.
        pass

    def _apply_preserve(update_root: Path, update_dir: Path) -> set:
        """Top-level names a staged release never swaps in. A flat staging
        (update/VERSION) also holds update/'s own docs, which aren't part of
        the release."""
        keep = set(PRESERVE_PATHS)
        if update_root.resolve() == update_dir.resolve():
            keep |= {"UPDATE_README.md", ".gitkeep"}
        return keep

    # The live folders a release ships (so an apply may move them): what the
    # preflight checks while nothing is staged yet.
    _RELEASE_DIRS = ("static", "templates", "scripts", "tools", "docs", ".github")

    def _update_preflight(install_dir: Path, update_root: Optional[Path] = None) -> dict:
        """Read-only: could an apply write everything it has to? (v3.4.147)
        Checks exactly what an apply checks: the install directory and the
        live directories the staged release would move. With nothing staged,
        the live directories a release ships. A folder no release contains
        (your own, a stray __pycache__) is never moved, so never flagged."""
        try:
            if update_root is not None and update_root.is_dir():
                names = set(updater.release_entries(
                    update_root, _apply_preserve(update_root, install_dir / "update")))
            else:
                names = {n for n in _RELEASE_DIRS
                         if (install_dir / n).is_dir() and not (install_dir / n).is_symlink()}
            bad = updater.permission_problems(install_dir, sorted(names))
        except OSError as e:
            return {"ok": False, "problems": [str(install_dir)],
                    "message": f"Couldn't check the install folder: {e}", "fix_command": None}
        if not bad:
            return {"ok": True, "problems": [], "message": None, "fix_command": None}
        return {"ok": False, "problems": bad,
                "message": "Updates can't be applied yet: " + updater.permission_message(bad),
                "fix_command": updater.fix_command(bad)}

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
    def check_local_update():
        """Scan the ./update folder and report whether a newer version is staged.

        v3.4.147: also `preflight` — whether an apply could write everything
        it has to (read-only; {ok, problems, message, fix_command})."""
        install_dir = _install_dir()
        update_dir = install_dir / "update"
        current_ver = (install_dir / "VERSION").read_text().strip() if (install_dir / "VERSION").exists() else "unknown"

        # `staged` says what update/ holds when nothing can be applied: "none"
        # (empty, or only its own UPDATE_README.md/.gitkeep — the normal state
        # after every apply) or "partial" (something is there, but no VERSION).
        if not update_dir.exists():
            return {
                "available": False,
                "current_version": current_ver,
                "update_version": None,
                "staged": "none",
                "reason": "No update/ directory found. Copy a new release there to stage an update.",
                "update_dir": str(update_dir),
                "preflight": _update_preflight(install_dir),
            }

        # Look for VERSION file inside update/ — accept either update/VERSION
        # or update/aerodrome/VERSION (user may have copied the whole zip contents)
        candidates = [
            update_dir / "VERSION",
            update_dir / "aerodrome" / "VERSION",
        ]
        version_file = next((c for c in candidates if c.exists()), None)
        if not version_file:
            try:
                leftovers = [p.name for p in update_dir.iterdir()
                             if p.name not in UPDATE_FOLDER_REFRESH]
            except OSError:
                leftovers = ["?"]   # can't list it: don't claim it's empty
            if not leftovers:
                return {
                    "available": False,
                    "current_version": current_ver,
                    "update_version": None,
                    "staged": "none",
                    "reason": "Nothing staged. To update, upload a release zip below, "
                              "or copy one into update/ over SSH.",
                    "update_dir": str(update_dir),
                    "preflight": _update_preflight(install_dir),
                }
            return {
                "available": False,
                "current_version": current_ver,
                "update_version": None,
                "staged": "partial",
                "reason": "No VERSION file in update/. Make sure you copied the full release contents.",
                "update_dir": str(update_dir),
                "preflight": _update_preflight(install_dir),
            }

        update_ver = updater.read_version(version_file) or ""
        # Root of the staged release (the dir that contains VERSION)
        update_root = version_file.parent

        cur_tuple = updater.parse_version(current_ver)
        new_tuple = updater.parse_version(update_ver)

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
            "preflight": _update_preflight(install_dir, update_root),
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
    def upload_local_update(file: UploadFile = File(...)):
        """Receive a release zip and extract it into update/ (see
        _stage_uploaded_update). v3.4.147: a plain def (runs in the
        threadpool, never on the event loop); one maintenance operation at a
        time, and refused while the last update is still being confirmed."""
        try:
            with _maintenance("an update upload", update_op=True):
                return _stage_uploaded_update(file)
        except _Busy as b:
            return _busy_response(b)

    def _stage_uploaded_update(file: UploadFile):
        """Receive a release zip and extract it into update/. Runs the
        same check logic as /api/updates/local/check after extraction so
        the frontend gets a ready-to-apply response."""
        import io, zipfile

        install_dir = _install_dir()
        update_dir = install_dir / "update"
        MAX_UPLOAD_BYTES = UPDATE_UPLOAD_MAX_BYTES  # 50 MB (the middleware's cap too)

        # v3.4.9: size-bounded stream-read. Was unbounded `await file.read()`
        # followed by post-hoc len() check, which allocated full upload
        # before rejection. Now aborts as soon as the 50 MB cap is hit.
        raw, err = _read_upload_bounded(file, MAX_UPLOAD_BYTES)
        if err is not None:
            _msg = (f"{err}. Aerodrome release zips are a few MB; "
                    f"did you upload a full backup?")
            return JSONResponse(status_code=413, content={
                "ok": False, "error": _msg, "message": _msg})
        if not raw:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "Uploaded file is empty"
            })

        # Validate zip format
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw), "r")
            # v3.4.147: refuse a zip that unpacks too big (or wouldn't fit)
            # before anything is decompressed — and before update/ is wiped,
            # so the release staged before it is kept.
            refusal = _unpacked_size_refusal(zf, install_dir)
            if refusal:
                return JSONResponse(status_code=refusal[0], content={
                    "ok": False, "code": refusal[1], "error": refusal[2], "message": refusal[2]})
            try:
                bad_file = zf.testzip()
            except (NotImplementedError, RuntimeError):
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": "This zip can't be read (an unsupported or "
                                          "encrypted entry). Nothing was changed."})
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
        update_ver = updater.read_version(version_file) or ""
        cur_tuple = updater.parse_version(current_ver)
        new_tuple = updater.parse_version(update_ver)
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
                result = privhelper.run_sudo(["cat", str(path)], timeout=5)
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

    def _sudoers_state():
        """(v3.4.150) The live drop-in: ("absent", None) when it is known
        not to be there, ("unreadable", None) when it can't be read (sudo
        refused the cat, or /etc/sudoers.d isn't searchable for this
        account), ("ok", N) with its SUDOERS_VERSION.
        Where /etc/sudoers.d isn't searchable for this account (Fedora/RHEL
        ship it 0750), a missing drop-in looks unreadable too: when sudo
        won't show it and there is no helper either, it counts as absent.
        (A drop-in sudo won't let this account read is one from before
        v2.40.1, which never ran on those distros: they came in v3.2.0.)"""
        try:
            os.stat(SUDOERS_DROPIN)
            hidden = False
        except FileNotFoundError:
            return "absent", None
        except OSError:
            hidden = True
        n = _read_sudoers_version(SUDOERS_DROPIN)
        if n is None and hidden and not os.path.lexists(privhelper.HELPER):
            return "absent", None
        return ("unreadable", None) if n is None else ("ok", n)

    def _effective_rules_version(n: int):
        """(version the live rules really give, helper version or None).
        From version 6 the rules name Aerodrome's system helper, so they are
        only as new as the helper they name (its line 2, read without sudo;
        0 when it's missing)."""
        if n < 6:
            return n, None
        helper = privhelper.helper_file_version()
        return min(n, helper), helper

    def _rules_reason(wanted: int, rules: int, helper: Optional[int], what: str) -> str:
        if helper is not None and helper < rules:
            have = "is missing" if helper == 0 else f"is at version {helper}"
            return (f"{what} requires sudoers version {wanted}; /etc/sudoers.d/aerodrome is "
                    f"at version {rules}, but Aerodrome's system helper {have}. "
                    f"Run the command below on the server, then press Re-check.")
        return (f"{what} requires sudoers version {wanted}, but "
                f"/etc/sudoers.d/aerodrome is at version {rules}. "
                f"Run the command below on the server, then press Re-check.")

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
          needs_refresh=False, can_verify=False, managed=False — (v3.4.150)
              no drop-in at all: Aerodrome doesn't manage this host's
              permissions (a manual install, no sudo); never blocks
        v3.4.150: live rules of version 6 or later count only as far as the
        system helper they name (_effective_rules_version): live_version is
        that effective version; rules_version / helper_version the parts.
        """
        staged_installer = update_root / "install.sh"

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

        state, rules_ver = _sudoers_state()
        if state == "absent":
            return {
                "can_verify": False,
                "needs_refresh": False,
                "managed": False,
                "reason": "Aerodrome doesn't manage system permissions on this host.",
                "staged_version": staged_ver,
                "live_version": None,
            }
        if state == "unreadable":
            # Couldn't read the live file at all. Most common reason:
            # pre-v2.40.1 sudoers grant doesn't include a rule letting us
            # sudo-cat this file. We don't want to hard-block the upgrade
            # that WOULD fix that situation, so we let it through. After the
            # upgrade the live check (/api/sudoers/status) can read it and
            # shows the refresh command if one is still needed.
            return {
                "can_verify": False,
                "needs_refresh": False,
                "reason": "Cannot read /etc/sudoers.d/aerodrome to verify version. "
                          "After this upgrade the version can be verified automatically.",
                "staged_version": staged_ver,
                "live_version": None,
            }

        live_ver, helper_ver = _effective_rules_version(rules_ver)
        if live_ver < staged_ver:
            # v3.4.44: recovery_command points at the STAGED release, not
            # the live one — /opt/aerodrome/install.sh is the LIVE install.sh,
            # which writes the OLD SUDOERS_VERSION and can never lift the gate.
            # v3.4.145: the staged scripts/recover-sudoers.sh is a hand-off: it
            # runs whichever install.sh (staged or installed) carries the
            # newest rules in --privileges-only mode — the sudoers rule only,
            # same service account, never older rules over newer ones. It is
            # told the install dir explicitly (--install-dir).
            return {
                "can_verify": True,
                "needs_refresh": True,
                "reason": _rules_reason(staged_ver, rules_ver, helper_ver, "Staged release"),
                "staged_version": staged_ver,
                "live_version": live_ver,
                "rules_version": rules_ver,
                "helper_version": helper_ver,
                "recovery_command": _sudoers_recovery_command(update_root),
            }

        # live_ver >= staged_ver — we're fine. The >= case handles downgrade,
        # which doesn't need a sudoers refresh.
        return {
            "can_verify": True,
            "needs_refresh": False,
            "reason": "Sudoers version matches.",
            "staged_version": staged_ver,
            "live_version": live_ver,
            "rules_version": rules_ver,
            "helper_version": helper_ver,
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
        with the existing applyLocal frontend code.

        v3.4.150: no drop-in at all → can_verify False (no banner: a manual
        install, or no sudo). An unreadable one, while this release expects
        version 6 or later → ask the helper itself (`version`, through sudo):
        an answer of the expected version is fine, anything else raises the
        banner with the refresh command. Readable rules count only as far as
        the helper they name (_effective_rules_version)."""
        installed = Path(__file__).parent / "install.sh"
        recovery_command = _live_recovery_command()
        expected_ver = _read_sudoers_version(installed)
        if expected_ver is None or expected_ver == 0:
            return {
                "can_verify": False,
                "needs_refresh": False,
                "reason": "Cannot read installed install.sh or it has no SUDOERS_VERSION marker.",
                "expected_version": expected_ver,
                "live_version": None,
                "install_dir": str(installed.parent),
                "recovery_command": recovery_command,
            }
        state, rules_ver = _sudoers_state()
        if state == "absent":
            return {
                "can_verify": False,
                "needs_refresh": False,
                "managed": False,
                "reason": "Aerodrome doesn't manage system permissions on this host.",
                "expected_version": expected_ver,
                "live_version": None,
                "install_dir": str(installed.parent),
                "recovery_command": recovery_command,
            }
        helper_ver = None
        if state == "unreadable":
            if expected_ver < 6:
                return {
                    "can_verify": False,
                    "needs_refresh": False,
                    "reason": "Cannot read /etc/sudoers.d/aerodrome. "
                              "Run the command below on the server to set up the permission check.",
                    "expected_version": expected_ver,
                    "live_version": None,
                    "install_dir": str(installed.parent),
                    "recovery_command": recovery_command,
                }
            try:
                probe = privhelper.run_helper("version")
                live_ver = rules_ver = helper_ver = int(probe.get("version") or 0)
            except (privhelper.HelperError, ValueError, TypeError):
                live_ver = rules_ver = helper_ver = 0
        else:
            live_ver, helper_ver = _effective_rules_version(rules_ver)
        if live_ver < expected_ver:
            if state == "unreadable":
                reason = ("Cannot read /etc/sudoers.d/aerodrome, and Aerodrome's system helper "
                          + ("didn't answer" if not live_ver else f"is at version {live_ver}")
                          + f" (this release expects version {expected_ver}). "
                          "Run the command below on the server, then press Re-check.")
            else:
                reason = _rules_reason(expected_ver, rules_ver, helper_ver, "Installed Aerodrome")
            return {
                "can_verify": True,
                "needs_refresh": True,
                "reason": reason,
                "expected_version": expected_ver,
                "live_version": live_ver,
                "rules_version": rules_ver,
                "helper_version": helper_ver,
                "install_dir": str(installed.parent),
                "recovery_command": recovery_command,
            }
        return {
            "can_verify": True,
            "needs_refresh": False,
            "reason": "Sudoers version matches installed Aerodrome.",
            "expected_version": expected_ver,
            "live_version": live_ver,
            "rules_version": rules_ver,
            "helper_version": helper_ver,
            "install_dir": str(installed.parent),
            "recovery_command": recovery_command,
        }

    @app.get("/api/sudoers/status")
    def sudoers_status():
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
    # v3.4.50: stampede-protection lock for the db_check recompute in
    # /api/status. v3.4.49 moved the (non-awaiting) status handler into
    # the threadpool, which was the right fix for one slow endpoint
    # freezing navigation — but it also removed the implicit
    # one-request-at-a-time serialization the single event loop used to
    # provide. The unintended consequence: every admin tab polls
    # /api/status on a 30s cadence, and when the 30s db_stats/capacity
    # caches expire, ALL the concurrent pollers saw the miss and
    # recomputed the expensive COUNT(DISTINCT)/capacity queries IN
    # PARALLEL. On a RAM-constrained box those concurrent SQLite scans
    # thrash the page cache and feed back on each other, escalating from
    # ~1s to 60s+ and leaving the UI non-responsive — a classic cache
    # stampede. This lock collapses the concurrent recomputes into one:
    # the first thread to see a stale cache acquires the lock and
    # refreshes; the others either serve the still-present (slightly
    # stale) cached value without recomputing, or — only on a cold cache
    # with nothing to serve — wait for that single refresh. Same
    # double-checked pattern as the v3.4.46 row-count cache.
    _db_check_refresh_lock = _threading.Lock()
    DB_STATS_CACHE_TTL_SEC = 30
    HEXDB_PROBE_CACHE_TTL_SEC = 30

    # Initial populate at startup so the first /api/status call has a
    # meaningful value for the header badge.
    try:
        nonlocal_state["last_sudoers_check"] = _check_live_sudoers_drift()
    except Exception as e:
        logger.warning("Initial sudoers drift check failed: %s", e)

    @app.post("/api/updates/local/apply")
    def apply_local_update(snapshot_db: bool = False, allow_downgrade: bool = False,
                           allow_reinstall: bool = False):
        """Apply the release staged in update/ (see _apply_staged_update).

        v3.4.147: staged apply with automatic rollback. One maintenance
        operation at a time; refused while the last update is still being
        confirmed. allow_downgrade / allow_reinstall let an older or the
        same version through (API only)."""
        try:
            with _maintenance("an update", update_op=True) as tok:
                return _apply_staged_update(tok, snapshot_db, allow_downgrade, allow_reinstall)
        except _Busy as b:
            return _busy_response(b)

    def _configured_db_path(install_dir: Path) -> Path:
        db = Path((CONFIG.get("data") or {}).get("db_file") or "aircraft_history.db")
        return db if db.is_absolute() else install_dir / db

    def _fresh_update_ts(install_dir: Path) -> str:
        """A YYYYMMDD-HHMMSS no staging/old/failed dir or .backups/ entry uses."""
        t = time.time()
        for _ in range(120):
            ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
            if not any((install_dir / n).exists() for n in (
                    f"{updater.STAGING_PREFIX}{ts}", f"{updater.OLD_PREFIX}{ts}",
                    f"{updater.FAILED_PREFIX}{ts}", f".backups/{ts}")):
                return ts
            t += 1
        return ts

    def _snapshot_db_for_update(db_path: Path, backup_dir: Path) -> None:
        """v3.4.38: opt-in consistent DB snapshot (SQLite online-backup API)
        into .backups/<ts>/<db name>.snapshot, the folder the previous
        release moves into once the update is confirmed. Best effort: a
        failure is logged and the apply continues (the database itself is
        never touched by an update)."""
        try:
            if db_path.exists():
                atomic_io.ensure_private_dir(backup_dir.parent)   # .backups/: 0700 when new
                backup_dir.mkdir(parents=True, exist_ok=True)
                _snapshot_path = backup_dir / f"{db_path.name}.snapshot"
                _t0 = time.time()
                _src = _open_db_conn(str(db_path))
                _dst = _open_db_conn(str(_snapshot_path))
                try:
                    with _dst:
                        _src.backup(_dst)
                finally:
                    _src.close()
                    _dst.close()
                logger.info(f"DB snapshot written to {_snapshot_path.name}: "
                            f"{_snapshot_path.stat().st_size:,} bytes in "
                            f"{time.time() - _t0:.1f}s")
            else:
                logger.warning(f"DB snapshot requested but {db_path} does not exist; "
                               f"skipping snapshot, continuing with apply")
        except Exception as snap_err:
            logger.warning(f"DB snapshot failed (apply continues): {snap_err}")

    def _refuse_update(e, cur_version: str, new_version: str) -> JSONResponse:
        """An update refusal: logged, recorded for the Updates page (best
        effort), and returned with both `error` and `message`."""
        new_version = updater.shown_version(new_version)
        logger.error(f"Update to {new_version} not applied ({e.code}): {e.message}"
                     + (f"\n{e.detail}" if e.detail else ""))
        try:
            atomic_io.write_json_atomic(_install_dir() / updater.LAST_RESULT, {
                "result": "refused", "code": e.code, "message": e.message,
                "detail": e.detail, "from_version": cur_version, "to_version": new_version,
                "at": time.time(), "acknowledged": False})
        except OSError as w:
            logger.warning(f"Could not record the refused update: {w}")
        return JSONResponse(status_code=e.http_status, content={
            "ok": False, "code": e.code, "error": e.message, "message": e.message,
            "detail": e.detail, "from_version": cur_version, "to_version": new_version})

    def _apply_staged_update(tok, snapshot_db: bool = False, allow_downgrade: bool = False,
                             allow_reinstall: bool = False):
        """Apply the release staged in update/ (or update/aerodrome/).

        v3.4.147: nothing of Aerodrome's own changes until the new release
        is staged to the side (.update-staging-<ts>/), its Python packages
        are installed if its requirements changed, and it test-loads in a
        fresh interpreter. Then it is switched in (journal first; updater.py),
        the service restarts, and main.py's update guard puts this version
        back if the new one fails to start twice. A refusal keeps update/ and
        leaves the install as it was. If the restart call itself fails, the
        swap is undone in this process and the update is refused.

        v3.4.38: when `snapshot_db=true`, also writes a consistent
        SQLite-online-backup-API snapshot of the live database into
        .backups/<ts>/. Disabled by default because the snapshot is the size
        of the live DB, which can be gigabytes; opting in is an
        informed-consent choice.
        """
        import sys as _sys
        install_dir = _install_dir()
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
        new_version = updater.read_version(version_file) or ""
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
                    "Aerodrome's system permissions need a refresh first — "
                    "run the command shown, then Re-check."),
                "sudoers": sudoers_check,
                # v3.1.1: include the install_dir so the client-side modal can
                # show the exact command path. Without this the modal falls
                # back to a placeholder /opt/aerodrome which is wrong
                # for any non-default install location.
                "install_dir": str(install_dir),
            })

        ts = _fresh_update_ts(install_dir)
        preserve = _apply_preserve(update_root, update_dir)
        venv_pip = install_dir / "venv" / "bin" / "pip"
        venv_py = install_dir / "venv" / "bin" / "python3"
        if not venv_py.exists():
            venv_py = Path(_sys.executable)
        # A release from before 3.4.147 has no update guard: it can never
        # confirm or roll back an update, so going back to one (API only)
        # is made final as soon as its restart is under way.
        target_guarded = updater.has_guard(update_root)
        staging = None
        pip_detail = None
        deps_ran = False
        previous_loads = None
        packages_note = "Aerodrome's files were not changed (its Python packages may have been updated)."
        try:
            try:
                updater.check_versions(cur_version, new_version, allow_downgrade, allow_reinstall)
                entries = updater.release_entries(update_root, preserve)
                updater.preflight_space(install_dir, update_root, preserve)
                updater.preflight_permissions(install_dir, entries)
                staging = updater.stage(install_dir, update_root, preserve, ts)
                if updater.deps_changed(staging, install_dir):
                    logger.info(f"Update to {new_version}: Python requirements changed; "
                                f"installing them")
                    deps_ran = True
                    pip_ok, pip_out = updater.install_deps(staging, venv_pip)
                    if not pip_ok:
                        pip_detail = pip_out or "pip failed"
                        logger.warning(f"Update to {new_version}: installing Python "
                                       f"packages failed: {pip_detail}")
                    # The venv is shared and never rolled back: check that the
                    # RUNNING version still loads with the new packages (a
                    # rollback, or the next restart after a refusal, starts it).
                    try:
                        updater.verify(install_dir, venv_py)
                        previous_loads = True
                    except updater.ApplyError as pe:
                        previous_loads = False
                        logger.warning(f"Update to {new_version}: after installing its Python "
                                       f"packages, the running {cur_version} no longer loads; "
                                       f"restarting it (or rolling back to it) would fail: "
                                       f"{pe.detail}")
                try:
                    updater.verify(staging, venv_py)
                except updater.ApplyError as e:
                    if pip_detail:
                        raise updater.ApplyError(
                            "deps_failed",
                            "Installing the new release's Python packages failed, and it "
                            "doesn't load without them. Aerodrome's files were not changed.",
                            500, detail=f"pip:\n{pip_detail}\n\ntest-load:\n{e.detail}")
                    raise
            except OSError as e:
                raise updater.ApplyError(
                    "no_space" if atomic_io.is_disk_full(e) else "apply_failed",
                    f"The update stopped: {e}. Aerodrome's files were not changed.",
                    507 if atomic_io.is_disk_full(e) else 500)
            db_path = _configured_db_path(install_dir)
            if snapshot_db:
                _snapshot_db_for_update(db_path, install_dir / ".backups" / ts)
            journal = updater.swap_in(install_dir, staging, ts, cur_version, new_version, extra={
                "db_file": str(db_path), "schema_before": updater.schema_version_ro(db_path),
                "pip_warning": pip_detail, "previous_loads": previous_loads},
                entries=entries)
        except updater.ApplyError as e:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if deps_ran:
                # pip already changed the venv: "nothing changed" isn't true.
                for said in ("Aerodrome's files were not changed.", "Nothing was changed."):
                    if said in e.message:
                        e.message = e.message.replace(said, packages_note)
                        break
                if previous_loads is False:
                    e.message += (f" Warning: with the new packages the running {cur_version} "
                                  f"no longer loads, so its next restart will fail; see the log.")
            return _refuse_update(e, cur_version, new_version)

        # The new release is in place; the running process is still the old
        # code. Restart into it.
        restart_ok, restart_note = _do_restart()
        if not restart_ok:
            note = restart_note or "the restart command failed"
            try:
                updater.swap_back(install_dir, journal, reason=f"restart failed: {note}",
                                  restore_config=False, record=False, discard_failed=True)
                back = "The previous version is still running."
            except Exception as e:
                logger.error(f"Could not undo the update after the restart failed: {e}")
                back = ("The previous version is still running; Aerodrome finishes putting "
                        "its files back at its next start.")
            if deps_ran:
                back += " Its Python packages may have been updated."
            return _refuse_update(updater.ApplyError(
                "restart_failed",
                f"The update was not applied: Aerodrome can't restart itself on this "
                f"install ({note}). {back} See \"Updates need a restart permission\" "
                f"in INSTALL.", 500, detail=note), cur_version, new_version)

        final_now = False
        if not target_guarded:
            # No guard in the target: nothing would ever confirm this journal
            # (and a later guarded release would trip over it). Final now.
            try:
                final_now = updater.commit(install_dir)
                logger.info(f"Update {cur_version} -> {new_version}: that release has no "
                            f"automatic rollback, so the update is final now")
            except OSError as e:
                logger.warning(f"Could not make the update to {new_version} final: {e}")

        # Refresh update/'s own docs from the new release, then clear the
        # staged copy. After the restart call on purpose: a refused restart
        # keeps the staged release. (--no-block returns at once; systemd runs
        # ExecStop — a new interpreter — before SIGTERM reaches us, and this
        # takes milliseconds. An interrupted clear leaves only inert files.)
        if (update_root / "update").is_dir():
            _refresh_update_folder_docs(update_root / "update", update_dir)
        update_dir_keep = {"UPDATE_README.md", ".gitkeep"}
        try:
            for item in list(update_dir.iterdir()):
                if item.name in update_dir_keep:
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        except Exception as e:
            logger.warning(f"Could not fully clear update/: {e}")

        # Prune old install snapshots so .backups/ doesn't grow unbounded
        # (never the folder this update's journal names).
        _prune_install_backups()
        _MAINT.park(tok, "restarting after an update", _RESTART_HOLD_SEC)
        logger.info(f"Update {cur_version} -> {new_version} switched in; restarting")
        return {
            "ok": True,
            "from_version": cur_version,
            "to_version": new_version,
            "backup_dir": f".backups/{ts}",
            "restart_ok": True,
            "restart_note": "",
            "pip_warning": pip_detail,
            "previous_loads": previous_loads,
            "final": final_now,
            # Kept for pages from before v3.4.147 (they read pip_ok/pip_msg).
            "pip_ok": pip_detail is None,
            "pip_msg": pip_detail or "",
            "message": ("Update applied. Aerodrome is restarting. That release has no "
                        "automatic rollback, so the previous version is kept in .backups/ "
                        "for going back by hand." if final_now else
                        "Update applied. Aerodrome is restarting; it confirms the new "
                        "version once it answers."),
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

    class _StagingRefused(ValueError):
        """A downloaded release refused before staging (size / space)."""

        def __init__(self, status, code, message):
            super().__init__(message)
            self.status, self.code = status, code

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

        # v3.4.147: refuse an oversized release before update/ is wiped
        # (and before anything is decompressed).
        try:
            with _zipfile.ZipFile(_io.BytesIO(zip_bytes)) as _zf:
                _refusal = _unpacked_size_refusal(_zf, install_dir)
        except _zipfile.BadZipFile:
            raise ValueError(
                "Downloaded file isn't a valid zip archive. "
                "The download was corrupted — try again."
            )
        if _refusal:
            raise _StagingRefused(*_refusal)

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

                # v3.4.88: zip-slip guard. Mirror the local-upload path's
                # containment check (which this GitHub-staging path was missing):
                # resolve each destination and refuse anything that lands outside
                # update/. The wrapper-strip above turns a crafted
                # "aerodrome-vX/../evil.py" into "../evil.py", which would escape
                # the staging dir — and the sha256 is an integrity check, not an
                # authenticity one, so a tampered release / defeated TLS to GitHub
                # shouldn't be able to write outside update/.
                target_root = update_dir.resolve()

                for member in zf.namelist():
                    if has_common_prefix:
                        # Strip the wrapper folder
                        stripped = member[len(candidate_prefix):]
                        if not stripped:  # the prefix entry itself
                            continue
                        dest_path = update_dir / stripped
                    else:
                        dest_path = update_dir / member

                    if "\x00" in member:
                        raise ValueError(f"Release zip entry has a null byte: {member!r}")
                    try:
                        dest_path.resolve().relative_to(target_root)
                    except ValueError:
                        raise ValueError(
                            f"Release zip entry escapes the staging directory: {member!r}"
                        )

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
    def apply_github_update(snapshot_db: bool = False):
        """Download + verify + stage from GitHub, then apply (see
        _github_apply_locked). v3.4.147: a plain def; the whole download,
        staging and apply hold the maintenance lock, and it is refused while
        the last update is still being confirmed."""
        try:
            with _maintenance("an update download", update_op=True) as tok:
                return _github_apply_locked(tok, snapshot_db)
        except _Busy as b:
            return _busy_response(b)

    def _github_apply_locked(tok, snapshot_db: bool = False):
        """v3.0.1: download + verify + stage from GitHub, then apply.

        No request body — the tag to apply is read from update_state's
        last_known_latest (the same tag the user just saw on the card).
        Refuses if no update is currently available (running >= latest,
        or last_known_latest is null), so a stale UI cache can't trigger
        an unwanted apply.

        v3.4.38: accepts a `snapshot_db` query param threaded through to
        apply_local_update — opt-in consistent SQLite snapshot in the
        backup dir, off by default. See apply_local_update for the
        rationale on failure semantics (best-effort, doesn't block apply).

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

        install_dir = _install_dir()

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
            return JSONResponse(status_code=getattr(e, "status", 500), content={
                "ok": False,
                "code": getattr(e, "code", None),
                "error": str(e),
                "message": str(e),
            })

        # Hand off to the staged apply, still holding the lock — the same
        # sudoers pre-flight, test-load, switch-in, restart and rollback as
        # a local apply, and the same response shape. (v3.4.51's lesson: this
        # is the path that applies the NEXT release; it calls a plain def
        # directly and never awaits one.)
        return _apply_staged_update(tok, snapshot_db, False, False)

    @app.get("/api/updates/github/check")
    def check_github_update(force: bool = False):
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
          latest_unreadable     — true iff latest_known is set but not parseable as a version
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
            "latest_unreadable": bool(latest and updater.parse_version(latest) is None),
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
    def get_changelog(source: str = "install"):
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
    def notification_test_ok():
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
    def status_page():
        return _serve_template("status.html")

    @app.get("/config", response_class=HTMLResponse)
    def config_page():
        return _serve_template("config.html")

    @app.get("/updates", response_class=HTMLResponse)
    def updates_page():
        return _serve_template("updates.html")

    @app.get("/documentation", response_class=HTMLResponse)
    def docs_page():
        return _serve_template("docs.html")

    @app.get("/about", response_class=HTMLResponse)
    def about_page():
        return _serve_template("about.html")

    @app.get("/board", response_class=HTMLResponse)
    def board_page():
        """v3.4.110: kiosk display board for FBO/office wall TVs. Three
        layouts (radar wall / flight board / hybrid+spotlight) chosen per
        screen — /board with no mode shows a one-time picker persisted in
        that browser's localStorage; ?mode=radar|board|hybrid bypasses it
        (kiosk autostart URLs); ?mode=pick re-opens the picker. Chrome-less,
        read-only, dark-only; reuses /api/live, /api/ui-config, and the
        photo/owner/route enrichment endpoints."""
        return _serve_template("board.html")

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page():
        return _serve_template("logs.html")

    @app.get("/performance", response_class=HTMLResponse)
    def performance_page():
        """v2.41.8: diagnostic page that renders /api/perf/diagnostics.
        Accessed from the Updates page or gear menu; intended for debugging
        slow-hardware scenarios (Pi with large databases, etc.).

        v2.41.23: Performance is now one of several diagnostics reachable
        from the /diagnostics hub. The page itself is unchanged; only the
        navigation path to it changed."""
        return _serve_template("performance.html")

    # v3.1.0: switch-to-real wizard. Multi-step page that walks demo-mode
    # users through transitioning to a real receiver. Lives at its own
    # URL (not embedded in /config) so the browser back button does the
    # right thing (returns to /config, doesn't unwind half a wizard).
    @app.get("/setup/switch-to-real", response_class=HTMLResponse)
    def switch_to_real_page():
        return _serve_template("switch-to-real.html")

    @app.post("/api/setup/switch-to-real")
    async def switch_to_real_execute(request: Request):
        """Run the demo→real transition (see _switch_to_real_work).
        v3.4.147: one maintenance operation at a time; the lock is kept for
        a while once the restart is requested. v3.4.148: a thin async shell
        — it reads the JSON body (that needs await) and runs the blocking
        work (the receiver probe, systemctl, file writes) in the threadpool,
        so the page and every other request stay responsive meanwhile. The
        lock is try-only, so taking it here on the event loop is safe."""
        from starlette.concurrency import run_in_threadpool
        try:
            with _maintenance("switching to real data") as tok:
                # Refuse if not actually in demo mode — defensive guard
                # against accidental nukes via stale browser state or
                # scripted retries.
                if not bool((CONFIG.get("demo") or {}).get("enabled", False)):
                    return {
                        "ok": False,
                        "error": "Refusing to run switch-to-real on a non-demo install.",
                        "steps": [],
                    }
                try:
                    body = await request.json()
                except Exception:
                    return {"ok": False, "error": "Invalid JSON body.", "steps": []}
                if not isinstance(body, dict):
                    return {"ok": False, "error": "Invalid JSON body.", "steps": []}
                result = await run_in_threadpool(_switch_to_real_work, body)
                if isinstance(result, dict) and result.get("ok"):
                    _MAINT.park(tok, "restarting after switching to real data",
                                _RESTART_HOLD_SEC)
                return result
        except _Busy as b:
            return _busy_response(b)

    def _switch_to_real_work(body: dict):
        """Execute the demo→real transition (runs in the threadpool).

        Step 0 — reachability test on the new receiver. If unreachable,
            abort without touching anything (defends against typo'd IPs
            taking the install offline). The new config is then built and
            validated — also before anything is touched.
        Step 1 — stop + disable + remove the synthetic-feeder service
            (v3.4.150: the system helper's feeder-remove). If the helper
            can't do it, the step is marked failed with the command that
            finishes it and the switch carries on: the feeder only
            listens on 127.0.0.1, so a leftover one is harmless.
        Step 2 — nuke aircraft_history.db (+ -wal, -shm) to clear demo
            sightings from stats forever.
        Step 3 — update config.yaml: watchlist=[], receiver.* to the
            new values, demo.enabled=false, hexdb.enabled=true.
        Step 4 — restart aerodrome.service via sudo.

        Returns {ok: bool, error: str|None, steps: [...]} so the wizard
        can advance or display the failure point.

        v3.4.148: the new config.yaml is rendered and STAGED (written to a
        synced temp file — every step that can fail for lack of space)
        before the feeder is stopped or the demo database deleted; step 3
        only renames it into place. A full disk now stops the switch with
        nothing touched.
        """
        import os as _os
        import requests as _req
        import yaml as _yaml

        new_ip = (body.get("receiver_ip") or "").strip()
        new_port = body.get("receiver_port")
        new_lat = body.get("latitude")
        new_lon = body.get("longitude")
        new_path = (body.get("receiver_path") or "/data/aircraft.json").strip()

        if not new_ip:
            return {"ok": False, "error": "receiver_ip is required.", "steps": []}
        try:
            new_port = int(new_port) if new_port is not None else 8080
        except (TypeError, ValueError):
            return {"ok": False, "error": "receiver_port must be an integer.",
                    "steps": []}

        steps = []

        def _step(name, ok, detail=""):
            steps.append({"name": name, "ok": ok, "detail": detail})

        # ----- Step 0: reachability test on the new receiver -----
        # Catches typo'd IPs / wrong ports BEFORE we destroy anything.
        # 5s timeout is generous; a healthy receiver responds in ms.
        try:
            test_url = f"http://{new_ip}:{new_port}{new_path}"
            r = _req.get(test_url, timeout=5)
            if r.status_code != 200:
                _step("reachability", False,
                      f"HTTP {r.status_code} from {test_url}")
                return {"ok": False,
                        "error": (f"Could not reach the new receiver "
                                  f"({test_url} returned HTTP "
                                  f"{r.status_code}). Double-check the "
                                  f"IP, port, and path."),
                        "steps": steps}
            _step("reachability", True, f"Reached {test_url}")
        except _req.ConnectionError:
            _step("reachability", False, "Connection refused")
            return {"ok": False,
                    "error": (f"Could not connect to {new_ip}:{new_port}. "
                              f"Check that the receiver is running and "
                              f"reachable from this server."),
                    "steps": steps}
        except _req.Timeout:
            _step("reachability", False, "Timed out after 5s")
            return {"ok": False,
                    "error": (f"Timed out waiting for {new_ip}:{new_port}. "
                              f"Check that the receiver is reachable "
                              f"from this server."),
                    "steps": steps}
        except Exception as e:
            _step("reachability", False, str(e))
            return {"ok": False,
                    "error": f"Reachability test failed: {e}",
                    "steps": steps}

        # v3.4.141: build and validate the new config BEFORE anything
        # destructive, from the file the server actually loaded (it used to
        # write a cwd-relative config.yaml, unvalidated, after the demo DB
        # was already gone).
        from config_validator import validate_config
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _yaml.safe_load(f) or {}
            cfg["watchlist"] = []
            cfg.setdefault("receiver", {})["ip"] = new_ip
            cfg["receiver"]["port"] = new_port
            cfg["receiver"]["path"] = new_path
            if new_lat is not None:
                try:
                    cfg["receiver"]["latitude"] = float(new_lat)
                except (TypeError, ValueError):
                    pass
            if new_lon is not None:
                try:
                    cfg["receiver"]["longitude"] = float(new_lon)
                except (TypeError, ValueError):
                    pass
            cfg.setdefault("demo", {})["enabled"] = False
            cfg.setdefault("hexdb", {})["enabled"] = True
        except Exception as e:
            _step("update_config", False, str(e))
            return {"ok": False, "error": "Failed to read config.yaml.",
                    "steps": steps}
        errors = validate_config(cfg)
        if errors:
            _step("update_config", False,
                  "; ".join(f"{p}: {m}" for p, m in errors))
            return {"ok": False,
                    "error": ("These settings aren't valid: "
                              + "; ".join(f"{p}: {m}" for p, m in errors[:3])),
                    "steps": steps}
        # v3.4.148: stage the new config.yaml now (comments kept where they
        # read back the same), before the feeder or the database is touched.
        try:
            _staged_cfg = config_io.stage_text(CONFIG_PATH,
                                               config_io.render_config(CONFIG_PATH, cfg))
        except (config_io.ConfigWriteError, ValueError) as e:
            msg = str(e) if isinstance(e, config_io.ConfigWriteError) \
                else f"Couldn't save config.yaml: {e}. Nothing was changed."
            _step("update_config", False, msg)
            return {"ok": False, "error": msg, "steps": steps}

        # ----- Step 1: stop + disable + remove the feeder service -----
        # v3.4.150: one helper call. A failure doesn't stop the switch (see
        # the docstring); the step says how to finish it by hand.
        feeder_warning = None
        try:
            privhelper.run_helper("feeder-remove")
            _step("stop_feeder_service", True,
                  "Synthetic feeder stopped, disabled, and removed.")
        except privhelper.HelperError as e:
            remove = f"sudo {privhelper.HELPER} feeder-remove"
            how = (f"run `{e.command}` on the server, then `{remove}`"
                   if isinstance(e, privhelper.HelperUnavailable) else f"run `{remove}` on the server")
            feeder_warning = (f"The demo feeder couldn't be removed ({e}). It is harmless — it "
                              f"only listens on 127.0.0.1. To remove it, {how}.")
            logger.warning("switch-to-real: %s", feeder_warning)
            _step("stop_feeder_service", False, feeder_warning)

        # ----- Step 2: nuke the demo aircraft_history database -----
        # Per design: option A (nuke). The demo DB is full of synthetic
        # sightings; carrying them into real-receiver mode would pollute
        # all-time stats forever. Delete the DB and its WAL/SHM siblings;
        # the collector will recreate on first poll.
        try:
            # v3.4.141: data.db_file, resolved like _db_conn — the old
            # CONFIG["database"]["path"] lookup never existed, so this
            # deleted a cwd-relative aircraft_history.db instead.
            db_path = (CONFIG.get("data") or {}).get(
                "db_file", "aircraft_history.db")
            if not Path(db_path).is_absolute():
                db_path = str(Path(__file__).parent / db_path)
            removed = []
            for suffix in ["", "-wal", "-shm"]:
                p = db_path + suffix
                if _os.path.exists(p):
                    _os.remove(p)
                    removed.append(p)
            _step("nuke_demo_db", True,
                  f"Removed: {', '.join(removed) if removed else '(nothing to remove)'}")
        except Exception as e:
            _step("nuke_demo_db", False, str(e))
            config_io.discard(_staged_cfg)
            return {"ok": False,
                    "error": "Failed to clear demo database.",
                    "steps": steps}

        # ----- Step 3: clear watchlist + update config -----
        # A rename of the file staged above: it needs no free space.
        try:
            with config_io.CONFIG_LOCK:
                config_io.commit(_staged_cfg, CONFIG_PATH)
            # (last-good is written once this config has started — step 4.)
            _step("update_config", True,
                  "Receiver pointed at real address, demo flag off, watchlist cleared.")
        except Exception as e:
            _step("update_config", False, str(e))
            return {"ok": False,
                    "error": "Failed to update config.yaml.",
                    "steps": steps}

        # ----- Step 4: restart aerodrome.service -----
        # v3.4.147: a just-updated version that has answered commits first,
        # so this restart can't count as a failed start and roll back to
        # the pre-switch config.yaml after the demo database is gone.
        _confirm_update_before_restart()
        try:
            privhelper.run_sudo(["systemctl", "restart", "--no-block", "aerodrome"], timeout=15)
            _step("restart_aerodrome", True,
                  "Aerodrome service restart requested.")
        except Exception as e:
            _step("restart_aerodrome", False, str(e))
            return {"ok": False,
                    "error": ("Config updated but service restart failed. "
                              "Run `sudo systemctl restart aerodrome` manually."),
                    "steps": steps}

        return {"ok": True, "error": None, "warning": feeder_warning, "steps": steps}

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
    def aircraft_detail_page(icao: str):
        # Validate format first — 6 hex chars, no exceptions
        if len(icao) != 6 or not all(c in "0123456789ABCDEFabcdef" for c in icao):
            raise HTTPException(status_code=400, detail="invalid ICAO hex")
        # Canonicalize case via redirect
        if icao != icao.upper():
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/aircraft/{icao.upper()}", status_code=301)
        return _serve_template("aircraft.html")

    @app.get("/api/aircraft/{icao}")
    def get_aircraft_detail(icao: str):
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
            d = detail_page_data_for_aircraft(conn, icao.upper(),
                                              tzwindow.resolve_tz(CONFIG))
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

    @app.get("/api/aircraft/{icao}/enrich")
    async def enrich_aircraft_registry(icao: str):
        """v3.4.58: resolve + return the registered owner and manufacturer
        for one aircraft, for the detail page's "owned by" line.

        Reads seen_aircraft first; if the owner isn't populated yet, fires
        a single hexdb forward lookup (the same call the ICAO→tail resolver
        makes — it now also captures owner/manufacturer from that response)
        with a short time budget so the request can't hang, then reads back
        the mirrored values. Deliberately kept OFF the main
        /api/aircraft/{icao} payload so the heavy detail query never blocks
        on a network call — the frontend fetches this separately and fills
        the owner line in once it returns."""
        key = (icao or "").upper().strip()
        if len(key) != 6 or not all(c in "0123456789ABCDEF" for c in key):
            raise HTTPException(status_code=400, detail="invalid ICAO hex")

        def _read():
            conn = _open_db_conn(CONFIG["data"]["db_file"])
            try:
                row = conn.execute(
                    "SELECT registration, registered_owner, manufacturer "
                    "FROM seen_aircraft WHERE icao = ?", (key,)
                ).fetchone()
            finally:
                conn.close()
            if not row:
                return None
            return {
                "registration": row[0] or "",
                "registered_owner": row[1] or "",
                "manufacturer": row[2] or "",
            }

        cur = _read()
        if cur is None:
            raise HTTPException(status_code=404, detail="aircraft not found")

        # Already enriched — return immediately, no lookup.
        if cur["registered_owner"]:
            return {"ok": True, "icao": key, **cur}

        # Not yet resolved — one forward lookup with a short budget.
        # resolve_icao_to_tail handles caching, negative-cache, and the
        # owner/manufacturer capture as a side effect; we re-read after.
        import asyncio as _asyncio
        try:
            await _asyncio.wait_for(
                _asyncio.to_thread(_collector_mod.resolve_icao_to_tail, key),
                timeout=4.0,
            )
        except _asyncio.TimeoutError:
            pass  # leave blank this round; it'll be populated on next view
        except Exception as e:
            logger.debug(f"enrich resolve failed for {key}: {e}")

        cur = _read() or cur
        return {"ok": True, "icao": key, **cur}

    def _fa_client():
        import flightaware as _flightaware
        return _flightaware.shared(_flightaware.state_path_for(CONFIG), db_path=CONFIG["data"]["db_file"])

    async def _flightaware_route(cs: str, cached_only: bool = False) -> Optional[dict]:
        """v3.4.168: the flight's real route from FlightAware AeroAPI, or None
        (no key, paused at the monthly limit, busy, not found, error) so the
        caller falls back to the free list. FlightAware's route is the
        flight's own, so it isn't checked against the plane's position.
        v3.4.170: cached_only answers only from routes already looked up,
        never spending a lookup (the display board's rows and cards)."""
        cfg = CONFIG.get("flightaware") or {}
        if not str(cfg.get("api_key") or "").strip():
            return None
        if cached_only:
            try:
                r = _fa_client().cached(cs)
            except Exception:
                r = None
            if not r:
                return None
        else:
            try:
                res = await asyncio.wait_for(asyncio.to_thread(_fa_client().lookup, cfg, cs), timeout=9.0)
            except Exception as e:
                logger.debug(f"FlightAware lookup failed for {cs}: {type(e).__name__}")
                return None
            if not isinstance(res, dict) or res.get("status") != "found":
                return None
            r = res.get("route") or {}
        o, d = r.get("origin") or {}, r.get("destination") or {}
        if not (o.get("icao") and d.get("icao")):
            return None

        def _place(ap):
            return ap.get("city") or ap.get("name") or ""
        return {"ok": True, "found": True, "source": "flightaware", "callsign": cs,
                "origin_icao": o["icao"], "origin_name": _place(o),
                "dest_icao": d["icao"], "dest_name": _place(d),
                "airline": airline_name(cs[:3]) or "",
                "airports": [{"icao": o["icao"], "name": _place(o)},
                             {"icao": d["icao"], "name": _place(d)}],
                "current_leg": None,
                "departed_at": r.get("departed_at"), "arrives_at": r.get("arrives_at")}

    @app.get("/api/flightaware/status")
    def flightaware_status():
        """v3.4.168: the Configuration box's numbers: key state (never the key;
        only its last 4 characters), this month's lookups against the limit,
        and FlightAware's own usage figure (a free call, cached 10 minutes,
        which can lag an hour)."""
        import flightaware as _flightaware
        cfg = CONFIG.get("flightaware") or {}
        client = _fa_client()
        st = client.status(cfg)
        usage = None
        if st.get("configured"):
            try:
                usage = client.usage(cfg)          # free; the client times out after 6 s
            except Exception:
                usage = None
        return {"ok": True, **st, "usage": usage,
                "usage_url": _flightaware.USAGE_URL, "signup_url": _flightaware.SIGNUP_URL}

    @app.post("/api/flightaware/test")
    def flightaware_test():
        """v3.4.168: check the saved key with FlightAware's free usage call."""
        cfg = CONFIG.get("flightaware") or {}
        if not str(cfg.get("api_key") or "").strip():
            return {"ok": False, "state": "no_key", "message": "Save a key first, then test it."}
        try:
            res = _fa_client().test_key(cfg)     # the client times out after 6 s
        except Exception:
            return {"ok": False, "state": "error",
                    "message": "FlightAware didn't answer. Try again in a minute."}
        return {"ok": res.get("state") == "ok", **res}

    @app.get("/api/callsign/{callsign}/route")
    async def get_callsign_route(callsign: str,
                                 lat: Optional[float] = None,
                                 lon: Optional[float] = None,
                                 track: Optional[float] = None,
                                 fa: Optional[str] = None):
        """v3.4.97: resolve + return the route for one flight callsign — the
        route line on the radar overlay + detail page. v3.4.109: re-sourced
        from adsbdb.com to adsb.lol's VRS standing data (the old source
        proved unreliable — display was pulled v3.4.105), which also brings
        MULTI-LEG itineraries ("KMSP-KPHL-KMSP") and per-airport coordinates.

        ADS-B carries no route; it's looked up by CALLSIGN and cached in
        route_cache (callsign-keyed, with a negative-cache for callsigns that
        have no scheduled route). Lazy: the UI calls this when an aircraft is
        opened, OFF the main payloads so a network call never blocks the
        detail/live queries. The blocking lookup runs in a thread with a
        short budget, mirroring /enrich.

        Optional lat/lon/track = the aircraft's current position + heading.
        v3.4.167: with a position, the route must FIT the plane — some leg's
        path near it and, away from the airports, the plane heading toward
        that leg's destination (route_resolver.fitting_legs). The callsign
        data is crowd-sourced and wrong for a large share of flights (on
        2026-11-06, 9 of 28 live routes didn't match the plane at all), so a
        route that doesn't fit comes back found=False, mismatch=True and the
        UI shows no route row. For a multi-leg chain the position also drives
        current-leg inference (which fitting leg this plane is flying; heading
        splits an out-and-back's two opposite legs); the UI shows just that
        leg, falling back to the full chain when inference is ambiguous.
        Without a position the route is returned unchecked; the pages only
        ask with one.

        v3.4.168: fa=1 (sent when you open an aircraft: the radar panel, the
        aircraft page, the display board's details panel) asks FlightAware
        first if a key is set, spending a lookup unless it's cached; its answer
        carries source="flightaware", departed_at and arrives_at. v3.4.170:
        fa=cached (the display board's rows and cards) uses a FlightAware route
        only if one was already looked up, and never spends a lookup.

        Returns {ok, found, callsign[, origin_icao, origin_name, dest_icao,
        dest_name, airline, airports, current_leg], cached}. origin/dest are
        the chain's first/last stops; `airports` is the full ordered chain;
        `current_leg` is {origin_icao, origin_name, dest_icao, dest_name} or
        None. found=False (the UI shows no route row) for GA/private/unknown
        callsigns, for a transient lookup failure, and (with mismatch=True)
        for a route that doesn't fit the plane's position or can't be checked
        against it (an airport without coordinates)."""
        cs = (callsign or "").upper().strip()
        # Fast-reject obvious non-callsigns before spending a thread/conn; the
        # resolver re-validates with its [A-Z0-9]{2,8} charset rule.
        if not (2 <= len(cs) <= 8) or not cs.isalnum():
            return {"ok": True, "found": False, "callsign": cs}

        fa_mode = str(fa or "").strip().lower()
        if fa_mode in ("1", "true", "yes", "cached"):
            fa_route = await _flightaware_route(cs, cached_only=(fa_mode == "cached"))
            if fa_route is not None:
                return fa_route

        import route_resolver as _route_resolver

        def _resolve():
            conn = _open_db_conn(CONFIG["data"]["db_file"])
            try:
                return _route_resolver.resolve_route(conn, cs)
            finally:
                conn.close()

        import asyncio as _asyncio
        try:
            result = await _asyncio.wait_for(_asyncio.to_thread(_resolve), timeout=7.0)
        except _asyncio.TimeoutError:
            return {"ok": True, "found": False, "callsign": cs}
        except Exception as e:
            logger.debug(f"route resolve failed for {cs}: {e}")
            return {"ok": True, "found": False, "callsign": cs}

        # Fit check + current-leg inference (pure math, no I/O — fine on the
        # loop). Leg inference only matters for a 3+-stop chain; a plain A→B
        # route IS its own leg.
        airports = result.get("airports") if result.get("found") else None
        if airports and lat is not None and lon is not None:
            if not _route_resolver.fitting_legs(airports, lat, lon, track):
                return {"ok": True, "found": False, "callsign": cs,
                        "mismatch": True}
        if airports and len(airports) > 2:
            leg = _route_resolver.pick_current_leg(airports, lat, lon, track)
            if leg is not None:
                o, d = airports[leg], airports[leg + 1]
                result["current_leg"] = {
                    "origin_icao": o["icao"], "origin_name": o["name"] or "",
                    "dest_icao": d["icao"], "dest_name": d["name"] or "",
                }
            else:
                result["current_leg"] = None
        elif airports:
            result["current_leg"] = None
        return result

    @app.get("/api/aircraft/{icao}/photo")
    async def get_aircraft_photo(icao: str):
        """v3.4.107: resolve + return an aircraft photo for one airframe — the
        thumbnail shown on the aircraft detail page.

        Keyed by ICAO hex; looked up from planespotters.net's free photo API
        (needs a descriptive User-Agent) and cached in photo_cache (with a
        negative-cache for airframes planespotters has no photo of). Lazy: the
        UI calls this when an aircraft is opened, OFF the main payloads so a
        network call never blocks the detail query. The blocking lookup runs in
        a thread with a short budget, mirroring /enrich and /route.

        Returns {ok, found, icao[, thumbnail_url, photo_link, photographer],
        cached}. found=False (the UI shows the by-type fallback link instead)
        for unphotographed airframes and for a transient lookup failure."""
        hexid = (icao or "").upper().strip()
        # Fast-reject non-hex before spending a thread/conn; the resolver
        # re-validates with its ^[0-9A-F]{6}$ rule.
        if len(hexid) != 6 or any(c not in "0123456789ABCDEF" for c in hexid):
            return {"ok": True, "found": False, "icao": hexid}

        def _resolve():
            import photo_resolver as _photo_resolver
            conn = _open_db_conn(CONFIG["data"]["db_file"])
            try:
                return _photo_resolver.resolve_photo(conn, hexid)
            finally:
                conn.close()

        import asyncio as _asyncio
        try:
            return await _asyncio.wait_for(_asyncio.to_thread(_resolve), timeout=7.0)
        except _asyncio.TimeoutError:
            return {"ok": True, "found": False, "icao": hexid}
        except Exception as e:
            logger.debug(f"photo resolve failed for {hexid}: {e}")
            return {"ok": True, "found": False, "icao": hexid}

    @app.get("/api/aircraft/{icao}/positions")
    def get_aircraft_positions(icao: str, window: str = "24h"):
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
            # Map-consistency: the detail map draws the same cyan receiver
            # marker + range rings as the radar, from the same resolved config.
            "distance_unit": (CONFIG.get("receiver", {}).get("distance_unit") or "mi").lower(),
            "show_range_rings": bool((CONFIG.get("map") or {}).get("show_range_rings", True)),
            "ring_distances": _resolved_ring_distances(),
        }

    @app.get("/diagnostics", response_class=HTMLResponse)
    def diagnostics_hub_page():
        """v2.41.23: hub page listing all available troubleshooting diagnostics.
        Each registered diagnostic appears as a card linking to its dedicated
        page. The hub itself is pure static content; the registry lives in the
        template's JavaScript so adding a new diagnostic doesn't touch server
        code except to register a route for the new page."""
        return _serve_template("diagnostics.html")

    @app.get("/diagnostics/watchlist-alerts", response_class=HTMLResponse)
    def diagnostics_watchlist_page():
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
    def diagnostics_slow_queries_page():
        """Slow-query diagnostic page (v2.84.0)."""
        return _serve_template("diagnostics-slow-queries.html")

    @app.get("/api/diagnostics/slow-queries/recent")
    def get_recent_slow_queries(limit: int = Query(50, ge=1, le=200)):
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
    def clear_slow_queries():
        """Empty the ring. Backs the diagnostic page's "Clear" button."""
        from slow_query_log import clear
        clear()
        return {"ok": True}

    @app.get("/api/diagnostics/slow-queries/explain")
    def explain_query_plan(query: str = Query(..., pattern=r"^[a-z_]+$")):
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
    def connection_tuning_audit():
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
    def get_doc(slug: str):
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
    def get_doc_asset(filename: str):
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
    def get_logs_info():
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
    def get_logs_tail(n: int = Query(500, ge=0, le=200_000)):
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
    def download_log():
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


NOTIFY_PROBE_CACHE_TTL_SEC = 300
NOTIFY_PROBE_TIMEOUT_SEC = 2


def _notify_server_probe(url: str) -> Dict[str, Any]:
    """Is the server behind notifications.url answering? One GET of ntfy's
    /v1/health on the same scheme/host/port, under the same address rules and
    no-redirect rule as a real send. Any answer below 500 counts as reachable
    (an older ntfy without /v1/health answers 404, which still proves it's
    there); the body is never read back. `server` is host:port only — the
    path can hold a private topic name and never leaves this function."""
    import requests as _rq
    from config_validator import _validate_outbound_url
    parts = urlsplit((url or "").strip())
    netloc = parts.netloc.rsplit("@", 1)[-1]      # drop any user:password
    out = {"reachable": False, "response_ms": None, "error": None, "server": netloc}
    if parts.scheme not in ("http", "https") or not parts.hostname:
        out["error"] = "the notification address isn't an http(s) address"
        return out
    if _validate_outbound_url(url, "notifications.url"):
        out["error"] = "the notification address points somewhere Aerodrome won't send to"
        return out
    t0 = time.time()
    try:
        r = _rq.get(f"{parts.scheme}://{netloc}/v1/health",
                    timeout=NOTIFY_PROBE_TIMEOUT_SEC, allow_redirects=False, stream=True)
        r.close()       # headers are enough; don't download whatever it sent
        out["response_ms"] = int((time.time() - t0) * 1000)
        if r.status_code >= 500:
            out["error"] = f"the server answered HTTP {r.status_code}"
        else:
            out["reachable"] = True
    except _rq.Timeout:
        out["error"] = f"no answer within {NOTIFY_PROBE_TIMEOUT_SEC} s"
    except _rq.ConnectionError:
        out["error"] = "couldn't connect"
    except _rq.RequestException as e:
        out["error"] = f"request failed ({type(e).__name__})"
    return out


def _short_send_error(err: Optional[str]) -> str:
    """The notifier's failure text, without anything that can carry the URL
    (a connection error's text includes the path, i.e. the topic)."""
    err = err or ""
    if err.startswith("HTTP ") or err.startswith("Blocked"):
        return err
    if err.startswith("Timeout"):
        return "no answer in time"
    if err.startswith("Connection error"):
        return "couldn't connect"
    return "the request failed"


def _notifications_check(nt_cfg: Optional[Dict[str, Any]], stats: Optional[Dict[str, Any]],
                         cache: Optional[Dict[str, Any]], now: float,
                         probe=_notify_server_probe) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """(component, cache to keep) for /api/status's `notifications`.

    Off (notifications disabled or no URL) is ok. On, it is ok when the
    server answers (probed at most every NOTIFY_PROBE_CACHE_TTL_SEC, or at
    once when the URL changes) and the last alert that was actually sent
    didn't fail. Advisory only: it can turn the gear amber, never red."""
    nt_cfg = nt_cfg or {}
    url = nt_cfg.get("url") or ""
    if not (nt_cfg.get("enabled") and url):
        return {"enabled": False, "ok": True}, cache
    if cache and cache.get("url") == url and now - cache.get("timestamp", 0) < NOTIFY_PROBE_CACHE_TTL_SEC:
        found, age = cache["data"], int(now - cache["timestamp"])
    else:
        found, age = probe(url), 0
        cache = {"url": url, "data": dict(found), "timestamp": now}
    stats = stats or {}
    sent, err = stats.get("last_sent"), stats.get("last_error")
    last_failed = None
    if err and (not sent or err.get("ts", 0) > sent.get("ts", 0)):
        last_failed = {"ts": err.get("ts"), "event": err.get("event"),
                       "error": _short_send_error(err.get("error"))}
    return {
        "enabled": True,
        "ok": bool(found.get("reachable")) and last_failed is None,
        "server": found.get("server"),
        "reachable": bool(found.get("reachable")),
        "response_ms": found.get("response_ms"),
        "error": found.get("error"),
        "probe_cached_age_sec": age,
        "last_sent_ts": (sent or {}).get("ts"),
        "last_failed": last_failed,
    }, cache


def _health_summary(core: Dict[str, bool], hexdb_ok: bool, capacity_ok: bool,
                    sudoers: Optional[Dict[str, Any]],
                    system: Optional[Dict[str, Any]],
                    notifications: Optional[Dict[str, Any]] = None,
                    flightaware: Optional[Dict[str, Any]] = None) -> Tuple[str, List[str], Optional[str]]:
    """(severity, failing, reason) for /api/status: the one place that decides
    the colour of the health dot and gear on every page, and the Status banner.

    'error' is a core component failing. 'warn' is everything still running but
    needing attention: sudoers rules out of date (ntfy and data-purge actions
    would fail), a degraded tail lookup, a disk capacity alert, or a load over
    1.5x cores for 15 minutes (not 1 minute, so it doesn't flap on a spike, and
    not 1x, which is normal on a busy tracker). `reason` says why, for the dot's
    tooltip and the Live tab's toast. `notifications` is /api/status's component
    of that name: on and not ok is a warning too (alerts aren't arriving).
    `flightaware` (v3.4.169) likewise: a key FlightAware refused is a warning
    (routes quietly fall back to the free list); reaching the monthly limit is
    not, because that's the limit doing its job."""
    notify_bad = bool(notifications and notifications.get("enabled") and not notifications.get("ok"))
    fa_bad = bool(flightaware and flightaware.get("configured") and flightaware.get("key_state") == "invalid")
    down = [name for name, ok in core.items() if not ok]
    if down:
        failing = (down + ([] if hexdb_ok else ["hexdb_resolver"]) + ([] if capacity_ok else ["capacity"])
                   + (["notifications"] if notify_bad else []) + (["flightaware"] if fa_bad else []))
        return "error", failing, ", ".join(down) + " offline"

    warnings = []
    sudoers = sudoers or {}
    if sudoers.get("can_verify") and sudoers.get("needs_refresh"):
        warnings.append(("sudoers",
                         f"sudoers refresh needed (expects v{sudoers.get('expected_version')}, "
                         f"live v{sudoers.get('live_version')}) — visit Updates page for instructions"))
    if not hexdb_ok:
        warnings.append(("hexdb_resolver", "hexdb lookup degraded — tail numbers may be missing"))
    if not capacity_ok:
        warnings.append(("capacity", "disk capacity alert — see Status"))
    if notify_bad:
        warnings.append(("notifications",
                         "notification server unreachable — alerts aren't reaching your phone"
                         if not notifications.get("reachable")
                         else "the last alert failed to send — see Status"))
    if fa_bad:
        warnings.append(("flightaware",
                         "FlightAware didn't accept the API key — routes come from the free list"))
    load = ((system or {}).get("load_average") or {}).get("15m")
    cores = (system or {}).get("cpu_cores")
    if load is not None and cores and load >= cores * 1.5:
        warnings.append(("system_load",
                         f"sustained load {load:.2f} on {cores} core{'' if cores == 1 else 's'} "
                         "— server under pressure for 15+ minutes"))
    if warnings:
        return "warn", [name for name, _ in warnings], warnings[0][1]
    return "ok", [], None


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


def _save_config(cfg=None):
    """v3.4.148: persist `cfg` (default: CONFIG) to config.yaml — all or
    nothing, comments kept when possible, through config_io under
    CONFIG_LOCK. RAISES config_io.ConfigWriteError (the file is untouched;
    .disk_full → 507) or ValueError (the config couldn't start the
    service): callers tell the user. Before, a failed save was only logged,
    so a full disk could report success with config.yaml cut short."""
    config_io.save_config(CONFIG_PATH, CONFIG if cfg is None else cfg)
    logger.info("Config saved")


def _save_config_or_response(cfg):
    """_save_config for handlers: None on success, else the error response
    ({ok: false, error, message}; 507 disk full, 400 refused, 500 other)."""
    try:
        _save_config(cfg)
        return None
    except config_io.ConfigWriteError as e:
        logger.error(f"Failed to save config: {e}")
        return JSONResponse(status_code=507 if e.disk_full else 500,
                            content={"ok": False, "error": str(e), "message": str(e)})
    except ValueError as e:
        logger.error(f"Refused to save config: {e}")
        msg = f"Couldn't save config.yaml: {e}. Nothing was changed."
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": msg, "message": msg})
