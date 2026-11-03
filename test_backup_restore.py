"""Restoring a full backup can't leave Aerodrome unable to start (v3.4.149).

The old restore wrote the backup's config.yaml and database straight over
the live files while the collector and web requests used them: a damaged
backup, a full disk, a config from another machine or one that fails
validation left a restart loop with the web UI gone, or a start on an empty
history. Now a restore is checked and staged (nothing live changes), the
next start switches to it before any database connection exists, and a
restored backup that doesn't load is rolled back — by moving the
pre-restore copies back, which a rollback consumes.

Every install here is a throwaway directory (the server module's __file__
and main's paths point at it); nothing is written to the repo. sudo /
systemctl are intercepted (subprocess.run is mocked), restarts and re-execs
are mocked, and every "next start" of the startup half is a call of its
real code. Addresses are RFC 5737 examples; hexes and labels are invented.

Cross-filesystem cases (a database on another filesystem than the install
or config.yaml) run for real when a second filesystem is available:
AERODROME_TEST_XFS_DIR (else /dev/shm) must be on a different device than
the temp directory; otherwise they are skipped.

How the tests relate to the release before (3.4.148), where each was also
run (the base check):
  - endpoint/startup tests that fail there on a behaviour assertion name
    that defect in their comment ("3.4.148 wrote …");
  - many others stop earlier there, because what they exercise doesn't
    exist yet: no restore_io, no staged restore for setUp to make (the
    _Waiting classes), a marker/response field or an endpoint that isn't
    there. They are guards of the new mechanism — their evidence is the
    mutation check (each recovery branch reintroduced as a bug is caught),
    not the base;
  - the kill-point, cross-filesystem and unit classes are skipped there
    (no restore_io), and the few tests that pass there say "Guard".
The review-round classes at the end name what the review build of 3.4.149
did before their fix; those marked "Guard (coverage)" pin code that was
already right but untested — each catches its reintroduced bug.
"""
import asyncio
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

import atomic_io
import config_io
from test_update_apply import _tmpdir, repo_guard

try:
    import restore_io
except ImportError:            # the release before 3.4.149 (base check)
    restore_io = None

setUpModule, tearDownModule = repo_guard()

REPO = Path(__file__).resolve().parent
EXAMPLE = (REPO / "config.yaml.example").read_text()
MARKER = "pending-restore.json"
RESULT = "restore-result.json"
TMP = os.environ.get("TMPDIR") or None
needs_restore_io = unittest.skipIf(restore_io is None, "restore_io (v3.4.149) not present")
_REAL_RUN = subprocess.run          # the fixtures mock subprocess.run (sudo, systemctl)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def make_history_db(path, first_hex, rows):
    import collector
    from schema_migrations import apply_schema_migrations
    collector.init_db(str(path))
    conn = sqlite3.connect(str(path))
    apply_schema_migrations(conn, "test")
    conn.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
                     [(f"{first_hex + i:06X}", 1792600000 + i) for i in range(rows)])
    conn.commit()
    conn.close()                       # the last close checkpoints the WAL


def make_pre_v250_db(path, rows=3000, n_icao=40):
    """A v2.49-shape history (schema 0: no rollups, no meta, no
    schema_version) — what a backup from an old install holds."""
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA journal_mode=WAL")
    cols = ("icao TEXT NOT NULL, callsign TEXT DEFAULT '', speed REAL, lat REAL, lon REAL, "
            "altitude REAL, aircraft_type TEXT DEFAULT '', type_desc TEXT DEFAULT '', "
            "seen_at INTEGER NOT NULL")
    c.execute(f"CREATE TABLE all_sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
    c.execute(f"CREATE TABLE military_sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols}, "
              "special_label TEXT DEFAULT '')")
    c.execute(f"CREATE TABLE watchlist_sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              f"{cols}, watchlist_label TEXT DEFAULT '')")
    c.execute("CREATE TABLE seen_aircraft (icao TEXT PRIMARY KEY, first_seen_at INTEGER NOT "
              "NULL, first_callsign TEXT DEFAULT '', first_aircraft_type TEXT DEFAULT '')")
    now = int(time.time())
    c.executemany("INSERT INTO all_sightings (icao,callsign,speed,lat,lon,altitude,"
                  "aircraft_type,type_desc,seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  [(f"{0xF10000 + i % n_icao:06X}", "TST1", 300.0, 40.0, -90.0,
                    float(i * 37 % 9000), "B738", "Example Type", now - 5 * 86400 + i * 30)
                   for i in range(rows)])
    c.executemany("INSERT INTO military_sightings (icao,callsign,speed,lat,lon,altitude,"
                  "aircraft_type,type_desc,seen_at,special_label) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  [(f"{0xF10000 + i % n_icao:06X}", "TST1", 300.0, 40.0, -90.0, 1000.0,
                    "C130", "Example Type", now - 5 * 86400 + i * 30, "example-label")
                   for i in range(rows // 10)])
    c.executemany("INSERT INTO seen_aircraft VALUES (?,?,?,?)",
                  [(f"{0xF10000 + k:06X}", now - 6 * 86400, "TST1", "B738")
                   for k in range(n_icao)])
    c.commit()
    c.close()


def count_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT count(*), min(icao) FROM all_sightings").fetchone()
    finally:
        conn.close()


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def example_cfg(db_file, **web):
    cfg = yaml.safe_load(EXAMPLE)
    cfg["receiver"].update(ip="192.0.2.10", port=30005, poll_interval=5)
    cfg["web"].update(host="127.0.0.1", port=18299)
    cfg["web"].update(web)
    cfg["data"]["db_file"] = str(db_file)
    cfg["watchlist"] = [{"icao": "A0B1C2", "label": "invented one"}]
    # Every get_app starts a daemon update-check thread that outlives its
    # test: unless the interval is "never" it opens the GLOBAL config's
    # data.db_file each tick (even with checks disabled) — a later test's
    # database, which it recreates when that test has just moved it aside.
    cfg.setdefault("updates", {}).setdefault("github", {}).update(enabled=False,
                                                                  poll_interval="never")
    return cfg


def make_backup_zip(path, cfg_text=None, db_bytes=None, manifest_version=1, prefix="",
                    extra=None, manifest=None):
    with zipfile.ZipFile(str(path), "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(prefix + "manifest.json", manifest if manifest is not None else json.dumps({
            "manifest_version": manifest_version, "aerodrome_version": "3.4.140",
            "created_at_iso": "2026-10-01T00:00:00Z"}))
        if cfg_text is not None:
            zf.writestr(prefix + "config.yaml", cfg_text)
        if db_bytes is not None:
            zf.writestr(prefix + "aerodrome.db", db_bytes)
        for k, v in (extra or {}).items():
            zf.writestr(k, v)


def corrupt_member(zip_path, name):
    with zipfile.ZipFile(str(zip_path)) as zf:
        info = zf.getinfo(name)
    raw = bytearray(Path(zip_path).read_bytes())
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    pos = start + info.compress_size // 2
    for k in range(32):
        raw[pos + k] ^= 0xFF
    Path(zip_path).write_bytes(bytes(raw))


def _xfs_dir():
    """A directory on another filesystem than TMPDIR, or None."""
    here = os.stat(tempfile.gettempdir() if TMP is None else TMP).st_dev
    for cand in (os.environ.get("AERODROME_TEST_XFS_DIR"), "/dev/shm"):
        if cand and os.path.isdir(cand) and os.access(cand, os.W_OK):
            if os.stat(cand).st_dev != here:
                return cand
    return None


def _xfs_tmpdir(test):
    base = _xfs_dir()
    if base is None:
        test.skipTest("no second filesystem (set AERODROME_TEST_XFS_DIR)")
    d = Path(tempfile.mkdtemp(prefix="aerodrome-b4c-", dir=base))
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _call(r):
    """An endpoint's answer: awaited if it is a coroutine (the release before
    3.4.149 had async restore handlers), as (status, body)."""
    if asyncio.iscoroutine(r):
        r = asyncio.run(r)
    if isinstance(r, dict):
        return 200, r
    try:
        return r.status_code, json.loads(r.body)
    except (ValueError, AttributeError):
        return getattr(r, "status_code", 200), {}


def _json_request(body):
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "query_string": b""}, receive)


def _endpoint(app, path, method="POST"):
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))


class _Restore(unittest.TestCase):
    """A throwaway install running get_app(): config.yaml + .backups/ in
    <work>/install, the live database (30 rows from A00000) in db_dir()."""

    def db_dir(self):
        return self.inst

    def customize(self, cfg):
        pass

    def setUp(self):
        import server
        self.server = server
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.addCleanup(setattr, tempfile, "tempdir", tempfile.tempdir)
        self.work = _tmpdir(self)
        self.inst = self.work / "install"
        (self.inst / "static").mkdir(parents=True)
        (self.inst / ".backups").mkdir()
        self.live = Path(self.db_dir()) / "aircraft_history.db"
        make_history_db(self.live, 0xA00000, 30)
        self.cfg_path = self.inst / "config.yaml"
        self.cfg = example_cfg(self.live)
        self.customize(self.cfg)
        self.cfg_path.write_text("# my settings\n" + yaml.safe_dump(self.cfg, sort_keys=False))
        self.cfg_before = self.cfg_path.read_bytes()
        self.run_mock = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        for p in (mock.patch.object(server, "__file__", str(self.inst / "server.py")),
                  mock.patch("subprocess.run", self.run_mock)):
            p.start()
            self.addCleanup(p.stop)
        self.app = server.get_app(yaml.safe_load(self.cfg_before), str(self.cfg_path))
        src = self.work / "src.db"
        make_history_db(src, 0xB00000, 40)
        self.db_bytes = src.read_bytes()
        self.zip = self.inst / ".backups" / "aerodrome-backup-20261001-000000.zip"

    # --- helpers ---
    def backup_cfg(self, **web):
        cfg = example_cfg(self.live)
        cfg["receiver"]["ip"] = "192.0.2.44"            # the backup's own receiver
        cfg["web"].update(web)
        return cfg

    def backup_text(self, cfg=None):
        return "# backup settings\n" + yaml.safe_dump(cfg or self.backup_cfg(), sort_keys=False)

    def ep(self, path, method="POST"):
        return _endpoint(self.app, path, method)

    def restore(self, keep_current_config=False, path=None, bt=None):
        ep = self.ep("/api/backup/restore-from-path")
        model = ep.__annotations__["payload"]
        try:
            payload = model(path=str(path or self.zip), keep_current_config=keep_current_config)
        except TypeError:
            payload = model(path=str(path or self.zip))
        out = _call(ep(payload, background_tasks=bt))
        self.release_lock()
        return out

    def upload(self, data, keep_current_config=False):
        from starlette.datastructures import UploadFile
        ep = self.ep("/api/backup/import")
        up = UploadFile(file=io.BytesIO(data), filename="b.zip")
        kw = {"keep_current_config": keep_current_config} if keep_current_config else {}
        out = _call(ep(file=up, background_tasks=None, **kw))
        self.release_lock()
        return out

    def release_lock(self):
        # A restore keeps the maintenance lock until its (mocked) restart
        # lands; here the restart has "happened".
        self.server._MAINT = self.server._SingleRun()

    def restart_calls(self):
        return [c.args[0] for c in self.run_mock.call_args_list
                if c.args and "restart" in c.args[0] and "aerodrome" in c.args[0]]

    def marker(self):
        try:
            return json.loads((self.inst / ".backups" / MARKER).read_text())
        except (OSError, ValueError):
            return None

    def result(self):
        try:
            return json.loads((self.inst / ".backups" / RESULT).read_text())
        except (OSError, ValueError):
            return None

    def staged_files(self):
        out = []
        for d in {Path(self.db_dir()), self.inst}:
            out += sorted(p.name for p in d.glob(".*restore-staged*"))
        return out

    def leftovers(self):
        out = []
        for sub in (".tmp", ".uploads"):
            d = self.inst / ".backups" / sub
            if d.is_dir():
                out += [f"{sub}/{p.name}" for p in d.iterdir()]
        return out

    def assert_untouched(self):
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before, "config.yaml changed")
        self.assertEqual(count_rows(self.live), (30, "A00000"), "the live database changed")
        self.assertIsNone(self.marker())
        self.assertEqual(self.staged_files(), [])
        self.assertEqual(self.leftovers(), [])

    def assert_staged_not_live(self):
        """Live files untouched, a staged restore waiting."""
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before, "config.yaml changed")
        self.assertEqual(count_rows(self.live), (30, "A00000"), "the live database changed")
        m = self.marker()
        self.assertIsNotNone(m, "no restore was staged")
        self.assertEqual(m["phase"], "staged")
        return m

    def start_half(self):
        """What the next start does with the marker (restore_io), then the
        restored data 'loads': finish."""
        state, m = restore_io.apply_pending_restore(self.inst, log=lambda *_: None)
        if state in ("applied", "retry"):
            restore_io.finish_pending_restore(self.inst, m, log=lambda *_: None)
        return state


# ===========================================================================
# The restore endpoints (each names what 3.4.148 did; see the module
# docstring for how each fails there)
# ===========================================================================

class TestRestoreIsStagedNotLive(_Restore):

    def test_good_backup_is_staged_live_files_untouched_restart_requested(self):
        # 3.4.148 wrote config.yaml and the database while Aerodrome ran.
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.restart_calls()), 1)
        m = self.assert_staged_not_live()
        self.assertEqual(count_rows(m["db"]["staged"]), (40, "B00000"))
        self.assertEqual(count_rows(m["db"]["snapshot"]), (30, "A00000"))
        self.assertEqual(body["staged"], True)
        # the running settings are untouched too
        self.assertEqual(self.server.CONFIG["receiver"]["ip"], "192.0.2.10")
        # ... and the next start switches to it
        self.assertEqual(self.start_half(), "applied")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertIn("# backup settings", self.cfg_path.read_text())
        self.assertEqual(yaml.safe_load(self.cfg_path.read_text())["receiver"]["ip"], "192.0.2.44")
        self.assertTrue(self.result()["ok"])
        self.assertEqual(self.staged_files(), [])

    def test_damaged_database_changes_nothing_and_does_not_restart(self):
        # 3.4.148 kept the restored config.yaml, skipped the database, and
        # restarted anyway — a half restore.
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        corrupt_member(self.zip, "aerodrome.db")
        status, body = self.restore()
        self.assertEqual(status, 400, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], body["message"])
        self.assertEqual(self.restart_calls(), [])
        self.assert_untouched()

    def test_not_a_sqlite_database(self):
        make_backup_zip(self.zip, self.backup_text(), b"not a database\n" * 4096)
        status, body = self.restore()
        self.assertEqual(status, 400, body)
        self.assertIn("isn't a SQLite database", body["message"])
        self.assertEqual(self.restart_calls(), [])
        self.assert_untouched()

    def test_truncated_zip_changes_nothing(self):
        # Guard (3.4.148 refused it too: no central directory, BadZipFile).
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        data = self.zip.read_bytes()
        self.zip.write_bytes(data[: len(data) * 2 // 3])
        status, body = self.restore()
        self.assertEqual(status, 400, body)
        self.assertEqual(self.restart_calls(), [])
        self.assert_untouched()

    def test_newer_schema_is_409_update_first(self):
        # 3.4.148 restored it; the next start then ran on a schema it didn't know.
        from schema_migrations import CURRENT_SCHEMA_VERSION
        src = self.work / "newer.db"
        make_history_db(src, 0xC00000, 5)
        c = sqlite3.connect(str(src))
        c.execute("INSERT INTO schema_version VALUES (?, 0, 'from a future release', '9.9.9')",
                  (CURRENT_SCHEMA_VERSION + 1,))
        c.commit()
        c.close()
        make_backup_zip(self.zip, self.backup_text(), src.read_bytes())
        status, body = self.restore()
        self.assertEqual(status, 409, body)
        self.assertIn("Update Aerodrome first", body["message"])
        self.assertEqual(self.restart_calls(), [])
        self.assert_untouched()

    def test_foreign_sqlite_file_is_refused(self):
        src = self.work / "notes.db"
        c = sqlite3.connect(str(src))
        c.execute("CREATE TABLE notes (x)")
        c.commit()
        c.close()
        make_backup_zip(self.zip, self.backup_text(), src.read_bytes())
        status, body = self.restore()
        self.assertEqual(status, 400, body)
        self.assertIn("all_sightings", body["message"])
        self.assert_untouched()

    def test_this_machines_address_port_and_database_are_kept_with_notes(self):
        # 3.4.148 restored another machine's web.host (a bind failure and a
        # restart loop) and data.db_file (an empty history).
        theirs = self.backup_cfg(host="192.0.2.77", port=8080)
        theirs["data"]["db_file"] = "/srv/other-box/history.db"
        make_backup_zip(self.zip, self.backup_text(theirs), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["notes"]), 3, body["notes"])
        self.assertTrue(any("web.host" in n and "192.0.2.77" in n for n in body["notes"]))
        self.assert_staged_not_live()
        staged = Path(self.marker()["config"]["staged"]).read_text()
        cfg = yaml.safe_load(staged)
        self.assertEqual((cfg["web"]["host"], cfg["web"]["port"], cfg["data"]["db_file"]),
                         ("127.0.0.1", 18299, str(self.live)))
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.44")      # the rest is the backup's
        self.assertIn("# backup settings", staged)                 # comments kept
        self.start_half()
        self.assertEqual(count_rows(self.live), (40, "B00000"))   # restored into the live path
        self.assertEqual(self.result()["notes"], body["notes"])

    def test_invalid_settings_are_422_with_errors_then_database_only_works(self):
        # 3.4.148 wrote a config.yaml the validator rejects (a restart loop).
        bad = self.backup_cfg()
        bad["receiver"]["port"] = 99999
        make_backup_zip(self.zip, self.backup_text(bad), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 422, body)
        self.assertIn("receiver.port", [e["path"] for e in body["errors"]])
        self.assertTrue(body["database_only"])
        self.assertEqual(self.restart_calls(), [])
        self.assert_untouched()
        status, body = self.restore(keep_current_config=True)
        self.assertEqual(status, 200, body)
        self.assertIn("config.yaml", [s["file"] for s in body["skipped"]])
        m = self.assert_staged_not_live()
        self.assertEqual(m["config"], {})
        self.start_half()
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)

    def test_unreadable_settings_are_422(self):
        for text in ("web: [unclosed\n", "- just\n- a list\n",
                     yaml.safe_dump({k: v for k, v in self.backup_cfg().items()
                                     if k != "retention"})):
            with self.subTest(text=text[:20]):
                make_backup_zip(self.zip, text, self.db_bytes)
                status, body = self.restore()
                self.assertEqual(status, 422, body)
                self.assert_untouched()

    def test_upload_with_database_only_query_parameter(self):
        bad = self.backup_cfg()
        bad["receiver"]["port"] = 99999
        make_backup_zip(self.zip, self.backup_text(bad), self.db_bytes)
        status, body = self.upload(self.zip.read_bytes())
        self.assertEqual(status, 422, body)
        self.assert_untouched()
        status, body = self.upload(self.zip.read_bytes(), keep_current_config=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.marker()["config"], {})
        self.assertEqual(self.leftovers(), [])       # the upload's copy is gone

    def test_wrapper_folder_backup(self):
        # Guard (v2.49.8: cloud sync tools add a folder level).
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes,
                        prefix="aerodrome-backup-20261001-000000/")
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assert_staged_not_live()

    def test_backup_with_only_ntfy_settings_needs_no_restart(self):
        # 3.4.148 restarted Aerodrome for a backup holding nothing of its own.
        make_backup_zip(self.zip, extra={"ntfy/server.yml": "base-url: \"http://192.0.2.10:2586\"\n"})
        with mock.patch("ntfy_installer.install_status", return_value={"state": "external"}):
            status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertFalse(body["staged"])
        self.assertEqual(self.restart_calls(), [])
        self.assertIsNone(self.marker())

    def test_empty_backup_is_refused(self):
        make_backup_zip(self.zip)
        status, body = self.restore()
        self.assertEqual(status, 400, body)
        self.assertIn("Nothing to restore", body["message"])
        self.assert_untouched()

    def test_temporary_files_are_not_restore_sources(self):
        # 3.4.148 restored from a spool or staged upload in .backups/.tmp.
        tmpd = self.inst / ".backups" / ".tmp"
        tmpd.mkdir(exist_ok=True)
        z = tmpd / "upload-x.zip"
        make_backup_zip(z, self.backup_text(), self.db_bytes)
        status, body = self.restore(path=z)
        self.assertEqual(status, 403, body)
        z.unlink()
        self.assert_untouched()

    def test_restart_refused_leaves_it_staged_and_says_so(self):
        # 3.4.148: "Restore applied but restart failed" — with the files
        # already replaced under the running process.
        self.run_mock.return_value = subprocess.CompletedProcess(
            [], 1, "", "sudo: a password is required")
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        ep = self.ep("/api/backup/restore-from-path")
        model = ep.__annotations__["payload"]
        status, body = _call(ep(model(path=str(self.zip)), background_tasks=None))
        self.assertEqual(status, 200, body)
        self.assertIn("staged and applies the next time Aerodrome starts within the hour",
                      body["restart_note"])
        m = self.assert_staged_not_live()
        self.assertIn("password is required", m["restart_failed"])
        # The lock is free at once: Cancel works right away.
        self.assertIsNone(self.server._MAINT.holder())
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertEqual(st["restore"]["pending"]["restart_failed"], m["restart_failed"])
        self.assertTrue(st["restore"]["pending"]["waiting_for_restart"])

    def test_background_restart_refused_frees_the_lock_and_records_it(self):
        # The page's own path: the restart runs after the response.
        self.run_mock.return_value = subprocess.CompletedProcess(
            [], 1, "", "sudo: a password is required")
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        from starlette.background import BackgroundTasks
        bt = BackgroundTasks()
        ep = self.ep("/api/backup/restore-from-path")
        model = ep.__annotations__["payload"]
        status, body = _call(ep(model(path=str(self.zip)), background_tasks=bt))
        self.assertEqual(status, 200, body)
        self.assertIsNotNone(self.server._MAINT.holder())       # parked for the restart
        with mock.patch("time.sleep"):
            asyncio.run(bt())
        self.assertIsNone(self.server._MAINT.holder())
        self.assertIn("password is required", self.marker()["restart_failed"])

    def test_restore_status_in_maintenance_status(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.restore()
        st = self.ep("/api/maintenance/status", "GET")()
        p = st["restore"]["pending"]
        self.assertEqual((p["phase"], p["source"]), ("staged", "server-side-path"))
        self.assertAlmostEqual(p["expires_at"] - p["staged_at"], 3600)
        self.start_half()
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertIsNone(st["restore"]["pending"])
        self.assertTrue(st["restore"]["result"]["ok"])
        r = self.ep("/api/maintenance/ack")(self.ep("/api/maintenance/ack").__annotations__[
            "body"](kind="restore"))
        self.assertEqual(r, {"ok": True, "acknowledged": True})
        self.assertTrue(self.result()["acknowledged"])


class TestPrivateModes(_Restore):
    """B4b's private-files rule for what a restore writes (umask 0022):
    config.yaml's staged copy and pre-restore copy and the marker are 0600
    (they hold the receiver's location, API keys, notification credentials);
    the database's staged file and pre-restore copy take the live database's
    own mode (database files are left as the install has them). 3.4.148 wrote
    the pre-restore copies with shutil.copy2 and the restored files in place."""

    def test_modes(self):
        os.chmod(self.live, 0o640)
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        m = self.marker()

        def mode(p):
            return stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode(m["config"]["staged"]), 0o600)
        self.assertEqual(mode(m["config"]["snapshot"]), 0o600)
        self.assertEqual(mode(self.inst / ".backups" / MARKER), 0o600)
        self.assertEqual(mode(m["db"]["staged"]), 0o640)
        self.assertEqual(mode(m["db"]["snapshot"]), 0o640)
        self.start_half()
        self.assertEqual(mode(self.cfg_path), 0o600)
        self.assertEqual(mode(self.live), 0o640)
        self.assertEqual(mode(self.inst / ".backups" / RESULT), 0o600)


