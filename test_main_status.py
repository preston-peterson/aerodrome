"""`main.py status` — the terminal summary an operator runs by hand.

The `All:` count must come from `seen_aircraft` (one row per aircraft, the
query /api/status already uses), not `COUNT(DISTINCT icao)` over
`all_sightings`, which scans every raw row in the window (~6.5s cold on a big
install) for the same number. Military and watchlist keep their raw counts.
The printed lines themselves must not change. The receiver request and the
PID lookup are mocked; hexes and the receiver address are invented.
"""
import contextlib
import io
import os
import re
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import requests

import collector
from schema_migrations import apply_schema_migrations

DAY = 86400
ANSI = re.compile(r"\033\[[0-9;]*m")

# Invented hexes (never a real ICAO assignment).
RECENT_HEX = "A00001"
STALE_HEX = "A00002"
MILITARY_HEX = "A00003"


def field(text, label):
    """The value after `label:` in the printed summary, escapes stripped."""
    for line in ANSI.sub("", text).splitlines():
        if line.strip().startswith(label):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no {label!r} line in:\n{text}")


class _StatusCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.now = int(time.time())
        self.config = {
            "receiver": {"ip": "192.0.2.10", "port": 8080,
                         "path": "/data/aircraft.json"},
            "data": {"db_file": self.db},
            "retention": {"military_days": 30, "watchlist_days": 30, "all_days": 30},
        }

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def sql(self, statement, args=()):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(statement, args)
            conn.commit()
        finally:
            conn.close()

    def seed_seen_aircraft(self, icao, last_seen_at):
        self.sql("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at) "
                 "VALUES (?, ?, ?)", (icao, last_seen_at, last_seen_at))

    def seed_sighting(self, table, icao, seen_at):
        self.sql(f"INSERT INTO {table} (icao, callsign, seen_at) VALUES (?, '', ?)",
                 (icao, seen_at))

    def status_text(self):
        import main
        out = io.StringIO()
        with mock.patch.object(requests, "get", return_value=mock.Mock(status_code=200)), \
                mock.patch.object(main, "_running_instance_pid", return_value=None), \
                contextlib.redirect_stdout(out):
            main.status(self.config)
        return out.getvalue()


class TestStatusCounts(_StatusCase):

    def test_all_counts_aircraft_last_seen_in_the_window(self):
        # One aircraft inside retention, one last seen before it — and raw
        # sightings for BOTH inside the window, which is exactly what the old
        # COUNT(DISTINCT icao) over all_sightings counted (2).
        self.seed_seen_aircraft(RECENT_HEX, self.now - DAY)
        self.seed_seen_aircraft(STALE_HEX, self.now - (400 * DAY))
        for icao in (RECENT_HEX, STALE_HEX):
            self.seed_sighting("all_sightings", icao, self.now - DAY)
        self.assertEqual(field(self.status_text(), "All"), "1 unique aircraft")

    def test_an_empty_window_counts_zero(self):
        self.seed_seen_aircraft(STALE_HEX, self.now - (400 * DAY))
        self.assertEqual(field(self.status_text(), "All"), "0 unique aircraft")

    def test_military_and_watchlist_still_count_unique_aircraft_from_raw(self):
        self.seed_seen_aircraft(MILITARY_HEX, self.now - DAY)
        for seen_at in (self.now - DAY, self.now - (2 * DAY)):
            self.seed_sighting("military_sightings", MILITARY_HEX, seen_at)
        self.seed_sighting("military_sightings", MILITARY_HEX, self.now - (400 * DAY))
        text = self.status_text()
        self.assertEqual(field(text, "Military"), "1 unique aircraft")
        self.assertEqual(field(text, "Watchlist"), "0 unique aircraft")
        self.assertEqual(field(text, "All"), "1 unique aircraft")

    def test_the_summary_lines_are_unchanged(self):
        self.seed_seen_aircraft(RECENT_HEX, self.now - DAY)
        text = self.status_text()
        self.assertEqual(field(text, "Tracker"), "not running")
        self.assertEqual(field(text, "Receiver"),
                         "reachable  (http://192.0.2.10:8080/data/aircraft.json)")
        self.assertEqual(field(text, "Database"), f"exists  ({self.db})")
        self.assertEqual(field(text, "Retention"),
                         "military 30d / watchlist 30d / all 30d")
        self.assertEqual(field(text, "All"), "1 unique aircraft")

    def test_a_missing_database_counts_zero_and_says_missing(self):
        self.config["data"]["db_file"] = os.path.join(
            os.path.dirname(self.db), "example-not-here.db")
        text = self.status_text()
        self.assertEqual(field(text, "Database"),
                         f"missing  ({self.config['data']['db_file']})")
        self.assertEqual(field(text, "All"), "0 unique aircraft")


if __name__ == "__main__":
    unittest.main()
