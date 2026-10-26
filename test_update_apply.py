"""Staged update apply, the boot-time update guard and automatic rollback
(updater.py, main.py's guard and confirm thread, server.py's update
endpoints).

Nothing here touches the repo: every install is a throwaway directory (the
server module's __file__ is pointed at it, so _install_dir() finds it), pip
is a stub script, `sudo` is intercepted (never run), and every "next start"
is a real subprocess running the fixture
install's own main.py the way the service unit does (`python3 main.py
start`, no -I). Versions, paths and hex values are invented.

The stdlib-only classes (TestVersionGuard … TestServedAt, TestOtherInstancePid
minus its main.py cases) import neither FastAPI nor the app, so they also run
under a bare interpreter:  python3.11 -m unittest test_update_apply.TestKillPoints
"""
import ast
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import updater

REPO = Path(__file__).resolve().parent
TMP = os.environ.get("TMPDIR") or None
PRESERVE = {"config.yaml", "aircraft_history.db", "logs", "venv", ".backups", "update",
            ".tracker.pid"}
OLD, NEW = "3.4.150", "3.4.151"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# ---------------------------------------------------------------------------
# Test hygiene: no test may change the repo's own install state. The repo is
# the build host's running dev service, so a stray .update-pending.json there
# would make it roll itself back at its next start.
# ---------------------------------------------------------------------------

def _repo_state():
    out = {}
    for pat in (".update-*", "config.yaml*", ".tracker.pid"):
        for p in sorted(REPO.glob(pat)):
            out[p.name] = p.stat().st_mtime_ns
    b = REPO / ".backups"
    if b.is_dir():
        out[".backups/"] = sorted((q.name, q.stat().st_mtime_ns) for q in b.iterdir())
    return out


def repo_guard():
    """(setUpModule, tearDownModule): fail the module if its tests changed
    the repo's .update-*, .backups/ (names + mtimes), config.yaml* or
    .tracker.pid."""
    state = {}

    def setup():
        state["before"] = _repo_state()

    def teardown():
        after = _repo_state()
        if after != state.get("before"):
            raise AssertionError(f"tests changed the repo's install state: "
                                 f"before={state.get('before')} after={after}")
    return setup, teardown


setUpModule, tearDownModule = repo_guard()


def _tmpdir(test):
    d = Path(tempfile.mkdtemp(prefix="aerodrome-b4a-", dir=TMP))
    test.addCleanup(_force_rmtree, d)
    return d


def _force_rmtree(d):
    for root, dirs, _files in os.walk(d):
        for n in dirs:
            try:
                os.chmod(os.path.join(root, n), 0o755)
            except OSError:
                pass
    shutil.rmtree(d, ignore_errors=True)


def _write(p, text):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _guard_block():
    """The real update-guard block from the repo's main.py (between its markers)."""
    src = (REPO / "main.py").read_text()
    a = src.index("# --- update guard (boot-critical; stdlib only) ---")
    b = src.index("# --- end update guard ---")
    return src[a:b]


# ---------------------------------------------------------------------------
# A minimal "release": VERSION, main.py (main/start), server.py (get_app), a
# templates/ dir and requirements.txt. Imports only stdlib.
# ---------------------------------------------------------------------------

def _fake_release(root, version, *, extra=None, main_body=None, reqs="# Version: x\nexample-pkg>=1\n",
                  guarded=True):
    """guarded: ships updater.py and a main.py that runs its guard, as every
    release from 3.4.147 on does (False: a release from before it)."""
    root = Path(root)
    _write(root / "VERSION", version + "\n")
    guard = textwrap.dedent("""\
        from pathlib import Path
        try:
            import updater as _updater
            _updater.on_boot(Path(__file__).resolve().parent)
        except Exception:
            _updater = None
        """) if guarded else ""
    _write(root / "main.py", main_body or guard + textwrap.dedent(f"""\
        import server
        TAG = {version!r}
        def start(): pass
        def main(): pass
        """))
    if guarded:
        shutil.copy2(REPO / "updater.py", root / "updater.py")
    _write(root / "server.py", f"TAG = {version!r}\ndef get_app(*a): return None\n")
    _write(root / "templates" / "index.html", f"<p>{version}</p>\n")
    _write(root / "requirements.txt", reqs)
    for rel, text in (extra or {}).items():
        _write(root / rel, text)
    return root


class _InstallCase(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmpdir(self)
        self.inst = _fake_release(self.tmp / "install", OLD,
                                  extra={"only_in_old.py": "OLD = 1\n"})
        _write(self.inst / "config.yaml", "web: {port: 18299}\n")
        _write(self.inst / "aircraft_history.db", "db-bytes")
        _write(self.inst / "user-notes.txt", "not part of any release\n")
        self.staged = _fake_release(self.inst / "update" / "aerodrome", NEW,
                                    extra={"only_in_new.py": "NEW = 1\n"})

    def snapshot(self):
        out = {}
        for p in sorted(self.inst.rglob("*")):
            rel = p.relative_to(self.inst)
            if rel.parts[0] == "update" or p.is_dir():
                continue
            out[str(rel)] = p.read_bytes()
        return out

    def live_version(self):
        return (self.inst / "VERSION").read_text().strip()

    def stage_and_swap(self, ts="20261021-120000"):
        staging = updater.stage(self.inst, self.staged, PRESERVE, ts)
        return updater.swap_in(self.inst, staging, ts, OLD, NEW)


# ===========================================================================
# Apply side (stdlib)
# ===========================================================================

class TestVersionGuard(unittest.TestCase):

    def test_downgrade_refused_unless_explicit(self):
        with self.assertRaises(updater.ApplyError) as cm:
            updater.check_versions("3.4.150", "3.4.100")
        self.assertEqual((cm.exception.code, cm.exception.http_status), ("downgrade_refused", 409))
        updater.check_versions("3.4.150", "3.4.100", allow_downgrade=True)

    def test_same_version_refused_unless_reinstall(self):
        with self.assertRaises(updater.ApplyError) as cm:
            updater.check_versions("3.4.150", "3.4.150")
        self.assertEqual(cm.exception.code, "same_version")
        updater.check_versions("3.4.150", "3.4.150", allow_reinstall=True)

    def test_numeric_not_lexical(self):
        updater.check_versions("3.4.99", "3.4.100")

    def test_unparseable_staged_refused_unknown_current_allowed(self):
        with self.assertRaises(updater.ApplyError):
            updater.check_versions("3.4.150", "3.4.151-rc1")
        updater.check_versions("unknown", "3.4.151")

    def test_parse_version(self):
        self.assertEqual(updater.parse_version("v3.4.147"), (3, 4, 147))
        self.assertEqual(updater.parse_version(" 3.4.147\n"), (3, 4, 147))
        for bad in ("3.4", "3.4.x", "3.4.1-rc1", "3.4.1+b", "", None, "v"):
            self.assertIsNone(updater.parse_version(bad), bad)


class TestStage(_InstallCase):

    def test_stage_skips_preserved_and_leaves_live_untouched(self):
        _write(self.staged / "config.yaml", "SHOULD NOT SHIP\n")
        before = self.snapshot()
        staging = updater.stage(self.inst, self.staged, PRESERVE, "20261021-120000")
        self.assertFalse((staging / "config.yaml").exists())
        self.assertFalse((staging / "update").exists())
        self.assertTrue((staging / "only_in_new.py").exists())
        live = {k: v for k, v in self.snapshot().items() if not k.startswith(".update-staging-")}
        self.assertEqual(live, before)

    def test_enospc_while_staging_changes_nothing(self):
        before = self.snapshot()
        real = shutil.copyfile
        calls = {"n": 0}

        def full_disk(src, dst, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                Path(dst).write_bytes(b"half")
                raise OSError(errno.ENOSPC, "No space left on device", dst)
            return real(src, dst, *a, **kw)

        with mock.patch("shutil.copyfile", full_disk):
            with self.assertRaises(updater.ApplyError) as cm:
                updater.stage(self.inst, self.staged, PRESERVE, "20261021-120000")
        self.assertEqual((cm.exception.code, cm.exception.http_status), ("no_space", 507))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.inst.glob(".update-staging-*")), [])
        self.assertTrue((self.staged / "VERSION").exists(), "staged release kept for retry")

    def test_preflight_space(self):
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10)):
            with self.assertRaises(updater.ApplyError) as cm:
                updater.preflight_space(self.inst, self.staged, PRESERVE)
        self.assertEqual((cm.exception.code, cm.exception.http_status), ("no_space", 507))

    def test_staging_sweep_keeps_the_journals_dir(self):
        (self.inst / ".update-staging-19990101-000000").mkdir()
        (self.inst / ".update-staging-19990101-000000" / "x").write_text("leftover")
        updater.stage(self.inst, self.staged, PRESERVE, "20261021-120000")
        self.assertEqual([p.name for p in self.inst.glob(".update-staging-*")],
                         [".update-staging-20261021-120000"])
        # A boot sweep removes staging dirs no journal names, keeps the 2
        # newest failed dirs that hold a parked release, and drops empty
        # ones (swap_in makes one ahead of time; nothing was parked there).
        for ts in ("1", "2", "3"):
            _write(self.inst / f".update-failed-2026010{ts}-000000" / "main.py", "# parked\n")
        (self.inst / ".update-failed-20260104-000000").mkdir()
        self.assertEqual(updater.on_boot(self.inst, ["main.py", "start"]), "none")
        self.assertEqual(list(self.inst.glob(".update-staging-*")), [])
        self.assertEqual(sorted(p.name for p in self.inst.glob(".update-failed-*")),
                         [".update-failed-20260102-000000", ".update-failed-20260103-000000"])

    def test_scripts_are_executable_after_staging(self):
        _write(self.staged / "install.sh", "#!/bin/bash\n")
        _write(self.staged / "scripts" / "x.sh", "#!/bin/bash\n")
        (self.staged / "install.sh").chmod(0o644)
        (self.staged / "scripts" / "x.sh").chmod(0o644)
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        self.assertTrue(os.access(staging / "install.sh", os.X_OK))
        self.assertTrue(os.access(staging / "scripts" / "x.sh", os.X_OK))


@unittest.skipIf(IS_ROOT, "root can write a 0555 directory")
class TestPreflightPermissions(_InstallCase):

    def test_0555_directory_is_refused_and_nothing_is_staged(self):
        (self.inst / "templates").chmod(0o555)
        self.addCleanup((self.inst / "templates").chmod, 0o755)
        entries = updater.release_entries(self.staged, PRESERVE)
        with self.assertRaises(updater.ApplyError) as cm:
            updater.preflight_permissions(self.inst, entries)
        e = cm.exception
        self.assertEqual((e.code, e.http_status), ("not_writable", 409))
        self.assertIn(str(self.inst / "templates"), e.message)
        self.assertIn("sudo chown -R", e.message)
        self.assertEqual(list(self.inst.glob(".update-staging-*")), [])

    def test_writable_tree_passes(self):
        updater.preflight_permissions(self.inst, updater.release_entries(self.staged, PRESERVE))

    def test_the_rename_really_needs_it(self):
        # Why the check exists: renaming a 0555 directory to another parent
        # fails with EACCES even though both parents are writable.
        d = self.inst / "templates"
        d.chmod(0o555)
        self.addCleanup(d.chmod, 0o755)
        (self.inst / "elsewhere").mkdir()
        with self.assertRaises(PermissionError):
            os.rename(d, self.inst / "elsewhere" / "templates")


class TestDepsAndVerify(_InstallCase):

    def _pip(self, rc, stderr=""):
        pip = self.tmp / "pip"
        pip.write_text(f"#!/bin/sh\necho \"pip $*\" >> {self.tmp}/pip.log\necho '{stderr}' >&2\nexit {rc}\n")
        pip.chmod(0o755)
        return pip

    def test_pip_failure_is_reported_not_raised(self):
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        ok, detail = updater.install_deps(staging, self._pip(1, "No matching distribution found for example-dep"))
        self.assertFalse(ok)
        self.assertIn("example-dep", detail)

    def test_pip_timeout_is_reported(self):
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pip", 600)):
            ok, detail = updater.install_deps(staging, self._pip(0))
        self.assertFalse(ok)
        self.assertIn("600", detail)

    def test_deps_changed_compares_requirement_lines_only(self):
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        # Only the `# Version:` header and blank lines differ → unchanged.
        (staging / "requirements.txt").write_text("# Version: y\n\nexample-pkg>=1\n# note\n")
        self.assertFalse(updater.deps_changed(staging, self.inst))
        (staging / "requirements.txt").write_text("# Version: y\nexample-pkg>=2\n")
        self.assertTrue(updater.deps_changed(staging, self.inst))

    def test_verify_accepts_good_release(self):
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        updater.verify(staging, sys.executable)

    def test_verify_rejects_syntax_error_in_lazily_imported_module(self):
        _write(self.staged / "search.py", "def broken(:\n    pass\n")
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        with self.assertRaises(updater.ApplyError) as cm:
            updater.verify(staging, sys.executable)
        self.assertEqual(cm.exception.code, "verify_failed")
        self.assertIn("search.py", cm.exception.detail)

    def test_verify_rejects_missing_dependency(self):
        _write(self.staged / "server.py", "import example_missing_dep_b4\ndef get_app(): pass\n")
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        with self.assertRaises(updater.ApplyError) as cm:
            updater.verify(staging, sys.executable)
        self.assertIn("example_missing_dep_b4", cm.exception.detail)

    def test_verify_rejects_truncated_main_that_still_compiles(self):
        # ENOSPC cut main.py mid-function: it compiles, and `main.py start`
        # would exit 0 so Restart=on-failure never restarted it.
        _write(self.staged / "main.py", "import server\ndef setup_logging():\n    level = getatt\n")
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        with self.assertRaises(updater.ApplyError):
            updater.verify(staging, sys.executable)

    def test_verify_ignores_scripts_and_syntax_warnings(self):
        _write(self.staged / "scripts" / "broken.py", "def broken(:\n")
        _write(self.staged / "tools" / "x" / "broken.py", "def broken(:\n")
        _write(self.staged / "warns.py", 'X = "\\d"\n')     # invalid escape → SyntaxWarning
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        updater.verify(staging, sys.executable)

    def test_verify_imports_from_the_staging_dir_under_isolated_mode(self):
        # A same-named module beside the test (cwd, PYTHONPATH) must not be
        # what gets imported: -I + the probe's explicit sys.path insert.
        decoy = self.tmp / "decoy"
        _write(decoy / "server.py", "raise SystemExit('imported the decoy')\n")
        staging = updater.stage(self.inst, self.staged, PRESERVE, "t1")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(decoy)}):
            updater.verify(staging, sys.executable)
        _write(staging / "server.py", "import sys\nassert sys.flags.isolated\ndef get_app(): pass\n")
        updater.verify(staging, sys.executable)


