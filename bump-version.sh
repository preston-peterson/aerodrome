#!/bin/bash
# Version: 3.4.145
# =============================================================================
# bump-version.sh — Bump Aerodrome version + auto-update CHANGELOG.md
# =============================================================================
#
# Usage:
#   ./bump-version.sh patch "What changed"
#   ./bump-version.sh minor "What changed"
#   ./bump-version.sh major "What changed"
#   ./bump-version.sh set 4.2.1 "What changed"   (canonical X.Y.Z only — no
#                                                 'v', no leading zeros;
#                                                 never backwards)
#   ./bump-version.sh check                      (gates only — no network,
#                                                 writes nothing)
#
# Every REQUIRED gate (Python syntax, import, name resolution, JavaScript
# parse, type-shape, installer consistency, PII) runs BEFORE anything is
# written. A gate failure writes nothing: fix the problem and re-run the same
# command — a re-run of the same `set` re-runs every gate and, from the same
# inputs, rewrites byte-identical files. A skipped gate (--skip-*) is
# printed as SKIPPED, and the run is then not release-ready.
#
# After packaging (scripts/package-release.sh), re-gate with `check`, never
# `set`: once the box has applied vX.Y.Z, vX.Y.Z is frozen — don't re-set or
# repackage it. (A `set` run while ../aerodrome-packages/aerodrome-vX.Y.Z.zip
# exists warns, and leaves the Overview PDF alone.)
#
# Options:
#   --type=added|changed|fixed|removed   (default: changed)
#   --skip-docs-check / -y               (skip the pre-bump docs checklist
#                                         shown for minor/major bumps)
#   --skip-pdf                           (skip rebuilding the Overview PDF —
#                                         useful when iterating fast and you
#                                         know the PDF hasn't changed, or if
#                                         reportlab isn't installed. The
#                                         rebuild uses venv/bin/python3 when
#                                         present and is deterministic)
#   --skip-docs-drift                    (skip the post-bump drift detector —
#                                         it's advisory anyway, but this
#                                         silences the output during rapid
#                                         iteration)
#   --skip-name-check                    (skip the static name-resolution
#                                         check on server.py annotations.
#                                         The check would have caught the
#                                         v3.1.0/3.1.1 NameError bug — only
#                                         skip if you have verified that the
#                                         flagged name is a false positive)
#   --skip-shapes                        (skip the type-shape coverage
#                                         gate. The coverage REPORT is
#                                         advisory; the gate blocks only
#                                         when static/shapes-data.js is
#                                         unreadable/empty or, with
#                                         AERODROME_SHAPE_GATE=strict, on
#                                         a >=5% local-traffic gap)
#   --skip-js-parse                      (skip the JavaScript parse gate.
#                                         Only for a host without node >= 18
#                                         — never for a release you publish)
#
# There is no skip flag for the Python syntax, import, installer-consistency
# or PII gates.
#
# Examples:
#   ./bump-version.sh patch "Fixed sorting bug on Live tab"
#   ./bump-version.sh minor "Added export to CSV" --type=added
#   ./bump-version.sh patch "Better error messages" --type=fixed
#
# Without arguments, shows current version and what each bump would produce.
#
# =============================================================================

# pipefail: every gate below pipes its output through `sed` for indenting.
# Without pipefail a pipeline's status is sed's (always 0), so a gate written
# `if ! python3 gate.py | sed …` could never fail (code review #34). Advisory
# pipelines must say so explicitly with `|| …`.
set -eo pipefail

# Physical path (symlinks resolved), the same way package-release.sh and
# publish-release.sh resolve theirs — so "../aerodrome-packages" below is the
# directory package-release.sh actually writes to, even when the project dir
# is reached through a symlink.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd -P)"
VERSION_FILE="${SCRIPT_DIR}/VERSION"
CHANGELOG="${SCRIPT_DIR}/CHANGELOG.md"
# Every gate below uses paths relative to the repo root (the PII audit walks
# ".", the JS check reads templates/), so pin the cwd before any of them run.
cd "$SCRIPT_DIR"

if [ ! -f "$VERSION_FILE" ]; then
    echo "ERROR: VERSION file not found at $VERSION_FILE"
    exit 1
fi

OLD_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')
IFS='.' read -r MAJOR MINOR PATCH <<< "$OLD_VERSION"

# The one accepted version form: X.Y.Z, digits only, no leading zeros. The tag
# is derived from it (vX.Y.Z), and bash arithmetic reads a leading-zero field
# as octal (patch of 3.4.0144 would give 3.4.101; 0148 is an error).
CANON_RE='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
OLD_CANONICAL=0
[[ "$OLD_VERSION" =~ $CANON_RE ]] && OLD_CANONICAL=1
require_canonical_old() {
    if [ "$OLD_CANONICAL" != "1" ]; then
        echo "ERROR: VERSION holds '$OLD_VERSION', not a canonical X.Y.Z (digits only, no leading"
        echo "       zeros) — can't compute the next $1. Set it explicitly: $0 set X.Y.Z \"…\""
        exit 1
    fi
}

# Parse type flag from any position
ENTRY_TYPE="Changed"
SKIP_DOCS_CHECK=0
SKIP_PDF=0
SKIP_DOCS_DRIFT=0
SKIP_SHAPES=0
SKIP_JS_PARSE=0
SKIP_NAME_CHECK=0
GATES_ONLY=0
SKIPPED_GATES=()   # every gate a --skip-* flag turned off this run (printed, never silent)
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --type=added)        ENTRY_TYPE="Added" ;;
        --type=changed)      ENTRY_TYPE="Changed" ;;
        --type=fixed)        ENTRY_TYPE="Fixed" ;;
        --type=removed)      ENTRY_TYPE="Removed" ;;
        --skip-docs-check)   SKIP_DOCS_CHECK=1 ;;
        --yes|-y)            SKIP_DOCS_CHECK=1 ;;
        --skip-pdf)          SKIP_PDF=1 ;;
        --skip-docs-drift)   SKIP_DOCS_DRIFT=1 ;;
        --skip-shapes)       SKIP_SHAPES=1 ;;
        --skip-js-parse)     SKIP_JS_PARSE=1 ;;
        --skip-name-check)   SKIP_NAME_CHECK=1 ;;
        *) ARGS+=("$arg") ;;
    esac
done

CMD="${ARGS[0]}"

