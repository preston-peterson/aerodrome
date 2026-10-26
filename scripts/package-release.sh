#!/bin/bash
# Version: 3.4.146
# =============================================================================
# package-release.sh — Build the release zip + SHA256 for the current VERSION
# =============================================================================
#
# Usage:
#   bash scripts/package-release.sh            build the zip + .sha256
#   bash scripts/package-release.sh --verify   re-prove the existing zip still
#                                              equals the working tree (builds
#                                              nothing) — run it right before
#                                              staging
#   bash scripts/package-release.sh --help
#
# Where it sits in a release:
#   0. python3 -m unittest is green (slow suites included)
#   1. bash bump-version.sh set X.Y.Z "What changed" -y --type=…
#   2. Expand the auto-generated CHANGELOG entry into the explanatory voice
#      (bold lead → user voice → "Behind the scenes" → dev voice).
#   3. bash scripts/package-release.sh             ← builds
#   4. bash scripts/package-release.sh --verify    ← then stage for preview
#
# What a build does, in order — any failure leaves NO zip, .sha256 or
# staging dir behind, so a retry is never blocked:
#   - Pre-flight: refuses untracked, non-ignored files (the gates would see
#     them — an untracked module a tracked one imports passes the import
#     check — but the zip, made of tracked files only, would not contain
#     them: `git add` them to ship, or gitignore/remove them). Then re-runs
#     `bash bump-version.sh check` — every required gate, with no skip flags
#     (this is where the hand-expanded CHANGELOG gets its PII scan) — and
#     requires CHANGELOG.md's top `## [` heading to equal VERSION (a halted
#     bump is caught here).
#   - Snapshot: records the tracked files' working-tree content as a git
#     tree, through a throwaway index (.git/index is never touched).
#   - Refuses any maintainer-only / local path in that snapshot (the ONE
#     denylist, scripts/check_release_zip.py --deny-paths) — here, while
#     repackaging is still free, not after the box applied the build.
#   - Stages the git-tracked files only into an aerodrome-vX.Y.Z/ wrapper
#     (a tracked file deleted without `git rm` is refused, whatever its
#     position in the list), strips caches and maintainer-only files, and
#     refuses any untracked file in the staging tree.
#   - Zips, then proves zip == `git archive <snapshot>` (same files, bytes and
#     exec bits; scripts/check_release_zip.py) before writing the .sha256.
#
# --verify re-proves an existing zip against a fresh snapshot of the working
# tree (and refuses untracked, non-ignored files, as above), so an edit made
# after packaging is caught while repackaging is still free (before the box
# applies the build).
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
# if you really want to repackage. AFTER THE BOX HAS APPLIED X.Y.Z, NEVER
# REPACKAGE IT: the Local-update card only offers a newer version, so the
# zip is frozen and is the source of truth. A zip/commit mismatch is then
# fixed by making the commit match the zip, or by using up a patch version.
#
# Refuses to run in a linked git worktree: its ../aerodrome-packages is not
# the release dir. Package from the main checkout (or a scratch clone).
#
# Why this lives separate from bump-version.sh:
#   bump-version.sh writes an auto-generated single-line CHANGELOG entry
#   that the maintainer then expands by hand into the explanatory voice.
#   Packaging the zip BEFORE that expansion would ship a release whose
#   internal CHANGELOG.md doesn't match the rich entries every other
#   release has. Splitting packaging into a separate step makes the
#   "expand CHANGELOG first" requirement explicit in the workflow.
#
# =============================================================================

set -euo pipefail

# Self-locate. SCRIPT_DIR is .../scripts; PROJECT_DIR is its parent.
SCRIPT_FILE="$(readlink -f "$0" 2>/dev/null || echo "$0")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_FILE")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARENT_DIR="$(cd "$PROJECT_DIR/.." && pwd)"

GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
YELLOW='\033[33m\033[1m'
RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; exit 1; }

