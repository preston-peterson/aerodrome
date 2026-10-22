"""install.sh and scripts/recover-sudoers.sh under stubs — no root, no network,
nothing outside a temp dir.

Runs the WORKING tree's install.sh / recover-sudoers.sh (copied into temp
install dirs, one of them with a space in its path) with stub sudo, visudo,
systemctl, apt-get/dnf/pacman/zypper, python3, pip, id, whoami, getent, stat,
ss, ip, hostname, sleep and chown first on PATH. The sudo stub never escalates:
it maps /etc, /var and /usr/local arguments into a fake root under the temp
dir and runs the command as the current user. It can emulate sudo < 1.9.3 (a
malformed drop-in in the fake /etc/sudoers.d makes every later sudo call fail;
dot-named files are skipped, as sudo and sudo-rs skip them) and can kill the
whole run (`kill -KILL 0`) on the Nth call of a given command.

Covers B3b (#38): sudoers validated before it goes live and swapped in
atomically; package-manager failures reported, re-runs skip it; ExecStart with
a space in the path; no Group=<username>. And the permissions carrier: the
privileges-only refresh (from update/, update/aerodrome/, every recover command
an Updates page has shown, piped), the service-account rules, the downgrade
guard, the highest-marker hand-off in recover-sudoers.sh, and kill-points.

Skipped when AERODROME_SKIP_SLOW_TESTS=1, without bash, or as root.
Account names and paths are invented (one test also runs the real id and
whoami, so it reads the account it runs as at run time).
"""
import grp
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
INSTALL_SH = REPO / "install.sh"
RECOVER_SH = REPO / "scripts" / "recover-sudoers.sh"
MARKER = re.compile(r"\s*#\s*SUDOERS_VERSION\s*:\s*(\d+)")  # == server._read_sudoers_version
SKIP_SLOW = os.environ.get("AERODROME_SKIP_SLOW_TESTS") == "1"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
HAVE_BASH = bool(shutil.which("bash"))
GUARD = unittest.skipIf(SKIP_SLOW, "AERODROME_SKIP_SLOW_TESTS=1 (subprocess installer suite)")
NEEDS = unittest.skipUnless(HAVE_BASH and not IS_ROOT, "needs bash, and not root")

PARSER = r'''#!/bin/bash
# toy sudoers check: every rule line must be "<user> ALL=(ALL|root) NOPASSWD: /cmd..."
bad=0
while IFS= read -r l || [ -n "$l" ]; do
  case "$l" in ''|'#'*) continue ;; esac
  [[ "$l" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]*\$?\ ALL=\((ALL|root)\)\ NOPASSWD:\ /.+$ ]] || bad=1
done < "$1"
exit $bad
'''

SUDO = r'''#!/bin/bash
here="$(dirname "$0")"
state=valid
if [ "${SUDO_MODEL:-new}" = old ]; then
  # sudo < 1.9.3: one malformed drop-in breaks sudo for everyone. Dot-named
  # files are skipped (sudo and sudo-rs @includedir rules), like the real thing.
  for f in "$FAKE_ROOT"/etc/sudoers.d/*; do
    [ -f "$f" ] || continue
    "$here/sudoers-parse" "$f" || state=MALFORMED
  done
fi
echo "sudo[$state] $*" >> "$STUB_LOG"
if [ "$state" = MALFORMED ]; then
  echo "sudo: no valid sudoers sources found, quitting" >&2; exit 1
fi
args=()
for a in "$@"; do
  case "$a" in
    "$TEST_TMP"/*) args+=("$a") ;;
    /etc/*|/var/*|/usr/local/*) args+=("$FAKE_ROOT$a") ;;
    *) args+=("$a") ;;
  esac
done
cmd="$(basename "${args[0]}")"
if [ -n "${SUDO_REFUSE:-}" ] && [ "$cmd" = "$SUDO_REFUSE" ]; then
  echo "sudo: a password is required" >&2; exit 1
fi
if [ -n "${KILL_AT:-}" ] && [ "$cmd" = "$KILL_AT" ]; then
  n=$(( $(cat "$KILL_COUNTER" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$KILL_COUNTER"
  if [ "$n" = "${KILL_N:-1}" ]; then
    echo "KILLED at $cmd #$n" >> "$STUB_LOG"
    kill -KILL 0
  fi
fi
mkdir -p "$FAKE_ROOT/etc/systemd/system" "$FAKE_ROOT/etc/sudoers.d"
case "$cmd" in
  mktemp)   # hand back the path as the script asked for it (unmapped)
    out="$("${args[@]}")" || exit $?
    printf '%s\n' "${out#"$FAKE_ROOT"}"; exit 0 ;;
  install)  # non-root can't chown: drop -o/-g pairs
    out=(); skip=0
    for a in "${args[@]}"; do
      if [ $skip = 1 ]; then skip=0; continue; fi
      case "$a" in -o|-g) skip=1; continue ;; esac
      out+=("$a")
    done
    args=("${out[@]}") ;;
  chown) exit 0 ;;
esac
exec "${args[@]}"
'''

LOGGER = '#!/bin/bash\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'
PKG = ('#!/bin/bash\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
       'case "$1" in\n'
       '  update|refresh|-Sy) [ "${APT_UPDATE_RC:-0}" = 0 ] || { echo "E: repo-x has no Release file" >&2; exit $APT_UPDATE_RC; } ;;\n'
       '  install|-S) [ "${APT_INSTALL_RC:-0}" = 0 ] || { echo "E: Unable to locate package python3-venv" >&2; exit $APT_INSTALL_RC; } ;;\n'
       'esac\nexit 0\n')

