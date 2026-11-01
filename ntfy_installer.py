"""
ntfy_installer.py — Install, detect, upgrade, and uninstall the ntfy server
alongside Aerodrome.

Design principles:
  - Idempotent. Running install() when already installed is a no-op that
    returns the current state. Same for uninstall() when not installed.
  - Explicit. install_status() returns a dict describing every file and
    service we'd touch. No hidden state.
  - Verifiable. Downloads are checked against the SHA256 digests GitHub
    publishes for each release asset.
  - Respectful of externally-managed installs. If the user installed
    ntfy themselves (via apt, snap, a Go build, etc.) and it's running
    at a path we don't recognize, we report that and offer no
    install/uninstall actions — only "use existing".
  - No sudo inside this module. Every change to the system — the binary,
    the unit, server.yml, the message cache — is one call to Aerodrome's
    root-owned helper (privhelper.run_helper: scripts/aerodrome-helper,
    installed by install.sh), which checks every argument and writes the
    unit and server.yml together. Reading is done here, without sudo.
    Callers are server.py endpoints running as the aerodrome user.

Paths (the helper writes them; this module only reads):
  /usr/local/bin/ntfy                    — the binary
  /etc/ntfy/server.yml                   — the config (0644)
  /etc/systemd/system/ntfy.service       — the unit (NTFY_UNIT_TEXT: an
                                           unprivileged, sandboxed service)
  /var/lib/ntfy/aerodrome-installed      — the install stamp
  /var/lib/aerodrome-ntfy/cache.db       — ntfy's message cache (the unit's
                                           private state directory; only
                                           root and ntfy can see it)

Public API:
  install_status() → dict
      Describes what's present on disk + whether the service is running.
  install(port: int = 2586, bind: str = "0.0.0.0", topic: str = None) → dict
      Download, verify, install, configure, start. Idempotent.
  update_config(base_url, upstream_relay, port, bind, drop_custom_keys) → dict
      Re-render server.yml from Aerodrome's managed values and restart.
      Refuses (writes nothing) while the file has settings Aerodrome
      doesn't manage, unless drop_custom_keys names every one of them.
      An install still on the older root-run unit moves to the sandboxed
      one in the same step.
  current_config() → dict | None
      classify_server_yml() of the live server.yml (names only).
  upgrade() → dict
      Pull the latest release and swap the binary. Restarts the service.
  uninstall() → dict
      Stop, disable, remove files. Does not remove the binary if it's
      a system-managed package (we never touch apt-installed ntfy).
  latest_version() → str
      Query the GitHub API for the latest tag. Cached per process.
Every helper error comes back as {'ok': False, 'message', ...}: see
_helper_failure() for the codes.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import requests

import privhelper

log = logging.getLogger("aerodrome.ntfy_installer")

# ---------------------------------------------------------------
# Paths we manage. All absolute; no variation.
# ---------------------------------------------------------------
BINARY_PATH = Path("/usr/local/bin/ntfy")
CONFIG_DIR = Path("/etc/ntfy")
CONFIG_FILE = CONFIG_DIR / "server.yml"
SYSTEMD_UNIT = Path("/etc/systemd/system/ntfy.service")
# Stamp file — presence indicates Aerodrome-managed install vs external.
# Contents are the ntfy version string we installed.
STAMP_FILE = Path("/var/lib/ntfy/aerodrome-installed")
STAMP_DIR = STAMP_FILE.parent

# ---------------------------------------------------------------
# server.yml: read, validate, render (v3.4.146). One block, kept verbatim so
# a privileged helper can carry the same copy; every value is validated and
# JSON-quoted, so no input can start a new key.
# ---------------------------------------------------------------
# --- BEGIN shared ntfy-yml block (byte-identical in ntfy_installer.py and scripts/aerodrome-helper) ---
# Stdlib only (re, json), Python 3.8 syntax and APIs, so a privileged helper
# can run the same block under the system interpreter. A change here changes
# what root writes.
NTFY_MANAGED_VARIABLE = ("base-url", "listen-http", "upstream-base-url")
NTFY_MANAGED_FIXED = {"cache-duration": "12h", "behind-proxy": False}
NTFY_CACHE_FILES = ("/var/lib/ntfy/cache.db", "/var/lib/aerodrome-ntfy/cache.db")
NTFY_UPSTREAM = "https://ntfy.sh"
# The unit (v3.4.150): ntfy as an unprivileged, sandboxed service. Its
# --cache-file wins over a cache-file line in server.yml, so a server.yml
# written for the older root-run unit runs under this one, and the other way
# round. Only the helper writes it; the app compares the live unit (0644)
# with it to tell the older setup, without sudo.
NTFY_UNIT_TEXT = """[Unit]
Description=ntfy server (managed by Aerodrome)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/ntfy serve --config /etc/ntfy/server.yml --cache-file /var/lib/aerodrome-ntfy/cache.db
DynamicUser=yes
StateDirectory=aerodrome-ntfy
ProtectSystem=strict
ProtectHome=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
# Aerodrome's ntfy paths, then an official package's. Any of them without
# Aerodrome's stamp is someone else's ntfy, never touched.
NTFY_OWN_PATHS = ("/usr/local/bin/ntfy", "/etc/ntfy/server.yml",
                  "/etc/systemd/system/ntfy.service")
