"""v3.4.171: the Performance page's table sizes are opt-in. A normal
/api/perf/diagnostics run doesn't read dbstat at all — dbstat walks every page
of the file, which is most of a 30-second run and evicts the rest of the
database from memory. ?sizes=true does read it, remembers the result, and the
next normal run hands back those remembered bytes with fresh=false. Every
statement the endpoint's connection runs is recorded with a trace callback, so
"sizes=false never queries dbstat" is proven rather than assumed. Table names
are the app's own; rows and addresses are invented.
"""
import os
import sqlite3
import time
import unittest
from unittest import mock

import yaml

import server
from test_destructive_endpoints import _AppCase, _endpoint

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub


class _SqlTrace:
    """Wraps server._open_db_conn so every SQL statement the endpoint runs
    lands in `statements`."""

    def __init__(self, case):
        self.statements = []
        real = server._open_db_conn

        def opener(path, *args, **kwargs):
            conn = real(path, *args, **kwargs)
            conn.set_trace_callback(self.statements.append)
            return conn

        patcher = mock.patch.object(server, "_open_db_conn", opener)
        patcher.start()
        case.addCleanup(patcher.stop)

    def dbstat(self):
        return [s for s in self.statements if "dbstat" in s.lower()]


class TestPerfTableSizes(_AppCase):

    def setUp(self):
        super().setUp()
        self.diag = _endpoint(self.app, "/api/perf/diagnostics", "GET")
        conn = sqlite3.connect(self.db)
        conn.executemany(
            "INSERT INTO all_sightings (icao, callsign, lat, lon, altitude, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(f"ABC{i:03d}", f"EXA{i}", 40.0 + i / 1000, -95.0, 30000 + i, 1781539200 + i)
             for i in range(300)])
        conn.commit()
        conn.close()

    def sizes(self, report):
        return {t["name"]: t.get("size_bytes") for t in report["tables"]}

    def test_a_normal_run_never_touches_dbstat_and_reports_no_sizes(self):
        trace = _SqlTrace(self)
        report = self.diag()
        self.assertEqual(trace.dbstat(), [], "a normal run must not read every page")
        self.assertIsNone(report["table_sizes"])
        self.assertEqual([n for n, b in self.sizes(report).items() if b is not None], [])
        # The FTS5 shadow row exists only to carry a size, so it's absent too.
        self.assertNotIn("seen_aircraft_fts (shadows)", self.sizes(report))

    def test_measuring_reads_dbstat_and_reports_the_sizes_as_fresh(self):
        trace = _SqlTrace(self)
        report = self.diag(sizes=True)
        self.assertTrue(trace.dbstat(), "measuring is the dbstat pass")
        sizes = report["table_sizes"]
        self.assertTrue(sizes["fresh"])
        self.assertIsInstance(sizes["measured_at"], int)
        self.assertLess(sizes["measured_at"] - int(time.time()), 60)
        self.assertGreaterEqual(sizes["took_ms"], 0)
        table_sizes = self.sizes(report)
        self.assertGreater(table_sizes["all_sightings"], 0)
        self.assertGreater(table_sizes["seen_aircraft"], 0)
        self.assertIn("seen_aircraft_fts (shadows)", table_sizes)

    def test_the_next_normal_run_reuses_the_measured_sizes_without_being_fresh(self):
        measured = self.diag(sizes=True)
        self.assertTrue(measured["table_sizes"]["fresh"])
        trace = _SqlTrace(self)
        again = self.diag()
        self.assertEqual(trace.dbstat(), [], "the remembered sizes cost no measuring")
        self.assertEqual(again["table_sizes"]["fresh"], False)
        self.assertEqual(again["table_sizes"]["measured_at"],
                         measured["table_sizes"]["measured_at"])
        self.assertEqual(again["table_sizes"]["took_ms"], measured["table_sizes"]["took_ms"])
        first, second = self.sizes(measured), self.sizes(again)
        for name in ("all_sightings", "seen_aircraft", "hexdb_cache", "stats_records"):
            self.assertEqual(second[name], first[name], name)
        # The search index's synthetic row comes back from memory too.
        fts = "seen_aircraft_fts (shadows)"
        self.assertEqual(second.get(fts), first.get(fts))

    def test_an_app_built_after_measuring_remembers_nothing(self):
        # The remembered sizes live inside get_app, like the other per-app
        # state: an app rebuilt (a config save, an update) starts unmeasured
        # rather than showing another instance's numbers.
        self.diag(sizes=True)
        rebuilt = server.get_app(yaml.safe_load(self.cfg_path.read_text()), str(self.cfg_path))
        self.assertIsNone(_endpoint(rebuilt, "/api/perf/diagnostics", "GET")()["table_sizes"])


if __name__ == "__main__":
    unittest.main()
