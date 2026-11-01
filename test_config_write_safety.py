"""config.yaml writes are all-or-nothing; an unreadable one no longer stops
Aerodrome; private files stay private (v3.4.148).

Every config.yaml writer used to truncate the file and then write it: a
full disk, a crash or a second writer part-way through left it empty or cut
short, and the next start refused to run (a restart loop with the web UI
gone). These tests inject the failure — a real short write (RLIMIT_FSIZE in
a child process: write() fails part-way, the kernel-level shape of a full
disk) or ENOSPC at a chosen step — and check that the file and the running
config are exactly what they were, that the user is told, that a start with
an unreadable config.yaml runs on the last copy that worked, and that a
Settings save refuses settings that can't work on this machine.

Every install is a throwaway directory (the server module's __file__ and
main's paths point at it); nothing is written to the repo. No root, no
network. Addresses are RFC 5737 examples; hexes and labels are invented.
"""
import asyncio
import contextlib
import errno
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

import atomic_io
import config_io
import updater
from test_update_apply import NEW, OLD, PRESERVE, _fake_release, _tmpdir, repo_guard

setUpModule, tearDownModule = repo_guard()

REPO = Path(__file__).resolve().parent
EXAMPLE = (REPO / "config.yaml.example").read_text()
ENOSPC = OSError(errno.ENOSPC, "No space left on device")


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


def _status(r):
    return getattr(r, "status_code", 200)


def _body(r):
    return json.loads(r.body) if hasattr(r, "body") else r


def _leftover_tmps(d):
    return sorted(p.name for p in Path(d).rglob("*.tmp"))


def _umask_022(test):
    old = os.umask(0o022)
    test.addCleanup(os.umask, old)


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def _example_cfg(db, **over):
    cfg = yaml.safe_load(EXAMPLE)
    cfg["data"]["db_file"] = str(db)
    cfg["receiver"]["ip"] = "192.0.2.10"
    cfg["web"]["host"] = "127.0.0.1"
    cfg["watchlist"] = [{"icao": "A0B1C2", "label": "invented one"}]
    cfg.setdefault("updates", {}).setdefault("github", {})["enabled"] = False
    for k, v in over.items():
        cfg[k] = v
    return cfg


def _commented(db):
    """The shipped example (comments and all) pointing at `db`."""
    return (EXAMPLE.replace('db_file: "aircraft_history.db"', f'db_file: "{db}"')
            .replace('host: "0.0.0.0"', 'host: "127.0.0.1"'))


def _make_db(path):
    import collector
    from schema_migrations import apply_schema_migrations
    collector.init_db(str(path))
    c = sqlite3.connect(str(path))
    apply_schema_migrations(c, "test")
    c.close()


# ===========================================================================
# config_io itself (pure guards of the new module)
# ===========================================================================

class TestAtomicWrite(unittest.TestCase):
    """Guards: config_io.write_text is all or nothing at every step."""

    def setUp(self):
        _umask_022(self)
        self.dir = _tmpdir(self)
        self.path = self.dir / "config.yaml"
        self.path.write_text(EXAMPLE)

    def test_failure_at_each_step_leaves_the_file_byte_identical(self):
        before = self.path.read_bytes()
        for target in ("fsync", "replace", "fchmod"):
            with self.subTest(step=target), \
                    mock.patch.object(atomic_io.os, target, side_effect=ENOSPC):
                with self.assertRaises(config_io.ConfigWriteError) as cm:
                    config_io.write_text(self.path, EXAMPLE.replace("Aerodrome", "Changed"))
                self.assertTrue(cm.exception.disk_full)
                self.assertIn("the disk is full", str(cm.exception))
                self.assertIn("Nothing was changed", str(cm.exception))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(_leftover_tmps(self.dir), [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "RLIMIT_FSIZE semantics")
    def test_real_short_write_leaves_the_file_byte_identical(self):
        # A child whose file-size limit makes write() fail part-way through.
        code = textwrap.dedent("""\
            import resource, signal, sys
            sys.path.insert(0, sys.argv[1])
            import config_io
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (2048, resource.RLIM_INFINITY))
            try:
                config_io.write_text(sys.argv[2], open(sys.argv[3]).read())
            except config_io.ConfigWriteError as e:
                print('REFUSED', e.disk_full)
            """)
        before = self.path.read_bytes()
        out = subprocess.run([sys.executable, "-c", code, str(REPO), str(self.path),
                              str(REPO / "config.yaml.example")],
                             capture_output=True, text=True, timeout=60)
        self.assertIn("REFUSED True", out.stdout, out.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(_leftover_tmps(self.dir), [])

    def test_symlinked_config_is_written_through(self):
        real = self.dir / "elsewhere.yaml"
        self.path.rename(real)
        self.path.symlink_to(real)
        config_io.write_text(self.path, EXAMPLE.replace("Aerodrome", "Changed"))
        self.assertTrue(self.path.is_symlink())
        self.assertIn("Changed", real.read_text())

    def test_written_0600_whatever_the_old_mode_and_owner_kept_as_root(self):
        os.chmod(self.path, 0o644)
        config_io.write_text(self.path, EXAMPLE)
        self.assertEqual(_mode(self.path), 0o600)
        # As root (e.g. `sudo python3 main.py start`), the service account's
        # ownership is kept instead of the file becoming root's.
        calls = []
        with mock.patch.object(atomic_io.os, "geteuid", return_value=0), \
                mock.patch.object(atomic_io.os, "fchown",
                                  side_effect=lambda fd, u, g: calls.append((u, g))):
            config_io.write_text(self.path, EXAMPLE)
        st = self.path.stat()
        self.assertEqual(calls, [(st.st_uid, st.st_gid)])


_AS_ROOT = os.geteuid() == 0


class TestWhoseFileAndWhichFolder(unittest.TestCase):
    """A save renames a new file over config.yaml. That needs a folder this
    account can write, and would hand another account's file to this one,
    or replace one made read-only on purpose. Before, all three went wrong:
    a config.yaml linked into a folder Aerodrome can't write (a layout the
    updater supports) could no longer be saved at all, and another
    account's or a read-only config.yaml was silently replaced by a 0600
    file of Aerodrome's."""

    def setUp(self):
        _umask_022(self)
        self.dir = _tmpdir(self)
        self.path = self.dir / "config.yaml"
        self.new = EXAMPLE.replace("Aerodrome", "Changed")

    def link_into_readonly_folder(self, mode=0o600):
        etc = self.dir / "etc-aerodrome"
        etc.mkdir()
        real = etc / "config.yaml"
        real.write_text(EXAMPLE)
        os.chmod(real, mode)
        self.path.symlink_to(real)
        os.chmod(etc, 0o555)
        self.addCleanup(os.chmod, etc, 0o755)
        return real

    @unittest.skipIf(_AS_ROOT, "root can write any folder")
    def test_a_link_into_a_folder_this_account_cant_write_is_saved_in_place(self):
        real = self.link_into_readonly_folder(0o644)
        ino = real.stat().st_ino
        config_io.write_text(self.path, self.new)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(real.read_text(), self.new)
        self.assertEqual(real.stat().st_ino, ino)
        self.assertEqual(_mode(real), 0o600)
        self.assertEqual(_leftover_tmps(self.dir), [])
        # A shorter text leaves no tail of the old one.
        config_io.write_text(self.path, EXAMPLE[:400] + "\nreceiver: {}\n", check=False)
        self.assertEqual(real.read_text(), EXAMPLE[:400] + "\nreceiver: {}\n")

    @unittest.skipIf(_AS_ROOT, "root can write any folder")
    def test_in_place_refuses_when_the_disk_is_too_full_and_changes_nothing(self):
        real = self.link_into_readonly_folder()
        before = real.read_bytes()
        full = mock.Mock(f_bavail=1, f_frsize=4096)
        with mock.patch.object(config_io.os, "statvfs", return_value=full), \
                self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, self.new)
        self.assertTrue(cm.exception.disk_full)
        self.assertEqual(real.read_bytes(), before)

    @unittest.skipIf(_AS_ROOT, "root can write any folder")
    def test_in_place_puts_the_old_bytes_back_when_the_write_fails(self):
        real = self.link_into_readonly_folder()
        before = real.read_bytes()
        real_write, n = os.write, [0]

        def flaky(fd, data):
            n[0] += 1
            if n[0] == 1:                  # the new text: cut short by a full disk
                real_write(fd, bytes(data)[:100])
                raise ENOSPC
            return real_write(fd, data)
        new = "# differs from the first byte\n" + EXAMPLE
        with mock.patch.object(config_io.os, "write", side_effect=flaky), \
                self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, new)
        self.assertTrue(cm.exception.disk_full)
        self.assertIn("Nothing was changed", str(cm.exception))
        self.assertEqual(real.read_bytes(), before)

    @unittest.skipIf(_AS_ROOT, "root can write any folder")
    def test_in_place_refuses_a_file_with_other_hard_links(self):
        # Guard: writing in place would change every other name of the file.
        real = self.link_into_readonly_folder()
        os.chmod(real.parent, 0o755)
        os.link(real, real.parent / "other-name.yaml")
        os.chmod(real.parent, 0o555)
        with self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, self.new)
        self.assertIn("hard links", str(cm.exception))
        self.assertIn("Nothing was changed", str(cm.exception))
        self.assertEqual(real.read_text(), EXAMPLE)

    @unittest.skipIf(_AS_ROOT, "root can write any folder")
    def test_in_place_write_that_doesnt_read_back_is_refused(self):
        # Guard: the text is read back after an in-place write (a write the
        # disk reports done but stores differently is not a success).
        self.link_into_readonly_folder()
        real_write = os.write

        def garbling(fd, data):
            b = bytes(data)
            real_write(fd, b.replace(b"Changed", b"Chang3d"))
            return len(b)
        with mock.patch.object(config_io.os, "write", side_effect=garbling), \
                self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, self.new)
        self.assertIn("didn't read back as written", str(cm.exception))

    @unittest.skipIf(_AS_ROOT, "root can write any folder")
    def test_recovery_in_that_layout_keeps_a_real_copy_not_a_link(self):
        # A hard link would share the file written in place next: the kept
        # copy of the user's file would turn into last-good too.
        real = self.link_into_readonly_folder()
        good = yaml.safe_dump(_example_cfg("/nonexistent/h.db"), sort_keys=False)
        os.chmod(real.parent, 0o755)
        real.write_text("receiver: [\n")
        os.chmod(real.parent, 0o555)
        (self.dir / ".backups").mkdir()
        (self.dir / ".backups" / "config.yaml.last-good").write_text(good)
        cfg, info = config_io.recover(self.path, "not valid YAML", log=lambda m: None)
        self.assertTrue(info["rewritten"], info)
        self.assertEqual(real.read_text(), good)
        broken = self.dir / info["broken_copy"]
        self.assertEqual(broken.read_text(), "receiver: [\n")
        self.assertEqual(_mode(broken), 0o600)

    @unittest.skipIf(_AS_ROOT, "root can write any folder")
    def test_a_plain_config_in_a_folder_it_cant_write_is_refused_naming_the_folder(self):
        # Guard: only the linked layout is written in place (an install folder
        # Aerodrome can't write can't start anyway: .tracker.pid lives there).
        self.path.write_text(EXAMPLE)
        os.chmod(self.dir, 0o555)
        self.addCleanup(os.chmod, self.dir, 0o755)
        with self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, self.new)
        self.assertIn(str(self.dir), str(cm.exception))
        self.assertIn("Nothing was changed", str(cm.exception))
        self.assertEqual(self.path.read_text(), EXAMPLE)

    @unittest.skipIf(_AS_ROOT, "root may write a read-only file")
    def test_a_read_only_config_is_refused_not_replaced(self):
        self.path.write_text(EXAMPLE)
        os.chmod(self.path, 0o444)
        ino = self.path.stat().st_ino
        with self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, self.new)
        self.assertIn("read-only", str(cm.exception))
        self.assertEqual((self.path.read_text(), self.path.stat().st_ino, _mode(self.path)),
                         (EXAMPLE, ino, 0o444))

    @unittest.skipIf(_AS_ROOT, "the owner check is for a non-root service")
    def test_another_accounts_file_it_can_write_keeps_its_owner_and_mode(self):
        # (Another owner is simulated: this process's euid is reported as
        # someone else's.) A group-shared manual install: written in place,
        # as 3.4.147 did, not taken over by a rename.
        self.path.write_text(EXAMPLE)
        os.chmod(self.path, 0o660)
        ino = self.path.stat().st_ino
        with mock.patch.object(config_io.os, "geteuid", return_value=os.getuid() + 4242):
            config_io.write_text(self.path, self.new)
        self.assertEqual(self.path.read_text(), self.new)
        self.assertEqual((self.path.stat().st_ino, _mode(self.path)), (ino, 0o660))

    @unittest.skipIf(_AS_ROOT, "the owner check is for a non-root service")
    def test_another_accounts_file_it_can_write_but_not_read_is_refused_cleanly(self):
        # Guard: a write-only file can't be written in place (the old bytes
        # couldn't be put back): a ConfigWriteError, not a raw OSError.
        self.path.write_text(EXAMPLE)
        os.chmod(self.path, 0o220)
        self.addCleanup(os.chmod, self.path, 0o600)
        with mock.patch.object(config_io.os, "geteuid", return_value=os.getuid() + 4242), \
                self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, self.new)
        self.assertIn("Nothing was changed", str(cm.exception))
        os.chmod(self.path, 0o600)
        self.assertEqual(self.path.read_text(), EXAMPLE)

    @unittest.skipIf(_AS_ROOT, "the owner check is for a non-root service")
    def test_another_accounts_file_it_cant_write_is_refused(self):
        self.path.write_text(EXAMPLE)
        os.chmod(self.path, 0o444)
        with mock.patch.object(config_io.os, "geteuid", return_value=os.getuid() + 4242), \
                self.assertRaises(config_io.ConfigWriteError) as cm:
            config_io.write_text(self.path, self.new)
        self.assertIn("belongs to", str(cm.exception))
        self.assertEqual(self.path.read_text(), EXAMPLE)