NTFY_VENDOR_PATHS = ("/usr/bin/ntfy", "/lib/systemd/system/ntfy.service",
                     "/usr/lib/systemd/system/ntfy.service")
_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
_HOST = r"(?:" + _LABEL + r"(?:\." + _LABEL + r")*|\[[0-9A-Fa-f:.]{2,45}\])"
_URL_RE = re.compile(r"\Ahttps?://" + _HOST + r"(?::([0-9]{1,5}))?(?:/[A-Za-z0-9._~/-]*)?\Z")
_LISTEN_RE = re.compile(r"\A(0\.0\.0\.0|127\.0\.0\.1)?:([0-9]{1,5})\Z")
# Plain scalars YAML would read as something other than a string: numbers in
# any spelling (1.1 and 1.2), dates and timestamps, merge/value keys and the
# 1.1 booleans. A superset of PyYAML's implicit resolvers; refused, not guessed.
# No two quantifiers here can split the same run of characters, so a match
# takes linear time on any input.
_TYPED_RE = re.compile(
    r"\A(?:[-+]?(?:[0-9_]+|(?:[0-9][0-9_]*)?\.[0-9_]+)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?[0-9][0-9_]*\.[0-9_]*(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?0[xXoObB][0-9A-Fa-f_]+"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ].*)?"
    r"|<<|="
    r"|[Yy]|[Nn]|[Yy][Ee][Ss]|[Nn][Oo]|[Oo][Nn]|[Oo][Ff][Ff])\Z")
_KEY_RE = re.compile(r"\A([A-Za-z0-9_][A-Za-z0-9_-]*) *:(?: +(.*))?\Z")
_DOC_START_RE = re.compile(r"\A--- *(?: #.*)?\Z")
_SCALAR_MAX = 1024      # longer than any value Aerodrome manages (base-url <= 255)


def base_url_ok(s):
    """True for http(s)://host[:port][/path] with nothing YAML or a shell
    could misread: no quotes, spaces, control characters, query or fragment."""
    if not isinstance(s, str) or len(s) > 255:
        return False
    m = _URL_RE.match(s)
    return bool(m) and (m.group(1) is None or 1 <= int(m.group(1)) <= 65535)


def _after_quoted(rest):
    """What may follow a closing quote: nothing, or spaces and a comment."""
    return rest.strip(" ") == "" or re.match(r" +#", rest) is not None


