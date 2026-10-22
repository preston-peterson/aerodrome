"""Tests for scripts/check_installers.py — the installer-consistency gate
bump-version.sh runs on every bump and package.

Fast (~0.3 s): the only subprocess is `bash -n`. The clean-tree tests run on
this tree (a git checkout or an installed release — the script falls back to
globbing outside git); the negative cases copy the relevant files into a temp
dir and mutate exactly one thing.
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
INSTALLER_FILES = ("install.sh", "uninstall.sh", "scripts/bootstrap.sh",
                   "scripts/recover-sudoers.sh", "ntfy_installer.py")
NO_BASH_CHECKS = "pkg-detect,recover-delegates,sudo-user,pipefail-readers,ntfy-cache"


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

    def test_recover_delegates(self):
        self.assertEqual(ci.check_recover_delegates(REPO), [])

    def test_pipefail_readers(self):
        self.assertEqual(ci.check_pipefail_readers(REPO), [])

    def test_ntfy_cache(self):
        self.assertEqual(ci.check_ntfy_cache(REPO), [])

    def test_sudo_user(self):
        self.assertEqual(ci.check_sudo_user(REPO), [])

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
        for name in ("no-such-check", "sudoers-rules"):     # B3a's check is gone (v3.4.145)
            with self.subTest(name=name):
                rc, out = run_main(REPO, name)
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

    def test_first_marker_lives_in_render_sudoers(self):
        # server.py reads the FIRST `# SUDOERS_VERSION: N` line of a staged
        # install.sh; the gate uses the same pattern and pins where it is.
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        span = ci.func_span(text, "render_sudoers")
        self.assertIsNotNone(span)
        at = ci.first_marker_offset(text)
        self.assertTrue(span[0] <= at < span[1])
        self.assertEqual(ci.first_marker(text), ci.first_marker(text[span[0]:span[1]]))


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

    def append(self, rel, text):
        p = self.root / rel
        p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")

    def assertOnlyFinding(self, check):
        rc, out = run_main(self.root) if HAVE_BASH else run_main(self.root, NO_BASH_CHECKS)
        self.assertEqual(rc, 1, out)
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith("✗")]
        self.assertEqual(failed, [f"✗ {check}"], out)

    def test_copied_fixture_is_clean(self):
        rc, out = run_main(self.root) if HAVE_BASH else run_main(self.root, NO_BASH_CHECKS)
        self.assertEqual(rc, 0, out)

    # -- pkg-detect -------------------------------------------------------------
    def test_edited_pkg_detect_line(self):
        self.mutate("install.sh", 'local os_id="" os_like=""',
                    'local os_id="" os_like="" extra=""')
        self.assertEqual(len(ci.check_pkg_detect(self.root)), 1)
        self.assertOnlyFinding("pkg-detect")

    def test_missing_pkg_detect(self):
        self.mutate("scripts/bootstrap.sh", "pkg_detect() {", "pkg_detect_renamed() {")
        self.assertEqual(len(ci.check_pkg_detect(self.root)), 1)

    # -- recover-delegates -------------------------------------------------------
    def test_rule_line_added_to_recover(self):
        self.append("scripts/recover-sudoers.sh",
                    "# svc-a ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart aerodrome\n")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("rule text", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_marker_added_to_recover(self):
        self.append("scripts/recover-sudoers.sh", "# SUDOERS_VERSION: 5\n")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("marker", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_recover_without_privileges_only(self):
        p = self.root / "scripts" / "recover-sudoers.sh"
        p.write_text(p.read_text(encoding="utf-8").replace("--privileges-only", "--full"),
                     encoding="utf-8")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("--privileges-only", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_recover_hand_off_only_in_comments(self):
        # the flag still appears in the header comment and the candidate grep
        self.mutate("scripts/recover-sudoers.sh",
                    'exec bash "$best" --privileges-only --install-dir "$INSTALL_DIR"',
                    "echo would-run")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("does not hand off", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_marker_comment_inside_render_sudoers_before_the_heredoc(self):
        # server.py would read 6 while install.sh writes (and the gate lifts on) 5
        self.mutate("install.sh", '    local u="$1"\n    cat << SUDOEOF\n',
                    '    local u="$1"\n    # SUDOERS_VERSION: 6\n    cat << SUDOEOF\n')
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("first line of render_sudoers()'s << SUDOEOF heredoc", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_marker_comment_above_render_sudoers(self):
        self.mutate("install.sh", "render_sudoers() {",
                    "# SUDOERS_VERSION: 4  (stale note)\nrender_sudoers() {")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("not inside render_sudoers()", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_quoted_heredoc_opener(self):
        # << 'SUDOEOF' installs a literal ${u} (visudo rejects it) while the
        # rule TEXT looks right — the opener must be checked.
        self.mutate("install.sh", "cat << SUDOEOF\n", "cat << 'SUDOEOF'\n")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("quoted", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_render_sudoers_missing(self):
        self.mutate("install.sh", "render_sudoers() {", "render_rules() {")
        errs = ci.check_recover_delegates(self.root)
        self.assertTrue(errs)
        self.assertIn("render_sudoers() not found", errs[0])

    # -- sudo-user -----------------------------------------------------------------
    def test_unguarded_sudo_user_account_pick(self):
        # install.sh's account pick before it checked for root: a non-root run
        # with an inherited SUDO_USER installed the service for the admin.
        self.mutate("install.sh",
                    'elif [ "$(id -u)" -eq 0 ]; then\n    USER="${SUDO_USER:-$(whoami)}"\n'
                    'else\n    USER="$(id -un)"\nfi\n',
                    'else\n    USER="${SUDO_USER:-$(whoami)}"\nfi\n')
        errs = ci.check_sudo_user(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertRegex(errs[0], r"^install\.sh:\d+: SUDO_USER read without a root check")
        self.assertIn('USER="${SUDO_USER:-$(whoami)}"', errs[0])
        self.assertOnlyFinding("sudo-user")

    def test_unguarded_sudo_user_in_any_script(self):
        # The gate walks every script, not just install.sh (and the CLI runs it).
        for rel in ("uninstall.sh", "scripts/bootstrap.sh", "scripts/recover-sudoers.sh"):
            with self.subTest(script=rel):
                p = self.root / rel
                before = p.read_text(encoding="utf-8")
                self.append(rel, '\nADMIN_NAME="${SUDO_USER:-}"\n')
                try:
                    errs = ci.check_sudo_user(self.root)
                    self.assertEqual(len(errs), 1, errs)
                    self.assertTrue(errs[0].startswith(f"{rel}:"), errs[0])
                    self.assertOnlyFinding("sudo-user")
                finally:
                    p.write_text(before, encoding="utf-8")

    # -- pipefail-readers --------------------------------------------------------
    def test_early_exit_reader_in_bootstrap(self):
        self.append("scripts/bootstrap.sh",
                    "\nif systemctl list-unit-files 2>/dev/null | grep -q '^x\\.service'; then :; fi\n")
        errs = ci.check_pipefail_readers(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("scripts/bootstrap.sh:", errs[0])
        self.assertOnlyFinding("pipefail-readers")

    def test_reader_in_script_without_pipefail_is_fine(self):
        self.append("uninstall.sh", "\nls / | head -1 >/dev/null\n")
        self.assertEqual(ci.check_pipefail_readers(self.root), [])

    # -- ntfy-cache --------------------------------------------------------------
    def test_ntfy_cache_only_in_a_comment(self):
        p = self.root / "uninstall.sh"
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        p.write_text("".join(l if l.lstrip().startswith("#")
                             else l.replace("/var/lib/ntfy/cache.db", "/var/lib/ntfy/other.db")
                             for l in lines)
                     + "# purge /var/lib/ntfy/cache.db some day\n", encoding="utf-8")
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("/var/lib/ntfy/cache.db", errs[0])
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_template_moved_fails_closed(self):
        self.mutate("ntfy_installer.py", "cache-file: /var/lib/ntfy/cache.db",
                    "cache-file: {cache_path}")
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("no `cache-file:", errs[0])

    # -- bash-syntax -------------------------------------------------------------
    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_syntax_error(self):
        self.append("scripts/recover-sudoers.sh", "\nif then\n")
        errs = ci.check_bash_syntax(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("recover-sudoers.sh", errs[0])
        self.assertOnlyFinding("bash-syntax")


class PipefailLint(unittest.TestCase):
    """pipefail_reader_lines() on synthetic scripts."""

    HEAD = "#!/bin/bash\nset -euo pipefail\n"

    def hits(self, body, head=None):
        return [n for n, _ in ci.pipefail_reader_lines((self.HEAD if head is None else head) + body)]

    def test_flags_early_exit_readers(self):
        for line in ("x=$(cmd | grep -q foo)", "cmd | grep -m1 foo", "cmd 2>/dev/null | grep -qw x",
                     "cmd | grep -Em1 x", "cmd | grep --quiet x", "cmd | grep -l x",
                     "cmd | head -1", "cmd | head -n 1", "cmd | awk '{print $1; exit}'",
                     "    | awk '/x/ {print; exit}' \\"):
            with self.subTest(line=line):
                self.assertEqual(self.hits(line + "\n"), [3])

    def test_continuation_after_a_trailing_pipe(self):
        self.assertEqual(self.hits("x=$(cmd |\n    awk '/s/{print; exit}')\n"), [4])

    def test_ignores_safe_forms(self):
        for line in ("cmd || grep -q x file", "grep -q x file", "grep -m1 x file | sed s/a/b/",
                     "cmd | awk '!f && /x/ {print; f=1}'", "cmd | grep x", "cmd | sed -n 1p",
                     "# cmd | grep -q x", "cmd | grep -q x  # pipefail-ok: reads a tiny string",
                     "echo a | tr a b"):
            with self.subTest(line=line):
                self.assertEqual(self.hits(line + "\n"), [])

    def test_only_scripts_that_set_pipefail(self):
        self.assertEqual(self.hits("cmd | grep -q x\n", head="#!/bin/bash\nset -e\n"), [])
        for head in ("set -o pipefail\n", "set -eo pipefail\n", "set -e -o pipefail\n"):
            with self.subTest(head=head):
                self.assertEqual(self.hits("cmd | head -1\n", head=head), [2])

    def test_heredoc_bodies_are_skipped(self):
        body = "cat <<'EOF'\nusage: cmd | head -1\nEOF\ncat <<EOF\n x | grep -q y\nEOF\n"
        self.assertEqual(self.hits(body), [])
        self.assertEqual(self.hits(body + "cmd | head -1\n"), [9])


class SudoUserLint(unittest.TestCase):
    """sudo_user_lines() on synthetic scripts."""

    ROOT = 'if [ "$(id -u)" -eq 0 ]; then\n'

    def hits(self, body):
        return [n for n, _ in ci.sudo_user_lines("#!/bin/bash\n" + body)]

    def test_flags_reads_outside_a_root_branch(self):
        for body in ('USER="${SUDO_USER:-$(whoami)}"\n',
                     'if [ -n "${SUDO_USER:-}" ]; then echo x; fi\n',
                     'echo "$SUDO_UID:${SUDO_GID}"\n',
                     'f() {\n    u="$SUDO_USER"\n}\n',
                     'if [ "$(id -u)" -ne 0 ]; then\n    u="$SUDO_USER"\nfi\n',
                     'cat <<EOF\nuser: $SUDO_USER\nEOF\n'):        # unquoted: expands
            with self.subTest(body=body):
                self.assertEqual(len(self.hits(body)), 1)

    def test_else_or_sibling_elif_of_a_root_test_is_not_root(self):
        self.assertEqual(self.hits(self.ROOT + "    :\nelse\n    u=\"$SUDO_USER\"\nfi\n"), [5])
        self.assertEqual(self.hits(self.ROOT + "    :\nelif [ -n \"$x\" ]; then\n"
                                   "    u=\"$SUDO_USER\"\nfi\n"), [5])

    def test_flags_a_root_test_that_does_not_guard_the_read(self):
        # A root test on the line (or in the if) is not enough: the read must
        # be unable to run without it passing.
        for body in ('[ "$(id -u)" -eq 0 ] || u="$SUDO_USER"\n',             # runs when NOT root
                     '[ "$(id -u)" -eq 0 ]; u="$SUDO_USER"\n',
                     '[ "$(id -u)" -eq 0 ] && x=1 || u="$SUDO_USER"\n',
                     'if ! [ "$(id -u)" -eq 0 ]; then\n    u="$SUDO_USER"\nfi\n',
                     'if ! [ "$(id -u)" -eq 0 ]\nthen\n    u="$SUDO_USER"\nfi\n',
                     'if [ ! "$(id -u)" -eq 0 ]; then u="$SUDO_USER"; fi\n',
                     '[[ ! $EUID -eq 0 ]] && u="$SUDO_USER"\n',
                     'if [ "$(id -u)" -eq 0 ]; then :; else u="$SUDO_USER"; fi\n',
                     'if [ "$(id -u)" -eq 0 ]; then :; else\n    u="$SUDO_USER"\nfi\n',
                     'if [ "$(id -u)" -eq 0 ]; then :; elif [ -n "$x" ]; then u="$SUDO_USER"; fi\n',
                     'if [ "$(id -u)" -eq 0 ]; then :; fi; u="$SUDO_USER"\n',
                     'if [ "$(id -u)" -eq 0 ] || [ -n "$x" ]; then\n    u="$SUDO_USER"\nfi\n',
                     'if [ -n "$x" ] || [ "$(id -u)" -eq 0 ]; then\n    u="$SUDO_USER"\nfi\n',
                     '[ "$(id -u)" -eq 0 -o -n "$x" ] && u="$SUDO_USER"\n',
                     '[[ $EUID -eq 0 || -n $x ]] && u="$SUDO_USER"\n',
                     'until [ "$(id -u)" -eq 0 ]; do u="$SUDO_USER"; done\n',
                     '[ "$(id -u)" -eq 0 ] || cat <<EOF\nuser: $SUDO_USER\nEOF\n',
                     '[ -n "$SUDO_USER" ] && [ "$(id -u)" -eq 0 ] && u="$SUDO_USER"\n'):
            with self.subTest(body=body):
                self.assertEqual(len(self.hits(body)), 1)

    def test_accepts_a_root_test_joined_by_and_or_its_then_branch(self):
        for body in ('if [ "$(id -u)" -eq 0 ]; then u="$SUDO_USER"; fi\n',
                     'if [ "$(id -u)" -eq 0 ]; then if [ -n "$x" ]; then :; '
                     'else u="$SUDO_USER"; fi; fi\n',
                     'if [ "$(id -u)" -eq 0 ]; then a=1 || u="$SUDO_USER"; fi\n',
                     'if [ "$(id -u)" -eq 0 ] && ! [ -n "$x" ]; then\n    u="$SUDO_USER"\nfi\n',
                     '[ "$(id -u)" -eq 0 -a -n "${SUDO_USER:-}" ] && :\n',
                     '[ "$(id -u)" -eq 0 ] && echo "a || b; c" "$SUDO_USER"\n',  # quoted: text
                     '[ "$(id -u)" -eq 0 ] && cat <<EOF\nuser: $SUDO_USER\nEOF\n',
                     self.ROOT + '    [ -n "$x" ] || u="$SUDO_USER"\nfi\n'):   # in the branch
            with self.subTest(body=body):
                self.assertEqual(self.hits(body), [])

    def test_accepts_reads_under_a_root_test(self):
        for body in ('[ "$(id -u)" -eq 0 ] && u="${SUDO_USER:-}"\n',
                     'if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then :; fi\n',
                     self.ROOT + '    u="${SUDO_USER:-$(whoami)}"\nfi\n',
                     'if [ -n "$x" ]; then\n    :\nelif [ "$(id -u)" -eq 0 ]; then\n'
                     '    u="$SUDO_USER"\nfi\n',
                     self.ROOT + '    for a in b; do\n        if [ -n "$a" ]; then\n            :\n'
                     '        else\n            u="$SUDO_USER"\n        fi\n    done\nfi\n',
                     'if [ "$(id -u)" -eq 0 ] \\\n        && [ -n "${SUDO_USER:-}" ]; then\n'
                     '    echo "$SUDO_USER"\nfi\n',
                     'if [ "$(id -u)" = "0" ]\nthen\n    u="$SUDO_USER"\nfi\n',
                     'if [[ $EUID -eq 0 ]]; then\n    u="$SUDO_USER"\nfi\n',
                     'if [[ $(id -u) == 0 ]]; then\n    u="$SUDO_USER"\nfi\n',
                     self.ROOT + '    cat <<EOF\nuser: ${SUDO_USER}\nEOF\nfi\n'):
            with self.subTest(body=body):
                self.assertEqual(self.hits(body), [])

    def test_ignores_comments_quoted_heredocs_escapes_and_marked_lines(self):
        for body in ('# USER="${SUDO_USER:-$(whoami)}"\n',
                     'echo hi  # not $SUDO_USER\n',
                     "cat <<'EOF'\nuser: $SUDO_USER\nEOF\n",
                     'echo "set \\$SUDO_USER yourself"\n',
                     'echo "$SUDO_USER"  # sudo-user-ok: only shown, never used\n'):
            with self.subTest(body=body):
                self.assertEqual(self.hits(body), [])


@unittest.skipUnless(HAVE_GIT, "needs git")
class SudoUserWalksTrackedScripts(unittest.TestCase):
    """In a git checkout, sudo-user reads every tracked *.sh, wherever it sits."""

    def test_finds_a_read_in_a_tracked_script_outside_scripts(self):
        with tempfile.TemporaryDirectory(prefix="aerodrome-ci-git-") as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
            shutil.copy2(REPO / "install.sh", root / "install.sh")
            nested = root / "tools" / "demo" / "run.sh"
            nested.parent.mkdir(parents=True)
            nested.write_text('#!/bin/bash\nowner="${SUDO_USER:-$(id -un)}"\n', encoding="utf-8")
            (root / "untracked.sh").write_text('#!/bin/bash\nu="$SUDO_USER"\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "install.sh", "tools/demo/run.sh"],
                           check=True, capture_output=True, stdin=subprocess.DEVNULL)
            errs = ci.check_sudo_user(root)
            rc, out = run_main(root, "sudo-user")
        self.assertEqual(len(errs), 1, errs)
        self.assertTrue(errs[0].startswith("tools/demo/run.sh:2: "), errs[0])
        self.assertEqual(rc, 1, out)
        self.assertIn("✗ sudo-user", out)


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
            for rel in INSTALLER_FILES:
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
