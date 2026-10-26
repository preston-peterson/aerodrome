#!/bin/bash
# Version: 3.4.147
# =============================================================================
# Aerodrome — Uninstall Script
# =============================================================================
#
# Removes Aerodrome from the system, reversing what install.sh set up.
#
# Usage:
#   ./uninstall.sh           # Interactive — one question per category of data
#   ./uninstall.sh --purge   # Remove everything (data included) without prompts
#   ./uninstall.sh --keep    # Keep all data files, no prompts (service only)
#
# What gets removed (always):
#   - systemd service(s) (stopped, disabled, unit files deleted) and the
#     sudoers rule
#   - Python virtual environment (./venv)
#   - PID file (.tracker.pid) and staged updates (./update)
#
# What you're asked about (--purge removes all of it, --keep none of it):
#   - Database (data.db_file from config.yaml, default aircraft_history.db),
#     its -wal/-shm files, its .pre-restore safety copies, and the
#     daily-summary state file kept next to it
#   - Backups & update snapshots (.backups/: pre-update snapshots, database
#     snapshots, and backup zips you made from the web UI — copy any backup
#     you want to keep out of .backups/ first)
#   - Logs (logging.dir from config.yaml, default logs/)
#   - Config file (config.yaml) and its backups (config.yaml.bak.*,
#     ntfy-server.yml.from-backup.*)
#   - Local ntfy server (if Aerodrome installed one via the Notifications
#     tab — detected via /var/lib/ntfy/aerodrome-installed stamp) and its
#     cached messages
#   - Port 8000/tcp in firewalld — closed only if Aerodrome's installer
#     recorded opening it (.aerodrome-firewalld-port); otherwise the close
#     command is printed for you to run
#   - The project directory itself (the command to remove it is printed)
#
# Where it runs: only inside the install it removes (a symlink to it works on
# the real install). It stops before changing anything (exit 2) when its
# folder isn't an Aerodrome install (no VERSION + main.py: a copy kept
# somewhere else, or the script piped into bash), when it is the copy inside
# a staged update (<install>/update/…; it names the real uninstall.sh), or
# when the install sits directly in a system or home folder (remove that one
# by hand).
#
# Deletion rules: whole folders are removed only INSIDE the install folder.
# A database or log folder that config.yaml puts outside it is handled file
# by file, by name, never as a whole folder: a log folder elsewhere loses
# only Aerodrome's own tracker.log*, and a database elsewhere is removed only
# if it really is an SQLite database. System and home folders are never
# touched, whatever config.yaml says.
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

# The install folder is the REAL folder of this file: a symlinked entry point
# acts on the install it belongs to, not on the folder the link sits in.
_self="$(readlink -f -- "$0" 2>/dev/null || printf '%s\n' "$0")"
INSTALL_DIR="$(cd "$(dirname "$_self")" && pwd)"
SERVICE_NAME="aerodrome"

PURGE=false
KEEP=false
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=true ;;
        --keep)  KEEP=true ;;
        --help|-h)
            sed -n '6,/^# =====/{/^# =====/d;s/^# \{0,1\}//;p;}' "$0"
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