class TestSwapAndBoot(_InstallCase):

    def test_swap_installs_new_and_keeps_user_files(self):
        j = self.stage_and_swap()
        self.assertEqual(self.live_version(), NEW)
        self.assertTrue((self.inst / "only_in_new.py").exists())
        self.assertEqual((self.inst / "config.yaml").read_text(), "web: {port: 18299}\n")
        self.assertTrue((self.inst / "user-notes.txt").exists())
        self.assertEqual(updater.pending(self.inst)["state"], "pending_first_boot")
        for k in ("journal_version", "had_old", "applier_pid", "entries", "staging_dir",
                  "old_dir", "ts"):
            self.assertIn(k, j)
        self.assertEqual(j["applier_pid"], os.getpid())
        self.assertNotIn("only_in_new.py", j["had_old"])

    def test_files_are_hard_linked_not_copied(self):
        ino = os.stat(self.inst / "server.py").st_ino
        j = self.stage_and_swap()
        self.assertEqual(os.stat(self.inst / j["old_dir"] / "server.py").st_ino, ino)

    def test_rollback_after_max_failed_starts(self):
        before = self.snapshot()
        self.stage_and_swap()
        reexec = mock.Mock()
        argv = ["main.py", "start"]
        for _ in range(updater.MAX_FAILED_STARTS):
            self.assertEqual(updater.on_boot(self.inst, argv, reexec=reexec), "counting")
        self.assertEqual(updater.on_boot(self.inst, argv, reexec=reexec), "rolled_back")
        reexec.assert_called_once()
        after = {k: v for k, v in self.snapshot().items()
                 if not k.startswith((".update-failed-", updater.LAST_RESULT))}
        self.assertEqual(after, before)
        self.assertFalse((self.inst / "only_in_new.py").exists())
        res = updater.last_result(self.inst)
        self.assertEqual((res["result"], res["to_version"]), ("rolled_back", NEW))
        self.assertFalse(res["acknowledged"])
        self.assertIsNone(updater.pending(self.inst))

    def test_stop_and_status_do_not_count_as_starts(self):
        self.stage_and_swap()
        for cmd in ("stop", "status"):
            self.assertEqual(updater.on_boot(self.inst, ["main.py", cmd]), "skip")
        self.assertEqual(updater.pending(self.inst)["failed_starts"], 0)

    def test_commit_moves_old_tree_to_backups(self):
        self.stage_and_swap(ts="20261021-130000")
        updater.on_boot(self.inst, ["main.py", "start"], reexec=mock.Mock())
        self.assertTrue(updater.commit(self.inst))
        self.assertIsNone(updater.pending(self.inst))
        bk = self.inst / ".backups" / "20261021-130000"
        self.assertEqual((bk / "VERSION").read_text().strip(), OLD)
        self.assertFalse((bk / updater.CONFIG_SNAPSHOT).exists())
        self.assertEqual(list(self.inst.glob(".update-old-*")), [])
        self.assertEqual(updater.last_result(self.inst)["result"], "applied")

    def test_swap_failure_rolls_back_immediately(self):
        before = self.snapshot()
        real = os.replace
        n = {"i": 0}

        def flaky(a, b, *x, **kw):
            n["i"] += 1
            if n["i"] == 4:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(a, b, *x, **kw)

        staging = updater.stage(self.inst, self.staged, PRESERVE, "t2")
        with mock.patch("os.replace", flaky):
            with self.assertRaises(updater.ApplyError) as cm:
                updater.swap_in(self.inst, staging, "t2", OLD, NEW)
        self.assertEqual(cm.exception.code, "swap_failed")
        after = {k: v for k, v in self.snapshot().items() if not k.startswith(".update-")}
        self.assertEqual(after, before)
        self.assertIsNone(updater.pending(self.inst))

    def test_unknown_state_is_left_alone(self):
        (self.inst / updater.PENDING).write_text(json.dumps({"state": "from_the_future"}))
        self.assertEqual(updater.on_boot(self.inst, ["main.py", "start"]), "unknown")
        self.assertTrue((self.inst / updater.PENDING).exists())


class TestAtomicIO(unittest.TestCase):
    """atomic_io: temp + fsync + rename; a failed write changes nothing."""

    def setUp(self):
        import atomic_io
        self.aio = atomic_io
        self.dir = _tmpdir(self)

    def test_write_keeps_mode_and_leaves_no_temp(self):
        p = self.dir / "x.json"
        p.write_text("{}")
        p.chmod(0o600)
        self.aio.write_json_atomic(p, {"a": 1})
        self.assertEqual(self.aio.read_json(p), {"a": 1})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(q.name for q in self.dir.iterdir()), ["x.json"])

    def test_disk_full_keeps_the_old_file_and_no_temp(self):
        p = self.dir / "x.json"
        p.write_text('{"old": true}')
        with mock.patch("os.fsync", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError) as cm:
                self.aio.write_json_atomic(p, {"new": True})
        self.assertTrue(self.aio.is_disk_full(cm.exception))
        self.assertEqual(self.aio.read_json(p), {"old": True})
        self.assertEqual(sorted(q.name for q in self.dir.iterdir()), ["x.json"])

    def test_read_json_tolerates_missing_and_garbage(self):
        self.assertIsNone(self.aio.read_json(self.dir / "missing.json"))
        (self.dir / "bad.json").write_text("{not json")
        self.assertIsNone(self.aio.read_json(self.dir / "bad.json"))
        (self.dir / "list.json").write_text("[1]")
        self.assertIsNone(self.aio.read_json(self.dir / "list.json"))
        self.assertFalse(self.aio.is_disk_full(OSError(errno.EACCES, "x")))
        self.assertFalse(self.aio.is_disk_full(ValueError()))


# ===========================================================================
# Kill points: every "next start" is the fixture install's own main.py, run
# as a subprocess exactly as the service runs it.
# ===========================================================================

class _Kill(BaseException):
    """Stands in for SIGKILL: no `except Exception` sees it."""


_OPS = ("rename", "replace", "link", "unlink", "rmdir")


class _Faults:
    """Count the mutating os calls; raise _Kill (or `exc`) at call #k."""

    def __init__(self, k, exc=None):
        self.k, self.n, self.exc, self.real = k, 0, exc, {}

    def __enter__(self):
        for name in _OPS:
            real = getattr(os, name)
            self.real[name] = real

            def wrap(*a, _real=real, **kw):
                self.n += 1
                if self.n == self.k:
                    raise (self.exc or _Kill())
                return _real(*a, **kw)
            setattr(os, name, wrap)
        return self

    def __exit__(self, *a):
        for name, real in self.real.items():
            setattr(os, name, real)


def _fixture_main(version, migrates):
    body = "import sys\nfrom pathlib import Path\n" + _guard_block() + f"# main {version}\n"
    if migrates:   # stand-in for migrate_config rewriting config.yaml on start
        body += ("p = Path(__file__).resolve().parent / 'config.yaml'\n"
                 f"t = p.read_text()\nif 'migrated-by-{version}' not in t:\n"
                 f"    p.write_text(t + '# migrated-by-{version}\\n')\n")
    body += "print('BOOTED', (Path(__file__).resolve().parent / 'VERSION').read_text().strip())\n"
    return body


def _fixture(root, version, release=False):
    d = Path(root) / ("update/aerodrome" if release else "inst")
    (d / "templates").mkdir(parents=True)
    (d / "static" / "img").mkdir(parents=True)
    (d / "VERSION").write_text(version + "\n")
    (d / "main.py").write_text(_fixture_main(version, migrates=release))
    shutil.copy2(REPO / "updater.py", d / "updater.py")
    with open(d / "updater.py", "a") as f:
        f.write(f"# release {version}\n")
    (d / "server.py").write_text(f"# server {version}\n")
    (d / "templates" / "index.html").write_text(f"<p>{version}</p>")
    (d / "static" / "img" / "a.svg").write_text(f"<svg>{version}</svg>")
    if release:
        (d / "atomic_io.py").write_text(f"# atomic_io {version} (added by the release)\n")
    else:
        (d / "config.yaml").write_text("web: {host: 127.0.0.1}\n")
        (d / ".backups").mkdir()
        (d / "user-notes.txt").write_text("not part of any release\n")
    return d


def _census(inst):
    out = {}
    for p in sorted(Path(inst).rglob("*")):
        rel = p.relative_to(inst).as_posix()
        if rel.startswith((".update-", ".backups", "update/")) or "__pycache__" in rel:
            continue
        if p.is_file():
            out[rel] = p.read_text()
    return out


def _leftovers(inst):
    """What an update must never leave behind once its journal is gone: an
    old or staging dir (the old dir holds a copy of config.yaml), an empty
    failed dir, or a half-written journal or result file."""
    inst = Path(inst)
    out = [p.name for p in inst.glob(updater.OLD_PREFIX + "*")]
    out += [p.name for p in inst.glob(updater.STAGING_PREFIX + "*")]
    out += [p.name for p in inst.glob(updater.FAILED_PREFIX + "*")
            if p.is_dir() and not any(p.iterdir())]
    out += [p.name for p in inst.glob("*.json.tmp")]
    return sorted(out)


def _boot(inst, n=4):
    """Run main.py start like systemd (Restart=on-failure) until it boots."""
    outs = []
    for _ in range(n):
        r = subprocess.run([sys.executable, "-B", str(Path(inst) / "main.py"), "start"],
                           cwd=str(inst), capture_output=True, text=True, timeout=60,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                "PYTHONDONTWRITEBYTECODE": "1"})
        outs.append((r.returncode, r.stdout.strip()[-80:], r.stderr.strip()[-300:]))
        if r.returncode == 0 and "BOOTED" in r.stdout:
            return r.stdout.split("BOOTED", 1)[1].strip(), outs
    return None, outs


class _KillCase(unittest.TestCase):

    def fresh(self):
        root = Path(tempfile.mkdtemp(prefix="aerodrome-kill-", dir=self.tmp))
        inst = _fixture(root, OLD)
        rel = _fixture(root, NEW, release=True)
        return inst, rel

    def setUp(self):
        self.tmp = _tmpdir(self)

    @staticmethod
    def staging_of(inst, rel, ts="20260101-000000"):
        staging = inst / f"{updater.STAGING_PREFIX}{ts}"
        shutil.copytree(rel, staging, symlinks=True)
        return staging, ts

    def assert_one_version(self, label, inst, want, outs_and_got):
        got, outs = outs_and_got
        c = _census(inst)
        diff = sorted(set(c.items()) ^ set(want.items()))[:4]
        self.assertTrue(got is not None and c == want and not (inst / updater.PENDING).exists(),
                        f"{label}: booted={got} journal={(inst / updater.PENDING).exists()} "
                        f"diff={diff} last={outs[-1:]}")
        self.assertEqual(_leftovers(inst), [], f"{label}: left behind")

    def swapped_failing(self):
        inst, rel = self.fresh()
        want_old = _census(inst)
        staging, ts = self.staging_of(inst, rel)
        updater.swap_in(inst, staging, ts, OLD, NEW)
        j = updater.pending(inst)
        j["failed_starts"] = updater.MAX_FAILED_STARTS
        updater._write_json(inst / updater.PENDING, j)
        return inst, want_old


class TestKillPoints(_KillCase):
    """A kill at every filesystem operation of swap_in, of the boot-time
    rollback, of both in turn, and of commit — and the next starts always
    end in exactly one complete version."""

    def test_a_kill_at_every_point_of_swap_in(self):
        points = 0
        for k in range(1, 80):
            inst, rel = self.fresh()
            want = _census(inst)
            staging, ts = self.staging_of(inst, rel)
            with _Faults(k):
                try:
                    updater.swap_in(inst, staging, ts, OLD, NEW)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            with self.subTest(kill_at=k):
                self.assert_one_version(f"swap_in k={k}", inst, want, _boot(inst))
        self.assertGreaterEqual(points, 10)

    def test_b_kill_at_every_point_of_the_boot_rollback(self):
        points = 0
        for k in range(1, 100):
            inst, want = self.swapped_failing()
            with _Faults(k):
                try:
                    updater._on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                     log=lambda m: None, now=time.time)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            with self.subTest(kill_at=k):
                self.assert_one_version(f"rollback k={k}", inst, want, _boot(inst))
        self.assertGreaterEqual(points, 10)

    def test_c_kill_in_swap_in_then_in_the_rollback(self):
        n = 0
        for k in (2, 5, 9, 14, 20):
            for m in (1, 3, 6, 10, 15):
                inst, rel = self.fresh()
                want = _census(inst)
                staging, ts = self.staging_of(inst, rel)
                with _Faults(k):
                    try:
                        updater.swap_in(inst, staging, ts, OLD, NEW)
                        continue
                    except _Kill:
                        pass
                with _Faults(m):
                    try:
                        updater._on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                         log=lambda msg: None, now=time.time)
                    except _Kill:
                        pass
                n += 1
                with self.subTest(k=k, m=m):
                    self.assert_one_version(f"double kill {k}/{m}", inst, want, _boot(inst))
        self.assertGreaterEqual(n, 15)

    def test_d_kill_at_every_point_of_commit(self):
        points = 0
        for k in range(1, 80):
            inst, rel = self.fresh()
            staging, ts = self.staging_of(inst, rel)
            updater.swap_in(inst, staging, ts, OLD, NEW)
            got0, _ = _boot(inst, 1)                 # the new version starts once (counts 1)
            self.assertEqual(got0, NEW)
            want_new = _census(inst)
            updater.note_served(inst)
            j = updater.pending(inst)
            j["served_at"] = time.time() - updater.COMMIT_AFTER_SEC - 5
            updater._write_json(inst / updater.PENDING, j)
            with _Faults(k):
                try:
                    updater.commit(inst)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            with self.subTest(kill_at=k):
                got, outs = _boot(inst)
                self.assertEqual(got, NEW, outs)
                self.assertEqual(_census(inst), want_new)
                self.assertFalse((inst / updater.PENDING).exists())
                self.assertEqual((inst / ".backups" / ts / "VERSION").read_text().strip(), OLD)
        self.assertGreaterEqual(points, 5)