case "$CMD" in
    patch)
        require_canonical_old patch
        PATCH=$((PATCH + 1))
        DESC="${ARGS[1]}"
        ;;
    minor)
        require_canonical_old minor
        MINOR=$((MINOR + 1))
        PATCH=0
        DESC="${ARGS[1]}"
        ;;
    major)
        require_canonical_old major
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        DESC="${ARGS[1]}"
        ;;
    set)
        if [ -z "${ARGS[1]}" ]; then
            echo "Usage: $0 set <version> \"description\""
            exit 1
        fi
        if ! [[ "${ARGS[1]}" =~ $CANON_RE ]]; then
            echo "ERROR: version must be X.Y.Z (digits only, no 'v' prefix, no leading zeros) — got '${ARGS[1]}'"
            exit 1
        fi
        IFS='.' read -r MAJOR MINOR PATCH <<< "${ARGS[1]}"
        DESC="${ARGS[2]}"
        ;;
    check)
        # Run every gate against the current tree; write nothing.
        GATES_ONLY=1
        DESC="(gates only)"
        ;;
    *)
        echo "Usage: $0 {patch|minor|major|set <version>} \"description\" [--type=added|changed|fixed|removed]"
        echo "       $0 check     (run every release gate; writes nothing)"
        echo ""
        echo "Current version: $OLD_VERSION"
        echo ""
        if [ "$OLD_CANONICAL" = "1" ]; then
            echo "  patch  $OLD_VERSION → ${MAJOR}.${MINOR}.$((PATCH + 1))"
            echo "  minor  $OLD_VERSION → ${MAJOR}.$((MINOR + 1)).0"
            echo "  major  $OLD_VERSION → $((MAJOR + 1)).0.0"
        else
            echo "  (VERSION is not a canonical X.Y.Z — use: $0 set X.Y.Z \"…\")"
        fi
        echo ""
        echo "Examples:"
        echo "  $0 patch \"Fixed sorting bug on Live tab\" --type=fixed"
        echo "  $0 minor \"Added CSV export\" --type=added"
        exit 0
        ;;
esac

if [ -z "$DESC" ]; then
    echo "ERROR: a description is required so it can be added to CHANGELOG.md"
    echo "Example: $0 $CMD \"What changed in this version\""
    exit 1
fi

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"

RERUN=0
if [ "$GATES_ONLY" = "1" ]; then
    echo "Gates only — no file will be written (VERSION stays $OLD_VERSION)."
elif [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    # The documented recovery ("fix it and re-run the same command") lands
    # here. It used to `exit 0` without running a single gate. Now it re-runs
    # every gate and then re-applies idempotently (see "Apply the bump").
    RERUN=1
    echo "Version is already $NEW_VERSION — re-running every gate; nothing already at $NEW_VERSION is rewritten."
elif [ "$(printf '%s\n%s\n' "$OLD_VERSION" "$NEW_VERSION" | sort -V | tail -n1)" != "$NEW_VERSION" ]; then
    echo "ERROR: $NEW_VERSION is older than the current version $OLD_VERSION — refusing to go backwards."
    exit 1
fi

# The CHANGELOG entry is decided BEFORE the gates so the PII audit can scan it
# (the summary is free text; it used to be caught only after being written).
TODAY=$(date +%Y-%m-%d)
NEW_ENTRY="## [${NEW_VERSION}] — ${TODAY}

### ${ENTRY_TYPE}
- ${DESC}
"
# Anchored: only a real "## [X.Y.Z]" section heading counts — a "### [X.Y.Z]"
# sub-heading or a mention in prose must not suppress the new section.
CHANGELOG_HAS_ENTRY=0
if [ -f "$CHANGELOG" ] && grep -qE "^## \[${NEW_VERSION//./\\.}\]" "$CHANGELOG"; then
    CHANGELOG_HAS_ENTRY=1
fi
PENDING_ENTRY=""
if [ "$GATES_ONLY" = "0" ] && [ "$CHANGELOG_HAS_ENTRY" = "0" ]; then
    PENDING_ENTRY="$NEW_ENTRY"
fi

nothing_written() {
    echo "  Nothing was written by this run — VERSION is still $(tr -d '[:space:]' < "$VERSION_FILE")."
    echo "  Fix the problem above and re-run the same command."
}

# Already packaged? Re-running `set` is free only until the box applies the
# build: from then on vX.Y.Z is frozen (the Local-update card only offers a
# NEWER version, so a repackaged X.Y.Z could never be re-applied). Warn only —
# but while the zip exists this run leaves the Overview PDF alone (it is the
# one write that changes when a source file changed after the first `set`).
PACKAGED_ZIP="$(dirname "$SCRIPT_DIR")/aerodrome-packages/aerodrome-v${NEW_VERSION}.zip"
ALREADY_PACKAGED=0
if [ "$GATES_ONLY" = "0" ] && [ -e "$PACKAGED_ZIP" ]; then
    ALREADY_PACKAGED=1
    echo "⚠ v$NEW_VERSION is already packaged ($PACKAGED_ZIP)."
    echo "  If the box applied it, don't change anything: re-gate with \`bash bump-version.sh check\`."
    echo "  If it hasn't, \`rm\` the zip + .sha256 FIRST, then re-run this \`set\` and re-package"
    echo "  (while the zip exists this run does not rebuild the Overview PDF)."
    echo ""
fi

# =============================================================================
# Documentation checklist (skip with --skip-docs-check or -y)
# =============================================================================
# Shown for minor/major bumps — patch bumps are almost always bug fixes that
# don't need any manual doc work (just the auto-generated CHANGELOG entry).
# The prompt is a soft reminder, not a hard gate — answering "no" still lets
# you continue, but catches the common case of "oops, I forgot to update the
# README / re-take screenshots / update the example config".
#
# See CONTRIBUTING.md → Documentation for the full policy.
if [ "$SKIP_DOCS_CHECK" = "0" ] && ( [ "$CMD" = "minor" ] || [ "$CMD" = "major" ] ); then
    # Only prompt in an interactive terminal
    if [ -t 0 ] && [ -t 1 ]; then
        echo ""
        echo "Before bumping $OLD_VERSION → $NEW_VERSION, quick doc checklist:"
        echo ""
        echo "  [ ] README.md Features list updated for any user-visible change?"
        echo "  [ ] config.yaml.example + config_validator.py updated for any new config keys?"
        echo "  [ ] Screenshots re-taken if UI changed in a way that invalidates existing ones?"
        echo "      (Run:  python3 scripts/screenshots.py)"
        echo "  [ ] New screenshot added if this feature earns its own Features bullet?"
        echo "  [ ] Overview PDF will rebuild automatically below (unless --skip-pdf)."
        echo ""
        echo "See CONTRIBUTING.md for the full rules."
        echo ""
        read -r -p "Proceed with bump? [Y/n/skip] " -n 1 answer
        echo ""
        case "$answer" in
            [Nn])
                echo "Aborted. Update the relevant docs and re-run."
                exit 1
                ;;
            s|S)
                echo "Proceeding (checklist skipped this time)."
                ;;
            *)
                echo "Proceeding."
                ;;
        esac
    fi
fi

# =============================================================================
# Python syntax check — REQUIRED (v3.4.144+), no skip flag.
# =============================================================================
# compile() every *.py git can see (tracked + untracked-not-ignored — what can
# ship or be committed; outside a git checkout, a walk of the tree). The import
# check below reaches only the modules main.py's imports load at startup, so a
# syntax error in a lazily imported module (search.py, categorize.py,
# photo_resolver.py, …), in main.py itself or in schema_migrations.py would
# otherwise ship — and the service could not start. compile() writes no .pyc
# (py_compile would), so `check` still writes nothing.
PY_SYNTAX_PY=$(cat <<'PYEOF'
import os, subprocess, sys, warnings
warnings.simplefilter('ignore', SyntaxWarning)   # e.g. an invalid "\/" escape: a warning, not an error
SKIP_DIRS = {'.git', 'venv', '__pycache__', '.backups', 'node_modules',
             'logs', 'update', 'audits', 'graphify-out'}

def py_files():
    """git's view when the cwd is the top of a checkout; otherwise (an unpacked
    release, or a tree nested inside some other repository) a walk."""
    try:
        top = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                             capture_output=True, check=True).stdout
        if os.path.realpath(top.decode('utf-8', 'surrogateescape').strip()) == os.path.realpath('.'):
            out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others',
                                  '--exclude-standard', '--', '*.py'],
                                 capture_output=True, check=True).stdout
            return sorted({p for p in out.decode('utf-8', 'surrogateescape').split('\0') if p})
    except (OSError, subprocess.CalledProcessError):
        pass
    paths = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        paths += [os.path.relpath(os.path.join(root, f)) for f in files if f.endswith('.py')]
    return sorted(paths)