class TestRender(unittest.TestCase):
    """Guards: what a save writes."""

    def setUp(self):
        self.dir = _tmpdir(self)
        self.path = self.dir / "config.yaml"
        self.cfg = yaml.safe_load(EXAMPLE)

    def test_comments_survive_a_save_and_last_good_is_not_written(self):
        # last-good is the last config that STARTED: a save hasn't started
        # yet (a web port another program holds only fails at the restart,
        # and Decision 2b then needs the address that last worked).
        self.path.write_text(EXAMPLE)
        self.cfg["display"]["date_format"] = "DMY"
        config_io.save_config(self.path, self.cfg)
        text = self.path.read_text()
        self.assertIn("# Version:", text)
        self.assertEqual(yaml.safe_load(text), self.cfg)
        self.assertFalse((self.dir / ".backups" / "config.yaml.last-good").exists())

    def test_hhmm_values_read_back_as_strings_when_the_file_lacks_the_section(self):
        # ruamel writes YAML 1.2; PyYAML (main.py) reads 1.1, where an
        # unquoted 22:00 is the integer 1320.
        on_disk = dict(self.cfg)
        on_disk.pop("notifications")
        self.path.write_text(yaml.safe_dump(on_disk, sort_keys=False))
        config_io.save_config(self.path, self.cfg)
        n = yaml.safe_load(self.path.read_text())["notifications"]
        self.assertEqual(n["quiet_hours"]["start"], "22:00")
        self.assertEqual(n["daily_summary"]["time"], "20:00")

    def test_refuses_a_config_the_service_could_not_start_from(self):
        self.path.write_text(EXAMPLE)
        bad = dict(self.cfg)
        bad.pop("web")
        with self.assertRaises(ValueError):
            config_io.save_config(self.path, bad)
        self.assertEqual(self.path.read_text(), EXAMPLE)

    def test_a_section_that_is_empty_is_refused(self):
        # Guard (a mutation that accepted it survived the suite): `web:` with
        # nothing under it reads as None — startup would crash on
        # config["web"]["host"], so it isn't loadable.
        cfg = yaml.safe_load(EXAMPLE)
        cfg["web"] = None
        with self.assertRaises(ValueError) as cm:
            config_io.check_loadable(yaml.safe_dump(cfg))
        self.assertIn("web", str(cm.exception))

    def test_check_loadable_reasons_name_no_file_content(self):
        for text, want in (("", "empty"), ("receiver: [\n", "line"),
                           ("- a\n- b\n", "mapping"), ("receiver: {}\n", "missing section")):
            with self.subTest(text=text), self.assertRaises(ValueError) as cm:
                config_io.check_loadable(text)
            self.assertIn(want, str(cm.exception))


class TestLastGoodWhilePending(unittest.TestCase):
    """Guard: last-good (written once a config has started) is not refreshed
    while an update or a restore isn't confirmed yet (the step that confirms
    it writes it then)."""

    def setUp(self):
        _umask_022(self)
        self.dir = _tmpdir(self)
        self.path = self.dir / "config.yaml"
        self.path.write_text(EXAMPLE)
        self.cfg = yaml.safe_load(EXAMPLE)
        self.lg = self.dir / ".backups" / "config.yaml.last-good"

    def test_update_journal_pending(self):
        (self.dir / ".update-pending.json").write_text('{"state": "pending_first_boot"}')
        self.assertFalse(config_io.write_last_good(self.path))
        self.assertFalse(self.lg.exists())

    def test_restore_marker_pending(self):
        (self.dir / ".backups").mkdir()
        (self.dir / ".backups" / "pending-restore.json").write_text('{"phase": "staged"}')
        self.assertFalse(config_io.write_last_good(self.path))
        self.assertFalse(self.lg.exists())

    def test_written_once_nothing_is_pending(self):
        self.assertTrue(config_io.write_last_good(self.path))
        self.assertEqual(yaml.safe_load(self.lg.read_text()), self.cfg)
        self.assertEqual(_mode(self.lg), 0o600)
        self.assertEqual(_mode(self.dir / ".backups"), 0o700)


# ===========================================================================
# Every writer on a real short write (child processes, RLIMIT_FSIZE)
# ===========================================================================

_CHILD = r'''
import asyncio, json, os, resource, signal, sys
from pathlib import Path
from unittest import mock
repo, work, case, limit = sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
sys.path.insert(0, repo)
import logging
logging.disable(logging.CRITICAL)
import yaml, requests
import server, collector, main
inst = work / "install"
cfgp = inst / "config.yaml"
server.__file__ = str(inst / "server.py")

def ep(app, path, method):
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))

def cap():
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, resource.RLIM_INFINITY))

def uncap():
    resource.setrlimit(resource.RLIMIT_FSIZE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))

def told(out):
    if hasattr(out, "status_code"):
        return {"status": out.status_code, "body": json.loads(out.body)}
    return {"status": 200, "body": out}

def jreq(body):
    raw = json.dumps(body).encode()
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "query_string": b""}, receive)

res = {}
run = mock.MagicMock(return_value=mock.Mock(returncode=0, stderr="", stdout=""))
with mock.patch("subprocess.run", run):
    if case == "startup_migrate":
        main.CONFIG_PATH = cfgp
        main.CONFIG_EXAMPLE_PATH = Path(repo) / "config.yaml.example"
        cfg = yaml.safe_load(cfgp.read_text())
        cap()
        try:
            got = main.migrate_config(cfg)
        finally:
            uncap()
        res["told"] = {"returned_config": isinstance(got, dict) and "receiver" in got}
    elif case == "collector_port_recovery":
        cfg = yaml.safe_load(cfgp.read_text())
        collector._FEEDER_UNIT_PATH = str(work / "feeder.service")
        collector._demo_recovery_attempted = False
        with mock.patch.object(collector, "_port_in_use", return_value=False):
            cap()
            try:
                got = collector._attempt_demo_port_recovery(cfg)
            finally:
                uncap()
        res["told"] = {"returned": got}
    else:
        cfg = yaml.safe_load(cfgp.read_text())
        app = server.get_app(cfg, str(cfgp))
        before_cfg = json.loads(json.dumps(server.CONFIG))
        if case == "watchlist_add":
            e = ep(app, "/api/watchlist/add", "POST")
            arg = e.__annotations__["entry"](identifier="A0B1C3", id_type="icao",
                                             label="invented two")
            cap()
            try:
                out = e(arg)
            finally:
                uncap()
        elif case == "watchlist_remove":
            e = ep(app, "/api/watchlist/remove", "POST")
            arg = e.__annotations__["entry"](identifier="A0B1C2", id_type="icao")
            cap()
            try:
                out = e(arg)
            finally:
                uncap()
        elif case == "settings_save":
            e = ep(app, "/api/config", "PUT")
            new = json.loads(json.dumps(server.CONFIG))
            new["display"]["date_format"] = "DMY"
            arg = e.__annotations__["payload"](config=new)
            cap()
            try:
                out = e(arg)
            finally:
                uncap()
        elif case == "config_restore":
            e = ep(app, "/api/config/restore/{name}", "POST")
            cap()
            try:
                out = e("config.yaml.bak.20260101-000000")
            finally:
                uncap()
        elif case == "switch_to_real":
            e = ep(app, "/api/setup/switch-to-real", "POST")
            with mock.patch.object(requests, "get", return_value=mock.Mock(status_code=200)):
                cap()
                try:
                    out = asyncio.run(e(jreq({"receiver_ip": "192.0.2.20",
                                              "receiver_port": 30047})))
                finally:
                    uncap()
        res["told"] = told(out)
        res["memory_unchanged"] = json.loads(json.dumps(server.CONFIG)) == before_cfg
res["tee_or_restart_called"] = any(
    c.args and any(x in ("tee", "restart") for x in c.args[0]) for c in run.call_args_list)
print("RESULT " + json.dumps(res))
'''


