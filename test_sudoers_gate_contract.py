"""The sudoers-drift gate's contract with the Updates page (B5 carrier).

Whatever the staging layout, a release whose install.sh carries a newer
SUDOERS_VERSION than the live /etc/sudoers.d/aerodrome is refused by
POST /api/updates/local/apply (409) and reported by GET
/api/updates/local/check — and both carry `sudoers.recovery_command`, the
staged scripts/recover-sudoers.sh told the install dir (--install-dir), which
is what the "Sudoers update required" prompt shows. /api/sudoers/status
carries the installed release's command in every reply shape. A missing or
unreadable drop-in never blocks an apply (installs without the read grant
must still be able to update).

Later releases that rework apply must keep this file green.

Runs a COPY of this release's Python files from a temp install dir (one with
a space in its path) — the endpoints write into the install dir they live in,
so the real tree is never used. server.SUDOERS_DROPIN points at a fake file and
subprocess.run is a recorder: no sudo, systemctl or pip runs.
"""
import importlib.util
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collector
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _endpoint(app, path, method):
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))


def _copy_release(dest):
    """The files the update endpoints need: top-level modules, VERSION, the
    installers and scripts/ shell scripts."""
    dest.mkdir(parents=True)
    for p in REPO.iterdir():
        if p.is_file() and (p.suffix == ".py" or p.name in (
                "VERSION", "install.sh", "uninstall.sh", "config.yaml.example", "requirements.txt")):
            shutil.copy2(p, dest / p.name)
    (dest / "scripts").mkdir()
    for p in (REPO / "scripts").glob("*.sh"):
        shutil.copy2(p, dest / "scripts" / p.name)
    (dest / "update").mkdir()
    (dest / "static").mkdir()          # get_app mounts it (contents not needed here)
    return dest


_REAL_RUN = subprocess.run


class _Recorder:
    """subprocess.run stand-in. `sudo -n cat <drop-in>` is answered per mode.
    Anything that isn't sudo (an update's test-load, since v3.4.147) runs."""

    def __init__(self):
        self.calls, self.cat_reply = [], None

    def __call__(self, argv, *a, **k):
        argv = list(argv)
        if argv[:1] != ["sudo"]:
            return _REAL_RUN(argv, *a, **k)
        self.calls.append(argv)
        if argv[:3] == ["sudo", "-n", "cat"]:
            if self.cat_reply is None:
                return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required\n")
            return subprocess.CompletedProcess(argv, 0, self.cat_reply, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class _GateCase(unittest.TestCase):
    PARENT = "srv apps"     # the install path has a space in it

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="aerodrome-gate-contract-")
        cls.tmp = Path(cls._tmp.name)
        cls.root = _copy_release(cls.tmp / cls.PARENT / "aerodrome")
        name = f"_server_contract_{abs(hash(str(cls.root)))}"
        spec = importlib.util.spec_from_file_location(name, cls.root / "server.py")
        cls.srv = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.srv
        spec.loader.exec_module(cls.srv)
        cls.modname = name

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.modname, None)
        cls._tmp.cleanup()

    def setUp(self):
        self.dropin = self.tmp / "fake-etc" / "aerodrome"
        self.dropin.parent.mkdir(exist_ok=True)
        self.set_live("# SUDOERS_VERSION: 4\nsvc-a ALL=(ALL) NOPASSWD: /bin/true\n")
        self.rec = _Recorder()
        db = self.tmp / "h.db"
        if not db.exists():
            collector.init_db(str(db))
            c = sqlite3.connect(str(db))
            apply_schema_migrations(c, "test")
            c.close()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = str(db)
        cfg["watchlist"] = []
        cfgp = self.tmp / "config.yaml"
        cfgp.write_text(yaml.safe_dump(cfg))
        for p in (mock.patch.object(self.srv, "SUDOERS_DROPIN", self.dropin),
                  mock.patch("subprocess.run", self.rec)):
            p.start()
            self.addCleanup(p.stop)
        app = self.srv.get_app(cfg, str(cfgp))
        self.check = _endpoint(app, "/api/updates/local/check", "GET")
        self.apply = _endpoint(app, "/api/updates/local/apply", "POST")
        self.status = _endpoint(app, "/api/sudoers/status", "GET")
        self.addCleanup(self.clear_update)

    # -- fixtures ------------------------------------------------------------
    def set_live(self, text=None, mode=0o644):
        if self.dropin.exists():
            self.dropin.chmod(0o644)
            self.dropin.unlink()
        if text is not None:
            self.dropin.write_text(text)
            self.dropin.chmod(mode)

    def clear_update(self):
        upd = self.root / "update"
        for item in upd.iterdir():
            shutil.rmtree(item) if item.is_dir() else item.unlink()

    def stage(self, layout):
        self.clear_update()
        st = self.root / "update" if layout == "flat" else self.root / "update" / "aerodrome"
        (st / "scripts").mkdir(parents=True, exist_ok=True)
        (st / "VERSION").write_text("9.9.9\n")
        # loadable: since v3.4.147 an apply test-loads the release first
        (st / "main.py").write_text("# staged\ndef main(): pass\ndef start(): pass\n")
        (st / "server.py").write_text("# staged\ndef get_app(*a): pass\n")
        shutil.copy2(REPO / "install.sh", st / "install.sh")
        shutil.copy2(REPO / "scripts" / "recover-sudoers.sh", st / "scripts" / "recover-sudoers.sh")
        return st

    def staged_command(self, st):
        inst = Path(self.srv.__file__).resolve().parent
        return (f"sudo bash {shlex.quote(str(st / 'scripts' / 'recover-sudoers.sh'))} "
                f"--install-dir {shlex.quote(str(inst))}")

    def live_command(self):
        inst = Path(self.srv.__file__).resolve().parent
        return f"sudo bash {shlex.quote(str(inst / 'scripts' / 'recover-sudoers.sh'))}"

    @staticmethod
    def body(resp):
        import json
        return json.loads(resp.body)