STUBS = {
    "sudoers-parse": PARSER,
    "sudo": SUDO,
    "visudo": '#!/bin/bash\necho "visudo $*" >> "$STUB_LOG"\nf="${@: -1}"\n'
              '[ -n "${VISUDO_FAIL:-}" ] && { echo "$f:3:1: syntax error" >&2; exit 1; }\n'
              '"$(dirname "$0")/sudoers-parse" "$f" || { echo "$f:9:1: syntax error" >&2; exit 1; }\n',
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$STUB_LOG"\n'
                 'case "$1" in\n'
                 '  show) if [ -f "${UNIT_SHOW:-/nonexistent}" ]; then cat "$UNIT_SHOW";\n'
                 '        else printf "LoadState=not-found\\nWorkingDirectory=\\nUser=\\n"; fi ;;\n'
                 'esac\nexit 0\n',
    "apt-get": PKG, "dnf": PKG, "pacman": PKG, "zypper": PKG,
    "python3": '#!/bin/bash\necho "python3 $*" >> "$STUB_LOG"\n'
               'if [ "$1" = -c ] && [ -n "${PY_MISSING:-}" ]; then exit 1; fi\n'
               'if [ "$1 $2" = "-m venv" ]; then\n'
               '  mkdir -p "$3/bin"\n'
               '  printf "deactivate(){ :; }\\n" > "$3/bin/activate"\n'
               '  printf "#!/bin/sh\\nexit 0\\n" > "$3/bin/python3"; chmod +x "$3/bin/python3"\n'
               '  printf "#!/bin/sh\\nexit 0\\n" > "$3/bin/pip"; chmod +x "$3/bin/pip"\n'
               'fi\nexit 0\n',
    "pip": LOGGER,
    "sleep": "#!/bin/sh\nexit 0\n",
    "chown": LOGGER,
    "ss": "#!/bin/sh\nexit 0\n",
    "ip": "#!/bin/sh\nexit 1\n",
    "hostname": "#!/bin/sh\nexit 1\n",
    "id": '#!/bin/bash\ncase "$1" in\n  -u) echo "${FAKE_UID:-1000}" ;;\n'
          '  -un) echo "${FAKE_WHOAMI:-svc-a}" ;;\n  -gn) echo "${FAKE_GROUP:-svc-group}" ;;\n'
          '  *) exec /usr/bin/id "$@" ;;\nesac\n',
    "whoami": '#!/bin/sh\necho "${FAKE_WHOAMI:-svc-a}"\n',
    "getent": '#!/bin/sh\n[ "$1" = passwd ] || exit 2\n'
              'case "$2" in svc-a|svc-c|admin-b|demo-user|svc.dotted|SvcUpper|jdoe@corp.example.com)\n'
              '  echo "$2:x:1000:1000::/home/$2:/bin/sh"; exit 0 ;; esac\nexit 2\n',
    "stat": '#!/bin/bash\nif [ "$1 $2" = "-c %U" ]; then echo "${FAKE_OWNER:-svc-a}"; exit 0; fi\n'
            'exec /usr/bin/stat "$@"\n',
}


def first_marker(text):
    for line in text.splitlines():
        m = MARKER.match(line)
        if m:
            return int(m.group(1))
    return 0


def set_marker(script_text, n):
    """install.sh text with render_sudoers()'s marker changed to n."""
    out, k = re.subn(r"(?m)^# SUDOERS_VERSION: \d+$", f"# SUDOERS_VERSION: {n}", script_text, count=1)
    assert k == 1
    return out


