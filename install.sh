#!/bin/bash
# Version: 3.4.32
# =============================================================================
# Aerodrome — Server Install Script
# =============================================================================
#
# Usage:
#   1. Copy aerodrome/ to your server (rsync recommended over scp)
#   2. SSH into the server
#   3. cd ~/aerodrome && chmod +x install.sh && ./install.sh
#
# This script will:
#   - Install Python 3, pip, venv (via apt)
#   - Create a Python virtual environment in ./venv
#   - Install all Python dependencies
#   - Install a systemd service that runs as the current user
#   - Start the service
#
# =============================================================================

set -e
GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
CYAN='\033[36m\033[1m'
YELLOW='\033[33m\033[1m'
RESET='\033[0m'

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="aerodrome"
FEEDER_SERVICE_NAME="aerodrome-synthetic-feeder"

# v3.1.2: refuse to run from inside an existing install's `update/` staging
# directory. The hazard: if a user unpacks a release zip into ~/aerodrome/
# update/ (e.g. from the in-app updater's staged upload) and runs
# `./install.sh` from there, INSTALL_DIR resolves to the staging directory
# and the systemd unit ends up pointing at /home/user/aerodrome/update/
# instead of /home/user/aerodrome/. The service then 203/EXEC's because
# the venv lives in the real install root, not staging.
#
# The detection: if INSTALL_DIR's parent has a VERSION file and a main.py,
# we're sitting in a subdirectory of an existing install. Refuse with a
# clear pointer at the real install root.
INSTALL_DIR_PARENT="$(dirname "$INSTALL_DIR")"
INSTALL_DIR_BASENAME="$(basename "$INSTALL_DIR")"
if [ "$INSTALL_DIR_BASENAME" = "update" ] \
        && [ -f "$INSTALL_DIR_PARENT/VERSION" ] \
        && [ -f "$INSTALL_DIR_PARENT/main.py" ]; then
    echo -e "\033[31m\033[1mError:\033[0m install.sh is sitting inside an existing install's update/ staging directory:" >&2
    echo "  Current location: $INSTALL_DIR" >&2
    echo "  Real install:     $INSTALL_DIR_PARENT" >&2
    echo "" >&2
    echo "Running install.sh from here would write a systemd unit pointing at" >&2
    echo "the staging directory and break your install. Run from the real install" >&2
    echo "root instead:" >&2
    echo "" >&2
    echo "  cd $INSTALL_DIR_PARENT && ./install.sh" >&2
    echo "" >&2
    exit 2
fi

# v3.1.0: --demo flag puts the install into demo mode. The bootstrap
# passes --demo when the user picks "explore with simulated data" at
# the install-time prompt. Demo mode installs a second systemd unit
# (aerodrome-synthetic-feeder.service) alongside the main aerodrome
# service, seeds a small starter watchlist, and sets demo.enabled=true
# in config.yaml so the dashboard surfaces the demo banner and prefixes
# notifications with [DEMO].
DEMO_MODE=false
HOME_LAT="40.0"
HOME_LON="-75.0"
while [ $# -gt 0 ]; do
    case "$1" in
        --demo)             DEMO_MODE=true; shift ;;
        --home-lat)         HOME_LAT="$2"; shift 2 ;;
        --home-lon)         HOME_LON="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Aerodrome install script.

Usage:
  ./install.sh                    Real install (default — assumes you have a receiver)
  ./install.sh --demo             Demo install (synthetic feeder, no receiver needed)

Flags (used by bootstrap.sh --demo):
  --home-lat <n>                  Synthetic receiver latitude (default: 40.0)
  --home-lon <n>                  Synthetic receiver longitude (default: -75.0)
EOF
            exit 0 ;;
        *)
            echo "Unknown flag: $1" >&2
            echo "Run ./install.sh --help for usage." >&2
            exit 2 ;;
    esac
done

# Use the invoking user (works whether invoked directly or via sudo)
USER="${SUDO_USER:-$(whoami)}"

if [ "$USER" = "root" ]; then
    echo -e "${RED}Please run this script as a regular user (not root).${RESET}"
    echo "It will use sudo only where needed."
    exit 1
fi