bad, n = [], 0
for path in py_files():
    try:
        with open(path, 'rb') as f:
            src = f.read()
    except OSError:
        continue   # tracked but deleted in the working tree
    n += 1
    try:
        compile(src, path, 'exec', dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        line = getattr(e, 'lineno', None)
        bad.append(f'{path}:{line or "?"}  {type(e).__name__}: {getattr(e, "msg", None) or e}')
if bad:
    print(f'  ✗ {len(bad)} Python file(s) do not parse:')
    for b in bad:
        print(f'    • {b}')
    sys.exit(1)
print(f'  ✓ {n} Python files parse')
PYEOF
)
echo "Python syntax check (every *.py git can see)..."
if ! (cd "$SCRIPT_DIR" && python3 -c "$PY_SYNTAX_PY"); then
    echo ""
    echo "  !! Release aborted: a Python file above doesn't parse — the service (or"
    echo "  !! whatever imports that module) would fail at startup."
    nothing_written
    exit 1
fi
echo ""

# =============================================================================
# Pre-release import check.
# =============================================================================
# v2.42.2: catch the class of bug where a code edit produces a syntactically
# valid file that has nevertheless lost a required symbol. Specifically: v2.42.0
# shipped with server.py missing its `def get_app` line, so `from server import
# get_app` failed and the service couldn't start. The ast.parse() syntax check
# at the time passed because the file was valid Python — just missing the
# symbol main.py needs.
#
# This step imports the exact names main.py imports, in a subprocess so any
# import-time side effects (threads, signal handlers) don't leak into the
# bump script. If any import fails or any expected attribute is missing,
# the bump stops HERE — before PDF rebuild, before drift check, before the
# release is packaged. A broken zip is worse than a delayed one.
#
# The check uses sys.modules stubs for fastapi/pydantic/yaml/requests/uvicorn
# so the check doesn't need those installed in the bump environment. We're
# checking module structure, not runtime behaviour.
IMPORT_CHECK_SCRIPT='
import sys, importlib, types
class _Stub(types.ModuleType):
    def __getattr__(self, name): return _Stub(name)
    def __call__(self, *a, **k): return _Stub("call")
for name in ["fastapi", "fastapi.responses", "pydantic", "yaml",
             "requests", "uvicorn"]:
    if name not in sys.modules:
        sys.modules[name] = _Stub(name)

# main.py needs these exact imports:
#   from collector import init_db, build_watchlist_lookup, fetch_and_store
#   from server import get_app
# Also checking a couple of transitive helpers to catch broader drift.
checks = [
    ("collector", ["init_db", "build_watchlist_lookup", "fetch_and_store"]),
    ("server",    ["get_app"]),
    ("notifier",  ["Notifier"]),
    ("config_validator", ["validate_config"]),
]
failed = []
for modname, attrs in checks:
    try:
        m = importlib.import_module(modname)
    except Exception as e:
        failed.append(f"  import {modname}: {type(e).__name__}: {e}")
        continue
    for a in attrs:
        if not hasattr(m, a):
            failed.append(f"  {modname}.{a} missing")
if failed:
    print("IMPORT-CHECK FAILED:")
    for line in failed:
        print(line)
    sys.exit(1)
print("OK — all module-level symbols resolvable")
'
echo "Pre-release import check..."
if ! ( cd "$SCRIPT_DIR" && python3 -c "$IMPORT_CHECK_SCRIPT" 2>&1 | sed "s/^/  /" ); then
    echo ""
    echo "  !! Release aborted by the import check."
    nothing_written
    exit 1
fi
echo ""

# =============================================================================
# Static name-resolution check — required.
# =============================================================================
# Walk server.py's function annotations and flag any name referenced in an
# annotation that isn't defined at module scope or imported. v3.1.0 + v3.1.1
# both shipped with `async def switch_to_real_execute(request: Request):`
# but `Request` was never imported from fastapi — every install crashed at
# startup with NameError. AST-parse only catches syntax, not name resolution.
# This check would have caught the bug before either release shipped.
#
# Unlike check_docs.py which is advisory, this one is REQUIRED: a release
# that crashes every install at startup is not advisory — it's broken.
# Pass --skip-name-check to suppress (don't, unless you have a very good
# reason — like working around a false positive that has been verified).
NAME_CHECK_PY=$(cat << 'PYEOF'
import ast, sys, builtins
src = open('server.py').read()
tree = ast.parse(src)
defined = set(dir(builtins))
defined.update(['__file__', '__name__', '__doc__', '__package__',
                '__loader__', '__spec__', 'TYPE_CHECKING'])
# Common Pydantic / typing-stdlib names that ARE defined at the
# function-scope where they're used (Pydantic BaseModel subclasses
# defined inside get_app, for example). False-positive on these is
# OK — the check is conservative and excludes uppercase function-
# local names that look like nested-class references.
LOCAL_OK = set()
class TopCollector(ast.NodeVisitor):
    def visit_Import(self, n):
        for a in n.names: defined.add(a.asname or a.name.split('.')[0])
    def visit_ImportFrom(self, n):
        for a in n.names: defined.add(a.asname or a.name)
    def visit_FunctionDef(self, n): defined.add(n.name)
    def visit_AsyncFunctionDef(self, n): defined.add(n.name)
    def visit_ClassDef(self, n): defined.add(n.name)
    def visit_Assign(self, n):
        for t in n.targets:
            if isinstance(t, ast.Name): defined.add(t.id)
    def visit_AnnAssign(self, n):
        if isinstance(n.target, ast.Name): defined.add(n.target.id)
TopCollector().visit(tree)
# Also pick up nested classes anywhere — they may be referenced in
# sibling-function annotations within the same outer function scope.
class NestedCollector(ast.NodeVisitor):
    def visit_ClassDef(self, n):
        LOCAL_OK.add(n.name)
        self.generic_visit(n)
NestedCollector().visit(tree)
errors = []
class AnnChecker(ast.NodeVisitor):
    def _check(self, ann, lineno):
        for sub in ast.walk(ann):
            if isinstance(sub, ast.Name):
                if sub.id in defined or sub.id in LOCAL_OK:
                    continue
                errors.append((lineno, sub.id))
    def visit_FunctionDef(self, n): self._do(n)
    def visit_AsyncFunctionDef(self, n): self._do(n)
    def _do(self, n):
        for a in n.args.args + n.args.kwonlyargs:
            if a.annotation is not None: self._check(a.annotation, a.lineno)
        if n.returns is not None: self._check(n.returns, n.lineno)
        self.generic_visit(n)
AnnChecker().visit(tree)
if errors:
    print(f'  ✗ Found {len(errors)} undefined name(s) in type annotations:')
    seen = set()
    for lineno, name in errors:
        key = (lineno, name)
        if key in seen: continue
        seen.add(key)
        print(f'    server.py:{lineno}  {name!r}')
    sys.exit(1)
else:
    print('  ✓ Static name-resolution check passed (function annotations).')
PYEOF
)
if [ "${SKIP_NAME_CHECK:-0}" = "0" ]; then
    echo "Static name-resolution check..."
    if ! (cd "$SCRIPT_DIR" && python3 -c "$NAME_CHECK_PY"); then
        echo ""
        echo "  This means a function-annotation references a name that isn't imported"
        echo "  or defined at module scope. The release would crash at startup with a"
        echo "  NameError before the service can come up. Fix the missing import or"
        echo "  remove the annotation before shipping."
        echo ""
        echo "  Override with --skip-name-check ONLY if you have verified that the"
        echo "  flagged name is a false positive (e.g. a Pydantic BaseModel subclass"
        echo "  defined inside the same outer function)."
        nothing_written
        exit 1
    fi
    echo ""