class _Harness(unittest.TestCase):
    """Stubs + a fake root; helpers to lay out installs and run scripts."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aerodrome-install-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        for name, body in STUBS.items():
            p = self.stubs / name
            p.write_text(body)
            p.chmod(0o755)
        self.fr = self.tmp / "fakeroot"
        (self.fr / "etc" / "sudoers.d").mkdir(parents=True)
        (self.fr / "etc" / "systemd" / "system").mkdir(parents=True)
        self.log = self.tmp / "stub.log"
        self.log.write_text("")
        self.unit_show = self.tmp / "unit-show"
        self.live = self.fr / "etc" / "sudoers.d" / "aerodrome"
        (self.tmp / "home").mkdir()
        (self.tmp / "t").mkdir()
        self.os_release = self.tmp / "os-release"
        self.os_release.write_text('ID=debian\nPRETTY_NAME="Example Debian"\n')
        # Never let a real sudo run: the stub must win on the test PATH.
        self.assertEqual(shutil.which("sudo", path=self.env()["PATH"]), str(self.stubs / "sudo"))

    # -- environment / running ------------------------------------------------
    def env(self, **kw):
        e = {"PATH": f"{self.stubs}:/usr/bin:/bin", "HOME": str(self.tmp / "home"),
             "TERM": "dumb", "LC_ALL": "C.UTF-8", "TMPDIR": str(self.tmp / "t"),
             "FAKE_ROOT": str(self.fr), "STUB_LOG": str(self.log),
             "TEST_TMP": str(self.tmp), "UNIT_SHOW": str(self.unit_show),
             "KILL_COUNTER": str(self.tmp / "kill-counter"),
             "AERODROME_BOOTSTRAP_OS_RELEASE": str(self.os_release)}
        e.update({k: str(v) for k, v in kw.items()})
        return e

    def run_sh(self, argv, stdin_file=None, **kw):
        fh = open(stdin_file) if stdin_file else subprocess.DEVNULL
        try:
            return subprocess.run(argv, env=self.env(**kw), cwd="/", capture_output=True,
                                  text=True, timeout=60, start_new_session=True, stdin=fh)
        finally:
            if stdin_file:
                fh.close()

    def as_root(self, **kw):
        """`sudo bash …` typed by an admin account that is NOT the service user."""
        return dict({"FAKE_UID": 0, "FAKE_WHOAMI": "root", "SUDO_USER": "admin-b"}, **kw)

    def log_text(self):
        return self.log.read_text()

    def put_live(self, text):
        """Replace the fake live drop-in (a run leaves it 0440, like the real one)."""
        self.live.unlink(missing_ok=True)
        self.live.write_text(text)

    def dot_temps(self):
        return sorted(p.name for p in (self.fr / "etc" / "sudoers.d").iterdir()
                      if p.name.startswith(".aerodrome."))

    # -- layouts ----------------------------------------------------------------
    def make_tree(self, d, install_text=None, version="9.9.9", recover=True):
        d.mkdir(parents=True, exist_ok=True)
        (d / "scripts").mkdir(exist_ok=True)
        (d / "install.sh").write_text(install_text or INSTALL_SH.read_text())
        if recover:
            shutil.copy2(RECOVER_SH, d / "scripts" / "recover-sudoers.sh")
        (d / "VERSION").write_text(version + "\n")
        (d / "main.py").write_text("# main\n")
        (d / "requirements.txt").write_text("")
        (d / "config.yaml.example").write_text('receiver:\n  ip: "192.0.2.10"\n  port: 8080\n')
        return d

    def set_unit(self, wd, user="svc-a", load="loaded"):
        self.unit_show.write_text(f"LoadState={load}\nWorkingDirectory={wd}\nUser={user}\n")

    def unit_file(self, name="aerodrome"):
        return (self.fr / "etc" / "systemd" / "system" / f"{name}.service").read_text()


@GUARD
@NEEDS
class FullInstall(_Harness):
    """B3b #38 on a fresh full install (and its re-runs)."""

    def make_install(self, parent="My Apps", text=None):
        return self.make_tree(self.tmp / parent / "aerodrome", install_text=text)

    def run_install(self, d, *args, **env):
        return self.run_sh(["bash", str(d / "install.sh"), *args], **env)

    # ---- (c) ExecStart with a space in the install path ----------------------
    def _systemd_verify(self, unit_text, name):
        if not shutil.which("systemd-analyze"):
            return
        u = self.tmp / "verify" / name
        u.parent.mkdir(exist_ok=True)
        u.write_text(unit_text)
        v = subprocess.run(["systemd-analyze", "verify", "--man=no", str(u)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertNotIn("is not executable", v.stdout + v.stderr)
        self.assertNotIn("Invalid", v.stdout + v.stderr)

    def test_execstart_survives_space_in_path(self):
        d = self.make_install("My Apps")
        r = self.run_install(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        unit = self.unit_file()
        self.assertIn(f'ExecStart="{d}/venv/bin/python3" main.py start', unit)
        self.assertIn(f'ExecStop="{d}/venv/bin/python3" main.py stop', unit)
        self.assertIn(f"WorkingDirectory={d}\n", unit)   # must stay unquoted
        self._systemd_verify(unit, "aerodrome.service")

    def test_demo_feeder_unit_survives_space_in_path(self):
        d = self.make_install("My Apps")
        r = self.run_install(d, "--demo", "--feeder-port", "38080")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        unit = self.unit_file("aerodrome-synthetic-feeder")
        self.assertIn(f'ExecStart="{d}/venv/bin/python3" -m tools.synthetic_feeder.serve', unit)
        self.assertRegex(unit, r"--port\s+38080")  # collector's port-recovery regex still matches
        self.assertNotRegex(unit, r"(?m)^Group=")
        self._systemd_verify(unit, "aerodrome-synthetic-feeder.service")

    def test_path_with_systemd_special_char_refused_before_any_change(self):
        for parent in ("100%real", 'quote"d', "dollar$x", "back\\slash"):
            with self.subTest(parent=parent):
                self.log.write_text("")
                d = self.make_install(parent)
                r = self.run_install(d)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("can't use in a unit file", r.stderr)
                self.assertNotIn("sudo", self.log_text())

    def test_path_systemd_would_strip_or_split_is_refused(self):
        for name in ("aerodrome ", "aerodrome\t", "aero\rdrome", "aero\tdrome"):
            with self.subTest(name=name):
                self.log.write_text("")
                d = self.make_tree(self.tmp / "ws" / name)
                r = self.run_install(d)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("can't use in a unit file", r.stderr)
                self.assertNotIn("sudo", self.log_text())

    # ---- (d) primary group != username -----------------------------------------
    def test_no_group_equals_username(self):
        d = self.make_install("plain")
        r = self.run_install(d, **self.as_root(SUDO_USER="demo-user", FAKE_GROUP="shared-group"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotRegex(self.unit_file(), r"(?m)^Group=")
        self.assertIn("User=demo-user\n", self.unit_file())
        chowns = [l for l in self.log_text().splitlines() if l.startswith("chown ")]
        self.assertTrue(chowns)
        for l in chowns:
            self.assertIn("demo-user:shared-group", l)

    # ---- (b) package manager -----------------------------------------------------
    def test_package_install_failure_is_reported(self):
        d = self.make_install("plain")
        r = self.run_install(d, PY_MISSING=1, APT_INSTALL_RC=100)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Unable to locate package python3-venv", r.stdout + r.stderr)
        self.assertFalse((self.fr / "etc/systemd/system/aerodrome.service").exists())

    def test_refresh_failure_is_a_warning_not_fatal(self):
        d = self.make_install("plain")
        r = self.run_install(d, PY_MISSING=1, APT_UPDATE_RC=100)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("has no Release file", r.stdout)
        self.assertIn("apt-get install", self.log_text())

    def test_rerun_with_prereqs_present_skips_package_manager(self):
        d = self.make_install("plain")
        r = self.run_install(d, APT_UPDATE_RC=100, APT_INSTALL_RC=100)  # a broken repo must not matter
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already present", r.stdout)
        self.assertNotIn("apt-get", self.log_text())

    def test_unknown_distro_with_apt_is_guessed(self):
        osr = self.tmp / "os-release-examplelinux"
        osr.write_text("ID=examplelinux\nPRETTY_NAME=\"Example Linux\"\n")
        d = self.make_install("plain")
        r = self.run_install(d, PY_MISSING=1, AERODROME_BOOTSTRAP_OS_RELEASE=osr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Unrecognised distribution", r.stdout)
        self.assertIn("apt-get install -y -qq python3", self.log_text())

    # ---- (a) sudoers validated, then swapped in -----------------------------------
    def test_sudoers_installed_0440_via_dot_temp_and_rename(self):
        d = self.make_install("plain")
        r = self.run_install(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(oct(self.live.stat().st_mode & 0o777), "0o440")
        self.assertEqual(first_marker(self.live.read_text()), 5)
        self.assertIn("svc-a ALL=(ALL) NOPASSWD:", self.live.read_text())
        log = self.log_text()
        self.assertRegex(log, r"sudo\[valid\] mktemp /etc/sudoers\.d/\.aerodrome\.XXXXXX")
        self.assertRegex(log, r"sudo\[valid\] mv -f /etc/sudoers\.d/\.aerodrome\.\S+ /etc/sudoers\.d/aerodrome")
        self.assertNotIn("tee /etc/sudoers.d/aerodrome", log)       # never written in place
        self.assertNotIn("/tmp/aerodrome-sudoers", log)
        self.assertEqual(self.dot_temps(), [])

    def test_malformed_sudoers_never_goes_live_and_keeps_old_file(self):
        prev = "# SUDOERS_VERSION: 5\nsvc-a ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart aerodrome\n"
        self.put_live(prev)
        bad = INSTALL_SH.read_text().replace(
            "\nSUDOEOF\n", "\n${u} ALL=(ALL) NOPASSWD /usr/bin/true\nSUDOEOF\n", 1)
        d = self.make_install("plain", text=bad)
        r = self.run_install(d, SUDO_MODEL="old")  # sudo < 1.9.3: malformed drop-in = no sudo
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("sudo[MALFORMED]", self.log_text())
        self.assertEqual(self.live.read_text(), prev)
        self.assertIn("NOT changed", r.stdout)
        self.assertIn("syntax error", r.stdout)                     # visudo's own diagnosis shown
        self.assertEqual(self.dot_temps(), [])

    def test_leftover_dot_temp_from_a_killed_run_is_cleaned(self):
        stale = self.fr / "etc" / "sudoers.d" / ".aerodrome.Stale1"
        stale.write_text("half-written")
        d = self.make_install("plain")
        r = self.run_install(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.dot_temps(), [])

    # ---- the service account on a re-run (carrier C3, C1-11) -----------------------
    def test_rerun_by_another_account_without_sudo_is_refused_early(self):
        d = self.make_install("plain")
        self.set_unit(d, "svc-a")
        r = self.run_install(d, FAKE_WHOAMI="admin-b")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("runs as 'svc-a'", r.stderr)
        self.assertNotIn("sudo", self.log_text())
        self.assertFalse((self.fr / "etc/systemd/system/aerodrome.service").exists())
        self.assertNotIn("[1/5]", r.stdout)

    def test_sudo_rerun_keeps_service_account_and_data_owner(self):
        d = self.make_install("plain")
        (d / "config.yaml").write_text('receiver:\n  ip: "192.0.2.10"\n')
        (d / "aircraft_history.db").write_bytes(b"SQLite format 3\x00")
        self.set_unit(d, "svc-a")
        r = self.run_install(d, **self.as_root())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("keeping service account svc-a", r.stdout)
        self.assertIn("User=svc-a\n", self.unit_file())
        live = self.live.read_text()
        self.assertIn("svc-a ALL=", live)
        self.assertNotIn("admin-b", live)
        chowns = [l for l in self.log_text().splitlines() if l.startswith("chown ")]
        self.assertTrue(chowns)
        for l in chowns:
            self.assertIn("svc-a:", l)
            self.assertNotIn("config.yaml", l)
            self.assertNotIn("aircraft_history.db", l)

    def test_sudo_rerun_via_symlinked_path_keeps_account(self):
        d = self.make_install("plain")
        link = self.tmp / "opt-link"
        link.symlink_to(d)
        self.set_unit(d, "svc-a")               # the unit names the real path …
        r = self.run_sh(["bash", str(link / "install.sh")], **self.as_root())   # … run via the link
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("keeping service account svc-a", r.stdout)
        self.assertIn("User=svc-a\n", self.unit_file())
        self.assertNotIn("admin-b", self.live.read_text())

    def test_sudo_rerun_keeps_a_fully_qualified_account(self):
        d = self.make_install("plain")
        self.set_unit(d, "jdoe@corp.example.com")
        r = self.run_install(d, **self.as_root())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("User=jdoe@corp.example.com\n", self.unit_file())
        self.assertIn("jdoe@corp.example.com ALL=(ALL) NOPASSWD:", self.live.read_text())
        self.assertNotIn("admin-b", self.live.read_text())

    def test_unit_account_that_cannot_be_looked_up_stops_the_rerun(self):
        d = self.make_install("plain")
        self.set_unit(d, "ghost-acct")                     # e.g. directory offline
        for env in (self.as_root(), {"FAKE_WHOAMI": "admin-b"}):
            with self.subTest(env=env):
                r = self.run_install(d, **env)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("can't look that account up", r.stderr)
                self.assertNotIn("[1/5]", r.stdout)
                self.assertFalse((self.fr / "etc/systemd/system/aerodrome.service").exists())
                self.assertFalse(self.live.exists())
                self.assertNotIn("sudo", self.log_text())

    def test_unit_for_another_directory_does_not_set_the_account(self):
        d = self.make_install("plain")
        self.set_unit("/nonexistent/other-aerodrome", "svc-a")
        r = self.run_install(d, FAKE_WHOAMI="demo-user")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("User=demo-user\n", self.unit_file())

    # ---- SUDO_USER counts only as root ---------------------------------------------
    def assert_service_account(self, r, account, group):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"Run as user:  {account}\n", r.stdout)
        self.assertIn(f"User={account}\n", self.unit_file())
        live = self.live.read_text()
        self.assertIn(f"{account} ALL=(ALL) NOPASSWD:", live)
        self.assertEqual({l.split()[0] for l in live.splitlines() if "ALL=(" in l}, {account})
        chowns = [l for l in self.log_text().splitlines() if l.startswith("chown ")]
        self.assertTrue(chowns)
        for l in chowns:
            self.assertIn(f" {account}:{group} ", l)

    def test_inherited_sudo_user_is_ignored_when_not_root(self):
        # `sudo -iu demo-user bash bootstrap.sh …` runs install.sh as demo-user
        # with the admin's name still in SUDO_USER: the service is demo-user's.
        for inherited in ("admin-b", "root"):
            with self.subTest(SUDO_USER=inherited):
                self.log.write_text("")
                self.live.unlink(missing_ok=True)
                d = self.make_install(f"fresh-{inherited}")
                r = self.run_install(d, FAKE_WHOAMI="demo-user", SUDO_USER=inherited,
                                     FAKE_GROUP="shared-group")
                self.assert_service_account(r, "demo-user", "shared-group")
                self.assertNotIn("running via sudo", r.stdout)
                self.assertNotIn(inherited, self.live.read_text())

    def test_inherited_sudo_user_is_ignored_with_the_real_id_and_whoami(self):
        # The stubs answer `id -un` and `whoami` from one variable; here both
        # are the system's own, so the pick is the account this test runs as.
        try:
            me = pwd.getpwuid(os.geteuid())
            group = grp.getgrgid(me.pw_gid).gr_name
        except KeyError:
            self.skipTest("this account or its primary group has no name")
        for name in ("id", "whoami"):
            (self.stubs / name).unlink()
            self.assertNotEqual(shutil.which(name, path=self.env()["PATH"]),
                                str(self.stubs / name))
        for inherited in ("admin-b", "root"):
            if inherited == me.pw_name:
                continue
            with self.subTest(SUDO_USER=inherited):
                self.log.write_text("")
                self.live.unlink(missing_ok=True)
                d = self.make_install(f"real-{inherited}")
                r = self.run_install(d, SUDO_USER=inherited)
                self.assert_service_account(r, me.pw_name, group)
                self.assertNotIn("running via sudo", r.stdout)

    def test_service_account_rerun_with_inherited_sudo_user_has_no_sudo_note(self):
        d = self.make_install("plain")
        self.set_unit(d, "svc-a")
        r = self.run_install(d, FAKE_WHOAMI="svc-a", SUDO_USER="admin-b")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("keeping service account svc-a", r.stdout)
        self.assertNotIn("running via sudo", r.stdout)
        self.assertNotIn("admin-b", r.stdout + self.live.read_text())

    def test_sudo_rerun_by_an_admin_is_not_told_no_sudo_works(self):
        # K5: an admin who isn't the service account is refused without sudo,
        # so the note must not suggest './install.sh (no sudo)' to them.
        d = self.make_install("plain")
        self.set_unit(d, "svc-a")
        r = self.run_install(d, **self.as_root())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("will be set to svc-a, the account Aerodrome runs as.", r.stdout)
        self.assertNotIn("no sudo", r.stdout)

    def test_root_run_uses_the_account_that_typed_sudo(self):
        # Guard (unchanged behaviour): `sudo bash install.sh` — fresh, and a
        # re-run by the service account itself — keeps SUDO_USER and the note.
        d = self.make_install("plain")
        r = self.run_install(d, **self.as_root(FAKE_GROUP="shared-group"))
        self.assert_service_account(r, "admin-b", "shared-group")
        self.assertIn("will be set to admin-b. Running as './install.sh' (no sudo) also works",
                      r.stdout)
        self.log.write_text("")
        self.set_unit(d, "admin-b")
        r = self.run_install(d, **self.as_root(FAKE_GROUP="shared-group"))
        self.assert_service_account(r, "admin-b", "shared-group")
        self.assertIn("keeping service account admin-b", r.stdout)
        self.assertIn("(no sudo) also works", r.stdout)

    def test_root_without_sudo_user_is_refused(self):
        # Guard (unchanged behaviour).
        d = self.make_install("plain")
        r = self.run_install(d, FAKE_UID=0, FAKE_WHOAMI="root")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Please run this script as a regular user", r.stdout)
        self.assertNotIn("sudo", self.log_text())

    def test_service_user_flag_refused_in_full_mode(self):
        d = self.make_install("plain")
        for args in (["--service-user", "svc-a"], ["--install-dir", str(d)]):
            with self.subTest(args=args):
                r = self.run_install(d, *args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("only go with --privileges-only", r.stderr)
        self.assertNotIn("sudo", self.log_text())

    # ---- downgrade guard, full mode ------------------------------------------------
    def test_full_mode_keeps_newer_live_rules(self):
        newer = "# SUDOERS_VERSION: 6\nsvc-a ALL=(root) NOPASSWD: /usr/local/libexec/aerodrome/aerodrome-helper\n"
        self.put_live(newer)
        d = self.make_install("plain")
        r = self.run_install(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Kept the newer permission rules (version 6)", r.stdout)
        self.assertEqual(self.live.read_text(), newer)
        self.assertTrue((self.fr / "etc/systemd/system/aerodrome.service").exists())

    def test_full_mode_unreadable_live_rules_are_left_alone(self):
        newer = "# SUDOERS_VERSION: 6\nsvc-a ALL=(root) NOPASSWD: /usr/local/libexec/aerodrome/aerodrome-helper\n"
        self.put_live(newer)
        d = self.make_install("plain")
        r = self.run_install(d, SUDO_REFUSE="cat")         # the read is refused, the writes aren't
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Couldn't read the current permission rules", r.stdout)
        self.assertEqual(self.live.read_text(), newer)
        self.assertEqual(self.dot_temps(), [])

    def test_full_mode_unreadable_but_absent_rules_are_written(self):
        self.live.unlink(missing_ok=True)
        d = self.make_install("plain")
        r = self.run_install(d, SUDO_REFUSE="cat")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(first_marker(self.live.read_text()), 5)

    def test_full_mode_equal_or_absent_live_rules_are_written(self):
        for prev in (None, "# SUDOERS_VERSION: 5\nsvc-a ALL=(ALL) NOPASSWD: /bin/true\n"):
            with self.subTest(prev=prev):
                if prev is None:
                    self.live.unlink(missing_ok=True)
                else:
                    self.put_live(prev)
                d = self.make_install(f"p{0 if prev is None else 1}")
                r = self.run_install(d)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("/usr/bin/systemctl restart aerodrome", self.live.read_text())


@GUARD
@NEEDS
class PrivilegesOnly(_Harness):
    """The carrier's refresh: every command any Updates page has shown must
    refresh the sudoers rule — and nothing else — for the REAL install."""

    LIVE_OLD = "# SUDOERS_VERSION: 4\nsvc-a ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart aerodrome\n"

    def setUp(self):
        super().setUp()
        self.root = self.make_tree(self.tmp / "srv apps" / "aerodrome", version="3.4.144")
        self.unit = self.fr / "etc/systemd/system/aerodrome.service"
        self.unit.write_text(f"[Service]\nUser=svc-a\nWorkingDirectory={self.root}\n")
        self.unit_bytes = self.unit.read_bytes()
        self.set_unit(self.root, "svc-a")
        self.put_live(self.LIVE_OLD)
        self.live_bytes = self.live.read_bytes()

    def stage(self, layout="flat", text=None):
        dst = self.root / "update" if layout == "flat" else self.root / "update" / "aerodrome"
        return self.make_tree(dst, install_text=text)

    def assert_refreshed(self, r, want_marker=5, account="svc-a"):
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("refreshing system permissions only", out)
        live = self.live.read_text()
        self.assertEqual(first_marker(live), want_marker)
        self.assertIn(f"{account} ALL=(ALL) NOPASSWD: /bin/systemctl restart aerodrome", live)
        self.assertNotIn("admin-b", live)              # not whoever typed sudo
        self.assertEqual(oct(self.live.stat().st_mode & 0o777), "0o440")
        self.assertEqual(self.unit.read_bytes(), self.unit_bytes, "a unit was rewritten")
        self.assertEqual(sorted(p.name for p in (self.fr / "etc/systemd/system").iterdir()),
                         ["aerodrome.service"], "a unit was written")
        log = self.log_text()
        for verb in ("apt-get", "dnf ", "pacman", "zypper", "-m venv", "pip ", "chown",
                     "systemctl daemon-reload", "systemctl enable", "systemctl restart",
                     "systemctl start", "systemctl stop", "systemctl disable"):
            self.assertNotIn(verb, log, f"privileges-only refresh ran {verb!r}")
        self.assertEqual(self.dot_temps(), [])

    def assert_untouched(self, r, rc=None):
        if rc is None:
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        self.assertEqual(self.live.read_bytes(), self.live_bytes)
        self.assertEqual(self.unit.read_bytes(), self.unit_bytes)
        self.assertEqual(self.dot_temps(), [])

    # -- the commands Updates pages show (or showed) -----------------------------
    def test_staged_install_sh_flat(self):                # v3.4.44–v3.4.139 GitHub card
        st = self.stage("flat")
        r = self.run_sh(["bash", str(st / "install.sh")], **self.as_root())
        self.assert_refreshed(r)
        self.assertIn("sitting inside an existing install's update/ staging directory", r.stdout)

    def test_staged_install_sh_nested(self):
        st = self.stage("nested")
        self.assert_refreshed(self.run_sh(["bash", str(st / "install.sh")], **self.as_root()))

    def test_b1_env_var_recover_command(self):            # v3.4.140–v3.4.144
        st = self.stage("flat")
        r = self.run_sh(["bash", str(st / "scripts/recover-sudoers.sh")],
                        AERODROME_INSTALL_DIR=self.root, **self.as_root())
        self.assert_refreshed(r)

    def test_recover_with_install_dir_flag(self):          # v3.4.145+ staged command
        for layout in ("flat", "nested"):
            with self.subTest(layout=layout):
                self.put_live(self.LIVE_OLD)
                st = self.stage(layout)
                r = self.run_sh(["bash", str(st / "scripts/recover-sudoers.sh"),
                                 "--install-dir", str(self.root)], **self.as_root())
                self.assert_refreshed(r)
                shutil.rmtree(self.root / "update")

    def test_recover_piped_like_curl_bash(self):
        st = self.stage("flat")
        r = self.run_sh(["bash"], stdin_file=st / "scripts/recover-sudoers.sh",
                        AERODROME_INSTALL_DIR=self.root, **self.as_root())
        self.assert_refreshed(r)

    def test_installed_recover_command(self):              # the live banner's command
        r = self.run_sh(["bash", str(self.root / "scripts/recover-sudoers.sh")], **self.as_root())
        self.assert_refreshed(r)

    def test_install_sh_privileges_only_flag(self):
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"], **self.as_root())
        self.assert_refreshed(r)

    # -- refusals: nothing changes -------------------------------------------------
    def test_not_root_hint_round_trips_through_bash(self):
        shim = self.root / "scripts" / "recover-sudoers.sh"              # path has a space
        r = self.run_sh(["bash", str(shim), "--install-dir", str(self.root)],
                        FAKE_UID=1000, FAKE_WHOAMI="svc-a")
        self.assert_untouched(r, rc=1)
        cmd = r.stderr.split("This must run as root:", 1)[1].strip()
        argv = subprocess.run(["bash", "-c", 'eval "set -- $0"; printf "%s\\0" "$@"', cmd],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        self.assertEqual(argv.split("\0")[:5], ["sudo", "bash", str(shim), "--install-dir", str(self.root)])

    def test_not_root_is_refused(self):
        st = self.stage("flat")
        r = self.run_sh(["bash", str(st / "install.sh")], FAKE_UID=1000, FAKE_WHOAMI="svc-a")
        self.assert_untouched(r, rc=1)
        self.assertIn("sudo bash", r.stderr)
        self.assertNotIn("sudo[", self.log_text())

    def test_foreign_working_directory_is_refused(self):
        self.set_unit("/nonexistent/other-aerodrome", "svc-a")
        st = self.stage("flat")
        r = self.run_sh(["bash", str(st / "install.sh")], **self.as_root())
        self.assert_untouched(r, rc=2)
        self.assertIn("not " + str(self.root.resolve()), r.stderr)

    def test_install_dir_must_match_the_staging_install(self):
        other = self.make_tree(self.tmp / "other" / "aerodrome")
        st = self.stage("flat")
        r = self.run_sh(["bash", str(st / "install.sh"), "--install-dir", str(other)], **self.as_root())
        self.assert_untouched(r, rc=2)
        self.assertIn("is staged for the install at", r.stderr)

    def test_install_dir_compared_canonically(self):
        st = self.stage("flat")
        alias = str(self.root) + "/./"
        r = self.run_sh(["bash", str(st / "install.sh"), "--install-dir", alias], **self.as_root())
        self.assert_refreshed(r)

    def test_not_an_install_is_refused(self):
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only",
                         "--install-dir", str(self.tmp)], **self.as_root())
        self.assert_untouched(r, rc=2)
        self.assertIn("Not an Aerodrome install", r.stderr)

    def test_visudo_rejection_leaves_live_dropin(self):
        st = self.stage("flat")
        r = self.run_sh(["bash", str(st / "install.sh")], VISUDO_FAIL=1, **self.as_root())
        self.assert_untouched(r, rc=1)
        self.assertIn("NOT changed", r.stdout + r.stderr)

    # -- the service account -------------------------------------------------------
    def test_sudo_user_is_ignored(self):
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"],
                        **self.as_root(SUDO_USER="admin-b"))
        self.assert_refreshed(r, account="svc-a")

    def test_symlinked_install(self):
        link = self.tmp / "opt-link"
        link.symlink_to(self.root)
        self.set_unit(link, "svc-a")                       # the unit names the symlink
        st = self.stage("flat")                             # the refresh runs via the real path
        self.assert_refreshed(self.run_sh(["bash", str(st / "install.sh")], **self.as_root()))
        self.put_live(self.LIVE_OLD)
        self.set_unit(self.root, "svc-a")                  # and the other way round
        r = self.run_sh(["bash", str(link / "install.sh"), "--privileges-only"], **self.as_root())
        self.assert_refreshed(r)

    def test_unit_without_user_is_refused(self):
        self.set_unit(self.root, "")
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"], **self.as_root())
        self.assert_untouched(r, rc=2)
        self.assertIn("no non-root User=", r.stderr)

    def test_dotted_and_uppercase_accounts(self):
        for acct in ("svc.dotted", "SvcUpper"):
            with self.subTest(acct=acct):
                self.put_live(self.LIVE_OLD)
                self.set_unit(self.root, acct)
                r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"],
                                **self.as_root())
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"{acct} ALL=(ALL) NOPASSWD:", self.live.read_text())

    def test_account_missing_from_passwd_is_refused(self):
        self.set_unit(self.root, "ghost-acct")
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"], **self.as_root())
        self.assert_untouched(r, rc=2)
        self.assertIn("Can't use account 'ghost-acct'", r.stderr)
        self.assertIn("getent passwd ghost-acct", r.stderr)
        self.assertNotIn("--service-user", r.stderr)      # the unit names the account: no override

    def test_fully_qualified_account(self):
        self.set_unit(self.root, "jdoe@corp.example.com")
        for cmd in (["bash", str(self.root / "install.sh"), "--privileges-only"],
                    ["bash", str(self.root / "scripts/recover-sudoers.sh"), "--install-dir", str(self.root)]):
            with self.subTest(cmd=cmd[1]):
                self.put_live(self.LIVE_OLD)
                self.assert_refreshed(self.run_sh(cmd, **self.as_root()), account="jdoe@corp.example.com")

    def test_override_disagreeing_with_unit_is_refused(self):
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only",
                         "--service-user", "admin-b"], **self.as_root())
        self.assert_untouched(r, rc=2)
        self.assertIn("doesn't match", r.stderr)

    def test_no_unit_root_owned_install_is_refused(self):
        self.unit_show.unlink()
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"],
                        FAKE_OWNER="root", **self.as_root())
        self.assert_untouched(r, rc=2)
        self.assertIn("Can't use account 'root'", r.stderr)
        self.assertIn("Pass --service-user NAME", r.stderr)

    def test_no_unit_uses_install_owner_or_service_user_flag(self):
        self.unit_show.unlink()
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"],
                        FAKE_OWNER="svc-c", **self.as_root())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("svc-c ALL=", self.live.read_text())
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only",
                         "--service-user", "svc-a"], FAKE_OWNER="root", **self.as_root())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("svc-a ALL=", self.live.read_text())

    # -- downgrade guard -----------------------------------------------------------
    def test_newer_live_rules_refused_then_allowed_explicitly(self):
        newer = "# SUDOERS_VERSION: 6\nsvc-a ALL=(root) NOPASSWD: /usr/local/libexec/aerodrome/aerodrome-helper\n"
        self.put_live(newer)
        self.live_bytes = self.live.read_bytes()
        r = self.run_sh(["bash", str(self.root / "scripts/recover-sudoers.sh")], **self.as_root())
        self.assert_untouched(r, rc=3)
        self.assertIn("newer Aerodrome permission rules (version 6)", r.stderr)
        r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only",
                         "--allow-privilege-downgrade"], **self.as_root())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(first_marker(self.live.read_text()), 5)

    def test_downgrade_hint_is_a_command_that_works(self):
        newer = "# SUDOERS_VERSION: 6\nsvc-a ALL=(root) NOPASSWD: /usr/local/libexec/aerodrome/aerodrome-helper\n"
        quoted = self.make_tree(self.tmp / "o'brien apps" / "aerodrome", version="3.4.145")
        for root in (self.root, quoted):
            with self.subTest(root=root.parent.name):
                self.set_unit(root, "svc-a")
                self.put_live(newer)
                r = self.run_sh(["bash", str(root / "scripts/recover-sudoers.sh")], **self.as_root())
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertEqual(self.live.read_text(), newer)
                hint = [l.strip() for l in r.stderr.splitlines() if "--allow-privilege-downgrade" in l]
                self.assertEqual(len(hint), 1, r.stderr)
                self.assertTrue(hint[0].startswith("sudo bash "), hint)
                r2 = self.run_sh(["bash", "-c", hint[0]], **self.as_root())
                self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
                self.assertEqual(first_marker(self.live.read_text()), 5)
                r3 = self.run_sh(["bash", str(root / "scripts/recover-sudoers.sh"),
                                  "--allow-privilege-downgrade"], **self.as_root())
                self.assertEqual(r3.returncode, 2, r3.stdout + r3.stderr)
                self.assertIn("install.sh --privileges-only --allow-privilege-downgrade", r3.stderr)

    def test_unreadable_live_rules_are_never_replaced(self):
        newer = "# SUDOERS_VERSION: 6\nsvc-a ALL=(root) NOPASSWD: /usr/local/libexec/aerodrome/aerodrome-helper\n"
        self.put_live(newer)
        self.live_bytes = self.live.read_bytes()
        r = self.run_sh(["bash", str(self.root / "scripts/recover-sudoers.sh")],
                        SUDO_REFUSE="cat", **self.as_root())
        self.assert_untouched(r, rc=3)
        self.assertIn("Couldn't read the current permission rules", r.stderr)

    def test_equal_or_absent_live_rules_are_refreshed(self):
        for prev in ("# SUDOERS_VERSION: 5\nsvc-a ALL=(ALL) NOPASSWD: /bin/true\n", None):
            with self.subTest(prev=prev):
                if prev is None:
                    self.live.unlink()
                else:
                    self.put_live(prev)
                r = self.run_sh(["bash", str(self.root / "install.sh"), "--privileges-only"],
                                **self.as_root())
                self.assert_refreshed(r)

    # -- one source of rules ---------------------------------------------------------
    def test_exactly_one_marker_value_and_none_in_recover(self):
        text = INSTALL_SH.read_text()
        markers = [int(m.group(1)) for m in map(MARKER.match, text.splitlines()) if m]
        self.assertEqual(markers, [5])
        self.assertFalse([l for l in RECOVER_SH.read_text().splitlines() if MARKER.match(l)])

    def test_rendered_rules_pass_real_visudo_when_available(self):
        visudo = shutil.which("visudo")
        if not visudo:
            self.skipTest("no visudo on this host")
        probe = self.tmp / "probe"
        probe.write_text("svc-a ALL=(ALL) NOPASSWD: /bin/true\n")
        probe.chmod(0o440)
        if subprocess.run([visudo, "-cf", str(probe)], capture_output=True,
                          stdin=subprocess.DEVNULL).returncode != 0:
            self.skipTest("this host's visudo can't check a file unprivileged")
        text = INSTALL_SH.read_text()
        func = re.search(r"^render_sudoers\(\) \{\n.*?^\}\n", text, re.M | re.S).group(0)
        drv = self.tmp / "render.sh"
        drv.write_text(func + 'render_sudoers "$1"\n')
        for acct in ("svc-a", "svc.dotted", "SvcUpper", "jdoe@corp.example.com"):
            with self.subTest(acct=acct):
                out = self.tmp / f"rendered-{acct}"
                with open(out, "w") as fh:
                    subprocess.run(["bash", str(drv), acct], stdout=fh, check=True,
                                   stdin=subprocess.DEVNULL)
                out.chmod(0o440)
                v = subprocess.run([visudo, "-cf", str(out)], capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL)
                self.assertEqual(v.returncode, 0, v.stdout + v.stderr)


