"""
notifier.py — Aerodrome push notifications via ntfy.

Sends notifications to a configured ntfy URL (public ntfy.sh or self-hosted)
with per-event cooldowns, overall rate limiting, quiet hours, and an
in-memory log of recent notifications for UI display.

Key design choices:
  - Best-effort delivery. Network failures are logged but never raised — a
    broken notification pipeline must not break the collector.
  - In-memory state only. Cooldown timers and the per-hour send counter
    reset on service restart; that's acceptable for a home-scale tool and
    avoids a persistence layer.
  - Thread-safe. The collector runs in its own thread and may call
    notify() concurrently with API handlers (for the /test endpoint).
    A single lock guards all mutable state.
  - Event gating happens inside notify(). Callers just describe what
    happened; notifier decides whether/when to actually POST.

Public API:
  notifier = Notifier(config)       — construct from config dict
  notifier.update_config(config)    — apply a live config change
  notifier.notify(event, title, body, priority=None, tags=None, aircraft_icao=None)
                                     — attempt to send; returns bool sent
  notifier.send_test(url=None)       — bypass gating, test the URL
  notifier.recent(limit=20)          — read the recent-notifications log
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

log = logging.getLogger("aerodrome.notifier")

# ntfy's accepted priority names in order. If a caller passes a level we
# default to "default" rather than raising.
VALID_PRIORITIES = {"min", "low", "default", "high", "max"}

# Known event types. Callers pass one of these; anything else is rejected
# with a warning log (not raised — we don't want a typo to kill a notify
# path inside the collector).
KNOWN_EVENTS = {
    "receiver_offline", "receiver_recovered", "watchlist_hit",
    "new_record", "special_aircraft", "daily_summary",
    # v2.48.0: fires when an aircraft first becomes visible with an
    # emergency squawk (7500/7600/7700) OR transitions into an emergency
    # squawk from a different code. Per-ICAO cooldown applies; defaults
    # to 60 minutes (longer than watchlist's 10) because emergency codes
    # typically persist, and re-alerting every minute while the aircraft
    # is still squawking emergency would be noise, not signal.
    "emergency_squawk",
    # v2.50.31: capacity / disk-planning alerts. capacity_low fires once
    # when headroom or free disk crosses below the configured threshold;
    # capacity_recovered fires once when the condition clears (with
    # hysteresis to prevent flap-fire). State machine in capacity.py;
    # collector calls it once per poll.
    "capacity_low", "capacity_recovered",
    # v3.0.2: GitHub-Releases update channel notification. Fires once when
    # the background scheduler discovers a strictly-newer release than
    # what's currently known (transition event, not a per-tick spam).
    # Opt-in via both updates.github.notify.ntfy AND
    # notifications.events.update_available — same two-key gate other
    # ntfy events use. Default cooldown is 24h so a discovered update
    # doesn't re-fire every poll until the user applies it.
    "update_available",
    # "test" is a pseudo-event used by send_test() — bypasses gating.
    "test",
}

# Events that are NEVER suppressed by the per-aircraft cooldown, regardless
# of config — they're either rare enough not to need cooldowns, or carry
# no ICAO context.
NEVER_COOLDOWN = {"receiver_offline", "receiver_recovered", "new_record",
                  "daily_summary", "capacity_low", "capacity_recovered",
                  "update_available", "test"}


def _to_latin1_safe(s: str) -> str:
    """Ensure a string is safely representable as a latin-1 (ISO-8859-1)
    HTTP header value. Characters outside latin-1 (em-dash, ellipsis, smart
    quotes, emoji, CJK, etc) get replaced with a close ASCII approximation
    where one exists, or '?' as a last resort. Preserves all latin-1
    characters as-is (accented Latin letters, \u00a3, \u00a9, etc).

    A dedicated map handles the cases most likely to show up in our own
    code paths \u2014 dashes and ellipses copied from Markdown-styled source
    \u2014 so they render as intended rather than as '?'. Anything else
    outside latin-1 falls through to the encode(errors='replace') path.

    Why not UTF-8? HTTP headers per RFC 7230 are latin-1 only. RFC 8187
    defines an encoded-parameter syntax for UTF-8 header values but ntfy
    doesn't advertise support for it. Staying within latin-1 is the safe
    interoperable choice.
    """
    if not s:
        return s
    replacements = {
        "\u2013": "-",   # en dash
        "\u2014": "-",   # em dash
        "\u2015": "-",   # horizontal bar
        "\u2212": "-",   # minus sign
        "\u2026": "...", # horizontal ellipsis
        "\u2018": "'",   # left single quote
        "\u2019": "'",   # right single quote / apostrophe
        "\u201C": '"',   # left double quote
        "\u201D": '"',   # right double quote
        "\u00a0": " ",   # NBSP \u2014 latin-1-safe but normalize anyway
        "\u2022": "*",   # bullet
        "\u2192": "->",  # right arrow
        "\u2190": "<-",  # left arrow
        "\u2713": "v",   # check mark
        "\u2717": "x",   # ballot x
    }
    out = []
    for ch in s:
        if ord(ch) < 256:
            out.append(ch)
        elif ch in replacements:
            out.append(replacements[ch])
        else:
            # Anything else \u2014 encode/decode through ASCII with
            # replace so we don't crash at the socket layer.
            out.append(ch.encode("ascii", "replace").decode("ascii"))
    return "".join(out)


class Notifier:
    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 stats_timezone: Optional[str] = None,
                 demo_enabled: bool = False):
        """Build from a config dict matching the notifications: block in
        config.yaml. Unknown/missing values fall back to sensible defaults.

        stats_timezone is passed separately because quiet-hours windows
        evaluate in the same timezone users see on the Stats page.

        demo_enabled (v3.1.0): when true, all outgoing notification
        titles are prefixed with "[DEMO] " so push messages announce
        themselves as simulated data. Flag flips to false (and the
        prefix disappears) when the switch-to-real wizard transitions
        the install to real-receiver mode.
        """
        self._lock = threading.Lock()
        self._cfg: Dict[str, Any] = {}
        self._stats_tz: Optional[ZoneInfo] = None
        self._demo_enabled: bool = False
        # Per-aircraft cooldown state: key = (event, icao), value = unix_ts
        # of last notify. Look up before sending to decide whether to skip.
        self._cooldowns: Dict[Tuple[str, str], float] = {}
        # Rolling log of timestamps for rate-limit-per-hour enforcement.
        # deque so prune-old is O(k) where k is events older than 1h.
        self._send_timestamps: Deque[float] = deque()
        # Ring buffer of recent notification records for UI display.
        # Each entry is a dict with keys: ts, event, title, body, priority,
        # icao (opt), sent (bool), reason (str if not sent), response_code
        # (int if sent), response_error (str if exception).
        # v2.41.3: bumped from 100 → 2000 to support stats over a 24h window
        # at the default 20-per-hour rate cap (480/day worst case). 2000
        # entries at ~500 bytes each = ~1 MB memory, acceptable.
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=2000)
        # Drop-counter: how many notifications have been suppressed
        # per reason, since startup. Useful for /api/notifications/stats
        # someday but also for the logs.
        self._drops: Dict[str, int] = {}
        # v2.41.3: track when the counter started so "stats since N" is
        # meaningful. Cleared on any reset (not currently exposed, but
        # future-proofs).
        import time as _t
        self._stats_started_at: float = _t.time()

        self.update_config(config or {}, stats_timezone, demo_enabled=demo_enabled)

    # ---------------------------------------------------------------
    # Config
    # ---------------------------------------------------------------

    def update_config(self, config: Dict[str, Any],
                      stats_timezone: Optional[str] = None,
                      demo_enabled: Optional[bool] = None) -> None:
        """Apply a new config dict live. Safe to call from the server's
        config-reload path. Does NOT reset cooldowns or the recent log —
        those are runtime state, not config.

        demo_enabled (v3.1.0): if provided, updates the demo-mode flag
        live. The flag controls the [DEMO] notification-title prefix
        and is flipped by the switch-to-real wizard.
        """
        with self._lock:
            self._cfg = dict(config or {})
            if stats_timezone:
                try:
                    self._stats_tz = ZoneInfo(stats_timezone)
                except (ZoneInfoNotFoundError, ValueError):
                    log.warning("Unknown timezone %r, falling back to system tz",
                                stats_timezone)
                    self._stats_tz = None
            if demo_enabled is not None:
                self._demo_enabled = bool(demo_enabled)

    def _enabled(self) -> bool:
        return bool(self._cfg.get("enabled", False)) and bool(self._cfg.get("url"))

    def _event_enabled(self, event: str) -> bool:
        events = self._cfg.get("events") or {}
        # Test events always pass the gate (we're explicitly bypassing).
        if event == "test":
            return True
        return bool(events.get(event, False))

    def _cooldown_minutes_for(self, event: str) -> int:
        if event in NEVER_COOLDOWN:
            return 0
        cd = (self._cfg.get("cooldown_minutes") or {})
        v = cd.get(event, 0)
        if isinstance(v, bool) or not isinstance(v, int):
            return 0
        return max(0, min(v, 1440))

    def _rate_limit_per_hour(self) -> int:
        v = self._cfg.get("rate_limit_per_hour", 20)
        if isinstance(v, bool) or not isinstance(v, int):
            return 20
        return max(0, v)

    def _priority_default(self) -> str:
        v = self._cfg.get("priority", "default")
        return v if v in VALID_PRIORITIES else "default"

    # ---------------------------------------------------------------
    # Gating
    # ---------------------------------------------------------------

    def _in_quiet_hours(self, now_ts: float) -> bool:
        """Check whether the given moment falls inside the configured
        quiet-hours window. Window is HH:MM to HH:MM in stats timezone
        (or system tz if not set). Windows that wrap midnight (e.g. 22:00
        to 07:00) are handled naturally.
        """
        qh = self._cfg.get("quiet_hours") or {}
        if not qh.get("enabled"):
            return False
        start = qh.get("start", "22:00")
        end = qh.get("end", "07:00")
        try:
            sh, sm = map(int, start.split(":", 1))
            eh, em = map(int, end.split(":", 1))
        except (ValueError, AttributeError):
            return False
        # Convert to minutes-of-day for comparison
        now = datetime.fromtimestamp(now_ts, tz=self._stats_tz) if self._stats_tz \
            else datetime.fromtimestamp(now_ts)
        now_mod = now.hour * 60 + now.minute
        start_mod = sh * 60 + sm
        end_mod = eh * 60 + em
        if start_mod == end_mod:
            # Zero-width window → never in quiet hours.
            return False
        if start_mod < end_mod:
            # Same-day window, e.g. 01:00 to 05:00
            return start_mod <= now_mod < end_mod
        # Cross-midnight window, e.g. 22:00 to 07:00
        return now_mod >= start_mod or now_mod < end_mod

    def _prune_old_sends(self, now_ts: float) -> None:
        """Drop timestamps older than 1h from the rate-limit deque."""
        cutoff = now_ts - 3600
        while self._send_timestamps and self._send_timestamps[0] < cutoff:
            self._send_timestamps.popleft()

    def _rate_limited(self, now_ts: float) -> bool:
        limit = self._rate_limit_per_hour()
        if limit <= 0:
            return False  # 0 = unlimited
        self._prune_old_sends(now_ts)
        return len(self._send_timestamps) >= limit

    def _cooldown_active(self, event: str, icao: Optional[str],
                         now_ts: float) -> bool:
        if event in NEVER_COOLDOWN or not icao:
            return False
        minutes = self._cooldown_minutes_for(event)
        if minutes <= 0:
            return False
        key = (event, icao.upper())
        last = self._cooldowns.get(key)
        if last is None:
            return False
        return (now_ts - last) < (minutes * 60)

    # ---------------------------------------------------------------
    # Send
    # ---------------------------------------------------------------

    def notify(self, event: str, title: str, body: str,
               priority: Optional[str] = None,
               tags: Optional[List[str]] = None,
               aircraft_icao: Optional[str] = None,
               click_route: Optional[str] = None,
               track_url: Optional[str] = None) -> bool:
        """Attempt to send a notification. Returns True if sent, False
        if suppressed for any reason. Never raises.

        event — one of KNOWN_EVENTS. Used for gating and cooldown lookup.
        title — ntfy notification title (shown prominently on phone).
        body  — message body.
        priority — override the config default priority for this send.
        tags — list of ntfy tag strings (emoji or named, e.g. ['warning']).
        aircraft_icao — if set, per-aircraft cooldown applies for this event.
        click_route — v2.43.0: logical destination hint for the notification
            tap action. One of 'live', 'watchlist', 'military', 'stats',
            'status'. Combined with `notifications.public_url` from config
            to build the actual Click URL. If public_url isn't set or
            click_route is None, no Click header is sent and taps do the
            ntfy default (open ntfy app). No breakage for users who haven't
            configured public_url yet.
        track_url — v2.43.0: optional external URL (e.g. airplanes.live
            track link) that becomes a 'Track' action button on the
            notification. Only applicable to watchlist/military/special
            events. Gracefully omitted if None.
        """
        now_ts = time.time()

        if event not in KNOWN_EVENTS:
            log.warning("notify() called with unknown event %r", event)
            return False

        # v3.1.0: prefix titles with [DEMO] when running in demo mode so
        # push notifications announce themselves as simulated. Applied at
        # the top of notify() so the records buffer, the drop log, and the
        # actual ntfy POST all see the prefixed form. Flag flips to false
        # in real mode (post switch-to-real wizard) and the prefix disappears
        # automatically — no code change, no notifier reload.
        if self._demo_enabled and not title.startswith("[DEMO] "):
            title = "[DEMO] " + title

        with self._lock:
            # Gate 1: notifications disabled entirely
            if event != "test" and not self._enabled():
                self._drop("disabled")
                return False

            # Gate 2: this event type disabled
            if not self._event_enabled(event):
                self._drop("event_disabled")
                return False

            # Gate 3: quiet hours (test bypasses)
            if event != "test" and self._in_quiet_hours(now_ts):
                self._drop("quiet_hours")
                self._record(now_ts, event, title, body, priority or self._priority_default(),
                             aircraft_icao, sent=False, reason="quiet_hours")
                return False

            # Gate 4: per-aircraft cooldown
            if self._cooldown_active(event, aircraft_icao, now_ts):
                self._drop("cooldown")
                return False

            # Gate 5: rate limit
            if self._rate_limited(now_ts):
                self._drop("rate_limit")
                self._record(now_ts, event, title, body, priority or self._priority_default(),
                             aircraft_icao, sent=False, reason="rate_limit")
                return False

            url = self._cfg.get("url", "")
            effective_priority = priority if (priority in VALID_PRIORITIES) \
                else self._priority_default()

            # v2.43.0: rich notification additions. Build the Click URL from
            # notifications.public_url + the logical route; collect View
            # actions (currently just an optional external 'Track' button).
            # Both are optional — if public_url isn't set, click_url stays
            # None and _post_ntfy omits the Click header. Equivalent to
            # pre-v2.43.0 behavior for users who haven't configured it.
            public_url = (self._cfg.get("public_url") or "").strip().rstrip("/")
            click_url = self._build_click_url(public_url, click_route, aircraft_icao) \
                        if click_route else None
            actions = self._build_actions(track_url)

        # --- release lock before network call ---
        ok, http_code, err = self._post_ntfy(
            url, title, body, effective_priority, tags,
            click_url=click_url, actions=actions,
        )

        with self._lock:
            if ok:
                self._send_timestamps.append(now_ts)
                if aircraft_icao and event not in NEVER_COOLDOWN:
                    self._cooldowns[(event, aircraft_icao.upper())] = now_ts
                self._record(now_ts, event, title, body, effective_priority,
                             aircraft_icao, sent=True,
                             response_code=http_code)
                return True
            else:
                self._drop("send_failed")
                self._record(now_ts, event, title, body, effective_priority,
                             aircraft_icao, sent=False,
                             reason="send_failed",
                             response_code=http_code,
                             response_error=err)
                return False

    # ---------------------------------------------------------------
    # Daily summary (v2.41.35)
    # ---------------------------------------------------------------
    #
    # The data dict comes from server.compose_daily_summary_data(). This
    # notifier only formats it for ntfy and sends via notify(). The
    # server's scheduler decides WHEN to call send_daily_summary and
    # handles the "don't double-fire on the same day" state.

    def compose_daily_summary_body(self, data: dict,
                                    version: Optional[str] = None) -> Tuple[str, str]:
        """Format the gathered data into (title, body) strings.

        Follows the Option A format: top-line totals always shown, then
        optional sections for peak, military breakdown, records, and
        specials. Empty sections are omitted entirely — a quiet day
        produces a short message, not a message full of zeros.
        """
        lines = []
        unique = data.get("unique_aircraft", 0)
        sightings = data.get("total_sightings", 0)

        # Top line — total volume. Always present even if zero, since a
        # "no aircraft seen today" summary is itself a useful signal.
        lines.append(f"{unique} unique aircraft, {sightings:,} sightings")

        # Military. Include breakdown when present.
        mil = data.get("military_count", 0)
        if mil > 0:
            breakdown = data.get("military_breakdown") or []
            if breakdown:
                # e.g. "Military: 4 (2× F35, 1× KC135, 1× Reaper)"
                parts = [f"{n}× {label}" for label, n in breakdown[:3]]
                lines.append(f"Military: {mil} ({', '.join(parts)})")
            else:
                lines.append(f"Military: {mil}")
        elif mil == 0 and unique > 0:
            # Only show the "0" line when there WAS activity but no military.
            # If the day was completely quiet, skip it.
            lines.append("Military: 0")

        # Watchlist
        wl = data.get("watchlist_count", 0)
        if wl > 0:
            rules = data.get("watchlist_rules_hit", 0)
            if rules > 1:
                lines.append(f"Watchlist: {wl} hits across {rules} rules")
            else:
                lines.append(f"Watchlist: {wl} hits")
        elif wl == 0 and unique > 0:
            lines.append("Watchlist: 0")

        # Peak simultaneous — only include when meaningful
        peak = data.get("peak")
        if peak and peak.get("count", 0) >= 2:
            # Format the time in the user's tz (same as quiet hours)
            ts = peak.get("at_ts", 0)
            hhmm = self._format_hhmm(ts)
            lines.append("")
            lines.append(f"Peak: {peak['count']} aircraft simultaneously at {hhmm}")

        # New records
        records = data.get("new_records") or []
        if records:
            lines.append("")
            # Limit to 3 most recent so the message stays scannable
            for rec in records[:3]:
                pretty = self._format_record_line(rec)
                if pretty:
                    lines.append(pretty)

        # Specials
        specials = data.get("specials") or []
        if specials:
            lines.append("")
            # One per line, up to 3
            for sp in specials[:3]:
                cs = (sp.get("callsign") or "").strip() or sp.get("icao", "—")
                ts = sp.get("last_seen_at") or 0
                hhmm = self._format_hhmm(ts)
                lines.append(f"Special: {sp.get('special_label', '?')} "
                             f"({cs}) at {hhmm}")

        # Footer
        lines.append("")
        if version:
            lines.append(f"— Aerodrome v{version}")
        else:
            lines.append("— Aerodrome")

        title = "Aerodrome - last 24h summary"
        body = "\n".join(lines).strip()
        return title, body

    def _format_hhmm(self, ts: float) -> str:
        """Format a unix timestamp as HH:MM in the user's configured
        timezone. Falls back to UTC if no timezone is configured."""
        import datetime as _dt
        if not ts:
            return "—"
        try:
            dt = _dt.datetime.fromtimestamp(ts, tz=self._stats_tz or _dt.timezone.utc)
            return dt.strftime("%H:%M")
        except (ValueError, OSError):
            return "—"

    def _format_record_line(self, rec: dict) -> str:
        """Turn a stats_records row into a short human line. Ignores
        record types we don't know how to describe."""
        rt = rec.get("record_type", "")
        val = rec.get("value")
        ac = (rec.get("callsign") or "").strip() or rec.get("icao", "—")
        if rt == "fastest" and val is not None:
            return f"New record: Fastest — {ac} @ {int(val)} kts"
        if rt == "highest" and val is not None:
            return f"New record: Highest — {ac} @ {int(val):,} ft"
        if rt == "lowest" and val is not None:
            return f"New record: Lowest — {ac} @ {int(val):,} ft"
        if rt == "furthest" and val is not None:
            return f"New record: Furthest — {ac} @ {val:.1f} mi"
        if rt == "longest_track" and val is not None:
            # value is minutes; format as Hh Mm
            total = int(val)
            h, m = divmod(total, 60)
            if h > 0:
                span = f"{h}h {m}m"
            else:
                span = f"{m}m"
            return f"New record: Longest track — {ac} @ {span}"
        if rt == "peak_simultaneous" and val is not None:
            return f"New record: Peak simultaneous — {int(val)} aircraft"
        # Unknown record type — give a generic line rather than skipping,
        # so the user knows something happened but can open the app
        # to see the detail.
        return f"New record: {rt}"

    def send_daily_summary(self, data: dict,
                            version: Optional[str] = None) -> bool:
        """Compose + send the summary. Returns True if actually sent,
        False if suppressed (disabled, quiet hours, rate limit, etc.).

        The scheduler handles once-per-day dedup; this method itself just
        does the send. Callers invoking this for testing bypass the
        dedup automatically since they're not going through the scheduler.
        """
        title, body = self.compose_daily_summary_body(data, version=version)
        return self.notify("daily_summary", title, body, click_route="stats")

    def send_test(self, url: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        """Send a test notification, bypassing the enable/event gates
        (but NOT rate limit or quiet hours — those are protecting the
        user from ACTUAL spam on their phone). Returns (ok, message, info).

        If `url` is passed, uses it for the test instead of the configured
        URL — lets the user verify a URL before committing it to config.

        v2.47.2: when notifications.public_url is set, the test includes a
        Click header pointing at /notification-test-ok so tapping the
        notification opens a confirmation page. This verifies the full
        tap-to-open path, not just ntfy delivery. When public_url is blank
        the test still fires (delivery is useful to verify independently),
        but the returned `info` dict carries `tap_to_open_configured: False`
        so the UI can warn the user that this half of the feature needs
        setup.

        info dict shape:
          {
            "tap_to_open_configured": bool,
            "tap_to_open_url": str | None,   # the Click URL that was sent
          }
        """
        now_ts = time.time()

        with self._lock:
            if self._rate_limited(now_ts):
                return False, "Rate limited — try again in a minute", {
                    "tap_to_open_configured": False,
                    "tap_to_open_url": None,
                }
            test_url = url or self._cfg.get("url", "")
            if not test_url:
                return False, "No URL configured", {
                    "tap_to_open_configured": False,
                    "tap_to_open_url": None,
                }
            priority = self._priority_default()
            public_url = (self._cfg.get("public_url") or "").strip().rstrip("/")

        # Build the Click URL when public_url is configured. Points at a
        # dedicated confirmation page that says "you tapped correctly."
        # When public_url is blank, click_url stays None and ntfy doesn't
        # get a Click header, which matches pre-v2.47.2 behavior.
        click_url = f"{public_url}/notification-test-ok" if public_url else None

        # v3.1.0: [DEMO] prefix in demo mode. send_test bypasses notify()
        # for the event-gate-bypass reasons documented above, so the
        # prefix has to be applied at this call site directly. Keeps
        # demo-mode push titles consistent across every notification path.
        test_title = "Aerodrome test notification"
        if self._demo_enabled:
            test_title = "[DEMO] " + test_title

        ok, code, err = self._post_ntfy(
            test_url,
            test_title,
            ("If you received this, push notifications are working."
             if not click_url else
             "If you received this, push notifications are working. "
             "Tap to confirm tap-to-open is working too."),
            priority,
            tags=["white_check_mark"],
            click_url=click_url,
        )

        info = {
            "tap_to_open_configured": bool(click_url),
            "tap_to_open_url": click_url,
        }

        with self._lock:
            if ok:
                self._send_timestamps.append(now_ts)
                self._record(now_ts, "test", "Aerodrome test notification",
                             "If you received this, push notifications are working.",
                             priority, None, sent=True, response_code=code)
                return True, f"Sent (HTTP {code})", info
            else:
                self._record(now_ts, "test", "Aerodrome test notification",
                             "If you received this, push notifications are working.",
                             priority, None, sent=False, reason="send_failed",
                             response_code=code, response_error=err)
                return False, err or f"Send failed (HTTP {code})", info

    def _build_click_url(self, public_url: str, route: Optional[str],
                         icao: Optional[str]) -> Optional[str]:
        """Combine public_url + route into a destination URL for the
        notification tap. Returns None if public_url isn't set.

        Routes map to frontend pages/tabs:
            'live'      → /         (with ?icao=… + &highlight=1 if icao given)
            'watchlist' → /?tab=watchlist  (+ icao/highlight)
            'military'  → /?tab=military   (+ icao/highlight)
            'stats'     → /?tab=stats
            'status'    → /status
            'updates'   → /updates    (v3.0.2)
        Unknown routes fall back to the bare public_url.
        """
        if not public_url:
            return None
        base = public_url  # already stripped of trailing slash
        icao_q = f"?icao={icao.upper()}&highlight=1" if icao else ""
        if route == "live":
            return f"{base}/{icao_q}"
        if route == "watchlist":
            if icao:
                return f"{base}/?tab=watchlist&icao={icao.upper()}&highlight=1"
            return f"{base}/?tab=watchlist"
        if route == "military":
            if icao:
                return f"{base}/?tab=military&icao={icao.upper()}&highlight=1"
            return f"{base}/?tab=military"
        if route == "stats":
            return f"{base}/?tab=stats"
        if route == "status":
            return f"{base}/status"
        if route == "updates":
            return f"{base}/updates"
        return base

    def _build_actions(self, track_url: Optional[str]) -> Optional[str]:
        """Return an ntfy Actions header value, or None.

        ntfy Actions header format is a comma-separated list of
        semicolon-delimited fields, or JSON. We use the simple form:
            view, Track, <url>, clear=true
        'clear=true' dismisses the notification when the button is tapped,
        which matches expected UX for 'I'm going to go look at this now'.

        See https://docs.ntfy.sh/publish/#using-a-header
        """
        if not track_url:
            return None
        # Sanitize: ntfy headers are latin-1; urls with unusual chars would
        # already be percent-encoded before reaching us, so this is cheap
        # insurance rather than something I expect to trip.
        safe_url = _to_latin1_safe(track_url)
        return f"view, Track, {safe_url}, clear=true"

    def _post_ntfy(self, url: str, title: str, body: str, priority: str,
                   tags: Optional[List[str]] = None,
                   click_url: Optional[str] = None,
                   actions: Optional[str] = None) -> Tuple[bool, int, str]:
        """POST to ntfy. Returns (ok, http_code, error_string).

        ntfy accepts the message body as the POST body, with metadata in
        headers: `Title`, `Priority`, `Tags`, `Click`, `Actions`. See
        https://docs.ntfy.sh/publish/

        v2.42.4: sanitize header values to latin-1. HTTP headers per RFC 7230
        are restricted to ISO-8859-1; anything outside that (including em-dash
        \u2014, ellipsis \u2026, smart quotes, emoji) causes UnicodeEncodeError
        deep in urllib3 with no useful caller context. This used to crash the
        whole notify() call \u2014 triggered originally by the daily-summary
        title 'Aerodrome \u2014 last 24h summary' which embedded an em-dash. The
        body itself is encoded as UTF-8 (ntfy accepts that fine), so rich
        characters work in the message proper; only the metadata headers
        need the squeeze.

        v2.43.0: optional Click and Actions headers for rich notifications.
        Both are no-ops when None. URLs are latin-1-sanitized for the same
        reason title/priority are.
        """
        headers: Dict[str, str] = {
            "Title":    _to_latin1_safe(title),
            "Priority": _to_latin1_safe(priority),
        }
        if tags:
            # Each tag individually sanitized so one exotic tag doesn't
            # poison the rest of the comma-joined list.
            headers["Tags"] = ",".join(_to_latin1_safe(t) for t in tags)
        if click_url:
            headers["Click"] = _to_latin1_safe(click_url)
        if actions:
            # Already sanitized by _build_actions
            headers["Actions"] = actions

        try:
            r = requests.post(url, data=body.encode("utf-8"),
                              headers=headers, timeout=5)
        except requests.Timeout:
            return False, 0, "Timeout (5s) reaching ntfy"
        except requests.ConnectionError as e:
            return False, 0, f"Connection error: {e}"
        except requests.RequestException as e:
            return False, 0, f"Request failed: {e}"

        if 200 <= r.status_code < 300:
            return True, r.status_code, ""
        # ntfy returns JSON errors; include the body to aid debugging.
        snippet = (r.text or "")[:200].strip()
        return False, r.status_code, f"HTTP {r.status_code}: {snippet}"

    # ---------------------------------------------------------------
    # Observability
    # ---------------------------------------------------------------

    def _drop(self, reason: str) -> None:
        self._drops[reason] = self._drops.get(reason, 0) + 1

    def _record(self, ts: float, event: str, title: str, body: str,
                priority: str, icao: Optional[str],
                sent: bool, reason: str = "",
                response_code: Optional[int] = None,
                response_error: Optional[str] = None) -> None:
        self._recent.append({
            "ts": ts,
            "event": event,
            "title": title,
            "body": body,
            "priority": priority,
            "icao": icao,
            "sent": sent,
            "reason": reason,
            "response_code": response_code,
            "response_error": response_error,
        })

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Most-recent-first list of notification records."""
        with self._lock:
            items = list(self._recent)
        items.reverse()
        return items[:max(0, int(limit))]

    def stats(self) -> Dict[str, Any]:
        """(v2.41.3) Summary of notification activity since the service started.

        Returns:
          since              — epoch seconds when the counter started
          uptime_seconds     — how long the service has been running
          windows:
            last_24h         — counts + breakdown over the last 24h
            last_7d          — counts + breakdown over the last 7d
            since_startup    — counts + breakdown since process start
          last_sent          — most recent successful send (or None)
          last_error         — most recent failed send (or None)
        """
        import time as _t
        now = _t.time()
        with self._lock:
            # Snapshot to avoid holding the lock during iteration
            items = list(self._recent)
            drops_snapshot = dict(self._drops)
            started_at = self._stats_started_at

        def _summarize(cutoff: Optional[float]) -> Dict[str, Any]:
            """Count and breakdown for items with ts >= cutoff (or all if None)."""
            subset = [i for i in items if (cutoff is None or i["ts"] >= cutoff)]
            sent = sum(1 for i in subset if i.get("sent"))
            dropped = len(subset) - sent
            # Breakdown by event type for sent items
            by_event: Dict[str, int] = {}
            for i in subset:
                if i.get("sent"):
                    ev = i.get("event") or "unknown"
                    by_event[ev] = by_event.get(ev, 0) + 1
            # Breakdown by suppression reason for dropped items
            by_reason: Dict[str, int] = {}
            for i in subset:
                if not i.get("sent"):
                    rs = (i.get("reason") or "unknown").split(":")[0].strip() or "unknown"
                    by_reason[rs] = by_reason.get(rs, 0) + 1
            return {
                "sent": sent,
                "dropped": dropped,
                "total": len(subset),
                "by_event": by_event,
                "by_drop_reason": by_reason,
            }

        last_sent = None
        last_error = None
        # Items are appended in chronological order, so iterate in reverse
        # to find the most recent of each kind.
        for i in reversed(items):
            if last_sent is None and i.get("sent"):
                last_sent = {
                    "ts": i["ts"],
                    "event": i.get("event"),
                    "title": i.get("title"),
                    "response_code": i.get("response_code"),
                }
            if last_error is None and not i.get("sent") and i.get("response_error"):
                last_error = {
                    "ts": i["ts"],
                    "event": i.get("event"),
                    "title": i.get("title"),
                    "error": i.get("response_error"),
                }
            if last_sent is not None and last_error is not None:
                break

        return {
            "since": started_at,
            "uptime_seconds": max(0, int(now - started_at)),
            "windows": {
                "last_24h": _summarize(now - 86400),
                "last_7d": _summarize(now - 7 * 86400),
                "since_startup": _summarize(None),
            },
            "last_sent": last_sent,
            "last_error": last_error,
            # Expose the drop counter too — it's the all-time equivalent
            # that survives restarts IF the notifier instance persists
            # (it resets on config reload today, noted limitation).
            "all_time_drops": drops_snapshot,
            # Note: dropped events that never hit _record (e.g. due to
            # early gating) won't show in by_drop_reason above — they
            # only live in all_time_drops. We surface both for clarity.
        }
