#!/bin/bash
# Version: 3.0.9
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

# Remove sudoers rule for in-UI restart button (if present)
if [ -f /etc/sudoers.d/aerodrome ]; then
    sudo rm -f /etc/sudoers.d/aerodrome
    echo -e "  ${GREEN}✓${RESET} Sudoers rule removed"
fi

# --- Step 2: Remove virtual environment ---
echo -e "${CYAN}[2/5]${RESET} Removing Python virtual environment..."
if [ -d "${INSTALL_DIR}/venv" ]; then
    rm -rf "${INSTALL_DIR}/venv"
    echo -e "  ${GREEN}✓${RESET} venv/ removed"
else
    echo -e "  ${YELLOW}·${RESET} No venv found"
fi

# --- Step 3: Remove PID file ---
echo -e "${CYAN}[3/5]${RESET} Removing runtime files..."
rm -f "${INSTALL_DIR}/.tracker.pid"
find "${INSTALL_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "${INSTALL_DIR}" -name "*.pyc" -delete 2>/dev/null || true
echo -e "  ${GREEN}✓${RESET} PID file and Python caches removed"

# --- Step 4: Handle data files (prompt or flag-driven) ---
echo -e "${CYAN}[4/5]${RESET} Handling data files..."

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

# Don't remove the directory if we're running from inside it
# (We can remove it, but it'll be weird to leave the user sitting in a deleted cwd)
if [ "$PURGE" = true ]; then
    echo -e "  ${YELLOW}!${RESET} To remove the project directory itself, run from elsewhere:"
    echo "      cd / && rm -rf ${INSTALL_DIR}"
elif [ "$KEEP" = true ]; then
    echo -e "  ${YELLOW}·${RESET} Project directory kept at ${INSTALL_DIR}"
else
    read -p "  Show command to remove the project directory itself? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo ""
        echo "  Run from outside the directory:"
        echo -e "      ${CYAN}cd / && rm -rf ${INSTALL_DIR}${RESET}"
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
echo "  NOT removed (intentional):"
echo "    - System packages (python3, pip, python3-venv)"
echo "      Remove manually if desired: sudo apt remove python3-venv"
echo ""
echo "  If you ever want to reinstall, just run ./install.sh again."
echo ""