class _ChildCase(unittest.TestCase):
    """A throwaway install under a temp dir; each case runs the real writer
    in a child process whose file-size limit makes a write fail part-way."""

    def setUp(self):
        self.work = _tmpdir(self)
        self.inst = self.work / "install"
        self.inst.mkdir()
        self.db = self.work / "history.db"
        _make_db(self.db)
        self.cfgp = self.inst / "config.yaml"

    def write_cfg(self, text):
        self.cfgp.write_text(text)
        self.before = self.cfgp.read_bytes()

    def run_child(self, case, limit):
        r = subprocess.run([sys.executable, "-c", _CHILD, str(REPO), str(self.work), case,
                            str(limit)], capture_output=True, text=True, timeout=120,
                           cwd=str(self.work))
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, r.stdout[-1500:] + r.stderr[-3000:])
        return json.loads(line[len("RESULT "):])

    def assert_untouched(self):
        self.assertEqual(self.cfgp.read_bytes(), self.before,
                         "config.yaml changed (cut short) after a failed write")
        self.assertEqual(_leftover_tmps(self.inst), [])

    def plain(self, **over):
        return yaml.safe_dump(_example_cfg(self.db, **over), sort_keys=False)


class TestWritersOnAFullDisk(_ChildCase):
    """Each writer, on a write that fails part-way: config.yaml is
    byte-identical, the running config unchanged, the user told (507)."""

    def test_watchlist_add(self):
        self.write_cfg(self.plain())
        res = self.run_child("watchlist_add", 1024)
        self.assertEqual(res["told"]["status"], 507, res)
        self.assertIn("the disk is full", res["told"]["body"]["error"])
        self.assertTrue(res["memory_unchanged"])
        self.assert_untouched()

    def test_watchlist_remove(self):
        self.write_cfg(self.plain())
        res = self.run_child("watchlist_remove", 1024)
        self.assertEqual(res["told"]["status"], 507, res)
        self.assertTrue(res["memory_unchanged"])
        self.assert_untouched()

    def test_settings_save_keeps_the_commented_file(self):
        self.write_cfg(_commented(self.db))
        res = self.run_child("settings_save", 4096)
        self.assertEqual(res["told"]["status"], 507, res)
        self.assertIn("the disk is full", res["told"]["body"]["errors"][0]["message"])
        self.assertTrue(res["memory_unchanged"])
        self.assert_untouched()

    def test_config_restore_never_restarts_into_a_cut_short_file(self):
        # A larger (commented) backup over a small live file: the .bak of
        # the live file fits under the limit, the bigger new text does not.
        live = self.plain()
        big = _commented(self.db)
        (self.inst / "config.yaml.bak.20260101-000000").write_text(big)
        self.write_cfg(live)
        res = self.run_child("config_restore", (len(live) + len(big)) // 2)
        self.assertEqual(res["told"]["status"], 507, res)
        self.assertIn("the disk is full", res["told"]["body"]["error"])
        self.assertFalse(res["tee_or_restart_called"], "restarted into a bad state")
        self.assert_untouched()

    def test_switch_to_real_touches_nothing(self):
        self.write_cfg(self.plain(demo={"enabled": True}))
        res = self.run_child("switch_to_real", 1024)
        self.assertFalse(res["told"]["body"]["ok"], res)
        self.assertIn("the disk is full", res["told"]["body"]["error"])
        self.assertTrue(self.db.exists(), "the demo database was deleted")
        self.assertFalse(res["tee_or_restart_called"])
        self.assert_untouched()

    def test_startup_migration(self):
        # A real commented config from an older release: the example with
        # its whole stats: block missing, so the migration writes it back.
        lines = _commented(self.db).splitlines(True)
        a = next(i for i, ln in enumerate(lines) if ln.startswith("stats:"))
        b = next(i for i, ln in enumerate(lines) if ln.startswith("updates:"))
        txt = "".join(lines[:a] + lines[b:])
        self.write_cfg(txt)
        res = self.run_child("startup_migrate", len(txt.encode()) + 64)
        self.assertTrue(res["told"]["returned_config"])
        self.assert_untouched()

    def test_collector_demo_port_recovery_never_touches_the_unit_file(self):
        cfg = _example_cfg(self.db, demo={"enabled": True})
        cfg["receiver"]["port"] = 30999        # not a candidate → recovery edits the file
        cfg["data"]["config_path"] = str(self.cfgp)
        self.write_cfg(yaml.safe_dump(cfg, sort_keys=False))
        (self.work / "feeder.service").write_text("ExecStart=python3 serve --port 30005\n")
        res = self.run_child("collector_port_recovery", 1024)
        self.assertIsNone(res["told"]["returned"])
        self.assertFalse(res["tee_or_restart_called"], "the unit file was written")
        self.assert_untouched()


class TestDemoPortRecoveryLockHold(unittest.TestCase):
    """CONFIG_LOCK is held for the config.yaml read-modify-write and the
    `sudo tee` between staging and committing it — not across the
    daemon-reload and the feeder restart (up to 25 s more), during which
    every Settings save (and a restore, on the event loop) waited."""

    def test_the_feeder_restart_runs_without_the_lock(self):
        import collector
        work = _tmpdir(self)
        cfgp = work / "config.yaml"
        cfg = _example_cfg(work / "h.db", demo={"enabled": True})
        cfg["receiver"]["port"] = 30999
        cfg["data"]["config_path"] = str(cfgp)
        cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False))
        (work / "feeder.service").write_text("ExecStart=python3 serve --port 30005\n")
        held = {}

        def free_from_another_thread():
            got = []
            t = threading.Thread(target=lambda: got.append(
                config_io.CONFIG_LOCK.acquire(timeout=0.05)
                and (config_io.CONFIG_LOCK.release() or True)))
            t.start()
            t.join()
            return got[0]

        def run(argv, *a, **kw):
            held[argv[1] if argv[1] != "systemctl" else argv[2]] = not free_from_another_thread()
            return mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch.object(collector, "_FEEDER_UNIT_PATH", str(work / "feeder.service")), \
                mock.patch.object(collector, "_demo_recovery_attempted", False), \
                mock.patch.object(collector, "_port_in_use", return_value=False), \
                mock.patch.object(collector.subprocess, "run", side_effect=run):
            port = collector._attempt_demo_port_recovery(cfg)
        self.assertIsNotNone(port)
        self.assertEqual(yaml.safe_load(cfgp.read_text())["receiver"]["port"], port)
        self.assertEqual(held, {"tee": True, "daemon-reload": False, "restart": False})


# ===========================================================================
# Endpoints in-process
# ===========================================================================

class _AppCase(unittest.TestCase):

    def setUp(self):
        import server
        self.server = server
        _umask_022(self)
        self.work = _tmpdir(self)
        self.inst = self.work / "install"
        (self.inst / "static").mkdir(parents=True)        # get_app mounts it
        self.db = self.work / "history.db"
        _make_db(self.db)
        self.cfgp = self.inst / "config.yaml"
        cfg = _example_cfg(self.db)
        self.customize(cfg)
        self.cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False))
        for p in (mock.patch.object(server, "__file__", str(self.inst / "server.py")),
                  mock.patch("subprocess.run")):
            self.run_mock = p.start()
            self.addCleanup(p.stop)
        self.app = server.get_app(cfg, str(self.cfgp))
        self.before_text = self.cfgp.read_text()

    def customize(self, cfg):
        pass

    def ep(self, path, method="POST"):
        return _endpoint(self.app, path, method)


class TestConcurrentWriters(_AppCase):

    def test_disk_matches_memory_never_torn(self):
        # Guard (the base usually ends consistent here too; the deterministic
        # lock test below is the one that fails without the lock). Only the
        # file's consistency: a Settings save still writes back the whole
        # config its page loaded, watchlist included.
        add = self.ep("/api/watchlist/add")
        put = self.ep("/api/config", "PUT")
        Entry, Payload = add.__annotations__["entry"], put.__annotations__["payload"]
        prev = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)                      # adversarial scheduling
        self.addCleanup(sys.setswitchinterval, prev)
        hexes = [f"B{i:05X}" for i in range(24)]

        def settings(i):
            new = json.loads(json.dumps(self.server.CONFIG))
            new["display"]["date_format"] = "DMY" if i % 2 else "MDY"
            put(Payload(config=new))
        jobs = [lambda h=h: add(Entry(identifier=h, id_type="icao", label="invented"))
                for h in hexes] + [lambda i=i: settings(i) for i in range(8)]
        ts = [threading.Thread(target=j) for j in jobs]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        disk = yaml.safe_load(self.cfgp.read_text())
        self.assertEqual(disk, self.server.CONFIG)

    def test_read_modify_write_is_serialized(self):
        # Deterministic (the lock guard): slow every write down and count
        # overlapping writers. Removing CONFIG_LOCK makes this fail.
        add = self.ep("/api/watchlist/add")
        Entry = add.__annotations__["entry"]
        real_stage, live, peak = config_io.stage_text, [0], [0]
        guard = threading.Lock()

        def slow_stage(path, text, check=True):
            with guard:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                threading.Event().wait(0.05)
                return real_stage(path, text, check=check)
            finally:
                with guard:
                    live[0] -= 1
        hexes = ["C00001", "C00002", "C00003"]
        with mock.patch.object(config_io, "stage_text", side_effect=slow_stage):
            ts = [threading.Thread(target=add, args=(Entry(identifier=h, id_type="icao"),))
                  for h in hexes]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(peak[0], 1)
        on_disk = {e.get("icao") for e in yaml.safe_load(self.cfgp.read_text())["watchlist"]}
        self.assertTrue(set(hexes) <= on_disk, on_disk)


class TestWatchlistKeepsComments(_AppCase):

    def customize(self, cfg):
        pass

    def test_add_and_remove_keep_config_comments(self):
        # Before: watchlist add/remove re-dumped the whole file and dropped
        # every comment in config.yaml.
        self.cfgp.write_text(_commented(self.db))
        Entry = self.ep("/api/watchlist/add").__annotations__["entry"]
        self.ep("/api/watchlist/add")(Entry(identifier="A0B1C9", id_type="icao", label="x"))
        text = self.cfgp.read_text()
        self.assertIn("# Version:", text)
        self.assertIn("A0B1C9", text)
        self.ep("/api/watchlist/remove")(Entry(identifier="A0B1C9", id_type="icao"))
        self.assertIn("# Version:", self.cfgp.read_text())


