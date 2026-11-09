#!/bin/bash
# Version: 3.4.177
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
#   - Install Python 3, pip, venv and curl with the system package manager
#     (skipped when they are already present)
#   - Create a Python virtual environment in ./venv
#   - Install all Python dependencies
#   - Install a systemd service that runs as the current user (a re-run keeps
#     the account an existing service already runs as)
#   - Install Aerodrome's system permissions: its root-owned system helper
#     (/usr/local/libexec/aerodrome/aerodrome-helper) and the sudoers rule
#     that names it (checked with visudo before it goes live)
#   - Start the service
#
# Refresh ONLY the system permissions (the helper and the sudoers rule) of an
# existing install — no packages, venv, Aerodrome units or restart — then
# move a local notification server Aerodrome manages to its restricted setup:
#   sudo bash install.sh --privileges-only [--install-dir DIR]
# This mode is automatic when this file sits inside an install's update/
# staging folder, and scripts/recover-sudoers.sh hands off to it.
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
SUDOERS_FILE="/etc/sudoers.d/aerodrome"
HELPER_DIR="/usr/local/libexec/aerodrome"
HELPER="${HELPER_DIR}/aerodrome-helper"

# An install.sh inside an existing install's update/ staging directory (the
# in-app updater stages releases at <install>/update/, update/aerodrome/ or
# update/aerodrome-vX.Y.Z/) must never install from there: a systemd unit
# written from staging points at the staging dir and 203/EXECs the service
# (the venv lives in the real install root). Before v3.4.145 this refused
# with exit 2. Older Updates pages tell users to run exactly this file with
# sudo to refresh Aerodrome's permissions, so it now does that and nothing
# else: a PRIVILEGES-ONLY refresh for the real install (see below).
#
# Detection: walk the ancestors; an ancestor named `update` whose parent is a
# real install (VERSION + main.py) means we are staged.
_staging_root=""
_probe="$INSTALL_DIR"
while [ -n "$_probe" ] && [ "$_probe" != "/" ]; do
    _probe_parent="$(dirname "$_probe")"
    if [ "$(basename "$_probe")" = "update" ] \
            && [ -f "$_probe_parent/VERSION" ] \
            && [ -f "$_probe_parent/main.py" ]; then
        _staging_root="$_probe_parent"
        break
    fi
    _probe="$_probe_parent"
done
PRIV_ONLY=false
[ -n "$_staging_root" ] && PRIV_ONLY=true

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
# v3.4.40: feeder port is configurable. Default 8080 matches the historical
# bootstrap-written config. Demo installs detect conflicts on this port and
# fall back through a small candidate list (8080 → 8088 → 28080); explicit
# --feeder-port wins outright. On upgrade-style re-runs the existing port is
# preserved (read from the live unit file) so a user who is happily on a
# non-default port keeps it across releases.
FEEDER_PORT_REQUESTED=""
FEEDER_PORT_DEFAULTS=(8080 8088 28080)
INSTALL_DIR_ARG=""
SVC_OVERRIDE=""
ALLOW_DOWNGRADE=false
while [ $# -gt 0 ]; do
    case "$1" in
        --demo)             DEMO_MODE=true; shift ;;
        --home-lat)         HOME_LAT="$2"; shift 2 ;;
        --home-lon)         HOME_LON="$2"; shift 2 ;;
        --feeder-port)      FEEDER_PORT_REQUESTED="$2"; shift 2 ;;
        --privileges-only)  PRIV_ONLY=true; shift ;;
        --install-dir)
            [ $# -ge 2 ] && [ -n "$2" ] || { echo "--install-dir needs a directory" >&2; exit 2; }
            INSTALL_DIR_ARG="$2"; shift 2 ;;
        --service-user)
            [ $# -ge 2 ] && [ -n "$2" ] || { echo "--service-user needs an account name" >&2; exit 2; }
            SVC_OVERRIDE="$2"; shift 2 ;;
        --allow-privilege-downgrade) ALLOW_DOWNGRADE=true; shift ;;
        -h|--help)
            cat <<EOF
Aerodrome install script.

Usage:
  ./install.sh                    Real install (default — assumes you have a receiver)
  ./install.sh --demo             Demo install (synthetic feeder, no receiver needed)
  sudo bash install.sh --privileges-only [--install-dir DIR]
                                  Refresh ONLY Aerodrome's system permissions (its
                                  system helper and the sudoers rule) for an existing
                                  install, then move a local notification server
                                  Aerodrome manages to its restricted setup. No
                                  packages, venv, Aerodrome units or restart.

Flags (used by bootstrap.sh --demo):
  --home-lat <n>                  Synthetic receiver latitude (default: 40.0)
  --home-lon <n>                  Synthetic receiver longitude (default: -75.0)
  --feeder-port <n>               Synthetic feeder port (default: 8080, falls back
                                  to 8088 then 28080 if 8080 is in use)

Permissions refresh (--privileges-only; automatic when this file sits inside an
install's update/ staging folder):
  --install-dir <dir>             The install to refresh (default: this script's
                                  install, or the one whose update/ folder holds it)
  --service-user <name>           The account Aerodrome runs as, when no
                                  aerodrome.service exists to read it from
  --allow-privilege-downgrade     Replace NEWER permission rules on this system with
                                  this copy's older ones (only after a deliberate
                                  rollback)

A re-run keeps the account an existing aerodrome.service runs as. To move
Aerodrome to another account, see docs/INSTALL.md ("Moving Aerodrome to another
account").
EOF
            exit 0 ;;
        *)
            echo "Unknown flag: $1" >&2
            echo "Run ./install.sh --help for usage." >&2
            exit 2 ;;
    esac
done

