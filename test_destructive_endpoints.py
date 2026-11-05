"""v3.4.141 destructive endpoints: switch-to-real deletes the install's own
database and writes the config file the server loaded (validated before
anything is touched); watchlist history deletes also clear the
watchlist_hourly rollup the Stats cards read. Endpoints are called directly
on a get_app() instance — sudo/systemctl and the receiver probe are mocked.
Also: the background schedulers of an app a newer get_app() replaced stop.
Addresses are RFC 5737 examples; hex values and labels are invented.
"""
import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import requests
import yaml

import collector
import privhelper
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

    def test_a_feeder_the_helper_cant_remove_doesnt_stop_the_switch(self):
        """v3.4.150: step 1 is the system helper's feeder-remove. When it
        can't run (permissions not refreshed yet), the step is marked failed
        with the commands that finish it, and steps 2-4 still run: the
        leftover feeder only listens on 127.0.0.1."""
        def run(argv, *a, **kw):
            if list(argv[:3]) == ["sudo", "-n", str(privhelper.HELPER)]:
                return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        ep = _endpoint(self.app, "/api/setup/switch-to-real", "POST")
        with mock.patch.object(requests, "get", return_value=mock.Mock(status_code=200)), \
             mock.patch("subprocess.run", side_effect=run) as called:
            out = asyncio.run(ep(_json_request({"receiver_ip": "192.0.2.20",
                                                "receiver_port": 30047})))
        self.assertTrue(out["ok"], out)
        steps = {s["name"]: s for s in out["steps"]}
        feeder = steps["stop_feeder_service"]
        self.assertFalse(feeder["ok"])
        self.assertIn("recover-sudoers.sh", feeder["detail"])
        self.assertIn(f"sudo {privhelper.HELPER} feeder-remove", feeder["detail"])
        self.assertEqual(out["warning"], feeder["detail"])
        for name in ("nuke_demo_db", "update_config", "restart_aerodrome"):
            self.assertTrue(steps[name]["ok"], name)
        self.assertFalse(Path(self.db).exists())
        cfg = yaml.safe_load(self.cfg_path.read_text())
        self.assertEqual((cfg["receiver"]["ip"], cfg["demo"]["enabled"]), ("192.0.2.20", False))
        sudo = [c.args[0] for c in called.call_args_list if c.args[0][:1] == ["sudo"]]
        self.assertEqual(sudo, [["sudo", "-n", str(privhelper.HELPER), "feeder-remove"],
                                ["sudo", "-n", "systemctl", "restart", "--no-block", "aerodrome"]])

    def test_invalid_settings_touch_nothing(self):
        before = self.cfg_path.read_text()
        out, run = self.run_switch({"receiver_ip": "192.0.2.20", "receiver_port": 99999})
        self.assertFalse(out["ok"])
        self.assertIn("receiver.port", out["error"])
        self.assertTrue(Path(self.db).exists())               # demo DB kept
        self.assertEqual(self.cfg_path.read_text(), before)
        run.assert_not_called()                               # feeder untouched


class TestUpdateCheckNeverCreatesTheDatabase(_AppCase):
    """The update check reads and records its state in the sightings
    database. Run while the file is gone — switch-to-real deletes it before
    its restart; in a test run, the scheduler thread of an earlier test's
    app waking during this one — it must not leave an empty database where
    the deleted one was."""

    def check(self):
        ep = _endpoint(self.app, "/api/updates/github/check", "GET")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("example: unreachable")), \
                self.assertLogs("adsb.server", "WARNING"):
            return ep(force=True)

    def test_a_missing_database_stays_missing(self):
        for p in self.dir.glob("history.db*"):
            p.unlink(missing_ok=True)
        out = self.check()
        self.assertIsNone(out["last_check_result"])
        self.assertFalse(Path(self.db).exists())

    def test_a_present_one_still_records_the_check(self):
        out = self.check()
        self.assertEqual(out["last_check_result"], "error")
        self.assertIn("Couldn't reach GitHub", out["last_check_error"])
        self.assertEqual(self.q("SELECT last_check_result FROM update_state"), [("error",)])


SCHEDULERS = ("daily-summary-scheduler", "update-check-scheduler")


class _Cut(BaseException):
    """Ends a scheduler body the test drives (not an Exception: the
    schedulers' loops catch those)."""