else
    echo "⚠ Static name-resolution check SKIPPED (--skip-name-check) — package-release.sh re-runs it with no skips."
    echo ""
    SKIPPED_GATES+=("name-check")
fi

# =============================================================================
# JavaScript parse check — REQUIRED (advisory v3.4.27 → v3.4.143, required v3.4.144+).
# =============================================================================
# For every template, concatenate its inline <script> blocks (classic
# scripts share one global scope in the browser, so a cross-block `let`
# redeclaration is a real error) and parse them; then parse each
# first-party static/*.js file (static/leaflet/ is vendored and skipped).
# A parse error kills the WHOLE <script> block in the browser — the page
# renders but nothing on it works — so this gates the release.
#
# The parse uses the CLASSIC-script grammar every page loads these with
# (no template uses type="module"): node's `vm.Script` compiles without
# running. `node --check` is NOT that grammar — it wraps a file as a
# CommonJS function body (so a top-level `return` passes) and, on node
# >= 22.7, auto-detects ES modules (so top-level `await`/`import`/`export`
# pass) — all four are SyntaxErrors in a browser <script>.
#
# Templates are parsed INDEPENDENTLY (they're separate documents; they
# legitimately reuse identifiers like `let activeTab`).
#
# Still does NOT catch the v3.4.25 timefmt-injection class (a deleted
# server-side injection line, not a parse error) — see HANDOFF #12.
#
# Graduated from advisory after 115+ releases (v3.4.27–v3.4.143) with no
# false positive. Needs node >= 18 on PATH: older parsers reject modern
# syntax the browsers accept (Ubuntu 22.04's apt nodejs is 12 — use
# NodeSource or nvm there; Debian 12, Pi OS and Ubuntu 24.04 ship 18+).
# --skip-js-parse exists only for contributor hosts that can't have it;
# package-release.sh never passes it.
if [ "$SKIP_JS_PARSE" = "0" ]; then
    NODE_BIN="$(command -v node 2>/dev/null || command -v nodejs 2>/dev/null || true)"
    if [ -z "$NODE_BIN" ]; then
        echo "✗ JavaScript parse check needs node >= 18, which isn't on PATH."
        echo "  Install a current Node (NodeSource or nvm) and re-run, or pass"
        echo "  --skip-js-parse on a host that can't (never for a published release)."
        nothing_written
        exit 1
    fi
    # A node that crashes or prints nothing is "can't run", not "too old".
    NODE_RC=0
    NODE_OUT="$("$NODE_BIN" -p 'process.versions.node.split(".")[0]' 2>&1)" || NODE_RC=$?
    NODE_FIRST="${NODE_OUT%%$'\n'*}"
    case "$NODE_OUT" in
        ''|*[!0-9]*)
            echo "✗ could not run $NODE_BIN to read its version (exit $NODE_RC): ${NODE_FIRST:-(no output)}"
            echo "  Fix or replace that node (need ≥ 18), or pass --skip-js-parse on a host"
            echo "  that can't run one (never for a published release)."
            nothing_written
            exit 1
            ;;
    esac
    NODE_MAJOR="$NODE_OUT"
    if [ "$NODE_RC" != "0" ]; then
        echo "✗ could not run $NODE_BIN to read its version (exit $NODE_RC): $NODE_FIRST"
        nothing_written
        exit 1
    fi
    if [ "$NODE_MAJOR" -lt 18 ]; then
        echo "✗ node v$NODE_MAJOR is too old to parse modern JavaScript (need ≥ 18; Ubuntu 22.04's"
        echo "  apt nodejs is 12). Install a current Node (NodeSource or nvm), or pass"
        echo "  --skip-js-parse on a host that can't (never for a published release)."
        nothing_written
        exit 1
    fi
    echo "JavaScript parse check (templates' inline JS + static/*.js)..."
    JS_PARSE_PY=$(cat <<'PYEOF'
import os, re, sys, subprocess, tempfile
from pathlib import Path

NODE = os.environ['NODE_BIN']
SCRIPT_RE = re.compile(r'<script\b([^>]*)>(.*?)</script\s*>', re.DOTALL | re.I)
# Attribute names start at the start of the attribute text or after
# whitespace — a bare `\b` would also match inside data-src= / data-type=,
# which the browser ignores (and the block would be skipped as non-JS).
SRC_RE = re.compile(r'(?:^|\s)src\s*=', re.I)
TYPE_RE = re.compile(r'(?:^|\s)type\s*=\s*["\x27]?([^"\x27\s>]+)', re.I)
CLASSIC = {'text/javascript', 'application/javascript'}
# Compile (never run) the file as a CLASSIC script — the grammar of a
# browser <script> without type="module". Prints "<file>:<line>" then
# "<Name>Error: <message>", the same shape `node --check` printed.
VM_JS = ("const fs=require('fs'),vm=require('vm'),p=process.argv[1];"
         "try{new vm.Script(fs.readFileSync(p,'utf8'),{filename:p})}"
         "catch(e){const m=/:(\\d+)\\n/.exec(e.stack||'');"
         "console.error(p+':'+(m?m[1]:'?')+'\\n'+e.name+': '+e.message);process.exit(1)}")

def node_check(path):
    r = subprocess.run([NODE, '-e', VM_JS, str(path)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return None
    # "<file>:<line>\n<Name>Error: <msg>" (or node's own crash output)
    err = r.stderr
    m = re.match(r'.*:(\d+)\s*$', err.split('\n', 1)[0])
    msg = next((l.strip() for l in err.splitlines()
                if re.match(r'\s*[A-Za-z]*Error\b', l)),
               err.strip().split('\n')[0] or '(no message)')
    return (int(m.group(1)) if m else None), msg

failed, n_files, n_bytes = [], 0, 0

for path in sorted(Path('templates').glob('*.html')):
    html = path.read_text(encoding='utf-8')
    chunks, spans, cur = [], [], 1   # spans: (first line in concat, first line in template)
    for m in SCRIPT_RE.finditer(html):
        attrs, body = m.group(1), m.group(2)
        if SRC_RE.search(attrs):
            continue
        t = TYPE_RE.search(attrs)
        typ = t.group(1).lower() if t else ''
        if typ and typ not in CLASSIC:
            if typ == 'module':   # fail closed: never silently skip real JS
                failed.append((path.name, html.count('\n', 0, m.start()) + 1,
                               'inline <script type="module"> is not covered by this '
                               'check yet — extend bump-version.sh before shipping it'))
            continue              # JSON / template data blocks aren't JS
        spans.append((cur, html.count('\n', 0, m.start(2)) + 1))
        chunks.append(body)
        cur += body.count('\n') + 1
    if not chunks:
        continue
    js = '\n'.join(chunks)
    n_files += 1
    n_bytes += len(js)
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as tf:
        tf.write(js)
        tmp = tf.name
    try:
        res = node_check(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)
    if res:
        line, msg = res
        if line is not None:   # map the concat line back to the template line
            first, tline = max(sp for sp in spans if sp[0] <= line)
            line = tline + (line - first)
        failed.append((path.name, line, msg))

for path in sorted(Path('static').glob('*.js')):  # first-party; static/leaflet/ is vendored
    n_files += 1
    n_bytes += path.stat().st_size
    res = node_check(path)
    if res:
        failed.append((str(path), res[0], res[1]))

print(f'  Parsed {n_files} files ({n_bytes} bytes of JS)')
if failed:
    print(f'  ✗ {len(failed)} file(s) failed to parse:')
    for name, line, msg in failed:
        print(f'    • {name}:{line if line is not None else "?"}  {msg}')
    sys.exit(1)
print('  ✓ JavaScript parse OK (all templates + static/*.js)')
PYEOF
)
    if ! (cd "$SCRIPT_DIR" && NODE_BIN="$NODE_BIN" python3 -c "$JS_PARSE_PY"); then
        echo ""
        echo "  !! Release aborted: the page's JavaScript above doesn't parse. The"
        echo "  !! browser rejects the whole <script> block, so that page would load"
        echo "  !! with nothing on it working."
        nothing_written
        exit 1
    fi
    echo ""
else
    echo "⚠ JavaScript parse check SKIPPED (--skip-js-parse) — package-release.sh re-runs it with no skips."
    echo ""
    SKIPPED_GATES+=("js-parse")
fi

# =============================================================================
# Type-shape coverage audit — REQUIRED (v3.4.133+).
# =============================================================================
# Which types does the radar still draw a chevron for, and how much of
# YOUR sky is that? Reports local fixed-wing traffic coverage (the
# seen_aircraft table) plus ICAO DOC 8643 designator gaps; helicopters
# and tiltrotors are excluded — the rotor disc covers them. The gap
# count itself NEVER blocks a bump (most of DOC 8643 SHOULD stay
# chevrons — rarities aren't worth a SOURCES line). What is release-
# blocking is the script failing to run at all: that means the shipped
# shapes data can't be parsed, i.e. the file the radar depends on is
# corrupt. Set AERODROME_SHAPE_GATE=strict to additionally block on a
# local-traffic gap of >=5% on a single type (you're staring at generic
# markers for a meaningful slice of your own sky).
#
# `check` (and so package-release.sh) passes --cache-only: it reads the
# cached scripts/doc8643.csv when present — the same data the `set` run used,
# so the strict gate reaches the same verdict — but never fetches (no 15 s
# network wait) and never writes the cache. `set` keeps the cache-first report
# with a best-effort fetch.
#
# Pass --skip-shapes to suppress the section entirely.
SHAPE_AUDIT="${SCRIPT_DIR}/scripts/shape_coverage.py"
SHAPE_ARGS=()
[ "$GATES_ONLY" = "1" ] && SHAPE_ARGS+=(--cache-only)
if [ "$SKIP_SHAPES" = "0" ] && [ -f "$SHAPE_AUDIT" ]; then
    echo "Type-shape coverage audit..."
    if ! python3 "$SHAPE_AUDIT" "${SHAPE_ARGS[@]}" 2>&1 | sed 's/^/  /'; then
        echo "✗ Type-shape coverage gate failed (see above): static/shapes-data.js"
        echo "  is unreadable/empty, the audit crashed, or (strict mode) a type"
        echo "  without a shape is >=5% of local traffic. Fix or regenerate"
        echo "  (scripts/gen_type_shapes.py) before releasing."
        nothing_written
        exit 1
    fi
    echo ""
elif [ "$SKIP_SHAPES" = "1" ]; then
    echo "⚠ Type-shape coverage gate SKIPPED (--skip-shapes) — package-release.sh re-runs it with no skips."
    echo ""
    SKIPPED_GATES+=("shapes")
else
    echo "⚠ Type-shape coverage gate SKIPPED: scripts/shape_coverage.py is missing from this tree."
    echo ""
    SKIPPED_GATES+=("shapes (script missing)")
fi

# =============================================================================
# Installer consistency — REQUIRED (v3.4.144+), no skip flag.
# =============================================================================
# scripts/check_installers.py: every tracked *.sh parses (`bash -n`);
# pkg_detect() is byte-identical in install.sh and scripts/bootstrap.sh
# (duplicated on purpose — each script must work standalone); the sudoers
# rules live only in install.sh's render_sudoers() and
# scripts/recover-sudoers.sh hands off to `install.sh --privileges-only`
# (v3.4.145+); no early-exit reader after a pipe in a pipefail script; and
# uninstall.sh removes ntfy's cache file. Nothing else keeps these in step —
# a drift ships an installer that detects a different package manager, a
# second copy of the rules, or a check that fails at random. Like the PII
# audit, there is no skip flag: fix the drift.
echo "Installer consistency check..."
if ! python3 "${SCRIPT_DIR}/scripts/check_installers.py" "$SCRIPT_DIR"; then
    echo ""
    echo "  !! Release aborted: installer scripts out of sync (above)."
    nothing_written
    exit 1
fi
echo ""

# =============================================================================
# PII audit — REQUIRED (advisory v3.4.30 → v3.4.32, required v3.4.33+).
# =============================================================================
# Scans every file git can see — tracked plus untracked-but-not-ignored, i.e.
# everything that can ship in a zip or be committed, wherever it sits (a
# tracked update/UPDATE_README.md ships, so it is scanned; only binary
# extensions are skipped). A linked worktree's `.git` pointer file and
# gitignored local files such as config.yaml or scripts/doc8643.csv never
# ship, so they are out of scope by policy. Outside a git checkout the
# fallback tree walk prunes build/runtime dirs and maintainer files instead.
# Plus the CHANGELOG entry this run is about to add, for
# patterns that look like maintainer-identifying info — names and
# real-network IPs. The audit shipped as advisory in v3.4.30 to give it
# a release cycle or two to surface false positives before becoming a
# blocker. Three releases later (v3.4.30, v3.4.31, v3.4.32) all ran the
# audit clean with no false positives — the documentation allowlist
# (RFC 5737, common docs subnets, 10.254.254.254) covers every legitimate  (pii-ok)
# documentation use. v3.4.33 graduates the audit to required: a findings
# print is now followed by a non-zero exit, which under `set -e` halts
# the bump. If a future legitimate documentation pattern trips this,
# extend the allowlist rather than reverting to advisory.
#
# What it flags:
#   • Names: forms identifying the maintainer that aren't the approved
#     GitHub-username form (which is allow-listed because it appears in
#     repository URLs throughout the docs).
#   • Concrete RFC1918 IPs that aren't in the documentation allowlist.
#     RFC 5737 ranges (192.0.2.x, 198.51.100.x, 203.0.113.x), the common
#     docs subnets 192.168.0.x / 192.168.1.x / 10.0.0.x, and the special
#     unroutable 10.254.254.254 (used by ntfy_installer.py's  (pii-ok)
#     _detect_lan_ip kernel-route trick) are all skipped.
#
# Expected hits: LICENSE (MIT copyright holder) and templates/about.html
# (the /about page's copyright line, added v3.4.18). These are the project's
# two approved attribution surfaces — every OSS project has them. A third
# hit is always a real leak.
#
# Like the static name-resolution check, the audit has no --skip-pii flag
# (per decision 5.17). If you really need to bypass it for an emergency,
# scrub the worked tree to clean and re-run — that's faster than adding a
# flag and remembering to remove it.
echo "PII audit (names + private-network IPs)..."
PII_AUDIT_PY=$(cat <<'PYEOF'
import os, re, subprocess, sys

# Negative lookahead suppresses the maintainer's GitHub-username form
# (the approved attribution string that appears in repository URLs).
# Case-insensitive across all branches.
NAME_RE = re.compile(
    r'\b' + 'pre' + 'ston(?!-pet' + 'erson)\\b'
    r'|\b' + 'pre' + 'ston@'
    r'|\b' + 'pro' + 'ton-ubnt\\b'
    r'|/home/' + 'pre' + 'ston',
    re.IGNORECASE,
)
IP_RE = re.compile(r'\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b')

def is_ip_pii(o1, o2, o3, o4):
    """True if this IP is a concrete RFC1918 IP that's NOT in the docs allowlist."""
    if any(o > 255 for o in (o1, o2, o3, o4)):
        return False  # not a valid IP, regex coincidence
    # Documentation / known-intentional allowlist:
    if o1 == 192 and o2 == 0 and o3 == 2:     return False  # RFC 5737 TEST-NET-1
    if o1 == 198 and o2 == 51 and o3 == 100:  return False  # RFC 5737 TEST-NET-2
    if o1 == 203 and o2 == 0 and o3 == 113:   return False  # RFC 5737 TEST-NET-3
    if o1 == 192 and o2 == 168 and o3 in (0, 1):  return False  # common docs subnets
    if o1 == 10 and o2 == 0 and o3 == 0:      return False  # common docs subnet
    if (o1, o2, o3, o4) == (10, 254, 254, 254):  return False  # _detect_lan_ip trick
    # Private ranges that flag (everything else in RFC1918):
    if o1 == 10:                               return True
    if o1 == 172 and 16 <= o2 <= 31:           return True
    if o1 == 192 and o2 == 168:                return True
    return False

# Binary formats are never scanned (they'd only produce regex coincidences).
SKIP_EXT = {'.db', '.db-wal', '.db-shm', '.pyc', '.png', '.jpg', '.jpeg',
            '.gif', '.pdf', '.zip', '.sha256', '.ico'}
# Fallback walk only (no git): build/cache/runtime dirs that don't ship, and
# maintainer-only files (Claude-to-Claude session notes). In a git checkout
# NONE of these name rules apply — a file git lists is tracked or committable,
# so it is in scope wherever it sits and whatever it is called (gitignore
# already leaves out the local copies).
SKIP_DIRS = {'.git', 'venv', '__pycache__', '.backups', 'node_modules',
             'logs', 'update', 'audits', 'graphify-out'}
def is_binary(fname):
    return any(fname.lower().endswith(e) for e in SKIP_EXT)
def walk_should_skip(fname):
    if 'HANDOFF' in fname or fname in ('CLAUDE.md', 'AGENT_GUARDRAILS.md'):
        return True
    return is_binary(fname)

def candidate_paths():
    """git's tracked + untracked-not-ignored files — what can ship or be
    committed — minus binaries. Gitignored local files (config.yaml,
    doc8643.csv, HANDOFF*) never ship, so they are out of scope by policy.
    Outside a git checkout (an unpacked release zip) — or when this tree is
    only a subdirectory of some other repository — walk the tree instead."""
    try:
        top = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                             capture_output=True, check=True).stdout
        top = top.decode('utf-8', 'surrogateescape').strip()
        if os.path.realpath(top) == os.path.realpath('.'):
            out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others',
                                  '--exclude-standard'], capture_output=True,
                                 check=True).stdout.decode('utf-8', 'surrogateescape')
            return ['./' + p for p in sorted(set(out.split('\0')))
                    if p and not is_binary(p.rsplit('/', 1)[-1])]
    except (OSError, subprocess.CalledProcessError):
        pass
    paths = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and (not d.startswith('.') or d == '.github')]
        paths += [os.path.join(root, f) for f in files
                  if f != '.git'                 # a linked worktree's .git is a pointer FILE
                  and not walk_should_skip(f)]
    return sorted(paths)

