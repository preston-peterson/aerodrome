"""
Process-local ring buffer for slow queries (v2.84.0).

The Stats endpoint has had a `Stats slow query: ... took Nms` warning
in the file log since v2.41.x — useful when triaging from `journalctl`,
useless when triaging from a browser. This module hoists that signal
into a structured in-memory ring so a diagnostics page can render it
without anyone needing SSH access.

Design constraints:

  - Process-local. The ring resets on restart. That's fine: this is a
    triage aid, not an audit log. The file log is still authoritative
    for after-the-fact forensics.
  - Bounded. Default 200 entries. A pathological install that emits
    a slow query every second still only keeps the last 200 — old
    entries fall off without unbounded memory growth.
  - Thread-safe. The collector and FastAPI handlers run in different
    threads; the ring needs a lock. Held only during append/iterate,
    not during query execution.
  - Cheap to record. Slow queries are rare by definition; the lock
    contention is negligible. The wrapper around `time.time()` and a
    list operation is not a hot path.
  - No persistence. If a long-running diagnostic question needs
    persistence, file log + grep is the right tool. We're trying to
    answer "what slow query just hit me?" not "what was slow last
    Tuesday?".

The shape of each recorded entry is fixed (see `record_slow_query`)
so the JSON the diagnostics page consumes stays stable. New optional
fields can be added by callers; the consumer ignores unknown keys.
"""

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


# Ring capacity. 200 is enough to cover an active session of clicks
# without losing context, and small enough that memory cost is trivial
# (roughly 200KB at ~1KB per entry including plan output).
_RING_CAPACITY = 200

# Default threshold for what "slow" means. Endpoints can override per
# call; this is just the global fallback. Matches the Stats endpoint's
# pre-existing SLOW_QUERY_MS = 500.
DEFAULT_SLOW_MS = 500

_ring: Deque[Dict[str, Any]] = deque(maxlen=_RING_CAPACITY)
_lock = threading.Lock()


def record_slow_query(
    *,
    endpoint: str,
    label: str,
    duration_ms: float,
    sql: Optional[str] = None,
    params: Optional[Any] = None,
    rows_returned: Optional[int] = None,
    plan: Optional[List[str]] = None,
) -> None:
    """Append one slow-query entry to the ring.

    Caller decides what counts as slow — pass through unconditionally
    if you've already filtered. The recorded `ts_ms` is set here so all
    entries share one clock source.

    `sql` and `params` should be the raw values the caller passed to
    SQLite. They're stored as-is for the diagnostic UI to display.
    Callers handling secret-bearing parameters (none in this codebase
    today, but worth noting) would need to redact before passing.
    """
    entry = {
        "ts_ms": int(time.time() * 1000),
        "endpoint": endpoint,
        "label": label,
        "duration_ms": round(float(duration_ms), 1),
        "sql": sql,
        "params": _safe_repr(params) if params is not None else None,
        "rows_returned": rows_returned,
        "plan": plan,
    }
    with _lock:
        _ring.append(entry)


def recent_slow_queries(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return the most recent N slow-query entries, newest first.

    `limit=None` returns everything in the ring (up to capacity).
    The returned list is a snapshot copy — safe to mutate.
    """
    with _lock:
        # `deque` iterates oldest→newest; reverse so newest is first.
        items = list(reversed(_ring))
    if limit is not None:
        items = items[:limit]
    return items


def clear() -> None:
    """Empty the ring. Used by the diagnostic UI's "Clear" button."""
    with _lock:
        _ring.clear()


def capacity() -> int:
    """Expose the ring capacity for the diagnostic UI's stat display."""
    return _RING_CAPACITY


def time_query(
    conn: Any,
    sql: str,
    params: Any = (),
    *,
    endpoint: str,
    label: str,
    threshold_ms: float = DEFAULT_SLOW_MS,
    fetch: str = "all",
) -> Any:
    """Execute a query, time it, and record it to the ring if slow.

    Drop-in for `conn.execute(sql, params).fetchall()` patterns. Returns
    the query result. Captures EXPLAIN QUERY PLAN automatically when the
    query exceeds the threshold so the diagnostic UI doesn't need a
    second round-trip to render the plan — the plan is captured at the
    moment the query was actually slow, against the same data.

    `fetch` controls how the result is materialized:
      - "all"  : fetchall() (default — caller usually iterates)
      - "one"  : fetchone()
      - "none" : no fetch (used for INSERT/UPDATE/DELETE)

    Plan capture is cheap: it runs against the same connection and
    SQLite returns the plan in microseconds. We capture it inside the
    `if slow` branch so non-slow queries pay zero plan-capture cost.

    Threading note: this routine doesn't lock around the SQLite call
    itself — that's the caller's connection's responsibility. We only
    lock around the ring append (inside record_slow_query).
    """
    t0 = time.time()
    cur = conn.execute(sql, params)
    if fetch == "all":
        result = cur.fetchall()
    elif fetch == "one":
        result = cur.fetchone()
    elif fetch == "none":
        result = None
    else:
        raise ValueError(f"unknown fetch mode: {fetch!r}")
    duration_ms = (time.time() - t0) * 1000
    if duration_ms >= threshold_ms:
        plan = _capture_plan(conn, sql, params)
        rows = None
        if fetch == "all" and result is not None:
            rows = len(result)
        elif fetch == "one":
            rows = 1 if result else 0
        record_slow_query(
            endpoint=endpoint,
            label=label,
            duration_ms=duration_ms,
            sql=sql,
            params=params,
            rows_returned=rows,
            plan=plan,
        )
    return result


def _capture_plan(conn: Any, sql: str, params: Any) -> Optional[List[str]]:
    """Run EXPLAIN QUERY PLAN and return its `detail` column as a list.

    Best-effort: if the EXPLAIN itself fails for any reason (e.g. the
    query is a multi-statement script that EXPLAIN doesn't accept), we
    silently return None rather than letting a diagnostic-side failure
    take down the production query path.
    """
    try:
        plan_rows = conn.execute(
            "EXPLAIN QUERY PLAN " + sql, params
        ).fetchall()
        return [row[3] if len(row) >= 4 else str(row) for row in plan_rows]
    except Exception:
        return None


def _safe_repr(obj: Any) -> str:
    """Stringify params in a bounded way.

    Tuples of small scalars are typical (icao, from_ts, to_ts, limit,
    offset). We render them straight. If something unexpected lands here
    we cap the length so the ring entry never balloons.
    """
    s = repr(obj)
    if len(s) > 500:
        s = s[:500] + "...<truncated>"
    return s
