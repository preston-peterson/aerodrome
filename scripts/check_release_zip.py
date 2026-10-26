#!/usr/bin/env python3
"""check_release_zip.py — prove a release zip IS `git archive <ref>` in the
canonical single-wrapper layout; and hold the ONE maintainer-path denylist.

Usage: check_release_zip.py ZIP X.Y.Z REF [--repo DIR] [--label TEXT] [--no-hint]
       check_release_zip.py --deny-paths [X.Y.Z]   < newline-separated paths
       check_release_zip.py --import-check ZIP X.Y.Z

REF is any tree-ish: a commit (publish-release.sh: the release commit) or a
tree id (package-release.sh: a snapshot of the working tree's tracked files).
--label names REF in the findings ("only in working tree: …") instead of
printing the raw id. --no-hint drops the how-to-fix lines (package-release.sh
uses it: its zip is deleted on failure, so a restore hint would point at
nothing).

Exit 0 = clean, 1 = findings (each printed, capped), 2 = usage / git error.

Checks (all of them, every run — never stops at the first finding):
  integrity  zipfile.testzip() (CRC of every member)
  shape      every entry lives under exactly one top-level dir
             `aerodrome-vX.Y.Z/`; no absolute paths, backslashes, `..`
             components or symlink entries
  caches     no `__pycache__/` component, no `*.pyc` / `*.pyo`
  version    aerodrome-vX.Y.Z/VERSION (whitespace-stripped) == X.Y.Z
  content    the set of files == `git archive REF`'s regular files; each
             file's bytes identical; executable bit identical. Exact — no
             strip-list exceptions.
Reads the zip and the archive in memory — nothing is extracted to disk.

--import-check extracts ZIP to a temporary directory and test-imports the
PACKAGED tree in a fresh `python3 -I` (v3.4.147): every module that main.py,
server.py and collector.py import at module level — including the update
guard's `import updater`, which main.py deliberately wraps in try/except —
must be in the zip, and main / server / collector must import from it (the
third-party packages are stubbed, as in bump-version.sh's import check). It
imports with an argv[1] other than `start`, so main.py's update guard does
nothing. Exit 0 = imports, 1 = a module is missing or fails to import, 2 =
usage / unreadable zip. package-release.sh runs it on every build and
--verify, so a module that isn't in git (packaging ships the index only) can
never ship silently.

--deny-paths reads paths (one per line; a leading aerodrome-vX.Y.Z/ wrapper
is fine) and prints every one that MAINTAINER_RE matches — maintainer-only or
local files that must never ship. Exit 0 = none, 1 = hits. With X.Y.Z, a
known-history path that releases before its cutover legitimately carried
(KNOWN_HISTORY) is reported as a note on stderr instead of a hit.
package-release.sh runs it on the snapshot tree BEFORE zipping (while
repackaging is free); publish-release.sh on the --target tree and on the
downloaded zip.
"""
import io
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import zipfile
import zlib

MAX_LINES = 25
MAX_HINT_PATHS = 6
USAGE = ("usage: check_release_zip.py ZIP X.Y.Z REF [--repo DIR] [--label TEXT] [--no-hint]\n"
         "       check_release_zip.py --deny-paths [X.Y.Z]   < paths\n"
         "       check_release_zip.py --import-check ZIP X.Y.Z")

# Maintainer-only / local paths that must never ship or be published. The ONE
# definition (package-release.sh and publish-release.sh both call
# --deny-paths). Anchored to path components the way .gitignore is, so
# dialogs/, catalogs/ or changelogs/ never match logs/; matched
# case-insensitively, so a lowercase handoff.md is still caught. The file
# .pii-allowlist is NOT here: it is a tracked public repo file (two path
# globs, no PII), shipped like .gitignore since v3.4.111.
MAINTAINER_RE = re.compile(
    r"(^|/)(audits|logs|\.claude|graphify-out)/"
    r"|(^|/)(HANDOFF[^/]*|[^/]*-HANDOFF[^/]*|CLAUDE|AGENT_GUARDRAILS)\.md$"
    r"|(^|/)tech-debt-audit\.md$"
    r"|(^|/)\.graphifyignore$"
    r"|(^|/)config\.yaml$"
    r"|\.db$",
    re.I)