class TestDiskFull(_Restore):
    """A real short write: the restore runs in a child process whose
    file-size limit (RLIMIT_FSIZE) makes the write of the restored database
    fail part-way — the kernel-level shape of a full disk. 3.4.148 wrote the
    live database in place: it was left cut short."""

    CHILD = r"""
import asyncio, json, os, resource, signal, subprocess, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
work, limit = Path(sys.argv[2]), int(sys.argv[3])
import yaml, server
inst = work / "install"
cfg_path = inst / "config.yaml"
with mock.patch.object(server, "__file__", str(inst / "server.py")), \
        mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
    app = server.get_app(yaml.safe_load(cfg_path.read_text()), str(cfg_path))
    ep = next(r.endpoint for r in app.routes
              if getattr(r, "path", "") == "/api/backup/restore-from-path")
    model = ep.__annotations__["payload"]
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))
    try:
        r = ep(model(path=sys.argv[4]), background_tasks=None)
        if asyncio.iscoroutine(r):
            r = asyncio.run(r)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
    body = r if isinstance(r, dict) else json.loads(r.body)
    status = 200 if isinstance(r, dict) else r.status_code
    print("RESULT " + json.dumps({"status": status, "body": body,
                                  "restarted": any("restart" in str(c) for c in run.call_args_list)}))
"""

    def test_a_disk_that_fills_while_unpacking_changes_nothing(self):
        src = self.work / "big.db"
        make_history_db(src, 0xD00000, 4000)
        make_backup_zip(self.zip, self.backup_text(), src.read_bytes())
        r = _REAL_RUN([sys.executable, "-c", self.CHILD, str(REPO), str(self.work),
                            str(64 * 1024), str(self.zip)],
                           capture_output=True, text=True, timeout=120, cwd=str(self.work))
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, r.stdout[-1500:] + r.stderr[-3000:])
        res = json.loads(line[len("RESULT "):])
        self.assertEqual(res["status"], 507, res)
        self.assertFalse(res["restarted"])
        self.assertIn("disk", res["body"]["message"])
        self.assert_untouched()


class TestUploadLeavesNothingBehind(_Restore):
    """#23b: a refused upload's copy stayed in .backups/.uploads/ for good."""

    def bad_zips(self):
        good_cfg = self.backup_text()

        def z(**kw):
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w") as zf:
                for name, data in kw.items():
                    zf.writestr(name.replace("__", "/"), data)
            return b.getvalue()
        return {
            "no manifest": z(**{"config.yaml": good_cfg}),
            "two manifests": z(**{"manifest.json": '{"manifest_version": 1}',
                                  "a__manifest.json": '{"manifest_version": 1}'}),
            "manifest over 64 KB": z(**{"manifest.json": "{" + " " * (70 * 1024) + "}"}),
            "bad JSON": z(**{"manifest.json": "{not json"}),
            "manifest_version 2": z(**{"manifest.json": '{"manifest_version": 2}',
                                       "config.yaml": good_cfg}),
            "not a zip": b"this is not a zip file\n" * 100,
        }

    def test_six_malformed_backups_each_leave_nothing(self):
        for label, data in self.bad_zips().items():
            with self.subTest(label):
                status, body = self.upload(data)
                self.assertEqual(status, 400, (label, body))
                self.assertFalse(body["ok"])
                self.assertEqual(self.restart_calls(), [])
                self.assert_untouched()

    def test_a_good_upload_leaves_no_copy_either(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        status, body = self.upload(self.zip.read_bytes())
        self.assertEqual(status, 200, body)
        self.assertEqual(self.leftovers(), [])
        self.assert_staged_not_live()


# ===========================================================================
# While a restore waits for its restart
# ===========================================================================

class _Waiting(_Restore):
    """A restore staged whose restart call was refused: it waits."""

    def setUp(self):
        super().setUp()
        self.run_mock.return_value = subprocess.CompletedProcess(
            [], 1, "", "sudo: a password is required")
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        ep = self.ep("/api/backup/restore-from-path")
        status, body = _call(ep(ep.__annotations__["payload"](path=str(self.zip)),
                                background_tasks=None))
        self.assertEqual(status, 200, body)
        self.release_lock()
        self.run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.assertIsNotNone(self.marker())


class TestSettingsWhileStaged(_Waiting):
    """C1-6: the restart would put the backup's settings over a change made
    meanwhile, without a word. 3.4.148 had no staged restore (its restore
    wrote config.yaml at once)."""

    def writes(self):
        import server
        new = json.loads(json.dumps(self.server.CONFIG))
        new["display"]["date_format"] = "DMY"
        put = self.ep("/api/config", "PUT")
        add, rem = self.ep("/api/watchlist/add"), self.ep("/api/watchlist/remove")
        wl = add.__annotations__["entry"]
        from starlette.datastructures import UploadFile
        imp = self.ep("/api/config/import")
        (self.inst / "config.yaml.bak.20260101-000000").write_text(self.cfg_before.decode())
        return {
            "PUT /api/config": lambda: put(put.__annotations__["payload"](config=new)),
            "watchlist add": lambda: add(wl(identifier="A1B2C3", id_type="icao", label="x")),
            "watchlist remove": lambda: rem(wl(identifier="A0B1C2", id_type="icao")),
            "config import": lambda: imp(file=UploadFile(file=io.BytesIO(self.cfg_before),
                                                         filename="config.yaml")),
            "config restore": lambda: self.ep("/api/config/restore/{name}")(
                name="config.yaml.bak.20260101-000000"),
        }

    def test_each_settings_write_is_409_and_config_yaml_byte_identical(self):
        for label, call in self.writes().items():
            with self.subTest(label):
                status, body = _call(call())
                self.assertEqual(status, 409, (label, body))
                self.assertEqual(body["code"], "restore_pending")
                self.assertEqual(body["message"],
                                 "A restore is waiting for the restart — restart Aerodrome "
                                 "to finish it, or cancel it in Settings.")
                self.assertEqual(body["error"], body["message"])
                self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
                self.release_lock()

    def test_after_cancel_they_work(self):
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual((status, body["cancelled"]), (200, True), body)
        for label, call in self.writes().items():
            with self.subTest(label):
                status, body = _call(call())
                self.assertEqual(status, 200, (label, body))
                self.release_lock()


class TestMaintenanceWhileStaged(_Waiting):
    """Every maintenance operation is refused while a restore waits (it
    would land under the swap, or delete the copy a rollback needs) — except
    Cancel, the way out. The restart itself is allowed."""

    def test_maintenance_endpoints_are_409(self):
        from starlette.datastructures import UploadFile
        snap_ts = Path(self.marker()["db"]["snapshot"]).name.split(".bak.")[1][:15]
        calls = {
            "purge": lambda: self.ep("/api/backup/pre-restore/purge")(),
            "server-side delete": lambda: self.ep("/api/backup/server-side/{filename}",
                                                  "DELETE")(filename=self.zip.name),
            "server-side backup": lambda: self.ep("/api/backup/create-server-side")(),
            "another restore": lambda: self.ep("/api/backup/restore-from-path")(
                self.ep("/api/backup/restore-from-path").__annotations__["payload"](
                    path=str(self.zip)), background_tasks=None),
            "put back": lambda: self.ep("/api/backup/pre-restore/{ts}/restore")(
                ts=snap_ts, background_tasks=None),
            "update upload": lambda: self.ep("/api/updates/local/upload")(
                file=UploadFile(file=io.BytesIO(b"PK"), filename="u.zip")),
            "update apply": lambda: self.ep("/api/updates/local/apply")(),
            "switch to real data": lambda: self.ep("/api/setup/switch-to-real")(
                _json_request({})),
        }
        snaps_before = sorted(p.name for p in self.inst.glob("*.pre-restore"))
        for label, call in calls.items():
            with self.subTest(label):
                status, body = _call(call())
                self.assertEqual(status, 409, (label, body))
                self.assertEqual(body["code"], "restore_pending", (label, body))
                self.release_lock()
        self.assertEqual(sorted(p.name for p in self.inst.glob("*.pre-restore")), snaps_before)
        self.assertTrue(self.zip.exists())

    def test_restart_is_allowed_and_cancel_is_the_way_out(self):
        status, body = _call(self.ep("/api/restart")())
        self.assertEqual(status, 200, body)
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual(status, 200, body)

    def test_a_large_upload_is_refused_before_its_body(self):
        mw = self.server._BodySizeLimitMiddleware(None)
        status, body = mw._large_upload_refusal("/api/backup/import", 10 * 1024 * 1024)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["code"], "restore_pending")


class TestCancel(_Waiting):

    def test_cancel_removes_the_staged_files_and_marker(self):
        snap = Path(self.marker()["db"]["snapshot"])
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["cancelled"])
        self.assertIsNone(self.marker())
        self.assertEqual(self.staged_files(), [])
        self.assertTrue(self.result()["cancelled"])
        self.assertTrue(snap.exists())                # the copy it took stays listed
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        # nothing left to cancel
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual((status, body["cancelled"]), (200, False))

    def test_a_swapped_restore_cant_be_cancelled(self):
        m = self.marker()
        m["phase"] = "swapped"
        (self.inst / ".backups" / MARKER).write_text(json.dumps(m))
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual(status, 409, body)
        self.assertEqual(self.marker()["phase"], "swapped")


class TestExpiry(_Waiting):
    """A restore whose restart never came is dropped after an hour (nothing
    live changes), and no longer blocks anything."""

    def age(self, sec):
        m = self.marker()
        m["staged_at"] = time.time() - sec
        (self.inst / ".backups" / MARKER).write_text(json.dumps(m))

    def test_expired_at_startup_is_discarded(self):
        self.age(3601)
        state, _m = restore_io.apply_pending_restore(self.inst, log=lambda *_: None)
        self.assertEqual(state, "expired")
        self.assertIsNone(self.marker())
        self.assertEqual(self.staged_files(), [])
        self.assertTrue(self.result()["expired"])
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)

    def test_expired_no_longer_blocks_maintenance_and_shows_in_status(self):
        self.age(3601)
        self.assertIsNone(restore_io.pending(self.inst))
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertIsNone(st["restore"]["pending"])
        self.assertTrue(st["restore"]["result"]["expired"])
        self.assertEqual(self.staged_files(), [])
        new = json.loads(json.dumps(self.server.CONFIG))
        put = self.ep("/api/config", "PUT")
        status, body = _call(put(put.__annotations__["payload"](config=new)))
        self.assertEqual(status, 200, body)

    def test_not_yet_expired_still_waits(self):
        self.age(3500)
        self.assertIsNotNone(restore_io.pending(self.inst))

    def test_a_clock_that_jumped_back_doesnt_keep_it_forever(self):
        self.age(-7200)
        self.assertIsNone(restore_io.pending(self.inst))


# ===========================================================================
# Pre-restore copies: listed and pruned beside the database; marker-named
# ones are never pruned or purged; a rollback consumes them
# ===========================================================================

