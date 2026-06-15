#!/usr/bin/env bash
# Version: 3.4.44
# =============================================================================
# Aerodrome — Sudoers Recovery Script
# =============================================================================
#
# Refreshes /etc/sudoers.d/aerodrome to the SUDOERS_VERSION baked into this
# script. Self-contained: does NOT depend on the live install.sh being
# current.
#
# WHY THIS EXISTS:
#   When a release bumps SUDOERS_VERSION, the in-UI "Sudoers update required"
#   prompt asks you to run `sudo bash /opt/aerodrome/install.sh`. But that
#   path is the LIVE (already-installed) install.sh — which writes the OLD
#   sudoers version. Running it doesn't lift the gate; the staged install.sh
#   that writes the NEW version sits inside the update/ subdir, but the UI
#   doesn't surface that path.
#
#   This script is the escape hatch. It bakes in the latest sudoers content
#   directly and writes it without consulting either install.sh.
#
# USAGE:
#   # From a GitHub release / raw URL — typical case:
#   curl -fsSL https://raw.githubusercontent.com/preston-peterson/aerodrome/main/scripts/recover-sudoers.sh | sudo bash
#
#   # If your install is somewhere other than /opt/aerodrome:
#   curl -fsSL <url> | sudo AERODROME_INSTALL_DIR=/path/to/aerodrome bash
#
#   # From the staged update directory on a stuck install:
#   sudo bash /opt/aerodrome/update/aerodrome-vX.Y.Z/scripts/recover-sudoers.sh
#
# WHAT IT DOES:
#   1. Detects the Aerodrome install user from /opt/aerodrome ownership.
#   2. Composes the latest sudoers content with that user substituted in.
#   3. Validates via `visudo -cf` before installing — bails on parse errors
#      so we never break sudo for the whole system.
#   4. Atomically installs to /etc/sudoers.d/aerodrome with 0440 perms.
#   5. Best-effort restarts aerodrome.service so the running process picks
#      up the new sudoers grant immediately.
#
# IDEMPOTENT — safe to re-run.
# =============================================================================

set -euo pipefail

GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
YELLOW='\033[33m\033[1m'
CYAN='\033[36m\033[1m'
RESET='\033[0m'

# Must be root for sudoers write + visudo validate + service restart.
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}✗${RESET} This script must be run as root."
    echo "    Try:  sudo bash $0"
    echo "    Or:   curl -fsSL <url> | sudo bash"
    exit 1
fi

INSTALL_DIR="${AERODROME_INSTALL_DIR:-/opt/aerodrome}"
SUDOERS_FILE="/etc/sudoers.d/aerodrome"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║   Aerodrome — Sudoers Recovery               ║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Install dir:   ${INSTALL_DIR}"
echo "  Sudoers file:  ${SUDOERS_FILE}"
echo ""

if [ ! -d "$INSTALL_DIR" ]; then
    echo -e "${RED}✗${RESET} Install directory not found: ${INSTALL_DIR}"
    echo "    If your install is at a different path, set"
    echo "    AERODROME_INSTALL_DIR before running this script."
    exit 1
fi

# Detect aerodrome install user from the install dir's owner. This is more
# reliable than $SUDO_USER (which is whoever ran THIS script) and matches
# what install.sh would have written.
USER="$(stat -c %U "$INSTALL_DIR" 2>/dev/null || echo "")"
if [ -z "$USER" ] || [ "$USER" = "root" ]; then
    echo -e "${RED}✗${RESET} Could not detect Aerodrome install user from"
    echo "    ${INSTALL_DIR} ownership (got: '${USER}')."
    echo ""
    echo "    The install dir should be owned by the user the aerodrome"
    echo "    service runs as (typically the user who ran install.sh)."
    echo "    Fix ownership with:"
    echo "      sudo chown -R <user>:<user> ${INSTALL_DIR}"
    echo "    then re-run this recovery script."
    exit 1
fi
echo "  Detected user: ${USER}"
echo ""

# Compose the new sudoers content into a temp file, then validate via
# visudo BEFORE installing. visudo -cf will reject any syntax error
# (missing braces, ambiguous commands, etc.) so a broken file never
# reaches /etc/sudoers.d/ where it'd break sudo for the whole system.
TEMP_FILE="$(mktemp /tmp/aerodrome-sudoers.XXXXXX)"
trap 'rm -f "$TEMP_FILE"' EXIT

# MUST stay in sync with install.sh's sudoers heredoc (line ~492). When
# install.sh changes the sudoers content, this script's content must be
# updated to match AND the SUDOERS_VERSION header must be bumped.
cat > "$TEMP_FILE" << SUDOEOF
# SUDOERS_VERSION: 5
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

# v3.1.0: synthetic-feeder service lifecycle.
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop aerodrome-synthetic-feeder, /usr/bin/systemctl stop aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl start aerodrome-synthetic-feeder, /usr/bin/systemctl start aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart aerodrome-synthetic-feeder, /usr/bin/systemctl restart aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl enable aerodrome-synthetic-feeder, /usr/bin/systemctl enable aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/systemctl disable aerodrome-synthetic-feeder, /usr/bin/systemctl disable aerodrome-synthetic-feeder
${USER} ALL=(ALL) NOPASSWD: /bin/rm -f /etc/systemd/system/aerodrome-synthetic-feeder.service
# v3.4.42: write access to the feeder unit file, scoped to that exact path.
${USER} ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/systemd/system/aerodrome-synthetic-feeder.service
SUDOEOF

# Validate via visudo BEFORE installing.
if ! visudo -cf "$TEMP_FILE" >/dev/null 2>&1; then
    echo -e "${RED}✗${RESET} Sudoers validation failed:"
    visudo -cf "$TEMP_FILE" || true
    echo ""
    echo "    /etc/sudoers.d/aerodrome was NOT modified."
    exit 1
fi

# Atomic install with correct perms (0440 is the only mode sudoers accepts).
install -m 0440 -o root -g root "$TEMP_FILE" "$SUDOERS_FILE"
echo -e "  ${GREEN}✓${RESET} Sudoers refreshed to SUDOERS_VERSION 5"

# Best-effort restart of aerodrome so the running process can use the
# new grants immediately. Skipped if the service isn't installed (e.g.
# you're running this on a host where aerodrome is being staged but
# not yet servicified).
# `systemctl cat` returns 0 iff the unit exists — more reliable across systemd
# versions than `list-unit-files | grep`, which false-negatived on older Pi OS
# (the unit existed but didn't match, so a refresh printed "not installed").
if systemctl cat aerodrome.service >/dev/null 2>&1; then
    if systemctl restart aerodrome 2>/dev/null; then
        echo -e "  ${GREEN}✓${RESET} aerodrome service restarted"
    else
        echo -e "  ${YELLOW}!${RESET} aerodrome service didn't restart; restart manually if needed:"
        echo "      sudo systemctl restart aerodrome"
    fi
else
    echo -e "  ${YELLOW}·${RESET} aerodrome.service not installed; skipping restart"
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Recovery complete.${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo "  If you were unstuck on the Updates page, return to it now"
echo "  and click 'I ran the command — re-check'. The gate should lift."
echo ""
