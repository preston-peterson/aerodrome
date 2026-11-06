"""Aircraft type silhouettes: the generator's table, its dimension data, and
the files it ships must agree with each other.

The shipped files (static/shapes/*.svg, static/shapes-data.js) are generated
by scripts/gen_type_shapes.py and committed. Nothing regenerates them at
install time, so a table edit that isn't followed by a re-run would ship stale
shapes without any other test noticing.
"""

import csv
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHAPES_DIR = ROOT / "static" / "shapes"
DATA_JS = ROOT / "static" / "shapes-data.js"
DIMS_CSV = ROOT / "scripts" / "type_dims.csv"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "gen_type_shapes", ROOT / "scripts" / "gen_type_shapes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shipped_paths():
    """code -> path string, read from the data file the browser loads."""
    out = {}
    for m in re.finditer(r'^\s*("[A-Z0-9]+"): ("<path.*/>"),?$', DATA_JS.read_text(), re.M):
        out[json.loads(m.group(1))] = json.loads(m.group(2))
    return out


def _points(path):
    d = re.fullmatch(r'<path d="(M[-0-9. L]+ Z)"/>', path)
    if not d:
        return None
    n = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d.group(1))]
    return list(zip(n[0::2], n[1::2]))


class TypeShapeTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = _load_generator()
        cls.dims = cls.gen.load_dims()

    def test_every_row_names_a_preset_and_has_dimensions(self):
        for code, (family, _over) in self.gen.SOURCES.items():
            self.assertTrue(family in self.gen.PRESETS, f"{code}: no preset {family}")
            self.assertTrue(code in self.dims, f"{code}: no span/length in type_dims.csv or EXTRA_DIMS")
            span, length = self.dims[code]
            self.assertTrue(4 < span < 90 and 4 < length < 90, (code, span, length))

    def test_extra_dims_say_why_they_override_the_faa_table(self):
        """A code the FAA table covers uses the FAA figure unless its
        EXTRA_DIMS entry says what is wrong with that row."""
        with open(DIMS_CSV, newline="") as fh:
            in_faa = {r["code"] for r in csv.DictReader(fh)}
        for code, (span, length, source) in self.gen.EXTRA_DIMS.items():
            self.assertTrue(code in self.gen.SOURCES, f"{code}: EXTRA_DIMS entry without a SOURCES row")
            self.assertTrue(source.strip(), code)
            if code in in_faa:
                self.assertIn("FAA", source, f"{code}: say why the FAA row is not used")

    def test_aliases_point_at_drawn_types(self):
        for alias, target in self.gen.ALIASES.items():
            self.assertFalse(alias in self.gen.SOURCES, f"{alias}: both an alias and a row")
            self.assertTrue(target in self.gen.SOURCES, f"{alias}: alias target {target} is not drawn")

    def test_dimension_table_is_well_formed(self):
        with open(DIMS_CSV, newline="") as fh:
            rows = list(csv.DictReader(fh))
        codes = [r["code"] for r in rows]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertGreater(len(rows), 300)
        for r in rows:
            self.assertRegex(r["code"], r"^[A-Z0-9]{2,4}$")
            self.assertGreater(float(r["span_m"]), 0)
            self.assertGreater(float(r["length_m"]), 0)

    def test_engine_counts_match_the_dimension_table(self):
        """A twin drawn with three engines (or a single-engine jet drawn as a
        twin) is the kind of wrong row the old hand-typed table had."""
        with open(DIMS_CSV, newline="") as fh:
            listed = {r["code"]: int(r["engines"]) for r in csv.DictReader(fh)}
        # Types the FAA table lacks are checked against DOC 8643 where that
        # file is present (it is a local, untracked cache).
        doc = ROOT / "scripts" / "doc8643.csv"
        if doc.exists():
            with open(doc, newline="") as fh:
                for r in csv.DictReader(fh):
                    if r["engine_count"].isdigit():
                        listed.setdefault(r["type_designator"], int(r["engine_count"]))
        listed.update(self.gen.ENGINE_COUNT_FIXES)
        checked = 0
        for code in self.gen.SOURCES:
            if code in self.gen.BUILDERS or code not in listed or self.gen.is_rotorcraft(code):
                continue
            drawn = sum(e.get("count", 1 if e["kind"] in ("centre", "nose") else 2)
                        for e in self.gen.spec_for(code)["engines"])
            self.assertEqual(drawn, listed[code], code)
            checked += 1
        self.assertGreater(checked, 300)

    def test_rotorcraft_rows_are_helicopters_in_doc_8643(self):
        """A helicopter preset on a fixed-wing code (or the reverse) would
        draw rotor blades on an aeroplane. DOC 8643 says which is which; its
        CSV is a local, untracked cache, so the check runs only where it is."""
        doc = ROOT / "scripts" / "doc8643.csv"
        if not doc.exists():
            self.skipTest("scripts/doc8643.csv not present")
        with open(doc, newline="") as fh:
            kind = {}
            for r in csv.DictReader(fh):
                kind.setdefault(r["type_designator"], r["description"])
        for code in self.gen.SOURCES:
            if code in kind:
                self.assertEqual(self.gen.is_rotorcraft(code), kind[code] == "Helicopter", code)


class ShippedShapesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = _load_generator()
        cls.paths = _shipped_paths()

    def test_data_file_lists_exactly_the_table_and_its_aliases(self):
        want = set(self.gen.SOURCES) | set(self.gen.ALIASES)
        self.assertEqual(set(self.paths), want)

    def test_svg_directory_holds_exactly_the_table(self):
        have = {p.stem for p in SHAPES_DIR.glob("*.svg")}
        self.assertEqual(have, set(self.gen.SOURCES))

    def test_rotorcraft_list_matches_the_table(self):
        m = re.search(r"^const SHAPES_ROTORCRAFT = new Set\((\[.*\])\);$", DATA_JS.read_text(), re.M)
        self.assertIsNotNone(m, "shapes-data.js has no SHAPES_ROTORCRAFT")
        listed = set(json.loads(m.group(1)))
        want = {c for c in self.gen.SOURCES if self.gen.is_rotorcraft(c)}
        want |= {a for a, t in self.gen.ALIASES.items() if self.gen.is_rotorcraft(t)}
        self.assertEqual(listed, want)
        self.assertGreater(len(listed), 40)

    def test_aliases_ship_their_target_path(self):
        for alias, target in self.gen.ALIASES.items():
            self.assertEqual(self.paths[alias], self.paths[target], alias)

    def test_every_path_is_one_closed_outline_inside_the_box(self):
        for code, path in self.paths.items():
            pts = _points(path)
            self.assertIsNotNone(pts, f"{code}: not a single M/L path")
            self.assertGreater(len(pts), 6, code)
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            self.assertGreaterEqual(min(xs + ys), 0.5, code)
            self.assertLessEqual(max(xs + ys), 23.5, code)
            # fit to the same 22-unit box and centred on x=12
            self.assertAlmostEqual(max(max(xs) - min(xs), max(ys) - min(ys)), 22.0,
                                   delta=0.8, msg=code)
            self.assertAlmostEqual((max(xs) + min(xs)) / 2, 12.0, delta=0.15, msg=code)

    def test_shipped_files_match_the_generator(self):
        """Run the generator into a scratch directory and compare every file
        byte for byte: catches a row or preset edit that was not followed by a
        re-run. The same run checks that a run into another directory leaves
        the shipped data file alone, and that only the generator's own stale
        files are removed."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shapes"
            out.mkdir()
            stale = out / "ZZ99.svg"            # ours (scale comment), no row
            stale.write_text('<svg><!-- aerodrome-scale: 1.0 --><path d="M0 0 Z"/></svg>')
            foreign = out / "LOGO.svg"          # designator-shaped name, not ours
            foreign.write_text("<svg/>")
            other = out / "favicon.svg"
            other.write_text("<svg/>")
            before = DATA_JS.read_bytes()
            run = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "gen_type_shapes.py"), "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(DATA_JS.read_bytes(), before, "a preview run rewrote the shipped data file")
            self.assertFalse(stale.exists())
            self.assertTrue(foreign.exists())
            self.assertTrue(other.exists())
            foreign.unlink()
            other.unlink()
            for code in self.gen.SOURCES:
                self.assertEqual((out / f"{code}.svg").read_bytes(),
                                 (SHAPES_DIR / f"{code}.svg").read_bytes(),
                                 f"{code}: run python3 scripts/gen_type_shapes.py")
            self.assertEqual((out / "shapes-data.js").read_bytes(), before,
                             "static/shapes-data.js: run python3 scripts/gen_type_shapes.py")

    def test_a_detached_part_is_refused(self):
        """Only one outline is traced, so a part that does not touch the rest
        would vanish from the shape without this guard."""
        body = [(10.0, 4.0), (14.0, 4.0), (14.0, 20.0), (10.0, 20.0)]
        island = [(18.0, 10.0), (20.0, 10.0), (20.0, 12.0), (18.0, 12.0)]
        self.assertGreater(len(self.gen._merge([("p", body)])), 3)
        with self.assertRaises(RuntimeError):
            self.gen._merge([("p", body), ("p", island)])


if __name__ == "__main__":
    unittest.main()