class TestMachineKeyChecks(_AppCase):
    """Settings refuses settings that can't work on this machine (instead of
    saving them and failing at the next restart, the web UI gone)."""

    def put(self, **web):
        new = json.loads(json.dumps(self.server.CONFIG))
        new["web"].update(web)
        ep = self.ep("/api/config", "PUT")
        return ep(ep.__annotations__["payload"](config=new))

    def test_an_address_not_on_this_machine_is_refused(self):
        r = self.put(host="192.0.2.10")
        self.assertEqual(_status(r), 400)
        b = _body(r)
        self.assertTrue(b["message"])                # every refusal: error == message
        self.assertEqual(b["error"], b["message"])
        errs = b["errors"]
        self.assertEqual(errs[0]["path"], "web.host")
        self.assertIn("192.0.2.10 isn't an address on this machine", errs[0]["message"])
        self.assertIn("0.0.0.0", errs[0]["message"])
        self.assertEqual(self.cfgp.read_text(), self.before_text)
        self.assertEqual(self.server.CONFIG["web"]["host"], "127.0.0.1")

    def test_all_addresses_passes(self):
        # Guard: the usual listen-on-everything address is never refused.
        r = self.put(host="0.0.0.0")
        self.assertEqual(_status(r), 200, _body(r))
        self.assertEqual(yaml.safe_load(self.cfgp.read_text())["web"]["host"], "0.0.0.0")

    def test_an_unchanged_host_is_not_rechecked(self):
        # Guard: a running config that already has an odd address (a hand
        # edit) doesn't block saving something else.
        self.server.CONFIG["web"]["host"] = "192.0.2.10"
        new = json.loads(json.dumps(self.server.CONFIG))
        new["display"]["date_format"] = "DMY"
        ep = self.ep("/api/config", "PUT")
        r = ep(ep.__annotations__["payload"](config=new))
        self.assertEqual(_status(r), 200, _body(r))

    def listener(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_a_port_another_program_listens_on_is_refused(self):
        # Before: saved, then the restart failed "Address already in use" —
        # and the save had made it last-good too, so there was no way back.
        busy = self.listener()
        r = self.put(port=busy)
        self.assertEqual(_status(r), 400, _body(r))
        b = _body(r)
        self.assertEqual(b["errors"][0]["path"], "web.port")
        self.assertIn(f"Port {busy} is already used by another program", b["errors"][0]["message"])
        self.assertEqual(b["error"], b["message"])
        self.assertEqual(self.cfgp.read_text(), self.before_text)

    @unittest.skipIf(os.geteuid() == 0, "root may use any port")
    def test_a_port_this_account_may_not_use_is_refused(self):
        try:
            start = int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text())
        except (OSError, ValueError):
            start = 1024
        if start <= 80:
            self.skipTest("this machine lets every account use port 80")
        r = self.put(port=80)
        self.assertEqual(_status(r), 400, _body(r))
        self.assertIn("administrator rights", _body(r)["errors"][0]["message"])

    def test_a_free_port_and_the_port_it_serves_on_pass(self):
        # Guards: a free port saves; so does going back to the port this
        # process listens on (its own socket holds it) after a port change
        # that wasn't restarted yet.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(_status(self.put(port=port)), 200)
        self.assertEqual(self.server.CONFIG["web"]["port"], port)
        serving = self.listener()                   # stands in for our own socket
        with mock.patch.object(self.server, "_SERVING_WEB", ("127.0.0.1", serving)):
            r = self.put(port=serving)
        self.assertEqual(_status(r), 200, _body(r))
        self.assertEqual(yaml.safe_load(self.cfgp.read_text())["web"]["port"], serving)

    def test_a_config_import_with_a_busy_port_is_refused(self):
        busy = self.listener()
        cfg = yaml.safe_load(self.before_text)
        cfg["web"]["port"] = busy
        from starlette.datastructures import UploadFile
        up = UploadFile(file=io.BytesIO(yaml.safe_dump(cfg).encode()), filename="config.yaml")
        r = self.ep("/api/config/import")(file=up)
        self.assertEqual(_status(r), 400)
        self.assertEqual(_body(r)["errors"][0]["path"], "web.port")
        self.assertEqual(self.cfgp.read_text(), self.before_text)

    def test_a_database_folder_that_doesnt_exist_is_refused(self):
        new = json.loads(json.dumps(self.server.CONFIG))
        new["data"]["db_file"] = str(self.work / "no-such-folder" / "h.db")
        ep = self.ep("/api/config", "PUT")
        r = ep(ep.__annotations__["payload"](config=new))
        self.assertEqual(_status(r), 400)
        self.assertEqual(_body(r)["errors"][0]["path"], "data.db_file")
        self.assertEqual(self.cfgp.read_text(), self.before_text)

    def test_config_import_with_a_foreign_address_is_refused_and_never_restarts(self):
        cfg = yaml.safe_load(self.before_text)
        cfg["web"]["host"] = "192.0.2.10"
        from starlette.datastructures import UploadFile
        up = UploadFile(file=io.BytesIO(yaml.safe_dump(cfg).encode()), filename="config.yaml")
        r = self.ep("/api/config/import")(file=up)
        self.assertEqual(_status(r), 400)
        self.assertEqual(_body(r)["errors"][0]["path"], "web.host")
        self.assertEqual(self.cfgp.read_text(), self.before_text)
        self.run_mock.assert_not_called()


class TestSavesLeaveLastGoodAlone(_AppCase):
    """last-good is the last config that STARTED. A Settings save, a
    watchlist edit or a config import/restore hasn't started yet, so none of
    them may replace it (before: each did, so a saved port that turned out
    busy at the restart left nothing to fall back to)."""

    def setUp(self):
        super().setUp()
        self.lg = self.inst / ".backups" / "config.yaml.last-good"
        self.lg.parent.mkdir(exist_ok=True)
        self.lg.write_text(self.before_text)

    def test_settings_watchlist_and_import(self):
        new = json.loads(json.dumps(self.server.CONFIG))
        new["display"]["date_format"] = "DMY"
        put = self.ep("/api/config", "PUT")
        self.assertEqual(_status(put(put.__annotations__["payload"](config=new))), 200)
        add = self.ep("/api/watchlist/add")
        add(add.__annotations__["entry"](identifier="A0B1C7", id_type="icao", label="x"))
        from starlette.datastructures import UploadFile
        text = _commented(self.db)
        r = self.ep("/api/config/import")(file=UploadFile(file=io.BytesIO(text.encode()),
                                                          filename="config.yaml"))
        self.assertTrue(_body(r)["ok"], _body(r))
        self.assertEqual(self.cfgp.read_text(), text)
        self.assertEqual(self.lg.read_text(), self.before_text)

    def test_switch_to_real(self):
        # Guard: switch-to-real's new config only starts after the restart
        # it asks for (the start writes last-good then).
        import requests
        self.server.CONFIG.setdefault("demo", {})["enabled"] = True
        ep = self.ep("/api/setup/switch-to-real")
        body = {"receiver_ip": "192.0.2.20", "receiver_port": 30047}
        with mock.patch.object(requests, "get", return_value=mock.Mock(status_code=200)):
            r = asyncio.run(ep(_json_request(body)))
        self.assertTrue(_body(r)["ok"], _body(r))
        self.assertEqual(yaml.safe_load(self.cfgp.read_text())["receiver"]["ip"], "192.0.2.20")
        self.assertEqual(self.lg.read_text(), self.before_text)


class TestAutoBackupNames(_AppCase):
    """Only config.yaml.bak.<8 ASCII digits>-<6 ASCII digits> is an automatic
    backup the server lists and prunes; a name with a trailing newline or
    other digits (\\d and $ would accept both) is someone else's file."""

    def test_odd_names_are_never_listed_or_pruned(self):
        odd = [self.inst / "config.yaml.bak.20260105-000000\n",
               self.inst / "config.yaml.bak.٢٠٢٦٠١٠٥-000000"]
        for p in odd:
            p.write_text("receiver: {ip: 192.0.2.150}\n")
        real = self.inst / "config.yaml.bak.20260101-000000"
        real.write_text(self.before_text)
        self.assertEqual([p for p, _s, _m in self.server._list_config_auto_backups()], [real])
        deleted, _freed = self.server._prune_config_auto_backups(keep=0)
        self.assertEqual(deleted, 1)
        self.assertFalse(real.exists())
        self.assertTrue(all(p.exists() for p in odd))


class TestSaveRefusalBody(_AppCase):

    def test_a_disk_full_save_says_so_with_both_keys(self):
        new = json.loads(json.dumps(self.server.CONFIG))
        new["display"]["date_format"] = "DMY"
        ep = self.ep("/api/config", "PUT")
        with mock.patch.object(atomic_io.os, "fsync", side_effect=ENOSPC):
            r = ep(ep.__annotations__["payload"](config=new))
        self.assertEqual(_status(r), 507)
        b = _body(r)
        msg = "Couldn't save config.yaml: the disk is full. Nothing was changed."
        self.assertEqual((b["error"], b["message"]), (msg, msg))
        self.assertEqual(b["errors"], [{"path": "", "message": msg}])
        self.assertEqual(self.cfgp.read_text(), self.before_text)


class TestConfigImportWritesVerbatim(_AppCase):

    def test_import_keeps_the_uploaded_comments_and_backs_up_privately(self):
        from starlette.datastructures import UploadFile
        text = _commented(self.db)
        up = UploadFile(file=io.BytesIO(text.encode()), filename="config.yaml")
        r = self.ep("/api/config/import")(file=up)
        self.assertTrue(_body(r)["ok"], _body(r))
        self.assertEqual(self.cfgp.read_text(), text)
        bak = self.inst / _body(r)["backup_name"]
        self.assertEqual(bak.read_text(), self.before_text)
        self.assertEqual(_mode(bak), 0o600)
        self.assertEqual(_mode(self.cfgp), 0o600)


# ===========================================================================
# Startup: last-good, recovery, read-only stop/status, Decision 2b
# ===========================================================================

class _MainCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def setUp(self):
        _umask_022(self)
        self.addCleanup(setattr, tempfile, "tempdir", tempfile.tempdir)
        self.inst = _tmpdir(self)
        self.db = self.inst / "aircraft_history.db"
        self.db.write_bytes(b"")
        self.path = self.inst / "config.yaml"
        for p in (mock.patch.object(self.main, "CONFIG_PATH", self.path),
                  mock.patch.object(self.main, "BASE_DIR", self.inst),
                  mock.patch.object(self.main, "PID_FILE", self.inst / ".tracker.pid"),
                  mock.patch.object(self.main, "CONFIG_EXAMPLE_PATH",
                                    REPO / "config.yaml.example")):
            p.start()
            self.addCleanup(p.stop)
        good = _example_cfg(self.db)
        good["receiver"]["ip"] = "192.0.2.77"
        good["web"]["port"] = 18222
        self.good = yaml.safe_dump(good, sort_keys=False)
        self.lg = self.inst / ".backups" / "config.yaml.last-good"

    def load(self, *args):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                return self.main.load_config(*args)
        finally:
            self.out = out.getvalue()

    def seed_last_good(self, text=None):
        self.lg.parent.mkdir(exist_ok=True)
        self.lg.write_text(text or self.good)