# Approved attribution surfaces. Names appearing in these files are not
# leaks — they're the project's deliberate copyright/license attribution.
NAME_ALLOWLIST_FILES = {'./LICENSE', './templates/about.html'}

name_hits = []
ip_hits = []

def scan(path, content):
    for lno, line in enumerate(content.splitlines(), 1):
        for m in NAME_RE.finditer(line):
            if path not in NAME_ALLOWLIST_FILES:
                name_hits.append((path, lno, m.group(0), line.strip()[:100]))
        for m in IP_RE.finditer(line):
            o = tuple(int(m.group(i)) for i in (1, 2, 3, 4))
            if is_ip_pii(*o):
                ip_hits.append((path, lno, m.group(0), line.strip()[:100]))

for path in candidate_paths():
    try:
        with open(path, encoding='utf-8') as f:
            content = f.read()
    except (UnicodeDecodeError, IsADirectoryError, OSError):
        continue   # binary, unreadable, or tracked-but-deleted
    scan(path, content)

# The CHANGELOG entry this run is about to insert (not on disk yet).
pending = os.environ.get('BUMP_PENDING_ENTRY', '')
if pending:
    scan('(CHANGELOG entry this bump will add)', pending)

clean = True

if name_hits:
    clean = False
    print(f'  ⚠ {len(name_hits)} name-pattern hit(s) outside approved attribution surfaces:')
    for path, lno, hit, ctx in name_hits:
        print(f'    • {path}:{lno}  match="{hit}"')
        print(f'        {ctx}')