class TestCopiesLiveBesideTheDatabase(_Restore):
    """With data.db_file outside the install, 3.4.148 put the database's
    pre-restore copy beside it, but listed and pruned only the install
    folder: the copies piled up unseen."""

    def db_dir(self):
        d = self.work / "elsewhere"
        d.mkdir(exist_ok=True)
        return d

    def test_listed_and_pruned_in_the_databases_folder(self):
        for i in range(5):
            ts = f"2026010{i + 1}-000000"
            (self.db_dir() / f"aircraft_history.db.bak.{ts}.pre-restore").write_bytes(b"x")
            (self.inst / f"config.yaml.bak.{ts}.pre-restore").write_text("x")
        listed = _call(self.ep("/api/backup/pre-restore", "GET")())[1]
        kinds = {f["kind"] for s in listed["snapshots"] for f in s["files"]}
        self.assertEqual(kinds, {"database", "config"})
        self.assertEqual(listed["count"], 5)
        self.server._prune_pre_restore_snapshots()
        self.assertEqual(len(list(self.db_dir().glob("*.pre-restore"))), 3)
        self.assertEqual(len(list(self.inst.glob("*.pre-restore"))), 3)

    def test_a_restore_keeps_the_database_copy_beside_the_database(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        m = self.marker()
        self.assertEqual(Path(m["db"]["snapshot"]).parent, self.db_dir())
        self.assertEqual(Path(m["db"]["staged"]).parent, self.db_dir())
        self.assertEqual(Path(m["config"]["snapshot"]).parent, self.inst)
        listed = _call(self.ep("/api/backup/pre-restore", "GET")())[1]
        self.assertEqual(len(listed["snapshots"]), 1)
        self.assertTrue(listed["snapshots"][0]["in_use"])


class TestMarkerNamedCopiesAreKept(_Waiting):
    """C0-4: a purge or prune while a restore waits deleted the copy its
    rollback needs (3.4.148 had no marker to protect)."""

    def test_prune_and_purge_skip_the_marker_named_pair(self):
        m = self.marker()
        names = {Path(m["db"]["snapshot"]).name, Path(m["config"]["snapshot"]).name}
        # four newer pairs: the marker's pair is the oldest of five
        for i in range(4):
            ts = f"2099010{i + 1}-000000"
            (self.inst / f"aircraft_history.db.bak.{ts}.pre-restore").write_bytes(b"x")
            (self.inst / f"config.yaml.bak.{ts}.pre-restore").write_text("x")
        self.server._prune_pre_restore_snapshots()             # startup / after-restore prune
        left = {p.name for p in self.inst.glob("*.pre-restore")}
        self.assertTrue(names <= left, left)
        self.server._purge_all_pre_restore_snapshots()         # the purge (keep 0)
        self.assertEqual({p.name for p in self.inst.glob("*.pre-restore")}, names)

    def test_startup_prune_in_get_app_skips_it(self):
        m = self.marker()
        for i in range(4):
            ts = f"2099010{i + 1}-000000"
            (self.inst / f"aircraft_history.db.bak.{ts}.pre-restore").write_bytes(b"x")
        self.server.get_app(yaml.safe_load(self.cfg_before), str(self.cfg_path))
        self.assertTrue(Path(m["db"]["snapshot"]).exists())

    def test_startup_housekeeping_keeps_the_staged_restore(self):
        # Guard: B4b's startup sweep (spool folder, temp files) and the
        # one-time permission tightening never touch a staged restore.
        import main
        m = self.marker()
        before = {p: sha(p) for p in (m["db"]["staged"], m["db"]["snapshot"],
                                      m["config"]["staged"], m["config"]["snapshot"])}
        with contextlib.redirect_stdout(io.StringIO()):
            main._prepare_install_files(self.inst)
        self.assertEqual({p: sha(p) for p in before}, before)
        self.assertIsNotNone(self.marker())


class TestRollbackConsumesTheCopy(_Restore):
    """C1-5: the prototype hard-linked the copy onto the live path, so the
    'copy' changed with every later write; and C0-4: with the copy purged it
    reported 'rolled back' all the same."""

    def stage_and_swap(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        state, m = restore_io.apply_pending_restore(self.inst, log=lambda *_: None)
        self.assertEqual(state, "applied")
        return m

    def test_no_listed_copy_aliases_the_live_database(self):
        # an older pair from an earlier restore stays listed
        older = self.inst / "aircraft_history.db.bak.20260101-000000.pre-restore"
        shutil.copy(self.live, older)
        m = self.stage_and_swap()
        restore_io.request_rollback(self.inst, m, "simulated: migration failed")
        restore_io.apply_pending_restore(self.inst, log=lambda *_: None)
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertFalse(Path(m["db"]["snapshot"]).exists(), "the copy was not consumed")
        listed = [p for p in self.inst.glob("*.pre-restore")]
        live_ino = (os.stat(self.live).st_dev, os.stat(self.live).st_ino)
        hashes = {p: sha(p) for p in listed}
        for p in listed:
            self.assertNotEqual((os.stat(p).st_dev, os.stat(p).st_ino), live_ino, p)
        c = sqlite3.connect(str(self.live))
        c.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
                      [(f"{0xE00000 + i:06X}", 1792700000) for i in range(500)])
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        self.assertEqual({p: sha(p) for p in listed}, hashes)
        res = self.result()
        self.assertTrue(res["rolled_back"])
        self.assertIn("migration failed", res["error"])

    def test_a_purged_copy_is_reported_never_claimed(self):
        m = self.stage_and_swap()
        os.unlink(m["db"]["snapshot"])
        restore_io.request_rollback(self.inst, m, "simulated: migration failed")
        restore_io.apply_pending_restore(self.inst, log=lambda *_: None)
        res = self.result()
        self.assertFalse(res["rolled_back"])
        self.assertEqual(res["missing"], [Path(m["db"]["snapshot"]).name])
        self.assertIn("moved aside", res["manual"])
        self.assertIsNone(self.marker())
        # the restored database was set aside: the next start runs on a fresh one
        self.assertFalse(self.live.exists())
        aside = self.inst / res["set_aside"]
        self.assertEqual(count_rows(aside), (40, "B00000"))
        # config.yaml (its copy was there) is back
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertFalse(st["restore"]["result"]["rolled_back"])
        self.assertEqual(st["restore"]["result"]["missing"], res["missing"])


# ===========================================================================
# Put this back (Decision 5a)
# ===========================================================================

class TestPutBack(_Restore):
    """config.html promised "a one-click undo" for the pre-restore copies;
    3.4.148 had no such button or endpoint."""

    def test_put_back_stages_the_copy_and_keeps_it_listed(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.restore()
        m1 = self.marker()
        ts = m1["ts"]
        self.start_half()                              # running on the restored backup
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=ts, background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 200, body)
        m2 = self.marker()
        self.assertEqual((m2["phase"], m2["kind"]), ("staged", "put_back"))
        self.assertEqual(count_rows(m2["db"]["staged"]), (30, "A00000"))
        self.assertNotEqual(m2["ts"], ts)
        # the copy put back stays listed, and is never an alias of the staged file
        self.assertTrue(Path(m1["db"]["snapshot"]).exists())
        self.assertNotEqual(os.stat(m1["db"]["snapshot"]).st_ino,
                            os.stat(m2["db"]["staged"]).st_ino)
        self.assertEqual(len(self.restart_calls()), 2)
        self.start_half()
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(yaml.safe_load(self.cfg_path.read_text()),
                         yaml.safe_load(self.cfg_before))
        # ... and the restored data it replaced was kept the same way
        self.assertEqual(count_rows(m2["db"]["snapshot"]), (40, "B00000"))
        self.assertEqual(self.result()["kind"], "put_back")

    def test_unknown_or_bad_name(self):
        status, _b = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts="20990101-000000", background_tasks=None))
        self.assertEqual(status, 404)
        status, _b = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts="../../etc", background_tasks=None))
        self.assertEqual(status, 400)


# ===========================================================================
# Restores and updates never meet at the same start
# ===========================================================================

class TestRestoreAndUpdateJournal(_Restore):

    def journal(self, state="pending_first_boot"):
        (self.inst / ".update-pending.json").write_text(json.dumps({
            "journal_version": 1, "state": state, "from_version": "3.4.148",
            "to_version": "3.4.149", "ts": "20261022-120000", "staging_dir":
            ".update-staging-20261022-120000", "old_dir": ".update-old-20261022-120000",
            "entries": ["main.py"], "had_old": ["main.py"], "failed_starts": 0}))

    def test_a_restore_is_refused_while_an_update_is_being_confirmed(self):
        # 3.4.148 allowed it: a rollback of the update would then start the
        # previous release on the restored data (or put the pre-update
        # config.yaml over the restored one).
        self.journal()
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "update_pending")
        self.assert_untouched()

    def test_a_start_with_both_lets_the_update_go_first(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        self.journal()
        state, _m = restore_io.apply_pending_restore(self.inst, log=lambda *_: None,
                                                     update_pending=True)
        self.assertEqual(state, "deferred")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
        self.assertEqual(self.marker()["phase"], "staged")
        # the update confirmed: the next start applies the restore
        (self.inst / ".update-pending.json").unlink()
        self.assertEqual(self.start_half(), "applied")
        self.assertEqual(count_rows(self.live), (40, "B00000"))


# ===========================================================================
# The startup half (main.py)
# ===========================================================================

class _Stop(Exception):
    pass


class _Exec(Exception):
    pass


class _MainRestore(_Restore):
    """main.start in-process on the throwaway install, with the real
    restore code; the web server, the collector thread and re-exec mocked."""

    def setUp(self):
        super().setUp()
        import main
        self.main = main
        for p in (mock.patch.object(main, "CONFIG_PATH", self.cfg_path),
                  mock.patch.object(main, "BASE_DIR", self.inst),
                  mock.patch.object(main, "PID_FILE", self.inst / ".tracker.pid"),
                  mock.patch.object(main, "CONFIG_EXAMPLE_PATH", REPO / "config.yaml.example")):
            p.start()
            self.addCleanup(p.stop)
        self.execs = []

    def start(self, bind=lambda h, p: (True, ""), extra=(), real_db=True):
        def execv(exe, argv):
            self.execs.append(argv)
            raise _Exec()
        cfg = self.main.load_config("start")
        patches = [mock.patch.object(self.main, "run_collector", lambda *a, **k: None),
                   mock.patch.object(self.main, "get_app", return_value=object()),
                   mock.patch.object(self.main, "_preflight_bind_check", side_effect=bind),
                   mock.patch.object(self.main.signal, "signal"),
                   mock.patch.object(self.main.uvicorn, "run", side_effect=_Stop),
                   mock.patch.object(self.main.os, "execv", side_effect=execv)]
        if not real_db:
            patches += [mock.patch.object(self.main, "init_db"),
                        mock.patch("collector.rollup_backfill_incomplete", return_value=[]),
                        mock.patch("schema_migrations.apply_schema_migrations",
                                   return_value={"ok": True, "applied": [],
                                                 "starting_version": 18})]
        with contextlib.ExitStack() as st:
            for p in list(patches) + list(extra):
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            try:
                self.main.start(cfg)
                return "returned"
            except _Stop:
                return "served"
            except _Exec:
                return "reexec"
            except SystemExit as e:
                return f"exit {e.code}"

    def stage(self, text=None, db_bytes=None):
        make_backup_zip(self.zip, text or self.backup_text(), db_bytes or self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)

    def last_good(self):
        return (self.inst / ".backups" / "config.yaml.last-good").read_text()


class TestStartSwitchesAndRollsBack(_MainRestore):

    def test_a_good_restore_serves_and_its_config_becomes_last_good(self):
        # 3.4.148: no staged restore to switch to.
        self.stage()
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertIsNone(self.marker())
        self.assertTrue(self.result()["ok"])
        self.assertIn("# backup settings", self.last_good())
        self.assertEqual(self.last_good(), self.cfg_path.read_text())

    def test_a_failing_migration_rolls_back_and_the_previous_data_serves(self):
        self.stage()
        fail = mock.patch("schema_migrations.apply_schema_migrations",
                          return_value={"ok": False, "applied": [], "starting_version": 17,
                                        "error": "injected migration failure"})
        self.assertEqual(self.start(extra=[fail]), "reexec")
        self.assertEqual(self.execs, [[sys.executable, str(self.inst / "main.py"), "start"]])
        m = self.marker()
        self.assertEqual(m["phase"], "rollback")
        self.assertIn("injected migration failure", m["error"])
        # the re-exec'd start puts the previous data back and serves on it
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
        res = self.result()
        self.assertTrue(res["rolled_back"])
        self.assertIn("injected migration failure", res["error"])
        # last-good: the pre-restore config (the one that started)
        self.assertEqual(self.last_good().encode(), self.cfg_before)
        self.assertEqual(list(self.inst.glob("aircraft_history.db.bak.*.pre-restore")), [])

    def test_the_migration_gate_on_an_old_backup_rolls_back(self):
        # C1-1: a backfill that fails (disk full) on a restored OLD backup.
        old = self.work / "old.db"
        make_pre_v250_db(old)
        self.stage(db_bytes=old.read_bytes())
        gate = mock.patch("collector.rollup_backfill_incomplete",
                          return_value=["hourly_rollup_backfilled"])
        self.assertEqual(self.start(extra=[gate, mock.patch.object(self.main, "init_db")]),
                         "reexec")
        self.assertEqual(self.marker()["phase"], "rollback")
        self.assertIn("rollup build", self.marker()["error"])
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))

    def test_a_database_that_wont_open_rolls_back(self):
        self.stage()
        boom = mock.patch.object(self.main, "init_db",
                                 side_effect=sqlite3.DatabaseError("file is not a database"))
        self.assertEqual(self.start(extra=[boom]), "reexec")
        self.assertIn("couldn't be opened", self.marker()["error"])
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))

    def test_a_web_address_that_cant_be_used_waits_for_it(self):
        # The review build rolled a good restore back here (a restore keeps
        # this machine's address, so it can't be the cause): now the start
        # waits like any other, and the restore applies once it can listen.
        self.stage()
        self.assertEqual(self.start(bind=lambda h, p: (False, "in use")), "exit 2")
        self.assertEqual(self.marker()["phase"], "swapped")
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertTrue(self.result()["ok"])

    def test_starts_that_die_before_finishing_roll_back_after_two(self):
        self.stage()
        die = mock.patch.object(self.main, "get_app", side_effect=MemoryError("killed"))
        for _ in range(2):
            with self.assertRaises(MemoryError):
                self.start(extra=[die])
            self.assertEqual(self.marker()["phase"], "swapped")
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertIn("didn't finish starting", self.result()["error"])

    def test_a_restored_config_that_wont_load_rolls_back_not_recover(self):
        # load_config sees the restored config.yaml unreadable (here: made
        # so by hand after the swap): it is the restore's failure — the
        # previous data comes back, not just last-good's settings.
        self.stage()
        restore_io.apply_pending_restore(self.inst, log=lambda *_: None)
        self.cfg_path.write_text("web: [unclosed\n")
        (self.inst / ".backups").mkdir(exist_ok=True)
        (self.inst / ".backups" / "config.yaml.last-good").write_text(
            self.cfg_before.decode())
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = self.main.load_config("start")
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.10")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
        self.assertTrue(self.result()["rolled_back"])
        self.assertFalse((self.inst / ".backups" / "config-recovery.json").exists())

    def test_another_live_aerodrome_blocks_the_config_rollback(self):
        self.stage()
        restore_io.apply_pending_restore(self.inst, log=lambda *_: None)
        with mock.patch.object(self.main, "_running_instance_pid", return_value=4242):
            self.assertFalse(self.main._rollback_restored_config("x"))
        self.assertEqual(self.marker()["phase"], "swapped")
        self.assertEqual(count_rows(self.live), (40, "B00000"))

    def test_a_staged_restore_waits_while_an_update_is_confirmed(self):
        self.stage()
        # (any journal: a state with no confirm thread, so nothing outlives the test)
        with mock.patch.object(self.main._updater, "pending",
                               return_value={"state": "committing", "to_version": "x"}):
            self.assertEqual(self.start(real_db=False), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(self.marker()["phase"], "staged")

    def test_with_no_marker_leftovers_are_swept(self):
        staged = self.inst / ".aircraft_history.db.restore-staged"
        staged.write_bytes(b"x" * 100)
        Path(f"{staged}-wal").write_bytes(b"x")
        (self.inst / ".config.yaml.restore-staged").write_text("x")
        keep = self.inst / "aircraft_history.db.bak.20260101-000000.pre-restore"
        keep.write_bytes(b"x")
        self.assertEqual(self.start(real_db=False), "served")
        self.assertEqual(self.staged_files(), [])
        self.assertTrue(keep.exists())


class TestOldBackupEndToEnd(_MainRestore):
    """CLAUDE.md's SQLite rule for a change that swaps database files and
    migrates restored ones: a fresh install, a backup from an older release
    restored and migrated by the REAL startup (init_db, the rollup
    backfills, the gate, every migration), then the read path. 3.4.148:
    the restore wrote the old database while the collector ran."""

    def test_pre_v250_backup_restored_migrated_and_read(self):
        from schema_migrations import CURRENT_SCHEMA_VERSION, current_version
        import collector
        old = self.work / "old.db"
        make_pre_v250_db(old)
        self.stage(db_bytes=old.read_bytes())
        self.assertEqual(self.start(), "served")
        self.assertTrue(self.result()["ok"], self.result())
        c = sqlite3.connect(str(self.live))
        try:
            self.assertEqual(current_version(c), CURRENT_SCHEMA_VERSION)
            got = dict(c.execute("SELECT icao, sighting_count FROM seen_aircraft"))
            want = dict(c.execute("SELECT icao, COUNT(*) FROM all_sightings GROUP BY icao"))
            self.assertEqual(got, want)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM all_sightings").fetchone()[0], 3000)
        finally:
            c.close()
        self.assertEqual(collector.rollup_backfill_incomplete(str(self.live)), [])
        # the read path, through the app on the restored data
        app = self.server.get_app(self.main.load_config("start"), str(self.cfg_path))
        stats = _endpoint(app, "/api/stats", "GET")()
        self.assertIsInstance(stats, dict)
        detail = _endpoint(app, "/api/aircraft/{icao}", "GET")(icao="F10001")
        body = detail if isinstance(detail, dict) else json.loads(detail.body)
        self.assertEqual(json.dumps(body).count("F10001") > 0, True)

    def test_fresh_install_restores_a_current_backup(self):
        # Guard: the SQLite rule's fresh-install side — no history yet, so
        # no pre-restore copy of the database (3.4.148 restored it too, by
        # writing it while Aerodrome ran).
        from schema_migrations import CURRENT_SCHEMA_VERSION, current_version
        self.live.unlink()
        self.stage()
        self.assertEqual(self.start(), "served")
        c = sqlite3.connect(str(self.live))
        try:
            self.assertEqual(current_version(c), CURRENT_SCHEMA_VERSION)
        finally:
            c.close()
        self.assertEqual(count_rows(self.live), (40, "B00000"))


# ===========================================================================
# restore_io units (guards of the new module)
# ===========================================================================

@needs_restore_io
class TestSnapshotDatabase(unittest.TestCase):

    def setUp(self):
        self.dir = _tmpdir(self)

    def test_snapshot_includes_commits_still_in_the_wal(self):
        live = self.dir / "live.db"
        make_history_db(live, 0xA00000, 10)
        writer = sqlite3.connect(str(live))
        reader = sqlite3.connect(str(live))            # pins the WAL (no checkpoint)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM all_sightings").fetchone()
        writer.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
                           [(f"{0xA10000 + i:06X}", 1792700000) for i in range(50)])
        writer.commit()
        self.assertGreater(os.path.getsize(str(live) + "-wal"), 0)
        snap = restore_io.snapshot_database(live, self.dir / "snap.db", mode=0o640)
        reader.rollback()
        reader.close()
        writer.close()
        self.assertEqual(count_rows(snap)[0], 60)
        self.assertFalse(Path(str(snap) + "-wal").exists())
        self.assertEqual(stat.S_IMODE(os.stat(snap).st_mode), 0o640)

    def test_no_live_db_means_no_snapshot(self):
        self.assertIsNone(restore_io.snapshot_database(self.dir / "absent.db",
                                                       self.dir / "snap.db"))


@needs_restore_io
class TestPrepareConfig(unittest.TestCase):

    def setUp(self):
        from config_validator import validate_config
        self.validate = validate_config
        self.current = example_cfg("/srv/example/aircraft_history.db")

    def prep(self, text):
        return restore_io.prepare_config(text, self.current, self.validate)

    def test_identical_machine_keys_return_text_untouched(self):
        text = "# keep me\n" + yaml.safe_dump(example_cfg("/srv/example/aircraft_history.db"))
        out, notes = self.prep(text)
        self.assertEqual(out, text)
        self.assertEqual(notes, [])

    def test_hhmm_values_survive_the_override_render(self):
        theirs = example_cfg("/srv/other/h.db", host="192.0.2.77")
        theirs.setdefault("notifications", {})["quiet_hours_start"] = "22:00"
        out, _notes = self.prep(yaml.safe_dump(theirs, sort_keys=False))
        cfg = yaml.safe_load(out)
        self.assertEqual(cfg["notifications"]["quiet_hours_start"], "22:00")
        self.assertEqual(cfg["web"]["host"], "127.0.0.1")


# ===========================================================================
# Kill points (every recovery path; guards of the new module)
# ===========================================================================

class _Kill(BaseException):
    pass


