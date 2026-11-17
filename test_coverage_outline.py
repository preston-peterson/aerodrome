"""The Live map's coverage outline (/api/coverage): the farthest grid cell in
each bearing bin, kept for the finished hours so a refresh reads only the
last ones. Every answer must equal reading the whole rollup, which is what
3.4.192 did on each refresh."""
import json
import math
import os
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub

import collector
import hexdb_resolver
import server
from distance import compass_bearing, haversine
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
RX = (40.0, -100.0)            # an invented receiver
MOVED = (40.5, -99.0)
HOUR = 3600
BASE = 1794000000 // HOUR * HOUR


def full_read(conn, rx, unit="mi"):
    """3.4.192's /api/coverage, verbatim in effect: the points and the farthest distance."""
    rows = conn.execute("""
        SELECT ROUND(last_lat / 0.05) * 0.05, ROUND(last_lon / 0.05) * 0.05
        FROM sightings_hourly WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
        GROUP BY 1, 2""").fetchall()
    best_d, best_pt = [0.0] * 36, [None] * 36
    for lat2, lon2 in rows:
        d = haversine(rx[0], rx[1], lat2, lon2, unit)
        if d is None or d <= 0:
            continue
        bi = int(round(compass_bearing(rx[0], rx[1], lat2, lon2) / 10.0)) % 36
        if d > best_d[bi]:
            best_d[bi], best_pt[bi] = d, [round(lat2, 4), round(lon2, 4)]
    return [p for p in best_pt if p is not None], (round(max(best_d), 1) if any(best_d) else 0)