# path -> first version that no longer carries it. docs/tech-debt-audit.md (a
# stale, PII-clean static-scan report) was tracked and shipped until v3.4.109;
# re-checking an older release notes it rather than raising a false alarm.
# Every newer version is held to the full list.
KNOWN_HISTORY = {"docs/tech-debt-audit.md": "3.4.109"}


def _vkey(v):
    return tuple(int(x) for x in v.split("."))


def maintainer_paths(paths, ver=None):
    """(hits, notes) for an iterable of paths. A leading aerodrome-vX.Y.Z/
    wrapper is ignored for the known-history comparison."""
    hits, notes = [], []
    for p in paths:
        p = p.strip("\r\n")
        if not p or not MAINTAINER_RE.search(p):
            continue
        rel = p.split("/", 1)[1] if ver and p.startswith(f"aerodrome-v{ver}/") else p
        cut = KNOWN_HISTORY.get(rel)
        if ver and cut and _vkey(ver) < _vkey(cut):
            notes.append(f"v{ver} ships {rel} (known: tracked until v{cut}, PII-clean)")
            continue
        hits.append(p)
    return hits, notes


def deny_main(args):
    if len(args) > 1 or (args and not re.fullmatch(r"\d+\.\d+\.\d+", args[0])):
        print(USAGE, file=sys.stderr)
        return 2
    hits, notes = maintainer_paths(sys.stdin.read().splitlines(), args[0] if args else None)
    for n in notes:
        print(f"  · note: {n}", file=sys.stderr)
    for h in hits:
        print(h)
    return 1 if hits else 0


# The probe runs in a fresh `python3 -I -B -c PROBE <tree>`: -I drops the
# script directory from sys.path (3.11+), so the tree is inserted explicitly.
# sys.argv[1] is the tree path — never "start" — so main.py's update guard
# skips. Third-party packages are stubbed (same list as bump-version.sh's
# import check): this proves the zip's own modules are all there, not that
# the build host has the runtime dependencies.
_IMPORT_PROBE = r"""
import ast, importlib, os, sys, types
tree = os.path.realpath(sys.argv[1])
sys.path.insert(0, tree)
os.chdir(tree)
STUBS = ["fastapi", "fastapi.responses", "pydantic", "yaml", "requests", "uvicorn"]
class _Stub(types.ModuleType):
    def __getattr__(self, name): return _Stub(name)
    def __call__(self, *a, **k): return _Stub("call")
for name in STUBS:
    sys.modules[name] = _Stub(name)
stub_tops = {n.split(".")[0] for n in STUBS}
# Third-party: the stubs plus whatever the release's requirements.txt names
# (distribution name -> import name; the few that differ are listed).
ALIASES = {"pyyaml": "yaml", "ruamel.yaml": "ruamel", "python-multipart": "multipart"}
try:
    with open("requirements.txt") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            for ch in "<>=~!;[ ":
                line = line.replace(ch, " ")
            if line.strip():
                dist = line.split()[0].lower()
                stub_tops.add(ALIASES.get(dist, dist).split(".")[0].replace("-", "_"))
except OSError:
    pass
bad = []

def module_level_imports(path):
    def walk(stmts):
        for node in stmts:
            if isinstance(node, ast.Import):
                for a in node.names:
                    yield a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.module.split(".")[0]
            elif isinstance(node, (ast.Try, ast.If, ast.With)):
                for field in ("body", "orelse", "finalbody", "handlers"):
                    for sub in getattr(node, field, []) or []:
                        yield from walk(getattr(sub, "body", [sub]) if isinstance(sub, ast.ExceptHandler) else [sub])
    with open(path, "rb") as f:
        return set(walk(ast.parse(f.read(), path).body))

for root in ("main.py", "server.py", "collector.py"):
    if not os.path.isfile(root):
        bad.append(f"{root} is not in the zip")
        continue
    for m in sorted(module_level_imports(root)):
        if m in sys.stdlib_module_names or m in stub_tops:
            continue
        if not (os.path.isfile(m + ".py") or os.path.isfile(os.path.join(m, "__init__.py"))):
            bad.append(f"{root} imports {m}, which is not in the zip")
for mod, attrs in (("main", ("main", "start")), ("server", ("get_app",)),
                   ("collector", ("init_db",))):
    try:
        m = importlib.import_module(mod)
    except BaseException as e:
        bad.append(f"import {mod}: {type(e).__name__}: {e}")
        continue
    if not os.path.realpath(getattr(m, "__file__", "") or "").startswith(tree + os.sep):
        bad.append(f"{mod} was imported from outside the zip ({getattr(m, '__file__', None)})")
    for a in attrs:
        if not hasattr(m, a):
            bad.append(f"{mod}.{a} is missing")
if bad:
    for line in bad:
        print(line)
    sys.exit(1)
print("probe-ok")
"""