class _Faults:
    """Kill (an exception nothing catches) at the k-th filesystem operation."""
    OPS = ("rename", "replace", "link", "unlink")

    def __init__(self, k):
        self.k, self.n, self.real = k, 0, {}

    def __enter__(self):
        for name in self.OPS:
            real = getattr(os, name)
            self.real[name] = real

            def wrap(*a, _real=real, **kw):
                self.n += 1
                if self.n == self.k:
                    raise _Kill()
                return _real(*a, **kw)
            setattr(os, name, wrap)
        return self

    def __exit__(self, *a):
        for n, r in self.real.items():
            setattr(os, n, r)


def _quiet(*_a, **_k):
    pass


class _KillPointsBase(unittest.TestCase):
    """A staged restore made by restore_io.prepare (live: 100 rows from
    A00000; restored: 7 rows from B00000), killed at every filesystem
    operation of one recovery path; the next start (a replay) must end in
    one complete, consistent state."""

    STAGED_TEXT = ("receiver: {}\nweb: {port: 18230}\ndata: {}\nlogging: {}\n"
                   "retention: {}\n# restored\n")

    @classmethod
    def setUpClass(cls):
        cls._tdir = tempfile.mkdtemp(prefix="aerodrome-b4c-tpl-", dir=TMP)
        cls.tpl_live = Path(cls._tdir) / "live.db"
        cls.tpl_src = Path(cls._tdir) / "src.db"
        make_history_db(cls.tpl_live, 0xA00000, 100)
        make_history_db(cls.tpl_src, 0xB00000, 7)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tdir, True)

    def setUp(self):
        self.base = _tmpdir(self)

    def base_fixture(self, db_dir=None, cfg_link_dir=None):
        """inst/, the live database (in db_dir: another filesystem for the
        cross-filesystem runs) and config.yaml (a symlink to a file in
        cfg_link_dir when given)."""
        inst = Path(tempfile.mkdtemp(dir=self.base))
        (inst / ".backups").mkdir()
        dbd = Path(tempfile.mkdtemp(dir=db_dir)) if db_dir else inst
        live = dbd / "history.db"
        shutil.copyfile(self.tpl_live, live)
        cfg = inst / "config.yaml"
        if cfg_link_dir:
            target = Path(tempfile.mkdtemp(dir=cfg_link_dir)) / "config.yaml"
            target.write_text("web: {port: 18230}\n# live\n")
            cfg.symlink_to(target)
        else:
            cfg.write_text("web: {port: 18230}\n# live\n")
        return inst, live, cfg

    def stage(self, inst, live, cfg):
        restore_io.prepare(
            inst, db_live=live, config_live=cfg, source="example.zip", manifest={},
            notes=[], cfg_text=self.STAGED_TEXT,
            db_writer=lambda dest, mode: shutil.copyfile(self.tpl_src, dest),
            db_size=self.tpl_src.stat().st_size, current_schema=10 ** 6, free_bytes=10 ** 12)

    def fixture(self, **fx):
        inst, live, cfg = self.base_fixture(**fx)
        self.stage(inst, live, cfg)
        return inst, live, cfg

    def assert_restored(self, inst, live, cfg, k):
        m = restore_io.read_marker(inst)
        self.assertEqual(count_rows(live), (7, "B00000"), f"k={k}")
        self.assertIn("# restored", cfg.read_text(), f"k={k}")
        self.assertTrue(m and m["phase"] == "swapped", f"k={k}: {m}")

    def assert_rolled_back(self, inst, live, cfg, k):
        self.assertEqual(count_rows(live), (100, "A00000"), f"k={k}")
        self.assertIn("# live", cfg.read_text(), f"k={k}")
        self.assertIsNone(restore_io.read_marker(inst), f"k={k}")
        res = restore_io.read_result(inst)
        self.assertTrue(res and res["rolled_back"] is True, f"k={k}: {res}")
        restore_io.sweep_orphans(inst, live, cfg, log=_quiet)     # the start goes on
        self.assertEqual(sorted(p.name for p in live.parent.glob(".*restore-staged*")), [],
                         f"k={k}")
        self.assertEqual(sorted(p.name for p in inst.glob(".*restore-staged*")), [], f"k={k}")

    def run_apply(self, **fx):
        points = 0
        for k in range(1, 80):
            inst, live, cfg = self.fixture(**fx)
            with _Faults(k):
                try:
                    restore_io.apply_pending_restore(inst, log=_quiet)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            restore_io.apply_pending_restore(inst, log=_quiet)       # the next start
            self.assert_restored(inst, live, cfg, k)
        self.assertGreater(points, 2)
        return points

    def run_rollback(self, **fx):
        points = 0
        for k in range(1, 80):
            inst, live, cfg = self.fixture(**fx)
            state, m = restore_io.apply_pending_restore(inst, log=_quiet)
            self.assertEqual(state, "applied")
            # the start that failed had written to the restored database and
            # died without a checkpoint: its -wal must never replay into the
            # copy put back
            _child_wal(live, 0xC00000, 50)
            restore_io.request_rollback(inst, m, "migration failed (simulated)")
            with _Faults(k):
                try:
                    restore_io.apply_pending_restore(inst, log=_quiet)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            restore_io.apply_pending_restore(inst, log=_quiet)       # the next start
            self.assert_rolled_back(inst, live, cfg, k)
        self.assertGreater(points, 2)
        return points


