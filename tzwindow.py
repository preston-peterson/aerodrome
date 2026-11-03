"""The one place for time rules: which timezone counts as this install's, and
what a local day, hour, month or year is in unix seconds.

`tz` is a tzinfo, or None for the system's timezone, everywhere in this module.
An empty or unknown `stats.timezone` means None. Windows are half-open
[start, end) and are built from wall-clock datetimes, never `+ 86400`, so 23- and
25-hour days and half-hour zones come out right.

On a clock-change day a local hour can be empty (the skipped hour: start == end)
or two hours long (the repeated hour holds both of its passes). That is what the
hourly histogram counts too.
"""
import logging
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

Window = Tuple[int, int]
_warned_zones = set()


def resolve_tz(config: Optional[Dict[str, Any]]) -> Optional[tzinfo]:
    """The configured timezone, or None for system time."""
    name = str(((config or {}).get("stats") or {}).get("timezone") or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        if name not in _warned_zones:
            _warned_zones.add(name)
            logger.warning(f"Unknown stats.timezone {name!r}: using the system timezone")
        return None


def local_dt(ts: float, tz: Optional[tzinfo] = None) -> datetime:
    """A unix timestamp as local wall-clock time."""
    return datetime.fromtimestamp(ts, tz)


def _at(day: date, hour: int, tz: Optional[tzinfo]) -> int:
    # A naive datetime's .timestamp() reads it as system local time.
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=tz).timestamp())


def day_bounds(day: date, tz: Optional[tzinfo] = None) -> Window:
    return _at(day, 0, tz), _at(day + timedelta(days=1), 0, tz)


def hour_bounds(day: date, first: int, last: int, tz: Optional[tzinfo] = None) -> Window:
    """Hours `first` to `last` of a local day, both inclusive."""
    end = _at(day + timedelta(days=1), 0, tz) if last == 23 else _at(day, last + 1, tz)
    return _at(day, first, tz), end


def month_bounds(year: int, month: int, tz: Optional[tzinfo] = None) -> Window:
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return _at(date(year, month, 1), 0, tz), _at(end, 0, tz)


def year_bounds(year: int, tz: Optional[tzinfo] = None) -> Window:
    return _at(date(year, 1, 1), 0, tz), _at(date(year + 1, 1, 1), 0, tz)


def today(tz: Optional[tzinfo] = None, now: Optional[float] = None) -> date:
    return local_dt(time.time() if now is None else now, tz).date()


def today_bounds(tz: Optional[tzinfo] = None, now: Optional[float] = None) -> Window:
    return day_bounds(today(tz, now), tz)


def last_days_bounds(days: int, tz: Optional[tzinfo] = None, now: Optional[float] = None) -> Window:
    """Today and the days - 1 calendar days before it."""
    current = today(tz, now)
    return _at(current - timedelta(days=days - 1), 0, tz), _at(current + timedelta(days=1), 0, tz)


def calendar_days(first_ts: float, last_ts: float, tz: Optional[tzinfo] = None) -> int:
    """How many calendar days the span touches, both ends included."""
    return (local_dt(last_ts, tz).date() - local_dt(first_ts, tz).date()).days + 1


def _offset(ts: int, tz: Optional[tzinfo]) -> int:
    dt = local_dt(ts, tz)
    # A naive local datetime has no offset of its own; astimezone() supplies it.
    return int((dt if tz else dt.astimezone()).utcoffset().total_seconds())


def offset_segments(start: int, end: int, tz: Optional[tzinfo] = None) -> List[Tuple[int, int, int]]:
    """[start, end) as (segment_start, segment_end, utc_offset_seconds), split at
    each clock change. One segment on an ordinary day. Steps an hour at a time and
    narrows to the second, so it finds a change at any time of day."""
    if end <= start:
        return [(start, end, _offset(start, tz))]
    segments = []
    seg_start = start
    current = _offset(start, tz)
    probe = start
    while probe < end:
        step_end = min(probe + 3600, end)
        last_second = step_end - 1
        if _offset(last_second, tz) != current:
            # Offset at low == current, at high != current. The change can fall
            # exactly on this step's first second, so low starts one before it.
            low, high = (probe - 1 if probe > seg_start else probe), last_second
            while high - low > 1:
                mid = (low + high) // 2
                if _offset(mid, tz) == current:
                    low = mid
                else:
                    high = mid
            segments.append((seg_start, high, current))
            seg_start = high
            current = _offset(high, tz)
            probe = high
            continue
        probe = step_end
    segments.append((seg_start, end, current))
    return segments