class TestStartupRecovery(_MainCase):

    def test_empty_config_recovers_from_last_good(self):
        self.seed_last_good()
        self.path.write_text("")
        cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.77")
        self.assertEqual(yaml.safe_load(self.path.read_text())["receiver"]["ip"], "192.0.2.77")
        broken = list((self.inst / ".backups").glob("config.yaml.broken.*"))
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0].read_text(), "")
        self.assertIn("web port 18222", self.out)
        note = json.loads((self.inst / ".backups" / "config-recovery.json").read_text())
        self.assertEqual(note["source"], ".backups/config.yaml.last-good")
        self.assertEqual(note["web_port"], 18222)
        self.assertEqual(note["broken_copy"], f".backups/{broken[0].name}")
        self.assertIs(note["acknowledged"], False)
        self.assertEqual(note["kind"], "unreadable")
        self.assertIn("empty", note["reason"])

    def test_a_hand_edit_typo_recovers_and_keeps_the_edit(self):
        self.seed_last_good()
        typo = self.good.replace("ip: 192.0.2.77", "ip 192.0.2.99\n  oops: [", 1)
        self.path.write_text(typo)
        cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.77")
        broken = next((self.inst / ".backups").glob("config.yaml.broken.*"))
        self.assertEqual(broken.read_text(), typo)

    def test_cut_short_config_skips_bad_and_dbless_backups(self):
        # No last-good. Newest .bak is cut short, the next one's database is
        # gone (an old .bak must not start Aerodrome on an empty database),
        # the oldest is good. The pre-restore copy (a config the user
        # deliberately replaced) is never used, however new.
        self.path.write_text(self.good[:500])
        (self.inst / "config.yaml.bak.20260103-000000").write_text(
            self.good[:900] + "\nreceiver: [192.0.2")
        gone = yaml.safe_load(self.good)
        gone["data"]["db_file"] = str(self.inst / "archived.db")
        (self.inst / "config.yaml.bak.20260102-000000").write_text(yaml.safe_dump(gone))
        older = self.inst / "config.yaml.bak.20260101-000000"
        older.write_text(self.good)
        pre = yaml.safe_load(self.good)
        pre["receiver"]["ip"] = "192.0.2.200"
        (self.inst / "config.yaml.bak.20260104-000000.pre-restore").write_text(
            yaml.safe_dump(pre))
        os.utime(older, (1, 1))                 # mtime is irrelevant; the name decides
        cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.77")
        note = json.loads((self.inst / ".backups" / "config-recovery.json").read_text())
        self.assertEqual(note["source"], older.name)

    def test_the_newest_backup_by_name_wins_when_mtimes_disagree(self):
        # Guard (a sort-by-mtime mutation survived the suite): a copy keeps
        # its source's mtime, so the name's timestamp decides.
        self.path.write_text("")
        newer, older = yaml.safe_load(self.good), yaml.safe_load(self.good)
        newer["receiver"]["ip"], older["receiver"]["ip"] = "192.0.2.102", "192.0.2.101"
        n = self.inst / "config.yaml.bak.20260102-000000"
        o = self.inst / "config.yaml.bak.20260101-000000"
        n.write_text(yaml.safe_dump(newer))
        o.write_text(yaml.safe_dump(older))
        os.utime(n, (1000, 1000))
        os.utime(o, (2000, 2000))
        cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.102")
        note = json.loads((self.inst / ".backups" / "config-recovery.json").read_text())
        self.assertEqual(note["source"], n.name)

    def test_a_backup_name_with_a_newline_or_other_digits_is_never_used(self):
        self.path.write_text("")
        odd = yaml.safe_load(self.good)
        odd["receiver"]["ip"] = "192.0.2.150"
        for name in ("config.yaml.bak.20260105-000000\n",
                     "config.yaml.bak.\u0662\u0660\u0662\u0666\u0660\u0661\u0660\u0665-000000"):
            (self.inst / name).write_text(yaml.safe_dump(odd))
        good = self.inst / "config.yaml.bak.20260101-000000"
        good.write_text(self.good)
        cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.77")

    def stale_last_good_after_a_downgrade(self):
        """last-good kept by 3.4.148, then 3.4.147 ran (it never refreshes
        it) and saved newer settings; now an update from 3.4.147 is on its
        first start."""
        self.seed_last_good()
        os.utime(self.lg, (time.mktime((2026, 9, 1, 9, 0, 0, 0, 0, -1)),) * 2)
        (self.inst / ".update-pending.json").write_text(json.dumps(
            {"state": "pending_first_boot", "from_version": "3.4.147",
             "to_version": "3.4.148"}))
        newer = yaml.safe_load(self.good)
        newer["receiver"]["ip"] = "192.0.2.120"
        return newer

    def test_a_stale_last_good_after_a_downgrade_yields_to_a_newer_backup(self):
        newer = self.stale_last_good_after_a_downgrade()
        (self.inst / "config.yaml.bak.20261020-120000").write_text(yaml.safe_dump(newer))
        self.path.write_text("")
        cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.120")

    def test_a_stale_last_good_is_still_used_when_nothing_is_newer(self):
        # Guard: it only moves down the list, it isn't dropped.
        self.stale_last_good_after_a_downgrade()
        self.path.write_text("")
        cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.77")

    def test_last_good_comes_first_outside_that_case(self):
        # Guard: no update from an older release pending — last-good first.
        self.seed_last_good()
        newer = yaml.safe_load(self.good)
        newer["receiver"]["ip"] = "192.0.2.120"
        (self.inst / "config.yaml.bak.20991020-120000").write_text(yaml.safe_dump(newer))
        self.path.write_text("")
        self.assertEqual(self.load()["receiver"]["ip"], "192.0.2.77")

    def test_the_kept_copy_is_private_in_an_old_wide_backups_folder(self):
        # A hard link shares config.yaml's mode (often 0644 on an upgrade,
        # and recovery runs before the startup tighten step).
        self.seed_last_good()
        os.chmod(self.inst / ".backups", 0o755)
        self.path.write_text("receiver: [\n")
        os.chmod(self.path, 0o644)
        self.load()
        broken = next((self.inst / ".backups").glob("config.yaml.broken.*"))
        self.assertEqual(_mode(broken), 0o600)
        self.assertEqual(broken.read_text(), "receiver: [\n")

    def test_only_a_pre_restore_copy_exits_with_a_message(self):
        self.path.write_text("receiver: [\n")
        (self.inst / "config.yaml.bak.20260104-000000.pre-restore").write_text(self.good)
        with self.assertRaises(SystemExit) as cm:
            self.load()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("no usable copy", self.out)
        self.assertEqual(self.path.read_text(), "receiver: [\n")

    def test_without_a_kept_copy_config_yaml_is_never_replaced(self):
        # Guard of the new recovery: no copy of the unreadable file (e.g. no
        # room even for that) → run on last-good in memory, file untouched.
        self.seed_last_good()
        self.path.write_text("receiver: [\n")
        with mock.patch.object(config_io, "_keep_broken_copy", return_value=None):
            cfg = self.load()
        self.assertEqual(cfg["receiver"]["ip"], "192.0.2.77")
        self.assertEqual(self.path.read_text(), "receiver: [\n")
        note = json.loads((self.inst / ".backups" / "config-recovery.json").read_text())
        self.assertIs(note["rewritten"], False)
        self.assertIsNone(note["broken_copy"])

    def test_nothing_usable_exits_with_a_message(self):
        self.path.write_text("receiver: [\n")
        with self.assertRaises(SystemExit) as cm:
            self.load()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("no usable copy", self.out)

    def test_recovery_is_skipped_while_another_aerodrome_owns_the_install(self):
        self.seed_last_good()
        self.path.write_text("")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              str(self.inst / "main.py"), "start"], cwd=str(self.inst))
        self.addCleanup(lambda: (p.kill(), p.wait()))
        for _ in range(100):
            try:
                if str(self.inst / "main.py") in Path(f"/proc/{p.pid}/cmdline").read_text():
                    break
            except OSError:
                pass
            time.sleep(0.02)
        (self.inst / ".tracker.pid").write_text(str(p.pid))
        with self.assertRaises(SystemExit) as cm:
            self.load()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn(f"Another Aerodrome (PID {p.pid})", self.out)
        self.assertEqual(self.path.read_text(), "")
        self.assertEqual(list((self.inst / ".backups").glob("config.yaml.broken.*")), [])

    def test_a_permission_problem_never_replaces_the_file(self):
        # Guard: OSError is not "unreadable content" — a root-owned file
        # must never be swapped out.
        self.seed_last_good()
        self.path.write_text(self.good.replace("192.0.2.77", "192.0.2.88"))
        before = self.path.read_bytes()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "denied")), \
                self.assertRaises(PermissionError):
            self.load()
        self.assertEqual(self.path.read_bytes(), before)

    def test_first_run_creates_a_private_copy_of_the_example(self):
        self.assertFalse(self.path.exists())
        cfg = self.load()
        self.assertEqual(self.path.read_text(), EXAMPLE)
        self.assertEqual(_mode(self.path), 0o600)
        self.assertIn("web", cfg)

    def test_a_first_run_copy_cut_short_leaves_no_config_and_the_next_start_works(self):
        # Before: shutil.copy2 — a cut-short first copy was an unreadable
        # config.yaml with nothing to recover from, on every restart.
        with mock.patch.object(atomic_io.os, "fsync", side_effect=ENOSPC), \
                self.assertRaises(SystemExit) as cm:
            self.load()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Could not create config.yaml", self.out)
        self.assertFalse(self.path.exists())
        self.assertEqual(_leftover_tmps(self.inst), [])
        self.load()
        self.assertEqual(self.path.read_text(), EXAMPLE)

    def test_a_render_that_reads_back_differently_takes_the_plain_path(self):
        # Guard of the migration's read-back check (ruamel writes YAML 1.2,
        # the next start reads 1.1): a mismatch never writes the ruamel text.
        cfg = yaml.safe_load(self.good)
        cfg.pop("stats", None)
        text = yaml.safe_dump(cfg, sort_keys=False)
        self.path.write_text(text)
        plain = mock.Mock(return_value={"plain": True})
        with mock.patch.object(self.main, "_plain_merged", return_value={"different": 1}), \
                mock.patch.object(self.main, "_migrate_config_plain", plain):
            got = self.main.migrate_config(yaml.safe_load(text))
        plain.assert_called_once()
        self.assertEqual(got, {"plain": True})
        self.assertEqual(self.path.read_text(), text)

    def test_migration_write_failure_leaves_config_untouched(self):
        cfg = yaml.safe_load(self.good)
        cfg.pop("stats")
        text = yaml.safe_dump(cfg, sort_keys=False)
        self.path.write_text(text)
        real = config_io.write_text

        def fail_on_live(path, t, check=True):
            if Path(path).name == "config.yaml":
                raise config_io.ConfigWriteError("full", disk_full=True)
            return real(path, t, check=check)
        with mock.patch.object(config_io, "write_text", side_effect=fail_on_live), \
                contextlib.redirect_stdout(io.StringIO()):
            self.main.migrate_config(yaml.safe_load(text))
        self.assertEqual(self.path.read_text(), text)


