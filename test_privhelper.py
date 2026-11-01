"""privhelper.py — the one module that builds a sudo command line (v3.4.150).

subprocess.run is mocked: nothing runs. The outcome of a helper call is told
from the reply's shape (one JSON line on stdout), never from sudo's words, so
it holds in any language and for sudo and sudo-rs alike. Paths are invented.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import privhelper

HELPER = str(privhelper.HELPER)


def reply(rc, stdout="", stderr=""):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, stdout, stderr)


class RunHelper(unittest.TestCase):

    def call(self, side_effect, sub="version", *args):
        with mock.patch("subprocess.run", side_effect=side_effect) as run:
            try:
                return privhelper.run_helper(sub, *args), run
            except privhelper.HelperError as e:
                return e, run

    def test_argv_env_and_timeouts(self):
        for sub, timeout in (("ntfy-install", 300), ("ntfy-config", 90), ("feeder-remove", 90),
                             ("version", 15)):
            with self.subTest(sub=sub):
                out, run = self.call(reply(0, '{"ok": true}\n'), sub, "--x", "1")
                self.assertEqual(out, {"ok": True})
                (argv,), kw = run.call_args
                self.assertEqual(argv, ["sudo", "-n", HELPER, sub, "--x", "1"])
                self.assertEqual((kw["env"]["LC_ALL"], kw["env"]["LANG"]), ("C", "C"))
                self.assertEqual(kw["timeout"], timeout)
                self.assertTrue(kw["capture_output"])

    def test_no_helper_answer_is_unavailable_in_any_language(self):
        cases = {
            "sudo": reply(1, "", "sudo: a password is required\n"),
            "sudo-rs": reply(1, "", "sudo: I'm sorry svc-a. I'm afraid I can't do that\n"),
            "german, no JSON": reply(1, "", "sudo: Ein Passwort ist notwendig\n"),
            "helper missing": reply(1, "", "sudo: /usr/local/libexec/aerodrome/aerodrome-helper: "
                                            "command not found\n"),
            "rc 0 but no JSON": reply(0, "hello\n"),
            "a JSON list": reply(1, "[1]\n"),
            "no sudo binary": FileNotFoundError("sudo"),
        }
        for label, effect in cases.items():
            with self.subTest(label):
                e, _ = self.call(effect)
                self.assertIsInstance(e, privhelper.HelperUnavailable)
                self.assertEqual(e.command, privhelper.live_recovery_command())
                self.assertIn("recover-sudoers.sh", e.command)

    def test_the_helpers_own_reply(self):
        e, _ = self.call(reply(3, '{"ok": false, "message": "busy", "busy": true}\n'))
        self.assertIsInstance(e, privhelper.HelperRefused)
        self.assertEqual((str(e), e.payload["busy"]), ("busy", True))
        for rc in (1, 2):
            with self.subTest(rc=rc):
                e, _ = self.call(reply(rc, '{"ok": false, "message": "it broke"}\n'))
                self.assertIsInstance(e, privhelper.HelperFailed)
                self.assertEqual(str(e), "it broke")
        e, _ = self.call(reply(1, 'noise\n{"ok": false, "message": "last line"}\n'))
        self.assertEqual(str(e), "last line")

    def test_a_timeout_is_still_running(self):
        """…which promises nothing: a helper Aerodrome started is stopped
        with aerodrome.service, and then it never finishes."""
        e, _ = self.call(subprocess.TimeoutExpired(["sudo"], 300), "ntfy-install", "latest")
        self.assertIsInstance(e, privhelper.HelperStillRunning)
        self.assertEqual(str(e), "Still working — the download can take a few minutes. "
                                 "Check the status again in a minute, and try again if "
                                 "nothing has changed.")

    def test_sudo_that_cant_be_started_is_a_helper_error(self):
        """Not executable, or no process to spare: every call site catches
        HelperError only, so nothing else may escape (switch-to-real would
        stop half-way, its config staged)."""
        import errno
        for label, exc in (("EACCES", PermissionError(errno.EACCES, "Permission denied")),
                           ("EAGAIN", BlockingIOError(errno.EAGAIN, "Resource temporarily "
                                                                    "unavailable")),
                           ("ENOMEM", OSError(errno.ENOMEM, "Cannot allocate memory"))):
            with self.subTest(label):
                e, _ = self.call(exc, "feeder-remove")
                self.assertIsInstance(e, privhelper.HelperFailed)
                self.assertIn("couldn't run sudo", str(e))


class RunSudo(unittest.TestCase):

    def test_restart_and_cat_rules(self):
        with mock.patch("subprocess.run", side_effect=reply(0)) as run:
            privhelper.run_sudo(["systemctl", "restart", "--no-block", "aerodrome"], timeout=10)
        (argv,), kw = run.call_args
        self.assertEqual(argv, ["sudo", "-n", "systemctl", "restart", "--no-block", "aerodrome"])
        self.assertEqual((kw["env"]["LC_ALL"], kw["timeout"]), ("C", 10))


class LiveRecoveryCommand(unittest.TestCase):

    def test_default_is_this_install_and_paths_are_quoted(self):
        self.assertEqual(privhelper.INSTALL_DIR, Path(privhelper.__file__).resolve().parent)
        self.assertEqual(privhelper.live_recovery_command(),
                         privhelper.live_recovery_command(privhelper.INSTALL_DIR))
        self.assertEqual(privhelper.live_recovery_command(Path("/srv/example/aerodrome")),
                         "sudo bash /srv/example/aerodrome/scripts/recover-sudoers.sh")
        self.assertEqual(privhelper.live_recovery_command(Path("/srv/my apps/aerodrome")),
                         "sudo bash '/srv/my apps/aerodrome/scripts/recover-sudoers.sh'")


class HelperFileVersion(unittest.TestCase):

    def version_of(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "aerodrome-helper"
            if text is not None:
                p.write_text(text)
            with mock.patch.object(privhelper, "HELPER", p):
                return privhelper.helper_file_version()

    def test_line_two_without_sudo(self):
        self.assertEqual(self.version_of("#!/usr/bin/python3.12 -IS\n# PRIV_VERSION = 6\n"), 6)
        self.assertEqual(self.version_of("#!/usr/bin/python3 -IS\n# PRIV_VERSION = 17\nx\n"), 17)
        for text in (None, "", "#!/x\n", "#!/x\n# PRIV_VERSION = six\n", "#!/x\n# other = 6\n"):
            with self.subTest(text=text):
                self.assertEqual(self.version_of(text), 0)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a 000 file")
    def test_unreadable_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "aerodrome-helper"
            p.write_text("#!/x\n# PRIV_VERSION = 6\n")
            p.chmod(0)
            with mock.patch.object(privhelper, "HELPER", p):
                self.assertEqual(privhelper.helper_file_version(), 0)


if __name__ == "__main__":
    unittest.main()
