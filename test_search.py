"""
Test harness for search.py (v2.51.0 Phase 2).

Two layers of tests:

  1. TestParser — unit tests on parse_query(). No DB needed. Verifies
     each token-classification branch, including ambiguous tokens
     and time-range parsing.

  2. TestExecutor — integration tests on execute_search(). Builds a
     real SQLite DB with the v2.51 schema, populates it with a few
     test aircraft covering various filter dimensions, and asserts
     that queries return the expected aircraft in the expected order.

Run:
    python3 test_search.py
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from search import (
    parse_query, execute_search, detail_for_aircraft,
    _fts_quote, _try_parse_date,
)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestParser(unittest.TestCase):
    """Unit tests on the token classifier. No DB required."""

    def test_empty_query(self):
        for q in ("", "   ", None):
            r = parse_query(q)
            self.assertEqual(r["filters"], [])
            self.assertEqual(r["free_text"], [])
            self.assertIsNone(r["time_range"])

    def test_icao_hex(self):
        r = parse_query("A12345")
        self.assertEqual(len(r["filters"]), 1)
        self.assertEqual(r["filters"][0]["field"], "icao")
        self.assertEqual(r["filters"][0]["value"], "A12345")
        self.assertEqual(r["filters"][0]["match"], "exact")

    def test_icao_lowercase_canonicalized(self):
        # User types lowercase hex; we canonicalize to uppercase since
        # the storage is uppercase.
        r = parse_query("a12345")
        self.assertEqual(r["filters"][0]["value"], "A12345")

    def test_aircraft_type_code(self):
        r = parse_query("B738")
        # B738 is in AIRCRAFT_TYPES, so it classifies as type. It also
        # matches the registration regex though, so it's ambiguous.
        # Verify both filters appear and are in the same ambiguous group.
        type_filters = [f for f in r["filters"] if f["field"] == "aircraft_type"]
        reg_filters = [f for f in r["filters"] if f["field"] == "registration"]
        self.assertEqual(len(type_filters), 1)
        self.assertEqual(type_filters[0]["value"], "B738")
        # Either both have ambiguous_group set to the same id, or just type
        if reg_filters:
            self.assertEqual(type_filters[0]["ambiguous_group"],
                             reg_filters[0]["ambiguous_group"])

    def test_country_single_word(self):
        r = parse_query("Canada")
        self.assertEqual(len(r["filters"]), 1)
        self.assertEqual(r["filters"][0]["field"], "country")
        self.assertEqual(r["filters"][0]["value"], "Canada")

    def test_country_two_word(self):
        r = parse_query("United States")
        country_filters = [f for f in r["filters"] if f["field"] == "country"]
        self.assertEqual(len(country_filters), 1)
        self.assertEqual(country_filters[0]["value"], "United States")
        # And no free-text leftover
        self.assertEqual(r["free_text"], [])

    def test_country_case_insensitive(self):
        r = parse_query("canada")
        country_filters = [f for f in r["filters"] if f["field"] == "country"]
        self.assertEqual(len(country_filters), 1)
        self.assertEqual(country_filters[0]["value"], "Canada")  # canonicalized

    def test_callsign_with_digits(self):
        r = parse_query("UAL2024")
        cs_filters = [f for f in r["filters"] if f["field"] == "last_callsign"]
        self.assertEqual(len(cs_filters), 1)
        self.assertEqual(cs_filters[0]["value"], "UAL2024")
        self.assertEqual(cs_filters[0]["match"], "exact")

    def test_known_callsign_prefix(self):
        # UAL is in the AIRLINES table — should map to a prefix match
        r = parse_query("UAL")
        cs_filters = [f for f in r["filters"] if f["field"] == "last_callsign"]
        self.assertEqual(len(cs_filters), 1)
        self.assertEqual(cs_filters[0]["match"], "prefix")
        self.assertEqual(cs_filters[0]["value"], "UAL")

    def test_unknown_3letter_falls_to_freetext(self):
        # XYZ is not a known airline — should be treated as free text
        r = parse_query("XYZ")
        cs_filters = [f for f in r["filters"] if f["field"] == "last_callsign"]
        self.assertEqual(len(cs_filters), 0)
        self.assertIn("XYZ", r["free_text"])

    def test_us_registration(self):
        r = parse_query("N12345")
        reg_filters = [f for f in r["filters"] if f["field"] == "registration"]
        self.assertEqual(len(reg_filters), 1)
        self.assertEqual(reg_filters[0]["value"], "N12345")

    def test_combined_filters(self):
        r = parse_query("B738 Canada")
        types = [f for f in r["filters"] if f["field"] == "aircraft_type"]
        countries = [f for f in r["filters"] if f["field"] == "country"]
        self.assertEqual(len(types), 1)
        self.assertEqual(len(countries), 1)
        self.assertEqual(types[0]["value"], "B738")
        self.assertEqual(countries[0]["value"], "Canada")

    def test_date_full(self):
        r = parse_query("2026-04-29")
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        # The day starts at 2026-04-29 UTC
        import calendar
        expected_t0 = calendar.timegm((2026, 4, 29, 0, 0, 0, 0, 0, 0))
        self.assertEqual(t0, expected_t0)
        self.assertEqual(t1 - t0, 86400)

    def test_date_month(self):
        r = parse_query("2026-04")
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        # April has 30 days
        self.assertEqual(t1 - t0, 30 * 86400)

    def test_date_year(self):
        r = parse_query("2026")
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        # 2026 is not a leap year
        self.assertEqual(t1 - t0, 365 * 86400)

    def test_year_must_be_2000s(self):
        # 1999 shouldn't classify as a year
        r = parse_query("1999")
        self.assertIsNone(r["time_range"])
        self.assertIn("1999", r["free_text"])

    # v2.52.0: locale-aware slash-separated dates
    def test_slash_date_mdy(self):
        r = parse_query("4/29/26", date_format="MDY")
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        self.assertEqual(t1 - t0, 86400)
        # April 29, 2026 = epoch 1777593600
        from datetime import datetime, timezone
        d = datetime.fromtimestamp(t0, tz=timezone.utc)
        self.assertEqual((d.year, d.month, d.day), (2026, 4, 29))

    def test_slash_date_dmy(self):
        r = parse_query("29/4/26", date_format="DMY")
        self.assertIsNotNone(r["time_range"])
        t0, _ = r["time_range"]
        from datetime import datetime, timezone
        d = datetime.fromtimestamp(t0, tz=timezone.utc)
        self.assertEqual((d.year, d.month, d.day), (2026, 4, 29))

    def test_slash_date_mdy_rejects_dmy_input(self):
        # 29/4/26 in MDY mode means month 29, which is invalid → free text
        r = parse_query("29/4/26", date_format="MDY")
        self.assertIsNone(r["time_range"])
        self.assertIn("29/4/26", r["free_text"])

    def test_slash_date_iso_rejects_slash(self):
        # ISO mode rejects slash dates entirely as ambiguous
        r = parse_query("4/29/26", date_format="ISO")
        self.assertIsNone(r["time_range"])
        self.assertIn("4/29/26", r["free_text"])

    def test_iso_works_in_all_locales(self):
        # ISO format always works regardless of date_format setting
        for fmt in ("MDY", "DMY", "ISO"):
            r = parse_query("2026-04-29", date_format=fmt)
            self.assertIsNotNone(r["time_range"], f"ISO date should parse in {fmt}")

    def test_slash_date_4digit_year(self):
        # 4-digit year variant
        r = parse_query("4/29/2026", date_format="MDY")
        self.assertIsNotNone(r["time_range"])
        t0, _ = r["time_range"]
        from datetime import datetime, timezone
        d = datetime.fromtimestamp(t0, tz=timezone.utc)
        self.assertEqual((d.year, d.month, d.day), (2026, 4, 29))

    def test_slash_date_invalid_month_falls_through(self):
        # Month 13 isn't valid → token falls to free-text
        r = parse_query("13/45/26", date_format="MDY")
        self.assertIsNone(r["time_range"])
        self.assertIn("13/45/26", r["free_text"])

    def test_slash_date_2digit_year_resolution(self):
        # 26 → 2026, not 1926 (POSIX strptime convention: 00-69 → 2000s)
        r = parse_query("4/29/26", date_format="MDY")
        t0, _ = r["time_range"]
        from datetime import datetime, timezone
        d = datetime.fromtimestamp(t0, tz=timezone.utc)
        self.assertEqual(d.year, 2026)
        # And 75 → 1975 (would be too old for ADS-B but tests the boundary)
        r = parse_query("4/29/75", date_format="MDY")
        t0, _ = r["time_range"]
        d = datetime.fromtimestamp(t0, tz=timezone.utc)
        self.assertEqual(d.year, 1975)

    def test_slash_date_combined_with_filter(self):
        r = parse_query("Canada 4/29/26", date_format="MDY")
        self.assertIsNotNone(r["time_range"])
        self.assertEqual(len(r["filters"]), 1)
        self.assertEqual(r["filters"][0]["field"], "country")
        self.assertEqual(r["filters"][0]["value"], "Canada")

    def test_invalid_date_format_falls_to_mdy(self):
        # Garbage date_format value should fall back to MDY default
        r = parse_query("4/29/26", date_format="INVALID")
        self.assertIsNotNone(r["time_range"], "Invalid date_format should fall back to MDY")

    def test_default_date_format_is_mdy(self):
        # No date_format passed → defaults to MDY
        r = parse_query("4/29/26")
        self.assertIsNotNone(r["time_range"])

    def test_too_many_tokens_truncated(self):
        from search import MAX_TOKENS
        q = " ".join(["foo"] * (MAX_TOKENS + 5))
        r = parse_query(q)
        self.assertEqual(len(r["raw_tokens"]), MAX_TOKENS)

    def test_unrecognized_token_is_freetext(self):
        r = parse_query("hello")
        self.assertEqual(r["filters"], [])
        self.assertIn("hello", r["free_text"])

    # ------------------------------------------------------------------
    # v2.65.0 (Phase 2): today / hour:N / distance:LO-HI parser tokens
    # ------------------------------------------------------------------
    def test_today_token_resolves_to_local_day(self):
        # v2.66.2: parse_query without tz_offset uses offset=0 (UTC).
        # The window's start is at unix midnight (% 86400 == 0).
        r = parse_query("today")
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        # Window is exactly 24h
        self.assertEqual(t1 - t0, 86400)
        # Default offset=0 → start aligns to unix midnight (UTC)
        self.assertEqual(t0 % 86400, 0)
        # Free text empty (today shouldn't fall through)
        self.assertEqual(r["free_text"], [])

    def test_hour_token_basic(self):
        # v2.66.2: with default offset=0 (UTC), hour:14 starts at unix
        # 14:00 UTC today → t0 % 86400 == 14 * 3600.
        r = parse_query("hour:14")
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        # Window is exactly 1h
        self.assertEqual(t1 - t0, 3600)
        # Default offset=0 → 14:00 UTC alignment
        self.assertEqual(t0 % 86400, 14 * 3600)

    def test_hour_token_boundaries(self):
        # 0 and 23 both valid
        r0 = parse_query("hour:0")
        self.assertIsNotNone(r0["time_range"])
        r23 = parse_query("hour:23")
        self.assertIsNotNone(r23["time_range"])

    def test_hour_token_rejects_invalid(self):
        # Out of range, negative, non-numeric all fall through to free text
        for bad in ("hour:24", "hour:-1", "hour:abc", "hour:"):
            r = parse_query(bad)
            self.assertIsNone(r["time_range"], f"{bad} should not match")
            self.assertIn(bad, r["free_text"])

    def test_distance_range_basic(self):
        r = parse_query("distance:50-100")
        self.assertEqual(len(r["filters"]), 1)
        f = r["filters"][0]
        self.assertEqual(f["field"], "distance")
        self.assertEqual(f["match"], "range")
        self.assertEqual(f["value"], [50.0, 100.0])

    def test_distance_range_rejects_invalid(self):
        # Missing dash, non-numeric, lo>=hi, negative all fall through
        for bad in ("distance:50", "distance:abc-100", "distance:100-50",
                     "distance:50-50", "distance:-10-50"):
            r = parse_query(bad)
            distance_filters = [f for f in r["filters"]
                                if f.get("field") == "distance"]
            self.assertEqual(len(distance_filters), 0,
                             f"{bad} should not produce a distance filter")

    def test_today_and_hour_narrower_wins(self):
        # `today hour:14` and `hour:14 today` should both end up with
        # the 1-hour window, not the 24h window. Tests "narrower wins"
        # merging.
        r1 = parse_query("today hour:14")
        self.assertIsNotNone(r1["time_range"])
        self.assertEqual(r1["time_range"][1] - r1["time_range"][0], 3600)

        r2 = parse_query("hour:14 today")
        self.assertIsNotNone(r2["time_range"])
        self.assertEqual(r2["time_range"][1] - r2["time_range"][0], 3600)

    def test_distance_unit_conversion(self):
        # When distance_unit='mi' (default), bounds in miles convert to
        # km via the inverse of 0.621371. Verify by building WHERE
        # clauses for each unit and inspecting the params.
        from search import _build_where
        parsed = parse_query("distance:50-100")
        # mi → km: 50 mi ≈ 80.47 km, 100 mi ≈ 160.93 km
        _, params_mi = _build_where(parsed, distance_unit="mi")
        # The distance bounds are the last two numeric params
        nums_mi = [p for p in params_mi if isinstance(p, float)]
        self.assertAlmostEqual(nums_mi[0], 50 / 0.621371, places=2)
        self.assertAlmostEqual(nums_mi[1], 100 / 0.621371, places=2)

        # km → km: 1:1
        _, params_km = _build_where(parsed, distance_unit="km")
        nums_km = [p for p in params_km if isinstance(p, float)]
        self.assertAlmostEqual(nums_km[0], 50.0, places=2)
        self.assertAlmostEqual(nums_km[1], 100.0, places=2)

        # nmi → km: 50 nmi ≈ 92.6 km
        _, params_nmi = _build_where(parsed, distance_unit="nmi")
        nums_nmi = [p for p in params_nmi if isinstance(p, float)]
        self.assertAlmostEqual(nums_nmi[0], 50 / 0.539957, places=2)
        self.assertAlmostEqual(nums_nmi[1], 100 / 0.539957, places=2)

    # ------------------------------------------------------------------
    # v2.66.2: timezone-aware `today` / `hour:N` tokens
    # ------------------------------------------------------------------
    def test_today_token_tz_offset_aligns_to_user_midnight(self):
        # tz_offset_sec=-25200 → UTC-7 (e.g. America/Los_Angeles in summer).
        # The window's start should be unix midnight in UTC-7. Equivalently,
        # (start_ts + tz_offset) % 86400 == 0.
        offset = -25200
        r = parse_query("today", tz_offset_sec=offset)
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        self.assertEqual(t1 - t0, 86400)
        self.assertEqual((t0 + offset) % 86400, 0,
                         "today's start should be midnight in user-tz")

    def test_hour_token_tz_offset_aligns_to_user_hour(self):
        # In UTC-7, hour:12 should produce a window where
        # (start_ts + tz_offset) % 86400 == 12 * 3600.
        offset = -25200  # UTC-7
        r = parse_query("hour:12", tz_offset_sec=offset)
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        self.assertEqual(t1 - t0, 3600)
        self.assertEqual((t0 + offset) % 86400, 12 * 3600,
                         "hour:12 should be noon in user-tz")

    def test_hour_token_tz_offset_matches_histogram_semantics(self):
        # The Patterns hourly_histogram buckets aircraft via
        # ((seen_at + tz_offset) / 3600) % 24. So aircraft in bucket 14
        # have (seen_at + tz_offset) % 86400 in [14*3600, 15*3600).
        # Verify the parser's hour:14 window matches that exact bucket.
        offset = -18000  # UTC-5
        r = parse_query("hour:14", tz_offset_sec=offset)
        t0, t1 = r["time_range"]
        # An aircraft seen at the parser-window start should bucket at hour 14
        sample_ts = t0
        bucket = ((sample_ts + offset) // 3600) % 24
        self.assertEqual(bucket, 14)
        # An aircraft seen 1 second before the window should bucket at hour 13
        bucket_just_before = ((t0 - 1 + offset) // 3600) % 24
        self.assertEqual(bucket_just_before, 13)
        # An aircraft seen at the window end should bucket at hour 15
        bucket_at_end = ((t1 + offset) // 3600) % 24
        self.assertEqual(bucket_at_end, 15)

    def test_today_token_default_offset_is_zero(self):
        # No tz_offset_sec parameter → server-local UTC behavior (offset=0).
        # The window should be aligned to UTC midnight.
        r = parse_query("today")
        t0, t1 = r["time_range"]
        self.assertEqual(t0 % 86400, 0,
                         "default offset=0 → UTC midnight alignment")

    # ------------------------------------------------------------------
    # v2.69.0 (Phase 3): first_seen_today boolean filter
    # ------------------------------------------------------------------
    def test_first_seen_today_token_recognized(self):
        r = parse_query("first_seen_today")
        # Should produce one filter dict, no free-text fallthrough.
        self.assertEqual(r["free_text"], [])
        self.assertEqual(len(r["filters"]), 1)
        f = r["filters"][0]
        self.assertEqual(f["field"], "first_seen_today")
        self.assertEqual(f["match"], "boolean")
        self.assertTrue(f["value"])
        # Range present; 24h window aligned to UTC midnight at default offset.
        self.assertIn("first_seen_range", f)
        t0, t1 = f["first_seen_range"]
        self.assertEqual(t1 - t0, 86400)
        self.assertEqual(t0 % 86400, 0)
        # Must NOT set time_range (that's last_seen_at; this is first_seen_at).
        self.assertIsNone(r["time_range"])

    def test_first_seen_today_token_tz_offset(self):
        # UTC-7: range should be aligned to user-tz midnight.
        offset = -25200
        r = parse_query("first_seen_today", tz_offset_sec=offset)
        f = r["filters"][0]
        t0, t1 = f["first_seen_range"]
        self.assertEqual(t1 - t0, 86400)
        self.assertEqual((t0 + offset) % 86400, 0,
                         "range start should be midnight in user-tz")

    def test_first_seen_today_composes_with_other_filters(self):
        # `first_seen_today military` should produce both filters.
        r = parse_query("first_seen_today military")
        fields = [f["field"] for f in r["filters"]]
        self.assertIn("first_seen_today", fields)
        self.assertIn("military", fields)

    def test_first_seen_today_composes_with_today(self):
        # `today first_seen_today` should set both time_range (from today)
        # AND the first_seen_today filter. They're independent columns.
        r = parse_query("today first_seen_today")
        self.assertIsNotNone(r["time_range"])
        self.assertTrue(any(f["field"] == "first_seen_today"
                             for f in r["filters"]))

    def test_first_seen_today_emits_where_clause(self):
        # _build_where should emit a clause referencing
        # seen_aircraft.first_seen_at with the precomputed range.
        from search import _build_where
        r = parse_query("first_seen_today")
        clauses, params = _build_where(r)
        # Find the clause referencing first_seen_at
        first_seen_clauses = [c for c in clauses
                              if "seen_aircraft.first_seen_at" in c]
        self.assertEqual(len(first_seen_clauses), 1,
                         "Exactly one first_seen_at clause expected")
        self.assertIn(">=", first_seen_clauses[0])
        self.assertIn("<", first_seen_clauses[0])
        # The two timestamps should be in params, in the same order.
        f = r["filters"][0]
        t0, t1 = f["first_seen_range"]
        self.assertIn(t0, params)
        self.assertIn(t1, params)

    # ------------------------------------------------------------------
    # v2.82.0: peak_today filter (Stats peak_simultaneous redirect)
    # ------------------------------------------------------------------
    def test_peak_today_token_recognized(self):
        # peak_today is a single-word filter — should produce one filter
        # dict with today_range attached, no free-text fallthrough.
        r = parse_query("peak_today")
        self.assertEqual(r["free_text"], [])
        self.assertEqual(len(r["filters"]), 1)
        f = r["filters"][0]
        self.assertEqual(f["field"], "peak_today")
        self.assertEqual(f["match"], "boolean")
        self.assertTrue(f["value"])
        self.assertIn("today_range", f)
        t0, t1 = f["today_range"]
        self.assertEqual(t1 - t0, 86400)
        # peak_icaos / peak_at_ts not yet populated — they require DB
        # access via _resolve_peak_today_if_present at execute time.
        self.assertNotIn("peak_icaos", f)
        # Must NOT set time_range — peak_today filters by ICAO membership
        # in the peak bucket, not by last_seen_at.
        self.assertIsNone(r["time_range"])

    def test_peak_today_composes_with_other_filters(self):
        r = parse_query("peak_today military")
        fields = [f["field"] for f in r["filters"]]
        self.assertIn("peak_today", fields)
        self.assertIn("military", fields)

    def test_peak_today_unresolved_emits_safe_empty_clause(self):
        # If _build_where sees a peak_today filter that hasn't been
        # resolved (peak_icaos missing, e.g. unit test of parser with
        # no DB), it should emit "1=0" rather than every aircraft.
        # Honest empty result, same degradation pattern as
        # military/watchlist with empty config.
        from search import _build_where
        r = parse_query("peak_today")
        clauses, params = _build_where(r)
        self.assertIn("1=0", clauses)



    # ------------------------------------------------------------------
    # v2.70.0 (Phase 3): hour:LO-HI range syntax (inclusive-inclusive)
    # ------------------------------------------------------------------
    def test_hour_range_basic(self):
        # hour:14-16 = 14:00 through 16:59:59 = 3-hour window starting
        # at 14:00. End ts should be midnight + 17*3600 (exclusive).
        r = parse_query("hour:14-16")
        self.assertIsNotNone(r["time_range"])
        t0, t1 = r["time_range"]
        # 3 hours wide (inclusive-inclusive == hours 14, 15, 16)
        self.assertEqual(t1 - t0, 3 * 3600)
        # Default offset=0 → 14:00 UTC alignment
        self.assertEqual(t0 % 86400, 14 * 3600)
        # End at 17:00 UTC (exclusive)
        self.assertEqual(t1 % 86400, 17 * 3600)

    def test_hour_range_single_bucket(self):
        # hour:14-14 should be the same as hour:14 (one-hour window).
        r1 = parse_query("hour:14-14")
        r2 = parse_query("hour:14")
        self.assertEqual(r1["time_range"], r2["time_range"])

    def test_hour_range_full_day(self):
        # hour:0-23 = full day, 24 hours wide.
        r = parse_query("hour:0-23")
        t0, t1 = r["time_range"]
        self.assertEqual(t1 - t0, 24 * 3600)

    def test_hour_range_rejects_wraparound(self):
        # hour:23-1 is a wraparound — rejected.
        r = parse_query("hour:23-1")
        self.assertIsNone(r["time_range"])
        # Falls through to free text.
        self.assertEqual(r["free_text"], ["hour:23-1"])

    def test_hour_range_rejects_out_of_bounds(self):
        # hour:14-25 has hi out of range.
        r = parse_query("hour:14-25")
        self.assertIsNone(r["time_range"])
        # hour:-1-5 has negative lo (also a parse failure due to extra dash).
        # Stricter test: hour:24-25 — both out.
        r2 = parse_query("hour:24-25")
        self.assertIsNone(r2["time_range"])

    def test_hour_range_tz_aware(self):
        # Range alignment also respects tz_offset_sec.
        offset = -25200  # UTC-7
        r = parse_query("hour:14-16", tz_offset_sec=offset)
        t0, t1 = r["time_range"]
        self.assertEqual(t1 - t0, 3 * 3600)
        # Window starts at 14:00 in user-tz
        self.assertEqual((t0 + offset) % 86400, 14 * 3600)

    # ------------------------------------------------------------------
    # v2.70.0 (Phase 3): distance:<N and distance:>N comparison syntax
    # ------------------------------------------------------------------
    def test_distance_lt(self):
        # distance:<100 → range filter [None, 100]
        r = parse_query("distance:<100")
        self.assertEqual(len(r["filters"]), 1)
        f = r["filters"][0]
        self.assertEqual(f["field"], "distance")
        self.assertEqual(f["match"], "range")
        self.assertEqual(f["value"], [None, 100])

    def test_distance_gt(self):
        # distance:>200 → range filter [200, None]
        r = parse_query("distance:>200")
        f = r["filters"][0]
        self.assertEqual(f["value"], [200, None])

    def test_distance_lt_emits_correct_where(self):
        # _build_where for distance:<100 should emit IS NOT NULL + < clause,
        # NO lower-bound clause.
        from search import _build_where
        r = parse_query("distance:<100")
        clauses, params = _build_where(r, distance_unit="mi")
        # One distance clause
        d_clauses = [c for c in clauses if "last_distance" in c]
        self.assertEqual(len(d_clauses), 1)
        # Should mention both NULL check and < but not >= or >
        self.assertIn("IS NOT NULL", d_clauses[0])
        self.assertIn("<", d_clauses[0])
        self.assertNotIn(">=", d_clauses[0])
        self.assertNotIn("> ?", d_clauses[0])
        # Only one numeric param (the upper bound, converted to km)
        self.assertEqual(len(params), 1)
        self.assertAlmostEqual(params[0], 100 / 0.621371, places=2)

    def test_distance_gt_emits_correct_where(self):
        # _build_where for distance:>200 should emit IS NOT NULL + > clause,
        # NO upper-bound clause.
        from search import _build_where
        r = parse_query("distance:>200")
        clauses, params = _build_where(r, distance_unit="mi")
        d_clauses = [c for c in clauses if "last_distance" in c]
        self.assertEqual(len(d_clauses), 1)
        self.assertIn("IS NOT NULL", d_clauses[0])
        self.assertIn("> ?", d_clauses[0])
        # No upper-bound "<" — but careful, IS NOT NULL doesn't have <
        # so just check no explicit upper bound clause exists. Use a
        # stricter check: there should be no "< ?" param-bound clause.
        self.assertNotIn("< ?", d_clauses[0])
        self.assertEqual(len(params), 1)
        self.assertAlmostEqual(params[0], 200 / 0.621371, places=2)

    def test_distance_range_unchanged(self):
        # Existing distance:50-100 behavior unchanged: both bounds, >= on lo, < on hi.
        from search import _build_where
        r = parse_query("distance:50-100")
        clauses, params = _build_where(r, distance_unit="mi")
        d_clauses = [c for c in clauses if "last_distance" in c]
        self.assertEqual(len(d_clauses), 1)
        self.assertIn(">=", d_clauses[0])
        self.assertIn("<", d_clauses[0])
        self.assertEqual(len(params), 2)

    def test_distance_lt_rejects_invalid(self):
        # distance:<0 or distance:<-5 should fall through (nonsensical bound).
        r = parse_query("distance:<0")
        self.assertEqual(r["filters"], [])
        r = parse_query("distance:<abc")
        self.assertEqual(r["filters"], [])

    def test_distance_gt_rejects_invalid(self):
        r = parse_query("distance:>-5")
        self.assertEqual(r["filters"], [])
        r = parse_query("distance:>abc")
        self.assertEqual(r["filters"], [])

    def test_fts_quote_escapes_quotes(self):
        # A user typing '"foo"bar' shouldn't break FTS5
        self.assertEqual(_fts_quote('foo"bar'), '"foo""bar"')
        self.assertEqual(_fts_quote("hello"), '"hello"')


# ---------------------------------------------------------------------------
# Executor tests
# ---------------------------------------------------------------------------

def _make_test_db():
    """Build a tiny v2.51-schema DB with a handful of aircraft suitable
    for exercising the executor."""
    f = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    f.close()
    conn = sqlite3.connect(f.name)
    conn.executescript("""
        CREATE TABLE seen_aircraft (
            icao TEXT PRIMARY KEY, first_seen_at INTEGER NOT NULL,
            first_callsign TEXT, first_aircraft_type TEXT,
            registration TEXT, last_callsign TEXT, aircraft_type TEXT,
            aircraft_type_desc TEXT, operator TEXT, country TEXT,
            last_lat REAL, last_lon REAL, last_seen_at INTEGER,
            sighting_count INTEGER NOT NULL DEFAULT 0,
            fts_dirty INTEGER NOT NULL DEFAULT 0,
            last_distance REAL,
            registered_owner TEXT, manufacturer TEXT,
            best_track_seconds INTEGER);
        CREATE INDEX idx_seen_country ON seen_aircraft(country);
        CREATE INDEX idx_seen_type ON seen_aircraft(aircraft_type);
        CREATE INDEX idx_seen_callsign ON seen_aircraft(last_callsign);
        CREATE INDEX idx_seen_registration ON seen_aircraft(registration);
        CREATE INDEX idx_seen_last ON seen_aircraft(last_seen_at);
        CREATE INDEX idx_seen_distance ON seen_aircraft(last_distance);
        CREATE TABLE sightings_hourly (
            icao TEXT, hour_bucket INTEGER, callsign TEXT, aircraft_type TEXT,
            type_desc TEXT, sighting_count INTEGER, first_seen_at INTEGER,
            last_seen_at INTEGER, last_lat REAL, last_lon REAL,
            last_altitude REAL, last_speed REAL, last_squawk TEXT,
            min_altitude REAL, max_altitude REAL, max_speed REAL,
            PRIMARY KEY (icao, hour_bucket));
    """)
    conn.execute("""CREATE VIRTUAL TABLE seen_aircraft_fts USING fts5(
        icao, registration, last_callsign, aircraft_type, aircraft_type_desc,
        operator, country, tokenize='unicode61 remove_diacritics 1')""")

    now = int(time.time())
    fixtures = [
        # icao, registration, callsign, type, desc, operator, country, sighting_count, last_seen_offset, best_track_seconds
        # best_track_seconds = all-time longest single track (v3.4.62); distinct
        # per row so sort tests have a deterministic order.
        ("A12345", "N12345", "UAL2024", "B738", "Boeing 737-800", "United Airlines", "United States", 50, -3600, 1800),
        ("A12346", "N98765", "UAL101",  "B738", "Boeing 737-800", "United Airlines", "United States", 30, -7200, 3600),
        ("A22222", "N22222", "DAL550",  "B739", "Boeing 737-900", "Delta Air Lines", "United States", 20, -10800, 600),
        ("C00001", "C-FAAA", "ACA847",  "B738", "Boeing 737-800", "Air Canada",      "Canada",        100, -1800, 7200),
        ("C00002", "C-FBBB", "ACA101",  "A320", "Airbus A320",    "Air Canada",      "Canada",        15, -86400, 300),
        ("400001", "G-AAAA", "BAW100",  "B789", "Boeing 787-9",   "British Airways", "United Kingdom",75, -2400, 5400),
    ]
    for ic, reg, cs, atype, desc, op, country, count, ts_offset, best_track in fixtures:
        conn.execute("""INSERT INTO seen_aircraft (
            icao, first_seen_at, first_callsign, first_aircraft_type,
            registration, last_callsign, aircraft_type, aircraft_type_desc,
            operator, country, last_lat, last_lon, last_seen_at, sighting_count, fts_dirty,
            best_track_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (ic, now + ts_offset - 86400*30, cs, atype, reg, cs, atype, desc, op, country,
         37.5, -122.1, now + ts_offset, count, best_track))
    # Populate FTS
    conn.execute("""INSERT INTO seen_aircraft_fts (rowid, icao, registration, last_callsign,
        aircraft_type, aircraft_type_desc, operator, country)
        SELECT rowid, icao, registration, last_callsign, aircraft_type,
               aircraft_type_desc, operator, country FROM seen_aircraft""")
    conn.commit()
    return conn, f.name


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_test_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_empty_query_returns_browse(self):
        """No filters, no free text → return all aircraft, recent first."""
        r = execute_search(self.conn, parse_query(""), limit=10)
        self.assertEqual(r["total_count"], 6)
        # First result should be most-recently-seen — C00001 at -1800
        self.assertEqual(r["rows"][0]["icao"], "C00001")

    def test_country_filter(self):
        r = execute_search(self.conn, parse_query("Canada"))
        self.assertEqual(r["total_count"], 2)
        countries = {row["country"] for row in r["rows"]}
        self.assertEqual(countries, {"Canada"})

    def test_type_filter(self):
        r = execute_search(self.conn, parse_query("B739"))
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "A22222")

    def test_combined_type_country(self):
        # B738 in Canada → only C00001
        r = execute_search(self.conn, parse_query("B738 Canada"))
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "C00001")

    def test_icao_lookup(self):
        r = execute_search(self.conn, parse_query("A12345"))
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "A12345")

    def test_registration_lookup(self):
        r = execute_search(self.conn, parse_query("N12345"))
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["registration"], "N12345")

    def test_us_tail_matches_callsign_when_registration_empty(self):
        # v2.83.4: US GA aircraft transmit their tail number as the
        # callsign. seen_aircraft.last_callsign='N969TC' but
        # registration may be empty (hexdb hasn't resolved or doesn't
        # have the aircraft). Pre-v2.83.4 this returned 0 results
        # because the parser emitted an exact-match registration filter
        # only. The fix makes the registration token emit ambiguous
        # filters against (registration OR last_callsign), so the
        # aircraft is found via either column.
        self.conn.execute("""INSERT INTO seen_aircraft (
            icao, first_seen_at, registration, last_callsign,
            aircraft_type, aircraft_type_desc, operator, country,
            last_seen_at, sighting_count, fts_dirty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            ('AD7F41', 1000, None, 'N969TC', 'C208',
             'CESSNA 208 Caravan', '', 'United States', 2000, 86))
        self.conn.execute("""INSERT INTO seen_aircraft_fts (
            rowid, icao, registration, last_callsign,
            aircraft_type, aircraft_type_desc, operator, country)
            SELECT rowid, icao, registration, last_callsign,
                   aircraft_type, aircraft_type_desc, operator, country
            FROM seen_aircraft WHERE icao='AD7F41'""")
        self.conn.commit()
        r = execute_search(self.conn, parse_query("N969TC"))
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "AD7F41")

    def test_us_tail_matches_when_only_registration_populated(self):
        # The reverse case: registration='N969TD', last_callsign empty.
        # Should still match — both columns are OR'd.
        self.conn.execute("""INSERT INTO seen_aircraft (
            icao, first_seen_at, registration, last_callsign,
            aircraft_type, aircraft_type_desc, operator, country,
            last_seen_at, sighting_count, fts_dirty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            ('AD7F42', 1000, 'N969TD', None, 'C208',
             'CESSNA 208 Caravan', '', 'United States', 2000, 5))
        self.conn.execute("""INSERT INTO seen_aircraft_fts (
            rowid, icao, registration, last_callsign,
            aircraft_type, aircraft_type_desc, operator, country)
            SELECT rowid, icao, registration, last_callsign,
                   aircraft_type, aircraft_type_desc, operator, country
            FROM seen_aircraft WHERE icao='AD7F42'""")
        self.conn.commit()
        r = execute_search(self.conn, parse_query("N969TD"))
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "AD7F42")

    def test_callsign_prefix_known(self):
        # UAL is a known airline — prefix match returns both UAL aircraft
        r = execute_search(self.conn, parse_query("UAL"))
        self.assertEqual(r["total_count"], 2)
        callsigns = {row["last_callsign"] for row in r["rows"]}
        self.assertEqual(callsigns, {"UAL2024", "UAL101"})

    def test_callsign_exact(self):
        r = execute_search(self.conn, parse_query("UAL2024"))
        self.assertEqual(r["total_count"], 1)

    def test_freetext_operator_match(self):
        r = execute_search(self.conn, parse_query("Delta"))
        # Should match the Delta Air Lines aircraft via FTS5 on operator
        self.assertGreaterEqual(r["total_count"], 1)
        self.assertTrue(any(row["operator"] == "Delta Air Lines" for row in r["rows"]))

    def test_no_match(self):
        r = execute_search(self.conn, parse_query("Madagascar"))  # no Madagascar fixtures
        self.assertEqual(r["total_count"], 0)
        self.assertEqual(r["rows"], [])

    def test_ranking_orders_by_score_desc(self):
        # Without any filter, we order by recency. With B738 filter,
        # results should be score-ordered. C00001 has 100 sightings, A12345 has 50,
        # A12346 has 30. C00001 should rank highest.
        r = execute_search(self.conn, parse_query("B738"))
        self.assertGreaterEqual(r["total_count"], 3)
        # First row should be C00001 (highest sighting_count among B738s,
        # and recent).
        self.assertEqual(r["rows"][0]["icao"], "C00001")

    def test_score_field_present(self):
        r = execute_search(self.conn, parse_query("B738"))
        for row in r["rows"]:
            self.assertIn("score", row)
            self.assertIsInstance(row["score"], (int, float))

    def test_pagination(self):
        page1 = execute_search(self.conn, parse_query(""), limit=2, offset=0)
        page2 = execute_search(self.conn, parse_query(""), limit=2, offset=2)
        # Total should be the same
        self.assertEqual(page1["total_count"], page2["total_count"])
        # Page rows should be different
        self.assertNotEqual(
            [r["icao"] for r in page1["rows"]],
            [r["icao"] for r in page2["rows"]],
        )

    def test_hostile_fts_input_doesnt_crash(self):
        """A user typing FTS5 syntax characters should not break the query."""
        for hostile in ['"', '("foo', 'NEAR(a)', '*', 'AND', '"a""""b']:
            try:
                r = execute_search(self.conn, parse_query(hostile))
                # Either returns a result or an error field, but never raises
                self.assertIn("total_count", r)
            except Exception as e:
                self.fail(f"Hostile input {hostile!r} raised {type(e).__name__}: {e}")

    def test_detail_for_aircraft(self):
        d = detail_for_aircraft(self.conn, "A12345")
        self.assertIsNotNone(d)
        self.assertEqual(d["icao"], "A12345")
        self.assertEqual(d["registration"], "N12345")
        self.assertEqual(d["country"], "United States")
        self.assertEqual(d["sighting_count"], 50)

    def test_detail_for_unknown_icao(self):
        d = detail_for_aircraft(self.conn, "FFFFFF")
        self.assertIsNone(d)

    # v2.56.0: Search must surface last-state fields (speed, altitude,
    # squawk) so the result card can render the same data density as
    # All-tab rows. They live on sightings_hourly; the executor JOINs
    # via a correlated subquery for the latest hour_bucket per icao.
    def test_last_state_fields_present_when_hourly_populated(self):
        """When sightings_hourly has rows, last_speed/altitude/squawk surface."""
        # Insert an hourly bucket for A12345 with known values
        now_bucket = (int(time.time()) // 3600) * 3600
        self.conn.execute("""
            INSERT INTO sightings_hourly (
                icao, hour_bucket, callsign, aircraft_type, type_desc,
                sighting_count, first_seen_at, last_seen_at,
                last_lat, last_lon, last_altitude, last_speed, last_squawk,
                min_altitude, max_altitude, max_speed
            ) VALUES (?, ?, 'UAL2024', 'B738', 'Boeing 737-800',
                10, ?, ?, 37.5, -122.1, 35000, 482, '1234',
                30000, 38000, 510)
        """, ("A12345", now_bucket, now_bucket, now_bucket))
        self.conn.commit()
        r = execute_search(self.conn, parse_query("A12345"))
        self.assertEqual(r["total_count"], 1)
        row = r["rows"][0]
        self.assertEqual(row["last_speed"], 482)
        self.assertEqual(row["last_altitude"], 35000)
        self.assertEqual(row["last_squawk"], "1234")

    def test_last_state_fields_null_when_hourly_empty(self):
        """When sightings_hourly has no rows for an icao, fields are None."""
        # No hourly insert — rely on default empty hourly table from setUp
        r = execute_search(self.conn, parse_query("A12345"))
        self.assertEqual(r["total_count"], 1)
        row = r["rows"][0]
        self.assertIsNone(row["last_speed"])
        self.assertIsNone(row["last_altitude"])
        self.assertIsNone(row["last_squawk"])

    def test_callsign_alias_for_annotate_military(self):
        """v2.56.0 row dict includes 'callsign' alongside 'last_callsign'.

        Server-side _annotate_military reads 'callsign'; the alias lets
        existing helpers work on search rows without rename plumbing.
        """
        r = execute_search(self.conn, parse_query("A12345"))
        row = r["rows"][0]
        self.assertEqual(row["callsign"], row["last_callsign"])
        self.assertEqual(row["callsign"], "UAL2024")

    # v2.57.0: boolean filter parsing
    def test_parse_mil_token(self):
        """'mil' token is parsed as a boolean military filter."""
        p = parse_query("mil")
        self.assertEqual(len(p["filters"]), 1)
        f = p["filters"][0]
        self.assertEqual(f["field"], "military")
        self.assertEqual(f["match"], "boolean")
        self.assertEqual(f["value"], True)
        self.assertEqual(p["free_text"], [])

    def test_parse_military_token(self):
        """'military' (full word) parses the same as 'mil'."""
        p = parse_query("military")
        self.assertEqual(len(p["filters"]), 1)
        self.assertEqual(p["filters"][0]["field"], "military")

    def test_parse_watchlist_token(self):
        """'watchlist' token is parsed as a boolean watchlist filter."""
        p = parse_query("watchlist")
        self.assertEqual(len(p["filters"]), 1)
        f = p["filters"][0]
        self.assertEqual(f["field"], "watchlist")
        self.assertEqual(f["match"], "boolean")
        self.assertEqual(f["value"], True)

    def test_parse_wl_token(self):
        """'wl' is a short alias for 'watchlist'."""
        p = parse_query("wl")
        self.assertEqual(len(p["filters"]), 1)
        self.assertEqual(p["filters"][0]["field"], "watchlist")

    def test_parse_mil_and_watchlist_together(self):
        """'mil watchlist' parses both; AND'd at execute time."""
        p = parse_query("mil watchlist")
        self.assertEqual(len(p["filters"]), 2)
        fields = {f["field"] for f in p["filters"]}
        self.assertEqual(fields, {"military", "watchlist"})

    # v2.57.0: boolean filter executor — military
    def test_execute_military_filter_with_config(self):
        """Military filter matches via icao prefix from config."""
        # ADFDC8 looks military by virtue of being in the 'AD' icao
        # prefix range. Insert a row that matches.
        now = int(time.time())
        self.conn.execute("""INSERT INTO seen_aircraft (
            icao, first_seen_at, first_callsign, first_aircraft_type,
            registration, last_callsign, aircraft_type, aircraft_type_desc,
            operator, country, last_lat, last_lon, last_seen_at, sighting_count, fts_dirty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        ("ADFDC8", now-3600, "TREK01", "C130", "AE-MIL", "TREK01", "C130",
         "Lockheed C-130H Hercules", "USAF", "United States",
         44.1, -89.5, now-1800, 78))
        self.conn.execute("""INSERT INTO seen_aircraft_fts (rowid, icao, registration, last_callsign, aircraft_type, aircraft_type_desc, operator, country) VALUES (
            (SELECT rowid FROM seen_aircraft WHERE icao='ADFDC8'),
            'ADFDC8', 'AE-MIL', 'TREK01', 'C130', 'Lockheed C-130H Hercules', 'USAF', 'United States')""")
        self.conn.commit()

        # Without mil_config: no matches (1=0 fallback)
        r = execute_search(self.conn, parse_query("mil"))
        self.assertEqual(r["total_count"], 0)

        # With config containing the AD prefix: ADFDC8 matches
        mil_config = {"icao_prefixes": ["AD"], "callsign_prefixes": [], "special_aircraft": {}}
        r = execute_search(self.conn, parse_query("mil"), mil_config=mil_config)
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "ADFDC8")

    def test_execute_military_filter_callsign_prefix(self):
        """Military filter matches via callsign prefix from config."""
        # The default A12345 fixture has callsign UAL2024 which doesn't
        # match. C00001 has ACA847. Use a callsign prefix of "ACA" for
        # the test (Air Canada — not really military, but semantically
        # valid for a prefix-match test).
        mil_config = {
            "icao_prefixes": [],
            "callsign_prefixes": ["ACA"],
            "special_aircraft": {},
        }
        r = execute_search(self.conn, parse_query("mil"), mil_config=mil_config)
        # Two C00001 + C00002 callsigns start with ACA in the fixtures
        self.assertEqual(r["total_count"], 2)
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"C00001", "C00002"})

    def test_execute_military_filter_special_aircraft(self):
        """Military filter matches via explicit special_aircraft list."""
        mil_config = {
            "icao_prefixes": [],
            "callsign_prefixes": [],
            "special_aircraft": {"A22222": {"label": "VIP", "color": "#abc"}},
        }
        r = execute_search(self.conn, parse_query("mil"), mil_config=mil_config)
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "A22222")

    def test_execute_military_filter_no_config_returns_empty(self):
        """No mil_config → empty result, NOT all aircraft."""
        r = execute_search(self.conn, parse_query("mil"))
        self.assertEqual(r["total_count"], 0)

    # v2.57.0: boolean filter executor — watchlist
    def test_execute_watchlist_filter_icao_match(self):
        """Watchlist filter matches via exact icao."""
        watchlist = [{"icao": "A12345", "label": "Test 737"}]
        r = execute_search(self.conn, parse_query("watchlist"),
                            watchlist_config=watchlist)
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "A12345")

    def test_execute_watchlist_filter_callsign_prefix(self):
        """Watchlist filter matches via callsign prefix."""
        watchlist = [{"callsign": "UAL", "label": "United"}]
        r = execute_search(self.conn, parse_query("watchlist"),
                            watchlist_config=watchlist)
        # UAL2024 (A12345), UAL101 (A12346) both start with UAL
        self.assertEqual(r["total_count"], 2)
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"A12345", "A12346"})

    def test_execute_watchlist_filter_model_substring(self):
        """Watchlist filter matches via model substring on type/desc."""
        watchlist = [{"model": "737", "label": "Boeing 737s"}]
        r = execute_search(self.conn, parse_query("watchlist"),
                            watchlist_config=watchlist)
        # B738 (Boeing 737-800) and B739 (Boeing 737-900) all match
        # by descriptor substring "737"
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"A12345", "A12346", "A22222", "C00001"})

    def test_execute_watchlist_filter_no_config_returns_empty(self):
        """No watchlist_config → empty result, NOT all aircraft."""
        r = execute_search(self.conn, parse_query("watchlist"))
        self.assertEqual(r["total_count"], 0)

    def test_execute_mil_and_watchlist_together(self):
        """'mil watchlist' AND's the two filters."""
        # Set up: A22222 in the special list AND on the watchlist.
        # No other aircraft should satisfy both.
        mil_config = {
            "icao_prefixes": [], "callsign_prefixes": [],
            "special_aircraft": {"A22222": {"label": "VIP"}, "A12345": {"label": "VIP"}},
        }
        watchlist = [{"icao": "A22222", "label": "Watching VIP"}]
        r = execute_search(self.conn, parse_query("mil watchlist"),
                            mil_config=mil_config, watchlist_config=watchlist)
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "A22222")

    # v2.57.1: tail-only watchlist resolution via resolved_tails map
    def test_execute_watchlist_tail_resolution_match(self):
        """Tail-only entries match when resolved_tails maps them to ICAO."""
        # Watchlist has only a tail; resolved_tails translates it to A12345.
        watchlist = [{"tail": "N12345", "label": "Friend's plane"}]
        resolved = {"N12345": "A12345"}
        r = execute_search(self.conn, parse_query("watchlist"),
                            watchlist_config=watchlist,
                            resolved_tails=resolved)
        self.assertEqual(r["total_count"], 1)
        self.assertEqual(r["rows"][0]["icao"], "A12345")

    def test_execute_watchlist_tail_unresolved_skipped(self):
        """Tail entries whose tail isn't in resolved_tails are silently
        skipped (logged at startup, not per-request)."""
        watchlist = [{"tail": "N99999", "label": "Unknown plane"}]
        # Empty resolved_tails — the tail couldn't be looked up at startup.
        resolved = {}
        r = execute_search(self.conn, parse_query("watchlist"),
                            watchlist_config=watchlist,
                            resolved_tails=resolved)
        # Empty parts → 1=0 fallback in _watchlist_match_clause
        self.assertEqual(r["total_count"], 0)

    def test_execute_watchlist_mixed_icao_and_resolved_tail(self):
        """Direct ICAO entry + tail-resolved entry both contribute to
        the IN-clause and produce two matches."""
        watchlist = [
            {"icao": "A22222", "label": "VIP"},
            {"tail": "N12345", "label": "Friend's plane"},
        ]
        resolved = {"N12345": "A12345"}
        r = execute_search(self.conn, parse_query("watchlist"),
                            watchlist_config=watchlist,
                            resolved_tails=resolved)
        self.assertEqual(r["total_count"], 2)
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"A12345", "A22222"})

    def test_execute_watchlist_tail_without_resolved_map_skipped(self):
        """When execute_search is called without resolved_tails (older
        callers / tests), tail entries are skipped — preserves v2.57.0
        behavior for the no-resolution path."""
        watchlist = [{"tail": "N12345", "label": "Friend's plane"}]
        # resolved_tails not passed at all
        r = execute_search(self.conn, parse_query("watchlist"),
                            watchlist_config=watchlist)
        self.assertEqual(r["total_count"], 0)

    # v2.60.0 (Phase 1A.5): server-side sort allowlist + ORDER BY
    def test_sort_by_icao_asc(self):
        """Sort by icao asc puts hex-numeric values first, then A-prefixed."""
        r = execute_search(self.conn, parse_query(""),
                            order="icao", direction="asc")
        icaos = [row["icao"] for row in r["rows"]]
        # ASCII sort: digits (4) < letters (A,C). 400001 first, then A*, then C*.
        self.assertEqual(icaos[0], "400001")
        self.assertTrue(icaos[1].startswith("A"))
        # Ascending order
        self.assertEqual(icaos, sorted(icaos))

    def test_sort_by_icao_desc(self):
        r = execute_search(self.conn, parse_query(""),
                            order="icao", direction="desc")
        icaos = [row["icao"] for row in r["rows"]]
        self.assertEqual(icaos, sorted(icaos, reverse=True))

    def test_sort_by_callsign_asc(self):
        """Callsigns sort alphabetically. ACA101 first."""
        r = execute_search(self.conn, parse_query(""),
                            order="callsign", direction="asc")
        callsigns = [row["last_callsign"] for row in r["rows"]]
        self.assertEqual(callsigns[0], "ACA101")
        self.assertEqual(callsigns, sorted(callsigns))

    def test_sort_by_country_asc(self):
        r = execute_search(self.conn, parse_query(""),
                            order="country", direction="asc")
        countries = [row["country"] for row in r["rows"]]
        self.assertEqual(countries, sorted(countries))

    def test_sort_by_seen_at_desc(self):
        """Most-recently-seen first when sorted desc by seen_at."""
        r = execute_search(self.conn, parse_query(""),
                            order="seen_at", direction="desc")
        # C00001 has offset -1800, the most recent.
        self.assertEqual(r["rows"][0]["icao"], "C00001")
        # The fixtures' last_seen_at values, sorted descending
        seen_ats = [row["last_seen_at"] for row in r["rows"]]
        self.assertEqual(seen_ats, sorted(seen_ats, reverse=True))

    def test_sort_by_sightings_desc(self):
        """sighting_count column. C00001=100, BAW=75, A12345=50…"""
        r = execute_search(self.conn, parse_query(""),
                            order="sightings", direction="desc")
        self.assertEqual(r["rows"][0]["icao"], "C00001")
        self.assertEqual(r["rows"][0]["sighting_count"], 100)
        # Descending order
        counts = [row["sighting_count"] for row in r["rows"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_sort_invalid_order_falls_back_to_relevance(self):
        """Unknown column name → ignored, falls back to default order."""
        r = execute_search(self.conn, parse_query(""),
                            order="not_a_real_column", direction="asc")
        # Default order for empty query is recency desc (browse mode).
        # First row should be the most recent (C00001 at -1800).
        self.assertEqual(r["rows"][0]["icao"], "C00001")

    def test_sort_invalid_direction_uses_per_column_default(self):
        """Bogus direction → use the per-column default (asc for text)."""
        r = execute_search(self.conn, parse_query(""),
                            order="callsign", direction="sideways")
        callsigns = [row["last_callsign"] for row in r["rows"]]
        # Default for callsign is asc.
        self.assertEqual(callsigns, sorted(callsigns))

    def test_sort_with_filter(self):
        """Sort is independent of filtering — e.g. country filter +
        sort by sightings desc returns Canada aircraft, sightings desc."""
        r = execute_search(self.conn, parse_query("Canada"),
                            order="sightings", direction="desc")
        self.assertEqual(r["total_count"], 2)
        # Both Canada aircraft, sorted desc by count.
        # C00001=100, C00002=15.
        self.assertEqual(r["rows"][0]["icao"], "C00001")
        self.assertEqual(r["rows"][1]["icao"], "C00002")

    def test_sort_no_params_uses_relevance(self):
        """Default behavior unchanged from pre-v2.60: relevance order."""
        r = execute_search(self.conn, parse_query("B738"))
        # Original ranking test — C00001 should still rank highest by score.
        self.assertEqual(r["rows"][0]["icao"], "C00001")

    # v2.60.1 (Phase 1A.5 perf): distance sort now uses the stored
    # seen_aircraft.last_distance column rather than computing per-row
    # haversines + sorting in Python after fetch.
    def test_distance_sort_uses_stored_column_asc(self):
        """Populating last_distance and sorting asc returns rows in
        increasing distance order across the FULL result set, not
        just the current page."""
        # Set distinct last_distance values for the 6 fixture rows.
        # Pick values that don't match any existing ordering (relevance
        # / recency / sighting_count / icao) so the test fails if the
        # sort code falls back to anything other than last_distance.
        distances = {
            "A12345": 50.0,
            "A12346": 10.0,
            "A22222": 200.0,
            "C00001": 80.0,
            "C00002": 5.0,
            "400001": 130.0,
        }
        for icao, km in distances.items():
            self.conn.execute(
                "UPDATE seen_aircraft SET last_distance = ? WHERE icao = ?",
                (km, icao),
            )
        self.conn.commit()
        r = execute_search(self.conn, parse_query(""),
                            order="distance", direction="asc")
        # Result should be C00002, A12346, A12345, C00001, 400001, A22222
        # in increasing distance order.
        ordered = [row["icao"] for row in r["rows"]]
        expected = ["C00002", "A12346", "A12345", "C00001", "400001", "A22222"]
        self.assertEqual(ordered, expected)

    def test_distance_sort_uses_stored_column_desc(self):
        """Same fixture, desc direction → reverse order, NULL last."""
        distances = {
            "A12345": 50.0,
            "A12346": 10.0,
            "A22222": 200.0,
            "C00001": 80.0,
            "C00002": 5.0,
            "400001": 130.0,
        }
        for icao, km in distances.items():
            self.conn.execute(
                "UPDATE seen_aircraft SET last_distance = ? WHERE icao = ?",
                (km, icao),
            )
        self.conn.commit()
        r = execute_search(self.conn, parse_query(""),
                            order="distance", direction="desc")
        ordered = [row["icao"] for row in r["rows"]]
        expected = ["A22222", "400001", "C00001", "A12345", "A12346", "C00002"]
        self.assertEqual(ordered, expected)

    def test_distance_sort_null_rows_appear_last(self):
        """Aircraft with NULL last_distance (no coords or no receiver
        config) sort AFTER all aircraft with known distance, regardless
        of asc/desc direction. Matches user expectation: 'closest
        aircraft' should never include unknowns at the top."""
        # Only set distance on half the rows; leave the others NULL.
        self.conn.execute(
            "UPDATE seen_aircraft SET last_distance = 100.0 WHERE icao = 'C00001'"
        )
        self.conn.execute(
            "UPDATE seen_aircraft SET last_distance = 50.0 WHERE icao = 'A22222'"
        )
        self.conn.commit()
        # Asc: A22222, C00001, then the four NULL rows in whatever
        # relevance/recency tie-break order.
        r_asc = execute_search(self.conn, parse_query(""),
                                order="distance", direction="asc")
        ordered_asc = [row["icao"] for row in r_asc["rows"]]
        self.assertEqual(ordered_asc[0], "A22222")
        self.assertEqual(ordered_asc[1], "C00001")
        # Desc: C00001, A22222, then the four NULL rows.
        r_desc = execute_search(self.conn, parse_query(""),
                                 order="distance", direction="desc")
        ordered_desc = [row["icao"] for row in r_desc["rows"]]
        self.assertEqual(ordered_desc[0], "C00001")
        self.assertEqual(ordered_desc[1], "A22222")
        # Both asc and desc end with rows that have no last_distance.
        for row in r_asc["rows"][2:]:
            self.assertIsNone(row.get("last_distance_km"))
        for row in r_desc["rows"][2:]:
            self.assertIsNone(row.get("last_distance_km"))

    def test_distance_sort_full_result_set_not_just_page(self):
        """Sort by distance with limit=2 — must return the 2 CLOSEST
        aircraft globally, not the 2 closest of an arbitrary page.
        This is the v2.60.1 fix verifying server-side ORDER BY rather
        than the v2.60.0 post-fetch Python sort that only saw the
        current page."""
        distances = {
            "A12345": 50.0,
            "A12346": 10.0,
            "A22222": 200.0,
            "C00001": 80.0,
            "C00002": 5.0,
            "400001": 130.0,
        }
        for icao, km in distances.items():
            self.conn.execute(
                "UPDATE seen_aircraft SET last_distance = ? WHERE icao = ?",
                (km, icao),
            )
        self.conn.commit()
        r = execute_search(self.conn, parse_query(""), limit=2,
                            order="distance", direction="asc")
        # The 2 closest are C00002 (5 km) and A12346 (10 km).
        # Pre-v2.60.1 (Python post-sort on a 2-row page) would have
        # returned 2 random rows in distance order, not necessarily
        # the globally-closest two.
        self.assertEqual(len(r["rows"]), 2)
        self.assertEqual(r["rows"][0]["icao"], "C00002")
        self.assertEqual(r["rows"][1]["icao"], "A12346")
        # And total_count should still report all matching rows
        # (NOT just what's on this page).
        self.assertEqual(r["total_count"], 6)

    # v2.81.0: track-length sort. Computed on-the-fly from
    # (seen_aircraft.last_seen_at - seen_aircraft.first_seen_at) — no
    # stored column, no schema migration. Existing fixtures all have
    # identical 30-day track length (last_seen_at offset varies but
    # first_seen_at is always last_seen_at - 30d), so most ordering
    # tests insert a controlled row to break the tie deterministically.
    def test_sort_by_track_length_recognized(self):
        """track_length is in the SORTABLE_COLUMNS allowlist — query
        runs without falling through to relevance order."""
        r = execute_search(self.conn, parse_query(""),
                            order="track_length", direction="desc")
        # All 6 fixtures should be present (sort doesn't filter).
        self.assertEqual(r["total_count"], 6)

    def test_sort_by_track_length_desc_default(self):
        """Default direction for track_length is DESC (longest first).
        Insert a row with a very long single track (best_track_seconds)
        and confirm it sorts to the top when no explicit direction is
        passed. Also confirms the value is served as track_length_sec."""
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO seen_aircraft ("
            "icao, first_seen_at, first_callsign, first_aircraft_type, "
            "registration, last_callsign, aircraft_type, aircraft_type_desc, "
            "operator, country, last_lat, last_lon, last_seen_at, sighting_count, "
            "fts_dirty, best_track_seconds"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            ("L00001", now - 365 * 86400, "LONG1", "B777", "N-LONG", "LONG1",
             "B777", "Boeing 777", "TestOp", "TestCountry",
             37.5, -122.1, now, 999, 36000),  # 10h — longest in the set
        )
        self.conn.commit()
        # direction=None → uses _SORT_DEFAULT_DIR['track_length'] = 'desc'
        r = execute_search(self.conn, parse_query(""),
                            order="track_length", direction=None)
        self.assertEqual(r["rows"][0]["icao"], "L00001")
        self.assertEqual(r["rows"][0]["track_length_sec"], 36000)

    def test_sort_by_track_length_asc_short_first(self):
        """ASC puts shortest tracks first. Useful for spotting
        one-sighting aircraft / brief flyovers."""
        now = int(time.time())
        # 60-second longest track — a typical "passed through the
        # coverage area once" aircraft; smaller than every base fixture.
        self.conn.execute(
            "INSERT INTO seen_aircraft ("
            "icao, first_seen_at, first_callsign, first_aircraft_type, "
            "registration, last_callsign, aircraft_type, aircraft_type_desc, "
            "operator, country, last_lat, last_lon, last_seen_at, sighting_count, "
            "fts_dirty, best_track_seconds"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            ("S00001", now - 60, "SHORT1", "C172", "N-SHRT", "SHORT1",
             "C172", "Cessna 172", "TestOp", "TestCountry",
             37.5, -122.1, now, 2, 60),
        )
        self.conn.commit()
        r = execute_search(self.conn, parse_query(""),
                            order="track_length", direction="asc")
        self.assertEqual(r["rows"][0]["icao"], "S00001")

    def test_sort_by_track_length_null_sorts_last(self):
        """When best_track_seconds is NULL (no session ever tracked —
        a legacy row that predates the v11 backfill, or an aircraft
        with no aircraft_track_daily rows), the row sorts LAST in both
        directions via the `<col> IS NULL` ORDER BY prefix, matching
        every other column's NULL-last behavior."""
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO seen_aircraft ("
            "icao, first_seen_at, first_callsign, first_aircraft_type, "
            "registration, last_callsign, aircraft_type, aircraft_type_desc, "
            "operator, country, last_lat, last_lon, last_seen_at, sighting_count, "
            "fts_dirty, best_track_seconds"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)",
            ("N00001", now - 86400, "NULL1", "C172", "N-NULL", "NULL1",
             "C172", "Cessna 172", "TestOp", "TestCountry",
             37.5, -122.1, now, 1),
        )
        self.conn.commit()
        # DESC: NULL row at the end. SQLite's default for DESC already
        # sorts NULL last, so the IS-NULL guard is defensive for this
        # direction — but exercising it confirms the guard is in effect.
        r_desc = execute_search(self.conn, parse_query(""),
                                 order="track_length", direction="desc")
        self.assertEqual(r_desc["rows"][-1]["icao"], "N00001")
        # ASC: NULL would default to FIRST (the wrong UX — "shortest
        # tracks" shouldn't lead with rows that have no track at all).
        # The IS-NULL guard inverts the default and keeps NULL last.
        r_asc = execute_search(self.conn, parse_query(""),
                                order="track_length", direction="asc")
        self.assertEqual(r_asc["rows"][-1]["icao"], "N00001")

    def test_sort_by_track_length_value_and_order(self):
        """track_length_sec is served straight from best_track_seconds,
        and DESC orders the full browse set by it. C00001 (7200s) is
        the longest base fixture, C00002 (300s) the shortest."""
        r = execute_search(self.conn, parse_query(""),
                           order="track_length", direction="desc")
        self.assertEqual(r["rows"][0]["icao"], "C00001")
        self.assertEqual(r["rows"][0]["track_length_sec"], 7200)
        vals = [row["track_length_sec"] for row in r["rows"]]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_sort_by_track_length_with_filter(self):
        """Sort applies within the filtered subset. Canada filter
        narrows to C00001 (7200s) and C00002 (300s); DESC orders the
        longer track first and total_count is still correct."""
        r = execute_search(self.conn, parse_query("Canada"),
                            order="track_length", direction="desc")
        self.assertEqual(r["total_count"], 2)
        self.assertEqual([row["icao"] for row in r["rows"]], ["C00001", "C00002"])

    # v2.62.0 (Phase 1E): URL-supplied date-range filtering.
    def test_from_ts_filters_to_window(self):
        """from_ts alone: only aircraft seen at-or-after that timestamp.
        Fixture has 6 rows with offsets -3600, -7200, -10800, -1800,
        -86400, -2400 from now. from_ts = now - 5400 should match the
        rows with offsets -3600, -1800, -2400 (4 rows total: A12345,
        C00001, 400001, and the 0 baseline... wait, just 3 rows because
        no row sits at -5400 boundary)."""
        import time as _t
        now = int(_t.time())
        # Only rows where last_seen_at >= now - 5400.
        # Offsets in fixture: -3600, -7200, -10800, -1800, -86400, -2400
        # Pass: -3600, -1800, -2400 → 3 rows.
        r = execute_search(self.conn, parse_query(""),
                            from_ts=now - 5400)
        self.assertEqual(r["total_count"], 3)
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"A12345", "C00001", "400001"})

    def test_to_ts_filters_to_window(self):
        """to_ts alone: only aircraft seen STRICTLY BEFORE that timestamp.
        Half-open interval semantics. to_ts = now - 5400 should match
        rows with offsets -7200, -10800, -86400 → 3 rows."""
        import time as _t
        now = int(_t.time())
        r = execute_search(self.conn, parse_query(""),
                            to_ts=now - 5400)
        self.assertEqual(r["total_count"], 3)
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"A12346", "A22222", "C00002"})

    def test_from_and_to_ts_intersection(self):
        """Both bounds: seen between [from_ts, to_ts). Window
        [now - 8000, now - 2500) should match rows with offsets
        -7200, -3600 → 2 rows."""
        import time as _t
        now = int(_t.time())
        r = execute_search(self.conn, parse_query(""),
                            from_ts=now - 8000, to_ts=now - 2500)
        self.assertEqual(r["total_count"], 2)
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"A12346", "A12345"})

    def test_url_date_range_overrides_parser_time_range(self):
        """Phase 1E core decision: when from_ts/to_ts are provided,
        any time_range from the parser is ignored. Parser sees `2025`
        token → time_range = (2025-01-01 00:00, 2026-01-01 00:00).
        URL passes from_ts/to_ts that DON'T overlap with 2025 (a
        recent window). Without override, intersection would be 0
        rows (parser range is in the past); with override, the URL
        window applies and we should see the 3 recent rows."""
        import time as _t
        now = int(_t.time())
        parsed = parse_query("2025")
        # Verify parser actually extracted a time_range (sanity check
        # — if this fails the test premise is broken).
        self.assertIsNotNone(parsed["time_range"])
        # URL window: last 5400 seconds. Override parser's 2025 range.
        r = execute_search(self.conn, parsed,
                            from_ts=now - 5400)
        # If override worked, we get 3 rows (matching the URL window).
        # If override failed and parser range stayed in effect, the
        # 2025 window wouldn't include any row (fixture timestamps are
        # in 2026), so we'd get 0.
        self.assertEqual(r["total_count"], 3)
        icaos = {row["icao"] for row in r["rows"]}
        self.assertEqual(icaos, {"A12345", "C00001", "400001"})

    def test_no_url_dates_preserves_parser_time_range(self):
        """The override only happens when from_ts/to_ts are explicitly
        passed. Without them, parser-extracted time_range applies as
        before — preserves backward compat with v2.51 typed-date filtering."""
        # Fixture timestamps are in 2026. Parsing "2025" gives us a
        # year-long range in 2025, which doesn't overlap with the
        # fixture data — so total_count should be 0.
        r = execute_search(self.conn, parse_query("2025"))
        self.assertEqual(r["total_count"], 0)