MODE="package"
case "${1:-}" in
    -h|--help)
        # The header comment between the title rule and the closing rule.
        awk 'NR > 5 && /^# =====/ {exit}
             NR > 5 {sub(/^# ?/, ""); print}' "$SCRIPT_FILE"
        exit 0
        ;;
    --verify) MODE="verify" ;;
    "") ;;
    *)
        echo "Unknown argument: $1 (use --help for usage)" >&2
        exit 1
        ;;
esac
[ $# -le 1 ] || { echo "Too many arguments (use --help for usage)" >&2; exit 1; }

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

# --- Main checkout only ---
# A linked worktree resolves ../aerodrome-packages somewhere that is not the
# release dir (and the zip would not be the checkout the box is staged from).
GIT_DIR_ABS="$(git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-dir 2>/dev/null)" \
    || fail "$PROJECT_DIR is not a usable git checkout — packaging stages git-tracked files only"
GIT_COMMON_ABS="$(git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || fail "$PROJECT_DIR is not a usable git checkout — packaging stages git-tracked files only"
if [ "$GIT_DIR_ABS" != "$GIT_COMMON_ABS" ]; then
    fail "this is a linked worktree — its ../aerodrome-packages is not the release dir. Package from the main checkout (or a scratch clone)."
fi

RELEASE_DIR_NAME="aerodrome-v${VERSION}"
RELEASE_DIR="${PARENT_DIR}/${RELEASE_DIR_NAME}"          # temp staging tree (removed after zip)
# Finished artifacts collect in a dedicated sibling dir so they don't litter
# the repo's parent — one folder holds every release/RC zip + sidecar. The
# staging tree above still lives directly in PARENT_DIR (it's transient).
PACKAGES_DIR="${PARENT_DIR}/aerodrome-packages"
RELEASE_ZIP="${PACKAGES_DIR}/${RELEASE_DIR_NAME}.zip"
RELEASE_SHA="${RELEASE_ZIP}.sha256"

# Prints the tree id of the tracked files' WORKING-TREE content (what the
# staging step below copies). A throwaway copy of the index takes the
# `git add -u`, so .git/index is never touched — this works with an
# index.lock held, with intent-to-add entries, and in any checkout.
# Untracked files are excluded, exactly as packaging excludes them.
snapshot_tree() {
    local idx
    idx="$(mktemp "${TMPDIR:-/tmp}/aerodrome-pkg-index.XXXXXX")"
    ( cd "$PROJECT_DIR" && cp "$(git rev-parse --git-path index)" "$idx" \
      && GIT_INDEX_FILE="$idx" git add -u \
      && GIT_INDEX_FILE="$idx" git write-tree ) || { rm -f "$idx"; return 1; }
    rm -f "$idx"
}

# The gates see the working tree (the import check imports from it; the PII
# audit lists untracked files too), but the zip holds tracked files only. An
# untracked, non-ignored file is therefore checked yet never shipped — e.g. an
# untracked module that a tracked module imports passes every gate and the
# zip can't start. Refuse the mismatch outright: there is nothing to curate.
refuse_untracked() {
    local u
    u="$(git -C "$PROJECT_DIR" ls-files --others --exclude-standard)" \
        || fail "cannot list untracked files"
    [ -z "$u" ] || fail "untracked files the gates can see but the zip would not contain:
$(printf '%s\n' "$u" | sed 's/^/      /')
    git add them (to ship) or gitignore/remove them; $1"
}

# =============================================================================
# --verify: is the existing zip still the working tree? (builds nothing)
# =============================================================================
if [ "$MODE" = "verify" ]; then
    echo "Verifying aerodrome v${VERSION} against the working tree..."
    [ -f "$RELEASE_ZIP" ] || fail "No $RELEASE_ZIP — run bash scripts/package-release.sh first"
    [ -f "$RELEASE_SHA" ] || fail "No $RELEASE_SHA — rm the zip and re-package"
    ( cd "$PACKAGES_DIR" && sha256sum -c "$(basename "$RELEASE_SHA")" >/dev/null 2>&1 ) \
        || fail "$(basename "$RELEASE_ZIP") does not match its .sha256"
    ok "sha256 verifies"
    refuse_untracked "zip not re-verified"
    TREE="$(snapshot_tree)" \
        || fail "cannot snapshot the working tree (git add -u on a temporary index failed)"
    if python3 -I "$SCRIPT_DIR/check_release_zip.py" "$RELEASE_ZIP" "$VERSION" "$TREE" \
            --repo "$PROJECT_DIR" --label "working tree"; then
        ok "zip still == working tree — safe to stage"
        exit 0
    fi
    echo -e "  ${RED}✗${RESET} the tree changed since packaging (above)."
    echo "    Not applied on the box yet → rm the zip + .sha256 and re-package."
    echo "    Already applied → make the tree match the zip (restore hint above) or"
    echo "    use up a patch version. Never repackage an applied version."
    exit 1
fi

echo "Packaging aerodrome v${VERSION}..."

# --- Refuse to clobber existing artifacts ---
if [ -e "$RELEASE_DIR" ] || [ -e "$RELEASE_ZIP" ] || [ -e "$RELEASE_SHA" ]; then
    echo -e "  ${YELLOW}⚠${RESET}  Release artifacts already exist:"
    [ -e "$RELEASE_DIR" ] && echo "    $RELEASE_DIR"
    [ -e "$RELEASE_ZIP" ] && echo "    $RELEASE_ZIP"
    [ -e "$RELEASE_SHA" ] && echo "    $RELEASE_SHA"
    echo "  Remove them first if you really want to repackage. Refusing to"
    echo "  silently overwrite an existing release that may already be uploaded."
    echo "  (If the box already applied v${VERSION}, don't repackage it at all —"
    echo "  check it with: bash scripts/package-release.sh --verify)"
    exit 1
fi

# From here on, a failure removes whatever this run created. Set only AFTER
# the clobber guard, so it can never delete artifacts that existed before.
COMPLETED=0
cleanup() {
    rm -rf "$RELEASE_DIR"
    [ "$COMPLETED" = 1 ] || rm -f "$RELEASE_ZIP" "$RELEASE_SHA"
}
trap cleanup EXIT

# --- Pre-flight: what the gates see == what the zip will hold ---
refuse_untracked "nothing packaged"

# --- Pre-flight: every required gate, no skips ---
# This is the place that enforces "no skipped gate in a published zip": the
# CHANGELOG was hand-expanded after the bump, so it is scanned here.
echo "Pre-flight: bump-version.sh check (every required gate, no skips)..."
bash "$PROJECT_DIR/bump-version.sh" check 2>&1 | sed 's/^/  /' \
    || fail "gates failed — nothing packaged"
TOP="$(grep -m1 -oE '^## \[[0-9]+\.[0-9]+\.[0-9]+\]' "$PROJECT_DIR/CHANGELOG.md" || true)"
[ "$TOP" = "## [$VERSION]" ] \
    || fail "CHANGELOG.md's top section is '${TOP:-(none)}', not '## [$VERSION]' — did the bump halt? Re-run the same bump-version.sh set command; nothing packaged"
ok "CHANGELOG.md's top section is [$VERSION]"

# --- Snapshot what is about to be staged ---
SNAP_TREE="$(snapshot_tree)" \
    || fail "cannot snapshot the working tree (git add -u on a temporary index failed)"

# --- No maintainer-only / local path ships (the ONE list; see check_release_zip.py) ---
# Checked HERE, before anything is staged, because this is where the fix is
# still free; publish-release.sh re-checks the commit, after the box applied it.
SNAP_LIST="$(git -C "$PROJECT_DIR" ls-tree -r --name-only "$SNAP_TREE")" \
    || fail "cannot list the snapshot tree"
DRC=0
DENIED="$(printf '%s\n' "$SNAP_LIST" | python3 -I "$SCRIPT_DIR/check_release_zip.py" --deny-paths)" || DRC=$?
case "$DRC" in
    0) ok "No maintainer-only/local paths among the tracked files" ;;
    1) fail "maintainer-only/local paths are tracked — they would ship:
$(printf '%s\n' "$DENIED" | sed 's/^/      /')
    git rm --cached them (nothing is applied yet, so this is free); for a false
    positive, correct MAINTAINER_RE in scripts/check_release_zip.py. Nothing packaged" ;;
    *) fail "the maintainer-path check could not run (exit $DRC) — nothing packaged" ;;
esac

# --- Stage the release tree in the canonical wrapper-folder layout ---
# 2026-08-01: staged from `git ls-files` (tracked files ONLY), not `cp -r` of the
# working tree. cp -r made every untracked local file a release passenger unless a
# strip rule below named it — which is how audits/ shipped in the published
# v3.4.108 zip and a runtime logs/tracker.log shipped in v3.4.109. Tracked-only
# staging ends the class at the source; the strip rules below become
# belt-and-suspenders and the gate after them is the proof.
# Every per-file failure is fatal (`exit` in the loop's pipeline subshell
# reaches `|| fail` through pipefail — errexit is off inside `( … ) || …`, so
# without it only the LAST cp's status would count). A tracked file deleted
# in the working tree without `git rm` is refused: an accidental deletion and
# an intended removal look the same, and `git rm` costs nothing.
mkdir -p "$RELEASE_DIR"
( cd "$PROJECT_DIR" && git ls-files -z | \
  while IFS= read -r -d '' f; do
      if [ ! -e "$f" ] && [ ! -L "$f" ]; then
          echo "    $f is tracked but deleted in the working tree — git rm it (or restore it)" >&2
          exit 1
      fi
      mkdir -p "$RELEASE_DIR/$(dirname "$f")" || exit 1
      cp -p "$f" "$RELEASE_DIR/$f" || exit 1
  done ) || fail "staging the tracked files failed (above) — nothing packaged"

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
# v3.4.144: a TRACKED file removed here no longer ships silently — the zip
# self-check below fails, naming it (commit it out, or stop stripping it).
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
ok "Tracked-files-only gate: every staged file is git-tracked"