# The install path goes into systemd unit files. Spaces inside it are fine
# (ExecStart quotes the path; WorkingDirectory takes the rest of the line
# verbatim), but systemd gives " \ $ % special meaning even inside quotes
# (escapes, variable and specifier expansion), strips a trailing space or tab
# from a value, and can't carry a control character (tab, CR, newline) in a
# path — refuse those up front, before anything is changed.
# (A privileges-only refresh writes no unit, so it doesn't need this.)
if [ "$PRIV_ONLY" != true ]; then
    case "$INSTALL_DIR" in
        *[\"\\\$%]*|*[[:cntrl:]]*|*[[:space:]])
            echo -e "${RED}Error:${RESET} the install path contains a character systemd can't use in a unit file:" >&2
            echo "  ${INSTALL_DIR}" >&2
            echo "Move the aerodrome folder somewhere without \" \\ \$ %, a control character (tab, newline) or a" >&2
            echo "trailing space in its path (spaces inside it are fine) and re-run ./install.sh." >&2
            exit 2 ;;
    esac
fi

# ---------------------------------------------------------------------------
# Permission rules and the service account — shared by the full install and
# the privileges-only refresh.
# ---------------------------------------------------------------------------

# The rules Aerodrome's service account gets, for account $1. The marker below
# is the FIRST SUDOERS_VERSION marker in this file on purpose: the updater
# (server.py) reads the first one in a staged install.sh, and
# scripts/check_installers.py enforces that it lives in this function. It
# equals the helper's PRIV_VERSION (line 2 of scripts/aerodrome-helper), and
# any change to the rule lines or to the helper raises both
# (check_installers.py priv-version, priv-bump).
render_sudoers() {
    local u="$1"
    cat << SUDOEOF
# SUDOERS_VERSION: 6
# Aerodrome sudoers rule — machine-readable; written by install.sh.
# The SUDOERS_VERSION comment above is read by the updater to detect when
# a newer release requires this file to be refreshed. If Aerodrome shows
# "Sudoers update required", run the command it shows (or
# sudo bash <install>/scripts/recover-sudoers.sh). Never edit this file by hand.
#
# Aerodrome's system helper: a root-owned program with a fixed set of checked
# actions (install, configure, upgrade or remove the local ntfy server; move
# or remove the demo feeder). Named without arguments: it checks every
# argument itself.
${u} ALL=(root) NOPASSWD: /usr/local/libexec/aerodrome/aerodrome-helper
# Restart Aerodrome ("Restart now", and the restart after an update).
${u} ALL=(root) NOPASSWD: /bin/systemctl restart aerodrome, /usr/bin/systemctl restart aerodrome, /bin/systemctl restart --no-block aerodrome, /usr/bin/systemctl restart --no-block aerodrome
# Read this file, so the updater can compare its SUDOERS_VERSION with a
# staged release's. Read-only.
${u} ALL=(root) NOPASSWD: /bin/cat /etc/sudoers.d/aerodrome, /usr/bin/cat /etc/sudoers.d/aerodrome
# Compatibility for the previous release's switch-to-real only. Removed at SUDOERS_VERSION 7.
${u} ALL=(root) NOPASSWD: /bin/systemctl stop aerodrome-synthetic-feeder, /usr/bin/systemctl stop aerodrome-synthetic-feeder, /bin/systemctl disable aerodrome-synthetic-feeder, /usr/bin/systemctl disable aerodrome-synthetic-feeder, /bin/rm -f /etc/systemd/system/aerodrome-synthetic-feeder.service, /bin/systemctl daemon-reload, /usr/bin/systemctl daemon-reload
SUDOEOF
}

# The first version marker in text read from stdin ("0" when there is none).
# Reads to EOF (no early exit), so it is safe on the right of a pipe.
marker_of() {
    awk '!f && /^[[:space:]]*#[[:space:]]*SUDOERS_VERSION[[:space:]]*:[[:space:]]*[0-9]/ {
             v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); sub(/[^0-9].*$/, "", v); f = 1 }
         END { print (f ? v : 0) }'
}

# The marker of the LIVE drop-in on stdout ("0" when there is none). Read
# through sudo: it is 0440 root in a root-only directory (harmless when
# already root). Returns 1 when the drop-in may exist but couldn't be read —
# a refused read must never pass for "no rules yet" (the downgrade guard
# relies on it), so callers treat that as unknown and leave the rules alone.
live_marker() {
    local live
    if live="$(sudo cat "$SUDOERS_FILE" 2>/dev/null)"; then
        printf '%s\n' "$live" | marker_of
        return 0
    fi
    if sudo test ! -e "$SUDOERS_FILE" 2>/dev/null; then
        echo 0
        return 0
    fi
    return 1
}

# A name sudoers and passwd both accept, that exists, and is not root.
# Directory (SSSD/AD) logins may be fully qualified (name@corp.example.com)
# and longer than a local account; getent is the real existence check.
valid_account() {
    [ -n "$1" ] && [ "$1" != root ] && [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]*\$?$ ]] \
        && [ "${#1}" -le 256 ] && getent passwd "$1" >/dev/null 2>&1
}

# systemd's EFFECTIVE view of aerodrome.service (drop-ins included). Sets
# SELinux is on when its filesystem is mounted. Not `command -v selinuxenabled`:
# openSUSE keeps the SELinux tools in /usr/sbin, which isn't on a regular
# account's PATH there (sudo's own PATH has it, so `sudo restorecon` works).
selinux_on() { [ -e "${AERODROME_SELINUX_ENFORCE:-/sys/fs/selinux/enforce}" ]; }