# v2.40.3 note: if invoked via `sudo ./install.sh`, SUDO_USER points at
# the real user and we continue. We used to silently create root-owned
# files in that case, which broke the service at startup. The script
# now chowns everything it touches back to ${USER}, so both paths work
# — but the `./install.sh` form (without sudo) is still cleaner, since
# the script uses sudo only where it actually needs root.
if [ -n "${SUDO_USER:-}" ]; then
    echo -e "${CYAN}Note:${RESET} running via sudo. Ownership of created files"
    echo "  will be set to ${USER}. Running as './install.sh' (no sudo) also works"
    echo "  and is slightly cleaner — the script uses sudo where needed."
    echo ""
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║         Aerodrome — Server Install           ║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Install dir:  ${INSTALL_DIR}"
echo "  Service name: ${SERVICE_NAME}"
echo "  Run as user:  ${USER}"
if [ "$DEMO_MODE" = "true" ]; then
    echo ""
    echo -e "  ${YELLOW}Demo mode:${RESET} also installing ${FEEDER_SERVICE_NAME}"
    echo -e "             synthetic receiver at (${HOME_LAT}, ${HOME_LON})"
fi
echo ""

echo -e "${CYAN}[1/5]${RESET} Installing system packages..."

# v3.2.0: multi-distro package install. Detect the package manager family
# from /etc/os-release and use the right command + package names. Block is
# inlined (not sourced) so install.sh remains standalone if copied alone.
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
        echo "  ${RED}✗${RESET} Unknown package manager — cannot install: ${pkgs[*]}" >&2
        echo "    Aerodrome requires apt-get, dnf, pacman, or zypper." >&2
        return 1
    }
    $PKG_INSTALL_CMD "${pkgs[@]}"
}
pkg_refresh() {
    [ -z "$PKG_REFRESH_CMD" ] && return 0
    $PKG_REFRESH_CMD
}
pkg_detect

if [ "$PKG_FAMILY" = "unknown" ]; then
    echo -e "  ${RED}✗${RESET} Could not detect a supported package manager"
    echo "    Supported families: Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE"
    echo "    See docs/INSTALL.md for manual install steps on other distros."
    exit 1
fi

pkg_refresh > /dev/null 2>&1
pkg_install "$PKG_PYTHON3" "$PKG_PIP" "$PKG_VENV" "$PKG_CURL" > /dev/null 2>&1
echo -e "  ${GREEN}✓${RESET} Python3, pip, venv installed (via $PKG_FAMILY package manager)"

echo -e "${CYAN}[2/5]${RESET} Setting up Python virtual environment..."
cd "$INSTALL_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
deactivate
echo -e "  ${GREEN}✓${RESET} Virtual environment ready"

echo -e "${CYAN}[3/5]${RESET} Creating directories..."
mkdir -p "${INSTALL_DIR}/logs"
mkdir -p "${INSTALL_DIR}/update"
mkdir -p "${INSTALL_DIR}/.backups"
# v2.40.3 fix: if this script was invoked via `sudo ./install.sh`, the
# directories above were created as root and systemd can't write to them
# when the service starts as ${USER}. Same hazard for the venv directory
# (step 2 above — pip installs get written as whoever ran the shell).
# Force ownership to ${USER} on everything the service will need to
# write or read at runtime. Idempotent: safe when already correctly owned.
chown -R "${USER}:${USER}" \
    "${INSTALL_DIR}/logs" \
    "${INSTALL_DIR}/update" \
    "${INSTALL_DIR}/.backups" \
    "${INSTALL_DIR}/venv" 2>/dev/null || true
echo -e "  ${GREEN}✓${RESET} Log, update, and backup directories created"

# Create config.yaml from example if it doesn't exist (preserves existing config on upgrade)
if [ ! -f "${INSTALL_DIR}/config.yaml" ]; then
    if [ -f "${INSTALL_DIR}/config.yaml.example" ]; then
        cp "${INSTALL_DIR}/config.yaml.example" "${INSTALL_DIR}/config.yaml"
        # Same ownership fix — cp as root would leave config.yaml root-owned,
        # blocking the in-UI config editor from writing changes.
        chown "${USER}:${USER}" "${INSTALL_DIR}/config.yaml" 2>/dev/null || true
        echo -e "  ${GREEN}✓${RESET} Created config.yaml from example (edit it to set your receiver IP)"

        # v3.1.0: when --demo is used AND we just created a fresh
        # config.yaml from the example, patch the receiver target to
        # the synthetic feeder + flip demo.enabled=true. Lets a
        # manual zip install run `./install.sh --demo` without
        # needing the bootstrap. On upgrade paths (config.yaml
        # already existed) we leave the file alone — the in-app
        # switch-to-real wizard is the path for those.
        if [ "$DEMO_MODE" = "true" ]; then
            DEMO_HOME_LAT="$HOME_LAT" DEMO_HOME_LON="$HOME_LON" \
            python3 - "${INSTALL_DIR}/config.yaml" <<'PYEOF'