class TestStopAndStatusAreReadOnly(_MainCase):
    """`main.py stop` / `status` run while the service runs (ExecStop is
    `main.py stop`): they must never migrate, recover or rewrite anything."""

    def run_main(self, command):
        with mock.patch.object(sys, "argv", ["main.py", command]), \
                mock.patch.object(self.main, "setup_logging"), \
                mock.patch.object(self.main, command) as fn, \
                contextlib.redirect_stdout(io.StringIO()):
            self.main.main()
        return fn

    def test_an_older_config_is_not_migrated(self):
        cfg = yaml.safe_load(self.good)
        cfg.pop("stats")                         # a migration would add it back
        self.path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)
        for command in ("status", "stop"):
            with self.subTest(command=command):
                fn = self.run_main(command)
                fn.assert_called_once()
                self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
                self.assertEqual(list(self.inst.glob("config.yaml.bak.*")), [])

    def test_an_unreadable_config_is_left_alone(self):
        self.seed_last_good()
        self.path.write_text("")
        for command in ("status", "stop"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                self.run_main(command)
            self.assertEqual(self.path.read_text(), "")
            self.assertEqual(list((self.inst / ".backups").glob("config.yaml.broken.*")), [])

    def test_a_missing_config_is_not_created(self):
        for command in ("status", "stop"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                self.run_main(command)
            self.assertFalse(self.path.exists())


class TestStartLeavesAnOwnedInstallAlone(_MainCase):

    def test_no_migration_while_another_aerodrome_owns_the_install(self):
        # `main.py start` by hand while the service runs: start() refuses
        # ("Already running"), and nothing may be rewritten before that.
        cfg = yaml.safe_load(self.good)
        cfg.pop("stats")                         # a migration would add it back
        text = yaml.safe_dump(cfg, sort_keys=False)
        self.path.write_text(text)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              str(self.inst / "main.py"), "start"], cwd=str(self.inst))
        self.addCleanup(lambda: (p.kill(), p.wait()))
        for _ in range(100):
            try:
                if str(self.inst / "main.py") in Path(f"/proc/{p.pid}/cmdline").read_text():
                    break
            except OSError:
                pass
            time.sleep(0.02)
        (self.inst / ".tracker.pid").write_text(str(p.pid))
        self.load()
        self.assertEqual(self.path.read_text(), text)
        self.assertEqual(list(self.inst.glob("config.yaml.bak.*")), [])


class _Stop(Exception):
    pass


class _ExecCalled(Exception):
    pass


class TestStartWritesLastGoodAndRecoversAnUnbindableAddress(_MainCase):
    """A config that STARTED is the last one that worked; with Decision 2b,
    an address that can't be listened on falls back to last-good's."""

    def start(self, cfg, bind):
        calls = []

        def execv(exe, argv):
            calls.append(argv)
            raise _ExecCalled()
        with mock.patch.object(self.main, "init_db"), \
                mock.patch("collector.rollup_backfill_incomplete", return_value=[]), \
                mock.patch("schema_migrations.apply_schema_migrations",
                           return_value={"ok": True, "applied": [], "starting_version": 18}), \
                mock.patch("schema_migrations.verify_stats_schema"), \
                mock.patch("server._recompute_all_last_distance"), \
                mock.patch.object(self.main, "run_collector", lambda *a, **k: None), \
                mock.patch.object(self.main, "set_db_path"), \
                mock.patch.object(self.main, "set_receiver_location"), \
                mock.patch.object(self.main, "get_app", return_value=object()), \
                mock.patch.object(self.main, "_preflight_bind_check", side_effect=bind), \
                mock.patch.object(self.main.signal, "signal"), \
                mock.patch.object(self.main.uvicorn, "run", side_effect=_Stop), \
                mock.patch.object(self.main.os, "execv", side_effect=execv), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                self.main.start(cfg)
            except (_Stop, _ExecCalled):
                pass
        return calls

    def test_a_good_start_records_last_good(self):
        self.path.write_text(self.good)
        cfg = yaml.safe_load(self.good)
        self.start(cfg, lambda h, p: (True, ""))
        self.assertEqual(self.lg.read_text(), self.good)
        self.assertEqual(_mode(self.lg), 0o600)

    def test_unbindable_address_starts_on_last_goods_and_reexecs(self):
        self.seed_last_good()                        # web 127.0.0.1:18222
        hand = self.good.replace("host: 127.0.0.1", "host: 192.0.2.10")
        self.path.write_text(hand)
        cfg = yaml.safe_load(hand)

        def bind(h, p):
            return (h, p) != ("192.0.2.10", 18222), "OSError: [Errno 99] Cannot assign"
        calls = self.start(cfg, bind)
        self.assertEqual(calls, [[sys.executable, str(self.inst / "main.py"), "start"]])
        self.assertEqual(self.path.read_text(), self.good)
        broken = next((self.inst / ".backups").glob("config.yaml.broken.*"))
        self.assertEqual(broken.read_text(), hand)
        note = json.loads((self.inst / ".backups" / "config-recovery.json").read_text())
        self.assertEqual((note["kind"], note["web_host"], note["web_port"], note["tried"]),
                         ("bind", "127.0.0.1", 18222, "192.0.2.10:18222"))

    def test_same_address_as_last_good_exits_2_as_before(self):
        # Guard: nothing to fall back to — the exit stays as it was.
        self.seed_last_good()
        self.path.write_text(self.good)
        with self.assertRaises(SystemExit) as cm:
            self.start(yaml.safe_load(self.good), lambda h, p: (False, "in use"))
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.path.read_text(), self.good)

    def test_a_port_saved_in_settings_and_busy_at_restart_falls_back(self):
        # The config that started is last-good; a Settings save of a new
        # port does NOT replace it — so when that port turns out to be held
        # by another program at the restart, Aerodrome starts on the port
        # that last worked. (Before: the save wrote last-good, both had the
        # busy port, and it exited 2 into a restart loop, web UI gone.)
        self.path.write_text(self.good)                      # 127.0.0.1:18222
        self.start(yaml.safe_load(self.good), lambda h, p: (True, ""))
        saved = yaml.safe_load(self.good)
        saved["web"]["port"] = 18223
        config_io.save_config(self.path, saved)
        self.assertEqual(yaml.safe_load(self.lg.read_text())["web"]["port"], 18222)

        def bind(h, p):
            return p != 18223, "OSError: [Errno 98] Address already in use"
        calls = self.start(saved, bind)
        self.assertEqual(calls, [[sys.executable, str(self.inst / "main.py"), "start"]])
        self.assertEqual(yaml.safe_load(self.path.read_text())["web"]["port"], 18222)
        broken = next((self.inst / ".backups").glob("config.yaml.broken.*"))
        self.assertEqual(yaml.safe_load(broken.read_text())["web"]["port"], 18223)

    def test_a_busy_port_on_the_same_host_falls_back(self):
        # Guard: a port-only change keeps the 2b rescue.
        self.seed_last_good()                        # 127.0.0.1:18222
        hand = self.good.replace("port: 18222", "port: 18224")
        self.path.write_text(hand)
        calls = self.start(yaml.safe_load(hand),
                           lambda h, p: (p != 18224, "OSError: [Errno 98] Address already in use"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.path.read_text(), self.good)

    def test_a_narrowed_address_never_falls_back_to_listening_on_all(self):
        # The user narrowed web.host to one interface (to keep the web UI
        # off other networks); at boot that address isn't up yet. Falling
        # back to last-good's 0.0.0.0 would serve on every network — for
        # good, since config.yaml would be rewritten. Exit 2 (systemd
        # retries until the address is up) and leave the file alone.
        wide = self.good.replace("host: 127.0.0.1", "host: 0.0.0.0")
        self.seed_last_good(wide)
        hand = self.good.replace("host: 127.0.0.1", "host: 192.0.2.55")
        self.path.write_text(hand)
        before = self.path.read_bytes()
        with self.assertRaises(SystemExit) as cm:
            self.start(yaml.safe_load(hand),
                       lambda h, p: (h != "192.0.2.55", "OSError: [Errno 99] Cannot assign"))
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list((self.inst / ".backups").glob("config.yaml.broken.*")), [])

    def test_this_machine_only_never_falls_back_to_a_network_address(self):
        lan = self.good.replace("host: 127.0.0.1", "host: 192.0.2.5")
        self.seed_last_good(lan)
        hand = self.good.replace("port: 18222", "port: 18225")       # 127.0.0.1
        self.path.write_text(hand)
        with self.assertRaises(SystemExit) as cm:
            self.start(yaml.safe_load(hand), lambda h, p: (p != 18225, "in use"))
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.path.read_text(), hand)

    def test_a_stale_last_good_never_replaces_an_unbindable_config(self):
        # After a downgrade to a release that never refreshes last-good and
        # an update back, last-good may be older than config.yaml.
        self.seed_last_good()
        (self.inst / ".update-pending.json").write_text(json.dumps(
            {"state": "pending_first_boot", "from_version": "3.4.147",
             "to_version": "3.4.148"}))
        hand = self.good.replace("port: 18222", "port: 18226")
        self.path.write_text(hand)
        with self.assertRaises(SystemExit) as cm:
            self.start(yaml.safe_load(hand), lambda h, p: (p != 18226, "in use"))
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.path.read_text(), hand)

    def test_a_brief_failure_on_last_goods_own_address_rewrites_nothing(self):
        # Guard (dropping the same-address check survived the suite before:
        # its old test's bind always failed). config.yaml IS last-good; the
        # bind fails once (an address not up
        # yet), then would succeed. Nothing to fall back to: exit 2, no
        # broken copy, no notice.
        self.seed_last_good()
        self.path.write_text(self.good)
        n = [0]

        def bind(h, p):
            n[0] += 1
            return n[0] > 1, "OSError: [Errno 99] Cannot assign"
        with self.assertRaises(SystemExit) as cm:
            self.start(yaml.safe_load(self.good), bind)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(list((self.inst / ".backups").glob("config.yaml.broken.*")), [])
        self.assertFalse((self.inst / ".backups" / "config-recovery.json").exists())

    def test_last_goods_address_unbindable_too_exits_2(self):
        # Guard: last-good's address can't be used either — exit as before,
        # config.yaml untouched.
        self.seed_last_good()
        hand = self.good.replace("host: 127.0.0.1", "host: 192.0.2.10")
        self.path.write_text(hand)
        with self.assertRaises(SystemExit) as cm:
            self.start(yaml.safe_load(hand), lambda h, p: (False, "nope"))
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.path.read_text(), hand)


