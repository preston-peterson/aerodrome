#!/bin/bash
# Version: 3.4.111
# =============================================================================
# package-release.sh — Build the release zip + SHA256 for the current VERSION
# =============================================================================
#
# Run after you've finished a release flow:
#   1. Make the code change
#   2. bash bump-version.sh patch "What changed"
#   3. Manually expand the auto-generated CHANGELOG entry into the
#      explanatory paragraph voice (bold lead → user voice → "Behind the
#      scenes:" → dev voice). See any recent v2.50.x+ entry as a model.
#   4. bash scripts/package-release.sh    ← you are here
#
# Produces, in an aerodrome-packages/ dir alongside the project directory
# (../aerodrome-packages/ — created on first run, shared by every build so
# artifacts don't litter the repo's parent):
#   aerodrome-vX.Y.Z.zip          — the release tree, with __pycache__
#                                    and *.pyc stripped, in a top-level
#                                    aerodrome-vX.Y.Z/ wrapper folder.
#   aerodrome-vX.Y.Z.zip.sha256   — checksum file in `sha256sum -c` format,
#                                    so the curl-install bootstrap can
#                                    verify the download with one call.
#
# Refuses to clobber existing artifacts at those paths — remove them first
# if you really want to repackage. This protects against silently
# overwriting a release zip you've already uploaded.
#
# Why this lives separate from bump-version.sh:
#   bump-version.sh writes an auto-generated single-line CHANGELOG entry
#   that the maintainer then expands by hand into the explanatory voice.
#   Packaging the zip BEFORE that expansion would ship a release whose
#   internal CHANGELOG.md doesn't match the rich entries every other
#   release has. Splitting packaging into a separate step makes the
#   "expand CHANGELOG first" requirement explicit in the workflow.
#
# Options:
#   -h, --help     Show this help and exit.
#
# =============================================================================

set -e

# Self-locate. SCRIPT_DIR is .../scripts; PROJECT_DIR is its parent.
SCRIPT_FILE="$(readlink -f "$0" 2>/dev/null || echo "$0")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_FILE")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARENT_DIR="$(cd "$PROJECT_DIR/.." && pwd)"

GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
YELLOW='\033[33m\033[1m'
RESET='\033[0m'

case "${1:-}" in
    -h|--help)
        sed -n '1,/^# =\{20,\}$/p' "$0" | sed -n '/^# /p' | sed 's/^# \?//'
        exit 0
        ;;
    "") ;;
    *)
        echo "Unknown argument: $1 (use --help for usage)" >&2
        exit 1
        ;;
esac

# --- Read version from VERSION file ---
VERSION_FILE="${PROJECT_DIR}/VERSION"
if [ ! -f "$VERSION_FILE" ]; then
    echo -e "${RED}ERROR:${RESET} VERSION file not found at $VERSION_FILE" >&2
    exit 1
fi
VERSION=$(tr -d '[:space:]' < "$VERSION_FILE")

if [ -z "$VERSION" ]; then
    echo -e "${RED}ERROR:${RESET} VERSION file is empty" >&2
    exit 1
fi

RELEASE_DIR_NAME="aerodrome-v${VERSION}"
RELEASE_DIR="${PARENT_DIR}/${RELEASE_DIR_NAME}"          # temp staging tree (removed after zip)
# Finished artifacts collect in a dedicated sibling dir so they don't litter
# the repo's parent — one folder holds every release/RC zip + sidecar. The
# staging tree above still lives directly in PARENT_DIR (it's transient).
PACKAGES_DIR="${PARENT_DIR}/aerodrome-packages"
RELEASE_ZIP="${PACKAGES_DIR}/${RELEASE_DIR_NAME}.zip"
RELEASE_SHA="${RELEASE_ZIP}.sha256"

echo "Packaging aerodrome v${VERSION}..."

# --- Refuse to clobber existing artifacts ---
if [ -e "$RELEASE_DIR" ] || [ -e "$RELEASE_ZIP" ] || [ -e "$RELEASE_SHA" ]; then
    echo -e "  ${YELLOW}⚠${RESET}  Release artifacts already exist:"
    [ -e "$RELEASE_DIR" ] && echo "    $RELEASE_DIR"
    [ -e "$RELEASE_ZIP" ] && echo "    $RELEASE_ZIP"
    [ -e "$RELEASE_SHA" ] && echo "    $RELEASE_SHA"
    echo "  Remove them first if you really want to repackage. Refusing to"
    echo "  silently overwrite an existing release that may already be uploaded."
    exit 1
fi

# --- Stage the release tree in the canonical wrapper-folder layout ---
# 2026-08-01: staged from `git ls-files` (tracked files ONLY), not `cp -r` of the
# working tree. cp -r made every untracked local file a release passenger unless a
# strip rule below named it — which is how audits/ shipped in the published
# v3.4.108 zip and a runtime logs/tracker.log shipped in v3.4.109. Tracked-only
# staging ends the class at the source; the strip rules below become
# belt-and-suspenders and the gate after them is the proof.
mkdir -p "$RELEASE_DIR"
( cd "$PROJECT_DIR" && git ls-files -z | \
  while IFS= read -r -d '' f; do
      mkdir -p "$RELEASE_DIR/$(dirname "$f")"
      cp -p "$f" "$RELEASE_DIR/$f"
  done )

# Strip Python caches.
find "$RELEASE_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$RELEASE_DIR" -name '*.pyc' -delete 2>/dev/null || true