ECHO_INSTALL = """# supports --privileges-only
render_sudoers() {{ cat <<X
# SUDOERS_VERSION: {n}
X
}}
echo "RAN {label} (marker {n}) args: $*"
"""
PRE_CARRIER = """# SUDOERS_VERSION: {n}
echo "RAN {label} (pre-carrier full install) args: $*"
"""


@GUARD
@NEEDS
class RecoverHandOff(_Harness):
    """scripts/recover-sudoers.sh picks the install.sh with the highest first
    marker among those that support --privileges-only (B5 r5b cases)."""

    def tree(self, d, n, label, capable=True, real=False):
        if real and capable:
            text = set_marker(INSTALL_SH.read_text(), n)
        elif capable:
            text = ECHO_INSTALL.format(n=n, label=label)
        else:
            text = PRE_CARRIER.format(n=n, label=label)
        return self.make_tree(d, install_text=text)

    def cases(self, real):
        R = self.tmp / ("real" if real else "echo")
        inst = R / "inst"

        def fresh():
            if inst.exists():
                shutil.rmtree(inst)
            self.live.unlink(missing_ok=True)

        out = {}
        fresh(); self.tree(inst, 5, "INSTALLED", real=real); self.tree(inst / "update", 6, "STAGED", real=real)
        out[1] = (self.run_sh(["bash", str(inst / "scripts/recover-sudoers.sh")], **self.as_root()), "STAGED", 6)
        fresh(); self.tree(inst, 6, "INSTALLED", real=real); self.tree(inst / "update", 5, "OLDER-STAGED", real=real)
        out[2] = (self.run_sh(["bash"], stdin_file=inst / "scripts/recover-sudoers.sh",
                              AERODROME_INSTALL_DIR=inst, **self.as_root()), "INSTALLED", 6)
        fresh(); self.tree(inst, 5, "INSTALLED", real=real); self.tree(inst / "update/aerodrome", 6, "NESTED", real=real)
        out[3] = (self.run_sh(["bash", str(inst / "update/aerodrome/scripts/recover-sudoers.sh"),
                               "--install-dir", str(inst)], **self.as_root()), "NESTED", 6)
        fresh(); self.tree(inst, 5, "INSTALLED", real=real); self.tree(inst / "update", 6, "STAGED", real=real)
        out[4] = (self.run_sh(["bash", str(inst / "update/scripts/recover-sudoers.sh")], **self.as_root()), "STAGED", 6)
        fresh(); self.tree(inst, 5, "INSTALLED", real=real); self.tree(inst / "update", 5, "STAGED-SAME", real=real)
        out[5] = (self.run_sh(["bash", str(inst / "scripts/recover-sudoers.sh")], **self.as_root()), "INSTALLED", 5)
        fresh(); self.tree(inst, 5, "PRE-CARRIER", capable=False); self.tree(inst / "update", 6, "STAGED", real=real)
        out[6] = (self.run_sh(["bash", str(inst / "update/scripts/recover-sudoers.sh")],
                              AERODROME_INSTALL_DIR=inst, **self.as_root()), "STAGED", 6)
        fresh(); self.tree(inst, 5, "PRE-CARRIER", capable=False)
        out[7] = (self.run_sh(["bash", str(inst / "scripts/recover-sudoers.sh")], **self.as_root()), None, None)
        return inst, out

    def test_echo_stand_ins(self):
        inst, out = self.cases(real=False)
        real_inst = str(inst.resolve())
        for n, (r, label, marker) in out.items():
            with self.subTest(case=n):
                text = r.stdout + r.stderr
                if label is None:
                    self.assertEqual(r.returncode, 1, text)
                    self.assertIn("Stage the update", text)
                    self.assertNotIn("RAN", text)
                    self.assertNotIn("./install.sh", text)    # an old full installer moves the account
                    continue
                self.assertEqual(r.returncode, 0, text)
                self.assertIn(f"RAN {label} (marker {marker}) args: --privileges-only "
                              f"--install-dir {real_inst}", text)
                self.assertIn(f"rules version {marker}", text)

    def test_real_install_sh(self):
        self.unit_show.write_text("LoadState=not-found\nWorkingDirectory=\nUser=\n")
        inst, out = self.cases(real=True)
        for n, (r, label, marker) in out.items():
            with self.subTest(case=n):
                text = r.stdout + r.stderr
                if label is None:
                    self.assertEqual(r.returncode, 1, text)
                    self.assertIn("Stage the update", text)
                    continue
                self.assertEqual(r.returncode, 0, text)
                self.assertIn("refreshing system permissions only", text)
                self.assertIn(f"rules version {marker}", text)
        # only the last real case's file survives the fresh() resets; its rules
        # came from the staged copy (marker 6), never the pre-carrier full install
        r6 = out[6][0]
        self.assertNotIn("pre-carrier full install", r6.stdout + r6.stderr)