class TestRecoveryNoticeInStatus(_MainCase):

    def test_status_shows_the_recovery_and_ack_marks_it_seen(self):
        import server
        _make_db(self.db)
        self.seed_last_good()
        self.path.write_text("")
        cfg = self.load()
        with mock.patch.object(server, "__file__", str(self.inst / "server.py")):
            app = server.get_app(cfg, str(self.path))
            st = _endpoint(app, "/api/maintenance/status", "GET")()
            self.assertEqual(st["config"]["web_port"], 18222)
            self.assertIs(st["config"]["acknowledged"], False)
            ack = _endpoint(app, "/api/maintenance/ack", "POST")
            model = ack.__annotations__["body"]
            self.assertEqual(ack(model(kind="config")), {"ok": True, "acknowledged": True})
            self.assertIs(_endpoint(app, "/api/maintenance/status", "GET")()["config"]
                          ["acknowledged"], True)


class TestRecoverKillPoints(unittest.TestCase):
    """Guard of the new recovery code. §2.2 rule 6: a kill at every filesystem operation of config_io.recover,
    then the next start (a fresh process) — ends with a loadable config.yaml
    equal to last-good, and the user's unreadable file kept in a broken copy
    whenever config.yaml was replaced."""

    CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
import config_io, atomic_io, tempfile
path, kill_at = sys.argv[2], int(sys.argv[3])
n = [0]
def wrap(mod, name):
    real = getattr(mod, name)
    def f(*a, **kw):
        n[0] += 1
        if n[0] == kill_at:
            os._exit(137)
        return real(*a, **kw)
    setattr(mod, name, f)
for name in ("link", "replace", "rename", "fsync", "mkdir", "chmod", "fchmod", "unlink"):
    wrap(os, name)
wrap(tempfile, "mkstemp")
try:
    config_io.check_loadable(open(path).read())
    print("LOADABLE")
except ValueError as e:
    cfg, info = config_io.recover(path, e, log=lambda m: None)
    print("RECOVERED" if cfg else "NOTHING")
