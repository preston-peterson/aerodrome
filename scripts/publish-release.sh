#!/usr/bin/env bash
# =============================================================================
# publish-release.sh — publish a GitHub Release the ONE correct way (2026-08-01)
# =============================================================================
# The release surface is machine-consumed: the in-app updater parses `tag_name`
# and constructs asset URLs as aerodrome-{tag}.zip. Every field is load-bearing.
# This script derives all of them from the version so they cannot be improvised:
#
#   tag   == title == vX.Y.Z          (a malformed tag — v.3.4.53 — once
#                                      silently stranded every updater on
#                                      "up to date"; a decorated title was
#                                      reverted by the maintainer 2026-08-01)
#   assets = aerodrome-vX.Y.Z.zip + .zip.sha256, from ../aerodrome-packages/
#   notes  = the CHANGELOG section for X.Y.Z AS COMMITTED IN --target (the
#            ONLY free-text field)
#
# Usage:
#   scripts/publish-release.sh X.Y.Z --target <commit> --check-only  # pre-push proof
#   scripts/publish-release.sh X.Y.Z --target <commit>               # publish
#   scripts/publish-release.sh X.Y.Z --verify-only                   # re-verify existing
#   scripts/publish-release.sh --help
#
# X.Y.Z is canonical: digits only, no 'v', no leading zeros. Never create or
# push the tag yourself — `gh release create --target` makes it, and a tag
# that already exists elsewhere is refused (GitHub would IGNORE --target).
#
# --target is REQUIRED for publishing and for --check-only. It is the commit
# whose `git archive` equals the zip — normally the release commit.
#
# Pre-flight (publish and --check-only), all local, before any network call:
#   - the local zip verifies against its .sha256
#   - zip == `git archive <target>`: same files, bytes and exec bits, single
#     aerodrome-vX.Y.Z/ wrapper, VERSION inside (scripts/check_release_zip.py)
#   - no maintainer-only / local path is committed in <target>'s tree (the
#     ONE denylist, check_release_zip.py --deny-paths, which package-release.sh
#     already ran before zipping; since zip == archive was just proven, this is
#     a scan of the zip — before anything is pushed)
#   - the notes extract from CHANGELOG.md at <target>
#   - a local tag vX.Y.Z, if one exists, points at <target>
# --check-only stops there and publishes nothing: run it after the release
# commit and before `git push`. Publishing then also refuses a vX.Y.Z tag that
# already exists on GitHub at another commit.
#
# If the pre-flight says the zip is not `git archive <target>`:
#   the box has NOT applied vX.Y.Z → rm the zip + .sha256, re-package, re-stage;
#   the box HAS applied it (the usual case by now) → the zip is frozen: make the
#   commit match the zip (revert the listed edits, or
#   `unzip -p ZIP 'aerodrome-vX.Y.Z/<path>' > <path>`, then `chmod +x <path>`
#   if the zip has it executable — the checker prints the exact lines), amend
#   before the push, re-run --check-only — or use up a patch version for a
#   change that must ship. Never repackage an applied version.
#
# After publishing (and in --verify-only) it runs the full verification loop:
# field exactness and both assets; the tag resolves to <target> (publish mode);
# a real download + sha256 check; the downloaded .sha256 is the local one
# (publish mode — the bytes the box applied); the downloaded zip == `git
# archive` of the tag (releases >= 3.4.60, the verified zip==tag history;
# older ones print a note); a zip-content scan for maintainer-only files; and
# releases/latest resolution.
#
# --verify-only needs the tag's commit in the local repo for the content
# check (`git fetch origin --tags` first); it never fetches by itself. Releases
# before v3.4.109 carry docs/tech-debt-audit.md (a stale, PII-clean report,
# tracked until then): the leak scan prints a note for it, not a failure.
# =============================================================================
set -euo pipefail

REPO="preston-peterson/aerodrome"
SCRIPT_FILE="$(readlink -f "$0" 2>/dev/null || echo "$0")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_FILE")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PACKAGES_DIR="$(cd "$PROJECT_DIR/.." && pwd)/aerodrome-packages"

