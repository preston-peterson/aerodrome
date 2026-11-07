"""Search time and name semantics on the real schema (init_db + every migration):
"seen during a window", window sightings, airline names and free text, the date and
hour tokens, the chip fields, and parity between Stats cards, their drills and Search
through the real endpoints. Hexes, callsigns and data are invented.
"""
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import yaml

import collector
import hexdb_resolver
import search
import server
import tzwindow
from schema_migrations import apply_schema_migrations
from search import execute_search, parse_query

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")

REPO = Path(__file__).resolve().parent
CHICAGO = ZoneInfo("America/Chicago")
KOLKATA = ZoneInfo("Asia/Kolkata")
DAY = 86400


def utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


class _DbCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        collector.init_db(self.db)
        self.conn = sqlite3.connect(self.db)
        apply_schema_migrations(self.conn, "test")

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def aircraft(self, icao, first, last, count=None, **columns):
        names = ["icao", "first_seen_at", "last_seen_at", "sighting_count", *columns]
        values = [icao, first, last, count if count is not None else 1, *columns.values()]
        marks = ",".join("?" * len(names))
        self.conn.execute(f"INSERT INTO seen_aircraft ({','.join(names)}) VALUES ({marks})", values)
        self.conn.commit()

    def hour(self, icao, ts, count=1):
        """A rollup row for the UTC hour holding ts."""
        bucket = ts - ts % 3600
        self.conn.execute(
            "INSERT INTO sightings_hourly (icao, hour_bucket, sighting_count, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)", (icao, bucket, count, ts, ts))
        self.conn.commit()

    def raw(self, icao, ts):
        self.conn.execute("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)", (icao, ts))
        self.conn.commit()

    def found(self, query, tz=CHICAGO, **kwargs):
        return {r["icao"] for r in execute_search(self.conn, parse_query(query, tz=tz), **kwargs)["rows"]}