@GUARD
@NEEDS
class ShimTruncation(_Harness):
    """`curl … | sudo bash` of the shim, cut off at any line end or anywhere in
    its last line, runs nothing — never install.sh (which, with its flags
    dropped, would be a FULL install as root)."""

    def test_cut_download_runs_nothing(self):
        inst = self.make_tree(self.tmp / "inst", install_text=ECHO_INSTALL.format(n=5, label="INSTALLED"))
        data = RECOVER_SH.read_bytes()
        body = data.rstrip(b"\n")
        last = body.rfind(b"\n") + 1
        cuts = sorted({i + 1 for i, b in enumerate(data) if b == ord("\n")} | set(range(last, len(body))))
        part = self.tmp / "part.sh"
        for n in cuts:
            if n >= len(body):
                continue
            part.write_bytes(data[:n])
            r = self.run_sh(["bash"], stdin_file=part, AERODROME_INSTALL_DIR=inst, **self.as_root())
            self.assertNotIn("RAN", r.stdout + r.stderr, f"cut at byte {n}")
        part.write_bytes(data)
        r = self.run_sh(["bash"], stdin_file=part, AERODROME_INSTALL_DIR=inst, **self.as_root())
        self.assertIn(f"RAN INSTALLED (marker 5) args: --privileges-only --install-dir {inst.resolve()}",
                      r.stdout + r.stderr)

    def test_piped_from_a_folder_holding_a_file_named_bash(self):
        # piped, BASH_SOURCE inside a function is "bash": it must not be taken
        # for this script's path
        inst = self.make_tree(self.tmp / "inst", install_text=ECHO_INSTALL.format(n=5, label="INSTALLED"))
        cwd = self.tmp / "cwd" / "sub"
        cwd.mkdir(parents=True)
        (cwd / "bash").write_text("#!/bin/sh\n")
        # what a "bash" path would make this script's own tree: a higher marker there would win
        (self.tmp / "cwd" / "install.sh").write_text(ECHO_INSTALL.format(n=9, label="WRONG"))
        with open(RECOVER_SH) as fh:
            r = subprocess.run(["bash"], env=self.env(AERODROME_INSTALL_DIR=inst, **self.as_root()),
                               cwd=str(cwd), stdin=fh, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"RAN INSTALLED (marker 5) args: --privileges-only --install-dir {inst.resolve()}",
                      r.stdout)
        self.assertNotIn("WRONG", r.stdout + r.stderr)


