#!/bin/bash
# Version: 3.4.119
# =============================================================================
# Aerodrome — Uninstall Script
# =============================================================================
#
# Removes Aerodrome from the system, reversing what install.sh set up.
#
# Usage:
#   ./uninstall.sh           # Interactive — prompts before deleting data
#   ./uninstall.sh --purge   # Remove everything (data included) without prompts
#   ./uninstall.sh --keep    # Keep all data files, no prompts (service only)
#
# What gets removed (always):
#   - systemd service (stopped, disabled, unit file deleted)
#   - Python virtual environment (./venv)
#   - PID file (.tracker.pid)
#
# What you're prompted about:
#   - Database file (aircraft_history.db)
#   - Logs directory (logs/)
#   - Config file (config.yaml)
#   - Local ntfy server (if Aerodrome installed one via the Notifications
#     tab — detected via /var/lib/ntfy/aerodrome-installed stamp)
#   - Cached ntfy messages (/var/lib/ntfy/cache.db) — only prompted if
#     you chose to uninstall the local ntfy server
#   - The project directory itself
#
# What is NEVER removed:
#   - System packages (python3, pip, venv) — might be used by other apps
#   - Ubuntu itself :)
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

PURGE=false
KEEP=false
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=true ;;
        --keep)  KEEP=true ;;
        --help|-h)
            head -30 "$0" | tail -27 | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $arg${RESET}"
            echo "Run with --help for usage"
            exit 1
            ;;
    esac
done

if [ "$PURGE" = true ] && [ "$KEEP" = true ]; then
    echo -e "${RED}Cannot use --purge and --keep together${RESET}"
    exit 1
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║         Aerodrome — Uninstall                ║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Install dir:  ${INSTALL_DIR}"
echo "  Service name: ${SERVICE_NAME}"
echo ""

# --- Confirm ---
if [ "$PURGE" = false ] && [ "$KEEP" = false ]; then
    read -p "Are you sure you want to uninstall Aerodrome? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
fi

# --- Step 1: Stop and disable systemd service ---
echo -e "${CYAN}[1/6]${RESET} Stopping and removing systemd service..."
if sudo systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    sudo systemctl stop ${SERVICE_NAME} 2>/dev/null || true
    sudo systemctl disable ${SERVICE_NAME} 2>/dev/null || true
    sudo rm -f /etc/systemd/system/${SERVICE_NAME}.service
    sudo systemctl daemon-reload
    sudo systemctl reset-failed 2>/dev/null || true
    echo -e "  ${GREEN}✓${RESET} Service stopped, disabled, and removed"
else
    echo -e "  ${YELLOW}·${RESET} No systemd service found (already removed or never installed)"
fi

# v3.1.0: also remove the synthetic-feeder service if present.
# Demo-mode installs include this as a sibling unit; non-demo installs
# won't have it, so the detection-then-remove pattern keeps the
# output clean either way. Same operations as the main service:
# stop, disable, remove unit file, daemon-reload.
FEEDER_SERVICE_NAME="aerodrome-synthetic-feeder"
if sudo systemctl list-unit-files 2>/dev/null | grep -q "^${FEEDER_SERVICE_NAME}.service"; then
    sudo systemctl stop ${FEEDER_SERVICE_NAME} 2>/dev/null || true
    sudo systemctl disable ${FEEDER_SERVICE_NAME} 2>/dev/null || true
    sudo rm -f /etc/systemd/system/${FEEDER_SERVICE_NAME}.service
    sudo systemctl daemon-reload
    sudo systemctl reset-failed 2>/dev/null || true
    echo -e "  ${GREEN}✓${RESET} Synthetic-feeder service stopped, disabled, and removed (demo mode)"
fi

# Remove sudoers rule for in-UI restart button (if present)
if [ -f /etc/sudoers.d/aerodrome ]; then
    sudo rm -f /etc/sudoers.d/aerodrome
    echo -e "  ${GREEN}✓${RESET} Sudoers rule removed"
fi

# --- Step 2: Remove virtual environment ---
echo -e "${CYAN}[2/6]${RESET} Removing Python virtual environment..."
if [ -d "${INSTALL_DIR}/venv" ]; then
    rm -rf "${INSTALL_DIR}/venv"
    echo -e "  ${GREEN}✓${RESET} venv/ removed"
else
    echo -e "  ${YELLOW}·${RESET} No venv found"
fi

# --- Step 3: Remove PID file ---
echo -e "${CYAN}[3/6]${RESET} Removing runtime files..."
rm -f "${INSTALL_DIR}/.tracker.pid"
find "${INSTALL_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "${INSTALL_DIR}" -name "*.pyc" -delete 2>/dev/null || true
echo -e "  ${GREEN}✓${RESET} PID file and Python caches removed"

# --- Step 4: Handle data files (prompt or flag-driven) ---
echo -e "${CYAN}[4/6]${RESET} Handling data files..."

