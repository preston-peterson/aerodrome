"""
Disk-capacity metrics and capacity-alert state machine.

Two responsibilities live here:

  1. _compute_capacity_metrics(db_path, retention_days) — measures the
     install's current size, daily growth, projected settled size, free
     disk, and headroom. Returns a dict consumed by /api/status (the
     Capacity card on the Status page) and /api/capacity (lighter
     endpoint for Configuration → Retention's live preview).

  2. evaluate_capacity_alerts(metrics, config, state) — given fresh
     metrics and the user's threshold config, determines whether a
     capacity-warning alert should fire (cross-below-threshold), a
     recovered alert should fire (cross-above-with-hysteresis), or
     nothing. Pure function; the caller passes in the persistent
     alert-state dict.

Module split rationale: the metrics function used to live in server.py.
collector.py (which fires the alerts on a 60s cadence in its poll
loop) imports server.py only via the FastAPI app boundary, never as
a Python module — so reaching the helper from there required a
duplicated implementation. Lifting both pieces into capacity.py and
having both server.py and collector.py import from here is the clean
fix; both modules already depend on the standard library.

Two real-world reference points anchor the bytes/row constant below:

  Quiet airspace (Bay Area suburbs, modest antenna):
    ~51k all_sightings rows/day, ~9 MB/day DB growth (87 MB / 9.8 days)

  Busy airspace (urban with high-gain antenna):
    ~715k all_sightings rows/day, ~118 MB/day DB growth (1.4 GB / 11.9 days)

Bytes-per-row is similar across both: ~165-175 bytes including indexes,
rollups, WAL. We trust the install's *measured* bytes/row when the DB has
at least 3 days of accumulated data; before then we fall back to a 170
byte default. The fallback only matters during the first 72 hours
post-install — in steady state the measurement dominates.
"""
import os
import shutil
import sqlite3
import time
from typing import Any, Dict, Optional, Tuple


CAPACITY_DEFAULT_BYTES_PER_ROW = 170  # used only when measurement isn't available

# v2.50.31: alert thresholds. The defaults are also the values that the
# UI starts with on a fresh config — same shape as the rest of the
# notifications config tree.
DEFAULT_HEADROOM_THRESHOLD = 1.2          # alert when ratio < this
DEFAULT_DISK_FREE_FLOOR_MB = 1024.0       # alert when free disk < max(this, 5% of total)
DEFAULT_DISK_FREE_PCT_FLOOR = 0.05        # 5% of total disk

# Hysteresis: once an alert fires, the condition has to recover by 10%
# above the threshold before we'll declare it cleared. Prevents flap-fire
# from values wobbling around the boundary across consecutive polls.
HYSTERESIS_FACTOR = 1.10


