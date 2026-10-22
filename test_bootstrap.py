"""scripts/bootstrap.sh (the curl installer) — no root, no network, nothing
outside a temp dir.

The WORKING tree's bootstrap.sh runs in a sandbox: PATH is ONLY a dir of
log-only stubs (sudo, systemctl, apt-get, dnf, pacman, zypper, firewall-cmd,
ip, id) plus a curated dir of symlinks to real tools (bash, coreutils, sed,
awk, grep, unzip, python3, find, …). HOME and TMPDIR live in the sandbox and
the release comes from a fixture zip (--from-zip), so nothing is downloaded.
The read-only hooks AERODROME_BOOTSTRAP_OS_RELEASE / AERODROME_BOOTSTRAP_UNIT
stand in for /etc/os-release and the installed unit, and
AERODROME_BOOTSTRAP_SOURCE_ONLY=1 defines the functions without running main.

Covers #37 (never delete or take over a folder, a pipeline that can't fail at
random under pipefail, unknown distros), the L10 bootstrap half (temp dir
always removed, a failed move stops the install), the related R1–R4 fixes,
the firewall marker uninstall.sh reads, the production entry points
(`bash <(…)`, a truncated download), an account whose primary group has no
name, and the latest-release lookup (a GitHub rate limit is explained; no
answer keeps the network advice; a stub curl stands in for GitHub).

Skipped when AERODROME_SKIP_SLOW_TESTS=1, without bash/unzip, or as root.
Names, paths and addresses are invented (RFC 5737).
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
SKIP_SLOW = os.environ.get("AERODROME_SKIP_SLOW_TESTS") == "1"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
BASH = shutil.which("bash")
GUARD = unittest.skipIf(SKIP_SLOW, "AERODROME_SKIP_SLOW_TESTS=1 (subprocess installer suite)")
NEEDS = unittest.skipUnless(BASH and shutil.which("unzip") and not IS_ROOT,
                            "needs bash and unzip, and not root")

# Real tools the bootstrap may run, symlinked into the sandbox's only bin dir.
CURATED = ("bash", "cat", "cp", "mv", "rm", "mkdir", "ls", "dirname", "basename", "tr",
           "cut", "du", "head", "tail", "chmod", "env", "sed", "awk", "grep", "unzip",
           "find", "realpath", "mktemp", "uname", "stat", "sort", "test", "touch",
           "sha256sum", "wc", "curl", "readlink", "date")

LOG = '#!/bin/bash\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'
STUBS = {
    # log-only: never escalates; runs the (stubbed) package manager /
    # firewall / systemctl, or mkdir inside the sandbox; chown is a no-op
    "sudo": '#!/bin/bash\necho "sudo $*" >> "$SUDO_LOG"\n'
            'case "$1" in chown) exit 0 ;; esac\nexec "$@"\n',
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$STUB_LOG"\n'
                 'case "$1" in\n'
                 '  list-unit-files) [ -n "${UNIT_LISTED:-}" ] && echo "aerodrome.service enabled enabled"; exit 0 ;;\n'
                 '  is-active) [ "${FIREWALLD_ACTIVE:-0}" = 1 ] && [ "$2" = firewalld ] && exit 0; exit 3 ;;\n'
                 'esac\nexit 0\n',
    "apt-get": LOG, "dnf": LOG, "pacman": LOG, "zypper": LOG,
    "firewall-cmd": '#!/bin/bash\necho "firewall-cmd $*" >> "$STUB_LOG"\n'
                    'case "$1" in --list-ports) echo "${FW_PORTS:-22/tcp}" ;; esac\nexit 0\n',
    "ip": '#!/bin/sh\nexit "${IP_RC:-2}"\n',
    "id": '#!/bin/sh\ncase "$1" in -u) echo 1000 ;; -g) echo 1001 ;; -un) echo demo-user ;; -gn) echo demo-group ;;'
          ' *) exit 1 ;; esac\n',
}

INSTALL_STUB = '#!/bin/bash\necho "stub install.sh ran: $*"\nexit 0\n'
CONFIG_EXAMPLE = """receiver:
  ip: "192.0.2.1"
  port: 8080
  latitude: null
  longitude: null
  distance_unit: "mi"
stats:
  timezone: ""
demo:
  enabled: false