class _Db(unittest.TestCase):

    def setUp(self):
        d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.db = os.path.join(d, "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.rng = random.Random(7)
        self.n = 0

    def sql(self, statement, args=()):
        conn = sqlite3.connect(self.db)
        try:
            out = conn.execute(statement, args).fetchall()
            conn.commit()
            return out
        finally:
            conn.close()

    def seen(self, hour, lat, lon, icao=None):
        """One aircraft's row for that hour, as the collector's upsert leaves it."""
        if icao is None:
            self.n += 1
            icao = f"T{self.n:05X}"
        self.sql("""INSERT INTO sightings_hourly (icao, hour_bucket, sighting_count, first_seen_at,
                                                  last_seen_at, last_lat, last_lon)
                    VALUES (?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT (icao, hour_bucket) DO UPDATE SET
                        sighting_count = sighting_count + 1, last_lat = excluded.last_lat,
                        last_lon = excluded.last_lon, last_seen_at = excluded.last_seen_at""",
                 (icao, hour, hour, hour + 60, lat, lon))
        return icao

    def traffic(self, hour, n=40, spread=3.0):
        for _ in range(n):
            self.seen(hour, RX[0] + self.rng.uniform(-spread, spread),
                      RX[1] + self.rng.uniform(-spread, spread))

    def outline(self, now, rx=RX, trace=None):
        conn = sqlite3.connect(self.db)
        if trace is not None:
            conn.set_trace_callback(trace.append)
        try:
            best = server._coverage_outline(conn, rx[0], rx[1], now)
        finally:
            conn.close()
        return [[b[1], b[2]] for b in best if b is not None]

    def expected(self, rx=RX):
        conn = sqlite3.connect(self.db)
        try:
            return full_read(conn, rx)[0]
        finally:
            conn.close()

    def saved(self):
        row = self.sql("SELECT value FROM _aerodrome_meta WHERE key = ?", (server.COVERAGE_META_KEY,))
        return json.loads(row[0][0]) if row else None


class TestCoverageOutline(_Db):

    def test_matches_a_full_read_as_the_hours_pass(self):
        for h in range(48):
            self.traffic(BASE - (48 - h) * HOUR)
        now, prev = BASE + 60, None
        for step in range(8):
            hour = now // HOUR * HOUR
            if prev and prev[0] == hour:
                # the current hour's row is overwritten as the aircraft flies back in:
                # where it was at the last refresh no longer counts
                self.seen(hour, RX[0] + 0.1, RX[1] + 0.1, icao=prev[1])
            self.traffic(hour, n=15, spread=3.0 + step * 0.2)
            far = self.seen(hour, RX[0] + 5.0 * math.cos(step), RX[1] + 5.0 * math.sin(step))
            prev = (hour, far)
            self.assertEqual(self.outline(now), self.expected(), f"step {step}")
            now += 1700

    def test_kept_and_carried_on_from_where_it_left_off(self):
        for h in range(24):
            self.traffic(BASE - (24 - h) * HOUR)
        now = BASE + 600
        first = []
        self.outline(now, trace=first)
        self.assertTrue(any("+hour_bucket <" in s for s in first), "the first refresh reads the whole rollup")
        self.assertEqual(self.saved()["through"], BASE - HOUR)

        self.traffic(BASE + HOUR)
        self.traffic(BASE + 2 * HOUR)
        later = []
        got = self.outline(BASE + 3 * HOUR + 60, trace=later)   # a restart in between changes nothing
        reads = [s for s in later if "FROM sightings_hourly" in s]
        self.assertTrue(reads)
        self.assertFalse(any("+hour_bucket" in s for s in reads), reads)
        self.assertTrue(all(f"hour_bucket >= {BASE - HOUR}" in s or f"hour_bucket >= {BASE + 2 * HOUR}" in s
                            for s in reads), reads)
        self.assertEqual(got, self.expected())
        self.assertEqual(self.saved()["through"], BASE + 2 * HOUR)

    def test_the_whole_read_is_one_scan_and_a_refresh_uses_the_index(self):
        for h in range(6):
            self.traffic(BASE - (6 - h) * HOUR)
        self.sql("ANALYZE")
        stmts = []
        self.outline(BASE + 60, trace=stmts)
        self.outline(BASE + 2 * HOUR + 60, trace=stmts)
        plans = {}
        conn = sqlite3.connect(self.db)
        try:
            for s in stmts:
                if "FROM sightings_hourly" in s:
                    plans[s] = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + s))
        finally:
            conn.close()
        whole = [p for s, p in plans.items() if "+hour_bucket" in s]
        recent = [p for s, p in plans.items() if "+hour_bucket" not in s]
        self.assertEqual(len(whole), 1)
        self.assertIn("SCAN sightings_hourly", whole[0])
        self.assertNotIn("INDEX", whole[0])
        self.assertTrue(recent)
        for p in recent:
            self.assertRegex(p, r"SEARCH sightings_hourly USING INDEX idx_hourly_bucket")

    def test_a_moved_receiver_starts_over(self):
        for h in range(12):
            self.traffic(BASE - (12 - h) * HOUR)
        self.outline(BASE + 60)
        stmts = []
        got = self.outline(BASE + 120, rx=MOVED, trace=stmts)
        self.assertTrue(any("+hour_bucket <" in s for s in stmts))
        self.assertEqual(got, self.expected(MOVED))
        self.assertEqual(self.saved()["receiver"], list(MOVED))

    def test_damaged_saved_state_starts_over(self):
        for h in range(12):
            self.traffic(BASE - (12 - h) * HOUR)
        for bad in ("not json", json.dumps({"receiver": list(RX), "grid": 0.05, "through": BASE, "best": [None]}),
                    json.dumps([1, 2, 3])):
            self.sql("INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
                     (server.COVERAGE_META_KEY, bad))
            self.assertEqual(self.outline(BASE + 60), self.expected(), bad)

    def test_no_positions_is_an_empty_outline(self):
        self.assertEqual(self.outline(BASE + 60), [])
        self.assertEqual(self.saved()["best"], [None] * server.COVERAGE_BINS)

    def test_a_locked_database_still_answers(self):
        for h in range(6):
            self.traffic(BASE - (6 - h) * HOUR)
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN IMMEDIATE")
        try:
            conn = sqlite3.connect(self.db, timeout=0.1)
            try:
                with self.assertLogs("adsb.server", level="WARNING"):
                    best = server._coverage_outline(conn, RX[0], RX[1], BASE + 60)
            finally:
                conn.close()
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual([[b[1], b[2]] for b in best if b], self.expected())
        self.assertIsNone(self.saved())


class TestCoverageEndpoint(_Db):

    def setUp(self):
        super().setUp()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        cfg["receiver"].update(latitude=RX[0], longitude=RX[1], distance_unit="mi")
        for p in (mock.patch.object(hexdb_resolver, "enqueue", return_value=False),
                  mock.patch.object(hexdb_resolver, "start_worker")):
            p.start()
            self.addCleanup(p.stop)
        cfg_path = Path(self.db).parent / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        self.app = server.get_app(cfg, str(cfg_path))
        self.ep = next(r.endpoint for r in self.app.routes
                       if getattr(r, "path", "") == "/api/coverage" and "GET" in getattr(r, "methods", ()))

    def test_same_answer_as_before(self):
        import time
        for h in range(30):
            self.traffic(int(time.time()) // HOUR * HOUR - (30 - h) * HOUR)
        out = self.ep()
        conn = sqlite3.connect(self.db)
        try:
            points, far = full_read(conn, RX, "mi")
        finally:
            conn.close()
        self.assertEqual(out["available"], True)
        self.assertEqual(out["points"], points)
        self.assertAlmostEqual(out["max_distance"], far, delta=0.1)
        self.assertEqual((out["distance_unit"], out["receiver"]), ("mi", {"lat": RX[0], "lon": RX[1]}))
        self.assertIs(self.ep(), out, "cached for five minutes")


if __name__ == "__main__":
    unittest.main()