class TestBootENOSPC(_KillCase):
    """Disk full on each journal write the boot path makes."""

    def full(self):
        real_open = open

        def full(path, mode="r", *a, **kw):
            if "w" in mode and str(path).endswith(".json.tmp"):
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
            return real_open(path, mode, *a, **kw)
        return mock.patch("builtins.open", full)

    def test_counter_write_fails_rolls_back_at_once(self):
        inst, rel = self.fresh()
        want = _census(inst)
        staging, ts = self.staging_of(inst, rel)
        updater.swap_in(inst, staging, ts, OLD, NEW)
        with self.full():
            r = updater._on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                 log=lambda m: None, now=time.time)
        self.assertEqual(r, "rolled_back")
        self.assert_one_version("counter ENOSPC", inst, want, _boot(inst))

    def test_every_write_fails_during_a_rollback(self):
        inst, want = self.swapped_failing()
        with self.full():
            r = updater._on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                 log=lambda m: None, now=time.time)
        self.assertEqual(r, "rolled_back")
        self.assertIsNone(updater.last_result(inst))         # result was best effort
        self.assert_one_version("rollback ENOSPC", inst, want, _boot(inst))

    def test_committing_write_fails_commit_raises_and_moves_nothing(self):
        inst, rel = self.fresh()
        staging, ts = self.staging_of(inst, rel)
        updater.swap_in(inst, staging, ts, OLD, NEW)
        updater.note_served(inst)
        with self.full():
            with self.assertRaises(OSError):
                updater.commit(inst)
        j = updater.pending(inst)
        self.assertEqual(j["state"], "pending_first_boot")
        self.assertTrue((inst / j["old_dir"] / "VERSION").exists())

    def test_malformed_journal_never_stops_startup(self):
        inst, rel = self.fresh()
        # Paths that aren't what swap_in writes: left alone, startup goes on.
        (inst / updater.PENDING).write_text('{"state": "rolling_back", "entries": ["x"], "ts": "t"}')
        self.assertEqual(updater.on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                         log=lambda m: None), "unknown")
        got, outs = _boot(inst)
        self.assertEqual(got, OLD, outs)
        self.assertIn("unexpected staging_dir/old_dir; leaving it alone", outs[-1][2])
        self.assertTrue((inst / updater.PENDING).exists())
        # Well-formed paths but a broken field: the guard's own error is
        # caught, and startup still goes on.
        (inst / updater.PENDING).write_text(json.dumps({
            "state": "pending_first_boot", "entries": ["VERSION"], "had_old": ["VERSION"],
            "ts": "t", "staging_dir": ".update-staging-t", "old_dir": ".update-old-t",
            "to_version": OLD, "failed_starts": "many"}))
        (inst / ".update-old-t").mkdir()
        (inst / ".update-old-t" / "VERSION").write_text(OLD + "\n")
        got, outs = _boot(inst)
        self.assertEqual(got, OLD, outs)
        self.assertIn("update guard error, continuing startup", outs[-1][2])


class TestConfigRestoredOnRollback(_KillCase):

    def test_config_is_byte_identical_after_two_failed_starts(self):
        inst, rel = self.fresh()
        before = (inst / "config.yaml").read_bytes()
        want = _census(inst)
        staging, ts = self.staging_of(inst, rel)
        updater.swap_in(inst, staging, ts, OLD, NEW)
        # Two counted starts of the new version (its main.py rewrites
        # config.yaml, as migrate_config would), then the third rolls back.
        for _ in range(updater.MAX_FAILED_STARTS):
            got, _ = _boot(inst, 1)
            self.assertEqual(got, NEW)
        self.assertNotEqual((inst / "config.yaml").read_bytes(), before)
        got, outs = _boot(inst, 1)
        self.assertEqual(got, OLD, outs)
        self.assertEqual((inst / "config.yaml").read_bytes(), before)
        self.assertEqual(_census(inst), want)
        self.assertEqual(updater.last_result(inst)["result"], "rolled_back")


class TestServedAt(_InstallCase):
    """served_at: the first good answer. Old enough → committed at the next
    start; recent → the start is counted like any other, so a release that
    answers and then crashes inside the confirm window still rolls back."""

    def boot(self):
        return updater.on_boot(self.inst, ["main.py", "start"], reexec=mock.Mock(),
                               log=lambda m: None)

    def set(self, **kw):
        j = updater.pending(self.inst)
        j.update(kw)
        updater._write_json(self.inst / updater.PENDING, j)

    def test_served_11_minutes_ago_commits_not_counts(self):
        self.stage_and_swap()
        self.set(served_at=time.time() - 11 * 60, failed_starts=1)
        self.assertEqual(self.boot(), "committed")
        self.assertIsNone(updater.pending(self.inst))
        self.assertEqual(self.live_version(), NEW)

    def test_served_1_minute_ago_is_counted(self):
        self.stage_and_swap()
        self.set(served_at=time.time() - 60, failed_starts=0)
        self.assertEqual(self.boot(), "counting")
        self.assertEqual(updater.pending(self.inst)["failed_starts"], 1)

    def test_served_then_crashed_inside_the_window_rolls_back(self):
        self.stage_and_swap()
        self.set(served_at=time.time() - 10, failed_starts=updater.MAX_FAILED_STARTS)
        self.assertEqual(self.boot(), "rolled_back")
        self.assertEqual(self.live_version(), OLD)

    def test_never_served_after_two_failures_rolls_back(self):
        self.stage_and_swap()
        self.set(failed_starts=updater.MAX_FAILED_STARTS)
        self.assertEqual(self.boot(), "rolled_back")
        self.assertEqual(self.live_version(), OLD)


# ===========================================================================
# Instance identity
# ===========================================================================

