"""v3.4.143 Stats drill panels: today's-slice queries keep one fast plan under
any planner statistics, the all-categories panel agrees with its card, and the
extremes drills still return the record-setting sighting.

The plan test captures the SQL each drill actually runs (sqlite3 trace
callback) and EXPLAINs it under injected planner statistics, because each of
these once produced a different multi-second plan:
  - none (never ANALYZEd);
  - fresh-one-aircraft / fresh-two-aircraft: the collector's first-poll
    ANALYZE when that poll saw 1 or 2 aircraft (K=1 turned a join's inner
    loop into a scan of the per-aircraft peaks for every row);
  - busy-install: 251 rows per seen_at value (a skip-scan join, 52-58 s);
  - box-like: the maintainer's install (the original whole-table walk).
It allows only index seeks on all_sightings and index lookups as the inner
loop of any join. Data is invented.
"""
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collector
import server
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
SLICE_DRILLS = ["fastest", "slowest", "highest_altitude", "lowest_altitude",
                "unique_today", "top_types=B738", "top_operators=DAL",
                "all_category_mix", "all_countries"]


def _stats(n, per_seen_at, per_icao):
    return {"idx_all_seen_icao": f"{n} {per_seen_at} 1", "idx_all_icao": f"{n} {per_icao}",
            "idx_all_seen": f"{n} {per_seen_at}", "idx_all_seen_lat_lon": f"{n} {per_seen_at} 1 1"}


STAT_REGIMES = {
    "fresh-one-aircraft": _stats(1, 1, 1),
    "fresh-two-aircraft": _stats(2, 2, 1),
    "busy-install": _stats(3127301, 251, 857),
    "box-like": _stats(14819584, 50, 4300),
}
PINNED_SEEK = re.compile(r"^SEARCH (all_sightings|s) USING (COVERING )?INDEX idx_all_seen_icao "
                         r"\((seen_at>\?|seen_at=\? AND icao=\?)\)$")


