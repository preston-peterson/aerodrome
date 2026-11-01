"""The permission rules install.sh writes (SUDOERS_VERSION 6, v3.4.150): what
Aerodrome's service account may run as root, checked with sudo's own
matching rule for a command listed without wildcards — the program path, and
then either no argument list (any arguments) or exactly the listed ones.

The rules come from install.sh's render_sudoers(), run alone in bash for an
invented account. Nothing runs with privilege; with a visudo that can check
a file unprivileged (sudo-rs's can), the rendered file is checked too.
"""
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
SVC = "svc-a"
H = "/usr/local/libexec/aerodrome/aerodrome-helper"
MARKER = re.compile(r"\s*#\s*SUDOERS_VERSION\s*:\s*(\d+)")


def render(account=SVC):
    text = (REPO / "install.sh").read_text()
    func = re.search(r"^render_sudoers\(\) \{\n.*?^\}\n", text, re.M | re.S).group(0)
    return subprocess.run(["bash", "-c", func + 'render_sudoers "$1"', "render", account],
                          capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL).stdout


def grants(rules):
    """[(runas, path, args or None)] — None: listed without arguments."""
    out = []
    for line in rules.splitlines():
        m = re.match(rf"{re.escape(SVC)} ALL=\((\w+)\) NOPASSWD: (.*)$", line)
        if not m:
            continue
        for cmd in m.group(2).split(","):
            argv = shlex.split(cmd.strip())
            out.append((m.group(1), argv[0], argv[1:] or None))
    return out