class TestOtherInstancePid(unittest.TestCase):

    def setUp(self):
        self.inst = _tmpdir(self)
        (self.inst / "main.py").write_text("# fixture\n")

    def pid_file(self, pid):
        (self.inst / ".tracker.pid").write_text(str(pid))

    def spawn(self, argv, cwd=None):
        p = subprocess.Popen(argv, cwd=cwd)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def test_pid_1_is_stale(self):
        self.pid_file(1)
        self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_dead_pid_is_stale(self):
        p = subprocess.Popen(["true"])
        p.wait()
        self.pid_file(p.pid)
        self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_live_unrelated_process_is_stale(self):
        p = self.spawn(["sleep", "30"])
        self.pid_file(p.pid)
        self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_live_instance_absolute_argv(self):
        p = self.spawn([sys.executable, "-c", "import time; time.sleep(30)",
                        str(self.inst / "main.py"), "start"])
        self.pid_file(p.pid)
        self._wait_cmdline(p.pid)
        self.assertEqual(updater.other_instance_pid(self.inst), p.pid)

    def test_live_instance_unit_form_relative_argv_and_cwd(self):
        p = self.spawn([sys.executable, "-c", "import time; time.sleep(30)", "main.py", "start"],
                       cwd=str(self.inst))
        self.pid_file(p.pid)
        self._wait_cmdline(p.pid)
        self.assertEqual(updater.other_instance_pid(self.inst), p.pid)

    _OWN_PID = ("import os, sys; sys.path.insert(0, sys.argv[3]); import updater; "
                "inst = sys.argv[4] if len(sys.argv) > 4 else os.path.dirname(sys.argv[1]); "
                "open(os.path.join(inst, '.tracker.pid'), 'w').write(str(os.getpid())); "
                "print(updater.other_instance_pid(inst))")

    def test_own_pid_is_not_another_instance(self):
        # After a power cut the service often gets the PID the stale file
        # names. The child's own cmdline names THIS install's main.py, so
        # only the own-PID check keeps it from reading itself as "already
        # running" (and crash-looping).
        r = subprocess.run([sys.executable, "-c", self._OWN_PID, str(self.inst / "main.py"),
                            "start", str(REPO)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "None", r.stderr)
        # the unit form: relative main.py, cwd = the install
        r = subprocess.run([sys.executable, "-c", self._OWN_PID, "main.py", "start", str(REPO),
                            str(self.inst)], cwd=str(self.inst),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "None", r.stderr)

    def test_zombie_is_stale(self):
        p = subprocess.Popen(["true"])
        self.addCleanup(p.wait)
        for _ in range(200):                      # exited, not reaped yet
            try:
                if Path(f"/proc/{p.pid}/stat").read_text().split(") ")[1][0] == "Z":
                    break
            except (OSError, IndexError):
                break
            time.sleep(0.01)
        self.pid_file(p.pid)
        self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_pid_gone_between_the_checks_is_stale(self):
        p = subprocess.Popen(["true"])
        p.wait()
        self.pid_file(p.pid)
        with mock.patch.object(updater.os, "kill", lambda pid, sig: None):
            self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_non_dumpable_process_is_stale(self):
        # e.g. ssh-agent: its cwd can't be read; a real Aerodrome is never
        # non-dumpable, so it is not ours (and stop() must not signal it).
        code = ("import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); "
                "print('ready', flush=True); time.sleep(30)")
        p = subprocess.Popen([sys.executable, "-c", code, str(self.inst / "main.py"), "start"],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertEqual(p.stdout.readline().strip(), "ready")
        self.pid_file(p.pid)
        self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_without_proc_a_live_pid_counts(self):
        p = self.spawn(["sleep", "30"])
        self.pid_file(p.pid)
        real = os.path.isdir
        with mock.patch.object(updater.os.path, "isdir",
                               lambda q: False if str(q) == "/proc/self" else real(q)):
            self.assertEqual(updater.other_instance_pid(self.inst), p.pid)

    def test_another_accounts_instance_counts(self):
        # status/stop run by a different user than the service: kill(0)
        # gives EPERM. The PID file was written by that other account and
        # the process runs a main.py, so it is that account's instance.
        p = self.spawn([sys.executable, "-c", "import time; time.sleep(30)",
                        str(self.inst / "main.py"), "start"])
        self.pid_file(p.pid)
        self._wait_cmdline(p.pid)

        def eperm(pid, sig):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(updater.os, "kill", eperm), \
                mock.patch.object(updater.os, "geteuid", lambda _u=os.geteuid(): _u + 1):
            self.assertEqual(updater.other_instance_pid(self.inst), p.pid)
        # a PID file THIS account wrote naming a process it can't signal is
        # stale (the reused-PID-1 case)
        with mock.patch.object(updater.os, "kill", eperm):
            self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_garbage_file_is_stale(self):
        (self.inst / ".tracker.pid").write_text("not a pid\n")
        self.assertIsNone(updater.other_instance_pid(self.inst))

    def test_on_boot_leaves_everything_alone_while_another_instance_runs(self):
        # `main.py start` by hand next to the running service must neither
        # count a start nor sweep or roll anything back.
        journal = {"journal_version": 1, "state": "pending_first_boot", "failed_starts": 2,
                   "ts": "t", "entries": [], "had_old": [], "staging_dir": ".update-staging-t",
                   "old_dir": ".update-old-t"}
        (self.inst / updater.PENDING).write_text(json.dumps(journal))
        (self.inst / ".update-staging-x").mkdir()
        p = self.spawn([sys.executable, "-c", "import time; time.sleep(30)",
                        str(self.inst / "main.py"), "start"])
        self.pid_file(p.pid)
        self._wait_cmdline(p.pid)
        reexec = mock.Mock()
        self.assertEqual(updater.on_boot(self.inst, ["main.py", "start"], reexec=reexec),
                         "other_instance")
        reexec.assert_not_called()
        self.assertEqual(json.loads((self.inst / updater.PENDING).read_text()), journal)
        self.assertTrue((self.inst / ".update-staging-x").exists())

    @staticmethod
    def _wait_cmdline(pid):
        # Popen returns after fork; give exec a moment to replace the cmdline.
        for _ in range(100):
            try:
                if b"main.py" in Path(f"/proc/{pid}/cmdline").read_bytes():
                    return
            except OSError:
                return
            time.sleep(0.01)


class _MainCase(unittest.TestCase):
    """main.py's own start/stop/restart (imports the app: venv only)."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def setUp(self):
        self.inst = _tmpdir(self)
        (self.inst / "main.py").write_text("# fixture\n")
        p = mock.patch.object(self.main, "PID_FILE", self.inst / ".tracker.pid")
        p.start()
        self.addCleanup(p.stop)


class _StopStart(Exception):
    pass


class TestMainPidHandling(_MainCase):

    def test_start_with_a_reused_pid_1_proceeds_and_rewrites_the_file(self):
        # Before: os.kill(1, 0) raised PermissionError → "main.py start"
        # crashed with rc 1 on every restart until the file was deleted.
        (self.inst / ".tracker.pid").write_text("1")
        with mock.patch.object(self.main, "set_db_tuning_profile", side_effect=_StopStart), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(_StopStart):
                self.main.start({"data": {"db_file": str(self.inst / "x.db")}})
        self.assertEqual((self.inst / ".tracker.pid").read_text(), str(os.getpid()))

    def test_start_refuses_a_live_instance(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              str(self.inst / "main.py"), "start"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        TestOtherInstancePid._wait_cmdline(p.pid)
        (self.inst / ".tracker.pid").write_text(str(p.pid))
        out = io.StringIO()
        with mock.patch.object(self.main, "set_db_tuning_profile", side_effect=_StopStart), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                self.main.start({"data": {"db_file": str(self.inst / "x.db")}})
        self.assertIn(f"Already running (PID {p.pid})", out.getvalue())
        self.assertEqual(cm.exception.code, 1)

    def test_stop_never_signals_an_unrelated_process(self):
        p = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        (self.inst / ".tracker.pid").write_text(str(p.pid))
        with contextlib.redirect_stdout(io.StringIO()):
            self.main.stop({})
        time.sleep(0.2)
        self.assertIsNone(p.poll(), "stop() signalled a process that isn't Aerodrome")
        self.assertFalse((self.inst / ".tracker.pid").exists())

    def test_status_does_not_count_an_unrelated_process(self):
        p = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        (self.inst / ".tracker.pid").write_text(str(p.pid))
        self.assertIsNone(self.main._running_instance_pid())


class TestRestartReexecs(_MainCase):

    def test_restart_stops_then_execs_a_fresh_start(self):
        calls = []
        with mock.patch.object(sys, "argv", ["main.py", "restart"]), \
                mock.patch.object(self.main, "load_config", return_value={}), \
                mock.patch.object(self.main, "setup_logging"), \
                mock.patch.object(self.main, "stop", side_effect=lambda c: calls.append("stop")), \
                mock.patch.object(self.main, "start", side_effect=lambda c: calls.append("start")), \
                mock.patch.object(self.main.os, "execv",
                                  side_effect=lambda exe, argv: calls.append(("execv", exe, argv))):
            self.main.main()
        self.assertEqual(calls[0], "stop")
        self.assertEqual(calls[1], ("execv", sys.executable,
                                    [sys.executable, str(self.main.BASE_DIR / "main.py"), "start"]))
        self.assertNotIn("start", calls)


class _VersionServer:
    """A stub web UI on an ephemeral 127.0.0.1 port."""

    def __init__(self, status=200, version=NEW):
        import http.server
        outer = self
        self.status, self.version, self.hits = status, version, 0

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                body = json.dumps({"version": outer.version}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ConfirmCase(_MainCase):

    def setUp(self):
        super().setUp()
        self.install = _fake_release(self.inst / "i", OLD)
        staged = _fake_release(self.inst / "staged", NEW)
        staging = updater.stage(self.install, staged, PRESERVE, "20261021-140000")
        updater.swap_in(self.install, staging, "20261021-140000", OLD, NEW)
        for p in (mock.patch.object(self.main, "BASE_DIR", self.install),
                  mock.patch.object(updater, "SERVE_DEADLINE_SEC", 0.6),
                  mock.patch.object(updater, "HEALTHY_AFTER_SEC", 0.05)):
            p.start()
            self.addCleanup(p.stop)
        self.exit = mock.Mock()

    def run_confirm(self, host, port, **kw):
        kw.setdefault("sleep", lambda s: time.sleep(min(s, 0.05)))
        self.main._confirm_update_when_serving(host, port, NEW, exit_fn=self.exit, **kw)


class TestConfirmThread(_ConfirmCase):

    def test_good_answer_records_served_then_commits(self):
        srv = _VersionServer()
        self.addCleanup(srv.close)
        self.run_confirm("0.0.0.0", srv.port)
        self.exit.assert_not_called()
        self.assertIsNone(updater.pending(self.install))
        self.assertEqual(updater.last_result(self.install)["result"], "applied")

    def test_bad_http_for_the_whole_deadline_exits_1(self):
        srv = _VersionServer(status=500)
        self.addCleanup(srv.close)
        self.run_confirm("127.0.0.1", srv.port)
        self.exit.assert_called_once_with(1)
        self.assertEqual(updater.pending(self.install)["state"], "pending_first_boot")
        self.assertIsNone(updater.pending(self.install).get("served_at"))

    def test_wrong_version_counts_as_bad(self):
        srv = _VersionServer(version=OLD)
        self.addCleanup(srv.close)
        self.run_confirm("127.0.0.1", srv.port)
        self.exit.assert_called_once_with(1)

    def test_connection_refused_for_the_whole_deadline_confirms_on_uptime(self):
        self.run_confirm("127.0.0.1", _free_port())
        self.exit.assert_not_called()
        self.assertIsNone(updater.pending(self.install))
        self.assertEqual(updater.last_result(self.install)["result"], "applied")

    def test_commit_retried_until_it_works(self):
        srv = _VersionServer()
        self.addCleanup(srv.close)
        real = updater.commit
        n = {"i": 0}

        def flaky(inst):
            n["i"] += 1
            if n["i"] < 3:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(inst)
        slept = []
        with mock.patch.object(updater, "commit", flaky):
            self.run_confirm("127.0.0.1", srv.port,
                             sleep=lambda s: (slept.append(s), time.sleep(min(s, 0.01))))
        self.assertEqual(n["i"], 3)
        self.assertIn(10, slept)
        self.assertIn(60, slept)
        self.assertIsNone(updater.pending(self.install))

    def test_wildcard_v6_probes_v6_loopback_first(self):
        seen = []

        def probe(addr, hdr, port):
            seen.append((addr, hdr))
            return "ok", NEW
        self.run_confirm("::", 18210, probe=probe)
        self.assertEqual(seen[0], ("::1", "[::1]"))
        self.assertIsNone(updater.pending(self.install))


# ===========================================================================
# Endpoints (real get_app; _install_dir points at a throwaway install)
# ===========================================================================

class _SudoStub:
    """subprocess.run stand-in that intercepts `sudo …` only (never runs it);
    everything else (the test-load, the pip stub) runs for real.
    `return_value` = the restart outcome: (False, note) makes sudo exit 1
    with `note` on stderr, as a missing sudo rule does."""

    def __init__(self):
        self.calls = []
        self.return_value = (True, "")
        self._real = subprocess.run

    def __call__(self, cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "sudo":
            self.calls.append(list(cmd))
            ok, note = self.return_value
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, "",
                                               "" if ok else note + "\n")
        return self._real(cmd, *a, **kw)

    def restarts(self):
        return [c for c in self.calls if "restart" in c]

    def assert_not_called(self):
        assert not self.restarts(), self.restarts()

    def assert_called_once(self):
        assert len(self.restarts()) == 1, self.restarts()


def _endpoint(app, path, method):
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))


def _body(resp):
    if isinstance(resp, dict):
        return resp
    return json.loads(resp.body)


def _status(resp):
    return 200 if isinstance(resp, dict) else resp.status_code


class _EndpointCase(unittest.TestCase):
    """A throwaway install at OLD with NEW staged in update/aerodrome/."""
    LAYOUT = "nested"

    @classmethod
    def setUpClass(cls):
        import collector
        import server
        from schema_migrations import apply_schema_migrations
        cls.server, cls.collector = server, collector
        cls.migrate = staticmethod(apply_schema_migrations)

    def setUp(self):
        import sqlite3
        import yaml
        self.tmp = _tmpdir(self)
        self.inst = _fake_release(self.tmp / "install", OLD, extra={"only_in_old.py": "OLD = 1\n"})
        _write(self.inst / "config.yaml", "web: {port: 18299}\n")
        (self.inst / "update").mkdir()
        _write(self.inst / "update" / "UPDATE_README.md", "# Update staging folder\n")
        self.pip_log = self.tmp / "pip.log"
        self.pip_rc = self.tmp / "pip.rc"
        self.pip_rc.write_text("0")
        _write(self.inst / "venv" / "bin" / "pip",
               f"#!/bin/sh\necho \"pip $*\" >> {self.pip_log}\nexit $(cat {self.pip_rc})\n")
        (self.inst / "venv" / "bin" / "pip").chmod(0o755)
        self.stage_release(NEW)
        self.db = str(self.tmp / "history.db")
        self.collector.init_db(self.db)
        c = sqlite3.connect(self.db)
        self.migrate(c, "test")
        c.close()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        cfgp = self.tmp / "config-live.yaml"
        cfgp.write_text(yaml.safe_dump(cfg))
        (self.inst / "static").mkdir(exist_ok=True)       # get_app mounts it
        self.restart = _SudoStub()
        for p in (mock.patch.object(self.server, "__file__", str(self.inst / "server.py")),
                  mock.patch("subprocess.run", self.restart)):
            p.start()
            self.addCleanup(p.stop)
        self.app = self.server.get_app(cfg, str(cfgp))

    def stage_release(self, version, layout=None, **kw):
        upd = self.inst / "update"
        for item in upd.iterdir():
            if item.name != "UPDATE_README.md":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        root = upd if (layout or self.LAYOUT) == "flat" else upd / "aerodrome"
        kw.setdefault("extra", {"only_in_new.py": "NEW = 1\n",
                                "update/UPDATE_README.md": "# Update staging folder (new)\n"})
        _fake_release(root, version, **kw)
        return root

    def ep(self, path, method="POST"):
        return _endpoint(self.app, path, method)

    def apply(self, **kw):
        return self.ep("/api/updates/local/apply")(**{"snapshot_db": False, **kw})

    def live_snapshot(self):
        out = {}
        for p in sorted(self.inst.rglob("*")):
            rel = p.relative_to(self.inst)
            if rel.parts[0] in ("update", ".backups") or rel.parts[0].startswith(".update-") \
                    or p.is_dir():
                continue
            out[str(rel)] = p.read_bytes()
        return out

    def update_snapshot(self):
        return {str(p.relative_to(self.inst)): p.read_bytes()
                for p in sorted((self.inst / "update").rglob("*")) if p.is_file()}


class TestApplyEndpoint(_EndpointCase):

    def test_verify_failure_changes_nothing_and_never_restarts(self):
        self.stage_release(NEW, extra={"server.py": "def get_app(:\n"})
        live, upd = self.live_snapshot(), self.update_snapshot()
        r = self.apply()
        self.assertEqual(_status(r), 500)
        b = _body(r)
        self.assertFalse(b["ok"])
        self.assertEqual(b["code"], "verify_failed")
        self.assertEqual(b["error"], b["message"])
        self.assertIn("server.py", b["detail"])
        self.restart.assert_not_called()
        self.assertEqual(self.live_snapshot(), live)
        self.assertEqual(self.update_snapshot(), upd)                 # update/ kept
        self.assertIsNone(updater.pending(self.inst))
        self.assertEqual(list(self.inst.glob(".update-staging-*")), [])
        res = updater.last_result(self.inst)
        self.assertEqual((res["result"], res["code"]), ("refused", "verify_failed"))
        self.assertIsNone(self.server._MAINT.holder())

    def test_good_release_is_switched_in_and_restarts(self):
        r = self.apply()
        b = _body(r)
        self.assertTrue(b["ok"], b)
        self.assertEqual((b["from_version"], b["to_version"]), (OLD, NEW))
        self.assertIsNone(b["pip_warning"])
        self.restart.assert_called_once()
        self.assertEqual((self.inst / "VERSION").read_text().strip(), NEW)
        self.assertTrue((self.inst / "only_in_new.py").exists())
        j = updater.pending(self.inst)
        self.assertEqual(j["state"], "pending_first_boot")
        self.assertEqual(j["db_file"], str(Path(self.db)))
        self.assertIsInstance(j["schema_before"], int)
        # update/ cleared except its own docs, which came from the new release
        self.assertEqual(sorted(p.name for p in (self.inst / "update").iterdir()),
                         ["UPDATE_README.md"])
        self.assertIn("(new)", (self.inst / "update" / "UPDATE_README.md").read_text())
        # the lock is kept while the restart lands
        held = self.server._MAINT.holder()
        self.assertEqual(held["op"], "restarting after an update")

    def test_pip_fails_but_release_loads_applies_with_a_warning(self):
        self.stage_release(NEW, reqs="# Version: y\nexample-pkg>=2\n")
        self.pip_rc.write_text("1")
        b = _body(self.apply())
        self.assertTrue(b["ok"], b)
        self.assertTrue(b["pip_warning"])
        self.assertIn("install -r", self.pip_log.read_text())
        self.assertEqual(updater.pending(self.inst)["pip_warning"], b["pip_warning"])

    def test_pip_fails_and_release_doesnt_load_is_deps_failed(self):
        self.stage_release(NEW, reqs="# Version: y\nexample-pkg>=2\n",
                           extra={"server.py": "import example_missing_pkg_b4\ndef get_app(): pass\n"})
        self.pip_rc.write_text("1")
        live = self.live_snapshot()
        r = self.apply()
        b = _body(r)
        self.assertEqual((_status(r), b["code"]), (500, "deps_failed"))
        self.assertIn("pip", b["detail"])
        self.assertIn("example_missing_pkg_b4", b["detail"])
        self.restart.assert_not_called()
        self.assertEqual(self.live_snapshot(), live)

    def test_unchanged_requirements_never_run_pip(self):
        self.assertTrue(_body(self.apply())["ok"])
        self.assertFalse(self.pip_log.exists())

    def test_restart_call_fails_puts_everything_back(self):
        self.restart.return_value = (False, "sudo: a password is required")
        live, upd = self.live_snapshot(), self.update_snapshot()
        r = self.apply()
        b = _body(r)
        self.assertEqual((_status(r), b["code"]), (500, "restart_failed"))
        self.assertIn("can't restart itself", b["message"])
        self.assertIn("password is required", b["message"])
        self.assertEqual(b["detail"], "sudo: a password is required")
        self.assertEqual(self.live_snapshot(), live)                  # byte-identical
        self.assertEqual(self.update_snapshot(), upd)                 # still staged
        self.assertIsNone(updater.pending(self.inst))                 # no journal
        # no parked copy of the new release (it is still staged in update/)
        self.assertEqual(sorted(p.name for p in self.inst.glob(".update-*")),
                         [updater.LAST_RESULT])
        self.assertIsNone(self.server._MAINT.holder())                # lock free
        self.assertEqual(updater.last_result(self.inst)["code"], "restart_failed")
        # …and it works once the restart permission is fixed
        self.restart.return_value = (True, "")
        self.assertTrue(_body(self.apply())["ok"])

    def test_downgrade_refused_unless_allowed(self):
        self.stage_release("3.4.100")
        r = self.apply()
        self.assertEqual((_status(r), _body(r)["code"]), (409, "downgrade_refused"))
        self.assertTrue(_body(self.apply(allow_downgrade=True))["ok"])
        self.assertEqual((self.inst / "VERSION").read_text().strip(), "3.4.100")

    def test_same_version_refused_unless_reinstall(self):
        self.stage_release(OLD)
        r = self.apply()
        self.assertEqual((_status(r), _body(r)["code"]), (409, "same_version"))
        self.assertTrue(_body(self.apply(allow_reinstall=True))["ok"])

    def test_flat_layout_applies(self):
        self.stage_release(NEW, layout="flat")
        b = _body(self.apply())
        self.assertTrue(b["ok"], b)
        self.assertEqual((self.inst / "VERSION").read_text().strip(), NEW)
        # update/'s own docs are not part of the release
        self.assertFalse((self.inst / "UPDATE_README.md").exists())
        self.assertFalse((self.inst / ".gitkeep").exists())

    def test_held_lock_refuses(self):
        tok = self.server._MAINT.try_acquire("a server-side backup")
        self.addCleanup(self.server._MAINT.release, tok)
        r = self.apply()
        b = _body(r)
        self.assertEqual(_status(r), 409)
        self.assertIn("server-side backup is running", b["error"])
        self.assertEqual(b["busy"]["op"], "a server-side backup")
        self.assertEqual(b["error"], b["message"])

    def test_pending_journal_refuses_update_ops_only(self):
        self.assertTrue(_body(self.apply())["ok"])
        self.server._MAINT = self.server._SingleRun()      # the restart "happened"
        self.stage_release("3.4.152")
        r = self.apply()
        self.assertEqual((_status(r), _body(r)["code"]), (409, "update_pending"))
        up = self.ep("/api/updates/local/upload")
        r = up(file=_upload(_zip_release({"VERSION": "3.4.153\n", "main.py": "#\n"})))
        self.assertEqual((_status(r), _body(r)["code"]), (409, "update_pending"))
        gh = self.ep("/api/updates/github/apply")
        with mock.patch("urllib.request.urlopen") as uo:
            r = gh(snapshot_db=False)
        uo.assert_not_called()
        self.assertEqual((_status(r), _body(r)["code"]), (409, "update_pending"))
        # a server-side backup is not an update operation
        r = self.ep("/api/backup/create-server-side")()
        self.assertEqual(_status(r), 200, _body(r))

    def test_restart_endpoint_refused_only_while_the_lock_is_held(self):
        restart = self.ep("/api/restart")
        tok = self.server._MAINT.try_acquire("a config import")
        r = restart()
        self.assertEqual(_status(r), 409)
        self.server._MAINT.release(tok)
        (self.inst / updater.PENDING).write_text(json.dumps({"state": "pending_first_boot"}))
        self.assertTrue(_body(restart())["ok"])          # a journal alone never blocks it

    def test_maintenance_status_and_ack(self):
        status = self.ep("/api/maintenance/status", "GET")
        self.assertEqual(status(), {"busy": None, "update": None, "update_pending": None})
        self.assertTrue(_body(self.apply())["ok"])
        s = status()
        self.assertEqual(s["busy"]["op"], "restarting after an update")
        self.assertTrue(s["busy"]["parked"])
        p = s["update_pending"]
        self.assertEqual((p["from_version"], p["to_version"], p["failed_starts"]), (OLD, NEW, 0))
        self.assertIsNone(p["served_at"])
        self.assertTrue(p["waiting_for_restart"])        # same process that applied
        updater.commit(self.inst)
        s = status()
        self.assertIsNone(s["update_pending"])
        self.assertEqual((s["update"]["result"], s["update"]["acknowledged"]), ("applied", False))
        ack = self.ep("/api/maintenance/ack")
        self.assertTrue(ack(_ack_body(self.app, "update"))["ok"])
        self.assertTrue(status()["update"]["acknowledged"])
        self.assertEqual(_status(ack(_ack_body(self.app, "other"))), 400)

    def test_opt_in_db_snapshot_lands_with_the_backup(self):
        b = _body(self.apply(snapshot_db=True))
        self.assertTrue(b["ok"], b)
        ts = updater.pending(self.inst)["ts"]
        updater.commit(self.inst)
        bk = self.inst / ".backups" / ts
        self.assertTrue((bk / "history.db.snapshot").exists())
        self.assertEqual((bk / "VERSION").read_text().strip(), OLD)


def _ack_body(app, kind):
    import inspect
    ep = _endpoint(app, "/api/maintenance/ack", "POST")
    model = inspect.signature(ep).parameters["body"].annotation
    return model(kind=kind)


class TestCheckPreflight(_EndpointCase):

    @unittest.skipIf(IS_ROOT, "root can write a 0555 directory")
    def test_check_reports_an_unwritable_directory(self):
        (self.inst / "templates").chmod(0o555)
        self.addCleanup((self.inst / "templates").chmod, 0o755)
        j = self.ep("/api/updates/local/check", "GET")()
        pf = j["preflight"]
        self.assertFalse(pf["ok"])
        self.assertEqual(pf["problems"], [str(self.inst / "templates")])
        self.assertIn("sudo chown -R", pf["fix_command"])
        self.assertIn("aren't writable" if len(pf["problems"]) > 1 else "isn't writable",
                      pf["message"])
        # …and apply refuses before staging anything
        live = self.live_snapshot()
        r = self.apply()
        self.assertEqual((_status(r), _body(r)["code"]), (409, "not_writable"))
        self.assertEqual(list(self.inst.glob(".update-staging-*")), [])
        self.assertEqual(self.live_snapshot(), live)

    def test_check_preflight_ok(self):
        j = self.ep("/api/updates/local/check", "GET")()
        self.assertEqual(j["preflight"]["ok"], True)
        self.assertEqual(j["status"], "newer")


def _zip_release(files, wrapper=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(f"{wrapper}/{name}" if wrapper else name, text)
    return buf.getvalue()


def _upload(data, name="aerodrome-vX.zip"):
    from starlette.datastructures import UploadFile
    return UploadFile(file=io.BytesIO(data), filename=name)


def _release_files(version):
    tmp = Path(tempfile.mkdtemp(dir=TMP))
    try:
        _fake_release(tmp, version, extra={"only_in_new.py": "NEW = 1\n"})
        return {str(p.relative_to(tmp)): p.read_text() for p in tmp.rglob("*") if p.is_file()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestGithubApply(_EndpointCase):
    """The path that applies the NEXT release: download → checksum → stage →
    the same staged apply. urlopen serves a locally built release zip."""

    def setUp(self):
        import sqlite3
        super().setUp()
        tag = "v" + NEW
        self.zip = _zip_release(_release_files(NEW), wrapper=f"aerodrome-{tag}")
        sha = hashlib.sha256(self.zip).hexdigest()
        self.assets = {f"aerodrome-{tag}.zip": self.zip,
                       f"aerodrome-{tag}.zip.sha256": f"{sha}  aerodrome-{tag}.zip\n".encode()}
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO update_state (id, last_check_ts, last_check_result, "
                  "last_known_latest, last_known_latest_ts, updated_at) "
                  "VALUES (1, ?, 'success', ?, ?, ?)",
                  (int(time.time()), tag, int(time.time()), int(time.time())))
        c.commit()
        c.close()
        # nothing staged locally
        for item in (self.inst / "update").iterdir():
            if item.name != "UPDATE_README.md":
                shutil.rmtree(item) if item.is_dir() else item.unlink()

    def urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        data = self.assets[url.rsplit("/", 1)[1]]
        m = mock.MagicMock()
        m.read.return_value = data
        m.__enter__.return_value = m
        return m

    def test_download_stage_verify_swap(self):
        gh = self.ep("/api/updates/github/apply")
        import asyncio
        self.assertFalse(asyncio.iscoroutinefunction(gh))
        with mock.patch("urllib.request.urlopen", self.urlopen):
            r = gh(snapshot_db=False)
        b = _body(r)
        self.assertTrue(b["ok"], b)
        self.assertEqual((b["from_version"], b["to_version"]), (OLD, NEW))
        self.assertEqual((self.inst / "VERSION").read_text().strip(), NEW)
        self.assertTrue((self.inst / "only_in_new.py").exists())
        self.assertEqual(updater.pending(self.inst)["state"], "pending_first_boot")
        self.restart.assert_called_once()

    def test_checksum_mismatch_changes_nothing(self):
        self.assets["aerodrome-v" + NEW + ".zip.sha256"] = b"0" * 64 + b"  x.zip\n"
        live = self.live_snapshot()
        with mock.patch("urllib.request.urlopen", self.urlopen):
            r = self.ep("/api/updates/github/apply")(snapshot_db=False)
        self.assertEqual(_status(r), 500)
        self.assertEqual(self.live_snapshot(), live)
        self.restart.assert_not_called()


class TestUploadEndpoint(_EndpointCase):

    def test_upload_stages_and_reports(self):
        data = _zip_release(_release_files("3.4.160"), wrapper="aerodrome-v3.4.160")
        r = self.ep("/api/updates/local/upload")(file=_upload(data))
        b = _body(r)
        self.assertTrue(b["ok"], b)
        self.assertEqual((b["update_version"], b["status"]), ("3.4.160", "newer"))

    def test_held_lock_refuses_and_keeps_update_dir(self):
        before = self.update_snapshot()
        tok = self.server._MAINT.try_acquire("an update")
        self.addCleanup(self.server._MAINT.release, tok)
        data = _zip_release({"VERSION": "3.4.160\n", "main.py": "#\n"})
        r = self.ep("/api/updates/local/upload")(file=_upload(data))
        self.assertEqual(_status(r), 409)
        self.assertEqual(self.update_snapshot(), before)


# ===========================================================================
# Review fixes: each class pins one defect found in review (failing-first)
# ===========================================================================

def _no_links():
    """os.link refused as fs.protected_hardlinks refuses it for files another
    account owns (or a filesystem without hard links): every copy of a live
    file into the old dir is then a real copy."""
    def deny(*a, **kw):
        raise OSError(errno.EPERM, "Operation not permitted")
    return mock.patch("os.link", deny)


class _CopyFault:
    """shutil.copyfile writes HALF the file at call #k and then raises `exc`
    (_Kill by default: a kill or a power cut in the middle of a copy)."""

    def __init__(self, k, exc=None):
        self.k, self.n, self.exc = k, 0, exc

    def __enter__(self):
        real = shutil.copyfile

        def half(src, dst, *a, **kw):
            self.n += 1
            if self.n == self.k:
                data = Path(src).read_bytes()
                with open(dst, "wb") as f:
                    f.write(data[:len(data) // 2])
                raise (self.exc or _Kill())
            return real(src, dst, *a, **kw)
        self.p = mock.patch("shutil.copyfile", half)
        self.p.start()
        return self

    def __exit__(self, *a):
        self.p.stop()


class TestCopyFallbackIsAtomic(_KillCase):
    """When hard links are refused, the previous release's files are COPIED
    into the old dir. A copy cut short must never be what a rollback puts
    back (a truncated server.py, and no journal left to fix it)."""

    def test_kill_inside_a_copy_then_the_next_starts(self):
        points = 0
        for k in range(1, 40):
            inst, rel = self.fresh()
            want = _census(inst)
            staging, ts = self.staging_of(inst, rel)
            with _no_links(), _CopyFault(k):
                try:
                    updater.swap_in(inst, staging, ts, OLD, NEW)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            with self.subTest(kill_at=k):
                self.assert_one_version(f"copy kill k={k}", inst, want, _boot(inst))
        self.assertGreaterEqual(points, 4)

    def test_io_error_inside_a_copy_is_put_back_whole(self):
        for k in (2, 3, 4):
            with self.subTest(copy=k):
                inst, rel = self.fresh()
                want = _census(inst)
                staging, ts = self.staging_of(inst, rel)
                with _no_links(), _CopyFault(k, OSError(errno.EIO, "Input/output error")):
                    with self.assertRaises(updater.ApplyError) as cm:
                        updater.swap_in(inst, staging, ts, OLD, NEW)
                self.assertIn("was put back", cm.exception.message)
                self.assertEqual(_census(inst), want)
                self.assertFalse((inst / updater.PENDING).exists())


class TestReallyFullDisk(_KillCase):
    """A full ext4: every write-open AND every mkdir fails with ENOSPC, while
    renames, links and unlinks still work. The rollback that a full disk
    triggers must still complete (it can't create a directory then)."""

    @contextlib.contextmanager
    def full(self, after_first_journal_write=False):
        state = {"armed": not after_first_journal_write}
        real_open, real_mkdir, real_wj = open, os.mkdir, updater._write_json

        def enospc(p):
            raise OSError(errno.ENOSPC, "No space left on device", str(p))

        def full_open(path, mode="r", *a, **kw):
            if state["armed"] and any(c in mode for c in "wax+"):
                enospc(path)
            return real_open(path, mode, *a, **kw)

        def full_mkdir(path, *a, **kw):
            if state["armed"]:
                enospc(path)
            return real_mkdir(path, *a, **kw)

        def write_json(path, obj):
            try:
                return real_wj(path, obj)
            finally:
                state["armed"] = True
        with mock.patch("builtins.open", full_open), mock.patch("os.mkdir", full_mkdir), \
                mock.patch.object(updater, "_write_json", write_json):
            yield

    def test_counter_write_fails_and_the_rollback_still_completes(self):
        inst, rel = self.fresh()
        want = _census(inst)
        staging, ts = self.staging_of(inst, rel)
        updater.swap_in(inst, staging, ts, OLD, NEW)
        with self.full():
            r = updater._on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                 log=lambda m: None, now=time.time)
        self.assertEqual(r, "rolled_back")
        self.assertEqual(_census(inst), want)
        self.assertFalse((inst / updater.PENDING).exists())
        self.assert_one_version("full disk", inst, want, _boot(inst))

    def test_disk_fills_during_the_swap_and_everything_is_put_back(self):
        inst, rel = self.fresh()
        want = _census(inst)
        staging, ts = self.staging_of(inst, rel)
        with self.full(after_first_journal_write=True):
            with self.assertRaises(updater.ApplyError) as cm:
                updater.swap_in(inst, staging, ts, OLD, NEW)
        self.assertIn("was put back", cm.exception.message)
        self.assertEqual(_census(inst), want)
        self.assertFalse((inst / updater.PENDING).exists())
        self.assertEqual(_leftovers(inst), [])


class TestInterruptedRollbackIsFinished(_KillCase):
    """The immediate rollback on a full disk can't record 'rolling_back'.
    If it is cut short (power cut), the journal still says
    pending_first_boot — the next start must finish the rollback, never
    count (or commit) the mixed tree it finds."""

    def journal_writes_fail(self):
        return TestBootENOSPC.full(self)

    def test_kill_at_every_point_of_a_full_disk_rollback(self):
        points = mixed = 0
        rel_files = None
        for k in range(1, 120):
            inst, rel = self.fresh()
            want = _census(inst)
            if rel_files is None:
                rel_files = {p.relative_to(rel).as_posix(): p.read_text()
                             for p in rel.rglob("*") if p.is_file()}
            staging, ts = self.staging_of(inst, rel)
            updater.swap_in(inst, staging, ts, OLD, NEW)          # failed_starts 0
            with self.journal_writes_fail(), _Faults(k):
                try:
                    updater._on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                     log=lambda m: None, now=time.time)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            with self.subTest(kill_at=k):
                j = updater.pending(inst)
                self.assertEqual(j["state"], "pending_first_boot")   # nothing recorded
                if updater._swap_intact(inst, j):
                    # nothing of the live tree changed yet: the new version is
                    # whole, and the next start counts it as usual
                    got, outs = _boot(inst, 1)
                    self.assertEqual(got, NEW, outs)
                    c = _census(inst)
                    for f, text in rel_files.items():
                        self.assertEqual(c.get(f), text, f"k={k}: {f} is not the new one")
                    continue
                mixed += 1
                before = _census(inst)
                self.assertFalse(updater.commit(inst))           # never commit a mix
                self.assertEqual(_census(inst), before)
                self.assert_one_version(f"ENOSPC+kill k={k}", inst, want, _boot(inst))
        self.assertGreaterEqual(points, 10)
        self.assertGreaterEqual(mixed, 5)


class TestOldCopyRemovedByHand(_KillCase):
    """.update-old-<ts>/ removed by hand (to free space while the commit was
    stuck on a full disk) leaves a whole new release and nothing to put
    back. That is not a rollback cut short: the guard used to 'finish' one,
    which only took the release's added files away (atomic_io.py here),
    dropped the journal and recorded a rollback to a version that wasn't
    installed — and commit() refused the tree, so nothing could confirm it."""

    def swapped_without_old_dir(self, **change):
        inst, rel = self.fresh()
        staging, ts = self.staging_of(inst, rel)
        updater.swap_in(inst, staging, ts, OLD, NEW)
        want_new = _census(inst)
        j = dict(updater.pending(inst), **change)
        updater._write_json(inst / updater.PENDING, j)
        shutil.rmtree(inst / j["old_dir"])
        return inst, want_new

    def boot(self, inst, reexec):
        return updater.on_boot(inst, ["main.py", "start"], reexec=reexec, log=lambda m: None)

    def test_served_long_ago_is_committed_whole(self):
        inst, want_new = self.swapped_without_old_dir(
            failed_starts=1, served_at=time.time() - 11 * 60)
        reexec = mock.Mock()
        self.assertEqual(self.boot(inst, reexec), "committed")
        reexec.assert_not_called()
        self.assertEqual(_census(inst), want_new)
        self.assertTrue((inst / "atomic_io.py").exists())
        self.assertIsNone(updater.pending(inst))
        r = updater.last_result(inst)
        self.assertEqual((r["result"], r["to_version"]), ("applied", NEW))
        self.assertIsNone(r["backup_dir"])                 # there was no copy to keep
        self.assertEqual(_leftovers(inst), [])

    def test_the_confirm_step_can_commit_it(self):
        inst, want_new = self.swapped_without_old_dir()
        self.assertTrue(updater.commit(inst))
        self.assertIsNone(updater.pending(inst))
        self.assertEqual(_census(inst), want_new)

    def test_a_first_start_is_counted_as_usual(self):
        inst, want_new = self.swapped_without_old_dir()
        self.assertEqual(self.boot(inst, mock.Mock()), "counting")
        self.assertEqual(updater.pending(inst)["failed_starts"], 1)
        self.assertEqual(_census(inst), want_new)

    def test_a_rollback_with_nothing_to_put_back_is_skipped(self):
        for label, change, ctx in (
                ("two failed starts", {"failed_starts": updater.MAX_FAILED_STARTS},
                 contextlib.nullcontext),
                ("counter write fails", {}, lambda: TestBootENOSPC.full(self))):
            with self.subTest(label):
                inst, want_new = self.swapped_without_old_dir(**change)
                logs = []
                reexec = mock.Mock()
                with ctx():
                    got = updater.on_boot(inst, ["main.py", "start"], reexec=reexec,
                                          log=logs.append)
                self.assertEqual(got, "no_rollback")
                reexec.assert_not_called()
                self.assertEqual(_census(inst), want_new)
                self.assertEqual(updater.pending(inst)["state"], "pending_first_boot")
                self.assertIsNone(updater.last_result(inst))
                self.assertIn("nothing to put back", logs[-1])


class TestNoHardLinksAndAFullDisk(_KillCase):
    """A filesystem without hard links (exFAT, CIFS, some FUSE mounts: every
    os.link fails) on a full disk: the rollback can't park the new release's
    files by link or by copy. It used to fail on every start ('error'), so
    the new release ran uncounted for good. Now it lets the parked copy go
    and still puts every file back in one step."""

    def test_the_boot_rollback_completes(self):
        for links_at_swap in (True, False):
            with self.subTest(links_at_swap=links_at_swap):
                inst, rel = self.fresh()
                want = _census(inst)
                staging, ts = self.staging_of(inst, rel)
                with contextlib.nullcontext() if links_at_swap else _no_links():
                    updater.swap_in(inst, staging, ts, OLD, NEW)
                got = []
                for _ in range(3):
                    with _no_links(), TestReallyFullDisk.full(self), \
                            mock.patch.object(updater, "_log"):
                        got.append(updater.on_boot(inst, ["main.py", "start"],
                                                   reexec=lambda: None, log=lambda m: None))
                self.assertEqual(got, ["rolled_back", "none", "none"])
                self.assertEqual(_census(inst), want)
                self.assertFalse((inst / updater.PENDING).exists())
                self.assert_one_version("no links, full disk", inst, want, _boot(inst))

    def test_kill_at_every_point_of_it(self):
        points = mixed = 0
        for k in range(1, 120):
            inst, rel = self.fresh()
            want = _census(inst)
            staging, ts = self.staging_of(inst, rel)
            updater.swap_in(inst, staging, ts, OLD, NEW)
            want_new = _census(inst)
            with _no_links(), TestReallyFullDisk.full(self), _Faults(k), \
                    mock.patch.object(updater, "_log"):
                try:
                    updater._on_boot(inst, ["main.py", "start"], reexec=lambda: None,
                                     log=lambda m: None, now=time.time)
                    hit = False
                except _Kill:
                    hit = True
            if not hit:
                break
            points += 1
            with self.subTest(kill_at=k):
                j = updater.pending(inst)
                if not updater._rollback_cut_short(inst, j):
                    # nothing of the live tree changed yet: the new version
                    # is whole, and the next start counts it as usual
                    self.assertEqual(_census(inst), want_new)
                    got, outs = _boot(inst, 1)
                    self.assertEqual(got, NEW, outs)
                    continue
                mixed += 1
                before = _census(inst)
                self.assertFalse(updater.commit(inst))           # never commit a mix
                self.assertEqual(_census(inst), before)
                self.assert_one_version(f"no links+full+kill k={k}", inst, want, _boot(inst))
        self.assertGreaterEqual(points, 10)
        self.assertGreaterEqual(mixed, 5)


class TestInProcessUndo(_KillCase):
    """The server undoes a switch in its own process when the restart call
    fails (swap_back with restore_config=False, record=False,
    discard_failed=True)."""

    def undo(self, inst, j):
        updater.swap_back(inst, j, reason="restart failed: sudo: a password is required",
                          restore_config=False, record=False, discard_failed=True)

    def test_kill_at_every_point_with_and_without_a_full_disk(self):
        # Guard (behaviour already right; pinned here): every kill point,
        # with the journal writable or not, ends on one whole version — the
        # previous one, or the new one counted like any first start.
        for full in (False, True):
            points = 0
            for k in range(1, 120):
                inst, rel = self.fresh()
                want = _census(inst)
                staging, ts = self.staging_of(inst, rel)
                j = updater.swap_in(inst, staging, ts, OLD, NEW)
                want_new = _census(inst)
                with (TestBootENOSPC.full(self) if full else contextlib.nullcontext()), \
                        _Faults(k):
                    try:
                        self.undo(inst, j)
                        hit = False
                    except _Kill:
                        hit = True
                if not hit:
                    break
                points += 1
                with self.subTest(full=full, kill_at=k):
                    got, outs = _boot(inst)
                    if got == NEW:
                        drop = lambda c: {f: t for f, t in c.items() if f != "config.yaml"}
                        self.assertEqual(drop(_census(inst)), drop(want_new))
                        j2 = updater.pending(inst)
                        self.assertEqual((j2["state"], j2["failed_starts"]),
                                         ("pending_first_boot", 1))
                    else:
                        self.assert_one_version(f"undo full={full} k={k}", inst, want,
                                                (got, outs))
            self.assertGreaterEqual(points, 10, f"full={full}")

    def test_settings_saved_after_an_undo_that_stopped_are_kept(self):
        # The undo stops part-way (an I/O error, not a kill): the journal
        # says 'rolling_back' and the old process keeps running. Settings
        # saved there must survive the start that finishes the undo — it
        # used to put the pre-update config.yaml back over them.
        stopped = 0
        for k in range(2, 60):
            inst, rel = self.fresh()
            want = _census(inst)
            staging, ts = self.staging_of(inst, rel)
            j = updater.swap_in(inst, staging, ts, OLD, NEW)
            with _Faults(k, OSError(errno.EIO, "Input/output error")):
                try:
                    self.undo(inst, j)
                    continue                      # this op's error was absorbed
                except OSError:
                    pass
            stopped += 1
            with self.subTest(io_error_at=k):
                self.assertEqual(updater.pending(inst)["state"], "rolling_back")
                edited = "web: {host: 127.0.0.1}\n# saved in the old version's UI\n"
                (inst / "config.yaml").write_text(edited)
                got, outs = _boot(inst)
                self.assertEqual(got, OLD, outs)
                self.assertEqual((inst / "config.yaml").read_text(), edited)
                self.assertEqual(_census(inst), dict(want, **{"config.yaml": edited}))
                self.assertFalse((inst / updater.PENDING).exists())
        self.assertGreaterEqual(stopped, 5)


class TestStaleJournal(_InstallCase):
    """Going back (allow_downgrade) to a release without the guard leaves a
    journal it never reads. Going forward again with that release's own
    updater copies files over it. The next guarded start must not count
    the journal against a different version — that rolled a working release
    back after two starts, and put back a config.yaml from long before."""

    def test_a_journal_for_another_version_is_set_aside(self):
        self.stage_and_swap(ts="20261021-150000")
        (self.inst / "VERSION").write_text("3.4.152\n")     # applied by the older updater
        (self.inst / "config.yaml").write_text("web: {port: 18300}  # edited since\n")
        cfg = (self.inst / "config.yaml").read_bytes()
        reexec = mock.Mock()
        got = [updater.on_boot(self.inst, ["main.py", "start"], reexec=reexec,
                               log=lambda m: None) for _ in range(3)]
        self.assertEqual(got, ["stale", "none", "none"])
        reexec.assert_not_called()
        self.assertEqual((self.inst / "config.yaml").read_bytes(), cfg)
        self.assertEqual(self.live_version(), "3.4.152")
        self.assertIsNone(updater.pending(self.inst))
        self.assertIsNone(updater.last_result(self.inst))
        self.assertTrue((self.inst / f"{updater.STALE_PREFIX}20261021-150000.json").exists())
        bk = self.inst / ".backups" / "20261021-150000"
        self.assertEqual((bk / "VERSION").read_text().strip(), OLD)
        self.assertFalse((bk / updater.CONFIG_SNAPSHOT).exists())
        self.assertEqual(_leftovers(self.inst), [])


class TestOrphansAreSwept(_InstallCase):
    """An .update-old-<ts>/ nothing names any more (a kill before the first
    journal write, or a commit that couldn't move it) must not keep a copy
    of config.yaml around for good."""

    @unittest.skipIf(IS_ROOT, "root can write a 0555 directory")
    def test_a_commit_that_cant_move_the_old_tree_keeps_no_config_copy(self):
        j = self.stage_and_swap(ts="20261021-190000")
        bk = self.inst / ".backups"
        bk.mkdir()
        bk.chmod(0o555)
        self.addCleanup(bk.chmod, 0o755)
        self.assertTrue(updater.commit(self.inst))
        old = self.inst / j["old_dir"]
        self.assertTrue((old / "VERSION").exists())           # the only copy: kept
        self.assertFalse((old / updater.CONFIG_SNAPSHOT).exists())
        self.assertEqual(updater.last_result(self.inst)["backup_dir"], j["old_dir"])
        bk.chmod(0o755)
        self.assertEqual(updater.on_boot(self.inst, ["main.py", "start"]), "none")
        self.assertFalse(old.exists())
        self.assertEqual((bk / "20261021-190000" / "VERSION").read_text().strip(), OLD)

    def test_an_unreadable_journal_keeps_them_until_there_is_none(self):
        old = self.inst / ".update-old-20261021-191000"
        _write(old / "VERSION", OLD + "\n")
        _write(old / updater.CONFIG_SNAPSHOT, "web: {port: 18299}\n")
        tmp = self.inst / (updater.PENDING + ".tmp")
        tmp.write_text("{")
        (self.inst / updater.PENDING).write_text("{not json")
        boot = lambda: updater.on_boot(self.inst, ["main.py", "start"], log=lambda m: None)
        self.assertEqual(boot(), "none")
        self.assertTrue((old / "VERSION").exists())             # left for inspection
        self.assertTrue((old / updater.CONFIG_SNAPSHOT).exists())
        self.assertTrue(tmp.exists())
        (self.inst / updater.PENDING).unlink()                   # no journal at all
        self.assertEqual(boot(), "none")
        self.assertFalse(old.exists())
        self.assertFalse(tmp.exists())
        bk = self.inst / ".backups" / "20261021-191000"
        self.assertEqual((bk / "VERSION").read_text().strip(), OLD)
        self.assertFalse((bk / updater.CONFIG_SNAPSHOT).exists())


class TestSymlinkedConfig(_InstallCase):

    def test_rollback_writes_through_a_symlinked_config(self):
        real = self.tmp / "etc-aerodrome" / "config.yaml"
        _write(real, "web: {port: 18299}\n")
        before = real.read_bytes()
        (self.inst / "config.yaml").unlink()
        (self.inst / "config.yaml").symlink_to(real)
        self.stage_and_swap()
        with open(self.inst / "config.yaml", "a") as f:    # the new release migrates it
            f.write("# migrated by the new release\n")
        argv = ["main.py", "start"]
        for _ in range(updater.MAX_FAILED_STARTS):
            self.assertEqual(updater.on_boot(self.inst, argv, reexec=mock.Mock()), "counting")
        self.assertEqual(updater.on_boot(self.inst, argv, reexec=mock.Mock()), "rolled_back")
        self.assertTrue(os.path.islink(self.inst / "config.yaml"))
        self.assertEqual(os.path.realpath(self.inst / "config.yaml"), os.path.realpath(real))
        self.assertEqual(real.read_bytes(), before)


class TestSwapOnlyWhatWasStaged(_InstallCase):

    def test_files_written_into_the_staging_dir_refuse_the_swap(self):
        ts = "20261021-160000"
        entries = updater.release_entries(self.staged, PRESERVE)
        staging = updater.stage(self.inst, self.staged, PRESERVE, ts)
        # what an import-time write during the test-load could leave there
        _write(staging / "config.yaml", "defaults: true\n")
        _write(staging / "logs" / "tracker.log", "")
        before = self.snapshot()
        for kw in ({"entries": entries}, {}):
            with self.subTest(**{"with_entries": bool(kw)}):
                with self.assertRaises(updater.ApplyError) as cm:
                    updater.swap_in(self.inst, staging, ts, OLD, NEW, **kw)
                self.assertEqual(cm.exception.code, "stage_failed")
                self.assertIn("config.yaml", cm.exception.detail)
                self.assertEqual(self.snapshot(), before)
                self.assertIsNone(updater.pending(self.inst))
                self.assertEqual(list(self.inst.glob(".update-old-*")), [])
                self.assertEqual(list(self.inst.glob(".update-failed-*")), [])

    def test_the_test_load_writes_nothing_into_the_release(self):
        staging = updater.stage(self.inst, self.staged, PRESERVE, "20261021-161000")
        _write(staging / "server.py",
               "import os\nopen(os.path.join(os.environ['HOME'], 'written-at-import'), 'w')"
               ".close()\ndef get_app(): pass\n")
        updater.verify(staging, sys.executable)
        self.assertEqual([p.name for p in staging.rglob("written-at-import")], [])


class TestJournalPathsAreChecked(_InstallCase):
    """The guard renames and deletes what the journal names. A damaged or
    hand-edited journal must never make it touch anything outside the
    install (or the install's own data)."""

    def journal(self, state, **change):
        ts = "20261021-170000"
        j = {"journal_version": 1, "state": state, "from_version": OLD, "to_version": NEW,
             "ts": ts, "entries": ["VERSION"], "had_old": ["VERSION"],
             "staging_dir": updater.STAGING_PREFIX + ts, "old_dir": updater.OLD_PREFIX + ts,
             "failed_starts": updater.MAX_FAILED_STARTS}
        j.update(change)
        (self.inst / updater.PENDING).write_text(json.dumps(j))
        return j

    def test_paths_outside_the_install_are_left_alone(self):
        victim = self.tmp / "victim"
        changes = [{"staging_dir": str(victim)}, {"old_dir": "../victim"},
                   {"entries": ["../victim"], "had_old": []}, {"ts": "../../escaped"},
                   {"entries": ["config.yaml"], "had_old": []}, {"staging_dir": ["not", "a", "name"]}]
        for change in changes:
            for state in ("rolling_back", "committing", "pending_first_boot"):
                with self.subTest(state=state, change=change):
                    _write(victim / "keep.txt", "sentinel\n")
                    cfg = (self.inst / "config.yaml").read_bytes()
                    j = self.journal(state, **change)
                    self.assertEqual(updater.on_boot(self.inst, ["main.py", "start"],
                                                     reexec=mock.Mock(), log=lambda m: None),
                                     "unknown")
                    self.assertEqual((victim / "keep.txt").read_text(), "sentinel\n")
                    self.assertEqual((self.inst / "config.yaml").read_bytes(), cfg)
                    self.assertEqual(updater.pending(self.inst), j)
                    self.assertFalse(updater.commit(self.inst))
                    with self.assertRaises(OSError):
                        updater.swap_back(self.inst, j, reason="test")
                    self.assertEqual((victim / "keep.txt").read_text(), "sentinel\n")
                    self.assertFalse((self.tmp.parent / "escaped").exists())


class TestSmallBehaviours(_InstallCase):

    def test_rollback_records_the_schema_before_and_after(self):
        import sqlite3
        db = self.inst / "aircraft_history.db"
        db.unlink()
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE schema_version (version INTEGER)")
        c.execute("INSERT INTO schema_version VALUES (17)")
        c.commit()
        c.close()
        ts = "20261021-180000"
        staging = updater.stage(self.inst, self.staged, PRESERVE, ts)
        updater.swap_in(self.inst, staging, ts, OLD, NEW, extra={
            "db_file": str(db), "schema_before": updater.schema_version_ro(db)})
        c = sqlite3.connect(db)                       # the new version migrated it
        c.execute("INSERT INTO schema_version VALUES (18)")
        c.commit()
        c.close()
        updater.swap_back(self.inst, updater.pending(self.inst), reason="test")
        r = updater.last_result(self.inst)
        self.assertEqual((r["result"], r["schema_before"], r["schema_after"]),
                         ("rolled_back", 17, 18))

    def test_commit_refuses_a_rollback_in_progress(self):
        for state in ("rolling_back", "swapping"):
            with self.subTest(state=state):
                self.setUp()
                j = dict(self.stage_and_swap(), state=state)
                updater._write_json(self.inst / updater.PENDING, j)
                before = self.snapshot()
                self.assertFalse(updater.commit(self.inst))
                self.assertEqual(updater.pending(self.inst)["state"], state)
                self.assertTrue((self.inst / j["old_dir"]).exists())
                self.assertFalse((self.inst / ".backups" / j["ts"]).exists())
                self.assertEqual(self.snapshot(), before)

    def test_a_failed_reexec_exits_75(self):
        class _Exit(BaseException):
            pass
        with mock.patch.object(updater.os, "execv", side_effect=OSError(8, "Exec format error")), \
                mock.patch.object(updater.os, "_exit", side_effect=_Exit) as ex, \
                mock.patch.object(updater, "_log"):
            with self.assertRaises(_Exit):
                updater._reexec()
        ex.assert_called_once_with(75)

    @unittest.skipIf(IS_ROOT, "root can write a 0555 directory")
    def test_the_fix_command_really_fixes_a_read_only_folder(self):
        docs = self.inst / "docs"
        docs.mkdir()
        docs.chmod(0o555)
        self.addCleanup(docs.chmod, 0o755)
        self.assertEqual(updater.permission_problems(self.inst, ["docs"]), [str(docs)])
        cmd = updater.fix_command([str(docs)])
        self.assertTrue(cmd.startswith("sudo chown -R "), cmd)
        # this account owns it: chown to itself and chmod need no sudo
        subprocess.run(cmd.replace("sudo ", ""), shell=True, check=True)
        self.assertEqual(updater.permission_problems(self.inst, ["docs"]), [])

    def test_a_bad_staged_version_is_shown_safely(self):
        bad = "<img src=x onerror=alert(1)>'" + "x" * 5000
        with self.assertRaises(updater.ApplyError) as cm:
            updater.check_versions(OLD, bad)
        msg = cm.exception.message
        self.assertEqual(cm.exception.code, "bad_version")
        for bit in ("<", ">", "\"", "=", "<img", "x'x"):
            self.assertNotIn(bit, msg)
        self.assertLess(len(msg), 200)
        big = self.tmp / "VERSION"
        big.write_text("3.4.151" + " " * 100_000 + "tail")
        self.assertEqual(len(updater.read_version(big)), len("3.4.151"))

    def test_pip_never_waits_for_input(self):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0, "", "")
        pip = self.tmp / "pip"
        _write(pip, "#!/bin/sh\n")
        with mock.patch.object(updater.subprocess, "run", run):
            self.assertEqual(updater.install_deps(self.staged, pip), (True, ""))
        self.assertIn("--no-input", seen["cmd"])
        self.assertIs(seen["kw"].get("stdin"), subprocess.DEVNULL)