print("OPS", n[0])
'''

    def run_child(self, path, kill_at):
        return subprocess.run([sys.executable, "-c", self.CHILD, str(REPO), str(path),
                               str(kill_at)], capture_output=True, text=True, timeout=60)

    def test_every_kill_point_replays_to_a_good_config(self):
        good = yaml.safe_dump(_example_cfg("/nonexistent/h.db"), sort_keys=False)
        broken_text = "receiver: [\n"
        probe = _tmpdir(self)
        (probe / ".backups").mkdir()
        (probe / ".backups" / "config.yaml.last-good").write_text(good)
        (probe / "config.yaml").write_text(broken_text)
        r = self.run_child(probe / "config.yaml", 0)
        total = int(r.stdout.split("OPS")[-1])
        self.assertGreater(total, 5, r.stdout + r.stderr)
        for k in range(1, total + 1):
            with self.subTest(kill_at=k):
                d = _tmpdir(self)
                (d / ".backups").mkdir()
                (d / ".backups" / "config.yaml.last-good").write_text(good)
                (d / "config.yaml").write_text(broken_text)
                first = self.run_child(d / "config.yaml", k)
                self.assertEqual(first.returncode, 137, first.stdout + first.stderr)
                second = self.run_child(d / "config.yaml", 0)      # the next start
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual((d / "config.yaml").read_text(), good)
                copies = [p.read_text() for p in (d / ".backups").glob("config.yaml.broken.*")]
                self.assertIn(broken_text, copies)
                # Never a silent recovery: the notice names a kept copy of
                # the user's file (before: a kill right after the replace
                # left neither the WARNING nor the notice).
                note = d / ".backups" / "config-recovery.json"
                self.assertTrue(note.exists(), first.stdout)
                kept = json.loads(note.read_text())["broken_copy"]
                self.assertEqual((d / kept).read_text(), broken_text)


# ===========================================================================
# Private file modes (umask 0022)
# ===========================================================================

class TestPrivateFileModes(_AppCase):

    def test_settings_save_makes_config_yaml_0600(self):
        os.chmod(self.cfgp, 0o644)
        new = json.loads(json.dumps(self.server.CONFIG))
        new["display"]["date_format"] = "DMY"
        ep = self.ep("/api/config", "PUT")
        self.assertEqual(_status(ep(ep.__annotations__["payload"](config=new))), 200)
        self.assertEqual(_mode(self.cfgp), 0o600)

    def test_server_side_backup_zip_is_0600_in_a_0700_folder(self):
        r = self.ep("/api/backup/create-server-side")()
        b = _body(r)
        self.assertTrue(b["ok"], b)
        self.assertEqual(_mode(b["path"]), 0o600)
        self.assertEqual(_mode(self.inst / ".backups"), 0o700)

    def test_backup_upload_is_staged_0600(self):
        from starlette.datastructures import UploadFile
        seen = {}
        import zipfile

        def spy(path, *a, **kw):
            if str(path).endswith(".zip") and ".uploads" in str(path):
                seen["mode"] = _mode(path)
                seen["dir"] = _mode(Path(path).parent)
            raise zipfile.BadZipFile("stop here")
        up = UploadFile(file=io.BytesIO(b"PK" + b"\0" * 64), filename="b.zip")
        with mock.patch.object(zipfile, "ZipFile", side_effect=spy):
            asyncio.run(self.ep("/api/backup/import")(file=up, background_tasks=None))
        self.assertEqual(seen, {"mode": 0o600, "dir": 0o700})


class TestTightenExistingPrivateFiles(unittest.TestCase):
    """The DECISION function (main calls it behind one switch,
    main.TIGHTEN_EXISTING_PRIVATE_FILES): files earlier versions left
    readable by other local accounts are made private; the database files
    are left alone."""

    def setUp(self):
        _umask_022(self)
        self.inst = _tmpdir(self)
        b = self.inst / ".backups"
        b.mkdir()
        os.chmod(b, 0o755)
        self.zip = b / "aerodrome-backup-20260101-000000.zip"
        self.zip.write_bytes(b"PK")
        os.chmod(self.zip, 0o644)
        self.sha = b / "aerodrome-backup-20260101-000000.zip.sha256"
        self.sha.write_text("x\n")
        self.cfg = self.inst / "config.yaml"
        self.cfg.write_text("a: 1\n")
        os.chmod(self.cfg, 0o644)
        self.db = self.inst / "aircraft_history.db"
        self.db.write_bytes(b"")
        os.chmod(self.db, 0o644)

    def test_tightens_backups_zips_and_config_but_not_the_database(self):
        import server
        with self.assertLogs("adsb.server", "INFO") as logs:
            changed = server.tighten_existing_private_files(self.inst)
        self.assertEqual(set(changed), {self.inst / ".backups", self.zip, self.cfg})
        self.assertEqual(_mode(self.inst / ".backups"), 0o700)
        self.assertEqual(_mode(self.zip), 0o600)
        self.assertEqual(_mode(self.cfg), 0o600)
        self.assertEqual(_mode(self.db), 0o644)
        self.assertEqual(_mode(self.sha), 0o644)
        self.assertEqual(len([m for m in logs.output if "readable by this account only" in m]), 3)
        # Already private: nothing to do, nothing logged.
        self.assertEqual(server.tighten_existing_private_files(self.inst, force=True), [])

    def test_the_copies_of_config_yaml_next_to_it_too(self):
        # Earlier versions made them with shutil.copy2 (config.yaml's 0644)
        # or a plain write: full copies of the API keys and ntfy token.
        import server
        copies = [self.inst / "config.yaml.bak.20261001-120000",
                  self.inst / "config.yaml.bak.20261002-120000.pre-restore",
                  self.inst / "ntfy-server.yml.from-backup.20261003-120000"]
        for c in copies:
            c.write_text("token: invented-0000\n")
            os.chmod(c, 0o644)
        db_pre = self.inst / "aircraft_history.db.bak.20261002-120000.pre-restore"
        db_pre.write_bytes(b"")
        os.chmod(db_pre, 0o644)
        elsewhere = self.inst / "elsewhere"
        elsewhere.write_text("x")
        os.chmod(elsewhere, 0o644)
        (self.inst / "config.yaml.bak.20261004-120000").symlink_to(elsewhere)
        with self.assertLogs("adsb.server", "INFO") as logs:
            changed = server.tighten_existing_private_files(self.inst)
        for c in copies:
            self.assertEqual(_mode(c), 0o600, c)
            self.assertIn(c, changed)
        self.assertEqual(_mode(db_pre), 0o644)          # the database is left alone
        self.assertEqual(_mode(elsewhere), 0o644)       # a symlinked copy isn't followed
        self.assertEqual(len([m for m in logs.output if "readable by this account only" in m]),
                         3 + len(copies))

    def test_it_runs_once_so_a_mode_opened_up_later_is_kept(self):
        import server
        server.tighten_existing_private_files(self.inst)
        self.assertTrue((self.inst / ".backups" / ".private-modes-applied").exists())
        os.chmod(self.inst / ".backups", 0o750)        # e.g. for a backup job's group
        os.chmod(self.zip, 0o640)
        self.assertEqual(server.tighten_existing_private_files(self.inst), [])
        self.assertEqual((_mode(self.inst / ".backups"), _mode(self.zip)), (0o750, 0o640))
        # force=True (or deleting the marker) runs it again.
        server.tighten_existing_private_files(self.inst, force=True)
        self.assertEqual((_mode(self.inst / ".backups"), _mode(self.zip)), (0o700, 0o600))

    @unittest.skipIf(_AS_ROOT, "root may change any file")
    def test_another_accounts_files_are_skipped_without_a_warning(self):
        import server
        with mock.patch.object(server.os, "geteuid", return_value=os.getuid() + 4242), \
                self.assertNoLogs("adsb.server", "WARNING"):
            self.assertEqual(server.tighten_existing_private_files(self.inst), [])
        self.assertEqual(_mode(self.cfg), 0o644)

    def test_a_symlinked_config_tightens_its_target(self):
        import server
        real = self.inst / "elsewhere.yaml"
        self.cfg.rename(real)
        self.cfg.symlink_to(real)
        server.tighten_existing_private_files(self.inst)
        self.assertEqual(_mode(real), 0o600)

    def test_one_switch_in_main_turns_it_off(self):
        # Both calls (a start with nothing pending, the update confirm) are
        # behind main.TIGHTEN_EXISTING_PRIVATE_FILES; no other call.
        import main
        self.assertIs(main.TIGHTEN_EXISTING_PRIVATE_FILES, True)
        src = (REPO / "main.py").read_text()
        guarded = re.findall(r"^(\s*)if TIGHTEN_EXISTING_PRIVATE_FILES\b[^\n]*:\n"
                             r"\1    tighten_existing_private_files\((?:inst|BASE_DIR)\)\s*$",
                             src, re.M)
        self.assertEqual(len(guarded), 2)
        self.assertEqual(len(re.findall(r"^[^#\n]*\btighten_existing_private_files\(",
                                        src, re.M)), 2)


class TestPrivateModesWaitUntilNothingIsPending(_MainCase):
    """The step above runs at a start with no update or restore still to be
    confirmed, and once an update is confirmed. Before, the first start of
    a just-applied update ran it: when that release then failed its starts,
    the rollback put config.yaml back from the pre-update copy with its old
    mode, and the step, recorded as done, never ran again — config.yaml
    stayed readable by other local accounts."""

    def setUp(self):
        super().setUp()
        self.marker = self.inst / ".backups" / ".private-modes-applied"
        (self.inst / ".backups").mkdir()
        os.chmod(self.inst / ".backups", 0o755)
        self.zip = self.inst / ".backups" / "aerodrome-backup-20260101-000000.zip"
        self.zip.write_bytes(b"PK")
        os.chmod(self.zip, 0o644)
        self.path.write_text(self.good)
        os.chmod(self.path, 0o644)

    def modes(self):
        return _mode(self.inst / ".backups"), _mode(self.zip), _mode(self.path)

    def apply_update(self):
        """A just-applied update: journal pending_first_boot, config.yaml's
        pre-update copy (0644) kept for a rollback."""
        _fake_release(self.inst, OLD)
        staged = _fake_release(_tmpdir(self) / "staged", NEW)
        staging = updater.stage(self.inst, staged, PRESERVE, "20261021-140000")
        updater.swap_in(self.inst, staging, "20261021-140000", OLD, NEW)

    def test_not_at_a_start_an_update_or_a_restore_is_pending_for(self):
        for name, text in ((".update-pending.json", '{"state": "pending_first_boot"}'),
                           (".backups/pending-restore.json", '{"phase": "staged"}')):
            with self.subTest(name):
                (self.inst / name).write_text(text)
                self.main._prepare_install_files(self.inst)
                self.assertFalse(self.marker.exists())
                self.assertEqual(self.modes(), (0o755, 0o644, 0o644))
                (self.inst / name).unlink()

    def test_at_a_start_nothing_is_pending_for(self):
        # Guard: the step itself still runs.
        self.main._prepare_install_files(self.inst)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.modes(), (0o700, 0o600, 0o600))

    def test_once_the_update_is_confirmed(self):
        self.apply_update()

        def confirm():
            with mock.patch.object(updater, "HEALTHY_AFTER_SEC", 0), \
                    self.assertLogs("adsb.main", "INFO"):
                self.main._confirm_update_when_serving(
                    "127.0.0.1", 18222, NEW, probe=lambda a, h, p: ("ok", NEW),
                    sleep=lambda s: None, exit_fn=mock.Mock())
        # commit() raises, then finds a journal it can't commit: not confirmed.
        with mock.patch.object(updater, "commit", side_effect=[ENOSPC, False]):
            confirm()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.modes(), (0o755, 0o644, 0o644))
        confirm()
        self.assertIsNone(updater.pending(self.inst))
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.modes(), (0o700, 0o600, 0o600))

    def test_after_a_rolled_back_update_at_the_next_start(self):
        # The new release's first start (pending), then it fails its starts
        # and is rolled back — config.yaml put back from its pre-update copy,
        # 0644 — then a later start with nothing pending.
        self.apply_update()
        self.main._prepare_install_files(self.inst)
        marked_while_pending = self.marker.exists()
        updater.swap_back(self.inst, updater.pending(self.inst),
                          reason=f"{NEW} didn't start (2 attempts)")
        self.assertIsNone(updater.pending(self.inst))
        self.main._prepare_install_files(self.inst)
        self.assertEqual(_mode(self.path), 0o600, "config.yaml left readable by other accounts")
        self.assertTrue(self.marker.exists())
        self.assertFalse(marked_while_pending, "recorded as done at the pending start")

    def test_a_backups_folder_this_account_cannot_search(self):
        # On Python 3.10–3.12 Path.exists() raises PermissionError where it
        # can't search the folder (newer versions return False). Looking for
        # a restore marker there must not fail the start (before init_db,
        # looping under Restart=on-failure): nothing visible is pending.
        bdir = self.inst / ".backups"
        real_exists = Path.exists

        def exists(p, *args, **kwargs):
            if bdir in p.parents:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(p))
            return real_exists(p, *args, **kwargs)
        with mock.patch.object(Path, "exists", exists):
            self.assertFalse(config_io.pending(self.path))
            self.main._prepare_install_files(self.inst)
        self.assertEqual(self.modes(), (0o700, 0o600, 0o600))


class TestSpoolDir(unittest.TestCase):

    def test_prepare_backup_tmp_is_0700_even_when_it_existed_wider(self):
        import server
        _umask_022(self)
        inst = _tmpdir(self)
        (inst / ".backups" / ".tmp").mkdir(parents=True)
        os.chmod(inst / ".backups" / ".tmp", 0o755)
        d = server.prepare_backup_tmp(inst)
        self.assertEqual(d, inst / ".backups" / ".tmp")
        self.assertEqual(_mode(d), 0o700)

    def test_a_symlinked_spool_dir_is_refused_and_its_target_left_alone(self):
        # Spools and backups are never written into a folder somewhere else,
        # and its mode is never changed.
        import server
        _umask_022(self)
        inst = _tmpdir(self)
        other = _tmpdir(self)
        os.chmod(other, 0o755)
        (other / "keep.txt").write_text("x")
        (inst / ".backups").mkdir()
        (inst / ".backups" / ".tmp").symlink_to(other)
        with self.assertLogs("adsb.server", "WARNING"):
            self.assertIsNone(server.prepare_backup_tmp(inst))
        self.assertEqual(_mode(other), 0o755)
        self.assertTrue((other / "keep.txt").exists())
        with self.assertRaises(OSError):
            server._backup_tmp_dir(inst)

    def test_a_private_file_is_never_opened_through_a_planted_name(self):
        d = _tmpdir(self)
        victim = d / "victim"
        victim.write_text("keep me")
        (d / "upload-1-2.zip").symlink_to(victim)
        with self.assertRaises(OSError):
            atomic_io.open_private(d / "upload-1-2.zip")
        self.assertEqual(victim.read_text(), "keep me")
        (d / "existing.zip").write_text("old")
        with self.assertRaises(FileExistsError):
            atomic_io.open_private(d / "existing.zip")
        with atomic_io.open_private(d / "new.zip") as f:
            f.write(b"x")
        self.assertEqual(_mode(d / "new.zip"), 0o600)

    def test_start_points_tempfile_at_the_spool_dir(self):
        import main
        self.addCleanup(setattr, tempfile, "tempdir", tempfile.tempdir)
        inst = _tmpdir(self)
        with mock.patch.object(main, "tighten_existing_private_files"):
            main._prepare_install_files(inst)
        self.assertEqual(tempfile.gettempdir(), str(inst / ".backups" / ".tmp"))
        fd, p = tempfile.mkstemp()
        os.close(fd)
        self.assertEqual(Path(p).parent, inst / ".backups" / ".tmp")
        self.assertEqual(_mode(p), 0o600)


# ===========================================================================
# Static guards
# ===========================================================================

class TestNoRawConfigWrites(unittest.TestCase):
    """Every config.yaml write goes through config_io: no call in server.py,
    collector.py or main.py that writes, copies, moves or renames onto a
    config path (or opens one for writing) other than through `config_io.`.
    An AST walk, so the argument's position doesn't matter (before, a
    regex missed `shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)` — the
    first-run copy — and several other shapes)."""

    NAMES = {"CONFIG_PATH", "cfg_path", "config_path", "backup_path", "cfg_backup_path"}
    WRITERS = {"open", "write_text", "write_bytes", "copy", "copy2", "copyfile", "move",
               "replace", "rename", "link", "symlink_to", "touch", "write_atomic",
               "open_private", "stage_bytes"}
    READ_MODES = {"r", "rb", "rt"}

    @classmethod
    def raw_writes(cls, src):
        import ast
        hits = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name not in cls.WRITERS:
                continue
            if isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "config_io":
                continue
            mentions = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & cls.NAMES
            if not mentions:
                continue
            if name == "open":
                at = 0 if isinstance(f, ast.Attribute) and getattr(f.value, "id", "") != "os" \
                    else 1                              # Path.open(mode) / open(path, mode)
                mode = node.args[at] if len(node.args) > at else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if mode is None or (isinstance(mode, ast.Constant)
                                    and mode.value in cls.READ_MODES):
                    continue                            # a read
            hits.append(f"line {node.lineno}: {ast.unparse(node)[:100]}")
        return hits

    def test_the_guard_catches_every_shape(self):
        # Guard of the guard: each shape a raw write can take.
        for line in ("open(CONFIG_PATH, 'w')", "open(str(CONFIG_PATH), 'w')",
                     "Path(CONFIG_PATH).open('w')", "open(CONFIG_PATH, 'r+')",
                     "open(CONFIG_PATH, mode='a')", "CONFIG_PATH.write_text(x)",
                     "Path(cfg_path).write_bytes(b)", "shutil.copy2(CONFIG_PATH, dst)",
                     "shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)",
                     "shutil.copyfile(src, cfg_path)", "shutil.move(tmp, CONFIG_PATH)",
                     "os.replace(tmp, CONFIG_PATH)", "os.rename(tmp, config_path)",
                     "atomic_io.write_atomic(CONFIG_PATH, t)"):
            with self.subTest(line=line):
                self.assertEqual(len(self.raw_writes(line)), 1, line)
        for line in ("open(CONFIG_PATH)", "open(CONFIG_PATH, 'rb')", "CONFIG_PATH.read_text()",
                     "config_io.write_text(CONFIG_PATH, t)", "config_io.commit(tmp, cfg_path)"):
            with self.subTest(line=line):
                self.assertEqual(self.raw_writes(line), [], line)

    def test_sources(self):
        for name in ("server.py", "collector.py", "main.py"):
            hits = self.raw_writes((REPO / name).read_text())
            self.assertEqual(hits, [], f"{name}:\n" + "\n".join(hits))

    def test_config_io_parses_under_python_3_10(self):
        # Guard: the README's Python floor.
        import ast
        ast.parse((REPO / "config_io.py").read_text(), feature_version=(3, 10))


if __name__ == "__main__":
    unittest.main()
