#!/bin/bash
# Version: 3.4.66
# =============================================================================
# bump-version.sh — Bump Aerodrome version + auto-update CHANGELOG.md
# =============================================================================
#
# Usage:
#   ./bump-version.sh patch "What changed"
#   ./bump-version.sh minor "What changed"
#   ./bump-version.sh major "What changed"
#   ./bump-version.sh set 4.2.1 "What changed"
#
# Options:
#   --type=added|changed|fixed|removed   (default: changed)
#   --skip-docs-check / -y               (skip the pre-bump docs checklist
#                                         shown for minor/major bumps)
#   --skip-pdf                           (skip rebuilding the Overview PDF —
#                                         useful when iterating fast and you
#                                         know the PDF hasn't changed, or if
#                                         reportlab isn't installed)
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
#
# Examples:
#   ./bump-version.sh patch "Fixed sorting bug on Live tab"
#   ./bump-version.sh minor "Added export to CSV" --type=added
#   ./bump-version.sh patch "Better error messages" --type=fixed
#
# Without arguments, shows current version and what each bump would produce.
#
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION_FILE="${SCRIPT_DIR}/VERSION"
CHANGELOG="${SCRIPT_DIR}/CHANGELOG.md"

if [ ! -f "$VERSION_FILE" ]; then
    echo "ERROR: VERSION file not found at $VERSION_FILE"
    exit 1
fi

OLD_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')
IFS='.' read -r MAJOR MINOR PATCH <<< "$OLD_VERSION"

# Parse type flag from any position
ENTRY_TYPE="Changed"
SKIP_DOCS_CHECK=0
SKIP_PDF=0
SKIP_DOCS_DRIFT=0
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
        --skip-name-check)   SKIP_NAME_CHECK=1 ;;
        *) ARGS+=("$arg") ;;
    esac
done

CMD="${ARGS[0]}"

case "$CMD" in
    patch)
        PATCH=$((PATCH + 1))
        DESC="${ARGS[1]}"
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        DESC="${ARGS[1]}"
        ;;
    major)
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
        IFS='.' read -r MAJOR MINOR PATCH <<< "${ARGS[1]}"
        DESC="${ARGS[2]}"
        ;;
    *)
        echo "Usage: $0 {patch|minor|major|set <version>} \"description\" [--type=added|changed|fixed|removed]"
        echo ""
        echo "Current version: $OLD_VERSION"
        echo ""
        echo "  patch  $OLD_VERSION → ${MAJOR}.${MINOR}.$((PATCH + 1))"
        echo "  minor  $OLD_VERSION → ${MAJOR}.$((MINOR + 1)).0"
        echo "  major  $OLD_VERSION → $((MAJOR + 1)).0.0"
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

if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    echo "Version is already $NEW_VERSION"
    exit 0
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

echo "Bumping version: $OLD_VERSION → $NEW_VERSION"

# --- Update VERSION file ---
echo "$NEW_VERSION" > "$VERSION_FILE"

# --- Update version string in all files (NOT changelog) ---
cd "$SCRIPT_DIR"
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
    "templates/about.html"
    "templates/switch-to-real.html"
    "README.md"
    "bump-version.sh"
    "scripts/package-release.sh"
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
        prev_header_ver=$(grep -oE "^[[:space:]]*(#|//|<!--|/\\*)[[:space:]]*Version:[[:space:]]+[0-9]+\\.[0-9]+\\.[0-9]+" "$f" 2>/dev/null | head -1 | grep -oE "[0-9]+\\.[0-9]+\\.[0-9]+" || true)
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

# --- Add a new entry to CHANGELOG.md ---
TODAY=$(date +%Y-%m-%d)
NEW_ENTRY="## [${NEW_VERSION}] — ${TODAY}

### ${ENTRY_TYPE}
- ${DESC}
"

if [ -f "$CHANGELOG" ]; then
    # Insert the new entry right after the introductory paragraph
    # (after the first line starting with "follows [Semantic Versioning]")
    # If no such marker exists, prepend after the first heading.
    awk -v entry="$NEW_ENTRY" '
        BEGIN { inserted = 0 }
        /^## \[/ && !inserted {
            print entry
            inserted = 1
        }
        { print }
        END {
            if (!inserted) print entry
        }
    ' "$CHANGELOG" > "${CHANGELOG}.tmp" && mv "${CHANGELOG}.tmp" "$CHANGELOG"
    echo "  Updated CHANGELOG.md (added ${NEW_VERSION} entry)"