else:
    print('  ✓ Names: clean (LICENSE + templates/about.html allowlisted)')

if ip_hits:
    clean = False
    print(f'  ⚠ {len(ip_hits)} concrete-private-IP hit(s) outside docs allowlist:')
    for path, lno, ip, ctx in ip_hits:
        print(f'    • {path}:{lno}  {ip}')
        print(f'        {ctx}')
else:
    print('  ✓ Private IPs: clean (docs subnets + 10.254.254.254 allowlisted)')  # pii-ok

if not clean:
    print('  (REQUIRED check: scrub the worked tree and re-run the bump)')
    sys.exit(1)  # required — halts the bump when findings are present
sys.exit(0)
PYEOF
)
if ! BUMP_PENDING_ENTRY="$PENDING_ENTRY" python3 -c "$PII_AUDIT_PY"; then
    nothing_written
    exit 1
fi
echo ""

if [ "$GATES_ONLY" = "1" ]; then
    if [ "${#SKIPPED_GATES[@]}" -gt 0 ]; then
        echo "⚠ Gates passed with ${#SKIPPED_GATES[@]} SKIPPED (${SKIPPED_GATES[*]}) — gates-only run, nothing written; NOT release-ready."
    else
        echo "✓ All required gates passed — gates-only run, nothing written."
    fi
    exit 0
