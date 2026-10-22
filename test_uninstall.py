"""uninstall.sh under stubs — no root, nothing outside a temp dir.

Runs the WORKING tree's uninstall.sh from a fake install dir (HARD GUARD: it
must resolve under tempfile.gettempdir(); uninstall.sh takes its install dir
from its own location) with a fake HOME, cwd=/ and no terminal. sudo,
systemctl and firewall-cmd are stubs; the sudo stub maps /etc, /var and
/usr/local into a fake root, so the "system" files it removes are fakes too.

Covers L10: --purge removes everything Aerodrome created (database and its
side files, backups, update snapshots, staged updates, config backups);
config.yaml is parsed properly (quotes, comments, CRLF; PyYAML or the awk
fallback); outside the install dir only Aerodrome's own files go, never whole
folders, and system/home folders are refused whatever config.yaml says; the
firewall port is closed only when the installer recorded opening it;
prompts never stop a run that has no terminal.

Skipped when AERODROME_SKIP_SLOW_TESTS=1, without bash, or as root.
Names and paths are invented.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
UNINSTALL = REPO / "uninstall.sh"
SKIP_SLOW = os.environ.get("AERODROME_SKIP_SLOW_TESTS") == "1"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
GUARD = unittest.skipIf(SKIP_SLOW, "AERODROME_SKIP_SLOW_TESTS=1 (subprocess installer suite)")
NEEDS = unittest.skipUnless(shutil.which("bash") and not IS_ROOT, "needs bash, and not root")

SUDO = r'''#!/bin/bash
echo "sudo $*" >> "$STUB_LOG"
args=()
for a in "$@"; do
  case "$a" in
    "$TEST_TMP"/*) args+=("$a") ;;
    /etc/*|/var/*|/usr/local/*) args+=("$FAKE_ROOT$a") ;;
    *) args+=("$a") ;;
  esac
done
exec "${args[@]}"
'''
STUBS = {
    "sudo": SUDO,
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$STUB_LOG"\n'
                 'case "$1" in\n'
                 '  list-unit-files) printf "UNIT FILE STATE PRESET\\naerodrome.service enabled enabled\\n"'
                 '; [ -n "${FEEDER_UNIT:-}" ] && echo "aerodrome-synthetic-feeder.service enabled enabled"; exit 0 ;;\n'
                 '  is-active) [ "${FIREWALLD_ACTIVE:-0}" = 1 ] && [ "$2" = firewalld ] && exit 0; exit 3 ;;\n'
                 'esac\nexit 0\n',
    "firewall-cmd": '#!/bin/bash\necho "firewall-cmd $*" >> "$STUB_LOG"\n'
                    'case "$1" in --list-ports) echo "${FW_PORTS:-22/tcp}" ;; esac\nexit 0\n',
}

CFG = """receiver:
  ip: "192.0.2.10"
# --- Database ---
data:
{db}
  tuning:
    profile: "auto"