else
    echo "  WARNING: CHANGELOG.md not found, skipped"
fi

echo ""
echo "Done! All files now at version $NEW_VERSION"
echo ""
echo "CHANGELOG entry added under '### ${ENTRY_TYPE}':"
echo "  - ${DESC}"
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
if ! ( cd "$SCRIPT_DIR" && python3 -c "$IMPORT_CHECK_SCRIPT" 2>&1 | sed "s/^/  /" && [ "${PIPESTATUS[0]}" = "0" ] ); then
    echo ""
    echo "  !! Release aborted. Fix the import issue above and re-run."
    echo "  !! The version has been bumped and CHANGELOG updated, but no"
    echo "  !! zip has been produced — your working tree is in a partially"
    echo "  !! released state. Run the bump again after fixing the code."
    exit 1
fi
echo ""

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
# Requires reportlab + pillow. Install with:
#   pip install -r requirements-dev.txt
PDF_BUILDER="${SCRIPT_DIR}/scripts/build_overview_pdf.py"
if [ "$SKIP_PDF" = "0" ] && [ -f "$PDF_BUILDER" ]; then
    echo "Rebuilding docs/Aerodrome_Overview.pdf..."
    if python3 "$PDF_BUILDER" 2>&1 | sed 's/^/  /'; then
        echo "  ✓ PDF rebuilt"
    else
        echo "  ! PDF build failed — continuing anyway."
        echo "    Install dev deps:  pip install -r requirements-dev.txt"
        echo "    Or pass --skip-pdf to silence this."
    fi
    echo ""
fi

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
        exit 1
    fi
    echo ""
fi

# =============================================================================
# Inline-JS parse check — advisory (v3.4.27+).
# =============================================================================
# For every template with inline <script> blocks, concatenate that
# template's blocks and run `node --check` on the result. Catches
# syntax errors inside any one template's JS that the Python AST and
# name-resolution checks can't see (they only check Python).
#
# Important caveat: parses each template INDEPENDENTLY (not all
# templates concatenated). Different templates legitimately declare
# the same identifiers (`let activeTab`, `const cfg`, etc.) because
# the browser loads them as separate documents — a concatenated parse
# would false-positive on those collisions.
#
# Will NOT catch the specific v3.4.25 timefmt-injection regression
# that motivated this — that wasn't a parse error, it was a deleted
# line in _serve_template that left an HTML script reference
# unrendered. Catching that class would require a server-side check
# that the served HTML actually contains <script src> tags for every
# JS file the inline code references. That's a separate, more
# invasive check; the inline-parse pass here is the cheap one that
# wouldn't false-positive.
#
# Skipped silently if node isn't installed. Always advisory — never
# gates a release — until validated across at least one release cycle.
NODE_BIN="$(command -v node 2>/dev/null || true)"
if [ -n "$NODE_BIN" ]; then
    echo "Inline JavaScript parse check..."
    JS_PARSE_PY=$(cat <<'PYEOF'
import re, sys, subprocess, tempfile
from pathlib import Path

tmpl_dir = Path('templates')
if not tmpl_dir.is_dir():
    sys.exit(0)

ok = 0
failed = []
total_bytes = 0
for path in sorted(tmpl_dir.glob('*.html')):
    html = path.read_text()
    blocks = re.findall(r'<script>\s*(.*?)</script>', html, re.DOTALL)
    if not blocks:
        continue
    js = '\n'.join(blocks)
    total_bytes += len(js)
    with tempfile.NamedTemporaryFile(
            'w', suffix='.js', delete=False, encoding='utf-8') as tf:
        tf.write(js)
        tmpname = tf.name
    try:
        r = subprocess.run(
            ['node', '--check', tmpname],
            capture_output=True, text=True)
        if r.returncode == 0:
            ok += 1
        else:
            failed.append((path.name, r.stderr.strip()))
    finally:
        Path(tmpname).unlink(missing_ok=True)

print(f'  Parsed {ok + len(failed)} templates ({total_bytes} bytes inline JS)')
if failed:
    print(f'  ⚠ {len(failed)} template(s) failed parse:')
    for name, err in failed:
        # Show only the first line of stderr (the error message proper);
        # node's stack traces are long and not useful here.
        first = err.split('\n')[0] if err else '(no error message)'
        print(f'    • {name}: {first}')
    print('  (advisory at this release; not gating)')
    sys.exit(0)  # advisory — don't fail the bump
print('  ✓ Inline JS parse OK across all templates')
PYEOF
)
    python3 -c "$JS_PARSE_PY"
    echo ""
