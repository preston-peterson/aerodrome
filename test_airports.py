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


# For the airport page's details (static/airport-info.json): links on ZZA1 (one bad on ZZB2),
# a non-US regional field whose local code must not become an FAA code, a helipad, mixed and
# unknown surfaces, and frequencies with a truncated one, a repeat type and an unknown type.
INFO_AIRPORTS_CSV = (AIRPORTS_CSV
    .replace('"ZZA1","ZZA",,"ZZA",,,', '"ZZA1","ZZA",,"ZZA","https://example.com/","https://en.wikipedia.org/wiki/Example",')
    .replace('"ZZB2","ZZB",,,', '"ZZB2","ZZB","javascript:alert(1)","https://example.com/wiki/Example",')
    + '9,"ZZH8","medium_airport","Example Abroad",41.0,10.0,100,"EU","XX","XX-XX","Elsewhere","yes","ZZH8",,"ZZH8","H8X",,,\n')
INFO_RUNWAYS_CSV = RUNWAYS_CSV + (
    '17,2,"ZZB2",7000,120,"ASPH-TURF-G",0,0,"4",,,,,,"22",,,,,\n'
    '18,2,"ZZB2",,,"",0,0,"H1",,,,,,,,,,,\n'
    '19,2,"ZZB2",2000,40,"CRUSHED ROCK",0,0,"17",,,,,,"35",,,,,\n'
    '20,9,"ZZH8",9000,150,"UNK",1,0,"09",,,,,,"27",,,,,\n')
FREQS_CSV = """"id","airport_ref","airport_ident","type","description","frequency_mhz"
1,1,"ZZA1","TWR","TWR",120.75
2,1,"ZZA1","TWR","TWR",118.3
3,1,"ZZA1","GND","GND",121.9
4,1,"ZZA1","ATIS","ATIS",127.65
5,1,"ZZA1","APP","APP",28.465
6,1,"ZZA1","UNIC","UNICOM",122.95
7,1,"ZZA1","XYZ","XYZ",123.45
8,1,"ZZA1","MISC","MISC",130
9,2,"ZZB2","CTAF","CTAF",122.8
10,9,"ZZH8","TWR","TWR",118.1
"""
DTPP_XML = """<?xml version="1.0"?><digital_tpp cycle="2611"><state_code ID="XX"><city_name ID="DEMO">
<airport_name ID="EXAMPLE MAJOR" apt_ident="ZZA" icao_ident=""><record><chart_code>IAP</chart_code></record></airport_name>
<airport_name ID="EXAMPLE NOTHING" apt_ident="ZZQ" icao_ident="KZZQ"></airport_name>
</city_name></state_code></digital_tpp>"""


def _info():
    faa = build_airports.faa_idents(DTPP_XML)
    return {r[0]: r for r in build_airports.build_info(INFO_AIRPORTS_CSV, INFO_RUNWAYS_CSV, FREQS_CSV, faa)}