class _DrillCase(unittest.TestCase):
    """A migrated scratch DB with a few invented aircraft seen today (UTC)."""

    SIGHTINGS = [
        # icao, callsign, type, speed, altitude, seen_at offset
        ("A00001", "DAL101", "B738", 450, 35000, 60),
        ("A00001", "DAL101", "B738", 480, 36000, 120),   # A00001's peak…
        ("A00001", "DAL101", "B738", 480, 36000, 180),   # …reached again later
        ("A00002", "N1EX",   "C172", 95,  3500, 90),
        ("A00002", "N1EX",   "C172", 110, 5500, 150),
        ("A00003", "",       "R66",  60,  800,  200),
        ("~00004", "TISB1",  "",     500, 40000, 210),   # TIS-B: excluded
    ]

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        # Seed and drill on the same UTC day: right before midnight, wait
        # it out so the drill's "today" is the day the rows were put in.
        left = 86400 - time.time() % 86400
        if left < 5:
            time.sleep(left + 0.05)
        self.day = d = int(time.time()) // 86400 * 86400
        conn.executemany(
            "INSERT INTO all_sightings (icao, callsign, aircraft_type, speed, "
            "altitude, seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(i, c, t, s, a, d + o) for i, c, t, s, a, o in self.SIGHTINGS])
        for icao, cat, country in (("A00001", "commercial", "United States"),
                                   ("A00002", "general_aviation", "United States"),
                                   ("A00003", "helicopter", "United States")):
            conn.execute("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at, "
                         "category, country, sighting_count) VALUES (?, ?, ?, ?, ?, 1)",
                         (icao, d, d + 200, cat, country))
        conn.commit()
        conn.close()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        cfg.setdefault("stats", {})["timezone"] = "UTC"
        app = server.get_app(cfg, os.devnull)
        eps = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
        self.drill_ep, self.stats_ep = eps["/api/stats/drill"], eps["/api/stats"]

    def drill(self, spec, all_rows=False):
        card, _, value = spec.partition("=")
        return self.drill_ep(card=card, value=value or None, all=all_rows)

    def execute(self, sql, rows=()):
        conn = sqlite3.connect(self.db)
        try:
            conn.executemany(sql, rows) if rows else conn.execute(sql)
            conn.commit()
        finally:
            conn.close()


class TestDrillResults(_DrillCase):

    def test_extremes_report_the_first_record_setting_sighting(self):
        d = self.day
        # A second aircraft ties A00001's 480 kt (icao order breaks the tie).
        self.execute("INSERT INTO all_sightings (icao, callsign, aircraft_type, speed, "
                     "altitude, seen_at) VALUES ('A00000', 'DAL100', 'B738', 480, 30000, ?)"
                     .replace("?", str(d + 300)))
        expect = {
            "fastest": [("A00000", 480, d + 300), ("A00001", 480, d + 120),
                        ("A00002", 110, d + 150), ("A00003", 60, d + 200)],
            "slowest": [("A00003", 60, d + 200), ("A00002", 95, d + 90),
                        ("A00001", 450, d + 60), ("A00000", 480, d + 300)],
            "highest_altitude": [("A00001", 36000, d + 120), ("A00000", 30000, d + 300),
                                 ("A00002", 5500, d + 150), ("A00003", 800, d + 200)],
            "lowest_altitude": [("A00003", 800, d + 200), ("A00002", 3500, d + 90),
                                ("A00000", 30000, d + 300), ("A00001", 35000, d + 60)],
        }
        for card, want in expect.items():
            for all_rows in (False, True):
                with self.subTest(card=card, all=all_rows):
                    rows = self.drill(card, all_rows)["rows"]
                    self.assertEqual([(r["icao"], r["metric"], r["seen_at"]) for r in rows], want)

    def test_all_category_mix_agrees_with_its_card(self):
        d = self.day
        self.execute("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at, category, "
                     "sighting_count) VALUES (?, ?, ?, ?, 1)",
                     [("A00005", d, d + 30, "military"),        # no raw rows today
                      ("A00006", d - 86400, d - 60, "commercial"),  # yesterday only
                      ("A00007", d, d + 40, None)])              # never categorised
        card = {c["category"]: c["n"] for c in self.stats_ep()["cards"]["category_mix"]}
        self.assertEqual(card, {"Commercial": 1, "General Aviation": 1,
                                "Helicopter": 1, "Military": 1})
        rows = {r["category"]: (r["aircraft_count"], r["sightings"], r["first_seen"], r["last_seen"])
                for r in self.drill("all_category_mix")["rows"]}
        self.assertEqual(rows, {"Commercial": (1, 3, d + 60, d + 180),
                                "General Aviation": (1, 2, d + 90, d + 150),
                                "Helicopter": (1, 1, d + 200, d + 200),
                                "Military": (1, 0, None, None)})


class TestSliceDrillPlans(_DrillCase):

    def captured_sql(self):
        """Every all_sightings statement each slice drill runs, by drill."""
        by_spec, seen = {}, []
        real = server._open_db_conn

        def traced(*a, **kw):
            conn = real(*a, **kw)
            conn.set_trace_callback(seen.append)
            return conn
        with mock.patch.object(server, "_open_db_conn", traced):
            for spec in SLICE_DRILLS:
                seen.clear()
                self.drill(spec)
                by_spec[spec] = [s for s in seen if "all_sightings" in s]
        return by_spec

    def plans(self, regime, statements):
        if regime:
            conn = sqlite3.connect(self.db)
            conn.execute("ANALYZE")                       # creates sqlite_stat1
            conn.execute("DELETE FROM sqlite_stat1 WHERE tbl = 'all_sightings'")
            conn.executemany("INSERT INTO sqlite_stat1 VALUES ('all_sightings', ?, ?)",
                             list(STAT_REGIMES[regime].items()))
            conn.commit()
            conn.close()
        conn = sqlite3.connect(self.db)                   # (re)loads the stats
        try:
            return {s: list(conn.execute("EXPLAIN QUERY PLAN " + s)) for s in statements}
        finally:
            conn.close()

    def test_every_drill_read_is_an_index_seek_under_any_stats(self):
        by_spec = self.captured_sql()
        for spec, stmts in by_spec.items():
            self.assertTrue(any("seen_at >=" in s for s in stmts), f"{spec}: slice read not captured")
        statements = sorted({s for stmts in by_spec.values() for s in stmts})
        for regime in [None] + list(STAT_REGIMES):
            for sql, plan in self.plans(regime, statements).items():
                with self.subTest(regime=regime, sql=" ".join(sql.split())[:90]):
                    loops = {}
                    for _id, parent, _, detail in plan:
                        # SQLite < 3.36 says "SEARCH TABLE x" / "SCAN TABLE x".
                        detail = re.sub(r"^(SCAN|SEARCH) TABLE ", r"\1 ", detail)
                        if re.search(r"\b(all_sightings|s)\b", detail) and detail.startswith(("SCAN", "SEARCH")):
                            self.assertRegex(detail, PINNED_SEEK)
                        if detail.startswith(("SCAN ", "SEARCH ")):
                            loops.setdefault(parent, []).append(detail)
                    for parent, details in loops.items():
                        for inner in details[1:]:        # a join's inner loops
                            self.assertTrue(inner.startswith("SEARCH "),
                                            f"nested scan: {' | '.join(details)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