def _compute_capacity_metrics(db_path: str, retention_days: int = 30) -> Dict[str, Any]:
    """Compute disk-capacity metrics for the install.

    Returns a dict with current size, daily growth rate, projected
    steady-state size at current retention, free disk space, and
    headroom multiplier. All numeric values are floats in MB unless
    suffixed otherwise. Safe to call even when the DB is empty or
    very young — fields whose computation isn't possible yet are
    set to None.

    retention_days is passed in rather than read from CONFIG because
    this function lives outside the FastAPI app context and needs to
    be callable from collector.py's poll loop where CONFIG is a
    different binding.
    """
    out: Dict[str, Any] = {
        "ok": False,
        "db_size_mb": None,
        "rows_per_day": None,
        "mb_per_day": None,
        "bytes_per_row": None,
        "days_of_data": None,
        "data_source": None,        # "measured" | "estimated" | "insufficient"
        "retention_days": None,
        "projected_settled_mb": None,
        "disk_free_mb": None,
        "disk_total_mb": None,
        "headroom_ratio": None,     # disk_free / projected_settled (>1 means safe)
        "what_if": [],              # list of {days, projected_mb, headroom_ratio}
        "error": None,
    }

    try:
        if not os.path.exists(db_path):
            out["error"] = "DB file does not exist"
            return out

        # --- Current DB size and free disk (cheap; no SQL needed) ---
        st = os.stat(db_path)
        out["db_size_mb"] = round(st.st_size / (1024 * 1024), 2)

        try:
            usage = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)) or ".")
            out["disk_free_mb"] = round(usage.free / (1024 * 1024), 1)
            out["disk_total_mb"] = round(usage.total / (1024 * 1024), 1)
        except Exception:
            # Best-effort. On unusual filesystems disk_usage can fail;
            # we still want to surface the DB-side numbers.
            pass

        # --- Daily growth rate from all_sightings ---
        # We measure rows/day, then convert to MB/day via the install's
        # actual bytes/row (DB size / total rows in DB). This implicitly
        # accounts for indexes, rollups, and WAL — it's a measurement of
        # what disk grew by, not just what raw rows weigh.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            now_ts = int(time.time())
            min_ts_row = conn.execute(
                "SELECT MIN(seen_at), COUNT(*) FROM all_sightings"
            ).fetchone()
            min_ts = min_ts_row[0]
            total_rows = int(min_ts_row[1] or 0)

            if total_rows == 0 or min_ts is None:
                out["data_source"] = "insufficient"
                out["ok"] = True
                return out

            # Days actually present in DB. If less than 1 full day, the
            # extrapolation to "rows/day" is unreliable (a busy first hour
            # would multiply to absurd numbers). Treat as insufficient.
            days_present = (now_ts - min_ts) / 86400.0
            out["days_of_data"] = round(days_present, 2)
            if days_present < 1.0:
                out["data_source"] = "insufficient"
                out["ok"] = True
                return out

            # Use the rolling 7-day window if we have >= 7 days of data,
            # otherwise use everything we have.
            if days_present >= 7:
                seven_days_ago = now_ts - 7 * 86400
                recent_rows = conn.execute(
                    "SELECT COUNT(*) FROM all_sightings WHERE seen_at >= ?",
                    (seven_days_ago,)
                ).fetchone()[0]
                out["rows_per_day"] = round(int(recent_rows) / 7.0, 0)
            else:
                out["rows_per_day"] = round(total_rows / days_present, 0)

            # Bytes/row: DB size / total rows. Trustworthy once the DB has
            # been running for a few days and rollups have stabilized;
            # before then fall back to the documented average.
            if days_present >= 3:
                out["bytes_per_row"] = round(st.st_size / total_rows, 1)
                out["data_source"] = "measured"
            else:
                out["bytes_per_row"] = CAPACITY_DEFAULT_BYTES_PER_ROW
                out["data_source"] = "estimated"

            out["mb_per_day"] = round(
                out["rows_per_day"] * out["bytes_per_row"] / (1024 * 1024), 2
            )

            # --- Projection at current retention ---
            out["retention_days"] = retention_days
            out["projected_settled_mb"] = round(
                out["mb_per_day"] * retention_days, 1
            )

            if out["disk_free_mb"] and out["projected_settled_mb"] > 0:
                # Headroom is computed against (free + currently-allocated DB)
                # since the DB itself is part of the disk's used space — if
                # the user shrinks retention, the DB releases the difference.
                effective_free = out["disk_free_mb"] + out["db_size_mb"]
                out["headroom_ratio"] = round(
                    effective_free / out["projected_settled_mb"], 2
                )

            # --- What-if: same projection at other retention values ---
            for d in (7, 14, 30, 60, 90, 180):
                projected = round(out["mb_per_day"] * d, 1)
                entry = {"days": d, "projected_mb": projected}
                if out["disk_free_mb"]:
                    effective_free = out["disk_free_mb"] + out["db_size_mb"]
                    entry["headroom_ratio"] = round(effective_free / projected, 2) \
                        if projected > 0 else None
                else:
                    entry["headroom_ratio"] = None
                out["what_if"].append(entry)

            out["ok"] = True
        finally:
            conn.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def _resolve_disk_free_floor(metrics: Dict[str, Any], floor_mb: float,
                             pct_floor: float) -> float:
    """Effective free-disk floor: the larger of an absolute MB floor and
    a percentage of the total disk. Adapts to install scale — a 1 GB
    floor is fine on a 32 GB SD card, absurd on an 80 GB VPS slice.
    Falls back to absolute floor when total is unavailable."""
    abs_floor = float(floor_mb)
    total = metrics.get("disk_total_mb")
    if total and total > 0:
        return max(abs_floor, total * pct_floor)
    return abs_floor


