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
#   notes  = the CHANGELOG section for X.Y.Z (the ONLY free-text field)
#
# Usage:
#   scripts/publish-release.sh X.Y.Z --target <commit>   # publish
#   scripts/publish-release.sh X.Y.Z --verify-only       # re-verify existing
#
# --target is REQUIRED for publishing and should be the commit the zip was
# built from (NOT necessarily HEAD — tooling commits often land between the
# package step and the publish step).
#
# After publishing (and in --verify-only) it runs the full verification loop:
# field exactness, both assets, a real download + sha256 check, a zip-content
# scan for maintainer-only files, and releases/latest resolution.
set -euo pipefail

REPO="preston-peterson/aerodrome"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PACKAGES_DIR="$(cd "$PROJECT_DIR/.." && pwd)/aerodrome-packages"

GREEN='\033[32m\033[1m'; RED='\033[31m\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; exit 1; }

VER="${1:-}"; shift || true
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "First arg must be X.Y.Z (got '${VER}')"
TAG="v${VER}"
ZIP="${PACKAGES_DIR}/aerodrome-${TAG}.zip"
SHA="${PACKAGES_DIR}/aerodrome-${TAG}.zip.sha256"

TARGET=""; VERIFY_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target)      TARGET="${2:-}"; shift 2 ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        *) fail "Unknown argument: $1" ;;
    esac
done

if [ "$VERIFY_ONLY" -eq 0 ]; then
    [ -n "$TARGET" ] || fail "--target <commit> is required (the commit the zip was built from)"
    FULL_TARGET="$(cd "$PROJECT_DIR" && git rev-parse --verify "${TARGET}^{commit}")" \
        || fail "--target '$TARGET' is not a commit"
    [ -f "$ZIP" ] || fail "Missing $ZIP (run scripts/package-release.sh first)"
    [ -f "$SHA" ] || fail "Missing $SHA"
    ( cd "$PACKAGES_DIR" && sha256sum -c "$(basename "$SHA")" >/dev/null ) \
        || fail "Local zip does not match its .sha256"
    ok "Local artifacts present, sha256 verifies"

    if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
        fail "Release $TAG already exists — use --verify-only, or delete it deliberately first"
    fi

    # Notes = the CHANGELOG section for this version, verbatim.
    NOTES_FILE="$(mktemp)"
    awk -v ver="$VER" '
        $0 ~ ("^## \\[" ver "\\]") {grab=1; next}
        grab && /^## \[/ {exit}
        grab {print}' "$PROJECT_DIR/CHANGELOG.md" > "$NOTES_FILE"
    [ -s "$NOTES_FILE" ] || fail "No CHANGELOG section found for [$VER]"
    ok "Notes extracted from CHANGELOG [$VER] ($(wc -l < "$NOTES_FILE") lines)"

    gh release create "$TAG" -R "$REPO" \
        --target "$FULL_TARGET" \
        --title "$TAG" \
        --notes-file "$NOTES_FILE" \
        "$ZIP" "$SHA"
    rm -f "$NOTES_FILE"
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

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
( cd "$TMPD" && gh release download "$TAG" -R "$REPO" -p "aerodrome-${TAG}*" \
    && sha256sum -c "aerodrome-${TAG}.zip.sha256" >/dev/null ) \
    || fail "Downloaded assets failed sha256 verification"
ok "Downloaded from GitHub; sha256 verifies"

# (.pii-allowlist is NOT in this pattern: it's a tracked public repo file —
# two path globs, no PII — shipped like .gitignore since v3.4.111.)
LEAKS="$(unzip -l "${TMPD}/aerodrome-${TAG}.zip" | \
    grep -icE 'audits/|graphify|HANDOFF|CLAUDE\.md|AGENT_GUARD|tech-debt-audit|\.claude/|logs/|config\.yaml$|\.db$' || true)"
[ "$LEAKS" = "0" ] || fail "Zip contains maintainer-only/local files ($LEAKS matches) — pull the release NOW"
ok "Zip content scan clean (no maintainer-only files)"

LATEST="$(gh api "repos/${REPO}/releases/latest" --jq .tag_name)"
[ "$LATEST" = "$TAG" ] && ok "releases/latest resolves to ${TAG} (updater will serve it)" \
    || echo "  · note: releases/latest is ${LATEST}, not ${TAG} (fine if verifying an old release)"

echo -e "${GREEN}Done.${RESET} https://github.com/${REPO}/releases/tag/${TAG}"