import os, re, sys
path = sys.argv[1]
home_lat = os.environ.get("DEMO_HOME_LAT", "40.0")
home_lon = os.environ.get("DEMO_HOME_LON", "-75.0")
with open(path, "r", encoding="utf-8") as f:
    text = f.read()
# receiver.ip → 127.0.0.1 (first match is receiver.ip; same pattern bootstrap uses)
text = re.sub(r'^(\s*ip:\s*)"[^"]*"(.*)$', r'\1"127.0.0.1"\2',
              text, count=1, flags=re.M)
# receiver.port → 8080
text = re.sub(r'^(\s*port:\s*)\S+(\s.*)?$',
              lambda m: f'{m.group(1)}8080{m.group(2) or ""}',
              text, count=1, flags=re.M)
# receiver.latitude / longitude → demo home coords
text = re.sub(r'^(\s*latitude:\s*)\S+(\s.*)?$',
              lambda m: f'{m.group(1)}{home_lat}{m.group(2) or ""}',
              text, count=1, flags=re.M)
text = re.sub(r'^(\s*longitude:\s*)\S+(\s.*)?$',
              lambda m: f'{m.group(1)}{home_lon}{m.group(2) or ""}',
              text, count=1, flags=re.M)
# demo.enabled: false → true (under the demo: section header)
ds = re.search(r"^demo:\s*\n", text, re.M)
if ds:
    after = text[ds.end():]
    m = re.search(r"^(\s*enabled:\s*)false(.*)$", after, re.M)
    if m:
        new_after = after[:m.start()] + m.group(1) + "true" + m.group(2) + after[m.end():]
        text = text[:ds.end()] + new_after
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
PYEOF
            echo -e "  ${GREEN}✓${RESET} Patched config.yaml for demo mode"
        fi
    fi
else
    echo -e "  ${GREEN}✓${RESET} Existing config.yaml preserved — new keys will be merged on service start"
fi

echo -e "${CYAN}[4/5]${RESET} Installing systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << SERVICEEOF
[Unit]
Description=Aerodrome — ADS-B Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
Group=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 main.py start
ExecStop=${INSTALL_DIR}/venv/bin/python3 main.py stop
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
echo -e "  ${GREEN}✓${RESET} Service installed and enabled"

# v3.1.0: in demo mode, also install the synthetic-feeder service.
# It serves /data/aircraft.json on port 8080 (matching the demo-mode
# receiver config the bootstrap wrote: receiver.ip=127.0.0.1:8080).
# Runs independently of aerodrome.service — both start in parallel
# on boot, and the collector's existing retry behaviour handles the
# rare case where it polls before the feeder is listening.
#
# Seed is locked to 1903 (Wright Brothers' first powered flight) so:
#   - every demo install everywhere sees the same 50 simulated aircraft
#   - restarts produce the same fleet (the user's "regulars" persist)
#   - the seed_watchlist.py output matches what the running feeder
#     actually generates, since both use the same seed + home coords
if [ "$DEMO_MODE" = "true" ]; then
    echo -e "  ${YELLOW}·${RESET}  Installing ${FEEDER_SERVICE_NAME} (demo mode)..."
    sudo tee /etc/systemd/system/${FEEDER_SERVICE_NAME}.service > /dev/null << FEEDEREOF