def import_check_main(args):
    """--import-check ZIP X.Y.Z: test-import the packaged tree."""
    import os
    import subprocess as _sp
    import tempfile
    if len(args) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", args[1]):
        print(USAGE, file=sys.stderr)
        return 2
    zip_path, ver = args
    wrapper = f"aerodrome-v{ver}"
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        print(f"  ✗ cannot open {zip_path}: {e}")
        return 2
    with tempfile.TemporaryDirectory(prefix="aerodrome-import-check-") as tmp, zf:
        for info in zf.infolist():
            parts = info.filename.rstrip("/").split("/")
            if info.filename.startswith("/") or "\\" in info.filename or ".." in parts \
                    or stat.S_ISLNK(info.external_attr >> 16):
                print(f"  ✗ import check: unsafe entry {info.filename!r}; not extracting")
                return 1
        zf.extractall(tmp)
        tree = os.path.join(tmp, wrapper)
        if not os.path.isdir(tree):
            print(f"  ✗ import check: no {wrapper}/ in {zip_path}")
            return 1
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp,
               "LANG": os.environ.get("LANG", "C.UTF-8"), "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            r = _sp.run([sys.executable, "-I", "-B", "-W", "ignore", "-c", _IMPORT_PROBE, tree],
                        cwd=tree, capture_output=True, text=True, timeout=300, env=env)
        except (OSError, _sp.TimeoutExpired) as e:
            print(f"  ✗ import check could not run: {e}")
            return 1
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or "probe-ok" not in out:
        lines = [ln for ln in out if ln != "probe-ok"]
        lines += (r.stderr or "").strip().splitlines()[-8:]
        for ln in lines[:MAX_LINES]:
            print(f"  ✗ {ln}")
        print(f"  ✗ the packaged tree in {zip_path.rsplit('/', 1)[-1]} does not import")
        return 1
    print(f"  ✓ the packaged tree imports: main, server and collector load from the zip, "
          f"and every module they import at load time is in it")
    return 0


def _pop_opt(args, name):
    if name not in args:
        return None
    i = args.index(name)
    if i + 1 >= len(args):
        raise ValueError(f"{name} needs a value")
    val = args[i + 1]
    del args[i:i + 2]
    return val


def _pop_flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def _some(paths):
    shown = list(paths[:MAX_HINT_PATHS])
    if len(paths) > MAX_HINT_PATHS:
        shown.append(f"… {len(paths) - MAX_HINT_PATHS} more")
    return " ".join(shown)


