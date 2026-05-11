"""
Shared hexdb tail-resolver queue and workers (v2.50.38).

Up to v2.50.37, the hexdb tail-resolver lived inside server.py's
get_app() — the queue, worker thread, and seen-set were all closure-
scoped variables. That meant only the FastAPI request handlers could
push ICAOs in for resolution. The collector, which is the actual
source of every aircraft we ever see, had no way to enqueue.

Result on real installs: 98% of aircraft NEVER got a tail-resolution
attempt, because they were only resolved when the user happened to
have a tab open showing them. Aircraft that transit quickly through
the airspace, or that the user wasn't watching for, simply stayed
unresolved forever.

This module fixes that by hoisting the queue/worker into a shared
module that both the collector and server.py can import and use:

  - The collector enqueues every newly-seen ICAO at fetch-and-store
    time (see collector.fetch_and_store).
  - The server still uses /api/resolve-tail's inline + queued path
    for tabs that need urgent resolution (Live, Watchlist, etc).
  - Both producers feed the same single worker, which calls
    collector.resolve_icao_to_tail at ~2 req/sec.

Architecture notes:

  - Two queues, primary and backfill. Primary is for "active"
    resolution requests (user has a tab open, collector saw a NEW
    aircraft). Backfill is for "catch up on aircraft we never got
    around to" — drained at lower priority and only when primary
    is empty. Keeps fresh aircraft prompt while still chipping
    away at the historical backlog.
  - Single worker thread for both queues. ~2 req/sec respect for
    hexdb.io's free API. The rate isn't more aggressive than
    before — we're just better-utilized.
  - Seen-set is in-memory only (resets on restart). The hexdb_cache
    table is the persistent dedupe layer; the in-memory set is just
    a fast-path optimization to avoid re-hitting the DB cache check
    for every queued ICAO.
"""
import logging
import queue
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


# Worker rate. ~2 req/sec is the historical pace from server.py.
# hexdb.io is a free API with no published rate limit — this rate
# is "polite respect" rather than a hard ceiling.
_WORKER_SLEEP_SEC = 0.5


# Queues. Primary serves the hot path (UI requests, freshly-seen
# aircraft from the collector). Backfill serves "catch up on the
# historical gap" — lower priority, drained only when primary is empty.
# Bounded to defend against runaway producers; queue_full is logged
# and the ICAO is dropped (next sighting will re-enqueue).
_PRIMARY_QUEUE_CAP = 5000
_BACKFILL_QUEUE_CAP = 50000  # bigger because it's filled once during backfill
_primary: queue.Queue = queue.Queue(maxsize=_PRIMARY_QUEUE_CAP)
_backfill: queue.Queue = queue.Queue(maxsize=_BACKFILL_QUEUE_CAP)


# In-memory seen-set. Process-local; resets on restart. The DB cache
# in hexdb_cache is the source of truth for "have we tried this ICAO
# before"; this set is a fast-path so we don't pile the same ICAO into
# the queue many times in one process life.
_seen: set = set()
_seen_lock = threading.Lock()


# Worker reference — set when start_worker() is called. Idempotent —
# if start_worker() is called twice, the second call is a no-op (which
# can happen because both server.py and collector.py call it during
# their startup, and depending on launch order either could be first).
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def enqueue(icao: str, *, primary: bool = True) -> bool:
    """Enqueue an ICAO for hexdb resolution.

    primary=True (default): pushes to the primary queue. Use for hot
        path — UI tabs and collector-side fresh-sightings.
    primary=False: pushes to the backfill queue. Use for the one-time
        catch-up of historical seen_aircraft rows that were never
        queried.

    Returns True if the ICAO was newly enqueued, False if it was
    already in the seen-set (already queued or already attempted in
    this process). Safe to call from any thread.

    Does NOT check the persistent hexdb_cache — that check happens
    inside collector.resolve_icao_to_tail when the worker actually
    pulls the ICAO. Reason: cache check requires a DB connection,
    and we don't want every enqueue caller to need one. The in-memory
    seen-set is enough to avoid the obvious duplicate-enqueue case.
    """
    if not icao:
        return False
    icao = icao.upper()
    with _seen_lock:
        if icao in _seen:
            return False
        try:
            (_primary if primary else _backfill).put_nowait(icao)
            _seen.add(icao)
            return True
        except queue.Full:
            # Primary saturation is a real concern (5000 = ~40min of work);
            # backfill saturation is unlikely (50k cap is well past any
            # plausible install size). Either way: log and drop. The next
            # sighting (or backfill-restart) will re-enqueue.
            logger.warning(
                f"hexdb resolver {'primary' if primary else 'backfill'} "
                f"queue full ({_PRIMARY_QUEUE_CAP if primary else _BACKFILL_QUEUE_CAP}); dropping {icao}"
            )
            return False


