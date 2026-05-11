"""
Tests for categorize.classify().

Pure function tests. No DB, no imports beyond categorize itself.

Run:
    python3 test_categorize.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from categorize import (
    classify, HELICOPTER_TYPES, COMMERCIAL_PREFIXES, COMMERCIAL_EXACT
)


class TestClassify(unittest.TestCase):

    # --- Military precedence ---------------------------------------------

    def test_military_overrides_commercial_type(self):
        """A B738 marked military classifies as military, not commercial."""
        self.assertEqual(classify("B738", "Boeing 737-800", True), "military")

    def test_military_overrides_helicopter_type(self):
        """An H60 marked military classifies as military, not helicopter."""
        self.assertEqual(classify("H60", "Black Hawk", True), "military")

    def test_military_with_no_type(self):
        """Military flag with empty type code still classifies as military."""
        self.assertEqual(classify("", "", True), "military")
        self.assertEqual(classify(None, None, True), "military")

    # --- Helicopter ------------------------------------------------------

    def test_helicopter_type_codes(self):
        """Each entry in HELICOPTER_TYPES classifies as helicopter."""
        for t in HELICOPTER_TYPES:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "helicopter")

    def test_helicopter_type_lowercase(self):
        """Type codes are upper-cased before matching."""
        self.assertEqual(classify("h60", "", False), "helicopter")
        self.assertEqual(classify("ec45", "", False), "helicopter")

    def test_helicopter_via_description(self):
        """Type code unrecognized but description says 'helicopter'."""
        self.assertEqual(
            classify("XYZ", "Unknown helicopter type", False),
            "helicopter",
        )

    def test_helicopter_description_case_insensitive(self):
        """Description match is case-insensitive."""
        self.assertEqual(classify("XYZ", "HELICOPTER", False), "helicopter")
        self.assertEqual(classify("XYZ", "Helicopter", False), "helicopter")

    # --- Commercial ------------------------------------------------------

    def test_commercial_prefix_match(self):
        """Each prefix should classify as commercial when applied."""
        cases = ["A320", "A321", "A330", "A380",  # A3*
                 "A220",                            # A2* via exact
                 "B737", "B738", "B739", "B777",   # B7*
                 "B36X",                            # B3* (Beech Bonanza? -- actually GA, but matches prefix)
                 "CRJ7", "CRJ9",                   # CRJ
                 "E170", "E175", "E190",           # E1*
                 "E75L", "E75S"]                   # E7*
        # B36X is a known false-positive — Beech 36 is GA but matches B3*.
        # Documenting that here for transparency. The heuristic was lifted
        # verbatim from v2.85.9; if the false-positive ever matters, both
        # this test and the categorize module update together.
        for t in cases:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "commercial")

    def test_commercial_exact_set(self):
        """Each entry in COMMERCIAL_EXACT classifies as commercial."""
        for t in COMMERCIAL_EXACT:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "commercial")

    def test_commercial_lowercase(self):
        """Commercial classification respects upper-casing of input."""
        self.assertEqual(classify("b738", "", False), "commercial")
        self.assertEqual(classify("a320", "", False), "commercial")

    # --- General Aviation -----------------------------------------------

    def test_general_aviation_typical_codes(self):
        """Cessna, Piper, Cirrus, etc. all classify as GA."""
        ga_types = ["C172", "C182", "PA28", "PA34", "SR22", "DA40", "M20P"]
        for t in ga_types:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "general_aviation")

    def test_general_aviation_unrecognized_type(self):
        """A type code that doesn't match any other rule defaults to GA."""
        self.assertEqual(classify("XXXX", "", False), "general_aviation")

    # --- Unknown --------------------------------------------------------

    def test_unknown_empty_type(self):
        """Empty type code with no other signal classifies as unknown."""
        self.assertEqual(classify("", "", False), "unknown")
        self.assertEqual(classify(None, None, False), "unknown")
        self.assertEqual(classify("   ", "", False), "unknown")  # whitespace-only

    def test_unknown_does_not_become_helicopter_via_random_desc(self):
        """An empty type with a non-'helicopter' description stays unknown."""
        self.assertEqual(classify("", "Some plane", False), "unknown")

    # --- Edge cases -----------------------------------------------------

    def test_helicopter_overrides_commercial_when_type_matches_both(self):
        """Helicopter precedence is higher than commercial. (None of our
        helicopter types currently match commercial prefixes, but the
        classification order should still be checked.)"""
        # Hypothetical: a type code that matches both. None exists today,
        # but verify the precedence by adding A109 (in HELI set) — it
        # doesn't match commercial prefixes either, so this is more about
        # documenting the intended precedence than exercising overlap.
        self.assertEqual(classify("A109", "", False), "helicopter")

    def test_whitespace_in_type_stripped(self):
        """Leading/trailing whitespace on type code doesn't break classification."""
        self.assertEqual(classify(" B738 ", "", False), "commercial")
        self.assertEqual(classify("\tH60\n", "", False), "helicopter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
