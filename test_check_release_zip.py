"""Release-zip integrity: scripts/check_release_zip.py, package-release.sh and
publish-release.sh.

  TestChecker         the checker itself, on a throwaway repo + hand-built
                      zips (fast; needs only git).
  TestPackageRelease  the real package-release.sh in a fixture built from the
                      WORKING tree (gates pre-flight, snapshot self-check,
                      cleanup trap, --verify, linked-worktree refusal).
  TestPublish         the real publish-release.sh against a stub `gh` that
                      only accepts a fake repo slug — pre-flight, the full
                      publish path and the verify loop. Nothing reaches
                      GitHub.

The two subprocess classes skip when AERODROME_SKIP_SLOW_TESTS=1, outside a
git checkout, or without git/bash/zip/unzip. Offline, no root.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from test_bump_version import (FIXTURE_IP, IN_GIT_CHECKOUT, SKIP_SLOW,
                               SLOW_SKIP_REASON, CHECKOUT_SKIP_REASON,
                               build_worktree_fixture, git, run, subprocess_env)

REPO = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "check_release_zip", REPO / "scripts" / "check_release_zip.py")
crz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crz)

HAVE_GIT = bool(shutil.which("git"))
HAVE_ZIP_TOOLS = all(shutil.which(t) for t in ("zip", "unzip", "sha256sum", "cmp"))

VER = "9.8.7"
W = f"aerodrome-v{VER}/"
FILES = {"VERSION": (b"9.8.7\n", 0o644), "server.py": (b"print('hi')\n", 0o644),
         "install.sh": (b"#!/bin/sh\necho ok\n", 0o755), "static/app.js": (b"x=1\n", 0o644)}


def check(zpath, ver, ref, repo, *extra):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = crz.main([str(zpath), ver, ref, "--repo", str(repo), *extra])
    return rc, out.getvalue()


def snapshot_tree(repo):
    """Tree id of the tracked files' working-tree content (temp index)."""
    fd, idx = tempfile.mkstemp(prefix="aerodrome-test-index.")
    os.close(fd)
    try:
        git_path = git(repo, "rev-parse", "--git-path", "index")
        shutil.copyfile(Path(repo) / git_path if not os.path.isabs(git_path) else git_path, idx)
        git(repo, "add", "-u", env={"GIT_INDEX_FILE": idx})
        return git(repo, "write-tree", env={"GIT_INDEX_FILE": idx})
    finally:
        os.unlink(idx)


def sha_sidecar(zpath):
    zpath = Path(zpath)
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    Path(str(zpath) + ".sha256").write_text(f"{digest}  {zpath.name}\n")