def queue_stats() -> dict:
    """Snapshot of queue state for /api/resolve-tail/debug and Status."""
    return {
        "primary_depth": _primary.qsize(),
        "backfill_depth": _backfill.qsize(),
        "seen_lifetime": len(_seen),
        "primary_cap": _PRIMARY_QUEUE_CAP,
        "backfill_cap": _BACKFILL_QUEUE_CAP,
    }


def start_worker() -> None:
    """Idempotent: starts the background worker if it isn't running.

    Safe to call multiple times — only the first call actually creates
    the thread. Both server.py (during get_app) and collector.py
    (during init or first fetch) can call this without coordination.
    """
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return  # already running
        _worker_thread = threading.Thread(
            target=_worker_loop,
            name="hexdb-resolver-worker",
            daemon=True,
        )
        _worker_thread.start()
        logger.info(
            f"hexdb resolver worker started "
            f"(rate ~{1.0/_WORKER_SLEEP_SEC:.1f} req/sec, "
            f"primary cap {_PRIMARY_QUEUE_CAP}, backfill cap {_BACKFILL_QUEUE_CAP})"
        )


def _worker_loop() -> None:
    """Worker loop: drain primary first, fall through to backfill when
    primary is empty. Sleep ~0.5s between resolutions to be polite
    to hexdb.io's free API."""
    # Late import: collector imports this module, so we can't import
    # collector at module load. The worker only ever runs after
    # everything has imported, so a deferred import is safe.
    try:
        from collector import resolve_icao_to_tail
    except ImportError as e:
        logger.error(f"hexdb resolver worker: cannot import collector: {e}")
        return

    while True:
        icao = _next_icao()
        if icao is None:
            # Both queues empty — wait briefly then re-check
            time.sleep(1.0)
            continue
        try:
            resolve_icao_to_tail(icao)
        except Exception as e:
            logger.warning(f"hexdb resolver worker: error resolving {icao}: {e}")
        time.sleep(_WORKER_SLEEP_SEC)


def _next_icao() -> Optional[str]:
    """Get the next ICAO to resolve. Primary queue wins over backfill.
    Non-blocking — returns None if both are empty."""
    try:
        return _primary.get_nowait()
    except queue.Empty:
        pass
    try:
        return _backfill.get_nowait()
    except queue.Empty:
        return None


def backfill_unresolved(db_path: str, max_enqueue: int = 50000) -> int:
    """One-time scan: enqueue every seen_aircraft row that isn't in
    hexdb_cache.

    Called by main.py at startup (after migration). Returns the number
    of ICAOs enqueued. Uses the backfill queue (lower priority) so
    fresh aircraft from the collector aren't blocked behind the
    historical catch-up.

    On a fresh install with empty seen_aircraft, this is a no-op.
    On an install upgrading from v2.50.37 with thousands of unqueried
    aircraft, this is what closes the historical data gap.
    """
    import sqlite3
    n = 0
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute("""
            SELECT s.icao FROM seen_aircraft s
            WHERE s.icao NOT IN (SELECT icao FROM hexdb_cache)
            ORDER BY s.last_seen_at DESC
            LIMIT ?
        """, (max_enqueue,))
        for (icao,) in cur:
            if enqueue(icao, primary=False):
                n += 1
        conn.close()
    except Exception as e:
        logger.warning(f"hexdb backfill_unresolved scan failed: {e}")
        return n
    if n > 0:
        # Estimate time to drain: ~2 req/sec → minutes-to-hours
        eta_min = n * _WORKER_SLEEP_SEC / 60
        logger.info(
            f"hexdb backfill: enqueued {n} unresolved ICAOs "
            f"(estimated {eta_min:.0f} min to drain at current rate)"
        )
    return n
