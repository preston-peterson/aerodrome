"""privhelper.py — the one module that builds a sudo command line (v3.4.150).

Aerodrome's service account may run, as root, only:
  - /usr/local/libexec/aerodrome/aerodrome-helper (any arguments: the helper,
    a root-owned file install.sh puts there, checks every one of them) —
    run_helper();
  - `systemctl restart [--no-block] aerodrome` and `cat` of its own sudoers
    file — run_sudo().
Release gate: scripts/check_installers.py sudo-callsites (a list starting
with "sudo" appears in no other module). Stdlib only, no app imports, so
server, ntfy_installer and collector can all import it.
"""
import json
import os
import shlex
import subprocess
from pathlib import Path

HELPER = Path("/usr/local/libexec/aerodrome/aerodrome-helper")
INSTALL_DIR = Path(__file__).resolve().parent
TIMEOUT = 90
TIMEOUTS = {"ntfy-install": 300,       # a download: give it time
            "version": 15}             # read-only; asked by status checks
STILL_RUNNING = ("Still working — the download can take a few minutes. "
                 "Check the status again in a minute, and try again if nothing has changed.")


def live_recovery_command(install_dir=None):
    """The command that refreshes this install's system permissions (its
    scripts/recover-sudoers.sh; it also picks up a staged update's newer
    rules). install_dir: the install's root; default this module's own."""
    inst = Path(install_dir) if install_dir else INSTALL_DIR
    return f"sudo bash {shlex.quote(str(inst / 'scripts' / 'recover-sudoers.sh'))}"


class HelperError(Exception):
    """str(e) is a message for people; payload is the helper's JSON reply
    (empty when there was none)."""

    def __init__(self, message, payload=None, command=None):
        super().__init__(message)
        self.payload = payload or {}
        self.command = command


class HelperUnavailable(HelperError):
    """sudo didn't run the helper (no rule for it yet, no helper, no sudo):
    the permissions need a refresh. .command is the one that does it."""


class HelperStillRunning(HelperError):
    """The call timed out. The helper may still be at it (holding its lock:
    a call meanwhile is refused as busy) — or it was stopped part-way, as
    one Aerodrome started is when aerodrome.service restarts. Then the next
    ntfy action first settles what it left: kept when ntfy answers on it,
    put back when it doesn't."""


class HelperRefused(HelperError):
    """Exit 3: the helper wrote nothing (custom_keys, problems, busy, …)."""


class HelperFailed(HelperError):
    """Exit 1 (failed) or 2 (bad usage)."""


def _env():
    # Messages from sudo and the tools stay English whatever the locale; the
    # outcome is told from the reply's shape, never from its words.
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def run_sudo(argv, timeout):
    """`sudo -n <argv>` — for the restart and cat rules only."""
    return subprocess.run(["sudo", "-n", *argv], capture_output=True, text=True,
                          timeout=timeout, env=_env())


def _reply(stdout):
    """The helper's one JSON line (the last line of stdout), or None."""
    lines = stdout.strip().splitlines() if isinstance(stdout, str) else []
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    return reply if isinstance(reply, dict) and "ok" in reply else None


def run_helper(sub, *args):
    """Run the helper's `sub` action; return its JSON reply when it worked.
    Raises HelperUnavailable when no helper answered (no JSON line: sudo
    refused, in any language, or there is no sudo or no helper),
    HelperStillRunning on a timeout, HelperRefused on exit 3 and
    HelperFailed on any other non-zero exit — and when sudo itself couldn't
    be started (not executable, or no process to spare)."""
    unavailable = HelperUnavailable("Aerodrome's system permissions need a refresh",
                                    command=live_recovery_command())
    try:
        r = run_sudo([str(HELPER), sub, *args], timeout=TIMEOUTS.get(sub, TIMEOUT))
    except FileNotFoundError:
        raise unavailable from None
    except OSError as e:
        raise HelperFailed(f"couldn't run sudo: {e}") from None
    except subprocess.TimeoutExpired:
        raise HelperStillRunning(STILL_RUNNING) from None
    reply = _reply(r.stdout)
    if reply is None:
        raise unavailable
    if r.returncode == 0 and reply.get("ok"):
        return reply
    message = str(reply.get("message") or f"the system helper's {sub} failed")
    if r.returncode == 3:
        raise HelperRefused(message, reply)
    raise HelperFailed(message, reply)


def helper_file_version():
    """The installed helper's PRIV_VERSION, from its line 2 (the file is
    0755: no sudo needed). 0 when it is missing or unreadable."""
    try:
        with open(HELPER, encoding="utf-8") as f:
            f.readline()
            line = f.readline()
    except (OSError, UnicodeDecodeError):
        return 0
    head, _, n = line.strip().partition("=")
    return int(n) if head.strip() == "# PRIV_VERSION" and n.strip().isdigit() else 0
