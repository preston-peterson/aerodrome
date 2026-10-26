"""Release-gate tests for bump-version.sh.

Covers the gates (Python syntax, import, name resolution, JavaScript parse
with the classic-script grammar, type-shape, installer consistency, PII,
documentation images), the gates-before-writes ordering, skipped gates
being reported, `set` validation, `check`, the idempotent re-run (and the
already-packaged warning), the PII audit's scope, the verbatim CHANGELOG
entry and the deterministic Overview PDF.

Each case copies the WORKING tree — git's tracked + untracked-not-ignored
files, so new not-yet-added scripts are the ones under test — into a fresh
temp git repo, injects one fault, and runs the real bump-version.sh there.
Offline (a dead proxy), no root, never touches the source tree.

Skipped when AERODROME_SKIP_SLOW_TESTS=1 (fast local iteration — these
subprocess suites are REQUIRED before a bump), outside a git checkout (an
installed release has no .git), or without git/bash. Where node is missing
or older than 18 a stub `node` stands in, and the cases that need a real
JavaScript parser skip.

build_worktree_fixture() is shared with test_check_release_zip.py.
"""
import hashlib
import importlib.util
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
SKIP_SLOW = os.environ.get("AERODROME_SKIP_SLOW_TESTS") == "1"
SLOW_SKIP_REASON = "AERODROME_SKIP_SLOW_TESTS=1 (subprocess release-pipeline suite)"
IN_GIT_CHECKOUT = bool((REPO / ".git").exists()        # a linked worktree's .git is a file
                       and shutil.which("git") and shutil.which("bash"))
CHECKOUT_SKIP_REASON = "needs a git checkout of the repo plus git and bash"

FIXTURE_SKIP_PREFIX = ("docs/",)
FIXTURE_SKIP_EXT = (".png", ".pdf", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".db")

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
# A private-network address assembled at runtime so no literal sits in a
# tracked file for the PII audit (or the pre-commit guard) to trip on.
FIXTURE_IP = ".".join(["10", "99", "88", "77"])


def _clean_environ():
    """os.environ minus anything that could steer git or the bump."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("AERODROME_", "GIT_"))}


def git(cwd, *args, check=True, env=None):
    e = dict(_clean_environ(), **GIT_ENV)
    if env:
        e.update(env)
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, env=e, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def worktree_files(include_docs=False):
    """git-visible files of the working tree (tracked + untracked-not-ignored)."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"],
        capture_output=True, check=True, env=dict(_clean_environ(), **GIT_ENV)).stdout
    files = sorted({f for f in out.decode("utf-8", "surrogateescape").split("\0") if f})
    if not include_docs:
        files = [f for f in files if not f.startswith(FIXTURE_SKIP_PREFIX)
                 and not f.lower().endswith(FIXTURE_SKIP_EXT)]
    return files