[Unit]
Description=Aerodrome — Synthetic ADS-B Feeder (demo mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
Group=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 -m tools.synthetic_feeder.serve \\
    --host 127.0.0.1 \\
    --port 8080 \\
    --visible 50 \\
    --home-lat ${HOME_LAT} \\
    --home-lon ${HOME_LON} \\
    --seed 1903
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
FEEDEREOF

    sudo systemctl daemon-reload
    sudo systemctl enable ${FEEDER_SERVICE_NAME}
    echo -e "  ${GREEN}✓${RESET} ${FEEDER_SERVICE_NAME} installed and enabled"
fi

# Grant the service user passwordless permission for two scoped things:
#   1. Restarting the aerodrome service (for the "Restart now" UI button
#      and post-update restarts).
#   2. Installing and managing a LOCAL ntfy service alongside Aerodrome
#      (the one-click installer in the Notifications tab). The install
#      needs to write to /usr/local/bin, /etc/ntfy, /etc/systemd/system,
#      and /var/cache/ntfy, plus run systemctl on the ntfy unit.
#
# Each allowed command is listed explicitly — no wildcards that could be
# leveraged into broader privileges. We list the plain and --no-block forms
# separately because sudoers treats them as different commands. Arguments
# to 'install', 'tee', 'rm', and 'rmdir' are restricted by specifying the
# full path prefix we write to (/usr/local/bin/ntfy, /etc/ntfy/*, etc.).
SUDOERS_FILE="/etc/sudoers.d/aerodrome"
echo -e "  Creating sudoers rule for in-UI restart button + ntfy installer..."
sudo tee "${SUDOERS_FILE}" > /dev/null << SUDOEOF
# SUDOERS_VERSION: 4
# Aerodrome sudoers rule — machine-readable.
# The SUDOERS_VERSION comment above is read by the updater to detect when
# a newer release requires this file to be refreshed. If you see a
# "Sudoers update required" modal in the UI, re-run this install.sh on
# the server to write the new version. Never edit this file by hand.
#
# Allow ${USER} to restart the aerodrome service without a password.
# Used by the "Restart now" UI button and automatic post-update restarts.
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart aerodrome, /usr/bin/systemctl restart aerodrome, /bin/systemctl restart --no-block aerodrome, /usr/bin/systemctl restart --no-block aerodrome

# Allow ${USER} to install and manage the LOCAL ntfy service when the user
# opts in via the Notifications tab. Scoped strictly to the ntfy-related
# paths Aerodrome owns. These commands are only issued by ntfy_installer.py.
${USER} ALL=(ALL) NOPASSWD: /usr/bin/install -m 0755 /var/cache/ntfy/staging/ntfy /usr/local/bin/ntfy
${USER} ALL=(ALL) NOPASSWD: /usr/bin/install -d -m 0755 /etc/ntfy
${USER} ALL=(ALL) NOPASSWD: /usr/bin/install -d -m 0755 -o ${USER} -g ${USER} /var/cache/ntfy
${USER} ALL=(ALL) NOPASSWD: /usr/bin/install -d -m 0755 /var/lib/ntfy
${USER} ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/ntfy/server.yml
${USER} ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/systemd/system/ntfy.service
${USER} ALL=(ALL) NOPASSWD: /usr/bin/tee /var/lib/ntfy/aerodrome-installed
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /usr/local/bin/ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /etc/ntfy/server.yml
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /etc/systemd/system/ntfy.service
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /var/lib/ntfy/aerodrome-installed
# v2.41.0: data-purge commands, used only when user opts in via the
# "Also delete cached messages" checkbox during uninstall. Scoped to
# the three ntfy cache files + the attachments subdirectory. Explicit
# paths — no wildcards.
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /var/cache/ntfy/cache.db
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /var/cache/ntfy/cache.db-wal
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /var/cache/ntfy/cache.db-shm
${USER} ALL=(ALL) NOPASSWD: /bin/rm -rf /var/cache/ntfy/attachments
${USER} ALL=(ALL) NOPASSWD: /bin/rmdir --ignore-fail-on-non-empty /etc/ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/rmdir --ignore-fail-on-non-empty /var/cache/ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/rmdir --ignore-fail-on-non-empty /var/lib/ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl daemon-reload, /usr/bin/systemctl daemon-reload
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl enable ntfy, /usr/bin/systemctl enable ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl disable ntfy, /usr/bin/systemctl disable ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl start ntfy, /usr/bin/systemctl start ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop ntfy, /usr/bin/systemctl stop ntfy
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart ntfy, /usr/bin/systemctl restart ntfy

# Allow ${USER} to read /etc/sudoers.d/aerodrome so the updater can
# verify the live SUDOERS_VERSION against the staged release's marker.
# Read-only — sudoers does not grant write.
${USER} ALL=(ALL) NOPASSWD: /bin/cat /etc/sudoers.d/aerodrome, /usr/bin/cat /etc/sudoers.d/aerodrome

# v3.1.0: synthetic-feeder service lifecycle. Demo-mode installs need
# the switch-to-real wizard to be able to stop, disable, and remove
# the feeder service without a password. The rule is unconditional in
# the sudoers file (rather than gated on DEMO_MODE) because:
#   1. The feeder service file simply may not exist on real installs,
#      so these commands fail harmlessly there.
#   2. Keeping the sudoers rule consistent across real and demo installs
#      means the SUDOERS_VERSION marker doesn't need a demo-mode branch.
#   3. The aerodrome-synthetic-feeder unit name is exact (no wildcards)
#      so this can't be leveraged into broader privileges.
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop aerodrome-synthetic-feeder, /usr/bin/systemctl stop aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl start aerodrome-synthetic-feeder, /usr/bin/systemctl start aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart aerodrome-synthetic-feeder, /usr/bin/systemctl restart aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl enable aerodrome-synthetic-feeder, /usr/bin/systemctl enable aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl disable aerodrome-synthetic-feeder, /usr/bin/systemctl disable aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /etc/systemd/system/aerodrome-synthetic-feeder.service
SUDOEOF
sudo chmod 0440 "${SUDOERS_FILE}"
if sudo visudo -cf "${SUDOERS_FILE}" >/dev/null 2>&1; then
    echo -e "  ${GREEN}✓${RESET} Sudoers rule installed (${SUDOERS_FILE})"
else
    echo -e "  ${RED}✗${RESET} Sudoers rule failed validation — removing"
    sudo rm -f "${SUDOERS_FILE}"
    echo -e "  ${RED}⚠${RESET}  The 'Restart now' button and the ntfy auto-installer will not work."
    echo -e "     You can still use 'sudo systemctl restart aerodrome' manually."
fi

# v3.1.0: in demo mode, seed a small starter watchlist so the user
# sees watchlist hits trigger during their first exploration session.
# The 8 ICAOs are computed deterministically from seed=1903 + the
# user's home coords — same generation the running feeder uses, so
# the watchlisted aircraft are the actual "regulars" they'll see.
# Idempotent (script bails if watchlist is already populated).
if [ "$DEMO_MODE" = "true" ]; then
    echo -e "  ${YELLOW}·${RESET}  Seeding demo watchlist..."
    if "${INSTALL_DIR}/venv/bin/python3" -m tools.synthetic_feeder.seed_watchlist \
        "${INSTALL_DIR}/config.yaml" "${HOME_LAT}" "${HOME_LON}"; then
        echo -e "  ${GREEN}✓${RESET} Demo watchlist seeded"
    else
        echo -e "  ${YELLOW}⚠${RESET}  Demo watchlist seeding failed (non-fatal — you can add"
        echo -e "     watchlist entries from the Watchlist tab in the web UI)."
    fi
fi

echo -e "${CYAN}[5/5]${RESET} Starting the tracker..."
if [ "$DEMO_MODE" = "true" ]; then
    sudo systemctl start ${FEEDER_SERVICE_NAME}
fi
sudo systemctl start ${SERVICE_NAME}
sleep 3

if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "  ${GREEN}✓${RESET} Tracker is running!"
else
    echo -e "  ${RED}✗${RESET} Failed to start. Check: sudo journalctl -u ${SERVICE_NAME} -n 50"
    exit 1
fi

# v3.2.1: cross-distro server IP detection. The previous `hostname -I`
# is a Debian extension — on Arch (which uses inetutils' hostname) it
# fails silently and SERVER_IP ends up empty, rendering the welcome
# URL as "http://:8000". `ip route get` is the universal approach
# (iproute2 is on every modern Linux with systemd); it consults the
# routing table without actually sending packets. Layered fallbacks
# cover minimal containers and old systems.
SERVER_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
[ -z "$SERVER_IP" ] && SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$SERVER_IP" ] && SERVER_IP=$(hostname -i 2>/dev/null | awk '{print $1}')
[ -z "$SERVER_IP" ] && SERVER_IP="localhost"
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Install complete!${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo "  Web UI:  http://${SERVER_IP}:8000"
echo ""
if [ "$DEMO_MODE" = "true" ]; then
    echo -e "  ${YELLOW}Demo mode is on.${RESET}"
    echo "  · You'll see a yellow 'Demo mode' banner across every page."
    echo "  · The feeder serves 50 simulated aircraft at (${HOME_LAT}, ${HOME_LON})."
    echo "  · 8 starter watchlist entries are seeded for you."
    echo "  · When you're ready to connect a real receiver, use the"
    echo "    'Switch to real receiver' wizard at Configuration → Demo."
    echo ""
fi
echo "  Commands:"
echo "    sudo systemctl status ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    sudo systemctl stop ${SERVICE_NAME}"
echo "    sudo journalctl -u ${SERVICE_NAME} -f"
if [ "$DEMO_MODE" = "true" ]; then
    echo "    sudo systemctl status ${FEEDER_SERVICE_NAME}"
    echo "    sudo journalctl -u ${FEEDER_SERVICE_NAME} -f"
fi
echo ""
echo "  Config:  ${INSTALL_DIR}/config.yaml"
echo "  Logs:    ${INSTALL_DIR}/logs/tracker.log"
echo ""
