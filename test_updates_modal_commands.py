"""templates/updates.html — the "Sudoers update required" commands.

The prompt is drawn by the INSTALLED release, so every place that opens it
must pass the server-built recovery_command; the fallbacks (an older server's
reply) must be the installed scripts/recover-sudoers.sh — a hand-off to the
install.sh with the newest rules — never `sudo bash <install>/install.sh`,
the installed installer that rewrites the OLD rules and never lifts a staged
update's gate. Static checks, plus (when node is available) the real `_shq`
quoting run through bash. No server, no browser.
"""
import re
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
HTML = (REPO / "templates" / "updates.html").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


def js_function(name):
    m = re.search(rf"^function {name}\(.*?^\}}\n", HTML, re.M | re.S)
    return m.group(0) if m else None


class ModalCallSites(unittest.TestCase):

    def test_every_modal_call_passes_recovery_command(self):
        sites = [m.start() for m in re.finditer(r"showSudoersRequiredModal\(\{", HTML)]
        self.assertGreaterEqual(len(sites), 3)
        for at in sites:
            line = HTML.count("\n", 0, at) + 1
            body = HTML[at:HTML.index("})", at)]
            with self.subTest(line=line):
                self.assertIn("recovery_command", body,
                              f"updates.html:{line} opens the modal without recovery_command")

    def test_fallbacks_are_the_installed_recover_script(self):
        self.assertIn("opts.recovery_command || _recoverCmd(installDir)", HTML)
        self.assertIn("j.sudoers.recovery_command || _recoverCmd(installDir)", HTML)
        self.assertIn("driftCheck.recovery_command || _recoverCmd(installDir)", HTML)
        fn = js_function("_recoverCmd")
        self.assertIsNotNone(fn)
        self.assertIn("sudo bash ${_shq(", fn)
        self.assertIn("'/scripts/recover-sudoers.sh'", fn)

    def test_no_installed_install_sh_command_remains(self):
        for pat in (r"sudo bash \$\{installDir\}/install\.sh",
                    r"sudo bash \$\{_esc\(installDir\)\}/install\.sh",
                    r"sudo bash [^`'\"]*/install\.sh"):
            with self.subTest(pattern=pat):
                self.assertIsNone(re.search(pat, HTML))

    def test_texts_no_longer_say_rerun_install_sh(self):
        self.assertIsNone(re.search(r"(?i)re-?run(ning)? install\.sh", HTML))
        self.assertGreaterEqual(HTML.count("This only refreshes Aerodrome's system permissions."), 2)

    def test_drift_banner_shows_the_server_command_escaped(self):
        self.assertIn("${_esc(driftCmd)}", HTML)


class ShellQuoting(unittest.TestCase):
    """_shq() must produce one shell word that is exactly the path, whatever
    the path holds (spaces, quotes, $, backslashes)."""

    PATHS = ["/opt/aerodrome", "/home/u/My Apps/aero", "/srv/it's here", "/tmp/a$b\\c\"d"]

    def js_shq(self, s):
        if not NODE:
            # Same algorithm as the JS, for hosts without node.
            return "'" + s.replace("'", "'\\''") + "'"
        prog = js_function("_shq") + "process.stdout.write(_shq(process.argv[1]));"
        r = subprocess.run([NODE, "-e", prog, "--", s], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_round_trip(self):
        self.assertIsNotNone(js_function("_shq"))
        for p in self.PATHS:
            with self.subTest(path=p):
                q = self.js_shq(p)
                self.assertEqual(shlex.split(q), [p])
                if shutil.which("bash"):
                    out = subprocess.run(["bash", "-c", f"printf %s {q}"], capture_output=True,
                                         text=True, stdin=subprocess.DEVNULL).stdout
                    self.assertEqual(out, p)


if __name__ == "__main__":
    unittest.main()