def evaluate_capacity_alerts(metrics: Dict[str, Any], alert_cfg: Dict[str, Any],
                              prev_state: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Decide whether to fire a capacity alert.

    Inputs:
      metrics    — fresh dict from _compute_capacity_metrics()
      alert_cfg  — notifications.capacity sub-tree from CONFIG
      prev_state — the persisted alert state dict from the last call.
                   Shape: {"alert_active": bool, "reason": str|None,
                           "fired_at": int|None}

    Returns (new_state, action) where action is None or
      {"kind": "fire" | "recovered",
       "reason": str,
       "headroom": float|None,
       "disk_free_mb": float|None,
       "disk_total_mb": float|None,
       "free_floor_mb": float}

    State semantics:
      - Not active + condition tripped → fire alert, mark active.
      - Active + condition still tripped → suppress (idempotent).
      - Active + condition recovered (with hysteresis) → fire recovered
        alert if recovery_notification enabled, mark inactive.
      - Not active + condition not tripped → no action.

    Pure function — does NOT touch disk, network, or notifier. The
    caller is responsible for persisting new_state and dispatching
    notifications via Notifier.notify().
    """
    new_state = dict(prev_state) if prev_state else {
        "alert_active": False,
        "reason": None,
        "fired_at": None,
    }

    # Honor master enabled toggle. When disabled, just clear any prior
    # active state so we don't emit a "recovered" the next time someone
    # turns it back on. Simpler to think about than "remember state
    # across enable/disable cycles."
    if not alert_cfg.get("enabled", True):
        if new_state.get("alert_active"):
            new_state["alert_active"] = False
            new_state["reason"] = None
            new_state["fired_at"] = None
        return new_state, None

    # Need at least the basic metrics. If the DB is too young to compute
    # headroom, we have nothing to alert on — bail without changing state.
    if not metrics.get("ok") or metrics.get("data_source") == "insufficient":
        return new_state, None

    headroom_threshold = float(alert_cfg.get("headroom_threshold",
                                             DEFAULT_HEADROOM_THRESHOLD))
    floor_mb = float(alert_cfg.get("disk_free_floor_mb",
                                    DEFAULT_DISK_FREE_FLOOR_MB))
    pct_floor = float(alert_cfg.get("disk_free_pct_floor",
                                     DEFAULT_DISK_FREE_PCT_FLOOR))
    effective_floor = _resolve_disk_free_floor(metrics, floor_mb, pct_floor)

    headroom = metrics.get("headroom_ratio")
    disk_free = metrics.get("disk_free_mb")

    # Determine whether each individual condition is tripped. Either is
    # enough to fire — they answer different questions (planning vs
    # imminent). A failed metric (None) is treated as "not tripped" so
    # that a transient measurement gap doesn't generate spurious alerts.
    headroom_tripped = (headroom is not None and headroom < headroom_threshold)
    disk_tripped = (disk_free is not None and disk_free < effective_floor)
    any_tripped = headroom_tripped or disk_tripped

    if any_tripped:
        reasons = []
        if headroom_tripped:
            reasons.append(f"headroom {headroom:.2f}× below {headroom_threshold:.2f}× threshold")
        if disk_tripped:
            reasons.append(f"free disk {disk_free:.0f} MB below {effective_floor:.0f} MB floor")
        reason_str = "; ".join(reasons)

        if not new_state.get("alert_active"):
            new_state["alert_active"] = True
            new_state["reason"] = reason_str
            new_state["fired_at"] = int(time.time())
            action = {
                "kind": "fire",
                "reason": reason_str,
                "headroom": headroom,
                "disk_free_mb": disk_free,
                "disk_total_mb": metrics.get("disk_total_mb"),
                "free_floor_mb": effective_floor,
            }
            return new_state, action
        # Already active — update reason text in case the tripped
        # conditions changed (e.g. headroom recovered but free disk
        # dropped). No notification, but the message in case of a
        # later recovery should reflect the latest state.
        new_state["reason"] = reason_str
        return new_state, None

    # Nothing tripped (with hysteresis). If we were active, decide
    # whether the recovery margin has cleared. The recovery threshold
    # for the headroom case is `threshold * HYSTERESIS_FACTOR`; for
    # disk it's `floor * HYSTERESIS_FACTOR`. ALL conditions that could
    # have tripped need to be cleared by hysteresis margin to declare
    # the alert resolved.
    if new_state.get("alert_active"):
        recovery_headroom_ok = (headroom is None or
                                 headroom >= headroom_threshold * HYSTERESIS_FACTOR)
        recovery_disk_ok = (disk_free is None or
                             disk_free >= effective_floor * HYSTERESIS_FACTOR)
        if recovery_headroom_ok and recovery_disk_ok:
            new_state["alert_active"] = False
            new_state["reason"] = None
            new_state["fired_at"] = None
            if alert_cfg.get("recovery_notification", True):
                action = {
                    "kind": "recovered",
                    "reason": "capacity within thresholds",
                    "headroom": headroom,
                    "disk_free_mb": disk_free,
                    "disk_total_mb": metrics.get("disk_total_mb"),
                    "free_floor_mb": effective_floor,
                }
                return new_state, action
        # Still in the hysteresis band: keep alert_active true, no
        # notification, no flap.
        return new_state, None

    # Not active and not tripped — nothing to do.
    return new_state, None
