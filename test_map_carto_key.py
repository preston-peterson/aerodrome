"""map.carto_api_key validation (CARTO dark-basemap key)."""
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

from config_validator import validate_config


def _base():
    path = Path(__file__).parent / "config.yaml.example"
    with path.open() as handle:
        return yaml.safe_load(handle)


def _map_errs(cfg):
    return [item for item in validate_config(cfg) if item[0] == "map.carto_api_key"]


class TestMapCartoApiKey(unittest.TestCase):
    def test_example_config_is_valid(self):
        self.assertEqual(validate_config(_base()), [])

    def test_blank_and_missing_are_ok(self):
        cfg = _base()
        cfg["map"]["carto_api_key"] = ""
        self.assertEqual(_map_errs(cfg), [])
        cfg["map"]["carto_api_key"] = "   "
        self.assertEqual(_map_errs(cfg), [])
        del cfg["map"]["carto_api_key"]
        self.assertEqual(_map_errs(cfg), [])

    def test_plain_key_is_ok(self):
        cfg = _base()
        cfg["map"]["carto_api_key"] = "ak_test_fixture_only_not_real"
        self.assertEqual(_map_errs(cfg), [])

    def test_spaces_rejected(self):
        cfg = _base()
        cfg["map"]["carto_api_key"] = "ak test"
        errs = _map_errs(cfg)
        self.assertTrue(errs)
        self.assertIn("spaces", errs[0][1])

    def test_url_junk_rejected(self):
        cfg = _base()
        cfg["map"]["carto_api_key"] = "ak?foo=1"
        self.assertTrue(_map_errs(cfg))

    def test_non_string_rejected(self):
        cfg = _base()
        cfg["map"]["carto_api_key"] = 123
        errs = _map_errs(cfg)
        self.assertTrue(errs)
        self.assertIn("string", errs[0][1])

    def test_too_long_rejected(self):
        cfg = _base()
        cfg["map"]["carto_api_key"] = "a" * 257
        self.assertTrue(_map_errs(cfg))

    def test_other_map_keys_untouched(self):
        cfg = deepcopy(_base())
        cfg["map"]["carto_api_key"] = "ak_test_fixture_only_not_real"
        self.assertEqual(validate_config(cfg), [])


if __name__ == "__main__":
    unittest.main()
