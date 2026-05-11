"""
Tests for v2.91.0 search tokens: category filters (commercial /
general_aviation / helicopter / unknown, with ga / heli aliases) and
the last:Nd time-window token.

Two layers:
  1. parse_query — verify each token produces the expected filter or
     time_range output, including aliases, multi-category OR merging,
     and tz-aware day boundaries.
  2. _filter_clause — verify the new "in" match type emits the expected
     SQL pattern with parameter list.

Run:
    python3 test_search_v2_91_tokens.py
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from search import parse_query, _filter_clause


# ============================================================================
# Layer 1: parse_query produces correct filter/time_range output
# ============================================================================

class TestCategoryTokens(unittest.TestCase):
    """Each category keyword should produce a {field: 'category', match:
    'in', value: [...]} filter. Aliases (ga, heli) map to the canonical
    category name."""

    def _category_values(self, query):
        """Return sorted list of category values from the parsed query.
        Filters with field='category' should have exactly one entry after
        the post-pass (multiple tokens merge), value being a list."""
        out = parse_query(query)
        cats = [f for f in out["filters"]
                 if f["field"] == "category" and f["match"] == "in"]
        if not cats:
            return None
        self.assertEqual(len(cats), 1,
                          "Multiple category filters should merge to one")
        return sorted(cats[0]["value"])

    def test_commercial(self):
        self.assertEqual(self._category_values("commercial"), ["commercial"])

    def test_helicopter(self):
        self.assertEqual(self._category_values("helicopter"), ["helicopter"])

    def test_general_aviation(self):
        self.assertEqual(
            self._category_values("general_aviation"),
            ["general_aviation"],
        )

    def test_unknown(self):
        self.assertEqual(self._category_values("unknown"), ["unknown"])

    def test_alias_ga(self):
        """ga should resolve to general_aviation."""
        self.assertEqual(
            self._category_values("ga"),
            ["general_aviation"],
        )

    def test_alias_heli(self):
        """heli should resolve to helicopter."""
        self.assertEqual(self._category_values("heli"), ["helicopter"])

    def test_case_insensitive(self):
        """Tokens are uppercased/lowercased uniformly."""
        self.assertEqual(self._category_values("COMMERCIAL"), ["commercial"])
        self.assertEqual(self._category_values("Helicopter"), ["helicopter"])
        self.assertEqual(self._category_values("HELI"), ["helicopter"])

    def test_two_categories_merge(self):
        """commercial helicopter → single filter with both values, OR semantics."""
        self.assertEqual(
            self._category_values("commercial helicopter"),
            ["commercial", "helicopter"],
        )

    def test_three_categories_merge(self):
        self.assertEqual(
            self._category_values("commercial general_aviation helicopter"),
            ["commercial", "general_aviation", "helicopter"],
        )

    def test_alias_and_canonical_dedupe(self):
        """ga and general_aviation in the same query → single value."""
        self.assertEqual(
            self._category_values("ga general_aviation"),
            ["general_aviation"],
        )

    def test_does_not_match_substring(self):
        """'commercials' (with trailing s) should NOT match — falls to free text."""
        out = parse_query("commercials")
        cats = [f for f in out["filters"] if f["field"] == "category"]
        self.assertEqual(cats, [])
        self.assertIn("commercials", out["free_text"])

    def test_military_unaffected(self):
        """The category token list intentionally excludes military — that
        token stays on the existing live-config filter, not the category
        column. military typed alone produces a boolean filter, not a
        category filter."""
        out = parse_query("military")
        cats = [f for f in out["filters"] if f["field"] == "category"]
        self.assertEqual(cats, [], "military must not become a category filter")
        mils = [f for f in out["filters"] if f["field"] == "military"]
        self.assertEqual(len(mils), 1)
        self.assertEqual(mils[0]["match"], "boolean")


class TestLastNdToken(unittest.TestCase):
    """last:Nd produces a time_range from local-midnight (N-1) days ago
    to now. Calendar-day semantics, not rolling hours."""

    def test_last_1d_includes_today(self):
        """last:1d should produce [today_midnight, tomorrow_midnight) —
        the same calendar-day shape as the `today` token. Using
        tomorrow-midnight as end_ts (rather than `now`) avoids edge-of-
        second exclusion when the SQL clause uses strict `<`."""
        out = parse_query("last:1d")
        self.assertIsNotNone(out["time_range"])
        start_ts, end_ts = out["time_range"]
        now = int(time.time())
        today_start = (now // 86400) * 86400
        self.assertEqual(start_ts, today_start)
        self.assertEqual(end_ts, today_start + 86400)

    def test_last_7d_spans_seven_days(self):
        """last:7d window: [today - 6*86400, tomorrow_midnight). Total
        span is exactly 7 calendar days."""
        out = parse_query("last:7d")
        self.assertIsNotNone(out["time_range"])
        start_ts, end_ts = out["time_range"]
        now = int(time.time())
        today_start = (now // 86400) * 86400
        expected_start = today_start - 6 * 86400
        expected_end = today_start + 86400
        self.assertEqual(start_ts, expected_start)
        self.assertEqual(end_ts, expected_end)
        self.assertEqual(end_ts - start_ts, 7 * 86400)

    def test_last_30d(self):
        out = parse_query("last:30d")
        start_ts, end_ts = out["time_range"]
        now = int(time.time())
        today_start = (now // 86400) * 86400
        self.assertEqual(start_ts, today_start - 29 * 86400)
        self.assertEqual(end_ts, today_start + 86400)
        self.assertEqual(end_ts - start_ts, 30 * 86400)

    def test_invalid_n_zero_falls_to_freetext(self):
        """last:0d is not meaningful; falls through to free-text."""
        out = parse_query("last:0d")
        self.assertIsNone(out["time_range"])
        self.assertIn("last:0d", out["free_text"])

    def test_invalid_n_negative_falls_to_freetext(self):
        out = parse_query("last:-3d")
        self.assertIsNone(out["time_range"])
        self.assertIn("last:-3d", out["free_text"])

    def test_invalid_n_non_numeric_falls_to_freetext(self):
        out = parse_query("last:foo")
        self.assertIsNone(out["time_range"])
        self.assertIn("last:foo", out["free_text"])

    def test_missing_d_suffix_falls_to_freetext(self):
        """last:7 (no d) is not the syntax — falls to free text."""
        out = parse_query("last:7")
        self.assertIsNone(out["time_range"])
        self.assertIn("last:7", out["free_text"])

    def test_combines_with_category(self):
        """last:7d helicopter → time_range filter + category filter."""
        out = parse_query("last:7d helicopter")
        self.assertIsNotNone(out["time_range"])
        cats = [f for f in out["filters"] if f["field"] == "category"]
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]["value"], ["helicopter"])

    def test_narrower_today_wins_over_wider_last_7d(self):
        """today + last:7d → narrower today wins per v2.65.0 narrower-wins rule."""
        out = parse_query("last:7d today")
        start_ts, end_ts = out["time_range"]
        # today's window is 86400s wide; last:7d's is ~7×86400. Narrower wins.
        self.assertLessEqual(end_ts - start_ts, 86400 + 5)

    def test_tz_offset_applied(self):
        """tz_offset_sec shifts the local-midnight boundary correctly.
        For UTC-5 (e.g. -18000), local midnight is 5 hours later than UTC
        midnight, so start_ts should reflect that."""
        out = parse_query("last:7d", tz_offset_sec=-18000)
        start_ts, end_ts = out["time_range"]
        now = int(time.time())
        today_local_start = ((now + (-18000)) // 86400) * 86400 - (-18000)
        expected_start = today_local_start - 6 * 86400
        self.assertEqual(start_ts, expected_start)


# ============================================================================
# Layer 2: _filter_clause emits correct SQL for "in" match type
# ============================================================================

class TestFilterClauseIn(unittest.TestCase):

    def test_in_single_value(self):
        f = {"field": "category", "match": "in", "value": ["commercial"]}
        clause, params = _filter_clause(f)
        self.assertEqual(clause, "seen_aircraft.category IN (?)")
        self.assertEqual(params, ["commercial"])

    def test_in_multiple_values(self):
        f = {"field": "category", "match": "in",
             "value": ["commercial", "helicopter"]}
        clause, params = _filter_clause(f)
        self.assertEqual(clause, "seen_aircraft.category IN (?,?)")
        self.assertEqual(params, ["commercial", "helicopter"])

    def test_in_three_values(self):
        f = {"field": "category", "match": "in",
             "value": ["commercial", "general_aviation", "helicopter"]}
        clause, params = _filter_clause(f)
        self.assertEqual(clause, "seen_aircraft.category IN (?,?,?)")
        self.assertEqual(params, ["commercial", "general_aviation", "helicopter"])

    def test_in_empty_value_returns_no_match(self):
        """Defensive: empty value list emits 1=0 rather than the SQL
        syntax error 'IN ()' would produce."""
        f = {"field": "category", "match": "in", "value": []}
        clause, params = _filter_clause(f)
        self.assertEqual(clause, "1=0")
        self.assertEqual(params, [])

    def test_existing_match_types_still_work(self):
        """Sanity: adding 'in' didn't break exact / prefix."""
        c, p = _filter_clause({"field": "country", "match": "exact", "value": "US"})
        self.assertEqual(c, "seen_aircraft.country = ?")
        self.assertEqual(p, ["US"])

        c, p = _filter_clause({"field": "icao", "match": "prefix", "value": "AE"})
        self.assertEqual(c, "seen_aircraft.icao LIKE ?")
        self.assertEqual(p, ["AE%"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
