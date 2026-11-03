"""tzwindow.py: local days, hours, months and clock changes. No database."""
import os
import time
import unittest
from datetime import date, datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import tzwindow

CHICAGO = ZoneInfo("America/Chicago")
KOLKATA = ZoneInfo("Asia/Kolkata")


def utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


class _SystemTz:
    """Run with a given system timezone."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.saved = os.environ.get("TZ")
        os.environ["TZ"] = self.name
        time.tzset()

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.saved
        time.tzset()


class TestDays(unittest.TestCase):

    def test_ordinary_day(self):
        start, end = tzwindow.day_bounds(date(2026, 6, 15), CHICAGO)
        self.assertEqual(start, utc(2026, 6, 15, 5))
        self.assertEqual(end - start, 24 * 3600)

    def test_clock_change_days(self):
        spring = tzwindow.day_bounds(date(2026, 3, 8), CHICAGO)
        self.assertEqual(spring[1] - spring[0], 23 * 3600)
        autumn = tzwindow.day_bounds(date(2026, 11, 1), CHICAGO)
        self.assertEqual(autumn[1] - autumn[0], 25 * 3600)

    def test_half_hour_zone(self):
        start, end = tzwindow.day_bounds(date(2026, 6, 15), KOLKATA)
        self.assertEqual(start, utc(2026, 6, 14, 18, 30))
        self.assertEqual(end, utc(2026, 6, 15, 18, 30))

    def test_today_follows_the_timezone_not_utc(self):
        # 03:00 UTC is still the previous evening in Chicago.
        now = utc(2026, 6, 16, 3)
        self.assertEqual(tzwindow.today(CHICAGO, now), date(2026, 6, 15))
        self.assertEqual(tzwindow.today(timezone.utc, now), date(2026, 6, 16))
        self.assertEqual(tzwindow.today_bounds(CHICAGO, now),
                         tzwindow.day_bounds(date(2026, 6, 15), CHICAGO))

    def test_last_days_across_a_clock_change(self):
        now = utc(2026, 11, 2, 18)
        start, end = tzwindow.last_days_bounds(3, CHICAGO, now)
        self.assertEqual(start, tzwindow.day_bounds(date(2026, 10, 31), CHICAGO)[0])
        self.assertEqual(end, tzwindow.day_bounds(date(2026, 11, 2), CHICAGO)[1])
        self.assertEqual(end - start, (24 + 25 + 24) * 3600)
        self.assertEqual(tzwindow.last_days_bounds(1, CHICAGO, now),
                         tzwindow.today_bounds(CHICAGO, now))

    def test_calendar_days(self):
        self.assertEqual(tzwindow.calendar_days(utc(2026, 6, 15, 12), utc(2026, 6, 15, 13), CHICAGO), 1)
        # Under 24 hours apart, two calendar days.
        self.assertEqual(tzwindow.calendar_days(utc(2026, 6, 16, 4), utc(2026, 6, 16, 6), CHICAGO), 2)
        self.assertEqual(tzwindow.calendar_days(utc(2026, 6, 1, 12), utc(2026, 6, 30, 12), CHICAGO), 30)


class TestHours(unittest.TestCase):

    def test_ordinary_hour(self):
        self.assertEqual(tzwindow.hour_bounds(date(2026, 6, 15), 14, 14, CHICAGO),
                         (utc(2026, 6, 15, 19), utc(2026, 6, 15, 20)))

    def test_range_is_inclusive_and_ends_at_midnight(self):
        start, end = tzwindow.hour_bounds(date(2026, 6, 15), 8, 10, CHICAGO)
        self.assertEqual(end - start, 3 * 3600)
        self.assertEqual(tzwindow.hour_bounds(date(2026, 6, 15), 23, 23, CHICAGO)[1],
                         tzwindow.day_bounds(date(2026, 6, 16), CHICAGO)[0])

    def test_skipped_hour_is_empty(self):
        start, end = tzwindow.hour_bounds(date(2026, 3, 8), 2, 2, CHICAGO)
        self.assertEqual(start, end)

    def test_repeated_hour_holds_both_passes(self):
        start, end = tzwindow.hour_bounds(date(2026, 11, 1), 1, 1, CHICAGO)
        self.assertEqual(end - start, 2 * 3600)

    def test_hours_after_a_clock_change_are_not_an_hour_off(self):
        midnight = tzwindow.day_bounds(date(2026, 11, 1), CHICAGO)[0]
        start, _ = tzwindow.hour_bounds(date(2026, 11, 1), 14, 14, CHICAGO)
        self.assertEqual(start, midnight + 15 * 3600)   # not midnight + 14 h

    def test_half_hour_zone(self):
        self.assertEqual(tzwindow.hour_bounds(date(2026, 6, 15), 14, 14, KOLKATA),
                         (utc(2026, 6, 15, 8, 30), utc(2026, 6, 15, 9, 30)))


class TestMonthsAndYears(unittest.TestCase):

    def test_month(self):
        self.assertEqual(tzwindow.month_bounds(2026, 4, CHICAGO),
                         (utc(2026, 4, 1, 5), utc(2026, 5, 1, 5)))

    def test_december_rolls_over(self):
        self.assertEqual(tzwindow.month_bounds(2026, 12, CHICAGO)[1],
                         tzwindow.day_bounds(date(2027, 1, 1), CHICAGO)[0])

    def test_year(self):
        self.assertEqual(tzwindow.year_bounds(2026, CHICAGO),
                         (utc(2026, 1, 1, 6), utc(2027, 1, 1, 6)))


class TestOffsetSegments(unittest.TestCase):

    def test_ordinary_day_is_one_segment(self):
        start, end = tzwindow.day_bounds(date(2026, 6, 15), CHICAGO)
        self.assertEqual(tzwindow.offset_segments(start, end, CHICAGO),
                         [(start, end, -5 * 3600)])

    def test_autumn_change_splits_at_the_exact_second(self):
        start, end = tzwindow.day_bounds(date(2026, 11, 1), CHICAGO)
        change = utc(2026, 11, 1, 7)
        self.assertEqual(tzwindow.offset_segments(start, end, CHICAGO),
                         [(start, change, -5 * 3600), (change, end, -6 * 3600)])

    def test_spring_change(self):
        start, end = tzwindow.day_bounds(date(2026, 3, 8), CHICAGO)
        change = utc(2026, 3, 8, 8)
        self.assertEqual(tzwindow.offset_segments(start, end, CHICAGO),
                         [(start, change, -6 * 3600), (change, end, -5 * 3600)])

    def test_half_hour_shift(self):
        # Lord Howe moves by 30 minutes.
        lord_howe = ZoneInfo("Australia/Lord_Howe")
        start, end = tzwindow.day_bounds(date(2026, 10, 4), lord_howe)
        segments = tzwindow.offset_segments(start, end, lord_howe)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1][2] - segments[0][2], 1800)

    def test_empty_window(self):
        self.assertEqual(len(tzwindow.offset_segments(100, 100, CHICAGO)), 1)


class TestSystemTime(unittest.TestCase):
    """tz=None must behave exactly like naming the system's zone."""

    def test_same_answers_as_the_named_zone(self):
        with _SystemTz("America/Chicago"):
            for day in (date(2026, 6, 15), date(2026, 3, 8), date(2026, 11, 1)):
                self.assertEqual(tzwindow.day_bounds(day), tzwindow.day_bounds(day, CHICAGO))
                for hour in (1, 2, 14, 23):
                    self.assertEqual(tzwindow.hour_bounds(day, hour, hour),
                                     tzwindow.hour_bounds(day, hour, hour, CHICAGO), (day, hour))
            start, end = tzwindow.day_bounds(date(2026, 11, 1))
            self.assertEqual(tzwindow.offset_segments(start, end),
                             tzwindow.offset_segments(start, end, CHICAGO))
            self.assertEqual(tzwindow.local_dt(utc(2026, 6, 15, 19)).hour, 14)


class TestResolveTz(unittest.TestCase):

    def test_empty_means_system_time(self):
        for config in (None, {}, {"stats": None}, {"stats": {}}, {"stats": {"timezone": "  "}}):
            self.assertIsNone(tzwindow.resolve_tz(config))

    def test_named_zone(self):
        self.assertEqual(tzwindow.resolve_tz({"stats": {"timezone": " Asia/Kolkata "}}), KOLKATA)

    def test_unknown_zone_warns_once_and_means_system_time(self):
        config = {"stats": {"timezone": "Mars/Olympus_Mons"}}
        with mock.patch.object(tzwindow, "_warned_zones", set()), \
                mock.patch.object(tzwindow.logger, "warning") as warning:
            self.assertIsNone(tzwindow.resolve_tz(config))
            self.assertIsNone(tzwindow.resolve_tz(config))
        self.assertEqual(warning.call_count, 1)


if __name__ == "__main__":
    unittest.main()
