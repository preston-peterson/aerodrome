#!/bin/bash
# Version: 3.0.6
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
