"""One maintenance operation at a time (server._SingleRun / _maintenance).

Before v3.4.147 each maintenance path guarded itself, or didn't: an update
upload during an apply rewrote update/ under it (half-applied tree), and two
server-side backups in the same second both passed the running-flag check
and wrote the same zip. One try-only lock now covers every maintenance
endpoint, is kept for a while after a restart is requested, and is free at
once when the restart call fails.

Throwaway installs only (the server module's __file__ points at one); sudo
(the restart call), urlopen and the receiver probe are mocked. Invented data.
"""
import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import updater
from test_update_apply import (NEW, OLD, _body, _EndpointCase, _status, _upload,
                               _zip_release, repo_guard)

setUpModule, tearDownModule = repo_guard()


class TestSingleRun(unittest.TestCase):

    def setUp(self):
        import server
        self.server = server
        self.lock = server._SingleRun()

    def test_second_acquire_is_refused(self):
        tok = self.lock.try_acquire("an update")
        self.assertIsNotNone(tok)
        self.assertIsNone(self.lock.try_acquire("a restore"))
        self.assertEqual(self.lock.holder()["op"], "an update")

    def test_release_frees_it(self):
        tok = self.lock.try_acquire("an update")
        self.lock.release(tok)
        self.assertIsNone(self.lock.holder())
        self.assertIsNotNone(self.lock.try_acquire("a restore"))

    def test_stale_token_release_does_nothing(self):
        t1 = self.lock.try_acquire("an update")
        self.lock.release(t1)
        t2 = self.lock.try_acquire("a restore")
        self.lock.release(t1)                       # stale
        self.assertEqual(self.lock.holder()["op"], "a restore")
        self.lock.release(t2)

    def test_parked_hold_survives_release_and_expires_on_the_monotonic_clock(self):
        now = [1000.0]
        with mock.patch.object(self.server.time, "monotonic", lambda: now[0]):
            tok = self.lock.try_acquire("an update")
            self.assertTrue(self.lock.park(tok, "restarting after an update", 120))
            self.lock.release(tok)                  # the request finished
            self.assertEqual(self.lock.holder()["op"], "restarting after an update")
            self.assertTrue(self.lock.holder()["parked"])
            self.assertIsNone(self.lock.try_acquire("an update upload"))
            # wall-clock jumps change nothing
            with mock.patch.object(self.server.time, "time", lambda: 10.0**10):
                self.assertIsNone(self.lock.try_acquire("an update upload"))
            with mock.patch.object(self.server.time, "time", lambda: 1.0):
                self.assertIsNone(self.lock.try_acquire("an update upload"))
            now[0] += 121
            self.assertIsNone(self.lock.holder())
            self.assertIsNotNone(self.lock.try_acquire("an update upload"))

    def test_held_over_lock_is_taken_over_only_after_the_backstop(self):
        now = [5000.0]
        with mock.patch.object(self.server.time, "monotonic", lambda: now[0]):
            self.lock.try_acquire("a server-side backup")     # leaked: never released
            now[0] += self.server._SingleRun.BACKSTOP_SEC - 1
            self.assertIsNone(self.lock.try_acquire("an update"))
            with mock.patch.object(self.server.time, "time", lambda: time.time() + 86400):
                self.assertIsNone(self.lock.try_acquire("an update"))
            now[0] += 2
            with self.assertLogs("adsb.server", "WARNING"):
                self.assertIsNotNone(self.lock.try_acquire("an update"))

    def test_exactly_one_of_16_threads_wins_every_time(self):
        for _ in range(200):
            lock = self.server._SingleRun()
            barrier = threading.Barrier(16)
            wins = []

            def go():
                barrier.wait()
                if lock.try_acquire("x") is not None:
                    wins.append(1)
            ts = [threading.Thread(target=go) for _ in range(16)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(len(wins), 1)

    def test_busy_message_names_what_is_running(self):
        b = self.server._Busy({"op": "a server-side backup", "age_sec": 190, "parked": False})
        self.assertEqual(str(b), "A server-side backup is running (started 3 min ago). "
                                 "Try again when it finishes.")
        b = self.server._Busy({"op": "restarting after an update", "parked": True})
        self.assertIn("restarting after an update", str(b))


class _LockCase(_EndpointCase):

    def setUp(self):
        super().setUp()
        p = mock.patch("ntfy_installer.install_status", return_value={"aerodrome_managed": False})
        p.start()
        self.addCleanup(p.stop)

    def backups(self):
        b = self.inst / ".backups"
        return sorted(p.name for p in b.iterdir()) if b.is_dir() else []


class TestServerSideBackupRace(_LockCase):
    """#33b: two create-server-side calls in the same second."""

    def test_one_200_one_409_and_one_good_zip(self):
        real = shutil.disk_usage

        def slow(path):
            time.sleep(0.2)
            return real(path)
        ep = self.ep("/api/backup/create-server-side")
        out = []
        with mock.patch("shutil.disk_usage", slow):
            ts = [threading.Thread(target=lambda: out.append(ep())) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(sorted(_status(r) for r in out), [200, 409])
        busy = next(_body(r) for r in out if _status(r) == 409)
        self.assertEqual(busy["message"], "A server-side backup is already running.")
        self.assertIn("progress", busy)
        zips = sorted((self.inst / ".backups").glob("aerodrome-backup-*.zip"))
        self.assertEqual(len(zips), 1)
        sidecar = Path(str(zips[0]) + ".sha256").read_text().split()[0]
        self.assertEqual(sidecar, hashlib.sha256(zips[0].read_bytes()).hexdigest())
        with zipfile.ZipFile(zips[0]) as z:
            self.assertIsNone(z.testzip())


class TestApplyHeldMidStage(_LockCase):
    """An apply blocked inside staging: every other maintenance endpoint is
    refused and changes nothing; afterwards the tree is all the new version."""

    def test_everything_else_is_refused_while_an_apply_runs(self):
        entered, release = threading.Event(), threading.Event()
        real_copy2 = shutil.copy2
        first = {"done": False}

        def gated_copy2(*a, **kw):
            if not first["done"]:
                first["done"] = True
                entered.set()
                release.wait(30)
            return real_copy2(*a, **kw)

        result = {}
        with mock.patch("shutil.copy2", gated_copy2):
            t = threading.Thread(target=lambda: result.update(r=self.apply()))
            t.start()
            self.assertTrue(entered.wait(30), "apply never reached staging")
            try:
                self.assert_all_refused()
            finally:
                release.set()
                t.join(60)
        b = _body(result["r"])
        self.assertTrue(b["ok"], b)
        # census: every file of the release is the staged version
        for rel in ("VERSION", "main.py", "server.py", "templates/index.html"):
            self.assertIn(NEW, (self.inst / rel).read_text(), rel)
        self.assertTrue((self.inst / "only_in_new.py").exists())

    def assert_all_refused(self):
        backups = self.backups()
        upd = self.update_snapshot()
        cfg = (self.tmp / "config-live.yaml").read_text()
        # a second apply
        r = self.apply()
        self.assertEqual(_status(r), 409)
        self.assertIn("update is running", _body(r)["error"])
        # an upload
        data = _zip_release({"VERSION": "3.4.170\n", "main.py": "#\n"})
        r = self.ep("/api/updates/local/upload")(file=_upload(data))
        self.assertEqual(_status(r), 409)
        # a GitHub apply never even downloads
        with mock.patch("urllib.request.urlopen") as uo:
            r = self.ep("/api/updates/github/apply")(snapshot_db=False)
        uo.assert_not_called()
        self.assertEqual(_status(r), 409)
        # restores, config import/restore, switch-to-real, purge, delete, restart
        r = asyncio.run(self.ep("/api/backup/import")(file=_upload(b"PK\x05\x06" + b"\0" * 18),
                                                      background_tasks=None))
        self.assertEqual(_status(r), 409)
        model = _model(self.app, "/api/backup/restore-from-path")
        r = asyncio.run(self.ep("/api/backup/restore-from-path")(
            model(path=str(self.inst / ".backups" / "x.zip")), background_tasks=None))
        self.assertEqual(_status(r), 409)
        r = self.ep("/api/config/import")(file=_upload(b"web: {}\n", "config.yaml"))
        self.assertEqual(_status(r), 409)
        r = self.ep("/api/config/restore/{name}")(name="config.yaml.bak.20260101-000000")
        self.assertEqual(_status(r), 409)
        r = asyncio.run(self.ep("/api/setup/switch-to-real")(_json_request({})))
        self.assertEqual(_status(r), 409)
        r = self.ep("/api/backup/pre-restore/purge")()
        self.assertEqual(_status(r), 409)
        r = self.ep("/api/backup/server-side/{filename}", "DELETE")(filename="aerodrome-backup-x.zip")
        self.assertEqual(_status(r), 409)
        r = self.ep("/api/backup/create-server-side")()
        self.assertEqual(_status(r), 409)
        r = self.ep("/api/restart")()
        self.assertEqual(_status(r), 409)
        self.restart.assert_not_called()
        # nothing changed
        self.assertEqual(self.backups(), backups)
        self.assertEqual(self.update_snapshot(), upd)
        self.assertEqual((self.tmp / "config-live.yaml").read_text(), cfg)
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertEqual(st["busy"]["op"], "an update")


class TestHoldAfterRestart(_LockCase):

    def test_after_a_successful_apply_an_upload_is_refused_while_restarting(self):
        self.assertTrue(_body(self.apply())["ok"])
        # the journal alone would refuse an upload too; clear it to see the hold
        (self.inst / updater.PENDING).unlink()
        data = _zip_release({"VERSION": "3.4.170\n", "main.py": "#\n"})
        r = self.ep("/api/updates/local/upload")(file=_upload(data))
        self.assertEqual(_status(r), 409)
        self.assertIn("restarting after an update", _body(r)["error"])

    def test_restart_failure_frees_the_lock_at_once(self):
        self.restart.return_value = (False, "sudo: a password is required")
        r = self.apply()
        self.assertEqual(_body(r)["code"], "restart_failed")
        self.assertIsNone(self.server._MAINT.holder())
        r = self.ep("/api/backup/create-server-side")()
        self.assertEqual(_status(r), 200, _body(r))

    def test_config_import_holds_only_when_the_restart_was_requested(self):
        text = (self.tmp / "config-live.yaml").read_text().encode()
        self.restart.return_value = (False, "sudo: a password is required")
        r = self.ep("/api/config/import")(file=_upload(text, "config.yaml"))
        self.assertTrue(_body(r)["ok"], _body(r))
        self.assertIsNone(self.server._MAINT.holder())
        self.restart.return_value = (True, "")
        r = self.ep("/api/config/import")(file=_upload(text, "config.yaml"))
        self.assertTrue(_body(r)["ok"], _body(r))
        self.assertEqual(self.server._MAINT.holder()["op"], "restarting after a config import")


def _model(app, path):
    import inspect
    from test_update_apply import _endpoint
    ep = _endpoint(app, path, "POST")
    return inspect.signature(ep).parameters["payload"].annotation


def _json_request(body):
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "query_string": b""}, receive)


if __name__ == "__main__":
    unittest.main()