class TestASupersededAppsSchedulersStop(_AppCase):
    """A test run builds an app per test in one process; production builds
    one. The background schedulers read the newest app's settings (CONFIG),
    so those of an app a newer get_app() replaced must stop — not wake in a
    later test and read or record the update check's state in that test's
    database. Here the scheduler bodies are captured instead of started, and
    driven with every wait returning at once."""

    @contextlib.contextmanager
    def capturing(self):
        """get_app() with its scheduler threads captured, not started."""
        made, real = {}, threading.Thread

        def thread(*a, **kw):
            if kw.get("name") in SCHEDULERS:
                made[kw["name"]] = kw["target"]
                return mock.Mock()
            return real(*a, **kw)
        with mock.patch("threading.Thread", thread):
            yield made

    def setUp(self):
        with self.capturing() as self.a:
            super().setUp()
        self.fresh = self.dir / "fresh.db"
        shutil.copy(self.db, self.fresh)

    def build(self, tag):
        """Another app, on a fresh database: (its scheduler bodies, db)."""
        d = self.dir / f"install-{tag}"
        d.mkdir()
        db = d / "history.db"
        shutil.copy(self.fresh, db)
        cfg = yaml.safe_load(self.cfg_path.read_text())
        cfg["data"]["db_file"] = str(db)
        (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        with self.capturing() as made:
            server.get_app(cfg, str(d / "config.yaml"))
        return made, str(db.resolve())

    def drive(self, body, waits=3, on_wait=None):
        """Run a captured scheduler body in a thread of its own: each of its
        waits (time.sleep or an Event's wait) returns at once, the waits-th
        ends it; on_wait {n: fn} runs fn at the nth wait (opens from then on
        are opens[mark:]); its database opens are recorded by path, GitHub
        is unreachable."""
        res = {"waits": [], "opens": [], "mark": None, "cut": False, "error": None}
        real_sleep, real_wait, real_open = time.sleep, threading.Event.wait, server._open_db_conn
        busy = {"building": False}

        def run():
            try:
                body()
            except _Cut:
                res["cut"] = True
            except Exception as e:                  # noqa: BLE001 — reported below
                res["error"] = repr(e)
        runner = threading.Thread(target=run, daemon=True)

        def mine():
            return threading.current_thread() is runner and not busy["building"]

        def waited(seconds, answer):
            res["waits"].append(seconds)
            fn = (on_wait or {}).get(len(res["waits"]))
            if fn:
                res["mark"], busy["building"] = len(res["opens"]), True
                try:
                    fn()
                finally:
                    busy["building"] = False
            if len(res["waits"]) >= waits:
                raise _Cut()
            return answer()

        def sleep(s):
            return waited(s, lambda: None) if mine() else real_sleep(s)

        def wait(ev, timeout=None):
            return waited(timeout, ev.is_set) if mine() else real_wait(ev, timeout)

        def open_db(path, *a, **kw):
            if mine():
                res["opens"].append(str(Path(urllib.parse.urlsplit(str(path)).path).resolve()))
            return real_open(path, *a, **kw)
        with mock.patch("time.sleep", sleep), \
                mock.patch.object(threading.Event, "wait", wait), \
                mock.patch.object(server, "_open_db_conn", open_db), \
                mock.patch("urllib.request.urlopen",
                           side_effect=urllib.error.URLError("example: unreachable")), \
                mock.patch.object(server, "logger"):
            runner.start()
            runner.join(10)
        self.assertFalse(runner.is_alive(), "the scheduler body didn't end")
        self.assertIsNone(res["error"])
        return res

    def test_an_older_apps_schedulers_stop_once_a_newer_app_is_built(self):
        a_db, apps = str(Path(self.db).resolve()), {}

        # A's update check, current when it woke, is replaced while it waits
        # for its next check: it ends at that wait, touching nothing more.
        res = self.drive(self.a["update-check-scheduler"],
                         on_wait={2: lambda: apps.update(b=self.build("b"))})
        self.assertEqual(len(res["waits"]), 2, res)
        self.assertEqual(res["waits"][0], 15.0, res)
        # The first check failed (GitHub unreachable), so the wait is the back-off, not a minute.
        self.assertIn(res["waits"][1], range(3540, 3601), res)
        self.assertFalse(res["cut"], f"the replaced app's update check kept running: {res}")
        self.assertTrue(res["opens"][:res["mark"]], res)              # its own check, first
        self.assertEqual(set(res["opens"][:res["mark"]]), {a_db})
        self.assertEqual(res["opens"][res["mark"]:], [], "the replaced app's update check "
                         "opened a database after the newer app was built")

        # B's daily summary, likewise, replaced in its once-a-minute wait.
        b, b_db = apps["b"]
        res = self.drive(b["daily-summary-scheduler"],
                         on_wait={2: lambda: apps.update(c=self.build("c"))})
        self.assertEqual(res["waits"], [10.0, 60.0], res)
        self.assertFalse(res["cut"], f"the replaced app's daily summary kept running: {res}")

        # Replaced before their first wake: they end there.
        for label, body in (("A's daily summary", self.a["daily-summary-scheduler"]),
                            ("B's update check", b["update-check-scheduler"])):
            with self.subTest(label):
                res = self.drive(body)
                self.assertEqual(res["opens"], [], f"{label} opened a database")
                self.assertFalse(res["cut"], f"{label} kept running: {res}")
                self.assertEqual(len(res["waits"]), 1, res)

        # The newest app's keep their schedule, on its own database.
        c, c_db = apps["c"]
        res = self.drive(c["update-check-scheduler"])
        self.assertTrue(res["cut"], res)
        self.assertEqual(len(res["waits"]), 3, res)
        self.assertEqual(res["waits"][0], 15.0, res)
        # The first check fails (GitHub unreachable), so both later waits are the back-off.
        for w in res["waits"][1:]:
            self.assertIn(w, range(3540, 3601), res)
        self.assertTrue(res["opens"])
        self.assertEqual(set(res["opens"]), {c_db})
        res = self.drive(c["daily-summary-scheduler"])
        self.assertTrue(res["cut"], res)
        self.assertEqual(res["waits"], [10.0, 60.0, 60.0])
        # Each check went to its own app's database: A's one, C's; none to B's.
        self.assertEqual(self.q("SELECT last_check_result FROM update_state"), [("error",)])
        conn = sqlite3.connect(b_db)
        try:
            self.assertEqual(conn.execute("SELECT * FROM update_state").fetchall(), [])
        finally:
            conn.close()


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