GREEN='\033[32m\033[1m'; RED='\033[31m\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; exit 1; }

case "${1:-}" in
    -h|--help)
        # The header comment between the title rule and the closing rule.
        awk 'NR > 4 && /^# =====/ {exit}
             NR > 4 {sub(/^# ?/, ""); print}' "$SCRIPT_FILE"
        exit 0
        ;;
esac

# Maintainer-only / local paths: ONE list, in scripts/check_release_zip.py
# (MAINTAINER_RE). Prints the hits; exit 1 when there are any.
deny_paths() { python3 -I "$SCRIPT_DIR/check_release_zip.py" --deny-paths "$@"; }

VER="${1:-}"; shift || true
[[ "$VER" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || fail "First arg must be a canonical X.Y.Z — digits only, no 'v', no leading zeros (got '${VER}'; --help for usage)"
TAG="v${VER}"
ZIP="${PACKAGES_DIR}/aerodrome-${TAG}.zip"
SHA="${PACKAGES_DIR}/aerodrome-${TAG}.zip.sha256"

TARGET=""; VERIFY_ONLY=0; CHECK_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target)      [ $# -ge 2 ] || fail "--target needs a commit"
                       TARGET="$2"; shift 2 ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        --check-only)  CHECK_ONLY=1; shift ;;
        *) fail "Unknown argument: $1" ;;
    esac
done
if [ "$CHECK_ONLY" -eq 1 ] && [ "$VERIFY_ONLY" -eq 1 ]; then
    fail "--check-only and --verify-only don't combine (--check-only is the pre-push proof; --verify-only re-checks a published release)"
fi

# One temp dir for the whole run, removed on every exit path.
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT

FULL_TARGET=""
if [ "$VERIFY_ONLY" -eq 0 ]; then
    [ -n "$TARGET" ] || fail "--target <commit> is required (the commit whose git archive equals the zip — normally the release commit)"
    FULL_TARGET="$(git -C "$PROJECT_DIR" rev-parse --verify --quiet "${TARGET}^{commit}")" \
        || fail "--target '$TARGET' is not a commit"
    [ -f "$ZIP" ] || fail "Missing $ZIP (run scripts/package-release.sh first)"
    [ -f "$SHA" ] || fail "Missing $SHA"
    ( cd "$PACKAGES_DIR" && sha256sum -c "$(basename "$SHA")" >/dev/null 2>&1 ) \
        || fail "Local zip does not match its .sha256"
    ok "Local artifacts present, sha256 verifies"

    # 1. The zip IS `git archive <target>`.
    python3 -I "$SCRIPT_DIR/check_release_zip.py" "$ZIP" "$VER" "$FULL_TARGET" \
            --repo "$PROJECT_DIR" --label "commit ${FULL_TARGET:0:7}" \
        || fail "Zip is not git archive ${FULL_TARGET:0:7} (above). If the box has NOT applied v$VER: rm the zip + .sha256, re-package, re-stage. If it HAS (the usual case here): make the commit match the zip — restore or revert the listed files (the lines above say how), amend, re-run --check-only — or use up a patch version for a change that must ship. Never repackage an applied version."

    # 2. No maintainer-only / local file is committed (fail closed). Step 1
    #    proved zip == this tree, so a hit is IN the zip — normally caught by
    #    package-release.sh before zipping; reaching it here means it slipped by.
    TREE_LIST="$(git -C "$PROJECT_DIR" ls-tree -r --name-only "$FULL_TARGET")" \
        || fail "cannot list ${FULL_TARGET:0:7}"
    DRC=0
    HITS="$(printf '%s\n' "$TREE_LIST" | deny_paths)" || DRC=$?
    case "$DRC" in
        0) ;;
        1) fail "maintainer-only/local paths are committed in ${FULL_TARGET:0:7} — they are in the zip:
