"""The maps' airport layer (v3.4.182): scripts/build_airports.py trims
OurAirports' CSVs into static/airports.json; airports.near() serves the ones
near the receiver; /api/airports and /api/ui-config's map.airports carry them
to the Live map and the display board. Airport codes and positions in the
fixtures are invented.
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import airports
import collector
import server
from config_validator import validate_config
from schema_migrations import apply_schema_migrations

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub

REPO = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("build_airports", REPO / "scripts" / "build_airports.py")
build_airports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_airports)

AIRPORTS_CSV = """"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft","continent","iso_country","iso_region","municipality","scheduled_service","icao_code","iata_code","gps_code","local_code","home_link","wikipedia_link","keywords"
1,"ZZA1","large_airport","Example Major",40.000001,-90.000001,600,"NA","US","US-XX","Exampleton","yes","ZZA1","ZZA",,"ZZA",,,
2,"ZZB2","medium_airport","Example Regional",40.5,-90.5,700,"NA","US","US-XX","Sampleville","yes",,"ZZB","ZZB2","ZZB",,,
3,"ZZC3","small_airport","Example Paved Field",40.1,-90.1,650,"NA","US","US-XX","Demo","no",,,,"ZC3",,,
4,"ZZD4","small_airport","Example Grass Strip",40.2,-90.2,650,"NA","US","US-XX","Demo","no",,,"ZZD4",,,,
5,"ZZE5","heliport","Example Heliport",40.3,-90.3,650,"NA","US","US-XX","Demo","no",,,"ZZE5",,,,
6,"US-0001","small_airport","Example No Code",40.4,-90.4,650,"NA","US","US-XX","Demo","no",,,,,,,
7,"ZZF6","closed","Example Closed",40.6,-90.6,650,"NA","US","US-XX","Demo","no",,,"ZZF6",,,,
8,"X-ZZGG","small_airport","Example Placeholder",40.7,-90.7,650,"NA","XX","XX-XX","Demo","no",,,,,,,
"""
RUNWAYS_CSV = """"id","airport_ref","airport_ident","length_ft","width_ft","surface","lighted","closed","le_ident","le_latitude_deg","le_longitude_deg","le_elevation_ft","le_heading_degT","le_displaced_threshold_ft","he_ident","he_latitude_deg","he_longitude_deg","he_elevation_ft","he_heading_degT","he_displaced_threshold_ft"
10,1,"ZZA1",9000,150,"CON",1,0,"18",40.0123456,-90.0,,,,"36",39.9876543,-90.0,,,
11,1,"ZZA1",8000,150,"ASP",1,1,"9",40.0,-90.02,,,,"27",40.0,-89.98,,,
12,2,"ZZB2",6000,100,"ASPH",1,0,"13",40.51,-90.51,,,,"31",40.49,-90.49,,,
13,3,"ZZC3",3200,60,"ASP",0,0,"1",40.105,-90.1,,,,"19",40.095,-90.1,,,
14,4,"ZZD4",2500,60,"TURF",0,0,"8",,,,,,"26",,,,,
15,6,"US-0001",4000,60,"ASP",0,0,"5",,,,,,"23",,,,,
16,8,"X-ZZGG",4000,60,"ASP",0,0,"5",,,,,,"23",,,,,
"""


def _rows():
    return build_airports.build(AIRPORTS_CSV, RUNWAYS_CSV)


class TestBuild(unittest.TestCase):
    """scripts/build_airports.py: what the map's list keeps and drops."""

    def test_keeps_major_regional_and_paved_small_fields_only(self):
        codes = {r[0]: r[1] for r in _rows()}
        self.assertEqual(codes, {"ZZA1": "l", "ZZB2": "m", "ZC3": "s"},
                         "grass strip, heliport, made-up idents and closed airport are left out")

    def test_the_code_is_icao_then_gps_then_local(self):
        codes = [r[0] for r in _rows()]
        self.assertIn("ZZA1", codes)     # icao_code
        self.assertIn("ZZB2", codes)     # no icao_code: gps_code
        self.assertIn("ZC3", codes)      # neither: local_code

    def test_runways_are_open_ones_with_both_ends_rounded(self):
        rows = {r[0]: r for r in _rows()}
        self.assertEqual(rows["ZZA1"][4], [[40.01235, -90.0, 39.98765, -90.0, "18", "36"]],
                         "the closed runway is dropped; 5 decimals; both ends' names")
        self.assertEqual(rows["ZZA1"][2:4], [40.0, -90.0])

    def test_rows_carry_field_elevation_name_iata_and_city(self):
        rows = {r[0]: r for r in _rows()}
        self.assertEqual(rows["ZZA1"][5:], [600, "Example Major", "ZZA", "Exampleton"])
        self.assertEqual(rows["ZZB2"][5:], [700, "Example Regional", "ZZB", "Sampleville"])
        self.assertEqual(rows["ZC3"][5:], [650, "Example Paved Field", "", "Demo"], "no IATA code: empty")

    def test_near_passes_iata_and_city_on(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        build_airports.write(_rows(), tmp / "airports.json", built="2026-01-01")
        airports._cache.update(key=None, airports=None)
        self.addCleanup(airports._cache.update, key=None, airports=None)
        a = {x["code"]: x for x in airports.near(40.0, -90.0, path=tmp / "airports.json")}
        self.assertEqual((a["ZZA1"]["iata"], a["ZZA1"]["city"]), ("ZZA", "Exampleton"))
        self.assertEqual(a["ZC3"]["iata"], "")

    def test_a_trailing_airport_is_dropped_from_the_name(self):
        self.assertEqual(build_airports._name("Example  Regional Airport"), "Example Regional")
        self.assertEqual(build_airports._name("Airport"), "Airport")
        self.assertEqual(build_airports._name("Example Airport Road Field"), "Example Airport Road Field")

    def test_output_is_one_airport_per_line_and_reads_back(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        out = tmp / "airports.json"
        build_airports.write(_rows(), out, built="2026-01-01")
        text = out.read_text()
        self.assertEqual(text.count("\n"), 3 + 2, "head line, three airports, closing line")
        data = json.loads(text)
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["built"], "2026-01-01")
        self.assertIn("public domain", data["source"])


class TestShippedFile(unittest.TestCase):
    """static/airports.json, as it ships."""

    def test_every_row_is_well_formed(self):
        data = json.loads(airports.DATA.read_text(encoding="utf-8"))
        rows = data["airports"]
        self.assertEqual(data["count"], len(rows))
        self.assertGreater(len(rows), 5000)
        for code, cat, lat, lon, rw, elev, name, iata, city in rows:
            self.assertRegex(code, r"^[A-Z0-9]{2,7}$")
            self.assertRegex(iata, r"^([A-Z0-9]{3})?$", code)
            self.assertIsInstance(city, str, code)
            self.assertIn(cat, "lms")
            self.assertTrue(-90 <= lat <= 90 and -180 <= lon <= 180, code)
            self.assertTrue(elev is None or isinstance(elev, int), code)
            self.assertIsInstance(name, str, code)
            for r in rw:
                self.assertEqual(len(r), 6, code)
                self.assertTrue(all(isinstance(v, (int, float)) for v in r[:4]), code)
                self.assertTrue(all(isinstance(v, str) for v in r[4:]), code)
        self.assertEqual(len({(r[0], r[2], r[3]) for r in rows}), len(rows), "no duplicate rows")


class TestNear(unittest.TestCase):
    """airports.near(): only the airports within range, nearest first, cached per position."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.path = tmp / "airports.json"
        build_airports.write(_rows(), self.path, built="2026-01-01")
        airports._cache.update(key=None, airports=None)
        self.addCleanup(airports._cache.update, key=None, airports=None)

    def test_within_radius_nearest_first_with_category_names(self):
        found = airports.near(40.0, -90.0, radius_mi=30, path=self.path)
        self.assertEqual([a["code"] for a in found], ["ZZA1", "ZC3"], "ZZB2 is ~43 mi away")
        self.assertEqual(found[0]["cat"], "major")
        self.assertEqual(found[1]["cat"], "small")
        self.assertEqual(found[0]["runways"], [[40.01235, -90.0, 39.98765, -90.0, "18", "36"]])
        self.assertEqual((found[0]["elev"], found[0]["name"]), (600, "Example Major"))
        self.assertEqual([a["code"] for a in airports.near(40.0, -90.0, radius_mi=60, path=self.path)],
                         ["ZZA1", "ZC3", "ZZB2"])

    def test_reads_the_file_once_per_position(self):
        real = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=real) as rd:
            airports.near(40.0, -90.0, path=self.path)
            airports.near(40.0, -90.0, path=self.path)
            self.assertEqual(rd.call_count, 1)
            airports.near(41.0, -90.0, path=self.path)
            self.assertEqual(rd.call_count, 2, "a moved receiver reads it again")

    def test_a_missing_file_raises(self):
        with self.assertRaises(OSError):
            airports.near(40.0, -90.0, path=self.path.with_name("absent.json"))


class TestEndpoints(unittest.TestCase):
    """/api/airports and /api/ui-config's map.airports."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.data = self.dir / "airports.json"
        build_airports.write(_rows(), self.data, built="2026-01-01")
        airports._cache.update(key=None, airports=None)
        self.addCleanup(airports._cache.update, key=None, airports=None)
        p = mock.patch.object(airports, "DATA", self.data)
        p.start()
        self.addCleanup(p.stop)

    def app(self, receiver=(40.0, -90.0), map_cfg=None):
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        cfg["receiver"]["latitude"], cfg["receiver"]["longitude"] = receiver
        if map_cfg is not None:
            cfg["map"] = map_cfg
        cfg_path = self.dir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        app = server.get_app(cfg, str(cfg_path))
        return lambda path: next(r.endpoint for r in app.routes if getattr(r, "path", "") == path)()

    def test_airports_near_the_receiver(self):
        out = self.app()("/api/airports")
        self.assertTrue(out["ok"])
        self.assertEqual([a["code"] for a in out["airports"]], ["ZZA1", "ZC3", "ZZB2"])
        self.assertEqual(out["radius_mi"], airports.RADIUS_MI)

    def test_no_receiver_location_means_no_airports(self):
        out = self.app(receiver=(None, None))("/api/airports")
        self.assertEqual(out, {"ok": True, "airports": [], "reason": "no_receiver_location"})

    def test_an_unreadable_list_is_reported(self):
        self.data.write_text("{not json")
        with self.assertLogs("adsb.server", level="WARNING"):
            out = self.app()("/api/airports")
        self.assertEqual(out, {"ok": False, "airports": [], "reason": "data_unreadable"})

    def test_ui_config_default_is_major_and_regional(self):
        self.assertEqual(self.app(map_cfg={})("/api/ui-config")["map"]["airports"], "major")
        for v in ("off", "major", "all"):
            self.assertEqual(self.app(map_cfg={"airports": v})("/api/ui-config")["map"]["airports"], v)
        self.assertEqual(self.app(map_cfg={"airports": "lots"})("/api/ui-config")["map"]["airports"], "major")

    def test_the_example_config_says_major(self):
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.assertEqual(cfg["map"]["airports"], "major")


class TestValidator(unittest.TestCase):

    def errors(self, value):
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["map"]["airports"] = value
        return [e for e in validate_config(cfg) if e[0] == "map.airports"]

    def test_values(self):
        for ok in ("off", "major", "all"):
            self.assertEqual(self.errors(ok), [], ok)
        self.assertTrue(self.errors("some"))
        self.assertTrue(self.errors(True))


class TestBundledFonts(unittest.TestCase):
    """v3.4.188: Outfit and JetBrains Mono ship in static/fonts/ with their licences."""

    def test_every_font_fonts_css_names_is_there_with_its_licence(self):
        d = REPO / "static" / "fonts"
        css = (d / "fonts.css").read_text()
        files = re.findall(r"url\(([^)]+\.woff2)\)", css)
        self.assertEqual(len(set(files)), 4)
        for f in set(files):
            self.assertGreater((d / f).stat().st_size, 5000, f)
            self.assertEqual((d / f).read_bytes()[:4], b"wOF2", f)
        self.assertEqual(sorted(re.findall(r"font-family: '([^']+)'", css)), ["JetBrains Mono"] * 2 + ["Outfit"] * 2)
        for lic in ("OFL-Outfit.txt", "OFL-JetBrainsMono.txt"):
            self.assertIn("SIL Open Font License", (d / lic).read_text())


class TestPagesVersionStaticUrls(unittest.TestCase):
    """v3.4.186: every /static/ URL in every served page carries ?v=<version>, so an update can't
    leave a browser on a cached script. airports.js was missed by the old per-file list, and after
    v3.4.185 the Live map's airport codes didn't take clicks until a hard refresh."""

    SKIP = {"/openapi.json", "/docs/{filename}"}   # the API description, and the docs files

    def test_every_static_url_is_versioned(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        db = str(d / "history.db")
        collector.init_db(db)
        conn = sqlite3.connect(db)
        apply_schema_migrations(conn, "test")
        conn.close()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = db
        cfg["watchlist"] = []
        (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        app = server.get_app(cfg, str(d / "config.yaml"))
        version = (REPO / "VERSION").read_text().strip()
        seen = {}
        for r in app.routes:
            path = getattr(r, "path", "")
            if path.startswith(("/api/", "/static")) or path in self.SKIP or "GET" not in (getattr(r, "methods", None) or ()):
                continue
            args = {"/aircraft/{icao}": {"icao": "A00001"}, "/airport/{code}": {"code": "ZZA1"}}.get(path, {})
            out = r.endpoint(**args)
            if getattr(out, "media_type", None) != "text/html":
                continue
            html = out.body.decode()
            urls = re.findall(r'(?:src|href)="(/static/[^"]*)"', html)
            seen[path] = urls
            # v3.4.188: the fonts ship with Aerodrome; no page asks Google for them.
            self.assertNotIn("fonts.googleapis.com", html, path)
            self.assertNotIn("fonts.gstatic.com", html, path)
            self.assertIn("/static/fonts/fonts.css?v=" + version, urls, f"{path} doesn't load the fonts")
            for u in urls:
                self.assertTrue(u.endswith("?v=" + version), f"{path}: {u} has no ?v={version}")
        self.assertIn("/static/airports.js?v=" + version, seen["/"])
        self.assertIn("/static/airports.js?v=" + version, seen["/board"])
        self.assertIn("/static/leaflet/leaflet.js?v=" + version, seen["/aircraft/{icao}"])
        self.assertIn("/static/theme.css?v=" + version, seen["/notification-test-ok"])
        self.assertIn("/static/theme.js?v=" + version, seen["/airport/{code}"])
        self.assertGreaterEqual(len(seen), 12)


if __name__ == "__main__":
    unittest.main()
