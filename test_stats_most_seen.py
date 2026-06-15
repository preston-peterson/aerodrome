"""End-to-end SQLite read-path tests for the "Most seen, all time" Stats card.

The card and its drill read `seen_aircraft.sighting_count` directly (a true
all-time per-aircraft counter, never pruned) — top-5 for the card, top-100 for
the drill. These tests mirror the exact SQL the server runs (server.py
get_stats `most_seen_alltime` card + the drill branch) and verify ordering,
LIMIT, returned fields, the population COUNT, and the empty case against real
SQLite. If the server SQL changes, keep these in sync.
"""
import sqlite3
import unittest

# Mirrors server.py: get_stats() card_check("most_seen_alltime")
CARD_SQL = """
    SELECT icao, last_callsign, aircraft_type, aircraft_type_desc,
           operator, registration, sighting_count AS n
    FROM seen_aircraft
    ORDER BY sighting_count DESC
    LIMIT 5
"""
# Mirrors server.py: drill branch `elif card == "most_seen_alltime"`
DRILL_SQL = """
    SELECT icao, last_callsign, aircraft_type, aircraft_type_desc,
           operator, registration, first_seen_at, last_seen_at,
           sighting_count AS n
    FROM seen_aircraft
    ORDER BY sighting_count DESC
    LIMIT 100
"""
TOTAL_SQL = "SELECT COUNT(*) AS n FROM seen_aircraft"


def _db(rows):
    """rows = list of (icao, last_callsign, operator, first_seen_at, last_seen_at, sighting_count)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE seen_aircraft(
            icao TEXT PRIMARY KEY, last_callsign TEXT, aircraft_type TEXT,
            aircraft_type_desc TEXT, operator TEXT, registration TEXT,
            first_seen_at INTEGER, last_seen_at INTEGER,
            sighting_count INTEGER NOT NULL DEFAULT 0)"""
    )
    conn.executemany(
        "INSERT INTO seen_aircraft"
        "(icao,last_callsign,operator,first_seen_at,last_seen_at,sighting_count) "
        "VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


class TestMostSeenAllTime(unittest.TestCase):
    def test_card_top5_order_and_limit(self):
        rows = [(f"A{i:05X}", f"CS{i}", "UAL", 1000, 2000, i * 10) for i in range(20)]
        conn = _db(rows)
        res = conn.execute(CARD_SQL).fetchall()
        self.assertEqual(len(res), 5, "card is capped at top 5")
        ns = [r["n"] for r in res]
        self.assertEqual(ns, sorted(ns, reverse=True), "ordered by sighting_count DESC")
        self.assertEqual(ns[0], 190, "highest sighting_count first (i=19 -> 190)")

    def test_card_returns_expected_fields(self):
        conn = _db([("ABC123", "DAL512", "DAL", 1, 2, 500)])
        r = conn.execute(CARD_SQL).fetchone()
        self.assertEqual(r["icao"], "ABC123")
        self.assertEqual(r["n"], 500)
        for f in ("last_callsign", "aircraft_type", "aircraft_type_desc",
                  "operator", "registration"):
            self.assertIn(f, r.keys())

    def test_drill_top100_and_population_count(self):
        rows = [(f"A{i:05X}", None, None, 100, 200, i) for i in range(150)]
        conn = _db(rows)
        res = conn.execute(DRILL_SQL).fetchall()
        self.assertEqual(len(res), 100, "drill capped at 100")
        self.assertEqual(res[0]["n"], 149, "highest count first")
        self.assertEqual(res[-1]["n"], 50, "100th row is the 100th-highest count")
        total = conn.execute(TOTAL_SQL).fetchone()["n"]
        self.assertEqual(total, 150, "footer total is the full population, not the cap")

    def test_drill_carries_first_last_seen(self):
        conn = _db([("ABC123", "DAL512", "DAL", 111, 222, 7)])
        r = conn.execute(DRILL_SQL).fetchone()
        self.assertEqual(r["first_seen_at"], 111)
        self.assertEqual(r["last_seen_at"], 222)

    def test_empty_table(self):
        conn = _db([])
        self.assertEqual(conn.execute(CARD_SQL).fetchall(), [])
        self.assertEqual(conn.execute(DRILL_SQL).fetchall(), [])
        self.assertEqual(conn.execute(TOTAL_SQL).fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
