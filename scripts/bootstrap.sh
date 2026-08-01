#!/usr/bin/env bash
# Version: 3.0.14
# =============================================================================
# Aerodrome — Curl Install Bootstrap
# =============================================================================
#
# Headline command:
#   bash <(curl -fsSL https://install.aerodromeadsb.com)
#
# Local testing (offline install from a release zip already on disk):
#   bash scripts/bootstrap.sh --from-zip ~/Downloads/aerodrome-v3.0.12.zip --prefix /tmp/ad
#
# Flags:
#   --prefix <path>        Install directory (default: /opt/aerodrome)
#   --version <vX.Y.Z>     Pin to a specific release (default: latest)
#   --from-zip <path>      Skip GitHub fetch; install from a local zip
#   --receiver-ip <ip>     ADS-B receiver IP (skips prompt)
#   --receiver-port <n>    ADS-B receiver port (skips prompt; default 8080)
#   --lat <n>              Receiver latitude (skips prompt)
#   --lon <n>              Receiver longitude (skips prompt)
#   --distance-unit <u>    mi / nmi / km (skips prompt; default mi)
#   --timezone <tz>        IANA tz name (skips prompt; default: system)
#   --force                Bypass OS-compat warning on unrecognized distros
#   -y, --yes              Accept all defaults non-interactively
#   -h, --help             Show this help and exit
#
# What this script does:
#   1. Detects OS (Debian-family check, three-tier behavior)
#   2. Verifies/installs prereqs (curl, unzip, sha256sum, python3, python3-venv)
#   3. Refuses if Aerodrome is already installed (points at in-app updater)
#   4. Resolves install version via GitHub Releases API (or --version, or --from-zip)
#   5. Downloads release zip + .sha256, verifies checksum
#   6. Prompts for the bare-minimum config (or reads flags)
#   7. Extracts to <prefix>, patches config.yaml with the prompted values
#   8. Hands off to the bundled install.sh for venv + systemd + sudoers work
#   9. Prints the web UI URL
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
CYAN='\033[36m\033[1m'
YELLOW='\033[33m\033[1m'
DIM='\033[2m'
RESET='\033[0m'

REPO="preston-peterson/aerodrome"
RAW_BASE="https://raw.githubusercontent.com/${REPO}"
API_BASE="https://api.github.com/repos/${REPO}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
PREFIX="/opt/aerodrome"
VERSION="latest"
FROM_ZIP=""
RECV_IP=""
RECV_PORT="8080"
LAT=""
LON=""
DIST_UNIT="mi"
TIMEZONE=""
FORCE=false
ASSUME_YES=false
DEMO_MODE=false

# Track whether each value was supplied as a flag (skip the prompt) vs. left
# at default (still prompt, with the default offered).
RECV_IP_SET=false
RECV_PORT_SET=false
LAT_SET=false
LON_SET=false
DIST_UNIT_SET=false
TIMEZONE_SET=false
DEMO_MODE_SET=false

show_help() {
    # v3.0.3: literal heredoc instead of `sed "$0"`. The previous
    # implementation worked when invoked as a saved file (./bootstrap.sh
    # --help) but silently produced no output when invoked via process
    # substitution (bash <(curl -fsSL https://install.aerodromeadsb.com)
    # --help) — in that path, "$0" is /dev/fd/63, a file descriptor bash
    # has already consumed reading the script body, so the sed read from
    # an empty FD and exit 0 returned silently. Heredoc is invocation-
    # path-agnostic. If the canonical install URL or flags change, edit
    # both this block AND the top-of-file comment block above; they are
    # intentional duplicates so the user-facing help and the source-
    # reading reader see the same thing.
    cat <<'EOF'
Aerodrome — Curl Install Bootstrap

Headline command:
  bash <(curl -fsSL https://install.aerodromeadsb.com)

Local testing:
  bash scripts/bootstrap.sh --from-zip ~/Downloads/aerodrome-vX.Y.Z.zip --prefix /tmp/ad

Flags:
  --prefix <path>        Install directory (default: /opt/aerodrome).
                         Pre-v3.3.0 default was ~/aerodrome; override
                         with --prefix ~/aerodrome to restore that layout.
  --version <vX.Y.Z>     Pin to a specific release (default: latest)
  --from-zip <path>      Skip GitHub fetch; install from a local zip
  --demo                 Install in demo mode with simulated aircraft data
                         (skips receiver prompts; uses synthetic feeder)
  --receiver-ip <ip>     ADS-B receiver IP (skips prompt)
  --receiver-port <n>    ADS-B receiver port (skips prompt; default 8080)
  --lat <n>              Receiver latitude (skips prompt)
  --lon <n>              Receiver longitude (skips prompt)
  --distance-unit <u>    mi / nmi / km (skips prompt; default mi)
  --timezone <tz>        IANA tz name (skips prompt; default: system)
  --force                Bypass OS-compat warning on unrecognized distros
  -y, --yes              Accept all defaults non-interactively
  -h, --help             Show this help and exit

What this script does:
  1. Detects OS (Debian-family check, three-tier behavior)
  2. Verifies/installs prereqs (curl, unzip, sha256sum, python3, python3-venv)
  3. Refuses if Aerodrome is already installed (points at in-app updater)
  4. Resolves install version via GitHub Releases API (or --version, or --from-zip)
  5. Downloads release zip + .sha256, verifies checksum
  6. Prompts for the bare-minimum config (or reads flags)
  7. Extracts to <prefix>, patches config.yaml with the prompted values
  8. Hands off to the bundled install.sh for venv + systemd + sudoers work
  9. Prints the web UI URL
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)         PREFIX="$2"; shift 2 ;;
        --version)        VERSION="$2"; shift 2 ;;
        --from-zip)       FROM_ZIP="$2"; shift 2 ;;
        --demo)
            DEMO_MODE=true
            DEMO_MODE_SET=true
            # In demo mode, the receiver IP/port are the local synthetic
            # feeder. Set them now so the prompt-skip flags below are
            # already true; the user only gets asked for lat/lon.
            RECV_IP="127.0.0.1"
            RECV_IP_SET=true
            RECV_PORT="8080"
            RECV_PORT_SET=true
            shift ;;
        --receiver-ip)    RECV_IP="$2"; RECV_IP_SET=true; shift 2 ;;
        --receiver-port)  RECV_PORT="$2"; RECV_PORT_SET=true; shift 2 ;;
        --lat)            LAT="$2"; LAT_SET=true; shift 2 ;;
        --lon)            LON="$2"; LON_SET=true; shift 2 ;;
        --distance-unit)  DIST_UNIT="$2"; DIST_UNIT_SET=true; shift 2 ;;
        --timezone)       TIMEZONE="$2"; TIMEZONE_SET=true; shift 2 ;;
        --force)          FORCE=true; shift ;;
        -y|--yes)         ASSUME_YES=true; shift ;;
        -h|--help)        show_help; exit 0 ;;
        *)
            echo -e "${RED}Unknown option: $1${RESET}" >&2
            echo "Run with --help for usage" >&2
            exit 1
            ;;
    esac