fi
if [ "${#SKIPPED_GATES[@]}" -gt 0 ]; then
    echo "⚠ ${#SKIPPED_GATES[@]} gate(s) SKIPPED this run (${SKIPPED_GATES[*]}) — package-release.sh"
    echo "  re-runs every gate with no skips before it builds a zip."
    echo ""
fi

# =============================================================================
# Apply the bump — reached only when every required gate above passed.
# =============================================================================
# Order: version headers -> CHANGELOG -> VERSION LAST. VERSION is what
# package-release.sh keys off, so it flips only after everything else landed.
# Every write is idempotent (headers are rewritten to NEW_VERSION whatever they
# held; the CHANGELOG entry is inserted only when no "## [NEW_VERSION]" section
# exists, so an expanded entry is never touched) — if this phase dies midway,
# re-running the same command converges on the same tree.
if [ "$RERUN" = "1" ]; then
    echo "Re-applying $NEW_VERSION (idempotent)..."
else
    echo "Bumping version: $OLD_VERSION → $NEW_VERSION"
fi

# --- Update version string in all files (NOT changelog) ---
FILES=(
    "main.py"
    "collector.py"
    "server.py"
    "config_validator.py"
    "config.yaml"
    "config.yaml.example"
    "install.sh"
    "uninstall.sh"
    "requirements.txt"
    "templates/index.html"
    "templates/status.html"
    "templates/config.html"
    "templates/updates.html"
    "templates/docs.html"
    "templates/logs.html"
    "templates/diagnostics.html"
    "templates/diagnostics-watchlist.html"
    "templates/diagnostics-slow-queries.html"
    "templates/performance.html"
    "templates/aircraft.html"
    "templates/board.html"
    "templates/about.html"
    "templates/switch-to-real.html"
    "README.md"
    "bump-version.sh"
    "scripts/package-release.sh"
    "scripts/bootstrap.sh"
    "CONTRIBUTING.md"
)

for f in "${FILES[@]}"; do
    if [ -f "$f" ]; then
        # v2.40.2 fix: previously this script rewrote ANY occurrence of the
        # old version number surrounded by non-digit/non-dot chars. That
        # accidentally corrupted requirements.txt's `requests>=2.39.0` into
        # `requests>=2.40.0` etc. every release, because `=` is a non-digit
        # non-dot char. The real `requests` package has no version matching
        # our Aerodrome version, so once our version outpaced theirs, pip
        # install broke. Fix: only rewrite the explicit `# Version:` header
        # comment that bump-version is supposed to manage. Other version-like
        # strings in the file are left alone — they're either intentional
        # (dependency pins, CSS hex codes that happen to look numeric) or
        # meant to be updated by hand (changelog entries, code constants).
        #
        # v2.50.9 fix: previously this loop ran sed -i unconditionally and
        # printed "Updated $f" every time, even when the regex matched
        # nothing — masking exactly the drift v2.50.7 had to clean up
        # across 20 files (17 frozen at 2.42.12, 3 templates frozen even
        # earlier). The script's diagnostic was lying. Now we hash before
        # and after, only print "Updated" when the file actually changed.
        #
        # v2.97.8 fix: regex made drift-tolerant. The previous version
        # matched `# Version: ${OLD_VERSION}` literally, so a file whose
        # header had drifted (frozen at some earlier release) was never
        # caught up — every subsequent bump skipped it because the regex
        # didn't match. v2.97.8 replaces `${OLD_VERSION}` in the regex with
        # `[0-9]+\.[0-9]+\.[0-9]+` (any semver), so any file with a
        # Version: header gets rewritten to NEW_VERSION regardless of what
        # version it was previously at. Files with no Version: header
        # remain silent (nothing to update). When drift is being healed
        # (file was at some version other than OLD_VERSION), the message
        # surfaces the previous version so the maintainer sees the
        # catch-up happening; normal updates (file was at OLD_VERSION)
        # print just "Updated".
        before_hash=$(sha256sum "$f" | awk '{print $1}')
        # (-m1 makes grep stop reading the FILE, not a pipe: no early-exit
        # reader sits after a `|`, so pipefail can't turn SIGPIPE into a 141.)
        prev_header_ver=$(grep -m1 -oE "^[[:space:]]*(#|//|<!--|/\\*)[[:space:]]*Version:[[:space:]]+[0-9]+\\.[0-9]+\\.[0-9]+" "$f" 2>/dev/null | grep -oE "[0-9]+\\.[0-9]+\\.[0-9]+" || true)
        sed -i -E "s/^([[:space:]]*([#\"']|\\/\\/|\\/\\*|<!--)[[:space:]]*Version:[[:space:]]+)[0-9]+\\.[0-9]+\\.[0-9]+([[:space:]]|$|-->|\\*\\/)/\\1${NEW_VERSION}\\3/" "$f"
        after_hash=$(sha256sum "$f" | awk '{print $1}')
        if [ "$before_hash" != "$after_hash" ]; then
            if [ -n "$prev_header_ver" ] && [ "$prev_header_ver" != "$OLD_VERSION" ]; then
                echo "  Updated $f (drift caught up: v${prev_header_ver} → v${NEW_VERSION})"
            else
                echo "  Updated $f"
            fi
        fi
        # else: no Version: header in this file at all — silent, nothing to update.
    fi
done

# --- Add a new entry to CHANGELOG.md (NEW_ENTRY was built + PII-scanned above) ---
if [ "$CHANGELOG_HAS_ENTRY" = "1" ]; then
    echo "  CHANGELOG.md already has a [${NEW_VERSION}] section — left untouched"
elif [ -f "$CHANGELOG" ]; then
    # Insert the new entry right before the first "## [" section (or at the
    # end if there is none). The entry reaches awk through the ENVIRONMENT,
    # not `awk -v`: -v runs escape processing (gawk turns `10\.1` into
    # `10.1` and `\t` into a tab), so the text written could differ from the
    # text the PII audit just scanned. ENVIRON is verbatim in gawk, mawk and
    # busybox awk — the summary lands exactly as typed.
    BUMP_ENTRY="$NEW_ENTRY" awk '
        BEGIN { inserted = 0; entry = ENVIRON["BUMP_ENTRY"] }
        /^## \[/ && !inserted {
            print entry
            inserted = 1
        }
        { print }
        END {
            if (!inserted) print entry
        }
    ' "$CHANGELOG" > "${CHANGELOG}.tmp"
    mv "${CHANGELOG}.tmp" "$CHANGELOG"
    echo "  Updated CHANGELOG.md (added ${NEW_VERSION} entry)"
else
    echo "  WARNING: CHANGELOG.md not found, skipped"
fi

# --- VERSION last ---
if [ "$OLD_VERSION" != "$NEW_VERSION" ]; then
    echo "$NEW_VERSION" > "$VERSION_FILE"
fi

echo ""
echo "Done! All files now at version $NEW_VERSION"
echo ""
if [ "$CHANGELOG_HAS_ENTRY" = "0" ] && [ -f "$CHANGELOG" ]; then
    echo "CHANGELOG entry added under '### ${ENTRY_TYPE}':"
    echo "  - ${DESC}"
    echo ""
fi

