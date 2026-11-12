"""Automatic updates (v3.4.180): when a scheduled run is due, and what happened to the
last one.

At the configured day and time (updates.github.auto_apply, in the stats timezone)
server.py checks GitHub and, when the latest release is newer, installs it through the
same steps as the Updates page's Apply button. This module holds the rules and a small
JSON state file next to the database:

- A run happens in the hour after the set time, once per scheduled day. If Aerodrome
  was off for that whole hour it waits for the next scheduled day, so it never updates
  in the middle of the afternoon.
- `pending` is written just before the switch-in and restart. After the restart (or a
  rollback) the updater's own result file says how it went; `reconcile` reads it.
- A release that was put back, or that the apply refused, goes on the `skip` list and
  is never installed automatically again. A newer release is.
- `announce` holds the push message for a result until it can go out (quiet hours).

Stdlib only, plus the repo's own updater and atomic_io (both stdlib only).
"""
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import atomic_io
import tzwindow
import updater

STATE_NAME = ".auto-update-state.json"
DAYS = ("daily", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY = {d: i for i, d in enumerate(DAYS[1:])}      # Python: Monday is 0
DEFAULT_DAY = "daily"
DEFAULT_TIME = "03:00"
WINDOW_SEC = 3600            # a run starts in the hour after the set time
PENDING_GIVE_UP_SEC = 3600   # no result an hour after the restart: say so and move on
ANNOUNCE_MAX_AGE_SEC = 86400
SKIP_KEEP = 5


def parse_hhmm(s) -> Optional[tuple]:
    """'03:00' → (3, 0); None for anything that isn't a 24-hour HH:MM."""
    if not isinstance(s, str) or len(s) != 5 or s[2] != ":":
        return None
    hh, mm = s[:2], s[3:]
    if not (hh.isdigit() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    return (h, m) if h < 24 and m < 60 else None


def settings(github_cfg: Optional[dict]) -> dict:
    """updates.github.auto_apply with defaults: off, every day, 03:00. A value the
    validator would refuse falls back to its default."""
    a = (github_cfg or {}).get("auto_apply")
    a = a if isinstance(a, dict) else {}
    day = a.get("day") if a.get("day") in DAYS else DEFAULT_DAY
    hhmm = a.get("time") if parse_hhmm(a.get("time")) else DEFAULT_TIME
    return {"enabled": a.get("enabled") is True, "day": day, "time": hhmm}


def _starts(now_ts: float, day: str, hhmm: str, tz, first: int, last: int):
    """Scheduled start times (unix seconds) from `first` to `last` days after today,
    in order, on the scheduled weekday(s)."""
    h, m = parse_hhmm(hhmm) or parse_hhmm(DEFAULT_TIME)
    today = tzwindow.local_dt(now_ts, tz).date()
    for back in range(first, last + 1):
        d = today + timedelta(days=back)
        if day != "daily" and d.weekday() != _WEEKDAY.get(day):
            continue
        yield d.isoformat(), datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()


def due_run(now_ts: float, day: str, hhmm: str, last_run_date: Optional[str], tz=None) -> Optional[str]:
    """The scheduled date (YYYY-MM-DD) to run now, or None."""
    for run_date, start in _starts(now_ts, day, hhmm, tz, -1, 0):
        if start <= now_ts < start + WINDOW_SEC and run_date != last_run_date:
            return run_date
    return None


def next_run(now_ts: float, day: str, hhmm: str, last_run_date: Optional[str], tz=None) -> Optional[int]:
    """When the next run starts (unix seconds): a run that's due now counts as now."""
    for run_date, start in _starts(now_ts, day, hhmm, tz, -1, 8):
        if now_ts < start + WINDOW_SEC and run_date != last_run_date:
            return int(max(start, now_ts))
    return None


# ── state file ────────────────────────────────────────────────────────────

def state_path(db_file) -> Path:
    """Next to the database, like the daily summary's state: it follows the data
    directory, and no release ever contains it, so an update leaves it alone."""
    return Path(db_file).resolve().parent / STATE_NAME


def load_state(path) -> dict:
    return atomic_io.read_json(path) or {}


def save_state(path, state: dict) -> None:
    atomic_io.write_json_atomic(path, state)


def version(v) -> str:
    """'v3.4.180' → '3.4.180'; '' for anything that isn't a release version."""
    p = updater.parse_version(v)
    return ".".join(map(str, p)) if p else ""


def skips(state: dict, v) -> bool:
    """True when release `v` is never installed automatically (it was put back or refused)."""
    return bool(version(v)) and version(v) in (state.get("skip") or [])


def _skip(state: dict, v) -> None:
    v = version(v)
    if v:
        kept = [s for s in (state.get("skip") or []) if s != v]
        state["skip"] = (kept + [v])[-SKIP_KEEP:]


def record(state: dict, result: str, now_ts: float, frm="", to="", reason="") -> None:
    """The card's "Last automatic" line, and the push message when the result has one."""
    state["last"] = {"at": int(now_ts), "result": result, "from": version(frm) or str(frm or ""),
                     "to": version(to) or str(to or ""), "reason": str(reason or "")[:500]}
    msg = message(state["last"])
    if msg:
        state["announce"] = {"title": msg[0], "body": msg[1], "at": int(now_ts)}


def message(last: dict) -> Optional[tuple]:
    """(title, body) of the push for a result, or None (no push for passing problems:
    they're tried again at the next scheduled time)."""
    r, frm, to, reason = last.get("result"), last.get("from"), last.get("to"), last.get("reason")
    if r == "installed":
        return (f"Aerodrome updated itself to {to}",
                f"Aerodrome installed {to} automatically. It was on {frm}.")
    if r == "rolled_back":
        return (f"Aerodrome put {frm} back",
                f"{to} didn't start, so Aerodrome put {frm} back. Automatic updates will skip {to} "
                f"and wait for the next release.")
    if r == "refused":
        return (f"Aerodrome didn't update itself to {to}",
                f"{reason} Automatic updates won't try {to} again: open the Updates page to "
                f"install it by hand.")
    return None


def reconcile(state: dict, last_result: Optional[dict], update_pending, running: str,
              now_ts: float) -> bool:
    """Turn a pending automatic install into its result once the updater has one.
    Returns True when `state` changed. While the update is still being confirmed
    (its journal exists), wait: the update guard decides."""
    p = state.get("pending")
    if not p or update_pending:
        return False
    lr = last_result or {}
    to, frm, at = p.get("to"), p.get("from"), p.get("at") or 0
    if version(lr.get("to_version")) == version(to) and (lr.get("at") or 0) >= at:
        res = lr.get("result")
        if res == "applied":
            record(state, "installed", lr["at"], frm, to)
        elif res == "rolled_back":
            _skip(state, to)
            record(state, "rolled_back", lr["at"], frm, to, lr.get("reason"))
        elif res == "refused":
            _skip(state, to)
            record(state, "refused", lr["at"], frm, to, lr.get("message"))
        else:
            return False
    elif version(running) == version(to):
        record(state, "installed", now_ts, frm, to)
    elif now_ts - at > PENDING_GIVE_UP_SEC:
        record(state, "failed", now_ts, frm, to,
               "Aerodrome restarted but no result was recorded for the update; see the log.")
    else:
        return False
    state["pending"] = None
    return True


def take_announcement(state: dict, now_ts: float) -> Optional[dict]:
    """The waiting push message, removed from `state`; None when there's none or it's
    more than a day old (dropped)."""
    a = state.pop("announce", None)
    if a and now_ts - (a.get("at") or 0) <= ANNOUNCE_MAX_AGE_SEC:
        return a
    return None


def card(state: dict, cfg: dict, channel_enabled: bool, latest, now_ts: float, tz=None) -> dict:
    """What the Updates page's GitHub card shows about automatic updates."""
    active = bool(channel_enabled and cfg["enabled"])
    p = state.get("pending")
    return {
        "enabled": cfg["enabled"], "active": active, "day": cfg["day"], "time": cfg["time"],
        "next_ts": next_run(now_ts, cfg["day"], cfg["time"], state.get("last_run_date"), tz) if active else None,
        "skips_latest": skips(state, latest),
        "installing": version(p.get("to")) if p else None,
        "last": state.get("last"),
    }