@GUARD
@NEEDS
class KillPoints(_Harness):
    """A run killed at any write step never leaves a partial drop-in; the same
    command then converges — including when the re-run is killed too."""

    STEPS = ("mktemp", "tee", "chmod", "visudo", "mv")
    OLD = "# SUDOERS_VERSION: 4\nsvc-a ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart aerodrome\n"

    def setUp(self):
        super().setUp()
        self.root = self.make_tree(self.tmp / "srv apps" / "aerodrome", version="3.4.144")
        self.set_unit(self.root, "svc-a")
        self.staged = self.make_tree(self.root / "update")
        self.cmd = ["bash", str(self.staged / "scripts/recover-sudoers.sh"),
                    "--install-dir", str(self.root)]

    def run_cmd(self, kill_at=None):
        counter = self.tmp / "kill-counter"
        counter.unlink(missing_ok=True)
        kw = self.as_root()
        if kill_at:
            kw.update(KILL_AT=kill_at, KILL_N=1)
        return self.run_sh(self.cmd, **kw)

    def final_rules(self):
        self.put_live(self.OLD)
        r = self.run_cmd()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self.live.read_text()

    def test_kill_then_rerun(self):
        final = self.final_rules()
        self.assertEqual(first_marker(final), 5)
        for k1 in self.STEPS:
            for k2 in (None,) + self.STEPS:
                with self.subTest(first_killed=k1, rerun_killed=k2):
                    self.put_live(self.OLD)
                    r1 = self.run_cmd(k1)
                    self.assertEqual(r1.returncode, -9, r1.stdout + r1.stderr)
                    self.assertIn(self.live.read_text(), (self.OLD, final))
                    if k2:
                        r2 = self.run_cmd(k2)
                        self.assertEqual(r2.returncode, -9, r2.stdout + r2.stderr)
                        self.assertIn(self.live.read_text(), (self.OLD, final))
                    r3 = self.run_cmd()
                    self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
                    self.assertEqual(self.live.read_text(), final)
                    self.assertEqual(self.dot_temps(), [])


@NEEDS
class InstallShStatic(unittest.TestCase):
    def test_bash_syntax(self):
        for f in (INSTALL_SH, RECOVER_SH):
            with self.subTest(f=f.name):
                r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_staging_guard_text_kept(self):
        # Pre-carrier installs grep nothing; this exact text is what users of
        # older Updates pages see when they run the staged install.sh.
        self.assertIn("sitting inside an existing install's update/ staging directory",
                      INSTALL_SH.read_text())


if __name__ == "__main__":
    unittest.main()
