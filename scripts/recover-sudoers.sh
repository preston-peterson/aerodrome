#!/usr/bin/env bash
# =============================================================================
# Aerodrome — refresh system permissions (the sudoers rule)
# =============================================================================
#
# A hand-off with no rules of its own: install.sh owns Aerodrome's permission
# rules. This finds the install.sh — staged or installed — that carries the
# NEWEST rules and runs it in --privileges-only mode, which rewrites only
# /etc/sudoers.d/aerodrome (validated first, swapped in all at once), keeps
# the account Aerodrome runs as, and never replaces newer rules with older
# ones. No packages, venv, units or restart.
#
# USAGE (as root):
#   # The command Aerodrome's Updates page shows when an update is staged:
#   sudo bash <install>/update/scripts/recover-sudoers.sh --install-dir <install>
#
#   # For the installed release (also lifts a staged update's gate when the
#   # staged copy has newer rules):
#   sudo bash <install>/scripts/recover-sudoers.sh
#
#   # Older Updates pages show this form; it still works:
#   sudo AERODROME_INSTALL_DIR=<install> bash <install>/update/scripts/recover-sudoers.sh
#
#   # Piped (no file on disk). With neither AERODROME_INSTALL_DIR nor
#   # --install-dir, the install dir defaults to /opt/aerodrome:
#   curl -fsSL <raw url of this file> | sudo AERODROME_INSTALL_DIR=<install> bash
#
# Where it looks for install.sh: this script's own tree, <install>/update/,
# <install>/update/aerodrome/ and <install>/. It uses the candidate whose
# first version marker is highest (a tie keeps the earlier one in that
# list), among those that support --privileges-only (v3.4.145 and later).
#
# Structure: everything is inside main(), called on the LAST line within a
# { …; } group — a download cut short anywhere runs nothing.
# =============================================================================

# $1 = this file's path ("" when piped); the rest = the command-line flags.
main() {
    set -euo pipefail
    local self="$1"
    shift

    INSTALL_DIR="${AERODROME_INSTALL_DIR:-}"
    while [ $# -gt 0 ]; do
        case "$1" in
            --install-dir)
                [ $# -ge 2 ] && [ -n "$2" ] || { echo "--install-dir needs a directory" >&2; exit 2; }
                INSTALL_DIR="$2"; shift 2 ;;
            --allow-privilege-downgrade)
                echo "recover-sudoers.sh never replaces newer permission rules. After a deliberate" >&2
                echo "rollback, run the command install.sh printed, or:" >&2
                echo "  sudo bash <install>/install.sh --privileges-only --allow-privilege-downgrade" >&2
                exit 2 ;;
            -h|--help)
                echo "usage: sudo bash recover-sudoers.sh [--install-dir DIR]"
                echo "Refreshes Aerodrome's system permissions (sudoers rule) only, using the"
                echo "newest rules among the installed and staged copies of Aerodrome."
                exit 0 ;;
            *)
                echo "usage: sudo bash recover-sudoers.sh [--install-dir DIR]" >&2
                exit 2 ;;
        esac
    done

    if [ "$(id -u)" -ne 0 ]; then
        echo "This must run as root:  sudo bash $(printf %q "${self:-recover-sudoers.sh}")${INSTALL_DIR:+ --install-dir $(printf %q "$INSTALL_DIR")}" >&2
        exit 1
    fi

    # This script's own Aerodrome tree, when it runs from a file (not piped).
    here=""
    if [ -n "$self" ] && [ -f "$self" ]; then
        here="$(cd "$(dirname "$self")/.." && pwd -P)"
    fi

    # Which install: --install-dir / AERODROME_INSTALL_DIR, else derived from
    # this file's location (a staged copy lives under <install>/update/), else
    # this file's own tree; piped with neither: /opt/aerodrome.
    if [ -z "$INSTALL_DIR" ] && [ -n "$here" ]; then
        d="$here"
        while [ "$d" != / ] && [ -n "$d" ]; do
            if [ "$(basename "$d")" = update ] && [ -f "$(dirname "$d")/VERSION" ] \
                    && [ -f "$(dirname "$d")/main.py" ]; then
                INSTALL_DIR="$(dirname "$d")"
                break
            fi
            d="$(dirname "$d")"
        done
        [ -n "$INSTALL_DIR" ] || INSTALL_DIR="$here"
    fi
    [ -n "$INSTALL_DIR" ] || INSTALL_DIR=/opt/aerodrome
    if [ ! -f "$INSTALL_DIR/VERSION" ] || [ ! -f "$INSTALL_DIR/main.py" ]; then
        echo "No Aerodrome install at $INSTALL_DIR. Say where it is: --install-dir DIR" >&2
        exit 2
    fi
    INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"

    # The first version marker of a file (awk reads the whole file: no early
    # exit on the right of a pipe). Prints nothing when there is none.
    first_marker_of() {
        awk '!f && /^[[:space:]]*#[[:space:]]*SUDOERS_VERSION[[:space:]]*:[[:space:]]*[0-9]/ {
                 v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); sub(/[^0-9].*$/, "", v); print v; f = 1 }' "$1"
    }

    best="" best_v=-1
    for c in ${here:+"$here/install.sh"} "$INSTALL_DIR/update/install.sh" \
             "$INSTALL_DIR/update/aerodrome/install.sh" "$INSTALL_DIR/install.sh"; do
        [ -f "$c" ] || continue
        grep -qF -- '--privileges-only' "$c" || continue    # pre-v3.4.145: can't refresh only
        v="$(first_marker_of "$c")" || v=""
        [ -n "$v" ] || v=0
        if [ "$v" -gt "$best_v" ]; then best="$c"; best_v="$v"; fi
    done

    if [ -z "$best" ]; then
        echo "No install.sh that can refresh permissions only was found for $INSTALL_DIR" >&2
        echo "(the installed release predates v3.4.145)." >&2
        echo "Stage the update on Aerodrome's Updates page first, then run this again." >&2
        exit 1
    fi
    echo "Refreshing Aerodrome's system permissions with $best (rules version $best_v)"
    exec bash "$best" --privileges-only --install-dir "$INSTALL_DIR"
}

# Nothing above has run yet: a truncated download stops here (or earlier,
# mid-definition) and runs nothing; a cut inside this { …; } can't parse.
{ main "${BASH_SOURCE[0]:-}" "$@"; }
