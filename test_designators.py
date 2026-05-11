"""Tests for designators.operator_from_callsign and fts_operator_string.

These small pure-function helpers are the single source of truth for how
operator codes get derived from callsigns. The collector and the v2
migration both call operator_from_callsign — if these helpers behave
inconsistently, the operator column ends up with mismatched values
across "seen since v2.50.42" and "backfilled from existing data".
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from designators import operator_from_callsign, fts_operator_string, AIRLINES


class TestOperatorFromCallsign(unittest.TestCase):

    def test_standard_airline_callsigns(self):
        # United, Delta, Southwest — common US carriers, definitely in AIRLINES
        self.assertEqual(operator_from_callsign("UAL2024"), "UAL")
        self.assertEqual(operator_from_callsign("DAL415"), "DAL")
        self.assertEqual(operator_from_callsign("SWA8001"), "SWA")

    def test_lowercase_callsigns(self):
        # The collector's _cs.strip() doesn't uppercase — make sure we do
        self.assertEqual(operator_from_callsign("ual2024"), "UAL")
        self.assertEqual(operator_from_callsign("Dal415"), "DAL")

    def test_whitespace_padding(self):
        # ADS-B feeds sometimes pad with spaces
        self.assertEqual(operator_from_callsign("  UAL2024  "), "UAL")

    def test_tail_number_callsigns_return_none(self):
        # General aviation callsigns aren't airline operators
        self.assertIsNone(operator_from_callsign("N12345"))
        self.assertIsNone(operator_from_callsign("G-ABCD"))
        self.assertIsNone(operator_from_callsign("VH-XYZ"))

    def test_empty_callsign_returns_none(self):
        self.assertIsNone(operator_from_callsign(""))
        self.assertIsNone(operator_from_callsign(None))

    def test_too_short_returns_none(self):
        self.assertIsNone(operator_from_callsign("AB"))
        self.assertIsNone(operator_from_callsign("A"))

    def test_unknown_airline_code_returns_none(self):
        # ZZZ shouldn't be a real ICAO airline. If it ever becomes one,
        # this test will start failing — that's fine, just pick a different
        # placeholder.
        self.assertNotIn("ZZZ", AIRLINES, "ZZZ is now a known airline; pick a different placeholder")
        self.assertIsNone(operator_from_callsign("ZZZ1234"))

    def test_non_letter_prefix_returns_none(self):
        # Something like "123ABC" — first 3 chars aren't letters
        self.assertIsNone(operator_from_callsign("123ABC"))
        # Pure numeric
        self.assertIsNone(operator_from_callsign("12345"))


class TestFtsOperatorString(unittest.TestCase):

    def test_known_code_returns_code_and_name(self):
        # Result should contain both the code and the full airline name
        # so FTS5 tokenizes both
        s = fts_operator_string("UAL")
        self.assertIn("UAL", s)
        self.assertIn("United", s)  # at minimum, the airline name token

    def test_unknown_code_returns_just_the_code(self):
        # Defensive: if somehow operator column has an unknown code,
        # we still get the code into FTS rather than blanking it
        result = fts_operator_string("ZZZ")
        self.assertEqual(result, "ZZZ")

    def test_none_returns_empty_string(self):
        # Important: returning empty (not "None") so FTS doesn't tokenize
        # the literal word "None"
        self.assertEqual(fts_operator_string(None), "")
        self.assertEqual(fts_operator_string(""), "")

    def test_known_codes_get_enriched(self):
        # Spot-check several common airlines — they should all enrich
        for code in ["DAL", "AAL", "SWA", "JBU"]:
            if code in AIRLINES:
                s = fts_operator_string(code)
                self.assertIn(code, s)
                self.assertGreater(len(s), len(code),
                    f"{code} should enrich to '{code} <name>'")


if __name__ == "__main__":
    unittest.main(verbosity=2)