@needs_restore_io
class TestRestoreKillPoints(_KillPointsBase):

    def test_apply(self):
        # staged → swapped: live database = restored, config = staged, marker swapped
        self.run_apply()

    def test_rollback(self):
        # rollback: live = pre-restore rows and config, no marker, rolled_back true
        self.run_rollback()

    def test_the_old_databases_wal_never_replays_into_the_restored_one(self):
        # A start after a crash finds the old database's -wal (commits not yet
        # checkpointed). Left beside the restored file, SQLite would apply
        # those frames to it: a mix of both databases.
        inst, live, cfg = self.fixture()
        c = sqlite3.connect(str(live))
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
                      [(f"{0xE00000 + i:06X}", 1792700000) for i in range(300)])
        c.commit()
        wal = Path(f"{live}-wal")
        saved = wal.read_bytes()
        self.assertGreater(len(saved), 0)
        c.close()                                   # (a clean close checkpoints it)
        wal.write_bytes(saved)                      # ... the crashed process didn't
        state, _m = restore_io.apply_pending_restore(inst, log=_quiet)
        self.assertEqual(state, "applied")
        self.assertFalse(wal.exists(), "the old -wal is still beside the restored database")
        c = sqlite3.connect(str(live))
        try:
            self.assertEqual(c.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            c.close()
        self.assertEqual(count_rows(live), (7, "B00000"))

    def test_a_swap_cut_short_is_finished_even_an_hour_later(self):
        # A power cut part-way through the swap, and the box stays off for
        # over an hour: the expiry must not drop a half-moved restore (that
        # would leave the restored database under the old settings).
        points = 0
        for k in range(1, 80):
            inst, live, cfg = self.fixture()
            with _Faults(k):
                try:
                    restore_io.apply_pending_restore(inst, log=_quiet)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            m = restore_io.read_marker(inst)
            if m and m["phase"] == "staged":
                m["staged_at"] = time.time() - 7200
                restore_io.write_marker(inst, m)
            state, _m = restore_io.apply_pending_restore(inst, log=_quiet)
            if state == "expired":                      # nothing had moved yet
                self.assertEqual(count_rows(live), (100, "A00000"), k)
                self.assertIn("# live", cfg.read_text(), k)
            else:
                self.assert_restored(inst, live, cfg, k)
        self.assertGreater(points, 2)

    def test_rollback_started_without_recording_its_phase(self):
        # The rollback's own phase write is best effort (a full disk): a
        # kill inside it is still finished by the next start, which sees a
        # copy already moved back (or, before any move, counts the start).
        with mock.patch.object(restore_io, "request_rollback",
                               side_effect=OSError(errno.ENOSPC, "No space left on device")):
            points = 0
            for k in range(1, 80):
                inst, live, cfg = self.fixture()
                _s, m = restore_io.apply_pending_restore(inst, log=_quiet)
                with _Faults(k):
                    try:
                        restore_io.rollback_pending_restore(inst, m, "simulated", log=_quiet)
                        hit = False
                    except _Kill:
                        hit = True
                if not hit:
                    break
                points += 1
                mk = restore_io.read_marker(inst)
                if mk is not None:
                    db = mk["db"]
                    moved = (not os.path.lexists(db["snapshot"])
                             and restore_io._ino(live) == list(db["snapshot_ino"]))
                    state, _m = restore_io.apply_pending_restore(inst, log=_quiet)
                    if moved:
                        # a copy is already back: this start must finish the
                        # rollback (a 'retry' would serve the old database
                        # under the backup's settings and call it restored)
                        self.assertEqual(state, "rolled_back", f"k={k}")
                    elif state == "retry":
                        # nothing moved yet: the retry start succeeds — the
                        # restore is complete and consistent
                        restore_io.finish_pending_restore(inst, restore_io.read_marker(inst),
                                                          log=_quiet)
                        self.assertEqual(count_rows(live), (7, "B00000"), f"k={k}")
                        self.assertIn("# restored", cfg.read_text(), f"k={k}")
                        self.assertTrue(restore_io.read_result(inst)["ok"], f"k={k}")
                        continue
                self.assert_rolled_back(inst, live, cfg, k)
            self.assertGreater(points, 2)

    def test_cancel(self):
        points = 0
        for k in range(1, 60):
            inst, live, cfg = self.fixture()
            with _Faults(k):
                try:
                    restore_io.cancel_pending_restore(inst, log=_quiet)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            # Cut short, a cancel either still waits (cancelled again from
            # Settings, or it expires) or is over (the start sweeps its files).
            if restore_io.read_marker(inst) is not None:
                restore_io.cancel_pending_restore(inst, log=_quiet)
            restore_io.sweep_orphans(inst, live, cfg, log=_quiet)
            self.assertEqual(count_rows(live), (100, "A00000"), k)
            self.assertIn("# live", cfg.read_text())
            self.assertIsNone(restore_io.read_marker(inst))
            self.assertEqual(sorted(p.name for p in inst.glob(".*restore-staged*")), [], k)
        self.assertGreater(points, 1)

    def test_expiry_discard(self):
        points = 0
        for k in range(1, 60):
            inst, live, cfg = self.fixture()
            m = restore_io.read_marker(inst)
            m["staged_at"] = time.time() - 7200
            restore_io.write_marker(inst, m)
            with _Faults(k):
                try:
                    restore_io.apply_pending_restore(inst, log=_quiet)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            state, _m = restore_io.apply_pending_restore(inst, log=_quiet)
            self.assertIn(state, ("expired", "none"))
            if state == "none":
                restore_io.sweep_orphans(inst, live, cfg, log=_quiet)
            self.assertEqual(count_rows(live), (100, "A00000"), k)
            self.assertIn("# live", cfg.read_text())
            self.assertEqual(sorted(p.name for p in inst.glob(".*restore-staged*")), [], k)
        self.assertGreater(points, 1)

    def test_prepare_never_leaves_a_half_restore(self):
        # A kill while a restore is prepared: either no marker (the next
        # start sweeps what it made; the live files untouched) or a complete
        # staged restore that applies.
        points = 0
        for k in range(1, 80):
            inst, live, cfg = self.base_fixture()
            with _Faults(k):
                try:
                    self.stage(inst, live, cfg)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            if restore_io.read_marker(inst) is None:
                restore_io.sweep_orphans(inst, live, cfg, log=_quiet)
                self.assertEqual(sorted(p.name for p in inst.glob(".*restore-staged*")), [], k)
                self.assertEqual(count_rows(live), (100, "A00000"), k)
                self.assertIn("# live", cfg.read_text())
            else:
                restore_io.apply_pending_restore(inst, log=_quiet)
                self.assert_restored(inst, live, cfg, k)
        self.assertGreater(points, 2)


@needs_restore_io
class TestCrossFilesystem(_KillPointsBase):
    """The independent gap check's finding: rollback rev 2 kept config.yaml's
    pre-restore copy beside the DATABASE and put it back with a bare rename.
    With data.db_file on another filesystem that raised EXDEV after the
    database was already back; the marker stayed, and every start re-raised
    — a tight re-exec loop. Each copy now lives on its own file's
    filesystem, and a config.yaml symlinked onto another filesystem is put
    back by content (config_io), not by a rename. Runs for real across two
    filesystems (see the module docstring); every kill point too."""

    def test_copies_stay_on_their_own_filesystem(self):
        inst, live, cfg = self.fixture(db_dir=_xfs_tmpdir(self))
        m = restore_io.read_marker(inst)
        dev = lambda p: os.stat(p).st_dev                       # noqa: E731
        self.assertEqual(dev(m["db"]["snapshot"]), dev(live))
        self.assertEqual(dev(m["db"]["staged"]), dev(live))
        self.assertEqual(dev(m["config"]["snapshot"]), dev(cfg))
        self.assertEqual(dev(m["config"]["staged"]), dev(cfg))
        self.assertNotEqual(dev(live), dev(cfg))
        restore_io.apply_pending_restore(inst, log=_quiet)
        restore_io.request_rollback(inst, restore_io.read_marker(inst), "simulated")
        self.assertEqual(restore_io.apply_pending_restore(inst, log=_quiet)[0], "rolled_back")
        self.assertIsNone(restore_io.read_marker(inst))
        self.assert_rolled_back(inst, live, cfg, 0)

    def test_apply_with_the_database_on_another_filesystem(self):
        self.run_apply(db_dir=_xfs_tmpdir(self))

    def test_rollback_with_the_database_on_another_filesystem(self):
        self.run_rollback(db_dir=_xfs_tmpdir(self))

    def test_apply_with_config_linked_onto_another_filesystem(self):
        self.run_apply(cfg_link_dir=_xfs_tmpdir(self))

    def test_rollback_with_config_linked_onto_another_filesystem(self):
        self.run_rollback(cfg_link_dir=_xfs_tmpdir(self))

    def test_a_rename_that_fails_with_exdev_falls_back_to_writing(self):
        # Portable: the same fallback, forced (a bind mount gives EXDEV
        # within one filesystem too).
        inst, live, cfg = self.fixture()
        real = os.replace

        def replace(a, b, *x, **kw):
            if str(b) == os.path.realpath(str(cfg)) and ".pre-restore" in str(a):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(a, b, *x, **kw)
        restore_io.apply_pending_restore(inst, log=_quiet)
        with mock.patch("os.replace", replace):
            restore_io.rollback_pending_restore(inst, restore_io.read_marker(inst),
                                                "simulated", log=_quiet)
        self.assert_rolled_back(inst, live, cfg, 0)


@needs_restore_io
class TestNoRawConfigWritesInRestoreIo(unittest.TestCase):
    """restore_io writes config.yaml (and its staged copy and pre-restore
    copy) only through config_io — the AST guard of test_config_write_safety,
    with restore_io's own names for config paths."""

    def test_source(self):
        from test_config_write_safety import TestNoRawConfigWrites as G
        names = G.NAMES | {"config_live", "config_staged", "config_snapshot"}
        with mock.patch.object(G, "NAMES", names):
            hits = G.raw_writes((REPO / "restore_io.py").read_text())
        self.assertEqual(hits, [], "\n".join(hits))

    def test_parses_under_python_3_10(self):
        import ast
        for name in ("restore_io.py", "config_io.py", "test_backup_restore.py"):
            with self.subTest(module=name):
                ast.parse((REPO / name).read_text(), filename=name, feature_version=(3, 10))


# ===========================================================================
# Review fixes (3.4.149 review round): each test names what failed before
# its fix; "Guard" marks a test of the new module's own mechanics.
# ===========================================================================

def _child_wal(path, first_hex, rows):
    """A process that commits `rows` to `path` in WAL mode and dies without
    closing (no checkpoint): what an exec'd or killed start leaves behind."""
    code = ("import os, sqlite3, sys\n"
            "c = sqlite3.connect(sys.argv[1])\n"
            "c.execute('PRAGMA journal_mode=WAL')\n"
            "c.execute('PRAGMA wal_autocheckpoint=0')\n"
            "c.executemany('INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)',"
            " [('%06X' % (int(sys.argv[2]) + i), 1792800000) for i in range(int(sys.argv[3]))])\n"
            "c.commit()\n"
            "os._exit(0)\n")
    r = _REAL_RUN([sys.executable, "-c", code, str(path), str(first_hex), str(rows)],
                  capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    wal = Path(f"{path}-wal")
    assert wal.exists() and wal.stat().st_size > 0, "no -wal was left"
    return wal


def _quick_check(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        c.close()


class TestSymlinkedDatabase(_Restore):
    """data.db_file a symlink onto another disk (the history moved off an SD
    card with `ln -s`). The restore used the link's own path: the staged file
    and the copy sat on the install's disk and the swap renamed a regular
    file OVER the link — the history silently moved back onto the install's
    disk, and a stale full copy stayed behind. 3.4.148 wrote through the
    link. Runs for real across two filesystems (skipped without one)."""

    def db_dir(self):
        return _xfs_tmpdir(self)

    def customize(self, cfg):
        self.link = self.inst / "aircraft_history.db"
        self.link.symlink_to(self.live)
        cfg["data"]["db_file"] = str(self.link)

    def dev(self, p):
        return os.stat(p).st_dev

    def assert_link(self):
        self.assertTrue(self.link.is_symlink(), "the symlink was replaced by a regular file")
        self.assertEqual(os.path.realpath(self.link), str(self.live))

    def test_staged_copy_and_swap_stay_on_the_databases_own_disk(self):
        self.assertNotEqual(self.dev(self.live), self.dev(self.inst))
        wal = _child_wal(self.live, 0xE00000, 50)       # the real database's -wal (beside the target)
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        m = self.marker()
        self.assertEqual(m["db"]["live"], str(self.live))
        self.assertEqual(m["db"]["configured"], str(self.link))
        for k in ("staged", "snapshot"):
            self.assertEqual(Path(m["db"][k]).parent, self.live.parent, k)
            self.assertEqual(self.dev(m["db"][k]), self.dev(self.live), k)
        self.assertEqual(count_rows(m["db"]["snapshot"]), (80, "A00000"))  # -wal included
        self.assertEqual(self.start_half(), "applied")
        self.assert_link()
        self.assertFalse(wal.exists(), "the old -wal beside the target is still there")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertEqual(count_rows(self.link), (40, "B00000"))
        listed = _call(self.ep("/api/backup/pre-restore", "GET")())[1]
        self.assertEqual(listed["count"], 1)

    def test_rollback_and_put_back_keep_the_link(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        state, m = restore_io.apply_pending_restore(self.inst, log=_quiet)
        self.assertEqual(state, "applied")
        restore_io.request_rollback(self.inst, m, "simulated: migration failed")
        self.assertEqual(restore_io.apply_pending_restore(self.inst, log=_quiet)[0],
                         "rolled_back")
        self.assert_link()
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        # Put this back (an earlier restore's pair) also lands on the target
        self.assertEqual(self.restore()[0], 200)
        ts = self.marker()["ts"]
        self.start_half()
        self.assert_link()
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=ts, background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 200, body)
        self.assertEqual(self.dev(self.marker()["db"]["staged"]), self.dev(self.live))
        self.start_half()
        self.assert_link()
        self.assertEqual(count_rows(self.live), (30, "A00000"))

    def test_startup_sweep_finds_the_staged_file_beside_the_target(self):
        stray = restore_io.staged_db_path(self.live)
        stray.write_bytes(b"x" * 10)
        restore_io.sweep_orphans(self.inst, self.link, self.cfg_path, log=_quiet)
        self.assertFalse(stray.exists())


class _BindBase(_MainRestore):
    hold_port = False

    def customize(self, cfg):
        self.held = None
        if self.hold_port:
            import socket
            self.held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.held.bind(("127.0.0.1", 0))         # an OS-chosen free port
            self.held.listen(1)
            self.addCleanup(lambda: self.held and self.held.close())
            cfg["web"]["port"] = self.held.getsockname()[1]


class TestBindFailureIsNotTheRestores(_BindBase):
    """A restore keeps this machine's web.host and web.port (Decision 4), so
    a web address that can't be used can't be the restored files' fault —
    yet a bind failure rolled a good restore back (its copy used up, "the
    backup didn't load"). Now that start waits like any other (exit 2,
    systemd retries) and doesn't count against the restore."""

    def test_a_bind_failure_waits_and_the_restore_then_applies(self):
        self.stage()
        for _ in range(3):           # more failed starts than MAX_SWAPPED_STARTS
            self.assertEqual(self.start(bind=lambda h, p: (False, "in use")), "exit 2")
            m = self.marker()
            self.assertEqual(m["phase"], "swapped")
            self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertIsNone(self.marker())
        self.assertTrue(self.result()["ok"])
        self.assertEqual(len(list(self.inst.glob("aircraft_history.db.bak.*.pre-restore"))), 1)

    def test_a_restore_that_did_move_the_address_still_rolls_back(self):
        # Guard: config.yaml was hand-edited to another port and never loaded: the
        # restore kept the RUNNING port, so the restored config listens
        # somewhere the settings from before it didn't.
        text = self.cfg_path.read_text().replace("port: 18299", "port: 18298")
        self.assertNotEqual(text, self.cfg_path.read_text())
        self.cfg_path.write_text(text)
        self.stage()
        self.assertEqual(self.start(bind=lambda h, p: (False, "in use")), "reexec")
        self.assertIn("couldn't listen", self.marker()["error"])
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))


class TestBindFailureWithARealHeldPort(_BindBase):
    """The same with the real bind check and a port another socket holds,
    for a database-only restore (config.yaml never changes)."""
    hold_port = True

    def test_database_only_waits_then_applies(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        status, body = self.restore(keep_current_config=True)
        self.assertEqual(status, 200, body)
        real = self.main._preflight_bind_check
        self.assertEqual(self.start(bind=real), "exit 2")
        self.assertEqual(self.marker()["phase"], "swapped")
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
        self.held.close()
        self.held = None
        self.assertEqual(self.start(bind=real), "served")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertTrue(self.result()["ok"])


class _FullStartKills(_MainRestore):
    """Kill points across a WHOLE switching start — main.load_config (which
    migrates config.yaml) then main.start — each next start being the real
    load_config + start too. The kill-point classes above replay
    restore_io alone and never met load_config: killed at the write that
    records `swapped`, the next start's migrate_config rewrote the restored
    config.yaml (a backup from an older release lacks newer sections), the
    swap's replay found it changed, and a good restore was rolled back."""

    def backup_text_old_release(self):
        cfg = self.backup_cfg()
        for k in ("display", "map", "military"):
            cfg.pop(k, None)
        return "# backup settings\n" + yaml.safe_dump(cfg, sort_keys=False)

    def setUp(self):
        super().setUp()
        self.tpl = self.work / "tpl.db"
        shutil.copyfile(self.live, self.tpl)

    def reset(self):
        for d in {self.inst, self.live.parent, self.cfg_path.resolve().parent}:
            for p in list(d.iterdir()):
                if p.name in ("static", ".backups") or p.is_dir():
                    continue
                if p.name.startswith(("aircraft_history.db", ".aircraft_history.db",
                                      "config.yaml", ".config.yaml", ".tracker.pid")):
                    p.unlink()
        for p in list((self.inst / ".backups").iterdir()):
            if p.is_file() or p.is_symlink():
                p.unlink()
        shutil.copyfile(self.tpl, self.live)
        self.write_live_config(self.cfg_before)

    def write_live_config(self, data):
        self.cfg_path.write_bytes(data)

    def run_kills(self):
        points = 0
        text = self.backup_text_old_release()
        for k in range(1, 120):
            self.reset()
            make_backup_zip(self.zip, text, self.db_bytes)
            status, body = self.restore()
            self.assertEqual(status, 200, body)
            with _Faults(k):
                try:
                    out = self.start()
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                self.assertEqual(out, "served")
                break
            points += 1
            self.assertEqual(self.start(), "served", f"k={k}")
            self.assertEqual(count_rows(self.live), (40, "B00000"), f"k={k}")
            self.assertIn("# backup settings", self.cfg_path.read_text(), f"k={k}")
            self.assertIsNone(self.marker(), f"k={k}")
            res = self.result()
            self.assertTrue(res and res["ok"], f"k={k}: {res}")
            self.assertFalse((self.inst / ".backups" / "config-recovery.json").exists(), k)
        self.assertGreater(points, 5)


class TestFullStartKillPoints(_FullStartKills):

    def test_plain_layout(self):
        self.run_kills()


class TestFullStartKillPointsConfigOnAnotherDisk(_FullStartKills):
    """config.yaml a symlink onto another filesystem (its swap is a write
    through config_io, not a rename)."""

    def customize(self, cfg):
        self.cfg_target = _xfs_tmpdir(self) / "config.yaml"

    def setUp(self):
        super().setUp()
        self.cfg_target.write_bytes(self.cfg_path.read_bytes())
        self.cfg_path.unlink()
        self.cfg_path.symlink_to(self.cfg_target)

    def write_live_config(self, data):
        if not self.cfg_path.is_symlink():
            self.cfg_path.symlink_to(self.cfg_target)
        self.cfg_target.write_bytes(data)

    def test_config_linked_onto_another_filesystem(self):
        self.run_kills()


class TestTornInPlaceSettingsWrite(_MainRestore):
    """B4b's in-place layout (config.yaml a symlink into a folder Aerodrome
    can't create files in): a power cut inside the swap's in-place write
    left config.yaml a mix of old and new while the marker still said
    `staged`. The next start sent it to config recovery — a spurious notice
    with last-good, and with none every start exited 1 ("no usable copy")
    while the complete staged settings sat right beside it."""

    def customize(self, cfg):
        self.ro_dir = self.work / "ro-settings"
        self.ro_dir.mkdir()

    def setUp(self):
        super().setUp()
        target = self.ro_dir / "config.yaml"
        target.write_bytes(self.cfg_path.read_bytes())
        os.chmod(target, 0o600)
        self.cfg_path.unlink()
        self.cfg_path.symlink_to(target)
        os.chmod(self.ro_dir, 0o555)
        self.addCleanup(os.chmod, self.ro_dir, 0o755)
        if os.access(self.ro_dir, os.W_OK):
            self.skipTest("running as root: no folder is read-only")

    def test_a_torn_write_is_finished_not_recovered(self):
        self.assertEqual(config_io._write_mode(self.cfg_path), "in_place")
        # keys in another order than the live file: a mix of the two is
        # rarely valid YAML
        self.stage(text="# backup settings\n" + yaml.safe_dump(self.backup_cfg(),
                                                              sort_keys=True))
        real_write = config_io._write_in_place
        torn = []

        def tear(staged, name):
            old = Path(staged.target).read_bytes()
            for cut in range(len(staged.data) // 4, len(staged.data), 13):
                mix = staged.data[:cut] + old[cut:]
                try:
                    config_io.check_loadable(mix.decode("utf-8", "replace"))
                except ValueError:
                    break
            else:
                self.fail("no cut point leaves an unreadable file")
            fd = os.open(str(staged.target), os.O_WRONLY)
            try:
                os.write(fd, staged.data[:cut])
            finally:
                os.close(fd)
            torn.append(cut)
            raise _Kill()
        with mock.patch.object(config_io, "_write_in_place", tear):
            with self.assertRaises(_Kill):
                self.start()
        self.assertTrue(torn)
        with self.assertRaises(ValueError):
            config_io.check_loadable(self.cfg_path.read_text(errors="replace"))
        self.assertFalse((self.inst / ".backups" / "config.yaml.last-good").exists())
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertIn("# backup settings", self.cfg_path.read_text())
        self.assertTrue(self.result()["ok"])
        self.assertFalse((self.inst / ".backups" / "config-recovery.json").exists())
        self.assertTrue(self.cfg_path.is_symlink())
        del real_write


class TestDropIsSerialisedWithANewRestore(_Waiting):
    """An expired restore was dropped by any caller — the Settings page's
    status poll, a settings write, the upload check — without the
    maintenance lock. A new restore staged meanwhile under the same fixed
    file names lost its marker and staged files to the old drop: it
    answered "restarting to switch to it" and the next start did nothing."""

    def age(self, sec):
        m = self.marker()
        m["staged_at"] = time.time() - sec
        (self.inst / ".backups" / MARKER).write_text(json.dumps(m))

    def test_settings_writes_and_the_upload_check_never_drop(self):
        self.age(3601)
        self.assertIsNone(self.server._restore_waiting())
        mw = self.server._BodySizeLimitMiddleware(None)
        mw._large_upload_refusal("/api/backup/import", 10)
        self.assertIsNotNone(self.marker(), "dropped without the maintenance lock")

    def test_a_slow_status_drop_and_a_new_restore(self):
        self.age(3601)
        old_staged_at = self.marker()["staged_at"]
        self.run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
        real_fsync = os.fsync
        a_thread = []
        entered = threading.Event()

        def slow_fsync(fd):
            if threading.current_thread() in a_thread and not entered.is_set():
                entered.set()
                time.sleep(1.0)
            return real_fsync(fd)
        out = {}

        def a():
            a_thread.append(threading.current_thread())
            out["a"] = self.ep("/api/maintenance/status", "GET")()

        def b():
            entered.wait(5)
            ep = self.ep("/api/backup/restore-from-path")
            out["b"] = _call(ep(ep.__annotations__["payload"](path=str(self.zip)),
                                background_tasks=None))
        with mock.patch("os.fsync", slow_fsync):
            ta, tb = threading.Thread(target=a), threading.Thread(target=b)
            ta.start()
            tb.start()
            ta.join(30)
            tb.join(30)
        self.release_lock()
        status, body = out["b"]
        res = self.result()
        self.assertTrue(res["expired"])
        self.assertEqual(res["staged_at"], old_staged_at)
        if status == 200:
            m = self.marker()
            self.assertIsNotNone(m, "the new restore's marker was dropped")
            self.assertNotEqual(m["staged_at"], old_staged_at)
            self.assertTrue(os.path.exists(m["db"]["staged"]))
            self.assertTrue(os.path.exists(m["config"]["staged"]))
            self.assertEqual(self.start_half(), "applied")
            self.assertEqual(count_rows(self.live), (40, "B00000"))
        else:
            # the status poll held the lock: the restore was refused, plainly
            self.assertEqual(status, 409, body)
            self.assertIsNone(self.marker())

    def test_a_drop_never_takes_a_newer_marker(self):
        # Guard (defence in depth): the old marker as read, the new one on disk.
        old = self.marker()
        new = dict(old, staged_at=time.time(), ts="20991231-235959")
        (self.inst / ".backups" / MARKER).write_text(json.dumps(new))
        self.assertFalse(restore_io._drop(self.inst, old, {"ok": False, "expired": True},
                                          _quiet))
        self.assertEqual(self.marker()["ts"], "20991231-235959")
        self.assertTrue(os.path.exists(new["db"]["staged"]))
        self.assertEqual(list((self.inst / ".backups").glob(MARKER + ".dropping-*")), [])

    def test_a_drop_leaves_a_staged_file_that_isnt_this_restores(self):
        # Guard: the drop removes staged files by inode / content only.
        m = self.marker()
        other = Path(m["db"]["staged"] + ".other")
        other.write_bytes(b"another restore's file")      # (a new inode: written first)
        os.replace(other, m["db"]["staged"])
        Path(m["config"]["staged"]).write_text("another: restore\n")
        self.assertTrue(restore_io._drop(self.inst, m, {"ok": False, "cancelled": True},
                                         _quiet))
        self.assertIsNone(self.marker())
        self.assertTrue(os.path.exists(m["db"]["staged"]))
        self.assertTrue(os.path.exists(m["config"]["staged"]))


class TestMalformedMarker(_MainRestore):
    """A marker that parses but has the wrong shape (a hand edit, or a
    release that broke the add-only rule): every start raised
    AttributeError — the fallback rollback too — a restart loop with no web
    UI; and every settings write, maintenance call and the status poll
    answered 500."""

    def put(self, phase):
        (self.inst / ".backups" / MARKER).write_text(json.dumps(
            {"version": 1, "phase": phase, "db": "aircraft_history.db", "config": {},
             "staged_at": time.time()}))

    def test_each_phase_starts_and_is_set_aside(self):
        for phase in ("staged", "swapped", "rollback"):
            with self.subTest(phase):
                self.put(phase)
                self.assertEqual(self.start(real_db=False), "served")
                self.assertFalse((self.inst / ".backups" / MARKER).exists())
                self.assertTrue(list((self.inst / ".backups").glob(MARKER + ".unreadable-*")))
                self.assertEqual(count_rows(self.live), (30, "A00000"))

    def test_the_running_server_answers(self):
        self.put("staged")
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertIsNone(st["restore"]["pending"])
        new = json.loads(json.dumps(self.server.CONFIG))
        put = self.ep("/api/config", "PUT")
        status, body = _call(put(put.__annotations__["payload"](config=new)))
        self.assertEqual(status, 200, body)

    def test_a_fallback_rollback_that_fails_never_crashes_the_start(self):
        # Guard: whatever the rollback raises, the start goes on.
        self.stage()
        boom = [mock.patch.object(restore_io, "_swap_in", side_effect=OSError(5, "EIO")),
                mock.patch.object(restore_io, "rollback_pending_restore",
                                  side_effect=AttributeError("x"))]
        self.assertEqual(self.start(extra=boom, real_db=False), "served")
        self.assertIsNone(self.marker())


class TestARollbackWhoseResultCantBeWritten(_KillPointsBase):
    """A disk that is full while a restore is switched in: the `swapped`
    write fails, the rollback's own phase write fails, then its result
    write fails — and the marker was removed anyway: the next start (with
    room again) served the previous data and the user was never told."""

    def test_the_next_start_with_room_records_it(self):
        inst, live, cfg = self.fixture()
        full = OSError(errno.ENOSPC, "No space left on device")
        real_json = atomic_io.write_json_atomic

        def json_full(path, *a, **k):
            if Path(path).name in (MARKER, RESULT):
                raise full
            return real_json(path, *a, **k)
        with mock.patch.object(atomic_io, "write_json_atomic", json_full):
            with self.assertRaises(OSError):
                restore_io.apply_pending_restore(inst, log=_quiet)
            m = restore_io.read_marker(inst)
            restore_io.rollback_pending_restore(inst, m, "switching failed: disk full",
                                                log=_quiet)
        self.assertIsNotNone(restore_io.read_marker(inst), "the marker went unrecorded")
        self.assertIsNone(restore_io.read_result(inst))
        self.assertEqual(count_rows(live), (100, "A00000"))
        # the next start, with room
        state, _m = restore_io.apply_pending_restore(inst, log=_quiet)
        self.assertEqual(state, "rolled_back")
        res = restore_io.read_result(inst)
        self.assertTrue(res["rolled_back"])
        self.assertFalse(res["ok"])
        self.assertIsNone(restore_io.read_marker(inst))
        self.assert_rolled_back(inst, live, cfg, 0)

    def test_files_done_marker_records_the_stored_result(self):
        # Guard: the marker could be written, only the result couldn't.
        inst, live, cfg = self.fixture()
        _s, m = restore_io.apply_pending_restore(inst, log=_quiet)
        real_json = atomic_io.write_json_atomic

        def result_full(path, *a, **k):
            if Path(path).name == RESULT:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_json(path, *a, **k)
        with mock.patch.object(atomic_io, "write_json_atomic", result_full):
            restore_io.rollback_pending_restore(inst, m, "migration failed", log=_quiet)
        mk = restore_io.read_marker(inst)
        self.assertTrue(mk["files_done"])
        self.assertEqual(mk["phase"], "rollback")
        self.assertEqual(restore_io.apply_pending_restore(inst, log=_quiet)[0], "rolled_back")
        self.assertIn("migration failed", restore_io.read_result(inst)["error"])
        self.assert_rolled_back(inst, live, cfg, 0)


class TestDeferralOnlyBeforeAnythingMoved(_KillPointsBase):
    """A start cut short after the swap's database rename, then a start that
    sees an update journal: the restore was deferred half-done — the
    restored database ran under the pre-restore settings with no rollback
    armed, and Cancel answered "nothing was changed"."""

    def half_swapped(self):
        inst, live, cfg = self.fixture()
        real = os.replace

        def replace(a, b, *x, **k):
            real(a, b, *x, **k)
            if str(b) == str(live):
                raise _Kill()
        with mock.patch("os.replace", replace):
            with self.assertRaises(_Kill):
                restore_io.apply_pending_restore(inst, log=_quiet)
        self.assertEqual(count_rows(live), (7, "B00000"))
        self.assertIn("# live", cfg.read_text())
        return inst, live, cfg

    def test_a_half_done_swap_is_finished_not_deferred(self):
        inst, live, cfg = self.half_swapped()
        state, _m = restore_io.apply_pending_restore(inst, log=_quiet, update_pending=True)
        self.assertEqual(state, "applied")
        self.assert_restored(inst, live, cfg, 0)

    def test_a_half_done_swap_cant_be_cancelled(self):
        inst, _live, _cfg = self.half_swapped()
        with self.assertRaises(ValueError):
            restore_io.cancel_pending_restore(inst, log=_quiet)
        self.assertEqual(restore_io.read_marker(inst)["phase"], "staged")


class TestSetAsideNameSurvivesAKill(_KillPointsBase):
    """A kill right after the restored database was moved aside (its copy
    gone): the replay no longer found the live file and the result stopped
    saying where the restored database went. Guard of the new module."""

    def test_replay_still_names_it(self):
        inst, live, _cfg = self.fixture()
        _s, m = restore_io.apply_pending_restore(inst, log=_quiet)
        os.unlink(m["db"]["snapshot"])
        restore_io.request_rollback(inst, m, "migration failed")
        real = os.replace

        def replace(a, b, *x, **k):
            real(a, b, *x, **k)
            if ".restore-failed." in str(b) and not str(b).endswith(("-wal", "-shm",
                                                                      "-journal")):
                raise _Kill()
        with mock.patch("os.replace", replace):
            with self.assertRaises(_Kill):
                restore_io.apply_pending_restore(inst, log=_quiet)
        self.assertEqual(restore_io.apply_pending_restore(inst, log=_quiet)[0], "rolled_back")
        res = restore_io.read_result(inst)
        self.assertFalse(res["rolled_back"])
        self.assertTrue(res.get("set_aside"), res)
        self.assertTrue((live.parent / res["set_aside"]).exists())
        self.assertIn(res["set_aside"], res["manual"])


class TestNtfyAppliedIsSaidOnCancel(_Restore):
    """The backup's notification-server settings apply at once (ntfy is its
    own service) — yet Cancel and the expiry said "nothing was changed"."""

    NTFY = 'base-url: "http://192.0.2.77:2586"\nlisten-http: ":2586"\n'

    def test_cancel_says_the_ntfy_settings_stay(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes,
                        extra={"ntfy/server.yml": self.NTFY})
        with mock.patch("ntfy_installer.install_status",
                        return_value={"state": "aerodrome_managed"}), \
                mock.patch("ntfy_installer.update_config", return_value={"ok": True}) as uc:
            status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertEqual(uc.call_count, 1)
        self.assertTrue(self.marker()["ntfy_applied"])
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual(status, 200, body)
        self.assertIn("notification server's settings from the backup were already applied",
                      body["message"])
        self.assertNotIn("Nothing was changed", body["message"])
        self.assertTrue(self.result()["ntfy_applied"])

    def test_without_ntfy_cancel_still_says_nothing_changed(self):
        # Guard
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertIn("Nothing was changed", body["message"])
        self.assertNotIn("ntfy_applied", self.result())


class TestOtherWritersWaitForARestore(_Waiting):
    """The demo feeder's port recovery committed config.yaml while a restore
    waited (the swap then undid it without a word); main's config-backup
    prune counted pre-restore config copies against its keep-5 (a pair lost
    its settings half, and Put this back then quietly put back the database
    only)."""

    def test_demo_port_recovery_waits(self):
        import collector
        calls = []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a)):
            port = collector._demo_port_recovery_write({"data": {}}, self.cfg_path, 30105, [])
        self.assertIsNone(port)
        self.assertEqual(calls, [])
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)

    def test_mains_config_backup_prune_keeps_pre_restore_copies(self):
        import main
        copies = sorted(p.name for p in self.inst.glob("config.yaml.bak.*.pre-restore"))
        for i in range(3):
            (self.inst / f"config.yaml.bak.2026010{i + 1}-000000.pre-restore").write_text("x")
        for i in range(7):
            (self.inst / f"config.yaml.bak.2099010{i + 1}-000000").write_text("x")
        with mock.patch.object(main, "CONFIG_PATH", self.cfg_path), \
                mock.patch.object(main, "BASE_DIR", self.inst), \
                contextlib.redirect_stdout(io.StringIO()):
            main._prune_config_backups()
        left = {p.name for p in self.inst.glob("config.yaml.bak.*")}
        for i in range(3):
            self.assertIn(f"config.yaml.bak.2026010{i + 1}-000000.pre-restore", left)
        for name in copies:
            self.assertIn(name, left)
        self.assertEqual(len([n for n in left if not n.endswith(".pre-restore")]), 5)