def _yaml_scalar(raw):
    """(ok, value) for one flat YAML scalar: "double", 'single' or plain,
    with an optional trailing ' # comment'. Anything fancier is not ok."""
    if len(raw) > _SCALAR_MAX or "\t" in raw:
        return False, None              # over-long: not a managed value; tabs: YAML parsers disagree
    s = raw.strip(" ")
    if s[:1] == '"':
        i = 1
        while i < len(s) and s[i] != '"':
            i += 2 if s[i] == "\\" else 1
        if i >= len(s) or not _after_quoted(s[i + 1:]):
            return False, None
        try:
            return True, json.loads(s[:i + 1])
        except ValueError:
            return False, None
    if s[:1] == "'":
        i = 1
        while i < len(s):
            if s[i] == "'":
                if s[i + 1:i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        if i >= len(s) or not _after_quoted(s[i + 1:]):
            return False, None
        return True, s[1:i].replace("''", "'")
    m = re.search(r" #", s)
    if m:
        s = s[:m.start()].rstrip(" ")
    if s in ("", "~", "null", "Null", "NULL"):
        return True, None
    if s[0] in "!&*|>{}[]%@`,?-#'\"" or ": " in s or s.endswith(":"):
        return False, None
    if s in ("true", "True", "TRUE"):
        return True, True
    if s in ("false", "False", "FALSE"):
        return True, False
    if _TYPED_RE.match(s) or any(ord(c) < 32 or ord(c) == 127 for c in s):
        return False, None              # numbers, dates, 1.1 booleans: a human decides
    return True, s


def classify_server_yml(text):
    """Split an ntfy server.yml into what Aerodrome can carry over and what it
    can't. Returns {port, bind, base_url, upstream, custom, problems}:
    custom   = names of keys Aerodrome doesn't manage, or managed keys set to
               something other than Aerodrome's value (dropping them changes
               behaviour, so a human must agree);
    problems = base-url / listen-http / upstream-base-url values that can't
               be carried over (missing, malformed, duplicated, continued on
               an indented line, or failing validation).
    The same shapes on the fixed keys (cache-file, cache-duration,
    behind-proxy) count as custom. Names only, never values.
    As YAML does, a leading byte-order mark and a "---" before the first key
    are skipped; a tab in a line's indentation (YAML refuses the file) makes
    that line custom."""
    managed = NTFY_MANAGED_VARIABLE + tuple(NTFY_MANAGED_FIXED) + ("cache-file",)
    vals, custom, problems, seen, last = {}, [], [], set(), None
    started = False
    if text[:1] == "\ufeff":
        text = text[1:]
    for n, line in enumerate(text.splitlines(), 1):
        if "\t" in line[:len(line) - len(line.lstrip())]:
            custom.append("line %d" % n)     # even a blank or comment line
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not started and _DOC_START_RE.match(line):
            started = True                   # one document start marker, before any key
            continue
        started = True
        if line[0] in " \t":                 # nested under / continuing the previous key
            if last in NTFY_MANAGED_VARIABLE:
                problems.append(last)       # its value isn't the one-line scalar...
                custom.append("line %d" % n)  # ...and the extra line is someone's edit
            elif last in managed or last is None:
                custom.append(last or "line %d" % n)
            continue
        m = _KEY_RE.match(line.rstrip(" "))
        if not m:
            custom.append("line %d" % n)
            last = None
            continue
        key, last = m.group(1), m.group(1)
        if key in seen:
            (problems if key in NTFY_MANAGED_VARIABLE else custom).append(key)
            continue
        seen.add(key)
        ok, val = _yaml_scalar(m.group(2) or "")
        if key in NTFY_MANAGED_VARIABLE:
            if not ok:
                problems.append(key)
            else:
                vals[key] = val
        elif key == "cache-file":
            if not ok or val not in NTFY_CACHE_FILES:
                custom.append(key)
        elif key in NTFY_MANAGED_FIXED:
            want = NTFY_MANAGED_FIXED[key]
            if not ok or val != want or type(val) is not type(want):
                custom.append(key)
        else:
            custom.append(key)
    for key in problems:                     # a flagged value is never carried
        vals.pop(key, None)
    out = {"port": None, "bind": None, "base_url": None, "upstream": False}
    if base_url_ok(vals.get("base-url")):
        out["base_url"] = vals["base-url"]
    else:
        problems.append("base-url")
    lm = _LISTEN_RE.match(vals["listen-http"]) if isinstance(vals.get("listen-http"), str) else None
    if lm and 1 <= int(lm.group(2)) <= 65535:
        out["port"], out["bind"] = int(lm.group(2)), lm.group(1) or "0.0.0.0"
    else:
        problems.append("listen-http")
    if "upstream-base-url" in vals:
        if vals["upstream-base-url"] == NTFY_UPSTREAM:
            out["upstream"] = True
        else:
            custom.append("upstream-base-url")
    out["custom"] = sorted(set(custom))
    out["problems"] = sorted(set(problems))
    return out


def render_server_yml(port, bind, base_url, upstream, cache_file=None):
    """The whole file, from validated values only. Every value is a
    JSON-quoted scalar, so no input can start a new key."""
    if type(port) is not int or not 1 <= port <= 65535 or bind not in ("0.0.0.0", "127.0.0.1"):
        raise ValueError("bad port or bind")
    if not base_url_ok(base_url) or (cache_file is not None and cache_file not in NTFY_CACHE_FILES):
        raise ValueError("bad base-url or cache-file")
    pairs = [("base-url", base_url), ("listen-http", "%s:%d" % (bind, port))]
    if cache_file is not None:
        pairs.append(("cache-file", cache_file))
    pairs += [("cache-duration", "12h"), ("behind-proxy", False)]
    if upstream:
        pairs.append(("upstream-base-url", NTFY_UPSTREAM))
    return "\n".join(
        ["# Aerodrome-managed ntfy config. Saving in Aerodrome's Notifications tab rewrites",
         "# this file; settings you add here are named there and never removed silently."]
        + ["%s: %s" % (k, json.dumps(v)) for k, v in pairs]) + "\n"
# --- END shared ntfy-yml block ---

# An official package's ntfy (as Paths, so tests can move them).
VENDOR_PATHS = tuple(Path(p) for p in NTFY_VENDOR_PATHS)

# ---------------------------------------------------------------
# Release fetch
# ---------------------------------------------------------------
GITHUB_RELEASES_LATEST = "https://api.github.com/repos/binwiederhier/ntfy/releases/latest"
_version_cache: Optional[str] = None


def latest_version() -> Optional[str]:
    """Fetch the latest ntfy version from GitHub. Returns e.g. '2.11.0'
    (no leading 'v'). Returns None on failure. Cached for process lifetime."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    try:
        r = requests.get(GITHUB_RELEASES_LATEST, timeout=10,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        tag = r.json().get("tag_name", "")
        if not tag:
            return None
        v = tag[1:] if tag.startswith("v") else tag
        _version_cache = v
        return v
    except Exception as e:
        log.warning("Failed to fetch latest ntfy version: %s", e)
        return None


# ---------------------------------------------------------------
# Architecture detection
# ---------------------------------------------------------------

def _detect_arch() -> Optional[str]:
    """Map the current machine to the ntfy release artifact suffix.
    Returns one of 'linux_amd64', 'linux_arm64', 'linux_armv7', or None."""
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "linux_amd64"
    if m in ("aarch64", "arm64"):
        return "linux_arm64"
    if m in ("armv7l", "armv7"):
        return "linux_armv7"
    return None


# ---------------------------------------------------------------
# Status detection
# ---------------------------------------------------------------

def _file_info(path: Path) -> Dict[str, Any]:
    """Small helper: present-ness + mtime for reporting. Never raises: a
    path in a folder this account can't search (an official package's 0700
    /var/lib/ntfy) reads as absent, as the helper's os.path.lexists() does."""
    info = {"present": os.path.lexists(path), "path": str(path)}
    try:
        st = path.stat()
        info.update(size=st.st_size, mtime=int(st.st_mtime))
    except OSError:
        pass
    return info