$(printf '%s\n' "$HITS" | sed 's/^/      /')
    Box has NOT applied v$VER: git rm --cached them, amend, rm the zip + .sha256, re-package, re-stage.
    Box HAS applied it: v$VER stays box-only — use up a patch version whose commit removes them.
    A false positive: correct MAINTAINER_RE (scripts/check_release_zip.py) in that next release." ;;
        *) fail "the maintainer-path check could not run (exit $DRC)" ;;
    esac
    ok "No maintainer-only/local files in ${FULL_TARGET:0:7}"

    # 3. Notes = the CHANGELOG section for this version, verbatim, AS COMMITTED
    #    in the target. Blob to a file first, then awk on the FILE: an awk that
    #    exits early on the right of a pipe dies with SIGPIPE under pipefail.
    CL="$TMPD/CHANGELOG.at-target.md"; NOTES_FILE="$TMPD/notes.md"
    git -C "$PROJECT_DIR" show "${FULL_TARGET}:CHANGELOG.md" > "$CL" \
        || fail "cannot read CHANGELOG.md at ${FULL_TARGET:0:7}"
    awk -v ver="$VER" '$0 ~ ("^## \\[" ver "\\]") {grab=1; next}
                       grab && /^## \[/ {exit}
                       grab {print}' "$CL" > "$NOTES_FILE"
    [ -s "$NOTES_FILE" ] || fail "No CHANGELOG section for [$VER] at ${FULL_TARGET:0:7}"
    ok "Notes extracted from CHANGELOG [$VER] at ${FULL_TARGET:0:7} ($(wc -l < "$NOTES_FILE") lines)"

    # 4. A tag that already exists makes `gh release create` IGNORE --target
    #    (GitHub: target_commitish is "unused if the Git tag already exists"),
    #    so the release would go out on the wrong commit. Local tag first.
    LOCAL_TAG="$(git -C "$PROJECT_DIR" rev-parse -q --verify "refs/tags/${TAG}^{commit}" || true)"
    [ -z "$LOCAL_TAG" ] || [ "$LOCAL_TAG" = "$FULL_TARGET" ] \
        || fail "local tag $TAG points at ${LOCAL_TAG:0:7}, not --target ${FULL_TARGET:0:7} — gh would ignore --target. Don't create tags by hand; delete or move this one deliberately (and on GitHub, if it was pushed)."
    ok "No local tag $TAG elsewhere"

    if [ "$CHECK_ONLY" -eq 1 ]; then
        ok "--check-only: zip == git archive ${FULL_TARGET:0:7}, no maintainer files, notes OK — nothing published"
        exit 0
    fi

    if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
        fail "Release $TAG already exists — use --verify-only, or delete it deliberately first"
    fi

    # ...and the tag on GitHub (it can exist there without a release, or without
    # a local copy). "No commit found" (HTTP 422/404) = absent; anything else is
    # an error — fail closed rather than publish on a guess.
    RRC=0
    REMOTE_TAG="$(gh api "repos/${REPO}/commits/${TAG}" --jq .sha 2> "$TMPD/tagcheck.err")" || RRC=$?
    if [ "$RRC" -ne 0 ]; then
        grep -qE 'HTTP 4(04|22)' "$TMPD/tagcheck.err" \
            || fail "cannot check whether tag $TAG already exists on GitHub: $(tr '\n' ' ' < "$TMPD/tagcheck.err")"
        REMOTE_TAG=""
    fi
    [ -z "$REMOTE_TAG" ] || [ "$REMOTE_TAG" = "$FULL_TARGET" ] \
        || fail "tag $TAG already exists on GitHub at ${REMOTE_TAG:0:7}, not --target ${FULL_TARGET:0:7} — gh would ignore --target. Delete that tag deliberately (with the maintainer's OK) first."
    ok "No tag $TAG on GitHub elsewhere"

    gh release create "$TAG" -R "$REPO" \
        --target "$FULL_TARGET" \
        --title "$TAG" \
        --notes-file "$NOTES_FILE" \
        "$ZIP" "$SHA"
    ok "Release created"
fi