# Journals exactly as 3.4.147 writes them (for _InstallCase's fixture tree).
# NEVER EDIT these: every later updater must finish or undo any journal an
# earlier one can leave behind (the N-1 rule). When the journal changes, ADD
# a frozen copy of the new shape beside them and give it the same test.
_FROZEN_JOURNAL_3_4_147 = {
    "journal_version": 1, "state": "pending_first_boot", "from_version": "3.4.150",
    "to_version": "3.4.151", "ts": "20261022-010000",
    "entries": ["VERSION", "main.py", "only_in_new.py", "requirements.txt", "server.py",
                "templates", "updater.py"],
    "had_old": ["VERSION", "main.py", "requirements.txt", "server.py", "templates",
                "updater.py"],
    "staging_dir": ".update-staging-20261022-010000",
    "old_dir": ".update-old-20261022-010000", "failed_starts": 0, "applier_pid": 4242,
    "started_at": 1792647343.0, "db_file": "/nonexistent/aircraft_history.db",
    "schema_before": 18, "pip_warning": None, "previous_loads": None}


class TestFrozenJournals(_InstallCase):
    """The N-1 rule as a test: this updater finishes or undoes every state
    of a frozen 3.4.147 journal correctly."""

    def install_with(self, **change):
        frozen = _FROZEN_JOURNAL_3_4_147
        staging = updater.stage(self.inst, self.staged, PRESERVE, frozen["ts"])
        real = updater.swap_in(self.inst, staging, frozen["ts"], OLD, NEW)
        self.assertEqual(real["entries"], frozen["entries"],
                         "the fixture tree changed: the frozen journal is for the old one")
        self.assertLessEqual(set(frozen) - {"db_file", "schema_before", "pip_warning",
                                            "previous_loads"}, set(real),
                             "a journal field was dropped (fields are additive-only)")
        j = dict(frozen, **change)
        (self.inst / updater.PENDING).write_text(json.dumps(j))
        return j

    def test_every_state_of_a_3_4_147_journal(self):
        cases = [({}, "counting", NEW),
                 ({"failed_starts": 2}, "rolled_back", OLD),
                 ({"served_at": time.time() - 11 * 60, "failed_starts": 1}, "committed", NEW),
                 ({"state": "swapping"}, "rolled_back", OLD),
                 ({"state": "rolling_back", "rollback_reason": "test"}, "rolled_back", OLD),
                 ({"state": "committing"}, "committed", NEW)]
        for change, want, version in cases:
            with self.subTest(**{k: str(v) for k, v in change.items()}):
                self.setUp()
                before = self.snapshot()
                self.install_with(**change)
                got = updater.on_boot(self.inst, ["main.py", "start"], reexec=mock.Mock(),
                                      log=lambda m: None)
                self.assertEqual(got, want)
                self.assertEqual(self.live_version(), version)
                if want == "rolled_back":
                    after = {k: v for k, v in self.snapshot().items()
                             if not k.startswith(".update-")}
                    self.assertEqual(after, before)
                    self.assertIsNone(updater.pending(self.inst))
                if want == "committed":
                    self.assertIsNone(updater.pending(self.inst))
                    bk = self.inst / ".backups" / _FROZEN_JOURNAL_3_4_147["ts"]
                    self.assertEqual((bk / "VERSION").read_text().strip(), OLD)
                    self.assertEqual(_leftovers(self.inst), [])

    def test_a_3_4_147_in_process_undo_journal_keeps_config_yaml(self):
        # 3.4.147's server-side undo (the restart failed) records
        # restore_config: false with 'rolling_back'. Whoever finishes that
        # undo must leave config.yaml as it is now.
        self.install_with(state="rolling_back", restore_config=False,
                          rollback_reason="restart failed: sudo: a password is required")
        edited = "web: {port: 18300}  # saved after the update was refused\n"
        (self.inst / "config.yaml").write_text(edited)
        got = updater.on_boot(self.inst, ["main.py", "start"], reexec=mock.Mock(),
                              log=lambda m: None)
        self.assertEqual(got, "rolled_back")
        self.assertEqual(self.live_version(), OLD)
        self.assertEqual((self.inst / "config.yaml").read_text(), edited)