remove_or_keep() {
    local path="$1"
    local description="$2"

    if [ ! -e "$path" ]; then
        echo -e "  ${YELLOW}·${RESET} ${description}: not present"
        return
    fi

    if [ "$PURGE" = true ]; then
        rm -rf "$path"
        echo -e "  ${GREEN}✓${RESET} ${description}: removed (--purge)"
        return
    fi

    if [ "$KEEP" = true ]; then
        echo -e "  ${YELLOW}·${RESET} ${description}: kept (--keep)"
        return
    fi

    # Interactive prompt
    local size=""
    if [ -f "$path" ]; then
        size=" ($(du -h "$path" | cut -f1))"
    elif [ -d "$path" ]; then
        size=" ($(du -sh "$path" 2>/dev/null | cut -f1))"
    fi
    read -p "  Delete ${description}${size}? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$path"
        echo -e "    ${GREEN}✓${RESET} Removed"
    else
        echo -e "    ${YELLOW}·${RESET} Kept"
    fi
}

# Read the db file path from config if present
DB_FILE="${INSTALL_DIR}/aircraft_history.db"
if [ -f "${INSTALL_DIR}/config.yaml" ]; then
    CFG_DB=$(grep -E "^\s*db_file:" "${INSTALL_DIR}/config.yaml" | head -1 | sed -E 's/.*db_file:\s*"?([^"]+)"?.*/\1/')
    if [ -n "$CFG_DB" ]; then
        # Resolve relative paths against install dir
        case "$CFG_DB" in
            /*) DB_FILE="$CFG_DB" ;;
            *)  DB_FILE="${INSTALL_DIR}/${CFG_DB}" ;;
        esac
    fi
fi

remove_or_keep "$DB_FILE" "Database (aircraft history)"
remove_or_keep "${DB_FILE}-wal" "Database WAL file"
remove_or_keep "${DB_FILE}-shm" "Database SHM file"
remove_or_keep "${INSTALL_DIR}/logs" "Logs directory"
remove_or_keep "${INSTALL_DIR}/config.yaml" "Config file (config.yaml)"

# --- Step 5: Remove local ntfy server (if Aerodrome installed one) ---
# v2.41.0: the Notifications tab's "Install local ntfy" creates an install
# stamped at /var/lib/ntfy/aerodrome-installed. If that stamp exists, this
# is an Aerodrome-managed ntfy install and we should remove it along with
# Aerodrome. External (user-installed) ntfy is never touched — the stamp
# is what distinguishes the two.
echo -e "${CYAN}[5/6]${RESET} Local ntfy server..."
NTFY_STAMP="/var/lib/ntfy/aerodrome-installed"
if [ -f "$NTFY_STAMP" ]; then
    NTFY_VER=$(cat "$NTFY_STAMP" 2>/dev/null | tr -d '\n' || echo "?")

    should_remove_ntfy=false
    should_purge_ntfy_data=false
    if [ "$PURGE" = true ]; then
        should_remove_ntfy=true
        should_purge_ntfy_data=true
    elif [ "$KEEP" = true ]; then
        echo -e "  ${YELLOW}·${RESET} Local ntfy ${NTFY_VER} kept (--keep)"
    else
        # Interactive prompt
        read -p "  Remove Aerodrome-installed ntfy server (version ${NTFY_VER})? [y/N] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            should_remove_ntfy=true
            # Second prompt for the cache data — kept separate because some
            # users want to reinstall later and keep their topic history.
            ntfy_cache_size=""
            if [ -f /var/cache/ntfy/cache.db ]; then
                ntfy_cache_size=" ($(sudo du -sh /var/cache/ntfy 2>/dev/null | cut -f1))"
            fi
            read -p "    Also delete cached messages${ntfy_cache_size}? [y/N] " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                should_purge_ntfy_data=true
            fi
        else
            echo -e "    ${YELLOW}·${RESET} Kept"
        fi
    fi

    if [ "$should_remove_ntfy" = true ]; then
        # Stop + disable the service. Best-effort; ignore errors if the
        # unit somehow got removed separately.
        sudo systemctl stop ntfy 2>/dev/null || true
        sudo systemctl disable ntfy 2>/dev/null || true
        # Remove files
        for p in /usr/local/bin/ntfy /etc/ntfy/server.yml \
                 /etc/systemd/system/ntfy.service \
                 /var/lib/ntfy/aerodrome-installed; do
            if [ -e "$p" ]; then
                sudo rm -f "$p"
            fi
        done
        sudo systemctl daemon-reload 2>/dev/null || true
        echo -e "    ${GREEN}✓${RESET} Local ntfy removed"

        # Purge cache if requested
        if [ "$should_purge_ntfy_data" = true ]; then
            for p in /var/cache/ntfy/cache.db \
                     /var/cache/ntfy/cache.db-wal \
                     /var/cache/ntfy/cache.db-shm; do
                sudo rm -f "$p" 2>/dev/null || true
            done
            sudo rm -rf /var/cache/ntfy/attachments 2>/dev/null || true
            echo -e "    ${GREEN}✓${RESET} Cached messages removed"
        else
            if [ -f /var/cache/ntfy/cache.db ]; then
                echo -e "    ${YELLOW}·${RESET} Cached messages kept at /var/cache/ntfy/"
            fi
        fi

        # Clean up now-empty directories (best-effort; won't delete if non-empty)
        for d in /etc/ntfy /var/cache/ntfy /var/lib/ntfy; do
            sudo rmdir --ignore-fail-on-non-empty "$d" 2>/dev/null || true
        done
    fi
else
    echo -e "  ${YELLOW}·${RESET} No Aerodrome-managed ntfy install found"
fi

# --- Step 6: Offer to remove the install directory itself ---
echo -e "${CYAN}[6/6]${RESET} Project directory..."

# v3.3.0: if INSTALL_DIR is outside the user's home, the user can't
# remove its parent-directory entry without root — so emit the
# correct sudo'd command. Inside the home, no sudo needed.
case "$INSTALL_DIR" in
    "$HOME"/*|"$HOME") _rm_cmd="rm -rf ${INSTALL_DIR}" ;;
    *)                 _rm_cmd="sudo rm -rf ${INSTALL_DIR}" ;;
esac

# Don't remove the directory if we're running from inside it
# (We can remove it, but it'll be weird to leave the user sitting in a deleted cwd)
if [ "$PURGE" = true ]; then
    echo -e "  ${YELLOW}!${RESET} To remove the project directory itself, run from elsewhere:"
    echo "      cd / && ${_rm_cmd}"
elif [ "$KEEP" = true ]; then
    echo -e "  ${YELLOW}·${RESET} Project directory kept at ${INSTALL_DIR}"
else
    read -p "  Show command to remove the project directory itself? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo ""
        echo "  Run from outside the directory:"
        echo -e "      ${CYAN}cd / && ${_rm_cmd}${RESET}"
    fi
fi

# --- Done ---
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Uninstall complete!${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo "  Aerodrome has been removed from this system."
echo ""

# v3.2.0: family-aware "system packages weren't removed" hint. Most users
# will leave python3/pip alone — they're useful for other things — so this
# is purely informational. Detect the package manager and emit the right
# removal command for the user's distro.
_uninstall_os_id=""
if [ -r /etc/os-release ]; then
    eval "$(
        . /etc/os-release
        printf '_uninstall_os_id=%q\n' "${ID:-}"
    )"
fi
case "$_uninstall_os_id" in
    debian|ubuntu|raspbian|linuxmint|pop|elementary|neon|kali|parrot)
        _uninstall_pkg_remove="sudo apt-get remove python3-venv"
        _uninstall_pkg_list="python3, pip, python3-venv" ;;
    fedora|rhel|centos|rocky|almalinux|amzn|ol)
        _uninstall_pkg_remove="sudo dnf remove python3 python3-pip"
        _uninstall_pkg_list="python3, python3-pip" ;;
    arch|manjaro|endeavouros|garuda|artix|cachyos)
        _uninstall_pkg_remove="sudo pacman -R python python-pip"
        _uninstall_pkg_list="python, python-pip" ;;
    opensuse*|sles|sled)
        _uninstall_pkg_remove="sudo zypper remove python3 python3-pip"
        _uninstall_pkg_list="python3, python3-pip" ;;
    *)
        _uninstall_pkg_remove="(use your distro's package manager)"
        _uninstall_pkg_list="python3 and its modules" ;;
esac

echo "  NOT removed (intentional):"
echo "    - System packages ($_uninstall_pkg_list)"
echo "      Remove manually if desired: $_uninstall_pkg_remove"
echo ""

# v3.3.0: if firewalld is active and port 8000 is open, offer to close
# it. The bootstrap opens this port on Fedora/openSUSE Tumbleweed
# during install; pairing the close at uninstall keeps the firewall
# state symmetric. Skip silently on systems without firewalld
# (Debian/Ubuntu/Arch in their default state).
if command -v firewall-cmd >/dev/null 2>&1 && \
        systemctl is-active firewalld --quiet 2>/dev/null; then
    if sudo firewall-cmd --list-ports 2>/dev/null | grep -qw "8000/tcp"; then
        echo "  firewalld has port 8000/tcp open (opened during Aerodrome install)."
        read -p "  Close port 8000 now? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            if sudo firewall-cmd --remove-port=8000/tcp --permanent >/dev/null 2>&1 \
                    && sudo firewall-cmd --reload >/dev/null 2>&1; then
                echo -e "  ${GREEN}✓${RESET} Closed port 8000/tcp on the public zone"
            else
                echo -e "  ${YELLOW}!${RESET} Could not close port 8000 — run manually:"
                echo "      sudo firewall-cmd --remove-port=8000/tcp --permanent && sudo firewall-cmd --reload"
            fi
        else
            echo "  Port 8000 left open. To close later:"
            echo "      sudo firewall-cmd --remove-port=8000/tcp --permanent && sudo firewall-cmd --reload"
        fi
        echo ""
    fi
fi

echo "  If you ever want to reinstall, just run ./install.sh again."
echo ""