done

# Refuse to run as root — install.sh will refuse anyway, fail fast here.
if [ "$(id -u)" = "0" ]; then
    echo -e "${RED}Don't run this script as root.${RESET}" >&2
    echo "It will use sudo only where strictly needed." >&2
    exit 1
fi

# Resolve PREFIX to absolute path; expand ~ if literal
PREFIX="${PREFIX/#\~/$HOME}"
case "$PREFIX" in
    /*) ;;
    *)  PREFIX="$(pwd)/$PREFIX" ;;
esac

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log_step() { echo -e "${CYAN}[$1]${RESET} $2"; }
log_ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
log_warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
log_err()  { echo -e "  ${RED}✗${RESET} $1" >&2; }
log_info() { echo -e "  ${DIM}·${RESET} $1"; }

# Read with default. If ASSUME_YES, take the default silently.
ask_default() {
    local prompt="$1" default="$2" __resultvar="$3"
    local input=""
    if [ "$ASSUME_YES" = true ]; then
        printf -v "$__resultvar" '%s' "$default"
        return 0
    fi
    if [ -n "$default" ]; then
        read -r -p "  $prompt [$default]: " input
    else
        read -r -p "  $prompt: " input
    fi
    if [ -z "$input" ]; then
        printf -v "$__resultvar" '%s' "$default"
    else
        printf -v "$__resultvar" '%s' "$input"
    fi
}

# Cleanup on exit. If we created a tempdir or partial install, clean up unless
# the script completed fully (COMPLETED=true).
COMPLETED=false
TMPDIR_BOOT=""
INSTALL_CREATED_BY_US=false

cleanup() {
    if [ "$COMPLETED" = false ]; then
        if [ -n "$TMPDIR_BOOT" ] && [ -d "$TMPDIR_BOOT" ]; then
            rm -rf "$TMPDIR_BOOT"
        fi
        if [ "$INSTALL_CREATED_BY_US" = true ] && [ -d "$PREFIX" ]; then
            log_warn "Install incomplete — leaving partial tree at $PREFIX for inspection."
            log_warn "If you want it gone: rm -rf $PREFIX"
        fi
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║      Aerodrome — Install Bootstrap           ║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# ---------------------------------------------------------------------------
# [1/7] OS detection (three-tier)
# ---------------------------------------------------------------------------
log_step "1/7" "Checking platform..."

OS_ID=""
OS_LIKE=""
OS_PRETTY=""
if [ -r /etc/os-release ]; then
    # v3.0.13: source /etc/os-release in a SUBSHELL, not the current
    # shell, so its variable definitions don't leak. /etc/os-release
    # uses shell syntax (NAME=, VERSION=, ID=, ID_LIKE=, VERSION_ID=,
    # PRETTY_NAME=, etc.), and on Ubuntu 24.04 the VERSION key holds
    # "24.04.4 LTS (Noble Numbat)". Dot-sourcing it directly here
    # silently clobbered the bootstrap's own VERSION="latest" arg
    # default (set at the top of the file), which made the step-4
    # version resolver fall into the else branch and try to download
    # aerodrome-v24.04.4 LTS (Noble Numbat).zip from a release tag
    # that, naturally, does not exist. Surfaced by a clean-VM dogfood
    # on Ubuntu 24.04 — exactly what dogfooding is for. Using a
    # subshell + printf '%q' + eval is the canonical pattern: %q
    # shell-quotes each value so values containing spaces, quotes,
    # parens (like "Noble Numbat") survive the round trip; eval
    # re-injects them as plain assignments into the parent shell.
    eval "$(
        . /etc/os-release
        printf 'OS_ID=%q\nOS_LIKE=%q\nOS_PRETTY=%q\n' \
            "${ID:-}" "${ID_LIKE:-}" "${PRETTY_NAME:-${ID:-unknown}}"
    )"
else
    OS_PRETTY="unknown"
fi

ARCH="$(uname -m)"
log_info "Detected: $OS_PRETTY ($ARCH)"

# v3.2.0: multi-distro support. Detect the package manager family and load
# its install commands + translated package names. The four tier-1 families
# proceed silently; anything else falls to tier-2 (best-effort) if systemctl
# is present, or tier-3 (hard refuse) if not.
pkg_detect() {
    local os_id="" os_like=""
    if [ -r /etc/os-release ]; then
        eval "$(
            . /etc/os-release
            printf 'os_id=%q\nos_like=%q\n' "${ID:-}" "${ID_LIKE:-}"
        )"
    fi
    PKG_FAMILY=unknown
    case "$os_id" in
        debian|ubuntu|raspbian|linuxmint|pop|elementary|neon|kali|parrot)
            PKG_FAMILY=debian ;;
        fedora|rhel|centos|rocky|almalinux|amzn|ol)
            PKG_FAMILY=fedora ;;
        arch|manjaro|endeavouros|garuda|artix|cachyos)
            PKG_FAMILY=arch ;;
        opensuse*|sles|sled)
            PKG_FAMILY=opensuse ;;
        *)
            case " $os_like " in
                *" debian "*|*" ubuntu "*) PKG_FAMILY=debian ;;
                *" fedora "*|*" rhel "*|*" centos "*) PKG_FAMILY=fedora ;;
                *" arch "*) PKG_FAMILY=arch ;;
                *" suse "*|*" opensuse "*) PKG_FAMILY=opensuse ;;
            esac ;;
    esac
    case "$PKG_FAMILY" in
        debian)
            PKG_REFRESH_CMD="sudo apt-get update -qq"
            PKG_INSTALL_CMD="sudo apt-get install -y -qq"
            PKG_PYTHON3="python3"; PKG_PIP="python3-pip"
            PKG_VENV="python3-venv"; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        fedora)
            PKG_REFRESH_CMD=""
            PKG_INSTALL_CMD="sudo dnf install -y -q"
            PKG_PYTHON3="python3"; PKG_PIP="python3-pip"
            PKG_VENV=""; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        arch)
            PKG_REFRESH_CMD="sudo pacman -Sy --noconfirm"
            PKG_INSTALL_CMD="sudo pacman -S --needed --noconfirm"
            PKG_PYTHON3="python"; PKG_PIP="python-pip"
            PKG_VENV=""; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        opensuse)
            PKG_REFRESH_CMD="sudo zypper --non-interactive refresh"
            PKG_INSTALL_CMD="sudo zypper --non-interactive install"
            PKG_PYTHON3="python3"; PKG_PIP="python3-pip"
            PKG_VENV=""; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        *)
            PKG_REFRESH_CMD=""; PKG_INSTALL_CMD="" ;;
    esac
}
pkg_install() {
    local pkgs=() p
    for p in "$@"; do [ -n "$p" ] && pkgs+=("$p"); done
    [ "${#pkgs[@]}" -eq 0 ] && return 0
    [ -z "$PKG_INSTALL_CMD" ] && {
        echo "pkg_install: no install command for PKG_FAMILY=$PKG_FAMILY" >&2
        return 1
    }
    $PKG_INSTALL_CMD "${pkgs[@]}"
}
pkg_refresh() {
    [ -z "$PKG_REFRESH_CMD" ] && return 0
    $PKG_REFRESH_CMD
}
pkg_detect

# Tier 1: recognized family with a known package manager — proceed silently.
recognized=false
case "$PKG_FAMILY" in
    debian|fedora|arch|opensuse) recognized=true ;;
esac

if [ "$recognized" = true ]; then
    case "$PKG_FAMILY" in
        debian)   log_ok "Recognized Debian-family system" ;;
        fedora)   log_ok "Recognized Fedora/RHEL-family system" ;;
        arch)     log_ok "Recognized Arch-family system" ;;
        opensuse) log_ok "Recognized openSUSE/SUSE system" ;;
    esac
else
    # Tier 2/3: check capabilities. We require systemctl (no realistic
    # workaround for a service-managed app) plus a recognized package
    # manager. If both are present we proceed in best-effort mode; if
    # either is missing we hard-refuse.
    has_systemctl=false
    has_known_pkgmgr=false
    command -v systemctl >/dev/null 2>&1 && has_systemctl=true
    for cmd in apt-get dnf pacman zypper; do
        command -v "$cmd" >/dev/null 2>&1 && has_known_pkgmgr=true
    done

    if [ "$has_systemctl" = false ] || [ "$has_known_pkgmgr" = false ]; then
        log_err "Aerodrome's installer requires systemd (systemctl) and one of"
        log_err "apt-get, dnf, pacman, or zypper. Your system reports ID=$os_id."
        echo ""
        echo "  See docs/INSTALL.md for manual install steps on other distros."
        echo "  Browse the docs at: https://github.com/${REPO}/blob/main/docs/INSTALL.md"
        exit 1
    fi

    if [ "$FORCE" = true ]; then
        log_warn "Unrecognized distro ($os_id), but a known package manager and"
        log_warn "systemctl are present — continuing per --force"
    else
        log_warn "Aerodrome is tested on Debian/Ubuntu, Fedora/RHEL, Arch, and openSUSE."
        log_warn "Your system reports ID=$os_id, but a known package manager and"
        log_warn "systemctl are available, so the install will probably work."
        if [ "$ASSUME_YES" = true ]; then
            log_warn "Continuing anyway per --yes."
        else
            read -r -p "  Continue? [y/N] " reply
            if [[ ! "$reply" =~ ^[Yy]$ ]]; then
                echo "Aborted."
                exit 0
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# [2/7] Existing-install detection
# ---------------------------------------------------------------------------
log_step "2/7" "Checking for existing Aerodrome install..."

# Most reliable signal: the systemd unit file
if systemctl list-unit-files 2>/dev/null | grep -q '^aerodrome\.service'; then
    # Try to read the install dir from the unit file
    existing_dir=""
    unit_path="/etc/systemd/system/aerodrome.service"
    if [ -r "$unit_path" ]; then
        existing_dir="$(grep -m1 '^WorkingDirectory=' "$unit_path" | cut -d= -f2-)"
    fi
    existing_ver="?"
    if [ -n "$existing_dir" ] && [ -r "$existing_dir/VERSION" ]; then
        existing_ver="$(cat "$existing_dir/VERSION" 2>/dev/null || echo '?')"
    fi

    log_err "Aerodrome is already installed (v${existing_ver}${existing_dir:+ at $existing_dir})."
    echo ""
    echo "  To upgrade: open the web UI and use the Updates page"
    if [ -n "$existing_dir" ]; then
        # Best-effort: print the URL the user is most likely to hit it at
        echo "             (Updates → Local update or GitHub update)"
    fi
    echo "  To start fresh: run uninstall.sh in the existing install directory first"
    exit 1
fi

# Secondary check: install path already exists and contains an Aerodrome tree
if [ -d "$PREFIX" ] && [ -e "$PREFIX/VERSION" ] && [ -e "$PREFIX/main.py" ]; then
    log_err "An Aerodrome tree already exists at $PREFIX (v$(cat "$PREFIX/VERSION" 2>/dev/null || echo '?'))"
    log_err "but no systemd unit is registered."
    echo ""
    echo "  This usually means a partial install or a manual extract."
    echo "  Either run install.sh from inside the existing tree, or remove it"
    echo "  and re-run this bootstrap."
    exit 1
fi

log_ok "No existing install found"

# ---------------------------------------------------------------------------
# [3/7] Prereqs
# ---------------------------------------------------------------------------
log_step "3/7" "Checking prerequisites..."

# v3.2.0: use the family-aware abstraction. PKG_VENV is empty on
# fedora/arch/opensuse (venv is bundled into the python3 package);
# pkg_install skips empty args, so passing it unconditionally is safe.
NEED_PKGS=()
command -v curl       >/dev/null 2>&1 || NEED_PKGS+=("$PKG_CURL")
command -v unzip      >/dev/null 2>&1 || NEED_PKGS+=("$PKG_UNZIP")
# sha256sum is in coreutils on every supported family; coreutils is part
# of the base install on all four, so it's never actually missing. Skip.
command -v python3    >/dev/null 2>&1 || NEED_PKGS+=("$PKG_PYTHON3")

# venv may be a separate package (Debian-family) or built-in (others).
# Test whether the ensurepip module is importable; if not, add the venv
# package. On non-Debian families PKG_VENV is empty, in which case the
# python3 package itself needs (re)installing — but that's already in
# NEED_PKGS above if python3 was missing.
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c 'import ensurepip' 2>/dev/null; then
        [ -n "$PKG_VENV" ] && NEED_PKGS+=("$PKG_VENV")
    fi
else
    [ -n "$PKG_VENV" ] && NEED_PKGS+=("$PKG_VENV")
fi

if [ ${#NEED_PKGS[@]} -gt 0 ]; then
    log_info "Need to install: ${NEED_PKGS[*]}"
    log_info "Using: $PKG_INSTALL_CMD"
    pkg_refresh
    pkg_install "${NEED_PKGS[@]}" >/dev/null
    log_ok "Prerequisites installed"
else
    log_ok "All prerequisites present"
fi

# Python version check (need 3.10+)
# v3.3.1: capture both stdout and stderr so we can show real errors
# when the interpreter is broken (not just "old"). Common case: a
# distro Python install with corrupt bytecode or broken site hooks
# fails every -c invocation, which previously surfaced as a
# misleading "version too old" message pointing at deadsnakes.
_py_check_out=$(python3 -c 'import sys; print("yes" if sys.version_info >= (3,10) else "no")' 2>&1)
_py_check_rc=$?
if [ "$_py_check_rc" -ne 0 ] || [ "$_py_check_out" != "yes" ]; then
    # Two distinct failure modes:
    #   1. python3 -c failed entirely (broken install, stale bytecode,
    #      bad site-packages hook) — stderr probably has a traceback
    #   2. python3 -c succeeded but reported the wrong version
    _py_version_str=$(python3 --version 2>&1 || echo 'none')
    _py_version_works=false
    if [ "$_py_check_rc" -eq 0 ] && [ "$_py_check_out" = "no" ]; then
        # Case 2: interpreter ran fine, version is genuinely too old
        log_err "Python 3.10+ required; found $_py_version_str"
        echo ""
        echo "  On older Ubuntu/Debian releases you may need a newer python3 from"
        echo "  the deadsnakes PPA or a backport. See docs/INSTALL.md."
    else
        # Case 1: interpreter is broken (or python3 doesn't exist at all)
        log_err "Python interpreter is not working correctly"
        echo "  Reports version: $_py_version_str"
        echo ""
        echo "  Output from 'python3 -c ...':"
        echo "$_py_check_out" | sed 's/^/    /'
        echo ""
        echo "  This usually means a broken Python install — try reinstalling"
        echo "  Python via your distro's package manager. For example:"
        case "$PKG_FAMILY" in
            debian)   echo "    sudo apt-get install --reinstall python3" ;;
            fedora)   echo "    sudo dnf reinstall python3" ;;
            arch)     echo "    sudo pacman -S python" ;;
            opensuse) echo "    sudo zypper install --force python3" ;;
            *)        echo "    (use your distro's package manager to reinstall python3)" ;;
        esac
    fi
    exit 1
fi
log_ok "Python $(python3 --version | cut -d' ' -f2)"

# ---------------------------------------------------------------------------
# [4/7] Resolve release (latest from GitHub, or --version pin, or --from-zip)
# ---------------------------------------------------------------------------
log_step "4/7" "Resolving release..."

TMPDIR_BOOT="$(mktemp -d -t aerodrome-bootstrap.XXXXXX)"

if [ -n "$FROM_ZIP" ]; then
    if [ ! -r "$FROM_ZIP" ]; then
        log_err "--from-zip path not readable: $FROM_ZIP"
        exit 1
    fi
    log_ok "Using local zip: $FROM_ZIP"
    cp "$FROM_ZIP" "$TMPDIR_BOOT/release.zip"
    # Try to extract the version from the zip's top-level directory name
    RESOLVED_VER=$(unzip -l "$FROM_ZIP" 2>/dev/null \
        | awk '/aerodrome-v[0-9]+\.[0-9]+\.[0-9]+\/$/ {print $NF; exit}' \
        | sed -E 's|aerodrome-(v[0-9]+\.[0-9]+\.[0-9]+)/|\1|' \
        || true)
    [ -n "$RESOLVED_VER" ] || RESOLVED_VER="(unknown)"
    log_info "Version: $RESOLVED_VER"
    SKIP_CHECKSUM=true
else
    SKIP_CHECKSUM=false
    if [ "$VERSION" = "latest" ]; then
        log_info "Querying GitHub Releases API for latest..."
        api_resp="$TMPDIR_BOOT/releases-latest.json"
        if ! curl -fsSL -o "$api_resp" "$API_BASE/releases/latest"; then
            log_err "Failed to fetch latest release info from GitHub."
            log_err "Check network connectivity, or use --version vX.Y.Z to pin."
            exit 1
        fi
        # Extract tag_name without jq dependency
        RESOLVED_VER=$(grep -m1 '"tag_name"' "$api_resp" \
            | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
        if [ -z "$RESOLVED_VER" ]; then
            log_err "Could not parse tag_name from API response. Possibly rate-limited?"
            exit 1
        fi
    else
        RESOLVED_VER="$VERSION"
        # Normalize: accept "v2.97.4" or "2.97.4"
        case "$RESOLVED_VER" in
            v*) ;;
            *)  RESOLVED_VER="v$RESOLVED_VER" ;;
        esac
    fi
    log_ok "Version: $RESOLVED_VER"

    # Download zip + .sha256
    ZIP_NAME="aerodrome-${RESOLVED_VER}.zip"
    DL_BASE="https://github.com/${REPO}/releases/download/${RESOLVED_VER}"
    log_info "Downloading $ZIP_NAME..."
    if ! curl -fSL --progress-bar -o "$TMPDIR_BOOT/release.zip" "$DL_BASE/$ZIP_NAME"; then
        log_err "Failed to download $ZIP_NAME from $DL_BASE/"
        log_err "Check that the version exists at: https://github.com/${REPO}/releases"
        exit 1
    fi
    log_ok "Downloaded $(du -h "$TMPDIR_BOOT/release.zip" | cut -f1)"

    log_info "Downloading $ZIP_NAME.sha256..."
    if ! curl -fsSL -o "$TMPDIR_BOOT/release.zip.sha256" "$DL_BASE/$ZIP_NAME.sha256"; then
        log_err "Failed to download checksum file. Refusing to proceed without verification."
        exit 1
    fi

    # Verify
    log_info "Verifying SHA256..."
    expected_sha=$(awk '{print $1}' "$TMPDIR_BOOT/release.zip.sha256")
    actual_sha=$(sha256sum "$TMPDIR_BOOT/release.zip" | awk '{print $1}')
    if [ "$expected_sha" != "$actual_sha" ]; then
        log_err "SHA256 MISMATCH"
        log_err "  expected: $expected_sha"
        log_err "  actual:   $actual_sha"
        log_err "Refusing to install. The download is corrupt or has been tampered with."
        exit 1
    fi
    log_ok "SHA256 verified ($(echo "$actual_sha" | cut -c1-12)...)"
fi

# ---------------------------------------------------------------------------
# [5/7] Resolve install directory + extract
# ---------------------------------------------------------------------------
log_step "5/7" "Setting up install directory..."

# v3.3.0: detect whether we're upgrading an existing install at this
# location vs. doing a fresh install. An existing install is identified
# by the presence of VERSION + main.py; if found we proceed in upgrade
# mode and don't touch the directory permissions.
EXISTING_INSTALL=false
if [ -f "$PREFIX/VERSION" ] && [ -f "$PREFIX/main.py" ]; then
    EXISTING_INSTALL=true
    log_info "Existing install detected at $PREFIX — upgrading in place"
elif [ -d "$PREFIX" ] && [ -n "$(ls -A "$PREFIX" 2>/dev/null)" ]; then
    log_warn "Directory $PREFIX already exists and is not empty,"
    log_warn "but doesn't look like an Aerodrome install (no VERSION/main.py)."
    if [ "$ASSUME_YES" = true ]; then
        log_err "Refusing to overwrite (would need interactive confirmation)."
        exit 1
    fi
    read -r -p "  Overwrite? Existing contents will be removed [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
    # Need sudo if outside the user's home
    case "$PREFIX" in
        "$HOME"/*|"$HOME") rm -rf "$PREFIX" ;;
        *)                 sudo rm -rf "$PREFIX" ;;
    esac
fi

# Determine whether PREFIX is inside the user's home (no sudo needed for
# mkdir) or outside (need sudo to create + chown to user afterwards).
NEED_SUDO_FOR_PREFIX=false
case "$PREFIX" in
    "$HOME"/*|"$HOME") ;;
    *) NEED_SUDO_FOR_PREFIX=true ;;
esac

if [ "$EXISTING_INSTALL" = false ]; then
    if [ "$NEED_SUDO_FOR_PREFIX" = true ]; then
        log_info "Creating $PREFIX (requires sudo since it's outside your home)..."
        sudo mkdir -p "$PREFIX"
        # chown so the user can write release files, config.yaml, etc.
        # without sudo for the rest of this script and for in-app updates.
        sudo chown "$USER:$USER" "$PREFIX"
    else
        mkdir -p "$PREFIX"
    fi
    INSTALL_CREATED_BY_US=true
fi

# Extract — accept either "aerodrome-vX.Y.Z/..." wrapper or flat layout
log_info "Extracting to $PREFIX..."
extract_tmp="$TMPDIR_BOOT/extracted"
mkdir -p "$extract_tmp"
unzip -q "$TMPDIR_BOOT/release.zip" -d "$extract_tmp"

# Find the directory containing VERSION + main.py
src_root=""
if [ -f "$extract_tmp/VERSION" ] && [ -f "$extract_tmp/main.py" ]; then
    src_root="$extract_tmp"
else
    src_root="$(find "$extract_tmp" -mindepth 1 -maxdepth 2 -type d \
        \( -exec test -f '{}/VERSION' \; -a -exec test -f '{}/main.py' \; \) \
        -print -quit)"
fi
if [ -z "$src_root" ] || [ ! -d "$src_root" ]; then
    log_err "Could not locate Aerodrome tree inside the zip."
    exit 1
fi

# Move contents to PREFIX
mv "$src_root"/* "$src_root"/.[!.]* "$PREFIX/" 2>/dev/null || true
log_ok "Extracted to $PREFIX"

# ---------------------------------------------------------------------------
# [6/7] Initial configuration
# ---------------------------------------------------------------------------
log_step "6/7" "Initial configuration..."
echo ""
echo "  We'll set the bare minimum to get Aerodrome talking to your receiver."
echo "  Everything below — and much more (watchlist, notifications, retention,"
echo "  display preferences) — is editable in the web UI later under the gear"
echo "  menu → Configuration. No reinstall required."
echo ""

# Auto-detect timezone for default offering
detected_tz=""
if [ -r /etc/timezone ]; then
    detected_tz="$(cat /etc/timezone | tr -d '[:space:]')"
elif command -v timedatectl >/dev/null 2>&1; then
    detected_tz="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
fi
[ -z "$detected_tz" ] && detected_tz="UTC"

# v3.1.0: demo-mode prompt. If --demo wasn't already passed on the
# command line, ask whether the user has a real receiver or wants to
# explore Aerodrome with simulated data. Picking demo mode skips the
# receiver IP/port prompts (uses 127.0.0.1:8080 — the synthetic feeder)
# but still prompts for lat/lon since those become the simulated
# receiver's home coordinates.
if [ "$DEMO_MODE_SET" = false ] && [ "$RECV_IP_SET" = false ]; then
    echo ""
    echo "  Real receiver or demo mode?"
    echo ""
    echo "  Aerodrome needs an ADS-B receiver on your network to track real"
    echo "  aircraft — typically readsb, dump1090-fa, tar1090, or PiAware. If"
    echo "  you don't have one yet, you can install in demo mode and explore"
    echo "  Aerodrome with simulated data."
    echo ""
    echo "  In demo mode:"
    echo "    · 50 simulated aircraft visible at a time, with realistic motion"
    echo "    · ~5% in the US military hex range so military detection demos"
    echo "    · Occasional emergency squawks and watchlist hits"
    echo "    · A small starter watchlist so you can see hits trigger"
    echo "    · Stable across restarts (same simulated aircraft each session)"
    echo "    · Switch to a real receiver anytime via Configuration → Demo"
    echo ""
    echo "  [1] I have a receiver  (default — enter receiver details next)"
    echo "  [2] Demo mode          (skip receiver prompts, install synthetic feeder)"
    echo ""
    DEMO_CHOICE=""
    ask_default "Choice" "1" DEMO_CHOICE
    case "$DEMO_CHOICE" in
        2|d|D|demo|DEMO)
            DEMO_MODE=true
            DEMO_MODE_SET=true
            RECV_IP="127.0.0.1"
            RECV_IP_SET=true
            RECV_PORT="8080"
            RECV_PORT_SET=true
            log_info "Demo mode selected — synthetic feeder will be installed."
            ;;
        *)
            : # real install, proceed with normal prompts
            ;;
    esac
    echo ""
fi

# Receiver IP (required)
if [ "$RECV_IP_SET" = false ]; then
    while [ -z "$RECV_IP" ]; do
        ask_default "ADS-B receiver IP address (e.g. 192.168.1.50)" "" RECV_IP  # pii-ok
        if [ -z "$RECV_IP" ]; then
            log_warn "Receiver IP is required for Aerodrome to do anything useful."
        fi
    done
fi

# Receiver port (default 8080)
if [ "$RECV_PORT_SET" = false ]; then
    ask_default "Receiver port" "8080" RECV_PORT
fi

# Lat/lon (optional, with hint)
echo ""
if [ "$DEMO_MODE" = "true" ]; then
    echo "  Latitude/longitude become the simulated receiver's home position."
    echo "  Aircraft will be generated within ~250km of these coords."
    echo "  Defaults to 40N 75W if you skip. Find your coords at https://www.latlong.net/"
else
    echo "  Receiver location enables the Distance column."
    echo "  Find your coords at https://www.latlong.net/"
fi
echo ""

if [ "$LAT_SET" = false ]; then
    ask_default "Latitude  (decimal, optional, blank to skip)" "" LAT
fi
if [ "$LON_SET" = false ]; then
    ask_default "Longitude (decimal, optional, blank to skip)" "" LON
fi

# Distance unit
if [ "$DIST_UNIT_SET" = false ]; then
    while :; do
        ask_default "Distance unit (mi / nmi / km)" "mi" DIST_UNIT
        case "$DIST_UNIT" in
            mi|nmi|km) break ;;
            *) log_warn "Must be one of: mi, nmi, km" ;;
        esac
    done
fi

# Timezone
# v3.0.14: no prompt. The system timezone is auto-detected (line ~530) and
# used silently. Reasoning: the detection sources (/etc/timezone, timedatectl)
# only ever produce valid IANA names by construction, the system tz is
# almost always what the user wants on their own machine, and asking opens
# the door to typos like "CDT" (an abbreviation, not a valid IANA name)
# that produced broken installs pre-v3.0.14. Users who want a different
# timezone can change it in the web UI's Configuration page after install
# without touching the YAML — or pass --timezone at install time for
# scripted overrides. The post-install banner points users to /config so
# they know where to adjust this and other settings.
if [ "$TIMEZONE_SET" = false ]; then
    TIMEZONE="$detected_tz"
    log_info "Time zone: $TIMEZONE (auto-detected; change later in web UI → Configuration)"
fi

# Patch config.yaml from .example with the prompted values, BEFORE install.sh
# runs — install.sh's "if config.yaml doesn't exist, copy from .example" check
# will then see config.yaml exists and won't overwrite our patched copy.
log_info "Writing config.yaml..."
cp "$PREFIX/config.yaml.example" "$PREFIX/config.yaml"

# Use python3 for YAML-safe substitution (we know it's installed by now)
RECV_IP="$RECV_IP" RECV_PORT="$RECV_PORT" LAT="$LAT" LON="$LON" \
DIST_UNIT="$DIST_UNIT" TIMEZONE="$TIMEZONE" DEMO_MODE="$DEMO_MODE" \
python3 - "$PREFIX/config.yaml" <<'PYEOF'
import os, re, sys
path = sys.argv[1]
recv_ip = os.environ["RECV_IP"]
recv_port = os.environ["RECV_PORT"]
lat = os.environ["LAT"].strip()
lon = os.environ["LON"].strip()
dist_unit = os.environ["DIST_UNIT"]
tz = os.environ["TIMEZONE"]
demo_mode = os.environ.get("DEMO_MODE", "false") == "true"

with open(path) as f:
    text = f.read()

def sub_quoted(key_indent_re, value):
    """Replace a `key: "..."` line preserving indent and any trailing comment."""
    nonlocal_text = [text]
    pat = re.compile(rf'^({key_indent_re})"[^"]*"(.*)$', re.M)
    nonlocal_text[0] = pat.sub(rf'\1"{value}"\2', nonlocal_text[0])
    return nonlocal_text[0]

def sub_bare(key_indent_re, value):
    """Replace a `key: <value>` line where the value is not quoted (number/null)."""
    pat = re.compile(rf'^({key_indent_re})\S+(\s.*)?$', re.M)
    return pat.sub(rf'\1{value}\2', text) if False else pat.sub(
        lambda m: f"{m.group(1)}{value}{m.group(2) or ''}", text)

# receiver.ip — quoted string
text = re.sub(r'^(\s*ip:\s*)"[^"]*"(.*)$', rf'\1"{recv_ip}"\2', text, count=1, flags=re.M)
# receiver.port — bare integer (only first occurrence, which is receiver.port)
text = re.sub(r'^(\s*port:\s*)\S+(\s.*)?$',
              lambda m: f'{m.group(1)}{recv_port}{m.group(2) or ""}',
              text, count=1, flags=re.M)
# receiver.latitude / longitude — bare value (null or number)
lat_val = lat if lat else "null"
lon_val = lon if lon else "null"
text = re.sub(r'^(\s*latitude:\s*)\S+(\s.*)?$',
              lambda m: f'{m.group(1)}{lat_val}{m.group(2) or ""}',
              text, count=1, flags=re.M)
text = re.sub(r'^(\s*longitude:\s*)\S+(\s.*)?$',
              lambda m: f'{m.group(1)}{lon_val}{m.group(2) or ""}',
              text, count=1, flags=re.M)
# receiver.distance_unit — quoted string
text = re.sub(r'^(\s*distance_unit:\s*)"[^"]*"(.*)$',
              rf'\1"{dist_unit}"\2', text, count=1, flags=re.M)
# stats.timezone — quoted string (may be empty)
text = re.sub(r'^(\s*timezone:\s*)"[^"]*"(.*)$',
              rf'\1"{tz}"\2', text, count=1, flags=re.M)

# v3.1.0: demo.enabled — flip to true if --demo. The demo: section
# in config.yaml.example sits at the file's end with `enabled: false`
# directly under the `demo:` header. We find the section first, then
# patch its enabled line — pattern is narrow enough that it won't
# accidentally match other `enabled:` keys elsewhere in the file.
if demo_mode:
    demo_section = re.search(r"^demo:\s*\n", text, re.M)
    if demo_section:
        # Find the next `enabled:` line after the demo header
        after = text[demo_section.end():]
        m = re.search(r"^(\s*enabled:\s*)false(.*)$", after, re.M)
        if m:
            new_after = after[:m.start()] + m.group(1) + "true" + m.group(2) + after[m.end():]
            text = text[:demo_section.end()] + new_after

with open(path, "w") as f:
    f.write(text)
PYEOF
log_ok "config.yaml written"

# ---------------------------------------------------------------------------
# [7/7] Hand off to install.sh
# ---------------------------------------------------------------------------
log_step "7/7" "Running install.sh..."
echo ""
chmod +x "$PREFIX/install.sh"

# v3.1.0: pass demo flags to install.sh when in demo mode. The home
# coords mirror what the bootstrap wrote into config.yaml so the
# feeder service + the seed_watchlist script agree on geometry.
INSTALL_ARGS=()
if [ "$DEMO_MODE" = "true" ]; then
    INSTALL_ARGS+=("--demo")
    INSTALL_ARGS+=("--home-lat" "${LAT:-40.0}")
    INSTALL_ARGS+=("--home-lon" "${LON:--75.0}")
fi
( cd "$PREFIX" && ./install.sh "${INSTALL_ARGS[@]}" )

# ---------------------------------------------------------------------------
# Optional: open the web UI port in firewalld
# ---------------------------------------------------------------------------
# Several distros (Fedora, openSUSE Tumbleweed, RHEL family) enable
# firewalld by default with a restrictive "public" zone. Port 8000 is
# closed there, which makes the web UI reachable from localhost only —
# not what a user wants in the typical "view the dashboard from my
# laptop" scenario. Offer to open it persistently. Skip silently on
# systems without firewalld (Debian/Ubuntu/Arch in their default state).
if command -v firewall-cmd >/dev/null 2>&1 && \
        systemctl is-active firewalld --quiet 2>/dev/null; then
    # Only offer if port 8000 isn't already open on the default zone.
    if ! sudo firewall-cmd --list-ports 2>/dev/null | grep -qw "8000/tcp"; then
        echo ""
        log_info "firewalld is active. Port 8000 (the web UI) is currently closed."
        do_open=false
        if [ "$ASSUME_YES" = true ] || [ "$FORCE" = true ]; then
            do_open=true
            log_info "Opening port 8000 automatically (per --yes/--force)"
        else
            read -r -p "  Open port 8000 in firewalld so you can reach the dashboard? [Y/n] " reply
            if [[ ! "$reply" =~ ^[Nn]$ ]]; then
                do_open=true
            fi
        fi
        if [ "$do_open" = true ]; then
            if sudo firewall-cmd --add-port=8000/tcp --permanent >/dev/null 2>&1 \
                    && sudo firewall-cmd --reload >/dev/null 2>&1; then
                log_ok "Opened port 8000/tcp permanently on the public zone"
            else
                log_warn "Could not open port 8000 — run manually:"
                echo "    sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload"
            fi
        else
            log_info "Port 8000 left closed. To open later:"
            echo "    sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
COMPLETED=true
# v3.2.1: cross-distro server IP detection — see install.sh for rationale.
SERVER_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
[ -z "$SERVER_IP" ] && SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$SERVER_IP" ] && SERVER_IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
[ -z "$SERVER_IP" ] && SERVER_IP="localhost"
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Bootstrap complete!${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Aerodrome is running at: ${CYAN}http://${SERVER_IP}:8000${RESET}"
echo ""
if [ "$DEMO_MODE" = "true" ]; then
    echo -e "  ${YELLOW}Demo mode is on.${RESET}"
    echo "  · You'll see a yellow 'Demo mode' banner across every page."
    echo "  · 50 simulated aircraft are visible, with a starter watchlist."
    echo "  · Notifications (if configured) are prefixed with [DEMO]."
    echo ""
    echo -e "  ${YELLOW}When you're ready for a real receiver:${RESET}"
    echo -e "  visit ${CYAN}gear menu → Configuration → Demo${RESET} and use the"
    echo "  'Switch to real receiver' wizard. It will stop the synthetic feeder,"
    echo "  clear demo data, and set up Aerodrome to poll your real receiver."
    echo ""
else
    echo -e "  ${YELLOW}Next step:${RESET} open the URL above, then visit"
    echo -e "  ${CYAN}gear menu → Configuration${RESET} to review and adjust settings"
    echo "  (timezone, watchlist, notifications, retention, display preferences,"
    echo "  and more). The install picked sensible defaults but most users will"
    echo "  want to customize at least a few of them."
    echo ""
fi
echo "  Service:  sudo systemctl status aerodrome"
echo "  Logs:     sudo journalctl -u aerodrome -f"
echo "  Config:   $PREFIX/config.yaml"
echo ""