@unittest.skipUnless(HAVE_GIT, "needs git")
class TestChecker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aerodrome-crz-test-")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, "static"))
        git(self.repo, "-c", "init.defaultBranch=main", "init", "-q")
        for p, (data, mode) in FILES.items():
            fp = os.path.join(self.repo, p)
            with open(fp, "wb") as f:
                f.write(data)
            os.chmod(fp, mode)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "release")
        self.sha = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def make_zip(self, files=None, extra=(), wrapper=W):
        files = dict(FILES if files is None else files)
        path = os.path.join(self.tmp.name, f"z{len(os.listdir(self.tmp.name))}.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(zipfile.ZipInfo(wrapper), b"")
            for p, (data, mode) in files.items():
                zi = zipfile.ZipInfo(wrapper + p)
                zi.external_attr = (0o100000 | mode) << 16
                z.writestr(zi, data)
            for name, data, mode in extra:
                zi = zipfile.ZipInfo(name)
                zi.external_attr = mode << 16
                z.writestr(zi, data)
        return path

    def run_check(self, zpath, ver=VER, ref=None, *extra):
        return check(zpath, ver, ref or self.sha, self.repo, *extra)

    def test_identical_passes(self):
        rc, out = self.run_check(self.make_zip())
        self.assertEqual(rc, 0, out)

    def test_content_edit_fails(self):
        f = dict(FILES)
        f["server.py"] = (b"print('edited')\n", 0o644)
        rc, out = self.run_check(self.make_zip(f))
        self.assertEqual(rc, 1)
        self.assertIn("differs from", out)
        self.assertIn("server.py", out)
        self.assertIn("unzip -p", out)   # restore hint

    def test_missing_and_extra_files_fail(self):
        f = dict(FILES)
        del f["static/app.js"]
        f["audits/notes.md"] = (b"x", 0o644)
        rc, out = self.run_check(self.make_zip(f))
        self.assertEqual(rc, 1)
        self.assertIn("only in zip: audits/notes.md", out)
        self.assertIn("static/app.js", out)

    def test_exec_bit_fails(self):
        f = dict(FILES)
        f["install.sh"] = (FILES["install.sh"][0], 0o644)
        rc, out = self.run_check(self.make_zip(f))
        self.assertEqual(rc, 1)
        self.assertIn("exec bit", out)

    def test_pycache_fails(self):
        rc, out = self.run_check(self.make_zip(
            extra=[(W + "__pycache__/server.cpython-314.pyc", b"x", 0o100644)]))
        self.assertEqual(rc, 1)
        self.assertIn("caches:", out)

    def test_version_mismatch_fails(self):
        rc, out = self.run_check(self.make_zip(), "9.8.6")
        self.assertEqual(rc, 1)
        self.assertIn("version:", out)
        self.assertIn("shape:", out)

    def test_second_toplevel_fails(self):
        rc, out = self.run_check(self.make_zip(extra=[("README.md", b"x", 0o100644)]))
        self.assertEqual(rc, 1)
        self.assertIn("top-level entries", out)

    def test_zip_slip_and_symlink_fail(self):
        rc, out = self.run_check(self.make_zip(extra=[(W + "../evil", b"x", 0o100644),
                                                      (W + "lnk", b"/etc/passwd", 0o120777)]))
        self.assertEqual(rc, 1)
        self.assertIn("unsafe entry", out)
        self.assertIn("symlink", out)

    def test_wrong_commit_fails(self):
        with open(os.path.join(self.repo, "server.py"), "ab") as f:
            f.write(b"# later\n")
        git(self.repo, "commit", "-qam", "later")
        rc, out = self.run_check(self.make_zip(), VER, "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("server.py", out)

    def test_bad_ref_is_error(self):
        rc, _ = self.run_check(self.make_zip(), VER, "no-such-ref")
        self.assertEqual(rc, 2)

    def test_usage_error(self):
        rc, out = check(self.make_zip(), VER, self.sha, self.repo, "unexpected")
        self.assertEqual(rc, 2)
        self.assertIn("usage", out)

    def test_not_a_zip_fails(self):
        p = os.path.join(self.tmp.name, "trunc.zip")
        with open(p, "wb") as f:
            f.write(b"PK\x03\x04garbage")
        rc, _ = self.run_check(p)
        self.assertEqual(rc, 1)

    def test_option_missing_value(self):
        rc, out = check(self.make_zip(), VER, self.sha, self.repo, "--label")
        self.assertEqual(rc, 2, out)
        self.assertIn("--label needs a value", out)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = crz.main([self.make_zip(), VER, self.sha, "--repo"])
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn("--repo needs a value", out.getvalue())

    def test_hint_matches_the_kind_of_finding(self):
        # only in the REF (a file the zip lacks): the fix is to remove it, and
        # an unzip -p redirect would truncate it — never suggest that.
        f = dict(FILES)
        del f["static/app.js"]
        rc, out = self.run_check(self.make_zip(f))
        self.assertEqual(rc, 1)
        self.assertNotIn("unzip -p", out)
        self.assertIn("git rm", out)
        # exec bit only: chmod, not a restore
        f = dict(FILES)
        f["install.sh"] = (FILES["install.sh"][0], 0o644)
        rc, out = self.run_check(self.make_zip(f))
        self.assertEqual(rc, 1)
        self.assertNotIn("unzip -p", out)
        self.assertIn("chmod -x", out)
        # only in the zip, executable there: restore AND chmod +x
        os.remove(os.path.join(self.repo, "install.sh"))
        git(self.repo, "commit", "-qam", "drop install.sh")
        rc, out = self.run_check(self.make_zip(), VER, "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("unzip -p", out)
        self.assertIn("chmod +x", out)
        self.assertIn("install.sh", out)

    def test_no_hint(self):
        f = dict(FILES)
        f["server.py"] = (b"print('edited')\n", 0o644)
        rc, out = self.run_check(self.make_zip(f), VER, None, "--no-hint")
        self.assertEqual(rc, 1)
        self.assertIn("differs from", out)
        self.assertNotIn("unzip -p", out)

    def deny(self, paths, *args):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO("".join(p + "\n" for p in paths))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = crz.main(["--deny-paths", *args])
        finally:
            sys.stdin = old
        return rc, out.getvalue(), err.getvalue()

    def test_denylist_is_anchored_to_path_components(self):
        bad = ["HANDOFF.md", "HANDOFF-handheld.md", "docs/x-HANDOFF.md", "docs/HANDOFF-x.md",
               "handoff.md", "CLAUDE.md", "docs/claude.md", "AGENT_GUARDRAILS.md",
               "audits/plan.md", "logs/tracker.log", "sub/logs/x", ".claude/settings.json",
               "graphify-out/graph.json", ".graphifyignore", "config.yaml",
               "docs/tech-debt-audit.md", "aircraft_history.db",
               f"aerodrome-v{VER}/HANDOFF.md"]
        good = ["templates/logs.html", "config.yaml.example", "docs/dialogs/README.md",
                "static/catalogs/x.json", "docs/changelogs/a.md", "blogs/x.md",
                "scripts/graphify_helper.md", ".pii-allowlist", "update/UPDATE_README.md",
                "CHANGELOG.md", "README.md", "docs/claude-notes.txt"]
        hits, notes = crz.maintainer_paths(bad + good)
        self.assertEqual(sorted(hits), sorted(bad))
        self.assertEqual(notes, [])
        rc, out, _ = self.deny(good)
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.deny(["README.md", "docs/HANDOFF-x.md"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.split(), ["docs/HANDOFF-x.md"])

    def test_denylist_known_history_is_a_note_only_for_old_releases(self):
        old = "aerodrome-v3.4.100/docs/tech-debt-audit.md"
        rc, out, err = self.deny([old, "aerodrome-v3.4.100/server.py"], "3.4.100")
        self.assertEqual(rc, 0, out)
        self.assertIn("note", err)
        self.assertIn("tech-debt-audit.md", err)
        rc, out, _ = self.deny([old, "aerodrome-v3.4.100/HANDOFF.md"], "3.4.100")
        self.assertEqual(rc, 1)
        self.assertEqual(out.split(), ["aerodrome-v3.4.100/HANDOFF.md"])
        rc, out, _ = self.deny(["aerodrome-v3.4.109/docs/tech-debt-audit.md"], "3.4.109")
        self.assertEqual(rc, 1)
        rc, out, _ = self.deny(["docs/tech-debt-audit.md"])          # no version: strict
        self.assertEqual(rc, 1)

    def test_tree_ish_ref_and_label(self):
        # package-release.sh compares against a snapshot TREE, not a commit.
        with open(os.path.join(self.repo, "server.py"), "ab") as f:
            f.write(b"# uncommitted\n")
        tree = snapshot_tree(self.repo)
        self.assertEqual(self.run_check(self.make_zip(), VER, tree)[0], 1)
        f = dict(FILES)
        f["server.py"] = (FILES["server.py"][0] + b"# uncommitted\n", 0o644)
        rc, out = self.run_check(self.make_zip(f), VER, tree)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_check(self.make_zip(), VER, tree, "--label", "working tree")
        self.assertEqual(rc, 1)
        self.assertIn("differs from working tree: server.py", out)
        self.assertNotIn(tree, out)


class TestImportCheck(unittest.TestCase):
    """--import-check: the PACKAGED tree must import (v3.4.147). A zip built
    from this repo's top-level modules passes; dropping a module that
    main/server/collector import at load time fails, naming it — including
    updater.py, which main.py imports inside its never-fail guard."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="aerodrome-import-check-test-")
        cls.files = {p.name: p.read_bytes() for p in REPO.glob("*.py")}
        cls.files["VERSION"] = b"9.8.7\n"
        cls.files["requirements.txt"] = (REPO / "requirements.txt").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def make_zip(self, drop=(), wrapper=W):
        path = os.path.join(self.tmp.name, f"i{len(os.listdir(self.tmp.name))}.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in self.files.items():
                if name not in drop:
                    z.writestr(wrapper + name, data)
        return path

    def run_import_check(self, zpath, ver=VER):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = crz.main(["--import-check", str(zpath), ver])
        return rc, out.getvalue()

    def test_complete_tree_imports(self):
        rc, out = self.run_import_check(self.make_zip())
        self.assertEqual(rc, 0, out)
        self.assertIn("the packaged tree imports", out)

    def test_missing_update_guard_module_fails(self):
        rc, out = self.run_import_check(self.make_zip(drop={"updater.py"}))
        self.assertEqual(rc, 1, out)
        self.assertIn("main.py imports updater, which is not in the zip", out)

    def test_missing_shared_module_fails(self):
        rc, out = self.run_import_check(self.make_zip(drop={"distance.py"}))
        self.assertEqual(rc, 1, out)
        self.assertIn("imports distance, which is not in the zip", out)

    def test_usage_and_bad_zip(self):
        self.assertEqual(self.run_import_check(self.make_zip(), "x")[0], 2)
        bad = os.path.join(self.tmp.name, "not.zip")
        Path(bad).write_bytes(b"not a zip")
        self.assertEqual(self.run_import_check(bad)[0], 2)

    def test_wrong_wrapper_fails(self):
        rc, out = self.run_import_check(self.make_zip(wrapper="aerodrome-v1.2.3/"))
        self.assertEqual(rc, 1, out)


@unittest.skipIf(SKIP_SLOW, SLOW_SKIP_REASON)
@unittest.skipUnless(IN_GIT_CHECKOUT, CHECKOUT_SKIP_REASON)
@unittest.skipUnless(HAVE_ZIP_TOOLS, "needs zip, unzip, sha256sum and cmp")
class TestPackageRelease(unittest.TestCase):
    """The real scripts/package-release.sh (and the bump-version.sh check it
    runs) from the working tree, in a committed fixture with docs/."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="aerodrome-pkg-test-")
        cls.tmp = Path(cls._tmp.name)
        cls.base = build_worktree_fixture(cls.tmp / "base" / "aerodrome", include_docs=True)
        cls.ver = (cls.base / "VERSION").read_text().strip()
        cls.env = subprocess_env(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        d = Path(tempfile.mkdtemp(dir=self.tmp))
        self.t = d / "aerodrome"
        shutil.copytree(self.base, self.t, symlinks=True)
        self.pk = d / "aerodrome-packages"
        self.zip = self.pk / f"aerodrome-v{self.ver}.zip"
        self.sha = Path(str(self.zip) + ".sha256")
        self.staging = d / f"aerodrome-v{self.ver}"

    def package(self, *args, cwd=None, env=None):
        return run(["bash", "scripts/package-release.sh", *args], cwd or self.t, env or self.env)

    def assertNoArtifacts(self):
        self.assertFalse(self.zip.exists(), "zip left behind")
        self.assertFalse(self.sha.exists(), ".sha256 left behind")
        self.assertFalse(self.staging.exists(), "staging dir left behind")

    def test_01_clean_tree(self):
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("All required gates passed", out)
        self.assertIn("zip == git archive working tree", out)
        self.assertTrue(self.zip.is_file() and self.sha.is_file())
        self.assertFalse(self.staging.exists())
        rc, cout = check(self.zip, self.ver, "HEAD", self.t)
        self.assertEqual(rc, 0, cout)

    def test_02_uncommitted_edit_ships_the_working_tree(self):
        with open(self.t / "server.py", "a") as f:
            f.write("# uncommitted edit\n")
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(check(self.zip, self.ver, "HEAD", self.t)[0], 1)
        rc, cout = check(self.zip, self.ver, snapshot_tree(self.t), self.t)
        self.assertEqual(rc, 0, cout)

    def test_03_intent_to_add_and_real_index_untouched(self):
        (self.t / "new.txt").write_text("new file\n")
        git(self.t, "add", "-N", "new.txt")
        index = self.t / ".git" / "index"
        before = hashlib.sha256(index.read_bytes()).hexdigest()
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        with zipfile.ZipFile(self.zip) as z:
            self.assertIn(f"aerodrome-v{self.ver}/new.txt", z.namelist())
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), before)

    def test_04_index_lock_held(self):
        lock = self.t / ".git" / "index.lock"
        lock.write_text("")
        try:
            rc, out = self.package()
        finally:
            lock.unlink()
        self.assertEqual(rc, 0, out[-3000:])

    def test_05_broken_git_leaves_nothing(self):
        rc, out = self.package(env=dict(self.env, GIT_DIR=str(self.tmp / "no-such-git-dir")))
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertNoArtifacts()

    def test_06_tracked_file_a_strip_rule_removes(self):
        (self.t / "docs" / "tech-debt-audit.md").write_text("# audit\n")
        git(self.t, "add", "-f", "docs/tech-debt-audit.md")
        git(self.t, "commit", "-qm", "commit a stripped file")
        rc, out = self.package()
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("docs/tech-debt-audit.md", out)
        self.assertNoArtifacts()

    def test_07_changelog_top_section_must_be_version(self):
        major, minor, patch = self.ver.split(".")
        (self.t / "VERSION").write_text(f"{major}.{minor}.{int(patch) + 1}\n")
        rc, out = self.package()
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("top section", out)
        self.assertEqual(list(self.pk.glob("*")) if self.pk.exists() else [], [])

    def test_08_09_gate_failure_then_clean_retry(self):
        readme = self.t / "README.md"
        original = readme.read_text()
        readme.write_text(original + f"\nreceiver at {FIXTURE_IP}\n")
        rc, out = self.package()
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("gates failed", out)
        self.assertNoArtifacts()
        readme.write_text(original)
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        self.assertNotIn("already exist", out)

    def test_10_verify(self):
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        rc, out = self.package("--verify")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("safe to stage", out)
        with open(self.t / "collector.py", "a") as f:
            f.write("# edited after packaging\n")
        rc, out = self.package("--verify")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("differs from working tree: collector.py", out)
        self.assertIn("re-package", out)

    def test_11_linked_worktree_refused(self):
        wt = self.t.parent / "wt"
        git(self.t, "worktree", "add", "-q", str(wt), "-b", "wtb")
        for args in ((), ("--verify",)):
            rc, out = self.package(*args, cwd=wt)
            self.assertEqual(rc, 1, out[-3000:])
            self.assertIn("linked worktree", out)
        self.assertFalse((self.t.parent / "aerodrome-packages").exists())

    def test_12_help_prints_usage(self):
        rc, out = self.package("--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("Usage:", out)
        self.assertIn("--verify", out)
        self.assertIn("linked git worktree", out)
        self.assertNotIn("Version:", out)
        self.assertGreater(len(out.splitlines()), 20)

    def test_13_unknown_argument(self):
        rc, out = self.package("--bogus")
        self.assertEqual(rc, 1, out)
        self.assertIn("Unknown argument", out)

    def test_14_refused_repackage_keeps_artifacts(self):
        # After apply the zip is the source of truth: a refused re-run must
        # never delete it (the cleanup trap is armed only after this guard).
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        zip_before, sha_before = self.zip.read_bytes(), self.sha.read_bytes()
        self.staging.mkdir()          # a leftover staging dir is refused too
        rc, out = self.package()
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("already exist", out)
        self.assertTrue(self.zip.is_file(), "refused repackage deleted the zip")
        self.assertTrue(self.sha.is_file(), "refused repackage deleted the .sha256")
        self.assertEqual(self.zip.read_bytes(), zip_before)
        self.assertEqual(self.sha.read_bytes(), sha_before)
        self.assertTrue(self.staging.is_dir(), "refused repackage deleted the staging dir")

    def test_15_untracked_file_the_gates_would_see_is_refused(self):
        # The import check would import an untracked module from the working
        # tree; the zip (tracked files only) would not contain it.
        (self.t / "mod_x.py").write_text("VALUE = 1\n")
        with open(self.t / "collector.py", "a") as f:
            f.write("\nimport mod_x  # noqa\n")
        rc, out = self.package()
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("mod_x.py", out)
        self.assertIn("untracked", out)
        self.assertNoArtifacts()
        git(self.t, "add", "mod_x.py")
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        with zipfile.ZipFile(self.zip) as z:
            self.assertIn(f"aerodrome-v{self.ver}/mod_x.py", z.namelist())

    def test_16_verify_refuses_a_stray_untracked_file(self):
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        (self.t / "stray.txt").write_text("not part of the release\n")
        rc, out = self.package("--verify")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("stray.txt", out)

    def test_17_deleted_tracked_file_is_refused_wherever_it_sorts(self):
        # The last file the staging loop copies must be one whose deletion no
        # earlier gate refuses first (a deleted app module fails the import
        # check), so the fixture gets a plain file that sorts after the rest.
        last = "zz-sorts-last.txt"
        for rel in (last, "CONTRIBUTING.md"):
            with self.subTest(deleted=rel):
                d = Path(tempfile.mkdtemp(dir=self.tmp))
                t = d / "aerodrome"
                shutil.copytree(self.base, t, symlinks=True)
                if rel == last:
                    (t / rel).write_text("sorts after every other tracked file\n")
                    git(t, "add", rel)
                    git(t, "commit", "-qm", "sorts-last fixture")
                    self.assertEqual(git(t, "ls-files").splitlines()[-1], rel)
                (t / rel).unlink()
                rc, out = run(["bash", "scripts/package-release.sh"], t, self.env)
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn(f"{rel} is tracked but deleted", out)
                self.assertIn("git rm it", out)
                self.assertFalse((d / "aerodrome-packages" / self.zip.name).exists())
                self.assertFalse((d / f"aerodrome-v{self.ver}").exists())
        git(self.t, "rm", "-q", "CONTRIBUTING.md")       # a deliberate removal ships
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        with zipfile.ZipFile(self.zip) as z:
            self.assertNotIn(f"aerodrome-v{self.ver}/CONTRIBUTING.md", z.namelist())

    def test_18_maintainer_paths_refused_before_zipping_lookalikes_ship(self):
        (self.t / "docs" / "HANDOFF-notes.md").write_text("session notes\n")
        git(self.t, "add", "-f", "docs/HANDOFF-notes.md")
        git(self.t, "commit", "-qm", "oops")
        rc, out = self.package()
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("maintainer-only", out)
        self.assertIn("docs/HANDOFF-notes.md", out)
        self.assertNoArtifacts()
        git(self.t, "rm", "-q", "--cached", "docs/HANDOFF-notes.md")
        (self.t / "docs" / "HANDOFF-notes.md").unlink()
        (self.t / "docs" / "dialogs").mkdir()
        (self.t / "docs" / "dialogs" / "README.md").write_text("Dialog notes.\n")
        git(self.t, "add", "docs/dialogs/README.md")
        git(self.t, "commit", "-qm", "a legitimate path that merely contains 'logs/'")
        rc, out = self.package()
        self.assertEqual(rc, 0, out[-3000:])
        rc, out = self.package("--verify")
        self.assertEqual(rc, 0, out[-3000:])

    def test_19_strip_rule_on_a_tracked_file_fails_the_self_check(self):
        # .tracker.pid isn't a maintainer path, but the top-level strip removes
        # it: the self-check must name it (and give no restore hint — the zip
        # it would point at is deleted).
        (self.t / ".tracker.pid").write_text("12345\n")
        git(self.t, "add", "-f", ".tracker.pid")
        git(self.t, "commit", "-qm", "commit a stripped file")
        rc, out = self.package()
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("only in working tree: .tracker.pid", out)
        self.assertNotIn("unzip -p", out)
        self.assertNoArtifacts()


STUB_GH = r'''
import fnmatch, json, os, shutil, sys
SLUG = "example-owner/example-repo"
args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if "-R" in args:
    i = args.index("-R")
    if i + 1 >= len(args) or args[i + 1] != SLUG:
        sys.exit(97)
    del args[i:i + 2]
state_dir = os.environ["GH_STATE"]
os.makedirs(state_dir, exist_ok=True)
state_file = os.path.join(state_dir, "state.json")
state = {"releases": {}}
if os.path.exists(state_file):
    with open(state_file) as f:
        state = json.load(f)
def save():
    with open(state_file, "w") as f:
        json.dump(state, f)
def opt(name):
    if name in args:
        i = args.index(name)
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return None
if args[:2] == ["release", "view"] and len(args) >= 3:
    rel = state["releases"].get(args[2])
    if "--json" not in args:
        sys.exit(0 if rel else 1)
    if not rel:
        sys.exit(1)
    print(json.dumps({"tagName": args[2], "name": rel["title"], "isDraft": False,
                      "isPrerelease": False,
                      "assets": [{"name": n} for n in rel["assets"]]}))
    sys.exit(0)
if args[:2] == ["release", "create"] and len(args) >= 3:
    tag = args[2]
    del args[:3]
    target, title, notes = opt("--target"), opt("--title"), opt("--notes-file")
    if None in (target, title, notes) or any(a.startswith("-") for a in args):
        sys.exit(97)
    d = os.path.join(state_dir, tag)
    os.makedirs(d)
    shutil.copy(notes, os.path.join(d, "notes.md"))
    for a in args:
        shutil.copy(a, os.path.join(d, os.path.basename(a)))
    # Like GitHub: a tag that already exists keeps its commit; --target is
    # only used to CREATE a missing tag.
    tags = state.setdefault("tags", {})
    tags.setdefault(tag, target)
    state["releases"][tag] = {"target": tags[tag], "title": title,
                              "assets": [os.path.basename(a) for a in args]}
    state["latest"] = tag
    save()
    sys.exit(0)
if args[:2] == ["release", "download"] and len(args) >= 3:
    tag = args[2]
    del args[:3]
    pat = opt("-p") or "*"
    rel = state["releases"].get(tag)
    if not rel:
        sys.exit(1)
    src = os.environ.get("GH_SERVE_DIR") or os.path.join(state_dir, tag)
    for n in rel["assets"]:
        if fnmatch.fnmatch(n, pat):
            shutil.copy(os.path.join(src, n), n)
    sys.exit(0)
if args[:1] == ["api"] and len(args) >= 2:
    prefix = "repos/" + SLUG + "/"
    if not args[1].startswith(prefix):
        sys.exit(97)
    rest = args[1][len(prefix):]
    if rest.startswith("commits/"):
        ref = rest[len("commits/"):]
        rel = state["releases"].get(ref)
        if rel:
            print(os.environ.get("GH_FAKE_SHA") or rel["target"])
            sys.exit(0)
        if ref in state.get("tags", {}):        # a tag without a release
            print(state["tags"][ref])
            sys.exit(0)
        sys.stderr.write(f"gh: No commit found for SHA: {ref} (HTTP 422)\n")
        sys.exit(1)
    if rest == "releases/latest":
        print(state.get("latest", ""))
        sys.exit(0)
sys.exit(97)
'''


def changelog_section(text, ver):
    out, grab = [], False
    for line in text.splitlines(keepends=True):
        if re.match(rf"^## \[{re.escape(ver)}\]", line):
            grab = True
            continue
        if grab and line.startswith("## ["):
            break
        if grab:
            out.append(line)
    return "".join(out)


@unittest.skipIf(SKIP_SLOW, SLOW_SKIP_REASON)
@unittest.skipUnless(IN_GIT_CHECKOUT, CHECKOUT_SKIP_REASON)
@unittest.skipUnless(HAVE_ZIP_TOOLS, "needs zip, unzip, sha256sum and cmp")
class TestPublish(unittest.TestCase):
    """The real scripts/publish-release.sh (REPO sed'd to a fake slug) against
    a stub `gh` that refuses any other slug."""
    SLUG = "example-owner/example-repo"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="aerodrome-pub-test-")
        cls.tmp = Path(cls._tmp.name)
        cls.base = build_worktree_fixture(cls.tmp / "base" / "aerodrome")
        p = cls.base / "scripts" / "publish-release.sh"
        text = p.read_text()
        new, n = re.subn(r'(?m)^REPO=.*$', f'REPO="{cls.SLUG}"', text, count=1)
        assert n == 1, "REPO= line not found in publish-release.sh"
        p.write_text(new)
        git(cls.base, "commit", "-qam", "point publish-release.sh at the fake slug")
        cls.ver = (cls.base / "VERSION").read_text().strip()
        cls.tag = f"v{cls.ver}"
        cls.env = subprocess_env(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=self.tmp))
        self.t = self.d / "aerodrome"
        shutil.copytree(self.base, self.t, symlinks=True)
        self.pk = self.d / "aerodrome-packages"
        self.pk.mkdir()
        self.zip = self.pk / f"aerodrome-{self.tag}.zip"
        ghbin = self.d / "ghbin"
        ghbin.mkdir()
        (ghbin / "gh").write_text(f"#!{sys.executable}\n" + STUB_GH)
        (ghbin / "gh").chmod(0o755)
        self.gh_log = self.d / "gh.log"
        self.gh_state = self.d / "ghstate"
        self.penv = dict(self.env, PATH=os.pathsep.join([str(ghbin), self.env["PATH"]]),
                         GH_LOG=str(self.gh_log), GH_STATE=str(self.gh_state))

    def make_zip(self, ref="HEAD", dest=None):
        dest = Path(dest or self.zip)
        git(self.t, "archive", "--format=zip", f"--prefix=aerodrome-{self.tag}/", ref,
            "-o", str(dest))
        sha_sidecar(dest)
        return dest

    def publish(self, *args, env=None):
        return run(["bash", "scripts/publish-release.sh", self.ver, *args], self.t,
                   env or self.penv)

    def gh_calls(self):
        if not self.gh_log.exists():
            return []
        return [json.loads(l) for l in self.gh_log.read_text().splitlines() if l]

    def test_01_check_only_good_zip(self):
        self.make_zip()
        rc, out = self.publish("--target", "HEAD", "--check-only")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("nothing published", out)
        self.assertEqual(self.gh_calls(), [])

    def test_02_tampered_zip(self):
        self.make_zip()
        src = self.zip.read_bytes()
        tampered = self.d / "t.zip"
        with zipfile.ZipFile(io.BytesIO(src)) as zi, zipfile.ZipFile(tampered, "w") as zo:
            for info in zi.infolist():
                data = zi.read(info)
                if info.filename.endswith("/server.py"):
                    data = data[:-2] + bytes([data[-2] ^ 1]) + data[-1:]
                zo.writestr(info, data)
        shutil.move(tampered, self.zip)
        sha_sidecar(self.zip)
        rc, out = self.publish("--target", "HEAD", "--check-only")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("server.py", out)
        self.assertEqual(self.gh_calls(), [])

    def test_03_wrong_target(self):
        self.make_zip()
        rc, out = self.publish("--target", "HEAD~1", "--check-only")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("Zip is not git archive", out)
        self.assertEqual(self.gh_calls(), [])

    def test_04_trailing_target(self):
        self.make_zip()
        rc, out = self.publish("--target")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("needs a commit", out)
        self.assertEqual(self.gh_calls(), [])

    def test_04b_check_only_with_verify_only_refused(self):
        rc, out = self.publish("--target", "HEAD", "--check-only", "--verify-only")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertEqual(self.gh_calls(), [])

    def test_05_committed_maintainer_file(self):
        (self.t / "HANDOFF.md").write_text("maintainer notes\n")
        git(self.t, "add", "-f", "HANDOFF.md")
        git(self.t, "commit", "-qm", "oops")
        self.make_zip()
        rc, out = self.publish("--target", "HEAD", "--check-only")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("maintainer-only", out)
        self.assertIn("HANDOFF.md", out)
        self.assertEqual(self.gh_calls(), [])

    def test_06_notes_come_from_the_target(self):
        cl = self.t / "CHANGELOG.md"
        good = cl.read_text()
        cl.write_text(good.replace(f"## [{self.ver}]", f"## [{self.ver}-draft]", 1))
        git(self.t, "commit", "-qam", "no section at the target")
        self.make_zip()
        cl.write_text(good)          # the WORKING tree has the section; the target doesn't
        rc, out = self.publish("--target", "HEAD", "--check-only")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn(f"No CHANGELOG section for [{self.ver}]", out)
        self.assertEqual(self.gh_calls(), [])

    def test_07_full_publish(self):
        self.make_zip()
        head = git(self.t, "rev-parse", "HEAD")
        committed = changelog_section(git(self.t, "show", "HEAD:CHANGELOG.md") + "\n", self.ver)
        self.assertTrue(committed.strip())
        cl = self.t / "CHANGELOG.md"            # a later, uncommitted edit must not leak in
        cl.write_text(cl.read_text().replace(f"## [{self.ver}]",
                                             f"## [{self.ver}]\n\nUNCOMMITTED EDIT\n", 1))
        rc, out = self.publish("--target", "HEAD")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("Done.", out)
        self.assertIn("Published .sha256 == local .sha256", out)
        self.assertIn(f"zip == git archive tag {self.tag}", out)
        creates = [c for c in self.gh_calls() if c[:2] == ["release", "create"]]
        self.assertEqual(len(creates), 1)
        c = creates[0]
        self.assertEqual(c[c.index("--target") + 1], head)
        self.assertEqual(c[c.index("--title") + 1], self.tag)
        self.assertEqual(c[c.index("-R") + 1], self.SLUG)
        self.assertIn("--notes-file", c)
        notes = (self.gh_state / self.tag / "notes.md").read_text()
        self.assertTrue(notes.strip())
        self.assertNotIn("UNCOMMITTED EDIT", notes)
        self.assertEqual(notes.strip(), committed.strip())
        for call in self.gh_calls():
            if "-R" in call:
                self.assertEqual(call[call.index("-R") + 1], self.SLUG)

    def test_08_tag_resolves_elsewhere(self):
        self.make_zip()
        other = git(self.t, "rev-parse", "HEAD~1")
        rc, out = self.publish("--target", "HEAD", env=dict(self.penv, GH_FAKE_SHA=other))
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("resolves to", out)

    def test_09_served_zip_differs_from_local(self):
        self.make_zip()
        serve = self.d / "serve"
        serve.mkdir()
        self.make_zip(ref="HEAD~1", dest=serve / self.zip.name)   # self-consistent, but not ours
        rc, out = self.publish("--target", "HEAD", env=dict(self.penv, GH_SERVE_DIR=str(serve)))
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("differs from the local one", out)

    def test_10_verify_only_needs_the_tag_commit_locally(self):
        self.make_zip()
        rc, out = self.publish("--target", "HEAD")
        self.assertEqual(rc, 0, out[-3000:])
        rc, out = self.publish("--verify-only")
        self.assertEqual(rc, 0, out[-3000:])
        rc, out = self.publish("--verify-only", env=dict(self.penv, GH_FAKE_SHA="f" * 40))
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("git fetch origin --tags", out)

    def test_11_existing_release_refused(self):
        self.make_zip()
        self.assertEqual(self.publish("--target", "HEAD")[0], 0)
        rc, out = self.publish("--target", "HEAD")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("already exists", out)

    def seed_gh_state(self, state):
        self.gh_state.mkdir(parents=True, exist_ok=True)
        (self.gh_state / "state.json").write_text(json.dumps(state))

    def creates(self):
        return [c for c in self.gh_calls() if c[:2] == ["release", "create"]]

    def test_12_local_tag_elsewhere_refused_before_any_gh_call(self):
        # gh release create IGNORES --target when the tag already exists.
        self.make_zip()
        git(self.t, "tag", self.tag, "HEAD~1")
        rc, out = self.publish("--target", "HEAD", "--check-only")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn(f"local tag {self.tag} points at", out)
        self.assertEqual(self.gh_calls(), [])
        git(self.t, "tag", "-f", self.tag, "HEAD")      # a tag AT the target is fine
        rc, out = self.publish("--target", "HEAD", "--check-only")
        self.assertEqual(rc, 0, out[-3000:])

    def test_13_github_tag_elsewhere_refused_before_create(self):
        self.make_zip()
        self.seed_gh_state({"releases": {}, "tags": {self.tag: git(self.t, "rev-parse", "HEAD~1")}})
        rc, out = self.publish("--target", "HEAD")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("already exists on GitHub", out)
        self.assertEqual(self.creates(), [])

    def test_14_github_tag_at_the_target_publishes(self):
        self.make_zip()
        self.seed_gh_state({"releases": {}, "tags": {self.tag: git(self.t, "rev-parse", "HEAD")}})
        rc, out = self.publish("--target", "HEAD")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(len(self.creates()), 1)

    def _seed_published_old_release(self, ver, extra_files):
        """Commit VERSION=ver plus extra_files (force-added), and publish its
        `git archive` zip into the stub's state as v<ver>."""
        (self.t / "VERSION").write_text(ver + "\n")
        for rel, text in extra_files.items():
            (self.t / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.t / rel).write_text(text)
            git(self.t, "add", "-f", rel)
        git(self.t, "commit", "-qam", f"v{ver}")
        tag = f"v{ver}"
        rel_dir = self.gh_state / tag
        rel_dir.mkdir(parents=True, exist_ok=True)
        z = rel_dir / f"aerodrome-{tag}.zip"
        git(self.t, "archive", "--format=zip", f"--prefix=aerodrome-{tag}/", "HEAD", "-o", str(z))
        sha_sidecar(z)
        self.seed_gh_state({"releases": {tag: {"target": git(self.t, "rev-parse", "HEAD"),
                                               "title": tag,
                                               "assets": [z.name, z.name + ".sha256"]}},
                            "latest": self.tag})

    def test_15_verify_only_old_release_with_known_history_file(self):
        # v3.4.60–v3.4.108 shipped docs/tech-debt-audit.md (tracked until
        # v3.4.109, PII-clean): re-checking one is a note, not "pull it NOW".
        old = "3.4.100"
        self._seed_published_old_release(old, {"docs/tech-debt-audit.md": "# audit\n"})
        rc, out = run(["bash", "scripts/publish-release.sh", old, "--verify-only"], self.t, self.penv)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("note", out)
        self.assertIn("tech-debt-audit.md", out)
        self._seed_published_old_release(old, {"HANDOFF.md": "maintainer notes\n"})
        rc, out = run(["bash", "scripts/publish-release.sh", old, "--verify-only"], self.t, self.penv)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("pull the release NOW", out)
        self.assertIn("HANDOFF.md", out)

    def test_16_lookalike_path_is_not_a_maintainer_file(self):
        (self.t / "docs" / "dialogs").mkdir(parents=True)
        (self.t / "docs" / "dialogs" / "README.md").write_text("Dialog notes.\n")
        git(self.t, "add", "docs/dialogs/README.md")
        git(self.t, "commit", "-qm", "dialogs")
        self.make_zip()
        rc, out = self.publish("--target", "HEAD", "--check-only")
        self.assertEqual(rc, 0, out[-3000:])

    def test_17_help_and_canonical_version(self):
        rc, out = run(["bash", "scripts/publish-release.sh", "--help"], self.t, self.penv)
        self.assertEqual(rc, 0, out)
        self.assertIn("Usage:", out)
        self.assertIn("--check-only", out)
        self.assertNotIn("set -euo", out)
        major, minor, patch = self.ver.split(".")
        rc, out = run(["bash", "scripts/publish-release.sh", f"{major}.{minor}.0{patch}",
                       "--target", "HEAD", "--check-only"], self.t, self.penv)
        self.assertEqual(rc, 1, out)
        self.assertIn("canonical", out)
        self.assertEqual(self.gh_calls(), [])


if __name__ == "__main__":
    unittest.main()