def _systemd_service_status(unit: str = "ntfy") -> Dict[str, Any]:
    """Report systemd state for the ntfy service. Returns dict with
    'active' (bool), 'enabled' (bool), 'raw' (raw systemctl output)."""
    active = False
    enabled = False
    raw_active = ""
    raw_enabled = ""
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=5)
        raw_active = (r.stdout + r.stderr).strip()
        active = r.returncode == 0 and raw_active == "active"
    except Exception as e:
        raw_active = f"error: {e}"
    try:
        r = subprocess.run(["systemctl", "is-enabled", unit],
                           capture_output=True, text=True, timeout=5)
        raw_enabled = (r.stdout + r.stderr).strip()
        enabled = r.returncode == 0 and "enabled" in raw_enabled
    except Exception as e:
        raw_enabled = f"error: {e}"
    return {"active": active, "enabled": enabled,
            "raw_active": raw_active, "raw_enabled": raw_enabled}


def _binary_version() -> Optional[str]:
    """Return the installed ntfy version, e.g. '2.21.0'. Returns None
    if ntfy isn't installed or the version can't be determined.

    v2.40.5 rewrite. ntfy has never had a clean `ntfy version` command
    (proven the hard way) — `version` isn't a subcommand and `--version`
    isn't a flag. The version IS buried in the help output, but that's
    fragile. Better: ntfy 2.17+ exposes GET /v1/version as a proper HTTP
    endpoint, which is what we use now. Falls back to parsing the help
    output's version line for older ntfy or when the server is stopped.
    """
    if not BINARY_PATH.exists():
        return None

    # --- Primary: ask the running ntfy server via HTTP ---
    # Server is listening on whatever port the config file says. Read
    # server.yml to get the exact port (user may have changed it from
    # the default 2586). If we can't read the config or the server's
    # not running, fall through to the CLI parse.
    v = _version_via_http()
    if v:
        return v

    # --- Fallback: parse the CLI help output ---
    # Running `ntfy` with no args dumps help to STDOUT, and somewhere in
    # there is a line like "ntfy 2.21.0 (7ce5e8a), runtime go1.25.8, ..."
    # We find that line by looking for "ntfy N.N.N" as the leading tokens.
    # Also try invalid-flag paths in case the layout changes in the future.
    for args in (["--help"], []):
        try:
            r = subprocess.run([str(BINARY_PATH)] + args,
                               capture_output=True, text=True, timeout=5)
            # Help output may be on stdout OR stderr depending on invocation;
            # newer urfave/cli can split. Scan both.
            for text in (r.stdout or "", r.stderr or ""):
                for line in text.splitlines():
                    line = line.strip()
                    # Match lines that START with "ntfy " + a digit. The
                    # version line format across 2.x: "ntfy 2.21.0 (hash)..."
                    m = re.match(r"^ntfy\s+(\d+\.\d+\.\d+)\b", line)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    return None


def _version_via_http() -> Optional[str]:
    """Hit the ntfy server's /v1/version endpoint and return the version.
    Returns None if the server isn't listening, the port can't be read
    from config, or the response doesn't parse. Quick 2s timeout so a
    stopped service doesn't stall the status UI."""
    port = _read_server_port()
    if not port:
        return None
    try:
        r = requests.get(f"http://127.0.0.1:{port}/v1/version", timeout=2)
        if r.status_code != 200:
            return None
        data = r.json()
        # /v1/version returns JSON like {"version": "2.21.0", "commit": "...", "date": "..."}
        v = str(data.get("version", "") or "")
        if v.startswith("v"):
            v = v[1:]
        # Sanity check — "version" field should look like N.N.N
        if re.match(r"^\d+\.\d+\.\d+$", v):
            return v
        return None
    except Exception:
        return None


BASE_URL_ERROR = ("Server address must look like http://host:port — "
                  "no spaces, quotes or extra parts")


def validate_base_url(s) -> str:
    """Return s when it is a plain http(s)://host[:port][/path] address
    (the shared block's base_url_ok); raise ValueError otherwise. Every
    path that writes base-url — the endpoints, install(), update_config()
    and restore — goes through this or the renderer's own check."""
    if not base_url_ok(s):
        raise ValueError(BASE_URL_ERROR)
    return s