# --- Verification loop (always runs) ---
echo "Verifying ${TAG} on GitHub..."
JSON="$(gh release view "$TAG" -R "$REPO" --json tagName,name,isDraft,isPrerelease,assets)"
RELEASE_JSON="$JSON" python3 - "$TAG" <<'PYEOF' || exit 1
import json, os, sys
tag = sys.argv[1]
d = json.loads(os.environ["RELEASE_JSON"])
def die(m): print(f"  ✗ {m}"); sys.exit(1)
if d["tagName"] != tag:                 die(f"tag is {d['tagName']}, expected {tag}")
if d["name"] != tag:                    die(f"TITLE is '{d['name']}' — must be exactly '{tag}'")
if d["isDraft"] or d["isPrerelease"]:   die("release is draft or prerelease")
names = sorted(a["name"] for a in d["assets"])
want = sorted([f"aerodrome-{tag}.zip", f"aerodrome-{tag}.zip.sha256"])
if names != want:                       die(f"assets are {names}, expected exactly {want}")
print(f"  ✓ tag == title == {tag}; published; exactly 2 correctly-named assets")
PYEOF

TAG_SHA="$(gh api "repos/${REPO}/commits/${TAG}" --jq .sha)" \
    || fail "cannot resolve ${TAG}'s commit on GitHub"
if [ "$VERIFY_ONLY" -eq 0 ] && [ "$TAG_SHA" != "$FULL_TARGET" ]; then
    fail "tag ${TAG} resolves to ${TAG_SHA:0:7}, not --target ${FULL_TARGET:0:7}"
fi
ok "tag ${TAG} resolves to ${TAG_SHA:0:7}"

mkdir "$TMPD/dl"
( cd "$TMPD/dl" && gh release download "$TAG" -R "$REPO" -p "aerodrome-${TAG}*" \
    && sha256sum -c "aerodrome-${TAG}.zip.sha256" >/dev/null 2>&1 ) \
    || fail "Downloaded assets failed sha256 verification"
ok "Downloaded from GitHub; sha256 verifies"

if [ "$VERIFY_ONLY" -eq 0 ]; then
    cmp -s "$TMPD/dl/aerodrome-${TAG}.zip.sha256" "$SHA" \
        || fail "GitHub's .sha256 differs from the local one — the published zip is not the one the box applied"
    ok "Published .sha256 == local .sha256 (the bytes the box applied)"
fi

if [ "$(printf '%s\n%s\n' 3.4.60 "$VER" | sort -V | tail -n1)" = "$VER" ]; then   # VER >= 3.4.60
    git -C "$PROJECT_DIR" cat-file -e "${TAG_SHA}^{commit}" 2>/dev/null \
        || fail "tag commit ${TAG_SHA:0:7} not in the local repo — git fetch origin --tags, then re-run"
    python3 -I "$SCRIPT_DIR/check_release_zip.py" "$TMPD/dl/aerodrome-${TAG}.zip" "$VER" "$TAG_SHA" \
            --repo "$PROJECT_DIR" --label "tag ${TAG}" \
        || fail "The published zip is not git archive ${TAG} (above) — pull the release deliberately, fix, re-publish"
else
    echo "  · content check skipped: v$VER predates the verified zip==tag history (v3.4.60+)"
fi

LISTING="$(unzip -Z1 "$TMPD/dl/aerodrome-${TAG}.zip")" || fail "cannot list the downloaded zip"
LRC=0
LEAKS="$(printf '%s\n' "$LISTING" | deny_paths "$VER")" || LRC=$?   # notes (known history) go to stderr
case "$LRC" in
    0) ok "Zip content scan clean (no maintainer-only files)" ;;
    1) fail "Zip contains maintainer-only/local files — pull the release NOW:
$(printf '%s\n' "$LEAKS" | sed 's/^/      /')" ;;
    *) fail "the zip content scan could not run (exit $LRC)" ;;
esac

LATEST="$(gh api "repos/${REPO}/releases/latest" --jq .tag_name)"
[ "$LATEST" = "$TAG" ] && ok "releases/latest resolves to ${TAG} (updater will serve it)" \
    || echo "  · note: releases/latest is ${LATEST}, not ${TAG} (fine if verifying an old release)"

echo -e "${GREEN}Done.${RESET} https://github.com/${REPO}/releases/tag/${TAG}"