def build_worktree_fixture(dest, include_docs=False):
    """Copy the working tree's git-visible files into `dest` (minus docs/ and
    binaries unless include_docs), `git init` it and commit. The cached
    scripts/doc8643.csv (gitignored) comes along when present so the shape
    audit never needs the network. Returns dest as a Path."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for rel in worktree_files(include_docs):
        src = REPO / rel
        if src.is_file():
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    cache = REPO / "scripts" / "doc8643.csv"
    if cache.is_file():
        shutil.copy2(cache, dest / "scripts" / "doc8643.csv")
    git(dest, "-c", "init.defaultBranch=main", "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-qm", "fixture")
    return dest


def _node_major(exe):
    try:
        r = subprocess.run([exe, "-p", "process.versions.node.split('.')[0]"],
                           capture_output=True, text=True, timeout=30)
        return int(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


_NODE = shutil.which("node") or shutil.which("nodejs")
HAVE_REAL_NODE = bool(_NODE) and _node_major(_NODE) >= 18


def write_stub_node(dirpath, major=22):
    """A `node` that reports `major` for -p and accepts everything else."""
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "node"
    p.write_text(f'#!/bin/sh\ncase "$1" in -p) echo {major} ;; esac\nexit 0\n')
    p.chmod(0o755)
    return d


def subprocess_env(tmp, path_prefix=None):
    """Offline, hermetic env for running the release scripts in a fixture."""
    home = Path(tmp) / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(_clean_environ(), **GIT_ENV)
    dead = "http://127.0.0.1:9"
    env.update(https_proxy=dead, HTTPS_PROXY=dead, http_proxy=dead, HTTP_PROXY=dead,
               no_proxy="", NO_PROXY="", PYTHONDONTWRITEBYTECODE="1", HOME=str(home))
    prefix = list(path_prefix or [])
    if not HAVE_REAL_NODE:
        prefix.append(str(write_stub_node(Path(tmp) / "stubnode")))
    if prefix:
        env["PATH"] = os.pathsep.join(prefix + [env.get("PATH", "")])
    return env


def run(cmd, cwd, env, timeout=300):
    r = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL, start_new_session=True)
    return r.returncode, r.stdout + r.stderr


def _next_patch(v):
    major, minor, patch = (int(x) for x in v.split("."))
    return f"{major}.{minor}.{patch + 1}"


class _FixtureCase(unittest.TestCase):
    """One shared committed fixture per class; every test gets a fresh copy."""
    INCLUDE_DOCS = False

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="aerodrome-bump-test-")
        cls.tmp = Path(cls._tmp.name)
        cls.base = build_worktree_fixture(cls.tmp / "base" / "aerodrome", cls.INCLUDE_DOCS)
        cls.old = (cls.base / "VERSION").read_text().strip()
        cls.new = _next_patch(cls.old)
        cls.env = subprocess_env(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def tree(self):
        d = Path(tempfile.mkdtemp(dir=self.tmp))
        shutil.copytree(self.base, d / "aerodrome", symlinks=True)
        return d / "aerodrome"

    def bump(self, tree, *args, env=None, cwd=None,
             skip=("--skip-pdf", "--skip-docs-drift")):
        script = str(tree / "bump-version.sh") if cwd else "bump-version.sh"
        return run(["bash", script, *args, "-y", *skip], cwd or tree, env or self.env)

    def set_new(self, tree, *extra, desc="gate test", **kw):
        return self.bump(tree, "set", self.new, desc, *extra, **kw)

    def check(self, tree, *extra, **kw):
        return self.bump(tree, "check", *extra, **kw)

    def assertNothingWritten(self, tree, version=None, out=""):
        self.assertEqual(git(tree, "status", "--porcelain", "--untracked-files=no"), "",
                         f"tracked files changed by a failed run:\n{out[-2000:]}")
        self.assertEqual((tree / "VERSION").read_text().strip(), version or self.old)


@unittest.skipIf(SKIP_SLOW, SLOW_SKIP_REASON)
@unittest.skipUnless(IN_GIT_CHECKOUT, CHECKOUT_SKIP_REASON)
class GateBlocks(_FixtureCase):
    """Each required gate stops the release; advisory steps stay advisory
    and stop lying."""

    def test_clean_tree_passes_and_gates_print_first(self):
        t = self.tree()
        rc, out = self.set_new(t)
        self.assertEqual(rc, 0, out[-3000:])
        for gate in ("Python syntax check", "Pre-release import check",
                     "Static name-resolution check", "JavaScript parse check",
                     "Type-shape coverage audit", "Installer consistency check",
                     "PII audit", "Documentation image check"):
            self.assertIn(gate, out)
            self.assertLess(out.index(gate), out.index("Bumping version"), gate)
        self.assertLess(out.index("PII audit"), out.index("Documentation image check"))
        self.assertNotIn("SKIPPED", out)
        self.assertEqual((t / "VERSION").read_text().strip(), self.new)

    def test_python_syntax_error_in_any_module_blocks(self):
        # The import check only reaches what main.py's imports load at startup;
        # main.py itself and lazily imported modules need the compile() gate.
        for rel in ("main.py", "search.py"):
            with self.subTest(module=rel):
                t = self.tree()
                with open(t / rel, "a") as f:
                    f.write("\ndef (:\n")
                git(t, "commit", "-qam", f"break {rel}")
                rc, out = self.set_new(t)
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn(f"{rel}:", out)
                self.assertIn("do not parse", out)
                self.assertNothingWritten(t, out=out)

    def test_name_resolution_gate_blocks(self):
        t = self.tree()
        p = t / "server.py"
        src = p.read_text()
        sig = "\ndef get_app(config: dict, config_path: str) -> FastAPI:\n"
        self.assertIn(sig, src, "get_app signature moved — update this fixture anchor")
        # Valid Python that imports fine: only the annotation names nothing.
        p.write_text(src.replace(sig, sig + "    def _fixture_nested(x: UndefinedFixtureType):\n"
                                       "        return x\n", 1))
        git(t, "commit", "-qam", "unresolvable annotation")
        rc, out = self.set_new(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("UndefinedFixtureType", out)
        self.assertNothingWritten(t, out=out)

    def _plant_in_last_inline_script(self, t, rel, line):
        p = t / "templates" / rel
        html = p.read_text(encoding="utf-8")
        i = html.rindex("<script>") + len("<script>")
        p.write_text(html[:i] + "\n" + line + "\n" + html[i:], encoding="utf-8")
        return html[:i].count("\n") + 2

    @unittest.skipUnless(HAVE_REAL_NODE, "needs a real node >= 18")
    def test_classic_script_grammar(self):
        # Every page loads these as classic scripts: top-level return/await/
        # import/export are SyntaxErrors there, though `node --check` passes them.
        for desc, rel, line in (("return", "logs.html", "if (!document.body) return;"),
                                ("await", "logs.html", "const zzr = await fetch('/api/x');"),
                                ("export", "status.html", "export const zz = 1;")):
            with self.subTest(construct=desc):
                t = self.tree()
                want = self._plant_in_last_inline_script(t, rel, line)
                git(t, "commit", "-qam", f"plant {desc}")
                rc, out = self.check(t)
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn(f"{rel}:{want}", out)
                self.assertNothingWritten(t, out=out)
        t = self.tree()
        with open(t / "static" / "theme.js", "a") as f:
            f.write("\nexport const zz = 1;\n")
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("static/theme.js", out)

    @unittest.skipUnless(HAVE_REAL_NODE, "needs a real node >= 18")
    def test_data_attributes_do_not_hide_a_script(self):
        # data-type= / data-src= are ignored by the browser: still classic JS.
        for attrs in ('data-type="panel"', 'data-src="x"'):
            with self.subTest(attrs=attrs):
                t = self.tree()
                p = t / "templates" / "logs.html"
                html = p.read_text(encoding="utf-8")
                i = html.index("</body>")
                p.write_text(html[:i] + f"<script {attrs}>let broken = ;</script>\n" + html[i:],
                             encoding="utf-8")
                want = html[:i].count("\n") + 1
                rc, out = self.check(t)
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn(f"logs.html:{want}", out)

    def test_inline_module_script_fails_closed(self):
        t = self.tree()
        p = t / "templates" / "logs.html"
        html = p.read_text(encoding="utf-8")
        i = html.index("</body>")
        p.write_text(html[:i] + '<script type="module">\nconst x = 1;\n</script>\n' + html[i:],
                     encoding="utf-8")
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn('type="module"', out)
        self.assertIn("logs.html:", out)

    def test_shapes_data_missing_blocks(self):
        t = self.tree()
        (t / "static" / "shapes-data.js").unlink()
        rc, out = self.set_new(t, "--skip-js-parse")
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("shapes-data.js", out)
        self.assertEqual((t / "VERSION").read_text().strip(), self.old)
        self.assertEqual(git(t, "status", "--porcelain", "--untracked-files=no"),
                         "D static/shapes-data.js")   # only the injected fault

    def test_shapes_data_without_codes_blocks(self):
        t = self.tree()
        (t / "static" / "shapes-data.js").write_text("// nothing\n")
        rc, out = self.set_new(t, "--skip-js-parse")
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("no shape codes", out)

    def test_strict_local_gap_blocks(self):
        t = self.tree()
        con = sqlite3.connect(t / "aircraft_history.db")   # gitignored: never scanned/shipped
        con.execute("create table seen_aircraft(aircraft_type text, category text)")
        con.executemany("insert into seen_aircraft values (?, '')", [("ZZZZ",)] * 20)
        con.commit()
        con.close()
        rc, out = self.set_new(t, env=dict(self.env, AERODROME_SHAPE_GATE="strict"))
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("strict gate", out)

    @unittest.skipUnless(HAVE_REAL_NODE, "needs a real node >= 18")
    def test_inline_js_syntax_error_blocks_with_template_line(self):
        t = self.tree()
        p = t / "templates" / "logs.html"
        html = p.read_text(encoding="utf-8")
        i = html.index("<script>") + len("<script>")
        p.write_text(html[:i] + "\nlet = ;\n" + html[i:], encoding="utf-8")
        want_line = html[:i].count("\n") + 2
        rc, out = self.set_new(t)
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn(f"logs.html:{want_line}", out)

    @unittest.skipUnless(HAVE_REAL_NODE, "needs a real node >= 18")
    def test_truncated_static_js_blocks(self):
        t = self.tree()
        p = t / "static" / "shapes-data.js"
        s = p.read_text(encoding="utf-8")
        p.write_text(s[: len(s) // 2], encoding="utf-8")
        rc, out = self.set_new(t)
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("static/shapes-data.js", out)

    def test_node_missing_blocks_unless_skipped(self):
        farm = self.tmp / "nonode-bin"
        if not farm.exists():
            farm.mkdir()
            for d in ("/usr/local/bin", "/usr/bin", "/bin"):
                if not os.path.isdir(d):
                    continue
                for f in Path(d).iterdir():
                    if f.name in ("node", "nodejs") or os.path.lexists(farm / f.name):
                        continue
                    (farm / f.name).symlink_to(f)
        env = dict(self.env, PATH=str(farm))
        rc, out = self.set_new(self.tree(), env=env)
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("needs node", out)
        rc, out = self.set_new(self.tree(), "--skip-js-parse", env=env)
        self.assertEqual(rc, 0, out[-3000:])

    def test_node_too_old_blocks(self):
        stub = write_stub_node(self.tmp / "oldnode", major=12)
        env = dict(self.env, PATH=os.pathsep.join([str(stub), self.env["PATH"]]))
        t = self.tree()
        rc, out = self.set_new(t, env=env)
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("too old", out)
        self.assertNothingWritten(t, out=out)

    def test_node_that_cannot_run_is_not_called_too_old(self):
        d = self.tmp / "brokennode"
        d.mkdir(exist_ok=True)
        (d / "node").write_text("#!/bin/sh\nexit 1\n")
        (d / "node").chmod(0o755)
        env = dict(self.env, PATH=os.pathsep.join([str(d), self.env["PATH"]]))
        t = self.tree()
        rc, out = self.set_new(t, env=env)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("could not run", out)
        self.assertNotIn("too old", out)
        self.assertNothingWritten(t, out=out)

    def test_skipped_gates_are_reported_not_silent(self):
        t = self.tree()
        rc, out = self.check(t, "--skip-js-parse")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("JavaScript parse check SKIPPED", out)
        self.assertIn("1 SKIPPED (js-parse)", out)
        self.assertNotIn("All required gates passed", out)
        rc, out = self.check(t, "--skip-js-parse", "--skip-shapes", "--skip-name-check")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("3 SKIPPED", out)
        for name in ("name-check", "js-parse", "shapes"):
            self.assertIn(name, out)
        rc, out = self.set_new(t, "--skip-shapes")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("gate(s) SKIPPED this run (shapes)", out)

    def test_pdf_failure_is_reported_not_masked(self):
        t = self.tree()
        (t / "scripts" / "build_overview_pdf.py").write_text("import sys\nsys.exit(3)\n")
        rc, out = self.set_new(t, skip=("--skip-docs-drift",))
        self.assertEqual(rc, 0, out[-3000:])
        self.assertNotIn("✓ PDF rebuilt", out)
        self.assertIn("PDF build failed", out)

    def test_docs_drift_crash_stays_advisory(self):
        t = self.tree()
        (t / "scripts" / "check_docs.py").write_text("raise RuntimeError('x')\n")
        rc, out = self.set_new(t, skip=("--skip-pdf",))
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("docs-drift checker exited non-zero", out)


@unittest.skipIf(SKIP_SLOW, SLOW_SKIP_REASON)
@unittest.skipUnless(IN_GIT_CHECKOUT, CHECKOUT_SKIP_REASON)
class GatesBeforeWrites(_FixtureCase):
    """A failed gate writes nothing; the documented recovery (fix, re-run the
    same command) re-runs every gate and converges."""

    def _break_import(self, t, commit=False):
        p = t / "server.py"
        p.write_text(p.read_text().replace("\ndef get_app(", "\ndef get_app_RENAMED(", 1))
        if commit:
            git(t, "commit", "-qam", "broken")

    def _fix_import(self, t, commit=False):
        p = t / "server.py"
        p.write_text(p.read_text().replace("\ndef get_app_RENAMED(", "\ndef get_app(", 1))
        if commit:
            git(t, "commit", "-qam", "fixed")

    def test_t1_import_gate_writes_nothing_and_rerun_still_gates(self):
        t = self.tree()
        self._break_import(t, commit=True)
        rc, out = self.set_new(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("Nothing was written", out)
        self.assertNothingWritten(t, out=out)
        rc, out = self.set_new(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertNothingWritten(t, out=out)

    def test_t2_pii_in_summary_caught_before_write(self):
        t = self.tree()
        rc, out = self.set_new(t, desc=f"fixed receiver at {FIXTURE_IP}")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("(CHANGELOG entry this bump will add)", out)
        self.assertNothingWritten(t, out=out)

    def test_t3_pii_in_tracked_file(self):
        t = self.tree()
        (t / "docs").mkdir(exist_ok=True)
        (t / "docs" / "fixture-note.txt").write_text(f"fixture {FIXTURE_IP}\n")
        git(t, "add", "docs/fixture-note.txt")
        git(t, "commit", "-qm", "fixture note")
        rc, out = self.set_new(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("docs/fixture-note.txt", out)
        self.assertNothingWritten(t, out=out)

    def test_t4_expand_break_fix_converges(self):
        t = self.tree()
        rc, out = self.set_new(t, desc="auto one-liner")
        self.assertEqual(rc, 0, out[-3000:])
        dirty = git(t, "status", "--porcelain")
        cl = t / "CHANGELOG.md"
        cl.write_text(cl.read_text().replace("- auto one-liner\n",
                                             "**Expanded lead.** User-voice paragraph.\n", 1))
        self._break_import(t)
        rc, out = self.set_new(t, desc="auto one-liner")
        self.assertEqual(rc, 1, out[-3000:])
        self._fix_import(t)
        rc, out = self.set_new(t, desc="auto one-liner")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("re-running every gate", out)
        self.assertIn("left untouched", out)
        text = cl.read_text()
        self.assertEqual(len(re.findall(rf"^## \[{re.escape(self.new)}\]", text, re.M)), 1)
        self.assertIn("**Expanded lead.**", text)
        self.assertNotIn("- auto one-liner", text)
        self.assertEqual(git(t, "status", "--porcelain"), dirty)

    def test_t5_failed_then_fixed_patch_is_plus_one(self):
        t = self.tree()
        self._break_import(t, commit=True)
        rc, out = self.bump(t, "patch", "x")
        self.assertEqual(rc, 1, out[-3000:])
        self._fix_import(t, commit=True)
        rc, out = self.bump(t, "patch", "x")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual((t / "VERSION").read_text().strip(), self.new)

    def test_t6_set_validation_and_downgrade(self):
        t = self.tree()
        major, minor, patch = self.old.split(".")
        older = f"{int(major) - 1}.0.0" if int(major) else "0.0.0"
        leading_zero = f"{major}.{minor}.0{int(patch) + 1}"     # would tag v3.4.0144
        for v in (f"v{self.new}", f"{major}.{minor}", f"{self.new}-rc1", older, leading_zero):
            with self.subTest(version=v):
                rc, out = self.bump(t, "set", v, "x")
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn("ERROR", out)
                self.assertNothingWritten(t, out=out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores dir permissions")
    def test_t7_apply_dies_midway_version_untouched_then_converges(self):
        t = self.tree()
        tmpl = t / "templates"
        tmpl.chmod(0o555)
        try:
            rc, out = self.set_new(t)
        finally:
            tmpl.chmod(0o755)
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertEqual((t / "VERSION").read_text().strip(), self.old)
        rc, out = self.set_new(t)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual((t / "VERSION").read_text().strip(), self.new)
        text = (t / "CHANGELOG.md").read_text()
        self.assertEqual(len(re.findall(rf"^## \[{re.escape(self.new)}\]", text, re.M)), 1)
        stale = [p.name for p in tmpl.glob("*.html")
                 if re.search(rf"Version: {re.escape(self.old)}\b", p.read_text())]
        self.assertEqual(stale, [])

    def test_t8_check_is_gates_only(self):
        t = self.tree()
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("nothing written", out)
        self.assertEqual(git(t, "status", "--porcelain"), "", "check wrote something")
        (t / "fixture-note.txt").write_text(f"fixture {FIXTURE_IP}\n")   # untracked, not ignored
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])

    def test_t9_gates_scan_the_repo_from_any_cwd(self):
        t = self.tree()
        (t / "fixture-note.txt").write_text(f"fixture {FIXTURE_IP}\n")
        git(t, "add", "fixture-note.txt")
        git(t, "commit", "-qm", "fixture note")
        rc, out = self.set_new(t, cwd="/")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("fixture-note.txt", out)
        self.assertNothingWritten(t, out=out)   # v3.4.143 wrote VERSION before this gate

    def test_t10_subheading_in_prose_does_not_suppress_the_section(self):
        t = self.tree()
        cl = t / "CHANGELOG.md"
        text = cl.read_text()
        first = re.search(r"^## \[", text, re.M).start()
        cl.write_text(text[:first] + f"Planned: ### [{self.new}] notes go here.\n\n"
                      + text[first:])
        git(t, "commit", "-qam", "prose mention")
        rc, out = self.set_new(t)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(len(re.findall(rf"^## \[{re.escape(self.new)}\]", cl.read_text(), re.M)), 1)

    def _tracked_shas(self, t):
        out = {}
        for rel in git(t, "ls-files").splitlines():
            p = t / rel
            if p.is_file():
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def test_t11_rerun_of_the_same_set_is_byte_identical(self):
        t = self.tree()
        rc, out = self.set_new(t, desc="auto one-liner")
        self.assertEqual(rc, 0, out[-3000:])
        cl = t / "CHANGELOG.md"
        cl.write_text(cl.read_text().replace("- auto one-liner\n", "**Expanded.** Text.\n", 1))
        before = self._tracked_shas(t)
        rc, out = self.set_new(t, desc="auto one-liner")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(self._tracked_shas(t), before)

    def test_t12_already_packaged_warns_but_proceeds(self):
        t = self.tree()
        rc, out = self.set_new(t)
        self.assertEqual(rc, 0, out[-3000:])
        pk = t.parent / "aerodrome-packages"
        pk.mkdir()
        (pk / f"aerodrome-v{self.new}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        rc, out = self.set_new(t, skip=("--skip-docs-drift",))   # PDF step enabled
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("already packaged", out)
        self.assertIn("Overview PDF not rebuilt", out)     # the zip's PDF stands
        self.assertNotIn("Rebuilding docs/Aerodrome_Overview.pdf", out)
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertNotIn("already packaged", out)

    def test_t12b_already_packaged_seen_through_a_symlinked_project_dir(self):
        # package-release.sh resolves ../aerodrome-packages physically; so must bump.
        t = self.tree()
        rc, out = self.set_new(t)
        self.assertEqual(rc, 0, out[-3000:])
        pk = t.parent / "aerodrome-packages"
        pk.mkdir()
        (pk / f"aerodrome-v{self.new}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        elsewhere = Path(tempfile.mkdtemp(dir=self.tmp))
        link = elsewhere / "link"
        link.symlink_to(t)
        rc, out = self.set_new(link, cwd=elsewhere)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("already packaged", out)

    def test_t13_pii_scope_is_what_git_can_ship(self):
        t = self.tree()
        with open(t / ".gitignore", "a") as f:
            f.write("\nlocal.cfg\n")
        git(t, "commit", "-qam", "ignore local.cfg")
        (t / "local.cfg").write_text(f"receiver: {FIXTURE_IP}\n")
        with self.subTest("gitignored local file is out of scope"):
            rc, out = self.check(t)
            self.assertEqual(rc, 0, out[-3000:])
        (t / "local.cfg").rename(t / "local.txt")
        with self.subTest("untracked, not ignored: in scope"):
            rc, out = self.check(t)
            self.assertEqual(rc, 1, out[-3000:])
            self.assertIn("local.txt", out)
        (t / "local.txt").unlink()
        gh = t / ".github"
        gh.mkdir(exist_ok=True)
        (gh / "x.md").write_text(f"see {FIXTURE_IP}\n")
        git(t, "add", ".github/x.md")
        git(t, "commit", "-qm", "github file")
        with self.subTest("tracked .github file: in scope"):
            rc, out = self.check(t)
            self.assertEqual(rc, 1, out[-3000:])
            self.assertIn(".github/x.md", out)
        git(t, "rm", "-q", ".github/x.md")
        git(t, "commit", "-qm", "drop github file")
        # In git, the walk's name/dir allowlist does NOT apply: a tracked file
        # ships wherever it sits and whatever it is called.
        readme = t / "update" / "UPDATE_README.md"
        original = readme.read_text()
        with open(readme, "a") as f:
            f.write(f"\nhost {FIXTURE_IP}\n")
        with self.subTest("tracked update/UPDATE_README.md (ships in every zip): in scope"):
            rc, out = self.check(t)
            self.assertEqual(rc, 1, out[-3000:])
            self.assertIn("update/UPDATE_README.md", out)
        readme.write_text(original)
        for rel in ("tools/node_modules/x.md", "docs/HANDOFF-x.md", "tools/logs/readme.md"):
            p = t / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"see {FIXTURE_IP}\n")
            git(t, "add", "-f", rel)
            git(t, "commit", "-qm", f"track {rel}")
            with self.subTest(f"tracked {rel}: in scope"):
                rc, out = self.check(t)
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn(rel, out)
            git(t, "rm", "-q", rel)
            git(t, "commit", "-qm", f"untrack {rel}")
        # Outside git (an unpacked zip): the fallback walk skips a .git FILE
        # (a linked worktree's pointer) but still covers .github/.
        gh.mkdir(exist_ok=True)
        shutil.rmtree(t / ".git")
        (t / ".git").write_text(f"gitdir: {FIXTURE_IP}\n")
        with self.subTest("non-git fallback skips the .git pointer file"):
            rc, out = self.check(t)
            self.assertEqual(rc, 0, out[-3000:])
        (gh / "x.md").write_text(f"see {FIXTURE_IP}\n")
        with self.subTest("non-git fallback still covers .github/"):
            rc, out = self.check(t)
            self.assertEqual(rc, 1, out[-3000:])
            self.assertIn(".github/x.md", out)

    def test_t13b_pii_scope_when_nested_inside_another_repo(self):
        # A tree unpacked inside some OTHER repository that ignores it: git's
        # view there is the outer repo's (nothing listed), so the audit must
        # fall back to walking this tree — not scan nothing.
        t = self.tree()
        outer = Path(tempfile.mkdtemp(dir=self.tmp)) / "outer"
        outer.mkdir()
        git(outer, "-c", "init.defaultBranch=main", "init", "-q")
        (outer / ".gitignore").write_text("dl/\n")
        nested = outer / "dl" / "aerodrome"
        shutil.copytree(t, nested, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        with open(nested / "README.md", "a") as f:
            f.write(f"\nreceiver {FIXTURE_IP}\n")
        rc, out = self.bump(nested, "check")
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("README.md", out)

    def test_t16_summary_is_written_exactly_as_scanned(self):
        # awk -v would unescape "10\.99…" into a dotted private IP AFTER the PII
        # audit scanned the raw text; the entry must land verbatim.
        t = self.tree()
        desc = "moved feeder to " + "\\.".join(["10", "99", "88", "77"])
        rc, out = self.set_new(t, desc=desc)
        self.assertEqual(rc, 0, out[-3000:])
        text = (t / "CHANGELOG.md").read_text()
        self.assertIn(f"- {desc}\n", text)
        self.assertNotIn(FIXTURE_IP, text)
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])

    def test_t14_installer_gate(self):
        cases = {
            "pkg-detect": ("install.sh", lambda s: s.replace(
                "pkg_detect() {\n", "pkg_detect() {\n    : drifted\n", 1)),
            "recover-delegates": ("scripts/recover-sudoers.sh", lambda s: s +
                "# svc-a ALL=(ALL) NOPASSWD: /bin/true\n"),
            "pipefail-readers": ("scripts/bootstrap.sh", lambda s: s +
                "\nsystemctl list-unit-files | grep -q x || true\n"),
            "bash-syntax": ("uninstall.sh", lambda s: s + "\nif then\n"),
        }
        for check, (rel, mutate) in cases.items():
            with self.subTest(check=check):
                t = self.tree()
                p = t / rel
                before = p.read_text()
                after = mutate(before)
                self.assertNotEqual(before, after, f"mutation for {check} did not apply")
                p.write_text(after)
                git(t, "commit", "-qam", f"break {check}")
                rc, out = self.set_new(t)
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn(f"✗ {check}", out)
                self.assertNothingWritten(t, out=out)

    def test_t15_check_never_fetches_or_writes_the_doc8643_cache(self):
        t = self.tree()
        cache = t / "scripts" / "doc8643.csv"
        if cache.exists():
            cache.unlink()
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("DOC 8643: unavailable", out)
        self.assertNotIn("fetch failed", out)
        self.assertFalse(cache.exists())


@unittest.skipIf(SKIP_SLOW, SLOW_SKIP_REASON)
@unittest.skipUnless(IN_GIT_CHECKOUT, CHECKOUT_SKIP_REASON)
class DocImageGate(_FixtureCase):
    """Every image under docs/ is either one scripts/screenshots.py makes or
    listed in scripts/reviewed-doc-images.sha256 at its exact sha256."""
    INCLUDE_DOCS = True
    LIST = "scripts/reviewed-doc-images.sha256"
    IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    FAKE_IMAGE = b"\x89PNG\r\n\x1a\n" + b"fixture image"

    def _reviewed(self, t):
        rows = {}
        for line in (t / self.LIST).read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                sha, path = line.split(None, 1)
                rows[path.strip()] = sha
        return rows

    def test_real_docs_pass(self):
        t = self.tree()
        listed = self._reviewed(t)
        self.assertTrue(listed, "the reviewed list is empty")
        for path in listed:
            self.assertTrue((t / path).is_file(), f"{path} is listed but not in docs/")
        images = [p for p in (t / "docs").rglob("*") if p.suffix.lower() in self.IMAGE_EXT]
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("Documentation image check", out)
        self.assertIn(f"{len(images)} documentation images: "
                      f"{len(images) - len(listed)} made by scripts/screenshots.py, "
                      f"{len(listed)} reviewed", out)

    def test_unknown_image_blocks(self):
        # Untracked-but-not-ignored counts (it could be committed); a harness
        # name outside docs/ itself is not one the harness writes; the
        # extension match ignores case.
        for rel, tracked in (("docs/fixture-shot.png", False),
                             ("docs/img/screenshot-live.png", True),
                             ("docs/fixture-shot.JPG", True),
                             ("docs/fixture-shot.webp", True)):
            with self.subTest(image=rel):
                t = self.tree()
                p = t / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(self.FAKE_IMAGE)
                if tracked:
                    git(t, "add", rel)
                    git(t, "commit", "-qm", f"add {rel}")
                rc, out = self.set_new(t)
                self.assertEqual(rc, 1, out[-3000:])
                self.assertIn(f"✗ {rel}", out)
                self.assertIn("not made by scripts/screenshots.py", out)
                self.assertIn(self.LIST, out)   # says how to clear it
                self.assertNothingWritten(t, out=out)

    def test_changed_listed_image_blocks_until_reviewed_again(self):
        t = self.tree()
        path, sha = sorted(self._reviewed(t).items())[0]
        p = t / path
        with open(p, "ab") as f:
            f.write(b"\0")
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn(f"✗ {path}", out)
        self.assertIn("changed since it was reviewed", out)
        self.assertIn("scripts/screenshots.py", out)
        lst = t / self.LIST
        new = hashlib.sha256(p.read_bytes()).hexdigest()
        lst.write_text(lst.read_text().replace(sha, new, 1))
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])

    def test_generated_names_come_from_the_script(self):
        t = self.tree()
        # A name the harness writes passes whatever its bytes, with no list line.
        (t / "docs" / "screenshot-live.png").write_bytes(self.FAKE_IMAGE)
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("Documentation image check", out)
        # The names are read from scripts/screenshots.py: rename a shot there
        # and the old file stops being one it makes, while the new name is.
        s = t / "scripts" / "screenshots.py"
        src = s.read_text()
        self.assertIn("'screenshot-live.png'", src)
        s.write_text(src.replace("'screenshot-live.png'", "'screenshot-live-renamed.png'"))
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("✗ docs/screenshot-live.png", out)
        (t / "docs" / "screenshot-live.png").rename(t / "docs" / "screenshot-live-renamed.png")
        rc, out = self.check(t)
        self.assertEqual(rc, 0, out[-3000:])
        # Only the renderers main() runs count: one dropped from its list no
        # longer vouches for its image.
        src = s.read_text()
        self.assertIn("        screenshot_about,\n", src)
        s.write_text(src.replace("        screenshot_about,\n", "", 1))
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("✗ docs/screenshot-about.png", out)
        # A renderer whose file name the check can't read fails closed.
        s.write_text(src.replace("'screenshot-about.png'", "f'screenshot-{\"about\"}.png'", 1))
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("screenshot_about", out)

    def test_malformed_list_line_blocks(self):
        t = self.tree()
        lst = t / self.LIST
        n = len(lst.read_text().splitlines()) + 1
        with open(lst, "a") as f:
            f.write("docs/screenshot-live.png reviewed\n")
        rc, out = self.check(t)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn(f"{self.LIST}:{n}", out)


def _reportlab_available():
    try:
        return importlib.util.find_spec("reportlab") is not None
    except (ImportError, ValueError):
        return False


@unittest.skipIf(SKIP_SLOW, SLOW_SKIP_REASON)
@unittest.skipUnless(IN_GIT_CHECKOUT, CHECKOUT_SKIP_REASON)
@unittest.skipUnless(_reportlab_available(), "reportlab not importable by this python")
class PdfDeterminism(_FixtureCase):
    """Same tree → same Overview PDF bytes, so a re-run of `set` after
    packaging changes nothing."""
    INCLUDE_DOCS = True

    def _venv_wrapper(self, t):
        # bump-version.sh prefers venv/bin/python3; point it at this
        # interpreter (which has reportlab) without touching any real venv.
        vb = t / "venv" / "bin"
        vb.mkdir(parents=True)
        (vb / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (vb / "python3").chmod(0o755)

    def test_rerun_rebuilds_identical_pdf(self):
        t = self.tree()
        self._venv_wrapper(t)
        pdf = t / "docs" / "Aerodrome_Overview.pdf"
        rc, out = self.set_new(t, skip=("--skip-docs-drift",))
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("✓ PDF rebuilt", out)
        self.assertIn("venv/bin/python3", out)
        first = hashlib.sha256(pdf.read_bytes()).hexdigest()
        rc, out = self.set_new(t, skip=("--skip-docs-drift",))
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(hashlib.sha256(pdf.read_bytes()).hexdigest(), first)

    def test_rerun_after_packaging_leaves_the_pdf_alone(self):
        # A source edit after the first `set` changes the PDF's inputs (line
        # counts); once the version is packaged a re-run must not rebuild it.
        t = self.tree()
        self._venv_wrapper(t)
        pdf = t / "docs" / "Aerodrome_Overview.pdf"
        rc, out = self.set_new(t, skip=("--skip-docs-drift",))
        self.assertEqual(rc, 0, out[-3000:])
        first = hashlib.sha256(pdf.read_bytes()).hexdigest()
        with open(t / "server.py", "a") as f:
            f.write("# a fix made after the bump\n# second line\n")
        pk = t.parent / "aerodrome-packages"
        pk.mkdir()
        (pk / f"aerodrome-v{self.new}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        rc, out = self.set_new(t, skip=("--skip-docs-drift",))
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("Overview PDF not rebuilt", out)
        self.assertEqual(hashlib.sha256(pdf.read_bytes()).hexdigest(), first)

    def test_two_direct_builds_are_identical(self):
        t = self.tree()
        pdf = t / "docs" / "Aerodrome_Overview.pdf"
        shas = []
        for seed in ("1", "2"):
            env = dict(self.env, PYTHONHASHSEED=seed)
            rc, out = run([sys.executable, "scripts/build_overview_pdf.py"], t, env)
            self.assertEqual(rc, 0, out[-3000:])
            shas.append(hashlib.sha256(pdf.read_bytes()).hexdigest())
        self.assertEqual(shas[0], shas[1])


if __name__ == "__main__":
    unittest.main()