logging:
  level: "INFO"
{log}
"""


def sqlite_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("create table t(x)")
    c.commit()
    c.close()
    return path


class _Case(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aerodrome-uninstall-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inst = self.tmp / "inst"
        # HARD GUARD: uninstall.sh deletes relative to its own location.
        self.assertTrue(str(self.inst.resolve()).startswith(
            os.path.realpath(tempfile.gettempdir()) + os.sep), self.inst)
        self.home = self.tmp / "home" / "demo"
        self.home.mkdir(parents=True)
        (self.home / "notes.txt").write_text("mine\n")
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        for name, body in STUBS.items():
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        self.fr = self.tmp / "fakeroot"
        for d in ("etc/sudoers.d", "etc/systemd/system"):
            (self.fr / d).mkdir(parents=True)
        (self.fr / "etc/sudoers.d/aerodrome").write_text("# SUDOERS_VERSION: 5\n")
        (self.fr / "etc/sudoers.d/.aerodrome.Stale1").write_text("half-written\n")
        (self.fr / "etc/systemd/system/aerodrome.service").write_text("[Service]\n")
        self.log = self.tmp / "stub.log"
        self.log.write_text("")
        # the code that must survive
        self.inst.mkdir()
        shutil.copy2(UNINSTALL, self.inst / "uninstall.sh")
        (self.inst / "VERSION").write_text("3.4.145\n")
        (self.inst / "main.py").write_text("# code\n")
        (self.inst / "static").mkdir()
        (self.inst / "static" / "app.js").write_text("x\n")
        self.assertEqual(shutil.which("sudo", path=self.env()["PATH"]), str(self.stubs / "sudo"))

    def env(self, **kw):
        e = {"PATH": f"{self.stubs}:/usr/bin:/bin", "HOME": str(self.home), "TERM": "dumb",
             "LC_ALL": "C.UTF-8", "TMPDIR": str(self.tmp), "STUB_LOG": str(self.log),
             "FAKE_ROOT": str(self.fr), "TEST_TMP": str(self.tmp)}
        e.update({k: str(v) for k, v in kw.items()})
        return e

    def with_yaml_venv(self):
        """venv/bin/python3 = this interpreter (has PyYAML): the parser the app uses."""
        b = self.inst / "venv" / "bin"
        b.mkdir(parents=True, exist_ok=True)
        (b / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (b / "python3").chmod(0o755)

    def without_python(self):
        (self.stubs / "python3").write_text("#!/bin/sh\nexit 1\n")
        (self.stubs / "python3").chmod(0o755)

    def config(self, db='  db_file: "aircraft_history.db"', log='  dir: "logs"', raw=None):
        (self.inst / "config.yaml").write_text(raw if raw is not None else CFG.format(db=db, log=log))

    def run_un(self, *args, stdin=None, **kw):
        r = subprocess.run(["bash", str(self.inst / "uninstall.sh"), *args], env=self.env(**kw),
                           cwd="/", capture_output=True, text=True, timeout=60,
                           input=stdin if stdin is not None else "", start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def run_un_tty(self, *args, keys="", **kw):
        """Run with a pseudo-terminal on stdin (raw mode, keys pre-typed), so
        `read -p` prompts are printed the way a user sees them."""
        import pty
        import tty
        m, s = pty.openpty()
        try:
            tty.setraw(s)
            os.write(m, keys.encode())
            r = subprocess.run(["bash", str(self.inst / "uninstall.sh"), *args], env=self.env(**kw),
                               cwd="/", stdin=s, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=60, start_new_session=True)
        finally:
            os.close(s)
            os.close(m)
        return r.returncode, r.stdout

    def survivors(self):
        return sorted(str(p.relative_to(self.inst)) for p in self.inst.rglob("*")
                      if p.is_file() or p.is_symlink())

    CODE = ["VERSION", "main.py", "static/app.js", "uninstall.sh"]


@GUARD
@NEEDS
class Purge(_Case):

    def populate(self, db_rel="aircraft_history.db"):
        i = self.inst
        db = sqlite_file(i / db_rel)
        ts = "20261002-120000"
        for f in (f"{db}-wal", f"{db}-shm", f"{db}.bak.{ts}.pre-restore"):
            Path(f).write_text("data\n")
        (db.parent / ".daily-summary-state.json").write_text("{}\n")
        (i / "logs").mkdir()
        (i / "logs" / "tracker.log").write_text("log\n")
        (i / "logs" / "tracker.log.1").write_text("log\n")
        for f in ("config.yaml.bak.20261001-110000", f"config.yaml.bak.{ts}.pre-restore",
                  f"ntfy-server.yml.from-backup.{ts}"):
            (i / f).write_text("cfg\n")
        (i / ".backups" / "20261001-090000").mkdir(parents=True)
        (i / ".backups" / "20261001-090000" / "main.py").write_text("code\n")
        (i / ".backups" / ".uploads").mkdir()
        (i / ".backups" / ".uploads" / "upload-1-2.zip").write_text("zip\n")
        (i / ".backups" / f"aerodrome-backup-{ts}.zip").write_text("zip\n")
        (i / "update" / "aerodrome").mkdir(parents=True)
        (i / "update" / "aerodrome-v3.4.146.zip").write_text("zip\n")
        (i / "update" / "aerodrome" / "VERSION").write_text("3.4.146\n")
        (i / ".tracker.pid").write_text("1234\n")
        (i / "__pycache__").mkdir()
        (i / "__pycache__" / "server.cpython-314.pyc").write_text("pyc\n")
        (i / ".aerodrome-firewalld-port").write_text("8000/tcp\n")
        self.with_yaml_venv()

    def test_a_purge_leaves_only_the_code(self):
        self.config()
        self.populate()
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.survivors(), self.CODE, out)
        self.assertFalse((self.fr / "etc/sudoers.d/aerodrome").exists())
        self.assertEqual(sorted(p.name for p in (self.fr / "etc/sudoers.d").iterdir()), [])
        self.assertFalse((self.fr / "etc/systemd/system/aerodrome.service").exists())
        self.assertIn(f"cd / && sudo rm -rf {self.inst}", out)   # the dir itself: printed only
        self.assertEqual((self.home / "notes.txt").read_text(), "mine\n")

    def test_b_db_file_shapes_yaml(self):
        self._db_shapes()

    def test_g_db_file_shapes_awk_fallback(self):
        self._db_shapes(awk=True)

    def _db_shapes(self, awk=False):
        victim = sqlite_file(self.tmp / "victim" / "old-aircraft.db")
        shapes = {
            "double": '  db_file: "x.db"', "single": "  db_file: 'x.db'", "bare": "  db_file: x.db",
            "comment": "  db_file: x.db  # c", "crlf": "  db_file: x.db\r",
            "path in comment": f'  db_file: "x.db" # db_file: {victim}',
        }
        for name, line in shapes.items():
            with self.subTest(shape=name, awk=awk):
                if (self.inst / "venv").exists():
                    shutil.rmtree(self.inst / "venv")
                if awk:
                    self.without_python()
                else:
                    self.with_yaml_venv()
                text = CFG.format(db=line, log='  dir: "logs"')
                if name == "crlf":
                    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
                self.config(raw=text)
                sqlite_file(self.inst / "x.db")
                rc, out = self.run_un("--purge")
                self.assertEqual(rc, 0, out)
                self.assertFalse((self.inst / "x.db").exists(), out)
                self.assertTrue(victim.exists(), out)

    def test_c_absolute_db_outside_install(self):
        db = sqlite_file(self.tmp / "data" / "a.db")
        self.config(db=f'  db_file: "{db}"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(db.exists(), out)
        self.assertTrue(db.parent.is_dir())           # never the folder

    def test_d_custom_log_dir_inside_install(self):
        d = self.inst / "var" / "aero-logs"
        d.mkdir(parents=True)
        (d / "tracker.log").write_text("log\n")
        (d / "extra.txt").write_text("x\n")
        self.config(log='  dir: "var/aero-logs"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(d.exists(), out)             # inside the install: whole folder

    def test_f_db_file_dot_or_root_is_refused(self):
        for db in (".", "/"):
            with self.subTest(db=db):
                self.config(db=f'  db_file: "{db}"')
                rc, out = self.run_un("--purge")
                self.assertEqual(rc, 0, out)
                for f in ("VERSION", "main.py", "uninstall.sh"):
                    self.assertTrue((self.inst / f).exists(), out)
                self.assertRegex(out, r"(is a directory|refusing to delete)")

    def test_j_shared_outside_log_dir_keeps_other_files(self):
        s = self.tmp / "mnt-usb"
        (s / "photos").mkdir(parents=True)
        (s / "tracker.log").write_text("log\n")
        (s / "tracker.log.1").write_text("log\n")
        (s / "other-app.log").write_text("x\n")
        (s / "photos" / "IMG_0001.jpg").write_text("x\n")
        self.config(log=f'  dir: "{s}"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse((s / "tracker.log").exists())
        self.assertFalse((s / "tracker.log.1").exists())
        self.assertTrue((s / "other-app.log").exists())
        self.assertTrue((s / "photos" / "IMG_0001.jpg").exists())

    def test_dedicated_outside_log_dir_is_emptied_then_removed(self):
        d = self.tmp / "aero-logs"
        d.mkdir()
        (d / "tracker.log").write_text("log\n")
        self.config(log=f'  dir: "{d}"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(d.exists(), out)

    def test_k_log_dir_that_is_home_via_dotdot(self):
        (self.home / "stuff").mkdir()
        (self.home / "stuff" / "f").write_text("x\n")
        (self.home / "tracker.log").write_text("log\n")
        self.config(log=f'  dir: "{self.home}/../demo"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.home / "stuff" / "f").exists(), out)
        self.assertTrue((self.home / "notes.txt").exists(), out)

    def test_log_dir_root_is_refused(self):
        self.config(log='  dir: "/"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertIn("refusing to delete", out)

    def test_l_outside_db_that_is_not_sqlite_is_kept(self):
        v = self.tmp / "victim.txt"
        v.write_text("important\n")
        self.config(db=f'  db_file: "{v}"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertEqual(v.read_text(), "important\n")
        self.assertIn("not a SQLite database", out)

    def test_m_outside_sqlite_db_and_its_side_files(self):
        o = self.tmp / "data"
        db = sqlite_file(o / "a.db")
        for f in ("a.db-wal", "a.db.bak.20261021-101010.pre-restore", ".daily-summary-state.json"):
            (o / f).write_text("x\n")
        (o / "unrelated").write_text("keep\n")
        self.config(db=f'  db_file: "{db}"')
        rc, out = self.run_un("--purge")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(p.name for p in o.iterdir()), ["unrelated"], out)


@GUARD
@NEEDS
class KeepAndInteractive(_Case):

    def test_e_keep_keeps_all_data(self):
        db = sqlite_file(self.inst / "aircraft_history.db")
        (self.inst / "config.yaml").write_text(CFG.format(db='  db_file: "aircraft_history.db"',
                                                          log='  dir: "logs"'))
        (self.inst / "logs").mkdir()
        (self.inst / "logs" / "tracker.log").write_text("log\n")
        (self.inst / ".backups").mkdir()
        (self.inst / ".backups" / "b.zip").write_text("zip\n")
        rc, out = self.run_un("--keep")
        self.assertEqual(rc, 0, out)
        for p in (db, self.inst / "config.yaml", self.inst / "logs" / "tracker.log",
                  self.inst / ".backups" / "b.zip"):
            self.assertTrue(p.exists(), p)
        self.assertIn("Your data, settings and the", out)
        self.assertNotIn("Aerodrome has been removed from this system", out)
        self.assertFalse((self.fr / "etc/systemd/system/aerodrome.service").exists())

    def test_no_terminal_interactive_run_cancels_cleanly(self):
        rc, out = self.run_un()                      # stdin empty: "Are you sure?" -> no
        self.assertEqual(rc, 0, out)
        self.assertIn("Cancelled", out)
        self.assertTrue((self.fr / "etc/systemd/system/aerodrome.service").exists())

    def test_one_question_per_category_naming_the_path(self):
        db = sqlite_file(self.inst / "aircraft_history.db")
        (self.inst / "aircraft_history.db-wal").write_text("w\n")
        self.config()
        (self.inst / "logs").mkdir()
        (self.inst / "logs" / "tracker.log").write_text("log\n")
        (self.inst / ".backups").mkdir()
        (self.inst / ".backups" / "b.zip").write_text("zip\n")
        # sure=y, database=y, backups=n, logs=y, config=n, show-rm-command=n
        rc, out = self.run_un_tty(keys="yynynn")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("Delete Database"), 1)
        self.assertIn(f"{db} + 1 more", out)
        self.assertFalse(db.exists())
        self.assertFalse((self.inst / "aircraft_history.db-wal").exists())
        self.assertTrue((self.inst / ".backups" / "b.zip").exists())
        self.assertFalse((self.inst / "logs").exists())
        self.assertTrue((self.inst / "config.yaml").exists())

    def test_never_suggests_removing_system_packages(self):
        # Python is part of the system (pacman -R python would take core
        # tools with it); package names differ by distro and release.
        pkg_remove = re.compile(r"\b(apt-get|apt|dnf|yum|pacman|zypper|dpkg|rpm)\b")
        self.assertNotRegex(UNINSTALL.read_text(), pkg_remove)
        for flag in ("--purge", "--keep"):
            with self.subTest(flag=flag):
                rc, out = self.run_un(flag)
                self.assertEqual(rc, 0, out)
                self.assertNotRegex(out, pkg_remove)
                self.assertNotIn("Remove manually", out)
                self.assertIn("Uninstall complete!", out)

    def test_help(self):
        rc, out = self.run_un("--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("Deletion rules", out)
        self.assertIn("never as a whole folder", out)
        self.assertNotIn("# ", out.splitlines()[0])


@GUARD
@NEEDS
class WhereItRuns(_Case):
    """uninstall.sh deletes relative to its own folder: it must refuse, with
    nothing changed, anywhere but a real install — a copy in HOME, a staged
    update's copy, a piped run, an install placed directly in HOME — and a
    symlinked entry point must act on the install it points at."""

    def seed_home(self):
        h = self.home
        for d in ("venv/bin", "logs", ".backups", "proj/__pycache__", "update/dl"):
            (h / d).mkdir(parents=True, exist_ok=True)
        (h / "venv/bin/python3").write_text("someone else's venv\n")
        (h / "logs/app-2026.log").write_text("someone else's log\n")
        (h / ".backups/photos-2025.tar").write_text("someone else's backup\n")
        (h / "proj/__pycache__/m.cpython-314.pyc").write_text("pyc\n")
        (h / "update/dl/firmware.bin").write_text("download\n")
        (h / "config.yaml").write_text("other: app\n")

    def home_files(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")
                      if p.is_file() or p.is_symlink())

    def assert_nothing_changed(self, rc, out):
        self.assertEqual(rc, 2, out)
        self.assertIn("nothing was changed", out)
        self.assertTrue((self.fr / "etc/systemd/system/aerodrome.service").exists(), out)
        self.assertTrue((self.fr / "etc/sudoers.d/aerodrome").exists(), out)
        self.assertNotIn("[1/6]", out)
        self.assertNotIn("sudo ", self.log.read_text())

    def run_script(self, script, *args, cwd="/", stdin=""):
        r = subprocess.run(["bash", str(script), *args], env=self.env(), cwd=cwd,
                           capture_output=True, text=True, timeout=60, input=stdin,
                           start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def test_copy_in_home_is_refused(self):
        self.seed_home()
        shutil.copy2(UNINSTALL, self.home / "uninstall.sh")
        before = self.home_files()
        for flag in ("--purge", "--keep"):
            with self.subTest(flag=flag):
                rc, out = self.run_script(self.home / "uninstall.sh", flag)
                self.assert_nothing_changed(rc, out)
                self.assertIn("is not an Aerodrome install folder", out)
                self.assertEqual(self.home_files(), before)

    def test_staged_copy_is_refused_and_names_the_real_uninstall(self):
        self.with_yaml_venv()
        for staged in (self.inst / "update" / "aerodrome", self.inst / "update"):
            with self.subTest(staged=staged.name):
                staged.mkdir(parents=True, exist_ok=True)
                shutil.copy2(UNINSTALL, staged / "uninstall.sh")
                (staged / "VERSION").write_text("3.4.146\n")
                (staged / "main.py").write_text("# code\n")
                rc, out = self.run_script(staged / "uninstall.sh", "--purge")
                self.assert_nothing_changed(rc, out)
                self.assertIn("staged update inside the install at", out)
                self.assertIn(f"run: {self.inst.resolve()}/uninstall.sh", out)
                self.assertTrue((self.inst / "venv").is_dir())

    def test_symlinked_entry_point_acts_on_the_real_install(self):
        b = self.home / "bin"
        (b / "venv").mkdir(parents=True)
        (b / "venv" / "keep.txt").write_text("another tool's venv\n")
        (b / "aerodrome-uninstall").symlink_to(self.inst / "uninstall.sh")
        self.with_yaml_venv()
        rc, out = self.run_script(b / "aerodrome-uninstall", "--keep")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"Install dir:  {self.inst.resolve()}", out)
        self.assertFalse((self.inst / "venv").exists(), out)
        self.assertEqual((b / "venv" / "keep.txt").read_text(), "another tool's venv\n")
        self.assertFalse((self.fr / "etc/systemd/system/aerodrome.service").exists())

    def test_piped_run_is_refused(self):
        self.seed_home()
        before = self.home_files()
        r = subprocess.run(["bash", "-s", "--", "--purge"], env=self.env(), cwd=str(self.home),
                           input=UNINSTALL.read_text(), capture_output=True, text=True,
                           timeout=60, start_new_session=True)
        self.assert_nothing_changed(r.returncode, r.stdout + r.stderr)
        self.assertEqual(self.home_files(), before)

    def test_install_directly_in_a_home_folder_is_left_to_the_user(self):
        self.config()
        self.with_yaml_venv()
        (self.inst / "logs").mkdir()
        (self.inst / "logs" / "tracker.log").write_text("log\n")
        for home in (self.inst, self.inst / "sub"):        # HOME itself, or under it
            with self.subTest(home=home.name):
                home.mkdir(exist_ok=True)
                rc, out = self.run_un("--purge", HOME=home)
                self.assert_nothing_changed(rc, out)
                self.assertIn("by hand", out)
                self.assertNotIn("rm -rf", out)
                for f in self.CODE + ["config.yaml", "logs/tracker.log", "venv/bin/python3"]:
                    self.assertTrue((self.inst / f).exists(), f)

    def realpath_stub(self, body):
        real = shutil.which("realpath", path="/usr/bin:/bin")
        if not real:
            self.skipTest("no realpath on this host")
        (self.stubs / "realpath").write_text(f"#!/bin/bash\n{body}\nexec {real} \"$@\"\n")
        (self.stubs / "realpath").chmod(0o755)

    def test_realpath_without_m_is_refused(self):
        self.realpath_stub('case " $* " in *" -m "*) echo "realpath: unrecognized option: m" >&2; exit 1 ;; esac')
        self.config()
        self.with_yaml_venv()
        rc, out = self.run_un("--purge")
        self.assert_nothing_changed(rc, out)
        self.assertIn("realpath -m", out)
        self.assertTrue((self.inst / "venv").is_dir())

    def test_a_path_that_cannot_be_normalised_is_never_deleted(self):
        # realpath fails only on '..' paths: the up-front check passes, and
        # every config.yaml path through '..' must then count as protected.
        self.realpath_stub('for a in "$@"; do case "$a" in */../*|*/..) exit 1 ;; esac; done')
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep\n")
        (victim / "tracker.log").write_text("log\n")
        for log_dir in ("../victim", "../home/demo"):
            with self.subTest(log_dir=log_dir):
                self.with_yaml_venv()
                self.config(log=f'  dir: "{log_dir}"')
                rc, out = self.run_un("--purge")
                self.assertEqual(rc, 0, out)
                self.assertIn("refusing to delete", out)
                self.assertEqual((victim / "precious.txt").read_text(), "keep\n")
                self.assertTrue((victim / "tracker.log").exists(), out)
                self.assertEqual((self.home / "notes.txt").read_text(), "mine\n")


@GUARD
@NEEDS
class Firewall(_Case):

    def test_h_purge_closes_port_the_installer_recorded(self):
        (self.inst / ".aerodrome-firewalld-port").write_text("8000/tcp\n")
        rc, out = self.run_un("--purge", FIREWALLD_ACTIVE=1, FW_PORTS="22/tcp 8000/tcp")
        self.assertEqual(rc, 0, out)
        self.assertIn("firewall-cmd --remove-port=8000/tcp --permanent", self.log.read_text())
        self.assertFalse((self.inst / ".aerodrome-firewalld-port").exists())

    def test_h2_purge_leaves_a_port_aerodrome_did_not_open(self):
        rc, out = self.run_un("--purge", FIREWALLD_ACTIVE=1, FW_PORTS="22/tcp 8000/tcp")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("--remove-port", self.log.read_text())
        self.assertIn("didn't record opening it", out)
        self.assertIn("sudo firewall-cmd --remove-port=8000/tcp --permanent && sudo firewall-cmd --reload", out)

    def test_keep_never_closes_it(self):
        (self.inst / ".aerodrome-firewalld-port").write_text("8000/tcp\n")
        rc, out = self.run_un("--keep", FIREWALLD_ACTIVE=1, FW_PORTS="8000/tcp")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("--remove-port", self.log.read_text())
        self.assertIn("To close it:", out)
        self.assertTrue((self.inst / ".aerodrome-firewalld-port").exists())

    def test_interactive_defaults(self):
        for marker, closed in ((True, True), (False, False)):
            with self.subTest(marker=marker):
                self.log.write_text("")
                m = self.inst / ".aerodrome-firewalld-port"
                if marker:
                    m.write_text("8000/tcp\n")
                elif m.exists():
                    m.unlink()
                # sure=y, then end of input: every later question takes its default
                rc, out = self.run_un(stdin="y", FIREWALLD_ACTIVE=1, FW_PORTS="8000/tcp")
                self.assertEqual(rc, 0, out)
                self.assertEqual("--remove-port" in self.log.read_text(), closed, out)


if __name__ == "__main__":
    unittest.main()
