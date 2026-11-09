"""Tests for scripts/check_installers.py — the installer-consistency gate
bump-version.sh runs on every bump and package.

Fast (~1 s): the subprocesses are `bash -n`, render_sudoers() run alone in
bash, and git for priv-bump's fixture repos. The clean-tree tests run on this
tree (a git checkout or an installed release — the script falls back to
globbing outside git, and priv-bump is SKIPPED there); the negative cases
copy the relevant files into a temp dir and mutate exactly one thing.
"""
import contextlib
import importlib.util
import io
import re
import shutil
import subprocess
import sys
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
                   "scripts/recover-sudoers.sh", "ntfy_installer.py",
                   "scripts/aerodrome-helper", "privhelper.py", "bump-version.sh")
NO_BASH_CHECKS = ("pkg-detect,recover-delegates,sudo-user,pipefail-readers,ntfy-cache,"
                  "priv-version,sudo-callsites,ntfy-yml-shared,helper-api-floor,helper-not-stamped")
IN_GIT = ci.tracked(REPO, "*.sh") is not None


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
            if name == "priv-bump" and not (IN_GIT and HAVE_GIT):
                self.assertIn("– priv-bump SKIPPED", out)       # an unpacked release
                continue
            self.assertIn(f"✓ {name}", out)

    def test_the_v6_gates_on_this_tree(self):
        for fn in (ci.check_priv_version, ci.check_sudo_callsites, ci.check_ntfy_yml_shared,
                   ci.check_helper_api_floor, ci.check_helper_not_stamped):
            with self.subTest(fn.__name__):
                self.assertEqual(fn(REPO), [])

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
        # (some tests call this again for a fresh copy: each is cleaned up)
        self._tmp = tempfile.TemporaryDirectory(prefix="aerodrome-ci-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for rel in INSTALLER_FILES:
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dst)

    def mutate(self, rel, old, new, count=1):
        p = self.root / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text, f"fixture anchor missing from {rel}")
        p.write_text(text.replace(old, new, count), encoding="utf-8")

    def append(self, rel, text):
        p = self.root / rel
        p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")

    def assertOnlyFinding(self, *checks):
        rc, out = run_main(self.root) if HAVE_BASH else run_main(self.root, NO_BASH_CHECKS)
        self.assertEqual(rc, 1, out)
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith("✗")]
        self.assertEqual(failed, [f"✗ {c}" for c in checks], out)

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

    def test_edited_selinux_blocks_home_line(self):
        self.mutate("install.sh", "local dir home", "local dir home extra")
        errs = ci.check_pkg_detect(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("selinux_blocks_home()", errs[0])
        self.assertOnlyFinding("pkg-detect")

    def test_missing_selinux_blocks_home(self):
        self.mutate("scripts/bootstrap.sh", "selinux_blocks_home() {", "selinux_blocks_home_x() {")
        errs = ci.check_pkg_detect(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("selinux_blocks_home()", errs[0])
        self.assertOnlyFinding("pkg-detect")

    # -- recover-delegates -------------------------------------------------------
    def test_rule_line_added_to_recover(self):
        self.append("scripts/recover-sudoers.sh",
                    "# svc-a ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart aerodrome\n")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("rule text", errs[0])
        self.assertOnlyFinding("recover-delegates")

    def test_marker_added_to_recover(self):
        self.append("scripts/recover-sudoers.sh", "# SUDOERS_VERSION: 6\n")
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
        # server.py would read this line, not the one install.sh writes
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
        self.assertOnlyFinding("recover-delegates", "priv-version")    # 4 is no longer 6

    def test_quoted_heredoc_opener(self):
        # << 'SUDOEOF' installs a literal ${u} (visudo rejects it) while the
        # rule TEXT looks right — the opener must be checked.
        self.mutate("install.sh", "cat << SUDOEOF\n", "cat << 'SUDOEOF'\n")
        errs = ci.check_recover_delegates(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("quoted", errs[0])
        # (and the rules it renders aren't Aerodrome's: a literal ${u})
        self.assertOnlyFinding("recover-delegates", "sudoers-boundary")

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
    PURGE_LOOP = ("for p in /var/lib/private/aerodrome-ntfy/cache.db \\\n"
                  "                     /var/lib/private/aerodrome-ntfy/cache.db-wal \\\n")

    def test_ntfy_cache_only_in_a_comment(self):
        p = self.root / "uninstall.sh"
        block = ci.ntfy_purge_block(p.read_text(encoding="utf-8"))
        self.assertIn("/var/lib/ntfy/cache.db-shm", block)
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        p.write_text("".join(l if l.lstrip().startswith("#")
                             else l.replace("/var/lib/ntfy/cache.db", "/var/lib/ntfy/other.db")
                             for l in lines)
                     + "# purge /var/lib/ntfy/cache.db some day\n", encoding="utf-8")
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 3, errs)        # the file, its -wal and its -shm
        for e, side in zip(errs, ("", "-wal", "-shm")):
            self.assertIn(f"never removes /var/lib/ntfy/cache.db{side} ", e)
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_wal_alone_missing(self):
        """cache.db-wal must not pass because cache.db is on the list."""
        self.mutate("uninstall.sh", "/var/lib/private/aerodrome-ntfy/cache.db-wal \\\n                     /var/lib/private/aerodrome-ntfy/cache.db-shm",
                    "/var/lib/private/aerodrome-ntfy/other.db-wal \\\n                     /var/lib/private/aerodrome-ntfy/cache.db-shm")
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("never removes /var/lib/private/aerodrome-ntfy/cache.db-wal ", errs[0])
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_a_cache_left_out_of_the_helpers_purge(self):
        self.mutate("scripts/aerodrome-helper",
                    'NTFY_PURGE_DIRS = ("/var/lib/private/aerodrome-ntfy", "/var/lib/ntfy")',
                    'NTFY_PURGE_DIRS = ("/var/lib/private/aerodrome-ntfy",)')
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("NTFY_PURGE_DIRS lacks /var/lib/ntfy", errs[0])
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_follows_the_units_state_directory(self):
        for rel in ("ntfy_installer.py", "scripts/aerodrome-helper"):     # the shared block
            self.mutate(rel, "StateDirectory=aerodrome-ntfy\n", "StateDirectory=ntfy-moved\n")
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("--cache-file isn't in its StateDirectory=", errs[0])
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_constants_moved_fail_closed(self):
        self.mutate("scripts/aerodrome-helper", 'NTFY_CACHE_NAMES = ("cache.db",',
                    'NTFY_CACHE_NAMES = tuple(["cache.db"]) + (')
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("isn't a literal there any more", errs[0])

    def test_ntfy_cache_uninstall_must_use_the_helper(self):
        self.mutate("uninstall.sh", 'sudo "$HELPER" ntfy-uninstall', 'sudo "$HELPER" ntfy-uninstall-x')
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("never runs the helper's ntfy-uninstall", errs[0])
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_size_prompt_is_not_a_removal(self):
        """The prompt's `sudo du` list (NTFY_CACHES) names the files but
        removes nothing: dropping one from the purge loop is a finding."""
        self.assertIn('sudo du -shc -- "${NTFY_CACHES[@]}"',
                      (self.root / "uninstall.sh").read_text(encoding="utf-8"))
        self.mutate("uninstall.sh", self.PURGE_LOOP,
                    "for p in /var/lib/private/aerodrome-ntfy/cache.db-wal \\\n")
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("never removes /var/lib/private/aerodrome-ntfy/cache.db ", errs[0])
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_named_outside_the_purge_branch_is_not_a_removal(self):
        self.mutate("uninstall.sh", "/var/lib/ntfy/cache.db-wal \\\n"
                    "                     /var/lib/ntfy/cache.db-shm; do",
                    "/var/lib/ntfy/cache.db-wal; do")
        self.mutate("uninstall.sh", '        if [ "$should_purge_ntfy_data" = true ]; then\n',
                    '        echo "would purge /var/lib/ntfy/cache.db-shm"\n'
                    '        if [ "$should_purge_ntfy_data" = true ]; then\n')
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("never removes /var/lib/ntfy/cache.db-shm ", errs[0])
        self.assertOnlyFinding("ntfy-cache")

    def test_ntfy_cache_purge_branch_missing_fails_closed(self):
        self.mutate("uninstall.sh", 'if [ "$should_purge_ntfy_data" = true ]; then',
                    'if [ "${should_purge_ntfy_data}" = true ]; then')
        errs = ci.check_ntfy_cache(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn('no `if [ "$should_purge_ntfy_data" = true ]; then`', errs[0])

    # -- the privilege boundary (v3.4.150) -------------------------------------------
    def test_priv_version_line_two(self):
        self.mutate("scripts/aerodrome-helper", "# PRIV_VERSION = 6\n", "# PRIV_VERSION = 7\n")
        errs = ci.check_priv_version(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("line 2 says 7", errs[0])
        self.assertOnlyFinding("priv-version")

    def test_priv_version_constant(self):
        self.mutate("scripts/aerodrome-helper", "\nPRIV_VERSION = 6\n", "\nPRIV_VERSION = 5\n")
        self.assertOnlyFinding("priv-version")

    def test_priv_version_line_two_moved(self):
        self.mutate("scripts/aerodrome-helper", "# PRIV_VERSION = 6\n", "# PRIV_VERSION: 6\n")
        errs = ci.check_priv_version(self.root)
        self.assertIn("line 2 must be exactly", errs[0])

    def test_sudo_callsites(self):
        (self.root / "extra.py").write_text(
            'import subprocess\nsubprocess.run(["sudo", "-n", "tee", "/etc/x"])\n'
            'subprocess.run(("sudo", "true"))\n')
        errs = ci.check_sudo_callsites(self.root)
        self.assertEqual([e.split(":")[:2] for e in errs], [["extra.py", "2"], ["extra.py", "3"]])
        self.assertOnlyFinding("sudo-callsites")
        (self.root / "test_extra.py").write_text('X = ["sudo", "-n", "cat"]\n')   # tests may
        self.assertEqual(len(ci.check_sudo_callsites(self.root)), 2)

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_sudoers_boundary(self):
        """An allowlist, not a denylist: anything but exactly the grants
        Aerodrome means is one finding — root-equivalent shapes that write no
        file (a shell, link + start) included."""
        for line, why in (
                ("${u} ALL=(root) NOPASSWD: /usr/bin/tee /etc/ntfy/server.yml", "writing files"),
                ("${u} ALL=(root) NOPASSWD: /bin/rm -f /usr/local/bin/ntfy", "writing files"),
                ("${u} ALL=(root) NOPASSWD: /usr/bin/install -m 0755 /tmp/x /usr/local/bin/ntfy",
                 "writing files"),
                ("${u} ALL=(root) NOPASSWD: /bin/sh", "isn't one of the commands"),
                ("${u} ALL=(root) NOPASSWD: /usr/bin/systemctl link *", "no wildcards"),
                ("${u} ALL=(root) NOPASSWD: /usr/bin/systemctl start ntfy", "isn't one of the commands"),
                ("${u} ALL=(root) NOPASSWD: ALL", "no wildcards"),
                ("${u} ALL=(root) NOPASSWD: sudoedit /etc/ntfy/server.yml", "no wildcards"),
                ("${u} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart aerodrome", "every rule line"),
                ("${u} ALL=(root) /usr/bin/systemctl restart aerodrome", "every rule line"),
                ("Defaults:${u} !env_reset", "every rule line"),
                ("Cmnd_Alias X = /bin/sh", "every rule line"),
                ("#includedir /srv/example/sudoers.d", "no includes")):
            with self.subTest(line=line):
                self.setUp()
                self.mutate("install.sh", "\nSUDOEOF\n", f"\n{line}\nSUDOEOF\n")
                errs = ci.check_sudoers_boundary(self.root)
                self.assertEqual(len(errs), 1, errs)
                self.assertIn(why, errs[0])
                self.assertOnlyFinding("sudoers-boundary")

    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_sudoers_boundary_a_grant_taken_out(self):
        self.mutate("install.sh", "NOPASSWD: /bin/cat /etc/sudoers.d/aerodrome, ", "NOPASSWD: ")
        errs = ci.check_sudoers_boundary(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("no longer grants `/bin/cat /etc/sudoers.d/aerodrome`", errs[0])
        self.assertOnlyFinding("sudoers-boundary")

    def test_ntfy_yml_shared(self):
        self.mutate("scripts/aerodrome-helper", 'NTFY_UPSTREAM = "https://ntfy.sh"',
                    'NTFY_UPSTREAM = "https://ntfy.example.net"')
        errs = ci.check_ntfy_yml_shared(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("ntfy.example.net", errs[0])
        self.assertOnlyFinding("ntfy-yml-shared")

    def test_helper_api_floor(self):
        cases = {"tomllib": "import tomllib\n",
                 "rmtree dir_fd": "import shutil\nshutil.rmtree('x', dir_fd=3)\n",
                 "except*": "try:\n    pass\nexcept* ValueError:\n    pass\n",
                 "ExceptionGroup": "E = ExceptionGroup\n",
                 "datetime.UTC": "import datetime\nT = datetime.UTC\n",
                 "possessive": "import re\nR = re.compile(r'a++b')\n",
                 "atomic group": "import re\nR = re.compile(r'(?>ab)c')\n",
                 "not stdlib": "import yaml\n",
                 "from-import": "from datetime import UTC\n",
                 "from-import as": "from hashlib import file_digest as fd\n",
                 "aliased module": "import shutil as _sh\n_sh.rmtree('x', dir_fd=3)\n",
                 "from-imported call": "from shutil import rmtree as rt\nrt('x', dir_fd=3)\n",
                 "aliased attribute": "import enum as _e\nE = _e.StrEnum\n"}
        for label, code in cases.items():
            with self.subTest(label):
                self.setUp()
                self.append("scripts/aerodrome-helper", "\n" + code)
                errs = ci.check_helper_api_floor(self.root)
                self.assertTrue(errs, label)
                if label == "except*" and sys.version_info < (3, 11):
                    continue      # the gate's own Python can't parse it: other checks fail too
                self.assertOnlyFinding("helper-api-floor")

    def test_helper_not_stamped(self):
        self.mutate("bump-version.sh", '    "uninstall.sh"\n',
                    '    "uninstall.sh"\n    "scripts/aerodrome-helper"\n')
        errs = ci.check_helper_not_stamped(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertOnlyFinding("helper-not-stamped")

    # -- bash-syntax -------------------------------------------------------------
    @unittest.skipUnless(HAVE_BASH, "needs bash")
    def test_syntax_error(self):
        self.append("scripts/recover-sudoers.sh", "\nif then\n")
        errs = ci.check_bash_syntax(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("recover-sudoers.sh", errs[0])
        self.assertOnlyFinding("bash-syntax")


class HelperAndUninstallerPurgeTheSameFiles(unittest.TestCase):
    """The helper's purge ("Also delete cached messages", uninstall.sh with
    the helper) and uninstall.sh's own purge branch (a system without the
    helper) remove exactly the same paths."""

    PATH_RE = re.compile(r"(?<![\w.$])/(?:var|etc|usr|opt|srv|home)/[^\s\"';|&)\\]+")

    def test_same_paths_both_ways(self):
        helper = (REPO / "scripts" / "aerodrome-helper").read_text(encoding="utf-8")
        dirs = ci._module_constant(helper, "NTFY_PURGE_DIRS")
        names = ci._module_constant(helper, "NTFY_CACHE_NAMES")
        paths = sorted(f"{d}/{n}" for d in dirs for n in names)
        self.assertEqual(len(paths), 6)
        block = ci.ntfy_purge_block((REPO / "uninstall.sh").read_text(encoding="utf-8"))
        self.assertIsNotNone(block, "no purge branch in uninstall.sh")
        self.assertEqual(sorted(set(self.PATH_RE.findall(block))), paths)


@unittest.skipUnless(HAVE_BASH and HAVE_GIT, "needs bash and git")
class PrivBump(unittest.TestCase):
    """priv-bump against a tag in a throwaway repo: a helper or rule change
    needs a higher SUDOERS_VERSION than the tag's; a comment doesn't."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aerodrome-ci-bump-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.git("init", "-q")
        for rel in ("install.sh", "scripts/aerodrome-helper"):
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dst)
        self.commit("release")
        self.git("tag", "v1.0.0")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t",
                               "-c", "user.email=t@example.net", *args],
                              check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def edit(self, rel, old, new):
        p = self.root / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def bump(self):
        self.edit("install.sh", "# SUDOERS_VERSION: 6\n", "# SUDOERS_VERSION: 7\n")

    def test_unchanged_passes(self):
        self.assertEqual(ci.check_priv_bump(self.root), [])

    def test_a_helper_change_needs_a_bump(self):
        self.edit("scripts/aerodrome-helper", "LOCK_WAIT = 10.0", "LOCK_WAIT = 11.0")
        errs = ci.check_priv_bump(self.root)          # even uncommitted
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("scripts/aerodrome-helper changed", errs[0])
        self.bump()
        self.assertEqual(ci.check_priv_bump(self.root), [])

    def test_a_rule_change_needs_a_bump(self):
        self.edit("install.sh", "/usr/bin/cat /etc/sudoers.d/aerodrome\n",
                  "/usr/bin/cat /etc/sudoers.d/aerodrome, /bin/true\n")
        errs = ci.check_priv_bump(self.root)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("rule lines", errs[0])
        self.bump()
        self.assertEqual(ci.check_priv_bump(self.root), [])

    def test_a_comment_in_render_sudoers_needs_none(self):
        self.edit("install.sh", "# Read this file, so the updater", "# Read the file, so the updater")
        self.commit("comment")
        self.assertEqual(ci.check_priv_bump(self.root), [])

    def test_skipped_without_a_tag_before_the_carrier_or_git(self):
        self.git("tag", "-d", "v1.0.0")
        with self.assertRaises(ci.Skipped):
            ci.check_priv_bump(self.root)
        self.edit("install.sh", "render_sudoers() {", "render_rules() {")
        self.commit("old layout")
        self.git("tag", "v0.9.0")
        self.edit("install.sh", "render_rules() {", "render_sudoers() {")
        with self.assertRaises(ci.Skipped):
            ci.check_priv_bump(self.root)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ci.Skipped):
                ci.check_priv_bump(d)
        rc, out = run_main(self.root, "priv-bump")
        self.assertEqual(rc, 0, out)
        self.assertIn("– priv-bump SKIPPED", out)


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