class TestBuildInfo(unittest.TestCase):
    """scripts/build_airports.py: what static/airport-info.json says about each airport."""

    def test_the_same_airports_by_the_same_codes(self):
        self.assertEqual(set(_info()) - {"ZZH8"}, {r[0] for r in _rows()})

    def test_runways_radio_links_and_plates(self):
        self.assertEqual(_info()["ZZA1"], [
            "ZZA1", "ZZA", 1, "https://example.com/", "https://en.wikipedia.org/wiki/Example",
            [["18/36", 9000, 150, "Concrete", 1]],              # the closed 9/27 is left out
            [["ATIS", "127.650"], ["Ground", "121.900"], ["Tower", "118.300, 120.750"], ["Unicom", "122.950"],
             ["Other", "130.000"], ["XYZ", "123.450"]]])        # 28.465 isn't in the airband: dropped

    def test_surfaces_in_plain_words_longest_runway_first_no_helipads(self):
        self.assertEqual(_info()["ZZB2"][5], [["4/22", 7000, 120, "Asphalt / grass", 0],
                                              ["13/31", 6000, 100, "Asphalt", 1],
                                              ["17/35", 2000, 40, "Crushed rock", 0]])

    def test_only_web_links_and_only_wikipedia_for_wikipedia(self):
        self.assertEqual(_info()["ZZB2"][3:5], ["", ""])

    def test_faa_codes_only_for_us_airports(self):
        info = _info()
        self.assertEqual((info["ZZB2"][1], info["ZZB2"][2]), ("ZZB", 0), "US, but no plates")
        self.assertEqual(info["ZC3"][1], "", "its code already is its FAA code")
        self.assertEqual(info["ZZH8"][1:3], ["", 0], "abroad: H8X is not an FAA code")
        self.assertEqual(info["ZZH8"][5], [["09/27", 9000, 150, "", 1]], "UNK is no surface")

    def test_the_faa_index_counts_airports_with_a_plate(self):
        self.assertEqual(build_airports.faa_idents(DTPP_XML), {"ZZA"})

    def test_written_one_airport_per_line_and_reads_back(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        rows = list(_info().values())
        build_airports.write_info(rows, tmp / "airport-info.json", built="2026-01-01", cycle="2611")
        text = (tmp / "airport-info.json").read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertEqual((data["count"], data["airports"]), (len(rows), rows))
        self.assertIn("public domain", data["source"])
        self.assertIn("2611", data["source"])
        self.assertEqual(len(text.splitlines()), len(rows) + 2)


class TestAiracCycle(unittest.TestCase):
    """The FAA's 28-day cycle, in the builder (Python) and in the airport page's FAA link (JavaScript)."""

    CASES = [("2026-11-17T12:00", "2611"), ("2026-01-22T09:00", "2513"), ("2026-01-22T09:02", "2601"),
             ("2026-12-24T10:00", "2613"), ("2027-01-21T10:00", "2701"), ("2020-12-31T10:00", "2014"),
             ("2021-01-28T10:00", "2101"), ("2025-06-01T00:00", "2505")]

    def test_python(self):
        from datetime import datetime, timezone
        for when, cycle in self.CASES:
            at = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
            self.assertEqual(build_airports.airac_cycle(at), cycle, when)

    @unittest.skipUnless(shutil.which("node") or shutil.which("nodejs"), "needs node")
    def test_the_page_agrees(self):
        import subprocess
        js = (REPO / "static" / "airport-links.js").read_text(encoding="utf-8")
        script = "const window = {};\n" + js + "\nfor (const w of " + json.dumps([w for w, _ in self.CASES]) + \
            ") console.log(window.AerodromeAirportLinks.cycle(Date.parse(w + ':00Z')));"
        out = subprocess.run([shutil.which("node") or shutil.which("nodejs"), "-e", script],
                             capture_output=True, text=True, timeout=30, check=True).stdout.split()
        self.assertEqual(out, [c for _, c in self.CASES])


class TestLinks(unittest.TestCase):
    """static/airport-links.js: the links both the airport page and the Live map's panel show."""

    @unittest.skipUnless(shutil.which("node") or shutil.which("nodejs"), "needs node")
    def test_links(self):
        import subprocess
        js = (REPO / "static" / "airport-links.js").read_text(encoding="utf-8")
        now = "Date.parse('2026-11-17T12:00:00Z')"
        script = "const window = {};\n" + js + "\nconst L = window.AerodromeAirportLinks;\nconsole.log(JSON.stringify(["
        script += "L.links('ZZA1', {ident: 'ZZA', faa_plates: true, wikipedia: 'https://en.wikipedia.org/wiki/Example', website: 'https://example.com/'}, " + now + "),"
        script += "L.links('ZZH8', {ident: 'ZZH8', faa_plates: false, wikipedia: '', website: ''}, " + now + "),"
        script += "L.links('ZZQ1', null, " + now + ")]));"
        out = subprocess.run([shutil.which("node") or shutil.which("nodejs"), "-e", script],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        us, abroad, unknown = json.loads(out)
        self.assertEqual(us, [
            ["FAA plates", "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dtpp/search/results/?cycle=2611&ident=ZZA"],
            ["SkyVector", "https://skyvector.com/airport/ZZA"],
            ["Wikipedia", "https://en.wikipedia.org/wiki/Example"], ["Website", "https://example.com/"]])
        self.assertEqual(abroad, [["SkyVector", "https://skyvector.com/airport/ZZH8"]])
        self.assertEqual(unknown, [["SkyVector", "https://skyvector.com/airport/ZZQ1"]], "no details: its own code")

    def test_both_pages_load_it(self):
        for page in ("airport.html", "index.html"):
            html = (REPO / "templates" / page).read_text(encoding="utf-8")
            self.assertIn('<script src="/static/airport-links.js"></script>', html, page)
            self.assertIn("AerodromeAirportLinks.links(", html, page)


class TestShippedInfoFile(unittest.TestCase):
    """static/airport-info.json, as it ships: well formed, and built with airports.json."""

    def test_every_row_is_well_formed(self):
        data = json.loads(airports.INFO.read_text(encoding="utf-8"))
        rows = data["airports"]
        codes = {r[0] for r in json.loads(airports.DATA.read_text(encoding="utf-8"))["airports"]}
        self.assertEqual(data["count"], len(rows))
        self.assertGreater(len(rows), 5000)
        self.assertGreater(sum(1 for r in rows if r[2]), 2000, "US airports with FAA plates")
        for code, lid, plates, web, wiki, rws, radio in rows:
            self.assertIn(code, codes, "a code airports.json doesn't have")
            self.assertRegex(lid, r"^([A-Z0-9]{2,5})?$", code)
            self.assertIn(plates, (0, 1), code)
            self.assertRegex(web, r"^(https?://\S+)?$", code)
            self.assertRegex(wiki, r"^(https?://[^/]*wikipedia\.org/\S+)?$", code)
            for name, length, width, surface, lit in rws:
                self.assertIsInstance(name, str, code)
                self.assertTrue(length is None or isinstance(length, int), code)
                self.assertTrue(width is None or isinstance(width, int), code)
                self.assertIsInstance(surface, str, code)
                self.assertIn(lit, (0, 1), code)
            for label, mhz in radio:
                self.assertTrue(label, code)
                self.assertRegex(mhz, r"^\d{3}\.\d{3}(, \d{3}\.\d{3})*$", code)
        self.assertEqual(len({r[0] for r in rows}), len(rows), "one row per airport")


class TestDetails(unittest.TestCase):
    """airports.details(): one airport's row, keeping only the airports near the receiver."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.path = tmp / "airport-info.json"
        build_airports.write_info(list(_info().values()), self.path, built="2026-01-01")
        airports._info_cache.update(key=None, rows=None)
        self.addCleanup(airports._info_cache.update, key=None, rows=None)

    def test_one_airport(self):
        d = airports.details("ZZA1", {"ZZA1", "ZZB2"}, path=self.path)
        self.assertEqual(d["ident"], "ZZA")
        self.assertIs(d["faa_plates"], True)
        self.assertEqual((d["website"], d["wikipedia"]), ("https://example.com/", "https://en.wikipedia.org/wiki/Example"))
        self.assertEqual(d["runways"], [["18/36", 9000, 150, "Concrete", 1]])
        self.assertEqual(d["radio"][0], ["ATIS", "127.650"])
        self.assertEqual(airports.details("ZC3", {"ZC3"}, path=self.path)["ident"], "ZC3", "its own code")

    def test_keeps_only_the_nearby_ones_and_reads_once_per_set(self):
        real = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=real) as rd:
            airports.details("ZZA1", {"ZZA1"}, path=self.path)
            self.assertIsNone(airports.details("ZZB2", {"ZZA1"}, path=self.path), "not nearby")
            self.assertEqual(rd.call_count, 1)
            self.assertEqual(set(airports._info_cache["rows"]), {"ZZA1"})
            airports.details("ZZB2", {"ZZA1", "ZZB2"}, path=self.path)
            self.assertEqual(rd.call_count, 2, "a different set reads it again")

    def test_nothing_known_or_no_file_is_none(self):
        self.assertIsNone(airports.details("ZZQQ", {"ZZQQ"}, path=self.path))
        self.assertIsNone(airports.details("ZZA1", {"ZZA1"}, path=self.path.with_name("absent.json")))
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(airports.details("ZZA1", {"ZZA1", "X"}, path=self.path))


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