class TestPutBackSaysWhatItSkipped(_Restore):

    def test_a_pair_without_its_settings_half_says_so(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.restore()
        m = self.marker()
        self.start_half()
        os.unlink(m["config"]["snapshot"])
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=m["ts"], background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 200, body)
        self.assertTrue(any("no settings copy" in s["reason"] for s in body["skipped"]),
                        body["skipped"])


class TestLogFolderFromAnotherMachine(_MainRestore):
    """A backup whose logging.dir can't be created here: the restore said
    ok, then every later start (and `main.py stop`) crashed in
    setup_logging before anything — no marker left to roll back, a restart
    loop with no web UI."""

    def test_this_machines_log_folder_is_kept_with_a_note(self):
        theirs = self.backup_cfg()
        theirs["logging"]["dir"] = "/nonexistent-b4c-example/usb-stick/aerodrome-logs"
        make_backup_zip(self.zip, self.backup_text(theirs), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertTrue(any("logging.dir" in n for n in body["notes"]), body["notes"])
        staged = yaml.safe_load(Path(self.marker()["config"]["staged"]).read_text())
        self.assertEqual(staged["logging"]["dir"], self.cfg["logging"]["dir"])

    def test_setup_logging_never_fails_on_its_folder(self):
        ro = self.work / "ro"
        ro.mkdir()
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        cfg = dict(self.cfg)
        cfg["logging"] = dict(cfg["logging"], dir=str(ro / "missing" / "logs"))
        got = []
        with mock.patch("logging.basicConfig", side_effect=lambda **k: got.append(k)):
            self.main.setup_logging(cfg)
        files = [h for h in got[0]["handlers"] if hasattr(h, "baseFilename")]
        for h in got[0]["handlers"]:
            if hasattr(h, "close"):
                h.close()
        if os.access(ro, os.W_OK):
            self.skipTest("running as root")
        self.assertEqual([Path(h.baseFilename).parent for h in files], [self.inst / "logs"])


class TestUnreadableBackupSettingsOfferDatabaseOnly(_Restore):
    """A backup whose config.yaml isn't UTF-8 or is over 256 KB: 3.4.148
    skipped it and restored the database; the review's build refused the
    whole restore with 400 and no hint that the database could still be
    restored on its own."""

    def check(self, cfg_bytes):
        make_backup_zip(self.zip, None, self.db_bytes,
                        extra={"config.yaml": cfg_bytes})
        status, body = self.restore()
        self.assertEqual(status, 422, body)
        self.assertTrue(body["database_only"])
        self.assertIn("restore just the database", body["message"])
        self.assert_untouched()
        status, body = self.restore(keep_current_config=True)
        self.assertEqual(status, 200, body)
        self.start_half()
        self.assertEqual(count_rows(self.live), (40, "B00000"))

    def test_latin1(self):
        self.check(self.backup_text().replace("# backup settings",
                                              "# Einstellungen für\xfc").encode("latin-1"))

    def test_oversized(self):
        self.check((self.backup_text() + "# " + "x" * (300 * 1024) + "\n").encode())


def _zip_with_method(path, entries, method_of=None, flag_of=None):
    """A backup zip whose entries use a given compression method (or carry
    the "encrypted" flag) — re-packed by some tool."""
    method_of, flag_of = method_of or {}, flag_of or {}
    with zipfile.ZipFile(str(path), "w") as zf:
        for name, data in entries.items():
            m = method_of.get(name, zipfile.ZIP_DEFLATED)
            if m in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2,
                     zipfile.ZIP_LZMA):
                zf.writestr(zipfile.ZipInfo(name, (2026, 10, 1, 0, 0, 0)), data,
                            compress_type=m)
            else:
                zf.writestr(name, data, compress_type=zipfile.ZIP_STORED)
    raw = bytearray(Path(path).read_bytes())
    with zipfile.ZipFile(str(path)) as zf:
        infos = {i.filename: i for i in zf.infolist()}
    cd = raw.rfind(b"PK\x05\x06")
    cd_off = int.from_bytes(raw[cd + 16:cd + 20], "little")
    for name, info in infos.items():
        patch = {}
        if name in method_of and method_of[name] not in (0, 8, 12, 14):
            patch["method"] = method_of[name]
        if name in flag_of:
            patch["flag"] = flag_of[name]
        if not patch:
            continue
        lh = info.header_offset
        pos = cd_off
        while raw[pos:pos + 4] == b"PK\x01\x02":
            n = int.from_bytes(raw[pos + 28:pos + 30], "little")
            e = int.from_bytes(raw[pos + 30:pos + 32], "little")
            c = int.from_bytes(raw[pos + 32:pos + 34], "little")
            if raw[pos + 46:pos + 46 + n].decode() == name:
                break
            pos += 46 + n + e + c
        if "method" in patch:
            raw[lh + 8:lh + 10] = patch["method"].to_bytes(2, "little")
            raw[pos + 10:pos + 12] = patch["method"].to_bytes(2, "little")
        if "flag" in patch:
            for off in (lh + 6, pos + 8):
                f = int.from_bytes(raw[off:off + 2], "little") | patch["flag"]
                raw[off:off + 2] = f.to_bytes(2, "little")
    Path(path).write_bytes(bytes(raw))


class TestCompressionBombs(_Restore):
    """Every size check of the restore relies on the size an entry declares,
    and zipfile only stops at it for stored and deflated entries: a 546-byte
    zip of bzip2 entries made the server allocate about 1 GB before it
    refused (and an encrypted or unknown-method entry gave 500)."""

    def manifest(self):
        return json.dumps({"manifest_version": 1, "created_at_iso": "2026-10-01T00:00:00Z"})

    def cases(self):
        good = {"manifest.json": self.manifest(), "config.yaml": self.backup_text(),
                "aerodrome.db": self.db_bytes}
        return {
            "bzip2 manifest": (good, {"manifest.json": zipfile.ZIP_BZIP2}, {}),
            "lzma config": (good, {"config.yaml": zipfile.ZIP_LZMA}, {}),
            "bzip2 database": (good, {"aerodrome.db": zipfile.ZIP_BZIP2}, {}),
            "encrypted database": (good, {}, {"aerodrome.db": 0x1}),
            "unknown method": (good, {"config.yaml": 99}, {}),
        }

    def test_refused_before_anything_is_decompressed(self):
        real = zipfile._get_decompressor
        used = []

        def spy(compress_type):
            used.append(compress_type)
            return real(compress_type)
        for label, (entries, methods, flags) in self.cases().items():
            with self.subTest(label):
                _zip_with_method(self.zip, entries, methods, flags)
                used.clear()
                with mock.patch.object(zipfile, "_get_decompressor", spy):
                    for how in ("path", "upload"):
                        status, body = (self.restore() if how == "path"
                                        else self.upload(self.zip.read_bytes()))
                        self.assertEqual(status, 400, (label, how, body))
                        self.assertEqual(body["code"], "bad_backup", body)
                        self.assert_untouched()
                self.assertEqual([t for t in used if t not in (0, 8)], [], label)
                self.assertEqual(self.restart_calls(), [])


class TestDatabaseOnlyKeepsThisMachinesNtfy(_Restore):
    """"Restore database only (keep this machine's settings)" still applied
    the backup's notification-server settings — and could install ntfy
    listening on every interface."""

    NTFY = 'base-url: "http://192.0.2.77:2586"\nlisten-http: "0.0.0.0:2586"\n'

    def run_db_only(self, state, with_db=True):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes if with_db else None,
                        extra={"ntfy/server.yml": self.NTFY})
        with mock.patch("ntfy_installer.install_status", return_value={"state": state}), \
                mock.patch("ntfy_installer.update_config",
                           return_value={"ok": True}) as uc, \
                mock.patch("ntfy_installer.install", return_value={"ok": True}) as inst:
            status, body = self.restore(keep_current_config=True)
        self.assertEqual((uc.call_count, inst.call_count), (0, 0), body)
        return status, body

    def test_managed_and_not_installed(self):
        for state in ("aerodrome_managed", "not_installed"):
            with self.subTest(state):
                status, body = self.run_db_only(state)
                self.assertEqual(status, 200, body)
                self.assertIn("ntfy/server.yml", [s["file"] for s in body["skipped"]])
                self.assertNotIn("ntfy_config", body["restored"])
                self.assertEqual(_call(self.ep("/api/backup/staged-restore/cancel")())[0], 200)
                self.release_lock()

    def test_a_backup_without_a_database(self):
        status, body = self.run_db_only("aerodrome_managed", with_db=False)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "nothing_to_restore")
        self.assert_untouched()


class TestNtfyOnlyAfterEveryCheck(_Restore):
    """Guard (coverage). B5's binding rule: the notification server's part runs only after
    every check of the backup passed — a refused restore must not have
    rewritten (or installed) it while answering "Nothing was changed". No
    test pinned the order."""

    NTFY = 'base-url: "http://192.0.2.77:2586"\nlisten-http: ":2586"\n'

    def mocks(self):
        return (mock.patch("ntfy_installer.install_status",
                           return_value={"state": "aerodrome_managed"}),
                mock.patch("ntfy_installer.update_config", return_value={"ok": True}),
                mock.patch("ntfy_installer.install", return_value={"ok": True}))

    def test_refusals_leave_ntfy_alone(self):
        from schema_migrations import CURRENT_SCHEMA_VERSION
        newer = self.work / "newer.db"
        make_history_db(newer, 0xC00000, 5)
        c = sqlite3.connect(str(newer))
        c.execute("INSERT INTO schema_version VALUES (?, 0, 'future', '9.9.9')",
                  (CURRENT_SCHEMA_VERSION + 1,))
        c.commit()
        c.close()
        for label, db, expect in (("damaged", "corrupt", 400),
                                  ("newer schema", newer.read_bytes(), 409),
                                  ("disk space", self.db_bytes, 507)):
            with self.subTest(label):
                make_backup_zip(self.zip, self.backup_text(),
                                self.db_bytes if db == "corrupt" else db,
                                extra={"ntfy/server.yml": self.NTFY})
                if db == "corrupt":
                    corrupt_member(self.zip, "aerodrome.db")
                a, b, c_ = self.mocks()
                space = (mock.patch.object(restore_io, "_disk_free", return_value=1024)
                         if label == "disk space" else contextlib.nullcontext())
                with a as st, b as uc, c_ as ins, space:
                    status, body = self.restore()
                self.assertEqual(status, expect, body)
                self.assertIn("Nothing was changed", body["message"])
                self.assertEqual((uc.call_count, ins.call_count), (0, 0), label)
                self.assertEqual(list(self.inst.glob("ntfy-server.yml.from-backup.*")), [])
                self.assert_untouched()

    def test_a_good_backup_applies_ntfy_once_after_the_marker(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes,
                        extra={"ntfy/server.yml": self.NTFY})
        seen = []

        def update_config(**kw):
            seen.append(self.marker())
            return {"ok": True}
        a, _b, c_ = self.mocks()
        with a, c_, mock.patch("ntfy_installer.update_config", side_effect=update_config):
            status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["phase"], "staged")


def _alias_chain(depth, width=9):
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for d in range(1, depth + 1):
        lines.append(f"a{d}: &a{d} [" + ", ".join([f"*a{d - 1}"] * width) + "]")
    return "\n".join("  " + ln for ln in lines)


class TestYamlAliasesAreRefused(_Restore):
    """A config.yaml with nested YAML aliases made the read-back comparison
    run for exponential time (depth 10: 30 s, depth 13: ~40 min) with the
    maintenance lock held and the GIL never released — the whole server
    froze. Aerodrome never writes anchors; incoming text with them is
    refused."""

    def aliased_text(self, depth):
        cfg = self.backup_cfg(port=18301)          # another port: the compare runs
        return (self.backup_text(cfg) + "example_aliases:\n" + _alias_chain(depth) + "\n")

    def test_restore_upload_and_put_back_refuse_quickly(self):
        text = self.aliased_text(8)
        make_backup_zip(self.zip, text, self.db_bytes)
        for how in ("path", "upload"):
            with self.subTest(how):
                t0 = time.monotonic()
                status, body = (self.restore() if how == "path"
                                else self.upload(self.zip.read_bytes()))
                self.assertLess(time.monotonic() - t0, 2.0)
                self.assertEqual(status, 422, body)
                self.assertIn("anchors or aliases", body["message"])
                self.assertTrue(body["database_only"])
                self.assert_untouched()
        snap = self.inst / "config.yaml.bak.20260101-000000.pre-restore"
        snap.write_text(text)
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts="20260101-000000", background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 422, body)

    def test_config_import_refuses_them(self):
        from starlette.datastructures import UploadFile
        imp = self.ep("/api/config/import")
        status, body = _call(imp(file=UploadFile(file=io.BytesIO(
            self.aliased_text(8).encode()), filename="config.yaml")))
        self.release_lock()
        self.assertEqual(status, 400, body)
        self.assertIn("anchors or aliases", body["message"])
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)

    def test_a_live_config_with_an_anchor_still_starts(self):
        # Guard: only incoming text is refused.
        import main
        text = self.cfg_before.decode() + "example_anchor: &one 1\nexample_alias: *one\n"
        self.cfg_path.write_text(text)
        with mock.patch.object(main, "CONFIG_PATH", self.cfg_path), \
                mock.patch.object(main, "BASE_DIR", self.inst), \
                mock.patch.object(main, "PID_FILE", self.inst / ".tracker.pid"), \
                contextlib.redirect_stdout(io.StringIO()):
            cfg = main.load_config("start")
        self.assertEqual(cfg["example_alias"], 1)
        self.assertFalse((self.inst / ".backups" / "config-recovery.json").exists())


class TestSchemaVersionMustBeATable(_Restore):
    """A view named schema_version (SELECT 999) slipped past the "made by a
    newer Aerodrome" check (it only looked at tables) while the migrations
    read 999 from it; a text version column gave 500."""

    def db_with(self, sql):
        src = self.work / "odd.db"
        if src.exists():
            src.unlink()
        make_history_db(src, 0xC00000, 5)
        c = sqlite3.connect(str(src))
        c.executescript(sql)
        c.commit()
        c.close()
        return src.read_bytes()

    def test_view_and_text_version_are_400(self):
        for label, sql in (
                ("view", "DROP TABLE schema_version; "
                         "CREATE VIEW schema_version AS SELECT 999 AS version;"),
                ("text", "DROP TABLE schema_version; "
                         "CREATE TABLE schema_version (version TEXT); "
                         "INSERT INTO schema_version VALUES ('abc');")):
            with self.subTest(label):
                make_backup_zip(self.zip, self.backup_text(), self.db_with(sql))
                status, body = self.restore()
                self.assertEqual(status, 400, body)
                self.assertEqual(self.restart_calls(), [])
                self.assert_untouched()