def current_config() -> Optional[Dict[str, Any]]:
    """classify_server_yml() of the live server.yml, or None when it is
    missing or unreadable. Names only in custom/problems, never values."""
    try:
        return classify_server_yml(CONFIG_FILE.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _read_server_port() -> Optional[int]:
    """The listen port from server.yml's listen-http, or None when the file
    is missing or the value can't be read exactly as YAML would."""
    cur = current_config()
    return cur["port"] if cur else None


def install_status() -> Dict[str, Any]:
    """Return a full snapshot of what's present.

    Returned dict keys:
      state: 'not_installed' | 'aerodrome_managed' | 'external' | 'partial'
      binary: {present, path, ...}
      config: {present, path, ...}
      unit:   {present, path, ...}
      stamp:  {present, path, version_installed}
      service: {active, enabled, raw_...}
      version: installed version string (from `ntfy version`) or None
      installable: bool — true if we can install (arch supported, no conflict)
      arch: detected architecture suffix or None
      legacy_unit: (v3.4.150) True while Aerodrome's ntfy still runs from the
                   older root-run unit, False on the sandboxed one
                   (NTFY_UNIT_TEXT), None when there's no unit of ours
    """
    bin_info = _file_info(BINARY_PATH)
    cfg_info = _file_info(CONFIG_FILE)
    unit_info = _file_info(SYSTEMD_UNIT)
    stamp_info = _file_info(STAMP_FILE)
    service_info = _systemd_service_status()
    version = _binary_version()
    arch = _detect_arch()

    # Decide state — the helper's own answer (its _ntfy_state()), with a
    # stamped install that's missing a file told apart:
    #   - aerodrome_managed: the stamp and all three of our files
    #   - partial: the stamp, but a file is missing — needs repair
    #   - external: no stamp, but one of our paths or an official package's
    #     (VENDOR_PATHS) → someone else's ntfy, never touched
    #   - not_installed: none of them
    has_stamp = stamp_info["present"]
    has_bin = bin_info["present"]
    has_cfg = cfg_info["present"]
    has_unit = unit_info["present"]

    if has_stamp:
        state = "aerodrome_managed" if has_bin and has_cfg and has_unit else "partial"
    elif has_bin or has_cfg or has_unit or any(os.path.lexists(p) for p in VENDOR_PATHS):
        state = "external"
    else:
        state = "not_installed"

    # Stamp file may contain the version we wrote at install time
    stamp_version = None
    if has_stamp:
        try:
            stamp_version = STAMP_FILE.read_text().strip()
        except Exception:
            stamp_version = None
    stamp_info["version_installed"] = stamp_version

    legacy_unit = None
    if has_stamp and has_unit:
        try:
            legacy_unit = SYSTEMD_UNIT.read_text(encoding="utf-8") != NTFY_UNIT_TEXT
        except (OSError, UnicodeDecodeError):
            pass

    installable = (arch is not None) and state in ("not_installed", "partial")

    return {
        "state": state,
        "binary": bin_info,
        "config": cfg_info,
        "unit": unit_info,
        "stamp": stamp_info,
        "service": service_info,
        "version": version,
        "installable": installable,
        "arch": arch,
        "legacy_unit": legacy_unit,
    }


# ---------------------------------------------------------------
# Install
# ---------------------------------------------------------------

def _generate_topic() -> str:
    """Random hard-to-guess topic name. 8 urlsafe characters (~48 bits),
    prefixed with 'aerodrome-' so it's identifiable in the ntfy logs."""
    suffix = secrets.token_urlsafe(6)  # ~8 chars after base64url encoding
    return f"aerodrome-{suffix}"


PAUSED_MESSAGE = ("Notification-server changes are paused until Aerodrome's system "
                  "permissions are refreshed — run `{command}` on the server.")
BUSY_MESSAGE = "Another Aerodrome system task is still running — try again in a minute."


def _custom_keys_message(names) -> str:
    return ("Your notification server's config file has settings Aerodrome "
            f"doesn't manage: {', '.join(names)}. Saving would remove them.")


def _helper_failure(e: privhelper.HelperError, **extra) -> Dict[str, Any]:
    """The result dict for a helper error (v3.4.150). Codes:
      helper_unavailable — no helper answered: the system permissions need a
                           refresh; `command` is the one that does it
      still_running      — the call timed out; the helper may still be at it
                           (or was stopped part-way: the next ntfy action
                           first settles what it left — kept when ntfy
                           answers on it, put back when it doesn't)
      busy               — another system task holds the helper's lock
      custom_keys        — server.yml has settings Aerodrome doesn't manage
                           (custom_keys: names) the caller didn't confirm
    and no code for anything else (message = the helper's)."""
    if isinstance(e, privhelper.HelperUnavailable):
        return {"ok": False, "code": "helper_unavailable", "command": e.command,
                "message": PAUSED_MESSAGE.format(command=e.command), **extra}
    if isinstance(e, privhelper.HelperStillRunning):
        return {"ok": False, "code": "still_running", "still_running": True,
                "message": str(e), **extra}
    reply = e.payload
    if reply.get("busy"):
        return {"ok": False, "code": "busy", "busy": True, "message": BUSY_MESSAGE, **extra}
    if reply.get("custom_keys") and not reply.get("problems"):
        return {"ok": False, "code": "custom_keys", "custom_keys": reply["custom_keys"],
                "message": _custom_keys_message(reply["custom_keys"]), **extra}
    return {"ok": False, "message": str(e), **extra}


def _detect_lan_ip() -> Tuple[Optional[str], Optional[str]]:
    """Return (ip, error_reason) — best-guess LAN IP for this host, or
    (None, reason_string) if we can't figure one out. This is used as
    the default base-url at install time so the phone app can reach
    the ntfy server without pointing at 'localhost' (which would mean
    the phone itself). The user can always override this in the
    Notifications config UI later.

    Strategy: open a UDP socket to a non-routable address and ask which
    local interface the kernel would use. This doesn't actually send any
    packets — it's a pure routing-table lookup. Works on Linux, macOS,
    and any system with a default route.

    v3.4.27 changes (after an install-time race surfaced this):

    * Retry with backoff. The previous one-shot version returned None
      whenever it happened to be called before networking had settled —
      most notably during a fresh-install bootstrap where the wizard
      fires POST /api/ntfy/install within seconds of DHCP coming up.
      We now retry up to 3 times with 500ms gaps (1.5s total worst
      case), which closes the DHCP-race window without papering over
      genuine no-network situations.

    * Filter pathological results. Some configurations cause connect()
      to "succeed" with a source IP of 0.0.0.0, 127.x.x.x (loopback),
      or 169.254.x.x (link-local autoconf). None of those are usable
      as a base-url for phone clients, so we treat them like detection
      failures and either retry or give up.

    * Return the actual exception text. The previous bare `except`
      hid which error fired (ENETUNREACH? gaierror? something else?).
      Returning (None, reason) lets /api/ntfy/status surface that
      reason in the wizard's warning text, so future debug doesn't
      need an ssh session and a one-liner Python repro.

    Known limitation (unchanged): on multi-homed hosts (Tailscale +
    Ethernet + Docker bridge), the IP returned is "whatever the default
    route points at," which may not be the LAN the phone is on. The
    user can always override in the Notifications config UI.
    """
    import socket
    import time

    last_error: Optional[str] = None
    for attempt in range(3):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # 10.254.254.254 is unreachable in any normal config; the  (pii-ok)
                # kernel still does a route lookup to decide a source IP.
                s.connect(("10.254.254.254", 1))  # pii-ok
                ip = s.getsockname()[0]
            finally:
                s.close()

            # Filter pathological "successes" — these are technically valid
            # getsockname() results but useless as a phone-reachable URL.
            if not ip:
                last_error = "getsockname returned empty"
            elif ip == "0.0.0.0":
                last_error = "kernel returned 0.0.0.0 (no usable interface)"
            elif ip.startswith("127."):
                last_error = f"kernel returned loopback {ip} (no usable interface)"
            elif ip.startswith("169.254."):
                last_error = f"kernel returned link-local {ip} (DHCP not yet bound)"
            else:
                return (ip, None)
        except OSError as e:
            # The interesting one is ENETUNREACH ([Errno 101] Network is
            # unreachable) — typically means we ran before DHCP finished
            # bringing up the default route. Other OSErrors land here too.
            last_error = f"[{type(e).__name__}: errno {e.errno}] {e.strerror or str(e)}"
        except Exception as e:
            last_error = f"[{type(e).__name__}] {e}"

        # Backoff before retrying — but not after the last attempt.
        if attempt < 2:
            time.sleep(0.5)

    return (None, last_error or "unknown error")


def _validate_install_args(port, bind, base_url) -> None:
    """Raise ValueError unless port/bind/base_url are values the renderer
    will accept. Runs before the helper is called."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if bind not in ("0.0.0.0", "127.0.0.1"):
        raise ValueError("bind must be '0.0.0.0' or '127.0.0.1'")
    if base_url is not None:
        validate_base_url(base_url)


def install(port: int = 2586, bind: str = "0.0.0.0",
            topic: Optional[str] = None,
            base_url: Optional[str] = None,
            upstream_relay: bool = True) -> Dict[str, Any]:
    """Install ntfy as a systemd service. Idempotent — if already
    aerodrome_managed, returns current status with no changes. If
    externally managed, refuses (returns error; caller should present
    the external-install message in the UI).

    port  — port for ntfy to listen on. Default 2586.
    bind  — bind address: 0.0.0.0 for LAN-accessible, 127.0.0.1 for
            localhost-only (phone requires Tailscale/VPN).
    topic — subscription topic to present to the user for the phone app.
            Generated randomly if not passed. Not actually used in the
            ntfy config (ntfy has no per-topic auth) — it's just what
            the user subscribes to.
    base_url — (v2.40.5) external URL phones use to reach this server.
            If None, auto-detected from the LAN IP; if detection fails,
            falls back to localhost (the Notifications tab says so and
            offers a fix). Otherwise it must pass validate_base_url().
    upstream_relay — (v2.40.5) if True (default), adds upstream-base-url:
            https://ntfy.sh so iOS clients get real-time APNs push.

    v3.4.150: one helper call (ntfy-install latest + the four settings)
    downloads and verifies the release, installs the binary, writes the
    unit and server.yml together and starts ntfy, checking it answers.

    Returns a dict: {'ok', 'message', 'status' (install_status dict),
    'topic' (the one to subscribe to), 'version' (installed)}; a helper
    error as _helper_failure() describes. A value the config renderer
    would refuse returns {'ok': False, 'message'} before the helper runs.
    """
    try:
        _validate_install_args(port, bind, base_url)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    status = install_status()

    if status["state"] == "external":
        return {
            "ok": False,
            "message": "ntfy is already installed outside of Aerodrome. "
                       "Aerodrome will use it; manage the service yourself.",
            "status": status,
        }

    if status["state"] == "aerodrome_managed":
        return {
            "ok": True,
            "message": "ntfy is already installed and managed by Aerodrome.",
            "status": status,
            "version": status.get("version"),
        }

    if not status["arch"]:
        return {
            "ok": False,
            "message": f"Unsupported architecture: {platform.machine()}. "
                       "ntfy has no release binary for this platform.",
            "status": status,
        }

    topic = topic or _generate_topic()
    if base_url is None:
        # Fresh install only: the LAN address phones can reach, else
        # localhost (the Notifications tab shows detected_lan_ip_error).
        lan_ip, _err = _detect_lan_ip()
        base_url = f"http://{lan_ip}:{port}"
        if not lan_ip or not base_url_ok(base_url):
            base_url = f"http://localhost:{port}"
    try:
        res = privhelper.run_helper("ntfy-install", "latest", "--port", str(port),
                                    "--bind", bind, "--base-url", base_url,
                                    "--upstream", "1" if upstream_relay else "0")
    except privhelper.HelperError as e:
        log.warning("ntfy install failed: %s", e)
        return _helper_failure(e, status=install_status())

    version = res.get("version")
    return {
        "ok": True,
        "message": f"ntfy {version} installed and started. "
                   f"Subscribe from the ntfy mobile app.",
        "status": install_status(),
        "topic": topic,
        "version": version,
        "port": port,
        "bind": bind,
    }


def _stored_invalid_message(keys) -> str:
    parts = []
    if "base-url" in keys:
        parts.append("Your saved notification server address is no longer "
                     "accepted — re-enter it.")
    other = [k for k in keys if k != "base-url"]
    if other:
        parts.append(f"The notification server's config file has a value Aerodrome "
                     f"can't carry over: {', '.join(other)}.")
        if "listen-http" in other:
            parts.append("Aerodrome keeps listen-http only as 0.0.0.0:PORT, "
                         "127.0.0.1:PORT or :PORT.")
        parts.append(f"Fix it in {CONFIG_FILE}, or uninstall and reinstall the "
                     f"notification server.")
    return " ".join(parts)


def update_config(base_url: Optional[str] = None,
                  upstream_relay: Optional[bool] = None,
                  port: Optional[int] = None,
                  bind: Optional[str] = None,
                  drop_custom_keys: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """(v2.40.5; reworked v3.4.146) Rewrite /etc/ntfy/server.yml from
    Aerodrome's managed values and restart ntfy. None keeps the live value
    for that field. Only for aerodrome_managed installs.

    The file is always re-rendered whole, so settings Aerodrome doesn't
    manage would be lost: they are dropped only when the caller names every
    one of them in drop_custom_keys (the custom_keys list a refusal
    returned). A setting added after that refusal isn't in the caller's
    list, so it brings a fresh refusal instead of being dropped unseen.
    Refusals write nothing and make no sudo call:
      code "invalid"              — a supplied value the renderer refuses;
      code "unreadable"           — the live file can't be read;
      code "custom_keys"          — the live file has settings Aerodrome
                                    doesn't manage (custom_keys: names) that
                                    drop_custom_keys doesn't name;
      code "stored_value_invalid" — a live value that can't be carried over
                                    (keys: names): a field left as None, or
                                    a listen-http Aerodrome can't represent
                                    (an IPv6, host-name or one-interface
                                    bind set by hand) that a supplied
                                    port/bind would replace.
    Names only in every response, never values.

    v3.4.150: the write is the helper's ntfy-config, which reads the file
    again as root and refuses (custom_keys) any setting the names it is
    given don't cover; writes server.yml and the unit together — an install
    still on the older root-run unit moves to the sandboxed one here — and
    restarts ntfy if it was running. If ntfy then doesn't answer, the
    helper puts the previous files back and this returns {'ok': False,
    'message'}. Other helper errors: _helper_failure().

    Returns {'ok', 'message', 'base_url', 'upstream_relay', 'port', 'bind',
    'dropped_keys'} on success.
    """
    status = install_status()
    if status["state"] != "aerodrome_managed":
        return {
            "ok": False,
            "message": "ntfy config is not managed by Aerodrome. "
                       "Edit /etc/ntfy/server.yml directly.",
        }

    try:
        if base_url is not None:
            validate_base_url(base_url)
        if port is not None or bind is not None:
            _validate_install_args(port if port is not None else 2586,
                                   bind if bind is not None else "0.0.0.0", None)
    except ValueError as e:
        return {"ok": False, "code": "invalid", "message": str(e)}

    cur = current_config()
    if cur is None:
        return {"ok": False, "code": "unreadable",
                "message": f"Couldn't read {CONFIG_FILE}, so nothing was changed."}

    confirmed = (set(drop_custom_keys)
                 if isinstance(drop_custom_keys, (list, tuple, set, frozenset)) else set())
    if set(cur["custom"]) - confirmed:
        return {"ok": False, "code": "custom_keys", "custom_keys": cur["custom"],
                "message": _custom_keys_message(cur["custom"])}

    # Fields the caller left as None come from the live file — only when
    # that value can be carried over exactly. listen-http is always checked:
    # left as None it is carried, and a supplied port/bind (restore) would
    # replace a hand-set value Aerodrome can't represent, which never happens
    # silently. base-url is checked only when left as None, so re-entering a
    # rejected address stays the way out.
    needed = {"listen-http"}
    if base_url is None:
        needed.add("base-url")
    if upstream_relay is None:
        needed.add("upstream-base-url")
    bad = sorted(needed & set(cur["problems"]))
    if bad:
        return {"ok": False, "code": "stored_value_invalid", "keys": bad,
                "message": _stored_invalid_message(bad)}

    base_url = cur["base_url"] if base_url is None else base_url
    port = cur["port"] if port is None else port
    bind = cur["bind"] if bind is None else bind
    upstream_relay = cur["upstream"] if upstream_relay is None else bool(upstream_relay)
    try:
        render_server_yml(port, bind, base_url, upstream_relay)     # what the helper writes
    except ValueError as e:
        return {"ok": False, "code": "invalid", "message": str(e)}

    args = ["--port", str(port), "--bind", bind, "--base-url", base_url,
            "--upstream", "1" if upstream_relay else "0"]
    if cur["custom"]:
        args += ["--drop-custom-keys", ",".join(cur["custom"])]
    try:
        res = privhelper.run_helper("ntfy-config", *args)
    except privhelper.HelperError as e:
        log.warning("ntfy config change failed: %s", e)
        return _helper_failure(e)

    return {
        "ok": True,
        "message": ("ntfy config updated and service restarted." if res.get("restarted")
                    else "ntfy config updated."),
        "base_url": base_url,
        "upstream_relay": upstream_relay,
        "port": port,
        "bind": bind,
        "dropped_keys": cur["custom"],
    }


def _read_server_bind() -> Optional[str]:
    """Bind address from listen-http ('0.0.0.0' or '127.0.0.1'), or None
    when the file is missing or the value can't be carried over."""
    cur = current_config()
    return cur["bind"] if cur else None


def _read_base_url() -> Optional[str]:
    """The live base-url, or None when it is missing or no longer passes
    validate_base_url()."""
    cur = current_config()
    return cur["base_url"] if cur else None


def _read_upstream_relay() -> bool:
    """True when server.yml relays to ntfy.sh. Defaults to True when the
    file or the value can't be read, so a failed read never shows the
    relay as off to a user who has it on."""
    cur = current_config()
    if cur is None or "upstream-base-url" in cur["problems"]:
        return True
    return cur["upstream"]


def upgrade() -> Dict[str, Any]:
    """Upgrade to the latest ntfy. Only works on aerodrome_managed installs.
    The helper's ntfy-install pulls the latest release, verifies it, swaps
    the binary and restarts the service if it was running — and nothing
    else: the unit and server.yml stay exactly as they are (an install on
    the older root-run unit moves only when its settings are saved).
    """
    status = install_status()
    if status["state"] != "aerodrome_managed":
        return {"ok": False,
                "message": f"Cannot upgrade from state '{status['state']}'. "
                           "Upgrade is only available for Aerodrome-managed installs.",
                "status": status}

    latest = latest_version()          # None: the helper looks it up itself
    current = status.get("version") or status["stamp"].get("version_installed")
    if latest and current == latest:
        return {"ok": True,
                "message": f"Already on the latest ntfy ({latest}).",
                "status": status, "version": latest}

    try:
        res = privhelper.run_helper("ntfy-install", "latest")
    except privhelper.HelperError as e:
        log.warning("ntfy upgrade failed: %s", e)
        return _helper_failure(e, status=install_status())
    return {"ok": True,
            "message": f"Upgraded ntfy {current or '?'} → {res.get('version')}.",
            "status": install_status(), "version": res.get("version")}


def uninstall(purge_data: bool = False) -> Dict[str, Any]:
    """Remove Aerodrome-managed ntfy install. Safe to run from any state —
    missing files are ignored. Refuses to touch external installs.

    purge_data (v2.41.0) — if True, also delete ntfy's cached messages
    (the helper's purge: the sandboxed unit's cache and the older root-run
    unit's, each with SQLite's -wal/-shm; never /var/cache/ntfy, an official
    package's). Default False so users who reinstall later keep their
    topic's message history. With nothing installed it deletes a cache an
    earlier install left. purged_data is True only when that went through.

    The helper removes the stamp last, and only when every earlier step
    worked: anything left behind keeps the install 'partial', whose panel
    offers Uninstall again — without the stamp a leftover binary would read
    as 'external' (never touched)."""
    status = install_status()
    if status["state"] == "external":
        return {"ok": False,
                "message": "Refusing to uninstall: ntfy was installed outside "
                           "Aerodrome. Manage it with your package manager.",
                "status": status}
    if status["state"] == "not_installed" and not purge_data:
        return {"ok": True, "message": "Nothing to uninstall.", "status": status,
                "purged_data": False}

    # A cache-file set by hand in server.yml (a custom setting) isn't
    # Aerodrome's to delete: the purge removes Aerodrome's caches only, and
    # the result says the hand-set one was left (its name, never its value).
    # Read before server.yml goes.
    hand_cache = purge_data and "cache-file" in ((current_config() or {}).get("custom") or ())

    try:
        res = privhelper.run_helper("ntfy-uninstall", *(["--purge"] if purge_data else []))
    except privhelper.HelperError as e:
        log.warning("ntfy uninstall failed: %s", e)
        return _helper_failure(e, status=install_status(), purged_data=False)

    if status["state"] == "not_installed":
        msg = "Nothing to uninstall."
        if any("cache.db" in p for p in res.get("removed") or ()):
            msg += " Cached messages from an earlier install were deleted."
    else:
        msg = "Uninstalled ntfy."
        if purge_data:
            msg += " Its cached messages were deleted too."
            if hand_cache:
                msg += " The cache file you set by hand in server.yml (cache-file) was left in place."
        else:
            msg += " Its cached messages were kept."
    return {"ok": True, "message": msg, "status": install_status(),
            "purged_data": purge_data}


def stale_data_present() -> bool:
    """(v2.41.0) True when a message cache an earlier install left is
    still there although no ntfy install is. Used by the install flow to
    tell the user about old cached messages (and how to delete them).
    v3.4.150: the helper's `version` answers (the cache is in a folder only
    root can see); False when it can't be asked."""
    if install_status()["state"] != "not_installed":
        return False
    try:
        return bool(privhelper.run_helper("version").get("ntfy_cache"))
    except privhelper.HelperError:
        return False