class _Stop(Exception):
    pass


class TestMainReviewFixes(_MainCase):

    def test_stop_leaves_another_accounts_instance_and_its_pid_file(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              str(self.inst / "main.py"), "start"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        TestOtherInstancePid._wait_cmdline(p.pid)
        (self.inst / ".tracker.pid").write_text(str(p.pid))

        def eperm(pid, sig):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        out = io.StringIO()
        with mock.patch.object(updater.os, "kill", eperm), \
                mock.patch.object(updater.os, "geteuid", lambda _u=os.geteuid(): _u + 1), \
                contextlib.redirect_stdout(out):
            self.main.stop({})
        self.assertIn("running as another account", out.getvalue())
        self.assertTrue((self.inst / ".tracker.pid").exists())
        self.assertIsNone(p.poll())

    def test_restart_keeps_stops_output_when_stdout_is_a_pipe(self):
        code = textwrap.dedent(f"""\
            import os, sys
            sys.path.insert(0, {str(REPO)!r})
            sys.argv = ["main.py", "restart"]
            import main
            main.load_config = lambda: {{}}
            main.setup_logging = lambda c: None
            main.stop = lambda c: print("Sent stop signal (PID 4242).")
            real = os.execv
            main.os.execv = lambda exe, argv: real("/bin/true", ["/bin/true"])
            main.main()
            """)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=60, cwd=str(self.inst),
                           env={**os.environ, "PYTHONUNBUFFERED": ""})
        self.assertIn("Sent stop signal (PID 4242).", r.stdout, r.stderr[-500:])