# v2.53.0: detail page data + chip derivation
class TestDetailPageData(unittest.TestCase):
    """Tests for detail_page_data_for_aircraft.

    The detail page returns a 'mode' (full vs sparse) based on the
    LOW_SIGHTING_THRESHOLD constant. Sparse mode skips analytics. Full
    mode derives chips from deterministic rules. These tests cover
    both modes plus the threshold edge case."""

    def setUp(self):
        import tempfile
        import sqlite3 as _sq
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        # Build a real schema via init_db + migrations
        from collector import init_db
        init_db(self.tmp.name)
        self.conn = _sq.connect(self.tmp.name)
        # Apply the FULL migration chain (not just v1) so the test schema
        # matches a real install — otherwise columns added by later
        # migrations (e.g. v10's registered_owner/manufacturer, which
        # detail_for_aircraft selects) are missing and the detail tests fail.
        from schema_migrations import apply_schema_migrations
        apply_schema_migrations(self.conn, "test")

    def tearDown(self):
        self.conn.close()
        import os; os.unlink(self.tmp.name)

    def _seed_aircraft(self, icao, sighting_count, callsign="UAL2024"):
        """Insert one seen_aircraft row + N hourly buckets to back it.
        Hour buckets are spread across recent days starting from now."""
        import time
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO seen_aircraft (icao, first_seen_at, first_callsign, "
            "first_aircraft_type, last_callsign, aircraft_type, country, "
            "sighting_count, fts_dirty, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (icao, now - sighting_count * 3600, callsign, "B738",
             callsign, "B738", "United States", sighting_count, now)
        )
        # One bucket per sighting, spread over ~sighting_count hours
        for i in range(sighting_count):
            hb = now - i * 3600
            hb = (hb // 3600) * 3600
            self.conn.execute(
                "INSERT OR IGNORE INTO sightings_hourly "
                "(icao, hour_bucket, callsign, aircraft_type, sighting_count, "
                " first_seen_at, last_seen_at, max_altitude, max_speed) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (icao, hb, callsign, "B738", hb, hb + 1800, 36000, 450)
            )
        # Also insert into all_sightings so recent_sightings query works
        for i in range(min(sighting_count, 20)):
            ts = now - i * 60
            self.conn.execute(
                "INSERT INTO all_sightings (icao, callsign, lat, lon, altitude, speed, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (icao, callsign, 40.7, -74.0, 36000, 450, ts)
            )
        self.conn.commit()

    def test_returns_none_for_unknown_icao(self):
        from search import detail_page_data_for_aircraft
        result = detail_page_data_for_aircraft(self.conn, "ZZZZZZ")
        self.assertIsNone(result)

    def test_sparse_mode_below_threshold(self):
        from search import detail_page_data_for_aircraft, LOW_SIGHTING_THRESHOLD
        self._seed_aircraft("AAAAAA", LOW_SIGHTING_THRESHOLD - 1)
        result = detail_page_data_for_aircraft(self.conn, "AAAAAA")
        self.assertEqual(result["mode"], "sparse")
        self.assertEqual(result["chips"], [])
        self.assertIsNone(result["hour_of_day"])
        self.assertIsNone(result["day_of_week"])
        self.assertIsNone(result["ranges"])

    def test_full_mode_at_or_above_threshold(self):
        from search import detail_page_data_for_aircraft, LOW_SIGHTING_THRESHOLD
        self._seed_aircraft("BBBBBB", LOW_SIGHTING_THRESHOLD * 5)
        result = detail_page_data_for_aircraft(self.conn, "BBBBBB")
        self.assertEqual(result["mode"], "full")
        self.assertIsNotNone(result["hour_of_day"])
        self.assertIsNotNone(result["day_of_week"])
        self.assertIsNotNone(result["ranges"])
        self.assertEqual(len(result["hour_of_day"]), 24)
        self.assertEqual(len(result["day_of_week"]), 7)

    def test_threshold_boundary(self):
        """Aircraft with exactly LOW_SIGHTING_THRESHOLD sightings should
        be in full mode (>= comparison, not >)."""
        from search import detail_page_data_for_aircraft, LOW_SIGHTING_THRESHOLD
        self._seed_aircraft("CCCCCC", LOW_SIGHTING_THRESHOLD)
        result = detail_page_data_for_aircraft(self.conn, "CCCCCC")
        self.assertEqual(result["mode"], "full")

    def test_recent_sightings_capped_at_20(self):
        from search import detail_page_data_for_aircraft
        self._seed_aircraft("DDDDDD", 50)
        result = detail_page_data_for_aircraft(self.conn, "DDDDDD")
        self.assertLessEqual(len(result["recent_sightings"]), 20)

    def test_recent_sightings_sorted_desc(self):
        """Most-recent sighting should be first."""
        from search import detail_page_data_for_aircraft
        self._seed_aircraft("EEEEEE", 30)
        result = detail_page_data_for_aircraft(self.conn, "EEEEEE")
        seen_ats = [s["seen_at"] for s in result["recent_sightings"]]
        self.assertEqual(seen_ats, sorted(seen_ats, reverse=True))

    def test_active_chip_always_emitted_in_full_mode(self):
        """The 'Active: N of M days' chip should always emit when mode
        is full and we have first_seen + last_seen timestamps."""
        from search import detail_page_data_for_aircraft
        self._seed_aircraft("FFFFFF", 100)
        result = detail_page_data_for_aircraft(self.conn, "FFFFFF")
        active_chips = [c for c in result["chips"] if c["label"] == "Active"]
        self.assertEqual(len(active_chips), 1)
        # Value should match "N of M days" pattern
        self.assertIn("days", active_chips[0]["value"])

    def test_icao_is_canonicalized_uppercase(self):
        """Lowercase ICAO query should still find the uppercase row."""
        from search import detail_page_data_for_aircraft
        self._seed_aircraft("ABCDEF", 20)
        result = detail_page_data_for_aircraft(self.conn, "abcdef")
        self.assertIsNotNone(result)
        self.assertEqual(result["icao"], "ABCDEF")

    def test_mode_field_exposed(self):
        """The 'mode' field is what the frontend keys off of for adaptive
        rendering — it must be present."""
        from search import detail_page_data_for_aircraft
        self._seed_aircraft("121212", 5)
        result = detail_page_data_for_aircraft(self.conn, "121212")
        self.assertIn("mode", result)
        self.assertIn(result["mode"], ("full", "sparse"))

    def test_low_sighting_threshold_exposed(self):
        """Frontend uses this to render the 'needs N+ sightings' message
        in sparse mode — must be present in the response."""
        from search import detail_page_data_for_aircraft
        self._seed_aircraft("131313", 5)
        result = detail_page_data_for_aircraft(self.conn, "131313")
        self.assertIn("low_sighting_threshold", result)
        self.assertIsInstance(result["low_sighting_threshold"], int)


class TestPeakToday(unittest.TestCase):
    """v2.82.0: integration tests for peak_today resolver + executor.

    Uses a custom DB fixture with synthetic all_sightings rows arranged
    so that exactly one minute today has the highest distinct-ICAO
    count, plus a few "noise" minutes around it. That lets us assert
    the resolver picks the right bucket and the executor returns only
    the aircraft seen in that bucket.
    """

    def setUp(self):
        self.f = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.f.close()
        self.conn = sqlite3.connect(self.f.name)
        self.conn.row_factory = sqlite3.Row
        # Schema: seen_aircraft + FTS + all_sightings + sightings_hourly
        self.conn.executescript("""
            CREATE TABLE seen_aircraft (
                icao TEXT PRIMARY KEY, first_seen_at INTEGER NOT NULL,
                first_callsign TEXT, first_aircraft_type TEXT,
                registration TEXT, last_callsign TEXT, aircraft_type TEXT,
                aircraft_type_desc TEXT, operator TEXT, country TEXT,
                last_lat REAL, last_lon REAL, last_seen_at INTEGER,
                sighting_count INTEGER NOT NULL DEFAULT 0,
                fts_dirty INTEGER NOT NULL DEFAULT 0,
                last_distance REAL,
                best_track_seconds INTEGER);
            CREATE INDEX idx_seen_country ON seen_aircraft(country);
            CREATE INDEX idx_seen_type ON seen_aircraft(aircraft_type);
            CREATE INDEX idx_seen_last ON seen_aircraft(last_seen_at);
            CREATE TABLE all_sightings (
                icao TEXT NOT NULL, callsign TEXT, aircraft_type TEXT,
                seen_at INTEGER NOT NULL, lat REAL, lon REAL,
                altitude REAL, speed REAL, squawk TEXT);
            CREATE INDEX idx_all_seen_icao ON all_sightings(seen_at, icao);
            CREATE TABLE sightings_hourly (
                icao TEXT, hour_bucket INTEGER, callsign TEXT,
                aircraft_type TEXT, type_desc TEXT, sighting_count INTEGER,
                first_seen_at INTEGER, last_seen_at INTEGER,
                last_lat REAL, last_lon REAL, last_altitude REAL,
                last_speed REAL, last_squawk TEXT,
                min_altitude REAL, max_altitude REAL, max_speed REAL,
                PRIMARY KEY (icao, hour_bucket));
        """)
        self.conn.execute("""CREATE VIRTUAL TABLE seen_aircraft_fts USING fts5(
            icao, registration, last_callsign, aircraft_type,
            aircraft_type_desc, operator, country,
            tokenize='unicode61 remove_diacritics 1')""")
        # Anchor "today" to the start of UTC day for the current real
        # time — so default tz_offset_sec=0 in parse_query produces the
        # same window we seed below.
        now = int(time.time())
        self.today_start = (now // 86400) * 86400
        self.now = now
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.f.name)

    def _seed_aircraft(self, icaos):
        """Create seen_aircraft rows for the given ICAOs (so the IN-clause
        in the executor has joinable rows)."""
        for ic in icaos:
            self.conn.execute("""INSERT INTO seen_aircraft (
                icao, first_seen_at, last_callsign, aircraft_type,
                aircraft_type_desc, operator, country, last_seen_at,
                sighting_count, fts_dirty
            ) VALUES (?, ?, 'CS', 'B738', 'Boeing', 'Op', 'US', ?, 1, 0)""",
            (ic, self.today_start, self.today_start + 60))
        self.conn.execute("""INSERT INTO seen_aircraft_fts (
            rowid, icao, registration, last_callsign, aircraft_type,
            aircraft_type_desc, operator, country)
            SELECT rowid, icao, registration, last_callsign, aircraft_type,
                   aircraft_type_desc, operator, country FROM seen_aircraft""")
        self.conn.commit()

    def _seed_sighting(self, icao, minute_offset):
        """Insert one sighting at today_start + minute_offset*60 seconds."""
        ts = self.today_start + minute_offset * 60
        self.conn.execute(
            "INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
            (icao, ts),
        )

    def test_resolver_finds_peak_bucket_and_icaos(self):
        # Layout:
        #   minute  60: 2 distinct ICAOs (A,B)
        #   minute  61: 1 distinct ICAO  (A)
        #   minute 120: 4 distinct ICAOs (C,D,E,F)  ← peak
        #   minute 121: 2 distinct ICAOs (C,D)
        from search import _resolve_peak_today_if_present
        self._seed_aircraft(["A00001", "A00002", "C00001", "C00002",
                             "C00003", "C00004"])
        for ic in ["A00001", "A00002"]: self._seed_sighting(ic, 60)
        self._seed_sighting("A00001", 61)
        for ic in ["C00001", "C00002", "C00003", "C00004"]:
            self._seed_sighting(ic, 120)
        for ic in ["C00001", "C00002"]: self._seed_sighting(ic, 121)
        self.conn.commit()
        parsed = parse_query("peak_today")
        _resolve_peak_today_if_present(self.conn, parsed)
        f = parsed["filters"][0]
        self.assertEqual(f["peak_count"], 4)
        self.assertEqual(f["peak_at_ts"], self.today_start + 120 * 60)
        self.assertEqual(set(f["peak_icaos"]),
                         {"C00001", "C00002", "C00003", "C00004"})

    def test_resolver_empty_db_yields_empty_result(self):
        # No sightings today → peak_icaos empty, peak_at_ts None,
        # _build_where emits 1=0 (verified by integration test below).
        from search import _resolve_peak_today_if_present
        parsed = parse_query("peak_today")
        _resolve_peak_today_if_present(self.conn, parsed)
        f = parsed["filters"][0]
        self.assertEqual(f["peak_icaos"], [])
        self.assertIsNone(f["peak_at_ts"])
        self.assertEqual(f["peak_count"], 0)

    def test_resolver_tied_buckets_picks_earliest(self):
        # Two buckets with the same max count — earliest wins.
        from search import _resolve_peak_today_if_present
        self._seed_aircraft(["A00001", "A00002", "A00003", "A00004"])
        # minute 60: 2 distinct (A,B); minute 200: 2 distinct (C,D)
        for ic in ["A00001", "A00002"]: self._seed_sighting(ic, 60)
        for ic in ["A00003", "A00004"]: self._seed_sighting(ic, 200)
        self.conn.commit()
        parsed = parse_query("peak_today")
        _resolve_peak_today_if_present(self.conn, parsed)
        f = parsed["filters"][0]
        self.assertEqual(f["peak_count"], 2)
        self.assertEqual(f["peak_at_ts"], self.today_start + 60 * 60,
                         "Should pick minute 60 (earliest), not minute 200")
        self.assertEqual(set(f["peak_icaos"]), {"A00001", "A00002"})

    def test_executor_returns_only_peak_icaos(self):
        # Full execute_search round-trip: only the peak-bucket aircraft
        # come back, even though seen_aircraft has more rows.
        self._seed_aircraft(["A00001", "A00002", "C00001", "C00002",
                             "C00003"])
        for ic in ["A00001", "A00002"]: self._seed_sighting(ic, 60)
        for ic in ["C00001", "C00002", "C00003"]: self._seed_sighting(ic, 120)
        self.conn.commit()
        r = execute_search(self.conn, parse_query("peak_today"))
        self.assertEqual(r["total_count"], 3)
        self.assertEqual(set(row["icao"] for row in r["rows"]),
                         {"C00001", "C00002", "C00003"})

    def test_executor_composes_peak_today_with_country(self):
        # Set up: peak bucket has 4 ICAOs (2 US, 2 Canada).
        # `peak_today Canada` should return only the 2 Canadians.
        self.conn.execute("""INSERT INTO seen_aircraft (
            icao, first_seen_at, last_callsign, aircraft_type,
            aircraft_type_desc, operator, country, last_seen_at,
            sighting_count, fts_dirty
        ) VALUES
            ('US0001', ?, 'UAL1', 'B738', 'Boeing', 'United', 'United States', ?, 1, 0),
            ('US0002', ?, 'UAL2', 'B738', 'Boeing', 'United', 'United States', ?, 1, 0),
            ('CA0001', ?, 'ACA1', 'B738', 'Boeing', 'Air Canada', 'Canada', ?, 1, 0),
            ('CA0002', ?, 'ACA2', 'B738', 'Boeing', 'Air Canada', 'Canada', ?, 1, 0)
        """, (self.today_start, self.today_start + 60) * 4)
        self.conn.execute("""INSERT INTO seen_aircraft_fts (
            rowid, icao, registration, last_callsign, aircraft_type,
            aircraft_type_desc, operator, country)
            SELECT rowid, icao, registration, last_callsign, aircraft_type,
                   aircraft_type_desc, operator, country FROM seen_aircraft""")
        # All four sighted at minute 120 → peak bucket
        for ic in ["US0001", "US0002", "CA0001", "CA0002"]:
            self._seed_sighting(ic, 120)
        self.conn.commit()
        r = execute_search(self.conn, parse_query("peak_today Canada"))
        self.assertEqual(r["total_count"], 2)
        self.assertEqual(set(row["icao"] for row in r["rows"]),
                         {"CA0001", "CA0002"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
