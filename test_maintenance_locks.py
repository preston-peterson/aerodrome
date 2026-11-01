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
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import updater
from test_update_apply import (NEW, OLD, REPO, _body, _EndpointCase, _status, _upload,
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


# ===========================================================================
# B4b (v3.4.148): backups never leave a partial or a database-sized file
# behind; the backup download is for this page only; startup sweeps
# leftovers. Driven through the full get_app ASGI stack (same-origin and
# body-size middleware in the path) where the transport matters.
# ===========================================================================

def _backup_files(inst):
    """Every file under .backups/ (relative), and the backup list's names."""
    b = inst / ".backups"
    if not b.is_dir():
        return []
    return sorted(str(p.relative_to(b)) for p in b.rglob("*") if p.is_file())


def _fds_under(path):
    out = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            t = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if t.startswith(str(path)):
            out.append(t)
    return out


def _export_scope(headers=(), spec="2.4"):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": spec},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/api/backup/export", "raw_path": b"/api/backup/export",
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"127.0.0.1:18299"), *headers],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 18299)}


class TestServerSideBackupNeverListsAPartial(_LockCase):

    def test_the_zip_appears_only_when_complete(self):
        # Before: the zip was written in place in .backups/, so while it was
        # being built (and forever after a kill or a crash) a cut-short zip
        # sat in the backup list looking like a real backup.
        gate, seen = threading.Event(), {}
        real_write = zipfile.ZipFile.write

        def slow_write(zf, filename, arcname=None, *a, **kw):
            if arcname == "aerodrome.db":
                seen["listed"] = [b["filename"] for b in
                                  self.ep("/api/backup/list-server-side", "GET")()["backups"]]
                seen["files"] = [f for f in _backup_files(self.inst)
                                 if f.startswith("aerodrome-backup-")]
                gate.wait(10)
            return real_write(zf, filename, arcname, *a, **kw)
        out = []
        with mock.patch.object(zipfile.ZipFile, "write", slow_write):
            t = threading.Thread(target=lambda: out.append(
                self.ep("/api/backup/create-server-side")()))
            t.start()
            for _ in range(500):
                if "files" in seen:
                    break
                time.sleep(0.01)
            gate.set()
            t.join()
        self.assertEqual(seen["listed"], [])
        self.assertEqual(seen["files"], [])
        b = _body(out[0])
        self.assertTrue(b["ok"], b)
        zips = sorted((self.inst / ".backups").glob("aerodrome-backup-*.zip"))
        self.assertEqual([z.name for z in zips], [b["filename"]])
        sidecar = Path(str(zips[0]) + ".sha256").read_text().split()[0]
        self.assertEqual(sidecar, hashlib.sha256(zips[0].read_bytes()).hexdigest())
        self.assertEqual(list((self.inst / ".backups" / ".tmp").iterdir()), [])

    def test_a_full_disk_mid_build_leaves_nothing_and_frees_the_lock(self):
        def full(*a, **kw):
            raise OSError(28, "No space left on device")
        with mock.patch.object(zipfile.ZipFile, "write", full):
            r = self.ep("/api/backup/create-server-side")()
        self.assertEqual(_status(r), 507, _body(r))
        self.assertIn("Not enough free disk space", _body(r)["message"])
        self.assertEqual([f for f in _backup_files(self.inst) if "aerodrome-backup" in f], [])
        self.assertEqual(self.ep("/api/backup/list-server-side", "GET")()["backups"], [])
        self.assertEqual(list((self.inst / ".backups" / ".tmp").iterdir()), [])
        self.assertIsNone(self.server._MAINT.holder())

    def test_space_check_counts_snapshot_and_zip(self):
        db = Path(self.server.CONFIG["data"]["db_file"])
        size = db.stat().st_size
        need = 2 * size + 256 * 1024 * 1024
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=need - 1)):
            r = self.ep("/api/backup/create-server-side")()
        self.assertEqual(_status(r), 507)
        self.assertEqual(_body(r)["error"], _body(r)["message"])

    def test_a_failed_snapshot_leaves_nothing_in_tmp(self):
        # Guard: the snapshot's -journal/-wal/-shm go with it.
        import errno
        real = self.server._open_db_conn

        def opener(path, *a, **kw):
            if "snapshot-" in str(path):
                for sfx in ("-journal", "-wal", "-shm"):
                    Path(str(path) + sfx).write_bytes(b"x")
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, *a, **kw)
        with mock.patch.object(self.server, "_open_db_conn", opener):
            r = self.ep("/api/backup/create-server-side")()
        self.assertEqual(_status(r), 507, _body(r))
        self.assertEqual(list((self.inst / ".backups" / ".tmp").iterdir()), [])

    def test_two_in_the_same_second_get_distinct_names(self):
        with mock.patch.object(self.server.time, "strftime", return_value="20261022-120000"):
            a = _body(self.ep("/api/backup/create-server-side")())
            b = _body(self.ep("/api/backup/create-server-side")())
        self.assertEqual((a["filename"], b["filename"]),
                         ("aerodrome-backup-20261022-120000.zip",
                          "aerodrome-backup-20261022-120000-2.zip"))