class TestConfirmThreadSafety(_ConfirmCase):
    """The confirm thread's safety properties."""

    def test_served_at_is_recorded_and_commit_waits_30_s(self):
        clock = [1000.0]
        seen = {}
        real = updater.commit

        def spy(inst, *a, **kw):
            seen.setdefault("served_at", (updater.pending(inst) or {}).get("served_at"))
            seen.setdefault("t", clock[0])
            return real(inst, *a, **kw)
        with mock.patch.object(updater, "HEALTHY_AFTER_SEC", 30), \
                mock.patch.object(updater, "commit", spy):
            self.run_confirm("127.0.0.1", 18210, probe=lambda a, h, p: ("ok", NEW),
                             sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                             monotonic=lambda: clock[0])
        self.assertIsNotNone(seen.get("served_at"), "served_at wasn't recorded before commit")
        self.assertGreaterEqual(seen["t"] - 1000.0, 30, "committed before HEALTHY_AFTER_SEC")
        self.assertIsNone(updater.pending(self.install))

    def test_a_hostname_web_host_is_probed_by_ip(self):
        # web.host = a full name that isn't on the Host allowlist: the probe
        # must send the IP it connected to, or every update reads as broken.
        class _IpOnly(_VersionServer):
            def __init__(self):
                super().__init__()
                import http.server
                outer = self
                base = self.httpd.RequestHandlerClass

                class H(base):
                    def do_GET(self):
                        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                        try:
                            import ipaddress
                            ipaddress.ip_address(host)
                        except ValueError:
                            outer.status = 403
                        return base.do_GET(self)
                self.httpd.RequestHandlerClass = H
        srv = _IpOnly()
        self.addCleanup(srv.close)
        real = socket.getaddrinfo

        def resolve(host, *a, **kw):
            return real("127.0.0.1" if host == "box.example.com" else host, *a, **kw)
        with mock.patch("socket.getaddrinfo", resolve):
            self.assertEqual(self.main._probe_version("box.example.com", "box.example.com",
                                                      srv.port), ("ok", NEW))
            self.run_confirm("box.example.com", srv.port)
        self.exit.assert_not_called()
        self.assertEqual(srv.status, 200)
        self.assertIsNone(updater.pending(self.install))


class TestStartStartsTheConfirmThread(_MainCase):

    def test_pending_first_boot_starts_the_confirm_thread(self):
        install = _fake_release(self.inst / "i", OLD)
        staged = _fake_release(self.inst / "staged", NEW)
        staging = updater.stage(install, staged, PRESERVE, "20261021-140000")
        updater.swap_in(install, staging, "20261021-140000", OLD, NEW)
        called = threading.Event()
        got = {}

        def confirm(*a, **kw):
            got["args"] = a
            got["thread"] = threading.current_thread().name
            called.set()
        cfg = {"data": {"db_file": str(self.inst / "x.db")},
               "web": {"host": "127.0.0.1", "port": 18299},
               "receiver": {"ip": "192.0.2.10", "port": 8080, "poll_interval": 1},
               "retention": {"military_days": 1, "watchlist_days": 1, "all_days": 1}}
        with mock.patch.object(self.main, "BASE_DIR", install), \
                mock.patch.object(self.main, "_confirm_update_when_serving", confirm), \
                mock.patch.object(self.main, "run_collector", lambda *a, **k: None), \
                mock.patch.object(self.main, "set_db_path"), \
                mock.patch.object(self.main, "set_receiver_location"), \
                mock.patch.object(self.main, "get_app", return_value=object()), \
                mock.patch.object(self.main, "_preflight_bind_check", return_value=(True, "")), \
                mock.patch.object(self.main.signal, "signal"), \
                mock.patch.object(self.main.uvicorn, "run", side_effect=_Stop), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(_Stop):
                self.main.start(cfg)
        self.assertTrue(called.wait(5), "the confirm thread never started")
        self.assertEqual(got["args"], ("127.0.0.1", 18299, NEW))
        self.assertEqual(got["thread"], "aerodrome-update-confirm")