class TestExtraHostnamesAreNamed(_Restore):
    """DECISION note (the maintainer's call is open): a backup can widen the
    web UI's Host allowlist (web.allowed_hosts, notifications.public_url's
    host); the restore now names what it adds."""

    def test_note(self):
        theirs = self.backup_cfg()
        theirs["web"]["allowed_hosts"] = "rebind.example.com"
        theirs.setdefault("notifications", {})["public_url"] = "https://other.example.net/"
        make_backup_zip(self.zip, self.backup_text(theirs), self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        note = next((n for n in body["notes"] if "hostnames" in n), "")
        self.assertIn("rebind.example.com", note)
        self.assertIn("other.example.net", note)


class TestOddCharactersInTheDatabaseFolder(_Restore):
    """copy_database built its SQLite URI from the raw path: a folder named
    "a#b" opened ".../a" read-write (the read-only flags fell into the URI
    fragment), left a stray empty file there and refused the put-back."""

    def db_dir(self):
        d = self.work / "data#one?two"
        d.mkdir(exist_ok=True)
        return d

    def test_put_back_reads_the_copy(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        ts = self.marker()["ts"]
        self.start_half()
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=ts, background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 200, body)
        self.assertEqual(count_rows(self.marker()["db"]["staged"]), (30, "A00000"))
        self.assertFalse((self.work / "data").exists(), "a stray file was created")


class TestManifestTextIsCleaned(_Restore):
    """The manifest's created_at_iso went verbatim — any JSON type, newlines
    included — into the marker, the status, the result and a log line (a
    backup could forge log lines); a deeply nested config.yaml gave 500."""

    def test_odd_dates_become_none(self):
        for label, value in (("newline", "2026-10-01T00:00:00Z).\n2026-10-22 [adsb.main] "
                                         "ERROR: FORGED"),
                             ("object", {"x": [1, 2]}), ("long", "2026-10-01T00:00:00Z" * 9)):
            with self.subTest(label):
                make_backup_zip(self.zip, self.backup_text(), self.db_bytes,
                                manifest=json.dumps({"manifest_version": 1,
                                                     "created_at_iso": value,
                                                     "aerodrome_version": {"v": 1}}))
                self.assertEqual(self.restore()[0], 200)
                m = self.marker()
                self.assertIsNone(m["backup_created_at_iso"])
                self.assertIsNone(m["backup_aerodrome_version"])
                st = self.ep("/api/maintenance/status", "GET")()
                self.assertIsNone(st["restore"]["pending"]["backup_created_at_iso"])
                self.assertEqual(_call(self.ep("/api/backup/staged-restore/cancel")())[0], 200)
                self.release_lock()
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        self.assertEqual(self.marker()["backup_created_at_iso"], "2026-10-01T00:00:00Z")

    def test_deep_yaml_is_422(self):
        deep = "a: " + "[" * 5000 + "]" * 5000 + "\n"          # valid YAML, nested deep
        make_backup_zip(self.zip, deep, self.db_bytes)
        status, body = self.restore()
        self.assertEqual(status, 422, body)
        self.assertIn("nested too deeply", body["message"])
        self.assert_untouched()
        # ... and the same text is a reason, not a crash, wherever it is read
        with self.assertRaisesRegex(ValueError, "nested too deeply"):
            config_io.check_loadable(deep)


class TestRestorePathRevealsNothingOutside(_Restore):
    """The existence check ran before the scope check: 404 for a missing
    file and 403 for an existing one told any caller which files exist
    anywhere on the box."""

    def test_outside_is_always_403(self):
        for p in (self.cfg_path, self.work / "no-such-file.zip"):
            with self.subTest(str(p)):
                status, body = self.restore(path=p)
                self.assertEqual(status, 403, body)
        status, body = self.restore(path=self.inst / ".backups" / "missing.zip")
        self.assertEqual(status, 404, body)


class TestRollbackNeverReplaysTheRestoredWal(_KillPointsBase):
    """Guard (coverage). The rollback removes the RESTORED database's -wal before moving the
    copy back; no test left one there (every fixture closed cleanly), so a
    rollback without it 'rolled back' to the restored data plus the dying
    start's writes, and said the previous data was back."""

    def check(self, inst, live, cfg):
        self.assertEqual(count_rows(live), (100, "A00000"))
        self.assertEqual(_quick_check(live), "ok")
        for s in ("-wal", "-shm"):
            self.assertFalse(Path(f"{live}{s}").exists() and Path(f"{live}{s}").stat().st_size,
                             s)
        self.assertTrue(restore_io.read_result(inst)["rolled_back"])

    def test_a_rollback_asked_for_by_a_start_that_wrote(self):
        inst, live, cfg = self.fixture()
        _s, m = restore_io.apply_pending_restore(inst, log=_quiet)
        _child_wal(live, 0xC00000, 300)
        restore_io.request_rollback(inst, m, "migration failed")
        self.assertEqual(restore_io.apply_pending_restore(inst, log=_quiet)[0], "rolled_back")
        self.check(inst, live, cfg)

    def test_starts_that_died_after_writing(self):
        inst, live, cfg = self.fixture()
        self.assertEqual(restore_io.apply_pending_restore(inst, log=_quiet)[0], "applied")
        _child_wal(live, 0xC00000, 300)
        self.assertEqual(restore_io.apply_pending_restore(inst, log=_quiet)[0], "retry")
        _child_wal(live, 0xD00000, 300)
        self.assertEqual(restore_io.apply_pending_restore(inst, log=_quiet)[0], "rolled_back")
        self.check(inst, live, cfg)


class TestStartBranches(_MainRestore):
    """Guard (coverage). main.start's restore branches no test reached: the config re-read
    after the swap (the backup's receiver, watchlist and retention never
    took effect, and the next Settings save wrote the old settings back), an
    exception from the swap (every start re-raised it), a start that died
    and the retry that then succeeds (never finished — the next restart
    rolled a working restore back), a `rollback` marker with an unreadable
    config.yaml, and the collector stopped before a re-exec."""

    def receiver_ip(self, ga):
        return ga.call_args.args[0]["receiver"]["ip"]

    def test_the_restored_settings_are_the_ones_that_run(self):
        self.stage()
        ga = mock.Mock(return_value=object())
        self.assertEqual(self.start(extra=[mock.patch.object(self.main, "get_app", ga)]),
                         "served")
        self.assertEqual(self.receiver_ip(ga), "192.0.2.44")

    def test_after_a_rollback_the_previous_settings_run(self):
        self.stage()
        boom = mock.patch.object(self.main, "init_db",
                                 side_effect=sqlite3.DatabaseError("file is not a database"))
        self.assertEqual(self.start(extra=[boom]), "reexec")
        ga = mock.Mock(return_value=object())
        self.assertEqual(self.start(extra=[mock.patch.object(self.main, "get_app", ga)]),
                         "served")
        self.assertEqual(self.receiver_ip(ga), "192.0.2.10")

    def test_an_exception_from_the_swap_rolls_back(self):
        self.stage()
        real = config_io.move_into_place
        calls = []

        def once(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise config_io.ConfigWriteError("Couldn't save config.yaml: injected")
            return real(*a, **k)
        self.assertEqual(self.start(extra=[mock.patch.object(config_io, "move_into_place",
                                                              once)]), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)
        self.assertTrue(self.result()["rolled_back"])
        self.assertIsNone(self.marker())

    def test_a_start_that_dies_then_a_good_retry(self):
        self.stage()
        die = mock.patch.object(self.main, "get_app", side_effect=MemoryError("killed"))
        with self.assertRaises(MemoryError):
            self.start(extra=[die])
        self.assertEqual(self.start(), "served")
        self.assertIsNone(self.marker())
        self.assertTrue(self.result()["ok"])
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertIn("# backup settings", self.last_good())
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (40, "B00000"))

    def test_a_rollback_marker_with_an_unreadable_config(self):
        self.stage()
        _s, m = restore_io.apply_pending_restore(self.inst, log=_quiet)
        restore_io.request_rollback(self.inst, m, "migration failed")
        self.cfg_path.write_text("web: [unclosed\n")
        (self.inst / ".backups" / "config.yaml.last-good").write_text(self.cfg_before.decode())
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = self.main.load_config("start")
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.10")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertFalse((self.inst / ".backups" / "config-recovery.json").exists())

    def test_the_collector_is_stopped_before_a_re_exec(self):
        events = []
        seen = []

        def collector(cfg, ev):
            events.append(ev)

        def execv(exe, argv):
            seen.append(all(e.is_set() for e in events) and bool(events))
            raise _Exec()
        # the previous settings listened elsewhere: this bind failure is the restore's
        text = self.cfg_path.read_text().replace("port: 18299", "port: 18298")
        self.cfg_path.write_text(text)
        self.stage()
        out = self.start(bind=lambda h, p: (False, "in use"),
                         extra=[mock.patch.object(self.main, "run_collector", collector),
                                mock.patch.object(self.main.os, "execv", side_effect=execv)])
        self.assertEqual(out, "reexec")
        self.assertEqual(seen, [True])


class TestASwapThatNeverMovedTheDatabase(_MainRestore):
    """When the swap failed before the database's rename (EIO, EACCES on
    the old -wal), or the staged database had vanished, the rollback put the
    pre-restore copy — taken at staging — over a live database that was
    never replaced: every sighting since staging lost, the copy used up,
    and "rolled back" reported. A vanished staged database also kept the
    restore from ever expiring."""

    def add_rows_after_staging(self):
        c = sqlite3.connect(str(self.live))
        c.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
                      [(f"{0xA10000 + i:06X}", 1792700000) for i in range(5)])
        c.commit()
        c.close()

    def assert_kept(self, snap):
        self.assertEqual(count_rows(self.live), (35, "A00000"))
        self.assertTrue(os.path.exists(snap), "the copy was used up")
        res = self.result()
        self.assertFalse(res["ok"])
        self.assertIn("aircraft_history.db", res.get("kept_current", []))
        self.assertIsNone(self.marker())

    def test_the_rename_fails(self):
        self.stage()
        snap = self.marker()["db"]["snapshot"]
        self.add_rows_after_staging()
        real = os.replace

        def replace(a, b, *x, **k):
            if str(b) == str(self.live) and "restore-staged" in str(a):
                raise OSError(errno.EIO, "Input/output error")
            return real(a, b, *x, **k)
        self.assertEqual(self.start(extra=[mock.patch("os.replace", replace)]), "served")
        self.assert_kept(snap)

    def test_the_old_wal_cant_be_removed(self):
        self.stage()
        snap = self.marker()["db"]["snapshot"]
        self.add_rows_after_staging()
        Path(f"{self.live}-wal").write_bytes(b"")
        real = os.unlink

        def unlink(p, *a, **k):
            if str(p) == f"{self.live}-wal":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real(p, *a, **k)
        self.assertEqual(self.start(extra=[mock.patch("os.unlink", unlink)]), "served")
        self.assert_kept(snap)

    def test_the_staged_database_vanished(self):
        self.stage()
        m = self.marker()
        self.add_rows_after_staging()
        os.unlink(m["db"]["staged"])
        self.assertIsNotNone(restore_io.pending(self.inst))
        # it still expires (nothing was ever moved) ...
        self.assertTrue(restore_io.is_expired(m, now=time.time() + 7200))
        # ... and a start keeps the live database
        self.assertEqual(self.start(), "served")
        self.assert_kept(m["db"]["snapshot"])


class TestRestoreChecksThatHadNoTest(_Restore):
    """Guard (coverage). The free-space check before staging and quick_check(5) on the staged
    database had no test (the only disk-full test fills the disk part-way,
    and the 'damaged' backups fail on their zip CRC)."""

    def test_not_enough_space_is_507_before_anything_is_written(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        with mock.patch.object(restore_io, "_disk_free", return_value=1024 * 1024), \
                mock.patch.object(restore_io, "stage_database",
                                  side_effect=AssertionError("staged before the check")):
            status, body = self.restore()
        self.assertEqual(status, 507, body)
        self.assertEqual(body["code"], "no_space")
        self.assertIn("Not enough free disk space to restore", body["message"])
        self.assertEqual(self.restart_calls(), [])
        self.assert_untouched()

    def test_a_valid_header_with_damaged_pages_is_400(self):
        # quick_check reports a damaged index page, and raises on a damaged table
        # page. Either way the backup is refused.
        for name, wanted in (("idx_all_seen", "quick_check"), ("all_sightings", "can't be opened")):
            with self.subTest(damaged=name):
                src = self.work / f"damaged-{name}.db"
                make_history_db(src, 0xC00000, 4000)
                c = sqlite3.connect(str(src))
                page_size = c.execute("PRAGMA page_size").fetchone()[0]
                leaves = [r[0] for r in c.execute("SELECT pageno FROM dbstat WHERE name=? "
                                                  "AND pagetype='leaf' ORDER BY pageno", (name,))]
                c.close()
                page = leaves[len(leaves) // 2]
                raw = bytearray(src.read_bytes())
                raw[(page - 1) * page_size:page * page_size] = b"\xA5" * page_size
                make_backup_zip(self.zip, self.backup_text(), bytes(raw))
                status, body = self.restore()
                self.assertEqual(status, 400, body)
                self.assertIn(wanted, body["message"])
                self.assertEqual(self.restart_calls(), [])
                self.assert_untouched()


class TestSmallGuards(_Restore):
    """Guards (coverage): Decision 4 on a put-back's settings copy, the
    .uploads spool as a restore source, a marker phase this release
    doesn't know (left alone — the N-1 rule for a downgrade)."""

    def test_put_back_keeps_this_machines_port(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.restore()
        ts = self.marker()["ts"]
        self.start_half()
        # this machine now listens on 18311 (config.yaml and the running config)
        text = self.cfg_path.read_text().replace("port: 18299", "port: 18311")
        self.cfg_path.write_text(text)
        self.server.CONFIG["web"]["port"] = 18311
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=ts, background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 200, body)
        staged = yaml.safe_load(Path(self.marker()["config"]["staged"]).read_text())
        self.assertEqual(staged["web"]["port"], 18311)
        self.assertTrue(any("web.port" in n and "18311" in n for n in body["notes"]))

    def test_put_back_of_an_invalid_copy_is_422_database_only(self):
        bad = example_cfg(self.live)
        bad["receiver"]["port"] = 99999
        ts = "20260101-000000"
        (self.inst / f"config.yaml.bak.{ts}.pre-restore").write_text(yaml.safe_dump(bad))
        shutil.copyfile(self.live, self.inst / f"aircraft_history.db.bak.{ts}.pre-restore")
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=ts, background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 422, body)
        self.assertTrue(body["database_only"])

    def test_the_uploads_spool_is_not_a_restore_source(self):
        d = self.inst / ".backups" / ".uploads"
        d.mkdir(exist_ok=True)
        z = d / "upload-x.zip"
        make_backup_zip(z, self.backup_text(), self.db_bytes)
        status, body = self.restore(path=z)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["code"], "not_a_backup")
        z.unlink()
        self.assert_untouched()

    def test_an_unknown_phase_is_left_alone(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.restore()
        for phase in ("future", "future2"):
            with self.subTest(phase):
                m = self.marker()
                m["phase"] = phase
                raw = json.dumps(m)
                (self.inst / ".backups" / MARKER).write_text(raw)
                state, _m = restore_io.apply_pending_restore(self.inst, log=_quiet)
                self.assertEqual(state, "unknown")
                self.assertEqual((self.inst / ".backups" / MARKER).read_text(), raw)
                self.assertTrue(os.path.exists(m["db"]["staged"]))
                self.assertTrue(os.path.exists(m["db"]["snapshot"]))
                self.assertEqual(count_rows(self.live), (30, "A00000"))


class TestSettingsWhileStagedErrorShape(_Waiting):
    """Guard (coverage): the 409's path-less error the Settings page's save
    handler reads (mockup state 5)."""

    def test_put_config_409_carries_a_pathless_error(self):
        new = json.loads(json.dumps(self.server.CONFIG))
        put = self.ep("/api/config", "PUT")
        status, body = _call(put(put.__annotations__["payload"](config=new)))
        self.assertEqual(status, 409, body)
        self.assertEqual(body["errors"], [{"path": "", "message":
                                           self.server.RESTORE_WAITING_MESSAGE}])



# ===========================================================================
# Review round 2 (independent gap check of the review build): each test
# names what that build did before its fix; "Guard (coverage)" marks code
# that was already right there but that no test pinned.
# ===========================================================================

class TestTheWalMayGoWhileItIsMeasured(_Restore):
    """The space check looked at <db>-wal, then measured it. SQLite removes
    the -wal when a database's last connection closes — the collector opens
    and closes one on every poll — and a close landing between the two
    failed the restore: 500 restore_failed, "No such file or directory:
    …-wal" (nothing changed; a retry worked)."""

    def test_a_wal_removed_between_the_look_and_the_measure(self):
        wal = _child_wal(self.live, 0xE00000, 5)
        real_is_file = Path.is_file
        gone = []

        def is_file(path):
            there = real_is_file(path)
            if there and not gone and os.path.realpath(str(path)) == os.path.realpath(str(wal)):
                # the collector's poll ends right now: its close checkpoints
                # the -wal into the database and removes it
                c = sqlite3.connect(str(self.live))
                c.execute("SELECT count(*) FROM all_sightings").fetchone()
                c.close()
                gone.append(not wal.exists())
            return there
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        with mock.patch.object(Path, "is_file", is_file):
            status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertIn(gone, ([], [True]))             # (the fixed check never looks first)
        self.assertEqual(count_rows(self.marker()["db"]["snapshot"]), (35, "A00000"))


class TestABindWaitStartIsReallyNotCounted(_BindBase):
    """A start that waits for the web address "doesn't count against" the
    restore — but it was recorded as 0 starts, and the next start read 0 as
    "missing" (= 1): after one bind wait, a single start that died rolled a
    good restore back ("didn't finish starting (2 attempts)"), where without
    the wait it is retried."""

    def die(self):
        return mock.patch.object(self.main, "get_app", side_effect=MemoryError("killed"))

    def test_after_a_bind_wait_one_dying_start_is_retried(self):
        self.stage()
        self.assertEqual(self.start(bind=lambda h, p: (False, "in use")), "exit 2")
        self.assertEqual(self.marker()["starts"], 0)
        with self.assertRaises(MemoryError):
            self.start(extra=[self.die()])
        self.assertEqual(self.marker()["phase"], "swapped")
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (40, "B00000"))
        self.assertTrue(self.result()["ok"])

    def test_after_a_bind_wait_two_dying_starts_roll_back(self):
        # Guard: the same budget as without the wait (MAX_SWAPPED_STARTS).
        self.stage()
        self.assertEqual(self.start(bind=lambda h, p: (False, "in use")), "exit 2")
        for _ in range(2):
            with self.assertRaises(MemoryError):
                self.start(extra=[self.die()])
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertIn("didn't finish starting on the restored backup (2 attempts)",
                      self.result()["error"])