class TestSeenDuringAWindow(_DbCase):

    def setUp(self):
        super().setUp()
        # Regular: seen on Mar 10 and last seen a month later.
        self.mar10 = utc(2026, 3, 10, 18)
        self.apr10 = utc(2026, 4, 10, 18)
        self.aircraft("A00001", self.mar10, self.apr10, count=30)
        self.hour("A00001", self.mar10, 10)
        self.hour("A00001", self.apr10, 20)
        # Last seen inside the window, no rollup row at all.
        self.aircraft("A00002", self.mar10, self.mar10)
        # Gone before the window.
        self.aircraft("A00003", utc(2026, 2, 1), utc(2026, 2, 2))
        self.hour("A00003", utc(2026, 2, 1), 5)

    def test_seen_earlier_than_last_seen_is_found(self):
        self.assertEqual(self.found("2026-03-10"), {"A00001", "A00002"})
        self.assertEqual(self.found("2026-03"), {"A00001", "A00002"})

    def test_a_day_it_was_not_seen(self):
        self.assertEqual(self.found("2026-03-11"), set())

    def test_rollup_alone_is_enough_past_raw_retention(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM all_sightings").fetchone()[0], 0)
        self.assertIn("A00001", self.found("2026-03-10"))

    def test_day_boundaries_are_the_install_timezone(self):
        # 03:00 UTC on Mar 11 is the evening of Mar 10 in Chicago.
        late = utc(2026, 3, 11, 3)
        self.aircraft("A00004", late, self.apr10)
        self.hour("A00004", late)
        self.assertIn("A00004", self.found("2026-03-10", tz=CHICAGO))
        self.assertNotIn("A00004", self.found("2026-03-10", tz=timezone.utc))
        self.assertIn("A00004", self.found("2026-03-11", tz=timezone.utc))

    def test_presets_mean_the_same(self):
        out = execute_search(self.conn, parse_query("", tz=CHICAGO),
                             from_ts=utc(2026, 3, 10), to_ts=utc(2026, 3, 11))
        self.assertEqual({r["icao"] for r in out["rows"]}, {"A00001", "A00002"})
        self.assertEqual(out["total_count"], 2)

    def test_narrowing_with_a_filter(self):
        self.conn.execute("UPDATE seen_aircraft SET aircraft_type='B738' WHERE icao='A00001'")
        self.assertEqual(self.found("B738 2026-03-10"), {"A00001"})


class TestHalfHourZone(_DbCase):
    """Rollup buckets are UTC hours; a Kolkata day starts mid-bucket."""

    def setUp(self):
        super().setUp()
        self.last = utc(2026, 7, 1)
        # 00:10 IST on Jun 15 is 18:40 UTC on Jun 14: inside a bucket shared with
        # 23:30-00:30 IST.
        self.edge = utc(2026, 6, 14, 18, 40)
        self.aircraft("A00001", self.edge, self.last)
        self.hour("A00001", self.edge, 3)
        self.raw("A00001", self.edge)

    def test_partial_edge_hour_uses_raw_sightings(self):
        self.assertEqual(self.found("2026-06-15", tz=KOLKATA), {"A00001"})
        self.assertEqual(self.found("2026-06-14", tz=KOLKATA), set())

    def test_window_shorter_than_a_bucket(self):
        # 14:00-15:00 IST is 08:30-09:30 UTC: no whole UTC hour at all.
        inside = utc(2026, 6, 15, 9)
        self.aircraft("A00002", inside, self.last)
        self.raw("A00002", inside)
        day = date(2026, 6, 15)
        out = execute_search(self.conn, {**parse_query("", tz=KOLKATA),
                                         "time_range": tzwindow.hour_bounds(day, 14, 14, KOLKATA)})
        self.assertEqual({r["icao"] for r in out["rows"]}, {"A00002"})
        self.assertEqual(out["rows"][0]["window_sightings"], 1)

    def test_whole_hours_and_edges_add_up(self):
        icao = "A00003"
        whole = utc(2026, 6, 15, 12)
        self.aircraft(icao, self.edge, self.last)
        self.hour(icao, whole, 5)
        for ts in (self.edge, self.edge + 60):
            self.raw(icao, ts)
        out = execute_search(self.conn, parse_query("2026-06-15", tz=KOLKATA))
        row = next(r for r in out["rows"] if r["icao"] == icao)
        self.assertEqual(row["window_sightings"], 7)


class TestWindowSightings(_DbCase):

    def setUp(self):
        super().setUp()
        self.day = utc(2026, 3, 10, 18)
        # A has more sightings in all, B has more that day.
        self.aircraft("A00001", self.day, self.day + 30 * DAY, count=340)
        self.hour("A00001", self.day, 2)
        self.hour("A00001", self.day + 30 * DAY, 338)
        self.aircraft("A00002", self.day, self.day + 30 * DAY, count=50)
        self.hour("A00002", self.day, 12)
        self.hour("A00002", self.day + 30 * DAY, 38)

    def test_rows_carry_the_count_in_the_window(self):
        rows = execute_search(self.conn, parse_query("2026-03-10", tz=CHICAGO))["rows"]
        by_icao = {r["icao"]: r for r in rows}
        self.assertEqual(by_icao["A00001"]["window_sightings"], 2)
        self.assertEqual(by_icao["A00001"]["sighting_count"], 340)     # stays lifetime
        self.assertEqual(by_icao["A00002"]["window_sightings"], 12)

    def test_no_window_no_field(self):
        rows = execute_search(self.conn, parse_query("", tz=CHICAGO))["rows"]
        self.assertTrue(rows)
        self.assertNotIn("window_sightings", rows[0])

    def test_sort_goes_by_the_window(self):
        parsed = parse_query("2026-03-10", tz=CHICAGO)
        rows = execute_search(self.conn, parsed, order="sightings")["rows"]
        self.assertEqual([r["icao"] for r in rows], ["A00002", "A00001"])
        rows = execute_search(self.conn, parsed, order="sightings", direction="asc")["rows"]
        self.assertEqual([r["icao"] for r in rows], ["A00001", "A00002"])

    def test_sort_without_a_window_is_lifetime(self):
        rows = execute_search(self.conn, parse_query("", tz=CHICAGO), order="sightings")["rows"]
        self.assertEqual([r["icao"] for r in rows], ["A00001", "A00002"])

    def test_preset_window_sort(self):
        rows = execute_search(self.conn, parse_query("", tz=CHICAGO), order="sightings",
                              from_ts=utc(2026, 3, 10), to_ts=utc(2026, 3, 11))["rows"]
        self.assertEqual([r["icao"] for r in rows], ["A00002", "A00001"])
        self.assertEqual(rows[0]["window_sightings"], 12)


class TestNames(_DbCase):

    def setUp(self):
        super().setUp()
        t = utc(2026, 3, 10)
        fixtures = [("A00001", "UAL", "United States"), ("A00002", "DAL", "United States"),
                    ("C00001", "ACA", "Canada"), ("C00002", "WJA", "Canada"),
                    ("A00003", "SKW", "United States"), ("710001", "KAL", "South Korea"),
                    ("A00004", "AAL", "United States"), ("4B0001", "BAW", "United Kingdom")]
        for icao, operator, country in fixtures:
            self.aircraft(icao, t, t, operator=operator, country=country, fts_dirty=1,
                          last_callsign=operator + "1")
        collector._flush_dirty_to_fts(self.conn)
        self.conn.commit()

    def operator_filter(self, query):
        return next((f for f in parse_query(query)["filters"] if f["field"] == "operator"), None)

    def test_full_name_is_the_operator(self):
        f = self.operator_filter("United Airlines")
        self.assertEqual((f["value"], f["display"], f["match"]), ("UAL", "United Airlines", "exact"))
        self.assertEqual(parse_query("United Airlines")["free_text"], [])
        self.assertEqual(self.found("United Airlines"), {"A00001"})

    def test_close_spellings(self):
        for typed, code, name in (("Delta Airlines", "DAL", "Delta Air Lines"),
                                  ("delta air lines", "DAL", "Delta Air Lines"),
                                  ("British Airlines", "BAW", "British Airways")):
            f = self.operator_filter(typed)
            self.assertEqual((f["value"], f["display"]), (code, name), typed)
        self.assertEqual(self.found("Delta Airlines"), {"A00002"})

    def test_name_with_a_country_in_it_is_not_that_country(self):
        parsed = parse_query("Air Canada")
        self.assertEqual([f["field"] for f in parsed["filters"]], ["operator"])
        self.assertEqual(self.found("Air Canada"), {"C00001"})
        self.assertEqual([f["field"] for f in parse_query("Japan Airlines")["filters"]], ["operator"])

    def test_countries_still_work(self):
        self.assertEqual(self.found("Canada"), {"C00001", "C00002"})
        self.assertEqual(self.found("United States"), {"A00001", "A00002", "A00003", "A00004"})
        self.assertEqual(self.found("South Korea"), {"710001"})

    def test_name_and_country_together(self):
        self.assertEqual(self.found("Air Canada Canada"), {"C00001"})

    def test_free_text_matches_by_prefix(self):
        self.assertEqual(parse_query("Sky")["free_text"], ["Sky"])
        self.assertEqual(self.found("Sky"), {"A00003"})

    def test_free_text_leaves_out_the_country_column(self):
        self.assertEqual(self.found("Korea"), {"710001"})   # the "Korean Air" operator only
        self.conn.execute("UPDATE seen_aircraft SET operator='ZZZ', fts_dirty=1 WHERE icao='710001'")
        collector._flush_dirty_to_fts(self.conn)
        self.conn.commit()
        self.assertEqual(self.found("Korea"), set())

    def test_hostile_free_text_is_harmless(self):
        for q in ('foo"bar', '"', "NEAR(a b)", "a AND b", "---", "*", "icao:x", "{icao}", "a\x00b", "\x00"):
            out = execute_search(self.conn, parse_query(q))
            self.assertIsNone(out.get("error"), q)

    def test_tail_style_token_still_goes_to_columns(self):
        self.assertEqual({f["field"] for f in parse_query("G-EXMP")["filters"]},
                         {"registration", "last_callsign"})


class TestTokens(unittest.TestCase):

    def test_impossible_dates_are_free_text(self):
        for q in ("2026-02-30", "2/30/26", "2026-13-01", "2026-13", "13/45/26", "2026-00-10"):
            r = parse_query(q, tz=CHICAGO)
            self.assertIsNone(r["time_range"], q)
            self.assertEqual(r["free_text"], [q])

    def test_leap_day(self):
        self.assertIsNotNone(parse_query("2028-02-29", tz=CHICAGO)["time_range"])
        self.assertIsNone(parse_query("2026-02-29", tz=CHICAGO)["time_range"])

    def test_huge_last_days_are_free_text(self):
        for q in ("last:99999999999999999999999d", "last:36501d"):
            r = parse_query(q, tz=CHICAGO)
            self.assertIsNone(r["time_range"], q)
            self.assertEqual(r["free_text"], [q])
        self.assertIsNotNone(parse_query("last:36500d", tz=CHICAGO)["time_range"])

    def test_nul_is_removed(self):
        self.assertEqual(parse_query("foo\x00bar")["free_text"], ["foobar"])
        self.assertEqual(parse_query("\x00")["free_text"], [])

    def test_hour_applies_to_the_date_typed(self):
        want = tzwindow.hour_bounds(date(2026, 4, 29), 14, 14, CHICAGO)
        self.assertEqual(parse_query("2026-04-29 hour:14", tz=CHICAGO)["time_range"], want)
        self.assertEqual(parse_query("hour:14 2026-04-29", tz=CHICAGO)["time_range"], want)
        self.assertEqual(parse_query("4/29/26 hour:14", tz=CHICAGO)["time_range"], want)

    def test_hour_range_with_a_date(self):
        self.assertEqual(parse_query("2026-04-29 hour:8-10", tz=CHICAGO)["time_range"],
                         tzwindow.hour_bounds(date(2026, 4, 29), 8, 10, CHICAGO))

    def test_hour_alone_is_today(self):
        day = tzwindow.today(CHICAGO)
        want = tzwindow.hour_bounds(day, 14, 14, CHICAGO)
        for q in ("hour:14", "today hour:14", "hour:14 today", "last:1d hour:14"):
            self.assertEqual(parse_query(q, tz=CHICAGO)["time_range"], want, q)

    def test_hour_with_several_days_stays_visible(self):
        for q, window in (("2026-04 hour:14", tzwindow.month_bounds(2026, 4, CHICAGO)),
                          ("2026 hour:14", tzwindow.year_bounds(2026, CHICAGO))):
            r = parse_query(q, tz=CHICAGO)
            self.assertEqual(r["time_range"], window, q)    # the date isn't dropped
            self.assertEqual(r["free_text"], ["hour:14"], q)

    def test_narrowest_date_wins_in_either_order(self):
        want = tzwindow.day_bounds(date(2026, 4, 29), CHICAGO)
        self.assertEqual(parse_query("2026-04 2026-04-29", tz=CHICAGO)["time_range"], want)
        self.assertEqual(parse_query("2026-04-29 2026-04", tz=CHICAGO)["time_range"], want)

    def test_labels(self):
        for q, label in (("today", "Today"), ("last:1d", "Today"), ("last:7d", "Last 7 days"),
                         ("hour:14", "Hour 14:00 today"), ("today hour:14", "Hour 14:00 today"),
                         ("hour:8-10", "Hours 08:00–10:59 today"),
                         ("2026-04-29", "2026-04-29"), ("4/29/26", "2026-04-29"),
                         ("2026-04-29 hour:14", "2026-04-29 14:00–14:59"),
                         ("2026-04-29 hour:8-10", "2026-04-29 08:00–10:59"),
                         ("2026-04", "April 2026"), ("2026", "2026"),
                         ("2026-04 hour:14", "April 2026")):
            self.assertEqual(parse_query(q, tz=CHICAGO)["time_label"], label, q)
        self.assertIsNone(parse_query("B738", tz=CHICAGO)["time_label"])

    def test_label_on_a_clock_change_day(self):
        """A 25-hour today used to fall out of the page's delta === 86400 check."""
        now = utc(2026, 11, 1, 18)
        with mock.patch.object(tzwindow.time, "time", return_value=now):
            r = parse_query("today", tz=CHICAGO)
        self.assertEqual(r["time_range"][1] - r["time_range"][0], 25 * 3600)
        self.assertEqual(r["time_label"], "Today")

    def test_tokens_rebuild_the_same_query(self):
        """What the page rejoins after a chip is removed: same window, same filters."""
        for q in ("Delta last:7d", "B738 2026-04-29 hour:14", "Air Canada 2026-04", "distance:50-100 today",
                  "United States hour:14", "commercial helicopter 2026", "mil first_seen_today",
                  "N12345 today", "2026-04 hour:14 foo", "G-EXMP peak_today"):
            r = parse_query(q, tz=CHICAGO)
            parts = []
            for f in r["filters"]:
                if f["token"] not in parts:
                    parts.append(f["token"])
            parts += r["free_text"] + r["time_tokens"]
            again = parse_query(" ".join(parts), tz=CHICAGO)
            self.assertEqual(again["time_range"], r["time_range"], q)
            self.assertEqual(sorted(str(f["value"]) for f in again["filters"]),
                             sorted(str(f["value"]) for f in r["filters"]), q)
            self.assertEqual(again["free_text"], r["free_text"], q)


class TestAircraftPage(_DbCase):

    def fill(self):
        # Monday 2026-06-15 01:00 UTC is Sunday evening in Chicago.
        self.first = utc(2026, 6, 15, 1)
        self.last = utc(2026, 6, 25, 1)
        self.aircraft("A00001", self.first, self.last, count=50)
        self.hour("A00001", self.first, 50)

    def test_patterns_are_local(self):
        self.fill()
        out = search.detail_page_data_for_aircraft(self.conn, "A00001", CHICAGO)
        self.assertEqual(out["hour_of_day"][20], 50)     # 01:00 UTC = 20:00 CDT
        self.assertEqual(out["day_of_week"][6], 50)      # Sunday there
        self.assertEqual(out["ranges"]["days_active"], 1)

    def test_same_data_in_utc(self):
        self.fill()
        out = search.detail_page_data_for_aircraft(self.conn, "A00001", timezone.utc)
        self.assertEqual(out["hour_of_day"][1], 50)
        self.assertEqual(out["day_of_week"][0], 50)

    def test_active_days_never_exceed_the_span(self):
        """Seen in the evening of day 1 and the morning of day 2: two calendar days,
        under 24 hours apart. Used to read "2 of 1 days"."""
        first = utc(2026, 6, 15, 22)
        last = utc(2026, 6, 16, 6)
        self.aircraft("A00002", first, last, count=50)
        self.hour("A00002", first, 25)
        self.hour("A00002", last, 25)
        out = search.detail_page_data_for_aircraft(self.conn, "A00002", timezone.utc)
        chip = next(c for c in out["chips"] if c["label"] == "Active")
        self.assertEqual(chip["value"], "2 of 2 days")

    def test_floor_when_first_seen_lags_the_rollup(self):
        first = utc(2026, 6, 16, 12)
        self.aircraft("A00003", first, first, count=50)
        self.hour("A00003", first - 3 * DAY, 25)
        self.hour("A00003", first, 25)
        out = search.detail_page_data_for_aircraft(self.conn, "A00003", timezone.utc)
        chip = next(c for c in out["chips"] if c["label"] == "Active")
        self.assertEqual(chip["value"], "2 of 2 days")


class TestClockChangeBuckets(_DbCase):
    """Each second's histogram bucket must be the hour whose window holds it,
    including on clock-change days and in half-hour zones. Runs the SQL expression."""

    def check(self, day, tz):
        start, end = tzwindow.day_bounds(day, tz)
        offset_sql = server._histogram_offset_sql(tzwindow.offset_segments(start, end, tz))
        windows = {h: tzwindow.hour_bounds(day, h, h, tz) for h in range(24)}
        for ts in range(start, end, 600):
            self.raw("A00001", ts)
        rows = self.conn.execute(
            f"SELECT seen_at, ((seen_at + {offset_sql}) / 3600) % 24 FROM all_sightings").fetchall()
        self.assertEqual(len(rows), (end - start) // 600)
        for seen_at, bucket in rows:
            lo, hi = windows[bucket]
            self.assertTrue(lo <= seen_at < hi, (day, seen_at, bucket, lo, hi))
        self.conn.execute("DELETE FROM all_sightings")
        return windows

    def test_autumn(self):
        windows = self.check(date(2026, 11, 1), CHICAGO)
        self.assertEqual(windows[1][1] - windows[1][0], 2 * 3600)

    def test_spring(self):
        windows = self.check(date(2026, 3, 8), CHICAGO)
        self.assertEqual(windows[2][0], windows[2][1])

    def test_ordinary_and_half_hour(self):
        self.check(date(2026, 6, 15), CHICAGO)
        self.check(date(2026, 6, 15), KOLKATA)
        self.check(date(2026, 6, 15), timezone.utc)


class TestThroughTheApp(_DbCase):
    """A Stats card, its drill and a Search for the same thing find the same aircraft."""

    TZ_NAMES = ("America/Chicago", "Asia/Kolkata", "UTC", "")

    def app_for(self, tz_name):
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        self.cfg["stats"]["timezone"] = tz_name
        with mock.patch.object(hexdb_resolver, "start_worker"):
            self.app = server.get_app(self.cfg, os.devnull)

    def endpoint(self, path):
        return next(r.endpoint for r in self.app.routes if getattr(r, "path", "") == path)

    def api_search(self, q="", **kw):
        args = dict(q=q, limit=500, offset=0, order="", dir="", from_ts=0, to_ts=0)
        args.update(kw)
        return self.endpoint("/api/search")(**args)

    def test_the_today_button_means_today_in_aerodromes_timezone(self):
        now = int(time.time())
        for n, tz_name in enumerate(self.TZ_NAMES):
            with self.subTest(tz=tz_name):
                self.app_for(tz_name)
                start = tzwindow.today_bounds(tzwindow.resolve_tz(self.cfg))[0]
                today, yesterday = f"B0000{n}", f"C0000{n}"
                self.sight(today, max(start, now - 1))
                self.sight(yesterday, start - 1)
                self.conn.commit()
                found = {r["icao"] for r in self.api_search(preset="today")["rows"]}
                self.assertIn(today, found)
                self.assertNotIn(yesterday, found)

    def sight(self, icao, ts):
        """Everything the collector would have written for one sighting."""
        row = self.conn.execute("SELECT first_seen_at, last_seen_at FROM seen_aircraft WHERE icao=?",
                                (icao,)).fetchone()
        if row is None:
            self.aircraft(icao, ts, ts)
        else:
            self.conn.execute("UPDATE seen_aircraft SET first_seen_at=MIN(first_seen_at,?), "
                              "last_seen_at=MAX(last_seen_at,?), sighting_count=sighting_count+1 "
                              "WHERE icao=?", (ts, ts, icao))
        self.raw(icao, ts)
        self.conn.execute(
            "INSERT INTO sightings_hourly (icao, hour_bucket, sighting_count, first_seen_at, last_seen_at) "
            "VALUES (?, ?, 1, ?, ?) ON CONFLICT(icao, hour_bucket) DO UPDATE SET "
            "sighting_count = sighting_count + 1, last_seen_at = excluded.last_seen_at",
            (icao, ts - ts % 3600, ts, ts))
        self.conn.commit()

    def seed(self, tz):
        """Five days of sightings, with one aircraft last seen today but also earlier."""
        now = int(time.time())
        today = tzwindow.today(tz, now)
        start, _ = tzwindow.day_bounds(today, tz)
        early = max(start, now - 1)      # certainly inside today and in the past
        self.sight("A00001", early - 3 * DAY)
        self.sight("A00001", early)
        self.sight("A00002", early - 3 * DAY)
        self.sight("A00003", early - DAY)
        self.sight("A00004", early)
        return now, today, early

    def reset(self):
        for table in ("seen_aircraft", "all_sightings", "sightings_hourly"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    def test_card_drill_and_search_agree(self):
        for name in self.TZ_NAMES:
            with self.subTest(timezone=name or "(system)"):
                self.reset()
                self.app_for(name)
                tz = tzwindow.resolve_tz(self.cfg)
                now, today, early = self.seed(tz)
                cards = self.endpoint("/api/stats")()["cards"]

                # 7-day card == day drill == Search on that date.
                drill = self.endpoint("/api/stats/drill")
                for bucket in cards["daily_counts_7d"]:
                    got = drill(card="daily_counts_7d", value=bucket["date"], all=False)
                    icaos = {r["icao"] for r in got["rows"]} if isinstance(got, dict) else None
                    searched = self.api_search(bucket["date"])
                    self.assertEqual(bucket["n"], len(icaos), bucket["date"])
                    self.assertEqual(icaos, {r["icao"] for r in searched["rows"]}, bucket["date"])

                # Histogram bar == hour drill == Search for that hour today.
                bars = {b["hour"]: b["n"] for b in cards["hourly_histogram"]}
                self.assertGreater(sum(bars.values()), 0)
                for hour, n in bars.items():
                    got = drill(card="hourly_histogram", value=str(hour), all=False)
                    icaos = {r["icao"] for r in got["rows"]}
                    self.assertEqual(n, len(icaos), hour)
                    searched = self.api_search(f"today hour:{hour}")
                    self.assertTrue(icaos <= {r["icao"] for r in searched["rows"]}, hour)

    def test_half_hour_zone_today_cards_start_at_midnight(self):
        """v3.4.171: in Kolkata the hour holding midnight is half yesterday; the
        Today cards count 00:10 today and not 23:50 yesterday."""
        self.app_for("Asia/Kolkata")
        start = tzwindow.today_bounds(KOLKATA)[0]
        if int(time.time()) < start + 900:
            self.skipTest("too close to Kolkata midnight for a 00:11 reading to exist yet")
        for icao, ts, alt, spd in (("A00010", start + 600, 30000, 450), ("A00010", start + 660, 30100, 452),
                                   ("A00011", start - 600, 41000, 500), ("A00011", start - 540, 41000, 501)):
            self.sight(icao, ts)
            self.conn.execute("UPDATE all_sightings SET altitude=?, speed=?, aircraft_type='B738', callsign=? "
                              "WHERE icao=? AND seen_at=?", (alt, spd, "EX" + icao[-2:], icao, ts))
            self.conn.execute("UPDATE sightings_hourly SET max_altitude=MAX(COALESCE(max_altitude,0),?), "
                              "min_nonzero_altitude=MIN(COALESCE(min_nonzero_altitude,1e9),?), "
                              "max_speed=MAX(COALESCE(max_speed,0),?), aircraft_type='B738', callsign=? "
                              "WHERE icao=? AND hour_bucket=?", (alt, alt, spd, "EX" + icao[-2:], icao, ts - ts % 3600))
        self.conn.commit()
        cards = self.endpoint("/api/stats")()["cards"]
        self.assertEqual(cards["unique_today"], 1)
        self.assertEqual([a["icao"] for a in cards["top_aircraft"]], ["A00010"])
        self.assertEqual(cards["top_aircraft"][0]["n"], 2)
        self.assertEqual(cards["highest_altitude"]["icao"], "A00010")
        self.assertEqual(cards["lowest_altitude"]["icao"], "A00010")
        self.assertEqual((cards["fastest"] or {}).get("icao"), "A00010")

    def test_half_hour_zone_day_edges_agree(self):
        """Both ends of a Kolkata day fall inside a UTC rollup bucket."""
        self.app_for("Asia/Kolkata")
        tz = KOLKATA
        now = int(time.time())
        yesterday = date.fromordinal(tzwindow.today(tz, now).toordinal() - 1)
        start, end = tzwindow.day_bounds(yesterday, tz)
        self.sight("A00008", start + 600)      # 00:10 local
        self.sight("A00009", end - 600)        # 23:50 local
        cards = self.endpoint("/api/stats")()["cards"]
        days = {b["date"]: b["n"] for b in cards["daily_counts_7d"]}
        self.assertEqual(days[yesterday.isoformat()], 2)
        self.assertEqual(days[date.fromordinal(yesterday.toordinal() - 1).isoformat()], 0)
        self.assertEqual(days[tzwindow.today(tz, now).isoformat()], 0)
        got = self.endpoint("/api/stats/drill")(card="daily_counts_7d",
                                                value=yesterday.isoformat(), all=False)
        self.assertEqual({r["icao"] for r in got["rows"]}, {"A00008", "A00009"})
        self.assertEqual({r["icao"] for r in self.api_search(yesterday.isoformat())["rows"]},
                         {"A00008", "A00009"})

    def test_empty_timezone_means_system_time_everywhere(self):
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        try:
            self.app_for("")
            out = parse_query("today")
            self.assertEqual(out["time_range"], tzwindow.today_bounds(ZoneInfo("Asia/Tokyo")))
            self.assertEqual(self.api_search("today")["time_range"], out["time_range"])
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

    def test_response_carries_the_chip_fields(self):
        self.app_for("America/Chicago")
        out = self.api_search("2026-04-29 hour:14")
        self.assertEqual(out["time_label"], "2026-04-29 14:00–14:59")
        self.assertEqual(out["time_tokens"], ["2026-04-29", "hour:14"])

    def test_huge_numbers_find_nothing_rather_than_fail(self):
        self.app_for("UTC")
        self.sight("A00001", int(time.time()) - 60)
        huge = 2 ** 70
        self.assertEqual(self.api_search(offset=huge)["rows"], [])
        self.assertEqual(self.api_search(from_ts=huge)["rows"], [])
        self.assertTrue(self.api_search(to_ts=huge)["ok"])
        self.assertEqual(self.api_search("last:99999999999999999999999d")["time_range"], None)
        self.assertTrue(self.api_search("foo\x00bar")["ok"])

    def test_aircraft_page_uses_the_setting(self):
        self.app_for("America/Chicago")
        ts = utc(2026, 6, 15, 1)
        self.aircraft("A00001", ts, ts, count=50)
        self.hour("A00001", ts, 50)
        got = self.endpoint("/api/aircraft/{icao}")("A00001")
        self.assertEqual(got["hour_of_day"][20], 50)


class TestDigestTimes(unittest.TestCase):

    def test_hhmm_with_no_timezone_is_system_time(self):
        from notifier import Notifier
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        try:
            n = Notifier({}, stats_timezone=None)
            self.assertEqual(n._format_hhmm(utc(2026, 6, 15, 1)), "10:00")
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

    def test_hhmm_with_a_timezone(self):
        from notifier import Notifier
        n = Notifier({}, stats_timezone="America/Chicago")
        self.assertEqual(n._format_hhmm(utc(2026, 6, 15, 1)), "20:00")


class TestLastStateOnThePage(_DbCase):
    """Speed, altitude and squawk are read for the page of results, not for every match."""

    def setUp(self):
        super().setUp()
        for i, icao in enumerate(("A00001", "A00002", "A00003")):
            self.aircraft(icao, 100, 7200 + i, count=10 - i)
        self.conn.executemany(
            "INSERT INTO sightings_hourly (icao, hour_bucket, sighting_count, first_seen_at, last_seen_at,"
            " last_speed, last_altitude, last_squawk) VALUES (?,?,1,?,?,?,?,?)",
            [("A00001", 3600, 3600, 3600, 111, 1000, "1111"),   # older hour: must not be used
             ("A00001", 7200, 7200, 7200, 300, 30000, "1200"),
             ("A00002", 7200, 7200, 7200, 500, 20000, "7000")])
        self.conn.commit()

    def rows(self, **kwargs):
        return execute_search(self.conn, parse_query(""), **kwargs)["rows"]

    def test_each_row_gets_its_latest_hours_state(self):
        by_icao = {r["icao"]: r for r in self.rows()}
        self.assertEqual((by_icao["A00001"]["last_speed"], by_icao["A00001"]["last_altitude"],
                          by_icao["A00001"]["last_squawk"]), (300, 30000, "1200"))
        self.assertEqual(by_icao["A00002"]["last_speed"], 500)

    def test_an_aircraft_with_no_hourly_row_is_still_listed(self):
        by_icao = {r["icao"]: r for r in self.rows()}
        self.assertIsNone(by_icao["A00003"]["last_speed"])
        self.assertIsNone(by_icao["A00003"]["last_squawk"])

    def test_sorting_by_a_last_state_column_ranks_every_match(self):
        self.assertEqual([r["icao"] for r in self.rows(order="speed", direction="desc")],
                         ["A00002", "A00001", "A00003"])
        self.assertEqual([r["icao"] for r in self.rows(order="speed", direction="asc")],
                         ["A00001", "A00002", "A00003"])
        self.assertEqual(self.rows(order="speed", direction="desc", limit=1)[0]["last_altitude"], 20000)

    def test_only_those_sorts_join_it_and_the_lookup_covers_only_the_page(self):
        sql = []
        self.conn.set_trace_callback(sql.append)
        try:
            rows = self.rows(limit=1)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(rows), 1)
        self.assertFalse([s for s in sql if "latest_h" in s], "the ranking query joined the latest hour")
        lookups = [s for s in sql if "h.last_speed" in s]
        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0].count("'A0000"), 1)
        sql.clear()
        self.conn.set_trace_callback(sql.append)
        try:
            self.rows(order="altitude", direction="desc")
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue([s for s in sql if "latest_h" in s])

    def test_free_text_with_its_relevance_join_still_gets_state(self):
        self.conn.execute("INSERT INTO seen_aircraft_fts (rowid, icao, registration, last_callsign, aircraft_type,"
                          " aircraft_type_desc, operator, country) SELECT rowid, icao, 'N1', 'TESTCALL', '', '', '', ''"
                          " FROM seen_aircraft WHERE icao = 'A00002'")
        self.conn.commit()
        rows = execute_search(self.conn, parse_query("testcall"))["rows"]
        self.assertEqual([r["icao"] for r in rows], ["A00002"])
        self.assertEqual(rows[0]["last_squawk"], "7000")

    def test_a_date_range_sort_runs_its_sum_once_per_row(self):
        sql = []
        self.conn.set_trace_callback(sql.append)
        try:
            execute_search(self.conn, parse_query("1970-01-01", tz=timezone.utc), order="sightings", direction="desc")
        finally:
            self.conn.set_trace_callback(None)
        ranking = next(s for s in sql if "ORDER BY" in s and "score" in s)
        self.assertEqual(ranking.count("SUM(w.sighting_count)"), 1)


if __name__ == "__main__":
    unittest.main()