class TestRestoreConfigWriteOffTheLoop(_LockCase):
    """The restore's config.yaml writes take CONFIG_LOCK, which another
    thread (the collector's demo-port recovery) may hold across a `sudo
    tee`: on the event loop that froze every request. They run in the
    threadpool now."""

    def test_the_config_write_never_waits_on_the_loop(self):
        import asyncio
        import config_io
        b = _body(self.ep("/api/backup/create-server-side")())
        self.assertTrue(b["ok"], b)
        where = []
        real = config_io.write_text

        def spy(path, text, check=True):
            try:
                asyncio.get_running_loop()
                where.append("loop")
            except RuntimeError:
                where.append("thread")
            return real(path, text, check=check)
        model = _model(self.app, "/api/backup/restore-from-path")
        with mock.patch.object(config_io, "write_text", side_effect=spy):
            r = asyncio.run(self.ep("/api/backup/restore-from-path")(
                model(path=b["path"]), background_tasks=None))
        self.assertTrue(_body(r).get("ok"), _body(r))
        self.assertTrue(where)
        self.assertEqual(set(where), {"thread"})


class TestExport(_LockCase):
    """The browser backup download (GET /api/backup/export)."""

    def setUp(self):
        super().setUp()
        (self.inst / ".backups").mkdir(exist_ok=True)     # as on every real install

    def run_export(self, headers=(), spec="2.4", send_fails=False, disconnect=False):
        msgs, state = [], {}

        async def receive():
            if disconnect:
                return {"type": "http.disconnect"}
            await asyncio.sleep(3600)

        async def send(m):
            if m["type"] == "http.response.start":
                # The endpoint has returned: nothing may be left on disk.
                state["files_at_start"] = [f for f in _backup_files(self.inst)
                                           if not f.startswith("20")]
            if m["type"] == "http.response.body" and send_fails and m.get("body"):
                raise OSError(32, "Broken pipe")          # the client went away
            msgs.append(m)

        async def go():
            try:
                await asyncio.wait_for(self.app(_export_scope(headers, spec), receive, send), 30)
            except Exception as e:      # noqa: BLE001 - a dropped client may surface here
                state["raised"] = type(e).__name__
        asyncio.run(go())
        start = next((m for m in msgs if m["type"] == "http.response.start"), {})
        data = b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body")
        return start.get("status"), data, state

    def test_full_download_is_a_valid_backup_and_leaves_nothing_behind(self):
        status, data, state = self.run_export()
        self.assertEqual(status, 200, data[:300])
        self.assertEqual(state["files_at_start"], [])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertTrue({"manifest.json", "config.yaml", "aerodrome.db"} <= set(z.namelist()))
            self.assertIsNone(z.testzip())
        self.assertEqual(_fds_under(self.inst / ".backups"), [])
        self.assertIsNone(self.server._MAINT.holder())

    def test_a_dropped_download_closes_the_file_at_once_2_4_spec(self):
        before = _fds_under(self.inst / ".backups")
        status, data, state = self.run_export(send_fails=True)
        self.assertEqual(_fds_under(self.inst / ".backups"), before)
        self.assertEqual([f for f in _backup_files(self.inst) if not f.startswith("20")], [])

    def test_a_dropped_download_closes_the_file_at_once_2_3_spec(self):
        # Guard: Starlette already ends a 2.3-spec stream cleanly on a
        # disconnect; the 2.4-spec case above is the one that leaked.
        before = _fds_under(self.inst / ".backups")
        self.run_export(spec="2.3", disconnect=True)
        self.assertEqual(_fds_under(self.inst / ".backups"), before)
        self.assertEqual([f for f in _backup_files(self.inst) if not f.startswith("20")], [])

    def test_requests_from_other_websites_are_refused_before_building(self):
        for hdrs in ([(b"sec-fetch-site", b"cross-site")],
                     [(b"sec-fetch-site", b"same-site")],
                     [(b"origin", b"http://other.example.com")],
                     [(b"referer", b"http://other.example.com/page")],
                     [(b"origin", b"null")]):
            with self.subTest(headers=hdrs), \
                    mock.patch.object(zipfile, "ZipFile",
                                      side_effect=AssertionError("built a backup")):
                status, data, _ = self.run_export(hdrs)
                self.assertEqual(status, 403, data[:200])
                self.assertIn(b"other websites", data)

    def test_the_page_a_typed_url_and_curl_are_allowed(self):
        # Guard: the cross-site refusal never blocks the page, a typed URL or curl.
        for hdrs in ([(b"sec-fetch-site", b"same-origin"),
                      (b"referer", b"http://127.0.0.1:18299/config")],
                     [(b"sec-fetch-site", b"none")],
                     []):
            with self.subTest(headers=hdrs):
                status, data, _ = self.run_export(hdrs)
                self.assertEqual(status, 200, data[:200])

    def test_refused_while_another_maintenance_task_runs(self):
        tok = self.server._MAINT.try_acquire("an update")
        self.addCleanup(self.server._MAINT.release, tok)
        with mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("built")):
            status, data, _ = self.run_export()
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(data)["busy"]["op"], "an update")

    def test_refused_when_the_snapshot_and_zip_wouldnt_fit(self):
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 * 1024 * 1024)), \
                mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("built")):
            status, data, _ = self.run_export()
        self.assertEqual(status, 507)
        self.assertIn(b"Not enough free disk space", data)

    def test_one_at_a_time(self):
        gate, started = threading.Event(), threading.Event()
        real_write = zipfile.ZipFile.write

        def slow_write(zf, filename, arcname=None, *a, **kw):
            if arcname == "aerodrome.db":
                started.set()
                gate.wait(10)
            return real_write(zf, filename, arcname, *a, **kw)
        res = []
        with mock.patch.object(zipfile.ZipFile, "write", slow_write):
            t = threading.Thread(target=lambda: res.append(self.run_export()))
            t.start()
            self.assertTrue(started.wait(10))
            second = self.run_export()
            gate.set()
            t.join()
        self.assertEqual(second[0], 409)
        self.assertEqual(res[0][0], 200)