# SELinux blocks a home-folder install (refuse it up front, before anything
# is changed): systemd starts services as init_t, which can't read inside a
# home folder (user_home_t), so such a service ends with "Failed to start"
# after the whole install ran. The enforce file says 1 under enforcing and 0
# under permissive — permissive only logs the denials, so a home install
# works there and is not refused. $1 is the install dir; $2 (optional) the
# service account's own home, on top of /home, /root and $HOME. AERODROME_SELINUX_ENFORCE
# stands in for the real enforce file in the tests. selinux_blocks_home() is
# byte-identical in scripts/bootstrap.sh (checked on every release by
# scripts/check_installers.py).
selinux_blocks_home() {
    local dir home
    [ "$(cat -- "${AERODROME_SELINUX_ENFORCE:-/sys/fs/selinux/enforce}" 2> /dev/null)" = 1 ] || return 1
    dir="$(realpath -m -- "$1")"
    for home in /home /root "${2:-}" "${HOME:-}"; do
        [ -n "$home" ] || continue
        home="$(realpath -m -- "$home")"
        [ "$home" = / ] && continue
        case "$dir/" in "$home"/*) return 0 ;; esac
    done
    return 1
}

# UNIT_LOAD, UNIT_WD and UNIT_USER. Read-only; empty when there is no systemd.
read_unit() {
    local out
    out="$(systemctl show -p LoadState -p WorkingDirectory -p User "${SERVICE_NAME}.service" 2>/dev/null)" || out=""
    UNIT_LOAD="$(printf '%s\n' "$out" | sed -n 's/^LoadState=//p')"
    UNIT_WD="$(printf '%s\n' "$out" | sed -n 's/^WorkingDirectory=//p')"
    UNIT_USER="$(printf '%s\n' "$out" | sed -n 's/^User=//p')"
}

# resolve_service_user ROOT [OVERRIDE] -> prints the account the aerodrome
# service runs as; returns 2 with a message on stderr when it must refuse.
# ROOT is canonical (realpath -e). Never uses SUDO_USER: under `sudo bash …`
# that is whoever typed the command, not necessarily the service account.
resolve_service_user() {
    local root="$1" override="${2:-}" user real_wd
    read_unit
    if [ "$UNIT_LOAD" = loaded ]; then
        real_wd="$(realpath -e -- "$UNIT_WD" 2>/dev/null || true)"
        if [ "$real_wd" != "$root" ]; then
            echo "${SERVICE_NAME}.service runs Aerodrome from ${UNIT_WD:-?}, not $root — refusing to change permissions for another install." >&2
            return 2
        fi
        user="$UNIT_USER"
        if [ -z "$user" ] || [ "$user" = root ]; then
            echo "${SERVICE_NAME}.service has no non-root User= — Aerodrome's permission rules are for the account it runs as. Fix the unit first." >&2
            return 2
        fi
        if [ -n "$override" ] && [ "$override" != "$user" ]; then
            echo "Aerodrome runs as $user; --service-user $override doesn't match. Leave it out." >&2
            return 2
        fi
        if ! valid_account "$user"; then
            echo "Can't use account '$user' (the account ${SERVICE_NAME}.service runs as) for Aerodrome's permissions:" >&2
            echo "this system can't look it up, or it isn't a name sudo accepts. Check: getent passwd $(printf %q "$user")" >&2
            return 2
        fi
    else
        user="${override:-$(stat -c %U -- "$root" 2>/dev/null || true)}"
        if ! valid_account "$user"; then
            echo "Can't use account '${user:-?}' for Aerodrome's permissions (missing, root, or not a valid name). Pass --service-user NAME." >&2
            return 2
        fi
    fi
    printf '%s\n' "$user"
}

# The rules ($1), checked, in a dot-temp in /etc/sudoers.d (SUDOERS_TMP).
# sudo AND sudo-rs skip a file whose name starts with '.' (sudoers(5) /
# sudoers-rs(5), @includedir), so a temp is never read as rules; on any
# failure it is removed and the live file is exactly as it was. Every step is
# a sudo call (harmless when already root).
stage_sudoers_dropin() {
    local out
    SUDOERS_TMP=""
    # Temp files of a run that was killed mid-way (never read by sudo).
    sudo find /etc/sudoers.d -maxdepth 1 -type f -name '.aerodrome.*' -delete 2>/dev/null || true
    if ! SUDOERS_TMP="$(sudo mktemp /etc/sudoers.d/.aerodrome.XXXXXX)" || [ -z "$SUDOERS_TMP" ]; then
        echo -e "  ${RED}✗${RESET} Could not create a temporary file in /etc/sudoers.d — ${SUDOERS_FILE} was NOT changed."
        return 1
    fi
    if ! printf '%s\n' "$1" | sudo tee "$SUDOERS_TMP" > /dev/null || ! sudo chmod 0440 "$SUDOERS_TMP"; then
        sudo rm -f "$SUDOERS_TMP" || true
        echo -e "  ${RED}✗${RESET} Could not write the new sudoers rule — ${SUDOERS_FILE} was NOT changed."
        return 1
    fi
    if ! out="$(sudo visudo -cf "$SUDOERS_TMP" 2>&1)"; then
        sudo rm -f "$SUDOERS_TMP" || true
        echo -e "  ${RED}✗${RESET} Sudoers rule failed validation — ${SUDOERS_FILE} was NOT changed:"
        printf '%s\n' "$out" | sed 's/^/      /'
        return 1
    fi
}

# The checked SUDOERS_TMP onto the live file: mv within one directory is an
# atomic rename, so sudo sees the old rules or the new ones, never a part.
activate_sudoers_dropin() {
    if sudo mv -f "$SUDOERS_TMP" "$SUDOERS_FILE"; then
        echo -e "  ${GREEN}✓${RESET} Sudoers rule installed (${SUDOERS_FILE})"
        return 0
    fi
    sudo rm -f "$SUDOERS_TMP" || true
    echo -e "  ${RED}✗${RESET} Could not move the new sudoers rule into place — ${SUDOERS_FILE} was NOT changed."
    return 1
}

# trusted_dir DIR ACCOUNT — DIR and every folder above it are real folders
# (not symlinks) owned by root that neither everyone nor a group ACCOUNT is
# in can write to: nobody but root can swap what sits in them. Read through
# sudo stat (a folder on the way may not be searchable otherwise).
trusted_dir() {
    local d="$1" gids st uid gid mode kind
    gids=" $(id -G "$2" 2>/dev/null) "
    while :; do
        st="$(sudo stat -c '%u %g %a %F' -- "$d" 2>/dev/null)" || return 1
        read -r uid gid mode kind <<< "$st"
        [ "$kind" = directory ] && [ "$uid" = 0 ] || return 1
        if (( 8#$mode & 8#002 )) || { (( 8#$mode & 8#020 )) && [[ "$gids" == *" $gid "* ]]; }; then
            return 1
        fi
        [ "$d" = / ] && return 0
        d="$(dirname -- "$d")"
    done
}

# trusted_file FILE ACCOUNT — a regular file of root's that only root can
# write, in a trusted_dir.
trusted_file() {
    local st uid gid mode kind
    st="$(sudo stat -c '%u %g %a %F' -- "$1" 2>/dev/null)" || return 1
    read -r uid gid mode kind <<< "$st"
    case "$kind" in "regular file"|"regular empty file") ;; *) return 1 ;; esac
    [ "$uid" = 0 ] && ! (( 8#$mode & 8#022 )) && trusted_dir "$(dirname -- "$1")" "$2"
}

# pick_helper_python ACCOUNT — the system Python the helper runs under,
# printed: the first of a fixed list (never the venv, never $PATH) that is a
# root-owned file only root can write, in trusted folders, and is 3.10 or
# newer. A candidate is run only once it is trusted. AERODROME_HELPER_PYTHONS
# replaces the list (tests): only root's own environment can set it — sudo
# drops it. Returns 1, naming what it found, when none qualifies.
pick_helper_python() {
    local c real found="" v
    for c in ${AERODROME_HELPER_PYTHONS:-/usr/bin/python3 /usr/bin/python3.14 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3.10}; do
        real="$(realpath -e -- "$c" 2>/dev/null)" || continue
        if ! trusted_file "$real" "$1" || ! trusted_dir "$(dirname -- "$c")" "$1"; then
            found="${found:+$found, }$c (not root's alone)"
            continue
        fi
        v="$("$c" -IS -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || v="?"
        if "$c" -IS -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            printf '%s\n' "$c"
            return 0
        fi
        found="${found:+$found, }$c ($v)"
    done
    echo -e "  ${RED}✗${RESET} Aerodrome's helper needs a system Python 3.10 or newer in /usr/bin (found: ${found:-none})." >&2
    echo "    Install your distribution's python3.11 (or newer) package and run this again." >&2
    echo "    Nothing was changed." >&2
    return 1
}

# The PRIV_VERSION on line 2 of helper file $1 ("0" when it is missing).
helper_version_of() {
    local v
    v="$(sudo sed -n '2s/^# PRIV_VERSION = \([0-9][0-9]*\)$/\1/p' -- "$1" 2>/dev/null)" || v=""
    printf '%s\n' "${v:-0}"
}

# install_helper SRC PYTHON VERSION — SRC to $HELPER, root:root 0755, line 1
# naming PYTHON with -IS (no site packages, no user paths, no PYTHON*
# variables), through a dot-temp in the same folder that must parse and
# answer `version` with VERSION before it is renamed into place.
install_helper() {
    local tmp out
    if ! tmp="$(sudo mktemp "${HELPER_DIR}/.aerodrome-helper.XXXXXX")" || [ -z "$tmp" ]; then
        echo -e "  ${RED}✗${RESET} Could not create a temporary file in ${HELPER_DIR} — nothing was changed."
        return 1
    fi
    if ! { printf '#!%s -IS\n' "$2"; tail -n +2 -- "$1"; } | sudo tee "$tmp" > /dev/null \
            || ! sudo chmod 0755 "$tmp" \
            || ! sudo "$2" -IS -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$tmp" \
            || ! out="$(sudo "$2" -IS "$tmp" version 2>/dev/null)" \
            || [[ "$out" != *"\"version\": $3,"* ]]; then
        sudo rm -f "$tmp" || true
        echo -e "  ${RED}✗${RESET} The new system helper didn't pass its check (${2}) — nothing was changed."
        return 1
    fi
    if ! sudo mv -f "$tmp" "$HELPER"; then
        sudo rm -f "$tmp" || true
        echo -e "  ${RED}✗${RESET} Could not move the new system helper into place — nothing was changed."
        return 1
    fi
    if selinux_on; then
        sudo restorecon "$HELPER" 2> /dev/null || true
    fi
    echo -e "  ${GREEN}✓${RESET} System helper installed (${HELPER}, run by ${2})"
}

# migrate_ntfy PYTHON — the helper's ntfy-migrate: a local ntfy server
# Aerodrome manages moves to its restricted setup. Never fails the run: when
# it can't move, ntfy keeps its previous setup and this prints why and what
# finishes the move.
migrate_ntfy() {
    local out rc fields problems custom msg moved drop=""
    out="$(sudo "$HELPER" ntfy-migrate 2> /dev/null)" && rc=0 || rc=$?
    fields="$(printf '%s\n' "$out" | "$1" -IS -c 'import json, sys
lines = sys.stdin.read().strip().splitlines()
try:
    d = json.loads(lines[-1])
except Exception:
    d = {}
d = d if isinstance(d, dict) else {}
print(",".join(d.get("problems") or []))
print(",".join(d.get("custom_keys") or []))
print(" ".join(str(d.get("message") or "").split()))
print("yes" if d.get("migrated") else "already" if d.get("already") else "no")' 2> /dev/null)" || fields=""
    problems="$(sed -n 1p <<< "$fields")"
    custom="$(sed -n 2p <<< "$fields")"
    msg="$(sed -n 3p <<< "$fields")"
    moved="$(sed -n 4p <<< "$fields")"
    [ -n "$custom" ] && drop=" --drop-custom-keys $(printf %q "$custom")"
    if [ "$rc" = 0 ]; then
        case "$moved" in
            yes)     echo -e "  ${GREEN}✓${RESET} Local notification server moved to its restricted setup" ;;
            already) echo -e "  ${GREEN}✓${RESET} Local notification server: already on its restricted setup" ;;
            *)       echo -e "  ${GREEN}✓${RESET} Local notification server: nothing to move" ;;
        esac
    elif [ "$rc" = 3 ] && [ -n "$problems" ]; then
        echo -e "  ${YELLOW}⚠${RESET}  The local notification server was left on its previous setup: its config file has"
        echo "     values Aerodrome can't carry over (${problems//,/, })."
        # Saving in the Notifications tab rewrites the address and the relay
        # setting, never a listen address Aerodrome can't represent.
        if [[ ",${problems}," != *,listen-http,* ]]; then
            echo "     Once this version of Aerodrome is running, enter the server address in its"
            echo "     Notifications tab and save — that finishes the move. Or correct the value in"
            echo "     /etc/ntfy/server.yml and run:"
        else
            echo "     Correct them in /etc/ntfy/server.yml, then run:"
        fi
        echo "       sudo ${HELPER} ntfy-migrate${drop}"
        if [ -n "$custom" ]; then
            echo "     Moving it also removes the settings Aerodrome doesn't manage (${custom//,/, }); your"
            echo "     current file is kept as /etc/ntfy/server.yml.pre-v6."
        fi
    elif [ "$rc" = 3 ] && [ -n "$custom" ]; then
        echo -e "  ${YELLOW}⚠${RESET}  The local notification server was left on its previous setup: its config file has"
        echo "     settings Aerodrome doesn't manage (${custom//,/, }). Moving it removes them; your current"
        echo "     file is kept as /etc/ntfy/server.yml.pre-v6 when you run:"
        echo "       sudo ${HELPER} ntfy-migrate${drop}"
    else
        echo -e "  ${YELLOW}⚠${RESET}  The local notification server kept its previous setup: ${msg:-no answer from the system helper}"
    fi
    return 0
}

# install_privileged ACCOUNT RULES — Aerodrome's system permissions for
# ACCOUNT: the helper and the sudoers RULES that name it, then the move of a
# local ntfy server to its restricted setup. In this order, so a run killed
# anywhere leaves the old rules or the new ones, and the old helper or the
# whole new one, and running it again finishes:
#   0. leftovers of a killed run go;  1. the helper's folder must be root's
#   alone;  2. a system Python 3.10+;  3. a newer installed helper is kept;
#   4. RULES to a dot-temp, checked by visudo;  5. the helper into place;
#   6. RULES live — the step that lifts the Updates page's gate;
#   7. ntfy-migrate (never fails the run).
# Returns 1 with the live rules unchanged when a step up to 6 fails.
install_privileged() {
    local svc="$1" src="${INSTALL_DIR}/scripts/aerodrome-helper" py want have
    sudo find "$HELPER_DIR" -maxdepth 1 -type f -name '.aerodrome-helper.*' -delete 2> /dev/null || true
    if [ ! -f "$src" ]; then
        echo -e "  ${RED}✗${RESET} This copy of Aerodrome has no scripts/aerodrome-helper — nothing was changed."
        return 1
    fi
    if ! sudo test -d "$HELPER_DIR" && ! sudo install -d -m 0755 -o root -g root "$HELPER_DIR"; then
        echo -e "  ${RED}✗${RESET} Could not create ${HELPER_DIR} — nothing was changed."
        return 1
    fi
    if ! trusted_dir "$HELPER_DIR" "$svc"; then
        echo -e "  ${RED}✗${RESET} ${HELPER_DIR}, or a folder above it, isn't root's alone (a symlink, or"
        echo "    ${svc} could write to it) — nothing was changed. Make it a root-owned folder (0755)."
        return 1
    fi
    py="$(pick_helper_python "$svc")" || return 1
    want="$(helper_version_of "$src")"
    have="$(helper_version_of "$HELPER")"
    stage_sudoers_dropin "$2" || return 1
    if [ "$have" -gt "$want" ]; then
        echo -e "  ${YELLOW}⚠${RESET}  Kept the newer system helper (version ${have}) this system already has."
    elif ! install_helper "$src" "$py" "$want"; then
        sudo rm -f "$SUDOERS_TMP" || true
        return 1
    fi
    activate_sudoers_dropin || return 1
    migrate_ntfy "$py"
}

# ---------------------------------------------------------------------------
# Privileges-only refresh: the system helper and the sudoers rule for an
# existing install, then the move of a local ntfy server Aerodrome manages
# (install_privileged). No packages, venv, Aerodrome units, chown or restart —
# sudo re-reads its rules on every call. Runs before the not-root check
# below: it needs root.
# ---------------------------------------------------------------------------
if [ "$PRIV_ONLY" = true ]; then
    echo ""
    if [ -n "$_staging_root" ]; then
        echo "install.sh is sitting inside an existing install's update/ staging directory"
        echo "— refreshing system permissions only for ${_staging_root}"
    else
        echo "Aerodrome install.sh — refreshing system permissions only"
    fi
    if [ "$(id -u)" -ne 0 ]; then
        echo -e "${RED}✗${RESET} Refreshing Aerodrome's system permissions needs root. Run it with sudo:" >&2
        echo "    sudo bash $(printf %q "$0") --privileges-only${INSTALL_DIR_ARG:+ --install-dir $(printf %q "$INSTALL_DIR_ARG")}" >&2
        exit 1
    fi
    # Which install: --install-dir, else the one whose update/ holds this
    # file, else this file's own tree. Compared canonically, never as strings.
    if [ -n "$INSTALL_DIR_ARG" ]; then
        REAL_ROOT="$(realpath -e -- "$INSTALL_DIR_ARG" 2>/dev/null || true)"
        if [ -n "$_staging_root" ] \
                && [ "$REAL_ROOT" != "$(realpath -e -- "$_staging_root" 2>/dev/null || true)" ]; then
            echo -e "${RED}✗${RESET} This copy is staged for the install at ${_staging_root}, not ${INSTALL_DIR_ARG}." >&2
            exit 2
        fi
    else
        REAL_ROOT="$(realpath -e -- "${_staging_root:-$INSTALL_DIR}" 2>/dev/null || true)"
    fi
    if [ -z "$REAL_ROOT" ] || [ ! -f "$REAL_ROOT/VERSION" ] || [ ! -f "$REAL_ROOT/main.py" ]; then
        echo -e "${RED}✗${RESET} Not an Aerodrome install: ${INSTALL_DIR_ARG:-${_staging_root:-$INSTALL_DIR}}" >&2
        echo "    Say where it is: --install-dir DIR" >&2
        exit 2
    fi
    echo "  Install:          ${REAL_ROOT}"
    echo "  Rules from:       ${INSTALL_DIR} (v$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '?'))"
    SVC_USER="$(resolve_service_user "$REAL_ROOT" "$SVC_OVERRIDE")" || exit 2
    echo "  Service account:  ${SVC_USER} (unchanged)"
    RULES="$(render_sudoers "$SVC_USER")"
    NEW_MARK="$(printf '%s\n' "$RULES" | marker_of)"
    if ! LIVE_MARK="$(live_marker)"; then
        echo -e "${YELLOW}⚠${RESET}  Couldn't read the current permission rules (${SUDOERS_FILE}) — nothing was changed." >&2
        exit 3
    fi
    if [ "$LIVE_MARK" -gt "$NEW_MARK" ] && [ "$ALLOW_DOWNGRADE" != true ]; then
        echo -e "${YELLOW}⚠${RESET}  This system already has newer Aerodrome permission rules (version ${LIVE_MARK}) than this copy of Aerodrome (${NEW_MARK})." >&2
        echo "    Nothing was changed. If you rolled Aerodrome back on purpose and want this copy's older rules, run:" >&2
        echo "      sudo bash $(printf %q "$INSTALL_DIR/install.sh") --privileges-only --install-dir $(printf %q "$REAL_ROOT") --allow-privilege-downgrade" >&2
        exit 3
    fi
    # The rules are what lift the Updates page's gate, so they go live after
    # the helper they name (install_privileged's step 6).
    install_privileged "$SVC_USER" "$RULES" || exit 1
    echo ""
    echo -e "${GREEN}System permissions refreshed${RESET} (rules version ${NEW_MARK})."
    echo "Go back to Aerodrome's Updates page and press Re-check (or \"I ran the command — re-check\")."
    exit 0
fi

if [ -n "$SVC_OVERRIDE" ] || [ -n "$INSTALL_DIR_ARG" ]; then
    echo -e "${RED}Error:${RESET} --service-user and --install-dir only go with --privileges-only." >&2
    echo "A full install runs as the account that runs it (a re-run keeps an existing service's" >&2
    echo "account). To move Aerodrome to another account, see docs/INSTALL.md" >&2
    echo "(\"Moving Aerodrome to another account\")." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Full install: which account the service runs as
# ---------------------------------------------------------------------------
# A re-run keeps the account an existing aerodrome.service (for THIS install)
# already runs as — whoever runs the script; if that account can't be used
# right now (e.g. a directory login while the directory is unreachable) it
# stops rather than move the service to someone else. Otherwise: the account
# running the script, or — run as root (`sudo bash install.sh`) — the account
# that typed sudo (SUDO_USER). Not SUDO_USER otherwise: `sudo -iu <account>
# bash …` leaves the admin's name there, but that run is <account>'s.
INSTALL_REAL="$(realpath -e -- "$INSTALL_DIR" 2>/dev/null || printf '%s' "$INSTALL_DIR")"
KEEP_ACCOUNT=false
read_unit
# A second install must not take over the first one's service: rewriting
# aerodrome.service from this directory would move the OTHER install's unit
# here (the privileges-only refresh refuses the same case in
# resolve_service_user). A unit whose folder was deleted by hand no longer
# runs anything, so this install may take the (now orphaned) service.
if [ "$UNIT_LOAD" = loaded ] && [ -n "$UNIT_WD" ]; then
    if real_wd="$(realpath -e -- "$UNIT_WD" 2>/dev/null)"; then
        if [ "$real_wd" != "$INSTALL_REAL" ]; then
            echo -e "${RED}Aerodrome is already installed in ${UNIT_WD} (${SERVICE_NAME}.service runs it from there).${RESET}" >&2
            echo "This install, in ${INSTALL_DIR}, would take that service over. Nothing was changed." >&2
            echo "" >&2
            echo "  To update that copy: open its web UI → Updates" >&2
            echo "  To move Aerodrome here: run that copy's uninstall.sh first" >&2
            echo "             (bash $(printf %q "${UNIT_WD}/uninstall.sh"))" >&2
            exit 2
        fi
    else
        echo -e "${YELLOW}⚠${RESET}  ${SERVICE_NAME}.service pointed at ${UNIT_WD}, which no longer exists — this install takes the service over." >&2
    fi
fi
if [ "$UNIT_LOAD" = loaded ] && [ -n "$UNIT_USER" ] && [ "$UNIT_USER" != root ] \
        && [ "$(realpath -e -- "$UNIT_WD" 2>/dev/null || true)" = "$INSTALL_REAL" ]; then
    if ! valid_account "$UNIT_USER"; then
        echo -e "${RED}Aerodrome here runs as '${UNIT_USER}', but this system can't look that account up right now${RESET}" >&2
        echo "(or it isn't a name sudo accepts). Check: getent passwd $(printf %q "$UNIT_USER")" >&2
        echo "Nothing was changed. To move Aerodrome to another account, see docs/INSTALL.md" >&2
        echo "(\"Moving Aerodrome to another account\")." >&2
        exit 2
    fi
    USER="$UNIT_USER"
    KEEP_ACCOUNT=true
    # Without sudo, only the service account itself can re-run the install:
    # files it creates must belong to that account, and the unit keeps it.
    if [ "$(id -u)" -ne 0 ] && [ "$(id -un)" != "$USER" ]; then
        echo -e "${RED}Aerodrome here runs as '${USER}'.${RESET} Run this as ${USER}, or as root with" >&2
        echo "  sudo bash install.sh   (it keeps ${USER})" >&2
        echo "Nothing was changed. To move Aerodrome to another account, see docs/INSTALL.md" >&2
        echo "(\"Moving Aerodrome to another account\")." >&2
        exit 2
    fi
elif [ "$(id -u)" -eq 0 ]; then
    USER="${SUDO_USER:-$(whoami)}"
else
    USER="$(id -un)"
fi

if [ "$USER" = "root" ]; then
    echo -e "${RED}Please run this script as a regular user (not root).${RESET}"
    echo "It will use sudo only where needed."
    exit 1
fi

# The user's PRIMARY group — not a group named after the user. They differ for
# accounts created with a shared primary group (e.g. `users`), `useradd -N/-g`,
# or directory (LDAP/SSSD) logins. Used for chown; the systemd units omit
# Group= so systemd picks this same default group itself.
SVC_GROUP="$(id -gn "$USER" 2>/dev/null || true)"
if [ -z "$SVC_GROUP" ]; then
    echo -e "${RED}Could not determine the primary group of user '${USER}' (id -gn failed).${RESET}"
    exit 1
fi

# A home-folder install can never start under enforcing SELinux (see
# selinux_blocks_home above). Say so here, before the banner and before
# anything is changed — not after the whole run, at "Failed to start".
# Privileges-only refreshes are exempt: they touch no units and start
# nothing (that path exited far above).
acct_home="$(getent passwd "$USER" | cut -d: -f6)"
if selinux_blocks_home "$INSTALL_REAL" "$acct_home"; then
    echo -e "  ${RED}✗${RESET} Aerodrome can't run from ${INSTALL_DIR}: SELinux is enforcing here, and it" >&2
    echo "    doesn't let system services start programs inside a home folder. Nothing was changed." >&2
    echo "" >&2
    echo "  Move this folder to /opt/aerodrome and run the installer there:" >&2
    echo "    sudo mv $(printf %q "$INSTALL_DIR") /opt/aerodrome && sudo chown -R $(printf %q "$USER"): /opt/aerodrome" >&2
    echo "    /opt/aerodrome/install.sh" >&2
    exit 2
fi

# v2.40.3 note: if invoked via `sudo ./install.sh`, SUDO_USER points at
# the real user and we continue. We used to silently create root-owned
# files in that case, which broke the service at startup. The script
# now chowns everything it touches back to ${USER}, so both paths work
# — but the `./install.sh` form (without sudo) is still cleaner, since
# the script uses sudo only where it actually needs root. Only as root:
# a non-root run with an inherited SUDO_USER isn't running via sudo. And
# the no-sudo form is only offered to the service account itself — anyone
# else is refused without sudo (above).
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    echo -e "${CYAN}Note:${RESET} running via sudo. Ownership of created files"
    if [ "$SUDO_USER" = "$USER" ]; then
        echo "  will be set to ${USER}. Running as './install.sh' (no sudo) also works"
        echo "  and is slightly cleaner — the script uses sudo where needed."
    else
        echo "  will be set to ${USER}, the account Aerodrome runs as."
    fi
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
if [ "$KEEP_ACCOUNT" = true ]; then
    echo "                (keeping service account ${USER} from the existing service)"
fi
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
# pkg_detect() is byte-identical in scripts/bootstrap.sh (checked on every
# release by scripts/check_installers.py).
pkg_detect() {
    local os_id="" os_like="" osr="${AERODROME_BOOTSTRAP_OS_RELEASE:-/etc/os-release}"
    if [ -r "$osr" ]; then
        eval "$(
            . "$osr"
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
    # ID/ID_LIKE unrecognised but a known package manager is installed: use
    # its family so prerequisite installs can work. PKG_FAMILY_GUESSED tells
    # the caller it is a guess (the bootstrap still warns or needs --force).
    PKG_FAMILY_GUESSED=false
    if [ "$PKG_FAMILY" = unknown ]; then
        if   command -v apt-get >/dev/null 2>&1; then PKG_FAMILY=debian
        elif command -v dnf     >/dev/null 2>&1; then PKG_FAMILY=fedora
        elif command -v pacman  >/dev/null 2>&1; then PKG_FAMILY=arch
        elif command -v zypper  >/dev/null 2>&1; then PKG_FAMILY=opensuse
        fi
        if [ "$PKG_FAMILY" != unknown ]; then PKG_FAMILY_GUESSED=true; fi
    fi
    case "$PKG_FAMILY" in
        debian)
            # Never asks: debconf would otherwise try a dialog (and warn
            # over a non-tty ssh). No python3-pip: apt's recommends for it
            # are a C toolchain (~600 MB), and the venv brings its own pip.
            PKG_REFRESH_CMD="sudo apt-get update -qq"
            PKG_INSTALL_CMD="sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq"
            PKG_PYTHON3="python3"; PKG_PIP=""
            PKG_VENV="python3-venv"; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        fedora)
            PKG_REFRESH_CMD=""
            PKG_INSTALL_CMD="sudo dnf install -y -q"
            PKG_PYTHON3="python3"; PKG_PIP="python3-pip"
            PKG_VENV=""; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        arch)
            # -Syu in one step: a refresh followed by a separate -S is a
            # partial upgrade, which Arch doesn't support.
            PKG_REFRESH_CMD=""
            PKG_INSTALL_CMD="sudo pacman -Syu --needed --noconfirm"
            PKG_PYTHON3="python"; PKG_PIP="python-pip"
            PKG_VENV=""; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        opensuse)
            PKG_REFRESH_CMD="sudo zypper --non-interactive refresh"
            PKG_INSTALL_CMD="sudo zypper --non-interactive install"
            PKG_PYTHON3="python3"; PKG_PIP="python3-pip"
            PKG_VENV=""; PKG_CURL="curl"; PKG_UNZIP="unzip" ;;
        *)
            PKG_REFRESH_CMD=""; PKG_INSTALL_CMD=""
            PKG_PYTHON3=""; PKG_PIP=""; PKG_VENV=""; PKG_CURL=""; PKG_UNZIP="" ;;
    esac
}
pkg_install() {
    local pkgs=() p
    for p in "$@"; do [ -n "$p" ] && pkgs+=("$p"); done
    [ "${#pkgs[@]}" -eq 0 ] && return 0
    [ -z "$PKG_INSTALL_CMD" ] && {
        echo -e "  ${RED}✗${RESET} Unknown package manager — cannot install: ${pkgs[*]}" >&2
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

# Re-runs (and bootstrap installs, whose [3/7] already did this) usually have
# everything: skip the package manager then, so a broken third-party
# repository can't fail a re-run that needs nothing installed. (pip still
# runs below, so a re-run still needs the network.)
if command -v python3 >/dev/null 2>&1 \
        && python3 -c 'import venv, ensurepip' >/dev/null 2>&1 \
        && command -v curl >/dev/null 2>&1; then
    echo -e "  ${GREEN}✓${RESET} Python3, venv and curl already present"
else
    if [ "$PKG_FAMILY" = "unknown" ]; then
        echo -e "  ${RED}✗${RESET} Could not detect a supported package manager"
        echo "    Supported families: Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE"
        echo "    Install python3 (with venv) and curl yourself, then re-run ./install.sh;"
        echo "    see docs/INSTALL.md for manual install steps on other distros."
        exit 1
    fi
    if [ "$PKG_FAMILY_GUESSED" = true ]; then
        echo -e "  ${YELLOW}⚠${RESET}  Unrecognised distribution — using its ${PKG_FAMILY}-style package manager"
    fi
    # Quiet on success, but never silent on failure: capture the output and
    # show its tail (set -e used to exit here with nothing on screen).
    PKG_LOG="$(mktemp "${TMPDIR:-/tmp}/aerodrome-install-packages.XXXXXX")"
    if ! pkg_refresh > "$PKG_LOG" 2>&1; then
        echo -e "  ${YELLOW}⚠${RESET}  Package index refresh failed — trying the install anyway:"
        tail -n 5 "$PKG_LOG" | sed 's/^/      /'
    fi
    if [ "$PKG_FAMILY" = arch ]; then
        echo "    (Arch: pacman -Syu brings the rest of the system up to date first; Arch doesn't support partial upgrades)"
    fi
    if ! pkg_install "$PKG_PYTHON3" "$PKG_PIP" "$PKG_VENV" "$PKG_CURL" >> "$PKG_LOG" 2>&1; then
        echo -e "  ${RED}✗${RESET} Installing system packages failed (${PKG_FAMILY} package manager):"
        tail -n 20 "$PKG_LOG" | sed 's/^/      /'
        echo "    Full output: ${PKG_LOG}"
        echo "    Fix the error above (often a broken third-party repository or no"
        echo "    network), then re-run ./install.sh."
        exit 1
    fi
    rm -f "$PKG_LOG"
    echo -e "  ${GREEN}✓${RESET} Python3, venv and curl installed (via $PKG_FAMILY package manager)"
fi

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
chown -R "${USER}:${SVC_GROUP}" \
    "${INSTALL_DIR}/logs" \
    "${INSTALL_DIR}/update" \
    "${INSTALL_DIR}/.backups" \
    "${INSTALL_DIR}/venv" 2>/dev/null || true
echo -e "  ${GREEN}✓${RESET} Log, update, and backup directories created"

# SELinux: files keep the label of wherever they were created, so a tree
# unpacked under /tmp and moved here (the bootstrap does that) stays
# user_tmp_t. Give the whole install folder its proper labels.
if selinux_on; then
    if sudo restorecon -R -- "$INSTALL_DIR" 2> /dev/null; then
        echo -e "  ${GREEN}✓${RESET} SELinux labels set on ${INSTALL_DIR}"
    else
        echo -e "  ${YELLOW}⚠${RESET}  Couldn't reset SELinux labels on ${INSTALL_DIR} (restorecon failed) — continuing"
    fi
fi

# v3.4.40: resolve which port the synthetic feeder will use BEFORE
# the config.yaml-write branch (which needs the value to set
# receiver.port) AND the systemd-unit-write block (which needs the
# value for ExecStart's --port flag). Lifted out so upgrade-style
# re-runs — where config.yaml already exists and the fresh-config
# branch is skipped — still resolve and preserve the existing port
# choice rather than silently reverting a user-customized unit file.
#
# Order of precedence:
#   1. --feeder-port flag (explicit override, validated only as
#      numeric; honored even if the port is taken — the user knows
#      what they're doing).
#   2. Existing port read from the live feeder unit file (preserves
#      a non-default choice across upgrades).
#   3. Fallback chain — first port from FEEDER_PORT_DEFAULTS that
#      isn't already listening. Default chain is 8080 → 8088 → 28080.
# On chain exhaustion (all candidates taken), fail loudly with a
# clear message rather than silently colliding.
#
# ss is part of iproute2, on every Tier-1 distro. The `ss -tln` form
# intentionally doesn't pass -p (which would require root); we only
# need to know if the port has a listener, not who.
if [ "$DEMO_MODE" = "true" ]; then
    FEEDER_PORT=""
    if [ -n "$FEEDER_PORT_REQUESTED" ]; then
        if ! [[ "$FEEDER_PORT_REQUESTED" =~ ^[0-9]+$ ]]; then
            echo -e "  ${RED}✗${RESET} --feeder-port must be a number, got: ${FEEDER_PORT_REQUESTED}"
            exit 2
        fi
        FEEDER_PORT="$FEEDER_PORT_REQUESTED"
        if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE ":${FEEDER_PORT}$"; then
            echo -e "  ${YELLOW}⚠${RESET}  Port ${FEEDER_PORT} appears to already be in use, but --feeder-port was passed explicitly — proceeding."
        fi
    elif [ -f "/etc/systemd/system/${FEEDER_SERVICE_NAME}.service" ]; then
        # Upgrade-style re-run — preserve existing port choice IF the
        # port is still free. v3.4.43: previously this branch blindly
        # reused whatever port the existing unit file specified, which
        # would re-create the wedge state if something else had grabbed
        # that port between installs (the original PL7370 install hit
        # this exact case: install.sh preserved --port 8080 from the
        # prior wedged install, even though Hoptrail had taken 8080
        # in the meantime).
        EXISTING_PORT=$(grep -oE '\-\-port[[:space:]]+[0-9]+' \
            "/etc/systemd/system/${FEEDER_SERVICE_NAME}.service" \
            | awk '{print $2}' | head -1)
        if [ -n "$EXISTING_PORT" ]; then
            if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE ":${EXISTING_PORT}$"; then
                # The existing port is in use. Don't silently re-bind
                # to it — fall through to the candidate chain below so
                # we pick a free port instead.
                echo -e "  ${YELLOW}⚠${RESET}  Previous install used port ${EXISTING_PORT} which is now in use; selecting a new free port from the fallback chain"
                # Leave FEEDER_PORT empty so the chain-walk runs.
            else
                FEEDER_PORT="$EXISTING_PORT"
            fi
        fi
    fi

    if [ -z "$FEEDER_PORT" ]; then
        # Fresh install — walk the default chain looking for a free port
        for candidate in "${FEEDER_PORT_DEFAULTS[@]}"; do
            if ! ss -tln 2>/dev/null | awk '{print $4}' | grep -qE ":${candidate}$"; then
                FEEDER_PORT="$candidate"
                break
            fi
        done
        if [ -z "$FEEDER_PORT" ]; then
            echo
            echo -e "  ${RED}✗${RESET} Demo install failed: every candidate port is already in use."
            echo
            echo "    Candidates checked: ${FEEDER_PORT_DEFAULTS[@]}"
            echo "    What's using them:"
            for p in "${FEEDER_PORT_DEFAULTS[@]}"; do
                echo "      :${p}  $(ss -tlnp 2>/dev/null | awk -v port=":${p}" '$4 ~ port {print $NF}' | head -1)"
            done
            echo
            echo "    Free one of the ports above, or re-run with an explicit port:"
            echo "      ./install.sh --demo --feeder-port 38080"
            echo
            exit 1
        fi
        if [ "$FEEDER_PORT" != "${FEEDER_PORT_DEFAULTS[0]}" ]; then
            echo -e "  ${YELLOW}⚠${RESET}  Port ${FEEDER_PORT_DEFAULTS[0]} is in use; using port ${FEEDER_PORT} for the synthetic feeder"
        fi
    fi
fi

# Create config.yaml from example if it doesn't exist (preserves existing config on upgrade)
if [ ! -f "${INSTALL_DIR}/config.yaml" ]; then
    if [ -f "${INSTALL_DIR}/config.yaml.example" ]; then
        cp "${INSTALL_DIR}/config.yaml.example" "${INSTALL_DIR}/config.yaml"
        # Same ownership fix — cp as root would leave config.yaml root-owned,
        # blocking the in-UI config editor from writing changes.
        chown "${USER}:${SVC_GROUP}" "${INSTALL_DIR}/config.yaml" 2>/dev/null || true
        echo -e "  ${GREEN}✓${RESET} Created config.yaml from example (edit it to set your receiver IP)"

        # v3.1.0: when --demo is used AND we just created a fresh
        # config.yaml from the example, patch the receiver target to
        # the synthetic feeder + flip demo.enabled=true. Lets a
        # manual zip install run `./install.sh --demo` without
        # needing the bootstrap. On upgrade paths (config.yaml
        # already existed) we leave the file alone — the in-app
        # switch-to-real wizard is the path for those.
        if [ "$DEMO_MODE" = "true" ]; then

            DEMO_HOME_LAT="$HOME_LAT" DEMO_HOME_LON="$HOME_LON" DEMO_FEEDER_PORT="$FEEDER_PORT" \
            python3 - "${INSTALL_DIR}/config.yaml" <<'PYEOF'
import os, re, sys
path = sys.argv[1]
home_lat = os.environ.get("DEMO_HOME_LAT", "40.0")
home_lon = os.environ.get("DEMO_HOME_LON", "-75.0")
feeder_port = os.environ.get("DEMO_FEEDER_PORT", "8080")
with open(path, "r", encoding="utf-8") as f:
    text = f.read()
# receiver.ip → 127.0.0.1 (first match is receiver.ip; same pattern bootstrap uses)
text = re.sub(r'^(\s*ip:\s*)"[^"]*"(.*)$', r'\1"127.0.0.1"\2',
              text, count=1, flags=re.M)
# receiver.port → resolved FEEDER_PORT (v3.4.40: was hardcoded 8080)
text = re.sub(r'^(\s*port:\s*)\S+(\s.*)?$',
              lambda m: f'{m.group(1)}{feeder_port}{m.group(2) or ""}',
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
# ExecStart/ExecStop quote the interpreter path so an install folder with a
# space works; WorkingDirectory takes the rest of the line verbatim and must
# stay UNQUOTED (systemd refuses a quoted one). No Group=: systemd uses the
# account's primary group, which is not always a group named after it.
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << SERVICEEOF
[Unit]
Description=Aerodrome — ADS-B Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart="${INSTALL_DIR}/venv/bin/python3" main.py start
ExecStop="${INSTALL_DIR}/venv/bin/python3" main.py stop
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
WorkingDirectory=${INSTALL_DIR}
ExecStart="${INSTALL_DIR}/venv/bin/python3" -m tools.synthetic_feeder.serve \\
    --host 127.0.0.1 \\
    --port ${FEEDER_PORT:-8080} \\
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

# Aerodrome's system permissions (install_privileged): the service account
# may restart Aerodrome, read its own sudoers file, and run the root-owned
# system helper — whose fixed, checked actions install and manage a LOCAL
# ntfy server (the Notifications tab) and move or remove the demo feeder.
# The rules themselves are in render_sudoers() above.
echo -e "  Setting up Aerodrome's system permissions (system helper + sudoers rule)..."
RULES="$(render_sudoers "$USER")"
NEW_MARK="$(printf '%s\n' "$RULES" | marker_of)"
if ! LIVE_MARK="$(live_marker)"; then
    # Couldn't read the live rules (sudo refused the read): never overwrite
    # what might be newer. Everything else in this run still applies.
    echo -e "  ${YELLOW}⚠${RESET}  Couldn't read the current permission rules (${SUDOERS_FILE}) — left them unchanged."
    echo "     To refresh them: sudo bash $(printf %q "$INSTALL_DIR/install.sh") --privileges-only"
elif [ "$LIVE_MARK" -gt "$NEW_MARK" ] && [ "$ALLOW_DOWNGRADE" != true ]; then
    # Never replace newer rules (a later release's) with this copy's older
    # ones — e.g. after a rollback. Everything else in this run still applies.
    echo -e "  ${YELLOW}⚠${RESET}  Kept the newer permission rules (version ${LIVE_MARK}) this system already has."
    echo "     (After a deliberate rollback, to use this copy's rules instead:"
    echo "      sudo bash $(printf %q "$INSTALL_DIR/install.sh") --privileges-only --allow-privilege-downgrade)"
elif ! install_privileged "$USER" "$RULES"; then
    echo -e "  ${RED}⚠${RESET}  The 'Restart now' button and the ntfy auto-installer may not work."
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
# Use restart (not start) so re-running install.sh on an already-running install
# actually picks up the new code/unit — `start` is a no-op on a live service.
# `restart` is identical to `start` on a fresh install (the unit was just enabled
# above) and correct on re-runs. sudoers grants restart for both services.
if [ "$DEMO_MODE" = "true" ]; then
    sudo systemctl restart ${FEEDER_SERVICE_NAME}
fi
sudo systemctl restart ${SERVICE_NAME}
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
