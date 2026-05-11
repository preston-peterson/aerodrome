#!/bin/bash
# Version: 3.0.4
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
RESET='\033[0m'

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="aerodrome"

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
echo ""

echo -e "${CYAN}[1/5]${RESET} Installing system packages..."
sudo apt update -qq
sudo apt install -y -qq python3 python3-pip python3-venv curl > /dev/null 2>&1
echo -e "  ${GREEN}✓${RESET} Python3, pip, venv installed"

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
# SUDOERS_VERSION: 3
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

echo -e "${CYAN}[5/5]${RESET} Starting the tracker..."
sudo systemctl start ${SERVICE_NAME}
sleep 3

if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "  ${GREEN}✓${RESET} Tracker is running!"
else
    echo -e "  ${RED}✗${RESET} Failed to start. Check: sudo journalctl -u ${SERVICE_NAME} -n 50"
    exit 1
fi

SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Install complete!${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo "  Web UI:  http://${SERVER_IP}:8000"
echo ""
echo "  Commands:"
echo "    sudo systemctl status ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    sudo systemctl stop ${SERVICE_NAME}"
echo "    sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  Config:  ${INSTALL_DIR}/config.yaml"
echo "  Logs:    ${INSTALL_DIR}/logs/tracker.log"
echo ""