class TestStartupSweep(unittest.TestCase):
    """Leftovers of killed or crashed runs are removed at startup; backups,
    update snapshots, config copies and markers never are."""

    def test_removes_leftovers_and_keeps_everything_else(self):
        import server
        inst = Path(tempfile.mkdtemp(prefix="aerodrome-b4b-sweep-",
                                     dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, inst, True)
        b = inst / ".backups"
        removed = [b / ".tmp" / "export-x.zip", b / ".tmp" / "aerodrome-backup-1.zip.partial",
                   b / ".tmp" / "aerodrome-verify-abc" / "x", b / "tmpab12cd34.zip",
                   b / "tmpab12cd34.db", b / "tmpab12cd34.db-journal",
                   b / ".uploads" / "upload-1-2.zip", inst / ".config.yaml.ab12cd34.tmp",
                   # every other atomic write's temp file (v3.4.148 review):
                   inst / ".config.yaml.bak.20260101-000000.ab12cd34.tmp",
                   inst / ".config.yaml.bak.20260101-000000.pre-restore.ab12cd34.tmp",
                   b / ".config.yaml.last-good.ab12cd34.tmp",
                   b / ".config-recovery.json.ab12cd34.tmp",
                   b / ".config.yaml.broken.20260101-000000-2.ab12cd34.tmp",
                   b / ".aerodrome-backup-20260101-000000.zip.sha256.ab12cd34.tmp"]
        kept = [b / "aerodrome-backup-20260101-000000.zip",
                b / "aerodrome-backup-20260101-000000.zip.sha256",
                b / "20260101-000000" / "VERSION", b / "tmp-notes.zip",
                b / "config.yaml.last-good", b / "config.yaml.broken.20260101-000000",
                b / "config-recovery.json", inst / "config.yaml",
                inst / "config.yaml.bak.20260101-000000",
                # look-alikes that aren't atomic-write temp files
                inst / ".config.yaml.bak.20260101-000000.tmp", inst / ".other.yaml.ab12cd34.tmp",
                b / ".config.yaml.last-good.ab12cd34.tmp.keep", b / ".private-modes-applied"]
        for p in removed + kept:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 10)
        with self.assertLogs("adsb.server", "INFO") as logs:
            d = server.prepare_backup_tmp(inst)
        self.assertEqual(d, b / ".tmp")
        for p in removed:
            self.assertFalse(p.exists(), p)
        for p in kept:
            self.assertTrue(p.exists(), p)
        self.assertTrue(any("removed 14 leftover temporary file(s)" in m for m in logs.output),
                        logs.output)

    def test_a_config_write_temp_file_is_never_committed(self):
        # It is a full copy of config.yaml (receiver location, credentials):
        # a dev checkout must not pick it up with `git add -A`.
        import subprocess
        try:
            inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO,
                                    capture_output=True, text=True, timeout=30)
        except OSError:
            self.skipTest("git isn't installed")
        if inside.stdout.strip() != "true":
            self.skipTest("not a git checkout")
        for name in (".config.yaml.ab12cd34.tmp", ".config.yaml.bak.20260101-000000.ab12cd34.tmp",
                     ".config.yaml.bak.20260101-000000.pre-restore.ab12cd34.tmp"):
            with self.subTest(name=name):
                r = subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=REPO,
                                   timeout=30)
                self.assertEqual(r.returncode, 0, f"{name} isn't ignored by .gitignore")

    def test_real_killed_writes_are_swept(self):
        # A write killed between staging and the rename (power cut, kill -9)
        # leaves a full copy of config.yaml: last-good's and a .bak's temp
        # files used to stay for good.
        import server
        import subprocess
        import sys
        inst = Path(tempfile.mkdtemp(prefix="aerodrome-b4b-sweep-",
                                     dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, inst, True)
        (inst / "config.yaml").write_text((REPO / "config.yaml.example").read_text())
        child = (
            "import os, signal, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import config_io, pathlib\n"
            "os.replace = lambda *a: os.kill(os.getpid(), signal.SIGKILL)\n"
            "p = pathlib.Path(sys.argv[2])\n"
            "if sys.argv[3] == 'last-good':\n"
            "    config_io.write_last_good(p / 'config.yaml')\n"
            "else:\n"
            "    config_io.write_text(p / 'config.yaml.bak.20260101-000000',\n"
            "                         (p / 'config.yaml').read_text(), check=False)\n")
        for what in ("last-good", "bak"):
            r = subprocess.run([sys.executable, "-c", child, str(REPO), str(inst), what],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, -9, r.stderr)
        def left():
            return sorted(str(p.relative_to(inst)) for p in inst.rglob("*.tmp") if p.is_file())
        self.assertEqual(len(left()), 2, left())
        server.prepare_backup_tmp(inst)
        self.assertEqual(left(), [])


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