# =============================================================================
# Regenerate docs/Aerodrome_Overview.pdf so the release zip includes the
# current version number, release count, and code-line stats.
# =============================================================================
# The PDF is included in every release (docs/Aerodrome_Overview.pdf is a
# checked-in artifact) so users who unzip a release always have an up-to-date
# overview they can share. The builder (scripts/build_overview_pdf.py) reads
# VERSION, CHANGELOG.md, and the actual source files to produce accurate
# numbers every time — no hand-edited constants.
#
# v2.42.15: restricted to minor/major bumps only. Patch bumps were typically
# single-bug fixes where the version number on the cover was the only thing
# that would change, and rebuilding a multi-page PDF for a cover-text update
# wasn't deemed a good trade.
#
# v2.97.13: reverted v2.42.15. The patch-skip caused the PDF to drift far
# from the source state — the v2.97.12 audit found the shipped PDF had been
# frozen at v2.87.0, missing 10+ patch releases worth of code-line stats,
# release counts, embedded screenshots (which had themselves drifted), and
# CHANGELOG-derived content. Patch bumps now rebuild the PDF too. The
# rebuild is fast (a few seconds) and the consistency benefit outweighs the
# build time. Pass --skip-pdf to opt out per-release if reportlab isn't
# available or during rapid iteration.
#
# v3.4.144: the build is deterministic (reportlab invariant mode: a fixed
# metadata date and document ID) — the same inputs give byte-identical bytes.
# Its inputs include source line counts, templates and the CHANGELOG, so an
# edit made after the first `set` WOULD change it; that's why the rebuild is
# skipped while ../aerodrome-packages/aerodrome-vNEW.zip exists (the zip is
# frozen once the box applies it — rm the zip + .sha256 first to rebuild).
# It runs after the gates, and is advisory: a failure is reported, never
# masked, and never blocks.
#
# Requires reportlab + pillow. Install with:
#   pip install -r requirements-dev.txt
PDF_BUILDER="${SCRIPT_DIR}/scripts/build_overview_pdf.py"
# reportlab lives in the repo venv on the build host, not in system python3.
PDF_PY="python3"
[ -x "${SCRIPT_DIR}/venv/bin/python3" ] && PDF_PY="${SCRIPT_DIR}/venv/bin/python3"
if [ "$SKIP_PDF" = "0" ] && [ -f "$PDF_BUILDER" ] && [ "$ALREADY_PACKAGED" = "1" ]; then
    echo "Overview PDF not rebuilt: v$NEW_VERSION is already packaged — the zip's PDF stands."
    echo "  (To rebuild: rm the zip + .sha256 FIRST — only if the box hasn't applied it —"
    echo "  then re-run this \`set\` and re-package.)"
    echo ""
elif [ "$SKIP_PDF" = "0" ] && [ -f "$PDF_BUILDER" ]; then
    echo "Rebuilding docs/Aerodrome_Overview.pdf (${PDF_PY})..."
    if "$PDF_PY" "$PDF_BUILDER" 2>&1 | sed 's/^/  /'; then
        echo "  ✓ PDF rebuilt"
    else
        echo "  ! PDF build failed — continuing anyway (the zip will carry the"
        echo "    PREVIOUS docs/Aerodrome_Overview.pdf, still stamped with its old version)."
        echo "    Install dev deps:  pip install -r requirements-dev.txt"
        echo "    Or pass --skip-pdf to silence this."
    fi
    echo ""
fi

# =============================================================================
# Documentation drift check — advisory.
# =============================================================================
# scripts/check_docs.py scans for common ways documentation gets stale:
# version-header drift in source files, broken README links, missing or
# stale screenshots, project-structure tree that doesn't match reality,
# stale overview PDF. It's advisory — prints warnings but never fails the
# bump, so a minor inconsistency can't block a release when the release
# itself is correct. Fix what matters, ignore what doesn't.
#
# Pass --skip-docs-drift to suppress this section entirely.
DOCS_CHECKER="${SCRIPT_DIR}/scripts/check_docs.py"
if [ "$SKIP_DOCS_DRIFT" = "0" ] && [ -f "$DOCS_CHECKER" ]; then
    python3 "$DOCS_CHECKER" 2>&1 | sed 's/^/  /' \
        || echo "  ! docs-drift checker exited non-zero (advisory — continuing)"
    echo ""
fi

# =============================================================================
# Tech-debt audit freshness reminder — advisory, minor/major only.
# =============================================================================
# v2.46.0: print a one-line reminder if the tech-debt audit report is more
# than 60 days old (or missing). The audit is a static scanner at
# scripts/tech_debt_audit.py that produces docs/tech-debt-audit.md — it's
# deliberately NOT auto-rerun on every bump because it would spam noise
# about findings you've already decided to leave alone. But it does need
# to resurface periodically. Minor/major bumps are the natural moment.
# Patch bumps skip this check — patches are for bug fixes, not audits.
AUDIT_REPORT="${SCRIPT_DIR}/docs/tech-debt-audit.md"
if [ "$CMD" = "minor" ] || [ "$CMD" = "major" ]; then
    if [ ! -f "$AUDIT_REPORT" ]; then
        echo "  ⚠ Tech-debt audit has never been run on this install."
        echo "    Consider:  python3 scripts/tech_debt_audit.py"
        echo ""
    else
        # Cross-platform mtime-in-days check. 'stat -c %Y' works on Linux,
        # 'stat -f %m' on macOS. Fall back gracefully if neither works.
        NOW_TS=$(date +%s)
        if AUDIT_TS=$(stat -c %Y "$AUDIT_REPORT" 2>/dev/null); then :
        elif AUDIT_TS=$(stat -f %m "$AUDIT_REPORT" 2>/dev/null); then :
        else AUDIT_TS=""; fi
        if [ -n "$AUDIT_TS" ]; then
            AGE_DAYS=$(( (NOW_TS - AUDIT_TS) / 86400 ))
            if [ "$AGE_DAYS" -gt 60 ]; then
                echo "  ⚠ Tech-debt audit is ${AGE_DAYS} days old."
                echo "    Consider re-running:  python3 scripts/tech_debt_audit.py"
                echo ""
            fi
        fi
    fi
fi

echo ""
echo "Next steps for a release:"
echo "  1. Expand the auto-generated CHANGELOG entry into the explanatory voice"
echo "     (bold lead → user-voice paragraph → 'Behind the scenes:' dev paragraph)."
echo "     See any recent v2.50.x+ entry as a model."
echo "  2. Package: bash scripts/package-release.sh"
echo "     (re-runs \`bump-version.sh check\` with no skips, then proves the zip == the"
echo "      working tree's tracked files) → ../aerodrome-packages/aerodrome-v${NEW_VERSION}.zip + .sha256"
echo "  3. bash scripts/package-release.sh --verify, then stage for preview"
echo "     (maintainer box): bash ~/git/scripts/stage-aerodrome.sh"
echo "  4. After the box applies v${NEW_VERSION} it is FROZEN: don't re-\`set\` or repackage"
echo "     v${NEW_VERSION} — re-gate with \`bash bump-version.sh check\`. Then commit →"
echo "     bash scripts/publish-release.sh ${NEW_VERSION} --target <sha> --check-only → push →"
echo "     bash scripts/publish-release.sh ${NEW_VERSION} --target <sha>"
echo "     (Derives tag/title/assets/notes and runs the verify loop. Do NOT"
echo "      hand-run 'gh release create'.)"
echo ""
echo "Or if you're bumping a live install for development, restart the service:"
echo "  sudo systemctl restart aerodrome"