class _DatabaseLink(_Restore):
    """data.db_file a symlink (in the install) to the database in another
    folder — the history moved off the install's disk with `ln -s` (here the
    same filesystem: the listing doesn't depend on it)."""
    LINK_NAME = "aircraft_history.db"
    OLD = ("20200101-120000", "20200102-120000", "20200103-120000", "20200104-120000")

    def db_dir(self):
        d = self.work / "data"
        d.mkdir(exist_ok=True)
        return d

    def customize(self, cfg):
        self.link = self.inst / self.LINK_NAME
        self.link.symlink_to(self.live)
        cfg["data"]["db_file"] = str(self.link)
        # copies a release before 3.4.149 made beside the LINK (db_path.with_name)
        old_settings = "# settings then\n" + yaml.safe_dump(cfg, sort_keys=False)
        for ts in self.OLD:
            shutil.copyfile(self.live, self.inst / f"{self.LINK_NAME}.bak.{ts}.pre-restore")
            (self.inst / f"config.yaml.bak.{ts}.pre-restore").write_text(old_settings)

    def old_db_copies(self):
        return sorted(p.name for p in self.inst.glob(f"{self.LINK_NAME}.bak.*.pre-restore"))

    def listed(self):
        body = _call(self.ep("/api/backup/pre-restore", "GET")())[1]
        return {s["timestamp"]: sorted(f["kind"] for f in s["files"])
                for s in body["snapshots"]}


class TestCopiesBesideADatabaseLinkStayManaged(_DatabaseLink):
    """The review build looked for database copies only beside the link's
    TARGET: the copies earlier releases made beside the link went unlisted,
    unpruned (4 left with keep 3) and unpurged, and each pair showed only
    its settings half — "Put this back" would have restored settings only."""

    def test_listed_as_whole_pairs_pruned_and_purged(self):
        # get_app's startup prune kept the newest 3 of each kind
        self.assertEqual(self.old_db_copies(),
                         [f"{self.LINK_NAME}.bak.{ts}.pre-restore" for ts in self.OLD[1:]])
        self.assertEqual(self.listed(), {ts: ["config", "database"] for ts in self.OLD[1:]})
        st, body = _call(self.ep("/api/backup/pre-restore/purge")())
        self.release_lock()
        self.assertEqual(st, 200, body)
        self.assertEqual(body["deleted_count"], 6, body)
        self.assertEqual(self.old_db_copies(), [])
        self.assertEqual(self.listed(), {})

    def test_put_back_finds_the_copy_beside_the_link(self):
        c = sqlite3.connect(str(self.live))
        c.executemany("INSERT INTO all_sightings (icao, seen_at) VALUES (?, ?)",
                      [(f"{0xD00000 + i:06X}", 1792700000) for i in range(5)])
        c.commit()
        c.close()
        ts = self.OLD[-1]
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=ts, background_tasks=None))
        self.release_lock()
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("skipped"), [], body)
        m = self.marker()
        self.assertEqual(count_rows(m["db"]["staged"]), (30, "A00000"))
        self.assertTrue(m["config"])
        self.assertEqual(self.start_half(), "applied")
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(count_rows(self.live), (30, "A00000"))

    def test_the_new_copy_beside_the_target_is_listed_and_purged(self):
        # (the review build's fix itself had no test: listing beside the link only passed)
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        snap = self.marker()["db"]["snapshot"]
        self.assertEqual(Path(snap).parent, self.live.parent)
        ts = self.marker()["ts"]
        self.assertEqual(self.start_half(), "applied")
        self.assertEqual(self.listed()[ts], ["config", "database"])
        st, body = _call(self.ep("/api/backup/pre-restore/purge")())
        self.release_lock()
        self.assertEqual(st, 200, body)
        self.assertFalse(os.path.exists(snap), "the copy beside the target wasn't purged")
        self.assertEqual(self.listed(), {})


class TestCopiesBesideADifferentlyNamedLink(_DatabaseLink):
    """The same with a link whose name differs from its target's: the prune
    sorted one kind by NAME, so the old copies beside the link
    ("tracks.db.bak.…") outranked the newest copy beside the target
    ("aircraft_history.db.bak.…") — the newest was deleted first."""
    LINK_NAME = "tracks.db"

    def test_the_prune_keeps_the_newest_by_date(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        snap = Path(self.marker()["db"]["snapshot"])
        self.assertEqual(self.start_half(), "applied")
        self.server._prune_pre_restore_snapshots()
        self.assertTrue(snap.exists(), "the newest database copy was pruned")
        self.assertEqual(self.old_db_copies(),
                         [f"tracks.db.bak.{ts}.pre-restore" for ts in self.OLD[2:]])


class TestOnlyASwapCutShortIsFinished(_MainRestore):
    """finish_config_swap took "config.yaml unreadable and different from
    its pre-restore copy" as a start cut short inside the swap's own write.
    A config.yaml broken by hand while a restore waited matched that too: an
    EXPIRED restore was switched in at the next start (its hour ignored,
    with a log line about an interrupted start), and an update still being
    confirmed no longer went first. Now only a start that recorded beginning
    the switch (swap_started) — or that moved a file — is finished."""

    def age(self, sec):
        m = self.marker()
        m["staged_at"] = time.time() - sec
        (self.inst / ".backups" / MARKER).write_text(json.dumps(m))

    def break_config_by_hand(self):
        (self.inst / ".backups" / "config.yaml.last-good").write_text(self.cfg_before.decode())
        self.cfg_path.write_text("web: [unclosed\n")

    def test_an_expired_restore_is_dropped_not_switched_in(self):
        self.stage()
        self.age(7200)
        self.break_config_by_hand()
        self.assertEqual(self.start(), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertTrue(self.result().get("expired"), self.result())
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)   # recovered from last-good
        self.assertIsNone(self.marker())

    def test_an_update_being_confirmed_still_goes_first(self):
        self.stage()
        self.break_config_by_hand()
        journal = mock.patch.object(self.main._updater, "pending",
                                    return_value={"state": "committing", "to_version": "x"})
        with journal:
            self.assertEqual(self.start(real_db=False), "served")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        self.assertEqual(self.marker()["phase"], "staged")
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)


class TestATornConfigOnlySwapIsFinished(TestTornInPlaceSettingsWrite):
    """Guard (coverage): a restore of SETTINGS ONLY (no database to move
    first) torn inside the swap's in-place write, with no last-good copy and
    found after the hour — the start that began the switch recorded it, so
    the next one finishes it rather than exiting on "no usable copy" every
    time (or dropping it as expired)."""

    test_a_torn_write_is_finished_not_recovered = None

    def test_settings_only(self):
        make_backup_zip(self.zip, "# backup settings\n" + yaml.safe_dump(self.backup_cfg(),
                                                                         sort_keys=True), None)
        status, body = self.restore()
        self.assertEqual(status, 200, body)
        self.assertEqual(self.marker()["db"], {})

        def tear(staged, name):
            old = Path(staged.target).read_bytes()
            for cut in range(len(staged.data) // 4, len(staged.data), 13):
                try:
                    config_io.check_loadable((staged.data[:cut] + old[cut:])
                                             .decode("utf-8", "replace"))
                except ValueError:
                    break
            else:
                self.fail("no cut point leaves an unreadable file")
            fd = os.open(str(staged.target), os.O_WRONLY)
            try:
                os.write(fd, staged.data[:cut])
            finally:
                os.close(fd)
            raise _Kill()
        with mock.patch.object(config_io, "_write_in_place", tear):
            with self.assertRaises(_Kill):
                self.start()
        with self.assertRaises(ValueError):
            config_io.check_loadable(self.cfg_path.read_text(errors="replace"))
        self.assertFalse((self.inst / ".backups" / "config.yaml.last-good").exists())
        m = self.marker()
        self.assertTrue(m.get("swap_started"))
        m["staged_at"] = time.time() - 7200                  # ... and an hour offline
        (self.inst / ".backups" / MARKER).write_text(json.dumps(m))
        self.assertEqual(self.start(), "served")
        self.assertIn("# backup settings", self.cfg_path.read_text())
        self.assertTrue(self.result()["ok"], self.result())
        self.assertFalse((self.inst / ".backups" / "config-recovery.json").exists())
        self.assertEqual(count_rows(self.live), (30, "A00000"))


class _SettingsOnlyAcrossFilesystems(_KillPointsBase):
    """A settings-only restore with config.yaml linked onto another
    filesystem: its swap (and its rollback) WRITE config.yaml, then remove
    the staged copy (or the pre-restore copy)."""

    def setUp(self):
        super().setUp()
        self.xfs = _xfs_tmpdir(self)

    def fixture(self):
        inst, live, cfg = self.base_fixture(cfg_link_dir=self.xfs)
        restore_io.prepare(
            inst, db_live=live, config_live=cfg, source="example.zip", manifest={}, notes=[],
            cfg_text=self.STAGED_TEXT, current_schema=10 ** 6, free_bytes=10 ** 12)
        return inst, live, cfg

    def cut_after_unlinking(self, path, fn):
        real = os.unlink

        def unlink(p, *a, **k):
            real(p, *a, **k)
            if str(p) == str(path):
                raise _Kill()
        with mock.patch("os.unlink", unlink):
            with self.assertRaises(_Kill):
                fn()


class TestASettingsWriteCutShortBeforeItsCopyWent(_SettingsOnlyAcrossFilesystems):
    """Cut short after config.yaml was written but before the staged copy
    was removed, the review build counted that swap as "nothing moved": an
    hour offline and the restore was dropped as expired ("nothing was
    changed") with the restored settings live; and a rollback from there
    kept them ("never moved in")."""

    def test_every_kill_point_then_an_hour_offline(self):
        points = 0
        for k in range(1, 60):
            inst, live, cfg = self.fixture()
            with _Faults(k):
                try:
                    restore_io.apply_pending_restore(inst, log=_quiet)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            m = restore_io.read_marker(inst)
            if m and m["phase"] == "staged":
                m["staged_at"] = time.time() - 7200
                restore_io.write_marker(inst, m)
            state, _m = restore_io.apply_pending_restore(inst, log=_quiet)
            if state == "expired":
                self.assertIn("# live", cfg.read_text(), f"k={k}: dropped, settings switched")
            else:
                self.assertEqual(state, "applied", f"k={k}")
                self.assertIn("# restored", cfg.read_text(), f"k={k}")
            self.assertEqual(count_rows(live), (100, "A00000"), f"k={k}")
        self.assertGreater(points, 2)

    def test_the_window_itself(self):
        inst, live, cfg = self.fixture()
        staged = restore_io.read_marker(inst)["config"]["staged"]
        real = os.unlink
        calls = []

        def unlink(p, *a, **k):
            if str(p) == staged:
                calls.append(p)
                raise _Kill()                      # written, the staged copy not yet removed
            return real(p, *a, **k)
        with mock.patch("os.unlink", unlink):
            with self.assertRaises(_Kill):
                restore_io.apply_pending_restore(inst, log=_quiet)
        self.assertTrue(calls)
        self.assertIn("# restored", cfg.read_text())
        self.assertTrue(os.path.exists(staged))
        m = restore_io.read_marker(inst)
        self.assertFalse(restore_io.is_expired(m, now=time.time() + 7200))
        with self.assertRaises(ValueError):
            restore_io.cancel_pending_restore(inst, log=_quiet)
        # a rollback from here puts the settings from before back
        res = restore_io.rollback_pending_restore(inst, m, "simulated", log=_quiet)
        self.assertTrue(res["rolled_back"], res)
        self.assertIn("# live", cfg.read_text())
        self.assertFalse(os.path.exists(staged))
        self.assertIsNone(restore_io.read_marker(inst))


class TestASettingsRollbackCutShortAfterItsWrite(_SettingsOnlyAcrossFilesystems):
    """Guard (coverage): a rollback that couldn't record its phase (a full
    disk) and was cut short right after it WROTE config.yaml back across
    filesystems (a new inode: only the content tells): the next start
    finishes the rollback — not a "retry" that would call the settings from
    before the restore the restored ones."""

    def test_recognised_by_content(self):
        inst, live, cfg = self.fixture()
        state, m = restore_io.apply_pending_restore(inst, log=_quiet)
        self.assertEqual(state, "applied")
        self.assertIn("# restored", cfg.read_text())
        snap = m["config"]["snapshot"]
        with mock.patch.object(restore_io, "request_rollback",
                               side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.cut_after_unlinking(snap, lambda: restore_io.rollback_pending_restore(
                inst, m, "migration failed (simulated)", log=_quiet))
        self.assertIn("# live", cfg.read_text())
        self.assertNotEqual(restore_io._ino(os.path.realpath(cfg)), m["config"]["snapshot_ino"])
        self.assertEqual(restore_io.read_marker(inst)["phase"], "swapped")
        state, _m = restore_io.apply_pending_restore(inst, log=_quiet)
        self.assertEqual(state, "rolled_back")
        self.assertTrue(restore_io.read_result(inst)["rolled_back"])
        self.assertIsNone(restore_io.read_marker(inst))
        self.assertIn("# live", cfg.read_text())


class TestCutShortRollbackFoundAfterTheHour(_Restore):
    """Guard (coverage): a rollback cut short before its phase was recorded
    and found after the hour is finished and reported as rolled back — the
    expiry (checked after it) must not call it "expired"."""

    def test_reported_rolled_back_not_expired(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        m = self.marker()
        restore_io._swap_in(m)                      # the swap ran; its `swapped` write failed
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(restore_io, "request_rollback", side_effect=full), \
                mock.patch.object(restore_io, "write_result", return_value=False), \
                mock.patch.object(restore_io, "write_marker", side_effect=full):
            restore_io.rollback_pending_restore(self.inst, m, "disk full", log=_quiet)
        self.assertEqual(self.marker()["phase"], "staged")
        self.assertEqual(count_rows(self.live), (30, "A00000"))
        state, _m = restore_io.apply_pending_restore(self.inst, log=_quiet,
                                                     now=time.time() + 7200)
        self.assertEqual(state, "rolled_back")
        self.assertTrue(self.result().get("rolled_back"), self.result())
        self.assertNotIn("expired", self.result())


class TestPutBackOfAnUnusableSettingsCopy(_Restore):
    """Guard (coverage): a settings copy too large to read, or not UTF-8, is
    a 422 offering the database alone — like a backup's settings."""

    def pair(self, cfg_bytes):
        ts = "20260101-000000"
        (self.inst / f"config.yaml.bak.{ts}.pre-restore").write_bytes(cfg_bytes)
        shutil.copyfile(self.live, self.inst / f"aircraft_history.db.bak.{ts}.pre-restore")
        status, body = _call(self.ep("/api/backup/pre-restore/{ts}/restore")(
            ts=ts, background_tasks=None))
        self.release_lock()
        return status, body

    def test_oversized(self):
        status, body = self.pair(b"# x\n" * 70000)
        self.assertEqual(status, 422, body)
        self.assertTrue(body.get("database_only"), body)
        self.assert_untouched()

    def test_undecodable(self):
        status, body = self.pair("receiver:\n  ip: Grüße\n".encode("latin-1"))
        self.assertEqual(status, 422, body)
        self.assertTrue(body.get("database_only"), body)
        self.assert_untouched()


class TestAVanishedStagedSettingsFile(_Waiting):
    """Guard (coverage): the staged settings deleted by hand (a cleanup
    tool) while the restore waits were never moved in — config.yaml isn't
    them: the restore can still be cancelled, or expire."""

    def vanish(self):
        os.unlink(self.marker()["config"]["staged"])

    def test_cancel(self):
        self.vanish()
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["cancelled"])
        self.assertEqual(self.cfg_path.read_bytes(), self.cfg_before)

    def test_expiry(self):
        self.vanish()
        state, _m = restore_io.apply_pending_restore(self.inst, log=_quiet,
                                                     now=time.time() + 7200)
        self.assertEqual(state, "expired")
        self.assertEqual(count_rows(self.live), (30, "A00000"))


class TestARestoreOfTheSameSettings(_Restore):
    """Guard: a backup whose settings are the very text config.yaml holds
    (staged copy and pre-restore copy alike): config.yaml "holds the staged
    settings" from the start, which mustn't be taken for a switch that
    began — the restore can still be cancelled, or expire."""

    def stage_same(self):
        make_backup_zip(self.zip, self.cfg_before.decode(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        m = self.marker()
        self.assertEqual(m["config"]["staged_sha256"], m["config"]["snapshot_sha256"])

    def test_cancel(self):
        self.stage_same()
        status, body = _call(self.ep("/api/backup/staged-restore/cancel")())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["cancelled"])

    def test_expiry(self):
        self.stage_same()
        state, _m = restore_io.apply_pending_restore(self.inst, log=_quiet,
                                                     now=time.time() + 7200)
        self.assertEqual(state, "expired")
        self.assertEqual(count_rows(self.live), (30, "A00000"))


class TestNtfyAppliedIsShownWhileItWaits(TestNtfyAppliedIsSaidOnCancel):
    """Guard (coverage): the waiting restore's status says the notification
    server's settings were already applied (the proposed Cancel confirm
    reads it), and the cancel and expiry log lines say they stay."""

    test_cancel_says_the_ntfy_settings_stay = None
    test_without_ntfy_cancel_still_says_nothing_changed = None

    def stage_with_ntfy(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes,
                        extra={"ntfy/server.yml": self.NTFY})
        with mock.patch("ntfy_installer.install_status",
                        return_value={"state": "aerodrome_managed"}), \
                mock.patch("ntfy_installer.update_config", return_value={"ok": True}):
            status, body = self.restore()
        self.assertEqual(status, 200, body)

    def test_status(self):
        self.stage_with_ntfy()
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertIs(st["restore"]["pending"]["ntfy_applied"], True)

    def test_status_without_ntfy(self):
        make_backup_zip(self.zip, self.backup_text(), self.db_bytes)
        self.assertEqual(self.restore()[0], 200)
        st = self.ep("/api/maintenance/status", "GET")()
        self.assertIs(st["restore"]["pending"]["ntfy_applied"], False)

    def test_log_lines(self):
        for how in ("cancel", "expiry"):
            with self.subTest(how):
                self.stage_with_ntfy()
                lines = []
                if how == "cancel":
                    restore_io.cancel_pending_restore(self.inst, log=lines.append)
                else:
                    restore_io.discard_if_expired(self.inst, log=lines.append,
                                                  now=time.time() + 7200)
                self.assertTrue(any(restore_io.NTFY_KEPT_NOTE in ln for ln in lines), lines)
                self.assertFalse(any("nothing was changed" in ln for ln in lines), lines)


class TestSmallGuardsRound2(_MainRestore):
    """Guards (coverage): the startup sweep removes a marker a cancel or an
    expiry had claimed; a start that waits for the web address stops the
    collector it started before exiting."""

    def test_a_claimed_marker_is_swept(self):
        claimed = self.inst / ".backups" / f"{MARKER}{restore_io.DROPPING_INFIX}4242-1"
        claimed.write_text("{}")
        restore_io.sweep_orphans(self.inst, self.live, self.cfg_path, log=_quiet)
        self.assertFalse(claimed.exists())

    def test_the_collector_is_stopped_before_the_bind_wait_exit(self):
        events = []

        def collector(cfg, ev):
            events.append(ev)
        self.stage()
        out = self.start(bind=lambda h, p: (False, "in use"),
                         extra=[mock.patch.object(self.main, "run_collector", collector)])
        self.assertEqual(out, "exit 2")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].is_set(), "the collector was left running")


if __name__ == "__main__":
    unittest.main()