# --- Zip ---
# Write the finished zip straight into the packages dir; archive the staging
# tree by its relative name from PARENT_DIR so the zip's internal paths stay
# the clean single-wrapper layout.
mkdir -p "$PACKAGES_DIR"
( cd "$PARENT_DIR" && zip -rq "$RELEASE_ZIP" "$RELEASE_DIR_NAME" )

# --- Self-check: the zip IS the snapshot of the tree it was built from ---
# Same files, same bytes, same exec bits, single wrapper, no caches, VERSION
# inside == VERSION. Catches a tracked file a strip rule removed, an edit that
# landed mid-package, and anything the staging copy got wrong. Runs BEFORE the
# .sha256 exists; on failure the EXIT trap removes the zip.
# (--no-hint: this zip is deleted on failure, so a restore-from-zip hint would
# point at nothing — the fix is to the tree, then a re-package.)
python3 -I "$SCRIPT_DIR/check_release_zip.py" "$RELEASE_ZIP" "$VERSION" "$SNAP_TREE" \
        --repo "$PROJECT_DIR" --label "working tree" --no-hint \
    || fail "zip is not the working tree's tracked files (above) — fix the tree and re-package; nothing packaged"

# --- SHA256 ---
# Generated in path-relative (bare-name) form, sitting beside the zip in the
# packages dir, so `sha256sum -c <name>.sha256` works in that directory
# without path massaging.
( cd "$PACKAGES_DIR" && sha256sum "${RELEASE_DIR_NAME}.zip" > "${RELEASE_DIR_NAME}.zip.sha256" )
COMPLETED=1

# --- Clean up staging directory ---
rm -rf "$RELEASE_DIR"

# --- Report ---
ZIP_SIZE=$(du -h "$RELEASE_ZIP" | cut -f1)
SHA_HASH=$(awk '{print $1}' "$RELEASE_SHA")
ok "${RELEASE_DIR_NAME}.zip (${ZIP_SIZE})"
ok "${RELEASE_DIR_NAME}.zip.sha256 (sha256: ${SHA_HASH:0:16}...)"
echo ""
echo "Both artifacts ready for GitHub Release upload at:"
echo "  $RELEASE_ZIP"
echo "  $RELEASE_SHA"
echo ""
echo "Next: bash scripts/package-release.sh --verify, then stage."
echo "After the box applies v${VERSION}, don't repackage it."
echo ""