def main(argv):
    args = list(argv)
    if args[:1] == ["--deny-paths"]:
        return deny_main(args[1:])
    if args[:1] == ["--import-check"]:
        return import_check_main(args[1:])
    try:
        repo = _pop_opt(args, "--repo") or "."
        label = _pop_opt(args, "--label")
    except ValueError as e:
        print(f"  ✗ {e}\n{USAGE}", file=sys.stderr)
        return 2
    hint = not _pop_flag(args, "--no-hint")
    if len(args) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    zip_path, ver, ref = args
    name = label or ref
    wrapper = f"aerodrome-v{ver}"
    problems = []

    def bad(kind, msg):
        problems.append(f"{kind}: {msg}")

    # --- git archive of the ref, in memory ---
    try:
        tar_bytes = subprocess.run(
            ["git", "-C", repo, "archive", "--format=tar", f"{ref}^{{tree}}"],
            check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        err = e.stderr.decode(errors="replace").strip() if getattr(e, "stderr", None) else str(e)
        print(f"  ✗ git archive {ref} failed: {err}")
        return 2
    want = {}  # path -> (bytes, exec_bit)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        for m in tf.getmembers():
            if m.isdir() or m.name == "pax_global_header":
                continue
            if not m.isfile():  # symlink / submodule: the tree-copy packager can't reproduce it
                bad("content", f"{m.name}: non-regular entry in {name} (type {m.type!r})")
                continue
            want[m.name] = (tf.extractfile(m).read(), bool(m.mode & 0o111))

    # --- the zip ---
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        print(f"  ✗ cannot open {zip_path}: {e}")
        return 1
    with zf:
        try:
            crc = zf.testzip()
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as e:
            crc = f"(undecodable member: {e})"
        if crc is not None:
            bad("integrity", f"CRC/decompress failure in {crc}")
            for line in problems:
                print(f"  ✗ {line}")
            return 1  # don't diff contents of a corrupt archive
        tops = set()
        got = {}
        for info in zf.infolist():
            n = info.filename
            parts = n.rstrip("/").split("/")
            if n.startswith("/") or "\\" in n or ".." in parts or (parts and parts[0].endswith(":")):
                bad("shape", f"unsafe entry name {n!r}")
                continue
            tops.add(parts[0])
            if "__pycache__" in parts or n.endswith((".pyc", ".pyo")):
                bad("caches", n)
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                bad("shape", f"symlink entry {n}")
                continue
            if len(parts) < 2:
                continue  # a bare top-level file: reported by the wrapper check below
            got.setdefault(parts[0], {})["/".join(parts[1:])] = (zf.read(info), bool(mode & 0o111))
        if tops != {wrapper}:
            t = sorted(tops)
            shown = t if len(t) <= 5 else t[:5] + [f"… {len(t) - 5} more"]
            bad("shape", f"top-level entries {shown}, expected exactly ['{wrapper}']")
        # Compare content under the wrapper; if the wrapper is misnamed but unique, still
        # compare under it so one naming slip doesn't bury the content verdict.
        root = wrapper if wrapper in got else (next(iter(got)) if len(got) == 1 else wrapper)
        got = got.get(root, {})

    # --- internal VERSION ---
    v = got.get("VERSION")
    if v is None:
        bad("version", f"{wrapper}/VERSION missing")
    elif v[0].decode("utf-8", "replace").strip() != ver:
        bad("version", f"zip VERSION is {v[0].decode('utf-8', 'replace').strip()!r}, expected {ver!r}")

    # --- content == git archive <ref> ---
    # Kept by kind: each kind has a different fix.
    restorable, extra, modebits = [], [], []   # restorable: (path, zip +x)
    for p in sorted(set(got) - set(want)):
        bad("content", f"only in zip: {p}")
        restorable.append((p, got[p][1]))
    for p in sorted(set(want) - set(got)):
        bad("content", f"only in {name}: {p}")
        extra.append(p)
    for p in sorted(set(got) & set(want)):
        if got[p][0] != want[p][0]:
            bad("content", f"differs from {name}: {p}")
            restorable.append((p, got[p][1]))
        elif got[p][1] != want[p][1]:
            bad("content", f"exec bit differs from {name}: {p} "
                           f"(zip {'+x' if got[p][1] else '-x'}, {name} {'+x' if want[p][1] else '-x'})")
            modebits.append((p, got[p][1]))

    zname = zip_path.rsplit("/", 1)[-1]
    if problems:
        for line in problems[:MAX_LINES]:
            print(f"  ✗ {line}")
        if len(problems) > MAX_LINES:
            print(f"  ✗ … and {len(problems) - MAX_LINES} more")
        print(f"  ✗ {zname} is NOT git archive {name} "
              f"({len(problems)} finding{'s' if len(problems) != 1 else ''})")
        if hint and restorable:
            print(f"    to restore a file from the zip: "
                  f"unzip -p {shlex.quote(zip_path)} '{root}/<path>' > <path>")
            xs = [p for p, x in restorable if x]
            if xs:
                print(f"      then chmod +x (the zip has it executable): {_some(xs)}")
        if hint and extra:
            print(f"    only in {name}, not in the zip — remove it to match the zip "
                  f"(git rm it, or revert the add): {_some(extra)}")
        if hint and modebits:
            for want_x in (True, False):
                ps = [p for p, x in modebits if x is want_x]
                if ps:
                    flag = "+x" if want_x else "-x"
                    print(f"    exec bit to match the zip: chmod {flag} (and git update-index "
                          f"--chmod={flag}) {_some(ps)}")
        return 1
    print(f"  ✓ zip == git archive {name} ({len(got)} files, bytes + exec bits); "
          f"single wrapper {wrapper}/; no caches; VERSION == {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
