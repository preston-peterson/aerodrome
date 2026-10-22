"""Tests for scripts/check_installers.py — the installer-consistency gate
bump-version.sh runs on every bump and package.

Fast (~0.2 s): the only subprocess is `bash -n`. The clean-tree tests run on
this tree (a git checkout or an installed release — the script falls back to
globbing outside git); the negative cases copy the two or three relevant
files into a temp dir and mutate exactly one thing.
"""
import contextlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "check_installers", REPO / "scripts" / "check_installers.py")
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)

HAVE_BASH = bool(shutil.which("bash"))
HAVE_GIT = bool(shutil.which("git"))
INSTALLER_FILES = ("install.sh", "scripts/bootstrap.sh", "scripts/recover-sudoers.sh")


def run_main(root, checks=None):
    argv = ["check_installers.py", str(root)] + ([checks] if checks else [])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ci.main(argv)
    return rc, out.getvalue()


class CleanTree(unittest.TestCase):
    """Every check passes on the shipped scripts."""

    def test_pkg_detect_identical(self):
        self.assertEqual(ci.check_pkg_detect(REPO), [])

    def test_sudoers_rules_identical(self):
        self.assertEqual(ci.check_sudoers(REPO), [])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_bash_syntax(self):
        self.assertEqual(ci.check_bash_syntax(REPO), [])

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_cli_all_checks_pass(self):
        rc, out = run_main(REPO)
        self.assertEqual(rc, 0, out)
        for name, _ in ci.CHECKS:
            self.assertIn(f"✓ {name}", out)

    def test_cli_rejects_unknown_check(self):
        rc, out = run_main(REPO, "no-such-check")
        self.assertEqual(rc, 1)
        self.assertIn("unknown check", out)

    def test_cli_help(self):
        for flag in ("--help", "-h"):
            rc, out = run_main(flag)
            self.assertEqual(rc, 0, out)
            self.assertIn("Usage:", out)
            self.assertNotIn("✓ bash-syntax", out)   # never a (vacuous) check line

    def test_cli_root_must_be_a_directory(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = run_main(Path(d) / "no-such-dir")
        self.assertEqual(rc, 2, out)
        self.assertIn("not a directory", out)
        self.assertNotIn("✓", out)

    def test_no_shell_scripts_is_a_finding_not_a_pass(self):
        with tempfile.TemporaryDirectory() as d:
            errs = ci.check_bash_syntax(d)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("no *.sh files", errs[0])

    def test_marker_matches_server_regex(self):
        # server.py reads the FIRST `# SUDOERS_VERSION: N` line; the gate must
        # use the same pattern or it could pass a file the updater misreads.
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        self.assertIsNotNone(ci.first_marker(text))
        self.assertEqual(ci.first_marker(text),
                         ci.first_marker(ci.heredoc(text)))


class OneMutationOneFinding(unittest.TestCase):
    """Each mutation yields exactly one ✗ line, naming the right check."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aerodrome-ci-test-")
        self.root = Path(self._tmp.name)
        for rel in INSTALLER_FILES:
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dst)

    def tearDown(self):
        self._tmp.cleanup()

    def mutate(self, rel, old, new, count=1):
        p = self.root / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text, f"fixture anchor missing from {rel}")
        p.write_text(text.replace(old, new, count), encoding="utf-8")

    def assertOnlyFinding(self, check):
        rc, out = run_main(self.root) if HAVE_BASH else run_main(
            self.root, "pkg-detect,sudoers-rules")
        self.assertEqual(rc, 1, out)
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith("✗")]
        self.assertEqual(failed, [f"✗ {check}"], out)

    def test_copied_fixture_is_clean(self):
        rc, out = run_main(self.root) if HAVE_BASH else run_main(
            self.root, "pkg-detect,sudoers-rules")
        self.assertEqual(rc, 0, out)

    def test_edited_pkg_detect_line(self):
        self.mutate("install.sh", 'local os_id="" os_like=""',
                    'local os_id="" os_like="" extra=""')
        self.assertEqual(len(ci.check_pkg_detect(self.root)), 1)
        self.assertOnlyFinding("pkg-detect")

    def test_missing_pkg_detect(self):
        self.mutate("scripts/bootstrap.sh", "pkg_detect() {", "pkg_detect_renamed() {")
        self.assertEqual(len(ci.check_pkg_detect(self.root)), 1)

    def test_extra_rule_in_recover(self):
        self.mutate("scripts/recover-sudoers.sh", "# SUDOERS_VERSION: 5\n",
                    "# SUDOERS_VERSION: 5\n${USER} ALL=(ALL) NOPASSWD: /bin/true\n")
        errs = ci.check_sudoers(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("rule lines differ", errs[0])
        self.assertOnlyFinding("sudoers-rules")

    def test_comment_only_difference_is_fine(self):
        self.mutate("scripts/recover-sudoers.sh", "# SUDOERS_VERSION: 5\n",
                    "# SUDOERS_VERSION: 5\n# a comment only recover-sudoers.sh has\n")
        self.assertEqual(ci.check_sudoers(self.root), [])

    def test_marker_comment_above_install_heredoc(self):
        self.mutate("install.sh", "pkg_detect() {",
                    "# SUDOERS_VERSION: 4  (stale note)\npkg_detect() {")
        errs = ci.check_sudoers(self.root)
        self.assertTrue(errs)
        self.assertOnlyFinding("sudoers-rules")

    def test_marker_bumped_in_one_file(self):
        self.mutate("install.sh", "# SUDOERS_VERSION: 5\n", "# SUDOERS_VERSION: 6\n")
        errs = ci.check_sudoers(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("SUDOERS_VERSION", errs[0])
        self.assertOnlyFinding("sudoers-rules")

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_syntax_error(self):
        p = self.root / "scripts" / "recover-sudoers.sh"
        p.write_text(p.read_text(encoding="utf-8") + "\nif then\n", encoding="utf-8")
        errs = ci.check_bash_syntax(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("recover-sudoers.sh", errs[0])
        self.assertOnlyFinding("bash-syntax")

    def test_quoted_heredoc_opener_in_install(self):
        # << 'SUDOEOF' installs a literal ${USER} (visudo rejects it) while the
        # rule TEXT still matches line for line — the opener must be checked.
        self.mutate("install.sh", "<< SUDOEOF\n", "<< 'SUDOEOF'\n")
        errs = ci.check_sudoers(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("quoted", errs[0])
        self.assertIn("install.sh", errs[0])
        self.assertOnlyFinding("sudoers-rules")

    def test_quoted_heredoc_opener_in_recover(self):
        self.mutate("scripts/recover-sudoers.sh", "<< SUDOEOF\n", '<< "SUDOEOF"\n')
        errs = ci.check_sudoers(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("recover-sudoers.sh", errs[0])
        self.assertOnlyFinding("sudoers-rules")


@unittest.skipUnless(HAVE_BASH and HAVE_GIT, "needs bash and git")
class NestedInOuterRepo(unittest.TestCase):
    """A release unpacked inside some other git repository (untracked there):
    git's file list is the OUTER repo's, so bash-syntax must glob this tree
    rather than check nothing."""

    def test_bash_syntax_globs_when_root_is_not_the_toplevel(self):
        with tempfile.TemporaryDirectory(prefix="aerodrome-ci-outer-") as d:
            outer = Path(d)
            subprocess.run(["git", "init", "-q", str(outer)], check=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
            root = outer / "unpacked" / "aerodrome"
            for rel in INSTALLER_FILES + ("uninstall.sh",):
                dst = root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / rel, dst)
            with open(root / "uninstall.sh", "a", encoding="utf-8") as f:
                f.write("\nif then\n")
            self.assertTrue(ci.tracked_sh(root))
            rc, out = run_main(root)
        self.assertEqual(rc, 1, out)
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith("✗")]
        self.assertEqual(failed, ["✗ bash-syntax"], out)
        self.assertIn("uninstall.sh", out)


if __name__ == "__main__":
    unittest.main()
