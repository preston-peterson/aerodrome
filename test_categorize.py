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
        cases = ["B737", "B738", "B739", "B777",   # B7*
                 "CRJ7", "CRJ9",                   # CRJ
                 "E170", "E175", "E190",           # E1*
                 "E75L", "E75S"]                   # E7*
        for t in cases:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "commercial")

    def test_airliner_designators(self):
        """Airliner families matched by exact designator (v3.4.141): the
        A220 is BCS1/BCS3, the 737 MAX is B3xM, the E2 jets are E29x."""
        for t in ["A320", "A20N", "A21N", "A359", "A35K", "A388", "BCS1",
                  "BCS3", "B37M", "B38M", "B39M", "E295", "MD11"]:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "commercial")

    def test_regional_airliners_are_commercial(self):
        """v3.4.142: types built as airliners, whatever their size."""
        for t in ["AT76", "DH8D", "SF34", "SB20", "D328", "B462", "RJ85",
                  "F100", "JS41", "B190", "L410"]:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "commercial")

    def test_utility_types_stay_general_aviation(self):
        for t in ["C208", "DHC6"]:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "general_aviation")

    def test_prefix_false_positives_are_general_aviation(self):
        """v3.4.141: the dropped A2/A3/B3 prefixes also matched the King
        Air 350, the turbine Bonanza and light aircraft."""
        for t in ["B350", "B36T", "B360", "A210", "A33", "A35"]:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "general_aviation")

    def test_common_helicopters_from_doc8643(self):
        """Helicopters the original 18-code list missed."""
        for t in ["R66", "S76", "B505", "EN48", "EC30", "AS65", "UH60"]:
            with self.subTest(type=t):
                self.assertEqual(classify(t, "", False), "helicopter")

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
