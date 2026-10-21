"""v3.4.141 destructive endpoints: switch-to-real deletes the install's own
database and writes the config file the server loaded (validated before
anything is touched); watchlist history deletes also clear the
watchlist_hourly rollup the Stats cards read. Endpoints are called directly
on a get_app() instance — sudo/systemctl and the receiver probe are mocked.
Addresses are RFC 5737 examples; hex values and labels are invented.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

import collector
import server
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent


def _endpoint(app, path, method):
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))


def _json_request(body):
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "query_string": b""}, receive)


class _AppCase(unittest.TestCase):

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
        cfg["watchlist"] = []
        self.customize(cfg)
        self.cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        self.app = server.get_app(cfg, str(self.cfg_path))

    def customize(self, cfg):
        pass

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class TestSwitchToReal(_AppCase):

    def customize(self, cfg):
        cfg.setdefault("demo", {})["enabled"] = True
        cfg["watchlist"] = [{"icao": "A00001", "label": "demo pick"}]

    def run_switch(self, body):
        ep = _endpoint(self.app, "/api/setup/switch-to-real", "POST")
        ok = mock.Mock(status_code=200)
        with mock.patch.object(requests, "get", return_value=ok), \
             mock.patch("subprocess.run") as run:
            out = asyncio.run(ep(_json_request(body)))
        return out, run

    def test_deletes_the_configured_db_and_writes_the_loaded_config(self):
        # Run from the temp dir with a decoy where the old code looked
        # (a cwd-relative aircraft_history.db) — never the real cwd.
        prev = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, prev)
        decoy = self.dir / "aircraft_history.db"
        decoy.write_bytes(b"decoy")
        out, run = self.run_switch({"receiver_ip": "192.0.2.20", "receiver_port": 30047,
                                    "latitude": 40.1, "longitude": -95.2})
        self.assertTrue(out["ok"], out)
        self.assertFalse(Path(self.db).exists())
        self.assertTrue(decoy.exists())                       # cwd file untouched
        self.assertFalse((self.dir / "config.yaml").exists())  # no cwd config written
        cfg = yaml.safe_load(self.cfg_path.read_text())
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.20")
        self.assertEqual(cfg["receiver"]["port"], 30047)
        self.assertFalse(cfg["demo"]["enabled"])
        self.assertEqual(cfg["watchlist"], [])
        restart = [c.args[0] for c in run.call_args_list if "restart" in c.args[0]]
        self.assertEqual(len(restart), 1)

    def test_invalid_settings_touch_nothing(self):
        before = self.cfg_path.read_text()
        out, run = self.run_switch({"receiver_ip": "192.0.2.20", "receiver_port": 99999})
        self.assertFalse(out["ok"])
        self.assertIn("receiver.port", out["error"])
        self.assertTrue(Path(self.db).exists())               # demo DB kept
        self.assertEqual(self.cfg_path.read_text(), before)
        run.assert_not_called()                               # feeder untouched


class TestWatchlistHistoryDeletes(_AppCase):

    T = 1700000000

    def customize(self, cfg):
        cfg["watchlist"] = [{"icao": "A00001", "label": "Friend"},
                            {"icao": "A00002", "label": "Other"}]

    def seed(self):
        conn = sqlite3.connect(self.db)
        for icao, label in (("A00001", "Friend"), ("A00002", "Other")):
            conn.execute("INSERT INTO watchlist_sightings (icao, seen_at, watchlist_label) "
                         "VALUES (?, ?, ?)", (icao, self.T, label))
            conn.execute("INSERT INTO watchlist_hourly (icao, hour_bucket, watchlist_label, "
                         "sighting_count, first_seen_at, last_seen_at) VALUES (?, ?, ?, 1, ?, ?)",
                         (icao, self.T, label, self.T, self.T))
        conn.commit()
        conn.close()

    def test_remove_with_history_clears_that_labels_rollup(self):
        self.seed()
        ep = _endpoint(self.app, "/api/watchlist/remove", "POST")
        entry = ep.__annotations__["entry"](identifier="A00001", id_type="icao",
                                            delete_history=True)
        out = ep(entry)
        self.assertEqual(out["history_deleted"], 1)
        self.assertEqual(self.q("SELECT watchlist_label FROM watchlist_hourly"), [("Other",)])
        self.assertEqual(self.q("SELECT watchlist_label FROM watchlist_sightings"), [("Other",)])

    def test_clear_all_history_clears_the_rollup(self):
        self.seed()
        out = _endpoint(self.app, "/api/watchlist/history/clear", "POST")()
        self.assertEqual(out["deleted"], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM watchlist_hourly"), [(0,)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