fi

# =============================================================================
# PII audit — REQUIRED (advisory v3.4.30 → v3.4.32, required v3.4.33+).
# =============================================================================
# Scans all worked-tree files (excluding build/cache/data/HANDOFF) for
# patterns that look like maintainer-identifying info — names and
# real-network IPs. The audit shipped as advisory in v3.4.30 to give it
# a release cycle or two to surface false positives before becoming a
# blocker. Three releases later (v3.4.30, v3.4.31, v3.4.32) all ran the
# audit clean with no false positives — the documentation allowlist
# (RFC 5737, common docs subnets, 10.254.254.254) covers every legitimate
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
#     unroutable 10.254.254.254 (used by ntfy_installer.py's
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
import os, re, sys

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

# Skip build/cache/data dirs that don't ship in the release, plus HANDOFF
# files (Claude-to-Claude session continuity, not public release artifacts).
SKIP_DIRS = {'.git', 'venv', '__pycache__', '.backups', 'node_modules',
             'logs', 'update', '.tracker.pid'}
SKIP_EXT = {'.db', '.db-wal', '.db-shm', '.pyc', '.png', '.jpg', '.jpeg',
            '.gif', '.pdf', '.zip', '.sha256', '.ico'}
def should_skip(fname):
    # Maintainer-only files (gitignored, and stripped by package-release.sh)
    # never ship in the zip or get pushed, so they can't leak — allowlist
    # them. Keep this set in sync with .gitignore's maintainer-only group
    # and package-release.sh's strip list. (.claude/ is already pruned as a
    # dot-dir in the walk below.)
    if 'HANDOFF' in fname or fname in ('CLAUDE.md', 'AGENT_GUARDRAILS.md'):
        return True
    return any(fname.lower().endswith(e) for e in SKIP_EXT)

# Approved attribution surfaces. Names appearing in these files are not
# leaks — they're the project's deliberate copyright/license attribution.
NAME_ALLOWLIST_FILES = {'./LICENSE', './templates/about.html'}

name_hits = []
ip_hits = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs
               if d not in SKIP_DIRS and not d.startswith('.')]
    for fname in files:
        if should_skip(fname):
            continue
        path = os.path.join(root, fname)
        try:
            with open(path, encoding='utf-8') as f:
                content = f.read()
        except (UnicodeDecodeError, IsADirectoryError, OSError):
            continue
        for lno, line in enumerate(content.splitlines(), 1):
            for m in NAME_RE.finditer(line):
                if path not in NAME_ALLOWLIST_FILES:
                    name_hits.append((path, lno, m.group(0), line.strip()[:100]))
            for m in IP_RE.finditer(line):
                o = tuple(int(m.group(i)) for i in (1, 2, 3, 4))
                if is_ip_pii(*o):
                    ip_hits.append((path, lno, m.group(0), line.strip()[:100]))

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
    print('  ✓ Private IPs: clean (docs subnets + 10.254.254.254 allowlisted)')

if not clean:
    print('  (REQUIRED check: scrub the worked tree and re-run the bump)')
    sys.exit(1)  # required — halts the bump when findings are present
sys.exit(0)
PYEOF
)
python3 -c "$PII_AUDIT_PY"
echo ""

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
    python3 "$DOCS_CHECKER" 2>&1 | sed 's/^/  /'
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
echo "  2. Package the release:"
echo "     bash scripts/package-release.sh"
echo "     Produces ../aerodrome-v${NEW_VERSION}.zip and ../aerodrome-v${NEW_VERSION}.zip.sha256"
echo "     ready for upload to the GitHub Release."
echo ""
echo "Or if you're bumping a live install for development, restart the service:"
echo "  sudo systemctl restart aerodrome"