# Strip local-only paths that should never ship.
#   venv     — created by install.sh, not part of the source tree
#   .git     — we're shipping a zip, not a clone
#   .backups — live install snapshots
#   update   — staging area for the in-app updater (live-install local state)
#   *.bak.*  — config backups created by the auto-merge on startup
#   .tracker.pid, *.db, *.db-wal, *.db-shm — runtime files
#
# v3.1.0 note: `tools/` used to be excluded here (maintainer-only
# dev-tooling, shipped as the separate -synthetic-feeder.zip artifact).
# Demo mode runs the synthetic feeder as a real service at install
# time, so tools/synthetic_feeder/ now needs to be inside the release
# zip — the install.sh --demo branch references it from the install
# tree at runtime. The exclusion has been removed.
rm -rf "$RELEASE_DIR/venv" \
       "$RELEASE_DIR/.git" \
       "$RELEASE_DIR/.backups" \
       "$RELEASE_DIR/.claude" \
       "$RELEASE_DIR/audits" \
       "$RELEASE_DIR/graphify-out" \
       "$RELEASE_DIR/docs/tech-debt-audit.md" 2>/dev/null || true
# Also strip any runtime DB / pid / config-backup files that may be present
# if package-release.sh is run on a live install rather than a clean tree.
# v3.4.33: HANDOFF files (any path matching *-HANDOFF*.md or HANDOFF.md) are
# also stripped here. HANDOFFs are Claude-to-Claude session continuity, not
# public release artifacts; building them in the worked tree alongside
# release prep used to require either remembering to delete them before
# packaging or moving them to a parent directory. The maxdepth-1 strip
# covers the workflow where bump-version.sh emits a HANDOFF alongside the
# other release files; nested copies in docs/ or similar are intentionally
# NOT stripped (those would be deliberate documentation, not session notes).
# v3.4.60: also strip the maintainer-only files (gitignored, never public) —
# CLAUDE.md, AGENT_GUARDRAILS.md, and ALL HANDOFF*.md (the old `*-HANDOFF*.md`
# missed `HANDOFF-handheld.md`) — plus a live config.yaml if one is present
# (its example template still ships). `.claude/` is removed in the rm -rf
# above. Keep this set in sync with .gitignore's maintainer-only group and the
# bump-version.sh PII-audit allowlist (Lesson 4.15: one policy, three files).
find "$RELEASE_DIR" -maxdepth 1 \
    \( -name '.tracker.pid' \
    -o -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \
    -o -name 'config.yaml' -o -name 'config.yaml.bak.*' \
    -o -name 'HANDOFF*.md' -o -name '*-HANDOFF*.md' \
    -o -name 'CLAUDE.md' -o -name 'AGENT_GUARDRAILS.md' \
    -o -name '.graphifyignore' \) \
    -delete 2>/dev/null || true

# --- Tracked-files-only gate (2026-08-01) ---
# The strip lists above are a DENYLIST, and denylists lose: every new local-only
# dir is a leak by default until someone remembers to add it (audits/ shipped in
# the published v3.4.108 zip exactly this way; graphify-out/ nearly followed).
# This gate is the ALLOWLIST that ends the class: every file in the staging tree
# must be one git tracks. Anything untracked in the zip → hard fail, listing the
# offenders. Structural, zero curation. (Lesson 4.15 retired for packaging: the
# policy is now "git ls-files", not three hand-synced lists.)
UNTRACKED_IN_ZIP=$(comm -23 \
    <(cd "$RELEASE_DIR" && find . -type f | sed 's|^\./||' | LC_ALL=C sort) \
    <(cd "$PROJECT_DIR" && git ls-files | LC_ALL=C sort))
if [ -n "$UNTRACKED_IN_ZIP" ]; then
    echo -e "  ${RED}✗${RESET} Refusing to package: the staging tree contains files git does not track:"
    echo "$UNTRACKED_IN_ZIP" | sed 's/^/      /'
    echo "  If a file SHOULD ship, commit it. If it's local-only, it never belongs in a zip."
    rm -rf "$RELEASE_DIR"
    exit 1
fi
echo -e "  ${GREEN}✓${RESET} Tracked-files-only gate: every staged file is git-tracked"

# --- Zip ---
# Write the finished zip straight into the packages dir; archive the staging
# tree by its relative name from PARENT_DIR so the zip's internal paths stay
# the clean single-wrapper layout.
mkdir -p "$PACKAGES_DIR"
( cd "$PARENT_DIR" && zip -rq "$RELEASE_ZIP" "$RELEASE_DIR_NAME" )

# --- SHA256 ---
# Generated in path-relative (bare-name) form, sitting beside the zip in the
# packages dir, so `sha256sum -c <name>.sha256` works in that directory
# without path massaging.
( cd "$PACKAGES_DIR" && sha256sum "${RELEASE_DIR_NAME}.zip" > "${RELEASE_DIR_NAME}.zip.sha256" )

# --- Clean up staging directory ---
rm -rf "$RELEASE_DIR"

# --- Report ---
ZIP_SIZE=$(du -h "$RELEASE_ZIP" | cut -f1)
SHA_HASH=$(awk '{print $1}' "$RELEASE_SHA")
echo -e "  ${GREEN}✓${RESET} ${RELEASE_DIR_NAME}.zip (${ZIP_SIZE})"
echo -e "  ${GREEN}✓${RESET} ${RELEASE_DIR_NAME}.zip.sha256 (sha256: ${SHA_HASH:0:16}...)"
echo ""
echo "Both artifacts ready for GitHub Release upload at:"
echo "  $RELEASE_ZIP"
echo "  $RELEASE_SHA"
echo ""