def permitted(rules, argv):
    """Would `sudo -n <argv>` (as root, no -u) be allowed? argv[0] is the
    path sudo resolves; without a leading / it is tried in /bin and
    /usr/bin, as sudo's secure_path would find it."""
    paths = [argv[0]] if argv[0].startswith("/") else ["/bin/" + argv[0], "/usr/bin/" + argv[0]]
    return any(runas == "root" and path in paths and (args is None or args == argv[1:])
               for runas, path, args in grants(rules))


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class SudoersV6(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rules = render()

    def test_writes_are_denied(self):
        for argv in (["tee", "/etc/systemd/system/ntfy.service"],
                     ["tee", "/etc/systemd/system/aerodrome-synthetic-feeder.service"],
                     ["tee", "/etc/ntfy/server.yml"], ["tee", "/var/lib/ntfy/aerodrome-installed"],
                     ["install", "-m", "0755", "/var/cache/ntfy/staging/ntfy", "/usr/local/bin/ntfy"],
                     ["install", "-d", "-m", "0755", "-o", SVC, "-g", "users", "/var/cache/ntfy"],
                     ["rm", "-f", "/usr/local/bin/ntfy"], ["rm", "-f", "/etc/ntfy/server.yml"],
                     ["rm", "-rf", "/var/cache/ntfy/attachments"],
                     ["rmdir", "--ignore-fail-on-non-empty", "/etc/ntfy"],
                     ["systemctl", "start", "ntfy"], ["systemctl", "restart", "ntfy"],
                     ["systemctl", "enable", "ntfy"],
                     ["systemctl", "start", "aerodrome-synthetic-feeder"],
                     ["systemctl", "restart", "aerodrome-synthetic-feeder"],
                     ["/usr/bin/python3", "-I", H, "version"], ["/usr/bin/python3", H],
                     [H + "/../../bin/sh"], ["cat", "/etc/shadow"]):
            with self.subTest(argv=argv):
                self.assertFalse(permitted(self.rules, argv))

    def test_the_old_root_sequences_each_break(self):
        """v5's three ways to root, and R3b (a config root runs), each need a
        step v6 denies."""
        sequences = {
            "A: a unit with no User=": [["tee", "/etc/systemd/system/ntfy.service"],
                                        ["systemctl", "daemon-reload"], ["systemctl", "start", "ntfy"]],
            "B: the feeder unit": [["tee", "/etc/systemd/system/aerodrome-synthetic-feeder.service"],
                                   ["systemctl", "daemon-reload"],
                                   ["systemctl", "restart", "aerodrome-synthetic-feeder"]],
            "C: a planted binary": [["install", "-m", "0755", "/var/cache/ntfy/staging/ntfy",
                                     "/usr/local/bin/ntfy"], ["systemctl", "restart", "ntfy"]],
            "R3b: a config root's ntfy reads": [["tee", "/etc/ntfy/server.yml"],
                                                ["systemctl", "restart", "ntfy"]],
        }
        for name, steps in sequences.items():
            with self.subTest(name):
                self.assertFalse(all(permitted(self.rules, s) for s in steps))

    def test_what_the_previous_release_runs_still_works(self):
        """N-1: 3.4.149 still restarts, reads the rules (Re-check) and runs
        its switch-to-real with these rules; and the helper takes any
        arguments."""
        for argv in (["systemctl", "restart", "--no-block", "aerodrome"],
                     ["systemctl", "restart", "aerodrome"],
                     ["cat", "/etc/sudoers.d/aerodrome"],
                     ["systemctl", "stop", "aerodrome-synthetic-feeder"],
                     ["systemctl", "disable", "aerodrome-synthetic-feeder"],
                     ["rm", "-f", "/etc/systemd/system/aerodrome-synthetic-feeder.service"],
                     ["systemctl", "daemon-reload"],
                     [H, "version"], [H, "ntfy-install", "latest", "--port", "2586"],
                     [H, "ntfy-config", "--drop-custom-keys", "auth-file"], [H, "feeder-remove"]):
            with self.subTest(argv=argv):
                self.assertTrue(permitted(self.rules, argv))

    def test_the_grants_are_exactly_these(self):
        """An allowlist: the tests above name what must be denied, and a
        grant nobody thought of (a shell, `systemctl link *`) denies none of
        them. Every non-comment line is a plain NOPASSWD rule as root, and
        the commands are exactly these — no wildcard, negation, ALL or
        sudoedit; no Defaults, alias or include (`#include` is a directive)."""
        lines = [l for l in self.rules.splitlines()
                 if l.strip() and (not l.lstrip().startswith("#") or re.match(r"\s*#include", l))]
        cmds = []
        for line in lines:
            m = re.fullmatch(rf"{re.escape(SVC)} ALL=\(root\) NOPASSWD: (\S.*)", line)
            self.assertIsNotNone(m, line)
            cmds += [c.strip() for c in m.group(1).split(",")]
        self.assertEqual(sorted(cmds), sorted([
            H,
            "/bin/systemctl restart aerodrome", "/usr/bin/systemctl restart aerodrome",
            "/bin/systemctl restart --no-block aerodrome",
            "/usr/bin/systemctl restart --no-block aerodrome",
            "/bin/cat /etc/sudoers.d/aerodrome", "/usr/bin/cat /etc/sudoers.d/aerodrome",
            # the previous release's switch-to-real (gone at SUDOERS_VERSION 7)
            "/bin/systemctl stop aerodrome-synthetic-feeder",
            "/usr/bin/systemctl stop aerodrome-synthetic-feeder",
            "/bin/systemctl disable aerodrome-synthetic-feeder",
            "/usr/bin/systemctl disable aerodrome-synthetic-feeder",
            "/bin/rm -f /etc/systemd/system/aerodrome-synthetic-feeder.service",
            "/bin/systemctl daemon-reload", "/usr/bin/systemctl daemon-reload"]))
        for cmd in cmds:
            self.assertIsNone(re.search(r"[*?\[\]!\\]|\bALL\b|sudoedit", cmd), cmd)

    def test_runas_is_root_and_no_grant_points_into_an_install(self):
        for runas, path, args in grants(self.rules):
            self.assertEqual(runas, "root")
            self.assertTrue(path.startswith(("/bin/", "/usr/bin/", "/usr/local/libexec/aerodrome/")),
                            path)
            for a in args or ():
                self.assertNotIn("update/", a)
                self.assertNotIn("/opt/", a)
        self.assertNotIn("Defaults", self.rules)            # sudo-rs: no Defaults lines

    def test_one_marker_equal_to_the_helpers(self):
        markers = [int(m.group(1)) for m in map(MARKER.match, self.rules.splitlines()) if m]
        self.assertEqual(markers, [6])
        text = (REPO / "install.sh").read_text()
        first = next(int(m.group(1)) for m in map(MARKER.match, text.splitlines()) if m)
        self.assertEqual(first, 6)
        line2 = (REPO / "scripts" / "aerodrome-helper").read_text().splitlines()[1]
        self.assertEqual(line2, "# PRIV_VERSION = 6")

    def test_the_account_is_the_only_grantee_whatever_its_primary_group(self):
        """VM case G's shape (`useradd -g users`): no rule names a group, so
        nothing depends on a group named after the account."""
        for acct in ("svc-a", "svc.dotted", "SvcUpper", "jdoe@corp.example.com"):
            with self.subTest(acct=acct):
                rules = render(acct)
                rule_lines = [l for l in rules.splitlines() if "ALL=(" in l]
                self.assertTrue(rule_lines)
                self.assertTrue(all(l.startswith(acct + " ALL=(root) NOPASSWD: ") for l in rule_lines))
                self.assertNotIn("-g ", rules)
                self.assertNotIn("users", rules)

    def test_real_visudo_accepts_it_when_available(self):
        visudo = shutil.which("visudo")
        if not visudo:
            self.skipTest("no visudo on this host")
        with tempfile.TemporaryDirectory() as d:
            probe = Path(d) / "probe"
            probe.write_text("svc-a ALL=(root) NOPASSWD: /bin/true\n")
            probe.chmod(0o440)
            if subprocess.run([visudo, "-cf", str(probe)], capture_output=True,
                              stdin=subprocess.DEVNULL).returncode != 0:
                self.skipTest("this host's visudo can't check a file unprivileged")
            for acct in ("svc-a", "svc.dotted", "SvcUpper", "svc$"):
                with self.subTest(acct=acct):
                    f = Path(d) / f"rules-{len(acct)}"
                    f.write_text(render(acct))
                    f.chmod(0o440)
                    v = subprocess.run([visudo, "-cf", str(f)], capture_output=True, text=True,
                                       stdin=subprocess.DEVNULL)
                    self.assertEqual(v.returncode, 0, v.stdout + v.stderr)


if __name__ == "__main__":
    unittest.main()
