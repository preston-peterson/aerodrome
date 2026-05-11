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
#   --prefix <path>        Install directory (default: ~/aerodrome)
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
PREFIX="${HOME}/aerodrome"
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

# Track whether each value was supplied as a flag (skip the prompt) vs. left
# at default (still prompt, with the default offered).
RECV_IP_SET=false
RECV_PORT_SET=false
LAT_SET=false
LON_SET=false
DIST_UNIT_SET=false
TIMEZONE_SET=false

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
  --prefix <path>        Install directory (default: ~/aerodrome)
  --version <vX.Y.Z>     Pin to a specific release (default: latest)
  --from-zip <path>      Skip GitHub fetch; install from a local zip
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

# Tier 1: recognized Debian family — proceed silently
recognized=false
case "$OS_ID" in
    debian|ubuntu|raspbian|linuxmint|pop|elementary|neon)
        recognized=true ;;
esac
if [ "$recognized" = false ] && [ -n "$OS_LIKE" ]; then
    case " $OS_LIKE " in
        *" debian "*|*" ubuntu "*) recognized=true ;;
    esac
fi

if [ "$recognized" = true ]; then
    log_ok "Recognized Debian-family system"
else
    # Tier 2/3: check capabilities
    has_apt=false
    has_systemctl=false
    command -v apt >/dev/null 2>&1 && has_apt=true
    command -v systemctl >/dev/null 2>&1 && has_systemctl=true

    if [ "$has_apt" = false ] || [ "$has_systemctl" = false ]; then
        # Tier 3: hard refuse
        log_err "Aerodrome's installer requires apt (Debian-family package manager)"
        log_err "and systemctl (systemd). Your system reports ID=$OS_ID."
        echo ""
        echo "  See docs/INSTALL.md for manual install steps on other distros."
        echo "  Browse the docs at: https://github.com/${REPO}/blob/main/docs/INSTALL.md"
        exit 1
    fi

    # Tier 2: warn and prompt (or accept --force)
    if [ "$FORCE" = true ]; then
        log_warn "Unrecognized distro ($OS_ID), but apt and systemctl present — continuing per --force"
    else
        log_warn "Aerodrome is tested on Ubuntu, Debian, and Raspberry Pi OS."
        log_warn "Your system reports ID=$OS_ID, but apt and systemctl are available,"
        log_warn "so the install will probably work."
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

NEED_APT=()
command -v curl       >/dev/null 2>&1 || NEED_APT+=("curl")
command -v unzip      >/dev/null 2>&1 || NEED_APT+=("unzip")
command -v sha256sum  >/dev/null 2>&1 || NEED_APT+=("coreutils")
command -v python3    >/dev/null 2>&1 || NEED_APT+=("python3")

# python3-venv is a separate package on Debian/Ubuntu
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c 'import ensurepip' 2>/dev/null; then
        NEED_APT+=("python3-venv")
    fi
else
    NEED_APT+=("python3-venv")
fi

if [ ${#NEED_APT[@]} -gt 0 ]; then
    log_info "Need to install: ${NEED_APT[*]}"
    log_info "Running: sudo apt update && sudo apt install -y ${NEED_APT[*]}"
    sudo apt update -qq
    sudo apt install -y -qq "${NEED_APT[@]}" >/dev/null
    log_ok "Prerequisites installed"
else
    log_ok "All prerequisites present"
fi

# Python version check (need 3.10+)
PY_OK=$(python3 -c 'import sys; print("yes" if sys.version_info >= (3,10) else "no")' 2>/dev/null || echo "no")
if [ "$PY_OK" != "yes" ]; then
    log_err "Python 3.10+ required; found $(python3 --version 2>&1 || echo 'none')"
    echo ""
    echo "  On older Ubuntu/Debian releases you may need a newer python3 from"
    echo "  the deadsnakes PPA or a backport. See docs/INSTALL.md."
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

# If PREFIX exists and isn't empty, ask before clobbering
if [ -d "$PREFIX" ] && [ -n "$(ls -A "$PREFIX" 2>/dev/null)" ]; then
    log_warn "Directory $PREFIX already exists and is not empty."
    if [ "$ASSUME_YES" = true ]; then
        log_err "Refusing to overwrite (would need interactive confirmation)."
        exit 1
    fi
    read -r -p "  Overwrite? Existing contents will be removed [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
    rm -rf "$PREFIX"
fi

mkdir -p "$PREFIX"
INSTALL_CREATED_BY_US=true

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

# Receiver IP (required)
if [ "$RECV_IP_SET" = false ]; then
    while [ -z "$RECV_IP" ]; do
        ask_default "ADS-B receiver IP address (e.g. 192.168.1.50)" "" RECV_IP
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
echo "  Receiver location enables the Distance column."
echo "  Find your coords at https://www.latlong.net/"
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
DIST_UNIT="$DIST_UNIT" TIMEZONE="$TIMEZONE" \
python3 - "$PREFIX/config.yaml" <<'PYEOF'
import os, re, sys
path = sys.argv[1]
recv_ip = os.environ["RECV_IP"]
recv_port = os.environ["RECV_PORT"]
lat = os.environ["LAT"].strip()
lon = os.environ["LON"].strip()
dist_unit = os.environ["DIST_UNIT"]
tz = os.environ["TIMEZONE"]

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
( cd "$PREFIX" && ./install.sh )

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
COMPLETED=true
SERVER_IP="$(hostname -I | awk '{print $1}')"
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Bootstrap complete!${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Aerodrome is running at: ${CYAN}http://${SERVER_IP}:8000${RESET}"
echo ""
echo -e "  ${YELLOW}Next step:${RESET} open the URL above, then visit"
echo -e "  ${CYAN}gear menu → Configuration${RESET} to review and adjust settings"
echo "  (timezone, watchlist, notifications, retention, display preferences,"
echo "  and more). The install picked sensible defaults but most users will"
echo "  want to customize at least a few of them."
echo ""
echo "  Service:  sudo systemctl status aerodrome"
echo "  Logs:     sudo journalctl -u aerodrome -f"
echo "  Config:   $PREFIX/config.yaml"
echo ""