def _bzip2_bomb(declared=8, real=4 * 1024 * 1024, name="aerodrome-v3.4.199/VERSION"):
    """A zip whose bzip2 entry declares `declared` bytes but expands to `real`."""
    import struct
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("aerodrome-v3.4.199/main.py", "#\n", compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(name, b"\0" * real, compress_type=zipfile.ZIP_BZIP2)
    data = bytearray(buf.getvalue())
    cd = data.rfind(b"PK\x01\x02")
    lh = data.rfind(b"PK\x03\x04")
    struct.pack_into("<I", data, cd + 24, declared)
    struct.pack_into("<I", data, lh + 22, declared)
    return bytes(data)


class TestEndpointReviewFixes(_EndpointCase):

    def decompressors(self):
        seen = []
        real = zipfile._get_decompressor

        def rec(ct):
            seen.append(ct)
            return real(ct)
        p = mock.patch.object(zipfile, "_get_decompressor", rec)
        p.start()
        self.addCleanup(p.stop)
        return seen

    def test_upload_refuses_a_bzip2_entry_before_decompressing_it(self):
        seen = self.decompressors()
        before = self.update_snapshot()
        r = self.ep("/api/updates/local/upload")(file=_upload(_bzip2_bomb()))
        b = _body(r)
        self.assertEqual((_status(r), b.get("code")), (400, "not_a_release"), b)
        self.assertNotIn(zipfile.ZIP_BZIP2, seen)
        self.assertEqual(self.update_snapshot(), before)

    def test_github_refuses_a_bzip2_entry_before_decompressing_it(self):
        seen = self.decompressors()
        before = self.update_snapshot()
        data = _bzip2_bomb()
        tag = "v" + NEW
        assets = {f"aerodrome-{tag}.zip": data,
                  f"aerodrome-{tag}.zip.sha256":
                      f"{hashlib.sha256(data).hexdigest()}  aerodrome-{tag}.zip\n".encode()}
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO update_state (id, last_check_ts, last_check_result, "
                  "last_known_latest, last_known_latest_ts, updated_at) "
                  "VALUES (1, ?, 'success', ?, ?, ?)",
                  (int(time.time()), tag, int(time.time()), int(time.time())))
        c.commit()
        c.close()

        def urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            m = mock.MagicMock()
            m.read.return_value = assets[url.rsplit("/", 1)[1]]
            m.__enter__.return_value = m
            return m
        with mock.patch("urllib.request.urlopen", urlopen):
            r = self.ep("/api/updates/github/apply")(snapshot_db=False)
        self.assertEqual(_status(r), 400, _body(r))
        self.assertNotIn(zipfile.ZIP_BZIP2, seen)
        self.assertEqual(self.update_snapshot(), before)
        self.restart.assert_not_called()

    def test_a_stuck_swap_gets_its_own_refusal_text(self):
        (self.inst / updater.PENDING).write_text(json.dumps({"state": "swapping"}))
        r = self.apply()
        b = _body(r)
        self.assertEqual((_status(r), b["code"]), (409, "update_pending"))
        self.assertIn("didn't finish switching files", b["error"])
        self.assertNotIn("being confirmed", b["error"])

    def test_a_restart_asked_for_after_the_new_version_answered_commits_first(self):
        self.assertTrue(_body(self.apply())["ok"])
        self.server._MAINT = self.server._SingleRun()      # the restart "happened"
        restart = self.ep("/api/restart")
        # applied by THIS process, or not answered yet: never committed here
        j = updater.pending(self.inst)
        self.assertTrue(_body(restart())["ok"])
        self.assertEqual(updater.pending(self.inst), j)
        j = dict(j, applier_pid=os.getpid() + 100000)       # the new process
        updater._write_json(self.inst / updater.PENDING, j)
        self.assertTrue(_body(restart())["ok"])
        self.assertEqual(updater.pending(self.inst), j)
        # the new version answered: a planned restart commits first
        updater._write_json(self.inst / updater.PENDING, dict(j, served_at=time.time() - 5))
        cfg = (self.inst / "config.yaml").read_bytes()
        self.assertTrue(_body(restart())["ok"])
        self.assertIsNone(updater.pending(self.inst))
        self.assertEqual(updater.last_result(self.inst)["result"], "applied")
        self.assertEqual(updater.on_boot(self.inst, ["main.py", "start"]), "none")
        self.assertEqual((self.inst / "config.yaml").read_bytes(), cfg)

    def test_restart_without_sudo_says_so(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("sudo")):
            self.assertEqual(self.server._restart_service(), (False, "sudo or systemctl not found"))

    @unittest.skipIf(IS_ROOT, "root can write a 0555 directory")
    def test_preflight_ignores_folders_no_release_moves(self):
        for name in ("__pycache__", "my-notes"):
            (self.inst / name).mkdir()
            (self.inst / name).chmod(0o555)
            self.addCleanup((self.inst / name).chmod, 0o755)
        check = self.ep("/api/updates/local/check", "GET")
        self.assertTrue(check()["preflight"]["ok"], check()["preflight"])      # staged
        moved = self.inst / "update" / "aerodrome"
        moved.rename(self.tmp / "unstaged")
        self.assertTrue(check()["preflight"]["ok"], check()["preflight"])      # nothing staged
        (self.tmp / "unstaged").rename(moved)
        self.assertTrue(_body(self.apply())["ok"])

    def test_a_malformed_staged_version_is_refused_safely(self):
        root = self.stage_release(NEW)
        (root / "VERSION").write_text("<img src=x onerror=alert(1)>'" + "y" * 100_000)
        r = self.apply()
        b = _body(r)
        self.assertEqual((_status(r), b["code"]), (400, "bad_version"))
        for bit in ("<", ">", "=", "'y"):
            self.assertNotIn(bit, b["error"])
        self.assertLess(len(b["error"]), 200)
        res = updater.last_result(self.inst)
        self.assertLess(len(json.dumps(res)), 1000)
        self.assertNotIn("<", res["to_version"])

    def test_going_back_to_a_release_without_the_guard_is_final_at_once(self):
        self.stage_release("3.4.100", guarded=False)
        b = _body(self.apply(allow_downgrade=True))
        self.assertTrue(b["ok"], b)
        self.assertTrue(b["final"])
        self.assertIsNone(updater.pending(self.inst))
        res = updater.last_result(self.inst)
        self.assertEqual(res["result"], "applied")
        self.assertEqual((self.inst / res["backup_dir"] / "VERSION").read_text().strip(), OLD)
        self.assertEqual(_leftovers(self.inst), [])
        # a guarded release keeps its journal (it confirms itself)
        self.server._MAINT = self.server._SingleRun()
        self.stage_release("3.4.101")
        b = _body(self.apply())
        self.assertTrue(b["ok"], b)
        self.assertFalse(b["final"])
        self.assertEqual(updater.pending(self.inst)["state"], "pending_first_boot")

    def test_packages_that_break_the_running_version_are_reported(self):
        # The venv is shared and never rolled back. A stand-in for a package
        # the running version imports, which the new requirements break.
        _write(self.inst / "example_dep_b4.py", "old_api = 1\n")
        _write(self.inst / "server.py", "import example_dep_b4\nassert example_dep_b4.old_api\n"
                                        "def get_app(*a): return None\n")
        _write(self.inst / "venv" / "bin" / "pip",
               f"#!/bin/sh\necho \"pip $*\" >> {self.pip_log}\n"
               f"printf 'new_api = 1\\n' > {self.inst / 'example_dep_b4.py'}\nexit 0\n")
        self.stage_release(NEW, reqs="# Version: y\nexample-dep>=2\n")
        b = _body(self.apply())
        self.assertTrue(b["ok"], b)
        self.assertIs(b["previous_loads"], False)
        self.assertIs(updater.pending(self.inst)["previous_loads"], False)
        # …and a refusal after pip ran says the packages may have changed
        self.server._MAINT = self.server._SingleRun()
        updater.commit(self.inst)
        _write(self.inst / "example_dep_b4.py", "old_api = 1\n")
        _write(self.inst / "server.py", "import example_dep_b4\nassert example_dep_b4.old_api\n"
                                        "def get_app(*a): return None\n")
        self.stage_release("3.4.152", reqs="# Version: z\nexample-dep>=3\n",
                           extra={"server.py": "def get_app(:\n"})
        r = self.apply()
        b = _body(r)
        self.assertEqual(b["code"], "verify_failed")
        self.assertIn("Python packages may have been updated", b["error"])
        self.assertIn("no longer loads", b["error"])

    def test_a_file_the_test_load_writes_into_the_release_refuses_the_update(self):
        # Only the names stage() copied may be swapped in: the apply passes
        # them to swap_in. A release-like name (a .py file) written into the
        # staged copy while it test-loads would otherwise be switched in.
        self.stage_release(NEW, extra={"server.py": (
            "import os\n"
            "open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "
            "'written_while_loading.py'), 'w').close()\n"
            "def get_app(*a): return None\n")})
        live = self.live_snapshot()
        r = self.apply()
        b = _body(r)
        self.assertEqual((_status(r), b.get("code")), (500, "stage_failed"), b)
        self.assertIn("written_while_loading.py", b["detail"])
        self.restart.assert_not_called()
        self.assertEqual(self.live_snapshot(), live)
        self.assertIsNone(updater.pending(self.inst))

    def test_switching_to_real_data_commits_an_answered_update_first(self):
        import requests
        import yaml
        from starlette.requests import Request
        self.assertTrue(_body(self.apply())["ok"])
        self.server._MAINT = self.server._SingleRun()      # the restart "happened"
        j = dict(updater.pending(self.inst), applier_pid=os.getpid() + 100000,
                 served_at=time.time() - 5)                # the new process answered
        updater._write_json(self.inst / updater.PENDING, j)
        cfgp = self.tmp / "config-live.yaml"
        cfg = yaml.safe_load(cfgp.read_text())
        cfg.setdefault("demo", {})["enabled"] = True
        cfgp.write_text(yaml.safe_dump(cfg))
        app = self.server.get_app(cfg, str(cfgp))
        raw = json.dumps({"receiver_ip": "192.0.2.20", "receiver_port": 30047,
                          "latitude": 40.1, "longitude": -95.2}).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        req = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                       "query_string": b""}, receive)
        restarts_before = len(self.restart.restarts())
        with mock.patch.object(requests, "get", return_value=mock.Mock(status_code=200)):
            import asyncio
            out = asyncio.run(_endpoint(app, "/api/setup/switch-to-real", "POST")(req))
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(self.restart.restarts()), restarts_before + 1)
        self.assertIsNone(updater.pending(self.inst))
        self.assertEqual(updater.last_result(self.inst)["result"], "applied")
        cfg_after = (self.inst / "config.yaml").read_bytes()
        self.assertEqual(updater.on_boot(self.inst, ["main.py", "start"]), "none")
        self.assertEqual((self.inst / "config.yaml").read_bytes(), cfg_after)


# ===========================================================================
# Static gates
# ===========================================================================

def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


class TestStaticGates(unittest.TestCase):

    def test_guard_block_comes_before_any_non_stdlib_import(self):
        src = (REPO / "main.py").read_text()
        self.assertIn("# --- update guard (boot-critical; stdlib only) ---", src)
        self.assertIn("# --- end update guard ---", src)
        tree = ast.parse(src)
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        guard_seen = False
        for node in tree.body:
            if isinstance(node, ast.Try) and any(
                    isinstance(n, ast.Import) and n.names[0].name == "updater" for n in node.body):
                calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                         and getattr(n.func, "attr", "") == "on_boot"]
                self.assertTrue(calls, "the guard block never calls on_boot()")
                guard_seen = True
                break
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for m in _imports(ast.Module(body=[node], type_ignores=[])):
                    self.assertIn(m, stdlib, f"main.py imports {m} before the update guard")
        self.assertTrue(guard_seen, "main.py has no update-guard block")

    def test_updater_is_stdlib_only_and_imports_no_app_module(self):
        app_modules = {p.stem for p in REPO.glob("*.py")}
        for m in _imports(ast.parse((REPO / "updater.py").read_text())):
            self.assertIn(m, sys.stdlib_module_names, f"updater.py imports {m}")
            self.assertNotIn(m, app_modules - set(sys.stdlib_module_names),
                             f"updater.py imports the app module {m}")

    def test_atomic_io_is_stdlib_only(self):
        for m in _imports(ast.parse((REPO / "atomic_io.py").read_text())):
            self.assertIn(m, sys.stdlib_module_names, f"atomic_io.py imports {m}")

    def test_new_and_changed_modules_parse_as_python_3_10(self):
        # README's floor is 3.10. Grammar only (feature_version); the
        # stdlib-only classes above also run under a real 3.10/3.11.
        for name in ("updater.py", "atomic_io.py", "main.py", "server.py", "collector.py",
                     "schema_migrations.py", "test_update_apply.py",
                     "test_maintenance_locks.py", "test_request_limits_offload.py",
                     "test_startup_rollup_backfill.py"):
            with self.subTest(module=name):
                ast.parse((REPO / name).read_text(), filename=name, feature_version=(3, 10))

    def test_updates_page_escapes_server_text_it_shows_as_html(self):
        # An apply refusal can carry text from the staged release; the
        # Updates page shows it through renderStatus()/innerHTML, i.e. as
        # HTML. Every ${…} interpolation and every string concatenation of
        # a response's error/message/detail must go through _esc() (or the
        # page's escapeHtml()).
        import re
        src = (REPO / "templates" / "updates.html").read_text()
        field = r"j\.(?:error|message|detail)\b"
        raw = []
        for m in re.finditer(r"\$\{([^}]*)\}", src):
            body = m.group(1).strip()
            if re.search(field, body) and not body.startswith(("_esc(", "escapeHtml(")):
                raw.append(m.group(0))
        raw += re.findall(r"\+\s*\(?\s*" + field + r"[^\n]*", src)
        self.assertEqual(raw, [], "server text shown as HTML without _esc()")


class TestShipsWithRelease(unittest.TestCase):
    """Packaging ships the git index only: a module that main/server/
    collector/updater import but that isn't tracked (or `git add -N`'d)
    would be missing from the zip. (Backstop; the packaged-tree import
    check in package-release.sh tests the zip itself.)"""

    def test_every_imported_app_module_is_tracked(self):
        if not (REPO / ".git").exists() or shutil.which("git") is None:
            self.skipTest("not a git checkout")
        r = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest(f"git ls-files failed: {r.stderr.strip()}")
        tracked = set(r.stdout.split())
        for src in ("main.py", "server.py", "collector.py", "updater.py"):
            for m in set(_imports(ast.parse((REPO / src).read_text()))):
                if (REPO / f"{m}.py").exists():
                    with self.subTest(importer=src, module=m):
                        self.assertIn(f"{m}.py", tracked,
                                      f"{m}.py is imported by {src} but not tracked — "
                                      f"`git add -N {m}.py` before packaging")


if __name__ == "__main__":
    unittest.main()