class StagedGate(_GateCase):

    def test_check_and_apply_carry_the_staged_command(self):
        for layout in ("flat", "nested"):
            with self.subTest(layout=layout):
                st = self.stage(layout)
                want = self.staged_command(st)
                self.assertIn("'", want)            # the space forced quoting
                j = self.check()
                s = j["sudoers"]
                self.assertTrue(s["can_verify"] and s["needs_refresh"], s)
                self.assertEqual((s["staged_version"], s["live_version"]), (5, 4))
                self.assertEqual(s["recovery_command"], want)
                self.assertNotIn("Re-run install.sh", s["reason"])
                r = self.apply()
                self.assertEqual(r.status_code, 409)
                b = self.body(r)
                self.assertEqual(b["error"], "sudoers_refresh_required")
                self.assertEqual(b["sudoers"]["recovery_command"], want)
                self.assertTrue((st / "VERSION").exists())          # nothing applied

    def test_read_through_sudo_cat_also_gates(self):
        st = self.stage("flat")
        if IS_ROOT:
            self.skipTest("root can read a 000 file")
        self.set_live("# SUDOERS_VERSION: 4\n", mode=0o000)
        self.rec.cat_reply = "# SUDOERS_VERSION: 4\n"
        r = self.apply()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.body(r)["sudoers"]["recovery_command"], self.staged_command(st))
        self.assertIn(["sudo", "-n", "cat", str(self.dropin)], self.rec.calls)

    def test_same_rules_version_applies(self):
        self.stage("flat")
        self.set_live("# SUDOERS_VERSION: 5\n")
        j = self.check()
        self.assertFalse(j["sudoers"]["needs_refresh"])
        self.assertNotIn("recovery_command", j["sudoers"])


class ApplyNeverBlockedByAnUnreadableDropin(_GateCase):

    def assert_applied(self):
        # Backups are named by the second; two applies in one second collide.
        shutil.rmtree(self.root / ".backups", ignore_errors=True)
        # v3.4.147: an earlier test's update is still "being confirmed" (its
        # journal), and re-applying 9.9.9 over 9.9.9 needs allow_reinstall.
        (self.root / ".update-pending.json").unlink(missing_ok=True)
        r = self.apply(snapshot_db=False, allow_reinstall=True)
        self.assertIsInstance(r, dict, getattr(r, "body", r))
        self.assertTrue(r.get("ok"), r)
        self.assertEqual((self.root / "VERSION").read_text().strip(), "9.9.9")

    def test_missing_dropin_allows_apply(self):
        self.stage("flat")
        self.set_live(None)
        self.assert_applied()

    @unittest.skipIf(IS_ROOT, "root can read a 000 file")
    def test_unreadable_dropin_allows_apply(self):
        self.stage("nested")
        self.set_live("# SUDOERS_VERSION: 4\n", mode=0o000)
        self.rec.cat_reply = None                   # sudo refuses the cat
        self.assert_applied()


class LiveStatusShapes(_GateCase):
    """Every /api/sudoers/status reply carries the installed command."""

    def assert_command(self, j):
        self.assertEqual(j.get("recovery_command"), self.live_command(), j)
        self.assertNotIn("Re-run install.sh", j.get("reason", ""))

    def test_older_live_rules(self):
        j = self.status()
        self.assertTrue(j["can_verify"] and j["needs_refresh"], j)
        self.assert_command(j)

    def test_matching_rules(self):
        self.set_live("# SUDOERS_VERSION: 5\n")
        j = self.status()
        self.assertTrue(j["can_verify"] and not j["needs_refresh"], j)
        self.assert_command(j)

    def test_missing_dropin(self):
        self.set_live(None)
        j = self.status()
        self.assertFalse(j["can_verify"])
        self.assert_command(j)

    @unittest.skipIf(IS_ROOT, "root can read a 000 file")
    def test_unreadable_dropin(self):
        self.set_live("# SUDOERS_VERSION: 4\n", mode=0o000)
        j = self.status()
        self.assertFalse(j["can_verify"])
        self.assert_command(j)

    def test_installed_install_sh_unreadable(self):
        inst = self.root / "install.sh"
        hidden = self.root / "install.sh.hidden"
        inst.rename(hidden)
        self.addCleanup(hidden.rename, inst)
        j = self.status()
        self.assertFalse(j["can_verify"])
        self.assert_command(j)


if __name__ == "__main__":
    unittest.main()
