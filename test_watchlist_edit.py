"""Editing a watchlist entry (v3.4.201): POST /api/watchlist/update.

The Watchlist tab's pill opens an Edit window that saves here. The entry is
changed in place in one locked save; renaming its label moves the sightings
stored under the old label (watchlist_sightings and the watchlist_hourly
rollup), unless another entry still uses that label. Invented identifiers only.
"""
import os
os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

import collector
import server
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
T = 1700000000


def _endpoint(app, path, method="POST"):
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))


def _status(r):
    return getattr(r, "status_code", 200)


def _body(r):
    import json
    return json.loads(r.body) if hasattr(r, "body") else r


class _Case(unittest.TestCase):
    WATCHLIST = []

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        (self.dir / "install").mkdir()
        self.cfg_path = self.dir / "install" / "config.yaml"
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = [dict(e) for e in self.WATCHLIST]
        self.cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        self.app = server.get_app(cfg, str(self.cfg_path))

    def update(self, old_type, old_id, new_type, new_id, label=""):
        ep = _endpoint(self.app, "/api/watchlist/update")
        return ep(ep.__annotations__["body"](old_id_type=old_type, old_identifier=old_id,
                                             id_type=new_type, identifier=new_id, label=label))

    def add(self, id_type, ident, label=""):
        ep = _endpoint(self.app, "/api/watchlist/add")
        return ep(ep.__annotations__["entry"](identifier=ident, id_type=id_type, label=label))

    def saved(self):
        return yaml.safe_load(self.cfg_path.read_text())["watchlist"]

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def seed(self, rows):
        """rows: (icao, label, hour_bucket, count) → one watchlist_sightings row
        per count and one watchlist_hourly row."""
        conn = sqlite3.connect(self.db)
        for icao, label, bucket, n in rows:
            for k in range(n):
                conn.execute("INSERT INTO watchlist_sightings (icao, seen_at, watchlist_label) VALUES (?, ?, ?)",
                             (icao, bucket + k, label))
            conn.execute("INSERT INTO watchlist_hourly (icao, hour_bucket, watchlist_label, callsign, "
                         "sighting_count, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (icao, bucket, label, "", n, bucket, bucket + n - 1))
        conn.commit()
        conn.close()


class TestEditingAnEntry(_Case):
    WATCHLIST = [{"callsign": "EXA123", "label": "Example Air"},
                 {"icao": "A0B1C2", "label": "Test hex"},
                 {"model": "Cirrus", "label": "Any Cirrus"}]

    def test_a_callsign_becomes_a_shorter_prefix_in_place(self):
        out = self.update("callsign", "EXA123", "callsign", "exa", "Example Air")
        self.assertEqual(out["status"], "updated")
        self.assertEqual(out["entry"], {"label": "Example Air", "callsign": "EXA"})
        self.assertEqual(self.saved()[0], {"label": "Example Air", "callsign": "EXA"})
        self.assertEqual([e.get("label") for e in self.saved()], ["Example Air", "Test hex", "Any Cirrus"])
        self.assertEqual(server.CONFIG["watchlist"][0]["callsign"], "EXA")

    def test_the_collector_matches_with_the_edited_entry(self):
        self.update("callsign", "EXA123", "callsign", "EXA", "Example Air")
        lookup = collector.build_watchlist_lookup(server.CONFIG)
        self.assertEqual(collector.match_watchlist({"hex": "a00009", "flight": "EXA9  "}, lookup), "Example Air")

    def test_the_type_can_change_and_model_keeps_its_case(self):
        out = self.update("callsign", "EXA123", "model", "  Embraer 175 ", "Example Air")
        self.assertEqual(_status(out), 200, _body(out))
        self.assertEqual(self.saved()[0], {"label": "Example Air", "model": "Embraer 175"})

    def test_a_model_entry_is_found_whatever_the_case(self):
        out = self.update("model", "CIRRUS", "model", "Cirrus SR22", "Any Cirrus")
        self.assertEqual(self.saved()[2]["model"], "Cirrus SR22")

    def test_a_blank_label_becomes_the_identifier(self):
        self.update("icao", "A0B1C2", "icao", "A0B1C3", "  ")
        self.assertEqual(self.saved()[1], {"label": "A0B1C3", "icao": "A0B1C3"})

    def test_other_keys_on_the_entry_stay(self):
        server.CONFIG["watchlist"][1]["note"] = "kept"
        self.update("icao", "A0B1C2", "icao", "A0B1C3", "Test hex")
        self.assertEqual(self.saved()[1], {"note": "kept", "label": "Test hex", "icao": "A0B1C3"})

    def test_editing_into_another_entry_is_refused(self):
        r = self.update("callsign", "EXA123", "icao", "a0b1c2", "Example Air")
        self.assertEqual(_status(r), 409)
        self.assertEqual(_body(r)["error"], "Already in the watchlist as “Test hex”")
        self.assertEqual(self.saved()[0], {"callsign": "EXA123", "label": "Example Air"})

    def test_saving_an_entry_unchanged_is_not_a_duplicate_of_itself(self):
        r = self.update("callsign", "EXA123", "callsign", "EXA123", "Example Air")
        self.assertEqual(_status(r), 200, _body(r))

    def test_an_entry_that_is_gone_is_404(self):
        r = self.update("callsign", "ZZZ9", "callsign", "ZZZ", "x")
        self.assertEqual(_status(r), 404)
        self.assertEqual(len(self.saved()), 3)

    def test_an_identifier_that_can_never_match_is_refused(self):
        for new_type, ident, words in (("callsign", "EXA*", "No wildcards needed"),
                                       ("callsign", "EX?", "No wildcards needed"),
                                       ("icao", "A0B1C", "exactly 6 characters"),
                                       ("icao", "G0B1C2", "exactly 6 characters"),
                                       ("callsign", "  ", "Enter an identifier")):
            r = self.update("callsign", "EXA123", new_type, ident, "x")
            self.assertEqual(_status(r), 400, ident)
            self.assertIn(words, _body(r)["error"])
        self.assertEqual(self.saved()[0], {"callsign": "EXA123", "label": "Example Air"})

    def test_add_refuses_a_wildcard_callsign_too(self):
        r = self.add("callsign", "EXA*", "x")
        self.assertEqual(_status(r), 400)
        self.assertIn("No wildcards needed", _body(r)["error"])
        self.assertEqual(len(self.saved()), 3)
        # Guard: an ordinary prefix is still added.
        self.assertEqual(self.add("callsign", "SMPL", "Sample")["status"], "added")


class TestRenamingMovesTheHistory(_Case):
    WATCHLIST = [{"callsign": "EXA", "label": "Example Air"},
                 {"icao": "A0B1C2", "label": "Club"},
                 {"tail": "N0EXMP", "label": "Shared"},
                 {"tail": "N0DEMO", "label": "Shared"}]

    def test_sightings_and_the_hourly_rollup_move_to_the_new_label(self):
        self.seed([("A00001", "Example Air", T, 3), ("A00002", "Example Air", T + 3600, 2), ("A0B1C2", "Club", T, 1)])
        out = self.update("callsign", "EXA", "callsign", "EXA", "Example Air (all)")
        self.assertEqual(out["history_moved"], 5)
        self.assertFalse(out["history_shared"])
        self.assertEqual(self.q("SELECT watchlist_label, COUNT(*) FROM watchlist_sightings GROUP BY 1 ORDER BY 1"),
                         [("Club", 1), ("Example Air (all)", 5)])
        self.assertEqual(self.q("SELECT watchlist_label, SUM(sighting_count) FROM watchlist_hourly GROUP BY 1 ORDER BY 1"),
                         [("Club", 1), ("Example Air (all)", 5)])

    def test_renaming_to_a_label_already_in_use_merges_the_hourly_rows(self):
        # Same aircraft and hour under both labels: one row afterwards, counts added.
        self.seed([("A0B1C2", "Example Air", T, 2), ("A0B1C2", "Club", T, 3)])
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE watchlist_hourly SET callsign = 'EXA1' WHERE watchlist_label = 'Example Air'")
        conn.commit()
        conn.close()
        out = self.update("callsign", "EXA", "callsign", "EXA", "Club")
        self.assertEqual(out["history_moved"], 2)
        self.assertEqual(self.q("SELECT watchlist_label, sighting_count, first_seen_at, last_seen_at, callsign "
                                "FROM watchlist_hourly"),
                         [("Club", 5, T, T + 2, "EXA1")])
        self.assertEqual(self.q("SELECT COUNT(*) FROM watchlist_sightings WHERE watchlist_label = 'Club'"), [(5,)])

    def test_a_label_another_entry_still_uses_keeps_its_history(self):
        self.seed([("A00003", "Shared", T, 4)])
        out = self.update("tail", "N0EXMP", "tail", "N0EXMP", "Mine")
        self.assertTrue(out["history_shared"])
        self.assertEqual(out["history_moved"], 0)
        self.assertEqual(self.q("SELECT DISTINCT watchlist_label FROM watchlist_sightings"), [("Shared",)])
        self.assertEqual(self.q("SELECT DISTINCT watchlist_label FROM watchlist_hourly"), [("Shared",)])

    def test_changing_only_the_identifier_leaves_the_history_alone(self):
        self.seed([("A00001", "Example Air", T, 2)])
        out = self.update("callsign", "EXA", "callsign", "EXA1", "Example Air")
        self.assertEqual(out["history_moved"], 0)
        self.assertEqual(self.q("SELECT DISTINCT watchlist_label FROM watchlist_sightings"), [("Example Air",)])

    def test_the_history_count_follows_the_rename(self):
        self.seed([("A00001", "Example Air", T, 3)])
        self.update("callsign", "EXA", "callsign", "EXA", "Example Air (all)")
        count = _endpoint(self.app, "/api/watchlist/history/count", "GET")
        self.assertEqual(count(label="Example Air (all)")["count"], 3)
        self.assertEqual(count(label="Example Air")["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