"""


def build_zip(path, version="9.9.9"):
    """A minimal release zip with the usual aerodrome-vX.Y.Z/ wrapper, dotfiles
    and a dot-directory, and a stub install.sh."""
    w = f"aerodrome-v{version}/"
    with zipfile.ZipFile(path, "w") as z:
        def add(name, body, mode=0o644):
            info = zipfile.ZipInfo(w + name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(info, body)
        d = zipfile.ZipInfo(w)
        d.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10    # a normal dir entry
        z.writestr(d, "")
        add("VERSION", version + "\n")
        add("main.py", "# main\n")
        add("install.sh", INSTALL_STUB, 0o755)
        add("config.yaml.example", CONFIG_EXAMPLE)
        add(".gitignore", "config.yaml\n")
        add(".github/workflows/example.yml", "name: example\n")
        add("scripts/example.sh", "#!/bin/sh\n", 0o755)
    return path


class _Sandbox(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aerodrome-boot-test-"))
        self.addCleanup(self._cleanup)
        self.home = self.tmp / "home" / "demo-user"
        self.home.mkdir(parents=True)
        self.sentinel = self.home / "keep-me.txt"
        self.sentinel.write_text("user data\n")
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        for name, body in STUBS.items():
            p = self.stubs / name
            p.write_text(body)
            p.chmod(0o755)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in CURATED:
            real = shutil.which(tool)
            if real:
                (self.bin / tool).symlink_to(real)
        (self.bin / "python3").symlink_to(os.path.realpath(sys.executable))
        self.t = self.tmp / "t"
        self.t.mkdir()
        self.sudo_log = self.tmp / "sudo.log"
        self.stub_log = self.tmp / "stub.log"
        self.sudo_log.write_text("")
        self.stub_log.write_text("")
        self.os_release = self.tmp / "os-release"
        self.os_release.write_text('ID=debian\nPRETTY_NAME="Example Debian"\n')
        self.zip = build_zip(self.tmp / "aerodrome-v9.9.9.zip")
        self.assertEqual(shutil.which("sudo", path=self.env()["PATH"]), str(self.stubs / "sudo"))

    def _cleanup(self):
        for p in self.tmp.rglob("*"):          # undo chmod 555 fixtures
            if p.is_dir() and not p.is_symlink():
                try:
                    p.chmod(0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, True)

    def env(self, **kw):
        e = {"PATH": f"{self.stubs}:{self.bin}", "HOME": str(self.home), "TMPDIR": str(self.t),
             "TERM": "dumb", "LC_ALL": "C.UTF-8", "SUDO_LOG": str(self.sudo_log),
             "STUB_LOG": str(self.stub_log),
             "AERODROME_BOOTSTRAP_OS_RELEASE": str(self.os_release),
             "AERODROME_BOOTSTRAP_UNIT": str(self.tmp / "no-such-unit.service")}
        e.update({k: str(v) for k, v in kw.items()})
        return e

    def run_boot(self, *args, script=None, **kw):
        r = subprocess.run([BASH, str(script or BOOTSTRAP), *args], env=self.env(**kw),
                           cwd=str(self.home), capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def assertNothingDone(self, out):
        self.assertEqual(self.sudo_log.read_text(), "", out)
        self.assertEqual(self.sentinel.read_text(), "user data\n")
        self.assertNotIn("[4/7]", out)

    def bootstrap_temps(self):
        return sorted(p.name for p in self.t.iterdir() if p.name.startswith("aerodrome-bootstrap."))


@GUARD
@NEEDS
class PrefixTable(_Sandbox):
    """validate_prefix() on its own (source-only): system and home folders
    and systemd-hostile characters are refused, normal places accepted."""

    DRIVER = r'''#!/bin/bash
export AERODROME_BOOTSTRAP_SOURCE_ONLY=1
. "$1"
set +euo pipefail
while IFS= read -r -d '' arg && IFS= read -r -d '' cwd; do
    out="$(cd "$cwd" && validate_prefix "$arg" 2>&1 && printf 'PREFIX=%s' "$PREFIX")"
    printf '%s\t%s\0' "$?" "$out"
done < "$2"
'''

    def test_table(self):
        fh = self.home
        (self.tmp / "data").mkdir()
        (self.tmp / "link-to-home").symlink_to(fh)
        (self.tmp / "link-to-root").symlink_to("/")
        S = str(self.tmp)
        cases = [  # (expect, prefix, cwd, normal form when accepted)
            ("refuse", "~", S, None), ("refuse", f"{fh}/", S, None), ("refuse", f"{fh}/.", S, None),
            ("refuse", f"{fh}/sub/..", S, None), ("refuse", "", S, None), ("refuse", ".", str(fh), None),
            ("refuse", "/", S, None), ("refuse", "//", S, None), ("refuse", "/.", S, None),
            ("refuse", "/opt", S, None), ("refuse", "/opt/", S, None), ("refuse", "/usr", S, None),
            ("refuse", "/usr/local", S, None), ("refuse", "/usr/local/bin", S, None),
            ("refuse", "/usr/lib", S, None), ("refuse", "/var/lib", S, None), ("refuse", "/home", S, None),
            ("refuse", str(fh.parent), S, None), ("refuse", "/etc/aerodrome", S, None),
            ("refuse", "/srv", S, None), ("refuse", "/mnt", S, None), ("refuse", "/tmp", S, None),
            ("refuse", f"{S}/link-to-home", S, None), ("refuse", f"{S}/link-to-root", S, None),
            ("refuse", f"{S}/link-to-root/opt", S, None),
            ("refuse", "/opt/a%b", S, None), ('refuse', '/opt/a"b', S, None),
            ("refuse", "/opt/a$b", S, None), ("refuse", "/opt/a\\b", S, None),
            ("refuse", "/opt/a\nb", S, None),
            # systemd strips a trailing space/tab and can't carry a control character
            ("refuse", "/opt/aerodrome ", S, None), ("refuse", "/opt/aerodrome\t", S, None),
            ("refuse", "/opt/aero\rdrome", S, None), ("refuse", "/opt/aero\tdrome", S, None),
            ("accept", "/opt/aerodrome", S, "/opt/aerodrome"),
            ("accept", "/opt/aerodrome/", S, "/opt/aerodrome"),
            ("accept", "/srv/aerodrome", S, "/srv/aerodrome"),
            ("accept", "/var/lib/aerodrome", S, "/var/lib/aerodrome"),
            ("accept", "/usr/local/aerodrome", S, "/usr/local/aerodrome"),
            ("accept", "/tmp/ad", S, "/tmp/ad"),
            ("accept", "~/aerodrome", S, f"{fh}/aerodrome"),
            ("accept", "aerodrome", str(fh), f"{fh}/aerodrome"),
            ("accept", f"{S}/data/aerodrome", S, f"{S}/data/aerodrome"),
            ("accept", "/opt/aero drome", S, "/opt/aero drome"),
            ("accept", "/opt/aero /x", S, "/opt/aero /x"),
        ]
        drv = self.tmp / "drv.sh"
        drv.write_text(self.DRIVER)
        inp = self.tmp / "cases.bin"
        inp.write_bytes(b"".join(a.encode() + b"\0" + c.encode() + b"\0" for _, a, c, _ in cases))
        r = subprocess.run([BASH, str(drv), str(BOOTSTRAP), str(inp)], env=self.env(),
                           capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        rows = [x.decode() for x in r.stdout.split(b"\0") if x]
        self.assertEqual(len(rows), len(cases), r.stderr.decode())
        for (want, arg, _cwd, norm), row in zip(cases, rows):
            rc, out = row.split("\t", 1)
            with self.subTest(prefix=arg):
                if want == "refuse":
                    self.assertNotEqual(rc, "0", out)
                    self.assertIn("Refusing", out)
                else:
                    self.assertEqual(rc, "0", out)
                    self.assertTrue(out.endswith(f"PREFIX={norm}"), out)
                    if arg.startswith("/opt/") or arg.startswith("~"):
                        pass
        self.assertIn("installs directly INTO the prefix", "\n".join(rows))


@GUARD
@NEEDS
class ExistingInstallDetection(_Sandbox):
    """aerodrome_unit_registered: capture-then-match, never `| grep -q`."""

    SYSTEMCTL = r'''#!/usr/bin/env python3
# ROWS rows, NAMEW-wide name column, DELAY_US between 4 KiB writes; SIGPIPE
# kills it like a C program — what made `systemctl … | grep -q` lose the race.
import os, signal, sys, time
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
if sys.argv[1:2] != ["list-unit-files"]:
    sys.exit(0)
n = int(os.environ.get("ROWS", "620")); w = int(os.environ.get("NAMEW", "50"))
delay = int(os.environ.get("DELAY_US", "0")) / 1e6
fmt = lambda a, b, c: f"{a:<{w}} {b:<15} {c}"
rows = [fmt("UNIT FILE", "STATE", "PRESET"), fmt("-.mount", "generated", "-"),
        fmt("accounts-daemon.service", "enabled", "enabled"), fmt("aerodrome.service", "enabled", "enabled")]
rows += [fmt(f"zz-example-unit-{i:04d}.service", "disabled", "enabled") for i in range(n)]
rows += ["", f"{len(rows) - 1} unit files listed."]
data = ("\n".join(rows) + "\n").encode()
for off in range(0, len(data), 4096):
    if off and delay:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < delay:
            pass
    os.write(1, data[off:off + 4096])
'''
    DRIVER = r'''#!/bin/bash
export AERODROME_BOOTSTRAP_SOURCE_ONLY=1
. "$1"
set +euo pipefail
hits=0
for ((i = 0; i < $2; i++)); do aerodrome_unit_registered && hits=$((hits + 1)); done
echo "hits=$hits"
echo "wd=[$(unit_working_dir)]"
'''

    def run_driver(self, n, path, unit):
        drv = self.tmp / "drv.sh"
        drv.write_text(self.DRIVER)
        r = subprocess.run([BASH, str(drv), str(BOOTSTRAP), str(n)],
                           env=self.env(PATH=path, AERODROME_BOOTSTRAP_UNIT=unit,
                                        ROWS=1200, NAMEW=70, DELAY_US=80),
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        return r.stdout + r.stderr

    def test_big_slow_listing_is_always_detected(self):
        d = self.tmp / "slowstub"
        d.mkdir()
        (d / "systemctl").write_text(self.SYSTEMCTL)
        (d / "systemctl").chmod(0o755)
        out = self.run_driver(50, f"{d}:/usr/bin:/bin", self.tmp / "missing.service")
        self.assertIn("hits=50", out)

    def test_unit_file_without_systemctl(self):
        unit = self.tmp / "with-wd.service"
        unit.write_text("[Service]\nWorkingDirectory=/opt/example-dir\n")
        out = self.run_driver(1, str(self.bin), unit)
        self.assertIn("hits=1", out)
        self.assertIn("wd=[/opt/example-dir]", out)

    def test_unit_without_working_directory(self):
        unit = self.tmp / "no-wd.service"
        unit.write_text("[Service]\nExecStart=/bin/true\n")
        out = self.run_driver(1, str(self.bin), unit)
        self.assertIn("hits=1", out)
        self.assertIn("wd=[]", out)


@GUARD
@NEEDS
class Refusals(_Sandbox):
    """End to end: refused before anything is downloaded, created or deleted."""

    def test_system_and_home_prefixes(self):
        for prefix in (str(self.home), f"{self.home}/", "/usr/local", "/opt", "/", ""):
            with self.subTest(prefix=prefix):
                rc, out = self.run_boot("--yes", "--from-zip", "/nonexistent", "--prefix", prefix)
                self.assertEqual(rc, 1, out)
                self.assertIn("Refusing", out)
                self.assertNotIn("[1/7]", out)
                self.assertNothingDone(out)

    def test_value_flags_need_a_value(self):
        for args in (["--prefix"], ["--prefix", "--yes"], ["--from-zip"]):
            with self.subTest(args=args):
                rc, out = self.run_boot(*args)
                self.assertEqual(rc, 1, out)
                self.assertIn("needs a value", out)
                self.assertNothingDone(out)

    def test_yes_without_receiver_is_refused_not_a_hang(self):
        rc, out = self.run_boot("--yes", "--from-zip", str(self.zip),
                                "--prefix", str(self.home / "aerodrome"))
        self.assertEqual(rc, 1, out)
        self.assertIn("--receiver-ip", out)
        self.assertNothingDone(out)

    def test_non_empty_folder_is_never_deleted(self):
        d = self.home / "projects"
        d.mkdir()
        (d / "notes.txt").write_text("mine\n")
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", "/nonexistent",
                                "--prefix", str(d))
        self.assertEqual(rc, 1, out)
        self.assertIn("never deletes or takes over", out)
        self.assertNotIn("Overwrite", out)
        self.assertEqual((d / "notes.txt").read_text(), "mine\n")
        self.assertNothingDone(out)

    def test_existing_folder_not_ours_is_not_taken_over(self):
        d = self.tmp / "shared-empty"
        d.mkdir()
        d.chmod(0o555)
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", "/nonexistent",
                                "--prefix", str(d))
        self.assertEqual(rc, 1, out)
        self.assertIn("won't take over", out)
        self.assertIn(f"sudo chown 1000:1001 {d}", out)
        self.assertNothingDone(out)

    def test_own_unwritable_folder_gets_a_chmod_hint(self):
        real_stat = shutil.which("stat")
        (self.stubs / "stat").write_text(
            f'#!/bin/bash\n[ "$1 $2" = "-c %U" ] && {{ echo demo-user; exit 0; }}\nexec {real_stat} "$@"\n')
        (self.stubs / "stat").chmod(0o755)
        d = self.tmp / "mine-readonly"
        d.mkdir()
        d.chmod(0o555)
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", "/nonexistent",
                                "--prefix", str(d))
        self.assertEqual(rc, 1, out)
        self.assertIn(f"chmod u+wx {d}", out)
        self.assertNotIn("sudo chown", out)
        self.assertNothingDone(out)

    def test_folder_that_cannot_be_listed_is_not_taken_for_empty(self):
        d = self.tmp / "write-only"
        d.mkdir()
        (d / "config.yaml").write_text("mine: true\n")
        (d / ".gitignore").write_text("mine\n")
        d.chmod(0o333)                     # writable and searchable, not listable
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(self.zip),
                                "--prefix", str(d))
        d.chmod(0o755)
        self.assertEqual(rc, 1, out)
        self.assertIn("can't be listed", out)
        self.assertEqual((d / "config.yaml").read_text(), "mine: true\n")
        self.assertEqual((d / ".gitignore").read_text(), "mine\n")
        self.assertFalse((d / "VERSION").exists())
        self.assertNothingDone(out)

    def piped(self, *args):
        r = subprocess.run([BASH, "-s", "--", *args], env=self.env(), cwd=str(self.home),
                           input=BOOTSTRAP.read_text(), capture_output=True, text=True,
                           timeout=120, start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def test_piped_without_yes_stops_before_anything(self):
        prefix = self.home / "aerodrome"
        rc, out = self.piped("--from-zip", str(self.zip), "--prefix", str(prefix))
        self.assertEqual(rc, 1, out)
        self.assertIn("No terminal to answer the setup questions", out)
        self.assertNotIn("[1/7]", out)
        self.assertNothingDone(out)
        self.assertFalse(prefix.exists())
        self.assertEqual(self.bootstrap_temps(), [])

    def test_piped_with_yes_and_receiver_completes(self):
        prefix = self.home / "aerodrome"
        rc, out = self.piped("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(self.zip),
                             "--prefix", str(prefix))
        self.assertEqual(rc, 0, out)
        self.assertIn("Bootstrap complete!", out)
        self.assertIn('ip: "192.0.2.10"', (prefix / "config.yaml").read_text())

    def test_existing_aerodrome_tree_points_at_install_sh(self):
        d = self.home / "aerodrome"
        d.mkdir()
        (d / "VERSION").write_text("3.4.144\n")
        (d / "main.py").write_text("")
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(self.zip),
                                "--prefix", str(d))
        self.assertEqual(rc, 1, out)
        self.assertIn("already at", out)
        self.assertIn("./install.sh", out)
        self.assertNotIn("no systemd unit is registered", out)
        self.assertNothingDone(out)

    def test_registered_unit_means_already_installed(self):
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(self.zip),
                                "--prefix", str(self.home / "aerodrome"), UNIT_LISTED=1)
        self.assertEqual(rc, 1, out)
        self.assertIn("already installed", out)
        self.assertNothingDone(out)


@GUARD
@NEEDS
class ProductionEntry(_Sandbox):
    """`bash <(curl …)` runs the script from a pipe; a cut-off download must
    never run a partial script."""

    def via_procsub(self, script, *args):
        r = subprocess.run([BASH, "-c", 'bash <(cat "$0") "$@"', str(script), *args],
                           env=self.env(), cwd=str(self.home), capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL, start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def test_help_via_process_substitution(self):
        rc, out = self.via_procsub(BOOTSTRAP, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("Headline command:", out)
        self.assertIn("--prefix <path>", out)

    def test_refusal_via_process_substitution(self):
        rc, out = self.via_procsub(BOOTSTRAP, "--yes", "--prefix", "/")
        self.assertEqual(rc, 1, out)
        self.assertIn("Refusing", out)
        self.assertNothingDone(out)

    def test_every_cut_of_the_last_line_runs_nothing(self):
        data = BOOTSTRAP.read_bytes()
        start = data.rstrip(b"\n").rfind(b"\n") + 1           # first byte of the last line
        self.assertIn(b"main", data[start:])
        rc, out = self.via_procsub(BOOTSTRAP, "--yes", "--prefix", "/")
        self.assertIn("Refusing", out)                       # the whole script refuses
        for n in range(start, len(data.rstrip(b"\n"))):    # every strict prefix of the line
            with self.subTest(cut=n - start):
                part = self.tmp / "cut.sh"
                part.write_bytes(data[:n])
                rc, out = self.via_procsub(part, "--yes", "--prefix", "/")
                self.assertNotIn("[1/7]", out)
                self.assertNotIn("Refusing", out)            # main never ran, flags or not
                self.assertNothingDone(out)

    def test_truncated_download_runs_nothing(self):
        data = BOOTSTRAP.read_bytes()
        for frac in (0.25, 0.6, 0.9, 0.99):
            with self.subTest(fraction=frac):
                part = self.tmp / f"truncated-{frac}.sh"
                part.write_bytes(data[: int(len(data) * frac)])
                rc, out = self.via_procsub(part, "--yes", "--receiver-ip", "192.0.2.10",
                                           "--from-zip", str(self.zip),
                                           "--prefix", str(self.home / "aerodrome"))
                self.assertNotIn("[1/7]", out)
                self.assertNothingDone(out)
                self.assertFalse((self.home / "aerodrome").exists())


@GUARD
@NEEDS
class UnknownDistro(_Sandbox):

    def setUp(self):
        super().setUp()
        self.os_release.write_text('ID=examplelinux\nPRETTY_NAME="Example Linux"\n')

    def test_no_systemd_or_package_manager_is_refused_cleanly(self):
        for stub in ("systemctl", "apt-get", "dnf", "pacman", "zypper"):
            (self.stubs / stub).unlink()
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", "/nonexistent",
                                "--prefix", str(self.home / "aerodrome"))
        self.assertEqual(rc, 1, out)
        self.assertIn("ID=examplelinux", out)
        self.assertNotIn("unbound variable", out)
        self.assertNothingDone(out)

    def test_force_with_apt_installs_missing_prereqs(self):
        (self.bin / "unzip").unlink()
        rc, out = self.run_boot("--yes", "--force", "--receiver-ip", "192.0.2.10",
                                "--from-zip", "/nonexistent", "--prefix", str(self.home / "aerodrome"))
        self.assertIn("continuing per --force", out)
        self.assertNotIn("unbound variable", out)
        self.assertRegex(self.sudo_log.read_text(), r"sudo apt-get install -y -qq .*\bunzip\b")
        self.assertEqual(rc, 1, out)           # then stops at the missing --from-zip
        self.assertIn("--from-zip path not readable", out)


@GUARD
@NEEDS
class InstallFlow(_Sandbox):
    """The L10 bootstrap half and the happy path (also locks R3)."""

    def boot(self, prefix=None, **kw):
        return self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(self.zip),
                             "--prefix", str(prefix or self.home / "aerodrome"), **kw)

    def test_happy_path_from_zip(self):
        rc, out = self.boot()               # ip stub exits 2; no hostname: R3
        self.assertEqual(rc, 0, out)
        self.assertIn("Bootstrap complete!", out)
        self.assertIn("stub install.sh ran", out)
        p = self.home / "aerodrome"
        for rel in ("VERSION", "main.py", "install.sh", ".gitignore",
                    ".github/workflows/example.yml", "config.yaml"):
            self.assertTrue((p / rel).is_file(), rel)
        self.assertIn('ip: "192.0.2.10"', (p / "config.yaml").read_text())
        self.assertEqual(self.bootstrap_temps(), [])
        self.assertTrue(self.zip.exists())      # unzipped in place, never consumed
        self.assertFalse((p / ".aerodrome-firewalld-port").exists())

    def test_firewalld_port_opened_is_recorded(self):
        rc, out = self.boot(FIREWALLD_ACTIVE=1)
        self.assertEqual(rc, 0, out)
        log = self.sudo_log.read_text()
        self.assertIn("sudo firewall-cmd --add-port=8000/tcp --permanent", log)
        self.assertEqual((self.home / "aerodrome" / ".aerodrome-firewalld-port").read_text(), "8000/tcp\n")

    def test_port_already_open_is_not_recorded(self):
        rc, out = self.boot(FIREWALLD_ACTIVE=1, FW_PORTS="22/tcp 8000/tcp")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("--add-port", self.sudo_log.read_text())
        self.assertFalse((self.home / "aerodrome" / ".aerodrome-firewalld-port").exists())

    def test_pre_existing_empty_folder_is_used_as_is(self):
        d = self.tmp / "mine-empty"
        d.mkdir()
        rc, out = self.boot(prefix=d)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("chown", self.sudo_log.read_text())
        self.assertNotIn("mkdir", self.sudo_log.read_text())
        self.assertTrue((d / "VERSION").is_file())

    def test_new_folder_outside_home_is_created_with_sudo_and_chowned(self):
        d = self.tmp / "srv" / "aerodrome"
        rc, out = self.boot(prefix=d)
        self.assertEqual(rc, 0, out)
        log = self.sudo_log.read_text()
        self.assertIn(f"sudo mkdir -- {d}", log)               # never -p on the prefix itself
        self.assertIn(f"sudo chown 1000:1001 {d}", log)

    def race_stub(self, prefix):
        """unzip is missing; the package-manager stub creates the prefix (as a
        second run or another user would) while the bootstrap is installing it."""
        real_unzip = os.path.realpath(shutil.which("unzip"))
        (self.bin / "unzip").unlink()
        (self.stubs / "apt-get").write_text(
            '#!/bin/bash\necho "apt-get $*" >> "$STUB_LOG"\n'
            f'mkdir -p "{prefix}" && echo theirs > "{prefix}/foreign.txt"\n'
            f'{shutil.which("ln")} -sf "{real_unzip}" "{self.bin}/unzip"\nexit 0\n')

    def test_prefix_created_meanwhile_is_not_taken_over(self):
        for prefix in (self.tmp / "srv" / "aerodrome", self.home / "aerodrome"):
            with self.subTest(prefix=str(prefix)):
                self.sudo_log.write_text("")
                self.race_stub(prefix)
                rc, out = self.boot(prefix=prefix)
                self.assertEqual(rc, 1, out)
                self.assertIn("appeared while this ran", out)
                self.assertNotIn("chown", self.sudo_log.read_text())
                self.assertEqual(sorted(p.name for p in prefix.iterdir()), ["foreign.txt"])
                self.assertEqual((prefix / "foreign.txt").read_text(), "theirs\n")
                self.assertNotIn("rm -rf", out)
                self.assertEqual(self.bootstrap_temps(), [])

    def test_failed_install_sh_points_at_uninstall_then_rerun_explains_leftovers(self):
        unit = self.tmp / "fake-unit" / "aerodrome.service"
        unit.parent.mkdir()
        bad = self.tmp / "half.zip"
        with zipfile.ZipFile(self.zip) as src, zipfile.ZipFile(bad, "w") as dst:
            for info in src.infolist():
                body = src.read(info)
                if info.filename.endswith("/install.sh"):   # registers the service, then fails
                    body = (f'#!/bin/bash\nprintf "[Service]\\nWorkingDirectory=%s\\n" "$PWD" > "{unit}"\n'
                            'echo "Failed to start" >&2\nexit 1\n').encode()
                dst.writestr(info, body)
            dst.writestr("aerodrome-v9.9.9/uninstall.sh", "#!/bin/bash\n")
        prefix = self.home / "aerodrome"
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(bad),
                                "--prefix", str(prefix), AERODROME_BOOTSTRAP_UNIT=unit)
        self.assertNotEqual(rc, 0, out)
        self.assertIn(f"bash {prefix}/uninstall.sh --keep", out)
        self.assertLess(out.index("uninstall.sh --keep"), out.index(f"rm -rf {prefix}"))
        shutil.rmtree(prefix)                              # the user deletes only the folder
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(bad),
                                "--prefix", str(prefix), AERODROME_BOOTSTRAP_UNIT=unit)
        self.assertEqual(rc, 1, out)
        self.assertIn("no longer exists", out)
        self.assertIn("sudo systemctl disable --now aerodrome", out)
        self.assertIn("sudo rm -f /etc/systemd/system/aerodrome.service /etc/sudoers.d/aerodrome", out)
        self.assertNotIn("in the existing install directory", out)

    def test_failed_move_stops_and_cleans_temp(self):
        (self.bin / "mv").unlink()
        (self.bin / "mv").write_text("#!/bin/sh\necho 'mv: write error: No space left on device' >&2\nexit 1\n")
        (self.bin / "mv").chmod(0o755)
        rc, out = self.boot()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("Could not move", out)
        self.assertNotIn("Bootstrap complete", out)
        self.assertNotIn("stub install.sh ran", out)
        self.assertIn("leaving partial tree", out)
        self.assertEqual(self.bootstrap_temps(), [])

    def test_failed_install_in_pre_existing_folder_says_so_without_rm(self):
        d = self.tmp / "mine-empty"
        d.mkdir()
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(self.zip) as src, zipfile.ZipFile(bad, "w") as dst:
            for info in src.infolist():
                body = src.read(info)
                if info.filename.endswith("/install.sh"):
                    body = b"#!/bin/bash\necho stub install failed >&2\nexit 7\n"
                dst.writestr(info, body)
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(bad),
                                "--prefix", str(d))
        self.assertNotEqual(rc, 0, out)
        self.assertIn("existed before this run", out)
        self.assertNotIn("rm -rf", out)
        self.assertEqual(self.bootstrap_temps(), [])


@GUARD
@NEEDS
class GrouplessAccount(_Sandbox):
    """An account whose primary group has no name (gid 1000, no /etc/group
    entry): `id -gn` prints the number AND an error. Nothing may echo it."""

    def setUp(self):
        super().setUp()
        (self.stubs / "id").write_text(
            '#!/bin/sh\ncase "$1" in\n  -u|-g) echo 1000 ;;\n  -un) echo demo-user ;;\n'
            '  -gn) echo "id: cannot find name for group ID 1000" >&2; echo 1000; exit 1 ;;\n'
            '  *) exit 1 ;;\nesac\n')

    def test_new_folder_outside_home_is_chowned_without_noise(self):
        d = self.tmp / "srv" / "aerodrome"
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", str(self.zip),
                                "--prefix", str(d))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("cannot find name", out)
        self.assertIn(f"sudo chown 1000:1000 {d}", self.sudo_log.read_text())

    def test_take_over_hint_is_numeric_and_quiet(self):
        d = self.tmp / "shared-empty"
        d.mkdir()
        d.chmod(0o555)
        rc, out = self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--from-zip", "/nonexistent",
                                "--prefix", str(d))
        self.assertEqual(rc, 1, out)
        self.assertNotIn("cannot find name", out)
        self.assertIn(f"sudo chown 1000:1000 {d}", out)
        self.assertNothingDone(out)


# Stand-in for curl. The GitHub API's reply to the "latest release" lookup
# comes from API_CODE (000 = no answer, like a DNS failure), API_HEADERS (one
# per line) and API_BODY; a release download is the fixture zip and its
# checksum. Like real curl, -f turns an HTTP error into exit 22 with no body.
CURL = r'''#!/bin/bash
echo "curl $*" >> "$STUB_LOG"
out="" hdr="" fmt="" fail=0 url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -D) hdr="$2"; shift 2 ;;
        -w) fmt="$2"; shift 2 ;;
        --*) shift ;;
        -*) case "$1" in *f*) fail=1 ;; esac; shift ;;
        *) url="$1"; shift ;;
    esac
done
case "$url" in
    */releases/latest)
        code="${API_CODE:-200}"
        if [ "$code" = 000 ]; then
            echo "curl: (6) Could not resolve host: api.github.com" >&2
            [ -n "$fmt" ] && printf 000
            exit 6
        fi
        if [ "$fail" = 1 ] && [ "$code" -ge 400 ]; then
            echo "curl: (22) The requested URL returned error: $code" >&2
            exit 22
        fi
        [ -n "$out" ] && printf '%s' "${API_BODY:-}" > "$out"
        if [ -n "$hdr" ]; then
            { printf 'HTTP/2 %s\r\n' "$code"
              [ -n "${API_HEADERS:-}" ] && printf '%s\n' "$API_HEADERS" | sed 's/$/\r/'
              printf '\r\n'; } > "$hdr"
        fi
        [ -n "$fmt" ] && printf '%s' "$code"
        exit 0 ;;
    */aerodrome-v9.9.9.zip) cp "$FIXTURE_ZIP" "$out" ;;
    */aerodrome-v9.9.9.zip.sha256) echo "$(sha256sum < "$FIXTURE_ZIP" | cut -d' ' -f1)  aerodrome-v9.9.9.zip" > "$out" ;;
    *) echo "curl: (22) The requested URL returned error: 404" >&2; exit 22 ;;
esac
[ -n "$fmt" ] && printf 200
exit 0
'''

RATE_LIMITED = "\n".join(("x-ratelimit-limit: 60", "x-ratelimit-remaining: 0",
                          "x-ratelimit-reset: 1800000000", "x-ratelimit-resource: core",
                          "content-type: application/json; charset=utf-8"))


@GUARD
@NEEDS
class LatestLookup(_Sandbox):
    """No --version / --from-zip: the GitHub API names the latest release.
    A rate-limit refusal is explained (when it resets, and that --version
    skips the API); no answer at all keeps the network advice."""

    def setUp(self):
        super().setUp()
        (self.stubs / "curl").write_text(CURL)
        (self.stubs / "curl").chmod(0o755)
        self.prefix = self.home / "aerodrome"

    def latest(self, code, headers="", body="", **kw):
        return self.run_boot("--yes", "--receiver-ip", "192.0.2.10", "--prefix", str(self.prefix),
                             API_CODE=code, API_HEADERS=headers, API_BODY=body,
                             FIXTURE_ZIP=self.zip, TZ="UTC", **kw)

    def assertStoppedClean(self, rc, out):
        self.assertEqual(rc, 1, out)
        self.assertFalse(self.prefix.exists(), out)
        self.assertEqual(self.sudo_log.read_text(), "", out)
        self.assertEqual(self.bootstrap_temps(), [])
        self.assertNotIn("Downloading", out)

    def test_rate_limit_is_explained(self):
        (self.home / "tmp=rel").mkdir()
        cases = {   # name: (code, headers, reset line or None, extra env)
            "403, limit used up": ("403", RATE_LIMITED, "It resets at 08:00 UTC.", {}),
            "403, HTTP/1.1 header case": ("403", RATE_LIMITED.replace("x-ratelimit-", "X-RateLimit-"),
                                          "It resets at 08:00 UTC.", {}),
            "429, no headers": ("429", "", None, {}),
            # mktemp keeps a relative TMPDIR relative: awk must not read a
            # "name=value/..." header-file path as a variable assignment.
            "403, relative TMPDIR shaped name=value": ("403", RATE_LIMITED, "It resets at 08:00 UTC.",
                                                       {"TMPDIR": "tmp=rel"}),
        }
        for name, (code, headers, reset, env) in cases.items():
            with self.subTest(name):
                rc, out = self.latest(code, headers, '{"message": "API rate limit exceeded"}', **env)
                self.assertStoppedClean(rc, out)
                self.assertEqual(list((self.home / "tmp=rel").iterdir()), [])
                self.assertIn(f"GitHub's hourly limit for this network is used up (HTTP {code}).", out)
                self.assertIn("skip this lookup with --version vX.Y.Z", out)
                self.assertIn("https://github.com/preston-peterson/aerodrome/releases", out)
                self.assertNotIn("Check network connectivity", out)
                self.assertNotIn("curl: (22)", out)
                if reset:
                    self.assertIn(reset, out)
                else:
                    self.assertNotIn("It resets at", out)

    def test_other_failures_keep_the_network_advice(self):
        cases = {   # name: (code, headers, headline)
            "no answer (DNS)": ("000", "", "Failed to fetch latest release info from GitHub."),
            "403, allowance left (a proxy)": (
                "403", RATE_LIMITED.replace("remaining: 0", "remaining: 12"),
                "Failed to fetch latest release info from GitHub (HTTP 403)."),
            "403, no rate-limit headers": (
                "403", "", "Failed to fetch latest release info from GitHub (HTTP 403)."),
            "502": ("502", "", "Failed to fetch latest release info from GitHub (HTTP 502)."),
        }
        for name, (code, headers, headline) in cases.items():
            with self.subTest(name):
                rc, out = self.latest(code, headers)
                self.assertStoppedClean(rc, out)
                self.assertIn(headline, out)
                self.assertIn("Check network connectivity, or use --version vX.Y.Z to pin.", out)
                self.assertNotIn("hourly limit", out)

    def test_unreadable_reply_says_so(self):
        cases = {   # name: (code, body)
            "captive portal page": ("200", "<html>sign in</html>"),
            "tag_name not a string": ("200", '{"tag_name": null, "name": "example"}'),
            "no body": ("204", ""),
        }
        for name, (code, body) in cases.items():
            with self.subTest(name):
                rc, out = self.latest(code, "", body)
                self.assertStoppedClean(rc, out)
                self.assertIn("Could not read the latest version from GitHub's reply.", out)
                self.assertIn("Use --version vX.Y.Z to pin.", out)
                self.assertNotIn("Version: ", out)
                self.assertNotIn("hourly limit", out)

    def test_any_2xx_reply_is_read(self):
        # Only a refusal (4xx/5xx) or no answer stops the lookup; a filtering
        # proxy may answer 203 with GitHub's JSON intact.
        rc, out = self.latest("203", "", '{\n  "tag_name": "v9.9.9"\n}\n')
        self.assertEqual(rc, 0, out)
        self.assertIn("Version: v9.9.9", out)
        self.assertIn("Bootstrap complete!", out)

    def test_relative_tmpdir_shaped_name_value_downloads_and_verifies(self):
        # mktemp keeps a relative TMPDIR relative: awk must not read the
        # "name=value/..." checksum-file path as a variable assignment.
        (self.home / "tmp=rel").mkdir()
        rc, out = self.latest("200", "", '{\n  "tag_name": "v9.9.9"\n}\n', TMPDIR="tmp=rel")
        self.assertEqual(rc, 0, out)
        self.assertIn("SHA256 verified", out)
        self.assertIn("Bootstrap complete!", out)
        self.assertEqual(list((self.home / "tmp=rel").iterdir()), [])

    def test_after_the_limit_resets_the_latest_release_installs(self):
        rc, out = self.latest("403", RATE_LIMITED)
        self.assertStoppedClean(rc, out)
        self.assertIn("hourly limit", out)
        rc, out = self.latest("200", "x-ratelimit-remaining: 59",
                              '{\n  "url": "https://api.github.com/example",\n  "tag_name": "v9.9.9",\n'
                              '  "name": "v9.9.9"\n}\n')
        self.assertEqual(rc, 0, out)
        self.assertIn("Version: v9.9.9", out)
        self.assertIn("SHA256 verified", out)
        self.assertIn("Bootstrap complete!", out)
        self.assertEqual((self.prefix / "VERSION").read_text(), "9.9.9\n")
        self.assertEqual(self.bootstrap_temps(), [])


@NEEDS
class Static(unittest.TestCase):

    def test_bash_syntax(self):
        r = subprocess.run([BASH, "-n", str(BOOTSTRAP)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_never_deletes_the_prefix(self):
        text = BOOTSTRAP.read_text()
        self.assertNotIn('rm -rf "$PREFIX"', text)
        code = [l for l in text.splitlines() if not l.lstrip().startswith(("#", "echo", "log_"))]
        self.assertFalse([l for l in code if "sudo rm" in l])   # only ever printed as advice
        executed = [l.strip() for l in text.splitlines()
                    if re.search(r"(^|[;&|]\s*|\s)rm -rf ", l) and not l.lstrip().startswith(("#", "log_", "echo"))
                    and "rm_cmd=" not in l]
        self.assertEqual(executed, ['rm -rf "$TMPDIR_BOOT" || log_warn "Could not remove temp dir $TMPDIR_BOOT"'])

    def test_group_name_lookups_are_quiet(self):
        # `id -gn` prints an error when the primary group has no name: use
        # numeric ids, or silence it — however the flags are spelled and
        # whatever runs it, in $(…) or backticks.
        subst = re.compile(r"\$\(([^)]*)\)|`([^`]*)`")
        group_name = re.compile(r"\bid\s+(-[gG]n|-n[gG]|-[gG]\s+-n|-n\s+-[gG])\b")
        for script in (BOOTSTRAP, REPO / "install.sh", REPO / "uninstall.sh",
                       REPO / "scripts" / "recover-sudoers.sh"):
            for n, line in enumerate(script.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for m in subst.finditer(line):
                    body = m.group(1) if m.group(1) is not None else m.group(2)
                    if group_name.search(body):
                        with self.subTest(script=script.name, line=n):
                            self.assertIn("2>/dev/null", body, line)

    def test_os_id_only_inside_pkg_detect(self):
        text = BOOTSTRAP.read_text()
        body = re.search(r"^pkg_detect\(\) \{\n.*?^\}\n", text, re.M | re.S).group(0)
        rest = text.replace(body, "")
        self.assertNotRegex(rest, r"\$\{?os_id\b")

    def test_main_is_called_only_on_the_last_line(self):
        lines = [l for l in BOOTSTRAP.read_text().splitlines() if l.strip()]
        self.assertEqual(lines[-1], '[ "${AERODROME_BOOTSTRAP_SOURCE_ONLY:-0}" = 1 ] || { main "$@"; }')
        self.assertEqual(sum(1 for l in lines if re.search(r'\bmain "\$@"', l)), 1)


if __name__ == "__main__":
    unittest.main()