# Paths are normalised with realpath -m before every check, so /home/u/../u,
# trailing slashes and symlinks can't slip past them. Fails closed: a path
# that can't be normalised counts as outside the install AND protected, so
# nothing is ever deleted on a guess.
_real() { realpath -m -- "$1" 2>/dev/null; }
_system_or_home_dir() {   # /, any top-level dir, $HOME or an ancestor of it
    local r h
    r="$(_real "${1:-/}")" && h="$(_real "${HOME:-/}")" || return 0
    [ "$r" = / ] || [[ "$r" != /*/* ]] || [ "$r" = "$h" ] || [[ "$h" == "$r"/* ]]
}

# --- Is this an Aerodrome install? ---
# Everything below deletes relative to this script's own folder, so it
# refuses — before anything is asked or changed — to run anywhere else.
if ! _real / >/dev/null; then
    echo -e "${RED}✗${RESET} This system's realpath can't normalise paths (realpath -m) — nothing was changed." >&2
    echo "    uninstall.sh needs it (GNU or uutils coreutils) to keep its deletions inside the install folder." >&2
    exit 2
fi
if [ ! -f "$INSTALL_DIR/VERSION" ] || [ ! -f "$INSTALL_DIR/main.py" ]; then
    echo -e "${RED}✗${RESET} ${INSTALL_DIR} is not an Aerodrome install folder — nothing was changed." >&2
    echo "    Run the uninstall.sh inside your install folder (for example /opt/aerodrome/uninstall.sh)." >&2
    exit 2
fi
# A staged update's copy (<install>/update/, update/aerodrome/, …) belongs to
# the install above it: same walk as install.sh's staging check.
_p="$INSTALL_DIR"
while [ -n "$_p" ] && [ "$_p" != / ]; do
    _pp="$(dirname "$_p")"
    if [ "$(basename "$_p")" = update ] && [ -f "$_pp/VERSION" ] && [ -f "$_pp/main.py" ]; then
        echo -e "${RED}✗${RESET} This copy is a staged update inside the install at ${_pp} — nothing was changed." >&2
        echo "    To uninstall Aerodrome, run: $(printf %q "$_pp/uninstall.sh")" >&2
        exit 2
    fi
    _p="$_pp"
done
# An install placed directly in a system or home folder (an old installer
# allowed that): its venv/, logs/, update/ and .backups/ could be anyone's.
if _system_or_home_dir "$INSTALL_DIR"; then
    echo -e "${RED}✗${RESET} Aerodrome sits directly in ${INSTALL_DIR}, a system or home folder — nothing was changed." >&2
    echo "    Folders such as venv/, logs/ and .backups/ there may not be Aerodrome's, so remove it by hand:" >&2
    echo "      sudo systemctl disable --now aerodrome    (on a demo install: aerodrome-synthetic-feeder too)" >&2
    echo "      sudo rm -f /etc/systemd/system/aerodrome.service /etc/sudoers.d/aerodrome" >&2
    echo "      sudo systemctl daemon-reload" >&2
    echo "    then delete Aerodrome's own files from ${INSTALL_DIR} yourself." >&2
    exit 2
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
# (`|| REPLY=""`: with no terminal, read fails — treat that as "no", never
# as a reason for set -e to stop half-way.)
if [ "$PURGE" = false ] && [ "$KEEP" = false ]; then
    read -p "Are you sure you want to uninstall Aerodrome? [y/N] " -n 1 -r || REPLY=""
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
fi

# --- Resolve where this install keeps its data ---
# Runs BEFORE the venv is removed so the venv's PyYAML — the parser the app
# itself uses — reads config.yaml. A line-grep got trailing comments, single
# quotes and CRLF wrong, and could pick up a path named inside a comment.
# Relative paths resolve against the install dir, as server.py/main.py do.
# Falls back to a section-aware awk reader when no Python+PyYAML is usable.
DB_FILE="aircraft_history.db"
LOG_DIR="logs"
CFG_FILE="${INSTALL_DIR}/config.yaml"
if [ -f "$CFG_FILE" ]; then
    _py=""
    for _cand in "${INSTALL_DIR}/venv/bin/python3" python3; do
        if "$_cand" -I -c 'import yaml' >/dev/null 2>&1; then _py="$_cand"; break; fi
    done
    _resolved=""
    if [ -n "$_py" ]; then
        _resolved="$("$_py" -I - "$CFG_FILE" 2>/dev/null <<'PYEOF' || true
import sys, yaml
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        c = yaml.safe_load(f) or {}
except Exception:
    sys.exit(1)
d = c.get("data") if isinstance(c.get("data"), dict) else {}
g = c.get("logging") if isinstance(c.get("logging"), dict) else {}
print(str(d.get("db_file") or "aircraft_history.db"))
print(str(g.get("dir") or "logs"))
PYEOF
)"
    fi
    if [ -n "$_resolved" ]; then
        DB_FILE="$(printf '%s\n' "$_resolved" | sed -n 1p)"
        LOG_DIR="$(printf '%s\n' "$_resolved" | sed -n 2p)"
    else
        # $1 = top-level section, $2 = key. Strips CR, quotes, trailing comment.
        _cfg_get() {
            awk -v sect="$1" -v key="$2" -v q1="'" -v q2='"' '
                { sub(/\r$/, "") }
                /^[^[:space:]#]/ { cur = $0; sub(/:.*/, "", cur) }
                !done && cur == sect && $0 ~ ("^[[:space:]]+" key ":") {
                    v = $0; sub("^[[:space:]]+" key ":[[:space:]]*", "", v)
                    c = substr(v, 1, 1)
                    if (c == q1 || c == q2) {
                        v = substr(v, 2); i = index(v, c); if (i) v = substr(v, 1, i - 1)
                    } else {
                        sub(/[[:space:]]+#.*$/, "", v); sub(/[[:space:]]+$/, "", v)
                    }
                    print v; done = 1
                }' "$CFG_FILE"
        }
        _v="$(_cfg_get data db_file)";  [ -n "$_v" ] && DB_FILE="$_v"
        _v="$(_cfg_get logging dir)";   [ -n "$_v" ] && LOG_DIR="$_v"
    fi
fi
case "$DB_FILE" in /*) ;; *) DB_FILE="${INSTALL_DIR}/${DB_FILE}" ;; esac
case "$LOG_DIR" in /*) ;; *) LOG_DIR="${INSTALL_DIR}/${LOG_DIR}" ;; esac
DATA_DIR="$(dirname "$DB_FILE")"

# Deletion rules: `rm -rf` only ever runs on a path whose REAL location is
# strictly inside the install dir. Anything config.yaml points OUTSIDE it (a
# database or log folder on another disk) is removed file by file, by name,
# never recursively, and only after _protected_path says it is not a
# system/home location. _real (above) fails closed: a path it can't normalise
# is never "inside" and always "protected".
INSTALL_REAL="$(_real "$INSTALL_DIR")" || INSTALL_REAL=""
_inside_install() {
    local r; r="$(_real "$1")" || return 1
    [ -n "$INSTALL_REAL" ] && [ "$r" != "$INSTALL_REAL" ] && [[ "$r" == "$INSTALL_REAL"/* ]]
}
_protected_path() {       # normalised: never delete these, whatever config.yaml says
    local r; r="$(_real "${1:-/}")" || return 0
    [ -n "$INSTALL_REAL" ] || return 0
    _system_or_home_dir "$r" && return 0
    { [ "$r" = "$INSTALL_REAL" ] || [[ "$INSTALL_REAL" == "$r"/* ]]; } && return 0  # install dir or an ancestor
    return 1
}
# Remove what remove_or_keep decided to delete, under the rules above.
_remove_paths() {
    local p
    for p in "$@"; do
        if _inside_install "$p"; then
            rm -rf -- "$p"
        elif [ -d "$p" ] && [ ! -L "$p" ]; then
            echo -e "  ${YELLOW}!${RESET} not removing ${p}: a directory outside ${INSTALL_DIR}"
        else
            rm -f -- "$p"
        fi
    done
}
# Is unit $1 known to systemd? Capture, then match (no early-exit reader
# after a pipe, so this stays correct if pipefail is ever turned on).
_unit_known() {
    local units
    units="$(sudo systemctl list-unit-files 2>/dev/null || true)"
    [[ $'\n'"$units" == *$'\n'"$1"[[:space:]]* ]]
}

# --- Step 1: Stop and disable systemd service ---
echo -e "${CYAN}[1/6]${RESET} Stopping and removing systemd service..."
if _unit_known "${SERVICE_NAME}.service"; then
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
if _unit_known "${FEEDER_SERVICE_NAME}.service"; then
    sudo systemctl stop ${FEEDER_SERVICE_NAME} 2>/dev/null || true
    sudo systemctl disable ${FEEDER_SERVICE_NAME} 2>/dev/null || true
    sudo rm -f /etc/systemd/system/${FEEDER_SERVICE_NAME}.service
    sudo systemctl daemon-reload
    sudo systemctl reset-failed 2>/dev/null || true
    echo -e "  ${GREEN}✓${RESET} Synthetic-feeder service stopped, disabled, and removed (demo mode)"
fi

# Remove the sudoers rule (if present). Checked through sudo: on some
# distros /etc/sudoers.d isn't searchable by a normal user, so a plain
# [ -f ] would never see the file.
if sudo test -e /etc/sudoers.d/aerodrome; then
    sudo rm -f /etc/sudoers.d/aerodrome
    echo -e "  ${GREEN}✓${RESET} Sudoers rule removed"
fi
# Temp files of an install.sh run that was killed mid-write (sudo ignores them).
sudo find /etc/sudoers.d -maxdepth 1 -type f -name '.aerodrome.*' -delete 2>/dev/null || true

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
# Staged updates (release zips + extracted trees from the Updates page).
# Not user data — the app recreates the folder on demand.
rm -rf "${INSTALL_DIR}/update"
# Python caches only where Aerodrome's own code lives (the install folder
# itself, scripts/ and tools/), never a sweep of everything under it.
rm -rf -- "${INSTALL_DIR}/__pycache__"
rm -f -- "${INSTALL_DIR}"/*.pyc
for _d in "${INSTALL_DIR}/scripts" "${INSTALL_DIR}/tools"; do
    if [ -d "$_d" ] && [ ! -L "$_d" ]; then
        find "$_d" -name "__pycache__" -type d -prune -exec rm -rf -- {} + 2>/dev/null || true
        find "$_d" -name "*.pyc" -type f -delete 2>/dev/null || true
    fi
done
echo -e "  ${GREEN}✓${RESET} PID file, staged updates and Python caches removed"

# --- Step 4: Handle data files (prompt or flag-driven) ---
echo -e "${CYAN}[4/6]${RESET} Handling data files..."

# remove_or_keep "<description>" <path>... — one decision per category.
# Missing paths are skipped; callers expand globs with nullglob on.
# Returns 0 when it removed them, 1 when kept, absent or refused.
remove_or_keep() {
    local description="$1"; shift
    local present=() p
    for p in "$@"; do
        if _protected_path "$p"; then
            echo -e "  ${RED}!${RESET} ${description}: refusing to delete ${p} (resolved from config.yaml) — check data.db_file / logging.dir"
            return 1
        fi
        if [ -e "$p" ] || [ -L "$p" ]; then present+=("$p"); fi
    done

    if [ ${#present[@]} -eq 0 ]; then
        echo -e "  ${YELLOW}·${RESET} ${description}: not present"
        return 1
    fi

    if [ "$PURGE" = true ]; then
        _remove_paths "${present[@]}"
        echo -e "  ${GREEN}✓${RESET} ${description}: removed (--purge)"
        return 0
    fi

    if [ "$KEEP" = true ]; then
        echo -e "  ${YELLOW}·${RESET} ${description}: kept (--keep)"
        return 1
    fi

    # Interactive prompt: name the path when there's one, so a surprising
    # resolution is visible before anything is deleted.
    local size what
    size="$(du -shc -- "${present[@]}" 2>/dev/null | tail -1 | cut -f1)"
    what="${present[0]}"
    [ ${#present[@]} -gt 1 ] && what="${present[0]} + $(( ${#present[@]} - 1 )) more"
    read -p "  Delete ${description} — ${what} (${size})? [y/N] " -n 1 -r || REPLY=""
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        _remove_paths "${present[@]}"
        echo -e "    ${GREEN}✓${RESET} Removed"
        return 0
    fi
    echo -e "    ${YELLOW}·${RESET} Kept"
    return 1
}

shopt -s nullglob
if [ -d "$DB_FILE" ] && [ ! -L "$DB_FILE" ]; then
    echo -e "  ${RED}!${RESET} Database: ${DB_FILE} is a directory, not a database file — skipped (check data.db_file in config.yaml)"
elif [ -e "$DB_FILE" ] && ! _inside_install "$DB_FILE" \
        && [ "$(head -c 15 -- "$DB_FILE" 2>/dev/null)" != "SQLite format 3" ]; then
    echo -e "  ${RED}!${RESET} Database: ${DB_FILE} is outside ${INSTALL_DIR} and is not a SQLite database — left in place (check data.db_file in config.yaml)"
else
    remove_or_keep "Database (aircraft history)" \
        "$DB_FILE" "${DB_FILE}-wal" "${DB_FILE}-shm" \
        "${DB_FILE}".bak.*.pre-restore \
        "${DATA_DIR}/.daily-summary-state.json" || true
fi
remove_or_keep "Backups & update snapshots" "${INSTALL_DIR}/.backups" || true
if _inside_install "$LOG_DIR"; then
    remove_or_keep "Logs directory" "$LOG_DIR" || true
else
    # A log folder elsewhere may be shared (a USB/SSD mount, /var/log/…):
    # remove only the file Aerodrome writes there, then the folder if empty.
    if remove_or_keep "Logs (Aerodrome's files in ${LOG_DIR})" "$LOG_DIR"/tracker.log "$LOG_DIR"/tracker.log.*; then
        rmdir -- "$LOG_DIR" 2>/dev/null || true
    fi
fi
remove_or_keep "Config file and its backups" \
    "${INSTALL_DIR}/config.yaml" \
    "${INSTALL_DIR}"/config.yaml.bak.* \
    "${INSTALL_DIR}"/ntfy-server.yml.from-backup.* || true
shopt -u nullglob

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
        read -p "  Remove Aerodrome-installed ntfy server (version ${NTFY_VER})? [y/N] " -n 1 -r || REPLY=""
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            should_remove_ntfy=true
            # Second prompt for the cache data — kept separate because some
            # users want to reinstall later and keep their topic history.
            # server.yml (ntfy_installer.py) sets cache-file: /var/lib/ntfy/cache.db;
            # the size is what the purge below would delete.
            ntfy_cache_size=" ($(sudo du -shc /var/lib/ntfy/cache.db* 2>/dev/null | tail -1 | cut -f1))"
            read -p "    Also delete cached messages${ntfy_cache_size}? [y/N] " -n 1 -r || REPLY=""
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
        # The folder the app downloads the ntfy binary into (no messages).
        sudo rm -rf /var/cache/ntfy/staging 2>/dev/null || true
        echo -e "    ${GREEN}✓${RESET} Local ntfy removed"

        # Purge cache if requested. Exactly the files the app's "Also delete
        # cached messages" removes (ntfy_installer.ntfy_purge_argvs());
        # check_installers.py ntfy-cache and test_check_installers.py keep the
        # two in step. Nothing in /var/cache/ntfy: no Aerodrome release kept
        # messages there, and ntfy's own package does. (This step runs only
        # with the stamp; the app can also purge a cache left with no install.)
        if [ "$should_purge_ntfy_data" = true ]; then
            ntfy_purge_left=""
            for p in /var/lib/ntfy/cache.db \
                     /var/lib/ntfy/cache.db-wal \
                     /var/lib/ntfy/cache.db-shm; do
                sudo rm -f "$p" 2>/dev/null || ntfy_purge_left="${ntfy_purge_left} $p"
            done
            if [ -z "$ntfy_purge_left" ]; then
                echo -e "    ${GREEN}✓${RESET} Cached messages removed"
            else
                echo -e "    ${YELLOW}!${RESET} Cached messages not removed:${ntfy_purge_left}"
            fi
        else
            echo -e "    ${YELLOW}·${RESET} Cached messages (if any) kept at /var/lib/ntfy/"
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
# correct sudo'd command. Inside the home, no sudo needed. (An install that
# sits directly in a system or home folder was refused at the top.)
_q="$(printf %q "$INSTALL_DIR")"
case "$INSTALL_DIR" in
    "$HOME"/*) _rm_cmd="rm -rf ${_q}" ;;
    *)         _rm_cmd="sudo rm -rf ${_q}" ;;
esac
_rm_hint="cd / && ${_rm_cmd}"

# Don't remove the directory if we're running from inside it
# (We can remove it, but it'll be weird to leave the user sitting in a deleted cwd)
if [ "$PURGE" = true ]; then
    echo -e "  ${YELLOW}!${RESET} To remove the project directory itself, run from elsewhere:"
    echo "      ${_rm_hint}"
elif [ "$KEEP" = true ]; then
    echo -e "  ${YELLOW}·${RESET} Project directory kept at ${INSTALL_DIR}"
else
    read -p "  Show command to remove the project directory itself? [y/N] " -n 1 -r || REPLY=""
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo ""
        echo "  Run from outside the directory:"
        echo -e "      ${CYAN}${_rm_hint}${RESET}"
    fi
fi

# --- Done ---
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Uninstall complete!${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
if [ "$KEEP" = true ]; then
    echo "  The Aerodrome service has been removed. Your data, settings and the"
    echo "  program files were kept in ${INSTALL_DIR}."
else
    echo "  Aerodrome has been removed from this system."
fi
echo ""

# Port 8000/tcp in firewalld. The bootstrap opens it on firewalld distros
# (Fedora, openSUSE, RHEL family) and records that in
# .aerodrome-firewalld-port. Only a port Aerodrome recorded opening is
# closed — another service may need a port that was already open. --keep
# never closes it; either way the close command is printed when it stays.
FW_MARKER="${INSTALL_DIR}/.aerodrome-firewalld-port"
FW_CLOSE_CMD="sudo firewall-cmd --remove-port=8000/tcp --permanent && sudo firewall-cmd --reload"
if command -v firewall-cmd >/dev/null 2>&1 && \
        systemctl is-active firewalld --quiet 2>/dev/null; then
    _fw_ports="$(sudo firewall-cmd --list-ports 2>/dev/null || true)"
    if [[ " $_fw_ports " == *" 8000/tcp "* ]]; then
        if [ -f "$FW_MARKER" ]; then
            echo "  firewalld has port 8000/tcp open (Aerodrome's installer opened it)."
            if [ "$PURGE" = true ]; then
                REPLY=y
            elif [ "$KEEP" = true ]; then
                REPLY=n
            else
                read -p "  Close port 8000 now? [Y/n] " -n 1 -r || REPLY=""
                echo
                [ -n "$REPLY" ] || REPLY=y
            fi
        else
            echo "  firewalld has port 8000/tcp open, but Aerodrome didn't record opening it —"
            echo "  close it yourself if nothing else needs it."
            if [ "$PURGE" = true ] || [ "$KEEP" = true ]; then
                REPLY=n
            else
                read -p "  Close port 8000 now? [y/N] " -n 1 -r || REPLY=""
                echo
            fi
        fi
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            if sudo firewall-cmd --remove-port=8000/tcp --permanent >/dev/null 2>&1 \
                    && sudo firewall-cmd --reload >/dev/null 2>&1; then
                echo -e "  ${GREEN}✓${RESET} Closed port 8000/tcp on the public zone"
                rm -f "$FW_MARKER"
            else
                echo -e "  ${YELLOW}!${RESET} Could not close port 8000 — run manually:"
                echo "      ${FW_CLOSE_CMD}"
            fi
        else
            echo "  Port 8000 left open. To close it:"
            echo "      ${FW_CLOSE_CMD}"
        fi
        echo ""
    fi
fi
# The marker only describes this install; --purge removes it with the rest.
if [ "$PURGE" = true ]; then
    rm -f "$FW_MARKER"
fi

echo "  If you ever want to reinstall, just run ./install.sh again."
echo ""
